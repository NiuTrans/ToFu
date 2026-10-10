//! Metadata-only owned callback overhead against the borrowed Journal API.
//! Memory fixture excludes storage I/O; no production latency claim.
use std::{sync::Arc, time::Instant};
use tofu_runtime::checkpoint_journal::{Journal, OwnedLease, OwnedPolicy, Scope};
#[path = "../tests/support/checkpoint_store.rs"]
mod checkpoint_store;
use checkpoint_store::MemoryStore;
fn scope() -> Scope {
    Scope {
        owner: 7,
        task_id: "task".into(),
        epoch: "epoch".into(),
        binding: "model-tools-account".into(),
    }
}
fn sample(owned: bool, iterations: usize) -> f64 {
    let store = Arc::new(MemoryStore::default());
    let start = Instant::now();
    if owned {
        for _ in 0..iterations {
            let lease =
                OwnedLease::acquire(scope(), store.clone(), 1048576, 1024, "owner").unwrap();
            assert!(lease.load().unwrap().is_none());
            lease.release().unwrap();
        }
    } else {
        let journal = Journal::new(scope(), store.as_ref(), 1048576, 1024).unwrap();
        for _ in 0..iterations {
            let lease = journal.acquire("owner").unwrap();
            assert!(lease.load().unwrap().is_none());
            lease.release().unwrap();
        }
    }
    assert_eq!(*store.snapshot_reads.lock().unwrap(), 0);
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 0);
    start.elapsed().as_nanos() as f64 / iterations as f64
}
fn main() {
    let iterations = std::env::args()
        .nth(1)
        .map(|s| s.parse::<usize>().unwrap())
        .unwrap_or(10000);
    assert!(iterations > 0);
    sample(false, 100);
    sample(true, 100);
    let mut borrowed = Vec::new();
    let mut owned = Vec::new();
    // Alternate order to reduce first-run/host-drift bias.
    for index in 0..6 {
        if index % 2 == 0 {
            borrowed.push(sample(false, iterations));
            owned.push(sample(true, iterations));
        } else {
            owned.push(sample(true, iterations));
            borrowed.push(sample(false, iterations));
        }
    }
    println!(
        "{}",
        serde_json::json!({
            "format":"tofu.owned-checkpoint-cost/v1", "iterations_per_sample":iterations,
            "borrowed_acquire_load_release_ns":borrowed, "owned_acquire_load_release_ns":owned,
            "owned_lease_inline_bytes":std::mem::size_of::<OwnedLease>(),
            "owned_policy_inline_bytes":std::mem::size_of::<OwnedPolicy>(),
            "scope_characters":scope().task_id.len()+scope().epoch.len()+scope().binding.len(),
            "snapshot_reads":0,"snapshot_writes":0,"storage":"atomic-memory-fixture"
        })
    );
}
