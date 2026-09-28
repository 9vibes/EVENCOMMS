// Real private fixture video/canvas/auth; only Research provider-facing routes are synthetic.
import { stat } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';

process.env.DEBUG = '';
process.env.PWDEBUG = '0';

let stage = 'configuration';
let browser;
let context;
let token;
let timer;
let failed = false;

async function check() {
  const base = new URL(process.env.STREAM_TEST_URL);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password
      || base.pathname !== '/' || base.search || base.hash || !process.env.ADMIN_PASSWORD) {
    throw new Error('Explicit origin and password required');
  }
  const screenshot = process.env.RESEARCH_TEST_SCREENSHOT;
  if (screenshot && !(await stat(dirname(resolve(screenshot)))).isDirectory()) {
    throw new Error('Screenshot parent must exist');
  }
  stage = 'Playwright and Chromium';
  const { chromium, expect } = await import('@playwright/test');
  browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  context = await browser.newContext({
    baseURL: base.origin, viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block',
  });
  context.setDefaultTimeout(8000);
  const fakeKey = 'SYNTHETIC-DEVICE-CODE';
  const models = ['synthetic-smoke-vision-a', 'synthetic-smoke-vision-b'];
  const reply = 'Synthetic reply only: <img src="https://synthetic.invalid/pixel"> **not HTML**';
  const chats = [];
  let connected = false;
  let connections = 0;
  let modelCalls = 0;
  let permittedSends = 0;
  let violation = false;
  let mediaResponses = 0;
  let settingsCalls = 0;

  // Fail closed, including redirects/popups, so a route typo cannot reach OpenAI.
  await context.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const body = request.postData() ?? '';
    if (url.origin !== base.origin || url.username || url.password
        || (url.href + JSON.stringify(request.headers()) + body).includes(fakeKey)) {
      violation = true;
      return route.abort();
    }
    if (path.startsWith('/api/research/')) {
      let response;
      if (path === '/api/research/codex/status' && method === 'GET') {
        response = { enabled: true, state: connected ? 'connected' : 'disconnected', generation_enabled: connected, verification_url: null, user_code: null };
      } else if (path === '/api/research/codex/login' && method === 'POST') {
        if (body !== '{}') violation = true;
        connected = true;
        connections++;
        response = { enabled: true, state: 'connected', generation_enabled: true, verification_url: null, user_code: null };
      } else if (path === '/api/research/codex/models' && method === 'GET' && connected) {
        modelCalls++;
        response = { models: models.map(id => ({ id, image: true })) };
      } else if (path === '/api/research/codex/chat' && method === 'POST' && connected) {
        try {
          const chat = JSON.parse(body);
          chats.push(chat);
          if (chats.length !== permittedSends) violation = true;
          response = { request_id: chat.request_id, model: chat.model, text: reply,
            incomplete: false, usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 } };
        } catch { violation = true; }
      }
      if (!response) {
        violation = true;
        return route.abort();
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) });
    }
    if (path === '/api/stream/settings') settingsCalls++;
    // No pairing, wearer messages, settings changes or other mutations are allowed.
    if (!['GET', 'HEAD'].includes(method)
        && !(method === 'POST' && ['/api/login', '/api/logout', '/api/stream/playback-session'].includes(path))) {
      violation = true;
      return route.abort();
    }
    return route.continue();
  });
  await context.routeWebSocket('**/*', socket => {
    const url = new URL(socket.url());
    if (url.origin !== base.origin.replace(/^http/, 'ws')) {
      violation = true;
      socket.close();
    } else socket.connectToServer();
  });
  await context.addInitScript(key => {
    window.researchStorageLeak = false;
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (name, value) {
      if (`${name}${value}`.includes(key) || `${value}`.includes('data:image/jpeg;base64,')) {
        window.researchStorageLeak = true;
      }
      return original.call(this, name, value);
    };
  }, fakeKey);
  const page = await context.newPage();
  page.on('response', response => {
    if (new URL(response.url()).pathname.startsWith('/api/stream/live/') && response.status() === 200) mediaResponses++;
  });
  stage = 'real UI login';
  await page.goto('/');
  await page.getByLabel('Operator password').fill(process.env.ADMIN_PASSWORD);
  await page.getByRole('button', { name: 'Enter console', exact: true }).click();
  const research = page.getByRole('tab', { name: 'RESEARCH', exact: true });
  await expect(research).toBeVisible();
  token = await page.evaluate(() => sessionStorage.getItem('evencomms.operator.token'));
  expect(Boolean(token)).toBe(true);
  const statusResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/stream/status' && response.status() === 200);
  await research.click();
  const status = await (await statusResponse).json();
  expect(status.enabled && status.media_available && status.online && Boolean(status.publisher_session_id)).toBe(true);

  const panel = page.getByRole('tabpanel', { name: 'RESEARCH', exact: true });
  const log = page.getByRole('log', { name: 'Research conversation', exact: true });
  const question = page.getByRole('textbox', { name: 'Research question', exact: true });
  await expect(page.getByLabel('OpenAI API key', { exact: true })).toHaveCount(0);
  const model = page.getByLabel('Codex model selector', { exact: true });
  const send = page.getByRole('button', { name: 'Send via Codex', exact: true });
  const capture = page.getByRole('button', { name: 'Capture frame', exact: true });
  const drafts = page.getByRole('img', { name: /^Draft frame \d+$/ });
  const draft = number => page.getByRole('img', { name: `Draft frame ${number}`, exact: true });

  async function playing() {
    await page.waitForFunction(() => {
      const video = document.querySelector('video');
      return video?.videoWidth === 640 && video.videoHeight === 360 && video.readyState >= 2 && !video.paused;
    }, null, { timeout: 20_000 });
    await expect(page.getByText('Playing live preview', { exact: true })).toBeVisible();
  }
  async function image(number) {
    await expect(draft(number)).toBeVisible();
    await expect.poll(() => draft(number).evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true);
    const value = await draft(number).evaluate(img => ({
      src: img.src, width: img.naturalWidth, height: img.naturalHeight,
    }));
    expect(/^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/.test(value.src)).toBe(true);
    const bytes = Buffer.from(value.src.split(',')[1], 'base64');
    expect(bytes.length > 0 && bytes.length <= 1024 * 1024).toBe(true);
    expect(bytes[0] === 0xff && bytes[1] === 0xd8 && bytes.at(-2) === 0xff && bytes.at(-1) === 0xd9).toBe(true);
    expect(value.width > 0 && value.height > 0 && value.width <= 1280 && value.height <= 1280).toBe(true);
    expect(await draft(number).locator('..').locator('time').getAttribute('datetime')).toMatch(/^\d{4}-/);
    return value;
  }
  async function storageSafe() {
    expect(await page.evaluate(key => !window.researchStorageLeak
      && !JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie]).includes(key)
      && !JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]).includes('data:image/jpeg;base64,'), fakeKey)).toBe(true);
  }
  async function explicitSend() {
    await expect(send).toBeEnabled();
    expect(chats.length).toBe(permittedSends);
    permittedSends++;
    await send.click();
    await expect.poll(() => chats.length).toBe(permittedSends);
    await expect(question).toHaveValue('');
    await expect(drafts).toHaveCount(0);
    await expect(log.getByText(reply, { exact: true })).toHaveCount(permittedSends);
    await expect(log.locator('img[src^="https:"], strong')).toHaveCount(0);
    const chat = chats.at(-1);
    expect(Object.keys(chat).sort()).toEqual(['messages', 'model', 'request_id']);
    expect(chat.model).toBe(models[1]);
    expect(chat.request_id).toMatch(/^[0-9a-f-]{36}$/i);
    expect(new Set(chats.map(item => item.request_id)).size).toBe(chats.length);
    expect(JSON.stringify(chat).includes(fakeKey)).toBe(false);
    expect(Buffer.byteLength(JSON.stringify(chat))).toBeLessThanOrEqual(9 * 1024 * 1024);
    expect(chat.messages.length).toBe(2 * permittedSends - 1);
    expect(chat.messages.length).toBeLessThanOrEqual(20);
    let imageCount = 0;
    let characters = 0;
    for (const [index, message] of chat.messages.entries()) {
      const user = index % 2 === 0;
      expect(message.role).toBe(user ? 'user' : 'assistant');
      expect(Object.keys(message).sort()).toEqual(user ? ['images', 'role', 'text'] : ['role', 'text']);
      expect(Array.from(message.text).length).toBeLessThanOrEqual(user ? 8000 : 16000);
      characters += Array.from(message.text).length;
      expect((message.images ?? []).length).toBeLessThanOrEqual(user ? 3 : 0);
      for (const jpeg of message.images ?? []) {
        expect(typeof jpeg === 'string' && /^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/.test(jpeg)).toBe(true);
        expect(Buffer.from(jpeg.split(',')[1], 'base64').length).toBeLessThanOrEqual(1024 * 1024);
        imageCount++;
      }
    }
    expect(imageCount).toBeLessThanOrEqual(6);
    expect(characters).toBeLessThanOrEqual(64000);
    if (chats.length > 1) {
      expect(chat.messages.slice(0, -2)).toEqual(chats.at(-2).messages);
      expect(chat.messages.at(-2)).toEqual({ role: 'assistant', text: reply });
    }
    await storageSafe();
  }

  stage = 'local decoded JPEG captures before code login';
  await playing();
  stage = 'disconnected Research controls and compact preview';
  expect(mediaResponses).toBeGreaterThan(0);
  await expect(page.getByText('ChatGPT disconnected', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Encoder configuration', exact: true })).toHaveCount(0);
  await expect(model).toHaveValue('');
  const video = await page.locator('video').elementHandle();
  const before = await video.evaluate(video => ({ src: video.currentSrc, time: video.currentTime }));
  stage = 'full-frame native JPEG thumbnail and dimensions';
  await expect(page.locator('.op-live-frame')).toHaveAttribute('data-zoom', '1');
  await capture.click();
  const full = await image(1);
  // The bordered CSS viewport can add a few letterbox pixels to the source size.
  expect(Math.abs(full.width - 640)).toBeLessThan(8);
  expect(Math.abs(full.height - 360)).toBeLessThan(8);
  stage = 'zoomed native JPEG thumbnail and crop dimensions';
  await page.getByRole('button', { name: 'Zoom in', exact: true }).click();
  await page.getByRole('button', { name: 'Zoom in', exact: true }).click();
  await expect(page.locator('.op-live-frame')).toHaveAttribute('data-zoom', '2');
  await capture.click();
  const cropped = await image(2);
  expect(cropped.width).toBeLessThan(full.width);
  expect(Math.abs(cropped.width * 2 - full.width)).toBeLessThanOrEqual(1);
  expect(Math.abs(cropped.height * 2 - full.height)).toBeLessThanOrEqual(1);
  stage = 'three-draft limit and discard retention';
  await capture.click();
  const third = await image(3);
  await expect(drafts).toHaveCount(3);
  await expect(capture).toBeDisabled();
  await page.getByRole('button', { name: 'Discard frame 2', exact: true }).click();
  await expect(drafts).toHaveCount(2);
  expect((await image(1)).src === full.src && (await image(2)).src === third.src).toBe(true);
  await expect(capture).toBeEnabled();
  await expect(send).toBeDisabled();
  expect(chats.length === 0 && connections === 0 && modelCalls === 0).toBe(true);
  stage = 'capture preserves playing video element and source';
  await expect.poll(() => video.evaluate((video, before) => video === document.querySelector('video')
    && video.currentSrc === before.src && !video.paused && video.currentTime >= before.time + 2, before)).toBe(true);

  stage = 'Research tab memory and mobile draft layout';
  const prompt = 'Synthetic test pattern: compare the full frame and cropped frame.';
  await question.fill(prompt);
  await page.getByRole('tab', { name: 'OPERATOR', exact: true }).click();
  await expect(page.locator('video')).toHaveCount(0);
  await research.click();
  await playing(); // The remounted player may legitimately have a different blob URL.
  await expect(question).toHaveValue(prompt);
  expect((await image(1)).src === full.src && (await image(2)).src === third.src).toBe(true);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const locator of [question, capture, draft(1), draft(2), model, send]) {
      const box = await locator.boundingBox();
      expect(Boolean(box && box.width > 0 && box.x >= 0 && box.x + box.width <= width + 1)).toBe(true);
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });

  stage = 'synthetic connection, explicit model selection and JPEG-only chat wire';
  await storageSafe();
  await page.getByRole('button', { name: 'Get Codex login code', exact: true }).click();
  await expect(model.locator('option')).toHaveCount(3);
  await expect(model).toHaveValue('');
  await expect(send).toBeDisabled();
  await model.selectOption(models[1]);
  expect(connections === 1 && modelCalls > 0 && chats.length === 0).toBe(true);

  stage = 'Send and keyboard submission wait for capture encoding';
  await page.evaluate(() => {
    const original = HTMLCanvasElement.prototype.toBlob;
    window.researchBlobReady = false;
    HTMLCanvasElement.prototype.toBlob = function (callback, ...args) {
      HTMLCanvasElement.prototype.toBlob = original;
      return original.call(this, blob => {
        window.releaseResearchBlob = () => callback(blob);
        window.researchBlobReady = true;
      }, ...args);
    };
  });
  await capture.click();
  await page.waitForFunction(() => window.researchBlobReady);
  await expect(send).toBeDisabled();
  await question.press('Control+Enter');
  expect(chats).toHaveLength(0);
  await expect(question).toHaveValue(prompt);
  await page.evaluate(() => window.releaseResearchBlob());
  await image(3);
  await expect(send).toBeEnabled();
  expect(chats).toHaveLength(0);
  await page.getByRole('button', { name: 'Discard frame 3', exact: true }).click();
  await explicitSend();
  expect(chats[0].messages).toEqual([{ role: 'user', text: prompt, images: [full.src, third.src] }]);
  await question.fill('Synthetic follow-up: retain the previous frames.');
  await capture.click();
  const followup = await image(1);
  await explicitSend();
  expect(chats[1].messages.at(-1).images).toEqual([followup.src]);
  await expect(log.getByRole('img', { name: 'Sent frame', exact: true })).toHaveCount(3);
  if (screenshot) {
    stage = 'private synthetic screenshot with code login and no encoder settings';
    expect(settingsCalls).toBe(0);
    await expect(panel.getByText('Encoder configuration', { exact: true })).toHaveCount(0);
    await panel.screenshot({ path: screenshot });
  }

  stage = 'six-image and bounded follow-up history limits';
  for (let number = 1; number <= 3; number++) {
    await capture.click();
    await image(number);
  }
  await expect(capture).toBeDisabled();
  await question.fill('Synthetic final image turn.');
  await explicitSend();
  await expect(capture).toBeDisabled(); // Six images remain in sent history, no draft.
  for (let turn = 4; turn <= 10; turn++) {
    await question.fill(`Synthetic text-only turn ${turn}.`);
    await explicitSend();
  }
  await question.fill('Synthetic over-budget turn must not send.');
  await expect(send).toBeDisabled();
  await expect(page.getByText('This conversation exceeds 20 messages. Start a New chat; no history is dropped automatically.', { exact: true })).toBeVisible();

  stage = 'New chat rejects a late native canvas JPEG callback';
  page.on('dialog', dialog => dialog.accept());
  const newChat = page.getByRole('button', { name: 'New chat', exact: true });
  await newChat.click();
  await expect(log.getByRole('img')).toHaveCount(0);
  await expect(question).toHaveValue('');
  await expect(capture).toBeEnabled();
  await question.fill('Synthetic draft cleared during capture.');
  // Delay exactly one real JPEG callback, without replacing the canvas pixels/blob.
  await page.evaluate(() => {
    const original = HTMLCanvasElement.prototype.toBlob;
    window.researchBlobReady = false;
    HTMLCanvasElement.prototype.toBlob = function (callback, ...args) {
      HTMLCanvasElement.prototype.toBlob = original;
      return original.call(this, blob => {
        window.releaseResearchBlob = () => callback(blob);
        window.researchBlobReady = true;
      }, ...args);
    };
  });
  await capture.click();
  await page.waitForFunction(() => window.researchBlobReady);
  await newChat.click();
  await page.evaluate(() => window.releaseResearchBlob());
  await expect(capture).toBeEnabled();
  await expect(drafts).toHaveCount(0);
  await expect(question).toHaveValue('');
  await expect(log.getByText(reply, { exact: true })).toHaveCount(0);
  await page.waitForTimeout(300);
  await expect(drafts).toHaveCount(0);
  await capture.click();
  await image(1); // The fresh generation still captures normally.
  await newChat.click();
  await expect(drafts).toHaveCount(0);
  expect(chats.length).toBe(10);
  expect(violation).toBe(false);
  expect(settingsCalls).toBe(0);
  await storageSafe();

  stage = 'own operator logout and token revocation';
  const [logout] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/logout'),
    page.getByRole('button', { name: 'Sign out', exact: true }).click(),
  ]);
  expect(logout.status()).toBe(204);
  await expect(page.getByLabel('Operator password')).toBeVisible();
  const revoked = await context.request.get('/api/stream/status', {
    headers: { Authorization: `Bearer ${token}` }, timeout: 3000, maxRedirects: 0,
  });
  expect(revoked.status()).toBe(401);
  token = null;
}

try {
  const interrupted = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('Deadline exceeded')), 80_000);
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
      const logout = await context.request.post('/api/logout', {
        headers: { Authorization: `Bearer ${token}` }, timeout: 3000, maxRedirects: 0,
      });
      if (![204, 401].includes(logout.status())) failed = true;
    }
  } catch { failed = true; }
  try { await context?.close(); } catch { failed = true; }
  try { await browser?.close(); } catch { failed = true; }
}
if (failed) {
  console.error(`Research browser smoke failed during ${stage}; private diagnostics suppressed.`);
  process.exitCode = 1;
} else {
  console.log('Research browser smoke passed; real JPEG crops, local drafts, explicit synthetic chat, limits and New chat race verified; own login revoked.');
}
