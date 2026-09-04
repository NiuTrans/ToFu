# Browser automation

This domain lets an authenticated owner use a specific connected Chromium
extension for page reads and approved mutations. It owns device registration,
protocol negotiation, command routing, leases, consent, site adapters, and
redacted audit. Server-side fetching and rendering are adjacent transports,
not alternate browser authorities.

## Ownership

| Concern | Owner |
|---|---|
| Extension poll/result bridge | `routes/browser.py`, `lib/browser/queue/` |
| Authenticated browser-to-server file transfer | `lib/browser/file_transfer.py`, bridge transfer routes, extension `fetch_file_to_server` |
| Protocol and capability contract | `lib/browser/protocol.py`, extension manifest/background |
| High-level page API and leases | `lib/browser/page.py`, `leases.py` |
| Read-deny and write-grant policy | `lib/browser/access.py`, Sidecar browser operations |
| Credential-safe diagnostics | `lib/browser/log_safety.py`, durable-sink `lib/log_redaction.py` |
| Captured API evidence | extension `background.js`, `lib/browser/network_evidence.py` |
| Generic deep research | extension `research_url`, `lib/browser/research.py` |
| DevTools Bridge | extension CDP broker, `lib/browser/handlers/_devtools.py`, `browser_devtools` |
| Site adapter schema/runtime | `lib/browser/adapters.py` |
| Site-specific knowledge and health | `lib/site_knowledge.py`, `lib/site_doctor.py` |
| Model-facing browser tools | `lib/tools/browser.py`, `lib/browser/dispatch.py` |
| Settings/status UI | browser settings and Local Control frontend modules |
| Extension installation/release | `browser_extension/`, extension release scripts |

The extension bridge accepts one current protocol. Every poll declares a
stable device ID, protocol version, capability set, profile, and authenticated
owner. Commands, results, streams, leases, and grants are all addressed by
owner plus device; “most recently seen globally” is never an authority seam.

## Integration decision

OpenCLI's useful browser mechanisms are internalized here, not invoked through
an OpenCLI subprocess or a second long-running Node browser daemon. In
particular, Tofu adopts capture-before-navigation, CDP response-body reads,
bounded network-idle settling and generic response ranking. Device selection,
logged-in tabs, domain policy, leases, cleanup and audit remain owned by the
existing Tofu bridge. This avoids a second browser/session authority and a
second unbounded cache/process budget while keeping `tofu-search` behind its
existing host-provider interface.

## Runtime flow

1. Bridge middleware authenticates an owner-scoped device credential.
2. Poll validates device, protocol and capabilities, registers bounded live metadata, and settles only results claimed by that owner/device.
3. A task selects one device in its owner fleet and leases it with explicit profile, task and tab policy.
4. Capability negotiation rejects unsupported commands before enqueue.
5. Load-waiting navigation starts bounded CDP Network capture before leaving `about:blank`, then waits for DOM and network stability. The Tofu client tab (`isClient`, URL under the server origin) is never a navigation target or working-tab seed; targeting it opens a new foreground tab whose id the server re-binds as the working tab.
6. Poll atomically claims only commands addressed to that owner/device.
7. Server and extension re-check current tab/domain and grant before every page action.
8. API/WebSocket evidence is type-filtered, ranked, authorized per URL, redacted, and merged under one context budget.
9. A URL-addressed file that needs browser cookies is streamed from the response body to bounded server staging; it never enters Chrome's download manager.
10. Deep research may accumulate virtual-list/hydration data and recognized same-origin pagination in an owned background tab; cross-origin traversal stops.
11. DevTools, network, screenshot and trusted input share one serialized per-tab CDP lease; the last holder detaches.
12. Settlement verifies claim ownership; timeout/cancel/release closes captures, transfers and ephemeral tabs. All leases share one launch-probed process capacity (`TOFU_BROWSER_SESSION_LEASE_CAPACITY`: lean personal 64, distributed 2,048, adaptive personal aligned with the device registry). One lifecycle sweeper orders timed leases instead of one sleeping thread per lease; persistent leases hold capacity until owner release, and failed admission never displaces an existing owner/device lease.

## Access policy

| Operation | Default | Authority scope |
|---|---|---|
| Read/navigation | allowed unless denied | owner + domain + device/profile |
| Captured API response | allowed unless denied | owner + each response URL |
| Authenticated file export | allowed unless denied | exact owner + source/final domain + device/profile + one-time transfer token |
| Adapter search/detail | adapter-declared read domains | owner + live lease |
| Page mutation | denied until approved | exact owner + domain + device + profile |
| Console/source/debug lifecycle | allowed unless denied | owner + current/source domain + device |
| DevTools expression/breakpoint/execution control | denied until approved | exact owner + current domain + device + profile |
| Cleanup | allowed | resources owned by the lease |

A write approval is durable only for its exact scope. It does not cross to a
subdomain, redirect target, profile, browser, or owner. New grants require the
target device to be connected for the authenticated owner. Audit projections
never include cookies, credentials, page bodies, uploads, or raw script data.
Browser URLs in diagnostics retain only their HTTP(S) origin; userinfo, path,
query, fragment, and URLs embedded by exceptions are discarded fail-closed.
File-completion audits retain domains, byte count and digest, never the
response filename or URL.

## Adapter contract

Each `SiteAdapter` declares identity/version, domains, login URL, risk, and commands; every command declares access, JSON schemas, capabilities, timeout and one native handler. Inputs, outputs and returned URLs are boundary-validated.

Xiaohongshu and ModelPlaza prefer the authenticated rendered DOM over private-signing reimplementation. `tofu-search` sees only read-only `SiteSearchProvider`; it never owns tabs, credentials, grants, or device choice.

Friday Skills Market uses generic deep research, preserves its URL filter contract, captures the page's authenticated APIs, chooses/redacts the strongest list response, and normalizes records. It proves the generic path rather than adding a hostname capture exception.

Generic SPA extraction captures textual XHR/fetch/EventSource bodies and bounded text WebSocket frames. Sparse `browser_read_page` auto mode uses useful evidence; `mode="data"` exposes ranked evidence, and `fetch_url` receives it through the existing `tofu-search` provider seam.

## File acquisition and location contract

File location is a typed outcome, never an implication of “download”:

| Receipt location | Meaning | Writer |
|---|---|---|
| `device_downloads` | Chrome saved the file on the device running the extension | explicit legacy `download` / `wait_download` commands using `chrome.downloads` |
| `server_staging` | The Tofu server atomically committed a verified file under `data/fetched/` | server-direct fetch or negotiated `file_export` streaming |

`fetch_url` tries server HTTP first. If its authenticated browser text fallback discovers an extensionless attachment/non-text response, the extension streams that exact `fetch()` Response instead of issuing a second GET; a typed host handoff then stops the legacy text provider before cookie replay or anonymous HTTP can issue another request. Signed/one-time URLs therefore remain valid, including the post-login retry. Known binary suffixes skip tab navigation. Only a positively textual probe may open a rendered tab; missing metadata or probe failure stays on the byte path or fails explicitly. The server does not dispatch `fetch_url` to workers lacking `file_export`, because they predate this guard and could guess their way into a client download. `browser_download_url_to_server` is the explicit model contract when the requested outcome is a server file. Its browser namespace is intentional: the same contract can use a logged-in browser even though bounded server HTTP remains the first transport. It accepts an exact URL or resolves `text`/`selector` on one explicitly owned tab to the full link before acquisition, avoiding the 200-character interactive-element projection for signed URLs. Element resolution is a fixed, read-only DOM attribute projection; it never clicks, submits a form, interprets page JavaScript, or reads cookies. The tool then uses one acquisition owner: bounded server HTTP first, followed by the selected owner/device's `file_export` transport when the direct response is unavailable or is an HTML/login page. Success always returns `server_staging`, absolute path, size, SHA-256 and transport; the model never chooses or reconstructs the authentication transport. The pre-migration name `download_url_to_server` is an execution/search alias only and never a second wire schema. A narrow pre-subprocess guard recognizes single-URL cookie-bearing `curl`/`wget` file-download commands and routes them through this same owner. Ambiguous shell expansion/pipelines are blocked rather than allowed to replay credentials. The guard does not rewrite ordinary API inspection, uploads, non-GET requests or cookie-free shell commands, and a redirected `-o` destination remains unwritten until a separate authorized filesystem action copies the staged file.

The service-worker request uses `credentials: include` plus the extension's host permission, so Chrome applies its normal Cookie, SameSite and third-party-cookie policy. The bridge never extracts, copies or replays site cookies and cannot bypass a browser policy that withholds them. Model-facing `browser_get_cookies` exposes names, scopes and flags only; values are redacted. Internal verified login capture may inspect domain-scoped values inside the browser/auth-source owner, but values never enter tool results, task metadata or logs.

Before dispatch the server binds owner, device, source URL, profile, random transfer ID and one-time token. It accepts status/headers then ordered chunks, checks redirect policy at start and source/final policy again at commit, verifies declared length, hashes, fsyncs and atomically renames. The extension receives a path-free receipt; the exact originating task context—not a URL/global recency scan—claims the transfer ID and consumes the server-authored path. Unclaimed task handoffs are deleted when that binding closes.

Text-only search contexts omit the handoff entirely, so a classified attachment fails safely without streaming bytes into staging that no file owner can claim.

Staging is not arbitrary destination authority: copying into a project is a later filesystem action under that subsystem's policy. It is reconstructible and reclaimed oldest-first under a launch-probed budget or seven-day TTL, so models should materialize files the user wants to keep. Element resolution accepts anchor/area `href` plus bounded `data-download-url` / `data-href` / `data-url` attributes. Export currently represents only a complete HTTP(S) GET; 206/range, form POST, `blob:` and click-triggered downloads fail until their request/gesture provenance has an explicit contract.

Limits: every non-final chunk has a 16 KiB floor and every chunk has a 256 KiB ceiling; chunk count is derived from the byte ceiling; 2 active browser transfers/owner; 8 process-wide; 16 KiB/control envelope; 180-second inactive registry TTL; 500 MiB hard per-file ceiling plus the smaller live `FETCH_MAX_BYTES`; 2,048 staging artifacts. Server-direct and browser-authenticated receipts share this byte/inode budget, TTL sweep, live free-space reserve and ten-minute usefulness grace, so direct fallback cannot grow an unbounded parallel cache. Opportunistic response classification and streaming share one 30-second operation budget, leaving five seconds for command settlement; explicit export remains bounded by the 120-second command ceiling. No whole-file browser transport buffer exists. Every reservation and write preserves the shared live `TOFU_STORAGE_MIN_FREE_BYTES`; fresh receipts reject new work under pressure instead of being evicted. `TOFU_BROWSER_STAGING_MAX_MIB` is the retained compatibility name for the shared download-staging budget: 1% of launch-time free disk in personal mode, clamped to 64–2048 MiB (lean fallback 256 MiB) and hard-capped at 4096 MiB. `TOFU_BROWSER_STAGING_TTL_HOURS` defaults to 168 and is clamped to 1–720; its lazy sweep runs on the next staged download. Poll admission is mandatory even in open mode: a bounded credential-digest/global gate runs before storage auth, then an explicit owner gate bounds throughput and concurrency. Launch-probed `TOFU_BROWSER_POLL_MAX_INFLIGHT`, `TOFU_BROWSER_POLL_MAX_WAITERS`, `TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY` and `TOFU_BROWSER_POLL_BODY_MAX_MIB` cap all resident/request multipliers; the personal fallback is 8 requests, 8 waiters, 64 recent devices and 32 MiB/body. One poll returns at most 32 commands and accepts at most 64 results; device/result IDs, capability count/items and metadata strings are structurally bounded before registration or settlement. Extension 5.4.1 sends 32-result/12-MiB batches. Login-wall remediation reserves before its synchronous probe and hands the same slot to its at-most-ten-minute background poll; the poll budget derives a lean 4-process/2-owner fallback and 64/8 ceiling, while saturation fails the current fetch cleanly without opening a tab. Its 15-minute cooldown retains 32 lean/512 maximum LRU routes. The 20-second live-session cache retains 256 lean/8,192 maximum owner/device/domain results and clears with other reconstructible TTL caches under memory pressure. Working-tab affinity shares the device-registry route ceiling (64 lean, 2,048 distributed, 8,192 hard), expires after 30 minutes without a real tool action, and clears under pressure; display reads do not renew it, while a closed-tab receipt removes it immediately and the next miss safely reseeds from `list_tabs`.

`browser_research_page` captures before navigation, accumulates open-shadow/virtual-list text, recognizes hydration globals and JSON-LD, scrolls, follows only recognized same-origin next/load-more controls, and returns content plus source strategy. Pagination calls collapse by method/host/path; inferred shapes contain types, not values.

Capture is reconstructible, never durable: at most 4 active captures; each has 80 responses, 160 in-flight methods, 40 text WebSocket frames, 384 KiB per response/frame and 1 MiB total; only 12 completed tab snapshots remain in MV3 memory. Static/binary assets are excluded, telemetry is de-ranked, duplicates removed, secrets redacted, and every response URL re-authorized before at most 80,000 model characters.

Research runs in the serial browser lane for at most 65 seconds, 5 same-origin pages and 8 scrolls/page, with 80,000 DOM and 160,000 transient hydration characters; it always destroys its tab. Arguments only lower ceilings. Cookie replay, Playwright and direct HTTP are explicit separate transports; private-host SSRF exceptions apply only to server fetches.

## DevTools Bridge

`browser_devtools` is one model tool over `chrome.debugger`, not a DevTools page and not a Developer-mode toggle. Developer mode is used only to install the unpacked build; runtime attaches CDP to an ordinary HTTP(S) tab. It needs no OpenCLI/Node daemon, Playwright, or `tofu-search` install.

The actions are:

- bounded console/error reads and clearing, execution-context discovery;
- Promise-aware expression evaluation and recursive object inspection;
- temporary debugger start/state/stop, URL breakpoints, pause/resume/step;
- paused-call-frame evaluation and bounded script-source retrieval.

Inspection uses own `Runtime.getProperties`, never invokes getters, and releases object groups. Only same-origin iframe/worker targets survive auto-attach. Source/context/frame/console URLs are policy-filtered, secrets redacted, and HTTP(S) breakpoint source URLs separately authorized.

Limits: 2 debug tabs, 4 observers; per tab 200 console entries/256 KiB, 80 contexts, 120 scripts and 24 breakpoints; per object 400 nodes/60 KiB. Sessions last 10–120 seconds (default 60), pauses auto-resume at 30 seconds, output is 60,000 characters, and recent console evidence covers 12 MV3-memory tabs. Close, timeout, stop, external detach or worker reclaim discards all raw debug state.

The tool appears only with an owner-scoped current extension and is selected for requests such as “check this console error” or “debug this click.” `debug_start` precedes debugger actions; `debug_stop` ends them. Console/source/lifecycle reads need no write grant; expressions, breakpoint creation, pause/resume/step and frame evaluation do, with exact risk shown in approval.

Chrome permits one debugger owner per tab. Open DevTools, another debugger, or external detach yields an explicit error; the bridge never seizes the DevTools UI. `chrome://`, extension, file, view-source and DevTools pages are refused.

## User experience

Settings shows device/version/protocol/capabilities, adapter health, denials and grants. Missing capability names the device and links to Local Control upgrade; a handshake-rejected device stays outside the registry while a bounded owner-scoped recovery note offers the current build. The first 426 carries `Retry-After`, then a bounded credential-digest cooldown rejects before storage auth; a current-protocol header clears only that stale cooldown after an in-place upgrade while the authenticated frame remains authoritative. Version 5.4.1 preserves results across 429 or transport failure, obeys server delay, and bisects 413 batches before failing only an oversized singleton. A same-device retry seamlessly supersedes its older waiter and both retain normal 200 semantics. Repeated 4xx access rows keep first/power-of-two/heartbeat checkpoints plus exact metrics instead of durable-log spam.

Negotiated DevTools shows “ready”; invocations retain their timeline badge, approval dialog and Chrome debugging indicator. Adapter failures show unavailable, failed policy writes stay failed, and no unexplained upgrade badge is valid. Search exposes Fast/Balanced/Deep; backend presets own limits and custom overrides stay explicit.

## Failure semantics

- Missing device/protocol/capabilities: reject the poll with HTTP 426 + `Retry-After`; do not
  register an anonymous or downgraded client. Retain only bounded owner-scoped
  recovery/cooldown notes needed to direct an installed device to upgrade.
- Wrong owner/device result: ignore/reject it without settling another task.
- A response-URL policy lookup failure withholds captured bodies and emits one
  rate-limited diagnostic checkpoint; it never degrades into allow.
- Unsupported adapter command: fail before enqueue with upgrade guidance.
- Redirect or tab race: deny the action at the last possible boundary.
- CDP unavailable or already attached: continue with DOM text and URL-only
  network metadata; never label missing bodies as successfully extracted data.
- DevTools session absent/expired or externally detached: return an explicit
  error; resume a paused target when possible and discard all transient state.
- No useful captured body: `mode="data"` says to navigate/reload with the
  current extension; auto mode keeps the prior sparse-page diagnosis.
- Deep traversal stalls, has no safe next control, or reaches a cross-origin
  redirect: return the collected prefix with an explicit stop reason; never
  guess a URL or click an unrelated control.
- Extension disconnect/timeout: expire the command and release owned resources.
- Policy denial: return non-retryable V2 `browser_access_denied` and settle as error. Arbitrary `browser_execute_js` is always write-classified even when its expression looks read-only. In this unattended deployment a missing write grant no longer raises `browser_write_authorization_required`: `require_access` auto-creates the durable exact-domain grant on first use (only when a concrete browser client is identified; read-denied domains still fail closed). An origin-relative `fetch('/…')` without `tab_id` fails earlier with retryable `browser_explicit_tab_required`, so the server never guesses a page origin or requests a grant for an unrelated remembered tab. The temporary research tab never silently becomes the working tab.
- Wrong transfer owner/device/token, an out-of-order/conflicting chunk, or a
  newly denied redirect: reject without committing; delete partial bytes.
- Oversize response or exhausted staging budget: fail explicitly before
  dispatch when knowable, otherwise abort the stream; never fall back to a
  client-device download.
- Completed but unsettled transfer: retain only until the 180-second registry
  TTL, then delete it. Consumed server staging follows the bounded staging
  retention policy.
- Invalid adapter output: typed failure; never feed malformed data into search.

## Invariants

- Current protocol only; no anonymous client, protocol downgrade, unbounded poll/body/waiter/client state, or client-controlled bypass of server admission.
- Every command has one owner, one target device, and one claiming device.
- Result settlement matches command ID, owner, and claiming device atomically.
- Device discovery is owner-scoped and never based on a process-global poll.
- Mutation authority is exact-domain and refreshed immediately before action.
- Network bodies are transient, bounded, redacted, and independently checked
  against the owner's read policy before entering model context.
- Deep research is one bounded background-tab lifecycle; it never persists a
  crawl frontier, captured body, hydration state, or per-site session global.
- Adapter schemas are the single machine-readable command contract.
- One broker owns each tab's CDP attachment; subsystems hold bounded leases and
  never attach/detach behind one another.
- Debugger state is time-bounded and same-origin; inspection never invokes a
  getter, and a paused page has a 30-second resume failsafe.
- Server fetch policy and browser consent never grant one another authority.
- `downloads` always means `device_downloads`; `file_export` always means
  `server_staging`. Neither location may be inferred from a natural-language
  verb or an untyped download ID.
- `browser_download_url_to_server` is the canonical server-location intent; its schema requires a bounded URL/text/selector target, and cookie inspection or shell HTTP are never alternate authentication transports. `browser_preview_page` requires exactly one project HTML path or HTTP(S) URL and exposes the executor's bounded viewport/wait envelope.
- Browser file export is GET-only, bounded, sequential, digest-verified and
  atomically committed; server path authority never crosses the bridge.

## Change routing and tests

| Change | Start here | Verify |
|---|---|---|
| Poll/claim/result wire | `routes/browser.py`, `lib/browser/queue/` | async poll, owner/device isolation, expiry |
| Browser-to-server file export | `file_transfer.py`, bridge routes, extension | owner/device/token isolation, same-response reuse, chunk/total/disk bounds, atomic commit, cleanup, typed location |
| Protocol capability | `protocol.py` + extension | parity, rejection, upgrade UI |
| SPA/network evidence | extension + `network_evidence.py` | pre-navigation capture, bounds, redaction, per-URL policy |
| Generic deep research | extension + `research.py` | traversal bounds, endpoint shapes, redirect stops, cleanup |
| DevTools Bridge | extension + `_devtools.py` | CDP reuse, capability gate, object/console bounds, pause failsafe, redaction |
| Consent or redirect policy | `access.py`, Sidecar domain | exact scope, revocation, redirect race |
| Adapter | `adapters.py`, site module | schema, domain, audit, bound provider |
| Search profile | search settings resolver | preset behavior, i18n |

```bash
pytest -q tests/test_browser_async_poll.py tests/test_browser_v2_surface.py
pytest -q tests/test_browser_access_api.py tests/test_browser_native_runtime.py tests/test_agent_terminal_failure_breaker.py
pytest -q tests/test_browser_file_transfer.py tests/test_bridge_auth.py
pytest -q tests/test_browser_network_evidence.py tests/test_browser_read_payload.py
pytest -q tests/test_browser_research.py
pytest -q tests/test_browser_devtools.py
pytest -q tests/test_browser_adapter_upgrade_ui.py tests/test_auth_sources_xhs.py
```
