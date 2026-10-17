// The dashboard. Wires the pure link (protocol.js) to the browser: the
// WebSocket, the peer connection, timers, input and the page.

import { command, sessionExpired, toLogin } from './api.js';
import { attachButtons } from './buttons.js';
import { FreezeDetector } from './freeze.js';
import { DisplayInput } from './input.js';
import { Peer } from './peer.js';
import { Poster } from './poster.js';
import { Link } from './protocol.js';
import { openSession } from './session.js';
import { LatencyMeter, pickStats, summarize } from './stats.js';
import { createView } from './view.js';

const STATS_EVERY_MS = 1000;
const FREEZE_CHECK_MS = 500;

const link = new Link();
const view = createView();
const el = view.elements;
const video = document.getElementById('video');
const detector = new FreezeDetector();
const latency = new LatencyMeter();
const poster = new Poster(el.poster, () => update());

// A phone has no physical keyboard: its keyboard goes to Android only when the
// toolbar asks for it, so a tap on the display does not raise the on-screen one.
const capture = !matchMedia('(pointer: coarse)').matches;

// What the page shows besides the link: the decoded frame, the poster, the
// request in flight, the figures of the connection.
const model = { frame: null, frameSeen: false, busy: false, actionError: '', capture, stats: null, latency: null };

let session = null;
let peer = null;
let statsTimer = 0;
let previousStats = null;
let statusSeq = 0;
const timers = new Map();

const running = () => link.state.socket === 'open' && link.state.status?.state === 'running';
const send = message => session?.send(message) ?? false;
const now = () => performance.now();

// Link effects

const effects = {
  'socket-open': connect,
  'socket-close'() {
    session?.close();
    session = null;
  },
  send: ({ message }) => send(message),
  ping: () => send({ type: 'ping', t: Math.round(now()) }),
  timer({ name, ms }) {
    clearTimeout(timers.get(name));
    timers.set(
      name,
      setTimeout(() => {
        timers.delete(name);
        dispatch({ type: 'timer', name });
      }, ms),
    );
  },
  cancel({ name }) {
    clearTimeout(timers.get(name));
    timers.delete(name);
  },
  'peer-open': ({ config }) => openPeer(config),
  'peer-signal': ({ data }) => peer?.signal(data),
  'peer-close': closePeer,
  'check-auth': () => sessionExpired().then(expired => expired && toLogin()),
};

function dispatch(event) {
  const wasRunning = running();
  for (const effect of link.handle(event)) effects[effect.do](effect);
  // Android stopped or the socket went: whatever was held is gone.
  if (wasRunning && !running()) releaseAll();
  update();
}

function connect() {
  const own = (session = openSession({
    open: () => session === own && dispatch({ type: 'socket-open' }),
    message: m => session === own && onMessage(m),
    close: opened => {
      if (session !== own) return;
      session = null;
      dispatch({ type: 'socket-close', opened });
    },
  }));
}

function onMessage(m) {
  if (m.type === 'display') {
    // Only a live stream can freeze.
    if (link.state.stream === 'live' && !document.hidden) detector.display(m.hash, now());
    if (!model.frameSeen) poster.display(m.hash);
    return;
  }
  if (m.type === 'status') statusSeq++;
  dispatch({ type: 'message', message: m });
}

// The stream

function openPeer(config) {
  closePeer();
  let own;
  try {
    own = peer = new Peer(config, video, {
      onSignal: data => send({ type: 'jsep', data }),
      onState: state => peer === own && dispatch({ type: 'peer', ...state }),
    });
  } catch {
    // The browser refused the configuration (a malformed server URL): the
    // stream fails like any other and is tried again.
    setTimeout(() => dispatch({ type: 'peer', ice: 'failed', connection: 'failed' }));
    return;
  }
  detector.reset();
  watchFrames(own);
  previousStats = null;
  statsTimer = setInterval(() => pollStats(own), STATS_EVERY_MS);
}

function closePeer() {
  peer?.close();
  peer = null;
  clearInterval(statsTimer);
  Object.assign(model, { frame: null, frameSeen: false, stats: null, latency: null });
  latency.reset();
}

// A frame is presented. Scrolling gives up to 60 of these per second, so
// only a change of the picture size draws the page again.
function watchFrames(own) {
  const onFrame = () => {
    if (peer !== own) return;
    detector.frame();
    latency.frame(now());
    const frame = { width: video.videoWidth, height: video.videoHeight };
    if (!model.frameSeen || frame.width !== model.frame?.width || frame.height !== model.frame?.height) {
      Object.assign(model, { frame, frameSeen: true });
      update();
    }
    video.requestVideoFrameCallback(onFrame);
  };
  video.requestVideoFrameCallback(onFrame);
}

async function pollStats(own) {
  const reports = await own.stats();
  if (peer !== own) return;
  const current = { time: now(), ...pickStats(reports) };
  model.stats = summarize(previousStats, current);
  model.latency = latency.median;
  previousStats = current;
  update();
}

setInterval(() => {
  if (detector.frozen(now())) {
    detector.reset();
    dispatch({ type: 'froze' });
  }
}, FREEZE_CHECK_MS);

function update() {
  // A stopped Android shows nothing of the last picture.
  if (!running()) poster.clear();
  poster.want(running() && !model.frameSeen);
  view.render(link.state, { ...model, poster: poster.loaded });
}

// Input

const frameSize = () => (video.videoWidth ? { width: video.videoWidth, height: video.videoHeight } : link.state.device);

const input = new DisplayInput({
  screen: el.screen,
  sink: document.getElementById('sink'),
  geometry: () => ({ rect: video.getBoundingClientRect(), frame: frameSize(), device: link.state.device }),
  send,
  enabled: running,
  capture,
  onInput: () => latency.input(now()),
  onCapture: on => {
    model.capture = on;
    update();
  },
});

const buttons = attachButtons(el.toolbar, { send, enabled: running, onInput: () => latency.input(now()) });

function releaseAll() {
  input.releaseAll();
  buttons.releaseAll();
}

document.getElementById('keyboard-toggle').addEventListener('click', () => input.setCapture(!input.capture));

document.addEventListener('visibilitychange', () => {
  detector.reset();
  if (document.hidden) releaseAll();
  else poster.rearm();
  dispatch({ type: 'visibility', visible: !document.hidden });
});
window.addEventListener('blur', releaseAll);
window.addEventListener('online', () => dispatch({ type: 'retry' }));
window.addEventListener('pageshow', e => e.persisted && dispatch({ type: 'retry' }));
el.reconnect.addEventListener('click', () => {
  poster.rearm();
  dispatch({ type: 'retry' });
});

// Lifecycle controls

const confirmDialog = document.getElementById('confirm');

function confirmed(title, text, ok) {
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-text').textContent = text;
  document.getElementById('confirm-ok').textContent = ok;
  // Escape closes the dialog and keeps the value of the last answer.
  confirmDialog.returnValue = '';
  return new Promise(resolve => {
    confirmDialog.addEventListener('close', () => resolve(confirmDialog.returnValue === 'confirm'), { once: true });
    confirmDialog.showModal();
  });
}

async function run(name) {
  const seen = statusSeq;
  Object.assign(model, { busy: true, actionError: '' });
  update();
  try {
    const status = await command(name);
    // The session may have reported a newer state while the request ran.
    if (seen === statusSeq) dispatch({ type: 'message', message: { type: 'status', ...status } });
  } catch (error) {
    model.actionError = error.message;
  } finally {
    model.busy = false;
    update();
  }
}

const needs = {
  stop: ['Stop Android?', 'Running apps close. Your data stays on the device.', 'Stop'],
  restart: ['Restart Android?', 'Running apps close. Your data stays on the device.', 'Restart'],
};

function lifecycle(name) {
  return async () => {
    if (needs[name] && !(await confirmed(...needs[name]))) return;
    run(name);
  };
}

el.start.addEventListener('click', lifecycle('start'));
el.overlayStart.addEventListener('click', lifecycle('start'));
el.stop.addEventListener('click', lifecycle('stop'));
el.restart.addEventListener('click', lifecycle('restart'));

// The panel is a drawer on a narrow screen.

const app = document.getElementById('app');
const panel = document.getElementById('panel');
const panelToggle = document.getElementById('panel-toggle');
const narrow = matchMedia('(max-width: 899px)');

function setDrawer(open) {
  app.dataset.drawer = open ? 'open' : 'closed';
  panelToggle.setAttribute('aria-expanded', String(open));
  // A hidden drawer keeps its buttons out of the tab order.
  panel.inert = narrow.matches && !open;
  if (open) document.getElementById('panel-close').focus();
}

panelToggle.addEventListener('click', () => setDrawer(true));
document.getElementById('panel-close').addEventListener('click', () => {
  setDrawer(false);
  panelToggle.focus();
});
document.getElementById('scrim').addEventListener('click', () => setDrawer(false));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && app.dataset.drawer === 'open' && !confirmDialog.open) {
    setDrawer(false);
    panelToggle.focus();
  }
});
narrow.addEventListener('change', () => setDrawer(false));

setDrawer(false);
update();
dispatch({ type: 'start' });
