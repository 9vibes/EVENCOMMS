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
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: AbortSignal.any([signal, AbortSignal.timeout(120000)]),
    cache: 'no-store',
    credentials: 'omit',
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
