# Committed evolution stage outcomes

Authority: `lib/evolution/stage_outcomes.py`, `terminal_checkpoint.py`, and the `stage_outcome`,
`stage_outcome_part`, `terminal_checkpoint`, and `stage_outcome_page` definitions in
`../contracts/evolution_run_v1.schema.json`. The existing run consumer owns
progression; the task engine owns execution and terminal delivery.

## Problem

A transient executor can finish before the run receipt and next-stage request
are committed. Losing that process used to lose its result and measured
consumption, even when the external work had completed. Replaying an executor
could repeat writes. Reconstructing review evidence from its answer could
turn self-reported experiments into verification.

## Decision

The required terminal receiver archives the original terminal facts, observed
consumption, full output digest, and exact independent-review input before it
changes run receipts. The archive binds the owner, run, task, episode, attempt,
and immutable stage-launch identity. Ordinary completion requires the shared
terminal verdict; bare `done`, truncation, and failed streams cannot authorize
review. Research continues to use its native durable pipeline authority.

Problem: terminal callbacks and run polling can both observe a running episode;
one settles before the other's read or CAS, while run progression is still pending.
Decision: run-owned settlement explicitly reconciles through `episodes.py`.
Only an identical owner-bound settlement, including usage completeness, verdict,
receipts and learning, confirms the immutable winner without another write.
Commit timestamps remain the winner's; conflicting contents and unavailable
authority still refuse. Ordinary settlement remains strict by default. Recovery
after an episode commit checks the same evidence before completing the run.
A frozen unstarted repair names its predecessor through `stage_launch`; refusal
to admit that successor preserves the predecessor's already settled episode.
Alternatives: ignoring terminal errors can accept contradictory evidence;
process-local locks cannot protect other processes or recover lost acknowledgments.
No worker, cache, journal or retry loop is added. Deterministic callback/poll and
interrupted-repair tests plus concurrent SQLite/TofuDB CAS tests cover this boundary.
The settlement receipt
records the reproducer, verification scope and remaining whole-goal limits.

Managed durable stages prepare complete immutable output/tool parts through the
existing required terminal-delivery port. Parts alone authorize no progression.

**Problem.** A background command can finish without its complete output
remaining in model-visible messages. A completion boolean alone cannot support
independent result review. **Decision.** Terminal preparation reads current
producer launch handles through the existing owner-scoped zero-wait snapshot
interface and includes all available completed results, including failures, in
the existing immutable tool-evidence JSONL stream. Records identify the command
and original launch call; complete text is streamed into the same bounded parts.
The descriptor scope explicitly includes background completions when present,
and the existing source reader validates and pages that scope. No parallel
archive or model-visible transcript is created. Original task messages remain
unchanged. Terminal request context carries the command-observation policy,
so an authenticated capsule cannot accidentally omit its execution fact.

The preparer retains one first-read snapshot until all source parts are
acknowledged. It then releases the full raw snapshot; subsequent result-write
retries verify existing immutable parts rather than rereading the registry or
retaining another log buffer. Before part acknowledgement, ordinary retries
reuse the same first snapshot; registry eviction or later mutations cannot change its parts or review
prompt. Once committed, original reads and recovery use verified archive parts
and never query the registry again. Missing/running handles remain unavailable;
this captures existing results rather than waiting for or restarting processes.
Legacy sealed outcomes keep their exact scope and contents. **Alternatives.**
Display-only completion previews were rejected as an authority; requiring a
late log reread was rejected because registry retention can end before review.
A separate background-result store was rejected because stage outcomes already
own immutable evidence. Snapshot text references live only for terminal
preparation/retry, not in the bounded result capsule; persisted growth equals
the existing complete result bytes plus small identity fields, with no new
worker pools or lifetime header growth. A synthetic 786,460-character Unicode
result produced 27 existing-size parts (786,654 archive characters); streaming
descriptor/preview calculation took 6.99 ms and 481,516 traced peak bytes beyond
the already supplied text. This measures archive preparation, not experiment
execution or scientific validity.
The ordinary owned task-result CAS commits the complete `terminalContext` capsule:
exact terminal facts, consumption/completeness, review input, content epoch and
submission marker including prepared-input identity. No transcript or runtime
config is duplicated in this capsule; its complete frozen review input has no
application byte/character admission quota. Preparation
failure retains ordinary retry debt and heavy evidence without a terminal write.
A stale writer rejected by the result CAS cannot materialize an outcome.


## Waiting for admitted background experiments

Problem: an executor may finish after starting a long experiment. Treating its
unfinished command as failure can start duplicate work; an in-memory wait loses
its binding after process loss. Decision: required-command stages freeze current
known-running launch IDs in their ordinary owned terminal capsule. Once that
capsule is consumed, the durable run stays executing with `background_wait=true`.
The original receipt accounts for consumption exactly once. The existing reaper
makes zero-wait owner snapshots; it creates no model request, thread or executor
while results remain running. Registry recovery uncertainty remains waiting.
Absent results after recovery require attention, never speculative replay.

All pending commands must resolve before joining. Completed results, including
failures, are streamed into the existing part repository under the distinct
`background_evidence` source kind. One immutable companion manifest binds the
original terminal digest; it lives in `evolution.stage_background_joins.v1`.
Parts are verified before manifest publication; run CAS alone admits the single
ordinary review. Original terminal facts, sources and hashes remain unchanged.
Review coverage must include the complete late source; receipt/context history
retains its reference. Existing read-only source tooling and browser paging
expose it without write or execution authority. Metadata reads do not rescan the
whole result for every page; join admission verifies its complete digest and
individual page reads retain ordinary immutable part checks.

The original executing deadline, including review reserve, applies throughout
waiting. Cancellation/deadline signals only the committed manifest's owned
handles and admits no successor. Supervised command workers retain their process
and lease lifecycle until exit. Explicit background commands use the existing
restart recovery; automatic handoffs without restart records can become
unavailable after process loss, which is reported rather than rerun.
Alternatives: repeated paid polling, replaying the executor, changing a sealed
terminal source and accepting a completion label without results were rejected.
No new timer, worker pool, source store or model loop is introduced. Waiting
headers are bounded by the existing record admission and hold only state/IDs;
result growth is immutable paged user evidence, not lifetime header accumulation.
A synthetic one-command manifest is 97 UTF-8 bytes and its wait-state object is
24 bytes. Repeated waiting polls produce no durable writes or new tasks. A
210,028-character result requires only one part read for its first 6,000-character
page, independent of the remaining body; these are storage/flow measurements,
not scientific outcome measurements.

After commit, the required receiver materializes the outcome only from an
owner-scoped result read with matching task/status/epoch/finish reason, admitted
or refused dispatch marker, and a version later than the original birth.
Every source part and full digest must verify first. A sweep seeing a resident
managed terminal task waits for its owned result commit. Process loss between
that commit and the callback consumes the same frozen capsule, never recomputes
review evidence or actuals, and never replays an executor. Concurrent materializers
reuse identical immutable metadata; run CAS still owns successor admission.
Missing originals preserve authenticated consumption but require attention after
owner loss. Corrupt or foreign capsules refuse; legacy absent capsules retain
the previous conservative policy. Cancellation dispatches no successor.

Direct legacy archival still commits metadata as `collecting`, then immutable
Unicode parts with offsets/digests, and seals after verification. Concurrent
callbacks reuse the first committed terminal snapshot. A terminal callback
acknowledges only an owner-bound run containing its exact task receipt; lost CAS
or unrelated writes retain delivery debt, never dispatch a stale request.

When a task is unavailable, callback/sweep reconciliation consumes a verified
archive without executing its old stage again. All durable parts can seal
a collecting archive after process loss. The successor review receives the
original prompt, including observed tool results. Receipts identify restored
outcomes. Cancellation records known consumption without dispatching a review.

Failed and cancelled executors retain a preview of their verified original
output and exact source references, including empty output and any saved tool
or declared-file evidence. Newly settled exploration receipts inherit these
references so the notebook can open failure evidence directly. Readable sources
grant no review, success, replay, refund or measured-cost authority; incomplete
usage remains a lower bound. A failed reviewer retains its executor's sources.
Existing immutable receipts are not rewritten. Linking only to execution details
was insufficient for discovering the original evidence in the notebook.

Missing parts defer while the stamped owner is alive. After owner loss or
unverifiable ownership, known terminal consumption remains in the episode,
but incomplete output requires attention. Corrupt parts or changed launch
bindings refuse recovery. An executor lost before any terminal archive exists
retains the existing unknown-effects policy; this is not in-flight write
resolution or universal restart continuity.

Consumption with unsettled physical requests or live children is a lower
bound. Consumption and completeness share one settlement lock snapshot so
late request settlement cannot certify stale totals as complete. That flag survives archival, restart, settlement, and finite-budget
admission; late usage cannot silently rewrite the immutable snapshot or train
measured cost priors. Archival does not refund any model consumption.

## Declared project artifacts

Problem: another full-context model request can exhaust the allowance after artifact work is ready. Decision: the sole final `evolution_artifact` call may include executor-authored `delivery` (1–24,000 nonblank characters). Every selected and previously declared generation must be complete. The manifest CAS freezes the exact reply, call/round/argument identity and generations before acknowledgement. The live generic task port requires the matching successful receipt, verified original provider tool finish, current executor authority, no cancellation or expired allowance, and no unknown or live model/child consumption. It then uses the existing checkpoint and terminal result CAS; independent review remains mandatory. Ordinary declarations are unchanged.

Alternatives: driver-written summaries and automatic completion at budget cutoff invent delivery or weaken evidence; an extra model closing request retains the observed affordability failure. A durable intent without the live successful acknowledgement never authorizes completion after a crash, executor replay, acceptance, additional budget, or a new deadline. Partial capture failures remain evidence and commit no delivery intent. Repeated identical prepared intents can acknowledge the frozen bytes; changed identity refuses.

For exact file-body delivery, the sole call may instead contain only
`delivery_source`, an already sealed current-executor artifact ID and byte digest.
The complete UTF-8 body, without a private file byte quota, becomes the final
reply without model recopy, formatting or a live-file reread. Binary, blank,
foreign, incomplete or corrupt sources refuse. All previously declared generations
must remain complete. The intent stores only the immutable reference; full bytes
stay in sealed parts. The same live acknowledgement, cancellation, usage, checkpoint
and terminal CAS guards apply, including after an identical intent retry.
The default executor instruction describes both routes: authored reply text, or
validate and declare the final file before selecting its sealed ID and digest.
Source delivery preserves the original requested format, limits and checks.

A created file path or head-only tool output cannot verify its full contents.
The problem is a handoff that loses the evidence needed for independent review.
The decision is explicit capture of the selected generation before completion.
Alternatives considered: rereading live files can substitute a later generation;
embedding every body in the initial review prompt increases context and clipping;
retaining tool previews leaves unseen sections unverifiable.
Project executors save a useful draft before repeated formatting or size
optimization, then inspect and incrementally validate the saved bytes. A
command that constructs content and prints its length has not saved a file.
Stable useful drafts may be declared without `delivery`, with unfinished
checks explicit; captures prove bytes rather than acceptance. Select useful
milestones within the generation, byte and review budgets instead of capturing
every edit: all declared generations remain independent-review sources.
Automatic extraction of command arguments would invent file evidence; waiting
only for polished output defers retention until the work may already be lost.
Executors use `evolution_artifact` after the final generation's last edit to declare
one file with `path`, or up to 16 unique files with `paths` in one call, for
independent review. Batch capture is sequential and independent per file, not
an atomic filesystem snapshot. It reports each complete acknowledgement or
bounded error and `complete=false` if any requested capture failed. Successful
sibling snapshots remain durable; retries of identical generations stay
idempotent. Cancellation and the existing execution deadline/token fences are
checked before each file reservation, part write and seal; an admitted batch
consumes one tool slot. An interruption after reservation remains explicit
unavailable content at terminal closure. No batch failure, valid digest or
successful capture establishes cross-file consistency, correctness, or completeness of unselected
files. This reduces declaration round trips without weakening selected-byte
retention or review coverage. Declarations persist complete original
bytes, relative path, capture time, encoding and SHA-256 before acknowledging.
Each generation binds the owner/run/task; redeclaring identical bytes is
idempotent and subsequent generations retain earlier ones. UTF-8 text is kept
exactly; binary bytes use base64 without implying semantic interpretability.

Capture opens every directory/file component without following symlinks and
refuses hardlinks, nonregular files, traversal, configured runtime storage and
named credential paths. File bodies, selected bytes, generation count and serialized
bundles have no application admission quota. Capture reads through EOF in 64 KiB
chunks; the before/after descriptor identity and size must still match. Selection
reserves the generation through the manifest CAS before immutable part writes. Interrupted reservations
remain visible; terminal closure records unavailable content explicitly rather
than losing known consumption or implying a complete snapshot. Storage outages
retain the existing terminal-persistence retry debt. Nothing scans the project
or follows arbitrary paths supplied as provenance.

New prepared executor configurations also pin the admitted project in their
evolution binding. Verified legacy prepared inputs retain their original binding
and digest after upgrade; they gain no declaration-tool exposure.

The existing terminal boundary closes declarations, seals a separate
`artifact_evidence` JSONL source and commits its identity in the owned result
capsule. Every entry retains its declared byte digest and content or an explicit
unavailable state. Original parts alone grant no progression. Cold materialization
checks the entire frozen bundle; it never reads later filesystem generations.

The independent reviewer receives the source identity, not the file body in its
initial context, and uses bounded `evolution_source` reads. Its observed successful
provider inputs must cover this source separately from terminal output and tool
evidence. Incomplete capture or missing coverage makes the objective unknown and
prevents positive evidence-axis assessments. Read coverage establishes access,
not correctness, novelty, demand, binary interpretation or net value.

Artifact reviewers can also list and reread short inline deliveries and retained
tool messages from the same frozen executor. These aliases bind the sealed
outcome and cannot authorize another task or a later filesystem generation.

Specialized review disables programmatic calling: direct source pages enter
provider inputs. A source-backed reviewer may submit the canonical JSON through
`evolution_review` as the sole tool call, or return a final JSON response. A helper
that reduces originals to summaries or sustains unrelated computation adds no
review authority and complicates complete coverage. Executors retain ordinary
computation tools; stored stage requests preserve their frozen configuration.
Source-backed review keeps original text pages during routine L1 passes, using
only thinking/image cleanup. Shared capacity-pressure compaction and mandatory
input admission still apply; retained text alone grants no read coverage or verdict.

Reviewer source responses expose bounded `review_read_progress` under the run
schema. It reports only acknowledged successful final provider inputs before
that response: delivered character counts, the first unread offset for each
frozen source, and whether to read missing pages or submit the independent review.
Pending, compressed and failed deliveries cannot advance this feedback. Complete
access grants no positive verdict; reviewers may reread evidence and conclude
failure or unknown. Executors receive no reviewer coverage. This uses the existing
receipt state rather than a second memory store or forced tool-choice policy.

**Problem:** reviewers can finish receiving originals but continue reading without
submitting a judgment. **Decision:** offer a direct structured submission capability
bound to the current owner, reviewer, immutable launch and frozen source identities.
Preparation validates the canonical review shape and frozen requirements/hypothesis;
it does not decide acceptance, write a verdict or grant source coverage. The existing
live final-reply port requires a successful sole-call receipt, verified provider
finish, active task authority and known, drained usage. Ordinary terminal checkpoints,
owned result CAS and run parsing retain all coverage and evidence-audit enforcement.
An honest fail or unknown can finish the review; technical completion alone cannot
verify real demand, novelty or net value. Preparation is ephemeral: after process loss
it cannot complete or authorize reviewer replay; existing retry policy owns fresh reviews.
After a committed terminal
result, existing cold recovery can settle the exact archived judgment once.
**Alternatives:** forced positive verdicts or relaxed coverage would fabricate
acceptance; a second review loop/store would duplicate lifecycle and budget authority.
The new capability does neither, and short inline reviews retain their no-tool path.

Run/program receipts and recent memory carry the same immutable reference.
Explicit semantic queries may select `source_kind=artifact_evidence`; the normal
owner source reader presents it separately and marks incomplete snapshots.
Declaration does not authorize execution, external writes or learning.

## Large-delivery review and exploration

A completed nonempty executor output may exceed the 24,000-character run
preview. The sealed original remains complete; `result_source` binds its task,
full digest and Unicode length in the run and program receipt. Frozen review
input retains at most 6,000 deliverable-preview characters and bounded tool
previews, plus exact source references. Raising inline record/context limits
or approving a clipped preview would lose the durability and evidence boundary.

Large reviews expose only the read-only `evolution_source` family. It lists at
most 20 sources and reads at most 6,000 Unicode characters; `limit` requests a
smaller page if transport compresses one. Current-task and owner fences apply.
A program executor can read older originals only through its settled sequence
and current program membership. Source reads invoke no model and write no store;
subsequent model requests and tool calls spend the existing shared allowance.

A producer read is not proof that the reviewer received its contents. The
registered `model_input_observer` compares exact tool-page digests in the final
sealed provider body. Only the final successful physical request confirms its
bounded receipt; rejected attempts, clipped inputs and stale reviewers cannot
confirm. The port supports Chat Completions, Responses, Anthropic tool blocks,
and Gemini function-response text projections. OpenAI tool content may use
cache-marked text parts; only a complete text-only concatenation can confirm
an exact page digest. Mixed or incomplete parts refuse coverage; unsupported
projections fail closed. Receipts retain no transcript, only source identity and Unicode ranges.
At most 64 disconnected ranges and 64 outstanding read identities are retained.
Adjacent reads merge; terminal archival freezes coverage with the review.

**Problem:** a source-backed reviewer can submit before receiving complete
originals; an unfinished inspection then becomes a failed review and can
trigger unnecessary executor repair. **Decision:** structured `evolution_review`
preparation checks acknowledged source delivery first. If pages are missing and
the shared evidence projection establishes no definite failing criterion,
submission refuses with exact missing-source progress. The same reviewer
continues through its existing model/tool loop, route and remaining budget.
The natively validated progress JSON reaches the tool's model-facing response
in full; the generic 400-character error preview applies only to other failures.
No terminal reply is bound, verdict written, new task born or executor replayed
by this refusal. A material negative judgment can still close without all
positive-acceptance sources; complete source delivery still permits unknown
judgments. Ordinary adjudication keeps source, evidence and verdict authority.
**Alternatives:** treating unread pages as an executor defect repeats otherwise
valid work; silently accepting incomplete inspection weakens evidence; a new
review loop or retry allowance duplicates the existing task/budget lifecycle.

Deliverable and tool-evidence coverage are independent. A review missing any
required original range cannot claim full acceptance: the objective
criterion becomes `unknown`. Complete transport coverage is only an access
condition, never proof of correctness, demand, novelty or measured consumer
value. The existing independent requirement and tool-evidence checks still
apply. A fresh review after confirmed process loss must read its own frozen
source; executor work is never replayed. Runtime usage escrow and in-flight
side-effect resolution remain separate open continuity work.

## Retained tool evidence

`tool_evidence.py` streams terminal-retained tool messages as canonical JSONL,
preserving their original message indices, full call IDs and JSON contents.
Assistant claims are excluded. Each source has independent part keys, length,
digest and review coverage. Both sources must verify before an archive seals;
missing tool parts preserve known costs and prevent successful recovery.

A clipped or omitted inline tool preview creates a frozen `tool_evidence_source`.
The reviewer must receive every character through successful provider inputs
before it can accept. A deliverable read cannot satisfy this requirement.
The owner inspector can explicitly switch between deliverable and saved tools,
retaining one page and making source scope visible. Legacy archives remain
readable without inventing missing tool messages.

The scope is `terminal_retained_tool_messages`: earlier compaction, upstream
stdout truncation and external artifacts are not recovered. Even full transport
coverage cannot prove a complete experiment history or a correct interpretation.
Missing evidence leaves the corresponding substantive criterion unknown.

## Lossless review representation

When format recovery is enabled, runs may represent one complete archived
review JSON object without another model request. The original full response,
including any prose/fence, remains immutable. A normalization receipt names its
source digest, exact JSON range and field paths whose overlong strings are
represented by source pointers. IDs and outcomes stay unchanged. Duplicate keys,
multiple JSON values, incomplete objects, trailing prose, invalid schema fields
and missing/duplicate audit IDs cannot become an accepted normalized verdict.
Negative and unknown judgments remain negative/unknown; normalization supplies
no read coverage or evidence of correctness. Existing original-source paging
lets the owner inspect every referenced character. A disabled/legacy policy or
unavailable/mismatched source retains the format-refusal path.

## Reading and resource lifecycle

`GET /api/v4/evolution/runs/<run_id>/outcomes/<task_id>` pages owner-scoped
output with an optional Unicode-character `offset`. `source_kind=tool_evidence`
selects the saved tool-message JSONL; omitted kind reads the deliverable. Pages return the original
source digest, total length, next offset, archive state, and consumption
completeness. Foreign and missing resources have the same refusal. Reads never
invoke models, seal archives, settle episodes, or launch successors.

The detailed studio inspector reads originals only after the owner clicks
**Read original** on an archived execution or review. Canonical terminal verdicts
are exposed on source pages and stage receipts; a bare task status never turns
truncated or interrupted delivery into completed work. Delivery completion does
not imply acceptance: the independent review remains the verdict authority.
The reader shows restoration provenance and original-consumption lower bounds,
and states explicitly that reading does not invoke a model or rerun the task.

The inspector retains one source page and a keyed window of 20 execution records;
all records and original characters remain reachable through paging. The selected
source, text selection, scroll, focus, and fingerprint disclosure survive snapshot
refreshes. A run change, view closure, hiding, or disposal cancels outstanding
reads; request epochs fence responses even if cancellation is ignored. Source
identity, digest, range, and frozen consumption must agree between pages.
Failure retains the last verified page and retries the requested offset.
Switching source kind clears the old page; both kinds must agree on frozen facts. Legacy
records keep their execution links without inventing original availability.

The run header retains receipt flags rather than full output. Parts hold at
most 30,000 Unicode characters; source pages hold at most 6,000. Verification
reads one part at a time and retains at most 24,001 characters for executor
handoff. Large deliveries retain a bounded preview and an immutable source
reference; review requires coverage of the complete source.
Review JSON is reconstructed completely before its existing schema validation.
These are durable user artifacts, not an evictable cache. Growth follows actual
terminal output, retained tool JSONL, one manifest and exact review input per task. No additional
worker, timer, resident index, or process is introduced.

A controlled in-memory storage measurement with 200,000 Unicode characters
produced seven output parts and one manifest, approximately 491 KiB of serialized
records. Reconciliation retained 24,001 executor characters; traced peak was
approximately 319 KiB for verification and 151 KiB for 6,000-character pages.
The 100-read median was 3.78 ms with tracing enabled. This is a local allocation
check, not production storage throughput or evidence of model quality.

A controlled 200,000-character retained tool message with Unicode and escaping
produced 300,058 JSONL characters in 11 parts. Streaming serialization peaked
at 530 KiB and took 2.40 ms locally. The already resident task transcript and
storage/provider work are excluded; this is an allocation check only.

A controlled 200,000-character sealed provider input used 34 read identities:
pending metadata retained 8.4 KiB and the prepared receipt another 8.4 KiB;
decoding peaked at 1.78 MB. Across 100 preparations, median time with tracing
was 2.13 ms. Confirmation merged to one complete range and released pending
identities. This measures local allocation, not provider quality or throughput.

The inspector allocation check uses 1,000 synthetic stage records and 100
changed snapshots in JSDOM. It kept 20 record rows, one 6,000-character source
page, and 178 DOM elements; the original was read once. Median projection time
was 2.83 ms and p99 was 10.11 ms. The synchronous-update measurement retained
approximately 1.08 MiB more heap after GC, including JSDOM and warm-up state;
it does not prove a browser memory plateau or production throughput. Teardown
left no host nodes. Real browser journeys independently check source identity,
selection, focus, safe text and narrow-screen layout.

## Alternatives

- Persist only the run header: the loss window precedes that CAS, and long
  output competes with receipt capacity.
- Replay every missing executor: unconfirmed external effects can repeat.
- Rebuild review input from the result: original tool evidence can disappear.
- Move execution into another engine: does not resolve ambiguous side effects
  and introduces a competing lifecycle authority.

## Verification

`tests/evolution/test_evolution_stage_outcomes.py` injects loss before receipt
commit and before archive sealing, checks incomplete/corrupt sources, frozen
tool evidence, cancellation, partial usage, Unicode paging, owner fences, and
unverified terminal deliveries. API tests verify read-only source access and
refusal parity. `test_evolution_tool_evidence.py` guards distinct source identity,
coverage, lossless JSONL, missing parts and legacy recovery. Neighboring stage-launch, review-recovery, program, research,
and shared model-budget tests guard their existing authorities.

Frontend owning checks: `frontend/src/features/evolution/testing/outcome-reader.test.cjs`
cover immutable source continuity, read cancellation, safe text, focus, legacy
records, and history pagination. The real-server program browser journey reads
full originals past the inline limit at narrow and desktop widths, retains a
selected page across a changed snapshot, and reloads restored receipts. Only
terminal model output is synthetic; original GETs invoke no model.

Active task streams wait asynchronously; cold storage reads use the existing
serving-loop executor. The shared SSE wrapper also iterates synchronous producers
on that executor, preserving other requests while a producer waits. It adds no
pool or timer. Generic synchronous producers must still bound their own waits;
disconnect cannot forcibly interrupt an already-running synchronous call.

## Frozen quantitative validation

Problem: a successful command plus a model verdict can accept a numerical claim
that contradicts the reported measurements. Decision: generic runs and program
steps may precommit `scientific_protocol` through the canonical Research schema;
its numeric rule, named dataset/metric/baseline and at least two distinct seeds
are frozen before launch. This implies the command-execution policy. An optional
`scientific_required_outcome` fixes supported/refuted/inconclusive acceptance;
a valid measurement missing that target cannot be overridden by a review.
Idempotency refuses a changed protocol or target. Research discovery rejects
these unsupported inputs instead of ignoring them.

Terminal capture recomputes reported paired statistics, seed coverage, partition
separation, budget agreement and conclusion consistency from retained correlated
command outputs. A final answer or read-only retrieval cannot supply numbers.
The header holds only frozen policy and a fixed-size audit aggregate; complete
outputs stay in the existing source parts. Invalid/missing measurements or
contradictory packets refuse review without allocating another reviewer. All
scientific reviews require the original tool source to reach the review model.
This checks reported consistency, not data authenticity, complete historical
attempt coverage, formal significance, or objective completion.

Pending owned background measurements use the existing durable wait even when
an unrelated foreground command already succeeded. The companion join combines
its late audit with the immutable original audit, never rewriting the original.
`background_scientific_audit` is a read projection excluded from the parent's
binding digest. Original deadlines, cancellation, accounting and one-review
admission remain unchanged; neither executor replay nor a new timer is added.

Alternatives: accepting model-authored verification flags loses computable
checks; selecting only favorable packets hides counterevidence; mutating a
sealed original breaks replay and source identities. Rule-free legacy runs and
default frontiers preserve their existing policy. Automatically freezing a
protocol for the default frontier remains separate work; this boundary enables
explicit structured protocols throughout generic execution first. Canonical
Research schema loading is lazy and local, with no remote schema fetch.

Verification: `test_evolution_scientific_observations.py`, terminal checkpoint,
stage-outcome, background-wait, command-evidence and source-tool suites. This
adds bounded policy/audit fields within existing record limits, no raw-output
copy in the header and no additional resident pool, timer or cache namespace.

Operations measurement: one synthetic paired-observation packet audited 1,000
times with allocation tracing averaged 1.161 ms and peaked at 88,638 traced
Python bytes. Its optional terminal policy/audit fields serialized to 663 UTF-8
bytes, excluding the separately retained review prompt and existing output
parts. This is consistency-check overhead, not experiment throughput.

## Automatic preregistration for the default frontier

Problem: a numerical audit can only honor criteria that were frozen before the
validation executor. Decision: new default synthesis stages must preregister
future validation through the owner-bound `evolution_explore` tool. Its
`validation_context` lists target identities; `preregister` validates the shared
scientific protocol and required outcome, then writes only the unlaunched target
under program CAS. Same-value retries are read-only; changed rules are refused.
Original objectives/checks remain unchanged and registration launches no worker.

A synthesis run carries its immutable program/target binding. Terminal capture
checks the actual future queue, rather than a model-authored statement. Missing
registration blocks review and is a confirmed missing-plan failure eligible for
ordinary controlled diagnosis. This grants no experiment verification or
learning authority. Diagnosis retains the original failed step and final audit;
it does not replay ambiguous external effects. Valid registration is passed
unchanged into the future run's numerical and original-source checks.

Alternatives: trusting "preregistered" in prose creates false authority; freezing
inside the validation executor permits post-observation tuning; silent defaults
invent a criterion the evidence may not support. Missing choices require a
repaired grounded proposal. Stored legacy frontiers without the new marker keep
their policy. Verification: `test_evolution_preregistration.py`, program,
exploration-tool, replanning, numerical and terminal-checkpoint suites. No new
store namespace, timer, resident pool or independent model loop is added.


### Bounded durable task birth under transient storage pressure

Problem: Evolution dispatch requires a confirmed pending task row, but the ordinary
500ms diagnostic checkpoint budget also covered its birth. A short storage queue
could therefore refuse a safe launch before any executor was submitted.
Decision: Evolution explicitly grants birth and its confirming owner-scoped read
one shared admission budget, at most five seconds and clamped to the frozen stage
deadline. Parent/witness reads, CAS writes, conflict refreshes and transient retries
consume that same monotonic budget; each operation receives only its remainder.
Retries are bounded to five per operation with short bounded backoff. Ordinary
chat births and running diagnostics retain the 500ms best-effort policy. Owner,
parent, terminal and cancellation fences remain authoritative; exhaustion rejects
the unstarted carrier without paid dispatch. No new worker, queue or durable store.
Alternatives: extending every progress write would delay ordinary task startup;
launching without a durable birth would break restart and cancellation authority.
Measurement: injected 600ms witness delay plus one transient timeout recovers in
1.25 simulated seconds (including 50ms backoff), leaving 3.75 seconds for the write.
Tests verify decreasing RPC deadlines, preserved abort tombstones and one dispatch;
a birth exceeding the five-second shared budget receives no confirming read or
executor submission. These are storage policy measurements, not research outcomes.


Background launch closure: a producer reply must match the registered handle,
explicitly be completed and retain a string result before capture removes the
launch from the pending join. Missing, foreign, unknown or malformed replies stay
unresolved. The existing join reconciliation distinguishes registry recovery from
definitive unavailability; neither admits an executor replay or premature review.
Problem: dropping an unresolved launch during initial capture could bypass the
join. Decision: preserve the launch identity until producer closure. Alternatives:
absence-as-completion and repairing malformed results would invent evidence.
Verification: background execution and waiting tests cover initial missing handles
and malformed producer replies. No additional query, polling loop or store.


Background producer provenance also binds the original execution task. Ordinary
background tools remain user-scoped; Evolution opts into an additional task-ID
restriction for capture, joined reads and cancellation. Registry entries retain
only the launch task ID after freeing completed task resources, and native command
reattachment reads that ID from its existing durable launch record. Legacy completed
entries without a provable task identity cannot supply current-stage evidence.
Problem: user ownership alone could accept a different experiment's result or
cancel its work. Decision: verify producer task provenance at the registry boundary.
Alternatives: trusting a copied tool display or retaining the entire task object
would weaken authority or prolong resource ownership. The comparison introduces
no extra query, worker or timer; a typical 32-character Python task-ID string uses
73 bytes on the measured Python 3.12 runtime (excluding dictionary entry overhead),
within the existing resource-sized registry lifecycle. Background tests
cover same-user/different-task completion, cancellation and release of task objects.

## Scientific command originals before model projection

Problem: numerical reports can be hidden by result budgets or removed by model
context compaction before terminal review. Decision: scientific executor stages
bind an optional result-capture port. The ordinary settlement pipeline calls it
after post-tool hooks and before model projection. Framework-owned command
children are visited; spilled originals use the existing owner-bound canonical
artifact reader. Model-returned program values never create a command original.

Acknowledged originals live in `scientific_commands.py`'s owner/task/birth-bound
immutable namespace, using 30,000-character parts and a full UTF-8 digest.
All parts must acknowledge before the manifest. A local close fence freezes the
capture scope for terminal serialization; in-flight or failed captures remain
unverified. Terminal tool evidence includes these originals, with an explicit
scope and incomplete preview. Numerical audit prefers captured originals over
model views for the same call, preventing duplicate measurement counts.

Execution observation remains distinct from successful source retention. Capture
failure preserves the paid handler return, reports unavailable evidence in the
numeric audit, and cannot create a verified scientific outcome. Missing/changed
parts or foreign bindings refuse; inspection never replays a command. This
preserves reported measurements, not benchmark authenticity, independent
replication or external-work quiescence. Upstream producer clipping cannot be
repaired by a downstream capture port.

Alternatives: trust compacted summaries, retrieve temporary artifacts only at
termination, or retain complete results in task headers. These lose numbers,
risk expired originals, or grow resident headers with task lifetime. The capture
port holds only binding/counters; originals are durable and streamed into the
existing terminal archive. No worker, timer, model loop or per-command run-header
entry is added. Persistent growth is one lossless journal copy (compressed when smaller) plus the
existing terminal JSONL copy and framing metadata per scientific-looking output;
content-addressed retries do not create extra copies. Non-scientific stages bind
no port and perform no added source writes. Native daemon verification captured
a synthetic 4,001,425-character / 10,001,425-byte original in 134 parts, preserving
every Unicode character and refusing foreign-owner access. Capture took 3,186 ms
with tracemalloc enabled and 16,026,499 additional traced peak bytes beyond the
already supplied source. This is capture/transport cost, not model or experiment
performance; the proof made no model calls and confirmed idempotent retry.

Captured scientific output is an attempted report even when its JSON is broken,
log-prefixed, transport-failed or missing a measurable packet. Such originals
increment the audit's unverified count instead of disappearing behind another
favorable report. Ordinary unrelated setup outputs remain outside this capture
scope. `capture_policy_version=1` distinguishes this complete-output accounting
from legacy aggregates; missing policy provenance cannot grant current validity,
and merging a new audit never upgrades a legacy one. Existing archives stay
readable and immutable; current-policy conclusions require re-auditing originals.
Problem: parse refusal previously erased captured attempted reports. Alternatives:
skip unreadable outputs or promote the favorable subset; both hide missing proof.
The change adds one scalar per aggregate and no source duplication or worker.
Scientific stages always join declared owned background commands before review,
even when an earlier foreground report already passes. Producer-confirmed launch
call identities are excluded from report accounting; their completed originals
supply the measurements. A favorable foreground result cannot bypass a pending
or contradictory late result. Original foreground receipts remain immutable.

Four-axis hypothesis assessment includes background originals in its existing
full-read condition. Problem: late evidence was omitted from the positive-axis
check. Decision: an unread/partially delivered background source changes positive
axes to unknown; negative/unknown judgments remain intact. Full model-observed
original coverage removes this refusal but does not itself prove novelty or
value. Alternatives: trust a late-result preview or the aggregate criterion
verdict; neither establishes that the reviewer inspected the decisive original.
The shared source-coverage receipts remain the sole authority, with no new store.

Background scientific attempt completeness
-------------------------------------------

Problem: a successful foreground measurement could hide a late background
packet that carried scientific fields but could not be parsed. Decision:
owned background originals containing scientific field markers count as
attempts even when malformed, log-prefixed, or transport-failed; numerical
acceptance then remains unverified. Ordinary setup output adds no measurement.
Skipping unreadable packets was rejected because it selects favorable results.
This checks reported completeness, not execution authenticity or replication.

Scientific summary admission
----------------------------

Problem: incomplete summaries could either raise during adjudication or classify
fewer attempts than the claimed total. Decision: the shared validity boundary
checks the canonical audit schema and requires all observations to be classified,
with zero failures and a single consistent outcome. Review and negative-result
diagnosis share this boundary. Trusting version flags alone was rejected; schema
validation alone cannot establish the cross-field count equation. Malformed
summaries remain unverified and never acquire continuation authority.

Scientific source read cost
---------------------------

Problem: each original part previously required a storage round trip. Decision:
read owner/observation-scoped pages of eight parts, shrink resource-refused pages
without moving their cursor, and restore numeric ordinal order before full hash
verification. Missing, extra, foreign or changed parts refuse. No cache is added.
On private native TofuDB, a 10,001,425-byte Unicode original (134 parts) required
135 queries / 243 ms / 32.06 MB traced peak before, versus 18 queries / 167 ms /
32.04 MB after, in single controlled reads with tracing enabled and no model
calls. These are local measurements, not a universal latency claim. Pages of
32 were rejected: six queries reduced latency but raised traced peak to 48.12 MB.

Scientific journal encoding
---------------------------

Problem: retaining complete command originals also duplicates terminal evidence
on disk. Decision: each journal frame may declare `zlib-base64` encoding, only
when its encoded character count is smaller than the original. Missing encoding
means raw UTF-8 text. Identity and digest still bind decoded originals. Decoding
is bounded to 120,000 UTF-8 bytes and 30,000 characters per frame; invalid streams,
trailing data and expansion beyond these bounds refuse. Equivalent old raw frames
remain immutable on retry. Older readers reject the new encoding through their
schema rather than treating it as source text. The terminal archive stays plain.
Deleting original frames was rejected pending a proven archive/recovery lifecycle.
On private native TofuDB, the same highly repetitive 10,001,425-byte Unicode
fixture occupied 43,480 journal content bytes across 134 encoded frames; framing
and the terminal archive are additional. Capture measured 1,882 ms versus the
prior 3,094 ms; read measured 150 ms versus 167 ms, with traced read peak remaining
32.04 MB. These single traced local samples prove round-trip and workload-specific
space savings, not general compression ratio or latency. Incompressible content
keeps raw encoding; no durable user evidence is removed.

Controlled long-horizon verification
-----------------------------------

Problem: isolated boundary tests do not establish repeated repair stability or
original readback after a storage process crash. Decision: retain the private
native benchmark `benchmarks/evolution_long_horizon.py`; it uses real run/episode,
source coverage and archive authorities with controlled execution/provider seams.
It requires checkout test dependencies and a source-matching `TOFUDB_BIN`; run
`TOFUDB_TRANSPORT=daemon python benchmarks/evolution_long_horizon.py` in that
environment. All state is temporary; it never opens the live application store.
The default 48 revisions preserve objective/protocol, receive complete Unicode
originals, assess then repair, and finally verify every archive. After revisions
16 and 40 it kills its private native process, confirms a negative exit status,
restarts and demands exact run readback. Coverage validation rejects missing
crashes, receipts, originals or measurements; a requested restart is no evidence.
The controlled run passed with 96 receipts, 48 exact originals and two SIGKILL
recoveries. The run header grew from 3881 to 23062 bytes, roughly 408 bytes per
revision in the original full projection. Durable receipt headers are now bounded;
full runtime/public hydration and other historical fields still need lifecycle work. This does
not test agent-process recovery, executor replay, real model quality or independent
scientific replication. Counting unit passes as long-horizon evidence was rejected.

Run receipt publication and storage bounds
-----------------------------------------

Problem: every repair rewrote its complete stage history into a record capped at
1 MiB. Decision: `run_receipts.py` acknowledges immutable 32-row pages before the
run CAS publishes an owner/run-bound hash pointer; the durable run keeps at most
64 recent receipts. The linked page hashes, prefix digest and exact descending
counts bind the complete original order. Failed acknowledgements or stale CAS
cannot publish candidates. Missing, changed or foreign pages refuse recovery;
archived rows cannot be edited on resave. Unreferenced candidates grant no
authority and are not deleted by this change. Public/detail/list projections
currently restore complete history so budgeting, source authorization, episode
recovery and existing callers retain their semantics. This is a durability
boundary, not a claim of bounded runtime memory or faster reads: hydration still
reads historical pages and other growing run fields remain future work. The
9,000-receipt test exceeds the prior single-record byte limit and commits through
bounded pages with exact readback. The native 48-revision benchmark additionally
checks bounded stored receipt counts, exact original sources and recovery after
a private process crash following archive publication. Truncating old receipts
or merely raising the record cap was rejected: these respectively lose authority
or postpone the same lifetime limit.
The final local native run stored 16,731 header bytes at revision 48 versus
23,259 bytes for its full public projection. Its last-eight-revision median was
969.8 ms, versus approximately 573 ms in the prior non-archived local run;
these unpaired samples expose hydration cost, not a speedup. Bounded durable
publication precedes the still-required lazy runtime read migration.

Workspace ownership read cost
-----------------------------

Problem: task authorization already loads complete run history, but coordination
snapshot ownership checks repeated that projection on every poll and source read.
Decision: workspace snapshots assert only the owner-prefixed durable run record's
existence and exact run identity, then read its workspace. The existence helper
returns no partial run or history authority. Task authorization, stage recovery,
consumption and full original inspection keep their complete-history boundary;
a missing archive still denies authorization even if its workspace is readable.
Missing or mismatched owner/run records refuse. No cache or new worker is added.
The fake owner fixture now seeds the declared durable identity instead of only
mocking detail projection. On private native TofuDB with 9,000 synthetic receipts,
identical workspace state required 282 reads / 1,206 ms before versus two reads /
0.89 ms after in a single controlled comparison with no model calls. This removes
redundant history projection, not all runtime hydration costs or recovery gates.
Skipping ownership entirely or returning a partial run from the complete detail
API was rejected because either weakens scope or silently changes consumers.

Complete receipt reconstruction memory
--------------------------------------

Problem: full detail reconstruction retained page containers, a flattened list,
and a second concatenated list. Decision: collect reverse-order rows once,
reverse in place, validate every prefix commitment while hashing forward, then
append recent receipts. Each page still checks owner/run identity, content hash
and exact descending counts. No allocation is sized from an untrusted count.
A valid outer hash with an inconsistent earlier prefix now refuses as well.
On private native TofuDB with 9,000 synthetic receipts, exact full readback used
281 queries in both implementations; traced peak fell from 5,107,734 to
5,001,242 bytes (about 2.1%). Single traced elapsed samples were 4,740 versus
3,995 ms; they do not establish universal latency. Returning less history or
trusting only the final prefix was rejected. Full projections remain linear in
history length; this reduces temporary copying and strengthens commitments,
not the still-required lazy runtime/HTTP paging migration.

Repair source continuity
------------------------

Problem: repair prompts named the prior deliverable source but omitted frozen
tool observations, artifact generations and late background originals. Decision:
include every existing source identity in the frozen executor prompt, preserving
its run/task, digest, kind, size and completeness flag. Repairs inspect originals
relevant to cited review gaps before repeating costly work; old measurements
remain reference evidence, not fresh validation of a changed generation. Missing
sources are not fabricated. Source reads still require the current executor and
exact owner-bound archived identity; prompt references grant no new authority.
A controlled late-background failure test verifies that the next repair prompt
carries the exact original and can read its falsifier through the normal source
tool. Reliance on review prose or re-running blindly was rejected because it
loses inspectable failure context and repeats work without a diagnosis.

Background original discovery
-----------------------------

Problem: original-source listing selected an artifact-count field for background
evidence, hiding complete late command originals from reviewers and repairs.
Decision: tool and background JSONL streams use their canonical message count;
artifact snapshots continue to use artifact count. Listing preserves exact
source identity and owner/task authority. Discovering a source does not satisfy
provider-input inspection coverage. A real controlled background join test
checks both current reviewer discovery and subsequent repair discovery/readback.
Relying only on direct prompt references was rejected: source inventory must
truthfully expose each retained original even when the reader lacks its digest.

Requested original page limits
------------------------------

Problem: small source-tool retries read a full 6,000-character window and clipped
after storage access, sometimes fetching an unnecessary adjacent part. Decision:
forward the canonical requested limit to the original-page reader before choosing
parts. The default and maximum stay 6,000 Unicode characters; invalid limits
refuse before I/O. Exact offsets, next offsets, source identity, current-task
authority and successful-provider-input coverage remain unchanged. Boundary
tests distinguish a one-character read at offset 29,999 (one part) from a
37-character read across the same boundary (two parts). Requesting or returning
a smaller page still grants no inspection coverage by itself. Post-read clipping
was rejected because it saves output bytes but not original-part reads.
A private native TofuDB comparison over an 80,000-character Unicode original
confirmed identical returned page metadata/content at offset 29,999 with one
requested character: original-part reads fell from two to one, with no model
calls. This is a boundary-specific I/O reduction, not a general latency claim.

Precommitted candidate/control versions
---------------------------------------

Evolution reuses the Research generation scope in `RESEARCH_CYCLE.md`: paired
`candidate_digest`/`baseline_digest` optionally bind the two reported versions.
Default synthesis asks to freeze inspected source/configuration digests when
established, never fabricate missing facts. Shared normalization retains them
in the immutable protocol and protocol digest; mismatched or missing report
identities block review admission. Original objective and numeric target remain
unchanged. Trusting names or silently promoting old protocols into version-bound
validation was rejected. This is consistency evidence, not benchmark authenticity.

Complete terminal context — Problem: a fixed 1 MiB checkpoint guard and
200,000-character frozen-prompt schema could block owned result persistence and
subsequent review restoration. Decision: retain the complete schema-valid
terminal capsule and frozen launch prompt through ordinary task-result CAS.
Exact owner/task/dispatch/content-epoch/status/verdict and source digests remain
required before materialization. Preparation still acknowledges immutable parts
before result commit; uncommitted parts grant no outcome authority. Real
serialization/storage failures retain retry debt and cannot fake settlement.
Alternatives: larger quotas retain refusal; clipping frozen input changes the
review; dropping binding/digest checks grants stale writers authority. Tests
commit a large Unicode capsule, simulate a lost post-commit callback, remove
the transient carrier and restore the original prompt without executor replay
or input regeneration. Whole-capsule storage remains atomic and scales with
its actual complete metadata; transcript/tool originals retain their archives.
The shared terminal-delivery preparer likewise has no opaque-payload byte
quota. It still requires an object, serializable finite JSON, the committed
task/owner identity and an unchanged terminal content epoch/digest. Prepared
values are frozen once and returned as isolated copies; preparation failure
remains retryable without granting delivery acknowledgement.
Measurement: a complete 1,741,325-byte UTF-8 terminal capsule committed and
restored its original large review input after a simulated lost callback in
2.7233 s under tracing, with 29,926,180 peak traced bytes. This includes owned
task-result persistence/recovery with controlled model submission; it does not
measure real provider work or an actual process crash.
The large terminal capsule commits and restores on both isolated SQLite and
TofuDB authorities. Native task ownership remains enforced: the fixture starts
its authority with the same explicit user subject as the tasks. Artifact identity
and copied external compile inputs are verified; live server configuration is
unchanged. Size relaxation grants no foreign-owner or stale-writer authority.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
