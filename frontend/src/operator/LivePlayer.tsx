import type Hls from 'hls.js';
import { useEffect, useRef, useState } from 'react';

// Fixed same-origin playback path: never use publishing credentials in media URLs.
const MANIFEST = '/api/stream/live/index.m3u8';

export default function LivePlayer({ waiting, authorizationError, session, onUnauthorized }: {
  waiting: string | null;
  authorizationError: string;
  session: string | null;
  onUnauthorized: () => void;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<'loading' | 'playing' | 'paused' | 'blocked' | 'error' | 'unsupported'>('loading');
  const [reason, setReason] = useState('');
  const [retry, setRetry] = useState(0);
  const active = waiting === null && !authorizationError;

  useEffect(() => {
    setState('loading');
    setReason('');
    if (!active) return;
    const element = video.current!;
    const controller = new AbortController();
    let disposed = false;
    let hls: Hls | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    let manuallyPaused = false;
    let hasStarted = false;
    let probing = false;
    const clearWatchdog = () => { clearTimeout(watchdog); watchdog = undefined; };
    const fail = (text = 'Live preview interrupted. Retrying in five seconds.') => {
      if (disposed || manuallyPaused) return;
      clearWatchdog();
      setState('error');
      setReason(text);
      if (!retryTimer) retryTimer = setTimeout(() => setRetry(value => value + 1), 5000);
    };
    const loading = () => {
      if (disposed || manuallyPaused || retryTimer) return;
      setState('loading');
      // Repeated waiting/stalled events must not postpone the watchdog forever.
      if (!watchdog) watchdog = setTimeout(() => fail('No playable media received. Retrying the live connection.'), 20000);
    };
    const playing = () => {
      hasStarted = true;
      manuallyPaused = false;
      clearWatchdog();
      clearTimeout(retryTimer);
      retryTimer = undefined;
      setState('playing');
    };
    const paused = () => {
      if (!hasStarted || element.error || element.ended) return;
      manuallyPaused = true;
      clearWatchdog();
      clearTimeout(retryTimer);
      retryTimer = undefined;
      setState('paused');
    };
    const resumed = () => { hasStarted = true; manuallyPaused = false; loading(); };
    const play = () => {
      void element.play().catch((error: unknown) => {
        if (disposed) return;
        if (error instanceof DOMException && error.name === 'NotAllowedError') {
          clearWatchdog();
          setState('blocked');
        } else if (!(error instanceof DOMException && error.name === 'AbortError')) fail();
      });
    };
    const nativeError = () => {
      if (probing || disposed) return;
      probing = true;
      // Native media errors omit HTTP status. Probe only on this fallback path.
      void fetch(MANIFEST, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal })
        .then(async response => {
          await response.body?.cancel();
          if (!disposed) {
            if (response.status === 401) onUnauthorized();
            else fail();
          }
        })
        .catch(() => { if (!disposed) fail(); })
        .finally(() => { probing = false; });
    };
    const ended = () => { manuallyPaused = false; fail(); };
    element.addEventListener('playing', playing);
    element.addEventListener('play', resumed);
    element.addEventListener('pause', paused);
    element.addEventListener('waiting', loading);
    element.addEventListener('stalled', loading);
    element.addEventListener('ended', ended);
    loading();
    void import('hls.js').then(({ default: Hls }) => {
      if (disposed) return;
      if (Hls.isSupported()) {
        hls = new Hls({ lowLatencyMode: true, backBufferLength: 30 });
        hls.on(Hls.Events.MANIFEST_PARSED, play);
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (disposed) return;
          if (data.response?.code === 401) { onUnauthorized(); return; }
          if (data.fatal) {
            // Destroy can emit pause; that is not a human action.
            hasStarted = false;
            hls?.destroy();
            hls = null;
            fail();
          }
        });
        hls.loadSource(MANIFEST);
        hls.attachMedia(element);
      } else if (element.canPlayType('application/vnd.apple.mpegurl')) {
        element.addEventListener('error', nativeError);
        element.addEventListener('loadedmetadata', play);
        element.src = MANIFEST;
      } else {
        clearWatchdog();
        setState('unsupported');
      }
    }).catch(() => fail('The playback engine could not be loaded. Retrying in five seconds.'));
    return () => {
      disposed = true;
      controller.abort();
      clearWatchdog();
      clearTimeout(retryTimer);
      element.removeEventListener('playing', playing);
      element.removeEventListener('play', resumed);
      element.removeEventListener('pause', paused);
      element.removeEventListener('waiting', loading);
      element.removeEventListener('stalled', loading);
      element.removeEventListener('ended', ended);
      element.removeEventListener('error', nativeError);
      element.removeEventListener('loadedmetadata', play);
      hls?.destroy();
      element.pause();
      element.removeAttribute('src');
      element.load();
    };
  }, [active, session, retry, onUnauthorized]);

  const message = authorizationError || waiting || (state === 'playing' ? 'Playing live preview'
    : state === 'paused' ? 'Preview paused. Use the player controls to resume.'
    : state === 'blocked' ? 'Autoplay blocked. Press play to start the preview.'
    : state === 'unsupported' ? 'Media unsupported. Use a current browser with HLS playback support.'
    : state === 'error' ? reason : 'Publisher warming up. Waiting for the first playable segment.');

  return (
    <div className="op-live-player">
      <div className="op-live-frame">
        <video ref={video} autoPlay muted playsInline controls aria-label="Live stream preview" />
        {!active && <div className="op-live-standby" aria-hidden="true"><span className="op-square" /><span>STANDING BY</span></div>}
      </div>
      <div className="op-live-caption">
        <p role="status">{message}</p>
        {active && state === 'error' && <button className="op-button" onClick={() => setRetry(value => value + 1)}>Retry now</button>}
        {active && state === 'blocked' && <button className="op-button op-button-acid" onClick={() => { void video.current?.play().catch(() => setState('blocked')); }}>Play preview</button>}
      </div>
    </div>
  );
}
