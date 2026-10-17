import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatMs, formatRate, headline, overlayFor, pillFor, stateLabel, streamLabel, socketLabel } from '../../../web/js/format.js';
import { LatencyMeter, pickStats, summarize } from '../../../web/js/stats.js';

const reports = (bytes, frames, extra = {}) => [
  { id: 'IT01V', type: 'inbound-rtp', kind: 'video', codecId: 'COT', bytesReceived: bytes, framesDecoded: frames, frameWidth: 1080, frameHeight: 1920, packetsLost: 2, framesDropped: 1, ...extra },
  { id: 'COT', type: 'codec', mimeType: 'video/VP8' },
  { id: 'T01', type: 'transport', selectedCandidatePairId: 'CP1' },
  { id: 'CP1', type: 'candidate-pair', nominated: true, state: 'succeeded', currentRoundTripTime: 0.0123 },
  { id: 'IT01A', type: 'inbound-rtp', kind: 'audio' },
];

test('the video, the codec and the pair come out of the reports', () => {
  const { video, pair, codec } = pickStats(reports(1, 1));
  assert.equal(video.id, 'IT01V');
  assert.equal(pair.id, 'CP1');
  assert.equal(codec.mimeType, 'video/VP8');
});

test('without a transport the nominated pair is used', () => {
  const list = reports(1, 1).filter(r => r.type !== 'transport');
  assert.equal(pickStats(list).pair.id, 'CP1');
});

test('rates come from two samples', () => {
  const prev = { time: 1000, ...pickStats(reports(100_000, 100)) };
  const cur = { time: 3000, ...pickStats(reports(600_000, 160)) };
  const s = summarize(prev, cur);
  assert.equal(s.fps, 30);
  assert.equal(s.kbps, 2000);
  assert.equal(s.codec, 'VP8');
  assert.equal(s.width, 1080);
  assert.equal(s.height, 1920);
  assert.equal(s.lost, 2);
  assert.equal(s.decoded, 160);
  assert.equal(s.dropped, 1);
  assert.ok(Math.abs(s.rtt - 12.3) < 1e-9);
});

test('the first sample has no rate', () => {
  const s = summarize(null, { time: 1, ...pickStats(reports(100, 5, { framesPerSecond: 24 })) });
  assert.equal(s.kbps, null);
  assert.equal(s.fps, 24);
  assert.equal(s.decoded, 5);
});

test('no video yet', () => {
  const s = summarize(null, { time: 1, ...pickStats([]) });
  assert.equal(s.codec, null);
  assert.equal(s.fps, null);
  assert.equal(s.rtt, null);
});

test('the latency meter measures the first frame after an input and reports the median', () => {
  const m = new LatencyMeter();
  assert.equal(m.median, null);
  for (const [input, frame] of [[0, 100], [1000, 1300], [2000, 2200]]) {
    m.input(input);
    m.input(input + 10);
    m.frame(frame);
  }
  assert.equal(m.median, 200);
});

test('a frame without an input, and an input that never gets a frame, make no sample', () => {
  const m = new LatencyMeter();
  m.frame(50);
  assert.equal(m.median, null);
  m.input(100);
  // 5 s later: the old input is forgotten, the new one is measured.
  m.input(5100);
  m.frame(5160);
  assert.equal(m.median, 60);
  m.input(6000);
  m.frame(9000);
  assert.equal(m.median, 60);
});

test('the meter keeps the last samples only', () => {
  const m = new LatencyMeter(3);
  for (const ms of [500, 500, 500, 10, 20, 30]) {
    m.input(0);
    m.frame(ms);
  }
  assert.equal(m.median, 20);
  m.reset();
  assert.equal(m.median, null);
});

test('state texts', () => {
  assert.equal(stateLabel(null), 'Unknown');
  assert.equal(stateLabel({ state: 'running' }), 'Running');
  assert.equal(stateLabel({ state: 'starting', phase: 'booting' }), 'Starting: booting');
  assert.equal(stateLabel({ state: 'starting', phase: 'restarting' }), 'Starting: restarting after a crash');
  assert.equal(stateLabel({ state: 'starting', phase: 'something new' }), 'Starting: something new');
  assert.equal(stateLabel({ state: 'stopping', phase: null }), 'Stopping');
});

test('the headline shows the loss of the connection before the last state', () => {
  const link = (socket, status, socketAttempts = 0) => ({ socket, status, socketAttempts });
  assert.deepEqual(headline(link('open', { state: 'running' })), { label: 'Running', tone: 'ok' });
  assert.deepEqual(headline(link('closed', null, 1)), { label: 'Disconnected', tone: 'pending' });
  assert.deepEqual(headline(link('connecting', null)), { label: 'Connecting', tone: 'pending' });
  // The retry of a socket that was lost is still a lost connection.
  assert.deepEqual(headline(link('connecting', null, 2)), { label: 'Disconnected', tone: 'pending' });
  assert.deepEqual(headline(link('open', { state: 'stopped', last_error: null })), { label: 'Stopped', tone: 'off' });
  assert.deepEqual(headline(link('open', { state: 'stopped', last_error: { kind: 'crash' } })), { label: 'Stopped', tone: 'error' });
  assert.equal(headline(link('open', { state: 'starting', phase: 'booting' })).tone, 'pending');
});

test('the overlay says what is going on, and offers Start only for a stopped device', () => {
  const link = (socket, status, socketAttempts = 0) => ({ socket, status, socketAttempts });
  assert.equal(overlayFor(link('open', { state: 'running' })), null);
  assert.deepEqual(overlayFor(link('open', { state: 'stopped', last_error: null })), {
    title: 'Android is stopped',
    detail: 'Start it to use the display.',
    spinner: false,
    start: true,
  });
  const crashed = overlayFor(link('open', { state: 'stopped', last_error: { message: 'qemu crashed' } }));
  assert.equal(crashed.title, 'Android stopped unexpectedly');
  assert.equal(crashed.detail, 'qemu crashed');
  assert.equal(crashed.start, true);
  const starting = overlayFor(link('open', { state: 'starting', phase: 'booting' }));
  assert.deepEqual([starting.title, starting.detail, starting.spinner, starting.start], ['Starting Android', 'booting', true, false]);
  assert.equal(overlayFor(link('open', { state: 'stopping' })).start, false);
  assert.equal(overlayFor(link('closed', null, 1)).title, 'Connection lost');
  assert.equal(overlayFor(link('connecting', null, 1)).title, 'Connection lost');
  assert.equal(overlayFor(link('connecting', null)).title, 'Connecting');
});

test('the note under the overlay appears only while a running device has no live video', () => {
  const running = { state: 'running' };
  assert.equal(pillFor({ socket: 'open', status: running, stream: 'live' }), null);
  assert.equal(pillFor({ socket: 'open', status: running, stream: 'negotiating' }), 'Connecting the video');
  assert.equal(pillFor({ socket: 'open', status: running, stream: 'retrying' }), 'Reconnecting the video');
  assert.equal(pillFor({ socket: 'open', status: running, stream: 'idle', paused: true }), 'Video paused while the tab is hidden');
  assert.equal(pillFor({ socket: 'open', status: { state: 'stopped' }, stream: 'idle' }), null);
  assert.equal(pillFor({ socket: 'closed', status: running, stream: 'idle' }), null);
});

test('labels of the connection', () => {
  assert.equal(socketLabel({ socket: 'open' }), 'Connected');
  assert.equal(socketLabel({ socket: 'closed', socketAttempts: 3 }), 'Reconnecting (attempt 3)');
  assert.equal(streamLabel({ stream: 'live' }), 'Live');
  assert.equal(streamLabel({ stream: 'retrying', streamAttempts: 2 }), 'Reconnecting (attempt 2)');
  assert.equal(streamLabel({ stream: 'idle', status: { state: 'stopped' }, socket: 'open' }), 'Off');
  assert.equal(streamLabel({ stream: 'idle', paused: true }), 'Paused (tab hidden)');
});

test('numbers', () => {
  assert.equal(formatRate(340.4), '340 kbit/s');
  assert.equal(formatRate(2300), '2.3 Mbit/s');
  assert.equal(formatRate(null), '-');
  assert.equal(formatMs(12.4), '12 ms');
  assert.equal(formatMs(undefined), '-');
});
