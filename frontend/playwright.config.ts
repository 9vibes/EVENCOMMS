import { defineConfig } from '@playwright/test'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const python = process.env.EVENCOMMS_PYTHON || 'python3'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  outputDir: process.env.EVENCOMMS_E2E_OUTPUT || join(tmpdir(), 'evencomms-playwright'),
  use: {
    baseURL: 'http://127.0.0.1:8765',
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.CHROMIUM_PATH
      ? { executablePath: process.env.CHROMIUM_PATH }
      : {},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `'${python.replaceAll("'", "'\\''")}' -m backend.e2e_server`,
    cwd: root,
    url: 'http://127.0.0.1:8765/health',
    reuseExistingServer: false,
    timeout: 30_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
    env: { ADMIN_PASSWORD: 'evencomms-e2e-only-password', STT_ENABLED: 'false' },
  },
})
