/* Minimal login view. Owner: Auth. Replace without changing app.js's hooks. */
(function () {
  "use strict";

  var form, errorEl, button, onLogin;

  var FRIENDLY = {
    bad_credentials: "Wrong username or password.",
    name_taken: "That username is already online.",
    bad_username: "Usernames are 3-20 characters: a-z, 0-9, _.",
    tracker_unreachable: "The tracker is not reachable.",
    login_timeout: "The tracker did not answer.",
    bad_request: "Invalid login request.",
    already_logged_in: "Already logged in."
  };

  function setError(text) {
    errorEl.textContent = text || "";
  }

  function show(message) {
    document.getElementById("login-view").hidden = false;
    document.getElementById("app-view").hidden = true;
    setError(message || "");
    var name = form.elements.namedItem("username");
    if (name && !name.value) name.focus();
  }

  function hide() {
    document.getElementById("login-view").hidden = true;
    document.getElementById("app-view").hidden = false;
    setError("");
  }

  async function submit(event) {
    event.preventDefault();
    setError("");

    var username = (form.elements.namedItem("username").value || "").trim().toLowerCase();
    var password = form.elements.namedItem("password").value || "";
    if (!username || !password) {
      setError("Enter a username and password.");
      return;
    }

    button.disabled = true;
    try {
      var response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username, password: password })
      });
      var data = await response.json().catch(function () { return {}; });

      if (response.status === 409) {
        // Daemon was started with --username: just pick up its state.
        var current = await fetch("/api/state").then(function (r) { return r.json(); });
        if (current.ok && current.state && current.state.status === "authenticated") {
          await onLogin(current.state);
          return;
        }
      }

      if (!response.ok || !data.ok) {
        setError(FRIENDLY[data.reason] || ("Login failed: " + (data.reason || "unknown error")));
        return;
      }

      form.elements.namedItem("password").value = "";
      await onLogin(data.state);
    } catch (err) {
      setError("Cannot reach the local gateway.");
    } finally {
      button.disabled = false;
    }
  }

  function init(options) {
    onLogin = options.onLogin;
    form = document.getElementById("login-form");
    errorEl = document.getElementById("login-error");
    button = document.getElementById("login-button");
    form.addEventListener("submit", submit);
  }

  window.LoginView = { init: init, show: show, hide: hide };
})();
