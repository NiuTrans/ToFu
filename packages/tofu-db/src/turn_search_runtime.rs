//! Search-index runtime: capacity admission, deferred open and shared GC ports.
//! The index remains disposable; its errors never select another source authority.
use crate::authority::AuthorityDatabase;
use crate::authority_gc_online::{OnlineGarbageCollectionHandle, OnlineGcDatabase};
use crate::turn_search_projection::{
    ConversationSearchHit, ConversationSearchPage, ConversationSearchRequest,
    OpenTurnSearchProjection, TurnSearchDocument,
};
use crate::vfs::{RealVfs, Vfs};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub struct TurnSearchProjection {
    opened: Option<OpenTurnSearchProjection>,
    directory: PathBuf,
    vfs: Arc<dyn Vfs>,
    maximum_logical_bytes_per_owner: u64,
    source: Option<uuid::Uuid>,
    recover_real_filesystem: bool,
    gc: Option<OnlineGarbageCollectionHandle>,
    stop: Option<Arc<std::sync::atomic::AtomicBool>>,
    opening: bool,
}

impl TurnSearchProjection {
    fn from_opened(
        opened: OpenTurnSearchProjection,
        directory: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> Self {
        Self {
            opened: Some(opened),
            directory: directory.to_path_buf(),
            vfs,
            maximum_logical_bytes_per_owner,
            source: None,
            recover_real_filesystem: false,
            gc: None,
            stop: None,
            opening: false,
        }
    }
    pub fn initialize(directory: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        crate::search_volume::require_capacity(&RealVfs, directory)?;
        let opened =
            OpenTurnSearchProjection::initialize(directory, maximum_logical_bytes_per_owner)?;
        Ok(Self::from_opened(
            opened,
            directory,
            Arc::new(RealVfs),
            maximum_logical_bytes_per_owner,
        ))
    }
    pub fn initialize_with_vfs(
        directory: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        crate::search_volume::require_capacity(vfs.as_ref(), directory)?;
        let opened = OpenTurnSearchProjection::initialize_with_vfs(
            directory,
            Arc::clone(&vfs),
            maximum_logical_bytes_per_owner,
        )?;
        Ok(Self::from_opened(
            opened,
            directory,
            vfs,
            maximum_logical_bytes_per_owner,
        ))
    }
    pub fn open(directory: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        let opened = OpenTurnSearchProjection::open(directory, maximum_logical_bytes_per_owner)?;
        Ok(Self::from_opened(
            opened,
            directory,
            Arc::new(RealVfs),
            maximum_logical_bytes_per_owner,
        ))
    }
    pub fn open_with_vfs(
        directory: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        let opened = OpenTurnSearchProjection::open_with_vfs(
            directory,
            Arc::clone(&vfs),
            maximum_logical_bytes_per_owner,
        )?;
        Ok(Self::from_opened(
            opened,
            directory,
            vfs,
            maximum_logical_bytes_per_owner,
        ))
    }
    pub(crate) fn open_or_defer(
        directory: &Path,
        maximum_logical_bytes_per_owner: u64,
        source: uuid::Uuid,
    ) -> io::Result<Self> {
        Self::deferred(
            directory,
            Arc::new(RealVfs),
            maximum_logical_bytes_per_owner,
            source,
            true,
        )
    }
    pub(crate) fn open_or_defer_with_vfs(
        directory: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
        source: uuid::Uuid,
    ) -> io::Result<Self> {
        Self::deferred(
            directory,
            vfs,
            maximum_logical_bytes_per_owner,
            source,
            false,
        )
    }
    fn deferred(
        directory: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
        source: uuid::Uuid,
        recover_real_filesystem: bool,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "search projection budget must be positive"));
        }
        // The existing maintenance owner opens the disposable index after
        // readiness. Constructing this handle must never touch its volume.
        let runtime = Self {
            opened: None,
            directory: directory.to_path_buf(),
            vfs,
            maximum_logical_bytes_per_owner,
            source: Some(source),
            recover_real_filesystem,
            gc: None,
            stop: None,
            opening: false,
        };
        Ok(runtime)
    }
    fn ensure_open(&mut self) -> io::Result<()> {
        if self.opened.is_some() {
            return Ok(());
        }
        if self.opening {
            return Err(io::Error::new(io::ErrorKind::WouldBlock, "search index is opening in background"));
        }
        let stop = self.stop.clone();
        let should_stop = || stop.as_ref().is_some_and(|flag| flag.load(std::sync::atomic::Ordering::Acquire));
        if should_stop() { return Err(io::Error::new(io::ErrorKind::Interrupted, "search open cancelled")); }
        let mut opened = match self.vfs.metadata(&self.directory) {
            Ok(_) if self.recover_real_filesystem => OpenTurnSearchProjection::open_until(
                &self.directory,
                self.maximum_logical_bytes_per_owner,
                &should_stop,
            )?,
            Ok(_) => OpenTurnSearchProjection::open_with_vfs_until(
                &self.directory,
                Arc::clone(&self.vfs),
                self.maximum_logical_bytes_per_owner,
                &should_stop,
            )?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                crate::search_volume::require_capacity(self.vfs.as_ref(), &self.directory)?;
                if self.recover_real_filesystem {
                    OpenTurnSearchProjection::initialize(
                        &self.directory,
                        self.maximum_logical_bytes_per_owner,
                    )?
                } else {
                    self.vfs.create_dir(&self.directory)?;
                    crate::vfs::sync_directory_barrier(
                        self.vfs.as_ref(),
                        self.directory.parent().ok_or_else(|| {
                            io::Error::new(
                                io::ErrorKind::InvalidInput,
                                "search index parent missing",
                            )
                        })?,
                    )?;
                    OpenTurnSearchProjection::initialize_with_vfs(
                        &self.directory,
                        Arc::clone(&self.vfs),
                        self.maximum_logical_bytes_per_owner,
                    )?
                }
            }
            Err(error) => return Err(error),
        };
        if let Some(stop) = &self.stop {
            opened.install_stop(Arc::clone(stop));
        }
        if let Some(source) = self.source {
            opened.require_source_admission(source);
        }
        if let Some(handle) = &self.gc {
            opened
                .database
                .set_online_garbage_collection(handle.clone());
        }
        self.opened = Some(opened);
        Ok(())
    }
    /// Open on the existing maintenance worker without retaining the query/GC
    /// mutex. Foreground search reports unavailability immediately. A single
    /// opening claim owns the lease; errors release it for the existing retry.
    pub(crate) fn open_in_background(shared: &Arc<std::sync::Mutex<Self>>) -> io::Result<()> {
        let mut candidate = {
            let mut index = shared.lock().map_err(|_| io::Error::other("search index mutex is poisoned"))?;
            if index.opened.is_some() { return Ok(()); }
            if index.opening { return Err(io::Error::new(io::ErrorKind::WouldBlock, "search index is already opening")); }
            index.opening = true;
            Self { opened: None, directory: index.directory.clone(), vfs: Arc::clone(&index.vfs),
                maximum_logical_bytes_per_owner: index.maximum_logical_bytes_per_owner,
                source: index.source, recover_real_filesystem: index.recover_real_filesystem,
                gc: None, stop: index.stop.clone(), opening: false }
        };
        let result = crate::engine_log::lifecycle_step(
            "background_timing", "search_projection_open", || candidate.ensure_open());
        let mut index = shared.lock().map_err(|_| io::Error::other("search index mutex is poisoned"))?;
        index.opening = false;
        result?;
        if index.source != candidate.source {
            return Err(io::Error::new(io::ErrorKind::WouldBlock, "search source changed during open"));
        }
        if let Some(handle) = index.gc.clone() { candidate.install_gc_handle(handle)?; }
        if let Some(stop) = index.stop.clone() { candidate.install_stop(stop); }
        index.opened = candidate.opened.take();
        Ok(())
    }
    pub(crate) fn install_stop(&mut self, stop: Arc<std::sync::atomic::AtomicBool>) {
        if let Some(opened) = &mut self.opened { opened.install_stop(Arc::clone(&stop)); }
        self.stop = Some(stop);
    }
    pub(crate) fn available(&self) -> bool {
        self.opened.is_some()
    }
    fn opened(&self) -> io::Result<&OpenTurnSearchProjection> {
        self.opened.as_ref().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotConnected,
                "search index is opening in background",
            )
        })
    }
    fn readable(
        &self,
        request: &ConversationSearchRequest,
    ) -> io::Result<&OpenTurnSearchProjection> {
        let opened = self.opened()?;
        // A cold owner cannot be admitted while writes are paused. Reporting an
        // empty READY page here would hide the capacity failure as zero hits.
        if !self.owner_admitted(request.tenant_id, request.owner_user_id) {
            crate::search_volume::require_capacity(self.vfs.as_ref(), &self.directory)?;
        }
        Ok(opened)
    }
    fn writable(&mut self) -> io::Result<&mut OpenTurnSearchProjection> {
        // Existing indexes must open even on a full volume so the common GC
        // can reclaim them. Only a missing index requires capacity to open.
        self.ensure_open()?;
        if let Err(error) =
            crate::search_volume::require_capacity(self.vfs.as_ref(), &self.directory)
        {
            if error.kind() == io::ErrorKind::StorageFull {
                if let Some(gc) = &self.gc {
                    gc.request_expedited_epoch();
                }
            }
            return Err(error);
        }
        self.opened
            .as_mut()
            .ok_or_else(|| io::Error::other("search index was not opened"))
    }
    pub(crate) fn require_source_admission(&mut self, source: uuid::Uuid) {
        self.source = Some(source);
        if let Some(opened) = self.opened.as_mut() {
            opened.require_source_admission(source);
        }
    }
    pub(crate) fn owner_admitted(&self, tenant: u64, owner: u64) -> bool {
        self.opened
            .as_ref()
            .is_some_and(|opened| opened.owner_admitted(tenant, owner))
    }
    pub(crate) fn checkpoint_wal_window_for_shutdown(&mut self) -> io::Result<u64> {
        match self.opened.as_mut() {
            Some(opened) => opened.checkpoint_wal_window_for_shutdown(),
            None => Ok(0),
        }
    }

    pub(crate) fn owner_indexing(&self, tenant: u64, owner: u64) -> io::Result<bool> {
        self.opened()?.owner_indexing(tenant, owner)
    }
    pub(crate) fn set_owner_indexing(
        &mut self,
        tenant: u64,
        owner: u64,
        indexing: bool,
    ) -> io::Result<()> {
        self.writable()?.set_owner_indexing(tenant, owner, indexing)
    }
    pub(crate) fn is_published(
        &self,
        tenant: u64,
        owner: u64,
        conversation: &str,
        generation: &str,
    ) -> io::Result<bool> {
        self.opened()?
            .is_published(tenant, owner, conversation, generation)
    }
    pub(crate) fn source_incomplete(&self, tenant: u64, owner: u64) -> io::Result<bool> {
        self.opened()?.source_incomplete(tenant, owner)
    }
    pub(crate) fn mark_rebuild_incomplete(
        &mut self,
        tenant: u64,
        owner: u64,
        conversation: &str,
        generation: &str,
    ) -> io::Result<()> {
        self.writable()?
            .mark_rebuild_incomplete(tenant, owner, conversation, generation)
    }
    pub(crate) fn prepare_owner(
        &mut self,
        tenant: u64,
        owner: u64,
        source: uuid::Uuid,
        authority_activation: Option<uuid::Uuid>,
    ) -> io::Result<(uuid::Uuid, bool)> {
        self.writable()?
            .prepare_owner(tenant, owner, source, authority_activation)
    }
    #[cfg(test)]
    pub(crate) fn gram_index_state_for_test(
        &self,
        tenant: u64,
        owner: u64,
    ) -> io::Result<Option<u8>> {
        self.opened()?.gram_index_state_for_test(tenant, owner)
    }
    pub(crate) fn mark_gram_index_ready(&mut self, tenant: u64, owner: u64) -> io::Result<()> {
        self.writable()?.mark_gram_index_ready(tenant, owner)
    }
    pub(crate) fn acknowledge_owner_backfill(
        &mut self,
        tenant: u64,
        owner: u64,
        source: uuid::Uuid,
        activation: uuid::Uuid,
    ) -> io::Result<()> {
        self.writable()?
            .acknowledge_owner_backfill(tenant, owner, source, activation)
    }
    pub(crate) fn set_conversation_title(
        &mut self,
        tenant: u64,
        owner: u64,
        conversation: &str,
        generation: &str,
        title: &str,
    ) -> io::Result<()> {
        self.writable()?
            .set_conversation_title(tenant, owner, conversation, generation, title)
    }
    pub fn begin_conversation_rebuild(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
        updated_at_ms: u64,
    ) -> io::Result<()> {
        self.writable()?.begin_conversation_rebuild(
            tenant_id,
            owner_user_id,
            conversation_id,
            generation,
            updated_at_ms,
        )
    }
    pub fn append_conversation_page(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
        documents: &[TurnSearchDocument],
    ) -> io::Result<()> {
        self.writable()?.append_conversation_page(
            tenant_id,
            owner_user_id,
            conversation_id,
            generation,
            documents,
        )
    }
    pub fn finalize_conversation_rebuild(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
    ) -> io::Result<()> {
        self.writable()?.finalize_conversation_rebuild(
            tenant_id,
            owner_user_id,
            conversation_id,
            generation,
        )
    }
    pub fn remove_conversation(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
    ) -> io::Result<bool> {
        self.writable()?
            .remove_conversation(tenant_id, owner_user_id, conversation_id)
    }
    pub fn search(
        &self,
        request: &ConversationSearchRequest,
    ) -> io::Result<Vec<ConversationSearchHit>> {
        self.readable(request)?.search(request)
    }
    pub fn search_until(
        &self,
        request: &ConversationSearchRequest,
        deadline_elapsed: impl FnMut() -> bool,
    ) -> io::Result<Vec<ConversationSearchHit>> {
        self.readable(request)?
            .search_until(request, deadline_elapsed)
    }
    pub fn search_page_until(
        &self,
        request: &ConversationSearchRequest,
        cursor: Option<&str>,
        deadline_elapsed: impl FnMut() -> bool,
    ) -> io::Result<ConversationSearchPage> {
        self.readable(request)?
            .search_page_until(request, cursor, deadline_elapsed)
    }
}

impl OnlineGcDatabase for TurnSearchProjection {
    fn gc_budget(
        &self,
        mut budget: crate::authority_gc::AuthorityGarbageCollectionBudget,
    ) -> io::Result<crate::authority_gc::AuthorityGarbageCollectionBudget> {
        if let Some(available) = self
            .vfs
            .filesystem_capacity(&self.directory)?
            .available_bytes
        {
            budget.maximum_temporary_bytes = budget.maximum_temporary_bytes.min(available / 100);
            if budget.maximum_temporary_bytes == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::StorageFull,
                    "search volume cannot stage GC scratch",
                ));
            }
        }
        Ok(budget)
    }

    fn gc_database(&self) -> io::Result<&AuthorityDatabase> {
        Ok(&self.opened()?.database)
    }
    fn gc_database_mut(&mut self) -> io::Result<&mut AuthorityDatabase> {
        self.opened
            .as_mut()
            .map(|opened| &mut opened.database)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::WouldBlock, "search index open is deferred")
            })
    }
    fn install_gc_handle(&mut self, handle: OnlineGarbageCollectionHandle) -> io::Result<()> {
        if let Some(opened) = self.opened.as_mut() {
            opened
                .database
                .set_online_garbage_collection(handle.clone());
        }
        self.gc = Some(handle);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::authority_gc::AuthorityGarbageCollectionBudget;
    use crate::authority_gc_online::{OnlineGarbageCollectionOutcome, OnlineGarbageCollector};
    use crate::entity::EntityKey;
    use crate::vfs::{DeterministicVfs, FilesystemCapacity};
    use std::sync::{atomic::AtomicBool, Mutex};

    fn healthy() -> FilesystemCapacity {
        FilesystemCapacity {
            total_bytes: Some(1 << 40),
            available_bytes: Some(1 << 39),
            total_inodes: Some(16_777_216),
            available_inodes: Some(8_000_000),
        }
    }

    #[test]
    fn blocked_background_open_releases_query_mutex_and_observes_stop() {
        let vfs = Arc::new(crate::test_stop_vfs::StopVfs::new());
        vfs.create_dir(Path::new("/index")).unwrap();
        drop(TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs.clone(), 1 << 24).unwrap());
        let mut index = TurnSearchProjection::open_or_defer_with_vfs(Path::new("/index"), vfs.clone(),
            1 << 24, uuid::Uuid::new_v4()).unwrap();
        index.install_stop(Arc::clone(&vfs.stop));
        let shared = Arc::new(Mutex::new(index));
        let (entered, release) = vfs.block_next_open();
        let opening = Arc::clone(&shared);
        let worker = std::thread::spawn(move || TurnSearchProjection::open_in_background(&opening));
        entered.recv_timeout(std::time::Duration::from_secs(5)).unwrap();
        {
            let mut guard = shared.try_lock().expect("cold I/O held the search request mutex");
            assert!(!guard.available());
            assert_eq!(guard.search(&request()).unwrap_err().kind(), io::ErrorKind::NotConnected);
            assert_eq!(guard.checkpoint_wal_window_for_shutdown().unwrap(), 0);
        }
        vfs.stop.store(true, std::sync::atomic::Ordering::Release);
        release.send(()).unwrap();
        assert_eq!(worker.join().unwrap().unwrap_err().kind(), io::ErrorKind::Interrupted);
        assert!(!shared.lock().unwrap().opening);
        vfs.stop.store(false, std::sync::atomic::Ordering::Release);
        TurnSearchProjection::open_in_background(&shared).unwrap();
        assert!(shared.lock().unwrap().available());
    }

    #[test]
    fn deferred_handle_does_no_io_and_stop_can_interrupt_its_open() {
        use crate::test_stop_vfs::StopVfs;
        let vfs = Arc::new(StopVfs::new());
        vfs.create_dir(Path::new("/index")).unwrap();
        let mut original = TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs.clone(), 1 << 24).unwrap();
        publish(&mut original, "durable");
        drop(original);
        vfs.arm(2);
        let before = vfs.inner.trace().unwrap().len();
        let mut index = TurnSearchProjection::open_or_defer_with_vfs(Path::new("/index"), vfs.clone(),
            1 << 24, uuid::Uuid::new_v4()).unwrap();
        assert_eq!(vfs.inner.trace().unwrap().len(), before);
        assert!(!index.available());
        assert!(matches!(crate::semantic_executor::map_storage_error(index.search(&request()).unwrap_err()),
            crate::semantic_executor::SemanticExecutionError::StorageUnavailable));
        index.install_stop(Arc::clone(&vfs.stop));
        assert_eq!(index.ensure_open().unwrap_err().kind(), io::ErrorKind::Interrupted);
        assert!(!index.available());
        assert_eq!(index.checkpoint_wal_window_for_shutdown().unwrap(), 0);
        drop(index);
        vfs.arm(usize::MAX);
        let reopened = TurnSearchProjection::open_with_vfs(Path::new("/index"), vfs, 1 << 24).unwrap();
        assert_eq!(reopened.search(&request()).unwrap().len(), 1);
    }
    fn starved() -> FilesystemCapacity {
        FilesystemCapacity {
            available_inodes: Some(100),
            ..healthy()
        }
    }
    fn document(text: &str) -> TurnSearchDocument {
        TurnSearchDocument {
            turn_id: "turn".into(),
            ordinal: 1,
            search_text: text.into(),
            chunk_index: 0,
            role: "assistant".into(),
            match_field: "content".into(),
        }
    }
    fn request() -> ConversationSearchRequest {
        ConversationSearchRequest {
            tenant_id: 7,
            owner_user_id: 11,
            query: "needle".into(),
            limit: 20,
            snippet_radius: 40,
        }
    }
    fn publish(index: &mut TurnSearchProjection, generation: &str) {
        index
            .begin_conversation_rebuild(7, 11, "conversation", generation, 1)
            .unwrap();
        index
            .append_conversation_page(
                7,
                11,
                "conversation",
                generation,
                &[document(&format!("needle {generation}"))],
            )
            .unwrap();
        index
            .finalize_conversation_rebuild(7, 11, "conversation", generation)
            .unwrap();
    }
    fn loose_files(vfs: &dyn Vfs) -> usize {
        vfs.read_directory(Path::new("/index/blocks"))
            .unwrap()
            .iter()
            .map(|shard| vfs.read_directory(shard).unwrap().len())
            .sum()
    }

    #[test]
    fn capacity_drop_pauses_writes_without_touching_files_or_losing_readable_index() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.set_filesystem_capacity(healthy()).unwrap();
        vfs.create_dir(Path::new("/index")).unwrap();
        let mut index =
            TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs.clone(), 1 << 24)
                .unwrap();
        publish(&mut index, "old");
        vfs.set_filesystem_capacity(starved()).unwrap();
        let before = vfs.trace().unwrap().len();
        assert_eq!(
            index
                .begin_conversation_rebuild(7, 11, "conversation", "new", 2)
                .unwrap_err()
                .kind(),
            io::ErrorKind::StorageFull
        );
        assert_eq!(vfs.trace().unwrap().len(), before);
        assert_eq!(index.search(&request()).unwrap().len(), 1);
        vfs.set_filesystem_capacity(healthy()).unwrap();
        publish(&mut index, "new");
        assert_eq!(index.search(&request()).unwrap().len(), 1);
    }

    #[test]
    fn full_missing_index_defers_then_reopens_with_source_binding_and_gc_handle() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.set_filesystem_capacity(starved()).unwrap();
        let source = uuid::Uuid::new_v4();
        let mut index = TurnSearchProjection::open_or_defer_with_vfs(
            Path::new("/index"),
            vfs.clone(),
            1 << 24,
            source,
        )
        .unwrap();
        assert!(vfs
            .trace()
            .unwrap()
            .iter()
            .all(|operation| *operation == crate::vfs::Operation::Metadata));
        assert_eq!(
            index.search(&request()).unwrap_err().kind(),
            io::ErrorKind::NotConnected
        );
        assert_eq!(index.checkpoint_wal_window_for_shutdown().unwrap(), 0);
        let collector =
            OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget::conservative());
        index.install_gc_handle(collector.handle()).unwrap();
        vfs.set_filesystem_capacity(healthy()).unwrap();
        let (activation, pending) = index.prepare_owner(7, 11, source, None).unwrap();
        assert!(pending);
        index
            .acknowledge_owner_backfill(7, 11, source, activation)
            .unwrap();
        index.set_owner_indexing(7, 11, true).unwrap();
        assert!(index.owner_admitted(7, 11));
        assert!(index
            .gc_database()
            .unwrap()
            .online_garbage_collection()
            .is_some());
        publish(&mut index, "resumed");
        assert_eq!(index.search(&request()).unwrap().len(), 1);
    }

    #[test]
    fn search_low_capacity_does_not_disable_independent_source_writes() {
        let source_vfs = Arc::new(DeterministicVfs::new(None));
        source_vfs.create_dir(Path::new("/source")).unwrap();
        let mut source =
            AuthorityDatabase::initialize_with_vfs(Path::new("/source"), source_vfs).unwrap();
        let index_vfs = Arc::new(DeterministicVfs::new(None));
        index_vfs.set_filesystem_capacity(starved()).unwrap();
        let _index = TurnSearchProjection::open_or_defer_with_vfs(
            Path::new("/index"),
            index_vfs,
            1 << 24,
            source.authority_uuid(),
        )
        .unwrap();
        let mut tx = source.begin(7, 11).unwrap();
        let key = EntityKey::new(7, 11, "record", b"available").unwrap();
        source
            .entity_put(&mut tx, key.clone(), b"durable source".to_vec())
            .unwrap();
        source.commit(tx).unwrap();
        let mut tx = source.begin(7, 11).unwrap();
        assert_eq!(
            source.entity_get(&mut tx, &key).unwrap(),
            Some(b"durable source".to_vec())
        );
    }

    #[test]
    fn existing_index_opens_under_capacity_pressure_and_remains_collectable() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/index")).unwrap();
        let mut index =
            TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs.clone(), 1 << 24)
                .unwrap();
        publish(&mut index, "retained");
        drop(index);
        vfs.set_filesystem_capacity(starved()).unwrap();
        let mut index = TurnSearchProjection::open_or_defer_with_vfs(
            Path::new("/index"),
            vfs,
            1 << 24,
            uuid::Uuid::new_v4(),
        )
        .unwrap();
        assert!(!index.available());
        index.ensure_open().unwrap();
        assert!(index.gc_database().is_ok());
        assert_eq!(
            index.search(&request()).unwrap_err().kind(),
            io::ErrorKind::StorageFull
        );
    }

    #[test]
    fn generation_replacement_and_abandoned_publications_reclaim_blocks_and_preserve_live_hits() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/index")).unwrap();
        let mut index =
            TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs.clone(), 1 << 26)
                .unwrap();
        for generation in 0..24 {
            publish(&mut index, &format!("generation-{generation}"));
            // A failed or older-format publication can leave a valid loose block
            // outside every committed root. The shared collector must retire it.
            index
                .gc_database_mut()
                .unwrap()
                .write_orphan_block_for_test(
                    format!("abandoned projection generation {generation}").as_bytes(),
                )
                .unwrap();
        }
        // Reopen materializes carried WAL blocks, including superseded CoW pages.
        // This is the physical-growth path that a serving projection must reclaim.
        drop(index);
        let mut index =
            TurnSearchProjection::open_with_vfs(Path::new("/index"), vfs.clone(), 1 << 26).unwrap();
        index.checkpoint_wal_window_for_shutdown().unwrap();
        let before = loose_files(vfs.as_ref());
        let index = Arc::new(Mutex::new(index));
        let mut collector =
            OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget::conservative());
        let stop = AtomicBool::new(false);
        for _ in 0..3 {
            assert_eq!(
                collector.run_epoch(&index, &stop).unwrap(),
                OnlineGarbageCollectionOutcome::EpochComplete
            );
        }
        let after = loose_files(vfs.as_ref());
        let metrics = collector.handle().metrics();
        eprintln!("search_projection_churn generations=24 loose_before={before} loose_after={after} reclaimed_blocks={} reclaimed_bytes={} runtime_bytes={} collector_bytes={}", metrics.total_reclaimed_blocks, metrics.total_reclaimed_bytes, std::mem::size_of::<TurnSearchProjection>(), std::mem::size_of::<OnlineGarbageCollector>());
        assert!(
            after < before,
            "dead index blocks must be physically reclaimed"
        );
        assert!(metrics.total_reclaimed_blocks >= 24);
        assert_eq!(index.lock().unwrap().search(&request()).unwrap().len(), 1);
        drop(index);
        let reopened =
            TurnSearchProjection::open_with_vfs(Path::new("/index"), vfs, 1 << 26).unwrap();
        assert_eq!(reopened.search(&request()).unwrap().len(), 1);
    }
    #[test]
    fn mounted_projection_collector_reclaims_and_joins_without_an_operator() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/index")).unwrap();
        let mut index =
            TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs, 1 << 24).unwrap();
        publish(&mut index, "live");
        index
            .gc_database_mut()
            .unwrap()
            .write_orphan_block_for_test(b"abandoned index page")
            .unwrap();
        let index = Arc::new(Mutex::new(index));
        let collector = OnlineGarbageCollector::with_min_epoch_interval(
            AuthorityGarbageCollectionBudget::conservative(),
            std::time::Duration::ZERO,
        );
        let driver =
            crate::authority_gc_online::OnlineGarbageCollectionDriver::start_with_collector(
                &index, collector, false,
            )
            .unwrap();
        let observer = driver.observer();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while observer.metrics().total_reclaimed_blocks == 0 && std::time::Instant::now() < deadline
        {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        driver.stop_and_join().unwrap();
        assert_eq!(observer.metrics().total_reclaimed_blocks, 1);
        assert!(observer.require_healthy().is_err());
        assert_eq!(index.lock().unwrap().search(&request()).unwrap().len(), 1);
    }
}
