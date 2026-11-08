//! Mark read-cost specifications (docs/TOFUDB_ONLINE_GC.md, "Catalog location
//! table for marks", "Pages shared by several roots are walked once per
//! mark" and "One mark-run pass per segment window"). The location-table
//! fixture packs every entity-reachable block into shuffled full-range payload
//! segments with filler, keeps history sources loose, and gives the
//! verified-index cache a quarter of the index bytes. The shared-page fixture
//! pins several roots that share all but one path. A counting VFS attributes
//! every segment read to its framing, index or payload region and counts loose
//! block opens and the bytes read from mark spill runs.

use super::tests::test_budget;
use super::tests_capture::unmarked_epoch;
use super::*;
use crate::authority::AuthorityTransaction;
use crate::entity::{visit_entity_page_graph_with_values, EntityKey};
use crate::payload_segment::read_path_counters;
use crate::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, OpenRequest, VfsFile,
    VfsPositionedReader,
};

const SEGMENTS: usize = 32;
const FILLER_BLOCKS_PER_SEGMENT: usize = 96;
/// Segment framing (payload_segment.rs): 48-byte header and index entries,
/// 60-byte trailer.
const SEGMENT_HEADER_BYTES: u64 = 48;
const SEGMENT_INDEX_ENTRY_BYTES: u64 = 48;
const SEGMENT_TRAILER_BYTES: u64 = 60;

#[derive(Clone, Copy)]
enum Source {
    Segment(Uuid),
    Loose,
    MarkRun,
}

#[derive(Default)]
struct ReadLog {
    /// (segment, offset, bytes) of every payload-segment read.
    segment_reads: Vec<(Uuid, u64, usize)>,
    loose_opens: u64,
    loose_bytes: u64,
    mark_run_bytes: u64,
}

struct CountingVfs {
    inner: Arc<DeterministicVfs>,
    log: Arc<Mutex<ReadLog>>,
}

impl CountingVfs {
    fn take(&self) -> ReadLog {
        std::mem::take(&mut *self.log.lock().unwrap())
    }
}

fn segment_of(path: &Path) -> Option<Uuid> {
    let name = path.file_name()?.to_str()?.strip_suffix(".pseg")?;
    Uuid::parse_str(name).ok()
}

impl Vfs for CountingVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let file = self.inner.open(path, request)?;
        if !request.read || request.write {
            return Ok(file);
        }
        let source = if let Some(segment) = segment_of(path) {
            Source::Segment(segment)
        } else if path.extension().is_some_and(|extension| extension == "blk") {
            self.log.lock().unwrap().loose_opens += 1;
            Source::Loose
        } else if path.extension().is_some_and(|extension| extension == "mark") {
            Source::MarkRun
        } else {
            return Ok(file);
        };
        Ok(Box::new(CountingFile { inner: file, source, log: Arc::clone(&self.log) }))
    }
    fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        // Segment reads count through whichever handle serves them; loose and
        // history sources keep their accounting above.
        Ok(match (self.inner.open_positioned_reader(path)?, segment_of(path)) {
            (Some(inner), Some(segment)) => {
                Some(Arc::new(CountingReader { inner, segment, log: Arc::clone(&self.log) }))
            }
            (reader, _) => reader,
        })
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
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<BoundedDirectoryEntries> {
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
        self.inner.sync_directory(path)
    }
}

struct CountingReader {
    inner: Arc<dyn VfsPositionedReader>,
    segment: Uuid,
    log: Arc<Mutex<ReadLog>>,
}

impl VfsPositionedReader for CountingReader {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)?;
        self.log.lock().unwrap().segment_reads.push((self.segment, offset, bytes.len()));
        Ok(())
    }
}

struct CountingFile {
    inner: Box<dyn VfsFile>,
    source: Source,
    log: Arc<Mutex<ReadLog>>,
}

impl CountingFile {
    fn note(&self, offset: u64, bytes: usize) {
        let mut log = self.log.lock().unwrap();
        match self.source {
            Source::Segment(segment) => log.segment_reads.push((segment, offset, bytes)),
            Source::Loose => log.loose_bytes += bytes as u64,
            Source::MarkRun => log.mark_run_bytes += bytes as u64,
        }
    }
}

impl VfsFile for CountingFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        let bytes = self.inner.read_all(maximum)?;
        self.note(0, bytes.len());
        Ok(bytes)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)?;
        self.note(offset, bytes.len());
        Ok(())
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

/// Work one mark build performed, attributed by region.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct MarkCost {
    marked_blocks: u64,
    whole_index_reads: u64,
    most_whole_reads_of_one_index: u64,
    index_entry_reads: u64,
    index_bytes: u64,
    framing_reads: u64,
    payload_reads: u64,
    payload_bytes: u64,
    loose_opens: u64,
    loose_bytes: u64,
    witness_checks: u64,
    location_candidates: u64,
    /// Reads of the most-read catalogued payload (one segment offset).
    most_reads_of_one_payload: u64,
}

fn mark_cost(
    log: ReadLog,
    catalog: &[PayloadSegmentReference],
    marked_blocks: usize,
    (witness_checks, location_candidates): (u64, u64),
) -> MarkCost {
    let mut cost = MarkCost {
        marked_blocks: marked_blocks as u64,
        loose_opens: log.loose_opens,
        loose_bytes: log.loose_bytes,
        witness_checks,
        location_candidates,
        ..MarkCost::default()
    };
    let mut whole_reads: BTreeMap<Uuid, u64> = BTreeMap::new();
    let mut payload_reads: BTreeMap<(Uuid, u64), u64> = BTreeMap::new();
    for (segment_id, offset, bytes) in log.segment_reads {
        let reference = catalog
            .iter()
            .find(|reference| reference.segment_id == segment_id)
            .expect("mark reads stay inside the installed catalog");
        let index_offset = SEGMENT_HEADER_BYTES + reference.payload_bytes;
        let index_bytes = u64::from(reference.block_count) * SEGMENT_INDEX_ENTRY_BYTES;
        if offset == 0 || offset == reference.file_bytes - SEGMENT_TRAILER_BYTES {
            cost.framing_reads += 1;
        } else if (index_offset..index_offset + index_bytes).contains(&offset) {
            cost.index_bytes += bytes as u64;
            if offset == index_offset && bytes as u64 == index_bytes {
                cost.whole_index_reads += 1;
                *whole_reads.entry(segment_id).or_default() += 1;
            } else {
                cost.index_entry_reads += 1;
            }
        } else {
            cost.payload_reads += 1;
            cost.payload_bytes += bytes as u64;
            *payload_reads.entry((segment_id, offset)).or_default() += 1;
        }
    }
    cost.most_whole_reads_of_one_index = whole_reads.values().copied().max().unwrap_or(0);
    cost.most_reads_of_one_payload = payload_reads.values().copied().max().unwrap_or(0);
    cost
}

fn write_records(authority: &mut AuthorityDatabase) {
    for batch in 0..8_u32 {
        let mut transaction = authority.begin(7, 11).unwrap();
        for index in batch * 300..(batch + 1) * 300 {
            let key = EntityKey::new(7, 11, "markcost", &index.to_be_bytes()).unwrap();
            let value = format!("mark-cost-record-{index}-{}", "v".repeat(160)).into_bytes();
            authority.entity_put(&mut transaction, key, value).unwrap();
        }
        authority.commit(transaction).unwrap();
    }
    for document in 0..6_u32 {
        let logical_key = format!("document-{document}");
        let text: String = (0..12 * 1024_usize)
            .map(|position| char::from(b'a' + ((position * 7 + document as usize) % 26) as u8))
            .collect();
        let mut transaction = authority.begin(7, 11).unwrap();
        crate::versioned_document::put(authority, &mut transaction, crate::versioned_document::PutRequest {
            key: EntityKey::new(7, 11, "markdocs", logical_key.as_bytes()).unwrap(),
            namespace: "markdocs".into(),
            logical_key,
            value_json: serde_json::to_vec(&text).unwrap(),
            expected_version: None,
            updated_at_ms: 1,
        })
        .unwrap();
        authority.commit(transaction).unwrap();
    }
    let stream = crate::stream::StreamKey::new(7, 11, "markcost", b"task").unwrap();
    for append in 0..3_u64 {
        let mut transaction = authority.begin(7, 11).unwrap();
        let events = (0..4_u64)
            .map(|index| {
                let sequence = append * 4 + index;
                crate::stream::StreamEvent::new(sequence as i64, "delta", vec![sequence as u8; 16 * 1024]).unwrap()
            })
            .collect();
        authority.stream_append(&mut transaction, stream.clone(), append * 4 + 1, events).unwrap();
        authority.commit(transaction).unwrap();
    }
}

/// The checkpoint packs every entity-reachable block (pages, blob manifests
/// and chunks, stream segments) into one segment. The fixture re-homes those
/// blocks across `SEGMENTS` shuffled segments with filler, as the live
/// catalog's full-range segments hold them, publishes that catalog and
/// reopens, so no process cache or hint remembers where anything lives.
/// History sources and catalog roots stay loose.
fn mark_cost_authority() -> (Arc<CountingVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let inner = Arc::new(DeterministicVfs::new(None));
    inner.create_dir(Path::new("/data")).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let vfs = Arc::new(CountingVfs { inner, log: Arc::default() });
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    write_records(&mut authority);
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    drop(authority);
    let mut authority = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let blocks = authority.block_store_shared();
    let store = blocks.payload_segment_store().unwrap();
    let previous = blocks.installed_payload_manifest().unwrap().unwrap().segments;
    let mut packed = Vec::new();
    for reference in &previous {
        packed.extend(store.open_segment(reference.segment_id).unwrap().block_ids());
    }
    let mut state = 0x9e37_79b9_7f4a_7c15_u64;
    for index in (1..packed.len()).rev() {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        packed.swap(index, (state % (index as u64 + 1)) as usize);
    }
    assert!(packed.len() >= SEGMENTS);
    let mut references = Vec::with_capacity(SEGMENTS);
    for segment in 0..SEGMENTS {
        let filler: BTreeMap<BlockId, Vec<u8>> = (0..FILLER_BLOCKS_PER_SEGMENT)
            .map(|filler| {
                let payload = format!("mark-cost-filler-{segment}-{filler}").into_bytes();
                (BlockId::for_payload(&payload), payload)
            })
            .collect();
        let ids = packed.iter().skip(segment).step_by(SEGMENTS).copied().chain(filler.keys().copied()).collect();
        let created = store
            .create(Uuid::from_u128(0x6d61_726b_636f_7374 << 64 | segment as u128), ids, |id| {
                filler.get(&id).map_or_else(|| blocks.get(id), |payload| Ok(payload.clone()))
            })
            .unwrap();
        references.push(
            PayloadSegmentReference::new(created.metadata(), created.first_block_id(), created.last_block_id())
                .unwrap(),
        );
    }
    authority
        .engine_mut_for_test()
        .publish_payload_manifest_for_test(crate::payload_manifest::PayloadManifest::new(references).unwrap())
        .unwrap();
    for reference in &previous {
        store.remove_segment(reference.segment_id).unwrap();
    }
    drop(blocks);
    drop(authority);
    let authority = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, Arc::new(Mutex::new(authority)))
}

/// One complete mark build from a fresh capture; `located` selects the
/// catalog location table (true) or the exact catalog path alone (false).
fn mark(
    vfs: &CountingVfs,
    database: &Arc<Mutex<AuthorityDatabase>>,
    collector: &OnlineGarbageCollector,
    catalog: &[PayloadSegmentReference],
    located: bool,
) -> (BTreeSet<BlockId>, MarkCost) {
    let (marked, cost, _) = mark_capture(vfs, database, collector, catalog, located, |_| {});
    (marked, cost)
}

/// `mark` over a capture `adjust` may narrow; also returns the mark's
/// inserts (every reference it recorded, duplicates included).
fn mark_capture(
    vfs: &CountingVfs,
    database: &Arc<Mutex<AuthorityDatabase>>,
    collector: &OnlineGarbageCollector,
    catalog: &[PayloadSegmentReference],
    located: bool,
    adjust: impl FnOnce(&mut OnlineGcCapture),
) -> (BTreeSet<BlockId>, MarkCost, u64) {
    let mut capture = database
        .lock()
        .unwrap()
        .online_gc_capture(test_budget().maximum_temporary_bytes)
        .unwrap();
    adjust(&mut capture);
    let mut epoch = unmarked_epoch(capture, collector);
    let blocks = Arc::clone(&epoch.capture.blocks);
    blocks.set_cache_capacity_bytes(0).unwrap();
    blocks.set_cache_capacity_bytes(256 * 1024 * 1024).unwrap();
    crate::block::set_catalog_locations_disabled_for_test(!located);
    vfs.take();
    read_path_counters::take();
    let built = collector.build_marks(&mut epoch, &AtomicBool::new(false));
    let counters = read_path_counters::take();
    let log = vfs.take();
    crate::block::set_catalog_locations_disabled_for_test(false);
    built.unwrap();
    let mut marks = epoch.marks.take().unwrap();
    let inserts = marks.finish().unwrap().references;
    let mut marked = BTreeSet::new();
    for shard in 0..=u8::MAX {
        marked.extend(marks.live_for_shard(shard).unwrap());
    }
    marks.cleanup().unwrap();
    let cost = mark_cost(log, catalog, marked.len(), counters);
    (marked, cost, inserts)
}

fn per_block(value: u64, cost: &MarkCost) -> f64 {
    value as f64 / cost.marked_blocks as f64
}

fn report(label: &str, cost: &MarkCost) {
    println!(
        "{label}: marked={} whole_index_reads={} ({:.3}/marked) index_bytes={} ({:.1}/marked) \
         index_entry_reads={} payload_reads={} payload_bytes={} loose_opens={} loose_bytes={} \
         witness_checks={} ({:.1}/marked) location_candidates={}",
        cost.marked_blocks,
        cost.whole_index_reads,
        per_block(cost.whole_index_reads, cost),
        cost.index_bytes,
        per_block(cost.index_bytes, cost),
        cost.index_entry_reads,
        cost.payload_reads,
        cost.payload_bytes,
        cost.loose_opens,
        cost.loose_bytes,
        cost.witness_checks,
        per_block(cost.witness_checks, cost),
        cost.location_candidates,
    );
}

/// Live shape on 2026-10-31: ~220 whole verified index opens/s against 437
/// marked blocks/s, and a witness check per catalogued segment per lookup.
/// With the location table a mark reads each non-resident catalogued index
/// once, while building the table, and then one candidate, at most one index
/// entry and the payload per located block. No witness is walked, the
/// verified-index and payload caches admit nothing, and the marks equal the
/// exact catalog path's.
#[test]
fn located_mark_reads_each_index_once_and_one_candidate_per_block_with_identical_marks() {
    let (vfs, database) = mark_cost_authority();
    let blocks = database.lock().unwrap().block_store_shared();
    let store = blocks.payload_segment_store().unwrap();
    let catalog = blocks.installed_payload_manifest().unwrap().unwrap().segments;
    assert_eq!(catalog.len(), SEGMENTS);
    let index_bytes: u64 = catalog
        .iter()
        .map(|reference| u64::from(reference.block_count) * SEGMENT_INDEX_ENTRY_BYTES)
        .sum();
    blocks.set_segment_index_cache_bytes((index_bytes / 4) as usize).unwrap();
    let collector = OnlineGarbageCollector::new(test_budget());

    let resident = store.resident_index_ids_for_test();
    let (located_marks, located) = mark(&vfs, &database, &collector, &catalog, true);
    report("location_table", &located);
    assert_eq!(store.resident_index_ids_for_test(), resident, "a located mark admits no verified index");
    let admitted: Vec<_> = located_marks.iter().filter(|id| blocks.cache_contains_for_test(**id)).collect();
    assert!(admitted.is_empty(), "a located mark admitted {admitted:?} to the payload cache");
    let (exact_marks, exact) = mark(&vfs, &database, &collector, &catalog, false);
    report("exact_catalog_path", &exact);

    assert_eq!(located_marks, exact_marks, "the location table changes no mark");
    let resident_catalogued =
        catalog.iter().filter(|reference| resident.contains(&reference.segment_id)).count() as u64;
    assert_eq!(located.whole_index_reads, SEGMENTS as u64 - resident_catalogued);
    assert_eq!(located.most_whole_reads_of_one_index, 1);
    assert_eq!(located.witness_checks, 0);
    assert_eq!(located.location_candidates, located.payload_reads, "one candidate per located block");
    assert!(located.index_entry_reads <= located.payload_reads);
    assert_eq!((located.payload_reads, located.payload_bytes), (exact.payload_reads, exact.payload_bytes));
    assert_eq!(located.loose_opens, exact.loose_opens);
    assert!(exact.whole_index_reads > located.whole_index_reads);
    assert!(exact.witness_checks > 0);
}

/// Roots the shared-page fixture pins besides the current root.
const PINNED_ROOTS: u32 = 8;

/// The mark-cost records and documents plus one persistent root pin,
/// checkpointed into one payload segment and reopened. Then `PINNED_ROOTS`
/// open transactions each pin the root of one more single-record update, so
/// neighbouring roots share every page but one leaf-to-root path. Every root
/// declares the same persistent pin, whose root shares all but its own path.
fn pinned_roots_authority() -> (Arc<CountingVfs>, Arc<Mutex<AuthorityDatabase>>, Vec<AuthorityTransaction>) {
    let inner = Arc::new(DeterministicVfs::new(None));
    inner.create_dir(Path::new("/data")).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let vfs = Arc::new(CountingVfs { inner, log: Arc::default() });
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    write_records(&mut authority);
    let mut transaction = authority.begin(7, 11).unwrap();
    authority.stage_persistent_snapshot_pin(&mut transaction, b"mark-cost-pin").unwrap();
    authority.commit(transaction).unwrap();
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    drop(authority);
    let mut authority = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    authority.set_write_overlay_for_test(false);
    authority.configure_hot_data_caches(0, 0).unwrap();
    let mut pins = Vec::new();
    for root in 0..PINNED_ROOTS {
        pins.push(authority.begin(7, 11).unwrap());
        let mut transaction = authority.begin(7, 11).unwrap();
        let key = EntityKey::new(7, 11, "markcost", &(root * 300 + 150).to_be_bytes()).unwrap();
        authority.entity_put(&mut transaction, key, format!("pinned-root-update-{root}").into_bytes()).unwrap();
        authority.commit(transaction).unwrap();
    }
    (vfs, Arc::new(Mutex::new(authority)), pins)
}

/// Walking each root on its own through one traversal reader, the way marks
/// walked their roots before 2026-10-31.
struct PerRootWalks {
    marked: BTreeSet<BlockId>,
    reads: BTreeMap<BlockId, u64>,
    inserts: u64,
    /// Inserts of walking every distinct page once: the page plus the value
    /// references its entries hold.
    distinct_page_inserts: u64,
    /// Reads of walking every distinct page once, per distinct page: the page
    /// and the blob manifests its entries reference.
    distinct_page_reads: BTreeMap<BlockId, Vec<BlockId>>,
}

fn per_root_walks(blocks: &BlockStore, roots: &[Option<BlockId>]) -> PerRootWalks {
    let reader = blocks.traversal_reader(|| Ok(())).unwrap();
    let reads = RefCell::new(BTreeMap::<BlockId, u64>::new());
    // A page is read, then its values are visited, then the page itself: the
    // reads and value references since the previous page belong to it.
    let page_reads = RefCell::new(Vec::new());
    let read = |id: BlockId| {
        *reads.borrow_mut().entry(id).or_default() += 1;
        page_reads.borrow_mut().push(id);
        reader.get(id)
    };
    let marked = RefCell::new(BTreeSet::new());
    let inserts = std::cell::Cell::new(0_u64);
    let leaf_references = std::cell::Cell::new(0_u64);
    let page_inserts = RefCell::new(BTreeMap::<BlockId, u64>::new());
    let distinct_page_reads = RefCell::new(BTreeMap::new());
    for root in roots {
        visit_entity_page_graph_with_values(
            *root,
            &read,
            |id, _| {
                marked.borrow_mut().insert(id);
                inserts.set(inserts.get() + 1);
                page_inserts.borrow_mut().entry(id).or_insert(1 + leaf_references.replace(0));
                distinct_page_reads.borrow_mut().entry(id).or_insert(std::mem::take(&mut *page_reads.borrow_mut()));
                Ok(())
            },
            |key, value| {
                visit_semantic_value_references(key, value, &read, |id| {
                    marked.borrow_mut().insert(id);
                    inserts.set(inserts.get() + 1);
                    leaf_references.set(leaf_references.get() + 1);
                    Ok(())
                })
            },
        )
        .unwrap();
    }
    let distinct_page_inserts = page_inserts.borrow().values().sum();
    PerRootWalks {
        marked: marked.into_inner(),
        reads: reads.into_inner(),
        inserts: inserts.get(),
        distinct_page_inserts,
        distinct_page_reads: distinct_page_reads.into_inner(),
    }
}

/// Open transactions pin roots that share every page but one path with each
/// other and with the current root, and every root declares one persistent
/// pin whose root shares the rest. Walked one root at a time, a mark read and
/// inserted each shared page once per root and once more per walk of the
/// persistent pin. A page ID fixes its whole subtree, so one mark reads and
/// inserts each distinct page, with the manifests and value references of its
/// entries, once, while its marks still cover every root. (Distinct pages that
/// reference one blob each read its manifest: the persistent pin's root and
/// the current root hold two versions of one leaf.)
#[test]
fn mark_reads_and_inserts_each_page_shared_by_pinned_roots_once() {
    let (vfs, database, pins) = pinned_roots_authority();
    let blocks = database.lock().unwrap().block_store_shared();
    let catalog = blocks.installed_payload_manifest().unwrap().unwrap().segments;
    let collector = OnlineGarbageCollector::new(test_budget());
    let roots = {
        let capture = database.lock().unwrap().online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
        let mut roots = vec![capture.snapshot.authority_state_root];
        roots.extend(capture.snapshot.pinned_roots.iter().copied().map(Some));
        roots
    };
    assert_eq!(roots.len(), PINNED_ROOTS as usize + 1);

    vfs.take();
    read_path_counters::take();
    let per_root = per_root_walks(&blocks, &roots);
    let per_root_cost = mark_cost(vfs.take(), &catalog, per_root.marked.len(), read_path_counters::take());
    let (marked, cost, inserts) = mark_capture(&vfs, &database, &collector, &catalog, true, |_| {});
    let (_, rootless, rootless_inserts) = mark_capture(&vfs, &database, &collector, &catalog, true, |capture| {
        capture.snapshot.authority_state_root = None;
        capture.snapshot.pinned_roots.clear();
    });
    let distinct_page_catalogued_reads = per_root
        .distinct_page_reads
        .values()
        .flatten()
        .filter(|id| blocks.has_catalogued_payload_block(**id).unwrap())
        .count() as u64;
    report("per_root_walks", &per_root_cost);
    report("mark", &cost);
    println!(
        "roots={} per_root: page_and_manifest_reads={} distinct_blocks_read={} inserts={} \
         most_reads_of_one_payload={}; mark: root_payload_reads={} root_inserts={} \
         most_reads_of_one_payload={}",
        roots.len(),
        per_root.reads.values().sum::<u64>(),
        per_root.reads.len(),
        per_root.inserts,
        per_root_cost.most_reads_of_one_payload,
        cost.payload_reads - rootless.payload_reads,
        inserts - rootless_inserts,
        cost.most_reads_of_one_payload,
    );

    assert!(per_root.marked.is_subset(&marked), "the mark covers every root");
    assert_eq!(
        per_root_cost.most_reads_of_one_payload,
        2 * (u64::from(PINNED_ROOTS) + 1),
        "walked root by root, a shared page is read by every root and every walk of its persistent pin"
    );
    assert_eq!(cost.payload_reads - rootless.payload_reads, distinct_page_catalogued_reads);
    assert_eq!(inserts - rootless_inserts, per_root.distinct_page_inserts);
    drop(pins);
}

/// The payload scan intersects each segment of its catalog window with the
/// completed mark. Against a mark spilled into several runs, the window is
/// answered from one pass over the runs (each run read once, not once per
/// scanned segment), and every window segment's dead set is exactly its
/// unmarked IDs.
#[test]
fn segment_window_scan_reads_each_mark_run_once() {
    let (_vfs, database) = mark_cost_authority();
    let mut collector = OnlineGarbageCollector::new(test_budget());
    collector.payload_window_cursor = Some(0);
    let capture = database
        .lock()
        .unwrap()
        .online_gc_capture(test_budget().maximum_temporary_bytes)
        .unwrap();
    let blocks = Arc::clone(&capture.blocks);
    let protected = capture.snapshot.payload_manifest_block_id;
    let store = blocks.payload_segment_store().unwrap();
    let catalog = blocks.installed_payload_manifest().unwrap().unwrap().segments;
    assert_eq!(catalog.len(), SEGMENTS);
    let window_len = MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED;
    let mut live = Vec::new();
    let mut expected_dead = BTreeMap::new();
    for (segment, reference) in catalog.iter().enumerate() {
        let mut dead = Vec::new();
        for (position, id) in store.open_segment(reference.segment_id).unwrap().block_ids().enumerate() {
            if segment < window_len && position % 4 == segment % 4 && Some(id) != protected {
                dead.push(id);
            } else {
                live.push(id);
            }
        }
        if !dead.is_empty() {
            expected_dead.insert(reference.segment_id, dead);
        }
    }
    assert_eq!(expected_dead.len(), window_len);

    // Every live ID, some twice, among filler IDs that spill past two full
    // runs, shuffled so that every run holds live IDs.
    let mut inserts = live.clone();
    inserts.extend(live.iter().step_by(7).copied());
    let mut state = 0x6d61_726b_7275_6e73_u64;
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    for _ in 0..140_000 {
        let mut id = [0_u8; 32];
        for word in id.chunks_mut(8) {
            word.copy_from_slice(&next().to_le_bytes());
        }
        inserts.push(BlockId(id));
    }
    for index in (1..inserts.len()).rev() {
        inserts.swap(index, (next() % (index as u64 + 1)) as usize);
    }
    let inner = Arc::new(DeterministicVfs::new(None));
    for directory in ["/window-marks", "/window-marks/gc-runs"] {
        inner.create_dir(Path::new(directory)).unwrap();
    }
    inner.sync_directory(Path::new("/")).unwrap();
    inner.sync_directory(Path::new("/window-marks")).unwrap();
    let marks_vfs = Arc::new(CountingVfs { inner, log: Arc::default() });
    let mut marks = SpillMarkSet::initialize(Path::new("/window-marks"), marks_vfs.clone()).unwrap();
    for id in inserts {
        marks.insert(id).unwrap();
    }
    let spilled = marks.finish().unwrap();
    assert!(spilled.run_count >= 3, "{spilled:?}");

    marks_vfs.take();
    let mut epoch = unmarked_epoch(capture, &collector);
    collector
        .scan_segment_window(&mut epoch, &blocks, &marks, &AtomicBool::new(false))
        .unwrap();
    let run_bytes = marks_vfs.take().mark_run_bytes;
    assert_eq!(epoch.segment_dead_next, expected_dead);
    println!(
        "segment_window_scan: window_segments={window_len} mark_runs={} spill_bytes={} \
         mark_run_bytes_read={run_bytes}",
        spilled.run_count, spilled.spill_bytes,
    );
    assert!(
        run_bytes <= spilled.spill_bytes,
        "a window of {window_len} segments read {run_bytes} mark-run bytes; one pass over {} runs is {}",
        spilled.run_count,
        spilled.spill_bytes,
    );
    marks.cleanup().unwrap();
}
