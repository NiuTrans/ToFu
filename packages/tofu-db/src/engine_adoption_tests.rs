// Full-catalog publication, I/O isolation and crash durability contracts.
use super::*;
use std::sync::{atomic::{AtomicBool, Ordering}, mpsc, Condvar};
use std::time::Duration;

fn build_full_catalog(engine: &mut Engine) -> (BlockId, Vec<PayloadSegmentReference>) {
    let payload = b"historical packed content";
    let id = BlockId::for_payload(payload);
    let mut references = Vec::with_capacity(MAX_PAYLOAD_SEGMENTS);
    for index in 0..MAX_PAYLOAD_SEGMENTS {
        let segment = engine.blocks.payload_segment_store().unwrap()
            .create(Uuid::from_u128(index as u128 + 1), vec![id], |_| Ok(payload.to_vec())).unwrap();
        references.push(PayloadSegmentReference::new(segment.metadata(),
            segment.first_block_id(), segment.last_block_id()).unwrap());
    }
    engine.publish_payload_manifest(PayloadManifest::new(references.clone()).unwrap(), &[]).unwrap();
    (id, references)
}

fn full_catalog_engine() -> (Arc<DeterministicVfs>, Engine) {
    static SEED: std::sync::OnceLock<Arc<DeterministicVfs>> = std::sync::OnceLock::new();
    let seed = SEED.get_or_init(|| {
        let (vfs, mut engine) = initialized_simulated_engine();
        build_full_catalog(&mut engine);
        drop(engine);
        vfs
    });
    let vfs = Arc::new(seed.fork_durable().unwrap());
    let engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, engine)
}

fn prepare_full(engine: &mut Engine) -> Box<PreparedPackAdoption> {
    engine.commit_transaction(b"acknowledged", &[b"new packed content"]).unwrap();
    build_and_complete_background_pack(engine, &[b"new packed content"]);
    match engine.try_prepare_pack_adoption().unwrap() {
        PackAdoptionPreparation::Ready(prepared) => prepared,
        _ => panic!("full catalog must use the unlocked publication lane"),
    }
}

#[test]
fn full_catalog_adoption_reclaims_bounded_files_and_recovers() {
    let (vfs, mut engine) = full_catalog_engine();
    let old = BlockId::for_payload(b"historical packed content");
    let mut prepared = prepare_full(&mut engine);
    assert_eq!(engine.blocks.unpublished_background_segment_ids().unwrap().len(), 2);
    assert!(engine.plan_payload_segment_orphan().unwrap().candidate.is_none());
    assert_eq!(engine.require_physical_maintenance_available().unwrap_err().kind(), io::ErrorKind::WouldBlock);
    vfs.arm_fault(None).unwrap();
    publish_prepared_pack_adoption(&mut prepared).unwrap();
    let trace = vfs.trace().unwrap();
    // Retired sources are parked, never unlinked in the publication path,
    // and the orphan scan protects the parked files although no manifest
    // references them: pre-install snapshot readers always resolve.
    assert!(!trace.contains(&Operation::RemoveFile));
    assert_eq!(engine.payload_segment_count(), MAX_PAYLOAD_SEGMENTS);
    assert_eq!(vfs.read_directory(Path::new("/data/payload-segments")).unwrap().len(),
        MAX_PAYLOAD_SEGMENTS + 2);
    assert!(engine.plan_payload_segment_orphan().unwrap().candidate.is_none());
    // The deferred-retirement sweep performs the unlink once the floor
    // expires (forced here with a zero floor).
    let reclaimed = engine
        .blocks
        .sweep_deferred_segment_retirements(std::time::Duration::ZERO)
        .unwrap();
    assert!(reclaimed > 0);
    assert_eq!(vfs.trace().unwrap().iter().filter(|op| **op == Operation::RemoveFile).count(), 2);
    assert_eq!(vfs.read_directory(Path::new("/data/payload-segments")).unwrap().len(),
        MAX_PAYLOAD_SEGMENTS);
    vfs.arm_fault(None).unwrap();
    engine.finish_pack_adoption(prepared, Ok(())).unwrap();
    assert!(vfs.trace().unwrap().is_empty(), "finish must be memory-only");
    assert_eq!(engine.payload_segment_count(), MAX_PAYLOAD_SEGMENTS);
    drop(engine);
    vfs.crash().unwrap();
    let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(recovered.read_block(old).unwrap(), b"historical packed content");
    assert_eq!(recovered.read_block(BlockId::for_payload(b"new packed content")).unwrap(), b"new packed content");
    assert_eq!(recovered.state.durable_sequence, 1);
}

#[test]
fn full_catalog_adoption_crash_at_publication_boundaries_preserves_acknowledged_data() {
    let (vfs, mut engine) = full_catalog_engine();
    let mut prepared = prepare_full(&mut engine);
    vfs.arm_fault(None).unwrap();
    publish_prepared_pack_adoption(&mut prepared).unwrap();
    let trace = vfs.trace().unwrap();
    // Exercise sealing, both CONTROL writes, and each post-publication unlink.
    let points: Vec<_> = trace.iter().enumerate().filter_map(|(i, op)| {
        matches!(op, Operation::Write | Operation::Rename | Operation::RemoveFile)
            .then_some((trace[..=i].iter().filter(|item| *item == op).count(), *op))
    }).collect();
    drop(engine);
    for (number, operation) in points {
        let (vfs, mut engine) = full_catalog_engine();
        let old = BlockId::for_payload(b"historical packed content");
        let mut prepared = prepare_full(&mut engine);
        vfs.arm_operation_fault(operation, number,
            if operation == Operation::Write { FaultAction::ShortWrite(1) }
            else { FaultAction::ErrorBefore(io::ErrorKind::Other) }).unwrap();
        let result = publish_prepared_pack_adoption(&mut prepared);
        assert!(vfs.operation_fault_fired().unwrap());
        let _ = engine.finish_pack_adoption(prepared, result);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state.durable_sequence, 1, "fault {number}");
        assert_eq!(recovered.read_block(old).unwrap(), b"historical packed content");
        assert_eq!(recovered.read_block(BlockId::for_payload(b"new packed content")).unwrap(), b"new packed content");
    }
}

struct PausedOpen {
    inner: Arc<DeterministicVfs>,
    armed: AtomicBool,
    entered: mpsc::SyncSender<()>,
    released: Mutex<bool>,
    wake: Condvar,
}
impl Vfs for PausedOpen {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        if path.extension().is_some_and(|ext| ext == "pseg") && self.armed.swap(false, Ordering::AcqRel) {
            self.entered.send(()).unwrap();
            let (released, _) = self.wake.wait_timeout_while(self.released.lock().unwrap(),
                Duration::from_secs(10), |released| !*released).unwrap();
            if !*released { return Err(io::Error::new(io::ErrorKind::TimedOut, "test I/O gate expired")); }
        }
        self.inner.open(path, request)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.inner.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { self.inner.rename(source, destination) }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> { self.inner.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> { self.inner.sync_directory(path) }
    fn remove_file(&self, path: &Path) -> io::Result<()> { self.inner.remove_file(path) }
}

#[test]
fn paused_full_catalog_merge_does_not_block_foreground_reads_or_commits() {
    let (inner, seed_engine) = full_catalog_engine();
    drop(seed_engine);
    let (entered, receiver) = mpsc::sync_channel(1);
    let vfs = Arc::new(PausedOpen { inner, armed: AtomicBool::new(false), entered,
        released: Mutex::new(false), wake: Condvar::new() });
    let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let old = BlockId::for_payload(b"historical packed content");
    let mut prepared = prepare_full(&mut engine);
    let engine = Arc::new(Mutex::new(engine));
    vfs.armed.store(true, Ordering::Release);
    let publishing = std::thread::spawn(move || {
        let outcome = publish_prepared_pack_adoption(&mut prepared);
        (prepared, outcome)
    });
    let entered = receiver.recv_timeout(Duration::from_secs(2));
    let (done, progress) = mpsc::sync_channel(1);
    let foreground = Arc::clone(&engine);
    let writer = std::thread::spawn(move || {
        let mut engine = foreground.lock().unwrap();
        let read = engine.read_block(old);
        let write = engine.commit(b"foreground during merge");
        done.send((read, write)).unwrap();
    });
    let progressed = progress.recv_timeout(Duration::from_secs(2));
    *vfs.released.lock().unwrap() = true;
    vfs.wake.notify_all();
    writer.join().unwrap();
    let (prepared, outcome) = publishing.join().unwrap();
    entered.expect("merge must enter the paused disk operation");
    let (read, write) = progressed.expect("foreground must progress while disk merge is paused");
    assert_eq!(read.unwrap(), b"historical packed content");
    assert_eq!(write.unwrap(), 2);
    engine.lock().unwrap().finish_pack_adoption(prepared, outcome).unwrap();
}

#[test]
fn online_rotation_seals_without_foreground_payload_materialization() {
    let (vfs, mut engine) = initialized_simulated_engine();
    let _registration = engine.blocks.register_background_packer().unwrap();
    engine.attach_rotation_driver();
    engine.commit_transaction(b"small", &[b"sub MiB tail"]).unwrap();
    vfs.arm_fault(None).unwrap();
    engine.set_rotation_threshold_bytes_for_test(128);
    engine.commit(b"threshold-crossing ACK").unwrap();
    assert!(engine.state.sealed_log.is_none(), "foreground must not publish CONTROL");
    assert!(vfs.trace().unwrap().iter().all(|operation|
        matches!(operation, Operation::Write | Operation::SyncData)),
        "the accepted append may only write and synchronize its WAL");
    let rotation = match engine.take_prepared_rotation().unwrap().unwrap() {
        crate::engine::RotationWork::Fresh(prepared) => prepared,
        crate::engine::RotationWork::Deferred(..) => panic!("expected a fresh driver rotation"),
    };
    // The driver owns the durable handover. Its staged covered tail still
    // drains through the existing packer without foreground materialization.
    assert!(engine.state.sealed_log.is_some());
    assert_eq!(engine.wal.len(), 0);
    assert!(engine.blocks.pending_payload(BlockId::for_payload(b"sub MiB tail")).unwrap().is_some());
    assert!(engine.history_manifest.is_none());
    assert!(engine.blocks.background_drain_requested());
    assert!(engine.has_rotation_drain_pending_for_test());
    let (segment_id, ids) = engine.blocks.begin_background_pack(1, checkpoint_materialize_batch_bytes()).unwrap().unwrap();
    let segment = engine.blocks.payload_segment_store().unwrap().create(segment_id, ids, |id| engine.read_block(id)).unwrap();
    let reference = PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap();
    engine.blocks.note_packed(&segment.block_ids().collect::<Vec<_>>()).unwrap();
    engine.blocks.complete_background_pack(segment_id, reference).unwrap();
    let mut adoption = match engine.try_prepare_pack_adoption().unwrap() {
        PackAdoptionPreparation::Ready(prepared) => prepared, _ => panic!(),
    };
    let outcome = publish_prepared_pack_adoption(&mut adoption);
    engine.finish_pack_adoption(adoption, outcome).unwrap();
    assert!(!engine.has_rotation_drain_pending_for_test());
    // Successor commits remain admitted while the captured predecessor is
    // encoded and stay outside that predecessor's checkpoint.
    engine.commit(b"window").unwrap();
    let outcome = encode_rotation_segments(&rotation);
    engine.finish_rotation_checkpoint(rotation, outcome).unwrap();
    assert!(!engine.blocks.background_drain_requested());
    engine.commit(b"resumed").unwrap();
    assert_eq!(engine.state.durable_sequence, 4);
    assert_eq!(engine.state.checkpoint_sequence, 2);
    assert_eq!(
        engine.transaction_at(3).unwrap().unwrap().envelope.inline_payload,
        b"window"
    );
}

#[test]
fn paused_segment_handle_open_does_not_serialize_unrelated_reads() {
    let inner = prepared_simulated_vfs();
    let (entered, receiver) = mpsc::sync_channel(1);
    let vfs = Arc::new(PausedOpen { inner, armed: AtomicBool::new(false), entered,
        released: Mutex::new(false), wake: Condvar::new() });
    let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let blocks = engine.block_store_shared();
    let store = blocks.payload_segment_store().unwrap();
    let first_id = BlockId::for_payload(b"first");
    let second_id = BlockId::for_payload(b"second");
    let first = store.create(Uuid::new_v4(), vec![first_id], |_| Ok(b"first".to_vec())).unwrap();
    let second = store.create(Uuid::new_v4(), vec![second_id], |_| Ok(b"second".to_vec())).unwrap();
    vfs.armed.store(true, Ordering::Release);
    let slow_blocks = Arc::clone(&blocks);
    let slow = std::thread::spawn(move || slow_blocks.payload_segment_store().unwrap().get_with_handle(&first, first_id));
    let entered = receiver.recv_timeout(Duration::from_secs(2));
    let (sent, received) = mpsc::sync_channel(1);
    let fast = std::thread::spawn(move || sent.send(blocks.payload_segment_store().unwrap().get_with_handle(&second, second_id)).unwrap());
    let independent = received.recv_timeout(Duration::from_secs(2));
    *vfs.released.lock().unwrap() = true;
    vfs.wake.notify_all();
    fast.join().unwrap();
    assert_eq!(slow.join().unwrap().unwrap().unwrap(), b"first");
    entered.expect("first file must pause inside its cache-miss open");
    assert_eq!(independent.expect("second file must bypass slow first file").unwrap().unwrap(), b"second");
}

#[test]
fn window_restaging_of_covered_block_never_duplicates_the_pack_batch() {
    // 2026-09-20 generational-pending invariant: content addressing makes
    // the covered generation's copy THE block; a window-era fresh restage
    // of the same bytes (fresh CoW pages skip the durable probe) must not
    // duplicate it across generations — the pack batch's duplicate-ID
    // validation otherwise fails forever and the rotation never drains.
    let (_vfs, mut engine) = initialized_simulated_engine();
    let _registration = engine.blocks.register_background_packer().unwrap();
    engine.attach_rotation_driver();
    engine.commit_transaction(b"covered", &[b"shared-block"]).unwrap();
    let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
        RotationCheckpointPreparation::Ready(snapshot) => snapshot,
        other => panic!("expected ready rotation, got {other:?}"),
    };
    let (id, _) = engine.blocks.put_deferred_fresh(b"shared-block").unwrap();
    let (segment_id, ids) = engine
        .blocks
        .begin_background_pack(1, 1 << 20)
        .unwrap()
        .expect("staged blocks must form a batch");
    assert_eq!(ids.iter().filter(|candidate| **candidate == id).count(), 1);
    assert_eq!(engine.blocks.get(id).unwrap().as_slice(), b"shared-block");
    let _ = engine.blocks.fail_background_pack(segment_id);
    drop(snapshot);
}

/// Full-catalog history promotion replaces the smallest fitting segment.
/// Before 2026-10-30 the replaced source was left for orphan GC (the live
/// payload-segment leak); it now takes the parked retirement of every other
/// catalog publisher, after both CONTROL slots name the new catalog.
fn full_catalog_history(engine: &mut Engine) -> (BlockId, BlockId) {
    let root = engine.write_block(b"full catalog history root").unwrap();
    engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
    let old = engine.commit_transaction(b"old", &[b"promoted at the ceiling"]).unwrap().block_ids[0];
    indexed_rotate(engine);
    let recent = engine.commit_transaction(b"new", &[b"retained indexed bytes"]).unwrap().block_ids[0];
    indexed_rotate(engine);
    (old, recent)
}

fn catalog_ids(engine: &Engine) -> BTreeSet<Uuid> {
    engine.payload_segment_references().iter().map(|reference| reference.segment_id).collect()
}

fn segment_files(vfs: &DeterministicVfs) -> BTreeSet<Uuid> {
    vfs.read_directory(Path::new("/data/payload-segments")).unwrap().iter()
        .filter_map(|path| path.file_name()?.to_str()?.strip_suffix(".pseg").map(str::to_owned))
        .map(|name| Uuid::parse_str(&name).unwrap())
        .collect()
}

/// Every segment any durable CONTROL slot can select must exist on disk.
fn assert_durable_slots_resolve(vfs: &Arc<DeterministicVfs>, engine: &Engine, context: &str) {
    let mut control = ControlFile::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    control.read_current().unwrap();
    let files = segment_files(vfs);
    for state in control.selectable_states_for_test() {
        let Some(manifest_id) = state.payload_manifest_block_id else { continue };
        let manifest = PayloadManifest::decode(&engine.blocks.get(manifest_id).unwrap()).unwrap();
        for reference in manifest.segments {
            assert!(files.contains(&reference.segment_id),
                "{context}: CONTROL generation {} names missing segment {}", state.generation, reference.segment_id);
        }
    }
}

#[test]
fn full_catalog_history_compaction_parks_replaced_sources_after_both_slots_move() {
    let (vfs, mut engine) = full_catalog_engine();
    let (old, recent) = full_catalog_history(&mut engine);
    let before = catalog_ids(&engine);
    let files_before = segment_files(&vfs).len();
    engine.compact_history(1).unwrap();
    let after = catalog_ids(&engine);
    assert_eq!(after.len(), MAX_PAYLOAD_SEGMENTS);
    let replaced = before.difference(&after).copied().collect::<Vec<_>>();
    assert_eq!(replaced.len(), 1, "one promotion batch replaces one catalog source");
    assert!(engine.control.lock().unwrap()
        .slots_name_only_payload_manifest(engine.state.payload_manifest_block_id),
        "both CONTROL slots must name the new catalog before the source is parked");
    assert_eq!(engine.blocks.payload_segment_store().unwrap().deferred_retirement_ids().unwrap(), replaced);
    assert_eq!(segment_files(&vfs).len(), files_before + 1, "parked source keeps its reader floor");
    assert_durable_slots_resolve(&vfs, &engine, "after compaction");
    engine.blocks.sweep_deferred_segment_retirements(Duration::ZERO).unwrap();
    assert_eq!(segment_files(&vfs).len(), files_before, "the sweep reclaims the replaced source");
    assert_eq!(engine.read_block(old).unwrap(), b"promoted at the ceiling");
    assert_eq!(engine.read_block(recent).unwrap(), b"retained indexed bytes");
    drop(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    assert_durable_slots_resolve(&vfs, &reopened, "after reopen");
    assert_eq!(reopened.read_block(old).unwrap(), b"promoted at the ceiling");
    assert_eq!(reopened.read_block(recent).unwrap(), b"retained indexed bytes");
}

/// Crash matrix for the stabilized full-catalog promotion. Faults before the
/// first CONTROL write cannot publish or park anything (the promotion build
/// itself is covered by `full_catalog_history_promotion_faults_keep_the_acknowledged_source`
/// and the indexed compaction matrices). From that write on, every durable
/// operation (write, sync, rename, unlink) of both CONTROL publications, the
/// catalog install and parking is failed once (error, short write, lost sync),
/// as in the full-catalog adoption matrix, followed by an immediate zero-floor
/// sweep. The read-only stats and index reads of the first install after open
/// (about 8k segment opens in this fixture) cannot change durable state and
/// are not enumerated. Recovery must keep every acknowledged payload, and no
/// CONTROL slot may name a segment file that is gone.
#[test]
fn every_full_catalog_history_compaction_fault_keeps_slot_named_segments() {
    static HISTORY_SEED: std::sync::OnceLock<(Arc<DeterministicVfs>, BlockId, BlockId)> =
        std::sync::OnceLock::new();
    let (seed, old, recent) = HISTORY_SEED.get_or_init(|| {
        let (vfs, mut engine) = full_catalog_engine();
        let (old, recent) = full_catalog_history(&mut engine);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        (vfs, old, recent)
    }).clone();
    let fork = |seed: &Arc<DeterministicVfs>| {
        let vfs = Arc::new(seed.fork_durable().unwrap());
        let engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, engine)
    };
    let trace = {
        let (vfs, mut engine) = fork(&seed);
        vfs.arm_fault(None).unwrap();
        engine.compact_history(1).unwrap();
        vfs.trace().unwrap()
    };
    let first_control_write = (0..trace.len()).find(|&index| {
        trace[index] == Operation::Write
            && !matches!(index.checked_sub(1).map(|previous| trace[previous]),
                Some(Operation::Open | Operation::Write))
            && trace.get(index + 1) == Some(&Operation::SyncAll)
    }).expect("compaction publishes CONTROL");
    let mut cases = 0;
    for (index, operation) in trace.iter().enumerate().skip(first_control_write) {
        if !matches!(operation, Operation::Write | Operation::SyncData | Operation::SyncAll
            | Operation::SyncDirectory | Operation::Rename | Operation::RemoveFile)
        {
            continue;
        }
        let occurrence = trace[..=index].iter().filter(|candidate| *candidate == operation).count();
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(3)); }
        if matches!(operation, Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let (vfs, mut engine) = fork(&seed);
            vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
            let _ = engine.compact_history(1);
            assert!(vfs.operation_fault_fired().unwrap(), "unexercised {operation:?}#{occurrence} {action:?}");
            let _ = engine.blocks.sweep_deferred_segment_retirements(Duration::ZERO);
            // The durable image is the crash; the faulted engine and its
            // volume are released without touching it.
            let crashed = Arc::new(vfs.fork_durable().unwrap());
            drop(engine);
            drop(vfs);
            let reopened = Engine::open_with_vfs(Path::new("/data"), crashed.clone())
                .unwrap_or_else(|error| panic!("{operation:?}#{occurrence} {action:?}: {error}"));
            let context = format!("{operation:?}#{occurrence} {action:?}");
            assert_durable_slots_resolve(&crashed, &reopened, &context);
            assert_eq!(reopened.read_block(old).unwrap(), b"promoted at the ceiling", "{context}");
            assert_eq!(reopened.read_block(recent).unwrap(), b"retained indexed bytes", "{context}");
            cases += 1;
        }
    }
    assert!(cases >= 12, "both CONTROL publications must be faulted, got {cases}");
}

/// Bytes and files one history compaction round moves, by owner directory.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct RoundIo {
    segment_bytes_written: u64,
    segment_files_created: u64,
    segment_bytes_read: u64,
    loose_bytes_written: u64,
    loose_files_created: u64,
    control_bytes_written: u64,
    other_bytes_written: u64,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum IoOwner { Segment, Loose, Control, Other }

fn io_owner(path: &Path) -> IoOwner {
    if path.components().any(|part| part.as_os_str() == "payload-segments") { IoOwner::Segment }
    else if path.components().any(|part| part.as_os_str() == "blocks") { IoOwner::Loose }
    else if path.file_name().is_some_and(|name| name == "CONTROL") { IoOwner::Control }
    else { IoOwner::Other }
}

/// Counts written/read bytes and published files around the deterministic volume.
struct CountingVfs { inner: Arc<DeterministicVfs>, io: Arc<Mutex<RoundIo>> }
impl Vfs for CountingVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let inner = self.inner.open(path, request)?;
        Ok(Box::new(CountingFile { inner, owner: io_owner(path), io: Arc::clone(&self.io) }))
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.inner.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        self.inner.rename(source, destination)?;
        let mut io = self.io.lock().unwrap();
        match io_owner(destination) {
            IoOwner::Segment => io.segment_files_created += 1,
            IoOwner::Loose => io.loose_files_created += 1,
            IoOwner::Control | IoOwner::Other => {}
        }
        Ok(())
    }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> { self.inner.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> { self.inner.sync_directory(path) }
    fn remove_file(&self, path: &Path) -> io::Result<()> { self.inner.remove_file(path) }
}
struct CountingFile { inner: Box<dyn VfsFile>, owner: IoOwner, io: Arc<Mutex<RoundIo>> }
impl VfsFile for CountingFile {
    fn len(&self) -> io::Result<u64> { self.inner.len() }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        let bytes = self.inner.read_all(maximum)?;
        if self.owner == IoOwner::Segment { self.io.lock().unwrap().segment_bytes_read += bytes.len() as u64; }
        Ok(bytes)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)?;
        if self.owner == IoOwner::Segment { self.io.lock().unwrap().segment_bytes_read += bytes.len() as u64; }
        Ok(())
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.inner.write_all_at(offset, bytes)?;
        let written = bytes.len() as u64;
        let mut io = self.io.lock().unwrap();
        match self.owner {
            IoOwner::Segment => io.segment_bytes_written += written,
            IoOwner::Loose => io.loose_bytes_written += written,
            IoOwner::Control => io.control_bytes_written += written,
            IoOwner::Other => io.other_bytes_written += written,
        }
        Ok(())
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> { self.inner.set_len(length) }
    fn sync_data(&mut self) -> io::Result<()> { self.inner.sync_data() }
    fn sync_all(&mut self) -> io::Result<()> { self.inner.sync_all() }
    fn try_lock_exclusive(&mut self) -> io::Result<()> { self.inner.try_lock_exclusive() }
}

/// Seeds a sized full catalog with one payload-directory barrier for the whole
/// batch. The sealed files and the resulting durable image are unchanged; the
/// simulator's per-barrier directory copy would otherwise be quadratic.
struct SeedVfs(Arc<DeterministicVfs>);
impl Vfs for SeedVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> { self.0.open(path, request) }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.0.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { self.0.rename(source, destination) }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> { self.0.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.0.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.0.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.0.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        if path.ends_with("payload-segments") { return Ok(()); }
        self.0.sync_directory(path)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> { self.0.remove_file(path) }
}

/// Catalog segments hold 32 blocks of 128 bytes; each retired history source
/// carries one 128-byte block. The 32:1 ratio of the smallest fitting segment
/// to one source's promotion mirrors the live volume (~136 MiB : 4 MiB).
const SIZED_CATALOG_BLOCKS: usize = 32;
const SIZED_BLOCK_BYTES: usize = 128;
const SIZED_RETIRED_SOURCES: usize = 8;

struct SizedHistory {
    vfs: Arc<DeterministicVfs>,
    promoted: Vec<(BlockId, Vec<u8>)>,
    catalog: Vec<(BlockId, Vec<u8>)>,
    retained: BlockId,
}

fn sized_payload(tag: u8, index: usize) -> Vec<u8> {
    let mut payload = vec![tag; SIZED_BLOCK_BYTES];
    payload[..8].copy_from_slice(&(index as u64).to_le_bytes());
    payload
}

/// `catalog_segments` sized segments (the full ceiling or an empty catalog),
/// then eight retired indexed history sources and one retained source.
fn sized_history(catalog_segments: usize) -> SizedHistory {
    let (vfs, mut engine) = initialized_simulated_engine();
    let catalog: Vec<_> = (0..SIZED_CATALOG_BLOCKS).filter(|_| catalog_segments > 0).map(|index| {
        let payload = sized_payload(0xc5, index);
        (BlockId::for_payload(&payload), payload)
    }).collect();
    if catalog_segments > 0 {
        let by_id: BTreeMap<_, _> = catalog.iter().cloned().collect();
        let seeding = crate::payload_segment::PayloadSegmentStore::open_with_vfs(
            Path::new("/data"), Arc::new(SeedVfs(vfs.clone()))).unwrap();
        let references = (0..catalog_segments).map(|index| {
            let segment = seeding.create(Uuid::from_u128(index as u128 + 1),
                by_id.keys().copied().collect(), |id| Ok(by_id[&id].clone())).unwrap();
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap()
        }).collect::<Vec<_>>();
        drop(seeding);
        sync_directory_barrier(vfs.as_ref(), Path::new("/data/payload-segments")).unwrap();
        engine.publish_payload_manifest(PayloadManifest::new(references).unwrap(), &[]).unwrap();
    }
    let root = engine.write_block(b"sized history root").unwrap();
    engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
    let promoted = (0..SIZED_RETIRED_SOURCES).map(|source| {
        let payload = sized_payload(0x5c, source);
        let id = engine.commit_transaction(format!("source {source}").as_bytes(), &[&payload]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        (id, payload)
    }).collect();
    let retained = engine.commit_transaction(b"retained", &[b"retained sized history bytes"]).unwrap().block_ids[0];
    indexed_rotate(&mut engine);
    std::mem::forget(engine);
    vfs.crash().unwrap();
    SizedHistory { vfs, promoted, catalog, retained }
}

fn sized_full_catalog_history() -> &'static SizedHistory {
    static SEED: std::sync::OnceLock<SizedHistory> = std::sync::OnceLock::new();
    SEED.get_or_init(|| sized_history(MAX_PAYLOAD_SEGMENTS))
}

struct MeasuredRounds { rounds: Vec<RoundIo>, smallest_segment_file_bytes: u64, final_segments: usize }

/// Runs automatic-sized compaction rounds (one launch slice each, exactly as
/// `AuthorityDatabase::try_prepare_history_compaction` sizes them) and counts
/// only the unlocked encode/publish phase of every round.
fn measure_history_rounds(seed: &SizedHistory, batch_bytes: u64) -> MeasuredRounds {
    let io = Arc::new(Mutex::new(RoundIo::default()));
    let vfs = Arc::new(CountingVfs { inner: Arc::new(seed.vfs.fork_durable().unwrap()), io: Arc::clone(&io) });
    let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    engine.set_checkpoint_materialize_batch_bytes(batch_bytes).unwrap();
    // The first install after open scans every index once; keep it out of the rounds.
    if let Some(manifest) = engine.payload_manifest.clone() {
        engine.blocks.install_payload_manifest(manifest).unwrap();
    }
    let smallest_segment_file_bytes = engine.payload_segment_references().iter()
        .map(|reference| reference.file_bytes).min().unwrap_or(0);
    let slice = (batch_bytes / HISTORY_SEGMENT_MAX_BYTES as u64).max(1) as usize;
    let mut rounds = Vec::new();
    while engine.history_segment_count() > 1 {
        let maximum = engine.history_segment_count().saturating_sub(slice).max(1);
        let prepared = engine.try_prepare_history_compaction(maximum).unwrap().unwrap();
        *io.lock().unwrap() = RoundIo::default();
        let outcome = encode_history_compaction(&prepared);
        rounds.push(*io.lock().unwrap());
        engine.finish_history_compaction(prepared, outcome).unwrap();
    }
    for (id, payload) in seed.promoted.iter().chain(&seed.catalog) {
        assert_eq!(engine.read_block(*id).unwrap(), *payload);
    }
    assert_eq!(engine.read_block(seed.retained).unwrap(), b"retained sized history bytes");
    MeasuredRounds { rounds, smallest_segment_file_bytes, final_segments: engine.payload_segment_count() }
}

fn rounds_json(measured: &MeasuredRounds) -> serde_json::Value {
    let total = |field: fn(&RoundIo) -> u64| measured.rounds.iter().map(field).sum::<u64>();
    serde_json::json!({
        "rounds": measured.rounds.len(),
        "smallest_segment_file_bytes": measured.smallest_segment_file_bytes,
        "final_segments": measured.final_segments,
        "segment_bytes_written": total(|io| io.segment_bytes_written),
        "segment_files_created": total(|io| io.segment_files_created),
        "segment_bytes_read": total(|io| io.segment_bytes_read),
        "loose_bytes_written": total(|io| io.loose_bytes_written),
        "loose_files_created": total(|io| io.loose_files_created),
        "control_bytes_written": total(|io| io.control_bytes_written),
        "other_bytes_written": total(|io| io.other_bytes_written),
        "per_round_segment_bytes_written": measured.rounds.iter().map(|io| io.segment_bytes_written).collect::<Vec<_>>(),
        "per_round_segment_files_created": measured.rounds.iter().map(|io| io.segment_files_created).collect::<Vec<_>>(),
    })
}

/// Write cost of one automatic round is the round's promoted data plus at most
/// one existing-segment rewrite. The headroom arm retires the identical history
/// into an empty catalog, so its round writes exactly the promoted segment;
/// the ceiling arm may add only the smallest segment's file on top of that.
/// Before 2026-10-31 every retired source rewrote its own smallest fitting
/// segment: the 32 MiB round sealed 8 files / 47,778 bytes here (8 MiB: 2 per
/// round), and below the ceiling every source took its own catalog slot.
#[test]
fn history_compaction_round_rewrites_at_most_one_catalog_segment() {
    const MIB: u64 = 1024 * 1024;
    let full = sized_full_catalog_history();
    let empty = sized_history(0);
    let mut report = serde_json::Map::new();
    // 32 MiB is the live override; 8 MiB is the probe value on network mounts.
    for batch in [32 * MIB, 8 * MIB] {
        let slice = (batch / HISTORY_SEGMENT_MAX_BYTES as u64) as usize;
        let ceiling = measure_history_rounds(full, batch);
        let headroom = measure_history_rounds(&empty, batch);
        assert_eq!(ceiling.rounds.len(), SIZED_RETIRED_SOURCES / slice);
        assert_eq!(headroom.rounds.len(), ceiling.rounds.len());
        for (round, (at_ceiling, with_headroom)) in ceiling.rounds.iter().zip(&headroom.rounds).enumerate() {
            assert_eq!(with_headroom.segment_files_created, 1, "{batch}: round {round} below the ceiling");
            assert_eq!(at_ceiling.segment_files_created, 1, "{batch}: round {round} at the ceiling");
            assert!(at_ceiling.segment_bytes_written
                <= ceiling.smallest_segment_file_bytes + with_headroom.segment_bytes_written,
                "{batch}: round {round} wrote {at_ceiling:?}");
            assert_eq!(at_ceiling.loose_files_created, with_headroom.loose_files_created,
                "{batch}: round {round} spilled carried blocks to loose files");
        }
        assert_eq!(ceiling.final_segments, MAX_PAYLOAD_SEGMENTS);
        assert_eq!(headroom.final_segments, headroom.rounds.len());
        report.insert(format!("ceiling_{}mib", batch / MIB), rounds_json(&ceiling));
        report.insert(format!("headroom_{}mib", batch / MIB), rounds_json(&headroom));
    }
    println!("HISTORY_WRITE_AMP={}", serde_json::Value::Object(report));
}

/// Operator compaction streams the whole retired history group by group: one
/// group keeps at most the launch materialization budget of source bytes
/// resident (minimum one source), so a one-byte budget keeps the former
/// one-segment-per-source shape and a 32 MiB budget packs the slice once.
#[test]
fn operator_history_compaction_groups_sources_within_the_materialization_budget() {
    let seed = sized_history(0);
    for (batch, segments) in [(1, SIZED_RETIRED_SOURCES), (32 * 1024 * 1024, 1)] {
        let vfs = Arc::new(seed.vfs.fork_durable().unwrap());
        let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        engine.set_checkpoint_materialize_batch_bytes(batch).unwrap();
        assert_eq!(engine.compact_history(1).unwrap().retired_segments as usize, SIZED_RETIRED_SOURCES);
        assert_eq!(engine.payload_segment_count(), segments, "budget {batch}");
        for (id, payload) in &seed.promoted {
            assert_eq!(engine.read_block(*id).unwrap(), *payload);
        }
        assert_eq!(engine.read_block(seed.retained).unwrap(), b"retained sized history bytes");
    }
}
