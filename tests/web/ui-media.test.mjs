// UI tests of the video: the dashboard's real RTCPeerConnection against a
// fake emulator (a canvas offered from another page). They check the
// negotiation, the first frame, the connection panel, freezes, the reconnection
// of a stream, a hidden tab and a frame in the other orientation.

import assert from 'node:assert/strict';
import { after, afterEach, before, describe, test } from 'node:test';
import { startFakeEmulator } from './fake-emulator.mjs';
import { launch, near, openDashboard, setVisibility, skip } from './harness.mjs';
import { startFakeServer } from './fake-server.mjs';

// Records every peer connection of the page, and every offer it makes.
function capturePeers() {
  const Native = window.RTCPeerConnection;
  window.__pcs = [];
  window.__offers = 0;
  window.RTCPeerConnection = class extends Native {
    constructor(config, ...rest) {
      super(config, ...rest);
      window.__pcs.push({ pc: this, config });
    }

    createOffer(...args) {
      window.__offers++;
      return super.createOffer(...args);
    }
  };
}

describe('media', { skip }, () => {
  let browser;
  let app;
  let emulator;
  let emulatorContext;

  before(async () => {
    browser = await launch();
  });
  after(() => browser?.close());
  afterEach(async () => {
    await emulator?.stop().catch(() => {});
    await app?.close();
    await emulatorContext?.close();
    app = emulator = emulatorContext = null;
  });

  /** A dashboard whose stream comes from a fake emulator that draws at 30 Hz. */
  async function open({ frame = { width: 1080, height: 1920 }, animate = true, ...options } = {}) {
    const server = await startFakeServer();
    emulatorContext = await browser.newContext();
    emulator = await startFakeEmulator(emulatorContext, frame);
    server.onStreamStart = async session => {
      const sdp = await emulator.offer();
      server.sendJsep(session, { type: 'offer', sdp });
      emulator.onCandidate(candidate => server.sendJsep(session, candidate));
    };
    server.onJsep = (session, data) => (data.type === 'answer' ? emulator.answer(data.sdp) : emulator.addCandidate(data)).catch(() => {});
    if (animate) await emulator.animate();
    app = await openDashboard(browser, { server, init: capturePeers, ...options });
    return app;
  }

  const fact = name => app.page.locator(`[data-fact=${name}]`);
  // Connected; and with a frame, which a screen that does not change never sends.
  const connected = () => app.page.waitForFunction(() => document.querySelector('[data-fact=stream]').textContent === 'Live', null, { timeout: 15000 });
  const live = () => app.page.waitForFunction(() => document.querySelector('[data-fact=stream]').textContent === 'Live' && document.getElementById('screen').dataset.frame === 'true', null, { timeout: 15000 });
  const starts = () => app.server.messages('stream').filter(m => m.action === 'start').length;

  test('the stream connects, the first frame replaces the poster and the panel shows the connection', async () => {
    await open();
    await live();
    assert.equal(await app.page.locator('#poster').isHidden(), true);
    assert.equal(await app.page.locator('#video-pill').isHidden(), true);
    assert.equal(await app.page.locator('#overlay').isHidden(), true);
    const video = await app.page.evaluate(() => {
      const v = document.getElementById('video');
      return { width: v.videoWidth, height: v.videoHeight, muted: v.muted, autoplay: v.autoplay, playsInline: v.playsInline, controls: v.controls };
    });
    // The sender of a canvas starts small and grows; the shape is the display's.
    assert.ok(Math.abs(video.width / video.height - 1080 / 1920) < 0.01, `${video.width}x${video.height}`);
    assert.deepEqual([video.muted, video.autoplay, video.playsInline, video.controls], [true, true, true, false]);
    // The figures of the panel come from getStats, once a second.
    await app.page.waitForFunction(() => /^\d+$/.test(document.querySelector('[data-fact=decoded]').textContent) && document.querySelector('[data-fact=codec]').textContent !== '-', null, { timeout: 8000 });
    assert.match(await fact('codec').textContent(), /^(VP8|VP9|H264|AV1)$/);
    const [width, height] = (await fact('size').textContent()).split(' x ').map(Number);
    assert.ok(Math.abs(width / height - 1080 / 1920) < 0.01, `${width}x${height}`);
    await app.page.waitForFunction(() => Number(document.querySelector('[data-fact=fps]').textContent) > 5, null, { timeout: 8000 });
    await app.page.waitForFunction(() => /(kbit|Mbit)\/s$/.test(document.querySelector('[data-fact=rate]').textContent), null, { timeout: 8000 });
    assert.equal(await fact('lost').textContent(), '0');
    assert.match(await fact('ice').textContent(), /^(connected|completed)$/);
    assert.equal(await fact('problem').isHidden(), true);
    assert.equal(await app.page.locator('[data-fact=viewers]').textContent(), '1');
    assert.deepEqual(app.server.violations, []);
    assert.deepEqual(app.errors, []);
  });

  test('the peer connection uses what the server sent, answers only, and sends clean candidates', async () => {
    await open();
    await live();
    const peers = await app.page.evaluate(() => ({
      configs: window.__pcs.map(p => p.config),
      offers: window.__offers,
      local: window.__pcs.map(p => p.pc.localDescription?.type),
      jitter: window.__pcs.at(-1).pc.getReceivers().filter(r => r.track.kind === 'video').map(r => r.jitterBufferTarget),
    }));
    assert.deepEqual(peers.configs, [{ iceServers: [], iceTransportPolicy: 'all' }]);
    assert.equal(peers.offers, 0);
    assert.deepEqual(peers.local, ['answer']);
    assert.deepEqual(peers.jitter, [0]);
    const jsep = app.server.messages('jsep').map(m => m.data);
    assert.equal(jsep.filter(d => d.type === 'answer').length, 1);
    const candidates = jsep.filter(d => d.type !== 'answer');
    assert.ok(candidates.length >= 1, 'trickle ICE: candidates go to the server');
    for (const c of candidates) {
      assert.ok(c.candidate.startsWith('candidate:'), JSON.stringify(c));
      assert.deepEqual(Object.keys(c).sort(), ['candidate', 'sdpMLineIndex', 'sdpMid']);
    }
  });

  test('the ICE servers and the policy of the server are used as they are', async () => {
    const server = await startFakeServer();
    server.iceServers = [{ urls: ['turn:127.0.0.1:9?transport=udp', 'turn:127.0.0.1:9?transport=tcp'], username: '1760000000:aa', credential: 'secret' }];
    server.iceTransportPolicy = 'relay';
    server.onStreamStart = session => server.sendJsep(session, { candidate: 'candidate:1 1 udp 1 127.0.0.1 9 typ host', sdpMid: '0', sdpMLineIndex: 0 });
    app = await openDashboard(browser, { server, init: capturePeers });
    await app.server.until(() => app.server.streams.length === 1, 'a stream');
    await app.page.waitForFunction(() => window.__pcs.length === 1);
    const config = await app.page.evaluate(() => window.__pcs[0].config);
    assert.deepEqual(config, { iceServers: server.iceServers, iceTransportPolicy: 'relay' });
    // A candidate that comes before the offer does not break the dashboard.
    assert.deepEqual(app.errors, []);
  });

  test('a configuration that the browser refuses fails the stream, and the dashboard tries again', async () => {
    const server = await startFakeServer();
    server.iceServers = [{ urls: ['turn:bad url:3478?transport=udp'], username: 'u', credential: 'c' }];
    app = await openDashboard(browser, { server, init: capturePeers });
    await app.page.waitForFunction(() => document.querySelector('[data-fact=problem]').textContent === 'The video connection failed.');
    await app.server.until(() => starts() >= 2, 'a second attempt', 4000);
    assert.equal(await fact('session').textContent(), 'Connected');
    assert.deepEqual(app.errors, []);
  });

  test('a stream that the emulator ends comes back after a backoff, with the poster in between', async () => {
    await open();
    await live();
    app.server.endStreams('the emulator ended the stream');
    await app.page.waitForFunction(() => !document.getElementById('poster').hidden || document.getElementById('screen').dataset.frame === 'false');
    assert.equal(await app.page.locator('#video-pill').isVisible(), true);
    await app.server.until(() => starts() >= 2, 'the second start');
    await live();
    assert.equal(await app.page.locator('#poster').isHidden(), true);
    assert.equal(app.server.streams.length, 1);
    // The device was left alone.
    assert.equal(app.server.requests.some(r => r.method === 'POST'), false);
  });

  test('Reconnect starts a new stream over the old one, and it goes live again', async () => {
    await open();
    await live();
    await app.page.click('#reconnect');
    await app.server.until(() => starts() >= 2, 'the second start');
    // A start replaces the stream: no stop was needed.
    assert.equal(app.server.messages('stream').some(m => m.action === 'stop'), false);
    await live();
    assert.equal(app.server.streams.length, 1);
  });

  test('a lost session brings the stream back with it, without starting the device', async () => {
    await open();
    await live();
    [...app.server.sessions].forEach(s => s.drop());
    await app.page.waitForFunction(() => document.querySelector('[data-fact=session]').textContent.startsWith('Reconnecting'));
    await app.server.until(() => app.server.sessions.size === 1 && starts() >= 2, 'the new stream', 9000);
    await live();
    assert.equal(app.server.requests.some(r => r.method === 'POST'), false);
  });

  test('a video that freezes while the screen changes is restarted after three seconds', async () => {
    await open();
    await live();
    // The screen keeps changing for the server...
    let tick = 0;
    const changing = setInterval(() => (app.server.displayHash = `h${tick++}`), 300);
    try {
      await app.page.waitForTimeout(2500);
      assert.equal(starts(), 1, 'frames flow, so nothing is restarted');
      // ...and the emulator stops sending frames.
      await emulator.stop();
      const stoppedAt = Date.now();
      await app.server.until(() => starts() >= 2, 'the restart', 12000);
      const waited = Date.now() - stoppedAt;
      assert.ok(waited >= 3000 && waited < 9000, `restarted after ${waited} ms`);
      await emulator.animate();
      await live();
    } finally {
      clearInterval(changing);
    }
  });

  test('a still screen is not a freeze, however long it stays still', async () => {
    await open({ animate: false });
    await connected();
    // The hash never changes: the daemon sees a still screen. No frame comes,
    // so the poster stays, and nothing is restarted.
    await app.page.waitForTimeout(6000);
    assert.equal(starts(), 1);
    assert.equal(await app.page.locator('#poster').isVisible(), true);
    assert.deepEqual(app.errors, []);
  });

  test('frames keep a changing screen alive: no restart while they arrive', async () => {
    await open();
    await live();
    let tick = 0;
    const changing = setInterval(() => (app.server.displayHash = `h${tick++}`), 250);
    try {
      await app.page.waitForTimeout(7000);
    } finally {
      clearInterval(changing);
    }
    assert.equal(starts(), 1);
  });

  test('the estimate of the input-to-frame latency appears after an input', async () => {
    await open();
    await live();
    const { x, y } = await app.at(0.5, 0.5);
    for (let i = 0; i < 3; i++) {
      await app.page.mouse.click(x, y);
      await app.page.waitForTimeout(150);
    }
    await app.page.waitForFunction(() => /^\d+ ms$/.test(document.querySelector('[data-fact=latency]').textContent), null, { timeout: 5000 });
  });

  test('a landscape frame turns the display and the touch mapping with it', async () => {
    await open({ frame: { width: 1920, height: 1080 } });
    await live();
    const box = await app.box();
    assert.ok(Math.abs(box.width / box.height - 1920 / 1080) < 0.02, `${box.width}x${box.height}`);
    // The red corner of the picture is at the top left of the landscape frame.
    // Natural = (W - 1 - ly, lx): a point at 25 % along and 20 % down is (864, 480).
    const point = await app.at(0.25, 0.2);
    await app.page.mouse.click(point.x, point.y);
    const [down] = app.server.messages('mouse');
    await app.server.until(() => app.server.messages('mouse').length >= 1, 'the click');
    const [first] = app.server.messages('mouse');
    assert.ok(near(first.x, 864, 4) && near(first.y, 480, 4), `${JSON.stringify(first)} ${JSON.stringify(down)}`);
    // The top left of the landscape frame is the top right of the natural one.
    app.server.clear();
    const corner = await app.at(0.01, 0.02);
    await app.page.mouse.click(corner.x, corner.y);
    await app.server.until(() => app.server.messages('mouse').length >= 1, 'the corner click');
    const [top] = app.server.messages('mouse');
    assert.ok(top.x >= 1050 && top.y <= 40, JSON.stringify(top));
  });

  test('a tab hidden for a minute closes the stream, and showing it brings the stream back', async () => {
    const server = await startFakeServer();
    emulatorContext = await browser.newContext();
    emulator = await startFakeEmulator(emulatorContext);
    server.onStreamStart = async session => {
      server.sendJsep(session, { type: 'offer', sdp: await emulator.offer() });
      emulator.onCandidate(candidate => server.sendJsep(session, candidate));
    };
    server.onJsep = (session, data) => (data.type === 'answer' ? emulator.answer(data.sdp) : emulator.addCandidate(data)).catch(() => {});
    await emulator.animate();
    // Fake time, so that a minute passes at once.
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    await page.clock.install();
    await page.goto(`${server.url}/`);
    await page.waitForFunction(() => document.querySelector('[data-fact=stream]').textContent === 'Live', null, { timeout: 15000 });
    assert.equal(server.streams.length, 1);

    await setVisibility(page, true);
    // The minute passes in steps, so that the server's answers keep the socket alive.
    for (let i = 0; i < 13; i++) {
      await page.clock.runFor(5000);
      await page.waitForTimeout(40);
    }
    await server.until(() => server.streams.length === 0, 'the stream to close');
    assert.equal(server.messages('stream').filter(m => m.action === 'stop').length, 1);
    assert.equal(await page.locator('[data-fact=stream]').textContent(), 'Paused (tab hidden)');
    assert.equal(await page.locator('[data-fact=session]').textContent(), 'Connected');

    await setVisibility(page, false);
    await server.until(() => server.streams.length === 1, 'the stream to come back');
    await page.waitForFunction(() => document.querySelector('[data-fact=stream]').textContent === 'Live', null, { timeout: 15000 });
    await context.close();
    await server.close();
  });

  test('a tab that is back within the minute keeps its stream', async () => {
    await open();
    await live();
    await setVisibility(app.page, true);
    await app.page.waitForTimeout(500);
    await setVisibility(app.page, false);
    await app.page.waitForTimeout(500);
    assert.equal(starts(), 1);
    assert.equal(app.server.messages('stream').some(m => m.action === 'stop'), false);
    assert.equal(await fact('stream').textContent(), 'Live');
  });
});
