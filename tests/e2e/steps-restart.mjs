// Step: Restart. It comes before the keyboard step on purpose: Android is calm only after its first
// boot (see steps-keyboard.mjs).

import { assert, facts, info, step } from './lib.mjs';
import { S, confirmed, measure, pageNow, recovery } from './shared.mjs';

step('Restart from the dashboard: every phase, then live video by itself', async () => {
  const page = S.page;
  const marker = (await facts(page)).marker;
  const since = await pageNow(page);
  await confirmed(page, '#restart');
  const { ms, phases } = await recovery(page, since, 'live video after Restart', 240_000);
  assert(phases.includes('Stopping') && phases.some(p => p.startsWith('Starting')) && phases.at(-1) === 'Running', `the dashboard did not show the restart: ${phases}`);
  assert((await facts(page)).marker === marker, 'the page was reloaded');
  measure('restart_button_to_live_s', Number((ms / 1000).toFixed(1)));
  info(`Restart to live video: ${(ms / 1000).toFixed(1)} s (${phases.join(' > ')})`);
});
