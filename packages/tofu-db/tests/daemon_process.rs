//! Cross-process certification for the pre-authority supervised daemon boundary.

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::process::{Command, Stdio};

use tofu_db::authority::AuthorityDatabase;
use tofu_db::daemon::derive_auth_token;
use tofu_db::entity::EntityKey;
use tofu_db::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use tofu_db::protocol::{read_frame, write_frame, Hello, Message, PROTOCOL_VERSION};

#[test]
fn serve_publishes_no_secret_and_exits_when_parent_pipe_closes() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let secret = "private-test-token-that-must-not-be-rendered-123456";
    let mut child = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("serve")
        .arg("--data-dir")
        .arg(directory.path())
        .arg("--owner-id")
        .arg("11")
        .arg("--tenant-id")
        .arg("7")
        .env("TOFU_STORAGE_TOKEN", secret)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();

    let mut readiness = String::new();
    BufReader::new(child.stdout.as_mut().unwrap())
        .read_line(&mut readiness)
        .unwrap();
    let document: serde_json::Value = serde_json::from_str(&readiness).unwrap();
    assert_eq!(document["type"], "storage.ready");
    assert_eq!(document["protocol"], 2);
    assert_eq!(document["backend"], "tofudb");
    assert_eq!(document["preAuthority"], true);
    assert!(document["port"].as_u64().is_some_and(|port| port > 0));
    assert!(matches!(
        document["resourceBudget"]["historyRetainedSegments"].as_u64(),
        Some(16 | 64)
    ));
    assert_eq!(
        document["resourceBudget"]["authorityGcMaximumVictimBytes"],
        256 * 1024 * 1024
    );
    assert_eq!(
        document["resourceBudget"]["authorityGcMaximumBlocksPerRound"],
        65_536
    );
    assert_eq!(
        document["resourceBudget"]["authorityGcMaximumPayloadSegmentsScanned"],
        16
    );
    assert_eq!(
        document["resourceBudget"]["authorityGcMaximumPayloadSegmentFilesScanned"],
        4097
    );
    assert_eq!(
        document["resourceBudget"]["authorityGcMaximumOrphanPayloadSegmentFilesRemoved"],
        1
    );
    assert_eq!(
        document["resourceBudget"]["authorityGcMinimumPayloadCompactionBlocks"],
        128
    );
    assert_eq!(
        document["resourceBudget"]["authorityGcMaximumTemporaryBlockFilesRemoved"],
        1
    );
    assert!(
        document["resourceBudget"]["authorityGcMaximumTemporaryBytes"]
            .as_u64()
            .is_some_and(|bytes| bytes > 0 && bytes <= 1024 * 1024 * 1024)
    );
    assert!(!readiness.contains(secret));
    assert!(!readiness.contains(directory.path().to_str().unwrap()));

    let mut client = TcpStream::connect((
        "127.0.0.1",
        u16::try_from(document["port"].as_u64().unwrap()).unwrap(),
    ))
    .unwrap();
    client
        .set_read_timeout(Some(std::time::Duration::from_secs(1)))
        .unwrap();
    write_frame(
        &mut client,
        &Message::Hello(Hello {
            correlation_id: [1; 16],
            minimum_version: PROTOCOL_VERSION,
            maximum_version: PROTOCOL_VERSION,
            schema_ids: vec![STORAGE_SCHEMA_VERSION],
            auth_token: derive_auth_token(secret.as_bytes()).unwrap(),
        }),
    )
    .unwrap();
    let Message::Response(acknowledgement) = read_frame(&mut client).unwrap() else {
        panic!("daemon did not return a Hello acknowledgement");
    };
    assert_eq!(acknowledgement.status, 0);
    assert_eq!(acknowledgement.schema_id, STORAGE_SCHEMA_VERSION);
    drop(client);

    drop(child.stdin.take());
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!String::from_utf8_lossy(&output.stderr).contains(secret));
}

#[test]
fn serve_refuses_to_create_a_missing_authority_before_readiness() {
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("missing");
    let output = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("serve")
        .arg("--data-dir")
        .arg(&missing)
        .arg("--owner-id")
        .arg("11")
        .env(
            "TOFU_STORAGE_TOKEN",
            "private-test-token-that-is-long-enough-123456",
        )
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert!(!missing.exists());
}

#[test]
fn explicit_activity_index_backfill_is_bounded_and_idempotent() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();

    let run = |maximum_rows: &str| {
        Command::new(env!("CARGO_BIN_EXE_tofu-db"))
            .arg("backfill-activity-index")
            .arg("--data-dir")
            .arg(directory.path())
            .arg("--tenant-id")
            .arg("7")
            .arg("--owner-id")
            .arg("11")
            .arg("--maximum-rows")
            .arg(maximum_rows)
            .arg("--execute")
            .output()
            .unwrap()
    };
    let first = run("1");
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let first_stdout = String::from_utf8(first.stdout).unwrap();
    assert!(first_stdout.contains("format=tofu-db.activity-index-backfill.v1"));
    assert!(first_stdout.contains("processed_rows=0"));
    assert!(first_stdout.contains("complete=true"));
    assert!(first_stdout.contains("committed=true"));
    assert!(first_stdout.contains("maximum_rows_per_transaction=256"));

    let repeated = run("1");
    assert!(repeated.status.success());
    assert!(String::from_utf8(repeated.stdout)
        .unwrap()
        .contains("committed=false"));

    let oversized = run("257");
    assert!(!oversized.status.success());
    assert!(String::from_utf8(oversized.stderr)
        .unwrap()
        .contains("activity backfill row bound is invalid"));
}

#[test]
fn serve_starts_bounded_mount_maintenance_only_after_readiness() {
    let directory = tempfile::tempdir().unwrap();
    let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
    let key =
        |index: u16| EntityKey::new(7, 11, "daemon_maintenance", &index.to_be_bytes()).unwrap();
    let mut seed = authority.begin(7, 11).unwrap();
    for index in 0..4 {
        authority
            .entity_put(&mut seed, key(index), vec![index as u8; 64])
            .unwrap();
    }
    authority.commit(seed).unwrap();
    let ranges = vec![(key(0), key(10))];
    let mut pin = authority.begin(7, 11).unwrap();
    authority
        .stage_persistent_range_snapshot_pin(&mut pin, b"daemon-mount", &ranges)
        .unwrap();
    authority.commit(pin).unwrap();
    let mut retire = authority.begin(7, 11).unwrap();
    authority
        .entity_retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
        .unwrap();
    authority.commit(retire).unwrap();
    let mut restore = authority.begin(7, 11).unwrap();
    authority
        .stage_persistent_range_snapshot_restore(&mut restore, b"daemon-mount", &ranges)
        .unwrap();
    authority.commit(restore).unwrap();
    drop(authority);

    let mut child = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("serve")
        .arg("--data-dir")
        .arg(directory.path())
        .arg("--owner-id")
        .arg("11")
        .arg("--tenant-id")
        .arg("7")
        .env(
            "TOFU_STORAGE_TOKEN",
            "private-maintenance-token-that-is-long-enough-123456",
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut readiness = String::new();
    BufReader::new(child.stdout.as_mut().unwrap())
        .read_line(&mut readiness)
        .unwrap();
    let document: serde_json::Value = serde_json::from_str(&readiness).unwrap();
    let interval = document["resourceBudget"]["maintenanceIdleIntervalMilliseconds"]
        .as_u64()
        .unwrap();
    assert!(interval > 0 && interval <= 1000);
    std::thread::sleep(std::time::Duration::from_millis(
        interval.saturating_mul(2).saturating_add(500),
    ));
    drop(child.stdin.take());
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );

    let authority = AuthorityDatabase::open(directory.path()).unwrap();
    let mut maintenance = authority.begin(7, 11).unwrap();
    assert!(authority
        .consolidate_one_entity_range_mount(&mut maintenance)
        .unwrap()
        .is_none());
    let mut read = authority.begin(7, 11).unwrap();
    assert_eq!(
        authority.entity_get(&mut read, &key(3)).unwrap(),
        Some(vec![3; 64])
    );
}
