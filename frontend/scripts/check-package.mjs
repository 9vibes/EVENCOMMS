import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile, rm } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { chromium, expect } from '@playwright/test'
import { preparePackage } from './pack.mjs'

const frontend = resolve(import.meta.dirname, '..')
const prefix = '/private-builds/com.kunas.evencomms/0.4.3/'
const origin = 'https://stone.example.test'
const { directory, manifest } = await preparePackage({ origin, version: '0.4.3' })
const servers = []
let browser
let stage = 'package metadata'
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json' }

async function serve(nested) {
  const server = createServer(async (request, response) => {
    try {
      const path = decodeURIComponent(new URL(request.url, 'http://localhost').pathname)
      const packaged = !nested || path.startsWith(prefix)
      const root = packaged ? directory : resolve(frontend, 'dist')
      const relative = packaged && nested ? path.slice(prefix.length) : path.slice(1)
      const target = resolve(root, relative || 'index.html')
      if (!target.startsWith(root + sep)) throw Error('Outside static root')
      const data = await readFile(target)
      response.writeHead(200, { 'Content-Type': mime[extname(target)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' })
      response.end(data)
    } catch {
      response.writeHead(404)
      response.end()
    }
  })
  servers.push(server)
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  return `http://127.0.0.1:${server.address().port}`
}

try {
  assert.equal(manifest.entrypoint, 'index.html')
  assert.equal(await readFile(resolve(directory, 'index.html'), 'utf8'), await readFile(resolve(directory, 'glasses.html'), 'utf8'))
  const bases = [await serve(false), await serve(true)]
  assert.match(await (await fetch(bases[1] + '/')).text(), /<title>EVENCOMMS \/ Operator<\/title>/)
  browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) })
  for (const [index, base] of bases.entries()) {
    const mount = index ? prefix : '/'
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
    const requests = []
    const external = []
    await context.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== base) { external.push(url.origin); await route.abort(); return }
      requests.push(url.pathname)
      await route.continue()
    })
    try {
      const page = await context.newPage()
      for (const entry of ['', 'index.html', 'glasses.html']) {
        stage = `companion ${index ? 'nested' : 'root'} ${entry || 'default entry'}`
        await page.goto(base + mount + entry)
        await expect(page.getByRole('heading', { name: 'Pair your connection', exact: true })).toBeVisible()
        await expect(page.locator('.wearer-tag')).toHaveText('G2 COMPANION')
        await expect(page.getByLabel('Stone address', { exact: true })).toHaveValue(origin)
        await expect(page.getByLabel('Your name', { exact: true })).toBeVisible()
        await expect(page.getByLabel('Pairing code', { exact: true })).toBeVisible()
        await expect(page.locator('.operator-app')).toHaveCount(0)
        await expect(page.getByLabel('Operator password', { exact: true })).toHaveCount(0)
        await expect(page.locator('.wearer-header a')).toHaveCount(0)
        const before = page.url()
        await page.locator('.wearer-brand').click()
        assert.equal(page.url(), before)
        await page.reload()
        await expect(page.getByRole('heading', { name: 'Pair your connection', exact: true })).toBeVisible()
        await page.getByRole('link', { name: 'browser simulation', exact: true }).click()
        assert.equal(new URL(page.url()).pathname, new URL(before).pathname)
        assert.equal(new URL(page.url()).search, '?simulate=1')
        await expect(page.locator('.wearer-tag')).toHaveText('BROWSER SIMULATION')
        await expect(page.getByLabel('Stone address', { exact: true })).toHaveValue(origin)
        await page.reload()
        await expect(page.locator('.wearer-tag')).toHaveText('BROWSER SIMULATION')
        for (const width of [320, 390, 1440]) {
          await page.setViewportSize({ width, height: 1000 })
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
        }
      }
      assert.deepEqual(external, [])
      assert.ok(requests.every(path => path.startsWith(mount)), 'Navigation escaped the installed package prefix')
      assert.ok(!requests.some(path => /\/assets\/(?:operator|hls)[.-]/.test(path)), 'Operator-only assets requested')
      assert.ok(requests.some(path => /\/assets\/glasses-.*\.js$/.test(path)), 'Wearer bundle was not loaded')
    } finally {
      await context.close()
    }
  }
  console.log('PASS: companion-only root/index/legacy entry, reload/navigation, nested hosting and mobile/desktop layout; web operator root unchanged')
} catch (error) {
  console.error(`Package browser check failed at ${stage}: ${error.message}`)
  process.exitCode = 1
} finally {
  await browser?.close()
  for (const server of servers) {
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
  await rm(directory, { recursive: true, force: true })
}
