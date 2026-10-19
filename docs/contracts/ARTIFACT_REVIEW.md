# Artifact and ordinary Turn owner review

Wire authority: `contracts/artifact_review_v1.schema.json`. Entry points are
`lib/artifacts/review.py` and authenticated GET/POST
`/api/v4/artifacts/{artifact_id}/review`. The existing artifact panel mounts
`frontend/src/rich-response/artifact-review.ts`; no second execution loop or
task status owner exists.

## Problem and decision

An immutable output and a completed task do not establish that a user's
requirements were met. Store an explicit owner assessment against an exact
artifact ID, content SHA-256 and criterion generation. The UI requires a note
describing the evidence for each pass/fail decision. `basis=owner_attestation`
is deliberate: neither the model's assertion nor proof-bundle integrity is an
independent correctness test. Task completion and review remain separate.

Alternatives considered: reusing Git workspace acceptance would exclude
non-code deliverables; extending read-only proof bundles would confuse evidence
integrity with correctness; a second task dashboard would duplicate Turn and
artifact presentation. This feature reuses immutable artifact publication,
existing version selection, common authentication and selected-store record CAS.

## Requirements, versions and invalidation

The oldest accessible parent in one owned conversation identifies a lineage.
The lineage has one current objective and ordered criteria, each with a stable
ID and a kind (`requirement` or global `constraint`). A version has its own
latest decision for each criterion; opening an old version cannot accept a new
one. New versions inherit requirements and start unreviewed.

Editing a requirement invalidates only that requirement's judgment. Editing the
objective invalidates all judgments. Adding, removing or changing a constraint
invalidates requirement judgments; other unchanged constraints stay valid.
Reordering items has no semantic effect. Generations change on wording/kind
changes and on removal/readdition, so restoring old wording never resurrects an
old pass. Stale evidence stays visible. Removed criteria's stored decisions are
retained but do not contribute to the current result.

No criteria means `unconfigured`; a current fail means `changes_requested`;
all current criteria passing means `accepted`; otherwise the result is `pending`.
This is a review of the selected artifact only, not whole-task acceptance or
acceptance of every file in a project. The current requirements and latest
per-criterion decisions are editable aggregates, not an append-only review
history. No claim of automated correctness verification is made.

## Supporting working facts

Problem: a recorded pass could outlive a corrected input or changed source.
Decision: a decision may explicitly bind up to eight working facts from the
artifact's owner/tenant/conversation. The command submits exact IDs/revisions;
the server admits only current observations or owner-assessed facts and stores
their text and status at review. Derived claims awaiting review cannot serve as
current evidence. Empty bindings deliberately retain the manual-note workflow.
Replacing a previously bound decision must explicitly supply bindings, including
an empty list when the owner chooses to remove them.

Every read rechecks just the selected facts through the shared dependency graph.
A correction, withdrawal, changed transitive source, unavailable source or
pending source review makes only dependent judgments stale. The original text
and note remain visible alongside bound/current revisions and source status.
An unchanged fact revision can still have a changed physical source. Tool-result
facts retain their historical-observation semantics: unchanged bindings do not
establish current external truth or independent correctness.

Requirements are a separate read port (`read_criteria`) of the same review
owner. Facts quoting criteria resolve their generation without evaluating owner
decisions, avoiding a review → fact → review cycle. Task impact and review use
the same dependency assessment. Rejected alternatives: invalidating all reviews
would discard independent judgments; checking only fact revisions would miss
file changes; following complete review views from criterion sources would
recurse. Ordinary Turn acceptance uses the scope rules below; automated reruns remain separate work.

## Ownership, concurrency and recovery

Every read first resolves the live artifact and every parent through the
explicit authenticated owner. A missing, deleted or inaccessible ancestor
fails closed. Cross-conversation parents and cycles are rejected. The owner
and tenant are part of the hashed record key and are checked in record values.
HTTP requires the existing `chat` scope; this endpoint is not a model tool or
an authorization inherited by synthetic users.

`artifact_review_v1` records use the selected backend's `record.get/put`.
Requirements and per-version decisions have separate monotonic revisions.
Commands must present the requirements revision; decisions must also present
the decision revision and both evidence digests. Stale commands return 409.
The UI preserves the unsaved draft, explains the conflict and offers reload.
It never silently overwrites newer evidence or retries a write on its own.

If the store loses an acknowledgement, exact committed record digest and
revision readback can establish success. Otherwise the error propagates. A
requirements change racing a decision leaves that decision bound to the old
generation: later reads show it as stale. There is no cross-record transaction
claim, nor a guarantee that an open tab stays current without reloading.

Fact admission and review CAS are not one transaction. A source change racing
the write remains bound to the old revision and the fresh returned/read view
marks it stale. Storage failures propagate instead of synthesizing a current
pass. Missing or foreign facts never enter a decision. Synthetic/system
principals cannot write owner attestations.

## Resource lifecycle and verification

Review queries start only when the user opens the review section. A mount owns
one request controller and its drafts; close, version switch and library switch
abort/fence replies and release the mount. No timers, polling, resident workers,
external calls, task injections or preview-content reads are added.

The fact picker loads the conversation's bounded fact list only on explicit
request. Refreshing options does not replace an already selected revision.
Conflicts preserve notes and selected versions; changing artifact/version or
closing the panel aborts and fences picker replies. Removed criteria release
their draft state. Controls use the existing UI factories and locale owner.
The native snapshot must not suppress artifact loading without evidence of
absence; its count-based optional hint follows `CONVERSATION_SYNC_V3.md`.
Background snapshots retain only their optional artifact hint in a shell-scoped
WeakMap. The existing Surface's `onConversationMounted` callback starts metadata
hydration when a warm conversation becomes visible; later active snapshots
refresh through the same reader. A preloaded snapshot must not lose its artifact
list by publishing before the Surface exists. There is no inactive metadata
request, polling, second presentation store or additional request per streamed
paint. Cold mounts wait for their snapshot; warm empty hints skip list requests.

Each query performs ancestor metadata reads plus two record reads. A decision
or requirements mutation uses one CAS and rereads the view. Cost scales with
lineage depth and current criteria, not total conversation Turns. Schema limits
bound one request to 1 MiB, current criteria to 256 and one durable record to
8 MiB. Reaching a bound refuses the edit; existing owner evidence is preserved.
Records follow durable artifact ownership and survive restart; there is no
background reclamation of review decisions.

A local warm metadata-only probe (25 reads per case) measured p95 of 1.74 ms
on SQLite / 1.07 ms on native TofuDB for one version and criterion. With 32
versions and 64 criteria, p95 was 27.06 / 15.62 ms and the response was 12,620
bytes. The latter made 32 artifact, 32 owner-conversation and two record reads;
ancestor traversal remains linear and is not an indexed constant-time claim.

Bound reviews add one fact-record read per view and resolve each selected fact
once, sharing graph/source caches within that query. Unbound reviews add zero
fact reads. A local ten-read probe with 16 criteria, 32 facts and one common
selected fact measured medians 7.48 ms (SQLite) / 5.84 ms (native), 13,175 response
bytes, one fact-record read and one source resolution; 31 unrelated facts were
not resolved. These are local view costs, not task correctness or token savings.

Run `tests/artifacts/test_artifact_review.py` against SQLite/native storage,
neighboring artifact ownership/version/publication tests, the generated DTO
check (`scripts/gen_artifact_review_contract.py --check`), frontend unit tests
and `tests/visual/test_visual_artifact_review.py` on the real application.
The browser test covers owner decisions, version navigation, targeted stale
judgments and an actual HTTP CAS conflict at desktop/mobile widths.
Dependency cases live in `tests/artifacts/test_artifact_review_dependencies.py`
and `tests/visual/test_visual_artifact_review_dependencies.py`: both stores,
restart then physical file change, transitive corrections, racing CAS, source
failure, ownership/tenant fences, recursion avoidance and English/Chinese UI.

## Ordinary Turn acceptance

Problem: accepting one artifact cannot establish that the answer and all
intended deliverables have been checked. Decision: an authenticated owner may
configure a main-lane assistant Turn through GET/POST
`/api/v4/conversations/{conversation_id}/turns/{turn_id}/review`. The additive
`Turn*` definitions live in the same wire schema. `lib/acceptance/review.py`
owns the shared requirement generations, evidence admission/projection and CAS;
`turns.py` supplies Turn/output identity. The artifact adapter preserves its
existing keys and write identities. There is no second execution state machine.

The owner declares artifact output slots, each with a stable ID, label and an
owned artifact from the same conversation. The answer is always present as the
reserved `answer` slot. Membership is an explicit declaration of what is being
reviewed, not proof of which task produced an artifact or that every produced
file has been included. The backend never trusts browser attachment heuristics.
The ordinary assistant Turn action opens the shared review presenter in a
UI-kit dialog while pending, running or settled. The owner explicitly selects
output versions, sees coverage, and can open previewable current and historical files.
Navigation, target removal and shell disposal retire the dialog and abort reads.
Current and historical output actions open the exact server-resolved artifact
through the browser window with `noopener`. An undeclared runtime alias broke
this action despite passing presenter tests; the actual-application delivery
correction test opens both versions and checks their contents before deciding.
Problem: Markdown returned an unsupported-format response after a successful tab
launch. Decision: share the inert preview/PDF body renderer. Alternative rejected:
a second viewer would duplicate rendering and ownership. Standalone preview covers
Markdown, HTML and SVG; this does not establish binary or rich-output navigation.
The delivery browser receipt records the checked version-change scenario and its limits.
A conflict retains notes, output selections and requirement drafts; reloading
updates the CAS base but never silently selects a newer output version.

Problem: a review left open after execution changes continued to show old
status/evidence until a manual reload. Decision: the existing conversation
observer compares only the mounted Turn's Attempt, model-context revision and
status. It invalidates the shared presenter, which owns one background read and
coalesces intervening changes into one pending refresh. No polling, retained
snapshot log or second Turn owner is added. Display-only changes and unrelated
Turns do not trigger reads; retired mounts unsubscribe and abort late work.
Explicit reads/saves fence older automatic responses. Read failure is visible
and does not retry without another committed change or owner action.

An active field, requirement draft, selected fact/output version or unsaved note
keeps its exact DOM, selection and CAS basis. Pending updates offer the existing
explicit reload action. Reload may update the requirement CAS base but keeps
exact evidence-version choices; it never upgrades an old selection silently.
Alternatives rejected: periodic reloads add idle work; rebuilding active inputs
loses focus/caret; replaying stale background responses after a save reverses
the owner's visible decision; automatically rebinding outputs grants unchecked
versions. Existing artifact-review behavior remains on the same presenter.

Local native-browser desktop/English and 390px/Chinese probes observed the
terminal commit become visibly stale in 47.4–47.6 ms with one review GET each,
without a reload click. They also preserve a live evidence draft and caret
through an answer update, then require explicit refresh/reselection. A burst
test coalesces 100 in-flight invalidations into one follow-up read; 1,000
unrelated/display-only observations cause no further request. These are local
interaction and ownership checks, not a full long-history performance SLO.

Each decision binds one or more exact output versions and captures the server-
resolved output kind, label and artifact ID. Replacing a slot cannot rewrite
the historical file reference. An answer version uses
the Turn identity, current attempt and authority `modelContextRevision` (or the
conservative projection revision on older records). A repeated attempt or an
A→B→A answer change cannot inherit the old pass. Declared display-only settled
enrichment follows the Turn owner's existing context-version classification.
Artifact versions bind the slot generation, immutable artifact ID and content
SHA-256. Replacing or removing/readding a slot changes its generation, including
when an old artifact is selected again. Renaming/reordering a slot preserves its
version. Independent unchanged outputs retain their judgments.

A global constraint must assess all declared outputs; adding a slot makes a
previous constraint judgment stale. Ordinary requirements bind only their
selected outputs. Adding an uncovered slot preserves independent judgments but
prevents acceptance. Removed/deleted/unavailable bound outputs make dependent
judgments stale while retaining the original notes and versions. A transient
storage failure propagates rather than being reported as a current pass.

`accepted` requires a completed Turn, every current criterion passing and every
declared output covered by current passing evidence. Running/interrupted/failed
execution is never accepted. Current failure yields `changes_requested`; no
criteria yields `unconfigured`; remaining cases are `pending`. This is owner
attestation over a declared scope, not automated correctness verification.
Working-fact bindings use the same selected graph and source assessment as
artifact reviews. Requirement edits, global constraints, exact source changes,
owner/tenant fencing, lost acknowledgements and stale-tab conflicts follow the
shared rules above. Writes remain user-principal-only; no model tool may attest.

Requirements and output membership share one CAS record; decisions have their
own revision. Concurrent changes can leave a newly written judgment stale,
never rebound to a newer output silently. These separate records and external
sources are not a transactionally consistent cross-domain snapshot. A fresh
read reevaluates them; an open tab alone is not a continuing validity guarantee.
`read_requirements` excludes decisions and source evaluation. The execution
port additionally verifies owner, task/attempt/Turn binding and current attempt
through the lifecycle facade; foreign or superseded bindings fail closed.
Non-main/non-assistant executions receive no ordinary review requirements.

The UI adds no polling, workers, timers or source-body cache. Each review read
uses one owned Turn read, two review-record reads
and metadata/ownership reads for declared artifacts. Working-fact work is only
for explicitly bound sources. There are at most 256 criteria and 256 declared
artifact slots per configuration, with the shared 1 MiB request and 8 MiB record
limits. Exceeding a limit rejects the mutation and preserves prior evidence.
Records are durable owner data in `turn_review_v1`, never a disposable cache.
Deletion of the target hides access without reclaiming owner evidence.

Alternatives rejected: inferring completeness from an artifact list can omit
unattached outputs or include another task's files; invalidating every output
on any file change discards independent evidence; extending proof integrity
into correctness would misrepresent what was verified. Specs are
`tests/artifacts/test_turn_review.py` on SQLite and native TofuDB, alongside the
unchanged artifact review tests and endpoint/DTO drift checks.

A local ten-read probe with 16 criteria and one artifact slot measured median
3.44 ms on SQLite / 2.56 ms on native TofuDB, with a 3,822-byte response. With
32 artifact slots, medians were 22.41 / 11.69 ms and responses 10,593 bytes.
Each view made one Turn and two review-record queries, plus one artifact and
one owner-conversation metadata query per slot; unbound views made no fact
queries or artifact-body reads. These are local API-view costs, not task
wall-time improvements, end-to-end latency bounds or correctness comparisons.

## Shared execution context

Problem: a recovered Goal creates a new ordinary Turn, so reading that Turn's
empty review silently loses the owner's latest corrections and existing output
judgments. Decision: accepted recovery captures a flat original review identity
in private Attempt configuration. The machine schema and reserved key are owned
by `conversation_execution_identity_v1.json`; public commands discard supplied
bindings, and a new attempt retains only its previously accepted source.
The common review/context owner validates owner, tenant, conversation, current
source Attempt and producer task before reading the original requirement and
decision records. Repeated recovery flattens this identity rather than adding
a traversal chain. Nested, foreign, malformed or superseded sources refuse.
The binding grants no execution, tool, wallet or lifecycle authority.

Unchanged artifact generations retain their owner decisions. Answer versions
remain bound to the current Turn/Attempt, so a successor's answer requires a new
judgment and an interrupted original Turn cannot become accepted. Changes to
requirements or facts invalidate affected decisions through the existing owners.
Alternatives rejected: copying a review freezes later corrections; injecting a
Goal-specific prompt or acceptance store duplicates common behavior; copying
the original answer's acceptance grants an unreviewed result. No new worker,
cache or review record is added. The protected descriptor occupies only bounded
identity fields in existing accepted configuration.

The real application fixture exercises both native transports, two complete
service losses, owner correction while a recovered actor request is active,
preservation of an accepted ledger's inode/mtime and review, replacement of only
the affected report, and exact five-call cumulative usage/fees. Providers are
controlled; this does not establish real-model correctness or a two-hour trial.
`test_turn_review_continuity.py` measures common admission queries and checks
flat binding, cold readback, retry, and scope rejection on both stores.
One local three-read probe used a 185-byte compact source descriptor with fixture
IDs. Each inherited admission made two Attempt, two Turn and one review-record
queries, compared with one Attempt, one Turn and one review record for ordinary
execution; repeated continuations do not increase that count. The samples took
1.86–1.92 ms on SQLite and 0.93–0.98 ms on daemon TofuDB. These are bounded local
observations, excluding provider/tool latency, not throughput or SLO evidence.

Problem: storing requirements in a review panel does not make them model input,
and compaction or overflow recovery can remove a previously injected snapshot.
Decision: `context_composer/turn_requirements.py` reacquires one complete
owner-scoped requirement record at common final prompt admission, after forced
compaction, before reactive retry body construction, and before rebuilding a
model fallback request. Requirement acquisition failure propagates through the
recovery boundary; it cannot trigger a fallback without current scope. Ordinary
transport resends retain their already admitted snapshot. ROOT and native/Flow
preparation share this owner. `turn_requirements` is a required user-authority
registry block, with no local truncation or optional-ablation bypass. It uses
the existing provider executor and 15-second context acquisition deadline; a
timeout, unavailable authority or invalid binding refuses the request. Late
results cannot mutate the task or prompt; cancellation releases the local lease.
The canonical prompt/body admission counts the complete block and refuses an
irreducible request. Content-addressed reuse avoids appending unchanged copies.

Delegated invocations carry a frozen, read-only source identity from the common
child factory in `tasks_pkg/requirement_source.py`. Descendants share the same
source; final admission rechecks the source owner's current Attempt, task and
Turn before reading requirements. Transient child carriers no longer imply that
durable owner requirements are absent. Child Turn/Attempt lifecycle fields stay
unset; supplied requirements grant neither tool capabilities nor human approval.
The shared assignment snapshot persists only source IDs, owner and tenant. Its
existing loader reconstructs the source after restart; older snapshots use their
already recorded parent Turn/Attempt birth. Requirement text and decisions are
never frozen into this binding. A follow-up uses its current caller's source,
not the old dialogue or session owner's source. Invalid scope, malformed metadata
or a superseded source refuses admission rather than reusing stale requirements.
Problem: child carriers previously bypassed the requirement reader. Decision:
reuse the ordinary final-admission owner with an explicit context source.
Alternatives rejected: copying root lifecycle fields would grant unrelated
write authority; freezing requirement text or relying on the initial assignment
would miss later owner changes. This delivers current scope, not proof of model
compliance or automatic reexecution of previously completed outputs.

Ordinary Swarm parent proxies carry this source just like retained assignments.
The session snapshot persists it; cold reconstruction validates the original
scope, while accepted Continue binds the current caller. Native execution uses
a separate process-local parent binding, so freezing assignment inputs cannot
drop child accounting or silently change the execution route. The public native
delegation test changes the owner region while a child request is in flight,
checks the next request, preserves the accepted ledger, and verifies both report
versions and all parent/child fees. Its controlled provider proves delivery and
accounting; it does not establish real-model compliance or long-horizon recovery.
One local binding probe used a 48-byte shallow descriptor and 158 compact JSON
bytes for both durable fields with fixture IDs; borrowing cost 5.88–6.05 µs per
call across five batches of 1,000. References borrow the existing task/account;
no worker, cache, journal or extra model request is created. These figures exclude
the borrowed object graph and do not establish end-to-end latency or memory use.

A local two-acquisition probe per store measured 2.01–3.09 ms on SQLite and
1.41–1.49 ms on native TofuDB, with one Attempt, one owned Turn and one review
record query per child admission. The frozen source used 80 bytes of Python
slots (excluding referenced strings); its assignment JSON field used 196 bytes
with the fixture IDs. Size varies with IDs, not requirement text; descendants
reuse the immutable source. No additional cache, worker or durable record is
introduced. These small local samples do not establish parallel throughput or
model compliance.

The snapshot contains objective, current requirement revision and criterion
digests, plus declared output IDs/labels and artifact IDs. The answer is implicit.
It contains no review decisions, artifact body or recursive source evaluation,
and grants no additional permissions. A successful save is not evidence that
a model has consumed it: a request already in flight keeps its prior snapshot,
and the next admitted request reads the new one. Physical request tests capture
actual HTTP bodies after a requirement change and after history replacement;
this is delivery evidence, not proof that a model followed every requirement.

Alternatives rejected: a separate review/model loop would split execution
authority; letting summaries stand in for current requirements can preserve
superseded constraints; treating acquisition failure as optional would permit
silent execution against old scope. Read cost is one attempt, one owned Turn
(including its projection), and one requirement-record query per admission.
Large Turn projection read amplification remains a measurement target; this
increment does not claim whole-task speed or correctness improvements.

Specs: `tests/artifacts/test_turn_requirements_context.py`, shared presenter and
mount tests, and `tests/visual/test_visual_turn_review.py` on desktop/mobile.
`tests/tasks_pkg/test_native_delivery_rehearsal.py` combines real ROOT process
death after file publication, pending-effect reconciliation, a revised criterion,
preserved acceptance of unchanged output and explicit final owner review. Both
same-executor continuation and accepted executor takeover retain the logical
Turn. Its scripted provider verifies request delivery and recovery only; it is
not evidence of real-model task quality or two-hour service continuity.

The accepted-report variant first publishes and owner-accepts both a ledger and
a west-region report. Report acceptance binds a fact quoting the Turn's region
criterion. After real ROOT SIGKILL, changing only that criterion to east makes
the old report's evidence stale while preserving the ledger's exact acceptance.
The continued provider request carries the revised scope; its scripted response
rewrites only the report. A new immutable report version receives explicit owner
review and the old west report and stale evidence remain inspectable. Both
executor paths reconcile the initial two file effects without republishing the
ledger and settle exactly three model requests and three tool effects. This is
an integrated deterministic recovery proof, not an autonomous selective planner
or a real-model two-hour task.

The full-service rehearsal now uses `server.create_production_app`, both complete
production startups, selected embedded storage, actual loopback HTTP and the
ordinary native ROOT. Two HTML files are published by real `write_file` handlers;
authenticated review/fact endpoints record the fixture owner's acceptance. The
whole service dies after publication but before checkpoint completion. Public
Continue reconciles those original effects, carries their original paid price
rows and receives the revised region through the common context owner. Only the
report is rewritten; the ledger inode, bytes and exact acceptance remain intact,
and the old report version stays readable. The same browser page reconnects at
the same origin and shows pending execution, stale dependent evidence and final
acceptance at desktop/mobile widths. Three scripted provider calls consume 15
synthetic tokens and three file effects; this qualifies composition, not model
quality, real financial cost or two-hour continuity.

Problem: a ROOT subprocess or mocked application startup could miss production
recovery, authentication and browser resynchronization boundaries. Decision:
`test_native_production_recovery.py` and `test_visual_production_recovery.py`
exercise those owners without replacing storage, routes or execution. Explicit
test launch policy supplies the existing checkpoint factory and one crash hold;
this does not enable automatic/default recovery. Alternatives rejected: direct
database seeding bypasses admission; replaying writes hides reconciliation bugs;
accepting outputs inside the model grants it human authority. A later user edit
survives because committed proof establishes historical publication, not current
content. When that private publication evidence is unavailable, recovery refuses
before a second provider call and preserves the spent five tokens and file.

The same rehearsal uses the production-owned `lib.hypercorn_runtime` transport,
configuration and shutdown bridge. Browser SSE remains connected through final
SIGTERM; only the application receives that signal, so native children must be
released by their owners. Exit code zero, joined children, an empty native host
and a closed image are required. Problem: an upstream Hypercorn test launcher
joined listeners before live streams drained and timed out before the lifespan.
Decision: verify the existing production transport and retain the live consumer.
Alternatives rejected: closing the browser first conceals that boundary;
signalling the whole process group cannot prove owner-driven child cleanup.

A local warm ten-read probe (16 criteria, one declared artifact) measured
read+render medians of 1.97 ms SQLite / 1.24 ms native for an empty answer,
and 2.07 / 3.43 ms for a 256 KiB answer projection. Each admission made
three queries; the reused model context was 3,638 JSON bytes in both cases.
This excludes provider latency and Inspector persistence; it is diagnostic
read amplification evidence, not a throughput or end-to-end performance claim.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
