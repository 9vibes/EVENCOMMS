import { test } from 'node:test'
import assert from 'node:assert/strict'
import { copyFile, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import { preparePackage } from './pack.mjs'

const frontend = resolve(import.meta.dirname, '..')
const origin = 'https://ec.kunas.pro'
const readJson = async path => JSON.parse(await readFile(path, 'utf8'))
const sourceVersion = (await readJson(join(frontend, 'app.json'))).version
const remove = directory => rm(directory, { recursive: true, force: true })

async function fixture(t, realSources = false) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'evencomms-package-test-')))
  t.after(() => remove(root))
  for (const path of ['scripts', 'node_modules/@evenrealities/evenhub-cli', 'tmp', 'public', 'dist']) {
    await mkdir(join(root, path), { recursive: true })
  }
  for (const path of ['scripts/pack.mjs', 'app.json', 'package.json']) {
    await copyFile(join(frontend, path), join(root, path))
  }
  await symlink(join(frontend, 'node_modules/vite'), join(root, 'node_modules/vite'), 'dir')
  if (realSources) {
    await copyFile(join(frontend, 'glasses.html'), join(root, 'glasses.html'))
    await symlink(join(frontend, 'src'), join(root, 'src'), 'dir')
  } else {
    await writeFile(join(root, 'glasses.html'), '<title>EVENCOMMS / Wearer</title><script type="module" src="./wearer.js"></script>')
    await writeFile(join(root, 'wearer.js'), 'document.body.dataset.wearer = "ready"')
  }
  await writeFile(join(root, 'dist/index.html'), 'old operator web distribution')
  await writeFile(join(root, 'public/operator-only.html'), 'public files are not dependency-graph assets')
  await writeFile(join(root, 'vite.config.ts'), `
    import base from ${JSON.stringify(join(frontend, 'vite.config.ts'))}
    import { writeFileSync } from 'node:fs'
    import { join } from 'node:path'
    export default { ...base, plugins: [{
      name: 'package-test-graph',
      generateBundle(_, bundle) {
        this.emitFile({ type: 'asset', fileName: 'graph.json', source: JSON.stringify(
          Object.values(bundle).filter(item => item.type === 'chunk').map(chunk => ({
            fileName: chunk.fileName, entry: chunk.isEntry, facade: chunk.facadeModuleId,
            modules: Object.keys(chunk.modules),
          }))
        ) })
      },
      writeBundle({ dir }) {
        if (process.env.PACK_TEST_BUILD_FAIL === '1') {
          writeFileSync(join(dir, 'partial.txt'), 'partial build')
          throw new Error('intentional partial build failure')
        }
      },
    }] }
  `)
  // This fixture records the official writer's inputs; it is NOT an EHPK encoder or decoder.
  await writeFile(join(root, 'node_modules/@evenrealities/evenhub-cli/package.json'), '{"type":"module"}')
  await writeFile(join(root, 'node_modules/@evenrealities/evenhub-cli/main.js'), `
    import { readFile, readdir, writeFile } from 'node:fs/promises'
    import { join } from 'node:path'
    const args = process.argv.slice(2)
    if (args[0] !== 'pack' || args[3] !== '--sdk-ver' || args[5] !== '-o' || args.length !== 7) {
      throw new Error('unexpected writer arguments')
    }
    const directory = args[2]
    await writeFile(args[6], JSON.stringify({
      fixtureOnly: true, cli: process.argv[1], execPath: process.execPath, args,
      manifest: JSON.parse(await readFile(args[1], 'utf8')),
      stone: JSON.parse(await readFile(join(directory, 'stone.json'), 'utf8')),
      index: await readFile(join(directory, 'index.html'), 'utf8'),
      glasses: await readFile(join(directory, 'glasses.html'), 'utf8'),
      files: await readdir(directory, { recursive: true }),
    }))
    if (process.env.PACK_TEST_RACE) await writeFile(process.env.PACK_TEST_RACE, 'concurrent output')
    if (process.env.PACK_TEST_PUBLISH_FAIL === '1') process.exit(7)
  `)
  return root
}

function run(root, args = [], environment = {}) {
  return spawnSync(process.execPath, [join(root, 'scripts/pack.mjs'), ...args], {
    cwd: tmpdir(), encoding: 'utf8',
    env: { ...process.env, PATH: '', TMPDIR: join(root, 'tmp'), EVENCOMMS_ORIGIN: origin,
      ALLOW_HTTP_DEV: '', ...environment },
  })
}

test('real package root and legacy entry are wearer-only; public web and source manifests stay unchanged', async t => {
  const unchanged = ['app.json', 'package.json', 'index.html', 'vite.config.ts', 'dist/index.html']
  const before = await Promise.all(unchanged.map(path => readFile(join(frontend, path))))
  assert.match(before[4].toString(), /<title>EVENCOMMS \/ Operator<\/title>/)
  const { directory, manifest } = await preparePackage({ origin, version: '0.4.3' })
  t.after(() => remove(directory))
  const index = await readFile(join(directory, 'index.html'), 'utf8')
  assert.match(index, /<title>EVENCOMMS \/ Wearer<\/title>/)
  assert.equal(index, await readFile(join(directory, 'glasses.html'), 'utf8'))
  const files = await readdir(directory, { recursive: true })
  assert.deepEqual(files.filter(path => path.endsWith('.html')).sort(), ['glasses.html', 'index.html'])
  assert.ok(!files.some(path => /(?:^|\/)(?:operator|hls)[.-]/.test(path)))
  for (const [, path] of index.matchAll(/(?:src|href)="(\.\/[^\"]+)"/g)) {
    assert.ok((await lstat(join(directory, path))).isFile(), path)
  }
  const source = JSON.parse(before[0])
  assert.deepEqual(manifest, { ...source, entrypoint: 'index.html', version: '0.4.3', permissions: [
    ...source.permissions,
    { name: 'network', desc: 'Connect to your local Kunas Stone.', whitelist: [origin, 'wss://ec.kunas.pro'] },
  ] })
  assert.equal(source.version, sourceVersion)
  assert.equal(manifest.min_sdk_version, '0.0.16')
  assert.equal(manifest.min_app_version, '2.2.10')
  assert.deepEqual(await readJson(join(directory, 'app.json')), manifest)
  assert.deepEqual(await readJson(join(directory, 'stone.json')), { origin })
  assert.deepEqual(await Promise.all(unchanged.map(path => readFile(join(frontend, path)))), before)
})

test('single-entry graph excludes operator UI and HLS, allows shared API, and merges existing network permissions', async t => {
  const root = await fixture(t, true)
  const source = await readJson(join(root, 'app.json'))
  source.permissions.push(
    { name: 'network', desc: 'Existing network access', whitelist: [origin, 'https://other.example'] },
    { name: 'network', desc: 'Duplicate permission', whitelist: [origin, 'wss://ec.kunas.pro'] },
  )
  await writeFile(join(root, 'app.json'), JSON.stringify(source))
  const { preparePackage: prepare } = await import(pathToFileURL(join(root, 'scripts/pack.mjs')))
  const { directory, manifest } = await prepare({ origin: origin + '/' })
  t.after(() => remove(directory))
  const graph = await readJson(join(directory, 'graph.json'))
  assert.deepEqual(graph.filter(chunk => chunk.entry).map(chunk => chunk.facade), [await realpath(join(root, 'glasses.html'))])
  const modules = graph.flatMap(chunk => chunk.modules)
  assert.ok(modules.some(path => path.endsWith('/src/glasses/main.tsx')))
  assert.ok(modules.some(path => path.endsWith('/src/operator/api.ts')), 'wearer legitimately shares API helpers')
  assert.ok(!modules.some(path => /\/src\/operator\/(?!api\.ts(?:\?|$))|\/node_modules\/hls\.js\//.test(path)))
  const files = await readdir(directory, { recursive: true })
  assert.deepEqual(files.filter(path => path.endsWith('.html')).sort(), ['glasses.html', 'index.html'])
  assert.ok(!files.some(path => /(?:^|\/)(?:operator|hls)[.-]/.test(path)))
  assert.deepEqual(manifest.permissions, [source.permissions[0], {
    name: 'network', desc: 'Existing network access',
    whitelist: [origin, 'https://other.example', 'wss://ec.kunas.pro'],
  }])
  assert.equal(manifest.version, sourceVersion)
  assert.deepEqual(await readJson(join(directory, 'stone.json')), { origin })
  assert.deepEqual(await readJson(join(root, 'app.json')), source)
  assert.equal(await readFile(join(root, 'dist/index.html'), 'utf8'), 'old operator web distribution')
})

test('rejects malformed origins rather than normalizing paths, credentials, queries or wildcards away', async () => {
  for (const invalid of [null, '', 'ec.kunas.pro', 'https:ec.kunas.pro', 'ftp://ec.kunas.pro',
    'https://ec.kunas.pro/path', 'https://ec.kunas.pro/./', 'https://ec.kunas.pro?', 'https://ec.kunas.pro#',
    'https://user:pass@ec.kunas.pro', 'https://@ec.kunas.pro', 'https://*.kunas.pro', 'https://%2a.kunas.pro',
    'https://ec.kunas.pro\\', ' https://ec.kunas.pro', 'https://ec.kunas.pro\n', 'https://ec.kunas.pro:invalid']) {
    await assert.rejects(preparePackage({ origin: invalid }), /exact HTTP\(S\) Stone origin/, String(invalid))
  }
})

test('package version override accepts only X.Y.Z strings', async () => {
  for (const version of ['', null, 43, '0.4', 'v0.4.3', '0.4.3?cache=1', '0.4.3-beta', '0.4.3+private', '0.4.3\n']) {
    await assert.rejects(preparePackage({ origin, version }), /X\.Y\.Z/, String(version))
  }
})

test('validates source version even with an override, and checks the exact SDK pin', async t => {
  const root = await fixture(t)
  const { preparePackage: prepare } = await import(pathToFileURL(join(root, 'scripts/pack.mjs')))
  const manifest = await readJson(join(root, 'app.json'))
  await writeFile(join(root, 'app.json'), JSON.stringify({ ...manifest, version: 'invalid' }))
  await assert.rejects(prepare({ origin, version: '0.4.3' }), /X\.Y\.Z/)
  await writeFile(join(root, 'app.json'), JSON.stringify({ ...manifest, min_sdk_version: '0.0.15' }))
  await assert.rejects(prepare({ origin }), /SDK pin/)
  await writeFile(join(root, 'app.json'), JSON.stringify(manifest))
  const packageJson = await readJson(join(root, 'package.json'))
  packageJson.dependencies['@evenrealities/even_hub_sdk'] = '^0.0.16'
  await writeFile(join(root, 'package.json'), JSON.stringify(packageJson))
  await assert.rejects(prepare({ origin }), /SDK pin/)
})

test('HTTP requires ALLOW_HTTP_DEV=1 and uses matching HTTP/WS config and default version/output', async t => {
  const root = await fixture(t)
  const environment = { EVENCOMMS_ORIGIN: 'http://127.0.0.1:28097' }
  for (const flag of ['', 'true']) {
    const result = run(root, [], { ...environment, ALLOW_HTTP_DEV: flag })
    assert.equal(result.status, 1, result.stderr)
    assert.match(result.stderr, /require HTTPS/)
    assert.deepEqual(await readdir(join(root, 'tmp')), [])
  }
  const result = run(root, [], { ...environment, ALLOW_HTTP_DEV: '1' })
  assert.equal(result.status, 0, result.stderr)
  const payload = await readJson(join(root, 'evencomms.ehpk'))
  assert.deepEqual(payload.stone, { origin: environment.EVENCOMMS_ORIGIN })
  assert.deepEqual(payload.manifest.permissions.at(-1).whitelist, ['http://127.0.0.1:28097', 'ws://127.0.0.1:28097'])
  assert.equal(payload.manifest.version, sourceVersion)
  assert.deepEqual(await readdir(join(root, 'tmp')), [])
})

test('CLI invokes the local writer with this Node, staged inputs, SDK pin and private version; cleans up on success', async t => {
  const root = await fixture(t)
  const output = join(root, 'private client 0.4.3.ehpk')
  const result = run(root, ['--output', output, '--package-version', '0.4.3'])
  assert.equal(result.status, 0, result.stderr)
  const payload = await readJson(output)
  assert.equal(payload.fixtureOnly, true)
  assert.equal(payload.cli, join(root, 'node_modules/@evenrealities/evenhub-cli/main.js'))
  assert.equal(payload.execPath, process.execPath)
  assert.deepEqual(payload.args.slice(0, 6), ['pack', join(payload.args[2], 'app.json'), payload.args[2], '--sdk-ver', '0.0.16', '-o'])
  assert.ok(!payload.args[6].startsWith(payload.args[2] + '/'), 'archive stays outside the packaged payload')
  assert.equal(payload.manifest.version, '0.4.3')
  assert.equal(payload.manifest.entrypoint, 'index.html')
  assert.equal(payload.index, payload.glasses)
  assert.deepEqual(payload.stone, { origin })
  assert.equal((await readJson(join(root, 'app.json'))).version, sourceVersion)
  assert.deepEqual(await readdir(join(root, 'tmp')), [])
  await assert.rejects(lstat(payload.args[2]), { code: 'ENOENT' })
})

test('refuses existing default/explicit outputs, including dangling symlinks, without replacing anything', async t => {
  const root = await fixture(t)
  const output = join(root, 'evencomms.ehpk')
  const dangling = join(root, 'dangling.ehpk')
  await writeFile(output, 'existing private 0.4.2 artifact')
  await symlink(join(root, 'missing-target'), dangling)
  for (const args of [[], ['--output', output], ['--output', dangling]]) {
    const result = run(root, args)
    assert.equal(result.status, 1, result.stderr)
    assert.match(result.stderr, /Refusing to overwrite existing output/)
    assert.deepEqual(await readdir(join(root, 'tmp')), [])
  }
  assert.equal(await readFile(output, 'utf8'), 'existing private 0.4.2 artifact')
  assert.ok((await lstat(dangling)).isSymbolicLink())
})

test('rejects missing/unknown CLI arguments before staging', async t => {
  const root = await fixture(t)
  for (const args of [['--output'], ['--package-version'], ['--unknown'], ['--output', '']]) {
    const result = run(root, args)
    assert.equal(result.status, 1, result.stderr)
    assert.deepEqual(await readdir(join(root, 'tmp')), [])
  }
})

test('cleans partially written build and archive files after failures; does not publish an output', async t => {
  const root = await fixture(t)
  for (const [flag, message] of [
    ['PACK_TEST_BUILD_FAIL', /intentional partial build failure/],
    ['PACK_TEST_PUBLISH_FAIL', /EvenHub pack failed \(7\)/],
  ]) {
    const result = run(root, [], { [flag]: '1' })
    assert.equal(result.status, 1, result.stderr)
    assert.match(result.stderr, message)
    assert.deepEqual(await readdir(join(root, 'tmp')), [])
    await assert.rejects(lstat(join(root, 'evencomms.ehpk')), { code: 'ENOENT' })
  }
})

test('exclusive publication preserves an output created after the initial collision check', async t => {
  const root = await fixture(t)
  const output = join(root, 'evencomms.ehpk')
  const result = run(root, [], { PACK_TEST_RACE: output })
  assert.equal(result.status, 1, result.stderr)
  assert.match(result.stderr, /EEXIST/)
  assert.equal(await readFile(output, 'utf8'), 'concurrent output')
  assert.deepEqual(await readdir(join(root, 'tmp')), [])
})
