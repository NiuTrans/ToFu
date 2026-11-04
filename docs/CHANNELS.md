# Channels: external conversations as event sources and reply targets

Responsibility: channel accounts, normalized channel events, threads, the
deterministic ingress policy, subscriptions (passthrough), delivery into
conversations, the outbox, and the 大象 (Daxiang) and GitHub adapters. Router
roles, forwarding and standing instructions are owned by [ROUTER.md](ROUTER.md).

Owner: `lib/channels/` (guide: `lib/channels/AGENTS.md`). Wire, tool and
vocabulary authority: [`contracts/channels_v1.schema.json`](../contracts/channels_v1.schema.json)
(enums, defaults, provider limits, HTTP DTOs, tool schemas); the browser types
are generated from it (`npm run generate:channels` →
`frontend/src/api/channels.generated.ts`).

## Vocabulary

| Term | Meaning |
|---|---|
| **Channel** | An external conversation medium: `dx` (大象) and `github` (GitHub issues and pull requests, C8). Per-channel capabilities (command chat, quote replies) are `x-channelTraits` in the contract. |
| **Channel account** | The owner's binding to one channel identity (大象 uid/mis/name; GitHub numeric user ID/login/name) plus its configuration, status and catch-up frontier. Owner-scoped. |
| **Thread** | One event stream: `dx:gid:<gid>` (group), `dx:uid:<uid>` (private chat), `dx:pid:<pid>` (public account / bot session), `github:<owner>/<repo>#<number>` (one issue or pull request, stored lowercase because GitHub names are case-insensitive). External IDs are digits. |
| **Channel event** | One inbound message, recorded once per `(owner, thread, external ID)`; its event ID is `<thread>/<external ID>` (split at the last `/`). A provider message ID belongs to exactly one thread, so this is the `(owner, channel, externalId)` identity. |
| **Command chat** | The owner-selected 大象 thread whose owner-authored messages are human input to the Global Router. GitHub has none (C8). |
| **Thread mode** | Deterministic ingress policy: `command`, `watch`, `mentions`, `ignore`. |
| **Reply policy** | Outbound policy for a non-command thread: `auto`, `draft`, `never`. |
| **Subscription** | A thread → conversation passthrough edge created by a router. Many-to-many. |
| **Outbox item** | One durable outbound message with an idempotency key, a per-thread queue position and a state (`draft`, `queued`, `sending`, `unknown`, `sent`, `failed`, `discarded`). |
| **Watermark / frontier** | Per thread: the newest processed provider ID (bounds history replay; never decides duplicates). Per account: the start time of the last completed catch-up. Both drive reconnect catch-up. |

## Decisions

### C1. Record once, then route; gaps become duplicates, never losses

**Problem:** IM transports deliver at least once, reconnect, get kicked off, and
the 大象 engine's `subscribe_messages` only listens during its call window, so
messages between windows are silently lost.

**Decision:** the bridge keeps persistent handlers for its whole process
lifetime and every message becomes a `ChannelEvent`. `ingest` processes events
in order: echo check → `ignore` keeps thread metadata only → classify →
record with CAS (`record.put`, `expected_version=0`) → route → advance the
thread watermark. A routing failure raises **before** the watermark moves; the
adapter then halts its session and the supervisor restarts it, and the
replay finds the event recorded but unrouted (`state: pending`) and routes it
again with the same deterministic turn command ID. On every (re)connect the
adapter lists sessions updated since the account frontier and replays each
non-ignored thread's history after its watermark (or, for a thread without
one, after `max(frontier, thread.sinceMs)`) through the same `ingest`, before
any live event queued behind the `connected` status; dedup turns overlaps into
duplicates. Only the CAS record decides a duplicate: a push may arrive after a
newer one of the same thread, so a never-recorded event below the watermark is
processed normally (the watermark never moves back). The account frontier moves
only to the start of a completed catch-up, never to the server time of a live
push, because pushes of different threads can arrive out of server-time order.
Enabling an account sets the frontier to "now"; turning a thread
from `ignore` to another mode resets its watermark and sets `sinceMs`, so an
ignored past is never replayed.

**Alternatives rejected:** trusting the live stream (loses messages); periodic
polling of every thread (adds a latency/interval knob and cost — constraint 5);
per-event retry loops in the adapter (a retry count; the existing tick restart
already replays).

### C2. The fast path is deterministic; the router is the slow path

| Mode | Owner-authored message | Other people's messages |
|---|---|---|
| `command` | Global Router, **human input** | as `watch` |
| `watch` | context for subscribers only | delivered as a candidate |
| `mentions` | context for subscribers only | delivered when the thread is a private chat, or the message @-mentions the owner (or everyone) or quote-replies to the owner |
| `ignore` | dropped | dropped; only thread metadata is kept so the owner can enable it |

Defaults: the command chat is `command`; private chats `mentions`; groups
`ignore`; public-account/bot sessions (`pid`) `ignore` — they are broadcast-like
(approval and notification bots) and the owner enables the ones that matter;
GitHub issues and pull requests (`issue`) `mentions`. Reply policy default:
`draft`. Owner-authored messages outside the command chat are never commands.
On a channel without a command chat (GitHub) the owner's own messages are
delivered as nonhuman candidates in `watch` and are context otherwise (C8). Messages sent by the outbox are never ingested as input:
an owner-authored event is an echo when it carries the outbox marker
(`extension.tofuOutbox`), when its provider ID or client UUID was recorded for a
sent item, or when the bridge flags it as its own connection's send; an echo
of an in-flight item confirms that item as sent. Mentions and quote replies
are protocol facts (`extension.at`, mention links, decoded quote senders),
compared with the identity bound at enable time. Delivered events, owner
command messages and owner context are stored (`channel_read`); dropped
messages and ignored threads are not. The Global Router decides on delivered
candidates and may stay silent.

**Alternatives rejected:** keyword or regex prefilters (heuristic knobs); running
the model on every message of every group (cost and privacy).

### C3. Subscriptions are passthrough, observed by the Global Router

**Decision:** `channel_subscribe(thread, conversation_id)` (Global Router only)
creates an owner-scoped edge to an active conversation of the same owner.
Delivered candidates and owner context of a subscribed thread go directly to
every subscriber, skipping a router decision. The Global Router receives a
non-waking observation in its **project mailbox** (the router is the bound
Coordinator of its home project): a fixed-shape pointer naming the thread, the
event ID range and the subscriber count (the subscriber list is one recall
away in `channel_read(thread=…)`), injected as nonhuman context at the router's
next round without starting a turn. A mailbox refusal never blocks ingress;
the event then records no `observation` delivery. `channel_unsubscribe` hands the thread back. A
subscriber whose conversation is gone is unsubscribed at delivery and its
candidates return to the router. Subscribers never send to channels; they may
read their own threads with `channel_read`.

**Alternatives rejected:** re-deciding every follow-up in the router (the cost
passthrough removes); a channel-private observation queue or a new per-round
injection step (a second inbox — constraint 4; the mailbox already owns
durable, non-waking, receipt-confirmed context); waking the router per
passthrough batch (the cost passthrough exists to avoid).

### C4. Delivery reuses the turn command

**Decision:** delivering events into a conversation is one
`dispatch_scheduled_turn(..., inject_mode='steer', turn_kind='channel_reply')`
with a command ID derived from the target and the event IDs, in two namespaces:
`channel-event-…` for nonhuman batches and `channel-owner-…` for owner command
messages. The steer lane records a merged input only by that command ID
(`_userSteerInjects[].commandId` on the running Turn), not by origin, so the
namespace is what lets C7 see a nonhuman batch inside a human Turn. A running target
receives the batch at its next round boundary, merged with other pending input
(the router's natural batching under load); an idle target starts one turn; a
closed steer window queues the batch. A catch-up replay delivers one batch per
target. Third-party events and owner context use the nonhuman initiator
`channel` (`lib/turn_initiation.py`; persisted as `origin.initiator`, refused by
`require_human_turn`). Command-chat owner messages are human input whose text
is byte-identical to the 大象 message (so ROUTER.md R2 forwarding stays
verbatim), with `origin.channel = {channel, thread, eventIds, title}`. The
nonhuman block lists each thread (`<channel-thread key title mode
reply_policy subscribers>`) and each event (`<channel-event id thread sender
sender_id time disposition private_chat mentions_owner replies_to_owner
reply_to>`) with escaped text, so the model can walk thread → events →
subscribers (chain of recall). Text above the ordinary tool-result inline
limit (`lib.tools.result_value.INLINE_LIMIT`) is spilled to an owner-scoped
tool-result artifact and referenced by `artifact_ref` and event ID.

**Alternatives rejected:** a second inbox, queue or wake loop (constraint 4);
batching windows or debounce intervals (constraint 5); wrapping command text
in metadata (breaks verbatim forwarding).

### C5. Outbox: durable, ordered, exactly once or reconciled

**Decision:** `channel_send(thread, text, reply_to?, idempotency_key)` records an
outbox item (ID derived from owner, thread and key) before any network effect.
Policy is decided once at enqueue: command chat → `queued`; other threads by
reply policy: `auto` → `queued`, `draft` → waits for owner approval (Settings
or the Global Router's `channel_outbox_approve` on a genuine human turn),
`never` refuses and records nothing. A Global Router Turn whose human input
came from a channel thread enqueues its final assistant answer to that thread
with idempotency key = the Turn ID (`lib/channels/autoreply.py`, observing the
existing task terminal-listener seam). Agent-authored messages in threads
other than the command chat carry the visible label `[Tofu 代发]`. Each queued
item takes an exact per-thread queue position (CAS on the thread record);
approval appends a draft at the end. The executor sends each thread in order,
threads independently: drafts and definite provider refusals (`failed`) never
block, an `unknown` item blocks only its thread. Before any resend an unknown
or interrupted (`sending`) item is reconciled against the thread history since
its attempt, by marker or client UUID. Sent items keep the provider message ID
and client UUID for echo suppression and recall. Messages are sent as markdown
(大象 quote replies through `reply_message`; GitHub has no quote replies, so
`reply_to` is refused there). Each running adapter registers one executor per
(owner, channel) and executes only its own channel's items. A send that
settles `failed` or `unknown` also becomes one Router Watchdog fault signal per
item and state ([ROUTER.md](ROUTER.md) R7); the outbox's own handling is
unchanged.

**Alternatives rejected:** sending inside the tool call (lost on crash,
duplicated on retry); impersonating the owner without disclosure; ordering by
timestamps (equal milliseconds reorder a thread).

### C6. 大象 adapter

- **Engine and auth:** the owner's installed `dx-pura-api` skill
  (`owner_global_skills_dir(owner)/dx-pura-api`, engine
  `vendor/node_modules/dx-engine`), always run through its Tofu launcher
  `scripts/tofu/dx-tofu.sh` with `DX_TOFU_OWNER=tofu:<owner>`. SSO tickets
  exist only in the launcher child's environment; the parent passes no ambient
  secrets and nothing enters model context, Tofu storage or argv.
- **Login MIS** (`lib/channels/dx/login.py`): the account logs in as the MIS
  the owner entered in Settings, else its bound identity's, else the host's
  shared SSO session account. That MIS selects the launcher child's SSO
  session: the host's shared DataFE session when it belongs to that MIS,
  otherwise the owner's own `<data>/channels/dx/sso/u<owner>/<mis>.json`
  (directories `0700`). The SSO adapter's `exec` reuses a usable session and
  requests one 大象 login approval only when there is none; only the DataFE SDK
  writes session files and Tofu never reads them. The shared session's MIS and
  whether a login needs an approval are the adapter's own answers
  (`discover_login_mis`, the non-interactive `session_status`), asked in a
  child process.
- **Bridge** (`lib/channels/dx/bridge.mjs`, spawned from `PinnedHelperSource`):
  one Node process per account. It logs in with the engine's own
  `DxWebSdkRuntime`, attaches persistent handlers for `IM_message`,
  `PUB_MESSAGE`, `KickOff`, `KickOffWithSameDeviceId` and `ConnStatusChange`,
  writes JSON-lines (`status`,
  `message`, `response`) to stdout and executes JSON-lines commands from stdin
  (`whoami`, `send_text`, `send_markdown`, `reply_message`, `history`,
  `sessions`, `create_group`), each answered with its request ID. It decodes
  quote parts so reply facts survive and flags its own sends. Disconnect is
  the SDK's own `CONSTANT.LOGIN_STATUS.DISCONNECTED`. On a transport loss it
  reports `disconnected`, reconnects the existing SDK socket and calls the
  engine's `login()` with its in-memory credentials. The engine owns rejection
  recovery and its existing login deadline; its `AuthFail` retry also needs a
  new socket because `autoConnect` is off. Concurrent loss notifications and
  commands join that login. Only successful authentication for the same uid
  reports `connected`, triggering the adapter's ordinary catch-up again.
  Commands already dispatched are never replayed. Failed recovery exits (4)
  for the supervisor/launcher to refresh the session; kick-off exits (43) and
  stdin close exits (0), including during recovery. A kick-off reports
  `DISCONNECTED` before its kick event in the same synchronous dispatch, so
  recovery defers one event-loop turn to let the kick decide. An SDK without
  the required status or socket-reconnect interface fails closed (4).
  It holds no policy and no durable state. One-shot mode
  (`--command whoami|create_group`) serves identity confirmation and
  command-chat creation.

  **Problem:** exiting on every socket close discarded valid IM credentials;
  repeated network closes therefore called `/login/im_token/generate` on each
  restart and left ingress offline until the scheduler tick. **Decision:**
  reconnect in the existing process, retaining the engine's credential cache
  and using its authentication outcome as the validity check. **Alternatives
  rejected:** ignoring disconnect leaves ingress deaf; the engine's
  `reconnect()` unconditionally invalidates credentials; a disk token cache or
  guessed TTL duplicates authentication policy; a new refresh/backoff timer
  adds a tuning knob. No new timer, retry budget or credential store is added.
- **Adapter** (`lib/channels/dx/adapter.py`): normalization of live pushes and
  history rows (server time from the snowflake ID), reconnect catch-up,
  sequential ingest, and the outbox sender port. Provider limits are named in
  the contract (`dxHistoryPageLimit` 100, `dxSessionListLimit` 500; a full
  session page bisects its time window). History paging (`history_pages`)
  follows the provider's observed contract, not the engine's `nextCursor`:
  group and private-chat pages arrive oldest-first, public-account pages
  newest-first, `cursorMsg` is an exclusive upper bound, and the engine's
  `nextCursor` is the page's *last* row (the newest one for groups, so
  following it re-reads the same page). The next cursor is the page's smallest
  message ID, as in the skill's own exporter; `-1` or a page without unseen IDs
  ends the walk.
- **Supervisor** (`lib/channels/dx/supervisor.py`): runs where the
  scheduled-jobs capability runs, from the scheduler's existing 30 s tick
  (`lib/scheduler/manager.py`) and immediately after a configuration change in
  a hosting process. It starts a session for an enabled account (after
  checking identity, skill and Global Router), stops it when disabled or
  switched to another login MIS, and restarts it after any exit at the next
  tick — no timer, backoff schedule or retry count of its own. An enabled
  account whose login MIS has no bound identity is first confirmed on its own
  thread (see Identity), so neither the tick nor an HTTP request waits for it. Launcher exit `42` (`SSO_SESSION_REQUIRED`) and
  kick-off stop restarts and surface `needs_login` / `kicked` until the owner
  re-enables; an absent router or skill surfaces `error` without starting.
  A session reports a state transition once, so an account command that does
  not start a session must not overwrite that status: enabling an already
  enabled account, or saving its unchanged configuration, keeps the reported
  state and `connectedAtMs` (`session_owns_status`, `lib/channels/model.py`).
  Only a real session start sets `starting`, and disabling sets `disabled`.
  Each tick of this and of the GitHub supervisor (C8) also reports an
  unhealthy enabled account to the Router Watchdog: one fault signal per state
  since the last connection ([ROUTER.md](ROUTER.md) R7).
- **Identity:** the supervisor confirms it with `whoami` logged in as the login
  MIS before a session starts; an event whose sender equals that uid is
  owner-authored. The account reports `authorizing` while the adapter says the
  login waits for an approval (`login_required` or `pending`), otherwise
  `starting`. A `whoami` answering another MIS is `identity_mismatch`
  (`error`); a refused or unapproved login is `needs_login`. Choosing another
  MIS clears the identity and the command chat and restarts ingress from now.
  A newer choice starts its own confirmation at once instead of waiting behind
  an older pending approval; the older one's late answer is discarded by an
  atomic condition on the account write (`ChannelStore.set_status(when=)`).
  Problem: enabling ran `whoami`, including a possible 180 s approval wait,
  inside the HTTP request, and the account could not be switched. Decision:
  account commands return at once and the supervisor confirms asynchronously.
  Alternatives rejected: a shorter request timeout (fails slow approvals and
  adds a knob); a browser-polled login job (a second job resource for one
  state the account already reports); copying tokens or running CIBA in Tofu
  (a second credential store or authentication implementation).
- **Command chat:** chosen in Settings, or created on explicit owner request
  (`POST …/command-chat`) as a single-member group named `Tofu Router`; a
  creation claim on the account prevents a second group.

### C7. Authority

| Source | Authority |
|---|---|
| Owner in the command chat | Human input (sender = bound owner account, thread = command chat) |
| Owner in any other thread | Nonhuman context |
| Owner's own GitHub comments | Nonhuman evidence: a candidate in `watch` threads, context otherwise; never human input (C8) |
| Other people | Nonhuman evidence (initiator `channel`) |
| Outbox echoes | Never input |
| Router or worker text | Nonhuman |

Tool authority is decided once in `lib/channels/tools.py`: every channel tool
requires the owner's Global Router conversation (resolved through
`lib.router.binding.global_router`), except `channel_read`, which a subscriber
of the requested thread may also use; `channel_outbox_approve` and
`channel_configure_thread` additionally require the executing user attempt
(user principal, attended, the task's own assistant Turn) whose human input
passes `require_human_turn`, and refuse when a `channel-event-` batch was
steered into that Turn (persisted or already consumed): a third-party message
merged into the owner's request never carries the owner's authority. Owner
command-chat messages steered in (`channel-owner-`) keep it. The catalog projection (`ToolSpec 'channels'`)
mirrors this and the Global Router's coordinator ceiling admits the names via
`ROUTER_TOOLS`. HTTP account changes need the `admin` scope; reads and owner
decisions need `conversations`.

TODO(enterprise): one channel account and bridge per Tofu user; the account
binding and store keys are owner-keyed, but the supervisor hosts only the
scheduler principal's owner and ingest is serialized per owner inside one
process. Several scheduler replicas need an enabled-account index and a
per-account lease first. The GitHub supervisor has the same limit.

### C8. GitHub adapter

**Problem:** task context lives on work surfaces as well as in IM: the owner
chooses plan A in the 大象 command chat, then switches to plan B in a pull
request comment, and the Global Router must relate the two. Tofu runs on a
private server that GitHub webhooks cannot reach, and a GitHub comment is
written, and editable, by many people.

**Decision:**

- **Threads.** One thread per issue or pull request,
  `github:<owner>/<repo>#<number>`, kind `issue` (GitHub's REST API treats
  every pull request as an issue: one number space, one comments endpoint),
  stored lowercase. Default mode `mentions`, default reply policy `draft`.
  Subscriptions, standing instructions (ROUTER.md R4), `channel_read` and the
  outbox work on these keys like on 大象 threads.
- **No command chat; every comment is nonhuman.** A command chat requires that
  only the owner can author and alter its text. On GitHub, repository
  maintainers, organization owners and apps with write access can edit any
  comment, and an edit made before the poll is indistinguishable in REST from
  the owner's text, so no GitHub thread qualifies (`x-channelTraits.github.
  commandChat: false`; the 大象 account refuses a GitHub command chat). The
  owner's own comments are therefore evidence, rendered with
  `owner_authored="true"`: a candidate in `watch` threads, so the Global Router
  sees the owner's pull request comment next to the IM history that chose
  plan A, and context (subscribers only) elsewhere. To act on it the router
  either asks the owner in the command chat or applies a standing instruction
  the owner created in a human turn and scoped to that thread.
- **Ingress by polling** (`lib/channels/github/adapter.py`, one serial worker
  per owner, as GitHub asks integrations not to make concurrent requests).
  `GET /notifications?all=true&since=<frontier>&per_page=50` (the documented
  maximum) sends the stored `Last-Modified` as `If-Modified-Since`; `304`
  means nothing changed and costs no rate limit. GitHub's `X-Poll-Interval`
  is the only cadence: it is persisted (`poll.nextAtMs`) right after each
  notifications response, before any processing, so a restarted session
  never polls early; until GitHub has sent one, the scheduler's existing tick
  triggers passes. Only `Issue` and `PullRequest` subjects are threads. For
  every notified thread and every thread in `watch` the pass reads the opening
  post once (when it was created after the start), issue comments and, for
  pull requests, review comments (`?since=`, `per_page=100`, `Link` pages).
  Each list keeps its `since` until it returns rows and sends its ETag
  (`If-None-Match`), so an unchanged thread answers `304`. A review reply
  carries its parent's author (`in_reply_to_id`, read by ID when the parent is
  outside the window). All events are ingested in one `ingest_batch`; only
  after it succeeds do the thread cursors and ETags, the account frontier
  (newest notification `updated_at`, GitHub's clock) and `Last-Modified`
  move. A route failure ends the session; the supervisor restarts it at the
  next tick and the same window replays as duplicates. A `404`/`410` thread
  is marked unavailable and is read again only when GitHub notifies it.
- **Watch reads the thread directly.** GitHub documents notifications as
  updates for subscribed activity and offers one's own updates only as an
  email option, so the owner's own comments are seen by reading watched
  threads on every pass. A thread that enters `watch` starts at that moment
  (`modeChangedAtMs`); what `mentions` left unread is not replayed. Such owner
  decision times are this host's clock; each pass moves them onto GitHub's
  clock by the skew measured from the notifications response's `Date`.
- **Event IDs.** `<created_at epoch seconds, 10 digits><kind, 1 digit><GitHub
  id, 20 digits>` with kinds 1 opening post, 2 issue comment, 3 review comment
  (`x-github.contentKinds`). GitHub keeps separate ID sequences per kind, so
  the kind keeps a thread's IDs unique; the time prefix orders them like a
  snowflake (watermark, newest-first reads). Mentions are `@login` outside
  code (team mentions do not count); events carry their GitHub `url`.
- **Outbox.** `POST /repos/{owner}/{repo}/issues/{n}/comments` with the
  disclosure label and an invisible marker `<!-- tofuOutbox:<item> -->`
  (echo suppression and reconciliation); at least one second between posts
  (GitHub's guidance for mutative requests). `400`/`422`, `401`, a
  non-rate-limit `403` and `404`/`410` are definite failures
  (`invalid_input`, `auth_failed`, `forbidden`, `unsupported_target`); a rate
  limit, `5xx` or network error leaves the item `unknown`, and it is
  reconciled by reading the owner's comments in the thread for the exact
  marker before any resend (no clock comparison between GitHub and this
  host). Redirects repeat the same request on the same origin; the token
  never leaves the configured origin.
- **Rate limits.** `retry-after`, else `x-ratelimit-remaining: 0` until
  `x-ratelimit-reset` (measured against the response's `Date`, so local clock
  skew cannot shorten the wait), else (`429` or a secondary-limit message) at
  least one minute: the worker pauses polling and sending until then (status
  `disconnected` with the resume time).
- **Token.** The notifications endpoints accept only a personal access token
  (classic) with `notifications` or `repo`. A token pasted in Settings is
  verified with `GET /user` and `GET /notifications` before it is stored, in
  the owner-scoped vault entry `channel.u<owner>.github.token`. The vault
  reserves `channel.` entries from agents (vault index, `run_command`) and
  from the generic credential routes, so no agent can post outside the
  outbox and its reply policy; the owner may instead choose an existing
  general vault entry by name. The value is read per request and never
  logged, returned or written to channel storage. A `401` ends the session as
  `needs_login` with no restarts until the owner saves a token that verifies.
  A changed identity, credential or API base restarts the session. Status
  ownership follows C6: saving a token whose probe leaves the session running
  does not reset a reported state.
- **GitHub Enterprise Server.** `apiBaseUrl` (https only, e.g.
  `https://<host>/api/v3`) is an account field changeable while disabled.

**Alternatives rejected:** webhooks with `X-Hub-Signature-256` (GitHub cannot
reach the private server, and they need an unauthenticated route through the
default-deny boundary plus per-owner secrets, while conditional polls cost no
rate limit when nothing changed); a GitHub command thread (anyone with write
access could edit text into owner commands); raw comment IDs as external IDs
(separate ID sequences collide and misorder mixed threads); an own poll
interval or `since` overlap margin (tuning knobs, constraint 5: GitHub's
header and second-granular `since` with dedup suffice); repository-wide
comment listings or the user events API for the owner's comments (more
requests, and GitHub documents event latency of 30 s to 6 h); fine-grained
tokens or GitHub Apps (the notifications API rejects them).

**Limits:** a comment that becomes visible later than the window that should
have held it is missed (GitHub documents no visibility bound, so no margin is
invented); the owner's comments on threads outside `watch` arrive only with a
later notified pass of that thread; a renamed repository is a new thread
key; pull request review summaries and edits of recorded comments are not
events.

## Durable state

`ChannelStore(owner)` keeps everything in the `channels.v1` record namespace
under `u<owner>/` keys (lowercase ASCII only, because the SQLite prefix match
is case-insensitive; GitHub thread keys are lowercased): `account/{dx,github}`,
`thread/<key>`,
`event/<thread>/<zero-padded id>`, `subscription/<conversation digest>/<thread>`,
`outbox/<item>`, `outbox-active/<thread>/<queue position>-<item>` and
`echo/dx/{mid,uuid}/<digest>`. It is durable user state: no TTL, no pruning.
Growth is one record per stored event (delivered, command or context), one per
outbox item plus two small index rows, and one per thread; ignored threads add
only their thread record. The GitHub account adds its credential name, poll
schedule (`intervalS`, `lastAtMs`, `nextAtMs`, `lastModified`) and API base;
each GitHub thread record holds `github` state (opening-post time, pull
request flag, per-list cursors and ETags). The token itself lives only in the
credential vault. The 大象 account adds its chosen login MIS (`mis`); its SSO
session files (C6) are SDK-owned credentials outside the record namespace.

## Interfaces

- `lib/channels/model.py` — `ChannelEvent`, `ThreadKey`, `OutboxItem`, contract vocabularies.
- `lib/channels/store.py` — `ChannelStore(owner)`.
- `lib/channels/policy.py` — `effective_mode`, `effective_reply_policy`, `classify`.
- `lib/channels/ingress.py` — `ingest(owner, event)`, `ingest_batch(owner, events)`.
- `lib/channels/delivery.py` — `deliver`, `deliver_command`, `observe_router`, `global_router_binding`.
- `lib/channels/outbox.py` — `enqueue`, `approve`, `discard`, `execute`, `confirm_echo`, `kick`.
- `lib/channels/service.py` — owner commands/views shared by HTTP and tools.
- `lib/channels/tools.py` — `channel_threads`, `channel_read`, `channel_send`,
  `channel_configure_thread`, `channel_subscribe`, `channel_unsubscribe`,
  `channel_outbox`, `channel_outbox_approve` (handler `lib/tasks_pkg/handlers/channels.py`).
- `lib/channels/autoreply.py` — router answer → command-chat thread.
- `lib/channels/dx/` — `bridge.mjs`, `adapter.py`, `login.py`, `supervisor.py`, `probe.py`.
- `lib/channels/github/` — `client.py` (REST transport, conditional requests,
  paging, rate limits), `normalize.py`, `adapter.py` (poll worker, sender
  port), `account.py` (Settings commands, token custody), `supervisor.py`.
- HTTP (`routes/api_v1/channels.py`, served under `/api/v4`):
  `GET|PUT /api/v4/channels/dx` (config and status; `PUT` enables/disables,
  sets the login `mis` or the command chat and returns without waiting for a
  login),
  `POST /api/v4/channels/dx/command-chat` (create `Tofu Router`),
  `GET /api/v4/channels/dx/threads`, `PUT /api/v4/channels/dx/threads/{key}`
  (mode / reply policy; `null` restores the default),
  `GET|PUT /api/v4/channels/github` (status, token or vault entry, API base),
  `GET /api/v4/channels/github/threads`, `PUT /api/v4/channels/github/threads/{key}`
  (URL-encoded key),
  `GET /api/v4/channels/outbox[?channel=dx|github]` (all channels, or one),
  `POST /api/v4/channels/outbox/{id}/approve|discard`.
- Browser: Settings → 大象 (`frontend/src/features/settings/dx-channel.ts`,
  markup `frontend/src/settings-panels/dx.html`) and Settings → GitHub
  (`github-channel.ts`, `settings-panels/github.html`), facade `Api.channels`.
  Both tabs share one presentation (`channel-settings-view.ts`, styles
  `frontend/src/styles/settings/13-dx-channel.css`): a status card, the
  channel's own cards, a thread list, and the outbox of that channel only
  (`?channel=`). A tab re-reads its account while it is `starting`,
  `authorizing` or `disconnected`. The 大象 account card (`dx-account.ts`)
  says what each state needs (approve in 大象, reconnect, enter a MIS) and
  switches the login MIS inline; the command chat is picked from a searchable
  list. Its thread list (`dx-thread-list.ts`) is built for many groups: search
  by name or key, a mode filter with counts, a one-click mode and a reply
  policy per thread, and selection with bulk mode / reply changes, in place
  of per-row dropdowns. GitHub keeps a thread table with one column header row.

## Verification

`tests/channels/` covers dedup under replay, every mode and default, echo
suppression (including a forged marker), subscription passthrough with the
router observation, steer/queue/start delivery and failure replay, outbox
ordering, draft approval, unknown-result reconciliation and disclosure, tool
authority (router, subscriber, other conversations, genuine human turns),
auto-reply, supervisor restart policy, adapter catch-up with a fake bridge,
the HTTP surface against the contract, and the persistence of the `channel`
initiator. `tests/channels/test_dx_bridge_contract.py` runs the real
`bridge.mjs` against a stub engine (persistent handlers, request IDs, exit
codes, one-shot commands). `tests/channels/test_dx_login.py` covers login-MIS
session selection and the approval question; the supervisor suite covers
asynchronous confirmation, switching and superseded approvals;
`frontend/src/features/settings/dx-channel.test.ts` covers the 大象 tab. The
live probe is read-only (`whoami`, `sessions`,
`history`, a persistent listen; counts only):
`TOFU_DATA_DIR=<data> python -m lib.channels.dx.probe --owner 1 --listen-seconds 45`.
`tests/channels/test_github_{normalize,adapter,account}.py` run the real
GitHub client against a fake REST server (no network): normalization and
event IDs, `Last-Modified`/`304`, `X-Poll-Interval` as the cadence, catch-up
after a failed route, watched-thread ETags, rate limits and `needs_login`,
draft approval, echo suppression, reconciliation before resend, owner
isolation, token verification and custody, the supervisor and the HTTP
surface; `frontend/src/features/settings/github-channel.test.ts` covers the
Settings tab.
