export interface Session {
  id: string;
  name: string;
  connected: boolean;
  created_at: string;
}

export interface Message {
  id: string;
  session_id: string;
  role: 'wearer' | 'operator';
  text: string;
  created_at: string;
  client_id: string;
}

export interface ServiceStatus {
  stt_enabled: boolean;
  stt_model: string;
  ai_configured: boolean;
  ollama_model: string;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function request<T>(
  path: string,
  token: string | null,
  signal: AbortSignal,
  options: { method?: string; body?: unknown; credentials?: 'same-origin'; onText?: (text: string) => void } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: options.onText ? 'application/x-ndjson' : 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: AbortSignal.any([signal, AbortSignal.timeout(120000)]),
    cache: 'no-store',
    credentials: options.credentials ?? 'omit',
  });
  if (!response.ok) {
    let detail =
      response.status === 401
        ? 'Your session has expired. Sign in again.'
        : `Request failed (${response.status}).`;
    try {
      const error: unknown = await response.json();
      if (error && typeof error === 'object' && 'detail' in error && typeof error.detail === 'string') {
        detail = error.detail;
      }
    } catch {
      // A reverse proxy may return a non-JSON error page.
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  if (options.onText && response.headers.get('content-type')?.split(';')[0] === 'application/x-ndjson') {
    if (!response.body) throw new ApiError('Response stream is unavailable.', 502);
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let buffer = '';
    let count = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let end: number;
        while ((end = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, end);
          buffer = buffer.slice(end + 1);
          if (line.length > 200000) throw new Error('Oversized event');
          if (!line) continue;
          if (++count > 4098) throw new Error('Too many events');
          const event = JSON.parse(line);
          if (event?.type === 'text' && typeof event.text === 'string' && characterCount(event.text) <= 16000) {
            options.onText(event.text);
          } else if (event?.type === 'done' && event.response && typeof event.response.text === 'string'
              && characterCount(event.response.text) <= 16000) {
            return event.response as T;
          } else if (event?.type === 'error' && typeof event.detail === 'string' && Number.isInteger(event.status)) {
            throw new ApiError(event.detail, event.status);
          } else {
            throw new Error('Invalid event');
          }
        }
        if (buffer.length > 200000) throw new Error('Oversized event');
        if (done) throw new Error('Missing completion');
      }
    } catch (error) {
      if (error instanceof ApiError || signal.aborted) throw error;
      throw new ApiError('The response stream ended before a complete reply was received.', 502);
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  return response.json() as Promise<T>;
}

export function errorText(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : 'Could not reach the local service. Check the connection and try again.';
}

export function mergeMessages(previous: Message[], incoming: Message[]): Message[] {
  const messages = new Map(previous.map((message) => [message.id, message]));
  for (const message of incoming) messages.set(message.id, message);
  return [...messages.values()].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
}

export function characterCount(text: string): number {
  // Match the backend's Unicode code point limit, not UTF-16 code units.
  return Array.from(text).length;
}

export function clientId(): string {
  // getRandomValues also works on an HTTP LAN origin, unlike randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
