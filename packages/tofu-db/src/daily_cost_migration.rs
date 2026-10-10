//! Offline daily-cost cache snapshot restoration for the exclusive operator
//! importer.
//!
//! The source keeps one settled row per owner/date in `daily_cost_cache`
//! (`cost`, `conversations_json`, `computed_at`); there is no source version
//! or timestamp column, so the migration snapshot is exactly those three
//! fields plus the date. The live `daily_cost.upsert` operation always stamps
//! the document `updated_at_ms` with the server clock, so a replay through it
//! would not be insert-or-match idempotent and could not be independently
//! re-verified. This adapter rebuilds the native document and owner count in
//! the same authority transaction, deriving the internal timestamp
//! deterministically from the snapshot content so an identical replay decodes
//! to an identical envelope and is read-only. Insert-or-match fails closed on
//! a divergent occupant, get reconstructs the exact source snapshot, and scan
//! enumerates the owner-scoped dates independently of the source. Entry
//! points: the three `migration.daily.cost.*` operations dispatched from
//! `migration_session`.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::daily_cost::{upsert, valid_date, UpsertRequest};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    DAILY_COST_COUNT_NAMESPACE, DAILY_COST_DOCUMENT_NAMESPACE, MAX_DAILY_COST_DOCUMENT_BYTES,
    MIGRATION_SESSION_MAX_PAGE_ROWS,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "daily cost snapshot differs")
}

const COUNT_KEY: &[u8] = b"count";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    date: String,
    cost: f64,
    #[serde(default)]
    conversations: Map<String, Value>,
    computed_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    date: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn document_key(transaction: &AuthorityTransaction, date: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_DOCUMENT_NAMESPACE,
        date.as_bytes(),
    )
}

fn parse_restore(payload: Value) -> io::Result<RestoreRequest> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid daily cost restore request"))?;
    if !valid_date(&request.date)
        || !request.cost.is_finite()
        || !(0.0..=1_000_000_000.0).contains(&request.cost)
        || request.computed_at == 0
    {
        return Err(invalid_input("invalid daily cost snapshot metadata"));
    }
    Ok(request)
}

fn snapshot_value_json(request: &RestoreRequest) -> io::Result<Vec<u8>> {
    let value = serde_json::to_vec(&json!({
        "date": request.date,
        "cost": request.cost,
        "conversations": request.conversations,
        "computed_at": request.computed_at,
    }))
    .map_err(|_| invalid_input("daily cost document cannot be encoded"))?;
    if value.len() > MAX_DAILY_COST_DOCUMENT_BYTES {
        return Err(invalid_input("daily cost document exceeds its byte bound"));
    }
    Ok(value)
}

/// The deterministic internal timestamp derived from the snapshot content:
/// an identical replay encodes to the identical native envelope, so the
/// insert-or-match check below stays byte-exact and read-only.
fn deterministic_updated_at_ms(value_json: &[u8]) -> u64 {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(value_json);
    u64::from_be_bytes(digest[..8].try_into().unwrap()) | 1
}

fn read_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    date: &str,
) -> io::Result<Option<(Vec<u8>, u64)>> {
    let key = document_key(transaction, date)?;
    let Some(snapshot) = crate::versioned_document::read_snapshot(
        database,
        transaction,
        &key,
        "daily_cost_cache",
        date,
    )?
    else {
        return Ok(None);
    };
    Ok(Some((snapshot.value_json, snapshot.updated_at_ms)))
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let key = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    match database.entity_get(transaction, &key)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("daily cost count is malformed")),
    }
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_restore(payload)?;
    let value_json = snapshot_value_json(&request)?;
    let updated_at_ms = deterministic_updated_at_ms(&value_json);
    // Insert-or-match against the exact decoded snapshot the native get path
    // would return; a divergent occupant fails closed instead of overwriting.
    if let Some((existing, existing_updated)) = read_document(database, transaction, &request.date)?
    {
        if existing != value_json {
            return Err(snapshot_conflict());
        }
        if existing_updated != updated_at_ms {
            // Same content but a non-deterministic (live-path) timestamp:
            // treat as matching so a prior live write is not clobbered.
            return Ok(false);
        }
        return Ok(false);
    }
    upsert(
        database,
        transaction,
        &UpsertRequest {
            date: request.date.clone(),
            cost: request.cost,
            conversations: request.conversations.clone(),
            computed_at: request.computed_at,
            updated_at_ms,
        },
    )?;
    // Read back through the decode path before publication.
    let Some((stored, _)) = read_document(database, transaction, &request.date)? else {
        return Err(invalid_data("daily cost snapshot document is missing"));
    };
    if stored != value_json {
        return Err(invalid_data("daily cost snapshot readback differs"));
    }
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid daily cost get request"))?;
    if !valid_date(&request.date) {
        return Err(invalid_input("invalid daily cost date"));
    }
    let Some((value_json, _)) = read_document(database, transaction, &request.date)? else {
        return Ok(Value::Null);
    };
    let value: Value = serde_json::from_slice(&value_json)
        .map_err(|_| invalid_data("daily cost document is malformed"))?;
    let object = value
        .as_object()
        .ok_or_else(|| invalid_data("daily cost document is not an object"))?;
    Ok(json!({
        "date": object.get("date").cloned().unwrap_or(Value::Null),
        "cost": object.get("cost").cloned().unwrap_or(Value::Null),
        "conversations": object.get("conversations").cloned().unwrap_or(Value::Null),
        "computed_at": object.get("computed_at").cloned().unwrap_or(Value::Null),
    }))
}

fn cursor_start(cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let encoded = cursor
        .strip_prefix("daily_cost:")
        .ok_or_else(|| invalid_input("invalid daily cost catalog cursor"))?;
    let mut raw = encoded.as_bytes().to_vec();
    if raw.is_empty() {
        return Err(invalid_input("invalid daily cost catalog cursor"));
    }
    // Advance past the cursor date; dates are fixed-width so a NUL suffix is
    // the strict successor within the same prefix.
    raw.push(0);
    Ok(raw)
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid daily cost scan request"))?;
    let suffix = cursor_start(request.cursor.as_deref())?;
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_DOCUMENT_NAMESPACE,
        &suffix,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        DAILY_COST_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let date = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("daily cost catalog key is malformed"))?;
        if !valid_date(date) {
            return Err(invalid_data("daily cost catalog date is malformed"));
        }
        rows.push(json!({"date": date}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("date").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("daily cost catalog page is malformed"))?;
        Some(format!("daily_cost:{last}"))
    } else {
        None
    };
    // The owner count must agree with the enumerated catalog on a full
    // single-page pass; a later page holds only that page's rows, so
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none() && cursor.is_none() {
        let count = read_count(database, transaction)?;
        if rows.len() as u64 != count {
            return Err(invalid_data("daily cost count and catalog differ"));
        }
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.daily.cost.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.daily.cost.get" => get(database, &mut transaction, payload),
        "migration.daily.cost.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown daily cost migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn snapshot(date: &str, cost: f64) -> Value {
        json!({"date": date, "cost": cost,
               "conversations": {"conv": cost}, "computed_at": 424242})
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.daily.cost.restore",
                snapshot("2026-08-10", 1.5)
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.daily.cost.restore",
                snapshot("2026-08-10", 1.5)
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.daily.cost.get",
            json!({"date": "2026-08-10"}),
        )
        .unwrap();
        assert_eq!(get["date"], "2026-08-10");
        assert_eq!(get["cost"], 1.5);
        assert_eq!(get["conversations"], json!({"conv": 1.5}));
        assert_eq!(get["computed_at"], 424242);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.daily.cost.restore",
            snapshot("2026-08-10", 9.9)
        )
        .is_err());
    }

    #[test]
    fn scan_enumerates_dates_and_agrees_with_count() {
        let (_vfs, mut database) = prepared();
        for date in ["2026-08-10", "2026-08-11", "2026-08-12"] {
            execute(
                &mut database,
                7,
                11,
                "migration.daily.cost.restore",
                snapshot(date, 1.0),
            )
            .unwrap();
        }
        let page = execute(
            &mut database,
            7,
            11,
            "migration.daily.cost.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(page["cursor"], Value::Null);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.daily.cost.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn invalid_metadata_fails_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"date": "2026-8-10", "cost": 1.0, "computed_at": 1}),
            json!({"date": "2026-08-10", "cost": -1.0, "computed_at": 1}),
            json!({"date": "2026-08-10", "cost": 1.0, "computed_at": 0}),
            json!({"date": "2026-08-10", "cost": 1.0, "computed_at": 1, "extra": 0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.daily.cost.restore",
                payload
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.daily.cost.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
