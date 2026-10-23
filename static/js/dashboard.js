"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
  const typeLabel = (type) => ({ water: "Water", food: "Food", medicine: "Medicine", shelter: "Shelter" }[type] || String(type || "Aid"));
  const dateValue = (value) => value ? new Date(value) : null;
  const elapsedMinutes = (value) => {
    const date = dateValue(value);
    return date && !Number.isNaN(date.getTime()) ? Math.max(0, (Date.now() - date.getTime()) / 60000) : 0;
  };
  const formatAge = (minutes) => minutes < 1 ? "Just now" : minutes < 60 ? `${Math.floor(minutes)} min waiting` : `${Math.floor(minutes / 60)}h ${Math.floor(minutes % 60)}m waiting`;
  const formatTime = (value) => {
    const date = dateValue(value);
    return date && !Number.isNaN(date.getTime()) ? date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "Time unavailable";
  };
  const state = { suggestions: [], requests: [], resources: [], audit: [], map: null, mapLayers: [], mapInitialized: false, refreshTimer: null, overrideRequest: null, pendingAtClick: false };
  const errorBox = $("#dashboard-error");
  const criticalBanner = $("#critical-banner");

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "include",
      ...options,
      headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) }
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Your session has ended. Sign in to continue.");
    }
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
    return payload;
  }
  function notifyError(error) {
    errorBox.textContent = error.message || "Could not complete that action. Try again.";
    errorBox.hidden = false;
  }
  function clearError() { errorBox.hidden = true; }
  function safeRead(key) {
    try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch (_error) { return {}; }
  }
  function markAlert(key) {
    try {
      const alerts = safeRead("reliefmatch-alerts-v1");
      if (alerts[key]) return false;
      alerts[key] = Date.now();
      localStorage.setItem("reliefmatch-alerts-v1", JSON.stringify(alerts));
      return true;
    } catch (_error) { return true; }
  }
  function beep(loud = false) {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      const context = new AudioContext();
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = loud ? 760 : 590;
      gain.gain.setValueAtTime(loud ? .13 : .065, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + (loud ? .65 : .34));
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + (loud ? .65 : .34));
      oscillator.onended = () => context.close();
    } catch (_error) { /* Browser sound can be blocked until a user gesture. */ }
  }
  function speakOnce(request) {
    try {
      if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) return;
      const utterance = new SpeechSynthesisUtterance(`Critical aid request. ${typeLabel(request.type)} needed for ${request.people} people.`);
      utterance.rate = .92;
      utterance.volume = .85;
      window.speechSynthesis.speak(utterance);
    } catch (_error) { /* Voice alert is optional. */ }
  }
  function checkAlerts(suggestions, requests) {
    const critical = suggestions.filter((item) => Number(item.urgency) > 1000);
    criticalBanner.hidden = critical.length === 0;
    if (critical.length) {
      $("#critical-copy").textContent = `${critical.length} pending ${critical.length === 1 ? "request needs" : "requests need"} immediate review. Highest urgency ${Math.round(Math.max(...critical.map((item) => Number(item.urgency) || 0)))}.`;
    }
    critical.forEach((request) => {
      if (markAlert(`critical:${request.id}`)) {
        beep(false);
        speakOnce(request);
      }
    });
    suggestions.forEach((request) => {
      const age = elapsedMinutes(request.created_at);
      if (age >= 30 && markAlert(`age-red:${request.id}`)) beep(true);
      else if (age >= 15) markAlert(`age-orange:${request.id}`);
    });
    requests.filter((request) => request.status === "matched").forEach((request) => {
      if (elapsedMinutes(request.matched_at) >= 60) markAlert(`stale:${request.id}`);
    });
  }
  function resourceById(id) { return state.resources.find((resource) => Number(resource.id) === Number(id)); }
  function suggestionsFor(request) {
    return request.matches || [];
  }
  function escalation(request) {
    const age = elapsedMinutes(request.created_at);
    return age >= 30 ? "red" : age >= 15 ? "orange" : "";
  }
  function allocationMarkup(request) {
    const matches = suggestionsFor(request);
    const lines = matches.map((match) => {
      const resource = resourceById(match.resource_id);
      const provider = resource ? `${resource.provider} · ` : "";
      return `<div class="resource-line"><strong>${escapeHtml(provider)}Resource #${escapeHtml(match.resource_id)} · ${escapeHtml(match.quantity)} people</strong><span class="resource-distance">${Number(match.distance_km || 0).toFixed(1)} km <button class="resource-reject" type="button" data-action="reject" data-request="${escapeHtml(request.id)}" data-resource="${escapeHtml(match.resource_id)}" aria-label="Reject resource ${escapeHtml(match.resource_id)} for request ${escapeHtml(request.id)}">Reject</button></span></div>`;
    }).join("");
    const shortfall = Number(request.shortfall || 0);
    return `<div class="allocation-block"><div class="allocation-head"><span>Suggested resources</span><span>${matches.length} ${matches.length === 1 ? "location" : "locations"}</span></div>${matches.length ? `<div class="resource-list">${lines}</div>` : '<div class="no-match">No available resource of this type.</div>'}${shortfall > 0 ? `<div class="shortfall">Shortfall · ${shortfall} people still need support</div>` : ""}</div>`;
  }
  function pendingCard(request) {
    const age = elapsedMinutes(request.created_at);
    const tier = escalation(request);
    const urgency = Number(request.urgency) || 0;
    const severityClass = Number(request.severity) <= 2 ? "severity-high" : "";
    const tierText = tier === "red" ? "Escalated · 30+ min" : tier === "orange" ? "Escalating · 15+ min" : formatAge(age);
    const tierClass = tier ? ` ${tier}` : "";
    return `<article class="request-card-row${tier ? ` escalation-${tier}` : ""}" data-request-card="${escapeHtml(request.id)}">
      <div class="request-mainline"><div class="request-title-wrap"><h3 class="request-title">${escapeHtml(typeLabel(request.type))}<span class="request-id">#${escapeHtml(request.id)}</span></h3><div class="request-facts"><span>${escapeHtml(request.people)} people</span><span>Severity ${escapeHtml(request.severity)}/5</span><span>${escapeHtml(tierText)}</span></div></div><div><span class="severity-chip ${severityClass}">S${escapeHtml(request.severity)} · ${Number(request.severity) <= 2 ? "Critical" : "Severity"}</span><div style="margin-top:5px"><span class="urgency-chip">Urgency ${Math.round(urgency)}</span></div></div></div>
      ${allocationMarkup(request)}
      <div class="request-actions"><button class="button button-primary button-small" type="button" data-action="approve" data-request="${escapeHtml(request.id)}">Approve</button><button class="button button-secondary button-small" type="button" data-action="override" data-request="${escapeHtml(request.id)}">Override</button><span class="request-location">${Number(request.lat).toFixed(3)}, ${Number(request.lon).toFixed(3)}</span></div>
    </article>`;
  }
  function matchedCard(request) {
    const age = elapsedMinutes(request.matched_at);
    const stale = request.matched_at && age >= 60;
    const delivered = request.status === "delivered";
    return `<article class="matched-row">
      <div class="matched-info"><h3 class="request-title">${escapeHtml(typeLabel(request.type))}<span class="request-id">#${escapeHtml(request.id)}</span>${stale && !delivered ? '<span class="stale-label">STALE</span>' : ""}</h3><div class="request-facts"><span>${escapeHtml(request.people)} people</span><span>${delivered ? "Delivered" : `Matched ${formatAge(age)} ago`}</span><span>${formatTime(request.matched_at)}</span></div></div>
      <div class="matched-actions">${delivered ? '<span class="delivered-label">Delivered</span>' : `<button class="button button-primary button-small" type="button" data-action="deliver" data-request="${escapeHtml(request.id)}">Deliver</button><button class="button button-secondary button-small" type="button" data-action="undo" data-request="${escapeHtml(request.id)}">Undo</button>`}</div>
    </article>`;
  }
  function emptyState(title, copy) {
    return `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">—</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(copy)}</p></div>`;
  }
  function renderPending() {
    const container = $("#pending-list");
    const sorted = [...state.suggestions].sort((a, b) => Number(b.urgency) - Number(a.urgency));
    container.innerHTML = sorted.length ? sorted.map(pendingCard).join("") : emptyState("No pending requests", "New field requests will appear here for coordinator review.");
    $("#pending-count").textContent = sorted.length;
    $("#pending-tab-count").textContent = sorted.length;
    $("#people-count").textContent = sorted.reduce((total, request) => total + Number(request.people || 0), 0).toLocaleString();
  }
  function renderMatched() {
    const matched = state.requests.filter((request) => request.status === "matched" || request.status === "delivered");
    $("#matched-list").innerHTML = matched.length ? matched.map(matchedCard).join("") : emptyState("Nothing matched yet", "Approved requests will appear here until they are delivered.");
    $("#matched-count").textContent = matched.filter((request) => request.status === "matched").length;
    $("#matched-tab-count").textContent = matched.length;
  }
  function renderAudit() {
    const target = $("#audit-list");
    target.innerHTML = state.audit.length ? state.audit.map((entry) => {
      let details = entry.details || "";
      try { if (typeof details === "string") details = JSON.parse(details); } catch (_error) {}
      const readable = typeof details === "object" ? Object.entries(details).map(([key, value]) => `${key.replaceAll("_", " ")}: ${typeof value === "object" ? JSON.stringify(value) : value}`).join(" · ") : String(details);
      return `<article class="audit-entry"><span class="audit-action">${escapeHtml(String(entry.action || "activity").replaceAll("_", " "))}</span><span class="audit-detail">${escapeHtml(readable)}</span><span class="audit-meta">${escapeHtml(entry.user || "System")} · ${escapeHtml(formatTime(entry.timestamp))}</span></article>`;
    }).join("") : emptyState("No audit events yet", "Coordinator decisions and field updates will be recorded here.");
  }
  function updateMap() {
    const target = $("#map");
    const L = window.L;
    if (!L) {
      target.innerHTML = '<div class="empty-state"><h3>Map library unavailable</h3><p>The request and resource list remains available below.</p></div>';
      return;
    }
    if (!state.mapInitialized) {
      state.map = L.map(target, { zoomControl: true, scrollWheelZoom: false }).setView([11.0, 78.0], 7);
      const tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 18
      });
      tiles.on("tileerror", () => { $("#tiles-note").hidden = false; });
      tiles.on("load", () => { $("#tiles-note").hidden = true; });
      tiles.addTo(state.map);
      state.mapInitialized = true;
    }
    state.mapLayers.forEach((layer) => state.map.removeLayer(layer));
    state.mapLayers = [];
    const requestIcon = L.divIcon({ className: "leaflet-div-icon", html: '<span class="map-pin map-pin-request"></span>', iconSize: [20, 20], iconAnchor: [10, 18] });
    const resourceIcon = L.divIcon({ className: "leaflet-div-icon", html: '<span class="map-pin map-pin-resource"></span>', iconSize: [20, 20], iconAnchor: [10, 18] });
    const bounds = [];
    state.resources.forEach((resource) => {
      const marker = L.marker([resource.lat, resource.lon], { icon: resourceIcon }).bindPopup(`<strong>${escapeHtml(resource.provider)}</strong><br>${escapeHtml(typeLabel(resource.type))} · ${escapeHtml(resource.quantity)} available`);
      marker.addTo(state.map); state.mapLayers.push(marker); bounds.push([resource.lat, resource.lon]);
    });
    state.suggestions.forEach((request) => {
      const point = [request.lat, request.lon];
      const marker = L.marker(point, { icon: requestIcon }).bindPopup(`<strong>Request #${escapeHtml(request.id)} · ${escapeHtml(typeLabel(request.type))}</strong><br>${escapeHtml(request.people)} people · Severity ${escapeHtml(request.severity)}`);
      marker.addTo(state.map); state.mapLayers.push(marker); bounds.push(point);
      suggestionsFor(request).forEach((match) => {
        const resource = resourceById(match.resource_id);
        if (!resource) return;
        const line = L.polyline([point, [resource.lat, resource.lon]], { color: "#538879", weight: 2, opacity: .75, dashArray: "5 6" });
        line.addTo(state.map); state.mapLayers.push(line);
      });
    });
    if (bounds.length > 1) state.map.fitBounds(bounds, { padding: [24, 24], maxZoom: 11 });
    else if (bounds.length === 1) state.map.setView(bounds[0], 9);
    window.setTimeout(() => state.map.invalidateSize(), 0);
  }
  async function refresh() {
    try {
      const [suggestions, requests, resources] = await Promise.all([
        api("/api/suggestions"), api("/api/requests"), api("/api/resources")
      ]);
      state.suggestions = Array.isArray(suggestions) ? suggestions : [];
      state.requests = Array.isArray(requests) ? requests : [];
      state.resources = Array.isArray(resources) ? resources : [];
      checkAlerts(state.suggestions, state.requests);
      renderPending(); renderMatched(); updateMap();
      if ($("#tab-audit").getAttribute("aria-selected") === "true") await loadAudit();
      clearError();
      $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
      $("#resource-count").textContent = state.resources.length;
    } catch (error) {
      notifyError(error);
      if (!state.suggestions.length && !state.requests.length) {
        $("#pending-list").innerHTML = `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">!</span><h3>Live data unavailable</h3><p>${escapeHtml(error.message)} Refresh to try again.</p><button class="button button-secondary button-small" type="button" data-action="refresh">Retry</button></div>`;
      }
    }
  }
  function setTab(name) {
    ["pending", "matched", "audit"].forEach((tab) => {
      const active = name === tab;
      $(`#tab-${tab}`).classList.toggle("active", active);
      $(`#tab-${tab}`).setAttribute("aria-selected", String(active));
      $(`#${tab}-panel`).hidden = !active;
    });
  }
  async function performAction(action, requestId, resourceId) {
    const routes = {
      approve: [`/api/matches/${requestId}/approve`, "POST"],
      reject: [`/api/matches/${requestId}/reject`, "POST"],
      deliver: [`/api/matches/${requestId}/deliver`, "POST"],
      undo: [`/api/matches/${requestId}/undo`, "POST"],
      override: [`/api/matches/${requestId}/override`, "POST"]
    };
    const [url, method] = routes[action];
    const body = action === "reject" ? { resource_id: Number(resourceId) } : action === "override" ? { resource_id: Number(resourceId) } : undefined;
    try {
      clearError();
      await api(url, { method, ...(body ? { body: JSON.stringify(body) } : {}) });
      await refresh();
    } catch (error) { notifyError(error); }
  }
  function openOverride(requestId) {
    const request = state.suggestions.find((item) => Number(item.id) === Number(requestId));
    if (!request) return;
    state.overrideRequest = request;
    const suggestedIds = new Set(suggestionsFor(request).map((match) => Number(match.resource_id)));
    const alternatives = state.resources.filter((resource) => resource.type === request.type && Number(resource.quantity) >= Number(request.people) && !suggestedIds.has(Number(resource.id)));
    $("#override-context").textContent = `Request #${request.id} needs ${request.people} people covered by a different ${typeLabel(request.type).toLowerCase()} resource.`;
    $("#override-resource").innerHTML = alternatives.length
      ? alternatives.map((resource) => `<option value="${escapeHtml(resource.id)}">${escapeHtml(resource.provider)} · Resource #${escapeHtml(resource.id)} · ${escapeHtml(resource.quantity)} available</option>`).join("")
      : '<option value="">No alternative with enough stock</option>';
    $("#override-submit").disabled = alternatives.length === 0;
    $("#override-error").hidden = true;
    $("#override-dialog").showModal();
  }
  document.addEventListener("click", async (event) => {
    const tab = event.target.closest("[role=tab]");
    if (tab) { setTab(tab.id.replace("tab-", "")); if (tab.id === "tab-audit") loadAudit(); return; }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "refresh") return refresh();
    if (action === "override") return openOverride(button.dataset.request);
    if (action === "reject") {
      if (!window.confirm(`Reject the suggested resource #${button.dataset.resource} for request #${button.dataset.request}?`)) return;
      return performAction(action, button.dataset.request, button.dataset.resource);
    }
    await performAction(action, button.dataset.request);
  });
  async function loadAudit() {
    try { state.audit = await api("/api/audit"); renderAudit(); }
    catch (error) { $("#audit-list").innerHTML = `<div class="empty-state"><h3>Audit log unavailable</h3><p>${escapeHtml(error.message)}</p></div>`; }
  }
  $("#override-submit").addEventListener("click", async () => {
    const resourceId = $("#override-resource").value;
    if (!resourceId || !state.overrideRequest) return;
    try {
      await api(`/api/matches/${state.overrideRequest.id}/override`, { method: "POST", body: JSON.stringify({ resource_id: Number(resourceId) }) });
      $("#override-dialog").close();
      await refresh();
    } catch (error) {
      const box = $("#override-error");
      box.textContent = error.message;
      box.hidden = false;
    }
  });
  $("#refresh-button").addEventListener("click", refresh);
  $("#current-date").textContent = new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  document.querySelector("[data-logout]")?.addEventListener("click", async () => {
    try { await fetch("/api/logout", { method: "POST", credentials: "include" }); } finally { window.location.assign("/login"); }
  });
  refresh();
  state.refreshTimer = window.setInterval(refresh, 10000);
})();
