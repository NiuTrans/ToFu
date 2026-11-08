//! Two-mark epoch collector tests (docs/TOFUDB_ONLINE_GC.md). The fault
//! injection matrix mirroring the offline `every_*_fault` families lives in
//! `tests_fault.rs`.

use super::*;
use crate::authority::AuthorityDatabase;
use crate::entity::EntityKey;
use crate::vfs::{sync_all_barrier, sync_directory_barrier, DeterministicVfs, OpenRequest};

pub(super) fn test_budget() -> AuthorityGarbageCollectionBudget {
    AuthorityGarbageCollectionBudget::conservative()
}

pub(super) fn authority() -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, Arc::new(Mutex::new(database)))
}

pub(super) fn run_epoch(
    collector: &mut OnlineGarbageCollector,
    database: &Arc<Mutex<AuthorityDatabase>>,
) {
    let stop = AtomicBool::new(false);
    let outcome = collector.run_epoch(database, &stop).unwrap();
    assert_eq!(outcome, OnlineGarbageCollectionOutcome::EpochComplete);
}

pub(super) fn metrics(collector: &OnlineGarbageCollector) -> OnlineGarbageCollectionMetrics {
    collector.handle().metrics()
}

pub(super) fn create_temporary_block_file(vfs: &DeterministicVfs, block_id: BlockId) -> PathBuf {
    let shard = block_id.to_hex()[..2].to_owned();
    let shard_path = Path::new("/data/blocks").join(shard);
    let temporary_path = shard_path.join(crate::block::temporary_block_file_name(
        uuid::Uuid::from_bytes([0x3c; 16]),
    ));
    let mut file = vfs
        .open(
            &temporary_path,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )
        .unwrap();
    file.write_all_at(0, b"torn-block-prefix").unwrap();
    sync_all_barrier(file.as_mut()).unwrap();
    sync_directory_barrier(vfs, &shard_path).unwrap();
    temporary_path
}

pub(super) fn assert_live_value(
    database: &Arc<Mutex<AuthorityDatabase>>,
    key: &EntityKey,
    value: &[u8],
) {
    let database = database.lock().unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    assert_eq!(
        database.entity_get(&mut transaction, key).unwrap(),
        Some(value.to_vec())
    );
}

#[test]
fn first_epoch_confirms_nothing_and_second_reclaims_orphan_and_temp_file() {
    let (vfs, database) = authority();
    let key = EntityKey::new(7, 11, "record", b"live").unwrap();
    {
        let mut database_guard = database.lock().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
    }
    let orphan = {
        let database_guard = database.lock().unwrap();
        database_guard
            .write_orphan_block_for_test(b"unreferenced-block")
            .unwrap()
    };
    let temp_path = create_temporary_block_file(vfs.as_ref(), orphan);
    let mut collector = OnlineGarbageCollector::new(test_budget());

    run_epoch(&mut collector, &database);
    {
        let database_guard = database.lock().unwrap();
        assert!(database_guard.read_block_for_test(orphan).is_ok());
    }
    assert!(vfs.metadata(&temp_path).is_ok());
    assert_eq!(metrics(&collector).total_reclaimed_blocks, 0);

    run_epoch(&mut collector, &database);
    {
        let database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard
                .read_block_for_test(orphan)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotFound
        );
    }
    assert_eq!(
        vfs.metadata(&temp_path).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
    let final_metrics = metrics(&collector);
    assert_eq!(final_metrics.epochs_completed, 2);
    assert_eq!(final_metrics.total_reclaimed_blocks, 1);
    assert_eq!(final_metrics.total_temporary_files_removed, 1);
    assert_live_value(&database, &key, b"live-value");
}

#[test]
fn snapshot_pin_root_marking_survives_history_retirement() {
    let (_vfs, database) = authority();
    let key = EntityKey::new(7, 11, "record", b"pinned").unwrap();
    let replica = {
        let mut database_guard = database.lock().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, key.clone(), b"value-v1".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
        database_guard.checkpoint_for_test().unwrap();
        // Pin the v1 root before the overwrite creates its replacement.
        let replica = database_guard.build_read_replica().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, key.clone(), b"value-v2".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
        database_guard.checkpoint_for_test().unwrap();
        // Retire every history segment the v1 commit was reachable through:
        // after this, the replica pin is the only thing keeping v1 alive.
        database_guard.compact_history(1).unwrap();
        replica
    };
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    // The pinned snapshot still resolves v1: its root was part of every mark.
    {
        let mut transaction = replica.begin(7, 11).unwrap();
        assert_eq!(
            replica.entity_get(&mut transaction, &key).unwrap(),
            Some(b"value-v1".to_vec())
        );
    }
    assert_live_value(&database, &key, b"value-v2");
    let reclaimed_while_pinned = metrics(&collector).total_reclaimed_bytes;

    drop(replica);
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    let reclaimed_after_drop = metrics(&collector).total_reclaimed_bytes;
    assert!(
        reclaimed_after_drop > reclaimed_while_pinned,
        "dropping the pin makes the old snapshot's pages collectible"
    );
}

#[test]
fn relink_fence_skips_candidates_rereferenced_since_the_snapshot() {
    let (_vfs, database) = authority();
    let orphan = {
        let database_guard = database.lock().unwrap();
        database_guard
            .write_orphan_block_for_test(b"fence-candidate")
            .unwrap()
    };
    // Arm the fence exactly like an epoch snapshot hold would.
    let capture = {
        let database_guard = database.lock().unwrap();
        database_guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap()
    };
    // A commit path re-references the orphan after the snapshot: the write
    // path observes existing durable content and records the relink.
    {
        let database_guard = database.lock().unwrap();
        database_guard
            .block_store_shared()
            .put(b"fence-candidate")
            .unwrap();
    }
    let removal = {
        let mut database_guard = database.lock().unwrap();
        database_guard.online_gc_delete_loose(&[orphan], &mut Vec::new()).unwrap()
    };
    assert_eq!(removal.blocks_removed, 0);
    drop(capture);
    {
        let database_guard = database.lock().unwrap();
        assert!(database_guard.read_block_for_test(orphan).is_ok());
    }
    // The next epoch re-arms the fence; without a fresh relink the victim
    // deletes normally.
    {
        let database_guard = database.lock().unwrap();
        database_guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    }
    let removal = {
        let mut database_guard = database.lock().unwrap();
        database_guard.online_gc_delete_loose(&[orphan], &mut Vec::new()).unwrap()
    };
    assert_eq!(removal.blocks_removed, 1);
}

/// The relink fence is exact over one epoch window and has no cap. Under the
/// former 16,384-entry cap the 16,385th distinct dedupe hit after a capture
/// set an overflow flag, and every deletion hold of that epoch returned
/// `WouldBlock`: a store that busy never reclaimed anything.
#[test]
fn relink_fence_keeps_every_dedupe_hit_of_an_epoch_window_and_still_filters() {
    const DEDUPE_HITS: usize = 16_385;
    let (_vfs, database) = authority();
    let payloads = (0..DEDUPE_HITS)
        .map(|index| format!("fence-dedupe-{index}").into_bytes())
        .collect::<Vec<_>>();
    let (garbage, relinked) = {
        let mut database_guard = database.lock().unwrap();
        for chunk in payloads.chunks(4_096) {
            database_guard.commit_block_payloads_for_test(chunk).unwrap();
        }
        database_guard.checkpoint_for_test().unwrap();
        (
            database_guard.write_orphan_block_for_test(b"fence-garbage").unwrap(),
            database_guard.write_orphan_block_for_test(b"fence-relinked").unwrap(),
        )
    };
    let blocks = database.lock().unwrap().block_store_shared();
    assert_eq!(blocks.relink_fence_len_for_test(), None, "nothing records outside an epoch window");
    database.lock().unwrap().online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    {
        let mut database_guard = database.lock().unwrap();
        for chunk in payloads.chunks(4_096) {
            database_guard.commit_block_payloads_for_test(chunk).unwrap();
        }
        database_guard.commit_block_payload_for_test(b"fence-relinked").unwrap();
    }
    assert_eq!(blocks.relink_fence_len_for_test(), Some(DEDUPE_HITS + 1));
    let mut unlinked = Vec::new();
    let removal = database
        .lock()
        .unwrap()
        .online_gc_delete_loose(&[garbage, relinked], &mut unlinked)
        .unwrap();
    assert_eq!((removal.blocks_removed, unlinked), (1, vec![garbage]));
    assert!(database.lock().unwrap().read_block_for_test(relinked).is_ok());

    // A whole epoch closes its window and releases the fence.
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    assert_eq!(blocks.relink_fence_len_for_test(), None);
    database.lock().unwrap().commit_block_payload_for_test(&payloads[0]).unwrap();
    assert_eq!(blocks.relink_fence_len_for_test(), None);
}

#[test]
fn segment_rewrite_requires_two_epoch_confirmation() {
    let (_vfs, database) = authority();
    let (live_id, dead_id) = {
        let mut database_guard = database.lock().unwrap();
        let live_payload = b"segment-live-payload".to_vec();
        let dead_payload = b"segment-dead-payload".to_vec();
        let live_id = database_guard
            .commit_block_payload_for_test(&live_payload)
            .unwrap();
        let dead_id = database_guard
            .write_orphan_block_for_test(&dead_payload)
            .unwrap();
        database_guard
            .compact_payload_blocks_for_test(&[live_id, dead_id])
            .unwrap();
        (live_id, dead_id)
    };
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    assert_eq!(metrics(&collector).total_segments_rewritten, 0);
    {
        let database_guard = database.lock().unwrap();
        assert!(database_guard.read_block_for_test(dead_id).is_ok());
    }
    run_epoch(&mut collector, &database);
    let final_metrics = metrics(&collector);
    assert_eq!(final_metrics.total_segments_rewritten, 1);
    let database_guard = database.lock().unwrap();
    assert!(database_guard.read_block_for_test(live_id).is_ok());
    assert_eq!(
        database_guard
            .read_block_for_test(dead_id)
            .unwrap_err()
            .kind(),
        io::ErrorKind::NotFound
    );
}

#[test]
fn live_loose_blocks_compact_into_a_segment_in_one_epoch() {
    let (_vfs, database) = authority();
    let payloads = oversized_payloads_in_one_shard(
        crate::authority_gc::MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS,
    );
    let block_ids = {
        let mut database_guard = database.lock().unwrap();
        database_guard
            .commit_block_payloads_for_test(&payloads)
            .unwrap()
    };
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    let final_metrics = metrics(&collector);
    assert_eq!(
        final_metrics.total_compacted_blocks,
        crate::authority_gc::MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS as u64
    );
    let database_guard = database.lock().unwrap();
    assert_eq!(database_guard.payload_segment_count_for_test(), 1);
    for block_id in block_ids {
        assert!(database_guard.read_block_for_test(block_id).is_ok());
    }
}

const SCAN_PROBE_PAYLOAD_BYTES: usize = 16 * 1024;
const SCAN_PROBE_FILE_BYTES: u64 =
    SCAN_PROBE_PAYLOAD_BYTES as u64 + crate::block::MIN_BLOCK_FILE_BYTES;
const SCAN_PROBE_PLANNED_SHARD: u8 = 0xf0;

/// Deterministic 16 KiB payloads whose block IDs land in accepted shards.
fn scan_probe_payloads(tag: &[u8], count: usize, accept: impl Fn(u8) -> bool) -> Vec<Vec<u8>> {
    let mut payloads = Vec::with_capacity(count);
    let mut value = 0_u32;
    while payloads.len() < count {
        let mut payload = Vec::with_capacity(SCAN_PROBE_PAYLOAD_BYTES);
        payload.extend_from_slice(tag);
        payload.extend_from_slice(&value.to_le_bytes());
        payload.resize(SCAN_PROBE_PAYLOAD_BYTES, (value % 251) as u8);
        if accept(BlockId::for_payload(&payload).0[0]) {
            payloads.push(payload);
        }
        value += 1;
    }
    payloads
}

fn scan_probe_ids(payloads: &[Vec<u8>]) -> Vec<BlockId> {
    payloads.iter().map(|payload| BlockId::for_payload(payload)).collect()
}

fn loose_block_path(block_id: BlockId) -> PathBuf {
    let hexadecimal = block_id.to_hex();
    Path::new("/data/blocks")
        .join(&hexadecimal[..2])
        .join(format!("{hexadecimal}.blk"))
}

/// Counts loose block opens and attributes payload-segment reads to the loose
/// block opened most recently while counting is armed.
#[derive(Default)]
struct ScanProbeLog {
    counting: AtomicBool,
    last_block: Mutex<Option<PathBuf>>,
    block_opens: Mutex<BTreeMap<PathBuf, u64>>,
    segment_reads_after: Mutex<BTreeMap<PathBuf, u64>>,
}

impl ScanProbeLog {
    fn opens(&self, block_id: BlockId) -> u64 {
        self.block_opens.lock().unwrap().get(&loose_block_path(block_id)).copied().unwrap_or(0)
    }

    fn segment_reads_after(&self, block_ids: &[BlockId]) -> u64 {
        let reads = self.segment_reads_after.lock().unwrap();
        block_ids
            .iter()
            .map(|block_id| reads.get(&loose_block_path(*block_id)).copied().unwrap_or(0))
            .sum()
    }
}

struct ScanProbeVfs {
    inner: Arc<DeterministicVfs>,
    log: Arc<ScanProbeLog>,
}

impl Vfs for ScanProbeVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        let file = self.inner.open(path, request)?;
        let loose_block = path.parent().and_then(Path::parent) == Some(Path::new("/data/blocks"))
            && path.extension().is_some_and(|extension| extension == "blk");
        if request.read && loose_block && self.log.counting.load(Ordering::Acquire) {
            *self.log.block_opens.lock().unwrap().entry(path.to_path_buf()).or_default() += 1;
            *self.log.last_block.lock().unwrap() = Some(path.to_path_buf());
        }
        if path.extension().is_some_and(|extension| extension == "pseg") {
            return Ok(Box::new(ScanProbeSegmentFile { inner: file, log: self.log.clone() }));
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
        self.inner.read_directory(path)
    }
    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        maximum: usize,
        matches: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner
            .match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

struct ScanProbeSegmentFile {
    inner: Box<dyn crate::vfs::VfsFile>,
    log: Arc<ScanProbeLog>,
}

impl ScanProbeSegmentFile {
    fn note_read(&self) {
        if !self.log.counting.load(Ordering::Acquire) {
            return;
        }
        if let Some(block) = self.log.last_block.lock().unwrap().clone() {
            *self.log.segment_reads_after.lock().unwrap().entry(block).or_default() += 1;
        }
    }
}

impl crate::vfs::VfsFile for ScanProbeSegmentFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.note_read();
        self.inner.read_all(maximum)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.note_read();
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

struct ScanProbeFixture {
    log: Arc<ScanProbeLog>,
    database: Arc<Mutex<AuthorityDatabase>>,
    /// Live loose blocks scanned before the planned shard, in scan order.
    earlier: Vec<BlockId>,
    /// Live loose blocks in `SCAN_PROBE_PLANNED_SHARD`.
    planned_shard: Vec<BlockId>,
    /// One live loose block in a later shard, so the planned shard is never
    /// the last block the scan opens.
    tail: BlockId,
}

/// 140 earlier 16 KiB blocks (more than the plan budgets below admit), eight
/// in the planned shard and one later block, all spilled to loose files by one
/// commit. `catalog` adds two compacted two-block segments spanning the planned
/// shard. The packed filter covers every publication, so filter bits noted for
/// the loose blocks stand in for the stale bits of retired segments and filter
/// false positives that keep it from excluding them, and a zero index cache
/// keeps no membership witnesses: every catalog probe reads segment indexes
/// exactly as an index-cache miss does in production.
fn scan_probe_fixture(catalog: bool) -> ScanProbeFixture {
    let inner = Arc::new(DeterministicVfs::new(None));
    inner.create_dir(Path::new("/data")).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let log = Arc::new(ScanProbeLog::default());
    let vfs = Arc::new(ScanProbeVfs { inner: inner.clone(), log: log.clone() });
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
    inner.arm_fault(None).unwrap();
    authority.block_store_shared().set_segment_index_cache_bytes(0).unwrap();
    let earlier_payloads =
        scan_probe_payloads(b"scan-probe-earlier-", 140, |shard| shard < SCAN_PROBE_PLANNED_SHARD);
    let planned_payloads =
        scan_probe_payloads(b"scan-probe-planned-", 8, |shard| shard == SCAN_PROBE_PLANNED_SHARD);
    let tail_payloads =
        scan_probe_payloads(b"scan-probe-tail-", 1, |shard| shard > SCAN_PROBE_PLANNED_SHARD);
    let mut segment_payloads = Vec::new();
    if catalog {
        for tag in [&b"scan-probe-segment-a-"[..], &b"scan-probe-segment-b-"[..]] {
            segment_payloads.push(scan_probe_payloads(tag, 1, |shard| shard < 0x80).remove(0));
            segment_payloads.push(
                scan_probe_payloads(tag, 1, |shard| shard > SCAN_PROBE_PLANNED_SHARD).remove(0),
            );
        }
    }
    let mut payloads = earlier_payloads.clone();
    payloads.extend(planned_payloads.iter().cloned());
    payloads.extend(tail_payloads.iter().cloned());
    payloads.extend(segment_payloads.iter().cloned());
    authority.commit_block_payloads_for_test(&payloads).unwrap();
    let segment_ids = scan_probe_ids(&segment_payloads);
    for pair in segment_ids.chunks(2) {
        authority.compact_payload_blocks_for_test(pair).unwrap();
    }
    if catalog {
        let loose: Vec<BlockId> = [&earlier_payloads, &planned_payloads, &tail_payloads]
            .into_iter()
            .flat_map(|payloads| scan_probe_ids(payloads))
            .collect();
        authority.block_store_shared().note_packed(&loose).unwrap();
    }
    let mut earlier = scan_probe_ids(&earlier_payloads);
    earlier.sort_unstable();
    ScanProbeFixture {
        log,
        database: Arc::new(Mutex::new(authority)),
        earlier,
        planned_shard: scan_probe_ids(&planned_payloads),
        tail: scan_probe_ids(&tail_payloads)[0],
    }
}

fn run_scan_probe_epoch(
    fixture: &ScanProbeFixture,
    maximum_temporary_bytes: u64,
) -> OnlineGarbageCollectionMetrics {
    let mut collector = OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget {
        maximum_temporary_bytes,
        ..test_budget()
    });
    fixture.log.counting.store(true, Ordering::Release);
    run_epoch(&mut collector, &fixture.database);
    fixture.log.counting.store(false, Ordering::Release);
    let database = fixture.database.lock().unwrap();
    for block_id in fixture.earlier.iter().chain(&fixture.planned_shard).chain([&fixture.tail]) {
        assert!(database.read_block_for_test(*block_id).is_ok());
    }
    metrics(&collector)
}

#[test]
fn scan_packs_the_segment_it_closes_and_sizes_each_later_block_once() {
    let fixture = scan_probe_fixture(false);
    let mut expected = fixture.earlier.clone();
    expected.extend(&fixture.planned_shard);
    expected.push(fixture.tail);
    expected.sort_unstable();
    {
        let database = fixture.database.lock().unwrap();
        let blocks = database.block_store_shared();
        assert_eq!(blocks.list_block_ids(expected.len() + 64).unwrap(), expected);
    }
    // The budget closes a segment after exactly the first 128 earlier blocks.
    // The scan packs it there and keeps admitting: the 21 blocks after it
    // form the trailing segment, which stays below the floor.
    let admitted = crate::authority_gc::MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS;
    let final_metrics = run_scan_probe_epoch(&fixture, admitted as u64 * SCAN_PROBE_FILE_BYTES);
    assert_eq!(final_metrics.total_compacted_blocks, admitted as u64);
    assert_eq!(final_metrics.total_compaction_segments, 1);
    for block_id in fixture.earlier[admitted..]
        .iter()
        .chain(&fixture.planned_shard)
        .chain([&fixture.tail])
    {
        assert_eq!(fixture.log.opens(*block_id), 1, "one size probe per live block after the closed segment");
    }
}

#[test]
fn scan_probes_catalog_membership_for_every_block_a_segment_can_hold() {
    let fixture = scan_probe_fixture(true);
    // Room for about 133 earlier blocks per segment: the scan packs the first
    // segment and admits every later block to the next one, so each of them
    // must still prove it is not catalogued.
    let final_metrics = run_scan_probe_epoch(&fixture, 2_200_000);
    assert_eq!(final_metrics.total_compaction_segments, 1);
    assert!(
        fixture.log.segment_reads_after(&fixture.earlier) > 0,
        "admitted blocks must still prove they are not catalogued"
    );
    assert!(fixture.log.segment_reads_after(&fixture.planned_shard) > 0);
    for block_id in &fixture.planned_shard {
        assert_eq!(fixture.log.opens(*block_id), 1, "one size probe per live block");
    }
}

/// A closed compaction sizes no live block. The membership probe still runs
/// for each of them, because it also finds loose copies of catalogued blocks
/// (none here); `tests_redundant_copies` removes such copies with compaction
/// closed.
#[test]
fn scan_skips_compaction_probes_while_no_catalog_slot_stays_below_the_pressure_floor() {
    let fixture = scan_probe_fixture(true);
    // Two catalogued segments: a third would put the catalog under pressure.
    let mut collector = OnlineGarbageCollector::new(test_budget());
    collector.set_catalog_pressure_floor_for_test(3);
    fixture.log.counting.store(true, Ordering::Release);
    run_epoch(&mut collector, &fixture.database);
    fixture.log.counting.store(false, Ordering::Release);
    assert_eq!(metrics(&collector).total_compacted_blocks, 0);
    let live: Vec<BlockId> = fixture
        .earlier
        .iter()
        .chain(&fixture.planned_shard)
        .chain([&fixture.tail])
        .copied()
        .collect();
    for block_id in &live {
        assert_eq!(fixture.log.opens(*block_id), 0, "closed compaction sized a live block");
    }
    assert_eq!(metrics(&collector).total_redundant_copies_removed, 0);
    let database = fixture.database.lock().unwrap();
    assert_eq!(database.payload_segment_count_for_test(), 2);
    for block_id in &live {
        assert!(database.read_block_for_test(*block_id).is_ok());
    }
}

/// Blocks per segment in the backlog fixtures: their temporary-byte budget
/// admits exactly this many 16 KiB probe blocks per segment.
const BACKLOG_SEGMENT_BLOCKS: usize = crate::authority_gc::MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS;

pub(super) fn backlog_budget() -> AuthorityGarbageCollectionBudget {
    AuthorityGarbageCollectionBudget {
        maximum_temporary_bytes: BACKLOG_SEGMENT_BLOCKS as u64 * SCAN_PROBE_FILE_BYTES,
        ..test_budget()
    }
}

/// A live loose backlog `segments` segments deep under `backlog_budget`:
/// 16 KiB payloads committed in near-equal batches of at least 160, so each
/// commit exceeds the carried ceiling and every block spills to its own
/// loose file.
pub(super) fn loose_backlog(
    database: &Arc<Mutex<AuthorityDatabase>>,
    segments: usize,
) -> Vec<(BlockId, Vec<u8>)> {
    let payloads = scan_probe_payloads(b"loose-backlog-", segments * BACKLOG_SEGMENT_BLOCKS, |_| true);
    let mut database_guard = database.lock().unwrap();
    let mut backlog = Vec::with_capacity(payloads.len());
    let commits = (payloads.len() / 160).max(1);
    for chunk in payloads.chunks(payloads.len().div_ceil(commits)) {
        let block_ids = database_guard.commit_block_payloads_for_test(chunk).unwrap();
        backlog.extend(block_ids.into_iter().zip(chunk.iter().cloned()));
    }
    backlog
}

pub(super) fn loose_backlog_remaining(
    database: &Arc<Mutex<AuthorityDatabase>>,
    backlog: &[(BlockId, Vec<u8>)],
) -> usize {
    let loose: BTreeSet<BlockId> = database
        .lock()
        .unwrap()
        .block_store_shared()
        .list_block_ids(backlog.len() + 4096)
        .unwrap()
        .into_iter()
        .collect();
    backlog.iter().filter(|(block_id, _)| loose.contains(block_id)).count()
}

/// The 2026-10-31 inode exhaustion: the search projection held ~12.4M live
/// loose blocks while the collector packed at most one segment per epoch,
/// and epochs start at least 120 s apart. A backlog five segments deep packs
/// completely in one epoch: the scan closes a segment whenever the next block
/// would exceed a format or temporary-byte limit, builds it off the mutex and
/// publishes it under one short hold, and the trailing segment packs in the
/// deletion phase. Each loose copy goes only after both CONTROL slots name the
/// catalog that holds it, so recovery from either slot reads the backlog.
#[test]
fn one_epoch_packs_a_loose_backlog_into_every_segment_the_limits_require() {
    let (vfs, database) = authority();
    let backlog = loose_backlog(&database, 5);
    assert_eq!(
        loose_backlog_remaining(&database, &backlog),
        backlog.len(),
        "fixture: every backlog block starts loose"
    );
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    let mut epochs = Vec::new();
    while epochs.len() < 8 {
        let before = database.lock().unwrap().payload_segment_count_for_test();
        run_epoch(&mut collector, &database);
        let published = database.lock().unwrap().payload_segment_count_for_test() - before;
        let remaining = loose_backlog_remaining(&database, &backlog);
        epochs.push((published, remaining));
        if remaining == 0 {
            break;
        }
    }
    assert_eq!(
        epochs,
        vec![(5, 0)],
        "per epoch: (segments published, backlog blocks still loose)"
    );
    assert_eq!(metrics(&collector).total_compacted_blocks, backlog.len() as u64);
    assert_eq!(metrics(&collector).total_compaction_segments, 5);
    for lose_newest_slot in [false, true] {
        let image = Arc::new(vfs.fork_durable().unwrap());
        if lose_newest_slot {
            super::tests_fallback_slot::destroy_newest_slot(&image);
        }
        let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), image).unwrap();
        for (block_id, payload) in &backlog {
            assert_eq!(&reopened.read_block_for_test(*block_id).unwrap(), payload);
        }
    }
}

#[test]
fn doorbell_and_pressure_never_break_the_pacing_floor() {
    // 2026-09-21 FUSE brownout regression: the app rings `system.reclaim`
    // every 30 s while reclaim reports a backlog, and one full-store epoch
    // takes minutes on a degraded mount. Unthrottled, the collector ran
    // back-to-back epochs indefinitely; the pacing floor must hold under
    // doorbells AND volume pressure alike.
    let (_vfs, database) = authority();
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        test_budget(),
        Duration::from_millis(60),
    );
    // First epoch is due immediately (no boot grace in this constructor).
    assert!(collector.should_run_now(false));
    run_epoch(&mut collector, &database);
    // Doorbell + pressure may accelerate the idle backoff, but neither may
    // restart an epoch inside the min interval or inside the previous
    // epoch's own duration.
    collector.request_expedited_epoch();
    assert!(!collector.should_run_now(true));
    // A consumed doorbell stays consumed; pressure alone still fails the
    // floor.
    assert!(!collector.should_run_now(true));
    // The floor eventually lifts — the next start becomes legal once both
    // the min interval and the epoch's own duration have passed. Poll
    // instead of sleeping a fixed span: a debug-build epoch's duration is
    // not wall-clock predictable.
    let deadline = Instant::now() + Duration::from_secs(30);
    while !collector.should_run_now(false) {
        assert!(
            Instant::now() < deadline,
            "the pacing floor never lifted after one epoch"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn duty_cycle_floor_waits_out_the_previous_epochs_duration() {
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        test_budget(),
        Duration::from_millis(10),
    );
    // Simulate a just-finished long epoch: due by the idle timer, past the
    // min-interval floor, but inside its own duty-cycle window.
    let now = Instant::now();
    collector.last_epoch_started = Some(now - Duration::from_millis(50));
    collector.last_epoch_ended = Some(now - Duration::from_millis(10));
    collector.last_epoch_duration = Duration::from_millis(100);
    collector.next_epoch_not_before = now - Duration::from_millis(1);
    assert!(!collector.should_run_now(true));
    collector.last_epoch_duration = Duration::from_millis(5);
    assert!(collector.should_run_now(true));
}

/// At or above 90% of the 4,096-entry payload catalog ceiling consolidation
/// work is pending, so an epoch that ends there keeps the minimum interval
/// instead of doubling its idle backoff; the start and duty floors still apply
/// (`should_run_now`). One segment below, idle epochs back off again once the
/// shrinking publication's orphan segment and old catalog root are collected.
/// A two-segment catalog stands in for one at the threshold (a 3,687-segment
/// fixture takes minutes to build on `DeterministicVfs`), and the pressure
/// floor is moved past the catalog so that no consolidation runs and the
/// fixture's epochs find nothing else. Before the change the epochs at the
/// threshold backed off from the first one: [2 ms, 4 ms].
#[test]
fn an_epoch_ending_at_ninety_percent_of_the_catalog_ceiling_keeps_the_minimum_interval() {
    let expedited = (crate::payload_manifest::MAX_PAYLOAD_SEGMENTS * 90).div_ceil(100);
    let production = OnlineGarbageCollector::new(test_budget());
    assert_eq!(
        (production.catalog_pressure_expedited(expedited), production.catalog_pressure_expedited(expedited - 1)),
        (true, false),
        "the threshold is 90% of the catalog ceiling: {expedited} segments"
    );
    let (_vfs, database) = authority();
    let payload = b"catalog-pressure-cadence-shared-live";
    database.lock().unwrap().commit_block_payload_for_test(payload).unwrap();
    let references = publish_one_block_segments(&database, payload, 2);
    let minimum = Duration::from_millis(1);
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(test_budget(), minimum);
    collector.set_catalog_pressure_floor_for_test(crate::payload_manifest::MAX_PAYLOAD_SEGMENTS + 1);
    collector.set_catalog_expedite_floor_for_test(references.len());
    let idle_intervals = |collector: &mut OnlineGarbageCollector, epochs: usize| -> Vec<Duration> {
        (0..epochs)
            .map(|_| {
                run_epoch(collector, &database);
                collector.idle_interval
            })
            .collect()
    };
    assert_eq!(idle_intervals(&mut collector, 2), vec![minimum; 2], "epochs ending at the threshold keep the minimum");
    database
        .lock()
        .unwrap()
        .engine_mut_for_test()
        .publish_payload_manifest_for_test(
            crate::payload_manifest::PayloadManifest::new(references[..1].to_vec()).unwrap(),
        )
        .unwrap();
    assert_eq!(
        idle_intervals(&mut collector, 3),
        vec![minimum, minimum, 2 * minimum],
        "below it: the orphan segment and the old root are found, then the idle epoch backs off"
    );
}

/// docs/TOFUDB_BLOCK_READ_PATH.md, "Published catalog location table": the
/// first table needs no authority hold. While a foreground holder keeps the
/// mutex, every epoch's capture defers, yet the driver publishes the table of
/// the installed catalog (live 2026-11-07 the capture deferred twice and the
/// authority's table appeared only after five minutes).
#[test]
fn driver_publishes_the_first_catalog_location_table_while_epochs_defer() {
    use crate::block::BlockId;
    use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
    let (_vfs, database) = authority();
    let blocks = {
        let mut guard = database.lock().unwrap();
        let blocks = guard.block_store_shared();
        let store = blocks.payload_segment_store().unwrap();
        let payloads: std::collections::BTreeMap<BlockId, Vec<u8>> = (0..8)
            .map(|index| format!("first-table-{index}").into_bytes())
            .map(|payload| (BlockId::for_payload(&payload), payload))
            .collect();
        let created = store
            .create(uuid::Uuid::from_u128(41), payloads.keys().copied().collect(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        let reference =
            PayloadSegmentReference::new(created.metadata(), created.first_block_id(), created.last_block_id()).unwrap();
        guard.engine_mut_for_test()
            .publish_payload_manifest_for_test(PayloadManifest::new(vec![reference]).unwrap())
            .unwrap();
        blocks
    };
    assert!(blocks.published_catalog_locations_for_test().is_none());
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(test_budget(), Duration::from_millis(25));
    collector.defer_first_epoch(Duration::from_millis(500));
    let driver =
        OnlineGarbageCollectionDriver::start_with_collector(&database, collector, false).unwrap();
    let observer = driver.observer();
    let foreground = database.lock().unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while blocks.published_catalog_locations_for_test().is_none() || observer.metrics().deferred_epochs == 0 {
        assert!(std::time::Instant::now() < deadline, "no first table while the capture defers");
        std::thread::sleep(Duration::from_millis(10));
    }
    let (publication, table) = blocks.published_catalog_locations_for_test().unwrap();
    assert_eq!(publication, blocks.installed_catalog_publication_for_test());
    assert_eq!(table.located_blocks(), 8);
    assert_eq!(observer.metrics().marks_built, 0, "no epoch got past its capture");
    drop(foreground);
    driver.stop_and_join().unwrap();
}

#[test]
fn driver_runs_epochs_and_reports_health() {
    let (_vfs, database) = authority();
    {
        let database_guard = database.lock().unwrap();
        database_guard
            .write_orphan_block_for_test(b"driver-orphan")
            .unwrap();
    }
    let collector = OnlineGarbageCollector::with_min_epoch_interval(
        test_budget(),
        Duration::from_millis(25),
    );
    let driver =
        OnlineGarbageCollectionDriver::start_with_collector(&database, collector, false).unwrap();
    let observer = driver.observer();
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while observer.metrics().epochs_completed < 2 {
        assert!(std::time::Instant::now() < deadline, "driver made no epochs");
        assert!(observer.require_healthy().is_ok());
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(observer.metrics().total_reclaimed_blocks >= 1);
    driver.stop_and_join().unwrap();
}

#[test]
fn driver_attributes_mark_reads_to_the_online_gc_component() {
    let (_vfs, database) = authority();
    {
        let mut database_guard = database.lock().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, EntityKey::new(7, 11, "record", b"attributed").unwrap(), b"value".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
    }
    let reads = || crate::fsync_stats::component_stage_calls(
        crate::fsync_stats::COMPONENT_ONLINE_GC, crate::fsync_stats::STAGE_BLOCK_GET);
    let before = reads();
    let collector = OnlineGarbageCollector::with_min_epoch_interval(test_budget(), Duration::from_millis(25));
    let driver =
        OnlineGarbageCollectionDriver::start_with_collector(&database, collector, false).unwrap();
    let observer = driver.observer();
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while observer.metrics().epochs_completed < 1 {
        assert!(std::time::Instant::now() < deadline, "driver made no epochs");
        std::thread::sleep(Duration::from_millis(25));
    }
    driver.stop_and_join().unwrap();
    assert!(reads() > before, "the collector's traversal must not be billed to foreground");
}

/// Same-shard fixtures sized so one commit carrying `count` of them exceeds
/// the carried ceiling and spills every block to a loose file (mirrors the
/// offline collector's compaction fixtures).
fn oversized_payloads_in_one_shard(count: usize) -> Vec<Vec<u8>> {
    let payload_len = crate::transaction::MAX_CARRIED_PAYLOAD_BYTES / count + 1;
    let fixture = |value: u32| {
        let mut payload = Vec::with_capacity(payload_len);
        payload.extend_from_slice(b"online-gc-auto-compaction-");
        payload.extend_from_slice(&value.to_le_bytes());
        payload.resize(payload_len, (value % 251) as u8);
        payload
    };
    let mut counts = [0_usize; 256];
    let mut winning = None;
    for value in 0..1_000_000_u32 {
        let shard = BlockId::for_payload(&fixture(value)).0[0];
        counts[shard as usize] += 1;
        if counts[shard as usize] == count {
            winning = Some((shard, value));
            break;
        }
    }
    let (shard, last) = winning.expect("failed to find enough same-shard fixtures");
    (0..=last)
        .map(fixture)
        .filter(|payload| BlockId::for_payload(payload).0[0] == shard)
        .collect()
}

#[test]
fn catalog_pressure_consolidates_small_segments_into_one() {
    let (_vfs, database) = authority();
    // Three small segments with all-live content (two payloads each) plus
    // one dead-heavy segment.
    let mut live_ids = Vec::new();
    {
        let mut database_guard = database.lock().unwrap();
        for batch in 0..3_u32 {
            let first = database_guard
                .commit_block_payload_for_test(format!("consolidate-live-{batch}-a").as_bytes())
                .unwrap();
            let second = database_guard
                .commit_block_payload_for_test(format!("consolidate-live-{batch}-b").as_bytes())
                .unwrap();
            database_guard
                .compact_payload_blocks_for_test(&[first, second])
                .unwrap();
            live_ids.push((first, format!("consolidate-live-{batch}-a").into_bytes()));
            live_ids.push((second, format!("consolidate-live-{batch}-b").into_bytes()));
        }
        let dead = database_guard
            .write_orphan_block_for_test(b"consolidate-dead")
            .unwrap();
        let live = database_guard
            .commit_block_payload_for_test(b"consolidate-live-final")
            .unwrap();
        database_guard
            .compact_payload_blocks_for_test(&[live, dead])
            .unwrap();
        live_ids.push((live, b"consolidate-live-final".to_vec()));
        assert_eq!(database_guard.payload_segment_count_for_test(), 4);
    }
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        test_budget(),
        Duration::from_millis(1),
    );
    collector.set_catalog_pressure_floor_for_test(1);
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    let final_metrics = metrics(&collector);
    assert!(
        final_metrics.total_consolidations >= 1,
        "pressured consolidations must run: {final_metrics:?}"
    );
    assert!(final_metrics.total_consolidated_victims >= 3);
    let database_guard = database.lock().unwrap();
    assert!(
        database_guard.payload_segment_count_for_test() < 4,
        "consolidation freed catalog slots: {}",
        database_guard.payload_segment_count_for_test()
    );
    for (id, payload) in live_ids {
        assert_eq!(database_guard.read_block_for_test(id).unwrap(), payload);
    }
}

/// Publishes a catalog of `count` one-block segments that all hold `payload`
/// (mirrors engine_adoption_tests::build_full_catalog) and returns it.
fn publish_one_block_segments(
    database: &Arc<Mutex<AuthorityDatabase>>,
    payload: &[u8],
    count: usize,
) -> Vec<crate::payload_manifest::PayloadSegmentReference> {
    let block_id = BlockId::for_payload(payload);
    let mut database_guard = database.lock().unwrap();
    let engine = database_guard.engine_mut_for_test();
    let mut references = Vec::with_capacity(count);
    for index in 0..count {
        let segment = engine
            .block_store()
            .payload_segment_store()
            .unwrap()
            .create(uuid::Uuid::from_u128(index as u128 + 1), vec![block_id], |_| Ok(payload.to_vec()))
            .unwrap();
        references.push(
            crate::payload_manifest::PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )
            .unwrap(),
        );
    }
    engine
        .publish_payload_manifest_for_test(
            crate::payload_manifest::PayloadManifest::new(references.clone()).unwrap(),
        )
        .unwrap();
    references
}

#[test]
fn full_catalog_recovers_capacity_through_pressured_consolidation() {
    let (_vfs, database) = authority();
    let shared_payload = b"full-catalog-shared-live";
    let shared_id = {
        let mut database_guard = database.lock().unwrap();
        database_guard
            .commit_block_payload_for_test(shared_payload)
            .unwrap()
    };
    // Fill the catalog to its 4,096-entry ceiling with one-block segments.
    publish_one_block_segments(
        &database,
        shared_payload,
        crate::payload_manifest::MAX_PAYLOAD_SEGMENTS,
    );
    assert_eq!(
        database.lock().unwrap().payload_segment_count_for_test(),
        crate::payload_manifest::MAX_PAYLOAD_SEGMENTS
    );
    // Loose compaction needs a free catalog slot and must fail at the wall.
    let loose = {
        let database_guard = database.lock().unwrap();
        database_guard
            .write_orphan_block_for_test(b"capacity-probe")
            .unwrap()
    };
    {
        let mut database_guard = database.lock().unwrap();
        let store = database_guard.block_store_shared();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(uuid::Uuid::new_v4(), vec![loose], |id| store.get(id))
            .unwrap();
        let reference = crate::payload_manifest::PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        assert_eq!(
            database_guard
                .online_gc_finish_compaction(&[loose], reference)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput,
            "catalog-full must reject new-segment publication"
        );
        let _ = store
            .payload_segment_store()
            .unwrap()
            .remove_orphan_file(&crate::payload_segment::PayloadSegmentOrphanCandidate {
                file_name: format!("{}.pseg", reference.segment_id.simple()),
                segment_id: Some(reference.segment_id),
                file_bytes: reference.file_bytes,
            });
    }
    // Pressured epochs consolidate the one-block segments and free slots.
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        test_budget(),
        Duration::from_millis(1),
    );
    let stop = AtomicBool::new(false);
    for _ in 0..64 {
        collector.run_epoch(&database, &stop).unwrap();
        let count = database
            .lock()
            .unwrap()
            .payload_segment_count_for_test();
        if count < crate::payload_manifest::MAX_PAYLOAD_SEGMENTS {
            break;
        }
    }
    let remaining = database
        .lock()
        .unwrap()
        .payload_segment_count_for_test();
    assert!(
        remaining < crate::payload_manifest::MAX_PAYLOAD_SEGMENTS,
        "pressured consolidation never freed a slot: {remaining}"
    );
    assert!(metrics(&collector).total_consolidations >= 1);
    // The shared live block survives consolidation everywhere it was packed.
    let database_guard = database.lock().unwrap();
    assert_eq!(
        database_guard.read_block_for_test(shared_id).unwrap(),
        shared_payload.to_vec()
    );
}

#[test]
fn doorbell_expedites_an_epoch_past_the_idle_backoff() {
    let (_vfs, database) = authority();
    let collector = OnlineGarbageCollector::with_min_epoch_interval(
        test_budget(),
        Duration::from_secs(3600),
    );
    let driver =
        OnlineGarbageCollectionDriver::start_with_collector(&database, collector, false).unwrap();
    let observer = driver.observer();
    // No natural epoch for an hour; the doorbell must still start one.
    {
        let database_guard = database.lock().unwrap();
        database_guard.online_garbage_collection().unwrap().request_expedited_epoch();
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while observer.metrics().epochs_completed < 1 {
        assert!(std::time::Instant::now() < deadline, "doorbell never started an epoch");
        std::thread::sleep(Duration::from_millis(50));
    }
    driver.stop_and_join().unwrap();
}

/// Requests shutdown during the first cold source read, not before the epoch.
struct StopDuringMarkRead {
    inner: Arc<DeterministicVfs>,
    stop: Arc<AtomicBool>,
    armed: Arc<AtomicBool>,
    reads_after_stop: Arc<std::sync::atomic::AtomicUsize>,
}
impl Vfs for StopDuringMarkRead {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        let file = self.inner.open(path, request)?;
        if request.read
            && path
                .components()
                .any(|part| part.as_os_str() == "blocks" || part.as_os_str() == "payload-segments")
        {
            return Ok(Box::new(StopDuringSourceRead {
                inner: file,
                stop: self.stop.clone(),
                armed: self.armed.clone(),
                reads_after_stop: self.reads_after_stop.clone(),
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
        self.inner.read_directory(path)
    }
    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        maximum: usize,
        matches: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner
            .match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

struct StopDuringSourceRead {
    inner: Box<dyn crate::vfs::VfsFile>,
    stop: Arc<AtomicBool>,
    armed: Arc<AtomicBool>,
    reads_after_stop: Arc<std::sync::atomic::AtomicUsize>,
}
impl StopDuringSourceRead {
    fn note_read(&self) {
        if self.stop.load(Ordering::Acquire) {
            self.reads_after_stop.fetch_add(1, Ordering::Relaxed);
        }
        if self.armed.swap(false, Ordering::AcqRel) {
            self.stop.store(true, Ordering::Release);
        }
    }
}
impl crate::vfs::VfsFile for StopDuringSourceRead {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        let result = self.inner.read_all(maximum);
        self.note_read();
        result
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        let result = self.inner.read_exact_at(offset, bytes);
        self.note_read();
        result
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

#[test]
fn shutdown_during_mark_read_abandons_before_reading_the_remaining_graph() {
    let inner = Arc::new(DeterministicVfs::new(None));
    inner.create_dir(Path::new("/data")).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let vfs = Arc::new(StopDuringMarkRead {
        inner,
        stop: stop.clone(),
        armed: Arc::new(AtomicBool::new(false)),
        reads_after_stop: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
    });
    let mut authority =
        AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let key = EntityKey::new(7, 11, "record", b"retained").unwrap();
    let mut transaction = authority.begin(7, 11).unwrap();
    for index in 0..256_u32 {
        authority
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "record", &index.to_le_bytes()).unwrap(),
                vec![7; 256],
            )
            .unwrap();
    }
    authority
        .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
        .unwrap();
    authority.commit(transaction).unwrap();
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    drop(authority);
    let authority = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    authority.configure_hot_data_caches(0, 0).unwrap();
    let database = Arc::new(Mutex::new(authority));
    let mut collector = OnlineGarbageCollector::new(test_budget());
    vfs.armed.store(true, Ordering::Release);
    let error = collector.run_epoch(&database, &stop).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    assert_eq!(vfs.reads_after_stop.load(Ordering::Relaxed), 0);
    assert_eq!(metrics(&collector).total_reclaimed_blocks, 0);
    stop.store(false, Ordering::Release);
    run_epoch(&mut collector, &database);
    assert_live_value(&database, &key, b"live-value");
}

#[test]
fn idle_epochs_eventually_verify_every_payload_catalog_window() {
    let (vfs, database) = authority();
    let excluded_segment = {
        let mut guard = database.lock().unwrap();
        for index in 0..(MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED + 1) {
            let payload = format!("idle-window-live-{index}");
            let id = guard.commit_block_payload_for_test(payload.as_bytes()).unwrap();
            guard.compact_payload_blocks_for_test(&[id]).unwrap();
        }
        let capture = guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
        let manifest = capture.blocks.installed_payload_manifest().unwrap().unwrap();
        let count = manifest.segments.len().div_ceil(MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED);
        assert!(count > 1);
        let start = (capture.snapshot.generation as usize % count) * MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED;
        manifest.segments.iter().enumerate()
            .find(|(index, _)| *index < start || *index >= start + MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED)
            .unwrap().1.segment_id
    };
    // Corruption outside the first selected window is a reachability oracle:
    // repeated idle epochs must eventually inspect that real catalog file.
    let path = Path::new("/data/payload-segments").join(format!("{}.pseg", excluded_segment.simple()));
    let mut file = vfs.open(&path, OpenRequest { write: true, ..OpenRequest::default() }).unwrap();
    file.write_all_at(0, b"BROKEN!!").unwrap();
    sync_all_barrier(file.as_mut()).unwrap();
    drop(file);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    let stop = AtomicBool::new(false);
    let mut detected = false;
    for _ in 0..8 {
        if let Err(error) = collector.run_epoch(&database, &stop) {
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
            detected = true;
            break;
        }
    }
    assert!(detected, "unchanged generation must not starve another catalog window");
}

#[test]
fn idle_epochs_reclaim_dead_segment_beyond_first_window_and_preserve_live_data() {
    let (_vfs, database) = authority();
    let (dead_id, live_ids) = {
        let mut guard = database.lock().unwrap();
        let mut payloads = Vec::new();
        for index in 0..(MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED + 1) {
            let payload = format!("idle-reclaim-payload-{index}").into_bytes();
            let id = guard.write_orphan_block_for_test(&payload).unwrap();
            guard.compact_payload_blocks_for_test(&[id]).unwrap();
            payloads.push((id, payload));
        }
        let capture = guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
        let manifest = capture.blocks.installed_payload_manifest().unwrap().unwrap();
        let windows = manifest.segments.len().div_ceil(MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED);
        let start = (capture.snapshot.generation as usize % windows) * MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED;
        let target = manifest.segments.iter().enumerate()
            .find(|(index, _)| *index < start || *index >= start + MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED)
            .unwrap().1;
        let dead_id = capture.blocks.payload_segment_store().unwrap()
            .open_segment(target.segment_id).unwrap().block_ids().next().unwrap();
        let mut live_ids = Vec::new();
        for (id, payload) in payloads {
            if id != dead_id {
                assert_eq!(guard.commit_block_payload_for_test(&payload).unwrap(), id);
                live_ids.push((id, payload));
            }
        }
        let after = guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
        assert_eq!(after.snapshot.generation, capture.snapshot.generation);
        (dead_id, live_ids)
    };
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    assert!(database.lock().unwrap().read_block_for_test(dead_id).is_ok(),
        "first window must not retire another window's segment");
    for _ in 0..6 { run_epoch(&mut collector, &database); }
    let guard = database.lock().unwrap();
    assert_eq!(guard.read_block_for_test(dead_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    for (id, payload) in live_ids { assert_eq!(guard.read_block_for_test(id).unwrap(), payload); }
    assert!(metrics(&collector).total_segments_retired > 0);
}

#[test]
fn cancelled_and_contended_epochs_do_not_advance_catalog_confirmation() {
    let (_vfs, database) = authority();
    let mut collector = OnlineGarbageCollector::new(test_budget());
    let stop = AtomicBool::new(true);
    assert_eq!(collector.run_epoch(&database, &stop).unwrap(), OnlineGarbageCollectionOutcome::Deferred);
    assert!(collector.payload_window_cursor.is_none());
    assert!(!collector.payload_window_second_epoch);
    stop.store(false, Ordering::Release);
    let guard = database.lock().unwrap();
    assert_eq!(collector.run_epoch(&database, &stop).unwrap(), OnlineGarbageCollectionOutcome::Deferred);
    assert!(collector.payload_window_cursor.is_none());
    drop(guard);
    run_epoch(&mut collector, &database);
    let cursor = collector.payload_window_cursor.unwrap();
    assert!(collector.payload_window_second_epoch);
    stop.store(true, Ordering::Release);
    collector.run_epoch(&database, &stop).unwrap();
    assert_eq!(collector.payload_window_cursor, Some(cursor));
    assert!(collector.payload_window_second_epoch);
    stop.store(false, Ordering::Release);
    run_epoch(&mut collector, &database);
    assert_eq!(collector.payload_window_cursor, Some(cursor.wrapping_add(1)));
    assert!(!collector.payload_window_second_epoch);
}

#[test]
fn idle_epoch_sweeps_expired_retirement_without_another_catalog_publication() {
    let (vfs, database) = authority();
    let (live_id, victim) = {
        let mut guard = database.lock().unwrap();
        let live_id = guard.commit_block_payload_for_test(b"idle-expiry-live").unwrap();
        let dead_id = guard.write_orphan_block_for_test(b"idle-expiry-dead").unwrap();
        guard.compact_payload_blocks_for_test(&[live_id, dead_id]).unwrap();
        let victim = guard.engine_mut_for_test().payload_segment_references()[0].segment_id;
        (live_id, victim)
    };
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);run_epoch(&mut collector, &database);
    assert_eq!(metrics(&collector).total_segments_rewritten, 1);
    let path = Path::new("/data/payload-segments").join(format!("{}.pseg", victim.simple()));
    assert!(vfs.metadata(&path).is_ok(), "retirement must first preserve reader grace");
    let catalog = {
        let mut guard = database.lock().unwrap();
        let store = guard.block_store_shared();
        let age = crate::payload_segment::segment_retirement_delay().saturating_add(Duration::from_secs(1));
        assert_eq!(store.payload_segment_store().unwrap().age_deferred_retirements_for_test(age).unwrap(), 1);
        guard.engine_mut_for_test().payload_segment_references().to_vec()
    };
    run_epoch(&mut collector, &database);
    assert_eq!(database.lock().unwrap().engine_mut_for_test().payload_segment_references(), catalog.as_slice(),
        "expiry reclamation must not require another catalog publication");
    assert_eq!(database.lock().unwrap().read_block_for_test(live_id).unwrap(), b"idle-expiry-live");
    assert_eq!(vfs.metadata(&path).unwrap_err().kind(), io::ErrorKind::NotFound,
        "an expired predecessor must not remain parked indefinitely on an idle authority");
}
