import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FreezeDetector } from '../../../web/js/freeze.js';

test('a screen that does not change is not a freeze, however long it is quiet', () => {
  const d = new FreezeDetector();
  for (let t = 0; t < 60_000; t += 1000) {
    d.display('same', t);
    assert.equal(d.frozen(t), false);
  }
});

test('a changed hash with a frame in the same interval is fine', () => {
  const d = new FreezeDetector();
  d.display('a', 0);
  d.frame();
  d.display('b', 1000);
  assert.equal(d.frozen(10_000), false);
});

test('a changed hash without a frame freezes after three seconds', () => {
  const d = new FreezeDetector();
  d.display('a', 0);
  d.display('b', 1000);
  assert.equal(d.frozen(1000), false);
  assert.equal(d.frozen(3999), false);
  assert.equal(d.frozen(4000), true);
});

test('a frame that follows the event clears the suspicion (the frame is in flight)', () => {
  const d = new FreezeDetector();
  d.display('a', 0);
  d.display('b', 1000);
  d.frame();
  assert.equal(d.frozen(9000), false);
});

test('the first event only sets the baseline', () => {
  const d = new FreezeDetector();
  d.display('x', 0);
  assert.equal(d.frozen(100_000), false);
});

test('the time is counted from the first event that showed the change', () => {
  const d = new FreezeDetector();
  d.display('a', 0);
  d.display('b', 1000);
  d.display('c', 2000);
  d.display('d', 3000);
  assert.equal(d.frozen(3999), false);
  assert.equal(d.frozen(4000), true);
});

test('a video that freezes in the middle of steady frames is found at the next change', () => {
  const d = new FreezeDetector();
  d.display('a', 0);
  d.frame();
  d.display('b', 1000);
  d.frame();
  d.display('c', 2000);
  // No frame decoded in this interval, although the screen changed.
  d.display('d', 3000);
  assert.equal(d.frozen(5999), false);
  assert.equal(d.frozen(6000), true);
});

test('reset forgets everything', () => {
  const d = new FreezeDetector();
  d.display('a', 0);
  d.display('b', 1000);
  d.reset();
  assert.equal(d.frozen(60_000), false);
  d.display('c', 61_000);
  assert.equal(d.frozen(70_000), false);
});

test('a custom timeout is used', () => {
  const d = new FreezeDetector(500);
  d.display('a', 0);
  d.display('b', 1000);
  assert.equal(d.frozen(1500), true);
});
