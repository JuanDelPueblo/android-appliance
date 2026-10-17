// The client side of the session protocol, as a state machine without I/O.
//
// Link.handle(event) updates `state` and returns effects. The caller runs
// them: send a message, set a timer, open the peer connection. The decisions
// live here and the browser calls live elsewhere, so every path (a stream that
// ends, a socket that drops, a tab that stays hidden) can be tested without
// a browser.
//
// Events:  start, socket-open, socket-close {opened}, message {message},
//          timer {name}, peer {ice, connection}, froze, visibility {visible},
//          retry.
// Effects: socket-open, socket-close, send {message}, ping, timer {name, ms},
//          cancel {name}, peer-open {config}, peer-signal {data}, peer-close,
//          check-auth.

import { backoffDelay } from './backoff.js';

export const HIDDEN_AFTER_MS = 60_000;
const PING_EVERY_MS = 10_000;
// The socket is dead when nothing arrives, not even a pong.
const SILENCE_MS = 25_000;
const NEGOTIATE_MS = 15_000;
const DISCONNECTED_MS = 4000;
// A connection that holds this long counts as repaired: the next failure
// starts the backoff over.
const STABLE_MS = 8000;

export const NATURAL_FRAME = { width: 1080, height: 1920 };

const ACTIVE = ['starting', 'negotiating', 'live'];

export class Link {
  /**
   * socket:  idle | connecting | open | closed (waiting to retry)
   * stream:  idle | requested | starting | negotiating | live | retrying
   * paused:  the tab was hidden for so long that the stream is closed
   */
  state = {
    socket: 'idle',
    socketAttempts: 0,
    status: null,
    device: NATURAL_FRAME,
    version: null,
    visible: true,
    paused: false,
    stream: 'idle',
    streamAttempts: 0,
    streamReason: '',
    ice: 'new',
    connection: 'new',
  };

  #random;
  #armed = new Set();

  constructor(random = Math.random) {
    this.#random = random;
  }

  /** Whether a video stream is wanted: Android runs, the socket is up and the tab is not long hidden. */
  get wantsStream() {
    const s = this.state;
    return s.socket === 'open' && s.status?.state === 'running' && !s.paused;
  }

  handle(event) {
    const effects = [];
    this.#on(event, effects);
    this.#reconcile(effects);
    return effects;
  }

  #timer(effects, name, ms) {
    this.#armed.add(name);
    effects.push({ do: 'timer', name, ms });
  }

  #cancel(effects, ...names) {
    for (const name of names) {
      if (this.#armed.delete(name)) effects.push({ do: 'cancel', name });
    }
  }

  #on(event, effects) {
    const s = this.state;
    switch (event.type) {
      case 'start':
        s.socket = 'connecting';
        effects.push({ do: 'socket-open' });
        break;
      case 'socket-open':
        s.socket = 'open';
        this.#timer(effects, 'ping', PING_EVERY_MS);
        this.#timer(effects, 'silence', SILENCE_MS);
        this.#timer(effects, 'socket-stable', STABLE_MS);
        break;
      case 'socket-close':
        this.#socketLost(effects, event.opened);
        break;
      case 'message':
        if (s.socket === 'open') this.#timer(effects, 'silence', SILENCE_MS);
        this.#message(event.message, effects);
        break;
      case 'timer':
        this.#armed.delete(event.name);
        this.#fired(event.name, effects);
        break;
      case 'peer':
        this.#peer(event, effects);
        break;
      case 'froze':
        if (ACTIVE.includes(s.stream)) this.#fail(effects, 'The video froze.');
        break;
      case 'visibility':
        s.visible = event.visible;
        if (event.visible) {
          this.#cancel(effects, 'hidden');
          s.paused = false;
        } else {
          this.#timer(effects, 'hidden', HIDDEN_AFTER_MS);
        }
        break;
      case 'retry':
        this.#retry(effects);
        break;
    }
  }

  #message(m, effects) {
    const s = this.state;
    switch (m.type) {
      case 'status': {
        const { type, ...status } = m;
        s.status = status;
        s.version = status.version ?? s.version;
        break;
      }
      case 'hello':
        if (m.device?.width > 0 && m.device?.height > 0) s.device = { width: m.device.width, height: m.device.height };
        s.version = m.version ?? s.version;
        break;
      case 'stream':
        this.#streamMessage(m, effects);
        break;
      case 'jsep':
        if (s.stream === 'negotiating' || s.stream === 'live') effects.push({ do: 'peer-signal', data: m.data });
        break;
      case 'error':
        // Errors of input are of no interest to the stream.
        if (['requested', 'starting', 'negotiating'].includes(s.stream)) this.#fail(effects, m.message || 'The server refused the stream.');
        break;
    }
  }

  #streamMessage(m, effects) {
    const s = this.state;
    if (m.state === 'starting') {
      if (s.stream === 'requested') s.stream = 'starting';
    } else if (m.state === 'configured') {
      if (s.stream === 'requested' || s.stream === 'starting') {
        s.stream = 'negotiating';
        effects.push({ do: 'peer-open', config: { iceServers: m.iceServers ?? [], iceTransportPolicy: m.iceTransportPolicy } });
      }
    } else if (m.state === 'ended') {
      // An ended before the "starting" of the new stream answers our own
      // earlier stop: it is not about this stream.
      if (ACTIVE.includes(s.stream)) this.#fail(effects, m.reason ? `The stream ended: ${m.reason}` : 'The stream ended.');
    }
  }

  #fired(name, effects) {
    const s = this.state;
    switch (name) {
      case 'ping':
        effects.push({ do: 'ping' });
        this.#timer(effects, 'ping', PING_EVERY_MS);
        break;
      case 'silence':
        // A dead connection may take minutes to report its end.
        effects.push({ do: 'socket-close' });
        this.#socketLost(effects, true);
        break;
      case 'socket-stable':
        s.socketAttempts = 0;
        break;
      case 'socket-retry':
        if (s.socket === 'closed') {
          s.socket = 'connecting';
          effects.push({ do: 'socket-open' });
        }
        break;
      case 'stream-retry':
        if (s.stream === 'retrying') s.stream = 'idle';
        break;
      case 'negotiate':
        this.#fail(effects, 'The video connection timed out.');
        break;
      case 'ice-disconnected':
        this.#fail(effects, 'The video connection was lost.');
        break;
      case 'stable':
        s.streamAttempts = 0;
        break;
      case 'hidden':
        s.paused = true;
        break;
    }
  }

  #peer({ ice, connection }, effects) {
    const s = this.state;
    s.ice = ice;
    s.connection = connection;
    if (s.stream !== 'negotiating' && s.stream !== 'live') return;
    if (connection === 'connected') {
      s.stream = 'live';
      s.streamReason = '';
      this.#cancel(effects, 'negotiate', 'ice-disconnected');
      this.#timer(effects, 'stable', STABLE_MS);
    } else if (connection === 'disconnected') {
      // It often recovers by itself.
      if (!this.#armed.has('ice-disconnected')) this.#timer(effects, 'ice-disconnected', DISCONNECTED_MS);
    } else if (connection === 'failed') {
      this.#fail(effects, 'The video connection failed.');
    }
  }

  #socketLost(effects, opened) {
    const s = this.state;
    s.socket = 'closed';
    // What the last socket said is stale: a new stream waits for a new status.
    s.status = null;
    this.#endStream(effects);
    this.#cancel(effects, 'ping', 'silence', 'socket-stable');
    this.#timer(effects, 'socket-retry', backoffDelay(s.socketAttempts++, this.#random));
    // A socket that never opened may be one that the server refuses: ask
    // whether the session is over.
    if (!opened) effects.push({ do: 'check-auth' });
  }

  #retry(effects) {
    const s = this.state;
    if (s.socket === 'closed') {
      this.#cancel(effects, 'socket-retry');
      s.socketAttempts = 0;
      s.socket = 'connecting';
      effects.push({ do: 'socket-open' });
    } else if (s.socket === 'open' && s.stream !== 'idle') {
      // reconcile starts a new stream; the start replaces the old one.
      this.#endStream(effects);
    }
  }

  /** Drop the stream here, without telling the server. */
  #endStream(effects) {
    const s = this.state;
    if (s.stream !== 'idle') effects.push({ do: 'peer-close' });
    this.#cancel(effects, 'negotiate', 'ice-disconnected', 'stable', 'stream-retry');
    s.stream = 'idle';
    s.streamAttempts = 0;
    s.ice = s.connection = 'new';
  }

  #fail(effects, reason) {
    const s = this.state;
    effects.push({ do: 'peer-close' });
    this.#cancel(effects, 'negotiate', 'ice-disconnected', 'stable');
    s.streamReason = reason;
    s.stream = 'retrying';
    s.ice = s.connection = 'new';
    this.#timer(effects, 'stream-retry', backoffDelay(s.streamAttempts++, this.#random));
  }

  #reconcile(effects) {
    const s = this.state;
    if (!this.wantsStream) {
      if (s.stream === 'idle') return;
      // Tell the server only while it can hear us and Android runs: it ends
      // the streams itself when Android stops.
      if (s.socket === 'open' && s.status?.state === 'running') effects.push({ do: 'send', message: { type: 'stream', action: 'stop' } });
      this.#endStream(effects);
    } else if (s.stream === 'idle') {
      s.stream = 'requested';
      effects.push({ do: 'send', message: { type: 'stream', action: 'start' } });
      this.#timer(effects, 'negotiate', NEGOTIATE_MS);
    }
  }
}
