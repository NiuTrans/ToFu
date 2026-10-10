//! Bounded launch resource-gate measurement for an explicit operator path.
//!
//! `certify-resource-gate` runs the smallest fixed workload that turns the
//! existing wall-clock observations into a performance/resource gate: a
//! bounded sequential commit burst (inline plus 1 MiB block-carried commits
//! with one midpoint checkpoint), one explicit commit+checkpoint stage, and a
//! second reopen for checkpoint recovery latency, followed by the retained
//! authority-directory footprint. Each workload stage is measured inside a
//! spawned child whose peak RSS and wall-clock are reaped through `wait4`,
//! so observations are per-stage rather than process-lifetime, and the
//! commit child exits without running Rust destructors so recovery under
//! this workload is also proven.
//!
//! It emits one machine-readable `tofu-db.resource-gate.v1` line: the
//! launch-probe snapshot and derived budget, per-stage wall-clock,
//! `max_rss_kib`, and stage-timer attribution, plus the reference
//! personal-computer budgets (8 GiB RAM / 500 GB disk) and explicit
//! `*_exceeded` booleans. All reference budgets, including the latency
//! bounds, are observational numbers for one operator-recorded volume
//! class: this command never fails itself on a measurement; it exits
//! nonzero only when the workload misbehaves. The launch-gate decision is
//! made by the runner that pins the exact build and volume and compares
//! every `*_exceeded` flag.

use std::env;
use std::fs;
use std::io::{self, Read};
#[cfg(target_os = "linux")]
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};
use std::time::{Duration, Instant};

use crate::engine::Engine;
use crate::generated_tofudb_ir::{
    RESOURCE_GATE_BLOCK_BYTES, RESOURCE_GATE_COMMIT_BLOCK_OPS, RESOURCE_GATE_COMMIT_INLINE_OPS,
    RESOURCE_GATE_FORMAT, RESOURCE_GATE_MAX_INLINE_BYTES,
    RESOURCE_GATE_REFERENCE_COMMIT_MEAN_MICROS, RESOURCE_GATE_REFERENCE_PEAK_RSS_BYTES,
    RESOURCE_GATE_REFERENCE_RETAINED_BYTES, RESOURCE_GATE_REFERENCE_TOTAL_MICROS,
    RESOURCE_GATE_REFERENCE_VOLUME_BYTES,
};
use crate::resource_probe::{probe_launch_resources, DaemonResourceBudget};

/// Hidden commit child stage exit code: like the filesystem certification
/// write child it intentionally skips Rust destructors after succeeding.
pub const RESOURCE_GATE_COMMIT_EXIT_WITHOUT_DESTRUCTORS: i32 = 87;

const KIB: u64 = 1024;
const INLINE_PREFIX: &[u8] = b"tofu-db.resource-gate.inline.";
const BLOCK_FILL_A: u64 = 131;
const BLOCK_FILL_B: u64 = 251;
const COMMIT_ATTRIBUTION_WITNESS: &str = ".gate-commit-attribution";
const CHECKPOINT_ATTRIBUTION_WITNESS: &str = ".gate-checkpoint-attribution";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct StageMeasurement {
    wall_micros: u64,
    peak_rss_kib: u64,
}

/// Engine commit-path attribution sampled from `fsync_stats` stage timers.
/// The commit stage reports encode, rotation/checkpoint check, WAL append,
/// and commit tail; the checkpoint stage reports deferred-block drain,
/// history-segment packing, WAL generation swap, and CONTROL publication.
/// Stages nest, so they must not be summed as disjoint foreground latency.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct StageAttribution {
    pub first_nanos: u64,
    pub second_nanos: u64,
    pub third_nanos: u64,
    pub fourth_nanos: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ResourceGateReport {
    pub commit_wall_micros: u64,
    pub commit_mean_micros: u64,
    pub commit_peak_rss_kib: u64,
    pub checkpoint_wall_micros: u64,
    pub checkpoint_peak_rss_kib: u64,
    pub reopen_wall_micros: u64,
    pub reopen_peak_rss_kib: u64,
    pub total_wall_micros: u64,
    pub retained_file_count: u64,
    pub retained_file_bytes: u64,
    pub commit_attribution: StageAttribution,
    pub checkpoint_attribution: StageAttribution,
}

const COMMIT_ATTRIBUTION_STAGES: [usize; 4] = [
    crate::fsync_stats::STAGE_ENCODE,
    crate::fsync_stats::STAGE_CHECKPOINT_IF,
    crate::fsync_stats::STAGE_WAL,
    crate::fsync_stats::STAGE_COMMIT_TAIL,
];
const CHECKPOINT_ATTRIBUTION_STAGES: [usize; 4] = [
    crate::fsync_stats::STAGE_CK_DRAIN,
    crate::fsync_stats::STAGE_CK_SEGMENTS,
    crate::fsync_stats::STAGE_CK_WAL,
    crate::fsync_stats::STAGE_CK_CONTROL,
];

fn elapsed_micros(duration: Duration) -> u64 {
    u64::try_from(duration.as_micros()).unwrap_or(u64::MAX)
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn invalid_input(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ChildOutcome {
    status: ExitStatus,
    peak_rss_kib: u64,
}

/// Waits for one spawned child through `wait4`, collecting its exit status
/// and Linux high-water RSS in one reaping call. The gate refuses to run on
/// other platforms rather than emit a resident-memory metric of zero.
#[cfg(target_os = "linux")]
fn wait4_child(child: std::process::Child) -> io::Result<ChildOutcome> {
    let pid = match i32::try_from(child.id()) {
        Ok(pid) => pid as libc::pid_t,
        Err(_) => return Err(io::Error::other("resource gate child id overflows pid_t")),
    };
    let mut status = 0_i32;
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    // SAFETY: `status` and `usage` are valid out-buffers; no other waiter
    // reaps this child because the `Child` value is owned here and never
    // waited on through the standard library.
    let result = unsafe { libc::wait4(pid, &mut status, 0, usage.as_mut_ptr()) };
    if result < 0 {
        return Err(io::Error::last_os_error());
    }
    // `wait4` consumed the child; prevent `Child::drop` from re-waiting.
    std::mem::forget(child);
    let usage = unsafe { usage.assume_init() };
    let peak_rss_kib = u64::try_from(usage.ru_maxrss)
        .map_err(|_| invalid_data("resource gate child maxrss is negative"))?;
    Ok(ChildOutcome {
        status: ExitStatus::from_raw(status),
        peak_rss_kib,
    })
}

/// Linux measures each stage in a spawned child reaped through `wait4`.
#[cfg(target_os = "linux")]
fn spawn_measured_stage(
    stage: &str,
    data_dir: &Path,
    expected_code: i32,
) -> io::Result<StageMeasurement> {
    let started = Instant::now();
    let child = Command::new(env::current_exe()?)
        .arg(stage)
        .arg("--data-dir")
        .arg(data_dir)
        .spawn()?;
    let outcome = wait4_child(child)?;
    let wall_micros = elapsed_micros(started.elapsed());
    if outcome.status.code() != Some(expected_code) {
        return Err(io::Error::other(format!(
            "resource gate {stage} process returned {}",
            outcome.status
        )));
    }
    Ok(StageMeasurement {
        wall_micros,
        peak_rss_kib: outcome.peak_rss_kib,
    })
}

/// Non-Linux platforms have no `wait4` peak-RSS accounting; the gate refuses
/// to run rather than emit a resident-memory metric of zero.
#[cfg(not(target_os = "linux"))]
fn spawn_measured_stage(
    stage: &str,
    _data_dir: &Path,
    _expected_code: i32,
) -> io::Result<StageMeasurement> {
    let _ = stage;
    Err(io::Error::other(
        "resource gate measurement requires Linux wait4 peak-RSS accounting",
    ))
}

fn gate_inline_payload(sequence: u64) -> Vec<u8> {
    let mut payload = Vec::with_capacity(RESOURCE_GATE_MAX_INLINE_BYTES);
    payload.extend_from_slice(INLINE_PREFIX);
    payload.extend_from_slice(sequence.to_string().as_bytes());
    payload
}

fn gate_block_payload() -> Vec<u8> {
    (0..RESOURCE_GATE_BLOCK_BYTES)
        .map(|index| ((index as u64 * BLOCK_FILL_A + index as u64 / BLOCK_FILL_B) & 0xff) as u8)
        .collect()
}

fn total_commits() -> u64 {
    (RESOURCE_GATE_COMMIT_INLINE_OPS + RESOURCE_GATE_COMMIT_BLOCK_OPS) as u64
}

fn require_authority_shape(engine: &Engine, expected_durable: u64, stage: &str) -> io::Result<()> {
    if engine.state().durable_sequence != expected_durable {
        return Err(invalid_data(format!(
            "resource gate {stage} durable sequence mismatch"
        )));
    }
    if engine.transaction_snapshot()?.len() as u64 != expected_durable {
        return Err(invalid_data(format!(
            "resource gate {stage} transaction projection mismatch"
        )));
    }
    Ok(())
}

fn attribution_snapshot(stages: &[usize; 4]) -> StageAttribution {
    StageAttribution {
        first_nanos: crate::fsync_stats::stage_nanos(stages[0]),
        second_nanos: crate::fsync_stats::stage_nanos(stages[1]),
        third_nanos: crate::fsync_stats::stage_nanos(stages[2]),
        fourth_nanos: crate::fsync_stats::stage_nanos(stages[3]),
    }
}

fn write_attribution(data_dir: &Path, name: &str, stages: &[usize; 4]) -> io::Result<()> {
    let attribution = attribution_snapshot(stages);
    let encoded = format!(
        "{}:{}:{}:{}",
        attribution.first_nanos,
        attribution.second_nanos,
        attribution.third_nanos,
        attribution.fourth_nanos
    );
    fs::write(data_dir.join(name), encoded)
}

fn read_attribution(data_dir: &Path, name: &str) -> io::Result<StageAttribution> {
    let path = data_dir.join(name);
    let mut encoded = String::new();
    fs::File::open(&path)?.read_to_string(&mut encoded)?;
    fs::remove_file(&path)?;
    let fields = encoded
        .split(':')
        .map(|field| field.parse::<u64>())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| invalid_data("resource gate stage attribution is malformed"))?;
    if fields.len() != 4 {
        return Err(invalid_data(
            "resource gate stage attribution field count differs",
        ));
    }
    Ok(StageAttribution {
        first_nanos: fields[0],
        second_nanos: fields[1],
        third_nanos: fields[2],
        fourth_nanos: fields[3],
    })
}

/// Bounded footprint walk; the gate directory is created by this command,
/// so it can never contain more entries than the fixed workload wrote.
fn retained_store_footprint(data_dir: &Path) -> io::Result<(u64, u64)> {
    let mut pending = vec![PathBuf::from(data_dir)];
    let mut file_count = 0_u64;
    let mut file_bytes = 0_u64;
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let metadata = fs::symlink_metadata(entry.path())?;
            if metadata.is_dir() {
                pending.push(entry.path());
            } else if metadata.is_file() {
                file_count += 1;
                file_bytes += metadata.len();
            }
        }
    }
    Ok((file_count, file_bytes))
}

/// Hidden child stage: bounded sequential inline plus block-carried commits
/// with one forced checkpoint at the midpoint. Exits without running
/// destructors so the later stages prove recovery from this workload.
pub fn commit_stage(data_dir: &Path) -> io::Result<()> {
    let mut engine = Engine::initialize(data_dir)?;
    let block = gate_block_payload();
    for index in 0..RESOURCE_GATE_COMMIT_INLINE_OPS {
        let payload = gate_inline_payload(index as u64);
        if payload.len() > RESOURCE_GATE_MAX_INLINE_BYTES {
            return Err(invalid_data(
                "resource gate inline payload exceeds its bound",
            ));
        }
        engine.commit_transaction(&payload, &[])?;
        if index + 1 == RESOURCE_GATE_COMMIT_INLINE_OPS / 2 {
            engine.checkpoint()?;
        }
    }
    for _ in 0..RESOURCE_GATE_COMMIT_BLOCK_OPS {
        engine.commit_transaction(&[], &[block.as_slice()])?;
    }
    require_authority_shape(&engine, total_commits(), "commit")?;
    write_attribution(
        data_dir,
        COMMIT_ATTRIBUTION_WITNESS,
        &COMMIT_ATTRIBUTION_STAGES,
    )?;
    std::process::exit(RESOURCE_GATE_COMMIT_EXIT_WITHOUT_DESTRUCTORS);
}

/// Hidden child stage: one more commit crossing the rotation window, then
/// the checkpoint, measured separately from ordinary commit latency.
pub fn checkpoint_stage(data_dir: &Path) -> io::Result<()> {
    let mut engine = Engine::open(data_dir)?;
    engine.commit_transaction(&gate_inline_payload(u64::MAX), &[])?;
    engine.checkpoint()?;
    require_authority_shape(&engine, total_commits() + 1, "checkpoint")?;
    write_attribution(
        data_dir,
        CHECKPOINT_ATTRIBUTION_WITNESS,
        &CHECKPOINT_ATTRIBUTION_STAGES,
    )
}

/// Hidden child stage: pure reopen over the checkpointed authority.
pub fn reopen_stage(data_dir: &Path) -> io::Result<()> {
    let engine = Engine::open(data_dir)?;
    require_authority_shape(&engine, total_commits() + 1, "reopen")
}

/// Runs the fixed gate workload. `data_dir` must be an existing empty
/// directory; the measured store is retained as auditable evidence and this
/// function never deletes or overwrites a prior path.
pub fn certify_resource_gate(data_dir: &Path) -> io::Result<ResourceGateReport> {
    if !data_dir.is_absolute() {
        return Err(invalid_input(
            "resource gate measurement requires an absolute directory",
        ));
    }
    if fs::read_dir(data_dir)?.next().is_some() {
        return Err(invalid_input(
            "resource gate measurement requires an explicit empty directory",
        ));
    }
    let total_started = Instant::now();
    let commit = spawn_measured_stage(
        "__gate-commit",
        data_dir,
        RESOURCE_GATE_COMMIT_EXIT_WITHOUT_DESTRUCTORS,
    )?;
    let checkpoint = spawn_measured_stage("__gate-checkpoint", data_dir, 0)?;
    let reopen = spawn_measured_stage("__gate-reopen", data_dir, 0)?;
    let commit_attribution = read_attribution(data_dir, COMMIT_ATTRIBUTION_WITNESS)?;
    let checkpoint_attribution = read_attribution(data_dir, CHECKPOINT_ATTRIBUTION_WITNESS)?;
    let (retained_file_count, retained_file_bytes) = retained_store_footprint(data_dir)?;
    let total_wall_micros = elapsed_micros(total_started.elapsed());
    Ok(ResourceGateReport {
        commit_wall_micros: commit.wall_micros,
        commit_mean_micros: commit.wall_micros / total_commits(),
        commit_peak_rss_kib: commit.peak_rss_kib,
        checkpoint_wall_micros: checkpoint.wall_micros,
        checkpoint_peak_rss_kib: checkpoint.peak_rss_kib,
        reopen_wall_micros: reopen.wall_micros,
        reopen_peak_rss_kib: reopen.peak_rss_kib,
        total_wall_micros,
        retained_file_count,
        retained_file_bytes,
        commit_attribution,
        checkpoint_attribution,
    })
}

/// Formats the one machine-readable gate line: launch-probe snapshot and
/// derived budget, per-stage measurements and attribution, the reference
/// personal-computer budgets, and every comparison the launch gate runner
/// consumes. Observations are reference-budget comparisons, never release
/// certification by themselves.
pub fn format_gate_report(report: &ResourceGateReport, data_dir: &Path) -> String {
    let snapshot = probe_launch_resources(data_dir);
    let budget = DaemonResourceBudget::from_snapshot(snapshot);
    let peak_rss_bytes = report
        .commit_peak_rss_kib
        .max(report.checkpoint_peak_rss_kib)
        .max(report.reopen_peak_rss_kib)
        .saturating_mul(KIB);
    let commit_mean_exceeded =
        report.commit_mean_micros > RESOURCE_GATE_REFERENCE_COMMIT_MEAN_MICROS;
    let checkpoint_exceeded = report.checkpoint_wall_micros > RESOURCE_GATE_REFERENCE_TOTAL_MICROS;
    let reopen_exceeded = report.reopen_wall_micros > RESOURCE_GATE_REFERENCE_TOTAL_MICROS;
    let total_exceeded = report.total_wall_micros > RESOURCE_GATE_REFERENCE_TOTAL_MICROS;
    let rss_exceeded = peak_rss_bytes > RESOURCE_GATE_REFERENCE_PEAK_RSS_BYTES;
    let retained_exceeded = report.retained_file_bytes > RESOURCE_GATE_REFERENCE_RETAINED_BYTES;
    let any_exceeded = commit_mean_exceeded
        || checkpoint_exceeded
        || reopen_exceeded
        || total_exceeded
        || rss_exceeded
        || retained_exceeded;
    format!(
        "format={RESOURCE_GATE_FORMAT} inline_commits={} block_commits={} block_bytes={} commit_wall_micros={} commit_mean_micros={} commit_peak_rss_kib={} commit_encode_nanos={} commit_checkpoint_if_nanos={} commit_wal_nanos={} commit_commit_tail_nanos={} checkpoint_wall_micros={} checkpoint_peak_rss_kib={} checkpoint_ck_drain_nanos={} checkpoint_ck_segments_nanos={} checkpoint_ck_wal_nanos={} checkpoint_ck_control_nanos={} reopen_wall_micros={} reopen_peak_rss_kib={} total_wall_micros={} retained_file_count={} retained_file_bytes={} probe_logical_cpus={} probe_memory_capacity_bytes={} probe_memory_headroom_bytes={} probe_volume_free_bytes={} budget_maximum_connections={} budget_maximum_frame_bytes={} budget_lean_fallback={} reference_volume_bytes={} reference_peak_rss_bytes={} reference_retained_bytes={} reference_commit_mean_micros={} reference_total_micros={} commit_mean_exceeded={} checkpoint_exceeded={} reopen_exceeded={} total_exceeded={} rss_exceeded={} retained_exceeded={} gate_observations_exceed_reference_budgets={} measurements_are_reference_budget_comparisons_not_release_certification=true\n",
        RESOURCE_GATE_COMMIT_INLINE_OPS,
        RESOURCE_GATE_COMMIT_BLOCK_OPS,
        RESOURCE_GATE_BLOCK_BYTES,
        report.commit_wall_micros,
        report.commit_mean_micros,
        report.commit_peak_rss_kib,
        report.commit_attribution.first_nanos,
        report.commit_attribution.second_nanos,
        report.commit_attribution.third_nanos,
        report.commit_attribution.fourth_nanos,
        report.checkpoint_wall_micros,
        report.checkpoint_peak_rss_kib,
        report.checkpoint_attribution.first_nanos,
        report.checkpoint_attribution.second_nanos,
        report.checkpoint_attribution.third_nanos,
        report.checkpoint_attribution.fourth_nanos,
        report.reopen_wall_micros,
        report.reopen_peak_rss_kib,
        report.total_wall_micros,
        report.retained_file_count,
        report.retained_file_bytes,
        snapshot
            .logical_cpus
            .map_or_else(|| "unknown".to_owned(), |value| value.to_string()),
        snapshot
            .memory_capacity_bytes
            .map_or_else(|| "unknown".to_owned(), |value| value.to_string()),
        snapshot
            .memory_headroom_bytes
            .map_or_else(|| "unknown".to_owned(), |value| value.to_string()),
        snapshot
            .volume_free_bytes
            .map_or_else(|| "unknown".to_owned(), |value| value.to_string()),
        budget.maximum_connections,
        budget.maximum_frame_bytes,
        budget.used_lean_fallback,
        RESOURCE_GATE_REFERENCE_VOLUME_BYTES,
        RESOURCE_GATE_REFERENCE_PEAK_RSS_BYTES,
        RESOURCE_GATE_REFERENCE_RETAINED_BYTES,
        RESOURCE_GATE_REFERENCE_COMMIT_MEAN_MICROS,
        RESOURCE_GATE_REFERENCE_TOTAL_MICROS,
        commit_mean_exceeded,
        checkpoint_exceeded,
        reopen_exceeded,
        total_exceeded,
        rss_exceeded,
        retained_exceeded,
        any_exceeded,
    )
}
