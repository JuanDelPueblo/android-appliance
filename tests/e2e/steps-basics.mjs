// Steps: sign in, a stopped Android, the first Start.
// (tests/e2e/webrtc.mjs registers them in order.)

import {
  assert, ctl, facts, info, instance, newContext, sleep, status, step, waitFacts,
} from './lib.mjs';
import {
  S, measure, DESKTOP, setClock, openPage, dashboard, pageNow,
} from './shared.mjs';

async function sessionRefused() {
  // No cookie, no token: the daemon closes the socket before it opens.
  const socket = new WebSocket(`${instance.url.replace(/^http/, 'ws')}/api/v1/session`);
  return new Promise(resolve => {
    socket.addEventListener('open', () => {
      socket.close();
      resolve(false);
    });
    socket.addEventListener('close', () => resolve(true));
    socket.addEventListener('error', () => resolve(true));
    setTimeout(() => resolve(false), 5000);
  });
}

step('login: form, wrong token, one-time link, cookie, sign out', async () => {
  const token = ctl('token');
  const guest = await newContext(S.browser, { viewport: DESKTOP });
  const page = await guest.newPage();
  const first = await page.goto(`${instance.url}/`);
  assert(page.url() === `${instance.url}/login`, `an anonymous visit is not sent to /login: ${page.url()}`);
  const headers = first.headers();
  assert(headers['content-security-policy']?.startsWith("default-src 'self'"), `the login page has no strict CSP: ${headers['content-security-policy']}`);
  assert(headers['referrer-policy'] === 'same-origin', `unexpected Referrer-Policy: ${headers['referrer-policy']}`);
  assert(await sessionRefused(), 'an anonymous WebSocket session was accepted');

  await page.fill('#token', 'not-the-token');
  const [wrong] = await Promise.all([page.waitForResponse(r => r.url().endsWith('/login') && r.request().method() === 'POST'), page.click('button[type=submit]')]);
  assert(wrong.status() === 401, `a wrong token got status ${wrong.status()}`);
  assert((await page.locator('.error').textContent()) === 'Wrong token.', 'the login page does not say "Wrong token."');

  // A browser form post: it has to carry the page's own Origin (see Referrer-Policy).
  await page.fill('#token', token);
  await page.click('button[type=submit]');
  await page.waitForURL(`${instance.url}/`, { timeout: 10_000 });
  await waitFacts(page, 'the dashboard', f => f.state !== 'Connecting', { timeout: 20_000 });
  const cookie = (await guest.cookies()).find(c => c.name === 'appliance_session');
  assert(cookie?.httpOnly && cookie.sameSite === 'Strict', `the session cookie is not HttpOnly and SameSite=Strict: ${JSON.stringify(cookie)}`);
  assert((await page.evaluate(() => fetch('/api/v1/status').then(r => r.status))) === 200, 'the cookie does not open the API');

  await page.click('.signout button');
  await page.waitForURL(`${instance.url}/login`, { timeout: 10_000 });
  assert(!(await guest.cookies()).some(c => c.name === 'appliance_session' && c.value), 'sign out left the session cookie');
  await page.goto(`${instance.url}/`);
  assert(page.url() === `${instance.url}/login`, 'after sign out the dashboard still opens');
  await guest.close();

  // The one-time link: it opens the dashboard once.
  const link = ctl('dashboard');
  S.context = await newContext(S.browser, { viewport: DESKTOP });
  S.page = await openPage(S.context, link);
  await S.page.waitForURL(`${instance.url}/`, { timeout: 10_000 });
  await waitFacts(S.page, 'the dashboard', f => f.state !== 'Connecting', { timeout: 20_000 });
  const reuse = await newContext(S.browser, { viewport: DESKTOP });
  const again = await reuse.newPage();
  await again.goto(link);
  assert((await again.locator('.error').textContent()).includes('invalid or expired'), 'a login link worked twice');
  await reuse.close();
  info('form login, wrong token, one-time link (single use), HttpOnly SameSite=Strict cookie, sign out: ok');
});

step('a stopped Android: Start is offered, nothing starts by itself', async () => {
  const page = S.page;
  await waitFacts(page, 'a stopped Android', f => f.state === 'Stopped' && f.overlay === 'Android is stopped');
  assert(await page.locator('#overlay-start').isVisible(), 'the overlay has no Start button');
  assert(await page.locator('#start').isEnabled(), 'Start is disabled');
  assert(!(await page.locator('#stop').isEnabled()) && !(await page.locator('#restart').isEnabled()), 'Stop or Restart is enabled while stopped');
  assert((await facts(page)).stream === 'Off', 'the video is not off');
  await sleep(6000);
  assert(status().state === 'stopped', `Android started by itself: ${JSON.stringify(status())}`);
  assert(!S.requests.some(r => r.includes('/screenshot')), 'the dashboard asked for a screenshot of a stopped Android');
  assert(!S.log.some(l => l.includes('"stream"') && l.includes('start')), 'the dashboard asked for a stream while Android is stopped');
});

step('Start from the dashboard: phases, poster, first decoded frame', async () => {
  const page = S.page;
  const clicked = await page.evaluate(() => performance.now());
  await page.click('#start');
  await waitFacts(page, 'Android to run', f => f.state === 'Running', { timeout: 240_000, every: 100 });
  await waitFacts(page, 'a decoded frame', f => f.frame && f.stream === 'Live', { timeout: 30_000, every: 50 });
  const marks = await page.evaluate(() => window.__marks);
  const phases = [...new Set(marks.states.map(([, text]) => text))];
  assert(phases.some(p => p.startsWith('Starting')), `the dashboard showed no starting phase: ${phases}`);
  const running = marks.states.find(([, text]) => text === 'Running')[0];
  const boot = (running - clicked) / 1000;
  const frame = marks.frame[0] - running;
  const poster = marks.poster.length ? marks.poster[0] - running : null;
  // The poster is asked for as soon as Android runs. A first frame that comes before the screenshot makes the page drop the
  // request, so only an answer that did come has to be a JPEG.
  const asked = S.requests.filter(r => r.includes('/api/v1/screenshot'));
  assert(asked.length > 0 && asked.every(r => r.includes('passive=1')), `the poster was not asked for passively: ${JSON.stringify(asked)}`);
  const answers = S.responses.filter(r => r.url.includes('/api/v1/screenshot'));
  assert(answers.every(r => r.status === 200 && r.type === 'image/jpeg'), `a poster answer is not a JPEG: ${JSON.stringify(answers)}`);
  measure('boot_to_running_s', boot);
  measure('running_to_first_frame_ms', Math.round(frame));
  measure('running_to_poster_ms', poster === null ? null : Math.round(poster));
  info(`phases: ${phases.join(' > ')}`);
  info(`click to Running ${boot.toFixed(1)} s; first decoded frame ${Math.round(frame)} ms after Running; ${poster === null ? 'the first frame came before the poster' : `poster shown ${Math.round(poster)} ms after Running`}`);
  assert(status().viewers === 1, `viewers is not 1: ${status().viewers}`);

  // A new stream on a screen that does not change.
  setClock(false);
  await sleep(2500);
  const t = await pageNow(page);
  await page.click('#reconnect');
  const again = await waitFacts(page, 'the first frame of a new stream on a static screen', f => f.frame && f.stream === 'Live', { timeout: 15_000, every: 25 });
  const at = (await page.evaluate(() => window.__marks.frame)).find(m => m > t) - t;
  measure('static_screen_first_frame_ms', Math.round(at));
  info(`first decoded frame of a new stream on a static screen: ${Math.round(at)} ms after Reconnect (ICE ${again.ice}, ${again.codec} ${again.size})`);
  ctl('shell', 'settings put global power_button_long_press 1');
  setClock(true);
});
