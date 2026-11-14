/* chat.js — global chat panel (sidebar).
 *
 * A self-contained view module: it owns the message log and composer, renders
 * history from the daemon snapshots, and appends live `chat` SSE events. It
 * never echoes locally: the POST response only means "accepted"; the message
 * appears when the tracker broadcasts the CHAT event back.
 *
 * The shell mounts it through P2P.registerView({slot: "sidebar"}) and forwards
 * setState + every SSE event. See web/README.md.
 */
(function (global) {
  "use strict";

  var MAX_RENDERED = 200;
  var FRIENDLY = {
    not_logged_in: "Log in again to use chat.",
    empty_text: "Write a message first.",
    bad_request: "Invalid message.",
    text_too_long: "Message is too long."
  };

  var root = null;
  var onError = null;
  var els = {};
  var username = "";
  var status = "idle";
  var messages = [];
  var seen = Object.create(null);

  // --------------------------------------------------------------- helpers

  function messageKey(message) {
    if (!message) return "";
    if (message.id) return "id:" + message.id;
    if (message.gseq !== undefined) return "gseq:" + message.gseq;
    return "raw:" + (message.from || "") + ":" + (message.ts || "") + ":" +
      (message.text || "");
  }

  function byGseq(a, b) {
    var left = typeof a.gseq === "number" ? a.gseq : Infinity;
    var right = typeof b.gseq === "number" ? b.gseq : Infinity;
    return left - right;
  }

  function formatTime(ts) {
    var date = new Date(ts);
    if (isNaN(date.getTime())) return "";
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function showError(text) {
    if (els.error) els.error.textContent = text || "";
  }

  function isNearBottom() {
    if (!els.log) return true;
    return els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 24;
  }

  function scrollToBottom() {
    if (els.log) els.log.scrollTop = els.log.scrollHeight;
  }

  // -------------------------------------------------------------- rendering

  function renderMessage(message) {
    var item = document.createElement("div");
    var meta = document.createElement("div");
    var from = document.createElement("span");
    var time = document.createElement("span");
    var text = document.createElement("div");

    item.className = "chat-message";
    if (message.from && message.from === username) item.classList.add("own");

    meta.className = "chat-meta";
    from.className = "chat-from";
    from.textContent = message.from === username ? "you" : (message.from || "peer");
    time.className = "chat-time";
    time.textContent = formatTime(message.ts);

    text.className = "chat-text";
    text.textContent = message.text || "";

    meta.appendChild(from);
    meta.appendChild(time);
    item.appendChild(meta);
    item.appendChild(text);
    return item;
  }

  function renderAll() {
    if (!els.log) return;

    els.log.textContent = "";

    if (!messages.length) {
      var empty = document.createElement("p");
      empty.className = "chat-empty muted";
      empty.textContent = "No messages yet.";
      els.log.appendChild(empty);
      return;
    }

    messages.forEach(function (message) {
      els.log.appendChild(renderMessage(message));
    });
    scrollToBottom();
  }

  function appendMessage(message) {
    var key = messageKey(message);
    if (!key || seen[key]) return false;

    seen[key] = true;
    messages.push(message);
    messages.sort(byGseq);
    if (messages.length > MAX_RENDERED) messages.shift();

    if (!els.log) return true;

    var empty = els.log.querySelector(".chat-empty");
    if (empty) empty.remove();

    var stick = isNearBottom();
    els.log.appendChild(renderMessage(message));
    if (stick) scrollToBottom();

    while (els.log.querySelectorAll(".chat-message").length > MAX_RENDERED) {
      var oldest = els.log.querySelector(".chat-message");
      if (!oldest) break;
      oldest.remove();
    }
    return true;
  }

  function resetMessages(history) {
    messages = [];
    seen = Object.create(null);

    (history || []).forEach(function (entry) {
      if (!entry || typeof entry.text !== "string") return;
      var key = messageKey(entry);
      if (!key || seen[key]) return;
      seen[key] = true;
      messages.push(entry);
    });

    messages.sort(byGseq);
    if (messages.length > MAX_RENDERED) {
      messages = messages.slice(messages.length - MAX_RENDERED);
    }
    renderAll();
  }

  // --------------------------------------------------------------- composer

  function updateComposer() {
    if (!els.input || !els.send) return;
    var enabled = status === "authenticated";
    els.input.disabled = !enabled;
    els.send.disabled = !enabled;
    els.input.placeholder = enabled ? "Message everyone..." : "Log in to chat";
  }

  async function submit(event) {
    event.preventDefault();

    if (status !== "authenticated") {
      showError("Chat needs a tracker connection.");
      return;
    }

    var text = (els.input.value || "").trim();
    if (!text) {
      showError("Write a message first.");
      els.input.focus();
      return;
    }

    showError("");
    els.input.disabled = true;
    els.send.disabled = true;
    try {
      var response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text })
      });
      var data = await response.json().catch(function () {
        return {};
      });

      if (!response.ok || !data.ok) {
        var reason = data.reason || "http_" + response.status;
        showError(FRIENDLY[reason] || "Send failed: " + reason);
        if (response.status === 401) status = "disconnected";
        return;
      }

      // No local echo: clear the box; the chat event will render it.
      els.input.value = "";
      els.input.focus();
    } catch (error) {
      showError("Cannot reach the local gateway.");
      if (onError) onError("Cannot reach the local gateway.");
    } finally {
      updateComposer();
    }
  }

  // ------------------------------------------------------------- public API

  function init(rootEl, options) {
    root = rootEl;
    onError = (options && options.onError) || function () {};

    root.innerHTML =
      '<div class="chat-head"><h2>Global chat</h2></div>' +
      '<div class="chat-log" id="chat-log" aria-live="polite" aria-relevant="additions"></div>' +
      '<form class="chat-form" id="chat-form">' +
        '<input id="chat-input" type="text" maxlength="4096" autocomplete="off" ' +
               'placeholder="Message everyone..." aria-label="Global message">' +
        '<button type="submit" id="chat-send">Send</button>' +
      '</form>' +
      '<p class="error chat-error" id="chat-error" role="alert"></p>';

    els = {
      log: root.querySelector("#chat-log"),
      form: root.querySelector("#chat-form"),
      input: root.querySelector("#chat-input"),
      send: root.querySelector("#chat-send"),
      error: root.querySelector("#chat-error")
    };

    els.form.addEventListener("submit", submit);
    els.input.addEventListener("input", function () {
      showError("");
    });

    renderAll();
    updateComposer();
  }

  function setState(state) {
    if (!state) return;
    username = state.username || username;
    status = state.status || status;
    resetMessages(state.history || []);
    updateComposer();
  }

  function onEvent(event) {
    if (!event) return;

    if (event.kind === "chat" && event.message) {
      appendMessage(event.message);
      return;
    }

    if (event.kind === "history") {
      resetMessages(event.history || []);
      return;
    }

    if (event.kind === "status") {
      status = event.status || status;
      if (event.username) username = event.username;
      updateComposer();
    }
  }

  global.Chat = {
    init: init,
    setState: setState,
    onEvent: onEvent
  };
})(window);
