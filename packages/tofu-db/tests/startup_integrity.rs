//! Startup integrity failures cross the real process boundary without retrying
//! or modifying the CONTROL/WAL evidence needed by an operator.

mod common;

use std::process::{Command, Stdio};

use tofu_db::control::ControlFile;
use tofu_db::engine::Engine;

#[test]
fn missing_acknowledged_wal_suffix_is_a_terminal_startup_error() {
    let directory = tempfile::tempdir().unwrap();
    drop(Engine::initialize(directory.path()).unwrap());
    let mut control = ControlFile::open(directory.path()).unwrap();
    let mut state = control.read_current().unwrap();
    state.generation += 1;
    state.durable_sequence = 1;
    state.root_hash = [1; 32];
    control.publish(&state).unwrap();
    drop(control);
    let control_path = directory.path().join("CONTROL");
    let wal_path = directory.path().join("active.wal");
    let control_before = std::fs::read(&control_path).unwrap();
    let wal_before = std::fs::read(&wal_path).unwrap();

    let output = Command::new(common::native_binary())
        .args(["serve", "--data-dir"])
        .arg(directory.path())
        .args(["--tenant-id", "7", "--owner-id", "11"])
        .env("TOFU_STORAGE_TOKEN", "startup-integrity-test-token-0123456789")
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    let verdict = stdout
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .find(|value| value["type"] == "storage.error")
        .unwrap_or_else(|| panic!("missing startup verdict: {stdout}"));
    assert_eq!(verdict["protocol"], tofu_db::protocol::PROTOCOL_VERSION);
    assert_eq!(verdict["code"], "database_integrity");
    assert_eq!(verdict["retryable"], false);
    let diagnostic = verdict["diagnostic"].as_str().unwrap();
    assert!(diagnostic.contains("CONTROL points beyond active WAL"));
    assert!(diagnostic.contains("durable=1"));
    assert!(diagnostic.contains("recovered=0"));
    assert_eq!(std::fs::read(control_path).unwrap(), control_before);
    assert_eq!(std::fs::read(wal_path).unwrap(), wal_before);
}
