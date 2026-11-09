//! Loose-listing specifications (docs/TOFUDB_ONLINE_GC.md, "Loose listing
//! without a store-wide total"). A counting VFS attributes metadata calls and
//! read opens to loose block paths and records every shard listing, so these
//! specifications count eliminated work instead of timing it.

use super::tests::{metrics, run_epoch, test_budget};
use super::tests_capture::unmarked_epoch;
use super::*;
use crate::authority::AuthorityDatabase;
use crate::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, DirectoryEntry,
    OpenRequest, VfsFile, VfsPositionedReader,
};

/// The retired 20,000,000-entry store-wide listing total at test scale; the
/// fixtures list more loose entries than it, which used to fail every scan.
const RETIRED_LISTING_TOTAL_AT_TEST_SCALE: usize = 16;

#[derive(Default)]
struct ListingLog {
    block_metadata_calls: u64,
    block_read_opens: BTreeMap<PathBuf, u64>,
    shard_listing_entries: Vec<usize>,
}

struct ListingVfs {
    inner: Arc<DeterministicVfs>,
    log: Mutex<ListingLog>,
}

impl ListingVfs {
    fn take(&self) -> ListingLog {
        std::mem::take(&mut *self.log.lock().unwrap())
    }
}

fn is_loose_block(path: &Path) -> bool {
    path.extension().is_some_and(|extension| extension == "blk")
}

fn is_loose_shard(path: &Path) -> bool {
    path.parent() == Some(Path::new("/data/blocks"))
}

fn loose_path(block_id: BlockId) -> PathBuf {
    let hexadecimal = block_id.to_hex();
    Path::new("/data/blocks")
        .join(&hexadecimal[..2])
        .join(format!("{hexadecimal}.blk"))
}

impl Vfs for ListingVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        if request.read && !request.write && is_loose_block(path) {
            *self
                .log
                .lock()
                .unwrap()
                .block_read_opens
                .entry(path.to_path_buf())
                .or_default() += 1;
        }
        self.inner.open(path, request)
    }
    fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        self.inner.open_positioned_reader(path)
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
        if is_loose_block(path) {
            self.log.lock().unwrap().block_metadata_calls += 1;
        }
        self.inner.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(path)
    }
    fn read_directory_entries(&self, path: &Path) -> io::Result<Vec<DirectoryEntry>> {
        let listed = self.inner.read_directory_entries(path)?;
        if is_loose_shard(path) {
            self.log.lock().unwrap().shard_listing_entries.push(listed.len());
        }
        Ok(listed)
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

fn listing_authority() -> (Arc<DeterministicVfs>, Arc<ListingVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let inner = Arc::new(DeterministicVfs::new(None));
    inner.create_dir(Path::new("/data")).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let vfs = Arc::new(ListingVfs {
        inner: Arc::clone(&inner),
        log: Mutex::default(),
    });
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    inner.arm_fault(None).unwrap();
    (inner, vfs, Arc::new(Mutex::new(database)))
}

fn write_orphans(database: &Arc<Mutex<AuthorityDatabase>>, tag: &str, count: usize) -> Vec<BlockId> {
    let database = database.lock().unwrap();
    (0..count)
        .map(|index| {
            database
                .write_orphan_block_for_test(format!("{tag}-{index}").as_bytes())
                .unwrap()
        })
        .collect()
}

/// Entries of the most populated loose shard, read from the simulator itself.
fn largest_shard_entries(inner: &DeterministicVfs) -> usize {
    inner
        .read_directory(Path::new("/data/blocks"))
        .unwrap()
        .iter()
        .map(|shard| inner.read_directory(shard).unwrap().len())
        .max()
        .unwrap()
}

#[test]
fn scan_reclaims_a_store_above_the_retired_listing_total_one_shard_at_a_time() {
    let (inner, vfs, database) = listing_authority();
    let orphans = write_orphans(&database, "listing-total-orphan", 40);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    vfs.take();
    run_epoch(&mut collector, &database);
    let first = vfs.take();
    let listed: usize = first.shard_listing_entries.iter().sum();
    assert!(listed > RETIRED_LISTING_TOTAL_AT_TEST_SCALE, "listed {listed} loose entries");
    // Every listing holds one shard's entries, never the store's.
    assert_eq!(
        first.shard_listing_entries.iter().max(),
        Some(&largest_shard_entries(&inner))
    );

    run_epoch(&mut collector, &database);
    let database_guard = database.lock().unwrap();
    for orphan in &orphans {
        assert_eq!(
            database_guard.read_block_for_test(*orphan).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }
    assert!(metrics(&collector).total_reclaimed_blocks >= orphans.len() as u64);
}

/// Victims are unlinked during the scan, one per hold, shard by shard, and
/// admitted by their two observations alone: no count bounds what an epoch
/// reclaims (`maximum_blocks_per_round` sizes the offline round only). The
/// scan never sizes a victim beyond the one open its unlink makes; only
/// compaction candidates are sized, one read each.
#[test]
fn scan_unlinks_every_confirmed_victim_and_sizes_only_compaction_candidates() {
    const OFFLINE_VICTIMS_PER_ROUND: usize = 4;
    let (_inner, vfs, database) = listing_authority();
    let budget = AuthorityGarbageCollectionBudget {
        maximum_blocks_per_round: OFFLINE_VICTIMS_PER_ROUND,
        ..test_budget()
    };
    let mut collector = OnlineGarbageCollector::new(budget);
    // Observed unmarked by the first epoch, so the second confirms all 10.
    let confirmed = write_orphans(&database, "listing-confirmed-garbage", 10);
    run_epoch(&mut collector, &database);
    // Unmarked for the first time in the second scan: suspects only.
    let fresh = write_orphans(&database, "listing-fresh-garbage", 5);
    // Live loose files (written, then referenced without a staged copy):
    // enough for a compaction plan that survives the 128-block floor.
    let live_payloads: Vec<Vec<u8>> = (0..crate::authority_gc::MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS)
        .map(|index| format!("listing-live-{index}").into_bytes())
        .collect();
    {
        let mut database_guard = database.lock().unwrap();
        for payload in &live_payloads {
            database_guard.write_orphan_block_for_test(payload).unwrap();
        }
        database_guard
            .commit_block_payloads_for_test(&live_payloads)
            .unwrap();
    }

    // The second epoch runs phase by phase, so the log covers one scan.
    let capture = database
        .lock()
        .unwrap()
        .online_gc_capture(budget.maximum_temporary_bytes)
        .unwrap();
    let blocks = Arc::clone(&capture.blocks);
    let mut epoch = unmarked_epoch(capture, &collector);
    let stop = AtomicBool::new(false);
    collector.build_marks(&mut epoch, &stop).unwrap();
    vfs.take();
    collector.scan(&database, &mut epoch, &stop).unwrap();
    let log = vfs.take();
    let listed: usize = log.shard_listing_entries.iter().sum();
    let opens_of = |ids: &[BlockId]| -> u64 {
        ids.iter()
            .map(|id| log.block_read_opens.get(&loose_path(*id)).copied().unwrap_or(0))
            .sum()
    };
    // A staged payload is sized without I/O; every other candidate is one
    // size read of its loose file. A victim costs the one open of its unlink.
    let candidates_on_disk: Vec<BlockId> = epoch
        .compaction_ids
        .iter()
        .copied()
        .filter(|id| blocks.pending_payload(*id).unwrap().is_none())
        .collect();
    assert!(epoch.compaction_ids.len() >= live_payloads.len());
    let size_reads: u64 = log.block_read_opens.values().sum();
    assert_eq!(
        (
            log.block_metadata_calls,
            opens_of(&confirmed),
            opens_of(&fresh),
            opens_of(&candidates_on_disk),
            size_reads,
        ),
        (
            0,
            confirmed.len() as u64,
            0,
            candidates_on_disk.len() as u64,
            (candidates_on_disk.len() + confirmed.len()) as u64,
        ),
        "(metadata calls, confirmed-garbage opens, fresh-garbage opens, \
         candidate size reads, all opens) for {listed} listed loose entries"
    );
    assert!(size_reads < listed as u64, "{size_reads} size reads for {listed} listed entries");
    {
        let database_guard = database.lock().unwrap();
        for victim in &confirmed {
            assert_eq!(database_guard.read_block_for_test(*victim).unwrap_err().kind(),
                io::ErrorKind::NotFound, "a confirmed victim outlived the scan");
        }
        for suspect in &fresh {
            assert!(database_guard.read_block_for_test(*suspect).is_ok(), "a first-observed suspect was unlinked");
        }
    }
    collector.cleanup_epoch(&mut epoch);
}
