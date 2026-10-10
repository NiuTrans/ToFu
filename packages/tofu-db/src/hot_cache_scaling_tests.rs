//! Opt-in paired measurement of the launch-sized decoded-chunk working set.
//! Entry: hot_cache_scaling_measurement (ignored in ordinary tests).
//! Dependencies: production resource budgets and verified chunk decoder/cache.
//! Measures cache reuse only; source I/O, JSON, RPC and whole-task time are excluded.

use crate::blob::{decode_cache::DecodedChunkCache, encode_chunk, ChunkBytes};
use crate::block::BlockId;
use crate::resource_probe::{DaemonResourceBudget, LaunchResourceSnapshot};
use std::sync::Arc;
use std::time::Instant;

#[test]
#[ignore = "paired cache measurement allocates approximately 320 MiB"]
fn hot_cache_scaling_measurement() {
    const MIB: usize = 1024 * 1024;
    const CHUNKS: usize = 300;
    const PASSES: usize = 3;
    let budget = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
        logical_cpus: Some(64),
        memory_capacity_bytes: Some(256 * 1024 * MIB as u64),
        memory_headroom_bytes: Some(128 * 1024 * MIB as u64),
        volume_free_bytes: Some(1024 * 1024 * MIB as u64),
    });
    let old_capacity = budget.block_cache_bytes.min(2048 * MIB)
        / crate::blob::decode_cache::HOT_CACHE_BUDGET_DIVISOR;
    let scaled_capacity = budget.block_cache_bytes
        / crate::blob::decode_cache::HOT_CACHE_BUDGET_DIVISOR;
    assert!(scaled_capacity > CHUNKS * MIB);
    let mut raw = vec![b'x'; MIB];
    let chunks: Vec<_> = (0..CHUNKS).map(|index| {
        raw[..8].copy_from_slice(&(index as u64).to_le_bytes());
        let encoded = Arc::new(encode_chunk(&raw).unwrap());
        (BlockId::for_payload(&encoded), encoded)
    }).collect();
    let mut measurements = Vec::new();
    for sample in 0..3 {
        for scaled in if sample % 2 == 0 { [false, true] } else { [true, false] } {
            let cache = Arc::new(DecodedChunkCache::default());
            let capacity = if scaled { scaled_capacity } else { old_capacity };
            cache.configure_capacity(capacity);
            let observer = cache.observer();
            // Warm the same full corpus before timing, including its evictions.
            for (id, encoded) in &chunks {
                cache.decode(*id, encoded, MIB as u32).unwrap();
            }
            let before = observer.metrics();
            let started = Instant::now();
            let mut output = blake3::Hasher::new();
            for _ in 0..PASSES {
                for (index, (id, encoded)) in chunks.iter().enumerate() {
                    let decoded = cache.decode(*id, encoded, MIB as u32).unwrap();
                    let bytes = match &decoded {
                        ChunkBytes::Owned(bytes) => bytes.as_slice(),
                        ChunkBytes::Shared(bytes) => bytes.as_slice(),
                    };
                    assert_eq!(bytes.len(), MIB);
                    assert_eq!(&bytes[..8], &(index as u64).to_le_bytes());
                    assert_eq!(bytes[MIB - 1], b'x');
                    output.update(&bytes[..8]);
                }
            }
            let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
            let after = observer.metrics();
            let hits = after.hits - before.hits;
            let misses = after.misses - before.misses;
            assert!(after.accounted_bytes <= capacity);
            if scaled {
                assert_eq!(hits, (CHUNKS * PASSES) as u64);
                assert_eq!(misses, 0);
            } else {
                assert_eq!(hits, 0);
                assert_eq!(misses, (CHUNKS * PASSES) as u64);
            }
            measurements.push(serde_json::json!({
                "sample": sample, "scaled": scaled, "capacityBytes": capacity,
                "elapsedMs": elapsed_ms, "hits": hits, "misses": misses,
                "accountedBytes": after.accounted_bytes,
                "outputDigest": output.finalize().to_hex().to_string(),
            }));
            cache.configure_capacity(0);
            assert_eq!(observer.metrics().accounted_bytes, 0);
        }
    }
    assert!(measurements.iter().all(|row| row["outputDigest"] == measurements[0]["outputDigest"]));
    println!("HOT_CACHE_SCALING {}", serde_json::json!({
        "contract": "tofu.hot-cache-scaling-measure/v1", "chunks": CHUNKS,
        "chunkBytes": MIB, "passes": PASSES, "samples": measurements,
        "scope": "verified decode reuse over identical current encoded allocations",
        "excludes": ["source I/O", "JSON", "RPC", "whole task latency"],
    }));
}
