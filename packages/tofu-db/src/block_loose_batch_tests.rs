//! Loose batch publication (`BlockStore::flush_pending_selection`): per-shard
//! durability, concurrent shard workers, fault retention and retry, stage
//! attribution, and the shard directory barrier across a failed creator.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation, RealVfs};
use std::collections::HashSet;
use std::sync::Condvar;
use std::thread::ThreadId;
use std::time::Duration;

const DATA: &str = "/data";

fn simulated_store(vfs: &DeterministicVfs) -> BlockStore {
    vfs.create_dir(Path::new(DATA)).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let store = BlockStore::initialize_with_vfs(Path::new(DATA), Arc::new(vfs.clone())).unwrap();
    store.set_cache_capacity_bytes(0).unwrap();
    store
}

fn concurrent_store() -> (DeterministicVfs, BlockStore) {
    let vfs = DeterministicVfs::new(None);
    vfs.allow_concurrent_file_publication().unwrap();
    let store = simulated_store(&vfs);
    (vfs, store)
}

/// Distinct staged payloads whose IDs land in more than one hash shard.
fn staged_batch(store: &BlockStore, count: usize) -> Vec<(BlockId, Vec<u8>)> {
    let staged: Vec<_> = (0..count)
        .map(|index| {
            let payload = format!("loose batch payload {index}").into_bytes();
            let (id, pending) = store.put_deferred_fresh(&payload).unwrap();
            assert!(pending);
            (id, payload)
        })
        .collect();
    let shards: HashSet<_> = staged.iter().map(|(id, _)| store.shard_directory(*id)).collect();
    assert!(shards.len() > 1, "the batch must span several shards");
    staged
}

fn ids(staged: &[(BlockId, Vec<u8>)]) -> BTreeSet<BlockId> {
    staged.iter().map(|(id, _)| *id).collect()
}

fn assert_staged(store: &BlockStore, staged: &[(BlockId, Vec<u8>)]) {
    for (id, payload) in staged {
        assert_eq!(store.pending_payload(*id).unwrap().unwrap().as_slice(), payload.as_slice());
    }
}

fn assert_retired(store: &BlockStore, staged: &[(BlockId, Vec<u8>)]) {
    for (id, _) in staged {
        assert!(store.pending_payload(*id).unwrap().is_none());
    }
}

/// Every block reads back after power loss and no temporary name survived.
fn assert_durable_after_power_loss(vfs: &DeterministicVfs, staged: &[(BlockId, Vec<u8>)]) {
    vfs.crash().unwrap();
    let reopened = BlockStore::open_with_vfs(Path::new(DATA), Arc::new(vfs.clone())).unwrap();
    reopened.set_cache_capacity_bytes(0).unwrap();
    for (id, payload) in staged {
        assert_eq!(&reopened.get(*id).unwrap(), payload);
    }
    for shard in vfs.read_directory(&reopened.root).unwrap() {
        for entry in vfs.read_directory(&shard).unwrap() {
            let name = entry.file_name().unwrap().to_string_lossy().into_owned();
            assert!(!name.starts_with(".new-"), "durable temporary {name}");
        }
    }
}

/// Operations of each kind that one fault-free batch of `count` performs.
fn operation_counts(count: usize) -> Vec<(Operation, usize)> {
    let (vfs, store) = concurrent_store();
    let staged = staged_batch(&store, count);
    vfs.arm_fault(None).unwrap();
    store.flush_pending_subset(&ids(&staged)).unwrap();
    let mut counts: Vec<(Operation, usize)> = Vec::new();
    for operation in vfs.trace().unwrap() {
        match counts.iter_mut().find(|(kind, _)| *kind == operation) {
            Some((_, count)) => *count += 1,
            None => counts.push((operation, 1)),
        }
    }
    counts
}

fn occurrences(counts: &[(Operation, usize)], operation: Operation) -> usize {
    counts.iter().find(|(kind, _)| *kind == operation).map_or(0, |(_, count)| *count)
}

#[test]
fn a_concurrent_multi_shard_batch_is_durable_and_retires_its_staged_copies() {
    let (vfs, store) = concurrent_store();
    let staged = staged_batch(&store, 96);
    store.flush_pending_subset(&ids(&staged)).unwrap();
    assert_retired(&store, &staged);
    assert_eq!(store.pending_staged_bytes().unwrap(), 0);
    assert_eq!(store.write_metrics().blocks_written, staged.len() as u64);
    drop(store);
    assert_durable_after_power_loss(&vfs, &staged);
}

/// Whatever single operation fails, on whichever shard worker it lands, the
/// batch reports the failure, keeps every staged copy, and the next attempt
/// publishes the whole batch durably.
#[test]
fn every_failed_operation_keeps_the_batch_staged_until_a_retry_publishes_it() {
    let count = 24;
    for (operation, occurrences) in operation_counts(count) {
        for occurrence in 1..=occurrences {
            let (vfs, store) = concurrent_store();
            let staged = staged_batch(&store, count);
            vfs.arm_operation_fault(operation, occurrence, FaultAction::ErrorBefore(io::ErrorKind::Other)).unwrap();
            let error = store.flush_pending_subset(&ids(&staged)).unwrap_err();
            assert!(vfs.operation_fault_fired().unwrap(), "{operation:?} #{occurrence}");
            assert_eq!(error.kind(), io::ErrorKind::Other, "{operation:?} #{occurrence}");
            assert_staged(&store, &staged);
            vfs.arm_fault(None).unwrap();
            store.flush_pending_subset(&ids(&staged)).unwrap();
            assert_retired(&store, &staged);
            drop(store);
            assert_durable_after_power_loss(&vfs, &staged);
        }
    }
}

/// A torn temporary write fails the batch and leaves no name behind; one
/// lost sync, file or directory, never loses a block the batch published.
#[test]
fn torn_writes_and_lost_syncs_never_publish_or_lose_a_block() {
    let count = 24;
    let counts = operation_counts(count);
    assert!(occurrences(&counts, Operation::Write) > 0);
    for occurrence in 1..=occurrences(&counts, Operation::Write) {
        let (vfs, store) = concurrent_store();
        let staged = staged_batch(&store, count);
        vfs.arm_operation_fault(Operation::Write, occurrence, FaultAction::ShortWrite(3)).unwrap();
        assert_eq!(store.flush_pending_subset(&ids(&staged)).unwrap_err().kind(), io::ErrorKind::WriteZero);
        assert!(vfs.operation_fault_fired().unwrap());
        assert_staged(&store, &staged);
        vfs.arm_fault(None).unwrap();
        store.flush_pending_subset(&ids(&staged)).unwrap();
        drop(store);
        assert_durable_after_power_loss(&vfs, &staged);
    }
    for operation in [Operation::SyncAll, Operation::SyncDirectory] {
        assert!(occurrences(&counts, operation) > 0);
        for occurrence in 1..=occurrences(&counts, operation) {
            let (vfs, store) = concurrent_store();
            let staged = staged_batch(&store, count);
            vfs.arm_operation_fault(operation, occurrence, FaultAction::DropSync).unwrap();
            store.flush_pending_subset(&ids(&staged)).unwrap();
            assert!(vfs.operation_fault_fired().unwrap(), "{operation:?} #{occurrence}");
            drop(store);
            assert_durable_after_power_loss(&vfs, &staged);
        }
    }
}

/// The barrier publishing a new shard directory can fail after the
/// directory exists. The retry finds it existing and must still publish its
/// name before writing into it, or every block in it vanishes on power loss.
#[test]
fn a_shard_whose_root_barrier_failed_is_published_by_the_next_writer() {
    let vfs = DeterministicVfs::new(None);
    let store = simulated_store(&vfs);
    let payload = b"written into a shard whose creator lost its barrier".to_vec();
    let (id, _) = store.put_deferred_fresh(&payload).unwrap();
    vfs.arm_operation_fault(Operation::SyncDirectory, 1, FaultAction::ErrorBefore(io::ErrorKind::Other)).unwrap();
    assert_eq!(store.flush_pending_subset(&BTreeSet::from([id])).unwrap_err().kind(), io::ErrorKind::Other);
    assert!(vfs.operation_fault_fired().unwrap());
    vfs.arm_fault(None).unwrap();
    store.flush_pending_subset(&BTreeSet::from([id])).unwrap();
    drop(store);
    assert_durable_after_power_loss(&vfs, &[(id, payload)]);
}

/// Records the thread and stage component of every loose temporary create,
/// and holds the first creator until a second one runs beside it.
struct RendezvousVfs {
    inner: DeterministicVfs,
    creates: Mutex<Vec<(ThreadId, usize)>>,
    arrived: Mutex<usize>,
    second_creator: Condvar,
}

impl Vfs for RendezvousVfs {
    fn supports_concurrent_file_publication(&self) -> bool {
        true
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let temporary = path.file_name().is_some_and(|name| name.to_string_lossy().starts_with(".new-"));
        if temporary {
            self.creates
                .lock()
                .unwrap()
                .push((std::thread::current().id(), crate::fsync_stats::current_component()));
            let mut arrived = self.arrived.lock().unwrap();
            *arrived += 1;
            if *arrived == 1 {
                let (arrived, waited) = self
                    .second_creator
                    .wait_timeout_while(arrived, Duration::from_secs(30), |arrived| *arrived < 2)
                    .unwrap();
                drop(arrived);
                assert!(!waited.timed_out(), "no second shard ran concurrently");
            } else {
                self.second_creator.notify_all();
            }
        }
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
        self.inner.read_directory(path)
    }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self, path: &Path, prefix: &str, maximum: usize, matches: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

#[test]
fn shard_workers_run_concurrently_under_the_callers_stage_component() {
    let inner = DeterministicVfs::new(None);
    inner.create_dir(Path::new(DATA)).unwrap();
    inner.sync_directory(Path::new("/")).unwrap();
    let vfs = Arc::new(RendezvousVfs {
        inner,
        creates: Mutex::new(Vec::new()),
        arrived: Mutex::new(0),
        second_creator: Condvar::new(),
    });
    let store = BlockStore::initialize_with_vfs(Path::new(DATA), vfs.clone()).unwrap();
    let staged = staged_batch(&store, 32);
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_MAINTENANCE);
    let flushed = store.flush_pending_subset(&ids(&staged));
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_FOREGROUND);
    flushed.unwrap();
    let creates = vfs.creates.lock().unwrap();
    assert_eq!(creates.len(), staged.len());
    assert!(creates.iter().map(|(thread, _)| thread).collect::<HashSet<_>>().len() > 1);
    assert!(creates
        .iter()
        .all(|(_, component)| *component == crate::fsync_stats::COMPONENT_MAINTENANCE));
}

#[test]
fn a_real_filesystem_batch_publishes_every_shard_without_leftover_temporaries() {
    let directory = tempfile::tempdir().unwrap();
    let store = BlockStore::initialize_with_vfs(directory.path(), Arc::new(RealVfs)).unwrap();
    let staged = staged_batch(&store, 300);
    store.flush_pending_subset(&ids(&staged)).unwrap();
    assert_retired(&store, &staged);
    drop(store);
    let reopened = BlockStore::open_with_vfs(directory.path(), Arc::new(RealVfs)).unwrap();
    reopened.set_cache_capacity_bytes(0).unwrap();
    for (id, payload) in &staged {
        assert_eq!(&reopened.get(*id).unwrap(), payload);
    }
    for shard in std::fs::read_dir(directory.path().join("blocks")).unwrap() {
        for entry in std::fs::read_dir(shard.unwrap().path()).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().into_owned();
            assert!(!name.starts_with(".new-"), "leftover temporary {name}");
        }
    }
}
