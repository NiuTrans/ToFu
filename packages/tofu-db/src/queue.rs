//! Durable conversation intent queue with compact lease mutations.
//!
//! Large immutable payload/config cores are blob-capable. Compact state owns
//! position, lease and Turn bindings, so dispatch heartbeats never rewrite
//! payloads. Owner-local order indexes serve UI reads; narrowly scoped global
//! summary and expiry indexes serve only internal recovery operations.

use std::collections::{BTreeMap, BTreeSet};
use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS,
    MAX_QUEUE_REAP_ITEMS,
    QUEUE_AUTOPILOT_MARKER_NAMESPACE,
    QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
    QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE, QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE,
    QUEUE_ITEM_CORE_NAMESPACE, QUEUE_ITEM_ID_CLAIM_NAMESPACE, QUEUE_ITEM_STATE_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

#[path = "queue_append_metadata.rs"]
mod append_metadata;

#[cfg(test)]
std::thread_local! {
    static ORDER_ROWS_READ: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

const CORE_LOGICAL_NAMESPACE: &str = "conversation_queue";
const REAP_PROBE_CONTRACT: &str = "tofu.queue.reap-probe/v1";
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
        usize::MAX,
    )?
    .ok_or_else(|| invalid_data("queue core is missing"))?;
    let core = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|v| match v { Value::Object(object) => Some(object), _ => None })
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
    let found = scan_paged(database, tx, start, &end, usize::MAX)?;
    #[cfg(test)]
    ORDER_ROWS_READ.with(|count| count.set(count.get().saturating_add(found.len())));
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
    public_item_view(database, tx, identity, include_documents, false)
}

/// One queue row's public projection.
///
/// `queue.list` serves the queue bar (optionally with the stored documents);
/// `turn.sync.snapshot` serves the transcript, where a queued row is the only
/// explanation for its pending output Turn. Both go through this body so the
/// two surfaces cannot disagree about what a queued row says.
fn public_item_view(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &IndexIdentity,
    include_documents: bool,
    embedded_user_message: bool,
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
    // The transcript shows the operator's own wording: a queued Turn stores it
    // in the embedded `_user_msg`, while `payload.text` may be the model-facing
    // framed body. The queue bar keeps its own precedence.
    let user_message = payload.get("_user_msg").and_then(Value::as_object);
    let text = if embedded_user_message {
        user_message
            .and_then(|value| value.get("content"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .or_else(|| payload.get("_peerText").and_then(Value::as_str)
                .filter(|value| !value.is_empty()))
            .or_else(|| payload.get("text").and_then(Value::as_str))
            .unwrap_or("")
    } else if peer {
        payload.get("_peerText").and_then(Value::as_str).unwrap_or("")
    } else {
        payload.get("text").and_then(Value::as_str).unwrap_or("")
    };
    let timestamp = if embedded_user_message {
        user_message
            .and_then(|value| value.get("timestamp"))
            .and_then(Value::as_u64)
            .filter(|value| *value != 0)
            .or_else(|| payload.get("timestamp").and_then(Value::as_u64)
                .filter(|value| *value != 0))
            .unwrap_or(identity.created_at_ms)
    } else {
        identity.created_at_ms
    };
    let mut result = json!({"queueId":identity.id,"userId":identity.owner,"position":state.position,"kind":identity.kind,"priority":identity.priority,"timestamp":timestamp,"text":text.chars().take(2000).collect::<String>(),"hasImages":python_truthy(payload.get("images")),"hasPdfs":python_truthy(payload.get("pdfTexts")),"hasAttachments":python_truthy(payload.get("attachments")),"hasRefs":python_truthy(payload.get("convRefs")),"hasQuotes":python_truthy(payload.get("replyQuotes"))});
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
    // Background-command completion preview: explicit producer metadata,
    // display text bounded to the contract maxima (commandId 128,
    // command/resultPreview 2000 characters) like the legacy projection.
    if let Some(command_id) = payload
        .get("_backgroundCommand")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        let metadata = payload
            .get("_backgroundCommandCompletion")
            .and_then(Value::as_object);
        let metadata_text = |field: &str| -> String {
            metadata
                .and_then(|map| map.get(field))
                .map(|value| match value {
                    Value::String(text) => text.clone(),
                    Value::Number(number) => number.to_string(),
                    _ => String::new(),
                })
                .unwrap_or_default()
        };
        let command = metadata_text("command");
        let mut preview = metadata_text("resultPreview");
        let mut truncated = metadata
            .and_then(|map| map.get("resultTruncated"))
            .is_some_and(|value| python_truthy(Some(value)));
        let command: String = command.chars().take(2_000).collect();
        if preview.chars().count() > 2_000 {
            preview = preview.chars().take(2_000).collect();
            truncated = true;
        }
        object.insert(
            "backgroundCommand".into(),
            json!({
                "commandId": command_id.chars().take(128).collect::<String>(),
                "status": "completed",
                "command": command,
                "resultPreview": preview,
                "resultTruncated": truncated,
            }),
        );

        if let Some(completion) = object.get_mut("backgroundCommand").and_then(Value::as_object_mut) {
            if let Some(metadata) = metadata {
                for key in ["exitCode", "timedOut", "interrupted"] {
                    if let Some(value) = metadata.get(key) {
                        if (key == "exitCode" && value.is_i64())
                            || (key != "exitCode" && value.is_boolean()) {
                            completion.insert(key.into(), value.clone());
                        }
                    }
                }
            }
        }
    }
    if python_truthy(payload.get("_steerFallback")) {
        object.insert("steerFallback".into(), json!(true));
    }
    Ok(result)
}

/// Owner-scoped queue projection for the Turn sync snapshot.
///
/// A queued Turn pair is durable before its attempt starts, so this row is the
/// browser's only explanation for the pending output Turn. The item shape is
/// the contract's `ConversationQueueItem`, which forbids storage documents
/// (`userId`, `payload`, `config`); preference for the embedded user message
/// mirrors the SQL-side snapshot projection.
pub(crate) fn snapshot_items(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    deadline: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<Value>> {
    let mut items = Vec::new();
    for identity in rows(database, tx, conversation_id)? {
        if deadline() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "queue snapshot deadline elapsed",
            ));
        }
        let mut value = public_item_view(database, tx, &identity, false, true)?;
        value
            .as_object_mut()
            .ok_or_else(|| invalid_data("queue snapshot item is malformed"))?
            .remove("userId");
        items.push(value);
    }
    Ok(items)
}
fn is_legacy_completion(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    identity: &IndexIdentity,
) -> io::Result<bool> {
    if identity.kind != "workflow_step" {
        return Ok(false);
    }
    let core = read_core(database, tx, identity)?;
    Ok(core.get("payload")
        .and_then(Value::as_object)
        .and_then(|payload| payload.get("_backgroundCommand"))
        .and_then(Value::as_str)
        .is_some_and(|command_id| !command_id.is_empty()))
}
fn encode_response(value: &Value) -> io::Result<Option<Vec<u8>>> {
    Ok(Some(encode(value, "queue response")?))
}

fn scan_paged(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    mut start: EntityKey,
    end: &EntityKey,
    limit: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let mut rows = Vec::with_capacity(limit.min(MAX_ENTITY_RANGE_ROWS));
    while rows.len() < limit {
        let page_limit = (limit - rows.len()).min(MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(tx, &start, end, page_limit)?;
        if page.is_empty() { break; }
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if rows.len() == limit {
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
    if let Some(first) = items.first() {
        append_metadata::remove(database, tx, &first.conversation_id)?;
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
    append_metadata::rebuild(database, tx, items)
}
fn summary_oldest(items: &[IndexIdentity]) -> BTreeMap<Option<String>, u64> {
    let mut out: BTreeMap<Option<String>, u64> = BTreeMap::new();
    for item in items {
        if item.kind != "autopilot" && item.kind != "background_receipt" {
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
    let append = if request.kind == "real" {
        append_metadata::eligible(database, tx, &request.conversation_id)?
    } else { None };
    let mut items = if append.is_some() { Vec::new() }
        else { rows(database, tx, &request.conversation_id)? };
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
        // Legacy renumbers only when a synthetic continuation was actually
        // superseded; a plain real-message enqueue keeps insertion positions.
        if !removed.is_empty() {
            renumber(database, tx, &mut items)?;
        }
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
    let position = append.as_ref().map(|metadata| metadata.item_count)
        .or_else(|| u64::try_from(items.len()).ok())
        .and_then(|count| count.checked_add(1))
        .ok_or_else(|| invalid_data("queue position overflow"))?;
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
        usize::MAX,
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
    if let Some(metadata) = append {
        append_metadata::append(database, tx, metadata, &identity)?;
    } else {
        items.push(identity.clone());
        items.sort_by_key(|item| (item.priority, item.position, item.id.clone()));
        write_summaries(database, tx, &items)?;
    }
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

/// Retire a completed-command delivery intent without creating a Turn pair.
/// The caller owns the conversation revision and sync event in this transaction.
pub(crate) fn cancel_background_receipt(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    queue_id: &str,
    now_ms: u64,
) -> io::Result<bool> {
    let Some(identity) = read_identity(database, tx, queue_id, tx.owner_user_id())? else {
        return Ok(false);
    };
    if identity.conversation_id != conversation_id
        || !matches!(identity.kind.as_str(), "background_receipt" | "workflow_step")
    {
        return Ok(false);
    }
    let core = read_core(database, tx, &identity)?;
    let has_completion = core.get("payload")
        .and_then(Value::as_object)
        .and_then(|payload| payload.get("_backgroundCommand"))
        .and_then(Value::as_str)
        .is_some_and(|value| !value.is_empty());
    if !has_completion {
        return Ok(false);
    }
    let state = read_state(database, tx, &identity)?;
    if !state.input_turn_id.is_empty()
        || !state.output_turn_id.is_empty()
        || !state.attempt_id.is_empty()
        || !state.lease_task_id.is_empty()
        || state.leased_until_ms.is_some_and(|expiry| expiry >= now_ms)
    {
        return Err(conflict("background receipt is already being dispatched"));
    }
    let mut items = rows(database, tx, conversation_id)?;
    delete_summaries(database, tx, &items)?;
    remove_item(database, tx, &identity)?;
    items.retain(|item| item != &identity);
    renumber(database, tx, &mut items)?;
    write_summaries(database, tx, &items)?;
    Ok(true)
}

/// Validate the provider-consumption receipt and remove its exact queue rows
/// in the same transaction as the Turn event. Missing rows are replay-safe;
/// present mismatched/protected rows reject the whole transaction.
pub(crate) fn acknowledge_background_receipts(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    acknowledgement: &Value,
    projection: &Map<String, Value>,
) -> io::Result<Vec<String>> {
    let ack = acknowledgement.as_object()
        .ok_or_else(|| invalid_input("invalid background acknowledgement"))?;
    let block_id = ack.get("block_id").and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.chars().count() <= 256)
        .ok_or_else(|| invalid_input("invalid background receipt block"))?;
    let deliveries = ack.get("deliveries").and_then(Value::as_array)
        .ok_or_else(|| invalid_input("invalid background delivery batch"))?;
    let mut expected = BTreeMap::<String, String>::new();
    for delivery in deliveries {
        let queue_id = delivery.get("queue_id").and_then(Value::as_str)
            .filter(|value| !value.is_empty() && value.chars().count() <= 256)
            .ok_or_else(|| invalid_input("invalid background queue identity"))?;
        let command_id = delivery.get("command_id").and_then(Value::as_str)
            .filter(|value| !value.is_empty() && value.chars().count() <= 128)
            .ok_or_else(|| invalid_input("invalid background command identity"))?;
        if expected.insert(queue_id.to_owned(), command_id.to_owned()).is_some() {
            return Err(invalid_input("duplicate background delivery identity"));
        }
    }
    let receipts = projection.get("_bgCommandInjects").and_then(Value::as_array)
        .ok_or_else(|| invalid_input("background acknowledgement requires receipt"))?;
    let mut matching = receipts.iter().filter(|value| {
        value.get("blockId").and_then(Value::as_str) == Some(block_id)
    });
    let receipt = matching.next().ok_or_else(|| invalid_input("background receipt missing"))?;
    if matching.next().is_some() {
        return Err(invalid_input("duplicate background receipt block"));
    }
    let previews = receipt.get("previews").and_then(Value::as_array)
        .ok_or_else(|| invalid_input("invalid background receipt previews"))?;
    let mut observed = BTreeMap::<String, String>::new();
    for preview in previews {
        if let Some(queue_id) = preview.get("queueId").and_then(Value::as_str) {
            let command_id = preview.get("commandId").and_then(Value::as_str)
                .ok_or_else(|| invalid_input("background preview lacks command identity"))?;
            if observed.insert(queue_id.to_owned(), command_id.to_owned()).is_some() {
                return Err(invalid_input("duplicate background preview identity"));
            }
        }
    }
    if expected != observed {
        return Err(invalid_input("background receipt identities disagree"));
    }
    if expected.is_empty() {
        return Ok(Vec::new());
    }
    let mut items = rows(database, tx, conversation_id)?;
    let mut matched = Vec::new();
    for (queue_id, command_id) in &expected {
        let Some(identity) = read_identity(database, tx, queue_id, tx.owner_user_id())? else {
            continue;
        };
        if identity.conversation_id != conversation_id
            || !matches!(identity.kind.as_str(), "background_receipt" | "workflow_step")
        {
            return Err(conflict("background delivery intent differs from receipt"));
        }
        let state = read_state(database, tx, &identity)?;
        if !state.input_turn_id.is_empty()
            || !state.output_turn_id.is_empty()
            || !state.attempt_id.is_empty()
        {
            return Err(conflict("background delivery intent is protected"));
        }
        let core = read_core(database, tx, &identity)?;
        let message = core.get("payload").and_then(Value::as_object)
            .ok_or_else(|| invalid_data("queue payload is malformed"))?;
        if message.get("_backgroundCommand").and_then(Value::as_str)
            != Some(command_id.as_str()) || python_truthy(message.get("_peerHuman"))
        {
            return Err(conflict("background command identity differs"));
        }
        matched.push(identity);
    }
    if matched.is_empty() {
        return Ok(Vec::new());
    }
    matched.sort_by(|left, right| left.position.cmp(&right.position)
        .then_with(|| left.id.cmp(&right.id)));
    let removed_ids: Vec<String> = matched.iter().map(|item| item.id.clone()).collect();
    let selected: BTreeSet<&str> = matched.iter().map(|item| item.id.as_str()).collect();
    delete_summaries(database, tx, &items)?;
    for identity in &matched {
        remove_item(database, tx, identity)?;
    }
    items.retain(|item| !selected.contains(item.id.as_str()));
    renumber(database, tx, &mut items)?;
    write_summaries(database, tx, &items)?;
    Ok(removed_ids)
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

#[derive(Clone, Debug, serde::Serialize)]
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
        mode: String,
        page_size: usize,
        max_bytes: usize,
        cursor: Option<(u64, u64, String)>,
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
    InjectionAck {
        conversation_id: String,
        queue_ids: Vec<String>,
        occurred_at_ms: u64,
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
            | Self::List { conversation_id, .. }
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
            Self::InjectionAck {
                conversation_id,
                queue_ids,
                ..
            } => {
                valid_text(conversation_id, 256)
                    && queue_ids.iter().all(|id| valid_text(id, 256))
            }
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
        if !valid {
            return Err(invalid_input("invalid queue request"));
        }
        // Charge the encoded owned request without allocating another complete
        // message/config buffer. Transaction IR keeps its independent format bound.
        struct ByteCounter(usize);
        impl io::Write for ByteCounter {
            fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
                self.0 = self.0.checked_add(bytes.len())
                    .ok_or_else(|| invalid_input("queue encoded length overflow"))?;
                Ok(bytes.len())
            }
            fn flush(&mut self) -> io::Result<()> { Ok(()) }
        }
        let mut counter = ByteCounter(0);
        serde_json::to_writer(&mut counter, self)
            .map_err(|_| invalid_input("queue request cannot be encoded"))?;
        Ok(counter.0)
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
            encode_response(&visible.unwrap_or(Value::Null))
        }
        Request::AutopilotListAll => {
            let (start, end) = EntityKey::prefix_range(
                tx.tenant_id(),
                TENANT_GLOBAL_OWNER_ID,
                QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
                b"",
            )?;
            let rows = scan_paged(database, tx, start, &end, usize::MAX)?;
            encode_response(&Value::Array(
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
                return encode_response(&value);
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
            encode_response(
                &json!({"armed":true,"queueId":queue_id,"config":config,"createdAt":created_at_ms}),
            )
        }
        Request::AutopilotClear { conversation_id } => {
            let key = marker_key(tx, conversation_id)?;
            let Some(raw) = database.entity_get(tx, &key)? else {
                return encode_response(&json!({"cleared":false}));
            };
            let marker: Value = decode(&raw, "autopilot marker")?;
            if marker["userId"].as_u64() != Some(tx.owner_user_id()) {
                return encode_response(&json!({"cleared":false}));
            }
            database.entity_delete(tx, key)?;
            database.entity_delete(
                tx,
                marker_index_key(tx, tx.owner_user_id(), conversation_id)?,
            )?;
            encode_response(&json!({"cleared":true}))
        }
        Request::List { conversation_id, mode, page_size, max_bytes, cursor } => {
            if !matches!(mode.as_str(), "full" | "preview" | "dispatchable" | "background_pending" | "background_page") {
                return Err(invalid_input("invalid queue list mode"));
            }
            let mut items = rows(database, tx, conversation_id)?;
            if mode == "background_page" { items.sort_by(|a, b| (a.priority, a.created_at_ms, &a.id).cmp(&(b.priority, b.created_at_ms, &b.id))); }
            let paged = mode == "background_page";
            let mut values = Vec::new();
            let mut bytes = 0_usize;
            let mut last = None;
            let mut more = false;
            for item in &items {
                if paged && cursor.as_ref().is_some_and(|after| (item.priority, item.created_at_ms, &item.id) <= (after.0, after.1, &after.2)) { continue; }
                let legacy_completion = if item.kind == "workflow_step"
                    && mode != "full" && mode != "preview" {
                    is_legacy_completion(database, tx, item)?
                } else { false };
                if mode == "dispatchable" && (item.kind == "autopilot"
                    || item.kind == "background_receipt" || legacy_completion) {
                    continue;
                }
                if mode == "background_pending" || paged {
                    if item.kind != "background_receipt" && !legacy_completion {
                        continue;
                    }
                    let state = read_state(database, tx, item)?;
                    if !state.attempt_id.is_empty() {
                        continue;
                    }
                }
                let value = public_item(database, tx, item, mode != "preview")?;
                if paged {
                    let row_bytes = encode(&value, "queue page item")?.len() + usize::from(!values.is_empty());
                    let next_cursor = json!({"priority":item.priority,"createdAt":item.created_at_ms,"queueId":item.id});
                    let overhead = encode(&json!({"items":[], "nextCursor":next_cursor}), "queue page cursor")?.len();
                    if !values.is_empty() && (values.len() >= *page_size || bytes.saturating_add(row_bytes).saturating_add(overhead) > *max_bytes) {
                        more = true;
                        break;
                    }
                    bytes += row_bytes;
                    last = Some(next_cursor);
                }
                values.push(value);
            }
            if paged { encode_response(&json!({"items":values,"nextCursor":if more { last } else { None }})) }
            else { encode_response(&Value::Array(values)) }
        }
        Request::Depth { conversation_id } => {
            let count = rows(database, tx, conversation_id)?
                .iter()
                .filter(|v| v.kind != "autopilot" && v.kind != "background_receipt")
                .count();
            encode_response(&json!({"depth":count}))
        }
        Request::Enqueue {
            conversation_id,
            queue_id,
            kind,
            priority,
            message,
            config,
            created_at_ms,
        } => encode_response(&enqueue_item(
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
            encode_response(&json!({key:found.is_some()}))
        }
        Request::InjectionAck {
            conversation_id,
            queue_ids,
            occurred_at_ms,
        } => {
            // Acknowledge at most one inbox drain; never cancel accepted Turn
            // pairs.  Only never-accepted peer/workflow inbox rows are
            // removable; every other identity is ignored, not an error.
            let wanted: std::collections::BTreeSet<&str> =
                queue_ids.iter().map(String::as_str).collect();
            let mut items = rows(database, tx, conversation_id)?;
            let mut matched: Vec<IndexIdentity> = Vec::new();
            for identity in &items {
                if !wanted.contains(identity.id.as_str()) {
                    continue;
                }
                if identity.kind != "peer_msg"
                    && identity.kind != "workflow_step"
                    && identity.kind != "background_receipt"
                {
                    continue;
                }
                let state = read_state(database, tx, identity)?;
                if !state.input_turn_id.is_empty()
                    || !state.output_turn_id.is_empty()
                    || !state.attempt_id.is_empty()
                {
                    continue;
                }
                let core = read_core(database, tx, identity)?;
                let message = core
                    .get("payload")
                    .and_then(Value::as_object)
                    .ok_or_else(|| invalid_data("queue payload is malformed"))?;
                if python_truthy(message.get("_peerHuman")) {
                    continue;
                }
                let removable = if identity.kind == "peer_msg" {
                    python_truthy(message.get("_peerMessage"))
                } else {
                    message
                        .get("_backgroundCommand")
                        .and_then(Value::as_str)
                        .is_some_and(|completion| !completion.is_empty())
                };
                if removable {
                    matched.push(identity.clone());
                }
            }
            if matched.is_empty() {
                return encode_response(&json!({"removed":0,"removedQueueIds":[]}));
            }
            // Legacy reports removals in (position, id) order; every
            // mutation renumbers positions in priority order, so this sort
            // is a defensive pin of the exact legacy reporting contract.
            matched.sort_by(|left, right| {
                left.position
                    .cmp(&right.position)
                    .then_with(|| left.id.cmp(&right.id))
            });
            delete_summaries(database, tx, &items)?;
            for item in &matched {
                remove_item(database, tx, item)?;
            }
            items.retain(|item| !matched.contains(item));
            renumber(database, tx, &mut items)?;
            write_summaries(database, tx, &items)?;
            let removed_ids: Vec<String> = matched.iter().map(|item| item.id.clone()).collect();
            crate::turn::store_queue_injection_ack_sync_event(
                database,
                tx,
                conversation_id,
                &removed_ids,
                *occurred_at_ms,
            )?;
            encode_response(&json!({"removed":removed_ids.len(),"removedQueueIds":removed_ids}))
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
            // Legacy renumbers only when rows were actually deleted.
            if !removed.is_empty() {
                renumber(database, tx, &mut items)?;
            }
            write_summaries(database, tx, &items)?;
            encode_response(&json!({"cleared":removed.len()}))
        }
        Request::Dequeue {
            conversation_id,
            now_ms,
            lease_ms,
        } => {
            let items = rows(database, tx, conversation_id)?;
            let mut selected = None;
            for identity in items {
                if identity.kind == "autopilot" || identity.kind == "background_receipt" {
                    continue;
                }
                if is_legacy_completion(database, tx, &identity)? {
                    continue;
                }
                let state = read_state(database, tx, &identity)?;
                if state.leased_until_ms.is_none_or(|expiry| expiry < *now_ms) {
                    selected = Some((identity, state));
                    break;
                }
            }
            let Some((identity, mut state)) = selected else {
                return encode_response(&Value::Null);
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
            encode_response(&value)
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
                usize::MAX,
            )?;
            let conversations = found
                .into_iter()
                .map(|(_, v)| decode::<Value>(&v, "queue conversation index"))
                .collect::<io::Result<Vec<_>>>()?;
            if *reap_probe {
                let now = now_ms.ok_or_else(|| invalid_input("queue reap probe requires time"))?;
                let expired = !expired_leases(database, tx, now, false, 1)?.is_empty();
                encode_response(
                    &json!({"reapProbeContract":REAP_PROBE_CONTRACT,"conversations":conversations,"hasExpiredLeases":expired}),
                )
            } else {
                encode_response(&Value::Array(conversations))
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
            encode_response(&json!({"ok":!values.is_empty(),"conversations":values}))
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
        return encode_response(&json!({result_key:false}));
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
    encode_response(&json!({result_key:true}))
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

/// Offline queue import bypasses normal summary maintenance; invalidate the
/// same-owner derived header in its restoring transaction before adding rows.
pub(crate) fn invalidate_append_metadata(database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction, conversation_id: &str) -> io::Result<()> {
    append_metadata::remove(database, tx, conversation_id)
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
    append_metadata::remove(database, tx, conversation_id)?;
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

#[cfg(test)]
mod completion_outcome_tests {
    use super::*;
    use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, AuthenticatedScope};
    use crate::semantic_executor::execute_admitted_request;

    fn execute(database: &mut AuthorityDatabase, operation: &str, payload: Value, command: Option<&str>) -> Value {
        let request = Request {
            correlation_id: [3; 16], deadline_unix_ms: 10_000_000_000,
            owner_id: 11, tenant_id: Some(7), command_id: command.map(str::to_owned),
            schema_id: STORAGE_SCHEMA_VERSION, operation: operation.to_owned(),
            payload: serde_json::to_vec(&payload).unwrap(),
        };
        let admitted = admit_request(&request,
            NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION },
            AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 1_000).unwrap();
        serde_json::from_slice(&execute_admitted_request(database, admitted, 1_000).unwrap()).unwrap()
    }

    #[test]
    fn background_completion_outcome_matches_full_and_preview() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create",
            json!({"conv_id":"outcome-conv","user_id":11}), Some("outcome-create"));
        for (id, metadata) in [
            ("known", json!({"exitCode":7,"timedOut":false,"interrupted":true})),
            ("legacy", json!({})),
            ("invalid", json!({"exitCode":true,"timedOut":"false","interrupted":1})),
        ] {
            execute(&mut database, "queue.enqueue",
                json!({"conv_id":"outcome-conv","user_id":11,
                    "queue_id":id,"kind":"background_receipt","priority":80,
                    "message":{"text":"done","_backgroundCommand":id,
                        "_backgroundCommandCompletion":metadata},
                    "config":{},"created_at_ms":2}), Some(&format!("outcome-enqueue-{id}")));
        }
        let listed = execute(&mut database, "queue.list",
            json!({"conv_id":"outcome-conv","user_id":11}), None);
        // The HTTP snapshot composes queue preview separately from Turn sync.
        let preview = execute(&mut database, "queue.list",
            json!({"conv_id":"outcome-conv","user_id":11,"mode":"preview"}), None);
        for row in listed.as_array().unwrap() {
            let completion = &row["backgroundCommand"];
            assert_eq!(completion["status"], "completed");
            if row["queueId"] == "known" {
                assert_eq!(completion["exitCode"], 7);
                assert_eq!(completion["timedOut"], false);
                assert_eq!(completion["interrupted"], true);
            } else {
                for field in ["exitCode", "timedOut", "interrupted"] {
                    assert!(completion.get(field).is_none());
                }
            }
            let public = preview.as_array().unwrap().iter()
                .find(|item| item["queueId"] == row["queueId"]).unwrap();
            assert_eq!(public["backgroundCommand"], *completion);
        }
    }

    #[test]
    fn admitted_queue_demand_beyond_former_quota_preserves_messages_dedupe_leases_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create",
            json!({"conv_id":"demand-conv","user_id":11}), Some("demand-create"));
        ORDER_ROWS_READ.with(|count| count.set(0));
        let started = std::time::Instant::now();
        for ordinal in 0..1100 {
            execute(&mut database, "queue.enqueue", json!({
                "conv_id":"demand-conv", "user_id":11, "queue_id":format!("demand-{ordinal:04}"),
                "kind":"real", "priority":100, "message":{"text":format!("完整消息-{ordinal}")},
                "config":{}, "created_at_ms":ordinal+1,
            }), Some(&format!("enqueue-{ordinal}")));
        }
        let enqueue_us = started.elapsed().as_micros();
        ORDER_ROWS_READ.with(|count| assert_eq!(count.get(), 0, "ordinary append must not read earlier order rows"));
        let listed = execute(&mut database, "queue.list", json!({"conv_id":"demand-conv","user_id":11}), None);
        assert_eq!(listed.as_array().unwrap().len(), 1100);
        for (ordinal, item) in listed.as_array().unwrap().iter().enumerate() {
            assert_eq!(item["queueId"], format!("demand-{ordinal:04}"));
            assert_eq!(item["payload"]["text"], format!("完整消息-{ordinal}"));
        }
        let lease = json!({"conv_id":"demand-conv","user_id":11,"now_ms":2000,"lease_ms":10000});
        assert_eq!(execute(&mut database, "queue.dequeue", lease.clone(), Some("lease-first"))["queueId"], "demand-0000");
        assert_eq!(execute(&mut database, "queue.dequeue", lease.clone(), Some("lease-second"))["queueId"], "demand-0001");
        execute(&mut database, "queue.lease.release", json!({"queue_id":"demand-0000","user_id":11}), Some("release-first"));
        assert_eq!(execute(&mut database, "queue.dequeue", lease, Some("lease-again"))["queueId"], "demand-0000");
        execute(&mut database, "queue.finalize", json!({"conv_id":"demand-conv","queue_id":"demand-0000","user_id":11}), Some("finalize-first"));
        for ordinal in 1..=2 {
            let result = execute(&mut database, "queue.enqueue", json!({
                "conv_id":"demand-conv", "user_id":11, "queue_id":format!("continuation-{ordinal}"),
                "kind":"goal_continuation", "priority":100, "message":{"text":"continue"},
                "config":{}, "created_at_ms":2000,
            }), Some(&format!("continuation-command-{ordinal}")));
            assert_eq!(result["queueId"], "continuation-1");
            assert_eq!(result["deduped"], ordinal == 2);
        }
        execute(&mut database, "queue.enqueue", json!({"conv_id":"demand-conv","user_id":11,
            "queue_id":"after", "kind":"real","priority":100,"message":{"text":"最后消息"},"config":{},"created_at_ms":3000}), Some("enqueue-after"));
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let listed = execute(&mut reopened, "queue.list", json!({"conv_id":"demand-conv","user_id":11}), None);
        let items = listed.as_array().unwrap();
        assert_eq!(items.len(), 1100);
        assert_eq!(items[0]["queueId"], "demand-0001");
        assert_eq!(items[0]["position"], 1);
        assert_eq!(items.last().unwrap()["queueId"], "after");
        assert!(items.iter().all(|item| item["kind"] == "real"));
        let mut other = reopened.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert!(super::rows(&reopened, &mut other, "demand-conv").unwrap().is_empty());
        assert!(super::item_by_id(&reopened, &mut other, "demand-0001").unwrap().is_none());
        eprintln!("queue_demand messages=1100 leased=true deduped=true superseded=true reopened=true enqueue_us={} elapsed_ms={}", enqueue_us, started.elapsed().as_millis());
    }


    #[test]
    fn admitted_queue_catalogs_and_autopilot_markers_beyond_former_quota_survive_clear_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for ordinal in 0..1101 {
            let conv = format!("catalog-{ordinal:04}");
            execute(&mut database, "conversation.create", json!({"conv_id":conv,"user_id":11}), Some(&format!("create-{ordinal}")));
            execute(&mut database, "queue.enqueue", json!({"conv_id":conv,"user_id":11,
                "queue_id":format!("message-{ordinal}"),"kind":"real","priority":100,
                "message":{"text":format!("保留消息-{ordinal}")},"config":{},"created_at_ms":ordinal+1}), Some(&format!("message-command-{ordinal}")));
            execute(&mut database, "queue.autopilot.arm", json!({"conv_id":conv,"user_id":11,
                "queue_id":format!("autopilot-{ordinal}"),"config":{},"created_at_ms":ordinal+1}), Some(&format!("arm-{ordinal}")));
        }
        let markers = execute(&mut database, "queue.autopilot.list_all", json!({}), None);
        assert_eq!(markers.as_array().unwrap().len(), 1101);
        let conversations = execute(&mut database, "queue.conversations.list_all", json!({}), None);
        assert_eq!(conversations.as_array().unwrap().len(), 1101);
        for ordinal in 0..1101 {
            let conv = format!("catalog-{ordinal:04}");
            assert!(markers.as_array().unwrap().iter().any(|item| item["convId"] == conv && item["userId"] == 11));
            assert!(conversations.as_array().unwrap().iter().any(|item| item["convId"] == conv && item["userId"] == 11));
        }
        execute(&mut database, "queue.autopilot.clear", json!({"conv_id":"catalog-1100","user_id":11}), Some("clear-marker"));
        execute(&mut database, "queue.clear", json!({"conv_id":"catalog-1100","user_id":11}), Some("clear-queue"));
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        for operation in ["queue.autopilot.list_all", "queue.conversations.list_all"] {
            let result = execute(&mut reopened, operation, json!({}), None);
            assert_eq!(result.as_array().unwrap().len(), 1100);
            assert!(result.as_array().unwrap().iter().all(|item| item["convId"] != "catalog-1100"));
        }
    }


    #[test]
    fn append_metadata_bootstraps_legacy_queue_tracks_oldest_and_survives_rollback() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create", json!({"conv_id":"metadata-conv","user_id":11}), Some("metadata-create"));
        for (id, time) in [("first", 100), ("second", 200)] {
            execute(&mut database, "queue.enqueue", json!({"conv_id":"metadata-conv","user_id":11,
                "queue_id":id,"kind":"real","priority":100,"message":{"text":id},"config":{},"created_at_ms":time}), Some(id));
        }
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_metadata::remove(&database, &mut tx, "metadata-conv").unwrap();
        database.commit(tx).unwrap();
        ORDER_ROWS_READ.with(|count| count.set(0));
        execute(&mut database, "queue.enqueue", json!({"conv_id":"metadata-conv","user_id":11,
            "queue_id":"backdated","kind":"real","priority":50,"message":{"text":"backdated"},"config":{},"created_at_ms":50}), Some("backdated"));
        ORDER_ROWS_READ.with(|count| assert_eq!(count.get(), 2, "legacy bootstrap must read the complete existing index"));
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let header = append_metadata::eligible(&database, &mut tx, "metadata-conv").unwrap().unwrap();
        assert_eq!(header.item_count, 3);
        assert_eq!(summary_oldest(&rows(&database, &mut tx, "metadata-conv").unwrap())[&None], 50);
        let item = EnqueueItemRequest { conversation_id: "metadata-conv".into(), queue_id: "rolled-back".into(),
            kind: "real".into(), priority:100, message:serde_json::from_value(json!({"text":"rollback"})).unwrap(),
            config:Map::new(), created_at_ms:1, input_turn_id:String::new(), output_turn_id:String::new(),
            attempt_id:String::new(), dedupe_by_kind:true, include_documents:true };
        enqueue_item(&database, &mut tx, &item).unwrap();
        drop(tx); // staged message and metadata disappear together
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = reopened.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(append_metadata::eligible(&reopened, &mut tx, "metadata-conv").unwrap().unwrap().item_count, 3);
        assert!(item_by_id(&reopened, &mut tx, "rolled-back").unwrap().is_none());
        drop(tx);
        ORDER_ROWS_READ.with(|count| count.set(0));
        execute(&mut reopened, "queue.enqueue", json!({"conv_id":"metadata-conv","user_id":11,
            "queue_id":"new-earliest","kind":"real","priority":100,"message":{"text":"new"},"config":{},"created_at_ms":25}), Some("new-earliest"));
        ORDER_ROWS_READ.with(|count| assert_eq!(count.get(), 0));
        let summaries = execute(&mut reopened, "queue.conversations.list_all", json!({}), None);
        assert_eq!(summaries.as_array().unwrap().len(), 1, "backdated fast append must replace the prior summary key");
        execute(&mut reopened, "queue.clear", json!({"conv_id":"metadata-conv","user_id":11}), Some("metadata-clear"));
        let mut tx = reopened.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(append_metadata::eligible(&reopened, &mut tx, "metadata-conv").unwrap().is_none());
    }


    #[test]
    fn concurrent_append_metadata_cannot_publish_duplicate_position_or_stale_count() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create", json!({"conv_id":"cas-conv","user_id":11}), Some("cas-create"));
        execute(&mut database, "queue.enqueue", json!({"conv_id":"cas-conv","user_id":11,
            "queue_id":"base","kind":"real","priority":100,"message":{"text":"base"},"config":{},"created_at_ms":10}), Some("cas-base"));
        let request = |id: &str| EnqueueItemRequest { conversation_id:"cas-conv".into(), queue_id:id.into(),
            kind:"real".into(), priority:100, message:serde_json::from_value(json!({"text":id})).unwrap(),
            config:Map::new(), created_at_ms:20, input_turn_id:String::new(), output_turn_id:String::new(),
            attempt_id:String::new(), dedupe_by_kind:true, include_documents:true };
        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let mut second = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        enqueue_item(&database, &mut first, &request("first")).unwrap();
        enqueue_item(&database, &mut second, &request("second")).unwrap();
        database.commit(first).unwrap();
        assert!(database.commit(second).is_err());
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(append_metadata::eligible(&database, &mut tx, "cas-conv").unwrap().unwrap().item_count, 2);
        assert!(item_by_id(&database, &mut tx, "second").unwrap().is_none());
        assert_eq!(item_by_id(&database, &mut tx, "first").unwrap().unwrap()["position"], 2);
        let mut foreign = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert!(append_metadata::eligible(&database, &mut foreign, "cas-conv").unwrap().is_none());
    }


    #[test]
    fn admitted_large_queue_core_preserves_escaped_message_config_and_blob_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create", json!({"conv_id":"large-core","user_id":11}), Some("large-create"));
        let text = "一\n".repeat(350_000);
        let setting = "\\\"".repeat(250_000);
        let message = json!({"text":text,"_msgId":"source-large"});
        let config = json!({"retainedSetting":setting});
        let request = super::Request::Enqueue { conversation_id:"large-core".into(), queue_id:"large-intent".into(),
            kind:"real".into(), priority:100, message:message.as_object().unwrap().clone(),
            config:config.as_object().unwrap().clone(), created_at_ms:1 };
        let measured = request.validate().unwrap();
        assert!(measured > 1024 * 1024);
        assert_eq!(measured, serde_json::to_vec(&request).unwrap().len());
        let started = std::time::Instant::now();
        execute(&mut database, "queue.enqueue", json!({"conv_id":"large-core","user_id":11,
            "queue_id":"large-intent","kind":"real","priority":100,"message":message,"config":config,"created_at_ms":1}), Some("large-enqueue"));
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let listed = execute(&mut reopened, "queue.list", json!({"conv_id":"large-core","user_id":11}), None);
        let item = &listed.as_array().unwrap()[0];
        assert_eq!(item["payload"], message);
        assert_eq!(item["config"], config);
        assert_eq!(item["sourceMessageId"], "source-large");
        let mut tx = reopened.begin_with_identity_claim_scopes(7, 11).unwrap();
        let identity = read_identity(&reopened, &mut tx, "large-intent", 11).unwrap().unwrap();
        let key = core_key(&tx, &identity.id).unwrap();
        let row = versioned_document::get_value_with_blob_owner_bounded(&reopened, &mut tx, &key,
            CORE_LOGICAL_NAMESPACE, &identity.id, TENANT_GLOBAL_OWNER_ID, usize::MAX).unwrap().unwrap();
        assert!(row.len() > 1024 * 1024);
        assert!(reopened.entity_get(&mut tx, &key).unwrap().unwrap().len() < row.len());
        let mut foreign = reopened.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert!(super::item_by_id(&reopened, &mut foreign, "large-intent").unwrap().is_none());
        drop(tx);
        drop(foreign);
        let snapshot = json!({"id":"restored-large","conv_id":"large-core", "payload":message,"config":config,
            "position":2,"kind":"real","priority":100,"created_at_ms":2,"leased_until_ms":Value::Null,
            "lease_task_id":"","input_turn_id":"","output_turn_id":"","attempt_id":""});
        let restored = crate::queue_migration::execute(&mut reopened, 7, 11, "migration.queue.item.restore", snapshot.clone()).unwrap();
        assert_eq!(restored["inserted"], true);
        let mut tx = reopened.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(append_metadata::eligible(&reopened, &mut tx, "large-core").unwrap().is_none());
        drop(tx);
        ORDER_ROWS_READ.with(|count| count.set(0));
        let appended = execute(&mut reopened, "queue.enqueue", json!({"conv_id":"large-core","user_id":11,
            "queue_id":"after-import","kind":"real","priority":100,"message":{"text":"after"},"config":{},"created_at_ms":3}), Some("after-import"));
        assert_eq!(appended["position"], 3);
        ORDER_ROWS_READ.with(|count| assert_eq!(count.get(), 2));
        let got = crate::queue_migration::execute(&mut reopened, 7, 11, "migration.queue.item.get", json!({"id":"restored-large"})).unwrap();
        assert_eq!(got["payload"], message);
        assert_eq!(got["config"], config);
        let replay = crate::queue_migration::execute(&mut reopened, 7, 11, "migration.queue.item.restore", snapshot).unwrap();
        assert_eq!(replay["inserted"], false);
        eprintln!("large_queue_core measured_request_bytes={} retained_core_bytes={} reopened=true elapsed_ms={}", measured, row.len(), started.elapsed().as_millis());
    }


    #[test]
    fn admitted_large_injection_ack_keeps_protected_intent_and_exact_replay() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create", json!({"conv_id":"large-ack","user_id":11}), Some("ack-create"));
        let ids = (0..1025).map(|ordinal| format!("peer-{ordinal:04}")).collect::<Vec<_>>();
        for id in &ids {
            execute(&mut database, "queue.enqueue", json!({"conv_id":"large-ack","user_id":11,
                "queue_id":id,"kind":"peer_msg","priority":40,"message":{"text":id,"_peerMessage":true},
                "config":{},"created_at_ms":1}), Some(&format!("enqueue-{id}")));
        }
        execute(&mut database, "queue.enqueue", json!({"conv_id":"large-ack","user_id":11,
            "queue_id":"protected","kind":"real","priority":10,"message":{"text":"keep"},"config":{},"created_at_ms":2}), Some("enqueue-protected"));
        let mut wanted = ids.clone();
        wanted.push("protected".into());
        let payload = json!({"conv_id":"large-ack","user_id":11,"queue_ids":wanted});
        let acknowledged = execute(&mut database, "queue.injection.ack", payload.clone(), Some("large-ack-command"));
        assert_eq!(acknowledged["removed"], 1025);
        assert_eq!(acknowledged["removedQueueIds"], json!(ids));
        assert_eq!(execute(&mut database, "queue.injection.ack", payload.clone(), Some("large-ack-command")), acknowledged);
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(execute(&mut reopened, "queue.injection.ack", payload, Some("ack-after-reopen"))["removed"], 0);
        let listed = execute(&mut reopened, "queue.list", json!({"conv_id":"large-ack","user_id":11}), None);
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["queueId"], "protected");
    }


    #[test]
    fn background_pending_and_explicit_page_beyond_former_count_gate_remain_complete() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, "conversation.create", json!({"conv_id":"pending-all","user_id":11}), Some("pending-create"));
        for ordinal in 0..113 {
            execute(&mut database,"queue.enqueue",json!({"conv_id":"pending-all","user_id":11,
                "queue_id":format!("bg-{ordinal:03}"),"kind":"background_receipt","priority":80,
                "message":{"text":"complete","_backgroundCommand":format!("cmd-{ordinal}")},
                "config":{},"created_at_ms":ordinal+1}),Some(&format!("pending-{ordinal}")));
        }
        let pending = execute(&mut database,"queue.list",json!({"conv_id":"pending-all","user_id":11,"mode":"background_pending"}),None);
        assert_eq!(pending.as_array().unwrap().len(),113);
        let first = execute(&mut database,"queue.list",json!({"conv_id":"pending-all","user_id":11,"mode":"background_page","page_size":100}),None);
        assert_eq!(first["items"].as_array().unwrap().len(),100);
        let second = execute(&mut database,"queue.list",json!({"conv_id":"pending-all","user_id":11,"mode":"background_page","page_size":100,"cursor":first["nextCursor"]}),None);
        assert_eq!(second["items"].as_array().unwrap().len(),13);
        assert_eq!(second["nextCursor"],Value::Null);
        assert_eq!(second["items"][12]["queueId"],"bg-112");
    }


    #[test]
    fn oversized_complete_receipt_pages_progress_and_full_result_exceeds_former_response_quota() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database,"conversation.create",json!({"conv_id":"response-demand","user_id":11}),Some("response-create"));
        let text = "x".repeat(4 * 1024 * 1024 + 512 * 1024);
        for ordinal in 0..2 {
            execute(&mut database,"queue.enqueue",json!({"conv_id":"response-demand","user_id":11,
                "queue_id":format!("response-{ordinal}"),"kind":"background_receipt","priority":80,
                "message":{"text":text,"_backgroundCommand":format!("cmd-{ordinal}")},
                "config":{},"created_at_ms":ordinal+1}),Some(&format!("response-enqueue-{ordinal}")));
        }
        let first = execute(&mut database,"queue.list",json!({"conv_id":"response-demand","user_id":11,"mode":"background_page","max_bytes":1}),None);
        assert_eq!(first["items"].as_array().unwrap().len(),1);
        assert_eq!(first["items"][0]["payload"]["text"].as_str().unwrap(),text);
        assert!(!first["nextCursor"].is_null());
        let second = execute(&mut database,"queue.list",json!({"conv_id":"response-demand","user_id":11,"mode":"background_page","max_bytes":1,"cursor":first["nextCursor"]}),None);
        assert_eq!(second["items"].as_array().unwrap().len(),1);
        assert_eq!(second["items"][0]["queueId"],"response-1");
        assert_eq!(second["nextCursor"],Value::Null);
        let complete = execute(&mut database,"queue.list",json!({"conv_id":"response-demand","user_id":11}),None);
        assert_eq!(complete.as_array().unwrap().len(),2);
        assert!(serde_json::to_vec(&complete).unwrap().len()>8*1024*1024);
        for item in complete.as_array().unwrap() { assert_eq!(item["payload"]["text"].as_str().unwrap(),text); }
    }

}
