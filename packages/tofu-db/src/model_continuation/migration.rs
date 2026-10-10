//! Offline model-continuation snapshot restoration for the exclusive operator
//! importer.
//!
//! The source stores one latest checkpoint per owner/conversation/lane as a
//! generic `storage_records` row in the `model.continuation.v1` namespace; its
//! record key carries the owner id and the collision-resistant
//! conversation/lane digests, the value is the exact canonical projection, the
//! version is the settled-Turn ordinal CAS frontier, and the timestamp is the
//! last write. This adapter replays that exact snapshot into the native
//! continuation record document and rebuilds nothing else: model continuation
//! is a live-frontier CAS over a single record and maintains no index
//! namespaces beyond the record document. Insert-or-match keeps replays
//! idempotent, and every catalog page revalidates its row target.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use super::{
    encode_projection, invalid_data, invalid_input, json_record, materialize_record, read_record, read_record_bytes, write_record,
    CONTINUATION_NAMESPACE, MAX_PROJECTION_BYTES,
};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{MAX_ENTITY_KEY_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    record_key: String,
    value_json: String,
    version: u64,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    record_key: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "continuation snapshot differs")
}

/// Split `{user_id}:{conversation_digest}:{lane_digest}` and bind the owner.
/// The native entity key already scopes the owner, so the raw key keeps only
/// the two digests; the leading owner id must match the transaction scope.
fn parse_record_key(record_key: &str, owner_user_id: u64) -> io::Result<String> {
    let mut parts = record_key.splitn(3, ':');
    let owner = parts
        .next()
        .ok_or_else(|| invalid_input("continuation record key is malformed"))?;
    let owner: u64 = owner
        .parse()
        .ok()
        .filter(|parsed| *parsed > 0)
        .ok_or_else(|| invalid_input("continuation record key owner is malformed"))?;
    if owner != owner_user_id {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "continuation snapshot owner is outside the operator scope",
        ));
    }
    let conversation_digest = parts
        .next()
        .ok_or_else(|| invalid_input("continuation record key is malformed"))?;
    let lane_digest = parts
        .next()
        .ok_or_else(|| invalid_input("continuation record key is malformed"))?;
    let valid_digest = |digest: &str| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    };
    if !valid_digest(conversation_digest) || !valid_digest(lane_digest) {
        return Err(invalid_input("continuation record key digest is malformed"));
    }
    let raw = format!("{conversation_digest}:{lane_digest}");
    if raw.len() > MAX_ENTITY_KEY_BYTES {
        return Err(invalid_input("continuation record key exceeds its bound"));
    }
    Ok(raw)
}

struct Prepared {
    raw_key: String,
    projection: serde_json::Map<String, Value>,
    frontier: u64,
    updated_at_ms: u64,
    /// The exact source projection bytes, preserved for byte-exact snapshots.
    value_json: Vec<u8>,
}

/// Parse and cross-check one exact source snapshot. The version is the CAS
/// frontier and must be a positive u64; the projection must satisfy the same
/// bounded canonical encoding the live authority accepts.
fn prepare(request: RestoreRequest, owner_user_id: u64) -> io::Result<Prepared> {
    if request.version == 0
        || request.updated_at_ms == 0
        || request.value_json.len() > MAX_PROJECTION_BYTES
    {
        return Err(invalid_input("invalid continuation snapshot metadata"));
    }
    let raw_key = parse_record_key(&request.record_key, owner_user_id)?;
    let raw = request.value_json.into_bytes();
    let projection = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("continuation snapshot is not an object"))?;
    // Reject anything the live authority would never store; re-encoding must
    // reproduce the exact source bytes so the snapshot stays byte-identical.
    let encoded = encode_projection(&Value::Object(projection.clone()))
        .ok_or_else(|| invalid_input("continuation projection is invalid or over budget"))?;
    if encoded != raw {
        return Err(invalid_input("continuation projection is not canonical"));
    }
    Ok(Prepared {
        raw_key,
        projection,
        frontier: request.version,
        updated_at_ms: request.updated_at_ms,
        value_json: raw,
    })
}

fn native_record(prepared: &Prepared) -> Vec<u8> {
    let record = json_record(
        prepared.frontier,
        &prepared.projection,
        prepared.updated_at_ms,
    );
    serde_json::to_vec(&record).expect("continuation record always encodes")
}

fn snapshot_value_for(prepared: &Prepared, owner_user_id: u64) -> Value {
    json!({
        "record_key": format!("{owner_user_id}:{}", prepared.raw_key),
        "value_json": String::from_utf8(prepared.value_json.clone())
            .expect("continuation projection is UTF-8"),
        "version": prepared.frontier,
        "updated_at_ms": prepared.updated_at_ms,
    })
}

/// Verify the single derived record document for one snapshot and fail closed
/// when the stored native record disagrees with the imported state.
fn verify_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    prepared: &Prepared,
) -> io::Result<()> {
    let key = continuation_key_from_raw(transaction, &prepared.raw_key)?;
    let stored = read_record_bytes(database, transaction, &key)?
        .ok_or_else(|| invalid_data("continuation snapshot document is missing"))?;
    if materialize_record(database, transaction, &key, &stored)? != native_record(prepared) {
        return Err(invalid_data("continuation snapshot document differs"));
    }
    // The live reader must accept the reconstructed record and return the
    // identical canonical projection bytes.
    let Some((frontier, projection)) = read_record(database, transaction, &key)? else {
        return Err(invalid_data("continuation snapshot document is unreadable"));
    };
    if frontier != prepared.frontier {
        return Err(invalid_data("continuation snapshot frontier differs"));
    }
    // The lifecycle lane directory must point at the imported record or later
    // conversation/turn deletion would leak the derived continuation.
    if database
        .entity_get(transaction, &lane_index_key_from_raw(transaction, &prepared.raw_key)?)?
        .as_deref()
        != Some(b"".as_slice())
    {
        return Err(invalid_data("continuation lane directory entry is missing"));
    }
    let encoded = encode_projection(&Value::Object(projection))
        .ok_or_else(|| invalid_data("continuation snapshot projection is invalid"))?;
    if encoded != prepared.value_json {
        return Err(invalid_data("continuation snapshot projection differs"));
    }
    Ok(())
}

fn continuation_key_from_raw(
    transaction: &AuthorityTransaction,
    raw_key: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONTINUATION_NAMESPACE,
        raw_key.as_bytes(),
    )
}

fn lane_index_key_from_raw(
    transaction: &AuthorityTransaction,
    raw_key: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        crate::model_continuation::LANE_INDEX_NAMESPACE,
        raw_key.as_bytes(),
    )
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid continuation restore request"))?;
    let prepared = prepare(request, transaction.owner_user_id())?;
    let key = continuation_key_from_raw(transaction, &prepared.raw_key)?;
    if let Some(stored) = read_record_bytes(database, transaction, &key)? {
        // Insert-or-match: an identical replay performs no document commit, a
        // divergent occupant fails closed rather than overwriting the live
        // frontier. The derived lane directory entry is idempotently ensured
        // so checkpoints recorded before its introduction still resume.
        if stored != native_record(&prepared) {
            return Err(snapshot_conflict());
        }
        database.entity_put(
            transaction,
            lane_index_key_from_raw(transaction, &prepared.raw_key)?,
            Vec::new(),
        )?;
        verify_record(database, transaction, &prepared)?;
        return Ok(false);
    }
    write_record(database, transaction, key, prepared.frontier, &prepared.projection, prepared.updated_at_ms)?;
    database.entity_put(
        transaction,
        lane_index_key_from_raw(transaction, &prepared.raw_key)?,
        Vec::new(),
    )?;
    verify_record(database, transaction, &prepared)?;
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid continuation get request"))?;
    let raw_key = parse_record_key(&request.record_key, transaction.owner_user_id())?;
    let key = continuation_key_from_raw(transaction, &raw_key)?;
    let Some((frontier, projection)) = read_record(database, transaction, &key)? else {
        return Ok(Value::Null);
    };
    let raw = read_record_bytes(database, transaction, &key)?
        .ok_or_else(|| invalid_data("continuation snapshot document is missing"))?;
    let raw = materialize_record(database, transaction, &key, &raw)?;
    let updated_at_ms = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.get("updatedAtMs").and_then(Value::as_u64))
        .ok_or_else(|| invalid_data("continuation snapshot timestamp is malformed"))?;
    let value_json = encode_projection(&Value::Object(projection))
        .ok_or_else(|| invalid_data("continuation snapshot projection is invalid"))?;
    let prepared = Prepared {
        raw_key,
        projection: serde_json::from_slice::<Value>(&value_json)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("continuation snapshot projection is malformed"))?,
        frontier,
        updated_at_ms,
        value_json,
    };
    verify_record(database, transaction, &prepared)?;
    Ok(snapshot_value_for(&prepared, transaction.owner_user_id()))
}

fn cursor_start(cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let encoded = cursor
        .strip_prefix("continuations:")
        .ok_or_else(|| invalid_input("invalid continuation catalog cursor"))?;
    if encoded.is_empty()
        || encoded.len() > MAX_ENTITY_KEY_BYTES * 2
        || !encoded.len().is_multiple_of(2)
    {
        return Err(invalid_input("invalid continuation catalog cursor"));
    }
    let mut raw = encoded
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            u8::from_str_radix(
                std::str::from_utf8(pair)
                    .map_err(|_| invalid_input("invalid continuation catalog cursor"))?,
                16,
            )
            .map_err(|_| invalid_input("invalid continuation catalog cursor"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    if raw.len() < MAX_ENTITY_KEY_BYTES {
        raw.push(0);
    } else {
        let index = raw
            .iter()
            .rposition(|byte| *byte != 255)
            .ok_or_else(|| invalid_input("continuation catalog cursor has no successor"))?;
        raw[index] += 1;
        raw.truncate(index + 1);
    }
    Ok(raw)
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid continuation scan request"))?;
    let suffix = cursor_start(request.cursor.as_deref())?;
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONTINUATION_NAMESPACE,
        &suffix,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONTINUATION_NAMESPACE,
        b"",
    )?;
    // At most four bounded 8 MiB documents are validated per catalog call.
    let page_limit = MIGRATION_SESSION_MAX_PAGE_ROWS.min(4);
    let page = database.entity_scan(transaction, &start, &end, page_limit)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        // Per-row revalidation: the physical key must be the canonical
        // continuation record identity and the stored body must decode.
        let raw_key = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("continuation catalog key is malformed"))?;
        if physical != &continuation_key_from_raw(transaction, raw_key)? {
            return Err(invalid_data("continuation catalog identity differs"));
        }
        let stored = read_record_bytes(database, transaction, physical)?
            .ok_or_else(|| invalid_data("continuation catalog record disappeared"))?;
        let _: serde_json::Map<String, Value> = serde_json::from_slice::<Value>(&stored)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .filter(|record| {
                record.get("frontier").and_then(Value::as_u64).is_some()
                    && record.get("projection").is_some_and(Value::is_object)
                    && record.get("updatedAtMs").and_then(Value::as_u64).is_some()
            })
            .ok_or_else(|| invalid_data("continuation catalog record is malformed"))?;
        rows.push(json!({"record_key": format!("{}:{raw_key}", transaction.owner_user_id())}));
    }
    let cursor = if page.len() == page_limit {
        let encoded: String = page
            .last()
            .expect("a full continuation catalog page has a last key")
            .0
            .key_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Some(format!("continuations:{encoded}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.continuation.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.continuation.get" => get(database, &mut transaction, payload),
        "migration.continuation.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown continuation migration operation")),
    }
}

#[cfg(test)]
#[path = "migration_tests.rs"]
mod tests;
