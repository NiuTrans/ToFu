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
        .env("TOFU_DB_RECOVERY_PROFILE", "1")
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
    #[cfg(feature = "recovery-profile")]
    {
        let report = recovery_report(&output.stderr).unwrap();
        assert_eq!(report["completed"], false);
        assert!(report["phases"].as_array().unwrap().iter().any(|phase|
            phase["phase"] == "wal_scan" && phase["entered"] == true));
        assert!(report["phases"].as_array().unwrap().iter().all(|phase|
            phase["phase"] != "payload_replay" || phase["entered"] == false));
    }
    assert_eq!(std::fs::read(control_path).unwrap(), control_before);
    assert_eq!(std::fs::read(wal_path).unwrap(), wal_before);
}

fn recovery_report(stderr: &[u8]) -> Option<serde_json::Value> {
    let reports: Vec<_> = String::from_utf8_lossy(stderr).lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .filter(|row| row["target"] == "recovery_profile")
        .map(|row| serde_json::from_str::<serde_json::Value>(row["message"].as_str().unwrap()).unwrap())
        .collect();
    assert!(reports.len() <= 1, "one Engine::open emitted multiple recovery reports");
    reports.into_iter().next()
}

#[test]
#[cfg(feature = "recovery-profile")]
fn recovery_profile_is_opt_in_and_accounts_for_the_actual_replayed_prefix() {
    let directory = tempfile::tempdir().unwrap();
    let mut engine = Engine::initialize(directory.path()).unwrap();
    for payload in [b"one", b"two", b"tri"] { engine.commit(payload).unwrap(); }
    drop(engine);
    let quiet = Command::new(common::native_binary())
        .args(["inspect", "--data-dir"]).arg(directory.path())
        .env_remove("TOFU_DB_RECOVERY_PROFILE").output().unwrap();
    assert!(quiet.status.success());
    assert!(recovery_report(&quiet.stderr).is_none());

    let observed = Command::new(common::native_binary())
        .args(["inspect", "--data-dir"]).arg(directory.path())
        .env("TOFU_DB_RECOVERY_PROFILE", "1").output().unwrap();
    assert!(observed.status.success());
    let report = recovery_report(&observed.stderr).unwrap();
    assert_eq!(report["completed"], true);
    assert_eq!(report["work"]["wal_records"], 3);
    assert_eq!(report["work"]["replayed_transactions"], 3);
    assert_eq!(report["work"]["sealed_wal_records"], 0);
    assert_eq!(report["phases"].as_array().unwrap().iter()
        .map(|phase| phase["elapsedNanos"].as_u64().unwrap()).sum::<u64>(),
        report["elapsedNanos"].as_u64().unwrap());
    assert_eq!(observed.stdout, quiet.stdout);
}

#[test]
#[cfg(not(feature = "recovery-profile"))]
fn recovery_profile_environment_cannot_activate_an_uncompiled_probe() {
    let directory = tempfile::tempdir().unwrap();
    let mut engine = Engine::initialize(directory.path()).unwrap();
    engine.commit(b"retained").unwrap();
    drop(engine);
    let output = Command::new(common::native_binary())
        .args(["inspect", "--data-dir"]).arg(directory.path())
        .env("TOFU_DB_RECOVERY_PROFILE", "1").output().unwrap();
    assert!(output.status.success());
    assert!(recovery_report(&output.stderr).is_none());
    let recovered = Engine::open(directory.path()).unwrap();
    assert_eq!(recovered.committed_transactions().len(), 1);
    assert_eq!(recovered.committed_transactions()[0].envelope.inline_payload, b"retained");
}
