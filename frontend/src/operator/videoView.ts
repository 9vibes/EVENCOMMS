export interface VideoBounds {
  width: number;
  height: number;
  videoWidth: number;
  videoHeight: number;
}

export interface CapturedFrame {
  id: string;
  dataUrl: string;
  width: number;
  height: number;
  capturedAt: string;
}

export function videoCaptureGeometry(view: { zoom: number; x: number; y: number }, bounds: VideoBounds) {
  const { width, height, videoWidth, videoHeight } = bounds;
  if (Math.min(width, height, videoWidth, videoHeight) <= 0) throw new Error('A decoded frame is required');
  const fit = Math.min(width / videoWidth, height / videoHeight);
  const scale = Math.min(1280 / width, 1280 / height, 1 / (fit * view.zoom));
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    x: ((width - videoWidth * fit * view.zoom) / 2 + view.x) * scale,
    y: ((height - videoHeight * fit * view.zoom) / 2 + view.y) * scale,
    drawWidth: videoWidth * fit * view.zoom * scale,
    drawHeight: videoHeight * fit * view.zoom * scale,
  };
}

export function clampVideoView(view: { zoom: number; x: number; y: number }, bounds: VideoBounds) {
  const { width, height, videoWidth, videoHeight } = bounds;
  if (view.zoom <= 1 || Math.min(width, height, videoWidth, videoHeight) <= 0) {
    return { ...view, x: 0, y: 0 };
  }
  // object-fit: contain may leave letterboxing. Pan only where the scaled image
  // exceeds the frame, not where the scaled video element exceeds it.
  const fit = Math.min(width / videoWidth, height / videoHeight);
  const maxX = Math.max(0, (videoWidth * fit * view.zoom - width) / 2);
  const maxY = Math.max(0, (videoHeight * fit * view.zoom - height) / 2);
  return {
    ...view,
    x: maxX === 0 ? 0 : Math.max(-maxX, Math.min(maxX, view.x)),
    y: maxY === 0 ? 0 : Math.max(-maxY, Math.min(maxY, view.y)),
  };
}
