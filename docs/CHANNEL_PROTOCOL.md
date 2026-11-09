# Channel protocol: one contract between Tofu and every chat app

The channel protocol is the boundary between Tofu's channel core
(`lib/channels/`, behaviour in [`CHANNELS.md`](CHANNELS.md) C1–C12) and each chat
app's adapter (大象, GitHub, 企业微信, QQ, 微信, then Feishu and every later app).
It exists so that a new chat app is one adapter written to one contract, and
so that authority, the outbox, retries, fallbacks and degradation are decided
once, in the core, for every app.

- **Machine-readable authority:**
  [`contracts/channel_protocol_v1.schema.json`](../contracts/channel_protocol_v1.schema.json):
  the envelopes and objects (`$defs`), methods (`x-methods`), error codes
  (`x-errors`), health codes with their state, remedy and retry owner
  (`x-healthCodes`, `x-remedies`), the Watchdog mapping, thread-key and
  external-ID codecs, identities, self-echo and attachment-source rules,
  the legacy vocabulary maps (`x-legacy`) and the in-repository adapters'
  capability descriptors (`x-adapters`, whose platform numbers are pointers
  into `contracts/channels_v1.schema.json`). This document explains it and
  records the decisions; the schema wins where they differ.
- **Code:** `lib/channels/protocol/`: `contract` (vocabulary, validation,
  negotiation, descriptors), `codec` (protocol objects to and from today's
  keys, IDs and outbox vocabulary), `adapter` (the adapter base), `host`
  (the core's endpoint of one adapter session), `stdio` (the JSON-RPC 2.0
  binding for adapters in another process or runtime).
- **Executable specification:** `tests/channels/conformance/` runs every
  protocol case against every adapter (Conformance below).
- **Status:** stage 2. The contract, SDK and conformance suite exist, and the
  core closes the gaps the reference adapter exposed (migration step 1):
  every `reference` and `reference-stdio` case passes. The adapters still
  talk to the core through their legacy ports and run behind a
  compatibility shim in the suite. Migration below moves them one by one.

## Vocabulary

- **Core:** the channel layer that owns policy and state: ingress, policy,
  delivery, the outbox, approvals, subscriptions, account and health state,
  supervisors (`lib/channels/` minus the adapters).
- **Adapter:** the translation between one chat app and the protocol, with
  the platform session it runs. It owns no policy and no durable state of its
  own beyond the opaque checkpoint the core stores for it.
- **Binding:** how protocol objects travel: *in-process* (a Python adapter
  object called with the same validated objects) or *stdio* (JSON-RPC 2.0,
  one JSON object per line, to a child process).
- **Envelope:** an `InboundEvent`, the adapter's account of one platform fact
  (a message, an edit, a recall).
- **Intent / receipt:** an `OutboundIntent` asks for exactly one platform
  message; its `Receipt` says what happened to it.
- **Identity:** `self` (the owner's own platform account) or `bot` (Tofu's
  bot or robot on that platform).

## How today's concepts map onto the protocol

| Concept (owner) | In the protocol |
|---|---|
| Thread key `<channel>:<kind>:<id>` (CHANNELS.md Vocabulary) | Core-owned. Adapters send a `ThreadRef` `{kind, id}` with the raw platform ID; the core encodes the key (`x-threadKeyCodecs`): 大象 digits verbatim, GitHub `owner/repo#n` lowercased, base32 for the bot platforms and every later app. Keys are unchanged. |
| Event ID `<thread>/<external id>`, CAS record (C1) | `eventId` is the adapter's stable platform identity; the core derives the ordered external ID from `MessageRef` (`x-externalIds`: `seq` verbatim, else platform seconds + a digest of the message ID, minted once at first sighting when the platform sends no time). Stored IDs are unchanged. |
| Ingress order echo → ignore → classify → CAS → media → route → watermark (C1/C2) | Unchanged, run by the core for every envelope from every binding. |
| Modes command/watch/mentions/ignore, reply policies notify/draft/auto/never (C2/C5/C11) | Core-only. No protocol field carries them. |
| Owner command chat, owner identity (C2/C6/C7) | Core-only: the owner is the platform identity the core bound; an envelope carries only `Sender {id, name, kind}` and has no field by which an adapter could claim the owner. |
| Outbox states draft/queued/sending/unknown/sent/failed/discarded, idempotency key, approval code (C5/C11) | Core-only. The adapter sees intents whose `idempotencyKey` is the item ID (or a part's `<item>/<n>`); receipts map onto the states: `sent`/`accepted` → sent (accepted waits for its echo, C10), `failed` → failed, `deferred` → stays queued, `unknown` → unknown and reconciled (P4). Approval codes stay `id[2:8]`. |
| `[Tofu 代发]` label (C5) | Core-only, applied before the intent exists to `self` texts outside the command chat (P7). |
| Outbox marker `extension.tofuOutbox`, `<!-- tofuOutbox:<item> -->`, part markers (C5/C8/C10) | The adapter stamps the intent's key where the platform can carry it invisibly and reports any marker value it sees as `EchoFacts.marker`; the core decides echo (P12). |
| Subscriptions, watermarks, frontier (C1/C3) | Core-only; the adapter's own resume state is the opaque `EventBatch.checkpoint` (a long-poll cursor, ETags) that the core stores after ingest. |
| Account states disabled … error (channels_v1 `AccountState`) | Typed `Health {state, code, detail, resumeAtMs}` (P6); stored as the legacy state through `x-healthCodes.*.legacyState` until the account view carries health. |
| Media resolved once in ingress (C9) | `AttachmentPart.locator` (opaque, never recorded/rendered/logged) and the `download` method. |
| Robot notification identity, owner fallback (C6) | The `bot` and `self` identities of one adapter; the chain is core policy (P7). |
| Vault `channel.u<owner>.<channel>.*`, export hygiene | Unchanged; credentials never travel in protocol messages (P8). |

## Decisions

### P1. Responsibility split: adapters translate, the core decides

**Problem:** each adapter grew its own share of policy: the 大象 adapter
chooses robot or owner and falls back on its own
(`lib/channels/dx/adapter.py:1071`), runs the catch-up
(`lib/channels/dx/adapter.py:973`); the GitHub adapter ends its session on
one failed list read (`lib/channels/github/adapter.py:202`); the bot
platforms resend unknown sends; Feishu (`lib/feishu/`) has its own
allowlist, model pipeline, in-memory deduplication
(`lib/feishu/events.py:28`) and its own reconnect backoff
(`lib/feishu/startup.py:123`). Every new chat app re-decided authority,
retries and fallbacks, and every lesson (a failed history read that tore down
a session; a runtime fault shown as "please re-login") was fixed per adapter.

**Decision:**

| Core owns | The adapter owns |
|---|---|
| Authority: owner command vs other people's evidence vs our own echo (C2, C7, C11, P12) | Translating platform events into envelopes and intents into platform calls |
| Identity binding: platform ID → owner (C6, C12) | Raw platform identity and platform facts: sender ID, name and kind, marker values, own-connection flags |
| Reply policy, the outbox state machine, idempotency, approvals and drafts, labelling (C5, C11) | Exactly one platform message per intent, and a truthful receipt |
| Routing, subscriptions, watermarks, retention (C1, C3, C4) | Its platform session: connect, keep alive with its SDK's own transport, report each transition once as `Health` |
| Catch-up and backfill orchestration (P3), reconciliation (P4) | Reading history/threads/lookups when asked, as one complete answer or an error |
| Degradation by capability (P5), the outbound fallback chain (P7) | A truthful capability descriptor |
| Reconnect cadence (the supervisors' scheduler tick) and network policy (`docs/NETWORK.md`) | Nothing else: no retries, no timers, no policy |

An adapter can never assert "this is the owner": envelopes are strict
(`additionalProperties: false`) and have no authority field
(`test_no_envelope_can_carry_an_authority_field`); `policy.classify` compares
the sender with the identity the core bound.

**Alternatives considered:** rich adapters with a thin core (today's de facto
shape): every app re-implements the same policy and drifts, as above.
Per-app policy plug-ins: the policy is not per-app, only the platform facts
are, and those live in the descriptor.

### P2. Inbound: envelopes, at-least-once delivery, idempotent ingest

**Problem:** platforms redeliver (long-poll replays, WebSocket resumes,
GitHub's inclusive second-granular `since`), deliver out of order, and some
send edits and recalls; the core must record each fact once, route it once,
and never let an adapter's delivery order decide what is lost.

**Decision:**

- `InboundEvent {type, eventId, thread, message, sender, parts, target?,
  echo?, url?, source}`. `eventId` is deterministic from platform identity
  only (never receipt time or process state), unique within the account.
  `parts` are `text`, `mention` (raw user ID or `all`), `quote` (the quoted
  message and its sender), `attachment` (kind, name, MIME type, size,
  duration, opaque `locator`) and `card` (shown as its `fallbackText`).
- Delivery is at least once, in batches (`EventBatch`). The core ingests
  idempotently: the CAS record of `(thread, external ID)` decides a duplicate
  (C1), so a redelivery is recorded and routed once
  (`duplicate_delivery` passes for every adapter).
- Order: per thread, the watermark is the highest external ID ingested and
  bounds history reads; it is not a filter. An older event that arrives after
  a newer one is recorded and routed (`out_of_order`).
- A batch's `checkpoint` is stored only after every event in it was ingested;
  a failed ingest raises to the adapter, which delivers the batch again, so a
  gap becomes a duplicate, never a loss.
- `edit` (new content of `target`) and `recall` (withdrawal of `target`) are
  facts of their own with their own `eventId`. The core records an edit as a
  revision of the original event, never as a new message and never as a
  second command; a recall marks the event recalled and removes its text from
  Tofu's records. In delivery order, between the messages around them
  (`lib/channels/ingress.py` `apply_change`, `ChannelStore.record_change`):
  an edit appends `{id, text, senderId, atMs}` to the event's `revisions` and
  keeps the original text; a recall sets `recalled` and removes the text, the
  revisions and voice transcripts (text already delivered into a conversation
  stays in its transcript; a stored media original stays the owner's
  attachment). Neither is routed. A redelivered change is a `duplicate`; an
  edit of a recalled message, or a change of a message Tofu never recorded
  (dropped by policy, never seen, or a target it cannot resolve: the target's
  `seq`, else the ID the core minted at the message's first sighting) is
  `dropped`; in an ignored thread it is `ignored`. `channel_read` shows
  `revisions` and `recalled`. A change that arrives before its message is
  dropped rather than held: platforms deliver a change after the message,
  and history never returns a recalled one, so holding it would need a
  second store and a merge rule for no observed case.
- An attachment's `locator` may carry platform credentials; the core keeps it
  in memory for one ingest, hands it back in `download`, and never records,
  renders or logs it (C9).

**Alternatives considered:** exactly-once delivery from adapters: no
platform offers it, and an adapter that tried would need durable state the
core already has. Edits as new messages (GitHub today keeps the creation ID
and drops the edit, `lib/channels/github/normalize.py:123`): either loses the
edit or makes it a second command. Ordering by arrival: a late event would be
dropped behind the watermark.

### P3. Catch-up and backfill are the core's, and never end a connection

**Problem:** missed messages are replayed from history after a reconnect,
but each adapter did it its own way: 大象's catch-up lives in the adapter; a
failed 大象 history fetch once tore the session down and caused a reconnect
storm (fixed for 大象 only in C1), and GitHub still ends the whole session on
one failed list read, so no thread progresses until the next tick.

**Decision:** the core runs the catch-up through `threads` (conversations
active since the frontier) and `history` (messages after each thread's
watermark), when health returns to `connected` and at each supervisor tick
while a catch-up is owed. A failed read keeps the session and live delivery;
health becomes `degraded` with code `history_failed`; the catch-up stays owed
and the next tick retries. Live events ingested while a catch-up is owed do
not move the watermark (C1). History events go through the same ingest as
live ones, so echo detection and authority are identical (P12).
`lib/channels/protocol/catchup.py` runs it for every protocol adapter on its
own worker (never on a binding's dispatcher, which must not wait on the
adapter): `threads` over `[frontier, catch-up start)` on the core's clock (a
thread counts when any of its messages falls inside), else the threads the
core knows; `history` after each non-ignored thread's watermark, or after
`max(frontier, thread.sinceMs)` without one. A failed ingest is `degraded`
`ingest_failed`; health details carry an error code only, never content. A
completed catch-up moves the frontier to its own start and restores
`connected`; a connection that began meanwhile owes its own. `AdapterHost.tick`
is the supervisor's entry.

Evidence: on 2026-11-08 (16:45–22:50) the 大象 catch-up failed 81 times (78
engine-aborted session listings, 3 aborted history reads) against 56
completions, and every failure kept the session, so failures are routine
and must be cheap. `partial_history_failure` and `degraded_is_typed` pass for
the reference adapter (in-process and stdio); 大象's catch-up still runs in its
adapter until migration step 2 (`degraded_is_typed` fails there), and the
GitHub row fails both.

**Alternatives considered:** adapter-owned catch-up (today): re-implemented per
app and wrong in one. No catch-up (push only): messages during an outage are
lost.

### P4. Outbound: one intent per platform message; unknown is reconciled, never re-sent blindly

**Problem:** a send can fail definitely, be refused for a rate limit, or end
with an unknown outcome (the answer was lost). Today an unknown send on a
platform without history is sent again (`lib/channels/bots/session.py:81` →
`lib/channels/outbox.py:541`), which duplicates it when the platform did
deliver; a rate-limited send settles unknown and is retried on the next pass
inside the platform's window; and a long 企业微信 text was split inside its
transport, so a failure after the first chunk repeated the chunks already
sent.

**Decision:**

- An `OutboundIntent` is exactly one platform message: the item's text, one
  chunk of it (`<item>/t<n>`), or one attachment part (`<item>/<n>`).
  `idempotencyKey` equals `intentId`; the adapter carries it invisibly where
  the platform can (`markers`). The core has already applied reply policy,
  approval, the label, translation and degradation.
- `Receipt.status`: `sent` (the platform has it, with `platformMessageId`);
  `accepted` (acknowledged but confirmed only by its echo or the thread:
  大象 files, C10); `deferred` (definitely not sent, send later:
  `not_connected`, `rate_limited` with `retryAfterMs`); `failed` (definitely
  not sent, never retried automatically); `unknown` (`answer_lost`,
  `platform_unavailable`, `adapter_failed`).
- A deferred intent stays queued and is not attempted again before
  `retryAfterMs` (or the health's `resumeAtMs`); it is not an attempt. The
  outbox keeps the item `queued` with that time (`notBeforeMs`), holding its
  thread so nothing later overtakes it; it is neither reconciled nor a
  Watchdog fault (`lib/channels/outbox.py` `DEFERRED_SEND_CODES`). While the
  adapter reports `rate_limited` until `resumeAtMs`, `HostSender` attempts
  nothing. A rate-limit refusal that names no time waits out the identity's
  shortest documented window (descriptor `rateLimits`, a platform fact: the
  first moment a retry can be accepted; a further refusal defers again);
  with no documented window, and for `not_connected`, the next pass tries
  again. Rejected: retrying at the next tick inside a documented window, and
  each adapter deriving that time from its own documentation.
- An unknown intent, and one found `sending` after a restart, is reconciled
  down this ladder, first applicable rung only: (1) `lookup` by idempotency key
  when the platform keeps an idempotency record; (2) the thread's `history`
  searched for the marker, client ID or platform ID; (3) a resend inside a
  documented deduplication window (`idempotentResend:
  passive_reply_window`, QQ's `msg_seq`); (4) otherwise the item stays
  `unknown`, blocks only its thread (C5), raises the Watchdog outbox fault (R7)
  and waits for the owner's one action in 代发记录 ("send again"/"discard").
  Nothing is re-sent blindly.
- A rung concludes "not delivered" only from a complete, successful answer:
  `lookup` answering `found: false` out of the platform's own idempotency
  record, or a `history` walk that covers the attempt (an incomplete walk is
  an error, `HistoryQuery`). A failed, partial or unanswered read is no
  evidence: the intent stays unknown, nothing is sent, the next tick asks
  again, and the stall holds no other thread (C5): the executor leaves an
  item whose read raised as it was and holds only its thread
  (`lib/channels/outbox.py:490`, parts `:1035`). `reconciliation_read_failure`
  and `stalled_thread_isolation` pass for the reference adapter, 大象 and
  GitHub.
- An `accepted` intent (大象 files, C10) is confirmed by its echo or by
  the history read of the next pass, which is its confirmation point. If
  that pass cannot confirm it, it stays pending with a typed reason:
  `history_failed` when the read failed, or `needs_owner_confirmation` when
  the history answered without it or any later message. Either way it
  raises the Watchdog outbox fault once and is never sent again. It holds
  no thread. Each later pass asks again, and the first read that shows it
  or a later message settles it. This is rung (4)'s owner hold with no
  timer. `accepted_confirmation` passes for 大象; the other adapters carry
  no files.

Evidence: `restart_mid_send` and `unknown_reconciliation` pass for 大象, GitHub
and QQ (history or `msg_seq`) and fail for 企业微信 and 微信 (second copy);
`rate_limited` passes for the reference adapter (its `retryAfterMs`) and
企业微信 (its documented per-chat window) and fails for QQ, whose refusal
names no time and which documents no window.

**Alternatives considered:** at-least-once resend where nothing can be
reconciled (today's C12): duplicates the owner's message to other people; the
cost of a held message is one owner tap (an owner trade-off, below). A
per-adapter retry or hold: the hold must be visible in one outbox, and the
rate-limit window is a platform fact the core can honour once. Concluding
absence from an empty or failed read: one dropped page re-sends a message the
platform already delivered. OpenClaw 2026.11.1 holds the same line: its
unknown-send reconciliation answers `not_sent` only when a fresh send is
provably safe and stops on missing or incomplete proof (Reference design:
OpenClaw, below).

### P5. Capability descriptors; the core degrades content

**Problem:** what an app can carry (Markdown, length, files, quotes, edits,
history, notifications) is scattered over `x-channelTraits`, `x-wecom`,
`x-qq`, `x-github`, `x-providerLimits` and adapter code; a file sent to an
app without files was refused at enqueue, and text limits are applied inside
two adapters differently.

**Decision:** every adapter declares a `CapabilityDescriptor` at
`initialize`: thread kinds, command-chat suitability, inbound mode and
facts (mentions, quotes, edits, recalls, attachments, echoes, sender kinds),
history abilities, outbound reconciliation abilities and reply windows, and
per identity (`self`, `bot`): whether its message notifies the owner, formats,
`maxTextBytes`, quote replies, markers, attachment types and size bounds, and
the platform-documented rate limits with their source. The in-repository
targets live in the contract's `x-adapters`, with every number a `$fact`
pointer into `channels_v1`, so a platform limit has one home
(`test_platform_limits_come_from_the_channels_contract_not_copies`). The core
calls an optional method only when declared, and degrades content the chosen
identity cannot carry: a card → its fallback text; a file → a note naming it
(and its delivery link when it is a published artifact); text beyond
`maxTextBytes` → chunks at UTF-8 boundaries, each its own intent; Markdown →
plain text. No Tofu feature may depend on one chat app. Files today
(`lib/channels/outbox.py` `_place_attachments`): a channel without files
(`x-channelTraits.attachments`) gets each file named in the item's text from
enqueue on, so a draft and its notice show it, with a published artifact's
delivery URL; the admitted records stay on the item (`degradedAttachments`)
for idempotency, and admission verifies the source as for a sent file but
applies no upload limit (`attachments.admit(carried=False)`). Cards already
arrive as their fallback text (the codec). Text today (`lib/channels/outbox.py`,
text chunks): the sender port names the chosen identity's limit
(`max_text_bytes`: `HostSender` from the negotiated descriptor, the bot
platforms' sender from `x-adapters`); a longer text is cut once, on its final
delivery text, after a line break where one fits and otherwise at a character
boundary, and each chunk is its own intent `<item>/t<n>` with its own state. A
sent chunk is never sent again, an unknown one is found by its marker or sent
again under P4's rules, a deferral resumes at the chunk it deferred, and a
refused chunk fails the item without sending the rest. A markerless identity
on a platform with history (大象's robot) is reconciled by its exact posted
texts, which only its own send path records per item (C6 `robotTexts`,
`robotPosted`), so `HostSender` names no limit for it and that path splits
the robot's texts. Markdown to plain text is still the adapters' (migration
step 1).

**Alternatives considered:** traits per channel in core code (today): every
new app edits core code. Per-adapter degradation: the same message would
degrade differently per app. Chunking the robot's texts in the outbox as
well: its posts carry no marker, so the core would need a second,
text-matching reconciliation beside the robot's own (C6).

### P6. Connector lifecycle and typed health

**Problem:** account state is a coarse enum plus free text: a failed catch-up
shows as `connected` with a sentence (invisible to the Watchdog); a network
failure of a bot platform is `error` "session failed: …"
(`lib/channels/bots/session.py:163`; 5 微信 session failures on 2026-11-08,
connection-setup timeouts in a two-minute burst at tick cadence right after
the 16:55 restart); the 大象 runtime fault `SSO_NODE_UNAVAILABLE` was once shown as
"please re-login"; a kick needed its own state.

**Decision:** `Health {state, code, detail, resumeAtMs, atMs}` with states
`connected`, `degraded`, `auth_required`, `runtime_missing`, `rate_limited`,
`disconnected`, `displaced`. Each code (`x-healthCodes`) fixes its state, its
remedy (`x-remedies`), who retries (`core_tick`, `resume_at`, `owner`,
`none`) and the legacy `AccountState` it is stored as until the account view
carries health. The adapter reports transitions only; reconnects happen at
the supervisors' existing scheduler tick (no adapter loop, no backoff
knob), HTTP route recovery stays with the shared network authority
(`docs/NETWORK.md`), and an SDK's own reconnect within one session is that
SDK's transport lifetime. `auth_required`, `displaced` and
`protocol_version_unsupported` wait for the owner; a server restart keeps
them (the persisted state is checked before any launch). Health feeds the
Router Watchdog (R7): `auth_required`, `runtime_missing`, `rate_limited`,
`disconnected` and `displaced` open a channel signal keyed by channel,
state, code and the last connection time; `degraded` heals itself at the
next tick and is shown in Settings and the Watchdog snapshot without a
signal. Proactive pushes (R10) queue in the outbox and go once on reconnect.

Evidence: `runtime_fault_is_typed` passes for 大象 (launcher refusal →
`runtime_unavailable`); `network_fault_is_typed` fails for the three bot
platforms; `degraded_is_typed` fails for 大象 and GitHub.

**Alternatives considered:** free-text detail (today): unparseable, and
the Watchdog cannot tell a runtime fault from a login. A per-adapter backoff
(Feishu's): `docs/NETWORK.md` and design principle 5 reject domain-local
retry loops and tuning knobs.

### P7. Outbound identity: explicit, recorded, bot first

**Problem:** the owner asked «为什么突然发文件用的我的账号而不是tofu agent的？».
Which identity posts is decided inside the 大象 adapter, is not recorded per
message, and is not shown; on 大象 the robot posts only in its bound command
chat (C6) and cannot send files at all (its API takes files only as URLs on
the caller's storage, C10), so files always went out as the owner's own
messages, and the CLI path of 2026-11-08 sent everything as the owner.

**Decision:** every intent names its identity (`self` or `bot`), every
receipt repeats it, every outbox part records it, and 代发记录 and the send log
show it (由 Tofu 机器人发送 / 由你的账号代发). The core chooses, per intent:
`bot` first wherever the platform and the binding allow it (大象: the robot,
only in its bound group command chat, never for files); otherwise `self`. A
bot refusal (`identity_refused`) or a bot rate limit sends the rest of the
item as `self` at once; parts already sent are never repeated, because each
part has its own intent and receipt. That fallback follows only a definite
non-send (a `failed` or `deferred` receipt): an `unknown` receipt is
reconciled under the identity that sent it and continues there, never under
the next one, because its copy may already be posted
(`unknown_keeps_its_identity`, passing for 大象). A `self` text outside the
command chat carries the `[Tofu 代发]` label (C5, unchanged); a `self` file
cannot carry a label, so its record and the robot's following text name it.

**Alternatives considered:** adapter-chosen identity (today): other apps
cannot share the policy and nothing records the choice. Files through the
robot by URL: the phone must reach Tofu's host and the file leaves 大象's
storage (an owner trade-off, below). Falling back on an unknown outcome as
well: a second copy under the other identity whenever the first arrived;
OpenClaw 2026.11.1 states the rule outright (failure is no permission to send
the same payload through another path).

### P8. Identity binding, multi-user and credentials

**Decision:**

- The core binds a platform identity to the owner from proof material:
  `identity` (大象 whoami of the MIS the owner chose, GitHub `GET /user` of
  the owner's token) or, on bot platforms, the pairing code from an inbound
  message (C12). The per-server MIS binding and the shared robot's binding
  refusals stay C6's.
- Credentials live in the vault entries `channel.u<owner>.<channel>.*`; an
  in-process adapter reads them per request through the core's vault port,
  an out-of-process adapter receives them in its child environment at spawn.
  They never travel in protocol messages (`InitializeParams.account` is
  non-secret configuration). The reserved `channel.*` entries are never
  exported (`lib/credentials_vault.py` reserved prefixes, `export_pkg/_lists.py`).
  An adapter sees only its own channel's entries.
- Sessions are keyed by (owner, channel); the identity is an explicit
  parameter everywhere (`TODO(enterprise)` in C7: the supervisors host only
  the scheduler principal's owner today).
- A central relay is another adapter on the same protocol (the stdio
  binding, or a socket carrying the same frames); its own authentication is
  its binding's concern.

### P9. Process model: one protocol, two bindings (in-process and stdio JSON-RPC)

**Problem:** should adapters run inside the server process (A), out of
process over a versioned wire protocol like LSP or MCP (B), or B with an
in-process shim?

**Evidence gathered:**

- Runtimes today: 大象 is Node (the skill's engine) and already runs as a
  child speaking JSON lines over stdio behind a Python shim (`bridge.mjs`,
  `DxAdapter`): de facto "B with a shim", without versioning or capability
  negotiation. GitHub, 企业微信, QQ and 微信 are Python in-process; Feishu is
  Python in-process with its own pipeline.
- Crash and restart history (logs 2026-11-08 16:45–22:50): 62 大象
  reconnects inside the bridge, 4 bridge exits (failed recovery, exit 4)
  restarted at the next tick, 5 微信 session failures (network), no Python
  process fault involving channel code in any faulthandler dump. Faults are
  network and engine faults inside sessions, not adapter crashes that take
  the server down.
- Latency: inbound is human-scale (a catch-up took up to 124 s on
  2026-11-09); the stdio conformance row runs the same cases as the
  in-process row at about half a second each, including spawning the child.
  The binding costs nothing measurable against platform round trips.
- Deployment: adapters run in the execution host (`scheduled_jobs`,
  `lib/server/server_background_services.py`), started and reconciled by the
  scheduler tick, while account commands arrive in the API host; Node exists
  only where the 大象 skill provides it (`PinnedHelperSource`); the export
  ships no `channel.*` entries.
- Lifecycle owners: the 大象 bridge and the MCP stdio clients already follow
  `docs/DEFENSIVE_PATTERNS.md` (spawn helpers from the loaded source, dispose
  to quiescence, observe worker exit separately from completion).

**Decision:** one protocol with two bindings and the same validated objects
on both: *in-process* for Python adapters with a maintained SDK, and *stdio
JSON-RPC 2.0* for adapters in another runtime (大象's Node engine), third-party
adapters and a future relay. The core validates every object it receives
against the contract whatever the binding. The stdio child runs in its own
process group with its credentials in its environment; closing stdin is
its stop signal, then the core signals the group and reaps it
(`lib/channels/protocol/stdio.py`). Both bindings are exercised by the
conformance suite (`reference` and `reference-stdio` rows give identical
results).

**Alternatives considered:** (A) in-process only: cannot host 大象's Node
engine. (B) every adapter out of process: a process and a lifecycle per
Python adapter for no measured benefit; no Python adapter crash is on record.
A socket binding now: deferred until a remote relay exists; it carries the
same frames.

### P10. Versioning, negotiation and deprecation

**Decision:** `x-protocol`. The protocol has a major version (1). The core
offers its supported versions, N and N−1, in `initialize`; the adapter answers
the highest one it speaks or the core refuses (`unsupportedProtocolVersion`,
health `runtime_missing`/`protocol_version_unsupported`, remedy
`update_adapter`). A change that an N adapter cannot ignore is a new major
version. Optional additions inside a major version are announced by the core
in `coreFeatures` and used only when announced; envelopes stay strict. When
N+1 ships, the core supports N+1 and N; an adapter on N runs with health
`degraded`/`protocol_version_deprecated` and one log line per session, and
the following release drops N. In-repository adapters move in the release
that introduces N+1.

**Alternatives considered:** lenient parsing (ignore unknown fields): an
authority field could be smuggled in, and drift goes unnoticed. Version per
method: negotiation multiplies with no gain at this size.

### P11. One live session per platform account, owned by the core

**Problem:** at 23:42:35 on 2026-11-08 the server's 大象 session was kicked
("another login took it"). A Worker in another project, delegated to "find
the actual 大象 bridge available to you", ran the dx-pura-api CLI (`dx.mjs
send`, `send-file`, `send-image`); every such send opens a new 大象 Web-SDK
IM session for the owner's account with the server bridge's device ID, so
大象 displaced the bridge; 16 more image sends followed, the outbox stopped,
and every message went out as the owner's own, outside the outbox (no label,
no record, no echo entry). The chain began when the Global Router's
`channel_send` refused the files (`attachment_outside_workspace`) and the
project conversations, which have no channel tools (C7), had no supported
way to deliver them (P13).

**Decision:**

- The core owns the account's one live platform session, opened only by its
  supervisor through the adapter (`x-connectorOwnership`).
- Every Tofu send is an outbound intent: `channel_send` → outbox → adapter.
  No conversation, tool or script opens its own session or sends as the
  owner outside the core.
- Any Tofu-launched client of the platform checks the core's ownership first
  and fails closed while the core owns the account, with
  `connector_owned` (JSON-RPC `connectorOwned`, −32005) naming
  `channel_send` and, for files, the P13 path. For 大象 (the DX-KICK lane):
  the bridge holds the engine's per-host Web-channel lock from before its
  login until it exits; a Tofu-run CLI write then fails at once with the
  engine's own code `web_channel_mutex_timeout`, whose holder label leads
  with `connector_owned` and names `channel_send` (commands get no lock
  wait, because the CLI would otherwise report success after its 120 s
  engine timeout); HTTP reads take no lock. The lock is per host: a 大象 web
  page or another host can still displace the session, which the health code
  then names.
- A displacement is health `displaced`: code `superseded_same_device` when
  a client with the session's own device identity took it (a process on
  this host outside the core; 大象 `KickOffWithSameDeviceId`), `superseded`
  for another device, a browser or another host (`KickOff`), the platform's
  event name kept in `detail`. Remedy `restore_session`: close the other
  login, then reconnect once (Settings → 重新连接). No reconnect, no login
  push, across server restarts, until then. Evidence:
  `displaced_stays_down` passes for 大象 (`kicked` is terminal in the
  supervisor and on boot), 企业微信 (`disconnected_event`) and the
  reference adapter; on 2026-11-08 the same-device distinction alone would
  have named the culprit class.

**Alternatives considered:** a second device ID for tools: no kick, but the
sends still bypass the outbox, the label, approvals and echo detection (P12).
Sharing the engine's web-channel lock only: serializes sends, still opens a
second session. Reconnecting after a kick: ping-pong with the other client
and repeated login pushes to the owner's phone.

### P12. Self-echo is nonhuman evidence, whatever path sent it

**Problem:** after the owner restored the session (2026-11-09 00:17:47), the
catch-up replayed the Worker's CLI sends from the command chat: 19 messages
of the owner's account with no outbox marker and no echo record, each
delivered to the Global Router as a genuine owner command (`channel-owner-…`
steers, 00:19:15–00:19:47). The echo check knows only outbox items, recorded
IDs and the bridge's own-connection flag (`lib/channels/ingress.py:167`); an
approval code in such a text would have been honoured.

**Decision:** before classification, on live delivery and backfill alike, a
message from an identity the core bound to the account (the owner's account,
its bot, its bound robot) is an echo, recorded as context and never routed,
when the first of these matches (`x-selfEcho`):

1. its marker names an outbox item or part of the account (an in-flight item
   becomes sent);
2. its platform message ID or client ID is in the echo registry: every
   receipt's IDs, and every ID another Tofu send path registered through the
   core's self-send port;
3. it carries the platform's marker field with any other value: only an API
   can set it, so a Tofu send path stamped it (every Tofu send path must);
4. the platform says this session sent it;
5. the adapter's host recorded that another local client sent it
   (`EchoFacts.hostSent`; 大象: the engine's sent-message record, its newest
   100 sends within 24 hours); the core registers its IDs at first sight, so
   a later replay stays an echo.

An owner-account message matching none stays the owner's: an unmarked API
send cannot be told from typing, which is why P11 forbids sends outside the
core. The 大象 skill CLI stamps no marker today (`dx.mjs` sets `extension`
only for `--at`), so an out-of-band send beyond the host record's bound is
unrecognizable; therefore, after a displacement, owner-account messages from
the displaced window are delivered as owner context, never as commands or
approvals, because the displacing client sent as the owner
(`x-selfEcho.displacementWindow`).

Evidence: `self_send_outside_outbox` passes for the reference adapter and 大象
(steps 3 and 5 in `lib/channels/ingress.py` `_echo_item`) and for 微信 (the
platform flags the bot's own messages); it fails for GitHub, whose marker
pattern accepts only outbox item IDs (`lib/channels/github/normalize.py:28`),
so the core never sees the other marker (an adapter fix).

**Alternatives considered:** matching texts against Tofu's recent sends:
false matches against the owner's real words, and an attacker could quote
Tofu. Demoting every backfilled owner message to context: loses the owner's
real commands typed during an outage; the rule above narrows the demotion
to a displacement window.

### P13. Attachment sources: what an intent may attach, and from where

**Problem:** the Global Router's `channel_send` admits only files in its own
workspace roots or its own artifacts (C10), but only the Global Router may
send (C7); a project's deliverable therefore had no supported path, and the
refusal said only "attach a file inside one of its roots". A Worker went
looking for another way and found a second login (P11).

**Decision** (`x-attachmentSources`): the core admits every file before
anything is recorded, pins its size and sha256, and hands the adapter only a
private verified copy; adapters never read caller paths. Admissible:

1. a regular file in the sending conversation's verified roots, read without
   following symlinks;
2. an artifact or slide deck the sending conversation published;
3. `{path, project}`: a regular file inside the registered project root the
   Global Router names (`lib.project_mod.config.get_recent_projects`), read
   with the same no-follow snapshot and limits, recorded on the item with
   that project as its provenance and shown in 代发记录: the path by which a
   project's file reaches the Global Router.

Every refusal names the supported path: send with `channel_send` from the
Global Router; a project conversation reports the file's path to the Global
Router and asks it to send; nothing sends through a login of its own.
Platform limits (types, sizes) stay the descriptor's.

In code (`lib/channels/attachments.py`): `channel_send` takes
`{path, project}` beside `{path}`; `project` must equal one of the owner's
registered roots exactly as `get_router_projects` lists it (or name the same
directory). The registered root is resolved once, because the owner
registered it, and inside it the no-follow reader refuses any link; the
record's source keeps `project`, which `OutboxAttachmentView.project`
returns (Settings does not show it yet). Every `channel_send` attachment
refusal ends with the `{path, project}` form and `supportedPath`, and a
non-Router `channel_send` is refused with the same instruction. Names are
matched exactly, never by directory name, so one name in two places cannot
pick the wrong project.

**Alternatives considered:** a copy step into the Router's workspace: a
second copy of the owner's file per delivery and an approval round trip for a
file the owner already owns. Channel tools in project conversations: spreads
send authority, against C7. Any absolute path: would let a send exfiltrate
files outside the owner's projects.

### P14. Observability

**Decision:** per adapter, from records the core already keeps: ingest lag
(platform time → recorded), catch-up duration and outcome (today's
`dx catch-up completed … took_ms` line, generalized), send latency (intent →
receipt), receipt outcomes by status and code, and health transitions. One
trace runs event ID → delivery command ID (`channel-event-…`,
`channel-owner-…`) → turn and task → outbox item (its origin turn) →
intent IDs → platform message ID; every log line carries those IDs and
never message content, locators or credentials. The tool-call log shows
`channel_send`'s text and attachments only by size and sha256 (the channel
handler declares them content arguments at registration, docs/LOGGING.md).
No new store and no sampling knob.

### P15. Backward compatibility

**Decision:** thread keys, event and external IDs, outbox item IDs
(`ob` + sha256 of owner, thread and key), approval codes, markers and
account states keep their current form; the protocol is a boundary in front
of the same store, and the codecs reproduce today's values exactly
(`test_thread_keys_keep_their_current_form`,
`test_external_ids_reproduce_todays_event_ids`,
`test_a_legacy_event_survives_the_envelope_round_trip`,
`test_receipts_keep_the_outbox_vocabulary`). New durable fields are additive
(the adapter checkpoint, health, event revisions and recalls, part
identities); no data migration.

## Wire binding

- **Stdio:** JSON-RPC 2.0, one UTF-8 JSON object per line, both directions.
  A request has an `id`; a notification (`health`) has none. Methods,
  parameters and results are `x-methods`; errors are `x-errors` (standard
  codes, then −32001…−32005). stdout carries protocol lines only; stderr is
  diagnostics that never contain content or credentials. A request ends with
  its answer or with the binding (process exit, closed pipe), which makes an
  in-flight send `unknown`; the protocol adds no timeout. Messages in one
  direction are handled in order; a notification and a response may
  interleave, so health is read as eventually consistent.
- **In-process:** the adapter object is called with the same objects; the
  core validates them the same way.
- **Session:** `initialize` → `start` (with the stored checkpoint) → the
  adapter delivers `events` and `health`; the core calls `send`, `lookup`,
  `history`, `threads`, `download` as declared → `shutdown`.

## Conformance

`tests/channels/conformance/` makes the protocol executable:

- `cases.py`: one function per promise (duplicate delivery, out-of-order,
  edits, recalls, auth expiry, displacement, rate limits, partial history
  failure, typed degraded/runtime/network health, restart mid-send, unknown
  reconciliation, failed reconciliation reads, a stalled thread holding no
  other, an accepted file's confirmation, the identity an unknown send
  keeps, attachment degradation, echo
  loop, self-sends outside the outbox, other bots), written against platform
  verbs and core observations,
  never against one adapter.
- `reference.py`: a protocol-native adapter over an in-memory platform with
  every capability; it runs in-process and as a child over stdio, so its
  failures are core gaps.
- `harness.py`, `driver_*.py`: today's adapters, unchanged, against the
  platform fakes of the channel suite, behind a compatibility shim at three
  seams: ingress (`ChannelEvent` → envelope → `AdapterHost`), the outbox
  sender (intents and receipts) and account status (typed health through
  `x-legacy`).
- `baseline.json`: per adapter and case, `pass`, `xfail` (the cause with
  `file:line` and whether the core or the adapter owns the fix) or `n/a` (the
  platform offers nothing to exercise). Expected failures are strict: a case
  that starts passing fails the run until the baseline is updated in the
  same change, so every migration step records what it fixed.
- `test_protocol_contract.py`: the contract's own consistency and the codecs.

- `test_protocol_host.py`: the core's endpoint on its own, with a minimal
  in-process adapter: echo facts, edits and recalls, deferrals and the
  documented rate-limit window, the catch-up and its typed health.

Run `pytest tests/channels/conformance -q`. No case reaches a real platform.

## Migration

Each step lands with its baseline update, the existing channel suite and
`python3 scripts/check_architecture.py` green, no data migration (P15) and no
new knob.

0. **Immediate (from the 大象 kick).** The DX-KICK lane (commits
   `5aedd62a00`, `961648e099`): the bridge holds the engine's Web-channel
   lock, so Tofu-run CLI writes refuse with `connector_owned`; the bridge
   flags `hostSent` and ingress registers those echoes; the codec carries it
   as `EchoFacts.hostSent` both ways
   (`test_a_host_sent_owner_message_is_an_echo_and_its_replay_stays_one`).
   Done since: attachment refusals that name the supported path, with
   `{path, project}` admission (P13). Still open: the displacement-window
   rule; the same-device health code.
   Accept: `self_send_outside_outbox` passes for 大象 (done: any other value
   of the marker field is an echo); a kicked account stays down across a
   restart (already true).
1. **Core gaps (the reference rows).** Done: deferred receipts and the
   rate-limit hold (P4); core-owned catch-up over `threads`/`history` with
   `degraded` health (P3); edit and recall records (P2); sender kinds in
   policy (another bot is never a candidate); files degraded to a note
   instead of refused (P5); the self-echo ladder (P12); a reconciliation
   read that fails holds only its own thread (P4); text beyond `maxTextBytes`
   chunked by the core, one intent per chunk (P5; 企业微信's transport no
   longer splits); every `reference` and
   `reference-stdio` case passes, and `rate_limited` (企业微信),
   `attachment_degradation` (GitHub and the bot platforms),
   `self_send_outside_outbox` (大象) and `stalled_thread_isolation` (大象,
   GitHub) flipped with them. Still open in the
   core: the owner's hold where nothing can reconcile an unknown send
   (企业微信, 微信: `restart_mid_send`, `unknown_reconciliation`); Markdown
   to plain text (P5); the displacement
   window (P12); typed health on the account view (channels_v1
   `ChannelAccountStatus` gains the health object; Settings shows remedy
   text); the attachment provenance in Settings (代发记录).
2. **大象 natively on the protocol.** `bridge.mjs` speaks JSON-RPC 2.0
   (`initialize`, `start`, `events`, `health`, `send`, `history`, `threads`,
   `download`); the Python `DxAdapter` shrinks to the stdio link; the
   catch-up moves to the core; recalls become `recall` events; the robot is
   the adapter's `bot` identity and the fallback chain moves to the core.
   Accept: every applicable 大象 case passes, and the 大象 tests in
   `tests/channels/` (bridge contract, adapter, robot, shared robot,
   attachments, media) pass with only the port-level changes recorded.
3. **GitHub second**, chosen because it stresses the abstraction most: after
   大象 it runs the most cases (14 of 17 applicable), it owns the most
   adapter-side gaps (6), and it differs from 大象 in kind rather than by
   missing features: polling instead of push, no command chat (the owner's
   text is a candidate), editable comments, bot senders, platform-given
   resume times, markers inside content, per-thread checkpoints
   (ETags, `Last-Modified`). The bot platforms differ mostly by absence (no
   history, edits or quotes) and share one session core. Accept: every
   applicable GitHub case passes (`out_of_order`, `edit`,
   `partial_history_failure`, `degraded_is_typed`, `other_bots`,
   `attachment_degradation`, `self_send_outside_outbox` flip) and the GitHub
   tests pass.
4. **企业微信, QQ, 微信 together**: `bots/session.py` and `bots/platform.py`
   become one protocol adapter base for the three transports. Accept: their
   rows pass where applicable (`network_fault_is_typed`, `restart_mid_send`,
   `unknown_reconciliation` for 企业微信/微信, `rate_limited`). QQ's gateway
   session and last ingested sequence become its `EventBatch.checkpoint`, so
   a restart Resumes and QQ re-sends what it still holds, instead of a fresh
   Identify that loses what arrived while Tofu was down: today both live only
   in memory (`lib/channels/qq/transport.py:129`, advanced after ingest at
   `:226`). OpenClaw's QQ plugin persists the resume sequence only after
   durable admission (Reference design: OpenClaw).
5. **Feishu onto the channel core**: replace its own allowlist, pipeline,
   deduplication and backoff with a protocol adapter (it has history, edits,
   recalls, cards and a bot identity). Waits on the owner's Feishu decision
   below.
6. **Retire the legacy ports**: the `OutboxSender` port, `x-legacy`, the
   shim and the legacy account states once Settings and the Watchdog read
   typed health.

## Adding a chat app

1. Collect the platform's documented facts with sources: message and
   attachment limits, rate limits, edit/recall/history APIs, bot versus user
   accounts, whether a message notifies, login and session rules (another
   login displacing a session?). Platform numbers go into
   `contracts/channels_v1.schema.json` (`Channel`, `x-channelTraits`, an
   `x-<channel>` fact block); the descriptor goes into `x-adapters` with
   `$fact` pointers.
2. Choose the binding (P9): in-process for a Python SDK, stdio for another
   runtime.
3. Implement `ProtocolAdapter`: `capabilities`, `start` (connect, report
   health once per transition), envelopes with deterministic `eventId`s,
   `send` (one platform message per intent, a truthful receipt), and the
   optional methods the descriptor declares. No retries, timers or policy.
4. Write a platform fake under `tests/channels/` (never a real endpoint), a
   conformance driver, and record the row in `baseline.json`; a new adapter
   passes every applicable case except the core gaps already recorded.
5. Credentials in `channel.u<owner>.<channel>.*`; a Settings section in the
   消息平台 tab; the supervisor registration on the scheduler tick.
6. Document the channel's platform section in `CHANNELS.md`.

## Reference design: OpenClaw

The owner asked (2026-11-09) whether OpenClaw, built into chat apps from the
start, should shape this protocol. Sources: `github.com/openclaw/openclaw` at
commit `71bf0fcf30` (release 2026.11.1, 2026-11-08), cited `openclaw:<path>`,
and Tencent's 微信 plugin `github.com/Tencent/openclaw-weixin` at `874637a0db`
(2.4.10), cited `openclaw-weixin:<path>`. The test is what serves one owner's
multi-project Router, not what is popular. OpenClaw's supported boundary is a
trusted team in which everyone admitted to an agent shares its tools
(`openclaw:docs/gateway/security/trust-model.md`); Tofu has one owner whose
own words alone instruct (C7, C11), which decides most rows. Verdicts:
**adopt** (taken as is), **adapt** (taken in our form), **keep** (ours already
does it or does better), **reject**.

**Setup and identity**

| Behaviour | OpenClaw | Tofu | Verdict |
|---|---|---|---|
| Who may talk to the bot | An unknown DM sender gets an 8-character code (1 h, at most 3 pending per account); the operator approves in the Control UI or CLI; approval is chat access only, and the first one can make the command owner (`openclaw:docs/channels/pairing.md`) | Settings shows a one-time code the owner sends from his own private chat; it binds owner and command chat; strangers get no answer (`lib/channels/bots/session.py:230`, C12) | **Keep**: for one owner the code travels from the trusted surface to the chat; OpenClaw's direction answers strangers (C11) and needs a pending cap (a knob) |
| Chat access vs. command authority | Allowlists admit chat; owner-only commands need `commands.ownerAllowFrom`; allowlisting never makes an owner (same page) | Only the bound owner in the command chat is human input; everyone else is evidence (C7, `lib/channels/policy.py:70`) | **Keep** (convergent) |
| QR login (微信) | The plugin (2.4.8 on that page) registers no pairing; an empty allowlist falls back to the QR scanner and otherwise admits any sender (`openclaw:docs/channels/wechat.md`, Access control) | The scanner is the bound owner; nobody else instructs (`lib/channels/wechat/login.py:143`) | **Reject** fail-open admission: what adapter-owned security costs (P1) |
| Owner identity source | `ownerAllowFrom` IDs typed into config or bootstrapped by the first approval | The session's own login proves it (大象 `whoami`, GitHub `GET /user`, pairing, QR; P8, C6) | **Keep**: a typed ID proves nothing (C6) |
| Setup aids | `channels status --probe`; `doctor` prints the exact fix; QQ `/bot-me` shows the sender's openid for manual allowlisting; the QQ wizard binds credentials by QR (`openclaw:docs/channels/qqbot.md`) | Typed health with an owner remedy (P6, `x-remedies`); pairing needs no openid | **Keep**; QR credential binding for QQ is a follow-up once its API is documented outside Tencent's plugin |
| Group join introduction | One introduction per room when the bot joins (tool-less turn, 90-day claim) (`openclaw:docs/channels/index.md`) | Nothing is posted that the owner's reply policy does not allow (C11) | **Reject** |

**Conversation semantics**

| Behaviour | OpenClaw | Tofu | Verdict |
|---|---|---|---|
| Command surface | DMs of every channel share one main session, and a one-entry allowlist pins its route against other senders' DMs (`openclaw:docs/channels/channel-routing.md`); a personal WhatsApp number takes commands in its self-chat, never pairs its own outbound messages and prefixes replies with an identity (`openclaw:docs/channels/whatsapp.md`) | Each platform's command chat feeds the Global Router (C2, C4); owner messages elsewhere are context (C11); `[Tofu 代发]` outside the command chat (C5) | **Keep**; ours is stricter |
| Groups and mentions | Group policy, then the mention gate, else stored as context; trigger authorization apart from context visibility; tools follow the turn's requester; ambient rooms speak only by an explicit send (`openclaw:docs/channels/groups.md`, `openclaw:docs/channels/ambient-room-events.md`) | Modes `command`/`watch`/`mentions`/`ignore`; candidates reviewed under the review ceiling; sends only by `channel_send` under the reply policy (C2, C11) | **Keep** (convergent) |
| Other bots | Admitted on several channels, bounded by a pair rate guard (20 events per 60 s, 60 s cooldown) (`openclaw:docs/channels/bot-loop-protection.md`) | Another bot is never a command or a candidate (`other_bots`) | **Reject** the knob; the structural rule stays |
| Approvals in chat | Same-chat `/approve`, native buttons, the first answer wins across surfaces (`openclaw:docs/plugins/sdk-channel-plugins/approvals.md`) | 「批准 <code>」 in the owner's own words through a Router Turn, or Settings (C11) | Owner trade-off (below) |
| Steering while busy | Default `steer`: new messages join the active run after each tool batch; `interrupt`; whole-message stop words (`stop`, `停止`, `暂停`, …) abort deterministically (`openclaw:docs/concepts/queue.md`, `openclaw:src/auto-reply/reply/abort-trigger-text.ts`) | Steered at the next round boundary (C4); a stop is an ordinary message to the Router | Steering **keep**; a deterministic stop is an owner trade-off (below) |

**Proactive messages**

| Behaviour | OpenClaw | Tofu | Verdict |
|---|---|---|---|
| Scheduled checks | Heartbeats retired into ordinary jobs: one "Proactive check" job (every 30 min by default, waits for idle, optional `activeHours`, quiet `NO_REPLY`) (`openclaw:docs/gateway/heartbeat.md`) | Wakes only for open obligations, one pusher, quiet hours and a digest, no cadence knob (ROUTER.md R10) | **Keep**; **reject** a periodic wake (a Router wake is about six large-context requests, R10) |
| Where proactive output goes | The owner target resolves a positively identified owner DM, never the last group; a missing route records non-delivery; an ambiguous send is never replayed (`openclaw:docs/automation/cron-jobs/delivery.md`) | Proactive pushes queue in the outbox for the command chat and go once on reconnect (P6, R10) | **Keep** (convergent) |

**Continuity and reliability**

| Behaviour | OpenClaw | Tofu | Verdict |
|---|---|---|---|
| Unknown send outcomes | Reconciliation answers sent, not_sent (only when a fresh send is provably safe) or unresolved, and missing or incomplete proof stops recovery; Telegram text retries only on 429 or a provably unstarted request (`openclaw:docs/plugins/sdk-channel-outbound.md`, `openclaw:docs/concepts/retry.md`) | The P4 ladder; a failed read leaves the item unknown and holds only its own thread (`lib/channels/outbox.py:490`) | **Adapt**: P4's evidence standard, `LookupResult`/`HistoryQuery` semantics, `reconciliation_read_failure` and `stalled_thread_isolation` (both pass) |
| Another path after a failure | "Failure is not permission to send the same payload through another path" (`openclaw:docs/plugins/sdk-channel-outbound.md`, Durable sends) | 大象 reconciles a lost robot answer under the robot (`lib/channels/dx/robot.py:350`); P7 named only the fallback triggers | **Adopt**: P7 and `x-identities`; `unknown_keeps_its_identity` (pass for 大象) |
| Recognizing own sends | A 30-second in-memory window of recent outbound IDs (`openclaw:src/channels/message/outbound-echo.ts:13`) | Durable echo registry, markers and `hostSent`; a displacement window, specified and not yet built (P12) | **Reject** the window: the 2026-11-09 replay came through a catch-up long after 30 s |
| Durable ingress | The raw event is appended before the transport ack or cursor; transports are ack-gated, awaited-cursor or non-replay; QQ's resume sequence is saved after admission (`openclaw:docs/plugins/sdk-channel-plugins/durable-ingress.md`, `openclaw:docs/channels/qqbot.md`) | Recorded before routing, checkpoint after ingest (P2, C1); QQ's session and sequence only in memory (`lib/channels/qq/transport.py:129`) | **Adapt** in migration step 4: QQ's checkpoint carries them |
| Replayed side effects | Non-repeatable effects keyed by event ID and effect name (`createIngressEffectOnce`, same durable-ingress page) | Receipts keyed `receipt:<event id>` (`lib/channels/autoreply.py:94`); Turn commands derived from event IDs (C4) | **Keep** |
| Restarts | One resume input per interrupted session; queued input re-admitted under its sender's current authority; produced output keeps its queue (`openclaw:docs/gateway/restart-recovery.md`) | Restart resume (R10), outbox reconciliation (P4) | **Keep** |
| One live session | One Gateway per state directory; a Slack app shared by gateways hands each event to any connection unless one relay owns ingress (`openclaw:docs/gateway/gateway-lock.md`, `openclaw:docs/channels/slack/transports.md`) | One live session per platform account, `connector_owned` refusals (P11) | **Keep**; ours is per account |

**Cross-channel model and Tofu's own requirements**

| Behaviour | OpenClaw | Tofu | Verdict |
|---|---|---|---|
| Who decides security | Channel plugins own DM policy, allowlists and pairing; the core owns queueing, durability, retry, receipts and the one `message` tool (`openclaw:docs/plugins/sdk-channel-plugins.md`, `openclaw:docs/plugins/sdk-channel-outbound.md`) | Adapters translate; the core owns authority (P1) | **Keep**: the 微信 plugin's fail-open admission is what per-plugin security costs |
| Capability truthfulness | Declared capabilities need contract-test proofs (`openclaw:docs/plugins/sdk-channel-plugins/message-adapter.md`) | Descriptor plus conformance baseline; `n/a` needs a platform reason (P5, Conformance) | **Keep** |
| Shared bot credentials | Slack relay mode: a trusted router owns the one ingress connection; each gateway sends with its own token (`openclaw:docs/channels/slack/transports.md`) | One robot posts for many servers; robot inbound waits for a relay (C6, P8) | **Keep**; it confirms P8's relay-as-adapter |
| Versioning | Plugins declare a host range; 微信 2.4.6 stopped loading when an SDK path was retired (`openclaw:docs/channels/wechat.md`, Compatibility) | N and N−1 negotiated at `initialize` (P10) | **Keep** |
| Export hygiene | Pairing approvals and tokens live in the state directory (`openclaw:docs/channels/pairing.md`) | Reserved `channel.*` vault entries are never exported (P8) | **Keep** |
| Attachments | The core applies outbound media-access policy to declared media parameters; a platform that fetches media by URL gets a temporary tokened URL from a hosted media store (`openclaw:docs/plugins/sdk-channel-plugins/status-and-media.md`) | The core admits files from declared sources (P13); 大象 files go natively from the owner's session, since the robot takes only URLs (C10, P7) | **Keep**: a fetch URL needs a host the platform and the phone can reach, which a private Tofu server is not (owner trade-off on 大象 files, below) |
| Several people | Named sender groups reused across channels (`accessGroups`, `openclaw:docs/channels/pairing.md`); attested links from a person's profile to channel identities (`openclaw:docs/concepts/queue.md`) | One owner per server; identity an explicit (owner, channel) parameter (P8; `TODO(enterprise)` in CHANNELS.md C7) | Later, for enterprise use: an owner-scoped identity link is the seam; nothing now |
| 微信 session expiry | `-14` (stale token) pauses the account for an hour, then retries (`openclaw-weixin:src/api/session-guard.ts:3`) | `-14` is `needs_login`: the owner scans again (`lib/channels/wechat/transport.py:86`) | **Keep**: no timer; revisit only if `-14` proves transient |
| Typing and live previews | Typing on enqueue; previews edited in place (`openclaw:docs/plugins/sdk-channel-plugins/status-and-media.md`, `openclaw:docs/plugins/sdk-channel-plugins/message-adapter.md`) | Text receipts and mirrored progress (C5); no typing capability | Later, as an optional capability where a platform offers one (iLink `sendtyping`); 大象's own account has none |

## Open product trade-offs (the owner decides; defaults in force until then)

- **Who sends files on 大象.** The robot's API takes files only as URLs, so
  native files come from the owner's account. Default: keep native files from
  the owner's account, recorded and shown as 由你的账号代发, with the robot's
  text after them. Alternative: the robot posts a link to a Tofu download
  URL (the phone must reach the Tofu host).
- **Unknown sends where nothing can be reconciled** (企业微信, 微信; QQ outside
  its reply window). Default: hold and ask the owner (no duplicate, one tap).
  Alternative: today's resend (no miss, possible duplicate to other people).
- **Recalled messages.** Default: Tofu marks them recalled and deletes their
  text. Alternative: keep the text as context.
- **Edited owner commands.** Default: an edit is recorded and shown to the
  Router as context, never re-executed. Alternative: treat an edit of an
  unanswered command as the command.
- **Attachment sources.** Default: the Global Router may attach files from
  any of the owner's registered projects, with provenance shown.
  Alternative: only its own workspace plus an explicit copy step.
- **Owner-account messages during a displacement.** Default: delivered as
  context, never commands. Alternative: deliver them as commands.
- **Feishu.** Today non-owners can chat with Tofu through Feishu's
  third-party profile; on the channel core others are silent by default (C11).
  Default: keep Feishu as it is until the owner decides; migration step 5
  waits.
- **Deterministic controls in the command chat.** OpenClaw aborts a run on a
  whole-message stop word and settles `/approve <id>` without a model call
  (Reference design: OpenClaw). Tofu sends both through a Router Turn (about
  six large-context requests, ROUTER.md R10): an approval waits behind a busy
  Router, and a stop depends on the model honouring it. Default: unchanged;
  every owner message goes to the Router (C2 and C11 reject parsing at
  ingress). Alternative: two exact whole-message forms from the bound owner
  in the command chat, 「停止」 stopping the running Router Turn and
  「批准 <code>」/「放弃 <code>」 settling that draft, everything else to the
  Router as today.
