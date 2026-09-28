export class ConnectionError extends Error {}

export function validateOrigin(value: unknown, pageProtocol: string): string {
  let url: URL
  try {
    if (typeof value !== 'string' || !/^https?:\/\/[^/?#\s\\*@]+\/?$/i.test(value.trim())) throw new Error()
    url = new URL(value.trim())
    if (!['http:', 'https:'].includes(url.protocol) || url.hostname.includes('*') || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error()
  } catch {
    throw new ConnectionError('Target validation failed. Enter a full HTTP(S) Stone origin without a path, credentials, query or fragment.')
  }
  if (pageProtocol === 'https:' && url.protocol === 'http:') {
    throw new ConnectionError('Target validation failed. An HTTPS app cannot connect to an HTTP Stone. Use a trusted HTTPS address.')
  }
  return url.origin
}

export function connectionDetails(page: Pick<Location, 'origin' | 'protocol'>, browserOrigin: string | undefined, target: string) {
  const label = (origin: string | undefined) => origin === 'null' ? 'null (opaque origin)' : origin || 'Unavailable (not reported)'
  let stoneOrigin = 'Invalid or missing Stone address'
  let pairEndpoint = 'Unavailable (invalid Stone address)'
  try {
    stoneOrigin = validateOrigin(target, page.protocol)
    pairEndpoint = stoneOrigin + '/api/pair'
  } catch { /* Never display an invalid target, which may contain credentials or query data. */ }
  return { pageOrigin: label(page.origin), browserOrigin: label(browserOrigin), protocol: page.protocol, stoneOrigin, pairEndpoint }
}

export function pairingCredentials(value: unknown): { token: string; session_id: string } | null {
  if (!value || typeof value !== 'object' || !('token' in value) || !('session_id' in value) ||
      typeof value.token !== 'string' || !value.token.length || value.token.length > 256 ||
      typeof value.session_id !== 'string' || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value.session_id)) return null
  return { token: value.token, session_id: value.session_id }
}

export async function checkConnection(target: string, pageProtocol: string, fetcher: typeof fetch = fetch): Promise<string> {
  const server = validateOrigin(target, pageProtocol)
  const abort = new AbortController()
  const timer = setTimeout(() => abort.abort(), 8000)
  try {
    const response = await fetcher(server + '/health', {
      method: 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      // Match the pairing request's non-safelisted header without sending any pairing data.
      headers: { 'Content-Type': 'application/json' }, signal: abort.signal,
    })
    if (!response.ok) throw new ConnectionError(`Connection check failed (HTTP ${response.status} from /health). No pairing code was sent.`)
    const result: unknown = await response.json()
    if (!result || typeof result !== 'object' || !('status' in result) || result.status !== 'ok') {
      throw new ConnectionError('Connection check failed: /health did not return JSON status "ok". No pairing code was sent.')
    }
    return server
  } catch (error) {
    if (error instanceof ConnectionError) throw error
    if (error instanceof SyntaxError) throw new ConnectionError('Connection check failed: /health did not return valid JSON. No pairing code was sent.')
    throw new ConnectionError('Connection check failed or timed out: no readable /health response. Possible causes include network, TLS, WebView permissions or CORS. No pairing code was sent.')
  } finally { clearTimeout(timer) }
}

export async function pairWithPrecheck(target: string, pageProtocol: string, input: { code: string; name: string }, fetcher: typeof fetch = fetch) {
  const origin = await checkConnection(target, pageProtocol, fetcher)
  const abort = new AbortController()
  const timer = setTimeout(() => abort.abort(), 15000)
  try {
    let response: Response
    try {
      response = await fetcher(origin + '/api/pair', { method: 'POST', mode: 'cors', credentials: 'omit', cache: 'no-store',
        referrerPolicy: 'no-referrer', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: input.code, name: input.name }), signal: abort.signal })
    } catch {
      throw new ConnectionError('Pair request failed or timed out without a readable response. Possible causes include network, TLS, WebView permissions or CORS. The code may have been consumed; ask the operator for a new code before retrying.')
    }
    if (!response.ok) {
      if (response.status === 401) throw new ConnectionError('Pairing rejected (HTTP 401): the code is invalid or expired. Ask the operator for a new code.')
      if (response.status === 429) throw new ConnectionError('Pairing limited (HTTP 429). Wait before requesting a new code and trying again.')
      throw new ConnectionError(`Pairing failed (HTTP ${response.status}). Ask the operator to check before retrying.`)
    }
    let result: unknown
    try { result = await response.json() }
    catch { throw new ConnectionError('Pairing response was incomplete or invalid JSON. The code may have been consumed; ask the operator for a new code before retrying.') }
    const credentials = pairingCredentials(result)
    if (!credentials) throw new ConnectionError('Pairing response had an invalid success format. The code may have been consumed; ask the operator for a new code before retrying.')
    return { origin, token: credentials.token, session_id: credentials.session_id }
  } finally { clearTimeout(timer) }
}
