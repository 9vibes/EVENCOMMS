import { constants } from 'node:fs'
import { readFile, writeFile, mkdtemp, copyFile, lstat, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { parseArgs } from 'node:util'
import { build } from 'vite'

const root = resolve(import.meta.dirname, '..')

// The caller owns the returned directory. Failed preparation cleans up its own staging files.
export async function preparePackage({ origin = process.env.EVENCOMMS_ORIGIN, version } = {}) {
  const url = typeof origin === 'string' ? URL.parse(origin) : null
  if (!url || origin.trim() !== origin || !/^https?:\/\/[^/?#\s\\*@]+\/?$/.test(origin) || url.hostname.includes('*') ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Set EVENCOMMS_ORIGIN to an exact HTTP(S) Stone origin without a path, credentials, query or wildcard.')
  }
  if (url.protocol !== 'https:' && process.env.ALLOW_HTTP_DEV !== '1') {
    throw new Error('Packaged apps require HTTPS. For LAN development only, set ALLOW_HTTP_DEV=1.')
  }

  const manifest = JSON.parse(await readFile(join(root, 'app.json'), 'utf8'))
  for (const value of version === undefined ? [manifest.version] : [manifest.version, version]) {
    if (typeof value !== 'string' || value.trim() !== value || !/^\d+\.\d+\.\d+$/.test(value)) {
      throw new Error('Manifest version and --package-version must use X.Y.Z (digits only).')
    }
  }
  const packageJson = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
  const sdkVersion = packageJson.dependencies['@evenrealities/even_hub_sdk']
  if (sdkVersion !== '0.0.16' || manifest.min_sdk_version !== sdkVersion) {
    throw new Error('The package SDK pin and manifest min_sdk_version must both be 0.0.16.')
  }
  manifest.version = version ?? manifest.version
  manifest.entrypoint = 'index.html'
  const network = manifest.permissions.filter(permission => permission.name === 'network')
  manifest.permissions = [
    ...manifest.permissions.filter(permission => permission.name !== 'network'),
    { name: 'network', desc: 'Connect to your local Kunas Stone.', ...network[0],
      whitelist: [...new Set([...network.flatMap(permission => permission.whitelist ?? []),
        url.origin, url.origin.replace(/^http/, 'ws')])] },
  ]

  const directory = await mkdtemp(join(tmpdir(), 'evencomms-pack-'))
  try {
    await build({
      root,
      configFile: join(root, 'vite.config.ts'),
      // This public address must work even when a file/opaque WebView cannot fetch local JSON.
      define: { __EVENCOMMS_PACKAGED_ORIGIN__: JSON.stringify(url.origin) },
      build: {
        outDir: directory,
        emptyOutDir: true,
        copyPublicDir: false,
        // A string replaces the web config's input object instead of merging the operator entry back in.
        rollupOptions: { input: join(root, 'glasses.html') },
      },
    })
    await copyFile(join(directory, 'glasses.html'), join(directory, 'index.html'), constants.COPYFILE_EXCL)
    await writeFile(join(directory, 'app.json'), JSON.stringify(manifest, null, 2))
    await writeFile(join(directory, 'stone.json'), JSON.stringify({ origin: url.origin }))
    return { directory, manifest }
  } catch (error) {
    await rm(directory, { recursive: true, force: true })
    throw error
  }
}

async function main() {
  const { values } = parseArgs({ options: {
    output: { type: 'string', default: join(root, 'evencomms.ehpk') },
    'package-version': { type: 'string' },
  } })
  if (!values.output) throw new Error('--output requires a file path.')
  const output = resolve(values.output)
  try {
    await lstat(output)
    throw new Error(`Refusing to overwrite existing output: ${output}`)
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }

  const temporary = await mkdtemp(join(tmpdir(), 'evencomms-archive-'))
  let prepared
  try {
    prepared = await preparePackage({ version: values['package-version'] })
    const archive = join(temporary, 'evencomms.ehpk')
    const result = spawnSync(process.execPath, [join(root, 'node_modules/@evenrealities/evenhub-cli/main.js'),
      'pack', join(prepared.directory, 'app.json'), prepared.directory,
      '--sdk-ver', prepared.manifest.min_sdk_version, '-o', archive],
    { cwd: root, stdio: 'inherit', shell: false })
    if (result.error) throw result.error
    if (result.status !== 0) throw new Error(`EvenHub pack failed (${result.signal ?? result.status}).`)
    // Pack outside the payload, then publish exclusively so even a late collision cannot overwrite data.
    await copyFile(archive, output, constants.COPYFILE_EXCL)
    console.log(`Created ${output}`)
  } finally {
    await Promise.all([
      rm(temporary, { recursive: true, force: true }),
      prepared && rm(prepared.directory, { recursive: true, force: true }),
    ])
  }
}

if (import.meta.main) {
  try { await main() }
  catch (error) { console.error(error.message); process.exitCode = 1 }
}
