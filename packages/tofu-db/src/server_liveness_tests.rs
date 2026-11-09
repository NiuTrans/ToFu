// Included into `server::tests`: one stalled physical WAL write must hold only
// the responses that depend on it, never the authority every client shares.

fn respond(stores: &StorageV2Stores<'_>, session: &StorageV2Session, request: &Request) -> Value {
    let RequestDispatch::Responded(response) = dispatch_one_request(
        stores, session, request, false, 1_000, &mut || 1_000, &mut || true).unwrap()
        else { panic!("{} disconnected", request.operation) };
    assert_eq!(response.status, 0, "{} {:?}", request.operation, response.error_message);
    serde_json::from_slice(&response.payload).unwrap()
}

fn liveness_job(task_id: &str) -> Request {
    retention_command("worker_job.enqueue", json!({
        "task_id": task_id, "user_id": 11, "tenant_id": "", "task_kind": "liveness",
        "idempotency_key": task_id, "payload": {}, "priority": 0, "available_at_ms": 0, "now_ms": 1_000,
    }), &format!("enqueue-{task_id}"))
}

/// Normal durability (every network volume in production): the workspace-meta
/// bulk write's frame reaches a volume that stalls. A lease renewal and a claim
/// still commit and a read still answers during the stall, because the stalled
/// write belongs to the WAL worker, not to an authority holder; once the volume
/// accepts the write, one more batch acknowledges both queued commits.
#[test]
fn stalled_wal_write_never_holds_the_authority_against_renewals_claims_or_reads() {
    let directory = tempfile::tempdir().unwrap();
    let vfs = Arc::new(crate::test_wal_gate_vfs::WalGateVfs::new(Arc::new(crate::vfs::RealVfs)));
    let mut seeded = AuthorityDatabase::initialize_with_vfs(directory.path(), vfs.clone()).unwrap();
    seeded.set_wal_durability(crate::wal::WalDurability::Normal).unwrap();
    let (barrier, driver) = crate::wal::WalBarrierDriver::start_buffered(
        seeded.wal_vfs().unwrap(), seeded.wal_path().unwrap(),
        crate::wal::WalDurability::Normal, Duration::from_millis(10), 1 << 20).unwrap();
    seeded.attach_wal_barrier(&barrier).unwrap();
    let session = authenticator().authenticate(&hello()).unwrap();
    dispatched(&mut seeded, &session, &retention_command("integration.workspace.register", json!({
        "user_id": 11, "project_root": "/project", "task_id": "workspace", "title": "workspace",
        "workspace_path": "/workspace", "managed": false, "base_sha": "a".repeat(40), "now": 1.0,
    }), "register"), 1_000);
    dispatched(&mut seeded, &session, &retention_command("record.put", json!({
        "namespace": "liveness", "key": "read", "value": {"answer": 42},
    }), "put-read"), 1_000);
    dispatched(&mut seeded, &session, &liveness_job("renewed"), 1_000);
    let held = dispatched(&mut seeded, &session, &retention_command("worker_job.claim_next", json!({
        "worker_id": "holder", "task_kinds": ["liveness"], "now_ms": 1_000, "lease_ms": 60_000,
    }), "claim-renewed"), 1_000);
    assert_eq!(held["taskId"], "renewed");
    dispatched(&mut seeded, &session, &liveness_job("queued"), 1_000);
    let view = seeded.enable_published_queries().unwrap();
    barrier.wait_synced(seeded.wal_len().unwrap(), Instant::now() + Duration::from_secs(10)).unwrap();
    let database = Mutex::new(seeded);
    let stores = StorageV2Stores::new(&database, None)
        .with_wal_barrier(barrier.clone())
        .with_published_queries(view);

    let bulk = retention_command("integration.workspace.set_meta", json!({
        "user_id": 11, "project_root": "/project", "task_id": "workspace",
        "patch_json": json!({"bulk": "x".repeat(3_000)}).to_string(), "now": 1.0,
    }), "bulk-meta");
    let renewal = retention_command("worker_job.heartbeat", json!({
        "task_id": "renewed", "worker_id": "holder", "fencing_token": held["fencingToken"],
        "now_ms": 1_000, "lease_ms": 60_000, "replay_cursor": 7,
    }), "renewal");
    let claim = retention_command("worker_job.claim_next", json!({
        "worker_id": "claimer", "task_kinds": ["liveness"], "now_ms": 1_000, "lease_ms": 60_000,
    }), "claim-queued");
    let read = request("record.get", 11, json!({"namespace": "liveness", "key": "read"}));
    let job = |task_id: &str| request("worker_job.get", 11, json!({"task_id": task_id, "user_id": 11}));
    let batches = || barrier.write_buffer_metrics()["writtenBatches"].as_u64().unwrap();
    let batches_before = batches();

    let (stalled, release) = vfs.block_next_wal_write();
    let observed = thread::scope(|scope| {
        let bulk = scope.spawn(|| respond(&stores, &session, &bulk));
        stalled.recv_timeout(Duration::from_secs(10)).expect("the bulk frame reaches the volume");
        // The worker can reach the volume before the bulk commit releases
        // the mutex; only a stall that keeps it held is a failure.
        let authority_free = {
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                if database.try_lock().is_ok() { break true; }
                if Instant::now() >= deadline { break false; }
                thread::sleep(Duration::from_millis(1));
            }
        };
        let renewal = scope.spawn(|| respond(&stores, &session, &renewal));
        let claim = scope.spawn(|| respond(&stores, &session, &claim));
        let read_value = respond(&stores, &session, &read)["value"]["answer"].clone();
        // Both commits become visible to published reads while the volume
        // still stalls the bulk write; nothing waits for its holder.
        let committed_during_stall = {
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                let renewed = respond(&stores, &session, &job("renewed"))["replayCursor"] == json!(7);
                let claimed = respond(&stores, &session, &job("queued"))["claimOwner"] == json!("claimer");
                if renewed && claimed { break true; }
                if Instant::now() >= deadline { break false; }
                thread::sleep(Duration::from_millis(5));
            }
        };
        let still_stalled = !bulk.is_finished();
        release.send(()).unwrap();
        let bulk = bulk.join().unwrap();
        let renewal = renewal.join().unwrap();
        let claim = claim.join().unwrap();
        (authority_free, read_value, committed_during_stall, still_stalled, bulk, renewal, claim)
    });
    let (authority_free, read_value, committed_during_stall, still_stalled, bulk, renewal, claim) = observed;
    assert!(authority_free, "a stalled physical WAL write must not hold the authority mutex");
    assert_eq!(read_value, 42, "reads answer while the volume stalls");
    assert!(committed_during_stall, "the renewal and the claim commit while the volume stalls");
    assert!(still_stalled, "the bulk response waits for its own write");
    assert_eq!(bulk["origin"]["bulk"].as_str().map(str::len), Some(3_000));
    assert_eq!(renewal["ok"], true);
    assert_eq!(renewal["job"]["replayCursor"], 7);
    assert_eq!(claim["taskId"], "queued");
    assert_eq!(batches() - batches_before, 2,
        "the stalled batch, then one batch for every commit queued behind it");
    driver.stop_and_join().unwrap();
}
