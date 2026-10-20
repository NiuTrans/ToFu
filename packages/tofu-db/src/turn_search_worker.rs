//! Bounded authority-to-search projection maintenance round.
//! Source pages are read from a batch-scoped pinned read replica, never
//! under the foreground authority mutex. Activation/backfill commits and the
//! exact-token acknowledge use the writer (2026-09-22 FUSE: one cold source page had
//! serialized swarm writers for ~0.5 s).
//!
//! The worker holds an immutable MVCC source transaction across pages on the
//! replica and never re-acquires the foreground authority for reads. A
//! changed dirty epoch survives exact-token ACK and schedules another complete
//! generation, while an interrupted build remains invisible in the target.

use std::io;
use std::sync::{Arc, Mutex, TryLockError};

use blake3::Hasher;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::search_dirty::{self, DirtyConversation};
use crate::turn;
use crate::turn_search_projection::TurnSearchProjection;

const MAX_SOURCE_PAGES: usize = crate::generated_tofudb_ir::SEARCH_PROJECTION_MAXIMUM_SOURCE_PAGES;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct TurnSearchWorkerMetrics {
    pub dirty_conversations: u64,
    pub rebuilt_conversations: u64,
    pub removed_conversations: u64,
    pub acknowledged_tokens: u64,
    pub source_pages: u64,
    pub indexed_turns: u64,
    pub skipped_oversized_turns: u64,
    pub source_bytes: u64,
    pub foreground_deferrals: u64,
    pub page_budget_exhaustions: u64,
}

fn poisoned(name: &str) -> io::Error {
    io::Error::other(format!("{name} mutex is poisoned"))
}

fn generation(dirty: &DirtyConversation, source: uuid::Uuid) -> String {
    let mut tokens = dirty.tokens.clone();
    tokens.sort_by(|left, right| left.key.cmp(&right.key));
    let mut hasher = Hasher::new();
    hasher.update(b"tofu-db:turn-search:worker-generation:v2\0");
    hasher.update(source.as_bytes());
    hasher.update(&(dirty.conversation_id.len() as u64).to_be_bytes());
    hasher.update(dirty.conversation_id.as_bytes());
    for token in tokens {
        hasher.update(&(token.key.encoded().len() as u64).to_be_bytes());
        hasher.update(token.key.encoded());
        hasher.update(&(token.value.len() as u64).to_be_bytes());
        hasher.update(&token.value);
    }
    hasher.finalize().to_hex().to_string()
}

fn try_authority<'a>(
    authority: &'a Arc<Mutex<AuthorityDatabase>>,
    metrics: &mut TurnSearchWorkerMetrics,
) -> io::Result<Option<std::sync::MutexGuard<'a, AuthorityDatabase>>> {
    match authority.try_lock() {
        Ok(database) => Ok(Some(database)),
        Err(TryLockError::WouldBlock) => {
            metrics.foreground_deferrals = metrics.foreground_deferrals.saturating_add(1);
            Ok(None)
        }
        Err(TryLockError::Poisoned(_)) => Err(poisoned("turn-search authority")),
    }
}

fn projection_lock(
    projection: &Arc<Mutex<TurnSearchProjection>>,
) -> io::Result<std::sync::MutexGuard<'_, TurnSearchProjection>> {
    projection
        .lock()
        .map_err(|_| poisoned("turn-search projection"))
}

fn acknowledge(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    scope: crate::maintenance::MaintenanceScope,
    dirty: &DirtyConversation,
    metrics: &mut TurnSearchWorkerMetrics,
) -> io::Result<bool> {
    let Some(mut database) = try_authority(authority, metrics)? else {
        return Ok(false);
    };
    let _hold = crate::authority_watch::AuthorityHoldTimer::probe("search.acknowledge");
    let acknowledged =
        database.acknowledge_search_dirty(scope.tenant_id, scope.owner_user_id, &dirty.tokens)?;
    metrics.acknowledged_tokens = metrics
        .acknowledged_tokens
        .saturating_add(acknowledged as u64);
    Ok(true)
}

fn process_conversation(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    dirty: &DirtyConversation,
    mut source: AuthorityTransaction,
    metrics: &mut TurnSearchWorkerMetrics,
    replica: &AuthorityDatabase,
    should_stop: &impl Fn() -> bool,
) -> io::Result<bool> {
    if should_stop() { return Ok(false); }
    // Source reads run on the pinned read replica: they never hold the
    // foreground authority mutex, so a slow-mount page read can never
    // serialize a foreground writer behind it (2026-09-22 FUSE bench:
    // one cold 8-row source page ≈ the observed ~0.5 s swarm write p99).
    // Anything the replica snapshot misses stays dirty through the
    // exact-token acknowledge below and is rebuilt by a later round.
    let updated_at_ms = crate::conversation_header::search_projection_header(
        replica,
        &mut source,
        &dirty.conversation_id,
    )?;
    let generation = generation(dirty, replica.authority_uuid());
    let Some((updated_at_ms, title)) = updated_at_ms else {
        projection_lock(projection)?.remove_conversation(
            scope.tenant_id,
            scope.owner_user_id,
            &dirty.conversation_id,
        )?;
        metrics.removed_conversations = metrics.removed_conversations.saturating_add(1);
        return acknowledge(authority, scope, dirty, metrics);
    };

    if projection_lock(projection)?.is_published(
        scope.tenant_id,
        scope.owner_user_id,
        &dirty.conversation_id,
        &generation,
    )? {
        return acknowledge(authority, scope, dirty, metrics);
    }
    projection_lock(projection)?.begin_conversation_rebuild(
        scope.tenant_id,
        scope.owner_user_id,
        &dirty.conversation_id,
        &generation,
        updated_at_ms,
    )?;
    projection_lock(projection)?.set_conversation_title(
        scope.tenant_id,
        scope.owner_user_id,
        &dirty.conversation_id,
        &generation,
        &title,
    )?;
    let mut cursor = Vec::new();
    let mut archives = false;
    let mut source_complete = false;
    for _ in 0..MAX_SOURCE_PAGES {
        // Only stop between complete storage operations. The unpublished
        // generation and its exact dirty tokens remain retryable; never ACK
        // or publish a truncated source as complete during shutdown.
        if should_stop() { return Ok(false); }
        let page = if archives {
            crate::compaction_archive::search_source::page(
                replica,
                &mut source,
                &dirty.conversation_id,
                &cursor,
            )?
        } else {
            turn::search_projection_page(replica, &mut source, &dirty.conversation_id, &cursor)?
        };
        if !page.complete && page.next_cursor == cursor {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "turn-search source cursor stalled",
            ));
        }
        if page.skipped_oversized > 0 {
            projection_lock(projection)?.mark_rebuild_incomplete(
                scope.tenant_id,
                scope.owner_user_id,
                &dirty.conversation_id,
                &generation,
            )?;
        }
        let documents = page.turns;
        for batch in documents.chunks(crate::generated_conversation_search::SOURCE_PAGE_ROWS) {
            if should_stop() { return Ok(false); }
            projection_lock(projection)?.append_conversation_page(
                scope.tenant_id,
                scope.owner_user_id,
                &dirty.conversation_id,
                &generation,
                batch,
            )?;
        }
        metrics.source_pages = metrics.source_pages.saturating_add(1);
        metrics.indexed_turns = metrics
            .indexed_turns
            .saturating_add(page.source_turns as u64);
        metrics.skipped_oversized_turns = metrics
            .skipped_oversized_turns
            .saturating_add(page.skipped_oversized as u64);
        metrics.source_bytes = metrics.source_bytes.saturating_add(page.source_bytes);
        cursor = page.next_cursor;
        if page.complete && !archives {
            archives = true;
            cursor.clear();
            continue;
        }
        if page.complete {
            source_complete = true;
            break;
        }
    }
    if should_stop() { return Ok(false); }
    if !source_complete {
        // The page budget spans both sources, including the final archive
        // probe. Publish explicitly partial coverage rather than retrying an
        // unchanged oversized conversation forever and starving later work.
        projection_lock(projection)?.mark_rebuild_incomplete(
            scope.tenant_id,
            scope.owner_user_id,
            &dirty.conversation_id,
            &generation,
        )?;
        metrics.page_budget_exhaustions = metrics.page_budget_exhaustions.saturating_add(1);
    }
    projection_lock(projection)?.finalize_conversation_rebuild(
        scope.tenant_id,
        scope.owner_user_id,
        &dirty.conversation_id,
        &generation,
    )?;
    metrics.rebuilt_conversations = metrics.rebuilt_conversations.saturating_add(1);
    acknowledge(authority, scope, dirty, metrics)
}

/// Internal/operator entry point: schedule exactly this explicit owner scope.
/// This does not claim legacy global RPC semantics or projection completion.
pub fn schedule_owner_search_backfill(
    database: &mut AuthorityDatabase,
    scope: crate::maintenance::MaintenanceScope,
) -> io::Result<u64> {
    let mut transaction = database.begin(scope.tenant_id, scope.owner_user_id)?;
    let state = crate::semantic_executor::search_backfill::schedule(database, &mut transaction)?;
    database.commit(transaction)?;
    Ok(state.generation)
}

pub fn process_turn_search_batch(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
) -> io::Result<TurnSearchWorkerMetrics> {
    process_turn_search_batch_with_pressure(
        authority,
        projection,
        scope,
        &mut crate::fsync_stats::foreground_hotness_sampler(),
    )
}

pub(crate) fn process_turn_search_batch_with_pressure(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    hot_sample: &mut crate::fsync_stats::ForegroundSampler,
) -> io::Result<TurnSearchWorkerMetrics> {
    process_turn_search_batch_with_admission(authority, projection, scope, || {
        crate::fsync_stats::defer_search_projection(hot_sample)
    })
}

pub(crate) fn process_turn_search_batch_with_pressure_and_stop(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    hot_sample: &mut crate::fsync_stats::ForegroundSampler,
    should_stop: impl Fn() -> bool,
) -> io::Result<TurnSearchWorkerMetrics> {
    process_turn_search_batch_with_control(authority, projection, scope,
        || crate::fsync_stats::defer_search_projection(hot_sample),
        wait_for_authority_durability, should_stop)
}

fn wait_for_authority_durability(
    witness: Option<(crate::wal::WalBarrier, crate::wal::WalWitness)>,
) -> io::Result<()> {
    if let Some((barrier, witness)) = witness {
        barrier.wait_witness(
            &witness,
            std::time::Instant::now() + std::time::Duration::from_secs(5),
        )?;
    }
    Ok(())
}

fn process_turn_search_batch_with_admission(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    defer: impl FnMut() -> bool,
) -> io::Result<TurnSearchWorkerMetrics> {
    process_turn_search_batch_with_durability(
        authority,
        projection,
        scope,
        defer,
        wait_for_authority_durability,
    )
}

fn process_turn_search_batch_with_durability(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    defer: impl FnMut() -> bool,
    wait: impl FnOnce(Option<(crate::wal::WalBarrier, crate::wal::WalWitness)>) -> io::Result<()>,
) -> io::Result<TurnSearchWorkerMetrics> {
    process_turn_search_batch_with_control(authority, projection, scope, defer, wait, || false)
}

fn process_turn_search_batch_with_control(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
    scope: crate::maintenance::MaintenanceScope,
    mut defer: impl FnMut() -> bool,
    wait: impl FnOnce(Option<(crate::wal::WalBarrier, crate::wal::WalWitness)>) -> io::Result<()>,
    should_stop: impl Fn() -> bool,
) -> io::Result<TurnSearchWorkerMetrics> {
    let mut metrics = TurnSearchWorkerMetrics::default();
    if should_stop() { return Ok(metrics); }
    TurnSearchProjection::open_in_background(projection)?;
    let Some(database) = try_authority(authority, &mut metrics)? else {
        return Ok(metrics);
    };
    let capture_hold = crate::authority_watch::AuthorityHoldTimer::probe("search.activation-capture");
    // Fail closed before touching either store if deferred WAL durability has
    // no healthy coordinator. Capture a fresh covering witness after commits.
    database.wal_durability_witness()?;
    let source_uuid = database.authority_uuid();
    let mut activation_read = database.begin(scope.tenant_id, scope.owner_user_id)?;
    let authority_activation = crate::semantic_executor::search_backfill::load_activation(
        &database,
        &mut activation_read,
    )?;
    drop(activation_read);
    drop(capture_hold);
    drop(database);
    // The target has its own durable store. Never hold the foreground writer
    // across its read/commit I/O. Keep the existing source-key read boundary;
    // reacquisition validates its identity/version before any source mutation.
    let (activation, needs_acknowledgement) = projection_lock(projection)?.prepare_owner(
        scope.tenant_id,
        scope.owner_user_id,
        source_uuid,
        authority_activation,
    )?;
    if should_stop() { return Ok(metrics); }
    let Some(mut database) = try_authority(authority, &mut metrics)? else {
        return Ok(metrics);
    };
    let search_hold = crate::authority_watch::AuthorityHoldTimer::probe("search.prepare");
    let activation_hold = crate::authority_watch::AuthorityHoldTimer::probe("search.activation");
    database.wal_durability_witness()?;
    if database.authority_uuid() != source_uuid {
        metrics.foreground_deferrals += 1;
        return Ok(metrics);
    }
    let mut activation_read = database.begin(scope.tenant_id, scope.owner_user_id)?;
    let current_activation = crate::semantic_executor::search_backfill::load_activation(
        &database, &mut activation_read)?;
    drop(activation_read);
    if current_activation != authority_activation {
        // A pending target remains invisible and retryable. It must never
        // overwrite or acknowledge a newer source activation/authority.
        metrics.foreground_deferrals += 1;
        return Ok(metrics);
    }
    if needs_acknowledgement {
        let mut transaction = database.begin(scope.tenant_id, scope.owner_user_id)?;
        if crate::semantic_executor::search_backfill::activate(
            &database,
            &mut transaction,
            activation,
        )? {
            database.commit(transaction)?;
        }
    }
    drop(activation_hold);
    // Enqueue the next backfill page before selecting dirty work. Otherwise a
    // fresh projection first drains every existing token and then invalidates
    // the same corpus again. While backfill is pending, leave tokens beyond
    // its cursor queued so each source header is scheduled before rebuilding.
    let checkpoint = {
        let _hold = crate::authority_watch::AuthorityHoldTimer::probe("search.backfill");
        crate::semantic_executor::search_backfill::consume_page(
            &mut database, scope.tenant_id, scope.owner_user_id)?.checkpoint
    };
    let mut dirty = {
        let _hold = crate::authority_watch::AuthorityHoldTimer::probe("search.dirty-list");
        search_dirty::list(&database, scope.tenant_id, scope.owner_user_id)?
    };
    // consume_page returned the exact read/committed state. The writer hold
    // is uninterrupted and dirty-list is read-only, so another checkpoint
    // transaction adds no validation; the WAL publication fence stays below.
    let indexed = dirty.is_empty() && checkpoint.as_ref().is_some_and(|state| state.complete);
    if let Some(checkpoint) = checkpoint.filter(|state| !state.complete) {
        dirty.retain(|item| item.conversation_id.as_bytes() <= checkpoint.cursor.as_slice());
    }
    metrics.dirty_conversations = dirty.len() as u64;
    // Build one pinned read replica for the whole batch while still holding
    // the authority: the build is a microsecond Arc/pin registration with no
    // mount I/O, and every per-conversation source read then runs off-mutex
    // (docs/STORAGE_TOFUDB.md §Snapshot read lane). A fenced authority or a
    // saturated pin registry defers the batch; the dirty tokens persist and
    // the next round retries.
    let deferred = !dirty.is_empty() && defer();
    let replica = if dirty.is_empty() || deferred {
        None
    } else {
        match database.build_read_replica() {
            Ok(replica) => Some(replica),
            Err(_) => {
                drop(search_hold);
                drop(database);
                return Ok(metrics);
            }
        }
    };
    let sources = match &replica {
        Some(replica) => dirty
            .iter()
            .map(|_| replica.begin(scope.tenant_id, scope.owner_user_id))
            .collect::<io::Result<Vec<_>>>()?,
        None => Vec::new(),
    };
    // This prefix covers activation + checkpoint + page invalidations and all
    // source rows in the pinned replica. Both Strict group-commit and Normal
    // mode can return from authority.commit before this prefix is durable.
    let witness = database.wal_durability_witness()?;
    drop(search_hold);
    drop(database);
    wait(witness)?;
    // No authority/projection mutex spans the wait. On failure the pending
    // marker stays fenced, and durable checkpoint/dirty state permits retry.
    if needs_acknowledgement {
        projection_lock(projection)?.acknowledge_owner_backfill(
            scope.tenant_id,
            scope.owner_user_id,
            source_uuid,
            activation,
        )?;
    }
    projection_lock(projection)?.set_owner_indexing(
        scope.tenant_id,
        scope.owner_user_id,
        !indexed,
    )?;
    if indexed {
        projection_lock(projection)?.mark_gram_index_ready(scope.tenant_id, scope.owner_user_id)?;
    }
    // Publish truthful coverage even when pressure defers the rebuild itself.
    // Report pending debt so maintenance does not mistake deferral for idleness.
    if deferred {
        metrics.foreground_deferrals = 1;
        return Ok(metrics);
    }
    let Some(replica) = replica else {
        return Ok(metrics);
    };
    for (dirty, source) in dirty.iter().zip(sources) {
        if !process_conversation(
            authority,
            projection,
            scope,
            dirty,
            source,
            &mut metrics,
            &replica,
            &should_stop,
        )? {
            break;
        }
    }
    Ok(metrics)
}

#[cfg(test)]
#[path = "turn_search_worker_activation_tests.rs"]
mod activation_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::conversation_header::{CreateRequest, DeleteRequest, TurnDefaults};
    use crate::turn::AppendSettledRequest;
    use crate::turn_search_projection::ConversationSearchRequest;

    #[test]
    fn shutdown_stops_between_source_pages_without_ack_and_retry_finishes() {
        use std::cell::Cell;
        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(&database, &mut transaction, &CreateRequest {
            conversation_id: "conversation".into(), title: "shutdownneedle".into(),
            settings_json: b"{}".to_vec(), created_at_ms: 100,
            updated_at_ms: 100, committed_at_ms: 100,
        }).unwrap();
        database.commit(transaction).unwrap();
        append_turn(&mut database, "one", "assistant", "main", "completed",
            serde_json::json!({"content": "shutdownneedle"}), 101);
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap()));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let checkpoints = Cell::new(0);
        // First source page is complete, but the archive source still needs
        // its probe. Stop there, before publish or exact-token ACK.
        let interrupted = process_turn_search_batch_with_control(
            &authority, &projection, scope, || false, wait_for_authority_durability,
            || { let n = checkpoints.get() + 1; checkpoints.set(n); n >= 6 },
        ).unwrap();
        assert_eq!(interrupted.source_pages, 1);
        assert_eq!(interrupted.rebuilt_conversations, 0);
        assert_eq!(interrupted.acknowledged_tokens, 0);
        assert!(search(&projection, "shutdownneedle").is_empty());
        {
            let database = authority.lock().unwrap();
            assert!(!search_dirty::list(&database, 7, 11).unwrap().is_empty());
        }
        // A process replacement must also recover the unpublished staging
        // state and exact dirty tokens, not just an in-memory retry.
        drop(authority);
        drop(projection);
        let authority = Arc::new(Mutex::new(AuthorityDatabase::open(source_dir.path()).unwrap()));
        let projection = Arc::new(Mutex::new(TurnSearchProjection::open(target_dir.path(), 16 * 1024 * 1024).unwrap()));
        let finished = process_turn_search_batch_with_admission(
            &authority, &projection, scope, || false).unwrap();
        assert_eq!(finished.rebuilt_conversations, 1);
        assert!(finished.acknowledged_tokens > 0);
        assert_eq!(search(&projection, "shutdownneedle").len(), 1);
        // No new work is admitted once stop is already set.
        let stopped = process_turn_search_batch_with_control(
            &authority, &projection, scope, || false, wait_for_authority_durability,
            || true).unwrap();
        assert_eq!(stopped, TurnSearchWorkerMetrics::default());
    }

    #[test]
    fn activation_ack_waits_off_mutex_for_durable_authority_and_survives_rotation() {
        use crate::semantic_executor::search_backfill;
        use crate::vfs::{DeterministicVfs, Vfs};
        use crate::wal::{WalBarrier, WalDurability};
        use std::path::Path;

        for mode in [WalDurability::Strict, WalDurability::Normal] {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(Path::new("/source")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let mut database =
                AuthorityDatabase::initialize_with_vfs(Path::new("/source"), vfs.clone()).unwrap();
            let source = database.authority_uuid();
            database.set_wal_durability(mode).unwrap();
            // No driver: every asynchronous commit remains unsynced until the
            // explicit worker publication barrier below. No timing assumptions.
            let barrier = WalBarrier::default();
            database.attach_wal_barrier(&barrier).unwrap();
            let authority = Arc::new(Mutex::new(database));
            let target = tempfile::tempdir().unwrap();
            let mut index =
                TurnSearchProjection::initialize(target.path(), 16 * 1024 * 1024).unwrap();
            index.require_source_admission(source);
            let projection = Arc::new(Mutex::new(index));
            let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
            let failed = process_turn_search_batch_with_durability(
                &authority,
                &projection,
                scope,
                || false,
                |witness| {
                    assert!(
                        witness.is_some(),
                        "Strict group-commit also needs a witness"
                    );
                    assert!(
                        authority.try_lock().is_ok(),
                        "WAL wait held authority mutex"
                    );
                    assert!(!projection.try_lock().unwrap().owner_admitted(7, 11));
                    Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "injected barrier deadline",
                    ))
                },
            );
            assert_eq!(failed.unwrap_err().kind(), io::ErrorKind::TimedOut);
            let (activation, checkpoint) = {
                let database = authority.lock().unwrap();
                let mut transaction = database.begin(7, 11).unwrap();
                (
                    search_backfill::load_activation(&database, &mut transaction)
                        .unwrap()
                        .unwrap(),
                    search_backfill::load(&database, &mut transaction)
                        .unwrap()
                        .unwrap(),
                )
            };
            assert_eq!(
                projection
                    .lock()
                    .unwrap()
                    .prepare_owner(7, 11, source, Some(activation))
                    .unwrap(),
                (activation, true),
                "failed barrier must not acknowledge projection"
            );
            assert!(checkpoint.complete);
            let result = process_turn_search_batch_with_durability(
                &authority,
                &projection,
                scope,
                || false,
                |witness| {
                    assert!(authority.try_lock().is_ok());
                    assert!(!projection.try_lock().unwrap().owner_admitted(7, 11));
                    let (barrier, captured) = witness.as_ref().unwrap();
                    assert_eq!(
                        barrier
                            .wait_witness(captured, std::time::Instant::now())
                            .unwrap_err()
                            .kind(),
                        io::ErrorKind::TimedOut
                    );
                    barrier.sync_for_test(vfs.as_ref())?;
                    // Rotate while this outside-mutex witness is in flight.
                    // Generation-aware waiting must not wait on the new offset.
                    {
                        let mut database = authority.lock().unwrap();
                        let old_path = database.wal_path()?;
                        database.merge_entity_overlay_fully()?;
                        barrier.sync_for_test(vfs.as_ref())?;
                        database.checkpoint_wal_window()?;
                        assert_ne!(database.wal_path()?, old_path);
                    }
                    wait_for_authority_durability(witness)
                },
            )
            .unwrap();
            assert_eq!(result.rebuilt_conversations, 0);
            assert!(projection.lock().unwrap().owner_admitted(7, 11));
            // Capture power loss before destructor CONTROL/WAL flushes.
            let crashed = Arc::new(vfs.fork_durable().unwrap());
            drop(authority);
            drop(projection);
            let recovered =
                AuthorityDatabase::open_with_vfs(Path::new("/source"), crashed).unwrap();
            let mut transaction = recovered.begin(7, 11).unwrap();
            assert_eq!(
                search_backfill::load_activation(&recovered, &mut transaction).unwrap(),
                Some(activation)
            );
            assert_eq!(
                search_backfill::load(&recovered, &mut transaction).unwrap(),
                Some(checkpoint.clone())
            );
            let mut projection =
                TurnSearchProjection::open(target.path(), 16 * 1024 * 1024).unwrap();
            assert_eq!(
                projection
                    .prepare_owner(7, 11, source, Some(activation))
                    .unwrap(),
                (activation, false)
            );

            // Warm launch: no checkpoint generation advance or index rebuild.
            drop(transaction);
            let authority = Arc::new(Mutex::new(recovered));
            let projection = Arc::new(Mutex::new(projection));
            let metrics =
                process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                    .unwrap();
            assert_eq!(metrics.rebuilt_conversations, 0);
            let database = authority.lock().unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            assert_eq!(
                search_backfill::load(&database, &mut transaction).unwrap(),
                Some(checkpoint)
            );
        }
    }

    #[test]
    fn missing_normal_mode_barrier_leaves_both_stores_untouched() {
        use crate::semantic_executor::search_backfill;
        use crate::wal::WalDurability;

        let source = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source.path()).unwrap();
        database.set_wal_durability(WalDurability::Normal).unwrap();
        let wal_bytes = database.wal_len().unwrap();
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let error =
            process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                .unwrap_err();
        assert!(error.to_string().contains("requires a durability barrier"));
        let database = authority.lock().unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            search_backfill::load_activation(&database, &mut transaction).unwrap(),
            None
        );
        assert_eq!(
            search_backfill::load(&database, &mut transaction).unwrap(),
            None
        );
        assert_eq!(database.wal_len().unwrap(), wal_bytes);
        assert_eq!(
            projection
                .lock()
                .unwrap()
                .gram_index_state_for_test(7, 11)
                .unwrap(),
            None
        );
    }

    #[test]
    fn crash_before_activation_barrier_keeps_projection_pending() {
        use crate::semantic_executor::search_backfill;
        use crate::vfs::{DeterministicVfs, Vfs};
        use crate::wal::{WalBarrier, WalDurability};
        use std::path::Path;

        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/source")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/source"), vfs.clone()).unwrap();
        let source = database.authority_uuid();
        database.set_wal_durability(WalDurability::Normal).unwrap();
        let barrier = WalBarrier::default();
        database.attach_wal_barrier(&barrier).unwrap();
        let authority = Arc::new(Mutex::new(database));
        let target = tempfile::tempdir().unwrap();
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        assert!(process_turn_search_batch_with_durability(
            &authority,
            &projection,
            scope,
            || false,
            |_| Err(io::Error::other("power loss before sync"))
        )
        .is_err());
        let activation = {
            let database = authority.lock().unwrap();
            search_backfill::load_activation(&database, &mut database.begin(7, 11).unwrap())
                .unwrap()
                .unwrap()
        };
        let crashed = Arc::new(vfs.fork_durable().unwrap());
        drop(authority);
        drop(projection);
        let recovered = AuthorityDatabase::open_with_vfs(Path::new("/source"), crashed).unwrap();
        let mut transaction = recovered.begin(7, 11).unwrap();
        assert_eq!(
            search_backfill::load_activation(&recovered, &mut transaction).unwrap(),
            None
        );
        assert_eq!(
            search_backfill::load(&recovered, &mut transaction).unwrap(),
            None
        );
        drop(transaction);
        let mut index = TurnSearchProjection::open(target.path(), 16 * 1024 * 1024).unwrap();
        index.require_source_admission(source);
        assert_eq!(
            index.prepare_owner(7, 11, source, None).unwrap(),
            (activation, true)
        );
        assert!(!index.owner_admitted(7, 11));
        let authority = Arc::new(Mutex::new(recovered));
        let projection = Arc::new(Mutex::new(index));
        process_turn_search_batch_with_admission(&authority, &projection, scope, || false).unwrap();
        assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
    }

    #[test]
    fn durable_backfill_retries_after_power_loss_before_projection_ack() {
        use crate::semantic_executor::search_backfill;
        use crate::vfs::{DeterministicVfs, Vfs};
        use crate::wal::{WalBarrier, WalDurability};
        use std::path::Path;

        for mode in [WalDurability::Strict, WalDurability::Normal] {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(Path::new("/source")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let mut database =
                AuthorityDatabase::initialize_with_vfs(Path::new("/source"), vfs.clone()).unwrap();
            let source = database.authority_uuid();
            database.set_wal_durability(mode).unwrap();
            let barrier = WalBarrier::default();
            database.attach_wal_barrier(&barrier).unwrap();
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &CreateRequest {
                    conversation_id: "conversation".into(),
                    title: "durableneedle".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
            let authority = Arc::new(Mutex::new(database));
            let target = tempfile::tempdir().unwrap();
            let mut index =
                TurnSearchProjection::initialize(target.path(), 16 * 1024 * 1024).unwrap();
            index.require_source_admission(source);
            let projection = Arc::new(Mutex::new(index));
            let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
            let failed = process_turn_search_batch_with_durability(
                &authority,
                &projection,
                scope,
                || false,
                |witness| {
                    let database = authority
                        .try_lock()
                        .expect("authority locked during WAL wait");
                    assert_eq!(search_dirty::list(&database, 7, 11)?.len(), 1);
                    assert!(!projection.try_lock().unwrap().owner_admitted(7, 11));
                    drop(database);
                    let (barrier, captured) = witness.unwrap();
                    assert_eq!(
                        barrier
                            .wait_witness(&captured, std::time::Instant::now())
                            .unwrap_err()
                            .kind(),
                        io::ErrorKind::TimedOut
                    );
                    barrier.sync_for_test(vfs.as_ref())?;
                    barrier.wait_witness(&captured, std::time::Instant::now())?;
                    // Source rows, activation and invalidations are durable;
                    // the projection has not acknowledged or published them.
                    Err(io::Error::other(
                        "power loss after sync before projection ACK",
                    ))
                },
            );
            assert!(failed.is_err());
            assert!(search(&projection, "durableneedle").is_empty());
            let (activation, checkpoint) = {
                let database = authority.lock().unwrap();
                let mut transaction = database.begin(7, 11).unwrap();
                (
                    search_backfill::load_activation(&database, &mut transaction)
                        .unwrap()
                        .unwrap(),
                    search_backfill::load(&database, &mut transaction)
                        .unwrap()
                        .unwrap(),
                )
            };
            assert!(checkpoint.complete);
            // Fork before Drop: graceful shutdown must not rescue this test.
            let crashed = Arc::new(vfs.fork_durable().unwrap());
            drop(authority);
            drop(projection);
            let recovered =
                AuthorityDatabase::open_with_vfs(Path::new("/source"), crashed).unwrap();
            let mut transaction = recovered.begin(7, 11).unwrap();
            assert_eq!(
                search_backfill::load_activation(&recovered, &mut transaction).unwrap(),
                Some(activation)
            );
            assert_eq!(
                search_backfill::load(&recovered, &mut transaction).unwrap(),
                Some(checkpoint.clone())
            );
            assert_eq!(search_dirty::list(&recovered, 7, 11).unwrap().len(), 1);
            drop(transaction);
            let mut index = TurnSearchProjection::open(target.path(), 16 * 1024 * 1024).unwrap();
            index.require_source_admission(source);
            assert_eq!(
                index
                    .prepare_owner(7, 11, source, Some(activation))
                    .unwrap(),
                (activation, true)
            );
            assert!(!index.owner_admitted(7, 11));
            let authority = Arc::new(Mutex::new(recovered));
            let projection = Arc::new(Mutex::new(index));
            let rebuilt =
                process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                    .unwrap();
            assert_eq!(rebuilt.rebuilt_conversations, 1);
            process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                .unwrap();
            assert_eq!(search(&projection, "durableneedle").len(), 1);
            assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
            let database = authority.lock().unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            assert_eq!(
                search_backfill::load(&database, &mut transaction).unwrap(),
                Some(checkpoint)
            );
            assert!(search_dirty::list(&database, 7, 11).unwrap().is_empty());
        }
    }

    #[test]
    fn returning_to_a_after_b_drains_edits_and_deletes_rebuilds_stale_rows() {
        use crate::conversation_header::{self, MetadataUpdateRequest};
        use crate::semantic_executor::search_backfill;

        let source_dir = tempfile::tempdir().unwrap();
        let a_dir = tempfile::tempdir().unwrap();
        let b_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        let source = database.authority_uuid();
        for id in ["edited", "deleted"] {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            conversation_header::create(
                &database,
                &mut transaction,
                &CreateRequest {
                    conversation_id: id.into(),
                    title: "oldneedle".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        let authority = Arc::new(Mutex::new(database));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let activate = |path: &std::path::Path| {
            let mut index = TurnSearchProjection::initialize(path, 16 * 1024 * 1024).unwrap();
            index.require_source_admission(source);
            let projection = Arc::new(Mutex::new(index));
            for _ in 0..3 {
                process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                    .unwrap();
            }
            assert_eq!(search(&projection, "oldneedle").len(), 2);
            projection
        };
        let a = activate(a_dir.path());
        let a_activation = {
            let database = authority.lock().unwrap();
            search_backfill::load_activation(&database, &mut database.begin(7, 11).unwrap())
                .unwrap()
        };
        drop(a);
        let b = activate(b_dir.path());
        {
            let mut database = authority.lock().unwrap();
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            conversation_header::update_metadata(
                &database,
                &mut transaction,
                &MetadataUpdateRequest {
                    conversation_id: "edited".into(),
                    title: Some("newneedle".into()),
                    updated_at_ms: Some(200),
                    committed_at_ms: 200,
                },
            )
            .unwrap();
            conversation_header::delete(
                &mut database,
                &mut transaction,
                &DeleteRequest {
                    conversation_id: "deleted".into(),
                    deleted_at_ms: 200,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        for _ in 0..3 {
            process_turn_search_batch_with_admission(&authority, &b, scope, || false).unwrap();
        }
        assert!(search(&b, "oldneedle").is_empty());
        assert_eq!(search(&b, "newneedle").len(), 1);
        let b_activation = {
            let database = authority.lock().unwrap();
            assert!(search_dirty::list(&database, 7, 11).unwrap().is_empty());
            search_backfill::load_activation(&database, &mut database.begin(7, 11).unwrap())
                .unwrap()
        };
        assert_ne!(b_activation, a_activation);
        drop(b);
        // Reproduce the pre-fix trust error: A still opens with exactly the
        // same source UUID/format and READY rows, but B consumed every token.
        let a = Arc::new(Mutex::new(
            TurnSearchProjection::open(a_dir.path(), 16 * 1024 * 1024).unwrap(),
        ));
        assert_eq!(search(&a, "oldneedle").len(), 2);
        assert!(search(&a, "newneedle").is_empty());
        assert!(!a.lock().unwrap().owner_indexing(7, 11).unwrap());
        a.lock().unwrap().require_source_admission(source);
        assert!(search(&a, "oldneedle").is_empty());
        for _ in 0..3 {
            process_turn_search_batch_with_admission(&authority, &a, scope, || false).unwrap();
        }
        assert!(search(&a, "oldneedle").is_empty());
        assert_eq!(search(&a, "newneedle").len(), 1);
        assert!(!a.lock().unwrap().owner_indexing(7, 11).unwrap());
        let database = authority.lock().unwrap();
        let returned =
            search_backfill::load_activation(&database, &mut database.begin(7, 11).unwrap())
                .unwrap();
        assert_ne!(returned, a_activation);
        assert_ne!(returned, b_activation);
        assert!(search_dirty::list(&database, 7, 11).unwrap().is_empty());
    }

    #[test]
    fn warm_reopen_does_no_rebuild_or_authority_commit_for_nonempty_owner() {
        use crate::semantic_executor::search_backfill;

        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        // More than two source pages catches repeated invalidation/rebuilds
        // caused by draining existing dirty work before advancing the cursor.
        let count = search_backfill::SOURCE_PAGE_ROWS * 2 + 3;
        for index in 0..count {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &CreateRequest {
                    conversation_id: format!("c{index:03}"),
                    title: "warmneedle".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        let source = database.authority_uuid();
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let mut rebuilt = 0;
        for _ in 0..8 {
            rebuilt +=
                process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                    .unwrap()
                    .rebuilt_conversations;
            if !projection.lock().unwrap().owner_indexing(7, 11).unwrap() {
                break;
            }
        }
        assert_eq!(
            rebuilt, count as u64,
            "cold backfill must rebuild each header only once"
        );
        assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
        let checkpoint = {
            let database = authority.lock().unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            search_backfill::load(&database, &mut transaction)
                .unwrap()
                .unwrap()
        };
        drop(authority);
        drop(projection);
        let authority = Arc::new(Mutex::new(
            AuthorityDatabase::open(source_dir.path()).unwrap(),
        ));

        let wal_bytes = authority.lock().unwrap().wal_len().unwrap();
        let mut index = TurnSearchProjection::open(target_dir.path(), 16 * 1024 * 1024).unwrap();
        index.require_source_admission(source);
        let projection = Arc::new(Mutex::new(index));
        for _ in 0..3 {
            let result =
                process_turn_search_batch_with_admission(&authority, &projection, scope, || false)
                    .unwrap();
            assert_eq!(result.rebuilt_conversations, 0);
            assert_eq!(result.dirty_conversations, 0);
        }
        assert_eq!(search(&projection, "warmneedle").len(), count);
        assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
        let database = authority.lock().unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            search_backfill::load(&database, &mut transaction).unwrap(),
            Some(checkpoint)
        );
        assert_eq!(database.wal_len().unwrap(), wal_bytes);
    }

    #[test]
    fn one_fairness_permit_finishes_a_bounded_batch_under_continuous_reads() {
        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        for id in ["first", "second"] {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(&database, &mut transaction, &CreateRequest {
                conversation_id: id.into(), title: "needle".into(), settings_json: b"{}".to_vec(),
                created_at_ms: 100, updated_at_ms: 100, committed_at_ms: 100,
            }).unwrap();
            database.commit(transaction).unwrap();
        }
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap()));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        // Foreground polling remains hot: exactly one maintenance permit is
        // available. Both conversations must publish and ACK in that slice.
        let deferred = process_turn_search_batch_with_admission(
            &authority, &projection, scope, || true).unwrap();
        assert_eq!(deferred.foreground_deferrals, 1);
        assert_eq!(deferred.dirty_conversations, 2);
        assert_eq!(deferred.rebuilt_conversations, 0);
        let mut permits = 1;
        let result = process_turn_search_batch_with_admission(&authority, &projection, scope, || {
            let defer = permits == 0;
            permits = 0;
            defer
        }).unwrap();
        assert_eq!(result.rebuilt_conversations, 2);
        assert_eq!(result.foreground_deferrals, 0);
        assert!(search_dirty::list(&authority.lock().unwrap(), 7, 11).unwrap().is_empty());
        let drained = process_turn_search_batch_with_admission(
            &authority, &projection, scope, || true).unwrap();
        assert_eq!(drained.foreground_deferrals, 0);
        assert_eq!(drained.dirty_conversations, 0);
    }

    #[test]
    fn backfill_publishes_the_gram_index_once_drained() {
        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &CreateRequest {
                    conversation_id: "conversation".into(),
                    title: "fixture".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        for index in 0..4u32 {
            append_turn(
                &mut database,
                &format!("turn-{index}"),
                "human",
                "main",
                "completed",
                serde_json::json!({"content": format!("needle body {index}")}),
                200,
            );
        }
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        // Before any round the owner has no gram state: the reader walks.
        assert_eq!(
            projection
                .lock()
                .unwrap()
                .gram_index_state_for_test(7, 11)
                .unwrap(),
            None
        );
        for _ in 0..16 {
            let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
            if metrics.dirty_conversations == 0
                && !projection.lock().unwrap().owner_indexing(7, 11).unwrap()
            {
                break;
            }
        }
        assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
        assert_eq!(
            projection
                .lock()
                .unwrap()
                .gram_index_state_for_test(7, 11)
                .unwrap(),
            Some(1),
            "a fully drained backfill must publish the gram index"
        );
        for _ in 0..4 {
            assert_eq!(search(&projection, "needle body").len(), 1);
        }
        // Incremental rebuilds keep the published state and exact results.
        append_turn(
            &mut authority.lock().unwrap(),
            "turn-extra",
            "human",
            "main",
            "completed",
            serde_json::json!({"content": "needle body extra"}),
            300,
        );
        for _ in 0..16 {
            let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
            if metrics.dirty_conversations == 0 {
                break;
            }
        }
        assert_eq!(
            projection
                .lock()
                .unwrap()
                .gram_index_state_for_test(7, 11)
                .unwrap(),
            Some(1)
        );
        assert_eq!(search(&projection, "needle body").len(), 1);
    }

    #[test]
    fn initial_backfill_and_dirty_queue_rebuild_each_conversation_once() {
        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        let count = search_dirty::DIRTY_BATCH * 2 + 1;
        for index in 0..count {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &CreateRequest {
                    conversation_id: format!("conversation-{index:03}"),
                    title: "backfill fixture".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        // Existing dirty tokens and a fresh disposable projection must converge
        // without first publishing the entire corpus and then rebuilding it.
        for epoch in 0..2 {
            if epoch != 0 {
                schedule_owner_search_backfill(&mut authority.lock().unwrap(), scope).unwrap();
            }
            let mut rebuilt = 0;
            for _ in 0..count {
                let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
                assert!(metrics.dirty_conversations <= search_dirty::DIRTY_BATCH as u64);
                rebuilt += metrics.rebuilt_conversations;
                assert_eq!(
                    authority
                        .lock()
                        .unwrap()
                        .snapshot_pin_metrics()
                        .unwrap()
                        .active_handles,
                    0
                );
                if metrics.dirty_conversations == 0 {
                    break;
                }
            }
            assert_eq!(
                rebuilt, count as u64,
                "each explicit backfill rebuilds exactly once"
            );
            assert_eq!(search(&projection, "backfill fixture").len(), count);
            let database = authority.lock().unwrap();
            assert!(search_dirty::list(&database, 7, 11).unwrap().is_empty());
            let mut transaction = database.begin(7, 11).unwrap();
            assert!(
                crate::semantic_executor::search_backfill::load(&database, &mut transaction)
                    .unwrap()
                    .unwrap()
                    .complete
            );
        }
    }

    #[test]
    fn backfill_cursor_preserves_new_and_deleted_conversations_across_rounds() {
        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        let create = |database: &mut AuthorityDatabase, id: &str| {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                database,
                &mut transaction,
                &CreateRequest {
                    conversation_id: id.into(),
                    title: "cursor fixture".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        };
        let count = search_dirty::DIRTY_BATCH * 2 + 1;
        for index in 0..count {
            create(&mut database, &format!("conversation-{index:03}"));
        }
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let first = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(
            first.rebuilt_conversations,
            search_dirty::DIRTY_BATCH as u64
        );
        assert!(projection.lock().unwrap().owner_indexing(7, 11).unwrap());
        {
            let mut database = authority.lock().unwrap();
            create(&mut database, "aaa-new-before-cursor");
            create(&mut database, "zzz-new-after-cursor");
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::delete(
                &mut database,
                &mut transaction,
                &DeleteRequest {
                    conversation_id: format!("conversation-{:03}", count - 1),
                    deleted_at_ms: 200,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        // A busy foreground round leaves both the durable cursor and tokens
        // intact. The next round must pick up mutations on either side.
        let foreground = authority.lock().unwrap();
        let deferred = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(deferred.foreground_deferrals, 1);
        drop(foreground);
        let mut rebuilt = first.rebuilt_conversations;
        let mut removed = 0;
        for _ in 0..count {
            let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
            rebuilt += metrics.rebuilt_conversations;
            removed += metrics.removed_conversations;
            if !projection.lock().unwrap().owner_indexing(7, 11).unwrap() {
                break;
            }
        }
        assert_eq!(rebuilt, (count + 1) as u64);
        assert_eq!(removed, 1);
        let hits = search(&projection, "cursor fixture");
        assert_eq!(hits.len(), count + 1);
        assert_eq!(search(&projection, "aaa-new-before-cursor").len(), 1);
        assert_eq!(search(&projection, "zzz-new-after-cursor").len(), 1);
        assert!(search(&projection, &format!("conversation-{:03}", count - 1)).is_empty());
        assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
        let database = authority.lock().unwrap();
        assert!(search_dirty::list(&database, 7, 11).unwrap().is_empty());
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
    }

    #[test]
    fn archive_lifecycle_and_published_retry_preserve_search_coverage() {
        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut transaction,
            &CreateRequest {
                conversation_id: "conversation".into(),
                title: "header".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 100,
                updated_at_ms: 100,
                committed_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 64 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        process_turn_search_batch(&authority, &projection, scope).unwrap();
        for (id, text, oversized) in [
            ("small", "archive-only".to_owned(), false),
            (
                "large",
                "x".repeat(crate::generated_conversation_search::SOURCE_PAGE_BYTES + 1),
                true,
            ),
        ] {
            let mut database = authority.lock().unwrap();
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::compaction_archive::create(
                &database,
                &mut transaction,
                &crate::compaction_archive::CreateRequest {
                    archive_id: id.into(),
                    conversation_id: "conversation".into(),
                    archive_scope: String::new(),
                    messages_json: serde_json::to_vec(
                        &serde_json::json!([{"role":"user","originalContent":text}]),
                    )
                    .unwrap(),
                    summary: String::new(),
                    receipt_json: b"{}".to_vec(),
                    trigger: "test".into(),
                    task_id: String::new(),
                    round_num: 0,
                    model: String::new(),
                    tokens_before: 1,
                    tokens_after: 0,
                    msgs_before: 1,
                    msgs_after: 0,
                    reason: String::new(),
                    created_at_ms: 200,
                    committed_at_ms: 200,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
            let dirty = search_dirty::list(&database, 7, 11).unwrap();
            drop(database);
            let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
            assert_eq!(metrics.rebuilt_conversations, 1);
            assert_eq!(
                projection.lock().unwrap().source_incomplete(7, 11).unwrap(),
                oversized
            );
            if !oversized {
                let hit = search(&projection, "archive-only");
                assert_eq!(hit.len(), 1);
                assert_eq!(hit[0].match_field, "originalContent");
                // Repeat the exact work after an ambiguous ACK. Published
                // content is reused rather than deleted/rebuilt under its ID.
                let replica = authority.lock().unwrap().build_read_replica().unwrap();
                let source = replica.begin(7, 11).unwrap();
                let mut retry_metrics = TurnSearchWorkerMetrics::default();
                assert!(process_conversation(
                    &authority,
                    &projection,
                    scope,
                    &dirty[0],
                    source,
                    &mut retry_metrics,
                    &replica,
                    &|| false,
                )
                .unwrap());
                assert_eq!(retry_metrics.rebuilt_conversations, 0);
            }
            let mut database = authority.lock().unwrap();
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::compaction_archive::prune(&database, &mut transaction, "conversation", "", 0)
                .unwrap();
            database.commit(transaction).unwrap();
            drop(database);
            process_turn_search_batch(&authority, &projection, scope).unwrap();
            assert!(search(&projection, "archive-only").is_empty());
            assert!(!projection.lock().unwrap().source_incomplete(7, 11).unwrap());
        }
    }

    fn append_turn(
        database: &mut AuthorityDatabase,
        turn_id: &str,
        actor: &str,
        lane_id: &str,
        status: &str,
        projection_json: serde_json::Value,
        timestamp: u64,
    ) {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        turn::append_settled(
            database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "conversation".to_owned(),
                actor: actor.to_owned(),
                status: status.to_owned(),
                projection_json: serde_json::to_vec(&projection_json).unwrap(),
                projection_timestamp: None,
                settlement_json: br#"{"outcome":"completed"}"#.to_vec(),
                lane_id: lane_id.to_owned(),
                command_id: format!("command-{turn_id}"),
                kind: "message".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: None,
                created_at_ms: timestamp,
                committed_at_ms: timestamp,
                defaults: TurnDefaults {
                    allow_create: false,
                    title: String::new(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: timestamp,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }

    fn search(
        projection: &Arc<Mutex<TurnSearchProjection>>,
        query: &str,
    ) -> Vec<crate::turn_search_projection::ConversationSearchHit> {
        projection
            .lock()
            .unwrap()
            .search(&ConversationSearchRequest {
                tenant_id: 7,
                owner_user_id: 11,
                query: query.to_owned(),
                limit: 50,
                snippet_radius: 10,
            })
            .unwrap()
    }

    #[test]
    fn batch_rebuilds_settled_main_turns_acks_tokens_and_removes_deleted_header() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        let mut create = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut create,
            &CreateRequest {
                conversation_id: "conversation".to_owned(),
                title: "title".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 100,
                updated_at_ms: 100,
                committed_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(create).unwrap();
        append_turn(
            &mut database,
            "main",
            "assistant",
            "main",
            "completed",
            serde_json::json!({
                "content": "primary searchable phrase",
                "thinking": "reasoning token",
                "translatedContent": "翻译命中",
                "originalContent": "original token"
            }),
            200,
        );
        append_turn(
            &mut database,
            "branch",
            "assistant",
            "branch-a",
            "completed",
            serde_json::json!({"content": "branch must stay hidden"}),
            300,
        );
        append_turn(
            &mut database,
            "running",
            "assistant",
            "main",
            "running",
            serde_json::json!({"content": "running must stay hidden"}),
            400,
        );
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();

        let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(metrics.dirty_conversations, 1);
        assert_eq!(metrics.rebuilt_conversations, 1);
        assert_eq!(metrics.indexed_turns, 1);
        assert_eq!(metrics.acknowledged_tokens, 2);
        assert_eq!(
            search_dirty::list(&authority.lock().unwrap(), 7, 11).unwrap(),
            []
        );
        for query in ["searchable", "reasoning", "翻译", "original"] {
            assert_eq!(search(&projection, query).len(), 1);
        }
        for query in ["branch must", "running must"] {
            assert!(search(&projection, query).is_empty());
        }

        // A clean authority still rebuilds a disposable empty projection when
        // explicitly scheduled. The ordinary bounded worker consumes the page.
        projection
            .lock()
            .unwrap()
            .remove_conversation(7, 11, "conversation")
            .unwrap();
        assert!(search(&projection, "searchable").is_empty());
        schedule_owner_search_backfill(&mut authority.lock().unwrap(), scope).unwrap();
        let rebuilt = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(rebuilt.rebuilt_conversations, 1);
        assert_eq!(search(&projection, "searchable").len(), 1);
        assert_eq!(
            process_turn_search_batch(&authority, &projection, scope)
                .unwrap()
                .dirty_conversations,
            0
        );

        let mut database = authority.lock().unwrap();
        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &mut database,
            &mut delete,
            &DeleteRequest {
                conversation_id: "conversation".to_owned(),
                deleted_at_ms: 500,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();
        drop(database);
        assert_eq!(
            search_dirty::list(&authority.lock().unwrap(), 7, 11)
                .unwrap()
                .len(),
            1
        );

        let removed = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(removed.removed_conversations, 1);
        assert!(search(&projection, "searchable").is_empty());
        assert_eq!(
            search_dirty::list(&authority.lock().unwrap(), 7, 11).unwrap(),
            []
        );
    }
    #[test]
    fn process_conversation_reads_run_entirely_off_the_foreground_authority() {
        // 2026-09-22 FUSE bench: one cold 8-row source page held the
        // authority for ~0.5 s and serialized swarm writers behind it.
        // Source reads now run on a pinned read replica; a foreground writer
        // holding the authority defers only the final acknowledge, never the
        // rebuild's page reads.
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        {
            let mut create = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                &database,
                &mut create,
                &CreateRequest {
                    conversation_id: "conversation".to_owned(),
                    title: "replica fixture".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(create).unwrap();
        }
        append_turn(
            &mut database,
            "turn-1",
            "human",
            "main",
            "completed",
            serde_json::json!({"content": "needle replica"}),
            200,
        );
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 16 * 1024 * 1024)
                .unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        let replica = authority.lock().unwrap().build_read_replica().unwrap();
        let dirty = search_dirty::list(&authority.lock().unwrap(), 7, 11)
            .unwrap()
            .into_iter()
            .next()
            .expect("the settled append leaves a dirty token");
        let source = replica.begin(7, 11).unwrap();
        let mut metrics = TurnSearchWorkerMetrics::default();
        let guard = authority.lock().unwrap();
        let done = process_conversation(
            &authority,
            &projection,
            scope,
            &dirty,
            source,
            &mut metrics,
            &replica,
            &|| false,
        )
        .unwrap();
        assert!(!done, "the acknowledge defers while the writer is held");
        assert_eq!(metrics.rebuilt_conversations, 1);
        assert!(metrics.source_pages > 0, "source reads ran off-mutex");
        assert_eq!(metrics.acknowledged_tokens, 0);
        drop(guard);
        assert_eq!(search(&projection, "needle replica").len(), 1);
        // With the writer free, the exact-token acknowledge lands and the
        // conversation leaves the dirty set.
        let source = replica.begin(7, 11).unwrap();
        let done = process_conversation(
            &authority,
            &projection,
            scope,
            &dirty,
            source,
            &mut metrics,
            &replica,
            &|| false,
        )
        .unwrap();
        assert!(done);
        assert_eq!(metrics.acknowledged_tokens, dirty.tokens.len() as u64);
        assert!(search_dirty::list(&authority.lock().unwrap(), 7, 11)
            .unwrap()
            .is_empty());
    }
}
