import type Hls from 'hls.js';
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { clientId } from './api';
import { clampVideoView, videoCaptureGeometry, type CapturedFrame, type VideoBounds } from './videoView';

// Fixed same-origin playback path: never use publishing credentials in media URLs.
const MANIFEST = '/api/stream/live/index.m3u8';

export default function LivePlayer({ waiting, authorizationError, session, onUnauthorized, onCapture, onCaptureStateChange, captureDisabled = false }: {
  waiting: string | null;
  authorizationError: string;
  session: string | null;
  onUnauthorized: () => void;
  onCapture?: (frame: CapturedFrame) => void;
  onCaptureStateChange?: (capturing: boolean) => void;
  captureDisabled?: boolean;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<'loading' | 'playing' | 'paused' | 'blocked' | 'error' | 'unsupported'>('loading');
  const [reason, setReason] = useState('');
  const [retry, setRetry] = useState(0);
  const active = waiting === null && !authorizationError;
  const player = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const bounds = useRef<VideoBounds>({ width: 0, height: 0, videoWidth: 0, videoHeight: 0 });
  const drag = useRef<{ id: number; x: number; y: number; element: HTMLDivElement } | null>(null);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [metadataReady, setMetadataReady] = useState(false);
  const [mediaPaused, setMediaPaused] = useState(true);
  const [mediaMuted, setMediaMuted] = useState(true);
  const [fullscreenSupported, setFullscreenSupported] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [controlError, setControlError] = useState('');
  const [capturing, setCapturing] = useState(false);
  const captureLock = useRef(false);
  const captureGeneration = useRef(0);
  const captureStateCallback = useRef(onCaptureStateChange);
  captureStateCallback.current = onCaptureStateChange;
  const panHintId = useId();
  const fullscreenHintId = useId();
  const canRender = active && metadataReady;
  const zoomed = view.zoom > 1;

  function stopDrag() {
    const current = drag.current;
    drag.current = null;
    if (current?.element.hasPointerCapture(current.id)) current.element.releasePointerCapture(current.id);
  }

  useLayoutEffect(() => {
    const element = video.current!;
    const viewport = frame.current!;
    const measure = () => {
      stopDrag();
      const ready = element.readyState >= 1 && element.videoWidth > 0 && element.videoHeight > 0;
      setMetadataReady(ready);
      const rect = viewport.getBoundingClientRect();
      const style = getComputedStyle(viewport);
      // Keep the view across a same-publisher reconnect while metadata is absent.
      bounds.current = {
        width: rect.width - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth),
        height: rect.height - parseFloat(style.borderTopWidth) - parseFloat(style.borderBottomWidth),
        videoWidth: ready ? element.videoWidth : bounds.current.videoWidth,
        videoHeight: ready ? element.videoHeight : bounds.current.videoHeight,
      };
      setView(current => clampVideoView(current, bounds.current));
    };
    const emptied = () => { setMetadataReady(false); stopDrag(); };
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    element.addEventListener('loadedmetadata', measure);
    element.addEventListener('resize', measure);
    element.addEventListener('emptied', emptied);
    measure();
    return () => {
      observer.disconnect();
      element.removeEventListener('loadedmetadata', measure);
      element.removeEventListener('resize', measure);
      element.removeEventListener('emptied', emptied);
      stopDrag();
    };
  }, []);

  useEffect(() => {
    stopDrag();
    captureGeneration.current++;
    captureLock.current = false;
    setCapturing(false);
    captureStateCallback.current?.(false);
    setView({ zoom: 1, x: 0, y: 0 });
    setControlError('');
    return () => {
      captureGeneration.current++;
      captureStateCallback.current?.(false);
    };
  }, [session]);

  useEffect(() => {
    setFullscreenSupported(Boolean(document.fullscreenEnabled && player.current?.requestFullscreen));
    const changed = () => {
      stopDrag();
      setFullscreen(document.fullscreenElement === player.current);
      setControlError('');
    };
    document.addEventListener('fullscreenchange', changed);
    return () => document.removeEventListener('fullscreenchange', changed);
  }, []);

  function changeZoom(zoom: number) {
    stopDrag();
    setView(current => clampVideoView({ ...current, zoom: Math.max(1, Math.min(4, zoom)) }, bounds.current));
  }

  async function toggleFullscreen() {
    setControlError('');
    try {
      if (document.fullscreenElement === player.current) await document.exitFullscreen();
      else await player.current!.requestFullscreen();
    } catch {
      setControlError('Fullscreen is unavailable or was denied by this browser. The preview is still available here.');
    }
  }

  async function togglePlayback() {
    const element = video.current!;
    setControlError('');
    if (!element.paused) element.pause();
    else {
      try { await element.play(); }
      catch { setControlError('Playback could not start. Try Play video again.'); }
    }
  }

  async function captureFrame() {
    const element = video.current;
    if (!onCapture || captureDisabled || captureLock.current || !active) return;
    if (!element || element.readyState < 2 || !element.videoWidth || !element.videoHeight) {
      setControlError('Wait for a decoded video frame before capturing.');
      return;
    }
    const generation = captureGeneration.current;
    const receive = onCapture;
    captureLock.current = true;
    setCapturing(true);
    captureStateCallback.current?.(true);
    setControlError('');
    try {
      const capturedAt = new Date().toISOString();
      const geometry = videoCaptureGeometry(view, { ...bounds.current,
        videoWidth: element.videoWidth, videoHeight: element.videoHeight });
      const canvas = document.createElement('canvas');
      canvas.width = geometry.width;
      canvas.height = geometry.height;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('Canvas unavailable');
      context.fillStyle = '#000000';
      context.fillRect(0, 0, canvas.width, canvas.height);
      // Draw video pixels only, with the current crop, never HTML controls or credentials.
      context.drawImage(element, geometry.x, geometry.y, geometry.drawWidth, geometry.drawHeight);
      let blob: Blob | null = null;
      for (const quality of [0.82, 0.62, 0.42]) {
        blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
        if (blob && blob.size <= 1024 * 1024) break;
      }
      if (!blob || blob.size > 1024 * 1024 || blob.type !== 'image/jpeg') throw new Error('Capture limit exceeded');
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('Invalid image'));
        reader.onerror = () => reject(new Error('Image conversion failed'));
        reader.readAsDataURL(blob);
      });
      if (generation === captureGeneration.current) receive({
        id: clientId(), dataUrl, width: canvas.width, height: canvas.height, capturedAt,
      });
    } catch {
      if (generation === captureGeneration.current)
        setControlError('Could not capture this frame. Wait for video and try again; the stream must be same-origin.');
    } finally {
      if (generation === captureGeneration.current) {
        captureLock.current = false;
        setCapturing(false);
        captureStateCallback.current?.(false);
      }
    }
  }

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
        hls = new Hls({
          lowLatencyMode: true,
          backBufferLength: 30,
          // Keep a small jitter buffer and return to live if playback drifts too far.
          liveSyncDuration: 1,
          liveMaxLatencyDuration: 3,
          maxLiveSyncPlaybackRate: 1.05,
        });
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
    <div className="op-live-player" ref={player}>
      <div className="op-live-frame" ref={frame} data-zoom={view.zoom}>
        <video ref={video} autoPlay muted playsInline controls={!zoomed}
          controlsList={fullscreenSupported ? 'nofullscreen' : undefined}
          aria-label="Live stream preview"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}
          onPlay={() => setMediaPaused(false)} onPause={() => setMediaPaused(true)} onEnded={() => setMediaPaused(true)}
          onVolumeChange={() => setMediaMuted(Boolean(video.current?.muted || video.current?.volume === 0))} />
        {zoomed && canRender && <div className="op-live-pan" role="region" aria-label="Pan zoomed video"
          aria-describedby={panHintId} tabIndex={zoomed ? 0 : -1}
          onPointerDown={event => {
            if (!event.isPrimary || event.button !== 0 || drag.current) return;
            event.currentTarget.setPointerCapture(event.pointerId);
            event.currentTarget.focus({ preventScroll: true });
            drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, element: event.currentTarget };
            event.preventDefault();
          }}
          onPointerMove={event => {
            const current = drag.current;
            if (!current || current.id !== event.pointerId) return;
            const dx = event.clientX - current.x;
            const dy = event.clientY - current.y;
            current.x = event.clientX;
            current.y = event.clientY;
            setView(value => clampVideoView({ ...value, x: value.x + dx, y: value.y + dy }, bounds.current));
          }}
          onPointerUp={event => { if (drag.current?.id === event.pointerId) stopDrag(); }}
          onPointerCancel={event => { if (drag.current?.id === event.pointerId) stopDrag(); }}
          onLostPointerCapture={event => { if (drag.current?.id === event.pointerId) drag.current = null; }}
          onKeyDown={event => {
            const dx = event.key === 'ArrowLeft' ? -32 : event.key === 'ArrowRight' ? 32 : 0;
            const dy = event.key === 'ArrowUp' ? -32 : event.key === 'ArrowDown' ? 32 : 0;
            if (!dx && !dy) return;
            event.preventDefault();
            setView(current => clampVideoView({ ...current, x: current.x + dx, y: current.y + dy }, bounds.current));
          }} />}
        {!active && <div className="op-live-standby" aria-hidden="true"><span className="op-square" /><span>STANDING BY</span></div>}
      </div>
      <div className="op-live-toolbar" role="group" aria-label="Video view controls">
        <button type="button" className="op-button" aria-label="Zoom out" disabled={!canRender || view.zoom <= 1} onClick={() => changeZoom(view.zoom - 0.5)}>Zoom out</button>
        <output aria-label="Zoom level" className="op-live-zoom-level">{Math.round(view.zoom * 100)}%</output>
        <button type="button" className="op-button" aria-label="Zoom in" disabled={!canRender || view.zoom >= 4} onClick={() => changeZoom(view.zoom + 0.5)}>Zoom in</button>
        <button type="button" className="op-button" disabled={!canRender || !zoomed} onClick={() => changeZoom(1)}>Reset view</button>
        <button type="button" className="op-button" disabled={!fullscreenSupported} aria-describedby={!fullscreenSupported ? fullscreenHintId : undefined} onClick={() => void toggleFullscreen()}>{fullscreen ? 'Exit fullscreen' : 'Fullscreen'}</button>
        {onCapture && <button type="button" className="op-button op-button-acid"
          disabled={!canRender || (video.current?.readyState ?? 0) < 2 || captureDisabled || capturing}
          onClick={() => void captureFrame()}>{capturing ? 'Capturing frame...' : 'Capture frame'}</button>}
        {zoomed && <>
          <button type="button" className="op-button" disabled={!canRender} aria-label={mediaPaused ? 'Play video' : 'Pause video'} onClick={() => void togglePlayback()}>{mediaPaused ? 'Play video' : 'Pause video'}</button>
          <button type="button" className="op-button" disabled={!canRender} aria-label={mediaMuted ? 'Unmute video' : 'Mute video'} onClick={() => {
            const element = video.current!;
            element.muted = !mediaMuted;
            if (mediaMuted && element.volume === 0) element.volume = 1;
          }}>{mediaMuted ? 'Unmute video' : 'Mute video'}</button>
        </>}
      </div>
      {zoomed && <p id={panHintId} className="op-live-hint">Drag to pan, or focus the video and use the arrow keys. Reset view restores the full image and native controls.</p>}
      {!fullscreenSupported && <p id={fullscreenHintId} className="op-live-hint">Container fullscreen is not supported in this browser.</p>}
      {controlError && <p className="op-live-hint op-error-text" role="status">{controlError}</p>}
      <div className="op-live-caption">
        <p role="status">{message}</p>
        {active && state === 'error' && <button className="op-button" onClick={() => setRetry(value => value + 1)}>Retry now</button>}
        {active && state === 'blocked' && <button className="op-button op-button-acid" onClick={() => { void video.current?.play().catch(() => setState('blocked')); }}>Play preview</button>}
      </div>
    </div>
  );
}
