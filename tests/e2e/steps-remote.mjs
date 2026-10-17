// Steps: another host name, what TURN allows, and that no stream is left behind.

import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import {
  assert, ctl, info, instance, newContext, qemuDescriptors, relaySockets, status, step, until, videoStats, waitLive,
} from './lib.mjs';
import {
  S, DESKTOP, measurements, openApp, framesWhileScrolling,
} from './shared.mjs';
import { probe } from './turn-probe.mjs';

step('remote access: another host name, TURN URLs follow the page, media flows', async () => {
  const url = new URL(instance.url);
  assert(url.hostname === '127.0.0.1', `the instance must listen on 127.0.0.1, not ${url.hostname}`);
  const remote = `${url.protocol}//localhost:${url.port}`;
  const context = await newContext(S.browser, { viewport: DESKTOP });
  const servers = [];
  try {
    const link = ctl('dashboard').replace(instance.url, remote);
    const page = await context.newPage();
    page.on('websocket', socket => socket.on('framereceived', ({ payload }) => {
      try {
        const m = JSON.parse(payload);
        if (m.type === 'stream' && m.state === 'configured') servers.push(...m.iceServers);
      } catch {
        // Not JSON.
      }
    }));
    await page.goto(link);
    await page.waitForURL(`${remote}/`, { timeout: 10_000 });
    const f = await waitLive(page, { timeout: 60_000 });
    assert(servers.length > 0, 'the session sent no ICE servers');
    const urls = servers.flatMap(s => s.urls);
    assert(urls.every(u => u.includes(`localhost:${instance.turnPort}`)), `the TURN URLs do not follow the page host: ${urls}`);
    await openApp('com.android.settings');
    const frames = await framesWhileScrolling(page, 5000);
    ctl('force-stop', 'com.android.settings');
    assert(frames >= 20, `only ${frames} frames in 5 s of scrolling through localhost`);
    const stats = await videoStats(page);
    const pair = await page.evaluate(async () => {
      const reports = [...(await window.__peers.at(-1).getStats()).values()];
      const chosen = reports.find(r => r.type === 'candidate-pair' && r.nominated);
      const local = reports.find(r => r.id === chosen?.localCandidateId);
      const peer = reports.find(r => r.id === chosen?.remoteCandidateId);
      return `${local?.candidateType}/${local?.relayProtocol ?? local?.protocol} to ${peer?.candidateType}/${peer?.protocol}`;
    });
    info(`opened as ${remote}: TURN URLs ${urls.join(' and ')}; ${stats.frameWidth}x${stats.frameHeight}, ${frames} frames in 5 s of scrolling; candidate pair ${pair}`);
    assert(pair.startsWith('relay/'), `the media does not go through the relay: ${pair}`);
  } finally {
    await context.close();
  }
});

/** TURN credentials from a session that signs in with the bearer token, and the stream it asked for. */
function turnCredentials() {
  const socket = new WebSocket(`${instance.url.replace(/^http/, 'ws')}/api/v1/session`, { headers: { Authorization: `Bearer ${ctl('token')}` } });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('no ICE servers from the session within 10 s')), 10_000);
    socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'stream', action: 'start' })));
    socket.addEventListener('error', () => reject(new Error('the bearer-token session failed')));
    socket.addEventListener('message', ({ data }) => {
      const m = JSON.parse(data);
      if (m.type === 'error') reject(new Error(`${m.code}: ${m.message}`));
      if (m.type === 'stream' && m.state === 'configured') {
        clearTimeout(timer);
        resolve({ ...m.iceServers[0], close: () => { socket.send(JSON.stringify({ type: 'stream', action: 'stop' })); setTimeout(() => socket.close(), 300); } });
      }
    });
  });
}

step('TURN relays to the container and refuses every other peer, and checks credentials', async () => {
  const session = await turnCredentials();
  try {
    const { username, credential } = session;
    const gateway = execFileSync('ip', ['-4', 'route', 'show', 'default'], { encoding: 'utf8' }).match(/via (\S+)/)?.[1];
    // Under pasta the container has the host's LAN address: that address is the container's own and is allowed.
    const peers = ['1.1.1.1', '8.8.8.8', '192.0.2.77', '127.0.0.1', '10.0.2.2', '169.254.1.2', '224.0.0.1', ...(gateway ? [gateway] : [])];
    const client = { host: '127.0.0.1', port: instance.turnPort, username, credential };
    for (const transport of ['udp', 'tcp']) {
      const result = await probe({ ...client, transport, peers: [...peers] });
      assert(result.allocate === 200 && result.relayed, `the TURN allocation over ${transport} failed: ${JSON.stringify(result)}`);
      const refused = Object.entries(result.permissions).filter(([, code]) => code !== 403);
      assert(refused.length === 0, `TURN over ${transport} did not answer 403 for: ${JSON.stringify(refused)}`);
      const own = await probe({ ...client, transport, peers: [result.relayed] });
      assert(own.permissions[result.relayed] === 200, `the container's own address ${result.relayed} is refused over ${transport}: ${JSON.stringify(own)}`);
      info(`over ${transport}: relay at ${result.relayed}; permission for that address: 200; for ${peers.join(', ')}: 403`);
    }
    const bad = await probe({ ...client, credential: 'wrong' });
    assert(bad.allocate === 401, `a wrong TURN credential got ${bad.allocate}`);
    const expired = await probe({ ...client, username: `${Math.floor(Date.now() / 1000) - 3600}:${crypto.randomBytes(4).toString('hex')}` });
    assert(expired.allocate === 401, `an expired TURN user name got ${expired.allocate}`);
    info('a wrong credential and an expired user name: 401');
  } finally {
    session.close();
  }
});

step('no stream is left behind after all the reconnects of the run', async () => {
  // The run started dozens of streams: the emulator and the relay must hold none of them once nobody watches.
  const baseline = measurements.qemu_descriptors_without_viewer;
  assert(baseline > 0, 'the CPU step did not measure the descriptors of qemu without a viewer (run it first)');
  const page = S.page;
  await page.goto('about:blank');
  await until('no viewer', () => status().viewers === 0, { timeout: 15_000, every: 500 });
  // The emulator lets go of a closed stream 15-17 s after its viewer left; one whose browser was killed, after 40 s. The relay
  // frees what a browser that was closed without a goodbye left within a minute (max-allocate-lifetime).
  await until('the emulator and the relay to release the streams', () => qemuDescriptors() <= baseline + 3 && relaySockets() === 0, { timeout: 150_000, every: 3000 })
    .catch(error => {
      throw new Error(`${error.message}: qemu holds ${qemuDescriptors()} descriptors (${baseline} without a viewer), the relay has ${relaySockets()} ports open`);
    });
  info(`qemu holds ${qemuDescriptors()} descriptors (${baseline} when the run had no viewer), the relay has ${relaySockets()} ports open`);
  await page.goto(`${instance.url}/`);
  await waitLive(page, { timeout: 60_000 });
});
