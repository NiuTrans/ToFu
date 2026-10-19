# Revision-fenced live Turn text

Problem: compact projection heads still hydrated their checkpoint and patch
prefix on every text frame, and folded that growing document every 64 revisions.
The application also transmitted cumulative text even when only a suffix changed.
Decision: text frames use the existing version-1 projection patch with
at most two distinct top-level `content`/`thinking` string `set` or `append_text`
operations and no `slim` wire flag. This existing request grammar works with
older engines; current authorities preserve its exact wire base without segment
repair when it is an unattested running text-only patch.
Legacy cumulative slim events remain valid; structural patches retain their
normalization boundary. SQL authorities accept the same semantic request.

Native pure text appends of at most 64 KiB keep the checkpoint and structural
patch prefix fixed. A `tofu.turn-text-tail/v1` descriptor in the owning Turn
head tracks exact encoded projection bytes, appended revisions, and the two
page cursors. UTF-8-safe text pages occupy the same owner-qualified projection
head namespace, at most 16 KiB of raw text each. An append reads and updates
only each affected final page plus new pages and the ordinary transaction
headers. Initial conversion materializes the old projection once. Reads,
structural updates, terminal settlement and interrupted recovery hydrate and
validate the frozen prefix and pages; folds and settlement retire the complete
head in their existing atomic transaction. Snapshot readers retain their old
pages. No independent cache, background writer or relaxed acknowledgement is
introduced. Admission retains the existing settleable snapshot/recovery reserve;
larger suffixes and near-limit projections use the ordinary patch path.

Alternatives considered: extending the patch-count ceiling postpones the same
growing replay and fold cost; an independent append log requires another
publication/recovery authority; dropping acknowledged stream events weakens
reconnect and recovery. Text pages keep ownership, WAL publication, backup and
GC under the existing Turn authority. Older native readers cannot hydrate this
new internal head shape. The application producer remains compatible with previous engines. Native
downgrade requires folding all active text heads
through the current engine first. Activation requires matched performance
and release evidence for the exact native build and application producer.

Application baseline lifecycle: pure text acknowledgements update only the
cached root strings and mark segment mirrors as needing normalization. The
existing task-local owner/attempt/revision fence remains. A structural or
terminal write normalizes that baseline once before constructing its attested
patch; the authority still publishes any repair from its exact wire base under
CAS. Initial nonempty channels remain structural boundaries. Failed/stale
writes invalidate the baseline through the existing retry path; terminal
cleanup releases it. The 3 s structural cadence and progress coalescing policy
are unchanged. This moves rendering-structure work to its semantic boundary
without adding another cache or persistent authority.

Verification: native `turn::text_tail::tests`; cross-backend application
contracts; `TOFUDB_BIN=<matched artifact> pytest
tests/tofudb/test_turn_text_backup.py`; native browser stream/tool/abort/reload.
The bounded workload accepts `--carry-stream-events` to verify reconnect rows
and `--terminal-barrier` to compare synchronized settlement.
`settle_scheduled` includes waiting since the scheduled final text frame;
`settle` measures only the invoked terminal path. Full performance evidence
retains both and does not certify terminal-tail or capacity improvements.

Measured scope: final ABBA legs carry reconnect frames in the same durable
transaction, on the same FUSE volume for source and authority. At 1/4/16
conversations, 100 frames of 8 KiB, median commits changed from about
6.8/8.6/18.4 ms to 1.6/2.9/14.4 ms. Peak native and application RSS decreased
at 16 conversations. Exact bodies and replay frames survived every crash leg.
An earlier Turn-only measurement is preserved separately.
Terminal tails remain unresolved: one full-pipeline run showed slower 16-way
settlement. Repeated completion latency from the scheduled last text frame
was 834–1455 ms for baseline and 912–1256 ms for the candidate; ranges overlap.
Synchronized terminal arrival also varied. No terminal-tail or capacity
improvement is certified. Cached metrics snapshots cannot establish isolated
terminal-stage durations by subtraction. Next work examines duplicate event
persistence and terminal payload costs; the replay payload contract is in
[STORAGE_EVENT_REPLAY.md](STORAGE_EVENT_REPLAY.md). Artifact identities, all legs and
limits are in `tofudb_turn_text_pages_20261001.json`.

Producer measurement: a paired CPU-only probe removes 200 segment-normalization
calls from 200 suffix acknowledgements; it uses a fake command response and is
not an end-to-end latency claim. With 12 retained 64 KiB tool results and the
same native artifact, write medians were 1.45–1.47 versus 1.37–1.38 ms. Empty-tool
4/16-way pipeline ranges overlap; no universal improvement is certified.
Concurrent subscriber probes fold exact public patches and retain identical
projections after crash. Single-subscriber query median was 0.27 ms. At16-way,
query RPC P99 was 75–101 ms with profiling disabled; reconciled native dispatch
means were 3.34–3.72 ms and roughly95% waiting. The admission/client path remains
a separate measured bottleneck. Default browser behavior passes. Matched pins,
all workloads and baseline test failures are in
`tofudb_producer_baseline_20261001.json`.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
