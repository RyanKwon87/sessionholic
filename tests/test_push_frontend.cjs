"use strict";

// The service worker's push handling and the settings module, with synthetic browsers only.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ENDPOINT = "https://web.push.apple.com/fixture-device-token";
const swSource = fs.readFileSync(path.join(__dirname, "../web/sw.js"), "utf8");
const pushSource = fs.readFileSync(path.join(__dirname, "../web/push.js"), "utf8");
const settle = () => new Promise(setImmediate);
const plain = (value) => JSON.parse(JSON.stringify(value));

function worker({ pending = [], fetchFails = false, shown = [], windows = [], subscribed = true } = {}) {
  const listeners = {}, notifications = [], fetches = [], opened = [];
  const self = {
    location: { origin: "https://board.example" },
    addEventListener: (type, handler) => { listeners[type] = handler; },
    registration: {
      pushManager: { getSubscription: async () => (subscribed ? { endpoint: ENDPOINT } : null) },
      showNotification: async (title, options) => { notifications.push({ title, ...options }); },
      getNotifications: async () => shown,
    },
    clients: {
      claim: async () => {},
      matchAll: async () => windows,
      openWindow: async (url) => { opened.push(url); },
    },
  };
  vm.runInNewContext(swSource, {
    self, URL, Set, Response, TextEncoder, Uint8Array, Array, crypto: crypto.webcrypto,
    caches: { keys: async () => [], open: async () => ({ match: async () => null, put: async () => {}, keys: async () => [] }) },
    fetch: async (url, options) => {
      fetches.push({ url, options });
      if (fetchFails) throw new Error("offline");
      return { ok: true, json: async () => ({ notifications: pending }) };
    },
  });
  async function fire(type, event = {}) {
    let done;
    listeners[type]({ ...event, waitUntil: (promise) => { done = promise; } });
    await done;
  }
  return { fire, notifications, fetches, opened };
}

test("a push shows each queued notice with a safe board link, read with the board's device id and header", async () => {
  const w = worker({ pending: [
    { title: "정산 자동화", body: "답이 필요해요 · 노트북 · Codex", tag: "session:a|codex||1", url: "/?session=a%7Ccodex%7C%7C1" },
    { title: "외부 링크", body: "x", tag: "session:b", url: "https://evil.example/" },
    { title: 5, tag: "bad" },
  ] });
  await w.fire("push");
  const id = crypto.createHash("sha256").update(ENDPOINT).digest("hex").slice(0, 32);
  assert.equal(w.fetches.length, 1);
  assert.equal(w.fetches[0].url, `/api/push/pending?id=${id}`);
  assert.equal(w.fetches[0].options.headers["X-Board-Push"], "1");
  assert.equal(w.fetches[0].options.credentials, "same-origin");
  assert.deepEqual(w.notifications.map((item) => [item.title, item.tag, item.data.url, item.renotify]), [
    ["정산 자동화", "session:a|codex||1", "/?session=a%7Ccodex%7C%7C1", true],
    ["외부 링크", "session:b", "/", true],
  ]);
});

test("with nothing queued a push shows the latest notice again quietly, or a generic one, never nothing", async () => {
  const latest = { title: "이전 알림", body: "작업이 끝났어요", tag: "session:c", data: { url: "/?session=c" } };
  const quiet = worker({ shown: [{ title: "older", tag: "x" }, latest] });
  await quiet.fire("push");
  assert.deepEqual(quiet.notifications.map((item) => [item.title, item.tag, item.renotify]), [["이전 알림", "session:c", false]]);
  // The board could not be read: a new alert was sent, so the old notice is not reused.
  for (const options of [{}, { fetchFails: true }, { subscribed: false }, { fetchFails: true, shown: [latest] }]) {
    const generic = worker(options);
    await generic.fire("push");
    assert.deepEqual(generic.notifications.map((item) => [item.title, item.tag, item.data.url, item.renotify]), [["확인할 세션이 있어요", "board-notice", "/", true]]);
  }
});

test("tapping a notice focuses an open board and asks it to open the session, or opens the board", async () => {
  const messages = []; let focused = 0;
  const board = { url: "https://board.example/", focus: async () => { focused += 1; }, postMessage: (value) => messages.push(value) };
  const other = { url: "https://other.example/", focus: async () => { throw new Error("never another origin"); } };
  const open = worker({ windows: [other, board] });
  const closed = [];
  await open.fire("notificationclick", { notification: { data: { url: "/?session=a%7Cb" }, close: () => closed.push(1) } });
  assert.deepEqual([focused, plain(messages), closed.length], [1, [{ type: "open-session", url: "/?session=a%7Cb" }], 1]);
  const cold = worker();
  await cold.fire("notificationclick", { notification: { data: { url: "javascript:alert(1)" }, close() {} } });
  assert.deepEqual(cold.opened, ["/"]);
});

function settings({ supported = true, agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2)", standalone = true,
  permission = "default", grant = "granted", existing = null, registered = false,
  status = { available: true, publicKey: "BAEC" } } = {}) {
  const nodes = {}, calls = [], toasts = [], made = [];
  for (const id of ["push-status", "push-toggle", "push-test"]) {
    nodes[id] = { textContent: "", hidden: false, disabled: false, events: {},
      addEventListener(type, handler) { this.events[type] = handler; } };
  }
  const board = { registered };
  let subscription = existing;
  const pushManager = {
    getSubscription: async () => subscription,
    subscribe: async (options) => {
      made.push(options);
      subscription = { endpoint: ENDPOINT, options: { applicationServerKey: options.applicationServerKey.buffer },
        toJSON: () => ({ endpoint: ENDPOINT, keys: { p256dh: "k", auth: "a" } }),
        unsubscribe: async () => { calls.push(["unsubscribe-browser"]); subscription = null; return true; } };
      return subscription;
    },
  };
  const window = {
    matchMedia: () => ({ matches: standalone }), crypto: crypto.webcrypto,
    Notification: { get permission() { return permission; }, requestPermission: async () => { permission = grant; return grant; } },
  };
  if (supported) window.PushManager = function PushManager() {};
  const navigator = { userAgent: agent, maxTouchPoints: 5 };
  if (supported) navigator.serviceWorker = { ready: Promise.resolve({ pushManager }) };
  const context = { window, navigator, document: { getElementById: (id) => nodes[id] || null },
    setTimeout, atob, Uint8Array, Promise, TextEncoder, Array };
  context.Notification = window.Notification;
  vm.runInNewContext(pushSource, context);
  const module = window.SessionholicPush;
  module.init({
    api: async (url, options = {}) => {
      calls.push([url, options.method || "GET", options.body]);
      if (url === "/api/push/subscribe") board.registered = true;
      if (url === "/api/push/unsubscribe") board.registered = false;
      return url.startsWith("/api/push?id=") ? { ...status, registered: board.registered } : url === "/api/push" ? status : { ok: true };
    },
    toast: (text) => toasts.push(text),
  });
  return { module, nodes, calls, toasts, made, board, current: () => subscription };
}

test("settings explain unsupported, Safari-tab and blocked browsers instead of offering a switch that cannot work", async () => {
  const plain = settings({ supported: false, agent: "Mozilla/5.0 (X11; Linux)" });
  await plain.module.render();
  assert.match(plain.nodes["push-status"].textContent, /지원하지 않아요/);
  assert.equal(plain.nodes["push-toggle"].hidden, true);
  const tab = settings({ supported: false, standalone: false });
  await tab.module.render();
  assert.match(tab.nodes["push-status"].textContent, /홈 화면에 추가/);
  const blocked = settings({ permission: "denied" });
  await blocked.module.render();
  assert.match(blocked.nodes["push-status"].textContent, /차단/);
  assert.equal(blocked.nodes["push-toggle"].hidden, true);
  const off = settings();
  await off.module.render();
  assert.equal(off.nodes["push-toggle"].textContent, "알림 켜기");
  assert.equal(off.nodes["push-test"].hidden, true);
});

test("turning on asks permission, subscribes with the board key and registers this device; off and test use its endpoint", async () => {
  const s = settings();
  await s.module.render();
  await s.module.toggle();
  assert.equal(s.made.length, 1);
  assert.equal(s.made[0].userVisibleOnly, true);
  assert.deepEqual(Array.from(s.made[0].applicationServerKey), [4, 1, 2]);
  const register = s.calls.find((call) => call[0] === "/api/push/subscribe");
  assert.deepEqual(plain(register), ["/api/push/subscribe", "POST",
    { subscription: { endpoint: ENDPOINT, keys: { p256dh: "k", auth: "a" } }, label: "iPhone 앱" }]);
  const id = crypto.createHash("sha256").update(ENDPOINT).digest("hex").slice(0, 32);
  assert.ok(s.calls.some((call) => call[0] === `/api/push?id=${id}`), "the board is asked whether it still has this device");
  assert.equal(s.nodes["push-toggle"].textContent, "알림 끄기");
  assert.equal(s.nodes["push-test"].hidden, false);
  await s.module.test();
  assert.deepEqual(plain(s.calls.at(-2)), ["/api/push/test", "POST", { endpoint: ENDPOINT }]);
  await s.module.toggle();
  assert.ok(s.calls.some((call) => call[0] === "/api/push/unsubscribe" && call[2].endpoint === ENDPOINT));
  assert.ok(s.calls.some((call) => call[0] === "unsubscribe-browser"));
  assert.equal(s.current(), null);
  assert.equal(s.nodes["push-toggle"].textContent, "알림 켜기");
});

test("a refused permission or an unavailable board leaves notifications off with the reason", async () => {
  const refused = settings({ grant: "denied" });
  await refused.module.toggle();
  assert.equal(refused.made.length, 0);
  assert.match(refused.toasts.at(-1), /차단/);
  const unavailable = settings({ status: { available: false, reason: "이 보드에서는 알림을 보낼 수 없습니다." } });
  await unavailable.module.toggle();
  assert.equal(unavailable.made.length, 0);
  assert.equal(unavailable.toasts.at(-1), "이 보드에서는 알림을 보낼 수 없습니다.");
});

test("a device the board forgot, or one made for another board key, offers 다시 켜기 and is replaced", async () => {
  let dropped = 0;
  const old = { endpoint: "https://web.push.apple.com/old", options: { applicationServerKey: new Uint8Array([9, 9, 9]).buffer },
    toJSON: () => ({ endpoint: "https://web.push.apple.com/old" }), unsubscribe: async () => { dropped += 1; return true; } };
  const s = settings({ existing: old, registered: true });
  await s.module.render();
  assert.equal(s.nodes["push-toggle"].textContent, "알림 다시 켜기");
  assert.match(s.nodes["push-status"].textContent, /다시 켜면/);
  assert.equal(s.nodes["push-test"].hidden, true);
  await s.module.toggle();
  assert.equal(dropped, 1, "the old-key subscription is dropped before subscribing again");
  assert.equal(s.made.length, 1);
  assert.equal(s.nodes["push-toggle"].textContent, "알림 끄기");
  // Same key, but the board no longer has the device (e.g. the push service expired it).
  const forgotten = settings({ registered: false });
  await forgotten.module.render();
  await forgotten.module.toggle();
  forgotten.board.registered = false;
  await forgotten.module.render();
  assert.equal(forgotten.nodes["push-toggle"].textContent, "알림 다시 켜기");
  await forgotten.module.toggle();
  assert.equal(forgotten.made.length, 1, "a live same-key subscription is registered again without a new one");
  assert.equal(forgotten.nodes["push-toggle"].textContent, "알림 끄기");
});
