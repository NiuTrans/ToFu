# Server-Built AGENT-ONLY Installers — Design

> Status: DRAFT v6 (2026-08-06), epic `pt_59b62951aad2463e`.
> v2 folds in the three distribution-surface decisions (§5): the Local
> Control display matrix, the full-client direction policy, and the
> GitHub Releases contents. v3 folds in the owner's three review
> amendments: boot autostart for relay machines (§4.4, §6), agent
> version in the registration frame + drift surfacing (§5.2, §8), and
> the interactive-session boundary (§6). v4 supersedes the pairing UX
> (§11, owner directive "minimize shell — why doesn't Codex have this
> problem"): SSH auto-tunnel + one-time pairing code replace the
> address-carrying connect line as the primary flow. **v5 RETIRES the
> pairing code (§12, owner decree "no pairing codes, zero configuration
> burden"): the per-download attach bundle (ZIP = exe + baked
> {token, route candidates}) IS the pairing — install = auto-attach,
> zero input.**
> Splits the fused "one installer, two roles" distribution into TWO
> components: the controlled-machine **agent** (no frontend, no server
> stack) and the full desktop app (this machine = server + client).
> Sibling of `DESKTOP_CLIENT_BUILD_DESIGN.md` (same toolchain, same
> store, same NSIS authoring); depends on the egress epic
> `pt_4ea6bf05deaa46f0` only for the curl_cffi note in §4.4.

## 1. The agreement and the gap

Owner directive (2026-08-02, this epic's conversation):

> "There are two installable components: one is the medium for the server
> to control client computers (including bridges for OAuth subscription
> login); the other is a complete client for Windows users who use their
> local machine as both server and client. I believe they should be
> separated — the former hardly requires any frontend interface;
> configuration capability is sufficient."

The audit behind that directive (all verified in-tree 2026-08-02):

| Role | Code | Distribution |
|---|---|---|
| Controlled machine (agent) | `lib/desktop_agent/` — standalone-clean package, CLI entry `python -m lib.desktop_agent` (`_run.py::main`), zero frontend | **NONE.** Obtainable only by installing the FULL desktop app, or by copying the whole repo to the machine and running the module from source |
| Full desktop (server + client) | `desktop/launcher.py` + `tofu.spec` — PyInstaller-frozen Quart server + frontend + DB + tray, with an in-process agent thread | `Tofu-Setup-<ver>-win64.exe`, ~152.9 MB, server-built (`lib/desktop_dist/winbuilder.py`), served from Local Control |

The fusion is a **distribution** fact, not a code fact — and it has three
concrete costs today:

1. **A machine whose ONLY role is to be controlled must install a second
   entire Tofu.** `tofu.spec` bundles `collect_submodules('lib')` (the
   whole server + route tree), Hypercorn, psycopg2, playwright,
   trafilatura, pymupdf — 152.7 MB / 3316 files measured (S2,
   2026-08-01) — so that `launcher.py` can spawn a Quart child process
   and open a browser. A relay/egress-exit machine needs none of it.
2. **Local Control steers the controlled-machine case to the full
   installer.** `_lcRenderDesktop`'s remote branch (the ONLY branch that
   needs a token) offers `downloads[]` containing exactly one kind of
   artifact — the full app — and says "在你自己的电脑安装桌面版，再连过来".
3. **The egress design already ran up the debt.** `DESKTOP_EGRESS_DESIGN
   .md` §11 marks "拷贝 chatui 仓库到办公机，pip install requests" as the
   *temporary* path "桌面安装包就绪前" — and its §5 note plans an "agent
   打包流程" that was never built. The egress epic's deploy target is
   precisely the machine this epic's component serves.

## 2. Measured constraints (all verified on this host / in tree, 2026-08-02)

| Fact | Consequence |
|---|---|
| Module-level import sweep of `lib/desktop_agent/`: `requests`, `lib.log` (stdlib-only), `lib.json_store`; `pyautogui` / `pyperclip` / `psutil` / Pillow are **guarded optional** imports (`_gui.py:38-53`, `_exec.py:92-95`). `config.py` adds only `lib.json_store` | The frozen agent needs NONE of Quart/Flask/Hypercorn/psycopg2/playwright/trafilatura/pymupdf/lxml/bs4/mcp — i.e. none of the 152 MB bulk. The size claim is an import-graph fact, not a hope |
| Full payload measured 152.7 MB / 3316 files (S2 payload, 2026-08-01) | **Agent payload MEASURED (A1, 2026-08-02): 84.8 MB / 1904 files onedir, 53.0 MB tarball** — ~55% of full unpacked, ~35% of the full installer compressed. Banned sweep of the real artifact: zero quart/flask/hypercorn/psycopg2/playwright/trafilatura/fitz entries; tkinter present. Build time ~2.6 min vs the full build's half hour |
| Userspace Wine toolchain + native NSIS are provisioned and proven (`wintoolchain.py` S1–S3, epic `pt_ce4261579c1b4c64`); `installer.nsi.tmpl` + `test_installer_parity.py` exist | The agent build is a SECOND PyInstaller target on the SAME toolchain. Zero new infrastructure; the parity-contract pattern already absorbs "two authorings, one contract", so "two components, one contract" is the same move again |
| The preseed contract is launch-side: `preseed_server.json` next to the exe, one-shot, non-secret (URL only), never overrides an existing attachment (`launcher.py::_import_preseed`) | Reusing it for the agent costs moving one small function to a shared module |
| `launcher.py::_prompt_connect_line` + `desktop/_tk_theme.py` are a dependency-light config UI (tkinter — shipped by the python.org Windows Python the payload already embeds); `lib/desktop_agent/config.py::parse_connect_line` owns the connect-line wire format | The agent's entire "configuration capability" (owner's words) already exists as ~100 lines of tkinter. The dialog parses through the same owner of the format, so UI and wire can never drift |
| `desktop_agent/config.py` persists enabled-state + permission tiers via `save_computer_control` / `load_computer_control`, read identically by the full app's tray | One config floor for BOTH components: a machine that later installs the full app (or replaces it with the agent) inherits the owner's permission choices instead of re-asking |
| `curl_cffi` is NOT yet imported by `_egress.py` — the egress design lists it as a planned OPTIONAL dependency (TLS fingerprint, §5 note) | The agent spec declares it as an optional hidden import **now**, so the day the egress epic lands the dependency the packaging needs no change |
| `installer.nsi.tmpl` already runs per-user (`RequestExecutionLevel user`, `InstallDir $LOCALAPPDATA`) — an `HKCU\…\CurrentVersion\Run` value therefore needs NO UAC; Inno's `[Tasks]`+`[Registry]` pair is the CI-side equivalent and both keys uninstall cleanly | Boot autostart for the agent is a free addition in BOTH authorings — the privilege model already permits it (owner amendment ①) |
| `_build_agent_frame` (`_run.py`) carries `agent_id / name / platform / capabilities / share_roots` — **no `version`** | The server currently CANNOT see agent↔server drift; adding one field makes it observable (owner amendment ②) |

What is deliberately NOT reused from `launcher.py`: the server spawn, the
browser auto-open, the component manager, the GitHub update check, and the
"no remote attachment ⇒ poll my own local server" default. An agent-only
build HAS no local server — a missing attachment is a first-run dialog,
not a silent default.

## 3. What "separated" compiles in

Four axes, mirroring the client-build doc's discipline:

1. **Component kind** — manifest entries gain `kind: 'full' | 'agent'`
   (absent ⇒ `'full'`, back-compat). Selection is EXPLICIT (route
   parameter / UI branch), never inferred from the UA — a Windows visitor
   can legitimately want either.
2. **Version currency** — built from `git archive HEAD`, same snapshot
   discipline as the full build (a build never carries uncommitted
   working-tree state). The agent payload cache keys on the AGENT's own
   deps stamp (its much smaller requirement set), so agent payload reuse
   across full-app-only dependency bumps is even better than today's.
3. **Preseed** — identical mechanism; the server URL comes from the build
   request context, never guessed. The token still arrives via the minted
   connect line (Phase 2 per-user token-baked installers stay the
   separate, already-designed epic).
4. **Egress readiness** — `curl_cffi` hidden-imported when present (§2).

## 4. Architecture

### 4.1 `desktop/agent_launcher.py` — the agent's process (~150 lines)

One file, four acts, no server anywhere in it:

```
main():
  1. _import_preseed()            # shared seam, moved not copied (§4.2)
  2. attachment = remote_server() # lib.desktop_agent.config
     if none: _prompt_connect_line()   # shared seam; cancel ⇒ exit 0
  3. perms = load_computer_control() merged over safe_default()  # deny-all floor
  4. run_agent(url, perms, bridge_secret=secret, stop_event=tray_stop)
     # in the FOREGROUND — the agent loop IS this process
```

Plus a **minimal pystray tray** (this is the "configuration capability",
the whole of it): `[Server: <url>]` (disabled label — the silence gap the
full app's tray already fixed), `[Connect to a different Tofu…]`,
`[Permissions ▸ write / exec / gui / egress]` (live-mutating the shared
perms dict, persisted on click — the same toggle mechanics as
`launcher.py`), `[Start with Windows]` (owner amendment ① — toggles the
same HKCU Run value the installer writes, persisted to the agent config
and reconciled config→registry at every launch, so the choice survives
both reinstall and a registry edit; Windows-only in v1, hidden elsewhere),
`[Quit]`. `console=False`, all diagnostics through a
null-safe `_log` teeing to `<exe>/data/desktop-agent.log` — the
windowed-build logging discipline is copied verbatim from `launcher.py`
(its docstring item 1 exists because this exact trap bit before).

### 4.2 Shared seams — `desktop/connect_ui.py` (NEW, ~200 lines, a MOVE)

`_import_preseed` and `_prompt_connect_line` move out of `launcher.py`
into `desktop/connect_ui.py`; both launchers import from there. A move,
not a copy — two copies of the connect dialog or the preseed contract
would drift, and the parity-suite philosophy of this subsystem is that
the CONTRACT, not the duplication, is the single source of truth.
`launcher.py`'s call sites are otherwise untouched (the full app's
behaviour is byte-identical; its suite proves it).

### 4.3 `tofu-agent.spec` — the second PyInstaller target

- Analysis on `desktop/agent_launcher.py` only.
- hiddenimports: `collect_submodules('lib.desktop_agent')` +
  `lib.log`, `lib.json_store`, `lib.runtime_paths` + `requests` +
  `pystray`, `PIL.Image`, `psutil`, the pyautogui platform stack
  (`pygetwindow/pyscreeze/pytweening/mouseinfo/pyperclip`) + `tkinter` +
  `curl_cffi` when importable (§3.4).
- excludes: the full-app list PLUS the entire server stack — hypercorn,
  quart, flask, psycopg2, playwright, trafilatura, pymupdf, lxml, bs4,
  mcp. The exclude list is load-bearing: the closure must PROVE itself
  small. The smoke gate (§4.5) asserts it.
- No `static/`, no `index.html`, no `browser_extension` datas — the only
  data file is the icon set for the tray + wizard.

### 4.4 `winbuilder.py` — per-target Half A, parametrized Half B

- **Half A** gains a `target ∈ {'full', 'agent'}` dimension:
  `payload-<target>-<git_sha>-<deps_stamp>.tar.zst`. The agent target's
  pip recipe is the agent closure only (pyinstaller + requests + pystray
  + pillow + psutil + pyautogui + pyperclip + optional curl_cffi) — the
  build is minutes, not the full build's half hour, and its deps_stamp
  ignores server requirements entirely.
- **Half B**: `installer.nsi.tmpl` gains `@APP_NAME@`, `@APP_EXE@`,
  `@INSTALL_DIR_NAME@` placeholders. The full build renders the CURRENT
  values (`Tofu` / `Tofu.exe` / `$LOCALAPPDATA\Programs\Tofu`) — the
  parity suite is EXTENDED to pin both renderings, not rewritten. Agent
  artifact: `TofuAgent-Setup-<ver>-win64.exe`,
  `TofuAgent.exe`, `InstallDir $LOCALAPPDATA\Programs\TofuAgent`
  (side-by-side installs never collide), recorded with `kind: 'agent'`,
  `source: 'built'`, plus the preseed metadata — same `record_artifact`
  call shape as today.
- The preseed write (`_write_preseed`) is already target-agnostic — it
  drops the file next to whatever exe the payload carries.
- **The agent target DROPS a loopback/unspecified preseed** (loud log,
  `_agent_safe_preseed_url`; measured 2026-08-02: the first artifact
  baked `http://127.0.0.1:15000` from a server-local build request — a
  remote machine would attach to its OWN loopback, never reach the
  server, and never see the connect dialog because an attachment
  exists). Baking nothing makes first run ask for the connect line —
  one paste, always right. The full target keeps loopback preseeds
  byte-identically: its primary case is the server's own machine
  (local_source), where loopback is exactly correct.
- **Boot autostart (owner amendment ①).** The agent's primary scene is
  an UNATTENDED relay/egress machine — one Windows-Update reboot silently
  kills the bridge until someone notices failed traffic. The NSIS
  template gains a components page with a default-selected "Start with
  Windows" section writing `HKCU\Software\Microsoft\Windows\CurrentVersion
  \Run\TofuAgent = "$INSTDIR\TofuAgent.exe"`; the uninstaller deletes the
  value unconditionally (a removed app must not leave a dead autorun
  pointing at a missing exe). CI's Inno authoring gets the equivalent
  `[Tasks] autostart` + `[Registry] … Flags: uninsdeletevalue`, also
  default-on for the agent component. `test_installer_parity.py` pins
  the semantic contract for BOTH: agent installer ⇒ autostart offered,
  default ON, UAC-free (HKCU), removed at uninstall; full installer ⇒
  unchanged (no autostart — a user-present tray app does not need it).

### 4.5 Smoke gate — exit code as verdict, same discipline as TOFU_SMOKE

The full build's `TOFU_SMOKE=1` exists because "the process stayed alive"
is green by construction for a windowed binary. The agent target gets the
same gate with the agent's own assertions, run under `xvfb`/wine exactly
like S2:

```
TOFU_AGENT_SMOKE=1  ⇒  import lib.desktop_agent; assert COMMANDS non-empty;
                       assert 'quart' not in sys.modules and no quart/flask/
                       hypercorn module file exists in the bundle tree;
                       print TOFU_AGENT_SMOKE_OK version=<v> commands=<n>;
                       exit 0 — any failure: traceback + exit 1
```

The import-graph assertion is the frozen-build proof of §2's core claim:
if a future change drags the server stack into the agent closure, the
build goes red, not the user's machine.

### 4.6 Serving — the `kind` axis through the store

- `scripts/release_assets.py`: agent rows live in a NEW
  `AGENT_PLATFORM_ASSETS` table — SAME 5-tuple shape as
  `PLATFORM_ASSETS` (zero churn in tuple consumers), but
  `REQUIRED_PLATFORM_ASSETS` derives over BOTH tables: once CI ships
  agent legs (§5.3), a release missing them is INCOMPLETE, and the
  version gate's build-on-INCOMPLETE rule self-heals the current
  version into carrying them.
- `store.find_for_platform(os_key, arch, kind='full')`: candidate filter
  gains `entry.get('kind', 'full') == kind`. Default `'full'` ⇒ every
  current caller behaves byte-identically; absent-`kind` legacy entries
  read as `'full'` — zero manifest migration.
- `routes/api_v1/desktop.py`: `_request_platform_downloads` gains the
  kind parameter; the status payload's `downloads[]` entries carry
  `kind`. The remote-case branch requests `kind='agent'` first and
  includes the full installer as the secondary option. The download route
  itself is unchanged (manifest-key serving is already
  component-agnostic).
- Once CI carries agent assets (§5.3), `mirror.py` mirrors them like
  any other row: every server then serves the macOS agent DMGs (its
  structural impossibility) and a fallback Windows/Linux agent,
  same-origin. `remove_not_in` already never prunes `source='built'`.
- Autobuild: same `TOFU_DESKTOP_DIST_AUTOBUILD` gate, extended — a
  Windows visitor hitting the agent surface with no built agent artifact
  kicks the agent build (stale-while-build ⇒ they get the full installer
  with a note, never a dead end).

### 4.7 Local Control UI (`static/js/local-control.js`)

The display matrix per `setup_state` branch — preserving this file's
core rule (exactly ONE next action per detected state):

| Branch | Primary download | Secondary | Rationale |
|---|---|---|---|
| `remote` (controlled machine) | **受控端 · 轻量** (agent, `服务器直连 · built` chip + size) | One line: "这台电脑也要跑 Tofu 本体？下载完整桌面版"; mint connect line unchanged | This branch exists to let the server act on THIS machine — the agent is its exact component |
| `local_source` | **完整桌面版** (replaces the source run) + **collapsed "从另一台电脑访问本服务器？" escape hatch** (agent download + mint — the tunnel case below) | A source checkout already runs `python -m lib.desktop_agent`; the hatch covers what the backend cannot see | The machine already IS the server; it wants the packaged app |
| `tray` | none | none | Agent already runs in-process; instruction unchanged |
| `connected` | none | none | unchanged |

**The tunnel blind spot (measured live 2026-08-02, folded in via
`c4130943`).** An ssh -L forward makes a REMOTE browser present as
loopback → `_setup_state` returns `local_source` for a machine that is
NOT this one, and the branch's primary instruction ("install the full
app") installed a second Tofu whose bundled server took a fallback port
and whose agent polled IT — never this server. The server has NO signal
to detect a tunnel (documented in `_setup_state`: "there is nothing to
detect"), so the fix is surface-level: the collapsed hatch, with the
merge suite's one-action contract preserved (hint is `lc-substep`; the
suite's token test documents the exception: local_source may carry ONE
mint, only inside the collapsed hatch). Owner redesign of the remote
branch landed alongside (`1a2cca6b`): numbered ①②③ agent flow, full
app collapsed into `<details>`, a zero-touch 2-step variant when the
artifact carries a usable preseed AND the bridge is open, and —
catching a real defect in A2 — winbuilder now DROPS a loopback/
unspecified preseed for the agent target (the first wrap baked
`http://127.0.0.1:15000`, which on the office machine attaches to a
void AND suppresses the first-run dialog).

Each row carries a one-line role gloss — "受控端：只让这台电脑被服务器
操作（轻量，无界面，托盘配置）" vs "完整版：这台电脑自己跑 Tofu
（服务器+界面）" — so the choice needs no filename literacy. i18n keys
follow the existing `local.desktopDownload*` family.

**Rendering, final form (2026-08-02, owner: minimize cognitive load).**
The remote branch is a NUMBERED flow (like the browser row's ①②③), not
a layout the user must infer a sequence from: ① download the agent
(button-styled link + 服务器直连 chip + size) → ② mint the connect line
(auto-copied on success, toast) → ③ paste into the agent's first-run
connect box. The full-app offer collapses into a one-line `<details>`
secondary (zero JS). Two shapes, chosen by backend facts: the default
3-step, and a **zero-touch 2-step** when the artifact carries a usable
preseed AND the bridge needs no token — "install and it connects by
itself". The payload projects the two raw facts
(`bridge_token_required`, per-entry `preseed_url`); absent
`bridge_token_required` reads as REQUIRED (the 3-step flow also works
on an open bridge — the fail-safe direction).

## 5. Distribution surfaces — the three owner decisions (2026-08-02)

Owner questions: what does Local Control display, where is the full
client directed, and what goes to GitHub Releases.

### 5.1 Local Control — decided in §4.7 (the branch matrix)

The agent installer is the PRIMARY offer exactly where the branch's
purpose is "let the server act on this machine" (`remote`); everywhere
else the surface is unchanged. The full installer is never more than
one line away.

### 5.2 Where the full client is directed — server-first, unchanged principle

The two components share ONE supply policy, no new channel:

1. **PRIMARY: this server's store** (`/api/v1/desktop/download/<file>`,
   `服务器直连 · built`) — freshest (HEAD), preseeded, zero dependence
   on the client's route to GitHub. Windows: server-built full + agent.
   Linux: server-built native full (agent native build is a cheap
   follow-on). macOS: mirrored CI assets.
   For the AGENT kind this preference is stronger than "freshest": the
   command protocol (egress frames, stream_outbox shape, dispatch
   table) evolves WITH the server, so an agent built from the same HEAD
   as the server it polls is the only pairing guaranteed to speak the
   same protocol — a release-line agent from GitHub can silently
   mis-dispatch against a HEAD server. Server-built for the agent is a
   **protocol co-origin** guarantee, not a convenience (owner amendment
   ②; detection side in §8/A3).
2. **FALLBACK: the GitHub releases page** (`查看全部下载 ↗`) — for
   unrecognised platforms, missing assets, an empty store, and visitors
   arriving from the repo README.
3. **The mirror bridges what the server cannot build** — macOS of both
   kinds (structural, §7 of the client-build doc) and an agent fallback
   when a server's toolchain is unavailable.

### 5.3 GitHub Releases — additive agent legs, full line untouched

CI builds on REAL runners (windows-latest, macos matrix, ubuntu-latest)
— no wine, no seccomp traps; the agent closure is a strict subset, so
each agent leg is the corresponding full leg with a smaller spec.

| New asset | Runner | Note |
|---|---|---|
| `TofuAgent-Setup-<ver>-win64.exe` | windows-latest | CI uses INNO (native iscc — the wine 32-bit trap does not exist on a real runner); the server uses NSIS. The parity contract binds 2 components × 2 authorings |
| `TofuAgent-<ver>-macos-arm64.dmg` / `-x86_64.dmg` | macos matrix | **The ONLY macOS agent supply that can ever exist** (server-side macOS builds are structurally impossible) |
| `TofuAgent-<ver>-linux-x86_64.tar.gz` | ubuntu-latest | tar.gz only — `.deb` stays full-only, keeping the releases page from doubling |

`SHA256SUMS` covers the new assets; `release_assets.py`'s gates require
them (§4.6), in the SAME change as the CI legs — a release missing an
agent leg is INCOMPLETE, and the version gate then rebuilds the current
version into carrying them (build-on-INCOMPLETE is the designed
self-heal, not an accident).

**Why upload at all (the trade-off, decided):** an agent without a
server is useless, so a GitHub visitor could pick the wrong asset —
mitigated by naming (`TofuAgent-` vs `Tofu-Setup`), a release-notes
section, and a two-row README download table. The cost of NOT uploading
is worse: no macOS agent ever, and no fallback when a deployment's own
build toolchain is down. Verdict: upload.

## 6. The interactive-session boundary (the honest v1 limit)

The tray form factor requires a logged-in interactive session: pystray
needs a window station, and the autostart mechanism (§4.4) fires at
user logon. A truly HEADLESS relay (no user ever logs in — a rack
machine, a VM that only boots) cannot run this component; it needs a
Windows-SERVICE-packaged agent (Session 0, no tray, no tk dialog,
service-control recovery). That is a deliberate v1 NON-goal: the full
app's agent has the same limit today, every current deployment scene
(office PC, home machine) has an interactive user, and service
packaging is a different installer shape (SCM registration, its own
account model) that deserves its own measured design. Recorded here so
the next "why doesn't the agent start before logon" question has a
citation instead of a re-investigation.

## 7. Security posture

- **Subtraction, not addition.** The agent build adds no new trust
  decision to the controlled machine: no listening port, no DB, no
  browser auto-open, no outbound GitHub probe. Everything it does, the
  full app's agent thread already does today — minus 100+ MB of code
  that machine never needed.
- Token handling unchanged: the bridge token travels via the minted
  connect line only; the preseed carries the non-secret URL only.
- Permission floor unchanged: `safe_default()` deny-all; tier toggles
  persist through the same `save_computer_control` the full app reads —
  switching components on one machine never silently widens or drops a
  grant.
- The egress whitelist stays server-side; the agent remains a dumb
  executor of an allowlist it does not own.
- Unsigned installer — same SmartScreen note as the full build (signing
  stays the separate, human-credentialed question).

## 8. What stays fused, deliberately

The full desktop app KEEPS its in-process agent: for the standalone user
(this machine = server + client), own-machine computer control with zero
configuration is a feature, and the tray tier-toggles are its config
surface. "Separated" is a distribution fact. The code stays ONE tree with
shared seams (connect dialog, preseed, tk theme, config file, NSIS
template, parity contract) — the components diverge in what they SHIP,
never in what they KNOW.

Alternatives considered and rejected:

- **Zip + script instead of an installer** — the NSIS wrapper is the ~1
  minute cheap half and buys uninstall, shortcuts, and the parity
  contract for free. A zip saves nothing worth having.
- **ONE installer with a components checkbox** — the 152 MB is the
  payload, not the wrapper: a checkbox in one installer still ships every
  byte to every machine. The saving this epic exists for lives in the
  second PAYLOAD, which a checkbox cannot produce.

## 9. Slices

- **A1** — `desktop/connect_ui.py` extraction + `desktop/agent_launcher.py`
  + `tofu-agent.spec` + agent Half A in `winbuilder.py` (`target='agent'`)
  + the §4.5 smoke gate + **`version` in `_build_agent_frame`** (amendment
  ② detection half: read from `lib.version`, one field, the poll payload
  already carries the frame). Tests fake the pipeline exactly like
  `test_winbuilder.py` (staged provisioning, faked downloads); NEUTER on
  the payload cache-hit logic. Acceptance: a REAL agent payload built on
  the provisioned toolchain, **weighed** (the §2 estimate becomes a
  measurement), smoke exit 0 under wine, import-graph proof green.
  **LANDED 2026-08-02** (`3e5696f5` + `d773be5e` + `85c88049`):
  `payload-agent-85c88049dfff-…tar.gz` (53.0 MB), smoke
  `TOFU_AGENT_SMOKE_OK version=0.16.0 commands=19` under wine. The smoke
  gate caught a REAL latent defect on first contact: the nuget CPython
  ships no tkinter (0 tcl files in the nupkg) — the shipped FULL
  installer's connect dialog was dead in the wild; fixed at the root
  (python.org tcltk.msi graft into the shared winpy).
- **A2** — NSIS template parametrization + Half B target support +
  **autostart task (§4.4: components page, default-on HKCU Run value,
  uninstaller cleanup)** + tray autostart toggle + 
  `test_installer_parity.py` extended to both components AND the
  autostart contract + first real
  `TofuAgent-Setup-<ver>-win64.exe` recorded `kind='agent'` in the store.
  **LANDED 2026-08-02** (`eebbec35` + `c9d51216` + `da4a6c66`):
  `TofuAgent-Setup-0.16.0-win64.exe` — **53,185,986 B (35% of the full
  installer)**, `kind='agent'`, preseeded. Two measured lessons, both
  now pinned: (a) the renderer is a global replace, so a code-valued
  placeholder named in a COMMENT expanded there and makensis aborted —
  @-tokens are banned from comments and the parity suite pins the leak
  signature; (b) the kindless agent artifact SHADOWED the full
  installer on the shared platform row (same version, same source,
  newer wrap) — the §4.6 kind filter was pulled forward from A3 into
  `store.find_for_platform` (default 'full', byte-identical for all
  current callers) with a regression test, because the shadowing was
  live the moment the first artifact landed.
- **A2b** (CI, same change as the gates) — `build-desktop.yml` agent
  legs (windows/macos/linux runners, §5.3, Inno autostart `[Tasks]`
  equivalent) + `AGENT_PLATFORM_ASSETS`
  joining `REQUIRED_PLATFORM_ASSETS` + `test_desktop_build_workflow.py`
  extended to the new rows.
  **LANDED 2026-08-02** (`894ef397`): agent steps ride the three
  EXISTING platform jobs (same venv + icons; the agent's only extra dep
  is curl_cffi) — an agent build failure fails the leg, so a release
  can never ship missing an agent asset; `REQUIRED_PLATFORM_ASSETS`
  derives over both tables in the same commit (legs without the join
  publish hollow, join without the legs fails every publish). The
  version gate's build-on-INCOMPLETE rule self-heals the current
  release into carrying agent assets on the next run. Contract pins:
  the Inno autostart authoring (HKCU / default-ON / uninsdeletevalue /
  value name == `_RUN_VALUE`) asserted from BOTH test_installer_parity
  and test_desktop_build_workflow, NEUTER-verified.
- **A3** — serving + UI: `AGENT_PLATFORM_ASSETS` rows in
  `_platform_rows_for`, `find_for_platform` kind filter, status payload,
  `_lcRenderDesktop` branch matrix, autobuild gate, mirror extension to
  agent assets + **drift projection** (amendment ② surfacing half: the
  devices list / status payload compares each agent's frame `version`
  to the server's own and flags "agent outdated → download the
  same-HEAD installer"). Parity suite for the `downloads[]` shape (the
  api-contract discipline: shape pinned by test); frontend JSDOM harness
  for the primary/secondary link branches.
  **LANDED 2026-08-02** (`069776f8`): status payload carries
  `agent_downloads` (entries with `kind`); `_with_drift` flags outdated
  agents in status + devices (versionless legacy = unknown, never a
  false flag); autobuild is per-kind (a built full never suppresses a
  missing agent build); the remote branch renders the agent installer
  primary with the full app as one-line secondary plus a
  stale-while-build fallback; mirror iterates both tables and records
  kind. `AGENT_PLATFORM_ASSETS` stays OUT of
  `REQUIRED_PLATFORM_ASSETS` until A2b (the join must be atomic with
  the CI legs, or every publish fails its own completeness gate).
- **A4** (docs only) — `desktop/README.md` + `DESKTOP_EGRESS_DESIGN.md`
  §11: retire the "copy the whole repo" stopgap in favour of the agent
  installer; README download table gains the two-component rows.
  **LANDED 2026-08-02**: both READMEs carry the two-component table
  (role / size / contents); the egress runbook's step ② now installs
  the agent from Local Control (the repo-copy path retired), step ③
  notes the packaged agent needs no manual command line.

## 10. Acceptance

A Windows visitor to Local Control (remote case) is offered
`TofuAgent-Setup-<current>-win64.exe` marked `服务器直连 · built` as the
PRIMARY download. Installed, it is roughly a third (measured in A1) of
the full installer's footprint; on first run it asks for one connect
line (or finds the server preseeded), shows a tray icon with exactly the
permission tiers, and appears in `/api/v1/desktop/devices` — with no
Tofu server, no DB, and no browser ever starting on that machine. The
full installer remains one click away for the "this machine also runs
Tofu" case, on a byte-identical pipeline to today.

**Transferred from `pt_4ea6bf05deaa46f0` (closed 2026-08-02, owner:
"不要让我手动执行命令"):** once the installer lands and the agent
auto-starts with ZERO manual commands, the real-machine OAuth round
trip is verified on that machine — browser login to claude.ai → token
exchange egressing via the agent → streaming reply → Codex O3. This is
the end-to-end proof that the component this epic ships is the product
form of the egress bridge, not a parallel one.

## 11. Pairing UX v2 — SSH auto-tunnel + one-time pairing code

### 11.1 The directive and the diagnosis

Owner (2026-08-03, via msdcksqy): "This configuration is too complex —
minimize scenarios where users write code/shell; users may know
nothing. Why doesn't Codex have this many issues?"

Diagnosis (code-verified): the connect line = address + token pushes
network topology onto the user, and in an SSO-proxy world the address
half is necessarily wrong (`request.host_url` is browser-reachable ≠
agent-reachable — measured live: the agent polled a codelab proxy URL
into an SSO 401 wall for hours while the panel showed a dead toggle).
Codex's answer is centralized identity + pure outbound relay + one
component. Our agent→server is ALREADY pure outbound (long-poll); the
only unsolved part is bootstrapping the FIRST credential onto the
agent. `88eb3302`'s four diagnostic layers made that failure visible
but still asked the user to understand tunnels. v2 removes the
disease: the panel never hands the user an address for the agent at
all.

### 11.2 The target flow — a two-step funnel (endgame first)

**The endgame is a personalized, token-baked installer.** The two
flow layers in their priority order:

1. **PRIMARY — personalized installers (owner: the real zero-config).
   Phase 2 from the earlier design lands as the default, not a later
   phase.** The panel's `downloads[]` carries an artifact baked for
   THIS user (per-user `agents:bridge` token, per-user artifact
guarding on the download route). The user downloads it, installs it
in one click, and it connects — no SSH address, no pairing code, no
first-run dialog at all. The agent reads its attachment from the
baked `preseed_server.json` on first launch and runs.

2. **FALLBACK — pairing code (cross-machine / unbaked / re-image).**
When the panel offers only the GENERIC artifact (a release download,
a second machine, a machine that arrived before per-user baking
landed), the pairing-code flow below kicks in. The pairing code is
NOT the endgame; it is the graceful fallback to the generic
artifact.

#### 11.2.1 First-launch discovery ladder (zero-question goal)

On first launch the agent runs a **silent auto-discovery ladder** and
ONLY asks the user when every rung comes back empty — and then only
once. Rungs in order, each short-circuiting the rest on first hit:

| Rung | Mechanism | Cost to user |
|---|---|---|
| A | Probe `http://127.0.0.1:15000/api/health` — the user's OWN tunnel is
      up, or the user is the server's own machine (local_source) | Zero — silent |
| B | LAN discovery: the agent broadcasts a UDP query; the server's
      responder (ON by default since 2026-08-04 —
      TOFU_DESKTOP_LAN_DISCOVERY=0 disables; silent on loopback binds)
      advertises `http://<lan-ip>:15000`. Best-effort, ~2 s budget,
      silent when nothing answers. (mDNS is deliberately NOT relied on —
      corporate networks filter multicast; a plain UDP broadcast with an
      HMAC'd response is the v1 primitive.) | Zero — silent |
| C | SSH candidates: parse `~/.ssh/config` first Host, then VS Code
      Remote `remote.SSH.remotePlatform` / `~/.ssh/known_hosts`. For
      each, attempt `ssh -N -o BatchMode=yes -o ConnectTimeout=3
      -o ExitOnForwardFailure=yes -L <free>:127.0.0.1:15000 <host>`;
      first tunnel whose local port answers Tofu's health wins. | Zero — silent |
| D | **Ask once** (only if A–C all empty): the first-run dialog
      requests 「服务器 SSH 地址」(prefilled from the first surviving
      candidate) + 「配对码」(from the panel). The dialog appears ONCE.
      A second open reuses the result. | One prompt, then done |

On any hit the agent proceeds to pairing-code exchange (rung A–C
already hold a working tunnel; rung D spawns one) and then runs.

#### 11.2.2 Panel side (ONE action)

The remote branch collapses to a single primary action: 「配对这台
电脑」 mints a 6-digit one-time code (5-min TTL), displayed large
with a copy button and a countdown, plus one sentence: "把这 6 位
数字填进受控端首次启动". The connect line is demoted to
`<details>` 高级连接行 (keeping the paste-time probe and the
tofu-vs-proxy 401 diagnostics for the unbaked/generic case).

#### 11.2.3 Pairing exchange

`POST /api/desktop/pair {code, name, platform}` (no bearer — the code
IS the credential; peer loopback-checked only as depth-in-depth) →
agents:bridge token, minted by the SAME machinery as
`desktop_token_mint` (audit-logged, revocable on the devices page)
→ agent calls `save_remote_server(url, token)` → runs.

#### 11.2.4 Measured alternative — browser-extension relay

A FACT (verified at the server side 2026-08-03): the browser
extension polls `/api/browser/poll` THROUGH the SSO proxy while
carrying the user's SSO session; the bare agent's direct polls get
401'd by the SSO edge. So an office agent could relay its poll
frames through the LOCAL browser extension and need NO SSH tunnel at
all.

| | SSH auto-tunnel | Extension relay |
|---|---|---|
| Setup cost | ssh key login once (or once per new machine) | Install + sign-in to the extension once |
| Prereq on the machine | ssh.exe + a server the user can ssh to | Browser + extension open, live user session |
| Works headless / pre-logon | Yes | No (needs the browser running) |
| Always-on | Yes (agent owns the tunnel; reconnect backoff) | No (session-bound) |
| New wire protocol | No (agent→tunnel→server is the existing poll) | Yes (poll frames forwarded through the browser sandbox — a new relay contract) |

Verdict for v1: **SSH tunnel is the default relay** (always-on, no
new protocol). Extension relay is a measured ALTERNATIVE for machines
without SSH access — prototype + comparison as slice P5, not v1.

### 11.3 Security — what the boundary actually is

- The boundary is the CODE: 6 digits (1e6 space), TTL 300 s, one-shot,
  3-attempt lockout per code. NOT the peer address — an SSO proxy
  forwards requests that also present loopback, so a loopback check
  buys nothing (kept only as defense-in-depth; documented as such).
- Trust model: anyone who can open an SSH channel to the server
  already has a shell there (this deployment's reality) — the code
  does not widen that.
- The exchange mints via the SAME key machinery as the connect-line
  token (agents:bridge scope, per-user, audit-logged, revocable on
  the devices page).
- LAN-discovery responder (rung B) only advertises `http://<ip>:15000`
  (same info class as the hostname) and HMAC's its response — a
  minimal new surface, off by default, documented honestly.

### 11.4 What retires, what stays

Retired from the panel: proxyWarn / awaitingAgent (the problem they
diagnose cannot occur — no address is minted for the agent); the
connect line as the PRIMARY action. The currently blocked human-gated
office steps (owner manually running `ssh -L` and pasting a
proxy-URL connect line) are explicitly OBSOLETE once v1 lands — the
owner confirmed they will not perform a flow that is about to be
eliminated, and this design documents that. Kept for the advanced
path and the tray: paste-time probe (`_probe.py`), tofu-vs-proxy 401
envelope classification, `run_agent(on_status=)` + the tray link line.
Honest boundary: SSH-less pure public relay needs a public
rendezvous service — a different magnitude of infrastructure, out of
v1.

### 11.5 Slices

- **P1** server: `lib/desktop/pairing.py` (code store) +
  `POST /api/v1/desktop/pair-code` (mint) + `POST /api/desktop/pair`
  (exchange) + the LAN-discovery responder + full contract tests
  (mint/consume/expiry/lockout/one-shot/loopback/envelope/broadcast).
  **LANDED `b0b42ff9`** (+ per-IP global failure budget on the exchange
  after owner review: per-code lockout alone leaves 1e6 space
  brute-forceable via fresh-code guessing).
- **P2** panel: pairing action + big-code display + countdown, connect
  line into `<details>`, warnings retired, JSDOM harness updated.
  **LANDED `2043d23f`** ( Api.desktop.mintPairCode + `_lcPairCode`;
  `local.agentStep2` retired for the pairing key family).
- **P3** agent: `lib/desktop_agent/_pair.py` (exchange client +
  loopback/LAN/ssh-config ladder + BatchMode self-tunnel kept alive for
  the poll loop) + first-launch pairing dialog (address prefilled by
  the ladder, editable; 6-digit code; precise failure reasons) with the
  connect line behind "Use a connect line instead…" + launcher wiring
  (first run AND tray reconnect share `prompt_attachment_flow`);
  fakes + Linux smoke. **LANDED (this commit)**: the planned
  `_tunnel.py`/`_discover.py` folded into `_pair.py` — one module owns
  the whole "find + prove + keep" path, so no three-way drift; the
  dialog appears with the ladder's answer prefilled whenever it found
  one, not only on a total miss.
- **P4** acceptance (real machine): install → two fields (or zero,
  when the ladder finds the server) → connect → the §10 OAuth
  chain. Supersedes the blocked office steps of the connect-line
  flow; the board epic transitions to a P4 acceptance gate on real
  hardware. **Needs a rebuilt agent installer** carrying `_pair.py`
  (the in-store 0.16.0 predates it).
- **P5** (deferred, post-v1): extension-relay prototype + a measured
  comparison against the SSH tunnel on headless-capability, setup
  cost, and session-boundedness.

## 12. Pairing retired — the zero-config attach bundle (v5, 2026-08-05)

> **Owner decree (2026-08-05): "do not design any pairing code; either
> hardcode it directly into the installation package, or do not design
> it at all. We do not allow adding these configuration burdens to
> users."** §11's pairing-code UX is RETIRED. This section is the
> replacement contract.

### 12.1 The measured failure that killed §11

Real-machine acceptance (owner, 2026-08-05): the agent installed and
showed "controlled by a Tofu server", yet the panel sat on 未运行
forever. The evidence chain, all server-side:

* `POST /api/desktop/poll` arrivals in access.log: **0** on the day; the
  agent NEVER reached Tofu — not a wrong code, a dead route.
* The agent's saved address was the vscode proxy URL with BOTH the
  https scheme and the `/proxy/<port>` prefix stripped (minted from
  `request.host_url`, which structurally cannot see the prefix) — the
  same bug class as the 2026-08-04 extension "HTTP 405" incident.
* Even the CORRECTED proxy URL is a dead end for the agent: the SSO
  edge answers every cookieless `/api/*` with 401 before Tofu ever sees
  it (measured 2026-08-03, `_host_reachability`'s own docstring). The
  browser sails through on SSO cookies; the agent has none.
* And a platform-injected `BIND_HOST=127.0.0.1` env quietly overrode
  the 0.0.0.0 default, killing the direct-LAN route too.

A pairing code typed into a dialog could never have fixed ANY of these
— the code was redeemable only through the address that was already
dead. The code was a configuration burden AND not the blocker.

### 12.2 The v5 flow — download IS the pairing

1. The panel's ONE action is 「下载受控端 ZIP」 →
   `GET /api/v1/desktop/agent-bundle` (authenticated):
   * mints a fresh per-user `agents:bridge` token AT THE CLICK (fail-open
     when the keystore is down — an open bridge polls tokenless);
   * builds the ordered route candidates: direct `http://<lan-ip>:<port>`
     FIRST (only when the running bind is not loopback — the same honesty
     guard as the LAN discovery responder), the panel's live
     `origin + BASE_PATH` (host-pinned `?base=`) LAST;
   * streams `TofuAgent-Setup-<ver>-win64.zip` = the generic exe (stored,
     not re-deflated) + `tofu-agent-attach.json {token, candidates,
     fallback_candidates}`.
   * 409 + an automatic rebuild kick when the store's exe predates the
     attach flow (`git_sha != HEAD`) — serving a bundle an old payload
     would silently ignore is a lie; `agent_bundle_ready` on the status
     payload lets the panel render the honest "rebuilding" note instead
     of a dead button.
2. The NSIS installer adopts `$EXEDIR\tofu-agent-attach.json` into the
   install dir (no-op when absent — bare-exe installs keep working).
3. The agent's first run (`import_attach_bundle`): probes candidates →
   the discovery ladder (loopback → LAN broadcast → ssh self-tunnel) →
   fallbacks; first live `/api/health` wins; token + full route set are
   persisted (`attach_candidates`) so `resume_attachment` re-points a
   dead route by itself. NOTHING answers → the first candidate is saved
   optimistically (the server may simply be off; the poll loop retries).
   One-shot: the token-carrying file is deleted after any attempt.
4. The role window shows the tray's live link verdict (connected /
   unreachable / proxy-blocked / auth-failed / unconfigured), refreshed
   every 3 s — a window that says "controlled by…" while never
   connecting was the 2026-08-05 lie.

### 12.3 What retired, what stays

Retired: the panel's 「配对这台电脑」 button + 6-digit code UX
(`_lcPairBlockHtml` / `_lcPairCode` / `local.pair*` keys), the agent's
first-run pairing dialog (`prompt_attach` / `prompt_attachment_flow`),
the agent-side exchange client (`exchange_pair_code`). Stays: the
server-side `/api/desktop/pair` + `/api/v1/desktop/pair-code` endpoints
and the code store — SHIPPED-installer compat only (the 0.16.0 in the
field embeds that path); no UI may mint or collect codes again. The
connect line stays as the collapsed advanced fallback (bare-exe repair
path). `preseed_server.json` stays as the build-time, URL-only default.

Also v5: `server.py` boots with a loud banner when bound loopback
behind a cloud-IDE proxy (`VSCODE_PROXY_URI` set) — remote agents can
never attach in that state, and it previously failed silently.

### 12.4 Acceptance (supersedes §11.5 P4)

Fresh panel download → unzip → run installer → **zero input** → the
panel's 这台电脑 row turns green within a minute (a `POST
/api/desktop/poll` arrival appears in access.log); day-2 reboot
auto-reconnects (resume walks `attach_candidates` → ladder, token kept).
The run-from-inside-the-zip trap (Windows extracts only the exe to a
temp dir) degrades honestly: no bundle file → discovery ladder →
unattached role window with the link line, never a silent lie.

## 13. v6 — repair-aware import, self-healing poll loop, diagnostics return channel (2026-08-06)

The day-2 field failure of v5, measured end to end: the owner reinstalled
from a fresh bundle and the agent STILL never polled — the window said
「地址被代理/SSO 拦截——正在自动重找通路」 forever. Three root causes, all
fixed in the agent + one server-side seam:

1. **A dead saved route vetoed the repair material.**
   `import_attach_bundle` skipped the bundle whenever ANY attachment
   existed — including a DEAD one — and the one-shot delete then
   destroyed the bundle unused. The v5 "never override" discipline was
   written to protect the user's own LIVE connect; it was never meant to
   let a stale dead URL block the very download that existed to fix it.
   Now the existing attachment is PROBED first: alive → bundle ignored
   (unchanged); dead → the bundle re-points the attachment (candidates →
   ladder → fallbacks), the bundle's fresh token wins (an empty bundle
   token keeps the old secret), and the dead address is demoted to a
   TRAILING attach candidate rather than trusted again.
2. **The link line promised re-discovery the loop never performed.**
   The `proxy` / `unreachable` branches slept and retried the same URL
   forever. `run_agent` now takes `route_repair`: after
   `_ROUTE_REPAIR_THRESHOLD` (6) consecutive route-dead polls — `ok` and
   `auth` reset the streak, since both prove the address reached Tofu —
   the launcher-supplied hook re-walks `resume_attachment` (persisted
   candidates → ladder) and a live replacement rebinds the loop's
   endpoint + credential IN PLACE; `_ROUTE_REPAIR_COOLDOWN_S` (300s)
   bounds how often the (up-to ~30s) ladder walk may run.
3. **Probe and poll measured different networks.** `requests` honors
   system/env proxies (Windows registry included) while the poll loop
   pins `no_proxy: '*'`; a probe verdict on the proxied transport says
   nothing about the poll's direct one. `probe_server` now pins the same
   `no_proxy: '*'`.

**The diagnostics return channel.** A controlled machine that cannot
reach the server cannot push its logs anywhere — debugging it was blind.
The agent window (and tray) now carry「复制诊断信息」: one click copies
the evidence pack (saved route, persisted candidates, live link verdict,
last 120 log lines; secret reported as presence+length only). The Local
Control panel gained the collapsed「受控端连不上？把它的诊断信息粘贴到
这里」inbox: the paste POSTs to `/api/v1/desktop/client-diag` and lands
in `logs/desktop_client_diag.log` (JSONL, 200k cap), GET replays the
recent submissions so the panel confirms arrival. No shell access, no
screenshots, no retelling.
