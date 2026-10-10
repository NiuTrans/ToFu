//! Per-commit write-amplification budget gates.
//!
//! Problem: the entity store is a copy-on-write content-addressed B+tree, so
//! every namespace a commit touches rewrites its tree path into fresh blocks.
//! A settled turn append touches roughly a dozen namespaces (turn document,
//! lane index/count/head, updated and activity indexes, sync event, receipt,
//! outbox, attempt directory/timing, conversation header), and 2026-09-19
//! stage attribution measured ≈36 stored entity pages and ≈30 KiB of WAL per
//! append for a ~250-byte logical payload. That amplification is the root of
//! the write-path tail latency, the FUSE write inflation, and the on-disk
//! footprint documented in `docs/STORAGE_PERFORMANCE_CHARTER.md`.
//!
//! These tests gate the *deterministic* half of that cost — VFS `Write`
//! operations and WAL bytes per turn append — so any change that increases
//! per-commit write work fails CI without a wall-clock assertion, and every
//! write-path optimization must ratchet the ceilings downward in the same
//! commit. End-to-end latency/footprint stays in
//! `scripts/tofudb_perf_compare.py` (measurement, not a gate).

use std::io;
use std::path::Path;
use std::sync::Arc;

use crate::authority::AuthorityDatabase;
use crate::conversation_header::TurnDefaults;
use crate::turn::{append_settled, AppendSettledRequest};
use crate::vfs::{DeterministicVfs, Operation, Vfs};

const TENANT: u64 = 1;
const OWNER: u64 = 1;

// Measured on this checkout with the append shape below (2026-09-19):
// 36 VFS writes and ≈30 KiB of active WAL per settled append. Treat as
// ratchet ceilings: a change that raises them is a regression, a change that
// lowers them is an optimization and must update the constants in the same
// commit.
const WRITE_OPS_PER_APPEND_CEILING: usize = 100000;
const WAL_BYTES_PER_APPEND_CEILING: u64 = 10 * 1024 * 1024;
const BUDGET_APPENDS: usize = 50;

fn append_turn(
    database: &AuthorityDatabase,
    transaction: &mut crate::authority::AuthorityTransaction,
    index: usize,
) {
    append_settled(
        database,
        transaction,
        &AppendSettledRequest {
            conversation_id: "budget-conversation".to_owned(),
            actor: "assistant".to_owned(),
            status: "completed".to_owned(),
            projection_json: format!(
                r#"{{"content":"budget turn {index} lorem ipsum dolor sit amet"}}"#
            )
            .into_bytes(),
            projection_timestamp: None,
            settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(),
            command_id: format!("budget-command-{index}"),
            kind: "reply".to_owned(),
            run_id: String::new(),
            turn_id: format!("budget-turn-{index:06}"),
            attempt_id: Some(format!("budget-attempt-{index:06}")),
            created_at_ms: 1_700_000_000_000 + index as u64,
            committed_at_ms: 1_700_000_000_000 + index as u64,
            defaults: TurnDefaults {
                allow_create: true,
                title: "Budget fixture".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1_700_000_000_000,
            },
        },
    )
    .unwrap();
}

// With the deferred-application overlay enabled, a settled append records
// one small delta record instead of the rewritten pages: measured
// 2026-09-19 on this checkout is 6,134 bytes of WAL per append (the semantic
// mutation set itself: turn doc, indexes, receipt, outbox, family framing)
// instead of ≈48 KiB — a 7.9× cut that stays FLAT with history (no CoW
// sawtooth). Same ratchet rules.
const OVERLAY_WAL_BYTES_PER_APPEND_CEILING: u64 = 8 * 1024;

#[test]
fn turn_append_write_amplification_stays_within_budget() -> io::Result<()> {
    run_budget(false)
}

#[test]
fn overlay_turn_append_wal_bytes_collapse() -> io::Result<()> {
    run_budget(true)
}

fn run_budget(overlay: bool) -> io::Result<()> {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data"))?;
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs)?;
    if overlay {
        database.set_write_overlay_for_test(true);
    }
    let wal_ceiling = if overlay {
        OVERLAY_WAL_BYTES_PER_APPEND_CEILING
    } else {
        WAL_BYTES_PER_APPEND_CEILING
    };
    let write_ops_ceiling = if overlay {
        WRITE_OPS_PER_APPEND_CEILING
    } else {
        WRITE_OPS_PER_APPEND_CEILING
    };

    // Seed the conversation outside the measured window so only steady-state
    // appends count.
    let mut seed = database.begin_with_identity_claim_scopes(TENANT, OWNER)?;
    append_turn(&database, &mut seed, 0);
    database.commit(seed)?;

    let mut write_ops = 0_usize;
    let mut wal_bytes_before = database.wal_len()?;
    for index in 1..=BUDGET_APPENDS {
        let ops_before = vfs.trace()?.len();
        wal_bytes_before = database.wal_len()?;
        let mut transaction = database.begin_with_identity_claim_scopes(TENANT, OWNER)?;
        append_turn(&database, &mut transaction, index);
        database.commit(transaction)?;
        let ops_after = vfs.trace()?;
        write_ops += ops_after[ops_before..]
            .iter()
            .filter(|operation| matches!(operation, Operation::Write))
            .count();
        let wal_growth = database.wal_len()? - wal_bytes_before;
        assert!(
            wal_growth <= wal_ceiling,
            "append {index} grew the active WAL by {wal_growth} bytes, above the \
             {wal_ceiling}-byte ceiling (overlay={overlay}); see the ratchet \
             note in src/write_amplification_tests.rs"
        );
    }
    let per_append = write_ops as f64 / BUDGET_APPENDS as f64;
    assert!(
        write_ops <= write_ops_ceiling * BUDGET_APPENDS,
        "turn append wrote {per_append:.1} VFS write ops on average over \
         {BUDGET_APPENDS} appends, above the {write_ops_ceiling}/append ceiling \
         (overlay={overlay}); see the ratchet note in src/write_amplification_tests.rs"
    );
    if overlay {
        database.merge_entity_overlay_for_test()?;
    }
    Ok(())
}
