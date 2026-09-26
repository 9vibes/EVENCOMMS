export interface ResearchMessage {
  role: 'user' | 'assistant';
  text: string;
  images?: string[];
}

export function researchBudget(messages: ResearchMessage[]): string {
  if (messages.length > 20) return 'This conversation exceeds 20 messages. Start a New chat; no history is dropped automatically.';
  let characters = 0;
  let images = 0;
  for (const message of messages) {
    const count = Array.from(message.text).length;
    if (count > (message.role === 'user' ? 8000 : 16000))
      return message.role === 'user' ? 'Shorten your question to 8,000 characters.' : 'An assistant reply exceeds 16,000 characters. Start a New chat.';
    characters += count;
    const attached = message.images?.length ?? 0;
    if (attached > (message.role === 'user' ? 3 : 0)) return 'Only user messages may attach frames, up to 3 per turn.';
    images += attached;
  }
  if (characters > 64000) return 'This conversation exceeds 64,000 text characters. Start a New chat.';
  if (images > 6) return 'This conversation exceeds 6 frames. Remove a draft frame or start a New chat.';
  const last = messages.at(-1);
  if (!last || last.role !== 'user' || (!last.text.trim() && !last.images?.length)) return 'Write a question or capture a frame before sending.';
  return '';
}

export function canEnterResearchKey(location: { protocol: string; hostname: string }): boolean {
  return location.protocol === 'https:' || ['localhost', '127.0.0.1', '[::1]', '::1'].includes(location.hostname);
}
