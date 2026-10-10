# Overleaf MCP extension

This independently installed process wraps pinned upstream Overleaf tools and
owns short-lived immutable artifact export. It never imports Tofu host modules.
The host owns document authorization, task scheduling, publication and readers.

- Wire authorities are `contracts/paper_build_v1.schema.json` and
  `contracts/project_file_receipts_v1.schema.json` at repository root.
  Run `scripts/generate_paper_build_contract.py`; do not edit the packaged copy.
- Compile once per export. Verify every output URL against the same project
  and remote build; never infer compiled source identity from a later Git/ZIP read.
- Independent export requests have no local active/record/aggregate-byte quota.
  Duplicate identities share one completion; artifact framing and lease lifetimes
  remain explicit. Join producers before closing files on shutdown; retire leases
  on release/expiry, and preserve unrelated leases after genuine I/O failure.
- Treat owner identity as a caller binding, not an authentication credential.
  Artifact access also requires the unguessable lease token. Transport access
  and project authorization must be established by the integrating host.

`git_operations.py` owns Git admission/IPC; `git_worker.py` dispatches in a fresh
private clone. `git_process.py` and `git_resources.py` bound its process tree;
`git_workspace.py` owns cleanup and crash reclamation. `writes.py` confines the
pinned commit seam; `git_push.py` acknowledges exact commits. Task identity and
canonical projection remain host responsibilities. Never reclaim upstream caches.

Verify with `tests/paper/test_overleaf_paper_export.py`,
`tests/tasks_pkg/test_overleaf_write_receipts.py`, `tests/paper/test_overleaf_git_worker.py`, MCP transport integration tests and
`tests/paper/test_paper_build_contract.py` in the root repository.
