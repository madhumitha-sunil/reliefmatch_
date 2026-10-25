"use strict";

/*
 * ReliefMatch offline queue.
 *
 * Every request is written to IndexedDB on the phone FIRST, with a client_uuid.
 * It is then sent to the server. It is removed from the phone only after the
 * server confirms it (201 created, or 200 already_received). If the connection
 * drops, or the reply is lost, the request stays queued and is re-sent later with
 * the SAME client_uuid, so the server can never create it twice.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ReliefQueue = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const DB_NAME = "reliefmatch-queue";
  const STORE = "pending";

  // crypto.randomUUID() only exists on HTTPS/localhost. Phones on a plain
  // http://192.168.x.x hotspot address do not have it, so fall back.
  function makeUuid(cryptoObj) {
    if (cryptoObj && typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
    const bytes = new Uint8Array(16);
    if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
      cryptoObj.getRandomValues(bytes);
    } else {
      for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  const wrap = (request) =>
    new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });

  function createQueue(options) {
    const idb = options.indexedDB;
    const fetchFn = options.fetch;
    const cryptoObj = options.crypto;
    const url = options.url || "/api/requests";
    const timeoutMs = options.timeoutMs || 10000;
    let dbPromise = null;
    let flushing = null;

    function openDb() {
      if (!dbPromise) {
        dbPromise = new Promise((resolve, reject) => {
          const open = idb.open(DB_NAME, 1);
          open.onupgradeneeded = () => {
            open.result.createObjectStore(STORE, { keyPath: "client_uuid" });
          };
          open.onsuccess = () => resolve(open.result);
          open.onerror = () => reject(open.error);
        });
      }
      return dbPromise;
    }

    async function store(mode) {
      const db = await openDb();
      return db.transaction(STORE, mode).objectStore(STORE);
    }

    async function add(data) {
      const item = {
        client_uuid: makeUuid(cryptoObj),
        data,
        queued_at: new Date().toISOString(),
      };
      await wrap((await store("readwrite")).put(item));
      return item;
    }

    async function list() {
      const items = await wrap((await store("readonly")).getAll());
      // Most urgent first (severity 1 = immediate danger), then oldest first.
      return items.sort(
        (a, b) =>
          (a.data.severity || 5) - (b.data.severity || 5) ||
          a.queued_at.localeCompare(b.queued_at)
      );
    }

    async function count() {
      return wrap((await store("readonly")).count());
    }

    async function remove(uuid) {
      await wrap((await store("readwrite")).delete(uuid));
    }

    async function post(item) {
      const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        return await fetchFn(url, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...item.data, client_uuid: item.client_uuid }),
          signal: controller ? controller.signal : undefined,
        });
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    async function runFlush() {
      const result = { sent: [], dropped: [], stopped: null, remaining: 0 };
      const items = await list();
      for (const item of items) {
        let response;
        try {
          response = await post(item);
        } catch (_error) {
          result.stopped = "network"; // offline, weak signal or timeout: keep everything
          break;
        }
        const payload = await response.json().catch(() => ({}));
        if (response.status === 201 || response.status === 200) {
          await remove(item.client_uuid);
          result.sent.push({
            client_uuid: item.client_uuid,
            id: payload.id,
            already_received: payload.already_received === true,
          });
        } else if (response.status === 400 || response.status === 409) {
          // The server will never accept this one as-is (invalid, or a similar
          // request already exists nearby). Remove it so it cannot block the queue.
          await remove(item.client_uuid);
          result.dropped.push({
            client_uuid: item.client_uuid,
            status: response.status,
            error: payload.error || "Request was not accepted.",
          });
        } else if (response.status === 401) {
          result.stopped = "auth"; // signed out: keep everything
          break;
        } else if (response.status === 429) {
          result.stopped = "rate"; // too many per minute: keep and retry later
          break;
        } else {
          result.stopped = "server";
          break;
        }
      }
      result.remaining = await count();
      return result;
    }

    // Only one flush at a time, so a timer and a button press cannot double-send.
    function flush() {
      if (!flushing) {
        flushing = runFlush().finally(() => {
          flushing = null;
        });
      }
      return flushing;
    }

    return { add, list, count, flush };
  }

  return { createQueue, makeUuid };
});
