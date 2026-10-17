// Steps: reconnection, freezes, several viewers, a hidden tab, a long session.

import {
  assert, click, ctl, facts, framesIn, hasText, info, inContainer, newContext, point, scrolled, setVisibility, sleep, status, step, ui, until,
  frameLags, median, percentile, videoStats, waitFacts, waitFrames, waitLive,
} from './lib.mjs';
import {
  S, measure, DESKTOP, dashboard, framesWhileScrolling, openApp, pageNow, recovery,
} from './shared.mjs';

step('the WebSocket closes: the dashboard reconnects, session and video', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  const times = [];
  for (let i = 0; i < 3; i++) {
    await waitFrames(page, 'a steady video', { timeout: 60_000 });
    const since = await pageNow(page);
    await page.evaluate(() => window.__sockets.at(-1).close());
    const { ms } = await recovery(page, since, 'the session and the video after the WebSocket closed', 60_000);
    times.push(ms);
    // The reconnect delay doubles until a connection has held for 8 s.
    await sleep(9000);
  }
  assert((await facts(page)).marker === marker, 'the page was reloaded');
  measure('ws_close_to_live_ms', times);
  info(`WebSocket close to frames again: ${times.join(', ')} ms`);
});

step('the browser goes offline for 20 s and comes back', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  await S.context.setOffline(true);
  await sleep(20_000);
  const during = await facts(page);
  const since = await pageNow(page);
  await S.context.setOffline(false);
  const { ms } = await recovery(page, since, 'live video after the network came back', 60_000);
  assert((await facts(page)).marker === marker, 'the page was reloaded');
  measure('offline_back_to_live_ms', ms);
  info(`offline 20 s (the page showed "${during.session}", video "${during.stream}"); back to frames: ${ms} ms`);
});

step('the TURN server restarts: the video recovers', async () => {
  const page = S.page;
  await waitFrames(page, 'a steady video', { timeout: 60_000 });
  const since = await pageNow(page);
  const killed = Date.now();
  inContainer('pkill', '-9', 'turnserver');
  const stalled = await until('the video to notice the loss', async () => {
    const f = await facts(page);
    return f.stream !== 'Live' ? f : null;
  }, { timeout: 30_000, every: 100 });
  const noticed = Date.now() - killed;
  const { ms } = await recovery(page, since, 'live video after the TURN restart', 60_000);
  measure('turn_restart_to_live_ms', ms);
  info(`kill -9 of turnserver: the dashboard noticed after ${noticed} ms (${stalled.problem || stalled.stream}); frames again after ${ms} ms`);
});

step('the video stalls while the screen changes: freeze detected, recovered', async () => {
  const page = S.page;
  // A list that scrolls changes the screen far more than the clock does.
  await openApp('com.android.settings');
  await until('the Settings list', () => hasText(ui(), 'Network & internet'), { timeout: 30_000, every: 700 });
  const wheel = await point(page, 540, 1000);
  await page.mouse.move(wheel.x, wheel.y);
  await waitFrames(page, 'a steady video', { timeout: 60_000 });
  const since = await pageNow(page);
  const stopped = Date.now();
  // A stopped turnserver forwards nothing: the connection stays up, no frame arrives, and the screen keeps changing.
  inContainer('pkill', '-STOP', 'turnserver');
  let running = true;
  const scroller = (async () => {
    for (let direction = 1; running; direction = -direction) {
      await page.mouse.wheel(0, 300 * direction);
      await sleep(600);
    }
  })();
  let detected = null;
  let noticed = 0;
  try {
    detected = await until('the dashboard to notice the stalled video', async () => {
      const f = await facts(page);
      return f.stream !== 'Live' ? f : null;
    }, { timeout: 20_000, every: 100 });
    noticed = Date.now() - stopped;
    await sleep(2000);
  } finally {
    running = false;
    await scroller;
    inContainer('pkill', '-CONT', 'turnserver');
  }
  const resumed = await pageNow(page);
  const { ms } = await recovery(page, resumed, 'live video after the media path came back', 60_000);
  measure('freeze_detected_ms', noticed);
  measure('freeze_cont_to_live_ms', ms);
  info(`media path stopped while the list scrolled: noticed after ${noticed} ms ("${detected.problem}"); resumed, a decoded frame again after ${ms} ms`);
  assert(since > 0 && noticed < 8000, `the stall was noticed only after ${noticed} ms`);
  assert(ms < 20_000, `the video needed ${ms} ms to recover`);
  ctl('force-stop', 'com.android.settings');
});

step('a poster shows while the video cannot connect, and the first frame replaces it', async () => {
  const context = await newContext(S.browser, { viewport: DESKTOP });
  try {
    await context.addCookies(await S.context.cookies());
    inContainer('pkill', '-STOP', 'turnserver');
    let page;
    let shown = false;
    try {
      page = await dashboard(context);
      await until('the poster', async () => (await facts(page)).poster, { timeout: 15_000, every: 100 });
      const f = await facts(page);
      assert(!f.frame && f.state === 'Running', `the poster shows with ${JSON.stringify({ frame: f.frame, state: f.state })}`);
      shown = await page.evaluate(() => document.getElementById('poster').naturalWidth > 0);
      assert(shown, 'the poster has no picture');
      await click(page, 540, 960);
    } finally {
      inContainer('pkill', '-CONT', 'turnserver');
    }
    await waitFacts(page, 'the first frame to replace the poster', f => f.frame && !f.poster && f.stream === 'Live', { timeout: 60_000 });
    info('with the media path stopped the dashboard showed a screenshot; after it resumed, the video replaced it');
  } finally {
    await context.close();
  }
});

step('the daemon stops answering for 30 s: the dead connection is found, then the dashboard reconnects', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  await waitFrames(page, 'a steady video', { timeout: 60_000 });
  const stopped = Date.now();
  inContainer('pkill', '-STOP', '-f', 'appliance serve');
  let lost = null;
  let noticed = 0;
  try {
    lost = await until('the dashboard to find the silent connection', async () => {
      const f = await facts(page);
      return f.session !== 'Connected' ? f : null;
    }, { timeout: 45_000, every: 250 });
    noticed = Date.now() - stopped;
    await sleep(Math.max(0, 30_000 - noticed));
  } finally {
    inContainer('pkill', '-CONT', '-f', 'appliance serve');
  }
  const { ms } = await recovery(page, await pageNow(page), 'the session and the video after the daemon resumed', 60_000);
  assert((await facts(page)).marker === marker, 'the page was reloaded');
  measure('silent_server_noticed_ms', noticed);
  measure('silent_server_resume_to_live_ms', ms);
  info(`daemon stopped: the dashboard showed "${lost.session}" after ${noticed} ms; resumed, frames again after ${ms} ms`);
});

step('two viewers: both decode frames, the dashboards show 2 viewers', async () => {
  const other = await newContext(S.browser, { viewport: DESKTOP });
  try {
    await other.addCookies(await S.context.cookies());
    const second = await dashboard(other);
    await Promise.all([waitLive(S.page), waitLive(second)]);
    await until('2 viewers', async () => status().viewers === 2 && (await facts(S.page)).viewers === '2' && (await facts(second)).viewers === '2', { timeout: 15_000, every: 250 });
    // One viewer scrolls a list; both must show it.
    await openApp('com.android.settings');
    const [a, b] = await Promise.all([framesWhileScrolling(S.page, 5000), framesIn(second, 5000)]);
    assert(a >= 20 && b >= 20, `a viewer decoded too few frames in 5 s of scrolling: ${a} and ${b}`);
    info(`viewers 2; frames in 5 s while a list scrolls: ${a} and ${b}`);
    ctl('force-stop', 'com.android.settings');
  } finally {
    await other.close();
  }
  await until('1 viewer after the second left', () => status().viewers === 1, { timeout: 15_000, every: 250 });
});

step('a hidden tab closes its stream after the grace period and resumes when shown', async () => {
  const page = S.page;
  await setVisibility(page, true);
  await sleep(50_000);
  assert((await facts(page)).stream === 'Live', 'the stream closed before the grace period (60 s)');
  await until('the stream to pause', async () => (await facts(page)).stream === 'Paused (tab hidden)', { timeout: 25_000, every: 500 });
  await until('no viewer', () => status().viewers === 0, { timeout: 10_000, every: 500 });
  await setVisibility(page, false);
  await waitLive(page, { timeout: 30_000 });
  await until('1 viewer again', () => status().viewers === 1, { timeout: 10_000, every: 500 });
});

step('soak: a long session with activity; the stream restarts only after a real stall', async () => {
  const seconds = Number(process.env.E2E_SOAK_SECONDS ?? 120);
  if (!seconds) {
    info('skipped: E2E_SOAK_SECONDS is 0');
    return;
  }
  const page = S.page;
  await openApp('com.android.settings');
  await until('the Settings list', () => hasText(ui(), 'Network & internet'), { timeout: 30_000, every: 700 });
  await waitLive(page);
  const first = await facts(page);
  const marks = (await page.evaluate(() => window.__marks.frame)).length;
  const errors = S.errors.length;
  const statsBefore = await videoStats(page);
  const startedAt = await pageNow(page);
  const wheel = await point(page, 540, 1000);
  await page.mouse.move(wheel.x, wheel.y);
  const end = Date.now() + seconds * 1000;
  let samples = 0;
  let notLive = 0;
  const problems = [];
  let direction = 1;
  let next = 0;
  // A scroll every 5 s, a look at the dashboard twice a second.
  while (Date.now() < end) {
    if (Date.now() >= next) {
      await page.mouse.wheel(0, 300 * direction);
      direction = -direction;
      next = Date.now() + 5000;
    }
    const f = await facts(page);
    samples++;
    if (f.stream !== 'Live' || f.sockets !== 1 || f.state !== 'Running') {
      notLive++;
      if (f.problem && !problems.includes(f.problem)) problems.push(f.problem);
    }
    await sleep(500);
  }
  const last = await facts(page);
  const stats = await videoStats(page);
  const restarts = (await page.evaluate(() => window.__marks.frame)).length - marks;
  const lags = await frameLags(page, startedAt);
  const lag = lags.length ? { n: lags.length, median: Math.round(median(lags)), p95: Math.round(percentile(lags, 95)), max: Math.round(Math.max(...lags)) } : { n: 0 };
  measure('video_lag_after_a_screen_change_ms', lag);
  info(`after a change of the screen with no frame in that second, the next frame came after: ${JSON.stringify(lag)} ms (the dashboard waits 3 s)`);
  // A busy host delays frames by seconds now and then, and then the dashboard is right to restart the stream: it waits 3 s for
  // a frame after the screen changed. So every restart needs a stall of 3 s behind it, and nothing else may restart the stream.
  const stalls = lags.filter(l => l >= 3000).length;
  measure('stream_restarts_in_the_soak', { restarts, stalls_of_3s: stalls });
  assert(restarts <= stalls, `the stream restarted ${restarts} times in ${seconds} s with only ${stalls} stalls of 3 s (${problems.join('; ') || 'no reason shown'}; lags ${JSON.stringify(lag)})`);
  assert(restarts === 0 || problems.every(p => p === 'The video froze.'), `a restart without a freeze: ${problems.join('; ')}`);
  assert(notLive === 0, `the video was not live in ${notLive} of ${samples} looks`);
  assert(last.marker === first.marker, 'the page was reloaded');
  assert(S.errors.length === errors, `console errors: ${S.errors.slice(errors).join(' | ')}`);
  assert(stats.framesDecoded > statsBefore.framesDecoded + seconds / 4, `only ${stats.framesDecoded - statsBefore.framesDecoded} frames in ${seconds} s`);
  measure('soak', { seconds, frames: stats.framesDecoded - statsBefore.framesDecoded, packets_lost: stats.packetsLost, restarts });
  info(`${seconds} s with a scroll every 5 s: ${restarts} restarts (${stalls} stalls of 3 s), ${stats.framesDecoded - statsBefore.framesDecoded} frames (the screen is static between the scrolls), ${stats.packetsLost} packets lost`);
  ctl('force-stop', 'com.android.settings');
});
