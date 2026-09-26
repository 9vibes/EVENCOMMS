import { randomBytes } from 'node:crypto'
import { spawn } from 'node:child_process'
import { access } from 'node:fs/promises'
import { createServer } from 'node:net'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { chromium, expect } from '@playwright/test'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const assets = resolve(root, 'assets')
// Deliberately not configurable: never attach to a preview or production server.
const origin = 'http://127.0.0.1:8765'
const password = randomBytes(32).toString('hex')
const name = 'Alex (demo)'
const question = 'Which entrance should I use for the meeting?'
const reply = 'Use the north entrance. I will meet you at reception.'
const outputs = ['screenshot-operator.png', 'screenshot-wearer.png'].map(file => resolve(assets, file))
let backend, browser, backendClosed
let stopped = false
let stage = 'checking prerequisites and port 8765'
const interrupt = () => {
  stopped = true
  backend?.kill('SIGTERM')
  void browser?.close()
}
process.once('SIGINT', interrupt)
process.once('SIGTERM', interrupt)

async function api(path, token, body) {
  const response = await fetch(origin + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(5000),
    redirect: 'error',
  })
  if (!response.ok) throw new Error('Test API request failed')
  return response.json()
}

async function checkPage(page, secrets) {
  await page.evaluate(() => document.fonts.ready)
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'dark')
  await expect(page.locator('.operator-app, body:has(.wearer)')).toHaveCSS('background-color', 'rgb(16, 18, 16)')
  await expect(page.locator('.op-button-acid, .wearer-primary').first()).toHaveCSS('background-color', 'rgb(238, 182, 83)')
  const clean = await page.evaluate(values => {
    const text = document.body.innerText
    const width = document.documentElement.clientWidth
    return document.documentElement.scrollWidth === width && document.body.scrollWidth === width &&
      !values.some(value => text.includes(value)) &&
      !/Loading local pairing|Opening the conversation|CONNECTING|Processing preceding speech/.test(text) &&
      [...document.querySelectorAll('textarea')].every(input => input.value === '')
  }, secrets)
  if (!clean) throw new Error('Overflow, pending state, draft, or exposed credential')
  await page.mouse.move(0, 0)
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    window.scrollTo(0, 0)
  })
}

try {
  await access(assets)
  await access(resolve(root, 'frontend/dist/index.html'))
  await access(resolve(root, 'frontend/dist/glasses.html'))
  // A dual-stack bind refuses an existing IPv4 or IPv6 listener; no reuse or probes of live data.
  await new Promise((resolvePort, reject) => {
    const probe = createServer()
    probe.once('error', reject)
    probe.listen({ host: '::', port: 8765, exclusive: true }, () => probe.close(resolvePort))
  })

  stage = 'starting the isolated backend fixture'
  // Do not inherit application settings, production passwords, proxies, or model endpoints.
  const env = Object.fromEntries(['PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT']
    .filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]]))
  backend = spawn(process.env.EVENCOMMS_PYTHON || 'python3', ['-B', '-m', 'backend.e2e_server'], {
    cwd: root,
    env: { ...env, ADMIN_PASSWORD: password },
    stdio: 'ignore',
  })
  backendClosed = new Promise(resolveClose => {
    backend.once('error', () => { stopped = true; resolveClose() })
    backend.once('close', () => { stopped = true; resolveClose() })
  })
  let ready = false
  for (let attempt = 0; attempt < 100; attempt++) {
    if (stopped) throw new Error('Fixture exited')
    try {
      const response = await fetch(origin + '/health', { signal: AbortSignal.timeout(500), redirect: 'error' })
      if (response.ok) { ready = true; break }
    } catch { /* The owned fixture is still starting. */ }
    await delay(100)
  }
  if (!ready || stopped) throw new Error('Fixture unavailable')

  stage = 'creating the synthetic pairing'
  const { token } = await api('/api/login', null, { password })
  expect(await api('/api/sessions', token)).toEqual([])
  expect(await api('/api/status', token)).toMatchObject({
    stt_enabled: false, ai_configured: true, ollama_model: 'e2e-browser-route-only',
  })
  const { code } = await api('/api/pairings', token, {})
  const pairing = await api('/api/pair', null, { code, name })
  const secrets = [password, token, code, pairing.token, pairing.session_id]

  stage = 'opening the current built UI'
  browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  })
  let unexpectedRequest = false
  let pageError = false
  const contexts = []
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({
      viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC',
      colorScheme: 'dark', reducedMotion: 'reduce', serviceWorkers: 'block',
    })
    await context.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin || /\/transcribe$|\/suggest$/.test(url.pathname)) {
        unexpectedRequest = true
        return route.abort()
      }
      return route.continue()
    })
    await context.routeWebSocket('**/*', socket => {
      if (socket.url() === origin.replace('http:', 'ws:') + '/api/wearer') socket.connectToServer()
      else { unexpectedRequest = true; socket.close() }
    })
    context.on('page', page => page.on('pageerror', () => { pageError = true }))
    contexts.push(context)
  }
  await contexts[0].addInitScript(({ origin, token }) => {
    if (location.origin === origin) sessionStorage.setItem('evencomms.operator.token', token)
  }, { origin, token })
  await contexts[1].addInitScript(({ origin, pairing, name }) => {
    if (location.origin !== origin) return
    localStorage.setItem('evencomms.origin', origin)
    localStorage.setItem(`evencomms.wearer:${origin}`, JSON.stringify({
      ...pairing, name, draft: { text: '', pending: null },
    }))
  }, { origin, pairing, name })
  const operator = await contexts[0].newPage()
  const wearer = await contexts[1].newPage()
  await operator.goto(origin)
  await wearer.goto(origin + '/glasses.html?simulate=1')
  await expect(wearer.getByText('STONE CONNECTED', { exact: true })).toBeVisible()
  await expect(wearer.getByText('BROWSER SIMULATION', { exact: true })).toBeVisible()
  await expect(operator.getByRole('heading', { name, exact: true })).toBeVisible()

  stage = 'sending the typed question and human reply through the UI'
  await wearer.getByLabel('Unsent text').fill(question)
  await wearer.bringToFront()
  const hold = wearer.getByRole('button', { name: 'Hold to send', exact: true })
  await hold.focus()
  await wearer.keyboard.down('Space')
  await expect(wearer.getByRole('button', { name: 'Release to send', exact: true })).toBeVisible()
  await wearer.keyboard.up('Space')
  await expect(wearer.getByLabel('Unsent text')).toHaveValue('')
  await expect(operator.getByRole('log').getByText(question, { exact: true })).toBeVisible()
  await operator.getByLabel('Your reply', { exact: true }).fill(reply)
  await operator.getByRole('button', { name: 'Send reply', exact: true }).click()
  await expect(wearer.locator('.phone-reply')).toContainText(reply)
  await expect(wearer.getByLabel('Glasses text preview')).toContainText('Use the north entrance.')
  await operator.reload()

  stage = 'verifying clean, connected gallery content'
  await expect(operator.getByText('SERVICE ONLINE', { exact: true })).toBeVisible()
  await expect(operator.locator('.op-status-tag')).toHaveText('Connected')
  await expect(operator.getByRole('log').locator('li')).toHaveCount(2)
  await expect(operator.locator('.op-preview-screen')).toContainText(reply)
  await expect(operator.getByText('No active pairing code', { exact: true })).toBeVisible()
  await expect(operator.locator('[aria-label^="Pairing code "]')).toHaveCount(0)
  await expect(operator.locator('.op-service').getByText('Disabled', { exact: true })).toBeVisible()
  await expect(wearer.getByRole('button', { name: 'Reply', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(wearer.getByRole('status')).toHaveText('Operator reply received. Listening paused.')
  const history = await api(`/api/sessions/${pairing.session_id}/messages`, token)
  expect(history.map(({ role, text }) => ({ role, text }))).toEqual([
    { role: 'wearer', text: question }, { role: 'operator', text: reply },
  ])
  stage = 'checking operator overflow and privacy'
  await checkPage(operator, secrets)
  stage = 'checking wearer overflow and privacy'
  await checkPage(wearer, secrets)
  for (const [page, selector] of [
    [operator, '.op-message-list'], [operator, '.op-preview-screen > p'],
    [wearer, '.glasses-preview pre'], [wearer, '.phone-reply'],
  ]) {
    stage = `checking unclipped content in ${selector}`
    await expect(page.locator(selector)).toBeVisible()
    const unclipped = await page.locator(selector).evaluate(element => {
      const bounds = element.getBoundingClientRect()
      return bounds.left >= 0 && bounds.right <= document.documentElement.clientWidth &&
        element.scrollWidth <= element.clientWidth && element.scrollHeight <= element.clientHeight
    })
    if (!unclipped) throw new Error('Clipped gallery content')
  }
  if (unexpectedRequest || pageError || stopped) throw new Error('Unexpected network request or runtime failure')

  stage = 'capturing PNGs'
  await operator.screenshot({ path: outputs[0], fullPage: true, animations: 'disabled' })
  await wearer.screenshot({ path: outputs[1], fullPage: true, animations: 'disabled' })
  if (unexpectedRequest || pageError || stopped) throw new Error('Capture runtime failure')
} catch {
  // Never log API responses, browser storage, credentials, or backend output.
  console.error(`Screenshot generation failed while ${stage}.`)
  process.exitCode = 1
} finally {
  await browser?.close()
  if (backend) {
    backend.kill('SIGTERM')
    const timeout = setTimeout(() => backend.kill('SIGKILL'), 5000)
    await backendClosed
    clearTimeout(timeout)
  }
  process.removeListener('SIGINT', interrupt)
  process.removeListener('SIGTERM', interrupt)
}
if (!process.exitCode) for (const path of outputs) console.log(path)
