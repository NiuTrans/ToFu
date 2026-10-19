//! Offline identity-preserving Turn restore for the exclusive operator
//! importer. Live `turn.create_pair` still allocates identities; this path
//! claims source turn ids, ordinals, projections, and timestamps, and refuses
//! live attempts.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_KEY_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS, TURN_DOCUMENT_NAMESPACE,
    TURN_ID_CLAIM_NAMESPACE, TURN_LANE_HEAD_NAMESPACE, TURN_SEARCH_DIRTY_NAMESPACE,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.to_string())
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.to_string())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    turn_id: String,
    conversation_id: String,
    presentation_id: String,
    lane_id: String,
    parent_turn_id: Option<String>,
    ordinal: u64,
    actor: String,
    kind: String,
    run_id: String,
    status: String,
    current_attempt_id: Option<String>,
    projection: Value,
    projection_revision: u64,

    #[serde(default)]
    model_context_display_offset: u64,
    settlement: Value,
    created_at: u64,
    updated_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MigrationGetRequest {
    id: String,
    conversation_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MigrationScanRequest {
    cursor: Option<String>,
}

fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "turn snapshot already differs")
}

fn occupied() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "turn identity already occupies the target",
    )
}

fn lookup_turn_claim(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    turn_id: &str,
) -> io::Result<Option<u64>> {
    let key = super::global_identity_claim_key(transaction, TURN_ID_CLAIM_NAMESPACE, turn_id)?;
    match database.entity_get(transaction, &key)? {
        None => Ok(None),
        Some(raw) if raw.len() == 8 => Ok(Some(u64::from_be_bytes(raw.try_into().unwrap()))),
        Some(_) => Err(invalid_data("turn identity claim is malformed")),
    }
}

fn lane_head_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<EntityKey> {
    super::entity_key(
        transaction,
        TURN_LANE_HEAD_NAMESPACE,
        &super::lane_prefix(conversation_id, lane_id)?,
    )
}

fn bounded_identity(value: &str, max_chars: usize) -> bool {
    !value.is_empty() && value.chars().count() <= max_chars
}

fn valid_restore(request: &RestoreRequest) -> bool {

    if request.model_context_display_offset > request.projection_revision {
        return false;
    }
    bounded_identity(&request.turn_id, 128)
        && bounded_identity(&request.conversation_id, 256)
        && request.presentation_id.chars().count() <= 128
        && bounded_identity(&request.lane_id, 64)
        && request
            .parent_turn_id
            .as_ref()
            .map(|value| bounded_identity(value, 128))
            .unwrap_or(true)
        && bounded_identity(&request.actor, 64)
        && bounded_identity(&request.kind, 64)
        && request.run_id.chars().count() <= 256
        && bounded_identity(&request.status, 64)
        && request.current_attempt_id.as_ref().is_none_or(|id| {
            bounded_identity(id, 128) && !matches!(request.status.as_str(), "pending" | "running")
        })
        && request.projection.is_object()
        && request.settlement.is_object()
}

fn parent_value(parent: &Option<String>) -> Value {
    match parent {
        Some(value) => Value::String(value.clone()),
        None => Value::Null,
    }
}

fn with_context_display_offset(mut snapshot: Value, offset: u64) -> Value {
    // Keep legacy zero-offset snapshots byte-compatible.
    if offset > 0 {
        snapshot["model_context_display_offset"] = json!(offset);
    }
    snapshot
}

fn restore_snapshot(request: &RestoreRequest) -> Value {
    with_context_display_offset(json!({
        "turn_id": request.turn_id,
        "conversation_id": request.conversation_id,
        "presentation_id": request.presentation_id,
        "lane_id": request.lane_id,
        "parent_turn_id": parent_value(&request.parent_turn_id),
        "ordinal": request.ordinal,
        "actor": request.actor,
        "kind": request.kind,
        "run_id": request.run_id,
        "status": request.status,
        "current_attempt_id": request.current_attempt_id,
        "projection": request.projection,
        "projection_revision": request.projection_revision,
        "settlement": request.settlement,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
    }), request.model_context_display_offset)
}

fn required_text(document: &Map<String, Value>, field: &str) -> io::Result<String> {
    document
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| invalid_data("restored turn text field is malformed"))
}

fn emptyable_text(document: &Map<String, Value>, field: &str) -> io::Result<String> {
    match document.get(field) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(value)) => Ok(value.clone()),
        Some(_) => Err(invalid_data("restored turn text field is malformed")),
    }
}

fn unsigned_field(document: &Map<String, Value>, field: &str) -> io::Result<u64> {
    document
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("restored turn counter is malformed"))
}

fn restore_snapshot_from_document(document: &Map<String, Value>) -> io::Result<Value> {
    let parent = match document.get("parentTurnId") {
        None | Some(Value::Null) => Value::Null,
        Some(Value::String(value)) if !value.is_empty() => Value::String(value.clone()),
        _ => return Err(invalid_data("restored turn parent is malformed")),
    };
    let current = match document.get("currentAttemptId") {
        None | Some(Value::Null) => Value::Null,
        Some(Value::String(id)) if bounded_identity(id, 128) => Value::String(id.clone()),
        _ => return Err(invalid_data("restored turn attempt identity is malformed")),
    };
    let projection = document
        .get("projection")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| invalid_data("restored turn projection is malformed"))?;
    let settlement = document
        .get("settlement")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| invalid_data("restored turn settlement is malformed"))?;
    let offset = unsigned_field(document, "projectionRevision")?
        - super::model_context_revision(document)?;
    Ok(with_context_display_offset(json!({
        "turn_id": required_text(document, "turnId")?,
        "conversation_id": required_text(document, "conversationId")?,
        "presentation_id": emptyable_text(document, "presentationId")?,
        "lane_id": required_text(document, "laneId")?,
        "parent_turn_id": parent,
        "ordinal": unsigned_field(document, "ordinal")?,
        "actor": required_text(document, "actor")?,
        "kind": required_text(document, "kind")?,
        "run_id": emptyable_text(document, "runId")?,
        "status": required_text(document, "status")?,
        "current_attempt_id": current,
        "projection": projection,
        "projection_revision": unsigned_field(document, "projectionRevision")?,
        "settlement": settlement,
        "created_at": unsigned_field(document, "createdAt")?,
        "updated_at": unsigned_field(document, "updatedAt")?,
    }), offset))
}

fn verify_restore_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RestoreRequest,
    stored: &[u8],
    document: &Map<String, Value>,
) -> io::Result<()> {
    let conversation_id = &request.conversation_id;
    let turn_id = &request.turn_id;
    let lane_id = &request.lane_id;
    let ordinal = request.ordinal;
    let live = database
        .entity_get(
            transaction,
            &super::turn_key(transaction, conversation_id, turn_id)?,
        )?
        .ok_or_else(|| invalid_data("restored turn document is missing"))?;
    if live != stored {
        return Err(invalid_data("restored turn document target differs"));
    }
    let lane_stored = database
        .entity_get(
            transaction,
            &super::lane_index_key(transaction, conversation_id, lane_id, ordinal)?,
        )?
        .ok_or_else(|| invalid_data("restored turn lane index is missing"))?;
    if lane_stored != stored {
        return Err(invalid_data("restored turn lane index differs"));
    }
    let compact = database
        .entity_get(
            transaction,
            &super::lane_compaction_index_key(transaction, conversation_id, lane_id, ordinal)?,
        )?
        .ok_or_else(|| invalid_data("restored turn compaction index is missing"))?;
    if compact != super::lane_compaction_index_value(document)? {
        return Err(invalid_data("restored turn compaction index differs"));
    }
    let count = super::decode_u64(
        database.entity_get(
            transaction,
            &super::lane_count_key(transaction, conversation_id, lane_id)?,
        )?,
        "restored turn lane count is malformed",
    )?;
    if count == 0 {
        return Err(invalid_data("restored turn lane count is missing"));
    }
    let head = super::decode_u64(
        database.entity_get(
            transaction,
            &lane_head_key(transaction, conversation_id, lane_id)?,
        )?,
        "restored turn lane head is malformed",
    )?;
    if head < ordinal {
        return Err(invalid_data("restored turn lane head is behind"));
    }
    if lane_id == "main" {
        let activity = database
            .entity_get(
                transaction,
                &super::activity_index_key(transaction, conversation_id, ordinal)?,
            )?
            .ok_or_else(|| invalid_data("restored turn activity index is missing"))?;
        let expected = super::encode_activity_index_value(super::effective_activity_timestamp(
            document
                .get("projection")
                .and_then(|value| value.get("timestamp")),
            request.created_at,
        ));
        if activity != expected {
            return Err(invalid_data("restored turn activity index differs"));
        }
    }
    let indexed = database.entity_get(
        transaction,
        &super::updated_index_key(transaction, conversation_id, request.updated_at, turn_id)?,
    )?;
    let expected_updated =
        super::encode_updated_index_value(turn_id, request.projection_revision.max(1))?;
    if indexed.as_deref() != Some(expected_updated.as_slice()) {
        return Err(invalid_data("restored turn updated index differs"));
    }
    match lookup_turn_claim(database, transaction, turn_id)? {
        Some(owner) if owner == transaction.owner_user_id() => Ok(()),
        _ => Err(invalid_data("restored turn identity claim differs")),
    }
}

fn write_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RestoreRequest,
) -> io::Result<()> {
    let conversation_id = &request.conversation_id;
    let turn_id = &request.turn_id;
    let lane_id = &request.lane_id;
    let ordinal = request.ordinal;
    let lane_key = super::lane_index_key(transaction, conversation_id, lane_id, ordinal)?;
    if let Some(existing) = database.entity_get(transaction, &lane_key)? {
        let occupant = super::materialize_turn(database, transaction, &existing)?;
        if occupant.get("turnId").and_then(Value::as_str) != Some(turn_id) {
            return Err(occupied());
        }
        return Err(invalid_data(
            "restored turn lane index is missing its document",
        ));
    }
    let mut document = json!({
        "turnId": turn_id,
        "presentationId": request.presentation_id,
        "conversationId": conversation_id,
        "laneId": lane_id,
        "parentTurnId": parent_value(&request.parent_turn_id),
        "ordinal": ordinal,
        "actor": request.actor,
        "kind": request.kind,
        "runId": request.run_id,
        "status": request.status,
        "currentAttemptId": request.current_attempt_id,
        "projection": request.projection,
        "projectionRevision": request.projection_revision,
        "settlement": request.settlement,
        "createdAt": request.created_at,
        "updatedAt": request.updated_at,
        "_executionEpoch": 0,
    });

    if request.model_context_display_offset > 0 {
        document["_modelContextDisplayOffset"] = json!(request.model_context_display_offset);
    }
    let document_key = super::turn_key(transaction, conversation_id, turn_id)?;
    let (_, stored) = crate::versioned_document::put_with_blob_owner_bounded_returning_envelope(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: document_key,
            namespace: super::DOCUMENT_IDENTITY.to_owned(),
            logical_key: super::turn_logical_key(conversation_id, turn_id),
            value_json: serde_json::to_vec(&document)
                .map_err(|_| invalid_data("restored turn cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: request.updated_at.max(1),
        },
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    database.entity_put(
        transaction,
        super::global_identity_claim_key(transaction, TURN_ID_CLAIM_NAMESPACE, turn_id)?,
        transaction.owner_user_id().to_be_bytes().to_vec(),
    )?;
    database.entity_put(transaction, lane_key, stored.clone())?;
    let map = document
        .as_object()
        .ok_or_else(|| invalid_data("restored turn cannot be indexed"))?;
    super::put_lane_compaction_index(database, transaction, map)?;
    if lane_id == "main" {
        database.entity_put(
            transaction,
            super::activity_index_key(transaction, conversation_id, ordinal)?,
            super::encode_activity_index_value(super::effective_activity_timestamp(
                request.projection.get("timestamp"),
                request.created_at,
            )),
        )?;
    }
    let count_key = super::lane_count_key(transaction, conversation_id, lane_id)?;
    let count = super::decode_u64(
        database.entity_get(transaction, &count_key)?,
        "turn lane count is malformed",
    )?;
    database.entity_put(
        transaction,
        count_key,
        count
            .checked_add(1)
            .ok_or_else(|| invalid_data("turn lane count overflow"))?
            .to_le_bytes()
            .to_vec(),
    )?;
    let head_key = lane_head_key(transaction, conversation_id, lane_id)?;
    match database.entity_get(transaction, &head_key)? {
        None => {
            database.entity_put(transaction, head_key, ordinal.to_le_bytes().to_vec())?;
        }
        Some(raw) => {
            let head = super::decode_u64(Some(raw), "turn lane head is malformed")?;
            if ordinal > head {
                database.entity_put(transaction, head_key, ordinal.to_le_bytes().to_vec())?;
            }
        }
    }
    let main_count = super::main_turn_count(database, transaction, conversation_id)?;
    crate::conversation_header::set_imported_message_count(
        database,
        transaction,
        conversation_id,
        main_count,
    )?;
    database.entity_put(
        transaction,
        super::updated_index_key(transaction, conversation_id, request.updated_at, turn_id)?,
        super::encode_updated_index_value(turn_id, request.projection_revision.max(1))?,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        conversation_id,
    )?;
    verify_restore_indexes(database, transaction, request, &stored, map)
}

fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid turn restore request"))?;
    if !valid_restore(&request) {
        return Err(invalid_input("invalid turn restore request"));
    }
    if let Some(id) = &request.current_attempt_id {
        let attempt = super::attempt_migration::read_snapshot(
            database,
            transaction,
            &request.conversation_id,
            id,
        )?
        .ok_or_else(|| invalid_input("turn current attempt has not been imported"))?;
        if attempt["turn_id"].as_str() != Some(&request.turn_id)
            || matches!(attempt["status"].as_str(), Some("pending" | "running"))
        {
            return Err(invalid_input(
                "turn current attempt identity or status differs",
            ));
        }
    }
    let Some(_header) = crate::conversation_header::get(
        database,
        transaction,
        &request.conversation_id,
        false,
        0,
        None,
        // Offline operator lane: no live request deadline applies.
        &mut || false,
    )?
    else {
        return Err(invalid_input("conversation not found"));
    };
    let document_key = super::turn_key(transaction, &request.conversation_id, &request.turn_id)?;
    if let Some(stored) = database.entity_get(transaction, &document_key)? {
        let document = super::materialize_turn(database, transaction, &stored)?;
        if restore_snapshot_from_document(&document)? != restore_snapshot(&request) {
            return Err(snapshot_conflict());
        }
        verify_restore_indexes(database, transaction, &request, &stored, &document)?;
        return Ok(false);
    }
    match lookup_turn_claim(database, transaction, &request.turn_id)? {
        Some(owner) if owner == transaction.owner_user_id() => {
            return Err(invalid_data("turn identity claim is missing its document"));
        }
        Some(_) => return Err(occupied()),
        None => {}
    }
    write_restore(database, transaction, &request)?;
    Ok(true)
}

fn migration_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: MigrationGetRequest,
) -> io::Result<Value> {
    if !bounded_identity(&request.id, 128) || !bounded_identity(&request.conversation_id, 256) {
        return Err(invalid_input("invalid turn get request"));
    }
    let Some(stored) = database.entity_get(
        transaction,
        &super::turn_key(transaction, &request.conversation_id, &request.id)?,
    )?
    else {
        return Ok(Value::Null);
    };
    let document = super::materialize_turn(database, transaction, &stored)?;
    restore_snapshot_from_document(&document)
}

pub(super) fn cursor_start(table: &str, cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let prefix = format!("{table}:");
    if !cursor.starts_with(&prefix) {
        return Err(invalid_input("invalid turn catalog cursor"));
    }
    let encoded = &cursor[prefix.len()..];
    if encoded.len() > MAX_ENTITY_KEY_BYTES * 2 || !encoded.len().is_multiple_of(2) {
        return Err(invalid_input("invalid turn catalog cursor"));
    }
    let mut raw = encoded
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            u8::from_str_radix(
                std::str::from_utf8(pair)
                    .map_err(|_| invalid_input("invalid turn catalog cursor"))?,
                16,
            )
            .map_err(|_| invalid_input("invalid turn catalog cursor"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    if raw.len() < MAX_ENTITY_KEY_BYTES {
        raw.push(0);
    } else {
        let index = raw
            .iter()
            .rposition(|b| *b != 255)
            .ok_or_else(|| invalid_input("turn catalog cursor has no successor"))?;
        raw[index] += 1;
        raw.truncate(index + 1);
    }
    Ok(raw)
}

fn inspect_document_row(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    physical: &EntityKey,
    stored: &[u8],
) -> io::Result<Value> {
    let document = super::materialize_turn(database, transaction, stored)?;
    let conversation_id = required_text(&document, "conversationId")?;
    let turn_id = required_text(&document, "turnId")?;
    if physical != &super::turn_key(transaction, &conversation_id, &turn_id)? {
        return Err(invalid_data("turn catalog identity differs"));
    }
    Ok(json!({"id": turn_id}))
}

fn migration_scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: MigrationScanRequest,
) -> io::Result<Value> {
    let suffix = cursor_start("turns", request.cursor.as_deref())?;
    let start = super::entity_key(transaction, TURN_DOCUMENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, stored) in &page {
        rows.push(inspect_document_row(
            database,
            transaction,
            physical,
            stored,
        )?);
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let encoded: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("turns:{encoded}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

/// Reads one exact Turn through either the live authority or a trash range pin.
pub(crate) fn snapshot_in_transaction(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    id: &str,
) -> io::Result<Value> {
    migration_get(
        database,
        tx,
        MigrationGetRequest {
            id: id.to_owned(),
            conversation_id: conversation_id.to_owned(),
        },
    )
}

/// Physical conversation-local enumeration, independent of derived count rows.
pub(crate) fn scan_conversation(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    cursor: Option<&str>,
) -> io::Result<Value> {
    let prefix = super::conversation_prefix(conversation_id)?;
    let suffix = if cursor.is_some() {
        cursor_start("trash_turns", cursor)?
    } else {
        prefix.clone()
    };
    if !suffix.starts_with(&prefix) {
        return Err(invalid_input("trash Turn cursor scope differs"));
    }
    let start = super::entity_key(tx, TURN_DOCUMENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        TURN_DOCUMENT_NAMESPACE,
        &prefix,
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, stored) in &page {
        rows.push(inspect_document_row(database, tx, physical, stored)?);
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let encoded: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("trash_turns:{encoded}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

/// Explicit offline upgrade of legacy compact heads. Kept out of storage.v2
/// and directory queries: only the exclusive migration-session may materialize
/// old documents, one at a time, to populate immutable actor metadata.
fn upgrade_round_heads(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction, payload: Value,
) -> io::Result<Value> {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Request { cursor: Option<String>, limit: Option<usize> }
    let request: Request = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid compact round head upgrade"))?;
    let limit = request.limit.unwrap_or(16);
    if !(1..=32).contains(&limit) { return Err(invalid_input("compact head upgrade limit exceeds 32")); }
    let suffix = cursor_start("round_heads", request.cursor.as_deref())?;
    let start = super::entity_key(transaction, TURN_DOCUMENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(transaction.tenant_id(), transaction.owner_user_id(),
        TURN_DOCUMENT_NAMESPACE, b"")?;
    let rows = database.entity_scan(transaction, &start, &end, limit)?;
    let mut repaired = 0;
    for (key, stored) in &rows {
        let document = super::materialize_turn(database, transaction, stored)?;
        let map = &document;
        let conv = required_text(&document, "conversationId")?;
        let id = required_text(&document, "turnId")?;
        if *key != super::turn_key(transaction, &conv, &id)? {
            return Err(invalid_data("Turn upgrade document identity differs"));
        }
        let lane = required_text(&document, "laneId")?;
        let ordinal = unsigned_field(map, "ordinal")?;
        let head_key = super::lane_compaction_index_key(transaction, &conv, &lane, ordinal)?;
        let expected = super::lane_compaction_index_value(map)?;
        if database.entity_get(transaction, &head_key)?.as_deref() != Some(expected.as_slice()) {
            super::put_lane_compaction_index(database, transaction, map)?;
            repaired += 1;
        }
    }
    let cursor = if rows.len() == limit {
        Some(format!("round_heads:{}", rows.last().unwrap().0.key_bytes().iter()
            .map(|b| format!("{b:02x}")).collect::<String>()))
    } else { None };
    Ok(json!({"scanned": rows.len(), "repaired": repaired, "cursor": cursor}))
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
        "migration.turn.round_heads.upgrade" => {
            let result = upgrade_round_heads(database, &mut transaction, payload)?;
            if result["repaired"].as_u64().unwrap_or(0) > 0 { database.commit(transaction)?; }
            Ok(result)
        }
        operation if operation.starts_with("migration.turn.tombstone.") => {
            tombstones::execute(database, transaction, operation, payload)
        }
        "migration.turn.restore" => {
            let inserted = restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.turn.get" => {
            let request = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid turn get request"))?;
            migration_get(database, &mut transaction, request)
        }
        "migration.turn.scan" => {
            let request = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid turn scan request"))?;
            migration_scan(database, &mut transaction, request)
        }
        _ => Err(invalid_input("unknown turn migration operation")),
    }
}

#[cfg(test)]
#[path = "migration_tests.rs"]
mod tests;

#[path = "tombstone_migration.rs"]
mod tombstones;

#[path = "snapshot_transfer.rs"]
pub(crate) mod transfer;


#[cfg(test)]
mod context_revision_snapshot_tests {
    use super::*;

    fn legacy_snapshot() -> Value {
        json!({"turn_id": "a", "conversation_id": "conv", "presentation_id": "a",
            "lane_id": "main", "parent_turn_id": null, "ordinal": 1, "actor": "assistant",
            "kind": "real", "run_id": "", "status": "completed", "current_attempt_id": null,
            "projection": {"content": "same"}, "projection_revision": 9, "settlement": {},
            "created_at": 1, "updated_at": 2})
    }

    #[test]
    fn offset_is_optional_preserved_and_bounded() {
        let legacy = legacy_snapshot();
        let request: RestoreRequest = serde_json::from_value(legacy.clone()).unwrap();
        assert!(valid_restore(&request));
        assert_eq!(restore_snapshot(&request), legacy);
        let mut enriched = legacy.clone();
        enriched["model_context_display_offset"] = json!(3);
        let request: RestoreRequest = serde_json::from_value(enriched.clone()).unwrap();
        assert!(valid_restore(&request));
        assert_eq!(restore_snapshot(&request), enriched);
        enriched["model_context_display_offset"] = json!(10);
        let request: RestoreRequest = serde_json::from_value(enriched).unwrap();
        assert!(!valid_restore(&request));
    }

    #[test]
    fn private_offset_exports_without_losing_semantic_revision() {
        let document = json!({"turnId": "a", "conversationId": "conv", "presentationId": "a",
            "laneId": "main", "parentTurnId": null, "ordinal": 1, "actor": "assistant",
            "kind": "real", "runId": "", "status": "completed", "currentAttemptId": null,
            "projection": {"content": "same"}, "projectionRevision": 9, "settlement": {},
            "createdAt": 1, "updatedAt": 2, "_modelContextDisplayOffset": 3});
        let mut expected = legacy_snapshot();
        expected["model_context_display_offset"] = json!(3);
        assert_eq!(restore_snapshot_from_document(document.as_object().unwrap()).unwrap(), expected);
        assert_eq!(super::super::model_context_revision(document.as_object().unwrap()).unwrap(), 6);
    }
}
