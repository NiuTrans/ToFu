//! Durable owner-scoped timer definitions and idempotent poll progress.
//!
//! Timer documents are tenant-global only to support the internal active feed;
//! every read verifies the embedded owner and exact ID claim. Owner-local
//! counts/indexes enforce resource limits, while immutable poll rows and their
//! logical poll-ID claims commit with watcher progress in one OCC transaction.

use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_TIMERS_PER_OWNER, MAX_TIMER_DOCUMENT_BYTES,
    MAX_TIMER_GLOBAL_ACTIVE_ROWS, MAX_TIMER_ID_CHARACTERS, MAX_TIMER_LIST_ROWS,
    MAX_TIMER_POLL_DOCUMENT_BYTES, MAX_TIMER_POLL_ROWS, MAX_TRANSACTION_IR_LITERAL_BYTES,
    TIMER_ACTIVE_COUNT_NAMESPACE, TIMER_CONVERSATION_INDEX_NAMESPACE, TIMER_DOCUMENT_NAMESPACE,
    TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE, TIMER_ID_CLAIM_NAMESPACE,
    TIMER_POLL_DOCUMENT_NAMESPACE, TIMER_POLL_ID_CLAIM_NAMESPACE, TIMER_POLL_SEQUENCE_NAMESPACE,
    TIMER_POLL_TIME_INDEX_NAMESPACE, TIMER_STATUS_CREATED_INDEX_NAMESPACE,
    TIMER_TOTAL_COUNT_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "timers";
const COUNT_KEY: &[u8] = b"count";
const POLL_SEQUENCE_KEY: &[u8] = b"sequence";

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

fn owner_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}

fn global_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, raw)
}

fn document_key(tx: &AuthorityTransaction, timer_id: &str) -> io::Result<EntityKey> {
    global_key(tx, TIMER_DOCUMENT_NAMESPACE, timer_id.as_bytes())
}

fn id_claim_key(tx: &AuthorityTransaction, timer_id: &str) -> io::Result<EntityKey> {
    global_key(tx, TIMER_ID_CLAIM_NAMESPACE, timer_id.as_bytes())
}

fn count_key(tx: &AuthorityTransaction, namespace: &str) -> io::Result<EntityKey> {
    owner_key(tx, namespace, COUNT_KEY)
}

fn namespace_range(
    tx: &AuthorityTransaction,
    namespace: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), namespace, b"")
}

fn global_namespace_range(
    tx: &AuthorityTransaction,
    namespace: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, b"")
}

fn descending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[!byte, 0]);
    }
    output.push(u8::MAX);
}

fn ascending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[byte, u8::MAX]);
    }
    output.push(0);
}

fn status_index_key(
    tx: &AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<EntityKey> {
    let status = text_field(document, "status")?;
    let created_at = text_field(document, "created_at")?;
    let timer_id = text_field(document, "id")?;
    let mut raw = Vec::with_capacity((created_at.len() + timer_id.len()) * 2 + 3);
    raw.push(u8::from(status != "active"));
    descending_text(&mut raw, created_at);
    descending_text(&mut raw, timer_id);
    owner_key(tx, TIMER_STATUS_CREATED_INDEX_NAMESPACE, &raw)
}

fn conversation_prefix(conv_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(conv_id.len() + 2);
    raw.extend_from_slice(
        &u16::try_from(conv_id.len())
            .map_err(|_| invalid_input("timer conversation ID is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(conv_id.as_bytes());
    Ok(raw)
}

fn conversation_index_key(
    tx: &AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<EntityKey> {
    let mut raw = conversation_prefix(text_field(document, "conv_id")?)?;
    raw.extend_from_slice(text_field(document, "id")?.as_bytes());
    owner_key(tx, TIMER_CONVERSATION_INDEX_NAMESPACE, &raw)
}

fn global_active_index_key(
    tx: &AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<EntityKey> {
    let mut raw = Vec::new();
    ascending_text(&mut raw, text_field(document, "created_at")?);
    raw.extend_from_slice(&owner_field(document)?.to_be_bytes());
    ascending_text(&mut raw, text_field(document, "id")?);
    global_key(tx, TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE, &raw)
}

fn identity_value(document: &Map<String, Value>) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({
        "owner_user_id": owner_field(document)?,
        "id": text_field(document, "id")?,
    }))
    .map_err(|_| invalid_data("timer identity cannot be encoded"))
}

fn text_field<'a>(document: &'a Map<String, Value>, name: &str) -> io::Result<&'a str> {
    document
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("timer text field is malformed"))
}

fn owner_field(document: &Map<String, Value>) -> io::Result<u64> {
    document
        .get("user_id")
        .and_then(Value::as_u64)
        .filter(|owner| *owner > 0)
        .ok_or_else(|| invalid_data("timer owner is malformed"))
}

fn valid_timer_id(timer_id: &str) -> bool {
    !timer_id.is_empty() && timer_id.chars().count() <= MAX_TIMER_ID_CHARACTERS
}

fn validate_document(
    document: &Map<String, Value>,
    expected_owner: u64,
    timer_id: &str,
) -> io::Result<()> {
    if !valid_timer_id(timer_id)
        || document.get("id").and_then(Value::as_str) != Some(timer_id)
        || owner_field(document)? != expected_owner
        || text_field(document, "conv_id")?.is_empty()
        || text_field(document, "status")?.is_empty()
        || document
            .get("tools_config")
            .is_none_or(|value| !value.is_object())
        || [
            "poll_interval",
            "max_polls",
            "poll_count",
            "promotion_streak",
            "fallback_streak",
        ]
        .iter()
        .any(|field| document.get(*field).and_then(Value::as_i64).is_none())
    {
        return Err(invalid_data("timer document is malformed"));
    }
    let encoded = serde_json::to_vec(document)
        .map_err(|_| invalid_data("timer document cannot be encoded"))?;
    if encoded.len() > MAX_TIMER_DOCUMENT_BYTES {
        return Err(exhausted("timer document exceeds its bound"));
    }
    Ok(())
}

fn read_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    maximum: usize,
) -> io::Result<usize> {
    match database.entity_get(tx, &count_key(tx, namespace)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => {
            usize::try_from(u64::from_le_bytes(raw.try_into().expect("length checked")))
                .ok()
                .filter(|count| *count <= maximum)
                .ok_or_else(|| invalid_data("timer count exceeds its bound"))
        }
        Some(_) => Err(invalid_data("timer count is malformed")),
    }
}

fn write_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    count: usize,
    maximum: usize,
) -> io::Result<()> {
    if count > maximum {
        return Err(conflict("timer count exceeds its capacity"));
    }
    database.entity_put(
        tx,
        count_key(tx, namespace)?,
        u64::try_from(count)
            .map_err(|_| exhausted("timer count overflows"))?
            .to_le_bytes()
            .to_vec(),
    )
}

fn read_document_for_owner(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    timer_id: &str,
    expected_owner: u64,
) -> io::Result<Option<Map<String, Value>>> {
    let claim = database.entity_get(tx, &id_claim_key(tx, timer_id)?)?;
    let Some(claim) = claim else {
        if database
            .entity_get(tx, &document_key(tx, timer_id)?)?
            .is_some()
        {
            return Err(invalid_data("timer document has no ID claim"));
        }
        return Ok(None);
    };
    let claimed_owner = serde_json::from_slice::<Value>(&claim)
        .ok()
        .and_then(|value| value.get("owner_user_id").and_then(Value::as_u64))
        .ok_or_else(|| invalid_data("timer ID claim is malformed"))?;
    if claimed_owner != expected_owner {
        return Ok(None);
    }
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &document_key(tx, timer_id)?,
        LOGICAL_NAMESPACE,
        timer_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_TIMER_DOCUMENT_BYTES,
    )?
    else {
        return Err(invalid_data("timer ID claim target is missing"));
    };
    let document = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("timer document is malformed"))?;
    validate_document(&document, expected_owner, timer_id)?;
    Ok(Some(document))
}

fn read_document(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    timer_id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    read_document_for_owner(database, tx, timer_id, tx.owner_user_id())
}

fn put_document(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    timer_id: &str,
    document: &Map<String, Value>,
    updated_at_ms: u64,
    expected_version: Option<u64>,
) -> io::Result<()> {
    validate_document(document, tx.owner_user_id(), timer_id)?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        PutRequest {
            key: document_key(tx, timer_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: timer_id.to_owned(),
            value_json: serde_json::to_vec(document)
                .map_err(|_| invalid_data("timer document cannot be encoded"))?,
            updated_at_ms,
            expected_version,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_TIMER_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn write_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let identity = identity_value(document)?;
    database.entity_put(tx, status_index_key(tx, document)?, identity.clone())?;
    database.entity_put(tx, conversation_index_key(tx, document)?, identity.clone())?;
    if text_field(document, "status")? == "active" {
        database.entity_put(tx, global_active_index_key(tx, document)?, identity)?;
    }
    Ok(())
}

fn delete_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    database.entity_delete(tx, status_index_key(tx, document)?)?;
    database.entity_delete(tx, conversation_index_key(tx, document)?)?;
    if text_field(document, "status")? == "active" {
        database.entity_delete(tx, global_active_index_key(tx, document)?)?;
    }
    Ok(())
}

fn adjust_active_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    was_active: bool,
    is_active: bool,
) -> io::Result<()> {
    if was_active == is_active {
        return Ok(());
    }
    let current = read_count(
        database,
        tx,
        TIMER_ACTIVE_COUNT_NAMESPACE,
        MAX_TIMERS_PER_OWNER,
    )?;
    let next = if is_active {
        current.checked_add(1)
    } else {
        current.checked_sub(1)
    }
    .ok_or_else(|| invalid_data("timer active count underflow or overflow"))?;
    write_count(
        database,
        tx,
        TIMER_ACTIVE_COUNT_NAMESPACE,
        next,
        MAX_TIMERS_PER_OWNER,
    )
}

fn poll_prefix(conv_id: &str, timer_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(conv_id.len() + timer_id.len() + 4);
    raw.extend_from_slice(
        &u16::try_from(conv_id.len())
            .map_err(|_| invalid_input("timer conversation ID is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(conv_id.as_bytes());
    raw.extend_from_slice(
        &u16::try_from(timer_id.len())
            .map_err(|_| invalid_input("timer ID is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(timer_id.as_bytes());
    Ok(raw)
}

fn poll_document_key(
    tx: &AuthorityTransaction,
    conv_id: &str,
    timer_id: &str,
    poll_number: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_prefix(conv_id, timer_id)?;
    raw.extend_from_slice(&poll_number.to_be_bytes());
    owner_key(tx, TIMER_POLL_DOCUMENT_NAMESPACE, &raw)
}

fn poll_time_key(
    tx: &AuthorityTransaction,
    conv_id: &str,
    timer_id: &str,
    poll_time: &str,
    poll_number: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_prefix(conv_id, timer_id)?;
    descending_text(&mut raw, poll_time);
    raw.extend_from_slice(&(!poll_number).to_be_bytes());
    owner_key(tx, TIMER_POLL_TIME_INDEX_NAMESPACE, &raw)
}

fn poll_claim_key(
    tx: &AuthorityTransaction,
    conv_id: &str,
    timer_id: &str,
    poll_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = poll_prefix(conv_id, timer_id)?;
    raw.extend_from_slice(poll_id.as_bytes());
    owner_key(tx, TIMER_POLL_ID_CLAIM_NAMESPACE, &raw)
}

fn poll_range(
    tx: &AuthorityTransaction,
    namespace: &str,
    conv_id: &str,
    timer_id: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        namespace,
        &poll_prefix(conv_id, timer_id)?,
    )
}

fn next_poll_number(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let key = global_key(tx, TIMER_POLL_SEQUENCE_NAMESPACE, POLL_SEQUENCE_KEY)?;
    let current = match database.entity_get(tx, &key)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().expect("length checked")),
        Some(_) => return Err(invalid_data("timer poll sequence is malformed")),
    };
    let next = current
        .checked_add(1)
        .ok_or_else(|| invalid_data("timer poll sequence overflows"))?;
    database.entity_put(tx, key, next.to_le_bytes().to_vec())?;
    Ok(next)
}

fn append_poll(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    timer_id: &str,
    poll: &Map<String, Value>,
) -> io::Result<(bool, u64)> {
    let Some(timer) = read_document(database, tx, timer_id)? else {
        return Err(io::Error::new(io::ErrorKind::NotFound, "timer not found"));
    };
    let conv_id = text_field(&timer, "conv_id")?;
    let poll_id = text_field(poll, "poll_id")?;
    if !poll_id.is_empty() {
        let claim = poll_claim_key(tx, conv_id, timer_id, poll_id)?;
        if let Some(raw) = database.entity_get(tx, &claim)? {
            if raw.len() != 8 {
                return Err(invalid_data("timer poll ID claim is malformed"));
            }
            return Ok((
                false,
                u64::from_le_bytes(raw.try_into().expect("length checked")),
            ));
        }
    }
    let poll_number = next_poll_number(database, tx)?;
    let mut document = poll.clone();
    document.insert("id".to_owned(), json!(poll_number));
    document.insert("timer_id".to_owned(), json!(timer_id));
    let encoded =
        serde_json::to_vec(&document).map_err(|_| invalid_input("timer poll cannot be encoded"))?;
    if encoded.len() > MAX_TIMER_POLL_DOCUMENT_BYTES {
        return Err(exhausted("timer poll document exceeds its bound"));
    }
    database.entity_put(
        tx,
        poll_document_key(tx, conv_id, timer_id, poll_number)?,
        encoded,
    )?;
    database.entity_put(
        tx,
        poll_time_key(
            tx,
            conv_id,
            timer_id,
            text_field(&document, "poll_time")?,
            poll_number,
        )?,
        poll_number.to_le_bytes().to_vec(),
    )?;
    if !poll_id.is_empty() {
        database.entity_put(
            tx,
            poll_claim_key(tx, conv_id, timer_id, poll_id)?,
            poll_number.to_le_bytes().to_vec(),
        )?;
    }
    Ok((true, poll_number))
}

struct ProgressUpdate<'a> {
    poll_time: &'a str,
    decision: &'a str,
    reason: &'a str,
    require_active: bool,
    updated_at_ms: u64,
}

fn update_progress(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    timer_id: &str,
    update: ProgressUpdate<'_>,
) -> io::Result<bool> {
    let Some(mut document) = read_document(database, tx, timer_id)? else {
        return Ok(false);
    };
    if update.require_active && text_field(&document, "status")? != "active" {
        return Ok(false);
    }
    let count = document["poll_count"]
        .as_i64()
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| invalid_data("timer poll count overflows"))?;
    document.insert("poll_count".to_owned(), json!(count));
    document.insert("last_poll_at".to_owned(), json!(update.poll_time));
    document.insert("last_poll_decision".to_owned(), json!(update.decision));
    document.insert("last_poll_reason".to_owned(), json!(update.reason));
    document.insert("updated_at".to_owned(), json!(update.poll_time));
    put_document(
        database,
        tx,
        timer_id,
        &document,
        update.updated_at_ms,
        None,
    )?;
    Ok(true)
}

#[derive(Clone, Debug)]
pub enum Request {
    Create {
        timer_id: String,
        document: Map<String, Value>,
        updated_at_ms: u64,
    },
    Get {
        timer_id: String,
    },
    List {
        limit: usize,
    },
    History,
    ActiveListAll {
        limit: usize,
    },
    ActiveCount,
    Cancel {
        timer_id: String,
        now: String,
        updated_at_ms: u64,
    },
    Update {
        timer_id: String,
        updates: Map<String, Value>,
        expected_status: Option<String>,
        updated_at_ms: u64,
    },
    PollAppend {
        timer_id: String,
        poll: Map<String, Value>,
    },
    PollCommit {
        timer_id: String,
        poll: Map<String, Value>,
        updated_at_ms: u64,
    },
    Progress {
        timer_id: String,
        poll_time: String,
        decision: String,
        reason: String,
        updated_at_ms: u64,
    },
    PollLog {
        timer_id: String,
        limit: usize,
    },
}

impl Request {
    pub(crate) fn mutates_state(&self) -> bool {
        matches!(
            self,
            Self::Create { .. }
                | Self::Cancel { .. }
                | Self::Update { .. }
                | Self::PollAppend { .. }
                | Self::PollCommit { .. }
                | Self::Progress { .. }
        )
    }

    pub(crate) fn validate(&self, owner: u64) -> io::Result<usize> {
        let size = match self {
            Self::Create {
                timer_id,
                document,
                updated_at_ms,
            } => {
                if *updated_at_ms == 0 {
                    return Err(invalid_input("timer timestamp is zero"));
                }
                validate_document(document, owner, timer_id)?;
                timer_id.len() + serde_json::to_vec(document).map_or(0, |raw| raw.len())
            }
            Self::Get { timer_id } => timer_id.len(),
            Self::Cancel {
                timer_id,
                now,
                updated_at_ms,
            } if now.chars().count() <= 64 && *updated_at_ms > 0 => timer_id.len() + now.len(),
            Self::List { limit } if (1..=MAX_TIMER_LIST_ROWS).contains(limit) => 0,
            Self::ActiveListAll { limit } if (1..=MAX_TIMER_GLOBAL_ACTIVE_ROWS).contains(limit) => {
                0
            }
            Self::History | Self::ActiveCount => 0,
            Self::Update {
                timer_id,
                updates,
                expected_status,
                updated_at_ms,
            } if !updates.is_empty()
                && *updated_at_ms > 0
                && expected_status
                    .as_ref()
                    .is_none_or(|value| value.chars().count() <= 32) =>
            {
                timer_id.len() + serde_json::to_vec(updates).map_or(0, |raw| raw.len())
            }
            Self::PollAppend { timer_id, poll } if valid_poll_document(poll, false) => {
                timer_id.len() + serde_json::to_vec(poll).map_or(0, |raw| raw.len())
            }
            Self::PollCommit {
                timer_id,
                poll,
                updated_at_ms,
            } if valid_poll_document(poll, true) && *updated_at_ms > 0 => {
                timer_id.len() + serde_json::to_vec(poll).map_or(0, |raw| raw.len())
            }
            Self::Progress {
                timer_id,
                poll_time,
                decision,
                reason,
                updated_at_ms,
            } if !poll_time.is_empty()
                && poll_time.chars().count() <= 64
                && decision.chars().count() <= 128
                && reason.chars().count() <= 500
                && *updated_at_ms > 0 =>
            {
                timer_id.len() + poll_time.len() + decision.len() + reason.len()
            }
            Self::PollLog { timer_id, limit } if (1..=MAX_TIMER_POLL_ROWS).contains(limit) => {
                timer_id.len()
            }
            _ => return Err(invalid_input("invalid timer request")),
        };
        if size > MAX_TRANSACTION_IR_LITERAL_BYTES
            || self.timer_id().is_some_and(|id| !valid_timer_id(id))
        {
            return Err(invalid_input("timer request exceeds its bound"));
        }
        Ok(size)
    }

    fn timer_id(&self) -> Option<&str> {
        match self {
            Self::Create { timer_id, .. }
            | Self::Get { timer_id }
            | Self::Cancel { timer_id, .. }
            | Self::Update { timer_id, .. }
            | Self::PollAppend { timer_id, .. }
            | Self::PollCommit { timer_id, .. }
            | Self::Progress { timer_id, .. }
            | Self::PollLog { timer_id, .. } => Some(timer_id),
            _ => None,
        }
    }
}

fn valid_poll_document(poll: &Map<String, Value>, require_time: bool) -> bool {
    poll.get("poll_time")
        .and_then(Value::as_str)
        .is_some_and(|value| (!require_time || !value.is_empty()) && value.chars().count() <= 64)
        && poll
            .get("poll_id")
            .and_then(Value::as_str)
            .is_some_and(|value| value.chars().count() <= 80)
        && serde_json::to_vec(poll).is_ok_and(|raw| raw.len() <= MAX_TIMER_POLL_DOCUMENT_BYTES)
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    match request {
        Request::Create {
            timer_id,
            document,
            updated_at_ms,
        } => {
            if database
                .entity_get(tx, &id_claim_key(tx, timer_id)?)?
                .is_some()
            {
                return Err(conflict("timer already exists"));
            }
            let total = read_count(
                database,
                tx,
                TIMER_TOTAL_COUNT_NAMESPACE,
                MAX_TIMERS_PER_OWNER,
            )?;
            if total >= MAX_TIMERS_PER_OWNER {
                return Err(conflict("timer capacity reached"));
            }
            let active = read_count(
                database,
                tx,
                TIMER_ACTIVE_COUNT_NAMESPACE,
                MAX_TIMERS_PER_OWNER,
            )?;
            if active >= database.timer_live_capacity() {
                return Err(conflict("active timer capacity reached"));
            }
            put_document(database, tx, timer_id, document, *updated_at_ms, Some(0))?;
            write_indexes(database, tx, document)?;
            database.entity_put(tx, id_claim_key(tx, timer_id)?, identity_value(document)?)?;
            write_count(
                database,
                tx,
                TIMER_TOTAL_COUNT_NAMESPACE,
                total + 1,
                MAX_TIMERS_PER_OWNER,
            )?;
            write_count(
                database,
                tx,
                TIMER_ACTIVE_COUNT_NAMESPACE,
                active + 1,
                MAX_TIMERS_PER_OWNER,
            )?;
            Ok(Some(
                serde_json::to_vec(&json!({"applied":true,"timer":document}))
                    .map_err(|_| invalid_data("timer create response cannot be encoded"))?,
            ))
        }
        Request::Get { timer_id } => read_document(database, tx, timer_id)?
            .map(|document| {
                serde_json::to_vec(&document)
                    .map_err(|_| invalid_data("timer get response cannot be encoded"))
            })
            .transpose(),
        Request::List { limit } => {
            let (start, end) = namespace_range(tx, TIMER_STATUS_CREATED_INDEX_NAMESPACE)?;
            let rows = database.entity_scan(tx, &start, &end, *limit)?;
            encode_documents(database, tx, rows.into_iter(), None, false)
        }
        Request::History => Ok(Some(
            serde_json::to_vec(
                &(read_count(
                    database,
                    tx,
                    TIMER_TOTAL_COUNT_NAMESPACE,
                    MAX_TIMERS_PER_OWNER,
                )? > 0),
            )
            .unwrap(),
        )),
        Request::ActiveCount => Ok(Some(
            serde_json::to_vec(&read_count(
                database,
                tx,
                TIMER_ACTIVE_COUNT_NAMESPACE,
                MAX_TIMERS_PER_OWNER,
            )?)
            .unwrap(),
        )),
        Request::ActiveListAll { limit } => {
            let (start, end) =
                global_namespace_range(tx, TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE)?;
            let rows = scan_rows_paged(database, tx, start, &end, *limit)?;
            encode_documents(database, tx, rows.into_iter(), Some("active"), true)
        }
        Request::Cancel {
            timer_id,
            now,
            updated_at_ms,
        } => {
            let Some(mut document) = read_document(database, tx, timer_id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            if text_field(&document, "status")? != "active" {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            }
            delete_indexes(database, tx, &document)?;
            document.insert("status".to_owned(), json!("cancelled"));
            document.insert("cancelled_at".to_owned(), json!(now));
            document.insert("updated_at".to_owned(), json!(now));
            put_document(database, tx, timer_id, &document, *updated_at_ms, None)?;
            write_indexes(database, tx, &document)?;
            adjust_active_count(database, tx, true, false)?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::Update {
            timer_id,
            updates,
            expected_status,
            updated_at_ms,
        } => {
            let Some(mut document) = read_document(database, tx, timer_id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            if expected_status
                .as_ref()
                .is_some_and(|expected| document["status"] != *expected)
            {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            }
            let was_active = text_field(&document, "status")? == "active";
            delete_indexes(database, tx, &document)?;
            for (field, value) in updates {
                document.insert(field.clone(), value.clone());
            }
            let is_active = text_field(&document, "status")? == "active";
            put_document(database, tx, timer_id, &document, *updated_at_ms, None)?;
            write_indexes(database, tx, &document)?;
            adjust_active_count(database, tx, was_active, is_active)?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::PollAppend { timer_id, poll } => {
            let (inserted, id) = append_poll(database, tx, timer_id, poll)?;
            Ok(Some(
                serde_json::to_vec(&json!({"inserted":inserted,"id":id})).unwrap(),
            ))
        }
        Request::PollCommit {
            timer_id,
            poll,
            updated_at_ms,
        } => {
            let (inserted, id) = append_poll(database, tx, timer_id, poll)?;
            let advanced = if inserted {
                update_progress(
                    database,
                    tx,
                    timer_id,
                    ProgressUpdate {
                        poll_time: text_field(poll, "poll_time")?,
                        decision: text_field(poll, "decision")?,
                        reason: text_field(poll, "reason")?,
                        require_active: false,
                        updated_at_ms: *updated_at_ms,
                    },
                )?
            } else {
                false
            };
            Ok(Some(
                serde_json::to_vec(&json!({"inserted":inserted,"id":id,"advanced":advanced}))
                    .unwrap(),
            ))
        }
        Request::Progress {
            timer_id,
            poll_time,
            decision,
            reason,
            updated_at_ms,
        } => Ok(Some(
            serde_json::to_vec(
                &json!({"changed":update_progress(database, tx, timer_id, ProgressUpdate {
            poll_time,
            decision,
            reason,
            require_active: true,
            updated_at_ms: *updated_at_ms,
        })?}),
            )
            .unwrap(),
        )),
        Request::PollLog { timer_id, limit } => {
            let Some(timer) = read_document(database, tx, timer_id)? else {
                return Ok(Some(b"[]".to_vec()));
            };
            let conv_id = text_field(&timer, "conv_id")?;
            let (start, end) = poll_range(tx, TIMER_POLL_TIME_INDEX_NAMESPACE, conv_id, timer_id)?;
            let rows = database.entity_scan(tx, &start, &end, *limit)?;
            let mut documents = Vec::with_capacity(rows.len());
            for (_, raw) in rows {
                if raw.len() != 8 {
                    return Err(invalid_data("timer poll index is malformed"));
                }
                let id = u64::from_le_bytes(raw.try_into().expect("length checked"));
                let document = database
                    .entity_get(tx, &poll_document_key(tx, conv_id, timer_id, id)?)?
                    .ok_or_else(|| invalid_data("timer poll index target is missing"))?;
                documents.push(
                    serde_json::from_slice::<Value>(&document)
                        .map_err(|_| invalid_data("timer poll document is malformed"))?,
                );
            }
            encode_response(&documents, "timer poll log")
        }
    }
}

fn scan_rows_paged(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    mut start: EntityKey,
    end: &EntityKey,
    limit: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let mut rows = Vec::with_capacity(limit);
    while rows.len() < limit {
        let page_limit = (limit - rows.len()).min(MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(tx, &start, end, page_limit)?;
        let page_is_full = page.len() == page_limit;
        let next_start = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if !page_is_full || rows.len() == limit {
            break;
        }
        start = next_start
            .ok_or_else(|| invalid_data("timer index pagination lost its continuation"))?
            .1;
    }
    Ok(rows)
}

fn encode_documents<I>(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    rows: I,
    required_status: Option<&str>,
    allow_cross_owner: bool,
) -> io::Result<Option<Vec<u8>>>
where
    I: Iterator<Item = (EntityKey, Vec<u8>)>,
{
    let mut documents = Vec::new();
    for (_, raw) in rows {
        let identity: Value =
            serde_json::from_slice(&raw).map_err(|_| invalid_data("timer index is malformed"))?;
        let owner = identity
            .get("owner_user_id")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("timer index owner is malformed"))?;
        if !allow_cross_owner && owner != tx.owner_user_id() {
            return Err(invalid_data("owner-local timer index crosses owner scope"));
        }
        let timer_id = identity
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("timer index ID is malformed"))?;
        let document = read_document_for_owner(database, tx, timer_id, owner)?
            .ok_or_else(|| invalid_data("timer index target is missing"))?;
        if required_status.is_some_and(|status| document["status"] != status) {
            return Err(invalid_data("timer status index target differs"));
        }
        documents.push(Value::Object(document));
    }
    encode_response(&documents, "timer list")
}

fn encode_response(documents: &[Value], name: &str) -> io::Result<Option<Vec<u8>>> {
    let response = serde_json::to_vec(documents)
        .map_err(|_| invalid_data("timer response cannot be encoded"))?;
    if response.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(exhausted(&format!("{name} exceeds 8 MiB")));
    }
    Ok(Some(response))
}

pub(crate) fn delete_conversation(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<usize> {
    let prefix = conversation_prefix(conversation_id)?;
    let (start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        TIMER_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let rows = database.entity_scan(tx, &start, &end, MAX_TIMERS_PER_OWNER + 1)?;
    if rows.len() > MAX_TIMERS_PER_OWNER {
        return Err(invalid_data("conversation timer index exceeds its bound"));
    }
    let mut deleted = 0_usize;
    let mut deleted_active = 0_usize;
    for (_, raw) in rows {
        let identity: Value = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("conversation timer index is malformed"))?;
        if identity.get("owner_user_id").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
            return Err(invalid_data("conversation timer index crosses owner scope"));
        }
        let timer_id = identity
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("conversation timer ID is malformed"))?;
        let document = read_document(database, tx, timer_id)?
            .ok_or_else(|| invalid_data("conversation timer index target is missing"))?;
        if text_field(&document, "conv_id")? != conversation_id {
            return Err(invalid_data("conversation timer index target differs"));
        }
        deleted_active += usize::from(text_field(&document, "status")? == "active");
        delete_indexes(database, tx, &document)?;
        versioned_document::delete(
            database,
            tx,
            document_key(tx, timer_id)?,
            LOGICAL_NAMESPACE,
            timer_id,
            None,
        )?;
        let claim = id_claim_key(tx, timer_id)?;
        let expected_claim = identity_value(&document)?;
        if database.entity_get(tx, &claim)?.as_deref() != Some(expected_claim.as_slice()) {
            return Err(invalid_data(
                "timer ID claim differs during conversation delete",
            ));
        }
        database.entity_delete(tx, claim)?;
        deleted += 1;
    }
    if deleted == 0 {
        return Ok(0);
    }
    let total = read_count(
        database,
        tx,
        TIMER_TOTAL_COUNT_NAMESPACE,
        MAX_TIMERS_PER_OWNER,
    )?;
    let active = read_count(
        database,
        tx,
        TIMER_ACTIVE_COUNT_NAMESPACE,
        MAX_TIMERS_PER_OWNER,
    )?;
    write_count(
        database,
        tx,
        TIMER_TOTAL_COUNT_NAMESPACE,
        total
            .checked_sub(deleted)
            .ok_or_else(|| invalid_data("timer total count underflows"))?,
        MAX_TIMERS_PER_OWNER,
    )?;
    write_count(
        database,
        tx,
        TIMER_ACTIVE_COUNT_NAMESPACE,
        active
            .checked_sub(deleted_active)
            .ok_or_else(|| invalid_data("timer active count underflows"))?,
        MAX_TIMERS_PER_OWNER,
    )?;
    for namespace in [
        TIMER_POLL_DOCUMENT_NAMESPACE,
        TIMER_POLL_TIME_INDEX_NAMESPACE,
        TIMER_POLL_ID_CLAIM_NAMESPACE,
    ] {
        let (start, end) =
            EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), namespace, &prefix)?;
        if !database.entity_scan(tx, &start, &end, 1)?.is_empty() {
            database.entity_retire_range(tx, &start, &end)?;
        }
    }
    Ok(deleted)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timer_global_active_scan_crosses_the_entity_page_boundary_without_duplicates() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in 0..=MAX_ENTITY_RANGE_ROWS {
            let key = global_key(
                &write,
                TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE,
                format!("{index:08}").as_bytes(),
            )
            .unwrap();
            database
                .entity_put(&mut write, key, index.to_be_bytes().to_vec())
                .unwrap();
        }
        database.commit(write).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let (start, end) =
            global_namespace_range(&read, TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE).unwrap();
        let rows =
            scan_rows_paged(&database, &mut read, start, &end, MAX_ENTITY_RANGE_ROWS + 1).unwrap();
        assert_eq!(rows.len(), MAX_ENTITY_RANGE_ROWS + 1);
        for (index, (_, value)) in rows.into_iter().enumerate() {
            assert_eq!(value, index.to_be_bytes());
        }
    }
}
