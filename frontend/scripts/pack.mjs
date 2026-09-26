import { readFile, writeFile, mkdtemp, cp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const origin = new URL(process.env.EVENCOMMS_ORIGIN || 'invalid:')
if (!['https:', 'http:'].includes(origin.protocol) || origin.username || origin.password ||
    origin.pathname !== '/' || origin.search || origin.hash) {
  throw new Error('Set EVENCOMMS_ORIGIN to the full Stone origin, e.g. https://stone.example.net:28097')
}
if (origin.protocol !== 'https:' && process.env.ALLOW_HTTP_DEV !== '1') {
  throw new Error('Packaged apps require HTTPS. For LAN development only, set ALLOW_HTTP_DEV=1.')
}
const manifest = JSON.parse(await readFile('app.json', 'utf8'))
manifest.permissions.push({ name: 'network', desc: 'Connect to your local Kunas Stone.',
  whitelist: [origin.origin, origin.origin.replace(/^http/, 'ws')] })
const staging = await mkdtemp(join(tmpdir(), 'evencomms-pack-'))
let status = 1
try {
  await cp('dist', staging, { recursive: true })
  await writeFile(join(staging, 'app.json'), JSON.stringify(manifest, null, 2))
  await writeFile(join(staging, 'stone.json'), JSON.stringify({ origin: origin.origin }))
  const result = spawnSync('evenhub', ['pack', join(staging, 'app.json'), staging,
    '--sdk-ver', '0.0.16', '-o', 'evencomms.ehpk'], { stdio: 'inherit', shell: false })
  status = result.status ?? 1
} finally { await rm(staging, { recursive: true, force: true }) }
process.exit(status)
