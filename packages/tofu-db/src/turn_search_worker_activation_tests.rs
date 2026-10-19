//! Activation preparation must not serialize foreground writes behind target I/O.
//! Deterministic VFS gates also exercise the source-generation reentry fence.
use super::*;
use crate::semantic_executor::search_backfill;
use crate::vfs::{BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs,
    FileKind, OpenRequest, Vfs, VfsFile};
use std::path::{Path, PathBuf};
use std::sync::{atomic::{AtomicBool, Ordering}, mpsc, Condvar};
use std::time::Duration;

struct IoGate {
    armed: AtomicBool,
    entered: mpsc::SyncSender<()>,
    released: Mutex<bool>,
    wake: Condvar,
}
impl IoGate {
    fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.wake.notify_all();
    }
}
impl IoGate {
    fn pause(&self) -> io::Result<()> {
        if self.armed.swap(false, Ordering::AcqRel) {
            self.entered.send(()).unwrap();
            let (released, _) = self.wake.wait_timeout_while(self.released.lock().unwrap(),
                Duration::from_secs(10), |released| !*released).unwrap();
            if !*released { return Err(io::Error::new(io::ErrorKind::TimedOut, "target gate expired")); }
        }
        Ok(())
    }
}
struct PausedTarget { inner: Arc<DeterministicVfs>, gate: Arc<IoGate> }
struct PausedFile { inner: Box<dyn VfsFile>, gate: Arc<IoGate> }
impl VfsFile for PausedFile {
    fn len(&self) -> io::Result<u64> { self.inner.len() }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> { self.inner.read_all(maximum) }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.gate.pause()?;
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> { self.inner.set_len(length) }
    fn sync_data(&mut self) -> io::Result<()> { self.inner.sync_data() }
    fn sync_all(&mut self) -> io::Result<()> { self.inner.sync_all() }
    fn try_lock_exclusive(&mut self) -> io::Result<()> { self.inner.try_lock_exclusive() }
}
impl Vfs for PausedTarget {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        Ok(Box::new(PausedFile { inner: self.inner.open(path, request)?, gate: Arc::clone(&self.gate) }))
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.inner.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { self.inner.rename(source, destination) }
    fn remove_file(&self, path: &Path) -> io::Result<()> { self.inner.remove_file(path) }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> { self.inner.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> { self.inner.sync_directory(path) }
}

fn create(database: &mut AuthorityDatabase, name: &str) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    crate::conversation_header::create(database, &mut transaction,
        &crate::conversation_header::CreateRequest {
            conversation_id: name.into(), title: "activationneedle".into(),
            settings_json: b"{}".to_vec(), created_at_ms: 100,
            updated_at_ms: 100, committed_at_ms: 100,
        }).unwrap();
    database.commit(transaction).unwrap();
}

#[test]
fn activation_target_io_releases_writer_and_fences_changed_source() {
    for change in ["ordinary-write", "activation", "source-uuid", "writer-busy", "stop"] {
        let source_dir = tempfile::tempdir().unwrap();
        let replacement_dir = tempfile::tempdir().unwrap();
        let mut source = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        create(&mut source, "before");
        let authority = Arc::new(Mutex::new(source));
        let (entered, receiver) = mpsc::sync_channel(1);
        let inner = Arc::new(DeterministicVfs::new(None));
        inner.create_dir(Path::new("/target")).unwrap();
        inner.sync_directory(Path::new("/")).unwrap();
        let gate = Arc::new(IoGate { entered, armed: AtomicBool::new(false),
            released: Mutex::new(false), wake: Condvar::new() });
        let vfs = Arc::new(PausedTarget { inner, gate: Arc::clone(&gate) });
        let projection = Arc::new(Mutex::new(TurnSearchProjection::initialize_with_vfs(
            Path::new("/target"), vfs.clone(), 16 * 1024 * 1024).unwrap()));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let worker_source = Arc::clone(&authority);
        let worker_target = Arc::clone(&projection);
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = Arc::clone(&stop);
        gate.armed.store(true, Ordering::Release);
        let worker = std::thread::spawn(move ||
            process_turn_search_batch_with_control(&worker_source, &worker_target, scope,
                || false, wait_for_authority_durability,
                || worker_stop.load(Ordering::Acquire)));
        let blocked = receiver.recv_timeout(Duration::from_secs(2));
        let replacement_activation = uuid::Uuid::new_v4();
        let progressed = if let Ok(mut database) = authority.try_lock() {
            if change == "activation" {
                let mut transaction = database.begin(7, 11).unwrap();
                search_backfill::activate(&database, &mut transaction, replacement_activation).unwrap();
                database.commit(transaction).unwrap();
            } else if change == "source-uuid" {
                *database = AuthorityDatabase::initialize(replacement_dir.path()).unwrap();
            }
            create(&mut database, "foreground");
            true
        } else { false };
        let held_writer = (change == "writer-busy").then(|| authority.lock().unwrap());
        if change == "stop" { stop.store(true, Ordering::Release); }
        // Always release/join before assertions, including the old-code red arm.
        gate.release();
        let outcome = worker.join().unwrap();
        drop(held_writer);
        blocked.expect("prepare_owner must reach real target I/O");
        assert!(progressed, "foreground commit blocked behind {change} target I/O");
        let metrics = outcome.unwrap();
        if change != "ordinary-write" {
            assert_eq!(metrics.foreground_deferrals, u64::from(change != "stop"));
            assert_eq!(metrics.acknowledged_tokens, 0);
            assert_eq!(metrics.rebuilt_conversations, 0);
            let database = authority.lock().unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            assert_eq!(search_backfill::load_activation(&database, &mut transaction).unwrap(),
                (change == "activation").then_some(replacement_activation));
        }
        // Retained pending target + durable dirty/checkpoint state converge on retry.
        process_turn_search_batch(&authority, &projection, scope).unwrap();
        let database = authority.lock().unwrap();
        assert!(search_dirty::list(&database, 7, 11).unwrap().is_empty());
        drop(database);
        let hits = projection.lock().unwrap().search(
            &crate::turn_search_projection::ConversationSearchRequest {
                tenant_id: 7, owner_user_id: 11, query: "activationneedle".into(),
                limit: 10, snippet_radius: 10,
            }).unwrap();
        assert_eq!(hits.len(), if change == "source-uuid" { 1 } else { 2 });
    }
}
