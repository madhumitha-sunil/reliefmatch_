"use strict";

(() => {
  const form = document.getElementById("request-form");
  const message = document.getElementById("request-message");
  const locateButton = document.getElementById("locate-button");
  const submitButton = form.querySelector('button[type="submit"]');
  const showMessage = (text, isError) => {
    message.textContent = text;
    message.className = `notice${isError ? " notice-error" : ""}`;
    message.hidden = false;
  };

  locateButton.addEventListener("click", () => {
    message.hidden = true;
    if (!navigator.geolocation) {
      showMessage("Location is not available in this browser. Enter coordinates instead.", true);
      return;
    }
    locateButton.disabled = true;
    locateButton.textContent = "Finding location…";
    navigator.geolocation.getCurrentPosition(
      (position) => {
        document.getElementById("lat").value = position.coords.latitude.toFixed(6);
        document.getElementById("lon").value = position.coords.longitude.toFixed(6);
        locateButton.disabled = false;
        locateButton.innerHTML = '<span aria-hidden="true">◎</span> Use my location';
        showMessage("Location added. Confirm it is within Tamil Nadu before submitting.", false);
      },
      () => {
        locateButton.disabled = false;
        locateButton.innerHTML = '<span aria-hidden="true">◎</span> Use my location';
        showMessage("We could not access your location. Allow location access or enter coordinates manually.", true);
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  });

  // ---- Offline queue -------------------------------------------------------
  const queue =
    window.indexedDB && window.ReliefQueue
      ? window.ReliefQueue.createQueue({
          indexedDB: window.indexedDB,
          fetch: window.fetch.bind(window),
          crypto: window.crypto,
        })
      : null;
  const queueBar = document.getElementById("queue-status");
  const queueText = document.getElementById("queue-text");
  const queueSend = document.getElementById("queue-send");
  let lastStop = null;

  const updateQueueBar = async () => {
    if (!queue || !queueBar) return;
    let waiting = 0;
    try {
      waiting = await queue.count();
    } catch (_error) {
      return;
    }
    if (waiting === 0) {
      queueBar.hidden = true;
      return;
    }
    const noun = waiting === 1 ? "request is" : "requests are";
    let reason = "Waiting for a connection.";
    if (lastStop === "auth") reason = "Sign in again to send.";
    if (lastStop === "rate") reason = "Sending in small batches.";
    queueText.textContent = `${waiting} ${noun} saved on this phone. ${reason}`;
    queueBar.hidden = false;
  };

  const syncQueue = async () => {
    if (!queue) return null;
    let result;
    try {
      result = await queue.flush();
    } catch (_error) {
      return null;
    }
    lastStop = result.stopped;
    await updateQueueBar();
    return result;
  };

  queueSend?.addEventListener("click", async () => {
    queueSend.disabled = true;
    const result = await syncQueue();
    queueSend.disabled = false;
    if (result && result.sent.length) {
      showMessage(`${result.sent.length} saved request(s) sent to the coordinator.`, false);
    } else if (result && result.stopped === "auth") {
      showMessage("You are signed out. Sign in again and the saved requests will be sent.", true);
    } else if (result && result.stopped === "network") {
      showMessage("Still no connection. Your requests are safe on this phone.", true);
    }
  });

  window.addEventListener("online", syncQueue);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) syncQueue();
  });
  window.setInterval(syncQueue, 15000);
  syncQueue();

  const resetSubmitButton = () => {
    submitButton.disabled = false;
    submitButton.innerHTML = "<span>Submit aid request</span><span aria-hidden=\"true\">→</span>";
  };

  const clearForm = () => {
    form.reset();
    form.elements.severity.value = "3";
  };

  // Old behaviour, used only if this browser has no IndexedDB.
  const sendDirect = async (data) => {
    const response = await fetch("/api/requests", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Your request could not be submitted.");
    showMessage(`Request #${payload.id} is recorded. A coordinator can now review this need.`, false);
    clearForm();
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.hidden = true;
    if (!form.reportValidity()) return;
    const data = {
      type: form.elements.type.value,
      people: Number(form.elements.people.value),
      severity: Number(form.elements.severity.value),
      lat: Number(form.elements.lat.value),
      lon: Number(form.elements.lon.value)
    };
    if (!Number.isInteger(data.people) || data.people < 1 || !Number.isInteger(data.severity)) {
      showMessage("Enter a whole number of people and choose a severity from 1 to 5.", true);
      return;
    }
    if (data.lat < 8 || data.lat > 13.6 || data.lon < 76 || data.lon > 80.4) {
      showMessage("Coordinates must be within Tamil Nadu (latitude 8–13.6, longitude 76–80.4).", true);
      return;
    }
    submitButton.disabled = true;
    submitButton.innerHTML = "<span>Submitting request…</span>";
    try {
      if (!queue) {
        await sendDirect(data);
        return;
      }
      // 1) Save on the phone first, so nothing is lost if the signal drops.
      const item = await queue.add(data);
      clearForm();
      // 2) Try to send everything waiting (this request and any older ones).
      const result = await syncQueue();
      const sent = result && result.sent.find((entry) => entry.client_uuid === item.client_uuid);
      const dropped = result && result.dropped.find((entry) => entry.client_uuid === item.client_uuid);
      if (sent) {
        showMessage(
          sent.already_received
            ? `Request #${sent.id} was already recorded. Nothing was duplicated.`
            : `Request #${sent.id} is recorded. A coordinator can now review this need.`,
          false
        );
      } else if (dropped) {
        showMessage(dropped.error, true);
      } else if (result && result.stopped === "auth") {
        showMessage("Saved on this phone. You are signed out: sign in again and it will be sent.", true);
      } else {
        showMessage("Saved on this phone. It will be sent automatically when the connection returns.", false);
      }
    } catch (error) {
      showMessage(error.message || "Unable to connect. Your request has not been submitted.", true);
    } finally {
      resetSubmitButton();
    }
  });

  document.querySelector("[data-logout]")?.addEventListener("click", async () => {
    try {
      await fetch("/api/logout", { method: "POST", credentials: "include" });
    } finally {
      window.location.assign("/login");
    }
  });
})();
