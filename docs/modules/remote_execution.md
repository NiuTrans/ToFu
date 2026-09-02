# Desktop devices and remote execution

This domain connects an owner-authorized desktop device to the server for
desktop tools, remote project work, subscription egress, and local subscription
adapters. It also owns desktop/agent packaging and pairing. The bridge is a
capability boundary, not a trusted-network shortcut.

## Ownership

| Concern | Owner |
|---|---|
| Bridge credential resolution | `lib/bridge_auth.py`, `routes/_bridge_caller.py` |
| Poll HTTP adapter | `routes/desktop.py` |
| Device registry, command queue, streams | `lib/desktop/bridge.py` |
| Device status/pairing/distribution API | `routes/api_v1/desktop.py` |
| Pair-code authority | `lib/desktop/pairing.py` |
| Agent runtime | `lib/desktop_agent/` |
| Agent permission policy | `lib/desktop_agent/_permissions.py` |
| Remote project binding | `lib/desktop/remote.py`, `lib/conv_config/` |
| Remote project execution | `lib/tasks_pkg/handlers/project.py`, agent `_project.py` |
| Subscription egress routing | `lib/desktop/egress.py`, agent `_egress.py` |
| Local subscription adapter | `lib/desktop/adapter.py`, agent `_adapter.py` |
| Full-app/agent launchers | `desktop/launcher.py`, `desktop/agent_launcher.py` |
| Build and artifact store | `lib/desktop_dist/`, `tofu-agent.spec`, release scripts |
| Settings UI | `frontend/src/features/settings/devices.ts` |

## Trust and identity

Remote devices authenticate with an owner-scoped credential carrying
`agents:bridge`. The packaged full desktop app may use the non-persisted
process capability created by `lib/bridge_auth.py`; it is accepted only by the
desktop poll boundary. IP address, loopback appearance, and a global shared
secret grant no authority.

Every poll must include a stable `agent_id` frame. Anonymous/version-one
pollers are rejected. Registration records owner, credential ID, platform,
version, declared capabilities, and share roots. Status and device lists filter
by authenticated owner.

The in-memory bridge queue is deliberately ephemeral command transport, not
durable job state. Durable credentials and owner records remain in the
Sidecar. A server restart fails outstanding bridge calls honestly; durable
application jobs must reconcile that transport failure through their own
state machine.

## Command lifecycle

1. A service resolves the authenticated owner and optional target `agent_id`.
2. The bridge verifies that an explicit target is online and owned by that
   owner. An unaddressed command is accepted only when that owner has at most
   one online device; it is refused when selection would be ambiguous.
3. The command is queued with owner, target, TTL, and a unique command ID.
4. A matching device poll claims the command before it is projected on wire.
5. Only the claiming owner/device may append stream frames or settle the
   result.
6. Sequence numbers deduplicate resent stream frames.
7. Completion, timeout, or TTL expiry settles the waiter once and removes
   bounded state.

There is no environment switch that disables addressing. Feature switches may
hide remote-worktree UX, but cannot weaken bridge authority.

## Permissions

The agent reconstructs its permission floor on startup. Read, write, execute,
GUI, notification, browser relay, egress, and adapter capabilities are declared
and enforced on the device. Server-side visibility is not sufficient
authorization for a local action.

File and command operations resolve against declared share roots. Remote
project writes apply local equivalents of path containment, freshness,
read-before-edit, and atomic-write rules. Destructive command targets are
resolved before execution and must remain inside the selected share root.

## Remote projects

A conversation stores `remote:<agent_id>:<root>` in its existing project-path
field. `lib/conv_config` translates this to a `project_remote` binding only when
remote worktrees are enabled. Binding validation requires:

- an online device owned by the caller;
- an exact declared share-root name;
- both agent and root fields.

Project tools keep their public names. The server route selects the remote
adapter from the validated binding, and the agent revalidates the root. Streamed
`run_command` output enters the same `tool_progress` and terminal-tool result
contract as local execution.

## Subscription egress and adapter

`lib/desktop/egress.py` routes only the exact subscription-host allow-list or a
declared adapter loopback port. The agent repeats the target check before
opening a socket. Owner-scoped device candidates are ordered by explicit pin
and recent health; failover is bounded and observable.

Direct/proxy reachability probes are hints for route selection. Network
failure caches are short and invalidated when proxy topology changes. Secrets
remain request headers and are redacted from logs and status documents.

The optional local subscription adapter runs on the device loopback interface.
Server policy mints adapter credentials and relays management/model calls
through the addressed device. Provider catalogue state is synchronized through
the provider authority; stopping the adapter deprovisions its managed provider.

## Pairing and distribution

Pair codes are short-lived, single-use, and bound to the initiating owner.
Successful exchange mints a normal `agents:bridge` credential and returns it
once. Attach bundles carry explicit candidate routes and are validated by the
local broker before persistence.

The optional LAN discovery responder is owned once by the request-process
lifecycle. Its UDP socket and private wake socket block on readiness, so an
idle responder has no application-level polling cadence. Shutdown signals and
bounded-joins that exact owner; a timed-out owner remains registered so another
responder cannot bind the same discovery authority concurrently.

The full desktop app and agent-only app are separate artifacts and launchers.
Agent-only builds do not start the web server, database, browser UI, or full-app
component manager. `lib/desktop_dist/store.py` serves only exact manifest keys;
mirrors and builders publish complete files by atomic replacement and retain a
last-known-good artifact on refresh failure.

Startup role selection and tray strings are shared code with parity tests.
Installers may differ by platform, but artifact kind, version, checksum,
architecture, and attachment behavior remain explicit metadata.

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

```bash
pytest -q tests/test_desktop_bridge_addressing.py \
  tests/test_browser_async_poll.py
pytest -q tests/test_remote_worktree_entry.py \
  tests/test_remote_worktree_routing.py tests/test_remote_stream_ui.py
pytest -q tests/test_desktop_exec_streaming.py tests/test_desktop_agent_project.py
pytest -q tests/test_desktop_egress.py tests/test_desktop_egress_stream.py
pytest -q tests/test_desktop_pairing.py tests/test_desktop_pair_agent.py
pytest -q tests/test_desktop_dist.py tests/test_desktop_build_workflow.py \
  tests/test_installer_parity.py
```
