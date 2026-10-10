//! Explicit-path and deterministic-VFS filesystem certification matrix.
//!
//! The matrix runs the existing filesystem/durability certification across
//! cells and reports one machine-readable line per cell:
//!
//! - `primary.path`: the existing cross-process `certify-filesystem` flow
//!   against an empty primary directory, retaining the store as evidence.
//! - `primary.path.at-rest`: the same cross-process flow behind the
//!   block-at-rest encryption seam, deriving a fixed harness key and
//!   propagating it to the child stages through `TOFU_DB_AT_REST_KEY`,
//!   proving encrypted blocks/WAL at rest recover correctly.
//! - `mount.opt-in`: the same flow against an explicit operator path (the
//!   network/FUSE mount under evaluation). This real-mount cell runs only
//!   behind the `TOFU_DB_CERT_MATRIX_MOUNT_DIR` environment opt-in; the
//!   `mount.opt-in.at-rest` cell repeats it encrypted beneath `at-rest/`.
//! - `vfs-fault.*`: deterministic in-process cells injecting every observed
//!   syscall error, every short write, and every lost sync into a commit +
//!   checkpoint workflow, then proving crash recovery restores one complete
//!   prefix. The `vfs-fault.lost-sync.correlated-all[.at-rest]` cells widen
//!   this to arbitrary device sync violations: every single dropped sync,
//!   every pair, and the full correlated set (all commit+checkpoint barrier
//!   syncs dropped in one crash window), admitting only a complete prefix or
//!   a fail-closed reopen.
//!
//! Wall-clock numbers are per-cell observations for controlled same-volume
//! comparisons; they are neither release certification nor a performance
//! claim.

use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::at_rest::AtRestKey;
use crate::certification::{certify_filesystem, certify_filesystem_at_rest};
use crate::engine::{BatchTransaction, Engine};
use crate::generated_tofudb_ir::{
    FILESYSTEM_CERTIFICATION_MATRIX_FORMAT, FILESYSTEM_CERTIFICATION_MATRIX_MAX_DETAIL_CHARACTERS,
    FILESYSTEM_CERTIFICATION_MATRIX_MAX_TRACE_OPERATIONS,
    FILESYSTEM_CERTIFICATION_MATRIX_MOUNT_ENV,
};
use crate::vfs::{
    sync_directory_barrier, DeterministicVfs, FaultAction, FaultRule, Operation, Vfs,
};

/// Environment variable naming the explicit real-mount (network/FUSE) cell
/// target. The directory must exist and be empty; its store is retained.
pub const CERT_MATRIX_MOUNT_ENV: &str = FILESYSTEM_CERTIFICATION_MATRIX_MOUNT_ENV;

const MATRIX_FIRST_INLINE: &[u8] = b"tofu-db.certification-matrix.first";
const MATRIX_SECOND_INLINE: &[u8] = b"tofu-db.certification-matrix.second";
const MATRIX_BLOCK_BYTES: usize = 8 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MatrixCellOutcome {
    Pass,
    Fail,
    Skipped,
}

impl MatrixCellOutcome {
    fn as_str(self) -> &'static str {
        match self {
            Self::Pass => "pass",
            Self::Fail => "fail",
            Self::Skipped => "skipped",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MatrixCellReport {
    pub cell: String,
    pub outcome: MatrixCellOutcome,
    pub cases: u64,
    pub failures: u64,
    pub detail: String,
    pub micros: u64,
}

impl MatrixCellReport {
    pub fn format_line(&self) -> String {
        format!(
            "format={FILESYSTEM_CERTIFICATION_MATRIX_FORMAT} cell={} result={} cases={} failures={} micros={} detail={} measurements_are_observations_not_release_certification=true",
            self.cell,
            self.outcome.as_str(),
            self.cases,
            self.failures,
            self.micros,
            self.detail
        )
    }
}

fn elapsed_micros(duration: Duration) -> u64 {
    u64::try_from(duration.as_micros()).unwrap_or(u64::MAX)
}

fn invalid_input(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn sanitized_detail(detail: String) -> String {
    detail
        .chars()
        .take(FILESYSTEM_CERTIFICATION_MATRIX_MAX_DETAIL_CHARACTERS)
        .map(|character| {
            if character.is_whitespace() || character == '=' {
                '_'
            } else {
                character
            }
        })
        .collect()
}

fn empty_directory(path: &Path) -> io::Result<bool> {
    Ok(fs::read_dir(path)?.next().is_none())
}

/// Deterministic launch secret for the matrix's encrypted cells. The
/// operator-facing `certify-filesystem` flow reads the key only from the
/// bounded environment secret; the matrix is a certification harness, so it
/// derives an equivalent fixed key and propagates it to the child stages
/// through `TOFU_DB_AT_REST_KEY`, mirroring the launch mechanism (children
/// inherit the parent environment, consume the variable, and remove it).
const MATRIX_AT_REST_SECRET: &str = "tofu-db-certification-matrix-at-rest-key-v1";

fn matrix_at_rest_key() -> AtRestKey {
    AtRestKey::derive(MATRIX_AT_REST_SECRET.as_bytes()).expect("matrix at-rest secret is valid")
}

fn real_mount_cell(cell: &str, target: PathBuf) -> MatrixCellReport {
    let started = Instant::now();
    let result = if cell.ends_with(".at-rest") {
        env::set_var("TOFU_DB_AT_REST_KEY", MATRIX_AT_REST_SECRET);
        let result = certify_filesystem_at_rest(&target, MATRIX_AT_REST_SECRET.as_bytes());
        env::remove_var("TOFU_DB_AT_REST_KEY");
        result
    } else {
        certify_filesystem(&target)
    };
    match result {
        Ok(report) => MatrixCellReport {
            cell: cell.to_owned(),
            outcome: MatrixCellOutcome::Pass,
            cases: 1,
            failures: 0,
            detail: format!(
                "authority_uuid:{},durable_sequence:{},at_rest:{},retained_file_count:{},retained_file_bytes:{}",
                report.authority_uuid,
                report.durable_sequence,
                report.at_rest,
                report.retained_file_count,
                report.retained_file_bytes
            ),
            micros: elapsed_micros(started.elapsed()),
        },
        Err(error) => MatrixCellReport {
            cell: cell.to_owned(),
            outcome: MatrixCellOutcome::Fail,
            cases: 1,
            failures: 1,
            detail: sanitized_detail(format!("error:{error}")),
            micros: elapsed_micros(started.elapsed()),
        },
    }
}

fn matrix_block_payload() -> Vec<u8> {
    (0..MATRIX_BLOCK_BYTES)
        .map(|index| ((index as u64 * 57 + index as u64 / 131) & 0xff) as u8)
        .collect()
}

fn phase_batch() -> Vec<BatchTransaction> {
    vec![
        BatchTransaction {
            inline_payload: MATRIX_FIRST_INLINE.to_vec(),
            block_payloads: vec![matrix_block_payload()],
        },
        BatchTransaction {
            inline_payload: MATRIX_SECOND_INLINE.to_vec(),
            block_payloads: Vec::new(),
        },
    ]
}

fn is_sync(operation: &Operation) -> bool {
    matches!(
        operation,
        Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
    )
}

fn prepared_simulated_vfs() -> io::Result<Arc<DeterministicVfs>> {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data"))?;
    vfs.sync_directory(Path::new("/"))?;
    Ok(vfs)
}

#[cfg(test)]
#[allow(dead_code)]
fn initialize_fault_fixture(vfs: &Arc<DeterministicVfs>) -> io::Result<Engine> {
    initialize_fault_fixture_with_key(vfs, None)
}

fn initialize_fault_fixture_with_key(
    vfs: &Arc<DeterministicVfs>,
    key: Option<AtRestKey>,
) -> io::Result<Engine> {
    let engine = Engine::initialize_with_vfs_and_key(Path::new("/data"), vfs.clone(), key)?;
    // Random payload-segment IDs change manifest hashes. If two block hashes
    // share a shard, ensure_shard takes a different I/O branch. Pre-create the
    // bounded 256 empty shards so every replay has the same operation trace;
    // fresh-shard publication remains covered by the block fault tests and
    // the real-path certification cells.
    let blocks = Path::new("/data/blocks");
    for shard in 0..=u8::MAX {
        vfs.create_dir(&blocks.join(format!("{shard:02x}")))?;
    }
    sync_directory_barrier(vfs.as_ref(), blocks)?;
    Ok(engine)
}

/// Runs the commit + checkpoint phase under one optional fault, crashes, and
/// reports whether reopen recovers exactly one complete committed prefix.
#[cfg(test)]
fn fault_case_recovers_complete_prefix(fault: Option<FaultRule>) -> io::Result<bool> {
    fault_case_recovers_complete_prefix_with_key(fault, None)
}

/// The encrypted variant: the same fault case behind the block-at-rest
/// encryption seam, proving a crash or torn write under encryption recovers
/// only a complete prefix.
fn fault_case_recovers_complete_prefix_with_key(
    fault: Option<FaultRule>,
    key: Option<AtRestKey>,
) -> io::Result<bool> {
    let vfs = prepared_simulated_vfs()?;
    let acknowledged_sequence;
    {
        let mut engine = initialize_fault_fixture_with_key(&vfs, key.clone())?;
        vfs.arm_fault(fault)?;
        acknowledged_sequence = if engine.commit_batch(&phase_batch()).is_ok() {
            2
        } else {
            0
        };
        let _ = engine.checkpoint();
    }
    if let Some(rule) = fault {
        let trace = vfs.trace()?;
        let Some(operation) = trace.get(rule.operation_number as usize - 1) else {
            return Ok(false);
        };
        if matches!(rule.action, FaultAction::ShortWrite(_)) && operation != &Operation::Write
            || matches!(rule.action, FaultAction::DropSync) && !is_sync(operation)
        {
            return Ok(false);
        }
    }
    vfs.crash()?;
    vfs.arm_fault(None)?;
    let engine = Engine::open_with_vfs_and_key(Path::new("/data"), vfs, key)?;
    recovered_phase_is_complete(&engine, acknowledged_sequence)
}

fn recovered_phase_is_complete(engine: &Engine, acknowledged_sequence: u64) -> io::Result<bool> {
    let durable_sequence = engine.state().durable_sequence;
    // Individual frames of an unacknowledged group may survive; every
    // acknowledged frame must survive, including all of its block payloads.
    if durable_sequence < acknowledged_sequence
        || durable_sequence > 2
        || engine.state().checkpoint_sequence > durable_sequence
    {
        return Ok(false);
    }
    let snapshot = engine.transaction_snapshot()?;
    if snapshot.len() as u64 != durable_sequence {
        return Ok(false);
    }
    for (index, (actual, expected)) in snapshot.iter().zip(phase_batch()).enumerate() {
        if actual.sequence != index as u64 + 1
            || actual.envelope.inline_payload != expected.inline_payload
            || actual.envelope.block_ids.len() != expected.block_payloads.len()
        {
            return Ok(false);
        }
        for (block_id, payload) in actual
            .envelope
            .block_ids
            .iter()
            .zip(expected.block_payloads)
        {
            if engine.read_block(*block_id)? != payload {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

/// Outcome of one correlated multi-sync-loss crash. The acknowledged group
/// either survives whole, is lost whole (an earlier complete prefix), or the
/// reopen fails closed; anything else is a torn state and a certification
/// failure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CorrelatedLossOutcome {
    RecoveredCompletePrefix,
    FailedClosed,
    Torn,
}

/// Drops every sync in `operation_numbers` in one crash window, crashes, and
/// classifies recovery. Reopen failing closed is a certified outcome; a
/// reopened store must sit on a complete prefix (the whole acknowledged group
/// or the empty pre-commit state) with every acknowledged payload intact.
fn correlated_lost_sync_case_outcome(
    operation_numbers: &[u64],
    key: Option<AtRestKey>,
) -> io::Result<CorrelatedLossOutcome> {
    let vfs = prepared_simulated_vfs()?;
    {
        let mut engine = initialize_fault_fixture_with_key(&vfs, key.clone())?;
        let rules: Vec<FaultRule> = operation_numbers
            .iter()
            .map(|operation_number| FaultRule {
                operation_number: *operation_number,
                action: FaultAction::DropSync,
            })
            .collect();
        vfs.arm_faults(&rules)?;
        let _ = engine.commit_batch(&phase_batch());
        let _ = engine.checkpoint();
    }
    {
        let trace = vfs.trace()?;
        for operation_number in operation_numbers {
            let Some(operation) = trace.get(*operation_number as usize - 1) else {
                return Ok(CorrelatedLossOutcome::Torn);
            };
            if !is_sync(operation) {
                return Ok(CorrelatedLossOutcome::Torn);
            }
        }
    }
    vfs.crash()?;
    vfs.arm_fault(None)?;
    let engine = match Engine::open_with_vfs_and_key(Path::new("/data"), vfs, key) {
        Ok(engine) => engine,
        Err(_) => return Ok(CorrelatedLossOutcome::FailedClosed),
    };
    let durable_sequence = engine.state().durable_sequence;
    // A complete prefix is the empty pre-commit state or the whole
    // acknowledged group; an intermediate sequence is a torn group.
    if durable_sequence != 0 && durable_sequence != 2 {
        return Ok(CorrelatedLossOutcome::Torn);
    }
    match recovered_phase_is_complete(&engine, durable_sequence) {
        Ok(true) => Ok(CorrelatedLossOutcome::RecoveredCompletePrefix),
        Ok(false) => Ok(CorrelatedLossOutcome::Torn),
        Err(_) => Ok(CorrelatedLossOutcome::FailedClosed),
    }
}

/// Enumerates acknowledged-durability cases under arbitrary device sync
/// violations: every single dropped sync, every pair, and the full correlated
/// set (all commit+checkpoint barrier syncs dropped at once). Each case must
/// recover a complete prefix or fail closed; a torn state fails the cell.
fn run_correlated_lost_sync_family(
    cell: &str,
    syncs: &[u64],
    key: Option<AtRestKey>,
) -> io::Result<MatrixCellReport> {
    let started = Instant::now();
    let mut case_sets: Vec<Vec<u64>> = Vec::new();
    for &single in syncs {
        case_sets.push(vec![single]);
    }
    for first in 0..syncs.len() {
        for second in first + 1..syncs.len() {
            case_sets.push(vec![syncs[first], syncs[second]]);
        }
    }
    if syncs.len() > 2 {
        case_sets.push(syncs.to_vec());
    }
    let mut failures = u64::from(case_sets.is_empty());
    let mut first_failure = String::from(if case_sets.is_empty() {
        "no_fault_cases"
    } else {
        "none"
    });
    for case_set in &case_sets {
        let outcome = correlated_lost_sync_case_outcome(case_set, key.clone());
        if !matches!(
            outcome,
            Ok(CorrelatedLossOutcome::RecoveredCompletePrefix)
                | Ok(CorrelatedLossOutcome::FailedClosed)
        ) {
            failures += 1;
            if first_failure == "none" {
                first_failure = format!(
                    "operations:{case_set:?},outcome:{:?}",
                    outcome.map_err(|error| error.kind())
                );
            }
        }
    }
    Ok(MatrixCellReport {
        cell: cell.to_owned(),
        outcome: if failures == 0 {
            MatrixCellOutcome::Pass
        } else {
            MatrixCellOutcome::Fail
        },
        cases: case_sets.len() as u64,
        failures,
        detail: format!("first_failure:{first_failure}"),
        micros: elapsed_micros(started.elapsed()),
    })
}

fn baseline_phase_trace() -> io::Result<Vec<Operation>> {
    baseline_phase_trace_with_key(None)
}

/// The encrypted baseline: the commit+checkpoint phase behind the
/// block-at-rest seam. Region re-sealing changes the VFS operation sequence
/// relative to plaintext, so the encrypted fault cells must index faults by
/// THIS trace, not the plaintext one, or ShortWrite/DropSync land on the
/// wrong operations and recovery is misclassified.
fn baseline_phase_trace_with_key(key: Option<AtRestKey>) -> io::Result<Vec<Operation>> {
    let vfs = prepared_simulated_vfs()?;
    {
        let mut engine = initialize_fault_fixture_with_key(&vfs, key)?;
        vfs.arm_fault(None)?;
        engine.commit_batch(&phase_batch())?;
        engine.checkpoint()?;
    }
    vfs.trace()
}

fn run_fault_family(cell: &str, cases: &[(u64, FaultAction)]) -> io::Result<MatrixCellReport> {
    run_fault_family_with_key(cell, cases, None)
}

fn run_fault_family_with_key(
    cell: &str,
    cases: &[(u64, FaultAction)],
    key: Option<AtRestKey>,
) -> io::Result<MatrixCellReport> {
    let started = Instant::now();
    let mut failures = u64::from(cases.is_empty());
    let mut first_failure = String::from(if cases.is_empty() {
        "no_fault_cases"
    } else {
        "none"
    });
    for (operation_number, action) in cases {
        let recovered = fault_case_recovers_complete_prefix_with_key(
            Some(FaultRule {
                operation_number: *operation_number,
                action: *action,
            }),
            key.clone(),
        );
        if !matches!(recovered, Ok(true)) {
            failures += 1;
            if first_failure == "none" {
                first_failure = format!(
                    "operation:{operation_number},action:{action:?},error:{:?}",
                    recovered.err().map(|error| error.kind())
                );
            }
        }
    }
    Ok(MatrixCellReport {
        cell: cell.to_owned(),
        outcome: if failures == 0 {
            MatrixCellOutcome::Pass
        } else {
            MatrixCellOutcome::Fail
        },
        cases: cases.len() as u64,
        failures,
        detail: format!("first_failure:{first_failure}"),
        micros: elapsed_micros(started.elapsed()),
    })
}

fn deterministic_fault_cells() -> io::Result<Vec<MatrixCellReport>> {
    let trace = baseline_phase_trace()?;
    fault_cells_for_trace(&trace)
}

/// Replays the same deterministic fault families behind the block-at-rest
/// encryption seam. Every write passes through region re-sealing and every
/// recovery read through GCM authentication, proving a crash or torn write
/// under encryption recovers only a complete committed prefix.
fn encrypted_fault_cells() -> io::Result<Vec<MatrixCellReport>> {
    let trace = baseline_phase_trace_with_key(Some(matrix_at_rest_key()))?;
    let partial_group_bytes = phase_batch().remove(0).prepare()?.wal_encoded_bytes() + 7;
    let mut interrupted = Vec::new();
    let mut storage_full = Vec::new();
    let mut short_write = Vec::new();
    let mut lost_sync = Vec::new();
    for (index, operation) in trace.iter().enumerate() {
        let operation_number = index as u64 + 1;
        interrupted.push((
            operation_number,
            FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        ));
        storage_full.push((
            operation_number,
            FaultAction::ErrorBefore(io::ErrorKind::StorageFull),
        ));
        if operation == &Operation::Write {
            short_write.push((operation_number, FaultAction::ShortWrite(7)));
            short_write.push((
                operation_number,
                FaultAction::ShortWrite(partial_group_bytes),
            ));
        }
        if is_sync(operation) {
            lost_sync.push((operation_number, FaultAction::DropSync));
        }
    }
    let key = Some(matrix_at_rest_key());
    let correlated_syncs: Vec<u64> = lost_sync.iter().map(|(number, _)| *number).collect();
    Ok(vec![
        run_fault_family_with_key(
            "vfs-fault.syscall-error.interrupted.at-rest",
            &interrupted,
            key.clone(),
        )?,
        run_fault_family_with_key(
            "vfs-fault.syscall-error.storage-full.at-rest",
            &storage_full,
            key.clone(),
        )?,
        run_fault_family_with_key("vfs-fault.short-write.at-rest", &short_write, key.clone())?,
        run_fault_family_with_key("vfs-fault.lost-sync.at-rest", &lost_sync, key.clone())?,
        run_correlated_lost_sync_family(
            "vfs-fault.lost-sync.correlated-all.at-rest",
            &correlated_syncs,
            key,
        )?,
    ])
}

fn fault_cells_for_trace(trace: &[Operation]) -> io::Result<Vec<MatrixCellReport>> {
    let partial_group_bytes = phase_batch().remove(0).prepare()?.wal_encoded_bytes() + 7;
    if trace.len() > FILESYSTEM_CERTIFICATION_MATRIX_MAX_TRACE_OPERATIONS {
        return Err(invalid_input("certification trace exceeds 4096 operations"));
    }
    let mut interrupted = Vec::new();
    let mut storage_full = Vec::new();
    let mut short_write = Vec::new();
    let mut lost_sync = Vec::new();
    for (index, operation) in trace.iter().enumerate() {
        let operation_number = index as u64 + 1;
        interrupted.push((
            operation_number,
            FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        ));
        storage_full.push((
            operation_number,
            FaultAction::ErrorBefore(io::ErrorKind::StorageFull),
        ));
        if operation == &Operation::Write {
            short_write.push((operation_number, FaultAction::ShortWrite(7)));
            short_write.push((
                operation_number,
                FaultAction::ShortWrite(partial_group_bytes),
            ));
        }
        if is_sync(operation) {
            lost_sync.push((operation_number, FaultAction::DropSync));
        }
    }
    let correlated_syncs: Vec<u64> = lost_sync.iter().map(|(number, _)| *number).collect();
    Ok(vec![
        run_fault_family("vfs-fault.syscall-error.interrupted", &interrupted)?,
        run_fault_family("vfs-fault.syscall-error.storage-full", &storage_full)?,
        run_fault_family("vfs-fault.short-write", &short_write)?,
        run_fault_family("vfs-fault.lost-sync", &lost_sync)?,
        run_correlated_lost_sync_family(
            "vfs-fault.lost-sync.correlated-all",
            &correlated_syncs,
            None,
        )?,
    ])
}

/// Runs every matrix cell. `data_dir` must be an existing empty directory;
/// the `primary.path` cell store is retained beneath it as evidence and this
/// function never deletes or overwrites a prior path.
pub fn certify_filesystem_matrix(data_dir: &Path) -> io::Result<Vec<MatrixCellReport>> {
    if !data_dir.is_absolute() {
        return Err(invalid_input(
            "certification matrix requires an absolute directory",
        ));
    }
    if !empty_directory(data_dir)? {
        return Err(invalid_input(
            "certification matrix requires an explicit empty directory",
        ));
    }
    let mut reports = Vec::new();
    let primary_target = data_dir.join("primary-path");
    fs::create_dir(&primary_target)?;
    reports.push(real_mount_cell("primary.path", primary_target));
    let primary_encrypted_target = data_dir.join("primary-path-at-rest");
    fs::create_dir(&primary_encrypted_target)?;
    reports.push(real_mount_cell(
        "primary.path.at-rest",
        primary_encrypted_target,
    ));
    match env::var_os(CERT_MATRIX_MOUNT_ENV) {
        Some(target) if !target.is_empty() => {
            let target = PathBuf::from(target);
            reports.push(real_mount_cell("mount.opt-in", target.clone()));
            let encrypted_target = target.join("at-rest");
            fs::create_dir(&encrypted_target)?;
            reports.push(real_mount_cell("mount.opt-in.at-rest", encrypted_target));
        }
        _ => {
            reports.push(MatrixCellReport {
                cell: "mount.opt-in".to_owned(),
                outcome: MatrixCellOutcome::Skipped,
                cases: 0,
                failures: 0,
                detail: format!(
                    "set_{CERT_MATRIX_MOUNT_ENV}_to_an_empty_network_or_fuse_directory"
                ),
                micros: 0,
            });
            reports.push(MatrixCellReport {
                cell: "mount.opt-in.at-rest".to_owned(),
                outcome: MatrixCellOutcome::Skipped,
                cases: 0,
                failures: 0,
                detail: format!(
                    "set_{CERT_MATRIX_MOUNT_ENV}_to_an_empty_network_or_fuse_directory"
                ),
                micros: 0,
            });
        }
    }
    reports.extend(deterministic_fault_cells()?);
    reports.extend(encrypted_fault_cells()?);
    Ok(reports)
}

/// Formats the whole matrix, including a bounded summary trailer.
pub fn format_matrix_report(reports: &[MatrixCellReport]) -> String {
    let mut output = String::new();
    let mut passed = 0_u64;
    let mut failed = 0_u64;
    let mut skipped = 0_u64;
    for report in reports {
        output.push_str(&report.format_line());
        output.push('\n');
        match report.outcome {
            MatrixCellOutcome::Pass => passed += 1,
            MatrixCellOutcome::Fail => failed += 1,
            MatrixCellOutcome::Skipped => skipped += 1,
        }
    }
    output.push_str(&format!(
        "format={FILESYSTEM_CERTIFICATION_MATRIX_FORMAT} matrix_result={} cells={} passed={} failed={} skipped={} measurements_are_observations_not_release_certification=true\n",
        if failed > 0 { "fail" } else if skipped > 0 || reports.is_empty() { "partial" } else { "pass" },
        reports.len(),
        passed,
        failed,
        skipped
    ));
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matrix_rejects_lost_acknowledged_prefix_and_empty_fault_evidence() {
        let vfs = prepared_simulated_vfs().unwrap();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(recovered_phase_is_complete(&engine, 0).unwrap());
        assert!(!recovered_phase_is_complete(&engine, 2).unwrap());
        engine.commit_batch(&phase_batch()[..1]).unwrap();
        assert!(recovered_phase_is_complete(&engine, 0).unwrap());
        assert!(!recovered_phase_is_complete(&engine, 2).unwrap());
        engine.commit_batch(&phase_batch()[1..]).unwrap();
        assert!(recovered_phase_is_complete(&engine, 2).unwrap());
        assert_eq!(
            run_fault_family("empty", &[]).unwrap().outcome,
            MatrixCellOutcome::Fail
        );
    }

    #[test]
    fn deterministic_fault_cells_cover_every_observed_fault() {
        let trace = baseline_phase_trace().unwrap();
        let reports = fault_cells_for_trace(&trace).unwrap();
        assert_eq!(reports.len(), 5);
        for report in &reports {
            assert_eq!(
                report.outcome,
                MatrixCellOutcome::Pass,
                "{}: {}",
                report.cell,
                report.detail
            );
            assert!(report.cases > 0, "{}", report.cell);
            assert_eq!(report.failures, 0, "{}", report.cell);
        }
        let writes = trace
            .iter()
            .filter(|operation| operation == &&Operation::Write)
            .count() as u64;
        let syncs = trace.iter().filter(|operation| is_sync(operation)).count() as u64;
        assert_eq!(reports[0].cases, trace.len() as u64);
        assert_eq!(reports[1].cases, trace.len() as u64);
        assert_eq!(reports[2].cases, writes * 2);
        assert_eq!(reports[3].cases, syncs);
        // singles + all pairs + the full correlated set.
        assert_eq!(reports[4].cell, "vfs-fault.lost-sync.correlated-all");
        assert_eq!(reports[4].cases, syncs + syncs * (syncs - 1) / 2 + 1);
    }

    #[test]
    fn encrypted_fault_cells_cover_every_observed_fault_under_encryption() {
        let trace = baseline_phase_trace_with_key(Some(matrix_at_rest_key())).unwrap();
        let reports = encrypted_fault_cells().unwrap();
        assert_eq!(reports.len(), 5);
        for report in &reports {
            assert!(report.cell.ends_with(".at-rest"), "{}", report.cell);
            assert_eq!(
                report.outcome,
                MatrixCellOutcome::Pass,
                "{}: {}",
                report.cell,
                report.detail
            );
            assert!(report.cases > 0, "{}", report.cell);
            assert_eq!(report.failures, 0, "{}", report.cell);
        }
        let writes = trace
            .iter()
            .filter(|operation| operation == &&Operation::Write)
            .count() as u64;
        let syncs = trace.iter().filter(|operation| is_sync(operation)).count() as u64;
        assert_eq!(reports[0].cases, trace.len() as u64);
        assert_eq!(reports[1].cases, trace.len() as u64);
        assert_eq!(reports[2].cases, writes * 2);
        assert_eq!(reports[3].cases, syncs);
        assert_eq!(
            reports[4].cell,
            "vfs-fault.lost-sync.correlated-all.at-rest"
        );
        assert_eq!(reports[4].cases, syncs + syncs * (syncs - 1) / 2 + 1);
    }

    #[test]
    fn encrypted_reopen_with_wrong_key_fails_closed_after_crash() {
        let vfs = prepared_simulated_vfs().unwrap();
        {
            let mut engine =
                initialize_fault_fixture_with_key(&vfs, Some(matrix_at_rest_key())).unwrap();
            engine.commit_batch(&phase_batch()).unwrap();
            engine.checkpoint().unwrap();
        }
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        // The correct key recovers the complete prefix after the crash.
        let engine = Engine::open_with_vfs_and_key(
            Path::new("/data"),
            vfs.clone(),
            Some(matrix_at_rest_key()),
        )
        .unwrap();
        assert!(recovered_phase_is_complete(&engine, 2).unwrap());
        drop(engine);
        // A wrong key fails closed on the witness before touching payload.
        let wrong = Engine::open_with_vfs_and_key(
            Path::new("/data"),
            vfs.clone(),
            Some(AtRestKey::from_key([0x55; 32])),
        );
        match wrong {
            Err(error) => assert_eq!(error.kind(), io::ErrorKind::PermissionDenied),
            Ok(_) => panic!("wrong at-rest key must fail closed"),
        }
        // No key on an encrypted authority also fails closed: the simulator
        // replays ciphertext and the integrity envelope rejects it.
        assert!(Engine::open_with_vfs(Path::new("/data"), vfs).is_err());
    }

    #[test]
    fn fault_fixture_trace_is_stable_across_random_segment_identities() {
        let trace = baseline_phase_trace().unwrap();
        for _ in 0..64 {
            assert_eq!(baseline_phase_trace().unwrap(), trace);
        }
        assert!(!fault_case_recovers_complete_prefix(Some(FaultRule {
            operation_number: trace.len() as u64 + 1,
            action: FaultAction::DropSync,
        }))
        .unwrap());
    }

    #[test]
    fn correlated_lost_sync_recovers_complete_prefix_or_fails_closed() {
        let trace = baseline_phase_trace().unwrap();
        let syncs: Vec<u64> = trace
            .iter()
            .enumerate()
            .filter(|(_, operation)| is_sync(operation))
            .map(|(index, _)| index as u64 + 1)
            .collect();
        assert!(syncs.len() >= 4, "trace: {trace:?}");
        let mut recovered = 0_usize;
        let mut failed_closed = 0_usize;
        for first in 0..syncs.len() {
            for second in first + 1..syncs.len() {
                match correlated_lost_sync_case_outcome(&[syncs[first], syncs[second]], None)
                    .unwrap()
                {
                    CorrelatedLossOutcome::RecoveredCompletePrefix => recovered += 1,
                    CorrelatedLossOutcome::FailedClosed => failed_closed += 1,
                    CorrelatedLossOutcome::Torn => {
                        panic!(
                            "pair ({}, {}) recovered a torn state",
                            syncs[first], syncs[second]
                        )
                    }
                }
            }
        }
        assert!(recovered > 0, "some pair must recover a complete prefix");
        // The full correlated set drops every barrier at once: the store must
        // either recover the empty pre-commit prefix or fail closed, never a
        // torn group.
        let full = correlated_lost_sync_case_outcome(&syncs, None).unwrap();
        assert_ne!(full, CorrelatedLossOutcome::Torn);
        let _ = failed_closed;
    }

    #[test]
    fn matrix_report_lines_are_machine_readable() {
        let report = MatrixCellReport {
            cell: "mount.opt-in".to_owned(),
            outcome: MatrixCellOutcome::Skipped,
            cases: 0,
            failures: 0,
            detail: "set_env".to_owned(),
            micros: 0,
        };
        let line = report.format_line();
        assert!(line.contains("format=tofu-db.filesystem-certification-matrix.v1"));
        assert!(line.contains("cell=mount.opt-in"));
        assert!(line.contains("result=skipped"));
        assert!(line.contains("measurements_are_observations_not_release_certification=true"));
        assert!(format_matrix_report(&[report]).contains("matrix_result=partial"));
        assert!(format_matrix_report(&[]).contains("matrix_result=partial"));
    }
}
