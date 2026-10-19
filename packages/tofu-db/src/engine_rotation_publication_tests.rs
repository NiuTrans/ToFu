// Physical publication tests: real blocked fsync, exact successor reconciliation,
// deterministic crash faults and bounded same-process paired latency measurement.

    fn prepared_rotation_publication(engine: &mut Engine) -> Box<PreparedRotationPublication> {
        let snapshot = generation_snapshot(engine);
        let outcome = encode_rotation_segments(&snapshot);
        match engine.prepare_rotation_publication(snapshot, outcome).unwrap() {
            RotationPublication::Prepared(prepared) => prepared,
            RotationPublication::Complete(_) => panic!("sealed rotation must prepare publication"),
        }
    }

    #[test]
    fn rotation_publication_preparation_has_no_io_and_preserves_newer_successor_state() {
        for normal in [false, true] {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.attach_rotation_driver();
            if normal { engine.set_wal_durability(WalDurability::Normal).unwrap(); }
            engine.commit_transaction(b"old", &[b"old payload"]).unwrap();
            let snapshot = generation_snapshot(&mut engine);
            engine.commit_transaction(b"captured successor", &[b"captured payload"]).unwrap();
            let outcome = encode_rotation_segments(&snapshot);
            vfs.arm_fault(None).unwrap();
            let prepared = match engine.prepare_rotation_publication(snapshot, outcome).unwrap() {
                RotationPublication::Prepared(prepared) => prepared,
                _ => panic!("expected publication"),
            };
            assert!(vfs.trace().unwrap().is_empty(), "publication preparation must be metadata-only");
            engine.commit_transaction(b"concurrent successor", &[b"concurrent payload"]).unwrap();
            let newest = engine.state.clone();
            let published = publish_rotation_publication(&prepared);
            engine.finish_rotation_publication(prepared, published).unwrap();
            assert_eq!(engine.state.durable_sequence, newest.durable_sequence);
            assert_eq!(engine.state.root_hash, newest.root_hash);
            assert_eq!(engine.state.authority_state_root, newest.authority_state_root);
            assert_eq!(engine.state.checkpoint_sequence, 1);
            assert!(engine.control_dirty);
            assert!(!engine.async_publication_in_flight);
            engine.wal.sync_pending().unwrap(); // Normal's last tail is explicitly covered before crash.
            std::mem::forget(engine);
            vfs.crash().unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state.durable_sequence, 3);
            for text in [b"old payload".as_slice(), b"captured payload", b"concurrent payload"] {
                assert_eq!(recovered.read_block(BlockId::for_payload(text)).unwrap(), text);
            }
        }
    }

    #[test]
    fn rotation_publication_every_io_fault_keeps_acknowledged_predecessor_and_successor() {
        let (seed, mut engine) = initialized_simulated_engine();
        engine.commit_transaction(b"prefix", &[b"old payload"]).unwrap();
        engine.flush_dirty_control().unwrap();
        std::mem::forget(engine); seed.crash().unwrap();
        let perform = |engine: &mut Engine, vfs: &DeterministicVfs| {
            engine.attach_rotation_driver();
            let prepared = prepared_rotation_publication(engine);
            engine.commit_transaction(b"concurrent", &[b"new payload"]).unwrap();
            vfs.arm_fault(None).unwrap();
            prepared
        };
        let sample = Arc::new(seed.fork_durable().unwrap());
        let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
        let prepared = perform(&mut engine, &sample);
        let outcome = publish_rotation_publication(&prepared);
        let trace = sample.trace().unwrap();
        engine.finish_rotation_publication(prepared, outcome).unwrap();
        let mut cases = 0;
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(3)); }
            if matches!(operation, Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                let prepared = perform(&mut engine, &vfs);
                let occurrence = trace[..=index].iter().filter(|candidate| *candidate == operation).count();
                vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
                let outcome = publish_rotation_publication(&prepared);
                let failed = outcome.is_err();
                let result = engine.finish_rotation_publication(prepared, outcome);
                assert!(vfs.operation_fault_fired().unwrap(), "fault {index} {operation:?} must fire");
                if failed { assert!(result.is_err()); assert!(engine.is_restart_required()); }
                std::mem::forget(engine); vfs.crash().unwrap(); vfs.arm_fault(None).unwrap();
                let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap_or_else(|error|
                    panic!("fault {index} {operation:?}: {error}"));
                assert_eq!(recovered.state.durable_sequence, 2);
                assert_eq!(recovered.read_block(BlockId::for_payload(b"old payload")).unwrap(), b"old payload");
                assert_eq!(recovered.read_block(BlockId::for_payload(b"new payload")).unwrap(), b"new payload");
                cases += 1;
            }
        }
        assert!(cases > 10);
        eprintln!("rotation publication fault matrix: {cases} cases / {} operations", trace.len());
    }

    // A wrapper around the actual RealVfs CONTROL fd, not a fake storage call.
    // The blocked phase is identical in both arms; the changed lock placement
    // determines whether the foreground commit waits for it.
    fn measure_rotation_publication_overlap(unlocked: bool, delay_ms: u64) -> (f64, f64) {
        let directory = match std::env::var_os("TOFUDB_ROTATION_PUBLICATION_BENCH_PARENT") {
            Some(parent) => tempfile::tempdir_in(parent).unwrap(),
            None => tempfile::tempdir().unwrap(),
        };
        let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
        let gate = Arc::new(GenerationIoGate { entered: entered_sender,
            armed: std::sync::atomic::AtomicBool::new(false), released: Mutex::new(false), wake: std::sync::Condvar::new() });
        let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), true));
        let engine = Arc::new(Mutex::new(Engine::initialize_with_vfs(directory.path(), vfs.clone()).unwrap()));
        let snapshot = {
            let mut engine = engine.lock().unwrap();
            engine.attach_rotation_driver();
            engine.commit_transaction(b"prefix", &[b"durable prefix payload"]).unwrap();
            generation_snapshot(&mut engine)
        };
        let encoded = encode_rotation_segments(&snapshot);
        let publication_engine = Arc::clone(&engine);
        gate.armed.store(true, std::sync::atomic::Ordering::Release);
        let publisher = std::thread::spawn(move || {
            let start = std::time::Instant::now();
            if unlocked {
                let prepared = match publication_engine.lock().unwrap().prepare_rotation_publication(snapshot, encoded).unwrap() {
                    RotationPublication::Prepared(prepared) => prepared,
                    _ => panic!("expected prepared publication"),
                };
                let outcome = publish_rotation_publication(&prepared);
                publication_engine.lock().unwrap().finish_rotation_publication(prepared, outcome).unwrap();
            } else {
                publication_engine.lock().unwrap().finish_rotation_checkpoint(snapshot, encoded).unwrap();
            }
            start.elapsed().as_secs_f64() * 1000.0
        });
        entered_receiver.recv_timeout(std::time::Duration::from_secs(5)).unwrap();
        let release_gate = Arc::clone(&gate);
        let release = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(delay_ms));
            *release_gate.released.lock().unwrap() = true;
            release_gate.wake.notify_all();
        });
        let start = std::time::Instant::now();
        engine.lock().unwrap().commit_transaction(b"foreground ACK", &[b"durable successor payload"]).unwrap();
        let commit_ms = start.elapsed().as_secs_f64() * 1000.0;
        release.join().unwrap(); let publication_ms = publisher.join().unwrap();
        // Exact real-file reopen is required in every measured sample.
        drop(engine);
        let recovered = Engine::open(directory.path()).unwrap();
        assert_eq!(recovered.state.durable_sequence, 2);
        assert_eq!(recovered.read_block(BlockId::for_payload(b"durable prefix payload")).unwrap(), b"durable prefix payload");
        assert_eq!(recovered.read_block(BlockId::for_payload(b"durable successor payload")).unwrap(), b"durable successor payload");
        (commit_ms, publication_ms)
    }

    #[test]
    fn rotation_publication_real_control_fsync_does_not_block_foreground_ack() {
        let (commit_ms, publication_ms) = measure_rotation_publication_overlap(true, 500);
        assert!(commit_ms < publication_ms / 2.0, "foreground {commit_ms} ms waited for publication {publication_ms} ms");
    }

    #[test]
    fn history_publication_real_control_fsync_does_not_block_foreground_ack() {
        let directory = tempfile::tempdir().unwrap();
        let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
        let gate = Arc::new(GenerationIoGate { entered: entered_sender,
            armed: std::sync::atomic::AtomicBool::new(false), released: Mutex::new(false), wake: std::sync::Condvar::new() });
        let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), true));
        let mut engine = Engine::initialize_with_vfs(directory.path(), vfs).unwrap();
        let root = engine.write_block(b"authority root").unwrap();
        engine.commit_references_with_authority_state(b"old", &[], Some(root)).unwrap();
        indexed_rotate(&mut engine);
        engine.commit_transaction(b"new", &[b"promoted history bytes"]).unwrap();
        indexed_rotate(&mut engine);
        let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
        gate.armed.store(true, std::sync::atomic::Ordering::Release);
        let publisher = std::thread::spawn(move || {
            let outcome = encode_history_compaction(&prepared);
            (prepared, outcome)
        });
        let entered = entered_receiver.recv_timeout(std::time::Duration::from_secs(5));
        let foreground = engine.commit_transaction(b"during history publish", &[b"live history-window bytes"]);
        *gate.released.lock().unwrap() = true; gate.wake.notify_all();
        let (prepared, outcome) = publisher.join().unwrap();
        entered.expect("history publication must pause in actual CONTROL sync");
        assert_eq!(foreground.unwrap().sequence, 3);
        engine.finish_history_compaction(prepared, outcome).unwrap();
        assert_eq!(engine.state.durable_sequence, 3);
        assert!(engine.control_dirty);
        drop(engine);
        let recovered = Engine::open(directory.path()).unwrap();
        assert_eq!(recovered.state.durable_sequence, 3);
        assert_eq!(recovered.read_block(BlockId::for_payload(b"live history-window bytes")).unwrap(), b"live history-window bytes");
    }

    #[test]
    fn history_publication_every_io_fault_preserves_concurrent_acknowledgements() {
        let (seed, mut engine) = initialized_simulated_engine();
        let root = engine.write_block(b"authority root").unwrap();
        engine.commit_references_with_authority_state(b"old", &[], Some(root)).unwrap();
        indexed_rotate(&mut engine);
        engine.commit_transaction(b"new", &[b"promoted history bytes"]).unwrap();
        indexed_rotate(&mut engine);
        std::mem::forget(engine); seed.crash().unwrap();
        let prepare = |engine: &mut Engine| {
            let mut prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
            // Identical promotion IDs make manifest hashes and shard-creation
            // operations identical in the trace and every fault replay.
            prepared.set_payload_segment_test_ids(
                (1..=16).map(|id| Uuid::from_u128(0x1000 + id)).collect());
            engine.commit_transaction(b"concurrent", &[b"concurrent history payload"]).unwrap();
            prepared
        };
        let sample = Arc::new(seed.fork_durable().unwrap());
        let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
        let prepared = prepare(&mut engine); sample.arm_fault(None).unwrap();
        let outcome = encode_history_compaction(&prepared);
        let trace = sample.trace().unwrap();
        engine.finish_history_compaction(prepared, outcome).unwrap();
        let mut cases = 0;
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(3)); }
            if matches!(operation, Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                let prepared = prepare(&mut engine);
                let occurrence = trace[..=index].iter().filter(|candidate| *candidate == operation).count();
                vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
                let outcome = encode_history_compaction(&prepared);
                let _ = engine.finish_history_compaction(prepared, outcome);
                assert!(vfs.operation_fault_fired().unwrap(), "history fault {index} {operation:?} must fire");
                std::mem::forget(engine); vfs.crash().unwrap(); vfs.arm_fault(None).unwrap();
                let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap_or_else(|error|
                    panic!("history fault {index} {operation:?}: {error}"));
                assert_eq!(recovered.state.durable_sequence, 3);
                assert_eq!(recovered.read_block(BlockId::for_payload(b"promoted history bytes")).unwrap(), b"promoted history bytes");
                assert_eq!(recovered.read_block(BlockId::for_payload(b"concurrent history payload")).unwrap(), b"concurrent history payload");
                cases += 1;
            }
        }
        assert!(cases > 10);
        eprintln!("history publication fault matrix: {cases} cases / {} operations", trace.len());
    }

    #[test]
    #[ignore = "explicit paired slow-I/O latency measurement; writes only disposable authorities"]
    fn rotation_publication_paired_latency_measurement() {
        let samples = std::env::var("TOFUDB_ROTATION_PUBLICATION_BENCH_SAMPLES").ok()
            .map(|value| value.parse::<usize>().unwrap()).unwrap_or(20);
        assert!((4..=1000).contains(&samples));
        let delay_ms = std::env::var("TOFUDB_ROTATION_PUBLICATION_BENCH_DELAY_MS").ok()
            .map(|value| value.parse::<u64>().unwrap()).unwrap_or(100);
        assert!((10..=2000).contains(&delay_ms));
        let mut rows = Vec::new();
        for pair in 0..samples {
            for unlocked in if pair % 2 == 0 { [false, true] } else { [true, false] } {
                let (commit_ms, publication_ms) = measure_rotation_publication_overlap(unlocked, delay_ms);
                rows.push(serde_json::json!({"pair": pair, "unlocked": unlocked,
                    "foreground_commit_ms": commit_ms, "publication_ms": publication_ms}));
            }
        }
        eprintln!("ROTATION_PUBLICATION_MEASUREMENT={}", serde_json::json!({
            "source_sha256": env!("TOFUDB_SOURCE_SHA256"), "delay_ms": delay_ms,
            "samples_per_arm": samples, "all_exact_reopens": true, "rows": rows}));
    }

    #[test]
    fn rotation_publication_refuses_a_failed_unwritten_successor_prefix() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit_transaction(b"confirmed predecessor", &[b"confirmed payload"]).unwrap();
        let snapshot = generation_snapshot(&mut engine);
        let encoded = encode_rotation_segments(&snapshot);
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
        engine.attach_wal_barrier(&barrier).unwrap();
        engine.commit_transaction(b"unconfirmed successor", &[b"queued payload"]).unwrap();
        let prepared = match engine.prepare_rotation_publication(snapshot, encoded).unwrap() {
            RotationPublication::Prepared(prepared) => prepared,
            _ => panic!("sealed predecessor must prepare publication"),
        };
        vfs.arm_operation_fault(Operation::Write, 1,
            FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
        assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
        assert!(barrier.barrier_error().is_some());
        let outcome = publish_rotation_publication(&prepared);
        assert!(outcome.is_err(), "raw fsync of empty successor cannot publish its queued sequence");
        assert!(engine.finish_rotation_publication(prepared, outcome).is_err());
        assert!(engine.is_restart_required());
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 1);
        assert_eq!(reopened.read_block(BlockId::for_payload(b"confirmed payload")).unwrap(), b"confirmed payload");
    }

    #[test]
    fn pack_publication_refuses_a_failed_unwritten_strict_prefix() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
        engine.attach_wal_barrier(&barrier).unwrap();
        engine.commit_transaction(b"queued command", &[b"queued packed payload"]).unwrap();
        build_and_complete_background_pack(&engine, &[b"queued packed payload"]);
        let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
            PackAdoptionPreparation::Ready(prepared) => prepared,
            _ => panic!("completed segment must prepare adoption"),
        };
        vfs.arm_operation_fault(Operation::Write, 1,
            FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
        assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
        let outcome = publish_prepared_pack_adoption(&mut prepared);
        assert!(outcome.is_err(), "Strict handoff is not physical WAL coverage");
        assert!(engine.finish_pack_adoption(prepared, outcome).is_err());
        assert!(engine.is_restart_required());
        std::mem::forget(engine);
        vfs.crash().unwrap();
        assert_eq!(Engine::open_with_vfs(Path::new("/data"), vfs).unwrap().state.durable_sequence, 0);
    }

    #[test]
    fn history_publication_refuses_a_failed_unwritten_active_prefix() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let root = engine.write_block(b"confirmed authority root").unwrap();
        engine.commit_references_with_authority_state(b"first", &[], Some(root)).unwrap();
        engine.commit_transaction(b"old", &[b"confirmed pruned payload"]).unwrap();
        indexed_rotate(&mut engine);
        engine.commit_transaction(b"new", &[b"confirmed retained payload"]).unwrap();
        indexed_rotate(&mut engine);
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
        engine.attach_wal_barrier(&barrier).unwrap();
        engine.commit_transaction(b"queued active", &[b"unconfirmed active payload"]).unwrap();
        let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
        vfs.arm_operation_fault(Operation::Write, 1,
            FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)).unwrap();
        assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
        let outcome = encode_history_compaction(&prepared);
        assert!(outcome.is_err(), "history publication cannot cover empty physical active WAL");
        assert!(engine.finish_history_compaction(prepared, outcome).is_err());
        assert!(engine.is_restart_required());
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 3);
        assert_eq!(reopened.read_block(BlockId::for_payload(b"confirmed retained payload")).unwrap(), b"confirmed retained payload");
    }
