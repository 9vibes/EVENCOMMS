import { test } from 'node:test'
import assert from 'node:assert/strict'
import { checkConnection, ConnectionError, connectionDetails, pairingCredentials, pairWithPrecheck, validateOrigin } from './connection.ts'

const server = 'https://stone.example.test'
const credentials = { token: 'unit-test-token', session_id: '4aa647f6-e60c-4ec3-a42e-90407e4ac052' }
const input = { code: 'TESTCODE', name: 'Test wearer' }
const secret = 'must-not-appear-in-diagnostics'

test('target validation canonicalizes exact HTTP(S) origins and retains mixed-content protection', () => {
  assert.equal(validateOrigin(' HTTPS://STONE.example.test:443/ ', 'https:'), server)
  assert.equal(validateOrigin('http://umbrel.local:28097', 'file:'), 'http://umbrel.local:28097')
  assert.throws(() => validateOrigin('http://umbrel.local:28097', 'https:'), /HTTPS app cannot connect to an HTTP Stone/)
})

test('diagnostics contain only the two reported origins, scheme, validated target and pair endpoint', () => {
  const page = { origin: 'https://app.example.test', protocol: 'https:',
    get href() { assert.fail('Full page URL must not be read') },
    get pathname() { assert.fail('Page path must not be read') },
    get search() { assert.fail('Page query must not be read') },
    name: secret, token: secret, draft: secret }
  assert.deepEqual(connectionDetails(page, page.origin, server + '/'), {
    pageOrigin: page.origin, browserOrigin: page.origin, protocol: 'https:', stoneOrigin: server, pairEndpoint: server + '/api/pair',
  })
  assert.ok(!JSON.stringify(connectionDetails(page, page.origin, server)).includes(secret))
})

test('an opaque iframe reports window.origin null separately from its HTTP page URL origin', () => {
  const details = connectionDetails({ origin: 'http://app.example.test:28097', protocol: 'http:' }, 'null', server)
  assert.equal(details.pageOrigin, 'http://app.example.test:28097')
  assert.equal(details.browserOrigin, 'null (opaque origin)')
  assert.equal(details.stoneOrigin, server)
})

test('file and unreported browser origins are never replaced with a guessed origin', () => {
  const page = { origin: 'null', protocol: 'file:' }
  const details = connectionDetails(page, 'null', server)
  assert.equal(details.pageOrigin, 'null (opaque origin)')
  assert.equal(details.browserOrigin, 'null (opaque origin)')
  assert.equal(details.protocol, 'file:')
  assert.equal(details.pairEndpoint, server + '/api/pair')
  assert.equal(connectionDetails(page, undefined, server).browserOrigin, 'Unavailable (not reported)')
})

test('invalid targets produce fixed labels and validation errors, never raw input', () => {
  for (const target of ['', 'null', 'file:///private/' + secret, 'https://' + secret + '@stone.example.test',
    server + '/' + secret, server + '?token=' + secret, server + '#' + secret, 'https://*.example.test', 'https://%2A.example.test',
    'https:stone.example.test', 'https://stone.example.test\\', server + '?', server + '#']) {
    const details = connectionDetails({ origin: 'null', protocol: 'file:' }, 'null', target)
    assert.equal(details.stoneOrigin, 'Invalid or missing Stone address')
    assert.equal(details.pairEndpoint, 'Unavailable (invalid Stone address)')
    assert.ok(!JSON.stringify(details).includes(secret))
    assert.throws(() => validateOrigin(target, 'file:'), error => {
      assert.ok(error instanceof ConnectionError)
      assert.match(error.message, /^Target validation failed\./)
      assert.ok(!error.message.includes(secret))
      return true
    })
  }
})

test('pairing credentials are bounded, UUID-validated and extracted without response extras', () => {
  assert.deepEqual(pairingCredentials({ ...credentials, name: secret, code: secret, draft: secret }), credentials)
  assert.equal(pairingCredentials({ ...credentials, token: 'x'.repeat(256) }).token.length, 256)
  for (const value of [null, [], {}, { ...credentials, token: '' }, { ...credentials, token: 'x'.repeat(257) },
    { ...credentials, token: 7 }, { ...credentials, session_id: 'not-a-uuid' }, { ...credentials, session_id: 1 }]) {
    assert.equal(pairingCredentials(value), null)
  }
})

test('standalone health check sends no credentials, body or referrer and clears its timeout', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const calls = []
  assert.equal(await checkConnection(server, 'https:', async (url, options) => {
    calls.push({ url, options })
    return Response.json({ status: 'ok', detail: secret })
  }), server)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, server + '/health')
  const { signal, ...options } = calls[0].options
  assert.deepEqual(options, { method: 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store',
    referrerPolicy: 'no-referrer', headers: { 'Content-Type': 'application/json' } })
  assert.ok(signal instanceof AbortSignal)
  t.mock.timers.tick(8000)
  assert.equal(signal.aborted, false)
})

test('pairing waits for the health JSON before POST and sends only code and name', async () => {
  const calls = []
  let releaseHealth
  const health = new Promise(resolve => { releaseHealth = resolve })
  const result = pairWithPrecheck(server, 'https:', { ...input, token: secret }, async (url, options) => {
    calls.push({ url, options })
    return options.method === 'GET' ? { ok: true, json: () => health }
      : Response.json({ ...credentials, detail: secret, name: secret, draft: secret })
  })
  assert.deepEqual(calls.map(call => call.url), [server + '/health'])
  await Promise.resolve()
  assert.equal(calls.length, 1)
  releaseHealth({ status: 'ok' })
  assert.deepEqual(await result, { origin: server, ...credentials })
  assert.deepEqual(calls.map(call => call.url), [server + '/health', server + '/api/pair'])
  assert.equal(calls[0].options.body, undefined)
  assert.deepEqual(JSON.parse(calls[1].options.body), input)
  assert.equal(calls[1].options.credentials, 'omit')
  assert.equal(calls[1].options.mode, 'cors')
  assert.deepEqual(calls[1].options.headers, { 'Content-Type': 'application/json' })
})

test('target validation stops both check and pairing before any request', async () => {
  const fetcher = () => assert.fail('Invalid target must not be fetched')
  await assert.rejects(checkConnection('null', 'file:', fetcher), /Target validation failed/)
  await assert.rejects(pairWithPrecheck(server + '?token=' + secret, 'https:', input, fetcher), /Target validation failed/)
})

test('failed precheck leaves the synthetic one-use code unused until an explicit successful retry', async () => {
  let reachable = false
  let consumed = false
  const methods = []
  const fetcher = async (_url, options) => {
    methods.push(options.method)
    if (options.method === 'GET') {
      if (!reachable) throw new TypeError(secret)
      return Response.json({ status: 'ok' })
    }
    assert.equal(consumed, false)
    assert.deepEqual(JSON.parse(options.body), input)
    consumed = true
    return Response.json(credentials)
  }
  await assert.rejects(pairWithPrecheck(server, 'https:', input, fetcher), error => {
    assert.match(error.message, /Connection check failed.*Possible causes include network, TLS, WebView permissions or CORS/)
    assert.match(error.message, /No pairing code was sent/)
    assert.ok(!error.message.includes(secret))
    return true
  })
  assert.deepEqual(methods, ['GET'])
  assert.equal(consumed, false)
  reachable = true
  await pairWithPrecheck(server, 'https:', input, fetcher)
  assert.deepEqual(methods, ['GET', 'GET', 'POST'])
  assert.equal(consumed, true)
})

test('unhealthy, non-JSON and wrong-shape health responses stop before POST without exposing bodies', async () => {
  for (const response of [new Response(secret, { status: 503 }), new Response(secret),
    Response.json({ status: secret }), Response.json({ ok: true }), Response.json(null)]) {
    let requests = 0
    await assert.rejects(pairWithPrecheck(server, 'https:', input, async (_url, options) => {
      requests++
      assert.equal(options.method, 'GET')
      return response
    }), error => {
      assert.match(error.message, /Connection check failed.*No pairing code was sent/)
      assert.ok(!error.message.includes(secret))
      return true
    })
    assert.equal(requests, 1)
  }
})

test('health requests abort at eight seconds without a pairing POST', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let signal
  const result = pairWithPrecheck(server, 'https:', input, (_url, options) => {
    assert.equal(options.method, 'GET')
    signal = options.signal
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error(secret))))
  })
  const rejected = assert.rejects(result, /Connection check failed or timed out.*No pairing code was sent/)
  t.mock.timers.tick(7999)
  assert.equal(signal.aborted, false)
  t.mock.timers.tick(1)
  assert.equal(signal.aborted, true)
  await rejected
})

test('pairing HTTP failures report status-specific fixed text and never read the error body', async () => {
  for (const [status, message] of [[401, /code is invalid or expired/], [429, /Wait before/], [403, /HTTP 403/], [500, /HTTP 500/]]) {
    const response = new Response(secret, { status })
    response.json = () => assert.fail('Pairing HTTP errors must not read response bodies')
    await assert.rejects(pairWithPrecheck(server, 'https:', input, async (_url, options) =>
      options.method === 'GET' ? Response.json({ status: 'ok' }) : response), error => {
      assert.match(error.message, message)
      assert.ok(!error.message.includes(secret))
      return true
    })
  }
})

test('unreadable pair response reports uncertainty about code consumption without declaring CORS proven', async () => {
  await assert.rejects(pairWithPrecheck(server, 'https:', input, async (_url, options) => {
    if (options.method === 'GET') return Response.json({ status: 'ok' })
    throw new TypeError(secret)
  }), error => {
    assert.match(error.message, /Pair request failed.*Possible causes include network, TLS, WebView permissions or CORS/)
    assert.match(error.message, /code may have been consumed/)
    assert.ok(!error.message.includes(secret))
    return true
  })
})

test('pair timeout is bounded independently of the precheck and does not retry', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let started
  const postStarted = new Promise(resolve => { started = resolve })
  const methods = []
  const result = pairWithPrecheck(server, 'https:', input, async (_url, options) => {
    methods.push(options.method)
    if (options.method === 'GET') return Response.json({ status: 'ok' })
    started()
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error(secret))))
  })
  const rejected = assert.rejects(result, /Pair request failed or timed out.*code may have been consumed/)
  await postStarted
  t.mock.timers.tick(15000)
  await rejected
  assert.deepEqual(methods, ['GET', 'POST'])
})

test('malformed pair JSON and invalid success fields warn about consumption without exposing data', async () => {
  for (const response of [new Response(secret), Response.json(null), Response.json({ detail: secret }),
    Response.json({ ...credentials, token: 'x'.repeat(257) }), Response.json({ ...credentials, session_id: secret })]) {
    await assert.rejects(pairWithPrecheck(server, 'https:', input, async (_url, options) =>
      options.method === 'GET' ? Response.json({ status: 'ok' }) : response), error => {
      assert.match(error.message, /Pairing response.*code may have been consumed/)
      assert.ok(!error.message.includes(secret))
      assert.ok(!error.message.includes(credentials.token))
      return true
    })
  }
})
