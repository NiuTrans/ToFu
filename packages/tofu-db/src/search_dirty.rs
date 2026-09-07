//! Transactional invalidation tokens for rebuildable conversation search.
//!
//! A marker epoch increments while an entity remains dirty. Workers compare
//! the exact opaque bytes they observed before ACK, so a concurrent mutation
//! cannot be erased even when both writes share one wall-clock millisecond.

use std::collections::BTreeMap;
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_SEARCH_DIRTY_NAMESPACE, TURN_SEARCH_DIRTY_NAMESPACE,
};

pub(crate) const DIRTY_BATCH: usize = 16;

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

pub(crate) fn mark(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    let key = marker_key(transaction, namespace, conversation_id)?;
    let current = match database.entity_get(transaction, &key)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().unwrap()),
        Some(_) => return Err(invalid_data("search dirty marker token is malformed")),
    };
    let next = current
        .checked_add(1)
        .ok_or_else(|| invalid_data("search dirty marker epoch overflow"))?
        .to_le_bytes()
        .to_vec();
    database.entity_put(transaction, key, next.clone())?;
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
            if value.len() != 8 {
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
