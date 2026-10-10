//! Cross-process `verify`/`salvage` CLI coverage: the operator entry
//! points, their machine-readable summaries, and the restore equivalence
//! of a salvage target.

mod common;

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use tofu_db::authority::AuthorityDatabase;
use tofu_db::entity::EntityKey;

/// Seeds through the public persona: merge + checkpoint with the exact
/// overlay contract (the daemon shutdown tail does the same).
fn seeded_authority() -> tempfile::TempDir {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let key = EntityKey::new(7, 11, "record", b"cli-key").unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key, b"cli-value".to_vec())
        .unwrap();
    database.commit(transaction).unwrap();
    drop(database);
    let mut persona = tofu_db::entity::EntityDatabase::open(directory.path()).unwrap();
    persona.merge_overlay_fully().unwrap();
    persona.writer_engine_mut().unwrap().checkpoint().unwrap();
    drop(persona);
    directory
}

fn run_cli(args: &[&str]) -> std::process::Output {
    Command::new(common::native_binary())
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap()
}

#[test]
fn verify_reports_ok_and_degraded_exactly() {
    let directory = seeded_authority();
    let data = directory.path().to_str().unwrap();
    let output = run_cli(&["verify", "--data-dir", data]);
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("format=tofu-db.verify.v1"), "{stdout}");
    assert!(stdout.contains("verdict=ok"), "{stdout}");
    assert!(stdout.contains("pages_verified="), "{stdout}");

    // Torn tail: garbage after the last valid record of generation 2.
    let mut wal = std::fs::OpenOptions::new()
        .append(true)
        .open(directory.path().join("active-0000000000000002.wal"))
        .unwrap();
    wal.write_all(b"torn-cli-tail").unwrap();
    drop(wal);
    let output = run_cli(&["verify", "--data-dir", data]);
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("verdict=degraded"), "{stdout}");
    assert!(
        stdout
            .split_whitespace()
            .any(|field| field.starts_with("wal_tail_truncated_bytes=")
                && !field.ends_with("=0")),
        "{stdout}"
    );
}

#[test]
fn verify_fails_closed_on_fatal_control_damage() {
    let directory = seeded_authority();
    std::fs::write(directory.path().join("CONTROL"), vec![0x5a; 8192]).unwrap();
    let data = directory.path().to_str().unwrap();
    let output = run_cli(&["verify", "--data-dir", data]);
    assert!(!output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("verdict=fatal"), "{stdout}");
}

#[test]
fn salvage_publishes_a_restorable_target() {
    let directory = seeded_authority();
    let data = directory.path().to_str().unwrap();
    let target = tempfile::tempdir().unwrap();
    let salvage_dir = target.path().join("salvage");
    let output = run_cli(&[
        "salvage",
        "--data-dir",
        data,
        "--backup-dir",
        salvage_dir.to_str().unwrap(),
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("format=tofu-db.salvage.v1"), "{stdout}");
    assert!(stdout.contains("verdict=ok"), "{stdout}");
    assert!(stdout.contains("lost_blocks=0"), "{stdout}");

    // The salvage output restores into a fresh authority with the seeded content.
    let restored_parent = tempfile::tempdir().unwrap();
    let restored = restored_parent.path().join("restored");
    std::fs::create_dir(&restored).unwrap();
    let output = run_cli(&[
        "restore",
        "--backup-dir",
        salvage_dir.to_str().unwrap(),
        "--data-dir",
        restored.to_str().unwrap(),
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let database = AuthorityDatabase::open(&restored).unwrap();
    let key = EntityKey::new(7, 11, "record", b"cli-key").unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    assert_eq!(
        database.entity_get(&mut transaction, &key).unwrap(),
        Some(b"cli-value".to_vec())
    );
}

#[test]
fn salvage_refuses_a_nonempty_target() {
    let directory = seeded_authority();
    let data = directory.path().to_str().unwrap();
    let target = tempfile::tempdir().unwrap();
    std::fs::write(target.path().join("occupied"), b"x").unwrap();
    let output = run_cli(&[
        "salvage",
        "--data-dir",
        data,
        "--backup-dir",
        target.path().to_str().unwrap(),
    ]);
    assert!(!output.status.success());
    let _ = data;
}
