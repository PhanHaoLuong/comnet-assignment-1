/* login.js — the login gate.
 *
 * A self-contained module: it owns the form, validation, retry UX and the
 * mapping of gateway reasons to readable text. The shell only calls
 * LoginView.init({onLogin}), .show(message) and .hide(); it never reads the
 * form or the error element itself.
 *
 * Deliberately no visual work here: markup and classes come from index.html /
 * style.css so the UI/UX pass can restyle it without touching this file.
 */
(function (global) {
  "use strict";

  var USERNAME_RE = /^[a-z0-9_]{3,20}$/;
  var MAX_PASSWORD_BYTES = 128;

  var FRIENDLY = {
    bad_credentials: "Wrong username or password.",
    name_taken: "That username is already online.",
    bad_username: "Usernames are 3-20 characters: a-z, 0-9, _.",
    tracker_unreachable: "The tracker is not reachable.",
    login_timeout: "The tracker did not answer in time.",
    connection_lost: "The connection to the tracker was lost.",
    bad_reply: "The tracker sent an unexpected reply.",
    unexpected_reply: "The tracker sent an unexpected reply.",
    bad_request: "Invalid login request.",
    already_logged_in: "This client is already logged in."
  };

  var form = null;
  var usernameInput = null;
  var passwordInput = null;
  var submitButton = null;
  var errorEl = null;
  var onLogin = null;
  var busy = false;
  var visible = false;

  // --------------------------------------------------------------- helpers

  function byId(id) {
    return document.getElementById(id);
  }

  function setError(text) {
    errorEl.textContent = text || "";
  }

  function clearInvalid(field) {
    [usernameInput, passwordInput].forEach(function (input) {
      if (!input) return;
      if (!field || field === input.name) input.removeAttribute("aria-invalid");
    });
  }

  function markInvalid(field) {
    var input = field === "password" ? passwordInput : usernameInput;
    if (!input) return;
    input.setAttribute("aria-invalid", "true");
    input.focus();
  }

  function byteLength(text) {
    if (typeof global.TextEncoder === "function") {
      return new global.TextEncoder().encode(text).length;
    }
    return text.length;
  }

  function setBusy(value) {
    busy = value;
    if (!form || !submitButton) return;
    submitButton.disabled = value;
    form.setAttribute("aria-busy", value ? "true" : "false");
  }

  function readJson(response) {
    return response
      .json()
      .catch(function () {
        return {};
      });
  }

  function finish(state) {
    if (!state) {
      setError("The gateway did not return a session.");
      return;
    }
    passwordInput.value = "";
    clearInvalid();
    setError("");
    if (onLogin) onLogin(state);
  }

  // -------------------------------------------------------------- validation

  function validate(username, password) {
    if (!username) {
      return { field: "username", message: "Enter a username." };
    }
    if (!USERNAME_RE.test(username)) {
      return {
        field: "username",
        message: FRIENDLY.bad_username
      };
    }
    if (!password) {
      return { field: "password", message: "Enter a password." };
    }
    if (byteLength(password) > MAX_PASSWORD_BYTES) {
      return {
        field: "password",
        message: "Password is too long (128 bytes max)."
      };
    }
    return null;
  }

  // ------------------------------------------------------------------ submit

  async function submit(event) {
    event.preventDefault();
    if (busy) return;

    clearInvalid();
    setError("");

    var username = usernameInput.value.trim().toLowerCase();
    var password = passwordInput.value;
    usernameInput.value = username;

    var problem = validate(username, password);
    if (problem) {
      setError(problem.message);
      markInvalid(problem.field);
      return;
    }

    setBusy(true);
    try {
      var response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username, password: password })
      });
      var data = await readJson(response);

      // The daemon was started with --username: adopt its session instead of
      // asking the user to log in again.
      if (response.status === 409) {
        var current = await fetch("/api/state", { cache: "no-store" })
          .then(readJson)
          .catch(function () {
            return {};
          });
        if (current.ok && current.state && current.state.status === "authenticated") {
          finish(current.state);
          return;
        }
        setError(FRIENDLY.already_logged_in);
        return;
      }

      if (!response.ok || !data.ok) {
        var reason = data.reason || "http_" + response.status;
        setError(FRIENDLY[reason] || "Login failed: " + reason);
        if (reason === "bad_credentials") {
          passwordInput.value = "";
          markInvalid("password");
        }
        return;
      }

      finish(data.state);
    } catch (error) {
      setError("Cannot reach the local gateway.");
    } finally {
      setBusy(false);
    }
  }

  // ------------------------------------------------------------- public API

  function init(options) {
    onLogin = options && options.onLogin;

    form = byId("login-form");
    usernameInput = form.elements.namedItem("username");
    passwordInput = form.elements.namedItem("password");
    submitButton = byId("login-button");
    errorEl = byId("login-error");

    form.addEventListener("submit", submit);

    // Clear the invalid marker as soon as the user edits the field.
    [usernameInput, passwordInput].forEach(function (input) {
      input.addEventListener("input", function () {
        clearInvalid(input.name);
      });
    });
  }

  function show(message) {
    var wasHidden = !visible;
    visible = true;

    byId("login-view").hidden = false;
    byId("app-view").hidden = true;

    // An omitted message keeps the current error. That is what the shell
    // passes for status events, so a 401 message is never wiped by a
    // "connecting" / "error" event that arrives right after it.
    if (typeof message === "string" && message) setError(message);

    if (wasHidden) {
      if (usernameInput.value.trim()) passwordInput.focus();
      else usernameInput.focus();
    }
  }

  function hide() {
    visible = false;
    byId("login-view").hidden = true;
    byId("app-view").hidden = false;
    clearInvalid();
    setError("");
  }

  global.LoginView = {
    init: init,
    show: show,
    hide: hide
  };
})(window);
