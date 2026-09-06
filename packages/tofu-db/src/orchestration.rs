//! Durable orchestration definitions, run headers, Goal state, and event pages.
//!
//! Large immutable run inputs are separated from mutable lifecycle state.
//! Tenant-global exact identities support collision fencing and bounded startup
//! recovery; every public projection verifies the embedded owner and tenant
//! label before returning data.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER,
    MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES, MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS,
    MAX_ORCHESTRATION_EVENTS_PER_RUN, MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
    MAX_ORCHESTRATION_EVENT_PAGE_ROWS, MAX_ORCHESTRATION_MAINTENANCE_ROWS,
    MAX_ORCHESTRATION_RESPONSE_BYTES, MAX_ORCHESTRATION_RUNS_PER_OWNER,
    MAX_ORCHESTRATION_RUN_CORE_BYTES, MAX_ORCHESTRATION_RUN_ID_CHARACTERS,
    MAX_ORCHESTRATION_RUN_LIST_ROWS, MAX_ORCHESTRATION_RUN_STATE_BYTES,
    MAX_ORCHESTRATION_TENANT_LABEL_CHARACTERS, ORCHESTRATION_DEFINITION_COUNT_NAMESPACE,
    ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE, ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE,
    ORCHESTRATION_DEFINITION_UPDATED_INDEX_NAMESPACE, ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE,
    ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE, ORCHESTRATION_RUN_CORE_NAMESPACE,
    ORCHESTRATION_RUN_COUNT_NAMESPACE, ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE, ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE, ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_STATE_NAMESPACE, ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const DEFINITION_LOGICAL_NAMESPACE: &str = "orchestration_definitions";
const RUN_CORE_LOGICAL_NAMESPACE: &str = "orchestration_run_core";
const RUN_STATE_LOGICAL_NAMESPACE: &str = "orchestration_run_state";
const EVENT_LOGICAL_NAMESPACE: &str = "orchestration_run_event";
const COUNT_KEY: &[u8] = b"count";
const GOAL_CREATED_BY: &str = "chat_goal_mode";
const GOAL_FORMAT: &str = "tofu.goal-run/v1";
const GOAL_ORCHESTRATION_PREFIX: &str = "chat-goal:";
const MAX_GOAL_OBJECTIVE_CHARACTERS: usize = 48_000;
const STATUSES: [&str; 6] = ["pending", "running", "paused", "done", "error", "aborted"];

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
fn valid_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.chars().count() <= maximum
}
fn text<'a>(payload: &'a Map<String, Value>, field: &str, maximum: usize) -> io::Result<&'a str> {
    payload
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| valid_text(value, maximum))
        .ok_or_else(|| invalid_input("orchestration text field is invalid"))
}
fn optional_text(payload: &Map<String, Value>, field: &str, maximum: usize) -> io::Result<String> {
    match payload.get(field) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(value) => value
            .as_str()
            .filter(|value| value.chars().count() <= maximum)
            .map(ToOwned::to_owned)
            .ok_or_else(|| invalid_input("orchestration optional text field is invalid")),
    }
}
fn integer(payload: &Map<String, Value>, field: &str, default: Option<u64>) -> io::Result<u64> {
    payload
        .get(field)
        .and_then(Value::as_u64)
        .or(default)
        .ok_or_else(|| invalid_input("orchestration integer field is invalid"))
}
fn object(payload: &Map<String, Value>, field: &str) -> io::Result<Map<String, Value>> {
    payload
        .get(field)
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_input("orchestration object field is invalid"))
}
fn encode<T: Serialize>(value: &T, name: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data(&format!("{name} cannot be encoded")))
}
fn decode<T: for<'de> Deserialize<'de>>(raw: &[u8], name: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| invalid_data(&format!("{name} is malformed")))
}
fn bounded_response(value: &Value) -> io::Result<Option<Vec<u8>>> {
    let bytes = encode(value, "orchestration response")?;
    if bytes.len() > MAX_ORCHESTRATION_RESPONSE_BYTES {
        return Err(exhausted("orchestration response exceeds 8 MiB"));
    }
    Ok(Some(bytes))
}
fn owner_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}
fn global_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, raw)
}
fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("orchestration key text is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}
fn descending_text(raw: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        raw.extend_from_slice(&[!byte, 0]);
    }
    raw.push(u8::MAX);
}
fn descending_u64(value: u64) -> [u8; 8] {
    (!value).to_be_bytes()
}
fn namespace_range(
    tx: &AuthorityTransaction,
    owner: u64,
    namespace: &str,
    prefix: &[u8],
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(tx.tenant_id(), owner, namespace, prefix)
}
fn scan_paged(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    mut start: EntityKey,
    end: &EntityKey,
    limit: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let mut rows = Vec::new();
    while rows.len() < limit {
        let page_limit = (limit - rows.len()).min(MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(tx, &start, end, page_limit)?;
        if page.is_empty() {
            break;
        }
        let page_len = page.len();
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if page_len < page_limit || rows.len() >= limit {
            break;
        }
        start = next
            .ok_or_else(|| invalid_data("orchestration pagination lost its continuation"))?
            .1;
    }
    Ok(rows)
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct Identity {
    id: String,
    owner: u64,
    tenant_label: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Definition {
    id: String,
    owner: u64,
    tenant_label: String,
    name: String,
    definition: Value,
    created_at_ms: u64,
    updated_at_ms: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct RunCore {
    id: String,
    owner: u64,
    tenant_label: String,
    orch_id: String,
    name: String,
    definition: Value,
    input: String,
    created_by: String,
    created_at: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct RunState {
    status: String,
    final_text: String,
    error: Value,
    updated_at: u64,
    finished_at: u64,
    next_event_sequence: u64,
    goal_status: String,
    goal_reason: String,
    goal_policy: Value,
    goal_outcome: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct RunIndex {
    id: String,
    owner: u64,
    tenant_label: String,
    orch_id: String,
    created_at: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct EventDocument {
    run_id: String,
    owner: u64,
    tenant_label: String,
    sequence: u64,
    payload: Value,
}

#[derive(Clone, Debug)]
pub struct Request {
    pub operation: String,
    pub payload: Map<String, Value>,
    pub now_ms: u64,
}

impl Request {
    pub(crate) fn validate(&self) -> io::Result<usize> {
        let raw = encode(&self.payload, "orchestration request")?;
        if raw.len() > MAX_ORCHESTRATION_RESPONSE_BYTES || self.now_ms == 0 {
            return Err(invalid_input("orchestration request exceeds its bound"));
        }
        Ok(raw.len())
    }
    pub(crate) fn mutates_state(&self) -> bool {
        !matches!(
            self.operation.as_str(),
            "orchestration.definition.get"
                | "orchestration.definition.list"
                | "orchestration.run.get"
                | "orchestration.run.list"
                | "orchestration.event.page"
                | "goal.run.get"
                | "goal.run.latest"
        )
    }
}

fn tenant_label(payload: &Map<String, Value>) -> io::Result<String> {
    let value = optional_text(
        payload,
        "tenant_id",
        MAX_ORCHESTRATION_TENANT_LABEL_CHARACTERS,
    )?;
    Ok(value.trim().to_owned())
}
fn definition_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(
        tx,
        ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE,
        id.as_bytes(),
    )
}
fn definition_claim_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(
        tx,
        ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE,
        id.as_bytes(),
    )
}
fn definition_count_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(tx, ORCHESTRATION_DEFINITION_COUNT_NAMESPACE, COUNT_KEY)
}
fn definition_index_key(
    tx: &AuthorityTransaction,
    tenant_label: &str,
    updated: u64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::new();
    push_text(&mut raw, tenant_label)?;
    raw.extend_from_slice(&descending_u64(updated));
    descending_text(&mut raw, id);
    owner_key(tx, ORCHESTRATION_DEFINITION_UPDATED_INDEX_NAMESPACE, &raw)
}
fn run_core_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, ORCHESTRATION_RUN_CORE_NAMESPACE, id.as_bytes())
}
fn run_state_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, ORCHESTRATION_RUN_STATE_NAMESPACE, id.as_bytes())
}
fn run_claim_key(tx: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    global_key(tx, ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE, id.as_bytes())
}
fn run_count_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(tx, ORCHESTRATION_RUN_COUNT_NAMESPACE, COUNT_KEY)
}
fn run_created_index_key(
    tx: &AuthorityTransaction,
    tenant_label: &str,
    created: u64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = tx.owner_user_id().to_be_bytes().to_vec();
    push_text(&mut raw, tenant_label)?;
    raw.extend_from_slice(&descending_u64(created));
    descending_text(&mut raw, id);
    global_key(tx, ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE, &raw)
}
fn run_status_index_key(
    tx: &AuthorityTransaction,
    owner: u64,
    tenant_label: &str,
    status: &str,
    created: u64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = owner.to_be_bytes().to_vec();
    push_text(&mut raw, tenant_label)?;
    push_text(&mut raw, status)?;
    raw.extend_from_slice(&descending_u64(created));
    descending_text(&mut raw, id);
    global_key(tx, ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE, &raw)
}
fn run_orch_index_key(
    tx: &AuthorityTransaction,
    tenant_label: &str,
    orch_id: &str,
    created: u64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = tx.owner_user_id().to_be_bytes().to_vec();
    push_text(&mut raw, tenant_label)?;
    push_text(&mut raw, orch_id)?;
    raw.extend_from_slice(&descending_u64(created));
    descending_text(&mut raw, id);
    global_key(
        tx,
        ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE,
        &raw,
    )
}
fn goal_created_index_key(
    tx: &AuthorityTransaction,
    tenant_label: &str,
    orch_id: &str,
    created: u64,
    id: &str,
) -> io::Result<EntityKey> {
    let mut raw = tx.owner_user_id().to_be_bytes().to_vec();
    push_text(&mut raw, tenant_label)?;
    push_text(&mut raw, orch_id)?;
    raw.extend_from_slice(&descending_u64(created));
    descending_text(&mut raw, id);
    global_key(tx, ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE, &raw)
}
fn active_index_key(tx: &AuthorityTransaction, identity: &RunIndex) -> io::Result<EntityKey> {
    let mut raw = identity.owner.to_be_bytes().to_vec();
    push_text(&mut raw, &identity.tenant_label)?;
    push_text(&mut raw, &identity.id)?;
    global_key(tx, ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE, &raw)
}
fn goal_active_key(
    tx: &AuthorityTransaction,
    owner: u64,
    tenant_label: &str,
    orch_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = owner.to_be_bytes().to_vec();
    push_text(&mut raw, tenant_label)?;
    push_text(&mut raw, orch_id)?;
    global_key(tx, ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE, &raw)
}
fn event_prefix(run_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    push_text(&mut raw, run_id)?;
    Ok(raw)
}
fn event_key(tx: &AuthorityTransaction, run_id: &str, sequence: u64) -> io::Result<EntityKey> {
    let mut raw = event_prefix(run_id)?;
    raw.extend_from_slice(&sequence.to_be_bytes());
    global_key(tx, ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE, &raw)
}
fn event_range(tx: &AuthorityTransaction, run_id: &str) -> io::Result<(EntityKey, EntityKey)> {
    namespace_range(
        tx,
        TENANT_GLOBAL_OWNER_ID,
        ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE,
        &event_prefix(run_id)?,
    )
}
fn read_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: EntityKey,
) -> io::Result<u64> {
    database
        .entity_get(tx, &key)?
        .map(|raw| {
            if raw.len() != 8 {
                return Err(invalid_data("orchestration count is malformed"));
            }
            Ok(u64::from_be_bytes(raw.try_into().unwrap()))
        })
        .transpose()
        .map(|value| value.unwrap_or(0))
}
fn write_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: EntityKey,
    value: u64,
) -> io::Result<()> {
    database.entity_put(tx, key, value.to_be_bytes().to_vec())
}
struct GlobalDocumentSpec<'a> {
    namespace: &'a str,
    logical_key: &'a str,
    maximum_bytes: usize,
}

fn put_global_document<T: Serialize>(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: EntityKey,
    spec: GlobalDocumentSpec<'_>,
    value: &T,
    now_ms: u64,
) -> io::Result<()> {
    versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        PutRequest {
            key,
            namespace: spec.namespace.to_owned(),
            logical_key: spec.logical_key.to_owned(),
            value_json: encode(value, spec.namespace)?,
            expected_version: None,
            updated_at_ms: now_ms.max(1),
        },
        TENANT_GLOBAL_OWNER_ID,
        spec.maximum_bytes,
    )?;
    Ok(())
}
fn get_global_document<T: for<'de> Deserialize<'de>>(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &'static str,
    logical_key: &str,
    maximum: usize,
) -> io::Result<Option<T>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        key,
        namespace,
        logical_key,
        TENANT_GLOBAL_OWNER_ID,
        maximum,
    )?
    .map(|raw| decode(&raw, namespace))
    .transpose()
}
fn verify_identity(identity: &Identity, id: &str, owner: u64, label: &str) -> io::Result<bool> {
    if identity.id != id {
        return Err(invalid_data("orchestration identity claim differs"));
    }
    Ok(identity.owner == owner && identity.tenant_label == label)
}
fn read_run(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    owner: u64,
    label: &str,
) -> io::Result<Option<(RunCore, RunState)>> {
    let Some(claim) = database.entity_get(tx, &run_claim_key(tx, id)?)? else {
        return Ok(None);
    };
    let identity: Identity = decode(&claim, "orchestration run claim")?;
    if !verify_identity(&identity, id, owner, label)? {
        return Ok(None);
    }
    let core: RunCore = get_global_document(
        database,
        tx,
        &run_core_key(tx, id)?,
        RUN_CORE_LOGICAL_NAMESPACE,
        id,
        MAX_ORCHESTRATION_RUN_CORE_BYTES,
    )?
    .ok_or_else(|| invalid_data("orchestration run core is missing"))?;
    let state: RunState = get_global_document(
        database,
        tx,
        &run_state_key(tx, id)?,
        RUN_STATE_LOGICAL_NAMESPACE,
        id,
        MAX_ORCHESTRATION_RUN_STATE_BYTES,
    )?
    .ok_or_else(|| invalid_data("orchestration run state is missing"))?;
    if core.id != id || core.owner != owner || core.tenant_label != label {
        return Err(invalid_data("orchestration run core identity differs"));
    }
    Ok(Some((core, state)))
}
fn is_terminal(status: &str) -> bool {
    matches!(status, "done" | "error" | "aborted")
}
fn run_projection(core: &RunCore, state: &RunState, detail: bool) -> Value {
    let mut result = json!({
        "id":core.id,"orch_id":core.orch_id,"name":core.name,
        "status":state.status,"terminal":is_terminal(&state.status),
        "final":state.final_text,"error":state.error,"created_by":core.created_by,
        "created_at":core.created_at,"updated_at":state.updated_at,
        "finished_at":state.finished_at
    });
    if detail {
        result["definition"] = core.definition.clone();
        result["input"] = json!(core.input);
    }
    result
}
fn goal_projection(core: &RunCore, state: &RunState) -> Value {
    let conversation = core
        .orch_id
        .strip_prefix(GOAL_ORCHESTRATION_PREFIX)
        .unwrap_or("");
    json!({
        "format":GOAL_FORMAT,"runId":core.id,"conversationId":conversation,
        "objective":core.input,"status":state.goal_status,"reason":state.goal_reason,
        "policy":state.goal_policy,"final":state.final_text,"outcome":state.goal_outcome,
        "storageStatus":state.status,"terminal":matches!(state.goal_status.as_str(),"completed"|"blocked"|"failed"|"cancelled"),
        "createdAt":core.created_at,"updatedAt":state.updated_at,"finishedAt":state.finished_at
    })
}
fn run_index(core: &RunCore) -> RunIndex {
    RunIndex {
        id: core.id.clone(),
        owner: core.owner,
        tenant_label: core.tenant_label.clone(),
        orch_id: core.orch_id.clone(),
        created_at: core.created_at,
    }
}
fn add_run_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &RunCore,
    state: &RunState,
) -> io::Result<()> {
    let index = run_index(core);
    let encoded = encode(&index, "orchestration run index")?;
    database.entity_put(
        tx,
        run_created_index_key(tx, &core.tenant_label, core.created_at, &core.id)?,
        encoded.clone(),
    )?;
    database.entity_put(
        tx,
        run_status_index_key(
            tx,
            core.owner,
            &core.tenant_label,
            &state.status,
            core.created_at,
            &core.id,
        )?,
        encoded.clone(),
    )?;
    database.entity_put(
        tx,
        run_orch_index_key(
            tx,
            &core.tenant_label,
            &core.orch_id,
            core.created_at,
            &core.id,
        )?,
        encoded.clone(),
    )?;
    if core.created_by == GOAL_CREATED_BY {
        database.entity_put(
            tx,
            goal_created_index_key(
                tx,
                &core.tenant_label,
                &core.orch_id,
                core.created_at,
                &core.id,
            )?,
            encoded.clone(),
        )?;
    }
    if !is_terminal(&state.status) {
        database.entity_put(tx, active_index_key(tx, &index)?, encoded)?;
        if core.created_by == GOAL_CREATED_BY {
            database.entity_put(
                tx,
                goal_active_key(tx, core.owner, &core.tenant_label, &core.orch_id)?,
                encode(&index, "Goal active claim")?,
            )?;
        }
    }
    Ok(())
}
fn write_run_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &RunCore,
    old_status: &str,
    state: &RunState,
    now_ms: u64,
) -> io::Result<()> {
    if old_status != state.status {
        database.entity_delete(
            tx,
            run_status_index_key(
                tx,
                core.owner,
                &core.tenant_label,
                old_status,
                core.created_at,
                &core.id,
            )?,
        )?;
        database.entity_put(
            tx,
            run_status_index_key(
                tx,
                core.owner,
                &core.tenant_label,
                &state.status,
                core.created_at,
                &core.id,
            )?,
            encode(&run_index(core), "orchestration run status index")?,
        )?;
    }
    if is_terminal(&state.status) {
        database.entity_delete(tx, active_index_key(tx, &run_index(core))?)?;
        if core.created_by == GOAL_CREATED_BY {
            database.entity_delete(
                tx,
                goal_active_key(tx, core.owner, &core.tenant_label, &core.orch_id)?,
            )?;
        }
    } else {
        database.entity_put(
            tx,
            active_index_key(tx, &run_index(core))?,
            encode(&run_index(core), "orchestration active index")?,
        )?;
        if core.created_by == GOAL_CREATED_BY {
            database.entity_put(
                tx,
                goal_active_key(tx, core.owner, &core.tenant_label, &core.orch_id)?,
                encode(&run_index(core), "Goal active claim")?,
            )?;
        }
    }
    put_global_document(
        database,
        tx,
        run_state_key(tx, &core.id)?,
        GlobalDocumentSpec {
            namespace: RUN_STATE_LOGICAL_NAMESPACE,
            logical_key: &core.id,
            maximum_bytes: MAX_ORCHESTRATION_RUN_STATE_BYTES,
        },
        state,
        now_ms,
    )
}
fn append_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &RunCore,
    state: &mut RunState,
    sequence: u64,
    event: Value,
    now_ms: u64,
) -> io::Result<bool> {
    if sequence >= MAX_ORCHESTRATION_EVENTS_PER_RUN as u64 {
        return Err(exhausted("orchestration event history exceeds its bound"));
    }
    let key = event_key(tx, &core.id, sequence)?;
    if let Some(existing) = get_global_document::<EventDocument>(
        database,
        tx,
        &key,
        EVENT_LOGICAL_NAMESPACE,
        &format!("{}:{sequence}", core.id),
        MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
    )? {
        if existing.run_id != core.id
            || existing.owner != core.owner
            || existing.tenant_label != core.tenant_label
            || existing.sequence != sequence
            || existing.payload != event
        {
            return Err(conflict(
                "orchestration event sequence has a conflicting payload",
            ));
        }
        return Ok(false);
    }
    let document = EventDocument {
        run_id: core.id.clone(),
        owner: core.owner,
        tenant_label: core.tenant_label.clone(),
        sequence,
        payload: event,
    };
    put_global_document(
        database,
        tx,
        key,
        GlobalDocumentSpec {
            namespace: EVENT_LOGICAL_NAMESPACE,
            logical_key: &format!("{}:{sequence}", core.id),
            maximum_bytes: MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
        },
        &document,
        now_ms,
    )?;
    state.next_event_sequence = state.next_event_sequence.max(sequence.saturating_add(1));
    Ok(true)
}
fn canonical_goal_policy() -> Value {
    json!({
        "solutionHorizon":"long_term","rootCauseRequired":true,
        "verificationEvidenceRequired":true,
        "temporaryPatchPolicy":"reject_when_robust_solution_is_in_scope",
        "iterationBudget":{"default":40,"hardCeiling":64},
        "directive":"Pursue the stated objective for durable long-term benefit. Diagnose and fix root causes, require concrete verification evidence, and do not substitute a temporary patch when a robust maintainable solution is within the delegated scope."
    })
}
fn valid_goal_transition(status: &str, reason: &str) -> bool {
    matches!(
        (status, reason),
        ("completed", "objective_verified")
            | (
                "blocked",
                "iteration_budget_exhausted"
                    | "execution_budget_exhausted"
                    | "no_verified_progress"
            )
            | (
                "failed",
                "worker_lost" | "execution_unavailable" | "runtime_failure"
            )
            | (
                "cancelled",
                "human_stop"
                    | "superseded_by_human"
                    | "superseded_by_new_goal"
                    | "conversation_deleted"
                    | "runtime_shutdown"
            )
    )
}
fn goal_storage_status(status: &str) -> &'static str {
    match status {
        "completed" => "done",
        "blocked" | "failed" => "error",
        "cancelled" => "aborted",
        _ => "running",
    }
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    let payload = &request.payload;
    let label = tenant_label(payload)?;
    match request.operation.as_str() {
        "orchestration.definition.create" => {
            let id = text(
                payload,
                "orchestration_id",
                MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS,
            )?;
            let definition = Value::Object(object(payload, "definition")?);
            let now = integer(payload, "now_ms", None)?;
            if database
                .entity_get(tx, &definition_claim_key(tx, id)?)?
                .is_some()
            {
                return Err(conflict("orchestration definition id already exists"));
            }
            let count_key = definition_count_key(tx)?;
            let count = read_count(database, tx, count_key.clone())?;
            if count >= MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER as u64 {
                return Err(exhausted("orchestration definition capacity reached"));
            }
            let document = Definition {
                id: id.to_owned(),
                owner: tx.owner_user_id(),
                tenant_label: label.clone(),
                name: definition
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                definition,
                created_at_ms: now,
                updated_at_ms: now,
            };
            put_global_document(
                database,
                tx,
                definition_key(tx, id)?,
                GlobalDocumentSpec {
                    namespace: DEFINITION_LOGICAL_NAMESPACE,
                    logical_key: id,
                    maximum_bytes: MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES,
                },
                &document,
                request.now_ms,
            )?;
            database.entity_put(
                tx,
                definition_claim_key(tx, id)?,
                encode(
                    &Identity {
                        id: id.to_owned(),
                        owner: tx.owner_user_id(),
                        tenant_label: label,
                    },
                    "definition claim",
                )?,
            )?;
            database.entity_put(
                tx,
                definition_index_key(tx, &document.tenant_label, now, id)?,
                encode(
                    &Identity {
                        id: id.to_owned(),
                        owner: tx.owner_user_id(),
                        tenant_label: document.tenant_label.clone(),
                    },
                    "definition index",
                )?,
            )?;
            write_count(database, tx, count_key, count + 1)?;
            bounded_response(&definition_projection(&document))
        }
        "orchestration.definition.get" => {
            let id = text(
                payload,
                "orchestration_id",
                MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS,
            )?;
            bounded_response(
                &read_definition(database, tx, id, tx.owner_user_id(), &label)?
                    .map(|v| definition_projection(&v))
                    .unwrap_or(Value::Null),
            )
        }
        "orchestration.definition.list" => {
            let mut prefix = Vec::new();
            push_text(&mut prefix, &label)?;
            let (start, end) = namespace_range(
                tx,
                tx.owner_user_id(),
                ORCHESTRATION_DEFINITION_UPDATED_INDEX_NAMESPACE,
                &prefix,
            )?;
            let rows = scan_paged(
                database,
                tx,
                start,
                &end,
                MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER + 1,
            )?;
            if rows.len() > MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER {
                return Err(invalid_data(
                    "orchestration definition index exceeds its bound",
                ));
            }
            let mut output = Vec::with_capacity(rows.len());
            for (_, raw) in rows {
                let identity: Identity = decode(&raw, "definition index")?;
                if identity.owner != tx.owner_user_id() || identity.tenant_label != label {
                    return Err(invalid_data("definition index identity differs"));
                }
                let item = read_definition(
                    database,
                    tx,
                    &identity.id,
                    identity.owner,
                    &identity.tenant_label,
                )?
                .ok_or_else(|| invalid_data("definition index target missing"))?;
                output.push(definition_projection(&item));
            }
            bounded_response(&Value::Array(output))
        }
        "orchestration.definition.update" | "orchestration.definition.delete" => {
            definition_change(database, tx, request, &label)
        }
        "orchestration.run.create" => run_create(database, tx, request, &label),
        "orchestration.run.get" => {
            let id = text(payload, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
            bounded_response(
                &read_run(database, tx, id, tx.owner_user_id(), &label)?
                    .map(|(c, s)| run_projection(&c, &s, true))
                    .unwrap_or(Value::Null),
            )
        }
        "orchestration.run.list" => run_list(database, tx, request, &label),
        "orchestration.run.update_status" => run_update(database, tx, request, &label),
        "orchestration.run.retire_interrupted" => retire_owner(database, tx, request, &label),
        "orchestration.run.retire_interrupted_all" => retire_all(database, tx, request),
        "orchestration.run.delete" => run_delete(database, tx, request, &label),
        "orchestration.event.append" | "orchestration.event.project" => {
            event_write(database, tx, request, &label)
        }
        "orchestration.event.page" => event_page(database, tx, request, &label),
        "goal.run.get" => {
            let id = text(payload, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
            let value = read_run(database, tx, id, tx.owner_user_id(), &label)?
                .and_then(|(c, s)| {
                    (c.created_by == GOAL_CREATED_BY).then(|| goal_projection(&c, &s))
                })
                .unwrap_or(Value::Null);
            bounded_response(&value)
        }
        "goal.run.latest" => goal_latest(database, tx, request, &label),
        "goal.run.start" => goal_start(database, tx, request, &label),
        "goal.run.transition" => goal_transition(database, tx, request, &label),
        _ => Err(invalid_input("unsupported orchestration operation")),
    }
}

fn definition_projection(value: &Definition) -> Value {
    json!({"id":value.id,"name":value.name,"definition":value.definition,"createdAt":value.created_at_ms,"updatedAt":value.updated_at_ms})
}
fn read_definition(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    owner: u64,
    label: &str,
) -> io::Result<Option<Definition>> {
    let Some(raw) = database.entity_get(tx, &definition_claim_key(tx, id)?)? else {
        return Ok(None);
    };
    let claim: Identity = decode(&raw, "definition claim")?;
    if !verify_identity(&claim, id, owner, label)? {
        return Ok(None);
    }
    let value: Definition = get_global_document(
        database,
        tx,
        &definition_key(tx, id)?,
        DEFINITION_LOGICAL_NAMESPACE,
        id,
        MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES,
    )?
    .ok_or_else(|| invalid_data("definition document missing"))?;
    if value.id != id || value.owner != owner || value.tenant_label != label {
        return Err(invalid_data("definition document identity differs"));
    }
    Ok(Some(value))
}
fn definition_change(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let id = text(
        p,
        "orchestration_id",
        MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS,
    )?;
    let expected = integer(p, "expected_updated_at", None)?;
    let Some(mut current) = read_definition(database, tx, id, tx.owner_user_id(), label)? else {
        return bounded_response(
            &json!({"entry":null,"conflict":false,"current_updated_at":null,"deleted":false}),
        );
    };
    if current.updated_at_ms != expected {
        return bounded_response(
            &json!({"entry":null,"conflict":true,"current_updated_at":current.updated_at_ms,"deleted":false}),
        );
    }
    if request.operation.ends_with("delete") {
        database.entity_delete(tx, definition_key(tx, id)?)?;
        database.entity_delete(tx, definition_claim_key(tx, id)?)?;
        database.entity_delete(
            tx,
            definition_index_key(tx, label, current.updated_at_ms, id)?,
        )?;
        let key = definition_count_key(tx)?;
        let count = read_count(database, tx, key.clone())?;
        write_count(
            database,
            tx,
            key,
            count
                .checked_sub(1)
                .ok_or_else(|| invalid_data("definition count underflow"))?,
        )?;
        return bounded_response(
            &json!({"entry":null,"conflict":false,"current_updated_at":current.updated_at_ms,"deleted":true}),
        );
    }
    let definition = Value::Object(object(p, "definition")?);
    let updated = integer(p, "now_ms", None)?.max(current.updated_at_ms.saturating_add(1));
    database.entity_delete(
        tx,
        definition_index_key(tx, label, current.updated_at_ms, id)?,
    )?;
    current.name = definition
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    current.definition = definition;
    current.updated_at_ms = updated;
    put_global_document(
        database,
        tx,
        definition_key(tx, id)?,
        GlobalDocumentSpec {
            namespace: DEFINITION_LOGICAL_NAMESPACE,
            logical_key: id,
            maximum_bytes: MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES,
        },
        &current,
        request.now_ms,
    )?;
    database.entity_put(
        tx,
        definition_index_key(tx, label, updated, id)?,
        encode(
            &Identity {
                id: id.to_owned(),
                owner: tx.owner_user_id(),
                tenant_label: label.to_owned(),
            },
            "definition index",
        )?,
    )?;
    bounded_response(
        &json!({"entry":definition_projection(&current),"conflict":false,"current_updated_at":updated,"deleted":false}),
    )
}
fn initial_state(now: u64) -> RunState {
    RunState {
        status: "pending".into(),
        final_text: String::new(),
        error: Value::Null,
        updated_at: now,
        finished_at: 0,
        next_event_sequence: 0,
        goal_status: String::new(),
        goal_reason: String::new(),
        goal_policy: json!({}),
        goal_outcome: json!({}),
    }
}
fn create_run(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &RunCore,
    state: &RunState,
    now: u64,
) -> io::Result<()> {
    if database
        .entity_get(tx, &run_claim_key(tx, &core.id)?)?
        .is_some()
    {
        return Err(conflict("orchestration run id already exists"));
    }
    let count_key = run_count_key(tx)?;
    let count = read_count(database, tx, count_key.clone())?;
    if count >= MAX_ORCHESTRATION_RUNS_PER_OWNER as u64 {
        return Err(exhausted("orchestration run capacity reached"));
    }
    put_global_document(
        database,
        tx,
        run_core_key(tx, &core.id)?,
        GlobalDocumentSpec {
            namespace: RUN_CORE_LOGICAL_NAMESPACE,
            logical_key: &core.id,
            maximum_bytes: MAX_ORCHESTRATION_RUN_CORE_BYTES,
        },
        core,
        now,
    )?;
    put_global_document(
        database,
        tx,
        run_state_key(tx, &core.id)?,
        GlobalDocumentSpec {
            namespace: RUN_STATE_LOGICAL_NAMESPACE,
            logical_key: &core.id,
            maximum_bytes: MAX_ORCHESTRATION_RUN_STATE_BYTES,
        },
        state,
        now,
    )?;
    database.entity_put(
        tx,
        run_claim_key(tx, &core.id)?,
        encode(
            &Identity {
                id: core.id.clone(),
                owner: core.owner,
                tenant_label: core.tenant_label.clone(),
            },
            "run claim",
        )?,
    )?;
    add_run_indexes(database, tx, core, state)?;
    write_count(database, tx, count_key, count + 1)
}
fn run_create(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let id = text(p, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
    let definition = Value::Object(object(p, "definition")?);
    let now = request.now_ms;
    let core = RunCore {
        id: id.into(),
        owner: tx.owner_user_id(),
        tenant_label: label.into(),
        orch_id: optional_text(p, "orch_id", 512)?,
        name: optional_text(p, "name", 512)?,
        definition,
        input: optional_text(p, "input", MAX_ORCHESTRATION_RUN_CORE_BYTES)?,
        created_by: optional_text(p, "created_by", 256)?,
        created_at: now,
    };
    let state = initial_state(now);
    create_run(database, tx, &core, &state, now)?;
    bounded_response(&json!({"created":true}))
}
fn indexed_runs(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    limit: usize,
) -> io::Result<Vec<RunIndex>> {
    let mut owner_prefix = tx.owner_user_id().to_be_bytes().to_vec();
    owner_prefix.extend_from_slice(prefix);
    let (start, end) = namespace_range(tx, TENANT_GLOBAL_OWNER_ID, namespace, &owner_prefix)?;
    let rows = scan_paged(database, tx, start, &end, limit)?;
    rows.into_iter()
        .map(|(_, v)| decode(&v, "run index"))
        .collect()
}
fn run_list(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let status = optional_text(p, "status", 64)?;
    if !status.is_empty() && !STATUSES.contains(&status.as_str()) {
        return Err(invalid_input("invalid orchestration run status"));
    }
    let orch = optional_text(p, "orch_id", 512)?;
    let limit = integer(p, "limit", Some(50))? as usize;
    if !(1..=MAX_ORCHESTRATION_RUN_LIST_ROWS).contains(&limit) {
        return Err(invalid_input("invalid orchestration run list limit"));
    }
    let (namespace, prefix) = if !status.is_empty() {
        let mut x = Vec::new();
        push_text(&mut x, label)?;
        push_text(&mut x, &status)?;
        (ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE, x)
    } else if !orch.is_empty() {
        let mut x = Vec::new();
        push_text(&mut x, label)?;
        push_text(&mut x, &orch)?;
        (ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE, x)
    } else {
        let mut x = Vec::new();
        push_text(&mut x, label)?;
        (ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE, x)
    };
    let rows = indexed_runs(
        database,
        tx,
        namespace,
        &prefix,
        MAX_ORCHESTRATION_RUN_LIST_ROWS + 1,
    )?;
    let mut out = Vec::new();
    for index in rows {
        if index.owner != tx.owner_user_id() || index.tenant_label != label {
            return Err(invalid_data("run index identity differs"));
        }
        if (!status.is_empty() || !orch.is_empty()) || out.len() < limit {
            let (c, s) = read_run(database, tx, &index.id, index.owner, &index.tenant_label)?
                .ok_or_else(|| invalid_data("run index target missing"))?;
            if (status.is_empty() || s.status == status) && (orch.is_empty() || c.orch_id == orch) {
                out.push(run_projection(&c, &s, false));
                if out.len() == limit {
                    break;
                }
            }
        }
    }
    bounded_response(&Value::Array(out))
}
fn run_update(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let id = text(p, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
    let status = text(p, "status", 64)?;
    if !STATUSES.contains(&status) {
        return Err(invalid_input("invalid orchestration run status"));
    }
    let Some((core, mut state)) = read_run(database, tx, id, tx.owner_user_id(), label)? else {
        return bounded_response(&json!({"changed":false}));
    };
    if is_terminal(&state.status) && state.status != status {
        return bounded_response(&json!({"changed":false}));
    }
    let old = state.status.clone();
    state.status = status.into();
    if let Some(v) = p.get("final") {
        if !v.is_null() {
            state.final_text = match v {
                Value::String(x) => x.clone(),
                other => other.to_string(),
            };
        }
    }
    if let Some(error) = p.get("error") {
        state.error = error.clone();
    }
    state.updated_at = request.now_ms;
    state.finished_at = if is_terminal(status) {
        state.finished_at.max(request.now_ms)
    } else {
        0
    };
    write_run_state(database, tx, &core, &old, &state, request.now_ms)?;
    bounded_response(&json!({"changed":true}))
}
fn error_value(p: &Map<String, Value>) -> Value {
    p.get("error").cloned().unwrap_or(Value::Null)
}
fn retire_one(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &RunCore,
    mut state: RunState,
    error: Value,
    now: u64,
) -> io::Result<()> {
    let old = state.status.clone();
    state.status = "error".into();
    state.final_text.clear();
    state.error = error.clone();
    state.updated_at = now;
    state.finished_at = state.finished_at.max(now);
    if core.created_by == GOAL_CREATED_BY {
        state.goal_status = "failed".into();
        state.goal_reason = "worker_lost".into();
        state.goal_outcome = json!({"error":error});
        let outcome = state.goal_outcome.clone();
        let sequence = state.next_event_sequence;
        append_event(
            database,
            tx,
            core,
            &mut state,
            sequence,
            json!({"format":GOAL_FORMAT,"type":"goal_run_transition","status":"failed","reason":"worker_lost","outcome":outcome}),
            now,
        )?;
    }
    write_run_state(database, tx, core, &old, &state, now)
}
fn retire_owner(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let mut prefix = tx.owner_user_id().to_be_bytes().to_vec();
    push_text(&mut prefix, label)?;
    let (start, end) = namespace_range(
        tx,
        TENANT_GLOBAL_OWNER_ID,
        ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE,
        &prefix,
    )?;
    let rows = scan_paged(
        database,
        tx,
        start,
        &end,
        MAX_ORCHESTRATION_MAINTENANCE_ROWS + 1,
    )?;
    let mut targets = Vec::new();
    for (_, raw) in rows {
        let index: RunIndex = decode(&raw, "run index")?;
        if index.owner != tx.owner_user_id() || index.tenant_label != label {
            return Err(invalid_data("owner active run index identity differs"));
        }
        let Some((c, s)) = read_run(database, tx, &index.id, index.owner, label)? else {
            return Err(invalid_data("run index target missing"));
        };
        if !is_terminal(&s.status) {
            targets.push((c, s));
            if targets.len() > MAX_ORCHESTRATION_MAINTENANCE_ROWS {
                return Err(exhausted("owner run retirement exceeds its bound"));
            }
        }
    }
    let count = targets.len();
    let error = error_value(&request.payload);
    for (c, s) in targets {
        retire_one(database, tx, &c, s, error.clone(), request.now_ms)?;
    }
    bounded_response(&json!({"retired":count}))
}
fn retire_all(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let (start, end) = namespace_range(
        tx,
        TENANT_GLOBAL_OWNER_ID,
        ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE,
        b"",
    )?;
    let rows = scan_paged(
        database,
        tx,
        start,
        &end,
        MAX_ORCHESTRATION_MAINTENANCE_ROWS + 1,
    )?;
    if rows.len() > MAX_ORCHESTRATION_MAINTENANCE_ROWS {
        return Err(exhausted("global run retirement exceeds its bound"));
    }
    let error = error_value(&request.payload);
    for (_, raw) in &rows {
        let index: RunIndex = decode(raw, "active run index")?;
        let (c, s) = read_run(database, tx, &index.id, index.owner, &index.tenant_label)?
            .ok_or_else(|| invalid_data("active run index target missing"))?;
        if is_terminal(&s.status) {
            return Err(invalid_data("active run index points to terminal state"));
        }
        retire_one(database, tx, &c, s, error.clone(), request.now_ms)?;
    }
    bounded_response(&json!({"retired":rows.len()}))
}
fn run_delete(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let id = text(
        &request.payload,
        "run_id",
        MAX_ORCHESTRATION_RUN_ID_CHARACTERS,
    )?;
    let Some((core, state)) = read_run(database, tx, id, tx.owner_user_id(), label)? else {
        return bounded_response(&json!({"deleted":false}));
    };
    database.entity_delete(tx, run_core_key(tx, id)?)?;
    database.entity_delete(tx, run_state_key(tx, id)?)?;
    database.entity_delete(tx, run_claim_key(tx, id)?)?;
    database.entity_delete(
        tx,
        run_created_index_key(tx, &core.tenant_label, core.created_at, id)?,
    )?;
    database.entity_delete(
        tx,
        run_status_index_key(
            tx,
            core.owner,
            &core.tenant_label,
            &state.status,
            core.created_at,
            id,
        )?,
    )?;
    database.entity_delete(
        tx,
        run_orch_index_key(tx, &core.tenant_label, &core.orch_id, core.created_at, id)?,
    )?;
    database.entity_delete(tx, active_index_key(tx, &run_index(&core))?)?;
    if core.created_by == GOAL_CREATED_BY {
        database.entity_delete(
            tx,
            goal_created_index_key(tx, &core.tenant_label, &core.orch_id, core.created_at, id)?,
        )?;
        database.entity_delete(
            tx,
            goal_active_key(tx, core.owner, &core.tenant_label, &core.orch_id)?,
        )?;
    }
    let (start, end) = event_range(tx, id)?;
    database.entity_retire_range(tx, &start, &end)?;
    let key = run_count_key(tx)?;
    let count = read_count(database, tx, key.clone())?;
    write_count(
        database,
        tx,
        key,
        count
            .checked_sub(1)
            .ok_or_else(|| invalid_data("run count underflow"))?,
    )?;
    bounded_response(&json!({"deleted":true}))
}
fn event_write(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let id = text(p, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
    let sequence = integer(p, "sequence", None)?;
    let event = Value::Object(object(p, "event")?);
    let Some((core, mut state)) = read_run(database, tx, id, tx.owner_user_id(), label)? else {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "orchestration run does not exist",
        ));
    };
    let old = state.status.clone();
    let inserted = append_event(
        database,
        tx,
        &core,
        &mut state,
        sequence,
        event,
        request.now_ms,
    )?;
    if request.operation == "orchestration.event.project" {
        let status = optional_text(p, "status", 64)?;
        if !status.is_empty() && (!STATUSES.contains(&status.as_str()) || is_terminal(&status)) {
            return Err(invalid_input(
                "terminal orchestration status requires explicit transition",
            ));
        }
        if inserted {
            if is_terminal(&state.status) {
                return Err(conflict(
                    "orchestration run header rejected event projection",
                ));
            }
            if !status.is_empty() {
                state.status = status;
            }
            state.updated_at = request.now_ms;
            state.finished_at = 0;
            write_run_state(database, tx, &core, &old, &state, request.now_ms)?;
        }
        return bounded_response(&json!({"projected":true,"inserted":inserted}));
    }
    if inserted {
        write_run_state(database, tx, &core, &old, &state, request.now_ms)?;
    }
    bounded_response(&json!({"inserted":inserted,"accepted":true}))
}
fn event_page(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let payload = &request.payload;
    let run_id = text(payload, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
    let requested = integer(payload, "cursor", Some(0))?;
    let Some((_core, state)) = read_run(database, tx, run_id, tx.owner_user_id(), label)? else {
        return bounded_response(
            &json!({"events":[],"next_cursor":0,"cursor_reset":false,"caught_up":true}),
        );
    };
    let boundary = state.next_event_sequence;
    if requested > boundary {
        return bounded_response(
            &json!({"events":[],"next_cursor":boundary,"cursor_reset":true,"caught_up":true}),
        );
    }
    let mut raw_prefix = event_prefix(run_id)?;
    raw_prefix.extend_from_slice(&requested.to_be_bytes());
    let start = global_key(tx, ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE, &raw_prefix)?;
    let (_, end) = event_range(tx, run_id)?;
    let page = versioned_document::list_with_blob_owner_bounded(
        database,
        tx,
        &start,
        &end,
        EVENT_LOGICAL_NAMESPACE,
        MAX_ORCHESTRATION_EVENT_PAGE_ROWS,
        versioned_document::ListProjectionBounds {
            blob_owner_user_id: TENANT_GLOBAL_OWNER_ID,
            maximum_bytes: MAX_ORCHESTRATION_RESPONSE_BYTES,
        },
    )?;
    let projections: Vec<Value> = decode(&page, "orchestration event page")?;
    let mut events = Vec::with_capacity(projections.len());
    let mut last_sequence = None;
    for projection in projections {
        let document: EventDocument = serde_json::from_value(
            projection
                .get("value")
                .cloned()
                .ok_or_else(|| invalid_data("event page value is missing"))?,
        )
        .map_err(|_| invalid_data("event page value is malformed"))?;
        if document.owner != tx.owner_user_id()
            || document.tenant_label != label
            || document.run_id != run_id
        {
            return Err(invalid_data("event page identity differs"));
        }
        let mut event = document.payload;
        if event.get("seq").is_none() {
            event["seq"] = json!(document.sequence);
        }
        last_sequence = Some(document.sequence);
        events.push(event);
    }
    let next_cursor = if events.len() >= MAX_ORCHESTRATION_EVENT_PAGE_ROWS {
        last_sequence
            .unwrap_or(requested)
            .saturating_add(1)
            .min(boundary)
    } else {
        boundary
    };
    bounded_response(
        &json!({"events":events,"next_cursor":next_cursor,"cursor_reset":false,"caught_up":next_cursor>=boundary}),
    )
}
fn goal_latest(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let conv = text(&request.payload, "conversation_id", 256)?;
    let orch = format!("{GOAL_ORCHESTRATION_PREFIX}{conv}");
    let mut prefix = Vec::new();
    push_text(&mut prefix, label)?;
    push_text(&mut prefix, &orch)?;
    let rows = indexed_runs(
        database,
        tx,
        ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE,
        &prefix,
        1,
    )?;
    let value = if let Some(index) = rows.first() {
        let (c, s) = read_run(database, tx, &index.id, index.owner, label)?
            .ok_or_else(|| invalid_data("goal latest index target missing"))?;
        if c.created_by == GOAL_CREATED_BY {
            goal_projection(&c, &s)
        } else {
            Value::Null
        }
    } else {
        Value::Null
    };
    bounded_response(&value)
}
fn goal_start(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let id = text(p, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
    let conv = text(p, "conversation_id", 256)?;
    let objective = text(p, "objective", MAX_GOAL_OBJECTIVE_CHARACTERS)?;
    let definition = Value::Object(object(p, "definition")?);
    let policy = Value::Object(object(p, "policy")?);
    if policy != canonical_goal_policy() {
        return Err(invalid_input(
            "GoalRun policy does not match the canonical contract",
        ));
    }
    let orch = format!("{GOAL_ORCHESTRATION_PREFIX}{conv}");
    if let Some((core, state)) = read_run(database, tx, id, tx.owner_user_id(), label)? {
        if core.created_by != GOAL_CREATED_BY
            || core.orch_id != orch
            || core.input != objective
            || core.definition != definition
        {
            return Err(conflict("GoalRun id belongs to another launch"));
        }
        if state.goal_policy != policy {
            return Err(conflict("GoalRun id has a different policy"));
        }
        return bounded_response(
            &json!({"created":false,"supersededRunIds":[],"run":goal_projection(&core,&state)}),
        );
    }
    let mut superseded = Vec::new();
    if let Some(raw) =
        database.entity_get(tx, &goal_active_key(tx, tx.owner_user_id(), label, &orch)?)?
    {
        let index: RunIndex = decode(&raw, "Goal active claim")?;
        if index.owner != tx.owner_user_id() || index.tenant_label != label || index.orch_id != orch
        {
            return Err(invalid_data("Goal active claim identity differs"));
        }
        let Some((core, mut state)) = read_run(database, tx, &index.id, index.owner, label)? else {
            return Err(invalid_data("Goal active claim target missing"));
        };
        if core.created_by != GOAL_CREATED_BY || is_terminal(&state.status) {
            return Err(invalid_data("Goal active claim points to invalid state"));
        }
        let old = state.status.clone();
        state.status = "aborted".into();
        state.error = json!({"format":GOAL_FORMAT,"kind":"goal_run_cancelled","reason":"superseded_by_new_goal","supersededByRunId":id});
        state.updated_at = request.now_ms;
        state.finished_at = state.finished_at.max(request.now_ms);
        state.goal_status = "cancelled".into();
        state.goal_reason = "superseded_by_new_goal".into();
        let sequence = state.next_event_sequence;
        append_event(
            database,
            tx,
            &core,
            &mut state,
            sequence,
            json!({"format":GOAL_FORMAT,"type":"goal_run_transition","status":"cancelled","reason":"superseded_by_new_goal","supersededByRunId":id}),
            request.now_ms,
        )?;
        write_run_state(database, tx, &core, &old, &state, request.now_ms)?;
        superseded.push(core.id);
    }
    let core = RunCore {
        id: id.into(),
        owner: tx.owner_user_id(),
        tenant_label: label.into(),
        orch_id: orch,
        name: "Goal Mode".into(),
        definition,
        input: objective.into(),
        created_by: GOAL_CREATED_BY.into(),
        created_at: request.now_ms,
    };
    let mut state = initial_state(request.now_ms);
    state.status = "running".into();
    state.goal_status = "active".into();
    state.goal_reason = "started".into();
    state.goal_policy = policy.clone();
    append_event(
        database,
        tx,
        &core,
        &mut state,
        0,
        json!({"format":GOAL_FORMAT,"type":"goal_run_started","status":"active","reason":"started","conversationId":conv,"objective":objective,"policy":policy}),
        request.now_ms,
    )?;
    create_run(database, tx, &core, &state, request.now_ms)?;
    bounded_response(
        &json!({"created":true,"supersededRunIds":superseded,"run":goal_projection(&core,&state)}),
    )
}
fn goal_transition(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let id = text(p, "run_id", MAX_ORCHESTRATION_RUN_ID_CHARACTERS)?;
    let status = text(p, "status", 64)?;
    let reason = text(p, "reason", 64)?;
    if !valid_goal_transition(status, reason) {
        return Err(invalid_input("invalid GoalRun terminal status/reason pair"));
    }
    let final_text = optional_text(p, "final", MAX_ORCHESTRATION_RUN_STATE_BYTES)?;
    let outcome = Value::Object(object(p, "outcome")?);
    let Some((core, mut state)) = read_run(database, tx, id, tx.owner_user_id(), label)? else {
        return bounded_response(&json!({"transitioned":false,"run":null}));
    };
    if core.created_by != GOAL_CREATED_BY {
        return bounded_response(&json!({"transitioned":false,"run":null}));
    }
    if is_terminal(&state.status) {
        if state.goal_status != status
            || state.goal_reason != reason
            || state.final_text != final_text
            || state.goal_outcome != outcome
        {
            return Err(conflict("GoalRun terminal meaning is immutable"));
        }
        return bounded_response(
            &json!({"transitioned":false,"run":goal_projection(&core,&state)}),
        );
    }
    let old = state.status.clone();
    state.status = goal_storage_status(status).into();
    state.final_text = final_text;
    state.error = if status == "completed" {
        Value::Null
    } else {
        json!({"format":GOAL_FORMAT,"kind":format!("goal_run_{status}"),"reason":reason})
    };
    state.updated_at = request.now_ms;
    state.finished_at = state.finished_at.max(request.now_ms);
    state.goal_status = status.into();
    state.goal_reason = reason.into();
    state.goal_outcome = outcome.clone();
    let sequence = state.next_event_sequence;
    append_event(
        database,
        tx,
        &core,
        &mut state,
        sequence,
        json!({"format":GOAL_FORMAT,"type":"goal_run_transition","status":status,"reason":reason,"outcome":outcome}),
        request.now_ms,
    )?;
    write_run_state(database, tx, &core, &old, &state, request.now_ms)?;
    bounded_response(&json!({"transitioned":true,"run":goal_projection(&core,&state)}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn goal_start_supersession_and_terminal_transition_share_one_authority() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let start = |run_id: &str, objective: &str| Request {
            operation: "goal.run.start".to_owned(),
            payload: json!({
                "run_id":run_id,"conversation_id":"conversation",
                "objective":objective,"user_id":11,"tenant_id":"label",
                "definition":{"nodes":[]},"policy":canonical_goal_policy()
            })
            .as_object()
            .unwrap()
            .clone(),
            now_ms: 100,
        };
        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let response = execute(&database, &mut first, &start("goal-a", "first")).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&response.unwrap()).unwrap()["created"],
            true
        );
        database.commit(first).unwrap();

        let mut second = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let response = execute(&database, &mut second, &start("goal-b", "second")).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&response.unwrap()).unwrap()["supersededRunIds"],
            json!(["goal-a"])
        );
        database.commit(second).unwrap();

        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let transition = Request {
            operation: "goal.run.transition".to_owned(),
            payload: json!({
                "run_id":"goal-b","user_id":11,"tenant_id":"label",
                "status":"completed","reason":"objective_verified",
                "final":"done","outcome":{"verified":true}
            })
            .as_object()
            .unwrap()
            .clone(),
            now_ms: 200,
        };
        let response = execute(&database, &mut terminal, &transition).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&response.unwrap()).unwrap()["run"]["status"],
            "completed"
        );
        database.commit(terminal).unwrap();
    }

    #[test]
    fn run_identity_is_tenant_global_but_public_reads_remain_owner_scoped() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let create = Request {
            operation: "orchestration.run.create".to_owned(),
            payload: json!({
                "run_id":"shared-run","user_id":11,"tenant_id":"label",
                "definition":{"nodes":[]}
            })
            .as_object()
            .unwrap()
            .clone(),
            now_ms: 100,
        };
        let mut owner = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        execute(&database, &mut owner, &create).unwrap();
        database.commit(owner).unwrap();

        let mut other = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        let get = Request {
            operation: "orchestration.run.get".to_owned(),
            payload: json!({"run_id":"shared-run","user_id":12,"tenant_id":"label"})
                .as_object()
                .unwrap()
                .clone(),
            now_ms: 101,
        };
        assert_eq!(
            execute(&database, &mut other, &get).unwrap(),
            Some(b"null".to_vec())
        );
        let mut colliding = create.clone();
        colliding.payload["user_id"] = json!(12);
        assert_eq!(
            execute(&database, &mut other, &colliding)
                .unwrap_err()
                .kind(),
            io::ErrorKind::AlreadyExists
        );
    }

    #[test]
    fn event_page_crosses_the_entity_page_boundary_without_point_scans() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let create = Request {
            operation: "orchestration.run.create".to_owned(),
            payload: json!({
                "run_id":"paged-run","user_id":11,"tenant_id":"label",
                "definition":{"nodes":[]}
            })
            .as_object()
            .unwrap()
            .clone(),
            now_ms: 100,
        };
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        execute(&database, &mut seed, &create).unwrap();
        database.commit(seed).unwrap();

        let mut append = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let (core, mut state) = read_run(&database, &mut append, "paged-run", 11, "label")
            .unwrap()
            .unwrap();
        let old_status = state.status.clone();
        for sequence in 0..1001 {
            append_event(
                &database,
                &mut append,
                &core,
                &mut state,
                sequence,
                json!({"type":"tick","value":sequence}),
                200,
            )
            .unwrap();
        }
        write_run_state(&database, &mut append, &core, &old_status, &state, 200).unwrap();
        database.commit(append).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let page = Request {
            operation: "orchestration.event.page".to_owned(),
            payload: json!({
                "run_id":"paged-run","user_id":11,"tenant_id":"label","cursor":0
            })
            .as_object()
            .unwrap()
            .clone(),
            now_ms: 300,
        };
        let response: Value =
            serde_json::from_slice(&execute(&database, &mut read, &page).unwrap().unwrap())
                .unwrap();
        assert_eq!(response["events"].as_array().unwrap().len(), 1001);
        assert_eq!(response["next_cursor"], 1001);
        assert_eq!(response["caught_up"], true);
    }

    #[test]
    fn startup_retirement_updates_multiple_owners_through_global_covering_state() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for owner in [11, 12] {
            let mut transaction = database.begin_with_identity_claim_scopes(7, owner).unwrap();
            let request = Request {
                operation: "orchestration.run.create".to_owned(),
                payload: json!({
                    "run_id":format!("run-{owner}"),"user_id":owner,
                    "tenant_id":"label","definition":{"nodes":[]}
                })
                .as_object()
                .unwrap()
                .clone(),
                now_ms: 100 + owner,
            };
            execute(&database, &mut transaction, &request).unwrap();
            database.commit(transaction).unwrap();
        }

        let mut maintenance = database.begin_with_identity_claim_scopes(7, 99).unwrap();
        let retire = Request {
            operation: "orchestration.run.retire_interrupted_all".to_owned(),
            payload: json!({"error":{"kind":"restart"}})
                .as_object()
                .unwrap()
                .clone(),
            now_ms: 500,
        };
        let response: Value = serde_json::from_slice(
            &execute(&database, &mut maintenance, &retire)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(response, json!({"retired":2}));
        database.commit(maintenance).unwrap();

        for owner in [11, 12] {
            let mut read = database.begin_with_identity_claim_scopes(7, owner).unwrap();
            let (_core, state) = read_run(
                &database,
                &mut read,
                &format!("run-{owner}"),
                owner,
                "label",
            )
            .unwrap()
            .unwrap();
            assert_eq!(state.status, "error");
            assert_eq!(state.error, json!({"kind":"restart"}));
        }
    }
}
