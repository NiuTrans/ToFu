//! Content-addressed rewrites across online-GC epochs
//! (docs/TOFUDB_ONLINE_GC.md, "Unlinked blocks never confirm from older
//! suspects"). A block's name is its content, so a later write can produce a
//! suspect's name again: as a fresh loose file after an epoch unlinked it, or
//! as a staged copy beside a garbage loose file. Neither is a
//! reference-without-write decision, so neither enters the relink fence.

use super::tests::{metrics, run_epoch, test_budget};
use super::*;
use crate::vfs::{BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, OpenRequest, VfsFile};

/// Hooks one commit between an epoch's capture hold and its scan, and fails
/// one directory barrier after the unlink it publishes.
struct RebirthVfs {
    inner: Arc<DeterministicVfs>,
    /// Runs once, right before the next `payload-segments` listing: the
    /// segment lane lists it after the capture hold and before marking.
    before_segment_listing: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    /// The next directory sync of this path fails.
    fail_directory_sync: Mutex<Option<PathBuf>>,
}

impl RebirthVfs {
    fn new() -> Arc<Self> {
        let inner = Arc::new(DeterministicVfs::new(None));
        inner.create_dir(Path::new("/data")).unwrap();
        inner.sync_directory(Path::new("/")).unwrap();
        Arc::new(Self {
            inner,
            before_segment_listing: Mutex::new(None),
            fail_directory_sync: Mutex::new(None),
        })
    }

    fn listing_hook(&self, path: &Path) {
        if path.ends_with("payload-segments") {
            let hook = self.before_segment_listing.lock().unwrap().take();
            if let Some(hook) = hook {
                hook();
            }
        }
    }
}

impl Vfs for RebirthVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        self.inner.open(path, request)
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
        let fail = {
            let mut armed = self.fail_directory_sync.lock().unwrap();
            let fail = armed.as_deref() == Some(path);
            if fail {
                *armed = None;
            }
            fail
        };
        if fail {
            return Err(io::Error::other("injected shard barrier failure"));
        }
        self.inner.sync_directory(path)
    }
}

/// Above the WAL carry limit a commit spills its staged copy to a loose file
/// and logs an identifier-only reference: the rebirth is a fresh loose write,
/// and the WAL holds no second copy.
fn reborn_payload() -> Vec<u8> {
    vec![0x5a; crate::transaction::MAX_CARRIED_PAYLOAD_BYTES + 1]
}

fn shard_directory(block_id: BlockId) -> PathBuf {
    Path::new("/data/blocks").join(&block_id.to_hex()[..2])
}

fn garbage_fixture(payload: &[u8]) -> (Arc<RebirthVfs>, Arc<Mutex<AuthorityDatabase>>, BlockId) {
    let vfs = RebirthVfs::new();
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let block_id = database.write_orphan_block_for_test(payload).unwrap();
    (vfs, Arc::new(Mutex::new(database)), block_id)
}

/// Commits a reference to `payload` after the next epoch's capture hold.
fn commit_after_next_capture(vfs: &RebirthVfs, database: &Arc<Mutex<AuthorityDatabase>>, payload: &[u8]) {
    let database = Arc::clone(database);
    let payload = payload.to_vec();
    *vfs.before_segment_listing.lock().unwrap() = Some(Box::new(move || {
        database.lock().unwrap().commit_block_payloads_for_test(&[payload]).unwrap();
    }));
}

fn read_block(database: &Arc<Mutex<AuthorityDatabase>>, block_id: BlockId) -> io::Result<Vec<u8>> {
    database.lock().unwrap().read_block_for_test(block_id)
}

/// The committed reference resolves now, after the next mark, and after a
/// crash of the volume.
fn assert_reborn_block_survives(
    vfs: &RebirthVfs,
    database: &Arc<Mutex<AuthorityDatabase>>,
    collector: &mut OnlineGarbageCollector,
    block_id: BlockId,
    payload: &[u8],
) {
    assert!(vfs.before_segment_listing.lock().unwrap().is_none(), "the commit ran inside the epoch");
    assert_eq!(read_block(database, block_id).expect("the committed reborn block survives"), payload);
    run_epoch(collector, database);
    assert_eq!(read_block(database, block_id).expect("the next mark keeps the reference"), payload);
    let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), Arc::new(vfs.inner.fork_durable().unwrap()))
        .expect("the crash image reopens");
    assert_eq!(reopened.read_block_for_test(block_id).expect("the reborn block is durable"), payload);
}

/// Epoch 2 confirms and unlinks the garbage block; a commit writes the same
/// content again after epoch 3's capture. Epoch 3's mark predates the commit,
/// and its previous suspects (epoch 2's scan) still list the block, so
/// before the fix epoch 3 confirmed the reborn file and unlinked it.
#[test]
fn block_reborn_after_its_unlink_survives_the_next_epoch() {
    let payload = reborn_payload();
    let (vfs, database, block_id) = garbage_fixture(&payload);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    assert_eq!(read_block(&database, block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    let reclaimed = metrics(&collector).total_reclaimed_blocks;
    assert!(reclaimed >= 1);

    commit_after_next_capture(&vfs, &database, &payload);
    run_epoch(&mut collector, &database);
    assert_reborn_block_survives(&vfs, &database, &mut collector, block_id, &payload);
    assert_eq!(metrics(&collector).total_reclaimed_blocks, reclaimed, "epochs 3 and 4 unlink nothing");
}

/// The unlink reaches the volume but its shard barrier fails, so epoch 2
/// ends with an error after its deletion hold. The retry must not confirm
/// the block from suspects observed before that unlink either.
#[test]
fn block_reborn_after_a_failed_epochs_unlink_survives_the_retry() {
    let payload = reborn_payload();
    let (vfs, database, block_id) = garbage_fixture(&payload);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    *vfs.fail_directory_sync.lock().unwrap() = Some(shard_directory(block_id));
    let error = collector.run_epoch(&database, &AtomicBool::new(false)).unwrap_err();
    assert_eq!(error.to_string(), "injected shard barrier failure");
    assert_eq!(read_block(&database, block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    let reclaimed = metrics(&collector).total_reclaimed_blocks;

    commit_after_next_capture(&vfs, &database, &payload);
    run_epoch(&mut collector, &database);
    assert_reborn_block_survives(&vfs, &database, &mut collector, block_id, &payload);
    assert_eq!(metrics(&collector).total_reclaimed_blocks, reclaimed, "the retry and the next epoch unlink nothing");
}

/// Blob writes stage complete bytes without probing for a loose duplicate,
/// so staging the content of a garbage loose block records nothing in the
/// relink fence. Epoch 2 confirms that loose file (epoch 1 observed it, epoch
/// 2's mark predates the commit); its deletion hold must leave a block that a
/// write staged. Before the fix it dropped the staged copy with the file: the
/// committed block was missing until a restart replayed the WAL, and for good
/// once a checkpoint retired that WAL.
#[test]
fn garbage_block_restaged_by_a_blob_write_survives_its_confirmation() {
    let payload = b"blob chunk identical to a garbage loose block".to_vec();
    let (vfs, database, block_id) = garbage_fixture(&payload);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    {
        let database = Arc::clone(&database);
        let payload = payload.clone();
        *vfs.before_segment_listing.lock().unwrap() = Some(Box::new(move || {
            let mut guard = database.lock().unwrap();
            let engine = guard.engine_mut_for_test();
            assert_eq!(engine.write_blob_block(&payload).unwrap(), block_id);
            engine.commit_references(b"blob chunk reference", &[block_id]).unwrap();
        }));
    }
    run_epoch(&mut collector, &database);
    assert!(vfs.before_segment_listing.lock().unwrap().is_none(), "the commit ran inside the epoch");
    assert_eq!(read_block(&database, block_id).expect("the staged committed block survives"), payload);
    database.lock().unwrap().checkpoint_for_test().unwrap();
    run_epoch(&mut collector, &database);
    assert_eq!(read_block(&database, block_id).expect("the checkpointed block survives"), payload);
    let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), Arc::new(vfs.inner.fork_durable().unwrap()))
        .expect("the crash image reopens");
    assert_eq!(reopened.read_block_for_test(block_id).expect("the block is durable"), payload);
    assert_eq!(metrics(&collector).total_reclaimed_blocks, 0);
}
