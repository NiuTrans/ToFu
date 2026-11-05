# Routers: always-on, event-driven coordination

Responsibility: the Global Router and Project Router roles, forwarded human
requests, report relay, standing instructions, the long-lived context
contract that lets a router conversation run indefinitely, and the Router
Watchdog that monitors them. Channel ingress,
threads, subscriptions and the outbox are owned by [CHANNELS.md](CHANNELS.md).
Project workers, grants, delegation and reports remain owned by
[PROJECT_COORDINATION.md](PROJECT_COORDINATION.md); this document only adds the
router layer on top of them.

## Vocabulary

| Term | Meaning |
|---|---|
| **Router** | A long-lived, dispatch-only conversation that owns human-facing replies and coordination. It never executes concrete work itself. |
| **Project Router** | The existing bound Project Coordinator of one owner/project (`coordinator-*`). |
| **Global Router** | One per owner. The bound Coordinator of the owner's **router home** workspace, with the router addendum and router tools. Front door for Tofu and channels. |
| **Router home** | An owner-scoped Git repository created and owned by Tofu, used as the Global Router's project root (scratch worktrees for non-project work, project messages, dispatch records). |
| **Worker** | An ordinary, context-isolated conversation that executes one assignment. It has no human-interaction tools, but stays visible and the human may open and talk to it at any time. |
| **Forwarded request** | One genuine human message of the current Global Router request, delivered verbatim into a Project Router (`route_to_project`). |
| **Standing instruction** | Owner-authored text captured from a genuine human turn that lets the Global Router forward matching future channel events as work, without asking again. |
| **Report relay** | Project Router reports surfaced to the Global Router as passive notices. |
| **Watchdog** | One per owner. The bound Coordinator of the `.tofu-watchdog` path inside the router home with the narrowest tool ceiling: it reads health, judges faults and Worker progress, and messages or notifies routers. It never executes, delegates, cancels or acts as the owner. |
| **Fault signal** | A durable, idempotent record of one infrastructure fault an existing owner observed (failed Turn, recovery failure, outbox send, channel state); a new one wakes the Watchdog. |

The owner never sees these internal names (R8): the Global Router is the
**main coordinator** (总负责人), a Project Router is the **project
coordinator** (项目负责人), a Worker is a **worker chat** (执行会话) and the
Watchdog is **health checks** (故障巡检).

Topology:

```
            Tofu UI ─┐                     ┌─ 大象 command chat (owner)
                     ▼                     ▼
               ┌──────────── Global Router ────────────┐   ◀─ channel events (CHANNELS.md)
               │ dispatch-only, owns replies/outbox     │   ─▶ outbox replies
               └──────┬────────────────┬────────────────┘
          route_to_project      delegate_project_work (router home)
                      ▼                ▼
              Project Router(s)    Workers (home worktrees)
                      │ delegate_project_work
                      ▼
                 Workers (project worktrees)

  Watchdog (router home/.tofu-watchdog) ◀─ fault signals, scheduled reviews, re-checks
      │ watchdog_message ─▶ any Router or Worker mailbox (nonhuman context)
      └ watchdog_notify_router ─▶ Global Router (peer input)
```

## Decisions

### R1. A router is a Coordinator; the Global Router is the home Coordinator

**Problem:** the article-style Router/Worker split needs a resident, dispatch-only
session with context-isolated workers, durable reports, a progress view for the
human, questions that proceed on a stated default, result-driven continuation
and enforced tool policy. Tofu already has all of these in the Project
Coordinator, but only per project.

**Decision:** the Global Router is the bound Coordinator of the owner's router
home (`lib/router/home.py`), selected by the ordinary
`ProjectCoordinationRepository` binding on that path. No new binding namespace,
model loop, task state machine or presentation state machine exists. The router
role is the coordinator role plus a router addendum; its tool ceiling is
`ROUTER_TOOLS` (`COORDINATOR_TOOLS` plus the routing and channel tools), enforced by the same
`coordinator_call_rejection` / `filter_coordinator_tools` boundary. Non-project
work (summaries, drafts, investigations) is delegated to ordinary Workers in
home worktrees, exactly like project work.

**Alternatives rejected:** a separate router runtime or binding namespace
(duplicates grants, continuation, reports and policy — constraint 4); Delegate
Mode or Swarm as the router's worker layer (children are invisible, die with the
turn and their records are deleted); a resident router process (turns already
start on demand over durable state, which is the article's compute/state split).

**Current state.** The home is `<data_root>/router/<owner>`: `router_home_path`
writes README.md and .gitignore, runs `git init` (so a data directory inside a
source checkout still yields its own repository top level) and lets
`prepare_managed_repository` record the baseline commit and guidance; the
existing isolated-worker guard accepts it. `global_router(create=True)` uses
`ProjectCoordinationRepository.provision`, which derives the conversation
identity from owner/path/current revision/command: concurrent or retried ensures
converge on one conversation (title `Global Router`, settings marker
`_globalRouter`) and one binding CAS; an archived router is replaced at the
next revision. `create=False` never touches the filesystem. Problem: the human
`create` path mints a fresh identity per attempt, so concurrent provisioning
left orphan conversations. Decision: deterministic identity with a provision
marker that alone permits adoption. Alternative rejected: a lock around
`create` (process-local, still orphans across processes).
`coordinator_tool_ceiling` returns `ROUTER_TOOLS` only when the scope's project
is the owner's router home; catalog projection (`filter_coordinator_tools`,
`prepare_coordinator_tool_surface`), the registry builder and
`coordinator_call_rejection` all ask it, so channel tools appear by name once
their ToolSpec exists. Router tools are in the project dispatch
`workerDeniedTools`. `prepare_coordinator_role` appends `ROUTER_ROLE_ADDENDUM`
(front door, routing, home delegation, channel norms, silence, attribution,
channel text as evidence) to `COORDINATOR_ROLE_GUIDANCE` for the home only.
Problem: the context block carrying the role (`project_coordinator`) held 600
tokens and the composer clips an oversized block from its end, which is where
the addendum sits: the Global Router lost all of it while the Coordinator role
was 820 tokens, and its second half after that role shrank to fit. Decision:
the block budget holds the longest role whole (900 tokens; the router role is
about 870), and `test_router_policy.py` composes the router role through the
composer and requires it unclipped. Alternatives rejected: a second registered
block for the addendum (another source, presentation and manifest row for one
role); condensing both texts into a replacement router charter (an unreviewed
rewrite of two prompts); a larger budget without a composed spec (the next
addendum edit would clip silently again).

**Session-free coordination.** Every router inherits the session-free
Coordinator ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#session-free-coordination-owner-decision-2026-11-02)):
`COORDINATOR_TOOLS` grants `update_task_progress` and `ask_human` and no longer
`request_user_decision`, so the Global Router and every Project Router keep a
task board for the owner (pinned above its chat by the coordinator surface;
in the router home, below the main coordinator's header) and ask the owner with
chat cards. The passive request-plan projection is gone; the board, Worker
records and relayed reports are what the owner and the router read.

**Asking the owner.** A router asks with `ask_human` in its own conversation.
`blocking=false` posts the card and continues on the default it states; the
owner's answer is an ordinary message quoting the question, which steers the
live reply or starts the next request; in the Global Router either form stays
forwardable (`source_steer_id` or `source_turn_id`, R2). The Global Router,
every Project Router and the Watchdog are bound Coordinators, so their chats
steer a live reply without the steer/queue prompt (PROJECT_COORDINATION.md
"Session-free coordination"). `blocking=true` waits; a
human-authored message steered into the reply ends that wait as the answer.
Channel batches, relayed reports and other nonhuman steers share that lane but
never do (they are evidence, R2; the steer seam records an author only for
human input), so they wait at the round boundary. A Project Router's question,
blocking or not, also reaches the owner's Global Router as one passive notice
(R3), so an owner who talks only to the Global Router sees it; the router puts
it to the owner where they talk to it and forwards their answer verbatim with
`route_to_project`. Such a forward is the owner's own message only when
`require_human_turn` verifies it (R2): steered into the waiting reply, it ends
the wait exactly like the owner typing in the project chat, while an
unverifiable forward waits like any nonhuman steer. Cards render only in Tofu:
on 大象 the router sends the question to the owner's command chat with
`channel_send` (R3), and its answer to an owner command-chat message returns
there by C5 auto-reply, never as a card.

### R2. Forwarded human requests carry human authority only verbatim

**Problem:** the Global Router must hand project work to a Project Router, but
Project Router grants require a genuine human source Turn and synthetic input
can never authorize Workers.

**Decision:** `route_to_project(project_path, source_turn_id | source_steer_id,
note?)` forwards one human message of the router's *current* request (its own
Turn, or a message the owner steered into the running reply) into the Project Router of that
project (creating the binding and its empty conversation when absent). The
forwarded input is byte-identical to the human text and is submitted through the
ordinary turn command (`dispatch_scheduled_turn`, steer when busy) with
`origin = {initiator: 'human', forwardedFrom: {conversationId, turnId}}`.
`require_human_turn` admits a forwarded origin only when the source Turn is a
genuine human Turn of the same owner in the owner's bound Global Router and the
text is identical; it rejects every other cross-conversation origin as before.
The router's optional `note` travels as separate nonhuman context and never
becomes authority. Forwarding is idempotent per (source turn, project).

**Alternatives rejected:** cross-conversation grant sources (every grant reader
would change); synthetic forwards (cannot delegate, so useless); letting the
Global Router delegate inside other projects directly (two dispatchers per
project break the one-binding epoch fences); router paraphrases as authority
(prompt injection could mint instructions).

**Current state.** `TurnOrigin.forwardedFrom {conversationId, turnId,
instructionId?}` is part of `contracts/conversation_sync_v3.yaml` and survives
projection normalization only when well formed. `route_to_project` resolves the
current request's human Turn exactly as delegation does (`source_for_task`), so
synthetic input cannot forward; it forwards that Turn's authored text
(`originalContent` before any input translation). The command id hashes
(router conversation, source, project path). A new Project Router is provisioned
when the project has none (deterministic identity, no folder; it inherits the
router request's composer capability switches so its Workers keep them). The
note and an instruction's trigger are one peer-initiated input with command
id `<forward>:context`, queued behind a queued forward so it never steers an
unrelated live request. `require_human_turn(turn, user_id=...)` admits a
forwarded origin only through `verify_forwarded_turn`: the owner's currently
bound router, an existing genuine human source Turn that was not itself
forwarded, a different target conversation, identical authored text and, for
instructions, a matching record; a call without the owner refuses.
`ProjectDispatchRepository.validate_human_source` passes its owner, so every
grant fence repeats this check.

**Steered owner messages.** Problem: a message the owner steers into the
router's running reply has no Turn of its own, and
the steer lane kept only a 1,200-character display preview, so it could not be
forwarded verbatim. Decision: the steer itself carries a durable author.
`turn.steer.commit` (SQL and native, one transaction with the user-steer
record) stores `TurnSteerAuthor {actor, text, origin?}`
(`contracts/conversation_sync_v3.yaml`) for human-initiated input only, computed
at the single steer seam (`command_service._deliver_while_running` via
`turn_initiation.steer_author`): the input actor, the complete authored text
(`originalContent` before translation) and the origin normalized exactly like a
Turn's. Nonhuman, program-synthetic and text-less steers (channel event
batches, report relays, forward context) record none. Executor merges and client
whole-projection edits keep the stored value and cannot add one
(`_merge_user_steer_records`, `projection_with_committed_steer_authors`). The
steer is named by its command id inside the reply Turn: `forwardedFrom =
{conversationId, turnId: <reply Turn>, steerId}`, idempotent per (router
conversation, `{turnId, steerId}`, project). `route_to_project(source_steer_id)`
accepts only a steer of the task's own executing reply (current binding, the
task's live attempt, not stopped), whatever started that reply, because the
steer's author is the authority. `turn_initiation.steered_input_turn` turns the
record into a Turn-shaped view so `require_human_turn` and `verify_forwarded_turn`
apply the same checks as for a Turn: owner's bound router, genuine human, not
itself forwarded, identical text, another conversation; a queued steer (missed
the reply and became the next request), a steer on a human Turn, and `steerId`
with `instructionId` are refused. In the router's model view
(`orchestrator._swarm_inbox`), `owner_steer_carriers` prefixes each such owner
message with `[Owner message sent while you were replying · steer_id=<id>]`; the
role addendum tells the model to forward it with `source_steer_id`.
Alternatives rejected: turning every steer into its own Turn (changes transcript
and delivery semantics everywhere), a separate steer-author store (non-atomic
with the steer and a second authority), matching the steered text supplied by
the model (the model would have to reproduce the owner's bytes), and labelling
steers in every conversation (changes the prompt of every chat for a router-only
tool). Standing instructions are still created only from a whole Turn.

### R3. Project reports and questions relay to the Global Router

**Problem:** the owner talks to one agent; results produced by a Project Router
must reach the owner's current channel without the owner watching every
project conversation.

**Decision:** every published Project Router report (`report_to_user`) also
records a passive notice for the owner's Global Router (project, report kind,
report Turn ID, short body) and wakes it with a nonhuman initiator. The Global
Router decides whether and where to tell the owner (channel norms). The project
report Turn remains the canonical record; the notice is a pointer.

**Alternatives rejected:** polling project conversations (paid polling); routing
every report to 大象 directly (no judgment, violates low-disturbance norms).

**Current state.** `CoordinatorReports._deliver` calls `relay_project_report`
after the report Turn and attention pointer commit and before the publication
intent retires, so boot recovery replays a relay interrupted by a crash. The
notice is one `dispatch_scheduled_turn` input stamped `peer` with
`origin.sourceConversationId` = Project Router; it inlines project path,
Project Router conversation, report kind, report Turn ID, title and the full
report message (already bounded by the report contract). The command id hashes
(owner, Project Router conversation, report Turn), so replays deliver once.
Reports of the router home, and owners without an available Global Router,
relay nothing: relaying never creates a router. A relay failure is logged and
never fails publication. A relayed notice, like a colleague's channel event, is
a nonhuman request: it opens no request account (`bind_source_account` returns
nothing for a `SyntheticSource`), so an idle router starts its turn normally,
and delegation from that request is refused as before. Problem: the account
binding at task start treated every non-human source as an authority failure,
so an idle router could not start a turn for a relay or channel event at all
(steering into a live turn still worked). Decision: a typed `SyntheticSource`
refusal that the account binding, and only it, maps to "no account".
Alternative rejected: skipping the binding for routers (a human request would
lose its usage account and fences).

**Reviewed notices can conclude without an answer.** An exact `[SILENT]` is a
declared outcome only for a bound Global Router Turn whose parent is a
nonhuman channel/peer notice. The engine checks the durable parent and every
committed or pending steer, including its live author carrier; a genuine human
request, missing or failed evidence, an unknown initiator or any human steer
refuses the declaration and uses ordinary answer recovery. Ordinary prose that
mentions the token is never rewritten. Authorized silence publishes a
`deliberate-silence` system note, retains the provider declaration for replay
and has no deliverable text or automatic completion message. It cannot bypass
reply policy, drafts, approval or a required owner decision. Python and Native
stream/terminal projections share this policy; incomplete split markers flush
as ordinary text at structural boundaries, and discarded attempts drop their
uncommitted candidates. Automatic review failures/interruption send an
out-of-band 大象 command-chat warning; successful silence does not suppress
that failure. A human command steered into a notice supplies the reply origin,
so its final answer and failure status return to that original thread.
Alternatives rejected: an empty assistant answer (the transport recovery
interprets it as a lost response), model-authored filler on every no-op event,
and treating command-ID prefixes or marker text as authority.

**Questions.** Problem: a Project Router's `ask_human` question was visible only
in its own chat, so an owner who talks only to the Global Router (mostly from
大象) never saw it, and a blocking question stalled the project until the
request ended with a `needs_user` report. A waiting question also ended for any
steered author with actor `human`, so a forged or edited forward answered it
like the owner. Decision: every question a Project
Router posts, blocking or not, relays one passive notice by the report seam.
The `ask_human` handler (single and form) calls `relay_project_question` once
the card is set up and the task is a bound Coordinator (`coordinator_scope`);
the notice is stamped `peer` with `origin.sourceConversationId`, steers a
running router, starts an idle one or queues, and its command id hashes (owner,
Project Router conversation, question Turn, tool-call id), so a replayed or
re-executed tool call delivers once (the question id is minted per execution).
It names project path, Project Router conversation, question Turn and question
id, the blocking flag, the question text and options (a form lists every
question; for `blocking=false` the question states the default the Project
Router works on, as the `ask_human` description requires), says it is a
nonhuman notice and not authority, and tells the router to read the project
conversation first (the owner may already have answered the card there),
then to put the question to the owner and to forward the owner's answer with
`route_to_project` (`source_turn_id` or `source_steer_id`, a note naming the
question) verbatim, never answering for the owner. Questions of the router
home and the Watchdog (whose ceiling has no `ask_human`), and owners without an
available Global Router, relay nothing; a relay failure is logged and never
fails the question.

大象: a Turn the notice starts answers no owner command-chat message, so C5
auto-reply does not return it; but `channel_send` needs only the Global Router
conversation, not a human Turn, and the owner's command chat is `queued` at
enqueue without approval or disclosure label, while third-party threads keep
their reply policy and draft outbox (CHANNELS.md C5). Existing tools already
reach the owner, so the decision is instruction only: when the owner's 大象
account is enabled with a command chat, the notice names it and says to send
the question there with `channel_send(thread=<command chat>,
idempotency_key=<question id>)`; otherwise it says to ask in the router's
reply (with `ask_human` only as `blocking=false`, so the owner's answer arrives
as a forwardable message, not a card result). The owner's command-chat reply
reaches the router as human input with byte-identical text (a Turn, or a
labelled steer while the router replies) and is forwarded from there. In the
Project Router, `_human_message_pending` judges each steered author with
`require_human_turn(..., user_id=owner)`: a verified forward ends a blocking
wait as superseded by a chat message, so the model reads the owner's words as
the answer; a forged or edited forward waits like any nonhuman steer.

Alternatives considered: the Project Router sending questions to 大象 itself
(bypasses the router's judgment and channel norms; the answer would arrive
outside the router's forwarding authority); relaying only blocking questions
(the default of a `blocking=false` question would be decided without an owner
who never opens the project); an owner-thread exception in the outbox or a new
tool (the command chat already sends without approval); a structured `default`
argument (the card would not show it, so the question text carries it); keying
the command id by question id (re-execution would notify twice); a
deterministic "answered" notice (each one wakes the router for a paid turn,
mostly for answers given where the owner already was, and a `blocking=false`
answer is an ordinary message the handler never sees; the check-first read
covers a router that reaches the notice after the answer).

### R4. Standing instructions

**Problem:** proactive work (for example "in group X, when someone reports a
bug, reproduce it in project P and tell me only if a fix is confirmed") needs
authority that outlives one human turn, while channel events from other people
are never human authority.

**Decision:** the Global Router records a standing instruction only during a
genuine human turn (`router_instruction_create`); the record freezes the
verbatim owner text, its source Turn, an optional thread scope and target
project. The owner lists, pauses, resumes or cancels instructions in Tofu. When a channel
event matches (router judgment), `route_to_project(..., instruction_id)`
forwards the frozen instruction text verbatim plus the event as nonhuman
evidence; the instruction's source Turn is the authority. Every application is
recorded with the triggering event ID. The thread scope may be any channel
thread, including a GitHub issue or pull request: GitHub comments, the
owner's own included, are only evidence (CHANNELS.md C8), so an instruction
scoped to `github:<owner>/<repo>#<number>` is how the owner lets a later pull
request comment change work that an IM message started.

**Alternatives rejected:** treating router judgment as authority; deterministic
automation triggers only (cannot express "someone reports a bug"); renewing a
grant silently. Convergence with `project_dispatch/responsibilities.py`
(deterministic automation triggers, project scope) is a future unification, not
a reason to block the router path.

**Current state.** `RouterInstructions` stores one record per instruction
(`router.instruction.v1`) and one per application
(`router.instruction.application.v1`) in portable record CAS, so history has no
lifetime cap. The frozen text is the whole authored text of the source human
Turn (byte-identical, so the same forwarded-origin check verifies it); the
router adds a presentation label, an optional thread key and an optional target
project. Creating and cancelling from the router require the current request to
be a genuine human Turn; applying does not (the trigger is usually a channel
event), but needs an active instruction of the current router binding whose
source Turn is unchanged, an `event_id` or trigger `source_turn_id`, and the
instruction's project when it names one; an instruction that names no project
applies only to projects that already have a Project Router, so channel text
cannot steer it into creating one. Applications are idempotent per
(instruction, trigger, project). The owner lists, pauses, resumes and cancels
instructions over HTTP and in the router header (R5 UI); pause and resume are
owner-only (no model tool). States are `active`, `paused` (with `pausedAt`, null
after a resume) and the final `cancelled`; transitions are idempotent CAS writes
and a cancelled instruction can be neither paused nor resumed. Applying a paused
instruction is refused with a message telling the router to wait for the owner;
lists show active and paused instructions by default. Pausing and cancellation
stop future applications only; work already forwarded keeps the authority it was
admitted with (`verify_forwarded_turn` checks the record's text, not its state).

### R5. Router and Worker models are chosen separately

**Problem:** Workers inherit the Coordinator's model, so choosing a light,
fast router model downgrades all execution.

**Decision:** the coordinator conversation setting `workerModel` (validated by
the ordinary route compiler) selects the model for Workers it delegates;
absent, Workers use the owner's default model, not the router's. Models never
choose models.

**Current state.** `workerModel` is a server-authored key of
`contracts/conversation_settings_v1.json`, written only by
`ProjectCoordinationRepository.set_worker_model` (HTTP
`POST /api/v4/router/worker-model`) after `admit_worker_model` resolves it in
the owner's routing document; `null` clears it. `GET
/api/v4/router/worker-model?path=` reads it for any bound router. The owner
sees and sets it in the browser (`frontend/src/features/router-worker-model.ts`):
one compact labelled select on a single row (its closed value is the current
model, its tooltip the explanation) whose choices are the composer model
picker's catalog and visibility policy (hidden models, chat capability,
display-name order and labels), never a second model list; its first option,
the owner's default model, sends `null`, a refused choice keeps the stored
value with the server's message, and a failed read shows its error beside a
retry. The Global Router's control is in its header;
every other Project Router's leads the footer of the task board on its
coordinator conversation surface, beside "Manage work" rather than on a line
of its own (the board starts folded, so the control shows once it is opened),
where it stays attached across repaints and reads once per visit; the Watchdog
conversation has none. A
conversation is the Global Router when its project path is the router home the
Global Router read last reported (the `_globalRouter` setting is a server-side
presentation marker the settings contract does not project to the browser).
`CoordinatorWorkerPolicy`
separates `model` (Workers) from `coordinator_model` (the Coordinator's own
continuations). Tool assembly never reads storage for it: the first delegation
of a request resolves `resolve_worker_model` (setting, else the owner's default
model, else an explicit refusal) and pins it; continuation controls record
`workerModel` (grants issued earlier, without it, keep their single model). The
owner default is the server's configured default model today
(TODO(enterprise): a per-owner default).

### R6. Long-lived context: a router conversation runs indefinitely

**Problem:** a router lives for months. Automatic L2 compaction already bounds
the model view per request and each completed turn publishes a durable
continuation checkpoint, so manual compaction was never the long-term
mechanism. Several ceilings still made long-lived conversations degrade.

**Decision:** each ceiling below is removed structurally; none is raised.

| Ceiling | Decision | Owner |
|---|---|---|
| Continuation proof limited to 10,000 Turns (`count < 10000`, `MAX_LIST_ROWS` on the prefix, `MAX_SCAN_PAGES`) | Frontier count is a plain counter; cold scans stream the digest through all headers; only the uncovered suffix is materialized and paged | [CONTEXT_COMPACTION.md](CONTEXT_COMPACTION.md), TofuDB `turn/model_context` |
| Turns with a human steer never publish a checkpoint | Steered turns publish their verified model view | `model_replay.py` |
| Manual compaction refused for coordinator and every project conversation | Protected conversations schedule a non-destructive working-view compaction applied at the next model call; canonical Turns are never deleted | `compaction_policy.py`, compaction pipeline |
| A policy change discards the checkpoint and rebuilds the whole history | Reuse the previous checkpoint's portable text; rebuild only the uncovered suffix | `model_replay.py` |
| A conversation cannot recall its own out-of-view history | `get_conversation` reads the current conversation's turns; summary carriers name the covered range and recall cursor | `conv_ref`, L2 carrier |
| Project mailbox holds 128 messages per recipient for its lifetime | Segmented history without a lifetime cap | [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md) |
| Workers cannot purchase auxiliary summaries | Workers summarize under their own grant's existing ExecutionBudget | `_summary_accounting.py` |

### R7. The Router Watchdog is an independent monitor session

**Problem:** routers and Workers fail in ways nobody watches. A Worker's turn
dies on a provider error, a Project Router's own turn fails, dispatch recovery
cannot retry a grant, the 大象 bridge disconnects or a reply sits in the outbox
as `failed`/`unknown`; a Worker loops, stalls or drifts from its assignment.
The Global Router wakes only on human input, channel events and reports, and
must not fill its long-lived context with routine health; the owner learns of
a fault only by opening each conversation.

**Decision:** the Watchdog is the ordinary bound Coordinator of one more
owner-scoped path, `<router home>/.tofu-watchdog` (ignored by the home's
`.tofu*/` rule), provisioned like the Global Router (deterministic identity,
title `Watchdog`, settings marker `_routerWatchdog`). It reuses the binding,
role, tool-ceiling and turn machinery: `coordinator_tool_ceiling` returns the
narrowest ceiling, `WATCHDOG_TOOLS` (its `watchdog_*` tools plus
`list_conversations`, `get_conversation`, `search_tools`, `execute_tools`; no
`update_task_progress` or `ask_human`: it owns no tasks and reaches the owner
through the Global Router or its own chat), and
`prepare_coordinator_role` replaces the delegation charter with
`WATCHDOG_ROLE`. It is driven by timed and event messages, all nonhuman input
through `dispatch_scheduled_turn(..., inject_mode='steer')`:

- **Fault signals.** Existing owners report faults they already observe; each
  becomes one `watchdogSignal` record keyed by a deterministic identity in
  portable record CAS, so replays never duplicate a signal and a resolved one
  never reopens. A new signal wakes the Watchdog with the `peer` initiator and
  command id `router-watchdog-signal:<signalId>`. The signal is the authority,
  the wake a hint. Sources: failed Turns of router-managed conversations,
  dispatch recovery, outbox sends, channel accounts, and (`compute`) a parked
  Evolution run that cannot read or verify its registered compute job; one
  failure class per failure streak is one signal, and only the reporting run
  closes it, on its first successful read, the end of the wait or cancellation
  ([environment providers](ENVIRONMENT_PROVIDERS.md#durable-compute-dependency)).
  The studio's stall dialogue opens the Watchdog through the same ensure as the
  sidebar entry.
- **Periodic review.** An owner-visible keyed scheduler task (type
  `router_watchdog_review`). Its run builds the deterministic health snapshot
  and wakes the Watchdog (`proactive`, one command id per task and minute)
  only when something needs judgment.
- **Re-checks.** The Watchdog arms one-shot `once:` scheduler tasks for
  intermittent faults and keeps re-arming until recovery; a re-check always
  wakes it.

Interventions use only existing authority-safe paths: a project-mailbox message
to any Router or Worker of the owner (nonhuman context at the recipient's next
step), or one peer notice to the Global Router, which decides whether, when and
where the owner hears about it. The Watchdog never executes, delegates, cancels,
holds a grant or speaks as the owner; correction, re-delegation and
cancellation stay with each Worker's Coordinator and the human. The owner turns
health checks on, and opens the Watchdog to ask it questions like any
conversation, from the main coordinator's header (R8).

**Alternatives rejected:** monitoring inside the Global Router (routine health
floods its indefinite context and a failing router cannot watch itself); a
separate monitor runtime, daemon or polling loop (a second model loop and task
state machine, constraint 4); fixed-interval model polling regardless of state
(paid turns while idle, so idle reviews end without a model turn); letting the
Watchdog cancel or re-delegate Workers (it would need grants, and synthetic
input never gains human authority); a Watchdog-private timer or backoff (a new
interval knob; the owner-visible scheduler is the one timer); creating the
review enabled when the Watchdog is first opened (a standing paid schedule the
owner did not choose, through a route that lacks the scheduler scope).

**Current state.** Signal sources and identities (`lib/router/watchdog/signals.py`):

| Source | Reporter | Identity (one signal per) |
|---|---|---|
| `turn` | `turn_lifecycle` after a committed `failed` terminal settlement, off the settlement path | conversation, Turn, Attempt; only router-managed conversations (Global Router, Project Router, Worker, the Watchdog itself) and infrastructure kinds (`watchdogTurnErrorKind`) |
| `recovery` | `project_dispatch/recovery.py` per assignment and per startup scope | project, assignment and grant revision, error class and code, startup episode; authority refusals (`PermissionError`/`ValueError`) are the pass's ordinary skips |
| `outbox` | `channels/outbox.py` when a send settles | item and state (`failed`, `unknown`) |
| `channel` | each channel supervisor's scheduler tick (`reconcile_channel_bridges` for 大象, `reconcile_github_sessions` for GitHub) | channel, unhealthy state and last connection time; detail text changes every restart and is not identity |

Reporting never creates a Watchdog (owners without an available one record
nothing) and never raises into the reporter. Open signals keep an index row
carrying source and conversation, so a conversation's open Turn failures are
found without reading every signal. A signal about the Watchdog's own Turn
never wakes it; while that signal is open and the periodic review is enabled,
new signals do not wake it either (the review will), and its next
`watchdog_snapshot` closes its own failures. Without an enabled review every new
signal still wakes it, so a failure cannot silence it for good. A wake lost
after its signal committed surfaces at the next review or snapshot.
`health_snapshot` (`snapshot.py`) is a deterministic read with no model call or
side effect: the Global Router and a page of Project Routers (availability,
last activity, open signal IDs) with their Workers of the current binding
epoch that are active, failed or have a held delivery (objective, status, last
activity, the settled Turn's error); outbox items in `failed`/`unknown`;
channel account and bridge state; and a page of open signals. Pages follow
the project dispatch history page. The periodic review is the owner's keyed
task `system-<owner>-router-watchdog-review` (`scheduler.task.ensure`, so
retries converge): off until the owner enables it in the health-check settings,
`POST /api/v4/router/watchdog/review` (scope `agents:scheduler`) or by asking the
Watchdog (`watchdog_configure_review`, admitted only while the current request
is the owner's own genuine message, resolved like delegation authority); the
contract default cadence is `7,37 * * * *`. A review whose snapshot holds no
active, failed or held Worker, open signal, outbox item, unhealthy channel or
unreadable router ends without a model turn. A re-check is a `once:` task with
the deterministic id `watchdog-recheck-<digest(owner, Watchdog, key)>`; the
scheduler disables it after it fires. Watchdog-armed tasks use a
least-privilege `agents:scheduler` principal, like the scheduler tools.
`watchdog_message` resolves the target's role from durable metadata and
submits to that project's mailbox with the Watchdog as sender (labelled
nonhuman, kinds `reference`/`question`/`steer`, idempotent per key); mailbox
delivery admits the owner's currently bound Watchdog as a sender in every
project and nobody else from outside it. `watchdog_notify_router` is one peer
input with `origin.sourceConversationId` = Watchdog, command id hashed from
(owner, Watchdog, key, message, signals), refused while the Global Router's own
failed-Turn signal is open. The Global Router neither lists the watchdog path
as a project nor forwards into it, and Workers are denied every `watchdog_*`
tool. TODO(enterprise): channel signals ride the personal scheduler tick that
hosts the bridges.

### R8. The owner sees one kind of coordinator, not routers and a watchdog

**Problem:** the first browser surface exposed the architecture: two pinned
sidebar buttons (全局路由, 看门狗) above New Chat, a separate 项目负责人 ·
Coordinator card in the list, a header that repeated the router's name next to
the English title "Global Router", and the router home's directory name (`1`)
as the chat's project location. Owners could not tell how the three related or
what the Watchdog was for, and the extra chrome cost space on every screen.

**Decision:** one role, two scopes, one slot. The Global Router is shown as
the main coordinator (总负责人) and a Project Router as the project
coordinator (项目负责人), with the same card in the same place: the leader
slot above the conversation list holds the project's coordinator when the
listed conversations belong to a project, and the main coordinator otherwise
(an uncategorized or plain folder view, or a router-managed chat). Neither
chat is also a list row. Both available coordinator cards expose a localized
"Copy conversation ID" action, without requiring debug mode. The dedicated
cards had removed the ordinary row's diagnostic action; reintroducing a second
conversation row was rejected. Copy reads the current binding without opening
or creating a conversation, and a clipboard failure exposes the exact ID for
manual copying. The Watchdog has no entry of its own: health checks
(故障巡检) are a compact setting in the main coordinator's header next to the
Worker model and standing instructions, where the owner turns them on, sets
the periodic review and opens the Watchdog chat (which offers a way back). The
server's fixed role titles are language-neutral sentinels the browser shows
through the same localized names, and a router-managed chat (router home,
Watchdog location, router Workers) shows no project location, since its
workspace is Tofu's, not a project the owner attaches or edits. Copy uses
these names everywhere the owner reads it: settings, tool descriptions and
hints.

**Alternatives rejected:** keeping pinned entries and only renaming them (two
extra rows on every screen and still no visible relation to project
coordinators); showing the main coordinator card above the project card in
project folders (two stacked cards for one concept); a sidebar icon for the
main coordinator (hides the owner's primary entry); renaming the server titles
(language-specific durable data, and existing conversations keep the old
titles anyway); provisioning the Watchdog with the router so it needs no
setting (event-driven model turns the owner did not choose).

## Interfaces

- `lib/router/home.py` — `router_home_path(user_id) -> str` creates (idempotently)
  and returns the owner's router home Git repository; `router_home_location`
  and `is_router_home` compute the identity without effects.
- `lib/router/binding.py` — `global_router(user_id, *, create, client=None) ->
  dict` returns the binding view (`conversationId`, `projectPath`, `revision`,
  `availability`, `capabilities`) and creates the router conversation on demand.
- `lib/router/policy.py` — `ROUTER_TOOLS`, `ROUTER_ONLY_TOOLS`, `CHANNEL_TOOLS`,
  `router_scope(task)`, `is_router_scope(scope)`, `ROUTER_ROLE_ADDENDUM`;
  enforcement stays in `lib/tasks_pkg/coordinator_policy.py`
  (`coordinator_tool_ceiling`).
- `lib/router/tools.py` — `execute_router_tool` behind
  `execute_coordinator_tool`: `route_to_project` (`forwarding.py`, also
  `verify_forwarded_turn`), `get_router_projects` (`projects.py`; pages follow
  the project dispatch history page) and `router_instruction_create|list|cancel`
  (`instructions.py`; `RouterInstructions.pause|resume` are owner HTTP only).
  Schemas: `contracts/router_v1.schema.json` through `lib/router/contract.py`.
  `forwarding.owner_steer_carriers` labels steered owner messages in the router's
  model view; `lib/turn_initiation.py` owns `steer_author` and
  `steered_input_turn`.
- `lib/router/relay.py` — `relay_project_report(owner, report, *, client, dispatch)`,
  `relay_project_question(owner, question, *, client, dispatch)`; the `ask_human`
  handler (`lib/tasks_pkg/handlers/misc/_human.py`) relays questions and judges
  steered authors with `require_human_turn`.
- `lib/project_dispatch/delegation.py` — `resolve_worker_model`,
  `admit_worker_model`, `owner_default_model`.
- Channel tools (`channel_*`, [CHANNELS.md](CHANNELS.md)) are in `ROUTER_TOOLS` by
  name; channel delivery resolves the Global Router through `global_router`.
- `await_device` is in `ROUTER_TOOLS` through the coordinator ceiling: a router
  whose next dispatch needs an offline device of the owner registers a durable
  device-online wake of its own conversation and ends its turn; the wake is a
  nonhuman notice and grants no authority
  ([remote execution](modules/remote_execution.md#waiting-for-an-offline-device)).
  Workers cannot wait (`workerDeniedTools`); they report the dependency.
- `lib/router/watchdog/` — `binding.router_watchdog(user_id, *, create, client=None)`
  and `watchdog_location`; `policy.WATCHDOG_TOOLS`/`WATCHDOG_ROLE`;
  `signals.FaultSignals` and `report_turn_failure|report_recovery_failure|
  report_outbox_fault|report_channel_status|report_compute_stall|
  resolve_compute_stall`; `snapshot.health_snapshot`;
  `review.configure_review|review_state|schedule_recheck|run_scheduled_review`
  (the scheduler's `router_watchdog_review` handler); `tools.execute_watchdog_tool`
  behind `execute_coordinator_tool`. Schemas and records: the `watchdog*`
  definitions of `contracts/router_v1.schema.json`.
- `ScheduledTaskManager.ensure_owner_task(principal=..., ...)` installs one
  owner-authorized keyed task with the identity built-ins use; `create_task`
  accepts a caller-derived `task_id` so a retried creation never schedules twice.
- HTTP (`routes/api_v1/router.py`): `GET /api/v4/router` (read),
  `POST /api/v4/router` (ensure, Idempotency-Key), `GET|POST
  /api/v4/router/worker-model`, `GET /api/v4/router/instructions`, `POST
  /api/v4/router/instructions/{pause,resume,cancel}`, `GET
  /api/v4/router/watchdog` (read, never creates), `POST /api/v4/router/watchdog`
  (ensure, Idempotency-Key), `POST /api/v4/router/watchdog/review`; DTOs in
  `frontend/src/api/router.generated.ts`.
- Browser (R8): `frontend/src/features/global-router.ts` renders the main
  coordinator card (总负责人 / Main coordinator) with the project coordinator
  card's classes into `#globalRouterEntry`, the leader slot just above
  `#projectCoordinatorEntry`; activating it ensures and opens the router. On
  the router conversation it shows a compact header: one sentence on what the
  main coordinator does, the Worker model control, the standing-instructions
  list (`router-instructions.ts`: read only when opened, state, scope, pause,
  resume, cancel, cursor paging) and the controls other owners contribute
  (`hosts.controls`). Project Routers show the same Worker model control in
  the task board's footer on their coordinator conversation surface
  (`project-coordination-surface.ts`). `router-watchdog.ts` contributes the
  health-check disclosure to that header (off: turn on, which ensures the
  Watchdog; on: the periodic review switch, cron cadence and a button that
  opens the Watchdog conversation) and, on the Watchdog conversation, a
  one-line header with a way back; its `location()` lets presence omit the
  Worker model control and the task board there, since the Watchdog never
  delegates and authors no board. Both owners are created before the locale
  catalog loads, so labels carry `data-i18n`/`data-i18n-title` keys that
  `ready()` and every language switch translate; parameterized text (the
  Worker model's read and write failures) re-renders on `tofu:language-change` and removes that
  listener on teardown. The retained presence section hides the card where the
  listed conversations belong to a project (that project's coordinator card
  takes the slot), gives the router home and Watchdog location no project
  Coordinator row, hides `#projectBar` for chats in those homes, and publishes
  `runtimeScope.routerConversationIds()` so the conversation list never shows
  either chat as a row or counts it in a folder count. Each owner's `conversationId()` is the bound
  conversation its header marks, which the composer steers without the
  steer/queue prompt (RENDER_CONTRACT.md "Translation attribution"), and each
  announces a new home or conversation through `changed()`, which re-renders
  the list. `shell-localization.ts` shows the server's role titles
  (`ROUTER_TITLE`, `WATCHDOG_TITLE`, `PROJECT_ROUTER_TITLE`) through the same
  localized names. The presence section bridges these owners to the API
  (`Api.project.routerWorkerModel`, `setRouterWorkerModel`,
  `routerInstructions`, `pause|resume|cancelRouterInstruction`,
  `routerWatchdog`, `ensureRouterWatchdog`, `configureRouterWatchdogReview`)
  and to the composer model picker's catalog.

## Verification

Executable specifications live beside each owner (`tests/router/`,
`tests/channels/`, and the existing compaction/storage/coordination suites).
The router suites cover the home repository, policy ceiling and role,
forwarded-origin verification over a real storage authority (forged, edited,
synthetic, foreign-owner, non-router, self and re-forwarded origins), steered
owner messages on SQLite and native (`test_steer_forwarding.py`: author capture,
verbatim forward, forged/edited/synthetic/foreign-owner/non-router/self steers,
executor and client writes, model labels; `test_steer_authorship.py`),
forwarding replay, steer/start delivery, report relay, question relay
(`test_question_relay.py`; `test_question_answer_seam.py`, SQLite and native: a
blocking question's notice reaches the router naming the 大象 command chat, the
owner's command-chat answer forwarded verbatim ends the wait, forged/edited
forwards and nonhuman steers do not), standing instructions
including pause/resume, Worker model resolution, `tests/api/test_api_v4_router.py`
and the browser owners (`frontend/src/features/global-router*.test.ts`, including
the boot-before-catalog translation spec `global-router-i18n.test.ts`,
`router-worker-model.test.ts`, `router-instructions.test.ts`, the placement
spec in `project-coordination-entry.test.ts` and the live-reply delivery spec
in `conversation-project-draft-runtime.test.ts`); the role-title sentinels
stay paired by `tests/router/test_role_title_sentinels.py` and are rendered
as specified in `tests/i18n/test_shell_localization.py`. The real-browser
journey `tests/frontend/test_main_coordinator_browser.py` covers R8 end to
end: the single leader slot in both scopes, the header, health checks on,
the Watchdog chat and back, no project location and no rows or counts for
router chats, in English and Chinese. The Watchdog suites are
`tests/router/test_watchdog_policy.py` (ceiling, role, registry, worker denial,
fault kinds, the terminal hook, review paging) and
`tests/router/test_watchdog_authority.py` (SQLite and native: provisioning, one
signal and one wake per fault and episode, the failing-Watchdog rule,
owner-scoped snapshots, resolution, messages and notices, the keyed review,
idle-skipping reviews and re-checks, owner-only review changes, and a real idle
start without a request account), plus
`frontend/src/features/router-watchdog.test.ts` and the Watchdog bridge in
`project-coordination-entry.test.ts`. The
end-to-end acceptance: a human message in the Global Router forwards to a
Project Router that delegates a Worker; the Worker's result produces a project
report that relays back to the Global Router; a 大象 command-chat message
reaches the Global Router as human input and its reply leaves through the
outbox exactly once.
