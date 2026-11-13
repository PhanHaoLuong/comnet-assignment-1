/* Minimal shell: login, presence, SSE wiring, DM module hooks.
 * Owner: Auth. The DM panel is delegated to web/dm.js.
 */
(function () {
  "use strict";

  var STATUS_TEXT = {
    idle: "idle",
    connecting: "connecting",
    authenticated: "connected",
    disconnected: "disconnected",
    error: "error"
  };

  var appStarted = false;
  var source = null;

  function byId(id) { return document.getElementById(id); }

  function setStatus(status, detail) {
    var el = byId("status");
    var text = byId("status-text");
    var key = STATUS_TEXT[status] || status || "idle";
    el.dataset.status = status || "idle";
    text.textContent = detail ? key + " · " + detail : key;
    if (status === "authenticated") showBanner("");
  }

  function showBanner(text) {
    var el = byId("app-error");
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function setActivePeer(name) {
    document.querySelectorAll("#peer-list button[data-peer]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.peer === name);
    });
  }

  function renderPeers(peers) {
    var list = byId("peer-list");
    var names = Object.keys(peers || {}).sort();
    list.textContent = "";
    byId("peer-count").textContent = String(names.length);
    byId("no-peers").hidden = names.length > 0;

    names.forEach(function (name) {
      var entry = peers[name] || {};
      var li = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      button.dataset.peer = name;

      var label = document.createElement("span");
      label.textContent = name;

      var addr = document.createElement("span");
      addr.className = "peer-addr";
      var value = entry.addr || [];
      addr.textContent = Array.isArray(value) && value.length === 2
        ? value[0] + ":" + value[1]
        : "";

      button.appendChild(label);
      button.appendChild(addr);
      button.addEventListener("click", function () {
        window.DM.open(name);
      });
      li.appendChild(button);
      list.appendChild(li);
    });
  }

  function renderState(state) {
    if (!state) return;

    byId("who").textContent = state.username ? "@" + state.username : "";
    setStatus(state.status, state.error);
    renderPeers(state.peers || {});
    window.DM.setState({
      username: state.username,
      peers: state.peers || {},
      dms: state.dms || {}
    });

    if (state.status === "authenticated") {
      window.LoginView.hide();
    } else {
      window.LoginView.show(
        state.status === "disconnected"
          ? "Disconnected from the tracker. Log in to reconnect."
          : ""
      );
    }
  }

  function connectEvents() {
    if (source) source.close();
    source = new EventSource("/events");

    source.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (err) { return; }

      if (data.kind === "state") {
        renderState(data.state);
      } else if (data.kind === "peers") {
        renderPeers(data.peers || {});
        window.DM.setPeers(data.peers || {});
      } else if (data.kind === "dm") {
        window.DM.onEvent(data);
      } else if (data.kind === "status") {
        setStatus(data.status, data.error);
        if (data.status === "disconnected") {
          window.LoginView.show("Tracker connection lost. Log in to reconnect.");
        }
      } else if (data.kind === "error") {
        showBanner(data.text || data.code || "Error");
      }
      // `chat` and `history` are intentionally left to the Chat module.
    };

    source.onerror = function () {
      // EventSource reconnects automatically; the next `state` event wins.
      setStatus("connecting");
    };
  }

  async function startApp(state) {
    if (!appStarted) {
      window.DM.init(byId("dm-root"), {
        onError: showBanner,
        onSelect: setActivePeer
      });
      appStarted = true;
    }
    renderState(state);
    connectEvents();
  }

  async function boot() {
    window.LoginView.init({ onLogin: startApp });
    try {
      var response = await fetch("/api/state");
      var data = await response.json();
      if (data.ok && data.state && data.state.status === "authenticated") {
        await startApp(data.state);
      } else {
        setStatus(data.state ? data.state.status : "idle");
        window.LoginView.show();
      }
    } catch (err) {
      setStatus("error");
      window.LoginView.show("Cannot reach the local gateway.");
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
