// Android tab for the Hermes dashboard. Plain IIFE, no build step.
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;

  const API = "/api/plugins/android-appliance";
  const POLL_MS = 3000;

  // Which buttons make sense in each state.
  const ENABLED = {
    stopped: ["start"],
    starting: ["stop"],
    running: ["suspend", "stop", "restart"],
    suspended: ["resume", "stop", "restart"],
    stopping: [],
  };

  const ACTIONS = [
    ["start", "Start"],
    ["suspend", "Suspend"],
    ["resume", "Resume"],
    ["stop", "Stop"],
    ["restart", "Restart"],
  ];

  function formatIdle(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s)) return "unknown";
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min";
    return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min";
  }

  function AndroidPage() {
    const [status, setStatus] = useState(null);
    const [busy, setBusy] = useState(null);
    const [error, setError] = useState(null);

    const refresh = useCallback(function () {
      return SDK.fetchJSON(API + "/status")
        .then(function (data) {
          setStatus(data);
          setError(data.ok ? null : data.error);
        })
        .catch(function (err) {
          setError(String(err));
        });
    }, []);

    useEffect(function () {
      refresh();
      const timer = setInterval(refresh, POLL_MS);
      return function () {
        clearInterval(timer);
      };
    }, [refresh]);

    function run(name) {
      setBusy(name);
      setError(null);
      SDK.fetchJSON(API + "/actions/" + name, { method: "POST" })
        .then(function (data) {
          if (!data.ok) setError(data.error);
        })
        .catch(function (err) {
          setError(String(err));
        })
        .then(function () {
          setBusy(null);
          refresh();
        });
    }

    const state = status && status.ok ? status.state : "unknown";
    const ready = status && status.boot_completed === "1";
    const allowed = ENABLED[state] || [];

    return h(
      Card,
      null,
      h(CardHeader, null, h(CardTitle, null, "Android appliance")),
      h(
        CardContent,
        { className: "space-y-4" },
        h(
          "div",
          { className: "flex flex-wrap items-center gap-2 text-sm" },
          h("span", null, "State:"),
          h(Badge, null, state),
          h("span", { className: "ml-4" }, "Android:"),
          h(Badge, { variant: ready ? "default" : "secondary" }, ready ? "ready" : "not ready"),
          status && status.ok
            ? h("span", { className: "ml-4 text-muted-foreground" }, "Idle " + formatIdle(status.idle_seconds))
            : null
        ),
        h(
          "div",
          { className: "flex flex-wrap gap-2" },
          ACTIONS.map(function (pair) {
            return h(
              Button,
              {
                key: pair[0],
                disabled: busy !== null || allowed.indexOf(pair[0]) < 0,
                onClick: function () {
                  run(pair[0]);
                },
              },
              busy === pair[0] ? pair[1] + "…" : pair[1]
            );
          }),
          h(
            Button,
            {
              variant: "outline",
              disabled: !(status && status.display_url),
              onClick: function () {
                window.open(status.display_url, "_blank", "noopener");
              },
            },
            "Open Display"
          )
        ),
        error ? h("p", { className: "text-sm text-destructive" }, error) : null,
        h(
          "p",
          { className: "text-xs text-muted-foreground" },
          "Opening the display starts or resumes Android. Idle Android suspends, then stops with its Quick Boot state saved."
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("android-appliance", AndroidPage);
})();
