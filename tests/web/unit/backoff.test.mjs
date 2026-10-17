import assert from 'node:assert/strict';
import { test } from 'node:test';
import { backoffDelay, MAX_DELAY, MIN_DELAY } from '../../../web/js/backoff.js';

const low = () => 0;
const middle = () => 0.5;
const high = () => 1;

test('the delay doubles from half a second to ten seconds', () => {
  const delays = [0, 1, 2, 3, 4, 5, 6].map(attempt => backoffDelay(attempt, middle));
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 10000, 10000]);
});

test('jitter stays within a quarter of the delay and inside the bounds', () => {
  assert.equal(backoffDelay(2, low), 1500);
  assert.equal(backoffDelay(2, high), 2500);
  for (let attempt = 0; attempt < 20; attempt++) {
    for (const random of [low, middle, high]) {
      const delay = backoffDelay(attempt, random);
      assert.ok(delay >= MIN_DELAY && delay <= MAX_DELAY, `attempt ${attempt}: ${delay}`);
    }
  }
});

test('the first retry never comes sooner than the minimum', () => {
  assert.equal(backoffDelay(0, low), MIN_DELAY);
});

test('a huge attempt number stays at the maximum', () => {
  assert.equal(backoffDelay(10_000, middle), MAX_DELAY);
});
