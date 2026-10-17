// Draws the state of the dashboard into the page. Nothing here decides
// anything: main.js owns the state and calls render() after each change.

import {
  formatMs,
  formatNumber,
  formatRate,
  formatSize,
  formatTime,
  headline,
  overlayFor,
  pillFor,
  socketLabel,
  streamLabel,
} from './format.js';

const DEVICE_FACTS = [
  ['viewers', 'Viewers'],
  ['screen', 'Screen'],
  ['api', 'Android'],
  ['gpu', 'GPU'],
  ['avd', 'AVD'],
  ['version', 'Version'],
];

const CONNECTION_FACTS = [
  ['session', 'Session'],
  ['stream', 'Video'],
  ['problem', 'Last problem'],
  ['ice', 'ICE'],
  ['codec', 'Codec'],
  ['size', 'Resolution'],
  ['fps', 'Frame rate'],
  ['rate', 'Bit rate'],
  ['lost', 'Packets lost'],
  ['rtt', 'Round trip'],
  ['decoded', 'Frames decoded'],
  ['latency', 'Input to frame (est.)'],
];

const $ = id => document.getElementById(id);

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

function factRows(list, facts) {
  const cells = {};
  for (const [key, label] of facts) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.dataset.fact = key;
    list.append(dt, dd);
    cells[key] = dd;
  }
  return cells;
}

export function createView() {
  const el = {
    device: $('device'),
    screen: $('screen'),
    poster: $('poster'),
    overlay: $('overlay'),
    spinner: $('overlay-spinner'),
    overlayTitle: $('overlay-title'),
    overlayDetail: $('overlay-detail'),
    overlayStart: $('overlay-start'),
    pill: $('video-pill'),
    toolbar: $('toolbar'),
    keyboard: $('keyboard-toggle'),
    badge: $('panel-badge'),
    dot: $('state-dot'),
    state: $('state-text'),
    start: $('start'),
    stop: $('stop'),
    restart: $('restart'),
    reconnect: $('reconnect'),
    actionError: $('action-error'),
    failure: $('failure'),
    failureKind: $('failure-kind'),
    failureMessage: $('failure-message'),
    failureLog: $('failure-log'),
  };
  const device = factRows($('device-facts'), DEVICE_FACTS);
  const connection = factRows($('connection-facts'), CONNECTION_FACTS);

  function renderStatus(link) {
    const { status } = link;
    const { label, tone } = headline(link);
    setText(el.state, label);
    el.dot.dataset.tone = el.badge.dataset.tone = tone;
    document.title = `Android appliance: ${label}`;
    setText(device.viewers, status ? String(status.viewers) : '-');
    setText(device.screen, formatSize(link.device.width, link.device.height));
    setText(device.api, status?.api_level ? `API ${status.api_level}` : '-');
    setText(device.gpu, status?.gpu ?? '-');
    setText(device.avd, status?.avd ?? '-');
    setText(device.version, link.version ?? '-');

    const error = status?.last_error;
    el.failure.hidden = !error;
    if (error) {
      setText(el.failureKind, error.kind);
      const time = formatTime(error.time);
      setText(el.failureMessage, time ? `${error.message} (${time})` : error.message);
      setText(el.failureLog, error.log_tail || 'The log is empty.');
    }
  }

  function renderControls(link, model) {
    const state = link.socket === 'open' ? link.status?.state : null;
    const idle = !model.busy;
    el.start.disabled = !(idle && state === 'stopped');
    el.stop.disabled = !(idle && (state === 'running' || state === 'starting'));
    el.restart.disabled = !(idle && state === 'running');
    el.reconnect.disabled = link.socket === 'connecting';
    for (const button of el.toolbar.querySelectorAll('[data-button]')) button.disabled = state !== 'running';
    el.keyboard.setAttribute('aria-pressed', String(model.capture));
    setText(el.actionError, model.actionError);
    el.actionError.hidden = !model.actionError;
  }

  function renderDisplay(link, model) {
    const frame = model.frame ?? link.device;
    el.device.style.setProperty('--ratio', String(frame.width / frame.height));
    el.screen.dataset.frame = String(model.frameSeen);
    el.poster.hidden = model.frameSeen || !model.poster;

    const overlay = overlayFor(link);
    el.overlay.hidden = !overlay;
    if (overlay) {
      setText(el.overlayTitle, overlay.title);
      setText(el.overlayDetail, overlay.detail);
      el.overlayDetail.hidden = !overlay.detail;
      el.spinner.hidden = !overlay.spinner;
      el.overlayStart.hidden = !overlay.start;
      el.overlayStart.disabled = model.busy;
    }
    const pill = pillFor(link);
    el.pill.hidden = !pill;
    if (pill) setText(el.pill, pill);
  }

  function renderConnection(link, model) {
    const s = model.stats;
    setText(connection.session, socketLabel(link));
    setText(connection.stream, streamLabel(link));
    setText(connection.problem, link.streamReason);
    // The row of a problem shows only while there is one.
    connection.problem.hidden = connection.problem.previousElementSibling.hidden = !link.streamReason;
    setText(connection.ice, link.stream === 'idle' || link.stream === 'retrying' ? '-' : link.ice);
    setText(connection.codec, s?.codec ?? '-');
    setText(connection.size, formatSize(s?.width, s?.height));
    setText(connection.fps, formatNumber(s?.fps));
    setText(connection.rate, formatRate(s?.kbps));
    setText(connection.lost, formatNumber(s?.lost));
    setText(connection.rtt, formatMs(s?.rtt));
    setText(connection.decoded, formatNumber(s?.decoded));
    setText(connection.latency, formatMs(model.latency));
  }

  return {
    elements: el,
    render(link, model) {
      renderStatus(link);
      renderControls(link, model);
      renderDisplay(link, model);
      renderConnection(link, model);
    },
  };
}
