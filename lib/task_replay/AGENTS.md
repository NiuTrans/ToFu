# Task replay contracts and projections

This directory owns shared durable task-event vocabulary and pure read
projections. Storage authorities may import the dependency-free contract and
projection modules; they never import task execution or route owners.

`request_inspector_projection.py` implements the optional list view specified by
`contracts/request_inspector_projection_v1.json`. Regenerate Python/Rust constants
with `scripts/gen_request_inspector_projection.py`; generated files are not
authoring inputs. Preserve full replay and malformed/legacy behavior. Verify the
shared fixture, inspector cache/detail tests, both SQL backends and the native
semantic projection/owner/reopen tests named in `docs/STORAGE_OPERATIONS.md`.
