"use strict";

(() => {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("login-error");
  const submit = form.querySelector('button[type="submit"]');
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    const username = form.elements.username.value.trim();
    const password = form.elements.password.value;
    if (!username || !password) {
      errorBox.textContent = "Enter your username and password to continue.";
      errorBox.hidden = false;
      return;
    }
    submit.disabled = true;
    submit.innerHTML = "<span>Signing in…</span>";
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "Sign in could not be completed. Try again.");
      window.location.assign(payload.user && payload.user.role === "coordinator" ? "/dashboard" : "/request");
    } catch (error) {
      errorBox.textContent = error.message || "Unable to connect. Check your connection and try again.";
      errorBox.hidden = false;
      submit.disabled = false;
      submit.innerHTML = "<span>Sign in</span><span aria-hidden=\"true\">→</span>";
    }
  });
})();
