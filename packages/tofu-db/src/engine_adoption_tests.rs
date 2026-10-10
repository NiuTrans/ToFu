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
    // 2026-09-20 generational-pending contract: the soft rotation prepares
    // immediately — the staged tail rotates into the covered generation and
    // the drain rides the packer; no foreground materialization, and the
    // commit that crossed the threshold gets classified backpressure.
    let crossed = engine.drain_staged_then_checkpoint(false).unwrap_err();
    assert!(is_rotation_backpressure(&crossed));
    // Durable handover performs CONTROL/empty-WAL barriers, but never
    // materializes the carried payload on the foreground thread.
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
    let rotation = match engine.take_prepared_rotation().unwrap().unwrap() {
        crate::engine::RotationWork::Fresh(prepared) => prepared,
        crate::engine::RotationWork::Deferred(..) => {
            panic!("a first rotation must arrive fresh")
        }
    };
    // Window commits are ADMITTED during the encode (the old park fence is
    // gone); the install's final segment absorbs them exactly.
    engine.commit(b"window").unwrap();
    let outcome = encode_rotation_segments(&rotation);
    engine.finish_rotation_checkpoint(rotation, outcome).unwrap();
    assert!(!engine.blocks.background_drain_requested());
    engine.commit(b"resumed").unwrap();
    assert_eq!(engine.state.durable_sequence, 3);
    assert_eq!(engine.state.checkpoint_sequence, 1);
    assert_eq!(
        engine.transaction_at(2).unwrap().unwrap().envelope.inline_payload,
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
