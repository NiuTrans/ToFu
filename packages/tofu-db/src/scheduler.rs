//! Owner-scoped scheduled-task documents, indexes, quotas, and identity claims.
//!
//! This module owns the shared physical state used by scheduler CRUD and the
//! later due-claim/system-adoption/poll state machines. Documents may spill to
//! owner-bound blobs; compact counts, claims, and covering indexes commit in
//! the same OCC transaction.

use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_SCHEDULER_SYSTEM_KEY_CHARACTERS, MAX_SCHEDULER_TASKS_PER_OWNER,
    MAX_SCHEDULER_TASK_DOCUMENT_BYTES, MAX_SCHEDULER_TASK_ID_CHARACTERS,
    MAX_SCHEDULER_TASK_LIST_ROWS, MAX_TRANSACTION_IR_LITERAL_BYTES,
    SCHEDULER_POLL_DOCUMENT_NAMESPACE, SCHEDULER_POLL_SEQUENCE_NAMESPACE,
    SCHEDULER_POLL_TIME_INDEX_NAMESPACE, SCHEDULER_TASK_COUNT_NAMESPACE,
    SCHEDULER_TASK_CREATED_INDEX_NAMESPACE, SCHEDULER_TASK_DOCUMENT_NAMESPACE,
    SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE, SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
    SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE, SCHEDULER_TASK_ID_CLAIM_NAMESPACE,
    SCHEDULER_TASK_SYSTEM_KEY_CLAIM_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "scheduled_tasks";
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

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
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

fn task_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    global_key(
        transaction,
        SCHEDULER_TASK_DOCUMENT_NAMESPACE,
        task_id.as_bytes(),
    )
}

fn claim_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    global_key(
        transaction,
        SCHEDULER_TASK_ID_CLAIM_NAMESPACE,
        task_id.as_bytes(),
    )
}

fn count_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(transaction, SCHEDULER_TASK_COUNT_NAMESPACE, COUNT_KEY)
}

fn descending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[!byte, 0]);
    }
    output.push(u8::MAX);
}

fn index_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    created_at: &str,
    task_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((created_at.len() + task_id.len()) * 2 + 2);
    descending_text(&mut raw, created_at);
    descending_text(&mut raw, task_id);
    owner_key(transaction, namespace, &raw)
}

fn global_index_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    created_at: &str,
    owner_user_id: u64,
    task_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((created_at.len() + task_id.len()) * 2 + 10);
    descending_text(&mut raw, created_at);
    raw.extend_from_slice(&owner_user_id.to_be_bytes());
    descending_text(&mut raw, task_id);
    global_key(transaction, namespace, &raw)
}

fn global_namespace_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        namespace,
        b"",
    )
}

fn namespace_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        b"",
    )
}

fn task_history_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    task_id: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    let mut prefix = Vec::with_capacity(task_id.len() + 2);
    prefix.extend_from_slice(
        &u16::try_from(task_id.len())
            .map_err(|_| invalid_input("scheduler task ID is too long"))?
            .to_be_bytes(),
    );
    prefix.extend_from_slice(task_id.as_bytes());
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &prefix,
    )
}

fn poll_task_prefix(task_id: &str) -> io::Result<Vec<u8>> {
    let mut prefix = Vec::with_capacity(task_id.len() + 2);
    prefix.extend_from_slice(
        &u16::try_from(task_id.len())
            .map_err(|_| invalid_input("scheduler task ID is too long"))?
            .to_be_bytes(),
    );
    prefix.extend_from_slice(task_id.as_bytes());
    Ok(prefix)
}

fn poll_document_key(
    transaction: &AuthorityTransaction,
    task_id: &str,
    poll_id: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_task_prefix(task_id)?;
    raw.extend_from_slice(&poll_id.to_be_bytes());
    owner_key(transaction, SCHEDULER_POLL_DOCUMENT_NAMESPACE, &raw)
}

fn poll_index_key(
    transaction: &AuthorityTransaction,
    task_id: &str,
    poll_time: &str,
    poll_id: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_task_prefix(task_id)?;
    descending_text(&mut raw, poll_time);
    raw.extend_from_slice(&(!poll_id).to_be_bytes());
    owner_key(transaction, SCHEDULER_POLL_TIME_INDEX_NAMESPACE, &raw)
}

fn poll_sequence_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    global_key(
        transaction,
        SCHEDULER_POLL_SEQUENCE_NAMESPACE,
        POLL_SEQUENCE_KEY,
    )
}

fn next_poll_sequence(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let key = poll_sequence_key(transaction)?;
    let current = match database.entity_get(transaction, &key)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().expect("length checked")),
        Some(_) => return Err(invalid_data("scheduler poll sequence is malformed")),
    };
    let next = current
        .checked_add(1)
        .ok_or_else(|| invalid_data("scheduler poll sequence overflows"))?;
    database.entity_put(transaction, key, next.to_le_bytes().to_vec())?;
    Ok(next)
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<usize> {
    match database.entity_get(transaction, &count_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => {
            usize::try_from(u64::from_le_bytes(raw.try_into().expect("length checked")))
                .ok()
                .filter(|count| *count <= MAX_SCHEDULER_TASKS_PER_OWNER)
                .ok_or_else(|| invalid_data("scheduler task count exceeds its bound"))
        }
        Some(_) => Err(invalid_data("scheduler task count is malformed")),
    }
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    count: usize,
) -> io::Result<()> {
    if count > MAX_SCHEDULER_TASKS_PER_OWNER {
        return Err(conflict("too many scheduled tasks"));
    }
    database.entity_put(
        transaction,
        count_key(transaction)?,
        u64::try_from(count)
            .map_err(|_| resource_exhausted("scheduler task count overflow"))?
            .to_le_bytes()
            .to_vec(),
    )
}

fn valid_task_id(task_id: &str) -> bool {
    !task_id.is_empty() && task_id.chars().count() <= MAX_SCHEDULER_TASK_ID_CHARACTERS
}

#[derive(Clone, Copy, Debug)]
pub struct ParsedTimestamp {
    micros: i64,
    aware: bool,
}

fn parse_fixed_digits(bytes: &[u8], start: usize, count: usize) -> Option<i64> {
    let mut value = 0_i64;
    for byte in bytes.get(start..start.checked_add(count)?)? {
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value.checked_mul(10)?.checked_add(i64::from(byte - b'0'))?;
    }
    Some(value)
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = year - i64::from(month <= 2);
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let shifted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

pub(crate) fn parse_timestamp(value: &str) -> Option<ParsedTimestamp> {
    let bytes = value.as_bytes();
    if bytes.len() < 16
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || !matches!(bytes.get(10), Some(b'T' | b' '))
        || bytes.get(13) != Some(&b':')
    {
        return None;
    }
    let year = parse_fixed_digits(bytes, 0, 4)?;
    let month = parse_fixed_digits(bytes, 5, 2)?;
    let day = parse_fixed_digits(bytes, 8, 2)?;
    let hour = parse_fixed_digits(bytes, 11, 2)?;
    let minute = parse_fixed_digits(bytes, 14, 2)?;
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let month_days = [
        31,
        28 + i64::from(leap),
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if !(1..=12).contains(&month)
        || !(1..=month_days[usize::try_from(month - 1).ok()?]).contains(&day)
        || hour > 23
        || minute > 59
    {
        return None;
    }
    let mut offset = 16;
    let mut second = 0_i64;
    if bytes.get(offset) == Some(&b':') {
        second = parse_fixed_digits(bytes, offset + 1, 2)?;
        if second > 59 {
            return None;
        }
        offset += 3;
    }
    let mut fraction_micros = 0_i64;
    if bytes.get(offset) == Some(&b'.') || bytes.get(offset) == Some(&b',') {
        offset += 1;
        let start = offset;
        while bytes.get(offset).is_some_and(u8::is_ascii_digit) {
            offset += 1;
        }
        if offset == start {
            return None;
        }
        let digits = (offset - start).min(6);
        fraction_micros = parse_fixed_digits(bytes, start, digits)?;
        for _ in digits..6 {
            fraction_micros *= 10;
        }
    }
    let mut aware = false;
    let mut timezone_seconds = 0_i64;
    if offset < bytes.len() {
        if bytes[offset] == b'Z' && offset + 1 == bytes.len() {
            aware = true;
        } else if matches!(bytes[offset], b'+' | b'-') {
            aware = true;
            let sign = if bytes[offset] == b'+' { 1 } else { -1 };
            let timezone_hour = parse_fixed_digits(bytes, offset + 1, 2)?;
            if bytes.get(offset + 3) != Some(&b':') {
                return None;
            }
            let timezone_minute = parse_fixed_digits(bytes, offset + 4, 2)?;
            if timezone_hour > 23 || timezone_minute > 59 || offset + 6 != bytes.len() {
                return None;
            }
            timezone_seconds = sign * (timezone_hour * 3_600 + timezone_minute * 60);
        } else {
            return None;
        }
    }
    let seconds = days_from_civil(year, month, day)
        .checked_mul(86_400)?
        .checked_add(hour * 3_600 + minute * 60 + second)?
        .checked_sub(timezone_seconds)?;
    Some(ParsedTimestamp {
        micros: seconds
            .checked_mul(1_000_000)?
            .checked_add(fraction_micros)?,
        aware,
    })
}

fn validate_document(
    document: &Map<String, Value>,
    owner_user_id: u64,
    task_id: &str,
) -> io::Result<()> {
    if !valid_task_id(task_id)
        || document.get("id").and_then(Value::as_str) != Some(task_id)
        || document.get("user_id").and_then(Value::as_u64) != Some(owner_user_id)
        || document.get("system_key").and_then(Value::as_str).is_none()
        || document.get("created_at").and_then(Value::as_str).is_none()
        || document.get("enabled").and_then(Value::as_u64).is_none()
    {
        return Err(invalid_data("scheduler task identity fields are malformed"));
    }
    let encoded = serde_json::to_vec(document)
        .map_err(|_| invalid_data("scheduler task document cannot be encoded"))?;
    if encoded.len() > MAX_SCHEDULER_TASK_DOCUMENT_BYTES {
        return Err(resource_exhausted(
            "scheduler task document exceeds its bound",
        ));
    }
    Ok(())
}

fn read_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    read_document_for_owner(database, transaction, task_id, transaction.owner_user_id())
}

fn read_document_for_owner(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    expected_owner_user_id: u64,
) -> io::Result<Option<Map<String, Value>>> {
    let claim = database.entity_get(transaction, &claim_key(transaction, task_id)?)?;
    let Some(claim) = claim else {
        let orphan = database.entity_get(transaction, &task_key(transaction, task_id)?)?;
        if orphan.is_some() {
            return Err(invalid_data(
                "scheduler task document has no identity claim",
            ));
        }
        return Ok(None);
    };
    let claimed_owner_user_id = serde_json::from_slice::<Value>(&claim)
        .ok()
        .and_then(|value| value.get("owner_user_id").and_then(Value::as_u64))
        .filter(|owner_user_id| *owner_user_id > 0)
        .ok_or_else(|| invalid_data("scheduler task identity claim is malformed"))?;
    if claimed_owner_user_id != expected_owner_user_id {
        return Ok(None);
    }
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &task_key(transaction, task_id)?,
        LOGICAL_NAMESPACE,
        task_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_SCHEDULER_TASK_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let document = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("scheduler task document is malformed"))?;
    validate_document(&document, expected_owner_user_id, task_id)?;
    Ok(Some(document))
}

fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    document: &Map<String, Value>,
    updated_at_ms: u64,
    expected_version: Option<u64>,
) -> io::Result<()> {
    validate_document(document, transaction.owner_user_id(), task_id)?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: task_key(transaction, task_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: task_id.to_owned(),
            value_json: serde_json::to_vec(document)
                .map_err(|_| invalid_input("scheduler task document cannot be encoded"))?,
            expected_version,
            updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_SCHEDULER_TASK_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn index_value(task_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"id": task_id}))
        .map_err(|_| invalid_data("scheduler task index cannot be encoded"))
}

fn global_index_value(owner_user_id: u64, task_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"owner_user_id": owner_user_id, "id": task_id}))
        .map_err(|_| invalid_data("global scheduler task index cannot be encoded"))
}

fn write_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let task_id = document["id"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler task ID is malformed"))?;
    let created_at = document["created_at"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler created_at is malformed"))?;
    let value = index_value(task_id)?;
    database.entity_put(
        transaction,
        index_key(
            transaction,
            SCHEDULER_TASK_CREATED_INDEX_NAMESPACE,
            created_at,
            task_id,
        )?,
        value.clone(),
    )?;
    let global_value = global_index_value(transaction.owner_user_id(), task_id)?;
    database.entity_put(
        transaction,
        global_index_key(
            transaction,
            SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
            created_at,
            transaction.owner_user_id(),
            task_id,
        )?,
        global_value.clone(),
    )?;
    if document["enabled"].as_u64() == Some(1) {
        database.entity_put(
            transaction,
            index_key(
                transaction,
                SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE,
                created_at,
                task_id,
            )?,
            value,
        )?;
        database.entity_put(
            transaction,
            global_index_key(
                transaction,
                SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE,
                created_at,
                transaction.owner_user_id(),
                task_id,
            )?,
            global_value,
        )?;
    }
    Ok(())
}

fn delete_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let task_id = document["id"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler task ID is malformed"))?;
    let created_at = document["created_at"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler created_at is malformed"))?;
    database.entity_delete(
        transaction,
        index_key(
            transaction,
            SCHEDULER_TASK_CREATED_INDEX_NAMESPACE,
            created_at,
            task_id,
        )?,
    )?;
    database.entity_delete(
        transaction,
        global_index_key(
            transaction,
            SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
            created_at,
            transaction.owner_user_id(),
            task_id,
        )?,
    )?;
    if document["enabled"].as_u64() == Some(1) {
        database.entity_delete(
            transaction,
            index_key(
                transaction,
                SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE,
                created_at,
                task_id,
            )?,
        )?;
        database.entity_delete(
            transaction,
            global_index_key(
                transaction,
                SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE,
                created_at,
                transaction.owner_user_id(),
                task_id,
            )?,
        )?;
    }
    Ok(())
}

fn system_key_key(transaction: &AuthorityTransaction, system_key: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        SCHEDULER_TASK_SYSTEM_KEY_CLAIM_NAMESPACE,
        system_key.as_bytes(),
    )
}

fn create_task_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    document: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let count = read_count(database, transaction)?;
    if count >= MAX_SCHEDULER_TASKS_PER_OWNER {
        return Err(conflict("too many scheduled tasks"));
    }
    let claim = claim_key(transaction, task_id)?;
    if database.entity_get(transaction, &claim)?.is_some() {
        return Err(conflict("scheduled task already exists"));
    }
    put_document(
        database,
        transaction,
        task_id,
        document,
        updated_at_ms,
        Some(0),
    )?;
    write_indexes(database, transaction, document)?;
    database.entity_put(
        transaction,
        claim,
        serde_json::to_vec(&json!({
            "owner_user_id": transaction.owner_user_id(),
            "id": task_id,
        }))
        .map_err(|_| invalid_data("scheduler task claim cannot be encoded"))?,
    )?;
    let system_key = document["system_key"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler system key is malformed"))?;
    if !system_key.is_empty() {
        let key = system_key_key(transaction, system_key)?;
        if database.entity_get(transaction, &key)?.is_some() {
            return Err(conflict("scheduler system key already exists"));
        }
        database.entity_put(transaction, key, index_value(task_id)?)?;
    }
    write_count(database, transaction, count + 1)
}

fn delete_task_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let claim = claim_key(transaction, task_id)?;
    let expected_claim = serde_json::to_vec(&json!({
        "owner_user_id": transaction.owner_user_id(),
        "id": task_id,
    }))
    .map_err(|_| invalid_data("scheduler task claim cannot be encoded"))?;
    if database.entity_get(transaction, &claim)?.as_deref() != Some(expected_claim.as_slice()) {
        return Err(invalid_data("scheduler task claim differs or is missing"));
    }
    let system_key = document["system_key"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler system key is malformed"))?;
    if !system_key.is_empty() {
        let key = system_key_key(transaction, system_key)?;
        let expected_system_claim = index_value(task_id)?;
        if database.entity_get(transaction, &key)?.as_deref()
            != Some(expected_system_claim.as_slice())
        {
            return Err(invalid_data(
                "scheduler system-key claim differs or is missing",
            ));
        }
        database.entity_delete(transaction, key)?;
    }
    delete_indexes(database, transaction, document)?;
    versioned_document::delete(
        database,
        transaction,
        task_key(transaction, task_id)?,
        LOGICAL_NAMESPACE,
        task_id,
        None,
    )?;
    database.entity_delete(transaction, claim)?;
    let count = read_count(database, transaction)?;
    write_count(
        database,
        transaction,
        count
            .checked_sub(1)
            .ok_or_else(|| invalid_data("scheduler task count underflow"))?,
    )?;
    for namespace in [
        SCHEDULER_POLL_DOCUMENT_NAMESPACE,
        SCHEDULER_POLL_TIME_INDEX_NAMESPACE,
    ] {
        let (start, end) = task_history_range(transaction, namespace, task_id)?;
        if !database
            .entity_scan(transaction, &start, &end, 1)?
            .is_empty()
        {
            database.entity_retire_range(transaction, &start, &end)?;
        }
    }
    Ok(())
}

#[derive(Clone, Debug)]
pub enum Request {
    Create {
        task_id: String,
        document: Map<String, Value>,
        updated_at_ms: u64,
    },
    Ensure {
        task_id: String,
        system_key: String,
        create_document: Map<String, Value>,
        definition: Map<String, Value>,
        updated_at: String,
        updated_at_ms: u64,
    },
    Get {
        task_id: String,
    },
    List {
        limit: usize,
        enabled_only: bool,
    },
    ListAll {
        limit: usize,
        enabled_only: bool,
    },
    Update {
        task_id: String,
        updates: Map<String, Value>,
        updated_at_ms: u64,
    },
    RecordResult {
        task_id: String,
        now: String,
        result: String,
        success: bool,
        updated_at_ms: u64,
    },
    ClaimDue {
        task_id: String,
        lane: String,
        now: String,
        parsed_now: ParsedTimestamp,
        minimum_interval_seconds: u64,
        updated_at_ms: u64,
    },
    PollAppend {
        task_id: String,
        poll: Map<String, Value>,
    },
    PollLog {
        task_id: String,
        limit: usize,
    },
    Delete {
        task_id: String,
    },
}

impl Request {
    pub(crate) fn mutates_state(&self) -> bool {
        matches!(
            self,
            Self::Create { .. }
                | Self::Ensure { .. }
                | Self::Update { .. }
                | Self::RecordResult { .. }
                | Self::ClaimDue { .. }
                | Self::PollAppend { .. }
                | Self::Delete { .. }
        )
    }

    pub(crate) fn validate(&self, owner_user_id: u64) -> io::Result<usize> {
        match self {
            Self::Create {
                task_id,
                document,
                updated_at_ms,
            } => {
                if *updated_at_ms == 0 {
                    return Err(invalid_input("scheduler physical timestamp is zero"));
                }
                validate_document(document, owner_user_id, task_id)?;
                Ok(task_id.len() + serde_json::to_vec(document).map_or(0, |raw| raw.len()))
            }
            Self::Ensure {
                task_id,
                system_key,
                create_document,
                definition,
                updated_at,
                updated_at_ms,
            } => {
                if !valid_task_id(task_id)
                    || system_key.is_empty()
                    || system_key.chars().count() > MAX_SCHEDULER_SYSTEM_KEY_CHARACTERS
                    || updated_at.chars().count() > 20_000
                    || definition.is_empty()
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid scheduler ensure request"));
                }
                validate_document(create_document, owner_user_id, task_id)?;
                if create_document["system_key"].as_str() != Some(system_key) {
                    return Err(invalid_input("scheduler ensure system key differs"));
                }
                Ok(task_id.len()
                    + system_key.len()
                    + updated_at.len()
                    + serde_json::to_vec(create_document).map_or(0, |raw| raw.len())
                    + serde_json::to_vec(definition).map_or(0, |raw| raw.len()))
            }
            Self::Get { task_id } | Self::Delete { task_id } => {
                if !valid_task_id(task_id) {
                    return Err(invalid_input("invalid scheduler task ID"));
                }
                Ok(task_id.len())
            }
            Self::List { limit, .. } | Self::ListAll { limit, .. } => {
                if !(1..=MAX_SCHEDULER_TASK_LIST_ROWS).contains(limit) {
                    return Err(invalid_input("invalid scheduler task list limit"));
                }
                Ok(0)
            }
            Self::Update {
                task_id,
                updates,
                updated_at_ms,
            } => {
                if !valid_task_id(task_id) || updates.is_empty() || *updated_at_ms == 0 {
                    return Err(invalid_input("invalid scheduler task update"));
                }
                Ok(task_id.len() + serde_json::to_vec(updates).map_or(0, |raw| raw.len()))
            }
            Self::RecordResult {
                task_id,
                now,
                result,
                updated_at_ms,
                ..
            } => {
                if !valid_task_id(task_id)
                    || now.is_empty()
                    || now.chars().count() > 64
                    || result.chars().count() > 10_000
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid scheduler result update"));
                }
                Ok(task_id.len() + now.len() + result.len())
            }
            Self::ClaimDue {
                task_id,
                lane,
                now,
                minimum_interval_seconds,
                updated_at_ms,
                ..
            } => {
                if !valid_task_id(task_id)
                    || !matches!(lane.as_str(), "run" | "poll")
                    || now.is_empty()
                    || now.chars().count() > 64
                    || !(1..=3_600).contains(minimum_interval_seconds)
                    || *updated_at_ms == 0
                {
                    return Err(invalid_input("invalid scheduler due claim"));
                }
                Ok(task_id.len() + lane.len() + now.len())
            }
            Self::PollAppend { task_id, poll } => {
                if !valid_task_id(task_id)
                    || serde_json::to_vec(poll).map_or(true, |raw| {
                        raw.len() > crate::generated_tofudb_ir::MAX_SCHEDULER_POLL_DOCUMENT_BYTES
                    })
                {
                    return Err(invalid_input("invalid scheduler poll append"));
                }
                Ok(task_id.len() + serde_json::to_vec(poll).map_or(0, |raw| raw.len()))
            }
            Self::PollLog { task_id, limit } => {
                if !valid_task_id(task_id)
                    || !(1..=crate::generated_tofudb_ir::MAX_SCHEDULER_POLL_ROWS).contains(limit)
                {
                    return Err(invalid_input("invalid scheduler poll log query"));
                }
                Ok(task_id.len())
            }
        }
    }
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    match request {
        Request::Create {
            task_id,
            document,
            updated_at_ms,
        } => {
            create_task_state(database, transaction, task_id, document, *updated_at_ms)?;
            Ok(Some(
                serde_json::to_vec(&json!({"applied": true, "task": document}))
                    .map_err(|_| invalid_data("scheduler create response cannot be encoded"))?,
            ))
        }
        Request::Ensure {
            task_id,
            system_key,
            create_document,
            definition,
            updated_at,
            updated_at_ms,
        } => {
            let system_claim_key = system_key_key(transaction, system_key)?;
            let existing_id = database
                .entity_get(transaction, &system_claim_key)?
                .map(|raw| {
                    serde_json::from_slice::<Value>(&raw)
                        .ok()
                        .and_then(|value| {
                            value.get("id").and_then(Value::as_str).map(str::to_owned)
                        })
                        .ok_or_else(|| invalid_data("scheduler system-key claim is malformed"))
                })
                .transpose()?;
            let (start, end) =
                namespace_range(transaction, SCHEDULER_TASK_CREATED_INDEX_NAMESPACE)?;
            let rows = database.entity_scan(
                transaction,
                &start,
                &end,
                MAX_SCHEDULER_TASKS_PER_OWNER + 1,
            )?;
            if rows.len() != read_count(database, transaction)? {
                return Err(invalid_data("scheduler task count differs from its index"));
            }
            let expected_name = definition["name"]
                .as_str()
                .ok_or_else(|| invalid_input("scheduler ensure name is malformed"))?;
            let expected_type = definition["task_type"]
                .as_str()
                .ok_or_else(|| invalid_input("scheduler ensure type is malformed"))?;
            let mut legacy = Vec::new();
            for (_, raw) in rows {
                let candidate_id = serde_json::from_slice::<Value>(&raw)
                    .ok()
                    .and_then(|value| value.get("id").and_then(Value::as_str).map(str::to_owned))
                    .ok_or_else(|| invalid_data("scheduler task index is malformed"))?;
                let candidate = read_document(database, transaction, &candidate_id)?
                    .ok_or_else(|| invalid_data("scheduler task index target is missing"))?;
                if candidate["system_key"] == ""
                    && candidate["name"] == expected_name
                    && candidate["task_type"] == expected_type
                {
                    legacy.push(candidate);
                }
            }
            legacy.sort_by(|left, right| {
                (left["created_at"].as_str(), left["id"].as_str())
                    .cmp(&(right["created_at"].as_str(), right["id"].as_str()))
            });
            let mut current = if legacy.is_empty() {
                existing_id
                    .as_deref()
                    .map(|id| {
                        read_document(database, transaction, id)?.ok_or_else(|| {
                            invalid_data("scheduler system-key claim target is missing")
                        })
                    })
                    .transpose()?
            } else {
                let mut keeper = legacy.remove(0);
                let keeper_id = keeper["id"]
                    .as_str()
                    .ok_or_else(|| invalid_data("scheduler keeper ID is malformed"))?
                    .to_owned();
                for duplicate in legacy {
                    let duplicate_id = duplicate["id"]
                        .as_str()
                        .ok_or_else(|| invalid_data("scheduler duplicate ID is malformed"))?
                        .to_owned();
                    delete_task_state(database, transaction, &duplicate_id, &duplicate)?;
                }
                if let Some(existing_id) = existing_id.as_deref() {
                    if existing_id != keeper_id {
                        let duplicate = read_document(database, transaction, existing_id)?
                            .ok_or_else(|| invalid_data("scheduler keyed duplicate is missing"))?;
                        delete_task_state(database, transaction, existing_id, &duplicate)?;
                    }
                }
                delete_indexes(database, transaction, &keeper)?;
                keeper.insert("system_key".to_owned(), json!(system_key));
                keeper.insert("updated_at".to_owned(), json!(updated_at));
                put_document(
                    database,
                    transaction,
                    &keeper_id,
                    &keeper,
                    *updated_at_ms,
                    None,
                )?;
                write_indexes(database, transaction, &keeper)?;
                database.entity_put(
                    transaction,
                    system_claim_key.clone(),
                    index_value(&keeper_id)?,
                )?;
                Some(keeper)
            };
            if let Some(mut document) = current.take() {
                let changed = definition
                    .iter()
                    .any(|(field, value)| document.get(field) != Some(value));
                if changed {
                    delete_indexes(database, transaction, &document)?;
                    for (field, value) in definition {
                        document.insert(field.clone(), value.clone());
                    }
                    document.insert("updated_at".to_owned(), json!(updated_at));
                    let current_id = document["id"]
                        .as_str()
                        .ok_or_else(|| invalid_data("scheduler ensured ID is malformed"))?
                        .to_owned();
                    put_document(
                        database,
                        transaction,
                        &current_id,
                        &document,
                        *updated_at_ms,
                        None,
                    )?;
                    write_indexes(database, transaction, &document)?;
                }
                return Ok(Some(
                    serde_json::to_vec(&json!({
                        "created": false,
                        "updated": changed,
                        "task": document,
                    }))
                    .map_err(|_| invalid_data("scheduler ensure response cannot be encoded"))?,
                ));
            }
            create_task_state(
                database,
                transaction,
                task_id,
                create_document,
                *updated_at_ms,
            )?;
            Ok(Some(
                serde_json::to_vec(&json!({"created": true, "task": create_document}))
                    .map_err(|_| invalid_data("scheduler ensure response cannot be encoded"))?,
            ))
        }
        Request::Get { task_id } => read_document(database, transaction, task_id)?
            .map(|document| {
                serde_json::to_vec(&document)
                    .map_err(|_| invalid_data("scheduler get response cannot be encoded"))
            })
            .transpose(),
        Request::List {
            limit,
            enabled_only,
        } => {
            let namespace = if *enabled_only {
                SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE
            } else {
                SCHEDULER_TASK_CREATED_INDEX_NAMESPACE
            };
            let (start, end) = namespace_range(transaction, namespace)?;
            let rows = database.entity_scan(
                transaction,
                &start,
                &end,
                MAX_SCHEDULER_TASKS_PER_OWNER + 1,
            )?;
            if rows.len() > MAX_SCHEDULER_TASKS_PER_OWNER {
                return Err(invalid_data("scheduler task index exceeds its bound"));
            }
            if !enabled_only && rows.len() != read_count(database, transaction)? {
                return Err(invalid_data("scheduler task count differs from its index"));
            }
            let mut documents = Vec::with_capacity(rows.len().min(*limit));
            for (_, raw) in rows.into_iter().take(*limit) {
                let task_id = serde_json::from_slice::<Value>(&raw)
                    .ok()
                    .and_then(|value| value.get("id").and_then(Value::as_str).map(str::to_owned))
                    .ok_or_else(|| invalid_data("scheduler task index is malformed"))?;
                let document = read_document(database, transaction, &task_id)?
                    .ok_or_else(|| invalid_data("scheduler task index target is missing"))?;
                if *enabled_only && document["enabled"].as_u64() != Some(1) {
                    return Err(invalid_data("scheduler enabled index target is disabled"));
                }
                documents.push(Value::Object(document));
            }
            let response = serde_json::to_vec(&documents)
                .map_err(|_| invalid_data("scheduler list response cannot be encoded"))?;
            if response.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
                return Err(resource_exhausted(
                    "scheduler task list response exceeds 8 MiB",
                ));
            }
            Ok(Some(response))
        }
        Request::ListAll {
            limit,
            enabled_only,
        } => {
            let namespace = if *enabled_only {
                SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE
            } else {
                SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE
            };
            let (start, end) = global_namespace_range(transaction, namespace)?;
            let rows = database.entity_scan(transaction, &start, &end, *limit)?;
            let mut documents = Vec::with_capacity(rows.len());
            for (_, raw) in rows {
                let identity: Value = serde_json::from_slice(&raw)
                    .map_err(|_| invalid_data("global scheduler task index is malformed"))?;
                let owner_user_id = identity
                    .get("owner_user_id")
                    .and_then(Value::as_u64)
                    .filter(|owner| *owner > 0)
                    .ok_or_else(|| invalid_data("global scheduler task owner is malformed"))?;
                let task_id = identity
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("global scheduler task ID is malformed"))?;
                let document =
                    read_document_for_owner(database, transaction, task_id, owner_user_id)?
                        .ok_or_else(|| {
                            invalid_data("global scheduler task index target is missing")
                        })?;
                if *enabled_only && document["enabled"].as_u64() != Some(1) {
                    return Err(invalid_data("global scheduler enabled target is disabled"));
                }
                documents.push(Value::Object(document));
            }
            let response = serde_json::to_vec(&documents)
                .map_err(|_| invalid_data("global scheduler list cannot be encoded"))?;
            if response.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
                return Err(resource_exhausted(
                    "global scheduler list response exceeds 8 MiB",
                ));
            }
            Ok(Some(response))
        }
        Request::Update {
            task_id,
            updates,
            updated_at_ms,
        } => {
            let Some(mut document) = read_document(database, transaction, task_id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            delete_indexes(database, transaction, &document)?;
            for (field, value) in updates {
                document.insert(field.clone(), value.clone());
            }
            validate_document(&document, transaction.owner_user_id(), task_id)?;
            put_document(
                database,
                transaction,
                task_id,
                &document,
                *updated_at_ms,
                None,
            )?;
            write_indexes(database, transaction, &document)?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::RecordResult {
            task_id,
            now,
            result,
            success,
            updated_at_ms,
        } => {
            let Some(mut document) = read_document(database, transaction, task_id)? else {
                return Ok(Some(br#"{"changed":false}"#.to_vec()));
            };
            let run_count = document["run_count"]
                .as_i64()
                .ok_or_else(|| invalid_data("scheduler run count is malformed"))?
                .checked_add(1)
                .ok_or_else(|| invalid_data("scheduler run count overflows"))?;
            let fail_count = document["fail_count"]
                .as_i64()
                .ok_or_else(|| invalid_data("scheduler fail count is malformed"))?
                .checked_add(i64::from(!success))
                .ok_or_else(|| invalid_data("scheduler fail count overflows"))?;
            document.insert("last_run".to_owned(), json!(now));
            document.insert("last_result".to_owned(), json!(result));
            document.insert(
                "last_status".to_owned(),
                json!(if *success { "ok" } else { "failed" }),
            );
            document.insert("run_count".to_owned(), json!(run_count));
            document.insert("fail_count".to_owned(), json!(fail_count));
            document.insert("updated_at".to_owned(), json!(now));
            put_document(
                database,
                transaction,
                task_id,
                &document,
                *updated_at_ms,
                None,
            )?;
            Ok(Some(br#"{"changed":true}"#.to_vec()))
        }
        Request::ClaimDue {
            task_id,
            lane,
            now,
            parsed_now,
            minimum_interval_seconds,
            updated_at_ms,
        } => {
            let Some(mut document) = read_document(database, transaction, task_id)? else {
                return Ok(Some(br#"{"claimed":false}"#.to_vec()));
            };
            if document["enabled"].as_i64() != Some(1) {
                return Ok(Some(br#"{"claimed":false}"#.to_vec()));
            }
            let last_field = if lane == "poll" {
                "last_poll_at"
            } else {
                "last_run"
            };
            let last_claim = document[last_field]
                .as_str()
                .ok_or_else(|| invalid_data("stored scheduler claim timestamp is malformed"))?;
            if !last_claim.is_empty() {
                let parsed_last = parse_timestamp(last_claim)
                    .ok_or_else(|| invalid_data("stored scheduler claim timestamp is invalid"))?;
                if parsed_last.aware != parsed_now.aware {
                    return Err(invalid_data("stored scheduler claim timezone differs"));
                }
                let minimum_micros = i64::try_from(*minimum_interval_seconds)
                    .ok()
                    .and_then(|value| value.checked_mul(1_000_000))
                    .ok_or_else(|| invalid_input("scheduler claim interval overflows"))?;
                if parsed_now.micros.saturating_sub(parsed_last.micros) < minimum_micros {
                    return Ok(Some(br#"{"claimed":false}"#.to_vec()));
                }
            }
            document.insert(last_field.to_owned(), json!(now));
            document.insert("updated_at".to_owned(), json!(now));
            put_document(
                database,
                transaction,
                task_id,
                &document,
                *updated_at_ms,
                None,
            )?;
            Ok(Some(br#"{"claimed":true}"#.to_vec()))
        }
        Request::PollAppend { task_id, poll } => {
            if read_document(database, transaction, task_id)?.is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "scheduled task not found",
                ));
            }
            let poll_id = next_poll_sequence(database, transaction)?;
            let mut document = poll.clone();
            document.insert("id".to_owned(), json!(poll_id));
            document.insert("task_id".to_owned(), json!(task_id));
            let poll_time = document
                .get("poll_time")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_input("scheduler poll time is malformed"))?;
            let encoded = serde_json::to_vec(&document)
                .map_err(|_| invalid_input("scheduler poll cannot be encoded"))?;
            if encoded.len() > crate::generated_tofudb_ir::MAX_SCHEDULER_POLL_DOCUMENT_BYTES {
                return Err(resource_exhausted(
                    "scheduler poll document exceeds its bound",
                ));
            }
            database.entity_put(
                transaction,
                poll_document_key(transaction, task_id, poll_id)?,
                encoded,
            )?;
            database.entity_put(
                transaction,
                poll_index_key(transaction, task_id, poll_time, poll_id)?,
                poll_id.to_le_bytes().to_vec(),
            )?;
            Ok(Some(
                serde_json::to_vec(&json!({"inserted": true, "id": poll_id}))
                    .map_err(|_| invalid_data("scheduler poll response cannot be encoded"))?,
            ))
        }
        Request::PollLog { task_id, limit } => {
            if read_document(database, transaction, task_id)?.is_none() {
                return Ok(Some(b"[]".to_vec()));
            }
            let (start, end) =
                task_history_range(transaction, SCHEDULER_POLL_TIME_INDEX_NAMESPACE, task_id)?;
            let rows = database.entity_scan(transaction, &start, &end, *limit)?;
            let mut polls = Vec::with_capacity(rows.len());
            for (_, raw) in rows {
                if raw.len() != 8 {
                    return Err(invalid_data("scheduler poll index is malformed"));
                }
                let poll_id = u64::from_le_bytes(raw.try_into().expect("length checked"));
                let document = database
                    .entity_get(
                        transaction,
                        &poll_document_key(transaction, task_id, poll_id)?,
                    )?
                    .ok_or_else(|| invalid_data("scheduler poll index target is missing"))?;
                polls.push(
                    serde_json::from_slice::<Value>(&document)
                        .map_err(|_| invalid_data("scheduler poll document is malformed"))?,
                );
            }
            Ok(Some(serde_json::to_vec(&polls).map_err(|_| {
                invalid_data("scheduler poll log cannot be encoded")
            })?))
        }
        Request::Delete { task_id } => {
            let Some(document) = read_document(database, transaction, task_id)? else {
                return Ok(Some(br#"{"deleted":false}"#.to_vec()));
            };
            delete_task_state(database, transaction, task_id, &document)?;
            Ok(Some(br#"{"deleted":true}"#.to_vec()))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::parse_timestamp;

    #[test]
    fn timestamp_parser_matches_iso_claim_ordering_and_timezone_rules() {
        let naive = parse_timestamp("2026-02-28T23:59:59.123456").unwrap();
        let later = parse_timestamp("2026-03-01 00:00:00").unwrap();
        assert!(!naive.aware);
        assert_eq!(later.micros - naive.micros, 876_544);

        let utc = parse_timestamp("2026-09-04T08:00:00Z").unwrap();
        let offset = parse_timestamp("2026-09-04T16:00:00+08:00").unwrap();
        assert!(utc.aware && offset.aware);
        assert_eq!(utc.micros, offset.micros);
    }

    #[test]
    fn timestamp_parser_rejects_invalid_calendar_and_timezone_values() {
        for value in [
            "2025-02-29T00:00:00",
            "2026-13-01T00:00:00",
            "2026-01-01T24:00:00",
            "2026-01-01T00:00:60",
            "2026-01-01T00:00:00+24:00",
            "2026-01-01T00:00:00.",
        ] {
            assert!(parse_timestamp(value).is_none(), "accepted {value}");
        }
        assert!(parse_timestamp("2024-02-29T00:00:00").is_some());
    }
}
