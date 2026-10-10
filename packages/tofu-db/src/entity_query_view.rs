//! Immutable query roots published by the entity writer. Readers register their
//! own MVCC pin while holding the view read lock; publication replaces the root
//! under its write lock before the writer releases authority. No permanent pin,
//! historical-root cache, payload copy, I/O, or worker belongs to this mirror.
use super::*;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{RwLock, TryLockError};

struct Template {
    core: ReplicaCore,
    wal_barrier: Option<crate::wal::WalBarrier>,
    database_instance_id: Uuid,
    pins: Arc<SnapshotPinRegistry>,
    page_cache: Arc<RwLock<DecodedPageCache>>,
    compaction_hints: Arc<Mutex<CompactionHints>>,
    tombstone_reclaim_override: Option<bool>,
    overlay: crate::entity_overlay::EntityOverlay,
    overlay_trigger_bytes: usize,
    overlay_trigger_entries: usize,
    overlay_enabled: bool,
    overlay_budget_bytes: usize,
    overlay_wal_watermark_bytes: u64,
    merge_chunk_entries_adaptive: usize,
}
impl Template {
    fn capture(database: &EntityDatabase) -> io::Result<Self> {
        let engine = database.writer_engine()?;
        engine.require_usable_authority()?;
        Ok(Self {
            core: ReplicaCore {
                blocks: ReplicaBlockIo {
                    blocks: engine.block_store_shared(),
                    restart_required: engine.restart_flag(),
                },
                captured: database.current_snapshot(),
                authority_uuid: engine.state().authority_uuid,
            },
            wal_barrier: engine.wal_barrier(),
            database_instance_id: database.database_instance_id,
            pins: Arc::clone(&database.snapshot_pins),
            page_cache: Arc::clone(&database.page_cache),
            compaction_hints: Arc::clone(&database.compaction_hints),
            tombstone_reclaim_override: database.tombstone_reclaim_override,
            overlay: database
                .overlay
                .read()
                .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                .clone(),
            overlay_trigger_bytes: database.overlay_trigger_bytes.load(Ordering::Acquire),
            overlay_trigger_entries: database.overlay_trigger_entries.load(Ordering::Acquire),
            overlay_enabled: database.overlay_enabled,
            overlay_budget_bytes: database.overlay_budget_bytes,
            overlay_wal_watermark_bytes: database.overlay_wal_watermark_bytes,
            merge_chunk_entries_adaptive: database
                .merge_chunk_entries_adaptive
                .load(Ordering::Acquire),
        })
    }
    fn check_live(&self) -> io::Result<()> {
        self.core.blocks.check_live()?;
        if let Some(barrier) = &self.wal_barrier {
            if let Some(error) = barrier.barrier_error() {
                return Err(io::Error::new(error.kind(), format!("tofu-db WAL background barrier failed; close and reopen the engine: {error}")));
            }
        }
        Ok(())
    }
    fn build(&self) -> io::Result<EntityDatabase> {
        self.check_live()?;
        let pin = {
            let _pin = crate::fsync_stats::StageTimer::diagnostic(
                crate::fsync_stats::STAGE_PUBLISHED_READ_PIN,
            );
            pin_snapshot(
                &self.pins,
                self.core.captured,
            )?
        };
        let _clone = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_PUBLISHED_READ_CLONE,
        );
        Ok(EntityDatabase {
            persona: EntityPersona::Replica(ReplicaCore {
                blocks: ReplicaBlockIo {
                    blocks: Arc::clone(&self.core.blocks.blocks),
                    restart_required: Arc::clone(&self.core.blocks.restart_required),
                },
                captured: self.core.captured,
                authority_uuid: self.core.authority_uuid,
            }),
            root: self.core.captured.root,
            database_instance_id: self.database_instance_id,
            snapshot_pins: Arc::clone(&self.pins),
            page_cache: Arc::clone(&self.page_cache),
            compaction_hints: Arc::clone(&self.compaction_hints),
            tombstone_reclaim_override: self.tombstone_reclaim_override,
            replica_pin: Some(Arc::new(pin)),
            query_publisher: None,
            overlay: Arc::new(RwLock::new(self.overlay.clone())),
            overlay_trigger_bytes: std::sync::atomic::AtomicUsize::new(self.overlay_trigger_bytes),
            overlay_trigger_entries: std::sync::atomic::AtomicUsize::new(
                self.overlay_trigger_entries,
            ),
            overlay_enabled: self.overlay_enabled,
            overlay_budget_bytes: self.overlay_budget_bytes,
            overlay_wal_watermark_bytes: self.overlay_wal_watermark_bytes,
            merge_chunk_entries_adaptive: std::sync::atomic::AtomicUsize::new(
                self.merge_chunk_entries_adaptive,
            ),
        })
    }
}

pub(crate) struct PublishedEntityReads {
    state: RwLock<Option<Template>>,
    disabled: AtomicBool,
    publications: AtomicU64,
    successful_reads: AtomicU64,
    #[cfg(test)]
    before_replace: Mutex<Option<std::sync::mpsc::Sender<()>>>,
}
impl PublishedEntityReads {
    fn create(database: &EntityDatabase) -> io::Result<Arc<Self>> {
        Ok(Arc::new(Self {
            state: RwLock::new(Some(Template::capture(database)?)),
            disabled: AtomicBool::new(false),
            publications: AtomicU64::new(1),
            successful_reads: AtomicU64::new(0),
            #[cfg(test)]
            before_replace: Mutex::new(None),
        }))
    }
    // None preserves original authority acquisition on optional mirror failure
    // or its tiny publish window. Pin/source failures remain actual errors.
    pub(crate) fn try_build(&self) -> Option<io::Result<EntityDatabase>> {
        self.try_build_after(|| {})
    }
    fn try_build_after(&self, before_pin: impl FnOnce()) -> Option<io::Result<EntityDatabase>> {
        if self.disabled.load(Ordering::Acquire) {
            return None;
        }
        let lookup = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_PUBLISHED_READ_LOOKUP,
        );
        let state = match self.state.try_read() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock | TryLockError::Poisoned(_)) => return None,
        };
        let template = state.as_ref()?;
        drop(lookup);
        // Keep this guard through pin registration and descriptor copying.
        // The writer cannot discard the old published root before registration.
        before_pin();
        let result = template.build();
        if result.is_ok() {
            self.successful_reads.fetch_add(1, Ordering::Relaxed);
            crate::request_profile::work(crate::request_profile::Work::PublishedReadReuse, 1);
        }
        Some(result)
    }
    fn disable(&self) {
        self.disabled.store(true, Ordering::Release);
        *self
            .state
            .write()
            .unwrap_or_else(|error| error.into_inner()) = None;
    }
    fn publish(&self, database: &EntityDatabase) {
        if self.disabled.load(Ordering::Acquire) {
            return;
        }
        let _publish = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_PUBLISHED_READ_PUBLISH,
        );
        // Prepare outside the view lock; writer authority excludes GC final
        // reclamation until replacement and all preceding reader pins finish.
        let template = match Template::capture(database) {
            Ok(template) => template,
            Err(_) => {
                self.disable();
                return;
            }
        };
        #[cfg(test)]
        if let Some(notify) = self.before_replace.lock().unwrap().as_ref() {
            let _ = notify.send(());
        }
        match self.state.write() {
            Ok(mut state) => {
                let old = state.replace(template);
                drop(state);
                drop(old);
                self.publications.fetch_add(1, Ordering::Relaxed);
                crate::request_profile::work(
                    crate::request_profile::Work::PublishedReadPublication,
                    1,
                );
            }
            Err(error) => {
                self.disabled.store(true, Ordering::Release);
                *error.into_inner() = None;
            }
        }
    }
    pub(crate) fn metrics(&self) -> serde_json::Value {
        let available = !self.disabled.load(Ordering::Acquire) && !self.state.is_poisoned();
        serde_json::json!({ "available": available, "retainedViews": if available { 1 } else { 0 },
            "publications": self.publications.load(Ordering::Relaxed),
            "successfulReads": self.successful_reads.load(Ordering::Relaxed), "ownedPinHandles": 0 })
    }
    pub(crate) fn metadata_bytes(&self) -> usize {
        std::mem::size_of::<Self>() + 2 * std::mem::size_of::<usize>()
    }
}

pub(super) struct PublicationUpdate {
    publisher: Option<Arc<PublishedEntityReads>>,
    finished: bool,
}
impl PublicationUpdate {
    pub(super) fn finish(mut self, database: &EntityDatabase) {
        if let Some(publisher) = &self.publisher {
            publisher.publish(database);
        }
        self.finished = true;
    }
}
impl Drop for PublicationUpdate {
    fn drop(&mut self) {
        // A failed partial logical-root/overlay apply must not leave a stale
        // mirror eligible. The original writer/error path remains authoritative.
        if !self.finished {
            if let Some(publisher) = &self.publisher {
                publisher.disable();
            }
        }
    }
}
impl EntityDatabase {
    pub(crate) fn enable_published_queries(&mut self) -> io::Result<Arc<PublishedEntityReads>> {
        self.writer_engine()?.require_usable_authority()?;
        if let Some(publisher) = &self.query_publisher {
            return Ok(Arc::clone(publisher));
        }
        let publisher = PublishedEntityReads::create(self)?;
        self.query_publisher = Some(Arc::clone(&publisher));
        Ok(publisher)
    }
    pub(super) fn begin_query_publication(&self) -> PublicationUpdate {
        PublicationUpdate {
            publisher: self.query_publisher.clone(),
            finished: false,
        }
    }
}

#[cfg(test)]
impl PublishedEntityReads {
    pub(super) fn build_with_pin_hook(
        &self,
        hook: impl FnOnce(),
    ) -> Option<io::Result<EntityDatabase>> {
        self.try_build_after(hook)
    }
    pub(super) fn set_replace_notification(&self, notify: std::sync::mpsc::Sender<()>) {
        *self.before_replace.lock().unwrap() = Some(notify);
    }
    pub(super) fn poison_for_test(&self) {
        let _ = std::panic::catch_unwind(|| {
            let _guard = self.state.write().unwrap();
            panic!("optional published view poison");
        });
    }
}
