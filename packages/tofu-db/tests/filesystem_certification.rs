//! Cross-process certification of the real filesystem-backed operator command.

mod common;

use std::process::Command;
use std::sync::{Arc, Barrier};
use std::thread;
use std::{collections::HashMap, str::FromStr};

use tofu_db::engine::{BatchTransaction, Engine};
use tofu_db::sequencer::{CommitSequencer, SequencerConfig};

#[test]
fn explicit_empty_path_survives_destructor_free_and_cross_process_reopens() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-filesystem")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("format=tofu-db.filesystem-certification.v1"));
    assert!(stdout.contains("destructor_free_reopen=true"));
    assert!(stdout.contains("cross_process_reopen=true"));
    assert!(stdout.contains("payload_segment_publication=true"));
    assert!(stdout.contains("payload_segment_random_read=true"));
    assert!(stdout.contains("payload_loose_reclaim=true"));
    assert!(stdout.contains("payload_segment_count=1"));
    assert!(stdout.contains("payload_block_bytes=2097153"));
    assert!(stdout.contains("write_process_micros="));
    assert!(stdout.contains("first_reopen_micros="));
    assert!(stdout.contains("post_reopen_commit_micros="));
    assert!(stdout.contains("second_reopen_process_micros="));
    assert!(stdout.contains("total_micros="));
    assert!(stdout.contains("retained_file_count="));
    assert!(stdout.contains("retained_file_bytes="));
    assert!(stdout.contains("measurements_are_observations_not_release_certification=true"));
    let fields = stdout
        .split_whitespace()
        .filter_map(|field| field.split_once('='))
        .collect::<HashMap<_, _>>();
    let metric = |name: &str| u64::from_str(fields[name]).unwrap();
    assert!(metric("write_process_micros") > 0);
    assert!(metric("first_reopen_micros") > 0);
    assert!(metric("post_reopen_commit_micros") > 0);
    assert!(metric("second_reopen_process_micros") > 0);
    assert!(
        metric("total_micros")
            >= metric("write_process_micros")
                + metric("first_reopen_micros")
                + metric("post_reopen_commit_micros")
                + metric("second_reopen_process_micros")
    );
    assert!(metric("retained_file_count") >= 5);
    assert!(metric("retained_file_bytes") > metric("payload_block_bytes"));

    let engine = Engine::open(directory.path()).unwrap();
    assert_eq!(engine.state().durable_sequence, 3);
    assert_eq!(engine.state().checkpoint_sequence, 2);
    assert_eq!(engine.transaction_snapshot().unwrap().len(), 3);
    assert_eq!(engine.payload_segment_count(), 1);
}

const AT_REST_SECRET: &str = "tofu-db-certification-at-rest-test-key-v1";

#[test]
fn encrypted_certification_passes_and_wrong_key_fails_closed() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-filesystem")
        .arg("--data-dir")
        .arg(directory.path())
        .env("TOFU_DB_AT_REST_KEY", AT_REST_SECRET)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("format=tofu-db.filesystem-certification.v1"));
    assert!(stdout.contains("at_rest=true"));
    assert!(stdout.contains("destructor_free_reopen=true"));
    assert!(stdout.contains("cross_process_reopen=true"));
    assert!(stdout.contains("payload_block_bytes=2097153"));

    // The retained store is encrypted at rest: the exact payload bytes must
    // not appear anywhere beneath the data directory.
    let mut scanned = 0_u64;
    let mut pending = vec![directory.path().to_path_buf()];
    while let Some(directory_path) = pending.pop() {
        for entry in std::fs::read_dir(directory_path).unwrap() {
            let entry = entry.unwrap();
            let metadata = entry.metadata().unwrap();
            if metadata.is_dir() {
                pending.push(entry.path());
            } else if metadata.is_file() {
                scanned += 1;
                assert!(scanned <= 4096);
                let bytes = std::fs::read(entry.path()).unwrap();
                let payload = b"tofu-db.certification.first";
                if entry.file_name() != "encryption.keyid" {
                    assert!(
                        !bytes.windows(payload.len()).any(|window| window == payload),
                        "plaintext inline payload leaked into {:?}",
                        entry.path()
                    );
                }
            }
        }
    }

    // Wrong key fails closed at authority open on the key-identity witness.
    let output = Command::new(common::native_binary())
        .arg("inspect")
        .arg("--data-dir")
        .arg(directory.path())
        .env(
            "TOFU_DB_AT_REST_KEY",
            "a-different-at-rest-key-that-is-wrong-v1",
        )
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("key identity"));

    // No key on an encrypted authority also fails closed.
    let output = Command::new(common::native_binary())
        .arg("inspect")
        .arg("--data-dir")
        .arg(directory.path())
        .env_remove("TOFU_DB_AT_REST_KEY")
        .output()
        .unwrap();
    assert!(!output.status.success());

    // The correct key reopens cleanly with the exact certified payload.
    let output = Command::new(common::native_binary())
        .arg("inspect")
        .arg("--data-dir")
        .arg(directory.path())
        .env("TOFU_DB_AT_REST_KEY", AT_REST_SECRET)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("durable_sequence=3"));
}

#[test]
fn certification_refuses_to_reuse_a_nonempty_path() {
    let directory = tempfile::tempdir().unwrap();
    std::fs::write(directory.path().join("user-data"), b"preserve me").unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-filesystem")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert_eq!(
        std::fs::read(directory.path().join("user-data")).unwrap(),
        b"preserve me"
    );
}

#[test]
fn real_vfs_sequencer_drains_concurrent_commits_before_releasing_lease() {
    let directory = tempfile::tempdir().unwrap();
    let sequencer = Arc::new(
        CommitSequencer::initialize(directory.path(), SequencerConfig::default()).unwrap(),
    );
    let barrier = Arc::new(Barrier::new(17));
    let mut threads = Vec::new();
    for index in 0..16 {
        let sequencer = Arc::clone(&sequencer);
        let barrier = Arc::clone(&barrier);
        threads.push(thread::spawn(move || {
            barrier.wait();
            sequencer
                .submit(BatchTransaction {
                    inline_payload: format!("native-{index:02}").into_bytes(),
                    block_payloads: Vec::new(),
                })
                .unwrap()
                .sequence
        }));
    }
    barrier.wait();
    let mut sequences = threads
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect::<Vec<_>>();
    sequences.sort_unstable();
    assert_eq!(sequences, (1..=16).collect::<Vec<_>>());
    let metrics = sequencer.metrics().unwrap();
    assert_eq!(metrics.committed_transactions, 16);
    assert!(metrics.durability_groups <= 16);
    drop(sequencer);

    let engine = Engine::open(directory.path()).unwrap();
    assert_eq!(engine.state().durable_sequence, 16);
    assert_eq!(engine.transaction_snapshot().unwrap().len(), 16);
}

fn matrix_fields(stdout: &str) -> Vec<HashMap<String, String>> {
    stdout
        .lines()
        .map(|line| {
            line.split_whitespace()
                .filter_map(|field| field.split_once('='))
                .map(|(key, value)| (key.to_owned(), value.to_owned()))
                .collect::<HashMap<_, _>>()
        })
        .collect()
}

#[test]
fn resource_gate_measures_bounded_workload_and_retains_evidence() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-resource-gate")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("format=tofu-db.resource-gate.v1"));
    assert!(stdout
        .contains("measurements_are_reference_budget_comparisons_not_release_certification=true"));
    let fields = stdout
        .split_whitespace()
        .filter_map(|field| field.split_once('='))
        .collect::<HashMap<_, _>>();
    let metric = |name: &str| u64::from_str(fields[name]).unwrap();
    assert_eq!(metric("inline_commits"), 512);
    assert_eq!(metric("block_commits"), 24);
    assert!(metric("commit_wall_micros") > 0);
    assert!(metric("commit_mean_micros") > 0);
    assert!(metric("checkpoint_wall_micros") > 0);
    assert!(metric("reopen_wall_micros") > 0);
    assert!(
        metric("total_wall_micros")
            >= metric("commit_wall_micros")
                + metric("checkpoint_wall_micros")
                + metric("reopen_wall_micros")
    );
    // wait4 must report a real high-water resident size for every stage.
    for stage in ["commit", "checkpoint", "reopen"] {
        let rss = metric(&format!("{stage}_peak_rss_kib"));
        assert!(rss > 1_024, "{stage} peak rss {rss} KiB is implausible");
    }
    // The engine commit-path stage timers must attribute nonzero work.
    assert!(metric("commit_encode_nanos") > 0);
    assert!(metric("commit_wal_nanos") > 0);
    assert!(metric("commit_checkpoint_if_nanos") > 0);
    assert!(metric("checkpoint_ck_segments_nanos") > 0);
    assert!(metric("checkpoint_ck_control_nanos") > 0);
    // 24 x 1 MiB immutable blocks survive checkpoint publication.
    assert!(metric("retained_file_bytes") > 24 * 1024 * 1024);
    // Reference budgets and the launch-probe snapshot are always emitted.
    assert_eq!(metric("reference_volume_bytes"), 500_000_000_000);
    assert_eq!(metric("reference_peak_rss_bytes"), 512 * 1024 * 1024);
    assert!(stdout.contains("budget_maximum_connections="));
    assert!(stdout.contains("gate_observations_exceed_reference_budgets="));
    // The measured store is retained as auditable evidence.
    let engine = Engine::open(directory.path()).unwrap();
    assert_eq!(engine.state().durable_sequence, 537);
    assert_eq!(engine.transaction_snapshot().unwrap().len(), 537);
}

#[test]
fn resource_gate_refuses_to_reuse_a_nonempty_path() {
    let directory = tempfile::tempdir().unwrap();
    std::fs::write(directory.path().join("user-data"), b"preserve me").unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-resource-gate")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert_eq!(
        std::fs::read(directory.path().join("user-data")).unwrap(),
        b"preserve me"
    );
}

#[test]
fn certification_matrix_runs_path_and_deterministic_fault_cells() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-filesystem-matrix")
        .arg("--data-dir")
        .arg(directory.path())
        .env_remove("TOFU_DB_CERT_MATRIX_MOUNT_DIR")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    let rows = matrix_fields(&stdout);
    let cell = |name: &str| {
        rows.iter()
            .find(|row| row.get("cell").map(String::as_str) == Some(name))
            .unwrap_or_else(|| panic!("missing cell {name}: {stdout}"))
    };
    for row in &rows {
        assert_eq!(
            row.get("format").map(String::as_str),
            Some("tofu-db.filesystem-certification-matrix.v1")
        );
        assert_eq!(
            row.get("measurements_are_observations_not_release_certification")
                .map(String::as_str),
            Some("true")
        );
    }
    assert_eq!(cell("primary.path")["result"], "pass");
    assert!(cell("primary.path")["detail"].contains("at_rest:false"));
    assert_eq!(cell("primary.path.at-rest")["result"], "pass");
    assert!(cell("primary.path.at-rest")["detail"].contains("at_rest:true"));
    assert_eq!(cell("mount.opt-in")["result"], "skipped");
    assert_eq!(cell("mount.opt-in.at-rest")["result"], "skipped");
    for name in [
        "vfs-fault.syscall-error.interrupted",
        "vfs-fault.syscall-error.storage-full",
        "vfs-fault.short-write",
        "vfs-fault.lost-sync",
        "vfs-fault.lost-sync.correlated-all",
        "vfs-fault.syscall-error.interrupted.at-rest",
        "vfs-fault.syscall-error.storage-full.at-rest",
        "vfs-fault.short-write.at-rest",
        "vfs-fault.lost-sync.at-rest",
        "vfs-fault.lost-sync.correlated-all.at-rest",
    ] {
        let row = cell(name);
        assert_eq!(row["result"], "pass", "{name}: {stdout}");
        assert!(row["cases"].parse::<u64>().unwrap() > 0, "{name}");
        assert_eq!(row["failures"], "0", "{name}");
    }
    let summary = rows.last().unwrap();
    assert_eq!(summary["matrix_result"], "partial");
    assert_eq!(summary["cells"], "14");
    assert_eq!(summary["passed"], "12");
    assert_eq!(summary["failed"], "0");
    assert_eq!(summary["skipped"], "2");

    // The primary path cell retains its store as auditable evidence.
    let evidence = directory.path().join("primary-path");
    assert!(std::fs::read_dir(&evidence).unwrap().next().is_some());
    let engine = Engine::open(&evidence).unwrap();
    assert_eq!(engine.state().durable_sequence, 3);
}

#[test]
fn certification_matrix_mount_cell_is_explicit_opt_in() {
    let directory = tempfile::tempdir().unwrap();
    let mount = tempfile::tempdir().unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-filesystem-matrix")
        .arg("--data-dir")
        .arg(directory.path())
        .env("TOFU_DB_CERT_MATRIX_MOUNT_DIR", mount.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    let rows = matrix_fields(&stdout);
    let cell = |name: &str| {
        rows.iter()
            .find(|row| row.get("cell").map(String::as_str) == Some(name))
            .unwrap_or_else(|| panic!("missing cell {name}"))
    };
    let mount_row = cell("mount.opt-in");
    assert_eq!(mount_row["result"], "pass");
    assert!(mount_row["detail"].contains("retained_file_count"));
    let mount_at_rest_row = cell("mount.opt-in.at-rest");
    assert_eq!(mount_at_rest_row["result"], "pass");
    assert!(mount_at_rest_row["detail"].contains("at_rest:true"));
    let summary = rows.last().unwrap();
    assert_eq!(summary["matrix_result"], "pass");
    assert_eq!(summary["passed"], "14");
    assert_eq!(summary["skipped"], "0");
}

#[test]
fn certification_matrix_refuses_a_nonempty_path() {
    let directory = tempfile::tempdir().unwrap();
    std::fs::write(directory.path().join("user-data"), b"preserve me").unwrap();
    let output = Command::new(common::native_binary())
        .arg("certify-filesystem-matrix")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert_eq!(
        std::fs::read(directory.path().join("user-data")).unwrap(),
        b"preserve me"
    );
}

#[test]
fn certification_matrix_reports_mount_failure_and_preserves_existing_data() {
    let directory = tempfile::tempdir().unwrap();
    let mount = tempfile::tempdir().unwrap();
    std::fs::write(mount.path().join("keep"), b"owned data").unwrap();
    let output = Command::new(common::native_binary())
        .args(["certify-filesystem-matrix", "--data-dir"])
        .arg(directory.path())
        .env("TOFU_DB_CERT_MATRIX_MOUNT_DIR", mount.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    let rows = matrix_fields(&String::from_utf8(output.stdout).unwrap());
    assert_eq!(rows.last().unwrap()["matrix_result"], "fail");
    assert_eq!(
        std::fs::read(mount.path().join("keep")).unwrap(),
        b"owned data"
    );
}

#[test]
fn certification_matrix_refuses_relative_target_before_creating_files() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(common::native_binary())
        .args(["certify-filesystem-matrix", "--data-dir", "."])
        .current_dir(directory.path())
        .env_remove("TOFU_DB_CERT_MATRIX_MOUNT_DIR")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(std::fs::read_dir(directory.path())
        .unwrap()
        .next()
        .is_none());
}

#[test]
fn restore_cli_requires_target_lease_before_publishing_a_pin() {
    use fs2::FileExt;
    let source = tempfile::tempdir().unwrap();
    let backup = tempfile::tempdir().unwrap();
    let target = tempfile::tempdir().unwrap();
    let mut engine = Engine::initialize(source.path()).unwrap();
    engine.commit_transaction(b"lease-test", &[b"durable payload"]).unwrap();
    tofu_db::backup::create_incremental_backup(&mut engine, backup.path()).unwrap();
    let lease = std::fs::OpenOptions::new().read(true).write(true).create_new(true)
        .open(target.path().join("LOCK")).unwrap();
    lease.try_lock_exclusive().unwrap();
    let restore = || Command::new(common::native_binary())
        .arg("restore").arg("--backup-dir").arg(backup.path())
        .arg("--data-dir").arg(target.path()).output().unwrap();
    let refused = restore();
    assert!(!refused.status.success());
    assert!(String::from_utf8_lossy(&refused.stderr)
        .contains("another tofu-db process owns the data directory"));
    assert_eq!(std::fs::read_dir(backup.path().join("pins")).unwrap().count(), 0);
    assert_eq!(std::fs::read_dir(target.path()).unwrap().count(), 1);
    drop(lease);
    let completed = restore();
    assert!(completed.status.success(), "{}", String::from_utf8_lossy(&completed.stderr));
    assert_eq!(Engine::open(target.path()).unwrap().state().durable_sequence, 1);
}
