// Smoke test for the streamer page's JavaScript.
//
// `node --check` only proves it parses. This runs the real script against a
// stubbed DOM and a stubbed API, then fires the clicks that matter, so a
// ReferenceError or a TypeError in a handler fails here rather than silently
// doing nothing when someone presses Enable.
//
//     node streamer/tests/smoke_ui.js

"use strict";

const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "..", "eclerstreamer", "static", "index.html");

const STATE = {
  version: "0.1.0", systemd: true,
  config: { local_addr: "10.0.2.5", interface: "", manager_url: "",
            qmin: 18, no_bframes: true },
  dashboards: [{
    channel: 5, name: "Reception board", url: "https://example.com/d",
    enabled: false, display: null, capture_fps: 15, fps: 30,
    bitrate: "6M", size: "1920x1080", note: "",
    display_name: ":105", multicast: "239.255.42.47",
    status: { available: true, active: "inactive", sub: "dead", result: "",
              since: "", restarts: 0, pid: 0, at_boot: false },
  }],
  server_time: 1757930000,
};

const listeners = [];

function element(id) {
  const node = {
    id, value: "", textContent: "", innerHTML: "", hidden: false,
    className: "", dataset: {}, disabled: false, title: "", checked: false,
    style: { _p: {}, setProperty(k, v) { this._p[k] = v; },
             getPropertyValue(k) { return this._p[k] ?? ""; } },
    appendChild() {}, append() {}, remove() {}, replaceChildren() {},
    addEventListener(type, fn) { listeners.push([type, fn, node]); },
    setAttribute() {}, getAttribute() { return "false"; },
    querySelector() { return element("q"); },
    querySelectorAll() { return []; },
    closest() { return null; }, click() {}, focus() {},
    scrollIntoView() {},
  };
  return node;
}

const registry = new Map();
function byId(id) {
  if (!registry.has(id)) registry.set(id, element(id));
  return registry.get(id);
}

global.document = {
  getElementById: byId,
  createElement: (tag) => element(tag),
  querySelector: () => element("q"),
  querySelectorAll: () => [],
  addEventListener(type, fn) { listeners.push([type, fn, null]); },
};
global.window = global;
global.location = { search: "", href: "" };
global.confirm = () => true;
global.prompt = () => "7";
global.alert = () => {};

const calls = [];
global.fetch = async (url, options) => {
  calls.push([String(url), options && options.method]);
  return {
    ok: true, status: 200,
    json: async () => (String(url).includes("/api/state") ? STATE : { ok: true }),
  };
};

const html = fs.readFileSync(HTML, "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

function fail(what, err) {
  console.error(`✗ streamer UI ${what}:`);
  console.error("    " + (err && err.stack ? err.stack.split("\n").slice(0, 4).join("\n    ") : err));
  process.exit(1);
}

let run;
try {
  run = eval(script);            // eslint-disable-line no-eval
} catch (err) {
  fail("failed to evaluate", err);
}

(async () => {
  await new Promise((resolve) => setTimeout(resolve, 50));

  if (!calls.some(([url]) => url.includes("/api/state"))) {
    fail("never fetched state", "no /api/state call was made");
  }

  // Fire the clicks that a person actually makes. A handler that throws here
  // is a button that does nothing at all in the browser, with no clue why.
  const clickHandlers = listeners.filter(([type]) => type === "click");
  for (const action of ["enable", "disable", "start", "stop", "restart",
                        "toggle", "save-url", "save-settings", "save-host",
                        "delete"]) {
    const button = { dataset: { act: action, ch: "5" }, disabled: false };
    const event = { target: { closest: (sel) => sel.includes("data-act") ? button : null } };
    for (const [, fn, node] of clickHandlers) {
      if (node) continue;                 // the delegated document handler only
      try {
        await fn(event);
      } catch (err) {
        fail(`threw handling "${action}"`, err);
      }
    }
  }

  console.log("  ✓ streamer UI runs: load, render and every button");
  // The page sets a refresh interval, which keeps node's event loop alive
  // for ever; the test is finished, so say so.
  process.exit(0);
})();
