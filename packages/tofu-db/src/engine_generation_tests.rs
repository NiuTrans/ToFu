// Executable specification for predecessor/successor WAL ownership. Included
// inside engine::tests; deterministic crash tests use VFS, overlap uses RealVfs.

    fn generation_snapshot(engine: &mut Engine) -> Box<PreparedRotationCheckpoint> {
        match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected generation snapshot: {other:?}"),
        }
    }

    #[test]
    fn generation_handover_recovers_both_wals_before_and_after_prefix_encoding() {
        for encoded in [false, true] {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.attach_rotation_driver();
            let first = engine.commit_transaction(b"predecessor", &[b"predecessor payload"]).unwrap();
            let snapshot = generation_snapshot(&mut engine);
            assert_eq!(engine.state.active_log_generation, 2);
            assert_eq!(engine.state.sealed_log.as_ref().unwrap().generation, 1);
            assert_eq!(engine.wal.len(), 0, "handover must not copy the predecessor bytes");
            let second = engine.commit_transaction(b"successor", &[b"successor payload"]).unwrap();
            assert_eq!(engine.state.checkpoint_sequence, 0);
            if encoded { encode_rotation_segments(&snapshot).unwrap(); }
            // Both WALs are needed even when immutable prefix artifacts exist.
            assert!(vfs.metadata(&crate::wal::active_log_path(Path::new("/data"), 1).unwrap()).is_ok());
            drop(snapshot);
            std::mem::forget(engine);
            vfs.crash().unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state.durable_sequence, 2);
            assert_eq!(reopened.state.checkpoint_sequence, 1);
            assert!(reopened.state.sealed_log.is_none());
            assert_eq!(reopened.committed.iter().map(|tx| tx.sequence).collect::<Vec<_>>(), vec![2]);
            assert_eq!(reopened.read_block(first.block_ids[0]).unwrap(), b"predecessor payload");
            assert_eq!(reopened.read_block(second.block_ids[0]).unwrap(), b"successor payload");
            assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        }
    }

    #[test]
    fn generation_completion_preserves_successor_buffer_and_allows_the_next_rotation() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"first").unwrap();
        let snapshot = generation_snapshot(&mut engine);
        engine.commit_transaction(b"second", &[b"second carried"]).unwrap();
        let bytes = engine.unstaged_history_bytes;
        let wal_bytes = engine.wal.len();
        assert!(matches!(engine.try_prepare_rotation_checkpoint().unwrap(), RotationCheckpointPreparation::Busy));
        assert_eq!(engine.checkpoint().unwrap_err().kind(), io::ErrorKind::WouldBlock);
        let outcome = encode_rotation_segments(&snapshot);
        engine.finish_rotation_checkpoint(snapshot, outcome).unwrap();
        assert_eq!(engine.unstaged_history_bytes, bytes);
        assert_eq!(engine.wal.len(), wal_bytes);
        assert_eq!(engine.state.checkpoint_sequence, 1);
        assert_eq!(engine.committed.len(), 1);
        let next = generation_snapshot(&mut engine);
        assert_eq!(engine.state.active_log_generation, 3);
        engine.commit(b"third").unwrap();
        let outcome = encode_rotation_segments(&next);
        engine.finish_rotation_checkpoint(next, outcome).unwrap();
        assert_eq!(engine.state.checkpoint_sequence, 2);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        for sequence in 1..=3 { assert_eq!(reopened.transaction_at(sequence).unwrap().unwrap().sequence, sequence); }
    }

    #[test]
    fn generation_predecessor_corruption_and_missing_successor_preserve_evidence() {
        for missing_successor in [false, true] {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.attach_rotation_driver();
            engine.commit(b"durable predecessor").unwrap();
            let snapshot = generation_snapshot(&mut engine);
            engine.commit(b"durable successor").unwrap();
            engine.flush_dirty_control().unwrap();
            let predecessor = crate::wal::active_log_path(Path::new("/data"), 1).unwrap();
            let successor = crate::wal::active_log_path(Path::new("/data"), 2).unwrap();
            drop(snapshot);
            std::mem::forget(engine);
            if missing_successor {
                let mut file = vfs.open(&successor, crate::vfs::OpenRequest { read: true, write: true, ..Default::default() }).unwrap();
                file.set_len(0).unwrap(); file.sync_all().unwrap();
            } else {
                let mut file = vfs.open(&predecessor, crate::vfs::OpenRequest { read: true, write: true, ..Default::default() }).unwrap();
                file.write_all_at(10, &[0xff]).unwrap(); file.sync_all().unwrap();
            }
            vfs.crash().unwrap();
            let before = [predecessor, successor].map(|path| {
                let mut file = vfs.open(&path, crate::vfs::OpenRequest { read: true, ..Default::default() }).unwrap();
                (path, file.read_all(ACTIVE_LOG_MAX_BYTES as usize).unwrap())
            });
            assert!(Engine::open_with_vfs(Path::new("/data"), vfs.clone()).is_err());
            for (path, bytes) in before {
                let mut file = vfs.open(&path, crate::vfs::OpenRequest { read: true, ..Default::default() }).unwrap();
                assert_eq!(file.read_all(ACTIVE_LOG_MAX_BYTES as usize).unwrap(), bytes);
            }
        }
    }

    #[test]
    fn generation_torn_successor_tail_does_not_use_predecessor_file_offsets() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"predecessor").unwrap();
        let snapshot = generation_snapshot(&mut engine);
        let successor = crate::wal::active_log_path(Path::new("/data"), 2).unwrap();
        let mut file = vfs.open(&successor, crate::vfs::OpenRequest { read: true, write: true, ..Default::default() }).unwrap();
        file.write_all_at(0, &[1, 2, 3]).unwrap(); file.sync_all().unwrap();
        drop(snapshot);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.state.durable_sequence, 1);
        assert_eq!(reopened.wal.len(), 0, "active truncation must use successor offsets");
        assert!(reopened.transaction_at(1).unwrap().is_some());
    }

    #[test]
    fn generation_handover_successor_ack_and_completion_survive_every_io_fault() {
        let (seed, mut engine) = initialized_simulated_engine();
        engine.commit_transaction(b"one", &[b"old durable payload"]).unwrap();
        engine.flush_dirty_control().unwrap();
        std::mem::forget(engine);
        seed.crash().unwrap();
        let perform = |engine: &mut Engine, acknowledged: &mut bool| -> io::Result<()> {
            let snapshot = match engine.try_prepare_rotation_checkpoint()? {
                RotationCheckpointPreparation::Ready(snapshot) => snapshot,
                other => panic!("unexpected generation preparation: {other:?}"),
            };
            engine.commit_transaction(b"two", &[b"new durable payload"])?;
            *acknowledged = true;
            engine.flush_dirty_control()?;
            let outcome = encode_rotation_segments(&snapshot);
            engine.finish_rotation_checkpoint(snapshot, outcome)?;
            Ok(())
        };
        let sample = Arc::new(seed.fork_durable().unwrap());
        let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
        engine.attach_rotation_driver();
        sample.arm_fault(None).unwrap();
        perform(&mut engine, &mut false).unwrap();
        let trace = sample.trace().unwrap();
        std::mem::forget(engine);
        let mut cases = 0;
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if matches!(operation, Operation::Write) { actions.push(FaultAction::ShortWrite(3)); }
            if matches!(operation, Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory) { actions.push(FaultAction::DropSync); }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                engine.attach_rotation_driver();
                let occurrence = trace[..=index].iter().filter(|candidate| *candidate == operation).count();
                vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
                let mut acknowledged = false;
                let _ = perform(&mut engine, &mut acknowledged);
                assert!(vfs.operation_fault_fired().unwrap(), "generation fault {index} {operation:?} did not fire");
                std::mem::forget(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs.clone())
                    .unwrap_or_else(|error| panic!("generation fault {index} {operation:?}: {error}"));
                assert_eq!(recovered.read_block(BlockId::for_payload(b"old durable payload")).unwrap(), b"old durable payload");
                if acknowledged {
                    assert_eq!(recovered.state.durable_sequence, 2, "lost successor ACK at {index}");
                    assert_eq!(recovered.read_block(BlockId::for_payload(b"new durable payload")).unwrap(), b"new durable payload");
                }
                let head = recovered.state.durable_sequence;
                recovered.commit(b"after recovery").unwrap();
                recovered.checkpoint().unwrap();
                std::mem::forget(recovered);
                vfs.crash().unwrap();
                assert_eq!(Engine::open_with_vfs(Path::new("/data"), vfs).unwrap().state.durable_sequence, head + 1);
                cases += 1;
            }
        }
        eprintln!("generation fault matrix: {cases} cases over {} real operations", trace.len());
        assert!(cases > 30);
    }

    struct GenerationIoGate {
        entered: std::sync::mpsc::SyncSender<()>,
        armed: std::sync::atomic::AtomicBool,
        released: Mutex<bool>,
        wake: std::sync::Condvar,
    }

    struct GenerationPausedVfs(Arc<GenerationIoGate>, bool);
    struct GenerationPausedFile {
        inner: Box<dyn crate::vfs::VfsFile>,
        gate: Arc<GenerationIoGate>,
    }

    impl crate::vfs::VfsFile for GenerationPausedFile {
        fn len(&self) -> io::Result<u64> { self.inner.len() }
        fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> { self.inner.read_all(maximum) }
        fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> { self.inner.read_exact_at(offset, bytes) }
        fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> { self.inner.write_all_at(offset, bytes) }
        fn set_len(&mut self, len: u64) -> io::Result<()> { self.inner.set_len(len) }
        fn sync_data(&mut self) -> io::Result<()> { self.inner.sync_data() }
        fn try_lock_exclusive(&mut self) -> io::Result<()> { self.inner.try_lock_exclusive() }
        fn sync_all(&mut self) -> io::Result<()> {
            if self.gate.armed.swap(false, std::sync::atomic::Ordering::AcqRel) {
                self.gate.entered.send(()).map_err(|_| io::Error::other("test gate receiver closed"))?;
                let (released, _) = self.gate.wake.wait_timeout_while(
                    self.gate.released.lock().unwrap(), std::time::Duration::from_secs(10), |released| !*released,
                ).unwrap();
                if !*released { return Err(io::Error::new(io::ErrorKind::TimedOut, "generation IO gate timed out")); }
            }
            self.inner.sync_all()
        }
    }

    impl crate::vfs::Vfs for GenerationPausedVfs {
        fn open(&self, path: &Path, request: crate::vfs::OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
            let inner = crate::vfs::RealVfs.open(path, request)?;
            if (self.1 && path.file_name().is_some_and(|name| name == "CONTROL"))
                || (!self.1 && request.create_new && path.file_name().is_some_and(|name| name.to_string_lossy().starts_with(".new-"))) {
                Ok(Box::new(GenerationPausedFile { inner, gate: Arc::clone(&self.0) }))
            } else { Ok(inner) }
        }
        fn create_dir(&self, path: &Path) -> io::Result<()> { crate::vfs::RealVfs.create_dir(path) }
        fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { crate::vfs::RealVfs.rename(source, destination) }
        fn remove_file(&self, path: &Path) -> io::Result<()> { crate::vfs::RealVfs.remove_file(path) }
        fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> { crate::vfs::RealVfs.metadata(path) }
        fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { crate::vfs::RealVfs.read_directory(path) }
        fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
            crate::vfs::RealVfs.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
            -> io::Result<crate::vfs::BoundedDirectoryMatches> {
            crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, maximum, matches)
        }
        fn sync_directory(&self, path: &Path) -> io::Result<()> { crate::vfs::RealVfs.sync_directory(path) }
    }

    #[test]
    fn generation_successor_commit_and_exact_reads_finish_while_real_prefix_fsync_is_paused() {
        let directory = tempfile::tempdir().unwrap();
        let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
        let gate = Arc::new(GenerationIoGate { entered: entered_sender,
            armed: std::sync::atomic::AtomicBool::new(false), released: Mutex::new(false), wake: std::sync::Condvar::new() });
        let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), false));
        let engine = Arc::new(Mutex::new(Engine::initialize_with_vfs(directory.path(), vfs.clone()).unwrap()));
        let snapshot = {
            let mut engine = engine.lock().unwrap();
            engine.attach_rotation_driver();
            engine.commit_transaction(b"first", &[b"paused predecessor payload"]).unwrap();
            generation_snapshot(&mut engine)
        };
        gate.armed.store(true, std::sync::atomic::Ordering::Release);
        let encoder = std::thread::spawn(move || {
            let outcome = encode_rotation_segments(&snapshot);
            (snapshot, outcome)
        });
        let entered = entered_receiver.recv_timeout(std::time::Duration::from_secs(2));
        let writer_engine = Arc::clone(&engine);
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        let writer = std::thread::spawn(move || {
            let mut engine = writer_engine.lock().unwrap();
            let result = engine.commit_transaction(b"second", &[b"independent successor payload"])
                .and_then(|commit| {
                    assert_eq!(engine.transaction_at(1)?.unwrap().envelope.inline_payload, b"first");
                    assert_eq!(engine.transaction_at(2)?.unwrap().envelope.inline_payload, b"second");
                    assert_eq!(engine.transaction_snapshot()?.len(), 2);
                    assert_eq!(engine.read_block(commit.block_ids[0])?, b"independent successor payload");
                    Ok((commit.sequence, engine.state.active_log_generation))
                });
            sender.send(result).unwrap();
        });
        let committed = receiver.recv_timeout(std::time::Duration::from_secs(2));
        // Release and join before asserting, so regressions cannot leak a thread.
        *gate.released.lock().unwrap() = true;
        gate.wake.notify_all();
        writer.join().unwrap();
        let (snapshot, outcome) = encoder.join().unwrap();
        entered.expect("prefix fsync must reach the real VFS gate");
        assert_eq!(committed.expect("successor writer parked behind predecessor fsync").unwrap(), (2, 2));
        {
            let mut engine = engine.lock().unwrap();
            engine.finish_rotation_checkpoint(snapshot, outcome).unwrap();
            assert_eq!(engine.state.checkpoint_sequence, 1);
            assert_eq!(engine.committed.len(), 1);
        }
        drop(engine);
        let reopened = Engine::open_with_vfs(directory.path(), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 2);
        assert_eq!(reopened.read_block(BlockId::for_payload(b"paused predecessor payload")).unwrap(), b"paused predecessor payload");
        assert_eq!(reopened.read_block(BlockId::for_payload(b"independent successor payload")).unwrap(), b"independent successor payload");
    }

    #[test]
    fn generation_missing_unconfirmed_successor_reference_truncates_only_the_successor() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"predecessor").unwrap();
        let snapshot = generation_snapshot(&mut engine);
        let encoded = TransactionEnvelope { inline_payload: b"unconfirmed".to_vec(),
            block_ids: vec![BlockId::for_payload(b"missing successor block")], authority_state_update: None }.encode().unwrap();
        engine.wal.append(2, engine.state.root_hash, &encoded).unwrap();
        drop(snapshot);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 1);
        assert_eq!(reopened.wal.len(), 0);
        assert_eq!(reopened.transaction_at(1).unwrap().unwrap().envelope.inline_payload, b"predecessor");
    }

    #[test]
    fn generation_orphan_empty_successor_gets_both_barriers_before_handover() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"predecessor").unwrap();
        let path = crate::wal::active_log_path(Path::new("/data"), 2).unwrap();
        // Simulate a prior create failing before inode/directory barriers.
        drop(vfs.open(&path, crate::vfs::OpenRequest { read: true, write: true, create_new: true, ..Default::default() }).unwrap());
        let snapshot = generation_snapshot(&mut engine);
        drop(snapshot);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 1);
        assert_eq!(reopened.state.active_log_generation, 2);
        assert_eq!(reopened.wal.len(), 0);
    }

    #[test]
    fn generation_normal_handover_and_prefix_completion_sync_each_owned_wal() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.set_wal_durability(WalDurability::Normal).unwrap();
        engine.attach_rotation_driver();
        engine.commit(b"normal predecessor").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        let snapshot = generation_snapshot(&mut engine);
        engine.commit(b"normal successor").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        let outcome = encode_rotation_segments(&snapshot);
        engine.finish_rotation_checkpoint(snapshot, outcome).unwrap();
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 2);
        assert_eq!(reopened.state.checkpoint_sequence, 1);
        assert_eq!(reopened.transaction_at(2).unwrap().unwrap().envelope.inline_payload, b"normal successor");
    }

    #[test]
    fn generation_encrypted_handover_recovers_predecessor_and_successor() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let key = crate::at_rest::AtRestKey::from_key([0x53; 32]);
        let mut engine = Engine::initialize_with_vfs_and_key(Path::new("/data"), vfs.clone(), Some(key.clone())).unwrap();
        engine.attach_rotation_driver();
        let first = engine.commit_transaction(b"encrypted predecessor", &[b"encrypted old payload"]).unwrap();
        let snapshot = generation_snapshot(&mut engine);
        let second = engine.commit_transaction(b"encrypted successor", &[b"encrypted new payload"]).unwrap();
        drop(snapshot);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs_and_key(Path::new("/data"), vfs, Some(key)).unwrap();
        assert_eq!(reopened.state.durable_sequence, 2);
        assert_eq!(reopened.state.checkpoint_sequence, 1);
        assert_eq!(reopened.read_block(first.block_ids[0]).unwrap(), b"encrypted old payload");
        assert_eq!(reopened.read_block(second.block_ids[0]).unwrap(), b"encrypted new payload");
    }

    #[test]
    fn generation_corrupt_handover_slot_never_silently_ignores_a_confirmed_successor() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"predecessor").unwrap();
        let snapshot = generation_snapshot(&mut engine);
        let handover_control_generation = engine.state.generation;
        engine.commit(b"confirmed successor").unwrap();
        drop(snapshot);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        // Only the handover slot is damaged; the prior CONTROL slot and both
        // acknowledged WAL records remain durable. Falling back must not
        // expose a seemingly healthy head that omits the successor ACK.
        let mut control = vfs.open(Path::new("/data/CONTROL"), crate::vfs::OpenRequest {
            read: true, write: true, ..Default::default()
        }).unwrap();
        let offset = ((handover_control_generation - 1) % 2) * 4096;
        control.write_all_at(offset, &[0xff]).unwrap(); control.sync_all().unwrap();
        let result = Engine::open_with_vfs(Path::new("/data"), vfs);
        assert!(result.is_err());
        assert!(result.err().unwrap().to_string().contains("unreferenced successor WAL"));
    }

    #[cfg(unix)]
    #[test]
    fn generation_sigkill_child() {
        let Some(path) = std::env::var_os("TOFUDB_GENERATION_SIGKILL_CHILD_DIRECTORY") else { return; };
        let directory = PathBuf::from(path);
        let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
        let gate = Arc::new(GenerationIoGate { entered: entered_sender,
            armed: std::sync::atomic::AtomicBool::new(false), released: Mutex::new(false), wake: std::sync::Condvar::new() });
        let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), false));
        let mut engine = Engine::initialize_with_vfs(&directory, vfs).unwrap();
        engine.attach_rotation_driver();
        engine.commit_transaction(b"predecessor before kill", &[b"durable predecessor before kill"]).unwrap();
        let snapshot = generation_snapshot(&mut engine);
        gate.armed.store(true, std::sync::atomic::Ordering::Release);
        std::thread::spawn(move || { let _ = encode_rotation_segments(&snapshot); });
        entered_receiver.recv_timeout(std::time::Duration::from_secs(2)).expect("real prefix fsync never entered");
        engine.commit_transaction(b"successor ACK before kill", &[b"durable successor before kill"]).unwrap();
        assert_eq!(engine.state.active_log_generation, 2);
        assert!(engine.state.sealed_log.is_some());
        // The marker witnesses an actual strict successor ACK while predecessor
        // fsync is paused. It is test transport, never authority state.
        let marker = directory.join("successor-ack.ready");
        std::fs::write(&marker, b"2").unwrap();
        std::fs::File::open(&marker).unwrap().sync_all().unwrap();
        std::fs::File::open(&directory).unwrap().sync_all().unwrap();
        // A canceled parent must not leave an orphan actor indefinitely.
        std::thread::sleep(std::time::Duration::from_secs(10));
        panic!("parent did not SIGKILL the child within its lifetime bound");
    }

    #[cfg(unix)]
    #[test]
    fn generation_sigkill_during_real_prefix_fsync_recovers_both_acknowledged_wals() {
        use std::os::unix::process::ExitStatusExt;
        struct ChildGuard(std::process::Child);
        impl Drop for ChildGuard {
            fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
        }
        let root = match std::env::var_os("TOFUDB_GENERATION_SIGKILL_PARENT") {
            Some(parent) => tempfile::tempdir_in(parent).unwrap(),
            None => tempfile::tempdir().unwrap(),
        };
        let directory = root.path().join("authority");
        std::fs::create_dir(&directory).unwrap();
        let log = std::fs::File::create(root.path().join("sigkill-child.log")).unwrap();
        let mut child = ChildGuard(std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "engine::tests::generation_sigkill_child", "--nocapture"])
            .env("TOFUDB_GENERATION_SIGKILL_CHILD_DIRECTORY", &directory)
            .stdout(log.try_clone().unwrap()).stderr(log).spawn().unwrap());
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
        let marker = directory.join("successor-ack.ready");
        while !marker.exists() {
            assert!(child.0.try_wait().unwrap().is_none(), "child exited before successor ACK: {}",
                std::fs::read_to_string(root.path().join("sigkill-child.log")).unwrap());
            assert!(std::time::Instant::now() < deadline, "successor ACK marker timed out");
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        assert_eq!(std::fs::read(&marker).unwrap(), b"2");
        child.0.kill().unwrap();
        assert_eq!(child.0.wait().unwrap().signal(), Some(9), "child was not reaped after SIGKILL");
        let witness = ControlFile::open(&directory).unwrap().read_current().unwrap();
        assert_eq!(witness.active_log_generation, 2);
        assert_eq!(witness.sealed_log.as_ref().unwrap().generation, 1);
        assert_eq!(witness.sealed_log.as_ref().unwrap().terminal_sequence, 1);
        assert_eq!(witness.checkpoint_sequence, 0);
        let mut reopened = Engine::open(&directory).unwrap();
        assert_eq!(reopened.state.durable_sequence, 2);
        assert_eq!(reopened.state.checkpoint_sequence, 1);
        assert!(reopened.state.sealed_log.is_none());
        assert_eq!(reopened.read_block(BlockId::for_payload(b"durable predecessor before kill")).unwrap(), b"durable predecessor before kill");
        assert_eq!(reopened.read_block(BlockId::for_payload(b"durable successor before kill")).unwrap(), b"durable successor before kill");
        assert_eq!(reopened.transaction_at(2).unwrap().unwrap().envelope.inline_payload, b"successor ACK before kill");
        reopened.commit(b"post SIGKILL probe").unwrap();
        reopened.checkpoint().unwrap();
        drop(reopened);
        assert_eq!(Engine::open(&directory).unwrap().state.durable_sequence, 3);
    }

    #[test]
    fn generation_legacy_checkpoint_resyncs_an_empty_orphan_before_publication() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.commit(b"acknowledged legacy prefix").unwrap();
        let registration = engine.wal_retirement.register_worker().unwrap();
        let orphan = crate::wal::active_log_path(Path::new("/data"), 2).unwrap();
        drop(vfs.open(&orphan, crate::vfs::OpenRequest { read: true, write: true, create_new: true, ..Default::default() }).unwrap());
        engine.checkpoint().unwrap();
        std::mem::forget(engine);
        std::mem::forget(registration);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs)
            .unwrap_or_else(|error| panic!("acknowledged checkpoint references an unsynced orphan: {error}"));
        assert_eq!(reopened.state.durable_sequence, 1);
        assert_eq!(reopened.state.checkpoint_sequence, 1);
        assert_eq!(reopened.transaction_at(1).unwrap().unwrap().envelope.inline_payload, b"acknowledged legacy prefix");
    }

    #[test]
    fn generation_maximum_successor_can_complete_without_an_unused_third_generation() {
        let (vfs, engine) = initialized_simulated_engine();
        let mut state = engine.state.clone();
        drop(engine);
        vfs.remove_file(Path::new("/data/CONTROL")).unwrap();
        vfs.remove_file(Path::new("/data/active.wal")).unwrap();
        let mut wal = ActiveLog::initialize_generation_with_vfs(Path::new("/data"), u64::MAX - 1, vfs.clone()).unwrap();
        let encoded = TransactionEnvelope { inline_payload: b"last predecessor generation".to_vec(),
            block_ids: Vec::new(), authority_state_update: None }.encode().unwrap();
        state.root_hash = wal.append(1, [0; 32], &encoded).unwrap();
        state.durable_sequence = 1;
        state.active_log_generation = u64::MAX - 1;
        drop(wal);
        ControlFile::initialize_state_with_vfs(Path::new("/data"), state, vfs.clone()).unwrap();
        let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        let snapshot = generation_snapshot(&mut engine);
        assert_eq!(snapshot.next_active_log_generation, u64::MAX);
        assert_eq!(engine.state.active_log_generation, u64::MAX);
        let outcome = encode_rotation_segments(&snapshot);
        engine.finish_rotation_checkpoint(snapshot, outcome).unwrap();
        assert!(engine.state.sealed_log.is_none());
        assert_eq!(engine.state.checkpoint_sequence, 1);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 1);
        assert_eq!(reopened.transaction_at(1).unwrap().unwrap().envelope.inline_payload, b"last predecessor generation");
    }

    #[test]
    fn generation_deferred_catalog_publication_keeps_successor_writes_crash_safe() {
        // Hold the real publication lane unfinished, as with slow mount I/O.
        // Each crash boundary must preserve writes admitted during the defer.
        for durability in [WalDurability::Strict, WalDurability::Normal] {
            for crash_phase in 0..4 {
                let (vfs, mut engine) = initialized_simulated_engine();
                engine.set_wal_durability(durability).unwrap();
                engine.attach_rotation_driver();
                let registration = engine.block_store_shared().register_background_packer().unwrap();
                let prefix = engine.commit_transaction(b"prefix", &[b"prefix carried"]).unwrap();
                build_and_complete_background_pack(&engine, &[b"prefix carried"]);
                let snapshot = generation_snapshot(&mut engine);
                let mut adoption = match engine.try_prepare_pack_adoption().unwrap() {
                    PackAdoptionPreparation::Ready(prepared) => prepared,
                    _ => panic!("catalog publication must own the lane"),
                };
                let encoded = encode_rotation_segments(&snapshot).unwrap();
                assert!(is_publication_owns_materialization(
                    &engine.finish_rotation_checkpoint(snapshot, Ok(encoded)).unwrap_err()));
                assert!(!engine.online_rotation_blocks_writes());
                let suffix_id = engine.write_block(b"suffix carried").unwrap();
                let suffix = engine.commit_references_with_authority_state(
                    b"during defer", &[suffix_id], Some(suffix_id)).unwrap();
                // Normal ACK is covered by the daemon's independent WAL barrier.
                engine.wal.sync_pending().unwrap();
                if crash_phase > 0 {
                    let outcome = publish_prepared_pack_adoption(&mut adoption);
                    if crash_phase > 1 {
                        engine.finish_pack_adoption(adoption, outcome).unwrap();
                        if crash_phase > 2 {
                            let Some(RotationWork::Deferred(snapshot, encoded)) = engine.take_prepared_rotation().unwrap()
                                else { panic!("encoded predecessor must remain parked") };
                            engine.finish_rotation_checkpoint(snapshot, Ok(encoded)).unwrap();
                        }
                    } else { outcome.unwrap(); }
                }
                drop(registration);
                std::mem::forget(engine);
                vfs.crash().unwrap();
                let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                assert_eq!(reopened.state.durable_sequence, suffix.sequence);
                assert_eq!(reopened.state.authority_state_root, Some(Some(suffix_id)));
                assert_eq!(reopened.read_block(prefix.block_ids[0]).unwrap(), b"prefix carried");
                assert_eq!(reopened.read_block(suffix.block_ids[0]).unwrap(), b"suffix carried");
                assert_eq!(reopened.transaction_at(suffix.sequence).unwrap().unwrap().envelope.inline_payload, b"during defer");
            }
        }
    }
