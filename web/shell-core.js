/* shell-core.js — small, DOM-light building blocks for the web shell.
 *
 * Loaded as a plain <script> before the view modules and app.js, so add-ons
 * can be dropped in without a build step. It exposes one global, `window.P2P`:
 *
 *   P2P.store               observable app state ({status, username, peers, ...})
 *   P2P.views               view registry (register / attach / show / open)
 *   P2P.createEventClient() SSE client with EventSource auto-reconnect
 *
 * Nothing in this file knows about login, peers, chat, DM or files. Those live
 * in app.js and the view modules. See web/README.md for the contracts.
 */
(function (global) {
  "use strict";

  // ------------------------------------------------------------------ store
  // A tiny observable object. Views never mutate it directly; app.js replaces
  // it from a full server snapshot or patches one slice (peers, status, ...).
  function createStore(initial) {
    var state = initial || {};
    var listeners = [];

    function notify() {
      listeners.slice().forEach(function (listener) {
        try {
          listener(state);
        } catch (error) {
          console.error("[P2P] store listener failed", error);
        }
      });
    }

    return {
      get: function () {
        return state;
      },
      set: function (patch) {
        state = Object.assign({}, state, patch);
        notify();
        return state;
      },
      replace: function (next) {
        state = next || {};
        notify();
        return state;
      },
      subscribe: function (listener) {
        listeners.push(listener);
        return function unsubscribe() {
          var index = listeners.indexOf(listener);
          if (index >= 0) listeners.splice(index, 1);
        };
      }
    };
  }

  // ---------------------------------------------------------- view registry
  // A "view" is one panel in the workspace. The registry owns the DOM host for
  // each view and the optional tab strip. Descriptor:
  //
  //   {
  //     id:      "chat",                 // required, unique, stable
  //     label:   "Chat",                 // tab title
  //     order:   10,                     // optional tab order (default 100)
  //     tab:     true,                   // false = sidebar-selected, no tab
  //     mount:   function (host) {},     // build DOM once; host is empty
  //     setState:function (state) {},    // full snapshot (login, SSE "state")
  //     setPeers:function (peers) {},    // roster-only change (SSE "peers")
  //     onEvent: function (event) {},    // every SSE event (chat, dm, ...)
  //     open:    function (payload) {}   // shell asks this view to open something
  //   }
  //
  // Views live in the workspace. Views with `tab: false` are selected from the
  // sidebar instead (global chat, the DM thread) and never add a tab.
  //
  // All handlers are optional. Errors in one view never break the shell.
  function createViewRegistry() {
    var views = [];
    var byId = {};
    var activeId = null;
    var stackEl = null;
    var navEl = null;
    var activeListener = null;

    function orderOf(view) {
      return typeof view.order === "number" ? view.order : 100;
    }

    function sortViews() {
      views.sort(function (a, b) {
        var diff = orderOf(a) - orderOf(b);
        if (diff !== 0) return diff;
        return String(a.label || a.id).localeCompare(String(b.label || b.id));
      });
    }

    function mount(view) {
      if (!stackEl || view._host) return;

      var host = document.createElement("section");
      host.className = "view";
      host.dataset.view = view.id;
      host.hidden = true;
      stackEl.appendChild(host);
      view._host = host;

      try {
        if (typeof view.mount === "function") view.mount(host);
      } catch (error) {
        console.error("[P2P] view '" + view.id + "' failed to mount", error);
        host.textContent = "This view failed to load.";
        return;
      }

      if (!activeId) show(view.id);
    }

    function navViews() {
      return views.filter(function (view) {
        return view.tab !== false;
      });
    }

    function renderNav() {
      if (!navEl) return;

      navEl.textContent = "";

      var tabs = navViews();

      // With a single tabbed view there is nothing to switch between, so the
      // strip stays out of the layout.
      if (tabs.length < 2) {
        navEl.hidden = true;
        return;
      }

      tabs.forEach(function (view) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "view-tab";
        button.dataset.view = view.id;
        button.textContent = view.label || view.id;
        if (view.id === activeId) button.setAttribute("aria-current", "page");
        button.addEventListener("click", function () {
          show(view.id);
        });
        navEl.appendChild(button);
      });
      navEl.hidden = false;
    }

    function register(view) {
      if (!view || !view.id) {
        throw new Error("P2P.registerView: a view id is required");
      }
      if (byId[view.id]) {
        console.warn("[P2P] view '" + view.id + "' is already registered");
        return false;
      }

      view.id = String(view.id);
      view.label = view.label || view.id;
      byId[view.id] = view;
      views.push(view);
      sortViews();
      mount(view);
      renderNav();
      return true;
    }

    function attach(stack, nav) {
      stackEl = stack || null;
      navEl = nav || null;
      views.forEach(mount);
      renderNav();
    }

    function show(id) {
      var view = byId[id];
      if (!view || !view._host) return false;

      activeId = id;
      views.forEach(function (item) {
        if (item._host) item._host.hidden = item.id !== id;
      });
      renderNav();

      try {
        if (typeof view.onShow === "function") view.onShow();
      } catch (error) {
        console.error("[P2P] view '" + view.id + "' onShow failed", error);
      }
      if (typeof activeListener === "function") activeListener(id);
      return true;
    }

    function open(id, payload) {
      if (!show(id)) return false;
      var view = byId[id];
      try {
        if (typeof view.open === "function") view.open(payload);
      } catch (error) {
        console.error("[P2P] view '" + view.id + "' open failed", error);
      }
      return true;
    }

    function call(method, argument) {
      views.forEach(function (view) {
        if (typeof view[method] !== "function") return;
        try {
          view[method](argument);
        } catch (error) {
          console.error("[P2P] view '" + view.id + "' " + method + " failed", error);
        }
      });
    }

    return {
      register: register,
      attach: attach,
      show: show,
      open: open,
      get: function (id) {
        return byId[id] || null;
      },
      list: function () {
        return views.slice();
      },
      active: function () {
        return activeId;
      },
      onActiveChange: function (listener) {
        activeListener = listener;
      },
      setState: function (state) {
        call("setState", state);
      },
      setPeers: function (peers) {
        call("setPeers", peers);
      },
      emit: function (event) {
        call("onEvent", event);
      }
    };
  }

  // ------------------------------------------------------------ SSE client
  // Wraps EventSource: parses JSON payloads, forwards them to one handler and
  // reports connection state changes. EventSource retries by itself; callers
  // should treat the next `state` event as the source of truth.
  function createEventClient(url) {
    var source = null;
    var messageHandler = null;
    var statusHandler = null;

    function report(status) {
      if (typeof statusHandler !== "function") return;
      try {
        statusHandler(status);
      } catch (error) {
        console.error("[P2P] SSE status handler failed", error);
      }
    }

    function connect() {
      if (source) return source;
      if (typeof global.EventSource !== "function") {
        console.warn("[P2P] EventSource is not supported by this browser");
        return null;
      }

      source = new global.EventSource(url);
      source.onopen = function () {
        report("open");
      };
      source.onerror = function () {
        report("error");
      };
      source.onmessage = function (message) {
        var event;
        try {
          event = JSON.parse(message.data);
        } catch (error) {
          console.warn("[P2P] ignoring malformed SSE payload", error);
          return;
        }
        if (typeof messageHandler !== "function") return;
        try {
          messageHandler(event);
        } catch (error) {
          console.error("[P2P] SSE handler failed", error);
        }
      };
      return source;
    }

    function close() {
      if (!source) return;
      source.close();
      source = null;
    }

    return {
      connect: connect,
      close: close,
      onMessage: function (handler) {
        messageHandler = handler;
      },
      onStatus: function (handler) {
        statusHandler = handler;
      }
    };
  }

  // -------------------------------------------------------------------- API
  var views = createViewRegistry();

  global.P2P = {
    store: createStore({
      status: "idle",
      username: null,
      tracker: null,
      peers: {},
      history: [],
      dms: {}
    }),
    views: views,
    registerView: views.register,
    createStore: createStore,
    createEventClient: createEventClient
  };
})(window);
