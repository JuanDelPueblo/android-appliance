// webrtc.mjs: the real dashboard, in a real browser, against a real appliance.
//
// Every effect is checked against Android (UI dumps, dumpsys, getevent
// through androidctl), not against the dashboard. tests/e2e/webrtc.sh
// installs the instance and runs this file; the environment names it (see
// lib.mjs). Playwright comes from AA_PLAYWRIGHT or tests/e2e/node_modules.
//
// The steps are in the steps-*.mjs files, in the order of the imports. They
// build on each other. E2E_STEPS=<regular expression> runs only the steps
// whose names match, against an instance that earlier steps prepared.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import { loadPlaywright } from '../web/playwright.mjs';
import { assert, facts, instance, runSteps, status, step, ctl, writeWork } from './lib.mjs';
import { S, measurements } from './shared.mjs';
import './steps-basics.mjs';
import './steps-input.mjs';
import './steps-restart.mjs';
import './steps-keyboard.mjs';
import './steps-lifecycle.mjs';
import './steps-resilience.mjs';
import './steps-measure.mjs';
import './steps-remote.mjs';
import './steps-idle.mjs';
import './steps-auth.mjs';

const playwright = loadPlaywright();
if (!playwright) {
  console.error(`webrtc.mjs: ${loadPlaywright.reason}`);
  process.exit(2);
}

step('the daemon log has no traceback', async () => {
  // The journal spans the daemon restarts of the run; the daemon's own log only the last start.
  const log = process.env.E2E_SINCE
    ? execFileSync('journalctl', ['--user', '-u', `${instance.name}.service`, '--since', process.env.E2E_SINCE, '--no-pager', '-o', 'cat'], { encoding: 'utf8', maxBuffer: 1 << 28 })
    : ctl('logs', 'daemon', '-n', '100000');
  writeWork('daemon.log', log);
  const bad = log.split('\n').filter(line => /Traceback|exception|Exception/.test(line));
  assert(bad.length === 0, `the daemon log has errors: ${bad.slice(0, 3).join(' | ')}`);
});

// The run

async function evidence(name, error) {
  const work = instance.work;
  fs.mkdirSync(work, { recursive: true });
  writeWork('fail.txt', `${name}\n${error.stack ?? error}\n`);
  writeWork('ws.log', S.log.join('\n'));
  writeWork('console.log', S.errors.join('\n'));
  writeWork('requests.log', S.requests.slice(-200).join('\n'));
  try {
    writeWork('status.json', JSON.stringify(status(), null, 2));
  } catch (e) {
    writeWork('status.json', String(e));
  }
  try {
    writeWork('daemon.log', ctl('logs', 'daemon', '-n', '400'));
  } catch {
    // The daemon may be down.
  }
  if (S.page) {
    await S.page.screenshot({ path: `${work}/fail-page.png` }).catch(() => {});
    writeWork('fail-facts.json', JSON.stringify(await facts(S.page).catch(e => String(e)), null, 2));
  }
  try {
    if (status().state === 'running') {
      writeWork('fail-ui.xml', ctl('ui'));
      ctl('screenshot', `${work}/fail-android.png`);
    }
  } catch {
    // Android or the daemon may be busy or gone.
  }
  console.log(`  evidence: ${work}/fail.txt, ws.log, console.log, daemon.log, fail-page.png, fail-android.png, fail-ui.xml`);
}

S.browser = await playwright.chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
let ok = false;
try {
  ok = await runSteps({ evidence });
} finally {
  writeWork('measurements.json', JSON.stringify(measurements, null, 2));
  await S.browser.close();
}
console.log(`measurements: ${instance.work}/measurements.json`);
console.log(JSON.stringify(measurements));
process.exit(ok ? 0 : 1);
