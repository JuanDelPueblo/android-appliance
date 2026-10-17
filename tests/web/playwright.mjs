// Finds Playwright for the UI tests. It is not a dependency of the
// repository. Point AA_PLAYWRIGHT at a node_modules directory that holds
// `playwright` (and have its Chromium installed), or install it where Node
// resolves it (tests/e2e/package.json pins it: `npm ci` there). Without it
// the UI tests are skipped.

import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

/** The playwright module, or null with the reason on `loadPlaywright.reason`. */
export function loadPlaywright() {
  const places = [];
  if (process.env.AA_PLAYWRIGHT) places.push(path.join(path.resolve(process.env.AA_PLAYWRIGHT), 'resolve.js'));
  places.push(import.meta.url, new URL('../e2e/resolve.js', import.meta.url).href);
  for (const place of places) {
    try {
      const playwright = createRequire(place)('playwright');
      if (fs.existsSync(playwright.chromium.executablePath())) return playwright;
      loadPlaywright.reason = 'Chromium is not installed (run: npx playwright install chromium)';
    } catch {
      loadPlaywright.reason ??= 'playwright is not installed (set AA_PLAYWRIGHT to a node_modules directory that has it)';
    }
  }
  return null;
}
