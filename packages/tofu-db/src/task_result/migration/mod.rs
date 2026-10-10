//! Offline task-result snapshot restoration and independent index inspection.
//!
//! Entry: `execute`, called only after operator UUID/deadline admission. The
//! parent task_result codecs own every physical key and derived mutation; this
//! adapter replays one exact source document and rebuilds its header, replay
//! projection, summary, live and cost-experiment indexes inside the same
//! authority transaction. An interrupted-task tombstone or cost-experiment
//! outcome is never reconstructed from a bare projection. Every catalog page
//! revalidates its index targets and refuses extras.
//!
//! The source stores each checkpoint as the generic `task_results` record
//! document; the checkpoint semantic additionally merges abort tombstones into
//! that document on write. Restore therefore admits only documents whose
//! `abort_requested_at`/`abort_source` fields either agree with the declared
//! tombstone or are absent (unset tombstone), and every source update that
//! followed an abort must already carry the merged tombstone fields.

use std::io;

use serde::Deserialize;
use serde_json::{json, Value};

use super::{
    abort_source_value, cost_experiment, document_key, encode, invalid_data, invalid_input,
    live_key, metadata_object, payload_digest, read_header, read_replay_projection, replay_key,
    snapshot_conflict, summary_key, text_within, value_clock, value_text, write_header,
    write_indexes, write_summary, Header, ReplayProjection, Summary,
    DOCUMENT_ENVELOPE_OVERHEAD_BYTES, FULL_LOGICAL_NAMESPACE, MAX_ABORT_SOURCE_CHARACTERS,
    MAX_TASK_RESULT_DOCUMENT_BYTES, REPLAY_LOGICAL_NAMESPACE,
};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_KEY_BYTES, MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES,
    MIGRATION_SESSION_MAX_PAGE_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS,
    TASK_RESULT_COST_EXPERIMENT_NAMESPACE, TASK_RESULT_HEADER_NAMESPACE,
    TASK_RESULT_LIVE_NAMESPACE, TASK_RESULT_SUMMARY_NAMESPACE,
};
use crate::versioned_document::{self, SnapshotDocument, SnapshotRestoreOutcome};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    task_id: String,
    owner_user_id: u64,
    version: u64,
    updated_at_ms: u64,
    value_json: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    task_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    table: String,
    cursor: Option<String>,
}

struct PreparedSnapshot {
    task_id: String,
    document: Value,
    raw: Vec<u8>,
    header: Header,
    replay_json: Vec<u8>,
}

/// Parse and cross-check one exact source snapshot. Tombstone fields inside
/// the stored document must agree with the declared header projection: a
/// source write that followed an abort already carries the merged tombstone.
fn prepare(request: RestoreRequest) -> io::Result<PreparedSnapshot> {
    if !text_within(&request.task_id, super::MAX_TASK_ID_CHARACTERS, false)
        || request.owner_user_id == 0
        || request.version == 0
        || request.updated_at_ms == 0
        || request.value_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES
        // Frame-coherent migration lane budget: the inline restore document
        // crosses one 16 MiB operator frame, so this cap stays below it and
        // below the 64 MiB live-checkpoint blob-graph ceiling.
        || request.value_json.len() > MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES
    {
        return Err(invalid_input("invalid task result snapshot metadata"));
    }
    let raw = request.value_json.into_bytes();
    let document = serde_json::from_slice::<Value>(&raw)
        .ok()
        .filter(Value::is_object)
        .ok_or_else(|| invalid_input("task result snapshot is not an object"))?;
    let object = document.as_object().expect("object document");
    if object
        .get("task_id")
        .is_some_and(|value| value.as_str() != Some(request.task_id.as_str()))
    {
        return Err(invalid_input("task result snapshot identity differs"));
    }
    if object.get("user_id").and_then(Value::as_u64) != Some(request.owner_user_id) {
        return Err(invalid_input("task result snapshot owner differs"));
    }
    let declared_abort_at = match object.get("abort_requested_at") {
        None | Some(Value::Null) => 0,
        Some(value) => value
            .as_u64()
            .filter(|value| *value > 0)
            .ok_or_else(|| invalid_input("task result abort tombstone is invalid"))?,
    };
    let declared_source = abort_source_value(&document)?;
    if declared_abort_at == 0 && !declared_source.is_empty() {
        return Err(invalid_input("task result abort source lacks a tombstone"));
    }
    if declared_abort_at != 0 && !text_within(&declared_source, MAX_ABORT_SOURCE_CHARACTERS, false)
    {
        return Err(invalid_input("task result abort source is invalid"));
    }
    let conv_id = value_text(object.get("conv_id"), "")?;
    let status = value_text(object.get("status"), "")?;
    if !text_within(&conv_id, super::MAX_CONVERSATION_ID_CHARACTERS, true)
        || !text_within(&status, super::MAX_STATUS_CHARACTERS, true)
    {
        return Err(invalid_input("task result summary projection is too large"));
    }
    let metadata = metadata_object(object.get("metadata"));
    let cost_experiment_id = cost_experiment(&metadata)
        .map(|(_, experiment_id)| experiment_id)
        .unwrap_or_default();
    let interrupted_reason = value_text(object.get("interrupted_reason"), "")?;
    if !text_within(
        &interrupted_reason,
        super::MAX_INTERRUPTED_REASON_CHARACTERS,
        true,
    ) {
        return Err(invalid_input("task result interrupted reason is invalid"));
    }
    let header = Header {
        task_id: request.task_id.clone(),
        owner_user_id: request.owner_user_id,
        version: request.version,
        updated_at_ms: request.updated_at_ms,
        payload_bytes: raw.len() as u64,
        payload_sha256: payload_digest(&raw),
        conv_id,
        status,
        created_at: value_clock(object.get("created_at"))?,
        completed_at: value_clock(object.get("completed_at"))?,
        abort_requested_at: declared_abort_at,
        abort_source: declared_source,
        interrupted_reason,
        cost_experiment_id,
        // Preserve any legitimate source-side child birth identity so the
        // live checkpoint guard keeps fencing it after import.
        child_invocation: object
            .get("child_invocation")
            .filter(|value| !value.is_null())
            .cloned(),
    };
    header.validate(&request.task_id)?;
    let replay_json = encode(
        &ReplayProjection {
            metadata,
            error: object.get("error").cloned().unwrap_or(Value::Null),
        },
        "task result replay projection cannot be encoded",
    )?;
    if replay_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES {
        return Err(invalid_input(
            "task result replay projection exceeds 64 MiB",
        ));
    }
    Ok(PreparedSnapshot {
        task_id: request.task_id,
        document,
        raw,
        header,
        replay_json,
    })
}

fn snapshot_document(prepared: &PreparedSnapshot) -> Value {
    json!({
        "task_id": prepared.task_id,
        "owner_user_id": prepared.header.owner_user_id,
        "version": prepared.header.version,
        "updated_at_ms": prepared.header.updated_at_ms,
        "value_json": prepared.document,
    })
}

fn equal_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    expected: &[u8],
) -> io::Result<()> {
    if database.entity_get(transaction, &key)?.as_deref() != Some(expected) {
        return Err(invalid_data(
            "task result snapshot index is missing or differs",
        ));
    }
    Ok(())
}

/// Verify every derived record for one header and fail closed when any index
/// disagrees with the imported state.
fn verify_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    header: &Header,
    raw: &[u8],
    document_key: &EntityKey,
) -> io::Result<()> {
    let summary = encode(&header.summary(), "task result summary cannot be encoded")?;
    equal_index(
        database,
        transaction,
        summary_key(transaction, &header.task_id)?,
        &summary,
    )?;
    let live = database.entity_get(transaction, &live_key(transaction, &header.task_id)?)?;
    if matches!(header.status.as_str(), "pending" | "running") {
        if live.as_deref() != Some(header.owner_user_id.to_le_bytes().as_slice()) {
            return Err(invalid_data("task result live index is missing or differs"));
        }
    } else if live.is_some() {
        return Err(invalid_data(
            "task result live index retains a settled task",
        ));
    }
    let cost = database.entity_get(
        transaction,
        &super::cost_index_key(transaction, &header.cost_experiment_id, &header.task_id)?,
    )?;
    if header.cost_experiment_id.is_empty() {
        if cost.is_some() {
            return Err(invalid_data(
                "task result cost index retains an untagged task",
            ));
        }
    } else if cost.as_deref() != Some([].as_slice()) {
        return Err(invalid_data("task result cost index is missing or differs"));
    }
    let Some(stored) = database.entity_get(transaction, document_key)? else {
        return Err(invalid_data("task result snapshot document is missing"));
    };
    if versioned_document::stored_document_version(
        &stored,
        FULL_LOGICAL_NAMESPACE,
        &header.task_id,
    )? != header.version
    {
        return Err(invalid_data(
            "task result snapshot document version differs",
        ));
    }
    let materialized = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        document_key,
        FULL_LOGICAL_NAMESPACE,
        &header.task_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_TASK_RESULT_DOCUMENT_BYTES + DOCUMENT_ENVELOPE_OVERHEAD_BYTES,
    )?
    .ok_or_else(|| invalid_data("task result snapshot document is missing"))?;
    if materialized != raw {
        return Err(invalid_data("task result snapshot document differs"));
    }
    let replay = read_replay_projection(database, transaction, &header.task_id)?;
    let expected = metadata_object(
        serde_json::from_slice::<Value>(raw)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .map(Value::Object)
            .as_ref()
            .and_then(|value| value.get("metadata")),
    );
    let expected_error = serde_json::from_slice::<Value>(raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .and_then(|value| value.get("error").cloned())
        .unwrap_or(Value::Null);
    if replay.metadata != expected || replay.error != expected_error {
        return Err(invalid_data("task result replay projection differs"));
    }
    Ok(())
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    expected_authority_uuid: uuid::Uuid,
    request: RestoreRequest,
) -> io::Result<bool> {
    let prepared = prepare(request)?;
    if prepared.header.owner_user_id != transaction.owner_user_id() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "task result snapshot owner is outside the operator scope",
        ));
    }
    let document_key = document_key(transaction, &prepared.task_id)?;
    if let Some(current) = read_header(database, transaction, &prepared.task_id)? {
        if current != prepared.header {
            return Err(snapshot_conflict());
        }
        verify_indexes(
            database,
            transaction,
            &prepared.header,
            &prepared.raw,
            &document_key,
        )?;
        return Ok(false);
    }
    if database.entity_get(transaction, &document_key)?.is_some() {
        return Err(invalid_data("task result document exists without a header"));
    }
    let outcome = crate::migration::restore_document(
        database,
        transaction,
        expected_authority_uuid,
        SnapshotDocument {
            key: document_key.clone(),
            namespace: FULL_LOGICAL_NAMESPACE.to_owned(),
            logical_key: prepared.task_id.clone(),
            version: prepared.header.version,
            updated_at_ms: prepared.header.updated_at_ms,
            value_json: prepared.raw.clone(),
            blob_owner_user_id: TENANT_GLOBAL_OWNER_ID,
        },
    )?;
    if !matches!(outcome, SnapshotRestoreOutcome::Inserted) {
        return Err(invalid_data("task result document exists without a header"));
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        versioned_document::PutRequest {
            key: replay_key(transaction, &prepared.task_id)?,
            namespace: REPLAY_LOGICAL_NAMESPACE.to_owned(),
            logical_key: prepared.task_id.clone(),
            value_json: prepared.replay_json.clone(),
            expected_version: Some(0),
            updated_at_ms: prepared.header.updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_TASK_RESULT_DOCUMENT_BYTES,
    )?;
    write_header(database, transaction, &prepared.header)?;
    write_summary(database, transaction, &prepared.header)?;
    write_indexes(database, transaction, None, &prepared.header)?;
    verify_indexes(
        database,
        transaction,
        &prepared.header,
        &prepared.raw,
        &document_key,
    )?;
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: GetRequest,
) -> io::Result<Value> {
    if !text_within(&request.task_id, super::MAX_TASK_ID_CHARACTERS, false) {
        return Err(invalid_input("invalid task result identity"));
    }
    let Some(header) = read_header(database, transaction, &request.task_id)? else {
        return Ok(Value::Null);
    };
    if header.owner_user_id != transaction.owner_user_id() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "task result snapshot owner is outside the operator scope",
        ));
    }
    let document_key = document_key(transaction, &request.task_id)?;
    let raw = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &document_key,
        FULL_LOGICAL_NAMESPACE,
        &request.task_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES + DOCUMENT_ENVELOPE_OVERHEAD_BYTES,
    )?
    .ok_or_else(|| invalid_data("task result snapshot document is missing"))?;
    let _: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("task result snapshot document is malformed"))?;
    let prepared = prepare(RestoreRequest {
        task_id: request.task_id.clone(),
        owner_user_id: header.owner_user_id,
        version: header.version,
        updated_at_ms: header.updated_at_ms,
        value_json: String::from_utf8(raw.clone())
            .map_err(|_| invalid_data("task result snapshot is not UTF-8"))?,
    })?;
    if prepared.header != header {
        return Err(invalid_data("task result snapshot header differs"));
    }
    verify_indexes(database, transaction, &header, &raw, &document_key)?;
    Ok(snapshot_document(&prepared))
}

fn cursor_start(table: &str, cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let encoded = cursor
        .strip_prefix(table)
        .and_then(|value| value.strip_prefix(':'))
        .ok_or_else(|| invalid_input("task result cursor belongs to another table"))?;
    if encoded.is_empty()
        || encoded.len() > MAX_ENTITY_KEY_BYTES * 2
        || !encoded.len().is_multiple_of(2)
    {
        return Err(invalid_input("invalid task result cursor"));
    }
    let mut raw = encoded
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            u8::from_str_radix(
                std::str::from_utf8(pair)
                    .map_err(|_| invalid_input("invalid task result cursor"))?,
                16,
            )
            .map_err(|_| invalid_input("invalid task result cursor"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    if raw.len() < MAX_ENTITY_KEY_BYTES {
        raw.push(0);
    } else {
        let index = raw
            .iter()
            .rposition(|byte| *byte != 255)
            .ok_or_else(|| invalid_input("task result cursor has no successor"))?;
        raw[index] += 1;
        raw.truncate(index + 1);
    }
    Ok(raw)
}

fn inspect_row(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    table: &str,
    physical: &EntityKey,
    raw: &[u8],
) -> io::Result<Value> {
    match table {
        "headers" => {
            let header: Header = serde_json::from_slice(raw)
                .map_err(|_| invalid_data("task result header catalog is malformed"))?;
            header.validate(&header.task_id)?;
            if &super::header_key(transaction, &header.task_id)? != physical
                || header.owner_user_id != transaction.owner_user_id()
            {
                return Err(invalid_data("task result header catalog identity differs"));
            }
            Ok(json!({"task_id": header.task_id}))
        }
        "summaries" => {
            let summary: Summary = serde_json::from_slice(raw)
                .map_err(|_| invalid_data("task result summary catalog is malformed"))?;
            summary.validate()?;
            if &summary_key(transaction, &summary.task_id)? != physical {
                return Err(invalid_data("task result summary catalog identity differs"));
            }
            let header = read_header(database, transaction, &summary.task_id)?
                .ok_or_else(|| invalid_data("task result summary header is missing"))?;
            if header.summary() != summary {
                return Err(invalid_data("task result summary catalog differs"));
            }
            Ok(json!({"task_id": summary.task_id}))
        }
        "live" => {
            if raw != transaction.owner_user_id().to_le_bytes() {
                return Err(invalid_data("task result live catalog owner is malformed"));
            }
            let task_id = std::str::from_utf8(physical.key_bytes())
                .map_err(|_| invalid_data("task result live catalog key is malformed"))?;
            let header = read_header(database, transaction, task_id)?
                .ok_or_else(|| invalid_data("task result live header is missing"))?;
            if header.owner_user_id != transaction.owner_user_id()
                || !matches!(header.status.as_str(), "pending" | "running")
            {
                return Err(invalid_data("task result live catalog is stale"));
            }
            Ok(json!({"task_id": task_id}))
        }
        "cost_index" => {
            if !raw.is_empty() {
                return Err(invalid_data(
                    "task result cost catalog payload is malformed",
                ));
            }
            let digest_prefix = physical
                .key_bytes()
                .get(..33)
                .ok_or_else(|| invalid_data("task result cost catalog key is malformed"))?;
            if digest_prefix[32] != 0 {
                return Err(invalid_data("task result cost catalog key is malformed"));
            }
            let task_id = std::str::from_utf8(&physical.key_bytes()[33..])
                .map_err(|_| invalid_data("task result cost catalog key is malformed"))?;
            let header = read_header(database, transaction, task_id)?
                .ok_or_else(|| invalid_data("task result cost header is missing"))?;
            if header.cost_experiment_id.is_empty()
                || super::cost_index_prefix(&header.cost_experiment_id) != digest_prefix
            {
                return Err(invalid_data("task result cost catalog identity differs"));
            }
            Ok(json!({"task_id": task_id}))
        }
        _ => Err(invalid_input("unknown task result migration table")),
    }
}

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: ScanRequest,
) -> io::Result<Value> {
    let namespace = match request.table.as_str() {
        "headers" => TASK_RESULT_HEADER_NAMESPACE,
        "summaries" => TASK_RESULT_SUMMARY_NAMESPACE,
        "live" => TASK_RESULT_LIVE_NAMESPACE,
        "cost_index" => TASK_RESULT_COST_EXPERIMENT_NAMESPACE,
        _ => return Err(invalid_input("unknown task result migration table")),
    };
    let owner = if request.table == "headers" {
        TENANT_GLOBAL_OWNER_ID
    } else {
        transaction.owner_user_id()
    };
    let raw = cursor_start(&request.table, request.cursor.as_deref())?;
    let start = EntityKey::new(transaction.tenant_id(), owner, namespace, &raw)?;
    let (_, end) = EntityKey::prefix_range(transaction.tenant_id(), owner, namespace, b"")?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    let mut bytes = 64_usize;
    for (physical, value) in &page {
        let row = inspect_row(database, transaction, &request.table, physical, value)?;
        bytes = bytes
            .checked_add(16)
            .and_then(|total| total.checked_add(row.to_string().len()))
            .filter(|total| *total <= MIGRATION_SESSION_MAX_PAGE_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "task result migration catalog exceeds its page budget",
                )
            })?;
        rows.push(row);
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let encoded: String = page
            .last()
            .expect("a full task result catalog page has a last key")
            .0
            .key_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Some(format!("{}:{encoded}", request.table))
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
    let expected_uuid = database.authority_uuid();
    match operation {
        "migration.task.result.restore" => {
            let request: RestoreRequest = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid task result restore request"))?;
            let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
            let inserted = restore(database, &mut transaction, expected_uuid, request)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.task.result.get" => {
            let request: GetRequest = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid task result get request"))?;
            let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
            get(database, &mut transaction, request)
        }
        "migration.task.result.scan" => {
            let request: ScanRequest = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid task result scan request"))?;
            let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
            scan(database, &mut transaction, request)
        }
        _ => Err(invalid_input("unknown task result migration operation")),
    }
}

#[cfg(test)]
mod tests;
