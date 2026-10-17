// Step: rotating the API token ends every dashboard session.

import { assert, ctl, info, instance, step, until, waitFacts } from './lib.mjs';
import { S } from './shared.mjs';

const status = async token => (await fetch(`${instance.url}/api/v1/status`, { headers: { Authorization: `Bearer ${token}` } })).status;

step('rotating the token ends the session, sends the dashboard to the login page, and the new token signs in', async () => {
  const page = S.page;
  const old = ctl('token');
  assert((await status(old)) === 200, 'the current token does not open the API');
  const fresh = ctl('token', '--rotate');
  assert(fresh !== old && fresh.length >= 32, 'the token did not change');
  assert((await status(old)) === 401 && (await status(fresh)) === 200, 'the old token still works, or the new one does not');
  // The open session is closed; the page tries again, is refused, and goes to the login page.
  await until('the login page', () => page.url() === `${instance.url}/login`, { timeout: 40_000, every: 250 });
  await page.fill('#token', old);
  await page.click('button[type=submit]');
  await page.locator('.error').waitFor();
  assert((await page.locator('.error').textContent()) === 'Wrong token.', 'the old token signs in');
  await page.fill('#token', fresh);
  await page.click('button[type=submit]');
  await page.waitForURL(`${instance.url}/`, { timeout: 10_000 });
  await waitFacts(page, 'the dashboard with the new session', f => f.state !== 'Connecting' && f.session === 'Connected', { timeout: 30_000 });
  info('the open session was closed, the old token and cookie stopped working, the dashboard went to /login and the new token signed in');
});
