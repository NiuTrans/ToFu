# Rootless VM guidance

## Scope and first reads

This package owns the bounded rootless QEMU sandbox and Harbor adapters. Read
`docs/ROOTLESS_VM_SANDBOX.md` and `docs/modules/remote_execution.md`.

## Editing rules

- Preserve the rootless trust boundary. Guest commands, images, mounts,
  loopback services, network routes, and host paths are untrusted inputs.
- Egress is default-deny and policy-driven. Validate every destination after
  resolution and redirect; keep credentials out of guest-visible arguments,
  environment, trajectories, and logs.
- Verify image locks, checksums, formats, and cache lineage before use. Never
  silently substitute an unpinned image or widen a mount.
- CPU, memory, disk, process count, network, stdout/stderr, trajectory, and
  session lifetime are explicit budgets with deterministic teardown.
- Preserve cancellation, parent-death handling, orphan cleanup, integrity
  checks, and failure attribution across host/guest boundaries.
- Harness adapters translate protocols; they do not fork the task or tool
  execution state machines.

## Verification

Run the focused `test_rootless_*`, `test_harbor_*`, egress, image-integrity,
and cleanup tests. Live QEMU smoke tests are opt-in and must use a disposable,
bounded cache and workspace.
