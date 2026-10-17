// Shared by the UI tests: the browser, a fake appliance and a page that has
// opened the dashboard.

import { startFakeServer } from './fake-server.mjs';
import { loadPlaywright } from './playwright.mjs';

export const playwright = loadPlaywright();
// A reason to skip the suite, or false.
export const skip = playwright ? false : loadPlaywright.reason;

// Host candidates only work between two pages when mDNS does not hide them.
export const browserArgs = ['--disable-features=WebRtcHideLocalIpsWithMdns', '--autoplay-policy=no-user-gesture-required'];

export function launch() {
  return playwright.chromium.launch({ args: browserArgs });
}

/**
 * Open the dashboard on a fake appliance. Options: `viewport`, `touch`
 * (a phone: touch screen, mobile), `scheme`, `status` (a patch of the
 * status before the page loads) and `server`.
 *
 * Returns {server, context, page, errors, at, close}. `errors` lists console
 * errors and page errors; `at(fx, fy)` is a client position at fractions of
 * the display.
 */
export async function openDashboard(browser, { viewport = { width: 1280, height: 800 }, touch = false, scheme = 'light', status = {}, server, init } = {}) {
  server ??= await startFakeServer();
  Object.assign(server.status, status);
  const context = await browser.newContext({ viewport, hasTouch: touch, isMobile: touch, colorScheme: scheme });
  if (init) await context.addInitScript(init);
  const page = await context.newPage();
  const errors = [];
  page.on('console', m => m.type() === 'error' && errors.push(m.text()));
  page.on('pageerror', e => errors.push(String(e)));
  const response = await page.goto(`${server.url}/`);
  await server.until(() => server.sessions.size > 0, 'the session socket');
  await page.waitForFunction(() => document.getElementById('state-text').textContent !== 'Connecting');
  return {
    server,
    context,
    page,
    errors,
    response,
    async box() {
      return page.locator('#video').boundingBox();
    },
    async at(fx, fy) {
      const b = await page.locator('#video').boundingBox();
      return { x: b.x + b.width * fx, y: b.y + b.height * fy };
    },
    async close() {
      await context.close();
      await server.close();
    },
  };
}

/** Whether `actual` is within `tolerance` of `expected`. */
export function near(actual, expected, tolerance = 2) {
  return Math.abs(actual - expected) <= tolerance;
}

/** Hide or show the tab for the page, as a browser does when it is switched away. */
export function setVisibility(page, hidden) {
  return page.evaluate(hidden => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}
