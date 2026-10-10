//! Cold-authority read cost gates (2026-09-16/18 timeout-storm incident).
//!
//! Problem: on a high-latency volume every VFS `Open` costs tens of
//! milliseconds, so the first read against a cold entity family is priced by
//! how many files it touches. The production incident multiplied thousands of
//! opens by ~25 ms FUSE latency against a 5 s client deadline; abandoned
//! calls never finished the scan that warms the block cache, so the storm
//! was self-sustaining.
//!
//! These tests gate the *deterministic* half of that cost — the number of
//! `Operation::Open` calls a read issues on a freshly opened authority — so
//! any regression in the cold path fails CI without a wall-clock assertion,
//! and every cold-path optimization must ratchet the ceilings downward.
//! Wall-clock latency per open is environment-specific and stays in the
//! `scripts/tofudb_perf_compare.py` cold-read phase (measurement, not a gate).

use std::io;
use std::path::Path;
use std::sync::Arc;

use serde_json::json;

use crate::authority::AuthorityDatabase;
use crate::generated_tofudb_ir::RECORD_DOCUMENT_NAMESPACE;
use crate::versioned_document::{
    list as document_list, put as document_put, record_composite_key, record_key, PutRequest,
};
use crate::vfs::{DeterministicVfs, Operation, Vfs};

const TENANT: u64 = 1;
const OWNER: u64 = 1;
const LOGICAL_NAMESPACE: &str = "evolution.runs.v1";
/// 600 documents x 64 KiB ≈ 38 MiB: larger than the 32 MiB block cache, so a
/// cold scan cannot be fully pre-warmed by WAL replay. Small enough for CI.
const SEED_DOCUMENTS: usize = 600;

// Measured on this checkout with the seed shape below (cold record.list = 11
// io-ops, cold record.get = 3, warm = 0). Treat as ratchet ceilings: a change
// that raises them is a regression, a change that lowers them is an
// optimization and must update the constant in the same commit.
const COLD_LIST_IO_CEILING: usize = 11;
const COLD_GET_IO_CEILING: usize = 3;
const WARM_IO_CEILING: usize = 0;

/// Sync every directory in the tree so the crash image is complete. The
/// simulated VFS publishes a directory's children only when the parent is
/// synced after the child appears (parent-publish semantics), which is
/// exactly what a journaled filesystem guarantees for real deployments.
fn publish_recursive(vfs: &Arc<DeterministicVfs>, path: &Path) -> io::Result<()> {
    vfs.sync_directory(path)?;
    for child in vfs.read_directory(path)? {
        if vfs.metadata(&child)? == crate::vfs::FileKind::Directory {
            publish_recursive(vfs, &child)?;
        }
    }
    Ok(())
}

fn seed_authority() -> io::Result<Arc<DeterministicVfs>> {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data"))?;
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs)?;
    // Commit in bounded batches so one transaction stays inside the IR cap.
    for batch in 0..(SEED_DOCUMENTS / 100) {
        let mut transaction = database.begin(TENANT, OWNER)?;
        for index in (batch * 100)..(batch * 100 + 100) {
            let logical_key = format!("{OWNER}:run-{index:04}");
            let value = json!({
                "schema_version": 1,
                "run_id": format!("run-{index:04}"),
                "objective": "x".repeat(64 * 1024),
                "status": "completed",
            });
            document_put(
                &database,
                &mut transaction,
                PutRequest {
                    key: record_key(TENANT, OWNER, LOGICAL_NAMESPACE, &logical_key)?,
                    namespace: LOGICAL_NAMESPACE.to_owned(),
                    logical_key,
                    value_json: serde_json::to_vec(&value).unwrap(),
                    expected_version: None,
                    updated_at_ms: 1_700_000_000_000 + index as u64,
                },
            )?;
        }
        database.commit(transaction)?;
    }
    // Retire the WAL so the reopen replays nothing: the production storm
    // state is a large checkpointed store whose blocks recovery never
    // touches, leaving the block cache genuinely cold for the first query.
    database.checkpoint_for_test()?;
    drop(database);
    publish_recursive(&vfs, Path::new("/"))?;
    Ok(vfs)
}

/// Reopen the authority on the same simulated volume: the crash image keeps
/// every synced byte, the block cache starts empty, and the trace resets so
/// only the measured query is counted. On failure the trace tail names the
/// missing artifact instead of an opaque NotFound.
fn reopen_cold(vfs: &Arc<DeterministicVfs>) -> io::Result<AuthorityDatabase> {
    vfs.crash()?;
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    match AuthorityDatabase::open_with_vfs(Path::new("/data"), dyn_vfs) {
        Ok(database) => Ok(database),
        Err(error) => {
            for operation in vfs.trace()?.iter().rev().take(12) {
                eprintln!("trace: {operation:?}");
            }
            Err(error)
        }
    }
}

/// Every VFS touch is one potential network round trip on a high-latency
/// volume: opens for handle misses, pread for cached handles.
fn io_op_count(vfs: &Arc<DeterministicVfs>) -> io::Result<usize> {
    Ok(vfs
        .trace()?
        .iter()
        .filter(|operation| matches!(operation, Operation::Open | Operation::Read))
        .count())
}

fn record_prefix_range() -> io::Result<(crate::entity::EntityKey, crate::entity::EntityKey)> {
    crate::entity::EntityKey::prefix_range(
        TENANT,
        OWNER,
        RECORD_DOCUMENT_NAMESPACE,
        &record_composite_key(LOGICAL_NAMESPACE, &format!("{OWNER}:"))?,
    )
}

#[test]
fn cold_record_list_open_budget_and_warm_collapse() -> io::Result<()> {
    let cold_vfs = seed_authority()?;
    let database = reopen_cold(&cold_vfs)?;
    cold_vfs.arm_fault(None)?; // open itself does reads; count only the query
    let (start, end) = record_prefix_range()?;
    let mut transaction = database.begin(TENANT, OWNER)?;
    let raw = document_list(
        &database,
        &mut transaction,
        &start,
        &end,
        LOGICAL_NAMESPACE,
        5,
    )?;
    let documents: Vec<serde_json::Value> = serde_json::from_slice(&raw).unwrap();
    assert_eq!(documents.len(), 5, "seeded documents must be listable");
    let cold_ops = io_op_count(&cold_vfs)?;
    eprintln!("cold record.list io-ops: {cold_ops} (ceiling {COLD_LIST_IO_CEILING})");
    assert!(
        cold_ops <= COLD_LIST_IO_CEILING,
        "cold record.list touched {cold_ops} files (ceiling {COLD_LIST_IO_CEILING}); \
         a regression here multiplies by per-open latency on network volumes",
    );
    cold_vfs.arm_fault(None)?;
    let mut warm_transaction = database.begin(TENANT, OWNER)?;
    document_list(
        &database,
        &mut warm_transaction,
        &start,
        &end,
        LOGICAL_NAMESPACE,
        5,
    )?;
    let warm_ops = io_op_count(&cold_vfs)?;
    eprintln!("warm record.list io-ops: {warm_ops}");
    assert!(
        warm_ops == WARM_IO_CEILING,
        "warm record.list touched {warm_ops} files; the block cache must make \
         repeat reads free for the keep-warm keeper to hold the line",
    );
    Ok(())
}

#[test]
fn cold_record_get_open_budget_and_warm_collapse() -> io::Result<()> {
    let cold_vfs = seed_authority()?;
    let database = reopen_cold(&cold_vfs)?;
    cold_vfs.arm_fault(None)?;
    let logical_key = format!("{OWNER}:run-0300");
    let key = record_key(TENANT, OWNER, LOGICAL_NAMESPACE, &logical_key)?;
    let mut transaction = database.begin(TENANT, OWNER)?;
    let value = crate::versioned_document::get_value(
        &database,
        &mut transaction,
        &key,
        LOGICAL_NAMESPACE,
        &logical_key,
    )?;
    assert!(value.is_some(), "seeded document must be readable");
    let cold_ops = io_op_count(&cold_vfs)?;
    eprintln!("cold record.get io-ops: {cold_ops} (ceiling {COLD_GET_IO_CEILING})");
    assert!(
        cold_ops <= COLD_GET_IO_CEILING,
        "cold record.get touched {cold_ops} files (ceiling {COLD_GET_IO_CEILING}); \
         a point lookup must stay within tree depth of the store",
    );
    cold_vfs.arm_fault(None)?;
    let mut warm_transaction = database.begin(TENANT, OWNER)?;
    crate::versioned_document::get_value(
        &database,
        &mut warm_transaction,
        &key,
        LOGICAL_NAMESPACE,
        &logical_key,
    )?;
    let warm_ops = io_op_count(&cold_vfs)?;
    eprintln!("warm record.get io-ops: {warm_ops}");
    assert!(
        warm_ops == WARM_IO_CEILING,
        "warm record.get touched {warm_ops} files; the block cache must make \
         repeat reads free",
    );
    Ok(())
}
