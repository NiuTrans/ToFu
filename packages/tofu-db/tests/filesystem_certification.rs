//! Cross-process certification of the real filesystem-backed operator command.

use std::process::Command;
use std::sync::{Arc, Barrier};
use std::thread;
use std::{collections::HashMap, str::FromStr};

use tofu_db::engine::{BatchTransaction, Engine};
use tofu_db::sequencer::{CommitSequencer, SequencerConfig};

#[test]
fn explicit_empty_path_survives_destructor_free_and_cross_process_reopens() {
    let directory = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
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
    assert!(stdout.contains("payload_block_bytes=1048576"));
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

#[test]
fn certification_refuses_to_reuse_a_nonempty_path() {
    let directory = tempfile::tempdir().unwrap();
    std::fs::write(directory.path().join("user-data"), b"preserve me").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
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
