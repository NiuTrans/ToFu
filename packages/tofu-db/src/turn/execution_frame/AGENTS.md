# Turn projection evidence

`witness.rs` owns the bounded, durable `frameWitness` carried by an existing
Turn projection head. It derives facts from actual materialization and updates
them only for supported top-level patches. It is not a new request, authority,
producer batch, process cache or passive replay protocol.

- Production entry: `turn.rs::event_record`; admission, lifecycle/owner fencing,
  checkpoint policy, exact publication and commit remain in the shared owners.
- Semantic proofs reuse `turn_projection_segments::stabilize_for_patch`, the
  existing guidance-preservation helper and `turn_projection_patch` validation.
  New normalizer dependencies must invalidate reuse here before a fast write.
- `VerifiedStableSegments` is transaction-local: reuse it only beside the exact
  unchanged materialized revision/actor/status. Minting may inherit this proof
  only when the applied replay patch leaves every normalizer dependency intact.
  Materialization still verifies all persisted facts before any reuse.
- Missing/future evidence and unsupported transitions use materialization;
  known malformed evidence retains integrity errors. Text-tail and lifecycle
  writers must drop or replace facts when their state changes.
- The containing `execution_frame.rs::record` and its multi-step IR port remain
  `cfg(test)` experiments. Do not infer producer batching from those tests.
- Runtime contracts: `src/turn_execution_frame_runtime_tests.rs`; real producer
  capture: `tests/turn_lifecycle/test_execution_frame_producer.py` and the shared
  root `tests/fixtures/execution_frame_producer.json` fixture. Opt-in work/timing
  cases use the same owner, with explicit profiling and durability settings.
- Decision, limitations and measurements: `docs/TOFUDB_EXECUTION_FRAMES.md`.
