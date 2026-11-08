//! Per-owner recency directory (`docs/TOFUDB_RECENCY_DIRECTORY.md`).
//!
//! One derived document per owner holds the newest
//! `MAX_RECENCY_DIRECTORY_ENTRIES` `(updated_at_ms, conversation_id)`
//! entries, sorted exactly like the legacy sidebar comparator
//! (`updated_at` desc, `id` desc). Maintenance rides the same transaction
//! as every recency-affecting mutation (`store_header` funnel, create,
//! delete/purge, restore); a lazy bounded backfill brings pre-directory
//! authorities forward over a handful of commits. The directory only ever
//! *serves* a list when every coverage gate passes — any disagreement
//! falls back to the exact stable-catalog selection in the parent module.

use std::io;

use serde_json::Value;

use super::{
    header_from_index_value, invalid_data, key, load_header, project_header,
    ListOrder, ListRequest, CONVERSATION_COUNT_NAMESPACE, COUNT_KEY,
};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_DOCUMENT_NAMESPACE, CONVERSATION_RECENCY_DIRECTORY_NAMESPACE,
    CONVERSATION_RECENCY_DIRECTORY_STATE_NAMESPACE, MAX_RECENCY_DIRECTORY_BACKFILL_ROWS_PER_TRANSACTION,
    MAX_RECENCY_DIRECTORY_ENTRIES,
};

const DIRECTORY_LOGICAL_KEY: &[u8] = b"directory";
const STATE_LOGICAL_KEY: &[u8] = b"state";
const VALUE_MAGIC: &[u8; 3] = b"RD1";
const STATE_COMPLETE: &[u8; 1] = b"C";
const STATE_BACKFILLING: &[u8; 1] = b"B";
const MAX_CONVERSATION_ID_BYTES: usize = 255;

/// One directory entry. Ordering matches the legacy comparator exactly:
/// `updated_at` descending, then `id` (byte-wise == string-wise for the
/// ASCII conversation ids) descending.
pub(crate) type Entry = (u64, Vec<u8>);

#[derive(Debug, Eq, PartialEq)]
enum DirectoryState {
    Absent,
    Backfilling(Vec<u8>),
    Complete,
}

fn directory_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_RECENCY_DIRECTORY_NAMESPACE,
        DIRECTORY_LOGICAL_KEY,
    )
}

fn state_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_RECENCY_DIRECTORY_STATE_NAMESPACE,
        STATE_LOGICAL_KEY,
    )
}

fn read_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<DirectoryState> {
    match database.entity_get(transaction, &state_key(transaction)?)? {
        None => Ok(DirectoryState::Absent),
        Some(value) if value.as_slice() == STATE_COMPLETE => Ok(DirectoryState::Complete),
        Some(value)
            if value.len() > 1
                && value.starts_with(STATE_BACKFILLING)
                && value.len() - 1 <= MAX_CONVERSATION_ID_BYTES =>
        {
            Ok(DirectoryState::Backfilling(value[1..].to_vec()))
        }
        Some(_) => Err(invalid_data("conversation recency directory state is malformed")),
    }
}

fn write_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    state: &DirectoryState,
) -> io::Result<()> {
    let key = state_key(transaction)?;
    match state {
        DirectoryState::Absent => database.entity_delete(transaction, key),
        DirectoryState::Backfilling(cursor) => {
            let mut value = Vec::with_capacity(1 + cursor.len());
            value.extend_from_slice(STATE_BACKFILLING);
            value.extend_from_slice(cursor);
            database.entity_put(transaction, key, value)
        }
        DirectoryState::Complete => {
            database.entity_put(transaction, key, STATE_COMPLETE.to_vec())
        }
    }
}

fn sort_entries(entries: &mut [Entry]) {
    entries.sort_unstable_by(|left, right| {
        right
            .0
            .cmp(&left.0)
            .then_with(|| right.1.cmp(&left.1))
    });
}

fn upsert(entries: &mut Vec<Entry>, updated_at_ms: u64, conversation_id: &[u8]) {
    if let Some(existing) = entries
        .iter_mut()
        .find(|(_, id)| id.as_slice() == conversation_id)
    {
        existing.0 = updated_at_ms;
    } else {
        entries.push((updated_at_ms, conversation_id.to_vec()));
    }
    sort_entries(entries);
    entries.truncate(MAX_RECENCY_DIRECTORY_ENTRIES);
}

fn remove(entries: &mut Vec<Entry>, conversation_id: &[u8]) {
    entries.retain(|(_, id)| id.as_slice() != conversation_id);
}

fn encode_directory(live_count: u64, entries: &[Entry]) -> Vec<u8> {
    let mut encoded = Vec::with_capacity(15 + entries.len() * 24);
    encoded.extend_from_slice(VALUE_MAGIC);
    encoded.extend_from_slice(&live_count.to_le_bytes());
    encoded.extend_from_slice(&(entries.len() as u32).to_le_bytes());
    for (updated_at_ms, conversation_id) in entries {
        encoded.extend_from_slice(&updated_at_ms.to_le_bytes());
        encoded.push(conversation_id.len() as u8);
        encoded.extend_from_slice(conversation_id);
    }
    encoded
}

fn decode_directory(value: &[u8]) -> io::Result<(u64, Vec<Entry>)> {
    if value.len() < 15 || !value.starts_with(VALUE_MAGIC) {
        return Err(invalid_data("conversation recency directory is malformed"));
    }
    let live_count = u64::from_le_bytes(value[3..11].try_into().unwrap());
    let declared = u32::from_le_bytes(value[11..15].try_into().unwrap()) as usize;
    if declared > MAX_RECENCY_DIRECTORY_ENTRIES {
        return Err(invalid_data("conversation recency directory exceeds its entry bound"));
    }
    let mut entries = Vec::with_capacity(declared);
    let mut offset = 15_usize;
    for _ in 0..declared {
        if offset + 9 > value.len() {
            return Err(invalid_data("conversation recency directory entry is truncated"));
        }
        let updated_at_ms = u64::from_le_bytes(value[offset..offset + 8].try_into().unwrap());
        let id_len = value[offset + 8] as usize;
        offset += 9;
        if id_len == 0 || offset + id_len > value.len() {
            return Err(invalid_data("conversation recency directory id is truncated"));
        }
        entries.push((updated_at_ms, value[offset..offset + id_len].to_vec()));
        offset += id_len;
    }
    if offset != value.len() {
        return Err(invalid_data("conversation recency directory has trailing bytes"));
    }
    Ok((live_count, entries))
}

fn load_directory(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<(u64, Vec<Entry>)> {
    match database.entity_get(transaction, &directory_key(transaction)?)? {
        None => Ok((0, Vec::new())),
        Some(value) => decode_directory(&value),
    }
}

fn store_directory(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    live_count: u64,
    entries: &[Entry],
) -> io::Result<()> {
    database.entity_put(
        transaction,
        directory_key(transaction)?,
        encode_directory(live_count, entries),
    )
}

fn owner_live_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let count_key = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    super::read_count(database, transaction, &count_key)
}

/// Drive at most one bounded backfill batch. Absent starts at the document
/// namespace head; a short page (or an empty one) seals the directory as
/// Complete. Backfill rows upsert from the stored headers, so mutations
/// hooked during the window stay idempotent.
fn backfill_one_batch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<()> {
    let state = read_state(database, transaction)?;
    let cursor = match state {
        DirectoryState::Complete => return Ok(()),
        DirectoryState::Absent => Vec::new(),
        DirectoryState::Backfilling(cursor) => cursor,
    };
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let (mut start, end) = EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        CONVERSATION_DOCUMENT_NAMESPACE,
        b"",
    )?;
    if !cursor.is_empty() {
        let mut after = cursor;
        after.push(0);
        start = key(tenant_id, owner_user_id, CONVERSATION_DOCUMENT_NAMESPACE, &after)?;
    }
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_RECENCY_DIRECTORY_BACKFILL_ROWS_PER_TRANSACTION,
    )?;
    let (_, mut entries) = load_directory(database, transaction)?;
    let mut next_cursor = None;
    for (document_key, stored) in &rows {
        let (conversation_id, header) =
            header_from_index_value(database, tenant_id, owner_user_id, stored)?;
        let updated_at_ms = header
            .get("updated_at")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
        if conversation_id.as_bytes().len() > MAX_CONVERSATION_ID_BYTES {
            return Err(invalid_data("conversation id exceeds the recency directory bound"));
        }
        upsert(&mut entries, updated_at_ms, conversation_id.as_bytes());
        next_cursor = Some(document_key.key_bytes().to_vec());
    }
    let live_count = owner_live_count(database, transaction)?;
    store_directory(database, transaction, live_count, &entries)?;
    if rows.len() < MAX_RECENCY_DIRECTORY_BACKFILL_ROWS_PER_TRANSACTION {
        write_state(database, transaction, &DirectoryState::Complete)?;
    } else if let Some(cursor) = next_cursor {
        write_state(database, transaction, &DirectoryState::Backfilling(cursor))?;
    }
    Ok(())
}

/// The shared maintenance core: drive one backfill batch (no-op once
/// Complete), then apply `mutate` to the directory and persist it with the
/// current live count. Called only from the recency-affecting funnels.
fn maintain(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    mutate: impl FnOnce(&mut Vec<Entry>),
) -> io::Result<()> {
    backfill_one_batch(database, transaction)?;
    let (_, mut entries) = load_directory(database, transaction)?;
    mutate(&mut entries);
    let live_count = owner_live_count(database, transaction)?;
    store_directory(database, transaction, live_count, &entries)
}

/// A logical secondary-index update, retained by a prepared transaction and
/// applied to the current physical directory only after business validation.
/// Reading the index earlier flushes these updates into the snapshot so
/// read-your-writes and its predicate witnesses remain exact.
pub(crate) enum DeferredMutation {
    Created { conversation_id: String, created_at_ms: u64, updated_at_ms: u64 },
    Upsert {
        conversation_id: String,
        updated_at_ms: u64,
    },
    Remove {
        conversation_id: String,
    },
}

impl DeferredMutation {
    pub(crate) fn retained_bytes(&self) -> usize {
        let id = match self {
            Self::Upsert {
                conversation_id, ..
            }
            | Self::Created { conversation_id, .. }
            | Self::Remove { conversation_id } => conversation_id,
        };
        id.len().saturating_add(128)
    }
    pub(crate) fn apply(
        self,
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<()> {
        match self {
            Self::Created { conversation_id, created_at_ms, updated_at_ms } =>
                super::apply_created_metadata(database, transaction, &conversation_id,
                    created_at_ms, updated_at_ms),
            Self::Upsert { conversation_id, updated_at_ms } => maintain(database, transaction,
                |entries| upsert(entries, updated_at_ms, conversation_id.as_bytes())),
            Self::Remove { conversation_id } => maintain(database, transaction,
                |entries| remove(entries, conversation_id.as_bytes())),
        }
    }
}

/// `store_header` funnel: a header write (turn settlement, settings,
/// relink, clone parent, restore) upserts the conversation's recency.
pub(crate) fn note_upsert(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    updated_at_ms: u64,
) -> io::Result<()> {
    if conversation_id.as_bytes().len() > MAX_CONVERSATION_ID_BYTES {
        return Err(invalid_data("conversation id exceeds the recency directory bound"));
    }
    if database.is_preparing_transaction() {
        return database.defer_index_update(transaction, DeferredMutation::Upsert {
            conversation_id: conversation_id.to_owned(), updated_at_ms,
        });
    }
    maintain(database, transaction, |entries| {
        upsert(entries, updated_at_ms, conversation_id.as_bytes());
    })
}

/// Delete/purge funnel: the conversation leaves the live set.
pub(crate) fn note_remove(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    if database.is_preparing_transaction() {
        return database.defer_index_update(transaction, DeferredMutation::Remove {
            conversation_id: conversation_id.to_owned(),
        });
    }
    maintain(database, transaction, |entries| {
        remove(entries, conversation_id.as_bytes());
    })
}

fn unfiltered(request: &ListRequest) -> bool {
    request.project_path.is_none()
        && request.title_contains.is_none()
        && request.ids.is_none()
        && request.updated_at_gte.is_none()
        && request.updated_at_gt.is_none()
        && request.created_at_lt.is_none()
}

/// The owner's live count and newest-first entries when the directory state,
/// recorded live count and entry count all agree with the authority; `None`
/// sends the caller to its exact legacy read. Page rows still need per-row
/// hydration checks by the caller.
pub(crate) fn covered_entries(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<(u64, Vec<Entry>)>> {
    database.flush_deferred_indexes(transaction)?;
    if read_state(database, transaction)? != DirectoryState::Complete {
        return Ok(None);
    }
    let (live_count, entries) = load_directory(database, transaction)?;
    if live_count != owner_live_count(database, transaction)? {
        return Ok(None);
    }
    if entries.len() != (live_count as usize).min(MAX_RECENCY_DIRECTORY_ENTRIES) {
        return Ok(None);
    }
    Ok(Some((live_count, entries)))
}

/// Serve an updated-descending unfiltered list from the directory, or
/// return `None` so the caller takes the exact legacy path. Every coverage
/// gate from `docs/TOFUDB_RECENCY_DIRECTORY.md` is checked: request shape,
/// state, live-count agreement, page depth, and per-row timestamp equality
/// after hydration.
pub(crate) fn try_directory_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<Vec<u8>>> {
    if !matches!(request.order, ListOrder::UpdatedDescending) || !unfiltered(request) {
        return Ok(None);
    }
    if request.limit > MAX_RECENCY_DIRECTORY_ENTRIES {
        return Ok(None);
    }
    let Some((_, entries)) = covered_entries(database, transaction)? else {
        return Ok(None);
    };
    if request.limit > entries.len() {
        return Ok(None);
    }
    let maximum_bytes = if request.include_messages {
        crate::generated_tofudb_ir::MAX_FROZEN_TRANSCRIPT_PAGE_BYTES
    } else {
        crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES
    };
    let mut response = vec![b'['];
    for (updated_at_ms, conversation_id) in entries.iter().take(request.limit) {
        let conversation_id = std::str::from_utf8(conversation_id)
            .map_err(|_| invalid_data("conversation recency directory id is not UTF-8"))?;
        let Some((header, _)) = load_header(database, transaction, conversation_id)? else {
            return Ok(None);
        };
        if header.get("updated_at").and_then(Value::as_u64) != Some(*updated_at_ms) {
            return Ok(None);
        }
        let mut document = project_header(header, request.settings_keys.as_deref())?;
        if request.include_messages {
            if deadline_elapsed() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "conversation list message hydration deadline elapsed",
                ));
            }
            let (messages, count, _, _) = crate::turn::legacy_messages(
                database,
                transaction,
                conversation_id,
                0,
                None,
                deadline_elapsed,
            )?;
            document["messages"] = Value::Array(messages);
            document["metadata"]["msg_count"] = Value::from(count as u64);
        }
        let bytes = serde_json::to_vec(&document)
            .map_err(|_| invalid_data("conversation list cannot be encoded"))?;
        let separator = usize::from(response.len() > 1);
        if response
            .len()
            .checked_add(separator + bytes.len() + 1)
            .is_none_or(|length| length > maximum_bytes)
        {
            return Err(crate::resource_exhaustion::domain_bound(
                "conversation list response exceeds its byte budget",
            ));
        }
        if separator == 1 {
            response.push(b',');
        }
        response.extend_from_slice(&bytes);
    }
    response.push(b']');
    Ok(Some(response))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(at: u64, id: &str) -> Entry {
        (at, id.as_bytes().to_vec())
    }

    #[test]
    fn codec_round_trips_and_rejects_garbage() {
        let entries = vec![entry(3, "c"), entry(2, "b"), entry(2, "a"), entry(1, "a0")];
        let encoded = encode_directory(4, &entries);
        let (live_count, decoded) = decode_directory(&encoded).unwrap();
        assert_eq!(live_count, 4);
        assert_eq!(decoded, entries);
        assert!(decode_directory(b"").is_err());
        assert!(decode_directory(b"RD2............").is_err());
        let mut truncated = encoded.clone();
        truncated.pop();
        assert!(decode_directory(&truncated).is_err());
        let mut trailing = encoded.clone();
        trailing.push(0);
        assert!(decode_directory(&trailing).is_err());
    }

    #[test]
    fn upsert_orders_caps_and_replaces() {
        let mut entries = Vec::new();
        upsert(&mut entries, 1, b"a");
        upsert(&mut entries, 5, b"b");
        upsert(&mut entries, 5, b"c");
        upsert(&mut entries, 3, b"d");
        upsert(&mut entries, 9, b"a");
        assert_eq!(
            entries,
            vec![entry(9, "a"), entry(5, "c"), entry(5, "b"), entry(3, "d")]
        );
        remove(&mut entries, b"c");
        assert_eq!(entries, vec![entry(9, "a"), entry(5, "b"), entry(3, "d")]);
        for index in 0..200_u64 {
            upsert(&mut entries, 1_000 + index, format!("conv-{index:03}").as_bytes());
        }
        assert_eq!(entries.len(), MAX_RECENCY_DIRECTORY_ENTRIES);
        assert_eq!(entries[0].0, 1_199);
        assert_eq!(entries[MAX_RECENCY_DIRECTORY_ENTRIES - 1].0, 1_072);
    }

    // ---- Integration: directory-served lists are byte-identical to the
    // legacy answer across mutations, backfill windows, and fallbacks.

    use std::path::Path;
    use std::sync::Arc;

    use serde_json::json;

    use crate::vfs::{DeterministicVfs, Vfs};

    const TENANT: u64 = 7;
    const OWNER: u64 = 11;

    fn authority() -> AuthorityDatabase {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
        AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs).unwrap()
    }

    fn make_create(id: &str, at: u64) -> super::super::CreateRequest {
        super::super::CreateRequest {
            conversation_id: id.to_owned(),
            title: format!("Title {id}"),
            settings_json: b"{}".to_vec(),
            created_at_ms: at,
            updated_at_ms: at,
            committed_at_ms: at,
        }
    }

    fn create(database: &mut AuthorityDatabase, id: &str, at: u64) {
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        super::super::create(database, &mut tx, &make_create(id, at)).unwrap();
        database.commit(tx).unwrap();
    }

    /// Move a conversation's recency through the production funnel: it
    /// stores the header (directory upsert) and rekeys the updated-time index
    /// and the activity candidate in the same transaction.
    fn bump(database: &mut AuthorityDatabase, id: &str, at: u64) {
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        super::super::update_metadata(database, &mut tx, &super::super::MetadataUpdateRequest {
            conversation_id: id.to_owned(),
            title: None,
            updated_at_ms: Some(at),
            committed_at_ms: at,
        })
        .unwrap();
        database.commit(tx).unwrap();
    }

    fn remove_conversation(database: &mut AuthorityDatabase, id: &str, at: u64) {
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        super::super::delete(
            database,
            &mut tx,
            &super::super::DeleteRequest {
                conversation_id: id.to_owned(),
                deleted_at_ms: at,
            },
        )
        .unwrap();
        database.commit(tx).unwrap();
    }

    fn revive(database: &mut AuthorityDatabase, id: &str, at: u64) {
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        super::super::restore(
            database,
            &mut tx,
            &super::super::RestoreRequest {
                conversation_id: id.to_owned(),
                committed_at_ms: at,
            },
        )
        .unwrap();
        database.commit(tx).unwrap();
    }

    fn list_request(limit: usize) -> super::super::ListRequest {
        super::super::ListRequest {
            project_path: None,
            title_contains: None,
            ids: None,
            updated_at_gte: None,
            updated_at_gt: None,
            created_at_lt: None,
            order: ListOrder::UpdatedDescending,
            settings_keys: None,
            include_messages: false,
            limit,
        }
    }

    fn served(database: &AuthorityDatabase, limit: usize) -> Vec<u8> {
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        super::super::list(database, &mut tx, &list_request(limit), &mut || false).unwrap()
    }

    fn legacy(database: &AuthorityDatabase, limit: usize) -> Vec<u8> {
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        super::super::document_list(database, &mut tx, &list_request(limit), &mut || false).unwrap()
    }

    fn state_of(database: &AuthorityDatabase) -> DirectoryState {
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        read_state(database, &mut tx).unwrap()
    }

    #[test]
    fn directory_serves_identical_lists_across_a_mutation_script() {
        let mut database = authority();
        for index in 0..8_u64 {
            create(&mut database, &format!("conv-{index}"), 100 + index);
            assert_eq!(
                served(&database, 50),
                legacy(&database, 50),
                "list diverged after create {index}"
            );
        }
        assert_eq!(state_of(&database), DirectoryState::Complete);
        bump(&mut database, "conv-2", 300);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        bump(&mut database, "conv-7", 200);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        remove_conversation(&mut database, "conv-5", 400);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        revive(&mut database, "conv-5", 500);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        // Restore keeps the original updated_at: conv-5 sorts by 105 again.
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        let (header, _) = super::super::load_header(&database, &mut tx, "conv-5").unwrap().unwrap();
        assert_eq!(header.get("updated_at").and_then(Value::as_u64), Some(105));
    }

    #[test]
    fn directory_completes_backfill_over_bounded_batches() {
        let mut database = authority();
        // Plant a legacy-sized namespace, then force the state machine back to
        // Absent (a pre-directory authority): the next mutations must walk the
        // 64-row backfill batches to completion while staying exact.
        for index in 0..150_u64 {
            create(&mut database, &format!("fill-{index:03}"), 10 + index);
        }
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let reset_key = state_key(&tx).unwrap();
        database.entity_delete(&mut tx, reset_key).unwrap();
        database.commit(tx).unwrap();
        assert_eq!(state_of(&database), DirectoryState::Absent);

        bump(&mut database, "fill-000", 500);
        assert!(matches!(state_of(&database), DirectoryState::Backfilling(_)));
        assert_eq!(served(&database, 50), legacy(&database, 50));
        bump(&mut database, "fill-001", 501);
        bump(&mut database, "fill-002", 502);
        assert_eq!(state_of(&database), DirectoryState::Complete);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        assert_eq!(served(&database, 128), legacy(&database, 128));
    }

    #[test]
    fn count_mismatch_falls_back_to_the_legacy_answer() {
        let mut database = authority();
        for index in 0..4_u64 {
            create(&mut database, &format!("conv-{index}"), 10 + index);
        }
        assert_eq!(state_of(&database), DirectoryState::Complete);
        // A count-key write that bypasses the maintenance funnels desyncs the
        // directory's recorded live count: lists must take the legacy path.
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let count_key = super::super::key(
            TENANT,
            OWNER,
            CONVERSATION_COUNT_NAMESPACE,
            COUNT_KEY,
        )
        .unwrap();
        database.entity_put(&mut tx, count_key, 99_u64.to_le_bytes().to_vec()).unwrap();
        database.commit(tx).unwrap();
        assert_eq!(served(&database, 50), legacy(&database, 50));
    }

    #[test]
    fn deep_owner_falls_back_beyond_the_directory_tail() {
        let mut database = authority();
        for index in 0..130_u64 {
            create(&mut database, &format!("conv-{index:03}"), 1_000 + index);
        }
        assert_eq!(state_of(&database), DirectoryState::Complete);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        assert_eq!(served(&database, 128), legacy(&database, 128));
        // limit beyond the directory cap falls back, still exact.
        assert_eq!(served(&database, 130), legacy(&database, 130));
    }

    #[test]
    fn purge_removes_the_entry_permanently() {
        let mut database = authority();
        for index in 0..4_u64 {
            create(&mut database, &format!("conv-{index}"), 10 + index);
        }
        remove_conversation(&mut database, "conv-1", 100);
        assert_eq!(served(&database, 50), legacy(&database, 50));
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        super::super::purge(
            &mut database,
            &mut tx,
            &super::super::PurgeRequest {
                conversation_id: "conv-1".to_owned(),
                purged_at_ms: 200,
            },
        )
        .unwrap();
        database.commit(tx).unwrap();
        assert_eq!(served(&database, 50), legacy(&database, 50));
        // A purged id can be recreated and re-enters the directory once.
        create(&mut database, "conv-1", 300);
        assert_eq!(served(&database, 50), legacy(&database, 50));
    }

    fn create_with_settings(database: &mut AuthorityDatabase, id: &str, at: u64, settings: Value) {
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let mut request = make_create(id, at);
        request.settings_json = serde_json::to_vec(&settings).unwrap();
        super::super::create(database, &mut tx, &request).unwrap();
        database.commit(tx).unwrap();
    }

    fn catalog_request(
        limit: usize,
        cursor: Option<(u64, &str)>,
        settings_keys: Option<Vec<String>>,
    ) -> super::super::CatalogPageRequest {
        super::super::CatalogPageRequest {
            folder_id: None,
            before_updated_at_ms: cursor.map(|(at, _)| at),
            before_id: cursor.map_or_else(String::new, |(_, id)| id.to_owned()),
            limit,
            settings_keys,
        }
    }

    const ALL_LIMITS: [usize; 5] = [0, 1, 3, 50, MAX_RECENCY_DIRECTORY_ENTRIES];

    fn all_key_variants() -> [Option<Vec<String>>; 3] {
        [None, Some(Vec::new()), Some(vec!["model".to_owned(), "missing".to_owned()])]
    }

    /// (directory answer if it served the page, public answer, legacy answer)
    fn catalog_answers(
        database: &AuthorityDatabase,
        request: &super::super::CatalogPageRequest,
    ) -> (Option<Vec<u8>>, Vec<u8>, Vec<u8>) {
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        let directory = super::super::directory_catalog_page(database, &mut tx, request).unwrap();
        let public = super::super::catalog_page(database, &mut tx, request, &mut || false).unwrap();
        let legacy =
            super::super::document_catalog_page(database, &mut tx, request, &mut || false).unwrap();
        (directory, public, legacy)
    }

    /// Every first page, cursor page (existing and synthetic cursor values)
    /// and settings projection must equal the legacy read-all+sort answer
    /// byte for byte; pages wholly inside the window must not need it.
    fn assert_catalog_pages_match_legacy(
        database: &AuthorityDatabase,
        live: usize,
        sampled: &dyn Fn(usize) -> bool,
        limits: &[usize],
        key_variants: &[Option<Vec<String>>],
    ) {
        let (_, _, full) = catalog_answers(database, &catalog_request(live + 1, None, None));
        let full: Value = serde_json::from_slice(&full).unwrap();
        let order: Vec<(u64, String)> = full["items"].as_array().unwrap().iter().map(|item| (
            item["metadata"]["updated_at"].as_u64().unwrap(),
            item["metadata"]["id"].as_str().unwrap().to_owned(),
        )).collect();
        assert_eq!(order.len(), live);
        let mut cursors: Vec<Option<(u64, String)>> = vec![None];
        for (at, id) in order.iter().enumerate().filter(|(index, _)| sampled(*index)).map(|(_, row)| row) {
            cursors.extend([Some((*at, id.clone())), Some((*at, String::new())),
                Some((*at + 1, String::new())), Some((*at, format!("{id}~")))]);
        }
        let window = live.min(MAX_RECENCY_DIRECTORY_ENTRIES);
        let truncated = live > window;
        let mut served_pages = 0;
        for cursor in &cursors {
            let start = cursor.as_ref().map_or(0, |(at, id)| order.iter()
                .take_while(|(row_at, row_id)| row_at > at || row_at == at && row_id >= id).count());
            for &limit in limits {
                for keys in key_variants {
                    let request = catalog_request(limit, cursor.as_ref().map(|(at, id)| (*at, id.as_str())), keys.clone());
                    let (directory, public, legacy) = catalog_answers(database, &request);
                    assert_eq!(String::from_utf8(public).unwrap(), String::from_utf8(legacy.clone()).unwrap(),
                        "cursor={cursor:?} limit={limit}");
                    let inside_window = !(truncated && start >= window)
                        && start + (live - start).min(limit) <= window;
                    assert_eq!(directory.is_some(), inside_window, "cursor={cursor:?} limit={limit}");
                    if let Some(directory) = directory {
                        assert_eq!(directory, legacy);
                        served_pages += 1;
                    }
                }
            }
        }
        assert!(served_pages > 0);
    }

    #[test]
    fn catalog_pages_inside_the_directory_window_match_the_legacy_catalog() {
        let mut database = authority();
        for index in 0..12_u64 {
            // Shared timestamps exercise id-descending ties and tie cursors.
            create_with_settings(&mut database, &format!("conv-{index:02}"), 100 + index / 3,
                json!({"model": format!("m{index}"), "folderId": format!("f{}", index % 2)}));
        }
        assert_eq!(state_of(&database), DirectoryState::Complete);
        assert_catalog_pages_match_legacy(&database, 12, &|_| true, &ALL_LIMITS, &all_key_variants());
        bump(&mut database, "conv-03", 500);
        remove_conversation(&mut database, "conv-07", 501);
        assert_catalog_pages_match_legacy(&database, 11, &|_| true, &ALL_LIMITS, &all_key_variants());
        revive(&mut database, "conv-07", 502);
        assert_catalog_pages_match_legacy(&database, 12, &|_| true, &ALL_LIMITS, &all_key_variants());
        // Folder pages count only matching headers and keep the legacy plan.
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        let mut folder = catalog_request(50, None, None);
        folder.folder_id = Some("f1".to_owned());
        assert!(super::super::directory_catalog_page(&database, &mut tx, &folder).unwrap().is_none());
    }

    #[test]
    fn directory_catalog_page_reads_only_its_own_headers() {
        let mut database = authority();
        for index in 0..6_u64 {
            create(&mut database, &format!("conv-{index}"), 100 + index);
        }
        // An uncounted document is visible only to a read of every header.
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let stored = super::super::stored_document_envelope(&database, &mut tx, "conv-0").unwrap();
        let ghost = super::super::document_key(&tx, "conv-ghost").unwrap();
        database.entity_put(&mut tx, ghost, stored).unwrap();
        database.commit(tx).unwrap();
        let request = catalog_request(3, None, None);
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        assert!(super::super::document_catalog_page(&database, &mut tx, &request, &mut || false)
            .is_err());
        let page: Value = serde_json::from_slice(
            &super::super::catalog_page(&database, &mut tx, &request, &mut || false).unwrap())
            .unwrap();
        assert_eq!(page["total_count"], 6);
        assert_eq!(page["has_more"], true);
        let ids: Vec<_> = page["items"].as_array().unwrap().iter()
            .map(|item| item["metadata"]["id"].as_str().unwrap().to_owned()).collect();
        assert_eq!(ids, ["conv-5", "conv-4", "conv-3"]);
    }

    #[test]
    fn catalog_pages_beyond_a_truncated_directory_fall_back_exactly() {
        let mut database = authority();
        let live = MAX_RECENCY_DIRECTORY_ENTRIES + 7;
        for index in 0..live as u64 {
            create_with_settings(&mut database, &format!("deep-{index:03}"), 1_000 + index / 2,
                json!({"model": "m"}));
        }
        assert_eq!(state_of(&database), DirectoryState::Complete);
        // Exhaustive cursors are quadratic; sample both ends and the window edge.
        let window = MAX_RECENCY_DIRECTORY_ENTRIES;
        assert_catalog_pages_match_legacy(&database, live,
            &|index| index < 3 || index + 3 >= live || (window - 53..window + 2).contains(&index),
            &[0, 1, 50], &[None]);
        // A count write outside the maintenance funnels disables the directory.
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let count_key = super::super::key(TENANT, OWNER, CONVERSATION_COUNT_NAMESPACE, COUNT_KEY).unwrap();
        database.entity_put(&mut tx, count_key, (live as u64 + 1).to_le_bytes().to_vec()).unwrap();
        database.commit(tx).unwrap();
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        let request = catalog_request(50, None, None);
        assert!(super::super::directory_catalog_page(&database, &mut tx, &request).unwrap().is_none());
        // The candidate plan defers to the exact document plan, which still
        // reports the corrupted count.
        assert!(super::super::catalog_page(&database, &mut tx, &request, &mut || false).is_err());
    }

    fn page_ids(page: &[u8]) -> Vec<String> {
        let page: Value = serde_json::from_slice(page).unwrap();
        page["items"].as_array().unwrap().iter()
            .map(|item| item["metadata"]["id"].as_str().unwrap().to_owned()).collect()
    }

    /// Production failure 2026-11-06: an owner crossed the former 1,000-row
    /// cutoff, the sidebar's 500-row page moved onto the rekeyed updated-time
    /// index, and its tombstone history kept every attempt running past its
    /// deadline. Ordered rows and cost must come from stable-keyed catalogs,
    /// so replacing that whole index with a decoy changes no answer.
    #[test]
    fn large_owner_ordered_reads_never_consult_the_rekeyed_updated_index() {
        use crate::generated_tofudb_ir::CONVERSATION_UPDATED_INDEX_NAMESPACE;

        let mut database = authority();
        let live = super::super::INDEX_SCAN_PAGE_ROWS as u64 + 1;
        for index in 0..live {
            create(&mut database, &format!("conv-{index:04}"), 1_000 + index);
        }
        for at in 10_000..10_064_u64 {
            bump(&mut database, "conv-0000", at);
        }
        let first = catalog_request(500, None, Some(Vec::new()));
        let deep = catalog_request(500, Some((1_480, "conv-0480")), None);
        let read = |database: &AuthorityDatabase| {
            let mut tx = database.begin(TENANT, OWNER).unwrap();
            let served = (
                super::super::catalog_page(database, &mut tx, &first, &mut || false).unwrap(),
                super::super::catalog_page(database, &mut tx, &deep, &mut || false).unwrap(),
                super::super::list(database, &mut tx, &list_request(500), &mut || false).unwrap(),
            );
            let exact = (
                super::super::document_catalog_page(database, &mut tx, &first, &mut || false)
                    .unwrap(),
                super::super::document_catalog_page(database, &mut tx, &deep, &mut || false)
                    .unwrap(),
                super::super::document_list(database, &mut tx, &list_request(500), &mut || false)
                    .unwrap(),
            );
            assert_eq!(served, exact);
            served
        };
        let before = read(&database);
        let first_ids = page_ids(&before.0);
        assert_eq!(first_ids.len(), 500);
        assert_eq!(first_ids[0], "conv-0000");
        assert_eq!(first_ids[1], format!("conv-{:04}", live - 1));
        assert_eq!(page_ids(&before.1)[0], "conv-0479");

        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let (mut start, end) =
            EntityKey::prefix_range(TENANT, OWNER, CONVERSATION_UPDATED_INDEX_NAMESPACE, b"")
                .unwrap();
        let mut indexed = Vec::new();
        loop {
            let rows = database.entity_scan(&mut tx, &start, &end, 1_000).unwrap();
            let Some((last, _)) = rows.last() else { break };
            start = super::super::after_index_key(last, TENANT, OWNER,
                CONVERSATION_UPDATED_INDEX_NAMESPACE).unwrap();
            indexed.extend(rows.into_iter().map(|(key, _)| key));
        }
        assert_eq!(indexed.len() as u64, live);
        for key in indexed {
            database.entity_delete(&mut tx, key).unwrap();
        }
        let decoy = super::super::stored_document_envelope(&database, &mut tx, "conv-0001").unwrap();
        let ghost = super::super::updated_index_key(TENANT, OWNER, 20_000, "conv-ghost").unwrap();
        database.entity_put(&mut tx, ghost, decoy).unwrap();
        database.commit(tx).unwrap();
        assert_eq!(read(&database), before);
    }

    #[test]
    fn ordered_reads_past_the_directory_stop_at_the_request_deadline() {
        let mut database = authority();
        let live = MAX_RECENCY_DIRECTORY_ENTRIES + 2;
        for index in 0..live as u64 {
            create(&mut database, &format!("conv-{index:03}"), 100 + index);
        }
        let mut tx = database.begin(TENANT, OWNER).unwrap();
        let page = catalog_request(live, None, None);
        assert!(super::super::directory_catalog_page(&database, &mut tx, &page).unwrap().is_none());
        let mut folder = catalog_request(10, None, None);
        folder.folder_id = Some("f".to_owned());
        for request in [&page, &folder] {
            let error = super::super::catalog_page(&database, &mut tx, request, &mut || true)
                .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        }
        let mut titled = list_request(10);
        titled.title_contains = Some("title".to_owned());
        for request in [list_request(live), titled] {
            let error = super::super::list(&database, &mut tx, &request, &mut || true).unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        }
    }

    #[test]
    fn candidate_disagreement_and_legacy_owners_keep_the_exact_document_plan() {
        let mut database = authority();
        let live = MAX_RECENCY_DIRECTORY_ENTRIES + 4;
        for index in 0..live as u64 {
            create_with_settings(&mut database, &format!("conv-{index:03}"), 1_000 + index / 2,
                json!({"model": "m"}));
        }
        bump(&mut database, "conv-010", 5_000);
        remove_conversation(&mut database, "conv-020", 5_001);
        let live = live - 1;
        // Pages past the 128-entry directory window: a whole first page, a
        // cursor page near the tail and an empty page past the last entry.
        let first_page = catalog_request(live, None, Some(vec!["model".to_owned()]));
        let tail_page = catalog_request(50, Some((1_002, "conv-005")), None);
        let empty_page = catalog_request(0, Some((1_000, "conv-001")), None);
        let mut windowed = list_request(live);
        windowed.updated_at_gte = Some(1_010);
        windowed.created_at_lt = Some(1_060);
        let mut picked = list_request(5);
        picked.ids = Some(["conv-003", "conv-010", "conv-099", "conv-missing"]
            .into_iter().map(str::to_owned).collect());
        picked.updated_at_gt = Some(1_000);
        let mut titled = list_request(live);
        titled.title_contains = Some("CONV-1".to_owned());
        let assert_exact = |database: &AuthorityDatabase, pages: &[&super::super::CatalogPageRequest],
                            lists: &[&super::super::ListRequest]| {
            let mut tx = database.begin(TENANT, OWNER).unwrap();
            for request in pages {
                assert!(super::super::directory_catalog_page(database, &mut tx, request)
                    .unwrap().is_none());
                assert_eq!(
                    super::super::catalog_page(database, &mut tx, request, &mut || false).unwrap(),
                    super::super::document_catalog_page(database, &mut tx, request, &mut || false)
                        .unwrap(),
                );
            }
            for request in lists {
                let served =
                    super::super::list(database, &mut tx, request, &mut || false).unwrap();
                assert_eq!(served,
                    super::super::document_list(database, &mut tx, request, &mut || false)
                        .unwrap());
                assert!(!serde_json::from_slice::<Value>(&served).unwrap()
                    .as_array().unwrap().is_empty());
            }
        };
        let all_pages = [&first_page, &tail_page, &empty_page];
        let all_lists = [&list_request(live), &windowed, &picked, &titled];
        assert_exact(&database, &all_pages, &all_lists);

        // A selected candidate whose timestamps disagree with its header is
        // caught at hydration. (Like directory entries, candidates are
        // checked per selected row and by count; an unselected stale row is
        // trusted to the same-transaction maintenance funnels.)
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let candidate = super::super::activity_candidate_key(TENANT, OWNER, 0, "conv-003").unwrap();
        let stale = super::super::encode_activity_candidate("conv-003", 1_001, 999_999).unwrap();
        database.entity_put(&mut tx, candidate, stale).unwrap();
        database.commit(tx).unwrap();
        assert_exact(&database, &[&first_page], &[&list_request(live), &windowed, &picked]);

        // A candidate without a live conversation breaks the count check.
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let ghost = super::super::activity_candidate_key(TENANT, OWNER, 0, "conv-ghost").unwrap();
        let ghost_value = super::super::encode_activity_candidate("conv-ghost", 1, 1).unwrap();
        database.entity_put(&mut tx, ghost, ghost_value).unwrap();
        database.commit(tx).unwrap();
        assert_exact(&database, &all_pages, &all_lists);

        // An owner whose candidate index predates its conversations.
        let mut tx = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let state = super::super::activity_candidate_state_key(&tx).unwrap();
        database.entity_delete(&mut tx, state).unwrap();
        database.commit(tx).unwrap();
        let mut read = database.begin(TENANT, OWNER).unwrap();
        assert!(!super::super::activity_candidate_index_is_complete(&database, &mut read).unwrap());
        drop(read);
        assert_exact(&database, &all_pages, &all_lists);
    }

    // Real-volume component measurement at the legacy primary plan's largest
    // owner. Hot caches are disabled, so every header read reaches the volume;
    // this is not an application or sidebar end-to-end latency.
    #[test]
    #[ignore = "real-volume measurement; requires an explicit disposable parent and output"]
    fn catalog_page_cold_read_measurement() {
        let parent = std::env::var("TOFU_DB_CATALOG_MEASURE_ROOT").unwrap();
        let output = std::env::var("TOFU_DB_CATALOG_MEASURE_OUTPUT").unwrap();
        let directory = tempfile::tempdir_in(&parent).unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        let owner_conversations = super::super::INDEX_SCAN_PAGE_ROWS as u64;
        for index in 0..owner_conversations {
            create_with_settings(&mut database, &format!("measure-{index:04}"), 10_000 + index,
                json!({"model": "m", "folderId": format!("f{}", index % 7)}));
        }
        assert_eq!(state_of(&database), DirectoryState::Complete);
        database.merge_entity_overlay_fully().unwrap();
        database.checkpoint_wal_window().unwrap();
        database.configure_hot_data_caches(0, 0).unwrap();
        let request = catalog_request(50, None, Some(vec!["model".to_owned(), "folderId".to_owned()]));
        let mut legacy_micros = Vec::new();
        let mut directory_micros = Vec::new();
        for _ in 0..5 {
            let mut tx = database.begin(TENANT, OWNER).unwrap();
            let started = std::time::Instant::now();
            let legacy = super::super::document_catalog_page(&database, &mut tx, &request, &mut || false)
                .unwrap();
            legacy_micros.push(started.elapsed().as_micros() as u64);
            let mut tx = database.begin(TENANT, OWNER).unwrap();
            let started = std::time::Instant::now();
            let served = super::super::catalog_page(&database, &mut tx, &request, &mut || false)
                .unwrap();
            directory_micros.push(started.elapsed().as_micros() as u64);
            assert_eq!(served, legacy);
        }
        let median = |samples: &[u64]| {
            let mut sorted = samples.to_vec();
            sorted.sort_unstable();
            sorted[sorted.len() / 2]
        };
        let report = json!({
            "source_sha256": env!("TOFUDB_SOURCE_SHA256"), "parent": parent,
            "scope": "one owner, primary-plan maximum, limit 50, hot caches disabled; component, not sidebar end-to-end",
            "owner_conversations": owner_conversations,
            "legacy_micros": legacy_micros, "directory_micros": directory_micros,
            "legacy_median_micros": median(&legacy_micros),
            "directory_median_micros": median(&directory_micros),
        });
        std::fs::write(output, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    }

    fn prepared_bump(
        database: &AuthorityDatabase,
        id: &str,
        at: u64,
        blob_header: bool,
    ) -> AuthorityTransaction {
        let replica = database.build_preparation_replica().unwrap();
        let mut tx = replica
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        let (mut header, version) = super::super::load_header(&replica, &mut tx, id)
            .unwrap()
            .unwrap();
        header.insert("updated_at".to_owned(), Value::from(at));
        if blob_header {
            header.insert(
                "settings".to_owned(),
                serde_json::json!({"synthetic": "x".repeat(40 * 1024)}),
            );
        }
        super::super::store_header(&replica, &mut tx, id, header, version, at).unwrap();
        tx
    }

    #[test]
    fn independent_prepared_updates_merge_into_the_current_directory_without_lost_entries() {
        let mut database = authority();
        for index in 0..12 {
            create(&mut database, &format!("prepared-{index:02}"), index + 1);
        }
        let transactions: Vec<_> = (0..12)
            .map(|index| {
                prepared_bump(
                    &database,
                    &format!("prepared-{index:02}"),
                    index + 100,
                    index % 2 == 0,
                )
            })
            .collect();
        for tx in transactions {
            database.commit(tx).unwrap();
        }
        assert_eq!(served(&database, 12), legacy(&database, 12));
        let mut read = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        for index in 0..12 {
            let (header, _) =
                super::super::load_header(&database, &mut read, &format!("prepared-{index:02}"))
                    .unwrap()
                    .unwrap();
            assert_eq!(header["updated_at"], index + 100);
        }
    }

    #[test]
    fn prepared_index_reads_see_own_writes_and_keep_their_conflict_predicate() {
        let mut database = authority();
        create(&mut database, "a", 1);
        create(&mut database, "b", 2);
        let replica = database.build_preparation_replica().unwrap();
        let mut tx = replica
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        let (mut header, version) = super::super::load_header(&replica, &mut tx, "a")
            .unwrap()
            .unwrap();
        header.insert("updated_at".to_owned(), Value::from(100));
        super::super::store_header(&replica, &mut tx, "a", header, version, 100).unwrap();
        let indexed = try_directory_list(&replica, &mut tx, &list_request(2), &mut || false)
            .unwrap()
            .unwrap();
        let exact =
            super::super::document_list(&replica, &mut tx, &list_request(2), &mut || false).unwrap();
        assert_eq!(
            indexed, exact,
            "index reads must include pending business updates"
        );
        drop(replica);
        bump(&mut database, "b", 200);
        assert_eq!(
            database.commit(tx).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(served(&database, 2), legacy(&database, 2));
    }

    #[test]
    fn prepared_same_header_conflict_cannot_publish_a_stale_directory_update() {
        let mut database = authority();
        create(&mut database, "a", 1);
        create(&mut database, "b", 2);
        let first = prepared_bump(&database, "a", 200, false);
        let stale = prepared_bump(&database, "a", 100, false);
        database.commit(first).unwrap();
        let wal_before = database.wal_len().unwrap();
        assert_eq!(
            database.commit(stale).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(database.wal_len().unwrap(), wal_before);
        assert_eq!(served(&database, 2), legacy(&database, 2));
    }

    #[test]
    fn deferred_directory_and_blob_header_remain_atomic_at_every_commit_io_fault() {
        use crate::vfs::{FaultAction, FaultRule, Operation};
        fn setup() -> (
            Arc<DeterministicVfs>,
            AuthorityDatabase,
            AuthorityTransaction,
        ) {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(Path::new("/data")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let mut database =
                AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            create(&mut database, "a", 1);
            create(&mut database, "b", 2);
            let tx = prepared_bump(&database, "a", 100, true);
            (vfs, database, tx)
        }
        let (vfs, mut database, tx) = setup();
        vfs.arm_fault(None).unwrap();
        database.commit(tx).unwrap();
        let trace = vfs.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let (vfs, mut database, tx) = setup();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = database.commit(tx).is_ok();
                let durable = Arc::new(vfs.fork_durable().unwrap());
                let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
                assert_eq!(served(&reopened, 2), legacy(&reopened, 2));
                let mut read = reopened
                    .begin_with_identity_claim_scopes(TENANT, OWNER)
                    .unwrap();
                let (header, _) = super::super::load_header(&reopened, &mut read, "a")
                    .unwrap()
                    .unwrap();
                if acknowledged {
                    assert_eq!(header["updated_at"], 100);
                }
                if header["updated_at"] == 100 {
                    assert_eq!(
                        header["settings"]["synthetic"].as_str().unwrap().len(),
                        40 * 1024
                    );
                }
            }
        }
    }

    fn prepared_create(database: &AuthorityDatabase, ids: &[&str]) -> AuthorityTransaction {
        let replica = database.build_preparation_replica().unwrap();
        let mut tx = replica.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        for (index, id) in ids.iter().enumerate() {
            super::super::create(&replica, &mut tx, &make_create(id, index as u64 + 1)).unwrap();
        }
        tx
    }

    #[test]
    fn independent_prepared_creations_share_count_without_conflict() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let first = prepared_create(&database, &["a"]);
        let second = prepared_create(&database, &["b", "c"]);
        database.commit(first).unwrap();
        database.commit(second).unwrap();
        let mut query = database.begin(TENANT, OWNER).unwrap();
        assert_eq!(owner_live_count(&database, &mut query).unwrap(), 3);
        assert_eq!(served(&database, 10), legacy(&database, 10));
        assert!(super::super::activity_candidate_index_is_complete(&database, &mut query).unwrap());
        drop(query);
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut query = database.begin(TENANT, OWNER).unwrap();
        assert_eq!(owner_live_count(&database, &mut query).unwrap(), 3);
        assert_eq!(served(&database, 10), legacy(&database, 10));
    }

    #[test]
    fn prepared_count_read_observes_own_creation_and_keeps_occ_predicate() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        create(&mut database, "seed", 1);
        let replica = database.build_preparation_replica().unwrap();
        let mut tx = replica.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        super::super::create(&replica, &mut tx, &make_create("a", 2)).unwrap();
        assert_eq!(owner_live_count(&replica, &mut tx).unwrap(), 2);
        create(&mut database, "b", 3);
        assert_eq!(database.commit(tx).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        let mut query = database.begin(TENANT, OWNER).unwrap();
        assert_eq!(owner_live_count(&database, &mut query).unwrap(), 2);
        assert!(super::super::load_header(&database, &mut query, "a").unwrap().is_none());
    }

    #[test]
    fn prepared_create_claim_conflict_does_not_increment_count() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let first = prepared_create(&database, &["a"]);
        let second = prepared_create(&database, &["a"]);
        database.commit(first).unwrap();
        assert_eq!(database.commit(second).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        let mut query = database.begin(TENANT, OWNER).unwrap();
        assert_eq!(owner_live_count(&database, &mut query).unwrap(), 1);
    }

    #[test]
    fn deferred_creation_count_activity_and_header_are_atomic_at_every_io_fault() {
        use crate::vfs::{FaultAction, FaultRule, Operation};
        fn setup() -> (Arc<DeterministicVfs>, AuthorityDatabase, AuthorityTransaction) {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(Path::new("/data")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let tx = prepared_create(&database, &["a", "b"]);
            (vfs, database, tx)
        }
        let (vfs, mut db, tx) = setup();
        vfs.arm_fault(None).unwrap();
        db.commit(tx).unwrap();
        let trace = vfs.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
            if matches!(operation, Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let (vfs, mut db, tx) = setup();
                vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
                let acknowledged = db.commit(tx).is_ok();
                let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), Arc::new(vfs.fork_durable().unwrap())).unwrap();
                let mut query = reopened.begin(TENANT, OWNER).unwrap();
                let count = owner_live_count(&reopened, &mut query).unwrap();
                assert!(count == 0 || count == 2);
                if acknowledged { assert_eq!(count, 2); }
                for id in ["a", "b"] {
                    assert_eq!(super::super::load_header(&reopened, &mut query, id).unwrap().is_some(), count == 2);
                }
                assert_eq!(served(&reopened, 10), legacy(&reopened, 10));
                assert_eq!(super::super::activity_candidate_index_is_complete(&reopened, &mut query).unwrap(), count == 2);
            }
        }
    }

}
