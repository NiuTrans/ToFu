//! Offline conversation queue item snapshot restoration for the exclusive
//! operator importer.
//!
//! The source keeps one row per item in `storage_queue_items` plus one row
//! per armed conversation in `storage_autopilot_markers`. The native
//! authority splits an item into a blob-capable versioned core, a compact
//! state (position, lease, Turn bindings), a tenant-global ID claim, an
//! owner-local (conversation, priority, position) order index, per-kind
//! conversation summary locators, and a global lease-expiry index. Live
//! enqueue derives positions from current rows, so replaying through it
//! cannot preserve the exact source position and lease state and is not
//! insert-or-match idempotent. This adapter rewrites the whole conversation
//! order index plus the summary locators in the same authority transaction
//! as the restored core/state/claim/lease rows, so derived indexes are
//! rebuilt atomically with the exact source positions. The autopilot marker
//! restore preserves the exact marker JSON and rebuilds its global owner
//! index. Insert-or-match fails closed on a divergent occupant, get
//! reconstructs the exact source snapshot, and scan enumerates the
//! owner-scoped claim catalog independently, cross-checking every claim
//! against its conversation order index. Entry points: the
//! `migration.queue.item.*` and `migration.queue.autopilot.marker.*`
//! operations dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_QUEUE_CONVERSATION_ID_CHARACTERS, MAX_QUEUE_ID_CHARACTERS, MAX_QUEUE_ITEM_CORE_BYTES,
    MIGRATION_SESSION_MAX_PAGE_ROWS, QUEUE_AUTOPILOT_MARKER_NAMESPACE,
    QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
    QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE, QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE,
    QUEUE_ITEM_CORE_NAMESPACE, QUEUE_ITEM_ID_CLAIM_NAMESPACE, QUEUE_ITEM_STATE_NAMESPACE,
};
use crate::versioned_document::{self, SnapshotDocument, SnapshotRestoreOutcome};

const CORE_LOGICAL_NAMESPACE: &str = "conversation_queue";
const KINDS: [&str; 6] = [
    "real",
    "goal_continuation",
    "peer_msg",
    "workflow_step",
    "autopilot",
    "background_receipt",
];

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "queue snapshot differs")
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize, serde::Deserialize)]
struct IndexIdentity {
    id: String,
    owner: u64,
    conversation_id: String,
    kind: String,
    priority: u64,
    created_at_ms: u64,
    position: u64,
}
#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize, serde::Deserialize)]
struct State {
    position: u64,
    leased_until_ms: Option<u64>,
    lease_task_id: String,
    input_turn_id: String,
    output_turn_id: String,
    attempt_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ItemRestoreRequest {
    id: String,
    conv_id: String,
    payload: Map<String, Value>,
    config: Map<String, Value>,
    position: u64,
    kind: String,
    priority: u64,
    created_at_ms: u64,
    leased_until_ms: Option<u64>,
    lease_task_id: String,
    input_turn_id: String,
    output_turn_id: String,
    attempt_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ItemGetRequest {
    id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ItemScanRequest {
    cursor: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MarkerRestoreRequest {
    conv_id: String,
    queue_id: String,
    config: Map<String, Value>,
    created_at_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MarkerGetRequest {
    conv_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MarkerScanRequest {
    cursor: Option<String>,
}

fn owner_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}
fn global_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, raw)
}
fn core_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_ITEM_CORE_NAMESPACE, id.as_bytes())
}
fn state_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_ITEM_STATE_NAMESPACE, id.as_bytes())
}
fn claim_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_ITEM_ID_CLAIM_NAMESPACE, id.as_bytes())
}
fn marker_key(tx: &AuthorityTransaction, conv: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_AUTOPILOT_MARKER_NAMESPACE, conv.as_bytes())
}
fn marker_index_key(tx: &AuthorityTransaction, owner: u64, conv: &str) -> io::Result<EntityKey> {
    let mut raw = owner.to_be_bytes().to_vec();
    push_text(&mut raw, conv)?;
    global_key(tx, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE, &raw)
}

fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("queue text exceeds key bound"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}
fn conversation_prefix(conversation_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    push_text(&mut raw, conversation_id)?;
    Ok(raw)
}
fn order_key(tx: &AuthorityTransaction, identity: &IndexIdentity) -> io::Result<EntityKey> {
    let mut raw = conversation_prefix(&identity.conversation_id)?;
    raw.extend_from_slice(&identity.priority.to_be_bytes());
    raw.extend_from_slice(&identity.position.to_be_bytes());
    push_text(&mut raw, &identity.id)?;
    owner_key(tx, QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE, &raw)
}
fn order_range(
    tx: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )
}
fn kind_code(kind: Option<&str>) -> io::Result<u8> {
    match kind {
        None => Ok(0),
        Some(value) => KINDS
            .iter()
            .position(|item| item == &value)
            .map(|v| v as u8 + 1)
            .ok_or_else(|| invalid_input("invalid queue kind")),
    }
}
fn summary_key(
    tx: &AuthorityTransaction,
    kind: Option<&str>,
    oldest: u64,
    owner: u64,
    conv: &str,
) -> io::Result<EntityKey> {
    let mut raw = vec![kind_code(kind)?];
    raw.extend_from_slice(&oldest.to_be_bytes());
    raw.extend_from_slice(&owner.to_be_bytes());
    push_text(&mut raw, conv)?;
    global_key(tx, QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE, &raw)
}
fn lease_index_key(
    tx: &AuthorityTransaction,
    expiry: u64,
    owner: u64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::new();
    raw.extend_from_slice(&expiry.to_be_bytes());
    raw.extend_from_slice(&owner.to_be_bytes());
    push_text(&mut raw, id)?;
    global_key(tx, QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE, &raw)
}

fn encode<T: serde::Serialize>(value: &T, name: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data(&format!("{name} cannot be encoded")))
}
fn decode<T: serde::de::DeserializeOwned>(raw: &[u8], name: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| invalid_data(&format!("{name} is malformed")))
}

fn core_value(request: &ItemRestoreRequest, owner: u64) -> Map<String, Value> {
    json!({
        "id": request.id,
        "user_id": owner,
        "conv_id": request.conv_id,
        "payload": request.payload,
        "config": request.config,
        "kind": request.kind,
        "priority": request.priority,
        "created_at_ms": request.created_at_ms,
    })
    .as_object()
    .expect("queue core is an object")
    .clone()
}
fn identity_of(request: &ItemRestoreRequest, owner: u64) -> IndexIdentity {
    IndexIdentity {
        id: request.id.clone(),
        owner,
        conversation_id: request.conv_id.clone(),
        kind: request.kind.clone(),
        priority: request.priority,
        created_at_ms: request.created_at_ms,
        position: request.position,
    }
}
fn state_of(request: &ItemRestoreRequest) -> State {
    State {
        position: request.position,
        leased_until_ms: request.leased_until_ms,
        lease_task_id: request.lease_task_id.clone(),
        input_turn_id: request.input_turn_id.clone(),
        output_turn_id: request.output_turn_id.clone(),
        attempt_id: request.attempt_id.clone(),
    }
}

fn read_claim(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<IndexIdentity>> {
    database
        .entity_get(tx, &claim_key(tx, id)?)?
        .map(|raw| decode(&raw, "queue ID claim"))
        .transpose()
}
fn read_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<State>> {
    database
        .entity_get(tx, &state_key(tx, id)?)?
        .map(|raw| decode(&raw, "queue state"))
        .transpose()
}
fn read_core(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &core_key(tx, id)?,
        CORE_LOGICAL_NAMESPACE,
        id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_QUEUE_ITEM_CORE_BYTES,
    )?
    .map(|raw| {
        serde_json::from_slice::<Value>(&raw)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("queue core is malformed"))
    })
    .transpose()
}

fn summary_oldest(items: &[IndexIdentity]) -> std::collections::BTreeMap<Option<String>, u64> {
    let mut out = std::collections::BTreeMap::new();
    for item in items {
        if item.kind != "autopilot" && item.kind != "background_receipt" {
            out.entry(None)
                .and_modify(|v: &mut u64| *v = (*v).min(item.created_at_ms))
                .or_insert(item.created_at_ms);
        }
        out.entry(Some(item.kind.clone()))
            .and_modify(|v| *v = (*v).min(item.created_at_ms))
            .or_insert(item.created_at_ms);
    }
    out
}
fn delete_summaries(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    items: &[IndexIdentity],
) -> io::Result<()> {
    if items.is_empty() {
        return Ok(());
    }
    for (kind, oldest) in summary_oldest(items) {
        database.entity_delete(
            tx,
            summary_key(
                tx,
                kind.as_deref(),
                oldest,
                tx.owner_user_id(),
                items[0].conversation_id.as_str(),
            )?,
        )?;
    }
    Ok(())
}
fn write_summaries(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    items: &[IndexIdentity],
) -> io::Result<()> {
    if items.is_empty() {
        return Ok(());
    }
    for (kind, oldest) in summary_oldest(items) {
        database.entity_put(
            tx,
            summary_key(
                tx,
                kind.as_deref(),
                oldest,
                tx.owner_user_id(),
                &items[0].conversation_id,
            )?,
            encode(
                &json!({"userId":tx.owner_user_id(),"convId":items[0].conversation_id}),
                "queue summary",
            )?,
        )?;
    }
    Ok(())
}

fn validate_item(request: &ItemRestoreRequest) -> io::Result<()> {
    let text_within =
        |value: &str, maximum: usize| !value.is_empty() && value.chars().count() <= maximum;
    if !text_within(&request.id, MAX_QUEUE_ID_CHARACTERS)
        || !text_within(&request.conv_id, MAX_QUEUE_CONVERSATION_ID_CHARACTERS)
        || !KINDS.contains(&request.kind.as_str())
        || request.position == 0
        || request.created_at_ms == 0
        || request.leased_until_ms == Some(0)
        || (request.leased_until_ms.is_none() && !request.lease_task_id.is_empty())
        || (request.leased_until_ms.is_some() && request.lease_task_id.len() > 256)
    {
        return Err(invalid_input("invalid queue snapshot metadata"));
    }
    let core = core_value(request, 1);
    if serde_json::to_vec(&core)
        .map_err(|_| invalid_input("queue core cannot be encoded"))?
        .len()
        > MAX_QUEUE_ITEM_CORE_BYTES
    {
        return Err(invalid_input("queue core exceeds its byte bound"));
    }
    Ok(())
}

fn restore_item(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
    expected_uuid: uuid::Uuid,
) -> io::Result<bool> {
    let request: ItemRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid queue restore request"))?;
    validate_item(&request)?;
    let owner = tx.owner_user_id();
    let expected_core = core_value(&request, owner);
    let expected_identity = identity_of(&request, owner);
    let expected_state = state_of(&request);
    match read_claim(database, tx, &request.id)? {
        Some(existing) => {
            // Insert-or-match against the exact restored layout; a divergent
            // occupant fails closed instead of overwriting.
            if existing != expected_identity
                || read_state(database, tx, &request.id)?.as_ref() != Some(&expected_state)
                || read_core(database, tx, &request.id)?.as_ref() != Some(&expected_core)
            {
                return Err(snapshot_conflict());
            }
            return Ok(false);
        }
        None => {
            if read_state(database, tx, &request.id)?.is_some()
                || read_core(database, tx, &request.id)?.is_some()
            {
                return Err(invalid_data("queue item fragment has no ID claim"));
            }
        }
    }
    // Rewrite the whole conversation order index in the same transaction so
    // summary locators track the exact restored (priority, position) rows.
    let (start, end) = order_range(tx, &request.conv_id)?;
    let mut items = Vec::new();
    let mut page_start = start;
    loop {
        let page = database.entity_scan(
            tx,
            &page_start,
            &end,
            crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS,
        )?;
        let full = page.len() == crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        for (_, raw) in page {
            items.push(decode::<IndexIdentity>(&raw, "queue order index")?);
        }
        if !full {
            break;
        }
        page_start = next
            .ok_or_else(|| invalid_data("queue order index pagination lost its continuation"))?
            .1;
    }
    if items
        .iter()
        .any(|item| item.priority == request.priority && item.position == request.position)
    {
        return Err(invalid_data("queue order slot is occupied"));
    }
    delete_summaries(database, tx, &items)?;
    for item in &items {
        database.entity_delete(tx, order_key(tx, item)?)?;
    }
    items.push(expected_identity.clone());
    items.sort_by_key(|item| (item.priority, item.position, item.id.clone()));
    for item in &items {
        database.entity_put(tx, order_key(tx, item)?, encode(item, "queue order index")?)?;
    }
    write_summaries(database, tx, &items)?;
    match crate::migration::restore_document(
        database,
        tx,
        expected_uuid,
        SnapshotDocument {
            key: core_key(tx, &request.id)?,
            namespace: CORE_LOGICAL_NAMESPACE.into(),
            logical_key: request.id.clone(),
            version: 1,
            updated_at_ms: request.created_at_ms,
            value_json: encode(&expected_core, "queue core")?,
            blob_owner_user_id: TENANT_GLOBAL_OWNER_ID,
        },
    )? {
        SnapshotRestoreOutcome::Inserted => {}
        SnapshotRestoreOutcome::AlreadyMatches => {
            return Err(invalid_data("queue core exists without its ID claim"));
        }
    }
    database.entity_put(
        tx,
        state_key(tx, &request.id)?,
        encode(&expected_state, "queue state")?,
    )?;
    database.entity_put(
        tx,
        claim_key(tx, &request.id)?,
        encode(&expected_identity, "queue ID claim")?,
    )?;
    if let Some(expiry) = request.leased_until_ms {
        database.entity_put(
            tx,
            lease_index_key(tx, expiry, owner, &request.id)?,
            encode(&expected_identity, "queue lease index")?,
        )?;
    }
    // Read back through the decode path before publication.
    if read_claim(database, tx, &request.id)?.as_ref() != Some(&expected_identity)
        || read_state(database, tx, &request.id)?.as_ref() != Some(&expected_state)
        || read_core(database, tx, &request.id)?.as_ref() != Some(&expected_core)
    {
        return Err(invalid_data("queue snapshot readback differs"));
    }
    Ok(true)
}

fn get_item(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ItemGetRequest =
        serde_json::from_value(payload).map_err(|_| invalid_input("invalid queue get request"))?;
    let Some(identity) = read_claim(database, tx, &request.id)? else {
        return Ok(Value::Null);
    };
    if identity.owner != tx.owner_user_id() {
        return Ok(Value::Null);
    }
    let state = read_state(database, tx, &request.id)?
        .ok_or_else(|| invalid_data("queue state is missing"))?;
    let core = read_core(database, tx, &request.id)?
        .ok_or_else(|| invalid_data("queue core is missing"))?;
    if state.position != identity.position {
        return Err(invalid_data("queue state position differs"));
    }
    let payload = core
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("queue payload is malformed"))?;
    let config = core
        .get("config")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("queue config is malformed"))?;
    Ok(json!({
        "id": identity.id,
        "conv_id": identity.conversation_id,
        "payload": payload,
        "config": config,
        "position": identity.position,
        "kind": identity.kind,
        "priority": identity.priority,
        "created_at_ms": identity.created_at_ms,
        "leased_until_ms": state.leased_until_ms,
        "lease_task_id": state.lease_task_id,
        "input_turn_id": state.input_turn_id,
        "output_turn_id": state.output_turn_id,
        "attempt_id": state.attempt_id,
    }))
}

fn scan_items(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ItemScanRequest =
        serde_json::from_value(payload).map_err(|_| invalid_input("invalid queue scan request"))?;
    let mut suffix = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("queue_item:")
            .ok_or_else(|| invalid_input("invalid queue catalog cursor"))?;
        if encoded.is_empty() {
            return Err(invalid_input("invalid queue catalog cursor"));
        }
        suffix = encoded.as_bytes().to_vec();
        suffix.push(0);
    }
    let start = global_key(tx, QUEUE_ITEM_ID_CLAIM_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        QUEUE_ITEM_ID_CLAIM_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, raw) in &page {
        let identity: IndexIdentity = decode(raw, "queue ID claim")?;
        let id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("queue catalog key is malformed"))?;
        if identity.id != id {
            return Err(invalid_data("queue catalog claim differs"));
        }
        if identity.owner == tx.owner_user_id() {
            // Cross-check the independent claim catalog against the
            // conversation order index derived from it.
            let (_, order_end) = order_range(tx, &identity.conversation_id)?;
            if database
                .entity_get(tx, &order_key(tx, &identity)?)?
                .is_none()
            {
                return Err(invalid_data("queue claim is absent from its order index"));
            }
            let _ = order_end;
            rows.push(json!({"id": id}));
        }
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("queue catalog page is malformed"))?;
        Some(format!("queue_item:{last}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn marker_stored(request: &MarkerRestoreRequest, owner: u64) -> Value {
    json!({
        "queueId": request.queue_id,
        "userId": owner,
        "config": request.config,
        "createdAt": request.created_at_ms,
    })
}

fn restore_marker(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: MarkerRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid autopilot marker restore request"))?;
    if request.conv_id.is_empty()
        || request.conv_id.chars().count() > MAX_QUEUE_CONVERSATION_ID_CHARACTERS
        || request.queue_id.is_empty()
        || request.queue_id.chars().count() > MAX_QUEUE_ID_CHARACTERS
        || request.created_at_ms == 0
    {
        return Err(invalid_input("invalid autopilot marker snapshot metadata"));
    }
    let owner = tx.owner_user_id();
    let expected = encode(&marker_stored(&request, owner), "autopilot marker")?;
    let indexed = encode(
        &json!({
            "convId": request.conv_id,
            "queueId": request.queue_id,
            "userId": owner,
            "config": request.config,
            "createdAt": request.created_at_ms,
        }),
        "autopilot index",
    )?;
    match database.entity_get(tx, &marker_key(tx, &request.conv_id)?)? {
        Some(existing) => {
            if existing != expected {
                return Err(snapshot_conflict());
            }
            return Ok(false);
        }
        None => {
            if database
                .entity_get(tx, &marker_index_key(tx, owner, &request.conv_id)?)?
                .is_some()
            {
                return Err(invalid_data("autopilot marker index has no marker"));
            }
        }
    }
    database.entity_put(tx, marker_key(tx, &request.conv_id)?, expected.clone())?;
    database.entity_put(tx, marker_index_key(tx, owner, &request.conv_id)?, indexed)?;
    if database
        .entity_get(tx, &marker_key(tx, &request.conv_id)?)?
        .as_deref()
        != Some(expected.as_slice())
    {
        return Err(invalid_data("autopilot marker readback differs"));
    }
    Ok(true)
}

fn get_marker(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: MarkerGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid autopilot marker get request"))?;
    let Some(raw) = database.entity_get(tx, &marker_key(tx, &request.conv_id)?)? else {
        return Ok(Value::Null);
    };
    let marker: Value = decode(&raw, "autopilot marker")?;
    if marker.get("userId").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
        return Ok(Value::Null);
    }
    Ok(json!({
        "conv_id": request.conv_id,
        "queue_id": marker.get("queueId").and_then(Value::as_str).unwrap_or(""),
        "config": marker.get("config").cloned().unwrap_or(json!({})),
        "created_at_ms": marker.get("createdAt").and_then(Value::as_u64).unwrap_or(0),
    }))
}

fn scan_markers(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: MarkerScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid autopilot marker scan request"))?;
    let mut prefix = tx.owner_user_id().to_be_bytes().to_vec();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("queue_autopilot:")
            .ok_or_else(|| invalid_input("invalid autopilot marker catalog cursor"))?;
        if encoded.is_empty() || encoded.len() > u16::MAX as usize {
            return Err(invalid_input("invalid autopilot marker catalog cursor"));
        }
        push_text(&mut prefix, encoded)?;
        prefix.push(0);
    }
    let start = global_key(tx, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE, &prefix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
        &tx.owner_user_id().to_be_bytes(),
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (_, raw) in &page {
        let entry: Value = decode(raw, "autopilot index")?;
        if entry.get("userId").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
            return Err(invalid_data("autopilot marker index crosses owner scope"));
        }
        let conv_id = entry
            .get("convId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("autopilot marker index is malformed"))?;
        // Cross-check the index against its marker occupant.
        if database
            .entity_get(tx, &marker_key(tx, conv_id)?)?
            .is_none()
        {
            return Err(invalid_data("autopilot marker index target is missing"));
        }
        rows.push(json!({"conv_id": conv_id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("conv_id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("autopilot marker catalog page is malformed"))?;
        Some(format!("queue_autopilot:{last}"))
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
    let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.queue.item.restore" => {
            let inserted = restore_item(database, &mut transaction, payload, expected_uuid)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.queue.item.get" => get_item(database, &mut transaction, payload),
        "migration.queue.item.scan" => scan_items(database, &mut transaction, payload),
        "migration.queue.autopilot.marker.restore" => {
            let inserted = restore_marker(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.queue.autopilot.marker.get" => get_marker(database, &mut transaction, payload),
        "migration.queue.autopilot.marker.scan" => {
            scan_markers(database, &mut transaction, payload)
        }
        _ => Err(invalid_input("unknown queue migration operation")),
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

    fn snapshot(id: &str, conv: &str, priority: u64, position: u64, created: u64) -> Value {
        json!({
            "id": id,
            "conv_id": conv,
            "payload": {"text": format!("body-{id}")},
            "config": {"model": "m"},
            "position": position,
            "kind": "real",
            "priority": priority,
            "created_at_ms": created,
            "leased_until_ms": Value::Null,
            "lease_task_id": "",
            "input_turn_id": "",
            "output_turn_id": "",
            "attempt_id": "",
        })
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let payload = snapshot("q-1", "conv-1", 100, 1, 5_000);
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.queue.item.restore",
                payload.clone()
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
                "migration.queue.item.restore",
                payload
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.queue.item.get",
            json!({"id": "q-1"}),
        )
        .unwrap();
        assert_eq!(get["position"], 1);
        assert_eq!(get["priority"], 100);
        assert_eq!(get["created_at_ms"], 5_000);
        assert_eq!(get["payload"]["text"], "body-q-1");
        assert_eq!(get["leased_until_ms"], Value::Null);
        // A divergent snapshot fails closed.
        let mut divergent = snapshot("q-1", "conv-1", 100, 2, 5_000);
        divergent["position"] = json!(2);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.queue.item.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn order_index_summaries_and_lease_rebuilt_atomically() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.queue.item.restore",
            snapshot("q-1", "conv-1", 100, 1, 1_000),
        )
        .unwrap();
        let mut leased = snapshot("q-2", "conv-1", 50, 2, 2_000);
        leased["leased_until_ms"] = json!(9_000);
        leased["lease_task_id"] = json!("task-9");
        execute(&mut database, 7, 11, "migration.queue.item.restore", leased).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        // The order index holds both items in priority order.
        let (start, end) = order_range(&transaction, "conv-1").unwrap();
        let found = database
            .entity_scan(&mut transaction, &start, &end, 16)
            .unwrap();
        assert_eq!(found.len(), 2);
        let first: IndexIdentity = decode(&found[0].1, "queue order index").unwrap();
        assert_eq!(first.id, "q-2");
        // The lease index row encodes the exact identity.
        let lease = lease_index_key(&transaction, 9_000, 11, "q-2").unwrap();
        let stored = database
            .entity_get(&mut transaction, &lease)
            .unwrap()
            .unwrap();
        let entry: IndexIdentity = decode(&stored, "queue lease index").unwrap();
        assert_eq!(entry.id, "q-2");
        // Summary locators exist for the None and kind buckets.
        let summary = summary_key(&transaction, None, 1_000, 11, "conv-1").unwrap();
        assert!(database
            .entity_get(&mut transaction, &summary)
            .unwrap()
            .is_some());
        let kind_summary = summary_key(&transaction, Some("real"), 1_000, 11, "conv-1").unwrap();
        assert!(database
            .entity_get(&mut transaction, &kind_summary)
            .unwrap()
            .is_some());
    }

    #[test]
    fn passive_receipt_restore_keeps_kind_index_without_recovery_summary() {
        let (_vfs, mut database) = prepared();
        let mut receipt = snapshot("receipt", "conv-1", 80, 1, 1_000);
        receipt["kind"] = json!("background_receipt");
        receipt["payload"] = json!({"text":"done","_backgroundCommand":"bg_1"});
        execute(&mut database, 7, 11, "migration.queue.item.restore", receipt).unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let recovery_summary = summary_key(&transaction, None, 1_000, 11, "conv-1").unwrap();
        assert!(database.entity_get(&mut transaction, &recovery_summary).unwrap().is_none());
        let kind_summary = summary_key(
            &transaction, Some("background_receipt"), 1_000, 11, "conv-1",
        ).unwrap();
        assert!(database.entity_get(&mut transaction, &kind_summary).unwrap().is_some());
    }

    #[test]
    fn scan_enumerates_claims_and_agrees_with_order_index() {
        let (_vfs, mut database) = prepared();
        for (id, conv, position) in [
            ("q-1", "conv-1", 1),
            ("q-2", "conv-1", 2),
            ("q-3", "conv-2", 1),
        ] {
            execute(
                &mut database,
                7,
                11,
                "migration.queue.item.restore",
                snapshot(id, conv, 100, position, 1_000),
            )
            .unwrap();
        }
        let page = execute(
            &mut database,
            7,
            11,
            "migration.queue.item.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(page["cursor"], Value::Null);
        // A different owner sees an empty catalog.
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.queue.item.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn marker_restore_preserves_exact_state_and_is_owner_scoped() {
        let (_vfs, mut database) = prepared();
        let marker = json!({
            "conv_id": "conv-1",
            "queue_id": "q-9",
            "config": {"interval": 60},
            "created_at_ms": 7_000,
        });
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.queue.autopilot.marker.restore",
                marker.clone()
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.queue.autopilot.marker.restore",
                marker
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.queue.autopilot.marker.get",
            json!({"conv_id": "conv-1"}),
        )
        .unwrap();
        assert_eq!(get["queue_id"], "q-9");
        assert_eq!(get["created_at_ms"], 7_000);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.queue.autopilot.marker.get",
                json!({"conv_id": "conv-1"})
            )
            .unwrap(),
            Value::Null
        );
        let scan = execute(
            &mut database,
            7,
            11,
            "migration.queue.autopilot.marker.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(scan["rows"], json!([{"conv_id": "conv-1"}]));
        // A divergent marker fails closed.
        let mut divergent = json!({
            "conv_id": "conv-1",
            "queue_id": "q-other",
            "config": {"interval": 60},
            "created_at_ms": 7_000,
        });
        divergent["queue_id"] = json!("q-other");
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.queue.autopilot.marker.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn invalid_metadata_fails_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"id":"","conv_id":"c","payload":{},"config":{},"position":1,"kind":"real","priority":1,"created_at_ms":1,"leased_until_ms":null,"lease_task_id":"","input_turn_id":"","output_turn_id":"","attempt_id":""}),
            json!({"id":"q","conv_id":"c","payload":{},"config":{},"position":0,"kind":"real","priority":1,"created_at_ms":1,"leased_until_ms":null,"lease_task_id":"","input_turn_id":"","output_turn_id":"","attempt_id":""}),
            json!({"id":"q","conv_id":"c","payload":{},"config":{},"position":1,"kind":"bogus","priority":1,"created_at_ms":1,"leased_until_ms":null,"lease_task_id":"","input_turn_id":"","output_turn_id":"","attempt_id":""}),
            json!({"id":"q","conv_id":"c","payload":{},"config":{},"position":1,"kind":"real","priority":1,"created_at_ms":0,"leased_until_ms":null,"lease_task_id":"","input_turn_id":"","output_turn_id":"","attempt_id":""}),
            json!({"id":"q","conv_id":"c","payload":{},"config":{},"position":1,"kind":"real","priority":1,"created_at_ms":1,"leased_until_ms":null,"lease_task_id":"t","input_turn_id":"","output_turn_id":"","attempt_id":""}),
            json!({"id":"q","conv_id":"c","payload":{},"config":{},"position":1,"kind":"real","priority":1,"created_at_ms":1,"leased_until_ms":null,"lease_task_id":"","input_turn_id":"","output_turn_id":"","attempt_id":"","extra":0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.queue.item.restore",
                payload
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.queue.item.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
