// UI tests of the dashboard on a fake appliance, in Chromium: the page, the
// lifecycle controls, the connection and the layout. Input is in
// ui-input.test.mjs and the media in ui-media.test.mjs.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import { after, afterEach, before, describe, test } from 'node:test';
import { CSP, startFakeServer } from './fake-server.mjs';
import { launch, near, openDashboard, skip } from './harness.mjs';

// AA_WEB_SHOTS=<dir> also saves screenshots of the layouts, to look at.
const shots = process.env.AA_WEB_SHOTS;

describe('dashboard', { skip }, () => {
  let browser;
  let app;

  before(async () => {
    browser = await launch();
    if (shots) fs.mkdirSync(shots, { recursive: true });
  });
  after(() => browser?.close());
  afterEach(async () => {
    await app?.close();
    app = null;
  });

  const open = async options => (app = await openDashboard(browser, options));
  const text = selector => app.page.locator(selector).textContent();
  const enabled = selector => app.page.locator(selector).isEnabled();
  const screenshot = name => shots && app.page.screenshot({ path: `${shots}/${name}.png` });

  test('the page loads under the content security policy, without errors', async () => {
    await open();
    assert.equal(app.response.headers()['content-security-policy'], CSP);
    // Icons are CSS masks: they must be fetched and drawn under img-src 'self'.
    const icon = await app.page.evaluate(() => {
      const style = getComputedStyle(document.querySelector('.i-home'));
      return { width: style.width, mask: style.maskImage };
    });
    assert.ok(near(parseFloat(icon.width), 22.4, 0.1), icon.width);
    assert.match(icon.mask, /icons\/home\.svg/);
    assert.equal(await app.page.evaluate(() => performance.getEntriesByType('resource').some(r => r.name.endsWith('/icons/home.svg') && r.transferSize >= 0)), true);
    assert.deepEqual(app.errors, []);
  });

  test('the poster shows the screenshot while there is no video, and is refreshed', async () => {
    await open();
    await app.page.waitForFunction(() => !document.getElementById('poster').hidden && document.getElementById('poster').naturalWidth > 0);
    const shot = app.server.requests.filter(r => r.path === '/api/v1/screenshot?format=jpeg&quality=80&max_size=960&passive=1');
    assert.ok(shot.length >= 1);
    // Refreshed every few seconds while it waits.
    await app.server.until(() => app.server.requests.filter(r => r.path.startsWith('/api/v1/screenshot')).length >= 2, 'a second poster', 7000);
    assert.equal(await text('#video-pill'), 'Connecting the video');
    assert.deepEqual(app.errors, []);
  });

  test('the poster rests after a while, so that an open dashboard does not keep Android awake', async () => {
    app = await openDashboard(browser, { server: await startFakeServer(), init: () => {} });
    await app.close();
    const server = await startFakeServer();
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    await page.clock.install();
    await page.goto(`${server.url}/`);
    const shots = () => server.requests.filter(r => r.path.startsWith('/api/v1/screenshot')).length;
    await server.until(() => shots() >= 1, 'the first poster');
    // A minute and a half in steps, so that the answers of the server arrive.
    for (let i = 0; i < 24; i++) {
      await page.clock.runFor(4000);
      await page.waitForTimeout(25);
    }
    const rested = shots();
    assert.ok(rested >= 6 && rested <= 11, `${rested} posters`);
    for (let i = 0; i < 6; i++) {
      await page.clock.runFor(4000);
      await page.waitForTimeout(25);
    }
    assert.equal(shots(), rested);
    // A screen that changes without a frame gets a new poster.
    await page.waitForFunction(() => document.querySelector('[data-fact=stream]').textContent === 'Connecting');
    server.displayHash = 'changed';
    for (let i = 0; i < 3; i++) {
      await page.clock.runFor(1000);
      await page.waitForTimeout(25);
    }
    await server.until(() => shots() > rested, 'a poster for the changed screen');
    await context.close();
    await server.close();
  });

  test('a stopped device shows Start over the display; nothing starts it and no screenshot is asked for', async () => {
    await open({ status: { state: 'stopped', boot_completed: false } });
    await app.page.locator('#overlay-start').waitFor({ state: 'visible' });
    assert.equal(await text('#overlay-title'), 'Android is stopped');
    await app.page.waitForTimeout(1500);
    assert.deepEqual(app.server.violations, []);
    assert.equal(app.server.requests.filter(r => r.path.startsWith('/api/v1/screenshot') || r.method === 'POST').length, 0);
    assert.equal(app.server.messages('stream').length, 0);
    assert.equal(await app.page.locator('#poster').isHidden(), true);
    await screenshot('stopped');
  });

  test('Start over the display starts Android and the dashboard follows its progress', async () => {
    await open({ status: { state: 'stopped', boot_completed: false } });
    app.server.transitionMs = null;
    await app.page.locator('#overlay-start').click();
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Starting: booting');
    const start = app.server.requests.find(r => r.method === 'POST' && r.path === '/api/v1/start');
    assert.deepEqual(JSON.parse(start.body), { wait: false });
    assert.equal(await text('#overlay-title'), 'Starting Android');
    assert.equal(await text('#overlay-detail'), 'booting');
    assert.equal(await app.page.locator('#overlay-start').isHidden(), true);
    // Nothing asks for the screen before Android runs.
    assert.deepEqual(app.server.violations, []);
    assert.equal(app.server.messages('stream').length, 0);
    app.server.settle();
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Running');
    await app.server.until(() => app.server.messages('stream').some(m => m.action === 'start'), 'the stream start');
    await app.page.locator('#overlay').waitFor({ state: 'hidden' });
  });

  test('the controls are enabled by the state', async () => {
    await open();
    const row = async () => [await enabled('#start'), await enabled('#stop'), await enabled('#restart'), await enabled('[data-button=HOME]')];
    assert.deepEqual(await row(), [false, true, true, true]);
    app.server.setStatus({ state: 'stopped', boot_completed: false });
    await app.page.waitForFunction(() => document.getElementById('start').disabled === false);
    assert.deepEqual(await row(), [true, false, false, false]);
    app.server.setStatus({ state: 'starting', phase: 'booting' });
    await app.page.waitForFunction(() => document.getElementById('stop').disabled === false);
    assert.deepEqual(await row(), [false, true, false, false]);
    app.server.setStatus({ state: 'stopping', phase: null });
    await app.page.waitForFunction(() => document.getElementById('stop').disabled === true);
    assert.deepEqual(await row(), [false, false, false, false]);
    app.server.setStatus({ state: 'running' });
    await app.page.waitForFunction(() => document.getElementById('restart').disabled === false);
    assert.deepEqual(await row(), [false, true, true, true]);
  });

  test('Stop asks first; Cancel does nothing and Stop calls the API', async () => {
    await open();
    await app.page.click('#stop');
    await app.page.locator('#confirm').waitFor({ state: 'visible' });
    assert.equal(await text('#confirm-title'), 'Stop Android?');
    await app.page.getByRole('button', { name: 'Cancel' }).click();
    await app.page.locator('#confirm').waitFor({ state: 'hidden' });
    assert.equal(app.server.requests.some(r => r.path === '/api/v1/stop'), false);
    // A cancelled dialog must not answer the next one: Escape cancels too.
    await app.page.click('#stop');
    await app.page.getByRole('button', { name: 'Stop', exact: true }).last().click();
    await app.server.until(() => app.server.requests.some(r => r.path === '/api/v1/stop'), 'the stop request');
    assert.deepEqual(JSON.parse(app.server.requests.find(r => r.path === '/api/v1/stop').body), { wait: false });
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Stopped');
    assert.equal(await app.page.locator('#overlay-start').isVisible(), true);
    // The stream ended with Android.
    assert.equal(app.server.streams.length, 0);
  });

  test('Escape in the stop dialog stops nothing', async () => {
    await open();
    await app.page.click('#stop');
    await app.page.getByRole('button', { name: 'Stop', exact: true }).last().click();
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Stopped');
    app.server.requests.length = 0;
    app.server.setStatus({ state: 'running' });
    await app.page.waitForFunction(() => document.getElementById('restart').disabled === false);
    await app.page.click('#stop');
    await app.page.keyboard.press('Escape');
    await app.page.locator('#confirm').waitFor({ state: 'hidden' });
    await app.page.waitForTimeout(200);
    assert.equal(app.server.requests.some(r => r.path === '/api/v1/stop'), false);
  });

  test('Restart asks and calls the API; the device goes through its states', async () => {
    await open();
    app.server.transitionMs = 150;
    await app.page.click('#restart');
    await app.page.getByRole('button', { name: 'Restart', exact: true }).last().click();
    await app.server.until(() => app.server.requests.some(r => r.path === '/api/v1/restart'), 'the restart request');
    assert.deepEqual(JSON.parse(app.server.requests.find(r => r.path === '/api/v1/restart').body), { wait: false });
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Starting: booting');
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Running');
    // The stream was dropped with Android and starts again with it.
    await app.server.until(() => app.server.messages('stream').filter(m => m.action === 'start').length >= 2, 'the second stream start');
  });

  test('a refused command shows the message of the server', async () => {
    await open();
    await app.page.route('**/api/v1/restart', route =>
      route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unavailable', message: 'The appliance is shutting down.' } }) }),
    );
    await app.page.click('#restart');
    await app.page.getByRole('button', { name: 'Restart', exact: true }).last().click();
    await app.page.locator('#action-error').waitFor({ state: 'visible' });
    assert.equal(await text('#action-error'), 'The appliance is shutting down.');
    assert.equal(await enabled('#restart'), true);
  });

  test('the panel shows the phase, a crash with its log, and the viewers', async () => {
    await open();
    app.server.setStatus({ state: 'starting', phase: 'restarting', boot_completed: false });
    await app.page.waitForFunction(() => document.getElementById('state-text').textContent === 'Starting: restarting after a crash');
    assert.equal(await text('#overlay-detail'), 'restarting after a crash');
    app.server.setStatus({
      state: 'stopped',
      phase: null,
      last_error: { kind: 'crash', message: 'qemu was killed by signal 11', exit_status: -11, log_tail: 'line one\nGL error: null ctx', time: '2026-10-16T13:20:11+00:00' },
    });
    await app.page.locator('#failure').waitFor({ state: 'visible' });
    assert.equal(await text('#failure-kind'), 'crash');
    assert.match(await text('#failure-message'), /^qemu was killed by signal 11/);
    assert.equal(await text('#overlay-title'), 'Android stopped unexpectedly');
    assert.equal(await app.page.locator('#failure-log').isVisible(), false);
    await app.page.getByText('Log tail').click();
    assert.match(await text('#failure-log'), /GL error: null ctx/);
    await screenshot('crash');
    app.server.setStatus({ state: 'running', last_error: null });
    await app.server.until(() => app.server.streams.length === 1, 'the stream');
    // Other viewers: the daemon counts them all.
    app.server.setStatus({ viewers: 3 });
    await app.page.waitForFunction(() => document.querySelector('[data-fact=viewers]').textContent === '3');
    assert.equal(await app.page.locator('#failure').isHidden(), true);
  });

  test('the connection panel and the title follow the connection', async () => {
    await open();
    assert.equal(await app.page.locator('[data-fact=session]').textContent(), 'Connected');
    assert.equal(await app.page.title(), 'Android appliance: Running');
    const sockets = [...app.server.sessions];
    sockets.forEach(s => s.drop());
    await app.page.waitForFunction(() => document.querySelector('[data-fact=session]').textContent.startsWith('Reconnecting'));
    assert.equal(await text('#overlay-title'), 'Connection lost');
    assert.equal(await app.page.title(), 'Android appliance: Disconnected');
    // The buttons of the device are off while nobody listens.
    assert.equal(await enabled('[data-button=HOME]'), false);
  });

  test('the session reconnects with a growing backoff while the server stays away', async () => {
    await open();
    const first = app.server.connections.length;
    app.server.refuseSockets = true;
    [...app.server.sessions].forEach(s => s.drop());
    await app.server.until(() => app.server.connections.length >= first + 4, 'four attempts', 9000);
    const times = app.server.connections.slice(first - 1);
    const gaps = times.slice(1).map((t, i) => t - times[i]);
    // 0.5 s, 1 s, 2 s, ... with 25 % jitter and the time to fail.
    assert.ok(gaps[0] >= 450 && gaps[0] < 800, `first gap ${gaps[0]}`);
    assert.ok(gaps[1] >= 700 && gaps[1] < 1400, `second gap ${gaps[1]}`);
    assert.ok(gaps[2] >= 1400 && gaps[2] < 2700, `third gap ${gaps[2]}`);
    app.server.refuseSockets = false;
    await app.page.click('#reconnect');
    await app.page.waitForFunction(() => document.querySelector('[data-fact=session]').textContent === 'Connected');
    // The stream comes back with the session; the device was never started or stopped.
    await app.server.until(() => app.server.messages('stream').filter(m => m.action === 'start').length >= 2, 'a new stream');
    assert.equal(app.server.requests.some(r => r.method === 'POST'), false);
  });

  test('Reconnect does not wait for the backoff', async () => {
    await open();
    app.server.refuseSockets = true;
    [...app.server.sessions].forEach(s => s.drop());
    await app.page.waitForFunction(() => document.querySelector('[data-fact=session]').textContent.startsWith('Reconnecting'));
    await app.page.waitForTimeout(1200);
    app.server.refuseSockets = false;
    const before = app.server.connections.length;
    await app.page.click('#reconnect');
    await app.server.until(() => app.server.connections.length > before, 'an immediate attempt', 600);
    await app.page.waitForFunction(() => document.querySelector('[data-fact=session]').textContent === 'Connected');
  });

  test('a session that ended sends the browser to the login page', async () => {
    await open();
    app.server.requireAuth = true;
    [...app.server.sessions].forEach(s => s.drop());
    await app.page.waitForURL('**/login', { timeout: 8000 });
    assert.equal(await app.page.locator('main.login h1').textContent(), 'Android appliance');
  });

  test('Sign out posts to /logout and ends at the login page', async () => {
    await open();
    await app.page.getByRole('button', { name: 'Sign out' }).click();
    await app.page.waitForURL('**/login');
    const post = app.server.requests.find(r => r.method === 'POST' && r.path === '/logout');
    assert.equal(post.headers.origin, app.server.url);
  });

  test('the login form signs in with a same-origin Origin, and the session opens the dashboard', async () => {
    app = await openDashboard(browser, { server: await startFakeServer() });
    app.server.requireAuth = true;
    await app.page.goto(`${app.server.url}/login`);
    await app.page.fill('#token', 'secret');
    await app.page.getByRole('button', { name: 'Sign in' }).click();
    await app.page.waitForURL(`${app.server.url}/`);
    await app.page.locator('#state-text').waitFor();
    const login = app.server.requests.find(r => r.method === 'POST' && r.path === '/login');
    assert.equal(login.headers.origin, app.server.url);
    await app.server.until(() => app.server.sessions.size > 0, 'the session socket');
  });

  test('the login page is styled by the stylesheet of the dashboard', async () => {
    app = await openDashboard(browser, { server: await startFakeServer() });
    await app.page.goto(`${app.server.url}/login?error=Wrong%20token.`);
    assert.equal(await app.page.locator('.login .error').textContent(), 'Wrong token.');
    const style = await app.page.evaluate(() => {
      const button = getComputedStyle(document.querySelector('.login button'));
      return { background: button.backgroundColor, radius: button.borderTopLeftRadius };
    });
    assert.notEqual(style.background, 'rgba(0, 0, 0, 0)');
    assert.equal(style.radius, '10px');
    await screenshot('login');
    assert.deepEqual(app.errors, []);
  });

  test('every control has a name for assistive technology, and the state is a live region', async () => {
    await open();
    const unnamed = await app.page.evaluate(() =>
      [...document.querySelectorAll('button, textarea, input')]
        .filter(el => !(el.getAttribute('aria-label') || el.textContent.trim() || el.title))
        .map(el => el.outerHTML.slice(0, 80)),
    );
    assert.deepEqual(unnamed, []);
    assert.equal(await app.page.locator('.state').getAttribute('role'), 'status');
    // Focus is visible on a button reached by the keyboard.
    await app.page.keyboard.press('Tab');
    const outline = await app.page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
    assert.notEqual(outline, 'none');
  });

  describe('layout', () => {
    for (const scheme of ['light', 'dark']) {
      test(`desktop 1280x800, ${scheme}: the device is centred and as large as the window allows`, async () => {
        await open({ scheme });
        await app.page.waitForFunction(() => !document.getElementById('poster').hidden);
        const box = await app.box();
        const panel = await app.page.locator('#panel').boundingBox();
        assert.ok(Math.abs(box.width / box.height - 1080 / 1920) < 0.01, `aspect ${box.width}x${box.height}`);
        assert.ok(box.height > 600, `height ${box.height}`);
        assert.ok(panel.x >= 1280 - 360 - 1, 'the panel is on the right');
        assert.ok(box.x + box.width < panel.x);
        assert.equal(await app.page.locator('#panel-toggle').isHidden(), true);
        const bars = await app.page.locator('.toolbar').boundingBox();
        assert.ok(bars.y > box.y + box.height - 1 && bars.y + bars.height <= 800);
        await screenshot(`desktop-${scheme}`);
      });
    }

    test('a large window gives the device more room, a short one less', async () => {
      await open({ viewport: { width: 1920, height: 1080 } });
      const large = await app.box();
      await app.page.setViewportSize({ width: 1280, height: 600 });
      await app.page.waitForTimeout(100);
      const short = await app.box();
      assert.ok(large.height > 800 && short.height < 520, `${large.height} ${short.height}`);
      assert.ok(Math.abs(short.width / short.height - 1080 / 1920) < 0.01);
    });

    for (const scheme of ['light', 'dark']) {
      test(`phone 390x844, ${scheme}: the device fills the width, the bar sits below, the panel is a drawer`, async () => {
        await open({ viewport: { width: 390, height: 844 }, touch: true, scheme });
        await app.page.waitForFunction(() => !document.getElementById('poster').hidden);
        const box = await app.box();
        assert.ok(near(box.width, 390, 1), `width ${box.width}`);
        const bar = await app.page.locator('.toolbar').boundingBox();
        assert.ok(bar.y + bar.height >= 843 && bar.height < 70, `bar ${JSON.stringify(bar)}`);
        // All eight buttons fit the bar, each a target of at least 44 px.
        const buttons = await app.page.locator('.toolbar button').evaluateAll(list => list.map(b => b.getBoundingClientRect()).map(r => [r.width, r.height, r.right]));
        assert.equal(buttons.length, 8);
        for (const [w, h, right] of buttons) assert.ok(w >= 44 && h >= 44 && right <= 390, `${w}x${h} ${right}`);
        await screenshot(`phone-${scheme}`);

        // The drawer is closed, out of the tab order, and opens from the bar.
        assert.equal(await app.page.locator('#panel').isVisible(), false);
        await app.page.click('#panel-toggle');
        await app.page.locator('#panel').waitFor({ state: 'visible' });
        assert.equal(await app.page.locator('#panel-toggle').getAttribute('aria-expanded'), 'true');
        await app.page.waitForTimeout(300);
        await screenshot(`phone-drawer-${scheme}`);
        await app.page.keyboard.press('Escape');
        await app.page.locator('#panel').waitFor({ state: 'hidden' });
        assert.equal(await app.page.evaluate(() => document.activeElement.id), 'panel-toggle');
        await app.page.click('#panel-toggle');
        await app.page.locator('#scrim').click({ position: { x: 10, y: 400 } });
        await app.page.locator('#panel').waitFor({ state: 'hidden' });
      });
    }

    test('a phone on its side puts the buttons beside the display', async () => {
      await open({ viewport: { width: 844, height: 390 }, touch: true });
      const box = await app.box();
      const bar = await app.page.locator('.toolbar').boundingBox();
      assert.ok(bar.x >= box.x + box.width - 1, `bar ${JSON.stringify(bar)} display ${JSON.stringify(box)}`);
      assert.ok(box.height >= 380, `height ${box.height}`);
      await screenshot('phone-landscape');
    });
  });
});
