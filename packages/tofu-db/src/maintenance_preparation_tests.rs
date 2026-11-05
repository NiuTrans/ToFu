// Executable specifications for maintenance preparation outside the writer.
// Included in maintenance::tests to reuse the deterministic physical-read gate.
mod preparation {
    use super::*;
    use crate::vfs::Vfs;
    use std::path::Path;

    fn seed_expired_tool(db: &mut AuthorityDatabase, owner: u64) -> String {
        let mut tx = db.begin(7, owner).unwrap();
        let response =
            crate::artifact::tool_put(db, &mut tx, "expired", "text/plain", 1, 2).unwrap();
        db.commit(tx).unwrap();
        serde_json::from_slice::<serde_json::Value>(&response).unwrap()["artifactRef"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    fn prepare_tool_prune(
        db: &AuthorityDatabase,
    ) -> io::Result<(Option<crate::authority::AuthorityTransaction>, usize)> {
        let mut tx = db.begin(7, 11)?;
        let progress = crate::artifact::tool_prune(db, &mut tx, 3, 10)?;
        Ok(((progress.deleted > 0).then_some(tx), progress.deleted))
    }

    #[test]
    fn prepared_prune_refuses_concurrent_renewal_and_preserves_other_owner() {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        db.set_write_overlay_for_test(true);
        seed_expired_tool(&mut db, 11);
        seed_expired_tool(&mut db, 12);
        let authority = Arc::new(Mutex::new(db));
        let error = prepared::run_transaction(&authority, |replica| {
            let prepared = prepare_tool_prune(replica)?;
            assert_eq!(prepared.1, 1);
            let mut writer = authority.try_lock().expect("preparation holds the writer");
            let mut tx = writer.begin(7, 11)?;
            crate::artifact::tool_put(&writer, &mut tx, "expired", "text/plain", 100, 200)?;
            writer.commit(tx)?;
            Ok(prepared)
        })
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert_eq!(
            authority
                .lock()
                .unwrap()
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
        assert_eq!(
            prepared::run_transaction(&authority, prepare_tool_prune).unwrap(),
            (0, false)
        );
        drop(authority);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        for (owner, now) in [(11, 201), (12, 3)] {
            let mut tx = db.begin(7, owner).unwrap();
            assert_eq!(
                crate::artifact::tool_prune(&db, &mut tx, now, 10)
                    .unwrap()
                    .deleted,
                1
            );
        }
    }

    #[test]
    fn prepared_mount_refuses_replacing_a_concurrent_foreground_value() {
        let (directory, authority) = authority_with_mount();
        let error = prepared::run_transaction(&authority, |replica| {
            let mut tx = replica.begin(7, 11)?;
            let progress = replica
                .consolidate_one_entity_range_mount(&mut tx)?
                .unwrap();
            assert_eq!(progress.rows_materialized, 4);
            let mut writer = authority
                .try_lock()
                .expect("mount source reads hold the writer");
            let mut foreground = writer.begin(7, 11)?;
            writer.entity_put(&mut foreground, key(1), b"new value".to_vec())?;
            writer.commit(foreground)?;
            Ok((Some(tx), ()))
        })
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert!(matches!(
            run_round(&authority, MaintenanceScope::new(7, 11).unwrap(), 64),
            RoundOutcome::Committed {
                mount_completed: true,
                ..
            }
        ));
        drop(authority);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = db.begin(7, 11).unwrap();
        assert_eq!(
            db.entity_get(&mut read, &key(1)).unwrap(),
            Some(b"new value".to_vec())
        );
    }

    #[test]
    fn busy_commit_discards_preparation_without_losing_pending_prune() {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_expired_tool(&mut db, 11);
        let authority = Arc::new(Mutex::new(db));
        let mut foreground_hold = None;
        let error = prepared::run_transaction(&authority, |replica| {
            let staged = prepare_tool_prune(replica)?;
            foreground_hold = Some(authority.try_lock().unwrap());
            Ok(staged)
        })
        .unwrap_err();
        drop(foreground_hold);
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert_eq!(
            authority
                .lock()
                .unwrap()
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
        assert_eq!(
            prepared::run_transaction(&authority, prepare_tool_prune).unwrap(),
            (1, true)
        );
        assert_eq!(
            prepared::run_transaction(&authority, prepare_tool_prune).unwrap(),
            (0, false)
        );
    }

    #[test]
    fn prepared_prune_commit_faults_preserve_document_and_expiry_index_together() {
        use crate::vfs::{DeterministicVfs, FaultAction, Operation};
        let seed = Arc::new(DeterministicVfs::new(None));
        seed.create_dir(Path::new("/data")).unwrap();
        seed.sync_directory(Path::new("/")).unwrap();
        let mut db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), seed.clone()).unwrap();
        db.set_write_overlay_for_test(true);
        let artifact_ref = seed_expired_tool(&mut db, 11);
        db.merge_entity_overlay_fully().unwrap();
        db.checkpoint_wal_window().unwrap();
        drop(db);
        let open = |vfs: &Arc<DeterministicVfs>| {
            let mut db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            db.set_write_overlay_for_test(true);
            Arc::new(Mutex::new(db))
        };
        let baseline_vfs = Arc::new(seed.fork_durable().unwrap());
        let baseline = open(&baseline_vfs);
        baseline_vfs.arm_fault(None).unwrap();
        assert_eq!(
            prepared::run_transaction(&baseline, prepare_tool_prune).unwrap(),
            (1, true)
        );
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        let mut cells = 0;
        for (index, operation) in trace.iter().enumerate() {
            let occurrence = trace[..=index]
                .iter()
                .filter(|previous| *previous == operation)
                .count();
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if operation == &Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            if matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let authority = open(&vfs);
                vfs.arm_operation_fault(operation.clone(), occurrence, action)
                    .unwrap();
                let result = prepared::run_transaction(&authority, prepare_tool_prune);
                assert!(
                    vfs.operation_fault_fired().unwrap(),
                    "unexercised fault: {operation:?}/{occurrence}/{action:?}"
                );
                // Freeze only durable bytes before clean Drop can flush any
                // failed/unsynced tail. The dead process's later Drop cannot
                // repair this independent crash image.
                let crashed = Arc::new(vfs.fork_durable().unwrap());
                drop(authority);
                let recovered = open(&crashed);
                let present = {
                    let db = recovered.lock().unwrap();
                    let mut read = db.begin(7, 11).unwrap();
                    crate::artifact::tool_read(
                        &db,
                        &mut read,
                        &artifact_ref,
                        1,
                        &serde_json::json!(0),
                        64,
                        "",
                    )
                    .unwrap()
                    .is_some()
                };
                let pending = prepared::run_transaction(&recovered, prepare_tool_prune)
                    .unwrap()
                    .0;
                assert_eq!(
                    pending,
                    usize::from(present),
                    "document and expiry index diverged"
                );
                if result.is_ok() {
                    assert_eq!(
                        pending, 0,
                        "acknowledged prune lost after {operation:?}/{occurrence}/{action:?}"
                    );
                }
                assert!(pending <= 1);
                assert_eq!(
                    prepared::run_transaction(&recovered, prepare_tool_prune).unwrap(),
                    (0, false)
                );
                cells += 1;
            }
        }
        eprintln!("prepared maintenance fault cells: {cells}");
    }

    // Real filesystem variant of the same physical-read gate. The ordinary
    // Strict writer is used on both arms; this is a component measurement,
    // not the daemon/embedded buffered-WAL pipeline or an application SLO.
    struct GatedRealVfs(Arc<BlockingMergeProbe>);
    impl Vfs for GatedRealVfs {
        fn open(
            &self,
            path: &Path,
            request: crate::vfs::OpenRequest,
        ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
            Ok(Box::new(BlockingMergeReadFile {
                inner: crate::vfs::RealVfs.open(path, request)?,
                probe: self.0.clone(),
            }))
        }
        fn create_dir(&self, path: &Path) -> io::Result<()> {
            crate::vfs::RealVfs.create_dir(path)
        }
        fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
            crate::vfs::RealVfs.rename(source, destination)
        }
        fn remove_file(&self, path: &Path) -> io::Result<()> {
            crate::vfs::RealVfs.remove_file(path)
        }
        fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> {
            crate::vfs::RealVfs.metadata(path)
        }
        fn read_directory(&self, path: &Path) -> io::Result<Vec<std::path::PathBuf>> {
            crate::vfs::RealVfs.read_directory(path)
        }
        fn read_directory_bounded(
            &self,
            path: &Path,
            maximum: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
            crate::vfs::RealVfs.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(
            &self,
            path: &Path,
            prefix: &str,
            scanned: usize,
            matched: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
            crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, scanned, matched)
        }
        fn sync_directory(&self, path: &Path) -> io::Result<()> {
            crate::vfs::RealVfs.sync_directory(path)
        }
    }

    #[test]
    #[ignore = "paired filesystem measurement; requires an explicit disposable parent and output"]
    fn maintenance_and_foreground_writes_measurement() {
        let parent = std::env::var("TOFU_DB_MAINTENANCE_MEASURE_ROOT").unwrap();
        let output = std::env::var("TOFU_DB_MAINTENANCE_MEASURE_OUTPUT").unwrap();
        let directory = tempfile::tempdir_in(&parent).unwrap();
        let (entered_sender, entered_receiver) = std::sync::mpsc::channel();
        let (release_sender, release_receiver) = std::sync::mpsc::channel();
        let probe = Arc::new(BlockingMergeProbe {
            armed: std::sync::atomic::AtomicBool::new(false),
            entered: entered_sender,
            release: Mutex::new(release_receiver),
        });
        let vfs = Arc::new(GatedRealVfs(probe.clone()));
        let mut db = AuthorityDatabase::initialize_with_vfs(directory.path(), vfs.clone()).unwrap();
        db.set_write_overlay_for_test(true);
        db.set_entity_tombstone_reclaim_override_for_test(Some(false));
        let database = Arc::new(Mutex::new(db));
        let mut latencies = Vec::new();
        let mut committed_during_pause = Vec::new();
        let mut pause_micros = Vec::new();
        let mut followup_rounds = 0;
        let mut maintenance_micros = Vec::new();
        for round in 0..8_u32 {
            {
                let mut db = database.lock().unwrap();
                let mut tx = db.begin(7, 11).unwrap();
                crate::artifact::tool_put(&db, &mut tx, "expired", "text/plain", 1, 2).unwrap();
                db.commit(tx).unwrap();
                db.merge_entity_overlay_fully().unwrap();
                db.checkpoint_wal_window().unwrap();
                db.configure_hot_data_caches(0, 0).unwrap();
            }
            probe
                .armed
                .store(true, std::sync::atomic::Ordering::Release);
            let maintenance_started = Instant::now();
            let worker_database = database.clone();
            let worker = thread::spawn(move || {
                run_round(&worker_database, MaintenanceScope::new(7, 11).unwrap(), 64)
            });
            let entered = entered_receiver.recv_timeout(Duration::from_secs(30));
            let paused_at = Instant::now();
            let completed = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            let foreground_database = database.clone();
            let foreground_completed = completed.clone();
            let foreground = thread::spawn(move || {
                let mut samples = Vec::new();
                for index in round * 32..(round + 1) * 32 {
                    let start = Instant::now();
                    put_overlay_record(&mut foreground_database.lock().unwrap(), index);
                    samples.push(start.elapsed().as_micros() as u64);
                    foreground_completed.fetch_add(1, std::sync::atomic::Ordering::Release);
                }
                samples
            });
            thread::sleep(Duration::from_millis(100));
            committed_during_pause.push(completed.load(std::sync::atomic::Ordering::Acquire));
            pause_micros.push(paused_at.elapsed().as_micros() as u64);
            release_sender.send(()).unwrap();
            let mut outcome = worker.join().unwrap();
            latencies.extend(foreground.join().unwrap());
            assert!(
                entered.is_ok(),
                "measurement did not reach a physical source read"
            );
            // A busy commit legitimately defers its prepared transaction.
            // Join foreground work, then finish through the same scheduler
            // round owner. Include this catch-up time and extra rounds in
            // the report; never count a discarded preparation as a deletion.
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                match outcome {
                    RoundOutcome::Committed {
                        tool_result_artifacts_pruned: 1,
                        ..
                    } => break,
                    RoundOutcome::Terminal(error) => panic!("maintenance failed: {error}"),
                    RoundOutcome::NoWork => {
                        panic!("expired tool result vanished without committed progress")
                    }
                    _ => {}
                }
                assert!(
                    Instant::now() < deadline,
                    "maintenance failed to catch up after foreground joined"
                );
                followup_rounds += 1;
                outcome = run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64);
            }
            maintenance_micros.push(maintenance_started.elapsed().as_micros() as u64);
        }
        // Measure the extra preparation/pin cost separately on warm idle rounds.
        {
            let mut db = database.lock().unwrap();
            db.merge_entity_overlay_fully().unwrap();
            db.checkpoint_wal_window().unwrap();
            db.configure_hot_data_caches(1024 * 1024, 1024 * 1024)
                .unwrap();
        }
        let scope = MaintenanceScope::new(7, 11).unwrap();
        run_round(&database, scope, 64);
        let start = Instant::now();
        for _ in 0..100 {
            assert!(matches!(
                run_round(&database, scope, 64),
                RoundOutcome::NoWork
            ));
        }
        let idle_micros = start.elapsed().as_micros() as u64;
        assert_eq!(
            database
                .lock()
                .unwrap()
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
        drop(database);
        let db = AuthorityDatabase::open_with_vfs(directory.path(), vfs).unwrap();
        for index in 0..256_u32 {
            assert_eq!(
                get_overlay_record(&db, index),
                Some(index.to_be_bytes().to_vec())
            );
        }
        let mut tx = db.begin(7, 11).unwrap();
        assert_eq!(
            crate::artifact::tool_prune(&db, &mut tx, 3, 10)
                .unwrap()
                .deleted,
            0
        );
        let report = serde_json::json!({
            "source_sha256": env!("TOFUDB_SOURCE_SHA256"), "parent": parent,
            "scope": "Strict authority component; eight controlled cold source pauses; profile off; not application P99",
            "foreground_commit_micros": latencies, "cold_read_pause_micros": pause_micros,
            "committed_during_pause": committed_during_pause,
            "maintenance_followup_rounds": followup_rounds,
            "maintenance_through_catchup_micros": maintenance_micros,
            "warm_idle_100_rounds_micros": idle_micros,
            "exact_foreground_records_recovered": 256, "expired_tool_results_pruned": 8,
        });
        std::fs::write(output, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    }

    #[test]
    fn stalled_maintenance_source_read_allows_foreground_commit() {
        let (entered_sender, entered_receiver) = std::sync::mpsc::channel();
        let (release_sender, release_receiver) = std::sync::mpsc::channel();
        let vfs = Arc::new(BlockingMergeReadVfs {
            inner: Arc::new(crate::vfs::DeterministicVfs::new(None)),
            probe: Arc::new(BlockingMergeProbe {
                armed: std::sync::atomic::AtomicBool::new(false),
                entered: entered_sender,
                release: Mutex::new(release_receiver),
            }),
        });
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        db.set_write_overlay_for_test(true);
        // The first deferred write after the merge republishes the root; with
        // TOFU_DB_TOMBSTONE_RECLAIM range validation conservatively conflicts.
        db.set_entity_tombstone_reclaim_override_for_test(Some(false));
        let mut seed = db.begin(7, 11).unwrap();
        crate::artifact::tool_put(&db, &mut seed, "expired", "text/plain", 1, 2).unwrap();
        db.commit(seed).unwrap();
        db.merge_entity_overlay_fully().unwrap();
        db.checkpoint_wal_window().unwrap();
        db.configure_hot_data_caches(0, 0).unwrap();
        let database = Arc::new(Mutex::new(db));
        vfs.probe
            .armed
            .store(true, std::sync::atomic::Ordering::Release);
        let worker_database = Arc::clone(&database);
        let worker = thread::spawn(move || {
            run_round(&worker_database, MaintenanceScope::new(7, 11).unwrap(), 64)
        });
        let entered = entered_receiver.recv_timeout(Duration::from_secs(5));
        // Always release and join before assertions, including the negative
        // baseline. A failed assertion must not strand a blocked worker.
        let foreground = entered.is_ok()
            && match database.try_lock() {
                Ok(mut db) => {
                    put_overlay_record(&mut db, 101);
                    true
                }
                Err(_) => false,
            };
        release_sender.send(()).unwrap();
        let outcome = worker.join().unwrap();
        assert!(
            entered.is_ok(),
            "maintenance never reached a cold source read"
        );
        assert!(
            foreground,
            "maintenance source I/O holds the foreground writer"
        );
        assert!(matches!(
            outcome,
            RoundOutcome::Committed {
                tool_result_artifacts_pruned: 1,
                ..
            }
        ));
        assert_eq!(
            database
                .lock()
                .unwrap()
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
        drop(database);
        vfs.inner.crash().unwrap();
        let db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            get_overlay_record(&db, 101),
            Some(101_u32.to_be_bytes().to_vec())
        );
        let mut tx = db.begin(7, 11).unwrap();
        assert_eq!(
            crate::artifact::tool_prune(&db, &mut tx, 3, 10)
                .unwrap()
                .deleted,
            0
        );
    }
}
