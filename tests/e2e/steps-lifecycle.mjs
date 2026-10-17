// Steps: the lifecycle buttons, and what the dashboard does when Android crashes or the daemon restarts.

import { execFileSync } from 'node:child_process';
import {
  assert, facts, info, inContainer, instance, sleep, status, step, until, waitFacts, waitLive,
} from './lib.mjs';
import {
  S, measure, dashboard, pageNow, confirmed, recovery,
} from './shared.mjs';

step('Stop from the dashboard: stopped, Start offered, no automatic start', async () => {
  const page = S.page;
  const before = S.requests.length;
  await confirmed(page, '#stop');
  await waitFacts(page, 'Stopped', f => f.state === 'Stopped' && f.overlay === 'Android is stopped', { timeout: 60_000 });
  assert(status().state === 'stopped', `the daemon says ${status().state}`);
  const f = await facts(page);
  assert(f.stream === 'Off' && f.frame === false, `the video is not off after Stop: ${f.stream}`);
  assert(await page.locator('#overlay-start').isVisible(), 'no Start button after Stop');
  await sleep(10_000);
  assert(status().state === 'stopped', 'Android started by itself after Stop');
  assert((await facts(page)).state === 'Stopped', 'the dashboard left Stopped by itself');
  const later = S.requests.slice(before);
  assert(!later.some(r => r.includes('/api/v1/start') || r.includes('/screenshot')), `the dashboard asked for a start or a screenshot after Stop: ${later}`);
});

step('Start from the dashboard: boots, and the video reconnects by itself', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  const clicked = await page.evaluate(() => performance.now());
  await page.click('#start');
  const live = await waitFacts(page, 'Android to run with live video', f => f.state === 'Running' && f.stream === 'Live' && f.frame, { timeout: 240_000, every: 100 });
  assert(live.marker === marker, 'the page was reloaded');
  const marks = await page.evaluate(() => window.__marks);
  const running = marks.states.findLast(([, text]) => text === 'Running')[0];
  measure('restart_click_to_running_s', (running - clicked) / 1000);
  measure('restart_running_to_live_frame_ms', Math.round(marks.frame.at(-1) - running));
  info(`click to Running ${((running - clicked) / 1000).toFixed(1)} s; decoded frame ${Math.round(marks.frame.at(-1) - running)} ms after Running; ${live.codec} ${live.size}`);
});

// A crash within 30 s after Running counts as a failed boot and is retried once: this step has to follow the Start above.
step('a crash right after the boot is retried: the video comes back without a reload', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  const since = await pageNow(page);
  inContainer('pkill', '-9', '-f', 'qemu-system');
  const { ms, phases } = await recovery(page, since, 'the video after the crash retry');
  assert(phases.some(p => p.includes('restarting after a crash')), `the dashboard did not show the retry: ${phases}`);
  assert(status().last_error === null, `a retried crash is the last error: ${JSON.stringify(status().last_error)}`);
  assert((await facts(page)).marker === marker, 'the page was reloaded');
  measure('crash_retry_to_live_s', Number((ms / 1000).toFixed(1)));
  info(`kill -9 of qemu to a live video again: ${(ms / 1000).toFixed(1)} s (${phases.join(' > ')})`);
});

// A crash later than that is reported, and Android stays stopped.
step('a crash later than 30 s after the boot: Android stopped unexpectedly, Start recovers', async () => {
  const page = S.page;
  await until('30 s since the boot', () => status().uptime_seconds > 35, { timeout: 120_000, every: 1000 });
  inContainer('pkill', '-9', '-f', 'qemu-system');
  await waitFacts(page, 'the crash to show', f => f.state === 'Stopped' && f.overlay === 'Android stopped unexpectedly', { timeout: 30_000 });
  const shown = await page.evaluate(() => ({ kind: document.getElementById('failure-kind').textContent, hidden: document.getElementById('failure').hidden }));
  assert(!shown.hidden && shown.kind === 'crash', `the failure card does not show the crash: ${JSON.stringify(shown)}`);
  await sleep(5000);
  assert(status().state === 'stopped', 'Android started again by itself after an unretried crash');
  await page.click('#overlay-start');
  await waitLive(page, { timeout: 240_000 });
  assert(status().last_error === null, 'the new boot did not clear last_error');
});

step('the daemon restarts: the dashboard reconnects, Android stays stopped until Start', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  execFileSync('systemctl', ['--user', 'restart', `${instance.name}.service`], { timeout: 180_000 });
  await until('the daemon to answer', () => {
    try {
      return status().state === 'stopped';
    } catch {
      return false;
    }
  }, { timeout: 90_000, every: 500 });
  await waitFacts(page, 'the dashboard to show Stopped on the new session', f => f.state === 'Stopped' && f.session === 'Connected' && f.overlay === 'Android is stopped', { timeout: 60_000 });
  assert((await facts(page)).marker === marker, 'the page was reloaded');
  await sleep(5000);
  assert(status().state === 'stopped', 'Android started by itself after the daemon restart');
  await page.click('#start');
  await waitLive(page, { timeout: 240_000 });
});
