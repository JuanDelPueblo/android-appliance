// Exit status 0 when the UI tests can run; otherwise says why they cannot.
import { loadPlaywright } from './playwright.mjs';

if (loadPlaywright()) process.exit(0);
console.log(loadPlaywright.reason);
process.exit(1);
