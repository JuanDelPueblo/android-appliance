// Steps: latency, frame rate and CPU.

import {
  qemuDescriptors, assert, byText, click, ctl, drag, facts, hasText, info, inContainer, instance, median, percentile, point, sleep, status, step, ui, until,
  videoStats, waitFacts,
} from './lib.mjs';
import {
  S, measure, setClock, dashboard, goHome, openApp, toolbar,
} from './shared.mjs';

/** The wait for a quiet screen: no frame for 700 ms. */
async function quiet(page) {
  let seen = (await facts(page)).frames;
  for (;;) {
    await sleep(700);
    const now = (await facts(page)).frames;
    if (now === seen) return;
    seen = now;
  }
}

/** Start and stop a recording of frames and pointer events in the page. */
const record = page => page.evaluate(() => {
  window.__record = { frames: [], inputs: [], moves: [], arm: false };
});
const collect = page => page.evaluate(() => {
  const r = window.__record;
  window.__record = null;
  return r;
});

/**
 * For each time, the time to the first frame that arrived at or after it (the
 * page's clock, ms): to its first packet (`receive`) or to its presentation
 * (`now`, a few ms after the decode).
 */
const toFrames = (times, frames, field = 'now') => times.map(t => {
  const frame = frames.find(f => f.receive !== null && f.receive >= t);
  return frame ? frame[field] - t : null;
}).filter(v => v !== null);

const summary = samples => ({ n: samples.length, min: Math.round(Math.min(...samples)), median: Math.round(median(samples)), p95: Math.round(percentile(samples, 95)), max: Math.round(Math.max(...samples)) });

step('latency: input to decoded frame, over 20 taps and 20 drags', async () => {
  const page = S.page;
  setClock(false);
  await openApp('com.android.settings');
  await until('the Settings list', () => hasText(ui(), 'Network & internet'), { timeout: 30_000, every: 700 });
  const item = byText(ui(), 'Network & internet');
  await click(page, item.cx, item.cy);
  const up = await until('the page of Network & internet', () => byText(ui(), 'Navigate up'), { timeout: 15_000, every: 700 });
  await click(page, up.cx, up.cy);
  await until('the Settings list again', () => hasText(ui(), 'Network & internet'), { timeout: 15_000, every: 700 });

  // 20 taps that change the screen: an item opens its page, the arrow goes back. Time from the press to the first frame that arrived after it.
  await quiet(page);
  await record(page);
  const targets = [item, up];
  for (let i = 0; i < 20; i++) {
    await click(page, targets[i % 2].cx, targets[i % 2].cy);
    await sleep(500);
    await quiet(page);
  }
  const taps = await collect(page);
  const all = toFrames(taps.inputs, taps.frames);
  const items = toFrames(taps.inputs.filter((_, i) => i % 2 === 0), taps.frames);
  const arrows = toFrames(taps.inputs.filter((_, i) => i % 2 === 1), taps.frames);
  assert(all.length >= 18, `only ${all.length} of ${taps.inputs.length} taps got a frame`);
  measure('tap_to_frame_ms', { ...summary(all), list_item: summary(items), toolbar_arrow: summary(arrows) });
  info(`20 taps, press to decoded frame: median ${Math.round(median(all))} ms, p95 ${Math.round(percentile(all, 95))}, min ${Math.round(Math.min(...all))}, max ${Math.round(Math.max(...all))} (list item: median ${Math.round(median(items))}; toolbar arrow: median ${Math.round(median(arrows))})`);
  ctl('force-stop', 'com.android.settings');

  // 20 drags of a list: the finger moves a few pixels past Android's touch slop, and the list follows at once, so this is the path itself.
  await openApp('com.android.settings');
  await until('the Settings list', () => hasText(ui(), 'Network & internet'), { timeout: 30_000, every: 700 });
  await quiet(page);
  await record(page);
  const start = await point(page, 540, 1000);
  for (let i = 0; i < 20; i++) {
    const direction = i % 2 ? 1 : -1;
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    await sleep(60);
    // The step that crosses the slop is the input; the moves after it only keep the finger going.
    await page.evaluate(() => {
      window.__record.arm = true;
    });
    await page.mouse.move(start.x, start.y + direction * 12);
    await page.mouse.move(start.x, start.y + direction * 40, { steps: 6 });
    await sleep(120);
    await page.mouse.up();
    await sleep(300);
    await quiet(page);
  }
  const drags = await collect(page);
  // The move of 12 screen pixels (about 33 device pixels) is the one that starts the list.
  const dragged = toFrames(drags.moves, drags.frames);
  assert(dragged.length >= 18, `only ${dragged.length} of ${drags.moves.length} drags got a frame`);
  const arrived = toFrames(drags.moves, drags.frames, 'receive');
  measure('drag_start_to_frame_ms', summary(dragged));
  measure('drag_start_to_first_packet_ms', summary(arrived));
  info(`20 drag starts, first move to decoded frame: median ${Math.round(median(dragged))} ms, p95 ${Math.round(percentile(dragged, 95))}, min ${Math.round(Math.min(...dragged))}, max ${Math.round(Math.max(...dragged))}; to its first packet: median ${Math.round(median(arrived))} ms`);
  ctl('force-stop', 'com.android.settings');
});

step('steady frame rate while scrolling', async () => {
  const page = S.page;
  await openApp('com.android.settings');
  await until('the Settings list', () => hasText(ui(), 'Network & internet'), { timeout: 30_000, every: 700 });
  const before = await facts(page);
  const statsBefore = await videoStats(page);
  const started = Date.now();
  // Fingers move at 60 Hz for 8 s: up, then down.
  while (Date.now() - started < 8000) {
    await drag(page, { x: 540, y: 1500 }, { x: 540, y: 500 }, { ms: 700, steps: 42 });
    await drag(page, { x: 540, y: 500 }, { x: 540, y: 1500 }, { ms: 700, steps: 42 });
  }
  const seconds = (Date.now() - started) / 1000;
  const after = await facts(page);
  const stats = await videoStats(page);
  const fps = (after.frames - before.frames) / seconds;
  const per = key => ((stats[key] - statsBefore[key]) / (stats.framesDecoded - statsBefore.framesDecoded)) * 1000;
  measure('scroll_fps', Number(fps.toFixed(1)));
  measure('browser_ms_per_frame', {
    decode: Number(per('totalDecodeTime').toFixed(1)),
    jitter_buffer: Number(((stats.jitterBufferDelay - statsBefore.jitterBufferDelay) / (stats.jitterBufferEmittedCount - statsBefore.jitterBufferEmittedCount) * 1000).toFixed(1)),
    bytes_per_frame: Math.round((stats.bytesReceived - statsBefore.bytesReceived) / (stats.framesDecoded - statsBefore.framesDecoded)),
  });
  info(`${after.frames - before.frames} frames in ${seconds.toFixed(1)} s of scrolling: ${fps.toFixed(1)} fps; the panel says ${after.fps} fps, ${after.rate}, ${after.lost} packets lost, ${after.rtt}; the browser spends ${per('totalDecodeTime').toFixed(1)} ms decoding a frame`);
  assert(fps > 5, `the video shows ${fps.toFixed(1)} fps while scrolling`);
  ctl('force-stop', 'com.android.settings');
  await goHome();
});

/** CPU seconds by process name, from /proc in the container. */
function cpuTimes() {
  const code = `
import json, os
out = {}
for pid in os.listdir('/proc'):
    if pid.isdigit():
        try:
            stat = open(f'/proc/{pid}/stat').read()
            comm = stat[stat.index('(') + 1:stat.rindex(')')]
            fields = stat[stat.rindex(')') + 2:].split()
            out[comm] = out.get(comm, 0) + int(fields[11]) + int(fields[12])
        except OSError:
            pass
print(json.dumps({'ticks': out, 'hz': os.sysconf('SC_CLK_TCK')}))
`;
  return JSON.parse(inContainer('/opt/appliance/venv/bin/python', '-c', code));
}

async function cpuOver(ms, during = () => sleep(ms)) {
  const a = cpuTimes();
  const started = Date.now();
  await during();
  const seconds = (Date.now() - started) / 1000;
  const b = cpuTimes();
  const names = { emulator: /^qemu-system/, daemon: /^python/, turnserver: /^turnserver/, xvnc: /^Xvnc/, adb: /^adb/ };
  const out = {};
  for (const [label, re] of Object.entries(names)) {
    let ticks = 0;
    for (const comm of new Set([...Object.keys(a.ticks), ...Object.keys(b.ticks)])) {
      if (re.test(comm)) ticks += (b.ticks[comm] ?? 0) - (a.ticks[comm] ?? 0);
    }
    out[label] = Number(((ticks / a.hz / seconds) * 100).toFixed(1));
  }
  return out;
}

step('CPU: Android alone, one viewer on a static screen, one viewer scrolling', async () => {
  const page = S.page;
  setClock(false);
  await goHome();
  // Nobody watches: the page leaves the dashboard.
  await page.goto('about:blank');
  await until('no viewer', () => status().viewers === 0, { timeout: 15_000, every: 500 });
  await sleep(3000);
  const none = await cpuOver(20_000);
  measure('qemu_descriptors_without_viewer', qemuDescriptors());

  await page.goto(`${instance.url}/`);
  await waitFacts(page, 'a live video', f => f.state === 'Running' && f.stream === 'Live', { timeout: 60_000 });
  await sleep(5000);
  const framesBefore = (await facts(page)).frames;
  const idle = await cpuOver(20_000);
  const framesIdle = (await facts(page)).frames - framesBefore;

  await openApp('com.android.settings');
  const scrolling = await cpuOver(8000, async () => {
    const started = Date.now();
    while (Date.now() - started < 8000) {
      await drag(page, { x: 540, y: 1500 }, { x: 540, y: 500 }, { ms: 700, steps: 42 });
      await drag(page, { x: 540, y: 500 }, { x: 540, y: 1500 }, { ms: 700, steps: 42 });
    }
  });
  ctl('force-stop', 'com.android.settings');
  await goHome();
  measure('cpu_percent_of_one_core', { no_viewer: none, one_viewer_static: idle, one_viewer_scrolling: scrolling });
  measure('frames_in_20s_on_static_screen', framesIdle);
  const show = r => `emulator ${r.emulator}%, daemon ${r.daemon}%, turnserver ${r.turnserver}%, Xvnc ${r.xvnc}%, adb ${r.adb}%`;
  info(`CPU, % of one core. no viewer: ${show(none)}`);
  info(`one viewer, static screen (${framesIdle} frames in 20 s): ${show(idle)}`);
  info(`one viewer, scrolling: ${show(scrolling)}`);
  setClock(true);
});
