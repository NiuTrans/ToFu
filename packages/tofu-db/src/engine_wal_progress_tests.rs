// Foreground WAL progress while the existing maintenance/rotation owner is
// delayed. Included by engine::tests; durability and recovery remain mandatory.

    #[test]
    fn online_append_crosses_rotation_trigger_without_running_maintenance() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.attach_rotation_driver();
        engine.set_rotation_threshold_bytes_for_test(2048);
        for index in 0..24_u8 {
            engine.commit(&vec![index; 512]).unwrap_or_else(|error| {
                panic!("foreground append {index} waited for background rotation: {error}")
            });
        }
        assert_eq!(engine.state.active_log_generation, 1,
            "foreground append must leave physical maintenance to its driver");
        assert!(engine.wal.len() > 6 * 2048);
        let work = engine.take_prepared_rotation().unwrap().expect("driver must observe the backlog");
        let RotationWork::Fresh(snapshot) = work else { panic!("expected a fresh rotation") };
        let outcome = encode_rotation_segments(&snapshot);
        engine.finish_rotation_checkpoint(snapshot, outcome).unwrap();
        assert_eq!(engine.state.checkpoint_sequence, 24);
        assert_eq!(engine.wal.len(), 0);
        drop(engine);
        let reopened = Engine::open(directory.path()).unwrap();
        for index in 0..24_u8 {
            assert_eq!(reopened.transaction_at(u64::from(index) + 1).unwrap().unwrap()
                .envelope.inline_payload, vec![index; 512]);
        }
    }

    #[test]
    fn parked_history_publication_allows_wal_growth_recovery_and_catchup() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        let root = engine.write_block(b"authority root").unwrap();
        engine.commit_references_with_authority_state(b"first", &[], Some(root)).unwrap();
        indexed_rotate(&mut engine);
        engine.commit(b"second").unwrap();
        indexed_rotate(&mut engine);
        engine.attach_rotation_driver();
        engine.set_rotation_threshold_bytes_for_test(1024 * 1024);
        let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
        let backlog_path = engine.wal.path().to_owned();
        let mut payload_ids = Vec::new();
        // Leave the physical publication owner parked throughout more than
        // three former 64 MiB WAL windows, not just one small append.
        for index in 0..96_u8 {
            let committed = engine.commit_transaction(&[index], &[&vec![index; 2 * 1024 * 1024]]).unwrap_or_else(|error| {
                panic!("history maintenance blocked accepted write {index}: {error}")
            });
            payload_ids.push(committed.block_ids[0]);
        }
        let backlog_bytes = engine.wal.len();
        assert!(backlog_bytes > 3 * 64 * 1024 * 1024);
        assert_eq!(engine.state.durable_sequence, 98);
        let outcome = encode_history_compaction(&prepared);
        engine.finish_history_compaction(prepared, outcome).unwrap();
        // Reopen must accept and verify the entire durable backlog before
        // its existing maintenance driver catches up and retires the WAL.
        drop(engine);
        let mut reopened = Engine::open(directory.path()).unwrap();
        assert_eq!(reopened.state.durable_sequence, 98);
        for index in 0..96_u8 {
            assert_eq!(reopened.transaction_at(u64::from(index) + 3).unwrap().unwrap()
                .envelope.inline_payload, vec![index]);
            assert_eq!(reopened.read_block(payload_ids[index as usize]).unwrap(), vec![index; 2 * 1024 * 1024]);
        }
        reopened.attach_rotation_driver();
        let work = reopened.take_prepared_rotation().unwrap().expect("recovered backlog must be drained");
        let RotationWork::Fresh(snapshot) = work else { panic!("expected backlog rotation") };
        let outcome = encode_rotation_segments(&snapshot);
        reopened.finish_rotation_checkpoint(snapshot, outcome).unwrap();
        assert_eq!(reopened.state.checkpoint_sequence, 98);
        assert_eq!(reopened.wal.len(), 0);
        assert!(!backlog_path.exists());
        eprintln!("parked maintenance: 96 writes, {backlog_bytes} WAL bytes; recovered=96; active WAL after catchup=0");
    }

    #[cfg(unix)]
    #[test]
    fn wal_progress_sigkill_child() {
        let Some(path) = std::env::var_os("TOFUDB_WAL_PROGRESS_CHILD_DIRECTORY") else { return; };
        let directory = PathBuf::from(path);
        let mode = std::env::var("TOFUDB_WAL_PROGRESS_CHILD_MODE").unwrap();
        let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
        let gate = Arc::new(GenerationIoGate { entered: entered_sender,
            armed: std::sync::atomic::AtomicBool::new(false), released: Mutex::new(false), wake: std::sync::Condvar::new(),
            maximum_pause: None });
        let control = mode != "strict-encode";
        let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), control));
        let mut engine = Engine::initialize_with_vfs(&directory, vfs).unwrap();
        engine.attach_rotation_driver();
        engine.set_rotation_threshold_bytes_for_test(1024 * 1024);
        if mode == "normal-control" { engine.set_wal_durability(WalDurability::Normal).unwrap(); }
        engine.commit_transaction(b"predecessor", &[b"acknowledged predecessor"]).unwrap();
        if control {
            let prepared = prepared_rotation_publication(&mut engine);
            gate.armed.store(true, std::sync::atomic::Ordering::Release);
            std::thread::spawn(move || { let _ = publish_rotation_publication(&prepared); });
        } else {
            let snapshot = generation_snapshot(&mut engine);
            gate.armed.store(true, std::sync::atomic::Ordering::Release);
            std::thread::spawn(move || { let _ = encode_rotation_segments(&snapshot); });
        }
        entered_receiver.recv_timeout(std::time::Duration::from_secs(10)).expect("background fsync never entered");
        if mode == "strict-control" {
            // Regression: the former shared gate injected a TimedOut I/O
            // failure after ten seconds, fencing an otherwise healthy WAL.
            // Keep the publisher paused past that interval before writing.
            std::thread::sleep(std::time::Duration::from_secs(11));
        }
        if mode == "strict-group-control" {
            for pair in 0..20_u8 {
                let transactions = [pair * 2, pair * 2 + 1].map(|index| BatchTransaction {
                    inline_payload: vec![index], block_payloads: vec![vec![index; 2 * 1024 * 1024]],
                });
                engine.commit_batch(&transactions).unwrap();
            }
        } else {
            for index in 0..40_u8 {
                engine.commit_transaction(&[index], &[&vec![index; 2 * 1024 * 1024]]).unwrap();
            }
        }
        // Normal-mode publication also requires a data barrier before a
        // visible ACK. Exercise that same explicit witness before signaling.
        engine.sync_wal_pending().unwrap();
        assert!(engine.wal.len() > 64 * 1024 * 1024);
        let marker = directory.join("wal-progress-ack.ready");
        let temporary = directory.join("wal-progress-ack.tmp");
        std::fs::write(&temporary, b"41").unwrap();
        std::fs::File::open(&temporary).unwrap().sync_all().unwrap();
        std::fs::rename(&temporary, &marker).unwrap();
        std::fs::File::open(&directory).unwrap().sync_all().unwrap();
        // The parent's pipe owns this test process lifetime. Parent death
        // releases it even when the background thread remains deliberately
        // paused; scheduling delays cannot expire an acknowledged fixture.
        let _ = std::io::Read::read(&mut std::io::stdin().lock(), &mut [0_u8; 1]);
        std::process::exit(0);
    }

    #[cfg(unix)]
    #[test]
    fn wal_progress_sigkill_recovers_large_successor_while_background_fsync_is_paused() {
        use std::os::unix::process::ExitStatusExt;
        struct ChildGuard(std::process::Child);
        impl Drop for ChildGuard {
            fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
        }
        for mode in ["strict-encode", "strict-control", "normal-control", "strict-group-control"] {
            let root = tempfile::tempdir().unwrap();
            let directory = root.path().join("authority");
            std::fs::create_dir(&directory).unwrap();
            let log_path = root.path().join("child.log");
            let log = std::fs::File::create(&log_path).unwrap();
            let mut child = ChildGuard(std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "engine::tests::wal_progress_sigkill_child", "--nocapture"])
                .env("TOFUDB_WAL_PROGRESS_CHILD_DIRECTORY", &directory)
                .env("TOFUDB_WAL_PROGRESS_CHILD_MODE", mode)
                .stdin(std::process::Stdio::piped())
                .stdout(log.try_clone().unwrap()).stderr(log).spawn().unwrap());
            let marker = directory.join("wal-progress-ack.ready");
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(120);
            while !marker.exists() {
                assert!(child.0.try_wait().unwrap().is_none(), "{mode} child exited before ACK: {}",
                    std::fs::read_to_string(&log_path).unwrap());
                assert!(std::time::Instant::now() < deadline, "{mode} did not make foreground progress");
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            assert_eq!(std::fs::read(&marker).unwrap(), b"41");
            child.0.kill().unwrap();
            assert_eq!(child.0.wait().unwrap().signal(), Some(9));
            let mut reopened = Engine::open(&directory).unwrap();
            assert_eq!(reopened.state.durable_sequence, 41);
            assert_eq!(reopened.read_block(BlockId::for_payload(b"acknowledged predecessor")).unwrap(), b"acknowledged predecessor");
            for index in 0..40_u8 {
                let expected = vec![index; 2 * 1024 * 1024];
                assert_eq!(reopened.read_block(BlockId::for_payload(&expected)).unwrap(), expected);
                assert_eq!(reopened.transaction_at(u64::from(index) + 2).unwrap().unwrap().envelope.inline_payload, vec![index]);
            }
            reopened.commit(b"after abrupt recovery").unwrap();
            reopened.checkpoint().unwrap();
            eprintln!("WAL progress {mode}: 41 acknowledged records survive SIGKILL with >64 MiB successor");
        }
    }

    #[test]
    fn wal_io_busy_during_rotation_fences_uncertain_append_instead_of_retrying_it() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"acknowledged prefix").unwrap();
        let snapshot = generation_snapshot(&mut engine);
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::ResourceBusy),
        })).unwrap();
        assert_eq!(engine.commit(b"uncertain append").unwrap_err().kind(), io::ErrorKind::ResourceBusy);
        assert_eq!(vfs.trace().unwrap()[0], Operation::Write);
        assert!(engine.require_usable_authority().is_err(),
            "a physical WAL write failure must never be mistaken for harmless maintenance deferral");
        assert!(engine.commit(b"must not reuse uncertain WAL").is_err());
        drop(snapshot);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 1);
    }
