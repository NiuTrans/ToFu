# Channels: external conversations as event sources and reply targets

Responsibility: channel accounts, normalized channel events and their media,
threads, the deterministic ingress policy, subscriptions (passthrough), delivery into
conversations, the outbox, the owner's assistant for other people's messages
(review authority, reply flow, approval and audit, C11), and the 大象 (Daxiang)
and GitHub adapters. Router
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
| **Channel media** | An image, file or voice original of an event: provider facts, then the owner-scoped attachment it was stored as, or why it was not (C9). |
| **Command chat** | The owner-selected 大象 thread whose owner-authored messages are human input to the Global Router. GitHub has none (C8). |
| **Notification robot** | An optional 大象 open-platform robot that posts a group command chat's outbox items, so they notify the owner (C6). |
| **Thread mode** | Deterministic ingress policy: `command`, `watch`, `mentions`, `ignore`. |
| **Reply policy** | What Tofu may send to a non-command thread (C11): `notify` (default), `draft`, `auto`, `never` (shown as Off). A contact's policy is its private chat's, a group's its thread's. |
| **Review** | A Global Router Turn whose input is a batch of other people's messages; it runs under the review ceiling and thread scope (C11). |
| **Approval code** | The short code of a draft (its item ID's first hex digits) that the owner's own words name to approve or discard it from a chat (C11). |
| **Owner note** | Owner-written guidance for replies Tofu drafts: one for the 大象 account (the owner's voice) and one per thread (C11). |
| **Subscription** | A thread → conversation passthrough edge created by a router. Many-to-many. |
| **Outbox item** | One durable outbound message with an idempotency key, a per-thread queue position and a state (`draft`, `queued`, `sending`, `unknown`, `sent`, `failed`, `discarded`). |
| **Outbox attachment** | A file sent with an item's text as its own 大象 message (image or file), or a preview copy of one (C10). |
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
`notify` (C11). Owner-authored messages outside the command chat are never commands (C11).
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

**Decision:** `channel_send(thread, text, reply_to?, idempotency_key, attachments?)` records an
outbox item (ID derived from owner, thread and key) before any network effect;
attachments follow C10.
Policy is decided once at enqueue from the thread's reply policy and the
request's authority (C11): command chat → `queued`; `auto` → `queued` for the
owner's own request or a review's reply to a message it reviews; `draft`, and
`notify` for an owner request, → waits for owner approval (Settings, or the
Global Router's `channel_outbox_approve` on a genuine human turn naming the
draft's approval code); `never` refuses and records nothing. A command-chat owner message that the
Turn command accepts immediately enqueues a receipt: started, steered into the
live Turn, or queued behind it. A queued command adds its started receipt when
its Attempt actually begins (`lib/channels/autoreply.py`). While that genuine
human command-chat Turn runs, the passive chat-Push observer mirrors each
canonical non-deliverable assistant text block once (tool-round
`assistantContent` and released continuation prose, never reasoning or raw
tool payloads), plus each changed `update_task_progress` board summary. The
observer does no model or network work. A Global Router Turn whose human input
came from a channel thread, including a genuine human command steered into a
nonhuman notice Turn, enqueues its final assistant answer to that thread with
idempotency key = the Turn ID, followed by an explicit completed, failed,
interrupted or aborted receipt from the existing task terminal-listener seam.
A successfully reviewed nonhuman notice may instead declare deliberate
silence (ROUTER.md R3): its audit note stays in Tofu, with no empty reply or
misleading completion receipt. A failed or interrupted review sends a
command-chat warning even when that notice had no reply origin. Receipts and
progress use the command chat; other channel events and nonhuman Turns never
gain these automatic messages. With a notification robot (C6), the command
chat's items go out under the robot instead of the owner's account. Agent-authored messages in threads
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
unchanged. Router-generated receipts, content, progress, replies and terminal
status are translated to the conversation's configured display language
immediately before the provider send. The first translated bytes are written
back to the durable item before the network call, so an unknown result or
restart retries exactly those bytes. A translator failure preserves the
original text and never blocks later items. Manually authored `channel_send`
text is not rewritten.

**Alternatives rejected:** sending inside the tool call (lost on crash,
duplicated on retry); impersonating the owner without disclosure; ordering by
timestamps (equal milliseconds reorder a thread); forwarding token deltas
(unbounded message spam and provisional retry text); translating in an
independent async queue (reorders replies and can retry different bytes).

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
  `sessions`, `create_group`, `download` (C9)), each answered with its request ID. It decodes
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
  Only a first session start (or one for a switched account) sets `starting`,
  and disabling sets `disabled`. A restart after an exit keeps reporting that
  exit's outcome until the new session reports `connected`; its detail names
  the launcher's public refusal (`error_code: error`) when the launcher refused
  to start the bridge, and the exit is logged once per distinct outcome.
  Each tick of this and of the GitHub supervisor (C8) also reports an
  unhealthy enabled account to the Router Watchdog: one fault signal per state
  since the last connection ([ROUTER.md](ROUTER.md) R7).

  **Problem:** each restart overwrote the exit's outcome with `starting` in
  the same tick, so a launcher that refused every 30 s for hours read as a
  perpetual start in Settings, left no log line, and the Watchdog, which reads
  the state after the tick, never saw an unhealthy one. **Decision:** the exit
  stays the reported state through the restart. **Alternatives rejected:**
  making every launcher refusal terminal (a transient one, such as a network
  failure, would then wait for the owner to re-enable); a separate restarting
  state or failure counter (a second state machine; the exit already says what
  is wrong).
- **Shutdown:** the process lifecycle opens the supervisor before starting the
  scheduler and stops it after the scheduler joins. Stop closes admission for
  ticks, configuration notifications and identity completions before joining
  adapters and all supervisor-owned threads. A timed-out or failed stop keeps
  ownership for the next stop attempt; a replacement session and a new hosting
  lifecycle cannot start before that ownership is released. Production shutdown
  keeps storage live while this join is incomplete. Pending identity probes keep
  their existing provider deadline; their late result cannot write account state
  or start a session after Stop. Ingress wakes immediately on Stop and leaves
  queued events for catch-up from the committed watermarks. An in-flight ingest
  or outbox operation must finish before the final frontier checkpoint and a
  successful stop receipt.

  **Problem:** clearing the adapter registry before joining lost cleanup debt,
  and untracked identity/configuration callbacks could start a new bridge after
  shutdown had passed the channel owner. Ingest failure diagnostics then tried
  the same unavailable storage and skipped bridge termination. **Decision:**
  retain physical worker ownership, fence new work under the supervisor lock,
  and always terminate a failed ingest bridge even if its error status cannot
  be persisted. The original failure remains available through `outcome()` and
  logs; no failed event advances its watermark. **Alternatives rejected:**
  swallowing storage errors alone leaves the producer alive; draining the queue
  after Stop prolongs shutdown unnecessarily; forgetting a timed-out join falsely
  permits storage release. No retry timer or capacity limit is added.
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
- **Notification robot** (`lib/channels/dx/robot.py`, facts in the contract's
  `x-dxRobot` and `x-providerLimits`). **Problem:** the outbox posted as the
  owner, and 大象 never notifies anyone of their own messages, so receipts
  and answers in the command chat arrived silently. **Decision:** an optional
  open-platform robot posts the outbox items of a *group* command chat; inbound
  stays on the owner's bridge, other threads keep the owner's account, and no
  robot means today's behaviour. The robot follows the official documents:
  a client_secret_jwt `client_credentials` token with scope `client_id:xm-xai`
  (应用认证接入指引 §3.2.1/§3.2.4), reused until its `expires_in`, one renewal
  when the API answers 20004/20005; `sendGroupMsgByRobot` with a markdown
  `text` message (大象消息 §2.2, 消息extension); text split into parts of at
  most 6000 UTF-8 bytes, ending at a line break where possible; posts paced at
  the documented 5 per second. The Authorization scheme is the token's
  `token_type` ("Bearer"). Status codes (错误码) decide the outcome: a refusal
  before anything was posted (application, permission, membership, a missing
  or rejected secret, an unreachable token endpoint) sends the item as the
  owner instead and records the refusal on the account (`robot.error`, shown
  in Settings, cleared by the next robot post); the rate limit (10003) and
  server failures or lost responses leave the item `unknown`, so it is
  reconciled and sent again under the robot. A group send has no documented
  quote type, so robot posts never quote. Echoes: robot posts reach the
  owner's connection from another sender without a marker, so the item
  records its exact part texts (`robotTexts`) before posting and each
  returned message ID is remembered at once; in the robot thread an event
  whose ID was remembered, or whose text is a part of an active robot item,
  is an echo that confirms nothing (the API answer settles the item), and
  reconciliation finds a robot item when all its parts are in the history.
  The robot is a separate sender, so its posts notify the owner like any
  member's message (unless the owner mutes the group). It does not reduce
  the owner's IM token requests (`/login/im_token/generate` per bridge or
  launcher process): inbound still needs the bridge, and robot inbound would
  need an open-platform event callback, a Thrift service with an APPKEY.
  Attachments do not go through the robot (C10): its documented `image`/`file`
  bodies carry URLs on the caller's own storage (IM stores no files for
  open-platform sends), so files go as native messages of the owner's session
  and the robot posts the text after them. **Alternatives rejected:** @-mentioning the owner
  (the documented @ prompt targets other members; nothing documents a self-@
  notification); a private chat with the owner (still the owner's own
  message); routing robot posts to the robot's own private chat (splits the
  conversation from the command chat); an undocumented marker in the robot's
  extension (no documented preservation).

  **Owner setup** (only the owner can do these):
  1. In 企平应用开放平台 (https://open.internal.example.com/developer/app/apply) create an
     application, keeping the default client authentication
     (client_secret_jwt); its 应用凭证 give the client_id and client secret.
     Request the 大象 办公 resource there, which creates the 办公应用.
  2. In 办公开发者后台 (https://xopen.internal.example.com/home): 应用功能 → 机器人, enable;
     权限管理, open 「发送单聊、群组消息」; 应用发布, add yourself to the visible
     range and submit for approval (it takes effect about 10 minutes after).
  3. In the 大象 desktop client, open the command chat group → 群信息 → 群机器人
     → 管理, search the robot's exact name and add it.
  4. In Tofu, Settings → 大象 → 通知机器人: enter the client_id and paste the
     secret, or choose an existing vault entry, and save (the card writes
     `PUT /api/v4/channels/dx` `robot: {clientId, secret}` or
     `robot: {clientId, credentialName}`). The secret is checked with a token
     request, then kept only in the owner's reserved credential vault entry
     `channel.u<owner>.dx.robot`; Remove (`robot: null`) deletes it.

  The robot card shows these four steps (`settings.dx.robot.step1`–`step4`);
  change both together. **Problem:** the owner needs the steps where the
  robot is configured, but Tofu serves no repository documents and the
  public repository has no copy of this file. **Decision:** the card lists
  the steps with their two consoles and names this section as their source.
  **Alternatives rejected:** a link to the GitHub copy (missing until it is
  published, then trailing this checkout); a route serving `docs/` (a
  document server for one card).

### C7. Authority

| Source | Authority |
|---|---|
| Owner in the command chat | Human input (sender = bound owner account, thread = command chat) |
| Owner in any other thread | Nonhuman context |
| Owner's own GitHub comments | Nonhuman evidence: a candidate in `watch` threads, context otherwise; never human input (C8) |
| Other people | Nonhuman evidence (initiator `channel`); a Router Turn reviewing it runs under the review ceiling and thread scope (C11) |
| Outbox echoes | Never input |
| Router or worker text | Nonhuman |

Tool authority is decided once in `lib/channels/tools.py`: every channel tool
requires the owner's Global Router conversation (resolved through
`lib.router.binding.global_router`), except `channel_read`, which a subscriber
of the requested thread may also use; `channel_outbox_approve`,
`channel_configure_thread`, `channel_subscribe` and `channel_unsubscribe`
(the owner's decisions: what Tofu sends and who receives a thread)
additionally require the executing user attempt
(user principal, attended, the task's own assistant Turn) whose human input
passes `require_human_turn`, and refuse when a `channel-event-` batch was
steered into that Turn (persisted or already consumed): a third-party message
merged into the owner's request never carries the owner's authority. Owner
command-chat messages steered in (`channel-owner-`) keep it. A chat decision
on a draft also needs its approval code in the owner's own words, and a
review is confined to its threads (C11). The catalog projection (`ToolSpec 'channels'`)
mirrors this and the Global Router's coordinator ceiling admits the names via
`ROUTER_TOOLS` (a review's ceiling via `x-assistant.reviewTools`, C11). HTTP account changes need the `admin` scope; reads and owner
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
  stored lowercase. Default mode `mentions`, default reply policy `notify` (C11).
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

### C9. Media: images, files and voice

**Problem:** 大象 is a chat app with files: the owner sends screenshots, decks
and voice notes from the phone, and colleagues share them in groups. The
adapter flattened every non-text message to `[type:N]`, so the receiving
conversation saw neither the content nor what kind of message arrived.

**Decision:**

- **Types.** The adapter reads each message by its wire type
  (`x-dx.messageTypes`: the official enum of
  [消息类型与消息体字段](https://km.internal.example.com/collabpage/2648372848); numbers
  that page omits follow the Web SDK's `MESSAGE_TYPE` enum). Text and quote
  replies keep their own text. Rich text (general subtype 100, a JSON string on
  the IM wire and Base64 JSON on the send API) renders its nodes: text, links
  with their URL, mentions, emoji, lists and `[image]` for inline images. Every
  other type becomes a readable description of its label and metadata:
  `[file: deck.pptx]`, `[voice message: 3 s]`, `[video: 4 s, 288x352, 286.5 KB]`,
  `[location: …]`, `[card: <summary>]`, `[message type 99]`.
- **Media facts.** Images (type 4 and rich-text images), files (8) and voice
  (2, AMR only) also become `ChannelMedia`: kind, a sanitized name (one path
  component: no directories, control characters or leading dots), MIME type,
  size, duration, and a transient provider locator (URL and file token) that is
  never recorded, rendered or logged.
- **Resolution, once.** Ingress resolves the media of an event that is about
  to be routed (never of dropped messages or ignored threads) after its CAS
  record and before routing. Each original is downloaded through the adapter's
  media port and stored as an owner-scoped retained attachment by
  `lib.media_attachments.ingest_received_file`, the chat-upload pipeline:
  indexed when the knowledge parser reads it, otherwise kept verbatim; never
  executed or opened. A voice message is transcribed by
  `lib.video_analysis.transcribe_track` (ffmpeg, then the owner's
  speech-recognition route, Settings → 语音识别) beside its audio; without a
  route the audio is attached and the turn says transcription is unavailable.
  The outcome is recorded on the event (`media`), so the replay of an unrouted
  event reuses it. A failed download or store is an outcome shown in the turn
  (`failed` with its reason), never a silent drop; a lost bridge raises and
  replays (C1).
- **Download.** Only through the session's own bridge (`download`: no new
  process, login or token mint), only for URLs under the Web SDK's own file
  hosts (`x-dx.fileUrlPrefixes`, `http.common.ts` `DX_FILE_URL.online`), with
  the SDK's file headers (`HTTPCommon.getFileBlob`: the message's file token
  as `X-Xmftk` plus the IM session identity). Redirects are followed by hand
  (the WHATWG Fetch limit of 20) so the headers never reach the signed storage
  location; the stream goes to a private scratch file the adapter chose. A web
  page returned instead of the file (`text/html`) is a failure: the session may
  need a new login. No size cap is added: 大象's own limits bound what arrives
  and the attachment pipeline has none.
- **Delivery.** Stored originals ride the Turn as `attachments`
  (`TurnMediaAttachment`), so model projection shows images and document text
  and the UI shows them, and the text names each by `att_media_<id>`, because a
  steered input reaches the running model as text only (`read_files` and
  `inspect_image` open it). A nonhuman batch lists `<channel-media kind name
  mime_type size_bytes duration_s status attachment_ref detail
  transcript_status transcript_detail>` (the transcript as its content) inside
  the sender's `<channel-event>`: third-party media stay nonhuman evidence like
  their text (C7). An owner command keeps its verbatim text and adds one plain
  line per item (`[file attached: deck.pptx — att_media_…]`, `[transcript] …`).

**Alternatives rejected:** downloading in the adapter before ingress (fetches
the media of dropped messages and ignored groups into the owner's store); the
open platform's temporary-URL exchange (`changeDownloadTempUrl`: a separate
permission and identity, while the session already has the SDK's file access);
the skill's attachment CLI per file (a new process and IM-token mint per
download); the legacy `/api/images` directory (not owner-scoped, a second
store); a channel media store, size caps or type allow-lists (constraints 3–5).

**Limits:** catch-up history rows carry no file token and no voice duration,
and an image only as its preview URL; videos and stickers are described, not
downloaded; 文枢-controlled files outside the SDK's file hosts are reported as
not downloaded. A large download or a transcription delays the owner's later
events, because ingest is sequential per owner (C1).

### C10. Outbound attachments (大象)

**Problem:** the owner reads Tofu on the phone, and decks, reports and charts
arrived only as text or as server paths a phone cannot open. 大象 is a chat
app with files, but the open-platform robot sends files only by URL ("IM不提供
文件存储能力", 消息类型与消息体字段,
https://km.internal.example.com/collabpage/2648372848), and the owner's own messages
never notify the owner (C6).

**Decision:** files go as native 大象 messages through the owner's session;
the notification robot keeps posting the text.

- **Admission.** `channel_send(..., attachments=[{path}])` admits every file
  before anything is recorded (`lib/channels/attachments.py`): a regular file
  in the sending conversation's verified workspace roots (relative to the
  primary root, `rootname:path`, or absolute inside a root), read without
  following any symlink (`lib.artifacts.file_snapshot`), one of that
  conversation's own published artifacts by its delivery URL
  (`/api/artifacts/<id>/download`, `/api/response-assets/<id>`), or a slide
  deck it produced by the download URL the slide tools return
  (`/api/v4/slides/<task_id>/file`; the job manifest names the owner and
  conversation, and the newest export in its deck directory is sent).
  Traversal, symlinks, absolute paths outside the roots, other
  conversations' artifacts and decks, undocumented types and oversize files
  are refused with a code
  and what to do instead; nothing is dropped silently. The limits are 大象's
  documented chat upload limits (`x-dxAttachments`; 服务端聊天文件上传接口,
  https://km.internal.example.com/collabpage/684760118): images jpg, jpeg, png, gif,
  bmp; files txt, csv, zip, rar, pdf, doc, docx, xls, xlsx, ppt, pptx; each
  under 30 MiB, checked before the file is read. GitHub threads take none
  (`x-channelTraits.attachments`).
- **Records, not bytes.** An attachment record keeps name, MIME type, size,
  sha256, kind (`image` | `file`) and its source. At send time the original
  is read again and a changed file is not sent (`attachment_changed`). The
  idempotency key covers the attachments (names and digests). Reply policy,
  the disclosure label and draft approval apply to the item as a whole.
- **Transport.** The bridge's `send_image` / `send_attachment` hand one file
  to the engine, which uploads it to 大象's own storage and sends an image or
  file message over the owner's IM session with the part marker
  `<item>/<n>` (`x-dxAttachments.partMarker`) in `extension.tofuOutbox`.
  In the robot's thread the files go first and the robot's text last, so the
  notification arrives when everything is in the chat; elsewhere the text
  goes first, so the `[Tofu 代发]` label introduces the files.
- **Confirmation.** The skill documents that 大象 can acknowledge a file
  message it never delivers. An item with attachments is a list of parts with
  their own state (`textPart` and each attachment), so a resumed item never
  sends a confirmed part again. The text is sent once accepted, as before. An
  accepted attachment (`acked`) is `sent` only when the thread shows it: its
  echo on the owner's connection or the thread history (marker, client UUID
  or message ID). The history of accepted parts is read from the oldest one's
  message ID on (`history_from`; 大象 IDs are time-ordered snowflakes), not
  from the local attempt time, so clock skew cannot hide a delivered file. An
  accepted part that is absent while that history already holds a later
  message is `failed` (`not_delivered`) and never sent again; a
  part whose send outcome is unknown is looked up by its marker and sent again
  only when absent, like an unknown text item. While only accepted parts
  await confirmation the item is `unknown` (`awaiting_confirmation`) but does
  not hold back its thread: those parts are never resent, so they cannot
  reorder it. The item is `sent` when every part is, `failed`
  (`attachment_failed`, or the text's code) when any part failed; a refused
  text stops the files it should have introduced (`not_sent`). Failed items
  and blocking unknown items become Router Watchdog signals (R7).
- **Previews for the phone.** Images go as image messages and preview
  inline; documents go as file messages, which 大象's clients open in their
  Office/PDF preview. A pptx, docx or xlsx is meant to be followed by a PDF
  copy, and a pptx also by one PNG per slide, each recorded with
  `renditionOf` (its original's index). The only converter in this repository
  and environment is Tofu's slide renderer (`lib/channels/renditions.py`): a
  pptx exported from a Tofu deck, i.e. beside its `deck.pptd` and at least as
  new as it and its page files (every Tofu edit re-exports), is rendered fresh
  to page PNGs and a PDF of them (PyMuPDF). A deck changed after its export
  gets no copy (it would not match the file); docx, xlsx and other pptx files
  have no converter. Then the original goes alone and the text says why no
  preview copy was made. Copies are made at send time in a scratch directory
  removed after the attempt, never uploaded elsewhere; a resumed item makes
  them again for parts still to send.
- **Final answers.** A Global Router answer to a command-chat message (C5)
  attaches the files of its own workspace and the slide decks it produced that
  it links with Markdown links or images (outside code), through the same
  admission. Web URLs, artifact links (their decoding runs worker processes,
  not for the task's terminal seam) and anything else are left alone; a linked
  file 大象 does not accept is named in a note under the answer; attachment
  trouble never holds back the answer.
- **Settings.** Each outbox item lists its attachments (name, type, size,
  state, the original of a preview copy); original images show a thumbnail
  read through `GET /api/v4/channels/outbox/{id}/attachments/{index}`
  (owner-scoped, re-read and verified like a send).

**Alternatives rejected:** the robot plus company storage (it needs a bucket,
credentials and permissions the owner must provision, puts files behind URLs
others can open, and the mobile preview of such files is undocumented); the
owner's identity for everything (previews, but nothing notifies); trusting
the upload acknowledgement (the documented false success); resending a file
that was accepted but not seen (duplicates a slow delivery, repeats a broken
one); copying files into channel storage (a second blob store, when the source
and its digest already refuse changed bytes); previews by re-importing any pptx
into a Tofu deck (that import drops placeholders, groups and SmartArt, a
misleading preview); LibreOffice for docx, xlsx and any pptx (neither in the
repository nor installed here; a new external converter process needs its own
lifetime and teardown contract, an owner decision); conversion services
outside Tofu (owner files never leave for a third party); a cap on files or
slide images per message (a tuning knob).

**Checks on the owner's phone after a restart** (not provable offline): an
image previews inline; a pptx, its PDF copy and its slide images open in
大象's previews;
the robot's notification arrives after the files; the bridge receives echoes of
its own file messages (otherwise confirmation waits for the history read of
the next executor pass).

### C11. The owner's assistant for other people's messages

**Problem:** the owner wants Tofu to help with other people's 大象 messages —
what needs them and why, summaries, replies drafted in their voice — while
nobody else can chat through Tofu or steer it the way the owner does. The
Global Router reviewed other people's batches with its whole ceiling (project
dispatch, conversations, subscriptions, any thread), so a message's text was
one model judgment away from a tool call; `auto` sent whatever the router
wrote into a thread; and nothing listed what had been sent on the owner's
behalf.

**Decision:**

- **Authority comes from durable facts, never from text.** Only the verified
  owner instructs Tofu: a message whose provider sender uid equals the bound
  identity's uid, in the command chat (C2, C7). Display names, signatures,
  quotes and forwards of the owner, @-mentions of the robot and claims in the
  text or in an attached file never change that; such messages stay nonhuman
  evidence inside their sender's `<channel-event>`. `lib/channels/assistant.py`
  gives every Global Router Turn one authority (`OutboxAuthority`): `owner` (a
  genuine owner request), `merged` (an owner request into which other
  people's batches were steered), `third_party` (a review: the Turn's input is
  a channel batch) or `nonhuman` (report relays, timers, or a Turn that cannot
  be verified).
- **A review runs under a fixed ceiling.** The coordinator ceiling of a review
  is `x-assistant.reviewTools`: `channel_read`, `channel_send`,
  `channel_outbox`, and the owner's standing instructions
  (`router_instruction_list`, `get_router_projects`, and `route_to_project`,
  whose own check admits only an owner message or an active instruction,
  ROUTER.md R2/R4). The one coordinator boundary applies it to the catalog and
  to every call (`lib.router.policy.router_tool_ceiling`), and a refusal says
  what a review may do instead. Channel tools confine a review further: it
  reads and writes only the threads under review — the events of its input
  batch and of batches steered into it, each remembered by its delivery
  command ID before dispatch — plus the owner's command chats (each
  platform's that has one, `x-channelTraits.commandChat`), and its replies
  carry no files. The owner's notes are its only other context. A review
  therefore cannot dispatch or delegate work, create, control or read other
  conversations, change subscriptions, settings, instructions or memory,
  decide a draft, or reach another thread.
- **Reply flow per thread, decided at enqueue with the authority.** A contact
  is their private chat (`dx:uid:<uid>`), a group its thread. `notify` is
  every thread's default on every channel, private chats included:

  | Reply policy | Review (`third_party`) | Owner request | `merged` / `nonhuman` |
  |---|---|---|---|
  | `notify` (default) | refused: tell the owner in the command chat | draft | draft |
  | `draft` | draft | draft | draft |
  | `auto` | queued only as a reply (`reply_to`) to a reviewed message and without `needs_approval`; otherwise a draft | queued | draft |
  | `never` (Off) | refused | refused | refused |

  The command chat always sends. Every item for another thread carries the
  `[Tofu 代发]` label and keeps C5's ordering and reconciliation.
- **Approval** happens in Settings (the outbox card) or from a chat. A new
  draft is announced in its own platform's command chat, else in 大象's, or
  only in Settings while neither is live (origin `draft_notice`, idempotency
  key `draft-notice:<item>`, not translated, so the owner reads the exact
  text that would go out and the files it carries) with its approval code
  (`approvalCode`: the item ID's first `x-assistant.approvalCodeLength` hex
  digits). `channel_outbox_approve` needs a genuine owner request with no
  batch merged into it (C7) whose owner's own words — the human input or an
  owner message steered into the reply — name that code (「批准 7F3A2C」).
  Text the router read or was sent cannot decide a draft.
- **Owner notes.** `assistantNote` on the 大象 account (who the owner is,
  their voice) and a `note` per thread (that contact or group; for `auto`, what
  Tofu may say without approval) are written only from Settings (`PUT
  /api/v4/channels/dx` `assistantNote`, `PUT …/threads/{key}` `note`); no model
  tool writes one. A batch for the Global Router carries them as escaped
  `<owner-note>` elements after a fixed review instruction
  (`delivery.REVIEW_GUIDANCE`); subscribers never see them.
- **Audit.** Every item recorded for a thread other than the command chat is
  listed, whatever becomes of it, in creation order
  (`GET /api/v4/channels/audit?channel=&before=`, newest first, pages of
  `x-limits.readPageEvents`). Each item keeps the `authority` it was recorded
  with and its latest `decision`: `queued` by `owner_request` or `auto_reply`,
  `approved` or `discarded` by `owner_settings` or `owner_message` (with the
  Turn ID).
- **Others cannot chat through Tofu: silence.** A non-owner's message is never
  handled as a request — written to the owner, @-mentioning the robot in a
  group, or posted in the command chat — and Tofu writes to that person only
  when the thread's policy allows a reply, as the owner's labelled assistant.
  The notification robot has no inbound (C6): a private chat with it reaches
  no one. A fixed notice ("this is X's assistant…") was rejected: it would be
  an unapproved message on the owner's behalf in every thread, it tells a
  prober an agent is listening, and in groups it is noise.
- **The owner's own messages in other chats stay context.** There the owner
  talks to other people, and the same account carries Tofu's labelled sends:
  an "OK, do it" said to a colleague, a sentence someone coaxed from the owner,
  or an echo that escaped suppression would otherwise command Tofu.
  Instructions come only from the command chat and Tofu itself.

**Alternatives rejected:** a separate tool-less drafting call in the channel
layer (a second model invocation path beside the router Turn, constraint 4,
and the router still triages); prompt rules alone (the model would be the only
guard); never steering other people's batches into a running owner Turn (the
dispatcher cannot choose atomically by the running Turn's authority, and
queueing every batch multiplies paid Turns; owner decisions already refuse
merged Turns and their sends become drafts); approval commands parsed at
ingress (C2 rejects keyword prefilters; the router plus the code in the
owner's own words is exact); per-sender policies across groups (a group reply
is read by the whole group, so the group's policy governs it); auto-reply
without binding to a reviewed message (a message in one thread could make
Tofu "reply" in another `auto` thread).

**Limits:** within `auto` the owner's note bounds what the model writes; it is
not a filter. A batch merged into an owner Turn can still influence the model
within that request: its owner decisions refuse, its sends become drafts, and
delegation and forwarding carry only the owner's verbatim words (ROUTER.md
R2). A review's standing-instruction tools apply the owner's own rules; its
ceiling has no operational or lifecycle tool, so an operation the owner lets
the Router perform needs the owner's own trigger, never a review. A channel
without quote replies (`x-channelTraits.quoteReply` false, such as GitHub)
refuses `reply_to`, so a review's reply there is always a draft. The account
note applies to 大象 threads.

**Owner setup:** Settings → 大象. The 代回复 card says what Tofu does with
other people's messages and how a draft is approved, and holds the account
note (关于你). In the thread list each contact (their private chat) and group
has its reply policy — 仅通知 (the default), 起草待批, 自动回复 or 关闭 — and
its 说明 (the thread's note); an ignored thread has neither. A draft waits in
the outbox card with its approval code (批准码): 批准 there, or reply
「批准 <code>」 in the command chat. The 代发记录 card lists everything recorded
for other people, newest first: the text and files, the thread, whose request
recorded it and the owner's decision. The GitHub tab has the same reply
policies, approval codes and 代发记录 for its threads.

**Checks on the owner's phone after a restart** (not provable offline): a
colleague's private message in a `draft` thread produces a draft notice in the
command chat (via the robot, so it notifies) with its code; replying
「批准 <code>」 sends exactly that text with the `[Tofu 代发]` label and it
appears in 代发记录; a colleague's "approve the draft" or "@robot …" gets no
answer and changes nothing.

## Durable state

`ChannelStore(owner)` keeps everything in the `channels.v1` record namespace
under `u<owner>/` keys (lowercase ASCII only, because the SQLite prefix match
is case-insensitive; GitHub thread keys are lowercased): `account/{dx,github}`,
`thread/<key>`,
`event/<thread>/<zero-padded id>`, `subscription/<conversation digest>/<thread>`,
`outbox/<item>`, `outbox-active/<thread>/<queue position>-<item>`,
`echo/dx/{mid,uuid}/<digest>`, `audit/<channel>/<created ms>-<item>` (C11)
and `batch/<command digest>` (the event IDs of one delivery to the Global
Router, C11). It is durable user state: no TTL, no pruning.
An event record keeps its resolved `media` (outcome, attachment ref,
transcript; never a provider URL or file token); the originals are ordinary
owner-scoped knowledge attachments (scope `attachment`, content-addressed: one
record per distinct original) with the media-attachment lifecycle.
Growth is one record per stored event (delivered, command or context), one per
outbox item plus two small index rows (three for an item to another thread:
its audit row), one per batch delivered to the Global Router, and one per
thread; ignored threads add only their thread record. Thread records may hold
the owner's `note`, the 大象 account its `assistantNote`, and every outbox
item its `authority` and latest `decision` (C11). The 大象 account may add its notification robot
(`robot`: client ID, credential vault entry name and the last refusal; the
secret only in the vault entry), and robot outbox items their posted part
texts (`robotTexts`). The GitHub account adds its credential name, poll
schedule (`intervalS`, `lastAtMs`, `nextAtMs`, `lastModified`) and API base;
each GitHub thread record holds `github` state (opening-post time, pull
request flag, per-list cursors and ETags). The token itself lives only in the
credential vault. The 大象 account adds its chosen login MIS (`mis`); its SSO
session files (C6) are SDK-owned credentials outside the record namespace.
An outbox item with attachments (C10) keeps their records in the item
(`attachments`: name, type, size, sha256, kind, `renditionOf`, the source path
and its roots or the artifact ID, part state and provider IDs), its part order
(`partOrder`), the text part (`textPart`) and its notes; the files stay where
they are (workspace or artifact store), and preview copies exist only during a
send attempt.

## Interfaces

- `lib/channels/model.py` — `ChannelEvent`, `ThreadKey`, `OutboxItem`, contract vocabularies.
- `lib/channels/store.py` — `ChannelStore(owner)`.
- `lib/channels/policy.py` — `effective_mode`, `effective_reply_policy`, `classify`.
- `lib/channels/ingress.py` — `ingest(owner, event, media=)`, `ingest_batch(owner, events, media=)`.
- `lib/channels/media.py` — `safe_media_name`, `MediaPort`, `resolve_event_media` (C9);
  originals are stored by `lib.media_attachments.ingest_received_file`.
- `lib/channels/delivery.py` — `deliver`, `deliver_command`, `observe_router`, `global_router_binding`.
- `lib/channels/outbox.py` — `enqueue`, `approve`, `discard`, `execute`, `confirm_echo`, `kick`.
- `lib/channels/attachments.py` — attachment admission (`from_tool`, `for_final_answer` for
  the files a router answer links, `admit`), verified bytes at send time (`read_original`);
  `renditions.py` — preview copies (C10).
- `lib/channels/service.py` — owner commands/views shared by HTTP and tools.
- `lib/channels/tools.py` — `channel_threads`, `channel_read`, `channel_send`,
  `channel_configure_thread`, `channel_subscribe`, `channel_unsubscribe`,
  `channel_outbox`, `channel_outbox_approve` (handler `lib/tasks_pkg/handlers/channels.py`).
- `lib/channels/autoreply.py` — router answer → command-chat thread.
- `lib/channels/assistant.py` — Turn authority, the review ceiling and scope, approval codes,
  draft notices and owner notes (C11); `lib.router.policy.router_tool_ceiling` asks it.
- `lib/channels/dx/` — `bridge.mjs`, `adapter.py` (normalization, catch-up, sender port,
  media port `download_media`), `login.py`, `supervisor.py`, `probe.py`,
  `robot.py` (notification robot: token, posts, configuration).
- `lib/channels/github/` — `client.py` (REST transport, conditional requests,
  paging, rate limits), `normalize.py`, `adapter.py` (poll worker, sender
  port), `account.py` (Settings commands, token custody), `supervisor.py`.
- HTTP (`routes/api_v1/channels.py`, served under `/api/v4`; each body
  admits exactly the properties of its contract DTO):
  `GET|PUT /api/v4/channels/dx` (config and status; `PUT` enables/disables,
  sets the login `mis`, the command chat or the notification `robot` and
  returns without waiting for a login),
  `POST /api/v4/channels/dx/command-chat` (create `Tofu Router`),
  `GET /api/v4/channels/dx/threads`, `PUT /api/v4/channels/dx/threads/{key}`
  (mode / reply policy, `null` restores the default; the owner `note`),
  `GET|PUT /api/v4/channels/github` (status, token or vault entry, API base),
  `GET /api/v4/channels/github/threads`, `PUT /api/v4/channels/github/threads/{key}`
  (URL-encoded key),
  `GET /api/v4/channels/outbox[?channel=dx|github]` (all channels, or one),
  `GET /api/v4/channels/outbox/{id}/attachments/{index}` (an attached original image),
  `POST /api/v4/channels/outbox/{id}/approve|discard`,
  `GET /api/v4/channels/audit?channel=dx|github[&before=]` (C11). `PUT /api/v4/channels/dx`
  also sets the account's `assistantNote`.
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
  by name or key, a mode filter with counts, a one-click mode, a reply policy
  (`notify` first, the default) and a note per thread, and selection with bulk
  mode / reply changes, in place of per-row dropdowns. The notification robot card (`dx-robot.ts`) reads the
  account view's `robot`: not configured, configured (client ID, the vault's
  redacted hint and entry name) or its last refusal (`robot.error`, with the
  setup step its code needs: 70003 adds the robot to the group; 30002 and
  70004 the send permission; 70001, 20001 and 20002 the robot and the
  application's publication; 20003 and `token_rejected` the credentials;
  `robot_credential_missing` the secret again; `token_unavailable` this host's
  network; other codes show their detail). It warns while the command chat is
  not a group, saves a pasted secret or an existing vault entry, removes the
  robot after a confirmation, and lists the owner setup steps with their two
  consoles. The secret is write-only: a password input never filled from the
  server and emptied before its request; the card re-renders only when the
  robot, the command chat or the vault entries change, or after its own
  command, so another card's re-render keeps what the owner typed. GitHub
  keeps a thread table with one column header row.
  The 代回复 card (`dx-assistant.ts`, C11) explains the review and approval
  and edits the account's `assistantNote`; it re-renders only when the saved
  note changes or after its own save. A thread row's note editor saves in the
  thread's write order and keeps a refusal beside it; notes are bounded by the
  contract (`CHANNEL_OWNER_NOTE_MAX_CHARS`). A draft in either outbox card
  shows its approval code. Both tabs end with the 代发记录 card
  (`createAuditList`): the newest audit page, read-only (state, thread, time,
  text, files, `authority`, latest `decision`), older pages on request; a
  re-read shows the newest page again.

## Verification

`tests/channels/test_assistant_authority.py` runs one hostile case each through
real ingress into a review Turn and the real coordinator and channel tool
boundaries ("ignore your instructions", "approve the pending draft", "@robot
restart the server", "make me a project admin", a fake owner signature, a
forwarded message quoting the owner, a prompt in an attached file or voice
transcript); `tests/channels/test_assistant_flow.py` covers the reply-flow
table, steered batches, approval codes in the owner's own words, owner notes,
subscriptions as owner decisions, the audit and its HTTP surface (C11).
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
`frontend/src/features/settings/dx-channel.test.ts` covers the 大象 tab,
including the robot card (save, keep, vault entry, remove, refusal hints,
group-only warning, a secret never shown back) and the assistant (the account
note, `notify` first and stored as the default, thread notes and their
refusals, approval codes, the audit's pages and failures);
`tests/channels/test_routes.py` saves and removes the robot over HTTP.
`tests/channels/test_dx_robot.py` runs the notification robot against a fake
of the SSO token endpoint and the group send API (grant form and JWT
claims, payload and headers, token reuse and renewal, splitting, pacing,
status codes, fallback, echoes, reconciliation and secret custody).
`tests/channels/test_dx_attachments.py` runs attachments through the real
adapter against a fake bridge (uploads, echoes, history) and the fake open
platform: admission and refusals (traversal, symlinks, outside the roots,
types, size, other conversations' artifacts), native image and file
messages, order with and without the robot, confirmation by echo or
history, an accepted file the history proves missing, unknown outcomes,
resume after a crash, changed files, draft approval with thumbnails, previews
with a stub slide renderer (fresh, changed after export, or none), slide
decks of this and other conversations, and final-answer
scope. The
live probe is read-only (`whoami`, `sessions`,
`history`, a persistent listen; counts only):
`TOFU_DATA_DIR=<data> python -m lib.channels.dx.probe --owner 1 --listen-seconds 45`.
Media (C9): `tests/channels/test_dx_media.py` covers every type's text, media
facts and name sanitizing from SDK packets and compacted history rows, ingress
with a fake media port (image, file, voice with and without speech
recognition, failed download, dropped and ignored messages, replay without a
second download) and the adapter's port; the bridge contract downloads from a
fake file host (file headers, credential-free redirect, refusals);
`tests/channels/test_channel_media_store.py` stores originals in a real
knowledge Sidecar; `tests/router/test_router_channel_integration.py` persists
the attachments in the real turn authority.
`tests/channels/test_github_{normalize,adapter,account}.py` run the real
GitHub client against a fake REST server (no network): normalization and
event IDs, `Last-Modified`/`304`, `X-Poll-Interval` as the cadence, catch-up
after a failed route, watched-thread ETags, rate limits and `needs_login`,
draft approval, echo suppression, reconciliation before resend, owner
isolation, token verification and custody, the supervisor and the HTTP
surface; `frontend/src/features/settings/github-channel.test.ts` covers the
Settings tab.
