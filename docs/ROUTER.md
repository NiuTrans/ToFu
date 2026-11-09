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
is the owner's router home (a Turn reviewing other people's channel messages
gets the narrower review ceiling instead, CHANNELS.md C11); catalog projection (`filter_coordinator_tools`,
`prepare_coordinator_tool_surface`), the registry builder and
`coordinator_call_rejection` all ask it, so channel tools appear by name once
their ToolSpec exists. Every Router tool is eager, but only `ROUTER_DIRECT_TOOLS`
(its usage-derived core) is required-direct: on Claude at `your-llm-gateway.example.com`
the tool-name budget hides the rest behind `search_tools`/`execute_tools`, and
every other route sends the whole surface
([LLM_COST_OPTIMIZATION.md](LLM_COST_OPTIMIZATION.md#6-gateway-tool-name-budget),
"Coordinator-role surfaces"). Router tools are in the project dispatch
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
The router's optional `note` is nonhuman context of that same request and never
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
note and an instruction's trigger are nonhuman evidence **inside the same
forwarded input** ("Evidence rides the same request" below), never a second
input. `require_human_turn(turn, user_id=...)` admits a
forwarded origin only through `verify_forwarded_turn`: the owner's currently
bound router, an existing genuine human source Turn that was not itself
forwarded, a different target conversation, identical authored text and, for
instructions, a matching record; a call without the owner refuses.
`ProjectDispatchRepository.validate_human_source` passes its owner, so every
grant fence repeats this check.

**Evidence rides the same request.** Problem: a forward used to be delivered as
TWO inputs — the owner text, then a peer-initiated `:context` message carrying
the note and the instruction's trigger. Each input was delivered independently
(started, steered or queued). Whenever the evidence missed the steer window it
was durably queued and later activated as its OWN Coordinator request: a
synthetic, peer-initiated request that named the instruction and re-attempted
its work, so `source_for_task` refused it (`Synthetic authorization origin
denied`) and the Project Router stalled asking the owner for "继续" (observed
2026-11-09 on the parallel project's Project Router: four queued evidence
requests, every `delegate_project_work` and even the read-only
`get_project_workers` refused, while the original live reply that had actually
received the owner text kept working).

Decision: one forward is ONE Coordinator request. Its input `content` is the
owner's verbatim text followed by the labelled nonhuman evidence block (the
note, the instruction id and its triggering event); its `originalContent` is
the owner's verbatim text alone, which is exactly what `authored_text` returns
and `verify_forwarded_turn` compares. The owner words the authority is judged
by are unchanged, no human message is synthesized and no gate is bypassed; the
evidence is labelled nonhuman in the same model view, and it can no longer
become a standalone request on any delivery path (started, steered or queued).

Alternatives rejected: delivering the evidence through the R9 owner delegation
(the delegation is a UI grant for lifecycle/session control, not a carrier for
forwarded context, and it must not mint Worker authority); letting a verified
`forwardedFrom.instructionId` source pass the synthetic-input gate in
`source_for_task` (re-derives authority at read time, needs a per-application
link and a revocation model, and still leaves a duplicate synthetic request
running the same work); dropping the evidence on the floor (the trigger and the
note are how the Project Router knows why it was woken); merging the two
queue rows at activation time (changes generic queue semantics for one
caller); a mailbox-only evidence input (either it never reaches the model, or
it still starts an unauthorized request).

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
uncommitted candidates. The control token never reaches public text: a
tool round's `[SILENT]` lives only in its private provider replay
(`assistantModelContent`), stored answers, segments and channel replies strip
it, and a refused declaration whose only text was the token is an empty
response that Python recovers as an empty stop and Native settles as
`abnormal_stop`, never a raw provider instruction shown to people. The native
Turn projection takes its system-note kinds from the contract (generated
`generated_system_note_vocabulary.rs`). Automatic review failures/interruption
send an out-of-band command-chat warning naming the failure cause; successful
silence does not suppress that failure. A human command steered into a notice supplies the reply origin,
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

**A Router without a model choice.** Problem: a Project Router made by `create`
or `provision` stores no model until the owner sends from its composer. A turn
the server started there (a forwarded request, a timer) resolved its config from
those settings alone and ran with an empty model, which the shared dispatcher
answers with any free route (2026-11-06: the autoharness Project Router ran on
GLM-5.3). Tool assembly then issued the Worker ceiling without a Coordinator
model, and `policy_for_task` refused every delegation with an error that blamed
tool assembly and advised retrying the same key. Decision: `build_task_config`
(`lib/scheduler/_shared.py`) gives a conversation without a model choice (no
name and no `modelRef`, the rule `resolve_conv_settings` applies) the owner's
default model as `resolve_conv_config`'s `serverModel`, as a fresh composer
would, the same fallback Workers use. An issued policy without a Coordinator
model is still refused, as `CoordinatorModelMissing`: effect-free
(`execution_started: false`), with a recovery that reports the remedy instead of
retrying. Alternatives rejected: writing a model into the settings at
`create`/`provision` (a server-authored composer choice the owner never made,
and existing Routers stay broken until migrated); forwarding with the Global
Router's model (router and project models are chosen separately, and a light
router model would silently become the project's); recording the routed model
after the fact (the Coordinator would still run on an arbitrary route); relaxing
the refusal (continuations would replay an empty model).

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
`list_conversations`, `get_conversation`, `search_tools`, `execute_tools` and the
`read_tool_artifact`/`search_tool_artifact` readers that continue a partial
result it already received; no
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
latest Turn is failed.

**Problem:** a Turn signal never closes itself (the subject reports failures, not
recovery), so an open signal is not evidence that the Router is still failing. A
Router that recovered and kept working was still refused notice, and the refusal
itself told the Watchdog to resolve the signal before notifying — a deadlock.
**Decision:** before refusing, the tool reads durable Turns; when the newest
main-lane Turn is `completed` and newer than the failed one, it resolves the
signal with that Turn as recovery evidence and sends the notice. Only a
still-failing, running or interrupted Router is refused. A signal whose failed
Turn is on a branch lane, or whose Turn no longer exists, stays open for the
Watchdog to judge. Recovery is a durable fact, not the model's say-so.
**Alternatives rejected:** auto-resolving Turn signals on every completed
settlement (a storage read on every Turn for a condition only this tool needs);
merely rewording the refusal (the snapshot reports `last_activity_at` but no
latest-Turn state, so the Watchdog still cannot tell a stale signal from a live
one).

The Global Router neither lists the watchdog path as a project nor forwards into
it, and Workers are denied every `watchdog_*`
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

### R9. The owner's delegation: the Global Router acts for the owner (owner decision 2026-11-06)

**Problem:** the owner wants the main coordinator to carry out owner
operations itself, starting with restarting Tofu, without a click each time.
The owner ruling of 2026-07-28 (`lib/lifecycle_approval.py`) requires a human
to approve every live-server restart: a request the owner approves in the UI,
the owner's interactive terminal, or the handoff variable of a caller that
already passed that gate. AGENTS.md principle 4 kept every synthetic user
from inheriting human authorization, and the Router's turns are often
nonhuman (channel events, relayed reports, standing instructions). So a
Router restart was always refused (2026-11-06 10:20: a non-interactive
`serverctl.py restart` stopped at the gate and created a pending request),
while two restarts had passed the gate without any human decision on record
(11-05: an agent approved a request with `decided_by=explicit-user-instruction-via-codex-chat`;
11-06 01:21: a terminal or handoff restart left no record).

**Decision:** the owner explicitly delegates operational authority to their
Global Router, and this narrows the 2026-07-28 ruling to: *a human approves
every live restart, or the owner has granted their Global Router a revocable
restart delegation*. The lifecycle approval gate stays the single decision
point. Scope:

- **One grant per owner**, held by the owner's current Global Router
  conversation and binding revision (it never carries over to a replaced
  router), stored in the gate's own document (`lifecycle_approvals.json`,
  `delegations`). It covers `lifecycle_approval.DELEGATION_SCOPES`: `restart`
  (`restart_server`) and `conversation_control` (Continue, checkpoint resume
  and Stop of an exact Attempt through `control_conversation(owner_delegation=…)`
  when the Router's request has no owner message of its own). It is off until
  the owner turns it on; it is an authorization setting, not a tuning knob.
- **Granting needs the owner in the Tofu UI.** `POST /api/v4/router/owner-delegation
  {"granted": true}` (scope `admin`) admits only a user principal over the UI
  session cookie, or the loopback open-mode UI, with same-origin browser
  evidence (Fetch Metadata where the browser sends it, else an `Origin` naming
  this host): `routes/api_v1/auth.human_ui_provenance`. API keys, scripts and
  agents get 403, and no model tool can write the grant. The record keeps that
  provenance (user agent, peer, credential) and an `assurance`:
  `authenticated` for a UI session, `advisory` in open mode.
- **Revoking** is any authenticated owner request (`{"granted": false}`) and
  takes effect at once: every delegated action re-checks the grant in the gate
  transaction that commits it, including restart acceptance after preparation.
- **Every action** needs the grant, a reason and an owner trigger: the owner's
  own message of the current request (`source_turn_id`), an owner message
  steered into the reply (`source_steer_id`), or an active standing
  instruction with what triggered it (`instruction_id` + `event_id` or
  `source_turn_id`), resolved by the forwarding owner exactly as
  `route_to_project` resolves them, plus the live router attempt fences.
- **Audit.** The gate writes one `delegatedActions` record per action: who
  (`requester`: router conversation, binding revision, tenant, Turn, Attempt),
  why (`reason`, `trigger` with the owner's verbatim words), what (`target`),
  under which grant (`grantRevision`, `grantedBy`), when (`requestedAt`,
  `acceptedAt`) and the outcome (`status` `accepted`/`restarting`/`completed`/
  `refused`/`failed`, `code`, `detail`, `interrupted`). A refusal while a grant
  exists is recorded as well; without a grant only `audit.log` records it.
  A `restarting` record reads `completed` once a server process that booted
  after its acceptance answers (`lib.boot_identity`; every restart replaces
  the serving worker); its detail names every resident host the restart
  replaced after an unclean close, from the manager's recorded outcome
  (`server_manager.read_restart_outcome`). The manager decides every refusal
  before it stops the old server, which keeps serving and reads that
  refusal: the record settles `failed` with code
  `restart_refused_by_manager` and the manager's reason. Once the old server
  stopped, the manager always completes the replacement.
  Every transition also writes `audit.log` (`owner_delegation_*`). The
  document keeps the newest 50 records, the approval records' existing bound;
  `audit.log` keeps every event. The owner reads them under "Act for me".
- **Restart (`restart_server`).** Grant → trigger → restart protection →
  audit record → a detached thread, so the restart outlives the calling turn:
  `server_reexec.prepare_server_reexec_frontend()` (the Settings preparation)
  → protection again → gate acceptance (grant, cooldown and restart stamp in
  one transaction) → `perform_server_reexec('restart', frontend_prevalidated=True)`
  → the Supervisor's `application-restart`, whose manager restart runs the
  repair-capable frontend preflight and stops the old worker only if it
  succeeds. There is no token, environment variable or pending request on
  this path, and no grant means a typed `owner_delegation_not_granted` refusal
  with nothing pending (an agent-made request would be a prompt the owner
  did not ask for, and its 600-second token does not fit an asynchronous
  chat). Protection is the Settings signal, `list_running_tasks`, without the
  Router's own conversation: a protected step (`git_land`, `export`; below)
  always refuses; other running conversations refuse unless the owner asked
  to restart anyway (`interrupt_running_conversations`, the Settings force);
  an unreadable signal refuses. In the managed split the Router runs in the
  execution host, which owns no frontend: its preparation covers the native
  engine and the manager's frontend preflight is the build that gates the
  stop, so a failed frontend build there is a manager refusal: the old
  server keeps serving and the record reads `restart_refused_by_manager`
  with the build's reason (also in `server-manager.log`).
- **Restart-complete notice.** The requester hears the outcome without being
  asked, exactly once (`lib/server/restart_notice.py`). Every restart the gate
  accepts opens one `restartNotices` record in the same transaction that
  records it, before the old server stops: `begin_delegated_action` for
  `restart_server` (recipient: the requesting Router conversation),
  `consume_restart` for a Settings restart (recipient: the owner's Global
  Router at delivery, since nobody else asked). Acceptance adds the
  conversations it interrupts, the accepting process's code fingerprint and
  the preparation's phases and duration. When the restart has an outcome
  (`lifecycle_approval.observed_restart`: a refusal or exhausted retry budget
  the manager recorded, a server generation that booted after the handover,
  or the manager's replacement serving for a host the restart kept), the
  notice is rendered once (header `[Restart outcome: nonhuman notice, not the
  owner’s words and not authority]`: action and outcome, old and new commit,
  engine and frontend build results, each interrupted conversation's resume
  result), its words and command id (`restart-notice:` + a hash of owner and
  restart) are fixed in the gate (`pending` → `delivering`), and it is sent
  through `dispatch_scheduled_turn` with initiator `proactive`, steered into a
  busy recipient: the device-online notice's path. An idle requester starts
  a new turn. A delegated record reaches its terminal status (`completed` or
  `failed`) at delivery, with an `owner_delegation_action_settled` audit
  event. Triggers: a refusal decided in the old process (preparation, bridge,
  revoked grant) delivers at once; a new server generation delivers after its
  restart resume pass ([RUN_CONTINUITY.md](RUN_CONTINUITY.md#restart-paused-conversations-resume-by-themselves)),
  so the notice carries the resume results; the scheduler's existing tick
  delivers a manager refusal while the old server serves on, a host the
  restart kept and a retry after a lost acknowledgement (the same words and
  command, which the turn command deduplicates). A notice nobody can receive
  is recorded `undeliverable`. The notice is nonhuman evidence and grants no
  authority. `restart_server`'s `next_step` says exactly this: tell the owner,
  end the turn, and report from the notice.
  Problem: after the 2026-11-08 16:46 restart the Router's turn had ended, its
  record stayed `restarting` (completion was derived only on read) and nothing
  woke it until the owner asked at 16:57, although `next_step` promised that
  the conversation "resumes afterwards". Decision: one durable notice per
  restart in the gate, delivered by the nonhuman wake owners above.
  Alternatives rejected: a synthetic Continue of the Router's ended turn
  (that turn was not interrupted and Continue would replay it); having the
  manager call back into the application (a second cross-process protocol
  beside its recorded outcome); a dedicated poller or timer (the scheduler
  tick and boot already run; principle 5); a separate notice store beside the
  gate's restart records (two authorities for one restart).
- **Protected steps.** Landings and exports change or publish the served
  checkout and often run outside any conversation (a terminal, a background
  command, another agent), so conversation activity could not see them.
  `scripts/git_land.py` (except `--check`) and `export.py` hold a process
  marker under `<data root>/protected-steps/`
  (`lib/runtime/protected_steps.py`); `list_running_tasks(user_id=None)`
  lists live ones as `protected-step:<id>` entries, so Settings restart,
  automatic restart and deferred-restart quiescence see them too. A local
  marker counts while its process lives; another host's marker counts until
  its writer removes it, and the refusal names its path.

**The boundary: act, never speak.** The grant lets the Router act as the
owner; it never lets it speak as the owner. It cannot answer questions
addressed to the owner or forward its own text as theirs
(`route_to_project` and `require_human_turn` still require the owner's
verbatim message), approve its own drafts or prompts meant for the owner
(`channel_outbox_approve` and `channel_configure_thread` keep the genuine
human-Turn gate; lifecycle approvals need the owner's UI decision), start
Workers or record standing instructions from nonhuman input
(`source_for_task`), or make an irreversible external publication the owner
has not approved (pushing an edition to GitHub runs in Workers or behind
owner approvals; the owner's question hg_844c7fcedf23 on the Internal edition
stays open). Under the grant, conversation control allows only Continue,
checkpoint resume and Stop of the pinned Attempt, never Regenerate, and
never creates an assignment: it runs the unchanged session command
(`session_control.control_session`) with the delegation standing in only for
the request's source principal, so the resumed Attempt keeps the target's own
accepted execution identity. Specifications: `tests/router/test_owner_delegation.py`.

**Agents never approve, operators are recorded.** `lifecycle_approval.decide`
requires the `human-ui` provenance only the HTTP decide endpoint mints, so a
direct call such as the 11-05 one is refused and audited. `serverctl.py`
keeps the owner's interactive-terminal confirmation and the handoff variables
(`TOFU_LIFECYCLE_GATE_PASSED`, `TOFU_RESTART_GATE_PASSED`) but writes each pass
as a consumed record with its own provenance (`interactive-terminal` with the
tty, `environment-override` with the variable, pid, parent and user); a
broken store never blocks the owner's terminal.

**Extending the grant.** Another owner operation (settings, provider keys,
channel accounts, …) joins the same pattern rather than a new authority: add
a scope to `DELEGATION_SCOPES` (the grant stays one switch the owner turns on
or off), give the Router one tool that resolves the owner trigger with
`owner_delegation.resolve_trigger`, records the action through
`begin_delegated_action` before any effect and settles it with the outcome,
and keep that operation's existing owner (the settings store, the credential
vault) as the writer. Operations whose result speaks for the owner
(answers, approvals, publications) stay outside the grant.

**Residual risks (stated, not solved).** No Tofu tool, instruction or code
path can grant the delegation; only the HTTP grant above can. Agents run as
the server's operating-system user, though, so the server cannot prove that a
request came from the owner's hands. Open mode has no authentication: any
local process can send a loopback request with browser Fetch Metadata, so a
grant made there is recorded as `advisory` and the switch says so. Such a
forgery is deliberate, not an accident of a chat instruction, and it stays
visible in the grant record (user agent, peer, credential) and in `audit.log`:
the same limit as every human-only decision of this gate. An
agent that drives the owner's real browser acts through the owner's session
and cannot be told apart from the owner here; the existing browser controls
(page mutation needs the owner's per-domain approval and the Tofu client tab
is never an automation target, [browser automation](modules/browser_automation.md))
and that agent's durable tool rounds are the evidence, and every decision
keeps its user agent and peer in the record and in `audit.log`.

**Alternatives rejected:** a self-issued approval token or an environment
variable for the Router (a second, invisible approval path); creating a
pending approval on every refused Router restart (the owner's click is what
the grant replaces, and the Router could consume it only through a second
path); a grant written by the Router or a settings file (indistinguishable
from the forgery the gate exists to stop); a separate delegation store,
principal kind or Router-only restart path (a parallel authority beside the
gate, principle 4); letting the grant also authorize answers, approvals or
publications (the owner's voice is not an operation); per-operation switches
or rate knobs (principle 5).

### R10. Proactive coordination (owner request 2026-11-08)

**Problem.** The owner asked the main and project coordinators to design how
they act on their own: how to coordinate, when to infer his intent and hand
out work, and what to report to him and when (2026-11-08,
「我们的总负责人和项目负责人一定要思考清楚，怎么设计这个主动机制…」). The same
day showed the gaps:

- The restart he delegated woke nobody, so nothing was checked or reported
  until he asked (R9).
- "I'll report back when X" held only if a later relay happened to arrive,
  and nothing checked that one did.
- His decisions were spread over conversations and drifted.
- No lead had a view of active, stalled or silent work, or of his unanswered
  questions, and he kept nudging ("你怎么没…", "进展呢").

A wake is not free: a Global Router turn is about six model requests over a
350–800k-token context (latency study, 2026-11-08). A mechanism that wakes a
lead for every event would trade nudges for cost and noise.

**Decision.** Proactive means fewer owner prompts, not more messages.
Deterministic code detects, counts, assembles and deduplicates. A model is
woken only for an open obligation that needs its judgment, exactly once,
through one wake primitive. The owner is pushed only what the reporting rules
allow, and outside quiet hours only what cannot wait.

**Principles and authority.** Proactivity never enlarges authority.

- Inside an authorized objective, next steps, verification and repairs are
  just done: that is a Coordinator continuation
  ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#result-driven-coordinator-continuation)).
- When intent is unclear, take the reversible default and state the
  assumption (`ask_human(blocking=false)` names the default it applies).
- New scope, irreversible or externally visible actions, and quality or cost
  trade-offs are asked: batched, with a default and numbers.
- Only the owner's own messages, and his standing instructions (R4),
  authorize. Reports, relays, Watchdog notices, wakes and digests are
  evidence. A wake never widens what the woken turn may do: a nonhuman turn
  opens no request account (R3), so it reads, reports, asks and updates the
  board. Work that needs authority runs as a continuation of the request that
  authorized it, or waits for the owner.

**One wake primitive.** Every proactive wake has the same four parts:

1. A durable obligation in its owner's record.
2. A deterministic command id derived from (owner, obligation identity).
3. `dispatch_scheduled_turn(..., inject_mode='steer')` with a nonhuman
   initiator: it steers a live turn, starts an idle one, or queues behind a
   closed steer window.
4. The obligation settles on an accepted disposition; `busy` keeps it for its
   next trigger.

Time is the owner-visible scheduler: a `once:` row with a deterministic id
runs the obligation's handler, which re-reads durable state and wakes only
while the obligation is still open. No model call decides, no loop polls, and
no process-local timer is the authority. A Coordinator obligation inside an
open request is delivered as that request's continuation instead, so the
woken turn keeps the request's authority.

| Trigger | Detector (existing owner) | Obligation | Woken | Delivery |
|---|---|---|---|---|
| Restart done, failed or refused | restart gate and the new server generation, `lib/server/restart_notice.py` (R9) | `restartNotices` in `lifecycle_approvals.json` | the requesting conversation; for a Settings restart, the Global Router | `restart-notice:` notice, `proactive` |
| Restart-interrupted turn | resume pass, `lib/conversations/restart_resume.py` | the interrupted Attempt | its conversation | checkpoint Continue, `restart-resume:<attemptId>` |
| Device online | desktop bridge connection epoch | device wait (`await_device`) | the registering conversation | `device-wait:` notice |
| Worker settled, decision due | dispatch settlement and recovery | continuation grant | Coordinator | continuation |
| Project report or question | `report_to_user`, `ask_human` (R3) | report Turn, question card | Global Router | `router-relay:`, `router-question-relay:` peer notice; held in quiet hours |
| Commitment due (a promise) | task-board `due` | board task and its receipt | the lead that owns the board | continuation `commitment_due` inside an open request, else a `commitment-due:` notice |
| Scheduled deadline, e.g. a quota reset | a commitment due at that time | board task | the lead waiting for it | as above |
| Owner question unanswered | `ask_human` seam | owner question record | nobody: listed in the digest and the quiet-end release | deterministic message |
| Fault: failed Turn, recovery, outbox, channel, compute stall | existing reporters (R7) | fault signal | Watchdog, then Router or mailbox | `router-watchdog-signal:` |
| Stalled Worker | Watchdog review (owner-enabled); the lead's own commitment | fault signal, commitment | Watchdog; the lead at its due | as above |
| Channel reconnect | channel supervisor, outbox | queued outbox item | nobody: queued pushes send once on reconnect | outbox |
| Digest time, quiet-hours end | owner preferences | preference record | nobody | deterministic message |
| Cost or latency anomaly | cost and log owners | none | nobody until the owner chooses a threshold; daily figures go to the digest | digest |

**Commitments (the promise ledger).** A promise is a task-board task with a
`due` time ([PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#commitments-on-the-task-board-owner-request-2026-11-08)).
The board the owner already reads is the ledger (one progress source, owner
decision 2026-11-08), so he sees every commitment, its time and whether it is
overdue where he follows progress.

- The lead sets `due` whenever it tells the owner it will do or report
  something later.
- Keeping it is marking the task completed. Re-dating it is a visible new
  `due`. Dropping it is marking it cancelled. A board write that silently
  drops an open commitment is refused.
- The platform keeps one receipt per commitment in the same board head:
  first and current due, slips, wake and closure.
- At the due time the lead is woken once if the commitment is still open.
  It is never woken twice for the same due time: a commitment left open after
  its wake is surfaced in the digest and the cross-project view, not repeated.

**Owner-intent record.** No new store.

- His standing instructions (R4, e.g. `ri-9887c7de…`) hold his verbatim
  standing objectives, constraints and explicit don'ts as well as channel
  triggers. The Router records one only from his own message.
- My Context holds stable preferences (language, report style).
- His own Turns and answered questions remain the canonical wording.

A model's summary of a decision is never the record: it cites the
instruction ID or the owner's Turn, which is what keeps decisions from
stretching. Every inferred task traces to an entry: the Global Router applies
an instruction with `route_to_project(instruction_id=…)`, which records the
trigger, and a Project Coordinator works inside the request it was given. An
inferred task without an entry is a question to the owner with a default, not
work.

**Coordination.**

- One voice per conversation, one pusher. A lead answers the owner in its own
  conversation. Unprompted pushes to his phone have exactly one sender, the
  Global Router, through its outbox (R3, [CHANNELS.md](CHANNELS.md) C5).
  Project Coordinators never push; their reports and questions relay to the
  Router, which pushes only what is new and unread.
- Deduplication is by identity, not text: one push per report Turn, question
  id, commitment (lead, title, due) or fault signal. The digest and the
  quiet-end release skip identities already pushed, except open decisions,
  which they repeat with their age until answered.
- The cross-project view is `get_router_projects`, extended with each
  project's board summary, open and overdue commitments, open owner questions
  with age and default, unread reports, active Workers and open faults. It is
  a deterministic read; the digest is assembled from the same read.
- Restarts: protection already lists running conversations (R9). Afterwards
  the restart notice gives the requester each interrupted conversation's
  resume result to compare with the view.
- Conflicts: the newer owner message wins; otherwise the reversible default,
  with the assumption stated.

**Reporting rules.**

- Push now, via 大象 so his phone notifies: decisions only he can make,
  blockers needing his action, verified deliveries, serious anomalies,
  corrections.
- Digest, at the owner's digest time and never when empty, most valuable
  first: open decisions (blocking first, then oldest), overdue commitments,
  open faults, verified deliveries, projects that were active but sent no
  update since the last digest, then daily figures.
- Silent: routine status changes, duplicate relays, acknowledgements
  (`[SILENT]`, R3).
- Quiet hours: urgent items only. Report and question relays that arrive in
  quiet hours do not wake the Router; they are held and released at quiet end
  as one deterministic message (decisions batched into one card) and one
  Router notice. Watchdog notices still wake the Router, which pushes at night
  only what cannot wait.
- Every message: conclusion first, numbers, verified and unverified marked,
  and the exact action needed from him. Related decisions go in one card.

**Owner preferences** are settings he chooses (principle 5): digest times,
quiet hours and push channel. Until he chooses: one digest at 21:00, quiet
hours 00:00–08:00, 大象 falling back to in-app. There is no other knob: no
reminder interval (a reminder is the next digest), no wake budget and no
debounce window. Batching comes from steering into a live turn, from quiet
hours and from the digest.

**Failure modes.**

| Failure | Prevention |
|---|---|
| Wake storm | Wakes only for open obligations. One wake per obligation identity (deterministic command id). A live lead is steered, not restarted. A lead's commitments due together share one wake, and each due time wakes once. Quiet hours hold relay wakes. |
| Duplicate or conflicting reports | One pusher. Identity-keyed deduplication. Reports the owner already read are not pushed. The digest repeats only open decisions. |
| Stale or over-stretched intent | Intent is the owner's verbatim words with IDs. The newer owner message wins. Pausing or cancelling an instruction stops future applications. |
| Authority creep | Wakes are nonhuman: no request account and no delegation. Authority-bearing follow-up runs only as a continuation of the authorizing request. Every inferred task cites an intent entry. |
| Cost blow-up from wakes | Detection and assembly are deterministic. The digest and the quiet-end release cost no model request. Wakes need an open obligation. Wake requests and tokens are measured per day. |
| Promise spam | A commitment is a board task, so the 24-task board bounds them. One wake per due time. A missed commitment surfaces in the digest instead of waking again. Dropping one needs an explicit cancel. |
| Quiet-hours violation | Deterministic messages run at owner-chosen times outside quiet hours. Relays are held. Notices that arrive inside the window name it. |

**Measuring the mechanism.** Deterministic, from durable records and logs:

- owner nudges per day (owner messages matching the nudge phrases);
- event-to-owner latency: report, question or commitment due to push or
  owner read;
- commitments kept on time, kept late, slipped, cancelled or missed;
- pushes never read, and replies that complain;
- proactive wakes per day by trigger, with their model requests and tokens.

**Current state (2026-11-08).** Implemented: commitments on the board
(`progressTask.due`, `lib/conversations/coordinator_commitments.py`), due wakes
through the scheduler (`coordinator_commitment_due`), continuation reason
`commitment_due` (`lib/project_dispatch/continuation.py`), the owner question
index (`lib/router/proactive/questions.py`), quiet-hours hold and release
(`lib/router/proactive/held.py`), the deterministic digest
(`lib/router/proactive/digest.py`), and the cross-project view extension
(`lib/router/projects.py`). Specs: `tests/project_mod/test_coordinator_commitments.py`,
`test_coordinator_commitment_continuation.py`, `tests/router/test_proactive.py`,
`test_proactive_replay.py`. Owner preferences (digest times, quiet hours, push
channel) are documented defaults; a settings UI for changing them is not built.
The Watchdog's fault signals, the restart notice (RESTART-NOTICE lane) and the
device-online wake are reused unchanged.

**Alternatives rejected.**

- A promise store and list beside the board: a second progress list, which
  the owner asked to remove on 2026-11-08.
- Waking a lead for every event in the catalog, as the Router's own proposal
  had it: most events need no judgment, and each wake is a full
  large-context request.
- A model-written digest: a daily Router turn of about six large-context
  requests for text the records already hold.
- A model-paraphrased decision log: paraphrase is how decisions stretch.
- The Router as the only voice: the owner talks to Project Coordinators
  directly, and routing every answer through the Router adds minutes and
  cost.
- Quiet hours only in the outbox: the Router would still be woken and billed
  at night for every relay.
- Reminder intervals, wake budgets and debounce windows: knobs (principle 5).

## Interfaces

Session execution control uses the common Coordinator tools
`get_conversation_status` and `control_conversation`. The Global Router can
operate any conversation of its owner; project Coordinators remain scoped to
their project. Continue and Stop reuse the browser command service and preserve
target execution authority. See
[session execution control](PROJECT_COORDINATION.md#session-execution-control).

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
  the project dispatch history page), `router_instruction_create|list|cancel`
  (`instructions.py`; `RouterInstructions.pause|resume` are owner HTTP only)
  and `restart_server` (`owner_delegation.py`, R9).
- `lib/router/owner_delegation.py` (R9) — `restart_server`,
  `delegated_conversation_control` (used by `session_control.control_session`),
  `resolve_trigger`, `router_requester`, `owner_delegation_state`; the grant,
  audit records and restart acceptance are `lib/lifecycle_approval.py`
  (`set_delegation`, `check_delegation`, `begin_delegated_action`,
  `record_delegated_refusal`, `settle_delegated_action`,
  `accept_delegated_restart`, `delegated_actions`, `observed_restart`, the
  `restartNotices` store `restart_notices`/`note_restart_notice`/
  `claim_restart_notice`/`finish_restart_notice`); protected steps are
  `lib/runtime/protected_steps.py`.
- `lib/server/restart_notice.py` (R9) — `deliver_restart_notice(restart_id)`,
  `deliver_restart_notices()`, `notice_command_id`, `render_notice`; specs
  `tests/router/test_restart_notice.py`.
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
  (ensure, Idempotency-Key), `POST /api/v4/router/watchdog/review`, `GET|POST
  /api/v4/router/owner-delegation` (R9: read the grant and its audit records;
  grant only from the owner's UI session, revoke from any owner request);
  DTOs in `frontend/src/api/router.generated.ts`.
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
  (`project-coordination-surface.ts`). `router-owner-delegation.ts` contributes
  "Act for me" (代我操作, R9) to that header: the grant switch, its state, an
  advisory note for an open-mode grant and the last audited use (read when
  created and when opened, no polling). `router-watchdog.ts` contributes the
  health-check disclosure to that header (off: turn on, which ensures the
  Watchdog; on: the periodic review switch, cron cadence and a button that
  opens the Watchdog conversation) and, on the Watchdog conversation, a
  one-line header with a way back; its `location()` lets presence omit the
  Worker model control and the task board there, since the Watchdog never
  delegates and authors no board. Both owners are created before the locale
  catalog loads, so labels carry `data-i18n`/`data-i18n-title` keys that
  `ready()` and every language switch translate; parameterized text (the
  Worker model's read and write failures) re-renders on `tofu:language-change` and removes that
  listener on teardown. The retained presence section hides the card where a
  folder is selected or the listed conversations belong to a project (that
  project's coordinator card takes the slot; a folder that names no directory
  shows its coordinator row with the way to add one, see
  [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md#coordinator-started-sidebar-conversations-owner-decision-2026-11-03)),
  gives the router home and Watchdog location no project Coordinator row,
  hides `#projectBar` for chats in those homes, and publishes
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
end: the single leader slot in both scopes and in a folder that names no
directory, the header, health checks on,
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
