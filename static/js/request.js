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
      const response = await fetch("/api/requests", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data)
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "Your request could not be submitted.");
      showMessage(`Request #${payload.id} is recorded. A coordinator can now review this need.`, false);
      form.reset();
      form.elements.severity.value = "3";
    } catch (error) {
      showMessage(error.message || "Unable to connect. Your request has not been submitted.", true);
    } finally {
      submitButton.disabled = false;
      submitButton.innerHTML = "<span>Submit aid request</span><span aria-hidden=\"true\">→</span>";
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
