// The WebSocket of the dashboard session: JSON in and out. Reconnecting is
// the job of the link, not of the socket.

const sessionUrl = () => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/session`;

/**
 * Open a session. `handlers.open()`, `handlers.message(object)` and
 * `handlers.close(opened)` are called as the socket lives; `opened` tells
 * whether the socket was ever open. A socket that the caller closes reports
 * nothing.
 */
export function openSession(handlers) {
  const socket = new WebSocket(sessionUrl());
  let opened = false;
  let done = false;
  socket.addEventListener('open', () => {
    opened = true;
    handlers.open();
  });
  socket.addEventListener('message', ({ data }) => {
    let message;
    try {
      message = JSON.parse(data);
    } catch {
      return;
    }
    if (typeof message?.type === 'string') handlers.message(message);
  });
  // An error is always followed by close.
  socket.addEventListener('close', () => {
    if (!done) handlers.close(opened);
    done = true;
  });
  return {
    /** False when the message could not be sent. */
    send(message) {
      if (socket.readyState !== WebSocket.OPEN) return false;
      socket.send(JSON.stringify(message));
      return true;
    },
    close() {
      done = true;
      socket.close();
    },
  };
}
