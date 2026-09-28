import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile, rm } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { chromium, expect } from '@playwright/test'
import { preparePackage } from './pack.mjs'

const servers = []
const requests = []
const allowed = new Set()
const pairCode = 'TESTCODE'
const wearerName = 'Private fixture wearer'
const token = 'private-fixture-token-not-a-real-credential'
const sessionId = '4aa647f6-e60c-4ec3-a42e-90407e4ac052'
let pairStatus = 401
let holdHealth
let packageInfo
let browser
let stage = 'setup'

async function listen(handler) {
  const server = createServer(handler)
  servers.push(server)
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  return `http://127.0.0.1:${server.address().port}`
}

try {
  const api = await listen(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname
    const origin = request.headers.origin
    const record = { method: request.method, path, origin, headers: request.headers, body: '' }
    requests.push(record)
    for await (const chunk of request) record.body += chunk
    const permitted = allowed.has(origin)
    const headers = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
    if (permitted) headers['Access-Control-Allow-Origin'] = origin
    if (request.method === 'OPTIONS') {
      response.writeHead(permitted ? 204 : 403, { ...headers,
        'Access-Control-Allow-Methods': 'GET, POST', 'Access-Control-Allow-Headers': 'Content-Type' })
      response.end()
    } else if (path === '/health') {
      if (holdHealth) await holdHealth
      response.writeHead(200, headers)
      response.end(JSON.stringify({ status: 'ok' }))
    } else if (path === '/api/pair' && request.method === 'POST') {
      response.writeHead(pairStatus, headers)
      response.end(JSON.stringify(pairStatus === 200 ? { token, session_id: sessionId }
        : { detail: 'private upstream body must not be displayed' }))
    } else {
      response.writeHead(404, headers)
      response.end('{}')
    }
  })
  const oldHttp = process.env.ALLOW_HTTP_DEV
  try {
    process.env.ALLOW_HTTP_DEV = '1'
    packageInfo = await preparePackage({ origin: api, version: '0.4.5' })
  } finally {
    if (oldHttp === undefined) delete process.env.ALLOW_HTTP_DEV
    else process.env.ALLOW_HTTP_DEV = oldHttp
  }
  let localConfigReads = 0
  const site = await listen(async (request, response) => {
    try {
      const path = new URL(request.url, 'http://localhost').pathname
      if (path === '/opaque.html') {
        response.writeHead(200, { 'Content-Type': 'text/html' })
        response.end('<iframe title="Opaque companion" sandbox="allow-scripts allow-forms" src="/index.html?simulate=1&private-query=not-for-diagnostics"></iframe>')
        return
      }
      if (path === '/stone.json') { localConfigReads++; throw Error('Deliberately unavailable local config') }
      const target = resolve(packageInfo.directory, path.slice(1) || 'index.html')
      if (!target.startsWith(packageInfo.directory + sep)) throw Error('Invalid static path')
      const bytes = await readFile(target)
      const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }
      response.writeHead(200, { 'Content-Type': types[extname(target)] ?? 'application/octet-stream',
        'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store' })
      response.end(bytes)
    } catch { response.writeHead(404); response.end() }
  })
  browser = await chromium.launch({ headless: true, args: ['--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'],
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) })
  const context = await browser.newContext({ viewport: { width: 390, height: 1000 } })
  // Playwright request interception can bypass preflights, even for unmatched
  // routes. Use actual local traffic and block non-loopback DNS at launch instead.
  const external = []
  context.on('request', request => {
    const origin = new URL(request.url()).origin
    if (![api, site].includes(origin)) external.push(origin)
  })
  const page = await context.newPage()
  const details = page.locator('details.connection-details')
  stage = 'embedded target without local JSON'
  await page.goto(site + '/index.html?simulate=1&private-query=not-for-diagnostics')
  await expect(page.getByLabel('Stone address', { exact: true })).toHaveValue(api)
  assert.equal(localConfigReads, 0)
  await page.getByLabel('Your name', { exact: true }).fill(wearerName)
  await page.getByLabel('Pairing code', { exact: true }).fill(pairCode)

  stage = 'rejected CORS preflight stops before pairing'
  await page.getByRole('button', { name: 'Connect to Stone', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('No pairing code was sent')
  assert.equal(requests.filter(item => item.path === '/api/pair').length, 0)
  assert.ok(requests.some(item => item.method === 'OPTIONS' && item.path === '/health' && item.origin === site),
    JSON.stringify({ site, requests: requests.map(({ method, path, origin }) => ({ method, path, origin })),
      message: await page.getByRole('alert').innerText() }))
  await expect(details).toHaveAttribute('open', '')
  const detailText = await details.innerText()
  for (const privateValue of [pairCode, wearerName, token, 'private-query', 'not-for-diagnostics']) {
    assert.ok(!detailText.includes(privateValue))
  }
  await expect(details).toContainText(site)
  await expect(details).toContainText(api + '/api/pair')

  stage = 'explicit origin and credential-free connection check'
  allowed.add(site)
  let releaseHealth
  holdHealth = new Promise(resolve => { releaseHealth = resolve })
  await page.getByRole('button', { name: 'Check connection', exact: true }).click()
  await expect(page.getByLabel('Stone address', { exact: true })).toBeDisabled()
  await expect(page.getByLabel('Pairing code', { exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Connect to Stone', exact: true })).toBeDisabled()
  releaseHealth()
  holdHealth = null
  await expect(details.getByRole('status')).toContainText('Connection check passed')
  assert.equal(requests.filter(item => item.path === '/api/pair').length, 0)
  for (const item of requests.filter(item => item.path === '/health')) {
    assert.equal(item.body, '')
    assert.equal(item.headers.authorization, undefined)
    assert.equal(item.headers.cookie, undefined)
    assert.equal(item.headers.referer, undefined)
  }

  stage = 'HTTP pairing error is distinct from network failure'
  await page.getByRole('button', { name: 'Connect to Stone', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Pairing rejected (HTTP 401)')
  const posts = requests.filter(item => item.method === 'POST' && item.path === '/api/pair')
  assert.equal(posts.length, 1)
  assert.deepEqual(JSON.parse(posts[0].body), { code: pairCode, name: wearerName })
  assert.ok(!await page.getByRole('alert').innerText().then(text => text.includes('private upstream body')))

  stage = 'opaque effective origin is reported without inference'
  const opaque = await context.newPage()
  await opaque.goto(site + '/opaque.html')
  const frame = opaque.frameLocator('iframe')
  await expect(frame.getByLabel('Stone address', { exact: true })).toHaveValue(api)
  const count = requests.filter(item => item.path === '/api/pair').length
  await frame.getByLabel('Pairing code', { exact: true }).fill(pairCode)
  await frame.getByRole('button', { name: 'Connect to Stone', exact: true }).click()
  await expect(frame.getByRole('alert')).toContainText('No pairing code was sent')
  await expect(frame.locator('details.connection-details')).toContainText('null (opaque origin)')
  const values = await frame.locator('details.connection-details dd').allTextContents()
  assert.equal(values[0], site)
  assert.equal(values[1], 'null (opaque origin)')
  assert.ok(requests.some(item => item.method === 'OPTIONS' && item.path === '/health' && item.origin === 'null'))
  assert.equal(requests.filter(item => item.path === '/api/pair').length, count)
  assert.equal(localConfigReads, 0)

  stage = 'successful pairing stays usable when storage writes fail'
  const memory = await context.newPage()
  await memory.addInitScript(() => { Storage.prototype.setItem = () => { throw new DOMException('test storage failure', 'QuotaExceededError') } })
  await memory.goto(site + '/index.html?simulate=1')
  await expect(memory.getByLabel('Stone address', { exact: true })).toHaveValue(api)
  await memory.getByLabel('Pairing code', { exact: true }).fill(pairCode)
  pairStatus = 200
  await memory.getByRole('button', { name: 'Connect to Stone', exact: true }).click()
  await expect(memory.getByRole('alert').first()).toContainText('Paired for this page only')
  await expect(memory.getByRole('heading', { name: 'G2 wearer', exact: true })).toBeVisible()
  await expect(memory.getByRole('heading', { name: 'Pair your connection', exact: true })).toHaveCount(0)
  assert.equal(requests.filter(item => item.method === 'POST' && item.path === '/api/pair').length, 2)
  assert.ok(!(await memory.locator('details.connection-details').innerText()).includes(token))

  stage = 'blocked storage permits memory-only pairing and clearing without a reload'
  await memory.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('test blocked storage', 'SecurityError') } })
  })
  await memory.reload()
  await expect(memory.getByRole('alert')).toContainText('Browser storage is unavailable')
  await expect(memory.getByLabel('Stone address', { exact: true })).toHaveValue(api)
  await memory.getByLabel('Pairing code', { exact: true }).fill(pairCode)
  await memory.getByRole('button', { name: 'Connect to Stone', exact: true }).click()
  await expect(memory.getByRole('heading', { name: 'G2 wearer', exact: true })).toBeVisible()
  assert.equal(requests.filter(item => item.method === 'POST' && item.path === '/api/pair').length, 3)
  memory.once('dialog', dialog => dialog.accept())
  await memory.getByRole('button', { name: 'Clear device', exact: true }).click()
  await expect(memory.getByRole('heading', { name: 'Pair your connection', exact: true })).toBeVisible()
  await expect(memory.getByRole('heading', { name: 'G2 wearer', exact: true })).toHaveCount(0)
  await expect(memory.getByRole('alert')).toContainText('Pairing cleared for this page only')

  stage = 'responsive diagnostic layout'
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  }
  assert.deepEqual(external, [])
  await context.close()
  console.log('PASS: real CORS denial prevents pairing POST, embedded target survives missing local JSON, opaque origins are explicit, HTTP/storage outcomes and responsive diagnostics verified')
} catch (error) {
  console.error(`Pairing browser check failed at ${stage}: ${error.message}`)
  process.exitCode = 1
} finally {
  await browser?.close()
  for (const server of servers) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
  if (packageInfo) await rm(packageInfo.directory, { recursive: true, force: true })
}
