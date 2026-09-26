// Real running application and media only. No routes, mock responses or recordings.
import { stat } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';

// Disable inherited Playwright protocol/API tracing before loading it.
process.env.DEBUG = '';
process.env.PWDEBUG = '0';

let stage = 'configuration';
let browser;
let context;
let page;
let token;
let timer;
let failed = false;

async function check() {
  const base = new URL(process.env.STREAM_TEST_URL);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password
      || base.pathname !== '/' || base.search || base.hash || !process.env.ADMIN_PASSWORD) {
    throw new Error('Invalid test configuration');
  }
  const screenshot = process.env.STREAM_TEST_SCREENSHOT;
  if (screenshot && !(await stat(dirname(resolve(screenshot)))).isDirectory()) {
    throw new Error('Screenshot parent must exist');
  }
  stage = 'Playwright dependency';
  const { chromium, expect } = await import('@playwright/test');
  stage = 'Chromium launch';
  browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  context = await browser.newContext({ baseURL: base.origin, viewport: { width: 1440, height: 1000 } });
  context.setDefaultTimeout(10_000);
  page = await context.newPage();
  let streamCalls = 0;
  let settingsCalls = 0;
  let mediaCalls = 0;
  let unsafeMedia = false;
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/stream/')) streamCalls++;
    if (url.pathname === '/api/stream/settings') settingsCalls++;
    if (url.pathname.startsWith('/api/stream/live/')) {
      mediaCalls++;
      if (url.origin !== base.origin || url.username || url.password
          || [...url.searchParams.keys()].some(key => !['_HLS_msn', '_HLS_part', '_HLS_skip'].includes(key))
          || request.headers().authorization) unsafeMedia = true;
    }
  });
  stage = 'UI login';
  await page.goto('/');
  await page.getByLabel('Operator password').fill(process.env.ADMIN_PASSWORD);
  await page.getByRole('button', { name: 'Enter console', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'STREAM', exact: true })).toBeVisible();
  token = await page.evaluate(() => sessionStorage.getItem('evencomms.operator.token'));
  expect(Boolean(token)).toBe(true);

  async function playing() {
    await page.waitForFunction(() => {
      const video = document.querySelector('video');
      return video && video.videoWidth > 0 && video.readyState >= 2 && !video.paused;
    }, null, { timeout: 20_000 });
    await expect(page.getByText('Playing live preview', { exact: true })).toBeVisible();
    const start = await page.locator('video').evaluate(video => video.currentTime);
    await page.waitForTimeout(3000);
    expect(await page.locator('video').evaluate((video, start) =>
      video.videoWidth > 0 && video.readyState >= 2 && !video.paused && !video.ended
      && video.currentTime >= start + 2, start)).toBe(true);
    await expect(page.getByText('Playing live preview', { exact: true })).toBeVisible();
  }

  stage = 'decoded and advancing live video';
  const stream = page.getByRole('tab', { name: 'STREAM', exact: true });
  const operator = page.getByRole('tab', { name: 'OPERATOR', exact: true });
  await stream.click();
  await playing();
  expect(mediaCalls).toBeGreaterThan(0);
  expect(unsafeMedia).toBe(false);
  const cookie = (await context.cookies()).find(item => item.name === 'evencomms_playback');
  expect(Boolean(cookie?.httpOnly && cookie.sameSite === 'Strict' && cookie.path === '/api/stream/live/')).toBe(true);

  stage = 'zoom and pan preserve the video element and media source';
  const zoomVideo = await page.locator('video').elementHandle();
  const sourceBeforeZoom = await zoomVideo.evaluate(video => video.currentSrc);
  const frame = page.locator('.op-live-frame');
  const zoomIn = page.getByRole('button', { name: 'Zoom in', exact: true });
  const zoomOut = page.getByRole('button', { name: 'Zoom out', exact: true });
  const resetView = page.getByRole('button', { name: 'Reset view', exact: true });
  await expect(zoomOut).toBeDisabled();
  await zoomIn.click();
  await zoomIn.click();
  await expect(frame).toHaveAttribute('data-zoom', '2');
  await expect(page.getByLabel('Zoom level', { exact: true })).toHaveText('200%');
  expect(await zoomVideo.evaluate(video => video.controls)).toBe(false);
  const pan = page.getByRole('region', { name: 'Pan zoomed video', exact: true });
  await pan.focus();
  await pan.press('ArrowRight');
  await pan.press('ArrowDown');
  await expect.poll(() => zoomVideo.evaluate(video => new DOMMatrixReadOnly(getComputedStyle(video).transform).e)).toBeGreaterThan(0);
  await pan.scrollIntoViewIfNeeded();
  const box = await pan.boundingBox();
  await page.mouse.move(box.x + 15, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 15, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();
  expect(await zoomVideo.evaluate(video => {
    const transform = new DOMMatrixReadOnly(getComputedStyle(video).transform);
    return transform.a === 2 && transform.e > 0 && transform.e <= video.parentElement.clientWidth / 2 + 1;
  })).toBe(true);
  expect(await zoomVideo.evaluate(video => video === document.querySelector('video') && !video.paused)).toBe(true);
  expect(await zoomVideo.evaluate(video => video.currentSrc)).toBe(sourceBeforeZoom);
  await page.getByRole('button', { name: 'Unmute video', exact: true }).click();
  expect(await zoomVideo.evaluate(video => video.muted)).toBe(false);
  await page.getByRole('button', { name: 'Mute video', exact: true }).click();
  expect(await zoomVideo.evaluate(video => video.muted)).toBe(true);
  for (let step = 0; step < 4; step++) await zoomIn.click();
  await expect(frame).toHaveAttribute('data-zoom', '4');
  await expect(zoomIn).toBeDisabled();
  for (let step = 0; step < 6; step++) await zoomOut.click();
  await expect(frame).toHaveAttribute('data-zoom', '1');
  await expect(zoomOut).toBeDisabled();
  expect(await zoomVideo.evaluate(video => video.controls)).toBe(true);
  await zoomIn.click();
  await resetView.click();
  expect(await zoomVideo.evaluate(video => {
    const m = new DOMMatrixReadOnly(getComputedStyle(video).transform);
    return m.a === 1 && m.e === 0 && m.f === 0;
  })).toBe(true);

  stage = 'container fullscreen keeps zoom controls available';
  const fullscreen = page.getByRole('button', { name: 'Fullscreen', exact: true });
  if (await fullscreen.isEnabled()) {
    await zoomIn.click();
    await fullscreen.click();
    await expect.poll(() => page.evaluate(() => document.fullscreenElement?.classList.contains('op-live-player'))).toBe(true);
    await expect(zoomIn).toBeVisible();
    await page.getByRole('button', { name: 'Exit fullscreen', exact: true }).click();
    await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);
    await resetView.click();
  }

  stage = 'manual pause remains paused with latency catch-up enabled';
  await zoomIn.click();
  await page.getByRole('button', { name: 'Pause video', exact: true }).click();
  await expect(page.getByText('Preview paused. Use the player controls to resume.', { exact: true })).toBeVisible();
  await page.waitForTimeout(2500);
  expect(await page.locator('video').evaluate(video => video.paused)).toBe(true);
  await zoomIn.click();
  expect(await page.locator('video').evaluate(video => video.paused)).toBe(true);
  await page.getByRole('button', { name: 'Play video', exact: true }).click();
  await playing();

  stage = 'responsive full-width 16:9 layout';
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const panel = await page.getByRole('tabpanel', { name: 'STREAM', exact: true }).boundingBox();
    const frame = await page.locator('.op-live-frame').boundingBox();
    expect(Math.abs(panel.width - frame.width)).toBeLessThan(2);
    expect(Math.abs(frame.width / frame.height - 16 / 9)).toBeLessThan(0.02);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(frame.x >= 0 && frame.x + frame.width <= width + 1).toBe(true);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await resetView.click();

  stage = 'touch drag pans zoomed video without scrolling the page';
  await page.setViewportSize({ width: 390, height: 1000 });
  await zoomIn.click();
  await zoomIn.click();
  await pan.scrollIntoViewIfNeeded();
  const touchBox = await pan.boundingBox();
  const touch = await context.newCDPSession(page);
  await touch.send('Emulation.setTouchEmulationEnabled', { enabled: true });
  const touchPoint = { x: touchBox.x + touchBox.width / 2, y: touchBox.y + touchBox.height / 2, id: 1 };
  const scrollBeforePan = await page.evaluate(() => scrollY);
  await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [touchPoint] });
  await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ ...touchPoint, x: touchPoint.x + 40, y: touchPoint.y + 20 }] });
  await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await expect.poll(() => zoomVideo.evaluate(video => new DOMMatrixReadOnly(getComputedStyle(video).transform).e)).toBeGreaterThan(0);
  expect(await page.evaluate(() => scrollY)).toBe(scrollBeforePan);
  await touch.send('Emulation.setTouchEmulationEnabled', { enabled: false });
  await touch.detach();
  await resetView.click();
  await page.setViewportSize({ width: 1440, height: 1000 });

  stage = 'missing-cookie recovery using real HTTP';
  const rejected = page.waitForResponse(response =>
    new URL(response.url()).pathname.startsWith('/api/stream/live/') && response.status() === 401,
  { timeout: 20_000 });
  const renewed = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/stream/playback-session'
      && response.request().method() === 'POST' && response.status() === 200,
  { timeout: 20_000 });
  // Attach both waiters before removing the real cookie; no HTTP interception.
  await Promise.all([rejected, renewed, context.clearCookies({ name: 'evencomms_playback' })]);
  await page.waitForResponse(response =>
    new URL(response.url()).pathname.startsWith('/api/stream/live/') && response.status() === 200,
  { timeout: 10_000 });
  await playing();
  expect((await context.cookies()).some(item => item.name === 'evencomms_playback')).toBe(true);

  stage = 'leaving STREAM stops playback requests';
  const oldVideo = await page.locator('video').elementHandle();
  await operator.click();
  await expect(page.locator('video')).toHaveCount(0);
  expect(await oldVideo.evaluate(video => !video.isConnected && !video.getAttribute('src') && video.paused)).toBe(true);
  // Allow already-dispatched requests to settle, then cover multiple polling intervals.
  await page.waitForTimeout(500);
  const stopped = streamCalls;
  await page.waitForTimeout(5500);
  expect(streamCalls).toBe(stopped);
  // Do not pair a wearer, read conversations, or modify somebody else's draft.
  stage = 'returning to STREAM resumes decoded playback';
  await stream.click();
  await playing();
  expect(settingsCalls).toBe(0);
  expect(unsafeMedia).toBe(false);
  if (screenshot) {
    stage = 'optional screenshot';
    await page.getByRole('tabpanel', { name: 'STREAM', exact: true }).screenshot({ path: screenshot });
  }
  stage = 'UI logout';
  const [logout] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/logout'),
    page.getByRole('button', { name: 'Sign out', exact: true }).click(),
  ]);
  expect(logout.status()).toBe(204);
  await expect(page.getByLabel('Operator password')).toBeVisible();
  token = null;
}

try {
  // Avoid Playwright exception dumps: they can contain request headers or UI data.
  const interrupted = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('Deadline exceeded')), 75_000);
    process.once('SIGTERM', () => reject(new Error('Interrupted')));
    process.once('SIGINT', () => reject(new Error('Interrupted')));
  });
  await Promise.race([check(), interrupted]);
} catch {
  failed = true;
} finally {
  clearTimeout(timer);
  try {
    if (token && context) {
      await context.request.post('/api/logout', { headers: { Authorization: `Bearer ${token}` }, timeout: 3000 });
    }
  } catch { failed = true; }
  try { await context?.close(); } catch { failed = true; }
  try { await browser?.close(); } catch { failed = true; }
}
if (failed) {
  console.error(`Stream browser smoke failed during ${stage}; private diagnostics suppressed.`);
  process.exitCode = 1;
} else {
  console.log('Stream browser smoke passed; browser context closed.');
}
