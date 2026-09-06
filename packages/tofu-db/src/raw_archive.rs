//! Bounded owner-scoped provider request/response archives.
//!
//! Compressed transport bytes live in immutable owner-bound blob documents;
//! compact covering indexes serve metadata lists without hydrating bodies.
//! Tenant-global identity and usage witnesses serialize quota admission while
//! owner/Attempt counters keep deletion and retry work bounded.

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
    MAX_RAW_ARCHIVE_DECODED_PART_BYTES, MAX_RAW_ARCHIVE_DOCUMENT_BYTES, MAX_RAW_ARCHIVE_LIST_ROWS,
    MAX_RAW_ARCHIVE_READ_CHUNK_BYTES, MAX_RAW_ARCHIVE_STORED_BYTES, MAX_RAW_ARCHIVE_SUMMARY_BYTES,
    MAX_RAW_ARCHIVE_SUMMARY_KEYS, MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES,
    RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE, RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
    RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE, RAW_ARCHIVE_CONVERSATION_STATS_NAMESPACE,
    RAW_ARCHIVE_DOCUMENT_NAMESPACE, RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES,
    RAW_ARCHIVE_ID_CLAIM_NAMESPACE, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE,
    RAW_ARCHIVE_TASK_INDEX_NAMESPACE, RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE,
    RAW_ARCHIVE_TENANT_USAGE_NAMESPACE,
};

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

fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
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
                    .ok_or_else(|| exhausted("raw archive byte count overflow"))?;
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
                .ok_or_else(|| exhausted("raw archive metadata byte count overflow"))
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

fn put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
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
    let attempt = crate::turn::attempt_get(database, transaction, attempt_id)?
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "raw archive owner not found"))?;
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
    let owner_count_key = counter_key(transaction, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
    let owner_count = read_u64(database, transaction, &owner_count_key, "owner count")?;
    if owner_count >= MAX_RAW_ARCHIVES_PER_OWNER {
        return Err(exhausted("raw archive owner capacity is exhausted"));
    }
    let (conversation_count, conversation_bytes) =
        read_conversation_stats(database, transaction, conversation_id)?;
    if conversation_count >= MAX_RAW_ARCHIVES_PER_CONVERSATION as u64 {
        return Err(exhausted("raw archive conversation capacity is exhausted"));
    }
    let tenant_usage_key = global_key(transaction, TENANT_USAGE_NAMESPACE, b"bytes")?;
    let tenant_usage = read_u64(database, transaction, &tenant_usage_key, "tenant usage")?;
    let attempt_usage_key = counter_key(
        transaction,
        RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        attempt_id.as_bytes(),
    )?;
    let attempt_usage = read_u64(database, transaction, &attempt_usage_key, "Attempt usage")?;
    let requested = u64::try_from(request_blob.len() + response_blob.len()).unwrap();
    let quota = tenant_usage
        .checked_add(requested)
        .is_none_or(|next| next > *budget_bytes)
        || (*available_free_bytes > 0
            && (requested > *available_free_bytes
                || *available_free_bytes - requested < *min_free_bytes));
    let attempt_exhausted = attempt_usage
        .checked_add(requested)
        .is_none_or(|next| next > MAX_RAW_ARCHIVE_STORED_BYTES as u64);
    let (stored_request, stored_response, stored_bytes, final_integrity, final_reason) = if quota {
        (&[][..], &[][..], 0, "partial", "quota_exhausted")
    } else if attempt_exhausted {
        (&[][..], &[][..], 0, "partial", "attempt_limit")
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
    let mut records = Vec::with_capacity(indexed.len());
    let mut observed_bytes = 0_u64;
    for (key, raw) in &indexed {
        let record: RemovalRecord = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("raw archive removal index is malformed"))?;
        observed_bytes = observed_bytes
            .checked_add(record.stored_bytes)
            .ok_or_else(|| invalid_data("raw archive removal bytes overflow"))?;
        records.push((key.clone(), record));
    }
    if observed_bytes != expected_bytes {
        return Err(invalid_data("raw archive conversation byte count differs"));
    }
    let mut removed_by_attempt = BTreeMap::<String, u64>::new();
    for (conversation_key, record) in &records {
        let archive_claim_key = claim_key(transaction, &record.archive_id)?;
        let claim_owner = database
            .entity_get(transaction, &archive_claim_key)?
            .ok_or_else(|| invalid_data("raw archive identity claim is missing"))?;
        if claim_owner.as_slice() != transaction.owner_user_id().to_be_bytes() {
            return Err(invalid_data("raw archive identity claim owner differs"));
        }
        versioned_document::delete(
            database,
            transaction,
            document_key(transaction, &record.archive_id)?,
            "raw_archives",
            &record.archive_id,
            None,
        )?;
        database.entity_delete(transaction, archive_claim_key)?;
        let task_prefix = index_prefix(&record.task_id, None)?;
        let round_prefix = index_prefix(&record.task_id, Some(record.round_num))?;
        let attempt_prefix = attempt_index_prefix(&record.attempt_id)?;
        for key in [
            index_key(
                transaction,
                RAW_ARCHIVE_TASK_INDEX_NAMESPACE,
                &task_prefix,
                record.created_at_ms,
                &record.archive_id,
            )?,
            index_key(
                transaction,
                RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE,
                &round_prefix,
                record.created_at_ms,
                &record.archive_id,
            )?,
            index_key(
                transaction,
                RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE,
                &attempt_prefix,
                record.created_at_ms,
                &record.archive_id,
            )?,
            conversation_key.clone(),
        ] {
            database.entity_delete(transaction, key)?;
        }
        let removed = removed_by_attempt
            .entry(record.attempt_id.clone())
            .or_default();
        *removed = removed
            .checked_add(record.stored_bytes)
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
        decoded.reserve(1 << 20);
        let status = decompressor
            .decompress_vec(&compressed, &mut decoded, flate2::FlushDecompress::None)
            .map_err(|_| invalid_data("raw archive body is malformed"))?;
        if decompressor.total_out() as usize > MAX_RAW_ARCHIVE_DECODED_PART_BYTES {
            return Err(invalid_data("raw archive body is malformed"));
        }
        match status {
            flate2::Status::StreamEnd => break,
            _ if decompressor.total_in() as usize == compressed.len()
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
}
