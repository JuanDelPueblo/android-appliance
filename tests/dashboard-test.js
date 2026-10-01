// Exercise the shipped dashboard against a minimal hook/SDK harness.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync("dashboard/dist/index.js", "utf8");
const flush = () => new Promise(setImmediate);

function harness() {
  let component, cursor = 0, tree;
  const hooks = [], effects = [], requests = [], timers = new Map();
  let nextTimer = 1;
  const React = {
    createElement(type, props, ...children) { return { type, props: props || {}, children: children.flat() }; },
    useRef(initial) {
      const i = cursor++;
      if (!hooks[i]) hooks[i] = { current: initial };
      return hooks[i];
    },
  };
  const context = {
    URL,
    setTimeout(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    window: {
      location: { origin: "https://hermes.example.net", protocol: "https:", hostname: "hermes.example.net" },
      __HERMES_PLUGINS__: { register(id, fn) { component = fn; } },
      __HERMES_PLUGIN_SDK__: {
        React,
        hooks: {
          useState(initial) {
            const i = cursor++;
            if (!(i in hooks)) hooks[i] = initial;
            return [hooks[i], value => { hooks[i] = value; }];
          },
          useCallback(fn) { const i = cursor++; return hooks[i] || (hooks[i] = fn); },
          useEffect(fn) { const i = cursor++; if (!hooks[i]) { hooks[i] = true; effects.push(fn); } },
        },
        components: Object.fromEntries(["Card", "CardHeader", "CardTitle", "CardContent", "Badge", "Button"].map(x => [x, x])),
        fetchJSON(url, opts) {
          return new Promise((resolve, reject) => requests.push({ url, opts, resolve, reject }));
        },
      },
    },
  };
  vm.runInNewContext(source, context);
  function render() { cursor = 0; tree = component(); return tree; }
  function find(type, text) {
    const walk = node => {
      if (!node || typeof node !== "object") return null;
      if (node.type === type && (text === undefined || node.children.includes(text))) return node;
      for (const child of node.children) { const found = walk(child); if (found) return found; }
      return null;
    };
    return walk(tree);
  }
  render();
  const cleanup = effects.map(fn => fn());
  return { requests, timers, render, find, unmount() { cleanup.forEach(fn => fn()); } };
}
const status = { ok: true, state: "running", boot_completed: "1", idle_seconds: "5", display_url: "/api/plugins/android-appliance/display/vnc.html" };

async function ready(app) {
  app.requests[0].resolve(status);
  await flush();
  app.render();
}

test("only schedules another poll after the previous response, and cancels on unmount", async () => {
  const app = harness();
  assert.equal(app.requests.length, 1);
  assert.equal(app.timers.size, 0);
  await ready(app);
  assert.equal(app.timers.size, 1);
  const [id, fn] = app.timers.entries().next().value;
  app.timers.delete(id);
  fn();
  assert.equal(app.requests.length, 2);
  assert.equal(app.timers.size, 0);
  app.unmount();
  app.requests[1].resolve(status);
  await flush();
  assert.equal(app.timers.size, 0);
});

test("embeds same-origin display and removes suspend/resume controls", async () => {
  const app = harness();
  await ready(app);
  assert.equal(app.find("Button", "Suspend"), null);
  assert.equal(app.find("Button", "Resume"), null);
  const show = app.find("Button", "Show Display");
  show.props.onClick();
  show.props.onClick(); // A rapid second click must not mint another ticket.
  assert.equal(app.requests.length, 2);
  app.requests[1].resolve({ ok: true, url: status.display_url + "?path=ticket" });
  await flush();
  app.render();
  assert.equal(app.find("iframe").props.src, "https://hermes.example.net" + status.display_url + "?path=ticket");
  assert.equal(app.find("iframe").props.title, "Android display");
  assert.ok(app.find("Button", "Reload Display"));
  app.unmount();
});

test("polling preserves action errors and a failed poll disables stale controls", async () => {
  const app = harness();
  await ready(app);
  app.find("Button", "Stop").props.onClick();
  app.requests[1].resolve({ ok: false, error: "shutdown failed" });
  await flush();
  app.requests[2].resolve(status);
  await flush();
  app.render();
  assert.match(app.find("p").children.join(""), /shutdown failed/);
  const [id, fn] = app.timers.entries().next().value;
  app.timers.delete(id);
  fn();
  app.requests[3].reject(new Error("network down"));
  await flush();
  app.render();
  assert.equal(app.find("Button", "Stop").props.disabled, true);
  assert.equal(app.find("Button", "Show Display").props.disabled, true);
  app.unmount();
});

test("remote browser rejects a loopback display URL", async () => {
  const app = harness();
  await ready(app);
  app.find("Button", "Show Display").props.onClick();
  app.requests[1].resolve({ ok: true, url: "http://127.0.0.1:6090/vnc.html" });
  await flush();
  app.render();
  assert.equal(app.find("iframe"), null);
  assert.match(app.find("p").children.join(""), /localhost/);
  app.unmount();
});
