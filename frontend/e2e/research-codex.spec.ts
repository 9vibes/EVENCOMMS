import { test as base, expect, type Page, type Route } from '@playwright/test';

interface Status {
  enabled: boolean;
  state: 'disconnected' | 'pending' | 'connected' | 'failed';
  verification_url: string | null;
  user_code: string | null;
  generation_enabled: boolean;
}
interface ChatBody {
  request_id: string;
  model: string;
  messages: { role: string; text: string; images?: string[] }[];
}
const deviceURL = 'https://auth.openai.com/codex/device';
const disconnected: Status = { enabled: true, state: 'disconnected', verification_url: null, user_code: null, generation_enabled: false };
const pending: Status = { ...disconnected, state: 'pending', verification_url: deviceURL, user_code: '123456789' };
const connected: Status = { ...disconnected, state: 'connected', generation_enabled: true };
const modelID = 'synthetic-codex-vision'; // Intentionally not the pinned runtime ID.
const answer = '<b>Synthetic Codex answer</b>\nReview this personally.';
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function reply(route: Route) {
  const body = route.request().postDataJSON() as ChatBody;
  return route.fulfill({ json: {
    request_id: body.request_id, model: body.model, text: answer, incomplete: false,
    usage: { input_tokens: 12, output_tokens: 8, total_tokens: 20 },
  } });
}

async function mockCodex(page: Page, token: string) {
  const mock = {
    token, status: { ...disconnected }, statusHTTP: 200,
    models: [{ id: modelID, image: true }, { id: 'synthetic-codex-text', image: false }],
    calls: [] as string[], chats: [] as ChatBody[], apiChats: [] as string[],
    hold: new Set<string>(), held: new Map<string, Route>(), chatError: false,
  };
  page.on('request', request => {
    if (/^\/api\/research\/(status|models|connection|chat)$/.test(new URL(request.url()).pathname)) mock.apiChats.push(request.url());
  });
  await page.route('**/api/research/status', route => route.fulfill({ json: { configured: false, key_source: null } }));
  await page.route('**/api/research/codex/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.split('/').at(-1)!;
    expect(request.headers().authorization).toBe(`Bearer ${token}`);
    expect(request.headers().cookie).toBeUndefined();
    expect(request.method()).toBe(path === 'login' || path === 'chat' ? 'POST' : path === 'connection' ? 'DELETE' : 'GET');
    mock.calls.push(path);
    if (path === 'login') {
      expect(request.postDataJSON()).toEqual({});
      mock.status = { ...pending };
    } else if (path === 'connection') mock.status = { ...disconnected };
    else if (path === 'chat') {
      expect(request.headers()['content-type']).toBe('application/json');
      mock.chats.push(request.postDataJSON());
    } else {
      expect(['status', 'models']).toContain(path);
      expect(request.postData()).toBeNull();
    }
    if (mock.hold.has(path)) { mock.held.set(path, route); return; }
    if (path === 'chat') {
      if (mock.chatError) await route.fulfill({ status: 502, json: { detail: 'Synthetic Codex runtime failure. No paid API fallback.' } });
      else await reply(route);
    } else if (path === 'models') await route.fulfill({ json: { models: mock.models } });
    else if (path === 'status' && mock.statusHTTP !== 200) await route.fulfill({ status: mock.statusHTTP, json: { detail: 'Not found' } });
    else await route.fulfill({ json: mock.status });
  });
  return mock;
}

// Reuse a real, ephemeral operator token rather than mocking auth or exceeding
// the backend's five-logins/minute limit. Reauthenticate if a test revoked it.
const test = base.extend<{ codexMock: Awaited<ReturnType<typeof mockCodex>> }, { operatorLogin: () => Promise<string> }>({
  operatorLogin: [async ({ playwright }, use) => {
    const api = await playwright.request.newContext({ baseURL: 'http://127.0.0.1:8765' });
    let token = '';
    await use(async () => {
      if (token && (await api.get('/api/sessions', { headers: { Authorization: `Bearer ${token}` } })).ok()) return token;
      let response = await api.post('/api/login', { data: { password: 'evencomms-e2e-only-password' } });
      if (response.status() === 429) {
        await new Promise(resolve => setTimeout(resolve, 61_000));
        response = await api.post('/api/login', { data: { password: 'evencomms-e2e-only-password' } });
      }
      expect(response.status()).toBe(200);
      token = (await response.json()).token;
      return token;
    });
    if (token) await api.post('/api/logout', { headers: { Authorization: `Bearer ${token}` } });
    await api.dispose();
  }, { scope: 'worker' }],
  codexMock: [async ({ page, operatorLogin }, use) => {
    const token = await operatorLogin();
    const mock = await mockCodex(page, token);
    await page.addInitScript(token => sessionStorage.setItem('evencomms.operator.token', token), token);
    await page.goto('/');
    await expect(page.getByText('SERVICE ONLINE', { exact: true })).toBeVisible();
    await page.getByRole('tab', { name: 'RESEARCH', exact: true }).click();
    await expect(page.getByText('ChatGPT disconnected', { exact: true })).toBeVisible();
    await use(mock);
    expect(mock.apiChats, 'Codex must never fall back to paid API chat').toEqual([]);
  }, { timeout: 90_000 }],
});

async function refreshConnection(page: Page) {
  await page.getByRole('button', { name: 'Refresh Codex connection' }).click();
}

async function selectModel(page: Page) {
  const selector = page.getByLabel('Codex model selector', { exact: true });
  await expect(selector).toBeEnabled();
  await expect(selector).toHaveValue('');
  await selector.selectOption(modelID);
}

test('code login is the only method; disabled, unmapped and unverified Codex fail closed', async ({ page, codexMock: mock }) => {
  await expect(page.getByLabel('Research provider', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('OpenAI API key', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Send to OpenAI', exact: true })).toHaveCount(0);
  await page.getByLabel('Research question', { exact: true }).fill('Unsent draft stays local');
  mock.status = { ...disconnected, enabled: false };
  await refreshConnection(page);
  await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('Unsent draft stays local');
  await expect(page.getByText('Codex account login is disabled on this server.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Get Codex login code', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Send via Codex', exact: true })).toBeDisabled();

  mock.status = { ...connected, generation_enabled: false };
  await page.getByRole('button', { name: 'Refresh Codex connection' }).click();
  await expect(page.getByText('Runtime verification did not pass; sending disabled.', { exact: true })).toBeVisible();
  await page.getByLabel('Research question', { exact: true }).fill('Do not transmit with an unverified runtime');
  await page.getByLabel('Research question', { exact: true }).press('Control+Enter');
  await expect(page.getByLabel('Codex model selector', { exact: true })).toBeDisabled();
  expect(mock.calls).not.toContain('models');
  expect(mock.chats).toEqual([]);

  mock.statusHTTP = 404;
  await page.getByRole('button', { name: 'Refresh Codex connection' }).click();
  await expect(page.getByText('Codex account login is disabled on this server.', { exact: true })).toBeVisible();
  const stopped = mock.calls.length;
  await page.waitForTimeout(2200);
  expect(mock.calls).toHaveLength(stopped);
  await expect(page.getByLabel('Research provider', { exact: true })).toHaveCount(0);
});

test('HTTP device-code login requires confirmation without enabling API-key entry', async ({ page, codexMock: mock }) => {
  // Chromium resolves *.localhost locally, but this is not one of the exact
  // loopback names allowed by the application's API-key entry policy.
  await page.goto('http://umbrel.localhost:8765/');
  await expect(page.getByText('SERVICE ONLINE', { exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'RESEARCH', exact: true }).click();
  await expect(page.getByLabel('OpenAI API key', { exact: true })).toHaveCount(0);
  await refreshConnection(page);
  const getCode = page.getByRole('button', { name: 'Get Codex login code', exact: true });
  await expect(getCode).toBeEnabled();
  await expect(page.getByText(/Code login is available after a trusted-network confirmation/)).toBeVisible();
  page.once('dialog', async dialog => {
    expect(dialog.message()).toContain('unencrypted HTTP');
    await dialog.dismiss();
  });
  await getCode.click();
  expect(mock.calls.filter(path => path === 'login')).toHaveLength(0);
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
  page.once('dialog', async dialog => {
    expect(dialog.message()).toContain('OpenAI sign-in itself opens over HTTPS');
    await dialog.accept();
  });
  await getCode.click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveText('123456789');
  await expect(page.getByRole('link', { name: 'Open OpenAI device sign-in' })).toHaveAttribute('href', deviceURL);
  expect(mock.calls.filter(path => path === 'login')).toHaveLength(1);
  expect(mock.chats).toEqual([]);
  await page.getByRole('button', { name: 'Cancel sign-in', exact: true }).click();
  await expect(page.getByText('ChatGPT disconnected', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Connect for this sign-in', exact: true })).toHaveCount(0);
});

test('device login polls without drafts, then explicit model selection and Send authenticate the exact chat body', async ({ page, codexMock: mock }) => {
  await refreshConnection(page);
  const question = page.getByLabel('Research question', { exact: true });
  const send = page.getByRole('button', { name: 'Send via Codex', exact: true });
  await question.fill('Only this explicit question should be sent');
  await page.getByRole('button', { name: 'Get Codex login code', exact: true }).click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveText('123456789');
  const link = page.getByRole('link', { name: 'Open OpenAI device sign-in' });
  await expect(link).toHaveAttribute('href', deviceURL);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noreferrer noopener');
  await expect(link).toHaveAttribute('referrerpolicy', 'no-referrer');
  await expect(page.getByRole('button', { name: 'Get Codex login code', exact: true })).toBeDisabled();
  await expect(page.getByText(/Enable device code authentication/)).toBeVisible();
  await expect.poll(() => mock.calls.filter(path => path === 'status').length).toBeGreaterThanOrEqual(2);
  expect(mock.calls.filter(path => path === 'login')).toHaveLength(1);
  expect(mock.calls).not.toContain('models');
  expect(mock.chats).toEqual([]);
  await expect(question).toHaveValue('Only this explicit question should be sent');
  mock.status = { ...connected };
  await expect(page.getByLabel('Codex model selector', { exact: true })).toBeEnabled();
  await expect(send).toBeDisabled();
  await selectModel(page);
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
  await expect(send).toBeEnabled();
  expect(mock.chats).toEqual([]);
  await send.click();
  const log = page.getByRole('log', { name: 'Research conversation' });
  await expect(log.locator('li')).toHaveCount(2);
  await expect(log).toContainText(answer);
  await expect(log.locator('b')).toHaveCount(0);
  await expect(log).toContainText('Tokens: 12 input / 8 output / 20 total');
  expect(mock.chats[0]).toEqual({ request_id: expect.stringMatching(uuid), model: modelID,
    messages: [{ role: 'user', text: 'Only this explicit question should be sent', images: [] }] });
  await question.fill('Explicit follow-up');
  await expect(send).toBeEnabled();
  await send.click();
  await expect(log.locator('li')).toHaveCount(4);
  expect(mock.chats[1].messages).toEqual([
    ...mock.chats[0].messages, { role: 'assistant', text: answer }, { role: 'user', text: 'Explicit follow-up', images: [] },
  ]);
  expect(mock.chats[1].request_id).not.toBe(mock.chats[0].request_id);
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  for (const privateValue of [modelID, '123456789', 'Only this explicit question', answer]) expect(storage).not.toContain(privateValue);
});

test('Cancel sign-in and Disconnect clear state immediately, ignore late polls and keep Codex selected', async ({ page, codexMock: mock }) => {
  await refreshConnection(page);
  await page.getByRole('button', { name: 'Get Codex login code', exact: true }).click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toBeVisible();
  mock.hold.add('status');
  await expect.poll(() => mock.held.has('status')).toBe(true);
  await expect(page.getByRole('button', { name: 'Cancel sign-in', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Cancel sign-in', exact: true }).click();
  await expect(page.getByText('ChatGPT disconnected', { exact: true })).toBeVisible();
  await mock.held.get('status')!.fulfill({ json: connected }).catch(() => {});
  mock.hold.delete('status');
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Research provider', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Send via Codex', exact: true })).toBeDisabled();
  expect(mock.calls).not.toContain('models');
  expect(mock.calls.filter(path => path === 'connection')).toHaveLength(1);

  await page.getByRole('button', { name: 'Get Codex login code', exact: true }).click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toBeVisible();
  mock.status = { ...connected };
  await selectModel(page);
  await page.getByLabel('Research question', { exact: true }).fill('Clear this submitted history on disconnect');
  await page.getByRole('button', { name: 'Send via Codex', exact: true }).click();
  const log = page.getByRole('log', { name: 'Research conversation' });
  await expect(log.locator('li')).toHaveCount(2);
  await page.getByLabel('Research question', { exact: true }).fill('Clear this draft too');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'Disconnect ChatGPT', exact: true }).click();
  await expect(log.locator('li')).toHaveCount(2);
  await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('Clear this draft too');
  mock.hold.add('connection');
  page.once('dialog', async dialog => { expect(dialog.message()).toContain('clear this chat'); await dialog.accept(); });
  await page.getByRole('button', { name: 'Disconnect ChatGPT', exact: true }).click();
  await expect.poll(() => mock.held.has('connection')).toBe(true);
  await expect(log.locator('li')).toHaveCount(0);
  await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('Codex model selector', { exact: true })).toHaveValue('');
  await expect(page.getByRole('button', { name: 'Send via Codex', exact: true })).toBeDisabled();
  await mock.held.get('connection')!.fulfill({ json: disconnected });
  await expect(page.getByText('ChatGPT disconnected', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Research provider', { exact: true })).toHaveCount(0);
});

test('New chat clears drafts and history and discards a late reply without changing login', async ({ page, codexMock: mock }) => {
  mock.status = { ...connected };
  await refreshConnection(page);
  await selectModel(page);
  const question = page.getByLabel('Research question', { exact: true });
  const log = page.getByRole('log', { name: 'Research conversation' });
  await question.fill('Old conversation');
  await page.getByRole('button', { name: 'Send via Codex', exact: true }).click();
  await expect(log.locator('li')).toHaveCount(2);
  await question.fill('Unsent draft');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'New chat', exact: true }).click();
  await expect(question).toHaveValue('Unsent draft');
  mock.hold.add('chat');
  await page.getByRole('button', { name: 'Send via Codex', exact: true }).click();
  await expect.poll(() => mock.chats.length).toBe(2);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'New chat', exact: true }).click();
  await reply(mock.held.get('chat')!).catch(() => {});
  await expect(log.locator('li')).toHaveCount(0);
  await expect(question).toHaveValue('');
  await expect(page.getByText('ChatGPT connected', { exact: true })).toBeVisible();
  expect(mock.calls.filter(path => path === 'login' || path === 'connection')).toEqual([]);
});

test('pending login can be cancelled before HTTP completes; leaving stops polls without replaying login or stale models', async ({ page, codexMock: mock }) => {
  await refreshConnection(page);
  mock.hold.add('login');
  await page.getByRole('button', { name: 'Get Codex login code', exact: true }).click();
  await expect.poll(() => mock.held.has('login')).toBe(true);
  await page.getByRole('button', { name: 'Cancel sign-in', exact: true }).click();
  await expect(page.getByText('ChatGPT disconnected', { exact: true })).toBeVisible();
  await mock.held.get('login')!.fulfill({ json: pending }).catch(() => {});
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
  mock.hold.delete('login');
  await page.getByRole('button', { name: 'Get Codex login code', exact: true }).click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveText('123456789');
  mock.hold.add('status');
  await expect.poll(() => mock.held.has('status')).toBe(true);
  await page.getByRole('tab', { name: 'OPERATOR', exact: true }).click();
  const stopped = mock.calls.length;
  await mock.held.get('status')!.fulfill({ json: { ...pending, user_code: 'STALE-CODE' } }).catch(() => {});
  await page.waitForTimeout(2300);
  expect(mock.calls).toHaveLength(stopped);
  mock.hold.delete('status');
  mock.status = { ...pending, user_code: 'CURRENT-CODE' };
  await page.getByRole('tab', { name: 'RESEARCH', exact: true }).click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveText('CURRENT-CODE');
  expect(mock.calls.filter(path => path === 'login')).toHaveLength(2);
  mock.status = { ...connected };
  mock.hold.add('models');
  await expect.poll(() => mock.held.has('models')).toBe(true);
  await page.getByRole('tab', { name: 'OPERATOR', exact: true }).click();
  await mock.held.get('models')!.fulfill({ json: { models: mock.models } }).catch(() => {});
  mock.hold.delete('models');
  mock.status = { ...disconnected, state: 'failed' };
  await page.getByRole('tab', { name: 'RESEARCH', exact: true }).click();
  await expect(page.getByText('ChatGPT sign-in failed', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Get Codex login code', exact: true })).toBeEnabled();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Codex model selector', { exact: true })).toBeDisabled();
});

test('runtime retry is manual with the same UUID; idle polling never interrupts chat and detects a closed generation gate', async ({ page, codexMock: mock }) => {
  test.setTimeout(120_000);
  mock.status = { ...connected };
  await refreshConnection(page);
  await selectModel(page);
  const question = page.getByLabel('Research question', { exact: true });
  const send = page.getByRole('button', { name: 'Send via Codex', exact: true });
  await question.fill('Keep the unconfirmed draft for a manual retry');
  mock.chatError = true;
  await send.click();
  await expect(page.getByText(/Reply not confirmed\. Your draft and frames are kept/)).toBeVisible();
  await expect(page.getByText(/Retry is manual and may consume more plan allowance/)).toBeVisible();
  await expect(question).toHaveValue('Keep the unconfirmed draft for a manual retry');
  await page.waitForTimeout(2200);
  expect(mock.chats).toHaveLength(1);
  await expect(send).toBeEnabled();
  page.once('dialog', async dialog => { expect(dialog.message()).toContain('may consume more plan allowance'); await dialog.dismiss(); });
  await send.click();
  expect(mock.chats).toHaveLength(1);
  mock.hold.add('chat');
  page.once('dialog', async dialog => { expect(dialog.message()).toContain('No paid API fallback'); await dialog.accept(); });
  await send.click();
  await expect.poll(() => mock.chats.length).toBe(2);
  expect(mock.chats[1]).toEqual(mock.chats[0]);
  const stopped = mock.calls.filter(path => path === 'status').length;
  await expect(page.getByRole('button', { name: 'Waiting for Codex...', exact: true })).toBeDisabled();
  await expect(page.getByLabel('Codex model selector', { exact: true })).toBeDisabled();
  await page.getByRole('tab', { name: 'OPERATOR', exact: true }).click();
  await page.getByRole('tab', { name: 'RESEARCH', exact: true }).click();
  // Real timers cross the 30s idle-poll boundary, not simulated live inference.
  await page.waitForTimeout(31_000);
  expect(mock.calls.filter(path => path === 'status')).toHaveLength(stopped);
  await expect(page.getByRole('button', { name: 'Waiting for Codex...', exact: true })).toBeVisible();
  await reply(mock.held.get('chat')!);
  await expect(page.getByRole('log', { name: 'Research conversation' }).locator('li')).toHaveCount(2);
  await expect(page.getByText('ChatGPT connected', { exact: true })).toBeVisible();
  mock.status = { ...connected, generation_enabled: false };
  await expect(page.getByText('Runtime verification did not pass; sending disabled.', { exact: true })).toBeVisible({ timeout: 35_000 });
  await expect(page.getByLabel('Codex model selector', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('Codex model selector', { exact: true })).toBeDisabled();
  await expect(send).toBeDisabled();
  expect(mock.chats).toHaveLength(2);
  await expect(page.getByLabel('Research provider', { exact: true })).toHaveCount(0);
});

test('untrusted device instructions are not navigable and the established UI fits mobile and desktop', async ({ page, codexMock: mock }, testInfo) => {
  mock.status = { ...pending, verification_url: 'https://auth.openai.com.evil.test/codex/device' };
  await refreshConnection(page);
  for (const [url, code] of [
    ['https://auth.openai.com.evil.test/codex/device', 'CODE-1234'],
    ['https://auth.openai.com/codex/device?redirect=evil', 'CODE-1234'],
    ['javascript:alert(document.cookie)', 'CODE-1234'],
    [deviceURL, '<script>secret</script>'],
    [deviceURL, 'A'.repeat(33)],
    [deviceURL, 'CODE\n'],
  ]) {
    mock.status = { ...pending, verification_url: url, user_code: code };
    await page.getByRole('button', { name: 'Refresh Codex connection' }).click();
    await expect(page.getByText('Waiting for ChatGPT sign-in', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open OpenAI device sign-in' })).toHaveCount(0);
    await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
    await expect(page.locator('.op-research a[href]')).toHaveCount(0);
  }
  mock.status = { ...pending, user_code: 'A'.repeat(32) };
  await page.getByRole('button', { name: 'Refresh Codex connection' }).click();
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveText(mock.status.user_code!);
  await expect(page.getByRole('link', { name: 'Open OpenAI device sign-in' })).toHaveAttribute('href', deviceURL);
  await expect(page.locator('.operator-app')).toHaveCSS('background-color', 'rgb(16, 18, 16)');
  await expect(page.locator('.operator-app')).toHaveCSS('font-family', /Open Sans/);
  await expect(page.getByRole('button', { name: 'Send via Codex', exact: true })).toHaveCSS('background-color', 'rgb(238, 182, 83)');
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => ({ document: document.documentElement.scrollWidth, body: document.body.scrollWidth }))).toEqual({ document: width, body: width });
    const code = (await page.getByLabel('ChatGPT device code', { exact: true }).boundingBox())!;
    expect(code.width).toBeGreaterThan(100);
    expect(code.x + code.width).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`research-codex-${width}.png`), fullPage: true });
  }
  expect(mock.chats).toEqual([]);
  await expect(page.getByText(/not free or unlimited/)).toBeVisible();
  await expect(page.getByText(/isolated bridge RAM/)).toBeVisible();
  await expect(page.getByText(/At most 8 Research requests per login/)).toBeVisible();
});

test('real operator logout revokes its token and stops a held pending poll without exposing a late code', async ({ page, codexMock: mock }) => {
  mock.status = { ...pending };
  await refreshConnection(page);
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toBeVisible();
  await page.getByLabel('Research question', { exact: true }).fill('Private draft cleared on logout');
  mock.hold.add('status');
  await expect.poll(() => mock.held.has('status')).toBe(true);
  const logout = page.waitForResponse(response => response.url().endsWith('/api/logout'));
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  expect((await logout).status()).toBe(204);
  await expect(page.getByLabel('Operator password', { exact: true })).toBeVisible();
  expect((await page.request.get('/api/sessions', { headers: { Authorization: `Bearer ${mock.token}` } })).status()).toBe(401);
  const stopped = mock.calls.length;
  await mock.held.get('status')!.fulfill({ json: { ...pending, user_code: 'LATE-CODE' } }).catch(() => {});
  await page.waitForTimeout(2300);
  expect(mock.calls).toHaveLength(stopped);
  await expect(page.getByLabel('ChatGPT device code', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => sessionStorage.getItem('evencomms.operator.token'))).toBeNull();
  expect(mock.chats).toEqual([]);
});


for (const ending of ['complete', 'failed', 'cleared'] as const) {
  test(`live Codex reply is visible before ${ending} and never commits unconfirmed text`, async ({ page, codexMock: mock }) => {
    mock.status = { ...connected };
    await refreshConnection(page);
    await selectModel(page);
    await page.evaluate(() => {
      const original = window.fetch;
      window.fetch = async (input, options) => {
        if (String(input) !== '/api/research/codex/chat') return original(input, options);
        const body = JSON.parse(String(options?.body));
        return new Response(new ReadableStream({
          start(controller) {
            (window as any).researchChunk = (event: unknown) => {
              try { controller.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\n')); } catch {}
            };
            (window as any).researchFinish = () => {
              controller.enqueue(new TextEncoder().encode(JSON.stringify({ type: 'done', response: {
                request_id: body.request_id, model: body.model, text: 'The complete streamed answer.', incomplete: false, usage: null,
              } }) + '\n'));
              controller.close();
            };
            options?.signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')));
          },
        }), { headers: { 'Content-Type': 'application/x-ndjson' } });
      };
    });
    await page.getByLabel('Research question', { exact: true }).fill('A streamed question');
    await page.getByRole('button', { name: 'Send via Codex', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Waiting for Codex...', exact: true })).toBeDisabled();
    await page.evaluate(() => (window as any).researchChunk({ type: 'text', text: 'The first streamed words' }));
    const log = page.getByRole('log', { name: 'Research conversation' });
    await expect(log.getByText('The first streamed words', { exact: true })).toBeVisible();
    await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('A streamed question');
    if (ending === 'complete') {
      await page.evaluate(() => (window as any).researchFinish());
      await expect(log.getByText('The complete streamed answer.', { exact: true })).toBeVisible();
      await expect(log.locator('li')).toHaveCount(2);
      await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('');
    } else if (ending === 'failed') {
      await page.evaluate(() => (window as any).researchChunk({ type: 'error', status: 429, detail: 'Usage limit reached' }));
      await expect(page.getByRole('alert').filter({ hasText: 'Reply not confirmed' })).toBeVisible();
      await expect(log.locator('li')).toHaveCount(0);
      await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('A streamed question');
    } else {
      page.once('dialog', dialog => dialog.accept());
      await page.getByRole('button', { name: 'New chat', exact: true }).click();
      await page.evaluate(() => (window as any).researchChunk({ type: 'text', text: 'Late text must be ignored' }));
      await expect(log.locator('li')).toHaveCount(0);
      await expect(log).not.toContainText('Late text');
      await expect(page.getByLabel('Research question', { exact: true })).toHaveValue('');
    }
  });
}
