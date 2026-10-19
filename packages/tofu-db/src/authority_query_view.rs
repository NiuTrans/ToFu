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
            published_query_results: None,
        }
    }
}
#[derive(Clone)]
pub(crate) struct PublishedQueryView {
    entities: Arc<crate::entity::PublishedEntityReads>,
    options: Arc<RwLock<ReadOptions>>,
    results: Arc<crate::query_result_cache::QueryResultCache>,
}
pub(crate) struct CachedQueryResult {
    response: Arc<crate::transaction_ir::IrResponse>,
    foreground_admissions: Arc<std::sync::atomic::AtomicU64>,
}
impl CachedQueryResult {
    pub(crate) fn into_response(self) -> crate::transaction_ir::IrResponse {
        self.foreground_admissions.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        (*self.response).clone()
    }
}
impl PublishedQueryView {
    pub(crate) fn try_cached(&self, key: crate::query_result_cache::Lookup<'_>) -> Option<CachedQueryResult> {
        // Empty, uncacheable and evicted keys avoid health/publication locks.
        // The owned candidate is never an answer until live generation proof.
        let (stored, response) = self.results.candidate(key)?;
        let options = self.options.try_read().ok()?;
        if !self.entities.with_healthy_current_snapshot(|snapshot| snapshot == stored)? { return None; }
        Some(CachedQueryResult { response, foreground_admissions: Arc::clone(&options.foreground_admissions) })
    }
    pub(crate) fn remember(&self, request: &crate::protocol::Request,
        snapshot: crate::entity::EntitySnapshot, response: &crate::server::response::DispatchedResponse) {
        if request.command_id.is_some() { return; }
        let Some(admission) = response.cache_admission() else { return; };
        self.entities.with_healthy_current_snapshot(|current| {
            if current == snapshot { self.results.insert(request.into(), snapshot, response.payload.len(), admission, || response.snapshot_cache_copy()); }
        });
    }
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
            results: Arc::clone(&self.results),
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
        let results = self.published_query_results.get_or_insert_with(||
            Arc::new(crate::query_result_cache::QueryResultCache::new(self.canonical_json_cache.observer().metrics().capacity_bytes))).clone();
        Ok(PublishedQueryView { entities, options, results })
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
    results: Arc<crate::query_result_cache::QueryResultCache>,
    metadata_bytes: usize,
}
impl PublishedQueryObserver {
    pub fn metrics(&self) -> serde_json::Value {
        let mut value = self.entities.metrics();
        value["available"] =
            (value["available"].as_bool().unwrap_or(false) && !self.options.is_poisoned()).into();
        value["metadataBytes"] = self.metadata_bytes.into();
        value["queryResults"] = self.results.metrics();
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
            replica.logical_outbox.as_ref().unwrap().cipher.key_id(),
            writer.logical_outbox.as_ref().unwrap().cipher.key_id()
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

#[cfg(test)]
#[path = "authority_query_cache_tests.rs"]
mod authority_query_cache_tests;
