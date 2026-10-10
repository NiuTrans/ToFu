//! In-process micro-benchmark for the `conversation.create` hot path.
//!
//! Drives the production Transaction IR (`TransactionIrStep::ConversationCreate`
//! with command effects, exactly what the semantic executor emits for the
//! storage.v2 `conversation.create` command) against a real on-disk
//! `AuthorityDatabase`, then prints per-window latency percentiles.
//!
//! Run:
//!   cargo run --release --example conversation_create_bench -- [iterations]

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
        .and_then(|value| value.parse().ok())
        .unwrap_or(400);
    let directory = std::env::temp_dir().join(format!(
        "tofu-db-create-bench-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    ));
    let mut database = AuthorityDatabase::initialize(&directory)?;

    let base_ms: u64 = 1_800_000_000_000;
    let settings = serde_json::json!({"model": "bench-model", "agent": "bench"});
    let settings_json = serde_json::to_vec(&settings)?;

    let mut samples = Vec::with_capacity(iterations);
    for i in 0..iterations {
        let at = base_ms + i as u64;
        let ir = TransactionIr {
            tenant_id: 7,
            owner_user_id: 11,
            operation: "conversation.create".to_owned(),
            operation_kind: StorageOperationKind::Command,
            steps: vec![TransactionIrStep::ConversationCreate {
                conversation_id: format!("bench_create_{i:06}"),
                title: "Create bench conversation".to_owned(),
                settings_json: settings_json.clone(),
                created_at_ms: at,
                updated_at_ms: at,
                committed_at_ms: at,
            }],
            result: TransactionIrResult::Literal(b"null".to_vec()),
            command_effects: Some(TransactionIrCommandEffects {
                alternate_request_digest: None,
                legacy_receipt_owner_bound: false,
                command_id: format!("bench-create-{i}"),
                request_digest: [3; 32],
                request_id: format!("bench-request-create-{i}"),
                schema_version: 1,
                registry_version: 1,
                committed_at_ms: at,
                outbox_payload: Vec::new(),
                store_receipt: true,
            }),
        };
        let started = Instant::now();
        execute_transaction_ir(&mut database, &ir)?;
        samples.push(started.elapsed().as_micros() as u64);
    }

    window_report("total", &samples);
    let windows = 10;
    let window_size = samples.len().div_ceil(windows);
    for (index, chunk) in samples.chunks(window_size).enumerate() {
        window_report(
            &format!("window{index} (start {})", index * window_size),
            chunk,
        );
    }
    println!("fsync_stats: {}", tofu_db::fsync_stats::report());
    std::fs::remove_dir_all(&directory).ok();
    Ok(())
}
