export type SavedDraft = { text: string; pending: { text: string; id: string } | null }
type Operation = { kind: 'audio'; pcm: Uint8Array } | { kind: 'delete' } | { kind: 'send'; id: string }
export type DraftState = SavedDraft & { busy: boolean; error: string; queuedAudio: number }

export function deleteLastWord(text: string): string {
  return text.trimEnd().replace(/\S+\s*$/, '').trimEnd()
}

export function pages(text: string, limit = 180): string[] {
  const result: string[] = []
  // Flatten whitespace on the HUD so pasted newlines cannot overflow its small canvas.
  let remaining = text.trim().replace(/\s+/g, ' ')
  while (remaining) {
    let end = Math.min(remaining.length, limit)
    if (end < remaining.length) {
      const space = remaining.lastIndexOf(' ', end)
      if (space > end / 2) end = space
    }
    result.push(remaining.slice(0, end))
    remaining = remaining.slice(end).trimStart()
  }
  return result.length ? result : ['']
}

// Audio, corrections and sends share one queue: a late ASR result cannot undo a correction.
export class DraftController {
  text: string
  pending: SavedDraft['pending']
  error = ''
  busy = false
  queue: Operation[] = []
  transcribe: (pcm: Uint8Array) => Promise<string>
  send: (text: string, id: string) => Promise<void>
  changed: (state: DraftState) => void
  persist: (state: SavedDraft) => void

  constructor(options: {
    saved: SavedDraft
    transcribe: (pcm: Uint8Array) => Promise<string>
    send: (text: string, id: string) => Promise<void>
    changed: (state: DraftState) => void
    persist: (state: SavedDraft) => void
  }) {
    this.text = options.saved.text
    this.pending = options.saved.pending
    this.transcribe = options.transcribe
    this.send = options.send
    this.changed = options.changed
    this.persist = options.persist
    if (this.pending) {
      this.text = this.pending.text
      this.queue.push({ kind: 'send', id: this.pending.id })
      this.error = 'Previous send was not confirmed. Retry safely before editing.'
    }
  }

  snapshot(): DraftState {
    return { text: this.text, pending: this.pending, error: this.error, busy: this.busy,
      queuedAudio: this.queue.filter(op => op.kind === 'audio').length }
  }

  update() {
    this.persist({ text: this.text, pending: this.pending })
    this.changed(this.snapshot())
  }

  edit(text: string) {
    if (this.busy || this.queue.length) return
    this.text = text
    this.update()
  }

  audio(pcm: Uint8Array) {
    if (!pcm.length) return
    if (pcm.length > 480000 || this.queue.filter(op => op.kind === 'audio').length >= 3) {
      throw new Error('Audio limit reached. Capture stopped; the overflowing audio was not retained. Review your draft before sending.')
    }
    this.queue.push({ kind: 'audio', pcm })
    void this.run()
  }

  deleteWord() {
    if (this.error) return
    this.queue.push({ kind: 'delete' })
    void this.run()
  }

  submit(id: string) {
    if (this.error || this.queue.some(op => op.kind === 'send')) return
    this.queue.push({ kind: 'send', id })
    void this.run()
  }

  retry() {
    this.error = ''
    void this.run()
  }

  discardAudio() {
    if (this.busy || this.pending) return
    // Also drop queued gestures: discarding speech must never trigger a partial send.
    this.queue = []
    this.error = ''
    this.update()
  }

  async run() {
    if (this.busy || this.error) { this.update(); return }
    this.busy = true
    this.update()
    try {
      while (this.queue.length) {
        const op = this.queue[0]
        if (op.kind === 'audio') {
          const result = (await this.transcribe(op.pcm)).trim()
          const next = [this.text, result].filter(Boolean).join(' ')
          if (Array.from(next).length > 4000) throw new Error('Draft is full. Discard pending audio, then send or edit the draft.')
          this.text = next
        } else if (op.kind === 'delete') {
          this.text = deleteLastWord(this.text)
        } else if (this.text.trim()) {
          this.pending ??= { text: this.text.trim(), id: op.id }
          this.update()
          await this.send(this.pending.text, this.pending.id)
          this.text = ''
          this.pending = null
        }
        this.queue.shift()
        this.update()
      }
    } catch (error) {
      if (this.queue[0]?.kind === 'send' && error instanceof Error && 'status' in error &&
          typeof error.status === 'number' && error.status >= 400 && error.status < 500) {
        // A received 4xx is a definite rejection, unlike a lost acknowledgement.
        this.pending = null
        this.queue.shift()
      }
      this.error = error instanceof Error ? error.message : 'Request failed. Retry or discard pending audio.'
    } finally {
      this.busy = false
      this.update()
    }
  }
}

// Silence segmentation is a transport heuristic; Whisper's VAD handles recognition.
export class AudioSegments {
  samples: number[] = []
  preRoll: number[] = []
  silence = 0
  emit: (pcm: Uint8Array) => void

  constructor(emit: (pcm: Uint8Array) => void) { this.emit = emit }

  push(pcm: Uint8Array) {
    if (pcm.byteLength % 2) throw new Error('Invalid PCM frame length')
    if (pcm.byteLength > 64000) throw new Error('Oversized microphone frame. Capture stopped; this frame was not retained. Review your draft.')
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength)
    // Fixed 20ms windows also bound unusually large SDK frames.
    for (let offset = 0; offset < pcm.byteLength; offset += 640) {
      const frame: number[] = []
      for (let i = offset; i < Math.min(offset + 640, pcm.byteLength); i += 2) frame.push(view.getInt16(i, true))
      const rms = Math.sqrt(frame.reduce((sum, sample) => sum + sample * sample, 0) / frame.length)
      if (!this.samples.length && rms < 280) {
        this.preRoll = [...this.preRoll, ...frame].slice(-3200)
        continue
      }
      if (!this.samples.length) { this.samples.push(...this.preRoll); this.preRoll = [] }
      this.samples.push(...frame)
      this.silence = rms < 280 ? this.silence + frame.length : 0
      if (this.samples.length >= 160000 || this.silence >= 10400) this.flush()
    }
  }

  flush() {
    const samples = this.samples
    this.samples = []
    this.preRoll = []
    this.silence = 0
    if (!samples.length) return
    const pcm = new Uint8Array(samples.length * 2)
    const view = new DataView(pcm.buffer)
    samples.forEach((sample, index) => view.setInt16(index * 2, sample, true))
    this.emit(pcm)
  }
}

export type InputEvent = {
  sysEvent?: { eventType?: number }
  textEvent?: { eventType?: number }
  listEvent?: { eventType?: number }
}

export function gestureType(event: InputEvent): number | null {
  // Proto omits CLICK=0. Default only inside a real envelope, never on audio events.
  const envelopes = [event.sysEvent, event.textEvent, event.listEvent].filter(item => item !== undefined)
  return envelopes.find(item => item!.eventType !== undefined)?.eventType ?? (envelopes.length ? 0 : null)
}

export class Gestures {
  timer: ReturnType<typeof setTimeout> | null = null
  holding = false
  ignoreClickUntil = 0
  callbacks: { remove: () => void; hold: () => void; send: () => void; exit: () => void; page: (delta: number) => void }

  constructor(callbacks: Gestures['callbacks']) { this.callbacks = callbacks }

  cancel() {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.holding = false
  }

  handle(type: number | null) {
    if (type === 9) {
      if (this.timer) clearTimeout(this.timer)
      this.timer = null
      if (!this.holding) { this.holding = true; this.callbacks.hold() }
    } else if (type === 10) {
      if (this.holding) {
        this.holding = false
        this.ignoreClickUntil = Date.now() + 500
        this.callbacks.send()
      }
    } else if (type === 3) {
      this.cancel()
      this.ignoreClickUntil = Date.now() + 500
      this.callbacks.exit()
    } else if (type === 0 && !this.holding && Date.now() >= this.ignoreClickUntil) {
      if (this.timer) clearTimeout(this.timer)
      this.timer = setTimeout(() => { this.timer = null; this.callbacks.remove() }, 450)
    } else if (type === 1 || type === 2) this.callbacks.page(type === 1 ? -1 : 1)
  }
}
