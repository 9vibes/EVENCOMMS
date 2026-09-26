import assert from 'node:assert/strict';
import { test } from 'node:test';
import { canEnterResearchKey, researchBudget } from './research.ts';

test('Research permits HTTPS and exact loopback hosts, not a remote plaintext origin', () => {
  for (const hostname of ['localhost', '127.0.0.1', '[::1]', '::1'])
    assert.equal(canEnterResearchKey({ protocol: 'http:', hostname }), true);
  for (const hostname of ['192.168.1.2', 'localhost.example.org', '127.0.0.1.example.org']) {
    assert.equal(canEnterResearchKey({ protocol: 'http:', hostname }), false);
    assert.equal(canEnterResearchKey({ protocol: 'https:', hostname }), true);
  }
});

test('Research text budgets count code points without silently truncating history', () => {
  const user = text => ({ role: 'user', text });
  const assistant = text => ({ role: 'assistant', text });
  assert.equal(researchBudget([user('\u{1f30d}'.repeat(8000))]), '');
  assert.match(researchBudget([user('\u{1f30d}'.repeat(8001))]), /8,000/);
  assert.equal(researchBudget([assistant('\u{1f30d}'.repeat(16000)), user('Next')]), '');
  assert.match(researchBudget([assistant('x'.repeat(16001)), user('Next')]), /16,000/);
  assert.equal(researchBudget(Array.from({ length: 8 }, () => user('x'.repeat(8000)))), '');
  assert.match(researchBudget([...Array.from({ length: 8 }, () => user('x'.repeat(8000))), user('x')]), /64,000/);
  assert.equal(researchBudget(Array.from({ length: 20 }, () => user('x'))), '');
  assert.match(researchBudget(Array.from({ length: 21 }, () => user('x'))), /20 messages/);
});

test('Research permits three images per turn, six total, and image-only questions', () => {
  const user = count => ({ role: 'user', text: '', images: Array(count).fill('data:image/jpeg;base64,synthetic') });
  assert.equal(researchBudget([user(3)]), '');
  assert.equal(researchBudget([user(3), user(3)]), '');
  assert.match(researchBudget([user(4)]), /3 per turn/);
  assert.match(researchBudget([user(3), user(3), user(1)]), /6 frames/);
  assert.match(researchBudget([{ ...user(1), role: 'assistant' }, user(1)]), /Only user/);
  assert.match(researchBudget([user(0)]), /Write a question/);
  assert.match(researchBudget([{ role: 'assistant', text: 'not a question' }]), /Write a question/);
});
