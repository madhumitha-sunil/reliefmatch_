"use strict";

/*
 * ReliefMatch service worker.
 *
 *  - Static files (css, js, icons, Leaflet): served from cache, refreshed in the background.
 *  - Pages (/request, /login): network first, so users always get the newest version,
 *    but the last good copy is used when there is no connection.
 *  - /api/* and every non-GET request: NEVER touched. Live data and submissions always
 *    go straight to the server (the offline queue handles failures).
 *
 * Bump CACHE_VERSION whenever the static files change, so phones drop old copies.
 */
const CACHE_VERSION = "v1";
const CACHE = `reliefmatch-${CACHE_VERSION}`;
const PRECACHE = [
  "/static/css/reliefmatch.css",
  "/static/js/queue.js",
  "/static/js/request.js",
  "/static/js/login.js",
  "/static/js/pwa.js",
  "/static/offline.html",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];
const CACHEABLE_PAGES = ["/request", "/login"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key.startsWith("reliefmatch-") && key !== CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const refresh = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);
  if (cached) return cached;
  const fresh = await refresh;
  return fresh || Response.error();
}

async function networkFirstPage(request, path) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    // Do not store redirects (e.g. /request -> /login when signed out) under the wrong URL.
    if (response.ok && !response.redirected && CACHEABLE_PAGES.includes(path)) {
      cache.put(path, response.clone());
    }
    return response;
  } catch (_error) {
    // Launching the installed app opens "/", which redirects by role; offline, show the
    // last request page the volunteer used (or the login page) instead of an error.
    const candidates = path === "/" ? ["/request", "/login"] : [path];
    for (const candidate of candidates) {
      const cached = await cache.match(candidate);
      if (cached) return cached;
    }
    const fallback = await cache.match("/static/offline.html");
    return fallback || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // submissions are never intercepted
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // live data is never cached

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(networkFirstPage(request, url.pathname));
  }
});
