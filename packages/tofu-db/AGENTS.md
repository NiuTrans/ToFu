# Tofu-DB engine guidance

## Scope

This package owns the experimental Rust storage engine.  It is pre-authority:
no application startup path may select it until the certification gates in
`docs/STORAGE.md` are implemented and pass.

## Boundaries

- `src/control.rs` exclusively owns the alternating, checksummed `CONTROL`
  slots and their publication order.
- `src/wal.rs` exclusively owns active-log framing, hash chaining, bounded
  recovery, and tail truncation.
- `src/engine.rs` composes those primitives and owns the process lease.
- Never import SQLite/PostgreSQL libraries or expose physical records as the
  application contract.  Future semantic commands compile from the canonical
  contracts under `contracts/`.
- Every allocation, file, queue, worker, and recovery scan needs a hard bound.
  Normal open must never migrate or scan beyond the active 64 MiB log.

## Verification

Run `cargo test --manifest-path packages/tofu-db/Cargo.toml`.  Filesystem or
commit changes must add a deterministic crash/torn-write test before broader
application integration tests.
