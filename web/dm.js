/* Direct-message module skeleton. Owner: DM friend.
 *
 * The shell calls:
 *   DM.init(rootEl, {onError})
 *   DM.setPeers(peers)                 // {username: {username, addr}}
 *   DM.setState({username, dms, peers})// dms: {peer: [message, ...]}
 *   DM.onEvent(ev)                     // ev.kind === "dm"
 *   DM.open(username)
 *
 * Messages are never echoed locally: an outgoing message appears only when
 * the gateway sends the `dm` SSE event with direction "outgoing".
 */
(function () {
  "use strict";

  var MAX_RENDERED_PER_PEER = 200;
  var FRIENDLY = {
    peer_offline: "That peer is offline.",
    peer_unreachable: "Could not reach that peer directly.",
    send_failed: "Could not send the message.",
    text_too_long: "Message is too long (4096 bytes max).",
    bad_username: "Invalid peer name.",
    empty_text: "Write a message first.",
    not_logged_in: "Session ended. Log in again.",
    bad_request: "Invalid message."
  };

  var root = null;
  var onError = null;
  var onSelect = null;
  var username = "";
  var peers = {};
  var dms = {};
  var selected = null;
  var els = {};
  var unread = {};

  function init(rootEl, options) {
    root = rootEl;
    onError = (options && options.onError) || function () {};
    onSelect = (options && options.onSelect) || function () {};

    // Static template only; message text is always inserted with textContent.
    root.innerHTML =
      '<div class="dm-head">' +
        '<h2>Direct messages</h2>' +
      '</div>' +
      '<div class="dm-empty" id="dm-empty">' +
        'Pick an online peer from the list to start a private conversation.' +
      '</div>' +
      '<div class="dm-thread" id="dm-thread" hidden>' +
        '<div class="dm-thread-head">' +
          '<strong id="dm-title"></strong>' +
          '<span class="muted" id="dm-peer-status"></span>' +
        '</div>' +
        '<div class="dm-messages" id="dm-messages"></div>' +
        '<form class="dm-form" id="dm-form">' +
          // Placeholder for file transfer: disabled until app/files.py,
          // ClientDaemon.send_file and POST /api/file exist. Enable it then.
          '<button type="button" id="dm-attach" class="dm-attach" ' +
                  'aria-label="Attach a file" title="File transfer coming soon" disabled>' +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" ' +
                 'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
                 'stroke-linejoin="round" aria-hidden="true">' +
              '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 1 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>' +
            '</svg>' +
          '</button>' +
          '<input id="dm-input" type="text" maxlength="4096" autocomplete="off" ' +
                 'placeholder="Write a message..." aria-label="Message">' +
          '<button type="submit" id="dm-send">Send</button>' +
        '</form>' +
        '<p class="error" id="dm-error" role="alert"></p>' +
      '</div>';

    els = {
      empty: root.querySelector("#dm-empty"),
      thread: root.querySelector("#dm-thread"),
      title: root.querySelector("#dm-title"),
      peerStatus: root.querySelector("#dm-peer-status"),
      messages: root.querySelector("#dm-messages"),
      form: root.querySelector("#dm-form"),
      attach: root.querySelector("#dm-attach"),
      input: root.querySelector("#dm-input"),
      send: root.querySelector("#dm-send"),
      error: root.querySelector("#dm-error")
    };

    els.form.addEventListener("submit", send);
  }

  function setPeers(nextPeers) {
    peers = nextPeers || {};
    renderThread();
  }

  function setState(state) {
    if (!state) return;
    username = state.username || username;
    dms = state.dms || {};
    if (state.peers) peers = state.peers;
    renderThread();
  }

  function open(name) {
    if (!root || typeof name !== "string" || !name) return;
    selected = name;
    delete unread[name];
    markUnread(name, false);
    onSelect(name);
    renderThread();
    els.input.focus();
  }

  function onEvent(event) {
    if (!root || !event || event.kind !== "dm" || !event.message) return;
    var message = event.message;
    var peer = event.direction === "incoming" ? message.from : message.to;
    if (!peer) return;

    var bucket = dms[peer] || (dms[peer] = []);
    if (bucket.some(function (item) { return item.id === message.id; })) return;
    bucket.push(message);
    while (bucket.length > MAX_RENDERED_PER_PEER) bucket.shift();

    if (selected !== peer) {
      unread[peer] = true;
      markUnread(peer, true);
    }
    if (selected === peer) renderThread();
  }

  async function send(event) {
    event.preventDefault();
    if (!selected) return;

    var text = (els.input.value || "").trim();
    if (!text) {
      showError("Write a message first.");
      els.input.focus();
      return;
    }
    if (!peers[selected]) {
      showError(FRIENDLY.peer_offline);
      return;
    }

    showError("");
    els.send.disabled = true;
    els.input.disabled = true;
    try {
      var response = await fetch("/api/dm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: selected, text: text })
      });
      var data = await response.json().catch(function () { return {}; });
      if (!response.ok || !data.ok) {
        showError(FRIENDLY[data.reason] || ("Send failed: " + (data.reason || response.status)));
        return;
      }
      // Do not append here: the outgoing `dm` event will render it.
      els.input.value = "";
      els.input.focus();
    } catch (err) {
      showError("Cannot reach the local gateway.");
      if (onError) onError("Cannot reach the local gateway.");
    } finally {
      els.send.disabled = false;
      els.input.disabled = false;
    }
  }

  function renderThread() {
    if (!els.thread) return;

    if (!selected) {
      els.empty.hidden = false;
      els.thread.hidden = true;
      return;
    }

    els.empty.hidden = true;
    els.thread.hidden = false;
    els.title.textContent = selected;
    var online = Boolean(peers[selected]);
    els.peerStatus.textContent = online ? "online" : "offline";
    els.peerStatus.className = "muted " + (online ? "is-online" : "is-offline");
    els.input.disabled = false;

    els.messages.textContent = "";
    var bucket = dms[selected] || [];
    if (!bucket.length) {
      var hint = document.createElement("p");
      hint.className = "muted dm-empty-thread";
      hint.textContent = "No messages yet.";
      els.messages.appendChild(hint);
    } else {
      bucket.forEach(function (message) {
        els.messages.appendChild(renderMessage(message));
      });
    }
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function renderMessage(message) {
    var outgoing = message.direction
      ? message.direction === "outgoing"
      : message.from === username;

    var wrap = document.createElement("div");
    wrap.className = "dm-message " + (outgoing ? "outgoing" : "incoming");

    var meta = document.createElement("div");
    meta.className = "dm-meta";
    meta.textContent = (outgoing ? "you" : (message.from || "peer")) +
      (message.ts ? " · " + formatTime(message.ts) : "");

    var text = document.createElement("div");
    text.className = "dm-text";
    text.textContent = message.text || "";

    wrap.appendChild(meta);
    wrap.appendChild(text);
    return wrap;
  }

  function formatTime(ts) {
    var date = new Date(ts);
    if (isNaN(date.getTime())) return "";
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function markUnread(name, state) {
    var button = document.querySelector('.peer-list button[data-peer="' + cssEscape(name) + '"]');
    if (button) button.classList.toggle("unread", Boolean(state));
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function showError(text) {
    if (els.error) els.error.textContent = text || "";
  }

  window.DM = {
    init: init,
    setPeers: setPeers,
    setState: setState,
    onEvent: onEvent,
    open: open
  };
})();
