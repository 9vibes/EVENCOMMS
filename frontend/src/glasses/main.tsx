import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { WearerRuntime, type WearerState } from './runtime.ts'
import type { SavedDraft } from './draft.ts'
import { checkConnection, ConnectionError, connectionDetails, pairingCredentials, pairWithPrecheck, validateOrigin } from './connection.ts'
import './wearer.css'

declare const __EVENCOMMS_PACKAGED_ORIGIN__: string
const packagedOrigin = typeof __EVENCOMMS_PACKAGED_ORIGIN__ === 'undefined' ? undefined : __EVENCOMMS_PACKAGED_ORIGIN__
const simulated = new URLSearchParams(location.search).get('simulate') === '1'
type Pairing = { token: string; session_id: string; name: string; draft: SavedDraft }
const emptyDraft: SavedDraft = { text: '', pending: null }

function App() {
  const [origin, setOrigin] = useState('')
  const [pairing, setPairing] = useState<Pairing | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [state, setState] = useState<WearerState | null>(null)
  const [code, setCode] = useState('')
  const [name, setName] = useState('G2 wearer')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'check' | 'pair' | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [checkStatus, setCheckStatus] = useState('')
  const busyRef = useRef(false)
  const storageAvailable = useRef(true)
  const runtime = useRef<WearerRuntime | null>(null)
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const armed = useRef(false)
  const heldKey = useRef(false)

  useEffect(() => {
    const abort = new AbortController()
    const timer = setTimeout(() => abort.abort(), 8000)
    let cancelled = false
    void (async () => {
      let configured: unknown = packagedOrigin
      let savedOrigin: string | null = null
      let warning = ''
      try { savedOrigin = localStorage.getItem('evencomms.origin') }
      catch {
        storageAvailable.current = false
        warning = 'Browser storage is unavailable. Enter or check the Stone address to pair; pairing and drafts will be memory-only.'
      }
      // A packaged public origin must not depend on fetching local JSON in an opaque/file WebView.
      if (packagedOrigin === undefined) {
        try {
          const response = await fetch('./stone.json', { signal: abort.signal, cache: 'no-store' })
          const config = response.ok ? await response.json() : null
          configured = config?.origin
        } catch { /* Normal web builds can still use a saved target or their HTTP(S) page origin. */ }
      }
      clearTimeout(timer)
      if (cancelled) return
      let server = ''
      for (const candidate of [configured, savedOrigin, location.origin]) {
        try { server = validateOrigin(candidate, location.protocol); break }
        catch { /* Invalid candidates are never used as server addresses. */ }
      }
      setOrigin(server)
      if (!server) warning ||= 'No valid Stone address is available. Enter a full HTTP(S) Stone origin to pair.'
      if (server && storageAvailable.current) {
        let saved: string | null = null
        try { saved = localStorage.getItem(`evencomms.wearer:${server}`) }
        catch {
          storageAvailable.current = false
          warning = 'Browser storage is unavailable. The Stone address is ready, but pairing and drafts will be memory-only.'
        }
        if (saved) {
          try {
            const value = JSON.parse(saved)
            const credentials = pairingCredentials(value)
            const draft = value?.draft
            if (!credentials || typeof value.name !== 'string' || typeof draft?.text !== 'string' ||
                !(draft.pending === null || typeof draft.pending?.text === 'string' && typeof draft.pending?.id === 'string')) throw new Error()
            setPairing({ token: credentials.token, session_id: credentials.session_id, name: value.name,
              draft: { text: draft.text, pending: draft.pending ? { text: draft.pending.text, id: draft.pending.id } : null } })
          } catch { warning = 'Saved pairing could not be read. Check browser storage before pairing again; a saved draft may still be present.' }
        }
      }
      setError(warning)
      setLoaded(true)
    })()
    return () => { cancelled = true; clearTimeout(timer); abort.abort() }
  }, [])

  useEffect(() => { if (error || state?.error) setDetailsOpen(true) }, [error, state?.error])

  useEffect(() => {
    if (!pairing) return
    const connection = new WearerRuntime({ origin, token: pairing.token, simulated, saved: pairing.draft,
      changed: setState,
      persist: draft => {
        if (!storageAvailable.current) return
        try { localStorage.setItem(`evencomms.wearer:${origin}`, JSON.stringify({ ...pairing, draft })) }
        catch {
          storageAvailable.current = false
          setError('Browser storage is unavailable. Draft recovery after reload is not guaranteed. Keep this page open.')
        }
      },
    })
    runtime.current = connection
    connection.start()
    const hide = () => {
      if (document.visibilityState === 'hidden') {
        stopHold(true)
        connection.cancelHold()
        connection.pause()
      }
    }
    const blur = () => stopHold(true)
    const restore = (event: PageTransitionEvent) => { if (event.persisted) location.reload() }
    const exit = () => connection.close()
    const unload = (event: BeforeUnloadEvent) => {
      if (connection.controller.busy || connection.controller.snapshot().queuedAudio || connection.segments.samples.length) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    document.addEventListener('visibilitychange', hide)
    window.addEventListener('pagehide', exit)
    window.addEventListener('pageshow', restore)
    window.addEventListener('blur', blur)
    window.addEventListener('beforeunload', unload)
    return () => {
      connection.close()
      runtime.current = null
      document.removeEventListener('visibilitychange', hide)
      window.removeEventListener('pagehide', exit)
      window.removeEventListener('pageshow', restore)
      window.removeEventListener('blur', blur)
      window.removeEventListener('beforeunload', unload)
    }
  }, [pairing, origin])

  useEffect(() => () => { if (holdTimer.current) clearTimeout(holdTimer.current) }, [])

  function pair(event: FormEvent) {
    event.preventDefault()
    void connect(true)
  }

  async function connect(shouldPair: boolean) {
    if (busyRef.current || !loaded || pairing) return
    busyRef.current = true
    setBusy(shouldPair ? 'pair' : 'check')
    setError('')
    setCheckStatus(shouldPair ? 'Checking /health before sending the pairing request...' : 'Checking /health without credentials...')
    if (!shouldPair) setDetailsOpen(true)
    try {
      if (shouldPair) {
        const result = await pairWithPrecheck(origin, location.protocol, { code, name })
        const next: Pairing = { token: result.token, session_id: result.session_id, name, draft: emptyDraft }
        if (storageAvailable.current) {
          try {
            localStorage.setItem(`evencomms.wearer:${result.origin}`, JSON.stringify(next))
            localStorage.setItem('evencomms.origin', result.origin)
          } catch { storageAvailable.current = false }
        }
        if (!storageAvailable.current) setError('Paired for this page only: browser storage is unavailable. Do not pair again now. Keep this page open; pairing and draft recovery after reload are not guaranteed.')
        setOrigin(result.origin)
        setPairing(next)
        setCode('')
      } else setOrigin(await checkConnection(origin, location.protocol))
      setCheckStatus('Connection check passed: /health returned JSON status "ok".')
    } catch (err) {
      setCheckStatus('')
      setError(err instanceof ConnectionError ? err.message : 'Connection attempt failed. Check the Stone address and connection details before trying again.')
    } finally { busyRef.current = false; setBusy(null) }
  }

  function clear() {
    if (!confirm('Clear this device pairing and draft? Pending audio is lost. The server conversation is not deleted.')) return
    stopHold(true)
    runtime.current?.close()
    try {
      localStorage.removeItem(`evencomms.wearer:${origin}`)
      localStorage.removeItem('evencomms.origin')
      setError('')
    } catch {
      storageAvailable.current = false
      setError('Pairing cleared for this page only. Browser storage could not be cleared; saved pairing or drafts may return after reload. Clear this site data in browser settings.')
    }
    setPairing(null)
    setState(null)
    setCheckStatus('')
  }

  function startHold() {
    if (holdTimer.current || armed.current) return
    // The phone/browser button models a continuous hold; a quick click never sends.
    holdTimer.current = setTimeout(() => {
      holdTimer.current = null
      if (document.visibilityState === 'hidden' || !runtime.current || runtime.current.closed) return
      armed.current = true
      runtime.current?.beginHold()
    }, 650)
  }

  function stopHold(cancel = false) {
    if (holdTimer.current) clearTimeout(holdTimer.current)
    holdTimer.current = null
    if (armed.current) {
      if (cancel) runtime.current?.cancelHold()
      else runtime.current?.releaseHold()
    }
    armed.current = false
    heldKey.current = false
  }

  const disabled = !state?.connected || !!state.draft.error || state.holding
  const details = connectionDetails(location, window.origin, origin)

  return <main className="wearer">
    <header className="wearer-header"><strong className="wearer-brand">KUNAS<span> / EVENCOMMS</span></strong><span className="wearer-tag">{simulated ? 'BROWSER SIMULATION' : 'G2 COMPANION'}</span></header>
    {error && <div className="wearer-error" role="alert">{error}</div>}
    <details className="wearer-card connection-details" open={detailsOpen} onToggle={event => setDetailsOpen(event.currentTarget.open)}>
      <summary>Connection details</summary>
      <dl>
        <dt>Page URL origin (location.origin)</dt><dd><code>{details.pageOrigin}</code></dd>
        <dt>Browser-effective origin (window.origin)</dt><dd><code>{details.browserOrigin}</code></dd>
        <dt>Scheme (location.protocol)</dt><dd><code>{details.protocol}</code></dd>
        <dt>Stone target</dt><dd><code>{details.stoneOrigin}</code></dd>
        <dt>Pair endpoint</dt><dd><code>{details.pairEndpoint}</code></dd>
      </dl>
      {checkStatus && <p className="wearer-small" role="status">{checkStatus}</p>}
      <p className="wearer-small">A readable /health response does not prove pairing POST or WebSocket access. An opaque origin is reported as null, not inferred from the page URL.</p>
    </details>
    {!loaded ? <p>Loading local pairing...</p> : !pairing ? <form className="wearer-card pairing" onSubmit={pair}>
      <h2>Pair your connection</h2><p>Ask the operator for a one-use pairing code. No glasses? Open <a href="?simulate=1">browser simulation</a>.</p>
      <label>Stone address<input type="url" value={origin} disabled={!!busy} onChange={event => { setOrigin(event.target.value); setCheckStatus('') }} required autoComplete="url" /></label>
      <label>Your name<input value={name} disabled={!!busy} maxLength={80} onChange={event => setName(event.target.value)} required autoComplete="nickname" /></label>
      <label>Pairing code<input value={code} disabled={!!busy} onChange={event => setCode(event.target.value.toUpperCase())} maxLength={8} minLength={8} required autoComplete="off" autoCapitalize="characters" spellCheck={false} /></label>
      <div className="wearer-actions pairing-actions">
        <button className="wearer-primary" disabled={!!busy}>{busy === 'pair' ? 'Checking / pairing...' : 'Connect to Stone'}</button>
        <button type="button" disabled={!!busy} onClick={() => void connect(false)}>{busy === 'check' ? 'Checking...' : 'Check connection'}</button>
      </div>
      <p className="wearer-small">Use trusted HTTPS for private conversations. HTTP is for isolated LAN development only.</p>
    </form> : state && <>
      <section className="wearer-card">
        <div className="wearer-row"><h2>{pairing.name}</h2><span className={state.connected ? 'connection online' : 'connection'}>{state.connected ? 'STONE CONNECTED' : 'OFFLINE'}</span></div>
        <p className="wearer-status" role="status">{state.draft.busy ? 'Processing preceding speech / sending...' : state.status}</p>
        {state.error && <p className="wearer-error" role="alert">{state.error}</p>}
        {state.draft.error && <div className="wearer-error" role="alert"><p>{state.draft.error}</p><div className="wearer-actions">
          {(state.draft.pending || state.draft.queuedAudio > 0) && <button onClick={() => runtime.current?.controller.retry()} disabled={!state.connected || state.draft.busy}>Retry safely</button>}
          {!state.draft.pending && <button disabled={state.draft.busy} onClick={() => {
            if (!state.draft.queuedAudio || confirm('Discard pending audio and queued actions? Your transcribed draft will remain.')) runtime.current?.controller.discardAudio()
          }}>{state.draft.queuedAudio ? 'Discard pending audio' : 'Keep draft / edit'}</button>}
        </div></div>}
        <div className="glasses-preview" aria-label="Glasses text preview"><pre>{runtime.current?.display()}</pre></div>
        <div className="wearer-actions wearer-tabs"><button aria-pressed={state.view === 'draft'} onClick={() => runtime.current?.show('draft')}>Draft</button><button aria-pressed={state.view === 'reply'} onClick={() => runtime.current?.show('reply')}>Reply</button>
          <button aria-label="Previous page" onClick={() => runtime.current?.page(-1)}>Previous</button><button aria-label="Next page" onClick={() => runtime.current?.page(1)}>Next</button></div>
        <div className="wearer-actions main-controls">
          <button disabled={state.holding || (!state.listening && (disabled || state.draft.busy || !state.bridgeReady))} onClick={() => state.listening ? runtime.current?.pause() : void runtime.current?.listen()}>{state.listening ? 'Pause listening' : 'Start listening'}</button>
          <button disabled={!!state.draft.error || state.holding} onClick={() => runtime.current?.deleteWord()}>Tap / Delete word</button>
          <button className={`wearer-primary hold-button ${state.holding ? 'holding' : ''}`} disabled={!state.connected || !!state.draft.error || !!state.draft.pending}
            onPointerDown={event => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); startHold() }}
            onPointerUp={() => stopHold()} onPointerCancel={() => stopHold(true)} onLostPointerCapture={() => stopHold(true)}
            onContextMenu={event => event.preventDefault()} onBlur={() => stopHold(true)}
            onKeyDown={event => { if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) { event.preventDefault(); heldKey.current = true; startHold() } }}
            onKeyUp={event => { if (heldKey.current && (event.key === ' ' || event.key === 'Enter')) { event.preventDefault(); stopHold() } }}>
            {state.holding ? 'Release to send' : 'Hold to send'}</button>
        </div>
        {!state.bridgeReady && !simulated && <button onClick={() => void runtime.current?.connectBridge()}>Reconnect Even bridge</button>}
        <p className="wearer-small">Corrections pause listening so pending speech can finish before deletion. Resume to speak a replacement. Double-tap exits on G2; tap-then-hold opens the system menu, not Send.</p>
      </section>
      <section className="wearer-card"><p className="eyebrow">{simulated ? 'SIMULATED TRANSCRIPT' : 'PRIVATE DRAFT / PHONE EDITOR'}</p>
        <label htmlFor="draft">Unsent text</label><textarea id="draft" rows={5} maxLength={4000} value={state.draft.text}
          disabled={state.draft.busy || !!state.draft.error || state.listening && !simulated || state.holding}
          onChange={event => runtime.current?.controller.edit(event.target.value)} placeholder={simulated ? 'Type what the glasses would transcribe, then test delete and hold-to-send.' : 'Speak on G2, or type here while listening is paused.'} />
        <div className="wearer-row"><span className="wearer-small">Only submitted text is visible to the operator.</span><span className="wearer-small">{Array.from(state.draft.text).length} / 4000</span></div>
        {state.reply && <div className="phone-reply"><p className="eyebrow">LATEST OPERATOR REPLY</p><p>{state.reply}</p></div>}
      </section>
      <footer className="wearer-row"><span className="wearer-small">Audio retries are memory-only. Keep this page open.</span><button className="wearer-clear" onClick={clear}>Clear device</button></footer>
    </>}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
