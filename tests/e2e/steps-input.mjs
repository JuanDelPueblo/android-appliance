// Steps: mouse, touch, keyboard and hardware buttons, each checked against Android.

import {
  assert, byText, parseNodes, click, ctl, ctlAsync, drag, hasText, info, instance, longPress, LAUNCHER, musicVolume, newContext, onHome, point, scrolled, sleep,
  step, topActivity, topPackage, ui, until, waitFacts, wakefulness,
} from './lib.mjs';
import {
  S, PHONE, openPage, goHome, openApp, toolbar, itemKey,
} from './shared.mjs';

step('mouse click on a launcher icon opens the app', async () => {
  await goHome();
  const phone = byText(ui(), 'Phone');
  assert(phone, 'no Phone icon on the home screen');
  await click(S.page, phone.cx, phone.cy);
  await until('the dialer in front', () => topPackage().includes('dialer'), { timeout: 20_000, every: 500 });
  await goHome();
});

step('mouse drag up opens the app drawer', async () => {
  await goHome();
  let opened = false;
  // Android can drop a gesture while it is still busy after a first boot.
  for (let attempt = 0; attempt < 3 && !opened; attempt++) {
    await drag(S.page, { x: 540, y: 1500 }, { x: 540, y: 400 });
    opened = await until('the app drawer', () => hasText(ui(), 'Settings'), { timeout: 8000, every: 700 }).then(() => true, () => false);
  }
  assert(opened, 'the drag did not open the app drawer');
  await goHome();
});

step('mouse long press opens the icon menu', async () => {
  await goHome();
  const phone = byText(ui(), 'Phone');
  await longPress(S.page, phone.cx, phone.cy, 900);
  await until('the app menu', () => hasText(ui(), 'App info'), { timeout: 15_000, every: 700 });
  ctl('key', 'BACK');
  await until('the home screen', () => onHome(), { timeout: 15_000, every: 700 });
});

step('mouse wheel scrolls a list', async () => {
  await openApp('com.android.settings');
  await until('the Settings list', () => hasText(ui(), 'Network & internet'), { timeout: 30_000, every: 700 });
  const before = ui();
  const p = await point(S.page, 540, 1100);
  await S.page.mouse.move(p.x, p.y);
  await S.page.mouse.wheel(0, 400);
  const down = await until('the list to scroll down', () => {
    const shift = scrolled(before, ui(), itemKey);
    return shift !== null && shift < -150 ? shift : null;
  }, { timeout: 15_000, every: 800 });
  const middle = ui();
  await S.page.mouse.wheel(0, -400);
  const up = await until('the list to scroll back up', () => {
    const shift = scrolled(middle, ui(), itemKey);
    return shift !== null && shift > 150 ? shift : null;
  }, { timeout: 15_000, every: 800 });
  info(`wheel +400 moved the list by ${down} px, wheel -400 by +${up} px (device pixels)`);
});

step('mouse right click is Back', async () => {
  assert(topPackage() === 'com.android.settings', `Settings is not in front: ${topPackage()}`);
  const p = await point(S.page, 540, 1100);
  await S.page.mouse.click(p.x, p.y, { button: 'right' });
  await until('the launcher in front (Settings closed)', () => topPackage() === LAUNCHER, { timeout: 15_000, every: 500 });
});

step('a rotated guest: the video stays in the natural frame and a click lands on what is drawn', async () => {
  // With the guest in landscape the picture stays 1080x1920 with its content sideways, and touch stays in that
  // frame. Android reports bounds in the landscape space: natural = (1079 - y, x) for a turn of 90 degrees.
  const page = S.page;
  const landscape = () => ctl('ui').includes('<hierarchy rotation="1"');
  try {
    await openApp('com.android.settings');
    // Android sometimes keeps the lock at the old rotation (seen after an earlier lock): free it and lock again.
    let rotated = false;
    for (let attempt = 0; attempt < 4 && !rotated; attempt++) {
      ctl('shell', 'cmd window user-rotation lock 1');
      rotated = await until('the landscape layout', landscape, { timeout: 6000, every: 1000 }).then(() => true, () => false);
      if (!rotated) {
        ctl('shell', 'cmd window user-rotation free');
        await sleep(1500);
      }
    }
    assert(rotated, 'Android did not rotate to landscape');
    const item = await until('the Settings list in landscape', () => byText(parseNodes(ctl('ui')), 'Network & internet'), { timeout: 30_000, every: 1000 });
    const size = await page.evaluate(() => ({ width: document.getElementById('video').videoWidth, height: document.getElementById('video').videoHeight }));
    assert(size.width === 1080 && size.height === 1920, `the picture is ${size.width}x${size.height}, not the natural frame`);
    await click(page, 1079 - item.cy, item.cx);
    await until('the page of Network & internet', () => topActivity().endsWith('.SubSettings'), { timeout: 15_000, every: 700 });
  } finally {
    ctl('shell', 'cmd window user-rotation free; am force-stop com.android.settings');
  }
});

step('touch: tap, swipe and two fingers on a phone-sized dashboard', async () => {
  const context = await newContext(S.browser, { viewport: PHONE, hasTouch: true, isMobile: true });
  const link = ctl('dashboard');
  const page = await openPage(context, link);
  try {
    await page.waitForURL(`${instance.url}/`, { timeout: 10_000 });
    await waitFacts(page, 'a live video on the phone', f => f.state === 'Running' && f.stream === 'Live' && f.frame, { timeout: 60_000 });
    await goHome();
    const phone = byText(ui(), 'Phone');
    const tap = await point(page, phone.cx, phone.cy);
    await page.touchscreen.tap(tap.x, tap.y);
    await until('the dialer opened by a touch tap', () => topPackage().includes('dialer'), { timeout: 20_000, every: 500 });
    await goHome();

    const cdp = await context.newCDPSession(page);
    const touch = (type, points) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: points.map(([x, y], id) => ({ x, y, id, force: 0.5 })) });
    let opened = false;
    for (let attempt = 0; attempt < 3 && !opened; attempt++) {
      const a = await point(page, 540, 1500);
      const b = await point(page, 540, 400);
      await touch('touchStart', [[a.x, a.y]]);
      for (let i = 1; i <= 20; i++) {
        await touch('touchMove', [[a.x, a.y + ((b.y - a.y) * i) / 20]]);
        await sleep(16);
      }
      await touch('touchEnd', []);
      opened = await until('the app drawer', () => hasText(ui(), 'Settings'), { timeout: 8000, every: 700 }).then(() => true, () => false);
    }
    assert(opened, 'a touch swipe did not open the app drawer');
    await goHome();

    // Two fingers: the guest's touch screen must report two contacts (slots 0 and 1).
    const events = ctlAsync('shell', 'timeout 6 getevent -l; true').catch(error => String(error));
    await sleep(1500);
    const c = await point(page, 400, 900);
    const d = await point(page, 700, 900);
    await touch('touchStart', [[c.x, c.y], [d.x, d.y]]);
    for (let i = 1; i <= 10; i++) {
      await touch('touchMove', [[c.x - i * 2, c.y], [d.x + i * 2, d.y]]);
      await sleep(16);
    }
    await touch('touchEnd', []);
    const log = await events;
    const slots = new Set([...log.matchAll(/ABS_MT_SLOT\s+([0-9a-f]+)/g)].map(m => m[1]));
    const ids = new Set([...log.matchAll(/ABS_MT_TRACKING_ID\s+([0-9a-f]+)/g)].map(m => m[1]).filter(id => id !== 'ffffffff'));
    const pressures = [...log.matchAll(/ABS_MT_PRESSURE\s+([0-9a-f]+)/g)].map(m => parseInt(m[1], 16));
    assert(slots.has('00000001') && ids.size >= 2, `no two contacts in getevent (slots ${[...slots]}, tracking ids ${[...ids]})`);
    info(`touch tap and swipe worked; two fingers: slots ${[...slots].join(',')}, ${ids.size} tracking ids, pressure ${Math.min(...pressures)}..${Math.max(...pressures)}`);
    await goHome();
  } finally {
    await context.close();
  }
});

step('hardware buttons: Home, Back, Recents, Volume, Power hold and press', async () => {
  const page = S.page;
  await openApp('com.android.settings');
  await toolbar(page, 'HOME');
  await until('Home to show the launcher', () => topPackage() === LAUNCHER && onHome(), { timeout: 20_000, every: 700 });

  await openApp('com.android.settings');
  await toolbar(page, 'BACK');
  await until('Back to leave Settings', () => topPackage() === LAUNCHER, { timeout: 20_000, every: 700 });

  await openApp('com.android.settings');
  await toolbar(page, 'APP_SWITCH');
  await until('the overview with the Settings card', () => {
    const nodes = ui();
    return nodes.some(n => n['resource-id'].endsWith(':id/overview_panel')) && hasText(nodes, 'Settings');
  }, { timeout: 20_000, every: 700 });
  await goHome();
  assert(!ui().some(n => n['resource-id'].endsWith(':id/overview_panel')), 'the overview is still open after Home');
  ctl('force-stop', 'com.android.settings');

  // The first press after a pause only brings up the volume panel, as with `androidctl key VOLUME_UP`.
  const start = musicVolume();
  for (let i = 0; i < 4; i++) {
    await toolbar(page, 'VOLUME_UP');
    await sleep(800);
  }
  const high = musicVolume();
  assert(high > start, `Volume up did not raise the music volume (${start} to ${high})`);
  for (let i = 0; i < 4; i++) {
    await toolbar(page, 'VOLUME_DOWN');
    await sleep(800);
  }
  const low = musicVolume();
  assert(low < high, `Volume down did not lower the music volume (${high} to ${low})`);
  info(`music volume ${start} > up > ${high} > down > ${low}`);

  // A hold of Power opens the power menu (power_button_long_press=1, a fixture).
  const power = await page.locator('[data-button="POWER"]').boundingBox();
  await page.mouse.move(power.x + power.width / 2, power.y + power.height / 2);
  await page.mouse.down();
  await sleep(1500);
  await page.mouse.up();
  await until('the power menu', () => {
    const nodes = ui();
    return hasText(nodes, 'Power off') && hasText(nodes, 'Restart');
  }, { timeout: 15_000, every: 700 });
  ctl('key', 'BACK');
  await until('the power menu to close', () => !hasText(ui(), 'Power off'), { timeout: 15_000, every: 700 });

  // A press of Power turns the screen off, and the next one on.
  await toolbar(page, 'POWER');
  await until('the screen to turn off', () => ['Asleep', 'Dozing'].includes(wakefulness()), { timeout: 15_000, every: 700 });
  await toolbar(page, 'POWER');
  await until('the screen to turn on', () => wakefulness() === 'Awake', { timeout: 15_000, every: 700 });
  await goHome();
});
