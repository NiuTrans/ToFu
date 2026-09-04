# File history

This domain owns bounded, per-project copy backups and round snapshots used by
undo, redo, external-edit detection, and commit-round file attribution. It does
not choose a project or authorize a browser path: callers must first resolve an
explicit authorized project root. The user’s working files remain the primary
state and are never rewritten by log compaction.

## Ownership

| Concern | Owner |
|---|---|
| Public snapshot, history, diff, undo, redo API | `lib/file_history/api.py` |
| Backup blobs, indexes, JSONL durability, compaction | `lib/file_history/store.py` |
| Pure full-anchor/delta representation | `lib/file_history/snapshot_codec.py` |
| Authorized project paths and undo/redo routing | `lib/project_mod/modifications.py` |
| Atomic task-end snapshot and attribution window | `lib/tasks_pkg/commit_round/_commit.py` |

The store always receives `base_path` explicitly. It holds no process-global
user or tenant identity; authentication and project-root authority stay at the
project boundary rather than being guessed by storage code.

## Write lifecycle

1. `track_edit` normalizes one path below the project root, copies at most 16
   MiB, and atomically advances `tracked.json`.
2. `make_snapshot` normalizes and de-duplicates the round’s declared paths,
   stages their post-images, and builds the logical full `{path: version}` map.
3. `append_snapshot_record` takes the project lock plus the shared JSON-store
   advisory lock. It writes either a full anchor or a v2 delta, flushes and
   fsyncs `snapshots.jsonl`, then publishes the disposable tail index.
4. The commit-round worker diffs the preceding and new maps while still holding
   the same project lock, then filters changes by `last_writer_task_id`.

Every public reader still receives a full snapshot. The v2 encoding is private:
legacy full rows are anchors; a delta names its exact base ID and contains only
changed/removed paths. A full anchor is forced at least every 64 rows and
whenever delta JSON would not save 128 bytes.

## Disk layout and authority

```text
<project>/.tofu/file-history/
  snapshots.jsonl       authoritative retained round history
  tracked.json           authoritative current path/version index
  backups/<hash>@v<n>    authoritative bytes needed by retained undo points
  snapshot-tail.json     reconstructible latest-map/reverse-delta cache
  snapshots.jsonl.lock   advisory cross-process append/rewrite lock
```

`snapshot-tail.json` is trusted only when device, inode, size, and nanosecond
mtime match the log. Deleting it changes only the next-read cost. The in-process
pinned-version LRU is also reconstructible and keyed by the same fingerprint.

## Resource budgets

| Resource | Bound / lifecycle |
|---|---|
| One backup blob | 16 MiB; larger files are recorded as unbacked versions |
| Unpinned versions per path | target 20; version 1, latest, and retained snapshot pins survive |
| One JSONL record | 16 MiB; oversized reads are discarded without buffering the whole line |
| Tail index | 8 MiB; oversize skips the cache, never the authoritative append |
| Delta recovery chain | at most 63 deltas between full anchors |
| Snapshot log | check every 200 rows; rewrite above 64 MiB or beyond the retained-row target |
| Retained snapshots | newest 2,000 after rewrite; valid logs stay below 2,200 between gates |
| Version-reference cache | at most 8 projects and 100,000 path/version pairs process-wide |

Compaction performs two streaming passes rather than retaining every full map:
one counts materializable records, and one writes retained anchors/deltas while
collecting the version pins required for blob GC. It fsyncs the temporary log
before atomic replacement. Old full rows require no migration to remain usable;
the next eligible maintenance rewrite converts retained rows in place.

## Failure semantics

- Malformed JSON is skipped. A wrong-base or malformed delta and all dependent
  deltas fail closed until the next full anchor.
- A torn final append is separated before the next row, so it cannot poison all
  later snapshots.
- Compaction refuses to rewrite when raw non-empty row count differs from the
  materializable count; corrupt evidence is not silently erased.
- A stale, corrupt, missing, or over-budget tail index triggers log replay. A
  cache-write failure cannot turn an already-fsynced append into failure.
- Pinned-version discovery that races a writer returns “uncertain”; version GC
  then deletes nothing.
- Undo/redo lookup uses a metadata-only newest-ID scan. It does not construct
  thousands of history summaries or accept a path from the browser.

## Measured production opportunity

A read-only copy of the 2,162-row legacy log contained 246,370,670 bytes.
Retaining the newest 2,000 rows in the actual v2 codec projected 220,660,523
bytes of full-row JSON to 20,689,094 bytes (90.62%). One full replay took
1.748863 seconds; after the 64 KiB reverse-reader change, nine bounded cold
latest-ID reads had a 0.000554-second median (0.002210-second maximum). The
one-time two-pass encoding took 4.001954 seconds.

The running development process later reached the normal maintenance boundary:
its external-edit probe logged an actual 2,163 → 2,000 rewrite from 246,371,142
to 20,683,387 logical JSONL bytes (91.60%) and removed 743 unreferenced blobs.
No manual migration or restart was performed. On the resulting 2,010-row v2
log, five full replays had a 0.102553-second median (94.14% below the legacy
copy), while 25 latest-ID and adjacent-diff reads had 0.000578- and
0.000574-second medians respectively.

Pinned-version discovery over that copy took 2.371968 seconds once and produced
16,030 distinct path/version pairs. Nine subsequent path lookups together took
0.000053 seconds from the bounded cache. The first paragraph is a read-only
counterfactual; the second reports observed local maintenance and logical file
size, not API traffic or physical-device block reclamation.

## Verification

```bash
pytest -q tests/test_file_history_snapshot_log.py
pytest -q tests/test_file_history_compaction.py tests/test_file_history_external_edit_guards.py
pytest -q tests/test_commit_round_daemon.py tests/test_project_undo_redo_concurrent_resolution.py
```

The snapshot-log tests pin legacy/v2 parity, byte reduction, anchors, corrupt
chains, torn lines, cache reconstruction and budgets, single-scan version pins,
metadata lookup, and compaction refusal. Compaction tests retain blob and rewind
semantics; commit-round tests retain the cross-conversation attribution lock.
