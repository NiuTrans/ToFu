# Tofu-DB storage and migration

Native backend contract. Common authority, ownership and transaction rules remain
in [Storage](STORAGE.md); maintenance lives in [Storage operations](STORAGE_OPERATIONS.md).
Embedded response ownership and codec compatibility: [FFI response boundary](TOFUDB_EMBEDDED_RESPONSE.md). Immutable entity-page formats: [page storage](TOFUDB_ENTITY_PAGES.md).

Search-index startup recovery: **Problem:** a disposable projection with CONTROL beyond its WAL prevented the healthy source authority from serving after restart. **Decision:** only projection `InvalidData` opens rebuild under an exclusive index lease, stage one fresh sibling, publish it by rename, and reclaim the old index after verifying replacement open. Busy/permission/I/O errors and source integrity failures remain terminal. Both fresh and reopened projections disable write overlays. Search remains indexing until existing source-admission/backfill fences complete. **Alternatives rejected:** relaxing source WAL validation risks acknowledged user data; repeated retries cannot recreate missing index bytes; timestamped corrupt copies leak disk. A failed publication retains one bounded `.startup-recovery` sibling for diagnosis rather than overwriting it. Recovery adds no resident worker and retains at most the previous index plus one fresh replacement. The 1,018,861,619-byte restart-failure fixture rebuilt to an 8,192-byte index before `storage.ready`; source admission uses the existing budgeted worker. Verification: `turn_search_projection::recovery::tests`, `turn_search_worker`, and `tests/startup_integrity.rs` (source integrity remains fail-closed).

## WAL generation-bound acknowledgements

Problem: a background flush can finish after checkpoint installation changes the active WAL. A bare byte offset can falsely credit the old flush to new bytes, or strand an old response waiting for the successor to reach its offset. Decision: capture path/generation plus length while holding the authority mutex, then wait outside it. A flush updates only its captured generation. Installing a successor occurs only after synchronizing the complete old WAL and durably publishing its checkpoint or sealed ownership in CONTROL. This covers old witnesses and wakes them; it never covers successor writes. A late open/flush error for the retired path cannot poison the successor. Current generation failures remain latched and fail closed. Reattachment to the same path preserves progress. Alternatives rejected: holding the authority lock through fsync destroys group commit; retaining every historical generation would grow an unnecessary registry. `wal::tests` deterministically orders an old flush after rotation and crashes the simulated filesystem, verifying that the successor is acknowledged only after its own durability barrier.

Online rotation owns carried payload bytes through indexed immutable history. `history_payload.rs` records block IDs and exact offsets using the canonical transaction parser; it checks the carried bytes against each content ID. Before encoding an online prefix, the engine synchronizes its exact tip and publishes version-3 CONTROL with one sealed-predecessor descriptor and an empty successor WAL. New writes enter only that successor. The driver durably writes the predecessor segment, offset index, sorted routing pages, routing root and version-3 history manifest, then publishes the prefix checkpoint through CONTROL before retiring the predecessor. Successor transactions remain WAL-owned and keep their active buffer. The existing CONTROL publication conflict fence and hard successor WAL ceiling remain; encoding alone does not park daemon writes. Manual engine checkpoints retain the complete-prefix path. A second generation handover waits until the sealed prefix is installed and prior retirement finishes.

Recovery verifies both WAL generations and requires the predecessor's exact
terminal witness. It finishes that prefix before exposing a writer. A nonempty
unreferenced successor fails closed so fallback to an older CONTROL slot cannot
silently hide confirmed writes. Protocol, probes, resource bounds and defeated
alternatives live in [rotation ownership](TOFUDB_ROTATION_WINDOW.md).

Routing uses immutable sorted runs with at most 64 levels and one candidate
page per level. Pages and roots remain within the existing 4 MiB block bound.
Point reads verify source framing and the returned payload's content hash;
packed source relocation falls back to a verified ordinary block. Duplicate
locations may supply another verified copy after a missing/corrupt source.
Routing pages share the existing byte-bounded block cache; verification witnesses
are weak pointers to those exact allocations, not a new payload cache. Reopen
loads the routing root without scanning or restoring all history-carried bytes.

Verified history source handles use an LRU slot budget derived from the existing
block-cache budget divided by the maximum block size; zero budget disables them.
Opening and framing I/O run outside the cache mutex. Individual source handles
serialize their mutable VFS reads, validate each returned block hash, and evict
on corruption/read failure. Installing a catalog drops retired cached handles;
background relocation reads whole sources without polluting the point-read cache.

Entity recovery cannot infer tree materialization from a retired WAL. The first
deferred mutation publishes a root-directory coverage cursor in the same durable
transaction as its delta. Cursor replay reads history as well as the active log;
missing uncovered transactions fail closed. Automatic history pruning respects
that durable cursor, including a partially covered transaction. Explicit operator
pruning first merges the overlay. Legacy complete-root opens keep their lazy
history behavior. This preserves acknowledged entity values through empty active
logs and through partial-merge crash recovery.

After successful CONTROL publication, covered carried RAM copies are released with staged-byte accounting; in-flight pack plans retain their own Arc buffers. Synchronous checkpoints keep ordinary materialization and preserve previous indexed roots. History pruning reserves the existing physical publication lane with no I/O. The maintenance worker releases the authority mutex, verifies one bounded history source at a time and promotes **every** carried block into immutable payload segments (durable loose fallback only at catalog capacity). This includes authority roots, so pinned readers and identifier-only references retain bytes. It rewrites routing metadata outside the mutex, then publishes payload and history roots together using the current logical state. Source-root identity checks prevent a stale physical rebase; foreground WAL/authority updates remain current. A lost publication task latches restart-required. Synchronous operators reuse the same prepare/encode/install protocol. Backup, salvage, online and offline GC retain source indexes, routing roots and pages. Verification rebuilds each segment offset index from its authenticated source and checks the persisted witness. The deterministic fault matrices cover first/subsequent rotation and indexed pruning with I/O errors, short writes and one lost sync.

Mixed-shard payload segments may change their first-block shard hint during
online GC. Reclaim publishes a canonical replacement manifest and rebuilds
routing; it must not reject a valid retained subset merely because the minimum
block was deleted. Deferred file retirement remains bounded and distinct from
catalog publication; fault tests drain that lifecycle before comparing the exact
remaining file set with the published catalog.

Format boundary: old version-1/2 history manifests remain readable. New indexed
rotations write version 3; earlier binaries reject them. Downgrades require a
compatible export/restore path, never running an earlier binary directly against
indexed storage. Online handover also writes version-3 CONTROL while a sealed
predecessor exists; versions 1/2 remain readable. Descriptor-free publications
write version 2, but an alternate version-3 slot can remain. WAL framing is
unchanged; binary downgrade requires compatible export/restore.

Problem: online rotation previously required duplicating carried payloads into
loose/packed storage before it could retire the WAL, even though immutable
history already contained the exact bytes. Decision: make those immutable bytes
a durable block source with a content-addressed location index. Alternatives
rejected: relaxing acknowledgement durability; copying a growing WAL suffix
under the write lock; skipping window durability; restoring every historical
payload into memory at startup. Explicit pruning retains batched relocation cost behind the existing worker,
outside the authority mutex and hot rotation path. Performance acceptance and defeated experiments
live in [the experiment record](TOFUDB_PERFORMANCE_EXPERIMENTS.md).


Problem: a fenced rotation let commands repeatedly load projections and stage payloads before discovering at commit that writes were unavailable; a thousand live tasks amplified this work and queued again just to inspect rotation state. Decision: after validating replay identity and returning any committed receipt, mutating IR checks the engine's write admission before evaluating its steps. Reads remain available, and commit still enforces the final fence. A busy dispatch captures its rotation predicate and completion generation in its existing authority hold. It waits outside the authority until publication or terminal failure; preparatory doorbells and transient drain retries do not redispatch it. Parking checks cancellation in 25 ms slices and retains its original deadline. The FIFO permit lives through the mutex hold and is dropped after unlocking, so the successor wakes when it can acquire, rather than polling a still-held mutex. Neither path needs a second admission. Alternatives rejected: larger connection pools merely relocate the backlog; relaxing durability does not remove repeated semantic work. Fixed aggregate stage counters separate admission, execution, WAL waits and Turn-event phases, with fixed component labels distinguishing foreground and background work; `benchmarks/conversation_concurrency.py --profile-client` additionally reports bounded pool/wire histograms without user payloads. Queue/wait totals overlap across callers and must not be added as serial wall time.

Blob staging sizes each read buffer from the remaining admitted bytes plus
one overflow-detection byte, bounded by the existing 1 MiB chunk format. A
short EOF avoids allocating another empty chunk. Each Engine lazily owns one
reusable level-3 compressor; reuse preserves exact chunk bytes across input
sizes and does not introduce thread-local codec caches. The existing chunk
ceiling bounds its workspace and Engine teardown releases it. Per-block codec
construction and a 1 MiB scratch buffer for every small document were rejected
because streaming projections multiplied their allocation/initialization cost.

Application superseded-Attempt cleanup defaults to one page of 64, within the
native transaction ceiling; explicit larger limits remain subject to the
selected authority's admission. The domain facade returns the exact deleted
count from either the native diagnostic receipt or the Sidecar integer;
malformed counts fail closed. Verification uses both real backends and the
embedded transport (`tests/storage/test_storage_application_contract.py`).

Live `turn.event.record` patches carrying `projection_segments_stable: true`
apply against the stable segment view under the Turn revision lock. The
committed replay patch is rebuilt from the exact previous wire projection
when that normalization changes the base, so reconnecting clients receive the
repair as part of the same revision. Unattested legacy patches retain their
exact supplied base; slim events cannot also carry a structural patch. Native
normalization rejects stored states requiring unsupported full segment
assembly instead of silently inventing a different projection.

`record.get` for `task_results` resolves through the task-result owner header
and bounded payload codec. It returns the header's current version and overlays
abort/recovery lifecycle facts onto the stored content: those operations advance
the header without rewriting the large content document. Generic record writes
and lists remain unavailable for this namespace; cross-owner reads return no
record, even with an explicit payload owner that differs from the session.

Live text persistence and its internal native format are owned by
[STORAGE_TURN_TEXT.md](STORAGE_TURN_TEXT.md); replay references: [STORAGE_EVENT_REPLAY.md](STORAGE_EVENT_REPLAY.md).

## Foreground blob staging and maintenance progress

Problem: complete blob content was already available for WAL publication, but negative loose-file deduplication probes added a network metadata round trip for every new chunk and manifest. Decision: blob staging still reuses byte-verified cached/packed content and records its GC relink fence; otherwise it carries the complete payload in WAL without probing an optional loose copy. Materialization retains its ordinary verification and durability barriers. No cache or queue is added. General block probes and checkpoint-history staging keep their existing loose-file semantics. Alternatives rejected: trusting a bloom hit could reference missing data; omitting the payload before verified durable reuse weakens recovery. `blob_staging_commit_faults_preserve_every_acknowledged_payload` snapshots durable VFS state before destructor catch-up for every commit I/O error and torn write; the block gate verifies zero I/O for new staging and exact packed reuse.

Problem: per-conversation pressure checks consumed a maintenance fairness permit
again inside its admitted batch, so continuous search polling reduced index
progress to one conversation per two seconds. Decision: a permit admits one
existing bounded dirty batch. Light query polling alone does not defer indexing;
foreground writes or measured admission waits do, with a two-second fairness
bound. Deferred batches retain their dirty count so idle backoff cannot hide debt.
Snapshot reads remain outside the authority mutex and publication uses exact ACKs. Increasing timeouts or dropping
invalidations was rejected because neither repairs scheduling progress.

Problem: parent EOF set a stop flag but connection threads blocked in socket reads
could wait the entire 30-second I/O timeout. Decision: the bounded worker list owns
shared socket handles, shuts down their read halves, then joins every worker.
In-flight durable commands may finish and write their responses; authority teardown
never races a detached worker. Sharing each socket reduces descriptors from two
to one per connection. Reducing the normal frame timeout was rejected because it
would penalize slow healthy peers. A real loopback regression retains a connected
peer under the production timeout and requires prompt parent-stop completion.

Entity read-your-writes reuses its existing snapshot witness instead of rereading
tree pages. The first witness resolves the WAL overlay before the base tree;
concurrent updates still fail commit validation. This adds no resident cache.

## Committed conversation wakeups

Each native transaction coalesces content-free notices by owned conversation,
retaining its highest durable sync sequence. Notice keys/counts have hard
bounds in `contracts/storage_v2.json`; they are returned only by successful
commit. The RPC handler owns a fresh request-local notice list: failed
commits, queries, and receipt-only retries never emit notices or inherit a
previous command's state; no global pending-notification queue is retained.

Successful commands with notices use the private `storage.committed-events/v1`
JSON envelope already understood by the application port; frame fields remain
unchanged. The application maps native topic/key notices into owner-scoped
conversation invalidations, then reads authoritative replay by cursor; a
subscriber failure cannot invalidate a committed command. Raw wire clients use
`StorageV2Response.value()` for semantic results and `.json()` for diagnostics
or notification delivery. Envelope bounds and application topic mappings are
generated from the same protocol contract. Offline migration sessions retain
their separate operator response contract.

## Live projection patch budgets

The native contract separates the individual wire patch budget (8 MiB) from
the compact projection-head chain budget (1 MiB across at most 64 patches).
Request-frame and transaction admission remain independent hard limits. A
patch larger than the chain budget folds directly into a checkpoint at its
committed revision; it never creates an unreadable oversized chain. Later
patches use that exact checkpoint, and durable attempt/sync replay retains
the same revision transition; every fold follows the same transaction
boundary as Turn/Attempt publication.

## Restart recovery of materialized Turns

**Problem:** individually valid projection checkpoints and append patches can
materialize beyond the generic 8 MiB document-write limit. Recovery previously
retired that head and attempted to store the full Turn through the generic
writer, producing `database_protocol_error` instead of settling the orphan.
**Decision:** `turn.recover` uses the existing bounded Turn-snapshot writer
(`MAX_TURN_SNAPSHOT_BYTES`) and the same blob owner, physical-version fence,
transaction, indexes and terminal event. Recovery remains atomic and idempotent
across reopen. **Rejected alternatives:** raising generic request/document caps,
truncating projections, skipping the orphan or retrying deterministic validation
errors. No ordinary live-write or protocol-frame admission limit is widened.

Executable specification: `packages/tofu-db/tests/large_turn_restart_recovery.rs` checks a below-8-MiB control and a checkpoint-plus-patch above 8 MiB, exact projection preservation, settlement after reopen and repeat recovery; the existing commit-fault matrix checks recovery atomicity. This proves the native failure mode, not attribution of any production orphan without operation-specific diagnostic evidence. Live checkpoint/materialized writes budget the reconstructed envelope and roll back atomically (`turn_settlement_regression_tests.rs`).

Derived transcript freshness: **Problem:** sealing a running Turn and then changing its content/status without invalidation left `conversation.get` stale even after reopen while exact Turn reads were current. **Decision:** `turn::store_changed_lane_turn` invalidates the main-lane archive within every lane rewrite transaction (events, lifecycle, visible phases, new attempts, compaction and recovery); conversation restore also invalidates when advancing execution epoch. Fresh tail appends retain prefix reuse. The canonical archive format is v2; v1 is an ineligible reconstructible cache and reads use authoritative Turns until the existing sealer rebuilds it. Unknown formats and current-format corruption remain errors. A v1-only engine refuses a rebuilt v2 archive; rollback therefore requires a v2-aware engine, and must never retag cached bytes as v1. **Alternatives rejected:** lane count/digest cannot prove same-count content freshness; sleeps/retries conceal durable stale data; disabling the archive removes useful bulk-read reuse. No new worker, durable receipt family or retention policy is added; obsolete page/blob versions follow existing GC. `transcript_archive_mutation_tests.rs` forces sealing before each mutation and checks full/windowed readback and reopen; archive preparation tests retain conflict and commit-fault fences.

## Online reclaim boundary

Physical reclamation runs online: the daemon's `tofu-db-online-gc` thread collects dead blocks/segments, orphan files and temporaries in two-mark epochs, without operator action and without holding the foreground authority mutex across any long operation (`docs/TOFUDB_ONLINE_GC.md`). Each epoch captures its mark inputs under an O(in-memory) hold, builds marks and scans off-mutex, and deletes only what two consecutive marks prove dead, the in-memory relink fence shows unreferenced, and the catalog still confirms. Epoch cadence has a doorbell-proof floor (2026-09-21): no trigger (reclaim doorbell, volume pressure, idle timer) may start epochs faster than the 120 s minimum interval by start time, and the collector never exceeds a 50% duty cycle — without it, backlog-driven doorbells plus multi-minute epochs on a slow mount ran the collector continuously, a self-inflicted denial of service against the foreground. `system.reclaim` is an observation plus doorbell boundary: it requests an expedited epoch and reports cumulative collector progress (`offline_required: false`, exact reclaimed bytes/blocks, per-epoch state, `statistics_complete` once the first epoch finished); it performs no physical I/O itself. The exclusive offline `collect-garbage` CLI remains the certified heavy hammer. Manifest-retired payload segments are parked, never unlinked, in the publication path: a superseded segment joins the in-memory deferred retirement set only after the CONTROL generation that drops it is durable in both alternating slots, and the physical unlink happens in a later sweep (`TOFU_DB_SEGMENT_RETIREMENT_DELAY_MS` floor) or — after a restart drops the set — through ordinary orphan reclamation, which re-derives the referenced set from the installed manifest on every epoch. No crash point of the build/publish/park sequence can therefore produce a durable CONTROL manifest that references an absent segment file. A manifest-referenced segment that disappears out of band fails closed: the affected read returns the integrity-fence diagnostic `CONTROL payload manifest references a missing segment`, mutates nothing, and stays fenced across restarts until an operator restores or salvages; the engine never silently drops the reference. Offline integrity operators have two bounded commands: `tofu-db verify` walks every durable structure read-only (CONTROL slots, selected WAL chains through the shared never-truncating scan, history manifest and segments, the payload catalog and segment indexes, the full entity graph and every loose block with content-hash verification) and reports an exact machine-readable `tofu-db.verify.v1` verdict (ok/degraded/fatal with named corrupt blocks, failed segments and torn tail bytes); `tofu-db salvage` extracts everything verifiably reachable from the newest valid CONTROL anchor into backup layout, merging the overlay first exactly like the backup CLI, skipping unreadable blocks into an explicit loss list and publishing the checksummed salvage manifest last, so the target is always restorable to a complete prefix — an unusable CONTROL pair fails closed instead of guessing. Never interpret the RPC response as a complete volume inventory.

## Snapshot read lane

Problem. Executing queries outside the authority mutex still serialized every
snapshot capture through writer FIFO admission. The preceding native array
sample attributed 76.339 µs per sampled query to that queue, versus 113.600 µs
for dispatch. Immutable pages alone did not remove read admission contention.

Decision. `entity_query_view.rs` publishes one immutable current root/overlay
view before a successful logical update releases writer authority. A query registers its own existing MVCC pin while holding the view read guard, then
copies the replica descriptor and executes the unchanged semantic dispatcher.
Publication takes the matching write guard: GC cannot pass the writer's final
fence before readers of the preceding view have registered. The mirror owns no
permanent pin, history, copied body, worker or persisted state. All four root
installation paths publish; an aborted update disables the optional view.
`authority_query_view.rs` supplies current read configuration, shared caches and the authority's worker lease clock.

Queries check current restart/WAL failure and primary mutex poison. A missing, contended or poisoned optional mirror uses the original FIFO capture path. Preparation, command certification and maintenance retain writer admission. Private replica transaction staging remains permitted; actual commit is denied. Executed queries retain scope, source validation and a registered root. [Certified result reuse](TOFUDB_QUERY_RESULT_CACHE.md) returns owned bytes after fresh admission and live snapshot/health proof, without a physical pin. Snapshot handles have no count quota, reserved writer slots or capacity FIFO. Root/GC topology remains under the registry mutex; byte ownership uses its independent checked counter. Replica leases keep their root registered until all escaped transactions and buffers drop. Published root capture remains under its read guard; writer capture remains under authority/GC exclusion. Deadline, connection cancellation, poison and restart/WAL errors keep their original boundaries. Search retains its projection lane. Runtime embed and daemon publish views; legacy serving APIs retain the authority capture fallback.


Alternatives considered. More workers around the same FIFO still serialize capture; a permanent view pin would retain an unused root and WAL history indefinitely. Fully concurrent writers expand certification without removing this measured query queue. A separate semantic read implementation duplicates ownership and sync rules. The replica persona preserves one semantic authority, shared immutable block/page caches, and reachable blob validation. Four optional stages and three counters separate lookup, pin, copy, publication, reuse and fallback. `tofudb_published_read_20261003.json` retains failed attempts and adverse cohorts. Strict native long eight-caller scalar reads improve 39.6%/41.0%; 32-caller main scalar reads improve 48.4%/42.3% (overlay/materialized). Long single-caller overlay is 9.4% slower; the SDK main 32-caller cohort is 13.2% slower, while its balanced followup is 4.8% faster. These do not establish all-metric superiority. Diagnostic array dispatch is 114.850→44.148 µs with no retained FIFO waits; scalar embedded queue time rises 4.096→38.580 µs and pin registration is 4.677 µs. The bounded collector drops 20/7,763 scalar and 1/1,663 array samples; retained spans are reconciled. Embedded calls now borrow immutable `server_bindings.rs` runtime bindings, removing per-request admission allocation and launch-budget copies. The profile boundary includes binding preparation; pin release has its own stage. Legacy owned builders retain isolated overrides. `tofudb_borrowed_bindings_20261003.json`.

## Transaction-local entity read reuse

Problem: a point-version witness avoided lost updates but repeated reads of an
unmodified key still traversed the immutable tree or WAL overlay. Decision:
retain verified values (including absence) beside that transaction's version
witness. Staged writes win; owner/authority/restart checks precede cache hits,
and commit validates the original version, including tombstones and ABA.
Validated rebase preserves cached values only because those witnesses have
passed validation; newly read commit-owned metadata uses the new snapshot.

Witness metadata and exact retained payload lengths use shared transaction-byte
accounting without an aggregate admission ceiling. Replacing a value releases
its cached bytes. Arithmetic overflow and poisoned-registry errors propagate
without dropping mandatory version witnesses. Retired quota eviction is removed.
Commit, abort and escaped snapshot-lease teardown release the remaining charge.
No new worker, persistent artifact, global value cache or resource cap is added.
Alternatives rejected: cross-transaction value caching (new invalidation and
ownership boundary), and dropping version witnesses on eviction (lost-update
risk). Verification: `entity_read_reuse_tests.rs`, entity/OCC/overlay tests and
the paired workload in `STORAGE_PERFORMANCE_CHARTER.md`.

Preparation captures and registers the current root under the authority mutex,
then evaluates independently. It uses the original request deadline and checks
connection liveness while acquiring authority. It has no separate snapshot
capacity admission, pressure hints or pin-release wait. Rotation backpressure
and post-preparation conflicts keep their existing exclusive fallback. Problem:
fixed pin limits made valid work wait for unrelated readers. Alternatives:
raising limits moves the refusal point; removing root registration breaks GC;
removing writer exclusion breaks root capture. Tests retain 512 readers while
new queries and preparations start, and preserve poison, deadlines, cancellation
and final-lease root retention.

## Authority contention watch (2026-09-21)

Problem. Every pre-existing health signal answers from observer handles
and stayed green while one holder kept the authority mutex for 13 minutes
(a thread parked in uninterruptible mount I/O inside the hold): every
foreground request died at its wire deadline, and the supervisor's
liveness recycling never fired.

Decision. A `tofu-db-auth-watch` thread probes the authority mutex with
`try_lock` every 250 ms and tracks the longest CONTINUOUS contention
window — exactly what an admitted request experiences. Past 10 s (twice
the 5 s foreground wire deadline; no legitimate online hold approaches
it) `system.health` reports the `authority` component unhealthy and flips
`ready`, with `authorityContendedMilliseconds` in health/metrics; the
supervisor's liveness path recycles the daemon inside seconds (every
engine phase is crash-safe at any interruption point). The watch also
runs in the embedded runtime.

Alternatives rejected. (a) Hold timers at every lock site: same signal,
hot-path churn. (b) A longer threshold for slow-mount checkpoints: any
hold past 10 s has already failed every request admitted during it.

## Mid-operation deadline budgets in long read loops

Problem: request deadlines were only re-checked at admission and at executor
entry. A replay that materializes per-row payload documents (`turn.sync.changes`,
`turn.events.list`) could then run for minutes behind the single authority
mutex; on a network-mounted authority every other request exhausted its 5 s
deadline waiting for the mutex and the whole authority answered
`deadline_elapsed` (2026-09-18 incident).

Decision: IR execution now derives one wall-clock budget
(`deadline_unix_ms - dispatch_now`) per admitted request, shared via an
injectable check with the long read loops. On exhaustion a loop either
truncates to a resumable prefix or fails loud, chosen by whether the
operation's contract carries a resumption cursor:

- `turn.sync.changes` truncates to the contiguous materialized prefix;
  `hasMore = last < head` stays honest so SSE replay resumes from the last
  delivered sequence. `turn.events.list` returns the short page, matching its
  existing byte-budget truncation semantic.
- `turn.sync.page` / windowed `turn.sync.snapshot` truncate the lane tail to
  the newest turns already materialized and force `hasMore` with
  `nextBeforeOrdinal` at the oldest retained turn, so the browser pages
  backwards exactly once per turn.
- Unwindowed `turn.sync.snapshot`, `turn.list` and attempt materialization
  have no resumption cursor (or must stay complete for already-returned
  turns), so they answer `deadline_elapsed` (`io::ErrorKind::TimedOut`),
  never a silently incomplete history or attempt map.
- `conversation.get`/`conversation.list` with `include_messages` hydrate each
  conversation's full main lane (`legacy_messages` over `list_values_until`);
  the check fires before each per-conversation hydration and inside the lane
  read, failing the whole operation as `deadline_elapsed` — a list cannot
  silently degrade one entry's history. Frozen-transcript history stays
  untouched: it reads only the pages overlapping the requested window.

Alternatives considered: (a) raising the client RPC deadline only delays the failure signal and cannot bound a scan whose cost grows with history; (b) a dedicated read snapshot/second mutex was rejected as a much larger consistency change than the incident justifies; (c) relying on the client's read retry was already inert (wire `deadline_elapsed` is non-retryable) and would only have queued more full scans; (d) degrading message hydration per conversation inside list responses was rejected because a partially hydrated entry is indistinguishable from a complete one on the wire. The landed follow-up: bounded-window `legacy_messages` reads only the windowed lane-index range and materializes at most `window` turn documents (the maintained lane count supplies `total`; any count/lane disagreement falls back to the exact historical full read). Unwindowed reads and `conversation.list` hydration keep the full-lane contract.

## Resumable model-context reads for large conversations

Problem: on 2026-09-28 conversation `mufmucgu8c8ljb` had 118 rendered Turns totaling about 82.6 MB (about 39.9 MB of tool rounds and 25.5 MB of segments). Executor startup failed on `turn.context.read` with `database_resource_exhausted` after a durable attempt was created, before executor binding. The native 67,104,768-byte Turn response ceiling was exceeded by the complete selected suffix; retrying the identical query or changing the configured backend cannot help.

Decision: `turn.context.read` returns a complete prefix of the selected suffix under the Turn response byte ceiling, with `nextTurnId` naming the final complete Turn in the page. The next owner-scoped request passes `after_turn_id`; native and SQL-sidecar implementations validate this cursor against the selected suffix. The application assembles the pages only while the authority-issued history proof, covered count and continuation agree, and discards the entire assembly on mismatch. It restarts from the first page at most twice: background translation/settlement enrichment can advance projection revisions while a queued executor starts. A continuation advancing beyond an already validated page cursor also invalidates the assembly. Persistent churn still fails closed with `database_conflict`; unrelated storage failures propagate immediately. Each restart reselects the continuation and suffix and recomputes eligibility, without dropping tool rounds, segment reconstruction or attachments. The response remains bounded; a single oversize Turn still fails explicitly. Display and Turn bodies remain authoritative and unchanged. Proofs exclude the unfinished target (as before), so target edits during a multi-page read require the normal attempt/revision fences at binding; no response is treated as a durable snapshot across calls.

Alternatives considered: raising the global response limit merely postpones failure as history grows and enlarges every transport buffer; stripping rounds/segments corrupts model-visible history; asking the executor to use another backend violates configured authority; transparent retries repeat a deterministically unfulfillable query. Per-Turn pages with a revision witness preserve the existing storage boundary and give bounded progress.

## Page byte budgets in long read loops

Problem: `turn.sync.changes` enforced its 8 MiB wire-page budget as a hard
`database_resource_exhausted` error. Replay callers re-ask for one fixed page
shape (the SSE route uses `limit=500`), so a conversation whose next 500 sync
events exceed the budget — agent/swarm runs embed hundred-KiB tool frames —
failed deterministically. The SSE loop retried the unfulfillable page forever
and delivered only degraded heartbeats, freezing every lagging browser
projection at its last applied state (2026-09-28 incident: Turns pinned at
the pre-start queue placeholder while the bound tasks ran normally).

Decision: the byte budget truncates exactly like the deadline budget above:
the page stops before the event that would cross the cap, keeps
`hasMore = last < head` honest, and the caller resumes from the last
delivered sequence. Zero delivered events still fails loud — a single
over-budget event can never make progress, and an empty page would read as
caught-up.

Alternatives considered: (a) a smaller fixed `limit` still breaks on fatter
events and wastes round trips on thin ones; (b) client-side recovery on
degraded heartbeats treats the symptom and doubles snapshot load during
genuine storage wedges; (c) a larger page budget only moves the wedge to
longer histories.

## Continuation document budget

Native continuation checkpoints use the shared versioned-document codec:
small records remain inline and larger records use owner-bound Blob
references. The projection retains its 8 MiB limit plus a 4 KiB wrapper.

Task-event logical JSON uses the existing 8 MiB transaction literal budget.
Payloads beyond the physical stream's 256 KiB event limit are stored with the
shared owner-bound document/Blob codec under the event index namespace; the
stream reference, content and all indexes commit together, and
list/latest/export hydrate and verify the original digest and timestamp. GC
and backup discover these documents through the entity graph; retention
pruning deletes the associated document in the same transaction. Replay pages
retain their 8 MiB output cap; large writes must fit a one-row replay
envelope before any content is staged.

Publication remains in the terminal settlement transaction, and the common
reachability walker includes these references in backup and garbage collection.
Legacy JSON records remain readable and are never rewritten; only a real
frontier advance upgrades a record, atomically. Offline snapshot import
compares logical bytes, preserving the frontier and clock across physical
codecs. Catalog validation reads at most four documents per page, bounding
decoded input to 32 MiB plus four record wrappers; callers must follow its
cursor. Crash and dropped-transaction tests cover publication,
and a real filesystem test checks Blob reachability through GC and reopen.

## Hot-data residency (probe-sized caches + startup prefetch)

Decoded entity-page hits use a shared read lock and byte-bounded CLOCK eviction;
see [parallel decoded-page reads](TOFUDB_WRITE_PATH_OVERLAY.md#parallel-decoded-page-reads).

Two engine caches decide whether a read is a heap hit or a volume round trip: the block payload cache and the decoded entity-page cache. Both live in the daemon's own heap, which a swapless host never reclaims — unlike the kernel page cache, which a pressured host evicts continuously (2026-09-22: 30s+ cold reads on a network mount while the cgroup sat at 96% of its memory limit). Their historical fixed defaults (32 MiB / 16 MiB) were laptop-sized, so on large hosts the authority thrashed: every miss left the process. Both budgets now derive from the same launch probe as the connection/frame budgets — block payloads get headroom/16 with a 32 MiB floor, entity pages headroom/32 with a 16 MiB floor. There is no fixed 2/1 GiB plateau on larger hosts; a failed probe keeps the legacy defaults via the lean fallback, and explicit `TOFU_DB_BLOCK_CACHE_MIB` / `TOFU_DB_PAGE_CACHE_MIB` overrides win over the probe, bounded per cache by one quarter of observed headroom (or its lean floor). Budgets reserve no RAM; existing eviction, integrity checks and zero-capacity teardown remain. Decoded chunk/proof capacities continue to derive from the block budget. Measurement and rationale: [cache scaling](STORAGE_PERFORMANCE_CHARTER.md#hot-cache-scaling). Right after readiness, one detached, byte-budgeted pass (`TOFU_DB_PREFETCH_MAX_MIB`, default 512, 0 disables) sequentially reads the loose blocks (hottest, most recent writes) then the payload segments into the kernel page cache, converting cold network-volume fault-in into mount readahead so the client warm-up and first user reads complete in seconds; it never blocks readiness and reads each file at most once.

## WAL durability modes

`tofu-db serve` tiers WAL durability automatically from the launch probe: when the authority volume classifies as network/fuse (`/proc/mounts` fstype, longest-prefix match; unobservable platforms report `Unknown` and fall back safe), the daemon selects `--durability normal` so mount fsync latency never taxes every command; on local or unobservable volumes it keeps `--durability strict`, the per-commit `fdatasync` barrier identical to the legacy SQLite authority. An explicit `--durability` (or `TOFU_DB_DURABILITY` through `native_launch`, whose validation converts it to serve arguments) always wins over the automatic tier. `--durability normal [--wal-flush-interval-ms 1..1000]` remains the bounded-RPO mode: the daemon acknowledges after the WAL write, a background barrier thread (`tofu-db-wal-barrier`) fdatasyncs the log through an independent descriptor outside the authority critical section, and every CONTROL publication, checkpoint, and clean shutdown still performs the deferred barrier first (`Engine::publish_control` syncs pending WAL bytes before any slot write). A crash therefore loses at most one flush interval of acknowledged commits; recovery truncates the unsynced tail exactly like a torn record, and command receipts resolve acknowledged-but-lost operations through ordinary idempotent replay. A background barrier failure fails the authority closed (restart-required). The mode is launch-level (like PostgreSQL `synchronous_commit`) and is echoed in readiness as `walDurability`/`walFlushIntervalMilliseconds`, so the automatic tier is always observable. The embedded in-process transport serves strict durability only and refuses `TOFU_DB_DURABILITY=normal` at start. Measured effect and certification evidence live in [STORAGE_PERFORMANCE_CHARTER.md](STORAGE_PERFORMANCE_CHARTER.md).

## Deferred-application write overlay (default write path since 2026-09-21)

The overlay is the default write path. The 2026-09-20 flip attempt surfaced 63 tests written against legacy-default semantics and one replay-stranding defect (fixed: generic engine records with `authority_state_update == None` no longer bound the overlay-delta replay walk); the per-test mode triage completed on 2026-09-21, the lib suite passes in both configurations, and the former sole blocker — an off-by-two orphan-scan admission bound that fired on a full catalog with an in-flight pack publication — is fixed by the 4,099-entry scan window. `TOFU_DB_WRITE_OVERLAY=0` survives only as an undocumented emergency escape hatch (`runtime_guards` passthrough), never a user tuning path. With the overlay, plain-writes commits no longer apply their entity mutations to the copy-on-write tree on the commit path: the commit records one bounded `EntityOverlay` family delta in the transaction envelope (≈6 KiB instead of ≈48 KiB per settled turn append) and installs the delta in an in-memory overlay store. Every read — foreground transactions, pinned replicas, the snapshot read lane — merges tree+overlay at its snapshot sequence. A bounded chunked merge (2,048 entries per chunk — the oldest chain positions rounded up to the boundary commit's atomic span, inside the transaction block-reference budget) applies the deltas to the tree with per-key original commit versions at the entry/byte/WAL watermark triggers (the hard commit-path backstop merges exactly one chunk per tripped commit since 2026-09-21 — never an unbounded full merge inside a foreground commit), before structural or stale-snapshot transactions, in idle maintenance rounds, and mandatorily before backup and garbage collection.

Durability invariants are unchanged: the WAL carries the delta records, recovery replays them into the overlay (deltas always follow the last root-advancing transaction; deep replay covers operator-checkpoint stranding), and the merge commit itself is an ordinary group-barrier commit, so a crash lands exactly on a pre-merge or post-merge state. OCC witness semantics are preserved: merges keep original versions, stale snapshots flush-then-verify against the merged tree, and watermark-aware resolution keeps pre-merge snapshots reading their exact pinned view until every pin advances. The search projection store opts out (a derived index served better by the synchronous path); reads still merge its overlay whenever one exists. Full rationale, alternatives, and measured results: `docs/TOFUDB_WRITE_PATH_OVERLAY.md` and `docs/STORAGE_PERFORMANCE_CHARTER.md`.

## Storage retention tiers (2026-09-19)

The authority keeps three storage tiers (hot/warm/cold):

* **Hot** — the live entity tree and overlay: current conversations, turns,
  attempts, receipts, outbox, and projection state (unbounded lifetime).
* **Warm** — owner-bound versioned documents and blobs for large payloads:
  tool results carry `MAX_TOOL_RESULT_TTL_MILLISECONDS` expiry pruned by
  maintenance; turn tombstones live seven days (`TOMBSTONE_RETENTION_MS`).
* **Cold** — raw streaming evidence: settled attempt event chunks
  (`turn.events.prune`) and aged conversation sync events
  (`turn.sync.prune`). Maintenance rounds schedule both certified operator
  prunes with a seven-day horizon (matching the tombstone horizon), each in
  its own bounded transaction; sync-referenced attempts are skipped, and
  replay for cursors older than the horizon degrades to the snapshot
  fallback (`docs/contracts/CONVERSATION_SYNC_V3.md`). Retired payload bytes become
  unreachable and are reclaimed by the ordinary garbage-collection boundary;
  prune counters ride the readiness metrics.

### Indexed-stream retention sweep (2026-09-22)

**Problem.** `event.prune` (streaming/structural task-event tiers) scanned
its whole retention range from the epoch every call. Deleted index entries
stay in the entity tree as OCC tombstones until their pages are rewritten,
so the churn-heaviest tier accumulated a tombstone field no bounded scan
could cross: a 25-row prune walked >1,024 tombstone leaves without
collecting one live row, tripped the range-witness budget, and failed
closed with `database_resource_exhausted` on every pass (996 consecutive
production failures; the backlog then grew unboundedly while each failing
pass also held a client session for its full retry budget).

**Decision.** `indexed_stream::prune` sweeps in adaptive time slices behind a durable per-class watermark: empty slices (zero live rows — tombstones never count) advance the watermark and widen ×8 up to a ~35-year cap; a slice overflowing the witness is halved toward a one-minute floor; the first slice holding live rows is the batch, with an empty-window verification pass preserving the legacy `has_more` parity. The watermark is clamped to the request's wall-clock now (entries are daemon-stamped, so later appends always land at/above it — a far-future cutoff can never leak them), resets on clock regression, and a persisted slice width is re-clamped to one hour on read so a stale wide slice cannot livelock the deadline. Every call is bounded on its scan and delete phases by a hard 5 s authority-friendliness budget, and the post-delete per-task/per-type reconciliation is bounded by a 256-entry cap — the authority-contention watchdog recycles the daemon past a 10 s wedge, and un-budgeted 45–57 s catch-up batches on the FUSE authority previously triggered exactly that recycle, aborting the sweep with it. A truncated batch rolls the watermark back to its first unprocessed row, so the cap and the budget are both ordinary `has_more` resumption. Client maintenance additionally duty-cycles every inter-batch gap (0.75 s) so catch-up work never starves lock waiters.

**Alternatives considered.** Physical tombstone compaction (reclaiming
dead pages pin-aware) is the structural cure for read-side tombstone walks
as well, but is a multi-day engine project; raising the 1,024-leaf witness
budget only delays the overflow while multiplying commit-verification cost;
single-call full-drain chaining matched the legacy contract but could not
outrun the caller's deadline on cold cache, losing the whole catch-up to
each abort.

## Public integration boundaries

Project relink visits every owner-scoped active and trash candidate without the former 10,000-count refusal. SQLite discovers stable identity windows; PostgreSQL retains one statement snapshot per table and re-reads each candidate under the existing conversation semantic lock. Native discovery follows physical windows to actual EOF. Exact project fields, unrelated settings, transcript revisions and immutable trash capsules keep their existing semantics; the owning command commits every rewrite, receipt and outbox effect atomically. Problem: directory moves were refused after fixed candidate/write counts. Decision: demand-sized preparation within the existing transaction, without partial commit splitting. Alternatives rejected: increasing fixed quotas retains refusal; separate commits leave mixed old/new paths; changing PostgreSQL statement snapshots weakens its existing concurrency boundary. Native entity staging has no arbitrary 14,336-key quota: the overlay count keeps its actual u32 encoding check, and decoding validates minimum row bytes against the stored payload before iterating. Internal range traversal accepts positive representable windows; inline/key/root codec guards remain. Tests cover 10,001 native updates/reopen, 10,001 active plus one trash SQL rewrite with owner isolation, 20,001 distinct entity writes, and the neighboring overlay/commit/GC fault matrix.

Recent-project navigation state accepts every supplied valid path and every owner-scoped project, without the former 32-path batch and 1,000-project admission quotas. Application, SQL and native paths deduplicate in request order; SQL locks remain in stable path order. SQL lists the complete owner prefix, and native lists with an exact count/lookahead witness plus physical read windows until actual EOF, without the former private list-byte bound. The shared document reader caps only initial reservation to a physical page, preventing a corrupted large count from requesting a huge up-front allocation. Problem: valid batches were refused and retained owner history became invisible beyond 1,000 rows. Decision: complete atomic updates and owner lists with checked representation/count integrity. Alternatives rejected: larger quotas retain refusal; truncating lists hides durable state; splitting commits changes replay/outbox atomicity. Tests touch 1,101 unique paths plus duplicates in one command, preserve exact replay/use counts through reopen, verify another owner remains empty, and clear the exact full count. Synthetic facade preparation deduplicates 1,201 supplied paths to 1,101 in 0.277 ms with 43,688 traced peak bytes excluding input construction and backend I/O. The malformed-count regression reports integrity failure without reserving a counter-sized list.

Repositories consume runtime/service-provided `lib.storage.port.StoragePort`; `StorageClient` (v1) and `StorageV2Client` (v2) are transport adapters, not application extension interfaces.
The supervisor owns backend selection/lifecycle; failure never selects another authority. Ambiguous maintenance failures are not replayed because maintenance may mutate state.
Independent harnesses install `tofu-db-client` from `tofu_db/`, using public `StorageV2Session`/response/error types for framing, authentication and bounded streaming without Tofu/operator imports.
Hosts supply endpoint, credentials, owner/tenant scope, lifecycle and semantic receipt policy; see [`tofu_db/README.md`](../tofu_db/README.md).
`gen_storage_operation_catalog.py` generates the schema default and Python/Rust consumers. Hello responses must correlate and select an offered schema; failed handshakes close the socket.
`tests/storage/test_storage_public_boundary.py` guards this boundary and cleanup.

### Workspace delivery authority and limits

Conversation queue items, autopilot markers and global recovery catalogs have no count quota. Queue replies have no private 8 MiB admission ceiling. `max_bytes` is a positive page-cut hint: the first eligible receipt always returns complete, even when it exceeds the hint; further items split into exact cursor-following pages. Caller row hints and byte hints remain positive and representation-checked, without an arbitrary byte range. Problem: one saved notification larger than a page hint permanently stalled recovery, and a complete queue reply could be refused despite supported storage/transport. Decision: admit the first complete item and encode the owned reply, while preserving scope, cursor, payload and real transport/I/O error checks. Alternatives rejected: truncation destroys notification content; raising fixed page quotas reproduces the refusal; skipping an oversized item loses recovery evidence. Regressions preserve two 4.5 MiB message bodies through complete native replies above 8 MiB, return each under a one-byte page hint with advancing/terminal cursors, and verify SQL recovery under the same hint plus an explicit 16 MiB window. `background_pending` returns every matching unbound completion; `background_page` accepts any positive representable row hint and returns exact keyset continuation under the complete-item byte hints and transport representation checks. A 32-row default is a window hint, not a total receipt or page-count admission limit; application recovery visits every window and grows full windows. Injection and consumed-background delivery acknowledgements accept the complete supplied ID/preview set without the former 256-entry quota; identity/type checks, exact receipt equivalence, protected or linked intent refusal and atomic event/receipt/queue mutation remain. Large native ACK regressions remove 1,025 eligible rows while preserving human intent, replay the same receipt exactly and reopen without duplicate removals. Queue request, core write and core read no longer apply the former 1 MiB byte quota; immutable message/config data uses the existing owner-bound blob path, retaining physical codec checks and the separate Transaction IR and transport representation checks. Request validation measures streamed JSON bytes without allocating a complete Debug copy. Problem: the same valid message was refused before writing or again after reopen, despite available blob storage. Decision: remove admission byte hints while preserving nonempty identities, kinds, owner/conversation/core identity and real storage failures. Alternatives rejected: raising the quota repeats the refusal policy; truncating text/config loses accepted intent; another store would split authority. Migration import/get uses the same core path without the admission byte quota and invalidates owner-local append metadata in its restoring transaction; the next ordinary append rebuilds the exact count from the complete index. Decoded core maps move into the reader result without cloning their large strings/config. The regression admits a 2,750,195-byte JSON request and preserves its 2,750,182-byte core through local commit/reopen, exact payload/config checks, migration restore/get/replay and a correctly positioned later append; shape, scope and remaining format boundaries are unchanged. Internal order-index scans use physical page windows until actual EOF, including short mount-split pages; they preserve every accepted item beyond the former 512-item and 1,000-catalog-row thresholds. Problem: the native queue rejected new intent and refused reads once its catalog exceeded a fixed count. Decision: demand acceptance with checked position arithmetic, exact owner/conversation validation, existing synthetic-kind deduplication/supersession, lease and atomic summary mutations. Alternatives rejected: increasing the fixed count keeps the same refusal policy; truncating scans would lose ordering and recovery evidence. Marker admission no longer scans the full global catalog. Measurement: admitted native requests enqueue and verify 1,100 messages, exercise lease release/finalization and continuation dedupe/supersession, then reopen and verify owner isolation in 3.601 s on a temporary local filesystem; a neighboring test retains 1,101 complete conversation/marker catalogs through clear and reopen. Ordinary real-message append (`packages/tofu-db/src/queue_append_metadata.rs`) point-reads owner-local `tofu.queue-append/v1` metadata in the existing state namespace; dispatchable item states remain tenant-global, so their keys cannot alias this metadata. The count and per-kind oldest times update in the same transaction as message, state, ID claim and changed recovery summaries. Existing queues bootstrap from the complete index when metadata is missing; goal-continuation supersession, deduplication and removal still take their complete ordered transition path, invalidating/rebuilding metadata atomically. Conversation deletion retires it even for an empty queue. Problem: scanning and sorting the entire queue for every ordinary append made a deep accepted queue expensive. Decision: a transaction-owned derived header using existing namespace/ownership, with visible corruption and checked counters. Alternatives rejected: a process cache loses coherence across handles/reopen; a separate counter service would create another authority; assigning positions from timestamps would change legacy ordering. Measurement: the same local 1,100-message lifecycle regression reads zero earlier order-index rows during ordinary append; 1,100 admitted appends take 0.698 s and the complete lifecycle takes 1.101 s, compared with the earlier 3.601 s lifecycle run. These are synthetic local observations, not a throughput guarantee. Metadata adds one small owner-local row per nonempty queue with at most the six declared kind timestamps, independent of queue depth; clear/delete removes it. Tests cover legacy rebuild, backdated summary replacement, staged rollback, reopen, owner isolation and concurrent writers refusing a stale count/position.

Integration rows plus bounded origin metadata own task binding/`delivery` intent; no second ledger or backend fallback. Workspace registration and active readiness have no owner or authority count quota. Checked counters record actual rows; ready/integrating indexes are visited one physical page at a time until a match or actual EOF, without materializing the complete active catalog. Problem: the 1,000-active/10,000-owner gates rejected durable task work and made native acceptance differ from SQL. Decision: retain ownership, checkout/project claims, revocation and atomic transitions while accepting demand. Alternatives rejected: larger fixed quotas preserve refusal; parallel publication workers would defeat project serialization. Measurement: deterministic-VFS coverage constructs 10,002 retained owner workspaces and 1,132 active rows, claims the free project beyond 1,130 project-blocked ready rows, and verifies exact counters and state after reopen in 8.730 s including fixture population. This measures synthetic catalog construction and recovery, not production disk throughput. Candidate `merged` is not canonical `delivered` (see [project integration](modules/project_integration.md)).
SQL `lib/storage_authority_sql/operation_domains/integration.py` locks `integration.checkout` by workspace path and rejects another owner/project/task's non-discarded claim. Merged history still reserves the checkout.
Native `packages/tofu-db/src/integration_workspace.rs` reserves the same checkout with a tenant-scoped SHA-256 path claim key; the value retains the exact path and workspace ID, and conflicting ownership fails closed. Live admission point-reads the claim rather than accumulating witnesses over all retained rows. Legacy initialization is bounded to 128 row locators; larger histories require the offline `backfill-integration-checkout-claims` command (explicit tenant/user, batch 1..128, application stopped), with readiness/cursor/claims committed atomically. The 3,200-row cross-reopen regression and independent-process CLI gates pass after partial entity roots gained an explicit WAL replay watermark (see [overlay recovery](TOFUDB_WRITE_PATH_OVERLAY.md)). Do not mark an incomplete backfill ready or weaken live admission to work around it. This is test certification, not evidence that an operator has migrated a running authority.
Claims compare supplied paths; caller normalization/registration and cooperative filesystem leases are distinct obligations. These records are not a sandbox or protection from non-cooperating external writers.
`integration.workspace.delivery_page` is a receipt-free internal query: ID-ordered `rows`, `hasMore`, `nextAfterId`, default 64, cap 128 plus one lookahead. SQL joins origin metadata; native scans tenant row locators and projects the same rows.
The worker rereads each row with its owner before retrying `pending`/`blocked` intent; cursor wrap/restart rescans history. Cold rows do not write. Page bounds do not imply bounded total recovery time or a pending-only index.
Native operation-catalog presence is not deployment or end-to-end certification: real-backend recovery/claim gates must pass against the selected binary. Python/SQLite tests do not certify PostgreSQL concurrency or native physical fault behavior.
Checkpoint preservation, completed-only publication, canonical branch/dirty/lease checks and crash-after-adoption ancestry reconciliation remain application Git responsibilities, not storage-page side effects.

## Native Supervisor selection (storage.v2 direct, no Python sidecar in the hot path)

Supervisor selection is a **native v2 direct path**, not a Python-sidecar translation shim:
* Repositories/services retain `get_storage_client()` → `StoragePort.query/command/health/metrics/maintenance` (`lib/storage/port.py`).
* The legacy adapter uses storage.v1; `tofu-db serve` uses storage.v2 (MessagePack+CRC32C, Hello capability negotiation, fixed owner/tenant scope). `contracts/storage_operations_v1.json` owns the shared catalog; `lib/storage/v2_semantics.py` adapts v2. Catalog membership alone does not certify payload bounds, committed notifications, recovery or maintenance: real-backend gates must pass.
* `serve_supervised` starts the owner-bound logical outbox relay, turn-search projection and maintenance/GC scheduler when tenant scope is supplied (`packages/tofu-db/src/daemon.rs`). Native batching addresses commit latency; optional [whole-authority fastpath](TOFUDB_FASTPATH.md) separately relocates native I/O.

### Logical outbox accepts durable demand

Problem: a fixed 4 MiB plaintext quota and configured record/backlog budgets refused business commits; an oversized first pending record could not be read under a smaller publication page. Decision: retain every captured encrypted record in the existing inline/blob codec and increment exact pending-byte integrity counters without a capacity refusal. Legacy configuration size arguments do not grant or deny writes. Fetch byte targets stop between records and always return the first eligible record; already-selected publication batches retain every validated record. The publication worker accepts FIFO tickets immediately without queue slot/byte admission waits. Closure, caller deadlines, actual allocation failure and sink errors remain explicit. Identity, encryption key, authenticated payload, contiguous sequence, byte witnesses, sink durability and post-publication ACK still fence progress. Sink growth and volume-reservation hints do not reject appends or reopen; real I/O owns disk-full handling. Only actual allocation/I/O failures and the existing u32 encoded-length representation can prevent storage. Alternatives: dropping old records weakens replication durability; increasing independent magic limits still strands a record at another boundary. No new worker or authority is created; unacknowledged backlog remains durable until its existing authenticated publication/ACK lifecycle reclaims it. Single-owner sinks retain small entries in the legacy inline form. Large entries use content-addressed Engine blocks in the same commit, with a 48-byte inline length/SHA-256 descriptor (`TDBSNKB1`). Recovery and idempotent retry reconstruct and verify the entire entry before matching scope/sequence/event identity; legacy inline records remain readable. No second filesystem authority or weakened WAL/sync path is introduced. Verification: 80 native outbox tests cover bad keys/scope/sequence/byte witnesses, ambiguous publication, configured-outbox semantic commit/replay, crash/short-write/disk-full recovery and carried plus noncarried block entries. A 9,437,184-byte payload becomes a 9,437,352-byte sealed record; commit/reopen/publication/retry/ACK succeeds with one-byte legacy hints and ACK leaves zero pending bytes. Publication plus retry took 456.59 ms on the test host. With a gated sink, 31 additional tickets enter in 2.37 ms; actual peak resident accounting is 10,656 bytes for synthetic small records, all 32 complete FIFO and shutdown leaves no worker or resident batch. These measurements exclude model work and do not promise large-record queue throughput.

### Search projection launch admission

**Problem:** an opened index may belong to a replaced authority at the same path or retain `ready` while source work is pending. **Decision:** daemon and embedded fence every owner before request dispatch. The existing worker validates format/budget/source UUID, schedules necessary owner-local rebuilds and refreshes authoritative backfill/dirty-work coverage before runtime-only `(tenant, owner)` admission. Before admission, queries expose no retained hits and coverage is `indexing`; validated partial rebuilds remain searchable. No foreground corpus scan or startup-wide owner walk is added. Projection-open failure fails launch rather than disabling search or deleting unfamiliar files.
**Alternatives:** persisted readiness permits stale/foreign hits; synchronous rebuilding makes startup corpus-proportional; path namespacing cannot detect authority replacement. Placement/reset/active-pending paths follow [`STORAGE.md`](STORAGE.md#settings-database-panel).
**Evidence:** `turn_search_projection::tests::startup_admission_*` covers pre-worker queries, source replacement and per-owner stale readiness. `tests/tofudb/test_tofudb_projection_relocation.py` reconstructs two owners' body/title matches after authority WAL retirement through both transports, preserves the old directory and verifies source rows on reopen.

### Projection activation continuity and durable acknowledgement

**Problem:** after A→B, B may consume edits/deletes from the shared dirty queue; UUID-only trust would resurrect A's old hits as `ready` on return. Authority commit may also precede WAL durability in Normal/Strict group-commit modes.
**Decision:** persist one 16-byte activation token per `(tenant, owner)` beside `search_backfill_checkpoint_v1`, binding the projection marker to source UUID plus token. Mismatch retires only that owner's disposable search namespaces, generates a fresh pending token and atomically schedules replacement backfill plus activation. Returning A gets a new token; pending retries are idempotent; legacy UUID-only markers rebuild once. Authority data, receipts, other owners and old index directories are not moved or deleted.
Before marker acknowledgement, query admission or source-generation publication, capture `(WAL generation, byte prefix)` under the authority mutex, release it, and wait on the existing barrier (up to five seconds per batch). The witness covers activation, checkpoint/page invalidations and pinned source snapshot; rotation durably covers old generations, never substitutes a new log offset. Failed waits remain pending/retryable, not `ready`; deferred coordinators that are already missing/poisoned fail before either store changes. A failure during off-mutex target preparation leaves that target pending and unacknowledged. Dirty-token ACK follows durable projection publication.
**Performance/alternatives:** unchanged-directory restart keeps token/index without full rebuild. Each batch reads and revalidates one owner key and waits on one usually satisfied barrier; activation adds one bounded key and 16 marker bytes, not a directory ledger. Rebuild only on activation/source/format change or legacy migration. Reject always rebuilding (corpus-proportional I/O) and path/directory-UUID identity (A→B→A remains stale). Authority lease plus existing single maintenance consumer serialize activation; multiple independent live consumers for one owner are unsupported.
**Measured work:** the 35-header (>2 backfill pages) regression performs 35 initial rebuilds, then zero rebuilds, dirty work or authority WAL growth across three unchanged-index reopen maintenance rounds; this is a work/I/O check, not a production latency benchmark.
**Evidence:** relocation integration covers body edits, title removal, new conversations, B draining work/retiring WAL, A reactivation, unchanged reopen and owner isolation through both transports. Projection tests cover no-wipe unchanged activation and owner-local legacy migration; deterministic worker tests reproduce UUID-only stale READY then verify recovery, failed barriers, pre-sync and post-sync/pre-projection-ACK crashes with dirty work, retry without new generation, Strict/Normal deferred publication, outside-mutex waiting and rotation before ACK. See [writer ownership, paired evidence and remaining limits](TOFUDB_PIPELINE_CRITICAL_PATH.md).

The native integration has three entry points:

1. `lib/storage/v2_client.py` — a `StorageClient`-interface-compatible client over v2. It does not re-implement the wire codec: framing/auth/blob streams come from the single shared implementation in `tofu_db/wire.py`; this module only adapts its session/response model to the synchronous interface and maps v2 classifications onto the stable `StorageError` taxonomy. Data operations default an omitted payload owner to the authenticated session owner (explicit values are preserved); invalid caller fields are never silently removed. `command()` unwraps the committed-event envelope and re-publishes `publish_committed_events` exactly like the v1 client. `health()`/`metrics()` prefer the daemon's authenticated `system.health`/`system.metrics` control operations (see `contracts/storage_v2.json` `controlOperations`): they dispatch ahead of semantic admission, answer from observer handles and the launch resourceBudget, and never touch the authority mutex — so they stay out of the operation catalog and the semantic differential gate. Binaries older than the control operations reject them with `unknown_operation`; the client then degrades to the previous bridge (a cheap authenticated liveness op plus the readiness-envelope budgets).
2. Supervisor `tofudb` launch mode (`lib/storage/supervisor.py`) — resolves the
   tofu-db binary, spawns `tofu-db serve
   --data-dir … --owner-id … [--tenant-id …] [--search-projection-dir …]`,
   reads the credential-free readiness line, and reuses the existing
   crash-fence/restart machinery (shutdown closes stdin, the supervised EOF
   signal). The child path resolver (`lib/storage_authority_sql/native_launch.py`)
   holds the shared project lease across exec, publishes a credential-free
   native locator, and prevents SQLite and native authorities from running
   simultaneously.

   Bootstrap is fail-closed and never a silent SQLite fallback. When the
   authority directory is entirely absent — a true fresh install — the
   launcher runs `tofu-db init` once and serves the new empty authority;
   when it is present at all (migrated, partial, or corrupt), startup
   refuses and points at the offline migration, because initializing over it
   would silently discard recoverable evidence (`tofu-db init` itself
   refuses a non-empty directory, so an interrupted bootstrap stays
   fail-closed).

   Engine supply paths (newest first): a **bundled binary** inside the frozen
   application (PyInstaller's `sys._MEIPASS`, then the executable's own
   directory, using the platform-specific name), the source checkout's
   `packages/tofu-db/target/release/tofu-db`, then `PATH`. A valid
   `TOFUDB_BIN` override is authoritative and never falls back when invalid.
   The drift pin is declared, highest authority first, by
   `TOFUDB_EXPECTED_SHA256`, then by a `<binary>.sha256` sidecar next to the
   selected binary; with neither declared, development resolution applies no
   drift check. Malformed pins, changed files, and mismatches fail before
   spawn; this check is not a signature or hostile-filesystem fence.

   **Decision (2026-09-17): the pin travels with the artifact.** Problem: a hand-maintained `.env` pin and the deployed binary were upgraded in separate places and one out-of-band deployment desynchronized them, crash-looping the next restart. Decision: `scripts/deploy_tofudb_engine.py` is the ONE supported engine-upgrade path — it installs the candidate, writes the sidecar pin and a `build.json` provenance record atomically, rewrites any stale `.env` pin (the env pin outranks the sidecar), and re-verifies before reporting success; a mismatch error names the binary, pin source, provenance, and the exact re-pin remedy. Alternatives rejected: auto-adopting a certified binary after a mismatch (weakens fail-closed), silently falling back to the checkout build (runs an unselected engine), and dropping the pin (it detected the desync). The content pin proves artifact identity only, not semantic compatibility after the contract moves. `python3 scripts/check_tofudb_engine_drift.py` is the behavioral companion: it resolves the deployment binary exactly like an application restart, launches it against a disposable authority, and replays the contract probe set (semantic round-trip, online-reclaim shape, owner-scoped write/read). Run it after every contract-affecting change; `--binary PATH` certifies a candidate before deployment. **Decision: prepare the matching engine before stopping the application.** Problem: content pins and advisory Git checks allowed GUI restarts to keep running an old engine. Decision: `--build-info` embeds a SHA-256 over the crate manifest, lockfile, build script and Rust sources; source installs require that identity on every launch, including dirty trees and non-Git source archives. `serverctl_pkg/native_engine.py` prepares native engines during GUI update, restart, managed startup and the cold-boot storage phase: verified matching release download first, isolated source build second, disposable semantic probes before publication. Preparation is serialized and bounded; a failure retains the old serving worker and reports an actionable UI error. Explicit external binaries/pins remain authoritative; SQLite/PostgreSQL never trigger native preparation. Preparation relays download/build/probe output through the CLI subprocess and reports elapsed/remaining time every five seconds without output; lock waits identify the competing preparation. Its shared 600 s work deadline (630 s outer guard), 420 s compiler and 90 s semantic-probe budgets remain bounded. Each command keeps at most an 8 MiB output budget and an 8 KiB failure tail in an owned temporary log, removed on exit; the nested CLI/build capture can hold two such logs, with no resident reader thread or persistent log added. Timeouts retain diagnostics, cancellation reaps the command and terminates remaining compiler-group members even after the leader exits. The outer cancellation grace is 15 s so the worker can finish its 5 s compiler cleanup. Matching engines skip downloads/builds; command waits wake on exit rather than imposing a polling sleep on every identity check. Alternatives rejected: silent captured builds (indistinguishable from a hang), unbounded output, and stopping the old worker before preparation succeeds. Search-projection writable probes retain their private temporary filename through flush/close, then unlink it: unlink-before-write fails on some FUSE mounts even when ordinary engine writes are supported. Probe cleanup removes only its own file, never an existing projection or authority. Alternatives rejected: advisory-only freshness, deployment-time Git HEAD as build evidence, and stopping before compiling. Daemon-only releases use the daemon in auto transport; explicit embedded mode requires a verified library pair. Pre-built release packages ship the engine inside the bundle (declared in `tofu.spec`, built per-platform in `build-desktop.yml`), so package users need no extra dependencies. Source installs run a single command: `install.sh` optionally builds the engine with `cargo build --release` when a Rust toolchain is present (skipped silently otherwise; `--no-tofudb` disables it), because until tofu-db is the default backend the engine is an accelerant, not an install blocker. `runtime_guards.DeploymentConfiguration.storage_backend` gaining a `'tofudb'` value is the selection switch; SQLite/PostgreSQL keep the existing sidecar path, and `tofudb_transport` selects the daemon or embedded transport (below). The migration/rollback authority (`lib/storage_authority_sql/migration/tofudb_migration_import.py`) streams its declared domains and remains the only supported import path. Its passing fixtures do not prove full production-table coverage: trash graphs, child-scoped archives and sync state still need complete adapters and independent verification before application cutover; preserve the source authority until that verification succeeds.

### Transport selection (`TOFUDB_TRANSPORT`, default `auto`)

With the native backend selected, the supervisor's transport knob chooses HOW the application reaches the one native authority — never a second authority or engine. `auto` (default since 2026-09-23) resolves at launch from the authority volume class (`lib/storage/volume_class.py`, mirroring `resource_probe.rs`): `embedded` everywhere except observed network mounts, where it resolves to `daemon` — there the engine's automatic durability tier is Normal (bounded-RPO background flush), which the strict-only embedded runtime refuses, and per-commit network fsyncs under Strict would be the latency storm the tier exists to avoid. `embedded` dlopens the same build's `libtofu_db.so` (the sibling of the resolved, pin-verified binary) and runs `packages/tofu-db/src/embed.rs` in-process: the identical `server::dispatch_one_request` pipeline, strict WAL barrier, search projection, maintenance scheduler, background packer, rotation, archive-seal, and online two-mark GC / online backup drivers, minus the listener (measured: `record.get` ≈26 µs embedded vs ≈78 µs pooled-daemon). `daemon` spawns `tofu-db serve` as today; both explicit values remain supported overrides. The launch contract is unchanged either way: the same engine resolution and content pin, fail-closed bootstrap, project lease and locator (the launching PID owns the authority lifecycle), and health preflight; the embedded runtime's budgets and component health come from its authenticated `system.health`/`system.metrics` control operations instead of a readiness line, carrying the same probe-derived resourceBudget as the daemon. The client (`lib/storage/v2_embedded_client.py`) inherits the v2 client's classified retry, deadline, and committed-event discipline verbatim — the daemon-shaped envelope is the only framing difference — and opens NO socket. Honest limits of the embedded transport:

* **Crash = process crash.** There is no child to respawn; the supervisor's
  health monitor still fences engine-reported unhealth through the ordinary
  crash callbacks, and graceful stop runs the same WAL-window retirement
  (`abandon()` is the harness-only SIGKILL approximation).
* **Strict durability only** (`TOFU_DB_DURABILITY=normal` is refused at
  start) — which is exactly why `auto` keeps network mounts on the daemon.
* **Single-process.** The app holds the exclusive lease directly; no second
  process (daemon included) can co-serve or health-probe the authority.

FFI hardening contract (`packages/tofu-db/src/embed_ffi.rs`): every entry runs under `catch_unwind` — a contained engine panic becomes the standard `embedded_transport_error` envelope plus one `panic_containment` JSON stderr line, and a panic in open returns no handle. Executes run on the handle's demand-created detached workers with launch-derived idle reuse under an execute watchdog whose wait budget is the request's own remaining `deadline_unix_ms` + 30 s grace, clamped to [120 s, 1 h]; a timeout means a wedged (not crashed) engine, so the handle is poisoned, one `embedded_watchdog` line is logged, and the abandoned worker can never write the caller's buffer (results travel back by channel). A poisoned handle fails every later execute fast with the same error code, and close/abandon free its memory without running engine operations on the unknown-state engine (a wedged worker leaks one bounded runtime, at most once per handle). All semantic request payloads share the 64 MiB aggregate ceiling, enforced at serialization; the FFI envelope permits another 64 KiB. The framed transport chunks requests above `MAX_PAYLOAD_BYTES` (8 MiB minus frame slack) regardless of operation, including queries with null command IDs. Semantic admission still requires command IDs for mutations and validates the catalog, ownership, schema and deadline.

Problem: an artifact-only streaming whitelist rejected large agent checkpoints,
turn projections and raw archives before dispatch, making identical retries fail.
Decision: frame size determines transport chunking; semantic operation admission
is the sole operation gate, with the existing aggregate memory reservation.
Alternatives rejected: adding individual operations to the whitelist repeats the
failure for every new payload owner; dropping persistence loses resumable state;
removing memory admission makes concurrent requests unbounded.

Parallel preparation, queue budgets and verification boundaries are specified in
[the write-path contract](TOFUDB_WRITE_PATH_OVERLAY.md#parallel-preparation-and-embedded-execution).

**Decision (2026-09-21): transport is a sibling knob, not a backend value.** Problem: a third backend value would share the tofudb authority while pretending to be its own backend — breaking the one-authority-location-per-backend contract and doubling every `== 'tofudb'` gate. Decision: `TOFUDB_TRANSPORT=daemon|embedded` rides alongside `TOFU_PERSONAL_STORAGE_BACKEND=tofudb`; `storage_backend` stays `tofudb` because the authority, engine, and migration story are identical. Alternatives rejected: a third backend value (above), and direct unsupervised opens in application code (they bypass readiness, the project lease, and crash fencing). Certification status: harness-verified transport, the full application contract (`tofudb_embedded` param in `tests/storage/test_storage_application_contract.py`), and supervisor integration (`tests/storage/test_storage_v2_embedded_client.py`).

**Decision (2026-09-23): `auto` is the default transport.** Problem: the certified embedded transport was opt-in, so production kept paying the loopback tax; a naive embedded-everywhere default would force Strict per-commit fsyncs on network mounts. Decision: `auto` resolves at launch from the authority volume class (`lib/storage/volume_class.py`, drift-pinned to `resource_probe.rs` by `tests/storage/test_volume_class.py`) — embedded everywhere except observed network mounts, which keep the daemon's Normal durability tier. Alternatives rejected: embedded everywhere (the network-fsync storm); daemon-by-default (the ≈41→78 µs per-op tax); an engine CLI query at boot (a process spawn for a 1 ms /proc read). Certification: unchanged from the paragraph above — `auto` only changes which certified transport starts; fixtures in `tests/storage/test_transport_resolution.py` and `tests/deploy/test_deployment_configuration.py`.

Private `migration.conversation.restore` imports header identity, settings,
timestamps and logical revision atomically with its owner claim and covering
index. Replay rejects any source-header difference. Turn and frozen-page imports
reconstruct message counts without advancing that revision or timestamp;
importing old content is not a new conversation event (plan v17 binds the
source revision).

Private `migration.command.receipt.restore/get/scan` operations preserve a receipt's explicit tenant/owner, hashed command key, request digest, exact encoded response and original commit time. Restore uses the live atomic receipt/blob/family publisher: identical retries perform no commit, divergence fails, and ordinary command lookup returns the historical response without repeating its mutation. Receipt bounds are 4 MiB stored and 4 MiB decoded and 7 KiB inline; catalog pages contain only bounded identities. These operations require an explicitly established source scope. They do not infer ownership from an operator's identity or rewrite request digests. Live replay accepts both the exact v2 digest and the historical ownerless spelling: the storage.v2 client injects the session owner into every payload while the v1 authority hashed the payload exactly as received, so a migrated receipt written for a caller that omitted `user_id` still resolves instead of reporting command-ID reuse. Acceptance stays fenced by the (tenant, owner, command ID) receipt key and the operation name; new receipts store only the v2 spelling. Native snapshot support alone does not certify the source-wide receipt mapping, which remains outside the admitted v27 import domains.

Native legacy-global receipt primitives use the distinct reserved identity in `offlineMigration.legacyReceiptImport`, separate from existing tenant-global operations and user receipt scopes. One source UUID, receipt count and stored byte total publish atomically with the existing receipt/blob family. The profile bounds this historical state at one million receipts and 1 GiB of encoded responses per tenant; identical restores preserve counters, while source identity changes, divergent rows and quota overflow fail closed. Before ordinary command replay or mutation, an existing legacy identity requires an identical operation/digest and an explicit numeric `user_id` in the exact hashed request payload matching the authenticated owner. Missing ownership, changed payloads and reserved-system callers receive conflict with no historical response or mutation. The guard also applies to commands that now use receipt-free natural idempotency. The private operator operations `migration.legacy.receipt.restore/get/scan/stats` require an explicit tenant and source authority UUID. The custodian is not assigned as historical owner. Catalogs enumerate sorted 32-byte keys in pages of 128 with source-bound cursors; get preserves exact stored snapshots and stats exposes atomic row/byte accounting. The source importer does not yet map these tables, so operator support alone does not make legacy source receipt tables eligible for cutover.

The offline source reader's `digest_key_rows` and `digest_key_point` support
32-byte BLOB primary keys, including WITHOUT ROWID command-receipt tables.
Pagination and point lookup compare raw bytes through the primary index;
canonical lowercase hex is only the returned identity/checkpoint spelling.
Malformed widths/types/cursors, unbounded sort plans and oversized rows fail
closed. Returned hex expansion counts against the ordinary source-row budget.
Abandoned iterators release their cursors, and all pages share the source's
pinned snapshot and committed-change detection. This reader capability does
not establish receipt ownership or certify native receipt migration/replay.

`tofudb_migration_receipt_source.audit_receipt_source` verifies both global
source formats without assigning owners. It preserves the live resolver's
refusal of a command identity found in both formats, even when their contents
are identical; migration must not silently coalesce these rows. Raw response
codec validation, exact request digests and commit clocks feed separate source
hash chains. Work is capped at one million rows and a configurable 60-second
default deadline (300-second hard ceiling), using indexed digest point reads
and no all-receipt identity set. Exhaustion or malformed input returns no
successful partial admission. This remains a source-only audit; replay scope
and target restoration must be separately certified before covering the tables.

Plan v27 archives the explicitly retired table inventory in `tofudb_migration_retired.TABLES` under the owner-scoped generic record namespace `migration.retired_sqlite.v1`. These rows retain historical state; they never recreate a scheduled task, timer, login account or current project setting. The retired `users` table is a different username/password schema from current `tenant_users`; archive restoration preserves its old hash and identity without adding an authentication principal. Old `schema_meta` and `scoped_sequences` likewise remain historical evidence, not live schema or counter overrides. The explicitly retired `pricing_cache` and `rate_limit_events` also retain all original values and timestamps without replacing the current generic pricing record or reactivating old limit counters. Current limits migrate separately from `storage_rate_limit_events`. The explicitly selected operator owner is their custodian, not an inferred historical owner. Each document records its table, complete column list, primary-key columns and typed cells. Integer decimal strings, hexadecimal floating-point strings and BLOB hex preserve SQLite values without JSON coercion. Envelope version/time are fixed archive-format sentinels (`1`); original timestamps remain in cells. The record key binds the table and SHA-256 of the typed original primary key. Source use of the reserved archive namespace, unknown columns, oversized rows or targets with different/extra data fails closed. Archives are durable user history, have no automatic expiry, and participate in ordinary native backups. Operator `migration.record.scan/get` reads them independently of live tasks.

Admission bounds are 50,000 archived rows, 2 MiB per encoded record and 512 MiB
total encoded data. Source admission runs before binding/target mutation. The
checkpoint retains its 64 KiB file bound with at most 128 progress domains;
each retired table owns a separate verified prefix. Reopening verifies every
published prefix and an independent target catalog before completion; missing
verified rows cannot be silently recreated.

Source `storage_meta` uses the same typed historical archive with explicit
per-key admission in `tofudb_migration_metadata.POLICY`, included in the plan
digest. Source authority UUIDs must parse, schema versions must match the
required SQLite schema, raw-archive byte counters must be unsigned 64-bit
decimal text, and a present Project Brain cutover marker must be `complete`.
Unknown keys fail before target writes because they may own replay or
projection state. These values are provenance: native authority UUID/schema
remain native, and native archive counters are recomputed from imported blobs.
The original counter is preserved even if it differs from that recomputation;
it is never trusted as a native quota or accounting input. Source metadata
therefore participates in resume binding and exact independent archive checks.

Large retired rows listed in `tofudb_migration_retired_large.TABLES` use `migration.retired_sqlite_parts.v1`. This inventory covers old conversations, messages, turns, attempts, task results, transcript archives and queued messages. These remain inactive history under the explicit archive custodian. Every column retains its SQLite type and raw bytes, including embedded NULs. Cells over 1 KiB are split into 256 KiB parts; manifests bind length, SHA-256 and part count. Parts publish and verify before the manifest. Exact orphan parts can resume an interrupted unpublished row; missing parts beneath a published manifest fail verification rather than being repaired. Independent catalogs reject foreign rows and extra parts, including parts beyond the source length. The generated `retiredLargeArchive` profile admits at most 65,536 source rows, 64 MiB per source row, 1 MiB per manifest, 1,048,576 target records and a conservative 32 GiB encoded archive allowance. Metadata and byte slices use the pinned source reader without loading whole cells; ordinary document limits remain unchanged. These durable archives have no automatic expiry. Profile admission is not a disk-space or stopped-source cutover certificate.

The retired `conversations_fts` search index is admitted only when the pinned
source proves the exact known `fts5(search_text, tokenize='unicode61')` layout
and SQLite reports its virtual/shadow ownership. Its complete `_content`
table, including original row IDs and any search-only text, uses the large-row
archive; its complete `_config` table uses the small typed archive. Only the
virtual view and `_data`, `_docsize`, `_idx` structures are then classified as
reconstructible index state. `verifiedRebuilds` records this separately from
direct mappings. Same-name ordinary tables, omitted input mappings, unknown
DDL and unreadable provenance fail closed. This never activates the retired
search path: current native search belongs to migrated current conversations.

Retired trading tables instead require a pinned-source duplicate proof against the current `storage_plugin_rows` authority, which must itself be mapped along with `storage_plugin_manifests`. The registered `legacy_scan` declarations own the table/column/key inventory. Every source column and the complete row multiset must match; a historical migration receipt alone is insufficient. Missing rows, extra columns, duplicate copies, malformed documents or exhausted proof budgets leave populated tables blocked. The proof admits at most 256 manifest operations and 50,000 rows on each side, retains only SHA-256 row digests, and uses the ordinary bounded source-row reader. It deletes nothing and is repeated inside the actual import snapshot. `verifiedMirrors` records the proof separately from direct table mappings; source coverage remains distinct from target fidelity and stopped-source cutover certification.

Turn bodies are read through exact-PK 256 KiB chunks while ordinary metadata rows retain their 8 MiB admission limit. Source projection/settlement bytes share a 64 MiB ceiling; decoded native snapshots reserve 64 KiB beneath 64 MiB for envelopes. One exclusive operator-session buffer uploads or reads one Turn snapshot, mutually excluding event/frozen uploads. Uploads require exact offsets or identical chunk replay and a SHA-256 check before atomic publication; failed finish, abort and EOF release the buffer. Read snapshots carry length/hash receipts and release their buffer on the final chunk. Turn materialization and ordinary streamed get/list/delta responses use the Turn-specific bounds from `turnSnapshots` in `contracts/tofudb_ir_v1.json`; normal write/IR admission stays at 8 MiB. Aggregate responses still fail at their declared ceiling rather than omit Turns. Source binding hashes original body chunks; independent verification reads complete native snapshots.

Plan v19 decodes the private storage projection codec for active and trashed
Turn imports through one source conversion. Tool arguments and result content
must match the public materialized projection after native import and reopen;
raw source bytes remain part of the binding. Before source binding, admission
rejects retained checkpoint rows, active patch heads, or malformed head metadata.
The source must settle/recover those attempts and materialize their projections
before offline import; an inline placeholder is never a complete snapshot.

Plan v18 also restores Turn deletion markers without changing parent revisions.
It preserves exact owner, conversation/Turn identity and deletion time, publishes
identity claims and both delta/retention indexes atomically, and verifies the
physical owner catalog independently. Source-deleted attempts contribute no
native attempt pins. Replay rejects changed timestamps, occupied identities and
inconsistent indexes; ordinary seven-day retention continues after cutover.

Plan v17 imports recoverable trash before active-domain verification. The private
`migration.trash.*` adapter admits one pending capsule per owner, at most 65,536
Turns and an 8 MiB stage document. Header plus stage publish atomically; exact
source header/Turn/page verification precedes a durable seal. A separate commit
pins that sealed root and retires active ranges with the stage marker, preserving
source deletion time, revision and display count. Pending stages refuse daemon
readiness; catalogs expose pending identities and completion requires none.
Resume finishes trash before checking active catalogs; pinned Turn catalogs,
retained counts, retention indexes and frozen pages are independently
verified, and source composite-key prefix reads stay owner-scoped. This does
not certify remaining durable domains or cutover.

Plan v17 preserves frozen pre-Turn conversation transcripts through private
`migration.frozen.transcript.*` operations. Original source-byte hashes bind the
codec-bearing source to decoded public message pages. Pages normally target
4 MiB; a whole oversized message may occupy its own page up to 64 MiB minus
64 KiB. Each transcript admits at most 1,024 pages, 65,536 messages and 1 GiB of
decoded page data. One volatile page-upload buffer accepts contiguous or identical
replayed 256 KiB chunks, verifies its SHA256, and publishes a page plus directory
in one commit. Abort, failed finish and EOF release the buffer; the private
operator excludes simultaneous event/page buffers and wire caps stay fixed.

A complete manifest is published only after independent page enumeration and
content/count verification; incomplete history is unreadable, and resumed
checkpoints revalidate native page catalogs and content.
Conversation reads fall back to these exact messages only when no main Turns
exist, preserving window indices and counts; message-bearing conversation
lists share the page byte ceiling (metadata-only lists keep 8 MiB).
Delete/restore includes manifest and page ranges in existing persistent
snapshot pins; clone reuses owner-bound blob references. Native JSON parsing
enables serde_json `float_roundtrip` so fractional historical timestamps
retain the source binary float.

A read-only production audit decoded all 5,439 conversation archives without
errors: 4,544 were nonempty, containing 34,282 messages in 5,105 pages and
5,696,596,486 decoded bytes. Maximum observed page size was 62,880,840 bytes;
per-conversation maxima were 10 pages and 1,163 messages. Of these conversations,
4,539 have no main Turns and depend on frozen history. This audit and fixture
passes do not certify a stopped-source full-authority import or application
cutover; remaining unmapped durable tables still require their own adapters.

Root compaction archives use `migration.compaction.archive.restore/get/scan`: exact public messages (including legacy compression decoding), optional post messages, summary, receipt, timestamp and source payload byte count survive an insert-or-match import. Identity, chronological index and count publish in one native transaction. Detached history is retained; live reads still require an active owner-scoped conversation. Nonempty child archive scopes still fail closed at import (their source inventory needs a certified adapter), but the live authority serves them end to end: `invocation.register_child` and `invocation.resolve_child` port the immutable birth evidence with the same parent fences, generic task-result writes are fenced by the mirrored header identity, and all six compaction archive operations carry `archive_scope` through the claim, per-scope chronological index/count and retention layout — with exact scope filtering and the legacy `_archive_scope` child check. Decoded transcripts are capped at 8 MiB each. Shared document-domain verification retains one row, bounds catalog pages, and re-enumerates the target even for a completed checkpoint; a source-only point-read pass cannot certify a target with extra rows. The 4096-document resume regression enforces a 2 MiB Python verification peak (source fixture and precomputed checkpoint excluded), independent of total document bytes.

Terminal attempts use `migration.attempt.restore/get/scan` before Turn import:
exact command, configuration, error, timing, queue and timestamp facts publish
atomically with identity, command, per-Turn directory/count and retention indexes.
Detached history requires explicit source ownership from a conversation, Turn,
trash entry or tombstone. Live attempts are refused. Terminal Turn pointers are
retained only when their imported attempt belongs to that Turn. The independent
owner catalog pages at 128 rows and completed-checkpoint verification rejects
extra target attempts.

Attempt events use `migration.attempt.event.restore/get/scan` after Attempt
snapshots. UTF-8 text/BLOB event JSON bytes and timestamps survive exactly;
native byte accounting derives from the restored bytes. Event envelope identity
must match the source columns and imported terminal Attempt. Each event and its
maximum-sequence head commit atomically; retention gaps remain gaps. Independent
128-row catalog pages detect extra target rows and invalid heads. Ordinary reads
still require an active owner-visible conversation and Turn. Historical event
documents have a separate 32 MiB ceiling; event pages stop before 64 MiB minus
protocol headroom and use the existing storage.v2 response chunks. Ordinary
document and frame budgets remain unchanged. Source-wide migration throughput
and fixed-snapshot checks remain necessary before production cutover.

Offline source `blob_chunks` reads one exact-primary-key TEXT/BLOB through a read-only SQLite incremental handle, at most 256 KiB per chunk. Callers declare the total domain byte ceiling; ordinary 8 MiB row admission remains unchanged. Iteration exhaustion or explicit iterator close releases the handle. Event binding and checkpoints hash metadata plus streamed source-byte manifests. Offline upload begin/append/finish/abort owns one volatile 32 MiB buffer per exclusive child; decoded chunks are at most 256 KiB. Offsets require contiguous append or identical replay. A matching length/SHA-256 is required before atomic publication, while abort, EOF and failed finish release the buffer. Native chunk reads report independently recomputed byte hashes; oversized ordinary operator gets return a bounded error without killing the child. The 25 MiB stream regression requires Python peak allocations below 8 MiB and the Linux child RSS high-water mark below 512 MiB. The isolated production-event probe retained all 25,294,288 bytes across native reopen with matching SHA-256, about 2.43 MB Python peak and 97.1 MB native peak RSS; this proves that record's round trip, not full-authority migration or cutover readiness.

Raw archive reads advance the compressed input offset between inflate windows, reject streams that stop making progress, and preserve strict end-of-stream, trailing-byte and 64 MiB decoded-part checks. The native per-conversation archive ceiling is 1,792, covering the observed 1,734-record source conversation while reserving transaction capacity for counters and outer lifecycle work. A boundary test creates the maximum, rejects one more, then atomically removes the archive set and verifies zero counts. Owner count (65,536), 4 GiB tenant storage, and 16 MiB per-attempt byte ceilings remain in force. Plan v14 imports raw archives through private `migration.raw.archive.restore/get/scan/stats` operations. It preserves compressed bytes, metadata, missing-part flags and detached history; quota overflow refuses the restore instead of scrubbing imported content. The live parent fence and quota behavior remain unchanged. Each restore atomically publishes all four covering indexes, identity claim and usage counters; exact replay compares the entire snapshot. Completion independently enumerates the owner catalog and compares owner/tenant totals and source-grouped conversation and Attempt counters, including when resuming a completed checkpoint. Source aggregation streams rows with SQLite temporary work restricted to disk. Ordinary source-row and operator-frame ceilings still apply; larger compressed archives fail closed and require a chunk adapter before their source can be certified. A read-only production preflight converted 58,856 archives without errors. The largest compressed archive (2,062,761 bytes) survived an isolated native restore and reopen with exact metadata/bytes and matching usage; Python peak allocation was 8,255,291 bytes and native peak RSS 45,748,224 bytes. This evidence covers that archive and conversion preflight, not a full production import.

Schema 62 adds missing browser observation hint counters to authorities created
before the version-58 table definition grew those fields. Existing counters and
observation data survive; absent counters start at zero because historical rows
cannot establish hint usage. The ordered additive migration is restart-safe on
SQLite and PostgreSQL and does not infer counts from successful visits.

## Client and application contract ownership

`tofu_db.wire` is the independently installable storage.v2 transport owner; `scripts/tofudb_v2_client.py` is only a compatibility import. The wire generator writes committed-envelope constants into `tofu_db/generated_v2.py`. Application owner injection, bounded cleanup progress and broker-topic translation belong to `lib/storage/v2_semantics.py`, generated from `applicationSemantics` in the same protocol contract. Maintenance mutations never replay after ambiguous connection loss. Optional client CRC acceleration never compiles during import or blocks a request on compilation. One worker per process and an exclusive per-key file lock bound compiler concurrency; a five-second deadline and an exit hook kill the compiler's process group. The owner-checked, ABI/source-keyed cache holds at most eight entries, admits 16 KiB source and 2 MiB libraries, and scans at most nine root entries/four entries per key before declining unsafe or excessive state. Compiler output stays private until atomic rename; stale staging is reclaimed before another build. Checksum fallback preserves framing when acceleration is unavailable. Native reachability/data durability never depend on this reconstructible client cache.

Native event records use the IR contract's integer `v: 1` envelope; legacy
SQLite rows remain unversioned. Cross-backend tests verify this outer version
before comparing event content, preserving all nested producer fields and
rejecting unknown versions. Executable native inventory must exactly match
the application operation catalog; an unfinished implementation does not make
an undeclared operation available.

## Application completion and backend certification

Terminal settlement stores the Turn, Attempt, replay event and model
continuation atomically. A continuation's logical 8 MiB ceiling is not the
entity inline limit (8 KiB): `model_continuation` uses the common owner-bound
versioned document/blob codec. Reads accept historical inline JSON; advancing
writes upgrade it within the settlement transaction. Migration verification
compares materialized logical records, preserving frontier, timestamp and
projection bytes. Blob references participate in normal backup/GC traversal.
Deterministic commit faults must preserve the old or new complete state even
when the continuation crosses the inline boundary.

Durability and notification are separate obligations. Each native transaction
coalesces content-free conversation key/sequence notices under
`storage_v2.json:committedEvents`. The transaction owns them; a successful
commit returns them explicitly to the response dispatcher. Rollback and receipt
replay cannot leak an earlier request's hints. The budget is 1,024 keys of at
most 2,048 UTF-8 bytes each; admission fails before commit if it exceeds that
bound. The application adapter maps the generic conversation topic into its
owner-scoped broker contract. Hints carry no transcript content and never
replace durable cursor replay; the standalone client remains independent of
Tofu's push bus. Browser stream/abort gates cover normal delivery.


WAL recovery reuses byte-verified payloads in the published segment catalog.
Carried payloads with no valid catalogued copy are staged from WAL without
probing loose files. A membership filter alone never permits discarding the
recovery copy. This prevents read-only daemon restarts from repeatedly packing
the same carried bytes. The process gate checks preserved values, unchanged
WAL bytes and stable file inventory across consecutive reopens; it does not
depend on whether maintenance or the adoption driver publishes a pack.

**Startup recovery — Problem:** a missing whole WAL suffix can end at a valid record boundary while CONTROL still acknowledges later records. The previous open discovered this only after expensive payload replay, and application boot retried the deterministic failure six times. Identifier-only WAL references also repeatedly searched segment indexes outside the carried-block lookup. **Decision:** scan the acknowledged WAL prefix before opening payload catalogs; clean EOF below CONTROL is an integrity failure just like a corrupt record. Keep failed-open evidence intact and report checkpoint/durable/recovered sequences. Native `storage.error` startup envelopes carry a bounded diagnostic, code and Boolean `retryable`; the supervisor preserves this verdict, and boot does not retry explicitly terminal failures. Lease contention remains retryable. Search-index open errors identify the projection separately from the authority.

Recovery's existing transient lookup (at most 65,536 IDs) covers carried and identifier-only references, the history manifest and authority root. One verified catalog traversal builds membership, exact recovery locations and the existing budgeted routing cache. Excess entries retain verified fallback; every payload read still validates its bytes. No durable format, cache budget, acknowledgement rule or selected authority changes. On the affected FUSE volume, a read-only-source/write-overlay benchmark of the same authority reduced engine open from 30.3–30.9 s to 9.6 s, segment opens from 30,517 to 4,106 and index bytes read from 7.42 GB to 0.97 GB. This measures engine recovery, not complete application readiness or a cold-cache guarantee. The missing-suffix projection failed in 0.057 s before any payload reads. **Alternatives considered:** increasing caches hides repeated I/O and costs resident memory; retrying unchanged corrupt files cannot repair them; lowering CONTROL loses acknowledged state. A damaged reconstructible search projection can instead be retained and a new directory explicitly selected through the existing projection-settings authority, leaving user data untouched.

**Checkpointed opens — Problem:** even an empty active WAL paid the full
payload-catalog recovery scan. Deferring that scan alone also moves its cost
onto the first application read on catalogs with heavily overlapping hashes.
**Decision:** when the checked WAL contains no records, resolve the history
manifest and authority root with verified point reads, without preparing the
WAL recovery lookup. Until packed membership is built, try a verified loose
copy first; transient I/O failures still propagate. Nonempty WAL recovery keeps
its complete validation and bounded lookup.

`payload_hints.rs` owns optional positive block-ID → segment-UUID hints learned from successful reads and the already-required verification of newly packed indexes. This also covers pages packed during clean shutdown, before their first disk read. Every hit must intersect the current CONTROL-selected catalog and pass the ordinary segment metadata, CRC and content-hash checks. A hint cannot prove absence. Missing, stale, checksum-invalid or unreadable hints fall back to the canonical lookup. Engine teardown atomically replaces one bounded `payload-read-hints.cache`, with one bounded scratch file; cache publication failure never changes CONTROL/WAL or fails shutdown. This derived file needs no fsync and is neither backup data nor a recovery authority. Admission is FIFO, at most 65,536 entries, using 1/16 of the probe-sized routing budget at a conservative 192 bytes per entry. The encoded file is at most 3,145,772 bytes; the launch budget can shrink admission to zero or grow it and reload saved entries. No worker or persistent user-data format is added.

On the affected FUSE authority in a private write overlay, checkpointed engine open took 0.007 s without a payload-segment read; application-authority open without learned hints took 0.934 s. Opening and reading the first conversation catalog took 4.831 s while learning its locations, then 0.392/0.121 s on two reopens with saved hints (95 segment opens, 20.23 MB index/payload reads versus 4,093 opens and 956.82 MB). With hints also learned during pack publication, the first reopen after normal checkpointing opened the authority in 0.033 s and read the catalog in 0.123 s total; the following reopen/read took 0.111 s. Existing interrupted Turn/task recovery still took 7.852 s including open. These are storage/first-read measurements with OS caches uncontrolled, not complete application readiness or cold-device claims. **Alternatives considered:** skipping WAL checks weakens durability; moving all work after a ready signal hides first-use latency; authoritative cached negative results could conceal live blocks after publication. Positive verified hints improve repeated startup while retaining conservative cold-cache reads.

The HTTP conversation catalog uses `Cache-Control: private, no-cache` on
both 200 and 304 responses. Its ETag permits validated reuse; a positive
freshness lifetime would allow a pre-create list to become the only catalog
loaded after immediate browser reload. This policy applies to every backend.

Certification has three distinct layers: raw wire differential scenarios,
`tests/storage/test_storage_application_contract.py` using real application producers
against both authorities, and `tests/e2e/test_e2e_smoke.py` in real Chromium.
The browser gate requires durable terminal state and no waiting indicator
after reload, not merely visible generated text. Run it separately with
`TOFU_PERSONAL_STORAGE_BACKEND=sqlite` and `tofudb`; use disposable test data.
Set `TOFUDB_BIN` and `TOFUDB_DIFFERENTIAL_BIN` to the same candidate executable
when certifying a deployment. A failed differential build must fail the gate,
never fall back to a stale executable. A successful Hello/health probe proves
liveness, not application behavioral compatibility.

Offline restore validates and leases its backup source, then acquires the
target authority’s ordinary process lease before creating restore pins or
markers. Both locks are nonblocking; the target lease remains held through
CONTROL and encryption-witness publication. A locked target is refused without
leaving a backup pin. Unrelated target entries are rejected before pinning,
without changing user files. Invalid backup layouts leave an empty target untouched.

Rust cross-process release tests accept `TOFUDB_CERTIFICATION_BIN` as an absolute
path to the same frozen candidate. An invalid explicit path fails without
falling back to Cargo’s rebuilt binary. Record its SHA256 before and after the
run; Cargo build outputs alone do not pin release evidence.

Rust cross-process release tests accept `TOFUDB_CERTIFICATION_BIN` as an absolute
path to the same frozen candidate. An invalid explicit path fails without
falling back to Cargo’s rebuilt binary. Record its SHA256 before and after the
run; Cargo build outputs alone do not pin release evidence.

The repeatable gate is `python3 scripts/certify_storage_backends.py --binary
packages/tofu-db/target/release/tofu-db`. It pins both native consumers to one
SHA-256, rejects skipped/xfail/deselected coverage, missing required cases,
executable replacement or changing verification sources, and includes
abort, reload, multiple turns, new conversation, settings and pending-tool
browser journeys on both databases. The required scenarios and uncovered claims
live in `contracts/storage_harness_certification_v1.json`. Use `--report-dir
NEW_DIR` to retain node inventories, phase outcomes, source fingerprints and
the machine-readable summary. A green report certifies only that named profile. Native `cargo test` remains the separate
physical durability and fault-injection gate.

Operational coverage is separate from conversation certification. `debug/inspect_conversation.py --api-url http://127.0.0.1:15000` can inspect native conversations through the running application's authorization boundary; use `--api-token-env NAME` when authentication is required. This bounded read-only mode does not open storage or enumerate all archives, and by default cannot prove local-log ownership; an explicit operator-supplied `--logs-dir PATH` opts into the same bounded, redacted log scan for a verified local directory. Automatic file discovery remains SQLite-only and refuses native selection; never inspect a retained SQLite fallback as the active native store. The scheduler's `system.backup` RPC is implemented natively as a control operation (`tofu-db.system-backup.v1`): the daemon's `tofu-db-online-backup` thread first drains the write overlay in certified bounded chunks (one short `try_lock` hold per chunk; contention or an over-budget drain defers the epoch as routine `WouldBlock`), then anchors a checkpoint under one short authority hold — at most what foreground commits appended during the anchor race, where the pre-2026-09-21 single-hold full merge could keep the authority for minutes on a contended mount. The epoch then copies incrementally off-mutex into the backup directory (default `<data>/backups/tofudb`, disjoint from the authority), publishes the checksummed snapshot manifest last, and prunes to two retained generations; registered anchors join every online-GC mark, so the copy can never lose a block mid-epoch. The RPC rings the doorbell and waits on the connection thread for the next epoch receipt (`ok`/`backup`/`manifest`/`bytes` plus sha256, durable_sequence and retention fields); it never touches the authority mutex, and failures are retryable `database_unavailable`. Cross-process tests prove scheduler-shape receipts, incremental copy, two-generation retention, foreground continuity, and restore-into-fresh-authority equivalence; the offline backup/restore CLI remains for operator-driven recovery.

Certification-gap status as of 2026-09-21: every claim is covered. The long-duration soak completes 120/120 one-minute windows with 7.97M operations and zero errors on this FUSE host (the wedge class — catalog exhaustion, unbounded merge chunks, continuous-collector contention — was root-caused and fixed by catalog-pressure consolidation, adaptive overlay-merge chunk sizing, doorbell-proof collector pacing, and packer segment sizing/backoff); engine memory stays inside the probe-derived budgets, which scale with the host by design (on 8 GiB-class hosts the same launch probe derives the lean profile, and the lean fallback is unit-tested). Power loss is covered on each supported volume class by the filesystem certification matrix (`certify-filesystem-matrix`, 14/14 cells across both real mounts and the at-rest variant, 2,273 fault cases including the correlated lost-sync power-loss sets, evidence archived under `data/benchmarks/`); large migrated-corpus equivalence is covered by the migration gate's digest comparison over a 36,097-document corpus (billing 3,097, credential 3,000 across 30 owner/tenant boundaries, record 30,000 — every domain digest-matched after the replay path was fixed to surface quota conflicts instead of swallowing them, and both credential authorities now admit complete owner/tenant catalogs with differential demand scripts verifying keys beyond the retired 1,000-row quota); P99 performance evidence now clears the contract's own 1,000-tail-sample floor (medium scale: mean superiority stable across repeats, p99 competitive with documented mount-weather variance and every background write stream quiet-gated). The sole remaining uncovered claim is the long-duration soak under an 8 GiB host budget.

## Tofu-DB pre-authority work
`packages/tofu-db/` is a certified, selectable Rust storage backend (admitted by the Supervisor backend allowlist; not the default backend). The real VFS explicitly unlocks an exclusive lease when its owning file guard ends, matching the deterministic VFS; duplicated/inherited descriptors cannot extend that lifetime, and an unsuccessful lock attempt never unlocks another owner. Decoded `storage.v2` requests now cross an explicit authenticated session boundary: a zeroized 256-bit Hello capability is checked in constant time against an in-memory SHA-256 witness before a session can exist, owner/tenant scope is fixed for that session, negotiation happens exactly once, and every bounded response preserves correlation plus the negotiated schema. A transport-generic connection loop requires Hello first, emits a negotiation ACK, distinguishes clean EOF from truncation and shares exact RAII buffer ownership across connections. Raw frame buffers use launch-derived RAM thresholds with private anonymous-file overflow; cumulative frame bytes do not reject requests (see `STORAGE_NATIVE_SESSIONS.md`). The loop keeps two socket budgets distinct: once a frame's first byte arrives the rest is owed within the 30 s mid-frame I/O bound (a wedged peer dies errored), while a lapse waiting for that first byte only marks an idle session and re-arms until the 300 s session-idle budget lapses (`maximumSessionIdleTimeoutMilliseconds` in the wire contract caps its configuration); idle retirement closes at a frame boundary, so a pooled client observes a clean EOF instead of a reset mid-request. The application client's session pool stays younger than any daemon build's idle policy (20 s freshness cap) and probes an idle socket for a queued peer close before lending it out; a frame-boundary close surfaces as retryable `database_unavailable` whose same-command-id replay is fenced by the durable receipt exactly like `deadline_elapsed`, while a mid-frame close stays a fail-fast `database_protocol_error`. Application calls claim exclusive sessions on demand; the pool size bounds only idle retention and no long-request quota remains ([session lifecycle](STORAGE_NATIVE_SESSIONS.md)). Semantic admission precedes authority acquisition, so invalid or already-expired work never waits behind storage I/O; admitted work uses only its remaining request/operation deadline to acquire the mutex and returns `deadline_elapsed` without executing when that budget expires. Its numeric-loopback-only acceptor creates connection workers on demand with no active count quota, fixes every connection stack at ≤2 MiB, and holds the authority mutex for one admitted request. Before opening the authority, the pre-authority `serve` command performs one bounded probe of process-affinity/cgroup CPU, host/cgroup memory capacity and headroom, and authority-volume free bytes; missing observations select four initial connection reservations and a 16 MiB frame budget; observed pressure reduces that initial reservation hint to one, while observed CPU and memory derive larger initial-reservation/idle-reuse targets (16 per core, bounded by headroom at 16 MiB per reservation), the legacy connection value is an initial-reservation/idle-reuse hint bounded at 512; frames remain bounded at 128 MiB, and observed free space below two WAL windows plus 16 MiB refuses writable startup. It then derives and clears its bounded environment secret, emits one credential-free readiness line containing only aggregate budgets, and exits after its inherited empty stdin pipe reaches EOF. Its first slice owns an exclusive process lease, two alternating 4 KiB
BLAKE3-checked `CONTROL` slots with checkpoint/manifest/active-generation witnesses, immutable 4 MiB content-addressed blocks, and one
CRC32C/BLAKE3-chained active log capped at 64 MiB. Above the 60 MiB soft limit, commits let partial history encoding and its live packer finish, then materialize toward an 8 MiB foreground target and defer checkpoint until every staged payload (including reserved or completed-but-unpublished background payloads) is durable. Single/group appends that would exceed 64 MiB finish the complete checkpoint before writing, preserving the recovery ceiling even with a large backlog. The checkpoint condition wait checks the creating state under its mutex, with at most 240 × 250 ms before synchronous fallback; `activeWalRotation` in the native IR contract owns these bounds. After durable checkpoint publication, the existing background packer deletes the old WAL and persists its parent directory outside the authority mutex. One fixed slot retains at most one retired 64 MiB generation; the next checkpoint drains it before another rotation, and delete/sync errors retain backpressure without changing the already published checkpoint result. Worker absence keeps synchronous cleanup; shutdown atomically drains/unregisters and surfaces failures. After the listener returns and every driver joins, a graceful stop additionally merges the overlay and checkpoints the active WAL window on the main authority and the search projection, so the next open replays only an empty generation instead of the full pre-shutdown window; every failure there is logged and swallowed (the worst case is the status quo replay) and a hard kill never reaches the checkpoint. Reopen derives only the immediate predecessor from CONTROL without scanning, and this handoff does not discover pre-existing older orphans. Checkpoint packs active transactions into immutable ≤4 MiB history segments and publishing their manifest plus a new empty WAL through `CONTROL`. History preparation starts at one full segment and hashes/copies toward 256 KiB per foreground call; indivisible larger records may exceed that target within the 4 MiB format bound. One ≤4 MiB partial encoder or pending history block shares the existing staging allowance. Checkpoint finishes any partial encoder and durably publishes all pending blocks before retiring their source WAL; worker absence resumes synchronous preparation and drain. The exact segment bytes and hash validation remain unchanged.
History loads only for an explicit snapshot. History maintenance retains a complete segment suffix through one v2 manifest and same-WAL `CONTROL` republication; the existing low-priority worker keeps 16 segments after a lean probe or 64 after a complete probe, requires a current Entity authority root, trims checkpointed history even while the active WAL contains committed transactions, first catches CONTROL up to that durable tail before publishing the smaller manifest, and preserves active WAL bytes/generation, the checkpoint anchor and staged history prefixes without forcing a checkpoint or rewriting transaction payloads, and leaves retired immutable segments to the online two-mark collector (or the offline GC). GC mark admission retains at most 65,536 pending references even after spill-budget or I/O failure; a full pending run must flush before further admission, and successful retry preserves its marks across crash. Explicit authority GC refuses live MVCC handles, first stream-matches at most 20 million loose-directory entries while retaining two paths and removes at most one strictly named, size-bounded `.new-*` block temporary before marking, then marks current Entity/capsule/semantic/history/active-WAL references with on-volume spill bounded by `min(1 GiB, 2% free)` or a 64 MiB lean fallback, republishes unchanged state before referenced-file deletion, and deletes at most 65,536 loose blocks/256 MiB per repeatable round. It then scans at most 4,097 payload-directory entries and removes at most one strictly named, size-bounded, manifest-unreferenced complete or temporary segment; the 4,096-entry manifest ceiling plus the two in-flight background-publication slots guarantees a 4,099-entry scan window always exposes an orphan, while unknown entries fail closed. When no earlier orphan wins, it inspects one generation-selected catalog window of at most 16 segments and rewrites or retires at most one. Segment liveness intersects exact block IDs across all hash partitions with one bounded segment index and one decoded mark run at a time; catalog buckets are scheduling metadata, not liveness partitions. Only after no reclaimable victim remains may it pack at least 128 reachable loose blocks, excluding IDs already present in any manifest segment through a bounded negative filter and exact validated index membership without building a catalog-wide ID set, skipping mark-run reads for empty loose-block shards, and fitting both the 65,536-block/256 MiB segment bounds and the launch-derived temporary budget; sub-threshold shards are not rewritten. An executed no-victim shard advances the durable CONTROL cursor so repeated rounds cannot starve later shards. Partial segment rewrites, but not fully dead retirement, must fit the temporary-space budget. Its existing-authority-only operator command defaults to a non-deleting plan and requires `--execute`; periodic reclamation is the online collector's domain (`docs/TOFUDB_ONLINE_GC.md`), and the offline session remains the certified exclusive boundary for deep operator-driven passes.
Opening recovers only the selected active generation; a bad tail before its durable witness fails closed. Referenced blocks sync before the log, missing active references fail closed, and failed `CONTROL` publication makes the live authority restart-required before further authority work.

Entity reference commits carry a pending authority-root payload only once when its block ID is already in the transaction reference list. Replay restores referenced payload slots before resolving the authority-root witness; roots outside that list retain their separate carried slot. This reduces WAL bytes by exactly the root payload size per such commit without changing the 8 KiB page-pack target, durability barriers, or verified-read-only cache admission.
Payload-segment reads use an index-derived negative membership filter; every positive probe still requires the exact block ID and payload CRC32C/content-hash verification. The native IR `payloadSegmentReadCache` contract bounds combined resident index/filter allocations at 64 MiB, resident segments at 4,096, allocated recency slots at 8,192 and each filter at 128 KiB. Access-history compaction preserves exact LRU order before allocation growth, and segment removal invalidates its cache entry. The filter is reconstructible memory and does not change any persisted format. The verified block-payload cache charges vector capacity against its existing launch-derived byte cap and additionally bounds entries at 65,536 and allocated recency slots at 131,072; zero capacity disables all population and releases both payload and metadata allocations.
New entity pages allocate their exact encoded length and transfer the allocation into existing WAL-carried staging, which retains no spare capacity; content verification and publication barriers stay unchanged. Copy-on-write pages and rebuilt root directories stage through a fresh-content entry point that skips the durable dedup probe: staging stays idempotent through the pending-set entry API, the commit's WAL envelope remains the durability point, and a coincidental durable duplicate costs WAL bandwidth only, never correctness. Pages target 4 KiB and retain the 16 KiB format ceiling; internal splits keep at least two children and merge or redistribute a small tail within that ceiling, including maximum-size keys. Adjacent modified leaf runs repack through one page buffer while retaining tombstones/OCC versions and untouched block references, keeping the 5,000-row tool-result cleanup compact while block references follow the actual transaction encoding. Packing bounds derive from the native IR contract. Batched entity COW roots use owner-scoped MVCC/OCC witnesses and commit every new page reference; every transaction pins its exact immutable root in a per-open database-instance registry, registers every live handle without a count quota, exports content-free handle/distinct-root/oldest-sequence/retained-byte metrics, releases on handle drop, and rejects handles crossing a reopen boundary. Each transaction retains every point and range witness needed for OCC without a count admission quota, and reuses a point witness on repeated reads. The registry conservatively accounts point keys, worst-case range leaf witnesses, and staged key/value writes across every live transaction under a 160 MiB process hard limit; pressure is retryable resource exhaustion and transaction drop releases its entire reservation. Authority-global persistent root pins use reserved owner-scoped identities and a transactionally maintained count record: catalogued create/remove admission performs bounded point reads, scales to one million durable pins, and an absent count may scan and upgrade at most 64 legacy pins. Pin, count, and business changes publish in the same OCC commit; reachability verifies the declared count, while malformed records, forward references, empty roots and reserved identities fail closed. A pin may capture a capsule of at most 64 sorted non-overlapping ranges: complete old subtrees are shared, boundary leaves are filtered, mixed-level fragments receive unary COW wrappers, and only the capsule root plus newly built pages enter the commit, so backup and GC never traverse unrelated authority pages. A transaction may retire at most 64 sorted non-overlapping key ranges under an exact current-root witness; wholly covered subtrees are detached without reading them, only boundary paths are repacked, overlapping writes fail before commit, and the capsule plus retirement publish atomically. Restore publishes a versioned root directory with at most 64 sorted non-overlapping range mounts, normally writes one directory block regardless of capsule size, merges mounted data into point/range reads, and gives base writes and tombstones precedence; repeated retirement clips mounts and may publish an explicit empty base instead of a synthetic tombstone. Explicit lazy consolidation atomically replaces one mounted prefix with the same logical rows in base, advances or removes that mount, handles at most 999 rows/8 MiB per transaction, and collapses an exhausted directory back to a direct root; it is operator/maintenance work and never runs during open. After readiness, one 512 KiB-stack low-priority worker round-robins at most 64 explicit scopes, attempts one transaction per probe-derived 250 ms or lean 1 s interval, never waits for a busy foreground mutex, and exponentially backs empty authorities off to 60 s; terminal maintenance errors stop daemon admission. Roots without mounts remain direct B+Tree pages, so normal open still reads one bounded root node. Explicit reachability walks stream the Entity page graph and root directories without a database-sized visited set, reject child level and key-range transplants, and cap work at 20 million pages with an 8,192-node DFS frontier; only the current `CONTROL` graph declares persistent roots, while incremental backup copy, restore and retention GC traverse all declared graphs. Blobs use 1 MiB deduplicated zstd chunks, while fenced streams use immutable 2 MiB segments and bounded cursor pages. A versioned family-transaction container canonically combines an entity root, multiple stream commits, one command receipt, one logical outbox record and at most 2,048 deduplicated block references under the existing 256 KiB inline bound; entity/stream recovery remains compatible with original single-family records. The synchronous pre-authority coordinator binds transaction handles to one authority UUID, stages entity/stream/blob work, publishes exactly one family transaction, and advances all in-memory witnesses only after durability. Receipts use the existing domain-separated SHA-256 command key and 200-byte ID, 64 KiB stored/4 MiB decoded response limits; small results stay in the entity index, larger encoded results use an atomically referenced blob, and operation/request mismatches or concurrent first delivery conflict before a second business commit. The native logical-record codec uses AES-256-GCM with random 96-bit nonces, exposes only an eight-byte key fingerprint, binds owner/sequence/schema/request routing fields as AAD, limits clear payloads to 4 MiB, and rejects metadata/ciphertext transplantation. When configured, the authority requires exactly one outbox capture for each business transaction; owner-scoped OCC metadata assigns continuous sequences, applies pending-byte backpressure before blob staging, stores records above 4 KiB through atomically referenced blobs, and deletes only the next record through an idempotent ACK transaction. Pending reads materialize at most 64 records/8 MiB. The publisher validates a whole contiguous single-owner batch before side effects, performs sink I/O without borrowing the authority writer, accepts only identity-matched durable receipts, and leaves lost acknowledgements pending for idempotent retry; counters contain no user content. The native owner-bound sink reuses Engine CONTROL/WAL durability in an explicit absolute directory, holds an exclusive lease, reserves two active-log windows at capacity preflight, rejects gaps and cross-owner records, and resolves old retries with one exact active lookup or one bounded 4 MiB history-segment read. One isolated worker runs sink I/O and accepts owned batches without queue slot or byte settings. Metrics count queued plus active residency; deadlines do not cancel potentially durable writes, shutdown stops acceptance and drains to an explicit deadline, and ambiguous sink state terminally fails queued work. An automatic relay round-robins every unique explicitly authorized tenant/owner scope through the aggregate queue, releases the authority mutex during sink I/O, commits ordered identity-matched ACKs, accepts foreground wakeups, and uses a bounded fallback interval; retrying scopes cannot starve healthy scopes, while ambiguous authority state and thread panics fail closed. Arbitrary I/O errors, ENOSPC, short writes, and lost syncs during sink append plus capture or ACK failures recover only a complete prefix. Every WAL path, including pre-published reference commits, poisons the live authority on an ambiguous append failure until reopen recovery selects the prefix. The engine durability primitive admits at most 64 transactions and 8 MiB each of total logical payload and encoded WAL per group, then uses one WAL barrier and one final-sequence CONTROL publication; every member remains independently framed for unacknowledged-prefix recovery. Its single background sequencer prepares hashes/envelopes on submitting threads, waits no more than 1 ms and never groups more than 64 requests or 8 MiB; the coalescing window closes immediately once four requests are queued, so loaded writers pay one barrier instead of the residual window. The sequencer accepts prepared owned transactions without count or residency admission quotas. `enqueue` returns a durable completion ticket; `submit` waits for that same result. WAL group limits split accepted work across barriers without rejecting it, and queue/group/commit/failure metrics report actual work. Problem: slot and byte quotas stalled callers or rejected valid transactions before the writer ran. Decision: demand acceptance with checked residency accounting, fallible queue reservation, FIFO durability and joined shutdown. Terminal failure transfers each original pending queue to its error fan-out without allocating another queue-sized collection. Alternatives rejected: raising fixed quotas retains the same refusal policy; extra writers would violate the existing authority order. Measurement: the deterministic-VFS regression accepts 1,100 prepared transactions with 36,325,290 accounted resident bytes in 32.631 ms, then drains across WAL groups, joins the writer and verifies every block after reopen. This measures synthetic acceptance and lifecycle behavior, not production disk throughput. Explicit incremental backup copies only missing reachable blocks, publishes a checksummed snapshot manifest last, and uses bounded capacity preflight plus durable restore pins to resume verified restore; plan-backed retention GC preserves pinned generations, spills up to 33.4 million marks within 1 GiB, and deletes blocks in shard batches.
The multiplexed native sink routes every explicitly frozen source scope into one Engine authority, without an owner-count quota. Nonempty, nonzero, unique owner/tenant identities and exact persisted administrative/source authorization remain required; the configuration codec verifies its u32 count against actual encoded bytes before allocating. Problem: the relay and sink independently refused scope sets beyond 64. Decision: preserve all declared scopes and keep existing per-owner sequence/fairness/ACK behavior. Alternatives: a larger constant moves the failure; silently omitting scopes can strand committed outbox records. An explicit administrative scope owns its COW exact-record index, per-owner sequence witnesses and aggregate capacity counter in the same transaction; records above 4 KiB reuse content-addressed blobs, and reopen uses at most 64 bounded point reads instead of a history scan. A shared deterministic VFS drives end-to-end commit faults. The explicit `certify-filesystem` operator command requires an empty persistent target, exercises the real VFS through lock, immutable-block publication, group commit, checkpoint rotation, a destructor-free child exit, and two cross-process reopens, and retains the resulting store as evidence without touching normal startup. Its contract-owned 1 MiB payload is packed by the no-destructor child through segment fsync/rename, manifest and double CONTROL publication, and loose reclamation; the parent and final child must both recover the catalog and read the exact payload through segment random I/O. Its machine-readable result records each process/reopen wall-clock observation and a 4,096-entry-bounded retained file count/length for controlled same-volume comparisons; these observations are neither release certification nor a performance claim. `contracts/storage_v2.json` is the machine authority for protocol v2 field IDs and bounds. The Rust codec implements canonical flat MessagePack with a big-endian length prefix and CRC32C suffix, explicit nonzero correlation/deadline/owner/schema identities, nullable tenant/command fields, Hello version/schema negotiation, mutually exclusive success/error responses, and command-bound 1 MiB blob chunks. It rejects oversized declared frames before allocation and exposes guard-based read/write byte admission. Its mandatory default-deny semantic admission binds decoded requests to generated metadata for all frozen operations, matches request identity against authenticated owner/tenant scope, validates negotiated schema and deadlines, and requires command IDs for every command and maintenance request independently of receipt policy; the pre-authority loopback listener accepts one identity-bound streamed artifact or task-result checkpoint request at a time, requires every chunk to repeat a positive total-length witness, reserves that total once before an exact-capacity allocation, and admits at most 64 ordered chunks/64 MiB under the shared frame budget through dispatch; successful responses above 1 MiB use at most 64 identity-bound chunks/64 MiB followed by one empty success terminator, and the Python harness validates and transparently reassembles that sequence without constructing a second chunk list or final response copy.

Immutable payload compaction packs at most 65,536 blocks across hash partitions/256 MiB payload behind a sorted index of at most 3 MiB. Its canonical CONTROL-referenced catalog admits 4,096 segments, selecting point-lookup candidates by full block-ID ranges. Compaction durably writes the segment and a forced-loose manifest root, publishes that root, installs the bounded reader, republishes the same root into the fallback CONTROL slot, and only then removes loose victims; any error before stabilization leaves those victims intact. The explicit authority planner scans at most 16 catalog segments per round, excludes already packed IDs and requires 128 live loose files before packing. An exclusive authority GC session admits at most 4,096 plan/execute calls, shares stable-data marks, dynamically protects the current payload-manifest root, rejects pinned snapshots and excludes business writes/checkpoints/history retirement. Retained marks and replacement files share the existing temporary-space budget; when both cannot fit, checked mark cleanup precedes rewriting and the next round rebuilds marks. Errors poison reuse; explicit finish surfaces cleanup errors and drop cleans best effort. No cache crosses a session boundary. Completion requires every unchanged catalog window to be checked; publication resets coverage and plans never advance it. The execute-only CLI batch accepts 1–4,096 rounds and 1–600,000 ms (default 60,000 ms), checks time between bounded rounds, and streams round metrics plus explicit complete/round-budget/time-budget status; without batch flags it remains one round. The daemon additionally runs the two-mark online collector of `docs/TOFUDB_ONLINE_GC.md`: one dedicated thread, mark inputs captured under an O(in-memory) authority hold, marks built off-mutex over the current root plus every transient pin root plus the history/WAL projection, deletion limited to suspects two consecutive marks prove dead with a ≥120 s epoch floor bounding staged-write straddle, an in-memory relink fence (16,384 IDs, overflow defers) catching every reference-without-write decision between snapshot and deletion hold, off-mutex segment rebuilds published through the single physical lane under bounded holds, and best-effort discard only when an error proves no publish happened. `system.reclaim` validates the legacy request bounds, requests an expedited epoch, and reports the collector's cumulative progress with `offline_required: false`; the response performs no physical walk and stays latency-bounded for any graph size. Bounded logical retention continues independently. The exclusive offline GC command remains available for operator-driven deep reclamation. Normal open reads one at-most-4-MiB catalog block, loads indexes only for a matching point lookup, and never scans the segment directory. Payload catalogs use CONTROL layout v2 while current readers accept zero-extended v1; older binaries reject v2 rather than ignore a catalog whose loose blocks may already be reclaimed. Incremental backup reads through the installed catalog and restores portable loose blocks. Index, payload, syscall, short-write, lost-sync, fallback-slot and cross-reopen faults fail closed while preserving committed references. An otherwise idle existing maintenance round publishes at most one completed background pack only with spare catalog capacity, without merge or WAL checkpoint; foreground contention defers it. Worker exit reclaims its still-completed unpublished segment before releasing registration, preserves an in-progress authority publication and all staged/WAL source bytes, and surfaces cleanup errors. Published/discarded pack counts and discarded bytes are bounded aggregate diagnostics. Abrupt-exit orphan files remain the explicit GC responsibility.
The generated baseline `contracts/storage_operations_v1.json` freezes all 351 operations at
schema 59 / registry 40 without copying SQL or handlers.  Existing operations
retain handler-enforced ownership until their owner keys and constraints move
into the future Schema/Transaction IR. The catalog generator also owns the sorted Rust metadata projection used by semantic admission, so catalog drift fails before dispatch code can compile against stale classifications.

This slice tolerates any one silently lost file/directory sync and is now certified for acknowledged durability under arbitrary device sync violations across the commit+checkpoint barrier set: the deterministic matrix enumerates every single dropped fsync/fdatasync/dir-sync, every pair, and the full correlated set (all barrier syncs dropped in one crash window), and each case must recover a complete prefix (the whole acknowledged group or an earlier one) or fail closed — never a torn group. Remaining outside this certification are device faults other than a dropped sync (a device that silently reorders or corrupts written bytes, or reports them durable before any sync), plus extended rotation window faults across chained WAL generations, but still lacks block encryption. Each v2 transaction envelope carries a tri-state entity-root update and every successful authority commit publishes the resulting current root in checksummed `CONTROL`; normal entity open reads and verifies only that content-addressed root page, descendants verify lazily on accessed paths, and locally generated commit pages no longer trigger a whole-tree scan. Legacy slots require only one exact bounded transaction lookup. Lost-ACK recovery, group commit, checkpoint rotation and incremental backup/restore preserve the same root witness, and legacy CONTROL and backup manifests decode with an explicit unknown-root state. Stream appends atomically persist their cursor and immutable segment references inside that root; normal Authority open performs no historical stream rebuild, while bounded predecessor lookup and forward traversal load at most 1,000 events and 8 MiB on demand from the caller's MVCC snapshot. The generated Schema/Transaction IR contract compiles or routes operations across these domain slices: both `browser.site_observation` operations, all six `compaction_archive` operations, `system.schema_version`, `system.reclaim`, `rate_limit.record_and_check`, all five `daily_cost` operations, both `log_aggregate` operations, all ten `optimizer.proposal` and `optimizer.action` operations, all eleven scheduler task and poll operations, all twelve timer definition, active-feed, progress, and poll-ledger operations, all seventeen queue item, lease, reap, injection-acknowledgement, and autopilot-marker operations, all nineteen orchestration definition, run, event, and Goal-run operations, all eight durable swarm session and agent-checkpoint operations, all sixteen integration workspace, event, status, and global worker operations, all nineteen `project_brain` projection, active-work, work, narrative, checker, decision, watch, cursor, rebuild, owner-scoped recovery, and format-native cutover operations, `conversation.activity_dates/clone/create/count/delete/get/list/metadata.update/purge/restore/search/settings.update/trash.prune`, `turn.append_settled/create_pair/attempt.bind/attempt.claim/attempt.create/attempt.dispatch_worker/attempt.dispatchable.list/attempt.get/attempt.start/branch.create/branch.delete/cleanup/compact/delete/event.record/events.list/events.prune/exists/get/image.get/list/list_delta/perception.record/projection.update/queue.activate/queue.cancel/recover/related.announce/revision/steer.commit/sync.changes/sync.page/sync.prune/sync.snapshot/timing_trace.get/timing_trace.list`, three `desktop.egress_agent` operations, `record.get/list/put/delete`, and `event.append/append_batch/list/latest/bounds/inspector_summary/prune`, plus `project.recent.list/touch/touch_many/clear` and all six `provider` operations, all seven `worker_job` operations, and all nine `model_routing` operations, and all eight `task_results` operations, plus all seven `tenant.user` account operations, all eleven `credential` operations, and all fifteen `billing` operations, with version CAS, prefix range witnesses, bounded blob overflow and atomic receipt/outbox effects. The schema-version query returns generated frozen metadata rather than inspecting a physical backend. Activity-date projection scans at most 10,000 compact owner-scoped candidate records and 100,000 aggregate 24-byte main-lane timestamp records under the same snapshot, retaining only distinct interval ordinals per conversation; a completeness marker is established only from an empty active-and-trash owner scope, while markerless legacy candidates and completely absent legacy Turn timestamps use bounded compatibility reads and partial marked indexes fail closed. Seven wallet/ledger operations atomically maintain immutable entries, exact global ID/reference claims, wallet balances, O(1) user sum/count aggregates, descending bounded indexes, and active-reserve projections; checked signed arithmetic and exhaustive fault injection preserve one complete money prefix. All eleven credential operations use exact-verified tenant-global ID and secret-hash indexes, an exact 1,000-live-row owner/tenant count, a descending created-time index, blob-capable settings, and compact mutable state; authentication/touch witness bound account state without rewriting settings, public projections redact hashes, and revocation retains identify-only hash tombstones. Settled Turn ingestion uses OCC-protected lane head and exact-count records to allocate monotonic ordinals and commits the Turn/blob document, lane and update indexes, non-human attempt, conversation revision/main-lane count/timestamp/index, search invalidation, sync event, receipt, outbox, and tenant-global Turn/attempt ID claims as one authority transaction; those claims serialize cross-owner collisions without widening ordinary owner reads. `turn.delete` resolves at most 2,000 requested and branch-lane descendants before writing, rejects pending/running attempts, then atomically removes Turn/attempt/index state, repairs exact lane and conversation counts, and writes revision- and age-ordered tombstones plus one sync event. Lane heads remain monotonic across deletion gaps, new Turns select the latest surviving predecessor, and tenant-global identity claims remain fenced through tombstone retention to prevent delete/recreate ABA. Each delete prunes at most 256 owner-scoped tombstones older than seven days through the age-leading index and releases only claims whose stored owner matches. Deterministic syscall-error and short-write injection verifies recovery to one complete append or delete transcript/index/count/tombstone prefix. Settled projection replacement uses projection-revision CAS, preserves typed stale/in-progress failures, and atomically rekeys the blob-capable document, lane/time indexes, conversation revision, search marker, outbox and a deterministic compact `turn.patch` replay event; exhaustive commit fault injection admits only the complete pre-update or post-update state. Branch create/delete uses the same projection-CAS path, derives a command-stable UUID-shaped lane identity, bounds nested deletion to 2,000 Turns across 256 lanes, and atomically publishes descendant tombstones with the parent update under one conversation revision and one compact patch event; empty branches remain deletable. `turn.get/list/exists/revision/attempt.get` remain owner scoped; `turn.list_delta` scans a bounded owner/conversation/time revision index, suppresses unchanged overlap rows before blob materialization, and refuses more than 2,000 candidates or 8 MiB instead of returning an incomplete view. Sync snapshot/page/changes use bounded tail scans, exact replay heads, stale-cursor fencing, and top-level Turn/Attempt routing identities on specific changes, and `conversation.get` plus transcript-bearing `conversation.list` derive their legacy messages from that same authority with bounded response and message-window semantics. Conversation creation atomically publishes the owner header, exact O(1) count, search-dirty marker, receipt/outbox, and a narrowly scoped tenant-global ID claim that preserves active/trash uniqueness without granting generic cross-owner entity access; deterministic syscall and short-write injection verifies replay convergence. Owner-scoped conversation get reads large settings and Turn projections through bounded blob paths and projects the storage.v1 full-transcript and message-window shapes without exposing physical document envelopes. Settings snapshot-CAS and scalar metadata updates retain transcript `rev`, use the hidden physical document version to prevent lost updates, and commit search invalidation, receipt, and outbox atomically; the shared update commit path also passes exhaustive syscall-error and short-write recovery. Create and updates maintain an owner-local covering `(updated_at DESC,id DESC)` index in the same transaction using a prefix-safe descending UTF-8 encoding; normal list and cursor-based catalog pages consume bounded range scans without N+1 entity reads, preserve snapshot-consistent counts, filters and settings projection, and reject responses above 8 MiB. Event batches admit the complete finite item list without a stream-count quota, including distinct physical streams; entity writes retain the overlay u32 count representation, while transaction family counts follow the existing u16 encoding and inline envelopes retain their declared 256 KiB bound. Sparse event application sequences use atomic natural-key, event-type, age-leading retention and physical-position indexes over the continuous physical stream cursor; unfiltered list resolves up to 1,000 positions in one segment pass, while exact types and literal prefixes use a bounded type catalog plus ordered index merge without scanning high-volume unmatched events. Retention deletes at most 1,000 oldest owner-scoped rows per transaction, treats blank types conservatively as structural, and atomically repairs bounds, filters and inspector aggregates. The same commit advances a durable owner-scoped retirement queue for one stream and at most 128 complete immutable segments; stream metadata v2 records the retained base while decoding v1 as base one, retired cursors fail explicitly, and monotonic append continues after fully retired history. Separate bounded authority GC then reclaims blocks no longer referenced by the current catalog, retained history, snapshots or capsules. Inspector summaries fold structural counts and first timestamps directly from the same type metadata for every requested root and all owner-scoped `#agent:` children through 512-row metadata windows without reading event bodies. Prefix or child expansion reaching 1,000 entries returns resource exhaustion instead of an incomplete result. Private `_wire_*` projection and payloads above 256 KiB remain explicitly rejected. Browser site observations use digest-keyed, exact-identity-verified blob-capable documents plus an atomic owner-local LRU index; passive hint validation, 30-day expiry, 64-row expiry pruning, deterministic oldest-first eviction and the 200-document owner budget require no unbounded namespace scan; deterministic syscall-error and short-write injection preserves atomic document, LRU and outbox recovery. BYO providers use blob-capable exact-identity documents, an exact owner/tenant-label count without an admission quota, a newest-first covering index, and a tenant-global ID claim; list excludes ciphertext while mutation, receipt, outbox, count, index, and claim publish atomically. Durable worker jobs use tenant-global owner-bearing blob-capable documents, exact task and per-user idempotency identities, 1001-priority per-kind availability summaries, and ordered queue/lease indexes; enqueue, bounded multi-kind claim, monotonic heartbeat, cancellation, completion, fencing, receipt, and outbox transitions publish atomically. Model-routing revision CAS atomically publishes separate blob-capable current, backup, and migration-receipt records; sealed secrets use an exact 1,024-row owner/boundary count, reference and updated-time indexes, ciphertext-redacted lists, and 256-row pruning. `task_results` checkpoints store up to 64 MiB through an immutable tenant-global blob graph while a compact owner-bearing header owns semantic version CAS and abort fencing. Default replay reads only a separate metadata/error projection, summary scans consume owner-local covering rows, terminal replay alone materializes content/thinking, and abort updates no payload blob. Owner-local live and hashed experiment indexes make restart settlement and cost scans proportional to live or matching tasks rather than payload bytes; recovery atomically advances header, summary, and index state without rewriting the full blob, while cost results fail closed above an 8 MiB aggregate. Guarded-v1 checkpoint atomically witnesses its active parent and merges bounded cache HWM/LWW facts, using an exact response echo to retire rolling-upgrade compatibility reads; identical stale-version lost-ACK replay remains stable. Payment records additionally use exact provider/ID claims, a blob-capable 8 MiB document, a bounded created-time index, and atomic wallet/ledger settlement; duplicate provider delivery returns before unrelated payload validation, matching storage.v1. Redemption codes retain the 10,000-code mint contract through 4,096 bounded blob-capable batch, locator, and mutable-state shards: maximum mint and list workloads remain below point-witness and write-set ceilings, while one-code consumption rewrites only one compact state shard. Stale-reserve queries consume the atomically maintained age projection without ledger scans. A compact lane covering index lets `turn.compact` validate up to 100,000 structural rows without hydrating retained Turn blobs; one CAS transaction admits at most 2,000 combined deletion/reparent/projection mutations under a 64 MiB materialization ceiling, with at most 512 reparented Turns and 512 projection updates totaling 8 MiB, repairs ancestry/counts/indexes, inserts the summary, and emits one snapshot-required replay invalidation. Queued pair activation now atomically removes the exact queue binding, installs recovery, dispatchable, and lane-live indexes, and emits one replay revision; cancellation instead removes only the never-started pair and all of its queue, Attempt, event, and index state. Live steering fences the exact unqueued, task-bound Attempt and current running Turn, retires any materialized projection head, appends one command-keyed injection, and publishes its revision, compact sync patch, receipt, and outbox atomically under exhaustive commit-fault certification. Related-Turn announcement reads at most 2,000 explicit identities and advances the live root through a head-consistent no-op patch, preserving externalized projection checkpoints while publishing one Attempt/conversation replay event and revision atomically. Visible-run synchronization admits at most 2,000 messages/8 MiB, removes only provably empty virtual-user ghost pairs before authority access, inherits full live tool fields by stable call identity, and atomically publishes deterministic child Turns/Attempts, stable render segments, root projection/head state, indexes, replay, and revision. Optimizer proposals and reversible action audit rows use blob-capable owner-scoped documents, exact owner-scoped counters without proposal/action admission quotas, prefix-safe compact time/status/proposal/active-expiry indexes, complete requested lists with the existing 50-row default and no private document/response byte quota; proposal existence, compact status, action indexes, receipts, and outbox records publish atomically. Research Foundry reports use bounded owner-scoped blob documents; optimistic workspaces retain complete owner-scoped user documents without private byte quotas; compact prefix-safe state and a descending-created index make direction discovery independent of report size, while artifact quota, workspace revision CAS, receipts, and outbox records publish atomically. Paper notes use bounded owner-scoped blob documents and a prefix-safe chronological paper/language index; exact counts, documents, indexes, receipts, and outbox records publish atomically without list-time body scans. Operations outside the executable set stay default-denied.

Event prefix filters enumerate every matching owner-scoped type in 512-row
metadata windows within the original transaction, with no total type-count
quota. Inspector legacy-flow totals reuse that traversal. A priority queue
merges sparse per-type cursors by application sequence; overlapping prefixes
and explicit types yield each event once, under the caller's output page size.

The artifact Schema IR owns bounded chat and tool-result physical layouts: complete chat content and independently bounded 16 MiB tool-result blobs, atomic dedupe/version/library/expiry indexes, UTF-8-safe 64 KiB range reads, backup/GC reachability edges, and exhaustive single-fault commit recovery. Eleven storage.v1 operations compile through typed Transaction IR with owner checks, receipts, compact content-digest outbox evidence, and exact CPython 15.0 casefold search generated from a checked machine source. Authenticated request chunks admit the large write bounds without relaxing the 8 MiB single-frame limit. The single low-priority maintenance worker prunes at most 128 expired owner-scoped tool results per transaction with content-free progress metrics; the explicit maintenance RPC uses that same owner-scoped path with a 5,000-row hard ceiling. `system.reclaim` executes one explicit launch-budgeted physical GC round, preserves the legacy request bounds, and reports content-free block/segment progress without manufacturing a business transaction. `rate_limit.record_and_check` uses exact owner-scoped event identities, a 256-row expiry index, and a radix-256 timestamp count tree so arbitrary seven-day sliding windows require at most 2,040 counter reads rather than an event scan. The five `daily_cost` operations use chronological date keys, an exact owner count, blob-capable documents, complete paged month scans, one-row reverse latest reads, and complete valid date probes; whole-cache deletion is one range retirement, and blob-backed documents/responses have no private byte quota. Scheduler Schema IR fixes tenant-global owner-bearing blob-capable task documents and exact task-ID claims, owner-local exact system-key claims and counts, owner-local plus narrow tenant-global created/enabled covering indexes, a global poll sequence, and a 200-row owner/task poll index. All eleven task and poll operations now execute through the shared OCC layout: adoption preserves the oldest legacy identity while retiring duplicates, the internal cross-owner feed verifies embedded owners, due claims and result accounting update compact task state, and task deletion range-retires poll partitions. Receipt replay, SQLite differential coverage, focused owner/timestamp tests, and deterministic syscall-error/short-write create recovery preserve the frozen semantics. Timer Schema IR adds tenant-global owner-bearing blob-capable documents and exact ID claims, owner-local status/conversation indexes and exact counts, a narrow oldest-first global active index, and conversation-prefixed poll partitions. A one-shot launch probe bounds active timers at 8–16 by default with a 64 hard ceiling; historical rows are capped at 512 per owner so conversation deletion atomically retires all timer state within transaction bounds. Poll append/progress, receipt, and outbox effects share one OCC commit, and repeated poll IDs never double-advance. Differential, page-boundary, lifecycle, and exhaustive syscall-error/short-write tests preserve these semantics. Queue Schema IR separates immutable blob-capable payload cores from compact mutable position, lease, and binding state; exact tenant-global ID claims, owner-local order indexes, and narrow owner-bearing conversation, lease, and autopilot indexes keep worker feeds bounded without widening public scope. Conversations admit at most 512 items, reap releases at most 128 leases, internal indexes page through the 1,000-row Entity boundary, and responses stay below 8 MiB. All seventeen queue operations execute through one OCC transaction: real messages supersede synthetic continuation work atomically, autopilot rows never dequeue or reap, idle reap creates neither receipt nor outbox churn, and permanent conversation deletion retires queue and marker state. A queue injection acknowledgement drains at most 256 never-accepted peer/workflow inbox rows per command — it never cancels accepted Turn pairs — renumbers the remaining positions, and publishes one revision-free `turn.upsert` change carrying only the drained identities, matching the legacy reporting order and projection contract. SQLite differential coverage and deterministic syscall-error/short-write injection preserve legacy ordering, lease, dedupe, receipt, and recovery semantics. Orchestration Schema IR separates immutable blob-capable run inputs from compact mutable state, uses exact tenant-global identities and owner-bearing bounded startup indexes, and stores versioned event documents. All nineteen orchestration and Goal-run operations share one OCC authority; exact active claims make Goal supersession O(1), public reads re-verify owner scope, and startup recovery retires at most 1,000 active runs across owners. Definition CAS, event projection, terminal fencing, receipts, and outbox effects are covered by SQLite differential and exhaustive commit-fault tests. Swarm Schema IR separates blob-capable session specifications and agent message/result cores from compact lifecycle, rounds, delivery, and exact resumability state. Exact tenant-global owner-bearing keys prevent aliasing; a 512-row owner-local resumable index makes startup proportional to useful work, 1,000-agent session deletion stays within one transaction, and delivery acknowledgement never rewrites transcripts. SQLite differential, owner-isolation, page-boundary, and exhaustive checkpoint commit-fault tests cover all eight operations. Integration Schema IR adds tenant-global owner-bearing workspace documents, exact natural and row identities, oldest-first ready/integrating indexes, one exact project-active claim across owners, bounded owner/project status indexes, and a globally sequenced 300-event retention window. All sixteen integration operations preserve worker CAS, stale-claim recovery, metadata projection, receipts, and outbox effects; SQLite differential, cross-owner serialization, retention, and exhaustive commit-fault tests lock the semantics. Attempt creation adds exact command replay, per-Turn counts, projection/input CAS, bounded checkpoint/regenerate rewrites, and atomic attempt/event/sync publication under exhaustive commit-fault testing. A tenant-global owner-bearing created-time index exposes at most 32 canonical conversation-executor attempts and revalidates exact owner Turn, attempt, and config records under the same snapshot. Worker dispatch validates a canonical explicit principal and atomically enqueues the deterministic job, binds the attempt, removes discovery eligibility, and publishes sync/outbox state; replay fails closed unless binding and durable job agree. Compact owner-scoped task/effective-time and conversation/created-time indexes make timing-trace lookup and discovery proportional to at most 100 small records; list never reads projection blobs, while detail validates the exact indexed Attempt and Turn before using the permanent trace or legacy projection fallback. `turn.perception.record` now appends the closed, content-free browser receipt lane idempotently inside the Attempt document. It enforces the shared 64-row/96-KiB bounds, derives render and transport clocks, adopts only task-matched legacy receipts, and commits without Turn/revision/sync/receipt/outbox amplification under exhaustive crash and short-write testing. Owner-scoped sync-age markers and exact Attempt-event transport references now make `turn.sync.prune` proportional to expired replay data; each transaction processes at most 999 events plus one continuation witness and 64 MiB while preserving the monotonic sync head. Terminal Attempts maintain settled-time cursors so `turn.events.prune` retires at most 64 clustered ranges and 200,000 rows under a 64 MiB hydration ceiling only after sync references and projection heads clear; permanent Turn projections are never retention data. Both operations are owner-isolated, resumable, and crash-atomic. Existing authorities will acquire these indexes only through the explicit migration required before cutover; ordinary startup never scans or backfills historical data. Terminal supersession now maintains one owner-scoped age-leading superseded-Attempt index in the same transaction as the Attempt document, and `turn.cleanup` executes as a receipted owner-scoped command: each transaction scans at most 256 index rows, revalidates the exact superseded status and timestamp, refuses attempts still referenced as a live Turn current attempt or by retained conversation-sync replay fences, and atomically removes at most 64 superseded non-current Attempts with their event ranges, derived indexes, exact Turn directory/count repair, and bounded raw-archive state while retaining tenant-global identity claims against delete/recreate ABA; exhaustive commit-fault injection recovers one complete cleanup round. This leaves the full 335-operation catalog executable at the current contract revision; `contracts/tofudb_ir_v1.json` `executableOperations` is the count authority. The explicit offline importer `lib/storage_authority_sql/migration/tofudb_migration_import.py` streams declared tenant-user, conversation-header, turn, credential, provider, billing, and record keys from a pinned SQLite snapshot into an exclusive `tofu-db migration-session` child through private inherited pipes. `tofudb_migration_target.py` binds readiness and every request to the target UUID and explicit tenant/owner, bounds frames to 16 MiB + 8 KiB, and closes its owned child on timeout or protocol failure; no network listener or live sidecar token is involved. The v2 mapping preserves record JSON bytes, version and timestamp through insert-or-match, verifies each write independently, then publishes an incremental SHA-256 prefix. A 128-key owner catalog plus indexed source lookups independently verifies every target namespace and the final count without retaining document catalogs or imposing a 1,000-record ceiling. Resume accepts committed rows ahead of the checkpoint only when their exact snapshots match the bound source. Supplied bindings are recomputed from the pinned source. The CLI requires `--source`, `--checkpoint`, `--binary`, `--target`, `--authority-uuid`, `--owner-id` and `--tenant-id`; the old live TCP import invocation and old plan checkpoints are rejected. The operator CLI is `python -m lib.storage_authority_sql.migration.tofudb_migration_import`; it is never a startup or wire path. Bounded primary-key source scans live in `tofudb_migration_source.py`, identity-bound progress in `tofudb_migration_checkpoint.py`, and native insert-or-match document import in `packages/tofu-db/src/migration.rs`. A dedicated task-result adapter (`migration.task.result.*` in `task_result/migration.rs`) replays the exact stored `task_results` document with its version/timestamp through insert-or-match and atomically rebuilds its tenant-global header, replay projection, owner summary, live and cost-experiment indexes in the same authority transaction, then independently enumerates every index catalog; abort tombstones must already be merged into the source document exactly as the checkpoint semantic would write them. The tenant-global paper budget ledgers replay through a dedicated adapter (`migration.paper.build.budget.*` and `migration.paper.document.budget.*` in `paper_budget_migration.rs`): it validates the exact source snapshot against the live bounds, cross-validates every build reservation against the restored artifact metadata, and rewrites the native capacity ledger, owner-bound reservation claims and migration provenance atomically, while its get operations reconstruct the exact source snapshot for independent verification. Generic replay covers the remaining `paper.build.*` and `paper.document.*` records, `model.continuation.v1` has its own adapter, and namespaces without a domain adapter stay rejected. Credential, BYO-provider, and billing replay retain their existing semantic adapters; provider list redacts ciphertext while get preserves the envelope, and wallet balance, currency, alert threshold and timestamp must all match or billing remains incomplete. Identity-preserving tenant.user restore claims the source owner and advances the sequence high-water; live create still allocates. Conversation-header replay uses owner-scoped `conversation.create` with the source id, title, settings, and timestamps, then independently pages `conversation.list` catalog plus `conversation.count` and point `conversation.get`. Turn replay restores the exact settled `storage_conversation_turns` snapshot (lane, parent link, ordinal, actor, kind, run, status, projection/settlement documents, revision, and timestamps) through the dedicated `migration.turn.restore/get/scan` adapter (`turn/migration.rs`), rejects rows with a live attempt or an owner outside the operator scope, requires the parent conversation to exist in the target, and independently enumerates the owner-bound turn catalog before publishing its checkpoint prefix. Resumable import and explicit rollback are now certified: the importer publishes an incrementally verified checkpoint prefix per domain and resumes a partial run only when already-restored target rows re-verify against the pinned source, and `python -m lib.storage_authority_sql.migration.tofudb_migration_import --rollback` certifies (read-only, fail-closed) that abandoning a partial target is safe by re-hashing the pinned source against the checkpoint binding and reporting complete/incomplete/unchanged domains. This is covered by `tests/tofudb/test_tofudb_migration_import.py` and `test_tofudb_migration_boundaries.py` (73 passing) plus operator-run end-to-end evidence under `run-2026-09-10/`. This still does not certify remaining source tables without a domain adapter. The source holds one query-only snapshot, admits at most 8 MiB per row through SQLite before Python materialization, refuses temporary sorting, and detects intervening source commits. Progress holds at most 64 domains/64 KiB, binds source digest/schema, target authority UUID and mapping-plan digest, requires independently matching target counts/digests, and uses the existing file-sync/replace/directory-sync publication primitive under an exclusive lock. Completed domains release their last cursor after full verification, keeping the count/digest within the aggregate budget. An ambiguous publish requires reopen; progress alone never certifies current target contents. Daemon readiness now includes the UUID of its opened CONTROL authority, so reused paths/ports cannot stand in for target identity. Native import preserves positive version/timestamp metadata and JSON bytes, enlists owner-bound blobs in the same caller-owned transaction as indexes/progress, rejects divergent existing records and other target UUIDs, and caps each materialized document at 8 MiB; domain adapters still own all indexes and complete coverage. The current CLI exposes `certify-filesystem` for one explicit empty persistent target and `certify-filesystem-matrix` for an absolute empty primary path, an optional explicit second mount, and bounded deterministic syscall-error/ENOSPC/short-write/lost-sync cells. Its canonical limits and required cells live in `contracts/tofudb_ir_v1.json` under `filesystemCertification.matrix`; no filesystem type is inferred from the supplied path. Matrix recovery checks every acknowledged transaction and its payload, permits only complete prefixes of unacknowledged groups, and counts recovery errors as failed cases. Its deterministic fixture pre-creates exactly 256 empty block shards to remove random segment-hash branch drift; fault families reuse one captured trace and reject unhit or mistyped fault positions. Fresh-shard publication is verified separately by block fault tests and real-path cells. An omitted mount produces `matrix_result=partial`, a failed cell produces nonzero exit, and successful cells retain their stores as evidence. The primary path must be empty and absolute; nonempty mount targets remain untouched. Release-tree evidence now includes same-binary XFS/FUSE path cells, the operator-mounted beegfs-fuse cell passed on 2026-09-08 (full matrix result: 6/6 cells pass; cross-process write, reopen and read-back on the real mount; retained store evidence under the operator-provided mount path), and  acknowledged-prefix checks across twelve rotation windows, and an explicit 33,488,896-reference/511-run spill test (about 1,022 MiB on disk and 19.7 MiB measured peak RSS on the test host). Acknowledged durability under arbitrary device sync violations is now certified by the deterministic `vfs-fault.lost-sync.correlated-all[.at-rest]` cells: every single dropped commit+checkpoint sync, every pair, and the full correlated set (all barrier syncs lost in one crash window) must each recover a complete prefix — the whole acknowledged group or an earlier one — or fail closed, never a torn group; losing the full barrier set may still lose the unprotected acknowledged group, but only as a complete earlier prefix. What remains uncertified is any non-sync device fault (silent byte reordering/corruption, or a device reporting bytes durable before any barrier). Release certification must bind these bounded observations to its exact build and filesystem assumptions. TofuSQL P0–P2 is implemented: `src/tofusql.rs` serves the read-only SQL front over the catalog declared in `contracts/tofusql_catalog_v1.json` (generated by `scripts/gen_tofusql_catalog.py`), with key-prefix and trailing-restText range pushdown to `EntityKey::prefix_range`, a read-only authorizer, a progress-handler deadline, owner-isolated scans, and the `tofu-db sql` operator CLI. P1 added the index/stream catalog tables with `orderedBinary` opaque key parts addressable through the `tofusql_key()` hex table function. P2 adds constrained joins: at most two catalog tables through one INNER JOIN whose ON clause equates only catalog-declared join-edge identity columns, with both sides scanning under an equality-pinned leading key part and the entity-read ceiling charged statement-wide; every other multi-table construct stays rejected. Remaining release work covers block-at-rest encryption and signed release packaging. Supervisor selection is now implemented (see *Native Supervisor selection* above): a personal deployment opts in through `TOFU_PERSONAL_STORAGE_BACKEND=tofudb`, while SQLite stays the default and startup fails closed when the selected backend is unavailable. Signed packaging and block-at-rest encryption remain open until the full certification gate set passes. The semantic differential tests now pass 167/167 against the current tree (the 2026-09-13 `tool_result_artifact.search` folded-stream offset fix changed the release binary to `9a2a0755…242612`; only the differential gate has been re-run against it, so the remaining certifications below still refer to the prior operator-certified build). As of 2026-09-10 the semantic differential tests (160/160 at the time), the deterministic filesystem/syscall-fault certification (`certify-filesystem` / `certify-filesystem-matrix`), the explicit resumable migration and rollback certification (73 migration tests plus `run-2026-09-10/`), and the performance/resource gates all pass against release binary `c6cda189…2017a3f` (evidence retained under `data/`). Signed release packaging remains open; none of these gates waive correctness, durability, migration/rollback, or resource checks. The performance/resource gate is `tofu-db certify-resource-gate` (contract `filesystemCertification.resourceGate`): one fixed bounded workload (512 inline commits + 24 x 1 MiB block-carried commits across the rotation window, one commit+checkpoint stage, one reopen) measured per stage in child processes through Linux `wait4` wall-clock and peak RSS, plus the retained authority footprint and launch-probe snapshot on one `tofu-db.resource-gate.v1` line. It passes when the runner pins the exact build and volume, retains the measured store as evidence, and every emitted `*_exceeded` flag against the personal-computer reference budgets (512 MiB peak RSS, 1 GiB retained bytes, 5 ms mean commit, 300 s total) is false; the budgets are observational references for one operator-recorded volume class, so the runner owns the pass/fail comparison, never the measurement command itself. There is no mandatory calendar-duration shadow run; representative shadow/replay comparisons remain validation evidence, not a fixed waiting period. Removing that waiting period neither certifies the release nor waives correctness, durability, migration/rollback, or resource checks. Converged cross-authority semantics, pinned by semantic differential probes in `tests/tofudb/test_tofudb_differential.py`: `record.list` treats `prefix` as a literal byte prefix on both engines (legacy escapes LIKE metacharacters and declares `ESCAPE '\'`, so `_` and `%` carry no wildcard meaning), and both search engines use Unicode lowercase mappings. `contracts/conversation_search_v2.json` owns paged search, source-role/match-field metadata, phrase-first ordering and snippets mapped back through Unicode expansion. The native projection implements bounded chunk continuations, title/ID matches and retained compaction sources; differential probes compare complete logical result pages, including long-message tails. Operation dispatch coverage alone does not certify these semantics. `turn.append_settled` pre-checks a claimed `turn_id` and raises `database_conflict` on duplicate identity on both engines (the SQLite primary key never leaks `database_integrity`), and `turn.list` breaks cross-lane ordinal ties by `turn_id` on both engines. Gate regressions are resolved by converging behavior or amending the contract, never by deleting probes.
Canonical result trees, signed cursors, private-key lifecycle and bounded blob reads follow [Tool result queries](contracts/TOOL_RESULT_QUERY.md).


The internal Rust `schedule_owner_search_backfill` entry point accepts one explicit tenant/owner scope and persists a rebuild generation. The existing search worker drains prior dirty work, then reads at most eight owner-local conversation headers per round and atomically publishes their dirty tokens plus a bounded forward cursor. Restart resumes that cursor; stale workers fail OCC, and exact-token ACK preserves concurrent mutations. Cursor completion certifies enqueue completion only, not projection publication. The compatibility `turn.search.backfill` RPC implements that global contract as explicit authorization-scope enumeration: each receipt-free command invocation scans at most 64 tenant-global conversation ID claims, durably schedules one owner-scoped rebuild generation per discovered owner, and publishes a resumable tenant-global cursor in the same authority transaction, so a crash can never advance the sweep past an unscheduled owner. Payloads carrying any owner or tenant identity fail closed, the IR executes in the explicit tenant-global scope rather than the authenticated-owner scope, owners with an in-flight rebuild keep their committed cursor, tokens ahead of the durable sweep fail closed, and consumed tokens answer as lost-ACK no-ops. The RPC adds no startup scan or thread; projection admission separately schedules necessary owner-local rebuilds lazily in the existing worker.

Full history segments are prepared incrementally before foreground single/group commits, at most one 4 MiB segment per invocation. Preparation lead equals the rotation window multiplied by the incoming framed WAL bytes divided by segment capacity, rounded up and clamped between one segment and the rotation window: small writes wait until near rotation, while large writes start earlier. Short generations incur no speculative history writes. The existing history codec and WAL hash chain are unchanged; a checkpoint publishes prepared references and its short remaining suffix before retiring WAL. A launch retains at most 33 unpublished segment references, no additional payload cache or worker, and at most one 64 MiB active WAL window plus segment headers of temporary history files. GC marks those unpublished references; a crash discards only their in-memory cursor, resumes from bounded WAL recovery, and leaves orphan reclamation to explicit GC. Preparation faults never advance the cursor or publish a manifest, and the dedicated staging/publication fault matrix verifies acknowledged transactions and referenced payloads through reopen and continued writes. Background payload packs retain their exclusive reservation through manifest publication and staged-copy retirement; a scoped guard releases it on every return path, preventing duplicate packing without another block-ID set or worker. Foreground materialization defers while publication owns that reservation. The rotation-checkpoint finish drains before destructuring its covered snapshot, so the same publication-owned defer restores the covered prefix whole and re-prepares the lane after publication; any other drain failure fails closed restart-required exactly like a phase-2 failure. The install's in-hold fsync chain is kept minimal because a 0.5 s authority hold serializes every foreground writer behind it (2026-09-22 FUSE swarm bench: one ~460 ms install hold stalled all four writers simultaneously): the driver pre-creates the next-generation WAL file off the mutex (the install's `AlreadyExists` arm reuses it), and the install's history/manifest blocks write through the loose path, skipping the dedupe probe read on fresh-by-construction content while keeping the same write/fsync/rename barrier set. On FUSE/network mounts the background packer can starve foreground round trips with its segment write+fsync storm, so the packer paces itself with an adaptive feedback loop (no operator rate): foreground commit rate (`STAGE_ENGINE_COMMIT`) is the activity signal and foreground WAL stage latency (`STAGE_WAL`) is the pain signal, the write rate halves when a packed window's foreground latency exceeds max(2× the learned quiet-window baseline, 10 ms), recovers ×1.5 under max(1.25× baseline, 5 ms), floors at 1 MiB/s, and unpaces entirely after ~1 s of idle foreground or once the recovered rate passes 4 GiB/s. Network-class volumes (the same launch probe as WAL durability tiering) start paced at 64 MiB/s; local volumes start unpaced and clamp down only on measured pain. A quiet-gate defers ordinary batches entirely while the foreground is hot (staged pressure above 32 MiB or an explicit drain overrides), batch size caps at 1–2 MiB while the measured rate is low so one fsync storm has a bounded duration, and the driver gets a 30 s boot grace plus a crash-safe bounded stop join so startup/shutdown never pay a background stall. The commit path additionally requests the background drain once the active WAL crosses half the rotation threshold, so the quiet-gate's own backlog is retired across the WAL's second half and the rotation install never inherits a multi-second fenced drain storm (2026-09-22 FUSE: ~32 MiB published mid-rotation while writers fenced at the hard ceiling). An explicit `TOFU_DB_PACK_BYTES_PER_SECOND` (integer bytes/sec, optional KiB/MiB/GiB suffix, hard ceiling 16 GiB/s) remains as the fixed escape hatch — at 32 MiB/s on this repository's FUSE mount the medium-scale create p99 drops 24.9 → 4.6 ms with unchanged p50 (see `STORAGE_PERFORMANCE_CHARTER.md`). The effective mode/rate is exported through `system.metrics` (`backgroundPackerPacing`). On the serving path, a command rejected with rotation backpressure while a rotation owns the lane (or the active log is within 1 MiB of the hard ceiling) parks on the daemon's rotation barrier — a generation-counter condvar the driver signals after every finish — and replays in place up to sixteen 8 ms waits inside its own deadline instead of bouncing the round trip off the client; budget exhaustion or a deadline shortfall returns the same retryable `database_busy` the client already absorbs, so behavior degrades to the pre-parking semantics rather than to a new failure mode. The maintenance worker classifies the same rotation backpressure (and `WouldBlock` publication conflicts) as a conflict deferral, never a terminal worker failure: a terminal record reaches the listener's health gate and recycles the whole daemon on a signal that is retryable by design (2026-09-22: two daemon exits behind slow-mount rotation windows, one failed round each).


### Live projection admission and connection readiness

A projection patch document has an 8 MiB logical ceiling, matching the Turn
projection field budget. The storage.v2 request frame and the event envelope
retain their independent bounds; this ceiling is not a promise that an 8 MiB
payload plus its envelope fits a request. The compact projection head still
retains at most 64 patches totaling 1 MiB. An individually larger admissible
patch causes checkpoint materialization instead of a permanent write rejection.
Both limits derive from `contracts/tofudb_ir_v1.json`; changing a physical head
budget must not silently change the public update vocabulary. Application
certification exercises a high-entropy 1.25 MiB structural update, a subsequent
increment, and exact recovery after authority restart on both backends.

On Unix, the native listener waits for socket readability with a bounded poll.
Incoming connections wake it immediately; the existing 25 ms timeout bounds
parent-lease observation only. No worker, connection, frame, or cache budget
increases. Interrupted waits return to lifecycle observation. Non-Unix builds
retain the previous timed accept fallback pending native platform verification.
The listener tests exercise readiness before a deliberately long lease timeout,
idle wakeup, authenticated requests, capacity rejection, and parent EOF shutdown.


Segment membership witnesses survive full-index eviction. One quarter of the existing segment-cache byte budget retains independent verified Bloom filters; the remaining three quarters hold full indexes (including their filters). Both partitions retain at most the manifest's declared segment count, and their sum stays inside the existing default 64 MiB budget. No extra startup scan is introduced: the existing packed-union scan supplies the witnesses. Unknown or evicted witnesses fall back to exact reads. A negative answer first validates all cached metadata and first/last block IDs against the current manifest; positive answers still require the exact index and per-read payload integrity. Segment removal forgets both caches. This prevents a cold or thrashing index cache from reopening every unrelated segment for each entity-tree page. Verification includes real LRU pressure, total byte/count bounds, malformed manifest witnesses, and a 64-segment overlapping-range read with an I/O oracle.


Startup recovery also retains at most 65,536 exact locations for block IDs in the active WAL, including carried authority roots. The existing packed-union index pass supplies these locations; no extra segment-directory scan is added. Each selected location still reads and validates the actual payload before its WAL copy may be discarded. Repeated references during the exclusive open reuse that verified result. Missing/torn files restore from the carried bytes; other I/O errors remain failures. Partial index validation never proves absence, and IDs beyond the cap use the conservative recovery path. The temporary map is dropped immediately after replay. A preliminary bounded-record decode only collects IDs: malformed/ambiguous-tail decisions remain in the authoritative replay loop. The limit derives from the active-WAL IR contract. Tests cover large overlapping catalogs, repeated and WAL-only blocks, injected repairable and nonrepairable read errors, admission limits, and the existing exhaustive engine crash/short-write/lost-sync families.


Small owner catalogs (at most 1,000 live conversations) read primary headers in
one transaction, under the existing 8 MiB list budget, then sort and apply the
same timestamp/ID cursor and folder projection. Updated-time index tombstones
can grow independently of live cardinality and exhaust the 1,024-leaf OCC
witness bound; they are not a suitable scan plan for a small, frequently updated
catalog. The primary plan checks the owner count invariant. Larger catalogs
retain the indexed plan and its explicit bounds; this is not an unbounded scan
or a relaxation of concurrency witnesses. The native tombstone-density
regression and real application catalog tests cover this plan.

Canonical command payloads are serialized once for ordinary outbox publication
and both current/legacy-ownerless receipt digests. The ownerless digest omits
only the root `user_id` member's exact encoded byte range; nested fields and
all other canonical bytes remain bound. Legacy replay and owner fencing are
unchanged. Search projection n-gram enumeration uses constant-space Unicode
iterators and a per-call exact cache of 256 entries (3,328 bytes); eviction
never suppresses a different gram. These optimizations do not change the
persisted projection format, Bloom bits, integrity hashes or durability barriers.

Task replay events have separate logical and physical budgets, declared in
`tofudb_ir_v1.json.indexedStreamPayloads`. A logical event may be up to
8 MiB minus 1 KiB while a physical stream payload remains at most 256 KiB.
Larger events atomically publish an owner-scoped versioned blob document,
a small stream marker and the original payload digest/index. Ordinary,
filtered, latest and offline migration reads hydrate and verify that digest.
Retention deletes the overflow document with its event index; the existing
entity/blob reachability graph protects backups, snapshots and garbage
collection. No extra cache or worker is introduced. Existing inline events
remain readable; older binaries cannot read new overflow markers. The
aggregate Transaction IR and response page budgets remain 8 MiB.


## Activity-index churn and retry deadlines (2026-09-18)

Problem. Production activity reads failed with `database_resource_exhausted`
with only 19 live conversations, even for an empty future interval. A restored
production backup reproduced the failure with 13 conversations: the native
scan hit its 1,024-leaf witness ceiling after only two live rows. Timestamp-keyed
activity candidates accumulated a tombstone per header update. Small fresh-store
benchmarks did not model this persistent stream-update history. The application
also reused the first RPC deadline across retries, so a slow first read made
subsequent attempts arrive expired.

Decision. Activity candidate/state namespaces are versioned to v2; candidates
are keyed by stable conversation ID and keep timestamps in their compact value.
Reads select the newest bounded top-k candidates (updated time then ID, both
descending); old authorities use the stable document catalog until explicit
bounded backfill completes. Backfill uses that same stable catalog. Reads never
rewrite user state. Existing v1 indexes remain untouched and are no longer the
activity read authority. Transactional maintenance, owner checks, trash/restore,
and completeness validation remain required. A retry receives a fresh wire
deadline capped by the original aggregate attempt budget; ambiguous commands
remain non-replayable.

Alternatives rejected: enlarging leaf/memory limits leaves growth proportional
to stream history; longer RPC timeouts cannot repair deterministic scan failure;
silently returning zero activity corrupts the visible result. Verification must
include repeated updates followed by reopen, old-authority reads and resumable
backfill, plus application differential contracts, not just fresh-store speed.


## Oversized-commit spill scope (2026-09-18)

Problem. Oversized commits once flushed the full pending backlog while snapshot
readers waited, producing thousands of unrelated small-file operations on FUSE.
Scoped spills avoid that cost, but an existing loose filename was still treated
as a complete-copy witness: syncing a damaged duplicate could retire valid
pending bytes and permit an identifier-only ACK that cannot recover its block.

Decision. Reference-only commits flush only their referenced blocks/authority
root, including matching recovery sync marks. Before retiring a pending batch,
every existing loose copy must pass framing, CRC, content-hash and staged-byte
equality checks. Validation, directory and sync failures preserve the entire
pending batch and prevent identifier-only acknowledgement; verified reuse also
records the GC relink fence. Unrelated pending commits keep their own durable
WAL envelopes; checkpoint retirement still drains all pending data. Tests cover
256-commit scoped backlogs, corrupted duplicates, partial-batch rollback and
power-loss recovery.

Alternatives rejected: existence plus fsync cannot prove payload integrity;
silently replacing a bad duplicate would hide corruption. Relaxing durability,
raising deadlines or increasing the carried-record size hides the threshold.
Moving all I/O outside the mutex requires a separate publication protocol;
scoped, verified flushing preserves serialized commit and recovery guarantees.

The retained `packages/tofu-db/examples/spill_backlog_bench.rs` exercises 256
carried commits followed by an oversized spill in a disposable subdirectory of
an explicit filesystem parent; it reports spill latency and verifies reopen.
Both fresh-process and recovered-WAL backlog states are covered by the unit
budget/recovery test.

## Worker restarts re-open the authority in-process (2026-09-19)

Problem. Every in-app restart stops the native daemon with the worker and
re-opens the authority during boot phase 3; on the production FUSE volume the
measured budget is ~10-16s of engine open plus a cold block cache, inside a
~47s click-to-ready restart (drain ~13s, boot ~30s, verification ~2s). A
persistent, independently owned daemon would remove phase 3 and the
storage-boundary stop from every restart.

Shutdown search work checks the maintenance owner's stop state between source pages and projection append batches, before publishing a generation. An interrupted build keeps its exact dirty tokens and remains unpublished; a new process can rebuild and ACK it. Maintenance admits no further retention round after stop. This permits the existing joined shutdown to reach both WAL checkpoints instead of consuming its deadline in a multi-page search rebuild. Observed baseline: a 30s stop exhausted at maintenance, followed by 245s storage startup (47s authority open, 193s search open). The native regression interrupts one staged page, reopens both stores, and verifies complete publication/ACK; worker and maintenance neighbors run in 0.20s and 0.97s on this host. This is correctness evidence, not a prediction of production FUSE latency. Increasing the stop budget or publishing interrupted work as complete were rejected.

Decision. The authority remains a child of the worker image: the close-on-exec
owner channel binds the lease to exactly one serving process, so an orphaned
daemon can never hold the project lease or outlive its config generation. The
external-attachment mode (`TOFU_STORAGE_CONNECTION_FILE`,
`lib/storage/connection_file.py`) is kept for the SQL co-container contract
only; it speaks the v1 protocol and never carried the native v2 wire. Making a
lifecycle-manager-owned native daemon publish a v2 connection file is the only
viable persistence path, and it must redesign crash re-spawn ownership, token
rotation, offline-maintenance fencing, and upgrade protocol negotiation
together; it is deferred as one design episode, not an incremental change.

Alternatives rejected: attaching the existing v1 connection-file path to the
native daemon (protocol mismatch; would fork the wire contract), letting the
worker spawn detach the daemon without an owner (breaks the lease/EOF fence
and offline-maintenance exclusion), and masking the open cost with longer
client deadlines (pays the cold cost inside user requests, already rejected by
the warm-up keeper in `lib/storage/warmup.py`).

## Stream-append attempt fence (2026-09-18)

Problem. Coalesced stream frames skip the `turn.event.record` transaction,
so their attempt liveness was proven by a per-frame authoritative pre-read
(`turn.attempt.get`): one extra round trip per visible delta, ~10-40 reads/s
per streaming task, plus a second read for a Flow phase root. Those reads
queued hardest exactly while the authority was wedged. The same investigation
found the native engine silently dropped `flow_root_attempt_id` on
`turn.event.record`, leaving native authority without any flow-root fence.

Decision. `event.append` and `event.append_batch` items accept an optional `require_attempts_live` fence (1-2 attempt ids). The authority checks every listed attempt exists with a live status inside the append transaction; a stale or missing attempt rejects only that row with `{"inserted": false, "rejected": "attempt_not_live"}` — batch siblings still commit, the aggregate response gains a `rejected` count on both authorities, and the producer (`lib/tasks_pkg/event_log.append_persistent_event`) maps the verdict to the definitive `EventAttemptStaleError`, plants the same cooperative-abort stamp the pre-read produced, and persists nothing. A stale frame is now rejected outright rather than persisted-and-then-aborted; the terminal settlement still carries the final content through the write lane. Fenced native appends run under the identity-claim transaction scope, like the turn steps. `turn.event.record` enforces `flow_root_attempt_id` natively: the immutable attempt `_config.flowRootAttemptId` must equal the declared hint, and a declared root must be a live current attempt in the same conversation — mirroring the SQL sidecar.

Alternatives rejected: a TTL cache of attempt liveness weakens the
never-leak-a-late-delta guarantee by up to the TTL; more client-side retries
keep the per-frame round trips that caused the queue pressure; batching the
pre-reads themselves would still pay one RPC per window per task.

Verification: native engine tests in `transaction_ir::tests` (live/settled/
missing verdicts, mixed batches, fence bounds) and
`tests/events/test_event_attempt_fence.py` (sidecar verdicts, batch aggregates,
producer mapping, coalesced-lane hand-off, SSE degradation).


## Activity-index churn and retry deadlines (2026-09-18)

Problem. Production activity reads failed with `database_resource_exhausted`
with only 19 live conversations, even for an empty future interval. A restored
production backup reproduced the failure with 13 conversations: the native
scan hit its 1,024-leaf witness ceiling after only two live rows. Timestamp-keyed
activity candidates accumulated a tombstone per header update. Small fresh-store
benchmarks did not model this persistent stream-update history. The application
also reused the first RPC deadline across retries, so a slow first read made
subsequent attempts arrive expired.

Decision. Activity candidate/state namespaces are versioned to v2; candidates
are keyed by stable conversation ID and keep timestamps in their compact value.
Reads select the newest bounded top-k candidates (updated time then ID, both
descending); old authorities use the stable document catalog until explicit
bounded backfill completes. Backfill uses that same stable catalog. Reads never
rewrite user state. Existing v1 indexes remain untouched and are no longer the
activity read authority. Transactional maintenance, owner checks, trash/restore,
and completeness validation remain required. A retry receives a fresh wire
deadline capped by the original aggregate attempt budget; ambiguous commands
remain non-replayable.

Alternatives rejected: enlarging leaf/memory limits leaves growth proportional
to stream history; longer RPC timeouts cannot repair deterministic scan failure;
silently returning zero activity corrupts the visible result. Verification must
include repeated updates followed by reopen, old-authority reads and resumable
backfill, plus application differential contracts, not just fresh-store speed.


## Full-catalog publication and foreground isolation

Problem: at the 4,096-segment ceiling, background pack adoption stopped. A
foreground checkpoint then merged files under the authority mutex, choosing
slot zero of a hash-sorted catalog and searching the whole catalog for every
historical block. Slow storage therefore stalled unrelated requests. Even
unlocked maintenance could indirectly block them through a read-handle cache
mutex held during disk reads or a Bloom-filter lock held during index scans.

Decision: the existing single publication lane handles both catalog append and full-catalog replacement. Preparation reserves at most one incoming segment and one merge output. Outside the authority mutex, it chooses the smallest fitting source using validated metadata, streams verified bytes through exact segment locations, seals the replacement, covers the WAL, and publishes both CONTROL slots. It then installs the shared read catalog and reclaims the two superseded files. The final authority phase only reconciles generation and catalog metadata; concurrent acknowledged WAL sequence/root updates always win. Physical maintenance defers while publication owns the catalog, and orphan GC protects both reserved IDs. An old read view retries against a changed catalog if its source was retired; an unchanged corrupt catalog still errors. If no union fits, the existing durable loose-block representation provides bounded-batch overflow outside the mutex.

Online rotation requests background draining, including sub-MiB tails, and never
steals a pending pack for synchronous foreground materialization. The rotation
driver retries independently of new writes. While online rotation drains or its snapshot is
in flight, writes receive classified retryable backpressure, keeping the final
history tail bounded; reads can still acquire the authority. The existing hard
WAL ceiling and durability barriers are unchanged. Waiting for retired-WAL
cleanup uses a nonblocking admission probe instead of waiting behind unlink/fsync.
Offline engine users retain explicit synchronous checkpoint behavior.

Segment handles are leased out of the cache before open/read. The cache retains
at most 32 idle handles; active leases belong to the already bounded request and
maintenance workers and close on failure. No new thread or unbounded queue is
introduced. A merge retains at most 65,536 IDs, reads one block at a time (4 MiB
maximum), obeys the 256 MiB segment ceiling, and has at most two unpublished
segment files. Filter rebuilds scan outside their reader lock and install only
if both the catalog and insertion revision still match, preventing false
negatives from concurrent insertions.

Verification includes real 4,096-entry catalogs, deterministic torn writes and
publication/unlink failures with crash recovery, exact file-count bounds, a
memory-only finish phase, paused merge I/O with concurrent reads and commits,
independent file reads during a paused cache miss, and autonomous rotation after
background drain. Terminal Turn retry behavior is specified in TURN_SETTLEMENT.md.

Alternatives: increasing deadlines preserves hidden blocking; raising catalog
capacity postpones the cliff; only changing source selection still leaves disk
latency under a global mutex; unbounded workers move the problem into memory,
file-descriptor and disk budgets. The selected design preserves the configured
native backend and existing on-disk format and durability protocol.

## Owner-level gram posting index for conversation search (2026-09-21)

Problem: `conversation.search` walked every conversation header in recency
order and opened each generation's meta namespace (~3-5 µs per conversation),
so a query matching nothing still cost ~255 µs on 64 conversations, and a
title-heavy query ~650 µs, while raw SQLite FTS5 answers in ~60 µs. The
per-chunk bloom filters were already exact; the missing structure was an
owner-level way to resolve *which* conversations can match at all.

Decision: a new projection namespace `search_gram_postings_v1` maps each width-1/2/3 gram digest to one packed, sorted posting list of the conversation-generation digests whose lowercased title, id, or chunk text emitted that gram (title/id publish at finalize; documents page by page). Lists inline at most 255 entries (the 8 KiB inline-value bound) and promote to a universal marker beyond it, which the reader treats as unfilterable for that gram. Every update rides inside the same transactions that build, publish, replace, or remove the generation's meta/text rows, so postings and content move atomically; retirement recomputes the exact gram set from the reassembled texts and retracts it. Query-time, a per-owner durable state byte (absent → legacy walk, BUILDING → walk, READY → serve, DEGRADED → walk) gates the index. Once a query goes barren (two consecutive non-matching conversations in the current phase), the walk intersects the rarest gram lists first (up to 8 lookups per phase, entry-budgeted) and then skips the per-conversation meta scan for every conversation outside the candidate set, with identical state effects, so result pages stay byte-identical; budgets (CANDIDATE/CHUNK scan pages) charge exactly as before except skipped chunk scans, which can only shift page boundaries for owners whose single-query scan would cross those budgets — never the items. The index is a candidate filter only: every candidate still runs the exact legacy title/id/contains/ phrase verification. A versioned wipe (same marker machinery as the format version, checked by `prepare_owner`) rebuilds legacy projections on the first maintenance round; the worker publishes the index when the first complete backfill drains. Emission is bounded opportunistically: an update that would push the owner past `maximum_logical_bytes_per_owner` durably marks the index DEGRADED (sticky until the next wipe, usage accounting stays exact under retraction), and a conversation-generation whose emission overflows the engine's per-transaction write/witness ceilings is recorded in `search_gram_excluded_v1` as an unconditional candidate while the rest of the owner keeps the index. Measured on the 64-document probe (internal search time): match-none ~190 µs → ~47 µs, match-title ~625 µs → ~154 µs, match-heavy unchanged by design (the walk is already optimal when every conversation matches; the harness's match-all shape stays at p50 0.19 ms).

Alternatives rejected: per-(gram, conversation) rows (12x more tree rows; +25% walk cost from tree growth and no point-lookup path); packed lists with no write/witness caps (single 4,096-character documents can emit more distinct grams than a transaction can witness); skipping retraction for a degraded index (usage accounting would leak); and filtering eagerly on every query (a common-word query would pay set construction for no benefit, so activation waits for two barren conversations). Verification: posting lifecycle/retraction tests, byte-identical page batteries walk-vs-indexed across a query battery and past the header page budget, budget-trip degradation with exact reclamation, universal-list promotion, exclusion fallback, worker publish-after-drain, plus the differential and conversation-search suites.

### Live continuation segment repair

**Problem:** continued Flow turns can retain durable tool rounds with an empty segment lane. Rejecting that valid running projection at patch normalization also rejects terminal settlement, leaving a finished executor displayed as running. **Decision:** under the existing revision and attempt fences, native patch normalization mirrors the producer: materialize live prose, append only uncovered tool occurrences from durable rounds, preserve existing blocks, and publish the normalization delta in the ordinary replay patch. Synthetic inbox rounds stay excluded. Repeated normalization is idempotent. **Alternatives:** dropping tool rounds loses execution evidence; bypassing revision checks breaks replay; merely suppressing the error leaves positional patches without their required base. Malformed and uncovered terminal projections continue to fail closed.

## Project recovery scan performance

Problem: legacy updated-index rows can name the same project many times;
recovery hydrated and parsed its full document for every row before application
deduplication. Decision: each bounded index page deduplicates project keys
before hydration, borrowing identities from the immutable page. The raw
1000-row cursor and successor probe remain unchanged, pages read fresh authority
state, and owner/tenant validation remains mandatory. Active listing uses the
same bounded deduplication. Projection parsing owns the decoded object directly
instead of cloning all nested values. Alternatives rejected: deleting legacy
rows during a diagnostic read or caching projects across recovery pages would
change read-only semantics or conceal later settlement changes.

On this host, the disposable 1000-row duplicate first page with 69,632 bytes
of retained narrative took 780.119 ms before and 1.698 ms after, returning
1000 duplicate projects versus one. This is a debug-build, local synthetic
case, not a production reboot prediction; total boot still includes authority
open and settlement. `restart_recovery_20261007.json`.


Agent checkpoint cores use the existing blob-capable documents up to 64 MiB
minus 64 KiB of envelope slack, and complete recovery responses use 64 MiB
minus 4 KiB. The old independent 8 MiB swarm request/core/response ceilings
would otherwise reject the same checkpoint after transport reassembly or when
resuming it. Compact state, counts, ownership claims, transactional rollback,
and blob reclamation retain their existing lifecycles; no new resident pool or
cache is introduced.

Checkpoint bodies are charged to an independent cumulative swarm document
budget in Transaction IR, matching the existing task-result blob treatment.
Save notifications contain operation and checkpoint identity/status fields;
full message history remains in the blob document, while the receipt digest
still covers the entire original payload. Repeating the same command and
content replays its receipt; changing content under that command still conflicts.
The 9 MiB UTF-8 regression exercises semantic compilation, commit, receipt
replay/conflict, reopen and exact owner-scoped recovery.

A disposable native daemon debug-build probe on this host saved 9,437,184
UTF-8 bytes in 1792.956 ms and recovered them in 428.678 ms;
identical replay and delivery acknowledgement retained exact content. These
single-request timings describe correctness evidence, not a production latency
promise. Aggregate stream reservations and launch-probed queue/frame budgets
remain in force; the change adds no worker, cache or extra history copy to outbox.


Provider catalogs and transaction read witnesses have no count admission quota.
Exact provider counts still certify the owner/label created index; checked
overflow, count/index disagreement and duplicate tenant-global IDs fail closed.
Native lists traverse bounded index pages on one original transaction snapshot
and retain all read witnesses. SQL uses the same owner filtering and public
projection, with no quota-only owner lock. Listing omits encrypted API-key
material; model registration and document/wire validation keep their owners.
Verification and measured boundaries: [provider ownership](STORAGE_PERFORMANCE_CHARTER.md#provider-catalog-and-occ-witness-ownership).


Range-read ownership: leaf count is not an admission quota. Each raw scan window
retains its complete leaf witness on the original snapshot; masked base rows
advance key cursors rather than expanding and re-reading a capped prefix. Byte
accounting uses retained leaf-vector capacity plus keys/header allowance. A
failed later window removes only that scan's new witnesses and repays their
reservations. Format validation, requested result-page sizes and actual I/O/
allocation failures keep their own boundaries. See [range traversal](STORAGE_PERFORMANCE_CHARTER.md#range-traversal-and-witness-memory).


Timer counts are exact integrity observations without saved/live admission
quotas. The retired launch live-capacity setting reports zero and cannot refuse
creation. The internal cross-owner recovery query accepts limit=0 to traverse
all active index pages, verifying each embedded owner; positive limits retain
explicit projection semantics. Definition import, count overflow, ID claims,
poll idempotency, terminal transitions and atomic conversation purge keep their
own boundaries. [Timer admission](STORAGE_PERFORMANCE_CHARTER.md#timer-admission-and-complete-recovery) records verification and remaining idle-thread costs.

## Standard native runtime checkpoint capability

Problem: standard engine artifacts lacked the optional native journal required by Rust task recovery. Decision: native-runtime-checkpoints is a default build feature, with provider-json-order disabled in the runtime dependency. Explicit owner/tenant/task, byte budgets, original financial/time policy and joined lease lifecycle still govern activation; no startup journal, worker, store selection or automatic takeover is added. Explicit recover requires kernel-proven old-writer death; inflight, terminal and unproven legacy fences remain. --no-default-features builds the engine without this capability. Alternative rejected: a separate checkpoint daemon splits authority; silently substituting SQLite violates backend selection; memory-only verification bypasses WAL and canonical task-result CAS. Real application tests bind native stdio root/child fee checkpoints and terminal persistence to the same selected embedded authority. Close/reopen retains original 31/31 credits before a new 61-credit request; explicit tree profiles retain root/tree tokens separately. Mixed child routes require checkpoint_child_routes; their original model/provider quotes survive restart. The mixed-tree fixture adds 120 compact JSON bytes across two child route records within its 27,003-byte checkpoint; frames and journal retention stay unchanged. Accepted v4 handoff charges source 84 and target 55 once. Explicit paid-tree SIGKILL recovery retains 15 tokens, adds one 5-token request and settles 112 credits once after lost ACK. Inflight work refuses recovery and retains its hold; release-before-ACK, owner/commit-fault fences remain. Every test names its compiled source identity; these results do not certify dirty engine sources or automatic crash takeover. Writer identity adds one bounded current-header value, overwritten with each lease, and no worker/cache/history copy; acquisition captures local procfs evidence once. See explicit SIGKILL recovery evidence in DEVELOPER_RUNTIME.md. Idle footprint observations live in RESOURCE_CONTROL.md; production-default execution remains separately gated. Problem: the selected daemon lacked the embedded checkpoint port. Decision: checkpoint_runtime shares the exact journal/store/WAL callbacks, while authenticated native_checkpoint.capabilities/control frames borrow existing listener workers. The volatile capability registry has one process UUID; stale IDs, foreign owner/tenant, schema/command-receipt changes, expiry and insufficient reply bounds refuse before effects. Commands never automatically retry. Shutdown joins entered connection calls before driver retirement; the writer identity is the actual daemon, so application disconnect alone cannot authorize takeover. Capability probing requires the selected authority's existing durability witness. Normal daemons already attach the engine-owned WAL barrier in `start_wal_flusher`; checkpoint operations wait for the exact generation outside the writer while ordinary Normal commands keep asynchronous acknowledgement. Bare deferred authorities without a coordinator remain refused without changing mode. Problem: earlier transport tests covered Strict only. Decision: verify actual readiness/health durability, large snapshot reopen, paid Goal SIGKILL/unknown ACK/binding refusal, plus deterministic Normal checkpoint wait and sync-failure fences on the existing owner. Alternatives rejected: another flusher duplicates ownership; switching to Strict changes the selected policy; acknowledging checkpoints before their WAL witness weakens recovery. Alternative rejected: opening an embedded engine alongside the daemon would create another authority; adapting these commands through ordinary semantic writes would bypass reserved journal ownership. The canonical checkpoint contract generates both transports' constants and its packaged schema, including Goal/frontier/invoice definitions.

Transaction family admission: **Problem:** the WAL family codec rejected the 513th record even when the complete atomic envelope fit its declared byte representation. **Decision:** builder, encoder and recovery decoder share the actual u16 count representation, with no private 512-record quota; decode validates that the claimed count is supported by encoded nonempty records before allocation. Canonical ordering, singleton-family uniqueness, reserved bytes, payload lengths, block references and atomic envelope checks remain authoritative. **Alternatives considered:** increasing the quota merely moves refusal; splitting commits changes atomicity. The executable regression submits 1,101 stream-family records through the builder, complete envelope codec, real WAL commit and engine reopen, verifies every ordered payload, and rejects corrupted counts and u16 overflow. The independent inline-envelope byte admission still requires a separate spill/format review.

Event batch admission: **Problem:** SQL and native entries, native IR, indexed application and authority staging each imposed a 500-item/stream quota, refusing valid work before storage could commit it. **Decision:** all five stages validate nonempty typed batches without a count quota. Individual event encodings, stream positions, authenticated scope, exact request replay and one atomic commit remain authoritative. **Alternatives considered:** enlarging 500 merely moves refusal; splitting the business batch into commits permits partial publication. The real semantic regression submits 513 distinct streams, replays the exact command, reopens the engine and verifies every event plus foreign-owner isolation. Independent byte, referenced-block and encoding boundaries remain under separate review.

Stream append demand: **Problem:** the authority staging aggregate and stream preparation refused writes above 8 MiB or 4,096 events, even though immutable segment construction already divides payloads into independently stored pieces and publishes their references atomically. **Decision:** complete appends use existing segmentation without aggregate count/byte quotas. The u32 event counts and u16 segment counts remain explicit codec representations; decoders validate claimed counts against encoded bytes before allocation. **Alternatives considered:** larger quotas move refusal; committing segments separately loses atomicity. The real authority regression stages a 5,001-event, 10,242,048-byte payload stream in two contiguous calls plus a 5,001-event small stream, commits both together, reopens and verifies all 10,002 payloads with paged reads. The focused lifecycle regression completed in 0.08 s on the development host; this synthetic fixture measures correctness under that demand, not deployment throughput. The independent event-size, referenced-block, inline-envelope and semantic IR boundaries require separate review.

Transaction block-reference admission: **Problem:** envelope encoding/recovery, family builders, staged blobs, persistent snapshots/restores and entity merge selection applied an extra 2,048-reference quota even though the WAL count uses u32 and exact envelope lengths already fence recovery allocation. **Decision:** those stages use checked actual u32 count representation, with no private block-count quota. Exact references, block integrity, carried-payload spill, durability and atomic publication remain authoritative. **Alternatives considered:** larger quotas merely move refusal; splitting transactions changes atomicity. A real engine regression commits 2,101 distinct payload blocks, verifies family reference deduplication and both envelope forms, rejects a corrupted claimed count, reopens and reads every exact payload. A second real authority regression stages 2,101 distinct blob graphs plus their entity locators in one transaction, reopens and verifies every locator and blob payload. Independent WAL record and inline-payload encodings remain under review.

Transaction IR step admission: **Problem:** a fixed 128-step validation quota rejected otherwise valid ordered compositions before the authority could apply them. **Decision:** interpret the complete finite ordered step vector after the existing owner, operation-kind, typed-step, slot-dependency, byte-overflow and command-effect checks; no step-count quota remains. **Alternatives considered:** increasing 128 moves refusal; committing groups separately exposes partial state. The executable IR regression validates 1,002 ordered steps, forces a stale stream-position conflict after 1,001 staged entity writes, proves unchanged WAL and absent entity values, then commits with the correct witness and reopens to verify every entity and stream. Slot addressing and literal-byte admission still require separate review.

Transaction IR slot admission: **Problem:** fixed 64-cell arrays and u8 identifiers rejected valid compositions and reserved unused payload/proof cells on every interpretation. **Decision:** in-process slot identifiers are usize and payloads, initialization witnesses and response proofs use demand-owned sparse cells; memory grows with touched identities, never the greatest identifier. Read misses do not allocate. Mutation invalidates JSON/receipt proofs before exposing bytes, and duplicate initialization, unread dependencies, foreign ownership and command authority remain checked. **Alternatives considered:** larger arrays move refusal and retain fixed residency; dense growth by identifier permits a sparse large ID to allocate unrelated memory. Cells end with the interpretation and selected response allocations transfer without cloning. Literal-byte admission remains under separate review.
The sparse-slot regression initializes 514 identities (513 contiguous plus usize::MAX), applies 513 conditional writes, rejects duplicate and unread identities, commits and reopens to verify every written payload. A proof regression uses usize::MAX as the sole touched identity, verifies zero allocation of slot cells on a read miss, one resident payload cell after mutation, and receipt-proof invalidation before mutable byte access.
The measured empty interpreter slot-storage header is 112 bytes on the development host, excluding heap cells and payload buffers. The usize::MAX fixture retains one touched payload cell; untouched identities allocate no cells. These measurements describe storage layout and demand handling, not throughput.

Transaction IR literal demand: **Problem:** the aggregate IR validator refused compositions or literal responses above 8 MiB, independently of their typed storage encodings. One misleading shared constant also represented separate old domain-document limits. **Decision:** the IR tracks complete identity/value/result lengths with checked usize arithmetic and no aggregate byte quota; typed validation, owner/slot/command authority and atomic commit remain mandatory. The retained 8 MiB domain limits are explicitly named `MAX_LEGACY_DOCUMENT_BYTES` and `maximumLegacyDocumentBytes`, not represented as an IR aggregate contract; eliminating those remaining domain limits is unfinished work. **Alternatives considered:** raising the aggregate limit moves refusal; silently splitting commits publishes partial state. The real regression validates 1,501 inline-safe 7,000-byte writes (10,507,000 payload bytes) plus a 9 MiB literal response, forces a late stream-witness conflict and proves no WAL/entity publication, then commits and reopens to verify every payload and exact response digest.

Generic versioned-document admission: **Problem:** generic hot writes, ordinary writes, fresh inserts, reads and list projections shared an unrelated 8 MiB legacy domain quota; bounded reads also silently clamped a larger explicit caller allowance back to 8 MiB. **Decision:** the generic document owner stages large values through its existing authenticated immutable blob graph and materializes complete values/projections without a private byte quota; bounded variants honor the caller's declared allowance. Timestamp/version/identity/CAS, JSON proof, blob digest and owner checks remain. Page assembly checks representation overflow and actual allocator failure. **Alternatives considered:** larger default caps move refusal; truncating documents breaks value integrity. Regressions commit and reopen two 9 MiB documents through known-version and fresh paths, verify a larger explicit read allowance, and exercise real admitted record queries returning one full document and a complete two-document list plus foreign-owner absence. Wire admission and separate domain-specific caller budgets remain unfinished work.

Streamed transport admission: **Problem:** Rust request assembly/response emission and Python peers rejected aggregates above 64 MiB or 64 chunks although framing already supports bounded per-chunk windows. The embedded path inherited the same aggregate gate. **Decision:** complete streams follow actual u64 total-byte/u32 ordinal representations, without aggregate byte/count quotas. Per-frame/chunk windows, exact authenticated identity and sequence, declared-total/final/terminator agreement, deadlines and reservation release remain mandatory. Embedded borrowed buffers reject unaddressable Rust slice lengths before dereference. **Alternatives considered:** larger aggregate quotas move refusal; truncating data breaks message integrity. Native regressions assemble a complete 70 MiB request across 70 chunks with owned-reservation release and emit/reassemble a 70 MiB+7-byte response across 71 chunks plus its terminator, preserving exact digest and released frame accounting. Domain-specific JSON/storage limits remain under review.
Python real-loopback regressions transmit a 70 MiB request through more than 64 frames and receive a 70 MiB response through 71 chunks, checking complete contents, final termination and released accounting. Embedded regressions accept a valid query carrying 70 MiB of JSON input, continue classifying invalid large payload shapes as semantic errors, and reject unaddressable borrowed-buffer lengths before dereference.

Frame memory hints: **Problem:** the native planner rejected targets below the 8 MiB frame window, and launch probing forced that minimum above observed headroom, despite existing anonymous-file overflow. Old machine-contract prose also described retired 64-connection/frame/stream quotas. **Decision:** every usize RAM preference, including zero/file-only, is valid; observed headroom divided by the declared divisor defines the hint directly. Missing probes keep the declared lean fallback. Identity, framing, owned payloads, lifecycle accounting and actual I/O/allocation failures remain authoritative. **Alternatives considered:** lowering a fixed minimum leaves an arbitrary refusal; disabling large frames starves valid work already supported by file overflow. Regressions exercise 0/1/1,024-byte and sub-frame targets, prove file-buffer selection and full reservation release, round-trip a real 4 KiB payload with a zero RAM-frame hint, and verify launch pressure can produce a zero hint without rejecting work. Connection numbers are initial reservation/reuse targets, not active-work admission.

Indexed event payload admission: **Problem:** compile, append, carried Turn events, blob hydration and replay projection enforced separate 8 MiB gates although large logical events already use the generic authenticated blob owner. **Decision:** complete logical event payloads and requested replay rows use that existing graph with no private byte quota; the physical small-stream payload remains a verified reference. Duplicate JSON parsing for the former replay-size preflight is removed. Natural-key conflicts, exact digest/type/sequence/owner witnesses, malformed JSON, missing graphs and GC/prune semantics remain authoritative; page assembly checks overflow and actual allocation failure. **Alternatives considered:** larger gates move refusal; partial event bodies destroy replay integrity. The real admitted semantic regression appends two 9 MiB events, deduplicates both, reopens and verifies every byte plus metadata in the complete roughly 18 MiB replay and foreign-owner absence. Remaining whitespace-variant aggregate IR filters are also removed so those compositions retain checked size arithmetic without a hidden old quota. Other domain limits remain under review.

Optimizer history admission: **Problem:** online creation, migration restore and index scans refused or misclassified history above 4,096 proposals / 2,048 action audit rows. **Decision:** exact u64 counts use checked arithmetic without private owner-row quotas; creation and restore retain one atomic document/index/count publication. Ordinary proposal/action lists scan only their requested index window and latest-action lookup scans one covering entry; complete expiration reads page to actual EOF without a saved-history cap. **Alternatives considered:** larger quotas move refusal; evicting audit records loses durable user state. Real regressions create history beyond both former caps, reopen, verify boundary identities, exact counts, ordinary ordered lists, complete expired actions and foreign-owner absence. Field/document/response admission is described below.
The development-host lifecycle regression records 4,202 durable proposal rows and 2,202 action rows, including exact migration replay and a complete 2,202-row expired-action result. Requested two-row lists and one-row latest-action lookups retain only their requested covering-index windows; these logical row measurements do not claim production throughput.

Optimizer payload admission: **Problem:** independent 500/4,000/2,000,000-character field limits, 4 MiB documents, 8 MiB responses and 500-row lists refused valid audit history despite the existing blob and segmented transport paths. **Decision:** descriptive text, evidence and metrics use the existing authenticated blob-backed document machinery without private byte quotas; lists accept positive addressable row counts and preserve the 50-row default. Identity/index limits, required fields, finite confidence, owner isolation, atomic publication and corruption checks remain. Admission counts JSON through a writer instead of allocating another serialized copy; aggregate counters retain checked arithmetic. **Alternatives considered:** larger quotas only postpone refusal; truncation or audit eviction loses durable state. Regressions exercise 9 MiB fields, full joined responses, reopen, idempotent offline restore, foreign-owner absence and 1,001-row ordered lists. This increases retained durable bytes with requested content; it creates no resident worker or queue.

Daily-cost demand admission: **Problem:** native 8 MiB documents/month responses and 366-date probes rejected complete cost history; a 100-row month assumption contradicted the retained Unicode-digit date syntax. **Decision:** reuse authenticated blob documents without private byte quotas, page month keys to actual EOF, and accept complete valid date probes. Native compilation deduplicates in linear expected time while preserving request order; SQL probes execute portable 512-bind windows within the existing read transaction and return complete request-order matches. Owner predicates, finite cost, date syntax, exact counts, receipts, outbox and atomic restore stay strict. **Alternatives considered:** raising quotas postpones rejection; calendar-only validation changes accepted identity syntax; truncation produces incomplete billing data. Real regressions cover a 9 MiB document, 110 syntactically valid month dates, 1,003 requested dates, reopen, large idempotent migration and foreign-owner absence. No worker or queue is added; durable bytes scale with retained content and month response memory scales with the requested complete result.

Project Brain collection admission: **Problem:** private work/history, narrative, path, artifact, Watch, Checker, Charter and cursor counts refused activity or dropped retained state during folds, restore/cutover and relink. **Decision:** owner-scoped collections preserve complete history without count quotas, retaining exact identity/immutability conflicts and atomic projection/event publication. Native and SQL folds share this behavior; projection/checkpoint byte quotas are removed and event reads page through the existing u32 chunk format. **Alternatives considered:** raising quotas postpones loss; eviction discards durable or reconstructible user-visible history; gating tasks on the read model adds execution authority. Regressions exercise 121 simultaneous active tasks, terminal history beyond 100, 401 paths, 151 artifacts, 601 narratives, 1,101 delivery cursors and registry/Charter/Watch counts beyond former caps, with reopen, rebuilding and conflict checks. No resident concurrency is added; retained projection memory and durable bytes scale with complete requested history.

Project Brain payload admission: **Problem:** native 8 MiB projection/checkpoint/event gates and one-page chunk decoding rejected larger project history; the offline Watch fold still referenced its retired count quota. **Decision:** reuse authenticated blob documents without private byte quotas and page logical events through the retained digest-bound chunk codec. Each header must agree with its u32 total and chunk count; every source chunk retains sequence, ordinal, length and digest checks. Materialization grows from actual verified chunk bytes rather than claimed header size. Offline Watch fold preserves all source items. **Alternatives considered:** raising quotas postpones refusal; replacing the stream authority creates parallel persistence; a one-page decode cannot prove complete large events. Tests exercise 10,226,589 path bytes through real admitted writes, receipt replay, reopen, complete rebuild and idempotent large restore, plus malformed declarations and incomplete/corrupt streams. No resident concurrency is added; durable content grows with retained input, and decoder memory is the output plus a physical page. Per-field text limits remain under review.


## Native engine external build inputs

**Problem:** omitted project schemas break checkpoint builds and leave stale engine identities. **Decision:** package.metadata.tofu external-inputs declares VERSION/ contracts files; Rust build.rs and Python hash exact bytes under @external/ keys. Owned snapshots mirror inputs and keep a validated ledger for deleting only their obsolete copies. Missing, escaping, duplicate or symlinked inputs refuse. Alternatives: unhashed copies admit stale engines; disabled features lose capabilities; broad copies cross source boundaries. Direct/staged tests cover edits. **Manifest reads — Problem:** discovery and selection reparse manifests in separate children. **Decision:** each fingerprint/layout invocation reuses successful parses with child timeout/failure isolation; later calls read disk afresh. The standard-library parser uses isolated Python startup (`-I -S`): inherited module paths, checkout modules and site hooks cannot execute during source identity. This removes an ambient-code boundary without changing the ten-second child deadline or source hash protocol. **Alternatives:** global caching misses edits; in-process parsing removes isolation; extending child timeouts retains unrelated startup code. `system_native_manifest_child_isolation_20261016.json` retains ambient-import counterexamples and component measurements; the earlier full-cohort timeout cause remains unproven. `system_native_source_manifests_20261015.json` covers identity tests and diagnostic clocks. The ordinary HTTP ROOT fixture accepts an explicit daemon engine, verifies current source and content pin, and records the selected disposable authority. This selectability is not native functional/performance or production admission.


## Sparse Transaction IR slot ownership

The interpreter's slots.rs stores touched usize identifiers in a BTreeMap.
Absent read/index/get_mut returns an unallocated default/miss; mutation owns one
default cell. Storage depends on touched identifiers rather than the greatest
identifier, including usize::MAX; table retirement releases its values. This
implements the canonical sparse slot admission rule without a new slot quota.
Standalone MSRV tests verify sparse allocation and value disposal; full engine
compile/admission remains a separate gate.

Compatibility codecs and migration/control envelopes use the declared
MAX_LEGACY_DOCUMENT_BYTES where they previously borrowed the removed generic
transaction literal constant. The historical and current values are both
8 MiB; these references preserve existing domain error/byte boundaries and do
not reinstate an aggregate Transaction IR literal quota.


## Native domain consumers follow complete-admission contracts

Entity staging and transaction block/family counts validate their u32/u16
representations; corrupt count headers must be supported by actual encoded
bytes before allocation. Stream appends retain complete atomic data with checked
lengths. Domain documents and indexed payloads use the existing owner-bound blob
codec; explicit caller byte limits still govern bounded projections. Project,
workspace and queue full scans continue until an empty page, including short
mount-split pages. Counts and positions check overflow; owner/status/CAS, receipt
and outbox atomicity remain authoritative. Background queue pages keep complete
first oversized items and an exclusive cursor rather than dropping results.

Logical outbox ciphertext/record sizes follow u32 representation. Selected
complete publication batches preserve byte witnesses, scope, contiguity and
exact ACK; work hints do not discard already selected records. Archive sinks
keep inline legacy entries readable and store larger authenticated records in
referenced blocks with exact length/digest descriptors. Failed publication
retains source debt; retries/reopen preserve complete prefixes. Actual ENOSPC
and I/O remain failures; configured legacy size hints are not storage quotas.

**Problem:** consumers still referenced retired quotas and prevented current
engine builds/complete demand. **Decision:** implement the existing canonical
admission vocabulary at consumers. **Alternatives rejected:** restoring obsolete
constants contradicts the contract; discarding oversized rows loses evidence;
relaxing owner/format/durability checks sacrifices authority. Tests cover demand
above former limits, malformed JSON/counts, wrong identities, reopen and faults.
`system_native_domain_compile_coherence_20261013.json` includes
1,809 passing library tests, eight ignored, a matching release build and a real
paid-work ROOT pair on disposable native storage. Performance/default promotion
and remaining domains are not admitted by this functional cohort.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
