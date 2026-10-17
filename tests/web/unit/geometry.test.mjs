import assert from 'node:assert/strict';
import { test } from 'node:test';
import { contentRect, frameTurns, mapPoint } from '../../../web/js/geometry.js';

const device = { width: 1080, height: 1920 };
const portrait = { width: 1080, height: 1920 };
const landscape = { width: 1920, height: 1080 };

test('a frame that fills the element has no bars', () => {
  const rect = { left: 100, top: 50, width: 360, height: 640 };
  assert.deepEqual(contentRect(rect, portrait), { left: 100, top: 50, width: 360, height: 640 });
});

test('a wide element puts bars left and right', () => {
  const area = contentRect({ left: 0, top: 0, width: 1000, height: 640 }, portrait);
  assert.deepEqual(area, { left: 320, top: 0, width: 360, height: 640 });
});

test('a tall element puts bars above and below', () => {
  const area = contentRect({ left: 10, top: 20, width: 360, height: 1000 }, portrait);
  assert.equal(area.left, 10);
  assert.equal(area.width, 360);
  assert.equal(area.height, 640);
  assert.equal(area.top, 20 + 180);
});

test('the centre, the corners and the middle of the edges map to device pixels', () => {
  const rect = { left: 100, top: 50, width: 360, height: 640 };
  const at = (clientX, clientY) => mapPoint({ clientX, clientY, rect, frame: portrait, device });
  assert.deepEqual(at(280, 370), { x: 540, y: 960, inside: true });
  assert.deepEqual(at(100, 50), { x: 0, y: 0, inside: true });
  assert.deepEqual(at(460, 690), { x: 1079, y: 1919, inside: true });
  assert.deepEqual(at(100, 370), { x: 0, y: 960, inside: true });
  assert.deepEqual(at(190, 130), { x: 270, y: 240, inside: true });
});

test('a pointer on a letterbox bar is outside and clamped to the edge', () => {
  const rect = { left: 0, top: 0, width: 1000, height: 640 };
  const left = mapPoint({ clientX: 100, clientY: 320, rect, frame: portrait, device });
  assert.deepEqual(left, { x: 0, y: 960, inside: false });
  const right = mapPoint({ clientX: 900, clientY: 640, rect, frame: portrait, device });
  assert.deepEqual(right, { x: 1079, y: 1919, inside: false });
});

test('a pointer far outside the element is clamped', () => {
  const rect = { left: 100, top: 50, width: 360, height: 640 };
  const point = mapPoint({ clientX: -500, clientY: 9000, rect, frame: portrait, device });
  assert.deepEqual(point, { x: 0, y: 1919, inside: false });
});

test('a decoded frame of another size but the same shape maps proportionally', () => {
  const rect = { left: 0, top: 0, width: 270, height: 480 };
  const point = mapPoint({ clientX: 135, clientY: 120, rect, frame: { width: 540, height: 960 }, device });
  assert.deepEqual(point, { x: 540, y: 480, inside: true });
});

test('the orientation of the frame decides the turn', () => {
  assert.equal(frameTurns(portrait, device), 0);
  assert.equal(frameTurns({ width: 540, height: 960 }, device), 0);
  assert.equal(frameTurns(landscape, device), 3);
  assert.equal(frameTurns({ width: 1000, height: 1000 }, device), 3);
  assert.equal(frameTurns(landscape, { width: 1920, height: 1080 }), 0);
});

test('a landscape frame is the natural frame turned back (measured on Android: natural = (W-1-ly, lx))', () => {
  const rect = { left: 0, top: 0, width: 960, height: 540 };
  const at = (clientX, clientY) => mapPoint({ clientX, clientY, rect, frame: landscape, device });
  // Top left of the landscape frame is the top right of the natural frame.
  assert.deepEqual(at(0, 0), { x: 1079, y: 0, inside: true });
  // Bottom left is the top left; top right is the bottom right.
  assert.deepEqual(at(0, 540), { x: 0, y: 0, inside: true });
  assert.deepEqual(at(960, 0), { x: 1079, y: 1919, inside: true });
  assert.deepEqual(at(480, 270), { x: 540, y: 960, inside: true });
  // A point a quarter along the long side and a third down the short side.
  assert.deepEqual(at(240, 180), { x: 720, y: 480, inside: true });
});

test('letterboxing and a turned frame work together', () => {
  const rect = { left: 0, top: 0, width: 960, height: 800 };
  // The 960x540 frame sits in the middle, from y=130 to y=670.
  const middle = mapPoint({ clientX: 480, clientY: 400, rect, frame: landscape, device });
  assert.deepEqual(middle, { x: 540, y: 960, inside: true });
  const bar = mapPoint({ clientX: 480, clientY: 50, rect, frame: landscape, device });
  assert.equal(bar.inside, false);
});

test('all four turns are inverses of the rotation of the natural frame', () => {
  // A natural point (x, y) turned clockwise by n quarter turns lands at (fu, fv)
  // of the frame; mapping it back must give the point again.
  const rect = { left: 0, top: 0, width: 1000, height: 1000 };
  const natural = { x: 270, y: 480 };
  const nu = (natural.x + 0.5) / device.width;
  const nv = (natural.y + 0.5) / device.height;
  const turned = [
    [nu, nv],
    [1 - nv, nu],
    [1 - nu, 1 - nv],
    [nv, 1 - nu],
  ];
  turned.forEach(([fu, fv], turns) => {
    const swapped = turns % 2 === 1;
    const frame = swapped ? landscape : portrait;
    const area = contentRect(rect, frame);
    const point = mapPoint({
      clientX: area.left + fu * area.width,
      clientY: area.top + fv * area.height,
      rect,
      frame,
      device,
      turns,
    });
    assert.deepEqual([point.x, point.y], [natural.x, natural.y], `turns=${turns}`);
  });
});
