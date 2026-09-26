import assert from 'node:assert/strict';
import { test } from 'node:test';
import { characterCount, clientId, mergeMessages } from './api.ts';

test('character limits count Unicode code points, including astral characters', () => {
  assert.equal(characterCount(''), 0);
  assert.equal(characterCount('hello'), 5);
  assert.equal(characterCount('\u{1f30d}'.repeat(4000)), 4000);
  assert.equal(characterCount('e\u0301'), 2);
  assert.equal(characterCount('x'.repeat(4001)), 4001);
});

test('client IDs are unique UUID v4 values, including on origins without randomUUID', () => {
  const ids = Array.from({ length: 100 }, () => clientId());
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) {
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  }
});

test('polling merges deduplicated history without dropping a newer reply acknowledgement', () => {
  const wearer = { id: 'a', text: 'Question', created_at: '2026-09-26T12:00:00Z' };
  const operator = { id: 'b', text: 'Reply', created_at: '2026-09-26T12:00:02Z' };
  const previous = Object.freeze([operator]);
  const incoming = Object.freeze([wearer]);
  const merged = mergeMessages(previous, incoming);
  assert.deepEqual(merged, [wearer, operator]);
  assert.deepEqual(mergeMessages(merged, [wearer, operator]), merged);
  assert.deepEqual(previous, [operator]);
  assert.deepEqual(incoming, [wearer]);
});

test('same-timestamp messages preserve server order and duplicates use the latest value', () => {
  const first = { id: 'z', text: 'First', created_at: '2026-09-26T12:00:00Z' };
  const second = { id: 'a', text: 'Second', created_at: first.created_at };
  assert.deepEqual(mergeMessages([], [first, second]), [first, second]);
  const updated = { ...first, text: 'Canonical server value' };
  assert.deepEqual(mergeMessages([first, second], [updated]), [updated, second]);
});
