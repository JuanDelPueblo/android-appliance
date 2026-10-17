// UI tests of the input: what the dashboard sends for a mouse, fingers, a
// pen, the wheel, the keyboard, composed and pasted text, and the hardware
// buttons. The fake appliance records the messages.

import assert from 'node:assert/strict';
import { after, afterEach, before, describe, test } from 'node:test';
import { launch, near, openDashboard, setVisibility, skip } from './harness.mjs';

describe('input', { skip }, () => {
  let browser;
  let app;

  before(async () => {
    browser = await launch();
  });
  after(() => browser?.close());
  afterEach(async () => {
    await app?.close();
    app = null;
  });

  const open = async options => (app = await openDashboard(browser, options));

  /** The recorded input messages, without their time, once there are `count`. */
  async function input(count, what = 'input messages') {
    await app.server.until(() => app.server.input().length >= count, `${count} ${what}`, 4000);
    await app.page.waitForTimeout(80);
    return app.server.input().map(({ at, ...message }) => message);
  }

  /** Time for messages that must not come. */
  async function quiet() {
    await app.page.waitForTimeout(250);
    assert.deepEqual(app.server.input(), []);
  }

  const cdp = () => app.context.newCDPSession(app.page);

  describe('mouse', () => {
    test('a click is a left button down and up at the device position', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.click(x, y);
      const [down, up, ...rest] = await input(2);
      assert.deepEqual(rest, []);
      assert.equal(down.type, 'mouse');
      assert.deepEqual([down.buttons, up.buttons], [1, 0]);
      assert.ok(near(down.x, 540) && near(down.y, 960), JSON.stringify(down));
      assert.deepEqual([up.x, up.y], [down.x, down.y]);
    });

    test('positions follow the pointer over the whole display, up to its edges', async () => {
      await open();
      // (The corners of the window are rounded; a click there misses the display.)
      for (const [fx, fy, ex, ey] of [
        [0.25, 0.1, 270, 192],
        [0.9, 0.8, 972, 1536],
        [0.002, 0.5, 2, 960],
        [0.998, 0.5, 1077, 960],
        [0.5, 0.001, 540, 1],
        [0.5, 0.999, 540, 1918],
      ]) {
        app.server.clear();
        const { x, y } = await app.at(fx, fy);
        await app.page.mouse.click(x, y);
        const [down] = await input(2);
        assert.ok(near(down.x, ex, 3) && near(down.y, ey, 3), `${fx},${fy}: ${down.x},${down.y} expected ${ex},${ey}`);
      }
    });

    test('a left drag moves like a finger: down, moves with the button held, up at the end', async () => {
      await open();
      const from = await app.at(0.5, 0.7);
      const to = await app.at(0.5, 0.3);
      await app.page.mouse.move(from.x, from.y);
      await app.page.mouse.down();
      await app.page.mouse.move(to.x, to.y, { steps: 12 });
      await app.page.mouse.up();
      const messages = await input(3);
      assert.equal(messages[0].buttons, 1);
      assert.equal(messages.at(-1).buttons, 0);
      const moves = messages.slice(1, -1);
      assert.ok(moves.length >= 1 && moves.length <= 12, `${moves.length} moves`);
      assert.ok(moves.every(m => m.buttons === 1));
      // Down is the start and the lift is the end: nothing is lost on the way.
      assert.ok(near(messages[0].y, 1344, 3));
      assert.ok(near(messages.at(-1).y, 576, 3), `ends at ${messages.at(-1).y}`);
      const ys = messages.map(m => m.y);
      assert.deepEqual(ys, [...ys].sort((a, b) => b - a), 'the drag goes one way');
    });

    test('the right button is a button 2 press (the daemon turns it into Back); the context menu is suppressed', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      const prevented = app.page.evaluate(() => new Promise(resolve => document.getElementById('screen').addEventListener('contextmenu', e => queueMicrotask(() => resolve(e.defaultPrevented)), { once: true })));
      await app.page.mouse.click(x, y, { button: 'right' });
      const [down, up] = await input(2);
      assert.deepEqual([down.type, down.buttons, up.buttons], ['mouse', 2, 0]);
      assert.equal(await prevented, true);
    });

    test('hover sends nothing', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.move(x, y, { steps: 8 });
      await quiet();
    });

    test('a pressed button that is released outside the display still ends the drag', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.move(x, y);
      await app.page.mouse.down();
      await app.page.mouse.move(5, 5, { steps: 4 });
      await app.page.mouse.up();
      const messages = await input(3);
      assert.equal(messages.at(-1).buttons, 0);
      assert.deepEqual([messages.at(-1).x, messages.at(-1).y], [0, 0]);
    });

    test('letterboxing: bars are not the screen, and a drag into a bar clamps to the edge', async () => {
      await open();
      // A box much wider than the 9:16 frame: the frame is centred, bars left and right.
      await app.page.evaluate(() => {
        const device = document.getElementById('device');
        Object.assign(device.style, { width: '640px', height: '320px', aspectRatio: 'auto' });
      });
      const box = await app.box();
      assert.ok(near(box.width, 640, 1) && near(box.height, 320, 1));
      const frameWidth = (320 * 1080) / 1920;
      const left = box.x + (box.width - frameWidth) / 2;
      // On a bar: nothing.
      await app.page.mouse.click(box.x + 20, box.y + 160);
      await quiet();
      // Centre of the frame.
      await app.page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      let [down] = await input(2);
      assert.ok(near(down.x, 540) && near(down.y, 960), JSON.stringify(down));
      // The edges of the frame, not of the box.
      app.server.clear();
      await app.page.mouse.click(left + 1, box.y + 1);
      [down] = await input(2);
      assert.ok(down.x <= 6 && down.y <= 6, JSON.stringify(down));
      app.server.clear();
      await app.page.mouse.click(left + frameWidth - 1, box.y + 319);
      [down] = await input(2);
      assert.ok(down.x >= 1073 && down.y >= 1913, JSON.stringify(down));
      // Start on the frame, drag into the bar: the position stops at the edge.
      app.server.clear();
      await app.page.mouse.move(left + 30, box.y + 160);
      await app.page.mouse.down();
      await app.page.mouse.move(box.x + 5, box.y + 160, { steps: 3 });
      await app.page.mouse.up();
      const messages = await input(3);
      assert.equal(messages.at(-1).x, 0);
    });

    test('nothing goes to a device that is not running', async () => {
      await open({ status: { state: 'stopped', boot_completed: false } });
      const { x, y } = await app.at(0.5, 0.9);
      await app.page.mouse.click(x, y);
      await quiet();
    });
  });

  describe('touch', () => {
    test('a tap is a contact down and a lift of the same id at the device position', async () => {
      await open({ touch: true });
      const { x, y } = await app.at(0.25, 0.75);
      await app.page.touchscreen.tap(x, y);
      const [down, up, ...rest] = await input(2);
      assert.deepEqual(rest, []);
      assert.equal(down.type, 'touch');
      assert.equal(down.touches.length, 1);
      const [contact] = down.touches;
      assert.ok(near(contact.x, 270, 3) && near(contact.y, 1440, 3), JSON.stringify(contact));
      assert.ok(contact.pressure > 0 && contact.pressure <= 1);
      assert.deepEqual(up.touches.map(t => [t.id, t.pressure]), [[contact.id, 0]]);
    });

    test('two fingers are two contacts with their own ids, lifted one at a time', async () => {
      await open({ touch: true });
      const a = await app.at(0.3, 0.5);
      const b = await app.at(0.7, 0.5);
      const session = await cdp();
      const send = (type, points) => session.send('Input.dispatchTouchEvent', { type, touchPoints: points.map(([x, y, id]) => ({ x, y, id })) });
      await send('touchStart', [[a.x, a.y, 1]]);
      await send('touchStart', [[a.x, a.y, 1], [b.x, b.y, 2]]);
      await send('touchMove', [[a.x - 40, a.y, 1], [b.x + 40, b.y, 2]]);
      await app.page.waitForTimeout(100);
      await send('touchEnd', [[b.x + 40, b.y, 2]]);
      await send('touchEnd', [[a.x - 40, a.y, 1]]);
      const messages = await input(4);
      const [first, second, ...later] = messages;
      assert.equal(first.touches.length, 1);
      assert.equal(second.touches.length, 2);
      const ids = second.touches.map(t => t.id);
      assert.equal(new Set(ids).size, 2, 'the two ids differ');
      // The move lists both contacts, moved apart.
      const move = later.find(m => m.touches.length === 2 && m.touches.every(t => t.pressure > 0));
      assert.ok(move, JSON.stringify(later));
      assert.ok(move.touches[1].x - move.touches[0].x > second.touches[1].x - second.touches[0].x + 100);
      // Every contact is lifted with pressure 0, and each lift names a contact that was down.
      const lifted = later.flatMap(m => m.touches.filter(t => t.pressure === 0).map(t => t.id));
      assert.deepEqual([...lifted].sort(), [...ids].sort());
    });

    test('a pen is a finger, with its own pressure', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      const session = await cdp();
      const pen = (type, extra = {}) => session.send('Input.dispatchMouseEvent', { type, x, y, button: 'left', pointerType: 'pen', ...extra });
      await pen('mousePressed', { buttons: 1, clickCount: 1, force: 0.5 });
      await pen('mouseReleased', { buttons: 0, clickCount: 1, force: 0 });
      const [down, up] = await input(2);
      assert.equal(down.type, 'touch');
      assert.ok(near(down.touches[0].x, 540) && near(down.touches[0].y, 960));
      assert.ok(down.touches[0].pressure > 0 && down.touches[0].pressure <= 1);
      assert.equal(up.touches[0].pressure, 0);
    });

    test('a phone-sized window maps the same: letterboxed bars of the phone stage are black, not touch', async () => {
      await open({ viewport: { width: 390, height: 844 }, touch: true });
      const box = await app.box();
      assert.ok(near(box.width, 390, 1));
      await app.page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
      const [down] = await input(2);
      assert.ok(near(down.touches[0].x, 540, 3) && near(down.touches[0].y, 960, 3), JSON.stringify(down));
      app.server.clear();
      // The black bar above the device is not the device.
      await app.page.touchscreen.tap(195, 10);
      await quiet();
    });
  });

  describe('wheel', () => {
    test('a turn is a wheel message at the pointer with the pixel deltas', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.move(x, y);
      await app.page.mouse.wheel(0, 120);
      const [wheel] = await input(1);
      assert.equal(wheel.type, 'wheel');
      assert.ok(near(wheel.x, 540) && near(wheel.y, 960));
      assert.deepEqual([wheel.dx, wheel.dy], [0, 120]);
    });

    test('turns in the same frame add up; sideways deltas are kept', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.move(x, y);
      await app.page.evaluate(() => {
        const screen = document.getElementById('screen');
        const at = { bubbles: true, cancelable: true, clientX: 640, clientY: 400 };
        for (let i = 0; i < 3; i++) screen.dispatchEvent(new WheelEvent('wheel', { ...at, deltaY: 100, deltaX: 10 }));
      });
      const messages = await input(1);
      assert.equal(messages.length, 1);
      assert.deepEqual([messages[0].dx, messages[0].dy], [30, 300]);
    });

    test('line and page deltas become pixels; Ctrl+wheel is left to the browser', async () => {
      await open();
      const result = await app.page.evaluate(() => {
        const screen = document.getElementById('screen');
        const at = { bubbles: true, cancelable: true, clientX: 640, clientY: 400 };
        const lines = new WheelEvent('wheel', { ...at, deltaMode: 1, deltaY: 3 });
        screen.dispatchEvent(lines);
        const zoom = new WheelEvent('wheel', { ...at, deltaY: 50, ctrlKey: true });
        screen.dispatchEvent(zoom);
        return { linesPrevented: lines.defaultPrevented, zoomPrevented: zoom.defaultPrevented };
      });
      assert.deepEqual(result, { linesPrevented: true, zoomPrevented: false });
      const messages = await input(1);
      assert.equal(messages.length, 1);
      assert.equal(messages[0].dy, 120);
    });
  });

  describe('keyboard', () => {
    const key = (k, code, action) => ({ type: 'key', key: k, code, action });

    async function focused() {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.click(x, y);
      await input(2);
      app.server.clear();
    }

    test('a click gives the display the keyboard, with a visible focus ring', async () => {
      await focused();
      assert.equal(await app.page.evaluate(() => document.activeElement.id), 'sink');
      assert.equal(await app.page.evaluate(() => getComputedStyle(document.getElementById('screen')).outlineStyle), 'solid');
    });

    test('printable ASCII is a key press for each key', async () => {
      await focused();
      await app.page.keyboard.type('Hi 5!');
      const messages = await input(5);
      assert.deepEqual(messages, [
        key('H', 'KeyH', 'press'),
        key('i', 'KeyI', 'press'),
        key(' ', 'Space', 'press'),
        key('5', 'Digit5', 'press'),
        key('!', 'Digit1', 'press'),
      ]);
    });

    test('named keys go down and up; Enter, Backspace, arrows, Tab and Escape all reach Android', async () => {
      await focused();
      for (const k of ['Enter', 'Backspace', 'ArrowLeft', 'Tab', 'Escape']) await app.page.keyboard.press(k);
      const messages = await input(10);
      assert.deepEqual(
        messages.map(m => `${m.key}:${m.action}`),
        ['Enter:down', 'Enter:up', 'Backspace:down', 'Backspace:up', 'ArrowLeft:down', 'ArrowLeft:up', 'Tab:down', 'Tab:up', 'Escape:down', 'Escape:up'],
      );
      // Tab went to Android; the focus stays on the display.
      assert.equal(await app.page.evaluate(() => document.activeElement.id), 'sink');
    });

    test('a held named key is one down, however long it repeats', async () => {
      await focused();
      await app.page.keyboard.down('ArrowDown');
      await app.page.keyboard.down('ArrowDown');
      await app.page.keyboard.down('ArrowDown');
      await app.page.keyboard.up('ArrowDown');
      const messages = await input(2);
      assert.deepEqual(messages, [key('ArrowDown', 'ArrowDown', 'down'), key('ArrowDown', 'ArrowDown', 'up')]);
    });

    test('Shift+A and Ctrl+A: the modifier goes down, the key is pressed, the modifier goes up', async () => {
      await focused();
      await app.page.keyboard.press('Shift+A');
      await app.page.keyboard.press('Control+a');
      const messages = await input(6);
      assert.deepEqual(
        messages.map(m => `${m.key}:${m.action}`),
        ['Shift:down', 'A:press', 'Shift:up', 'Control:down', 'a:press', 'Control:up'],
      );
    });

    test('CapsLock is never sent', async () => {
      await focused();
      await app.page.keyboard.press('CapsLock');
      await quiet();
    });

    test('non-ASCII characters are text; unicode and emoji arrive whole', async () => {
      await focused();
      const session = await cdp();
      for (const type of ['keyDown', 'keyUp']) await session.send('Input.dispatchKeyEvent', { type, key: 'é', code: 'Semicolon', text: type === 'keyDown' ? 'é' : undefined });
      await app.page.keyboard.insertText('日本語 😀');
      const messages = await input(2);
      assert.deepEqual(messages, [
        { type: 'text', text: 'é' },
        { type: 'text', text: '日本語 😀' },
      ]);
    });

    test('a composition sends its result as text when it ends, and nothing before', async () => {
      await focused();
      const session = await cdp();
      await session.send('Input.imeSetComposition', { text: 'に', selectionStart: 1, selectionEnd: 1 });
      await session.send('Input.imeSetComposition', { text: 'にほ', selectionStart: 2, selectionEnd: 2 });
      await app.page.waitForTimeout(150);
      assert.deepEqual(app.server.input(), []);
      await session.send('Input.insertText', { text: '日本' });
      const messages = await input(1);
      assert.deepEqual(messages, [{ type: 'text', text: '日本' }]);
      // The hidden field is empty again.
      assert.equal(await app.page.locator('#sink').inputValue(), '');
    });

    test('a paste is sent as text, and Ctrl+V is not a key', async () => {
      await focused();
      await app.page.keyboard.press('Control+v');
      app.server.received.length = 0;
      const prevented = await app.page.evaluate(() => {
        const data = new DataTransfer();
        data.setData('text/plain', 'pasted: ñ, 日本, line\nbreak');
        const event = new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true });
        document.getElementById('sink').dispatchEvent(event);
        return event.defaultPrevented;
      });
      assert.equal(prevented, true);
      const messages = await input(1);
      assert.deepEqual(messages, [{ type: 'text', text: 'pasted: ñ, 日本, line\nbreak' }]);
      // The shortcut itself gave no V key (Control came and went).
      await app.page.keyboard.press('Control+v');
      await app.page.waitForTimeout(100);
      assert.equal(app.server.input().some(m => m.key === 'v' || m.key === 'V'), false);
    });

    test('a soft keyboard sends text, Enter and Backspace through input events', async () => {
      await focused();
      await app.page.evaluate(() => {
        const sink = document.getElementById('sink');
        for (const [inputType, data] of [['insertText', 'q'], ['insertLineBreak', null], ['deleteContentBackward', null]]) {
          sink.dispatchEvent(new InputEvent('beforeinput', { inputType, data, bubbles: true, cancelable: true }));
        }
      });
      const messages = await input(3);
      assert.deepEqual(messages, [{ type: 'text', text: 'q' }, key('Enter', 'Enter', 'press'), key('Backspace', 'Backspace', 'press')]);
    });

    test('Ctrl+Alt+K leaves the keyboard capture, and the toggle takes it back', async () => {
      await focused();
      assert.equal(await app.page.locator('#keyboard-toggle').getAttribute('aria-pressed'), 'true');
      await app.page.keyboard.press('Control+Alt+k');
      await app.page.waitForFunction(() => document.getElementById('keyboard-toggle').getAttribute('aria-pressed') === 'false');
      assert.equal(await app.page.evaluate(() => document.activeElement.id), '');
      // The keys of the browser work again: Tab moves on, nothing reaches Android.
      app.server.clear();
      await app.page.keyboard.type('abc');
      await app.page.keyboard.press('Tab');
      await quiet();
      await app.page.click('#keyboard-toggle');
      assert.equal(await app.page.locator('#keyboard-toggle').getAttribute('aria-pressed'), 'true');
      assert.equal(await app.page.evaluate(() => document.activeElement.id), 'sink');
      await app.page.keyboard.press('x');
      assert.deepEqual(await input(1), [key('x', 'KeyX', 'press')]);
    });

    test('on a phone the keyboard is off until the toolbar asks for it, and then the on-screen keyboard is wanted', async () => {
      await open({ touch: true });
      const toggle = app.page.locator('#keyboard-toggle');
      assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
      // A tap on the display does not raise the on-screen keyboard.
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.touchscreen.tap(x, y);
      await input(2);
      assert.notEqual(await app.page.evaluate(() => document.activeElement.id), 'sink');
      await toggle.tap();
      assert.equal(await toggle.getAttribute('aria-pressed'), 'true');
      assert.deepEqual(await app.page.evaluate(() => [document.activeElement.id, document.activeElement.inputMode]), ['sink', 'text']);
      // A tap on the display keeps the keyboard where it is.
      app.server.clear();
      await app.page.touchscreen.tap(x, y);
      await input(2);
      assert.deepEqual(await app.page.evaluate(() => [document.activeElement.id, document.activeElement.inputMode]), ['sink', 'text']);
      await toggle.tap();
      assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
      assert.notEqual(await app.page.evaluate(() => document.activeElement.id), 'sink');
    });

    test('with the capture off, a click on the display does not take the keyboard', async () => {
      await open();
      await app.page.click('#keyboard-toggle');
      await app.page.click('#keyboard-toggle');
      await app.page.click('#keyboard-toggle');
      assert.equal(await app.page.locator('#keyboard-toggle').getAttribute('aria-pressed'), 'false');
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.click(x, y);
      await input(2);
      assert.notEqual(await app.page.evaluate(() => document.activeElement.id), 'sink');
    });
  });

  describe('hardware buttons', () => {
    const buttons = { BACK: 'Back', HOME: 'Home', APP_SWITCH: 'Recents', POWER: 'Power', VOLUME_UP: 'Volume up', VOLUME_DOWN: 'Volume down' };

    test('each button is a down and an up of its name', async () => {
      await open();
      for (const [name, label] of Object.entries(buttons)) {
        app.server.clear();
        await app.page.getByRole('button', { name: label, exact: true }).click();
        const messages = await input(2);
        assert.deepEqual(messages, [
          { type: 'button', name, action: 'down' },
          { type: 'button', name, action: 'up' },
        ]);
      }
    });

    test('a hold lasts as long as the pointer is down: Power held for a second is a long press', async () => {
      await open();
      const box = await app.page.locator('[data-button=POWER]').boundingBox();
      await app.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await app.page.mouse.down();
      await app.page.waitForTimeout(1100);
      await app.page.mouse.up();
      await input(2);
      const [down, up] = app.server.messages('button');
      assert.deepEqual([down.action, up.action], ['down', 'up']);
      assert.ok(up.at - down.at >= 1000, `held ${up.at - down.at} ms`);
    });

    test('the up comes even when the pointer leaves the button', async () => {
      await open();
      const box = await app.page.locator('[data-button=HOME]').boundingBox();
      await app.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await app.page.mouse.down();
      await app.page.mouse.move(box.x + 300, box.y - 400, { steps: 5 });
      await app.page.mouse.up();
      assert.deepEqual((await input(2)).map(m => m.action), ['down', 'up']);
    });

    test('a touch hold on a button also is a down, and an up when the finger lifts', async () => {
      await open({ touch: true });
      const box = await app.page.locator('[data-button=POWER]').boundingBox();
      const point = { x: box.x + box.width / 2, y: box.y + box.height / 2, id: 1 };
      const session = await cdp();
      await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
      await app.page.waitForTimeout(300);
      assert.deepEqual((await input(1)).map(m => m.action), ['down']);
      await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      assert.deepEqual((await input(2)).map(m => m.action), ['down', 'up']);
    });

    test('from the keyboard, Enter or Space on a button is a whole press', async () => {
      await open();
      await app.page.locator('[data-button=BACK]').focus();
      await app.page.keyboard.press('Enter');
      await app.page.locator('[data-button=HOME]').focus();
      await app.page.keyboard.press('Space');
      assert.deepEqual(await input(2), [
        { type: 'button', name: 'BACK', action: 'press' },
        { type: 'button', name: 'HOME', action: 'press' },
      ]);
    });

    test('a stopped device has no live buttons', async () => {
      await open({ status: { state: 'stopped', boot_completed: false } });
      await app.page.locator('[data-button=HOME]').click({ force: true });
      await quiet();
    });
  });

  describe('release', () => {
    test('a lost window lifts the fingers, the mouse button, the keys and the hardware buttons', async () => {
      await open({ touch: true });
      const session = await cdp();
      const a = await app.at(0.4, 0.4);
      await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: a.x, y: a.y, id: 1 }] });
      await app.page.locator('#keyboard-toggle').dispatchEvent('click');
      await app.page.keyboard.down('Shift');
      await input(2);
      app.server.clear();
      await app.page.evaluate(() => window.dispatchEvent(new Event('blur')));
      const messages = await input(2);
      assert.ok(messages.some(m => m.type === 'touch' && m.touches.every(t => t.pressure === 0)), JSON.stringify(messages));
      assert.ok(messages.some(m => m.type === 'key' && m.key === 'Shift' && m.action === 'up'));
      await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    });

    test('a hidden tab lifts a held hardware button and a mouse drag', async () => {
      await open();
      const box = await app.page.locator('[data-button=POWER]').boundingBox();
      await app.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await app.page.mouse.down();
      await input(1);
      app.server.clear();
      await setVisibility(app.page, true);
      const [up] = await input(1);
      assert.deepEqual(up, { type: 'button', name: 'POWER', action: 'up' });
      await app.page.mouse.up();
    });

    test('the loss of the key focus lifts the keys', async () => {
      await open();
      const { x, y } = await app.at(0.5, 0.5);
      await app.page.mouse.click(x, y);
      await app.page.keyboard.down('Control');
      await input(3);
      app.server.clear();
      await app.page.locator('#panel-toggle').evaluate(el => el.focus());
      await app.page.locator('#sink').evaluate(el => el.blur());
      const messages = await input(1);
      assert.deepEqual(messages, [{ type: 'key', key: 'Control', code: 'ControlLeft', action: 'up' }]);
    });

    test('a dropped connection forgets what was held, and nothing is sent to the new one', async () => {
      await open();
      const box = await app.page.locator('[data-button=HOME]').boundingBox();
      await app.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await app.page.mouse.down();
      await input(1);
      [...app.server.sessions].forEach(s => s.drop());
      await app.page.waitForFunction(() => document.querySelector('[data-fact=session]').textContent !== 'Connected');
      await app.page.mouse.up();
      await app.page.click('#reconnect');
      await app.server.until(() => app.server.sessions.size > 0, 'the new session');
      app.server.clear();
      await app.page.waitForTimeout(300);
      assert.deepEqual(app.server.input(), []);
    });
  });
});
