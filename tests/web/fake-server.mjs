// A fake appliance for the dashboard tests: it serves web/ with the headers
// of the daemon, speaks the session protocol of docs/ARCHITECTURE.md without
// any media, and records what the dashboard sends. Node standard library only,
// with a minimal WebSocket (RFC 6455) of its own.

import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const webDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'web');

// The policy that the daemon sets for / and /login.
export const CSP =
  "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; " +
  "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

/** The screenshot: a home screen drawn as SVG, in the natural frame. */
function homeScreen(state) {
  const apps = ['#ea4335', '#34a853', '#4285f4', '#fbbc04', '#a142f4', '#24c1e0', '#ff7a45', '#7cb342'];
  const icons = apps
    .map((color, i) => {
      const x = 130 + (i % 4) * 230;
      const y = 900 + Math.floor(i / 4) * 280;
      return `<rect x="${x}" y="${y}" width="150" height="150" rx="40" fill="${color}"/><rect x="${x + 25}" y="${y + 195}" width="100" height="16" rx="8" fill="#ffffff" opacity=".7"/>`;
    })
    .join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920" width="1080" height="1920">
<defs><linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1"><stop offset="0" stop-color="#1b2a5e"/><stop offset=".55" stop-color="#5b3b8c"/><stop offset="1" stop-color="#d9608b"/></linearGradient></defs>
<rect width="1080" height="1920" fill="url(#g)"/>
<text x="60" y="90" font-family="sans-serif" font-size="44" fill="#fff">10:24</text>
<text x="1020" y="90" font-family="sans-serif" font-size="44" fill="#fff" text-anchor="end">${state}</text>
<text x="540" y="420" font-family="sans-serif" font-size="230" font-weight="300" fill="#fff" text-anchor="middle">10:24</text>
<text x="540" y="520" font-family="sans-serif" font-size="56" fill="#fff" opacity=".85" text-anchor="middle">Fri, Oct 16</text>
${icons}
<rect x="90" y="1640" width="900" height="130" rx="65" fill="#ffffff" opacity=".92"/>
<rect x="190" y="1688" width="420" height="34" rx="17" fill="#9aa0a6"/>
<rect x="440" y="1860" width="200" height="12" rx="6" fill="#ffffff" opacity=".8"/>
</svg>`;
}

function frame(opcode, payload) {
  const length = payload.length;
  const header =
    length < 126
      ? Buffer.from([0x80 | opcode, length])
      : length < 65536
        ? Buffer.from([0x80 | opcode, 126, length >> 8, length & 255])
        : Buffer.concat([Buffer.from([0x80 | opcode, 127]), Buffer.alloc(4), Buffer.from([length >>> 24, (length >> 16) & 255, (length >> 8) & 255, length & 255])]);
  return Buffer.concat([header, payload]);
}

/** One WebSocket connection of the dashboard. */
class Session {
  constructor(server, socket) {
    this.server = server;
    this.socket = socket;
    this.streaming = false;
    this.closed = false;
    this.buffer = Buffer.alloc(0);
    this.fragments = [];
    socket.on('data', chunk => this.#read(chunk));
    socket.on('close', () => this.#gone());
    socket.on('error', () => socket.destroy());
  }

  send(message) {
    if (!this.closed) this.socket.write(frame(1, Buffer.from(JSON.stringify(message))));
  }

  close(code = 1001) {
    if (this.closed) return;
    this.socket.write(frame(8, Buffer.from([code >> 8, code & 255])));
    this.socket.end();
  }

  /** Cut the connection without a close frame. */
  drop() {
    this.socket.destroy();
  }

  #gone() {
    if (this.closed) return;
    this.closed = true;
    this.server.sessions.delete(this);
    if (this.streaming) this.server.endStreamOf(this, null);
  }

  #read(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const b = this.buffer;
      if (b.length < 2) return;
      let length = b[1] & 127;
      let offset = 2;
      if (length === 126) {
        if (b.length < 4) return;
        length = b.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (b.length < 10) return;
        length = Number(b.readBigUInt64BE(2));
        offset = 10;
      }
      const masked = (b[1] & 128) !== 0;
      if (b.length < offset + (masked ? 4 : 0) + length) return;
      const mask = masked ? b.subarray(offset, offset + 4) : null;
      offset += masked ? 4 : 0;
      const payload = Buffer.from(b.subarray(offset, offset + length));
      if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
      this.buffer = b.subarray(offset + length);
      const fin = (b[0] & 128) !== 0;
      const opcode = b[0] & 15;
      if (opcode === 8) {
        this.socket.end(frame(8, payload.subarray(0, 2)));
        return;
      }
      if (opcode === 9) this.socket.write(frame(10, payload));
      else if (opcode === 1 || opcode === 0) {
        this.fragments.push(payload);
        if (fin) {
          const text = Buffer.concat(this.fragments).toString();
          this.fragments = [];
          this.server.onClientMessage(this, JSON.parse(text));
        }
      }
    }
  }
}

export class FakeServer {
  constructor() {
    this.status = {
      state: 'running',
      boot_completed: true,
      idle_seconds: 0,
      idle_stop_seconds: 3600,
      uptime_seconds: 120,
      phase: null,
      gpu: 'host',
      api_level: 36,
      avd: 'android',
      viewers: 0,
      last_error: null,
      version: '2.0.0-dev',
    };
    this.device = { width: 1080, height: 1920 };
    this.iceServers = [];
    this.iceTransportPolicy = 'all';
    this.displayHash = 'a';
    this.sessions = new Set();
    // What the dashboard sent, in order, and what the tests may look at.
    this.received = [];
    this.requests = [];
    this.connections = [];
    this.violations = [];
    this.requireAuth = false;
    this.refuseSockets = false;
    // Milliseconds between the steps of a boot or a stop; null holds each step until settle().
    this.transitionMs = 30;
    this.onStreamStart = null;
    this.onJsep = null;
    this.screenshotDelayMs = 0;
    this.http = http.createServer((req, res) => this.#request(req, res));
    this.http.on('upgrade', (req, socket) => this.#upgrade(req, socket));
    this.ticker = setInterval(() => this.#tick(), 1000);
    this.pending = [];
  }

  async listen() {
    await new Promise(resolve => this.http.listen(0, '127.0.0.1', resolve));
    this.port = this.http.address().port;
    this.url = `http://127.0.0.1:${this.port}`;
    return this;
  }

  async close() {
    clearInterval(this.ticker);
    for (const session of this.sessions) session.drop();
    this.http.closeAllConnections();
    await new Promise(resolve => this.http.close(resolve));
  }

  // Looking at what happened

  messages(type) {
    return this.received.filter(m => m.type === type);
  }

  /** The messages that carry input, without the housekeeping. */
  input() {
    return this.received.filter(m => !['ping', 'jsep', 'stream'].includes(m.type));
  }

  clear() {
    this.received.length = 0;
    this.requests.length = 0;
  }

  /** Wait until `check()` is true, or fail with `what`. */
  async until(check, what, ms = 8000) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (check()) return;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw new Error(`timed out waiting for ${what}`);
  }

  // Driving the dashboard

  broadcast(message) {
    for (const session of this.sessions) session.send(message);
  }

  setStatus(patch) {
    Object.assign(this.status, patch);
    this.broadcast({ type: 'status', ...this.status });
    // The daemon ends the streams when Android is not running, after the status.
    if (patch.state && patch.state !== 'running') this.endStreams('android is not running');
  }

  get streams() {
    return [...this.sessions].filter(s => s.streaming);
  }

  sendJsep(session, data) {
    session.send({ type: 'jsep', data });
  }

  endStreamOf(session, reason) {
    session.streaming = false;
    this.setStatus({ viewers: this.streams.length });
    if (reason !== null) session.send({ type: 'stream', state: 'ended', reason });
  }

  /** The emulator ends every stream. */
  endStreams(reason = 'the emulator ended the stream') {
    for (const session of this.streams) this.endStreamOf(session, reason);
  }

  /** Complete a held boot or stop. */
  settle() {
    const next = this.pending.shift();
    if (next) next();
  }

  // The session protocol

  onClientMessage(session, message) {
    this.received.push({ ...message, at: Date.now() });
    switch (message.type) {
      case 'ping':
        session.send({ type: 'pong', t: message.t });
        break;
      case 'stream':
        if (message.action === 'start') this.#startStream(session);
        else if (session.streaming) this.endStreamOf(session, 'stopped by the client');
        break;
      case 'jsep':
        this.onJsep?.(session, message.data);
        break;
      default:
        if (this.status.state !== 'running') session.send({ type: 'error', code: 'not_running', message: 'Android is not running.' });
    }
  }

  #startStream(session) {
    if (this.status.state !== 'running') {
      session.send({ type: 'error', code: 'not_running', message: 'Android is not running.' });
      return;
    }
    if (session.streaming) this.endStreamOf(session, null);
    if (this.streams.length >= 4) {
      session.send({ type: 'error', code: 'stream_limit', message: 'Too many streams.' });
      return;
    }
    session.streaming = true;
    session.send({ type: 'stream', state: 'starting' });
    session.send({ type: 'stream', state: 'configured', iceServers: this.iceServers, iceTransportPolicy: this.iceTransportPolicy });
    this.setStatus({ viewers: this.streams.length });
    this.onStreamStart?.(session);
  }

  #tick() {
    for (const session of this.streams) session.send({ type: 'display', hash: this.displayHash, t: Date.now() });
  }

  // Lifecycle commands: a state now and the next one after a moment.

  #steps(command) {
    switch (command) {
      case 'start':
        return this.status.state === 'stopped' ? [['starting', 'booting'], ['running', null]] : [];
      case 'stop':
        return ['running', 'starting'].includes(this.status.state) ? [['stopping', null], ['stopped', null]] : [];
      case 'restart':
        return this.status.state === 'running' ? [['stopping', null], ['starting', 'booting'], ['running', null]] : [];
      default:
        return [];
    }
  }

  #lifecycle(command) {
    const [first, ...rest] = this.#steps(command);
    const apply = ([state, phase]) => {
      this.setStatus({ state, phase, boot_completed: state === 'running', last_error: null });
    };
    if (first) apply(first);
    let delay = 0;
    for (const step of rest) {
      const run = () => apply(step);
      if (this.transitionMs === null) this.pending.push(run);
      else setTimeout(run, (delay += this.transitionMs));
    }
  }

  // HTTP

  #cookieOk(req) {
    return !this.requireAuth || /(^|;\s*)appliance_session=ok/.test(req.headers.cookie ?? '');
  }

  #upgrade(req, socket) {
    const origin = req.headers.origin;
    const refuse = status => {
      socket.end(`HTTP/1.1 ${status}\r\nConnection: close\r\n\r\n`);
    };
    this.connections.push(Date.now());
    if (this.refuseSockets) return refuse('503 Service Unavailable');
    if (new URL(req.url, 'http://x').pathname !== '/api/v1/session') return refuse('404 Not Found');
    if (!this.#cookieOk(req)) return refuse('403 Forbidden');
    if (origin !== undefined && origin !== this.url) return refuse('403 Forbidden');
    const key = req.headers['sec-websocket-key'];
    const accept = crypto.createHash('sha1').update(key + GUID).digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const session = new Session(this, socket);
    this.sessions.add(session);
    session.send({ type: 'status', ...this.status });
    session.send({ type: 'hello', device: this.device, version: this.status.version });
  }

  async #request(req, res) {
    const url = new URL(req.url, this.url);
    const body = await new Promise(resolve => {
      const chunks = [];
      req.on('data', c => chunks.push(c));
      req.on('end', () => resolve(Buffer.concat(chunks).toString()));
    });
    this.requests.push({ method: req.method, path: url.pathname + url.search, body, headers: req.headers, at: Date.now() });
    const headers = {
      'x-frame-options': 'DENY',
      'x-content-type-options': 'nosniff',
      'referrer-policy': 'same-origin',
      'cache-control': 'no-store',
    };
    const send = (status, type, content, extra = {}) => {
      res.writeHead(status, { ...headers, 'content-type': type, ...extra });
      res.end(content);
    };
    const json = (status, value, extra) => send(status, 'application/json', JSON.stringify(value), extra);

    if (url.pathname.startsWith('/api/')) return this.#api(req, url, body, json, send);

    if (url.pathname === '/login') {
      if (req.method === 'POST') {
        // The daemon refuses a login that does not come from its own pages.
        if (req.headers.origin !== this.url) return send(403, 'text/html; charset=utf-8', this.loginPage('Cross-origin login refused.'));
        return send(303, 'text/plain', '', { location: '/', 'set-cookie': 'appliance_session=ok; Path=/; HttpOnly; SameSite=Strict' });
      }
      return send(200, 'text/html; charset=utf-8', this.loginPage(url.searchParams.get('error')), { 'content-security-policy': CSP });
    }
    if (url.pathname === '/logout' && req.method === 'POST') {
      if (req.headers.origin !== this.url) return send(403, 'text/plain', 'cross-origin request refused');
      return send(303, 'text/plain', '', { location: '/login', 'set-cookie': 'appliance_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict' });
    }
    if (!this.#cookieOk(req)) return send(303, 'text/plain', '', { location: '/login' });
    return this.#static(url.pathname, send);
  }

  /** The login page as the daemon renders it. */
  loginPage(error) {
    const message = error ? `<p class="error">${error}</p>` : '';
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Android appliance: sign in</title><link rel="stylesheet" href="/app.css"></head>
<body><main class="login"><h1>Android appliance</h1>${message}
<form method="post" action="/login"><label for="token">API token</label>
<input id="token" name="token" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button></form>
<p class="hint">Print the token with <code>androidctl token</code>, or open a one-time link from <code>androidctl dashboard</code>.</p></main></body></html>
`;
  }

  #static(pathname, send) {
    const file = path.normalize(path.join(webDir, pathname === '/' ? 'index.html' : pathname));
    if (!file.startsWith(webDir + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      return send(404, 'text/plain', 'not found');
    }
    const extra = pathname === '/' ? { 'content-security-policy': CSP } : {};
    return send(200, TYPES[path.extname(file)] ?? 'application/octet-stream', fs.readFileSync(file), extra);
  }

  #api(req, url, body, json, send) {
    if (!this.#cookieOk(req)) return json(401, { error: { code: 'unauthorized', message: 'authentication required' } });
    const route = `${req.method} ${url.pathname.slice('/api/v1'.length)}`;
    switch (route) {
      case 'GET /status':
        return json(200, this.status);
      case 'POST /start':
      case 'POST /stop':
      case 'POST /restart':
        this.#lifecycle(route.split('/')[1]);
        return json(200, this.status);
      case 'GET /screenshot': {
        // Without passive=1 the daemon would boot Android for this and count it as use.
        if (url.searchParams.get('passive') !== '1') this.violations.push('screenshot without passive=1');
        if (this.status.state !== 'running') {
          this.violations.push(`screenshot while ${this.status.state}`);
          return json(409, { error: { code: 'conflict', message: `android is ${this.status.state}` } });
        }
        const answer = () => send(200, 'image/svg+xml', homeScreen(this.status.state));
        return this.screenshotDelayMs ? setTimeout(answer, this.screenshotDelayMs) : answer();
      }
      default:
        return json(404, { error: { code: 'not_found', message: 'not found' } });
    }
  }
}

export async function startFakeServer() {
  return new FakeServer().listen();
}
