import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'
import { AudioSegments, DraftController, deleteLastWord, pages, gestureType, Gestures } from './draft.ts'

const tick = () => delay(0)
const options = (overrides = {}) => ({ saved: { text: '', pending: null }, transcribe: async () => 'hello world',
  send: async () => {}, changed: () => {}, persist: () => {}, ...overrides })

test('one tap removes exactly one trailing word, including punctuation', () => {
  assert.equal(deleteLastWord('hello wrong!  '), 'hello')
  assert.equal(deleteLastWord('word'), '')
  assert.equal(deleteLastWord(''), '')
})
test('late transcription completes before delete and send', async () => {
  let resolve
  const sent = []
  const controller = new DraftController(options({ transcribe: () => new Promise(r => { resolve = r }),
    send: async (text, id) => { sent.push([text, id]) } }))
  controller.audio(new Uint8Array([1, 0]))
  controller.deleteWord()
  controller.submit('id-1')
  controller.submit('id-2')
  resolve('correct wrong')
  await tick()
  assert.deepEqual(sent, [['correct', 'id-1']])
  assert.equal(controller.text, '')
})
test('failed audio blocks submission and retry preserves ordering', async () => {
  let fail = true
  const sent = []
  const controller = new DraftController(options({ transcribe: async () => { if (fail) throw new Error('offline'); return 'question' },
    send: async text => { sent.push(text) } }))
  controller.audio(new Uint8Array([1, 0]))
  controller.submit('one')
  await tick()
  assert.equal(controller.error, 'offline')
  assert.deepEqual(sent, [])
  fail = false
  controller.retry()
  await tick()
  assert.deepEqual(sent, ['question'])
})
test('discarding failed audio never triggers a partial queued send', async () => {
  const sent = []
  const controller = new DraftController(options({ saved: { text: 'draft', pending: null },
    transcribe: async () => { throw new Error('offline') }, send: async text => { sent.push(text) } }))
  controller.audio(new Uint8Array([1, 0]))
  controller.submit('one')
  await tick()
  controller.discardAudio()
  await tick()
  assert.equal(controller.text, 'draft')
  assert.deepEqual(sent, [])
})
test('unconfirmed sends survive reload and reuse the same id', async () => {
  let saved
  const controller = new DraftController(options({ saved: { text: 'question', pending: null },
    send: async () => { throw new Error('connection lost') }, persist: state => { saved = state } }))
  controller.submit('stable')
  await tick()
  const sent = []
  const restored = new DraftController(options({ saved, send: async (text, id) => { sent.push([text, id]) } }))
  restored.edit('different')
  assert.equal(restored.text, 'question')
  restored.retry()
  await tick()
  assert.deepEqual(sent, [['question', 'stable']])
})
test('ASR overflow preserves original draft and retains failed audio', async () => {
  const controller = new DraftController(options({ saved: { text: 'x'.repeat(4000), pending: null } }))
  controller.audio(new Uint8Array([1, 0]))
  await tick()
  assert.equal(controller.text.length, 4000)
  assert.equal(controller.snapshot().queuedAudio, 1)
  assert.match(controller.error, /full/)
})
test('audio and lifecycle envelopes are not mistaken for taps', () => {
  assert.equal(gestureType({ audioEvent: {} }), null)
  assert.equal(gestureType({ sysEvent: {} }), 0)
  assert.equal(gestureType({ textEvent: { eventType: 0 } }), 0)
  assert.equal(gestureType({ sysEvent: { eventType: 9 }, textEvent: {} }), 9)
})
test('hold release sends once and suppresses click; double tap exits without delete', async () => {
  const events = []
  const gestures = new Gestures({ remove: () => events.push('delete'), hold: () => events.push('hold'),
    send: () => events.push('send'), exit: () => events.push('exit'), page: () => {} })
  gestures.handle(0)
  gestures.handle(9)
  gestures.handle(9)
  gestures.handle(10)
  gestures.handle(10)
  gestures.handle(0)
  await delay(480)
  assert.deepEqual(events, ['hold', 'send'])
  gestures.handle(3)
  await delay(480)
  assert.deepEqual(events, ['hold', 'send', 'exit'])
})
test('standalone single tap deletes once; orphan release never sends', async () => {
  let removed = 0
  const gestures = new Gestures({ remove: () => removed++, hold: () => {}, send: () => assert.fail(), exit: () => {}, page: () => {} })
  gestures.handle(10)
  gestures.handle(0)
  await delay(480)
  assert.equal(removed, 1)
})
test('audio segmentation bounds utterances and omits silence-only input', () => {
  const chunks = []
  const segments = new AudioSegments(pcm => chunks.push(pcm))
  segments.push(new Uint8Array(32000))
  segments.flush()
  assert.equal(chunks.length, 0)
  const loud = new Uint8Array(640000)
  const view = new DataView(loud.buffer)
  for (let i = 0; i < loud.length; i += 2) view.setInt16(i, 1000, true)
  for (let offset = 0; offset < loud.length; offset += 64000) segments.push(loud.subarray(offset, offset + 64000))
  segments.flush()
  assert.ok(chunks.length >= 2)
  assert.ok(chunks.every(pcm => pcm.length <= 320640))
})
test('oversized frames and full audio queues are rejected explicitly', () => {
  const segments = new AudioSegments(() => assert.fail())
  assert.throws(() => segments.push(new Uint8Array(64002)), /not retained/)
  const controller = new DraftController(options({ transcribe: () => new Promise(() => {}) }))
  for (let i = 0; i < 3; i++) controller.audio(new Uint8Array([1, 0]))
  assert.throws(() => controller.audio(new Uint8Array([1, 0])), /not retained/)
  assert.equal(controller.snapshot().queuedAudio, 3)
})
test('definitive send rejection can be dismissed without discarding draft', async () => {
  const controller = new DraftController(options({ saved: { text: 'keep me', pending: null }, send: async () => {
    throw Object.assign(new Error('Conversation limit reached'), { status: 409 })
  } }))
  controller.submit('id')
  await tick()
  assert.equal(controller.pending, null)
  controller.discardAudio()
  controller.edit('edited draft')
  assert.equal(controller.text, 'edited draft')
})
test('pagination never discards long words or remaining pages', () => {
  const text = 'x'.repeat(401)
  assert.equal(pages(text).join(''), text)
  assert.ok(pages(text).every(page => page.length <= 180))
  assert.equal(pages('first\n\nsecond\tthird')[0], 'first second third')
})
