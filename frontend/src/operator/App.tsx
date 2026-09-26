import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, characterCount, clientId, errorText, mergeMessages, request } from './api';
import type { Message, ServiceStatus, Session } from './api';
import StreamTab from './StreamTab';

const TOKEN_KEY = 'evencomms.operator.token';
const POLL_INTERVAL = 2000;
const fullDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const shortTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });

function dateLabel(value: string, short = false): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : (short ? shortTime : fullDate).format(date);
}

function Icon({ name }: { name: 'arrow' | 'plus' | 'spark' | 'link' | 'trash' | 'signal' }) {
  const paths = {
    arrow: <path d="M4 12h15m-6-6 6 6-6 6" />,
    plus: <path d="M12 5v14M5 12h14" />,
    spark: (
      <>
        <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z" />
        <path d="m20 2 .5 1.5L22 4l-1.5.5L20 6l-.5-1.5L18 4l1.5-.5L20 2Z" />
      </>
    ),
    link: (
      <>
        <path d="m10 8 3-3a4.2 4.2 0 0 1 6 6l-3 3M14 16l-3 3a4.2 4.2 0 0 1-6-6l3-3M8 16l8-8" />
      </>
    ),
    trash: (
      <>
        <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7" />
      </>
    ),
    signal: (
      <>
        <path d="M4 18v-3m5 3v-6m5 6V9m5 9V5" />
      </>
    ),
  };
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}

function Brand() {
  return (
    <a className="op-brand" href="/" aria-label="KUNAS EVENCOMMS operator home">
      <span className="op-brand-mark" aria-hidden="true">
        K
        <span>
          <Icon name="arrow" />
        </span>
      </span>
      <span>
        <strong>
          KUNAS<span className="op-brand-slash"> / </span>EVENCOMMS
        </strong>
        <small>LOCAL COMMUNICATIONS SYSTEM</small>
      </span>
    </a>
  );
}

export default function App() {
  const [token, setToken] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  });
  const [loginNotice, setLoginNotice] = useState('');
  const loggingOut = useRef(false);

  async function logout(expired = false) {
    if (loggingOut.current) return;
    loggingOut.current = true;
    let notice = expired ? 'Your operator session has expired. Sign in to continue.' : 'Signed out of this console.';
    if (!expired && token) {
      try {
        await request<void>('/api/logout', token, AbortSignal.timeout(10000), {
          method: 'POST', credentials: 'same-origin',
        });
      } catch {
        notice = 'Signed out locally. Server logout was not confirmed; playback access may remain until it expires.';
      }
    }
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* In-memory logout still succeeds. */
    }
    setLoginNotice(notice);
    setToken(null);
  }

  return (
    <div className="operator-app">
      {token ? (
        <Console key={token} token={token} onLogout={logout} />
      ) : (
        <Login
          notice={loginNotice}
          onLogin={(nextToken) => {
            sessionStorage.setItem(TOKEN_KEY, nextToken);
            loggingOut.current = false;
            setLoginNotice('');
            setToken(nextToken);
          }}
        />
      )}
    </div>
  );
}

function Login({ notice, onLogin }: { notice: string; onLogin: (token: string) => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function login(event: FormEvent) {
    event.preventDefault();
    if (pending.current || !password) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError('');
    try {
      const result = await request<{ token: string }>('/api/login', null, controller.signal, {
        method: 'POST',
        body: { password },
      });
      if (!controller.signal.aborted) {
        if (!result.token) throw new Error('Missing token');
        try {
          onLogin(result.token);
        } catch {
          setError('Session storage is unavailable. Allow site storage to sign in.');
        }
      }
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(
          failure instanceof ApiError && failure.status === 401
            ? 'That password was not accepted. Try again.'
            : errorText(failure),
        );
    } finally {
      if (!controller.signal.aborted) {
        pending.current = null;
        setBusy(false);
      }
    }
  }

  return (
    <div className="op-login">
      <header className="op-topbar">
        <Brand />
        <span className="op-eyebrow">OPERATOR ACCESS</span>
      </header>
      <main className="op-login-main">
        <section className="op-login-intro" aria-labelledby="login-title">
          <div className="op-eyebrow">
            <span className="op-square" /> CLOSE THE DISTANCE
          </div>
          <h1 id="login-title">
            A quiet line.
            <br />A clear connection.
          </h1>
          <p>
            Your local link between the wearer and the people behind them. Pair a device, follow the
            conversation, and send just what matters.
          </p>
          <div className="op-login-diagram" aria-hidden="true">
            <span>WEARER</span>
            <span className="op-diagram-line" />
            <Icon name="signal" />
            <span className="op-diagram-line" />
            <span>OPERATOR</span>
          </div>
          <span className="op-eyebrow">LOCAL FIRST / HUMAN IN THE LOOP</span>
        </section>
        <section className="op-login-card" aria-labelledby="signin-title">
          <span className="op-section-number">01 / CONSOLE ENTRY</span>
          <h2 id="signin-title">Take your station.</h2>
          <p>Sign in with the operator password configured for this server.</p>
          <form onSubmit={login}>
            <label htmlFor="operator-password">Operator password</label>
            <input
              id="operator-password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={busy}
              autoFocus
            />
            <div className="op-login-feedback">
              <p role="status">{notice}</p>
              {error && (
                <p role="alert" className="op-error-text">
                  {error}
                </p>
              )}
            </div>
            <button
              className="op-button op-button-acid op-login-submit"
              disabled={busy || !password}
              type="submit"
            >
              {busy ? 'Signing in...' : 'Enter console'}
              <Icon name="arrow" />
            </button>
          </form>
          <p className="op-fine-print">
            Access stays in this browser tab. Operator sessions expire after 8 hours.
          </p>
        </section>
      </main>
      <Footer />
    </div>
  );
}

type Draft = { text: string; revision: number; attempt?: { text: string; id: string } };
type Action = { kind: 'send' | 'suggest' | 'delete'; sessionId: string; controller: AbortController };

function Console({ token, onLogout }: { token: string; onLogout: (expired?: boolean) => Promise<void> }) {
  const [tab, setTab] = useState<'operator' | 'stream'>('operator');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedRef = useRef<string | null>(null);
  const selectionRevision = useRef(0);
  const sessionsRevision = useRef(0);
  const [history, setHistory] = useState<{ sessionId: string; messages: Message[]; loaded: boolean } | null>(
    null,
  );
  const [service, setService] = useState<ServiceStatus | null>(null);
  const [lastSync, setLastSync] = useState<string | null>(null);
  const [listError, setListError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const draftsRef = useRef<Record<string, Draft>>({});
  const [action, setAction] = useState<Action | null>(null);
  const actionRef = useRef<Action | null>(null);
  const pairingRequest = useRef<AbortController | null>(null);
  const [pairingBusy, setPairingBusy] = useState(false);
  const [pairing, setPairing] = useState<{ code: string; expiresAt: number } | null>(null);
  const [now, setNow] = useState(Date.now());
  const [pairingExpired, setPairingExpired] = useState(false);
  const unauthorizedRef = useRef(onLogout);
  unauthorizedRef.current = onLogout;
  const onUnauthorized = useCallback(() => { void unauthorizedRef.current(true); }, []);
  const mounted = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const followMessages = useRef(true);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      actionRef.current?.controller.abort();
      pairingRequest.current?.abort();
    };
  }, []);

  function selectSession(id: string | null) {
    if (selectedRef.current === id) return;
    selectedRef.current = id;
    selectionRevision.current += 1;
    followMessages.current = true;
    setSelectedId(id);
    setHistoryError('');
    setNotice('');
  }

  function updateDraft(id: string, draft: Draft) {
    draftsRef.current = { ...draftsRef.current, [id]: draft };
    setDrafts(draftsRef.current);
  }

  function handleFailure(failure: unknown, label: string) {
    if (failure instanceof ApiError && failure.status === 401) unauthorizedRef.current(true);
    else setActionError(`${label}: ${errorText(failure)}`);
  }

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    function rejectUnauthorized(failure: unknown): never {
      if (!controller.signal.aborted && failure instanceof ApiError && failure.status === 401) {
        unauthorizedRef.current(true);
        controller.abort();
      }
      throw failure;
    }
    async function poll() {
      const revision = sessionsRevision.current;
      try {
        // Wait for both requests even if one fails, so refreshes cannot overlap.
        const [sessionResult, serviceResult] = await Promise.allSettled([
          request<Session[]>('/api/sessions', token, controller.signal).catch(rejectUnauthorized),
          request<ServiceStatus>('/api/status', token, controller.signal).catch(rejectUnauthorized),
        ]);
        if (controller.signal.aborted) return;
        if (sessionResult.status === 'fulfilled' && revision === sessionsRevision.current) {
          setSessions(sessionResult.value);
          setSessionsLoaded(true);
          if (!sessionResult.value.some((session) => session.id === selectedRef.current))
            selectSession(sessionResult.value[0]?.id ?? null);
        }
        if (serviceResult.status === 'fulfilled') setService(serviceResult.value);
        if (sessionResult.status === 'rejected') throw sessionResult.reason;
        if (serviceResult.status === 'rejected') throw serviceResult.reason;
        setListError('');
        setLastSync(new Date().toISOString());
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 401) {
          unauthorizedRef.current(true);
          return;
        }
        setListError(`Service refresh: ${errorText(failure)}`);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, POLL_INTERVAL);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [token]);

  useEffect(() => {
    if (!selectedId) {
      setHistory(null);
      return;
    }
    const id = selectedId;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setHistory({ sessionId: id, messages: [], loaded: false });
    setHistoryError('');
    async function poll() {
      try {
        const messages = await request<Message[]>(
          `/api/sessions/${encodeURIComponent(id)}/messages`,
          token,
          controller.signal,
        );
        if (controller.signal.aborted || selectedRef.current !== id) return;
        setHistory((previous) => ({
          sessionId: id,
          messages: mergeMessages(previous?.sessionId === id ? previous.messages : [], messages),
          loaded: true,
        }));
        setHistoryError('');
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 401) {
          unauthorizedRef.current(true);
          return;
        }
        setHistoryError(`Conversation refresh: ${errorText(failure)}`);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, POLL_INTERVAL);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [token, selectedId]);

  const messages = history?.sessionId === selectedId ? history.messages : [];
  const lastMessageId = messages[messages.length - 1]?.id;
  useEffect(() => {
    const element = scrollRef.current;
    if (element && followMessages.current) element.scrollTop = element.scrollHeight;
  }, [lastMessageId, selectedId]);

  useEffect(() => {
    if (!pairing) return;
    const tick = () => {
      const time = Date.now();
      setNow(time);
      if (time >= pairing.expiresAt) {
        setPairing(null);
        setPairingExpired(true);
      }
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [pairing]);

  async function createPairing() {
    if (pairingRequest.current) return;
    const controller = new AbortController();
    pairingRequest.current = controller;
    setPairingBusy(true);
    setActionError('');
    const startedAt = Date.now();
    try {
      const result = await request<{ code: string; expires_in: number }>(
        '/api/pairings',
        token,
        controller.signal,
        { method: 'POST' },
      );
      if (controller.signal.aborted || !mounted.current) return;
      const time = Date.now();
      setNow(time);
      setPairing({ code: result.code, expiresAt: startedAt + result.expires_in * 1000 });
      setPairingExpired(false);
      setNotice('Pairing code ready. Enter it on the wearer device. Each code can be used once.');
    } catch (failure) {
      if (!controller.signal.aborted && mounted.current) handleFailure(failure, 'Pairing failed');
    } finally {
      if (!controller.signal.aborted && mounted.current) {
        pairingRequest.current = null;
        setPairingBusy(false);
      }
    }
  }

  async function runAction(kind: Action['kind']) {
    const id = selectedRef.current;
    if (!id || actionRef.current) return;
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    const draft = draftsRef.current[id] ?? { text: '', revision: 0 };
    const text = draft.text.trim();
    if (kind === 'send' && (!text || characterCount(text) > 4000)) return;
    if (kind === 'suggest' && (!service?.ai_configured || !history?.loaded || !messages.length)) return;
    if (
      kind === 'delete' &&
      !window.confirm(
        `Delete the conversation with ${session.name || 'this wearer'}?\n\nAll messages will be permanently deleted. The wearer token will be revoked and the device disconnected. This cannot be undone.`,
      )
    )
      return;
    const currentAction: Action = { kind, sessionId: id, controller: new AbortController() };
    actionRef.current = currentAction;
    setAction(currentAction);
    setActionError('');
    setNotice('');
    const revision = selectionRevision.current;
    const path = `/api/sessions/${encodeURIComponent(id)}`;
    const signal = currentAction.controller.signal;
    try {
      if (kind === 'send') {
        // Keep the same UUID after an uncertain response; edited text gets a new one.
        const attempt = draft.attempt?.text === text ? draft.attempt : { text, id: clientId() };
        updateDraft(id, { ...draft, attempt });
        const message = await request<Message>(`${path}/reply`, token, signal, {
          method: 'POST',
          body: { text, client_id: attempt.id },
        });
        if (signal.aborted || !mounted.current) return;
        const currentDraft = draftsRef.current[id];
        if (currentDraft?.revision === draft.revision)
          updateDraft(id, { text: '', revision: draft.revision + 1 });
        if (selectedRef.current === id)
          setHistory((previous) => ({
            sessionId: id,
            messages: mergeMessages(previous?.sessionId === id ? previous.messages : [], [message]),
            loaded: previous?.sessionId === id && previous.loaded,
          }));
        setNotice(`Reply accepted for ${session.name || 'wearer'}.`);
      } else if (kind === 'suggest') {
        const result = await request<{ text: string }>(`${path}/suggest`, token, signal, { method: 'POST' });
        if (signal.aborted || !mounted.current) return;
        if (
          selectedRef.current !== id ||
          selectionRevision.current !== revision ||
          (draftsRef.current[id]?.revision ?? 0) !== draft.revision
        ) {
          setNotice('Suggestion discarded because the conversation or draft changed. Nothing was sent.');
          return;
        }
        if (!result.text?.trim())
          throw new ApiError('The model returned an empty suggestion. Try again.', 502);
        updateDraft(id, { ...draft, text: result.text, revision: draft.revision + 1 });
        setNotice('Suggestion added to your draft. Review and edit it before sending.');
        composerRef.current?.focus();
      } else {
        await request<void>(path, token, signal, { method: 'DELETE' });
        if (signal.aborted || !mounted.current) return;
        // A list request started before deletion must not resurrect this session.
        sessionsRevision.current += 1;
        setSessions((previous) => previous.filter((item) => item.id !== id));
        const remaining = { ...draftsRef.current };
        delete remaining[id];
        draftsRef.current = remaining;
        setDrafts(remaining);
        if (selectedRef.current === id) selectSession(null);
        setNotice(`Conversation with ${session.name || 'wearer'} deleted. Wearer access revoked.`);
      }
    } catch (failure) {
      if (!signal.aborted && mounted.current)
        handleFailure(
          failure,
          `${kind === 'send' ? 'Reply not confirmed; your draft is kept for retry' : kind === 'suggest' ? 'Suggestion failed' : 'Deletion failed'} (${session.name || 'wearer'})`,
        );
    } finally {
      if (!signal.aborted && mounted.current && actionRef.current === currentAction) {
        actionRef.current = null;
        setAction(null);
      }
    }
  }

  const selected = sessions.find((session) => session.id === selectedId);
  const draft = selectedId ? (drafts[selectedId]?.text ?? '') : '';
  const count = characterCount(draft.trim());
  const secondsLeft = pairing ? Math.max(0, Math.ceil((pairing.expiresAt - now) / 1000)) : 0;
  const latestReply = [...messages].reverse().find((message) => message.role === 'operator');
  const busyHere = action?.sessionId === selectedId;
  const errors = [listError, historyError, actionError].filter(Boolean);

  return (
    <div className="op-console">
      <header className="op-topbar">
        <Brand />
        <div className="op-topbar-right">
          <span className={`op-connection ${lastSync && !listError ? 'is-online' : ''}`}>
            <i />
            {listError ? 'RECONNECTING' : lastSync ? 'SERVICE ONLINE' : 'CONNECTING'}
          </span>
          <button className="op-text-button" onClick={() => void onLogout()}>
            Sign out{' '}
            <span aria-hidden="true">
              <Icon name="arrow" />
            </span>
          </button>
        </div>
      </header>
      <main className="op-main">
        <section className="op-page-heading" aria-labelledby="console-title">
          <div>
            <div className="op-eyebrow op-console-navigation">
              <span>CONTROL ROOM /</span>
              <div role="tablist" aria-label="Control room" className="op-tabs">
                {(['operator', 'stream'] as const).map((name, index) => (
                  <button
                    key={name}
                    id={`tab-${name}`}
                    role="tab"
                    aria-selected={tab === name}
                    aria-controls={`panel-${name}`}
                    tabIndex={tab === name ? 0 : -1}
                    onClick={() => setTab(name)}
                    onKeyDown={(event) => {
                      const next = event.key === 'Home' ? 0 : event.key === 'End' ? 1
                        : event.key === 'ArrowRight' || event.key === 'ArrowLeft' ? 1 - index : null;
                      if (next === null) return;
                      event.preventDefault();
                      const name = next === 0 ? 'operator' : 'stream';
                      setTab(name);
                      document.getElementById(`tab-${name}`)?.focus();
                    }}
                  >{index === 1 && <span aria-hidden="true">/</span>}{name.toUpperCase()}</button>
                ))}
              </div>
            </div>
            <h1 id="console-title">
              Super Secret Comms Platform
            </h1>
            <p>{tab === 'operator' ? 'A direct line to your wearer. Thoughtful replies, without the noise.' : 'One live source. A direct view, without recording or analysis.'}</p>
          </div>
          {tab === 'operator' && <button
            className="op-button op-button-acid"
            onClick={() => void createPairing()}
            disabled={pairingBusy}
          >
            <Icon name="plus" />
            {pairingBusy ? 'Creating code...' : 'Pair a wearer'}
          </button>}
        </section>

        <section id="panel-stream" role="tabpanel" aria-labelledby="tab-stream" hidden={tab !== 'stream'} tabIndex={0}>
          {tab === 'stream' && <StreamTab token={token} onUnauthorized={onUnauthorized} />}
        </section>
        <section id="panel-operator" role="tabpanel" aria-labelledby="tab-operator" hidden={tab !== 'operator'} tabIndex={0}>
        {tab === 'operator' && <>
        <div className="op-feedback" aria-live="polite" aria-atomic="true">
          <span>{notice}</span>
        </div>
        {errors.length > 0 && (
          <div className="op-error-banner" role="alert">
            {errors.map((error) => (
              <p key={error}>{error}</p>
            ))}
            {actionError && (
              <button className="op-text-button" onClick={() => setActionError('')}>
                Dismiss action error
              </button>
            )}
          </div>
        )}

        <div className="op-workspace">
          <aside className="op-sessions op-panel" aria-labelledby="sessions-title">
            <div className="op-panel-heading">
              <h2 id="sessions-title">Conversations</h2>
              <span className="op-count">{sessions.length.toString().padStart(2, '0')}</span>
            </div>
            <div className="op-session-list">
              {!sessions.length && (
                <div className="op-sidebar-empty">
                  <Icon name="link" />
                  <h3>
                    {!sessionsLoaded
                      ? listError
                        ? 'Service unavailable'
                        : 'Finding your lines...'
                      : 'No wearers paired'}
                  </h3>
                  <p>
                    {!sessionsLoaded
                      ? 'Conversations appear once the service responds.'
                      : 'Create a pairing code to start your first conversation.'}
                  </p>
                </div>
              )}
              {sessions.map((session, index) => (
                <button
                  key={session.id}
                  className={`op-session ${session.id === selectedId ? 'is-selected' : ''}`}
                  aria-pressed={session.id === selectedId}
                  onClick={() => selectSession(session.id)}
                >
                  <span className="op-session-index">{String(index + 1).padStart(2, '0')}</span>
                  <span className="op-session-details">
                    <strong>{session.name || 'Unnamed wearer'}</strong>
                    <span className="op-session-status">
                      <i className={session.connected ? 'is-online' : ''} />
                      {session.connected ? 'Connected' : 'Offline'}
                      <span> / History saved</span>
                    </span>
                    <time dateTime={session.created_at} title={dateLabel(session.created_at)}>
                      Paired {dateLabel(session.created_at)}
                    </time>
                  </span>
                  <span className="op-session-arrow" aria-hidden="true">
                    <Icon name="arrow" />
                  </span>
                </button>
              ))}
            </div>
            <div className="op-sidebar-note">
              <span className="op-eyebrow">ONE CONVERSATION. ONE WEARER.</span>
              <p>History stays with each pairing, even when a device goes offline.</p>
            </div>
          </aside>

          <section className="op-conversation op-panel" aria-labelledby="conversation-title">
            <div className="op-conversation-heading">
              <div>
                <span className="op-eyebrow">{selected ? 'ACTIVE CONVERSATION' : 'YOUR DIRECT LINE'}</span>
                <h2 id="conversation-title">
                  {selected?.name || (selected ? 'Unnamed wearer' : 'Ready when you are.')}
                </h2>
              </div>
              {selected && (
                <div className="op-conversation-actions">
                  <span className={`op-status-tag ${selected.connected ? 'is-online' : ''}`}>
                    <i />
                    {selected.connected ? 'Connected' : 'Offline'}
                  </span>
                  <button
                    className="op-icon-button op-delete"
                    aria-label={`Delete conversation with ${selected.name || 'wearer'} and revoke access`}
                    title="Delete conversation and revoke wearer access"
                    disabled={!!action}
                    onClick={() => void runAction('delete')}
                  >
                    <Icon name="trash" />
                  </button>
                </div>
              )}
            </div>
            <div
              className="op-history"
              ref={scrollRef}
              onScroll={() => {
                const element = scrollRef.current;
                if (element)
                  followMessages.current =
                    element.scrollHeight - element.scrollTop - element.clientHeight < 80;
              }}
              tabIndex={0}
              aria-label="Conversation history"
            >
              {!selected ? (
                <EmptyConversation
                  title="Good communication starts here."
                  detail="Pair a wearer, then select their conversation. Their submitted messages will appear here."
                />
              ) : !history?.loaded || history.sessionId !== selectedId ? (
                <EmptyConversation
                  title={historyError ? 'History is unavailable.' : 'Opening the conversation...'}
                  detail={
                    historyError
                      ? 'We will keep trying. Your draft stays right here.'
                      : 'Fetching saved messages from the local service.'
                  }
                />
              ) : !messages.length ? (
                <EmptyConversation
                  title="The line is open."
                  detail="No messages yet. Send a welcome reply, or wait for the wearer to submit a message."
                />
              ) : (
                <div
                  role="log"
                  aria-label="Submitted conversation messages"
                  aria-live="polite"
                  aria-relevant="additions"
                >
                  <ol className="op-message-list">
                    {messages.map((message) => (
                      <li className={`op-message op-message-${message.role}`} key={message.id}>
                        <div className="op-message-meta">
                          <span>{message.role === 'operator' ? 'YOU / OPERATOR' : 'WEARER'}</span>
                          <time dateTime={message.created_at} title={dateLabel(message.created_at)}>
                            {dateLabel(message.created_at)}
                          </time>
                        </div>
                        <p>{message.text}</p>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
            <div className="op-history-caption">
              <span className="op-square" /> Submitted messages only. Wearer drafts stay private.
              <span className="op-history-count">
                {messages.length} {messages.length === 1 ? 'MESSAGE' : 'MESSAGES'}
              </span>
            </div>
            <form
              className="op-composer"
              onSubmit={(event) => {
                event.preventDefault();
                void runAction('send');
              }}
            >
              <div className="op-composer-label">
                <label htmlFor="operator-reply">Your reply</label>
                <span className={count > 4000 ? 'op-error-text' : ''} id="reply-count">
                  {count.toLocaleString()} / 4,000
                </span>
              </div>
              <textarea
                ref={composerRef}
                id="operator-reply"
                name="reply"
                value={draft}
                disabled={!selected}
                readOnly={busyHere && action?.kind === 'send'}
                placeholder={
                  selected ? 'Write something worth seeing.' : 'Select a conversation to write a reply.'
                }
                aria-describedby="reply-guidance reply-count"
                aria-invalid={count > 4000}
                onChange={(event) => {
                  if (!selectedId) return;
                  const previous = draftsRef.current[selectedId] ?? { text: '', revision: 0 };
                  updateDraft(selectedId, {
                    ...previous,
                    text: event.target.value,
                    revision: previous.revision + 1,
                  });
                }}
                onKeyDown={(event) => {
                  if (
                    (event.ctrlKey || event.metaKey) &&
                    event.key === 'Enter' &&
                    !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault();
                    void runAction('send');
                  }
                }}
              />
              <div className="op-composer-toolbar">
                <button
                  className="op-button op-button-suggest"
                  type="button"
                  disabled={
                    !selected || !!action || !service?.ai_configured || !history?.loaded || !messages.length
                  }
                  onClick={() => void runAction('suggest')}
                  title={
                    service?.ai_configured
                      ? 'Generate an editable draft from submitted conversation history. Nothing is sent automatically.'
                      : 'Configure the local AI model on the server to enable suggestions.'
                  }
                >
                  <Icon name="spark" />
                  {busyHere && action?.kind === 'suggest' ? 'Thinking...' : 'AI suggestion'}
                </button>
                <button
                  className="op-button op-button-ink"
                  type="submit"
                  disabled={!selected || !!action || count === 0 || count > 4000}
                >
                  {busyHere && action?.kind === 'send' ? 'Sending...' : 'Send reply'}
                  <Icon name="arrow" />
                </button>
              </div>
              <p className="op-composer-hint" id="reply-guidance">
                {count > 4000
                  ? 'Shorten your reply to 4,000 characters before sending.'
                  : 'AI only drafts. You decide what gets sent.'}
                <span>Ctrl / Cmd + Enter to send</span>
              </p>
            </form>
          </section>

          <aside className="op-rail" aria-label="Pairing and service details">
            <section className="op-pairing op-panel" aria-labelledby="pairing-title">
              <div className="op-rail-heading">
                <Icon name="link" />
                <span className="op-eyebrow">DEVICE CONNECTION</span>
              </div>
              <h2 id="pairing-title">Make the connection.</h2>
              <p>Open the wearer client on your device and enter a single-use pairing code.</p>
              {pairing && secondsLeft > 0 ? (
                <div className="op-pair-code">
                  <span className="op-eyebrow">SINGLE-USE CODE</span>
                  <strong aria-label={`Pairing code ${pairing.code}`}>{pairing.code}</strong>
                  <span className="op-code-timer">
                    Expires in{' '}
                    <time>
                      {Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, '0')}
                    </time>
                  </span>
                  <p>Used codes cannot be reused, even before this timer ends.</p>
                </div>
              ) : (
                <div className="op-pair-placeholder">
                  <span aria-hidden="true">- - - - - -</span>
                  <p role="status">
                    {pairingExpired ? 'Code expired. Generate a new one.' : 'No active pairing code'}
                  </p>
                </div>
              )}
              <button
                className="op-button op-button-outline"
                onClick={() => void createPairing()}
                disabled={pairingBusy}
              >
                <Icon name="plus" />
                {pairingBusy
                  ? 'Creating code...'
                  : pairing
                    ? 'Generate another code'
                    : 'Generate pairing code'}
              </button>
              <a className="op-rail-link" href="/glasses.html" target="_blank" rel="noopener noreferrer">
                Open wearer client{' '}
                <span aria-hidden="true">
                  <Icon name="arrow" />
                </span>
                <span className="op-sr-only"> (new tab)</span>
              </a>
              <a
                className="op-rail-link op-secondary-link"
                href="/glasses.html?simulate=1"
                target="_blank"
                rel="noopener noreferrer"
              >
                Try browser simulation{' '}
                <span aria-hidden="true">
                  <Icon name="arrow" />
                </span>
                <span className="op-sr-only"> (new tab)</span>
              </a>
            </section>
            <section className="op-preview" aria-labelledby="preview-title">
              <div className="op-preview-heading">
                <h2 id="preview-title">Last operator reply</h2>
                <span className="op-eyebrow">TEXT PREVIEW</span>
              </div>
              <div className="op-preview-screen">
                <span className="op-preview-corner" aria-hidden="true" />
                {latestReply ? (
                  <p>{latestReply.text}</p>
                ) : (
                  <p className="op-preview-empty">
                    {selected
                      ? 'Your next reply,\nwith nothing in the way.'
                      : 'A little context.\nRight in sight.'}
                  </p>
                )}
                <span className="op-preview-bottom" aria-hidden="true">
                  EVENCOMMS <span>...</span>
                </span>
              </div>
              <p className="op-fine-print">
                {latestReply
                  ? 'Last accepted reply, not a live device view or delivery receipt.'
                  : 'A text-only preview will appear after a reply is accepted.'}
              </p>
            </section>
            <section className="op-service op-panel" aria-labelledby="service-title">
              <div className="op-panel-heading">
                <h2 id="service-title">Local services</h2>
                <Icon name="signal" />
              </div>
              <dl>
                <ServiceRow
                  label="Speech to text"
                  value={service ? (service.stt_enabled ? 'Enabled' : 'Disabled') : 'Checking'}
                  active={service?.stt_enabled}
                />
                <ServiceRow
                  label="STT model"
                  value={service ? service.stt_model || 'Not configured' : 'Awaiting status'}
                />
                <ServiceRow
                  label="AI suggestions"
                  value={service ? (service.ai_configured ? 'Configured' : 'Not configured') : 'Checking'}
                  active={service?.ai_configured}
                />
                <ServiceRow
                  label="Ollama model"
                  value={service ? service.ollama_model || 'Not configured' : 'Awaiting status'}
                />
              </dl>
              <p className="op-service-note">
                {listError
                  ? 'Status may be out of date. Reconnecting...'
                  : 'Configuration reported by your server. Not a model health check.'}
              </p>
              <div className="op-sync">
                POLL / 2 SEC{' '}
                <span>{lastSync ? `SYNC ${dateLabel(lastSync, true)}` : 'AWAITING SERVICE'}</span>
              </div>
            </section>
          </aside>
        </div>
        </>}
        </section>
      </main>
      <Footer />
    </div>
  );
}

function ServiceRow({ label, value, active }: { label: string; value: string; active?: boolean }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {active !== undefined && <i className={active ? 'is-online' : ''} />}
        {value}
      </dd>
    </div>
  );
}

function EmptyConversation({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="op-empty-conversation">
      <div className="op-empty-art" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <h3>{title}</h3>
      <p>{detail}</p>
    </div>
  );
}

function Footer() {
  return (
    <footer className="op-footer">
      <span>KUNAS / EVENCOMMS</span>
      <span>BUILT FOR PRESENCE. NOT DISTRACTION.</span>
      <span>LOCAL COMMS / v0.1</span>
    </footer>
  );
}
