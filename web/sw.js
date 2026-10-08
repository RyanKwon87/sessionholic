"use strict";

// Sessionholic caches only public static assets. All session and terminal data remains online-only.
const CACHE = "sessionholic-shell-v1";
const SHELL = [
  "/",
  "/app.js",
  "/style.css",
  "/icon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
  "/manifest.webmanifest",
  "/vendor/xterm.js",
  "/vendor/xterm.css",
  "/vendor/addon-fit.js",
];
const ALLOWED = new Set(SHELL);
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
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
  const url = new URL(event.request.url);
  if (
    event.request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.search ||
    !ALLOWED.has(url.pathname)
  )
    return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (
          response.ok &&
          response.type !== "opaqueredirect" &&
          !response.redirected
        ) {
          const clone = response.clone();
          event.waitUntil(
            caches.open(CACHE).then((cache) => cache.put(event.request, clone)),
          );
        }
        return response;
      })
      .catch(
        async () =>
          (await (await caches.open(CACHE)).match(event.request)) ||
          new Response("연결을 확인한 뒤 다시 열어 주세요.", {
            status: 503,
            headers: { "Content-Type": "text/plain; charset=utf-8" },
          }),
      ),
  );
});
