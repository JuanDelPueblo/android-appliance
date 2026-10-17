// Helpers of tests/e2e/webrtc.mjs: the instance under test, androidctl, the
// UI dump of Android, the dashboard page, and the step runner.

import { execFile, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);

export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function required(name) {
  const value = process.env[name];
  if (!value) {
    console.error(`webrtc.mjs: ${name} is not set (tests/e2e/webrtc.sh sets it)`);
    process.exit(2);
  }
  return value;
}

/** The instance under test; tests/e2e/webrtc.sh sets the environment. */
export const instance = {
  url: required('E2E_URL').replace(/\/$/, ''),
  name: required('E2E_NAME'),
  androidctl: required('E2E_ANDROIDCTL'),
  config: required('E2E_CONFIG'),
  socket: required('E2E_SOCKET'),
  work: required('E2E_WORK'),
  turnPort: Number(required('E2E_TURN_PORT')),
  // install.sh, for the step that changes the configuration.
  install: process.env.E2E_INSTALL ?? '',
};

const ctlEnv = { ...process.env, ANDROID_APPLIANCE_CONFIG: instance.config };

/** androidctl, and its output without the final newline. It throws when androidctl fails. */
export const ctl = (...args) => execFileSync(instance.androidctl, args, { env: ctlEnv, encoding: 'utf8', timeout: 240_000 }).trim();

/** The same without blocking the event loop (a page keeps running meanwhile). */
export async function ctlAsync(...args) {
  const { stdout } = await run(instance.androidctl, args, { env: ctlEnv, encoding: 'utf8', timeout: 240_000 });
  return stdout.trim();
}

/** A command in the container of the instance. */
export const inContainer = (...args) => execFileSync('podman', ['exec', instance.name, ...args], { encoding: 'utf8', timeout: 60_000 }).trim();

/** The open file descriptors of qemu: every stream that the emulator still holds costs about 17. */
export const qemuDescriptors = () => Number(inContainer('sh', '-c', 'ls /proc/$(pgrep -f qemu-system | head -n 1)/fd | wc -l'));

/** The UDP ports of turnserver's relay range (49152-49251) that are open: one for each allocation. */
export const relaySockets = () => Number(inContainer('sh', '-c', "ss -H -lun | awk '{print $4}' | grep -cE ':(491[5-9][0-9]|49[2][0-4][0-9]|4925[01])$' || true"));

/** The status object of the daemon, over its unix socket. */
export function status() {
  const out = execFileSync('curl', ['-s', '--max-time', '10', '--unix-socket', instance.socket, 'http://localhost/api/v1/status'], { encoding: 'utf8' });
  return JSON.parse(out);
}

export class Timeout extends Error {}

/**
 * Poll `check` (sync or async) until it returns something truthy, and return
 * that. `what` names the wait in the failure message.
 */
export async function until(what, check, { timeout = 30_000, every = 250 } = {}) {
  const end = Date.now() + timeout;
  let last;
  for (;;) {
    last = await check();
    if (last) return last;
    if (Date.now() > end) throw new Timeout(`timed out after ${timeout / 1000}s waiting for ${what}`);
    await sleep(every);
  }
}

// Android: the UI dump

const entities = { quot: '"', amp: '&', lt: '<', gt: '>', apos: "'" };
const decode = text => text.replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n))).replace(/&(quot|amp|lt|gt|apos);/g, (_, e) => entities[e]);

/** The nodes of a UI dump, with their bounds as numbers. */
export function parseNodes(xml) {
  const nodes = [];
  for (const [tag] of xml.matchAll(/<node [^>]*>/g)) {
    const node = Object.fromEntries([...tag.matchAll(/([\w-]+)="([^"]*)"/g)].map(([, key, value]) => [key, decode(value)]));
    const bounds = node.bounds?.match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/);
    if (!bounds) continue;
    const [x1, y1, x2, y2] = bounds.slice(1).map(Number);
    nodes.push({ ...node, x1, y1, x2, y2, cx: Math.round((x1 + x2) / 2), cy: Math.round((y1 + y2) / 2) });
  }
  return nodes;
}

/** The nodes on the screen now. */
export const ui = () => parseNodes(ctl('ui'));

export const hasText = (nodes, text) => nodes.some(n => n.text === text || n['content-desc'] === text);
export const byText = (nodes, text) => nodes.find(n => n.text === text || n['content-desc'] === text);

/** The package and activity in front: "com.android.settings/.Settings". */
export function topActivity() {
  // grep finds no line (and androidctl fails) while Android switches activities.
  const line = ctl('shell', 'dumpsys activity activities 2>/dev/null | grep -m 1 topResumedActivity; true');
  return line.replace(/.* u0 ([^ ]+) .*/s, '$1').trim();
}

export const topPackage = () => topActivity().split('/')[0];
export const LAUNCHER = 'com.google.android.apps.nexuslauncher';

export const wakefulness = () => ctl('shell', 'dumpsys power 2>/dev/null | grep -m 1 mWakefulness=').replace(/^\s*mWakefulness=/, '');

/** The music volume of the guest. */
export function musicVolume() {
  const dump = ctl('shell', 'dumpsys audio 2>/dev/null | grep -A8 "^- STREAM_MUSIC"');
  return Number(dump.match(/streamVolume:(\d+)/)?.[1]);
}

/** The home screen shows the Phone icon; the app drawer lists Settings too. */
export function onHome() {
  const nodes = ui();
  return hasText(nodes, 'Phone') && !hasText(nodes, 'Settings') && !hasText(nodes, 'App info') && nodes.some(n => n.package === LAUNCHER);
}

/** Median of the vertical shift of the items that two dumps of a list share. */
export function scrolled(before, after, key = n => n.text) {
  const first = new Map(before.filter(n => key(n)).map(n => [key(n), n.y1]));
  const shifts = after.filter(n => key(n) && first.has(key(n))).map(n => n.y1 - first.get(key(n))).sort((a, b) => a - b);
  return shifts.length ? shifts[Math.floor(shifts.length / 2)] : null;
}

export function percentile(values, p) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor((sorted.length * p) / 100))] : null;
}

export const median = values => percentile(values, 50);

// The dashboard page

/** Runs in every page of the browser, before its own scripts. */
function instrument() {
  window.__marker = Math.random().toString(36).slice(2);
  window.__sockets = [];
  const Native = window.WebSocket;
  // The display events of the session (hash and arrival time), for the analysis of freezes.
  window.__display = [];
  window.WebSocket = class extends Native {
    constructor(...args) {
      super(...args);
      window.__sockets.push(this);
      this.addEventListener('message', ({ data }) => {
        if (typeof data === 'string' && data.startsWith('{"type":"display"')) window.__display.push([performance.now(), JSON.parse(data).hash]);
      });
    }
  };
  // The peer connections, for their statistics.
  const NativePeer = window.RTCPeerConnection;
  window.__peers = [];
  window.RTCPeerConnection = class extends NativePeer {
    constructor(...args) {
      super(...args);
      window.__peers.push(this);
    }
  };
  // Counts the frames that the video presents; a measurement also keeps
  // their times and the times of the pointer events (one clock).
  window.__frames = 0;
  window.__record = null;
  window.__marks = { states: [], poster: [], frame: [] };
  addEventListener('pointerdown', e => window.__record?.inputs.push(e.timeStamp), true);
  // A measurement arms this for the one move it wants the time of.
  addEventListener('pointermove', e => {
    if (window.__record?.arm) {
      window.__record.arm = false;
      window.__record.moves.push(e.timeStamp);
    }
  }, true);
  addEventListener('DOMContentLoaded', () => {
    const $ = id => document.getElementById(id);
    const video = $('video');
    // The login page has no video.
    if (!video) return;
    window.__frameTimes = [];
    const onFrame = (now, meta) => {
      window.__frames++;
      window.__frameTimes.push(now);
      window.__record?.frames.push({ now, receive: meta.receiveTime ?? null });
      video.requestVideoFrameCallback(onFrame);
    };
    video.requestVideoFrameCallback(onFrame);
    // The page sets these attributes at every render: note the changes only.
    const watch = (element, options, read, mark) => {
      let before = null;
      new MutationObserver(() => {
        const now = read();
        if (now !== before) mark(now);
        before = now;
      }).observe(element, options);
    };
    watch($('state-text'), { childList: true, characterData: true, subtree: true }, () => $('state-text').textContent, text => window.__marks.states.push([performance.now(), text]));
    watch($('poster'), { attributes: true, attributeFilter: ['hidden'] }, () => !$('poster').hidden, shown => shown && window.__marks.poster.push(performance.now()));
    watch($('screen'), { attributes: true, attributeFilter: ['data-frame'] }, () => $('screen').dataset.frame === 'true', seen => seen && window.__marks.frame.push(performance.now()));
  });
}

/** A browser context whose pages are instrumented. */
export async function newContext(browser, options = {}) {
  const context = await browser.newContext(options);
  await context.addInitScript(instrument);
  return context;
}

const FACTS = ['session', 'stream', 'problem', 'ice', 'codec', 'size', 'fps', 'rate', 'lost', 'rtt', 'decoded', 'latency', 'viewers', 'screen', 'api', 'gpu'];

/** What the dashboard shows now. */
export function facts(page) {
  return page.evaluate(names => {
    const out = {
      state: document.getElementById('state-text').textContent,
      overlay: document.getElementById('overlay').hidden ? '' : document.getElementById('overlay-title').textContent,
      pill: document.getElementById('video-pill').hidden ? '' : document.getElementById('video-pill').textContent,
      poster: !document.getElementById('poster').hidden,
      frame: document.getElementById('screen').dataset.frame === 'true',
      frames: window.__frames,
      sockets: window.__sockets.filter(s => s.readyState === 1).length,
      marker: window.__marker,
    };
    for (const name of names) out[name] = document.querySelector(`[data-fact="${name}"]`)?.textContent ?? '';
    return out;
  }, FACTS);
}

/** Wait until the dashboard shows what `predicate(facts)` accepts. */
export const waitFacts = (page, what, predicate, options) => until(what, async () => {
  const f = await facts(page);
  return predicate(f) ? f : null;
}, options);

/** The video is live and frames are presented. The screen has to change for that: see the seconds clock. */
export const waitLive = (page, options = { timeout: 60_000 }) => waitFacts(page, 'a live video with frames', f => f.state === 'Running' && f.stream === 'Live' && f.frame && f.sockets === 1, options);

/** The frames that the video presented within `ms`. */
export async function framesIn(page, ms) {
  const before = (await facts(page)).frames;
  await sleep(ms);
  return (await facts(page)).frames - before;
}

/** The video shows new frames again (after something broke it). */
export async function waitFrames(page, what, { timeout = 60_000, count = 3 } = {}) {
  let base = null;
  let at = 0;
  return until(what, async () => {
    const f = await facts(page);
    if (f.stream !== 'Live' || !f.frame || f.sockets !== 1) {
      base = null;
      return null;
    }
    if (base === null || f.frames < base) {
      base = f.frames;
      at = Date.now();
    }
    return f.frames - base >= count ? { ...f, waited: Date.now() - at } : null;
  }, { timeout, every: 100 });
}

/** The inbound video report of the newest peer connection, as a plain object. */
export function videoStats(page) {
  return page.evaluate(async () => {
    const peer = window.__peers.at(-1);
    const reports = peer ? [...(await peer.getStats()).values()] : [];
    const video = reports.find(r => r.type === 'inbound-rtp' && r.kind === 'video');
    return video ? { ...video } : null;
  });
}

/** The area of the video element that shows the picture, in client pixels, and the picture's size. */
async function area(page) {
  return page.evaluate(() => {
    const v = document.getElementById('video');
    const r = v.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height, vw: v.videoWidth || 1080, vh: v.videoHeight || 1920 };
  });
}

/** The client position of a device pixel (the video is letterboxed in its element). */
export async function point(page, x, y) {
  const a = await area(page);
  const scale = Math.min(a.width / a.vw, a.height / a.vh);
  return { x: a.left + (a.width - a.vw * scale) / 2 + (x + 0.5) * scale, y: a.top + (a.height - a.vh * scale) / 2 + (y + 0.5) * scale };
}

export async function click(page, x, y, options) {
  const p = await point(page, x, y);
  await page.mouse.click(p.x, p.y, options);
}

/** A drag with the mouse, in device pixels. */
export async function drag(page, from, to, { ms = 400, steps = 24 } = {}) {
  const a = await point(page, from.x, from.y);
  const b = await point(page, to.x, to.y);
  await page.mouse.move(a.x, a.y);
  await page.mouse.down();
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(a.x + ((b.x - a.x) * i) / steps, a.y + ((b.y - a.y) * i) / steps);
    await sleep(ms / steps);
  }
  await page.mouse.up();
}

export async function longPress(page, x, y, ms = 900) {
  const p = await point(page, x, y);
  await page.mouse.move(p.x, p.y);
  await page.mouse.down();
  await sleep(ms);
  await page.mouse.up();
}

/** Hide or show the tab for the page, as a browser does when it is switched away. */
export function setVisibility(page, hidden) {
  return page.evaluate(hidden => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}

/**
 * How long the video lagged behind the screen: for every second in which the display changed and no frame was decoded,
 * the time until the next frame (ms). The freeze detector of the dashboard waits 3 s for one.
 */
export async function frameLags(page, since = 0) {
  return page.evaluate(since => {
    const lags = [];
    const events = window.__display.filter(([t]) => t >= since);
    for (let i = 1; i < events.length; i++) {
      const [t, hash] = events[i];
      const [before] = events[i - 1];
      if (hash === events[i - 1][1]) continue;
      // A frame since the last event: the change was shown.
      if (window.__frameTimes.some(f => f > before && f <= t)) continue;
      const next = window.__frameTimes.find(f => f > t);
      lags.push(next === undefined ? Infinity : next - t);
    }
    return lags;
  }, since);
}

/** What the page's session socket received and sent, without the display events, SDP and candidates. */
export function logMessages(page, log) {
  page.on('websocket', socket => {
    const note = direction => ({ payload }) => {
      if (typeof payload !== 'string') return;
      let m;
      try {
        m = JSON.parse(payload);
      } catch {
        return;
      }
      if (m.type === 'display' || m.type === 'pong' || m.type === 'ping') return;
      const brief = m.type === 'jsep' ? { type: 'jsep', kind: m.data?.type ?? 'candidate' } : m.type === 'text' ? { type: 'text', length: m.text.length } : m;
      log.push(`${new Date().toISOString().slice(11, 23)} ${direction} ${JSON.stringify(brief).slice(0, 300)}`);
      if (log.length > 400) log.shift();
    };
    socket.on('framereceived', note('<'));
    socket.on('framesent', note('>'));
  });
}

// The steps

const steps = [];

/** Register a step. The steps build on each other: a failure ends the run. */
export function step(name, fn) {
  steps.push({ name, fn });
}

export const info = text => console.log(`  ${text}`);

export function assert(condition, message) {
  if (!condition) throw new Error(message);
}

export function writeWork(name, content) {
  fs.mkdirSync(instance.work, { recursive: true });
  fs.writeFileSync(path.join(instance.work, name), content);
}

/** Run the steps (those that match `only`, a regular expression of E2E_STEPS). */
export async function runSteps({ evidence }) {
  const only = process.env.E2E_STEPS ? new RegExp(process.env.E2E_STEPS, 'i') : null;
  let failed = false;
  for (const { name, fn } of steps) {
    if (only && !only.test(name)) continue;
    const started = Date.now();
    try {
      await fn();
      console.log(`PASS ${name} (${Math.round((Date.now() - started) / 1000)}s)`);
    } catch (error) {
      console.log(`FAIL ${name} (see ${instance.work})`);
      console.log(`  ${error.stack?.split('\n').slice(0, 3).join('\n  ') ?? error}`);
      failed = true;
      await evidence(name, error).catch(e => console.log(`  evidence failed: ${e.message}`));
      break;
    }
  }
  return !failed;
}
