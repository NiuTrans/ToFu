//! Published query facade over entity roots; keeps read-only authority persona
//! construction and live configuration in one boundary. Commands/preparation
//! retain writer certification and the original snapshot path.
use super::*;
use std::sync::RwLock;
#[derive(Clone)]
pub(super) struct ReadOptions {
    event_read_epoch: Uuid,
    worker_lease_clock: Arc<crate::worker_lease_clock::WorkerLeaseClock>,
    foreground_admissions: Arc<std::sync::atomic::AtomicU64>,
    canonical_json_cache: Arc<crate::canonical_json_bytes::cache::CanonicalProofCache>,
    decoded_chunk_cache: Arc<crate::blob::decode_cache::DecodedChunkCache>,
    logical_outbox: Option<LogicalOutboxRuntime>,
    timer_live_capacity: usize,
}
impl ReadOptions {
    fn capture(database: &AuthorityDatabase) -> Self {
        Self {
            event_read_epoch: database.event_read_epoch,
            worker_lease_clock: Arc::clone(&database.worker_lease_clock),
            foreground_admissions: Arc::clone(&database.foreground_admissions),
            canonical_json_cache: Arc::clone(&database.canonical_json_cache),
            decoded_chunk_cache: Arc::clone(&database.decoded_chunk_cache),
            logical_outbox: database.logical_outbox.clone(),
            timer_live_capacity: database.timer_live_capacity,
        }
    }
    fn build(&self, entities: EntityDatabase) -> AuthorityDatabase {
        AuthorityDatabase {
            entities,
            event_read_epoch: self.event_read_epoch,
            worker_lease_clock: Arc::clone(&self.worker_lease_clock),
            foreground_admissions: Arc::clone(&self.foreground_admissions),
            canonical_json_cache: Arc::clone(&self.canonical_json_cache),
            decoded_chunk_cache: Arc::clone(&self.decoded_chunk_cache),
            logical_outbox: self.logical_outbox.clone(),
            timer_live_capacity: self.timer_live_capacity,
            preparation: None,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
            published_query_options: None,
        }
    }
}
#[derive(Clone)]
pub(crate) struct PublishedQueryView {
    entities: Arc<crate::entity::PublishedEntityReads>,
    options: Arc<RwLock<ReadOptions>>,
}
impl PublishedQueryView {
    pub(crate) fn try_build(&self) -> Option<io::Result<AuthorityDatabase>> {
        // Configuration is copied under its read guard. A failed/contended
        // optional mirror uses the original authority path, never stale data.
        let options = self.options.try_read().ok()?;
        let result = self.entities.try_build()?;
        options
            .foreground_admissions
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Some(result.map(|entities| options.build(entities)))
    }
    pub(crate) fn observer(&self) -> PublishedQueryObserver {
        PublishedQueryObserver {
            entities: Arc::clone(&self.entities),
            options: Arc::clone(&self.options),
            metadata_bytes: self.metadata_bytes(),
        }
    }
    pub(crate) fn metadata_bytes(&self) -> usize {
        self.entities.metadata_bytes()
            + std::mem::size_of::<RwLock<ReadOptions>>()
            + 2 * std::mem::size_of::<usize>()
    }
}
impl AuthorityDatabase {
    pub(crate) fn enable_published_queries(&mut self) -> io::Result<PublishedQueryView> {
        let entities = self.entities.enable_published_queries()?;
        let options = match &self.published_query_options {
            Some(options) => Arc::clone(options),
            None => {
                let options = Arc::new(RwLock::new(ReadOptions::capture(self)));
                self.published_query_options = Some(Arc::clone(&options));
                options
            }
        };
        Ok(PublishedQueryView { entities, options })
    }
    pub(super) fn refresh_published_query_options(&self) {
        if let Some(options) = &self.published_query_options {
            *options.write().unwrap_or_else(|error| error.into_inner()) =
                ReadOptions::capture(self);
        }
    }
}

#[derive(Clone)]
pub struct PublishedQueryObserver {
    entities: Arc<crate::entity::PublishedEntityReads>,
    options: Arc<RwLock<ReadOptions>>,
    metadata_bytes: usize,
}
impl PublishedQueryObserver {
    pub fn metrics(&self) -> serde_json::Value {
        let mut value = self.entities.metrics();
        value["available"] =
            (value["available"].as_bool().unwrap_or(false) && !self.options.is_poisoned()).into();
        value["metadataBytes"] = self.metadata_bytes.into();
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn published_query_worker_clock_shares_live_epoch_and_utc_rollback() {
        let directory = tempfile::tempdir().unwrap();
        let mut writer = AuthorityDatabase::initialize(directory.path()).unwrap();
        writer.set_worker_lease_test_time(100_000);
        let view = writer.enable_published_queries().unwrap();
        let replica = view.try_build().unwrap().unwrap();
        assert_eq!(replica.worker_lease_epoch(), writer.worker_lease_epoch());
        writer.set_worker_lease_test_time(180_000);
        writer.set_worker_queue_test_time(90_000);
        let sample = replica.worker_clock_snapshot().unwrap();
        assert_eq!(sample.lease_now_ms, 180_000);
        assert_eq!(sample.utc_now_ms, 90_000);
        writer.set_worker_lease_test_time(190_000);
        assert_eq!(replica.worker_lease_now_ms().unwrap(), 190_000);
    }
    #[test]
    fn published_query_configuration_tracks_authority_overrides_and_poison_falls_back() {
        let directory = tempfile::tempdir().unwrap();
        let mut writer = AuthorityDatabase::initialize(directory.path()).unwrap();
        let view = writer.enable_published_queries().unwrap();
        writer.configure_timer_live_capacity(8).unwrap();
        writer
            .configure_logical_outbox(&[9; 32], 65536, 4096)
            .unwrap();
        let replica = view.try_build().unwrap().unwrap();
        assert_eq!(replica.timer_live_capacity, 0);
        assert_eq!(
            replica.logical_outbox.as_ref().unwrap().max_pending_bytes,
            65536
        );
        drop(replica);
        let _ = std::panic::catch_unwind(|| {
            let _guard = view.options.write().unwrap();
            panic!("optional query configuration poison");
        });
        assert!(view.try_build().is_none());
        assert_eq!(writer.build_read_replica().unwrap().timer_live_capacity, 0);
    }

    #[test]
    fn published_query_replicas_observe_the_advancing_authority_lease_clock() {
        let directory = tempfile::tempdir().unwrap();
        let mut writer = AuthorityDatabase::initialize(directory.path()).unwrap();
        writer.set_worker_lease_test_time(100_000);
        let view = writer.enable_published_queries().unwrap();
        let first = view.try_build().unwrap().unwrap();
        assert_eq!(first.worker_lease_now_ms().unwrap(), 100_000);
        writer.set_worker_lease_test_time(110_000);
        writer.set_worker_queue_test_time(90_000);
        let second = view.try_build().unwrap().unwrap();
        for replica in [&first, &second] {
            let sample = replica.worker_clock_snapshot().unwrap();
            assert_eq!(sample.lease_now_ms, 110_000);
            assert_eq!(sample.utc_now_ms, 90_000);
            assert_eq!(replica.worker_lease_epoch(), writer.worker_lease_epoch());
        }
    }
}
