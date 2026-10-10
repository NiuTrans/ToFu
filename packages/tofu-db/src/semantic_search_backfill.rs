//! Explicit owner-scoped, resumable search rebuild scheduling and paging.
//!
//! The bounded search worker enqueues one header page before rebuilding dirty work.
//! Scheduling is explicit: the worker schedules on owner activation changes;
//! opening the authority never creates checkpoints or enumerates owners.
//! This is NOT `turn.search.backfill`: the legacy command schedules a global
//! rebuild, which cannot be replaced with an authenticated-owner-only rebuild.
//! Completion means invalidations were enqueued, not that projection succeeded.
//! Completed epochs remain durable to prevent stale-worker ABA after restart.

use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;

const NAMESPACE: &str = "search_backfill_checkpoint_v1";
const MAGIC: &[u8; 8] = b"TSBF0001";
const MAX_CURSOR_BYTES: usize = 1024;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Checkpoint {
    pub generation: u64,
    pub complete: bool,
    pub cursor: Vec<u8>,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        NAMESPACE,
        b"checkpoint",
    )
}

fn encode(state: &Checkpoint) -> io::Result<Vec<u8>> {
    if state.generation == 0 || state.cursor.len() > MAX_CURSOR_BYTES {
        return Err(invalid("search backfill checkpoint exceeds its bound"));
    }
    let mut bytes = Vec::with_capacity(17 + state.cursor.len());
    bytes.extend_from_slice(MAGIC);
    bytes.extend_from_slice(&state.generation.to_be_bytes());
    bytes.push(u8::from(state.complete));
    bytes.extend_from_slice(&state.cursor);
    Ok(bytes)
}

fn decode(bytes: &[u8]) -> io::Result<Checkpoint> {
    if !(17..=17 + MAX_CURSOR_BYTES).contains(&bytes.len()) || &bytes[..8] != MAGIC || bytes[16] > 1
    {
        return Err(invalid("search backfill checkpoint is malformed"));
    }
    let state = Checkpoint {
        generation: u64::from_be_bytes(bytes[8..16].try_into().unwrap()),
        complete: bytes[16] == 1,
        cursor: bytes[17..].to_vec(),
    };
    if state.generation == 0 {
        return Err(invalid("search backfill checkpoint generation is zero"));
    }
    Ok(state)
}

pub(crate) fn load(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<Checkpoint>> {
    database
        .entity_get(transaction, &key(transaction)?)?
        .map(|bytes| decode(&bytes))
        .transpose()
}

/// The only projection activation allowed to consume this owner's shared dirty
/// queue. A fresh token on each switch fences A→B→A, unlike a directory UUID.
pub(crate) fn load_activation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<uuid::Uuid>> {
    let key = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        NAMESPACE,
        b"activation",
    )?;
    database
        .entity_get(transaction, &key)?
        .map(|bytes| {
            uuid::Uuid::from_slice(&bytes).map_err(|_| invalid("search activation is malformed"))
        })
        .transpose()
}

/// Activation and the replacement backfill checkpoint share one authority
/// transaction. Retrying after a lost projection acknowledgement is idempotent.
pub(crate) fn activate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    activation: uuid::Uuid,
) -> io::Result<bool> {
    if load_activation(database, transaction)? == Some(activation) {
        if load(database, transaction)?.is_none() {
            return Err(invalid("search activation has no backfill checkpoint"));
        }
        return Ok(false);
    }
    schedule(database, transaction)?;
    let key = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        NAMESPACE,
        b"activation",
    )?;
    database.entity_put(transaction, key, activation.as_bytes().to_vec())?;
    Ok(true)
}

/// Stage a new generation, never clearing or acknowledging existing dirty work.
/// The caller owns the enclosing transaction and, for commands, its receipt.
pub(crate) fn schedule(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Checkpoint> {
    let generation = load(database, transaction)?
        .map_or(0, |state| state.generation)
        .checked_add(1)
        .ok_or_else(|| invalid("search backfill generation overflow"))?;
    let state = Checkpoint {
        generation,
        complete: false,
        cursor: Vec::new(),
    };
    database.entity_put(transaction, key(transaction)?, encode(&state)?)?;
    Ok(state)
}

/// Stage cursor progress only for the exact checkpoint the worker observed.
/// Page invalidations must be staged in this SAME transaction before committing;
/// otherwise a crash could skip source rows. Callers must discard the entire
/// transaction on error. MVCC commit validation fences competing page writers.
pub(crate) fn advance(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    observed: &Checkpoint,
    cursor: &[u8],
    complete: bool,
) -> io::Result<Checkpoint> {
    if cursor.len() > MAX_CURSOR_BYTES
        || cursor < observed.cursor.as_slice()
        || (!complete && cursor == observed.cursor.as_slice())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "search backfill cursor must advance within its bound",
        ));
    }
    if observed.complete || load(database, transaction)?.as_ref() != Some(observed) {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "search backfill checkpoint was superseded",
        ));
    }
    let next = Checkpoint {
        generation: observed.generation,
        complete,
        cursor: cursor.to_vec(),
    };
    database.entity_put(transaction, key(transaction)?, encode(&next)?)?;
    Ok(next)
}

/// Inline source headers are bounded separately from Turn/archive text pages.
pub(crate) const SOURCE_PAGE_ROWS: usize =
    crate::generated_tofudb_ir::SEARCH_PROJECTION_BACKFILL_PAGE_ROWS;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct PageProgress {
    pub enqueued: usize,
    pub source_bytes: usize,
    pub complete: bool,
}

/// Consume one page for a scope supplied by the maintenance ownership boundary.
/// No checkpoint means no scan. Source range witnesses, dirty epochs and cursor
/// publication share one commit; retries cannot skip an uncommitted invalidation.
pub(crate) fn consume_page(
    database: &mut AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
) -> io::Result<PageProgress> {
    use crate::generated_tofudb_ir::{
        CONVERSATION_DOCUMENT_NAMESPACE, CONVERSATION_SEARCH_DIRTY_NAMESPACE,
    };
    let mut transaction = database.begin(tenant_id, owner_user_id)?;
    let Some(state) = load(database, &mut transaction)? else {
        return Ok(PageProgress::default());
    };
    if state.complete {
        return Ok(PageProgress {
            complete: true,
            ..PageProgress::default()
        });
    }
    let (mut start, end) = EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        CONVERSATION_DOCUMENT_NAMESPACE,
        b"",
    )?;
    if !state.cursor.is_empty() {
        start = EntityKey::new(
            tenant_id,
            owner_user_id,
            CONVERSATION_DOCUMENT_NAMESPACE,
            &state.cursor,
        )?
        .exact_range()?
        .1;
    }
    let rows = database.entity_scan(&mut transaction, &start, &end, SOURCE_PAGE_ROWS)?;
    let mut progress = PageProgress {
        complete: rows.len() < SOURCE_PAGE_ROWS,
        ..PageProgress::default()
    };
    let mut cursor = state.cursor.clone();
    for (key, value) in rows {
        let id = std::str::from_utf8(key.key_bytes())
            .map_err(|_| invalid("search backfill source identity is not UTF-8"))?;
        crate::search_dirty::mark(
            database,
            &mut transaction,
            CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            id,
        )?;
        progress.enqueued += 1;
        progress.source_bytes += value.len();
        cursor = key.key_bytes().to_vec();
    }
    advance(
        database,
        &mut transaction,
        &state,
        &cursor,
        progress.complete,
    )?;
    database.commit(transaction)?;
    Ok(progress)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE;
    use crate::search_dirty;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn read(database: &AuthorityDatabase, owner: u64) -> Option<Checkpoint> {
        load(database, &mut database.begin(7, owner).unwrap()).unwrap()
    }

    fn setup(vfs: Arc<DeterministicVfs>) -> (AuthorityDatabase, Checkpoint) {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let state = schedule(&database, &mut transaction).unwrap();
        database.commit(transaction).unwrap();
        (database, state)
    }

    fn page(database: &mut AuthorityDatabase, state: &Checkpoint) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        search_dirty::mark(
            database,
            &mut transaction,
            CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )?;
        advance(database, &mut transaction, state, b"conversation", false)?;
        database.commit(transaction)?;
        Ok(())
    }

    #[test]
    fn checkpoint_restart_scope_and_stale_worker_fencing() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let (mut database, initial) = setup(vfs.clone());
        assert_eq!(read(&database, 12), None);
        let mut abandoned = database.begin(7, 11).unwrap();
        schedule(&database, &mut abandoned).unwrap();
        drop(abandoned);
        assert_eq!(read(&database, 11), Some(initial.clone()));
        page(&mut database, &initial).unwrap();
        let progressed = read(&database, 11).unwrap();
        assert!(page(&mut database, &initial).is_err());
        let dirty = search_dirty::list(&database, 7, 11).unwrap();
        // Mutation after the page keeps its own epoch despite a stale worker ACK.
        let mut mutation = database.begin(7, 11).unwrap();
        search_dirty::mark(
            &database,
            &mut mutation,
            CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            "conversation",
        )
        .unwrap();
        database.commit(mutation).unwrap();
        assert_eq!(
            database
                .acknowledge_search_dirty(7, 11, &dirty[0].tokens)
                .unwrap(),
            0
        );
        let mut transaction = database.begin(7, 11).unwrap();
        let finished = advance(
            &database,
            &mut transaction,
            &progressed,
            &progressed.cursor,
            true,
        )
        .unwrap();
        database.commit(transaction).unwrap();
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(read(&database, 11), Some(finished));
        let mut transaction = database.begin(7, 11).unwrap();
        let restarted = schedule(&database, &mut transaction).unwrap();
        database.commit(transaction).unwrap();
        assert_eq!(restarted.generation, initial.generation + 1);
        assert!(page(&mut database, &progressed).is_err());
        assert_eq!(read(&database, 11), Some(restarted));
        assert_eq!(search_dirty::list(&database, 7, 11).unwrap().len(), 1);
        assert!(search_dirty::list(&database, 7, 12).unwrap().is_empty());
    }

    #[test]
    fn concurrent_page_commit_cannot_overwrite_newer_checkpoint() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let (mut database, initial) = setup(vfs);
        let mut stale = database.begin(7, 11).unwrap();
        advance(&database, &mut stale, &initial, b"stale-page", false).unwrap();
        search_dirty::mark(
            &database,
            &mut stale,
            CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            "stale-only-conversation",
        )
        .unwrap();
        page(&mut database, &initial).unwrap();
        assert!(database.commit(stale).is_err());
        assert_eq!(read(&database, 11).unwrap().cursor, b"conversation");
        let dirty = search_dirty::list(&database, 7, 11).unwrap();
        assert_eq!(dirty.len(), 1);
    }

    #[test]
    fn consumer_pages_resume_and_preserve_mutations_in_exact_scope() {
        use crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE;
        let vfs = Arc::new(DeterministicVfs::new(None));
        let (mut database, _) = setup(vfs.clone());
        for owner in [11, 12] {
            let mut transaction = database.begin(7, owner).unwrap();
            for index in 0..SOURCE_PAGE_ROWS * 2 + 3 {
                database
                    .entity_put(
                        &mut transaction,
                        EntityKey::new(
                            7,
                            owner,
                            CONVERSATION_DOCUMENT_NAMESPACE,
                            format!("c{index:02}").as_bytes(),
                        )
                        .unwrap(),
                        vec![b'x'; crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES],
                    )
                    .unwrap();
            }
            database.commit(transaction).unwrap();
        }
        assert_eq!(consume_page(&mut database, 7, 12).unwrap().enqueued, 0);
        let first = consume_page(&mut database, 7, 11).unwrap();
        assert_eq!(first.enqueued, SOURCE_PAGE_ROWS);
        assert_eq!(
            first.source_bytes,
            SOURCE_PAGE_ROWS * crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES
        );
        assert!(!first.complete);
        assert_eq!(
            read(&database, 11).unwrap().cursor,
            format!("c{:02}", SOURCE_PAGE_ROWS - 1).as_bytes()
        );
        let observed = search_dirty::list(&database, 7, 11).unwrap();
        let mut mutation = database.begin(7, 11).unwrap();
        search_dirty::mark(
            &database,
            &mut mutation,
            CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            "c00",
        )
        .unwrap();
        database.commit(mutation).unwrap();
        assert_eq!(
            database
                .acknowledge_search_dirty(7, 11, &observed[0].tokens)
                .unwrap(),
            0
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            consume_page(&mut database, 7, 11).unwrap().enqueued,
            SOURCE_PAGE_ROWS
        );
        let last = consume_page(&mut database, 7, 11).unwrap();
        assert_eq!(last.enqueued, 3);
        assert!(last.complete);
        assert_eq!(consume_page(&mut database, 7, 11).unwrap().enqueued, 0);
        assert!(search_dirty::list(&database, 7, 12).unwrap().is_empty());
        assert_eq!(read(&database, 12), None);
        assert!(search_dirty::list(&database, 8, 11).unwrap().is_empty());
        assert_eq!(
            read(&database, 11).unwrap().cursor,
            format!("c{:02}", SOURCE_PAGE_ROWS * 2 + 2).as_bytes()
        );
    }

    #[test]
    fn checkpoint_bounds_and_corruption_fail_closed() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        let (database, initial) = setup(vfs);
        for cursor in [Vec::new(), vec![b'x'; MAX_CURSOR_BYTES + 1]] {
            let mut transaction = database.begin(7, 11).unwrap();
            assert!(advance(&database, &mut transaction, &initial, &cursor, false).is_err());
        }
        for bytes in [vec![], vec![0; 17], vec![0; MAX_CURSOR_BYTES + 18]] {
            assert!(decode(&bytes).is_err());
        }
        let mut malformed = encode(&initial).unwrap();
        malformed[16] = 2;
        assert!(decode(&malformed).is_err());
        assert_eq!(read(&database, 11), Some(initial));
    }

    #[test]
    fn checkpoint_page_faults_never_publish_cursor_without_dirty_work() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let (mut baseline, state) = setup(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        page(&mut baseline, &state).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        let mut faults = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            faults.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                faults.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
        }
        for (operation_number, action) in faults {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let (mut database, initial) = setup(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = page(&mut database, &initial);
            drop(database);
            vfs.arm_fault(None).unwrap();
            vfs.crash().unwrap();
            let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let recovered = read(&database, 11).unwrap();
            let dirty = search_dirty::list(&database, 7, 11).unwrap();
            if recovered.cursor.is_empty() {
                assert!(dirty.is_empty());
                page(&mut database, &recovered).unwrap();
            } else {
                assert_eq!(recovered.cursor, b"conversation");
                assert_eq!(dirty.len(), 1);
            }
            assert_eq!(read(&database, 11).unwrap().cursor, b"conversation");
        }
    }
}
