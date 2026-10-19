# Context frontier owner

- `../model_context.rs` owns public v2 proof selection and bounded response pages.
- `frontier.rs` owns the private certificate in the existing continuation lane
  directory, authority certification, and the shared header-mutation hook.
- `sha_state.rs` owns the bounded resumable SHA256 state; RustCrypto owns block
  compression. Never reinterpret a finalized digest as resumable source state.
- `tests.rs` exercises actual authorities, OCC, WAL faults, SIGKILL and logical
  history-growth work. Run the focused `model_context` Rust tests, continuation
  migration neighbors, then public context differential tests.
- Contract and measurement boundaries: `docs/TOFUDB_CONTEXT_READ.md`. Public
  proof encoding remains `contracts/model_context_v1.schema.json`.
