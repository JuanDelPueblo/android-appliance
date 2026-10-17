// Short texts for the numbers and states of the dashboard.

const PHASES = {
  preparing: 'preparing',
  gpu: 'starting the GPU',
  launching: 'launching the emulator',
  connecting: 'connecting',
  'waiting for adb': 'waiting for adb',
  booting: 'booting',
  'setting up': 'setting up',
  restarting: 'restarting after a crash',
};

/** "Running", "Starting: booting", ... for a status object. */
export function stateLabel(status) {
  if (!status) return 'Unknown';
  const label = status.state[0].toUpperCase() + status.state.slice(1);
  const phase = status.state === 'starting' && status.phase ? PHASES[status.phase] ?? status.phase : null;
  return phase ? `${label}: ${phase}` : label;
}

// A socket that was lost and is being opened again is still a lost connection.
const lost = link => link.socket === 'closed' || link.socketAttempts > 0;

/** The text and the dot of the summary: state, or the loss of the connection. */
export function headline(link) {
  const { status } = link;
  if (link.socket !== 'open') return { label: lost(link) ? 'Disconnected' : 'Connecting', tone: 'pending' };
  const tone = { running: 'ok', starting: 'pending', stopping: 'pending', stopped: 'off' }[status?.state] ?? 'off';
  return { label: stateLabel(status), tone: status?.last_error && status.state === 'stopped' ? 'error' : tone };
}

export function formatRate(kbps) {
  if (kbps === null || kbps === undefined) return '-';
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbit/s` : `${Math.round(kbps)} kbit/s`;
}

export function formatMs(ms) {
  return ms === null || ms === undefined ? '-' : `${Math.round(ms)} ms`;
}

export function formatNumber(value, digits = 0) {
  return value === null || value === undefined ? '-' : value.toFixed(digits);
}

export function formatSize(width, height) {
  return width && height ? `${width} x ${height}` : '-';
}

/** The time of a last_error, "14:02:11". The daemon sends seconds since the epoch or an ISO date. */
export function formatTime(time) {
  const date = new Date(typeof time === 'number' ? time * 1000 : time);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** Text for the session socket. */
export function socketLabel(link) {
  switch (link.socket) {
    case 'open':
      return 'Connected';
    case 'closed':
      return link.socketAttempts > 1 ? `Reconnecting (attempt ${link.socketAttempts})` : 'Reconnecting';
    default:
      return 'Connecting';
  }
}

/** Text for the video stream. */
export function streamLabel(link) {
  if (link.stream === 'idle') {
    if (link.paused) return 'Paused (tab hidden)';
    return link.status?.state === 'running' && link.socket === 'open' ? 'Starting' : 'Off';
  }
  switch (link.stream) {
    case 'live':
      return 'Live';
    case 'retrying':
      return link.streamAttempts > 1 ? `Reconnecting (attempt ${link.streamAttempts})` : 'Reconnecting';
    default:
      return 'Connecting';
  }
}

/**
 * What covers the display: a message, with a spinner or a Start button, or
 * null when the display is live. `link` is the state of the Link.
 */
export function overlayFor(link) {
  const { status, socket } = link;
  if (socket !== 'open') {
    return {
      title: lost(link) ? 'Connection lost' : 'Connecting',
      detail: lost(link) ? 'Trying again.' : '',
      spinner: true,
      start: false,
    };
  }
  switch (status?.state) {
    case 'stopped': {
      const error = status.last_error;
      return {
        title: error ? 'Android stopped unexpectedly' : 'Android is stopped',
        detail: error ? error.message : 'Start it to use the display.',
        spinner: false,
        start: true,
      };
    }
    case 'starting':
      return { title: 'Starting Android', detail: stateLabel(status).split(': ')[1] ?? '', spinner: true, start: false };
    case 'stopping':
      return { title: 'Stopping Android', detail: '', spinner: true, start: false };
    case 'running':
      return null;
    default:
      return { title: 'Waiting for the appliance', detail: '', spinner: true, start: false };
  }
}

/** The small note while the video is not live, or null. */
export function pillFor(link) {
  if (link.socket !== 'open' || link.status?.state !== 'running') return null;
  if (link.paused) return 'Video paused while the tab is hidden';
  if (link.stream === 'live') return null;
  return link.stream === 'retrying' ? 'Reconnecting the video' : 'Connecting the video';
}
