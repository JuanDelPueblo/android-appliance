// Shared by the steps of webrtc.mjs: the state of the run and the helpers that
// more than one step needs (a page of the dashboard, Android's home screen,
// the wait for a video that came back).

import {
  ctl, facts, instance, logMessages, onHome, point, sleep, status, topPackage, until, waitFacts,
} from './lib.mjs';

export const DESKTOP = { width: 1280, height: 800 };
export const PHONE = { width: 390, height: 844 };

// What the failure evidence needs, and what the steps share: the browser, the main
// context and page, what the pages said and asked.
export const S = { browser: null, context: null, page: null, log: [], errors: [], responses: [], requests: [] };
export const measurements = {};
export const measure = (name, value) => {
  measurements[name] = value;
};

// Fixtures: settings of Android that make a check possible. They are not
// part of the appliance.
//  - clock_seconds: the status bar clock ticks, so the screen changes once a
//    second and the emulator sends a frame, whatever else is going on.
//  - power_button_long_press=1: a long press of Power opens the power menu
//    (the stock setting of this image opens the Assistant).
export const setClock = on => ctl('shell', `settings put secure clock_seconds ${on ? 1 : 0}`);

/** A page of the dashboard that records what the evidence needs. */
export async function openPage(context, url) {
  const page = await context.newPage();
  page.on('console', m => m.type() === 'error' && S.errors.push(`${new Date().toISOString().slice(11, 23)} ${m.text()}`));
  page.on('pageerror', e => S.errors.push(`pageerror ${e}`));
  page.on('request', r => S.requests.push(`${r.method()} ${r.url().replace(instance.url, '')}`));
  page.on('response', r => S.responses.push({ url: r.url().replace(instance.url, ''), status: r.status(), type: r.headers()['content-type'] ?? '' }));
  logMessages(page, S.log);
  await page.goto(url);
  return page;
}

export async function dashboard(context = S.context) {
  const page = await openPage(context, `${instance.url}/`);
  await waitFacts(page, 'the dashboard to leave "Connecting"', f => f.state !== 'Connecting', { timeout: 20_000 });
  return page;
}

/** The home screen of Android, whatever was open. */
export async function goHome() {
  ctl('key', 'HOME');
  await until('the home screen', () => onHome(), { timeout: 30_000, every: 700 });
}

export async function openApp(pkg) {
  ctl('launch', pkg);
  await until(`${pkg} in front`, () => topPackage() === pkg, { timeout: 60_000, every: 700 });
}

/** The page's clock now: take it before the action that breaks something. */
export const pageNow = page => page.evaluate(() => performance.now());

export const toolbar = (page, name, options) => page.click(`[data-button="${name}"]`, options);

/** The node of a list that does not move while the page scrolls. */
export const itemKey = n => (n.text && n.class === 'android.widget.TextView' ? `${n.text}|${n['resource-id']}` : null);

/** Click a button that asks first (Stop, Restart) and confirm. */
export async function confirmed(page, button) {
  await page.click(button);
  await page.locator('#confirm').waitFor({ state: 'visible' });
  await page.click('#confirm-ok');
}

/** The frames that the video presents in `ms` while the list under the pointer scrolls. */
export async function framesWhileScrolling(page, ms) {
  const wheel = await point(page, 540, 1000);
  await page.mouse.move(wheel.x, wheel.y);
  const before = (await facts(page)).frames;
  const end = Date.now() + ms;
  for (let direction = 1; Date.now() < end; direction = -direction) {
    await page.mouse.wheel(0, 300 * direction);
    await sleep(450);
  }
  return (await facts(page)).frames - before;
}

/**
 * Wait for a new first frame after `since` (a pageNow() from before the
 * action) with the session and the video up again. Returns the time to that
 * frame in ms and the states that the dashboard showed meanwhile.
 */
export async function recovery(page, since, what, timeout = 120_000) {
  const done = await until(what, async () => {
    const marks = await page.evaluate(() => window.__marks);
    const frame = marks.frame.find(t => t > since);
    if (!frame) return null;
    const f = await facts(page);
    return f.state === 'Running' && f.stream === 'Live' && f.sockets === 1 ? { frame, marks } : null;
  }, { timeout, every: 100 });
  const phases = [...new Set(done.marks.states.filter(([t]) => t > since).map(([, text]) => text))];
  return { ms: Math.round(done.frame - since), phases };
}
