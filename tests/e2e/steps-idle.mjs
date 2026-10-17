// Step: the idle stop with a dashboard open.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import {
  assert, info, instance, point, sleep, status, step, until, waitFacts, waitLive,
} from './lib.mjs';
import {
  S, measure, dashboard,
} from './shared.mjs';

step('idle stop: input keeps Android running, an open dashboard without input does not', async () => {
  assert(instance.install, 'E2E_INSTALL (the path of install.sh) is not set');
  const page = S.page;
  const original = fs.readFileSync(instance.config, 'utf8');
  const apply = text => {
    fs.writeFileSync(instance.config, text);
    execFileSync(instance.install, ['--config', instance.config, '--no-build'], { stdio: 'ignore', timeout: 300_000 });
  };
  try {
    // One minute of idle time stops Android (the check runs every 30 s). The unit restarts with the new configuration.
    apply(original.replace(/^APPLIANCE_IDLE_STOP_MINUTES=.*$/m, 'APPLIANCE_IDLE_STOP_MINUTES=1'));
    await waitFacts(page, 'the dashboard on the new daemon', f => f.state === 'Stopped' && f.session === 'Connected', { timeout: 90_000 });
    assert(status().idle_stop_seconds === 60, `the idle limit is ${status().idle_stop_seconds} s`);
    await page.click('#start');
    await waitLive(page, { timeout: 240_000 });
    // Input every 20 s for 100 s: Android stays up well past the limit.
    const wheel = await point(page, 540, 1000);
    await page.mouse.move(wheel.x, wheel.y);
    let lastInput = 0;
    for (let i = 0; i < 5; i++) {
      await page.mouse.wheel(0, i % 2 ? -300 : 300);
      lastInput = Date.now();
      await sleep(20_000);
      assert(status().state === 'running', `Android stopped after ${(i + 1) * 20} s with input every 20 s`);
      assert(status().idle_seconds <= 25, `the input did not count as activity: idle_seconds ${status().idle_seconds}`);
    }
    // The dashboard stays open with its video, and nobody touches the screen.
    await until('the idle stop', () => status().state === 'stopped', { timeout: 170_000, every: 1000 });
    const idle = Math.round((Date.now() - lastInput) / 1000);
    await waitFacts(page, 'the dashboard to show the stop', f => f.state === 'Stopped' && f.overlay === 'Android is stopped', { timeout: 30_000 });
    await sleep(5000);
    assert(status().state === 'stopped', 'Android started again after the idle stop');
    assert(status().last_error === null, `an idle stop is not an error: ${JSON.stringify(status().last_error)}`);
    measure('idle_stop_after_last_input_s', idle);
    info(`input every 20 s kept Android up for 100 s; with the dashboard open and no input it stopped ${idle} s after the last input (limit 60 s, checked every 30 s)`);
  } finally {
    apply(original);
  }
});
