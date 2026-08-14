# Desktop Startup Role UX — Design

> Status: APPROVED by owner 2026-08-03 (「按稿全量实施 S1-S4」); S1-S4 all
> landed same day — see the epic pt_6956ccfb605e497b journal entry for the
> per-slice test/NEUTER ledger. One simplification vs the draft: the
> "first launch always shows" rule is subsumed by the config gate (absent
> `show_role_window` key — which is exactly what both a fresh install AND
> an upgrade from a pre-window build look like — defaults to SHOW).
> Related: docs/DESKTOP_AGENT_DIST_DESIGN.md (component separation, shipped),
> epic pt_59b62951aad2463e (agent dist — owns the WEB panel surface; this doc
> owns the NATIVE surface of both packaged apps, disjoint write set).
>
> **Amendment 2026-08-04 (owner report: ugly UI / no i18n / minimize vanished
> the window).** §3.1's「minimizing sends it to the tray」was structurally
> impossible as first implemented — the tray only started AFTER the first
> window closed. Now TRAY-FIRST on Windows: pystray owns the main thread from
> second zero; a dedicated tk host thread (`desktop/_tk_host.py`) owns every
> window/dialog; tray callbacks marshal window work to it; BOTH the title-bar
> minimize and the in-window button hide to the already-running tray. Also
> root-fixed in the same pass: `detect_lang()` learnt Windows display-name
> locales (`Chinese (Simplified)_China` → zh — the whole native surface had
> been English-only on Chinese Windows), and the font family is resolved from
> an explicit per-platform stack (the tk default was SimSun serif there).
>
> **Amendment ② 2026-08-04 (owner review: i18n was NOT actually full).**
> Systematic sweep: lib modules (`config.parse_connect_line`, `_probe`,
> `_pair`, `post_install` workers) now return MACHINE TOKENS only — every
> native-surface string is either a `_tk_theme.STRINGS` key or a token mapped
> at the UI boundary via the three theme helpers (`reason_text` /
> `connect_error_text` / `component_msg`); dynamic details (stderr) keep the
> raw tail behind a localized prefix. `ConnectLineError(code, detail)`
> replaces prose ValueErrors. A census ratchet
> (`tests/test_desktop_native_i18n.py`) fails the build on any lib token
> without a translation.

## 1. The gap

Owner directive (2026-08-03): *the desktop app must clearly indicate whether
it is the client or the server side at startup, instead of dumping all the
client functionality into the system tray. Users have no idea they're
supposed to look there, and the tray doesn't even have i18n support.*

Three measured problems:

1. **Role ambiguity at startup.** Neither packaged app ever says what it IS.
   The full app (`desktop/launcher.py`) silently spawns a server, opens a
   browser tab, and retreats to the tray. The agent app
   (`desktop/agent_launcher.py`) shows a connect dialog once and then is
   tray-only forever. A user who installed both (the exact confusion behind
   the 2026-08-02 tunnel incident — a full desktop acting as a second,
   unintended server on the office machine) has no moment where the product
   tells them "this machine is the SERVER" vs "this machine is CONTROLLED".
2. **The client surface is tray-only.** Everything that configures THIS
   machine as a controlled endpoint — enable computer control, the four
   permission tiers, connect-to-remote, autostart — exists ONLY in the
   pystray menu (`launcher.py::_run_tray`, `agent_launcher.py::_run_tray`).
   Tray menus are the least discoverable UI surface on every OS; the
   2026-08-02/03 support history is a litany of users never finding them.
3. **The tray speaks English only.** Every pystray literal is hardcoded
   (`'Open Tofu'`, `'Enable Computer Control'`, `'Permissions'`, …) while
   the tk dialogs already went bilingual through
   `desktop/_tk_theme.py::STRINGS` — the tray is now the last English-only
   surface of the product.

## 2. Current-state inventory (verified in tree, 2026-08-03)

| Surface | File | Today |
|---|---|---|
| Full-app startup | `desktop/launcher.py::main` | spawn server → open browser → tray; zero role indication |
| Full-app tray | `desktop/launcher.py::_run_tray` | 10 hardcoded-English MenuItems; CC toggle + tiers + connect only here |
| Agent startup | `desktop/agent_launcher.py::main` | imports the installer attachment, resumes/discovers routes, then starts the agent; no auth prompt |
| Agent tray | `desktop/agent_launcher.py::_run_tray` | 8 hardcoded-English MenuItems; the whole configuration surface |
| i18n infra | `desktop/_tk_theme.py` | `STRINGS` zh/en table + `t(key, lang)` + `detect_lang()` — tk dialogs only; pystray never consumes it |
| Themed window infra | `desktop/_tk_theme.py::apply_theme` + `card_frame` | already powers post_install + connect_ui; the role window reuses it unchanged |
| Config persistence | `lib/desktop_agent/config.py` | `load_config` / `save_config` dict — a `show_role_window` key fits the existing blob |
| Shared-dialog precedent | `desktop/connect_ui.py` | ONE authoring consumed by both launchers — the pattern the role window copies |

## 3. Decisions

### 3.1 A role window at startup (both apps)

On launch, each app shows ONE small branded native window (tk, themed by
`_tk_theme.apply_theme`, bilingual by `t()`, dark-mode aware) whose first
line is the ROLE:

* **Full app:** 「这台电脑是 Tofu 服务器」/ "This computer runs your Tofu
  server" — with the loopback URL, an **Open Tofu** button, and the
  computer-control section (§3.2). If a remote attachment ALSO exists, a
  second line says so: this machine is then both server and a controlled
  endpoint of `<url>` — the exact state that was invisible during the
  tunnel incident.
* **Agent app:** 「这台电脑是 Tofu 受控端」/ "This computer is controlled
  by a Tofu server" — with the installer-supplied server URL, live link
  status, permission tiers, autostart, and browser-assisted reconnect when
  the environment requires it. No credential is shown or entered.

The window has a **「启动时显示此窗口」/ "Show this window at startup"**
checkbox (default ON, persisted as `show_role_window` in the agent config
blob). Unchecked → straight to tray as today. First launch always shows it
regardless of the flag — the role declaration is the onboarding.

Minimizing (not closing-to-quit) sends it to the tray; the tray gains a
「控制面板…」/ "Control panel…" item that re-opens it. The window is
re-entrant: opening it twice focuses the existing one.

### 3.2 The client surface moves INTO the window

The role window IS the computer-control panel — the tray's current CC
cluster is rendered there as real checkbuttons with room for one-line
explanations:

* Full app: enable/disable computer control, the three write/exec/gui tiers,
  and current attachment.
* Agent app: the four tiers (incl. egress), autostart (Windows), and live
  connection diagnostics. Changing servers means downloading that server's
  personalized installer, not handling a token.

Mutations go through the SAME seams the tray already calls
(`_start_computer_control` / `_stop_computer_control` / `_persist_cc_state`
in the full app; `save_computer_control` / `_autostart_apply` in the agent)
— the panel is a second VIEW over the existing state dict, not a new state
path. The tray menu keeps its items as a compact mirror (a user who learned
it there loses nothing), but nothing is tray-ONLY anymore.

### 3.3 Tray i18n

Every pystray literal in both launchers moves into `_tk_theme.STRINGS`
under `desktop.tray.*` keys with zh + en. pystray already accepts a
callable as item text (used today for the dynamic Server/update items), so
EVERY item's text becomes `lambda item: t('desktop.tray.open', lang)` —
language resolves at menu-open time via `detect_lang()`, zero pystray
limitations touched. A parity test fails the build on any re-introduced
hardcoded MenuItem literal.

## 4. Architecture

### 4.1 `desktop/role_window.py` (NEW, shared — the connect_ui pattern)

One authoring consumed by both launchers. Deliberately split into:

* **Pure builders** (`role_state_full(port, cc_state, attached_url)` /
  `role_state_agent(url, perms, autostart)`) returning a plain dict of
  already-localized strings + flags. Headless-testable; this is where the
  role sentence lives, so the tests assert the SENTENCE, not pixels.
* **tk renderer** (`show_role_window(kind, state, callbacks, …)`) — lazy
  tkinter import (headless rule), themed by `apply_theme`, logo via
  `load_logo_photo`, one re-entrant instance per process.

### 4.2 `desktop/launcher.py` wiring

`main()` calls `show_role_window('full', …)` after `_spawn_server` (in a
thread — the tray must stay on the main thread on macOS/Windows), gated by
first-launch OR the persisted flag. CC callbacks reuse the existing
`_cc_state` dict and its four functions verbatim. Tray literals → `t()`
keys; new 「控制面板…」 item re-opens the window.

### 4.3 `desktop/agent_launcher.py` wiring

Act 2 unchanged (unattached first run still gets the connect dialog
first — you cannot declare a client role with no server). Act 4.5 (new):
show the role window with the agent state. Tier checkbuttons mutate the
SAME `perms` dict the agent loop reads each poll; autostart reuses
`_autostart_apply` + `_persist_autostart`. Tray literals → `t()` keys.

### 4.4 String keys + parity gate

~30 new `desktop.tray.*` / `desktop.role.*` keys × 2 languages in
`_tk_theme.STRINGS`. `tests/test_desktop_tk_theme.py` already asserts
key-set parity (every key carries both languages); a new
`tests/test_desktop_tray_i18n.py` AST-scans both launchers and fails on a
string-literal first argument to `MenuItem(` — the same guard-rail style
as the bundle drift ratchets.

### 4.5 What this deliberately does NOT do

* **No web-page panel.** The agent build has no server and no UI by design
  (its 53 MB size claim is an import-graph fact, smoke-gated). A native
  window is the only surface BOTH apps can share — and the post_install /
  connect_ui precedent means the toolkit is already proven on all three
  OSes.
* **No tray removal.** The tray stays as the compact mirror and the
  re-entry point; removing it would strand headless-ish workflows and the
  update-available item.
* **No custom URL scheme** (`tofu://` deep link from the web Local Control
  page into the native panel). Attractive follow-up, independent slice,
  needs OS-registry work on three platforms — out of scope here.

## 5. Alternatives considered

| Option | Verdict | Why |
|---|---|---|
| Native role window (chosen) | ✅ | Both apps can share it; infra (theme/i18n/DPI) already shipped and tested |
| Web UI panel served by the app | ❌ | Agent has no server; OS-level toggles (HKCU autostart, tray) not web-reachable |
| Toast/notification at startup | ❌ | Ephemeral; cannot host the CC controls; says the role once and never again |
| Tray-only with i18n | ❌ | Fixes problem 3, leaves 1 and 2 — discoverability was the core complaint |

## 6. Slices

1. **S1 — Tray i18n (both launchers).** STRINGS keys + `t()` wiring +
   parity gate. Zero behavior change in English; the bilingual win lands
   immediately and independently.
2. **S2 — `role_window.py` + agent app.** Pure builders + renderer + agent
   wiring + tests. The agent is the simpler consumer (pure client role).
3. **S3 — Full app.** Role window + in-window CC panel + tray 「控制面板…」
   re-entry + tests.
4. **S4 — Docs.** `desktop/README.md` startup-experience section + the two
   READMEs' component table row about first-launch behavior.

## 7. Acceptance

* Launching the full app shows a themed bilingual window whose first line
  declares the server role (zh under `TOFU_LANG=zh`, en otherwise) —
  before or alongside the browser tab, never silently to tray only.
* Launching the agent (attached) declares the controlled role + server URL.
* Every computer-control mutation possible in the tray is possible in the
  window, through the same seams, and persists identically.
* `TOFU_LANG=zh` renders every tray item in Chinese in BOTH apps; the
  parity gate reds on any new hardcoded literal.
* Failing-first + NEUTER discipline per slice; desktop suite ring green
  (test_desktop_tk_theme / cc_persistence / dist / smoke_gate + the new
  role/i18n suites).
