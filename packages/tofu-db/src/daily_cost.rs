//! Owner-scoped, date-ordered daily cost cache.
//!
//! ISO-shaped dates are physical keys, so month and latest reads use bounded
//! B+Tree ranges. An exact owner count makes whole-cache deletion constant in
//! retained memory and avoids a document-sized delete scan.

use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    DAILY_COST_COUNT_NAMESPACE, DAILY_COST_DOCUMENT_NAMESPACE, MAX_DAILY_COST_DOCUMENT_BYTES,
    MAX_DAILY_COST_MONTH_ROWS, MAX_DAILY_COST_PERSISTED_DATES, MAX_TRANSACTION_IR_LITERAL_BYTES,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "daily_cost_cache";
const COUNT_KEY: &[u8] = b"count";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn document_key(transaction: &AuthorityTransaction, date: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_DOCUMENT_NAMESPACE,
        date.as_bytes(),
    )
}

fn document_range(
    transaction: &AuthorityTransaction,
    prefix: &[u8],
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_DOCUMENT_NAMESPACE,
        prefix,
    )
}

fn count_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_COUNT_NAMESPACE,
        COUNT_KEY,
    )
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    match database.entity_get(transaction, &count_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("daily-cost count is malformed")),
    }
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    count: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        count_key(transaction)?,
        count.to_le_bytes().to_vec(),
    )
}

pub(crate) fn valid_date(date: &str) -> bool {
    let characters = date.chars().collect::<Vec<_>>();
    characters.len() == 10
        && characters[4] == '-'
        && characters[7] == '-'
        && characters
            .iter()
            .copied()
            .enumerate()
            .all(|(index, character)| {
                matches!(index, 4 | 7)
                    || crate::generated_unicode_casefold::python_is_digit(character)
            })
}

fn decode_document(raw: &[u8], expected_date: &str) -> io::Result<Value> {
    let document: Value = serde_json::from_slice(raw)
        .map_err(|_| invalid_data("daily-cost document is malformed"))?;
    let object = document
        .as_object()
        .ok_or_else(|| invalid_data("daily-cost document is not an object"))?;
    if object.get("date").and_then(Value::as_str) != Some(expected_date)
        || !object.get("cost").is_some_and(Value::is_number)
        || !object.get("conversations").is_some_and(Value::is_object)
        || !object.get("computed_at").is_some_and(Value::is_u64)
    {
        return Err(invalid_data("daily-cost document fields are malformed"));
    }
    Ok(document)
}

fn get_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    date: &str,
) -> io::Result<Option<Value>> {
    let key = document_key(transaction, date)?;
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        date,
        transaction.owner_user_id(),
        MAX_DAILY_COST_DOCUMENT_BYTES,
    )?
    .map(|raw| decode_document(&raw, date))
    .transpose()
}

pub(crate) struct UpsertRequest {
    pub date: String,
    pub cost: f64,
    pub conversations: Map<String, Value>,
    pub computed_at: u64,
    pub updated_at_ms: u64,
}

pub(crate) fn upsert(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &UpsertRequest,
) -> io::Result<Vec<u8>> {
    if !valid_date(&request.date)
        || !request.cost.is_finite()
        || !(0.0..=1_000_000_000.0).contains(&request.cost)
        || request.updated_at_ms == 0
    {
        return Err(invalid_input("invalid daily-cost mutation"));
    }
    let key = document_key(transaction, &request.date)?;
    let is_new = database.entity_get(transaction, &key)?.is_none();
    let value_json = serde_json::to_vec(&json!({
        "date": request.date,
        "cost": request.cost,
        "conversations": request.conversations,
        "computed_at": request.computed_at,
    }))
    .map_err(|_| invalid_input("daily-cost document cannot be encoded"))?;
    if value_json.len() > MAX_DAILY_COST_DOCUMENT_BYTES {
        return Err(resource_exhausted("daily-cost document exceeds its bound"));
    }
    if is_new {
        let next = read_count(database, transaction)?
            .checked_add(1)
            .ok_or_else(|| resource_exhausted("daily-cost count overflow"))?;
        write_count(database, transaction, next)?;
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: request.date.clone(),
            value_json,
            expected_version: None,
            updated_at_ms: request.updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_DAILY_COST_DOCUMENT_BYTES,
    )?;
    Ok(br#"{"saved":true}"#.to_vec())
}

pub(crate) fn month(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    year: u64,
    month: u64,
) -> io::Result<Vec<u8>> {
    if !(1970..=9999).contains(&year) || !(1..=12).contains(&month) {
        return Err(invalid_input("invalid daily-cost month"));
    }
    let prefix = format!("{year:04}-{month:02}-");
    let (start, end) = document_range(transaction, prefix.as_bytes())?;
    let rows = database.entity_scan(transaction, &start, &end, MAX_DAILY_COST_MONTH_ROWS + 1)?;
    if rows.len() > MAX_DAILY_COST_MONTH_ROWS {
        return Err(invalid_data(
            "daily-cost month exceeds its syntactic date bound",
        ));
    }
    let mut values = Vec::with_capacity(rows.len());
    let mut response_bytes = 2_usize;
    for (key, _) in rows {
        let date = std::str::from_utf8(key.key_bytes())
            .map_err(|_| invalid_data("daily-cost date key is malformed"))?;
        if !valid_date(date) || !date.starts_with(&prefix) {
            return Err(invalid_data("daily-cost date key is inconsistent"));
        }
        let value = get_value(database, transaction, date)?
            .ok_or_else(|| invalid_data("daily-cost document disappeared"))?;
        response_bytes = response_bytes
            .checked_add(
                serde_json::to_vec(&value)
                    .map_err(|_| invalid_data("daily-cost response cannot be encoded"))?
                    .len()
                    + 1,
            )
            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
            .ok_or_else(|| resource_exhausted("daily-cost month response exceeds 8 MiB"))?;
        values.push(value);
    }
    serde_json::to_vec(&values).map_err(|_| invalid_data("daily-cost month cannot be encoded"))
}

pub(crate) fn latest(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<Vec<u8>>> {
    let (start, end) = document_range(transaction, b"")?;
    let Some((key, _)) = database
        .entity_scan_reverse(transaction, &start, &end, 1)?
        .into_iter()
        .next()
    else {
        return Ok(None);
    };
    let date = std::str::from_utf8(key.key_bytes())
        .map_err(|_| invalid_data("daily-cost date key is malformed"))?;
    let value = get_value(database, transaction, date)?
        .ok_or_else(|| invalid_data("daily-cost document disappeared"))?;
    serde_json::to_vec(&value)
        .map(Some)
        .map_err(|_| invalid_data("daily-cost latest response cannot be encoded"))
}

pub(crate) fn persisted_dates(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    dates: &[String],
) -> io::Result<Vec<u8>> {
    if dates.len() > MAX_DAILY_COST_PERSISTED_DATES || dates.iter().any(|date| !valid_date(date)) {
        return Err(invalid_input("invalid daily-cost date probe"));
    }
    let mut existing = Vec::new();
    for date in dates {
        if database
            .entity_get(transaction, &document_key(transaction, date)?)?
            .is_some()
        {
            existing.push(date);
        }
    }
    serde_json::to_vec(&json!({"dates": existing}))
        .map_err(|_| invalid_data("daily-cost date response cannot be encoded"))
}

pub(crate) fn delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    date: Option<&str>,
) -> io::Result<Vec<u8>> {
    let deleted = if let Some(date) = date {
        if !valid_date(date) {
            return Err(invalid_input("invalid daily-cost date"));
        }
        let key = document_key(transaction, date)?;
        if database.entity_get(transaction, &key)?.is_some() {
            database.entity_delete(transaction, key)?;
            let next = read_count(database, transaction)?
                .checked_sub(1)
                .ok_or_else(|| invalid_data("daily-cost count is inconsistent"))?;
            write_count(database, transaction, next)?;
            1
        } else {
            0
        }
    } else {
        let count = read_count(database, transaction)?;
        if count > 0 {
            let (start, end) = document_range(transaction, b"")?;
            database.entity_retire_range(transaction, &start, &end)?;
            write_count(database, transaction, 0)?;
        }
        count
    };
    serde_json::to_vec(&json!({"deleted": deleted}))
        .map_err(|_| invalid_data("daily-cost delete response cannot be encoded"))
}

#[cfg(test)]
mod tests {
    use super::valid_date;

    #[test]
    fn date_validation_matches_the_legacy_syntactic_contract() {
        assert!(valid_date("2026-08-13"));
        assert!(valid_date("2026-99-99"));
        assert!(!valid_date("2026-8-13"));
        assert!(!valid_date("2026-08-aa"));
        assert!(valid_date("２０２６-08-13"));
        assert!(valid_date("²026-08-13"));
        assert!(!valid_date("½026-08-13"));
    }
}
