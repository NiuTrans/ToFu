//! Bounded owner-scoped provider request/response archives.
//!
//! Compressed transport bytes live in immutable owner-bound blob documents;
//! compact covering indexes serve metadata lists without hydrating bodies.
//! Tenant-global identity and usage witnesses serialize quota admission while
//! owner/Attempt counters keep deletion and retry work bounded.
//!
//! Archives are reconstructible diagnostic evidence, not durable user state: a
//! live put that would meet the owner or conversation ceiling, its Attempt's
//! byte budget or the tenant byte budget first reclaims the oldest archives
//! (`reclaim_for_put`: the Attempt's own by its index, the owner's by the owner
//! age index, and for bytes the owner's oldest bodies by the owner body age
//! index), so capture never stops at a ceiling.

use std::collections::BTreeMap;
use std::io;

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::versioned_document::{self, PutRequest};

use crate::generated_tofudb_ir::{
    MAX_RAW_ARCHIVES_PER_CONVERSATION, MAX_RAW_ARCHIVES_PER_OWNER,
    MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT, MAX_RAW_ARCHIVE_AGE_BACKFILL_ROWS_PER_PUT,
    MAX_RAW_ARCHIVE_DECODED_PART_BYTES, MAX_RAW_ARCHIVE_DOCUMENT_BYTES, MAX_RAW_ARCHIVE_LIST_ROWS,
    MAX_RAW_ARCHIVE_READ_CHUNK_BYTES, MAX_RAW_ARCHIVE_STORED_BYTES, MAX_RAW_ARCHIVE_SUMMARY_BYTES,
    MAX_RAW_ARCHIVE_SUMMARY_KEYS, MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES,
    RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE, RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
    RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE, RAW_ARCHIVE_CONVERSATION_STATS_NAMESPACE,
    RAW_ARCHIVE_DOCUMENT_NAMESPACE, RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES,
    RAW_ARCHIVE_ID_CLAIM_NAMESPACE, RAW_ARCHIVE_OWNER_AGE_BACKFILL_NAMESPACE,
    RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE, RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE,
    RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, RAW_ARCHIVE_TASK_INDEX_NAMESPACE,
    RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE, RAW_ARCHIVE_TENANT_USAGE_NAMESPACE,
};

pub(crate) mod migration;

pub(crate) const ID_CLAIM_NAMESPACE: &str = RAW_ARCHIVE_ID_CLAIM_NAMESPACE;
pub(crate) const TENANT_USAGE_NAMESPACE: &str = RAW_ARCHIVE_TENANT_USAGE_NAMESPACE;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}

fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("raw archive index text is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}

fn owner_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn global_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        namespace,
        raw,
    )
}

fn document_key(transaction: &AuthorityTransaction, archive_id: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        RAW_ARCHIVE_DOCUMENT_NAMESPACE,
        archive_id.as_bytes(),
    )
}

fn claim_key(transaction: &AuthorityTransaction, archive_id: &str) -> io::Result<EntityKey> {
    global_key(
        transaction,
        RAW_ARCHIVE_ID_CLAIM_NAMESPACE,
        archive_id.as_bytes(),
    )
}

fn counter_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    owner_key(transaction, namespace, raw)
}

fn read_u64(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    label: &str,
) -> io::Result<u64> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(0);
    };
    let bytes: [u8; 8] = raw
        .try_into()
        .map_err(|_| invalid_data(&format!("raw archive {label} is malformed")))?;
    Ok(u64::from_le_bytes(bytes))
}

fn write_u64(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    value: u64,
) -> io::Result<()> {
    database.entity_put(transaction, key, value.to_le_bytes().to_vec())
}

fn conversation_stats_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        RAW_ARCHIVE_CONVERSATION_STATS_NAMESPACE,
        conversation_id.as_bytes(),
    )
}

fn read_conversation_stats(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<(u64, u64)> {
    let Some(raw) = database.entity_get(
        transaction,
        &conversation_stats_key(transaction, conversation_id)?,
    )?
    else {
        return Ok((0, 0));
    };
    if raw.len() != 16 {
        return Err(invalid_data("raw archive conversation stats are malformed"));
    }
    Ok((
        u64::from_le_bytes(raw[..8].try_into().unwrap()),
        u64::from_le_bytes(raw[8..].try_into().unwrap()),
    ))
}

fn write_conversation_stats(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    count: u64,
    stored_bytes: u64,
) -> io::Result<()> {
    let mut raw = Vec::with_capacity(16);
    raw.extend_from_slice(&count.to_le_bytes());
    raw.extend_from_slice(&stored_bytes.to_le_bytes());
    database.entity_put(
        transaction,
        conversation_stats_key(transaction, conversation_id)?,
        raw,
    )
}

#[derive(Clone, Debug)]
// Put owns a bounded blob-routing envelope. Boxing its many small fields would
// add allocations without reducing the separately budgeted body allocations.
#[allow(clippy::large_enum_variant)]
pub enum Request {
    Put {
        archive_id: String,
        conversation_id: String,
        turn_id: String,
        attempt_id: String,
        task_id: String,
        round_num: u64,
        transport_attempt: u64,
        request_blob: Vec<u8>,
        response_blob: Vec<u8>,
        request_bytes: u64,
        response_bytes: u64,
        request_sha256: String,
        response_sha256: String,
        integrity: String,
        truncation_reason: String,
        summary: Map<String, Value>,
        budget_bytes: u64,
        min_free_bytes: u64,
        available_free_bytes: u64,
        created_at_ms: u64,
    },
    List {
        task_id: String,
        round_num: Option<u64>,
        limit: usize,
    },
    Read {
        task_id: String,
        archive_id: String,
        part: String,
        offset: usize,
        limit: usize,
    },
}

impl Request {
    pub fn mutates_state(&self) -> bool {
        matches!(self, Self::Put { .. })
    }

    pub fn validate(&self) -> io::Result<usize> {
        match self {
            Self::Put {
                archive_id,
                conversation_id,
                turn_id,
                attempt_id,
                task_id,
                round_num,
                transport_attempt,
                request_blob,
                response_blob,
                request_bytes,
                response_bytes,
                request_sha256,
                response_sha256,
                integrity,
                truncation_reason,
                summary,
                budget_bytes,
                min_free_bytes,
                available_free_bytes,
                created_at_ms,
            } => {
                validate_text(archive_id, 160)?;
                validate_text(conversation_id, 128)?;
                validate_text(turn_id, 128)?;
                validate_text(attempt_id, 128)?;
                validate_text(task_id, 256)?;
                if !(1..=1_000_000).contains(round_num)
                    || *transport_attempt > 10_000
                    || *request_bytes > 1_000_000_000
                    || *response_bytes > 1_000_000_000
                    || *budget_bytes == 0
                    || *budget_bytes > MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES
                    || *min_free_bytes > RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES
                    || *available_free_bytes > RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES
                    || *created_at_ms == 0
                {
                    return Err(invalid_input("invalid raw archive numeric bound"));
                }
                validate_digest(request_sha256)?;
                validate_digest(response_sha256)?;
                if !matches!(integrity.as_str(), "complete" | "partial")
                    || !matches!(
                        truncation_reason.as_str(),
                        "" | "attempt_limit"
                            | "quota_exhausted"
                            | "secret_scrubbed"
                            | "transport_interrupted"
                    )
                {
                    return Err(invalid_input("invalid raw archive state"));
                }
                let stored = request_blob
                    .len()
                    .checked_add(response_blob.len())
                    .ok_or_else(|| {
                        crate::resource_exhaustion::accounting_overflow(
                            "raw archive byte count overflow",
                        )
                    })?;
                if request_blob.len() > MAX_RAW_ARCHIVE_STORED_BYTES
                    || response_blob.len() > MAX_RAW_ARCHIVE_STORED_BYTES
                    || stored > MAX_RAW_ARCHIVE_STORED_BYTES
                    || summary.len() > MAX_RAW_ARCHIVE_SUMMARY_KEYS
                    || serde_json::to_vec(summary)
                        .map_err(|_| invalid_input("raw archive summary cannot be encoded"))?
                        .len()
                        > MAX_RAW_ARCHIVE_SUMMARY_BYTES
                {
                    return Err(invalid_input("raw archive payload exceeds its bound"));
                }
                // The compressed bodies are independently bounded blob inputs.
                // Charging them to the shared 8 MiB Transaction IR literal pool
                // would make the advertised 16 MiB archive limit unreachable.
                [
                    archive_id.len(),
                    conversation_id.len(),
                    turn_id.len(),
                    attempt_id.len(),
                    task_id.len(),
                    request_sha256.len(),
                    response_sha256.len(),
                    integrity.len(),
                    truncation_reason.len(),
                    serde_json::to_vec(summary)
                        .map_err(|_| invalid_input("raw archive summary cannot be encoded"))?
                        .len(),
                ]
                .into_iter()
                .try_fold(0usize, usize::checked_add)
                .ok_or_else(|| {
                    crate::resource_exhaustion::accounting_overflow(
                        "raw archive metadata byte count overflow",
                    )
                })
            }
            Self::List {
                task_id,
                round_num,
                limit,
            } => {
                validate_text(task_id, 256)?;
                if round_num.is_some_and(|value| !(1..=1_000_000).contains(&value))
                    || !(1..=MAX_RAW_ARCHIVE_LIST_ROWS).contains(limit)
                {
                    return Err(invalid_input("invalid raw archive list bound"));
                }
                Ok(task_id.len())
            }
            Self::Read {
                task_id,
                archive_id,
                part,
                offset,
                limit,
            } => {
                validate_text(task_id, 256)?;
                validate_text(archive_id, 160)?;
                if !matches!(part.as_str(), "request" | "response")
                    || *offset > MAX_RAW_ARCHIVE_DECODED_PART_BYTES
                    || !(1..=MAX_RAW_ARCHIVE_READ_CHUNK_BYTES).contains(limit)
                {
                    return Err(invalid_input("invalid raw archive read bound"));
                }
                Ok(task_id.len() + archive_id.len())
            }
        }
    }
}

fn validate_text(value: &str, maximum: usize) -> io::Result<()> {
    if value.is_empty() || value.chars().count() > maximum {
        return Err(invalid_input("invalid raw archive identity"));
    }
    Ok(())
}

fn validate_digest(value: &str) -> io::Result<()> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(invalid_input("invalid raw archive digest"));
    }
    Ok(())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ArchiveDocument {
    archive_id: String,
    conversation_id: String,
    turn_id: String,
    attempt_id: String,
    task_id: String,
    round_num: u64,
    transport_attempt: u64,
    request_blob_b64: String,
    response_blob_b64: String,
    request_bytes: u64,
    response_bytes: u64,
    stored_bytes: u64,
    request_sha256: String,
    response_sha256: String,
    integrity: String,
    truncation_reason: String,
    summary: Map<String, Value>,
    created_at_ms: u64,
}

impl ArchiveDocument {
    fn public_value(&self, replay: Option<bool>) -> Value {
        let mut value = json!({
            "archiveId": self.archive_id,
            "attemptId": self.attempt_id,
            "taskId": self.task_id,
            "turnId": self.turn_id,
            "roundNum": self.round_num,
            "transportAttempt": self.transport_attempt,
            "summary": self.summary.get("text").and_then(Value::as_str).filter(|text| !text.is_empty()).unwrap_or("Provider request/response"),
            "byteCount": self.request_bytes.saturating_add(self.response_bytes),
            "requestBytes": self.request_bytes,
            "responseBytes": self.response_bytes,
            "storedBytes": self.stored_bytes,
            "requestSha256": self.request_sha256,
            "responseSha256": self.response_sha256,
            "sha256": self.summary.get("combinedSha256").and_then(Value::as_str).filter(|digest| !digest.is_empty()).unwrap_or(&self.response_sha256),
            "integrity": self.integrity,
            "truncationReason": self.truncation_reason,
            "requestAvailable": !self.request_blob_b64.is_empty(),
            "responseAvailable": !self.response_blob_b64.is_empty(),
            "createdAt": self.created_at_ms,
            "details": self.summary,
        });
        if let Some(replay) = replay {
            value
                .as_object_mut()
                .unwrap()
                .insert("idempotentReplay".to_owned(), Value::Bool(replay));
        }
        value
    }
}

fn load_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    archive_id: &str,
) -> io::Result<Option<ArchiveDocument>> {
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &document_key(transaction, archive_id)?,
        "raw_archives",
        archive_id,
        transaction.owner_user_id(),
        MAX_RAW_ARCHIVE_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let document: ArchiveDocument = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("raw archive document is malformed"))?;
    if document.archive_id != archive_id
        || document.stored_bytes > MAX_RAW_ARCHIVE_STORED_BYTES as u64
    {
        return Err(invalid_data("raw archive document identity is malformed"));
    }
    Ok(Some(document))
}

fn index_prefix(task_id: &str, round_num: Option<u64>) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    push_text(&mut raw, task_id)?;
    if let Some(round_num) = round_num {
        raw.extend_from_slice(&round_num.to_be_bytes());
    }
    Ok(raw)
}

fn index_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    created_at_ms: u64,
    archive_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = prefix.to_vec();
    raw.extend_from_slice(&created_at_ms.to_be_bytes());
    push_text(&mut raw, archive_id)?;
    owner_key(transaction, namespace, &raw)
}

fn attempt_index_prefix(attempt_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    push_text(&mut raw, attempt_id)?;
    Ok(raw)
}

fn conversation_index_prefix(conversation_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    push_text(&mut raw, conversation_id)?;
    Ok(raw)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct RemovalRecord {
    archive_id: String,
    task_id: String,
    attempt_id: String,
    round_num: u64,
    created_at_ms: u64,
    stored_bytes: u64,
}

/// Removal identity of one archive: the owner age index value and the unit
/// that reclamation, Attempt cleanup and conversation deletion retire.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct ArchiveIdentity {
    archive_id: String,
    conversation_id: String,
    task_id: String,
    attempt_id: String,
    round_num: u64,
    created_at_ms: u64,
    stored_bytes: u64,
}

impl ArchiveIdentity {
    fn from_removal(conversation_id: &str, record: &RemovalRecord) -> Self {
        Self {
            archive_id: record.archive_id.clone(),
            conversation_id: conversation_id.to_owned(),
            task_id: record.task_id.clone(),
            attempt_id: record.attempt_id.clone(),
            round_num: record.round_num,
            created_at_ms: record.created_at_ms,
            stored_bytes: record.stored_bytes,
        }
    }

    fn from_document(document: &ArchiveDocument) -> Self {
        Self {
            archive_id: document.archive_id.clone(),
            conversation_id: document.conversation_id.clone(),
            task_id: document.task_id.clone(),
            attempt_id: document.attempt_id.clone(),
            round_num: document.round_num,
            created_at_ms: document.created_at_ms,
            stored_bytes: document.stored_bytes,
        }
    }

    fn attempt_key(&self, transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
        index_key(
            transaction,
            RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE,
            &attempt_index_prefix(&self.attempt_id)?,
            self.created_at_ms,
            &self.archive_id,
        )
    }

    fn conversation_key(&self, transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
        index_key(
            transaction,
            RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &conversation_index_prefix(&self.conversation_id)?,
            self.created_at_ms,
            &self.archive_id,
        )
    }

    /// Key of this archive in an owner age index: `RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE`
    /// (every archive) or `RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE` (only
    /// archives with stored bytes). Both carry the same keys and values.
    fn owner_index_key(
        &self,
        transaction: &AuthorityTransaction,
        namespace: &str,
    ) -> io::Result<EntityKey> {
        index_key(
            transaction,
            namespace,
            &[],
            self.created_at_ms,
            &self.archive_id,
        )
    }

    fn age_key(&self, transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
        self.owner_index_key(transaction, RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE)
    }

    fn body_age_key(&self, transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
        self.owner_index_key(transaction, RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE)
    }

    fn encode(&self) -> io::Result<Vec<u8>> {
        serde_json::to_vec(self)
            .map_err(|_| invalid_data("raw archive age index cannot be encoded"))
    }
}

/// Splits a conversation or Attempt index key into its prefix text (the
/// conversation or Attempt ID), timestamp and archive identity: the inverse of
/// `index_key` over `conversation_index_prefix` or `attempt_index_prefix`.
fn decode_text_prefixed_index_key(raw: &[u8]) -> io::Result<(&str, u64, &str)> {
    fn text(raw: &[u8], at: usize) -> Option<(&str, usize)> {
        let start = at.checked_add(2)?;
        let length = usize::from(u16::from_be_bytes(raw.get(at..start)?.try_into().ok()?));
        let end = start.checked_add(length)?;
        Some((std::str::from_utf8(raw.get(start..end)?).ok()?, end))
    }
    (|| {
        let (prefix_text, at) = text(raw, 0)?;
        let created_at_ms = u64::from_be_bytes(raw.get(at..at.checked_add(8)?)?.try_into().ok()?);
        let (archive_id, end) = text(raw, at + 8)?;
        (end == raw.len()).then_some((prefix_text, created_at_ms, archive_id))
    })()
    .ok_or_else(|| invalid_data("raw archive index key is malformed"))
}

/// Retires one archive's document, identity claim and all covering indexes
/// (five, plus the body age row of an archive with stored bytes). Callers
/// repair the usage counters of the set they remove.
fn retire_archive(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    identity: &ArchiveIdentity,
) -> io::Result<()> {
    let archive_claim_key = claim_key(transaction, &identity.archive_id)?;
    let claim_owner = database
        .entity_get(transaction, &archive_claim_key)?
        .ok_or_else(|| invalid_data("raw archive identity claim is missing"))?;
    if claim_owner.as_slice() != transaction.owner_user_id().to_be_bytes() {
        return Err(invalid_data("raw archive identity claim owner differs"));
    }
    versioned_document::delete(
        database,
        transaction,
        document_key(transaction, &identity.archive_id)?,
        "raw_archives",
        &identity.archive_id,
        None,
    )?;
    database.entity_delete(transaction, archive_claim_key)?;
    let task_prefix = index_prefix(&identity.task_id, None)?;
    let round_prefix = index_prefix(&identity.task_id, Some(identity.round_num))?;
    for key in [
        index_key(
            transaction,
            RAW_ARCHIVE_TASK_INDEX_NAMESPACE,
            &task_prefix,
            identity.created_at_ms,
            &identity.archive_id,
        )?,
        index_key(
            transaction,
            RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE,
            &round_prefix,
            identity.created_at_ms,
            &identity.archive_id,
        )?,
        identity.attempt_key(transaction)?,
        identity.conversation_key(transaction)?,
        identity.age_key(transaction)?,
    ] {
        database.entity_delete(transaction, key)?;
    }
    if identity.stored_bytes > 0 {
        database.entity_delete(transaction, identity.body_age_key(transaction)?)?;
    }
    Ok(())
}

/// Retires one archive and repairs every counter it was charged to.
fn reclaim_archive(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    identity: &ArchiveIdentity,
) -> io::Result<()> {
    retire_archive(database, transaction, identity)?;
    let owner_count_key = counter_key(transaction, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
    let owner_count = read_u64(database, transaction, &owner_count_key, "owner count")?;
    write_u64(
        database,
        transaction,
        owner_count_key,
        owner_count
            .checked_sub(1)
            .ok_or_else(|| invalid_data("raw archive owner count underflow"))?,
    )?;
    let tenant_key = global_key(transaction, TENANT_USAGE_NAMESPACE, b"bytes")?;
    let tenant_usage = read_u64(database, transaction, &tenant_key, "tenant usage")?;
    write_u64(
        database,
        transaction,
        tenant_key,
        tenant_usage
            .checked_sub(identity.stored_bytes)
            .ok_or_else(|| invalid_data("raw archive tenant usage underflow"))?,
    )?;
    let attempt_key = counter_key(
        transaction,
        RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        identity.attempt_id.as_bytes(),
    )?;
    let attempt_remaining = read_u64(database, transaction, &attempt_key, "Attempt usage")?
        .checked_sub(identity.stored_bytes)
        .ok_or_else(|| invalid_data("raw archive Attempt usage underflow"))?;
    if attempt_remaining == 0 {
        database.entity_delete(transaction, attempt_key)?;
    } else {
        write_u64(database, transaction, attempt_key, attempt_remaining)?;
    }
    let (count, bytes) = read_conversation_stats(database, transaction, &identity.conversation_id)?;
    let next_count = count
        .checked_sub(1)
        .ok_or_else(|| invalid_data("raw archive conversation count underflow"))?;
    let next_bytes = bytes
        .checked_sub(identity.stored_bytes)
        .ok_or_else(|| invalid_data("raw archive conversation usage underflow"))?;
    if next_count == 0 {
        database.entity_delete(
            transaction,
            conversation_stats_key(transaction, &identity.conversation_id)?,
        )?;
    } else {
        write_conversation_stats(
            database,
            transaction,
            &identity.conversation_id,
            next_count,
            next_bytes,
        )?;
    }
    Ok(())
}

const AGE_BACKFILL_CURSOR_KEY: &[u8] = b"cursor";
const BODY_AGE_BACKFILL_CURSOR_KEY: &[u8] = b"bodies";
const AGE_BACKFILL_RESUME_AFTER: u8 = 0;
const AGE_BACKFILL_COMPLETE: u8 = 1;

fn age_backfill_cursor_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        RAW_ARCHIVE_OWNER_AGE_BACKFILL_NAMESPACE,
        AGE_BACKFILL_CURSOR_KEY,
    )
}

fn body_age_backfill_cursor_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        RAW_ARCHIVE_OWNER_AGE_BACKFILL_NAMESPACE,
        BODY_AGE_BACKFILL_CURSOR_KEY,
    )
}

/// Whether every archive of the owner with stored bytes is in the body age
/// index; until then a body written before that index existed may lack its row.
fn owner_body_age_index_complete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<bool> {
    let cursor_key = body_age_backfill_cursor_key(transaction)?;
    Ok(database.entity_get(transaction, &cursor_key)?.as_deref()
        == Some(&[AGE_BACKFILL_COMPLETE][..]))
}

/// Whether every archive of the owner is in the age index; until then an
/// archive written before the index existed may still lack its age row.
fn owner_age_index_complete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<bool> {
    let cursor_key = age_backfill_cursor_key(transaction)?;
    Ok(database.entity_get(transaction, &cursor_key)?.as_deref()
        == Some(&[AGE_BACKFILL_COMPLETE][..]))
}

/// Indexes archives written before the owner age index existed. Each call
/// reads at most `page_rows` conversation removal rows after the persistent
/// owner cursor; rewriting an already indexed row stores the identical value.
fn backfill_owner_age_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    page_rows: usize,
) -> io::Result<()> {
    let cursor_key = age_backfill_cursor_key(transaction)?;
    let (namespace_start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        b"",
    )?;
    let start = match database.entity_get(transaction, &cursor_key)?.as_deref() {
        None => namespace_start,
        Some([AGE_BACKFILL_COMPLETE]) => return Ok(()),
        Some([AGE_BACKFILL_RESUME_AFTER, last @ ..]) if !last.is_empty() => {
            let mut successor = last.to_vec();
            successor.push(0);
            owner_key(
                transaction,
                RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
                &successor,
            )?
        }
        Some(_) => return Err(invalid_data("raw archive age backfill cursor is malformed")),
    };
    let page = database.entity_scan(transaction, &start, &end, page_rows)?;
    for (key, raw) in &page {
        let (conversation_id, created_at_ms, archive_id) =
            decode_text_prefixed_index_key(key.key_bytes())?;
        let record: RemovalRecord = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("raw archive removal index is malformed"))?;
        if record.created_at_ms != created_at_ms || record.archive_id != archive_id {
            return Err(invalid_data("raw archive removal index key differs"));
        }
        let identity = ArchiveIdentity::from_removal(conversation_id, &record);
        database.entity_put(
            transaction,
            identity.age_key(transaction)?,
            identity.encode()?,
        )?;
    }
    let cursor = match page.last() {
        Some((last, _)) if page.len() == page_rows => {
            let mut cursor = vec![AGE_BACKFILL_RESUME_AFTER];
            cursor.extend_from_slice(last.key_bytes());
            cursor
        }
        _ => vec![AGE_BACKFILL_COMPLETE],
    };
    database.entity_put(transaction, cursor_key, cursor)
}

/// Indexes archives with stored bytes written before the body age index
/// existed, walking the complete owner age index oldest first. Each call reads
/// at most `page_rows` age rows after the persistent owner cursor; rewriting an
/// already indexed row stores the identical value.
fn backfill_owner_body_age_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    page_rows: usize,
) -> io::Result<()> {
    let cursor_key = body_age_backfill_cursor_key(transaction)?;
    let (namespace_start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE,
        b"",
    )?;
    let start = match database.entity_get(transaction, &cursor_key)?.as_deref() {
        None => namespace_start,
        Some([AGE_BACKFILL_COMPLETE]) => return Ok(()),
        Some([AGE_BACKFILL_RESUME_AFTER, last @ ..]) if !last.is_empty() => {
            let mut successor = last.to_vec();
            successor.push(0);
            owner_key(
                transaction,
                RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE,
                &successor,
            )?
        }
        Some(_) => {
            return Err(invalid_data(
                "raw archive body age backfill cursor is malformed",
            ))
        }
    };
    let page = database.entity_scan(transaction, &start, &end, page_rows)?;
    for (key, raw) in &page {
        let identity: ArchiveIdentity = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("raw archive age index is malformed"))?;
        if identity.age_key(transaction)? != *key {
            return Err(invalid_data("raw archive age index key differs"));
        }
        if identity.stored_bytes > 0 {
            database.entity_put(
                transaction,
                identity.body_age_key(transaction)?,
                raw.clone(),
            )?;
        }
    }
    let cursor = match page.last() {
        Some((last, _)) if page.len() == page_rows => {
            let mut cursor = vec![AGE_BACKFILL_RESUME_AFTER];
            cursor.extend_from_slice(last.key_bytes());
            cursor
        }
        _ => vec![AGE_BACKFILL_COMPLETE],
    };
    database.entity_put(transaction, cursor_key, cursor)
}

fn owner_ceiling_reached(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<bool> {
    let owner_count_key = counter_key(transaction, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
    Ok(
        read_u64(database, transaction, &owner_count_key, "owner count")?
            >= MAX_RAW_ARCHIVES_PER_OWNER,
    )
}

/// Whether a storable body exceeds the tenant budget while its Attempt still
/// has room; a body its Attempt scrubs anyway never reclaims for bytes.
fn byte_reclaim_needed(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    requested: u64,
    budget_bytes: u64,
) -> io::Result<bool> {
    let attempt_key = counter_key(
        transaction,
        RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        attempt_id.as_bytes(),
    )?;
    if read_u64(database, transaction, &attempt_key, "Attempt usage")?.saturating_add(requested)
        > MAX_RAW_ARCHIVE_STORED_BYTES as u64
    {
        return Ok(false);
    }
    let tenant_key = global_key(transaction, TENANT_USAGE_NAMESPACE, b"bytes")?;
    Ok(
        read_u64(database, transaction, &tenant_key, "tenant usage")?.saturating_add(requested)
            > budget_bytes,
    )
}

/// Retires the putting Attempt's own oldest archives while its stored bytes
/// plus the new body would exceed the per-Attempt budget. Each request of an
/// Attempt carries its predecessors (prior responses reappear in the next
/// request), so the newest archive dominates the Attempt's diagnostic value.
/// Returns the per-put reclamation allowance that is left.
fn reclaim_attempt_for_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    attempt_id: &str,
    requested: u64,
    mut remaining: usize,
) -> io::Result<usize> {
    let usage_key = counter_key(
        transaction,
        RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        attempt_id.as_bytes(),
    )?;
    let mut usage = read_u64(database, transaction, &usage_key, "Attempt usage")?;
    let exceeds =
        |usage: u64| usage.saturating_add(requested) > MAX_RAW_ARCHIVE_STORED_BYTES as u64;
    if remaining == 0 || !exceeds(usage) {
        return Ok(remaining);
    }
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE,
        &attempt_index_prefix(attempt_id)?,
    )?;
    let conversation_prefix = conversation_index_prefix(conversation_id)?;
    for (key, raw) in database.entity_scan(transaction, &start, &end, remaining)? {
        if !exceeds(usage) {
            break;
        }
        let (indexed_attempt, created_at_ms, archive_id) =
            decode_text_prefixed_index_key(key.key_bytes())?;
        if indexed_attempt != attempt_id || raw.as_slice() != archive_id.as_bytes() {
            return Err(invalid_data("raw archive Attempt index key differs"));
        }
        // The put proved this Attempt belongs to `conversation_id`, so its
        // removal row carries the stored bytes every counter was charged.
        let removal_key = index_key(
            transaction,
            RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &conversation_prefix,
            created_at_ms,
            archive_id,
        )?;
        let removal = database
            .entity_get(transaction, &removal_key)?
            .ok_or_else(|| invalid_data("raw archive Attempt index target differs"))?;
        let record: RemovalRecord = serde_json::from_slice(&removal)
            .map_err(|_| invalid_data("raw archive removal index is malformed"))?;
        if record.archive_id != archive_id || record.attempt_id != attempt_id {
            return Err(invalid_data("raw archive Attempt index target differs"));
        }
        reclaim_archive(
            database,
            transaction,
            &ArchiveIdentity::from_removal(conversation_id, &record),
        )?;
        usage = usage
            .checked_sub(record.stored_bytes)
            .ok_or_else(|| invalid_data("raw archive Attempt usage underflow"))?;
        remaining -= 1;
    }
    Ok(remaining)
}

/// Retires the owner's oldest archives in one owner age index namespace while
/// `needed` holds, returning the per-put reclamation allowance that is left.
fn reclaim_owner_oldest(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    mut remaining: usize,
    needed: impl Fn(&AuthorityDatabase, &mut AuthorityTransaction) -> io::Result<bool>,
) -> io::Result<usize> {
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        b"",
    )?;
    let mut oldest = None;
    while remaining > 0 && needed(database, transaction)? {
        if oldest.is_none() {
            oldest = Some(
                database
                    .entity_scan(transaction, &start, &end, remaining)?
                    .into_iter(),
            );
        }
        let Some((key, raw)) = oldest.as_mut().and_then(|candidates| candidates.next()) else {
            break;
        };
        let identity: ArchiveIdentity = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("raw archive age index is malformed"))?;
        if identity.owner_index_key(transaction, namespace)? != key {
            return Err(invalid_data("raw archive age index key differs"));
        }
        let claim = claim_key(transaction, &identity.archive_id)?;
        if database.entity_get(transaction, &claim)?.is_none() {
            // An engine that predates the age index retired this archive and
            // left its row behind (which the body backfill may have copied);
            // the rows alone are stale, not corruption.
            database.entity_delete(transaction, identity.age_key(transaction)?)?;
            if identity.stored_bytes > 0 {
                database.entity_delete(transaction, identity.body_age_key(transaction)?)?;
            }
        } else {
            reclaim_archive(database, transaction, &identity)?;
        }
        remaining -= 1;
    }
    Ok(remaining)
}

/// Makes room for one live put instead of refusing at a count ceiling or
/// scrubbing a body the budgets could hold: that conversation's oldest archives
/// for its ceiling, then that Attempt's oldest for the per-Attempt budget, then
/// the owner's oldest archives for the owner ceiling and the owner's oldest
/// bodies for the byte budget, at most `MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT` in
/// total. Byte reclamation walks the body age index so body-less placeholders,
/// which free nothing, never consume that bound. A body that is scrubbed anyway
/// (free space, parts above the budget, an Attempt still above its budget)
/// never reclaims for bytes. Whatever is still exceeded afterwards keeps the
/// existing refusal or scrub, and the next put continues reclaiming.
// TODO(enterprise): the byte budget is tenant-wide while reclamation only
// retires the putting owner's archives, so another owner's usage can make this
// owner reclaim its own oldest evidence without fitting the new body.
// Multi-user tenants need per-owner byte budgets.
fn reclaim_for_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    attempt_id: &str,
    requested: u64,
    budget_bytes: u64,
    space_exhausted: bool,
) -> io::Result<()> {
    if owner_age_index_complete(database, transaction)? {
        backfill_owner_body_age_index(
            database,
            transaction,
            MAX_RAW_ARCHIVE_AGE_BACKFILL_ROWS_PER_PUT,
        )?;
    } else {
        backfill_owner_age_index(
            database,
            transaction,
            MAX_RAW_ARCHIVE_AGE_BACKFILL_ROWS_PER_PUT,
        )?;
    }
    let mut remaining = MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT;
    let (conversation_count, _) = read_conversation_stats(database, transaction, conversation_id)?;
    let conversation_excess = conversation_count
        .saturating_add(1)
        .saturating_sub(MAX_RAW_ARCHIVES_PER_CONVERSATION as u64);
    if conversation_excess > 0 {
        let (start, end) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &conversation_index_prefix(conversation_id)?,
        )?;
        let limit = usize::try_from(conversation_excess)
            .unwrap_or(usize::MAX)
            .min(remaining);
        for (key, raw) in database.entity_scan(transaction, &start, &end, limit)? {
            let record: RemovalRecord = serde_json::from_slice(&raw)
                .map_err(|_| invalid_data("raw archive removal index is malformed"))?;
            let identity = ArchiveIdentity::from_removal(conversation_id, &record);
            if identity.conversation_key(transaction)? != key {
                return Err(invalid_data("raw archive removal index key differs"));
            }
            reclaim_archive(database, transaction, &identity)?;
            remaining -= 1;
        }
    }
    let body_storable = !space_exhausted && requested <= budget_bytes;
    if body_storable {
        remaining = reclaim_attempt_for_put(
            database,
            transaction,
            conversation_id,
            attempt_id,
            requested,
            remaining,
        )?;
    }
    let remaining = reclaim_owner_oldest(
        database,
        transaction,
        RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE,
        remaining,
        owner_ceiling_reached,
    )?;
    if body_storable {
        // Until its backfill completes the body age index may lack old bodies,
        // so the budget keeps reclaiming by the complete age index.
        let namespace = if owner_body_age_index_complete(database, transaction)? {
            RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE
        } else {
            RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE
        };
        reclaim_owner_oldest(
            database,
            transaction,
            namespace,
            remaining,
            |database, transaction| {
                byte_reclaim_needed(database, transaction, attempt_id, requested, budget_bytes)
            },
        )?;
    }
    Ok(())
}

fn put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Vec<u8>> {
    put_contents(database, transaction, request, false)
}

// Offline restoration preserves detached history and refuses quota overflow;
// ordinary writes retain the active-parent fence, reclaim the oldest archives
// before a ceiling or the budget would refuse them, and keep partial-archive
// behavior for what reclamation cannot cure.
fn put_contents(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
    restoring: bool,
) -> io::Result<Vec<u8>> {
    let Request::Put {
        archive_id,
        conversation_id,
        turn_id,
        attempt_id,
        task_id,
        round_num,
        transport_attempt,
        request_blob,
        response_blob,
        request_bytes,
        response_bytes,
        request_sha256,
        response_sha256,
        integrity,
        truncation_reason,
        summary,
        budget_bytes,
        min_free_bytes,
        available_free_bytes,
        created_at_ms,
    } = request
    else {
        return Err(invalid_input("raw archive request is not a put"));
    };
    if let Some(owner) = database.entity_get(transaction, &claim_key(transaction, archive_id)?)? {
        if owner.as_slice() != transaction.owner_user_id().to_be_bytes() {
            return Err(conflict("raw archive identity belongs to another owner"));
        }
        let existing = load_document(database, transaction, archive_id)?
            .ok_or_else(|| invalid_data("raw archive claim has no document"))?;
        if existing.attempt_id != *attempt_id
            || existing.request_sha256 != *request_sha256
            || existing.response_sha256 != *response_sha256
        {
            return Err(conflict("raw archive identity has conflicting content"));
        }
        return serde_json::to_vec(&existing.public_value(Some(true)))
            .map_err(|_| invalid_data("raw archive response cannot be encoded"));
    }
    if !restoring {
        let attempt =
            crate::turn::attempt_get(database, transaction, attempt_id)?.ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "raw archive owner not found")
            })?;
        let attempt: Value = serde_json::from_slice(&attempt)
            .map_err(|_| invalid_data("raw archive parent Attempt is malformed"))?;
        if attempt.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
            || attempt.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
            || attempt.get("turnId").and_then(Value::as_str) != Some(turn_id)
            || attempt.get("taskId").and_then(Value::as_str) != Some(task_id)
        {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "raw archive owner not found",
            ));
        }
    }
    let requested = u64::try_from(request_blob.len() + response_blob.len()).unwrap();
    let space_exhausted = *available_free_bytes > 0
        && (requested > *available_free_bytes
            || *available_free_bytes - requested < *min_free_bytes);
    if !restoring {
        reclaim_for_put(
            database,
            transaction,
            conversation_id,
            attempt_id,
            requested,
            *budget_bytes,
            space_exhausted,
        )?;
    }
    let owner_count_key = counter_key(transaction, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
    let owner_count = read_u64(database, transaction, &owner_count_key, "owner count")?;
    if owner_count >= MAX_RAW_ARCHIVES_PER_OWNER {
        return Err(crate::resource_exhaustion::raw_archive_owner_capacity(
            "raw archive owner capacity is exhausted",
        ));
    }
    let (conversation_count, conversation_bytes) =
        read_conversation_stats(database, transaction, conversation_id)?;
    if conversation_count >= MAX_RAW_ARCHIVES_PER_CONVERSATION as u64 {
        return Err(
            crate::resource_exhaustion::raw_archive_conversation_capacity(
                "raw archive conversation capacity is exhausted",
            ),
        );
    }
    let tenant_usage_key = global_key(transaction, TENANT_USAGE_NAMESPACE, b"bytes")?;
    let tenant_usage = read_u64(database, transaction, &tenant_usage_key, "tenant usage")?;
    let attempt_usage_key = counter_key(
        transaction,
        RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        attempt_id.as_bytes(),
    )?;
    let attempt_usage = read_u64(database, transaction, &attempt_usage_key, "Attempt usage")?;
    let budget_exhausted = tenant_usage
        .checked_add(requested)
        .is_none_or(|next| next > *budget_bytes);
    let attempt_exhausted = attempt_usage
        .checked_add(requested)
        .is_none_or(|next| next > MAX_RAW_ARCHIVE_STORED_BYTES as u64);
    if restoring && (budget_exhausted || space_exhausted || attempt_exhausted) {
        return Err(crate::resource_exhaustion::raw_archive_restore_budget(
            "raw archive snapshot exceeds retained-byte budget",
        ));
    }
    // Name the constraint that bound. Free space and parts above the whole
    // budget scrub whatever the Attempt holds; otherwise an Attempt still above
    // its budget also stopped byte reclamation, so it, not the tenant budget,
    // is why the body is gone.
    let scrub_reason = if space_exhausted || requested > *budget_bytes {
        Some("quota_exhausted")
    } else if attempt_exhausted {
        Some("attempt_limit")
    } else if budget_exhausted {
        Some("quota_exhausted")
    } else {
        None
    };
    let (stored_request, stored_response, stored_bytes, final_integrity, final_reason) =
        if let Some(reason) = scrub_reason {
            (&[][..], &[][..], 0, "partial", reason)
        } else {
            (
                request_blob.as_slice(),
                response_blob.as_slice(),
                requested,
                integrity.as_str(),
                truncation_reason.as_str(),
            )
        };
    let document = ArchiveDocument {
        archive_id: archive_id.clone(),
        conversation_id: conversation_id.clone(),
        turn_id: turn_id.clone(),
        attempt_id: attempt_id.clone(),
        task_id: task_id.clone(),
        round_num: *round_num,
        transport_attempt: *transport_attempt,
        request_blob_b64: base64::engine::general_purpose::STANDARD.encode(stored_request),
        response_blob_b64: base64::engine::general_purpose::STANDARD.encode(stored_response),
        request_bytes: *request_bytes,
        response_bytes: *response_bytes,
        stored_bytes,
        request_sha256: request_sha256.clone(),
        response_sha256: response_sha256.clone(),
        integrity: final_integrity.to_owned(),
        truncation_reason: final_reason.to_owned(),
        summary: summary.clone(),
        created_at_ms: *created_at_ms,
    };
    let raw = serde_json::to_vec(&document)
        .map_err(|_| invalid_data("raw archive document cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, archive_id)?,
            namespace: "raw_archives".to_owned(),
            logical_key: archive_id.clone(),
            value_json: raw,
            expected_version: Some(0),
            updated_at_ms: *created_at_ms,
        },
        transaction.owner_user_id(),
        MAX_RAW_ARCHIVE_DOCUMENT_BYTES,
    )?;
    let metadata = serde_json::to_vec(&document.public_value(None))
        .map_err(|_| invalid_data("raw archive metadata cannot be encoded"))?;
    let task_prefix = index_prefix(task_id, None)?;
    let round_prefix = index_prefix(task_id, Some(*round_num))?;
    let attempt_prefix = attempt_index_prefix(attempt_id)?;
    database.entity_put(
        transaction,
        index_key(
            transaction,
            RAW_ARCHIVE_TASK_INDEX_NAMESPACE,
            &task_prefix,
            *created_at_ms,
            archive_id,
        )?,
        metadata.clone(),
    )?;
    database.entity_put(
        transaction,
        index_key(
            transaction,
            RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE,
            &round_prefix,
            *created_at_ms,
            archive_id,
        )?,
        metadata,
    )?;
    database.entity_put(
        transaction,
        index_key(
            transaction,
            RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE,
            &attempt_prefix,
            *created_at_ms,
            archive_id,
        )?,
        archive_id.as_bytes().to_vec(),
    )?;
    let conversation_prefix = conversation_index_prefix(conversation_id)?;
    database.entity_put(
        transaction,
        index_key(
            transaction,
            RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &conversation_prefix,
            *created_at_ms,
            archive_id,
        )?,
        serde_json::to_vec(&RemovalRecord {
            archive_id: archive_id.clone(),
            task_id: task_id.clone(),
            attempt_id: attempt_id.clone(),
            round_num: *round_num,
            created_at_ms: *created_at_ms,
            stored_bytes,
        })
        .map_err(|_| invalid_data("raw archive removal index cannot be encoded"))?,
    )?;
    let identity = ArchiveIdentity::from_document(&document);
    let encoded_identity = identity.encode()?;
    if stored_bytes > 0 {
        database.entity_put(
            transaction,
            identity.body_age_key(transaction)?,
            encoded_identity.clone(),
        )?;
    }
    database.entity_put(
        transaction,
        identity.age_key(transaction)?,
        encoded_identity,
    )?;
    if owner_count == 0 {
        // No older archive exists, so none can be missing from either index.
        for cursor_key in [
            age_backfill_cursor_key(transaction)?,
            body_age_backfill_cursor_key(transaction)?,
        ] {
            database.entity_put(transaction, cursor_key, vec![AGE_BACKFILL_COMPLETE])?;
        }
    }
    database.entity_put(
        transaction,
        claim_key(transaction, archive_id)?,
        transaction.owner_user_id().to_be_bytes().to_vec(),
    )?;
    write_u64(database, transaction, owner_count_key, owner_count + 1)?;
    write_u64(
        database,
        transaction,
        tenant_usage_key,
        tenant_usage + stored_bytes,
    )?;
    write_u64(
        database,
        transaction,
        attempt_usage_key,
        attempt_usage + stored_bytes,
    )?;
    write_conversation_stats(
        database,
        transaction,
        conversation_id,
        conversation_count + 1,
        conversation_bytes
            .checked_add(stored_bytes)
            .ok_or_else(|| invalid_data("raw archive conversation usage overflow"))?,
    )?;
    serde_json::to_vec(&document.public_value(Some(false)))
        .map_err(|_| invalid_data("raw archive response cannot be encoded"))
}

fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    round_num: Option<u64>,
    limit: usize,
) -> io::Result<Vec<u8>> {
    let namespace = if round_num.is_some() {
        RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE
    } else {
        RAW_ARCHIVE_TASK_INDEX_NAMESPACE
    };
    let prefix = index_prefix(task_id, round_num)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &prefix,
    )?;
    let rows = database.entity_scan(transaction, &start, &end, limit)?;
    let archives = rows
        .into_iter()
        .map(|(_, raw)| {
            serde_json::from_slice::<Value>(&raw)
                .map_err(|_| invalid_data("raw archive metadata index is malformed"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    serde_json::to_vec(&json!({"archives": archives}))
        .map_err(|_| invalid_data("raw archive list cannot be encoded"))
}

pub(crate) fn delete_conversation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    let (expected_count, expected_bytes) =
        read_conversation_stats(database, transaction, conversation_id)?;
    if expected_count == 0 {
        return Ok(());
    }
    if expected_count > MAX_RAW_ARCHIVES_PER_CONVERSATION as u64 {
        return Err(invalid_data(
            "raw archive conversation count exceeds its bound",
        ));
    }
    let prefix = conversation_index_prefix(conversation_id)?;
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut indexed = Vec::new();
    while indexed.len() < MAX_RAW_ARCHIVES_PER_CONVERSATION + 1 {
        let limit = (MAX_RAW_ARCHIVES_PER_CONVERSATION + 1 - indexed.len())
            .min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, limit)?;
        if page.is_empty() {
            break;
        }
        let mut successor = page.last().unwrap().0.key_bytes().to_vec();
        successor.push(0);
        cursor = owner_key(
            transaction,
            RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &successor,
        )?;
        indexed.extend(page);
    }
    if indexed.len() != expected_count as usize {
        return Err(invalid_data("raw archive conversation index count differs"));
    }
    let mut identities = Vec::with_capacity(indexed.len());
    let mut observed_bytes = 0_u64;
    for (key, raw) in &indexed {
        let record: RemovalRecord = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("raw archive removal index is malformed"))?;
        observed_bytes = observed_bytes
            .checked_add(record.stored_bytes)
            .ok_or_else(|| invalid_data("raw archive removal bytes overflow"))?;
        let identity = ArchiveIdentity::from_removal(conversation_id, &record);
        if identity.conversation_key(transaction)? != *key {
            return Err(invalid_data("raw archive removal index key differs"));
        }
        identities.push(identity);
    }
    if observed_bytes != expected_bytes {
        return Err(invalid_data("raw archive conversation byte count differs"));
    }
    let mut removed_by_attempt = BTreeMap::<String, u64>::new();
    for identity in &identities {
        retire_archive(database, transaction, identity)?;
        let removed = removed_by_attempt
            .entry(identity.attempt_id.clone())
            .or_default();
        *removed = removed
            .checked_add(identity.stored_bytes)
            .ok_or_else(|| invalid_data("raw archive Attempt removal bytes overflow"))?;
    }
    for (attempt_id, removed) in removed_by_attempt {
        let key = counter_key(
            transaction,
            RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
            attempt_id.as_bytes(),
        )?;
        let current = read_u64(database, transaction, &key, "Attempt usage")?;
        let remaining = current
            .checked_sub(removed)
            .ok_or_else(|| invalid_data("raw archive Attempt usage underflow"))?;
        if remaining == 0 {
            database.entity_delete(transaction, key)?;
        } else {
            write_u64(database, transaction, key, remaining)?;
        }
    }
    let tenant_key = global_key(transaction, RAW_ARCHIVE_TENANT_USAGE_NAMESPACE, b"bytes")?;
    let tenant_usage = read_u64(database, transaction, &tenant_key, "tenant usage")?;
    write_u64(
        database,
        transaction,
        tenant_key,
        tenant_usage
            .checked_sub(expected_bytes)
            .ok_or_else(|| invalid_data("raw archive tenant usage underflow"))?,
    )?;
    let owner_key = counter_key(transaction, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
    let owner_count = read_u64(database, transaction, &owner_key, "owner count")?;
    write_u64(
        database,
        transaction,
        owner_key,
        owner_count
            .checked_sub(expected_count)
            .ok_or_else(|| invalid_data("raw archive owner count underflow"))?,
    )?;
    database.entity_delete(
        transaction,
        conversation_stats_key(transaction, conversation_id)?,
    )?;
    Ok(())
}

/// Removes every raw archive owned by one Attempt, repairing the exact
/// owner, conversation, Attempt, and tenant counters in the same transaction.
/// Returns `Ok(None)` without writing when the Attempt owns more than
/// `maximum_rows` archives so callers can retry with a fresh budget instead
/// of committing a partial removal.
pub(crate) fn delete_attempt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    attempt_id: &str,
    maximum_rows: usize,
) -> io::Result<Option<u64>> {
    let prefix = attempt_index_prefix(attempt_id)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE,
        &prefix,
    )?;
    let indexed = database.entity_scan(transaction, &start, &end, maximum_rows + 1)?;
    if indexed.len() > maximum_rows {
        return Ok(None);
    }
    let mut documents = Vec::with_capacity(indexed.len());
    let mut removed_bytes = 0_u64;
    for (attempt_key, archive_id_bytes) in &indexed {
        let archive_id = std::str::from_utf8(archive_id_bytes)
            .map_err(|_| invalid_data("raw archive Attempt index identity is not UTF-8"))?;
        let document = load_document(database, transaction, archive_id)?
            .ok_or_else(|| invalid_data("raw archive Attempt index has no document"))?;
        if document.attempt_id != attempt_id || document.conversation_id != conversation_id {
            return Err(invalid_data("raw archive Attempt index target differs"));
        }
        removed_bytes = removed_bytes
            .checked_add(document.stored_bytes)
            .ok_or_else(|| invalid_data("raw archive Attempt removal bytes overflow"))?;
        documents.push((attempt_key.clone(), document));
    }
    let removed_count = documents.len() as u64;
    for (attempt_key, document) in documents {
        let identity = ArchiveIdentity::from_document(&document);
        if identity.attempt_key(transaction)? != attempt_key {
            return Err(invalid_data("raw archive Attempt index key differs"));
        }
        retire_archive(database, transaction, &identity)?;
    }
    if removed_count == 0 {
        return Ok(Some(0));
    }
    let attempt_usage_key = counter_key(
        transaction,
        RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        attempt_id.as_bytes(),
    )?;
    let attempt_usage = read_u64(database, transaction, &attempt_usage_key, "Attempt usage")?;
    let attempt_remaining = attempt_usage
        .checked_sub(removed_bytes)
        .ok_or_else(|| invalid_data("raw archive Attempt usage underflow"))?;
    if attempt_remaining == 0 {
        database.entity_delete(transaction, attempt_usage_key)?;
    } else {
        write_u64(database, transaction, attempt_usage_key, attempt_remaining)?;
    }
    let tenant_key = global_key(transaction, RAW_ARCHIVE_TENANT_USAGE_NAMESPACE, b"bytes")?;
    let tenant_usage = read_u64(database, transaction, &tenant_key, "tenant usage")?;
    write_u64(
        database,
        transaction,
        tenant_key,
        tenant_usage
            .checked_sub(removed_bytes)
            .ok_or_else(|| invalid_data("raw archive tenant usage underflow"))?,
    )?;
    let owner_key = counter_key(transaction, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
    let owner_count = read_u64(database, transaction, &owner_key, "owner count")?;
    write_u64(
        database,
        transaction,
        owner_key,
        owner_count
            .checked_sub(removed_count)
            .ok_or_else(|| invalid_data("raw archive owner count underflow"))?,
    )?;
    let (conversation_count, conversation_bytes) =
        read_conversation_stats(database, transaction, conversation_id)?;
    let next_count = conversation_count
        .checked_sub(removed_count)
        .ok_or_else(|| invalid_data("raw archive conversation count underflow"))?;
    let next_bytes = conversation_bytes
        .checked_sub(removed_bytes)
        .ok_or_else(|| invalid_data("raw archive conversation usage underflow"))?;
    if next_count == 0 {
        database.entity_delete(
            transaction,
            conversation_stats_key(transaction, conversation_id)?,
        )?;
    } else {
        write_conversation_stats(
            database,
            transaction,
            conversation_id,
            next_count,
            next_bytes,
        )?;
    }
    Ok(Some(removed_bytes))
}

fn decode_part(encoded: &str) -> io::Result<Vec<u8>> {
    if encoded.is_empty() {
        return Ok(Vec::new());
    }
    let compressed = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|_| invalid_data("raw archive body encoding is malformed"))?;
    if compressed.len() > MAX_RAW_ARCHIVE_STORED_BYTES {
        return Err(invalid_data("raw archive stored body exceeds its bound"));
    }
    // Strict inflate mirroring the legacy zlib.decompressobj contract: the
    // stream must reach its end within the decoded budget (eof), consume every
    // stored byte (no unused_data), and leave no unconsumed tail.
    let mut decompressor = flate2::Decompress::new(true);
    let mut decoded = Vec::new();
    loop {
        let previous_out = decompressor.total_out();
        let previous_in = decompressor.total_in() as usize;
        decoded.reserve((1 << 20).min(MAX_RAW_ARCHIVE_DECODED_PART_BYTES + 1 - decoded.len()));
        let status = decompressor
            .decompress_vec(
                &compressed[previous_in..],
                &mut decoded,
                flate2::FlushDecompress::None,
            )
            .map_err(|_| invalid_data("raw archive body is malformed"))?;
        if decompressor.total_out() as usize > MAX_RAW_ARCHIVE_DECODED_PART_BYTES {
            return Err(invalid_data("raw archive body is malformed"));
        }
        match status {
            flate2::Status::StreamEnd => break,
            _ if decompressor.total_in() as usize == previous_in
                && decompressor.total_out() == previous_out =>
            {
                return Err(invalid_data("raw archive body is malformed"));
            }
            _ => {}
        }
    }
    if decompressor.total_in() as usize != compressed.len() {
        return Err(invalid_data("raw archive body is malformed"));
    }
    Ok(decoded)
}

fn read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    archive_id: &str,
    part: &str,
    offset: usize,
    limit: usize,
) -> io::Result<Vec<u8>> {
    let Some(document) = load_document(database, transaction, archive_id)? else {
        return Ok(b"null".to_vec());
    };
    if document.task_id != task_id {
        return Ok(b"null".to_vec());
    }
    let encoded = if part == "request" {
        &document.request_blob_b64
    } else {
        &document.response_blob_b64
    };
    let raw = decode_part(encoded)?;
    let end = raw.len().min(offset.saturating_add(limit));
    let chunk = if offset < raw.len() {
        &raw[offset..end]
    } else {
        &[]
    };
    serde_json::to_vec(&json!({"archive": document.public_value(None), "part": part, "offset": offset, "nextOffset": end, "hasMore": end < raw.len(), "availableBytes": raw.len(), "dataBase64": base64::engine::general_purpose::STANDARD.encode(chunk)})).map_err(|_| invalid_data("raw archive read cannot be encoded"))
}

pub fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        Request::Put { .. } => put(database, transaction, request).map(Some),
        Request::List {
            task_id,
            round_num,
            limit,
        } => list(database, transaction, task_id, *round_num, *limit).map(Some),
        Request::Read {
            task_id,
            archive_id,
            part,
            offset,
            limit,
        } => read(
            database,
            transaction,
            task_id,
            archive_id,
            part,
            *offset,
            *limit,
        )
        .map(Some),
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use base64::Engine as _;

    use super::{decode_part, ArchiveDocument, Request};
    use crate::generated_tofudb_ir::{
        MAX_RAW_ARCHIVE_DECODED_PART_BYTES, MAX_RAW_ARCHIVE_READ_CHUNK_BYTES,
    };

    fn encoded(raw: &[u8]) -> String {
        let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::fast());
        encoder.write_all(raw).unwrap();
        base64::engine::general_purpose::STANDARD.encode(encoder.finish().unwrap())
    }

    #[test]
    fn decode_part_accepts_a_well_formed_stream() {
        let raw = b"data: first\n\ndata: second\n\n";
        assert_eq!(decode_part(&encoded(raw)).unwrap(), raw);
        assert_eq!(decode_part("").unwrap(), Vec::<u8>::new());
    }

    #[test]
    fn decode_part_preserves_multi_window_provider_bodies() {
        let raw = b"data: a long provider response with exact bytes\n\n".repeat(70_000);
        assert!(raw.len() > 3 * 1024 * 1024);
        assert_eq!(decode_part(&encoded(&raw)).unwrap(), raw);
    }

    #[test]
    fn decode_part_rejects_truncated_streams_like_legacy_eof() {
        let mut compressed = base64::engine::general_purpose::STANDARD
            .decode(encoded(b"provider body"))
            .unwrap();
        compressed.truncate(compressed.len() - 4);
        let mangled = base64::engine::general_purpose::STANDARD.encode(compressed);
        assert!(decode_part(&mangled).is_err());
    }

    #[test]
    fn decode_part_rejects_trailing_garbage_like_legacy_unused_data() {
        let mut compressed = base64::engine::general_purpose::STANDARD
            .decode(encoded(b"provider body"))
            .unwrap();
        compressed.extend_from_slice(b"garbage");
        let mangled = base64::engine::general_purpose::STANDARD.encode(compressed);
        assert!(decode_part(&mangled).is_err());
    }

    #[test]
    fn decode_part_rejects_malformed_base64_and_bodies() {
        assert!(decode_part("!!!not-base64!!!").is_err());
        let not_zlib = base64::engine::general_purpose::STANDARD.encode(b"plain bytes");
        assert!(decode_part(&not_zlib).is_err());
    }

    #[test]
    fn decode_part_enforces_the_decoded_budget() {
        let raw = vec![0u8; MAX_RAW_ARCHIVE_DECODED_PART_BYTES + 1];
        assert!(decode_part(&encoded(&raw)).is_err());
    }

    fn put_request() -> Request {
        Request::Put {
            archive_id: "archive".to_owned(),
            conversation_id: "conversation".to_owned(),
            turn_id: "turn".to_owned(),
            attempt_id: "attempt".to_owned(),
            task_id: "task".to_owned(),
            round_num: 1,
            transport_attempt: 0,
            request_blob: vec![1, 2, 3],
            response_blob: vec![4, 5, 6],
            request_bytes: 3,
            response_bytes: 3,
            request_sha256: "a".repeat(64),
            response_sha256: "b".repeat(64),
            integrity: "complete".to_owned(),
            truncation_reason: "".to_owned(),
            summary: serde_json::Map::new(),
            budget_bytes: 1024,
            min_free_bytes: 0,
            available_free_bytes: 0,
            created_at_ms: 1,
        }
    }

    #[test]
    fn put_validation_rejects_bad_digests_states_and_budgets() {
        assert!(put_request().validate().is_ok());
        let mut bad_digest = put_request();
        let Request::Put { request_sha256, .. } = &mut bad_digest else {
            unreachable!()
        };
        *request_sha256 = "xyz".to_owned();
        assert!(bad_digest.validate().is_err());
        let mut bad_integrity = put_request();
        let Request::Put { integrity, .. } = &mut bad_integrity else {
            unreachable!()
        };
        *integrity = "halfway".to_owned();
        assert!(bad_integrity.validate().is_err());
        let mut zero_budget = put_request();
        let Request::Put { budget_bytes, .. } = &mut zero_budget else {
            unreachable!()
        };
        *budget_bytes = 0;
        assert!(zero_budget.validate().is_err());
        let mut bad_reason = put_request();
        let Request::Put {
            truncation_reason, ..
        } = &mut bad_reason
        else {
            unreachable!()
        };
        *truncation_reason = "mystery".to_owned();
        assert!(bad_reason.validate().is_err());
    }

    #[test]
    fn put_validation_accounts_blob_bodies_outside_the_ir_literal_pool() {
        let mut request = put_request();
        let Request::Put { request_blob, .. } = &mut request else {
            unreachable!()
        };
        *request_blob = vec![0; 9 * 1024 * 1024];
        assert!(request.validate().unwrap() < 8 * 1024 * 1024);
    }

    #[test]
    fn read_validation_rejects_unknown_parts_and_bad_windows() {
        let base = Request::Read {
            task_id: "task".to_owned(),
            archive_id: "archive".to_owned(),
            part: "response".to_owned(),
            offset: 0,
            limit: 16,
        };
        assert!(base.validate().is_ok());
        let mut bad_part = base.clone();
        let Request::Read { part, .. } = &mut bad_part else {
            unreachable!()
        };
        *part = "metadata".to_owned();
        assert!(bad_part.validate().is_err());
        let mut zero_limit = base.clone();
        let Request::Read { limit, .. } = &mut zero_limit else {
            unreachable!()
        };
        *limit = 0;
        assert!(zero_limit.validate().is_err());
        let mut oversized_limit = base;
        let Request::Read { limit, .. } = &mut oversized_limit else {
            unreachable!()
        };
        *limit = MAX_RAW_ARCHIVE_READ_CHUNK_BYTES + 1;
        assert!(oversized_limit.validate().is_err());
    }

    #[test]
    fn public_value_applies_falsy_summary_fallbacks() {
        let document = ArchiveDocument {
            archive_id: "archive".to_owned(),
            conversation_id: "conversation".to_owned(),
            turn_id: "turn".to_owned(),
            attempt_id: "attempt".to_owned(),
            task_id: "task".to_owned(),
            round_num: 1,
            transport_attempt: 0,
            request_blob_b64: String::new(),
            response_blob_b64: encoded(b"body"),
            request_bytes: 0,
            response_bytes: 4,
            stored_bytes: 10,
            request_sha256: "a".repeat(64),
            response_sha256: "b".repeat(64),
            integrity: "complete".to_owned(),
            truncation_reason: "".to_owned(),
            summary: serde_json::Map::new(),
            created_at_ms: 1,
        };
        let value = document.public_value(None);
        assert_eq!(value["summary"], "Provider request/response");
        assert_eq!(value["sha256"], "b".repeat(64));
        assert_eq!(value["requestAvailable"], false);
        assert_eq!(value["responseAvailable"], true);
        let replay = document.public_value(Some(true));
        assert_eq!(replay["idempotentReplay"], true);
    }
    /// Reclamation lifecycle: archives are reconstructible evidence, so the
    /// ceilings and the byte budget retire the oldest instead of refusing.
    mod lifecycle {
        use super::super::*;
        use crate::vfs::{DeterministicVfs, Vfs};
        use std::{path::Path, sync::Arc};

        const BUDGET: u64 = 1024 * 1024;

        const ROOT: &str = "/raw-lifecycle";

        fn database_on(vfs: &Arc<DeterministicVfs>) -> AuthorityDatabase {
            vfs.create_dir(Path::new(ROOT)).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            AuthorityDatabase::initialize_with_vfs(Path::new(ROOT), vfs.clone()).unwrap()
        }

        fn database() -> AuthorityDatabase {
            database_on(&Arc::new(DeterministicVfs::new(None)))
        }

        fn seed_parent(db: &mut AuthorityDatabase, conversation: &str) {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                db,
                &mut tx,
                &crate::conversation_header::CreateRequest {
                    conversation_id: conversation.into(),
                    title: "Conversation".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1,
                    updated_at_ms: 1,
                    committed_at_ms: 1,
                },
            )
            .unwrap();
            db.commit(tx).unwrap();
            crate::turn::attempt_migration::execute(db, 7, 11, "migration.attempt.restore", json!({
                "attempt_id":format!("{conversation}-attempt"),"conversation_id":conversation,
                "turn_id":format!("{conversation}-turn"),"command_id":format!("{conversation}-command"),
                "task_id":format!("{conversation}-task"),"operation":"generate","dispatch_mode":"",
                "queue_id":"","queue_state":"","status":"completed","base_projection_revision":1,
                "resume_anchor":{},"config":{},"error":{},"timing_trace":{},"created_at":1,
                "started_at":2,"settled_at":3,"superseded_at":null
            }))
            .unwrap();
            crate::turn::migration::execute(
                db,
                7,
                11,
                "migration.turn.restore",
                json!({
                    "turn_id":format!("{conversation}-turn"),"conversation_id":conversation,
                    "presentation_id":"","lane_id":"main","parent_turn_id":null,"ordinal":1,
                    "actor":"assistant","kind":"reply","run_id":"","status":"completed",
                    "current_attempt_id":format!("{conversation}-attempt"),"projection":{},
                    "projection_revision":1,"settlement":{},"created_at":1,"updated_at":3
                }),
            )
            .unwrap();
        }

        fn put_request(
            conversation: &str,
            archive: &str,
            created_at_ms: u64,
            body_bytes: usize,
            budget_bytes: u64,
            available_free_bytes: u64,
        ) -> Request {
            Request::Put {
                archive_id: archive.into(),
                conversation_id: conversation.into(),
                turn_id: format!("{conversation}-turn"),
                attempt_id: format!("{conversation}-attempt"),
                task_id: format!("{conversation}-task"),
                round_num: 1,
                transport_attempt: 0,
                request_blob: vec![7; body_bytes],
                response_blob: Vec::new(),
                request_bytes: body_bytes as u64,
                response_bytes: 0,
                request_sha256: "a".repeat(64),
                response_sha256: "b".repeat(64),
                integrity: "complete".into(),
                truncation_reason: String::new(),
                summary: Map::new(),
                budget_bytes,
                min_free_bytes: 0,
                available_free_bytes,
                created_at_ms,
            }
        }

        fn put(
            db: &mut AuthorityDatabase,
            conversation: &str,
            archive: &str,
            created_at_ms: u64,
            body_bytes: usize,
            budget_bytes: u64,
        ) -> Value {
            commit(
                db,
                &put_request(
                    conversation,
                    archive,
                    created_at_ms,
                    body_bytes,
                    budget_bytes,
                    0,
                ),
            )
        }

        fn commit(db: &mut AuthorityDatabase, request: &Request) -> Value {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let response = execute(db, &mut tx, request).unwrap().unwrap();
            db.commit(tx).unwrap();
            serde_json::from_slice(&response).unwrap()
        }

        fn age_order(db: &mut AuthorityDatabase) -> Vec<String> {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let (start, end) =
                EntityKey::prefix_range(7, 11, RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE, b"").unwrap();
            db.entity_scan(&mut tx, &start, &end, 1000)
                .unwrap()
                .into_iter()
                .map(|(_, raw)| {
                    serde_json::from_slice::<ArchiveIdentity>(&raw)
                        .unwrap()
                        .archive_id
                })
                .collect()
        }

        fn body_order(db: &mut AuthorityDatabase) -> Vec<String> {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let (start, end) =
                EntityKey::prefix_range(7, 11, RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE, b"")
                    .unwrap();
            db.entity_scan(&mut tx, &start, &end, 1000)
                .unwrap()
                .into_iter()
                .map(|(_, raw)| {
                    serde_json::from_slice::<ArchiveIdentity>(&raw)
                        .unwrap()
                        .archive_id
                })
                .collect()
        }

        fn retained(db: &mut AuthorityDatabase, archive: &str) -> bool {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let claim = claim_key(&tx, archive).unwrap();
            let claimed = db.entity_get(&mut tx, &claim).unwrap().is_some();
            let stored = load_document(db, &mut tx, archive).unwrap().is_some();
            assert_eq!(claimed, stored, "claim and document of {archive} diverge");
            stored
        }

        fn listed(db: &mut AuthorityDatabase, task: &str) -> Vec<String> {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let request = Request::List {
                task_id: task.into(),
                round_num: Some(1),
                limit: MAX_RAW_ARCHIVE_LIST_ROWS,
            };
            let raw = execute(db, &mut tx, &request).unwrap().unwrap();
            serde_json::from_slice::<Value>(&raw).unwrap()["archives"]
                .as_array()
                .unwrap()
                .iter()
                .map(|row| row["archiveId"].as_str().unwrap().to_owned())
                .collect()
        }

        fn owner_count(db: &mut AuthorityDatabase) -> u64 {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let key = counter_key(&tx, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count").unwrap();
            read_u64(db, &mut tx, &key, "owner count").unwrap()
        }

        fn tenant_usage(db: &mut AuthorityDatabase) -> u64 {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let key = global_key(&tx, TENANT_USAGE_NAMESPACE, b"bytes").unwrap();
            read_u64(db, &mut tx, &key, "tenant usage").unwrap()
        }

        fn conversation(db: &mut AuthorityDatabase, conversation: &str) -> (u64, u64) {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            read_conversation_stats(db, &mut tx, conversation).unwrap()
        }

        /// Stands in for an owner already holding the full ceiling: only the
        /// archives the test wrote exist, so reclamation must find them by age.
        fn fill_owner_ceiling(db: &mut AuthorityDatabase) {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let key = counter_key(&tx, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count").unwrap();
            write_u64(db, &mut tx, key, MAX_RAW_ARCHIVES_PER_OWNER).unwrap();
            db.commit(tx).unwrap();
        }

        fn forget_index(db: &mut AuthorityDatabase, namespace: &str, cursor_key: &[u8]) {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let (start, end) = EntityKey::prefix_range(7, 11, namespace, b"").unwrap();
            for (key, _) in db.entity_scan(&mut tx, &start, &end, 1000).unwrap() {
                db.entity_delete(&mut tx, key).unwrap();
            }
            let cursor =
                owner_key(&tx, RAW_ARCHIVE_OWNER_AGE_BACKFILL_NAMESPACE, cursor_key).unwrap();
            db.entity_delete(&mut tx, cursor).unwrap();
            db.commit(tx).unwrap();
        }

        /// Returns the owner to the state of archives written before the body
        /// age index existed: no body age rows and no body backfill cursor.
        fn forget_body_age_index(db: &mut AuthorityDatabase) {
            forget_index(
                db,
                RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE,
                BODY_AGE_BACKFILL_CURSOR_KEY,
            );
        }

        /// Returns the owner to the state of archives written before the age
        /// index existed: neither owner age index and no backfill cursor.
        fn forget_age_index(db: &mut AuthorityDatabase) {
            forget_body_age_index(db);
            forget_index(
                db,
                RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE,
                AGE_BACKFILL_CURSOR_KEY,
            );
        }

        fn cursor(db: &mut AuthorityDatabase, cursor_key: &[u8]) -> Option<Vec<u8>> {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let cursor =
                owner_key(&tx, RAW_ARCHIVE_OWNER_AGE_BACKFILL_NAMESPACE, cursor_key).unwrap();
            db.entity_get(&mut tx, &cursor).unwrap()
        }

        fn backfill_cursor(db: &mut AuthorityDatabase) -> Option<Vec<u8>> {
            cursor(db, AGE_BACKFILL_CURSOR_KEY)
        }

        fn body_backfill_cursor(db: &mut AuthorityDatabase) -> Option<Vec<u8>> {
            cursor(db, BODY_AGE_BACKFILL_CURSOR_KEY)
        }

        #[test]
        fn owner_ceiling_reclaims_the_oldest_archives_instead_of_refusing() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-a", "a-10", 10, 4, BUDGET);
            put(&mut db, "conv-b", "b-20", 20, 4, BUDGET);
            put(&mut db, "conv-a", "a-30", 30, 4, BUDGET);
            fill_owner_ceiling(&mut db);

            let stored = put(&mut db, "conv-b", "b-40", 40, 4, BUDGET);
            assert_eq!(stored["integrity"], "complete");
            assert_eq!(stored["idempotentReplay"], false);
            assert!(!retained(&mut db, "a-10"));
            assert_eq!(age_order(&mut db), ["b-20", "a-30", "b-40"]);
            assert_eq!(listed(&mut db, "conv-a-task"), ["a-30"]);
            assert_eq!(owner_count(&mut db), MAX_RAW_ARCHIVES_PER_OWNER);
            assert_eq!(conversation(&mut db, "conv-a"), (1, 4));
            assert_eq!(tenant_usage(&mut db), 12);

            // An identical replay is answered from its claim and reclaims nothing.
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let replay = execute(
                &db,
                &mut tx,
                &put_request("conv-b", "b-40", 40, 4, BUDGET, 0),
            )
            .unwrap()
            .unwrap();
            drop(tx);
            assert_eq!(
                serde_json::from_slice::<Value>(&replay).unwrap()["idempotentReplay"],
                true
            );
            assert_eq!(age_order(&mut db), ["b-20", "a-30", "b-40"]);

            put(&mut db, "conv-a", "a-50", 50, 4, BUDGET);
            assert!(!retained(&mut db, "b-20"));
            assert_eq!(age_order(&mut db), ["a-30", "b-40", "a-50"]);
            assert_eq!(conversation(&mut db, "conv-b"), (1, 4));
            assert_eq!(owner_count(&mut db), MAX_RAW_ARCHIVES_PER_OWNER);
        }

        #[test]
        fn conversation_ceiling_reclaims_its_own_oldest_and_stays_atomically_deletable() {
            // Each archive needs two point reads, up to one distinct Attempt
            // counter, and seven deletes plus that counter. Reserve space for
            // outer lifecycle work.
            assert!(u32::try_from(8 * MAX_RAW_ARCHIVES_PER_CONVERSATION + 1024).is_ok());
            let mut db = database();
            seed_parent(&mut db, "conv");
            seed_parent(&mut db, "other");
            // Older than every archive of "conv": its ceiling must not reach it.
            put(&mut db, "other", "other-0", 5, 0, BUDGET);
            // One stored byte each, so every archive also carries a body age row.
            for index in 0..=MAX_RAW_ARCHIVES_PER_CONVERSATION {
                let archive = format!("archive-{index:04}");
                let stored = put(&mut db, "conv", &archive, index as u64 + 10, 1, BUDGET);
                assert_eq!(stored["archiveId"], archive.as_str());
            }
            assert_eq!(
                conversation(&mut db, "conv"),
                (
                    MAX_RAW_ARCHIVES_PER_CONVERSATION as u64,
                    MAX_RAW_ARCHIVES_PER_CONVERSATION as u64
                )
            );
            assert_eq!(
                body_order(&mut db).first().map(String::as_str),
                Some("archive-0001")
            );
            assert!(!retained(&mut db, "archive-0000"));
            assert!(retained(&mut db, "archive-0001"));
            assert!(retained(&mut db, "other-0"));
            assert_eq!(
                owner_count(&mut db),
                MAX_RAW_ARCHIVES_PER_CONVERSATION as u64 + 1
            );

            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            delete_conversation(&db, &mut tx, "conv").unwrap();
            db.commit(tx).unwrap();
            assert_eq!(conversation(&mut db, "conv"), (0, 0));
            assert!(!retained(&mut db, "archive-0001"));
            assert_eq!(owner_count(&mut db), 1);
            assert_eq!(age_order(&mut db), ["other-0"]);
            assert!(body_order(&mut db).is_empty());
            assert_eq!(tenant_usage(&mut db), 0);
        }

        #[test]
        fn byte_budget_reclaims_oldest_bodies_and_scrubs_only_what_it_cannot_cure() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-a", "a-10", 10, 100, 1000);
            put(&mut db, "conv-b", "b-20", 20, 200, 1000);

            let stored = put(&mut db, "conv-a", "a-30", 30, 300, 400);
            assert_eq!(stored["integrity"], "complete");
            assert_eq!(stored["storedBytes"], 300);
            assert!(!retained(&mut db, "a-10") && !retained(&mut db, "b-20"));
            assert_eq!(age_order(&mut db), ["a-30"]);
            assert_eq!(tenant_usage(&mut db), 300);
            assert_eq!(conversation(&mut db, "conv-b"), (0, 0));

            // Parts larger than the whole budget cannot be cured by reclaiming.
            let oversized = put(&mut db, "conv-b", "b-40", 40, 500, 400);
            assert_eq!(oversized["truncationReason"], "quota_exhausted");
            assert_eq!(oversized["storedBytes"], 0);
            // Nor can free-space pressure: retired blobs are not free space yet.
            let space = commit(&mut db, &put_request("conv-b", "b-50", 50, 50, 400, 1));
            assert_eq!(space["truncationReason"], "quota_exhausted");
            assert_eq!(age_order(&mut db), ["a-30", "b-40", "b-50"]);
            assert_eq!(tenant_usage(&mut db), 300);
        }

        #[test]
        fn reclamation_is_bounded_per_put_and_the_next_put_continues() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            for index in 0..70 {
                put(
                    &mut db,
                    "conv-a",
                    &format!("s-{index:02}"),
                    10 + index,
                    1,
                    70,
                );
            }
            let first = put(&mut db, "conv-a", "large-0", 100, 70, 70);
            assert_eq!(first["truncationReason"], "quota_exhausted");
            let order = age_order(&mut db);
            assert_eq!(order.len(), 70 - MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT + 1);
            assert_eq!(
                order[0],
                format!("s-{MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT:02}")
            );

            let second = put(&mut db, "conv-a", "large-1", 101, 70, 70);
            assert_eq!(second["storedBytes"], 70);
            assert_eq!(age_order(&mut db), ["large-0", "large-1"]);
            assert_eq!(tenant_usage(&mut db), 70);
        }

        #[test]
        fn age_rows_left_by_an_engine_without_the_index_are_dropped_not_fatal() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            put(&mut db, "conv-a", "a-10", 10, 4, BUDGET);
            put(&mut db, "conv-a", "a-20", 20, 4, BUDGET);
            let stale = ArchiveIdentity {
                archive_id: "gone-5".into(),
                conversation_id: "conv-a".into(),
                task_id: "conv-a-task".into(),
                attempt_id: "conv-a-attempt".into(),
                round_num: 1,
                created_at_ms: 5,
                stored_bytes: 4,
            };
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let key = stale.age_key(&tx).unwrap();
            db.entity_put(&mut tx, key, stale.encode().unwrap())
                .unwrap();
            db.commit(tx).unwrap();
            fill_owner_ceiling(&mut db);

            put(&mut db, "conv-a", "a-30", 30, 4, BUDGET);
            assert!(!retained(&mut db, "a-10"));
            assert_eq!(age_order(&mut db), ["a-20", "a-30"]);
            assert_eq!(owner_count(&mut db), MAX_RAW_ARCHIVES_PER_OWNER);
            assert_eq!(tenant_usage(&mut db), 8);
        }

        #[test]
        fn legacy_archives_are_backfilled_into_the_age_index_by_a_bounded_cursor() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-b", "b-10", 10, 4, BUDGET);
            put(&mut db, "conv-a", "a-20", 20, 4, BUDGET);
            put(&mut db, "conv-a", "a-30", 30, 4, BUDGET);
            assert_eq!(backfill_cursor(&mut db), Some(vec![AGE_BACKFILL_COMPLETE]));
            forget_age_index(&mut db);
            assert!(age_order(&mut db).is_empty());

            // The cursor walks conversation removal rows: conv-a sorts first.
            for expected in [vec!["a-20", "a-30"], vec!["b-10", "a-20", "a-30"]] {
                let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
                backfill_owner_age_index(&db, &mut tx, 2).unwrap();
                db.commit(tx).unwrap();
                assert_eq!(age_order(&mut db), expected);
            }
            assert_eq!(backfill_cursor(&mut db), Some(vec![AGE_BACKFILL_COMPLETE]));
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            backfill_owner_age_index(&db, &mut tx, 2).unwrap();
            assert_eq!(age_order(&mut db), ["b-10", "a-20", "a-30"]);
        }

        #[test]
        fn a_live_put_backfills_legacy_archives_before_reclaiming_the_oldest() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-b", "b-10", 10, 4, BUDGET);
            put(&mut db, "conv-a", "a-20", 20, 4, BUDGET);
            put(&mut db, "conv-a", "a-30", 30, 4, BUDGET);
            forget_age_index(&mut db);
            fill_owner_ceiling(&mut db);

            put(&mut db, "conv-a", "a-40", 40, 4, BUDGET);
            assert!(!retained(&mut db, "b-10"));
            assert_eq!(age_order(&mut db), ["a-20", "a-30", "a-40"]);
            assert_eq!(conversation(&mut db, "conv-b"), (0, 0));
            assert_eq!(backfill_cursor(&mut db), Some(vec![AGE_BACKFILL_COMPLETE]));
        }

        #[test]
        fn every_reclaiming_put_fault_commits_reclamation_and_insert_together() {
            use crate::vfs::{FaultAction, FaultRule, Operation};
            let seeded = || {
                let vfs = Arc::new(DeterministicVfs::new(None));
                let mut db = database_on(&vfs);
                seed_parent(&mut db, "conv-a");
                seed_parent(&mut db, "conv-b");
                put(&mut db, "conv-b", "b-10", 10, 100, 400);
                put(&mut db, "conv-a", "a-20", 20, 100, 400);
                vfs.arm_fault(None).unwrap();
                (vfs, db)
            };
            // 200 retained plus 300 requested exceeds the budget by b-10 alone.
            let reclaiming = put_request("conv-a", "a-30", 30, 300, 400, 0);
            let observe = |db: &mut AuthorityDatabase| {
                (
                    age_order(db),
                    body_order(db),
                    tenant_usage(db),
                    owner_count(db),
                    conversation(db, "conv-b"),
                    retained(db, "b-10"),
                    retained(db, "a-30"),
                )
            };
            let before = (
                vec!["b-10".to_owned(), "a-20".to_owned()],
                vec!["b-10".to_owned(), "a-20".to_owned()],
                200,
                2,
                (1, 100),
                true,
                false,
            );
            let after = (
                vec!["a-20".to_owned(), "a-30".to_owned()],
                vec!["a-20".to_owned(), "a-30".to_owned()],
                400,
                2,
                (0, 0),
                false,
                true,
            );
            let (vfs, mut baseline) = seeded();
            commit(&mut baseline, &reclaiming);
            let trace = vfs.trace().unwrap();
            drop(baseline);
            vfs.crash().unwrap();
            let mut recovered = AuthorityDatabase::open_with_vfs(Path::new(ROOT), vfs).unwrap();
            assert_eq!(observe(&mut recovered), after);
            let mut cells = Vec::new();
            for (index, operation) in trace.iter().enumerate() {
                let operation_number = index as u64 + 1;
                cells.push((
                    operation_number,
                    FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
                ));
                if *operation == Operation::Write {
                    cells.push((operation_number, FaultAction::ShortWrite(7)));
                }
            }
            assert!(!cells.is_empty());
            for (operation_number, action) in cells {
                let (vfs, mut db) = seeded();
                vfs.arm_fault(Some(FaultRule {
                    operation_number,
                    action,
                }))
                .unwrap();
                if let Ok(mut tx) = db.begin_with_identity_claim_scopes(7, 11) {
                    if execute(&db, &mut tx, &reclaiming).is_ok() {
                        let _ = db.commit(tx);
                    }
                }
                drop(db);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut db = AuthorityDatabase::open_with_vfs(Path::new(ROOT), vfs).unwrap();
                let state = observe(&mut db);
                assert!(
                    state == before || state == after,
                    "{action:?} before operation {operation_number} left {state:?}"
                );
            }
        }

        #[test]
        #[ignore = "measurement"]
        fn measure_reclaiming_put_cost() {
            use crate::vfs::Operation;
            use std::time::Instant;
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut db = database_on(&vfs);
            seed_parent(&mut db, "conv-a");
            let budget = 64 * BUDGET;
            for index in 0..600 {
                put(
                    &mut db,
                    "conv-a",
                    &format!("w-{index:04}"),
                    10 + index,
                    2048,
                    budget,
                );
            }
            let ops = |vfs: &Arc<DeterministicVfs>| {
                let trace = vfs.trace().unwrap();
                let writes = trace.iter().filter(|op| **op == Operation::Write).count();
                (trace.len(), writes)
            };
            vfs.arm_fault(None).unwrap();
            let started = Instant::now();
            for index in 0..300 {
                put(
                    &mut db,
                    "conv-a",
                    &format!("x-{index:04}"),
                    10_000 + index,
                    2048,
                    budget,
                );
            }
            let plain = (started.elapsed() / 300, ops(&vfs));
            fill_owner_ceiling(&mut db);
            vfs.arm_fault(None).unwrap();
            let started = Instant::now();
            for index in 0..300 {
                put(
                    &mut db,
                    "conv-a",
                    &format!("y-{index:04}"),
                    20_000 + index,
                    2048,
                    budget,
                );
            }
            let reclaiming = (started.elapsed() / 300, ops(&vfs));
            forget_age_index(&mut db);
            vfs.arm_fault(None).unwrap();
            let started = Instant::now();
            put(&mut db, "conv-a", "z-0", 30_000, 2048, budget);
            let backfill = (started.elapsed(), ops(&vfs));
            let mut index = 1;
            while backfill_cursor(&mut db) != Some(vec![AGE_BACKFILL_COMPLETE]) {
                put(
                    &mut db,
                    "conv-a",
                    &format!("z-{index}"),
                    30_000 + index,
                    2048,
                    budget,
                );
                index += 1;
            }
            // With the age index complete, the next put backfills bodies.
            forget_body_age_index(&mut db);
            vfs.arm_fault(None).unwrap();
            let started = Instant::now();
            put(&mut db, "conv-a", "z-last", 30_000 + index, 2048, budget);
            let body_backfill = (started.elapsed(), ops(&vfs));
            eprintln!(
                "MEASURE plain/put {plain:?} reclaiming/put {reclaiming:?} first-backfill-put {backfill:?} first-body-backfill-put {body_backfill:?}"
            );
        }

        #[test]
        fn attempt_cleanup_retires_age_rows_with_its_archives() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-a", "a-10", 10, 4, BUDGET);
            put(&mut db, "conv-b", "b-20", 20, 4, BUDGET);
            put(&mut db, "conv-a", "a-30", 30, 4, BUDGET);

            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let removed = delete_attempt(&db, &mut tx, "conv-a", "conv-a-attempt", 64).unwrap();
            db.commit(tx).unwrap();
            assert_eq!(removed, Some(8));
            assert_eq!(age_order(&mut db), ["b-20"]);
            assert_eq!(owner_count(&mut db), 1);
            assert_eq!(tenant_usage(&mut db), 4);
            assert_eq!(conversation(&mut db, "conv-a"), (0, 0));
        }

        fn attempt_usage(db: &mut AuthorityDatabase, attempt: &str) -> u64 {
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let key =
                counter_key(&tx, RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE, attempt.as_bytes()).unwrap();
            read_u64(db, &mut tx, &key, "Attempt usage").unwrap()
        }

        /// Three of these exceed the per-Attempt budget; two fit.
        const THIRD: usize = MAX_RAW_ARCHIVE_STORED_BYTES / 3 + 1;

        #[test]
        fn legacy_bodies_are_backfilled_into_the_body_age_index_from_the_age_index() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-b", "b-10", 10, 4, BUDGET);
            put(&mut db, "conv-a", "p-15", 15, 2, 1);
            put(&mut db, "conv-a", "a-20", 20, 4, BUDGET);
            put(&mut db, "conv-a", "a-30", 30, 4, BUDGET);
            assert_eq!(body_order(&mut db), ["b-10", "a-20", "a-30"]);
            assert_eq!(
                body_backfill_cursor(&mut db),
                Some(vec![AGE_BACKFILL_COMPLETE])
            );
            forget_body_age_index(&mut db);

            // The cursor walks the age index oldest first and skips placeholders.
            for expected in [vec!["b-10"], vec!["b-10", "a-20", "a-30"]] {
                let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
                backfill_owner_body_age_index(&db, &mut tx, 2).unwrap();
                db.commit(tx).unwrap();
                assert_eq!(body_order(&mut db), expected);
                assert_eq!(
                    body_backfill_cursor(&mut db).unwrap()[0],
                    AGE_BACKFILL_RESUME_AFTER
                );
            }
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            backfill_owner_body_age_index(&db, &mut tx, 2).unwrap();
            db.commit(tx).unwrap();
            assert_eq!(
                body_backfill_cursor(&mut db),
                Some(vec![AGE_BACKFILL_COMPLETE])
            );
            assert_eq!(body_order(&mut db), ["b-10", "a-20", "a-30"]);
        }

        #[test]
        fn until_the_body_age_index_is_complete_the_budget_reclaims_by_age() {
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-b", "b-10", 10, 100, 200);
            put(&mut db, "conv-a", "a-20", 20, 100, 200);
            forget_age_index(&mut db);

            // The first put completes the age index, not yet the body index,
            // and reclaims by age.
            let first = put(&mut db, "conv-a", "a-30", 30, 100, 200);
            assert_eq!(first["storedBytes"], 100);
            assert!(!retained(&mut db, "b-10"));
            assert_eq!(backfill_cursor(&mut db), Some(vec![AGE_BACKFILL_COMPLETE]));
            assert_eq!(body_backfill_cursor(&mut db), None);
            assert_eq!(body_order(&mut db), ["a-30"]);

            // The next put backfills the body index and reclaims through it.
            let second = put(&mut db, "conv-a", "a-40", 40, 100, 200);
            assert_eq!(second["storedBytes"], 100);
            assert!(!retained(&mut db, "a-20"));
            assert_eq!(
                body_backfill_cursor(&mut db),
                Some(vec![AGE_BACKFILL_COMPLETE])
            );
            assert_eq!(body_order(&mut db), ["a-30", "a-40"]);
            assert_eq!(age_order(&mut db), ["a-30", "a-40"]);
            assert_eq!(tenant_usage(&mut db), 200);
        }

        #[test]
        fn body_less_placeholders_never_starve_byte_reclamation() {
            // Production 2026-11-07 23:44: a round needing about 230 KB was
            // scrubbed after its put retired 64 of the owner's oldest archives,
            // while no surveyed body was below 36 KB.
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            for index in 0..MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT {
                let placeholder = put(
                    &mut db,
                    "conv-b",
                    &format!("p-{index:02}"),
                    index as u64 + 1,
                    2,
                    1,
                );
                assert_eq!(placeholder["storedBytes"], 0);
            }
            put(&mut db, "conv-b", "b-100", 100, 200, 400);
            put(&mut db, "conv-b", "b-101", 101, 200, 400);

            let stored = put(&mut db, "conv-a", "a-200", 200, 200, 400);
            assert_eq!(stored["truncationReason"], "");
            assert_eq!(stored["storedBytes"], 200);
            assert!(!retained(&mut db, "b-100"));
            // Placeholders hold no bytes; only the count ceilings retire them.
            assert!(retained(&mut db, "p-00"));
            assert_eq!(
                age_order(&mut db).len(),
                MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT + 2
            );
            assert_eq!(body_order(&mut db), ["b-101", "a-200"]);
            assert_eq!(tenant_usage(&mut db), 400);
        }

        #[test]
        fn a_long_attempt_retires_its_own_oldest_archives_to_keep_the_newest() {
            let budget = 4 * MAX_RAW_ARCHIVE_STORED_BYTES as u64;
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            seed_parent(&mut db, "conv-b");
            put(&mut db, "conv-b", "b-05", 5, 4, budget);
            put(&mut db, "conv-a", "a-10", 10, THIRD, budget);
            put(&mut db, "conv-a", "a-20", 20, THIRD, budget);

            let newest = put(&mut db, "conv-a", "a-30", 30, THIRD, budget);
            assert_eq!(newest["integrity"], "complete");
            assert_eq!(newest["truncationReason"], "");
            assert_eq!(newest["storedBytes"], THIRD as u64);
            // Only the Attempt's own oldest goes; the owner's older archive of
            // another Attempt is not needed for the per-Attempt budget.
            assert!(!retained(&mut db, "a-10"));
            assert_eq!(age_order(&mut db), ["b-05", "a-20", "a-30"]);
            assert_eq!(attempt_usage(&mut db, "conv-a-attempt"), 2 * THIRD as u64);
            assert_eq!(conversation(&mut db, "conv-a"), (2, 2 * THIRD as u64));
            assert_eq!(tenant_usage(&mut db), 2 * THIRD as u64 + 4);
            assert_eq!(owner_count(&mut db), 3);
        }

        #[test]
        fn a_long_attempt_under_a_full_budget_keeps_every_newest_round() {
            // Production 2026-11-08: the tenant budget was full and the
            // Attempt reached its budget, so nothing was reclaimed and every
            // later round became a body-less placeholder.
            let budget = 2 * THIRD as u64;
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            put(&mut db, "conv-a", "a-10", 10, THIRD, budget);
            put(&mut db, "conv-a", "a-20", 20, THIRD, budget);
            for (round, created_at_ms) in [("a-30", 30), ("a-40", 40), ("a-50", 50)] {
                let stored = put(&mut db, "conv-a", round, created_at_ms, THIRD, budget);
                assert_eq!(stored["truncationReason"], "", "{round}");
                assert_eq!(stored["storedBytes"], THIRD as u64, "{round}");
            }
            assert_eq!(age_order(&mut db), ["a-40", "a-50"]);
            assert_eq!(tenant_usage(&mut db), budget);
            assert_eq!(attempt_usage(&mut db, "conv-a-attempt"), budget);
        }

        #[test]
        fn a_scrubbed_body_names_the_constraint_that_bound_it() {
            let budget = 2 * THIRD as u64;
            let mut db = database();
            seed_parent(&mut db, "conv-a");
            // Body-less placeholders (parts above the whole budget) fill the
            // Attempt's oldest slots up to the per-put reclamation bound.
            for index in 0..MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT {
                let placeholder = put(
                    &mut db,
                    "conv-a",
                    &format!("p-{index:02}"),
                    index as u64 + 1,
                    2,
                    1,
                );
                assert_eq!(placeholder["truncationReason"], "quota_exhausted");
            }
            put(&mut db, "conv-a", "a-100", 100, THIRD, budget);
            put(&mut db, "conv-a", "a-101", 101, THIRD, budget);

            // Free-space exhaustion scrubs whatever the Attempt holds and
            // reclaims nothing.
            let space = commit(
                &mut db,
                &put_request("conv-a", "a-102", 102, THIRD, budget, 1),
            );
            assert_eq!(space["truncationReason"], "quota_exhausted");
            assert_eq!(
                age_order(&mut db).len(),
                MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT + 3
            );

            // Both the Attempt and the tenant budget are exceeded. Retiring the
            // Attempt's oldest stops at the bound, before any of its bytes, so
            // the Attempt budget is what bound, not the tenant budget.
            let bound = put(&mut db, "conv-a", "a-103", 103, THIRD, budget);
            assert_eq!(bound["integrity"], "partial");
            assert_eq!(bound["truncationReason"], "attempt_limit");
            assert_eq!(bound["storedBytes"], 0);
            assert_eq!(age_order(&mut db), ["a-100", "a-101", "a-102", "a-103"]);

            // The next put continues with the Attempt's oldest body.
            let next = put(&mut db, "conv-a", "a-104", 104, THIRD, budget);
            assert_eq!(next["truncationReason"], "");
            assert_eq!(age_order(&mut db), ["a-101", "a-102", "a-103", "a-104"]);
            assert_eq!(tenant_usage(&mut db), budget);
            assert_eq!(attempt_usage(&mut db, "conv-a-attempt"), budget);
        }
    }
}
