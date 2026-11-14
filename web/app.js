/* app.js — the shell composition root.
 *
 * Responsibilities:
 *   - own the chrome (topbar, presence list, banner)
 *   - route between the login gate and the workspace from P2P.store
 *   - wire the gateway's SSE stream into the store and the view registry
 *   - adapt the bundled view modules (dm.js today, chat/files later)
 *
 * It deliberately contains no feature UI. Anything that renders inside the
 * workspace belongs to a view registered through P2P.registerView(). See
 * web/README.md for the descriptor contract.
 *
 * Load order: shell-core.js -> view modules -> login.js -> app.js.
 */
(function (global) {
  "use strict";

  var STATUS_TEXT = {
    idle: "idle",
    connecting: "connecting",
    authenticated: "connected",
    disconnected: "disconnected",
    error: "error"
  };

  var store = global.P2P.store;
  var views = global.P2P.views;
  var login = global.LoginView;
  var events = global.P2P.createEventClient("/events");

  var els = {};
  var loginRequested = false;
  var activePeer = null;
  var renderedPeers = null;

  // --------------------------------------------------------------- helpers

  function byId(id) {
    return document.getElementById(id);
  }

  function cacheElements() {
    els.app = byId("app-view");
    els.reconnect = byId("reconnect");
    els.status = byId("status");
    els.statusText = byId("status-text");
    els.who = byId("who");
    els.banner = byId("app-error");
    els.peerList = byId("peer-list");
    els.peerCount = byId("peer-count");
    els.noPeers = byId("no-peers");
    els.stack = byId("view-stack");
    els.nav = byId("view-tabs");
  }

  function showBanner(text) {
    if (!text) {
      els.banner.hidden = true;
      els.banner.textContent = "";
      return;
    }
    els.banner.hidden = false;
    els.banner.textContent = text;
  }

  function setStatus(status, detail) {
    var key = STATUS_TEXT[status] || status || "idle";
    els.status.dataset.status = status || "idle";
    els.statusText.textContent = detail ? key + " · " + detail : key;
  }

  // --------------------------------------------------------------- presence

  function setActivePeer(name) {
    activePeer = name || null;
    Array.prototype.forEach.call(
      document.querySelectorAll("#peer-list button[data-peer]"),
      function (button) {
        button.classList.toggle("active", button.dataset.peer === activePeer);
      }
    );
    if (activePeer) setGlobalActive(false);
  }

  function setGlobalActive(active) {
    var button = byId("global-chat");
    if (button) button.classList.toggle("active", Boolean(active));
  }

  function openGlobalChat() {
    if (!views.get("chat")) return;
    setActivePeer(null);
    setGlobalActive(true);
    views.show("chat");
  }

  function openPeer(name) {
    if (!name) return;
    setActivePeer(name);
    // The DM view owns the thread; if it is not installed yet this is a no-op
    // and the peer simply stays highlighted.
    views.open("dm", name);
  }

  function renderPeers(peers) {
    var names = Object.keys(peers || {}).sort();

    els.peerList.textContent = "";
    els.peerCount.textContent = String(names.length);
    els.noPeers.hidden = names.length > 0;

    names.forEach(function (name) {
      var entry = peers[name] || {};
      var item = document.createElement("li");
      var button = document.createElement("button");
      var identity = document.createElement("span");
      var label = document.createElement("span");
      var addr = entry.addr || [];

      button.type = "button";
      button.dataset.peer = name;
      if (name === activePeer) button.classList.add("active");

      identity.className = "peer-id";
      label.className = "peer-name";
      label.textContent = name;
      identity.appendChild(label);

      if (Array.isArray(addr) && addr.length === 2) {
        var at = document.createElement("span");
        var address = document.createElement("span");

        at.className = "peer-at";
        at.textContent = "@";

        address.className = "peer-addr";
        address.textContent = addr[0] + ":" + addr[1];

        identity.appendChild(at);
        identity.appendChild(address);
      }

      button.appendChild(identity);
      button.addEventListener("click", function () {
        openPeer(name);
      });

      item.appendChild(button);
      els.peerList.appendChild(item);
    });
  }

  // -------------------------------------------------------------- chrome

  function renderChrome(state) {
    var status = state.status || "idle";
    setStatus(status, state.error);
    els.who.textContent = state.username ? state.username : "";

    // Rebuild the list only when the roster actually changed, so status
    // updates do not wipe the DM module's unread/active markers.
    var peers = state.peers || {};
    if (peers !== renderedPeers) {
      renderedPeers = peers;
      renderPeers(peers);
    }
  }

  // --------------------------------------------------------- auth routing

  function enterApp() {
    els.app.hidden = false;
    els.reconnect.hidden = true;
    showBanner("");
    if (login) login.hide();
    events.connect();
  }

  function showDisconnected() {
    els.app.hidden = false;
    els.reconnect.hidden = false;
    showBanner(
      "Tracker offline. Direct messages with existing peers still work; " +
        "chat and new peer discovery need a reconnect."
    );
    if (login) login.hide();
    events.connect();
  }

  function showLogin(state) {
    els.app.hidden = true;
    els.reconnect.hidden = true;

    var message;
    if (state.status === "disconnected") {
      message = "Disconnected from the tracker. Log in to reconnect.";
    }
    // Passing undefined keeps whatever the login module is already showing
    // (important: a status event must not wipe a 401 message).
    if (login) login.show(message);
  }

  function route(state) {
    if (state.status === "authenticated") {
      loginRequested = false;
      enterApp();
      return;
    }

    // After a tracker loss the daemon keeps direct messaging alive, so stay
    // in the workspace until the user explicitly asks to log in again.
    if (!loginRequested && state.status === "disconnected" && state.username) {
      showDisconnected();
      return;
    }

    showLogin(state);
  }

  // ----------------------------------------------------------- SSE events

  function handleEvent(event) {
    if (!event || !event.kind) return;

    switch (event.kind) {
      case "state":
        if (event.state) {
          store.replace(event.state);
          views.setState(event.state);
        }
        break;

      case "status":
        store.set({
          status: event.status || "idle",
          username:
            event.username !== undefined ? event.username : store.get().username,
          error: event.error || null
        });
        views.emit(event);
        break;

      case "peers":
        store.set({ peers: event.peers || {} });
        views.setPeers(event.peers || {});
        views.emit(event);
        break;

      case "history":
        store.set({ history: event.history || [] });
        views.emit(event);
        break;

      case "chat":
      case "dm":
      case "file_progress":
        views.emit(event);
        break;

      case "error":
        showBanner(event.text || event.code || "Error");
        views.emit(event);
        break;

      default:
        views.emit(event);
    }
  }

  // ------------------------------------------------------- bundled views

  function registerDmView() {
    if (!global.DM || views.get("dm")) return;

    views.register({
      id: "dm",
      label: "Direct messages",
      order: 20,
      tab: false,
      mount: function (host) {
        host.id = "dm-root";
        global.DM.init(host, {
          onError: showBanner,
          onSelect: setActivePeer
        });
      },
      setState: function (state) {
        global.DM.setState({
          username: state.username,
          peers: state.peers || {},
          dms: state.dms || {}
        });
      },
      setPeers: function (peers) {
        global.DM.setPeers(peers || {});
      },
      onEvent: function (event) {
        if (event.kind === "dm" && event.message) global.DM.onEvent(event);
      },
      open: function (peer) {
        global.DM.open(peer);
      }
    });
  }

  // Bridge for future features that follow the same init/setState/setPeers/
  // onEvent/open convention. A module that wants full control can instead call
  // P2P.registerView() itself, before or after this file runs.
  function registerLegacyView(globalName, options) {
    var module = global[globalName];
    if (!module || views.get(options.id)) return;
    if (typeof module.init !== "function") {
      console.warn("[P2P] window." + globalName + " has no init(); not mounting it");
      return;
    }

    views.register({
      id: options.id,
      label: module.label || options.label,
      order: options.order,
      tab: options.tab,
      mount: function (host) {
        module.init(host, { onError: showBanner, openPeer: openPeer });
      },
      setState: function (state) {
        if (typeof module.setState === "function") module.setState(state);
      },
      setPeers: function (peers) {
        if (typeof module.setPeers === "function") module.setPeers(peers);
      },
      onEvent: function (event) {
        if (typeof module.onEvent === "function") module.onEvent(event);
      },
      open: function (payload) {
        if (typeof module.open === "function") module.open(payload);
      }
    });
  }

  function registerBundledViews() {
    // Both the global chat and the DM thread are workspace views; the sidebar
    // picks which one is visible, so neither needs a tab.
    registerLegacyView("Chat", {
      id: "chat",
      label: "Global chat",
      order: 5,
      tab: false
    });
    registerDmView();
    registerLegacyView("Files", { id: "files", label: "Files", order: 30 });
  }

  // ------------------------------------------------------------- lifecycle

  function acceptLogin(state) {
    if (state) {
      store.replace(state);
      views.setState(state);
    }
    events.connect();
  }

  function boot() {
    cacheElements();

    if (login) login.init({ onLogin: acceptLogin });

    registerBundledViews();
    views.attach(els.stack, els.nav);
    setGlobalActive(views.active() === "chat");

    var globalButton = byId("global-chat");
    if (globalButton) globalButton.addEventListener("click", openGlobalChat);

    if (login) {
      // Switching users / recovering a lost tracker connection is an explicit
      // action, never an automatic one.
      els.reconnect.addEventListener("click", function () {
        loginRequested = true;
        login.show("Log in to reconnect to the tracker.");
      });
    }

    store.subscribe(function (state) {
      renderChrome(state);
      route(state);
    });

    events.onMessage(handleEvent);
    events.onStatus(function (status) {
      // EventSource retries on its own; the next "state" event is the truth.
      // Kept as a hook for the UI/UX pass (e.g. a "reconnecting" indicator).
      els.status.dataset.stream = status;
    });

    fetchInitialState();
  }

  async function fetchInitialState() {
    try {
      var response = await fetch("/api/state", { cache: "no-store" });
      var data = await response.json();
      if (!data || !data.ok || !data.state) throw new Error("bad state reply");

      store.replace(data.state);
      views.setState(data.state);
    } catch (error) {
      store.set({ status: "error", error: "gateway unreachable" });
      showBanner("Cannot reach the local gateway.");
      if (login) login.show("Cannot reach the local gateway.");
    }
  }

  // A view (or the console) may want to open a peer without knowing the shell.
  global.P2P.openPeer = openPeer;

  document.addEventListener("DOMContentLoaded", boot);
  global.addEventListener("beforeunload", function () {
    events.close();
  });
})(window);
