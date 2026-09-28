import { test, expect, type Page } from '@playwright/test'

const password = 'evencomms-e2e-only-password'
const question = 'Where is the meeting'
const reply = 'Meet at the north entrance. ' +
  'Follow the blue signs past reception and wait beside the information desk. '.repeat(5) +
  'FINAL PAGE: The operator will meet you there.'

async function pair(operator: Page, wearer: Page, name: string) {
  await operator.goto('/')
  await operator.getByLabel('Operator password').fill(password)
  await operator.getByRole('button', { name: 'Enter console' }).click()
  await expect(operator.getByText('SERVICE ONLINE', { exact: true })).toBeVisible()
  await expect(operator.locator('.op-service').getByText('Disabled', { exact: true })).toBeVisible()
  await operator.getByRole('button', { name: 'Pair a wearer', exact: true }).click()
  const code = operator.locator('strong[aria-label^="Pairing code "]')
  await expect(code).toHaveText(/^[A-Z2-9]{8}$/)
  await wearer.goto('/glasses.html?simulate=1')
  await wearer.getByLabel('Your name').fill(name)
  await wearer.getByLabel('Pairing code', { exact: true }).fill((await code.innerText()).trim())
  await wearer.getByRole('button', { name: 'Connect to Stone' }).click()
  await expect(wearer.getByText('STONE CONNECTED', { exact: true })).toBeVisible()
  await operator.getByRole('button', { name: new RegExp(name + '.*Connected') }).click()
  await expect(operator.getByRole('heading', { name, exact: true })).toBeVisible()
  await expect(operator.getByRole('link', { name: /Open wearer client/ })).toHaveCount(0)
}

async function holdToSend(wearer: Page) {
  // A background/blurred page intentionally cancels holds in the real app.
  await wearer.bringToFront()
  const draft = await wearer.getByLabel('Unsent text').inputValue()
  const button = wearer.getByRole('button', { name: 'Hold to send', exact: true })
  await button.scrollIntoViewIfNeeded()
  const bounds = await button.boundingBox()
  if (!bounds) throw new Error('Hold button has no bounding box')
  await wearer.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
  await wearer.mouse.down()
  try {
    await wearer.waitForTimeout(750)
    await expect(wearer.getByRole('button', { name: 'Release to send', exact: true })).toBeVisible()
    await expect(wearer.getByLabel('Unsent text')).toHaveValue(draft)
  } finally {
    await wearer.mouse.up()
  }
  await expect(wearer.getByLabel('Unsent text')).toHaveValue('')
}

async function noOverflow(page: Page) {
  await page.mouse.move(0, 0)
  const surface = page.locator('.operator-app, body:has(.wearer)')
  await expect(surface).toHaveCSS('background-color', 'rgb(16, 18, 16)')
  await expect(surface).toHaveCSS('color', 'rgb(232, 233, 227)')
  await expect(surface).toHaveCSS('font-family', /Open Sans/)
  await expect(page.locator('.op-button-acid, .wearer-primary').first()).toHaveCSS('background-color', 'rgb(238, 182, 83)')
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'dark')
  await expect.poll(() => page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }))).toEqual({
    viewport: page.viewportSize()!.width,
    document: page.viewportSize()!.width,
    body: page.viewportSize()!.width,
  })
}

test('real pairing, correction, deliberate send, reply, recovery and revocation', async ({ page: operator, browser }, testInfo) => {
  const wearerContext = await browser.newContext({ baseURL: 'http://127.0.0.1:8765', viewport: { width: 390, height: 844 } })
  const wearer = await wearerContext.newPage()
  const submissions: string[] = []
  const replies: string[] = []
  wearer.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/messages') submissions.push(request.postData()!)
  })
  operator.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/reply')) replies.push(request.postData()!)
  })
  try {
    await pair(operator, wearer, 'E2E wearer')
    const saved = await wearer.evaluate(() => JSON.parse(localStorage.getItem(`evencomms.wearer:${location.origin}`)!))
    const token = await operator.evaluate(() => sessionStorage.getItem('evencomms.operator.token'))
    const history = async () => {
      const response = await operator.request.get(`/api/sessions/${saved.session_id}/messages`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      expect(response.status()).toBe(200)
      return response.json()
    }

    await test.step('a tap corrects privately and a quick click never submits', async () => {
      await wearer.getByLabel('Unsent text').fill(question + ' WRONG')
      await wearer.getByRole('button', { name: 'Tap / Delete word', exact: true }).click()
      await expect(wearer.getByLabel('Unsent text')).toHaveValue(question)
      await wearer.bringToFront()
      await wearer.getByRole('button', { name: 'Hold to send', exact: true }).click()
      // Observe beyond the arming threshold and one complete operator poll.
      await wearer.waitForTimeout(2300)
      await expect(wearer.getByLabel('Unsent text')).toHaveValue(question)
      expect(submissions).toHaveLength(0)
      expect(await history()).toEqual([])
      await expect(operator.getByRole('heading', { name: 'The line is open.' })).toBeVisible()

      // Losing focus before the arming threshold must cancel the timer, not send later.
      const hold = wearer.getByRole('button', { name: 'Hold to send', exact: true })
      await hold.scrollIntoViewIfNeeded()
      const box = (await hold.boundingBox())!
      await wearer.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await wearer.mouse.down()
      await wearer.waitForTimeout(100)
      await wearer.evaluate(() => window.dispatchEvent(new Event('blur')))
      await wearer.waitForTimeout(750)
      await wearer.mouse.up()
      await expect(hold).toBeVisible()
      expect(submissions).toHaveLength(0)
    })

    await test.step('continuous hold and release submits exactly once through the real backend', async () => {
      await holdToSend(wearer)
      const log = operator.getByRole('log', { name: 'Submitted conversation messages' })
      await expect(log.getByText(question, { exact: true })).toHaveCount(1)
      await expect(log.locator('li')).toHaveCount(1)
      expect(submissions).toHaveLength(1)
      expect(await history()).toMatchObject([{ role: 'wearer', text: question }])
    })

    await test.step('AI is a browser-only suggestion and does not auto-send', async () => {
      let suggestions = 0
      await operator.route('**/api/sessions/*/suggest', async route => {
        suggestions++
        expect(route.request().method()).toBe('POST')
        await route.fulfill({ json: { text: 'Suggestion only: review before sending.' } })
      })
      await operator.getByRole('button', { name: 'AI suggestion', exact: true }).click()
      await expect(operator.getByLabel('Your reply', { exact: true })).toHaveValue('Suggestion only: review before sending.')
      await operator.waitForTimeout(2300)
      expect(suggestions).toBe(1)
      expect(replies).toHaveLength(0)
      expect(await history()).toHaveLength(1)
      await expect(wearer.getByLabel('Glasses text preview')).not.toContainText('Suggestion only')
    })

    await test.step('operator reply reaches the glasses preview and paginates', async () => {
      await operator.getByLabel('Your reply', { exact: true }).fill(reply)
      await operator.getByRole('button', { name: 'Send reply', exact: true }).click()
      await expect(wearer.locator('.phone-reply')).toContainText(reply)
      const operatorPreview = operator.getByLabel('Last operator reply glasses preview')
      await expect(operatorPreview).toContainText('EVENCOMMS · REPLY')
      await expect(operatorPreview).toContainText('Meet at the north entrance.')
      await expect(operatorPreview).toHaveCSS('color', 'rgb(131, 223, 163)')
      const screen = operatorPreview.locator('.hud-screen')
      const box = (await screen.boundingBox())!
      expect(Math.abs(box.width / box.height - 2)).toBeLessThan(0.02)
      const lastPage = operator.getByRole('button', { name: 'Next reply preview page' })
      while (await lastPage.isEnabled()) await lastPage.click()
      await expect(operatorPreview).toContainText('FINAL PAGE:')
      await expect(lastPage).toBeDisabled()
      await operator.getByRole('button', { name: 'Previous reply preview page' }).click()
      const preview = wearer.getByLabel('Glasses text preview')
      await expect(preview).toContainText(/REPLY 1\/[2-9]/)
      await expect(preview).toContainText('Meet at the north entrance.')
      const first = await preview.innerText()
      const count = Number(first.match(/REPLY 1\/(\d+)/)![1])
      for (let i = 1; i < count; i++) await wearer.getByRole('button', { name: 'Next page' }).click()
      await expect(preview).toContainText(`REPLY ${count}/${count}`)
      await expect(preview).toContainText('FINAL PAGE:')
      await wearer.getByRole('button', { name: 'Next page' }).click()
      await expect(preview).toContainText(`REPLY ${count}/${count}`)
      for (let i = 1; i < count; i++) await wearer.getByRole('button', { name: 'Previous page' }).click()
      await expect(preview).toHaveText(first)
      expect(replies).toHaveLength(1)
    })

    await test.step('reload reconnects and preserves the private draft and server reply', async () => {
      await wearer.getByLabel('Unsent text').fill('Private follow-up not yet sent')
      await wearer.reload()
      await expect(wearer.getByText('STONE CONNECTED', { exact: true })).toBeVisible()
      await expect(wearer.getByLabel('Unsent text')).toHaveValue('Private follow-up not yet sent')
      await expect(wearer.locator('.phone-reply')).toContainText(reply)
      await expect(wearer.getByLabel('Glasses text preview')).toContainText('Meet at the north entrance.')
      await operator.reload()
      await expect(operator.getByRole('log').locator('li')).toHaveCount(2)
      expect(await history()).toHaveLength(2)
      expect(submissions).toHaveLength(1)
      await operator.screenshot({ path: testInfo.outputPath('operator-conversation.png'), fullPage: true })
      await wearer.screenshot({ path: testInfo.outputPath('wearer-reconnected.png'), fullPage: true })
    })

    await test.step('authenticated deletion closes the socket and revokes the token', async () => {
      const unauthorized = await wearer.request.delete(`/api/sessions/${saved.session_id}`)
      expect(unauthorized.status()).toBe(401)
      await expect(wearer.getByText('STONE CONNECTED', { exact: true })).toBeVisible()
      operator.once('dialog', dialog => dialog.accept())
      const deletion = operator.waitForResponse(response => response.request().method() === 'DELETE' && response.url().endsWith(saved.session_id))
      await operator.getByRole('button', { name: 'Delete conversation with E2E wearer and revoke access' }).click()
      expect((await deletion).status()).toBe(204)
      await expect(wearer.getByRole('alert')).toContainText('Pairing revoked')
      await expect(wearer.getByText('OFFLINE', { exact: true })).toBeVisible()
      await expect(wearer.getByRole('button', { name: 'Hold to send', exact: true })).toBeDisabled()
      const me = await wearer.request.get('/api/me', { headers: { Authorization: `Bearer ${saved.token}` } })
      expect(me.status()).toBe(401)
      await wearer.reload()
      await expect(wearer.getByRole('alert')).toContainText('Pairing revoked')
      await expect(wearer.getByLabel('Unsent text')).toHaveValue('Private follow-up not yet sent')
      await expect(operator.getByRole('heading', { name: 'No wearers paired' })).toBeVisible()
    })
  } finally {
    await wearerContext.close()
  }
})

for (const width of [320, 390, 1440]) {
  test(`both pages have no horizontal overflow at ${width}px, before and after pairing`, async ({ page: operator, browser }, testInfo) => {
    await operator.setViewportSize({ width, height: 900 })
    const context = await browser.newContext({ baseURL: 'http://127.0.0.1:8765', viewport: { width, height: 900 } })
    const wearer = await context.newPage()
    try {
      await operator.goto('/')
      await expect(operator.getByLabel('Operator password')).toBeVisible()
      await noOverflow(operator)
      await wearer.goto('/glasses.html?simulate=1')
      await expect(wearer.getByLabel('Pairing code', { exact: true })).toBeVisible()
      await noOverflow(wearer)
      await pair(operator, wearer, `Responsive ${width}`)
      await wearer.getByLabel('Unsent text').fill('Responsive wearer message')
      await holdToSend(wearer)
      await expect(operator.getByRole('log').getByText('Responsive wearer message', { exact: true })).toBeVisible()
      await operator.getByLabel('Your reply', { exact: true }).fill(reply)
      await operator.getByRole('button', { name: 'Send reply', exact: true }).click()
      await expect(wearer.locator('.phone-reply')).toContainText(reply)
      await noOverflow(operator)
      await noOverflow(wearer)
      await operator.screenshot({ path: testInfo.outputPath(`operator-${width}.png`), fullPage: true })
      await wearer.screenshot({ path: testInfo.outputPath(`wearer-${width}.png`), fullPage: true })
      operator.once('dialog', dialog => dialog.accept())
      await operator.getByRole('button', { name: `Delete conversation with Responsive ${width} and revoke access` }).click()
      await expect(wearer.getByRole('alert')).toContainText('Pairing revoked')
    } finally {
      await context.close()
    }
  })
}
