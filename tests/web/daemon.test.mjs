// The dashboard against the real daemon (session, login, static files, input
// to the emulator's gRPC), with the fake emulator of the Python tests. It
// checks the contract between the two, which the fake appliance of the other
// UI tests only mirrors. Needs Playwright and the Python environment of the
// daemon (AA_PYTHON, or .venv/bin/python of the repository).

import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { after, before, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { launch, near, skip as noPlaywright } from './harness.mjs';

const repo = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const python = process.env.AA_PYTHON || path.join(repo, '.venv', 'bin', 'python');
const skip = noPlaywright || (fs.existsSync(python) ? false : `no Python environment (${python}); run scripts/dev-venv.sh`);

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

describe('dashboard against the daemon', { skip }, () => {
  let work;
  let daemon;
  let browser;
  let base;
  let context;
  let page;

  const jsonl = name => {
    try {
      return fs.readFileSync(path.join(work, 'fake', `${name}.jsonl`), 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
    } catch {
      return [];
    }
  };
  /** The calls that the fake emulator got, once there are `count`. */
  const calls = async (count, what) => {
    const end = Date.now() + 6000;
    while (jsonl('input').length < count && Date.now() < end) await sleep(25);
    assert.ok(jsonl('input').length >= count, `timed out waiting for ${what}`);
    await sleep(100);
    return jsonl('input');
  };
  const fact = name => page.locator(`[data-fact=${name}]`);
  const state = () => page.locator('#state-text').textContent();

  /** An answer of the daemon's unix socket (the trusted way that `androidctl` takes). */
  const local = (method, route) =>
    new Promise((resolve, reject) => {
      const request = http.request({ socketPath: path.join(work, 'run', 'api.sock'), path: route, method }, response => {
        let body = '';
        response.on('data', chunk => (body += chunk));
        response.on('end', () => resolve(JSON.parse(body)));
      });
      request.on('error', reject);
      request.end();
    });
  /** A one-time login link (what `androidctl dashboard` prints). */
  const loginLink = async () => (await local('POST', '/api/v1/auth/login-link')).url;

  before(async () => {
    work = fs.mkdtempSync(path.join(os.tmpdir(), 'aa-web-'));
    const port = await new Promise(resolve => {
      const probe = net.createServer().listen(0, '127.0.0.1', () => {
        const { port } = probe.address();
        probe.close(() => resolve(port));
      });
    });
    daemon = spawn(python, [path.join(repo, 'tests', 'web', 'daemon.py'), work, String(port)], { stdio: 'ignore' });
    base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 150; i++) {
      try {
        if ((await fetch(`${base}/api/v1/health`)).ok) break;
      } catch {
        // Not listening yet.
      }
      await sleep(100);
    }
    browser = await launch();
    context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    await context.addInitScript(() => {
      const Native = window.RTCPeerConnection;
      window.__pcs = [];
      window.RTCPeerConnection = class extends Native {
        constructor(config, ...rest) {
          super(config, ...rest);
          window.__pcs.push(config);
        }
      };
    });
    page = await context.newPage();
  });

  after(async () => {
    await browser?.close();
    if (daemon) {
      // SIGTERM stops Android gracefully and the daemon with it.
      daemon.kill('SIGTERM');
      await new Promise(resolve => {
        const timer = setTimeout(() => daemon.kill('SIGKILL'), 10_000);
        daemon.on('exit', () => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
    if (work) fs.rmSync(work, { recursive: true, force: true });
  });

  test('a login link opens the dashboard; a stopped Android stays stopped', async () => {
    const errors = [];
    page.on('console', m => m.type() === 'error' && errors.push(m.text()));
    await page.goto(await loginLink());
    await page.waitForURL(`${base}/`);
    await page.waitForFunction(() => document.getElementById('state-text').textContent === 'Stopped');
    assert.equal(await page.locator('#overlay-title').textContent(), 'Android is stopped');
    assert.equal(await fact('session').textContent(), 'Connected');
    // The icons and the script come from the daemon's static files, under its policy.
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.i-home')).maskImage.includes('home.svg')), true);
    await sleep(1500);
    assert.deepEqual(jsonl('launches'), [], 'the dashboard did not start Android');
    assert.deepEqual(errors, []);
  });

  test('Start boots the fake device; the poster is a screenshot of it', async () => {
    await page.click('#overlay-start');
    await page.waitForFunction(() => document.getElementById('state-text').textContent === 'Running', null, { timeout: 30_000 });
    await page.waitForFunction(() => document.getElementById('poster').naturalWidth > 0, null, { timeout: 10_000 });
    assert.equal(jsonl('launches').length, 1);
    assert.equal(await fact('api').textContent(), 'API 35');
  });

  test('mouse, touch, wheel, keys, text and buttons reach the emulator as the daemon maps them', async () => {
    const box = await page.locator('#video').boundingBox();
    const at = (fx, fy) => ({ x: box.x + box.width * fx, y: box.y + box.height * fy });
    const centre = at(0.5, 0.5);
    const before = jsonl('input').length;

    // A click is a finger at the device position.
    await page.mouse.click(centre.x, centre.y);
    let all = await calls(before + 2, 'the click');
    const [down, up] = all.slice(before);
    assert.equal(down.type, 'touch');
    assert.ok(near(down.touches[0].x, 540) && near(down.touches[0].y, 960));
    assert.ok(down.touches[0].pressure > 0);
    assert.deepEqual([up.touches[0].id, up.touches[0].pressure], [down.touches[0].id, 0]);

    // Keys: printable ASCII is a key press, a named key goes down and up.
    const keys = jsonl('input').length;
    await page.keyboard.type('ab');
    await page.keyboard.press('Enter');
    all = await calls(keys + 4, 'the keys');
    assert.deepEqual(
      all.slice(keys).map(c => `${c.key}:${c.eventType}`),
      ['a:keypress', 'b:keypress', 'Enter:keydown', 'Enter:keyup'],
    );

    // Hardware buttons: Home is GoHome (the W3C Home key moves the cursor), held until the pointer lifts.
    const buttons = jsonl('input').length;
    for (const label of ['Home', 'Back', 'Recents', 'Volume up', 'Volume down']) await page.getByRole('button', { name: label, exact: true }).click();
    const power = page.getByRole('button', { name: 'Power', exact: true });
    const powerBox = await power.boundingBox();
    await page.mouse.move(powerBox.x + powerBox.width / 2, powerBox.y + powerBox.height / 2);
    await page.mouse.down();
    await sleep(300);
    await page.mouse.up();
    all = await calls(buttons + 12, 'the buttons');
    assert.deepEqual(
      all.slice(buttons).map(c => `${c.key}:${c.eventType}`),
      [
        'GoHome:keydown', 'GoHome:keyup',
        'GoBack:keydown', 'GoBack:keyup',
        'AppSwitch:keydown', 'AppSwitch:keyup',
        'AudioVolumeUp:keydown', 'AudioVolumeUp:keyup',
        'AudioVolumeDown:keydown', 'AudioVolumeDown:keyup',
        'Power:keydown', 'Power:keyup',
      ],
    );

    // The right button is Back.
    const right = jsonl('input').length;
    await page.mouse.click(centre.x, centre.y, { button: 'right' });
    all = await calls(right + 1, 'the right click');
    assert.deepEqual(all.slice(right).map(c => `${c.type}:${c.key}`), ['key:GoBack']);

    // Text goes through the clipboard of the guest, exactly (unicode too), and the clipboard is restored.
    const text = jsonl('input').length;
    await page.keyboard.insertText('héllo 日本');
    all = await calls(text + 4, 'the paste');
    const paste = all.slice(text);
    assert.deepEqual(paste.map(c => c.type), ['getClipboard', 'setClipboard', 'key', 'setClipboard']);
    assert.equal(paste[1].text, 'héllo 日本');
    assert.equal(paste[2].keyCode, 135);
    assert.equal(paste[3].text, 'user-clipboard');

    // A wheel turn is a short drag against the turn.
    const wheel = jsonl('input').length;
    await page.mouse.move(centre.x, centre.y);
    await page.mouse.wheel(0, 240);
    // The drag takes 200 ms; it ends with the lift.
    await calls(wheel + 4, 'the wheel drag');
    const end = Date.now() + 3000;
    while (jsonl('input').at(-1)?.touches?.[0]?.pressure !== 0 && Date.now() < end) await sleep(25);
    const drag = jsonl('input').slice(wheel).filter(c => c.type === 'touch').map(c => c.touches[0]);
    assert.ok(drag.length >= 3);
    assert.ok(drag.at(-1).y < drag[0].y - 200, 'scrolling down moves the finger up');
    assert.equal(drag.at(-1).pressure, 0);
  });

  test('two fingers are two emulator contacts with identifiers of their own', async () => {
    // A phone: the touch screen needs the page to be reloaded as a touch context, so use CDP touches.
    const box = await page.locator('#video').boundingBox();
    const session = await context.newCDPSession(page);
    const start = jsonl('input').length;
    const a = { x: box.x + box.width * 0.3, y: box.y + box.height * 0.5, id: 1 };
    const b = { x: box.x + box.width * 0.7, y: box.y + box.height * 0.5, id: 2 };
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [a] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [a, b] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    const touches = (await calls(start + 3, 'the two contacts')).slice(start).filter(c => c.type === 'touch');
    const both = touches.find(c => c.touches.length === 2);
    assert.ok(both, JSON.stringify(touches));
    assert.notEqual(both.touches[0].id, both.touches[1].id);
    const lifted = touches.flatMap(c => c.touches.filter(t => t.pressure === 0).map(t => t.id));
    assert.deepEqual([...new Set(lifted)].sort(), both.touches.map(t => t.id).sort());
  });

  test('the stream is negotiated with the relay servers of the daemon, and the answer reaches the emulator', async () => {
    await page.waitForFunction(() => window.__pcs.length >= 1);
    const config = await page.evaluate(() => window.__pcs[0]);
    assert.equal(config.iceTransportPolicy, 'relay');
    assert.ok(config.iceServers.length >= 1);
    const urls = config.iceServers.flatMap(s => s.urls);
    assert.ok(urls.some(u => u.startsWith('turn:127.0.0.1:') && u.includes('transport=udp')), JSON.stringify(urls));
    assert.ok(config.iceServers.every(s => s.username && s.credential));
    const end = Date.now() + 8000;
    while (!jsonl('rtc').some(e => e.event === 'send' && e.message.type === 'answer') && Date.now() < end) await sleep(50);
    assert.ok(jsonl('rtc').some(e => e.event === 'send' && e.message.type === 'answer'), 'the answer was relayed');
    // The daemon counts the stream as a viewer.
    assert.equal(await fact('viewers').textContent(), '1');
  });

  test('Stop asks, stops the fake device, and nothing starts it again', async () => {
    await page.click('#stop');
    await page.getByRole('button', { name: 'Stop', exact: true }).last().click();
    await page.waitForFunction(() => document.getElementById('state-text').textContent === 'Stopped', null, { timeout: 30_000 });
    await sleep(1500);
    assert.equal(await state(), 'Stopped');
    assert.equal(jsonl('launches').length, 1, 'no second launch');
    assert.equal(await page.locator('#overlay-start').isVisible(), true);
  });

  test('Sign out ends the session: the dashboard is behind the login again', async () => {
    await page.getByRole('button', { name: 'Sign out' }).click();
    await page.waitForURL('**/login');
    await page.goto(`${base}/`);
    await page.waitForURL('**/login');
    assert.equal(await page.locator('main.login h1').textContent(), 'Android appliance');
  });

  test('the login form signs in with the token: the form post carries the origin of the page', async () => {
    await page.fill('#token', 'not-the-token');
    await page.click('button[type=submit]');
    await page.locator('.error').waitFor();
    assert.equal(await page.locator('.error').textContent(), 'Wrong token.');
    await page.fill('#token', (await local('GET', '/api/v1/auth/token')).token);
    await page.click('button[type=submit]');
    await page.waitForURL(`${base}/`);
    await page.waitForFunction(() => document.getElementById('state-text').textContent === 'Stopped');
    assert.equal(await fact('session').textContent(), 'Connected');
  });
});
