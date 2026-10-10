//! Offline paper budget snapshot restoration for the exclusive operator
//! importer.
//!
//! The source keeps two single-record tenant-global budget ledgers as generic
//! `storage_records` rows: the retained build budget in
//! `paper.build.budget.v1` (`{"records": {key: reserved_bytes}}`) and the saved
//! document budget in `paper.document.budget.v1` (`{"count": n,
//! "associations": m}`). The native authority stores them in a different
//! physical layout — a fixed 16-byte capacity ledger plus one 8-byte
//! reservation record per retained build — so a rebuild cannot replay the raw
//! record bytes. This adapter parses the exact source snapshot, validates it
//! against the same bounds the live paths enforce, cross-validates every build
//! reservation against the restored artifact metadata, and rewrites the native
//! ledger, reservation claims and migration provenance in one transaction.
//! Insert-or-match keeps replays idempotent: an identical snapshot is
//! read-only, a divergent or foreign occupant fails closed, and the get
//! operations reconstruct the exact source snapshot for independent
//! verification. Entry points: the four `migration.paper.*.budget.*`
//! operations dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_paper_build as limits;
use crate::generated_tofudb_ir::{MAX_ENTITY_KEY_BYTES, MAX_TRANSACTION_IR_LITERAL_BYTES, MAX_ENTITY_RANGE_ROWS};
use crate::paper_build_artifact;
use crate::paper_build_receipt::lower_hex;
use crate::paper_document;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "paper budget snapshot differs")
}

const CAPACITY_KEY: &str = "capacity";
/// Provenance record holding the source row metadata (version/timestamp) the
/// native ledger layout has no room for; the live paths never read it and it
/// sorts outside the `r:` reservation range.
const MIGRATION_KEY: &str = "migration";
/// A migration packet has its ordinary literal byte bound, independent of
/// retained record counts.
const MAX_BUILD_LEDGER_JSON_BYTES: usize =
    MAX_TRANSACTION_IR_LITERAL_BYTES;
const MAX_DOCUMENT_LEDGER_JSON_BYTES: usize = 256;

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

fn parse_restore(payload: Value, maximum_json_bytes: usize) -> io::Result<RestoreRequest> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper budget restore request"))?;
    if request.record_key != CAPACITY_KEY
        || request.version == 0
        || request.updated_at_ms == 0
        || request.value_json.len() > maximum_json_bytes
    {
        return Err(invalid_input("invalid paper budget snapshot metadata"));
    }
    Ok(request)
}

fn parse_get(payload: Value) -> io::Result<GetRequest> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper budget get request"))?;
    if request.record_key != CAPACITY_KEY {
        return Err(invalid_input("invalid paper budget record key"));
    }
    Ok(request)
}

fn migration_metadata(request: &RestoreRequest) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "record_key": CAPACITY_KEY,
        "version": request.version,
        "updated_at_ms": request.updated_at_ms,
    }))
    .expect("paper budget migration metadata always encodes")
}

fn parse_migration_metadata(raw: &[u8]) -> io::Result<(u64, u64)> {
    let value: Value = serde_json::from_slice(raw)
        .map_err(|_| invalid_data("paper budget migration metadata is malformed"))?;
    let object = value
        .as_object()
        .filter(|object| object.len() == 3)
        .ok_or_else(|| invalid_data("paper budget migration metadata is malformed"))?;
    if object.get("record_key").and_then(Value::as_str) != Some(CAPACITY_KEY) {
        return Err(invalid_data("paper budget migration metadata is malformed"));
    }
    let version = object
        .get("version")
        .and_then(Value::as_u64)
        .filter(|version| *version > 0)
        .ok_or_else(|| invalid_data("paper budget migration metadata is malformed"))?;
    let updated_at_ms = object
        .get("updated_at_ms")
        .and_then(Value::as_u64)
        .filter(|timestamp| *timestamp > 0)
        .ok_or_else(|| invalid_data("paper budget migration metadata is malformed"))?;
    Ok((version, updated_at_ms))
}

/// Match one expected entity against the target: identical content replays
/// read-only, missing content is queued for insertion, anything else fails
/// closed instead of overwriting the live budget.
fn match_or_queue(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    expected: &[u8],
    writes: &mut Vec<(EntityKey, Vec<u8>)>,
) -> io::Result<()> {
    match database.entity_get(transaction, key)? {
        None => writes.push((key.clone(), expected.to_vec())),
        Some(existing) if existing == expected => {}
        Some(_) => return Err(snapshot_conflict()),
    }
    Ok(())
}

struct BuildSnapshot {
    /// Full `{owner}:{document_id}:{build_id}` reservation key → reserved
    /// bytes, exactly as the live reservation records store it.
    reservations: BTreeMap<String, u64>,
    total_bytes: u64,
}

/// Bind `{owner}:{document_id}:{build_id}` to the operator scope and enforce
/// the identity shape the live receipt validation requires.
fn parse_reservation_key(key: &str, owner_user_id: u64) -> io::Result<()> {
    if key.is_empty() || key.len() > MAX_ENTITY_KEY_BYTES {
        return Err(invalid_input(
            "paper build budget reservation key is malformed",
        ));
    }
    let mut parts = key.split(':');
    let owner = parts
        .next()
        .and_then(|part| part.parse::<u64>().ok())
        .filter(|parsed| *parsed > 0)
        .ok_or_else(|| invalid_input("paper build budget reservation owner is malformed"))?;
    if owner != owner_user_id {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "paper build budget reservation is outside the operator scope",
        ));
    }
    let document = parts
        .next()
        .filter(|part| lower_hex(part, 32, 64))
        .ok_or_else(|| invalid_input("paper build budget reservation document is malformed"))?;
    let build = parts
        .next()
        .filter(|part| lower_hex(part, 32, 32))
        .ok_or_else(|| invalid_input("paper build budget reservation build is malformed"))?;
    let _ = (document, build);
    if parts.next().is_some() {
        return Err(invalid_input(
            "paper build budget reservation key is malformed",
        ));
    }
    Ok(())
}

fn parse_build_ledger(request: &RestoreRequest, owner_user_id: u64) -> io::Result<BuildSnapshot> {
    let value: Value = serde_json::from_str(&request.value_json)
        .map_err(|_| invalid_input("paper build budget ledger is not an object"))?;
    let records = value
        .as_object()
        .filter(|object| object.len() == 1)
        .and_then(|object| object.get("records"))
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_input("paper build budget ledger is not an object"))?;
    let mut reservations = BTreeMap::new();
    let mut total_bytes = 0u64;
    for (key, bytes) in records {
        parse_reservation_key(key, owner_user_id)?;
        let bytes = bytes
            .as_u64()
            .filter(|bytes| *bytes > 0)
            .ok_or_else(|| invalid_input("paper build budget reservation bytes are invalid"))?;
        total_bytes = total_bytes
            .checked_add(bytes)
            .ok_or_else(|| invalid_input("paper build budget exceeds its byte bound"))?;
        reservations.insert(key.clone(), bytes);
    }
    Ok(BuildSnapshot {
        reservations,
        total_bytes,
    })
}

fn ledger_bytes(count: u64, bytes: u64) -> Vec<u8> {
    let mut raw = Vec::with_capacity(16);
    raw.extend_from_slice(&count.to_be_bytes());
    raw.extend_from_slice(&bytes.to_be_bytes());
    raw
}

fn build_ledger_json(reservations: &BTreeMap<String, u64>) -> String {
    let mut out = String::with_capacity(64 + reservations.len() * 32);
    out.push_str("{\"records\":{");
    for (index, (key, bytes)) in reservations.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str(
            &serde_json::to_string(key).expect("paper budget reservation key always encodes"),
        );
        out.push(':');
        out.push_str(&bytes.to_string());
    }
    out.push_str("}}");
    out
}

fn read_build_reservations(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<BTreeMap<String, u64>> {
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        limits::BUDGET_NAMESPACE,
        b"r:",
    )?;
    let mut reservations = BTreeMap::new();
    let mut cursor = start;
    loop {
        let page = database.entity_scan(transaction, &cursor, &end, MAX_ENTITY_RANGE_ROWS)?;
        if page.is_empty() { break; }
        let full_page = page.len() == MAX_ENTITY_RANGE_ROWS;
        let mut successor = page.last().expect("nonempty reservation page").0.key_bytes().to_vec();
        successor.push(0);
        for (key, raw) in &page {
            let name = std::str::from_utf8(key.key_bytes())
                .ok()
                .and_then(|raw| raw.strip_prefix("r:"))
                .ok_or_else(|| invalid_data("paper build budget reservation key is malformed"))?;
            if raw.len() != 8 {
                return Err(invalid_data("paper build budget reservation is malformed"));
            }
            reservations.insert(
                name.to_owned(),
                u64::from_be_bytes(raw.as_slice().try_into().unwrap()),
            );
        }
        if !full_page { break; }
        cursor = EntityKey::new(transaction.tenant_id(), TENANT_GLOBAL_OWNER_ID,
                                limits::BUDGET_NAMESPACE, &successor)?;
    }
    Ok(reservations)
}

fn restore_build(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_restore(payload, MAX_BUILD_LEDGER_JSON_BYTES)?;
    let snapshot = parse_build_ledger(&request, transaction.owner_user_id())?;
    // Cross-validate every reservation against the restored artifact metadata
    // before writing: the live budget only ever holds entries whose retained
    // artifact accounts the identical reserved bytes.
    for (key, bytes) in &snapshot.reservations {
        match paper_build_artifact::migration_reserved_bytes(database, transaction, key)? {
            Some(reserved) if reserved == *bytes => {}
            Some(_) => {
                return Err(invalid_data(
                    "paper build budget reservation disagrees with its retained artifact",
                ))
            }
            None => {
                return Err(invalid_data(
                    "paper build budget references a missing retained artifact",
                ))
            }
        }
    }
    let mut writes = Vec::new();
    let ledger = ledger_bytes(snapshot.reservations.len() as u64, snapshot.total_bytes);
    match_or_queue(
        database,
        transaction,
        &paper_build_artifact::budget_key(transaction, CAPACITY_KEY.as_bytes())?,
        &ledger,
        &mut writes,
    )?;
    for (key, bytes) in &snapshot.reservations {
        match_or_queue(
            database,
            transaction,
            &paper_build_artifact::reservation_key(transaction, key)?,
            &bytes.to_be_bytes(),
            &mut writes,
        )?;
    }
    let metadata = migration_metadata(&request);
    match_or_queue(
        database,
        transaction,
        &paper_build_artifact::budget_key(transaction, MIGRATION_KEY.as_bytes())?,
        &metadata,
        &mut writes,
    )?;
    // A reservation the snapshot does not claim is foreign live state; fail
    // closed rather than silently retaining or dropping it.
    for key in read_build_reservations(database, transaction)?.keys() {
        if !snapshot.reservations.contains_key(key) {
            return Err(snapshot_conflict());
        }
    }
    for (key, value) in &writes {
        database.entity_put(transaction, key.clone(), value.clone())?;
    }
    // Read back through the live decoding path before publication.
    if paper_build_artifact::usage(database, transaction)?
        != (snapshot.reservations.len() as u64, snapshot.total_bytes)
    {
        return Err(invalid_data("paper build budget ledger readback differs"));
    }
    if read_build_reservations(database, transaction)? != snapshot.reservations {
        return Err(invalid_data(
            "paper build budget reservation readback differs",
        ));
    }
    Ok(!writes.is_empty())
}

fn get_build(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    parse_get(payload)?;
    let (count, total_bytes) = paper_build_artifact::usage(database, transaction)?;
    let reservations = read_build_reservations(database, transaction)?;
    let metadata_key = paper_build_artifact::budget_key(transaction, MIGRATION_KEY.as_bytes())?;
    let metadata = database.entity_get(transaction, &metadata_key)?;
    if count == 0 && total_bytes == 0 && reservations.is_empty() && metadata.is_none() {
        return Ok(Value::Null);
    }
    let metadata =
        metadata.ok_or_else(|| invalid_data("paper build budget migration metadata is missing"))?;
    let (version, updated_at_ms) = parse_migration_metadata(&metadata)?;
    if reservations.len() as u64 != count
        || reservations.values().sum::<u64>() != total_bytes
        || reservations
            .keys()
            .any(|key| parse_reservation_key(key, transaction.owner_user_id()).is_err())
    {
        return Err(invalid_data("paper build budget ledger is inconsistent"));
    }
    Ok(json!({
        "record_key": CAPACITY_KEY,
        "value_json": build_ledger_json(&reservations),
        "version": version,
        "updated_at_ms": updated_at_ms,
    }))
}

fn parse_document_ledger(request: &RestoreRequest) -> io::Result<(u64, u64)> {
    let value: Value = serde_json::from_str(&request.value_json)
        .map_err(|_| invalid_input("paper document budget ledger is not an object"))?;
    let object = value
        .as_object()
        .filter(|object| !object.is_empty() && object.len() <= 2)
        .ok_or_else(|| invalid_input("paper document budget ledger is not an object"))?;
    let count = object
        .get("count")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_input("paper document budget count is invalid"))?;
    let associations = object
        .get("associations")
        .map(|value| {
            value
                .as_u64()
                .ok_or_else(|| invalid_input("paper document budget associations are invalid"))
        })
        .transpose()?
        .unwrap_or(0);
    Ok((count, associations))
}

fn document_budget_key(transaction: &AuthorityTransaction, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        limits::DOCUMENT_BUDGET_NAMESPACE,
        raw,
    )
}

fn restore_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_restore(payload, MAX_DOCUMENT_LEDGER_JSON_BYTES)?;
    let (count, associations) = parse_document_ledger(&request)?;
    let mut writes = Vec::new();
    let ledger = ledger_bytes(count, associations);
    match_or_queue(
        database,
        transaction,
        &document_budget_key(transaction, CAPACITY_KEY.as_bytes())?,
        &ledger,
        &mut writes,
    )?;
    let metadata = migration_metadata(&request);
    match_or_queue(
        database,
        transaction,
        &document_budget_key(transaction, MIGRATION_KEY.as_bytes())?,
        &metadata,
        &mut writes,
    )?;
    for (key, value) in &writes {
        database.entity_put(transaction, key.clone(), value.clone())?;
    }
    if paper_document::budget_usage(database, transaction)? != Some((count, associations)) {
        return Err(invalid_data(
            "paper document budget ledger readback differs",
        ));
    }
    Ok(!writes.is_empty())
}

fn get_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    parse_get(payload)?;
    let usage = paper_document::budget_usage(database, transaction)?;
    let metadata_key = document_budget_key(transaction, MIGRATION_KEY.as_bytes())?;
    let metadata = database.entity_get(transaction, &metadata_key)?;
    if usage.is_none() && metadata.is_none() {
        return Ok(Value::Null);
    }
    let (count, associations) =
        usage.ok_or_else(|| invalid_data("paper document budget ledger is missing"))?;
    let metadata = metadata
        .ok_or_else(|| invalid_data("paper document budget migration metadata is missing"))?;
    let (version, updated_at_ms) = parse_migration_metadata(&metadata)?;
    Ok(json!({
        "record_key": CAPACITY_KEY,
        "value_json": format!("{{\"count\":{count},\"associations\":{associations}}}"),
        "version": version,
        "updated_at_ms": updated_at_ms,
    }))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    // The budget ledgers are tenant-global; this scope admits both paper
    // budget namespaces while every reservation key stays owner-bound.
    let mut transaction = database.begin_with_paper_document_scope(tenant, owner)?;
    match operation {
        "migration.paper.build.budget.restore" => {
            let inserted = restore_build(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.paper.build.budget.get" => get_build(database, &mut transaction, payload),
        "migration.paper.document.budget.restore" => {
            let inserted = restore_document(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.paper.document.budget.get" => get_document(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown paper budget migration operation")),
    }
}

#[cfg(test)]
#[path = "paper_budget_migration_tests.rs"]
mod tests;
