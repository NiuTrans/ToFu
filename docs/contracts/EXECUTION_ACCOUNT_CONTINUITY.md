# Execution account continuity

Owner: `lib/agent_core/execution_budget.py` and `account_frontier.py`; Goal
composition: `lib/goal_runs/account_journal.py`. Wire authority:
[`execution_account_frontier_v1.schema.json`](../../contracts/execution_account_frontier_v1.schema.json).
The packaged schema comes from `scripts/gen_execution_account_frontier.py`.

## Problem and decision

Completed Flow receipts are cumulative; interrupted native checkpoints are local.
Adding them double-counts work, while one completed leaf may omit later or root spending.
Decision: observe consistent whole-account cuts on the existing `ExecutionBudget` and Goal
journal after readiness and accepted recovery/restoration, before effects. Cuts acknowledge
binding, child attachment/detachment, model dispatch/settlement, charged tool slots and recovered
prefix transfer. The budget lock precedes the journal lock; locked journal callbacks must not
acquire the budget lock. This port creates no account, store, executor or timer.

Cuts retain original limits/expiry, live-actor counters, pending model reservations, scalar closed
prefixes and optional source/version plus unrebound paid actors. Closed membership does not grow
in memory. Unknown usage and unsettled requests remain explicit; charged tool quota does not prove
effect completion. Alternatives rejected: summing overlapping receipts, creating an account per
actor, trusting an older checkpoint as a complete inventory, or duplicating the execution loop.

## Failure, recovery and authority

- Every observed dispatch/charged tool slot waits for exact publication ACK.
  A failed observation revokes subsequent model and tool admission, including
  unbounded tasks. Paid usage and consumed slots are never refunded by failure.
- Settlement and ordinary detachment finish cleanup even if publication fails;
  the account marks consumption incomplete. Process death leaves either the
  committed whole cut or an earlier cut with unresolved activity, never proof
  that an unobserved effect did not occur.
- Launch identity, owner, root, contiguous observation versions, fixed limits
  and nonregressing counters are validated from durable history. Schema shape is
  validated in full on account binding and every durable read. Producer cuts
  check actor identity, ownership, scalar bounds derived from the schema and
  counter relationships on every update; static schema trees are not rewalked
  for each child attachment. The live event projection omits the actor table and reservation body.
- A complete settled whole cut must dominate completed receipt counters. It
  restores the existing successor account once after accepted source/target
  lineage. An identical restore cannot rewind subsequent spending. The immutable
  successor launch carries the original account seed, so losing the process
  before or after observer binding does not grant a fresh allowance.
- `preflight_registered_actor_accounts` requires exactly the complete live
  direct-child set, known settled accounting and matching native actor counters
  and limits, with the proven pending-file quota exception below. Each native read still requires a dead writer and unchanged birth,
  header and snapshot versions. Closed prefixes are compared as part of the
  whole total; local actor totals are never added again.
- This preflight does not settle wallets, claim leases, complete children or
  authorize actor re-execution. Accepted aggregate takeover, atomic version
  revalidation and unresolved-tool reconciliation remain separate requirements.
  Nested actor takeover is not admitted by the direct-child preflight.

### Rebinding paid local consumption

`restore_pending_frontier` installs one known, settled direct-actor cut on a pristine successor.
Accepted lineage, native versions and wallet debt remain application responsibilities; no replay
or lease is granted. `rebind_actor_prefix` moves prepaid tokens, tools and attempt ordinals to the
exact pristine child without changing global consumption. Identical calls cannot rewind spending;
changed/reused sources, foreign/used recipients refuse. Original zero caps and expiry remain binding.

Each cut retains every unbound actor, including zero-cost actors, even after current children close.
History must prove each source-to-pristine-target move. Dropping, resurrecting, changing source version,
or moving unbound debt into closed totals refuses. Failed ACKs retain paid counters and used bindings,
fence new admission even without finite limits, and allow cleanup. Partial cuts cannot enter ordinary
Goal replay or complete native preflight; automatic recovery needs durable transfer and wallet settlement.

The frozen cut and shrinking pending map live with the recovery account; recipient entries disappear
at child close. Ordinary accounts allocate no recovery maps. No worker, timer or quota state machine
is added. Alternatives rejected: ordinary attachment double-charges the restored prefix; marking
unbound actors closed can hide unknown tool effects.

### Original payers in native actor checkpoints

Problem: executor identity cannot recover the original payer; treating every `billableTaskId` as
transferred descendant work confuses inherited actor rounds with child invoices. Decision: actor-local
fee checkpoints carry `billable_task_id`; `sourceRound` identifies transferred descendants. Historical
producers/payers precede fresh rounds, whose quotes name the current shared root. Root-local rounds
cannot claim this field. Original rates/integer credits survive; current prices never reprice them.

`NativeTaskCostPolicy` restores only against the exact owned actor account. Historical tokens require
acknowledged rebind and an immutable inherited-prefix copy. Unknown sibling usage remains possible;
local proof grants neither a root invoice nor quiescence. Priced actor composition must declare
`checkpoint_fee_payer_origins`; older closed configure schemas refuse before effects. Missing payer
evidence is never inferred. Root checkpoint executor/descendant semantics remain unchanged.

One process-lifetime schema validator preserves every ownership, shape and arithmetic check.
Seven alternating samples for 2/32/256 rounds measured capture at 13.850→0.367 / 16.965→3.208 /
38.421→24.836 ms. Six-character payer IDs added 56/896/7,168 JSON bytes under existing snapshot/frame
bounds and retention. Storage/provider/wallet work is excluded; no worker, journal or actor cache is added.

Alternatives rejected: rewriting/inferring payers, comparing local fees with whole-tree totals, or
rechecking static schemas on every capture. Installed SDK tests cover three authorized generations,
changed prices, exactly three model requests/two tool calls and old-artifact refusal; native Flow/SIGKILL
checks durable payer transport. Actor takeover still requires accepted aggregate handoff and settlement.

### Completed actor fee evidence

Problem: public Flow results omit frozen native quotes and cannot prove original wallet debt.
Decision: `invocation_recovery` stores optional private `native_fees` beside admission/public receipt
in the same immutable task-result completion CAS. `billing.native_actor_fees` captures only the
acknowledged child-transfer grant using the shared checkpoint schema. Owner/root/actor/producer/payer,
quote arithmetic and token coverage must match; the cumulative account covers this actor's usage
without certifying sibling settlement. Quotes never enter public results.

`read_registered_actor_fees` returns the owned registration version and validated evidence. Legacy
absence is unknown debt, distinct from explicit zero-token/empty-round proof. Present malformed data
refuses both public recovery and financial inspection. Atomic takeover/settlement remain separate.
Alternatives rejected: repricing public usage loses original charges; missing-as-zero loses debt;
a second fee journal creates a dual-write gap. Verify completion CAS/lost ACK, cold SIGKILL reads and
sequential/parallel children with `test_flow_invocation_completion` and `test_native_flow_completion_fees`.

Evidence follows durable task-result retention, with no extra transaction, worker or collection.
Seven-sample warm capture for 2/32/256 rounds took 399/4,423/33,679 µs; read validation took
311/3,371/25,798 µs; compact JSON was 1,828/26,992/215,086 bytes. These copying/schema/quote measurements
exclude provider/storage/wallet work; existing transport bounds apply.

### Interrupted actor evidence

`inspect_journal` returns a `JournalRecovery` with separate completed records
and pending agent admissions. The completed-only reader requires `settled()`;
it still refuses any unfinished agent. A completion resolver can report absence
only after validating the registered birth and owner. A present null, malformed,
mismatched or inaccessible completion is an error, never an absent receipt. A
later continuation header cannot hide unresolved admissions from its predecessor.

`inspect_goal_execution` binds this observation to the immutable Goal launch and
whole-account stream. Explicit bounded pending inspection through
`load_goal_recovery_seed(..., pending_actor_bounds=...)` additionally requires:

- Every pending agent matches exactly one active direct child of the same account.
- All model consumption is known and settled; the whole cut covers completed
  receipt totals and fixed limits without adding overlapping snapshots.
- Native dead-writer evidence and owned registration are unchanged across reads.
  Both registration and checkpoint versions are retained with the Flow key,
  admission nonce, input digest, original fee origins and unresolved tool IDs.

`PendingGoalActors` belongs to its source run and launch; it invents neither results
nor a complete account. Pending execution requires the trusted application composition
below. Native `completed` is not a Flow result or worker-context receipt.
Inspection retains bounded native snapshots for this seed's lifetime, with no
durable event, worker, lease or wallet mutation. Packet/native read limits remain.

Problem: native retirement needs physical source positions and an accepted successor;
reconstructing positions from decoded indexes can target a chunk instead of its reference.
Decision: `pending_claim.claim_pending_goal_actors` uses actual enclosing reference
positions, the full-stream cursor, accepted continuation/task/limits and original
fee evidence. One existing native `goal_claim` atomically revalidates and retires
the source. Unknown outcomes never retry or grant preparation rights. A matching
receipt binds the immutable source digest and exact successor; it grants no lease.
Explicit pending bounds use the ordinary recovery command and task-binding owners.
The checked claim plus a caller-owned `NativeGoalHandoff` permits journal preparation
and restores pending counters through the existing account owner. It writes no second
source claim. The prebound atomic sink fences root and child model/tool admissions
until all recipients are acknowledged; child preparation and cleanup remain possible.
Alternatives rejected: decoded event ordinals, inferred prices, a second claim writer,
or enabling automatic Flow replay before exact pending inputs and all recipients exist.
`test_goal_pending_claim_sigkill` covers an actual killed writer, chunked header,
accepted continuation, original-wallet settlement and pending successor journal;
lost native ACK grants neither prepared journal nor new account, with no retry.

### Charged files after the native pending checkpoint

Problem: native `tools_pending` acknowledges the paid model response and exact
prepared tool batch before the gateway charges each tool. A crash after file
publication can therefore leave the Goal cut one or more calls ahead of the
native actor snapshot. Requiring equality alone rejects this normal boundary.

Decision: all token/ordinal/limit comparisons remain exact. A positive tool-count
difference is observable only for `tools_pending`, and only when it equals the
entire supported `write_file` batch. The common `native_file_evidence` validator
binds its original producer, call IDs, distinct operation IDs and owned invocation
references. `inspect_child_effect_batch` reads one owned registration version,
matches every persisted call and round, queries the existing file-publication
authority, then rechecks the registration. Each operation must prove
`file_published` or `file_not_published`; partial, missing, foreign, changed or
unknown evidence refuses. Prepared-but-unpublished proof is not an instruction
to retry the write.

The returned `pending_file_tools` preserves ordered observations and the number
of already charged calls separately from both original snapshots. Neither the
native snapshot nor the cumulative Goal cut is rewritten, summed or recharged.
The read adds no journal or durable event and creates no execution authority.
Native model restore retains the loaded checkpoint. Registered-child file
reconciliation matches its rebound source, original counters, parent and exact
call/round receipts on the selected checkpoint authority, rechecking versions.
Only zero or the complete batch may explain prepaid calls; new spending refuses.
Known results become observations, preserving paid quota. Proven unpaid calls
publish one `tools_reconciled` cut; unknown results/failed ACKs fence even unbounded accounts.
Cancellation joins queries. Ordinary pending Goal admission remains fenced.

Batch inspection adds two owned-record reads per actor (before/after), not per
operation, and one existing domain query per file. A local in-memory measurement
for 1/32/256 calls took 0.70/2.81/17.73 ms median and retained 302/8,416/67,261
serialized proof bytes. It excludes storage/native I/O and real file hashing;
it is not an end-to-end recovery latency claim. No worker or persistent growth
is introduced; proof lifetime and count follow the bounded pending snapshot.

Alternatives rejected: infer tool identity/outcome from a counter difference;
pretend a modified snapshot was acknowledged; or reuse ROOT reconciliation's
charge increment after restoring the Goal's already-paid quota. Those lose
operation identity or double-charge. Real private-native SIGKILL tests park the
registered gateway immediately before replacement and after replacement before
undo/result ACK; both preserve the original operation and exactly one quota.

Older launches without this observer retain their completed-receipt recovery
contract. Launches declaring observation coverage cannot recover effects that
preceded account binding or omit account observations after effect admission.

## Aggregate original fee evidence

Problem: summing overlapping actor fee prefixes or cumulative receipts can double-charge a historical
model call or hide an uncovered closed participant.

Decision: `pending_fee_evidence.preflight_pending_fees` reads each completed
actor's private completion with exact admission, full public receipt and stored
version. Pending quotes decode through the same private actor fee schema and
native quote validators, with exact owner/root/actor/tenant and usage coverage.
The result binds the launch, whole cut, registration versions and native versions.
It creates no durable records, live account, lease, executor or financial grant.

`native_aggregate_fees.project` indexes original quotes by producer and ordinal.
Overlapping rows must agree in payer, route and the entire frozen quote; changing
a payer conflicts rather than creating a different debt key. Each original
producer has one payer. The union must cover the whole cut's tokens exactly and
each active direct actor's local prefix. Unknown usage, missing legacy fees,
unbound recovery participants, duplicate actor evidence and signed wallet overflow
refuse. Root-owned consumption requires a separate quote owner and currently
refuses this actor-only projection; zero is read from the whole cut, never assumed.
Historical quotes still cover tokens but only the requested original root's rows
enter its candidate capsule. No price is recomputed and cumulative receipts are
never added. Returned data is detached from the source observations.

The candidate uses the existing wallet capsule contract and a supplied wallet/hold
binding. That binding is not financial authority: accepted aggregate handoff must
still revalidate it, the source versions, complete actor set, dead writers and
file evidence in the source-retirement transaction. Pending Goal execution and
ordinary registered-child recovery remain fenced. A candidate cannot be submitted
as a terminal result or a substitute for that transaction.

Alternatives rejected: sum per-actor or cumulative receipt totals; deduplicate
with a key that includes a mutable payer; infer absent fees as free execution; or
freeze each child's invoice independently of root debt retirement. Each loses
coverage, permits repeated debt, or separates takeover from financial authority.

A local nine-sample warm projection of 1/32/256 one-round actors took median
0.59/7.60/56.98 ms, with 8.7/66.9/530.3 KiB peak traced allocations and
296/306/313-byte invoice capsules. The source inputs were 1,624/34,429/272,116
bytes. This includes schema/coverage validation and detached fee copies, excludes
storage/provider/wallet I/O, and adds no background worker or persistent data.
Transient memory follows the inspected bounded evidence, not elapsed run time.

## Native aggregate retirement boundary

Problem: retiring paid actors separately from a Goal root with no native journal can split execution
ownership or lose its original payer after a lost response.

Decision: the private `NativeCheckpointStore.claim_goal_actors` port binds the
closed `native_goal_actor_claim_v1` request to existing Goal event locators,
registered completion versions, native header versions and an accepted generate
continuation. The source must be abandoned by startup recovery. Both the accepted
target and any abandoned preparation attempts must have admitted no execution.
The port rereads accepted lineage, current source cursor and record versions
inside the retirement transaction, and rechecks kernel writer death there.
Unknown, live, foreign and passive writer observations refuse.

Problem: backend-specific ancestry and fee rules prevent SQL from sharing the native whole-Goal authority. Decision: `tofu_runtime::checkpoint_goal_context` owns bounded event/chunk decoding, immutable launch/account cuts and the full accepted continuation chain through transaction-scoped `ReadAuthority` reads. The adapter filters owner/tenant before returning bounded records; shared code also checks run identity and cursor shape. `checkpoint_goal_policy` owns fee mode, native wallet holds and exact prepared-actor birth validation. TofuDB keeps its atomic actor/ledger/event/outbox commit. Alternatives: Python policy copies can diverge; a preflight context alone cannot authorize a later transaction. SQL ordinary append, Goal sequence allocation, state transition and deletion now lock the same owner-filtered run row, so transaction-scoped native reads fence them. Actual PostgreSQL baseline tests reproduced duplicate event sequences, terminal overwrite and an orphan after concurrent delete/append; paired SQL checks and native repeated-crash/whole-set recovery pass with the fix. See `system_native_goal_authority_port_20261016.json`. Problem: copying remaining whole-Goal checks into Python would split protected receipts and financial authority. Decision: `checkpoint_goal_claim` and `checkpoint_goal_consume` own complete claim preparation, protected retirement, settlement intent, consumption, prepared witnesses and exact receipts for both backends; `checkpoint_goal_codec` preserves canonical authority digests under both provider JSON feature modes. The SQL adapter calls this owner through ABI 3 inside one joined transaction and holds owner-filtered run, task, attempt, Turn, wallet and private-record locks through commit. Transaction-local callbacks never submit another Backend job. All actors and account cuts succeed together, and leases publish only after durable ACK. Alternatives: preflight-only reads race mutation; returning an inner refusal without throwing through the transaction would commit partial writes. See `system_native_goal_sql_transaction_20261016.json`. Logical outbox/replay and application/default admission remain separate unfinished requirements.

The same transaction retires every active source actor, keeps each exact paid
snapshot, settles the original wallet through the existing billing owner and
appends one `execution_recovery_claim` event. A protected native header witness
binds the receipt to the request digest; an ordinary Goal event alone grants no
authority. Whole-account consumption must match the original quote union,
including completed registered children. Root-owned tokens and unresolved model
requests refuse. Explicit account/owner lookup prevents an empty or different
wallet argument from evading an existing reservation. This first port requires
the original positive reservation and native-admission ledger entry. Prices are
never queried again. Exact retries wait for WAL durability and return the same receipt
without allocating leases or applying another debit.

The checkpoint C ABI exposes this claim without actor capabilities. The whole-set
consumer below issues leases through the common registry. Ordinary Goal/Flow
dispatch remains fenced until accepted recovery and tool reconciliation are
composed; these ports do not provide complete interrupted-Flow recovery.

Configured outbox capture writes one encrypted `native_goal_claim_outbox_v1`
mutation in the same transaction as every actor retirement, original wallet and
Goal receipt. Capture failure rolls back all of them. The existing publisher
replays this aggregate through the same claim transaction before acknowledging
the source. Passive replay requires an explicit evidence budget, pinned source
identity, exact authenticated actor prefix and matching original application
context, registrations, account and quotes. It replaces only local writer-death
observation with passive prefix proof; it grants no promotion or lease.
Target outbox configuration refuses forwarding. Exact retry returns the original
Goal cursor without another settlement; older replay requires immutable archive
proof plus every actor's latest matching witness and protected claim marker.
Tests begin from an exact prior application baseline; routing an entire mixed
application/outbox stream remains a separate requirement.

Alternatives rejected: transferring child leases before root debt splits ownership; synthetic root
checkpoints add another lifecycle; ordinary events permit impersonation. Existing Goal, checkpoint
and immutable billing owners retain authority.

Transient preparation is bounded by the caller's evidence byte allowance and
the selected store's launch-derived header/snapshot bounds. Retention adds one
small witness per retired actor and one aggregate event; paid snapshots remain
on their existing task lifecycle until the accepted whole-set consumer atomically
replaces them with successor snapshots. It adds no worker, authority or background polling.
The native tests cover final-wallet rollback, accepted/source/registration/native
version races, live writer refusal, malformed and chunked evidence, forged claims,
lost-response reopen and armed WAL write/short-write/sync failures. These are
storage-boundary tests using shared Python-produced fees and account fixtures;
replication tests additionally cover missing prefixes, changed authenticated
records, late capture rollback, source/target WAL faults and lost publisher ACKs.
They do not certify real application takeover or full application replication.

The two-pending/one-completed storage fixture adds 474 serialized header bytes
and a 750-byte aggregate event. Snapshot growth is zero; original snapshots are
retained. This measures logical payload growth, not physical WAL amplification
or application recovery latency. Configured replication adds a 1,700-byte clear
mutation (1,716 encrypted bytes) without snapshot copies, on the existing outbox
ACK/archive retention lifecycle. Per-actor replay witnesses replace existing
witnesses. No new worker, polling loop or unbounded in-memory log is added.


## Atomic whole-actor consumption

Problem: publishing each account rebind before creating its native journal can leave a crash with partially
transferred actors. Decision: a prebound budget batch sink receives every canonical cut once; common
OwnedLease prepares every target and the native authority commits the complete set before returning leases.
The closed handoff contract binds the accepted claim, successor cursor, exact registered births, original
account counters and a fresh parent wallet admission. One transaction writes target checkpoints and all
account cuts, marks sources, reclaims their private snapshots and appends `execution_recovery_consumed`.
That receipt is durable/private, does not reduce UI state, and alone grants no capability. Paid rounds
preserve original producers, payers, quotes and limits. Alternatives rejected: per-child publication leaves
split ownership; a second budget/journal lifecycle duplicates authority. The common owners remain in use.

The batch account port validates complete membership and its prebound sink, fencing admission until ACK and
after unknown/failed delivery. `goal_claim` and `goal_handoff` reuse the existing checkpoint C
ABI/worker/lifecycle owner; all capabilities follow WAL acknowledgement without holding registry locks over
I/O. Identical successful requests retrieve the same IDs only before first member use; storage errors grant
no new leases. No durable retry log or worker is added.

`NativeGoalHandoff` binds existing registered policies; their load/reconcile/close path verifies the
already-moved account without adding counters. Failed adoption forgets unadopted handles; adopted policies
retain cleanup. Closed, foreign, changed, incomplete or unacknowledged inputs cannot enable execution.
Problem: native atomic cuts could leave the ordinary Goal journal cursor stale. Decision:
`GoalJournalPublisher` owns both sequences under `ExecutionJournal`'s existing lock, deriving launch/account
reference positions from acknowledged packets. A complete native ACK advances past all cuts and the consumed
receipt before policy adoption or ordinary writes; unknown ACKs and reentry fence both journal and account.
Chunked prefixes retain their final reference positions. Alternatives rejected: a second writer, blind
cursor increments or replaying the native commit after an unknown result. `test_goal_native_journal` and the
real C ABI composition verify subsequent account/effect writes and joined ordering. Automatic pending
Goal/Flow admission remains fenced; explicit retargeting requires the preparation witness below. Supported file
reconciliation grants no automatic tool replay.

One encrypted outbox mutation carries authorization, target headers and context digest. Authenticated passive
replay rebuilds paid snapshots from exact prefixes through the same transaction, returning no lease. Source/target WAL faults, late capture rollback and lost publisher ACKs
are covered, including archived retry after newer target progress. This verifies a supplied exact
application baseline, not mixed-stream application replication.

Caller byte allowances bound preparation/publication. No worker is added. Two-actor C ABI output is 199
bytes; preflight is 4,244 bytes, not the 8 MiB evidence bound. The temporary registry receipt retains a
64-byte digest key, 16 bytes of IDs and one shared 64-byte digest, excluding container overhead. Each
capability entry adds a 16-byte optional shared-digest reference on x86_64. The outbox adds 4,198 clear /
4,214 encrypted bytes without snapshot copies. A local five-sample 1/32/256-actor batch probe emitted
824/176,928/10,242,338 JSON bytes at median 0.14/14.60/758.73 ms. Peak traced batch allocations were
7.5/385.8/20,780.3 KiB; retained allocations were 3.3/33.3/24.3 KiB. Whole account cuts still cause
quadratic transient and logical-write growth; explicit byte refusal preserves debt and fences admission.
These figures include JSON sizing/encoding, not durable storage or task latency.

## Committed handoff before child admission

Problem: native handoff can commit before the process dies without receiving ACK or starting children.
Decision: the ordinary invocation owner reserves its completion identity/nonce during preparation;
prepared/started events share that binding. Capture, completion and execution still require real admission.
The native consumer validates every birth and original Flow occurrence, then enriches the existing
consumption receipt with preparation and optional original file producer/parent. The same transaction
hashes each complete actor row into its retired source's protected header. A later prepared-actor claim
requires this exact incoming witness, complete accounts and writer death; an ordinary event is insufficient.
Started occurrences must retain the reservation and follow consumption.

Account predecessor and file producer differ after transfer. Pending snapshots retain original call,
operation and tool counters. Repeated handoff authenticates the immediately incoming protected witness
before propagating the origin, requiring one generation rather than an unbounded ancestry walk. Fresh
checkpoints use their current producer. After native ACK the policy binds the inspected file origin;
the budget resolves its own acknowledged predecessor. Changed, partial or unknown proof still refuses.

Alternatives rejected: invented started events fabricate execution; ordinary receipts permit forgery;
rewritten producers lose publication proof; assumed zero debt can release unresolved reserves. Legacy
unbound preparation remains readable but cannot grant pre-admission retarget. Existing journal/header
retention and byte bounds apply; no worker, journal or snapshot
copy is added. An actual single-actor unentered SIGKILL added 441 compact JSON bytes to preparation and
756 to its consumed row (106 for origin); the populated header adds 84 bytes. These are logical sizes,
not WAL or latency measurements; the account-cut amplification described above remains.

## Producer tool coordinates across continuation

Problem: the SDK model cursor counts accumulated provider rounds; ordinary tool rows count each tool
occurrence independently within the current producer. A resumed model round or a multi-tool response can
therefore have different coordinates. Decision: each captured file-recovery call records its original
`tool_round` from the prepared common handler occurrence, alongside its call and operation identities.
Both actor preflight and reconciliation compare that coordinate with the owned durable receipt. The
canonical checkpoint schema defines its positive unsigned integer bound; booleans and mismatches refuse.
Alternatives rejected: using the model cursor conflates independent counters; dropping the coordinate
check weakens occurrence identity; rewriting durable rows changes the original producer's evidence.

The field is optional only for older checkpoints: absence retains the exact legacy model-cursor comparison.
Ambiguous old evidence still refuses; no counter is guessed from a matching operation. New captures always
write per-call coordinates and remain under the existing pending-byte budget and checkpoint lifecycle.
No additional worker, registry, journal or retained snapshot is introduced.
Compact JSON grows by 15 bytes per call for a one-digit tool counter (34 at the unsigned 64-bit bound).
This is metadata payload growth; physical WAL amplification and end-to-end recovery latency are not measured.

A consumed recovery keeps its earlier source/version provenance after `pending_actors` becomes empty.
Problem: treating any retained recovery envelope as outstanding debt rejects a later valid generation.
Decision: native whole-account handoff uses the same empty-pending-membership requirement as Python
restoration and native aggregate invoicing. Shape, exact counters/cuts, accepted lineage, fresh recipient
registration, wallet admission and atomic publication checks still apply. Alternatives rejected: removing
provenance would discard evidence; accepting nonempty pending membership could transfer unresolved debt.

## Ordinary Goal recovery composition

Problem: validated pending takeover required fixture-only assembly and a wallet, excluding personal production recovery.
Decision: trusted `NativeRootConfiguration.goal_recovery` supplies `NativeGoalRecoveryConfiguration` to command preflight
and the ordinary runner. Defaults follow issued transport/selected storage timeout; header/snapshot/evidence also obey
canonical `maximumLegacyDocumentBytes`. Explicit oversized documents refuse before storage acquisition; other transport
capacity is unchanged. Request flags grant no capability. `native_recovery.execute_pending_goal_flow` inspects exact inputs,
resolves archived fee mode, claims once, prepares ordinary children and waits for whole-set ACK on the existing serving loop.
The checkpoint binding must match; only its epoch follows the source. Flow events, Turn projection, cleanup and billing
remain their existing owners. No loop, worker, journal or retry is added; unknown ACK/incompatible policy admits no provider.

`fee_account` freezes trusted launch admission: `cost`, or `wallet` with its owner/hold, including zero estimates. The claim accepts the existing
wallet capsule or `native_aggregate_cost_v1`; both prove the same complete cut and unique original producer/payer quotes.
Cost records retain nonzero credits without wallet authority. Legacy launches remain wallet-only; successors cannot change
mode. Native claim/consumption check immutable source/target policy within existing transaction/version fences. Cost mode
also refuses reserve, native-admission, release or debit activity for either root in the owner's account. Billed recovery
still requires exact original settlement and fresh positive target admission. The wallet capsule decoder is unchanged.
Alternatives rejected: fixture-only assembly, replay before ACK, inferred free execution, erased quotes or fabricated holds;
these bypass common completion, permit financial downgrade or misrepresent all-attempt cost. Default rollout stays opt-in.

Verify native mode/coverage/rollback, whole consumption, passive replay and duplicate-ACK tests. `test_goal_production_recovery`
retains dedicated-process repeated SIGKILL coverage; `test_native_production_goal` adds actual personal production HTTP startup,
child checkpoints, file publication, whole-service SIGKILL and ordinary continuation. Synthetic providers do not qualify a
two-hour real-model workload. Compact JSON adds 30 bytes to a nonempty launch; fixture invoice shrinks 297 to 252 bytes with the same 22,779 credits. No new worker.
Cuts/preparation scale with admitted actors/issued evidence bytes, not proportional closed history. Five-sample traced cut
publication for 1/32/256 actors took median 0.012/0.015/0.281 ms, peak 328 bytes; 780 KB launch seed digests took 3.19/9.85/57.27 ms,
peak 1.57 MB, excluding validation/I/O. Local ordinary-resume samples (one unpublished/two published actors) took 0.988/0.687 s,
three calls each including fixture/native setup/persistence, not production latency or p95. Digests still reject seed mutation.
<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
