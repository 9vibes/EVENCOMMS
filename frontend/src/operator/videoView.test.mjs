import assert from 'node:assert/strict';
import { test } from 'node:test';
import { clampVideoView, videoCaptureGeometry } from './videoView.ts';

const landscape = { width: 320, height: 180, videoWidth: 1920, videoHeight: 1080 };

test('capture preserves source detail within the JPEG dimension limit', () => {
  assert.deepEqual(videoCaptureGeometry({ zoom: 1, x: 0, y: 0 }, landscape), {
    width: 1280, height: 720, x: 0, y: 0, drawWidth: 1280, drawHeight: 720,
  });
});

test('capture follows zoom and pan rather than grabbing the whole source', () => {
  assert.deepEqual(videoCaptureGeometry({ zoom: 2, x: 20, y: -10 }, landscape), {
    width: 960, height: 540, x: -420, y: -300, drawWidth: 1920, drawHeight: 1080,
  });
  assert.throws(() => videoCaptureGeometry({ zoom: 1, x: 0, y: 0 }, { ...landscape, videoWidth: 0 }));
});

test('1x and unavailable dimensions center the image', () => {
  assert.deepEqual(clampVideoView({ zoom: 1, x: 100, y: -100 }, landscape), { zoom: 1, x: 0, y: 0 });
  assert.deepEqual(clampVideoView({ zoom: 2, x: 100, y: -100 }, { ...landscape, videoWidth: 0 }), { zoom: 2, x: 0, y: 0 });
});

test('matching aspect ratio clamps pixel translations in both directions', () => {
  assert.deepEqual(clampVideoView({ zoom: 2, x: 900, y: -900 }, landscape), { zoom: 2, x: 160, y: -90 });
  assert.deepEqual(clampVideoView({ zoom: 1.5, x: -900, y: 900 }, landscape), { zoom: 1.5, x: -80, y: 45 });
});

test('portrait and ultrawide sources cannot pan into their letterboxing', () => {
  assert.deepEqual(clampVideoView({ zoom: 2, x: 900, y: 900 }, { ...landscape, videoWidth: 1080, videoHeight: 1920 }), { zoom: 2, x: 0, y: 90 });
  assert.deepEqual(clampVideoView({ zoom: 1.5, x: -900, y: -900 }, { ...landscape, videoWidth: 3840 }), { zoom: 1.5, x: -80, y: 0 });
  assert.deepEqual(clampVideoView({ zoom: 4, x: 900, y: 900 }, { ...landscape, videoWidth: 1080, videoHeight: 1920 }), { zoom: 4, x: 42.5, y: 270 });
});

test('resize, fullscreen aspect changes and zoom-out re-clamp existing pan', () => {
  const view = { zoom: 2, x: 160, y: -90 };
  assert.deepEqual(clampVideoView(view, { ...landscape, width: 160, height: 90 }), { zoom: 2, x: 80, y: -45 });
  assert.deepEqual(clampVideoView(view, { ...landscape, width: 320, height: 400 }), { zoom: 2, x: 160, y: 0 });
  assert.deepEqual(clampVideoView({ ...view, zoom: 1.5 }, landscape), { zoom: 1.5, x: 80, y: -45 });
  assert.deepEqual(clampVideoView(view, { ...landscape, videoWidth: 1080, videoHeight: 1920 }), { zoom: 2, x: 0, y: -90 });
  assert.deepEqual(clampVideoView({ zoom: 4, x: 12, y: -30 }, landscape), { zoom: 4, x: 12, y: -30 });
});

test('fractional bounds never expose an image edge through panning at any zoom step', () => {
  for (const [videoWidth, videoHeight] of [[1920, 1080], [1080, 1920], [4096, 1000], [1000, 1000]]) {
    for (const [width, height] of [[291.5, 163.96875], [361.5, 203.34375], [1440, 740]]) {
      const fit = Math.min(width / videoWidth, height / videoHeight);
      for (let zoom = 1; zoom <= 4; zoom += 0.5) {
        for (const direction of [-1, 1]) {
          const result = clampVideoView({ zoom, x: direction * 10000, y: direction * 10000 }, { width, height, videoWidth, videoHeight });
          for (const [pan, image, frame] of [[result.x, videoWidth * fit * zoom, width], [result.y, videoHeight * fit * zoom, height]]) {
            if (image < frame) assert.equal(pan, 0);
            else {
              assert.ok((frame - image) / 2 + pan <= 1e-9);
              assert.ok((frame + image) / 2 + pan >= frame - 1e-9);
            }
          }
        }
      }
    }
  }
});
