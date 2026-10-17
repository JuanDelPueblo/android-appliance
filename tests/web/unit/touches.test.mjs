import assert from 'node:assert/strict';
import { test } from 'node:test';
import { MAX_CONTACTS, TouchTable, touchPressure } from '../../../web/js/touches.js';

test('pressure is above 0 up to 1, and a missing pressure is a full press', () => {
  assert.equal(touchPressure(0.5), 0.5);
  assert.equal(touchPressure(1), 1);
  assert.equal(touchPressure(0.0001), 0.0001);
  assert.equal(touchPressure(0), 1);
  assert.equal(touchPressure(7), 1);
});

test('a tap is a down and a lift of the same id', () => {
  const table = new TouchTable();
  assert.deepEqual(table.down(41, 100, 200, 512), { type: 'touch', touches: [{ id: 1, x: 100, y: 200, pressure: 512 }] });
  assert.deepEqual(table.up(41, 100, 200), { type: 'touch', touches: [{ id: 1, x: 100, y: 200, pressure: 0 }] });
  assert.equal(table.size, 0);
});

test('every message lists all contacts that are down; a lift lists the lifted one with pressure 0', () => {
  const table = new TouchTable();
  table.down(7, 10, 10, 512);
  const second = table.down(9, 20, 20, 512);
  assert.deepEqual(second.touches.map(t => t.id), [1, 2]);
  table.move(7, 30, 30, 512);
  assert.deepEqual(table.snapshot().touches.map(t => [t.id, t.x, t.pressure]), [[1, 30, 512], [2, 20, 512]]);
  const lifted = table.up(7);
  assert.deepEqual(lifted.touches, [
    { id: 2, x: 20, y: 20, pressure: 512 },
    { id: 1, x: 30, y: 30, pressure: 0 },
  ]);
  assert.deepEqual(table.snapshot().touches.map(t => t.id), [2]);
});

test('ids are reused once a contact is lifted, and never collide', () => {
  const table = new TouchTable();
  table.down(1, 0, 0, 1);
  table.down(2, 0, 0, 1);
  table.up(1);
  const ids = table.down(3, 0, 0, 1).touches.map(t => t.id);
  assert.deepEqual(ids.sort(), [1, 2]);
});

test('the eleventh finger is refused', () => {
  const table = new TouchTable();
  for (let i = 0; i < MAX_CONTACTS; i++) assert.ok(table.down(100 + i, i, i, 512));
  assert.equal(table.down(999, 1, 1, 512), null);
  assert.equal(table.size, MAX_CONTACTS);
});

test('a move that changes nothing is not a change', () => {
  const table = new TouchTable();
  table.down(5, 10, 10, 512);
  assert.equal(table.move(5, 10, 10, 512), false);
  assert.equal(table.move(5, 11, 10, 512), true);
  assert.equal(table.move(6, 1, 1, 512), false);
});

test('releaseAll lifts every contact in one message, once', () => {
  const table = new TouchTable();
  table.down(1, 5, 6, 512);
  table.down(2, 7, 8, 512);
  assert.deepEqual(table.releaseAll(), {
    type: 'touch',
    touches: [
      { id: 1, x: 5, y: 6, pressure: 0 },
      { id: 2, x: 7, y: 8, pressure: 0 },
    ],
  });
  assert.equal(table.releaseAll(), null);
  assert.equal(table.snapshot(), null);
});

test('a lift of an unknown pointer is nothing', () => {
  assert.equal(new TouchTable().up(3), null);
});
