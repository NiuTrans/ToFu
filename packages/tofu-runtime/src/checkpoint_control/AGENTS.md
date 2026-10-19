# Shared native checkpoint control

`../checkpoint_control.rs` owns bounded request parsing, response admission and
volatile capability IDs. `runtime.rs` is the explicit authority port; `goal.rs`
reuses the existing whole-set claim/handoff and capability publication rules.
Journal transitions stay in `checkpoint_journal.rs` and `checkpoint_owned.rs`.

Storage implementations own authentication, atomic durable CAS, accepted lineage,
writer-death proof and joined close. No port opens or changes authority. Supply
its existing document limit when creating Registry; preflight output capacity
before effects and never retry ambiguous operations. Preserve the actual TofuDB
C ABI/daemon checkpoint tests when adding a selected SQL adapter. Behavior lives
in `docs/DEVELOPER_RUNTIME.md` and native checkpoint contracts.
