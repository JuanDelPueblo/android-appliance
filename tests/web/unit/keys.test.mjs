import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chunkText, classifyKey, inputMessage, KeyTracker } from '../../../web/js/keys.js';

const key = (k, extra = {}) => ({ key: k, code: extra.code ?? `Key${k.toUpperCase()}`, keyCode: 0, ...extra });

test('printable ASCII is a key press', () => {
  for (const k of ['a', 'Z', '7', ' ', '~', '!', '@']) assert.deepEqual(classifyKey(key(k)), { kind: 'press', key: k });
});

test('named keys are named', () => {
  for (const k of ['Enter', 'Backspace', 'ArrowLeft', 'Escape', 'Tab', 'Shift', 'Control', 'F5', 'Home']) {
    assert.deepEqual(classifyKey(key(k)), { kind: 'named', key: k });
  }
});

test('CapsLock, Dead, Process and Unidentified never go to Android', () => {
  for (const k of ['CapsLock', 'Dead', 'Process', 'Unidentified']) assert.deepEqual(classifyKey(key(k)), { kind: 'ignore' });
});

test('keys of a composition are left to the IME', () => {
  assert.deepEqual(classifyKey(key('a', { isComposing: true })), { kind: 'ignore' });
  assert.deepEqual(classifyKey(key('a', { keyCode: 229 })), { kind: 'ignore' });
});

test('non-ASCII characters, including emoji, are text', () => {
  assert.deepEqual(classifyKey(key('é')), { kind: 'text', text: 'é' });
  assert.deepEqual(classifyKey(key('日')), { kind: 'text', text: '日' });
  assert.deepEqual(classifyKey(key('😀')), { kind: 'text', text: '😀' });
});

test('AltGr (reported as Ctrl+Alt) still types its character; Ctrl or Meta alone is a shortcut', () => {
  assert.deepEqual(classifyKey(key('€', { ctrlKey: true, altKey: true })), { kind: 'text', text: '€' });
  assert.deepEqual(classifyKey(key('é', { ctrlKey: true })), { kind: 'ignore' });
  assert.deepEqual(classifyKey(key('é', { metaKey: true })), { kind: 'ignore' });
});

test('Ctrl+V and Cmd+V are left to the browser, which pastes', () => {
  assert.deepEqual(classifyKey(key('v', { ctrlKey: true })), { kind: 'paste' });
  assert.deepEqual(classifyKey(key('V', { ctrlKey: true, shiftKey: true })), { kind: 'paste' });
  assert.deepEqual(classifyKey(key('v', { metaKey: true })), { kind: 'paste' });
  assert.deepEqual(classifyKey(key('v')), { kind: 'press', key: 'v' });
  // Ctrl+A, Ctrl+C, Ctrl+X reach Android as keys.
  assert.deepEqual(classifyKey(key('a', { ctrlKey: true })), { kind: 'press', key: 'a' });
});

test('a tracker sends a press for a printable key, also when it repeats, and nothing on keyup', () => {
  const tracker = new KeyTracker();
  const down = key('a', { code: 'KeyA' });
  assert.deepEqual(tracker.keydown(down), { handled: true, messages: [{ type: 'key', key: 'a', code: 'KeyA', action: 'press' }] });
  assert.equal(tracker.keydown({ ...down, repeat: true }).messages.length, 1);
  assert.deepEqual(tracker.keyup(down), { handled: true, messages: [] });
});

test('a tracker sends down and up for a named key, and drops the repeats', () => {
  const tracker = new KeyTracker();
  const enter = key('Enter', { code: 'Enter' });
  assert.deepEqual(tracker.keydown(enter).messages, [{ type: 'key', key: 'Enter', code: 'Enter', action: 'down' }]);
  assert.deepEqual(tracker.keydown({ ...enter, repeat: true }), { handled: true, messages: [] });
  assert.deepEqual(tracker.keyup(enter).messages, [{ type: 'key', key: 'Enter', code: 'Enter', action: 'up' }]);
  // Nothing is held any more.
  assert.deepEqual(tracker.releaseAll(), []);
});

test('the key up carries the key that went down, even when the layer changed in between', () => {
  const tracker = new KeyTracker();
  tracker.keydown(key('Shift', { code: 'ShiftLeft' }));
  const up = tracker.keyup(key('Shift', { code: 'ShiftLeft' }));
  assert.equal(up.messages[0].key, 'Shift');
});

test('Ctrl+A sends Control down, the key press, and Control up', () => {
  const tracker = new KeyTracker();
  const sent = [];
  const run = (fn, event) => sent.push(...fn.call(tracker, event).messages);
  run(tracker.keydown, key('Control', { code: 'ControlLeft', ctrlKey: true }));
  run(tracker.keydown, key('a', { code: 'KeyA', ctrlKey: true }));
  run(tracker.keyup, key('a', { code: 'KeyA', ctrlKey: true }));
  run(tracker.keyup, key('Control', { code: 'ControlLeft' }));
  assert.deepEqual(
    sent.map(m => `${m.key}:${m.action}`),
    ['Control:down', 'a:press', 'Control:up'],
  );
});

test('a non-ASCII key sends text on keydown, and its keyup is swallowed', () => {
  const tracker = new KeyTracker();
  const e = key('é', { code: 'Semicolon' });
  assert.deepEqual(tracker.keydown(e), { handled: true, messages: [{ type: 'text', text: 'é' }] });
  assert.deepEqual(tracker.keyup(e), { handled: true, messages: [] });
});

test('keys that are ignored are not handled, so the browser keeps them', () => {
  const tracker = new KeyTracker();
  assert.deepEqual(tracker.keydown(key('CapsLock', { code: 'CapsLock' })), { handled: false, messages: [] });
  assert.deepEqual(tracker.keyup(key('CapsLock', { code: 'CapsLock' })), { handled: false, messages: [] });
  assert.deepEqual(tracker.keydown(key('v', { ctrlKey: true })), { handled: false, messages: [] });
});

test('releaseAll lifts the keys that are down, once', () => {
  const tracker = new KeyTracker();
  tracker.keydown(key('Shift', { code: 'ShiftLeft' }));
  tracker.keydown(key('ArrowDown', { code: 'ArrowDown' }));
  tracker.keydown(key('a', { code: 'KeyA' }));
  const released = tracker.releaseAll();
  assert.deepEqual(released.map(m => `${m.key}:${m.action}`).sort(), ['ArrowDown:up', 'Shift:up']);
  assert.deepEqual(tracker.releaseAll(), []);
});

test('soft keyboards: text, Enter and Backspace arrive as beforeinput events', () => {
  assert.deepEqual(inputMessage({ inputType: 'insertText', data: 'x', isComposing: false }), { type: 'text', text: 'x' });
  assert.deepEqual(inputMessage({ inputType: 'insertLineBreak', data: null, isComposing: false }), {
    type: 'key',
    key: 'Enter',
    code: 'Enter',
    action: 'press',
  });
  assert.equal(inputMessage({ inputType: 'deleteContentBackward', isComposing: false }).key, 'Backspace');
  assert.equal(inputMessage({ inputType: 'deleteContentForward', isComposing: false }).key, 'Delete');
});

test('the middle of a composition and unknown input types send nothing', () => {
  assert.equal(inputMessage({ inputType: 'insertCompositionText', data: 'か', isComposing: true }), null);
  assert.equal(inputMessage({ inputType: 'insertText', data: 'x', isComposing: true }), null);
  assert.equal(inputMessage({ inputType: 'insertText', data: null, isComposing: false }), null);
  assert.equal(inputMessage({ inputType: 'historyUndo', isComposing: false }), null);
});

test('a text that fits is one piece; a long one is cut into pieces that join to the original', () => {
  assert.deepEqual(chunkText('hello'), ['hello']);
  assert.deepEqual(chunkText(''), []);
  const long = 'a'.repeat(130_000);
  const pieces = chunkText(long);
  assert.deepEqual(pieces.map(p => p.length), [60_000, 60_000, 10_000]);
  assert.equal(pieces.join(''), long);
});

test('a cut never splits a surrogate pair', () => {
  const text = '😀'.repeat(10);
  const pieces = chunkText(text, 5);
  assert.equal(pieces.join(''), text);
  for (const piece of pieces) assert.ok(!/^[\udc00-\udfff]|[\ud800-\udbff]$/.test(piece), JSON.stringify(piece));
  assert.deepEqual(pieces.map(p => p.length), [4, 4, 4, 4, 4]);
});
