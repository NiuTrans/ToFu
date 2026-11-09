# Browser automation
This domain lets an authenticated owner use one connected Chromium extension for page reads and approved mutations. It owns devices, protocol, routing, leases, consent, adapters and redacted audit. Server fetching/rendering are adjacent transports, not alternate browser authorities.

## Ownership

| Concern | Owner |
|---|---|
| Server poll/result bridge | `routes/browser.py`, `lib/browser/queue/` |
| Worker transport, receipts and command lifecycle | `browser_extension/worker/`, composed by `background.js` |
| Authenticated browser-to-server file transfer | `lib/browser/file_transfer.py`, bridge transfer routes, extension `fetch_file_to_server` |
| Protocol and capability contract | `lib/browser/protocol.py`, extension manifest/background |
| High-level page API and leases | `lib/browser/page.py`, `leases.py` |
| Read-deny and write-grant policy | `lib/browser/access.py`, Sidecar browser operations |
| Browser diagnostics and bounded logs | `lib/browser/log_safety.py`, durable-sink `lib/logging_pkg/log_record_values.py` |
| Content target/receipt authority | `lib/browser/content_contract.py`, `lib/browser/content.py` |
| Page media inventory (`browser_read_page(mode="media")`) and media download targets | `lib/browser/media.py`, `lib/browser/download_target.py` |
| Embedded page data islands (JSON scripts, RSC flight streams, inline-assigned globals) | `lib/browser/data_islands.py` |
| Captured API policy, redaction and inventory | extension `background.js`, `lib/browser/network_evidence.py` |
| Interactive-element collection (read_page elements, text=/selector click/fill targets) | extension `background.js` `_getInteractiveElements`, `lib/browser/_resolve.py` |
| Generic deep research | extension `research_url`, `lib/browser/research.py` |
| DevTools Bridge | extension CDP broker, `lib/browser/handlers/_devtools.py`, `browser_devtools` |
| Native site discovery/execution, pacing and declarative site packs | `lib/browser/site_tool.py`, `lib/browser/adapters.py`, `lib/browser/site_pacing.py`, `lib/site_packs/` ([`../SITE_PACKS.md`](../SITE_PACKS.md)) |
| Site observations, verified knowledge and health | `lib/browser/site_observations.py`, `lib/site_knowledge/`, `lib/site_doctor/` |
| Model-facing browser tools | `lib/tools/browser.py`, `lib/browser/dispatch.py` |
| Settings/status UI | browser settings and Local Control frontend modules |
| Extension installation/release | `browser_extension/`, extension release scripts |

The extension uses a minimal two-color **T** monogram rather than a detailed
mascot, keeping it legible at toolbar size without borrowing a browser vendor's
mark. `browser_extension/icon.svg` is the vector source; the manifest declares
the rendered 16, 48 and 128 px PNGs.

The extension bridge accepts one current protocol. Every poll declares a stable device ID, protocol version, capability set, profile, and authenticated owner. Commands, results, streams, leases, and grants are all addressed by owner plus device; “most recently seen globally” is never an authority seam.

### Frame document authority

Problem: frame-targeted primitives checked only the parent URL, while `read_tab`
relabelled child content as the parent and could attach a different document's
snapshot or captured responses. A frame ID survives navigation and therefore
cannot identify the document that received permission.

Decision: extension 5.8.4 advertises `frame_authority`. Before an explicit frame
action, `BrowserPage._run` reads the active HTTP(S) frame URL and document ID,
checks the parent for read access and the child for the actual operation, and
passes `expectedDocumentId`. Declared adapter scope applies to both. The worker
validates that document against the frame and targets injections by document ID;
post-action state receives fresh read authorization. Missing/stale metadata fails
closed before dispatch, and older extensions require upgrade before frame use.
The ordinary top-frame path keeps its existing capability requirements.

Read receipts retain the actual document URL/title. Snapshot enrichment targets
that same document, and child reads do not inherit the parent's captured APIs.
Alternatives considered: authorizing only the parent or rechecking a frame ID
still allows navigation races; denying all cross-origin frames would remove
explicitly authorized capability. Chrome's existing [document targeting API](https://developer.chrome.com/docs/extensions/reference/api/scripting#type-InjectionTarget)
and [frame metadata API](https://developer.chrome.com/docs/extensions/reference/api/webNavigation#method-getFrame)
provide the required identity without new permissions, caches or tuning knobs.
Verify the frame authority/extension contracts and packaged Chromium journeys.

### Research origin continuity

Problem: research checked the initial and final tab origins but could collect a
foreign document during scrolling, then accept its contents after the tab came
back. An initial redirect also reached the collector before its stop check.
Decision: the shared `RESEARCH_CAPABILITIES` requires `research_origin_guard`
(extension 5.8.3). Both generic research and Site Pack projection use that owner;
older clients fail before dispatch and Settings names the missing capability.
Protocol version and extension permissions remain unchanged.

The worker checks the requested origin before collection, scroll and pagination,
and before the final receipt. Each serialized collector/action also checks its
actual page realm before accessing the DOM, closing the gap after a worker tab
lookup. A mismatch stops the operation and discards accumulated text, snapshots,
state and network evidence. The existing temporary-tab/capture cleanup still
runs. The `cross_origin_redirect` stop receipt is terminal at both server entry
points, including optional Site Pack groups; login-wall recovery remains intact.
Ordinary page snapshots without a research scope retain their existing behavior.

Alternatives rejected: final-URL checks miss a redirect that returns; worker-only
checks race the injected page realm; filtering only the final text leaves state
and captured evidence unaccounted for. Verify `test_browser_research_origins.py`,
the generic research, Site Pack and temporary-tab contracts, and both packaged
workers in Chromium.

### Search identity composition

Problem: the search bridge imported HTTP authentication to discover a browser
owner. Detached search workers could outlive that request, while reusable
providers silently inherited whichever request happened to surround them.
Decision: synchronous search, asynchronous search and browser-fetch routes pass
the authenticated owner into the existing `bind_search_browser` boundary.
Detached callbacks capture that owner before leaving the request. Task tool
handlers use the same boundary; providers capture its selected owner, device
and profile before fan-out. Browser and site-search providers without an
explicit binding are inert.
`lib/search/browser_scope.py` owns selection, context restoration and unclaimed
file-transfer cleanup; provider wiring in `search_bridge.py` re-exports the
existing entry points. It uses the shared exact owner parser before any device
lookup. An absent owner is inert and never queries the operator's global view.

Streaming search/fetch selects one immutable `SearchBrowserBinding` and activates
that value inside each worker, with an independent handoff registry. Problem:
repeating selection from a captured empty device ID converted an offline pinned
device into another device, while profile/fleet changes split one batch across
different routing decisions. Decision: capture the complete decision, including
absence, and never re-resolve it during fan-out. Activation restores its enclosing
scope even if file cleanup is interrupted. This routing value conveys no access
grant; browser commands still verify live ownership and access. There is no
second identity cache or device-selection policy. Keeping only the device ID
loses the negative result; copying the whole execution context would also carry
unrelated authority and handoff state. Regression tests cover single and batch
search/fetch, changing fleet/profile, invalid owners and concurrent file handoffs.
A scripted 64-page fetch preserves every returned byte and selected route while
reducing device-registry lookups from 65 to 1; this measures duplicate work, not
network throughput or model latency.

Alternatives rejected: a globally registered current-request callback would
retain the implicit dependency; binding every HTTP request would load the
optional search runtime for unrelated routes. Verify
`tests/search/test_search_request_identity.py`, `test_search_browser_scope.py`, browser provider contracts and
the existing authenticated file-transfer/task suites.

Problem: XHS's process-wide compatibility cache and backoff ran before native
site invocation. A prior browser's result could bypass the current owner's
adapter check, and compatibility cooldown could suppress an unrelated native
operation. Decision: native calls enter the already-bound site provider and
share only result projection with compatibility execution. Native outcomes never
read or update legacy pacing/cache state, whose owner is the search package's
`search/engines/_xhs_guard.py`. Explicit cookie-replay-first order is retained;
native fallback after an unavailable replay still owns its own outcome.
Alternatives rejected: adding owner IDs to the outer cache would still skip
live authorization and duplicate the native adapter's state. Verify native
provider terminal/isolation cases, concurrent public searches, the host's
browser-native runtime contracts and compatibility drift/replay-order tests.

Problem: compatibility state also outlived browser registrations and credential
changes. A query-only replay cache could return another session's rows, a prior
smaller result set, or rows mutated by its first caller. Decision: the package's
`_provider_scope.py` captures browser, site and auth-source views before binding
any callback. Nested calls and workers share that view; all exits restore it.
`search/engines/_xhs_sessions.py` owns weakly held registration/bound-view state.
Browser pacing/backoff follows that view, but browser content always reenters
the provider. Native providers retain their own account policy. A host needing
cross-operation pacing for different views must enforce it in its provider.

Cookie replay reads and copies the current enabled source before reuse. Its
retained guard follows the exact cookies and proxy, and cache keys also include
the requested result count. A changed, disabled or failed source read retires
that guard. Delayed lookups cannot replace a newer published session; in-flight
work holds only its borrowed state. Callback execution occurs outside ownership
locks. Guards disappear when the final active binding retires; the registry
keeps no credential history, raw-secret index or additional retention knob.
Alternatives rejected: a permanent identity-keyed cache retains retired owners;
browser caching skips live access checks; resetting a shared guard lets old work
settle into its replacement. Verify source rotations, delayed lookup/completion,
defensive result copies and binding/worker reclamation in the package session
and provider-lifetime tests, alongside native and host auth-source contracts.
Measurement: an installed-wheel offline probe makes 256 identical-credential
queries with one replay load. Across 256 credential rotations only the current
guard remains retained; clearing the registration leaves zero guards. An actual
public search binds once before its worker and retains no session after exit.
This verifies reuse and state lifetime, not live-site latency or account policy.

## Site repair ownership and verification

Problem: the search drift callback launched an unowned daemon thread. It lost the
captured browser, bypassed the originating task's model account and cancellation,
and wrote executable JavaScript into a process-wide JSON file. Speculative reads
could trigger those effects even when the corresponding tool was never admitted.

Decision: `site_doctor/ownership.py` captures the explicit task principal and exact
browser/profile. A bound browser provider carries that scope across search workers.
The listener records observations only. On successful completion, the admitted
search handler runs repair synchronously before its own tool settlement, borrowing
the existing ExecutionBudget admission and cancellation ports. Speculative search
has the same scoped rule reads but no repair port. Late callbacks, cancelled or
failed searches cannot launch repairs. Repair tools and outcomes use the existing
`tool_progress` stream; model requests use the originating task account. Identical
owner/tenant/device/profile/site attempts share the existing cooldown; independent
identities have no global single-flight admission limit. Expired cooldown metadata
is discarded on admission and contains no browser sessions, transcripts or rules.
Drift makes a speculative search result non-reusable, even when other engines
returned rows. The ordinary admitted handler performs the fresh search and repair;
cached results cannot silently bypass that authority boundary.

`catalog.py` owns briefs and schemas, `verification.py` owns live proof, and
`runner.py` composes the shared agent loop. Every new inspection or extractor attempt
revokes old proof before parsing. Every returned card must have a usable title and
absolute HTTP(S) URL. Only the exact verified selector/JavaScript and scroll count
can be published. Login walls and give-up terminate repair; a successful pin stops
through the loop's completion hook and suppresses remaining effects in that round.

Learned JavaScript is executable code. `browser/access.require_existing_write_access`
borrows grants without creating them; ordinary unattended browser actions retain
their existing automatic-grant policy. An existing browser write grant is required
before repair spends model tokens, before candidate execution/publication, and when
an engine selects a learned rule. The scrape provider checks source and final URLs
with that permission; read-only browser access never becomes script authorization.
Task cancellation interrupts command waits and lazy-load pauses, while owned tab
cleanup still runs without the cancelled event. Built-in extractors retain their
existing read policy. Native Site Pack adapters remain a separate rule authority;
the compatibility doctor must not publish a purported fix for their extraction.

Problem: host vertical search submitted an unbound thread, and native site search
captured the device without the task's cancellation port. The main branch could
also settle its repair scope while vertical work remained active. Decision:
`search_runtime.submit_search_worker` captures the explicit browser decision,
call configuration and borrowed knowledge scope in a fresh context, binds provider
registrations before submission, and gives the worker its own file-handoff lifetime.
The handler joins vertical work on success, failure and control exit; the former
five/ten-second result-discard waits are removed. Transport-owned deadlines and
the task's cancellation event remain authoritative. Both host browser providers
share one binding implementation; native adapters receive that task ID and abort
event through the existing adapter entry point. A cancelled adapter remains a
terminal failure and cannot authorize another transport.
Alternatives rejected: copying arbitrary request context retains HTTP/actor state;
rebinding a browser in the worker can select a different device; a new timeout or
detached worker would leave repair settlement racing its own observations. Verify
`test_search_vertical_scope.py`, including foreign-context exclusion, initially
unavailable devices, provider replacement, cancellation and exceptional cleanup.

`contracts/site_knowledge_v1.schema.json` defines owned records.
`site_knowledge/repository.py` uses the selected storage authority's existing record
CAS, keyed by owner, tenant, browser, profile, domain and operation. Publication uses
the revision captured before live verification. Conflicts require fresh verification;
lost acknowledgements succeed only after exact version/value readback, with no blind
retry. Retirement keeps a monotonic tombstone. One current entry per identity is
retained as durable user state; no history, truncation or retention tuning is added.
Tests measure a single winning revision under four competing publications and prove
that input mutation cannot change an acknowledged or recovered receipt.

Legacy `data/config/site_knowledge.json` has no ownership evidence. It remains
untouched and is never a runtime fallback. The global auth-source listing marks such
entries `migration_required`, rather than claiming an active pin. For an explicit
operator/user-directed adoption, `site_knowledge.adopt_legacy` takes the destination
repository and an already-authorized browser verification callable. It validates the
candidate, requires fresh successful extraction, and publishes with CAS; failed
verification leaves both stores unchanged. No current user is guessed as the owner.

Alternatives rejected: copying thread-local browser state does not issue model or
script authority; treating generated JavaScript as read-only data bypasses browser
consent; assigning legacy entries to the first caller crosses identities; a new
background-task/accounting loop duplicates existing lifecycle and settlement.

Verify the site-doctor, owned repository and repair-ownership suites, search
prefetch/provider contracts, browser scrape/access tests and shared agent-loop tests.

## JavaScript execution display

The JS card shares Run Command's inline description and independent code/output folds. The owner/device-scoped pre-execution page title is saved in result display metadata (`tabTitle`, at most 240 UTF-16 units), not executable arguments or model output. Problem: cached titles can predate navigation or an external title change. Decision: refresh with one existing `list_tabs` call on the selected route before JS, reusing a list already obtained during tab resolution. Failure remains advisory and never skips or retries the JS. This defeats cache-only labels and post-execution lookup (which can name a page changed by the script); no extension update or new command is needed. Running labels honor the configured device/working scope. Older rounds without title evidence show a neutral tab label and never consult today's browser state. Details: [`../RENDER_CONTRACT.md`](../RENDER_CONTRACT.md).

A successful auto/text `browser_read_page` read (accepted status `ok` or `partial`) stores the accepted page URL from the content receipt in result display metadata. The task handler copies it to the round's result-row `url`. The URL comes from the observed read, never from model arguments or page links. The answer's Sources footer cites that URL as an opened page ([decision](../RESOURCE_CONTROL.md#sources-footer-cites-only-pages-the-turn-opened)). Failed, empty, blocked, data, elements and app-state reads leave the row URL empty.

## Integration decision

OpenCLI's useful browser mechanisms are internalized here, not invoked through an OpenCLI subprocess or a second long-running Node browser daemon. Tofu adopts capture-before-navigation, CDP response-body reads, bounded network-idle settling and generic response ranking. Device selection, logged-in tabs, domain policy, leases, cleanup and audit remain owned by the existing Tofu bridge. This avoids a second browser/session authority and a second unbounded cache/process budget while keeping `tofu-search` behind its existing host-provider interface.

## Runtime flow

### Bridge owner across the API/execution split

Problem: the managed personal composition runs an `api` host (terminates extension polls) and a resident `execution` host (runs tools and context composition). The queue/registry in `lib/browser/queue/_state.py` is process-local, so the execution host always saw zero clients: the model was told no extension was connected while Settings showed it connected, and commands would have been queued where no extension polls.

Decision: the API host remains the single bridge owner. At background-service start it publishes a private loopback relay (`lib/browser/bridge_relay.py`, record `<project>/data/browser-bridge-relay.json`, mode 0600, per-incarnation bearer token, removed on stop); the execution host selects it, and the worker-facing verbs in `lib/browser/queue/_owner.py` (`get_connected_clients`, `is_extension_connected`, `client_owner_user_id`, `get_credit_hub_quota`, `wait_for_connected_clients`, `send_browser_command`) plus `protocol.client_protocol` forward to it. Role `all` keeps the in-process owner. One connection per call; caller EOF cancels the owner-side wait, withdrawing the queued command exactly like a local Stop. A missing or unreachable owner yields `browser_extension_status_unknown` evidence and a command error, never a replay.

Alternatives considered: moving the registry into the execution host would require the API to forward every poll and every panel read the other way; a durable shared registry would turn transient liveness into stored state.

Verification: `tests/browser/test_browser_bridge_relay.py` drives a real separate owner process (availability, owner scoping, protocol snapshot, command round trip), token refusal, missing owner, cancellation withdrawal and role selection. The desktop agent bridge (`lib/desktop/bridge.py`) has the same process-local registry and is not yet relayed.

### Agent connection failures

Problem: an extension may disconnect after tool discovery or environment composition. Plain-text connection errors were then settled as successful `browser_list_tabs` calls, including inside a child agent's `execute_tools` batch.

Decision: dispatch returns canonical typed failures for a missing authenticated owner (`browser_owner_required`), an unavailable pinned device (`browser_client_disconnected`), and an unavailable owner fleet (`browser_extension_disconnected`). Tab-list transport failures use `browser_list_tabs_failed`. The shared handler and gateway settle these as errors, retaining the reason and recovery action. Connection failures can be retried after recovery; they do not replay commands automatically. Each new call rechecks the authenticated owner's live registry, and a pinned device never falls back to another device. Parent and child agents use this same path.

A task tool call waits up to 15 seconds for a missing route **before enqueue**, using the authenticated owner and exact pinned device. Valid extension polls wake the shared registry condition; Stop is checked at most 100 ms apart. Root and child tools use the same handler and cancellation event. Reconnection continues the original call once; expiry returns the typed blocker and leaves no queued command for a late poll. An already-issued command is never replayed. Internal dispatch callers retain their explicit immediate-failure default. The wait borrows the existing tool thread, adds no worker or timer, and stores no durable state. Measurement: 20 expired waits add zero live threads.

Alternatives considered: interpreting every `Error`-prefixed page string as a failure would confuse page content with execution status; treating discovery as lasting connection evidence or selecting another owner's browser would violate live routing and ownership. Increasing the connection freshness window would conceal a disconnected transport without repairing it. A private child retry loop would drift from root execution and could duplicate page mutations.

Verification: `test_browser_connection_errors.py` covers isolation, refusal, reconnection and transport failure; `test_swarm_browser_tools.py` runs the real child invocation with direct and gateway browser calls through settlement, including reconnect-during-call and Stop. `test_browser_connection_recovery.py` checks root parity, owner/device fences and late-poll behavior.

### Extension worker lifecycle

Problem: shared poll globals allowed a retired request to dispatch commands or
change the next connection's status. Unacknowledged results and in-flight file
uploads could follow a newly selected server. Worker responsibilities also
shared one large file, while packaging only copied root scripts.

Decision: `background.js` synchronously composes three classic worker owners:
`BrowserPollTransport` owns each request, deadline and retry; `BrowserResultOutbox`
owns result receipts; `BrowserCommandExecutor` owns accepted-command settlement
and temporary resources. Stop/reconnect rolls back the active receipt before
aborting. Every asynchronous response checks request ownership before dispatch,
acknowledgement, retry or status updates. Cleanup finishes before publishing a
command result, including timeout and failure paths. All accepted commands still
start; these transport changes add no admission ceiling.

Receipts settle exactly once and preserve FIFO replay within an issuing
server/device/profile. Switching servers retains old results for return to that
scope, never sends them to the new server. Same-server credential renewal keeps
the existing server-side authenticated owner/claim checks. File-transfer start,
chunks, completion and cleanup use the connection captured when the command was
accepted. Device-download receipts retain that device identity as well.

Retention remains transient worker memory, not a durable queue: MV3 termination
can lose unacknowledged results. A transport frame still holds at most 32 results
and 12 MiB of serialized result bytes; HTTP 413 reduces the batch and eventually
uses an explicit oversize error. Empty inactive scopes are released. Pending and
leased results both defer the self-update reload. Measurement from the owner
probe: 256 distinct connection scopes deliver 8,192 synthetic results, peak 32
outstanding at a time, with one empty active scope afterward and zero after Stop.
Unacknowledged results are retained until settlement or worker termination;
connection switches do not silently evict them. No new persistent store is added.

Pairing checks exact HTTP(S) origin and a base-path boundary, both when selecting
a tab and in the serialized MAIN-world mint function after navigation. A late
mint cannot replace changed server/credential settings. Restart cleanup uses the
same worker predicate and leaves unrelated pages alone. The page function repeats
the small URL check intentionally: injected functions are independent serialized
copies, not closures over worker helpers.

Alternatives considered: relocating source strings would leave lifetime races
intact; a second transport/command loop would duplicate policy; converting the
manifest to ES modules adds an unnecessary packaging migration. Classic
`importScripts` is supported by [Chrome's worker model](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/basics),
and [injected functions cannot capture worker scope](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts).
All three worker files ship unchanged in dev/store packages and authenticated
downloads. Both manifests use version 5.8.2 so the existing self-update watchdog
can detect replaced files; protocol version and permissions are unchanged. Permission auditing covers every shipped script. Deterministic worker
probes cover retirement, replay, pairing, teardown and HTTP failures; the opt-in
Chromium package test verifies actual MV3 registration and popup messaging.

### Command lifecycle

1. Bridge middleware authenticates an owner-scoped device credential.
2. Poll validates device, protocol and capabilities, registers lifecycle-owned live metadata, and settles only results claimed by that owner/device.
3. A task selects one device in its owner fleet and leases it with explicit profile, task and tab policy.
4. Capability negotiation rejects unsupported commands before enqueue.
5. Load-waiting navigation starts bounded CDP Network capture before leaving `about:blank`, then waits for DOM and network stability. Capture startup is best-effort: when it cannot start (capacity, policy) the navigation still proceeds with a plain load wait, and a navigation that itself fails closes the inert tab rather than stranding an `about:blank`. The Tofu client tab (`isClient`, URL under the server origin) is never a navigation target or working-tab seed; targeting it opens a new background tab whose id the server re-binds as the working tab.
6. Poll atomically claims only commands addressed to that owner/device.
7. Server and extension re-check current tab/domain and grant before every page action.
8. DOM, hydration and API/WebSocket candidates enter the same target-aware collector. Final page and individual response URLs are authorized independently; only query/DOM/route-corroborated records survive projection and credential redaction under one context budget. Business-shaped JSON alone is not relevance evidence. A `research_hints` client may reserve 256 KiB of the unchanged 1 MiB body ceiling for up to five high-confidence passive endpoint templates; it never issues a hinted request, and unmatched current evidence retains the remaining budget.
9. A URL-addressed file that needs browser cookies is streamed from the response body to bounded server staging; it never enters Chrome's download manager.
10. Deep research may accumulate virtual-list/hydration data and recognized same-origin pagination in an owned background tab; cross-origin traversal stops. After authorization it may retain only the owner-scoped, query-free, non-executable structural projection defined by `contracts/browser_site_observation_v1.schema.json`; raw content and session material remain transient.
11. DevTools, network, screenshot and trusted input share one serialized per-tab CDP lease; the last holder detaches.
12. Settlement verifies claim ownership; timeout/cancel/release closes captures, transfers and ephemeral tabs. Search/research commands create owned temporary tabs on demand. Pending creates remain tied to the command; settlement closes exact IDs and late creates never navigate. The full `chrome.storage.session` ID ledger survives worker restarts and is recovered without a fixed slice; unrelated/user tabs are never inferred to be owned. Failed cleanup retains its record for a later recovery attempt. Captures likewise have no active-count quota. Their reaper still closes idle or old captures (5 min without activity or 15 min alive), while per-capture byte/record bounds remain. CDP attach/detach handoffs retain their five-second settlement checks and Chrome onDetach reconciliation. Leases have no process/owner count quota. One lifecycle sweeper expires timed leases; persistent leases remain owned until explicit release. Live devices and poll waiters likewise have no count quota. Same-device overlapping polls still supersede the old waiter without interrupting independent devices. HTTP request ownership uses exact releaseable leases without local credential/owner/global rate limits; authentication and the current frame's protocol remain mandatory. A previous protocol failure or client header cannot grant or deny a later frame.

### Working tab and focus

Problem: the working tab used to be seeded from the browser's active tab. That tab is where the owner is working — the Tofu chat, a page they are reading, a form they half-filled — so the first tab_id-less call navigated or clicked through it. Flagging the Tofu client tab fixed one instance of that defect; every other user tab was still fair game.

Decision: ownership, not activity, selects the target. The extension records every tab it opens for automation (`automation: true` in `list_tabs`, session-storage ledger so an MV3 worker restart does not forget) and the server seeds and remembers only those. When no automation tab exists, `browser_navigate` opens one in the background (`active: false`) instead of repurposing a user tab. A user tab is reachable by passing its id explicitly — a deliberate request rather than an inference, and that naming makes it the working tab for the calls that follow. Nothing in the flow activates a tab; the single exception remains the last-resort `captureVisibleTab` screenshot path, which needs the tab visible and restores the previous one.

Alternatives considered: keep seeding the active tab and rely on the `isClient` flag alone (protects the chat but not the owner's other tabs); open a new tab on every call (litters the tab strip and breaks multi-step flows that must stay on one page); mark automation tabs by URL (blank and freshly created tabs are indistinguishable, and the marker would drift).

Verification: `test_browser_v2_surface.py` pins automation-only seeding, the `list_tabs` marker and the background create on first navigation; the extension probe `automationOwnership` covers the ledger and `navigateGuard` covers background creation for the client tab.

`browser_screenshot` follows the same rule. It used to omit the tab id when the model did, its schema promised "the active tab", and the extension then captured the focused window's active tab — in practice the Tofu chat itself rather than the page the agent was working in, while the access check also authorized that active tab. Dispatch now resolves the working tab for screenshots before the access check, the handler sends that exact id, and with no owned tab it returns the same "pass tab_id" guidance as `browser_read_page` instead of guessing. The extension's tab-less fallback remains only for internal callers. Verified by the `test_screenshot_*` cases in `test_browser_v2_surface.py`.

### Interactive-element collection

`browser_read_page(mode="elements")`, text= click/fill resolution and `browser_fill_form` all share one collector, the extension's `_getInteractiveElements`. Three bounded passes run per call: (1) semantic interactives (`a/button/input/select/role=…/[onclick]/…`), (2) a cursor:pointer sweep for listener-at-root SPA cards, and (3) a custom-control sweep for component libraries (Meituan MTD, Element-UI, Ant Design) that hide the real `<input>` (`display:none`/0×0) behind a styled wrapper — the sweep surfaces the *visible wrapper* of hidden radio/checkbox inputs (role + label text + checked state + the hidden input as the action target), combobox/select triggers, and any currently-open dropdown options, so CDP trusted clicks and text= resolution can reach controls the first two passes structurally miss. Each pass is independently budgeted; the result carries `pointerSweep`/`customControlSweep` counters.

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

A write approval is durable only for its exact scope. It does not cross to a subdomain, redirect target, profile, browser, or owner. New grants require the target device to be connected for the authenticated owner. Audit projections never include cookies, credentials, page bodies, uploads, or raw script data. Browser URLs in diagnostics retain only their HTTP(S) origin; userinfo, path, query, fragment, and URLs embedded by exceptions are discarded fail-closed. File-completion audits retain domains, byte count and digest, never the response filename or URL.

## Adapter contract

Each `SiteAdapter` declares identity/version, domains, login URL, risk, and commands; every command declares access, executable JSON schemas, capabilities, timeout, optional pacing and one native handler. The same schema validates inputs and outputs. `browser_site()` discovers permitted sources; `browser_site(site=...)` exposes their schemas; adding `operation` and `params` invokes one declared read operation on the selected owner's device. Availability describes a transport, not task suitability.

Default `web_search` runs true Web engines only: it neither discovers native sources nor broadcasts to Xiaohongshu, ModelPlaza or Friday. Existing package clients may explicitly choose `Site:<id>` or `Xiaohongshu`; explicit empty/unknown/unavailable selections fail with diagnostics, never widen to general search. Native queries reject Web operators and freshness before navigation, rather than stripping their constraints. No extra routing model or automatic query rewrite exists: the current agent selects a capability from its purpose and input schema. ModelPlaza search accepts model names/aliases, not documentation questions; `model-info` accepts bounded model names for its dedicated attribute API. A syntax validator cannot determine semantic suitability by itself.

Xiaohongshu and ModelPlaza prefer the authenticated DOM over private-signing reimplementation. Adapters supply entry points, parameter knowledge and observed selector candidates; `content.py` alone decides whether those candidates satisfy the read. Friday uses this same deep-read pipeline without a hostname capture exception. `tofu-search` sees public provider interfaces, never host tabs, credentials, grants or device selection. Terminal native provider failures must not degrade to compatibility scraping or cookie replay.

Xiaohongshu search/detail declare a 12-second minimum interval and 300-second cooldown after a challenge or three empty reads. Admission happens inside adapter execution, covering tool and provider calls. `site_pacing.py` stores no queries/content: owner/device/site routes with a 15-minute lazy TTL and no route-count quota; live cooldowns are never evicted for another route. The standalone search package retains its existing query cache/backoff; the host guard is the execution safety boundary, not a second result cache.

### Target-driven content acceptance
`ReadTarget(kind, query, fields)` and `CONTENT_RECEIPT_SCHEMA` are owned by `content_contract.py`. `collect_content` returns `status`, `title`, `url`, `content`, `records`, `coverage` and `evidence`. Status is `ok`, `partial`, explicit `empty`, `blocked`, or `unavailable`; a blank/shell page is not an empty search result. Coverage names observed scope, missing fields, unread media, truncation and stop reason. A page open, nonempty API, or hydration-present flag never proves extraction success.

`browser_read_page` auto/text/data, browser text fetch and adapter search/detail use the same collector. Text mode excludes API/state enrichment; data mode selects API evidence and embedded data islands while retaining DOM only for corroboration. Unknown sites use generic snapshots and captured data, with an explicit unavailable diagnosis if insufficient (sparse page versus a target that selected none of the readable text). ModelPlaza's dedicated model-info operation uses bounded streamed JSON and the shared sanitizer; it is not generic page search. Its observed document/request/response receipt passes `read_pipeline.read_json_payload` and live `BrowserPage` source policy before admission ([contract and migration](../SITE_PACKS.md#specialized-json-source-receipts)).

The additive extension `contentSnapshot` excludes scripts/styles/navigation/hidden regions, traverses open shadow roots, scopes selectors without sibling escape, and accumulates virtual-list records during existing scrolling. Per snapshot: 5,000 nodes, 80 levels, 80 ms, 40 shadow roots and 40 records, sharing the text/record character budget; exhaustion is reported. No new protocol command, permission, process, or persistent page store was added. Closed shadow roots, image text, video and full comments are not inferred as read.

The collector has one 12,000-node/1-MiB work budget and at most 80,000 serialized output characters. It excludes unrelated account/chat/notification/history subtrees, preserves actual code, and does not append incidental APIs to a sufficient article. Endpoint inventory contains observed sanitized shapes, never fabricated records. Record URLs come only from observed links: absent detail URLs remain absent; secret-bearing signed URLs are withheld, not returned as broken redacted navigation links. Such records are partial and may require opening the real visible link in the browser; automatic signed search-to-detail continuation is not guaranteed.

Site observations are learned only after accepted content; missing requested fields, blocked/shell/empty reads cannot increment successful experience. Observation keys include read kind, and only accepted endpoint evidence can become passive hints. Existing bounded owner-scoped observation storage is reused; no raw content, executable instructions, cookie or query is persisted.

Problem: research diagnostics re-parsed captured bodies and used redacted receipt
URLs to select responses. Repeated URLs could therefore admit an unrelated
response, and capture totals counted only selected responses. Empty or blocked
receipts could also retain a source claim after their content was discarded.
Decision: `collect_content_and_network` passes the exact accepted, cleaned
responses from the common collector to inventory within one call. Research
analysis and rendering share its receipt; content-only rendering skips diagnostics.
Inventory rechecks live response policy and counts all original capture rows,
including denied and unrelated rows. Empty final content clears acceptance
evidence; research still reports authentication failure from the final receipt.
Authentication failure takes precedence over transport stop/truncation labels;
the capture and truncation facts remain separately available.
Bodies are not cached on receipts or retained in diagnostics.
Alternatives considered: a URL-keyed cache cannot distinguish repeated responses
or establish current ownership; re-parsing the original capture duplicates work
and separates diagnostics from acceptance. Public compatibility receipt arguments
do not authorize later analysis. Verify `test_browser_evidence_projection.py`
alongside content, network-evidence, research and site-observation contracts.

Problem: acceptance judged a page only by its text and captures. A proxy/WAF
error document (`403 Forbidden` / `openresty`) was accepted as `ok`; a sign-in
redirect returned its form and footer links as `partial` records, because the
`^`-anchored `_AUTH` text check fires only when the whole text starts with a
sign-in phrase; and `browser_read_page` called every `unavailable` receipt
"sparse (0 chars), likely Canvas/SPA", even when `target.query` had filtered out
thousands of readable characters.
Decision: `content.py` classifies two structural page signals before accepting
anything. An HTTP error document has a title that is exactly a 4xx/5xx status
line with its `http.HTTPStatus` reason phrase (case- and separator-insensitive),
and rendered text whose first line is that title: 401/403/407 are `blocked`,
other codes `unavailable`, both with stop reason `http_error_<code>` and no
content, records or evidence. An authentication gate is a page URL with an auth
host label or path segment (`login`, `signin`, `sign-in`, `sign_in`, `logon`,
`passport`, `sso`, `oauth`, `oauth2`, `cas`, `auth`, `authorize`; a segment's
file extension is ignored) plus a query value decoding to an absolute http(s)
return URL; it is `blocked` with `authentication_required`. Either half alone
(OAuth docs, a link-out `?redirect=`) stays content. For any other
`unavailable` receipt the read handler re-runs the same collector untargeted:
accepted page text means the target selected nothing, so it names the query or
kind, reports that text's real length and advises retrying without
`target.query` or with another keyword. Only when no page text is accepted does
it keep the Canvas/SPA diagnosis and structural summary; HTTP-error receipts
render as-is.
Alternatives considered: a `MULTILINE` `_AUTH` blocks every article whose header
has a "Sign in" link; a short-text threshold for error pages is a tuned knob that
still misfires on short articles; title-only or word-overlap phrase matching
misfires on titles such as "404 Not Found: a history of errors". Known gaps:
login modals that keep the URL, hash-routed (`#/login?...`), relative or
double-encoded return targets, nginx's non-standard phrases (`503 Service
Temporarily Unavailable`, `401 Authorization Required`) and error bodies that do
not repeat the title (Apache, CDN challenges) need a document-status or modal
signal from the extension. Verify `test_browser_content.py` and the read_page
specs in `test_browser_v2_surface.py`.

Problem: an untargeted API or hydrated-state record was corroborated when its
title occurred anywhere in the rendered text as a substring. Config/telemetry
objects carry generic `name` values (`action`, `default`, `dark`), so a live
video page's `mode="data"` returned `{"title": "action"}` from a monitoring
settings response, and embedded config islands would have flooded reads.
Decision: page corroboration is structural. The record's title or snippet must
equal one whole line of the cleaned rendered text (one text node in the
extension snapshot), case- and whitespace-insensitively, with the existing
three-character floor. Query matching, endpoint relevance and DOM-snapshot
records are unchanged. Requested `target.fields` that are not top-level scalars
are taken from the record's own nested objects, nearest first (JSON-LD
`offers.price`, `aggregateRating.ratingValue`); a nested object carrying its
own title is a different record (author, seller) and never lends fields.
Alternatives considered: a longer minimum title length is a tuned knob that
still admits long generic names; word-boundary matching still admits any
standalone word; dotted field paths would force the model to know each site's
nesting.

### Embedded data islands
Problem: modern pages ship their product, price, rating and post data inside
the HTML rather than the rendered text: `<script type="application/json">`
(sometimes URL-encoded, e.g. a `RENDER_DATA` island), JSON-LD, React Server
Components flight streams pushed through `self.<name>.push([n, "..."])`
(Next.js `__next_f`, ByteDance `__pace_f`) and inline assignments such as
`window.__NFES_DATA__ = {...}`. `browser_read_page` never read them, and
research read six fixed globals plus JSON-LD.

Decision: `lib/browser/data_islands.py` owns one fixed, server-authored,
read-only page script (`data_islands_script()`) sent through the existing
`execute_js` command, like the media inventory, so the deployed extension needs
no update. It collects three kinds and evaluates no page text (JSON.parse only):
`json` (every inline script with a JSON media type, raw text), `flight` (per
stream name, the string chunks of whole-script push calls in document order:
the page-world array when it accounts for every push in the markup, otherwise
the JSON-parseable push literals with gaps filled from an aligned live prefix,
because runtimes replace `push()` after hydration), and `global` (direct global
properties assigned at a statement boundary of an inline script, read as own
data properties so accessors on the global object never run, and projected as
JSON-like data without functions, DOM nodes, windows or binary buffers; see
the global projection below). JSON-LD leads; then document order. The bounds
are the collector's own: `HYDRATED_STATE_MAX_SOURCES` islands, `SOURCE_TEXT_MAX_CHARS` per JSON text
(a JSON island, a global, each flight row: a stream is a sequence of
independent rows, so cutting it at one source's bound would drop every later
row), `WORK_BUDGET_CHARS` in total and `CLEAN_MAX_DEPTH`; transferring more than
`collect_content` can process is waste, and the total keeps a result far below
the extension's 12 MiB result-outbox limit (lib/browser has no execute_js
result limit of its own). `parse_islands` re-applies them to the untrusted
result, rejects a result from another document than the read it enriches, and
decodes in Python: URL-encoded JSON only when the text starts with an encoded
`{` or `[`; flight rows `<hex id>:<json>`, `T<hex length>,<text>` rows that
consume that many UTF-8 bytes (React writes the byte length; the text may
contain newlines), letter-tagged rows (I, HL, D, E, ...) skipped, `$$`
unescaped, `$<id>` references to string rows resolved, `$D`/`$n` kept as text,
`$-0`/`$NaN`/`$Infinity` made JSON numbers or null, and `$undefined` plus every
unresolved reference dropped so no encoding becomes a title or snippet. The React 18.2 /
early Next.js 13 grammar puts the tag before the id (`J7:` model, `M2:`
module, `S8:` symbol, `E3:` error; React writes lowercase hex ids, so a leading
capital is a tag): J rows are models, `@<id>` lazy references resolve like
`$<id>`, and `$@` escapes a literal `@`. A text or string row
that is (URL-encoded) JSON becomes its own payload, and identical payloads are
kept once by content (Douyin's flight T row repeats `RENDER_DATA`). Malformed
islands are counted, never raised.

Global projection. Problem: a global is a live object graph, not JSON text. On
a Vue 3 search page `window.__INITIAL_STATE__` holds a `RefImpl` per store
field whose first own key, `dep`, leads into the runtime's subscriber graph
(Dep → Link → effects); a depth-first projection spent the whole per-source
bound there and never reached `_rawValue` or another slice, and its
ancestor-only cycle check re-projected shared subgraphs once per path.
Decision: the projection is data first across the whole island. It walks
depth-first in key order, like JSON serialization, through primitives, arrays
and plain objects (prototype `Object.prototype` or null; a Proxy, such as a
Vue reactive object, reports its target's), from any object including a
class-instance root. A class-instance child (reactive dependencies, Map/Set,
components, any other class) waits until the island's plain data is
projected, then fills its place tier by tier with the budget the data left,
so its key follows the holder's data keys and array elements keep their
positions. Each object is projected once per island, by identity: a later path
omits it, which also covers cycles. Only a cut of that plain data marks the
island truncated (the bound, or a value deeper than `CLEAN_MAX_DEPTH`, which
the collector would cut, so it is not transferred); class instances are
runtime state, projected best-effort. One framework grammar is unwrapped, like
the Flight sentinels: Vue 3 `isRef()` is an own `__v_isRef === true`, and
`ref()`/`shallowRef()` keep the raw value in own `_rawValue` (`_value` is its
reactive proxy, `dep` its subscribers), so such a ref projects as its
`_rawValue`, both read as data properties; computed and custom refs, whose
value sits behind a getter, stay runtime objects. Nested properties are read
with ordinary property access, which runs own accessors and Proxy traps as
JSON serialization does: Vue 2 turns observed state into accessors in place,
and MobX and Vue 3 reactive objects expose data only through them; the reader
still never writes, calls a page function or evaluates text. Only window-level
names are read as data properties, because an accessor there is not the value
an inline assignment stored. Alternatives considered: breadth-first over the
whole graph (a shallow skeleton everywhere, with record leaves cut first);
dropping class instances (loses data a page keeps in model classes or
Immutable collections); skipping framework field names such as `dep` or
`subs` (a version-specific list); deduplicating a ref only by identity
(`_value` and every object read through it are distinct reactive proxies, so
the data appears twice, behind runtime keys and one extra level per ref);
calling `ref.value` (runs page getters and returns the proxy); reading nested
properties as data properties only (hydrated Vue 2 state projects as empty
objects).

Islands enter `collect_content` as `hydrated_state` sources, the existing seam
for embedded state: page-URL evidence, query or whole-line DOM corroboration,
cleaning, private-subtree and secret-key removal, instruction filtering,
`observed_url`, requested fields and budgets apply exactly as for captured
data. `collect_content_and_network` was not used: its inventory keys captured
responses by method/host/path and scores status/content type, which islands do
not have. Mode `data` always reads islands; a client without `network_body`
no longer refuses the read but drops captured bodies and names the missing
capability. Mode `auto` consults islands once, only when DOM acceptance
accepted nothing, re-runs acceptance, and otherwise keeps the existing
query/kind/sparse diagnostics; rendered content is never replaced. Mode `text`
stays DOM-only. A coverage line names islands read, duplicates and every cut,
unparseable or omitted island. When no island supplied records, each island's
cleaned shape is rendered with `network_evidence.render_shape` (types only), so
raw island JSON never reaches the model.

Alternatives considered: collecting in the extension needs a release and an
extension reload on every device; a fixed global-name list misses
`__NFES_DATA__`-style names; evaluating inline scripts executes page code;
returning raw JSON floods context with config and bypasses relevance. Known
gaps: `browser_research_page` still uses the extension's fixed six-global and
JSON-LD collector; a JSON island or flight row longer than the per-source bound
is reported and dropped (a stream past the total bound keeps its complete rows);
globals are unreadable when a page CSP confines `execute_js` to the isolated
world; a cut inside class instances is not reported, so data a page keeps only
in class instances (other than Vue refs) is best-effort; runtime objects stay
under their holder, so the collector, which walks in tree order, can spend its
node budget on them before later data, and one island's runtime objects are
projected before later islands' data within the total bound; Map and Set
contents are not projected, as in JSON; binary flight chunks are not decoded and references to
model rows are dropped rather than inlined; Nuxt 3
`__NUXT_DATA__` is devalue-flattened JSON (containers hold indices into one
array) and is accepted only as that opaque array until a devalue `unflatten`
decoder with spec coverage is added. Verify
`tests/browser/test_browser_data_islands.py`, which also executes the page
script in an independent jsdom realm, with the content and read_page suites.
### Model output budgets
`browser_read_page`, `browser_research_page` and site workflows expose no `max_chars` presentation parameter. Accepted, policy-filtered results enter canonical capture before shared L0 sizes the model projection. Workflows do not pop completed results, clear steps or discard metrics to satisfy a caller text budget. Acquisition limits (pages, scrolls, captured bytes/nodes, leases, timeouts) and credential redaction stay authoritative: complete output means complete **accepted evidence**, not an unbounded or exhaustive website crawl.
## File acquisition and location contract
File location is a typed outcome, never an implication of “download”:

| Receipt location | Meaning | Writer |
|---|---|---|
| `device_downloads` | Chrome saved the file on the device running the extension | explicit legacy `download` / `wait_download` commands using `chrome.downloads` |
| `server_staging` | The Tofu server atomically committed a verified file under `data/fetched/` | server-direct fetch or negotiated `file_export` streaming |

`fetch_url` tries server HTTP first. If its authenticated browser text fallback discovers an extensionless attachment/non-text response, the extension streams that exact `fetch()` Response instead of issuing a second GET; a typed host handoff then stops the legacy text provider before cookie replay or anonymous HTTP can issue another request. Signed/one-time URLs therefore remain valid, including the post-login retry. Known binary suffixes skip tab navigation. Only a positively textual probe may open a rendered tab; missing metadata or probe failure stays on the byte path or fails explicitly. The server does not dispatch `fetch_url` to workers lacking `file_export`, because they predate this guard and could guess their way into a client download. `browser_download_url_to_server` is the explicit model contract when the requested outcome is a server file. Its browser namespace is intentional: the same contract can use a logged-in browser even though bounded server HTTP remains the first transport. It accepts an exact URL or resolves `text`/`selector` on one explicitly owned tab to the full link before acquisition, avoiding the 200-character interactive-element projection for signed URLs. Element resolution is a fixed, read-only DOM attribute projection; it never clicks, submits a form, interprets page JavaScript, or reads cookies. `text`/`selector` must name a real `<a href>` / `data-download-url` link or an `<img>`/`<video>`/`<audio>` element (see Page media inventory and acquisition) — action buttons (submit, menu items) have no URL to recover and the tool description points the model at `browser_click` for those; this is the conv-mubjnnu5k9jhfx lesson (128 `browser_download_target_ambiguous` failures on '新建容器' / '挂起' / etc.). When no explicit `tab_id` is given and the remembered working tab has died since the last call, resolution re-seeds from a live automation tab and retries once before surfacing `browser_download_target_ambiguous`, matching the self-heal `browser_click`/`browser_type` already had (`lib/browser/handlers/_interact.py:_retarget_if_tab_gone`); the access-policy grant is re-checked against the replacement tab. An explicit `tab_id` is a deliberate target and is never silently rerouted. The tool then uses one acquisition owner: bounded server HTTP first, followed by the selected owner/device's `file_export` transport when the direct response is unavailable or is an HTML/login page. Success always returns `server_staging`, absolute path, size, SHA-256 and transport; the model never chooses or reconstructs the authentication transport. The pre-migration name `download_url_to_server` is an execution/search alias only and never a second wire schema. A narrow pre-subprocess guard recognizes single-URL cookie-bearing `curl`/`wget` file-download commands and routes them through this same owner. Ambiguous shell expansion/pipelines are blocked rather than allowed to replay credentials. The guard does not rewrite ordinary API inspection (output to stdout or `/dev/null`), uploads, non-GET requests, cookie-free shell commands or a compound command's own cookie-jar session, and a redirected `-o` destination remains unwritten until a separate authorized filesystem action copies the staged file.

The shell interception owner is `lib/project_mod/download_intent.py`. **Problem:** whole-source keyword matching can combine a downloader example in a Python heredoc with Git's `-B2` and an unrelated `>` into a nonexistent cookie download. **Decision:** identify executable heads with the shared shell word/segment owners and bind cookie, output and method evidence to that invocation only. Quoted utility/program arguments, comments and proven literal Python/node/cat stdin are data; explicit shell `-c`/`eval` and live substitutions are separate executable sources. Static unquoted heredocs use expansion semantics even when their bodies contain quote bytes, and literal cat stdin directly piped into a shell is analyzed as executable shell source. Parsed file-output targets distinguish named destinations from descriptor aliases and `/dev/null`. Shell stdin classification separates redirects from argv and distinguishes command-string options from long options, option values and script operands. Command substitution boundaries ignore comment bytes, so a comment's `)` cannot hide later executable source; an embedded URL fragment is not a shell comment. Only the original standalone supported downloader can be redirected; matching wrapped downloads are refused before any subprocess or acquisition. One fact crosses invocations of a source: a cookie file that an earlier downloader wrote as its jar (curl `-c`, wget `--save-cookies`) while loading no other cookies is that command's own session, not browser material, because curl rewrites a `-c` jar from its in-memory store even when it holds no cookies; `curl -c jar … && curl -b jar -o out …` therefore runs. A writer that loads other cookies, or whose segment holds a substitution placeholder, withdraws the jars it may write. Source order states intent; control flow, concurrency and cwd changes between the two invocations are not modeled. The literal-stdin owner retains proven prefix evidence for intent analysis, while writer admission remains all-or-original and refuses physical-line continuations that alter header boundaries. **Alternatives rejected:** disabling interception loses credential safety; masking all quoted text hides executable shell payloads; a whole-command regex fallback recreates the false association; treating every cookie file as browser material blocks the anonymous sessions a command starts itself, while trusting any file a command names with `-c` would admit `curl -b exported.txt -c exported.txt`. This narrow recognizer is not a shell sandbox or authorization for arbitrary programs/unknown grammar. Verify `tests/project_mod/test_authenticated_download_boundaries.py`, `test_shell_data_write_admission.py`, `tests/tasks_pkg/test_authenticated_download_pipeline.py` and `tests/server/test_server_download_semantics.py`.

Explicit file intent has a distinct streaming budget. Its command deadline is likewise independent of the search-page `FETCH_TIMEOUT`: explicit downloads use the existing 120-second command ceiling, issuing a 110-second browser stream budget and reserving five seconds for failure cleanup plus five seconds for settlement. The direct HTTP attempt retains its existing bounded search transport. A low search timeout must not silently reduce explicit streaming to 30 seconds.

Problem: applying the default 20 MiB search-body limit to explicit downloads rejects ordinary installers. Decision: use the existing `MAX_TRANSFER_BYTES` (500 MiB) for explicit browser export and direct staging validation. The direct HTTP fast path retains its search-body memory cap; files beyond it fall through to the selected browser's 256 KiB-chunk stream. Opportunistic `fetch_url` handoffs still use `FETCH_MAX_BYTES`. Raising the global search cap or buffering a 500 MiB direct response was rejected because it expands text-fetch memory cost. The current extension's exact issued-limit refusal is normalized to non-retryable `browser_file_transfer_too_large`, with a stop action rather than a blind retry; arbitrary errors retain the command-failure classification. No extension upgrade or wire change is required.

Extension 5.8.1 transfer deadline failures retain `browser_file_transfer_timeout`, the current stage (upstream response, server start, upstream body, chunk upload, or server commit), and read/staged byte counts without source URLs or tokens. An already-aborted request is not retried. Older workers' exact unexplained abort response becomes `browser_file_transfer_aborted` with an explicitly unknown cause, never a fabricated timeout diagnosis; recovery asks for the served worker's diagnostics and connection checks instead of an unchanged blind retry. Every failed transfer aborts the source response, cancels the reader or unlocked body, and uses a fresh, separately bounded signal for server cleanup. Raising the global search timeout or merely replacing the abort text was rejected: neither fixes file-budget ownership or identifies a stalled stage.

The service-worker request uses `credentials: include` plus the extension's host permission, so Chrome applies its normal Cookie, SameSite and third-party-cookie policy. The bridge never extracts, copies or replays site cookies and cannot bypass a browser policy that withholds them. Model-facing `browser_get_cookies` exposes names, scopes and flags only; values are redacted. Internal verified login capture may inspect domain-scoped values inside the browser/auth-source owner, but values never enter tool results, task metadata or logs.

Before dispatch the server binds owner, device, source URL, profile, random transfer ID and one-time token. It accepts status/headers then ordered chunks, checks redirect policy at start and source/final policy again at commit, verifies declared length, hashes, fsyncs and atomically renames. The extension receives a path-free receipt; the exact originating task context—not a URL/global recency scan—claims the transfer ID and consumes the server-authored path. Unclaimed task handoffs are deleted when that binding closes.

Text-only search contexts omit the handoff entirely, so a classified attachment fails safely without streaming bytes into staging that no file owner can claim.

Staging is not arbitrary destination authority: copying into a project is a later filesystem action under that subsystem's policy. It is reconstructible and reclaimed oldest-first under a launch-probed budget or seven-day TTL, so models should materialize files the user wants to keep. Element resolution accepts anchor/area `href` plus bounded `data-download-url` / `data-href` / `data-url` attributes. Export currently represents only a complete HTTP(S) GET; 206/range, form POST, `blob:` and click-triggered downloads fail until their request/gesture provenance has an explicit contract.

Limits: every non-final chunk has a 16 KiB floor and every chunk has a 256 KiB ceiling; chunk count is derived from the byte ceiling; active transfers have no owner/process count quota; 16 KiB/control envelope; 180-second inactive registry TTL; 500 MiB hard per-file ceiling for explicit `browser_download_url_to_server`; opportunistic `fetch_url` file handoffs additionally obey the smaller live `FETCH_MAX_BYTES`; 2,048 staging artifacts. Server-direct and browser-authenticated receipts share this byte/inode budget, TTL sweep, live free-space reserve and ten-minute usefulness grace, so direct fallback cannot grow an unbounded parallel cache. Opportunistic response classification and streaming share one 30-second operation budget, leaving five seconds for command settlement; explicit export remains bounded by the 120-second command ceiling. No whole-file browser transport buffer exists. Every reservation and write preserves the shared live `TOFU_STORAGE_MIN_FREE_BYTES`; fresh receipts reject new work under pressure instead of being evicted. `TOFU_BROWSER_STAGING_MAX_MIB` is the retained compatibility name for the shared download-staging budget: 1% of launch-time free disk in personal mode, clamped to 64–2048 MiB (lean fallback 256 MiB) and hard-capped at 4096 MiB. `TOFU_BROWSER_STAGING_TTL_HOURS` defaults to 168 and is clamped to 1–720; its lazy sweep runs on the next staged download. Poll ownership is mandatory even in open mode: every request takes an exact credential-fingerprint lease before storage auth and binds one owner, with no rate, concurrency or waiter quota. `TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY` sizes only reconstructible recent-device metadata (64 on the personal fallback) and the fixed `TOFU_BROWSER_POLL_BODY_MAX_MIB` (64 MiB) bounds one request body. One poll returns at most 32 commands and accepts at most 64 results; device/result IDs, capability count/items and metadata strings are structurally bounded before registration or settlement. Extension 5.4.1 sends 32-result/12-MiB batches. Login-wall remediation reserves before its synchronous probe and hands the same slot to its at-most-ten-minute background poll; the poll budget derives a lean 4-process/2-owner fallback and 64/8 ceiling, while saturation fails the current fetch cleanly without opening a tab. Its 15-minute cooldown retains 32 lean/512 maximum LRU routes. The 20-second live-session cache retains 256 lean/8,192 maximum owner/device/domain results and clears with other reconstructible TTL caches under memory pressure. Working-tab affinity shares the device-registry route ceiling (64 lean, 2,048 distributed, 8,192 hard), expires after 30 minutes without a real tool action, and clears under pressure; display reads do not renew it, while a closed-tab receipt removes it immediately and the next miss safely reseeds from `list_tabs`.

`browser_research_page` captures before navigation, accumulates open-shadow/virtual-list text, recognizes hydration globals and JSON-LD, scrolls, follows only recognized same-origin next/load-more controls, and returns content plus source strategy. Pagination calls collapse by method/host/path; inferred shapes contain types, not values.

Capture is reconstructible, never durable: captures start on demand; each has 80 responses, 160 in-flight methods, 40 text WebSocket frames, 384 KiB per response/frame and 1 MiB total; only 12 completed tab snapshots remain in MV3 memory. Static/binary assets are excluded, telemetry is de-ranked, duplicates removed, secrets redacted, and every response URL re-authorized before at most 80,000 model characters.

Research runs in the serial browser lane for at most 65 seconds, 5 same-origin pages and 8 scrolls/page, with 80,000 DOM and 160,000 transient hydration characters; it always destroys its tab. Arguments only lower ceilings. Cookie replay, Playwright and direct HTTP are explicit separate transports; private-host SSRF exceptions apply only to server fetches.
### Page media inventory and acquisition
Problem: page reads only reported `coverage.media_unread: true`. To get an image the agent hand-wrote `browser_execute_js` (a write-classified arbitrary script) to scrape URLs and then fetched them: site-specific work per page, and signed CDN URLs passed through model context. A "rendered elements only" listing rule hides timed media: players keep the playing `<video>` `display:none` with a 0x0 box behind their own chrome (Douyin: https `<source>` children, `currentSrc` set; 23 images and no video listed, although staging the video by selector worked), and the UA stylesheet hides every `<audio>` without `controls`. Per-element output with full `nth-of-type` paths is also too large: a Ctrip hotel list rendered 75 items in about 25,600 characters, mostly `div:nth-of-type(1) > …` chains, with one 48x42 button icon listed 10 times.

Decision: `browser_read_page(mode="media")` runs one fixed, server-authored, read-only page script (`lib/browser/media.py::media_inventory_script`) through the existing `execute_js` command on the already-resolved, read-policy-checked working tab — the same transport as `download_target._link_reader_script`, so the deployed extension needs no update. The script is a pure DOM projection (no clicks, navigation, DOM writes, network or page-JavaScript interpretation). It walks `<img>`/`<video>`/`<audio>` in document order, including open shadow roots (`<picture>` resolves through its `<img>`). `MEDIA_ELEMENT_JS` defines once, for both page scripts, when an element is LISTED: an image while rendered (non-zero bounding rect, not `display:none`/`visibility:hidden`; no size cutoff), or a video/audio that is rendered or has a displayed resource (`currentSrc`, reflected `src`, first `<source src>`, or `srcObject`). A hidden timed medium is reported with width/height 0 and `hidden: true`; a pre-created player without any source is not listed. Order: video/audio first (rendered area descending, so hidden ones follow rendered ones, then document order), then images by rendered area, ties by document order. Items with the same kind and the same raw displayed-resource string collapse in the page, before the item bound, into the first after ordering with an occurrence `count`; `stream` and `none` items have no resource string and never collapse. The result reports `total` (all listed elements) and `distinct` (entries after collapsing). Each entry carries kind; a CSS selector computed and verified in the page (bottom-up, `tag` steps take `:nth-of-type(k)` only when a sibling shares the tag, and the walk stops at the shortest suffix that `querySelectorAll` finds unique in the document or at a uniquely identified `#id` ancestor via `CSS.escape`, else `:root`; the final `length === 1` plus identity check stays, and the selector is empty when not addressable, e.g. inside a shadow root); rendered and intrinsic size; finite duration; a label from alt/title/aria-label/enclosing figcaption; in-viewport; and the source class `http`, `blob`, `data`, `stream` (srcObject) or `none` of the displayed resource. data:, blob: and stream handles never leave the page; the raw string only compares resources inside it.

Server acceptance reuses existing owners. The reported page URL must pass `_result_url_allowed`. A resource or poster URL is shown only when `content.observed_url` admits it; otherwise the item says `withheld_signed` (an HTTP URL that is credential/signature-bearing or read-denied) or `not_http`. `observed_url` treats vendor-prefixed signature keys (`X-Amz-Signature`, `x-signature`, `q-signature`, `upsig`, a bare `sign`) like `sig`/`signature`. Labels and the title pass `content.clean_text`; a selector is kept only when it is single-line and unchanged by that cleaner. Only video/audio may be `hidden`, which forces size 0 and not in-viewport. `MEDIA_INVENTORY_MAX_ITEMS` is the receipt's record `maxItems` (an entry is one record), so the bound covers distinct resources, and truncation reports shown versus distinct. Rendering is one line per entry, with `hidden` in place of a size and `x<N>` for a collapsed count. An entry whose URL is shown prints the URL and no selector, because the URL stages it. An entry whose URL is withheld prints its selector, and an unfetchable one prints only its reason. The footer routes `browser_download_url_to_server(url=…)` for shown URLs and `(selector=…, tab_id=…)` for withheld ones, and `browser_screenshot` for unfetchable ones.

Acquisition stays one contract: `browser_download_url_to_server(url=…)` or `(selector=…)`, after which `inspect_image` (image) or `inspect_video` (video or audio) reads the staged path; fetch_url names the same reader for a staged `video/*` or `audio/*` file. The reader script shares the in-page definitions (`MEDIA_ELEMENT_JS`). An explicit `selector` must match exactly one element when the download runs: none is `element_not_found`, and several give `browser_download_target_ambiguous` with the count, so a page that gained a second match since the inventory (a feed inserting the next player) is refused instead of staging a different element. A `text` target resolves through element discovery, whose concise selectors are not unique by construction and name their first match, so that path keeps first-match resolution; requiring uniqueness there would refuse ordinary file tables whose links all share one concise selector. A media root (img, video, audio, source, picture → img) yields its displayed resource. Other roots keep link semantics: the root is, is inside, or contains `a[href]`/`area[href]`, then `data-download-url`/`data-href`/`data-url`. A container with neither uses its single LISTED media descendant, so a container whose only listed medium is a hidden player resolves to it; several give `browser_download_target_ambiguous` with the listed count. A button is never a media container, because its icon is not the file. blob:, data:, stream and source-less media fail with non-retryable `browser_download_media_not_addressable`, which points at `browser_screenshot`. The full signed URL is resolved and fetched server-side, and the staging receipt never echoes it.

Alternatives considered: returning every media URL to the model is simplest but leaks signed/credential CDN URLs into transcripts and invites curl/cookie replay. A new extension command would require every user to reload the extension and a protocol/capability bump for a projection that `execute_js` already carries. A minimum-size filter for "real" images, or a size threshold to drop icons from the output, is a tuned knob that hides small but relevant media; area ordering, collapsing identical resources and the receipt bound shrink the output without a cutoff. Listing every hidden element would add hidden images, sprites and source-less pre-created players, none of which shows or stages a resource; only a timed medium plays while hidden, so only video/audio with a displayed resource are listed hidden. Collapsing on the server after the bound would let repeated icons use up the receipt bound, and the server cannot compare blob/data resources it never receives. Keeping a selector on every item duplicates the shown URL, which already stages the item; the selector stays the only handle for a withheld URL. Fixed-depth or anchored-only selectors either fail uniqueness or keep long chains, while the shortest verified unique suffix needs no depth knob. CSS background images, `<canvas>`, SVG `<image>`, iframe documents and closed shadow roots each need a different, non-element acquisition path; the empty inventory names them and `browser_screenshot` remains the visual fallback. Known gaps: lazy placeholders report the placeholder (often `data`) until scrolled into view; a selector whose element was replaced by exactly one new match still resolves to the replacement, and a `text` target can resolve to an earlier element sharing its concise selector (shown URLs avoid both by staging by url); preloaded hidden players with sources are listed after the rendered media; `observed_url` still admits signature schemes without a signature-named key (e.g. Alibaba CDN `auth_key`). Verify `tests/browser/test_browser_media.py`, which also executes both page scripts in an independent jsdom realm.
## DevTools Bridge
`browser_devtools` is one model tool over `chrome.debugger`, not a DevTools page and not a Developer-mode toggle. Developer mode is used only to install the unpacked build; runtime attaches CDP to an ordinary HTTP(S) tab. It needs no OpenCLI/Node daemon, Playwright, or `tofu-search` install.

Actions: bounded console/error reads and clearing, execution-context discovery; Promise-aware expression evaluation and recursive object inspection; temporary debugger start/state/stop, URL breakpoints, pause/resume/step; paused-call-frame evaluation and bounded script-source retrieval.

Inspection uses own `Runtime.getProperties`, never invokes getters, and releases object groups. Only same-origin iframe/worker targets survive auto-attach. Source/context/frame/console URLs are policy-filtered, secrets redacted, and HTTP(S) breakpoint source URLs separately authorized.

Limits: 2 debug tabs, 4 observers; per tab 200 console entries/256 KiB, 80 contexts, 120 scripts and 24 breakpoints; per object 400 nodes/60 KiB. Sessions last 10–120 seconds (default 60), pauses auto-resume at 30 seconds, output is 60,000 characters, and recent console evidence covers 12 MV3-memory tabs. Close, timeout, stop, external detach or worker reclaim discards all raw debug state.

The tool appears only with an owner-scoped current extension and is selected for requests such as “check this console error” or “debug this click.” `debug_start` precedes debugger actions; `debug_stop` ends them. Console/source/lifecycle reads need no write grant; expressions, breakpoint creation, pause/resume/step and frame evaluation do, with exact risk shown in approval.

Chrome permits one debugger owner per tab. Open DevTools, another debugger, or external detach yields an explicit error; the bridge never seizes the DevTools UI. `chrome://`, extension, file, view-source and DevTools pages are refused.
## User experience
Settings shows device/version/protocol/capabilities, adapter health, denials and grants. Missing capability names the device and links to Local Control upgrade; a handshake-rejected device stays outside the registry while a bounded owner-scoped recovery note offers the current build. The first 426 carries `Retry-After`, then a bounded credential-digest cooldown rejects before storage auth; a current-protocol header clears only that stale cooldown after an in-place upgrade while the authenticated frame remains authoritative. Version 5.4.1 preserves results across 429 or transport failure, obeys server delay, and bisects 413 batches before failing only an oversized singleton. A same-device retry seamlessly supersedes its older waiter and both retain normal 200 semantics. Repeated 4xx access rows keep first/power-of-two/heartbeat checkpoints plus exact metrics instead of durable-log spam.

Negotiated DevTools shows “ready”; invocations retain their timeline badge, approval dialog and Chrome debugging indicator. Adapter failures show unavailable, failed policy writes stay failed, and no unexplained upgrade badge is valid. Search exposes Fast/Balanced/Deep; backend presets own limits and custom overrides stay explicit. `browser_fill_form` compiles the whole field list (text, checkbox, native/react-select/antd select, button, file, multi_search) into ONE page-side script per call — a single `execute_js` round trip (extension override 100 s, under the command ceiling) instead of one command per field. Every field is read back and verified in-page, and submit fires only when all fields verify, so a half-filled form is never posted; failures name the exact field plus observed options, so repairs cost one targeted retry (clear-first re-fills are idempotent), and the filled form's HTML is kept as a bounded per-owner snapshot under `data/browser_form_snapshots/` (newest 10, ≤1 MB each, never logged) for offline inspection instead of re-reading the live page. `type='file'` fields attach a local file to an `input[type=file]`: the server resolves the path through the workspace seam (absolute paths allowed), enforces a 20 MiB ceiling with an explicit error, tags label-addressed inputs with `data-tofu-ref`, streams bytes via `page_upload`, and the fill script verifies the attachment by filename — the submit gate covers uploads exactly like typed fields. `type='multi_search'` drives search-then-pick widgets (OpenReview Authors, assignee/tag pickers) without site selectors: each comma-separated query is searched, matched exactly (href parameter beats name text, so IDs win over duplicate names), added via the row's plus/sole-button control, and confirmed by the selection materializing; already-selected items are skipped, and per-item stage errors (resolve/search-input/search/result-match/add-button/confirm) name the first failing stage so repairs stay local instead of re-guessing the whole page.

Extension 5.7.0 turns the custom-control sweep (5.6.0, discovery) into *actionable* controls, adopting the 1024-browser semantic-action contract. `browser_click` now forwards a wrapper's hidden input via its collected `targetSelector`: the trusted CDP path measures the visible proxy for the coordinate while the synthetic path calls `.click()` on the real control, so an MTD/Element-UI/Ant-Design radio or checkbox actually toggles instead of a blind wrapper click. Two intent tools close the two failures the generic form filler could not ("option menu did not open"): `browser_check` sets a radio/checkbox/switch to an exact state idempotently (resolves the underlying checkable from a wrapper, a hidden input, an ARIA control, or a `<label for>`; applies via the visible proxy then verifies, falling back to setting the control + dispatching the framework event sequence), and `browser_select_option` performs the two-step custom dropdown (open the trigger, poll for the revealed option list, click the match; native `<select>` uses a fast path), both component-library-generic via ARIA roles + wrapper-class families rather than MTD-only selectors. Every interaction failure is classified with a stable `TARGET_*` actionability code (`TARGET_NOT_FOUND/DETACHED/NOT_VISIBLE/DISABLED/OBSCURED/NOT_EDITABLE/NOT_CHECKABLE/NOT_SELECTABLE`) surfaced verbatim in the tool result — naming the obscuring element or the available options — so the model picks a real next step instead of a blind retry. Both new tools are write-classified (serial + approval), carry approval enrichers that name the control/option, and are registered across the v2 surface, tool inventory, swarm hint, badge dispatch and tab-label set.
## Tool-Search discoverability
The browser family is `discovery_policy='searchable'`: its schemas surface to the model through Tool Search, ranked by field-weighted BM25 over `name` (×3) + `description` (×2) + private `search_hints` (×3) + property names (×1). **Problem (conv-mubjnnu5k9jhfx-adjacent):** the family had no `search_hints`, so discoverability rode entirely on terse wire descriptions, and the ranker mis-served core intents — "click a button on the page" → `browser_get_cookies`, "read what is on the page" → `browser_list_tabs`, "go to a url" → `browser_list_tabs`, "upload a file to a form" → `browser_get_cookies`. **Decision:**

- `lib/tools/registry/_build.py` — added ranking-only `search_hints` to the browser `ToolSpec` (never serialized into the wire schema, so zero prompt cost; verified via the `_spec.py` projection). Each action verb is pinned to its tool; `browser_fill_form` deliberately yields click/button terms to `browser_click` so its long field-type description cannot outrank the primitive.
- `lib/tools/discovery_vocabulary.py` — added a `browser_action` capability concept (click/type/fill/navigate/etc.). Every browser tool name carries the `browser_` prefix, so the concept resolves to a name-weighted token (`@browser_action` ∈ `_NAME_WEIGHTED_CONCEPTS`) that lifts the whole family above generic list/read tools for interaction phrasing.

**Alternatives considered:** renaming tools to bare verbs (`click`, `type`, `scroll` à la browser-use / Playwright MCP) — rejected: 18 renames break prompt-cache prefixes, dispatch, display, and the eval corpus for marginal gain, since the BM25 ranking layer (not the names) was the actual defect. Expanding wire descriptions — rejected: wire bytes cost prompt every request whereas `search_hints` cost nothing.

**Verified:** `tests/browser/test_browser_v2_surface.py::test_core_action_intents_rank_their_primitive_first` pins 15 core intents → their primitive via the real ranker. The market-research briefs (foundation models: Anthropic/OpenAI/Gemini computer-use; OSS: browser-use, Playwright MCP, Stagehand, Skyvern, Chrome DevTools MCP) confirmed our intent-first text/selector targeting + post-action receipts already match the dominant design; the gaps were discoverability and a missing `browser_scroll` primitive.

**`browser_scroll` (added):** the scroll primitive every market toolset ships. The extension already had the `scroll_page` command (used internally by the scrape provider); this only exposes it as a model-facing tool. Down/up by an amount (default ~75% viewport), to top/bottom, or a CSS selector into view; the handler reports the new `scrollY`/`scrollPercent` plus the top/bottom boundary so the model knows when to stop. **Read-only** — only the viewport changes, never page state — so it joins `provides`/`search_hints`/`_WORKING_TAB_TOOLS` but NOT `write_tools` (no approval, stays parallel-safe), matching every market toolset where scroll is unrestricted.

**Registry specs have one source.** `lib/tools/registry/_build.py::build_builtin_specs` seeds the live registry; `_builtin_specs.py` only re-exports it, so no test-only mirror can drift. (A former mirror once carried browser `search_hints` the live registry lacked, which is why the discoverability test reads the live `all_specs()`.)

### Always on, deferred by default

Problem: the Local Control browser switch set `browserEnabled`, but a
connected extension already put the family in the executable catalog. The
switch only changed exposure: on, it pinned all 19 schemas (about 3.6k schema
tokens) onto every native Tool Search request; off, it hid them from native
search entirely. Local Tool Search deferred them either way.
Decision: the browser has no switch. The composer and the active-conversation
resolver treat `browserEnabled` as always on, like fetch, and the family no
longer sets `pin_on_exposure`. It stays `searchable`: Tool Search lists it
behind `search_tools`, and only an explicit Tool Search `off` sends it eagerly. A connected extension remains the
sole availability gate. Callers that pass `browserEnabled=False` (evolution,
paper, project dispatch, explicit headless runs) keep it off their wire. The
Local Control badge, the Extras count and the per-turn chips no longer count
the browser. Timers inherit the flag, and their hand-built poll loop includes
the browser tools while an extension is connected. The desktop switch, its pin
and its one-way gate are unchanged.
Alternatives rejected: forcing the flag on but keeping the pin adds the eager
schemas to every native Tool Search request; deleting the flag would also
expose the browser to runs that explicitly opt out and break the headless
`browser` option.
Verify `tests/tools/test_browser_capability_discovery.py`,
`tests/conversation/test_conv_config.py` and
`tests/server/test_frontend_local_control_merge.py`.
## Failure semantics
- Missing device/protocol/capabilities: reject the poll with HTTP 426 + `Retry-After`; do not register an anonymous or downgraded client. Retain only bounded owner-scoped recovery/cooldown notes needed to direct an installed device to upgrade.
- Wrong owner/device result: ignore/reject it without settling another task.
- A response-URL policy lookup failure withholds captured bodies and emits one rate-limited diagnostic checkpoint; it never degrades into allow.
- Unsupported adapter command: fail before enqueue with upgrade guidance.
- Redirect or tab race: deny the action at the last possible boundary.
- CDP unavailable or already attached: continue with DOM text and URL-only network metadata; never label missing bodies as successfully extracted data.
- DevTools session absent/expired or externally detached: return an explicit error; resume a paused target when possible and discard all transient state.
- No useful captured body: `mode="data"` says to navigate/reload with the current extension; auto mode keeps the prior sparse-page diagnosis.
- Missing `network_body` capability: `mode="data"` returns embedded data islands and names the missing capability instead of refusing; a failed island reader only adds an "unavailable" note.
- Deep traversal stalls, has no safe next control, or reaches a cross-origin redirect: return the collected prefix with an explicit stop reason; never guess a URL or click an unrelated control.
- Extension disconnect/timeout: expire the command and release owned resources.
- Policy denial: return non-retryable V2 `browser_access_denied` and settle as error. Arbitrary `browser_execute_js` is always write-classified even when its expression looks read-only. In this unattended deployment a missing write grant no longer raises `browser_write_authorization_required`: `require_access` auto-creates the durable exact-domain grant on first use (only when a concrete browser client is identified; read-denied domains still fail closed). An origin-relative `fetch('/…')` without `tab_id` fails earlier with retryable `browser_explicit_tab_required`, so the server never guesses a page origin or requests a grant for an unrelated remembered tab. The temporary research tab never silently becomes the working tab.
- Wrong transfer owner/device/token, an out-of-order/conflicting chunk, or a newly denied redirect: reject without committing; delete partial bytes.
- Oversize response or exhausted staging budget: fail explicitly before dispatch when knowable, otherwise abort the stream; never fall back to a client-device download.
- Completed but unsettled transfer: retain only until the 180-second registry TTL, then delete it. Consumed server staging follows the bounded staging retention policy.
- Invalid adapter output: typed failure; never feed malformed data into search.
## Invariants
- Current protocol only; no anonymous client, protocol downgrade, unbounded poll/body/waiter/client state, or client-controlled bypass of server admission.
- Every command has one owner, one target device, and one claiming device.
- Result settlement matches command ID, owner, and claiming device atomically.
- Device discovery is owner-scoped and never based on a process-global poll.
- Mutation authority is exact-domain and refreshed immediately before action.
- Network bodies are transient, bounded, redacted, and independently checked against the owner's read policy before entering model context.
- Deep research is one bounded background-tab lifecycle; it never persists a crawl frontier, captured body, hydration state, credential, or session global. Its advisory observation expires, is LRU-bounded to 200 rows per owner, and never authorizes access or replays an endpoint.
- Adapter schemas are the single machine-readable command contract.
- One broker owns each tab's CDP attachment; subsystems hold bounded leases and never attach/detach behind one another.
- Debugger state is time-bounded and same-origin; inspection never invokes a getter, and a paused page has a 30-second resume failsafe.
- Server fetch policy and browser consent never grant one another authority.
- `downloads` always means `device_downloads`; `file_export` always means `server_staging`. Neither location may be inferred from a natural-language verb or an untyped download ID.
- `browser_download_url_to_server` is the canonical server-location intent; its schema requires a bounded URL/text/selector target, and cookie inspection or shell HTTP are never alternate authentication transports. `browser_preview_page` requires exactly one project HTML path or HTTP(S) URL and exposes the executor's bounded viewport/wait envelope.
- Browser file export is GET-only, bounded, sequential, digest-verified and atomically committed; server path authority never crosses the bridge.
## Change routing and tests
Use the ownership table above to locate the source. Policy/protocol changes require owner/device, redirect and upgrade tests; transport changes require byte/concurrency/cleanup tests; content changes require target, redaction, coverage and observation tests. `test_browser_temporary_tabs.py` covers stalled capture/extraction, timeout, late create, concurrency and restart cleanup.

```bash
pytest -q tests/browser/test_browser_async_poll.py tests/browser/test_browser_v2_surface.py
pytest -q tests/browser/test_browser_access_api.py tests/browser/test_browser_native_runtime.py tests/agent_core/test_agent_terminal_failure_breaker.py
pytest -q tests/browser/test_browser_file_transfer.py tests/browser/test_bridge_auth.py tests/browser/test_browser_media.py
pytest -q tests/browser/test_browser_network_evidence.py tests/browser/test_browser_read_payload.py
pytest -q tests/browser/test_browser_research.py tests/browser/test_browser_content.py tests/browser/test_browser_content_snapshot.py tests/browser/test_browser_site_tool.py tests/browser/test_browser_read_unification.py tests/browser/test_browser_data_islands.py
pytest -q tests/browser/test_browser_devtools.py

TOFU_REQUIRE_FRONTEND=1 pytest -q tests/browser/test_browser_js_display_metadata.py tests/tools/test_tool_browser_execution_presentation.py
pytest -q -m visual tests/visual/test_visual_browser_js_card.py
pytest -q tests/browser/test_browser_adapter_upgrade_ui.py tests/auth/test_auth_sources_xhs.py
```

Site workflows admit independent owner/device requests on demand. Foreach expands all source results unless the caller explicitly supplies a result limit; it has no fixed step/item quota or two-worker waves. Independent reads use owned pages and demand-driven workers by default. Explicit caller concurrency/serial choices and declared site pacing remain. Cancellation/deadline failure fences remaining work, joins started workers and closes every owned page while preserving completed result receipts. Login capture deduplicates each owner/device/domain during its probe and worker lifetime, with no global or owner count quota. Cooldowns expire by time rather than evicting still-live routes when another request arrives.

Extension 5.7.1 removes count admission for temporary pages, captures, debugger sessions, console observers and breakpoints. Debugger scope, paused-state failsafes, per-tab CDP ownership and output clipping remain. Breakpoint hit receipts retain every registered matching ID. Transfers accept independent owner/device work without reserving every advertised maximum as disk growth. Actual writes retain live-volume free-space checks, byte/order/hash validation and atomic publication. Staging budgets govern eligible old-cache reclamation; protected/fresh artifacts cannot refuse new work. Completed transport metadata expiry leaves the artifact under its cache TTL and handoff grace; aborted and uncompleted partial files are still reclaimed.

Concurrent debugger starts on the same tab join one initialization and return redundant CDP leases. Setup failure and cancellation fence late publication and release the exact old generation without removing a newer session's state.
## Serialized page interaction ownership
Problem: Chrome serializes an injected function without its service-worker closure. Helpers and `TARGET` constants therefore disappeared in the page, although tests executing the entire worker in one realm passed. Decision: `_pageInteraction` encloses the shared locator, click, checked-state and select implementations with all their dependencies, and selects a declared operation from explicit arguments. Existing permissions, CDP leases and protected-page checks remain with each command. Alternatives rejected: duplicating helpers in every command drifts; injecting persistent page globals risks collisions and retained state; evaluating source strings adds an unnecessary code boundary. The behavioral harness serializes the actual supplied function into an independent page realm and verifies successful and failed commands.

On Linux the common `chromium_env.ensure_chromium_env` can select a provisioned `HOME/.local/bin/node` for the Playwright driver when no explicit `PLAYWRIGHT_NODEJS_PATH` exists. Problem: either the bundled or locally installed Node can require a newer GLIBC than the host; exporting an unusable automatic selection also overrides a frontend build's working Node. Decision: accept the local candidate only after `--version` succeeds in the prepared child environment, with no inherited stdin, discarded output and a two-second timeout. Failure leaves driver selection unset, so the frontend build supplies its already-running Node. An existing explicit selection is preserved without probing; a successful automatic selection is reused on subsequent calls. Alternatives rejected: executable permission alone does not prove loader compatibility; overriding operator configuration or bypassing the browser canary hides failures. No installer, worker or persistent cache is added by this environment adapter.

Native typing selects the value setter for the target's actual HTML control class. Calling the input setter on a textarea throws `Illegal invocation`; the independent page-realm probe verifies both input and textarea mutation.
