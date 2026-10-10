//! Cross-process certification for the daemon's online garbage collector
//! (docs/TOFUDB_ONLINE_GC.md). The supervised `tofu-db serve` process runs
//! two-mark reclaim epochs on its dedicated `tofu-db-online-gc` thread while
//! the wire protocol keeps serving foreground traffic; `system.health` and
//! `system.metrics` expose the collector, and `system.reclaim` rings the
//! expedite doorbell without ever requiring offline access.

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
/// errors and completion, never about foreground latency numbers.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// Overall budget for driving enough expedited epochs to two-mark-confirm
/// the injected garbage. The production floor between epochs is 120 s
/// (`ONLINE_GC_MIN_EPOCH_INTERVAL`) paces every epoch start — the
/// doorbell-proof floor admits no trigger faster than one start per
/// interval plus a ≤50% duty cycle — and the boot grace delays the first
/// epoch, so two-mark confirmation needs up to two paced starts plus
/// margin; this bound only catches a wedged collector.
const EPOCH_DRIVE_BUDGET: Duration = Duration::from_secs(400);
/// One expedited-epoch attempt must make observable progress within this
/// window (the driver polls every 250 ms). A contended snapshot hold defers
/// the attempt without completing an epoch; the next loop iteration
/// re-rings the doorbell instead of failing.
const EPOCH_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(15);
const SECRET: &str = "online-gc-process-certification-token-0123456789";
const OVERWRITE_COMMITS: u32 = 200;
const OVERWRITTEN_DOCUMENT_BYTES: usize = 32 * 1024;
const ABANDONED_TEMPORARY_BYTES: usize = 40 * 1024;
/// `STATUS_BAD_REQUEST` from generated_storage_v2.rs (not re-exported);
/// the wire contract value is what this certification pins.
const STATUS_BAD_REQUEST: u16 = 400;

struct SupervisedDaemon {
    child: Option<Child>,
    port: u16,
}

fn spawn_daemon(data_dir: &Path) -> SupervisedDaemon {
    let mut child = Command::new(common::native_binary())
        .arg("serve")
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--owner-id")
        .arg("11")
        .arg("--tenant-id")
        .arg("7")
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
    /// then joins every worker, online GC included, and must exit cleanly.
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
            .set_read_timeout(Some(REQUEST_TIMEOUT + Duration::from_secs(5)))
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

    fn call(&mut self, operation: &str, payload: Value, command_id: Option<String>) -> Response {
        let correlation = self.next_correlation;
        self.next_correlation += 1;
        let request = Request {
            correlation_id: (correlation as u128).to_be_bytes(),
            deadline_unix_ms: now_unix_ms() + REQUEST_TIMEOUT.as_millis() as u64,
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

    /// State-changing operations require a unique command ID (admission).
    fn command(&mut self, operation: &str, payload: Value) -> Response {
        let command = self.next_command;
        self.next_command += 1;
        self.call(
            operation,
            payload,
            Some(format!("online-gc-process-{command}")),
        )
    }

    fn control_json(&mut self, operation: &str) -> Value {
        let response = self.call(operation, json!({}), None);
        assert_eq!(response.status, 0, "{operation} was rejected");
        serde_json::from_slice(&response.payload).unwrap()
    }
}

#[derive(Clone, Copy)]
struct OnlineGcMetrics {
    epochs_completed: u64,
    total_reclaimed_bytes: u64,
    total_temporary_files_removed: u64,
}

fn online_gc_metrics(client: &mut WireClient) -> OnlineGcMetrics {
    let metrics = client.control_json("system.metrics");
    let collector = &metrics["onlineGarbageCollector"];
    OnlineGcMetrics {
        epochs_completed: collector["epochs_completed"].as_u64().unwrap(),
        total_reclaimed_bytes: collector["total_reclaimed_bytes"].as_u64().unwrap(),
        total_temporary_files_removed: collector["total_temporary_files_removed"]
            .as_u64()
            .unwrap(),
    }
}

/// The `system.reclaim` payload is a fixed-shape contract; pin the exact
/// key set plus every field's documented value.
fn assert_reclaim_contract(payload: &Value) {
    let keys: BTreeSet<&str> = payload
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        BTreeSet::from([
            "reclaimed",
            "reclaimed_bytes",
            "backend",
            "more_candidates",
            "budget_ms",
            "offline_required",
            "reason_code",
            "statistics_complete",
            "epochs_completed",
            "epoch_in_progress",
        ])
    );
    assert!(payload["reclaimed"].is_u64());
    assert!(payload["reclaimed_bytes"].is_u64());
    assert_eq!(payload["backend"], json!("tofu-db"));
    assert!(payload["more_candidates"].is_boolean());
    assert!(payload["budget_ms"].is_u64());
    assert_eq!(payload["offline_required"], json!(false));
    assert_eq!(payload["reason_code"], Value::Null);
    assert!(payload["statistics_complete"].is_boolean());
    assert!(payload["epochs_completed"].is_u64());
    assert!(payload["epoch_in_progress"].is_boolean());
    // The RPC reports the collector's own snapshot: "more candidates" is
    // exactly "an epoch is still running".
    assert_eq!(payload["more_candidates"], payload["epoch_in_progress"]);
}

fn assert_online_gc_metric_fields(collector: &Value) {
    for field in [
        "epochs_completed",
        "marks_built",
        "deferred_epochs",
        "last_epoch_duration_ms",
        "last_epoch_reclaimed_bytes",
        "total_reclaimed_bytes",
        "total_reclaimed_blocks",
        "total_temporary_files_removed",
        "total_orphan_segments_removed",
        "total_segments_rewritten",
        "total_segments_retired",
        "total_compacted_blocks",
        "total_consolidations",
        "total_consolidated_victims",
    ] {
        assert!(
            collector[field].is_u64(),
            "onlineGarbageCollector.{field} must be a u64"
        );
    }
    assert!(collector["epoch_in_progress"].is_boolean());
}

/// Picks an existing block shard directory for the abandoned-temporary
/// fixture, creating a canonical empty shard when the store has none yet
/// (the shard scan enumerates the blocks root, so any two-hex directory
/// is a legal host).
fn block_shard(data_dir: &Path) -> PathBuf {
    let blocks = data_dir.join("blocks");
    let mut shards: Vec<PathBuf> = std::fs::read_dir(&blocks)
        .unwrap()
        .filter_map(|entry| {
            let entry = entry.ok()?;
            let is_shard = entry.file_type().ok()?.is_dir() && entry.file_name().len() == 2;
            is_shard.then(|| entry.path())
        })
        .collect();
    shards.sort();
    shards.into_iter().next().unwrap_or_else(|| {
        let shard = blocks.join("00");
        std::fs::create_dir(&shard).unwrap();
        shard
    })
}

fn upsert_large_document(client: &mut WireClient, index: u32) {
    // Unique content per commit: every version but the last overwrites the
    // same key, so each prior staged blob is graph-dead garbage.
    let blob = format!("{index:016x}").repeat(OVERWRITTEN_DOCUMENT_BYTES / 16);
    let response = client.command(
        "daily_cost.upsert",
        json!({
            "user_id": 11,
            "date": "2026-09-21",
            "cost": 1.5,
            "conversations": {"blob": blob},
            "computed_at": 1_000,
        }),
    );
    assert_eq!(
        response.status,
        0,
        "garbage-generating write {index} failed: {:?}",
        response.error_message
    );
}

fn assert_foreground_continuity(client: &mut WireClient, round: u32) {
    let write = client.command(
        "daily_cost.upsert",
        json!({
            "user_id": 11,
            "date": "2026-09-22",
            "cost": 0.5,
            "conversations": {"burst": format!("continuity-{round}")},
            "computed_at": 2_000,
        }),
    );
    assert_eq!(
        write.status,
        0,
        "foreground write failed while an online epoch was running: {:?}",
        write.error_message
    );
    let read = client.call("daily_cost.latest", json!({"user_id": 11}), None);
    assert_eq!(
        read.status,
        0,
        "foreground read failed while an online epoch was running: {:?}",
        read.error_message
    );
    assert!(
        String::from_utf8_lossy(&read.payload).contains("2026-09-22"),
        "foreground read returned stale state across an online epoch"
    );
}

#[test]
fn online_collector_reclaims_across_expedited_epochs_while_foreground_traffic_continues() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let daemon = spawn_daemon(directory.path());
    let mut client = WireClient::connect(daemon.port);

    // Health endpoint: the collector is configured and healthy from boot.
    let health = client.control_json("system.health");
    assert_eq!(health["contract"], json!("tofu-db.system-health.v1"));
    assert_eq!(health["backend"], json!("tofudb"));
    assert_eq!(health["ready"], json!(true));
    assert_eq!(
        health["components"]["onlineGarbageCollector"],
        json!({"configured": true, "healthy": true})
    );

    // Metrics endpoint: the collector section is present with the full
    // epoch/reclaim counter set from boot.
    let metrics = client.control_json("system.metrics");
    assert_eq!(metrics["contract"], json!("tofu-db.system-metrics.v1"));
    assert_online_gc_metric_fields(&metrics["onlineGarbageCollector"]);

    // A few hundred commits carrying large values, each overwriting the
    // same document: sustained foreground traffic plus graph-dead blobs
    // for the collector's mark/scan phases to wade through.
    for index in 0..OVERWRITE_COMMITS {
        upsert_large_document(&mut client, index);
    }

    // Stage the one garbage class that two-mark confirmation can reclaim
    // on a test timescale: an abandoned `.new-*` temporary block file, the
    // exact artifact a crashed block writer leaves behind. (Overwritten
    // blobs stay marked through retained history envelopes for many more
    // rotations than a test can afford, so they exercise scan load only.)
    let temporary = block_shard(directory.path()).join(format!(
        ".new-{}",
        uuid::Uuid::from_bytes([0x6c; 16]).simple()
    ));
    std::fs::write(&temporary, vec![0x2a_u8; ABANDONED_TEMPORARY_BYTES]).unwrap();

    // Drive expedited epochs: one reclaim rings the doorbell, the driver
    // starts an epoch within its poll interval, and the loop waits for the
    // epoch counter to advance before ringing again. The first sighting of
    // the temporary file only records it; a later epoch with the same
    // candidate confirms and deletes it (two-mark rule). Foreground reads
    // and writes keep flowing through every attempt.
    let started = Instant::now();
    let mut completed_epochs = online_gc_metrics(&mut client).epochs_completed;
    let mut confirmed = false;
    while !confirmed {
        assert!(
            started.elapsed() < EPOCH_DRIVE_BUDGET,
            "online collector never confirmed the abandoned temporary file"
        );
        let reclaim = client.command("system.reclaim", json!({}));
        assert_eq!(reclaim.status, 0);
        let payload: Value = serde_json::from_slice(&reclaim.payload).unwrap();
        assert_reclaim_contract(&payload);
        assert_eq!(payload["budget_ms"], json!(250));

        // The expedited epoch starts within the driver's poll interval;
        // this burst deliberately overlaps it.
        for round in 0..6 {
            assert_foreground_continuity(&mut client, round);
        }

        let attempt_deadline = Instant::now() + EPOCH_ATTEMPT_TIMEOUT;
        loop {
            let metrics = online_gc_metrics(&mut client);
            if metrics.epochs_completed > completed_epochs {
                completed_epochs = metrics.epochs_completed;
                confirmed = metrics.total_temporary_files_removed >= 1;
                break;
            }
            if Instant::now() >= attempt_deadline {
                // A contended snapshot hold defers the attempt; the next
                // iteration re-rings the doorbell.
                break;
            }
            std::thread::sleep(Duration::from_millis(150));
        }
    }

    assert!(
        !temporary.exists(),
        "confirmed garbage must be physically removed"
    );
    let metrics = online_gc_metrics(&mut client);
    assert!(metrics.epochs_completed >= 2);
    assert!(metrics.total_temporary_files_removed >= 1);
    assert!(metrics.total_reclaimed_bytes >= ABANDONED_TEMPORARY_BYTES as u64);

    // With at least one full epoch behind it, the reclaim RPC reports
    // complete statistics and still never asks for offline access.
    let reclaim = client.command("system.reclaim", json!({"budget_ms": 60_000}));
    assert_eq!(reclaim.status, 0);
    let payload: Value = serde_json::from_slice(&reclaim.payload).unwrap();
    assert_reclaim_contract(&payload);
    assert_eq!(payload["statistics_complete"], json!(true));
    assert_eq!(payload["budget_ms"], json!(60_000));

    // The health endpoint still reports a configured, healthy collector
    // after real reclaim work.
    let health = client.control_json("system.health");
    assert_eq!(health["ready"], json!(true));
    assert_eq!(
        health["components"]["onlineGarbageCollector"],
        json!({"configured": true, "healthy": true})
    );

    daemon.stop_gracefully();
}

#[test]
fn system_reclaim_validates_budget_bounds_before_expediting() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let daemon = spawn_daemon(directory.path());
    let mut client = WireClient::connect(daemon.port);

    for payload in [
        json!({"budget_ms": 5}),
        json!({"budget_ms": 60_001}),
        json!({"budget_ms": "fast"}),
        json!({"max_pages": 0}),
        json!({"min_free_pages": -1}),
    ] {
        let response = client.command("system.reclaim", payload.clone());
        assert_eq!(
            response.status,
            STATUS_BAD_REQUEST,
            "out-of-bounds reclaim payload was accepted: {payload}"
        );
    }

    for payload in [
        json!({"budget_ms": 10}),
        json!({"budget_ms": 60_000}),
        json!({}),
    ] {
        let response = client.command("system.reclaim", payload.clone());
        assert_eq!(
            response.status,
            0,
            "in-bounds reclaim payload was rejected: {payload}"
        );
        let document: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_reclaim_contract(&document);
    }

    daemon.stop_gracefully();
}
