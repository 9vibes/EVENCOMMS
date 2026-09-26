import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, characterCount, clientId, errorText, request } from './api';
import { canEnterResearchKey, researchBudget } from './research';
import type { ResearchMessage } from './research';
import StreamTab from './StreamTab';
import type { CapturedFrame } from './videoView';

interface Connection { configured: boolean; key_source: 'server' | 'session' | null }
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
  const [connection, setConnection] = useState<Connection | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [validated, setValidated] = useState(false);
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [connectionBusy, setConnectionBusy] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [revision, setRevision] = useState(0);
  const connectionRequest = useRef<AbortController | null>(null);
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = useState('');
  const [frames, setFrames] = useState<CapturedFrame[]>([]);
  const [busy, setBusy] = useState(false);
  const [capturing, setCapturing] = useState(false);
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
  const secureKeyEntry = canEnterResearchKey(window.location);

  // Refresh and connection mutations share one slot. Aborted/stale responses never
  // replace a newer credential's model list, even after a rapid tab switch.
  useEffect(() => {
    setApiKey('');
    if (!active || chatRequest.current) return;
    const controller = new AbortController();
    connectionRequest.current = controller;
    setConnectionBusy(true);
    setConnectionError('');
    setValidated(false);
    setModels([]);
    void (async () => {
      try {
        const status = await request<Connection>('/api/research/status', token, controller.signal);
        if (controller.signal.aborted || connectionRequest.current !== controller) return;
        setConnection(status);
        if (status.configured) {
          const result = await request<{ models: { id: string }[] }>('/api/research/models', token, controller.signal);
          if (controller.signal.aborted || connectionRequest.current !== controller) return;
          setModels(result.models.map(item => item.id));
          setValidated(true);
        }
      } catch (failure) {
        if (controller.signal.aborted || connectionRequest.current !== controller) return;
        if (failure instanceof ApiError && failure.status === 401) onUnauthorized();
        else setConnectionError(errorText(failure));
      } finally {
        if (!controller.signal.aborted && connectionRequest.current === controller) {
          connectionRequest.current = null;
          setConnectionBusy(false);
        }
      }
    })();
    return () => {
      connectionRequest.current?.abort();
      connectionRequest.current = null;
    };
  }, [active, token, onUnauthorized, revision]);

  useEffect(() => () => {
    generation.current++;
    chatRequest.current?.abort();
    connectionRequest.current?.abort();
  }, []);

  useEffect(() => {
    if (active && followHistory.current && historyView.current)
      historyView.current.scrollTop = historyView.current.scrollHeight;
  }, [history, active]);

  async function changeConnection(method: 'POST' | 'DELETE') {
    if (!active || chatRequest.current || connectionRequest.current || (method === 'POST' && (!secureKeyEntry || !apiKey.trim()))) return;
    const controller = new AbortController();
    connectionRequest.current = controller;
    setConnectionBusy(true);
    setConnectionError('');
    try {
      const status = await request<Connection>('/api/research/connection', token, controller.signal, {
        method, ...(method === 'POST' ? { body: { api_key: apiKey.trim() } } : {}),
      });
      if (controller.signal.aborted || connectionRequest.current !== controller) return;
      setApiKey('');
      setConnection(status);
      setValidated(false);
      setModels([]);
      setRevision(value => value + 1);
    } catch (failure) {
      if (controller.signal.aborted || connectionRequest.current !== controller) return;
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized();
      else setConnectionError(errorText(failure));
    } finally {
      if (!controller.signal.aborted && connectionRequest.current === controller) {
        connectionRequest.current = null;
        setConnectionBusy(false);
      }
    }
  }

  const messages: ResearchMessage[] = [...history.map(message => ({
    role: message.role, text: message.text,
    ...(message.role === 'user' ? { images: (message.frames ?? []).map(frame => frame.dataUrl) } : {}),
  })), { role: 'user', text: prompt, images: frames.map(frame => frame.dataUrl) }];
  const budgetError = researchBudget(messages);
  const totalImages = history.reduce((count, message) => count + (message.frames?.length ?? 0), frames.length);
  const canSend = active && !busy && !capturing && !connectionBusy && connection?.configured && models.includes(model) && !budgetError;

  async function send() {
    if (!canSend || captureInProgress.current || chatRequest.current || connectionRequest.current) return;
    const content = JSON.stringify({ model, messages });
    // Keep the UUID for an unchanged request after an uncertain response. This is
    // only a bounded server cache, not a guarantee against duplicate billing.
    if (attempt.current && !window.confirm('The previous reply was not confirmed. Retrying, or sending an edited request, may bill again. Failed requests and server restarts are not protected by the recent-result cache. Send to OpenAI?')) return;
    const currentAttempt = attempt.current?.content === content ? attempt.current : { content, id: clientId() };
    attempt.current = currentAttempt;
    const controller = new AbortController();
    const currentGeneration = ++generation.current;
    chatRequest.current = controller;
    followHistory.current = true;
    setBusy(true);
    setChatError('');
    setNotice('');
    try {
      const result = await request<ChatResponse>('/api/research/chat', token, controller.signal, {
        method: 'POST', body: { request_id: currentAttempt.id, model, messages },
      });
      if (controller.signal.aborted || generation.current !== currentGeneration) return;
      setHistory([...history, { role: 'user', text: prompt, frames, model }, {
        role: 'assistant', text: result.text, model: result.model, response: result,
      }]);
      setPrompt('');
      setFrames([]);
      attempt.current = null;
      setNotice(result.incomplete ? 'OpenAI returned an incomplete reply. No automatic retry was made.' : 'Reply received. Nothing was sent to the glasses.');
    } catch (failure) {
      if (controller.signal.aborted || generation.current !== currentGeneration) return;
      if (failure instanceof ApiError && failure.status === 401) onUnauthorized();
      else setChatError(`Reply not confirmed. Your draft and frames are kept. ${errorText(failure)}`);
    } finally {
      if (!controller.signal.aborted && generation.current === currentGeneration) {
        chatRequest.current = null;
        setBusy(false);
      }
    }
  }

  function newChat() {
    if ((history.length || prompt || frames.length || busy || capturing) && !window.confirm(busy
      ? 'Stop waiting and clear this chat? OpenAI may still process and bill the request. A late reply will be discarded.'
      : 'Clear this chat, question and captured frames? This cannot be undone.')) return;
    generation.current++;
    chatRequest.current?.abort();
    chatRequest.current = null;
    attempt.current = null;
    followHistory.current = true;
    setBusy(false);
    setHistory([]);
    setPrompt('');
    setFrames([]);
    setChatError('');
    setNotice('New chat. Local history and frames cleared.');
  }

  const captureGeneration = generation.current;
  function capture(frame: CapturedFrame) {
    if (!active || chatRequest.current || generation.current !== captureGeneration) return;
    const sentImages = history.reduce((count, message) => count + (message.frames?.length ?? 0), 0);
    setFrames(previous => previous.length >= 3 || sentImages + previous.length >= 6 ? previous : [...previous, frame]);
  }

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
        {!history.length && <div className="op-empty-conversation"><h3>A question, with context.</h3><p>Ask with text alone, or capture a still frame from the preview. Review it here before sending.</p></div>}
        <ol className="op-message-list">
          {history.map((message, index) => <li key={index} className={`op-message ${message.role === 'user' ? 'op-message-operator' : ''}`}>
            <div className="op-message-meta"><span>{message.role === 'user' ? 'YOU' : 'OPENAI'}</span><span>Model: {message.model}</span></div>
            {message.text && <p tabIndex={0}>{message.text}</p>}
            {!!message.frames?.length && <div className="op-research-frames">{message.frames.map(frame => <figure key={frame.id}><img src={frame.dataUrl} alt="Sent frame" /><figcaption><time dateTime={frame.capturedAt}>{new Date(frame.capturedAt).toLocaleString()}</time></figcaption></figure>)}</div>}
            {message.response?.incomplete && <span className="op-fine-print">Incomplete response</span>}
            {message.response?.usage && <div className="op-fine-print">Tokens: {message.response.usage.input_tokens} input / {message.response.usage.output_tokens} output / {message.response.usage.total_tokens} total</div>}
          </li>)}
        </ol>
      </div>
      <form className="op-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
        <div className="op-composer-label"><label htmlFor="research-prompt">Research question</label><span id="research-count" className={characterCount(prompt) > 8000 ? 'op-error-text' : ''}>{characterCount(prompt).toLocaleString()} / 8,000</span></div>
        <textarea id="research-prompt" value={prompt} readOnly={busy} placeholder="What would you like to understand?" aria-describedby="research-count research-budget research-privacy" aria-invalid={characterCount(prompt) > 8000} onChange={event => setPrompt(event.target.value)} onKeyDown={event => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
        }} />
        <div className="op-research-frames" aria-label="Draft frames">{frames.map((frame, index) => <figure key={frame.id}>
          <img src={frame.dataUrl} alt={`Draft frame ${index + 1}`} />
          <figcaption><time dateTime={frame.capturedAt}>{new Date(frame.capturedAt).toLocaleString()}</time><span>{frame.width} x {frame.height} / local draft</span></figcaption>
          <button type="button" className="op-text-button" disabled={busy} onClick={() => setFrames(previous => previous.filter(item => item.id !== frame.id))}>Discard frame {index + 1}</button>
        </figure>)}</div>
        <p className="op-fine-print" id="research-budget">{messages.length} / 20 request messages / {totalImages} / 6 frames ({frames.length} / 3 this turn). Full history is sent, never silently shortened.</p>
        {budgetError && (prompt || frames.length || messages.length > 20) && <p className="op-error-text" role="status">{budgetError}</p>}
        {chatError && <div className="op-error-banner" role="alert"><p>{chatError}</p><p>Retry is manual and may bill again, including with the same request ID after a failure, cache expiry or server restart.</p></div>}
        <div className="op-composer-toolbar"><span className="op-fine-print">Ctrl / Cmd + Enter to send</span><button type="submit" className="op-button op-button-acid" disabled={!canSend}>{busy ? 'Waiting for OpenAI...' : 'Send to OpenAI'}</button></div>
        <p className="op-fine-print" role="status">{capturing ? 'Preparing your captured frame. Review its thumbnail before sending.' : busy ? 'You can change tabs while waiting. New chat stops waiting, but OpenAI may still process and bill this request.' : notice}</p>
      </form>
    </section>
    <aside className="op-research-sidebar" aria-label="Research connection and live preview">
      <section className="op-panel op-research-settings">
        <fieldset disabled={busy || connectionBusy}>
          <legend>OpenAI connection</legend>
          <p role="status">{connectionBusy ? 'Checking connection...' : !connection ? 'Connection status unavailable' : !connection.configured ? 'Not configured' : `${validated ? 'Connected / models validated' : 'Configured / models not validated'} / ${connection.key_source === 'session' ? 'current sign-in key' : 'server-managed key'}`}</p>
          <p className="op-fine-print">A ChatGPT subscription is not an API key. A key entered here is held in server memory for this sign-in only, and also clears on server restart.</p>
          <label htmlFor="research-key">OpenAI API key</label>
          <input id="research-key" type="password" autoComplete="off" spellCheck={false} maxLength={512} value={apiKey} disabled={!secureKeyEntry} onChange={event => setApiKey(event.target.value)} />
          {!secureKeyEntry && <p className="op-fine-print">Browser key entry requires HTTPS or localhost. Configure OPENAI_API_KEY on the server instead.</p>}
          <div className="op-research-actions"><button type="button" className="op-button" disabled={!secureKeyEntry || !apiKey.trim()} onClick={() => void changeConnection('POST')}>Connect for this sign-in</button>{connection?.key_source === 'session' && <button type="button" className="op-button" onClick={() => void changeConnection('DELETE')}>Remove sign-in key</button>}</div>
          <p className="op-fine-print">Removing a sign-in key falls back to any server-managed key. It does not revoke that key at OpenAI or disable the server key.</p>
          <label htmlFor="research-model">OpenAI model</label>
          <select id="research-model" value={model} onChange={event => setModel(event.target.value)}>
            <option value="">Select an available model</option>
            {model && !models.includes(model) && <option value={model} disabled>{model} (not currently available)</option>}
            {models.map(id => <option key={id} value={id}>{id}</option>)}
          </select>
          <button type="button" className="op-text-button" onClick={() => { if (!connectionRequest.current) setRevision(value => value + 1); }}>Refresh models</button>
          <p className="op-fine-print">All IDs returned by OpenAI are listed. Availability does not prove Responses API or image compatibility. No model is selected or substituted automatically.</p>
        </fieldset>
        {connectionError && <p className="op-error-text" role="alert">{connectionError}</p>}
      </section>
      <section className="op-panel op-research-preview" aria-label="Research live preview">
        {active && <StreamTab token={token} onUnauthorized={onUnauthorized} compact onCapture={capture} onCaptureStateChange={captureStateChanged} captureDisabled={busy || frames.length >= 3 || totalImages >= 6} />}
        <p className="op-fine-print">Capture adds a local draft frame, never sends it. Text chat works without a stream.</p>
      </section>
      <section className="op-panel op-research-privacy" aria-labelledby="research-privacy-title">
        <h3 id="research-privacy-title">Before you send</h3>
        <p id="research-privacy">Captures stay local until you choose Send to OpenAI. The displayed conversation and attached frames are sent to OpenAI and billed under its API terms. Requests use store: false, not a zero-retention guarantee.</p>
        <p>No continuous video, audio, stream credentials or wearer drafts are sent. Research never automatically replies to the glasses. No web search or external tools are enabled.</p>
        <p>History and frames stay only in browser memory and clear on reload, sign-out or New chat. The server may briefly cache results for retries. Use a private deployment for sensitive footage.</p>
      </section>
    </aside>
  </div>;
}
