import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, characterCount, clientId, errorText, request } from './api';
import { canEnterResearchKey as isTrustedConsole, researchBudget } from './research';
import type { ResearchMessage } from './research';
import StreamTab from './StreamTab';
import type { CapturedFrame } from './videoView';

interface CodexStatus {
  enabled: boolean;
  state: 'disconnected' | 'pending' | 'connected' | 'failed';
  verification_url: string | null;
  user_code: string | null;
  generation_enabled: boolean;
}
interface CodexModel { id: string; image: boolean }
const DEVICE_URL = 'https://auth.openai.com/codex/device';
interface ChatResponse {
  request_id: string;
  model: string;
  text: string;
  incomplete: boolean;
  usage: { input_tokens: number; output_tokens: number; total_tokens: number } | null;
}
interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
  model: string;
  frames?: CapturedFrame[];
  response?: ChatResponse;
}

export default function ResearchTab({ token, onUnauthorized, active }: {
  token: string; onUnauthorized: () => void; active: boolean;
}) {
  const [model, setModel] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [revision, setRevision] = useState(0);
  const [codexStatus, setCodexStatus] = useState<CodexStatus | null>(null);
  const [codexModels, setCodexModels] = useState<CodexModel[]>([]);
  const [codexBusy, setCodexBusy] = useState(false);
  const codexRequest = useRef<AbortController | null>(null);
  const codexPolling = useRef(false);
  const codexAction = useRef<'login' | 'disconnect' | null>(null);
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = useState('');
  const [frames, setFrames] = useState<CapturedFrame[]>([]);
  const [busy, setBusy] = useState(false);
  const [streamText, setStreamText] = useState('');
  const [capturing, setCapturing] = useState(false);
  const [captureRevision, setCaptureRevision] = useState(0);
  const captureInProgress = useRef(false);
  const captureStateChanged = useCallback((pending: boolean) => {
    captureInProgress.current = pending;
    setCapturing(pending);
  }, []);
  const [chatError, setChatError] = useState('');
  const [notice, setNotice] = useState('');
  const chatRequest = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const attempt = useRef<{ content: string; id: string } | null>(null);
  const historyView = useRef<HTMLDivElement | null>(null);
  const followHistory = useRef(true);
  const trustedConsole = isTrustedConsole(window.location);

  // A single cancellable loop owns Codex connection work, never the chat request.
  // Commands consume their intent once, so returning to the tab cannot replay login.
  useEffect(() => {
    if (!active) { codexAction.current = null; return; }
    if (chatRequest.current) return;
    const controller = new AbortController();
    codexRequest.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    let action = codexAction.current;
    codexAction.current = null;
    let status = codexStatus;
    const current = () => !controller.signal.aborted && codexRequest.current === controller;
    async function poll() {
      if (!current() || chatRequest.current) return;
      codexPolling.current = true;
      setCodexBusy(true);
      setConnectionError('');
      try {
        status = await request<CodexStatus>(`/api/research/codex/${action === 'disconnect' ? 'connection' : action ?? 'status'}`, token, controller.signal,
          action === 'login' ? { method: 'POST', body: {} } : action === 'disconnect' ? { method: 'DELETE' } : {});
        if (!current()) return;
        setCodexStatus(status);
        if (status.enabled === true && status.state === 'connected' && status.generation_enabled === true) {
          const result = await request<{ models: CodexModel[] }>('/api/research/codex/models', token, controller.signal);
          if (!current()) return;
          setCodexModels(result.models);
          setModel(previous => result.models.some(item => item.id === previous) ? previous : '');
        } else {
          setCodexModels([]);
          setModel('');
        }
      } catch (failure) {
        if (!current()) return;
        setCodexModels([]);
        setModel('');
        if (failure instanceof ApiError && failure.status === 401) { onUnauthorized(); return; }
        if (!action && failure instanceof ApiError && failure.status === 404) {
          status = { enabled: false, state: 'disconnected', verification_url: null, user_code: null, generation_enabled: false };
        } else {
          setConnectionError(errorText(failure));
          // Keep cancellation available after an uncertain login, but hide old codes.
          status = status ? { ...status, verification_url: null, user_code: null, generation_enabled: false } : null;
        }
        setCodexStatus(status);
      } finally {
        if (current()) {
          action = null;
          codexPolling.current = false;
          setCodexBusy(false);
          if (status?.enabled && (status.state === 'pending' || status.state === 'connected'))
            timer = setTimeout(() => void poll(), status.state === 'pending' ? 2000 : 30000);
        }
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
      if (codexRequest.current === controller) {
        codexRequest.current = null;
        codexPolling.current = false;
        setCodexBusy(false);
      }
    };
  }, [active, token, onUnauthorized, revision, busy]);

  useEffect(() => () => {
    generation.current++;
    chatRequest.current?.abort();
    codexRequest.current?.abort();
  }, []);

  useEffect(() => {
    if (active && followHistory.current && historyView.current)
      historyView.current.scrollTop = historyView.current.scrollHeight;
  }, [history, active, streamText]);

  function changeCodexConnection(action: 'login' | 'disconnect' | null) {
    if (!active) return;
    if (action !== 'disconnect' && (chatRequest.current || codexPolling.current)) return;
    if (action === 'login' && (!codexStatus?.enabled || codexStatus.state === 'pending' || codexStatus.state === 'connected')) return;
    if (action === 'login' && !trustedConsole && !window.confirm('This console uses unencrypted HTTP. Its operator session and one-time code can be intercepted. Continue only on a trusted LAN or VPN. OpenAI sign-in itself opens over HTTPS. Request a Codex login code?')) return;
    if (action === 'disconnect') {
      if ((history.length || prompt || frames.length || busy || capturing) && !window.confirm('Disconnect ChatGPT and clear this chat, question and captured frames? A pending request may still consume plan allowance. Cleanup is best effort.')) return;
      clearChat(true);
      setNotice('ChatGPT disconnected locally. Server cleanup is best effort; no API fallback.');
    }
    codexRequest.current?.abort();
    codexRequest.current = null;
    codexAction.current = action;
    setCodexBusy(true);
    setCodexModels([]);
    setModel('');
    setConnectionError('');
    if (action) setCodexStatus({ enabled: true, state: action === 'login' ? 'pending' : 'disconnected', verification_url: null, user_code: null, generation_enabled: false });
    setRevision(value => value + 1);
  }

  const messages: ResearchMessage[] = [...history.map(message => ({
    role: message.role, text: message.text,
    ...(message.role === 'user' ? { images: (message.frames ?? []).map(frame => frame.dataUrl) } : {}),
  })), { role: 'user', text: prompt, images: frames.map(frame => frame.dataUrl) }];
  const budgetError = researchBudget(messages);
  const totalImages = history.reduce((count, message) => count + (message.frames?.length ?? 0), frames.length);
  const codexModel = codexModels.find(item => item.id === model);
  const imageError = totalImages > 0 && codexModel && !codexModel.image
    ? 'The selected Codex model does not support images. Choose an image-capable model, discard draft frames or start a New chat to clear image history.' : '';
  const providerReady = !codexBusy && codexStatus?.enabled === true && codexStatus.state === 'connected' && codexStatus.generation_enabled === true && !!codexModel;
  const canSend = active && !busy && !capturing && providerReady && !budgetError && !imageError;

  async function send() {
    if (!canSend || captureInProgress.current || chatRequest.current || codexPolling.current) return;
    const content = JSON.stringify({ model, messages });
    // Keep the UUID for an unchanged request after an uncertain response. This is
    // only a bounded server cache, not a guarantee against duplicate billing.
    if (attempt.current && !window.confirm('The previous reply was not confirmed. Retrying, or sending an edited request, may consume more plan allowance, including after a failure, cache expiry or restart. Send via Codex? No paid API fallback.')) return;
    const currentAttempt = attempt.current?.content === content ? attempt.current : { content, id: clientId() };
    attempt.current = currentAttempt;
    const controller = new AbortController();
    const currentGeneration = ++generation.current;
    chatRequest.current = controller;
    followHistory.current = true;
    setBusy(true);
    setStreamText('');
    setChatError('');
    setNotice('');
    try {
      const result = await request<ChatResponse>('/api/research/codex/chat', token, controller.signal, {
        method: 'POST', body: { request_id: currentAttempt.id, model, messages },
        onText: text => {
          if (!controller.signal.aborted && generation.current === currentGeneration) setStreamText(text);
        },
      });
      if (controller.signal.aborted || generation.current !== currentGeneration) return;
      setHistory([...history, { role: 'user', text: prompt, frames, model }, {
        role: 'assistant', text: result.text, model: result.model, response: result,
      }]);
      setPrompt('');
      setFrames([]);
      attempt.current = null;
      setNotice(result.incomplete ? 'Codex returned an incomplete reply. EVENCOMMS did not submit another request.' : 'Reply received. Nothing was sent to the glasses.');
    } catch (failure) {
      if (controller.signal.aborted || generation.current !== currentGeneration) return;
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized();
      else setChatError(`Reply not confirmed. Your draft and frames are kept. ${errorText(failure)}`);
    } finally {
      if (!controller.signal.aborted && generation.current === currentGeneration) {
        chatRequest.current = null;
        setBusy(false);
        setStreamText('');
      }
    }
  }

  function newChat() {
    if ((history.length || prompt || frames.length || busy || capturing) && !window.confirm(busy
      ? 'Stop waiting and clear this chat? Codex may still process the request and consume plan allowance. A late reply will be discarded.'
      : 'Clear this chat, question and captured frames? This cannot be undone.')) return;
    clearChat();
    setNotice('New chat. Local history and frames cleared.');
  }

  function clearChat(cancelCapture = false) {
    generation.current++;
    chatRequest.current?.abort();
    chatRequest.current = null;
    attempt.current = null;
    followHistory.current = true;
    setBusy(false);
    setHistory([]);
    setStreamText('');
    setPrompt('');
    setFrames([]);
    setChatError('');
    if (cancelCapture) {
      captureInProgress.current = false;
      setCapturing(false);
      setCaptureRevision(value => value + 1);
    }
  }

  const captureGeneration = generation.current;
  function capture(frame: CapturedFrame) {
    if (!active || chatRequest.current || generation.current !== captureGeneration) return;
    const sentImages = history.reduce((count, message) => count + (message.frames?.length ?? 0), 0);
    setFrames(previous => previous.length >= 3 || sentImages + previous.length >= 6 ? previous : [...previous, frame]);
  }

  const deviceInstructions = codexStatus?.state === 'pending' && codexStatus.verification_url === DEVICE_URL
    && typeof codexStatus.user_code === 'string' && /^[A-Za-z0-9][A-Za-z0-9-]{0,31}$/.test(codexStatus.user_code);

  return <div className="op-research">
    <section className="op-panel op-research-chat" aria-labelledby="research-title">
      <div className="op-conversation-heading">
        <div><span className="op-eyebrow">OPENAI / OPERATOR ONLY</span><h2 id="research-title">Research chat</h2></div>
        <button className="op-button" onClick={newChat}>New chat</button>
      </div>
      <div className="op-research-history" ref={historyView} role="log" aria-label="Research conversation" aria-live={active ? 'polite' : 'off'} tabIndex={0}
        onScroll={() => {
          const element = historyView.current;
          if (element) followHistory.current = element.scrollHeight - element.scrollTop - element.clientHeight < 60;
        }}>
        {!history.length && !busy && <div className="op-empty-conversation"><h3>A question, with context.</h3><p>Ask with text alone, or capture a still frame from the preview. Review it here before sending.</p></div>}
        <ol className="op-message-list">
          {history.map((message, index) => <li key={index} className={`op-message ${message.role === 'user' ? 'op-message-operator' : ''}`}>
            <div className="op-message-meta"><span>{message.role === 'user' ? 'YOU' : 'CODEX'}</span><span>Model: {message.model}</span></div>
            {message.text && <p tabIndex={0}>{message.text}</p>}
            {!!message.frames?.length && <div className="op-research-frames">{message.frames.map(frame => <figure key={frame.id}><img src={frame.dataUrl} alt="Sent frame" /><figcaption><time dateTime={frame.capturedAt}>{new Date(frame.capturedAt).toLocaleString()}</time></figcaption></figure>)}</div>}
            {message.response?.incomplete && <span className="op-fine-print">Incomplete response</span>}
            {message.response?.usage && <div className="op-fine-print">Tokens: {message.response.usage.input_tokens} input / {message.response.usage.output_tokens} output / {message.response.usage.total_tokens} total</div>}
          </li>)}
          {busy && <>
            <li className="op-message op-message-operator"><div className="op-message-meta"><span>YOU</span><span>Model: {model}</span></div><p>{prompt}</p></li>
            <li className="op-message" aria-busy="true"><div className="op-message-meta"><span>CODEX</span><span>{streamText ? 'Receiving reply…' : 'Waiting for Codex…'}</span></div><p>{streamText || 'Your reply will appear here as it is generated.'}</p></li>
          </>}
        </ol>
      </div>
      <form className="op-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
        <div className="op-composer-label"><label htmlFor="research-prompt">Research question</label><span id="research-count" className={characterCount(prompt) > 8000 ? 'op-error-text' : ''}>{characterCount(prompt).toLocaleString()} / 8,000</span></div>
        <textarea id="research-prompt" value={prompt} readOnly={busy} placeholder="What would you like to understand?" aria-describedby="research-count research-budget" aria-invalid={characterCount(prompt) > 8000} onChange={event => setPrompt(event.target.value)} onKeyDown={event => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
        }} />
        <div className="op-research-frames" aria-label="Draft frames">{frames.map((frame, index) => <figure key={frame.id}>
          <img src={frame.dataUrl} alt={`Draft frame ${index + 1}`} />
          <figcaption><time dateTime={frame.capturedAt}>{new Date(frame.capturedAt).toLocaleString()}</time><span>{frame.width} x {frame.height} / local draft</span></figcaption>
          <button type="button" className="op-text-button" disabled={busy} onClick={() => setFrames(previous => previous.filter(item => item.id !== frame.id))}>Discard frame {index + 1}</button>
        </figure>)}</div>
        <p className="op-fine-print" id="research-budget">{messages.length} / 20 request messages / {totalImages} / 6 frames ({frames.length} / 3 this turn). Full history is sent, never silently shortened.</p>
        {budgetError && (prompt || frames.length || messages.length > 20) && <p className="op-error-text" role="status">{budgetError}</p>}
        {imageError && <p className="op-error-text" role="status">{imageError}</p>}
        {chatError && <div className="op-error-banner" role="alert"><p>{chatError}</p><p>Retry is manual and may consume more plan allowance, including with the same request ID after a failure, cache expiry or server restart. No paid API fallback.</p></div>}
        <div className="op-composer-toolbar"><span className="op-fine-print">Ctrl / Cmd + Enter to send</span><button type="submit" className="op-button op-button-acid" disabled={!canSend}>{busy ? 'Waiting for Codex...' : 'Send via Codex'}</button></div>
        <p className="op-fine-print" role="status">{capturing ? 'Preparing your captured frame. Review its thumbnail before sending.' : busy ? 'You can change tabs while waiting. New chat stops waiting, but Codex may still process this request and consume plan allowance.' : notice}</p>
      </form>
    </section>
    <aside className="op-research-sidebar" aria-label="Research connection and live preview">
      <section className="op-panel op-research-settings">
        <fieldset>
          <legend>ChatGPT account / Codex</legend>
          <p role="status">{codexBusy ? 'Checking Codex connection...' : !codexStatus ? 'Codex status unavailable' : !codexStatus.enabled ? 'Codex account login is disabled on this server.' : codexStatus.state === 'connected' ? 'ChatGPT connected' : codexStatus.state === 'pending' ? 'Waiting for ChatGPT sign-in' : codexStatus.state === 'failed' ? 'ChatGPT sign-in failed' : 'ChatGPT disconnected'}</p>
          <p className="op-fine-print">Experimental Codex connection. Uses ChatGPT plan allowance; see the setup and privacy guide: <code>docs/codex.md</code>.</p>
          {codexStatus?.enabled && codexStatus.state === 'connected' && !codexStatus.generation_enabled && <p className="op-error-text" role="status">Runtime verification did not pass; sending disabled.</p>}
          <div className="op-research-actions">
            <button type="button" className="op-button" disabled={busy || codexBusy || !codexStatus?.enabled || codexStatus.state === 'pending' || codexStatus.state === 'connected'} onClick={() => changeCodexConnection('login')}>Get Codex login code</button>
            {codexStatus?.enabled !== false && <button type="button" className="op-button" disabled={codexBusy && codexStatus?.state === 'disconnected'} onClick={() => changeCodexConnection('disconnect')}>{codexStatus?.state === 'pending' ? 'Cancel sign-in' : 'Disconnect ChatGPT'}</button>}
          </div>
          {!trustedConsole && <p className="op-fine-print">This console uses unencrypted HTTP. Code login is available after a trusted-network confirmation, but your operator session and code can be intercepted. Prefer HTTPS; never use this on a public network.</p>}
          {codexStatus?.state === 'pending' && <>
            {deviceInstructions ? <div className="op-pair-code">
              <span>Enter this one-time Codex code on OpenAI's device sign-in page (expires in 15 minutes):</span>
              <strong aria-label="ChatGPT device code">{codexStatus.user_code}</strong>
              <a className="op-text-button" href={DEVICE_URL} target="_blank" rel="noreferrer noopener" referrerPolicy="no-referrer">Open OpenAI device sign-in</a>
            </div> : <p className="op-fine-print">OpenAI device instructions are unavailable or invalid. Waiting for valid instructions; you can cancel sign-in.</p>}
            <p className="op-fine-print">Sign in personally on OpenAI's site. Enable device code authentication in your ChatGPT security settings if required. Leaving this tab or aborting a browser request does not cancel server sign-in; use Cancel sign-in.</p>
          </>}
          <label htmlFor="research-codex-model">Codex model selector</label>
          <select id="research-codex-model" value={model} disabled={busy || codexBusy || !codexModels.length} onChange={event => setModel(event.target.value)}>
            <option value="">Select a Codex model</option>
            {codexModels.map(item => <option key={item.id} value={item.id}>{item.id}{item.image ? ' / images supported' : ' / text only'}</option>)}
          </select>
          <button type="button" className="op-text-button" disabled={busy || codexBusy} onClick={() => changeCodexConnection(null)}>Refresh Codex connection</button>
        </fieldset>
        {connectionError && <p className="op-error-text" role="alert">{connectionError}</p>}
      </section>
      <section className="op-panel op-research-preview" aria-label="Research live preview">
        {active && <StreamTab key={captureRevision} token={token} onUnauthorized={onUnauthorized} compact onCapture={capture} onCaptureStateChange={captureStateChanged} captureDisabled={busy || frames.length >= 3 || totalImages >= 6} />}
        <p className="op-fine-print">Capture adds a local draft frame, never sends it. Text chat works without a stream.</p>
      </section>
    </aside>
  </div>;
}
