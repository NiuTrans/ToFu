# Goal-driven self-evolution

## Product contract

The default backend accepts one objective. Model choice, cumulative execution
limits and attempt limits are optional; domain, checker implementation and execution
plan are inferred. Auto selection pins the strongest declared eligible model from
the owner routing authority; execution, repair and review retain that identity.
Submission authorizes execution and independent assessment under the recorded
policy; new runs have no cumulative token, tool, wall-time or repair-attempt ceiling
unless requested. It does not authorize unrelated capability installation. The existing model router and tool authority remain in
charge. This is a headless surface of the durable application, not a feature of
the storage-free embedded sidecar.

Wire authority: [evolution_run_v1](../contracts/evolution_run_v1.schema.json).
Application owner: [runs.py](../lib/evolution/runs.py). The detailed goal, plan,
episode and mutation endpoints remain available for domain integrations. The
execution studio uses this same run endpoint; the primary program notebook
composes independently receipted runs (see Durable long-horizon exploration
programs below). The execution studio exposes an objective
and optional model selection, without reconstructing the execution workflow.

```http
POST /api/v4/evolution/runs
Authorization: Bearer <token with tasks scope>
Idempotency-Key: <stable client request id>
Content-Type: application/json

{"objective":"Explain binary search with a worked example and its complexity."}
```

The response is `201 {"run": ...}` with a durable run ID, the current task ID,
recorded execution policy, stage and evidence. Reusing an idempotency key
returns that owner's existing run, including after completion. A different
objective under that key is a conflict. Keys are hashed and owner-prefixed;
without a key, each submission deliberately creates a new run.


Optional cumulative `budget` dimensions are `max_tokens`, `max_wall_clock_seconds` and `max_tool_calls`;
omitted/`null`/`0` means unlimited; positive integers limit consumption. Stored unlimited dimensions normalize to `0`.
Optional `max_attempts` is inclusive: omitted/`null`/`0` is unlimited; `1` disables automatic repair.
These fields and `review_reserve` are pinned by idempotency; retries cannot replace execution policy.
Problem: an executor can spend the whole allowance before independent review. Decision: optional reserves
(`tokens`, `tool_calls`, `wall_clock_seconds`) withhold amounts from each executing attempt inside the original budget.
Positive reserves require strictly larger finite corresponding limits. Executors receive remaining minus reserve,
including an earlier deadline; declaration and final delivery use that slice. Review receives actual remaining.
Alternative: merely report the final balance; it cannot protect review capacity. Fixed default ratios lack measured costs.
Provider overrun or inadequate allocation can still prevent review; cumulative/unknown-usage fences and original checks remain.
Programs persist the reserve per experiment and refuse a new launch when remaining cannot fund it.
`budget_source` is `default` or `explicit` for new runs. Legacy records retain stored limits and two-attempt policy;
reads never infer provenance, migrate or resume failed work. An owner may explicitly submit a new run after inspecting partial work.
The research adapter binds token/deadline limits to its durable model account before dispatch and folds physical attempt usage into terminal actuals; tool-call limits and review reserves still refuse. Problem: discovery ignored cumulative model authorization; decision: use the shared immutable account and original deadline, rejecting post-spawn attachment and per-stage resets. Typed model allowance/deadline stops retain `_executionLimitReason` and settle as needs_attention without replay; rejected: treating authorization exhaustion as a generic research failure. Verify research-domain and worker-budget tests. This does not authorize automatic discovery-to-experiment handoff.

| Endpoint | Meaning |
|---|---|
| `GET /api/v4/evolution/runs` | At most 50 metadata summaries; objectives clipped to 180 characters, no full deliverables |
| `GET /api/v4/evolution/models` | At most 128 owner-selectable structured model identities; no credentials |
| `GET /api/v4/evolution/runs/{id}` | Read durable run plus live `pending_questions`, including batch questions/options |
| `POST /api/v4/evolution/runs/{id}/answers` | Answer the current Ask Human question using `guidance_id` and `response` |
| `POST /api/v4/evolution/runs/{id}/cancel` | Stop the current task and prohibit new review/repair stages |
| `POST /api/v4/evolution/runs/{id}/reconcile` | Recover a missed terminal callback; never replay unavailable execution |

Use the current `task_id` with the ordinary task snapshot/event/stream API.
`human_guidance_request` events carry the actual question, options and guidance
ID. Following a stage handoff requires following the new task ID from the run;
the evolution domain does not introduce a second event journal.

## Planner numeric judgments

Problem: a zero information-gain estimate was treated as absent, and nonfinite
judgments could inflate utility or break a policy-ranked plan. Decision: preserve
explicit finite zero, clamp finite proposer values to [0,1], and use the existing
flat or admitted self-model default for missing, boolean, nonfinite or
unrepresentable judgments. Policy overrides must be finite, nonnegative and
nonboolean; invalid overrides fall back to the arithmetic score for that path.
Rejected: accepting infinity as a preferred route or aborting the whole plan
because one policy score is invalid. Scores remain proposer judgments, not
measured scientific benefit. No persistence schema, worker or budget is added.

## Failure-stage learning evidence

Problem: unfinished receipts or the last successful stage could be attributed as
the cause of a failed goal. Decision: repeated-failure advice requires at least
two settled, nonexploratory episodes with a named stage explicitly reporting
error/failed/failure/fail. Pending, unknown, aborted, successful and empty-stage
receipts establish no stage failure. All failures still contribute to the path's
observed success-rate denominator; omitting unsupported advice hides no verdict.
New anti-patterns bind failure_evidence_version=1. Legacy advice remains stored
but is not exposed to guidance, curriculum or awareness until ordinary maintenance
rebuilds it from original receipts. Reads filter this evidence policy and domain
before the 64-pattern result cap, within the existing 200-row owner record bound.
Rejected: inferring a failure location from a goal verdict or treating unavailable
stage evidence as a verified causal rule. Recorded stage failure is an observation,
not proof of the root cause. The existing record serialization adds 31 UTF-8
bytes per regenerated pattern for the version marker (a synthetic row increased
from 246 to 277 bytes); this is serialized size, not RSS. No model call, timer
or new persistent row is added.

## Maintenance episode read window

Problem: learning passes repeatedly retrieve the same owner episode window after
one terminal settlement. Decision: each maintenance cycle explicitly passes a
lazy episode reader through cost, hypothesis, strategy, gap, friction, holdout,
crystallization, curriculum and monitoring passes. The first successful read
pages the owner originals and retains the 200 most recent settled episodes,
ranked by recorded settled_at then episode ID, descending. Running episodes do
not displace closed evidence. Consumers get independent copies. Missing or
malformed pages cannot certify an empty window. Full scanning costs O(history)
RPC work and retains one page plus the bounded window, not all historical rows.
This is a collected episode view, not a transaction across pages or stores;
concurrent settlements missed behind the cursor enter a subsequent cycle.

Referenced immutable goals are resolved by identity, never inferred from the
first 200 goal IDs. One validated goal page seeds selected references; missing
references receive exact owner-scoped reads. At most 200 selected parent facts
are cached per cycle. Missing parents supply no domain, criteria or learning
facts; foreign identities refuse. Other record families remain live.

The existing strict repository page boundary rejects malformed or foreign rows.
Failed reads remain visible in the affected pass and are not cached as empty;
later fail-soft passes can retry. Close releases the additional retained window
on normal completion or exception. Owner identities are checked on each use;
there is no global cache, worker, timer, persistent record or new learning grant.
Rejected: a process-wide cache that serves stale or cross-owner evidence, and
reordering independent passes, which would lose their learning dependencies.
Ordinary lists retain their existing owner-key order; this temporal selection
belongs to maintenance. Direct learning consumers now resolve exact referenced
goals rather than silently losing parents behind a separate capped listing.
Problem: prefix-capped episode and goal reads could permanently ignore later
results. Decision: complete paged reconciliation plus bounded temporal selection
and exact parent resolution. Rejected: reversing random IDs as a proxy for time,
raising an arbitrary prefix cap, or assuming an unproved lookup index is complete.
Original episodes and goals are retained without migration or deletion. A future
index must preserve legacy completeness and exact original validation.
A controlled 205-record case reads two episode pages and fetches five parents
outside the initial goal prefix once across learning consumers. New failures
enter cost calibration, self-model evidence and holdout samples. The small paired
maintenance fixture still produces identical results with nine versus one
shared episode-list RPC. A warm 200-episode/200-parent allocation probe retained
108,760 episode JSON bytes and 69,180 parent JSON bytes, with 487,068 traced
retained bytes and 956,951 peak bytes; close released both caches. These are
fixture RPC and Python-allocation measurements, not wall-time, whole-process RSS
or scientific-quality evidence. Source-query cost grows with history; memory
holds one page, at most 200 selected episodes and their at most 200 parents.

## Original mutation evidence admission

Problem: sampling a capped episode listing could reject an explicitly referenced
historical experiment, or allow a supplied window to act as admission evidence.
Decision: validate the mutation shape and its existing 1..32 reference bound
before reading originals. Read each distinct exact owned episode and its exact
immutable goal, validate identities and lifecycle consistency, and require a
settled nonexploratory episode with a verdict and a nonexploratory owned parent.
Missing, foreign, pending, aborted or inconsistent originals cannot propose a
mutation. Repeated references retain their wire representation but reuse reads;
they do not establish additional independent observations.

No episode history scan or evidence truncation occurs. At most 32 episode reads
and 32 distinct parent reads are added per valid request, independent of history
volume; sampling windows retain ranking value but grant no mutation admission.
Confirmation, sandbox, regression, activation and rollback remain mandatory.
Rejected: raising the listing cap or trusting a cached positive projection.
No worker, timer, persistent evidence index or experiment replay is added.

## Complete mutation history and frozen policy rollback

Problem: capped mutation listings could hide active or parent versions, and
rollback changed metadata without restoring effective gate thresholds. Decision:
internal history readers reconcile validated owner-key pages before version,
activation and parent decisions. Metadata listings retain 128 results but include
active versions before recent inactive rows, with kind filtering before the cap.
Regression admission reads its exact owned original rather than a capped list.
Ambiguous active/parent versions and failed pages refuse before changing state.
Proposal numbering uses the owner/kind reservation authority below.

Budget-policy activation retains one owner/mutation transition before its effect.
The transition binds the immutable mutation fingerprint, complete before/after
vectors and the policy storage version. The original gate-policy schema validates
vectors; `evolution_policy_transition_v1` owns the journal envelope. Effect writes
use the captured storage version and exact readback for lost ACKs. Refused effects
leave their prior active metadata intact. A successful effect with interrupted
metadata can finish without reapplying the policy.

Rollback appends its current full projection and intended restoration before CAS,
restores the complete captured threshold vector, and then changes mutation states.
Descendant rollback can restore an earlier policy at a later storage generation;
its source identity and complete effect vector must still match. Metadata refusal
can retry against the acknowledged restored effect. A different policy is never
overwritten. Legacy policies without a retained preimage refuse rollback instead
of inventing former thresholds. The owner must explicitly establish a replacement
policy; no legacy history is deleted or rewritten to manufacture a preimage.

Rejected: increasing arbitrary listing caps, treating metadata rollback as effect
rollback, or reapplying partial overrides to reconstruct unknown prior values.
No model call, worker or timer is added. Each activated budget policy retains one
small durable transition row; rollback appends bounded vectors to that same row.
This is rollback authority and is not an evictable cache. Mutation reconciliation
remains linear in retained history and holds one page plus scalar/parent facts.
A controlled row-size probe retained one transition: 708 serialized JSON bytes
at preparation and 1,153 after rollback publication, including material digests.
These are bounded fixture sizes, not process RSS. Full vectors are checked against
the original mutation and a canonical material digest before use. SQLite and the
pinned native authority both preserve restoration through storage restart.

## Concurrent mutation version reservations

Problem: concurrent proposals could read the same maximum and publish duplicate
versions, making rollback parent references ambiguous. Decision: after original
evidence checks and complete historical discovery, CAS-reserve the next number
in one owner/kind head. It incorporates both the historical maximum and durable
reservation high-water mark before ordinary proposal publication. Successful CAS
acknowledgements or exact versioned readback establish ownership. Lost ACKs with
a later head cannot prove the old reservation; acquire a new number and retain
the possible gap. A publication failure never rewinds the head or reuses a number. New proposal
publication uses create-only storage CAS; ID collisions cannot replace older
records. Lost publication ACKs require exact immutable original readback.

Healthy contention retries only after validated observable head advancement;
unchanged or malformed authority propagates the refusal. Distinct kinds and users
have independent heads. These heads grant no confirmation, regression or effect
approval. All proposal writers must use this admission boundary: mixed deployments
with old unfenced writers are outside the uniqueness guarantee and need coherent
worker replacement. Existing history remains the initial maximum; old duplicate
parent histories remain ambiguous rather than being renumbered or guessed.

Each user/kind retains one small durable head, bounded by the existing mutation
kind catalog, with no per-attempt journal or new worker/timer. It is allocation
authority, not an evictable index. A controlled ten-proposal probe retained one
212-byte serialized head at version 10, not ten reservation rows; this is JSON
size, not process RSS. Rejected: process-local locks, trusting an old
listing maximum, and recycling gaps after unknown acknowledgements.

## Evidence-bound automatic rollback

Problem: harder later tasks or changed assessment contexts can lower historical
success rates without proving that an active mutation caused regression. Missing
historical baseline was also imputed as 0.5. Decision: temporal history reports
sample counts, nullable rates and an observed-decline alert, never rollback
permission. Automatic rollback requires an owned post-activation paired receipt,
created no later than the observation, bound to the exact active mutation and
current independent holdout suite. New paired receipts also bind the exact
immutable parent version and candidate controls; legacy absence grants no
automatic rollback. The monitor recomputes comparisons and scores,
requires complete distinct-case coverage and the existing monitor sample floor,
and confirms item outcomes agree with the stored pairs before ordinary rollback.

Historical, stale, future, foreign, partial or inconsistent receipts cannot
trigger rollback. A complete fresh paired loss can trigger it even when temporal
history looks healthy. Probes retain their trusted matched-execution contract;
this consistency check cannot authenticate external measurements or infer science
quality. Monitoring launches no paid probe, model or worker and persists no new
record. Legacy temporal-only decline remains visible without changing policy.
Rejected: causal interpretation of unrelated episode averages, invented missing
baseline rates, or trusting a receipt's verdict without recomputation.
A controlled three-case paired receipt grew by 98 serialized JSON bytes for the
context fingerprint (1,306 bytes total). No additional record is persisted. This
is fixture serialization size, not process RSS, experiment authenticity or a
research-quality result.

## Shared paired regression admission

Problem: activation trusted paired receipt labels while automatic rollback
recomputed original comparisons. Decision: one `verify_paired_regression` boundary
validates owned mutation, parent controls, current suite, distinct complete case
identity, individual outcomes and recomputed scores/comparison/verdict. Activation
and monitoring both use it. Activation also rejects observations dated in the
future. Observed paired losses cannot be waived; consistent
unknown measurements retain the explicit existing operator-waiver path. An invalid,
unbound or forged receipt never becomes an unmeasurable waiver opportunity.

Historical measurement compatibility retains its existing bound receipt policy;
new paired admission does not silently upgrade old missing context. Raw malformed
monitor receipts produce an invalid-evidence report rather than breaking temporal
observation. No new record, model, probe, worker or cache is introduced. Rejected:
separate implementations that drift on the same evidence, or trusting a pass label
that contradicts paired originals. This validates receipt consistency and its
trusted-probe contract, not external measurement authenticity or scientific gain.

## Concurrent gate decisions

Problem: confirmation and rejection used unconditional mutation writes, so a
stale caller could overwrite an already accepted competing decision. Decision:
read the physical record generation and CAS these single-record decisions;
resolve uncertain acknowledgments only through exact value and next-generation
readback. Rejected: last-writer-wins and retrying a stale decision against a new
generation. No new records or resident resources are introduced. This boundary
preserves concurrent decisions; multi-record activation/rollback still requires
separate coordination and is not made atomic by this change.

## Durable policy activation intent

Problem: a policy effect could commit before active metadata, allowing rejection
to strand a live effect behind rejected metadata. Decision: after ordinary
regression admission, CAS an `activation_intent` on the confirmed budget-policy
mutation before any effect. It freezes activation time and the mutation/parent
context fingerprint. Competing rejection either wins before admission (no effect)
or refuses the admitted operation. Final target metadata also uses CAS. Exact
readback resolves uncertain intent or completion acknowledgments.

Retries and the existing maintenance cycle finish the admitted operation with its
original time and immutable controls. They do not re-admit a candidate from a
later receipt, waive a new failed regression, or launch a probe. New receipts
remain available to monitoring after completion. A changed control, current
policy owner or parent refuses visibly. Mutation recovery runs before learning
passes; a recovery error is reported and other maintenance passes continue.

Rejected: unconditional last-writer-wins, rejection after effects without an undo,
and treating every retry as a new admission. This adds one temporary field and
one CAS write per successful policy activation, no additional record, worker,
model call or resident cache. A 10-digit timestamp plus 64-character fingerprint
adds 140 JSON bytes using the default serializer. Recovery scans original owner
history in bounded pages (O(history)), including intents outside the public
128-row projection; it does not delete history. There is still no per-kind
multi-record transaction: different candidates can compete, old writers do not
participate, and unresolved effect/parent conflicts remain explicit failures.
Flow-template materialization and other mutation kinds have no new recovery
rights from this policy-specific intent.

## Activation recovery on the workbench

Problem: admitted policy intents appeared as ordinary confirmed proposals, with
rejection and a composite retry that reran regression before finishing the intent.
Decision: pending policy activation is explicit in the detailed capability page
and office proposal drawer, with continuation and an explanation of later rollback.
Rejection and stale waiver controls are absent during an admitted activation;
failed enable attempts refresh original state before presenting retry. These are
presentation controls; the server still owns every decision.

The composite enable endpoint reads the exact owned mutation rather than the
128-row list projection. An existing intent continues ordinary activation without
reconfirmation, a new probe or a new waiver. Its response reports completion
without inventing a fresh regression receipt. An already-active original returns
its current state without writes, covering lost acknowledgments and maintenance
completion between page refresh and continuation. Ordinary proposals retain their
existing verification and separate explicit waiver journey. The office projection
copies the schema-owned optional intent and exposes no policy payload. Rejected:
keeping a misleading confirmed label, silently reproving an admitted operation,
or treating a list window as original-record authority. No new transport, timer,
cache or durable record is introduced; the eight-row proposal projection adds at
most eight small intents (140 serialized bytes each for the measured timestamp).

## One execution chain

1. Validate and persist the request with storage compare-and-set (CAS).
2. Admit an immutable goal with the original objective and a fixed assessment
   rubric: objective coverage, observed evidence, and usable delivery.
3. Index owner history, rank candidate paths using measured cost priors and
   admitted self-model factors, and persist the selected plan. Submission is
   the authorization to confirm this plan; there is no extra plan-confirm UI.
4. Create an ordinary task, bind its episode and run record durably, then spawn
   it through the existing bounded agent executor. If binding fails, reject the
   unstarted task through its normal terminal cleanup.
5. Execute: derive concrete acceptance checks, inspect context, do the work,
   verify, repair, and deliver. The registered `evolution_guidance` provider
   injects the selected path, owner-scoped successful strategies and repeated
   failure advice under an 800-token context budget.
6. On successful task termination, prepare originals and commit frozen review/usage context with the owned task result; materialize its verified archive, then CAS
   commit its pointer with the deliverable and actual usage before launching
   a separate review task. Its context contains the original
   objective, the fixed rubric, the deliverable and bounded observed tool
   messages. It does not inherit the execution conversation or learned advice.
7. Parse a strict review contract with exactly one result per criterion.
   Malformed output is a visible assessment failure. Unknown evidence is not
   success. A failing assessment can launch another repair with the prior
   deliverable and concrete review feedback, followed by a fresh review, unless
   the owner cancelled or an explicit limit ended. Malformed assessment/error
   tasks stop visibly rather than becoming an automatic retry-spend loop.
8. Settle the episode from measured execution-plus-review consumption, run the
   single learning heartbeat, and persist the result. New general goal runs have
   no default two-attempt ceiling. The research adapter still requires an explicit
   new submission rather than automatically replaying an expensive harvest.

`run_agent_loop` remains the sole LLM/tool loop. The evolution service neither
owns worker threads nor polls for models. The existing task terminal publication
notifies bounded fail-soft observers after task settlement. CAS fences run
handoffs and episode settlement. Stale duplicate terminal callbacks cannot
launch another review or repair.


When no reusable strategy is selected, the deterministic baseline is an honest
five-milestone scaffold: inspect context → define acceptance checks/dependencies
→ implement → verify/repair → deliver artifacts and limitations. It contains no
invented files, resources or completion evidence. The execution prompt carries
this scaffold and asks the existing agent loop to refine broad goals into
concrete deliverables and observable checks. These are proposed milestones, not
an additional scheduler or machine-verified milestone-progress journal.
## Durable long-horizon exploration programs

Required stages durably wait for admitted background commands; the existing reaper joins immutable late results before review without replay.
See `STAGE_OUTCOMES.md` for ownership, complete-read coverage, cancellation, original deadlines and restart limitations.

**Problem.** A goal run carries one deliverable/repair history; multi-day exploration needs separately receipted experiments, inherited memory, a mutable frontier, program-wide limits and a final audit of the unchanged objective. One lifetime run exhausts record/context bounds.
**Decision.** `lib/evolution/programs.py` composes ordinary runs into a durable program; `contracts/evolution_program_v1.schema.json` owns its wire shapes.
Program creation stores the objective and original acceptance requirements and returns `ready`.
Frozen continuation names the current stage; its frozen run payload owns the checks; discovery delivers evidence/protocol, later stages test alternatives, final audit retains every original requirement. Project executors declare complete file generations through `evolution_artifact`; independent reviewers receive bounded immutable `artifact_evidence` reads, and incomplete capture/coverage remains unknown (see `STAGE_OUTCOMES.md`). File citations need observed writes/reads; tool output can be cited directly. To prepare raw-file identities beyond snapshot limits, evolution_artifact accepts fingerprint_path alone: SHA-256 is computed from regular, non-symlink, project-owned bytes with cancellation, stability and final executor checks. Optional fingerprint_scope_field selects a digest from this run’s frozen measurement_scope; expected_sha256 comes from the frozen protocol and matches_precommit is computed by the server; the caller cannot replace the expected digest. A changed file reports its actual hash and mismatch without rewriting the protocol; changed protocol/authority during reading refuses delivery. Raw-file equality is not input-consumption proof. It retains no content and grants no execution/reproduction proof; declarations remain the content-retention path. Problem: message compaction could discard a live-file hash before independent review. Decision: append owner/run/task/project-bound immutable read receipts in evolution.artifact_fingerprints.v1 and CAS-publish head/count in the artifact manifest. Terminal artifact_evidence freezes and streams these receipts newest first, with sequence and observed time, without live-file rereads. Missing/corrupt/foreign chains and false ACKs refuse; identical lost ACKs and later writers preserve published receipts. Model-message-only hashes were rejected because compaction loses forensic evidence; matching hashes cannot prove input use. A 100-read probe retains about 68 KB of node JSON and a 342-byte manifest, excluding storage overhead. Receipt count has no fixed cap; snapshot content limits remain. Terminal parts and reviewer pages retain bounded transport and coverage. Lifecycle follows the owned stage manifest; unpublished nodes carry no source authority, and future reachability reclamation must preserve published roots. Reusing model-authored digest text or buffering a large file was rejected. One private 64 MiB read took 60.19 ms with 2,119,865 peak traced Python bytes after warmup (not process RSS or an end-to-end benchmark). This avoids overlapping stage mandates and invented receipt paths without weakening final acceptance. `resume` authorizes the queued experiments; `start` also
commits and submits the first launch immediately; see Preparation and experiment checkpoints for its activation fence.
The default frontier is discovery, measured baseline, alternatives, integration/stress checks, fresh reproduction/held-out validation of the frozen integrated generation, then the final original-objective audit. Independent reviewers receive the exact frozen executor context as untrusted source material, including recent failure/repair previews and omitted-history indicators. Context links grant no extra authority. New review launches with a nonempty frozen program history expose only evolution_source/evolution_review even when the current submission is short and has no large-source descriptor. A history-only reviewer’s ordinary action=list returns an empty current-source list without a fabricated coverage/completion record; current-source read still refuses absent frozen sources. This distinguishes source absence from malformed requests, avoiding repeated failing discovery calls while preserving unknown historical validity. History-only reviewers can submit pass/fail/unknown through the same terminal review port without inventing a current large-source coverage record; ordinary adjudication still owns acceptance. Prepared submission includes frozen context, scientific protocol and required numerical outcome in its identity fence, so changed history, thresholds or targets invalidate delivery. Problem: scientific fields were omitted from the prepared submission identity although they define acceptance. Decision: bind the full protocol and target through the existing live verification port; accepting an older prepared judgment after criteria changed was rejected. Scientific review preparation also compares those fields with the sealed executor outcome named by the frozen review launch, detecting prior drift or removal. The scientific archive must also be an executing outcome from the current episode and attempt. Problem: the same protocol could otherwise match a different attempt or wrong-stage record. Decision: compare the archived execution identity alongside scientific fields; protocol equality alone was rejected. Read-only review recovery retains the same executor episode/attempt, and mismatch never authorizes automatic replay. Native adjudication applies the same check to direct JSON replies; drift remains unknown/needs_attention with scientific_review_binding_changed, without automatic executor replay. Problem: a new prepared binding could otherwise bless criteria already changed before preparation. Decision: anchor it to the archived experiment as well as live submission identity. Nonscientific legacy reviews retain their original scope; no new cache/worker is introduced. Problem: exposed review submission still refused short history-only tasks. Decision: reuse the source-tool availability predicate and distinguish absent current sources from unread required sources; bypassing coverage for existing sources was rejected. The same shared remaining tool budget and deadline apply; stored legacy launch limits are not increased. Prompt exposure is not authority: every history read still verifies current owner/program binding. Problem: descriptor-only tool exposure made short submissions unable to inspect earlier feedback. Decision: one source-tool availability predicate drives configuration, limits and prompt; silently adding write tools or treating history as current coverage was rejected. evolution_source admits historical originals only from the reviewer’s current owned program, at settled sequences no later than the executor context’s frozen settled_through. action=list with program_id and sequence resolves archived run/source identities without knowing old run IDs, preserving bounded receipt pagination and the frozen sequence boundary. Supplying a run ID still requires exact archived membership; ordinary current-run list remains unchanged. Sequence/run membership, sealed source digests and current program/task binding are checked before and after I/O. The review submission port includes background_evidence_source in the same current-source inspection list and delivery identity fence. Problem: native adjudication rejected unread background evidence only after an early tool submission had already ended review. Decision: keep positive/unknown submissions in the reviewer until original background pages are received, preserving the existing negative-counterexample rule; closing review and re-running experiments was rejected. Historical delivery never becomes current submission coverage or permits exploration writes; unavailable relevant history remains unknown rather than proving independence. Reads reuse bounded pages and existing storage, with no new worker or growing header. Selected program detail and catalog/model reads launch concurrently within the existing single controller flight: the initial batch grows from two to three concurrent requests, with unchanged total request count and no new timer, cache or worker. Shared abort/route epochs still fence all responses; synchronous detail adapter failures remain contained and preserve prior confirmed evidence. Problem: waiting for catalogs before starting independent detail reads added serial latency to every refresh. Decision: batch those reads, retaining dependent run/history reads after confirmed detail; a second refresh loop was rejected. Deterministic tests hold catalogs unresolved and confirm exactly one selected detail starts, including across a route change; this establishes request scheduling, not end-to-end latency under real load. The notebook current-experiment projection accepts status, usage warnings and task identity only from the exact current run ID; foreign or late prior-run snapshots project preparation/unavailable state. Problem: task links checked identity but status and usage did not. Decision: filter once before the signature and all projections; trusting receipt arrival order was rejected. Problem: executor-only history hid earlier selection feedback from review. Decision: carry the same immutable context into new review prompts; relying on the current submission alone was rejected. A validation-generation successor’s independent review also receives server-derived validation_feedback_reuse: the confirmed direct-parent numerical failure, old/new candidate digests, equality of declared dataset/split digests and archived parent source references. It reads one parent without scanning lifetime history and introduces no worker/cache. Problem: current-only numerical evidence could conceal feedback-driven repair on the same declared holdout. Decision: expose owned direct-parent facts before review; trusting executor prose or equating new seeds/digests with fresh data was rejected. Facts do not attest actual data use or statistical validity and grant no automatic verdict. Newly frozen evidence-review policy v2 additionally requires full direct-parent original delivery to successful final provider inputs before positive acceptance. Up to four source coverage records, each with at most 64 merged ranges, remain separate from current submission coverage and are retained/restored in terminal archives. Read-only retrieval, previews, clipped inputs and unsuccessful dispatch do not count; legacy v1 remains unchanged. The source port reports historical unread offsets and canonical review_next_action combining current and historical delivery scopes. Current-only completion no longer suggests overall readiness: advice distinguishes reading current, reading history, submitting an explicitly unknown objective for unavailable references, and independent submission after delivery. The submission guard reuses that pure action derivation while native adjudication retains all authority. Problem: a scope-local submit hint could conflict with required unread history and cause repeated rejected submissions. Decision: derive one combined advisory action; conflating the two coverage stores or granting acceptance from delivery was rejected. This adds no I/O, worker or retained state. Each historical progress query obtains one fresh lineage observation and uses it to distinguish absent history from unavailable required references; no cross-request authority cache is added. Instrumented absent/empty-history queries reduced lineage reads from two to one, while a later query observes changed server facts. Duplicate reads inside one query were rejected because they add cost and can describe different observations. The terminal submission port keeps unfinished positive inspections in the reviewer, while native adjudication projects unreceived parents to unknown. Problem: merely exposing old failure sources allowed positive review without receiving them. Decision: retain exact wire delivery receipts under a frozen policy; counting a tool read or trusting prose was rejected. Missing required parent references remain incomplete, never an empty-source pass. Once current originals are received, an explicitly unknown objective can close that unavailable-history review through the ordinary terminal port; positive submission remains refused. Native settlement records validation_parent_originals_unavailable and needs_attention even with unbounded repair attempts, without re-running the executor. Problem: impossible parent reads could cause model/read loops and automatic experimental replay. Decision: retain a concrete unknown terminal blocker; fabricated coverage and repeated execution to reconstruct immutable history were rejected. Legacy reviews and available-but-unread sources retain their frozen inspection rules. Complete fixture history coverage occupies less than 4 KiB JSON, excluding storage overhead; partial deliveries remain bounded by four sources and 64 ranges each. The actual terminal persistence callback also freezes scientific protocol/outcome and program context into its review factory; the ordinary owned result commit is tested, not only direct advancement. Delivery confirmation rechecks the live reviewer after source-validation I/O and before publishing any current/historical coverage. Problem: cancellation/replacement during that I/O could leave late access receipts. Decision: stage updates privately and revalidate the same authority before publication; relying only on the initial check was rejected. Fault injection covers cancellation, reviewer replacement and Stop. Coverage proves access, not understanding or statistical validity, and introduces no worker/cache or lifetime-history array. New default validation independently reviews validation-feedback exposure and adaptive selection across repairs. Fresh commands/seeds/candidate digests do not establish an untouched test set. Generalization claims need untouched independent confirmation or an inspected justified adaptive-validity procedure; unresolved exposure/selection bias stays unknown despite numerical consistency. New confirmation uses ordinary separate frontier admission without rewriting frozen controls. Saved frontiers retain their original checks. Problem: repeated repair on the same split can turn validation into selection. Decision: make exposure validity an explicit independent acceptance requirement; assuming preregistration or ordinary standard errors resolve adaptivity was rejected (https://arxiv.org/abs/1506.02629). New default validation steps require a frozen measurement scope: dataset/split digests, matched budget, and candidate/baseline generation digests. Pre-registration, prefilled intake, synthesis acceptance and validation launch enforce the same stamped policy. The executor-facing validation_context lists required_measurement_scope_fields derived from that policy and the retained protocol schema; refusal messages identify missing fields, avoiding trial-and-error model dispatch or a duplicate checklist. Optional identity fields were rejected for new defaults because different inputs or generations could otherwise be combined; saved steps without this policy retain their exact admission. These reported identities constrain consistency, not data authenticity or independent reproduction. New default validation also has an independently judged execution-provenance requirement linking measured commands to the actual candidate/control code, configuration, data and split used. Problem: raw-file hash equality and model-reported identities could be mistaken for input-use evidence. Decision: reviewer guidance separates snapshot content from file_fingerprint read receipts and keeps missing provenance unknown; unknown/fail blocks final audit even when numerical checks pass. Merely naming versions was rejected because it does not establish what execution consumed. This is an explicit review boundary, not an automated execution tracer; saved frontiers retain their original checks. An opt-in operator diagnostic now runs explicit command arguments under strace with selected project paths (python -m scripts.trace_experiment_reads --project ROOT --file DATA --timeout-seconds SECONDS -- COMMAND). It streams read/pread64 and readable mmap events without retaining contents or raw traces, checks initial hashes and generation metadata stability, and marks incomplete transport/timeout separately. The core file_read_probe.run_observed_process adapter now reuses the shared run_command_process lifecycle, preserves command_result and streaming separately from read facts, and forwards explicit task/runtime cancellation. The operator CLI suppresses that output from its JSON. A trusted process guard installs tracer cleanup on the allocated process; failure reports allocated ambiguity and releases ownership, while namespace supervisors keep their original cleanup. Preflight hashes observe Stop between bounded reads. One monotonic caller timeout covers hashing/admission and command execution; exhausted preparation refuses before allocation and the managed process receives only the remaining budget. Explicit wall-clock deadlines remain independent, and timeout=None introduces no new cap. Resetting the timeout after hashing was rejected because large inputs could monopolize a bounded call. Tracing alters execution timing: observed duration cannot stand in for uninstrumented performance. A real directory-swap probe demonstrates that an unchanged original file and a read at the same visible path can still load substitute bytes; initial hashes/path traces therefore cannot attest exact loaded bytes or namespace identity. The internal adapter now supports pin_inputs=True with whole @tofu-input:relative-path argv tokens: only referenced selected files are inherited as borrowed read-only descriptors, offsets reset after hashing, and lease descriptors remain preserved. A real directory-swap execution then reads original bytes instead of substitute paths. Reports separate initial descriptor metadata stability from final-path identity, retain logical/effective command hashes, and never infer consumption or immutable contents from delivery; in-place mutation remains possible and is reported. Invalid tokens/descriptor grants refuse before allocation. Optional expected_input_sha256 snapshots the caller-provided expected map, requires canonical SHA-256 for every selected path, and refuses digest mismatch before allocation. Open-file metadata is rechecked immediately before launch to catch changes after hashing; callers can use CLI --expected-sha256 PATH=HASH. Caller expectations alone are not application preregistration. input_binding.prepare_input_binding now derives an immutable read-only plan from the current owned executor, project and normalized frozen protocol, accepting only scope-field paths. It rejects absent scope, foreign/stale tasks and conflicting digests for one path. InputBinding.check_current revalidates owner/task/project/full-protocol identity at the shared observer admission_check before allocation; path/stat checks follow it. Preparation writes nothing and grants no command approval or replay; existing argv/environment admission must still wrap execution. The observed adapter now emits evidence_result, routing file_read_observations JSON through stderr while preserving raw stdout and the complete terminal status trailer. When supplied to the existing result-settlement pipeline, the owned immutable command-original capture retains read observations after hooks and before message budgets; compaction cannot erase them. Numerical audit ignores read-only originals as measurements, while still refusing malformed numerical evidence. Marker text only routes capture and grants no authenticity/semantic-use authority. Real command comparison-count evidence remains numerically valid/refuted after compaction and terminal archival, regardless of model win claims. The real task-loop/API fixture now also executes a binary candidate against a linear control, obtaining a supported numerical improvement before independent review. An unknown independent judgment retains needs_attention and blocks final program completion despite the executor claiming victory; positive/negative originals and frozen fingerprints survive both storage backends and restart. Controlled providers and this synthetic search workload validate acceptance boundaries, not autonomous discovery, novelty, statistical independence or competitive research quality. run_command now exposes optional frozen_input_paths (frozen digest field to project-relative raw-file path), derives expectations through the owned input plan, and returns archived read evidence through ordinary tool settlement. It retains command/write/network/environment/sandbox preparation and skips shortcut execution that could bypass observation. Observed mode closes stdin; tracing changes timing and visible path/namespace limits remain explicit, so use it for provenance probes rather than uninstrumented performance claims. This observes local raw-file identities, not aggregate/configuration hashes or remote-host file consumption; mismatch refuses rather than rewriting the protocol. Descriptor pinning remains internal structured-argv capability, not hidden rewriting of shell strings. Mutation during execution and semantic input use remain separate unresolved evidence. The CLI exposes --pin-inputs; structured descriptor binding remains internal. Immutable contents and semantic input use still require stronger execution/checker boundaries. This grants no scientific acceptance, complete dependency or semantic input-use proof; the observer is not same-UID tamper resistant. Missing tracer refuses before executing; timeout cleans tracer-linked descendants including detached processes. Trace drain exhaustion marks evidence incomplete, signals cooperative reader Stop and waits for actual thread exit before closing its descriptor or publishing rows. Problem: a second bounded join could return while parsing still mutated the report. Decision: await quiescence after the drain bound; abandoning a live reader and closing its potentially reusable descriptor was rejected. This may extend teardown beyond the execution timeout while bounded parsing finishes, without changing scientific success. Problem: application-reported digests cannot establish OS access. Decision: establish the kernel-observation port with real commands before attaching it to command receipts; treating a mapping as byte consumption or silently running without the tracer was rejected. See test_evolution_kernel_read_probe.py; strace path/descriptor filtering is documented at https://strace.io/. Problem: a confirmed negative frozen candidate could never pass again after repair because its old digest remained queued. Decision: an explicit refresh_validation_generation replan, approved by ordinary independent review, creates a server-linked validation successor and retains the immutable failed experiment. The diagnosis instruction names this route explicitly, separating retained failure evidence from successor validation; merely exposing the flag in the tool schema was insufficient guidance. Its final follow-up must be synthesis; before successor launch, preregistration may change only candidate_digest, preserving all checks, controls and required outcome. The reviewer receives the exact stored proposal and original validation. Successor admission also rejects candidate digests already failed anywhere in the same linked validation lineage, checking descending parent sequences and unchanged controls before registration. Problem: that scan made repeated admission linear in failed generations. Decision: after independent diagnosis review, prepare owner/program/control-bound immutable failed-candidate trie nodes; the successor root publishes only in the existing program CAS. Revalidate the direct parent against its complete terminal scientific evidence, then use bounded membership lookup for older failures. Present missing/corrupt indexes refuse instead of silently scanning; index-free legacy chains retain full descending/control checks and bootstrap once at the next reviewed renewal. Numeric-equivalent budget notation preserves control identity. Full ancestor scans and growing header arrays were rejected for repeated admission. Index membership grants no scientific pass or replay authority. Silent protocol rewrite and repeating the old candidate were rejected; unknown/incomplete scientific failures cannot authorize renewal. The three- and nine-generation contract tests follow reviewed renewals across archive pagination and verify every earlier failure/protocol stays unchanged while each validation retains its immediate parent and original acceptance. Instrumented preregistration reduced the previous 1–8 archive pages to one direct-parent page per renewal. A cold 1,200-candidate fixture reads 3–5 trie nodes per query (at most 65 by key depth); its 4,716 immutable prepared nodes retain 3,009,730 JSON bytes including older roots, excluding storage overhead. Each candidate insertion prepares at most 65 nodes, reuses content hashes on retry and publishes under CAS; orphan prepared nodes never authorize membership and future reachability reclamation must preserve published roots. Cache sizing inherits the shared launch-capacity probe, with no new worker or queue. Reviews/measurements are synthetic and do not prove research quality. A successor adds a parent sequence and constant-size root/count reference (158 UTF-8 bytes in the normalized synthetic fixture); bulky membership stays in evolution.validation_candidate_nodes.v1.
These are runnable, independently reviewed experiments; their results are not inferred from a milestone label.
Default checks require grounded incidents and observable falsifiers for competing mechanisms/emerging scenarios. Unavailable prerequisites keep speculative, unrun protocols, never measured results. Final claims distinguish technical evidence, unvalidated demand/net benefit and unsupported novelty. Ordinary independent requirement review owns these checks, not proof of correct judgment. Baseline review requires named implementations/versions, observation types and separate denominators for direct execution, archived observations and authored models. Reference comparators must face scope/identity, missing/ambiguous prerequisite and abstention controls before performance claims; candidate superiority, equivalence/null and overhead require measured candidate/control comparisons under the same information, actions and cost accounting. This prevents a mixed modeled aggregate or an unqualified oracle from serving as evidence of application accuracy or value. New defaults apply only at creation; saved frontiers, launch intents and custom checks stay exact.

**Problem.** Baseline, implementation and integration can reuse measurements, leaving central claims unreplicated despite green reviews.
**Decision.** A separate ordinary experiment and independent review precede final acceptance. Frozen checks require implementation/input/environment identities, fresh candidate/control execution, held-out separation, uncertainty, complete attempts and validation of the current generation. Negative/null findings can pass truthful validation, not prove superiority. This mandatory workflow is neither proof of authenticity nor guaranteed reviewer judgment. Research discovery remains a separate pipeline.
**Alternatives.** Prompt reminders lack a progression-blocking verdict; command plans/archives observe no fresh execution. One extra sequential execution/review pair reuses existing task/launch/recovery/archive/budget owners without resident workers or parallelism. Original budgets may block admission and cannot reset for validation.
Reproduction freezes `require_command_execution=true`. Before review, terminal `command_execution_observed` correlates a current successful `run_command` with producer-stamped zero-exit metadata, rejecting background/not-run/timeout/interrupt flags. This survives context compaction without another log copy. Without structured metadata, the original correlated executor reply must include a terminal zero-exit trailer; nonempty messages alone are insufficient. Unfinished/error rounds, foreign IDs, retrieval, provisional handles and nonzero exits prove no completion.
The immutable outcome owns this fact: recovery neither recomputes it from prose nor reruns commands. Missing observation aborts without success learning/paid review, preserving ordinary program failure/recovery authorization. Idempotency cannot weaken policy; legacy requests retain absent/false. Research discovery rejects the option because its separate pipeline cannot enforce it.

The gate proves command observation, not benchmark validity, specified-candidate reproduction or authentic data; setup probes still need substantive independent review. Without completion metadata, a real invocation compacted away may be rejected; executor assertion was rejected as fabricated proof. One outcome boolean and linear scans of retained rows/messages add no stores/workers.
**Problem/decision.** Nonempty replies can accept provisional background handles yet reject completed compacted commands. Producer exit metadata fixes both without rereading logs/restarting work. Optimistic handles were rejected: submission is not completion. Exit stamps prove process completion, not the required comparison. Background completion needs its own terminal result and cannot borrow another command's success. Sealed outcomes stay unchanged.

**Background execution.** Terminal capture queries only producer-stamped handles in current successful `run_command` rows through existing owner-scoped snapshots (`wait_s=0`). It requires matching completed identity, no worker error and terminal zero exit; running/missing/foreign/mismatched/interrupted results remain unproved. Immutable outcomes seal observation before progression: review/recovery never depends on registry lifetime or repeats commands. `_bgCommandInjects`/previews are display-only, never authority.
Terminal preparation archives available completed background outputs, including failures, in the existing tool-evidence source (`STAGE_OUTCOMES.md`). This does not prove benchmark correctness: substantive review checks actual outputs and remains unknown when missing. Existing registry recovery/retention apply; evicted handles cannot be reconstructed from claims. Deduplicated reads perform no waits, model calls, subprocesses or registry mutations.
Measurements: 1,024 unavailable-handle lookups took 3.69 ms/45,344 traced peak bytes; 1,024 command rows with unmatched replies took 0.289 ms/41,264 bytes. These measure lookup/correlation overhead, not experiment cost/accuracy. The six-stage queue is 9,640 UTF-8 bytes (2,371 above five): bounded planning data, not a lifetime-growing result store.

The header stores at most 32 pending steps and one frozen launch intent. Owner expansions insert before the original final audit, never changing the objective or deleting completed experiments.
Each expansion requires an idempotency key; its header marker remains until the immutable command receipt is acknowledged, before a successor can replace it. Ambiguous POST retries are exactly once.

Every experiment is an existing evolution run with independent execution/review and its own
episodes/learning receipts. Exploration has no default overall attempt ceiling. New programs default
to `auto_replan=true`: an omitted `max_attempts` uses three attempts per technique before diagnosis.
Explicit `max_attempts=0/null` keeps unlimited repair; false disables automatic diagnosis and keeps
the unlimited omitted default. Existing frozen intents remain exact; legacy program headers without
`auto_replan` retain manual recovery.
Fresh start/resume/retry checks the frozen review reserve against remaining finite token/tool/time caps before enrollment, through the existing run validator. Equal reserve leaves no execution slice; one extra unit permits admission, not affordable review or acceptance. The notebook projects confirmed usage/server timestamps. Existing committed intents retain their payload/deadline. Launch-only refusal permitted unusable enrollment; UI-only disabling misses direct API clients.
Launch intent, remaining budget, absolute program deadline and context commit
before submission. A stable program/sequence key binds exactly one run. Retries reuse the frozen request. Pause prevents new submissions but lets
existing work settle; cancel fences progression and forwards cancellation to
the current run. If cancellation races submission, the returned run is cancelled
after submission. This race cannot promise zero side effects before cooperative task cancellation.
Cancelled programs remain enrolled while their current run drains. Header CAS counts newly consumed stages once; a terminal receipt or proved owner loss seals the experiment.
Unsettled/lost usage stays incomplete; cancellation and settlement submit no successor and preserve the original frontier. Studio keeps its five-second refresh while cancelled work drains, then returns to the ordinary thirty-second interval.

An experiment receipt commits before header progression, so reconciliation counts usage once.
It retains results, reviews, negative outcomes and usage; run/episode/task IDs locate execution evidence.
Unknown or unavailable execution enters `needs_attention`; explicit retry authorizes a new sequence.
Start, resume without a current intent, and retry validate original cumulative limits before enrollment
or header mutation; reaper launch uses the same check. Unknown token consumption blocks fresh finite
allowances; omitted/zero limits remain unlimited. Refusal preserves the header, budget and lower bound.
Definitive reviewed failures may enter automatic diagnosis below; failed receipts stay in memory.
A final audit passes only through independent run review of the original objective, acceptance requirements and audit checks.

Run requests can now carry `acceptance_requirements`. Independent review must
return one `requirement_results` item for every stable `requirement-N` ID.
Missing, duplicate or foreign IDs are rejected. A failed or unknown requirement
projects into the objective criterion and prevents a passing aggregate rubric
from declaring success. This narrower audit is stored on the run and experiment;
the episode keeps the canonical three-criterion verdict and learning boundary.

Experiment history uses integer sequence cursors, up to 50 receipts per read.
Continuation loads only the latest six receipts, with explicit 800-character
previews and truncation/omission markers. Durable originals are never evicted.
The 400-receipt growth test verifies six experiment reads for a new intent and
that the first full receipt remains accessible. This is archive-backed bounded
continuation memory. The current executor can use `evolution_explore` to recall
older receipts by literal query with a 50-receipt scan window, eight hits and an
explicit continuation cursor; `read` retrieves any settled receipt in 6,000
character chunks, including negative results and independent review. Semantic recall can explicitly scan frozen deliverable/tool originals beyond previews, binding task/kind/digest/Unicode length and reporting missing references; scope and cursor remain explicit. No match
in one window is not absence from the full archive. Context that exceeds the declared 24,000
character launch bound reports `needs_attention`, rather than silently dropping
requirements.

The existing serving-loop reaper calls `sweep_programs`; no new thread, ticker
or agent loop is added. Its owner-scoped durable registry holds up to 4,096
unfinished programs and rotates up to 32 visits per cadence. Each visit performs
one launch-intent, submission or settlement operation. Idle does one registry
read and no write; scans never enumerate archived programs. Deferred failures
stay enrolled. The deployment adapter uses its existing configured owner; the
program service and registry carry owner identity explicitly.

**Alternatives considered.** A second executor would duplicate task journals,
cancellation and model policy. A forever-growing run record would eventually
force historical evidence eviction. Replaying a missing transient task would
repeat unknown side effects. The program therefore preserves independent runs
and explicit interruption diagnosis. It still does not automatically resume a
task lost mid-experiment; restart-safe task continuation and durable semantic
memory remain required before this fulfills the long-horizon product objective.

**Problem discovery and invention.** Discovery checks the affected audience,
observed incidents, workarounds and unmet needs; experiment work constructs
competing technical mechanisms and emerging application scenarios, then tests
them against a measured baseline. The executor may extend the frontier with
`evolution_explore action=propose`. Every such step carries a structured
`hypothesis`: problem, audience, evidence references, baseline, scenario,
mechanism, expected value, unverified assumptions and a falsifier. These are
research inputs, never established demand, learning rights or verdicts. The notebook compares two selected original hypotheses and their exact experiment reviews; paging retains only these snapshots, owner changes clear them, and original evidence reads remain explicit. No new transport, timer, model or persistent cache; absent hypotheses are never reconstructed from prose. Every executor proposal adds three mandatory independent-review checks for source
support, baseline/falsifier measurements and the distinction between observed
value and unresolved assumptions; model-authored checks cannot remove them.
Problem evidence names user, trigger, failed workaround and consequence. Selection examines frequency/severity, alternative explanations, causes versus symptoms and low-effort nontechnical workarounds; it records a refuting observation and the cheapest discriminating protocol within the original objective.
Baselines include adoption and operating effort. Enabling-change necessity compares the same user value against competent present alternatives at equal information, actions and cost; assuming a future prerequisite cannot prove availability or necessity.
Emerging scenarios retain enabling conditions and untested prerequisites. Conditional protocol delivery may satisfy a stage's checks while all evidence axes remain unknown; absent demand evidence does not establish either demand or its absence.
An executor may submit its exact final reply via the sole final `evolution_artifact` call with `delivery`; complete selected generations close under the owned manifest CAS, then the ordinary task terminal port freezes the reply and hands off independent review. A prepared intent alone cannot complete or replay a task; provider finish, successful tool receipt, current authority, cancellation and complete consumption remain required. A prototype cannot establish demand or net benefit. Unavailable-alternative claims require inspected comparisons; missing evidence stays unknown and future scenarios remain hypotheses.
Problem: review of a submission alone can lose the original claim or falsifier.
Decision: freeze the typed hypothesis on its run, include it and its digest in
every original review, and require separate demand, feasibility, novelty and
net-value judgments bound to that digest. Missing, foreign or incomplete reviews
cannot pass; unread full originals downgrade positive axes to unknown. Acceptance
still governs experiment verdicts; axis judgments confer no new learning rights.
Alternatives rejected: rewritten submission claims or technical success as value.
Bounded continuation memory retains all four reviewer states, never inferring value from completion.
Receipts retain original hypotheses and all axes; comparison verifies identity
and shows unresolved axes separately, including for legacy absent assessments.

The handler verifies owner, live executor task and current program/run binding;
reviewers, coordinators, foreign and stale tasks cannot use it. Pause permits
archive reads but fences proposals; the append CAS checks the current active
run again. A task/tool-call-derived idempotency key prevents ambiguous tool
retries from duplicating frontier steps. The original final audit remains last;
no proposal launches work immediately or changes objective, acceptance or limits.
**Alternatives considered.** Unstructured idea lists obscure consumer value
and falsification. Automatically treating novel prose as evidence rewards
unsupported invention. The structured hypothesis therefore stays separate from
independently reviewed experimental findings. On-demand semantic recall now uses
source-bound quotes and the ordinary child budget; see [EXPLORATION_MEMORY.md](EXPLORATION_MEMORY.md).

**Failure diagnosis and replanning.** A settled negative review with at least one
`fail`, no `unknown`, and no execution error permits a `replan` ahead of the unchanged failed step; `recovery_of` locates its immutable receipt.
Ordinary runs execute diagnosis and independent review; no new model loop exists. Pause records the frontier without admitting execution.
Missing tasks, malformed reviews, uncertain evidence, exhausted budgets and a full 32-step frontier require attention, preserving work.
Problem: consistent negative measurements previously halted automatic exploration. Decision: a completed measurement missing its frozen required outcome also permits reviewed diagnosis, preserving the experiment and acceptance.
Programs read the owner-bound immutable execution outcome or completed background join, require a valid numerical audit bound to the full frozen protocol digest, and verify the mismatch. Error labels, missing originals, unverified numbers and unresolved background work do not qualify.
Alternatives: treat refutation as success or blindly repeat measurements; both bypass causal diagnosis. Eligible failures add bounded original/join metadata reads, no worker or persistent record type.

During diagnosis, `evolution_explore action=replan` submits a reason and one to
eight structured hypotheses. The current executor, active program and exact
failed receipt/step are checked again at the proposal CAS. One proposal freezes
per diagnosis run; retries preserve its identity and content. Renaming the same
failed instruction/hypothesis is rejected as an unchanged technique. The queue does
not change yet. Each follow-up has the mandatory hypothesis checks. The normal
independent review must pass and a matching proposal must exist before the
settlement CAS inserts follow-ups ahead of the original failed checks. Final
audits remain last even when the failed experiment was itself the final audit.
The proposal is included in the immutable diagnosis receipt before advancement;
header/acknowledgement failure cannot duplicate experiments or usage. Failed
diagnosis preserves the queued target and requires attention rather than
recursively manufacturing further diagnoses. A later explicit retry gets a new
run and must submit its own proposal. Candidate plans carry no learning rights.

The notebook offers automatic diagnosis or manual retry at creation, displays
the recovery source and staged candidate plan, and retains submitted proposals
in history with their independent review. Submitted plans remain labelled
candidates; archive presence alone does not mean admission or verified value.
**Alternatives considered.** Infinite repairs of one technique can miss a causal
failure. Blindly retrying lost tasks can repeat unknown effects. Replacing the
failed checks with easier ones can hide incomplete work. Bounded per-technique
repair followed by an evidence-gated ordinary diagnosis preserves an unbounded
exploration horizon and the original requirements.

The API provides `/api/v4/evolution/programs`, per-program `commands`, `steps`
and paged `experiments`. Creation/expansion require `Idempotency-Key`; all routes
use the shared `tasks` scope and canonical errors. Reads never progress work.
Typed browser transport lives in `frontend/src/api/evolution-programs.ts`.

The primary `#/evolution` surface is `programs/page.ts`: a program notebook
with original checks, current execution, queued experiments and a paged receipt
ledger; bounded headings retain the full goal in a stable keyboard-readable disclosure. Existing execution rooms remain at `#/evolution/runs`; research and
learning evidence keep their existing routes. The notebook submits a saved
program before explicitly starting it. An ambiguous creation retry retains the
body/key; once the program ID is known, a lost start response retries that ID.
Ambiguous expansion keys survive visits to other programs within the bounded
draft lifetime. A selected model that disappears from the catalog remains
selected with an unavailable label; submitting it uses ordinary model admission,
without silently changing the selection.
Mobile sidebar children retain their intrinsic height within the finite
navigation height; overflow scrolls the sidebar instead of compressing its
new-exploration action or clipping the program catalogue. Default flex shrink
compressed the action to about 18 px in a 390 px touch viewport.
The form accepts an optional server-side project directory, resolved and pinned
at admission and carried as string roots in each executor's ordinary project configuration.
Reviewers retain their tool-free configuration and receive no writable project.
An unavailable/non-directory path fails before creation or execution.

Problem: an original and its continuation can share the same clamped goal prefix; a status tooltip cannot distinguish them on touch screens. Decision: each catalogue entry visibly pairs its confirmed state with its immutable creation time, formatted in the current document language and local timezone. The full goal remains its label. Metadata updates preserve the button and focus. Alternatives: unclamping every full goal crowds navigation; hover-only status requires opening each candidate on touch screens. This projects existing owner summaries and adds no query, timer, durable state or authority.

Catalog pages return at most 50 owner summaries in identifier order via `after`/`before`; the notebook keeps one 20-item page. Native pressure reduces page size at the same cursor.
These are live views: first-page refresh reveals insertions behind a cursor. Problem: disabling read controls or hiding selected detail during polling loses keyboard focus.
Decision: catalog/receipt navigation preserves selected evidence, focus and drafts until admission;
focusable `aria-disabled` controls reject unavailable/repeated actions. Explicit paging supersedes
background reads through the existing epoch and trailing-refresh fence, with visible read-busy state.
Alternatives: disabling every poll interrupts input; parallel read loops admit stale pages.
Receipts load 20 at a time; originals stay collapsed. Problem: a failed final-receipt read on older pages erased confirmed delivery. Decision: retain one confirmed final receipt only for the completed program and terminal sequence; either the selected page or one bounded dedicated read verifies matching page sequence, program, final kind and completed outcome. Transport failures keep its text/DOM/selection with a localized warning; successful absence/mismatch, owner or terminal-identity changes clear it. Recovery clears the warning. Notebook preview/receipt controls explicitly open the shared outcome reader: one 6,000-Unicode-character page, separate delivery/tool evidence, frozen task/digest/length/source-kind admission, provenance/consumption/verdict, retry and close-focus restoration. Program change, hidden state and disposal abort reads; paging/polling preserve the source island. Alternatives rejected: history-page absence is not delivery absence; trusting the first dedicated row can label unrelated/failed work as delivery; larger inline previews hide original coverage, a second source reader/poller or browser-persisted evidence creates another authority. Reading original output never grants review/learning rights or executes a model.
One visible read flight polls at five/30 seconds for active/idle work, never hidden/disposed pages;
creator/expansion inputs keep DOM identity; program, execution and receipt responses must match their requested owner before admission. Failed or mismatched run reads retain confirmed questions only for the same run, preserving answer drafts/focus with a visible read error. Question identity includes program, run and guidance; successful empty reads or owner changes clear old questions. The current experiment projection binds links to program/run/sequence, preserves inspection focus and same-experiment hypothesis expansion, and distinguishes unavailable reads from waiting for execution. Failed memory reads retain confirmed source rows and expansion/focus alongside the unavailable warning; successful reads replace them. This bounded presentation state grants no execution or learning authority.
The shell treats the original hashless address as Chat, so returning there does
not push a new entry and discard forward navigation into the notebook.
Problem: leaving/reloading the notebook lost unsent inputs and ambiguous write identity. Successful creation retains chosen failure/execution settings for the next goal; acknowledged goal fields clear only if unchanged since submission. The creator exposes cumulative token/tool/time allowances, per-technique attempts and executor/reviewer model choices; empty values preserve server defaults, explicit zero remains unlimited. Saved five-field drafts extend with empty settings without changing an ambiguous original request/key. Role references match the Chat catalog by content independent of JSON key order, preserving the original serialized draft/request identity. Settings freeze on creation and the notebook shows their original limits, roles and known remaining allowances; incomplete consumption leaves remaining finite allowance unknown, and an unsettled current experiment marks it pending. The time allowance begins at first activation for new programs (creation for legacy records) and includes pauses. Decision: `programs/drafts.ts` saves creator, expansion and same-run/question answers in browser storage scoped by authenticated owner and application path; inputs and original creation/append keys restore without submitting. A known created ID retries its original start. The page retains 20 expansion drafts in memory; the browser envelope retains at most 64 forms/512 KiB, 32 fields/form and 10,000 characters/field. The store never reads/saves session tokens or caches server progress/evidence; browser input never grants execution authority.
Successful ACKs clear only the exact submitted envelope; newer edits survive. Every write merges current storage so separate forms/tabs do not erase each other. Unknown/changed owners, unavailable storage, malformed envelopes and full capacity fail visibly without truncation or eviction; pagehide/disposal capture current inputs and remove listeners. Browser clearing can remove these drafts. Alternatives rejected: page memory cannot recover a reload; automatic submission/reconstructed keys can duplicate work; a second server transcript/draft authority is unnecessary for unsent browser-owned input.

**Alternatives considered for the surface.** Synthetic milestones obscure real multi-experiment state; replacing the execution room discards its live task/material tools. The notebook instead composes the program API and links into that room, showing measured usage/reviewed receipts, never synthetic progress or previews as execution.
Verification: `programs.test.cjs` covers input/write/route races. `test_evolution_program_browser.py` exercises the production bundle, actual HTTP/program/run/episode stores and model-task seam at 390/1440 pixels: preparation, pacing, pause/resume, six experiments and delivery after reload.
`test_evolution_programs.py` covers fault injection, ownership, bounded memory, deadlines and real run/episode integration; neighboring contracts include `tests/api/test_api_v1_evolution.py`.

### Preparation and experiment checkpoints

**Problem.** Preparation must not silently consume execution time, and a lost initial-start ACK must not approve a later checkpoint.
**Decision.** **Save plan without starting** persists the same owner-scoped, idempotent program without launching. Its prepared view exposes queued instructions, checks and hypotheses alongside the unchanged original goal/acceptance. New programs use `started_at: null`; first activation persists the clock anchor. Pauses never suspend/reset it; missing legacy anchors retain creation-time accounting.
Repeated `start` after activation returns the snapshot without mutation/dispatch, even at paused, failed or terminal states. The creator skips confirmed activated records; only an explicit continuation control sends `resume`. The backend fence closes the remaining read/start race. Legacy headers without an activation witness allow `start` only when ready with no current intent or settled experiments; ambiguous paused records require explicit `resume`. This preserves their original creation-time allowance and frozen intents without inventing an activation timestamp.
`advance_mode` is `continuous` (compatible default) or `checkpoint`. Checkpoint settlement archives review, sources, actuals and receipt, then durably pauses before a successor or automatic diagnostic; completion and attention verdicts keep precedence. `set_pacing` changes future advancement only, never launches/resumes/replays or alters frozen inputs/acceptance. Polling/reloads cannot grant execution.
**Alternatives rejected.** Browser-only pause loses authority on restart; a second runtime duplicates recovery; resetting the clock grants extra budget. Checkpoints do not guarantee success or learning: archive/recall remain untrusted evidence until independently verified by existing gates. Two scalar header fields and a bounded protocol projection reuse the existing store/reaper/read lifecycle, adding no worker, timer, cache or unbounded history.

## States and recovery

| State | Meaning |
|---|---|
| `preparing` | Intake or a committed handoff is being bound to a task |
| `executing` | A bound task is working or waiting on a real human |
| `reviewing` | The independent assessment task is active |
| `settling` | Task receipts are committed; episode/learning closeout is recoverable |
| `completed` | Every review criterion passed |
| `needs_attention` | Assessment remained insufficient, a budget ended, or execution cannot be recovered safely |
| `failed` | Intake, execution or assessment produced an explicit error |
| `cancelled` | Owner cancellation prohibits any further stages |

Learning failure does not erase a delivered result: each heartbeat step reports
its own `ok` and error. The maintenance endpoint can retry these deterministic
learning passes. A failed episode write leaves the run in `settling`, so
reconciliation can finish it without rerunning the work. Cancellation receipts
can arrive after the cancellation response; they update actual usage and abort
the episode while preserving `cancelled`.

Generic stages use durable inline tasks with confirmed births and ordinary
checkpoints/events. `stage_launch.py` freezes pre-hook prompt/config before a `preparing` CAS, with a run-witnessed config digest.
Dispatch only tightens limits; explicit pre-allocation owner model changes link immutable successors. Legacy prompt-only intents retain their protocol.
Problem: committed task binding can lose submission across process death.
Decision: `stage_inputs.py` saves immutable owner-bound post-hook messages/config
(canonical max 1 MiB minus 16 KiB; serialized max 1 MiB); `stage_dispatch.py` separates binding from submission.
`pending_recovery.py` permits one cold restoration only after proven process loss
and startup retirement of the unchanged pending birth, without abort/effect evidence.
The shared factory skips hooks, rebinds resources, preserves episode/attempt/deadline;
deterministic identity and two CAS fences contain concurrency and binding ACK loss.
A `submission_voided` receipt counts observed waiting, zero tokens/tools, no verdict.
Cancellation must persist before acknowledgment; admitted tasks never replay.
Alternatives rejected: inferring no effects from status, repeating hooks or resetting
budgets. Callback/loader consumed-task fences also preserve cancelled handoffs.

Stage requests stay outside the run header, are immutable and owner-scoped, and retain at most 200,000 characters per
request. The growth test retains 400 independent requests while every live pointer stays below 100 JSON characters;
current recovery uses bounded point reads without scanning history. Missing, foreign or source-mismatched archives
refuse dispatch, close an open episode without a success verdict and expose `needs_attention`. Transient storage
failures remain retryable observations. No background worker or timer is added; ordinary callback, program progression
and run sweep drive the same transition. Initial task admission failure leaves a committed request preparing for retry;
intake failures before a request remain terminal. Legacy preparing rows without a request retain the 60-second
interruption diagnosis. Admission ACK loss, cancellation and remaining limits fence replay; legacy absence/process loss
never authorize executor recreation. A paused program allows current review/repair but no successor.
`review_recovery.py` re-admits only after the launch-stamped process is confirmed absent (or its PID reused) in the same
host, kernel boot and PID namespace. A live/inaccessible owner, namespace or host change, or an unstamped legacy task
requires attention. Executing stages remain unreplayed: their external effects cannot yet be resolved safely. Committed
terminal outcomes can resume receipt progression with their original output, consumption, and frozen review evidence;
missing or corrupt archives never authorize executor replay. See [STAGE_OUTCOMES.md](STAGE_OUTCOMES.md). The saved
review must match the consumed execution receipt and running episode. A CAS-committed loss marker authorizes the next
ordinary `preparing` admission; concurrent losers are rejected before dispatch. New runs allow two recoveries per
episode (`max_review_recoveries`, 0 disables); programs inherit overrides. Lossless archived-review projection avoids
another model call for formatting; unresolved JSON/text-limit failures retain a [finite format recovery
allowance](RUN_CONTINUITY.md#d4c--terminal-review-format-recovery-running-contract). Cancellation and deadlines remain
fences. Finite token or tool budgets refuse another review after process loss because consumption is unknown, never
zero.

`actuals_complete=false` marks observed lower bounds on the run, episode and program receipt/header; the notebook, studio and report disclose them. The same
mark persists on stage receipts when model requests lack usage; finite-token successors refuse dispatch. Program headers additionally retain cumulative
`tool_calls_complete`: confirmed stage loss makes it false permanently; missing provider token usage alone does not invalidate observed tool counts. Finite-tool
successors refuse when this witness is false, or absent on an incomplete legacy header. Receipts remain immutable and absolute deadlines never restart.
**Decision.** Track tool completeness separately so known tools remain usable under unlimited tokens. Treating all missing token usage as unknown tools would
unnecessarily block valid exploration; treating process loss as zero tools would refresh an unverified finite allowance.
[RUN_CONTINUITY.md](RUN_CONTINUITY.md#d6--one-budget-continuity-rule-running-contract-phase-1) owns the shared request reservation and settlement boundary. Such
episodes cannot authorize efficiency deltas and are excluded from measured cost priors, self-model calibration and trajectory-cost datasets. A later repair has
its own episode completeness; its known receipts are never counted twice. Cancellation after confirmed process loss closes the episode without a verdict.
Process ownership is a bounded launch stamp; each loss retains one small audit marker under the existing record-size bound, with no new worker or timer. An
owned-process probe measured 0.086 ms median across 200 calls; a representative loss marker occupies 381 JSON bytes. The default admits at most two per episode.
**Alternatives considered.** Replaying an executor risks duplicate writes;
restarting an invisible but living reviewer spends twice; assigning zero to
lost usage poisons cost learning. Frozen read-only review recovery preserves
progress while leaving side-effect continuation and cross-host recovery open.

**Alternatives considered.** Reconstructing a review from a result alone drops
observed tool evidence; keeping prompts in the run header multiplies polling
and record growth. A separate executor would duplicate task lifecycle. Frozen
requests therefore live in the archive and authorize only committed handoffs,
while execution and settlement stay in the shared task engine.


If a live task is already terminal but its durable run still says `executing` or
`reviewing`, run detail/list reads overlay observed terminal truth: `failed` for
an error/abort/interruption, or `settling` for a successful task awaiting handoff.
`snapshot_pending_reconciliation=true` identifies this projection. Observed usage
is included once without claiming a persisted stage receipt. Reads neither write
records nor launch assessment/repair; the existing callback, sweep or explicit
reconcile owns durable settlement. If the transient task is unavailable, no
terminal result is invented. A genuinely terminal durable run is never resumed.
Research observes its committed terminal record through the existing receiver; the sweep also folds cancelled runs whose episodes remain running, without replaying execution or inventing missing usage.


**Problem.** A failed repair task may still carry the preceding attempt's review
in the durable run until its terminal callback clears that review. Letting a
presentation-only `failed` status reach coordinator reconciliation can journal
that stale review under the final task's immutable event ID; it can also admit
follow-up spending before settlement.

**Decision.** `runs.get_run` and `runs.list_runs` return durable records only.
Only presentation consumers use `get_run_snapshot` (HTTP detail) or
`list_run_snapshots` (`workspace.py` summaries/floor). `_owner_run`, coordinator
journal repair, follow-up eligibility, task authorization and reindex writes
continue to use durable getters. Neither `snapshot_pending_reconciliation` nor
its overlaid status is settlement authority.

**Alternatives considered.** Guarding every write/eligibility caller against
`snapshot_pending_reconciliation` would make each future caller responsible for
remembering the distinction. Explicit snapshot getters keep the safe default;
removing the overlay entirely would restore misleading live UI status.

### Transient-stall resilience (2026-09-17 incident)

**Problem.** A storage authority stall (`deadline_elapsed`) wedged every run
in `executing`: `coordination.inject_updates` read the run record inside the
agent dispatch path without handling the failure, killing in-flight rounds,
and the terminal listener's single `advance_run` delivery died on the same
read, so no transition ever landed. Record reads also had no retry while
writes did, and nothing re-drove a lost transition except manual reconcile.

**Decision.** Three fail-soft layers, no new state machine:

1. Record reads classify retry eligibility by code
   (`database_timeout`/`database_busy`/`database_unavailable`) with the same
   bounded delays as writes. The server marks `deadline_elapsed`
   non-retryable because that flag gates *command* replay; read replay never
   duplicates work.
2. `inject_updates` skips its round on `StorageError` (coordination context
   is informational), and the terminal listener defers to the sweep instead
   of sleeping inside the engine's terminal path.
3. `sweep_stale_runs` runs on the task reaper's cadence and re-drives
   `advance_run` for non-terminal runs past a 90-second grace window. It is
   idempotent under CAS: live tasks return unchanged, terminal tasks advance,
   missing tasks land in `needs_attention` by design.

**Alternatives rejected.** Inline sleep-retry inside the terminal listener
(blocks the engine's settlement path); auto-replay of orphaned tasks
(execution is an explicit authorization; `needs_attention` stays the honest
outcome); a per-run watchdog timer (duplicated lifecycle ownership — one
sweep on the existing reaper cadence covers all runs).

### Capability domains: the research cycle is executed, not shadowed
(2026-09-18)

**Problem.** Auto-research used to be a standalone shell mode with its own
topbar page, task channel and archive, while evolution wrapped it only on
paper: `domains.py` mapped research directions onto goal payloads and run
records onto verdicts, but nothing consumed the mapping. Two surfaces, two
histories, and research outcomes never reached the learning loop.

**Decision.** One capability domain, one surface:

1. The standalone research mode is gone. The workbench (direction composer,
   live pipeline console, record archive, production workspace) is a page
   inside the Evolution workspace at `#/evolution/research`, and starting a
   direction submits a run with `domain='research'` (idempotency-keyed).
2. Execution binds the real pipeline: `runs.py` admits the mapped goal
   (`research_goal_payload`, falsification criterion) and its executing stage
   is `produce_research` itself, spawned as an ordinary `kind='research'`
   task carrying the `task.config.evolution` binding. Reconciliation and read-only
   terminal observations read the research runtime through its owner-scoped
   application API, and Stop/failed-launch cleanup signal that same runtime's
   existing abort authority. Looking only in the chat registry falsely reports
   a running research task as missing and leaves its worker alive after Stop.
   Receipt folding stays on the generic path; no registry, loop or timer is added.
3. Settlement consumes the pipeline's own evidence: the terminal research
   run record maps to the review verdict (`verdict_from_research_record`) —
   its independent evaluation supplies model-reviewed proposals, so no generic
   judge task runs. New runs freeze `research_review_version=1`: `done` and
   `degraded` leave the falsification criterion unknown and settle as partial,
   with the run needing attention. The mapped goal is exploratory and retains
   one falsification criterion as unverified intent, not an executable check: measured
   costs remain available, but proposals cannot train winning strategies,
   anti-patterns or capability mutations. Error/interruption remains failure.
   A failed pipeline never silently re-mints an expensive harvest (the owner
   resubmits an explicit new run). Legacy runs keep their frozen mapping and
   original verdicts; reads and idempotent submissions never upgrade them.

**Acceptance boundary.** Pipeline completion previously passed a scientific falsification criterion without an experiment. Accepted ideas, high rubric scores and model assertions cannot establish scientific validity or consumer value. Proposal delivery and measured cost are retained as evidence for a subsequent source-bound experiment; scientific acceptance needs independently verifiable criteria in that experiment. Merely changing `done` to `partial` while retaining ordinary learning rights was rejected: partial episodes also mint winning strategies. A second automatic judge or repeated harvest would add cost without providing the missing empirical evidence.

**Alternatives rejected.** Shadow execution (a generic agent task that calls
the research tool — pays for a full agent loop around a deterministic
pipeline and duplicates the research console's progress contract); keeping
the two surfaces with a read-only projection of research records into
evolution (history without settlement — research outcomes would still never
become episodes, so cost priors and strategies could not learn from them);
auto-repair on research failure (a retry re-mints the same harvest; failure
is a verdict, not a trigger).

### Material evidence objections

**Problem.** A reviewer can acknowledge a source conflict or unsupported baseline
and still return passing criteria. Complete source coverage and exact hashes
prove delivery, not the truth of derived claims.

**Decision.** New generic runs freeze `evidence_review_version=3` before task
birth. Their independent review must include a bounded `evidence_audit` with
coverage completeness and up to eight unresolved material objections. A
contradicted claim fails its affected criterion; an unverified claim leaves it
unknown. A requirement-bound objection also refuses that requirement and the
objective aggregate. Incomplete audit coverage cannot pass. Objections survive
in immutable review outputs and run/program receipts and are shown in the run
inspector. Costs remain measured even when acceptance fails. Any material objection
settles acceptance as failure rather than a partially winning strategy; passing
subchecks and their evidence remain intact.

Legacy runs retain their frozen contract, and original scientific runs/verdicts
are never rewritten. An empty complete audit is a reviewer judgment, not proof
of truth, baseline competence, novelty or consumer value. Source/claim and
baseline quality still require independent experiments. Conditional future
mechanisms are valid research inputs; missing enabling capabilities and claimed
causal necessity need evidence when asserted as facts. **Running contract.** Evidence policy v3 acknowledges owned, settled program-history pages only after successful final provider-input delivery; confirmation rechecks frozen sequence/run membership, source identity and live reviewer authority after storage reads. Historical receipts stay separate from current submission coverage and are bounded to the canonical 32 requirement slots times four original links (128 sources). A JSON serialization probe with 128 distinct 160-character task IDs, 64-character hashes, 1,000,000-character sources and 64 one-character disjoint intervals near each source end measured 192,000 bytes without identity scope; adding 160-character program/run identities and sequence 1,000,000 to every row measured 242,688 bytes, below the existing 1 MiB record boundary; receipts live only on the reviewer and its immutable terminal archive, with no new worker or retained cross-request validation cache. v1/v2 keep their direct-parent-only behavior and four-source bound. Within one confirmation, repeated pages share source validation, and every selected historical target is rechecked before publication. Requirement results may carry `original_sources` (one to four exact current-executor or v3 historical references per requirement). Native review preserves those links and projects a passing requirement to unknown if identity or complete successful model-input delivery is missing; its objective aggregate also cannot pass. Structured review preparation refuses incorrect requirement source identities before terminal binding, so the same reviewer can correct references without executor replay; native final-JSON adjudication still leaves incorrect or undelivered references unknown. Negative counterexamples retain their existing partial-inspection path when any declared references are correctly bound. v3 historical links require recorded program/sequence/run provenance, fresh owned archive membership and complete successful input coverage; partial or clipped delivery remains unknown, and positive structured submissions stay in the reviewer to finish inspection. Provenance survives terminal archiving and cold reconstruction. v1/v2 retain current-only requirement links. New v3 program creation freezes `final_evidence_version=1`; its final run requires exact original links for every passing requirement, including short current originals exposed through the read-only source port. Programs without that stamp retain their frozen launch behavior. Before archiving a completed final run, stamped programs compare its objective, exact original-plus-step requirement list and mandatory-original flag with the frozen intent; a changed target or weakened policy refuses completion without advancing or launching another task. Launch and archive derive the requirement list through one helper; runs remain the verdict authority. Missing or uninspected positive manifests stay in the current structured reviewer; terminal JSON uses only the existing finite, known-cost read-only review retry allowance (`originals_uninspected`), preserving executor, episode, attempt and cumulative limits. Exhaustion needs attention rather than executor replay, including unlimited execution attempts. Explicit negative counterexamples retain their partial-inspection path. The flag is frozen in executor archives and ordinary terminal-persistence capsules; SQLite/TofuDB headless tests verify the production handoff cannot accept missing originals. A positive one-final-step program gate also runs real source and structured-review tools: the controlled transport uses the ordinary budget/admission/dispatch receipt boundary, the exact original enters reviewer tool inputs, and the native receiver archives complete coverage plus every requirement link before marking the program completed. It uses one executor and two reviewer model requests; this synthetic worked-example transport test proves the handoff, not scientific quality or independent model competence. With 32 requirements, four links each, 160-character task IDs, 64-character hashes, 1,000,000-character originals and 500-character evidence notes, a JSON serialization measurement adds 42,240 bytes (60,695 bytes for the full linked requirement array); it adds no workers or retained caches (repeated links share binding checks only within one projection) and remains subject to the existing 1 MiB record limit and immutable receipt lifecycle. Linked review output reserves descriptor space per frozen requirement using the widest current descriptor and the canonical per-requirement link count. JSON parsing, archived format normalization and structured terminal submission share that bound; unlinked reviews retain their 8,000/24,000-character limits. Native review tests admit 32 requirements linked to two fully delivered originals through plain JSON, structured submission and archived wrapper normalization; the same legal manifest was previously refused by the fixed 24,000-character ceiling. **Current gap.** `programs.validation_generation_review_input` and `source_tools._review_history_facts` enforce original historical delivery only for a v2 validation successor and its direct numerical-failure parent; legacy final audits have no mandatory original manifest; new final audits require selected original links, but selection completeness and current-generation applicability still need independent semantic checks. Keeping the original acceptance text in the final payload does not close this gap. **Target decision.** Final acceptance needs an owner-bound, generation-specific claim-to-original-evidence manifest covering every original requirement, with successful provider delivery checked separately from semantic validity; absent or obsolete evidence must remain unknown. **Alternatives considered.** Stronger prompt wording and recent receipt summaries were rejected as delivery proof. Requiring every historical byte was rejected because unrelated exploration grows indefinitely; the manifest must select relevant originals and preserve unresolved requirements rather than treat truncation as completeness.

**Settlement boundary.** Terminal receipt delivery performs local learning but
sets `learning.complete=false` and records source refetch as deferred. It never
waits for academic/network re-query of another failed run. New source retrieval
uses the existing budgeted `evolution_reindex` executor tool or the owner's
explicit maintenance request; the normal requested maintenance cycle retains
its settle/freshness passes. No background thread, new store or model loop is
created. Deferred retrieval contributes no fresh-source evidence.

**Alternative rejected.** Prompt advice alone allows positive criteria to
contradict recorded objections. A deterministic projector handles that logical
conflict without pretending to infer semantic truth or launching another model
loop. It does not change the research pipeline adapter's separate settlement.

## Human interaction

The execution task enables the existing Ask Human tool and is marked attended
because the headless client has an answer endpoint. It never enables Autopilot
or a virtual user. Questions are reserved for missing facts that cannot be
retrieved or reasonably inferred, or actual authorization boundaries. The
execution instruction asks for concise batched questions explaining the impact
and a recommended choice; implementation choices and model/budget settings are
handled by the backend. This is model guidance, not a deterministic guarantee
that every question will be useful.

Answers must match an `awaiting_human` round on this run's active task and pass
the existing owner-bound human gate. Foreign, stale and duplicate answers are
refused. An unanswered question remains pending until a real answer,
cancellation, or an explicitly requested execution deadline; elapsed time is never consent.
Native tool write approvals retain their existing task API and authority.

## Quality and learning rights

A finished execution is not a met objective. A successful review is still model
assessment, explicitly recorded as `assessed_by=judge_panel`; it is not labeled
as a machine test or a guarantee of correctness. Tool messages are treated as
untrusted evidence, and missing required execution/source evidence must be
reported as unknown. A narrow or truncated evidence window can therefore cause
an honest non-completion. Critical domains should supply stronger deterministic
criteria through the detailed goal API or a domain adapter.

The independent review currently assesses supplied receipts rather than reruns
an isolated test suite. Generated rubric prose, same-model correlated errors,
and adversarial deliverables remain limitations. No live model quality benchmark
is implied by passing synthetic workflow tests. The result exposes the actual
verdict and evidence so callers can distinguish assessment from verified proof.

Settled non-exploratory episodes may update cost priors, strategies and repeated
failure patterns. Strategy outcome rates include success, partial and failure
attempts with the same domain/stage signature; the latest 64 observations form
both the denominator and the stored lineage. Anti-pattern samples are likewise
capped to their 64 receipt IDs. These are observational rates, not causal lifts. Exploration cannot mint capability changes; aborted or
verdict-free episodes are also excluded from mutation evidence. Goal statements
and criteria are immutable after admission, and confirmed/rejected plans cannot
be rewritten. This prevents later criterion changes from reinterpreting old
receipts as successes.

The maintenance cycle runs priors → hypothesis validation → strategy distillation
→ capability gaps → holdout construction → macro proposals → curriculum
proposals → active mutation monitoring. Each step is bounded and fail-soft.
It proposes improvements; it does not silently install executable changes.
Capability activation still requires the existing sandbox, regression receipt,
confirmation and rollback gate. The default regression probe returns
`unmeasurable`: rerunning a checkout test without applying the candidate cannot
measure its effect. Historical policy-ranking replay was removed because it
rewarded past selection rather than counterfactual success. Partial probe coverage
cannot certify a pass; baseline and candidate scores use the same measured items. Model distillation admits only checker-verified trajectories.

The supported learning owners remain skill forge/teaching (R1), curriculum
proposals (R2), gated planner policies (R3), falsifiable competence hypotheses
(R4), gated threshold policies (R5), and explicit trainer/model proposals (R6).
They consume the same receipts; they are not background autonomous loops.
The default executor can delegate through the ordinary swarm tool under the
aggregate child budget described below; the coordinator and reviewer cannot.
Skill authoring remains a separate explicit teaching workflow for owner-taught
capabilities.

## Resource and storage budget

**Problem.** Host-derived 100,000/200,000-token execution envelopes and a
one-repair ceiling stopped otherwise authorized goals independently of their
actual requirements. **Decision.** Host capacity sizes resident concurrency, not
objective consumption. New runs default to `0` (unlimited) in all cumulative
budget dimensions and `max_attempts=0`; positive caller limits remain enforced
across execution, review and repairs. **Alternatives rejected.** Raising fixed
constants only postpones interruption; rewriting legacy defaults could silently
spend more under an existing authorization.

No extra resident pool or timer is created. All tasks share the launch-probed
bounded worker executor and its queue; resident concurrency is unchanged.
Within the existing task engine, `executionLimits` uses `null` for an unlimited
dimension, while numeric `0` retains deny-all semantics (the reviewer remains
unable to call tools). All supplied dimension keys must be present and valid.
Delegates share accounting and atomic tool admission even without limits; closed
children release active references. Explicit deadlines still bind the invocation
and execution session. Cancellation, owner isolation, Ask Human, tool approvals,
and normal terminal/resource cleanup are unchanged. Owner model routing remains
the source of model choice; the task manager pins both the structured reference
and its resolved logical model in loop config.

For explicitly limited runs the shared loop checks reported tokens/deadline
before another model dispatch;
the unified per-tool executor atomically admits calls, including streaming and
nested gateway calls. The normal invocation deadline and execution-session
maintenance cancellation cover running provider/tool/human waits. These are
cooperative boundaries: execution, review and coordination use the ordinary
model/provider output policy, not a separate goal-mode per-response ceiling.
Physical request admission reduces output against the remaining account after sealing input.
Problem: reserving the cumulative goal envelope as one reply falsely reduced prompt capacity to one token. Decision: retain ordinary response policy and shared cumulative admission, rather than clamp the whole goal to one model window.
In-flight usage may still overshoot reported tokens; external service work is not a financial cost cap.

Provider output/context limits are not cumulative goal budgets; no-limit goals
are not a guarantee of completion or infinite capacity.
Nested agent/skill-authoring model tasks are excluded from this default lane.

A run retains one task consumption receipt per stage and one episode per execution
attempt, plus one plan and one goal. Removing the attempt ceiling means this
durable ledger grows with actual authorized work; it is not a resident worker
increase or a promise of infinite storage. The existing 1 MiB record safety
boundary remains and can require an explicit new run for exceptionally long
histories; receipts are not silently deleted. **Terminal-capacity decision:**
before allocating any next execution/review task, the run reserves 256 KiB
inside that record boundary for its delivery, review, usage receipt and
maintenance metadata. If the headroom is unavailable it settles as
`needs_attention` with `run_record_capacity_exhausted; start a follow-up goal`;
the current episode is settled, existing receipts remain intact and duplicate
terminal delivery does not allocate another task. The reserve is a physical
storage guard, not a token, tool, wall-clock or attempt quota. Merely waiting for
`put_record` to reject an oversized terminal record was rejected: the task has
already executed by then and cannot reliably publish its terminal status.

Coordinator wake likewise checks both journal entry count and byte headroom
before allocating a task. Replies cannot consume the last event slot or the
48 KiB reserved for the terminal run-result notice. Legacy journals already
at capacity retain their events and expose `run_result_capacity_exhausted` on
the manager; the full run remains the result authority and coordinator
reconciliation can still finish. Capacity can change during execution, so settlement
checks the full workspace CAS row again. A reply that no longer fits leaves the
manager in `needs_attention`, preserves its task reference and existing journal,
and records observed consumption exactly once. The reply preview is retained
when it fits; otherwise the previous preview is retained and the complete reply
remains on the bound task. This avoids evicting durable history or replaying an
already-finished coordinator merely to recover a reply slot.

A synthetic in-memory measurement
using the three-execution regression (2026-09-24; no provider calls or model refs)
produced six task receipts / 864 serialized receipt bytes; the run record grew
from 732 to 2,695 bytes. This measures fixture metadata growth, not live RSS or
production throughput.

Inline delivery is capped at 24,000 characters, review input tool messages at 16,000 serialized characters, and review JSON at 8,000 characters.
Every persisted evolution record is capped at 1 MiB. Command checkers retain a
bounded output tail while draining stdout, enforce a finite timeout and reap
POSIX process groups. Durable user records are not silently deleted; automatic
learning/index scans remain capped at 200 records and reports disclose their
bounded scope. This is not an all-history statistical guarantee.

## Exploration loop: three-plane guardrails (2026-09-18)

**Problem.** Real optimization processes unavoidably produce four by-products:
noticing anomalies without a failing task, questioning the frame itself,
judging a deliverable from its consumer's seat, and probing edge cases the
rubric never declared. The settlement boundary compressed every episode to
verdict + actuals + lineage, so these by-products died in transcripts. Three
concrete blindnesses followed: friction that never produced a failure was
invisible to every sensor; resource gathering ran once at intake and never
again (a person stuck for days goes back for fresher sources); and the
learnability band actively walked away from the walls where premise insights
live. Guardrails governed *thinking* (no evidence, no record) instead of only
governing *behavior* (no evidence, no activation).

**Decision.** Guardrails are re-aimed by plane. The **thought plane**
(recording, clustering, proposing) is open; the **evidence plane** (bounded
probes, reindexing) keeps budget and exploratory-rights limits; the
**behavior plane** (skills, strategies, thresholds, contracts) keeps every
existing invariant — lineage, gate, holdout, human confirmation, rollback —
unchanged. Six mechanisms, all additive:

1. **Observation ledger** (`observations.py`, contract
   `evolution_observation_v1`): durable, owner-scoped records for anomalies,
   premise objections, consumer friction, edge cases, mined friction and
   reindex receipts, recorded by executors/reviewers/coordinators/owners/
   miners. Zero learning rights: they never affect verdicts, strategies,
   mutations or planning. Recording is idempotent under deterministic ids;
   a full ledger (256) refuses, never evicts. Clustering is computed at read
   time — same-signature noticings surface together without record mutation.
2. **Friction miner** (`friction.py`): deterministic detection of struggle
   without failure — a truncated tool result followed by an identical retry,
   or an identical-call loop (≥3) — mined from settled episodes' bound tasks
   and recorded as `friction` observations. A maintenance pass, same posture
   as crystallize: one unloadable task never aborts it.
3. **Dual-axis frontier** (`curriculum.py`): the probe ranking now combines
   the learnability band with a surprise axis (corroborated cluster count),
   so a wall with protest outranks a quiet hopeless region. Friction and
   objection clusters become exploratory `probe:friction`/`probe:objection`
   goals through the ordinary intake, deduplicated and capped as before.
4. **On-demand reindex** (`refetch.py`): resource gathering is a loop action,
   not intake-only. One bounded core serves three lawful triggers — the bound
   `evolution_reindex` tool (executor/coordinator, refined queries + reason),
   a settle-triggered reindex for `needs_attention` runs (queries derived
   from the objective and failed criteria), and a freshness pass for
   long-stalled terminal runs. Results land on the goal shelf as untrusted
   reference links; every reindex writes a `reindex_note` observation
   receipt; at most 3 per run. The self-model gains a falsifiable `reindexed`
   feature, so whether on-demand reindexing helps is itself measured, never
   asserted.
5. **Advisory consumer lens** (review `consumer_assessment`): the review
   contract gains an optional, verdict-immune field — is the deliverable
   meaningful to its consumer? A `no` is recorded as a `consumer_friction`
   observation (fail-soft, settlement never breaks). A persistent `no`
   cluster against passing verdicts is a measurable signal that the rubric
   measures the wrong thing — without a single verdict being overridden.
   The run retains the optional assessment for display, while settlement
   projects only outcome, assessor, criterion results and evidence references
   into the episode verdict. Allowing the assessment in the episode verdict
   was rejected because advisory sentiment must not become learning evidence
   or prevent a valid execution/review pair from reaching a terminal state.
6. **Frame-change succession** (`observations.promote_observation`): an
   objection (with its same-signature siblings) promotes into a draft
   `[frame-change]` goal through the ordinary intake. Goal immutability is
   untouched — the old goal's receipts stay interpretable; succession
   creates a new goal. Promotion executes nothing: plan/confirm/launch
   remains owner authorization. Corroborated premise-objection clusters
   (≥2) auto-promote (one per maintenance cycle); singletons stay visible
   in the digest. HTTP: `GET/POST /api/v4/evolution/observations`,
   `POST …/observations/{id}/promote|dismiss`.

**Alternatives rejected.** Learning rights for observations (a noticing is
input to the frontier and the owner, never to behavior — re-admitting it as
evidence would collapse the evidence economy); auto-executing frame changes
(the proposal is machine-generated, the authorization stays human — the gate
keeps its meaning); a consumer lens inside the verdict rubric (same-model
correlated errors would contaminate settlement; the advisory distribution
keeps the signal without the authority); mid-run plan rewrites with
reindexed material (plans stay immutable for settlement interpretability —
material arrives through the shelf and the existing guidance boundary);
auto-retry of stalled runs (execution is an explicit authorization, same
posture as the reconcile contract).

## Verification

`tests/evolution/test_evolution_runs.py` covers goal-only intake, independent assessment, optional cumulative limits, repair beyond the second execution, legacy-policy preservation, truthful terminal snapshots without read-side spending, actionable fallback milestones, receipt accounting, idempotency, owner isolation, cancellation, Ask Human ownership, immutable goals/plans, missing-task handling and write failure. `tests/agent_core/test_execution_tree_budget.py` pins nullable dimensions, explicit caps, delegated accounting, cancellation and closed-child reference cleanup. `tests/api/test_api_v1_evolution.py` exercises the authenticated HTTP surface. Neighboring domain, task terminal, context composer, tool registry and execution tests protect the integration. The exploration loop is pinned by `tests/evolution/test_evolution_observations.py` (ledger discipline), `tests/evolution/test_evolution_friction.py` (deterministic struggle signals), `tests/evolution/test_evolution_refetch.py` (three triggers, budgets, receipts) and `tests/evolution/test_evolution_exploration.py` (dual-axis frontier, promotion, advisory lens, HTTP lifecycle).

### Repeated receipt-page schema validation

Problem: the 64-attempt profile spent 61.48 cumulative seconds in 5,256 history
expansions, including 55.26 seconds in repeated page validation. Decision:
`receipt_validation.py` retains only canonical digest/compiled-validator success
proofs. Each expansion still fetches the original, computes its digest, checks
owner/run identity, count links and every prefix commitment. Changed bytes,
missing originals, read failures and a new compiled contract cannot reuse proof.
Rejected: caching original history or skipping authorization/digest validation.

The lazy process LRU admits one small proof per MiB of minimum launch memory
capacity/headroom from the existing probe; missing/zero probes fall back to one.
Bookkeeping for 4,096 proofs sharing one compiled validator retained 611,056
traced bytes (not whole-process RSS or validator compilation). No page/row is
retained. Explicit clearing and LRU eviction reclaim reconstructible
proofs; no durable row, worker or timer is added. Under the same profiled controlled
64-attempt/three-crash workload, expansion time was 8.83 seconds with proofs and
5,256 calls unchanged. The full profiled run changed 190.98 to 142.13 seconds.
These include profiler overhead and synthetic execution, not real model/GPU speed.
History expansion remains linear and storage reads are retained. The ordinary
unprofiled run also retained all 128 receipts/64 originals across the three losses.

### Private native long-horizon measurement

Problem: the controlled repair/crash benchmark's fixture task owner could differ
from the native client's authority scope, refusing its durable task birth before
any experiment. Decision: `benchmarks/evolution_long_horizon.py` scopes its private
runtime to the explicit fixture owner for the entire run/restart/cleanup lifetime,
then restores the caller's environment. Rejected: bypassing acknowledged birth
or falling back to a different storage backend. No live application is restarted.

The controlled 64-attempt measurement retained 128 receipts and verified all 64
executor originals after forced native-engine losses at attempts 8, 32 and 56.
Stored headers retained at most 64 receipts; the final stored/projected header
sizes were 16,781/29,837 JSON bytes. First/last eight-attempt median latencies were
369/1,067 ms on the measured host. This observation motivates profiling the full
history reconstruction/validation path; it does not establish causality or a
performance regression relative to another system. Source and full measurements
remain in the run artifact; fixtures made zero real model calls and do not prove
executor replay or scientific quality. Verify the private owner cleanup and report
coverage with `test_evolution_long_horizon_benchmark.py` before running this probe.

### Headless continuity and performance gate

**Problem.** Synthetic terminal tasks and browser API fixtures can pass while the real task executor, model dispatch and terminal handoff never complete. An office screenshot cannot measure unattended reliability or provider cost.

**Decision.** Four layers carry distinct evidence. `test_evolution_headless_runtime.py` runs authenticated v4 intake, real execution and review tasks against isolated storage and a deterministic provider transport, then reads the durable run and episode after settlement on both SQLite and native TofuDB. It also proves that missing empirical measurements reject a frozen scientific goal before reviewer dispatch, with no successful strategy learned. Terminal working-context cleanup uses valid delete command identities and the integration test checks reclamation; immutable evidence remains available. `test_evolution_real_measurement.py` adds an actual project command: a deterministic linear-versus-binary search comparison over three paired seeds and held-out queries yields a valid negative outcome, despite a positive model claim; both backends retain its run and original measurements across storage restart. Its program path archives that confirmed negative result, queues diagnosis without replaying the experiment, and preserves the original objective and failed step across restart; a terminal single-run test alone was insufficient evidence of iterative progression. A foreground/background audit-version mismatch retains the immutable originals and merged counts but removes the validity marker; it settles as unverified rather than throwing during a background join. Protocol mismatch remains a typed refusal. Six recovery-order tests also vary three-part grouping: mixed-version invalidity is irreversible across subsequent joins, counts and evidence sums remain identical, and inputs stay immutable. Homogeneous current-version joins retain their ordinary validity. The command fixture now runs candidate and control seed loops in opposite orders and emits observation v2 identities; its protocol explicitly requires v2 and its program step carries the same admission policy. The positive/negative gates and exact frozen format survive native task dispatch, archival and both storage restarts without relying on array position. This toy experiment verifies the production evidence path, not scientific novelty. New default baseline steps require the existing command-execution receipt gate before reviewer dispatch; prose-only baseline claims cannot satisfy it. Refused execution retains the sealed terminal delivery and its exact source reference before command, preregistration or scientific-observation gates settle; long inline previews never replace the complete original during failure archival and diagnosis. Saved queues and custom baseline protocols remain frozen. Missing command-execution observations remain ambiguous rather than definitive experimental failures: enabling auto-replanning does not authorize diagnosis or executor replay for them. Short and long baseline refusal tests retain the original baseline and final audit through repeated reaper visits; recovery still needs evidence reconciliation or an explicit retry. This boundary is a current limit on unattended completion, not proof that every long-running objective can finish automatically. Archive admission rechecks any explicit deliverable reference against its sealed owned metadata and a bounded original-page read; a pointer alone cannot bypass missing-original refusal. The current launch remains intact when this check fails. If the immutable receipt already committed before a header failure, reconciliation requires exact receipt equality and reuses that admission instead of reading its original again. Changed references still refuse; the recovery test verifies zero original-page reads and one accounting settlement. Exit-status parsing accepts bounded native decimal statuses; oversized or malformed trailers remain unproved rather than throwing during terminal capture. A successful command receipt establishes execution, not measurement correctness, comparator competence or scientific validity; the frozen independent-validation protocol remains necessary. Transport-only or SQLite-only checks were rejected as insufficient evidence of the native execution/settlement chain; these deterministic tests do not establish real model quality or benchmark superiority. The v4 API tests cover owner scope, idempotency and failure envelopes. Studio browser tests cover the projection and interaction. For an installed model, `evaluations.evolution.soak` runs the actual v4 HTTP endpoint from a separate process with no browser, retries the same submission key, follows stage task IDs, checks monotone usage and unique receipts, requires every frozen acceptance requirement to have exactly one passing result (and original links when required), and writes one JSONL row per run. This transport-level consistency check does not independently adjudicate source identity or scientific validity. Each row carries a goal hash, probe identity, harness version and the resolved model and usage for each stage, so mixed executor/reviewer routes can be priced offline against the provider tariff used for that run. The probe retries temporary connection failures, socket read timeouts and 502/503/504 outages under the same idempotency key and counts outage time, including exhausted retries. Once a run identity is known, polling or retry-confirmation outages that exhaust the deadline reach its cancellation path; an observation failure alone cannot prove producer termination. Cancellation must return the bound run identity and cancelled status, followed by durable readback. Observation gaps keep provider usage unknown even if the last observed stage was settlement. Harness v7 shares its monotonic clock with each run observer and restricts that observer to the remaining overall duration or its per-run timeout, whichever is smaller; a known active run reaches explicit cancellation when that window expires. Blocking HTTP and cancellation/readback still have their separate transport timeouts, so this is not a strict process-wide wall-clock guarantee. The harness also propagates durable run/receipt incompleteness and stage-loss records even for completed goals. Such goals retain their completion outcome, but stop further soak submissions; their reported tokens are a lower bound and are excluded from completed-goal token percentiles. Aggregate tokens explicitly declare lower-bound status. Confirmed timeout cancellations are read back as incomplete receipts with their actual tokens and stage usage counted toward the aggregate cap; a failed or unconfirmed cancellation instead marks provider usage unknown. A confirmed cancellation during an active model stage also marks that stage's unreceipted provider usage unknown; settlement-only timeouts can report both stage receipts. If the server stays unreachable at timeout, cancellation cannot be confirmed. An orphaned task without a terminal receipt is marked `provider_usage_unknown`: zero reported tokens then means no durable accounting, not zero provider spend. It stops on an incomplete run or a declared token/run/time admission bound. Harness v7 submits the remaining aggregate token allowance through the shared run `budget.max_tokens` admission boundary, records `admitted_max_tokens`, and stops at zero allowance even with a zero reserve. This corrects the previous report-only aggregate cap; idempotent creation retries carry the same budget. Alternative rejected: relying only on post-run accounting leaves one admitted run with an unlimited token budget. `--reserve-tokens-per-run` conservatively withholds allowance before each new run; neither that reserve nor the run budget is a provider-enforced hard token cap, because an in-flight model response can overshoot. One admitted run can finish past the aggregate token/time threshold. Its p50/p95 measure whole-goal latency; token totals are a cost proxy, not a currency bill; optional `--server-pid` samples that process's RSS, peak and growth, not its worker tree. Throughput is completed runs per observed wall time, not model tokens per GPU second.

Harness v7 also preserves the admitted run identity and the greatest valid
observed cumulative counters when observation fails. **Problem:** a counter,
receipt or readback error previously discarded known usage and could leave an
active probe running. **Decision:** failure receipts retain those lower bounds,
mark provider usage unknown, and stop further submissions. A nonterminal owned
run receives one explicit cancellation attempt and exact identity/status
readback; an earlier cancellation attempt is not repeated. Cleanup failure
remains separately visible. Already terminal work is not cancelled. Foreign
response identities never contribute usage or grant cancellation authority.
Boolean, negative, fractional, null, nonfinite and missing cumulative counters
refuse. Cleanup identity/status confirmation is independent of unknown cleanup
usage; counter maxima never consume invalid values.

Rejected: zero usage on exception, treating a cancellation response as durable
termination without readback, or cancelling a foreign response identity. These
are observer safeguards over the existing authorized cancel API, not a new
runtime recovery loop, model probe or research-quality guarantee. The observer
retains only one identity/status and two counter maxima; failures may add at most
one cancel/readback pair with the existing transport timeout. Reports still hold
at most the CLI's admitted run count. Verify `test_evolution_probe_failure.py`
and the existing soak timeout/idempotence tests. A controlled identical-input
counter-regression comparison reports 0/non-lower-bound before, and 35/lower-bound
after cancel readback; both fail and neither makes a model call. These are
synthetic reported counters, not actual provider spending.

```bash
python -m evaluations.evolution.soak --live --runs 1000 \
  --duration-seconds 86400 --min-start-interval-seconds 600 \
  --max-total-tokens 1000000 --reserve-tokens-per-run 250000 \
  --server-pid <application-pid> --output output/evolution-soak/receipts.jsonl
```

Run a one-goal smoke first, then an unattended sample with limits chosen to cover the desired horizon. Use `--model-ref` with an owner-selectable structured model identity to keep the route fixed. Compare terminal completion, stage times, token use and RSS across identical conditions before changing concurrency or prompts. A process restart is a separate gate: transient evolution tasks still settle to `needs_attention`; the authorized re-admission design remains in `RUN_CONTINUITY.md`. Do not report the soak as restart-safe continuity or as proof that model quality improved.

**Alternative considered.** A browser-only replay was rejected because it cannot observe the executor or storage handoff. A synthetic task-only soak was rejected because it would hide dispatch, tool and terminal failures.

## Goal rooms and material shelves

Each Headless objective run is one room identity. The goal selector changes the
run route; `/evolution/start` is the explicit new-goal screen. Old detail,
questions, shelf and drafts are cleared on a goal change; delayed reads/mutations
cannot repaint the newly selected goal. New-goal input remains objective plus
optional model. This does not add a second project/goal ownership system.

Furniture links to real goal content: bookshelf → collected materials; whiteboard
→ selected plan and acceptance requirements; workstation → current task/receipts;
review desk → independent review; deliverables table → the actual result. Counts
are derived from returned records, never decorative. Native shortcuts support
keyboard and narrow screens; shelf titles open original sources in a new tab.

`library.py` owns a separate `evolution.libraries.v1` CAS record per owner/run,
so material writes cannot conflict with execution-stage receipts. Each shelf has
at most 64 links and 192 KiB UTF-8 JSON; full shelves refuse additions explicitly
and never evict collected user state. Link identity deduplicates updates, and
CAS retries merge concurrent writes up to three times. Supported kinds are
paper, repository, web, dataset and other. HTTP(S) source URLs only; no credentials,
control characters, local-file reads, downloads or server-side fetch. These are
reference records, not verification receipts or a full document/file store.

GET run adds the contract-defined `workspace` projection (materials, selected
stages, criteria). POST `/runs/{run_id}/materials` saves a link; DELETE
`/runs/{run_id}/materials/{material_id}` removes it. All routes retain tasks
scope and owner checks. The studio reuses its existing read flight; no new
poller or global cache is introduced.

The registered `evolution_material` tool is exposed only to evolution run
execution, including bounded Headless runs. Its handler checks the actual task's
owner, execution binding and current durable task ID. Save requires kind/title/
URL; list reads eight records per page with an explicit cursor. Ordinary chat
and independent review cannot access it. The bounded guidance block announces
shelf size/latest source titles so new user-added sources are discoverable next
round. Source metadata is untrusted reference data, never executable instructions.

## Studio interaction and measured limits

### Live office projection (2026-09-23)

**Problem.** A single status chip and a generic tool bubble concealed the
execution, review and coordinator states behind drawers. Sub-agent rooms had
nearly identical furniture, while their continuously animated Canvas redrew a
static room at 30 FPS. Viewers could mistake decorative room books for collected
sources.

**Decision.** The office now carries a compact read-only work board sourced from
the same run, workspace, coordination and swarm snapshots as the detail drawer.
It displays the three roles, material and agent counts, accounted tokens and
visible errors. Missing reads display an unknown mark. Live task SSE tool events
drive short executor/reviewer routes for web research, coordinator contact and
delegation; a tool completion returns the actor to its workstation. SSE remains
decorative: the run poll owns stage truth, and a book appears only after a
durable material record is read. Sub-agent room backgrounds vary by the rostered
role, while each room's status and brief remain roster data. Room backgrounds
are generated once, encoded as 133–166 KiB WebP files and requested only when
the viewer enters a pod; scenery never stands for a saved source or passed test.
A running pod repaints its working pose twice per second and has no animation timer when
hidden, idle or reduced-motion. In the 1536-pixel browser check the selected
running room painted 1–4 times in 1.2 seconds and zero times in 0.7 seconds
after switching back to the main office; previously the active room repainted
its entire Canvas at up to 30 FPS.

**Alternatives considered.** A new status polling endpoint would duplicate the
run authority and increase idle cost. Creating books on `web_search` start would
claim a saved source before `evolution_material` commits one. Replacing the
existing sprite and prop atlases with more generated bitmaps would add download
and decode cost without exposing more backend truth.

`frontend/src/features/evolution/workspace.ts` composes the primary program notebook, the execution studio and
existing evidence pages; `studio.ts` owns only its mounted DOM and requests.
Problem: opening the notebook also imported the office renderer/vendor controls, and failed research imports left a blank page. Decision: each route loads its own graph through the shared document-lifetime module cache. `route-host.ts` retains the latest pending path, disposes the old page before admission and fences late success/failure. Closing before admission starts no import; closing during a flight permits no later mount or reload. The shell mount handle owns repeat-open identity and preserves unsent forms. There is one mounted page, at most four cached module flights, and no additional timer, observer or transport.
CSS failures stay cached. Child routes use the shell's single bounded reload/replay authority, then show a localized failure with an explicit reload control. Rejected: static imports of hidden pages, per-route reload guards and retrying failed CSS imports that could mount unstyled UI.
The measured production notebook's incremental graph (excluding already-loaded shell chunks) fell from approximately 136 to 21 KiB gzip; this measures transfer size, not whole-page latency. Browser tests assert emitted page requests/vendor registration and inject a failed studio CSS transaction.
The studio uses cream paper, forest ink and sage surfaces around authored office assets; controls, text and state remain DOM.
The office has reserved regions for the persistent room directory, mission HUD,
scene and furniture tool shelf. The task heading opens details and human questions. No content panel opens by default. A uniformly scaled 4:3
world fits inside the viewport; narrow screens start centered and keep all
furniture reachable through the dock. Details, goal creation, Ask Human and
navigation use secondary Web Awesome drawers with Escape, focus trapping and
focus restoration. Object pins use Web Awesome tooltips; both keyboard labels
and touch-accessible dock labels remain available. `ui-components.ts` registers
only drawer, tooltip and button through `studio-entry.ts` on the studio route, scopes vendor root
tokens to the studio, and uses bundled system icons with no CDN requests.
Web Awesome is pinned to 3.13.0 (MIT); forms retain native browser semantics.
The library does not replace the Canvas renderer or introduce a game engine.
The office uses one fixed light frosted theme, independent of the chat theme.
Keyboard focus and narrow layouts are supported. The preview has no state/theme
toolbar; empty/error fixtures are selected only by test URL parameters.
The studio uses tinted frosted surfaces: a 560px desktop drawer inset 24px
from the viewport, a 760px delivery drawer, and 8px outer margins on phones.
Headers, objective selection, navigation dock and material cards share the
same theme-derived glass tokens. Native select pickers receive progressive
`base-select` styling where supported; fallback remains native. Unsupported
backdrop filtering and reduced-transparency preferences use opaque surfaces.
The core office interaction adds no new font, dependency or animation loop;
lazy room backgrounds and one bounded pod pose timer are described above.
`office-scene.ts` renders three supervising human sprites with four-direction walk
frames; `office-navigation.ts` bounds paths to a 48×36 grid with furniture
clearance. Select a person, then click clear floor or use arrow keys to walk.
Execution/review move to their respective desks; Ask Human moves the actor to
the entrance and clicking it focuses the response form. Manual walking is
presentation. Tool-triggered routes mirror live task events but do not claim a
result or replace persisted evidence. Motion is never required to understand task state.
Books are reconciled by durable material ID per selected goal, including after
the run becomes terminal. A newly committed agent material takes the desk-to-shelf
trip while execution is active; user-added and preexisting links appear on the
shelf directly. A newly observed durable `assignment` journal entry sends the
coordinator to the executor's desk once; a refresh never invents another
conversation, and walking does not claim that the executor accepted the update.
The original atlas is sampled using measured frame bounds, without raster edits.

**Problem.** The first office looked like a status board: the scenery already
contained chairs, while the Canvas drew another pair, and most characters only
changed clothing color. A desk arrival therefore could not read as a physical
action. **Decision.** The shipped chair-free WebP keeps the same 1448×1086
composition; each desk owns one dark rolling chair with its own position and
eased slide. The executor and reviewer approach from the side, pull the chair,
sit, and roll under the desk. Leaving rolls back, stands, steps aside, and
pushes the chair in. A task update interrupts the sequence by restoring the
chair to its tucked position before starting the new route. Activating a chair
target with pointer or keyboard runs the same presentation-only sequence. `office-character.ts`
adds role-shaped silhouettes and equipment on top of the walking atlas in both
the main office and agent rooms; visitor gear follows the live role string.
**Alternatives considered.** Baking more chairs into scenery prevents motion;
six additional full directional atlases would add decode memory and a second
animation pipeline. The vector equipment shares the existing gait and incurs
no separate texture decode. These animations do not claim a task completed or
a source saved; the backend evidence and durable material list remain authority.

### Persistent room directory and reserved scene layout

**Problem.** The room switcher disappeared without a live sub-agent roster, and
room tabs competed with task actions in the bottom dock. People could not find
rooms. HUD overlays obscured furniture, and agent removal kicked viewers back
to the main office.

**Decision.** `room-catalog.ts` defines six always-visitable rooms: main office,
research, engineering, review, writing and general. The main room hosts the
three supervising roles. The other rooms group the live swarm roster by actual
role; multiple occupants have separate identity-bound briefs and selection.
`office-rooms.ts` reconciles members in place so status updates preserve keyboard
focus. Removing the selected member selects another current occupant, or leaves
the room empty. A missing roster is explicitly unknown; an empty successful
roster never displays a manufactured worker. Browsing rooms launches no task.

Desktop uses a persistent illustrated room rail, a two-row mission header,
a 4:3 contained scene and a dedicated bottom tool shelf. The header is actionable
for task details and human questions. Secondary management actions remain in the
menu; the room rail owns new-goal and coordinator-chat entrances. At 850px and
below a labelled Rooms button opens the same directory in a focus-managed drawer.
On phones the stage pans at readable scale; the member brief moves to the tool
shelf so it remains visible after the directory closes. The scene and navigation
have separate layout regions; neither the mission header nor the tool shelf
covers interactive furniture. Escape closes drawers and restores their trigger.

Six 160×120 thumbnail derivatives total **18,764 encoded bytes / 460,800 decoded
RGBA bytes**. Full backgrounds still load only on entry. Only one pod renderer
exists, and returning to the main office disposes it; empty/terminal rooms have
no recurring paint timer. A browser assertion observes zero paints over 700ms
in an empty room, with the main canvas hidden. The directory adds no fetch timer,
worker, persistence or model call. Occupancy uses the studio's existing read flight.

**Alternatives considered.** Keeping tabs in the bottom dock preserves the
original discoverability problem. One room per agent makes empty offices
unavailable and causes navigation churn. Pre-mounting every room would retain
unnecessary canvases and decoders. A static building screenshot cannot expose
live identity, status, focus or empty-room behavior.

### Cohesive game art and viewport HUD (2026-09-24)

**Problem.** Vector equipment and chairs read as overlays against the textured
office, and recolored bodies still shared a silhouette. The large white
status board obscured the right workstation. On phones, fitting the whole 4:3
map left most of the viewport as empty blurred letterboxing. Private rooms
also contained a baked chair beneath the animated actor.

**Decision.** Eight independent role-specific 4×4 walking sheets replace the
vector costumes; a transparent upholstered chair is the shared moving prop.
The five private-room backgrounds have chair-free variants. Main-office
chairs keep the pull/sit/roll-in and reverse sequence. A private room starts
directly in the observed status pose; when the same rostered agent changes
from pending to running or running to terminal while visible, one 2.1-second
presentation sequence walks, pulls or restores the chair, and settles. These
beats change no task state. Hidden or reduced-motion rooms settle immediately.
The mission HUD retains the same backend facts and unknown marks; its layout
and always-visible room directory are specified below. Phones keep the stage at playable
scale and pan horizontally inside the office; the HUD stays fixed. The bottom
dock scrolls horizontally at narrow widths so its controls keep legible text.
Asset prompts, role briefs and crop authority are in
[`EVOLUTION_GAME_ART.md`](EVOLUTION_GAME_ART.md).

The four primary first-view sprites (three leads and chair) total 1,011,686
encoded bytes and 25,168,928 decoded RGBA bytes; the older walking atlas was
2,324,029 encoded and 20,030,976 decoded bytes. The old atlas is now requested
only after an authored-sheet load error. All eight role sheets plus the chair
total 2,730,310 encoded bytes but load by role, not at startup. The five
chair-free pod backgrounds total 913,026 encoded bytes and load only when a
pod is entered. A running pod still paints at 2 FPS; the arrival/leave beat
uses at most 30 FPS for 2.1 seconds. The main office retains its existing
30-FPS moving / zero-RAF stationary lifecycle. These are static asset sizes
and bounded browser paint observations, not a measured cross-device latency
claim.

**Alternatives considered.** Using the attractive concept image as the
runtime background would bake in characters and false positions. Additional
vector costume details keep the art mismatch. Loading both old and new sheets
on every entry duplicates texture memory. Scaling the entire map to phone
width makes the people too small to read. A full game engine would add startup
and idle cost without changing the backend-to-visual contract.

The two role markers represent real execution and review task IDs, sequentially;
they are not a claim of parallel Agent collaboration. Clicking a recorded role
opens Tofu's existing Request Inspector. Results and review evidence remain
readable after task eviction. Download exports the complete inline deliverable.
Ask Human preserves drafts across refresh, presents every batch field, never
pre-submits suggested choices, and resumes through the existing owner-bound gate.
Submission retries retain an idempotency key for the same objective/model.

One visible-page read flight polls active work at 3s and idle/history at 15s;
model options load once, at most 128. Unmount aborts reads and clears timers;
hiding pauses polling. The list transports at most 50 short summaries, excluding
large deliverables and learning blobs (budget test: <40 KiB for 50 CJK objectives).
The shipped chair-free scenery plus human atlas total 2,545,403 bytes encoded
and 26,321,088 bytes decoded (the asset test enforces <=5 MiB / <=27 MiB).
The original 2,024,654-byte PNG is retained only as an art comparison and is
not loaded by the studio. One canvas caps its backing store at
1440×1080, keeping total raster memory under 19 MiB, independent of device DPR.
Walking paints at most 30 FPS; standing actors have no RAF. Idle wandering wakes
at 6.5 seconds, with at most one timer and one pending RAF. Hidden/offscreen scenes
cancel both; unmount disconnects observers, handlers and zeros the backing store.
Pause and prefers-reduced-motion stop animation; manual movement then places the
actor at the valid destination instantly. No game-engine dependency is added.
These are bounds and browser-observed frame counts, not cross-device CPU claims.

Art research: [LimeZu Modern Interiors](https://limezu.itch.io/moderninteriors),
[Belohlavek's CC0 walk sprite](https://lpc.opengameart.org/content/character-sprite-walk-animation),
and [Pipoya characters](https://pipoya.itch.io/pipoya-free-rpg-character-sprites-32x32).
Further office candidates are [OpenGameArt Office Space Tileset](https://opengameart.org/content/office-space-tileset)
and [indoor office appliances](https://opengameart.org/content/indoor-office-appliances)
(both listed CC0), or [LimeZu Modern Office](https://limezu.itch.io/modernoffice)
(paid, commercial use with credit; redistribution of the source pack is barred).
Shipped scenery and people are original built-in Image Gen outputs, not copied
third-party asset packs. Prompts and comparison notes live with the v2 preview
artifacts in `output/evolution-design/v2/README.md`.

**Current evidence level: learning feedback is connected; capability improvement
is not yet demonstrated.** More stored strategies, successful reviews, or lower
cost on different tasks do not establish improvement. `holdout.run_regression(paired_probe=...)` accepts fresh baseline/candidate
observations from a trusted runner. It excludes mutation-evidence lineage,
requires at least three complete pairs, blocks any observed regression, and
records wins, losses, ties, unknown costs and an exact one-sided sign-test
p-value. `observed_gain` describes these cases, not statistical certainty or
general capability. A pass can mean no observed regression, not improvement.
Candidate and suite fingerprints bind activation to the measured inputs; old
unbound receipts and changed inputs require reevaluation, even with a waiver.
The default probe remains unmeasurable.

`evaluations/evolution/python_delivery.py` separately checks completed JSON-wrapped Python deliveries by bounded compilation without executing them. The paired runner accepts `--checker python_delivery_compile_v1` with frozen case answers `{"compiles":true}`; invalid rubrics refuse before model calls. This compares compilation delivery only, retaining failures and their costs. Syntax failure and incomplete provider output cannot pass; compilation alone proves neither behavior nor scientific validity. This gives repair feedback without a second executor. The retained `evaluations/evolution/format_fidelity_cases.json` suite checks literal JSON string/escape/array preservation, including Python identifiers and evidence paths. It is a copy-fidelity compatibility test, not a held-out reasoning or research-quality benchmark; compare plain requests and `--json-object` separately with identical instructions, using fresh report paths. Failures do not mutate registered model capabilities. The opt-in `evaluations/evolution/paired.py` runner defaults to the running
application Chat API with its existing owner model catalog and server-managed
credentials. `--run ID --role reviewer` reuses that goal role's selection;
`--model` accepts a structured reference. It uses fresh turns, one concurrent call, no tools, matching model/configuration,
alternating arm order, a 600-second total admission budget, 60 seconds per
call, and 2048 output / 8192 total tokens per arm. It accepts 3–16 frozen JSON
delivery cases and checks exact canonical JSON (duplicate fields and decoder nesting exhaustion fail scoring while completed-call usage remains counted) against answers withheld from
model messages. An explicitly selected `--checker json_numeric_equivalent_v1` treats finite JSON numbers such as 1 and 1.0 as equal while requiring the same keys, array order and all other values; booleans never equal numbers. Default strict scoring stays unchanged. Problem: numeric notation can obscure correct arithmetic. Decision: record the chosen checker in preflight and live reports. Alternative rejected: silently changing old scores or using tolerant/coercive equality. Neither checker proves physical execution. Observed provider/model drift invalidates the comparison. Missing reported model identity yields unknown route consistency and excludes the paired quality comparison; individual scoring and known costs remain reported. Structured model references compare by canonical identity, without relying on hashable Python objects. Matching reported identities still do not prove resolved provider identity.
Chat evaluation pins `/api/health` boot identity before calls and rechecks afterward. A changed/unavailable identity refuses new dispatch; completed replies and costs survive a failed post-check, but their quality score is excluded. Reports retain the initial boot identity and advisory source fingerprint; missing fingerprints remain unknown. This prevents mixing process generations without claiming full code provenance. Chat completion responses expose the requested model, so provider fallback
is not independently verified by this adapter; its report is not activation
evidence. Timeout or
missing usage remains unknown; malformed usage objects or any supplied noninteger/negative input/output alias also keep tokens unknown, without discarding a completed answer’s quality score. The shared usage normalizer remains the alias authority once those supplied counters are valid. The CLI exclusively creates a fresh output path before provider setup; existing evidence refuses a rerun without overwriting or calling the model. A new experiment requires a new output path; no resume/replay is inferred. Concurrent admissions have one winner. Before each call the same report records an `inflight` item/arm intent; an interrupted pending call has unknown provider outcome and cost, never a zero-cost receipt. Intent-write failure prevents dispatch. Finished-arm checkpoints clear the intent; no automatic resubmission is introduced. Each finished arm atomically checkpoints original output, score and known cost before another call. Partial reports carry `evaluation_state=running`, exclude aggregate comparison and remain ineligible for activation; a checkpoint failure stops further calls. Problem: process loss previously erased all completed arms. Decision: flush and replace a bounded report after each arm. Alternative rejected: automatic replay risks duplicate charges and changes the experiment. Final reports carry `evaluation_state=completed`. Reports retain exact answer text up to 64 KiB per arm alongside its hash and byte count, so failed deliveries remain auditable offline. Larger answers keep their hash, score and known costs with explicit non-retention; they are never silently truncated. At most 32 arms retain 2 MiB of answer text before JSON escaping. Problem: hashes alone require a still-live task API to inspect failure. Decision: bound original-answer retention in the evaluation artifact. Alternative rejected: keeping only digests loses independent offline inspection; unbounded text permits artifact growth. Reports contain hashes and bounded metadata,
not raw provider errors or credentials. This narrow harness measures prompt
variants on structured deliveries, not tool execution or general research
quality; its files never automatically activate production mutations.

```bash
python -m evaluations.evolution.paired --cases /path/cases.json \
  --baseline /path/baseline.txt --candidate /path/candidate.txt \
  --owner 123 --output output/evolution-eval/report.json
```

Cases are objects with `id`, `objective`, and `expected` in a JSON array. Preflight, CLI and direct evaluation reports carry harness version 2 plus source fingerprints for the runner, production paired comparison owner and its observation contract; result-schema version alone cannot identify scoring semantics. Both file intake and direct evaluate calls freeze and validate the complete bounded case list before model access; caller mutation cannot change either arm or its suite fingerprint. Frozen case files and model answers share one strict JSON decoder; duplicate fields at any depth and decoder nesting exhaustion refuse input during offline preflight, before model access. The
command defaults to offline preflight. Optional --json-object requires object answers before model access and requests identical structured object mode for both Chat and embedded-runtime arms; reports retain response_format. Plain mode supports scalar/array cases, and Markdown fences still fail strict scoring. A first live three-case probe showed fenced answers plus an actual candidate seed-pairing error; that probe does not establish improvement or research quality. A fresh structured-mode rerun passed all six arm calls (three ties, no gain), with reported 7,007 baseline versus 10,419 candidate tokens and 28.8 versus 56.7 seconds summed latency. Executor coordination reports explicitly identify themselves as progress-only and direct completed work to the ordinary final assistant answer; the transient response annotation never changes the stored event, terminal state or verdict. This addresses a live final-step probe that spent repeated rounds reporting progress without delivery, but reduced model-loop cost is not yet established. These are one small live probe, not a causal format study, provider-verified comparison, or broad research benchmark; the more verbose candidate is not promoted. `--live` connects to `--app-url`
(default loopback port 15000); authenticated installations use their existing
`TOFU_API_KEY`. The supplied owner must match the authenticated Chat principal.
No model key is exported. Explicit `--runtime-config` retains the independent
`TOFU_AGENT_MODEL_ROUTING` adapter for embedding evaluations. Baseline/candidate instructions and evaluation cases must be
frozen before running; keep training examples separate. Reports overwrite
the requested output path; no background jobs or durable experiment store
are added. No live-model quality or throughput claim follows from hermetic
workflow/UI tests.

## Accountable goal coordinator and live updates

The coordinator is a logical goal role, instantiated on demand as an ordinary
Tofu task. `lib/evolution/skills/goal-coordinator/SKILL.md` is its actual loaded
instruction source, not a separate design-only prompt. It defaults to the goal's
pinned model; an explicit coordinator role override applies on its next launch. No global chat prompt, installed skill catalog, second LLM loop,
resident worker or scheduler is added. Its tools are bound to this run and owner;
`evolution_coordinate` handles read/detail/ack/assign/report through the existing
registry, write policy, tool gateway and execution limits.

A `message` update starts the coordinator. Once enabled, material changes and
terminal results wake it after commit; active execution receives owner and
coordinator updates at the next closed tool boundary. A running model call or
tool is not interrupted by an informational update. Ask Human answers remain
on the existing answer endpoint: a message is never an implicit approval.

The bookshelf record also owns the coordination journal. Material changes and
their notification are one CAS commit. User conversation, guidance, reported
experiments, assignments, source changes, coordinator replies and terminal
results have increasing sequence numbers and explicit origins/targets. Reported
experiments and collected links never become verified episodes merely by being
recorded. Deleting a source removes it from the shelf but preserves its history.
The coordinator supplies a concrete assignment to the current execution task;
independent review remains a distinct task. The coordinator cannot spawn workers;
the executor may use the existing bounded swarm tool with aggregate child-work
accounting, and its roster drives the office's additional rooms.

`evolution_updates` is an evidence-authority inbox carrier in the existing
context composer. Shared goal-tool admission checks owner/identity and local terminal/abort state before and after authority reads; stale carriers cannot read or propose work.
Revoked carriers skip informational injection; archival/settlement retain their existing ports. It never splits a tool-call/result pair and adds at most eight updates in
an approximately 8,000-character data page. Oversized reference lists have an
explicit `detail_required` projection; the detail API/tool returns the full
original. Task-local presentation cursors mean only *appended to model context*.
Only `ack` records an explicit model decision, with accepted/not_applicable/
conflict and a note. Neither presentation nor acknowledgement proves execution.
Unacknowledged or conflicted guidance prevents an otherwise passing run from
minting a verified episode and yields `needs_attention`. An update arriving
after a completed run is new work, not retroactive evidence about that result.

The manager claims its task ID with CAS before spawn. Repeated messages use an
Idempotency-Key; concurrent wakes cannot spawn two managers. A message arriving
while a manager is running remains in the journal and schedules another bounded
turn after settlement if it was not presented. Terminal settlement is fenced by
manager task ID/status and does not double-charge usage. Lost tasks become
`needs_attention`; reconcile never replays unknown effects. Wake notifications
are hints: a failed wake leaves committed data readable, and coordinator
reconcile recovers pending wakes and missed terminal-result notices.

Limits are explicit: 64 source links / 192 KiB of source data; 768 KiB for the
combined goal workspace; 128 events, with external inputs capped at 96 to leave
slots for final replies/results; 32 task acknowledgement records. External
writes leave 128 KiB of byte headroom for receipts/replies; manager launch also reserves 96 KiB before admitting another reply. Full storage refuses
new work without evicting user data. Coordinator consumption is telemetry, not
an execution quota: no cumulative token/tool/turn ceiling or synthetic per-turn
deadline is injected. **Problem:** a long-lived goal must not lose its coordinator
after sixteen owner interactions. **Decision:** preserve single-flight claims,
read-only role permissions, explicit cancellation and record-capacity admission;
remove the fixed execution allowances. **Alternative rejected:** increasing the
old constants only postpones the same interruption. The ordinary task engine
owns admission and cancellation. At most one coordinator runs alongside its
execution/review task. It adds no pool or persistent polling loop. Source additions never mint a
fresh execution budget. Ordinary goal cancellation disables automatic manager
wakes and cancels its active task as well.

A terminal goal can receive an **explicit** follow-up POST. It creates one linked
run with the default unlimited execution policy, the parent's model, original objective,
owner's new guidance, and a frozen context snapshot. The snapshot declares its
source revision, material count and truncation, retains recent reports and the
prior deliverable, and is at most 24,000 characters. The follow-up intent commits
before run creation and uses a deterministic submission key; retries never
create duplicate execution. The old result and its evidence remain intact.
Further continuation belongs to the returned child goal. This interface does
not automatically infer permission to rerun work from a library edit.

### Shared conversation/UI API

All operations require the existing `tasks` scope and explicit run ownership.
Payloads and persisted fields are defined in `evolution_run_v1.schema.json`.

| Operation | Meaning |
|---|---|
| `POST /api/v4/evolution/runs/{id}/updates` | `kind` = message/guidance/experiment, `content`, optional `references`; required Idempotency-Key. Message enables/wakes the spokesperson. |
| `GET /api/v4/evolution/runs/{id}/coordination?after=0` | Bounded event page, revision, acknowledgements, coordinator task/status/reply and follow-up identity. |
| `GET /api/v4/evolution/runs/{id}/updates/{seq}` | Complete original event when a preview requires detail. |
| `POST /api/v4/evolution/runs/{id}/coordinator/reconcile` | Recover missed wakes or diagnose missing runtime, without replay. |
| `POST /api/v4/evolution/runs/{id}/followups` | Explicit new execution: kind=guidance, content, required Idempotency-Key; returns linked run. |

The conversation drawer renders this journal, without a second message store. Show “saved”, “presented/acknowledged”, and “verified result” as
different facts. Existing task IDs connect the manager to normal task events and
Request Inspector. Ordinary unbound chat does not expose coordinator tools.


### Joint conversation and office experience

The office retains the full scene. A small spokesperson button opens a frosted
conversation drawer. The goal input and optional model choice stay in the creation
drawer. Task questions, source shelves, review evidence and downloadable results
retain their furniture entry points; exploration never creates execution.

Natural messages use `kind=message`, target only the coordinator, and do not enter
the executor's inbox as instructions. The coordinator distinguishes questions,
evidence and requested changes. `assign` requires a presented owner `source_seq`;
the conditional schema declares `source_seq` locally in its `then` branch so
provider tool-schema preflight keeps `evolution_coordinate` visible. Problem:
the valid cross-branch JSON Schema requirement was rejected by providers that
require every `required` field to appear in the same branch's `properties`.
Decision: add an unconstrained local declaration while the root property keeps
the sole integer bound. Alternative considered: remove the conditional from the
provider schema, which would let models omit the required assignment reference.
receipt projections connect the original message to its assignment and executor
acknowledgement. Labels mean saved, considered, assigned, accepted, not applicable
or conflict. None means independently verified. Interpretation is model-mediated;
material ambiguity is answered with a focused question, not keyword routing.

`GET coordination?view=recent` returns the newest bounded page; `before=<seq>`
returns preceding updates. The server derives delivery labels from durable receipts,
not client inference or agent prose. Full-detail reads recover truncated content.
The browser retains at most 128 projected events for the selected goal and eight
unsent drafts (4,000 characters each); disposal clears them. There is no browser
transcript persistence or additional polling timer. Studio uses its existing
visible-page single flight, polling at three seconds during executor/coordinator
work and fifteen seconds otherwise; mutations refresh past pre-write reads.

Ambiguous sends retain their idempotency key and original payload, expose retry,
and lock editing until resolved. Goal switches fence late responses and preserve
bounded in-memory drafts. A terminal goal offers a separate, explicitly labelled
follow-up button: its textarea becomes the new guidance and the returned child
opens with inherited context. Sending a conversational question never calls it.
Existing follow-up identity remains navigable from the parent.

Runtime integration is part of this contract: the native blueprint is registered;
the tool builder/routing/handler adapter is wired to ordinary tool settlement;
terminal listeners run before transient evidence release (successful callbacks
are deduplicated, failures remain reconcilable). Internal `toolSpecAllowlist` and
`toolSpecDenylist` narrow both visible and executable catalogs. Coordinators allow
only evolution coordination and source-library tools, reviewers allow no tool specs, and execution admits
swarm only through aggregate child budgets: a spawn wave slices the run's remaining
token/tool-call budget per child (enforced at each child's own admission
boundaries), a spawn against an exhausted budget is rejected, and every
completion folds the child's bill into the parent task's admission counters and
episode actuals. Ordinary tasks without these fields
retain their catalog. Atomic tool admission and per-round token/deadline admission
use `execution_limits`; these limits do not imply a bound on an already-running
provider's reported output beyond its configured response limit.

The coordinator can page through the actual deliverable with `action=delivery`
(4,000 characters per call plus stored review), rather than treating the initial
preview as the entire result. Its prompt declares whether that preview is truncated.

Workspace transport lifecycle: entering Evolution suspends Chat display SSE
before its lazy feature import. Existing and newly created conversation
coordinators retain demand, projections and replay cursors; returning to Chat
resumes from those cursors. In-flight snapshots cannot reopen suspended streams.
Problem: six hidden Chat EventSources exhausted HTTP/1 browser connections;
live studio sprite requests remained pending beyond 45 seconds although direct
asset reads completed in roughly 30 ms. Decision: the shell injects visibility
into the typed Turn runtime; suspension clears source/reconnect/silence handles
and introduces no polling, backend cancellation, worker, or stored state.
Alternatives: image priority cannot release occupied sockets; disposing stores
would discard replay state. Preview-only tests cannot detect live catalog load;
verify production navigation and sprite readiness as well as replay lifecycle.

### Per-role model selection

Click an office actor to inspect its role and choose from the same owner-scoped
Chat model catalog; the coordinator has a matching menu entry. `role_models`
on intake optionally sets executor, reviewer and coordinator overrides. Missing
roles inherit the goal model. `POST /api/v4/evolution/runs/{id}/role-model`
accepts `{role, model}`; null removes the override. Updates use the run CAS
and affect subsequent launches, never mutate a live task or replay effects.
Stage receipts retain the model reference actually used; original intake
choices remain separate for idempotent retries. Follow-up runs inherit overrides.
No additional credential/configuration store or resident worker is introduced.

Executor explicit result delivery: Problem: a real bounded probe put its complete
answer in progress reports and exhausted its budget before final delivery.
Decision: `evolution_coordinate submit_result` uses the existing sole-call terminal
reply port, bound to the current executor and frozen stage launch. The ordinary
terminal archive and independent reviewer still decide acceptance; preparation
writes no verdict and grants no command evidence or replay. Alternative rejected:
interpreting every report as completion would terminate legitimate experiments.
Submission also requires the current admitted dispatch to name both the live task
and frozen launch; the terminal verifier retains the entire dispatch binding.
A valid launch alone cannot authorize another or merely pending carrier.
The existing bounded content schema applies to this optional tool path; ordinary
assistant final replies remain available. No measured cost reduction is claimed.

Contract lifetime: Problem: the 32-entry schema LRU could evict a loaded original,
reload edited disk content and diverge from an older cached validator. Decision:
on first use, capture the finite `.schema.json` catalogue as immutable strings;
parse each only when requested and retain parsed schemas for the process. New
names require a new process. Validator eviction rebuilds from the same captured
originals. Alternative rejected: enlarging the arbitrary LRU postpones drift;
stat-based reload intentionally mixes epochs. This pins contracts, not Python
code: coordinated deployment/restart remains necessary for code/schema alignment.
The current 39-file catalogue contains 423,020 characters. One fresh-process
measurement after imports captured it in 1.8 ms with 447 KB traced retention;
parsing all entries retained 1.91 MB (validator cache excluded, filesystem cache
uncontrolled). Retention is bounded by the captured application catalogue and
ends with the process; no worker, timer or durable cache is introduced.

Programme header CAS publication requires exact value and expected next version
readback from the selected authority, including after a lost ACK. Missing or
conflicting publication requires reloading authority; no experiment is launched
from an unconfirmed intent. Problem: reply-shaped success could hide lost
objective/budget/frontier state. Decision: read back before progression; rejected:
trusting ACK shape or replaying submission after uncertainty. This adds one
bounded header read per normal save, without a worker, cache or new namespace.
Verify programme fault-injection and progression tests.

Command execution evidence recognizes project `run_command` and standalone
`code_exec` producer identities through one maintained set. Completion still
requires observed zero exit without timeout/interruption/refusal; stdout or model
claims cannot override failed execution. Background handles retain owner/task
checks, and scientific extraction uses the same producer set. Problem: actual
standalone commands were archived but rejected as unobserved. Decision: recognize
both shipped identities; rejected: trusting arbitrary tool names or display text.
Headless tests cover HTTP → real command → archive → review on both stores,
including paired search measurements and contradictory claims. Controlled review frames prove plumbing, not independent research quality.
