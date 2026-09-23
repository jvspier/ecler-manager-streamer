// Smoke test for the dashboard's JavaScript.
//
// `node --check` only proves the file parses. It cannot see a function that is
// called and never declared — which is how a dashboard that parses cleanly
// throws on its first render and shows nothing but an error toast.
//
// This runs the real script against a stubbed DOM and a stubbed /api/state,
// then exercises the paths that normally need a click. A ReferenceError or a
// TypeError fails the run.
//
//     node tests/smoke_dashboard.js
//
// Requires node. Skipped, not failed, when node is absent — see
// tests/test_veo.py::TestDashboardSmoke.

"use strict";

const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "..", "eclermanager", "static", "index.html");

const STATE = {
  devices: [{
    id: "rx-01", name: "A", ip: "10.0.2.1", location: "", note: "",
    expected_group_id: 1, expected_channel_name: "Reception",
    online: true, group_id: 1, channel_name: "Reception", video_lock: true,
    device_name: "X", fw_version: "v1", mac_address: "00:1a:96:aa:bb:01",
    dhcp: false, error: null, latency_ms: 10, checked_at: 0, drifted: false,
    consecutive_no_signal: 0, consecutive_offline: 0, last_action: "",
    last_action_at: null, busy: false, held: false, held_from_group_id: null,
    enabled: true,
  }],
  channels: [{
    group_id: 1, name: "Reception", transmitter_ip: "10.0.1.1", note: "",
    multicast_group: "239.255.42.43", show_button: true,
  }, {
    group_id: 6, name: "Production (stream)", transmitter_ip: null, note: "",
    multicast_group: null, show_button: false, reports_lock: false,
  }],
  disabled: [{ id: "rx-90", name: "spare", ip: "10.0.2.90", location: "",
               note: "in storage", expected_group_id: null, enabled: false }],
  discovered: [{ ip: "10.0.2.50", group_id: 2, channel_name: null,
                 video_lock: true, device_name: "VEO-XRI1C", fw_version: "v1",
                 mac_address: "00:1a:96:aa:bb:09", role: "receiver?",
                 suggested_id: "rx-50", suggested_name: "VEO-XRI1C" }],
  discovery: { ranges: ["10.0.2.1-254"], interval_hours: 0, running: false,
               message: "", scanned: 0, error: "", last: null },
  batch: { running: true, total: 3, done: 1, message: "moving 3 to channel 6",
           results: [] },
  summary: { total: 1, online: 1, offline: 0, drifted: 0, no_signal: 0,
             held: 0, dhcp: 0, by_actual_channel: {}, by_expected_channel: {} },
  poll: { interval_seconds: 30, last_started: 0, last_finished: 0, count: 1,
          auto_repair: false, auto_nudge_on_signal_loss: false },
  events: [{ ts: 0, receiver_id: "rx-01", receiver_name: "A", kind: "switch",
             message: "ok" }],
  server_time: 0,
};

function element(id) {
  const node = {
    id, value: "", textContent: "", innerHTML: "", hidden: false,
    className: "", dataset: {}, files: [], disabled: false,
    title: "", checked: false,
    // style needs setProperty/getPropertyValue: the dashboard sets CSS custom
    // properties (--ch, --seg) to carry channel colour down to the cards, and
    // a bare {} silently lacks them until it throws at runtime.
    style: {
      _props: {},
      setProperty(name, value) { this._props[name] = value; },
      getPropertyValue(name) { return this._props[name] ?? ""; },
      removeProperty(name) { delete this._props[name]; },
    },
    scrollIntoView() {},
    appendChild() {}, append() {}, remove() {}, replaceChildren() {},
    addEventListener() {}, setAttribute() {},
    getAttribute() { return "false"; },
    querySelector() { return element("q"); },
    querySelectorAll() { return []; },
    closest() { return null; }, click() {}, select() {}, focus() {},
  };
  return node;
}

global.document = {
  getElementById: (id) => element(id),
  createElement: (tag) => element(tag),
  querySelector: () => element("q"),
  querySelectorAll: () => [],
  addEventListener: () => {},
};
// CSS.escape is used when looking a card up by id for the signal bar.
global.CSS = { escape: (value) => String(value).replace(/["\\]/g, "\\$&") };
global.window = global;
global.location = { search: "", href: "" };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.confirm = () => false;
global.prompt = () => null;
global.alert = () => {};
global.fetch = async (url) => ({
  ok: true,
  status: 200,
  json: async () => (String(url).includes("/api/state")
    ? STATE
    : { ok: true, user: "svc", auth: false, raw: {} }),
});

// Keep the page's own timers from running: this is a one-shot check.
global.setInterval = () => 0;
global.setTimeout = (fn) => { void fn; return 0; };

const html = fs.readFileSync(HTML, "utf8");
const match = html.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!match) {
  console.error("✗ no <script> block found in index.html");
  process.exit(2);
}

// Evaluate in this scope so the page's top-level functions become globals.
const run = new Function(match[1] + "\n;return { captureDrafts, draftOr, " +
  "clearDrafts, render, renderStats, renderFound, renderDisabled, " +
  "renderSetup, deviceCard, advancedPanel, renameEditor, disabledCard, " +
  "foundCard, groupSection, sortDevices, deviceHealth, compareName, " +
  "compareIp, load, api, toast, esc, fmtTime, withPending, state, " +
  "channelButtons, channelRow, renderChannels, renderBatch, announceBatch, " +
  "moveBar, groupMembers };");

let api;
try {
  api = run();
} catch (err) {
  console.error("✗ the script threw while loading:", err.message);
  process.exit(1);
}

function fail(message) { throw new Error(message); }

(async () => {
  try {
    // load() populates state and renders; this is what runs on page open.
    await api.load();

    // Then the paths that normally need a click or a 5s refresh.
    api.captureDrafts();
    if (api.draftOr("nope-1", "fallback") !== "fallback") {
      throw new Error("draftOr did not fall back");
    }
    api.clearDrafts(["nope-"]);
    api.render();
    api.renderStats();
    api.renderFound();
    api.renderDisabled();
    api.renderSetup();

    // Cards for every shape a device can take.
    api.deviceCard(STATE.devices[0]);
    api.deviceCard({ ...STATE.devices[0], online: false, group_id: null,
                     channel_name: null });
    api.deviceCard({ ...STATE.devices[0], drifted: true, group_id: 2 });
    api.deviceCard({ ...STATE.devices[0], held: true, held_from_group_id: 1 });
    api.deviceCard({ ...STATE.devices[0], video_lock: false, dhcp: true });
    if (!api.deviceCard({ ...STATE.devices[0], video_lock: null, lock_reported: false })
          .innerHTML.includes("signal n/a")) {
      fail("a receiver on a software stream should say signal n/a");
    }
    api.advancedPanel(STATE.devices[0]);
    api.renameEditor(STATE.devices[0]);
    api.disabledCard(STATE.disabled[0]);
    api.foundCard(STATE.discovered[0]);
    api.groupSection({ key: "ch1", groupId: 1, name: "Reception", note: "",
                       transmitter: "10.0.1.1", multicast: "239.255.42.43",
                       devices: STATE.devices });
    api.groupSection({ key: "empty", groupId: 3, name: "Canteen", note: "",
                       transmitter: "10.0.1.3", multicast: null, devices: [] });

    // Channel editor, group move and the batch banner.
    api.state.channelsOpen = true;
    api.render();
    api.channelRow(STATE.channels[0]);
    api.channelRow(null);
    api.renderBatch();
    api.moveBar("ch1", 1, STATE.devices);
    api.moveBar("ch1", 1, STATE.devices).innerHTML.includes("6 · Production")
      || fail("move bar does not offer channel 6");
    api.state.data = { ...STATE, channels: [STATE.channels[0]] };
    api.moveBar("ch1", 1, STATE.devices).innerHTML.includes("Channels…")
      || fail("move bar with nowhere to go should point at Channels…");
    api.state.data = STATE;
    if (api.groupMembers("ch1").length !== 1) fail("groupMembers(ch1)");
    const buttons = api.channelButtons(STATE.devices[0]);
    if (buttons.includes("Production (stream)")) {
      fail("a channel with show_button off still got a card button");
    }
    if (!api.channelButtons({ ...STATE.devices[0], expected_group_id: 6 })
          .includes("Production (stream)")) {
      fail("a card lost the button for its own expected channel");
    }
    api.announceBatch({ running: true });
    api.announceBatch({ running: false, message: "moved 2 of 3", results: [
      { id: "rx-01", name: "A", ok: false, message: "timed out" }] });

    console.log("  ✓ dashboard JavaScript runs: load, render and every card shape");
    process.exit(0);
  } catch (err) {
    console.error("✗ dashboard JavaScript threw at runtime:");
    console.error("   ", err.message);
    if (err.stack) {
      console.error(err.stack.split("\n").slice(1, 4).join("\n"));
    }
    process.exit(1);
  }
})();
