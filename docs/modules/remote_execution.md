# Desktop devices and remote execution

This domain connects an owner-authorized desktop device to the server for
desktop tools, remote project work, subscription egress, and local subscription
adapters, plus desktop/agent packaging and pairing. The bridge is a capability
boundary, not a trusted-network shortcut.

## Ownership

| Concern | Owner |
|---|---|
| Wire admission/lifetime policy | `lib/desktop_wire/`, `contracts/desktop_bridge_v3.schema.json` |
| Bridge credential resolution | `lib/bridge_auth.py`, `routes/_bridge_caller.py` |
| Poll HTTP adapter | `routes/desktop.py` |
| Device registry, command queue, streams | `lib/desktop/bridge.py` |
| Device status/pairing/distribution API | `routes/api_v1/desktop.py` |
| Pair-code authority | `lib/desktop/pairing.py` |
| Agent runtime | `lib/desktop_agent/` |
| Agent permission policy | `lib/desktop_agent/_permissions.py` |
| Remote project binding | `lib/desktop/remote.py`, `lib/conv_config/` |
| Remote project execution | `lib/tasks_pkg/handlers/project.py`, agent `_project.py`; [paper builds](../PAPER_BUILDS.md#desktop-execution) reuse the bridge and compiler via agent `_paper.py` |
| Subscription egress routing | `lib/desktop/egress.py`, `egress_preferences.py`, Sidecar `desktop` domain, agent `_egress.py` |
| Local subscription adapter / borrowed Codex login | `lib/desktop/adapter.py`, agent `_adapter.py`; [Codex login owner](../IDENTITY.md#borrowing-an-existing-codex-login) |
| Full-app/agent launchers | `desktop/launcher.py`, `desktop/agent_launcher.py` |
| Build and artifact store | `lib/desktop_dist/`, `tofu-agent.spec`, release scripts |
| Settings UI | `frontend/src/features/settings/devices.ts` |

## Trust and identity

Remote devices authenticate with an owner-scoped credential carrying `agents:bridge`. The packaged full desktop app may
use the non-persisted process capability from `lib/bridge_auth.py`; it is accepted only by the desktop poll boundary. IP
address, loopback appearance, and a global shared secret grant no authority. Every poll must include a stable `agent_id`
frame; anonymous/ version-one pollers are rejected. Registration records owner, credential ID, platform, version,
declared capabilities, and share roots; status and device lists filter by authenticated owner. The in-memory bridge
queue is deliberately ephemeral transport, not durable job state (credentials/owner records stay in the Sidecar); a
restart fails outstanding bridge calls honestly and durable jobs reconcile via their own state machine.

## Command lifecycle

1. A service resolves the authenticated owner and optional target `agent_id`.
2. The bridge verifies an explicit target is online and owner-owned. An
   unaddressed command is accepted only when that owner has at most one online
   device; ambiguous selection is refused.
3. The command queues with owner, target, optional explicit TTL, and a unique command ID.
4. A matching device poll claims the command before wire projection.
5. Only the claiming owner/device may append stream frames or settle the result.
6. Sequence numbers deduplicate resent stream frames.
7. Default synchronous waits have no private deadline. Completion or explicit
   cancellation/wait bounds settle the caller once. Unclaimed stopped calls
   withdraw their queue entry; claimed work retains owner/device settlement
   until its late result or an explicit pickup TTL settles it.

No environment switch disables addressing; feature switches may hide
remote-worktree UX but cannot weaken bridge authority.

## Client execution and connection lifecycle

`desktop/agent_connection.py` owns polling-worker start/stop/restart for both launchers. Remote full-app control gets
the same status, route-repair, and browser-relay hooks as agent-only control; local full-app control keeps its process
capability and never discovers remote destinations. A worker alive after the stop join stays tracked.

The agent executes ordinary commands, paper builds and egress streams outside of the poll thread, on demand without a local worker ceiling. Legacy IDs stay known until shutdown. Negotiated bridge v3 retires expired finite IDs only after completion and output ACK and refuses stale redelivery; server delivery credits derive from CPU/memory headroom. This is process-local deduplication, not durable exactly-once execution. See [client lifecycle](../DESKTOP_CLIENTS.md). `TOFU_DESKTOP_EXECUTION_WORKERS` is retired and ignored. Pending results/stream frames use launch-probed memory reuse (1/128 of the smaller capacity/headroom, 1 MiB lean fallback), split 7/8 for results and 1/8 for streams; overflow spools to private local temporary files instead of blocking producers. One result poll still sends up to 512 frames/16 MiB and one stream poll up to 256 frames/2 MiB. Full response validation precedes prefix acknowledgement; acknowledgement immediately deletes spooled frames. Teardown waits for owned execution to settle before reclaiming the outboxes. Project stream cancellation kills/reaps its process tree and joins pipe readers; `cancelled`, `timed_out`, and exit status remain independent outcomes. Only Popen reaps its direct child so a killed process cannot acquire a false zero exit status. The browser relay owns an uncapped live-job FIFO and removes each job on timeout, completion or close; close wakes pending request/take waiters. Server egress readers release consumed body chunks instead of retaining full replay; diagnostics retain the 256 KiB provider-error prefix budget (`test_egress_stream_memory.py`). Retry waits are jittered, capped 60s, interruptible by stop. Direct probes/polls honor Requests proxy/NO_PROXY and CA settings, suppress ambient netrc, and do not follow redirects; auth errors carry the flat `bridge_auth_required` envelope. Credentials fail over only to explicitly configured attach candidates — LAN health, public boot IDs, and discovered SSH hosts never authorize bearer reuse; unattended service needs a gateway-supported device route.

## Permissions

The agent reconstructs its permission floor on startup. Read, write, execute,
GUI, notification, browser relay, egress, and adapter capabilities are declared
and enforced on the device; server-side visibility is not sufficient
authorization for a local action. File and command operations resolve against
declared share roots. Remote project writes apply local equivalents of path
containment, freshness, read-before-edit, and atomic-write rules; destructive
targets resolve before execution and must stay inside the selected root.

## Remote projects

A conversation stores `remote:<agent_id>:<root>` in its existing project-path
field; `lib/conv_config` translates this to a `project_remote` binding only when
remote worktrees are enabled. Binding validation requires an online device owned
by the caller, an exact declared share-root name, and both agent and root fields.
Project tools keep their public names; the server route selects the remote
adapter from the validated binding and the agent revalidates the root. Streamed
`run_command` output enters the same `tool_progress`/terminal-tool result
contract as local execution.

## Subscription egress and adapter

`lib/desktop/egress.py` routes only the exact subscription-host allow-list or a
declared adapter loopback port; the agent repeats the target check before opening
a socket. Owner-scoped candidates order by explicit pin and recent health;
failover is bounded and observable. The explicit pin is durable user state in the
Storage Sidecar; the live device registry stays ephemeral transport, filtered by
owner/online/`egress` capability before API display or pin acceptance. A retired
per-installation JSON file is a read-once migration source only — an explicit
Sidecar row (even empty) prevents it becoming a second authority.

The authenticated owner is captured at each HTTP/task boundary and propagated
through dispatch, preflight, OAuth callback/device workers, token refresh, and
provider probes. No transport layer guesses a personal owner: an ownerless
request cannot address a desktop device. Adapter/account/ensure-task caches are
bounded, keyed by `(owner, agent)`, so a cached response cannot bypass ownership
after re-pairing. OAuth relay/device workers carry an opaque flow generation, so
a late callback cannot redeem a replacement login. Secrets stay request headers,
redacted from logs/status; steady healthy selections are DEBUG-only.

The optional local subscription adapter runs on the device loopback interface;
server policy mints adapter credentials and relays management/model calls through
the addressed device. Discovery replaces only its deterministic owner-scoped
model-routing v2 ProviderAccess bundle via bounded CAS — explicit Connection
adapter metadata and encrypted credential references, never a legacy row. Stopping deprovisions that bundle only after the owning device confirms.
Bring-up starts on demand with one active task per owner/device. Active task
state remains owned until settlement; only terminal status uses the TTL cache. Catalog refreshes are
event-driven; fresh quota headers trigger a TTL-throttled (1 h) re-sync only when
the upstream model set changed.

## Pairing and distribution

Pair codes are short-lived, single-use, and bound to the initiating owner;
successful exchange mints a normal `agents:bridge` credential, returned once.
Attach bundles carry explicit candidate routes, validated by the local broker
before persistence. The optional LAN discovery responder is owned once by the
request-process lifecycle; a timed-out owner stays registered, blocking
concurrent re-binding.

The controlled-computer product is **Tofu Remote** (Tofu 受控端), with
`TofuRemote-*` release assets. The AI runtime SDK remains `tofu-agent`.
Remote builds do not start the application server, database or browser UI.
Stable registry/install-directory, bundle IDs and protocol/config keys preserve upgrades. `lib/desktop_dist/store.py` serves only exact manifest keys;
mirrors/builders publish atomically and keep a last-known-good on refresh failure.

Local Control offers only servable artifacts matching OS, architecture and kind. An empty `agent_downloads` list means unavailable, retaining the release link. Windows requires its personalized installer's attachment slot; macOS agent DMGs come from native macOS release jobs, never the Linux server's on-demand builder. Publication requires the changelog, complete platform/component assets and size floors; each native build runs a frozen-runtime smoke before packaging. The full-app spec preserves the `lib/model_info/data` catalog and `frontend/authoring-inputs.json` metadata at their runtime-relative paths (`static/` alone cannot satisfy the import-time model/Vite contracts), and checks source freshness before building. Startup role and tray strings use shared code with parity tests. Agent login startup uses per-user HKCU Run, XDG autostart or macOS LaunchAgents via `desktop/autostart.py`; failed publication preserves the prior entry and does not persist false success.

Mirrored downloads are checked against the advertised byte count and, when
present, the release API's `sha256:` digest before atomic publication; invalid or
mismatched metadata keeps the previous installer, and a changed digest
invalidates same-name/same-size reuse. Releases lacking digest metadata are
legacy downloads, not signature-verified updates (a local checksum is not
publisher authentication); this cache is not an installed-client automatic
updater. It records `integrity_source` (`github-api-digest`/`computed-only`),
keeps `release_sha256` separate, and acquires sequentially (2 GiB/file, 30 min).

## Failure semantics

- Missing/invalid bridge credential: `401`.
- Authenticated poll without stable device identity: `400`.
- Offline/wrong-owner target or ambiguous unaddressed command: reject before
  enqueue.
- Wrong device/owner result or stream frame: ignore and audit; never settle.
- Pickup/result timeout: typed bridge transport failure.
- Remote path outside declared root or insufficient device capability: local
  denial returned as tool failure.
- Stream state expires before terminal: aborted transport, never an infinite
  wait.
- Distribution refresh failure: serve last known good artifact and expose
  refresh diagnostics.

## Invariants

- Credentials and stable agent identity are mandatory on every poll.
- Owner filtering happens before device selection and delivery.
- Only an online, owner-matching, egress-capable device can be newly pinned.
- Command claim and result/stream settlement bind to the same device.
- No compatibility switch can disable addressing or receipt validation.
- Local permissions and root validation remain authoritative on the device.
- Remote/local project paths share user-visible tool semantics and failure
  honesty.
- Egress uses exact target classes and defense-in-depth checks.
- Agent-only and full-app distributions remain separate dependency surfaces.
- Artifact serving never maps untrusted path text directly to the filesystem.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Poll/auth/identity | `lib/bridge_auth.py`, `routes/desktop.py` | credential and missing-frame tests |
| Routing/stream settlement | `lib/desktop/bridge.py` | cross-owner/device, TTL, async wake |
| Device command | agent `_dispatch.py` + focused implementation | permission and wire parity |
| Remote project operation | server project handler + agent `_project.py` | root, freshness, streaming |
| Egress host/route | both server and agent egress owners | allow-list, failover, redaction |
| Adapter lifecycle | both adapter owners | loopback policy, account/catalog sync |
| Pairing/attach | pairing service and attach route | expiry, one-use, owner binding |
| Installer/artifact | `lib/desktop_dist/`, workflow/spec | kind, checksum, smoke, traversal |

## Test map
Run focused tests under `tests/desktop/`, desktop distribution and installer
parity under `tests/desktop_dist/`, remote worktree routing under `tests/tasks_pkg/`
and `tests/tools/`, and asynchronous polling under `tests/browser/`.

Hope large-checkpoint publication is owned by the private environment provider's
[Worker result publication contract](../../bundles/meituan/environment-provider/README.md#worker-result-publication).
Problem: sequential transport prevented a real 25.5 GB research checkpoint from
being delivered. Decision: admit an optional host-pinned Java6 uploader into the
source seal, preserving actor/operation destinations, one publication intent,
the shared deadline and independent byte/hash collection. Alternatives: dropping
optimizer/RNG state prevents exact recovery; retrying uncertain uploads risks
replay. Private Hadoop configuration stays behind the provider boundary; public
compute specs and logical output names remain provider-neutral. The 512 MiB
live same-file trial supports full-checkpoint validation, not completed recovery.


Demand-owned desktop delivery — Problem: negotiated execution slots and replay counts withheld commands, while default 90-second pickup expiry rejected late accepted work. Decision: dispatch independently of CPU/memory/replay credits. `execution_slots` remains a compatibility poll-window hint; bounded command batches and rotating, byte-bounded activity pages regulate transport only. Owner/device-bound delivery acknowledgements prevent omitted activity IDs from causing old work to fill later batches. Default commands have no pickup deadline; their first-dispatch replay horizon is stable and `deadline_enforced=false` distinguishes it from an explicit TTL. Missing policy fields preserve old-server expiry behavior. Completed replay IDs retire only after the horizon, execution completion and both output ACKs. Cancellation and explicit TTLs remain effective. Alternative rejected: larger slots/TTLs still refuse later work; forgetting unacknowledged IDs permits duplicate execution. The queue remains ephemeral transport, not a durable-job authority; application restart reconciliation stays with the owning job. Guards cover forty old queued commands with zero advertised slots and thirty-two held handlers entering concurrently in legacy/v3 loops, plus lost ACK, addressing, cancellation and metadata page rotation.


## Synchronous desktop wait ownership

Problem: both the bridge and desktop tool handler imposed an implicit 30-second wait, then discarded the command even when a device was still executing it. Decision: default waits continue until an owned result; explicit nonnegative finite wait bounds remain exact (zero is immediate). Tool task Stop is checked without creating a watcher thread or another queue. Stop before publication creates no command. A timeout or Stop withdraws unclaimed work; a claimed command retains its existing entry until the claiming owner/device supplies a result. The caller receives an unconfirmed remote outcome on Stop, never a claim that a GUI action or remote process was killed. Settlement and caller detachment use one queue lock, closing their race. The transport remains ephemeral. Alternatives considered: increasing 30 seconds still loses healthy late work; dropping claimed entries loses result authority; pretending Stop kills device work is unsupported by the generic desktop protocol. Verification: addressing tests advance the wait clock beyond one hour before delivering the owned result, check task cancellation before/after pickup, late settlement after explicit timeout, foreign-owner refusal, duplicate result handling and entry cleanup.


## Desktop command execution time

Problem: healthy desktop commands were killed by implicit 30/300-second worker timeouts, project overrides were clamped to one hour, and server project calls abandoned results under a separate 60/330-second wait policy. Decision: absent execution timeout means actual completion; finite nonnegative explicit seconds are passed unchanged to the executing device. Invalid values fail before spawn. The server waits for the worker's observed outcome with task cancellation, forwarding explicit execution bounds without a private bridge clamp. Its progress reader retires in a joined `finally` path, including bridge failures. Generic desktop shell commands share the existing streamed subprocess path, including exec permission checks, owner-stop/connection-loss cancellation and joined pipe-reader cleanup. Their runtime admission lasts until the asynchronous terminal callback; replay cannot start another copy. Direct synchronous fallback keeps its separate completion owner. Alternatives considered: larger defaults still kill healthy work; just removing the server wait leaves the worker timer; removing cancellation abandons live processes. Verification: real blocked commands survive a simulated 4,000-second elapsed interval before completion, explicit 5,000-second values remain exact, invalid policies never spawn, and default-unlimited cancellation joins readers before terminal settlement. Cooperative stop/connection-loss and abrupt owner death share the supervised command lifetime described below.


## Independent desktop process supervision

Problem: a direct subprocess outlived an abruptly killed agent after private execution deadlines were removed. Decision: synchronous and streamed commands reuse `lib/paper/builds/process_worker.py` through the existing source/frozen worker entry. The worker waits for its private start marker, checks its creator before admission, and owns descendant retirement independently of the agent. Linux subreaper ownership catches descendants that start a separate session; Windows job binding precedes the start marker, with creator birth/liveness checks protecting the pre-binding interval. Separate private stdout/stderr files support windowed frozen executables without stdio handles. The agent tails these files and joins readers before removing its directory and delivering the final result. Explicit timeout/Stop requests ask the worker to retire children and retain the actual signed command exit status. Cleanup failure remains a visible terminal error. Parent death removes the validated desktop IPC files independently on POSIX. Alternatives considered: a thread watchdog dies with its owner; killing only a shell misses escaped descendants; restoring a timer kills healthy work. Verification: real Linux owner SIGKILL with an escaped child and grandchild leaves no live commands, reaps command descendants and removes private output; pre-admission retired ownership causes no side effect. Separate streams, signed exit status, Stop, explicit timeout, startup failure and cleanup failure remain covered. Windows creator identity is contract-tested; native Windows job/crash cleanup is not verified by the Linux tests. Source/frozen dispatch uses the existing worker flag and does not add an agent startup service.
