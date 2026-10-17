// Reconnect delays: exponential from 0.5 s to 10 s. The jitter keeps many
// dashboards that lost the same server from coming back in the same instant.

export const MIN_DELAY = 500;
export const MAX_DELAY = 10_000;
const JITTER = 0.25;

/** The delay in ms before retry number `attempt` (0 for the first retry). */
export function backoffDelay(attempt, random = Math.random) {
  const base = Math.min(MAX_DELAY, MIN_DELAY * 2 ** Math.max(0, attempt));
  const spread = 1 + JITTER * (2 * random() - 1);
  return Math.round(Math.min(MAX_DELAY, Math.max(MIN_DELAY, base * spread)));
}
