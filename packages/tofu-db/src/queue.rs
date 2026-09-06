//! Durable conversation intent queue with compact lease mutations.
//!
//! Large immutable payload/config cores are blob-capable. Compact state owns
//! position, lease and Turn bindings, so dispatch heartbeats never rewrite
//! payloads. Owner-local order indexes serve UI reads; narrowly scoped global
//! summary and expiry indexes serve only internal recovery operations.

use std::collections::BTreeMap;
use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_QUEUE_AUTOPILOT_MARKERS, MAX_QUEUE_GLOBAL_CONVERSATION_ROWS,
    MAX_QUEUE_ITEMS_PER_CONVERSATION, MAX_QUEUE_ITEM_CORE_BYTES, MAX_QUEUE_REAP_ITEMS,
    MAX_QUEUE_RESPONSE_BYTES, QUEUE_AUTOPILOT_MARKER_NAMESPACE,
    QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
    QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE, QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE,
    QUEUE_ITEM_CORE_NAMESPACE, QUEUE_ITEM_ID_CLAIM_NAMESPACE, QUEUE_ITEM_STATE_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const CORE_LOGICAL_NAMESPACE: &str = "conversation_queue";
const REAP_PROBE_CONTRACT: &str = "tofu.queue.reap-probe/v1";
const KINDS: [&str; 5] = [
    "real",
    "goal_continuation",
    "peer_msg",
    "workflow_step",
    "autopilot",
];

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
fn core_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_ITEM_CORE_NAMESPACE, id.as_bytes())
}
fn state_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_ITEM_STATE_NAMESPACE, id.as_bytes())
}
fn claim_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_ITEM_ID_CLAIM_NAMESPACE, id.as_bytes())
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
fn summary_range(
    tx: &AuthorityTransaction,
    kind: Option<&str>,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE,
        &[kind_code(kind)?],
    )
}
fn lease_key(
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
fn marker_key(tx: &AuthorityTransaction, conv: &str) -> io::Result<EntityKey> {
    global_key(tx, QUEUE_AUTOPILOT_MARKER_NAMESPACE, conv.as_bytes())
}
fn marker_index_key(tx: &AuthorityTransaction, owner: u64, conv: &str) -> io::Result<EntityKey> {
    let mut raw = owner.to_be_bytes().to_vec();
    push_text(&mut raw, conv)?;
    global_key(tx, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE, &raw)
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
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
struct State {
    position: u64,
    leased_until_ms: Option<u64>,
    lease_task_id: String,
    input_turn_id: String,
    output_turn_id: String,
    attempt_id: String,
}

fn encode<T: serde::Serialize>(value: &T, name: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data(&format!("{name} cannot be encoded")))
}
fn decode<T: serde::de::DeserializeOwned>(raw: &[u8], name: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| invalid_data(&format!("{name} is malformed")))
}
fn core_owner(core: &Map<String, Value>) -> io::Result<u64> {
    core.get("user_id")
        .and_then(Value::as_u64)
        .filter(|v| *v > 0)
        .ok_or_else(|| invalid_data("queue core owner is malformed"))
}
fn text<'a>(doc: &'a Map<String, Value>, field: &str) -> io::Result<&'a str> {
    doc.get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("queue core text is malformed"))
}

fn python_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64().is_some_and(|number| number != 0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
}

fn read_identity(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    owner: u64,
) -> io::Result<Option<IndexIdentity>> {
    let Some(raw) = database.entity_get(tx, &claim_key(tx, id)?)? else {
        return Ok(None);
    };
    let identity: IndexIdentity = decode(&raw, "queue ID claim")?;
    if identity.owner != owner {
        return Ok(None);
    }
    if identity.id != id {
        return Err(invalid_data("queue ID claim differs"));
    }
    Ok(Some(identity))
}
fn read_core(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &IndexIdentity,
) -> io::Result<Map<String, Value>> {
    let raw = versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &core_key(tx, &identity.id)?,
        CORE_LOGICAL_NAMESPACE,
        &identity.id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_QUEUE_ITEM_CORE_BYTES,
    )?
    .ok_or_else(|| invalid_data("queue core is missing"))?;
    let core = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .ok_or_else(|| invalid_data("queue core is malformed"))?;
    if core_owner(&core)? != identity.owner
        || text(&core, "id")? != identity.id
        || text(&core, "conv_id")? != identity.conversation_id
    {
        return Err(invalid_data("queue core identity differs"));
    }
    Ok(core)
}
fn read_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &IndexIdentity,
) -> io::Result<State> {
    let raw = database
        .entity_get(tx, &state_key(tx, &identity.id)?)?
        .ok_or_else(|| invalid_data("queue state is missing"))?;
    let state: State = decode(&raw, "queue state")?;
    if state.position != identity.position {
        return Err(invalid_data("queue state position differs"));
    }
    Ok(state)
}
fn rows(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conv: &str,
) -> io::Result<Vec<IndexIdentity>> {
    let (start, end) = order_range(tx, conv)?;
    let found = database.entity_scan(tx, &start, &end, MAX_QUEUE_ITEMS_PER_CONVERSATION + 1)?;
    if found.len() > MAX_QUEUE_ITEMS_PER_CONVERSATION {
        return Err(invalid_data("queue conversation exceeds its bound"));
    }
    found
        .into_iter()
        .map(|(_, raw)| {
            let identity: IndexIdentity = decode(&raw, "queue order index")?;
            if identity.owner != tx.owner_user_id() || identity.conversation_id != conv {
                return Err(invalid_data(
                    "queue order index crosses owner or conversation",
                ));
            }
            Ok(identity)
        })
        .collect()
}
fn public_item(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &IndexIdentity,
    include_documents: bool,
) -> io::Result<Value> {
    if read_identity(database, tx, &identity.id, identity.owner)?.as_ref() != Some(identity) {
        return Err(invalid_data("queue order index differs from its ID claim"));
    }
    let core = read_core(database, tx, identity)?;
    let state = read_state(database, tx, identity)?;
    let payload = core
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("queue payload is malformed"))?;
    let config = core
        .get("config")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("queue config is malformed"))?;
    let peer = python_truthy(payload.get("_peerMessage"));
    let mut result = json!({"queueId":identity.id,"userId":identity.owner,"position":state.position,"kind":identity.kind,"priority":identity.priority,"timestamp":identity.created_at_ms,"text":if peer {payload.get("_peerText").and_then(Value::as_str).unwrap_or("")}else{payload.get("text").and_then(Value::as_str).unwrap_or("")}.chars().take(2000).collect::<String>(),"hasImages":python_truthy(payload.get("images")),"hasPdfs":python_truthy(payload.get("pdfTexts")),"hasAttachments":python_truthy(payload.get("attachments")),"hasRefs":python_truthy(payload.get("convRefs")),"hasQuotes":python_truthy(payload.get("replyQuotes"))});
    let object = result.as_object_mut().unwrap();
    if include_documents {
        object.insert("payload".into(), Value::Object(payload.clone()));
        object.insert("config".into(), Value::Object(config.clone()));
    }
    for (value, name) in [
        (&state.input_turn_id, "inputTurnId"),
        (&state.output_turn_id, "outputTurnId"),
        (&state.attempt_id, "attemptId"),
    ] {
        if !value.is_empty() {
            object.insert(name.into(), json!(value));
        }
    }
    let source = payload
        .get("_user_msg")
        .and_then(Value::as_object)
        .and_then(|v| v.get("_msgId"))
        .and_then(Value::as_str)
        .or_else(|| payload.get("_msgId").and_then(Value::as_str))
        .unwrap_or("");
    if !source.is_empty() {
        object.insert("sourceMessageId".into(), json!(source));
    }
    if peer {
        object.insert("isPeerMessage".into(), json!(true));
        object.insert(
            "fromConv".into(),
            payload.get("_fromConv").cloned().unwrap_or(json!("")),
        );
        object.insert(
            "isPeerHuman".into(),
            json!(python_truthy(payload.get("_peerHuman"))),
        );
    }
    Ok(result)
}
fn bounded_response(value: &Value) -> io::Result<Option<Vec<u8>>> {
    let raw = encode(value, "queue response")?;
    if raw.len() > MAX_QUEUE_RESPONSE_BYTES {
        return Err(exhausted("queue response exceeds 8 MiB"));
    }
    Ok(Some(raw))
}

fn scan_paged(
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
        let full = page.len() == page_limit;
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if !full || rows.len() == limit {
            break;
        }
        start = next
            .ok_or_else(|| invalid_data("queue pagination lost its continuation"))?
            .1;
    }
    Ok(rows)
}

fn delete_summaries(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    items: &[IndexIdentity],
) -> io::Result<()> {
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
fn summary_oldest(items: &[IndexIdentity]) -> BTreeMap<Option<String>, u64> {
    let mut out: BTreeMap<Option<String>, u64> = BTreeMap::new();
    for item in items {
        if item.kind != "autopilot" {
            out.entry(None)
                .and_modify(|v| *v = (*v).min(item.created_at_ms))
                .or_insert(item.created_at_ms);
        }
        out.entry(Some(item.kind.clone()))
            .and_modify(|v| *v = (*v).min(item.created_at_ms))
            .or_insert(item.created_at_ms);
    }
    out
}
fn write_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &mut IndexIdentity,
    state: &State,
    new_position: u64,
) -> io::Result<()> {
    database.entity_delete(tx, order_key(tx, identity)?)?;
    identity.position = new_position;
    let mut next = state.clone();
    next.position = new_position;
    database.entity_put(
        tx,
        state_key(tx, &identity.id)?,
        encode(&next, "queue state")?,
    )?;
    database.entity_put(
        tx,
        order_key(tx, identity)?,
        encode(identity, "queue order index")?,
    )?;
    database.entity_put(
        tx,
        claim_key(tx, &identity.id)?,
        encode(identity, "queue ID claim")?,
    )
}
fn renumber(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    items: &mut [IndexIdentity],
) -> io::Result<()> {
    for (index, item) in items.iter_mut().enumerate() {
        let pos = index as u64 + 1;
        if item.position != pos {
            let state = read_state(database, tx, item)?;
            write_state(database, tx, item, &state, pos)?;
        }
    }
    Ok(())
}
fn remove_item(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &IndexIdentity,
) -> io::Result<()> {
    let state = read_state(database, tx, identity)?;
    database.entity_delete(tx, order_key(tx, identity)?)?;
    if let Some(expiry) = state.leased_until_ms {
        database.entity_delete(tx, lease_key(tx, expiry, identity.owner, &identity.id)?)?;
    }
    versioned_document::delete(
        database,
        tx,
        core_key(tx, &identity.id)?,
        CORE_LOGICAL_NAMESPACE,
        &identity.id,
        None,
    )?;
    database.entity_delete(tx, state_key(tx, &identity.id)?)?;
    database.entity_delete(tx, claim_key(tx, &identity.id)?)
}

pub(crate) struct EnqueueItemRequest {
    pub conversation_id: String,
    pub queue_id: String,
    pub kind: String,
    pub priority: u64,
    pub message: Map<String, Value>,
    pub config: Map<String, Value>,
    pub created_at_ms: u64,
    pub input_turn_id: String,
    pub output_turn_id: String,
    pub attempt_id: String,
    pub dedupe_by_kind: bool,
    pub include_documents: bool,
}

pub(crate) fn enqueue_item(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &EnqueueItemRequest,
) -> io::Result<Value> {
    if !crate::conversation_header::active_exists(database, tx, &request.conversation_id)? {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "conversation not found",
        ));
    }
    if database
        .entity_get(tx, &claim_key(tx, &request.queue_id)?)?
        .is_some()
    {
        return Err(conflict("queue item already exists"));
    }
    let mut items = rows(database, tx, &request.conversation_id)?;
    delete_summaries(database, tx, &items)?;
    if request.kind == "real" {
        let removed = items
            .iter()
            .filter(|item| item.kind == "goal_continuation")
            .cloned()
            .collect::<Vec<_>>();
        for item in &removed {
            remove_item(database, tx, item)?;
        }
        items.retain(|item| item.kind != "goal_continuation");
        renumber(database, tx, &mut items)?;
    }
    if request.dedupe_by_kind && matches!(request.kind.as_str(), "autopilot" | "goal_continuation")
    {
        if let Some(existing) = items.iter().find(|item| item.kind == request.kind) {
            write_summaries(database, tx, &items)?;
            let mut value = public_item(database, tx, existing, request.include_documents)?;
            value
                .as_object_mut()
                .ok_or_else(|| invalid_data("queue item response is malformed"))?
                .insert("deduped".into(), json!(true));
            return Ok(value);
        }
    }
    if items.len() >= MAX_QUEUE_ITEMS_PER_CONVERSATION {
        return Err(conflict("queue conversation capacity reached"));
    }
    let position = items.len() as u64 + 1;
    let core = json!({
        "id": request.queue_id,
        "user_id": tx.owner_user_id(),
        "conv_id": request.conversation_id,
        "payload": request.message,
        "config": request.config,
        "kind": request.kind,
        "priority": request.priority,
        "created_at_ms": request.created_at_ms
    });
    let core_map = core
        .as_object()
        .ok_or_else(|| invalid_data("queue core is malformed"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        PutRequest {
            key: core_key(tx, &request.queue_id)?,
            namespace: CORE_LOGICAL_NAMESPACE.into(),
            logical_key: request.queue_id.clone(),
            value_json: encode(core_map, "queue core")?,
            updated_at_ms: request.created_at_ms.max(1),
            expected_version: Some(0),
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_QUEUE_ITEM_CORE_BYTES,
    )?;
    let state = State {
        position,
        leased_until_ms: None,
        lease_task_id: String::new(),
        input_turn_id: request.input_turn_id.clone(),
        output_turn_id: request.output_turn_id.clone(),
        attempt_id: request.attempt_id.clone(),
    };
    let identity = IndexIdentity {
        id: request.queue_id.clone(),
        owner: tx.owner_user_id(),
        conversation_id: request.conversation_id.clone(),
        kind: request.kind.clone(),
        priority: request.priority,
        created_at_ms: request.created_at_ms,
        position,
    };
    database.entity_put(
        tx,
        state_key(tx, &request.queue_id)?,
        encode(&state, "queue state")?,
    )?;
    database.entity_put(
        tx,
        claim_key(tx, &request.queue_id)?,
        encode(&identity, "queue claim")?,
    )?;
    database.entity_put(
        tx,
        order_key(tx, &identity)?,
        encode(&identity, "queue order index")?,
    )?;
    items.push(identity.clone());
    items.sort_by_key(|item| (item.priority, item.position, item.id.clone()));
    write_summaries(database, tx, &items)?;
    let mut value = public_item(database, tx, &identity, request.include_documents)?;
    value
        .as_object_mut()
        .ok_or_else(|| invalid_data("queue item response is malformed"))?
        .insert("deduped".into(), json!(false));
    Ok(value)
}

pub(crate) fn contains_kind(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    kind: &str,
) -> io::Result<bool> {
    if !KINDS.contains(&kind) {
        return Err(invalid_input("invalid queue kind"));
    }
    Ok(rows(database, tx, conversation_id)?
        .iter()
        .any(|item| item.kind == kind))
}

pub(crate) fn item_by_id(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    queue_id: &str,
) -> io::Result<Option<Value>> {
    let Some(identity) = read_identity(database, tx, queue_id, tx.owner_user_id())? else {
        return Ok(None);
    };
    public_item(database, tx, &identity, true).map(Some)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct RemovedTurnQueueItem {
    pub input_turn_id: String,
    pub output_turn_id: String,
    pub attempt_id: String,
}

pub(crate) fn remove_turn_item(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    queue_id: &str,
) -> io::Result<Option<RemovedTurnQueueItem>> {
    let Some(identity) = read_identity(database, tx, queue_id, tx.owner_user_id())? else {
        return Ok(None);
    };
    if identity.conversation_id != conversation_id {
        return Ok(None);
    }
    let state = read_state(database, tx, &identity)?;
    let mut items = rows(database, tx, conversation_id)?;
    if !items.iter().any(|item| item == &identity) {
        return Err(invalid_data(
            "queue ID claim is absent from its conversation index",
        ));
    }
    delete_summaries(database, tx, &items)?;
    remove_item(database, tx, &identity)?;
    items.retain(|item| item != &identity);
    renumber(database, tx, &mut items)?;
    write_summaries(database, tx, &items)?;
    Ok(Some(RemovedTurnQueueItem {
        input_turn_id: state.input_turn_id,
        output_turn_id: state.output_turn_id,
        attempt_id: state.attempt_id,
    }))
}

#[derive(Clone, Debug)]
pub enum Request {
    AutopilotArm {
        conversation_id: String,
        queue_id: String,
        config: Map<String, Value>,
        created_at_ms: u64,
    },
    AutopilotClear {
        conversation_id: String,
    },
    AutopilotGet {
        conversation_id: String,
    },
    AutopilotListAll,
    Enqueue {
        conversation_id: String,
        queue_id: String,
        kind: String,
        priority: u64,
        message: Map<String, Value>,
        config: Map<String, Value>,
        created_at_ms: u64,
    },
    List {
        conversation_id: String,
    },
    Remove {
        conversation_id: String,
        queue_id: String,
    },
    Clear {
        conversation_id: String,
    },
    KindClear {
        conversation_id: String,
        kind: String,
    },
    Dequeue {
        conversation_id: String,
        now_ms: u64,
        lease_ms: u64,
    },
    LeaseBind {
        queue_id: String,
        task_id: String,
        now_ms: u64,
        lease_ms: u64,
    },
    LeaseRelease {
        queue_id: String,
    },
    Finalize {
        conversation_id: String,
        queue_id: String,
    },
    Depth {
        conversation_id: String,
    },
    ConversationsListAll {
        kind: Option<String>,
        reap_probe: bool,
        now_ms: Option<u64>,
    },
    Reap {
        now_ms: u64,
        force: bool,
    },
}
impl Request {
    pub(crate) fn mutates_state(&self) -> bool {
        !matches!(
            self,
            Self::AutopilotGet { .. }
                | Self::AutopilotListAll
                | Self::List { .. }
                | Self::Depth { .. }
                | Self::ConversationsListAll { .. }
        )
    }
    pub(crate) fn validate(&self) -> io::Result<usize> {
        let valid_text =
            |value: &str, maximum: usize| !value.is_empty() && value.chars().count() <= maximum;
        let valid_kind = |kind: &str| KINDS.contains(&kind);
        let valid = match self {
            Self::AutopilotArm {
                conversation_id,
                queue_id,
                ..
            } => valid_text(conversation_id, 256) && valid_text(queue_id, 256),
            Self::AutopilotClear { conversation_id }
            | Self::AutopilotGet { conversation_id }
            | Self::List { conversation_id }
            | Self::Clear { conversation_id }
            | Self::Depth { conversation_id } => valid_text(conversation_id, 256),
            Self::Enqueue {
                conversation_id,
                queue_id,
                kind,
                priority,
                ..
            } => {
                valid_text(conversation_id, 256)
                    && valid_text(queue_id, 256)
                    && valid_kind(kind)
                    && *priority <= 1000
            }
            Self::Remove {
                conversation_id,
                queue_id,
            }
            | Self::Finalize {
                conversation_id,
                queue_id,
            } => valid_text(conversation_id, 256) && valid_text(queue_id, 256),
            Self::KindClear {
                conversation_id,
                kind,
            } => valid_text(conversation_id, 256) && valid_kind(kind),
            Self::Dequeue {
                conversation_id,
                lease_ms,
                ..
            } => valid_text(conversation_id, 256) && (1..=3_600_000).contains(lease_ms),
            Self::LeaseBind {
                queue_id,
                task_id,
                lease_ms,
                ..
            } => {
                valid_text(queue_id, 256)
                    && valid_text(task_id, 256)
                    && (1..=3_600_000).contains(lease_ms)
            }
            Self::LeaseRelease { queue_id } => valid_text(queue_id, 256),
            Self::ConversationsListAll {
                kind,
                reap_probe,
                now_ms,
            } => {
                kind.as_deref().is_none_or(valid_kind)
                    && (!reap_probe || now_ms.is_some())
                    && !(*reap_probe && kind.is_some())
            }
            Self::AutopilotListAll | Self::Reap { .. } => true,
        };
        let raw = format!("{self:?}");
        if !valid || raw.len() > MAX_QUEUE_ITEM_CORE_BYTES {
            return Err(invalid_input("queue request exceeds its bound"));
        }
        Ok(raw.len())
    }
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    match request {
        Request::AutopilotGet { conversation_id } => {
            let value = database
                .entity_get(tx, &marker_key(tx, conversation_id)?)?
                .map(|v| decode::<Value>(&v, "autopilot marker"))
                .transpose()?;
            let visible = value.filter(|marker| {
                marker.get("userId").and_then(Value::as_u64) == Some(tx.owner_user_id())
            });
            bounded_response(&visible.unwrap_or(Value::Null))
        }
        Request::AutopilotListAll => {
            let (start, end) = EntityKey::prefix_range(
                tx.tenant_id(),
                TENANT_GLOBAL_OWNER_ID,
                QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
                b"",
            )?;
            let rows = scan_paged(database, tx, start, &end, MAX_QUEUE_AUTOPILOT_MARKERS + 1)?;
            if rows.len() > MAX_QUEUE_AUTOPILOT_MARKERS {
                return Err(exhausted("autopilot marker feed exceeds its bound"));
            }
            bounded_response(&Value::Array(
                rows.into_iter()
                    .map(|(_, v)| decode(&v, "autopilot index"))
                    .collect::<io::Result<_>>()?,
            ))
        }
        Request::AutopilotArm {
            conversation_id,
            queue_id,
            config,
            created_at_ms,
        } => {
            if !crate::conversation_header::active_exists(database, tx, conversation_id)? {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "conversation not found",
                ));
            }
            if let Some(raw) = database.entity_get(tx, &marker_key(tx, conversation_id)?)? {
                let mut value: Value = decode(&raw, "autopilot marker")?;
                if value.get("userId").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
                    return Err(invalid_data("autopilot marker owner differs"));
                }
                value
                    .as_object_mut()
                    .unwrap()
                    .insert("armed".into(), json!(false));
                return bounded_response(&value);
            }
            let (start, end) = EntityKey::prefix_range(
                tx.tenant_id(),
                TENANT_GLOBAL_OWNER_ID,
                QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
                b"",
            )?;
            if database
                .entity_scan(tx, &start, &end, MAX_QUEUE_AUTOPILOT_MARKERS)?
                .len()
                >= MAX_QUEUE_AUTOPILOT_MARKERS
            {
                return Err(conflict("autopilot marker capacity reached"));
            }
            let stored = json!({"queueId":queue_id,"userId":tx.owner_user_id(),"config":config,"createdAt":created_at_ms});
            database.entity_put(
                tx,
                marker_key(tx, conversation_id)?,
                encode(&stored, "autopilot marker")?,
            )?;
            let indexed = json!({"convId":conversation_id,"queueId":queue_id,"userId":tx.owner_user_id(),"config":config,"createdAt":created_at_ms});
            database.entity_put(
                tx,
                marker_index_key(tx, tx.owner_user_id(), conversation_id)?,
                encode(&indexed, "autopilot index")?,
            )?;
            bounded_response(
                &json!({"armed":true,"queueId":queue_id,"config":config,"createdAt":created_at_ms}),
            )
        }
        Request::AutopilotClear { conversation_id } => {
            let key = marker_key(tx, conversation_id)?;
            let Some(raw) = database.entity_get(tx, &key)? else {
                return bounded_response(&json!({"cleared":false}));
            };
            let marker: Value = decode(&raw, "autopilot marker")?;
            if marker["userId"].as_u64() != Some(tx.owner_user_id()) {
                return bounded_response(&json!({"cleared":false}));
            }
            database.entity_delete(tx, key)?;
            database.entity_delete(
                tx,
                marker_index_key(tx, tx.owner_user_id(), conversation_id)?,
            )?;
            bounded_response(&json!({"cleared":true}))
        }
        Request::List { conversation_id } => {
            let items = rows(database, tx, conversation_id)?;
            let values = items
                .iter()
                .map(|v| public_item(database, tx, v, true))
                .collect::<io::Result<Vec<_>>>()?;
            bounded_response(&Value::Array(values))
        }
        Request::Depth { conversation_id } => {
            let count = rows(database, tx, conversation_id)?
                .iter()
                .filter(|v| v.kind != "autopilot")
                .count();
            bounded_response(&json!({"depth":count}))
        }
        Request::Enqueue {
            conversation_id,
            queue_id,
            kind,
            priority,
            message,
            config,
            created_at_ms,
        } => bounded_response(&enqueue_item(
            database,
            tx,
            &EnqueueItemRequest {
                conversation_id: conversation_id.clone(),
                queue_id: queue_id.clone(),
                kind: kind.clone(),
                priority: *priority,
                message: message.clone(),
                config: config.clone(),
                created_at_ms: *created_at_ms,
                input_turn_id: String::new(),
                output_turn_id: String::new(),
                attempt_id: String::new(),
                dedupe_by_kind: true,
                include_documents: false,
            },
        )?),
        Request::Remove {
            conversation_id,
            queue_id,
        }
        | Request::Finalize {
            conversation_id,
            queue_id,
        } => {
            let mut items = rows(database, tx, conversation_id)?;
            delete_summaries(database, tx, &items)?;
            let found = items.iter().position(|v| v.id == *queue_id);
            if let Some(index) = found {
                let item = items.remove(index);
                remove_item(database, tx, &item)?;
                renumber(database, tx, &mut items)?;
            }
            write_summaries(database, tx, &items)?;
            let key = if matches!(request, Request::Finalize { .. }) {
                "finalized"
            } else {
                "removed"
            };
            bounded_response(&json!({key:found.is_some()}))
        }
        Request::Clear { conversation_id }
        | Request::KindClear {
            conversation_id, ..
        } => {
            let mut items = rows(database, tx, conversation_id)?;
            delete_summaries(database, tx, &items)?;
            let kind = if let Request::KindClear { kind, .. } = request {
                Some(kind.as_str())
            } else {
                None
            };
            let removed = items
                .iter()
                .filter(|v| kind.is_none_or(|k| v.kind == k))
                .cloned()
                .collect::<Vec<_>>();
            for item in &removed {
                remove_item(database, tx, item)?;
            }
            items.retain(|v| kind.is_some_and(|k| v.kind != k));
            renumber(database, tx, &mut items)?;
            write_summaries(database, tx, &items)?;
            bounded_response(&json!({"cleared":removed.len()}))
        }
        Request::Dequeue {
            conversation_id,
            now_ms,
            lease_ms,
        } => {
            let items = rows(database, tx, conversation_id)?;
            let mut selected = None;
            for identity in items {
                if identity.kind == "autopilot" {
                    continue;
                }
                let state = read_state(database, tx, &identity)?;
                if state.leased_until_ms.is_none_or(|expiry| expiry < *now_ms) {
                    selected = Some((identity, state));
                    break;
                }
            }
            let Some((identity, mut state)) = selected else {
                return bounded_response(&Value::Null);
            };
            let value = public_item(database, tx, &identity, true)?;
            if let Some(old) = state.leased_until_ms {
                database.entity_delete(tx, lease_key(tx, old, identity.owner, &identity.id)?)?;
            }
            let expiry = now_ms
                .checked_add(*lease_ms)
                .ok_or_else(|| invalid_input("queue lease overflows"))?;
            state.leased_until_ms = Some(expiry);
            state.lease_task_id.clear();
            database.entity_put(
                tx,
                state_key(tx, &identity.id)?,
                encode(&state, "queue state")?,
            )?;
            database.entity_put(
                tx,
                lease_key(tx, expiry, identity.owner, &identity.id)?,
                encode(&identity, "queue lease index")?,
            )?;
            bounded_response(&value)
        }
        Request::LeaseBind {
            queue_id,
            task_id,
            now_ms,
            lease_ms,
        } => lease_change(
            database,
            tx,
            queue_id,
            Some((
                task_id,
                now_ms
                    .checked_add(*lease_ms)
                    .ok_or_else(|| invalid_input("queue lease overflows"))?,
            )),
            "bound",
        ),
        Request::LeaseRelease { queue_id } => {
            lease_change(database, tx, queue_id, None, "released")
        }
        Request::ConversationsListAll {
            kind,
            reap_probe,
            now_ms,
        } => {
            let (start, end) = summary_range(tx, kind.as_deref())?;
            let found = scan_paged(
                database,
                tx,
                start,
                &end,
                MAX_QUEUE_GLOBAL_CONVERSATION_ROWS + 1,
            )?;
            if found.len() > MAX_QUEUE_GLOBAL_CONVERSATION_ROWS {
                return Err(exhausted("queue conversation feed exceeds its bound"));
            }
            let conversations = found
                .into_iter()
                .map(|(_, v)| decode::<Value>(&v, "queue conversation index"))
                .collect::<io::Result<Vec<_>>>()?;
            if *reap_probe {
                let now = now_ms.ok_or_else(|| invalid_input("queue reap probe requires time"))?;
                let expired = !expired_leases(database, tx, now, false, 1)?.is_empty();
                bounded_response(
                    &json!({"reapProbeContract":REAP_PROBE_CONTRACT,"conversations":conversations,"hasExpiredLeases":expired}),
                )
            } else {
                bounded_response(&Value::Array(conversations))
            }
        }
        Request::Reap { now_ms, force } => {
            let identities =
                expired_leases(database, tx, *now_ms, *force, MAX_QUEUE_REAP_ITEMS + 1)?;
            if identities.len() > MAX_QUEUE_REAP_ITEMS {
                return Err(exhausted("queue reap exceeds its bound"));
            }
            let mut conversations = BTreeMap::new();
            for identity in identities {
                let mut state = read_state(database, tx, &identity)?;
                if let Some(expiry) = state.leased_until_ms {
                    database
                        .entity_delete(tx, lease_key(tx, expiry, identity.owner, &identity.id)?)?;
                    state.leased_until_ms = None;
                    state.lease_task_id.clear();
                    database.entity_put(
                        tx,
                        state_key(tx, &identity.id)?,
                        encode(&state, "queue state")?,
                    )?;
                    conversations.insert(
                        (identity.owner, identity.conversation_id.clone()),
                        json!({"userId":identity.owner,"convId":identity.conversation_id}),
                    );
                }
            }
            let values = conversations.into_values().collect::<Vec<_>>();
            bounded_response(&json!({"ok":!values.is_empty(),"conversations":values}))
        }
    }
}

fn lease_change(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    next: Option<(&String, u64)>,
    result_key: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(identity) = read_identity(database, tx, id, tx.owner_user_id())? else {
        return bounded_response(&json!({result_key:false}));
    };
    let mut state = read_state(database, tx, &identity)?;
    if let Some(old) = state.leased_until_ms {
        database.entity_delete(tx, lease_key(tx, old, identity.owner, id)?)?;
    }
    match next {
        Some((task, expiry)) => {
            state.leased_until_ms = Some(expiry);
            state.lease_task_id = task.clone();
            if identity.kind != "autopilot" {
                database.entity_put(
                    tx,
                    lease_key(tx, expiry, identity.owner, id)?,
                    encode(&identity, "queue lease index")?,
                )?;
            }
        }
        None => {
            state.leased_until_ms = None;
            state.lease_task_id.clear();
        }
    }
    database.entity_put(tx, state_key(tx, id)?, encode(&state, "queue state")?)?;
    bounded_response(&json!({result_key:true}))
}
fn expired_leases(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    now: u64,
    force: bool,
    limit: usize,
) -> io::Result<Vec<IndexIdentity>> {
    let (start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE,
        b"",
    )?;
    let found = database.entity_scan(tx, &start, &end, limit)?;
    let mut out = Vec::new();
    for (key, raw) in found {
        if key.key_bytes().len() < 8 {
            return Err(invalid_data("queue lease index key is malformed"));
        }
        let expiry = u64::from_be_bytes(key.key_bytes()[..8].try_into().unwrap());
        if !force && expiry >= now {
            break;
        }
        let identity: IndexIdentity = decode(&raw, "queue lease index")?;
        if identity.kind == "autopilot" {
            return Err(invalid_data("autopilot item entered queue lease index"));
        }
        if read_identity(database, tx, &identity.id, identity.owner)?.as_ref() != Some(&identity) {
            return Err(invalid_data("queue lease index differs from its ID claim"));
        }
        out.push(identity);
    }
    Ok(out)
}

pub(crate) fn delete_conversation(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<usize> {
    let mut items = rows(database, tx, conversation_id)?;
    delete_summaries(database, tx, &items)?;
    for item in &items {
        remove_item(database, tx, item)?;
    }
    let marker = marker_key(tx, conversation_id)?;
    if let Some(raw) = database.entity_get(tx, &marker)? {
        let value: Value = decode(&raw, "autopilot marker")?;
        if value["userId"].as_u64() != Some(tx.owner_user_id()) {
            return Err(invalid_data("autopilot marker owner differs"));
        }
        database.entity_delete(tx, marker)?;
        database.entity_delete(
            tx,
            marker_index_key(tx, tx.owner_user_id(), conversation_id)?,
        )?;
    }
    let count = items.len();
    items.clear();
    Ok(count)
}
