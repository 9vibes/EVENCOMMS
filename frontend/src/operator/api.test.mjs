import assert from 'node:assert/strict';
import { test } from 'node:test';
import { characterCount, clientId, mergeMessages, request, ApiError } from './api.ts';

test('character limits count Unicode code points, including astral characters', () => {
  assert.equal(characterCount(''), 0);
  assert.equal(characterCount('hello'), 5);
  assert.equal(characterCount('\u{1f30d}'.repeat(4000)), 4000);
  assert.equal(characterCount('e\u0301'), 2);
  assert.equal(characterCount('x'.repeat(4001)), 4001);
});

test('client IDs are unique UUID v4 values, including on origins without randomUUID', () => {
  const ids = Array.from({ length: 100 }, () => clientId());
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) {
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  }
});

test('polling merges deduplicated history without dropping a newer reply acknowledgement', () => {
  const wearer = { id: 'a', text: 'Question', created_at: '2026-09-26T12:00:00Z' };
  const operator = { id: 'b', text: 'Reply', created_at: '2026-09-26T12:00:02Z' };
  const previous = Object.freeze([operator]);
  const incoming = Object.freeze([wearer]);
  const merged = mergeMessages(previous, incoming);
  assert.deepEqual(merged, [wearer, operator]);
  assert.deepEqual(mergeMessages(merged, [wearer, operator]), merged);
  assert.deepEqual(previous, [operator]);
  assert.deepEqual(incoming, [wearer]);
});

test('same-timestamp messages preserve server order and duplicates use the latest value', () => {
  const first = { id: 'z', text: 'First', created_at: '2026-09-26T12:00:00Z' };
  const second = { id: 'a', text: 'Second', created_at: first.created_at };
  assert.deepEqual(mergeMessages([], [first, second]), [first, second]);
  const updated = { ...first, text: 'Canonical server value' };
  assert.deepEqual(mergeMessages([first, second], [updated]), [updated, second]);
});


test('Research text arrives before completion, including split UTF-8 chunks', async () => {
  const original = globalThis.fetch;
  let controller;
  let received;
  const firstText = new Promise(resolve => { received = resolve; });
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers.Accept, 'application/x-ndjson');
    assert.equal(options.credentials, 'omit');
    return new Response(new ReadableStream({ start(value) { controller = value; } }), {
      headers: { 'Content-Type': 'application/x-ndjson' },
    });
  };
  try {
    let completed = false;
    const result = request('/chat', 'token', new AbortController().signal, { onText: received })
      .then(value => { completed = true; return value; });
    const data = new TextEncoder().encode(JSON.stringify({ type: 'text', text: 'Hello 🌍' }) + '\n');
    for (const byte of data) controller.enqueue(new Uint8Array([byte]));
    assert.equal(await firstText, 'Hello 🌍');
    assert.equal(completed, false);
    controller.enqueue(new TextEncoder().encode(JSON.stringify({ type: 'done', response: { text: 'Hello world!' } }) + '\n'));
    assert.deepEqual(await result, { text: 'Hello world!' });
  } finally { globalThis.fetch = original; }
});

test('Research never accepts a partial response or an error as a completed answer', async () => {
  const original = globalThis.fetch;
  try {
    for (const ending of ['', '{"type":"error","status":429,"detail":"Usage limit reached"}\n', '{bad json}\n']) {
      globalThis.fetch = async () => new Response('{"type":"text","text":"Partial"}\n' + ending, {
        headers: { 'Content-Type': 'application/x-ndjson' },
      });
      const seen = [];
      await assert.rejects(request('/chat', null, new AbortController().signal, { onText: text => seen.push(text) }),
        error => error instanceof ApiError && (ending.includes('429') ? error.status === 429 : error.status === 502));
      assert.deepEqual(seen, ['Partial']);
    }
  } finally { globalThis.fetch = original; }
});
