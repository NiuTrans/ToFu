//! In-process micro-benchmark for the `turn.append_settled` hot path.
//!
//! Drives the production Transaction IR (`TransactionIrStep::TurnAppendSettled`
//! with command effects, exactly what the semantic executor emits for the
//! storage.v2 `turn.append_settled` command) against a real on-disk
//! `AuthorityDatabase`, then prints per-window latency percentiles. This
//! removes the loopback socket from the measurement so regressions in entity
//! reads/writes, encoding, index maintenance, and commit IO are attributable
//! to the storage engine itself.
//!
//! Run:
//!   cargo run --release --example turn_append_bench -- [iterations]
//!
//! The script `scripts/tofudb_perf_compare.py --workloads turn_append_burst`
//! remains the end-to-end reproduction through the production client stack.

use std::time::Instant;

use tofu_db::authority::AuthorityDatabase;
use tofu_db::generated_storage_operations::StorageOperationKind;
use tofu_db::transaction_ir::{
    execute_transaction_ir, TransactionIr, TransactionIrCommandEffects, TransactionIrResult,
    TransactionIrStep,
};

#[cfg(not(feature = "embedded-mimalloc"))]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn percentile(sorted: &[u64], fraction: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let index = ((sorted.len() - 1) as f64 * fraction).round() as usize;
    sorted[index.min(sorted.len() - 1)]
}

fn window_report(label: &str, samples_us: &[u64]) {
    let mut ordered = samples_us.to_vec();
    ordered.sort_unstable();
    let total: u64 = ordered.iter().sum();
    println!(
        "{label}: n={} p50={}us p95={}us max={}us mean={:.1}us",
        ordered.len(),
        percentile(&ordered, 0.50),
        percentile(&ordered, 0.95),
        ordered.last().copied().unwrap_or(0),
        total as f64 / ordered.len().max(1) as f64,
    );
}

fn main() -> std::io::Result<()> {
    let iterations: usize = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "400".into())
        .parse()
        .ok()
        .filter(|n| (1..=10_000).contains(n))
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "benchmark iterations must be an integer in 1..=10000",
            )
        })?;
    let directory = std::env::var("BENCH_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| {
            std::env::temp_dir().join(format!(
                "tofu-db-turn-append-bench-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis())
                    .unwrap_or(0)
            ))
        });
    let mut database = AuthorityDatabase::initialize(&directory)?;
    // Match the daemon's default packing lifecycle. The explicit diagnostic
    // override retains the synchronous fallback for comparison and faults.
    let packer = if std::env::var_os("TOFU_DB_DISABLE_BACKGROUND_PACK").is_some() {
        None
    } else {
        Some(database.start_background_packer()?)
    };

    let base_ms: u64 = 1_800_000_000_000;
    // Seed the conversation through the production create step so the append
    // path runs with allow_create=false exactly like the benchmark harness.
    let create = TransactionIr {
        tenant_id: 7,
        owner_user_id: 11,
        operation: "conversation.create".to_owned(),
        operation_kind: StorageOperationKind::Command,
        steps: vec![TransactionIrStep::ConversationCreate {
            conversation_id: "bench_conversation".to_owned(),
            title: "Turn append bench".to_owned(),
            settings_json: b"{}".to_vec(),
            created_at_ms: base_ms,
            updated_at_ms: base_ms,
            committed_at_ms: base_ms,
        }],
        result: TransactionIrResult::Literal(b"null".to_vec()),
        command_effects: Some(TransactionIrCommandEffects {
                alternate_request_digest: None,
            legacy_receipt_owner_bound: false,
            command_id: "bench-create".to_owned(),
            request_digest: [3; 32],
            request_id: "bench-request-create".to_owned(),
            schema_version: 1,
            registry_version: 1,
            committed_at_ms: base_ms,
            outbox_payload: Vec::new(),
            store_receipt: true,
        }),
    };
    execute_transaction_ir(&mut database, &create)?;

    let projection = serde_json::json!({
        "content": format!("agent round {}", "lorem ipsum dolor sit amet ".repeat(8)),
    });
    let projection_json = serde_json::to_vec(&projection)?;
    let settlement_json =
        br#"{"outcome":"completed","cause":"ingested","resumeOptions":[]}"#.to_vec();

    let rotate: usize = std::env::var("BENCH_ROTATE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let tracked_stages = [
        tofu_db::fsync_stats::STAGE_SEMANTIC,
        tofu_db::fsync_stats::STAGE_PREPARE,
        tofu_db::fsync_stats::STAGE_APPLY_BATCH,
        tofu_db::fsync_stats::STAGE_ENGINE_COMMIT,
        tofu_db::fsync_stats::STAGE_CARRIED,
        tofu_db::fsync_stats::STAGE_ENCODE,
        tofu_db::fsync_stats::STAGE_WAL,
        tofu_db::fsync_stats::STAGE_APPLY,
        tofu_db::fsync_stats::STAGE_IR_STEPS,
        tofu_db::fsync_stats::STAGE_IR_COMMIT,
        tofu_db::fsync_stats::STAGE_LOAD_NODE,
        tofu_db::fsync_stats::STAGE_STORE_PAGE,
        tofu_db::fsync_stats::STAGE_PUT_DEFERRED,
        tofu_db::fsync_stats::STAGE_BLOCK_GET,
        tofu_db::fsync_stats::STAGE_BUILDER,
        tofu_db::fsync_stats::STAGE_CHECKPOINT_IF,
        tofu_db::fsync_stats::STAGE_COMMIT_TAIL,
        tofu_db::fsync_stats::STAGE_GET_PENDING,
        tofu_db::fsync_stats::STAGE_GET_CACHE,
        tofu_db::fsync_stats::STAGE_GET_UNCACHED_OK,
        tofu_db::fsync_stats::STAGE_GET_UNCACHED_MISS,
        tofu_db::fsync_stats::STAGE_PUT_STAGE,
    ];
    let windows = 10;
    let window_size = iterations.div_ceil(windows);
    let mut stage_snapshots: Vec<[u64; 22]> = Vec::new();
    let mut call_snapshots: Vec<[u64; 22]> = Vec::new();
    let mut samples = Vec::with_capacity(iterations);
    let mut slowest: Vec<(u64, usize, [u64; tofu_db::fsync_stats::STAGE_NAMES.len()])> =
        Vec::with_capacity(8);
    for i in 0..iterations {
        let at = base_ms + 60_000 + i as u64;
        let conversation_id = if let Some(slot) = i.checked_div(rotate) {
            let id = format!("bench_conversation_{slot}");
            let create = TransactionIr {
                tenant_id: 7,
                owner_user_id: 11,
                operation: "conversation.create".to_owned(),
                operation_kind: StorageOperationKind::Command,
                steps: vec![TransactionIrStep::ConversationCreate {
                    conversation_id: id.clone(),
                    title: "t".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: base_ms,
                    updated_at_ms: base_ms,
                    committed_at_ms: base_ms,
                }],
                result: TransactionIrResult::Literal(b"null".to_vec()),
                command_effects: Some(TransactionIrCommandEffects {
                alternate_request_digest: None,
                    legacy_receipt_owner_bound: false,
                    command_id: format!("bench-create-{slot}"),
                    request_digest: [3; 32],
                    request_id: format!("bench-request-create-{slot}"),
                    schema_version: 1,
                    registry_version: 1,
                    committed_at_ms: base_ms,
                    outbox_payload: Vec::new(),
                    store_receipt: true,
                }),
            };
            if i % rotate == 0 {
                execute_transaction_ir(&mut database, &create)?;
            }
            id
        } else {
            "bench_conversation".to_owned()
        };
        let ir = TransactionIr {
            tenant_id: 7,
            owner_user_id: 11,
            operation: "turn.append_settled".to_owned(),
            operation_kind: StorageOperationKind::Command,
            steps: vec![TransactionIrStep::TurnAppendSettled {
                conversation_id,
                actor: "human".to_owned(),
                status: "completed".to_owned(),
                projection_json: projection_json.clone(),
                projection_timestamp: None,
                settlement_json: settlement_json.clone(),
                lane_id: "main".to_owned(),
                command_id: format!("bench_turn_{i}_command"),
                kind: "ingested".to_owned(),
                run_id: String::new(),
                turn_id: format!("bench_turn_{i:06}"),
                attempt_id: None,
                created_at_ms: at,
                committed_at_ms: at,
                allow_create: false,
                default_title: "Turn append bench".to_owned(),
                default_settings_json: b"{}".to_vec(),
                default_created_at_ms: base_ms,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(TransactionIrCommandEffects {
                alternate_request_digest: None,
                legacy_receipt_owner_bound: false,
                command_id: format!("bench-{i}"),
                request_digest: [7; 32],
                request_id: format!("bench-request-{i}"),
                schema_version: 1,
                registry_version: 1,
                committed_at_ms: at,
                outbox_payload: Vec::new(),
                store_receipt: true,
            }),
        };
        let before: [_; tofu_db::fsync_stats::STAGE_NAMES.len()] =
            std::array::from_fn(tofu_db::fsync_stats::stage_nanos);
        let started = Instant::now();
        execute_transaction_ir(&mut database, &ir)?;
        let elapsed = started.elapsed().as_micros() as u64;
        samples.push(elapsed);
        if slowest.len() < 8 || elapsed > slowest.last().unwrap().0 {
            let deltas: [_; tofu_db::fsync_stats::STAGE_NAMES.len()] =
                std::array::from_fn(|stage| {
                    tofu_db::fsync_stats::stage_nanos(stage).saturating_sub(before[stage])
                });
            slowest.push((elapsed, i, deltas));
            slowest.sort_unstable_by_key(|sample| std::cmp::Reverse(sample.0));
            slowest.truncate(8);
        }
        if (i + 1) % window_size == 0 {
            stage_snapshots.push(std::array::from_fn(|slot| {
                tofu_db::fsync_stats::stage_nanos(tracked_stages[slot])
            }));
            call_snapshots.push(std::array::from_fn(|slot| {
                tofu_db::fsync_stats::stage_calls(tracked_stages[slot])
            }));
        }
    }

    window_report("total", &samples);
    let stage_names = [
        "semantic",
        "prepare",
        "apply_batch",
        "engine_commit",
        "carried",
        "encode",
        "wal",
        "apply",
        "ir_steps",
        "ir_commit",
        "load_node",
        "store_page",
        "put_deferred",
        "block_get",
        "builder",
        "checkpoint_if",
        "commit_tail",
        "get_pending",
        "get_cache",
        "get_unc_ok",
        "get_unc_miss",
        "put_stage",
    ];
    let mut previous = [0_u64; 22];
    let mut previous_calls = [0_u64; 22];
    for (index, chunk) in samples.chunks(window_size).enumerate() {
        window_report(
            &format!("window{index} (start {})", index * window_size),
            chunk,
        );
        let snapshot = stage_snapshots.get(index).copied().unwrap_or(previous);
        let calls_snapshot = call_snapshots.get(index).copied().unwrap_or(previous_calls);
        let mut line = format!("  stages@window{index}:");
        for slot in 0..22 {
            let delta = snapshot[slot] - previous[slot];
            let call_delta = calls_snapshot[slot] - previous_calls[slot];
            previous[slot] = snapshot[slot];
            previous_calls[slot] = calls_snapshot[slot];
            let per_commit = call_delta as f64 / chunk.len().max(1) as f64;
            line.push_str(&format!(
                " {}={:.1}us(x{:.0})",
                stage_names[slot],
                delta as f64 / 1e3 / chunk.len().max(1) as f64,
                per_commit
            ));
        }
        println!("{line}");
    }
    println!("{}", tofu_db::fsync_stats::stage_report());
    println!("fsync_stats: {}", tofu_db::fsync_stats::report());
    for (elapsed, iteration, deltas) in slowest {
        let mut line = format!("slow@{iteration}: total={elapsed}us");
        for (stage, nanos) in deltas.iter().enumerate() {
            if *nanos >= 10_000 {
                line.push_str(&format!(
                    " {}={:.1}us",
                    tofu_db::fsync_stats::STAGE_NAMES[stage],
                    *nanos as f64 / 1e3
                ));
            }
        }
        println!("{line}");
    }
    if let Some(packer) = packer {
        packer.stop_and_join()?;
    }
    drop(database);
    if std::env::var("BENCH_KEEP").is_err() {
        std::fs::remove_dir_all(&directory).ok();
    } else {
        println!("kept: {}", directory.display());
    }
    Ok(())
}
