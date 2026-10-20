//! Cross-process certification for the pre-authority supervised daemon boundary.

mod common;

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::process::{Command, Stdio};

use tofu_db::authority::AuthorityDatabase;
use tofu_db::daemon::derive_auth_token;
use tofu_db::entity::EntityKey;
use tofu_db::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use tofu_db::protocol::{read_frame, write_frame, Hello, Message, PROTOCOL_VERSION};

#[test]
fn busy_search_projection_does_not_gate_requests_and_recovers_after_lease_release() {
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
    use tofu_db::protocol::Request;
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("authority");
    let projection = directory.path().join("search");
    let mut database = AuthorityDatabase::initialize(&source).unwrap();
    let key = EntityKey::new(7, 11, "record", b"restart-sentinel").unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database.entity_put(&mut transaction, key.clone(), b"durable source".to_vec()).unwrap();
    database.commit(transaction).unwrap();
    drop(database);
    let held_projection = AuthorityDatabase::initialize(&projection).unwrap();
    let secret = "synthetic-search-restart-test-token-0123456789";
    let started = Instant::now();
    let mut child = Command::new(common::native_binary())
        .args(["serve", "--data-dir"]).arg(&source)
        .args(["--search-projection-dir"]).arg(&projection)
        .args(["--owner-id", "11", "--tenant-id", "7"])
        .env("TOFU_STORAGE_TOKEN", secret)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().unwrap();
    let stdout = child.stdout.take().unwrap();
    let (sender, receiver) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut line = String::new();
        BufReader::new(stdout).read_line(&mut line).unwrap();
        let _ = sender.send(line);
    });
    let readiness = receiver.recv_timeout(Duration::from_secs(5));
    if readiness.is_err() { child.kill().unwrap(); }
    reader.join().unwrap();
    let readiness: serde_json::Value = serde_json::from_str(&readiness.unwrap()).unwrap();
    assert_eq!(readiness["type"], "storage.ready", "{readiness}");
    assert_eq!(readiness["resourceBudget"]["searchProjectionAvailable"], false);
    let mut client = TcpStream::connect(("127.0.0.1", readiness["port"].as_u64().unwrap() as u16)).unwrap();
    client.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    write_frame(&mut client, &Message::Hello(Hello {
        correlation_id: [1; 16], minimum_version: PROTOCOL_VERSION, maximum_version: PROTOCOL_VERSION,
        schema_ids: vec![STORAGE_SCHEMA_VERSION], auth_token: derive_auth_token(secret.as_bytes()).unwrap(),
    })).unwrap();
    let Message::Response(hello) = read_frame(&mut client).unwrap() else { panic!("missing Hello"); };
    assert_eq!(hello.status, 0);
    let ready_ms = started.elapsed().as_millis();
    let search = |client: &mut TcpStream| {
        write_frame(client, &Message::Request(Request {
            correlation_id: [2; 16], deadline_unix_ms: SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64 + 2000,
            owner_id: 11, tenant_id: Some(7), command_id: None, schema_id: STORAGE_SCHEMA_VERSION,
            operation: "conversation.search".into(), payload: br#"{"user_id":11,"query":"needle","page":true}"#.to_vec(),
        })).unwrap();
        let Message::Response(response) = read_frame(client).unwrap() else { panic!("missing search response"); };
        response
    };
    let unavailable = search(&mut client);
    assert_eq!(unavailable.error_code.as_deref(), Some("database_unavailable"));
    assert!(unavailable.retryable);
    drop(held_projection);
    let recovery = Instant::now();
    loop {
        let response = search(&mut client);
        if response.status == 0 { break; }
        assert!(response.retryable && recovery.elapsed() < Duration::from_secs(10), "{response:?}");
        std::thread::sleep(Duration::from_millis(20));
    }
    let recovery_ms = recovery.elapsed().as_millis();
    drop(client);
    let stopping = Instant::now();
    drop(child.stdin.take());
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    assert!(stopping.elapsed() < Duration::from_secs(5));
    let shutdown_ms = stopping.elapsed().as_millis();
    let reopened = AuthorityDatabase::open(&source).unwrap();
    let mut transaction = reopened.begin(7, 11).unwrap();
    assert_eq!(reopened.entity_get(&mut transaction, &key).unwrap().unwrap(), b"durable source");
    eprintln!("deferred_search ready_and_hello_ms={ready_ms} retry_recovery_ms={recovery_ms} shutdown_ms={shutdown_ms}");
}

#[test]
fn read_only_daemon_reopens_do_not_accumulate_unpublished_payload_segments() {
    use std::collections::BTreeMap;
    use std::path::{Path, PathBuf};
    use tofu_db::engine::Engine;
    fn files(root: &Path) -> BTreeMap<PathBuf, (u64, blake3::Hash)> {
        let mut files = BTreeMap::new();
        let mut directories = vec![root.to_path_buf()];
        while let Some(directory) = directories.pop() {
            for entry in std::fs::read_dir(directory).unwrap() {
                let entry = entry.unwrap();
                if entry.file_type().unwrap().is_dir() {
                    directories.push(entry.path());
                } else {
                    let bytes = std::fs::read(entry.path()).unwrap();
                    assert!(bytes.len() < 8 * 1024 * 1024);
                    files.insert(
                        entry.path().strip_prefix(root).unwrap().to_path_buf(),
                        (bytes.len() as u64, blake3::hash(&bytes)),
                    );
                }
                assert!(files.len() + directories.len() < 4096);
            }
        }
        files
    }
    let directory = tempfile::tempdir().unwrap();
    let key = EntityKey::new(7, 11, "record", b"reopen-live").unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key.clone(), b"reopen-value".to_vec())
        .unwrap();
    database.commit(transaction).unwrap();
    drop(database);
    let mut engine = Engine::open(directory.path()).unwrap();
    let payload = vec![0x51; 1536 * 1024];
    let id = engine
        .commit_transaction(b"reopen-payload", &[&payload])
        .unwrap()
        .block_ids[0];
    drop(engine);
    let mut settled = None;
    for _ in 0..3 {
        let mut child = Command::new(common::native_binary())
            .args(["serve", "--data-dir"])
            .arg(directory.path())
            .args(["--owner-id", "11", "--tenant-id", "7"])
            .env(
                "TOFU_STORAGE_TOKEN",
                "synthetic-reopen-test-token-01234567890123456789",
            )
            .env("TOFU_DB_FSYNC_STATS", "1")
            .env_remove("TOFU_DB_DISABLE_BACKGROUND_PACK")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut readiness = String::new();
        BufReader::new(child.stdout.as_mut().unwrap())
            .read_line(&mut readiness)
            .unwrap();
        let ready: serde_json::Value = serde_json::from_str(&readiness).unwrap();
        let interval = ready["resourceBudget"]["maintenanceIdleIntervalMilliseconds"]
            .as_u64()
            .unwrap();
        assert!(interval > 0 && interval <= 1000);
        std::thread::sleep(std::time::Duration::from_millis(2 * interval + 500));
        drop(child.stdin.take());
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.entity_get(&mut read, &key).unwrap(),
            Some(b"reopen-value".to_vec())
        );
        drop(read);
        drop(database);
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.read_block(id).unwrap(), payload);
        // Either the adoption driver or idle maintenance can publish the
        // pack. Certify durable adoption and no subsequent file growth,
        // independently of which worker won the scheduling race.
        assert!(engine.payload_segment_count() > 0);
        drop(engine);
        let after = files(directory.path());
        let wal: BTreeMap<_, _> = after
            .iter()
            .filter(|(p, _)| p.extension().is_some_and(|e| e == "wal"))
            .map(|(p, v)| (p.clone(), *v))
            .collect();
        // A graceful stop retires the active WAL window (shutdown
        // checkpoint): with no new writes during the cycle, every remaining
        // WAL file is the empty new generation and the next serve replays
        // nothing.
        assert!(
            wal.values().all(|(size, _)| *size == 0),
            "expected the shutdown checkpoint to leave only empty WAL generations, got {wal:?}",
        );
        if let Some(previous) = &settled {
            assert_eq!(&after, previous);
        }
        settled = Some(after);
    }
}

#[test]
fn serve_publishes_no_secret_and_exits_when_parent_pipe_closes() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let authority_uuid = tofu_db::engine::Engine::open(directory.path())
        .unwrap()
        .state()
        .authority_uuid
        .to_string();
    let secret = "private-test-token-that-must-not-be-rendered-123456";
    let mut child = Command::new(common::native_binary())
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
    assert_eq!(document["authorityUuid"], authority_uuid);
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
        4163
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
    let output = Command::new(common::native_binary())
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
    let verdict: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(verdict["type"], "storage.error");
    assert_eq!(verdict["code"], "database_unavailable");
    assert_eq!(verdict["retryable"], false);
    assert!(!missing.exists());
}

#[test]
fn explicit_activity_index_backfill_is_bounded_and_idempotent() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();

    let run = |maximum_rows: &str| {
        Command::new(common::native_binary())
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

    let mut child = Command::new(common::native_binary())
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
