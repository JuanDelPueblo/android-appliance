// The REST calls of the dashboard. They are same-origin, so the session
// cookie goes with them.

const API = '/api/v1';

export function toLogin() {
  location.assign('/login');
}

async function failure(response) {
  if (response.status === 401) toLogin();
  let message = `The request failed (${response.status}).`;
  try {
    message = (await response.json()).error.message || message;
  } catch {
    // Not a JSON error: keep the generic text.
  }
  return new Error(message);
}

/** A lifecycle command. It does not wait: the session reports the progress. */
export async function command(name) {
  const response = await fetch(`${API}/${name}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ wait: false }),
  });
  if (!response.ok) throw await failure(response);
  return response.json();
}

/**
 * A JPEG of the display. It is passive: the daemon does not start Android for
 * it (a stop that races the request must not boot it again) and does not count
 * it as use. Rejects with an AbortError when `signal` aborts.
 */
export async function screenshot(signal) {
  const response = await fetch(`${API}/screenshot?format=jpeg&quality=80&max_size=960&passive=1`, { signal, cache: 'no-store' });
  if (!response.ok) throw await failure(response);
  return response.blob();
}

/** After a socket that never opened: is the session over? */
export async function sessionExpired() {
  try {
    return (await fetch(`${API}/status`, { cache: 'no-store' })).status === 401;
  } catch {
    return false;
  }
}
