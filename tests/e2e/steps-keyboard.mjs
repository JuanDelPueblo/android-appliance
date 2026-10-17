// Step: the keyboard. It runs after a restart of Android on purpose: in the first minutes after the first boot of a
// new AVD typed keys are dropped and reordered (measured through the API, a few key events at a time: "jane do",
// "ne doeja", "oede jan", "doejane"), with Gboard and with no input method enabled. After a restart of the same AVD
// the text landed exactly in 14 of 14 rounds, and the raw key events in the guest (getevent) were in order in
// every round that was measured.

import { assert, byText, click, ctl, info, instance, sleep, step, ui, until } from './lib.mjs';
import { S, goHome } from './shared.mjs';

step('keyboard: ASCII, Backspace, arrows, Delete, Shift, unicode text, IME, paste', async () => {
  const page = S.page;
  const field = () => ui().find(n => n.class === 'android.widget.EditText' && n.focused === 'true');
  // Typed keys are subject to the auto-capitalization of Android: letters compare without case.
  const expect = (what, text) => until(`${what}: the field to read ${JSON.stringify(text)}`, () => field()?.text?.toLowerCase() === text.toLowerCase(), { timeout: 15_000, every: 700 }).catch(error => {
    throw new Error(`${error.message}; the field reads ${JSON.stringify(field()?.text)}`);
  });
  const form = () => {
    ctl('shell', 'am force-stop com.google.android.contacts; am start -a android.intent.action.INSERT -t vnd.android.cursor.dir/contact');
    return until('the new-contact form', () => byText(ui(), 'First name') && byText(ui(), 'Last name'), { timeout: 40_000, every: 1000 });
  };
  const focus = async name => {
    const node = byText(ui(), name);
    await click(page, node.cx, node.cy);
    await until(`the ${name} field to have the focus`, () => field(), { timeout: 15_000, every: 700 });
    assert((await page.evaluate(() => document.activeElement.id)) === 'sink', 'the keyboard focus is not in the dashboard');
  };

  // In the first minutes after the first boot of a new AVD the keyboard of Android (Gboard) drops and reorders typed keys
  // (measured: "jane do", "doejane"), whoever sends them. A probe in the Last name field has to land exactly first; the
  // form is opened again until it does. The intent that opens it is a fixture, not the appliance.
  let attempts = 0;
  for (; attempts < 12; attempts++) {
    await form().catch(form);
    await focus('Last name');
    await page.keyboard.type('warm up', { delay: 40 });
    await page.keyboard.insertText(' ü');
    await sleep(2500);
    if (field()?.text?.toLowerCase() === 'warm up ü') break;
    await sleep(8000);
  }
  assert(attempts < 12, `the keyboard of Android did not settle: the field reads ${JSON.stringify(field()?.text)}`);
  if (attempts) info(`the keyboard of Android needed ${attempts} probe${attempts > 1 ? 's' : ''} to settle after the boot`);
  await focus('First name');

  await page.keyboard.type('jane doe', { delay: 40 });
  await expect('typing', 'jane doe');
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Backspace');
  await expect('Backspace', 'jane d');
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.type('12', { delay: 40 });
  await expect('arrows', 'jane12 d');
  await page.keyboard.press('Delete');
  await expect('Delete', 'jane12d');
  await page.keyboard.press('Shift+Digit1');
  await expect('Shift', 'jane12!d');
  // Text that no key press carries goes as text, pasted exactly.
  await page.keyboard.insertText('Grüße 日本 😀');
  await expect('unicode text', 'jane12!Grüße 日本 😀d');
  await sleep(1200);
  // An IME composition delivers its result when it ends.
  const cdp = await S.context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: 'ni', selectionStart: 2, selectionEnd: 2 });
  await sleep(200);
  await cdp.send('Input.insertText', { text: '你' });
  await expect('an IME result', 'jane12!Grüße 日本 😀你d');
  await sleep(1200);
  // The paste shortcut sends the host clipboard. Control goes down first, as for a user, and Android ignores a paste key
  // while a modifier is down: the daemon lets go of Control for the paste.
  await S.context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: instance.url });
  await page.evaluate(() => navigator.clipboard.writeText('PASTED ü 🚀'));
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Control', code: 'ControlLeft', windowsVirtualKeyCode: 17, modifiers: 2 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', modifiers: 2, key: 'v', code: 'KeyV', windowsVirtualKeyCode: 86, commands: ['paste'] });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers: 2, key: 'v', code: 'KeyV', windowsVirtualKeyCode: 86 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Control', code: 'ControlLeft', windowsVirtualKeyCode: 17, modifiers: 0 });
  await expect('the host clipboard', 'jane12!Grüße 日本 😀你PASTED ü 🚀d');
  // What came as text is exact, down to the case.
  assert(field().text.includes('Grüße 日本 😀你PASTED ü 🚀'), `the pasted text is not exact: ${JSON.stringify(field().text)}`);
  await cdp.detach();
  ctl('shell', 'am force-stop com.google.android.contacts');
  await goHome();
});
