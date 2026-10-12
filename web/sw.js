"use strict";

// Sessionholic caches only public static assets. All session and terminal data remains online-only.
// - "/" (the page): network first; the cached copy is used only while offline.
// - Allowed assets with "?v=<sha8>": cache first. The key is the full URL, and
//   storing a new version drops the older versions of the same path. Only a
//   response marked immutable is stored: the server marks it so only when the
//   requested version is the file it sent (a stale or rolled-back version gets
//   the current file with no-cache, which must not be kept under that URL).
// - The same assets without a version: network first (pages served before
//   versioned URLs, or by a server that does not fill them in).
// - Anything else (API, login, terminal, other queries) is never touched.
const CACHE = "sessionholic-shell-v14";
const ASSETS = new Set([
  "/app.js",
  "/session_labels.js",
  "/push.js",
  "/theme-init.js",
  "/message-format.js",
  "/style.css",
  "/icon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
  "/manifest.webmanifest",
  "/vendor/xterm.js",
  "/vendor/xterm.css",
  "/vendor/addon-fit.js",
]);
const VERSIONED = /^\?v=[0-9a-f]{8}$/;
// Precached from the page's asset list at install; the terminal library is
// only cached once a terminal is actually opened.
const PRECACHE_SKIP = new Set(["/vendor/xterm.js", "/vendor/xterm.css", "/vendor/addon-fit.js"]);

function cacheable(response) {
  return !!response && response.ok && response.type !== "opaqueredirect" && !response.redirected;
}
function immutable(response) {
  const control = String(response?.headers?.get?.("Cache-Control") || "");
  return control.split(",").some((part) => part.trim().toLowerCase() === "immutable");
}
async function storeVersion(cache, request, url, response) {
  if (!cacheable(response) || !immutable(response)) return;
  await cache.put(request, response);
  await pruneVersions(cache, url);
}
function offlineResponse() {
  return new Response("연결을 확인한 뒤 다시 열어 주세요.", {
    status: 503,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
async function pruneVersions(cache, url) {
  for (const request of await cache.keys()) {
    const cached = new URL(request.url);
    if (cached.pathname === url.pathname && cached.search !== url.search) await cache.delete(request);
  }
}
async function precache() {
  const cache = await caches.open(CACHE);
  const response = await fetch("/", { cache: "no-store", credentials: "same-origin" });
  if (!cacheable(response)) return;
  const page = await response.clone().text();
  await cache.put("/", response);
  const meta = /<meta\b[^>]*\bname="sessionholic-assets"[^>]*\bcontent="([^"]*)"/.exec(page);
  let versions = {};
  try {
    versions = JSON.parse((meta?.[1] || "{}").replace(/&quot;/g, '"').replace(/&amp;/g, "&")) || {};
  } catch {
    versions = {};
  }
  const urls = Object.entries(versions)
    .filter(([path, version]) => ASSETS.has(path) && !PRECACHE_SKIP.has(path) && /^[0-9a-f]{8}$/.test(String(version)))
    .map(([path, version]) => `${path}?v=${version}`);
  await Promise.all(urls.map(async (url) => {
    try {
      const asset = await fetch(url, { credentials: "same-origin" });
      await storeVersion(cache, url, new URL(url, self.location.origin), asset);
    } catch {
      /* cached on first use instead */
    }
  }));
}

self.addEventListener("install", (event) => {
  // Installing never fails on a flaky network; assets are cached on use too.
  event.waitUntil(precache().catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("sessionholic-") && key !== CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === "/") {
    if (!url.search) event.respondWith(networkFirst(event, request));
    return;
  }
  if (!ASSETS.has(url.pathname)) return;
  if (!url.search) event.respondWith(networkFirst(event, request));
  else if (VERSIONED.test(url.search)) event.respondWith(cacheFirst(event, request, url));
});
function networkFirst(event, request) {
  return fetch(request)
    .then((response) => {
      if (cacheable(response)) {
        const copy = response.clone();
        event.waitUntil(caches.open(CACHE).then((cache) => cache.put(request, copy)));
      }
      return response;
    })
    .catch(async () => (await (await caches.open(CACHE)).match(request)) || offlineResponse());
}
async function cacheFirst(event, request, url) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  let response;
  try {
    response = await fetch(request);
  } catch {
    return offlineResponse();
  }
  if (cacheable(response) && immutable(response)) event.waitUntil(storeVersion(cache, request, url, response.clone()));
  return response;
}

// Phone notifications. The board sends an empty push; what to show is read from the
// board itself, so no conversation text passes through a push service. Every push shows
// a notification (`userVisibleOnly`): the queued ones, else the latest one shown again
// quietly, else a generic one.
const PUSH_URL = /^\/(?:\?session=[A-Za-z0-9%._~-]{1,600})?$/;
function pushUrl(value) {
  return typeof value === "string" && PUSH_URL.test(value) ? value : "/";
}
async function pushId() {
  const subscription = await self.registration.pushManager.getSubscription();
  if (!subscription) return null;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subscription.endpoint));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, 32);
}
// null when the board could not be read (offline, signed out, an error): a new alert
// was sent, so a fresh notification sounds instead of an old one shown again.
async function pendingNotifications() {
  const id = await pushId();
  if (!id) return null;
  const response = await fetch(`/api/push/pending?id=${id}`, {
    credentials: "same-origin", cache: "no-store", headers: { "X-Board-Push": "1" },
  });
  if (!response.ok) return null;
  const data = await response.json();
  return (Array.isArray(data?.notifications) ? data.notifications : [])
    .filter((item) => item && typeof item.title === "string" && typeof item.tag === "string")
    .slice(-20);
}
async function showPushed() {
  let items = null;
  try {
    items = await pendingNotifications();
  } catch {
    items = null;
  }
  if (items?.length) {
    await Promise.all(items.map((item) => self.registration.showNotification(item.title.slice(0, 120), {
      body: String(item.body || "").slice(0, 300), tag: item.tag.slice(0, 700), renotify: true,
      icon: "/icon-192.png", data: { url: pushUrl(item.url) },
    })));
    return;
  }
  // Read, but nothing new (another wake-up already showed it): the latest notice again, quietly.
  const shown = items ? await self.registration.getNotifications() : [];
  const latest = shown[shown.length - 1];
  if (latest) {
    await self.registration.showNotification(latest.title, {
      body: latest.body, tag: latest.tag, renotify: false, icon: "/icon-192.png", data: latest.data,
    });
    return;
  }
  await self.registration.showNotification("확인할 세션이 있어요", {
    body: "눌러서 보드를 열어 주세요.", tag: "board-notice", renotify: true,
    icon: "/icon-192.png", data: { url: "/" },
  });
}
self.addEventListener("push", (event) => {
  event.waitUntil(showPushed());
});
async function openFromNotification(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const board = windows.find((client) => new URL(client.url).origin === self.location.origin);
  if (board) {
    await board.focus();
    board.postMessage({ type: "open-session", url });
    return;
  }
  await self.clients.openWindow(url);
}
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(openFromNotification(pushUrl(event.notification.data?.url)));
});
