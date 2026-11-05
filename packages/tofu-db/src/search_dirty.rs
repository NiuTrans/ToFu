//! Transactional invalidation tokens for rebuildable conversation search.
//!
//! Each invalidation gets a fresh persisted UUID token. Exact-byte ACK remains
//! safe across delete/recreate cycles as well as concurrent writes. Legacy
//! 8-byte epochs stay readable until the next invalidation replaces them.
//! `SearchInvalidations` counts committed invalidations per worker scope: it
//! wakes the maintenance worker and keeps query coverage `indexing` until a
//! round has consumed them.

use std::collections::BTreeMap;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_SEARCH_DIRTY_NAMESPACE, TURN_SEARCH_DIRTY_NAMESPACE,
};

pub(crate) const DIRTY_BATCH: usize =
    crate::generated_tofudb_ir::SEARCH_PROJECTION_MAXIMUM_DIRTY_CONVERSATIONS;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DirtyToken {
    pub key: EntityKey,
    pub value: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DirtyConversation {
    pub conversation_id: String,
    pub tokens: Vec<DirtyToken>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn marker_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    if !matches!(
        namespace,
        TURN_SEARCH_DIRTY_NAMESPACE | CONVERSATION_SEARCH_DIRTY_NAMESPACE
    ) || conversation_id.is_empty()
    {
        return Err(invalid_input("invalid search dirty marker identity"));
    }
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        conversation_id.as_bytes(),
    )
}

/// Process-local commit sequence for the scopes one maintenance worker
/// consumes. A commit that staged a dirty token advances its scope under the
/// authority mutex and rings the worker; a round that publishes READY from an
/// empty dirty listing records the sequence it observed in that same hold as
/// consumed. Durable tokens stay the only source of work: the sequence only
/// fences READY coverage and schedules rounds, so losing it on restart drops
/// no invalidation.
pub(crate) struct SearchInvalidations {
    scopes: Vec<(u64, u64)>,
    committed: Vec<AtomicU64>,
    consumed: Vec<AtomicU64>,
    wake: Box<dyn Fn(usize) + Send + Sync>,
}

impl SearchInvalidations {
    /// `wake` receives the index of the scope in sorted `(tenant, owner)` order.
    pub(crate) fn new(
        scopes: impl IntoIterator<Item = (u64, u64)>,
        wake: impl Fn(usize) + Send + Sync + 'static,
    ) -> Self {
        let mut scopes = scopes.into_iter().collect::<Vec<_>>();
        scopes.sort_unstable();
        scopes.dedup();
        Self {
            committed: scopes.iter().map(|_| AtomicU64::new(0)).collect(),
            consumed: scopes.iter().map(|_| AtomicU64::new(0)).collect(),
            scopes,
            wake: Box::new(wake),
        }
    }

    fn index(&self, tenant_id: u64, owner_user_id: u64) -> Option<usize> {
        self.scopes.binary_search(&(tenant_id, owner_user_id)).ok()
    }

    pub(crate) fn note_commit(&self, tenant_id: u64, owner_user_id: u64) {
        if let Some(index) = self.index(tenant_id, owner_user_id) {
            self.committed[index].fetch_add(1, Ordering::AcqRel);
            (self.wake)(index);
        }
    }

    /// Read under the authority hold that lists the scope's dirty tokens.
    pub(crate) fn committed(&self, tenant_id: u64, owner_user_id: u64) -> Option<u64> {
        self.index(tenant_id, owner_user_id)
            .map(|index| self.committed[index].load(Ordering::Acquire))
    }

    pub(crate) fn mark_consumed(&self, tenant_id: u64, owner_user_id: u64, observed: u64) {
        if let Some(index) = self.index(tenant_id, owner_user_id) {
            self.consumed[index].fetch_max(observed, Ordering::AcqRel);
        }
    }

    /// An acknowledged invalidation has not yet been proven consumed.
    pub(crate) fn pending(&self, tenant_id: u64, owner_user_id: u64) -> bool {
        self.index(tenant_id, owner_user_id).is_some_and(|index| {
            let consumed = self.consumed[index].load(Ordering::Acquire);
            self.committed[index].load(Ordering::Acquire) > consumed
        })
    }
}

pub(crate) fn valid_token(value: &[u8]) -> bool {
    value.len() == crate::generated_conversation_search::LEGACY_DIRTY_TOKEN_BYTES
        || value.len() == crate::generated_conversation_search::DIRTY_TOKEN_BYTES
}

pub(crate) fn mark(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    let key = marker_key(transaction, namespace, conversation_id)?;
    if let Some(raw) = database.entity_get(transaction, &key)? {
        if !valid_token(&raw) {
            return Err(invalid_data("search dirty marker token is malformed"));
        }
    }
    let next = uuid::Uuid::new_v4().as_bytes().to_vec();
    database.entity_put(transaction, key, next.clone())?;
    transaction.note_search_invalidation();
    Ok(next)
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
) -> io::Result<Vec<DirtyConversation>> {
    let mut transaction = database.begin(tenant_id, owner_user_id)?;
    let mut conversations = BTreeMap::<String, Vec<DirtyToken>>::new();
    for namespace in [
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        TURN_SEARCH_DIRTY_NAMESPACE,
    ] {
        let (start, end) = EntityKey::prefix_range(tenant_id, owner_user_id, namespace, b"")?;
        for (key, value) in database.entity_scan(&mut transaction, &start, &end, DIRTY_BATCH)? {
            if !valid_token(&value) {
                return Err(invalid_data("search dirty marker token is malformed"));
            }
            let conversation_id = std::str::from_utf8(key.key_bytes())
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| invalid_data("search dirty marker key is malformed"))?
                .to_owned();
            conversations
                .entry(conversation_id)
                .or_default()
                .push(DirtyToken { key, value });
        }
    }
    Ok(conversations
        .into_iter()
        .take(DIRTY_BATCH)
        .map(|(conversation_id, tokens)| DirtyConversation {
            conversation_id,
            tokens,
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    #[test]
    fn stale_ack_cannot_erase_a_concurrent_dirty_epoch() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut first = database.begin(7, 11).unwrap();
        let old = mark(
            &database,
            &mut first,
            TURN_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )
        .unwrap();
        database.commit(first).unwrap();
        let observed = list(&database, 7, 11).unwrap();
        assert_eq!(observed[0].tokens[0].value, old);

        let mut concurrent = database.begin(7, 11).unwrap();
        let new = mark(
            &database,
            &mut concurrent,
            TURN_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )
        .unwrap();
        database.commit(concurrent).unwrap();
        assert_ne!(old, new);

        assert_eq!(
            database
                .acknowledge_search_dirty(7, 11, &observed[0].tokens)
                .unwrap(),
            0
        );
        assert_eq!(list(&database, 7, 11).unwrap()[0].tokens[0].value, new);
    }

    #[test]
    fn only_committed_invalidations_of_tracked_scopes_advance_and_ring() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let rings = Arc::new(std::sync::Mutex::new(Vec::new()));
        let recorded = Arc::clone(&rings);
        let invalidations = Arc::new(SearchInvalidations::new([(7, 12), (7, 11), (7, 11)],
            move |index| recorded.lock().unwrap().push(index)));
        database.attach_search_invalidations(&invalidations);
        let mut abandoned = database.begin(7, 11).unwrap();
        mark(&database, &mut abandoned, TURN_SEARCH_DIRTY_NAMESPACE, "abandoned").unwrap();
        drop(abandoned);
        let mut plain = database.begin(7, 11).unwrap();
        database.entity_put(&mut plain, EntityKey::new(7, 11, "record", b"plain").unwrap(),
            b"value".to_vec()).unwrap();
        database.commit(plain).unwrap();
        let mut untracked = database.begin(7, 13).unwrap();
        mark(&database, &mut untracked, TURN_SEARCH_DIRTY_NAMESPACE, "other").unwrap();
        database.commit(untracked).unwrap();
        assert!(rings.lock().unwrap().is_empty());
        assert_eq!(invalidations.committed(7, 13), None);
        assert!(!invalidations.pending(7, 13));

        let mut first = database.begin(7, 11).unwrap();
        let mut conflicting = database.begin(7, 11).unwrap();
        mark(&database, &mut first, TURN_SEARCH_DIRTY_NAMESPACE, "conversation").unwrap();
        mark(&database, &mut first, CONVERSATION_SEARCH_DIRTY_NAMESPACE, "conversation").unwrap();
        mark(&database, &mut conflicting, TURN_SEARCH_DIRTY_NAMESPACE, "conversation").unwrap();
        database.commit(first).unwrap();
        assert!(database.commit(conflicting).is_err(), "stale marker read must not commit");
        // (7, 11) sorts first; one ring per committed transaction.
        assert_eq!(*rings.lock().unwrap(), [0]);
        assert_eq!(invalidations.committed(7, 11), Some(1));
        assert!(invalidations.pending(7, 11));
        assert!(!invalidations.pending(7, 12));
        invalidations.mark_consumed(7, 11, 1);
        assert!(!invalidations.pending(7, 11));
        invalidations.mark_consumed(7, 11, 0);
        assert!(!invalidations.pending(7, 11), "a stale observation cannot reopen coverage");
    }

    #[test]
    fn acknowledged_marker_recreation_cannot_reuse_an_old_worker_token() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut first = database.begin(7, 11).unwrap();
        mark(
            &database,
            &mut first,
            TURN_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )
        .unwrap();
        database.commit(first).unwrap();
        let observed = list(&database, 7, 11).unwrap();
        assert_eq!(
            database
                .acknowledge_search_dirty(7, 11, &observed[0].tokens)
                .unwrap(),
            1
        );
        let mut second = database.begin(7, 11).unwrap();
        let new = mark(
            &database,
            &mut second,
            TURN_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )
        .unwrap();
        database.commit(second).unwrap();
        assert_ne!(new, observed[0].tokens[0].value);
        assert_eq!(
            database
                .acknowledge_search_dirty(7, 11, &observed[0].tokens)
                .unwrap(),
            0
        );
        assert_eq!(list(&database, 7, 11).unwrap()[0].tokens[0].value, new);
    }

    fn prepared_marker(vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        mark(
            &database,
            &mut transaction,
            TURN_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )
        .unwrap();
        database.commit(transaction).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_ack_retries(vfs: Arc<DeterministicVfs>) {
        vfs.arm_fault(None).unwrap();
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let pending = list(&database, 7, 11).unwrap();
        assert!(pending.len() <= 1);
        if let Some(dirty) = pending.first() {
            database
                .acknowledge_search_dirty(7, 11, &dirty.tokens)
                .unwrap();
        }
        assert!(list(&database, 7, 11).unwrap().is_empty());
    }

    #[test]
    fn every_ack_io_fault_recovers_absent_or_retryable_marker() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_marker(Arc::clone(&baseline_vfs));
        let dirty = list(&baseline, 7, 11).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline
            .acknowledge_search_dirty(7, 11, &dirty[0].tokens)
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_marker(Arc::clone(&vfs));
            let dirty = list(&database, 7, 11).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = database.acknowledge_search_dirty(7, 11, &dirty[0].tokens);
            drop(database);
            assert_ack_retries(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_marker(Arc::clone(&vfs));
            let dirty = list(&database, 7, 11).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = database.acknowledge_search_dirty(7, 11, &dirty[0].tokens);
            drop(database);
            assert_ack_retries(vfs);
        }
    }
}
