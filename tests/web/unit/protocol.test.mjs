import assert from 'node:assert/strict';
import { test } from 'node:test';
import { HIDDEN_AFTER_MS, Link } from '../../../web/js/protocol.js';

// random() = 0.5 gives the exact delays of the backoff: 500, 1000, 2000, ...
const middle = () => 0.5;

/** A link with a record of its timers and of everything it asked for. */
function harness() {
  const link = new Link(middle);
  const timers = new Map();
  const log = [];
  const api = {
    link,
    timers,
    log,
    state: link.state,
    send(event) {
      const effects = link.handle(event);
      for (const e of effects) {
        if (e.do === 'timer') timers.set(e.name, e.ms);
        if (e.do === 'cancel') timers.delete(e.name);
      }
      log.push(...effects);
      return effects;
    },
    message: m => api.send({ type: 'message', message: m }),
    fire(name) {
      assert.ok(timers.has(name), `timer ${name} is not set`);
      timers.delete(name);
      return api.send({ type: 'timer', name });
    },
    sent: effects => effects.filter(e => e.do === 'send').map(e => e.message),
    kinds: effects => effects.map(e => e.do),
  };
  return api;
}

const status = (state, extra = {}) => ({ type: 'status', state, phase: null, viewers: 0, last_error: null, ...extra });
const configured = {
  type: 'stream',
  state: 'configured',
  iceServers: [{ urls: ['turn:host:3478?transport=udp'], username: 'u', credential: 'c' }],
  iceTransportPolicy: 'relay',
};

/** Open the socket and get to a running device. */
function running() {
  const h = harness();
  h.send({ type: 'start' });
  h.send({ type: 'socket-open' });
  const effects = h.message(status('running'));
  return { h, effects };
}

/** A live stream. */
function live() {
  const { h } = running();
  h.message({ type: 'stream', state: 'starting' });
  h.message(configured);
  h.send({ type: 'peer', ice: 'connected', connection: 'connected' });
  assert.equal(h.state.stream, 'live');
  return h;
}

test('the link opens the socket first', () => {
  const h = harness();
  assert.deepEqual(h.send({ type: 'start' }), [{ do: 'socket-open' }]);
  assert.equal(h.state.socket, 'connecting');
  h.send({ type: 'socket-open' });
  assert.equal(h.state.socket, 'open');
  assert.ok(h.timers.has('ping') && h.timers.has('silence'));
});

test('a running device starts a stream, and nothing else does', () => {
  const { h, effects } = running();
  assert.deepEqual(h.sent(effects), [{ type: 'stream', action: 'start' }]);
  assert.equal(h.state.stream, 'requested');
  assert.ok(h.timers.has('negotiate'));
});

test('a stopped, starting, restarting or stopping device never gets a stream', () => {
  for (const s of [status('stopped'), status('starting', { phase: 'booting' }), status('starting', { phase: 'restarting' }), status('stopping')]) {
    const h = harness();
    h.send({ type: 'start' });
    h.send({ type: 'socket-open' });
    const effects = h.message(s);
    assert.deepEqual(h.sent(effects), [], `${s.state}/${s.phase}`);
    assert.equal(h.state.stream, 'idle');
  }
});

test('the stream uses exactly the servers and the policy that the server sent', () => {
  const { h } = running();
  h.message({ type: 'stream', state: 'starting' });
  assert.equal(h.state.stream, 'starting');
  const effects = h.message(configured);
  assert.deepEqual(effects.find(e => e.do === 'peer-open'), {
    do: 'peer-open',
    config: { iceServers: configured.iceServers, iceTransportPolicy: 'relay' },
  });
  assert.equal(h.state.stream, 'negotiating');
});

test('offers and candidates go to the peer; a connected peer makes the stream live', () => {
  const { h } = running();
  h.message({ type: 'stream', state: 'starting' });
  h.message(configured);
  const offer = { type: 'offer', sdp: 'v=0' };
  assert.deepEqual(h.message({ type: 'jsep', data: offer }), [{ do: 'timer', name: 'silence', ms: 25000 }, { do: 'peer-signal', data: offer }]);
  h.send({ type: 'peer', ice: 'checking', connection: 'connecting' });
  assert.equal(h.state.stream, 'negotiating');
  const effects = h.send({ type: 'peer', ice: 'connected', connection: 'connected' });
  assert.equal(h.state.stream, 'live');
  assert.ok(effects.some(e => e.do === 'cancel' && e.name === 'negotiate'));
  assert.ok(h.timers.has('stable'));
});

test('jsep and errors that do not belong to a stream are ignored', () => {
  const h = harness();
  h.send({ type: 'start' });
  h.send({ type: 'socket-open' });
  h.message(status('stopped'));
  assert.equal(h.kinds(h.message({ type: 'jsep', data: { candidate: 'x' } })).includes('peer-signal'), false);
  h.message({ type: 'error', code: 'not_running', message: 'Android is not running.' });
  assert.equal(h.state.stream, 'idle');
});

test('an error while the stream starts is a failure; one on a live stream is not', () => {
  const { h } = running();
  h.message({ type: 'error', code: 'stream_limit', message: 'Too many streams.' });
  assert.equal(h.state.stream, 'retrying');
  assert.equal(h.state.streamReason, 'Too many streams.');
  const live2 = live();
  live2.message({ type: 'error', code: 'not_running', message: 'An input was refused.' });
  assert.equal(live2.state.stream, 'live');
});

test('a stream that ends is restarted with a backoff that grows', () => {
  const h = live();
  const delays = [];
  for (let i = 0; i < 6; i++) {
    h.message({ type: 'stream', state: 'ended', reason: 'gone' });
    assert.equal(h.state.stream, 'retrying');
    delays.push(h.timers.get('stream-retry'));
    const effects = h.fire('stream-retry');
    assert.deepEqual(h.sent(effects), [{ type: 'stream', action: 'start' }]);
    h.message({ type: 'stream', state: 'starting' });
    h.message(configured);
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 10000]);
});

test('the reason of the last failure stays until the stream is live again', () => {
  const h = live();
  h.message({ type: 'stream', state: 'ended', reason: 'gone' });
  assert.equal(h.state.streamReason, 'The stream ended: gone');
  h.fire('stream-retry');
  assert.equal(h.state.streamReason, 'The stream ended: gone');
  h.message({ type: 'stream', state: 'starting' });
  h.message(configured);
  h.send({ type: 'peer', ice: 'connected', connection: 'connected' });
  assert.equal(h.state.streamReason, '');
});

test('a stable connection starts the backoff over', () => {
  const h = live();
  h.message({ type: 'stream', state: 'ended' });
  h.fire('stream-retry');
  h.message({ type: 'stream', state: 'starting' });
  h.message(configured);
  h.send({ type: 'peer', ice: 'connected', connection: 'connected' });
  assert.equal(h.state.streamAttempts, 1);
  h.fire('stable');
  assert.equal(h.state.streamAttempts, 0);
  h.message({ type: 'stream', state: 'ended' });
  assert.equal(h.timers.get('stream-retry'), 500);
});

test('an ended that arrives before the new "starting" answers our own stop and is ignored', () => {
  const { h } = running();
  assert.equal(h.state.stream, 'requested');
  h.message({ type: 'stream', state: 'ended', reason: 'stopped by the client' });
  assert.equal(h.state.stream, 'requested');
  h.message({ type: 'stream', state: 'starting' });
  h.message({ type: 'stream', state: 'ended', reason: 'the emulator ended it' });
  assert.equal(h.state.stream, 'retrying');
});

test('ICE failed restarts the stream; disconnected waits to see whether it recovers', () => {
  const h = live();
  h.send({ type: 'peer', ice: 'disconnected', connection: 'disconnected' });
  assert.equal(h.state.stream, 'live');
  assert.ok(h.timers.has('ice-disconnected'));
  h.send({ type: 'peer', ice: 'connected', connection: 'connected' });
  assert.equal(h.timers.has('ice-disconnected'), false);

  h.send({ type: 'peer', ice: 'disconnected', connection: 'disconnected' });
  h.fire('ice-disconnected');
  assert.equal(h.state.stream, 'retrying');
  assert.equal(h.state.streamReason, 'The video connection was lost.');

  const second = live();
  const effects = second.send({ type: 'peer', ice: 'failed', connection: 'failed' });
  assert.equal(second.state.stream, 'retrying');
  assert.ok(effects.some(e => e.do === 'peer-close'));
});

test('a stream that does not connect in time fails', () => {
  const { h } = running();
  h.message({ type: 'stream', state: 'starting' });
  h.message(configured);
  h.fire('negotiate');
  assert.equal(h.state.stream, 'retrying');
  assert.equal(h.state.streamReason, 'The video connection timed out.');
});

test('a frozen video restarts the stream, and a freeze with no stream is nothing', () => {
  const h = live();
  h.send({ type: 'froze' });
  assert.equal(h.state.stream, 'retrying');
  const idle = harness();
  idle.send({ type: 'start' });
  idle.send({ type: 'socket-open' });
  assert.deepEqual(idle.send({ type: 'froze' }), []);
});

test('the stream is dropped, without a stop, when Android stops', () => {
  const h = live();
  const effects = h.message(status('stopping'));
  assert.deepEqual(h.sent(effects), []);
  assert.ok(effects.some(e => e.do === 'peer-close'));
  assert.equal(h.state.stream, 'idle');
  assert.equal(h.state.streamAttempts, 0);
});

test('after a crash and a retried boot the stream starts at once, without a backoff', () => {
  const h = live();
  h.message(status('starting', { phase: 'restarting' }));
  h.message({ type: 'stream', state: 'ended', reason: 'Android stopped' });
  assert.equal(h.state.stream, 'idle');
  const effects = h.message(status('running'));
  assert.deepEqual(h.sent(effects), [{ type: 'stream', action: 'start' }]);
});

test('a stop of Android that races its ended message leaves no retry behind', () => {
  const h = live();
  h.message({ type: 'stream', state: 'ended', reason: 'Android stopped' });
  assert.equal(h.state.stream, 'retrying');
  h.message(status('stopped'));
  assert.equal(h.state.stream, 'idle');
  assert.equal(h.timers.has('stream-retry'), false);
});

test('a socket that closes drops the stream and retries with a backoff', () => {
  const h = live();
  const effects = h.send({ type: 'socket-close', opened: true });
  assert.equal(h.state.socket, 'closed');
  assert.equal(h.state.stream, 'idle');
  assert.ok(effects.some(e => e.do === 'peer-close'));
  assert.equal(h.kinds(effects).includes('check-auth'), false);
  assert.equal(h.timers.get('socket-retry'), 500);
  assert.equal(h.timers.has('ping'), false);
  assert.deepEqual(h.fire('socket-retry'), [{ do: 'socket-open' }]);
  assert.equal(h.state.socket, 'connecting');
});

test('the socket backoff grows while the server stays away, and starts over after a stable connection', () => {
  const h = harness();
  h.send({ type: 'start' });
  const delays = [];
  for (let i = 0; i < 7; i++) {
    h.send({ type: 'socket-close', opened: false });
    delays.push(h.timers.get('socket-retry'));
    h.fire('socket-retry');
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 10000, 10000]);
  h.send({ type: 'socket-open' });
  h.fire('socket-stable');
  h.send({ type: 'socket-close', opened: true });
  assert.equal(h.timers.get('socket-retry'), 500);
});

test('a socket that opens and closes at once does not reset the backoff', () => {
  const h = harness();
  h.send({ type: 'start' });
  const delays = [];
  for (let i = 0; i < 3; i++) {
    h.send({ type: 'socket-open' });
    h.send({ type: 'socket-close', opened: true });
    delays.push(h.timers.get('socket-retry'));
    h.fire('socket-retry');
  }
  assert.deepEqual(delays, [500, 1000, 2000]);
});

test('the status of a lost socket is forgotten, so that nothing starts on a stale state', () => {
  const h = live();
  h.send({ type: 'socket-close', opened: true });
  assert.equal(h.state.status, null);
  h.fire('socket-retry');
  const effects = h.send({ type: 'socket-open' });
  assert.deepEqual(h.sent(effects), []);
  assert.equal(h.state.stream, 'idle');
});

test('a socket that never opened asks whether the session is over', () => {
  const h = harness();
  h.send({ type: 'start' });
  assert.ok(h.kinds(h.send({ type: 'socket-close', opened: false })).includes('check-auth'));
});

test('a stream is requested again after the socket is back and the device runs', () => {
  const h = live();
  h.send({ type: 'socket-close', opened: true });
  h.fire('socket-retry');
  h.send({ type: 'socket-open' });
  const effects = h.message(status('running'));
  assert.deepEqual(h.sent(effects), [{ type: 'stream', action: 'start' }]);
});

test('a silent socket is closed and retried', () => {
  const h = live();
  const effects = h.fire('silence');
  assert.ok(h.kinds(effects).includes('socket-close'));
  assert.equal(h.state.socket, 'closed');
  assert.equal(h.state.stream, 'idle');
  assert.ok(h.timers.has('socket-retry'));
});

test('every message from the server keeps the socket alive; the ping timer pings again', () => {
  const { h } = running();
  assert.deepEqual(h.message({ type: 'pong', t: 1 }), [{ do: 'timer', name: 'silence', ms: 25000 }]);
  assert.deepEqual(h.fire('ping'), [{ do: 'ping' }, { do: 'timer', name: 'ping', ms: 10000 }]);
});

test('a tab hidden for a minute closes the stream and a visible tab brings it back', () => {
  const h = live();
  const hide = h.send({ type: 'visibility', visible: false });
  assert.deepEqual(hide, [{ do: 'timer', name: 'hidden', ms: HIDDEN_AFTER_MS }]);
  assert.equal(h.state.stream, 'live');
  const effects = h.fire('hidden');
  assert.equal(h.state.paused, true);
  assert.deepEqual(h.sent(effects), [{ type: 'stream', action: 'stop' }]);
  assert.ok(h.kinds(effects).includes('peer-close'));
  assert.equal(h.state.stream, 'idle');
  const back = h.send({ type: 'visibility', visible: true });
  assert.deepEqual(h.sent(back), [{ type: 'stream', action: 'start' }]);
  assert.equal(h.state.paused, false);
});

test('a tab that is visible again within the minute keeps its stream', () => {
  const h = live();
  h.send({ type: 'visibility', visible: false });
  const effects = h.send({ type: 'visibility', visible: true });
  assert.deepEqual(effects, [{ do: 'cancel', name: 'hidden' }]);
  assert.equal(h.state.stream, 'live');
});

test('a hidden tab keeps the socket, and a status that arrives meanwhile starts no stream', () => {
  const h = live();
  h.send({ type: 'visibility', visible: false });
  h.fire('hidden');
  const effects = h.message(status('running', { viewers: 0 }));
  assert.deepEqual(h.sent(effects), []);
  assert.equal(h.state.socket, 'open');
});

test('the reconnect button opens the socket at once when it is waiting', () => {
  const h = harness();
  h.send({ type: 'start' });
  h.send({ type: 'socket-close', opened: true });
  h.send({ type: 'socket-close', opened: true });
  const effects = h.send({ type: 'retry' });
  assert.deepEqual(effects.map(e => e.do), ['cancel', 'socket-open']);
  assert.equal(h.state.socketAttempts, 0);
});

test('the reconnect button restarts a stream that is up, and a start replaces the old one', () => {
  const h = live();
  h.message({ type: 'stream', state: 'ended' });
  assert.equal(h.state.streamAttempts, 1);
  const effects = h.send({ type: 'retry' });
  assert.deepEqual(h.sent(effects), [{ type: 'stream', action: 'start' }]);
  assert.equal(h.state.streamAttempts, 0);
  assert.equal(h.state.stream, 'requested');
});

test('hello and status update the device and the version', () => {
  const h = harness();
  h.send({ type: 'start' });
  h.send({ type: 'socket-open' });
  h.message({ type: 'hello', device: { width: 720, height: 1280 }, version: '2.0.0' });
  assert.deepEqual(h.state.device, { width: 720, height: 1280 });
  assert.equal(h.state.version, '2.0.0');
  h.message(status('stopped', { version: '2.0.1' }));
  assert.equal(h.state.version, '2.0.1');
  assert.equal(h.state.status.state, 'stopped');
  assert.equal('type' in h.state.status, false);
  h.message({ type: 'hello', device: { width: 0, height: 0 } });
  assert.deepEqual(h.state.device, { width: 720, height: 1280 });
});
