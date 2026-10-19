use super::*;
fn request(key: &str) -> Request {
    Request {
        correlation_id: [1; 16],
        deadline_unix_ms: 100,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: None,
        schema_id: 1,
        operation: "record.get".into(),
        payload: key.as_bytes().to_vec(),
    }
}
fn snapshot(sequence: u64) -> EntitySnapshot {
    EntitySnapshot {
        sequence,
        root: None,
    }
}
fn put(cache: &QueryResultCache, request: &Request, sequence: u64) {
    for _ in 0..2 {
        cache.insert(request.into(), snapshot(sequence), 4, Admission::RepeatedKey, || {
            Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
        });
    }
}
#[test]
fn query_result_cache_exact_identity_generation_and_collision_proof() {
    let cache = QueryResultCache::new(65536);
    let request = request("one");
    put(&cache, &request, 1);
    assert!(cache.get((&request).into(), snapshot(1)).is_some());
    assert!(cache.get((&request).into(), snapshot(2)).is_none());
    for changed in 0..5 {
        let mut other = request.clone();
        match changed {
            0 => other.owner_id += 1,
            1 => other.tenant_id = None,
            2 => other.schema_id += 1,
            3 => other.operation = "record.list".into(),
            _ => other.payload = b"two".to_vec(),
        }
        assert!(cache.get((&other).into(), snapshot(1)).is_none());
    }
    let hash = Lookup::from(&request).hash_key();
    cache
        .state
        .lock()
        .unwrap()
        .entries
        .get_mut(&hash)
        .unwrap()
        .payload = b"forced collision".to_vec();
    assert!(cache.get((&request).into(), snapshot(1)).is_none());
}
#[test]
fn query_result_cache_is_bounded_lazy_and_held_bytes_survive_eviction() {
    let cache = QueryResultCache::new(16384);
    assert_eq!(cache.metrics()["accountedBytes"], 0);
    let request = request("first");
    put(&cache, &request, 1);
    let owned = cache.get((&request).into(), snapshot(1)).unwrap();
    for n in 0..1000 {
        put(&cache, &self::request(&format!("key-{n}")), 1);
    }
    assert!(cache.metrics()["accountedBytes"].as_u64().unwrap() <= 16384);
    assert!(cache.metrics()["evictions"].as_u64().unwrap() > 0);
    cache.configure(0);
    assert_eq!(cache.metrics()["accountedBytes"], 0);
    assert_eq!(cache.metrics()["entries"], 0);
    assert_eq!(owned.as_slice(), b"null");
    cache.insert((&request).into(), snapshot(1), 4, Admission::RepeatedKey, || {
        panic!("disabled cache must not copy bytes")
    });
    cache.configure(16384);
    cache.insert((&request).into(), snapshot(1), 2000, Admission::RepeatedKey, || {
        panic!("oversize cache entry must fall back before copying")
    });
    let mut unverified_copies = 0;
    for _ in 0..2 {
        cache.insert((&request).into(), snapshot(1), 4, Admission::RepeatedKey, || {
            unverified_copies += 1;
            Some(IrResponse::unverified(b"null".to_vec()))
        });
    }
    assert_eq!(
        unverified_copies, 1,
        "exercise the proof rejection after probation"
    );
    assert!(cache.get((&request).into(), snapshot(1)).is_none());
}
#[test]
fn query_result_cache_contention_and_poison_are_optional_misses() {
    let cache = Arc::new(QueryResultCache::new(65536));
    let request = request("key");
    put(&cache, &request, 1);
    let lock = cache.state.lock().unwrap();
    let worker_cache = cache.clone();
    let (done, result) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        let missed = worker_cache.get((&request).into(), snapshot(1)).is_none();
        worker_cache.insert((&request).into(), snapshot(1), 4, Admission::RepeatedKey, || {
            panic!("contended copy")
        });
        done.send(missed).unwrap();
    });
    let answer = result.recv_timeout(std::time::Duration::from_secs(2));
    drop(lock);
    worker.join().unwrap();
    assert_eq!(answer.unwrap(), true);
    let poisoned = cache.clone();
    assert!(std::thread::spawn(move || {
        let _guard = poisoned.state.lock().unwrap();
        panic!("cache-only poison");
    })
    .join()
    .is_err());
    assert!(cache
        .get((&self::request("key")).into(), snapshot(1))
        .is_none());
}

#[test]
fn query_result_cache_first_use_scans_defer_copy_and_preserve_repeated_hot_keys() {
    let cache = QueryResultCache::new(65536);
    let hot = request("hot");
    put(&cache, &hot, 1);
    let mut scan_copies = 0;
    for index in 0..4096 {
        let cold = request(&format!("cold-{index}"));
        cache.insert((&cold).into(), snapshot(1), 4, Admission::RepeatedKey, || {
            scan_copies += 1;
            Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
        });
        let metrics = cache.metrics();
        assert!(metrics["accountedBytes"].as_u64().unwrap() <= 65536);
        assert!(metrics["probationAccountedBytes"].as_u64().unwrap() <= 65536 / 64);
    }
    assert!(
        scan_copies < 32,
        "doorkeeper must defer nearly all of a one-pass scan"
    );
    assert!(cache.get((&hot).into(), snapshot(1)).is_some());
    let repeated = request("new-repeated");
    cache.insert((&repeated).into(), snapshot(1), 4, Admission::RepeatedKey, || {
        panic!("first observation copied")
    });
    assert!(cache.get((&repeated).into(), snapshot(1)).is_none());
    cache.insert((&repeated).into(), snapshot(1), 4, Admission::RepeatedKey, || {
        Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
    });
    assert!(cache.get((&repeated).into(), snapshot(1)).is_some());
    // Known-hot entries refresh after a write without restarting probation.
    cache.insert((&hot).into(), snapshot(2), 4, Admission::RepeatedKey, || {
        Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
    });
    assert!(cache.get((&hot).into(), snapshot(1)).is_none());
    assert!(cache.get((&hot).into(), snapshot(2)).is_some());
    for capacity in [8192, 16384, 1024, 65536, 0] {
        cache.configure(capacity);
        assert!(cache.metrics()["accountedBytes"].as_u64().unwrap() <= capacity as u64);
    }
    assert_eq!(cache.metrics()["probationObservations"], 0);
    assert_eq!(cache.metrics()["accountedBytes"], 0);
}

#[test]
fn query_result_cache_probation_covers_a_working_set_that_fits_the_result_budget() {
    let cache = QueryResultCache::new(512 * 1024);
    let requests: Vec<_> = (0..256)
        .map(|n| request(&format!("resident-{n}")))
        .collect();
    for _ in 0..2 {
        for request in &requests {
            cache.insert(request.into(), snapshot(1), 4, Admission::RepeatedKey, || {
                Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
            });
        }
    }
    for request in &requests {
        assert!(
            cache.get(request.into(), snapshot(1)).is_some(),
            "probation window starves a fitting repeated working set"
        );
    }
    assert!(cache.metrics()["accountedBytes"].as_u64().unwrap() <= 512 * 1024);
}

#[test]
fn query_result_cache_probation_has_exact_capacity_and_false_positives_never_answer() {
    for capacity in [16384usize, 29 * 4096, 57 * 4096, 113 * 4096, 128 * 4096] {
        let cache = QueryResultCache::new(capacity);
        for index in 0..4096 {
            let request = request(&format!("capacity-{index}"));
            cache.insert((&request).into(), snapshot(1), 4, Admission::RepeatedKey, || {
                Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
            });
            let metrics = cache.metrics();
            assert!(metrics["probationAccountedBytes"].as_u64().unwrap() <= (capacity / 64) as u64);
            assert!(metrics["accountedBytes"].as_u64().unwrap() <= capacity as u64);
        }
        cache.configure(0);
        assert_eq!(cache.metrics()["accountedBytes"], 0);
    }
    let cache = QueryResultCache::new(65536);
    let first = request("first");
    put(&cache, &first, 1);
    cache.state.lock().unwrap().probation.fill(u64::MAX);
    let other = request("false-positive");
    assert!(cache.get((&other).into(), snapshot(1)).is_none());
    cache.insert((&other).into(), snapshot(1), 4, Admission::RepeatedKey, || {
        Some(IrResponse::unverified(b"null".to_vec()))
    });
    assert!(
        cache.get((&other).into(), snapshot(1)).is_none(),
        "probation cannot mint a payload proof"
    );
    cache.insert((&other).into(), snapshot(1), 4, Admission::RepeatedKey, || {
        Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedKey))
    });
    assert!(cache.get((&other).into(), snapshot(1)).is_some());
    assert!(cache.get((&other).into(), snapshot(2)).is_none());
    assert!(cache
        .get((&request("another-key")).into(), snapshot(1))
        .is_none());
}

#[test]
fn query_result_cache_frame_bounds_apply_before_probation_copy_or_inline_hashing() {
    // The roomy budget would otherwise admit these inputs. Optional caching
    // still obeys ordinary operation/payload frame bounds before hashing.
    let cache = QueryResultCache::new(1024 * 1024 * 1024);
    let mut large_operation = request("key");
    large_operation.operation = "x".repeat(crate::protocol::MAX_OPERATION_BYTES + 1);
    let mut large_payload = request("key");
    large_payload.payload = vec![b' '; crate::protocol::MAX_PAYLOAD_BYTES + 1];
    for request in [&large_operation, &large_payload] {
        assert!(!Lookup::from(request).within_frame_bounds());
        for _ in 0..2 {
            cache.insert(request.into(), snapshot(1), 4, Admission::RepeatedKey, || {
                panic!("oversize key copied")
            });
        }
        assert!(cache.candidate(request.into()).is_none());
    }
    let request = request("response");
    for _ in 0..2 {
        cache.insert(
            (&request).into(),
            snapshot(1),
            crate::protocol::MAX_PAYLOAD_BYTES + 1,
            Admission::RepeatedKey,
            || panic!("oversize response copied"),
        );
    }
    assert_eq!(cache.metrics()["accountedBytes"], 0);
}

#[test]
fn query_result_cache_snapshot_admission_needs_repetition_after_each_publication() {
    let cache = QueryResultCache::new(65536);
    let request = request("snapshot");
    for sequence in 1..5 {
        cache.insert((&request).into(), snapshot(sequence), 4, Admission::RepeatedSnapshot, || {
            panic!("an older warm result must not trigger a copy in the new snapshot")
        });
        assert!(cache.get((&request).into(), snapshot(sequence)).is_none());
        cache.insert((&request).into(), snapshot(sequence), 4, Admission::RepeatedSnapshot, || {
            Some(IrResponse::from_snapshot_cache(b"null".to_vec(), Admission::RepeatedSnapshot))
        });
        assert!(cache.get((&request).into(), snapshot(sequence)).is_some());
        assert!(cache.get((&request).into(), snapshot(sequence + 1)).is_none());
    }
    assert_eq!(cache.metrics()["admissionDeferrals"], 4);
    assert_eq!(cache.metrics()["entries"], 1);
}
