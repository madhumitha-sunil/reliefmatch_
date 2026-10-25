"use strict";

// Registers the service worker. It only exists on HTTPS or localhost; on a plain
// http://192.168.x.x address the browser has no serviceWorker and this does nothing.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
}
