//! Compression wire parity, independent leases, resizing, poison and disposal.
use super::*;
use crate::resource_probe::LaunchResourceSnapshot;
use std::sync::mpsc;
use std::time::Duration;

fn configure(encoder: &ChunkEncoder, cpus: usize, bytes: u64) {
    encoder
        .configure(LaunchResourceSnapshot {
            logical_cpus: Some(cpus),
            memory_headroom_bytes: Some(bytes),
            ..Default::default()
        })
        .unwrap();
}

#[test]
fn an_entered_context_cannot_serialize_an_independent_compression_lease() {
    let encoder = ChunkEncoder::default();
    configure(&encoder, 2, 1024 * 1024 * 1024);
    let first = encoder.lease().unwrap();
    std::thread::scope(|scope| {
        let (send, receive) = mpsc::channel();
        let (release, wait) = mpsc::channel();
        let encoder = &encoder;
        scope.spawn(move || {
            let second = encoder.lease().unwrap();
            send.send(()).unwrap();
            wait.recv().unwrap();
            drop(second);
        });
        receive.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(encoder.metrics()["activeContexts"], 2);
        assert_eq!(encoder.metrics()["peakActiveContexts"], 2);
        release.send(()).unwrap();
    });
    drop(first);
    assert_eq!(encoder.metrics()["idleContexts"], 2);
    assert_eq!(encoder.metrics()["activeContexts"], 0);
}

#[test]
fn concurrent_reuse_keeps_exact_legacy_frame_bytes_across_sizes_and_content() {
    let encoder = ChunkEncoder::default();
    configure(&encoder, 8, 1024 * 1024 * 1024);
    std::thread::scope(|scope| {
        for worker in 0..8_u64 {
            let encoder = &encoder;
            scope.spawn(move || {
                for size in [1, 8192, 65_536, crate::blob::BLOB_CHUNK_BYTES, 8192] {
                    let mut random = vec![0; size];
                    let mut hash = blake3::Hasher::new();
                    hash.update(&worker.to_le_bytes());
                    hash.update(&(size as u64).to_le_bytes());
                    hash.finalize_xof().fill(&mut random);
                    for raw in [random, vec![b'x'; size]] {
                        assert_eq!(
                            encoder.encode(&raw).unwrap(),
                            crate::blob::encode_chunk(&raw).unwrap()
                        );
                    }
                }
            });
        }
    });
    let metrics = encoder.metrics();
    assert_eq!(metrics["activeContexts"], 0);
    assert!(
        metrics["idleContextBytes"].as_u64().unwrap()
            <= metrics["idleByteBudget"].as_u64().unwrap()
    );
    assert!(metrics["reusedContexts"].as_u64().unwrap() > 0);
}

#[test]
fn idle_reclamation_never_rejects_an_active_or_future_legal_frame() {
    let encoder = ChunkEncoder::default();
    configure(&encoder, 2, 1024 * 1024 * 1024);
    let mut active = encoder.lease().unwrap();
    active
        .codec
        .as_mut()
        .unwrap()
        .compress(&vec![b'x'; 65_536])
        .unwrap();
    configure(&encoder, 2, 0);
    drop(active);
    assert_eq!(encoder.metrics()["idleContexts"], 0);
    assert_eq!(encoder.metrics()["idleContextBytes"], 0);
    let raw = vec![b'x'; 65_536];
    assert_eq!(
        encoder.encode(&raw).unwrap(),
        crate::blob::encode_chunk(&raw).unwrap()
    );
    assert_eq!(encoder.metrics()["idleContexts"], 0);
    configure(&encoder, 32, 128 * 1024 * 1024 * 1024);
    assert_eq!(encoder.metrics()["idleContextLimit"], 32);
    assert_eq!(encoder.metrics()["idleByteBudget"], 1024 * 1024 * 1024_u64);
}

#[test]
fn malformed_chunks_do_not_acquire_or_allocate_a_codec() {
    let encoder = ChunkEncoder::default();
    for raw in [Vec::new(), vec![0; crate::blob::BLOB_CHUNK_BYTES + 1]] {
        assert_eq!(
            encoder.encode(&raw).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }
    assert_eq!(encoder.metrics()["createdContexts"], 0);
    assert_eq!(encoder.metrics()["activeContexts"], 0);
}

#[test]
fn unwind_preserves_fail_closed_codec_poison_and_releases_the_lease() {
    let encoder = ChunkEncoder::default();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _lease = encoder.lease().unwrap();
        panic!("injected compression panic");
    }));
    assert!(result.is_err());
    assert_eq!(encoder.metrics()["activeContexts"], 0);
    assert_eq!(encoder.metrics()["poisoned"], true);
    assert_eq!(
        encoder.encode(b"valid chunk").unwrap_err().to_string(),
        "blob codec is poisoned"
    );
}

#[test]
fn the_last_authority_or_observer_owns_context_disposal_without_tls_retention() {
    let encoder = Arc::new(ChunkEncoder::default());
    encoder.encode(&vec![b'x'; 8192]).unwrap();
    let weak = Arc::downgrade(&encoder);
    let observer = encoder.observer();
    drop(encoder);
    assert_eq!(observer.metrics()["idleContexts"], 1);
    drop(observer);
    assert!(weak.upgrade().is_none());
}
