//! One loose-file unlink per online-GC authority hold
//! (docs/TOFUDB_ONLINE_GC.md, "One file per deletion hold"). On the
//! production beegfs-fuse mount an unlink costs 0.653 ms (mean), so a hold
//! that unlinked a 65,536-victim batch, or the 65,536 loose copies of a
//! compacted segment, kept the authority mutex for about 43 s: four times the
//! 10 s contention-wedge threshold. These specifications observe every loose
//! block unlink and CONTROL write together with the collector hold it runs in.

use super::tests::{backlog_budget, loose_backlog, test_budget};
use super::tests_fallback_slot::{durable_slots, key, recover_from_fallback};
use super::*;
use crate::entity::EntityKey;
use crate::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, DirectoryEntry, OpenRequest, VfsFile,
};
use std::cell::Cell;
use std::rc::Rc;
use std::sync::atomic::AtomicU64;
use std::sync::{mpsc, OnceLock, Weak};

type HoldAttemptHook = Box<dyn FnMut(Duration, Duration)>;

thread_local! {
    static BEFORE_SINGLE_FILE_HOLD: RefCell<Option<HoldAttemptHook>> = const { RefCell::new(None) };
}

/// The probe seam of `SingleFileHolds`: runs before every hold attempt,
/// after the pause, with the previous hold's duration and the time the
/// collector has spent off the mutex since that hold.
pub(super) fn before_single_file_hold(previous_hold: Duration, off_mutex: Duration) {
    BEFORE_SINGLE_FILE_HOLD.with(|hook| {
        if let Some(hook) = hook.borrow_mut().as_mut() {
            hook(previous_hold, off_mutex);
        }
    });
}

pub(super) struct ResetHoldAttemptHook;

impl Drop for ResetHoldAttemptHook {
    fn drop(&mut self) {
        BEFORE_SINGLE_FILE_HOLD.with(|hook| *hook.borrow_mut() = None);
    }
}

pub(super) fn before_every_hold_attempt(hook: impl FnMut(Duration, Duration) + 'static) -> ResetHoldAttemptHook {
    BEFORE_SINGLE_FILE_HOLD.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
    ResetHoldAttemptHook
}

/// The collector's port to one authority. Every collector hold that can
/// mutate files calls `gc_database_mut` exactly once, so the counter names
/// the hold each observed operation runs in.
pub(super) struct CountedHolds {
    pub(super) authority: AuthorityDatabase,
    holds: Arc<AtomicU64>,
}

impl OnlineGcDatabase for CountedHolds {
    fn gc_database(&self) -> io::Result<&AuthorityDatabase> {
        Ok(&self.authority)
    }
    fn gc_database_mut(&mut self) -> io::Result<&mut AuthorityDatabase> {
        self.holds.fetch_add(1, Ordering::SeqCst);
        Ok(&mut self.authority)
    }
    fn install_gc_handle(&mut self, handle: OnlineGarbageCollectionHandle) -> io::Result<()> {
        self.authority.install_gc_handle(handle)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) enum Observed {
    /// A loose block file unlink. `hold` names the collector hold it ran in,
    /// `None` when the authority mutex was free.
    Unlink { path: PathBuf, hold: Option<u64> },
    ControlWrite { hold: Option<u64> },
    HookStarted,
    HookFinished,
    /// The contention watch's probe (`try_lock`) through the hold seam.
    Probe { busy: bool },
}

pub(super) type Hook = Box<dyn FnOnce() + Send>;

#[derive(Default)]
pub(super) struct Observer {
    authority: OnceLock<Weak<Mutex<CountedHolds>>>,
    holds: Arc<AtomicU64>,
    events: Mutex<Vec<Observed>>,
    /// Runs once, at the next directory barrier of this path.
    on_directory_sync: Mutex<Option<(PathBuf, Hook)>>,
    /// Raises this stop flag when that many more loose unlinks have started.
    pub(super) stop_after_unlinks: Mutex<Option<(usize, Arc<AtomicBool>)>>,
}

impl Observer {
    /// The collector hold in progress, while the authority mutex is held.
    fn hold(&self) -> Option<u64> {
        let authority = self.authority.get()?.upgrade()?;
        let held = authority.try_lock().is_err();
        held.then(|| self.holds.load(Ordering::SeqCst))
    }

    fn note(&self, event: Observed) {
        self.events.lock().unwrap().push(event);
    }

    pub(super) fn take(&self) -> Vec<Observed> {
        std::mem::take(&mut *self.events.lock().unwrap())
    }
}

pub(super) struct ObservedVfs {
    pub(super) inner: Arc<DeterministicVfs>,
    pub(super) observer: Arc<Observer>,
}

impl ObservedVfs {
    fn new() -> Arc<Self> {
        let inner = Arc::new(DeterministicVfs::new(None));
        inner.create_dir(Path::new("/data")).unwrap();
        inner.sync_directory(Path::new("/")).unwrap();
        Self::over(inner)
    }

    /// Observes an existing image, such as a crash image to reopen.
    pub(super) fn over(inner: Arc<DeterministicVfs>) -> Arc<Self> {
        Arc::new(Self { inner, observer: Arc::default() })
    }

    pub(super) fn arm_directory_sync(&self, path: PathBuf, hook: Hook) {
        *self.observer.on_directory_sync.lock().unwrap() = Some((path, hook));
    }
}

fn is_loose_block(path: &Path) -> bool {
    path.extension().is_some_and(|extension| extension == "blk")
}

impl Vfs for ObservedVfs {
    fn supports_buffered_wal_writes(&self) -> bool {
        self.inner.supports_buffered_wal_writes()
    }
    fn filesystem_capacity(&self, path: &Path) -> io::Result<crate::vfs::FilesystemCapacity> {
        self.inner.filesystem_capacity(path)
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let file = self.inner.open(path, request)?;
        if path.ends_with("CONTROL") {
            return Ok(Box::new(ObservedControlFile { inner: file, observer: Arc::clone(&self.observer) }));
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
        if is_loose_block(path) {
            self.observer.note(Observed::Unlink { path: path.to_path_buf(), hold: self.observer.hold() });
            let mut armed = self.observer.stop_after_unlinks.lock().unwrap();
            if let Some((remaining, stop)) = armed.as_mut() {
                *remaining -= 1;
                if *remaining == 0 {
                    stop.store(true, Ordering::Release);
                    *armed = None;
                }
            }
        }
        self.inner.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        self.inner.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(path)
    }
    fn read_directory_entries(&self, path: &Path) -> io::Result<Vec<DirectoryEntry>> {
        self.inner.read_directory_entries(path)
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
        let hook = {
            let mut armed = self.observer.on_directory_sync.lock().unwrap();
            if armed.as_ref().is_some_and(|(armed_path, _)| armed_path == path) {
                armed.take().map(|(_, hook)| hook)
            } else {
                None
            }
        };
        if let Some(hook) = hook {
            self.observer.note(Observed::HookStarted);
            hook();
            self.observer.note(Observed::HookFinished);
        }
        self.inner.sync_directory(path)
    }
}

struct ObservedControlFile {
    inner: Box<dyn VfsFile>,
    observer: Arc<Observer>,
}

impl VfsFile for ObservedControlFile {
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
        self.observer.note(Observed::ControlWrite { hold: self.observer.hold() });
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

pub(super) fn observed_authority() -> (Arc<ObservedVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = ObservedVfs::new();
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, Arc::new(Mutex::new(database)))
}

/// Moves the populated fixture behind the hold-counting port.
pub(super) fn count_holds(vfs: &ObservedVfs, database: Arc<Mutex<AuthorityDatabase>>) -> Arc<Mutex<CountedHolds>> {
    let authority = Arc::try_unwrap(database)
        .ok()
        .expect("the fixture released every handle")
        .into_inner()
        .unwrap();
    let counted = Arc::new(Mutex::new(CountedHolds { authority, holds: Arc::clone(&vfs.observer.holds) }));
    assert!(vfs.observer.authority.set(Arc::downgrade(&counted)).is_ok());
    counted
}

pub(super) fn epoch(collector: &mut OnlineGarbageCollector, database: &Arc<Mutex<CountedHolds>>) {
    let outcome = collector.run_epoch(database, &AtomicBool::new(false)).unwrap();
    assert_eq!(outcome, OnlineGarbageCollectionOutcome::EpochComplete);
}

pub(super) fn loose_path(block_id: BlockId) -> PathBuf {
    let hexadecimal = block_id.to_hex();
    shard_directory(block_id).join(format!("{hexadecimal}.blk"))
}

pub(super) fn shard_directory(block_id: BlockId) -> PathBuf {
    Path::new("/data/blocks").join(&block_id.to_hex()[..2])
}

/// Garbage loose blocks, each in its own shard, sorted by shard: the scan
/// confirms them in this order, so one shard's directory barrier separates
/// every victim's hold from the next one's.
fn garbage_in_distinct_shards(database: &Arc<Mutex<AuthorityDatabase>>, count: usize) -> Vec<BlockId> {
    let database = database.lock().unwrap();
    let mut by_shard = BTreeMap::new();
    let mut index = 0_u32;
    while by_shard.len() < count {
        let payload = format!("short-hold-garbage-{index}").into_bytes();
        by_shard.entry(BlockId::for_payload(&payload).0[0]).or_insert(payload);
        index += 1;
    }
    by_shard
        .into_values()
        .map(|payload| database.write_orphan_block_for_test(&payload).unwrap())
        .collect()
}

/// Loose unlinks per collector hold, and how many ran off the mutex.
pub(super) fn unlinks_per_hold(events: &[Observed]) -> (BTreeMap<u64, usize>, usize) {
    let mut per_hold = BTreeMap::new();
    let mut off_mutex = 0;
    for event in events {
        match event {
            Observed::Unlink { hold: Some(hold), .. } => *per_hold.entry(*hold).or_insert(0) += 1,
            Observed::Unlink { hold: None, .. } => off_mutex += 1,
            _ => {}
        }
    }
    (per_hold, off_mutex)
}

pub(super) fn unlinked_paths(events: &[Observed]) -> BTreeSet<PathBuf> {
    events
        .iter()
        .filter_map(|event| match event {
            Observed::Unlink { path, .. } => Some(path.clone()),
            _ => None,
        })
        .collect()
}

/// Spec (a): a compacted segment's loose copies and a confirmed victim batch
/// are unlinked one file per collector hold. Before the change each
/// compaction hold unlinked its segment's 128 copies and one deletion hold
/// every victim.
#[test]
fn every_compaction_copy_and_garbage_victim_is_unlinked_in_its_own_hold() {
    let (vfs, database) = observed_authority();
    // Two segments deep: one commit above the carried ceiling spills every
    // block to its own loose file.
    let backlog = loose_backlog(&database, 2);
    let garbage = garbage_in_distinct_shards(&database, 64);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    vfs.observer.take();
    epoch(&mut collector, &database);
    let packing = vfs.observer.take();
    epoch(&mut collector, &database);
    let deleting = vfs.observer.take();

    let copies: BTreeSet<PathBuf> = backlog.iter().map(|(block_id, _)| loose_path(*block_id)).collect();
    assert_eq!(unlinked_paths(&packing), copies, "the packing epoch unlinks exactly the packed copies");
    assert!(unlinked_paths(&deleting).is_superset(&garbage.iter().map(|id| loose_path(*id)).collect()));
    let (packing_holds, packing_off_mutex) = unlinks_per_hold(&packing);
    let (deleting_holds, deleting_off_mutex) = unlinks_per_hold(&deleting);
    let most = |holds: &BTreeMap<u64, usize>| holds.values().copied().max().unwrap_or(0);
    eprintln!(
        "short holds: compaction {} unlinks in {} holds (most in one hold {}), deletion {} unlinks in {} holds (most in one hold {})",
        packing_holds.values().sum::<usize>(),
        packing_holds.len(),
        most(&packing_holds),
        deleting_holds.values().sum::<usize>(),
        deleting_holds.len(),
        most(&deleting_holds),
    );
    assert_eq!(
        (most(&packing_holds), most(&deleting_holds), packing_off_mutex, deleting_off_mutex),
        (1, 1, 0, 0),
        "(most copy unlinks in one hold, most victim unlinks in one hold, copy unlinks off the mutex, \
         victim unlinks off the mutex)"
    );
    let guard = database.lock().unwrap();
    for (block_id, payload) in &backlog {
        assert_eq!(&guard.authority.read_block_for_test(*block_id).unwrap(), payload);
    }
    for block_id in &garbage {
        assert_eq!(guard.authority.read_block_for_test(*block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
}

fn foreground_key() -> EntityKey {
    EntityKey::new(7, 11, "record", b"short-hold-foreground").unwrap()
}

/// Spec (b): a foreground request polls the authority with `try_lock`
/// (`server.rs`). One issued from another thread at the directory barrier
/// after the first victim's hold commits right away, and the deletion goes on
/// with the remaining victims. Before the change that barrier ran inside the
/// batch hold, so the request found the mutex held.
#[test]
fn a_foreground_commit_completes_between_two_deletion_holds() {
    let (vfs, database) = observed_authority();
    let garbage = garbage_in_distinct_shards(&database, 8);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    epoch(&mut collector, &database);
    let foreground: Arc<Mutex<Option<Result<(), String>>>> = Arc::default();
    {
        let database = Arc::downgrade(&database);
        let foreground = Arc::clone(&foreground);
        vfs.arm_directory_sync(
            shard_directory(garbage[0]),
            Box::new(move || {
                let database = database.upgrade().unwrap();
                let outcome = std::thread::spawn(move || -> Result<(), String> {
                    let mut guard = database
                        .try_lock()
                        .map_err(|_| "the authority mutex is held".to_owned())?;
                    let authority = &mut guard.authority;
                    let mut transaction = authority.begin(7, 11).map_err(|error| error.to_string())?;
                    authority
                        .entity_put(&mut transaction, foreground_key(), b"committed-between-holds".to_vec())
                        .map_err(|error| error.to_string())?;
                    authority.commit(transaction).map(|_| ()).map_err(|error| error.to_string())
                })
                .join()
                .unwrap();
                *foreground.lock().unwrap() = Some(outcome);
            }),
        );
    }
    vfs.observer.take();
    epoch(&mut collector, &database);
    let outcome = foreground.lock().unwrap().take().expect("the deletion reached the first victim's shard barrier");
    assert_eq!(outcome, Ok(()), "the foreground commit acquires the authority between two deletion holds");

    let events = vfs.observer.take();
    let hook = events.iter().position(|event| *event == Observed::HookStarted).unwrap();
    let victims_after: BTreeSet<PathBuf> = garbage[1..].iter().map(|id| loose_path(*id)).collect();
    assert!(unlinked_paths(&events[..hook]).contains(&loose_path(garbage[0])));
    assert_eq!(
        unlinked_paths(&events[hook..]).intersection(&victims_after).count(),
        victims_after.len(),
        "the deletion was in progress: every later victim goes after the commit"
    );
    let guard = database.lock().unwrap();
    let mut transaction = guard.authority.begin(7, 11).unwrap();
    assert_eq!(
        guard.authority.entity_get(&mut transaction, &foreground_key()).unwrap(),
        Some(b"committed-between-holds".to_vec())
    );
    for block_id in &garbage {
        assert_eq!(guard.authority.read_block_for_test(*block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
}

/// `pruned_history_authority` before its pruning: four checkpointed history
/// segments without carried payloads, so pruning them later publishes one
/// CONTROL generation and changes no catalog.
fn unpruned_history_authority(vfs: Arc<dyn Vfs>) -> AuthorityDatabase {
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
    database.set_write_overlay_for_test(false);
    let mut transaction = database.begin(7, 11).unwrap();
    database.entity_put(&mut transaction, key(), b"value-v3".to_vec()).unwrap();
    database.commit(transaction).unwrap();
    database.checkpoint_for_test().unwrap();
    for round in 0..3 {
        database
            .engine_mut_for_test()
            .commit(format!("inline-only history {round}").as_bytes())
            .unwrap();
        database.checkpoint_for_test().unwrap();
    }
    database
}

/// Spec (c): rule S8 per file. History pruning between two victims publishes
/// one CONTROL generation, so the older slot names the unpruned manifest. The
/// next victim's hold must decide from the slots as they are then, republish
/// CONTROL before its unlink, and leave both slots naming the current roots.
/// Before the change no publication could run between two victims: the
/// barrier ran inside the batch hold.
#[test]
fn a_control_publication_between_two_victims_is_revalidated_by_the_next_hold() {
    let vfs = ObservedVfs::new();
    let database = Arc::new(Mutex::new(unpruned_history_authority(vfs.clone())));
    let garbage = garbage_in_distinct_shards(&database, 8);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    epoch(&mut collector, &database);
    let published: Arc<Mutex<Option<Result<(), String>>>> = Arc::default();
    {
        let database = Arc::downgrade(&database);
        let published = Arc::clone(&published);
        vfs.arm_directory_sync(
            shard_directory(garbage[0]),
            Box::new(move || {
                let database = database.upgrade().unwrap();
                let outcome = database
                    .try_lock()
                    .map_err(|_| "the authority mutex is held".to_owned())
                    .and_then(|mut guard| {
                        guard.authority.compact_history(1).map(|_| ()).map_err(|error| error.to_string())
                    });
                *published.lock().unwrap() = Some(outcome);
            }),
        );
    }
    vfs.observer.take();
    epoch(&mut collector, &database);
    let outcome = published.lock().unwrap().take().expect("the deletion reached the first victim's shard barrier");
    assert_eq!(outcome, Ok(()), "history pruning publishes between two deletion holds");

    let events = vfs.observer.take();
    let started = events.iter().position(|event| *event == Observed::HookStarted).unwrap();
    let finished = events.iter().position(|event| *event == Observed::HookFinished).unwrap();
    assert!(
        events[started..finished].iter().any(|event| matches!(event, Observed::ControlWrite { .. })),
        "fixture: pruning publishes CONTROL"
    );
    let (next_unlink, next_hold) = events[finished..]
        .iter()
        .enumerate()
        .find_map(|(offset, event)| match event {
            Observed::Unlink { hold: Some(hold), .. } => Some((finished + offset, *hold)),
            _ => None,
        })
        .expect("victims remain after the publication");
    assert_eq!(
        events[finished..next_unlink]
            .iter()
            .filter(|event| **event == Observed::ControlWrite { hold: Some(next_hold) })
            .count(),
        1,
        "the next victim's hold republishes CONTROL once, before its unlink: {:?}",
        &events[finished..=next_unlink]
    );
    let current = database
        .lock()
        .unwrap()
        .authority
        .writer_engine()
        .unwrap()
        .state()
        .history_manifest_block_id;
    assert!(durable_slots(&vfs.inner).iter().all(|state| state.history_manifest_block_id == current));
    assert_eq!(recover_from_fallback(&vfs.inner).unwrap(), (current, b"value-v3".to_vec()));
    let guard = database.lock().unwrap();
    for block_id in &garbage {
        assert_eq!(guard.authority.read_block_for_test(*block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
}

pub(super) fn backlog_still_loose(database: &Arc<Mutex<CountedHolds>>, backlog: &[(BlockId, Vec<u8>)]) -> usize {
    let loose: BTreeSet<BlockId> = database
        .lock()
        .unwrap()
        .authority
        .block_store_shared()
        .list_block_ids(backlog.len() + 4096)
        .unwrap()
        .into_iter()
        .collect();
    backlog.iter().filter(|(block_id, _)| loose.contains(block_id)).count()
}

fn foreground_value_key(index: u32) -> EntityKey {
    EntityKey::new(7, 11, "record", format!("short-hold-foreground-{index}").as_bytes()).unwrap()
}

type Holder = (mpsc::Sender<()>, std::thread::JoinHandle<Result<(), String>>);

/// A contended authority postpones a deletion hold instead of ending the run.
/// Before every other hold attempt a foreground thread takes the authority
/// and keeps it across the collector's `try_lock`; before the retry it
/// commits and lets go. Every compaction copy and every garbage victim still
/// goes in its epoch, one per hold, and every foreground commit lands.
/// Before the change no hold attempt ran between two unlinks.
#[test]
fn contended_deletion_holds_retry_until_every_file_goes_while_foreground_commits_land() {
    let (vfs, database) = observed_authority();
    let backlog = loose_backlog(&database, 2);
    let garbage = garbage_in_distinct_shards(&database, 16);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    let holder: Rc<RefCell<Option<Holder>>> = Rc::default();
    let commits = Rc::new(Cell::new(0_u32));
    let reset = {
        let database = Arc::downgrade(&database);
        let holder = Rc::clone(&holder);
        let commits = Rc::clone(&commits);
        before_every_hold_attempt(move |_, _| {
            if let Some((release, thread)) = holder.borrow_mut().take() {
                release.send(()).unwrap();
                thread.join().unwrap().unwrap();
                commits.set(commits.get() + 1);
                return;
            }
            let database = database.upgrade().unwrap();
            let index = commits.get();
            let (locked, entered) = mpsc::channel();
            let (release, released) = mpsc::channel::<()>();
            let thread = std::thread::spawn(move || -> Result<(), String> {
                let mut guard = database.lock().map_err(|_| "the authority mutex is poisoned".to_owned())?;
                locked.send(()).unwrap();
                released.recv().unwrap();
                let authority = &mut guard.authority;
                let mut transaction = authority.begin(7, 11).map_err(|error| error.to_string())?;
                authority
                    .entity_put(&mut transaction, foreground_value_key(index), index.to_le_bytes().to_vec())
                    .map_err(|error| error.to_string())?;
                authority.commit(transaction).map(|_| ()).map_err(|error| error.to_string())
            });
            entered.recv().unwrap();
            *holder.borrow_mut() = Some((release, thread));
        })
    };
    vfs.observer.take();
    epoch(&mut collector, &database);
    let packing = vfs.observer.take();
    epoch(&mut collector, &database);
    let deleting = vfs.observer.take();
    drop(reset);

    assert!(holder.borrow().is_none(), "every contended attempt was retried");
    let most = |events: &[Observed]| unlinks_per_hold(events).0.values().copied().max().unwrap_or(0);
    assert_eq!((most(&packing), most(&deleting)), (1, 1));
    assert_eq!(backlog_still_loose(&database, &backlog), 0, "every compaction copy goes in its epoch");
    assert!(
        commits.get() as usize >= backlog.len() + garbage.len(),
        "one foreground commit took the authority before every successful hold: {}",
        commits.get()
    );
    let guard = database.lock().unwrap();
    for block_id in &garbage {
        assert_eq!(guard.authority.read_block_for_test(*block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
    let mut transaction = guard.authority.begin(7, 11).unwrap();
    for index in 0..commits.get() {
        assert_eq!(
            guard.authority.entity_get(&mut transaction, &foreground_value_key(index)).unwrap(),
            Some(index.to_le_bytes().to_vec())
        );
    }
}

/// The stop flag still ends a run of deletion holds, and rule S10 then
/// records exactly the victims whose files the run unlinked. The stop rises
/// while the third victim's hold unlinks, so the run ends before the fourth
/// hold; the next epoch confirms the rest from the stopped epoch's scan.
/// Before the change the stop was read once, before the batch hold.
#[test]
fn stop_between_deletion_holds_records_exactly_the_unlinked_victims() {
    let (vfs, database) = observed_authority();
    let garbage = garbage_in_distinct_shards(&database, 8);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    epoch(&mut collector, &database);
    let stop = Arc::new(AtomicBool::new(false));
    *vfs.observer.stop_after_unlinks.lock().unwrap() = Some((3, Arc::clone(&stop)));
    vfs.observer.take();
    let error = collector.run_epoch(&database, &stop).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    assert!(error.to_string().contains("online GC epoch stopped"), "{error}");
    let unlinked = unlinked_paths(&vfs.observer.take());
    assert_eq!(unlinked.len(), 3, "the run stops before the fourth victim's hold");
    let recorded: BTreeSet<PathBuf> =
        collector.previous.unlinked_loose.iter().map(|block_id| loose_path(*block_id)).collect();
    assert_eq!(recorded, unlinked, "rule S10 records exactly the unlinked victims");
    {
        let guard = database.lock().unwrap();
        for block_id in &garbage {
            assert_eq!(
                guard.authority.read_block_for_test(*block_id).is_err(),
                unlinked.contains(&loose_path(*block_id))
            );
        }
    }
    stop.store(false, Ordering::Release);
    epoch(&mut collector, &database);
    let guard = database.lock().unwrap();
    for block_id in &garbage {
        assert_eq!(guard.authority.read_block_for_test(*block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
}

/// Asserts that every retry from the second on (attempts 2..=retried) waited
/// off the mutex at least as long as the run had already spent retrying.
/// `attempts` holds each hold attempt's seam timestamp and the off-mutex wait
/// the run measured for it. Attempt 1's seam runs after attempt 0's failure,
/// so `attempts[1]` bounds the contention's start from above and the bound is
/// exact for the implementation: no host speed enters it.
fn assert_retries_back_off(attempts: &[(Instant, Duration)], retried: usize) {
    assert!(attempts.len() > retried, "{} attempts", attempts.len());
    let first_retry = attempts[1].0;
    for index in 2..=retried {
        let off_mutex = attempts[index].1;
        let retrying = attempts[index - 1].0.saturating_duration_since(first_retry);
        assert!(off_mutex >= retrying, "retry {index} waited {off_mutex:?} after {retrying:?} of retries");
    }
}

/// A run whose first hold finds the authority held has no previous hold to
/// pace it. It must still back off: each retry waits off the mutex at least
/// as long as the run has already been retrying, so retries thin out
/// geometrically instead of spinning on `try_lock`. A foreground thread holds
/// the authority across the first CONTENDED attempts of a garbage run, then
/// lets go. Before the change every retry followed the failed one at once.
#[test]
fn a_contended_first_deletion_hold_backs_off_instead_of_spinning() {
    const CONTENDED: usize = 8;
    let (vfs, database) = observed_authority();
    let garbage = garbage_in_distinct_shards(&database, 4);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    epoch(&mut collector, &database);
    let attempts: Rc<RefCell<Vec<(Instant, Duration)>>> = Rc::default();
    let holder: Rc<RefCell<Option<(mpsc::Sender<()>, std::thread::JoinHandle<()>)>>> = Rc::default();
    let reset = {
        let database = Arc::downgrade(&database);
        let attempts = Rc::clone(&attempts);
        let holder = Rc::clone(&holder);
        before_every_hold_attempt(move |_, off_mutex| {
            let index = attempts.borrow().len();
            attempts.borrow_mut().push((Instant::now(), off_mutex));
            if index == 0 {
                let database = database.upgrade().unwrap();
                let (locked, entered) = mpsc::channel();
                let (release, released) = mpsc::channel::<()>();
                let thread = std::thread::spawn(move || {
                    let _guard = database.lock().unwrap();
                    locked.send(()).unwrap();
                    released.recv().unwrap();
                });
                entered.recv().unwrap();
                *holder.borrow_mut() = Some((release, thread));
            } else if index == CONTENDED {
                let (release, thread) = holder.borrow_mut().take().unwrap();
                release.send(()).unwrap();
                thread.join().unwrap();
            }
        })
    };
    epoch(&mut collector, &database);
    drop(reset);

    assert!(holder.borrow().is_none(), "the authority was released");
    assert_retries_back_off(&attempts.borrow(), CONTENDED);
    let guard = database.lock().unwrap();
    for block_id in &garbage {
        assert_eq!(guard.authority.read_block_for_test(*block_id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
}

/// A hold refused with `WouldBlock` (CONTROL slots briefly apart while a
/// publication owns the lane) is retried by a compaction-copy run. Each retry
/// takes the authority mutex, so a refusal must back off like a contended
/// authority instead of re-taking the mutex at the pace of its own short
/// holds for as long as the publication lasts. Before the change a refused
/// hold was retried after only as long as that refused hold took.
#[test]
fn refused_deletion_holds_back_off_like_a_contended_authority() {
    const REFUSED: usize = 8;
    let (vfs, database) = observed_authority();
    let database = count_holds(&vfs, database);
    let blocks = database.lock().unwrap().authority.block_store_shared();
    let attempts: Rc<RefCell<Vec<(Instant, Duration)>>> = Rc::default();
    let reset = {
        let attempts = Rc::clone(&attempts);
        before_every_hold_attempt(move |_, off_mutex| attempts.borrow_mut().push((Instant::now(), off_mutex)))
    };
    let mut holds = SingleFileHolds::new(&blocks, Duration::ZERO);
    let mut refused = 0;
    loop {
        let outcome = holds.remove(&database, BlockId([7; 32]), |_| {
            if refused < REFUSED {
                refused += 1;
                Err(would_block("CONTROL slots briefly apart"))
            } else {
                Ok(LooseRemoval::Kept)
            }
        });
        match outcome {
            Some(Err(error)) if error.kind() == io::ErrorKind::WouldBlock => {}
            Some(result) => break result.unwrap(),
            None => panic!("nothing else takes the authority"),
        }
    }
    drop(reset);
    assert_eq!(refused, REFUSED);
    assert_retries_back_off(&attempts.borrow(), REFUSED);
}

/// The contention watch reports a wedge only after 10 s of probes that find
/// the authority held with no foreground progress between them. On an idle
/// authority its probe runs before every hold attempt of a long compaction
/// and deletion run, through the hold seam, and the VFS observes the mutex at
/// every unlink: no two unlinks are observed without a free probe between
/// them, and each attempt waits off the mutex at least as long as the hold
/// before it took (the 50% duty rule). Before the change no probe could run
/// inside a run, so one window spanned a segment's copies or a victim batch.
#[test]
fn an_idle_authority_is_free_between_every_two_deletion_holds() {
    let (vfs, database) = observed_authority();
    let backlog = loose_backlog(&database, 2);
    let garbage = garbage_in_distinct_shards(&database, 64);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    let waits: Rc<RefCell<Vec<(Duration, Duration)>>> = Rc::default();
    let reset = {
        let database = Arc::downgrade(&database);
        let observer = Arc::clone(&vfs.observer);
        let waits = Rc::clone(&waits);
        before_every_hold_attempt(move |previous_hold, off_mutex| {
            let busy = database.upgrade().unwrap().try_lock().is_err();
            observer.note(Observed::Probe { busy });
            waits.borrow_mut().push((previous_hold, off_mutex));
        })
    };
    vfs.observer.take();
    epoch(&mut collector, &database);
    epoch(&mut collector, &database);
    drop(reset);
    let events = vfs.observer.take();

    let unlinks = events.iter().filter(|event| matches!(event, Observed::Unlink { .. })).count();
    assert!(unlinks >= backlog.len() + garbage.len(), "{unlinks} unlinks");
    let (mut window, mut longest) = (0, 0);
    for event in &events {
        match event {
            Observed::Unlink { hold: Some(_), .. } | Observed::Probe { busy: true } => {
                window += 1;
                longest = usize::max(longest, window);
            }
            Observed::Probe { busy: false } => window = 0,
            _ => {}
        }
    }
    assert_eq!(longest, 1, "most consecutive observations that found the authority held");
    let waits = waits.borrow();
    assert!(waits.len() >= unlinks);
    assert!(
        waits.iter().all(|(previous_hold, off_mutex)| off_mutex >= previous_hold),
        "every attempt waits off the mutex at least as long as the previous hold"
    );
}
