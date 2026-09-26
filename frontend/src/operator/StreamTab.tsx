import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorText, request } from './api';
import LivePlayer from './LivePlayer';

interface StreamStatus {
  enabled: boolean;
  media_available: boolean;
  online: boolean;
  publisher_session_id: string | null;
  started_at: string | null;
  tracks: string[];
  bitrate_mbps: number | null;
}

interface StreamSettings {
  enabled: boolean;
  server_url: string;
  stream_key: string;
  playback_url: string;
  rtmp_port: number;
}

export default function StreamTab({ token, onUnauthorized }: { token: string; onUnauthorized: () => void }) {
  const [status, setStatus] = useState<StreamStatus | null>(null);
  const [statusError, setStatusError] = useState('');
  const [authorized, setAuthorized] = useState(false);
  const [playbackError, setPlaybackError] = useState('');
  const [playbackRevision, setPlaybackRevision] = useState(0);
  const playbackExpired = useCallback(() => {
    // A short-lived media cookie can expire while the operator token is still valid.
    setAuthorized(false);
    setPlaybackRevision(value => value + 1);
  }, []);
  const [revealed, setRevealed] = useState(false);
  const [settings, setSettings] = useState<StreamSettings | null>(null);
  const [settingsError, setSettingsError] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [copyNotice, setCopyNotice] = useState('');
  const settingsRef = useRef<StreamSettings | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await request<StreamStatus>('/api/stream/status', token, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(result);
        setStatusError('');
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 401) { onUnauthorized(); return; }
        setStatusError(errorText(failure));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [token, onUnauthorized]);

  const enabled = status?.enabled === true;
  useEffect(() => {
    setAuthorized(false);
    setPlaybackError('');
    if (!enabled) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function authorize() {
      try {
        const result = await request<{ expires_in: number }>('/api/stream/playback-session', token, controller.signal, {
          method: 'POST', credentials: 'same-origin',
        });
        if (controller.signal.aborted) return;
        if (!Number.isFinite(result.expires_in) || result.expires_in <= 0) throw new Error('Invalid playback lifetime');
        setAuthorized(true);
        setPlaybackError('');
        timer = setTimeout(authorize, result.expires_in * 800);
      } catch (failure) {
        if (controller.signal.aborted) return;
        setAuthorized(false);
        if (failure instanceof ApiError && failure.status === 401) { onUnauthorized(); return; }
        setPlaybackError(`Playback authorization unavailable. ${errorText(failure)} Retrying shortly.`);
        timer = setTimeout(authorize, 5000);
      }
    }
    if (playbackRevision) timer = setTimeout(authorize, 1000);
    else void authorize();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [enabled, token, onUnauthorized, playbackRevision]);

  useEffect(() => {
    setSettings(null);
    setSettingsError('');
    setShowKey(false);
    setCopyNotice('');
    if (!revealed) return;
    const controller = new AbortController();
    void request<StreamSettings>('/api/stream/settings', token, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) { settingsRef.current = result; setSettings(result); }
      })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 401) onUnauthorized();
        else setSettingsError(errorText(failure));
      });
    return () => { controller.abort(); settingsRef.current = null; };
  }, [revealed, token, onUnauthorized]);

  async function copy(field: 'server_url' | 'stream_key') {
    const current = settingsRef.current;
    if (!current) return;
    try {
      await navigator.clipboard.writeText(current[field]);
      if (settingsRef.current === current) setCopyNotice(field === 'server_url' ? 'RTMP server copied.' : 'Stream key copied. Keep it private.');
    } catch {
      if (settingsRef.current === current) setCopyNotice('Clipboard unavailable. Select the field and copy manually.');
    }
  }

  const signal = !status ? 'Checking signal' : !enabled ? 'Streaming disabled'
    : !status.media_available ? 'Media service unavailable' : !status.online ? 'Publisher offline' : 'Source ready';
  const waiting = !status ? 'Checking stream setup.' : !enabled ? 'Enable streaming on the server to use this preview.'
    : !status.media_available ? 'The media service is not reachable. We will keep checking.'
    : !status.online ? 'Waiting for your encoder to publish.' : !authorized ? 'Authorizing browser playback.' : null;
  const started = status?.started_at ? new Date(status.started_at) : null;

  return (
    <div className="op-stream">
      <div className="op-stream-heading">
        <h2>Live preview</h2>
        <span className={`op-status-tag ${enabled && status?.media_available && status.online ? 'is-online' : ''}`} role="status"><i />{signal}</span>
      </div>
      {statusError && <p className="op-error-banner" role="alert">Signal status may be out of date. {statusError}</p>}
      <LivePlayer
        waiting={waiting}
        authorizationError={playbackError}
        session={status?.publisher_session_id ?? null}
        onUnauthorized={playbackExpired}
      />
      <dl className="op-stream-telemetry">
        <div><dt>Source bitrate</dt><dd>{status?.bitrate_mbps == null ? 'Not reported' : `${status.bitrate_mbps.toFixed(2)} Mbps`}</dd></div>
        <div><dt>Tracks</dt><dd>{status?.tracks.length ? status.tracks.join(' / ') : 'Not reported'}</dd></div>
        <div><dt>Publisher started</dt><dd>{started && !Number.isNaN(started.getTime()) ? <time dateTime={status!.started_at!}>{started.toLocaleString()}</time> : 'Not reported'}</dd></div>
      </dl>
      <p className="op-fine-print">Source ready reports the publisher, not browser playback. Leaving STREAM stops this preview, never your encoder.</p>
      <section className="op-stream-config op-panel" aria-label="Encoder configuration">
        <button className="op-stream-disclosure" aria-expanded={revealed} aria-controls="encoder-config" onClick={() => setRevealed(!revealed)}>
          <span>Encoder configuration</span><span>{revealed ? 'Hide credentials -' : 'Reveal credentials +'}</span>
        </button>
        {revealed && <div id="encoder-config" className="op-stream-config-body">
          <p>RTMP is plaintext, even when this console uses HTTPS. Use a trusted LAN/VPN and restrict ingest to TCP {settings?.rtmp_port ?? 21936}. Recommended: H.264 video, AAC audio, one-second keyframes.</p>
          <p className="op-fine-print">An HTTP preview tunnel does not forward RTMP. PUBLIC_HOST must point to the encoder-reachable LAN/VPN address of your Stone.</p>
          {settingsError ? <p role="alert" className="op-error-text">{settingsError} Hide and reveal to retry.</p> : !settings ? <p role="status">Loading encoder configuration...</p> : <>
            <label htmlFor="rtmp-server">RTMP server</label>
            <div className="op-stream-field"><input id="rtmp-server" value={settings.server_url} readOnly /><button className="op-button" onClick={() => void copy('server_url')}>Copy server</button></div>
            <label htmlFor="rtmp-key">Stream key</label>
            <div className="op-stream-field"><input id="rtmp-key" type={showKey ? 'text' : 'password'} value={settings.stream_key} readOnly autoComplete="off" spellCheck={false} /><button className="op-button" onClick={() => void copy('stream_key')}>Copy key</button><button className="op-button" aria-pressed={showKey} onClick={() => setShowKey(!showKey)}>{showKey ? 'Mask key' : 'Show key'}</button></div>
            <p className="op-fine-print">Publishing credentials are kept only in memory while revealed. Do not share your stream key.</p>
          </>}
          <p role="status">{copyNotice}</p>
        </div>}
      </section>
    </div>
  );
}
