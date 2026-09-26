import { test, expect, type Route } from '@playwright/test';

// Real isolated login/conversation/logout; stream HTTP is synthetic, not an RTMP/HLS integration test.
// One combined test adds only one login to the existing four-test suite (limit: five/minute).
test('stream tabs, private configuration, playback authorization and cleanup', async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  let status = {
    enabled: false, media_available: false, online: false,
    publisher_session_id: null as string | null, started_at: null as string | null,
    tracks: [] as string[], bitrate_mbps: null as number | null,
  };
  let unauthorized = false;
  let statusCalls = 0;
  let settingsCalls = 0;
  let playbackCalls = 0;
  let mediaCalls = 0;
  let logoutCalls = 0;
  let loginCalls = 0;
  let sessionsCalls = 0;
  let heldPlayback: Route | undefined;
  let holdPlayback = true;
  const acknowledge = (route: Route) => route.fulfill({
    json: { expires_in: 5 },
    headers: { 'Set-Cookie': 'stream-test=ack; HttpOnly; SameSite=Strict; Path=/api/stream/live/; Max-Age=300' },
  });
  page.on('request', request => {
    if (request.url().endsWith('/api/logout')) logoutCalls++;
    if (request.url().endsWith('/api/login')) loginCalls++;
    if (request.url().endsWith('/api/sessions')) sessionsCalls++;
  });
  await page.route('**/api/stream/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.includes('/live/')) expect(route.request().headers().authorization).toMatch(/^Bearer /);
    if (path.endsWith('/status')) {
      statusCalls++;
      await route.fulfill(unauthorized ? { status: 401, json: { detail: 'Expired' } } : { json: status });
    } else if (path.endsWith('/settings')) {
      settingsCalls++;
      await route.fulfill({ json: {
        enabled: true, server_url: 'rtmp://stream.test:21936/live',
        stream_key: 'stream?user=publisher&pass=synthetic-secret',
        playback_url: '/api/stream/live/index.m3u8', rtmp_port: 21936,
      } });
    } else if (path.endsWith('/playback-session')) {
      expect(route.request().method()).toBe('POST');
      playbackCalls++;
      if (holdPlayback) heldPlayback = route;
      else await acknowledge(route);
    } else {
      mediaCalls++;
      expect(route.request().url()).not.toContain('synthetic-secret');
      expect(route.request().headers().authorization).toBeUndefined();
      expect(route.request().headers().cookie).toContain('stream-test=ack');
      await route.fulfill({ status: 503, body: 'Synthetic publisher warming up' });
    }
  });

  await page.goto('/');
  await page.getByLabel('Operator password').fill('evencomms-e2e-only-password');
  await page.getByRole('button', { name: 'Enter console' }).click();
  await expect(page.getByText('SERVICE ONLINE', { exact: true })).toBeVisible();
  const token = (await page.evaluate(() => sessionStorage.getItem('evencomms.operator.token')))!;
  const headers = { Authorization: `Bearer ${token}` };
  const pairingResponse = await page.request.post('/api/pairings', { headers });
  expect(pairingResponse.ok()).toBeTruthy();
  const pairing = await pairingResponse.json();
  const wearerResponse = await page.request.post('/api/pair', { data: { code: pairing.code, name: 'Stream tab wearer' } });
  expect(wearerResponse.ok()).toBeTruthy();
  const wearer = await wearerResponse.json();
  await expect(page.getByRole('heading', { name: 'Stream tab wearer', exact: true })).toBeVisible();
  await page.getByLabel('Your reply', { exact: true }).fill('A real conversation message');
  await page.getByRole('button', { name: 'Send reply', exact: true }).click();
  await expect(page.getByRole('log')).toContainText('A real conversation message');
  await page.getByLabel('Your reply', { exact: true }).fill('Keep this unsent draft');
  expect(statusCalls).toBe(0);

  const operator = page.getByRole('tab', { name: 'OPERATOR', exact: true });
  const stream = page.getByRole('tab', { name: 'STREAM', exact: true });
  await expect(page.getByRole('tablist', { name: 'Control room' })).toBeVisible();
  await expect(operator).toHaveAttribute('aria-selected', 'true');
  await expect(stream).toHaveAttribute('tabindex', '-1');
  await operator.focus();
  await operator.press('End');
  await expect(stream).toBeFocused();
  await expect(stream).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel', { name: 'STREAM', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Super Secret Comms Platform' })).toBeVisible();
  await expect(page.getByText('Streaming disabled', { exact: true })).toBeVisible();
  await expect(page.getByRole('log')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Pair a wearer', exact: true })).toHaveCount(0);
  expect(settingsCalls).toBe(0);
  expect(playbackCalls).toBe(0);
  expect(mediaCalls).toBe(0);

  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const panel = (await page.getByRole('tabpanel', { name: 'STREAM', exact: true }).boundingBox())!;
    const player = (await page.locator('.op-live-frame').boundingBox())!;
    expect(Math.abs(panel.width - player.width)).toBeLessThan(2);
    expect(Math.abs(player.width / player.height - 16 / 9)).toBeLessThan(0.02);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await page.screenshot({ path: testInfo.outputPath(`stream-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });

  await stream.press('Home');
  await expect(operator).toBeFocused();
  await expect(page.getByLabel('Your reply', { exact: true })).toHaveValue('Keep this unsent draft');
  await expect(page.getByRole('heading', { name: 'Stream tab wearer', exact: true })).toBeVisible();
  await expect(page.getByRole('log')).toHaveCount(1);
  await operator.press('ArrowLeft');
  await expect(stream).toBeFocused();

  await page.getByRole('button', { name: /Encoder configuration/ }).click();
  await expect(page.getByLabel('RTMP server')).toHaveValue('rtmp://stream.test:21936/live');
  await expect(page.getByLabel('Stream key', { exact: true })).toHaveAttribute('type', 'password');
  await expect(page.getByText(/RTMP is plaintext/)).toContainText('TCP 21936');
  expect(settingsCalls).toBe(1);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.getByRole('button', { name: 'Copy server', exact: true }).click();
  await expect(page.getByText('RTMP server copied.', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('rtmp://stream.test:21936/live');
  await page.getByRole('button', { name: 'Copy key', exact: true }).click();
  await expect(page.getByText('Stream key copied. Keep it private.', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('synthetic-secret');
  await page.getByRole('button', { name: 'Show key', exact: true }).click();
  await expect(page.getByLabel('Stream key', { exact: true })).toHaveAttribute('type', 'text');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-secret');
  await page.getByRole('button', { name: /Encoder configuration/ }).click();
  await expect(page.getByLabel('Stream key', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: /Encoder configuration/ }).click();
  await expect(page.getByLabel('Stream key', { exact: true })).toHaveAttribute('type', 'password');
  expect(settingsCalls).toBe(2);

  status = { ...status, enabled: true };
  await expect(page.getByText('Media service unavailable', { exact: true })).toBeVisible();
  await expect.poll(() => playbackCalls).toBe(1);
  status = { ...status, media_available: true };
  await expect(page.getByText('Publisher offline', { exact: true })).toBeVisible();
  status = { ...status, online: true, publisher_session_id: 'publisher-1', started_at: '2026-09-26T12:00:00Z', tracks: ['H264', 'MPEG4Audio'], bitrate_mbps: 2.5 };
  await expect(page.getByText('Source ready', { exact: true })).toBeVisible();
  await expect(page.getByText('Authorizing browser playback.', { exact: true })).toBeVisible();
  expect(mediaCalls).toBe(0);
  await expect(page.getByText('2.50 Mbps', { exact: true })).toBeVisible();
  await expect(page.getByText('H264 / MPEG4Audio', { exact: true })).toBeVisible();
  holdPlayback = false;
  await acknowledge(heldPlayback!);
  await expect.poll(() => mediaCalls).toBeGreaterThan(0);
  await expect.poll(() => playbackCalls).toBeGreaterThan(1);
  await expect(page.getByText('Playing live preview', { exact: true })).toHaveCount(0);
  const beforeRestart = mediaCalls;
  status = { ...status, publisher_session_id: 'publisher-2' };
  await expect.poll(() => mediaCalls).toBeGreaterThan(beforeRestart);
  const oldVideo = await page.locator('video').elementHandle();
  await stream.press('ArrowRight');
  await expect(operator).toBeFocused();
  await expect(page.getByLabel('Your reply', { exact: true })).toHaveValue('Keep this unsent draft');
  expect(await oldVideo!.evaluate(element => element.getAttribute('src'))).toBeNull();
  const stopped = { statusCalls, settingsCalls, playbackCalls, mediaCalls };
  const sessionsBefore = sessionsCalls;
  await page.waitForTimeout(5500);
  expect({ statusCalls, settingsCalls, playbackCalls, mediaCalls }).toEqual(stopped);
  expect(sessionsCalls - sessionsBefore).toBeGreaterThanOrEqual(2);
  expect(sessionsCalls - sessionsBefore).toBeLessThanOrEqual(3);

  unauthorized = true;
  await stream.click();
  await expect(page.getByLabel('Operator password')).toBeVisible();
  await expect(page.getByText('Your operator session has expired. Sign in to continue.')).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('evencomms.operator.token'))).toBeNull();
  expect(logoutCalls).toBe(0);
  const expiredCalls = statusCalls;
  await page.waitForTimeout(2300);
  expect(statusCalls).toBe(expiredCalls);
  expect(loginCalls).toBe(1);

  // The mocked 401 did not revoke the real token. Restore it without another login.
  await page.evaluate(token => sessionStorage.setItem('evencomms.operator.token', token), token);
  await page.reload();
  await expect(page.getByText('SERVICE ONLINE', { exact: true })).toBeVisible();
  await page.route('**/api/logout', route => route.abort('failed'));
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByText(/Server logout was not confirmed/)).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('evencomms.operator.token'))).toBeNull();
  await page.unroute('**/api/logout');
  await page.evaluate(token => sessionStorage.setItem('evencomms.operator.token', token), token);
  await page.reload();
  await expect(page.getByText('SERVICE ONLINE', { exact: true })).toBeVisible();
  // Remove the real conversation before revoking its operator token.
  expect((await page.request.delete(`/api/sessions/${wearer.session_id}`, { headers })).status()).toBe(204);
  const logoutResponse = page.waitForResponse(response => response.url().endsWith('/api/logout'));
  await page.getByRole('button', { name: 'Sign out' }).click();
  expect((await logoutResponse).status()).toBe(204);
  await expect(page.getByText('Signed out of this console.', { exact: true })).toBeVisible();
  expect((await page.request.get('/api/sessions', { headers })).status()).toBe(401);
  expect(loginCalls).toBe(1);
});
