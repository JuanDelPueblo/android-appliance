// Android tab for the Hermes dashboard. Plain IIFE, no build step.
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const useRef = React.useRef;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  const API = "/api/plugins/android-appliance";
  const POLL_MS = 3000;
  const ENABLED = {
    stopped: ["start"],
    starting: ["stop"],
    running: ["stop", "restart"],
    stopping: [],
  };
  const ACTIONS = [["start", "Start"], ["stop", "Stop"], ["restart", "Restart"]];

  function formatIdle(seconds) {
    const s = Number(seconds);
    if (seconds == null || !Number.isFinite(s) || s < 0) return "unknown";
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min";
    return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min";
  }

  function displayURL(value) {
    const url = new URL(value, window.location.origin);
    if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("Display URL must use HTTP or HTTPS.");
    if (url.username || url.password) throw new Error("Display URL must not contain credentials.");
    const local = ["localhost", "127.0.0.1", "[::1]"];
    if (local.indexOf(url.hostname) >= 0 && local.indexOf(window.location.hostname) < 0) {
      throw new Error("The display URL points to localhost. Configure a public display_url or use Hermes dashboard authentication.");
    }
    if (window.location.protocol === "https:" && url.protocol !== "https:") {
      throw new Error("An HTTPS dashboard needs an HTTPS display URL.");
    }
    return url.href;
  }

  function AndroidPage() {
    const [status, setStatus] = useState(null);
    const [busy, setBusy] = useState(null);
    const [error, setError] = useState(null);
    const [statusError, setStatusError] = useState(null);
    const [display, setDisplay] = useState(null);
    const alive = useRef(false);
    const pending = useRef(null);
    const actionPending = useRef(false);

    const refresh = useCallback(function () {
      if (pending.current) return pending.current;
      pending.current = SDK.fetchJSON(API + "/status")
        .then(function (data) {
          if (!alive.current) return;
          setStatus(data);
          setStatusError(data.ok ? null : data.error);
        })
        .catch(function (err) {
          if (!alive.current) return;
          setStatus(null);
          setStatusError(String(err));
        })
        .finally(function () { pending.current = null; });
      return pending.current;
    }, []);

    useEffect(function () {
      alive.current = true;
      let timer;
      let cancelled = false;
      function poll() {
        refresh().finally(function () {
          if (!cancelled) timer = setTimeout(poll, POLL_MS);
        });
      }
      poll();
      return function () {
        cancelled = true;
        alive.current = false;
        clearTimeout(timer);
      };
    }, [refresh]);

    function run(name) {
      if (actionPending.current) return;
      actionPending.current = true;
      setBusy(name);
      setError(null);
      if (name === "stop" || name === "restart") setDisplay(null);
      const request = name === "display" ? "/display/session" : "/actions/" + name;
      SDK.fetchJSON(API + request, { method: "POST" })
        .then(function (data) {
          if (!alive.current) return;
          if (!data.ok) throw new Error(data.error || "Android command failed");
          if (name === "display") setDisplay(displayURL(data.url));
        })
        .catch(function (err) { if (alive.current) setError(String(err)); })
        .finally(function () {
          actionPending.current = false;
          if (!alive.current) return;
          setBusy(null);
          refresh();
        });
    }

    const state = status && status.ok ? status.state : "unknown";
    const ready = state === "running" && status.boot_completed === "1";
    const allowed = ENABLED[state] || [];

    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Android appliance")),
      h(CardContent, { className: "space-y-4" },
        h("div", { className: "flex flex-wrap items-center gap-2 text-sm" },
          h("span", null, "State:"), h(Badge, null, state),
          h("span", { className: "ml-4" }, "Android:"),
          h(Badge, { variant: ready ? "default" : "secondary" }, ready ? "ready" : "not ready"),
          status && status.ok ? h("span", { className: "ml-4 text-muted-foreground" }, "Idle " + formatIdle(status.idle_seconds)) : null
        ),
        h("div", { className: "flex flex-wrap gap-2" },
          ACTIONS.map(function (pair) {
            return h(Button, {
              key: pair[0],
              disabled: busy !== null || allowed.indexOf(pair[0]) < 0,
              onClick: function () { run(pair[0]); },
            }, busy === pair[0] ? pair[1] + "…" : pair[1]);
          }),
          h(Button, {
            variant: "outline",
            disabled: busy !== null || !(status && status.ok && status.display_url) || state === "stopping",
            onClick: function () { run("display"); },
          }, busy === "display" ? "Connecting…" : display ? "Reload Display" : "Show Display"),
          display ? h(Button, { variant: "outline", onClick: function () { setDisplay(null); } }, "Hide Display") : null
        ),
        error || statusError ? h("p", { role: "alert", className: "text-sm text-destructive" }, error || statusError) : null,
        status && status.display_error ? h("p", { className: "text-sm text-muted-foreground" }, status.display_error) : null,
        display ? h("iframe", {
          key: display,
          title: "Android display",
          src: display,
          referrerPolicy: "no-referrer",
          allow: "fullscreen; clipboard-read; clipboard-write",
          style: { display: "block", width: "100%", maxWidth: "520px", height: "min(75vh, 850px)", margin: "0 auto", border: "1px solid currentColor", borderRadius: "8px", background: "#111" },
        }) : null,
        h("p", { className: "text-xs text-muted-foreground" },
          "Show Display opens Android here. Idle Android shuts down; its apps and data stay saved. Use Reload Display to reconnect."
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("android-appliance", AndroidPage);
})();
