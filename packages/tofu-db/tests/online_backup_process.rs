//! Cross-process certification for the daemon's online incremental backup
//! (the dedicated `tofu-db-online-backup` driver thread). The supervised
//! `tofu-db serve` process anchors a checkpoint under a short authority hold
//! and copies reachable blocks off-mutex; `system.backup` rings the driver's
//! doorbell and waits out one epoch inside the request deadline,
//! `system.health` / `system.metrics` expose the driver, and the published
//! `blocks/ + snapshots/ + pins/ + gc-runs/` layout restores offline through
//! the `tofu-db restore` CLI.

mod common;

use std::collections::BTreeSet;
use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use tofu_db::authority::AuthorityDatabase;
use tofu_db::daemon::derive_auth_token;
use tofu_db::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use tofu_db::protocol::{
    read_frame, write_frame, Hello, Message, Request, Response, PROTOCOL_VERSION,
};

/// Generous so a slow mount never decides the test: assertions are about
/// receipt contents and on-disk state, never about latency numbers.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// `system.backup` waits for a full epoch inside its request deadline; the
/// task-level certification deadline is 60 s and the socket read timeout
/// must comfortably exceed it.
const BACKUP_DEADLINE: Duration = Duration::from_secs(60);
/// The driver starts with its fallback interval already elapsed, so a fresh
/// daemon runs one fallback epoch at boot. An empty authority anchors in
/// milliseconds; this bound only catches a wedged driver.
const BOOT_EPOCH_BUDGET: Duration = Duration::from_secs(30);
/// A contended anchor hold fails an epoch with the documented retryable
/// 503; retry a bounded number of times so a background hold can never
/// flake the suite.
const BACKUP_ATTEMPTS: u32 = 5;
const SECRET: &str = "online-backup-process-certification-token-0123456789";
/// `STATUS_UNAVAILABLE` from generated_storage_v2.rs (not re-exported);
/// the wire contract value is what this certification pins.
const STATUS_UNAVAILABLE: u16 = 503;
/// Batch shapes for the incrementality certification: a large first batch
/// whose unique 16 KiB document blobs dominate the first epoch's copy, then
/// a small second batch that must copy noticeably less.
const FULL_BATCH_DOCUMENTS: u32 = 120;
const INCREMENTAL_BATCH_DOCUMENTS: u32 = 5;
const DOCUMENT_BLOB_BYTES: usize = 16 * 1024;

struct SupervisedDaemon {
    child: Option<Child>,
    port: u16,
}

fn spawn_daemon(data_dir: &Path, backup_dir: &Path) -> SupervisedDaemon {
    let mut child = Command::new(common::native_binary())
        .arg("serve")
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--owner-id")
        .arg("11")
        .arg("--tenant-id")
        .arg("7")
        .arg("--backup-dir")
        .arg(backup_dir)
        .env("TOFU_STORAGE_TOKEN", SECRET)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut readiness = String::new();
    BufReader::new(child.stdout.as_mut().unwrap())
        .read_line(&mut readiness)
        .unwrap();
    let document: Value = serde_json::from_str(&readiness).unwrap();
    assert_eq!(document["type"], "storage.ready");
    let port = u16::try_from(document["port"].as_u64().unwrap()).unwrap();
    SupervisedDaemon {
        child: Some(child),
        port,
    }
}

impl SupervisedDaemon {
    /// EOF on the parent-lease pipe is the graceful-stop signal; the daemon
    /// then joins every worker, online backup included, and must exit
    /// cleanly.
    fn stop_gracefully(mut self) {
        let mut child = self.child.take().unwrap();
        drop(child.stdin.take());
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

impl Drop for SupervisedDaemon {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            drop(child.stdin.take());
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
}

/// One authenticated storage.v2 connection. Requests are issued
/// sequentially; every response for the operations used here is well under
/// the 1 MiB single-frame ceiling, so no chunk reassembly is needed.
struct WireClient {
    stream: TcpStream,
    next_correlation: u64,
    next_command: u64,
}

impl WireClient {
    fn connect(port: u16) -> Self {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        stream
            .set_read_timeout(Some(BACKUP_DEADLINE + Duration::from_secs(15)))
            .unwrap();
        write_frame(
            &mut stream,
            &Message::Hello(Hello {
                correlation_id: [1; 16],
                minimum_version: PROTOCOL_VERSION,
                maximum_version: PROTOCOL_VERSION,
                schema_ids: vec![STORAGE_SCHEMA_VERSION],
                auth_token: derive_auth_token(SECRET.as_bytes()).unwrap(),
            }),
        )
        .unwrap();
        let Message::Response(acknowledgement) = read_frame(&mut stream).unwrap() else {
            panic!("daemon did not return a Hello acknowledgement");
        };
        assert_eq!(acknowledgement.status, 0);
        Self {
            stream,
            next_correlation: 2,
            next_command: 1,
        }
    }

    fn call_with_deadline(
        &mut self,
        operation: &str,
        payload: Value,
        command_id: Option<String>,
        deadline: Duration,
    ) -> Response {
        let correlation = self.next_correlation;
        self.next_correlation += 1;
        let request = Request {
            correlation_id: (correlation as u128).to_be_bytes(),
            deadline_unix_ms: now_unix_ms() + deadline.as_millis() as u64,
            owner_id: 11,
            tenant_id: Some(7),
            command_id,
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: operation.to_owned(),
            payload: serde_json::to_vec(&payload).unwrap(),
        };
        write_frame(&mut self.stream, &Message::Request(request)).unwrap();
        let message = read_frame(&mut self.stream).unwrap();
        let Message::Response(response) = message else {
            panic!("{operation} returned an unexpected message variant");
        };
        assert_eq!(response.correlation_id, (correlation as u128).to_be_bytes());
        response
    }

    fn call(&mut self, operation: &str, payload: Value, command_id: Option<String>) -> Response {
        self.call_with_deadline(operation, payload, command_id, REQUEST_TIMEOUT)
    }

    /// State-changing operations require a unique command ID (admission).
    fn command(&mut self, operation: &str, payload: Value) -> Response {
        let command = self.next_command;
        self.next_command += 1;
        self.call(
            operation,
            payload,
            Some(format!("online-backup-process-{command}")),
        )
    }

    fn control_json(&mut self, operation: &str) -> Value {
        let response = self.call(operation, json!({}), None);
        assert_eq!(response.status, 0, "{operation} was rejected");
        serde_json::from_slice(&response.payload).unwrap()
    }

    /// Rings the `system.backup` doorbell and waits out one epoch. A
    /// contended anchor hold fails the attempt with the documented
    /// retryable 503; pin that failure shape and retry a bounded number of
    /// times so a background authority hold can never flake the suite.
    fn backup(&mut self) -> Value {
        for attempt in 1..=BACKUP_ATTEMPTS {
            let response =
                self.call_with_deadline("system.backup", json!({}), None, BACKUP_DEADLINE);
            if response.status == 0 {
                return serde_json::from_slice(&response.payload).unwrap();
            }
            assert_eq!(
                response.status, STATUS_UNAVAILABLE,
                "system.backup failure must be the documented retryable 503: {:?}",
                response.error_message
            );
            assert_eq!(response.error_code.as_deref(), Some("database_unavailable"));
            assert!(response.retryable, "system.backup failure must be retryable");
            assert!(
                attempt < BACKUP_ATTEMPTS,
                "system.backup kept failing: {:?}",
                response.error_message
            );
            std::thread::sleep(Duration::from_millis(300));
        }
        unreachable!()
    }
}

/// One upsert per date with a unique 16 KiB blob: every document becomes
/// fresh blocks the next backup epoch must copy.
fn upsert_document(client: &mut WireClient, date: &str, blob: &str) {
    let response = client.command(
        "daily_cost.upsert",
        json!({
            "user_id": 11,
            "date": date,
            "cost": 1.5,
            "conversations": {"blob": blob},
            "computed_at": 1_000,
        }),
    );
    assert_eq!(
        response.status,
        0,
        "write for {date} failed: {:?}",
        response.error_message
    );
}

/// A 16-character unique unit repeated to exactly 16 KiB: unique per
/// (batch, index) pair so content-addressed dedup never collapses two
/// documents into one block.
fn unique_blob(batch: u32, index: u32) -> String {
    format!("{batch:02x}{index:014x}").repeat(DOCUMENT_BLOB_BYTES / 16)
}

fn sha256_file(path: &Path) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(&std::fs::read(path).unwrap());
    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

fn snapshot_manifests(backup_dir: &Path) -> Vec<PathBuf> {
    let mut manifests: Vec<PathBuf> = std::fs::read_dir(backup_dir.join("snapshots"))
        .unwrap()
        .filter_map(|entry| {
            let path = entry.ok()?.path();
            let name = path.file_name()?.to_str()?;
            (path.is_file() && name.starts_with("snapshot-") && name.ends_with(".manifest"))
                .then_some(path)
        })
        .collect();
    manifests.sort();
    manifests
}

fn assert_backup_layout(backup_dir: &Path) {
    for leaf in ["blocks", "snapshots", "pins", "gc-runs"] {
        assert!(
            backup_dir.join(leaf).is_dir(),
            "backup layout is missing {leaf}/"
        );
    }
}

/// The `system.backup` payload is a fixed-shape contract
/// (`tofu-db.system-backup.v1`); pin the exact key set plus every field's
/// documented type. The first group mirrors the scheduler's receipt
/// validation (lib/scheduler/manager.py: the response is a dict, `ok` is
/// True, `backup`/`manifest` are nonempty strings, and `bytes` is an
/// integer >= 0), which is the consumer this RPC exists for.
fn assert_backup_receipt(payload: &Value, backup_dir: &Path) {
    let keys: BTreeSet<&str> = payload
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        BTreeSet::from([
            "contract",
            "ok",
            "backend",
            "artifact_kind",
            "backup",
            "manifest",
            "bytes",
            "sha256",
            "durable_sequence",
            "copied_blocks",
            "copied_block_bytes",
            "manifest_bytes",
            "estimated_bytes",
            "retained_generations",
            "pruned_generations",
            "pruned_block_bytes",
        ])
    );

    // Scheduler receipt shape (lib/scheduler/manager.py).
    assert!(payload.is_object());
    assert!(payload["ok"].is_boolean() && payload["ok"] == json!(true));
    for field in ["backup", "manifest"] {
        assert!(
            payload[field]
                .as_str()
                .is_some_and(|value| !value.trim().is_empty()),
            "scheduler receipt field {field} must be a nonempty string"
        );
    }
    assert!(
        payload["bytes"].is_u64(),
        "scheduler receipt field bytes must be an integer >= 0"
    );

    // Fixed contract fields.
    assert_eq!(payload["contract"], json!("tofu-db.system-backup.v1"));
    assert_eq!(payload["backend"], json!("tofudb"));
    assert_eq!(payload["artifact_kind"], json!("directory"));
    assert_eq!(
        payload["backup"],
        json!(backup_dir.to_string_lossy().as_ref())
    );
    for field in [
        "bytes",
        "durable_sequence",
        "copied_blocks",
        "copied_block_bytes",
        "manifest_bytes",
        "estimated_bytes",
        "retained_generations",
        "pruned_generations",
        "pruned_block_bytes",
    ] {
        assert!(
            payload[field].is_u64(),
            "system.backup field {field} must be a u64"
        );
    }
    // `bytes` is documented as this epoch's copied block bytes plus the
    // manifest bytes.
    assert_eq!(
        payload["bytes"].as_u64().unwrap(),
        payload["copied_block_bytes"].as_u64().unwrap()
            + payload["manifest_bytes"].as_u64().unwrap()
    );

    // `sha256` is the lowercase hex digest of the published manifest file.
    let sha256 = payload["sha256"].as_str().unwrap();
    assert_eq!(sha256.len(), 64, "manifest sha256 must be 64 hex characters");
    assert!(
        sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()),
        "manifest sha256 must be lowercase hex"
    );

    // `manifest` points at the published snapshot file inside the backup
    // directory layout, and every reported byte count matches the disk.
    let manifest = PathBuf::from(payload["manifest"].as_str().unwrap());
    assert!(manifest.is_file(), "receipt manifest must exist on disk");
    assert_eq!(manifest.parent().unwrap(), backup_dir.join("snapshots"));
    let name = manifest.file_name().unwrap().to_str().unwrap();
    assert!(name.starts_with("snapshot-") && name.ends_with(".manifest"));
    assert_eq!(sha256, sha256_file(&manifest));
    assert_eq!(
        payload["manifest_bytes"].as_u64().unwrap(),
        std::fs::metadata(&manifest).unwrap().len()
    );
    assert!(payload["estimated_bytes"].as_u64().unwrap() >= payload["bytes"].as_u64().unwrap());
}

fn assert_online_backup_metric_fields(section: &Value) {
    assert!(section["epochs_completed"].is_u64());
    assert!(section["epochs_failed"].is_u64());
    assert!(section["epoch_in_progress"].is_boolean());
    for optional in ["last_success_unix_ms", "last_error", "last_report"] {
        assert!(
            section.get(optional).is_some(),
            "onlineBackup.{optional} must be present (null before the first epoch)"
        );
    }
}

/// The driver starts with a boot grace (startup is the latency-sensitive
/// phase), so the first epoch only runs when `system.backup` rings it.
/// Assert that no epoch has run yet (and none failed), then return the
/// baseline completion counter for the doorbell assertions.
fn wait_for_boot_epoch(client: &mut WireClient) -> u64 {
    let metrics = client.control_json("system.metrics");
    let section = &metrics["onlineBackup"];
    assert_eq!(section["epochs_completed"].as_u64().unwrap(), 0);
    assert_eq!(
        section["epochs_failed"].as_u64().unwrap(),
        0,
        "boot backup epoch failed: {:?}",
        section["last_error"]
    );
    0
}

#[test]
fn system_backup_publishes_a_complete_restorable_snapshot() {
    let root = tempfile::tempdir().unwrap();
    let data_dir = root.path().join("data");
    let backup_dir = root.path().join("backup");
    std::fs::create_dir_all(&data_dir).unwrap();
    std::fs::create_dir_all(&backup_dir).unwrap();
    AuthorityDatabase::initialize(&data_dir).unwrap();
    let daemon = spawn_daemon(&data_dir, &backup_dir);
    let mut client = WireClient::connect(daemon.port);

    // Health endpoint: the driver is configured and healthy from boot.
    let health = client.control_json("system.health");
    assert_eq!(health["contract"], json!("tofu-db.system-health.v1"));
    assert_eq!(health["backend"], json!("tofudb"));
    assert_eq!(health["ready"], json!(true));
    assert_eq!(
        health["components"]["onlineBackup"],
        json!({"configured": true, "healthy": true})
    );

    // Metrics endpoint: the onlineBackup section is present with the full
    // epoch counter set from boot.
    let metrics = client.control_json("system.metrics");
    assert_eq!(metrics["contract"], json!("tofu-db.system-metrics.v1"));
    assert_online_backup_metric_fields(&metrics["onlineBackup"]);

    let baseline = wait_for_boot_epoch(&mut client);

    // One batch of documents, each carrying a unique 16 KiB blob.
    let days: Vec<String> = (1..=20).map(|day| format!("2026-03-{day:02}")).collect();
    for (index, date) in days.iter().enumerate() {
        upsert_document(&mut client, date, &unique_blob(0, index as u32));
    }

    // End-to-end epoch: the receipt honors the fixed contract, the
    // manifest exists on disk, and the layout is complete.
    let receipt = client.backup();
    assert_backup_receipt(&receipt, &backup_dir);
    assert_backup_layout(&backup_dir);
    assert!(receipt["copied_blocks"].as_u64().unwrap() > 0);
    assert!(receipt["durable_sequence"].as_u64().unwrap() > 0);
    // The first doorbell generation is the only one so far.
    let manifests = snapshot_manifests(&backup_dir);
    assert_eq!(manifests.len(), 1);
    assert!(manifests.contains(&PathBuf::from(receipt["manifest"].as_str().unwrap())));
    assert_eq!(receipt["retained_generations"].as_u64().unwrap(), 1);
    assert_eq!(receipt["pruned_generations"].as_u64().unwrap(), 0);

    // The metrics section reflects the completed epoch.
    let metrics = client.control_json("system.metrics");
    let section = &metrics["onlineBackup"];
    assert_eq!(section["epochs_completed"].as_u64().unwrap(), baseline + 1);
    assert!(section["last_success_unix_ms"].is_u64());
    assert_eq!(section["last_error"], Value::Null);
    assert_eq!(
        section["last_report"]["durable_sequence"],
        receipt["durable_sequence"]
    );
    assert_eq!(section["last_report"]["manifest"], receipt["manifest"]);

    // Health still reports a configured, healthy driver after real work.
    let health = client.control_json("system.health");
    assert_eq!(health["ready"], json!(true));
    assert_eq!(
        health["components"]["onlineBackup"],
        json!({"configured": true, "healthy": true})
    );

    daemon.stop_gracefully();

    // Offline restore into a fresh target with the CLI.
    let restore_dir = root.path().join("restored");
    let output = Command::new(common::native_binary())
        .arg("restore")
        .arg("--backup-dir")
        .arg(&backup_dir)
        .arg("--data-dir")
        .arg(&restore_dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("format=tofu-db.restore.v1"));
    assert!(stdout.contains(&format!(
        "durable_sequence={}",
        receipt["durable_sequence"].as_u64().unwrap()
    )));

    // Serve the restored authority and verify the written content over the
    // wire: every persisted date survived, and the newest document carries
    // its exact 16 KiB blob.
    let restored = spawn_daemon(&restore_dir, &root.path().join("restored-backup"));
    let mut client = WireClient::connect(restored.port);
    let dates = client.call(
        "daily_cost.persisted_dates",
        json!({"user_id": 11, "dates": days}),
        None,
    );
    assert_eq!(dates.status, 0);
    let dates: Value = serde_json::from_slice(&dates.payload).unwrap();
    let persisted: BTreeSet<String> = dates["dates"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| value.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(persisted.len(), 20);
    for date in &days {
        assert!(persisted.contains(date), "restored authority is missing {date}");
    }
    let latest = client.call("daily_cost.latest", json!({"user_id": 11}), None);
    assert_eq!(latest.status, 0);
    let latest: Value = serde_json::from_slice(&latest.payload).unwrap();
    assert_eq!(latest["date"], json!("2026-03-20"));
    assert_eq!(
        latest["conversations"]["blob"],
        json!(unique_blob(0, 19))
    );
    restored.stop_gracefully();
}

#[test]
fn successive_epochs_copy_incrementally_and_retain_two_generations() {
    let root = tempfile::tempdir().unwrap();
    let data_dir = root.path().join("data");
    let backup_dir = root.path().join("backup");
    std::fs::create_dir_all(&data_dir).unwrap();
    std::fs::create_dir_all(&backup_dir).unwrap();
    AuthorityDatabase::initialize(&data_dir).unwrap();
    let daemon = spawn_daemon(&data_dir, &backup_dir);
    let mut client = WireClient::connect(daemon.port);
    wait_for_boot_epoch(&mut client);

    // Batch 1: 120 documents with unique 16 KiB blobs (~1.9 MiB of
    // payload); the first driven epoch copies the whole reachable graph.
    let mut index = 0;
    for month in 1..=12_u32 {
        for day in 1..=10_u32 {
            upsert_document(
                &mut client,
                &format!("2026-{month:02}-{day:02}"),
                &unique_blob(1, index),
            );
            index += 1;
        }
    }
    assert_eq!(index, FULL_BATCH_DOCUMENTS);
    let first = client.backup();
    assert_backup_receipt(&first, &backup_dir);
    // Batch 1 opens the generation history.
    assert_eq!(first["retained_generations"].as_u64().unwrap(), 1);
    assert_eq!(first["pruned_generations"].as_u64().unwrap(), 0);

    // Batch 2: only 5 documents, so the second epoch copies strictly less
    // than the first while the durable sequence keeps advancing.
    for day in 1..=INCREMENTAL_BATCH_DOCUMENTS {
        upsert_document(
            &mut client,
            &format!("2027-01-{day:02}"),
            &unique_blob(2, day),
        );
    }
    let second = client.backup();
    assert_backup_receipt(&second, &backup_dir);
    assert!(
        second["durable_sequence"].as_u64().unwrap() > first["durable_sequence"].as_u64().unwrap(),
        "durable sequence must advance between epochs"
    );
    let first_blocks = first["copied_blocks"].as_u64().unwrap();
    let second_blocks = second["copied_blocks"].as_u64().unwrap();
    assert!(
        second_blocks <= first_blocks,
        "incremental epoch copied {second_blocks} blocks, full epoch copied {first_blocks}"
    );
    let first_bytes = first["copied_block_bytes"].as_u64().unwrap();
    let second_bytes = second["copied_block_bytes"].as_u64().unwrap();
    assert!(
        second_bytes < first_bytes,
        "incremental epoch copied {second_bytes} bytes, full epoch copied {first_bytes}"
    );
    // Two generations exist (batch 1, batch 2); both fit inside retention.
    assert_eq!(second["retained_generations"].as_u64().unwrap(), 2);
    assert_eq!(second["pruned_generations"].as_u64().unwrap(), 0);

    // Batch 3: one more document; the third epoch pushes batch 1's
    // generation out of retention. That generation's superseded tree nodes
    // and root are unreachable from the two retained generations, so the
    // prune physically removes blocks this time.
    upsert_document(&mut client, "2027-02-01", &unique_blob(3, 0));
    let third = client.backup();
    assert_backup_receipt(&third, &backup_dir);
    assert!(
        third["durable_sequence"].as_u64().unwrap() > second["durable_sequence"].as_u64().unwrap()
    );
    assert_eq!(third["retained_generations"].as_u64().unwrap(), 2);
    assert_eq!(third["pruned_generations"].as_u64().unwrap(), 1);
    assert!(third["pruned_block_bytes"].as_u64().unwrap() > 0);

    // Retention is physical: exactly the two newest manifests remain.
    let manifests = snapshot_manifests(&backup_dir);
    assert_eq!(manifests.len(), 2);
    assert!(manifests.contains(&PathBuf::from(second["manifest"].as_str().unwrap())));
    assert!(manifests.contains(&PathBuf::from(third["manifest"].as_str().unwrap())));
    assert!(!Path::new(first["manifest"].as_str().unwrap()).exists());

    daemon.stop_gracefully();
}
