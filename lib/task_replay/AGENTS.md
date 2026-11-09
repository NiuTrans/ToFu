# Task replay contracts and projections

This directory owns shared durable task-event vocabulary and pure read
projections. Storage authorities may import the dependency-free contract and
projection modules; they never import task execution or route owners.

`persistence_projection.py` owns the pure persistence projections (usage and API
round sanitization, tool-round trimming and frame compaction, window projection)
shared by the task engine, the storage-free agent wheel and storage authorities;
it never imports `lib.storage`. Verify `tests/tasks_pkg/test_oversized_field_persist_trim.py`
and `tests/storage_authority_sql/test_storage_projection_compaction.py`.

`request_inspector_projection.py` implements the optional list view specified by
`contracts/request_inspector_projection_v1.json`. Regenerate Python/Rust constants
with `scripts/gen_request_inspector_projection.py`; generated files are not
authoring inputs. Preserve full replay and malformed/legacy behavior. Verify the
shared fixture, inspector cache/detail tests, both SQL backends and the native
semantic projection/owner/reopen tests named in `docs/STORAGE_OPERATIONS.md`.
