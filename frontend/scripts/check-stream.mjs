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
