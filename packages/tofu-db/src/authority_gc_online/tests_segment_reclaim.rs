//! Superseded payload-segment reclamation without block marks
//! (docs/TOFUDB_ONLINE_GC.md, "Superseded payload-segment reclamation
//! independent of block marks"). Until 2026-10-31 the live main-authority
//! mark build aborted at its former 900 s budget on every epoch; these fixtures
//! make every mark build fail while directory listing, stat, unlink and
//! CONTROL I/O keep working, and count the files left in `payload-segments`.

use super::tests::{assert_live_value, metrics, run_epoch, test_budget};
use super::*;
use crate::entity::EntityKey;
use std::sync::atomic::AtomicUsize;

use crate::control::ControlFile;
use crate::payload_manifest::PayloadManifest;
use crate::vfs::{
    sync_all_barrier, sync_directory_barrier, BoundedDirectoryEntries, BoundedDirectoryMatches,
    DeterministicVfs, FaultAction, OpenRequest, Operation, VfsFile,
};

pub(super) const UNFINISHED_MARK_ERROR: &str = "simulated mark build that never finishes";

/// While `abort_marks` is set, every block or segment content read fails
/// with `UNFINISHED_MARK_ERROR`: the mark build can never finish.
pub(super) struct UnfinishableMarkVfs {
    pub(super) inner: Arc<DeterministicVfs>,
    pub(super) abort_marks: Arc<AtomicBool>,
    /// Runs once, right before the next `payload-segments` listing.
    pub(super) before_segment_listing: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    /// Runs once, right before the next `payload-segments` directory barrier.
    pub(super) before_segment_directory_sync: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    /// CONTROL slot writes attempted through handles opened on this VFS.
    pub(super) control_writes: Arc<AtomicUsize>,
    /// Every CONTROL write from this ordinal on fails before writing.
    pub(super) fail_control_writes_from: Arc<AtomicUsize>,
}

impl UnfinishableMarkVfs {
    pub(super) fn new() -> Arc<Self> {
        let inner = Arc::new(DeterministicVfs::new(None));
        inner.create_dir(Path::new("/data")).unwrap();
        inner.sync_directory(Path::new("/")).unwrap();
        Self::over(inner)
    }

    pub(super) fn over(inner: Arc<DeterministicVfs>) -> Arc<Self> {
        Arc::new(Self {
            inner,
            abort_marks: Arc::new(AtomicBool::new(false)),
            before_segment_listing: Mutex::new(None),
            before_segment_directory_sync: Mutex::new(None),
            control_writes: Arc::new(AtomicUsize::new(0)),
            fail_control_writes_from: Arc::new(AtomicUsize::new(usize::MAX)),
        })
    }

    fn listing_hook(&self, path: &Path) {
        if path.ends_with("payload-segments") {
            if let Some(hook) = self.before_segment_listing.lock().unwrap().take() {
                hook();
            }
        }
    }
}

impl Vfs for UnfinishableMarkVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let file = self.inner.open(path, request)?;
        if path.ends_with("CONTROL") {
            return Ok(Box::new(ControlWriteFile {
                inner: file,
                writes: Arc::clone(&self.control_writes),
                fail_from: Arc::clone(&self.fail_control_writes_from),
            }));
        }
        let source = path
            .components()
            .any(|part| part.as_os_str() == "blocks" || part.as_os_str() == "payload-segments");
        if request.read && source {
            return Ok(Box::new(UnfinishableMarkFile {
                inner: file,
                abort_marks: Arc::clone(&self.abort_marks),
            }));
        }
        Ok(file)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.create_dir(path)
    }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        self.inner.rename(source, destination)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.inner.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        self.inner.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.listing_hook(path);
        self.inner.read_directory(path)
    }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<BoundedDirectoryEntries> {
        self.listing_hook(path);
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        maximum: usize,
        matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        if path.ends_with("payload-segments") {
            let hook = self.before_segment_directory_sync.lock().unwrap().take();
            if let Some(hook) = hook {
                hook();
            }
        }
        self.inner.sync_directory(path)
    }
}

struct UnfinishableMarkFile {
    inner: Box<dyn VfsFile>,
    abort_marks: Arc<AtomicBool>,
}

impl UnfinishableMarkFile {
    fn admit_read(&self) -> io::Result<()> {
        if self.abort_marks.load(Ordering::Acquire) {
            return Err(would_block(UNFINISHED_MARK_ERROR));
        }
        Ok(())
    }
}

impl VfsFile for UnfinishableMarkFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.admit_read()?;
        self.inner.read_all(maximum)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.admit_read()?;
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.inner.set_len(length)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}

struct ControlWriteFile {
    inner: Box<dyn VfsFile>,
    writes: Arc<AtomicUsize>,
    fail_from: Arc<AtomicUsize>,
}

impl VfsFile for ControlWriteFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.inner.read_all(maximum)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        let ordinal = self.writes.fetch_add(1, Ordering::SeqCst) + 1;
        if ordinal >= self.fail_from.load(Ordering::SeqCst) {
            return Err(io::Error::other("injected CONTROL write failure"));
        }
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.inner.set_len(length)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}

pub(super) fn live_key() -> EntityKey {
    EntityKey::new(7, 11, "record", b"segment-reclaim-live").unwrap()
}

/// An authority whose mark build has real pages to read: a live value and a
/// few hundred neighbours, merged into the tree and checkpointed, with the
/// hot caches disabled so marking always reaches the (aborting) source.
pub(super) fn fixture() -> (Arc<UnfinishableMarkVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = UnfinishableMarkVfs::new();
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let mut transaction = authority.begin(7, 11).unwrap();
    for index in 0..256_u32 {
        authority
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "record", &index.to_le_bytes()).unwrap(),
                vec![5; 256],
            )
            .unwrap();
    }
    authority.entity_put(&mut transaction, live_key(), b"live-value".to_vec()).unwrap();
    authority.commit(transaction).unwrap();
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    authority.configure_hot_data_caches(0, 0).unwrap();
    (vfs, Arc::new(Mutex::new(authority)))
}

pub(super) fn segment_files(vfs: &UnfinishableMarkVfs) -> usize {
    vfs.inner.read_directory(Path::new("/data/payload-segments")).unwrap().len()
}

/// One epoch whose block mark build aborts, as every live epoch did at the
/// former 900 s budget.
pub(super) fn run_epoch_with_aborted_mark(
    collector: &mut OnlineGarbageCollector,
    database: &Arc<Mutex<AuthorityDatabase>>,
    vfs: &UnfinishableMarkVfs,
) {
    vfs.abort_marks.store(true, Ordering::Release);
    let error = collector.run_epoch(database, &AtomicBool::new(false)).unwrap_err();
    vfs.abort_marks.store(false, Ordering::Release);
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "{error}");
    assert!(error.to_string().contains(UNFINISHED_MARK_ERROR), "{error}");
}

/// Superseded files that no catalog, pack or retirement set references, as
/// left behind by full-catalog history promotion, an abandoned background
/// pack or a restart that dropped the in-memory retirement set.
pub(super) fn leave_superseded_segments(database: &Arc<Mutex<AuthorityDatabase>>, round: usize, count: usize) {
    let guard = database.lock().unwrap();
    for index in 0..count {
        let payload = format!("superseded-segment-{round}-{index}");
        let block_id = guard.write_orphan_block_for_test(payload.as_bytes()).unwrap();
        guard.create_orphan_payload_segment_for_test(block_id).unwrap();
    }
}

/// The incident shape: superseded segments keep arriving while no block mark
/// build ever completes. Before the fix the orphan sweep ran only after a
/// finished mark (and then reclaimed at most one file per epoch), so the
/// directory grew by every round's files; now each file is removed by the
/// epoch after the one that first observed it.
#[test]
fn superseded_segments_stay_bounded_while_every_mark_build_aborts() {
    let (vfs, database) = fixture();
    let live_files = segment_files(&vfs);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    let mut unreferenced_after_epoch = Vec::new();
    for round in 0..6 {
        leave_superseded_segments(&database, round, 4);
        run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
        unreferenced_after_epoch.push(segment_files(&vfs) - live_files);
    }
    assert_eq!(
        unreferenced_after_epoch,
        vec![4; 6],
        "superseded files must not accumulate while marks abort (each round adds 4)"
    );
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(segment_files(&vfs), live_files);
    let metrics = metrics(&collector);
    assert_eq!(metrics.epochs_completed, 0, "no block mark ever finished");
    assert_eq!(metrics.total_orphan_segments_removed, 24);
    assert_live_value(&database, &live_key(), b"live-value");
}

/// A restart drops the in-memory retirement set; the parked files become
/// orphans. They stay protected while parked, and after the restart they are
/// reclaimed by the second epoch although every mark build aborts.
#[test]
fn restart_dropped_retirements_are_reclaimed_although_marks_abort() {
    let (vfs, database) = fixture();
    let retired = {
        let mut guard = database.lock().unwrap();
        let mut retired = Vec::new();
        for index in 0..8 {
            let payload = format!("retired-segment-{index}");
            let block_id = guard.write_orphan_block_for_test(payload.as_bytes()).unwrap();
            guard.compact_payload_blocks_for_test(&[block_id]).unwrap();
            let segment_id = guard
                .engine_mut_for_test()
                .payload_segment_references()
                .iter()
                .find(|reference| {
                    reference.first_block_id == block_id && reference.last_block_id == block_id
                })
                .unwrap()
                .segment_id;
            retired.push(segment_id);
        }
        for segment_id in &retired {
            guard.engine_mut_for_test().collect_payload_segment(*segment_id, &[]).unwrap();
        }
        retired
    };
    let parked_files = segment_files(&vfs);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    for _ in 0..3 {
        run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    }
    assert_eq!(segment_files(&vfs), parked_files, "parked retirements keep their reader floor");

    drop(database);
    vfs.inner.crash().unwrap();
    let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    reopened.configure_hot_data_caches(0, 0).unwrap();
    let database = Arc::new(Mutex::new(reopened));
    assert!(database.lock().unwrap().block_store_shared().protected_segment_ids().unwrap().is_empty());
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(segment_files(&vfs), parked_files, "the first observation confirms nothing");
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(segment_files(&vfs), parked_files - retired.len());
    for segment_id in retired {
        let path = Path::new("/data/payload-segments").join(format!("{}.pseg", segment_id.simple()));
        assert_eq!(vfs.inner.metadata(&path).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
    assert_eq!(metrics(&collector).total_orphan_segments_removed, 8);
    assert_live_value(&database, &live_key(), b"live-value");
}

fn write_scratch_file(vfs: &DeterministicVfs, path: &Path, bytes: &[u8]) {
    let mut file = vfs
        .open(path, OpenRequest { write: true, create_new: true, ..OpenRequest::default() })
        .unwrap();
    file.write_all_at(0, bytes).unwrap();
    sync_all_barrier(file.as_mut()).unwrap();
    sync_directory_barrier(vfs, path.parent().unwrap()).unwrap();
}

/// Racing background pack: after a temporary file has been observed once, a
/// pack reserves staged payloads and writes its segment between the snapshot
/// hold and the directory listing. The removal hold must refuse the
/// temporary file while the pack is active, and the racing segment (seen
/// unreferenced once) must survive until it is published.
#[test]
fn racing_background_pack_files_survive_the_orphan_lane() {
    let (vfs, database) = fixture();
    let store = database.lock().unwrap().block_store_shared();
    let temporary = Path::new("/data/payload-segments")
        .join(format!(".new-{}", uuid::Uuid::from_bytes([0x5a; 16]).simple()));
    write_scratch_file(&vfs.inner, &temporary, b"abandoned temporary segment prefix");
    let staged = database
        .lock()
        .unwrap()
        .engine_mut_for_test()
        .write_block(b"racing background pack payload")
        .unwrap();
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert!(vfs.inner.metadata(&temporary).is_ok());

    let racing = Arc::new(Mutex::new(None));
    {
        let store = Arc::clone(&store);
        let racing = Arc::clone(&racing);
        let abort_marks = Arc::clone(&vfs.abort_marks);
        *vfs.before_segment_listing.lock().unwrap() = Some(Box::new(move || {
            abort_marks.store(false, Ordering::Release);
            let (segment_id, ids) = store.begin_background_pack(1, u64::MAX).unwrap().unwrap();
            let segment = store
                .payload_segment_store()
                .unwrap()
                .create(segment_id, ids, |id| store.get(id))
                .unwrap();
            abort_marks.store(true, Ordering::Release);
            *racing.lock().unwrap() = Some(segment);
        }));
    }
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    let segment = racing.lock().unwrap().take().expect("the racing pack ran inside the listing");
    let segment_id = segment.metadata().segment_id;
    let segment_path = Path::new("/data/payload-segments").join(format!("{}.pseg", segment_id.simple()));
    assert!(vfs.inner.metadata(&temporary).is_ok(), "an active pack owns temporary files");
    assert!(vfs.inner.metadata(&segment_path).is_ok());
    for _ in 0..2 {
        run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
        assert!(vfs.inner.metadata(&segment_path).is_ok(), "an unpublished pack segment is protected");
        assert!(vfs.inner.metadata(&temporary).is_ok());
    }

    let reference = PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap();
    store.verify_payload_segment(&reference).unwrap();
    store.note_packed(&segment.block_ids().collect::<Vec<_>>()).unwrap();
    store.complete_background_pack(segment_id, reference).unwrap();
    assert!(database.lock().unwrap().publish_completed_pack_if_capacity().unwrap());
    for _ in 0..2 {
        run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    }
    assert!(vfs.inner.metadata(&segment_path).is_ok(), "the published segment is catalog-referenced");
    assert_eq!(vfs.inner.metadata(&temporary).unwrap_err().kind(), io::ErrorKind::NotFound);
    assert_eq!(
        database.lock().unwrap().read_block_for_test(staged).unwrap(),
        b"racing background pack payload"
    );
    assert_live_value(&database, &live_key(), b"live-value");
}

/// Segments named by any selectable CONTROL slot, read from a fresh handle on
/// the durable image the next open would see.
fn durable_slot_segments(vfs: &UnfinishableMarkVfs, blocks: &BlockStore) -> Vec<(u64, BTreeSet<Uuid>)> {
    let mut control = ControlFile::open_with_vfs(Path::new("/data"), vfs.inner.clone()).unwrap();
    control.read_current().unwrap();
    control
        .selectable_states_for_test()
        .into_iter()
        .map(|state| {
            let segments = state.payload_manifest_block_id.map_or_else(BTreeSet::new, |manifest_id| {
                PayloadManifest::decode(&blocks.get(manifest_id).unwrap())
                    .unwrap()
                    .segments
                    .iter()
                    .map(|reference| reference.segment_id)
                    .collect()
            });
            (state.generation, segments)
        })
        .collect()
}

fn reopen(vfs: &Arc<UnfinishableMarkVfs>) -> Arc<Mutex<AuthorityDatabase>> {
    vfs.inner.crash().unwrap();
    vfs.fail_control_writes_from.store(usize::MAX, Ordering::SeqCst);
    let authority = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    authority.configure_hot_data_caches(0, 0).unwrap();
    Arc::new(Mutex::new(authority))
}

/// Retirement before the durable dual-slot CONTROL: a crash after the
/// publication that dropped segment S but before its stabilizing write leaves
/// S unparked, absent from the installed catalog, yet still named by the
/// fallback slot. The orphan lane must republish both slots before the
/// unlink, so losing the newest slot afterwards never selects a catalog whose
/// file is gone.
#[test]
fn orphan_named_by_the_fallback_control_slot_is_unlinked_only_after_both_slots_move() {
    let (seed, victim) = {
        let (vfs, database) = fixture();
        let victim = {
            let mut guard = database.lock().unwrap();
            let block_id = guard.write_orphan_block_for_test(b"named by the fallback slot").unwrap();
            guard.compact_payload_blocks_for_test(&[block_id]).unwrap();
            guard
                .engine_mut_for_test()
                .payload_segment_references()
                .iter()
                .find(|reference| reference.first_block_id == block_id)
                .unwrap()
                .segment_id
        };
        drop(database);
        vfs.inner.crash().unwrap();
        (vfs.inner.clone(), victim)
    };
    // The retirement publishes CONTROL and then stabilizes it: fail exactly
    // that last write and every later one (the crash stops all writes).
    let control_writes = {
        let vfs = UnfinishableMarkVfs::over(Arc::new(seed.fork_durable().unwrap()));
        let database = reopen(&vfs);
        vfs.control_writes.store(0, Ordering::SeqCst);
        database.lock().unwrap().engine_mut_for_test().collect_payload_segment(victim, &[]).unwrap();
        vfs.control_writes.load(Ordering::SeqCst)
    };
    let vfs = UnfinishableMarkVfs::over(Arc::new(seed.fork_durable().unwrap()));
    let database = reopen(&vfs);
    vfs.control_writes.store(0, Ordering::SeqCst);
    vfs.fail_control_writes_from.store(control_writes, Ordering::SeqCst);
    assert!(database.lock().unwrap().engine_mut_for_test().collect_payload_segment(victim, &[]).is_err());
    drop(database);
    let database = reopen(&vfs);
    let blocks = database.lock().unwrap().block_store_shared();
    let victim_path = Path::new("/data/payload-segments").join(format!("{}.pseg", victim.simple()));
    assert!(vfs.inner.metadata(&victim_path).is_ok());
    assert!(!blocks.installed_payload_manifest().unwrap().unwrap().segments.iter()
        .any(|reference| reference.segment_id == victim));
    let slots = durable_slot_segments(&vfs, &blocks);
    assert!(slots.iter().any(|(_, segments)| segments.contains(&victim)),
        "fixture: the fallback slot still names the dropped segment: {slots:?}");

    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert!(vfs.inner.metadata(&victim_path).is_ok(), "the first observation confirms nothing");
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(vfs.inner.metadata(&victim_path).unwrap_err().kind(), io::ErrorKind::NotFound);
    let slots = durable_slot_segments(&vfs, &blocks);
    assert_eq!(slots.len(), 2);
    assert!(slots.iter().all(|(_, segments)| !segments.contains(&victim)),
        "both durable slots must stop naming the segment before it is unlinked: {slots:?}");

    // Media loss of the newest slot: recovery selects the other one, whose
    // catalog must resolve completely.
    let newest = slots.iter().map(|(generation, _)| *generation).max().unwrap();
    drop(database);
    let mut control = vfs
        .inner
        .open(Path::new("/data/CONTROL"), OpenRequest { write: true, ..OpenRequest::default() })
        .unwrap();
    control.write_all_at(((newest - 1) % 2) * 4096, &[0xa5; 64]).unwrap();
    sync_all_barrier(control.as_mut()).unwrap();
    drop(control);
    let database = reopen(&vfs);
    let installed = database.lock().unwrap().block_store_shared().installed_payload_manifest().unwrap().unwrap();
    for reference in installed.segments {
        let path = Path::new("/data/payload-segments").join(format!("{}.pseg", reference.segment_id.simple()));
        assert!(vfs.inner.metadata(&path).is_ok(), "fallback catalog names missing segment {}", reference.segment_id);
    }
    assert_live_value(&database, &live_key(), b"live-value");
}

/// Shutdown during the orphan observation (tens of thousands of stats on the
/// live volume) stops before the next file and removes nothing; the
/// interrupted observation confirms nothing later, so two fresh observations
/// are needed again.
#[test]
fn stop_during_orphan_observation_removes_nothing_and_restarts_confirmation() {
    let (vfs, database) = fixture();
    let live_files = segment_files(&vfs);
    leave_superseded_segments(&database, 0, 3);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = Arc::clone(&stop);
        *vfs.before_segment_listing.lock().unwrap() =
            Some(Box::new(move || stop.store(true, Ordering::Release)));
    }
    let error = collector.run_epoch(&database, &stop).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    assert!(error.to_string().contains("online GC epoch stopped"), "{error}");
    assert_eq!(segment_files(&vfs), live_files + 3);
    let stopped = metrics(&collector);
    assert_eq!((stopped.marks_built, stopped.segment_lane_failures), (0, 0),
        "a stop abandons the epoch before marking and is not a lane failure");
    stop.store(false, Ordering::Release);
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(segment_files(&vfs), live_files + 3, "an interrupted observation confirms nothing");
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(segment_files(&vfs), live_files);
}

/// The superseded files `leave_superseded_segments` adds, and how many of
/// them still exist. Full epochs may also rewrite live segments, so these
/// tests count exactly the files they created.
fn superseded_files(vfs: &UnfinishableMarkVfs, database: &Arc<Mutex<AuthorityDatabase>>, count: usize) -> Vec<PathBuf> {
    let listing = || {
        vfs.inner.read_directory(Path::new("/data/payload-segments")).unwrap().into_iter().collect::<BTreeSet<_>>()
    };
    let before = listing();
    leave_superseded_segments(database, 0, count);
    let added = listing().difference(&before).cloned().collect::<Vec<_>>();
    assert_eq!(added.len(), count);
    added
}

fn remaining(vfs: &UnfinishableMarkVfs, files: &[PathBuf]) -> usize {
    files.iter().filter(|path| vfs.inner.metadata(path).is_ok()).count()
}

/// An integrity error of the segment lane (an entry the directory contract
/// does not allow) no longer stops the block-mark lane, and it is not
/// swallowed: every epoch still completes its marks and loose deletion, the
/// lane counts each failed pass and keeps the error visible until a pass
/// completes again, and it never guesses about the unknown file.
#[test]
fn segment_lane_integrity_failure_stays_visible_while_block_marks_continue() {
    let (vfs, database) = fixture();
    let superseded = superseded_files(&vfs, &database, 2);
    let orphan = database
        .lock()
        .unwrap()
        .write_orphan_block_for_test(b"loose orphan behind a failing segment lane")
        .unwrap();
    let unknown = Path::new("/data/payload-segments/operator-notes.txt");
    write_scratch_file(&vfs.inner, unknown, b"not a payload segment");
    let mut collector = OnlineGarbageCollector::new(test_budget());
    for epoch in 1..=2 {
        run_epoch(&mut collector, &database);
        let metrics = metrics(&collector);
        assert_eq!((metrics.epochs_completed, metrics.segment_lane_failures), (epoch, epoch));
        assert!(
            metrics.segment_lane_error.as_deref().is_some_and(|error| error.contains("unknown entry")),
            "{metrics:?}"
        );
    }
    assert_eq!(
        database.lock().unwrap().read_block_for_test(orphan).unwrap_err().kind(),
        io::ErrorKind::NotFound,
        "the block-mark lane confirmed and removed the loose orphan"
    );
    assert!(vfs.inner.metadata(unknown).is_ok());
    assert_eq!(remaining(&vfs, &superseded), 2, "the failing lane removed nothing");

    vfs.inner.remove_file(unknown).unwrap();
    run_epoch(&mut collector, &database);
    let repaired = metrics(&collector);
    assert_eq!((repaired.segment_lane_failures, repaired.segment_lane_error), (2, None));
    run_epoch(&mut collector, &database);
    assert_eq!(remaining(&vfs, &superseded), 0);
    assert_eq!(metrics(&collector).total_orphan_segments_removed, 2);
    assert_live_value(&database, &live_key(), b"live-value");
}

/// A failed unlink ends the lane's pass, not the epoch: the removal before it
/// stays counted, the marks still run, and the cleared observation memory
/// makes the remaining files wait for two fresh observations.
#[test]
fn segment_unlink_failure_is_recorded_and_retried_from_fresh_observations() {
    let (vfs, database) = fixture();
    let superseded = superseded_files(&vfs, &database, 3);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    vfs.inner
        .arm_operation_fault(Operation::RemoveFile, 2, FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied))
        .unwrap();
    run_epoch(&mut collector, &database);
    assert!(vfs.inner.operation_fault_fired().unwrap());
    vfs.inner.arm_fault(None).unwrap();
    let failed = metrics(&collector);
    assert_eq!((failed.epochs_completed, failed.marks_built), (2, 2));
    assert_eq!((failed.segment_lane_failures, failed.total_orphan_segments_removed), (1, 1));
    assert!(failed.segment_lane_error.is_some());
    assert_eq!(remaining(&vfs, &superseded), 2);

    run_epoch(&mut collector, &database);
    assert_eq!(remaining(&vfs, &superseded), 2, "a failed pass confirms nothing");
    assert_eq!(metrics(&collector).segment_lane_error, None);
    run_epoch(&mut collector, &database);
    assert_eq!(remaining(&vfs, &superseded), 0);
    assert_eq!(metrics(&collector).total_orphan_segments_removed, 3);
    assert_live_value(&database, &live_key(), b"live-value");
}

pub(super) fn segment_path(segment_id: Uuid) -> PathBuf {
    Path::new("/data/payload-segments").join(format!("{}.pseg", segment_id.simple()))
}

/// Three two-block segments whose blocks all stay live, the shape catalog
/// pressure consolidates. Returns every packed block with its payload.
pub(super) fn small_live_segments(database: &Arc<Mutex<AuthorityDatabase>>) -> Vec<(BlockId, Vec<u8>)> {
    let mut guard = database.lock().unwrap();
    let mut packed = Vec::new();
    for batch in 0..3_u32 {
        let mut ids = Vec::new();
        for member in 0..2_u32 {
            let payload = format!("consolidation-victim-{batch}-{member}").into_bytes();
            ids.push(guard.commit_block_payload_for_test(&payload).unwrap());
            packed.push((ids[ids.len() - 1], payload));
        }
        guard.compact_payload_blocks_for_test(&ids).unwrap();
    }
    packed
}

pub(super) fn segments_holding(blocks: &BlockStore, packed: &[(BlockId, Vec<u8>)]) -> Vec<PayloadSegmentReference> {
    let ids: BTreeSet<BlockId> = packed.iter().map(|(block_id, _)| *block_id).collect();
    blocks
        .installed_payload_manifest()
        .unwrap()
        .unwrap()
        .segments
        .into_iter()
        .filter(|reference| ids.contains(&reference.first_block_id))
        .collect()
}

/// Media loss of the newest durable CONTROL slot; the next open selects the
/// other one.
fn lose_newest_slot(vfs: &UnfinishableMarkVfs, slots: &[(u64, BTreeSet<Uuid>)]) {
    let newest = slots.iter().map(|(generation, _)| *generation).max().unwrap();
    let mut control = vfs
        .inner
        .open(Path::new("/data/CONTROL"), OpenRequest { write: true, ..OpenRequest::default() })
        .unwrap();
    control.write_all_at(((newest - 1) % 2) * 4096, &[0xa5; 64]).unwrap();
    sync_all_barrier(control.as_mut()).unwrap();
}

fn assert_catalog_resolves(
    vfs: &UnfinishableMarkVfs,
    database: &Arc<Mutex<AuthorityDatabase>>,
    packed: &[(BlockId, Vec<u8>)],
) {
    let guard = database.lock().unwrap();
    let installed = guard.block_store_shared().installed_payload_manifest().unwrap().unwrap();
    for reference in installed.segments {
        assert!(
            vfs.inner.metadata(&segment_path(reference.segment_id)).is_ok(),
            "the selected catalog names missing segment {}",
            reference.segment_id
        );
    }
    for (block_id, payload) in packed {
        assert_eq!(&guard.read_block_for_test(*block_id).unwrap(), payload);
    }
    drop(guard);
    assert_live_value(database, &live_key(), b"live-value");
}

/// Consolidation publishes its catalog into one CONTROL slot and then
/// stabilizes the other. A failure between those two writes leaves the
/// fallback slot naming every victim, so none may be gone: losing the newest
/// slot afterwards must select a catalog whose segments all exist.
#[test]
fn consolidation_failing_between_its_control_publications_keeps_every_victim() {
    let (seed, packed) = {
        let (vfs, database) = fixture();
        let packed = small_live_segments(&database);
        drop(database);
        vfs.inner.crash().unwrap();
        (vfs.inner.clone(), packed)
    };
    let consolidate = |database: &Arc<Mutex<AuthorityDatabase>>| {
        let mut guard = database.lock().unwrap();
        let blocks = guard.block_store_shared();
        let victims = segments_holding(&blocks, &packed);
        let retained_per_victim: Vec<Vec<BlockId>> = victims
            .iter()
            .map(|victim| {
                blocks
                    .payload_segment_store()
                    .unwrap()
                    .open_segment(victim.segment_id)
                    .unwrap()
                    .block_ids()
                    .collect()
            })
            .collect();
        let plan = ConsolidationPlan { victims, retained_per_victim };
        let (_replacement, reference) = OnlineGarbageCollector::new(test_budget())
            .build_consolidation_segment(&blocks, &plan)
            .unwrap()
            .unwrap();
        blocks.begin_relink_observation();
        let published =
            guard.online_gc_finish_consolidation(&plan.victims, &plan.retained_per_victim, Some(reference));
        blocks.end_relink_observation();
        let victim_ids: Vec<Uuid> = plan.victims.iter().map(|victim| victim.segment_id).collect();
        (victim_ids, published)
    };
    // Dry run on an identical fork: the publication's last CONTROL write is
    // the stabilizing one; fail exactly that write and every later one.
    let control_writes = {
        let vfs = UnfinishableMarkVfs::over(Arc::new(seed.fork_durable().unwrap()));
        let database = reopen(&vfs);
        vfs.control_writes.store(0, Ordering::SeqCst);
        let (victims, published) = consolidate(&database);
        assert_eq!(published.unwrap().victims as usize, victims.len());
        vfs.control_writes.load(Ordering::SeqCst)
    };
    assert!(control_writes >= 2, "publish plus stabilize: {control_writes}");
    let vfs = UnfinishableMarkVfs::over(Arc::new(seed.fork_durable().unwrap()));
    let database = reopen(&vfs);
    vfs.control_writes.store(0, Ordering::SeqCst);
    vfs.fail_control_writes_from.store(control_writes, Ordering::SeqCst);
    let (victims, published) = consolidate(&database);
    assert_eq!(victims.len(), 3);
    assert!(published.is_err(), "the stabilizing CONTROL write was injected to fail");
    drop(database);

    let database = reopen(&vfs);
    let blocks = database.lock().unwrap().block_store_shared();
    let slots = durable_slot_segments(&vfs, &blocks);
    assert!(
        slots.iter().any(|(_, segments)| victims.iter().all(|victim| segments.contains(victim))),
        "fixture: the fallback slot still names every victim: {slots:?}"
    );
    assert!(
        slots.iter().any(|(_, segments)| victims.iter().all(|victim| !segments.contains(victim))),
        "fixture: the newest slot names the consolidated catalog: {slots:?}"
    );
    for victim in &victims {
        assert!(vfs.inner.metadata(&segment_path(*victim)).is_ok(), "victim {victim} is gone");
    }
    assert_catalog_resolves(&vfs, &database, &packed);
    drop(database);
    lose_newest_slot(&vfs, &slots);
    let database = reopen(&vfs);
    let installed = database.lock().unwrap().block_store_shared().installed_payload_manifest().unwrap().unwrap();
    assert!(
        victims.iter().all(|victim| installed.segments.iter().any(|reference| reference.segment_id == *victim)),
        "recovery selected the fallback catalog that names the victims"
    );
    assert_catalog_resolves(&vfs, &database, &packed);
}

/// Readers resolve blocks off the authority mutex through segment handles
/// and catalog views captured before a publication. Consolidation therefore
/// parks its victims once both CONTROL slots name the new catalog, exactly
/// like the single-segment rewrite, instead of unlinking them in the hold;
/// the retirement floor then reclaims them, and losing the newest slot still
/// selects a catalog that resolves.
#[test]
fn consolidation_victims_stay_readable_through_handles_held_across_the_publication() {
    let (vfs, database) = fixture();
    let packed = small_live_segments(&database);
    let blocks = database.lock().unwrap().block_store_shared();
    let store = blocks.payload_segment_store().unwrap();
    let victims = segments_holding(&blocks, &packed);
    assert_eq!(victims.len(), 3);
    let handles: Vec<Arc<PayloadSegment>> = victims
        .iter()
        .map(|victim| store.open_segment_cached(victim.segment_id).unwrap())
        .collect();

    let mut collector =
        OnlineGarbageCollector::with_min_epoch_interval(test_budget(), Duration::from_millis(1));
    collector.set_catalog_pressure_floor_for_test(1);
    for _ in 0..64 {
        run_epoch(&mut collector, &database);
        if metrics(&collector).total_consolidations > 0 {
            break;
        }
    }
    assert!(metrics(&collector).total_consolidations > 0, "{:?}", metrics(&collector));
    let installed: BTreeSet<Uuid> = blocks
        .installed_payload_manifest()
        .unwrap()
        .unwrap()
        .segments
        .iter()
        .map(|reference| reference.segment_id)
        .collect();
    let retired: Vec<Uuid> = victims
        .iter()
        .map(|victim| victim.segment_id)
        .filter(|segment_id| !installed.contains(segment_id))
        .collect();
    assert!(retired.len() >= 2, "consolidation retired the small segments: {retired:?}");
    let slots = durable_slot_segments(&vfs, &blocks);
    assert_eq!(slots.len(), 2);
    assert!(
        slots.iter().all(|(_, segments)| retired.iter().all(|segment_id| !segments.contains(segment_id))),
        "both durable slots name the consolidated catalog: {slots:?}"
    );
    for (block_id, payload) in &packed {
        let handle = handles.iter().find(|handle| handle.contains(*block_id)).unwrap();
        let read = store.get_with_handle(handle, *block_id);
        assert!(
            matches!(&read, Ok(Some(bytes)) if bytes == payload),
            "a handle opened before the consolidation still reads its block: {read:?}"
        );
    }
    let parked = store.deferred_retirement_ids().unwrap();
    for segment_id in &retired {
        assert!(parked.contains(segment_id), "victim {segment_id} must be parked, not unlinked");
        assert!(vfs.inner.metadata(&segment_path(*segment_id)).is_ok());
    }

    store.age_deferred_retirements_for_test(Duration::from_secs(3600)).unwrap();
    store
        .sweep_deferred_retirement_with(crate::payload_segment::segment_retirement_delay())
        .unwrap();
    for segment_id in &retired {
        assert_eq!(
            vfs.inner.metadata(&segment_path(*segment_id)).unwrap_err().kind(),
            io::ErrorKind::NotFound,
            "the expired floor reclaims the parked victim"
        );
    }
    drop(handles);
    drop(blocks);
    drop(database);
    lose_newest_slot(&vfs, &slots);
    let database = reopen(&vfs);
    assert_catalog_resolves(&vfs, &database, &packed);
}
