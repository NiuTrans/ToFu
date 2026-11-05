//! GC capture/source boundary specifications. Ordinary epoch/fault campaigns
//! remain in tests.rs/tests_fault.rs; same-volume SIGKILL controls are ignored.
use super::*;
use super::tests::{metrics, test_budget};
use crate::entity::{visit_entity_page_graph_with_values, EntityKey};
use crate::history::{HistoryManifest, HistorySegmentReference, MAX_HISTORY_SEGMENTS};
use crate::vfs::{BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs,
    FileKind, OpenRequest, RealVfs, VfsFile, VfsPositionedReader};
use std::sync::{mpsc, Condvar};

enum MetadataAction {
    Block(mpsc::Sender<()>, Arc<(Mutex<bool>, Condvar)>),
    Error(io::ErrorKind),
}
#[derive(Default)]
struct MetadataGate {
    path: Option<PathBuf>, action: Option<MetadataAction>, hits: usize,
}
struct SourceMetadataVfs {
    inner: Arc<dyn Vfs>, gate: Mutex<MetadataGate>, removed: Mutex<Vec<PathBuf>>, scanned: Mutex<Vec<PathBuf>>,
}
impl SourceMetadataVfs {
    fn new(inner: Arc<dyn Vfs>) -> Self {
        Self { inner, gate: Mutex::new(MetadataGate::default()), removed: Mutex::new(Vec::new()), scanned: Mutex::new(Vec::new()) }
    }
    fn arm(&self, path: PathBuf, action: Option<MetadataAction>) {
        *self.gate.lock().unwrap() = MetadataGate { path: Some(path), action, hits: 0 };
    }
    fn hits(&self) -> usize { self.gate.lock().unwrap().hits }
}
impl Vfs for SourceMetadataVfs {
    fn open(&self, p: &Path, r: OpenRequest) -> io::Result<Box<dyn VfsFile>> { self.inner.open(p, r) }
    fn open_positioned_reader(&self, p: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> { self.inner.open_positioned_reader(p) }
    fn create_dir(&self, p: &Path) -> io::Result<()> { self.inner.create_dir(p) }
    fn rename(&self, a: &Path, b: &Path) -> io::Result<()> { self.inner.rename(a, b) }
    fn remove_file(&self, p: &Path) -> io::Result<()> {
        self.removed.lock().unwrap().push(p.to_path_buf()); self.inner.remove_file(p)
    }
    fn metadata(&self, p: &Path) -> io::Result<FileKind> {
        // The actual metadata call is mandatory. Its delayed response still
        // precedes the ordinary full framing/CRC/hash/root decoding checks.
        let kind = self.inner.metadata(p)?;
        let action = {
            let mut gate = self.gate.lock().unwrap();
            if gate.path.as_deref() != Some(p) { return Ok(kind); }
            gate.hits += 1; gate.action.take()
        };
        match action {
            Some(MetadataAction::Error(kind)) => Err(io::Error::from(kind)),
            Some(MetadataAction::Block(entered, release)) => {
                entered.send(()).unwrap();
                let (lock, changed) = &*release;
                let (released, wait) = changed.wait_timeout_while(lock.lock().unwrap(),
                    Duration::from_secs(10), |released| !*released).unwrap();
                if wait.timed_out() && !*released { return Err(io::Error::from(io::ErrorKind::TimedOut)); }
                Ok(kind)
            }
            None => Ok(kind),
        }
    }
    fn read_directory(&self, p: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(p) }
    fn read_directory_entries(&self, p: &Path) -> io::Result<Vec<crate::vfs::DirectoryEntry>> {
        self.scanned.lock().unwrap().push(p.to_path_buf()); self.inner.read_directory_entries(p)
    }
    fn read_directory_bounded(&self, p: &Path, n: usize) -> io::Result<BoundedDirectoryEntries> {
        self.scanned.lock().unwrap().push(p.to_path_buf()); self.inner.read_directory_bounded(p, n)
    }
    fn match_directory_prefix_bounded(&self, p: &Path, s: &str, n: usize, m: usize) -> io::Result<BoundedDirectoryMatches> { self.inner.match_directory_prefix_bounded(p, s, n, m) }
    fn sync_directory(&self, p: &Path) -> io::Result<()> { self.inner.sync_directory(p) }
}

fn key() -> EntityKey { EntityKey::new(7, 11, "record", b"gc-capture-proof").unwrap() }
fn put(database: &mut AuthorityDatabase, value: &[u8]) {
    let mut tx = database.begin(7, 11).unwrap();
    database.entity_put(&mut tx, key(), value.to_vec()).unwrap();
    database.commit(tx).unwrap();
}
fn indexed_rotate(database: &mut AuthorityDatabase) {
    let engine = database.engine_mut_for_test();
    engine.attach_rotation_driver();
    let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
        crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
        other => panic!("expected indexed rotation, got {other:?}"),
    };
    let encoded = crate::engine::encode_rotation_segments(&prepared).unwrap();
    engine.finish_rotation_checkpoint(prepared, Ok(encoded)).unwrap();
}
fn history(database: &mut AuthorityDatabase) -> HistoryManifest {
    let engine = database.engine_mut_for_test();
    let id = engine.state().history_manifest_block_id.unwrap();
    HistoryManifest::load(id, |id| engine.block_store_shared().get(id)).unwrap()
}
fn block_path(base: &Path, id: BlockId) -> PathBuf {
    let name = id.to_hex(); base.join("blocks").join(&name[..2]).join(format!("{name}.blk"))
}
fn fixture(path: &Path, vfs: Arc<SourceMetadataVfs>) -> (Arc<Mutex<AuthorityDatabase>>, PathBuf) {
    let mut database = AuthorityDatabase::initialize_with_vfs(path, vfs.clone()).unwrap();
    database.set_write_overlay_for_test(false);
    put(&mut database, b"original-one"); indexed_rotate(&mut database);
    put(&mut database, b"original-two"); indexed_rotate(&mut database);
    let manifest = history(&mut database);
    let root = manifest.payload_index_root_id.expect("fixture must have indexed routing root");
    let source = block_path(path, root);
    assert_eq!(vfs.inner.metadata(&source).unwrap(), FileKind::File);
    database.block_store_shared().set_cache_capacity_bytes(0).unwrap();
    (Arc::new(Mutex::new(database)), source)
}
fn simulated_fixture() -> (Arc<DeterministicVfs>, Arc<SourceMetadataVfs>, Arc<Mutex<AuthorityDatabase>>, PathBuf) {
    let inner = Arc::new(DeterministicVfs::new(None));
    inner.create_dir(Path::new("/data")).unwrap(); inner.sync_directory(Path::new("/")).unwrap();
    let vfs = Arc::new(SourceMetadataVfs::new(inner.clone()));
    let (database, source) = fixture(Path::new("/data"), vfs.clone());
    (inner, vfs, database, source)
}
pub(super) fn unmarked_epoch(capture: OnlineGcCapture, collector: &OnlineGarbageCollector) -> Epoch {
    let suspects_directory = capture.data_dir.join(WORK_DIRECTORY).join(SUSPECTS_DIRECTORIES[collector.suspects_parity]);
    Epoch { started: Instant::now(), capture, marks: None, suspects_directory,
        suspects_next: None, temp_candidate_next: None,
        segment_dead_next: BTreeMap::new(), confirmed_loose: Vec::new(), unlinked_loose: Vec::new(),
        confirmed_temp: None,
        rewrite_victim: None, compaction_ids: Vec::new(), compaction_failure: None, copy_failure: None,
        confirmed_loose_planned: false,
        rewrite_planned: false, compaction_planned: false, tracked_suspects: 0,
        reclaimed_bytes: 0, reclaimed_blocks: 0, temporary_files_removed: 0,
        segments_rewritten: 0, segments_retired: 0,
        compacted_blocks: 0, compaction_segments: 0, redundant_copies_removed: 0, consolidations: 0,
        consolidated_victims: 0,
        consolidation: None, pressure_candidates: Vec::new() }
}

#[test]
fn gc_capture_is_pure_but_eager_snapshot_still_reaches_cold_source_fault() {
    let (inner, vfs, database, source) = simulated_fixture();
    vfs.arm(source, Some(MetadataAction::Error(io::ErrorKind::Interrupted)));
    inner.arm_fault(None).unwrap();
    let mut guard = database.lock().unwrap();
    let capture = guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    assert!(capture.snapshot.selected_history_metadata.is_some());
    assert!(capture.snapshot.history_payload_metadata_ids.is_empty());
    assert!(inner.trace().unwrap().is_empty()); assert_eq!(vfs.hits(), 0);
    assert_eq!(guard.engine_mut_for_test().capture_online_gc_mark_snapshot(Vec::new()).err().unwrap().kind(), io::ErrorKind::Interrupted);
    assert_eq!(vfs.hits(), 1);
}

#[test]
fn gc_cold_metadata_response_does_not_hold_foreground_and_reopen_is_exact() {
    let (inner, vfs, database, source) = simulated_fixture();
    let (entered, observed) = mpsc::channel();
    let release = Arc::new((Mutex::new(false), Condvar::new()));
    vfs.arm(source, Some(MetadataAction::Block(entered, release.clone())));
    let worker = {
        let database = database.clone();
        std::thread::spawn(move || OnlineGarbageCollector::new(test_budget()).run_epoch(&database, &AtomicBool::new(false)))
    };
    observed.recv_timeout(Duration::from_secs(5)).unwrap();
    {
        let mut guard = database.try_lock().expect("actual cold source metadata must not retain authority");
        put(&mut guard, b"foreground-durable-ack");
        // The selected old routing root is still delayed while foreground
        // publishes and prunes another manifest. Its metadata must survive.
        indexed_rotate(&mut guard);
        guard.compact_history(1).unwrap();
    }
    *release.0.lock().unwrap() = true; release.1.notify_all();
    assert_eq!(worker.join().unwrap().unwrap(), OnlineGarbageCollectionOutcome::EpochComplete);
    assert!(vfs.hits() >= 1);
    std::mem::forget(database); inner.crash().unwrap();
    let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
    let mut tx = reopened.begin(7, 11).unwrap();
    assert_eq!(reopened.entity_get(&mut tx, &key()).unwrap().unwrap(), b"foreground-durable-ack");
}

#[test]
fn gc_metadata_fault_after_partial_mark_never_deletes_confirmed_user_orphan() {
    let (_inner, vfs, database, source) = simulated_fixture();
    let orphan = database.lock().unwrap().write_orphan_block_for_test(b"confirmed-user-orphan").unwrap();
    let mut collector = OnlineGarbageCollector::new(test_budget());
    super::tests::run_epoch(&mut collector, &database);
    let before = metrics(&collector);
    vfs.removed.lock().unwrap().clear();
    vfs.arm(source, Some(MetadataAction::Error(io::ErrorKind::Interrupted)));
    assert_eq!(collector.run_epoch(&database, &AtomicBool::new(false)).unwrap_err().kind(), io::ErrorKind::Interrupted);
    assert_eq!(vfs.hits(), 1);
    let after = metrics(&collector);
    assert_eq!(after.epochs_completed, before.epochs_completed);
    assert!(after.deferred_epochs > before.deferred_epochs);
    assert_eq!(after.total_reclaimed_blocks, before.total_reclaimed_blocks);
    assert!(!vfs.removed.lock().unwrap().contains(&block_path(Path::new("/data"), orphan)));
    assert_eq!(database.lock().unwrap().read_block_for_test(orphan).unwrap(), b"confirmed-user-orphan");
    super::tests::run_epoch(&mut collector, &database);
}

#[test]
fn gc_metadata_full_body_corruption_aborts_without_durable_deletion() {
    let (_inner, vfs, database, source) = simulated_fixture();
    let orphan = database.lock().unwrap().write_orphan_block_for_test(b"corrupt-source-no-delete").unwrap();
    let mut collector = OnlineGarbageCollector::new(test_budget());
    super::tests::run_epoch(&mut collector, &database);
    let mut file = vfs.open(&source, OpenRequest { read: true, write: true, ..Default::default() }).unwrap();
    let length = file.len().unwrap() as usize;
    let encoded = file.read_all(length).unwrap();
    file.write_all_at(16, &[encoded[16] ^ 1]).unwrap();
    crate::vfs::sync_all_barrier(file.as_mut()).unwrap();
    vfs.arm(source, None); vfs.removed.lock().unwrap().clear();
    let capture = database.lock().unwrap().online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    assert_eq!(vfs.hits(), 0, "capture must not validate a current body under authority");
    drop(capture);
    assert_eq!(collector.run_epoch(&database, &AtomicBool::new(false)).unwrap_err().kind(), io::ErrorKind::InvalidData);
    assert!(vfs.hits() > 0, "the actual corrupt source must be reached off-authority");
    assert_eq!(metrics(&collector).epochs_completed, 1);
    assert_eq!(metrics(&collector).total_reclaimed_blocks, 0);
    assert!(!vfs.removed.lock().unwrap().contains(&block_path(Path::new("/data"), orphan)));
}

#[test]
fn gc_selected_old_metadata_survives_rotation_retention_and_old_root_pin() {
    let (_inner, _vfs, database, old_source) = simulated_fixture();
    let (capture, replica, old_metadata) = {
        let mut guard = database.lock().unwrap();
        let manifest = history(&mut guard);
        let old_metadata = guard.block_store_shared().history_payload_metadata_ids(&manifest).unwrap();
        let replica = guard.build_read_replica().unwrap();
        let capture = guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
        put(&mut guard, b"replacement-after-capture"); indexed_rotate(&mut guard);
        guard.compact_history(1).unwrap();
        assert_ne!(block_path(Path::new("/data"), history(&mut guard).payload_index_root_id.unwrap()), old_source);
        (capture, replica, old_metadata)
    };
    let collector = OnlineGarbageCollector::new(test_budget());
    let mut epoch = unmarked_epoch(capture, &collector);
    collector.build_marks(&mut epoch, &AtomicBool::new(false)).unwrap();
    let mut retained = old_metadata.clone(); retained.sort(); retained.dedup();
    epoch.marks.as_ref().unwrap().retain_live_ids(&mut retained).unwrap();
    retained.sort(); let mut expected = old_metadata; expected.sort(); expected.dedup();
    assert_eq!(retained, expected, "captured metadata must not silently follow the replacement root");
    let mut tx = replica.begin(7, 11).unwrap();
    assert_eq!(replica.entity_get(&mut tx, &key()).unwrap().unwrap(), b"original-two");
    collector.cleanup_epoch(&mut epoch);
}

#[test]
fn gc_metadata_selection_capacity_is_charged_and_admitted_before_allocating() {
    eprintln!("GC capture tested source {}", env!("TOFUDB_SOURCE_SHA256"));
    let reference = HistorySegmentReference { block_id: BlockId([1; 32]), payload_index_block_id: Some(BlockId([2; 32])),
        first_sequence: 1, last_sequence: 1, parent_hash: [0; 32], terminal_hash: [0; 32] };
    for count in [1, 32, MAX_HISTORY_SEGMENTS] {
        let manifest = HistoryManifest { manifest_page_ids: Vec::new(), checkpoint_sequence: 1, checkpoint_hash: [0; 32],
            payload_index_root_id: Some(BlockId([3; 32])), segments: vec![reference.clone(); count] };
        let selection = crate::block::HistoryPayloadMetadataSelection::capture(&manifest, test_budget().maximum_temporary_bytes).unwrap();
        let bytes = selection.resident_bytes();
        assert!(bytes <= test_budget().maximum_temporary_bytes);
        assert!(crate::block::HistoryPayloadMetadataSelection::capture(&manifest, bytes - 1).is_err());
        eprintln!("GC metadata selection segments={count} resident_bytes={bytes}");
    }
    let (_inner, _vfs, database, _) = simulated_fixture();
    assert_eq!(database.lock().unwrap().online_gc_capture(1).err().unwrap().kind(), io::ErrorKind::WouldBlock);
}

#[path = "tests_capture_causal.rs"]
mod causal;

thread_local! {
    static SELECTED_METADATA_VISIT_HOOK: RefCell<Option<Box<dyn FnMut(bool)>>> = const { RefCell::new(None) };
}
pub(super) fn before_selected_metadata_visit() {
    SELECTED_METADATA_VISIT_HOOK.with(|hook| {
        if let Some(hook) = hook.borrow_mut().as_mut() { hook(false); }
    });
}
pub(super) fn after_selected_metadata_insert() {
    SELECTED_METADATA_VISIT_HOOK.with(|hook| {
        if let Some(hook) = hook.borrow_mut().as_mut() { hook(true); }
    });
}
struct ResetSelectedMetadataVisitHook;
impl Drop for ResetSelectedMetadataVisitHook {
    fn drop(&mut self) { SELECTED_METADATA_VISIT_HOOK.with(|hook| *hook.borrow_mut() = None); }
}

#[test]
fn gc_stop_during_root_response_allows_started_read_to_finish_but_never_scans() {
    let (inner, vfs, database, source) = simulated_fixture();
    let orphan = database.lock().unwrap().write_orphan_block_for_test(b"inflight-stop-orphan").unwrap();
    let (entered, observed) = mpsc::channel();
    let release = Arc::new((Mutex::new(false), Condvar::new()));
    let stop = Arc::new(AtomicBool::new(false));
    vfs.arm(source, Some(MetadataAction::Block(entered, release.clone())));
    vfs.scanned.lock().unwrap().clear(); vfs.removed.lock().unwrap().clear();
    let worker = {
        let database = database.clone(); let stop = stop.clone();
        std::thread::spawn(move || {
            let mut collector = OnlineGarbageCollector::new(test_budget());
            let result = collector.run_epoch(&database, &stop);
            (result, metrics(&collector))
        })
    };
    observed.recv_timeout(Duration::from_secs(5)).unwrap();
    inner.arm_fault(None).unwrap();
    stop.store(true, Ordering::Release);
    *release.0.lock().unwrap() = true; release.1.notify_all();
    let (result, finished) = worker.join().unwrap();
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::WouldBlock);
    assert!(inner.trace().unwrap().iter().any(|op| *op == crate::vfs::Operation::Read),
        "the already-started complete root-body read may finish after stop");
    assert_eq!(vfs.hits(), 1);
    // The mark-independent segment lane observes `payload-segments` once
    // before marking; the cancelled mark never reaches a source scan.
    assert_eq!(*vfs.scanned.lock().unwrap(), vec![PathBuf::from("/data/payload-segments")]);
    assert!(!vfs.removed.lock().unwrap().contains(&block_path(Path::new("/data"), orphan)));
    assert_eq!(finished.epochs_completed, 0); assert_eq!(finished.deferred_epochs, 1);
    assert!(!finished.epoch_in_progress);
    database.try_lock().unwrap().engine_mut_for_test().require_physical_maintenance_available().unwrap();
}

#[test]
fn gc_stop_before_or_during_selected_indices_never_starts_root_read_or_delete() {
    for (stop_at_visit, after_insert) in [(1, false), (2, false), (3, false), (3, true)] {
        let (_inner, vfs, database, source) = simulated_fixture();
        let orphan = database.lock().unwrap().write_orphan_block_for_test(b"cancel-confirmed-orphan").unwrap();
        let mut collector = OnlineGarbageCollector::new(test_budget());
        super::tests::run_epoch(&mut collector, &database);
        let before = metrics(&collector);
        let blocks = database.lock().unwrap().block_store_shared();
        let references_before = Arc::strong_count(&blocks);
        vfs.arm(source, None); vfs.removed.lock().unwrap().clear(); vfs.scanned.lock().unwrap().clear();
        let stop = Arc::new(AtomicBool::new(false));
        let visits = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        SELECTED_METADATA_VISIT_HOOK.with(|hook| {
            let stop = stop.clone(); let visits = visits.clone();
            *hook.borrow_mut() = Some(Box::new(move |after| {
                let visited = if after { visits.load(Ordering::Acquire) }
                    else { visits.fetch_add(1, Ordering::AcqRel) + 1 };
                if visited == stop_at_visit && after == after_insert {
                    stop.store(true, Ordering::Release);
                }
            }));
        });
        let reset_hook = ResetSelectedMetadataVisitHook;
        assert_eq!(collector.run_epoch(&database, &stop).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        drop(reset_hook);
        assert_eq!(visits.load(Ordering::Acquire), stop_at_visit);
        assert_eq!(vfs.hits(), 0, "cancelled selection must not begin root source metadata/read");
        assert_eq!(*vfs.scanned.lock().unwrap(), vec![PathBuf::from("/data/payload-segments")],
            "only the pre-mark segment observation lists; cancelled partial marks must never enter the user-source scan phase");
        let marks = Path::new("/data/authority-gc/online/marks/gc-runs");
        assert!(vfs.inner.read_directory(marks).unwrap().is_empty(), "cancelled mark spills must be cleaned");
        assert_eq!(Arc::strong_count(&blocks), references_before, "aborted epoch must release captured source handles");
        let after = metrics(&collector);
        assert_eq!(after.epochs_completed, before.epochs_completed);
        assert_eq!(after.deferred_epochs, before.deferred_epochs + 1);
        assert!(!after.epoch_in_progress);
        assert_eq!(after.total_reclaimed_blocks, before.total_reclaimed_blocks);
        assert!(!vfs.removed.lock().unwrap().contains(&block_path(Path::new("/data"), orphan)));
        { let mut guard = database.try_lock().unwrap();
          guard.engine_mut_for_test().require_physical_maintenance_available().unwrap();
          assert_eq!(guard.read_block_for_test(orphan).unwrap(), b"cancel-confirmed-orphan"); }
        super::tests::run_epoch(&mut collector, &database);
    }
}

#[test]
fn gc_retains_every_metadata_page_of_an_old_online_backup_anchor() {
    let (inner, vfs, database, _) = simulated_fixture();
    let (anchor, metadata) = {
        let mut guard = database.lock().unwrap();
        put(&mut guard, b"anchored-three"); indexed_rotate(&mut guard);
        let pages = guard.engine_mut_for_test().install_paged_history_for_test();
        assert!(!pages.is_empty());
        let anchor = guard.capture_backup_anchor().unwrap();
        let manifest = history(&mut guard);
        let metadata = anchor.source.history_payload_metadata_ids(&manifest).unwrap();
        assert!(pages.iter().all(|id| metadata.contains(id)));
        put(&mut guard, b"current-four"); indexed_rotate(&mut guard);
        assert_ne!(guard.engine_mut_for_test().state().history_manifest_block_id,
            anchor.manifest.history_manifest_block_id);
        (anchor, metadata)
    };
    let capture = database.lock().unwrap().online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    assert!(metadata.iter().all(|id| capture.snapshot.history_payload_metadata_ids.contains(id)),
        "an old backup anchor must freeze its complete manifest/index metadata projection");
    drop(capture);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    super::tests::run_epoch(&mut collector, &database);
    super::tests::run_epoch(&mut collector, &database);
    assert!(metadata.iter().all(|id| anchor.source.get(*id).is_ok()));
    inner.create_dir(Path::new("/backup")).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let (destination, _) = crate::backup::initialize_or_open_backup(Path::new("/backup"), vfs).unwrap();
    crate::backup::copy_reachable_blocks(&anchor.source, &destination, &anchor.manifest).unwrap();
    database.lock().unwrap().deregister_backup_anchor(anchor.anchor_token);
}

/// A pin leaf reached both by a persistent pin's walk (without persistent-root
/// discovery) and by its own root's walk (with it) still has its pins
/// discovered (docs/TOFUDB_ONLINE_GC.md, "Pages shared by several roots are
/// walked once per mark"): here the only path to a deleted record is pin `c`,
/// named by the pin leaf of a root that an open transaction holds and that the
/// current root's pin `b` also reaches. The record's commit is checkpointed and
/// its history pruned, so neither the WAL nor history marks its pages.
#[test]
fn pin_leaf_shared_with_a_persistent_pin_tree_still_marks_the_root_it_names() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
    authority.set_write_overlay_for_test(false);
    let mut transaction = authority.begin(7, 11).unwrap();
    for index in 0..256_u32 {
        let record = EntityKey::new(7, 11, "record", &index.to_be_bytes()).unwrap();
        authority.entity_put(&mut transaction, record, vec![index as u8; 64]).unwrap();
    }
    authority.commit(transaction).unwrap();
    let kept = EntityKey::new(7, 11, "record", b"kept-only-by-pin-c").unwrap();
    let mut transaction = authority.begin(7, 11).unwrap();
    authority.entity_put(&mut transaction, kept.clone(), b"pinned-value".to_vec()).unwrap();
    authority.commit(transaction).unwrap();
    authority.checkpoint_for_test().unwrap();
    let mut transaction = authority.begin(7, 11).unwrap();
    let pin_c_root = authority.stage_persistent_snapshot_pin(&mut transaction, b"c").unwrap().root;
    authority.entity_delete(&mut transaction, kept).unwrap();
    authority.commit(transaction).unwrap();
    let held = authority.begin(7, 11).unwrap();
    let held_root = authority.authority_state_root();
    let mut transaction = authority.begin(7, 11).unwrap();
    assert!(authority.remove_persistent_snapshot_pin(&mut transaction, b"c").unwrap());
    assert_eq!(authority.stage_persistent_snapshot_pin(&mut transaction, b"b").unwrap().root, held_root);
    authority.commit(transaction).unwrap();
    authority.checkpoint_for_test().unwrap();
    authority.compact_history(1).unwrap();

    let capture = authority.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    assert_eq!(capture.snapshot.pinned_roots, vec![held_root.unwrap()]);
    let blocks = Arc::clone(&capture.blocks);
    let mut pin_c_pages = Vec::new();
    visit_entity_page_graph_with_values(pin_c_root, |id| blocks.get_traversal(id),
        |id, _| { pin_c_pages.push(id); Ok(()) }, |_, _| Ok(())).unwrap();
    let collector = OnlineGarbageCollector::new(test_budget());
    let mut epoch = unmarked_epoch(capture, &collector);
    collector.build_marks(&mut epoch, &AtomicBool::new(false)).unwrap();
    let marks = epoch.marks.take().unwrap();
    pin_c_pages.sort_unstable();
    pin_c_pages.dedup();
    let mut marked = pin_c_pages.clone();
    marks.retain_live_ids(&mut marked).unwrap();
    assert_eq!(marked, pin_c_pages, "every page of the root pin c names stays marked");
    marks.cleanup().unwrap();
    drop(held);
}

/// A document spanning four blob chunks and a stream with three persisted
/// segments, durably checkpointed and reopened so every block is on disk.
fn leaf_heavy_authority() -> (Arc<DeterministicVfs>, AuthorityDatabase, EntityKey) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let document = EntityKey::new(7, 11, "reachability", b"document").unwrap();
    let text: String = (0..3 * crate::blob::BLOB_CHUNK_BYTES + 1024)
        .map(|index| char::from(b'a' + (index / 7 % 26) as u8))
        .collect();
    let mut transaction = authority.begin(7, 11).unwrap();
    crate::versioned_document::put(&authority, &mut transaction, crate::versioned_document::PutRequest {
        key: document.clone(), namespace: "reachability".into(), logical_key: "document".into(),
        value_json: serde_json::to_vec(&text).unwrap(), expected_version: None, updated_at_ms: 1,
    }).unwrap();
    authority.commit(transaction).unwrap();
    let stream = crate::stream::StreamKey::new(7, 11, "reachability", b"task").unwrap();
    for append in 0..3_u64 {
        let mut transaction = authority.begin(7, 11).unwrap();
        let events = (0..4_u64).map(|index| crate::stream::StreamEvent::new(
            (append * 4 + index) as i64, "delta", vec![(append * 4 + index) as u8; 128 * 1024]).unwrap()).collect();
        authority.stream_append(&mut transaction, stream.clone(), append * 4 + 1, events).unwrap();
        authority.commit(transaction).unwrap();
    }
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    drop(authority);
    let authority = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, authority, document)
}

/// (blocks reached with payloads, blocks reached by reference, blocks the
/// reference walk read, entity pages) over every entity value of `root`.
fn value_reachability(blocks: &BlockStore, root: Option<BlockId>)
    -> (Vec<BlockId>, Vec<BlockId>, Vec<BlockId>, Vec<BlockId>) {
    let (mut payload_visits, mut reference_visits, mut reference_reads) = (Vec::new(), Vec::new(), Vec::new());
    let mut pages = Vec::new();
    visit_entity_page_graph_with_values(root, |id| blocks.get_traversal(id), |id, _| { pages.push(id); Ok(()) }, |key, value| {
        crate::backup::visit_semantic_value_blocks_with(key, value, |id| blocks.get_traversal(id),
            |id, _| { payload_visits.push(id); Ok(()) })?;
        visit_semantic_value_references(key, value,
            |id| { reference_reads.push(id); blocks.get_traversal(id) },
            |id| { reference_visits.push(id); Ok(()) })
    }).unwrap();
    (payload_visits, reference_visits, reference_reads, pages)
}

fn disk_reads(vfs: &DeterministicVfs) -> usize {
    vfs.trace().unwrap().iter().filter(|operation| **operation == crate::vfs::Operation::Read).count()
}

#[test]
fn reachability_by_reference_reads_only_manifests_and_visits_the_same_blocks() {
    let (_vfs, authority, document) = leaf_heavy_authority();
    let mut transaction = authority.begin(7, 11).unwrap();
    let stored = authority.entity_get(&mut transaction, &document).unwrap().unwrap();
    let manifest = crate::versioned_document::stored_blob_reference(&stored).unwrap().unwrap().manifest_block_id;
    let root = authority.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap().snapshot.authority_state_root;
    let (payload_visits, reference_visits, reference_reads, _) = value_reachability(&authority.block_store_shared(), root);
    assert_eq!(reference_visits, payload_visits);
    // One manifest, four chunks and three stream segments are reachable; only
    // the manifest defines further references.
    assert_eq!(payload_visits.len(), 8, "{payload_visits:?}");
    assert_eq!(reference_reads, vec![manifest]);
}

#[test]
fn online_mark_skips_leaf_payloads_and_admits_nothing_to_the_block_cache() {
    let (vfs, authority, _) = leaf_heavy_authority();
    let blocks = authority.block_store_shared();
    let root = authority.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap().snapshot.authority_state_root;
    let (reachable, _, _, pages) = value_reachability(&blocks, root);
    let database = Arc::new(Mutex::new(authority));
    let collector = OnlineGarbageCollector::new(test_budget());
    // Each mark starts from an empty block cache of the given capacity.
    let mark = |database: &Arc<Mutex<AuthorityDatabase>>, cache_bytes: usize| {
        let capture = database.lock().unwrap().online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
        blocks.set_cache_capacity_bytes(0).unwrap();
        blocks.set_cache_capacity_bytes(cache_bytes).unwrap();
        let mut epoch = unmarked_epoch(capture, &collector);
        collector.build_marks(&mut epoch, &AtomicBool::new(false)).unwrap();
        epoch.marks.take().unwrap()
    };
    let marks = mark(&database, 256 * 1024 * 1024);
    assert!(pages.iter().chain(&reachable).all(|id| !blocks.cache_contains_for_test(*id)),
        "a mark must not admit the pages or value blocks it traverses");
    let mut live = reachable.clone();
    live.sort_unstable();
    live.dedup();
    let expected = live.len();
    marks.retain_live_ids(&mut live).unwrap();
    assert_eq!(live.len(), expected, "every reachable value block stays marked");
    let _ = marks.cleanup();
    // With every hot cache disabled each read reaches the volume: the mark
    // needs fewer reads than one payload walk of the same values alone.
    database.lock().unwrap().configure_hot_data_caches(0, 0).unwrap();
    let before = disk_reads(&vfs);
    let _ = mark(&database, 0).cleanup();
    let mark_reads = disk_reads(&vfs) - before;
    let before = disk_reads(&vfs);
    let mut payload_reads = 0_usize;
    visit_entity_page_graph_with_values(root, |id| blocks.get_traversal(id), |_, _| Ok(()), |key, value| {
        crate::backup::visit_semantic_value_blocks_with(key, value, |id| blocks.get_traversal(id),
            |_, _| { payload_reads += 1; Ok(()) })
    }).unwrap();
    let walk_reads = disk_reads(&vfs) - before;
    assert_eq!(payload_reads, reachable.len());
    assert!(mark_reads < walk_reads, "mark read {mark_reads} times; payload walk {walk_reads}");
    println!("online_mark_disk_reads={mark_reads} payload_walk_disk_reads={walk_reads} reachable_value_blocks={}",
        reachable.len());
}

// Real-volume component measurement of the mark's value reads: the same page
// graph walked with reference-only values against payload values (the reads
// the mark performed before 2026-10-30). Content is pseudo-random hex text, so
// blob chunks compress about two to one. Hot engine caches are disabled;
// operating-system file caches are not dropped.
#[test]
#[ignore = "real-volume measurement; requires an explicit disposable parent and output"]
fn mark_value_reads_measurement() {
    let parent = std::env::var("TOFU_DB_GC_MARK_MEASURE_ROOT").unwrap();
    let output = std::env::var("TOFU_DB_GC_MARK_MEASURE_OUTPUT").unwrap();
    let directory = tempfile::tempdir_in(&parent).unwrap();
    let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut state = 0x9e37_79b9_7f4a_7c15_u64;
    let mut hex = |length: usize| -> String {
        (0..length).map(|_| {
            state ^= state << 13; state ^= state >> 7; state ^= state << 17;
            char::from(b"0123456789abcdef"[(state & 15) as usize])
        }).collect()
    };
    let documents = 16_u64;
    for document in 0..documents {
        let text = hex(3 * crate::blob::BLOB_CHUNK_BYTES + 1024);
        let mut transaction = authority.begin(7, 11).unwrap();
        crate::versioned_document::put(&authority, &mut transaction, crate::versioned_document::PutRequest {
            key: EntityKey::new(7, 11, "measure", &document.to_be_bytes()).unwrap(),
            namespace: "measure".into(), logical_key: format!("document-{document}"),
            value_json: serde_json::to_vec(&text).unwrap(), expected_version: None, updated_at_ms: 1,
        }).unwrap();
        authority.commit(transaction).unwrap();
    }
    for stream in 0..4_u64 {
        let key = crate::stream::StreamKey::new(7, 11, "measure", &stream.to_be_bytes()).unwrap();
        for append in 0..3_u64 {
            let mut transaction = authority.begin(7, 11).unwrap();
            let events = (0..4_u64).map(|index| crate::stream::StreamEvent::new(
                (append * 4 + index) as i64, "delta", hex(128 * 1024).into_bytes()).unwrap()).collect();
            authority.stream_append(&mut transaction, key.clone(), append * 4 + 1, events).unwrap();
            authority.commit(transaction).unwrap();
        }
    }
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    drop(authority);
    let authority = AuthorityDatabase::open_with_vfs(directory.path(), Arc::new(RealVfs)).unwrap();
    authority.configure_hot_data_caches(0, 0).unwrap();
    let blocks = authority.block_store_shared();
    let root = authority.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap().snapshot.authority_state_root;
    let walk = |references: bool| {
        let (mut reads, mut bytes) = (0_u64, 0_u64);
        let started = Instant::now();
        let mut read = |id| { let payload = blocks.get_traversal(id)?; reads += 1; bytes += payload.len() as u64; Ok(payload) };
        let mut visited = 0_u64;
        visit_entity_page_graph_with_values(root, |id| blocks.get_traversal(id), |_, _| Ok(()), |key, value| {
            if references {
                visit_semantic_value_references(key, value, &mut read, |_| { visited += 1; Ok(()) })
            } else {
                crate::backup::visit_semantic_value_blocks_with(key, value, &mut read, |_, _| { visited += 1; Ok(()) })
            }
        }).unwrap();
        (started.elapsed().as_micros() as u64, reads, bytes, visited)
    };
    let mut runs = Vec::new();
    for _ in 0..3 {
        let (payload_micros, payload_reads, payload_bytes, payload_visited) = walk(false);
        let (reference_micros, reference_reads, reference_bytes, reference_visited) = walk(true);
        assert_eq!(payload_visited, reference_visited);
        runs.push(serde_json::json!({
            "payload_micros": payload_micros, "payload_value_reads": payload_reads, "payload_value_bytes": payload_bytes,
            "reference_micros": reference_micros, "reference_value_reads": reference_reads, "reference_value_bytes": reference_bytes,
            "value_blocks": reference_visited,
        }));
    }
    let report = serde_json::json!({
        "source_sha256": env!("TOFUDB_SOURCE_SHA256"), "parent": parent,
        "scope": "16 four-chunk hex documents and 4 three-segment streams; same page graph; hot engine caches disabled; OS caches not dropped; component, not production",
        "runs": runs,
    });
    std::fs::write(output, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
}
