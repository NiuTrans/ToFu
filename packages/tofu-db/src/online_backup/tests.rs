//! Online-backup epoch tests. Fault-injection coverage of the shared
//! copy/manifest machinery lives in `backup.rs`'s own matrix; these tests
//! pin the *online* properties: anchoring under load, GC-anchor
//! interaction, incrementalism, retention, and doorbell receipts.

use super::*;
use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::authority_gc_online::OnlineGarbageCollector;
use crate::entity::EntityKey;
use crate::vfs::DeterministicVfs;

fn authority() -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    vfs.create_dir(Path::new("/backup")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, Arc::new(Mutex::new(database)))
}

fn context(vfs: &Arc<DeterministicVfs>) -> EpochContext {
    EpochContext {
        backup_dir: PathBuf::from("/backup"),
        destination_vfs: vfs.clone(),
        retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
        _fastpath_lease: None,
    }
}

fn commit_value(database: &Arc<Mutex<AuthorityDatabase>>, key: &[u8], value: &[u8]) {
    let mut database = database.lock().unwrap();
    let key = EntityKey::new(7, 11, "record", key).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key, value.to_vec())
        .unwrap();
    database.commit(transaction).unwrap();
}

fn run(driver: &Arc<Mutex<AuthorityDatabase>>, context: &EpochContext) -> OnlineBackupEpochReport {
    let stop = AtomicBool::new(false);
    run_epoch(driver, context, &stop).unwrap()
}

/// Seed one atomic commit of `count` small values under the generous
/// default budget (no seed commit trips the commit-path trigger), leaving
/// its whole delta pending in the write overlay.
fn seed_overlay_commit(database: &Arc<Mutex<AuthorityDatabase>>, prefix: &str, count: u32) {
    let mut guard = database.lock().unwrap();
    let mut transaction = guard.begin(7, 11).unwrap();
    for index in 0..count {
        guard
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "record", format!("{prefix}-{index}").as_bytes()).unwrap(),
                vec![7_u8; 24],
            )
            .unwrap();
    }
    guard.commit(transaction).unwrap();
}

#[test]
fn anchor_drains_the_overlay_in_bounded_chunks_before_capturing() {
    // 2026-09-21 FUSE brownout regression: the anchor used to run the full
    // overlay merge inside ONE authority hold. The epoch now drains the
    // backlog in bounded chunks first, so the final anchor hold is short.
    let (_vfs, database) = authority();
    {
        let mut guard = database.lock().unwrap();
        guard.set_write_overlay_for_test(true);
    }
    // Two atomic commits: the drain's chunk window merges the oldest
    // sequence first (commit A's span), then commit B's.
    seed_overlay_commit(&database, "a", 2_500);
    seed_overlay_commit(&database, "b", 2_500);
    {
        let mut guard = database.lock().unwrap();
        // Any backlog beats the soft half of a tiny budget.
        guard.set_overlay_budget_for_test(2_048);
        assert!(
            !guard
                .entity_overlay_soft_trigger()
                .below_soft_thresholds(),
            "seeded backlog must sit above the soft thresholds"
        );
    }
    let stop = AtomicBool::new(false);
    drain_overlay_before_anchor(&database, &stop).unwrap();
    let guard = database.lock().unwrap();
    assert!(
        guard
            .entity_overlay_soft_trigger()
            .below_soft_thresholds(),
        "the pre-anchor drain must leave the overlay below soft thresholds"
    );
}

#[test]
fn anchor_drain_defers_when_its_chunk_budget_is_exceeded() {
    let (_vfs, database) = authority();
    {
        let mut guard = database.lock().unwrap();
        guard.set_write_overlay_for_test(true);
    }
    // Two atomic commits, 5,000 pending entries total. One chunk covers the
    // 2,048 oldest chain positions rounded up to commit A's span; commit
    // B's newer entries stay pending above the soft thresholds.
    seed_overlay_commit(&database, "a", 2_500);
    seed_overlay_commit(&database, "b", 2_500);
    {
        let mut guard = database.lock().unwrap();
        guard.set_overlay_budget_for_test(2_048);
    }
    let stop = AtomicBool::new(false);
    let error = drain_overlay_before_anchor_bounded(&database, &stop, 1).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    {
        let guard = database.lock().unwrap();
        assert!(
            !guard.entity_overlay_soft_trigger().below_soft_thresholds(),
            "the chunk bound must stop the drain mid-backlog"
        );
    }
    // The unbounded drain completes the same backlog.
    drain_overlay_before_anchor(&database, &stop).unwrap();
    let guard = database.lock().unwrap();
    assert!(guard.entity_overlay_soft_trigger().below_soft_thresholds());
}

#[test]
fn epoch_publishes_a_restorable_manifest_and_reports_the_receipt_shape() {
    let (_vfs, database) = authority();
    commit_value(&database, b"alpha", b"value-alpha");
    let context = context(&_vfs);
    let report = run(&database, &context);
    assert!(report.ok);
    assert_eq!(report.backend, "tofudb");
    assert_eq!(report.artifact_kind, "directory");
    assert!(!report.backup.is_empty());
    assert!(!report.manifest.is_empty());
    assert!(report.bytes >= report.manifest_bytes);
    assert_eq!(report.sha256.len(), 64);
    assert!(report.durable_sequence >= 1);
    assert!(report.copied_blocks >= 1);
    // The destination BlockStore holds the anchor-reachable blocks: a
    // second authority restores from it without the source.
    let destination_blocks = report.copied_block_bytes;
    assert!(destination_blocks > 0);
    // Restore through the certified offline path against the same vfs.
    _vfs.create_dir(Path::new("/restore")).unwrap();
    let manifest = crate::backup::restore_latest_backup_with_vfs(
        Path::new("/backup"),
        Path::new("/restore"),
        _vfs.clone(),
    )
    .unwrap();
    assert_eq!(manifest.durable_sequence, report.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), _vfs.clone()).unwrap();
    let key = EntityKey::new(7, 11, "record", b"alpha").unwrap();
    let mut transaction = restored.begin(7, 11).unwrap();
    assert_eq!(
        restored.entity_get(&mut transaction, &key).unwrap(),
        Some(b"value-alpha".to_vec())
    );
}

#[test]
fn second_epoch_copies_only_new_blocks() {
    let (_vfs, database) = authority();
    for index in 0..400_u32 {
        commit_value(
            &database,
            format!("bulk-{index}").as_bytes(),
            format!("value-bulk-{index}-{}", "x".repeat(80)).as_bytes(),
        );
    }
    let context = context(&_vfs);
    let first = run(&database, &context);
    commit_value(&database, b"two", b"value-two");
    let second = run(&database, &context);
    assert!(second.durable_sequence > first.durable_sequence);
    assert!(
        second.copied_blocks < first.copied_blocks,
        "incremental epoch copies only the delta: first={} second={}",
        first.copied_blocks,
        second.copied_blocks
    );
    assert!(second.copied_blocks >= 1);
}

#[test]
fn registered_anchor_protects_anchor_history_from_online_gc() {
    let (_vfs, database) = authority();
    commit_value(&database, b"anchored", b"value-anchored");
    let anchor = {
        let mut database_guard = database.lock().unwrap();
        database_guard.capture_backup_anchor().unwrap()
    };
    let history_blocks = anchor.manifest.history_manifest_block_id;
    assert!(history_blocks.is_some());
    // Advance the authority and retire the anchored history segment.
    commit_value(&database, b"anchored", b"value-rewritten");
    {
        let mut database_guard = database.lock().unwrap();
        database_guard.checkpoint_for_test().unwrap();
        database_guard.compact_history(1).unwrap();
    }
    // Two full GC epochs would ordinarily confirm and collect the retired
    // segment's blocks; the registered anchor keeps them marked.
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        AuthorityGarbageCollectionBudget::conservative(),
        Duration::from_millis(1),
    );
    let stop = AtomicBool::new(false);
    collector.run_epoch(&database, &stop).unwrap();
    collector.run_epoch(&database, &stop).unwrap();
    let anchored_history = history_blocks.unwrap();
    {
        let database_guard = database.lock().unwrap();
        assert!(
            database_guard.read_block_for_test(anchored_history).is_ok(),
            "anchor-referenced history manifest must survive online GC"
        );
    }
    // The off-mutex copy resolves every anchored reference.
    let context = context(&_vfs);
    let (destination, _) =
        crate::backup::initialize_or_open_backup(&context.backup_dir, _vfs.clone()).unwrap();
    crate::backup::copy_reachable_blocks(&anchor.source, &destination, &anchor.manifest).unwrap();
    {
        let database_guard = database.lock().unwrap();
        database_guard.deregister_backup_anchor(anchor.anchor_token);
    }
}

#[test]
fn epochs_prune_to_the_retained_generation_count() {
    let (_vfs, database) = authority();
    let context = context(&_vfs);
    for value in [b"a", b"b", b"c"] {
        commit_value(&database, b"rolling", value);
        run(&database, &context);
    }
    let manifests =
        crate::backup::existing_manifests(Path::new("/backup/snapshots"), _vfs.as_ref()).unwrap();
    assert_eq!(
        manifests.len(),
        ONLINE_BACKUP_RETAINED_GENERATIONS,
        "retention keeps exactly the newest verified generations"
    );
}

#[test]
fn conflicting_backup_lease_defers_the_second_epoch() {
    let (_vfs, database) = authority();
    commit_value(&database, b"lease", b"value-lease");
    let context = context(&_vfs);
    // Establish a valid backup layout first; an empty directory with only a
    // lease file is (correctly) rejected as an invalid backup target.
    run(&database, &context);
    let _lease = crate::backup::acquire_backup_lease(Path::new("/backup"), _vfs.as_ref()).unwrap();
    let stop = AtomicBool::new(false);
    let error = run_epoch(&database, &context, &stop).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
}

#[test]
fn driver_answers_a_doorbell_with_a_receipt() {
    let (_vfs, database) = authority();
    commit_value(&database, b"doorbell", b"value-doorbell");
    let driver = OnlineBackupDriver::start_with_destination_vfs(
        &database,
        PathBuf::from("/backup"),
        _vfs.clone(),
    )
    .unwrap();
    let observer = driver.observer();
    let deadline = now_unix_ms() + 30_000;
    let report = observer.run_and_wait(deadline).unwrap();
    assert!(report.ok);
    assert!(observer.metrics().epochs_completed >= 1);
    assert!(observer.require_healthy().is_ok());
    driver.stop_and_join().unwrap();
}

struct GatedDestination {
    armed: AtomicBool,
    fail_writes: AtomicBool,
    entered: std::sync::mpsc::SyncSender<()>,
    released: Mutex<bool>,
    signal: Condvar,
}

impl GatedDestination {
    fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.signal.notify_all();
    }
}

impl Vfs for GatedDestination {
    fn open(
        &self,
        path: &Path,
        request: crate::vfs::OpenRequest,
    ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        if request.write && self.fail_writes.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "injected shadow write failure",
            ));
        }
        if request.write && self.armed.swap(false, Ordering::AcqRel) {
            self.entered
                .send(())
                .map_err(|_| io::Error::other("gate receiver closed"))?;
            let mut released = self.released.lock().unwrap();
            while !*released {
                released = self.signal.wait(released).unwrap();
            }
        }
        crate::vfs::RealVfs.open(path, request)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.create_dir(path)
    }
    fn rename(&self, a: &Path, b: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.rename(a, b)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> {
        crate::vfs::RealVfs.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        crate::vfs::RealVfs.read_directory(path)
    }
    fn read_directory_bounded(
        &self,
        path: &Path,
        max: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        crate::vfs::RealVfs.read_directory_bounded(path, max)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        scanned: usize,
        max: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, scanned, max)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.sync_directory(path)
    }
}

fn native_fastpath() -> (
    tempfile::TempDir,
    crate::fastpath::FastpathConfig,
    Arc<crate::fastpath::FastpathLease>,
    Arc<Mutex<AuthorityDatabase>>,
) {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    let local = root.path().join("local");
    std::fs::create_dir(&source).unwrap();
    std::fs::create_dir(&local).unwrap();
    let database = Arc::new(Mutex::new(AuthorityDatabase::initialize(&source).unwrap()));
    commit_value(&database, b"seed", b"seed-value");
    {
        let mut guard = database.lock().unwrap();
        let anchor = guard.capture_backup_anchor().unwrap();
        guard.deregister_backup_anchor(anchor.anchor_token);
    }
    drop(database);
    let config = crate::fastpath::FastpathConfig::new(&source, &local, Some(1000)).unwrap();
    let lease = config.prepare_for_test(None).unwrap();
    let database = Arc::new(Mutex::new(
        AuthorityDatabase::open(&config.local_authority).unwrap(),
    ));
    (root, config, lease, database)
}

#[test]
fn native_shadow_slow_io_allows_foreground_commits_and_retains_the_source_lease() {
    let (_root, config, lease, database) = native_fastpath();
    commit_value(&database, b"before-copy", b"before-copy-value");
    let (entered, reached) = std::sync::mpsc::sync_channel(1);
    let gate = Arc::new(GatedDestination {
        armed: AtomicBool::new(true),
        fail_writes: AtomicBool::new(false),
        entered,
        released: Mutex::new(false),
        signal: Condvar::new(),
    });
    let driver = OnlineBackupDriver::start_scheduled(
        &database,
        config.shadow_directory.clone(),
        gate.clone(),
        Some(lease),
    )
    .unwrap();
    let observer = driver.observer();
    let requested = std::thread::spawn(move || observer.run_and_wait(now_unix_ms() + 30_000));
    let reached_result = reached.recv_timeout(Duration::from_secs(10));
    if reached_result.is_err() {
        gate.release();
    }
    assert!(reached_result.is_ok());
    // The driver owns the only remaining source-lease reference while its
    // destination I/O is blocked outside authority.
    assert!(crate::engine::acquire_lease(&config.durable_authority, &crate::vfs::RealVfs).is_err());
    let writer_database = database.clone();
    let (finished, completion) = std::sync::mpsc::sync_channel(1);
    let writer = std::thread::spawn(move || {
        commit_value(&writer_database, b"during-copy", b"concurrent-value");
        finished.send(()).unwrap();
    });
    let completed = completion.recv_timeout(Duration::from_secs(10));
    gate.release();
    writer.join().unwrap();
    let first = requested.join().unwrap().unwrap();
    assert!(
        completed.is_ok(),
        "a blocked durable shadow must not hold the foreground writer"
    );
    let current = database
        .lock()
        .unwrap()
        .writer_engine()
        .unwrap()
        .state()
        .durable_sequence;
    assert!(
        first.durable_sequence < current,
        "the captured snapshot must exclude the concurrent suffix"
    );
    let second = driver
        .observer()
        .run_and_wait(now_unix_ms() + 30_000)
        .unwrap();
    assert!(second.durable_sequence >= current);
    driver.stop_and_join().unwrap();
    assert!(crate::engine::acquire_lease(&config.durable_authority, &crate::vfs::RealVfs).is_ok());
    let restored = _root.path().join("restored");
    backup::restore_latest_backup(&config.shadow_directory, &restored).unwrap();
    let mut restored = AuthorityDatabase::open(&restored).unwrap();
    let mut read = restored.begin(7, 11).unwrap();
    let key = EntityKey::new(7, 11, "record", b"during-copy").unwrap();
    assert_eq!(
        restored.entity_get(&mut read, &key).unwrap().unwrap(),
        b"concurrent-value"
    );
}

#[test]
fn native_shadow_idle_ticks_do_not_build_unchanged_generations() {
    let (_root, config, lease, database) = native_fastpath();
    let driver =
        OnlineBackupDriver::start_fastpath(&database, config.shadow_directory, None, lease)
            .unwrap();
    std::thread::sleep(Duration::from_millis(2250));
    let metrics = driver.observer().metrics();
    driver.stop_and_join().unwrap();
    assert_eq!(metrics.epochs_completed, 0);
    assert_eq!(metrics.epochs_failed, 0);
}

#[test]
fn native_shadow_failures_back_off_without_losing_foreground_data() {
    let (_root, config, lease, database) = native_fastpath();
    commit_value(&database, b"pending", b"still-local");
    let (entered, _receiver) = std::sync::mpsc::sync_channel(1);
    let destination = Arc::new(GatedDestination {
        armed: AtomicBool::new(false),
        fail_writes: AtomicBool::new(true),
        entered,
        released: Mutex::new(true),
        signal: Condvar::new(),
    });
    let driver = OnlineBackupDriver::start_scheduled(
        &database,
        config.shadow_directory.clone(),
        destination,
        Some(lease),
    )
    .unwrap();
    std::thread::sleep(Duration::from_millis(2250));
    commit_value(&database, b"after-failure", b"preserved-local");
    let metrics = driver.observer().metrics();
    driver.stop_and_join().unwrap();
    assert!(
        (1..=3).contains(&metrics.epochs_failed),
        "failed replication must not spin"
    );
    assert!(metrics.last_error.is_some());
    let mut guard = database.lock().unwrap();
    let mut read = guard.begin(7, 11).unwrap();
    let key = EntityKey::new(7, 11, "record", b"after-failure").unwrap();
    assert_eq!(
        guard.entity_get(&mut read, &key).unwrap().unwrap(),
        b"preserved-local"
    );
}

#[test]
fn native_shadow_doorbell_reuses_an_unchanged_restored_generation() {
    let (_root, config, lease, database) = native_fastpath();
    let driver = OnlineBackupDriver::start_fastpath(&database,config.shadow_directory,None,lease).unwrap();
    let report = driver.observer().run_and_wait(now_unix_ms()+30_000).unwrap();
    driver.stop_and_join().unwrap();
    assert_eq!(report.copied_blocks,0);
    assert_eq!(report.manifest_bytes,0);
    assert!(report.ok);
}


#[test]
fn epoch_resumes_interrupted_prune_before_copying_latest_state() {
    use crate::vfs::{FaultAction, FaultRule};
    fn seed() -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>, EpochContext) {
        let (vfs, database) = authority();
        let mut context = context(&vfs); context.retention = 4;
        for value in [b"one", b"two", b"new"] {
            commit_value(&database, b"rolling", value);
            run(&database, &context);
        }
        (vfs, database, context)
    }
    let (baseline, _, _) = seed();
    baseline.arm_fault(None).unwrap();
    crate::backup::prune_backup_with_vfs(Path::new("/backup"), 1, baseline.clone()).unwrap();
    let operations = baseline.trace().unwrap().len();
    let mut exercised = false;
    for operation_number in 1..=operations as u64 {
        let (vfs, database, mut context) = seed();
        vfs.arm_fault(Some(FaultRule { operation_number,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted) })).unwrap();
        let failed = crate::backup::prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone());
        vfs.arm_fault(None).unwrap();
        if failed.is_err() && crate::backup::ensure_no_prune(Path::new("/backup"), vfs.as_ref())
            .is_err_and(|error| error.kind() == io::ErrorKind::WouldBlock) {
            commit_value(&database, b"rolling", b"latest");
            context.retention = 2; // the pending plan still owns its old retention set
            let report = run(&database, &context);
            assert!(report.ok);
            crate::backup::ensure_no_prune(Path::new("/backup"), vfs.as_ref()).unwrap();
            vfs.create_dir(Path::new("/restore")).unwrap();
            crate::backup::restore_latest_backup_with_vfs(Path::new("/backup"),
                Path::new("/restore"), vfs.clone()).unwrap();
            let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs).unwrap();
            let mut tx = restored.begin(7, 11).unwrap();
            assert_eq!(restored.entity_get(&mut tx, &EntityKey::new(7, 11,
                "record", b"rolling").unwrap()).unwrap().unwrap(), b"latest");
            exercised = true;
            break;
        }
    }
    assert!(exercised, "fault injection must leave a committed pending prune plan");
}
