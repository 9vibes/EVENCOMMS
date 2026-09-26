import { test, expect, type Route } from '@playwright/test';

// Real isolated login/conversation/logout; stream and Research HTTP are synthetic.
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
  let researchStatus: { configured: boolean; key_source: 'session' | 'server' | null } = { configured: false, key_source: null };
  let researchMode: 'success' | 'error' | 'hold' = 'success';
  let heldResearch: Route | undefined;
  let researchStatusCalls = 0;
  let researchModelsFailure = false;
  const researchRequests: { request_id: string; model: string; messages: { role: string; text: string; images?: string[] }[] }[] = [];
  const researchReply = (route: Route) => route.fulfill({ json: {
    request_id: route.request().postDataJSON().request_id,
    model: 'synthetic-response-model', text: '<b>Plain text research answer</b>\nSecond line', incomplete: false,
    usage: { input_tokens: 12, output_tokens: 8, total_tokens: 20 },
  } });
  await page.route('**/api/research/**', async route => {
    expect(route.request().headers().authorization).toMatch(/^Bearer /);
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/status')) {
      researchStatusCalls++;
      await route.fulfill({ json: researchStatus });
    } else if (path.endsWith('/models')) {
      await route.fulfill(researchModelsFailure ? { status: 502, json: { detail: 'Provider models unavailable' } }
        : { json: { models: [{ id: 'synthetic-response-model' }, { id: 'synthetic-other-model' }] } });
    } else if (path.endsWith('/connection')) {
      if (route.request().method() === 'POST') {
        expect(route.request().postDataJSON()).toEqual({ api_key: 'synthetic-openai-key' });
        researchStatus = { configured: true, key_source: 'session' };
      } else {
        expect(route.request().method()).toBe('DELETE');
        researchStatus = { configured: true, key_source: 'server' };
      }
      await route.fulfill({ json: researchStatus });
    } else if (path.endsWith('/chat')) {
      expect(route.request().method()).toBe('POST');
      researchRequests.push(route.request().postDataJSON());
      if (researchMode === 'hold') heldResearch = route;
      else if (researchMode === 'error') await route.fulfill({ status: 502, json: { detail: 'Provider compatibility unavailable' } });
      else await researchReply(route);
    } else throw new Error(`Unexpected Research route ${path}`);
  });
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
  const research = page.getByRole('tab', { name: 'RESEARCH', exact: true });
  await expect(page.getByRole('tablist', { name: 'Control room' })).toBeVisible();
  await expect(operator).toHaveAttribute('aria-selected', 'true');
  await expect(stream).toHaveAttribute('tabindex', '-1');
  await operator.focus();
  await operator.press('End');
  await expect(research).toBeFocused();
  await expect(research).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel', { name: 'RESEARCH', exact: true })).toBeVisible();
  await expect(page.getByText('Not configured', { exact: true })).toBeVisible();
  await research.press('ArrowLeft');
  await expect(stream).toBeFocused();
  await expect(stream).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel', { name: 'STREAM', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Super Secret Comms Platform' })).toBeVisible();
  await expect(page.getByText('Streaming disabled', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Zoom in', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Zoom out', exact: true })).toBeDisabled();
  await expect(page.getByLabel('Zoom level', { exact: true })).toHaveText('100%');
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
  await expect(research).toBeFocused();
  await research.press('ArrowLeft');
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
  await expect(research).toBeFocused();
  await research.press('ArrowRight');
  await expect(operator).toBeFocused();
  await expect(page.getByLabel('Your reply', { exact: true })).toHaveValue('Keep this unsent draft');
  expect(await oldVideo!.evaluate(element => element.getAttribute('src'))).toBeNull();
  const stopped = { statusCalls, settingsCalls, playbackCalls, mediaCalls };
  const sessionsBefore = sessionsCalls;
  await page.waitForTimeout(5500);
  expect({ statusCalls, settingsCalls, playbackCalls, mediaCalls }).toEqual(stopped);
  expect(sessionsCalls - sessionsBefore).toBeGreaterThanOrEqual(2);
  expect(sessionsCalls - sessionsBefore).toBeLessThanOrEqual(3);

  // Exercise Research within this login so the suite stays below five logins/minute.
  status = { ...status, enabled: false };
  await research.click();
  const researchPanel = page.getByRole('tabpanel', { name: 'RESEARCH', exact: true });
  const researchLog = page.getByRole('log', { name: 'Research conversation' });
  const question = page.getByLabel('Research question', { exact: true });
  const model = page.getByLabel('OpenAI model', { exact: true });
  const key = page.getByLabel('OpenAI API key', { exact: true });
  const send = page.getByRole('button', { name: 'Send to OpenAI', exact: true });
  await expect(page.getByText('Not configured', { exact: true })).toBeVisible();
  await question.fill('A text-only research question');
  await expect(send).toBeDisabled();
  await expect.soft(researchPanel.getByRole('button', { name: /Encoder configuration/ })).toHaveCount(0);
  await key.fill('synthetic-unsent-key');
  await expect(key).toHaveAttribute('type', 'password');
  await operator.click();
  await expect(page.getByLabel('Your reply', { exact: true })).toHaveValue('Keep this unsent draft');
  const hiddenResearchCalls = researchStatusCalls;
  await page.waitForTimeout(2200);
  expect(researchStatusCalls).toBe(hiddenResearchCalls);
  await research.click();
  await expect(question).toHaveValue('A text-only research question');
  await expect(key).toHaveValue('');
  await key.fill('synthetic-openai-key');
  await page.getByRole('button', { name: 'Connect for this sign-in', exact: true }).click();
  await expect(page.getByText('Connected / models validated / current sign-in key', { exact: true })).toBeVisible();
  await expect(key).toHaveValue('');
  await expect(model).toHaveValue('');
  await expect(send).toBeDisabled();
  await expect(model.locator('option')).toHaveText(['Select an available model', 'synthetic-response-model', 'synthetic-other-model']);
  await model.selectOption('synthetic-response-model');
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    const chatBox = (await page.getByRole('heading', { name: 'Research chat', exact: true }).boundingBox())!;
    const sidebarBox = (await page.getByRole('complementary', { name: 'Research connection and live preview' }).boundingBox())!;
    if (width === 1440) expect(sidebarBox.x).toBeGreaterThan(chatBox.x + chatBox.width);
    else expect(sidebarBox.y).toBeGreaterThan(chatBox.y + chatBox.height);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  researchMode = 'error';
  await send.click();
  await expect(page.getByText(/Reply not confirmed\. Your draft and frames are kept/)).toBeVisible();
  await expect(question).toHaveValue('A text-only research question');
  await expect(researchLog.locator('li')).toHaveCount(0);
  await expect(page.getByLabel('Operator password')).toHaveCount(0);
  expect(researchRequests[0]).toEqual({
    request_id: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/),
    model: 'synthetic-response-model', messages: [{ role: 'user', text: 'A text-only research question', images: [] }],
  });
  researchMode = 'hold';
  page.once('dialog', async dialog => { expect(dialog.message()).toContain('may bill again'); await dialog.accept(); });
  await send.click();
  await expect.poll(() => researchRequests.length).toBe(2);
  expect(researchRequests[1]).toEqual(researchRequests[0]);
  await expect(model).toBeDisabled();
  await expect(key).toBeDisabled();
  await expect(question).toHaveAttribute('readonly', '');
  await operator.click();
  await researchReply(heldResearch!);
  await research.click();
  await expect(researchLog).toContainText('<b>Plain text research answer</b>');
  await expect(researchLog.locator('b')).toHaveCount(0);
  await expect(researchLog.locator('li')).toHaveCount(2);
  await expect(question).toHaveValue('');
  await expect(model).toHaveValue('synthetic-response-model');
  await question.fill('Follow up with full history');
  researchMode = 'success';
  await send.click();
  await expect(researchLog.locator('li')).toHaveCount(4);
  expect(researchRequests[2].messages).toEqual([
    { role: 'user', text: 'A text-only research question', images: [] },
    { role: 'assistant', text: '<b>Plain text research answer</b>\nSecond line' },
    { role: 'user', text: 'Follow up with full history', images: [] },
  ]);
  expect(researchRequests[2].request_id).not.toBe(researchRequests[1].request_id);
  const persisted = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(persisted).not.toContain('synthetic-openai-key');
  expect(persisted).not.toContain('synthetic-response-model');
  expect(persisted).not.toContain('text-only research');
  await page.getByRole('button', { name: 'Remove sign-in key', exact: true }).click();
  await expect(page.getByText('Connected / models validated / server-managed key', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Remove sign-in key', exact: true })).toHaveCount(0);
  researchModelsFailure = true;
  await page.getByRole('button', { name: 'Refresh models', exact: true }).click();
  await expect(page.getByText('Configured / models not validated / server-managed key', { exact: true })).toBeVisible();
  await expect(page.getByText('Provider models unavailable', { exact: true })).toBeVisible();
  await expect(model).toHaveValue('synthetic-response-model');
  await expect(send).toBeDisabled();
  researchModelsFailure = false;
  await page.getByRole('button', { name: 'Refresh models', exact: true }).click();
  await expect(page.getByText('Connected / models validated / server-managed key', { exact: true })).toBeVisible();
  researchMode = 'hold';
  await question.fill('Discard this pending question');
  await send.click();
  await expect.poll(() => researchRequests.length).toBe(4);
  page.once('dialog', async dialog => { expect(dialog.message()).toContain('may still process and bill'); await dialog.dismiss(); });
  await page.getByRole('button', { name: 'New chat', exact: true }).click();
  await expect(question).toHaveValue('Discard this pending question');
  page.once('dialog', async dialog => { expect(dialog.message()).toContain('may still process and bill'); await dialog.accept(); });
  await page.getByRole('button', { name: 'New chat', exact: true }).click();
  await researchReply(heldResearch!).catch(() => {}); // Browser waiting was intentionally aborted.
  await expect(researchLog.locator('li')).toHaveCount(0);
  await expect(question).toHaveValue('');
  await expect(model).toBeEnabled();
  await question.fill('Memory must clear on sign-out');
  await send.click();
  await expect.poll(() => researchRequests.length).toBe(5);

  unauthorized = true;
  await stream.click();
  await expect(page.getByLabel('Operator password')).toBeVisible();
  await expect(page.getByText('Your operator session has expired. Sign in to continue.')).toBeVisible();
  await researchReply(heldResearch!).catch(() => {}); // Unmount also cancels browser waiting.
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
  unauthorized = false;
  await research.click();
  await expect(question).toHaveValue('');
  await expect(model).toHaveValue('');
  await expect(researchLog.locator('li')).toHaveCount(0);
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
