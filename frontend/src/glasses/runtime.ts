import {
  waitForEvenAppBridge, CreateStartUpPageContainer, TextContainerProperty, TextContainerUpgrade,
  MenuContainerProperty, MenuItemProperty, OsEventTypeList,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk'
import { ApiError, clientId, type Message } from '../operator/api.ts'
import { AudioSegments, DraftController, Gestures, gestureType, pages, type SavedDraft, type DraftState } from './draft.ts'

export type WearerState = {
  draft: DraftState; connected: boolean; listening: boolean; bridgeReady: boolean
  holding: boolean; status: string; error: string; reply: string; view: 'draft' | 'reply'; page: number
}

export class WearerRuntime {
  origin: string
  token: string
  simulated: boolean
  state: WearerState
  controller: DraftController
  segments: AudioSegments
  gestures: Gestures
  changed: (state: WearerState) => void
  bridge: EvenAppBridge | null = null
  socket: WebSocket | null = null
  unsubscribe: (() => void) | null = null
  reconnect: ReturnType<typeof setTimeout> | null = null
  heartbeat: ReturnType<typeof setInterval> | null = null
  renderTimer: ReturnType<typeof setTimeout> | null = null
  abort = new AbortController()
  closed = false
  fatal = false
  attempts = 0
  lastPong = 0
  lastReplyId = ''
  lastRender = ''
  rendering = false
  micWanted = false
  micQueue = Promise.resolve()
  connectingBridge = false

  constructor(options: { origin: string; token: string; simulated: boolean; saved: SavedDraft;
    persist: (saved: SavedDraft) => void; changed: (state: WearerState) => void }) {
    this.origin = options.origin
    this.token = options.token
    this.simulated = options.simulated
    this.changed = options.changed
    this.controller = new DraftController({ saved: options.saved,
      persist: saved => { if (!this.closed) options.persist(saved) },
      changed: draft => {
        if (this.closed) return
        this.state.draft = draft
        if (!draft.busy && !draft.error && !draft.text.trim() && this.state.status === 'Sending after transcription completes...') {
          this.state.status = 'No transcribed words to send. Resume listening.'
        }
        if (draft.error || draft.queuedAudio >= 2) {
          this.pause()
          if (!draft.error) this.state.status = 'Audio queue full. Wait for transcription, then resume.'
        }
        this.emit()
      },
      transcribe: async pcm => {
        const result = await this.request<{ text: string }>('/api/transcribe', {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: new Blob([new Uint8Array(pcm)]) })
        return result.text
      },
      send: async (text, id) => {
        await this.request('/api/messages', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, client_id: id }) })
        this.state.status = 'Question saved on Stone. Waiting for an operator reply.'
      },
    })
    this.state = { draft: this.controller.snapshot(), connected: false, listening: false, bridgeReady: options.simulated,
      holding: false, status: 'Connecting to Stone...', error: '', reply: '', view: 'draft', page: 0 }
    this.segments = new AudioSegments(pcm => this.controller.audio(pcm))
    this.gestures = new Gestures({ remove: () => this.deleteWord(), hold: () => this.beginHold(),
      send: () => this.releaseHold(), exit: () => this.exit(), page: delta => this.page(delta) })
  }

  emit() {
    if (this.closed) return
    this.changed({ ...this.state, draft: { ...this.state.draft } })
    this.scheduleRender()
  }

  async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    let response: Response
    try {
      response = await fetch(this.origin + path, { ...options, credentials: 'omit', cache: 'no-store',
        headers: { ...options.headers, Authorization: `Bearer ${this.token}` },
        signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(120000)]) })
    } catch { throw new Error('Stone unavailable or request timed out. Check the connection, then retry.') }
    if (!response.ok) {
      const error = await response.json().catch(() => null)
      if (response.status === 401) { this.fatal = true; this.pause() }
      throw new ApiError(error?.detail || `Request failed (${response.status}). Retry when the Stone is available.`, response.status)
    }
    return response.json() as Promise<T>
  }

  start() {
    this.connect()
    if (!this.simulated) void this.connectBridge()
    this.emit()
  }

  connect() {
    if (this.closed || this.fatal) return
    const socket = new WebSocket(this.origin.replace(/^http/, 'ws') + '/api/wearer')
    this.socket = socket
    socket.onopen = () => {
      if (this.closed) { socket.close(); return }
      socket.send(JSON.stringify({ type: 'auth', token: this.token }))
      this.lastPong = Date.now()
      this.heartbeat = setInterval(() => {
        if (Date.now() - this.lastPong > 45000) { socket.close(); return }
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'ping' }))
      }, 20000)
    }
    socket.onmessage = event => {
      if (this.closed || this.socket !== socket) return
      try {
        const data = JSON.parse(event.data)
        this.lastPong = Date.now()
        if (data.type === 'ready') {
          this.attempts = 0
          this.state.connected = true
          this.state.status = 'Connected. Start listening when ready.'
          const reply = [...(data.messages as Message[])].reverse().find(message => message.role === 'operator')
          if (reply) this.receiveReply(reply)
        } else if (data.type === 'message' && data.message.role === 'operator') this.receiveReply(data.message)
        this.emit()
      } catch { this.state.error = 'Unexpected server response. Reconnect to the Stone.'; this.emit() }
    }
    socket.onclose = event => {
      if (this.heartbeat) clearInterval(this.heartbeat)
      if (this.closed || this.socket !== socket) return
      this.state.connected = false
      this.pause()
      this.gestures.cancel()
      this.state.holding = false
      if ([4004, 4009, 4401, 4403, 1008].includes(event.code)) {
        this.fatal = true
        this.state.error = event.code === 4009 ? 'This pairing is open on another client. Close that client and reload to take over.'
          : 'Pairing revoked, expired, or origin rejected. Check setup or clear this device and pair again.'
      } else {
        this.state.status = 'Disconnected. Draft retained; reconnecting...'
        this.reconnect = setTimeout(() => this.connect(), Math.min(15000, 1000 * 2 ** this.attempts++))
      }
      this.emit()
    }
    socket.onerror = () => { this.state.status = 'Connection failed. Check Stone address, TLS and allowed origins.'; this.emit() }
  }

  receiveReply(message: Message) {
    if (message.id === this.lastReplyId) return
    this.lastReplyId = message.id
    this.state.reply = message.text
    this.state.view = 'reply'
    this.state.page = 0
    this.pause()
    this.state.status = 'Operator reply received. Listening paused.'
  }

  async connectBridge() {
    if (this.connectingBridge || this.bridge || this.closed) return
    this.connectingBridge = true
    this.state.error = ''
    this.state.status = 'Waiting for the Even app bridge...'
    this.emit()
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const bridge = await Promise.race([waitForEvenAppBridge(), new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('Even bridge not found. Open in Even Hub or use browser simulation.')), 10000)
      })])
      if (this.closed) return
      const result = await bridge.createStartUpPageContainer(new CreateStartUpPageContainer({
        containerTotalNum: 1,
        textObject: [new TextContainerProperty({ xPosition: 0, yPosition: 0, width: 576, height: 288,
          containerID: 1, containerName: 'comms', isEventCapture: 1, paddingLength: 8,
          content: 'EVENCOMMS\nReady. Open menu to start listening.' })],
        menuObject: new MenuContainerProperty({ menuItems: [
          new MenuItemProperty({ itemID: 1, itemName: 'Start listening' }),
          new MenuItemProperty({ itemID: 2, itemName: 'Pause listening' }),
          new MenuItemProperty({ itemID: 3, itemName: 'Show draft' }),
          new MenuItemProperty({ itemID: 4, itemName: 'Show reply' }),
          new MenuItemProperty({ itemID: 5, itemName: 'Retry failed work' }),
        ] }),
      }))
      if (result !== 0) throw new Error(`Could not create glasses display (${result}). Check the connected G2.`)
      this.bridge = bridge
      this.state.bridgeReady = true
      this.state.status = 'Glasses ready. Start listening when ready.'
      this.unsubscribe = bridge.onEvenHubEvent(event => {
        if (this.closed) return
        const type = gestureType(event)
        if (type === OsEventTypeList.SYSTEM_EXIT_EVENT || type === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
          this.close(); return
        }
        if (event.menuItemClickEvent) {
          this.gestures.cancel()
          this.state.holding = false
          switch (event.menuItemClickEvent.itemID) {
            case 1: void this.listen(); break
            case 2: this.pause(); break
            case 3: this.show('draft'); break
            case 4: this.show('reply'); break
            case 5: this.controller.retry(); break
          }
          return
        }
        if (type === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
          this.gestures.cancel(); this.state.holding = false; this.pause(); return
        }
        if (event.audioEvent?.audioPcm && this.state.listening && !this.state.holding) {
          try { this.segments.push(event.audioEvent.audioPcm) }
          catch (error) {
            this.pause()
            this.state.error = error instanceof Error ? error.message : 'Invalid microphone audio. Reconnect your glasses.'
            this.emit()
          }
        }
        this.gestures.handle(type)
      })
    } catch (error) {
      this.state.error = error instanceof Error ? error.message : 'Glasses connection failed.'
    } finally {
      if (timer) clearTimeout(timer)
      this.connectingBridge = false
      this.emit()
    }
  }

  async listen() {
    if (this.closed || !this.state.connected || !this.state.bridgeReady || this.controller.busy || this.controller.error || this.state.holding) return
    this.state.error = ''
    this.state.view = 'draft'
    this.state.page = Math.max(0, pages(this.controller.text).length - 1)
    this.micWanted = true
    this.micQueue = this.micQueue.then(async () => {
      if (!this.micWanted || this.closed) return
      if (!this.simulated && !await this.bridge!.audioControl(true)) throw new Error('Microphone permission denied or glasses unavailable.')
      if (this.micWanted && !this.closed) {
        this.state.listening = true
        this.state.status = this.simulated ? 'Simulation ready. Type a draft below.' : 'Listening. Audio stays on your local network.'
      }
    }).catch(error => { this.state.error = error.message; this.state.listening = false; this.micWanted = false })
    await this.micQueue
    this.emit()
  }

  pause() {
    const wasActive = this.micWanted || this.state.listening
    this.micWanted = false
    this.state.listening = false
    if (wasActive && this.bridge) {
      this.micQueue = this.micQueue.then(async () => {
        if (!await this.bridge!.audioControl(false)) throw new Error('Could not confirm microphone stopped. Close the app in Even Hub.')
      }).catch(error => { this.state.error = error.message; this.emit() })
    }
    this.segments.flush()
    if (wasActive) this.state.status = 'Listening paused.'
    this.emit()
  }

  deleteWord() {
    if (this.state.holding || this.controller.error) return
    this.pause()
    this.controller.deleteWord()
    this.state.view = 'draft'
    this.state.page = Math.max(0, pages(this.controller.text).length - 1)
    this.state.status = 'Last word deleted after pending transcription. Resume to speak a replacement.'
    this.emit()
  }

  beginHold() {
    if (this.closed || !this.state.connected || this.controller.error || this.state.holding) return
    this.state.holding = true
    this.pause()
    this.state.status = 'Release to send. Finalizing preceding speech...'
    this.emit()
  }

  releaseHold() {
    if (this.closed || !this.state.holding) return
    this.state.holding = false
    if (!this.state.connected) { this.emit(); return }
    if (!this.controller.text.trim() && !this.controller.snapshot().queuedAudio) {
      this.state.status = 'No draft to send. Start listening or type a question.'
      this.emit()
      return
    }
    this.controller.submit(clientId())
    this.state.status = 'Sending after transcription completes...'
    this.emit()
  }

  cancelHold() {
    this.gestures.cancel()
    this.state.holding = false
    this.state.status = 'Hold cancelled. Draft retained.'
    this.emit()
  }

  show(view: WearerState['view']) { this.state.view = view; this.state.page = 0; this.emit() }
  page(delta: number) {
    this.state.page = Math.min(Math.max(0, this.state.page + delta), this.displayPages().length - 1)
    this.emit()
  }
  displayPages() { return pages(this.state.view === 'reply' ? this.state.reply : this.controller.text) }
  display() {
    const content = this.displayPages()
    const page = Math.min(this.state.page, content.length - 1)
    const label = this.state.draft.error || this.state.error ? 'ERROR - CHECK PHONE'
      : this.state.holding ? 'RELEASE TO SEND' : this.controller.busy ? 'TRANSCRIBING / SENDING'
        : !this.state.connected ? 'DISCONNECTED' : this.state.listening ? 'LISTENING' : 'PAUSED'
    return `EVENCOMMS / ${label}\n${this.state.view.toUpperCase()} ${page + 1}/${content.length}\n${content[page] || (this.state.view === 'reply' ? 'Waiting for a reply.' : 'No draft. Start listening in menu.')}\nTap: delete word | Hold: send`
  }

  scheduleRender() {
    if (!this.bridge || this.renderTimer || this.rendering || this.closed) return
    this.renderTimer = setTimeout(async () => {
      this.renderTimer = null
      const text = this.display()
      if (text === this.lastRender || !this.bridge || this.closed) return
      this.rendering = true
      try {
        const ok = await this.bridge.textContainerUpgrade(new TextContainerUpgrade({
          containerID: 1, containerName: 'comms', content: text, contentOffset: 0, contentLength: text.length }))
        if (!ok) throw new Error('Display update was not acknowledged. Check the glasses connection.')
        this.lastRender = text
      } catch (error) {
        this.state.error = error instanceof Error ? error.message : 'Glasses display update failed.'
        this.changed({ ...this.state })
        return
      } finally { this.rendering = false }
      if (text !== this.display()) this.scheduleRender()
    }, 150)
  }

  exit() {
    this.pause()
    if (this.bridge) void this.bridge.shutDownPageContainer(1).catch(() => {
      this.state.error = 'Could not open exit confirmation. Close the app from its system menu.'; this.emit()
    })
    else { this.state.status = 'Double tap exits on G2. Simulation paused.'; this.emit() }
  }

  close() {
    this.pause()
    this.closed = true
    this.abort.abort()
    this.gestures.cancel()
    this.unsubscribe?.()
    if (this.reconnect) clearTimeout(this.reconnect)
    if (this.heartbeat) clearInterval(this.heartbeat)
    if (this.renderTimer) clearTimeout(this.renderTimer)
    this.socket?.close()
  }
}
