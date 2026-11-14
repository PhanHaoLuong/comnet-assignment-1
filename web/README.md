# Web shell — structure and extension points

The browser side is plain HTML/CSS/JS served by `client/gateway.py`. There is
no build step, no framework and no external CDN (the demo may run offline).

The shell is split into a small core, a composition root, and one file per
feature view:

| File | Role | Owner |
|---|---|---|
| `index.html` | static chrome: topbar, login gate, peers, workspace | Auth (UI lead) |
| `style.css` | all styling (tokens at the top) | Auth (UI lead) |
| `shell-core.js` | `P2P.store`, `P2P.views`, `P2P.createEventClient` — no app logic | Auth |
| `app.js` | composition root: auth routing, SSE wiring, bundled view adapters | Auth |
| `login.js` | login gate: form, validation, retry, error text | Auth |
| `dm.js` | Direct messages view (selected from the peer list) | DM |
| `chat.js` | Global chat view (selected from the sidebar) | Chat |
| `files.js` | future workspace view | Files |

## Boot flow

```
shell-core.js        creates window.P2P (store, views, SSE factory)
view modules         call P2P.registerView({...}) at load time
app.js (DOM ready)   attaches the registry, binds chrome, fetch("/api/state")
                     store.replace(state) -> render chrome -> route
route                authenticated -> workspace + SSE
                     disconnected with a username -> workspace + banner (DM survives)
                     otherwise -> login gate
```

SSE events update the store and are forwarded to every view. The next
`state` event is always the source of truth after a reconnect.

## State shape

`P2P.store.get()` returns the daemon snapshot, unchanged from
`GET /api/state`:

```js
{
  status: "idle" | "connecting" | "authenticated" | "disconnected" | "error",
  username: "alice" | null,
  tracker: ["127.0.0.1", 6767],
  peers: { bob: { username: "bob", addr: ["192.168.1.20", 51000] } },
  history: [{ gseq, from, text, ts }],
  dms: { bob: [{ id, t, from, to, text, ts, direction }] }
}
```

The shell patches `status`, `peers` and `history` from SSE events. Views must
not write to the store.

## Adding a view

Create one file and load it before `app.js` (`index.html`):

```html
<script src="chat.js"></script>
```

The module registers itself. The shell mounts it, optionally adds a tab, and
forwards state/events. Views selected from the sidebar are switched with
`P2P.views.show("chat")`; the sidebar row itself lives in `index.html`.

```js
P2P.registerView({
  id: "files",                      // unique, required
  label: "Files",                   // tab label (when tabbed)
  order: 30,                        // tab order (Chat 5, DM 20)
  tab: true,                        // false = selected from the sidebar, no tab
  mount: function (host) {
    // host is an empty <section class="view"> owned by the shell.
    // Build your DOM here, once.
    host.innerHTML = "<div class=\"files-root\"></div>";
  },
  setState: function (state) {
    // full snapshot: boot, login, SSE "state" (reconnect)
  },
  setPeers: function (peers) {
    // roster-only change; do not destroy message history
  },
  onEvent: function (event) {
    // every SSE event: peers, history, chat, dm, file_progress, status, error
  },
  open: function (payload) {
    // shell asks this view to open something (e.g. P2P.openPeer(name))
  }
});
```

The tab strip only appears when at least two views have `tab !== false`. The
bundled Chat and DM views set `tab: false` because the sidebar selects them:
`#global-chat` calls `P2P.views.show("chat")`, and a peer row calls
`P2P.openPeer(name)`.

All handlers are optional. Errors thrown by one view are logged and isolated;
they never stop the shell or the other views.

A module that follows the older `init/setState/setPeers/onEvent/open` shape
(`window.Chat`, `window.Files`, `window.DM`) is adapted automatically by
`app.js`, so a simple module does not have to touch the registry directly.

A module loaded after `app.js` still registers and mounts, but it has missed
the boot snapshot; seed it with `P2P.views.setState(P2P.store.get())` after
registering.

## Rules

- **Views own their subtree.** Build and update only inside `host`; never query
  the topbar, peers, login gate or another view.
- **No local echo.** The sender renders the `chat` / `dm` event; the POST
  response means "accepted", not "delivered".
- **`setState` vs `setPeers`.** `setState` receives a full server snapshot and
  may reset history; `setPeers` must only update presence. Live messages arrive
  through `onEvent`.
- **One file, one owner.** Feature modules are not edited by the shell owner;
  the shell is not edited by feature owners.
- **No new routes from view code.** If a feature needs an endpoint, it goes
  through the gateway owner (`client/gateway.py`).
- The login gate is not a view: `login.js` exposes `init/show/hide` and the
  shell decides when it is visible.

## Where the UI/UX pass plugs in

- `index.html` markup and `style.css` (tokens in `:root`) are the presentation
  layer; restyle freely without touching `app.js` or the view modules.
- The tab strip (`#view-tabs`) stays hidden because Global chat and DM are
  sidebar-selected (`tab: false`); the sidebar is `#global-chat` plus the peer
  rows.
- `login.js` only sets `textContent` and `aria-invalid`; add reveal buttons,
  remember-me, layout changes in the markup/UI layer.
