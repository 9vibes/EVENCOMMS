import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { WearerRuntime, type WearerState } from './runtime.ts'
import type { SavedDraft } from './draft.ts'
import './wearer.css'

const simulated = new URLSearchParams(location.search).get('simulate') === '1'
type Pairing = { token: string; session_id: string; name: string; draft: SavedDraft }
const emptyDraft: SavedDraft = { text: '', pending: null }

function validateOrigin(value: string) {
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Enter a full server origin without a path, e.g. https://stone.example.net')
  }
  if (location.protocol === 'https:' && url.protocol === 'http:') throw new Error('An HTTPS app cannot connect to an HTTP Stone. Use a trusted HTTPS address.')
  return url.origin
}

function App() {
  const [origin, setOrigin] = useState(location.origin)
  const [pairing, setPairing] = useState<Pairing | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [state, setState] = useState<WearerState | null>(null)
  const [code, setCode] = useState('')
  const [name, setName] = useState('G2 wearer')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const runtime = useRef<WearerRuntime | null>(null)
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const armed = useRef(false)
  const heldKey = useRef(false)

  useEffect(() => {
    const abort = new AbortController()
    void (async () => {
      try {
        const response = await fetch('./stone.json', { signal: abort.signal, cache: 'no-store' })
        const config = response.ok ? await response.json() : null
        const server = validateOrigin(config?.origin || localStorage.getItem('evencomms.origin') || location.origin)
        const saved = localStorage.getItem(`evencomms.wearer:${server}`)
        setOrigin(server)
        if (saved) {
          const value = JSON.parse(saved)
          if (typeof value.token === 'string' && typeof value.session_id === 'string' && typeof value.draft?.text === 'string') setPairing(value)
        }
      } catch (err) {
        if (!abort.signal.aborted) setError(err instanceof Error ? err.message : 'Could not load saved pairing. Check browser storage access.')
      } finally { if (!abort.signal.aborted) setLoaded(true) }
    })()
    return () => abort.abort()
  }, [])

  useEffect(() => {
    if (!pairing) return
    const connection = new WearerRuntime({ origin, token: pairing.token, simulated, saved: pairing.draft,
      changed: setState,
      persist: draft => {
        try { localStorage.setItem(`evencomms.wearer:${origin}`, JSON.stringify({ ...pairing, draft })) }
        catch { setError('Browser storage is unavailable. Draft recovery after reload is not guaranteed. Keep this page open.') }
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

  async function pair(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const server = validateOrigin(origin.trim())
      const response = await fetch(server + '/api/pair', { method: 'POST', credentials: 'omit',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code, name }), signal: AbortSignal.timeout(15000) })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || 'Pairing failed')
      const next: Pairing = { ...result, name, draft: emptyDraft }
      localStorage.setItem(`evencomms.wearer:${server}`, JSON.stringify(next))
      localStorage.setItem('evencomms.origin', server)
      setOrigin(server)
      setPairing(next)
      setCode('')
    } catch (err) { setError(err instanceof Error ? err.message : 'Pairing failed. Check the Stone address and code.') }
    finally { setBusy(false) }
  }

  function clear() {
    if (!confirm('Clear this device pairing and draft? Pending audio is lost. The server conversation is not deleted.')) return
    runtime.current?.close()
    try { localStorage.removeItem(`evencomms.wearer:${origin}`); localStorage.removeItem('evencomms.origin') }
    catch { setError('Could not clear browser storage. Clear this site data in browser settings.'); return }
    setPairing(null)
    setState(null)
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

  return <main className="wearer">
    <header className="wearer-header"><a href="/">KUNAS<span> / EVENCOMMS</span></a><span className="wearer-tag">{simulated ? 'BROWSER SIMULATION' : 'G2 COMPANION'}</span></header>
    <section className="wearer-intro"><p className="eyebrow">A DIRECT LINE TO YOUR STONE</p><h1>Speak. Correct.<br /><em>Stay connected.</em></h1>
      <p>One tap removes one word. Hold continuously, then release to send.</p></section>
    {error && <div className="wearer-error" role="alert">{error}</div>}
    {!loaded ? <p>Loading local pairing...</p> : !pairing ? <form className="wearer-card pairing" onSubmit={pair}>
      <h2>Pair your connection</h2><p>Ask the operator for a one-use pairing code. No glasses? Open <a href="/glasses.html?simulate=1">browser simulation</a>.</p>
      <label>Stone address<input type="url" value={origin} onChange={event => setOrigin(event.target.value)} required autoComplete="url" /></label>
      <label>Your name<input value={name} maxLength={80} onChange={event => setName(event.target.value)} required autoComplete="nickname" /></label>
      <label>Pairing code<input value={code} onChange={event => setCode(event.target.value.toUpperCase())} maxLength={8} minLength={8} required autoComplete="off" autoCapitalize="characters" spellCheck={false} /></label>
      <button className="wearer-primary" disabled={busy}>{busy ? 'Pairing...' : 'Connect to Stone'}</button>
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
