//! Durable, owner-scoped swarm sessions and resumable agent checkpoints.
//!
//! Large session and agent conversation bodies use versioned blob documents.
//! Compact lifecycle state owns delivery and resumability indexes so result
//! acknowledgement never rewrites message history. A tenant-global exact
//! swarm-key claim prevents cross-owner aliasing; every data read is owner local.

pub(crate) mod migration;

use std::collections::BTreeSet;
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_SWARM_AGENTS_PER_SESSION, MAX_SWARM_AGENT_CORE_BYTES,
    MAX_SWARM_AGENT_IDS_PER_DELIVERY, MAX_SWARM_AGENT_ID_CHARACTERS,
    MAX_SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS, MAX_SWARM_KEY_CHARACTERS,
    MAX_SWARM_OBJECTIVE_CHARACTERS, MAX_SWARM_RESPONSE_BYTES, MAX_SWARM_ROUNDS_USED,
    MAX_SWARM_SESSIONS_PER_OWNER, MAX_SWARM_SESSION_DOCUMENT_BYTES, SWARM_AGENT_CORE_NAMESPACE,
    SWARM_AGENT_COUNT_NAMESPACE, SWARM_AGENT_STATE_NAMESPACE,
    SWARM_CHECKPOINT_MARKER_RESULT_FIELDS, SWARM_RESUMABLE_INDEX_NAMESPACE,
    SWARM_SESSION_COUNT_NAMESPACE, SWARM_SESSION_DOCUMENT_NAMESPACE,
    SWARM_SESSION_KEY_CLAIM_NAMESPACE, SWARM_SESSION_STATE_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const SESSION_LOGICAL_NAMESPACE: &str = "swarm_session";
const AGENT_CORE_LOGICAL_NAMESPACE: &str = "swarm_agent_core";
const COUNT_KEY: &[u8] = b"count";
const SESSION_RUNNING: &str = "running";
const SESSION_TERMINATED: &str = "terminated";
const SESSION_QUARANTINED: &str = "quarantined:ownerless";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}
fn encode<T: Serialize>(value: &T, name: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data(&format!("{name} cannot be encoded")))
}
fn decode<T: for<'de> Deserialize<'de>>(raw: &[u8], name: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| invalid_data(&format!("{name} is malformed")))
}
fn bounded_response(value: &Value) -> io::Result<Option<Vec<u8>>> {
    let encoded = encode(value, "swarm response")?;
    if encoded.len() > MAX_SWARM_RESPONSE_BYTES {
        return Err(crate::resource_exhaustion::domain_bound("swarm response exceeds its aggregate bound"));
    }
    Ok(Some(encoded))
}
fn required_text<'a>(
    payload: &'a Map<String, Value>,
    field: &str,
    maximum: usize,
) -> io::Result<&'a str> {
    payload
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.chars().count() <= maximum)
        .ok_or_else(|| invalid_input("invalid swarm required text field"))
}
fn optional_text(
    payload: &Map<String, Value>,
    field: &str,
    default: &str,
    maximum: usize,
) -> io::Result<String> {
    match payload.get(field) {
        None => Ok(default.to_owned()),
        Some(value) => value
            .as_str()
            .filter(|text| text.chars().count() <= maximum)
            .map(ToOwned::to_owned)
            .ok_or_else(|| invalid_input("invalid swarm optional text field")),
    }
}
fn required_u64(payload: &Map<String, Value>, field: &str) -> io::Result<u64> {
    payload
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_input("invalid swarm integer field"))
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
            .map_err(|_| invalid_input("swarm key text is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}
fn session_document_key(tx: &AuthorityTransaction, swarm_key: &str) -> io::Result<EntityKey> {
    owner_key(tx, SWARM_SESSION_DOCUMENT_NAMESPACE, swarm_key.as_bytes())
}
fn session_state_key(tx: &AuthorityTransaction, swarm_key: &str) -> io::Result<EntityKey> {
    owner_key(tx, SWARM_SESSION_STATE_NAMESPACE, swarm_key.as_bytes())
}
fn session_claim_key(tx: &AuthorityTransaction, swarm_key: &str) -> io::Result<EntityKey> {
    global_key(tx, SWARM_SESSION_KEY_CLAIM_NAMESPACE, swarm_key.as_bytes())
}
fn session_count_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(tx, SWARM_SESSION_COUNT_NAMESPACE, COUNT_KEY)
}
fn agent_prefix(swarm_key: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(swarm_key.len() + 2);
    push_text(&mut raw, swarm_key)?;
    Ok(raw)
}
fn agent_key(
    tx: &AuthorityTransaction,
    namespace: &str,
    swarm_key: &str,
    agent_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = agent_prefix(swarm_key)?;
    raw.extend_from_slice(agent_id.as_bytes());
    owner_key(tx, namespace, &raw)
}
fn agent_range(
    tx: &AuthorityTransaction,
    namespace: &str,
    swarm_key: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        namespace,
        &agent_prefix(swarm_key)?,
    )
}
fn agent_count_key(tx: &AuthorityTransaction, swarm_key: &str) -> io::Result<EntityKey> {
    owner_key(tx, SWARM_AGENT_COUNT_NAMESPACE, swarm_key.as_bytes())
}
fn resumable_index_key(
    tx: &AuthorityTransaction,
    updated_at: u64,
    swarm_key: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(8 + swarm_key.len());
    raw.extend_from_slice(&(!updated_at).to_be_bytes());
    raw.extend_from_slice(swarm_key.as_bytes());
    owner_key(tx, SWARM_RESUMABLE_INDEX_NAMESPACE, &raw)
}
fn resumable_range(tx: &AuthorityTransaction) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        SWARM_RESUMABLE_INDEX_NAMESPACE,
        b"",
    )
}
fn read_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
) -> io::Result<u64> {
    database
        .entity_get(tx, key)?
        .map(|raw| {
            raw.try_into()
                .map(u64::from_be_bytes)
                .map_err(|_| invalid_data("swarm count is malformed"))
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
    if value == 0 {
        database.entity_delete(tx, key)
    } else {
        database.entity_put(tx, key, value.to_be_bytes().to_vec())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct SwarmClaim {
    swarm_key: String,
    owner_user_id: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct SessionDocument {
    swarm_key: String,
    conv_id: String,
    task_id: String,
    specs: Value,
    config: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct SessionState {
    status: String,
    created_at: u64,
    updated_at: u64,
    resumable_agents: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct AgentCore {
    swarm_key: String,
    agent_id: String,
    role: String,
    objective: String,
    messages: Value,
    result: Value,
    /// Caller id of the save or append that wrote this core: the base a later
    /// `swarm.agent.append` must name. Empty for writers without one.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    checkpoint_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct AgentState {
    status: String,
    rounds_used: u64,
    delivered: bool,
    updated_at: u64,
    /// Compact result fields written by `swarm.agent.checkpoint.mark` after the
    /// last full save; reads overlay them on the core result and the next
    /// `swarm.agent.save` clears them.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    checkpoint: Option<Map<String, Value>>,
}

#[derive(Clone, Debug)]
pub struct Request {
    pub operation: String,
    pub payload: Map<String, Value>,
    pub now_ms: u64,
}

impl Request {
    pub(crate) fn validate(&self) -> io::Result<usize> {
        if !self.operation.starts_with("swarm.") || self.operation.len() > 64 {
            return Err(invalid_input("invalid swarm operation"));
        }
        let bytes = encode(&self.payload, "swarm request")?.len();
        if bytes > MAX_SWARM_RESPONSE_BYTES || self.now_ms == 0 {
            return Err(invalid_input("swarm request exceeds its bound"));
        }
        Ok(bytes)
    }

    pub(crate) fn mutates_state(&self) -> bool {
        !matches!(
            self.operation.as_str(),
            "swarm.session.get"
                | "swarm.agents.page"
                | "swarm.agent.state.get"
                | "swarm.resumable.list"
        )
    }
}

fn read_session_document(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<Option<SessionDocument>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &session_document_key(tx, swarm_key)?,
        SESSION_LOGICAL_NAMESPACE,
        swarm_key,
        tx.owner_user_id(),
        MAX_SWARM_SESSION_DOCUMENT_BYTES,
    )?
    .map(|raw| decode(&raw, "swarm session document"))
    .transpose()
}
fn read_session_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<Option<SessionState>> {
    database
        .entity_get(tx, &session_state_key(tx, swarm_key)?)?
        .map(|raw| decode(&raw, "swarm session state"))
        .transpose()
}
fn read_agent_core(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    agent_id: &str,
) -> io::Result<Option<AgentCore>> {
    let logical_key = format!("{swarm_key}:{agent_id}");
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &agent_key(tx, SWARM_AGENT_CORE_NAMESPACE, swarm_key, agent_id)?,
        AGENT_CORE_LOGICAL_NAMESPACE,
        &logical_key,
        tx.owner_user_id(),
        MAX_SWARM_AGENT_CORE_BYTES,
    )?
    .map(|raw| decode(&raw, "swarm agent core"))
    .transpose()
}
fn read_agent_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    agent_id: &str,
) -> io::Result<Option<AgentState>> {
    database
        .entity_get(
            tx,
            &agent_key(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key, agent_id)?,
        )?
        .map(|raw| decode(&raw, "swarm agent state"))
        .transpose()
}
fn put_document<T: Serialize>(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: PutRequest,
    value: &T,
    maximum_bytes: usize,
) -> io::Result<()> {
    versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        PutRequest {
            value_json: encode(value, "swarm document")?,
            ..request
        },
        tx.owner_user_id(),
        maximum_bytes,
    )?;
    Ok(())
}
fn ensure_claim(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<()> {
    let key = session_claim_key(tx, swarm_key)?;
    if let Some(raw) = database.entity_get(tx, &key)? {
        let claim: SwarmClaim = decode(&raw, "swarm session claim")?;
        if claim.swarm_key != swarm_key || claim.owner_user_id != tx.owner_user_id() {
            return Err(conflict("swarm key belongs to another owner"));
        }
        return Ok(());
    }
    database.entity_put(
        tx,
        key,
        encode(
            &SwarmClaim {
                swarm_key: swarm_key.to_owned(),
                owner_user_id: tx.owner_user_id(),
            },
            "swarm session claim",
        )?,
    )
}
fn claim_is_owned(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<bool> {
    let Some(raw) = database.entity_get(tx, &session_claim_key(tx, swarm_key)?)? else {
        return Ok(false);
    };
    let claim: SwarmClaim = decode(&raw, "swarm session claim")?;
    if claim.swarm_key != swarm_key {
        return Err(invalid_data("swarm session claim identity is inconsistent"));
    }
    Ok(claim.owner_user_id == tx.owner_user_id())
}
fn session_is_resumable(state: &SessionState) -> bool {
    matches!(state.status.as_str(), SESSION_RUNNING | SESSION_TERMINATED)
        && state.resumable_agents > 0
}
fn agent_is_resumable(state: &AgentState) -> bool {
    matches!(state.status.as_str(), "pending" | "running" | "retrying")
        || (state.status == "completed" && !state.delivered)
}
fn replace_resumable_index(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    old: Option<&SessionState>,
    new: Option<&SessionState>,
) -> io::Result<()> {
    if let Some(state) = old.filter(|state| session_is_resumable(state)) {
        database.entity_delete(tx, resumable_index_key(tx, state.updated_at, swarm_key)?)?;
    }
    if let Some(state) = new.filter(|state| session_is_resumable(state)) {
        database.entity_put(
            tx,
            resumable_index_key(tx, state.updated_at, swarm_key)?,
            swarm_key.as_bytes().to_vec(),
        )?;
    }
    Ok(())
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
        if page.is_empty() {
            break;
        }
        let page_len = page.len();
        let next = page
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?;
        rows.extend(page);
        if page_len < page_limit || rows.len() == limit {
            break;
        }
        start = next
            .ok_or_else(|| invalid_data("swarm pagination lost continuation"))?
            .1;
    }
    Ok(rows)
}
fn decode_agent_id(key: &EntityKey, swarm_key: &str) -> io::Result<String> {
    let raw = key.key_bytes();
    let prefix_len = 2 + swarm_key.len();
    if raw.len() <= prefix_len {
        return Err(invalid_data("swarm agent key is malformed"));
    }
    std::str::from_utf8(&raw[prefix_len..])
        .map(ToOwned::to_owned)
        .map_err(|_| invalid_data("swarm agent id is malformed"))
}
fn project_agent(core: &AgentCore, state: &AgentState) -> Value {
    let mut result = core.result.clone();
    if let (Some(marker), Some(fields)) = (&state.checkpoint, result.as_object_mut()) {
        fields.extend(marker.iter().map(|(name, value)| (name.clone(), value.clone())));
    }
    json!({
        "agent_id":core.agent_id,
        "role":core.role,
        "objective":core.objective,
        "status":state.status,
        "messages":core.messages,
        "result":result,
        "rounds_used":state.rounds_used,
        "delivered":state.delivered,
        "updated_at":state.updated_at,
    })
}
fn read_agent(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    state_key: &EntityKey,
    raw_state: &[u8],
) -> io::Result<(String, Value)> {
    let agent_id = decode_agent_id(state_key, swarm_key)?;
    let state: AgentState = decode(raw_state, "swarm agent state")?;
    let core = read_agent_core(database, tx, swarm_key, &agent_id)?
        .ok_or_else(|| invalid_data("swarm agent state has no core"))?;
    if core.swarm_key != swarm_key || core.agent_id != agent_id {
        return Err(invalid_data("swarm agent identity is inconsistent"));
    }
    Ok((agent_id, project_agent(&core, &state)))
}
fn load_agents(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<Vec<Value>> {
    let (start, end) = agent_range(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key)?;
    let states = scan_paged(database, tx, start, &end, MAX_SWARM_AGENTS_PER_SESSION + 1)?;
    if states.len() > MAX_SWARM_AGENTS_PER_SESSION {
        return Err(crate::resource_exhaustion::domain_bound("swarm agent count exceeds its bound"));
    }
    let mut agents = Vec::with_capacity(states.len());
    for (key, raw) in states {
        agents.push(read_agent(database, tx, swarm_key, &key, &raw)?.1);
    }
    Ok(agents)
}
fn project_session(
    document: &SessionDocument,
    state: &SessionState,
    agents: Option<Vec<Value>>,
) -> Value {
    let mut value = json!({
        "swarm_key":document.swarm_key,
        "conv_id":document.conv_id,
        "task_id":document.task_id,
        "status":state.status,
        "specs":document.specs,
        "config":document.config,
        "created_at":state.created_at,
        "updated_at":state.updated_at,
    });
    if let Some(agents) = agents {
        value["agents"] = Value::Array(agents);
    }
    value
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request.operation.as_str() {
        "swarm.session.save" => session_save(database, tx, request),
        "swarm.session.terminate" => session_terminate(database, tx, request),
        "swarm.session.quarantine_ownerless" => session_quarantine(database, tx, request),
        "swarm.session.delete" => session_delete(database, tx, request),
        "swarm.agent.save" => agent_save(database, tx, request),
        "swarm.agent.append" => agent_append(database, tx, request),
        "swarm.agent.checkpoint.mark" => agent_checkpoint_mark(database, tx, request),
        "swarm.agents.mark_delivered" => mark_delivered(database, tx, request, false),
        "swarm.agents.acknowledge_results" => mark_delivered(database, tx, request, true),
        "swarm.session.get" => session_get(database, tx, request),
        "swarm.agents.page" => agents_page(database, tx, request),
        "swarm.agent.state.get" => agent_state_get(database, tx, request),
        "swarm.resumable.list" => resumable_list(database, tx),
        _ => Err(invalid_input("unknown swarm operation")),
    }
}

fn session_save(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let swarm_key = required_text(p, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let specs = p
        .get("specs")
        .filter(|value| value.is_array())
        .cloned()
        .ok_or_else(|| invalid_input("invalid swarm specs"))?;
    let config = p
        .get("config")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| invalid_input("invalid swarm config"))?;
    let now = required_u64(p, "now_ms")?;
    let status = optional_text(p, "status", SESSION_RUNNING, 64)?;
    if !matches!(status.as_str(), SESSION_RUNNING | SESSION_TERMINATED) {
        return Err(invalid_input("invalid swarm session status"));
    }
    let conv_id = optional_text(p, "conv_id", "", 4096)?;
    let task_id = optional_text(p, "task_id", "", 4096)?;
    ensure_claim(database, tx, swarm_key)?;
    let old_document = read_session_document(database, tx, swarm_key)?;
    let old_state = read_session_state(database, tx, swarm_key)?;
    if old_document.is_some() != old_state.is_some() {
        return Err(invalid_data("swarm session document/state split"));
    }
    let state = if let Some(old) = &old_state {
        SessionState {
            status,
            created_at: old.created_at,
            updated_at: now,
            resumable_agents: old.resumable_agents,
        }
    } else {
        let count_key = session_count_key(tx)?;
        let count = read_count(database, tx, &count_key)?;
        if count >= MAX_SWARM_SESSIONS_PER_OWNER as u64 {
            return Err(crate::resource_exhaustion::capacity_quota("swarm session capacity reached"));
        }
        write_count(database, tx, count_key, count + 1)?;
        SessionState {
            status,
            created_at: now,
            updated_at: now,
            resumable_agents: read_existing_resumable_agent_count(database, tx, swarm_key)?,
        }
    };
    let document = SessionDocument {
        swarm_key: swarm_key.to_owned(),
        conv_id,
        task_id,
        specs,
        config,
    };
    put_document(
        database,
        tx,
        PutRequest {
            key: session_document_key(tx, swarm_key)?,
            namespace: SESSION_LOGICAL_NAMESPACE.to_owned(),
            logical_key: swarm_key.to_owned(),
            value_json: Vec::new(),
            expected_version: None,
            updated_at_ms: request.now_ms.max(1),
        },
        &document,
        MAX_SWARM_SESSION_DOCUMENT_BYTES,
    )?;
    replace_resumable_index(database, tx, swarm_key, old_state.as_ref(), Some(&state))?;
    database.entity_put(
        tx,
        session_state_key(tx, swarm_key)?,
        encode(&state, "swarm session state")?,
    )?;
    bounded_response(&json!({"saved":true}))
}

fn read_existing_resumable_agent_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<u64> {
    let (start, end) = agent_range(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key)?;
    let rows = scan_paged(database, tx, start, &end, MAX_SWARM_AGENTS_PER_SESSION + 1)?;
    if rows.len() > MAX_SWARM_AGENTS_PER_SESSION {
        return Err(crate::resource_exhaustion::domain_bound("swarm agent count exceeds its bound"));
    }
    let mut count = 0_u64;
    for (_, raw) in rows {
        let state: AgentState = decode(&raw, "swarm agent state")?;
        count += u64::from(agent_is_resumable(&state));
    }
    Ok(count)
}

fn update_session_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    transform: impl FnOnce(&mut SessionState) -> io::Result<()>,
) -> io::Result<bool> {
    let Some(old) = read_session_state(database, tx, swarm_key)? else {
        return Ok(false);
    };
    let mut new = old.clone();
    transform(&mut new)?;
    replace_resumable_index(database, tx, swarm_key, Some(&old), Some(&new))?;
    database.entity_put(
        tx,
        session_state_key(tx, swarm_key)?,
        encode(&new, "swarm session state")?,
    )?;
    Ok(true)
}

fn session_terminate(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let swarm_key = required_text(&request.payload, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let now = required_u64(&request.payload, "now_ms")?;
    let mut changed = false;
    update_session_state(database, tx, swarm_key, |state| {
        if state.status == SESSION_RUNNING {
            state.status = SESSION_TERMINATED.to_owned();
            state.updated_at = now;
            changed = true;
        }
        Ok(())
    })?;
    bounded_response(&json!({"changed":changed}))
}

fn session_quarantine(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let swarm_key = required_text(&request.payload, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let now = required_u64(&request.payload, "now_ms")?;
    let Some(document) = read_session_document(database, tx, swarm_key)? else {
        return bounded_response(&json!({"changed":false}));
    };
    let has_owner = document
        .config
        .as_object()
        .and_then(|config| config.get("user_id"))
        .and_then(Value::as_u64)
        .is_some_and(|owner| owner > 0);
    if has_owner {
        return bounded_response(&json!({"changed":false}));
    }
    let mut changed = false;
    update_session_state(database, tx, swarm_key, |state| {
        if state.status != SESSION_QUARANTINED {
            state.status = SESSION_QUARANTINED.to_owned();
            state.updated_at = now;
            changed = true;
        }
        Ok(())
    })?;
    bounded_response(&json!({"changed":changed}))
}

fn session_delete(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let swarm_key = required_text(&request.payload, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let document = read_session_document(database, tx, swarm_key)?;
    let state = read_session_state(database, tx, swarm_key)?;
    if document.is_some() != state.is_some() {
        return Err(invalid_data("swarm session document/state split"));
    }
    let agents = load_agent_state_rows(database, tx, swarm_key)?;
    if !claim_is_owned(database, tx, swarm_key)? {
        if document.is_some() || !agents.is_empty() {
            return Err(invalid_data(
                "swarm owner data has no matching identity claim",
            ));
        }
        return bounded_response(&json!({"deleted":false}));
    }
    for (agent_id, key) in agents {
        versioned_document::delete(
            database,
            tx,
            agent_key(tx, SWARM_AGENT_CORE_NAMESPACE, swarm_key, &agent_id)?,
            AGENT_CORE_LOGICAL_NAMESPACE,
            &format!("{swarm_key}:{agent_id}"),
            None,
        )?;
        database.entity_delete(tx, key)?;
    }
    database.entity_delete(tx, agent_count_key(tx, swarm_key)?)?;
    if let Some(current) = &state {
        replace_resumable_index(database, tx, swarm_key, Some(current), None)?;
        versioned_document::delete(
            database,
            tx,
            session_document_key(tx, swarm_key)?,
            SESSION_LOGICAL_NAMESPACE,
            swarm_key,
            None,
        )?;
        database.entity_delete(tx, session_state_key(tx, swarm_key)?)?;
        let count_key = session_count_key(tx)?;
        let count = read_count(database, tx, &count_key)?;
        if count == 0 {
            return Err(invalid_data("swarm session count underflow"));
        }
        write_count(database, tx, count_key, count - 1)?;
    }
    database.entity_delete(tx, session_claim_key(tx, swarm_key)?)?;
    bounded_response(&json!({"deleted":document.is_some()}))
}

fn load_agent_state_rows(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<Vec<(String, EntityKey)>> {
    let (start, end) = agent_range(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key)?;
    let rows = scan_paged(database, tx, start, &end, MAX_SWARM_AGENTS_PER_SESSION + 1)?;
    if rows.len() > MAX_SWARM_AGENTS_PER_SESSION {
        return Err(crate::resource_exhaustion::domain_bound("swarm agent count exceeds its bound"));
    }
    rows.into_iter()
        .map(|(key, _)| Ok((decode_agent_id(&key, swarm_key)?, key)))
        .collect()
}

fn agent_save(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let swarm_key = required_text(p, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let agent_id = required_text(p, "agent_id", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    let messages = p
        .get("messages")
        .filter(|value| value.is_array())
        .cloned()
        .ok_or_else(|| invalid_input("invalid swarm messages"))?;
    let result = p
        .get("result")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| invalid_input("invalid swarm result"))?;
    let rounds_used = p.get("rounds_used").and_then(Value::as_u64).unwrap_or(0);
    if rounds_used > MAX_SWARM_ROUNDS_USED {
        return Err(invalid_input("invalid swarm rounds_used"));
    }
    let now = required_u64(p, "now_ms")?;
    let delivered = match p.get("delivered") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(value)) => Some(*value),
        _ => return Err(invalid_input("invalid swarm delivered flag")),
    };
    let role = optional_text(p, "role", "", 4096)?;
    let objective = optional_text(p, "objective", "", MAX_SWARM_OBJECTIVE_CHARACTERS)?;
    let status = optional_text(p, "status", "pending", 64)?;
    let checkpoint_id = optional_text(p, "checkpoint_id", "", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    ensure_claim(database, tx, swarm_key)?;
    let old_state = read_agent_state(database, tx, swarm_key, agent_id)?;
    let old_core = read_agent_core(database, tx, swarm_key, agent_id)?;
    if old_state.is_some() != old_core.is_some() {
        return Err(invalid_data("swarm agent core/state split"));
    }
    if old_state.is_none() {
        let count_key = agent_count_key(tx, swarm_key)?;
        let count = read_count(database, tx, &count_key)?;
        if count >= MAX_SWARM_AGENTS_PER_SESSION as u64 {
            return Err(crate::resource_exhaustion::capacity_quota("swarm agent capacity reached"));
        }
        write_count(database, tx, count_key, count + 1)?;
    }
    let state = AgentState {
        status,
        rounds_used,
        delivered: delivered
            .unwrap_or_else(|| old_state.as_ref().is_some_and(|state| state.delivered)),
        updated_at: now,
        checkpoint: None,
    };
    let core = AgentCore {
        swarm_key: swarm_key.to_owned(),
        agent_id: agent_id.to_owned(),
        role,
        objective,
        messages,
        result,
        checkpoint_id,
    };
    write_agent(database, tx, request, &core, old_state.as_ref(), &state)?;
    // Echoing the id acknowledges it as the base of a later swarm.agent.append.
    if core.checkpoint_id.is_empty() {
        bounded_response(&json!({"saved":true}))
    } else {
        bounded_response(&json!({"saved":true, "checkpoint_id":core.checkpoint_id}))
    }
}

/// Writes one agent's core and compact state and keeps the session's
/// resumable count exact across the state transition.
fn write_agent(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    core: &AgentCore,
    old_state: Option<&AgentState>,
    state: &AgentState,
) -> io::Result<()> {
    let (swarm_key, agent_id) = (core.swarm_key.as_str(), core.agent_id.as_str());
    put_document(
        database,
        tx,
        PutRequest {
            key: agent_key(tx, SWARM_AGENT_CORE_NAMESPACE, swarm_key, agent_id)?,
            namespace: AGENT_CORE_LOGICAL_NAMESPACE.to_owned(),
            logical_key: format!("{swarm_key}:{agent_id}"),
            value_json: Vec::new(),
            expected_version: None,
            updated_at_ms: request.now_ms.max(1),
        },
        core,
        MAX_SWARM_AGENT_CORE_BYTES,
    )?;
    database.entity_put(
        tx,
        agent_key(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key, agent_id)?,
        encode(state, "swarm agent state")?,
    )?;
    let old_resumable = old_state.is_some_and(agent_is_resumable);
    let new_resumable = agent_is_resumable(state);
    if old_resumable != new_resumable {
        update_session_state(database, tx, swarm_key, |session| {
            if new_resumable {
                session.resumable_agents = session
                    .resumable_agents
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("swarm resumable count overflow"))?;
            } else {
                session.resumable_agents = session
                    .resumable_agents
                    .checked_sub(1)
                    .ok_or_else(|| invalid_data("swarm resumable count underflow"))?;
            }
            Ok(())
        })?;
    }
    Ok(())
}

/// Appends new messages to the exact checkpoint the caller last wrote, named
/// by its checkpoint id and message count. A missing agent, any other write
/// since, or a different count changes nothing and answers `appended: false`
/// (the caller then saves in full). Success leaves the agent exactly as a full
/// save of the extended transcript, retiring the compact marker; delivery,
/// role and objective are unchanged.
fn agent_append(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let swarm_key = required_text(p, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let agent_id = required_text(p, "agent_id", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    let base_checkpoint_id = required_text(p, "base_checkpoint_id", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    let checkpoint_id = required_text(p, "checkpoint_id", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    let base_message_count = required_u64(p, "base_message_count")?;
    let appended = p
        .get("messages")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_input("invalid swarm messages"))?;
    let result = p
        .get("result")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| invalid_input("invalid swarm result"))?;
    let rounds_used = p.get("rounds_used").and_then(Value::as_u64).unwrap_or(0);
    if rounds_used > MAX_SWARM_ROUNDS_USED {
        return Err(invalid_input("invalid swarm rounds_used"));
    }
    let now = required_u64(p, "now_ms")?;
    let status = optional_text(p, "status", "pending", 64)?;
    let Some(old_state) = read_agent_state(database, tx, swarm_key, agent_id)? else {
        return bounded_response(&json!({"appended":false}));
    };
    let mut core = read_agent_core(database, tx, swarm_key, agent_id)?
        .ok_or_else(|| invalid_data("swarm agent core/state split"))?;
    if core.swarm_key != swarm_key || core.agent_id != agent_id {
        return Err(invalid_data("swarm agent identity is inconsistent"));
    }
    let messages = core
        .messages
        .as_array_mut()
        .ok_or_else(|| invalid_data("swarm agent messages are malformed"))?;
    if core.checkpoint_id != base_checkpoint_id
        || u64::try_from(messages.len()).ok() != Some(base_message_count)
    {
        return bounded_response(&json!({"appended":false}));
    }
    messages.extend(appended.iter().cloned());
    core.result = result;
    checkpoint_id.clone_into(&mut core.checkpoint_id);
    let state = AgentState {
        status,
        rounds_used,
        delivered: old_state.delivered,
        updated_at: now,
        checkpoint: None,
    };
    write_agent(database, tx, request, &core, Some(&old_state), &state)?;
    bounded_response(&json!({"appended":true}))
}

fn checkpoint_marker(payload: &Map<String, Value>) -> io::Result<Map<String, Value>> {
    let marker = payload
        .get("result")
        .and_then(Value::as_object)
        .filter(|marker| !marker.is_empty())
        .ok_or_else(|| invalid_input("invalid swarm checkpoint marker"))?;
    for (field, value) in marker {
        let scalar = match value {
            Value::Null | Value::Bool(_) | Value::Number(_) => true,
            Value::String(text) => {
                text.chars().count() <= MAX_SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS
            }
            Value::Array(_) | Value::Object(_) => false,
        };
        if !scalar || !SWARM_CHECKPOINT_MARKER_RESULT_FIELDS.contains(&field.as_str()) {
            return Err(invalid_input("invalid swarm checkpoint marker"));
        }
    }
    Ok(marker.clone())
}

/// Overlay compact checkpoint fields on an existing agent's last full save.
/// Only the compact state is read and rewritten, so the cost is independent
/// of the retained transcript; without a full checkpoint nothing is marked.
fn agent_checkpoint_mark(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let swarm_key = required_text(p, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let agent_id = required_text(p, "agent_id", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    let marker = checkpoint_marker(p)?;
    let rounds_used = required_u64(p, "rounds_used")?;
    if rounds_used > MAX_SWARM_ROUNDS_USED {
        return Err(invalid_input("invalid swarm rounds_used"));
    }
    let now = required_u64(p, "now_ms")?;
    let Some(mut state) = read_agent_state(database, tx, swarm_key, agent_id)? else {
        return bounded_response(&json!({"marked":false}));
    };
    state.rounds_used = rounds_used;
    state.updated_at = now;
    state.checkpoint.get_or_insert_with(Map::new).extend(marker);
    database.entity_put(
        tx,
        agent_key(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key, agent_id)?,
        encode(&state, "swarm agent state")?,
    )?;
    bounded_response(&json!({"marked":true}))
}

fn mark_delivered(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    require_session: bool,
) -> io::Result<Option<Vec<u8>>> {
    let p = &request.payload;
    let swarm_key = required_text(p, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let ids = p
        .get("agent_ids")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_input("invalid swarm agent_ids"))?;
    if ids.len() > MAX_SWARM_AGENT_IDS_PER_DELIVERY {
        return Err(invalid_input("invalid swarm agent_ids"));
    }
    let mut unique = BTreeSet::new();
    for value in ids {
        let id = value
            .as_str()
            .filter(|id| !id.is_empty() && id.chars().count() <= MAX_SWARM_AGENT_ID_CHARACTERS)
            .ok_or_else(|| invalid_input("invalid swarm agent_ids"))?;
        unique.insert(id.to_owned());
    }
    if require_session {
        let task_id = required_text(p, "task_id", 4096)?;
        let session = read_session_document(database, tx, swarm_key)?;
        if session.as_ref().is_none_or(|document| document.task_id != task_id) {
            return bounded_response(&json!({"acknowledged":false, "changed":0}));
        }
    }
    let mut changed = 0_u64;
    let mut resumable_delta = 0_u64;
    for agent_id in unique {
        let Some(mut state) = read_agent_state(database, tx, swarm_key, &agent_id)? else {
            continue;
        };
        changed += 1;
        if state.status == "completed" && !state.delivered {
            resumable_delta += 1;
        }
        state.delivered = true;
        database.entity_put(
            tx,
            agent_key(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key, &agent_id)?,
            encode(&state, "swarm agent state")?,
        )?;
    }
    if resumable_delta > 0 {
        update_session_state(database, tx, swarm_key, |session| {
            session.resumable_agents = session
                .resumable_agents
                .checked_sub(resumable_delta)
                .ok_or_else(|| invalid_data("swarm resumable count underflow"))?;
            Ok(())
        })?;
    }
    if require_session {
        bounded_response(&json!({"acknowledged":true, "changed":changed}))
    } else {
        bounded_response(&json!({"changed":changed}))
    }
}

fn session_get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let swarm_key = required_text(&request.payload, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let Some(document) = read_session_document(database, tx, swarm_key)? else {
        return bounded_response(&Value::Null);
    };
    let state = read_session_state(database, tx, swarm_key)?
        .ok_or_else(|| invalid_data("swarm session document has no state"))?;
    if document.swarm_key != swarm_key {
        return Err(invalid_data("swarm session identity is inconsistent"));
    }
    let include_agents = match request.payload.get("include_agents") {
        None => true,
        Some(Value::Bool(include)) => *include,
        Some(_) => return Err(invalid_input("invalid swarm include_agents flag")),
    };
    let agents = if include_agents {
        Some(load_agents(database, tx, swarm_key)?)
    } else {
        None
    };
    bounded_response(&project_session(&document, &state, agents))
}

/// Session header without specs/config plus one agent's compact state.
/// Exact-run message consumers fence on this every model round, so it never
/// reads an agent core: its cost is independent of retained checkpoint history.
fn agent_state_get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let swarm_key = required_text(&request.payload, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let agent_id = required_text(&request.payload, "agent_id", MAX_SWARM_AGENT_ID_CHARACTERS)?;
    let Some(document) = read_session_document(database, tx, swarm_key)? else {
        return bounded_response(&Value::Null);
    };
    let state = read_session_state(database, tx, swarm_key)?
        .ok_or_else(|| invalid_data("swarm session document has no state"))?;
    if document.swarm_key != swarm_key {
        return Err(invalid_data("swarm session identity is inconsistent"));
    }
    let agent = read_agent_state(database, tx, swarm_key, agent_id)?.map(|agent| {
        json!({
            "agent_id":agent_id,
            "status":agent.status,
            "rounds_used":agent.rounds_used,
            "delivered":agent.delivered,
            "updated_at":agent.updated_at,
        })
    });
    bounded_response(&json!({
        "swarm_key":document.swarm_key,
        "conv_id":document.conv_id,
        "task_id":document.task_id,
        "status":state.status,
        "created_at":state.created_at,
        "updated_at":state.updated_at,
        "agent":agent,
    }))
}

/// One agent core is bounded below the response ceiling with slack for its
/// compact state fields; this reserve covers the page envelope and its
/// escaped continuation id, so every page can carry at least one agent.
const AGENT_PAGE_ENVELOPE_RESERVE_BYTES: usize = 4096;

/// Agents of one session in key order, packed into a response below the
/// stream ceiling. A session larger than one response is read completely by
/// following `next_after_agent_id`; `swarm.session.get` keeps refusing it.
fn agents_page(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let swarm_key = required_text(&request.payload, "swarm_key", MAX_SWARM_KEY_CHARACTERS)?;
    let after = optional_text(
        &request.payload,
        "after_agent_id",
        "",
        MAX_SWARM_AGENT_ID_CHARACTERS,
    )?;
    let (mut start, end) = agent_range(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key)?;
    if !after.is_empty() {
        start = agent_key(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key, &after)?
            .exact_range()?
            .1;
    }
    let budget = MAX_SWARM_RESPONSE_BYTES - AGENT_PAGE_ENVELOPE_RESERVE_BYTES;
    let mut agents = Vec::new();
    let mut page_bytes = 0_usize;
    let mut last_agent_id = String::new();
    let mut next_after_agent_id = Value::Null;
    'scan: loop {
        let rows = database.entity_scan(tx, &start, &end, MAX_ENTITY_RANGE_ROWS)?;
        let complete = rows.len() < MAX_ENTITY_RANGE_ROWS;
        let Some(continuation) = rows
            .last()
            .map(|(key, _)| key.clone().exact_range())
            .transpose()?
        else {
            break;
        };
        for (key, raw) in rows {
            let (agent_id, agent) = read_agent(database, tx, swarm_key, &key, &raw)?;
            let agent_bytes = encode(&agent, "swarm agent")?.len() + 1;
            if !agents.is_empty() && page_bytes + agent_bytes > budget {
                next_after_agent_id = Value::String(last_agent_id);
                break 'scan;
            }
            page_bytes += agent_bytes;
            last_agent_id = agent_id;
            agents.push(agent);
        }
        if complete {
            break;
        }
        start = continuation.1;
    }
    bounded_response(&json!({
        "agents":agents,
        "next_after_agent_id":next_after_agent_id,
    }))
}

/// Keys of resumable sessions, newest first. Checkpoint bodies are never
/// materialized here: a caller reads each session separately, so retained
/// history can never grow this response past the stream ceiling.
fn resumable_list(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<Option<Vec<u8>>> {
    let (start, end) = resumable_range(tx)?;
    let rows = scan_paged(database, tx, start, &end, MAX_SWARM_SESSIONS_PER_OWNER + 1)?;
    if rows.len() > MAX_SWARM_SESSIONS_PER_OWNER {
        return Err(crate::resource_exhaustion::domain_bound("swarm resumable session count exceeds its bound"));
    }
    let mut sessions = Vec::with_capacity(rows.len());
    for (index_key, raw) in rows {
        let swarm_key = std::str::from_utf8(&raw)
            .map_err(|_| invalid_data("swarm resumable index is malformed"))?;
        if database
            .entity_get(tx, &session_document_key(tx, swarm_key)?)?
            .is_none()
        {
            return Err(invalid_data("swarm resumable index has no session"));
        }
        let state = read_session_state(database, tx, swarm_key)?
            .ok_or_else(|| invalid_data("swarm resumable index has no state"))?;
        if !session_is_resumable(&state)
            || resumable_index_key(tx, state.updated_at, swarm_key)? != index_key
        {
            return Err(invalid_data("swarm resumable index is inconsistent"));
        }
        if read_existing_resumable_agent_count(database, tx, swarm_key)? == 0 {
            return Err(invalid_data("swarm resumable count has no eligible agent"));
        }
        sessions.push(json!({"swarm_key":swarm_key, "updated_at":state.updated_at}));
    }
    bounded_response(&Value::Array(sessions))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acknowledgement_is_fenced_by_session_identity_across_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let current_task = "n".repeat(600);
        for task in ["old", current_task.as_str()] {
            commit_request(&mut database, 11, &request("swarm.session.save", json!({
                "swarm_key":"fenced", "task_id":task, "specs":[],
                "config":{"user_id":11}, "now_ms":100
            }), 100)).unwrap();
        }
        commit_request(&mut database, 11, &request("swarm.agent.save", json!({
            "swarm_key":"fenced", "agent_id":"worker", "status":"completed",
            "messages":[], "result":{"final_answer":"new evidence"},
            "delivered":false, "now_ms":200
        }), 200)).unwrap();
        let acknowledge = |task| request("swarm.agents.acknowledge_results", json!({
            "swarm_key":"fenced", "task_id":task, "agent_ids":["worker", "worker"]
        }), 300);
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(commit_request(&mut database, 11, &acknowledge("old")).unwrap(),
                   json!({"acknowledged":false, "changed":0}));
        assert_eq!(commit_request(&mut database, 12, &acknowledge(current_task.as_str())).unwrap(),
                   json!({"acknowledged":false, "changed":0}));
        let get = request("swarm.session.get", json!({"swarm_key":"fenced"}), 400);
        assert_eq!(commit_request(&mut database, 11, &get).unwrap()["agents"][0]["delivered"], false);
        for _ in 0..2 {
            assert_eq!(commit_request(&mut database, 11, &acknowledge(current_task.as_str())).unwrap(),
                       json!({"acknowledged":true, "changed":1}));
        }
        assert_eq!(commit_request(&mut database, 11, &get).unwrap()["agents"][0]["delivered"], true);
    }

    #[test]
    fn large_agent_checkpoint_survives_delivery_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        commit_request(&mut database, 11, &request("swarm.session.save", json!({
            "swarm_key":"large", "specs":[], "config":{"user_id":11}, "now_ms":100
        }), 100)).unwrap();
        let content = "x".repeat(9 * 1024 * 1024);
        let checkpoint = request("swarm.agent.save", json!({
            "swarm_key":"large", "agent_id":"worker", "status":"completed",
            "messages":[{"role":"tool", "content":content}],
            "result":{"final_answer":"done"}, "delivered":false, "now_ms":200
        }), 200);
        assert!(checkpoint.validate().unwrap() > crate::protocol::MAX_PAYLOAD_BYTES);
        commit_request(&mut database, 11, &checkpoint).unwrap();
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let recovery = commit_request(&mut database, 11, &request("swarm.session.get",
            json!({"swarm_key":"large"}), 300)).unwrap();
        assert_eq!(recovery["agents"][0]["messages"][0]["content"], content);
        let delivery = request("swarm.agents.mark_delivered", json!({
            "swarm_key":"large", "agent_ids":["worker"], "now_ms":400
        }), 400);
        commit_request(&mut database, 11, &delivery).unwrap();
        let recovered = commit_request(&mut database, 11, &request("swarm.session.get",
            json!({"swarm_key":"large"}), 500)).unwrap();
        assert_eq!(recovered["agents"][0]["messages"][0]["content"], content);
        assert_eq!(recovered["agents"][0]["delivered"], true);
    }

    #[test]
    fn agent_state_get_reads_no_checkpoint_body() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let state = |swarm_key: &str, agent_id: &str| request("swarm.agent.state.get", json!({
            "swarm_key":swarm_key, "agent_id":agent_id
        }), 300);
        assert!(!state("target", "worker").mutates_state());
        assert_eq!(commit_request(&mut database, 11, &state("target", "worker")).unwrap(), Value::Null);
        commit_request(&mut database, 11, &request("swarm.session.save", json!({
            "swarm_key":"target", "conv_id":"conv", "task_id":"launch",
            "specs":[{"id":"worker", "objective":"o".repeat(4096)}],
            "config":{"user_id":11}, "now_ms":100
        }), 100)).unwrap();
        save_running_checkpoint(&mut database, "target", "worker", 2 * 1024 * 1024);
        let found = commit_request(&mut database, 11, &state("target", "worker")).unwrap();
        assert_eq!(found, json!({
            "swarm_key":"target", "conv_id":"conv", "task_id":"launch", "status":"running",
            "created_at":100, "updated_at":100,
            "agent":{"agent_id":"worker", "status":"running", "rounds_used":0,
                     "delivered":false, "updated_at":200}
        }));
        let absent = commit_request(&mut database, 11, &state("target", "other")).unwrap();
        assert_eq!(absent["agent"], Value::Null);
        assert_eq!(commit_request(&mut database, 12, &state("target", "worker")).unwrap(), Value::Null);
    }

    #[test]
    fn checkpoint_marker_overlays_compact_state_until_the_next_full_save() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        commit_request(&mut database, 11, &request("swarm.session.save", json!({
            "swarm_key":"marked", "task_id":"launch", "specs":[],
            "config":{"user_id":11}, "now_ms":100
        }), 100)).unwrap();
        let mark = |result: Value, now: u64| request("swarm.agent.checkpoint.mark", json!({
            "swarm_key":"marked", "agent_id":"worker", "rounds_used":3,
            "result":result, "now_ms":now
        }), now);
        let in_flight = json!({"checkpoint_phase":"model_in_flight",
                               "actuals_complete":false, "total_tokens":140});
        assert_eq!(commit_request(&mut database, 11, &mark(in_flight.clone(), 150)).unwrap(),
                   json!({"marked":false}));
        let state = request("swarm.agent.state.get",
                            json!({"swarm_key":"marked", "agent_id":"worker"}), 151);
        assert_eq!(commit_request(&mut database, 11, &state).unwrap()["agent"], Value::Null);

        let messages = json!([{"role":"tool", "content":"x".repeat(1 << 20)}]);
        let settled = json!({"checkpoint_phase":"ready", "actuals_complete":true,
                             "total_tokens":100, "tool_log":[{"tool":"read_files"}]});
        let save = |now: u64| request("swarm.agent.save", json!({
            "swarm_key":"marked", "agent_id":"worker", "status":"running",
            "messages":messages, "result":settled, "rounds_used":2, "now_ms":now
        }), now);
        commit_request(&mut database, 11, &save(200)).unwrap();
        assert_eq!(commit_request(&mut database, 11, &mark(in_flight, 300)).unwrap(),
                   json!({"marked":true}));
        assert_eq!(commit_request(&mut database, 11, &mark(json!({"cost_usd":0.25}), 310)).unwrap(),
                   json!({"marked":true}));
        commit_request(&mut database, 11, &request("swarm.agents.mark_delivered", json!({
            "swarm_key":"marked", "agent_ids":["worker"]
        }), 320)).unwrap();
        drop(database);

        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let get = request("swarm.session.get", json!({"swarm_key":"marked"}), 400);
        let agent = commit_request(&mut database, 11, &get).unwrap()["agents"][0].clone();
        assert_eq!(agent["result"], json!({
            "checkpoint_phase":"model_in_flight", "actuals_complete":false,
            "total_tokens":140, "cost_usd":0.25, "tool_log":[{"tool":"read_files"}]
        }));
        assert_eq!((agent["rounds_used"].clone(), agent["updated_at"].clone(),
                    agent["delivered"].clone()), (json!(3), json!(310), json!(true)));
        assert_eq!(agent["messages"], messages);
        assert_eq!(agent_page(&database, 11, json!({"swarm_key":"marked"}))["agents"][0], agent);
        let compact = commit_request(&mut database, 11, &state).unwrap()["agent"].clone();
        assert_eq!((compact["rounds_used"].clone(), compact["updated_at"].clone()),
                   (json!(3), json!(310)));

        commit_request(&mut database, 11, &save(500)).unwrap();
        let agent = commit_request(&mut database, 11, &get).unwrap()["agents"][0].clone();
        assert_eq!((agent["result"].clone(), agent["rounds_used"].clone()), (settled, json!(2)));

        for invalid in [json!({}), json!({"messages":[]}), json!({"total_tokens":[1]}),
                        json!({"checkpoint_phase":"p".repeat(65)}), json!("ready")] {
            assert_eq!(commit_request(&mut database, 11, &mark(invalid, 600)).unwrap_err().kind(),
                       io::ErrorKind::InvalidInput);
        }
        assert_eq!(commit_request(&mut database, 12, &mark(json!({"rounds_used":4}), 700)).unwrap(),
                   json!({"marked":false}));
    }

    #[test]
    fn appended_checkpoints_read_exactly_as_full_saves_and_refuse_any_other_base() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for swarm_key in ["appended", "saved"] {
            commit_request(&mut database, 11, &request("swarm.session.save", json!({
                "swarm_key":swarm_key, "task_id":"launch", "specs":[],
                "config":{"user_id":11}, "now_ms":100
            }), 100)).unwrap();
        }
        let message = |index: usize| json!({"role":"tool", "content":format!("{index}:")
                                                 + &"x".repeat(1 << 16)});
        let save = |swarm_key: &str, messages: Value, result: Value, checkpoint_id: &str,
                    status: &str, now: u64| {
            let mut payload = json!({
                "swarm_key":swarm_key, "agent_id":"worker", "role":"coder",
                "objective":"verify", "status":status, "messages":messages,
                "result":result, "rounds_used":now / 100, "now_ms":now
            });
            if !checkpoint_id.is_empty() {
                payload["checkpoint_id"] = json!(checkpoint_id);
            }
            request("swarm.agent.save", payload, now)
        };
        let append = |base: &str, count: usize, messages: Value, result: Value, id: &str,
                      status: &str, now: u64| request("swarm.agent.append", json!({
            "swarm_key":"appended", "agent_id":"worker", "base_checkpoint_id":base,
            "base_message_count":count, "checkpoint_id":id, "messages":messages,
            "result":result, "status":status, "rounds_used":now / 100, "now_ms":now
        }), now);
        assert_eq!(commit_request(&mut database, 11,
            &append("c0", 0, json!([]), json!({}), "c1", "running", 150)).unwrap(),
            json!({"appended":false}));
        assert_eq!(commit_request(&mut database, 11, &save("appended", json!([message(0)]),
            json!({"step":0}), "c0", "running", 200)).unwrap(),
            json!({"saved":true, "checkpoint_id":"c0"}));
        assert_eq!(commit_request(&mut database, 11, &request("swarm.agent.checkpoint.mark", json!({
            "swarm_key":"appended", "agent_id":"worker", "rounds_used":2,
            "result":{"checkpoint_phase":"model_in_flight"}, "now_ms":250
        }), 250)).unwrap(), json!({"marked":true}));
        commit_request(&mut database, 11, &request("swarm.agents.mark_delivered", json!({
            "swarm_key":"appended", "agent_ids":["worker"]
        }), 260)).unwrap();
        assert_eq!(commit_request(&mut database, 11, &append("c0", 1,
            json!([message(1), message(2)]), json!({"step":1}), "c1", "running", 300)).unwrap(),
            json!({"appended":true}));
        // A stale base, a wrong count, a foreign owner or a replaced core refuse unchanged.
        for (base, count) in [("c0", 3), ("c1", 2), ("c1", 4)] {
            assert_eq!(commit_request(&mut database, 11, &append(base, count,
                json!([message(9)]), json!({"step":9}), "c9", "failed", 350)).unwrap(),
                json!({"appended":false}));
        }
        assert_eq!(commit_request(&mut database, 12, &append("c1", 3,
            json!([message(9)]), json!({}), "c9", "failed", 360)).unwrap(),
            json!({"appended":false}));
        assert_eq!(commit_request(&mut database, 11, &append("c1", 3, json!([message(3)]),
            json!({"step":3, "final_answer":"done"}), "c2", "completed", 400)).unwrap(),
            json!({"appended":true}));
        drop(database);

        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let messages = json!([message(0), message(1), message(2), message(3)]);
        commit_request(&mut database, 11, &save("saved", messages.clone(),
            json!({"step":3, "final_answer":"done"}), "", "completed", 400)).unwrap();
        commit_request(&mut database, 11, &request("swarm.agents.mark_delivered", json!({
            "swarm_key":"saved", "agent_ids":["worker"]
        }), 410)).unwrap();
        let read = |database: &mut AuthorityDatabase, swarm_key: &str| commit_request(
            database, 11, &request("swarm.session.get", json!({"swarm_key":swarm_key}), 500))
            .unwrap()["agents"][0].clone();
        let appended = read(&mut database, "appended");
        assert_eq!(appended, read(&mut database, "saved"));
        assert_eq!(appended["messages"], messages);
        assert_eq!((appended["status"].clone(), appended["delivered"].clone(),
                    appended["rounds_used"].clone()), (json!("completed"), json!(true), json!(4)));
        assert_eq!(agent_page(&database, 11, json!({"swarm_key":"appended"}))["agents"][0],
                   appended);
        assert_eq!(commit_request(&mut database, 11,
            &request("swarm.resumable.list", json!({}), 510)).unwrap(), json!([]));

        // A writer without a checkpoint id replaces the base; appends then refuse.
        commit_request(&mut database, 11, &save("appended", json!([message(0)]), json!({}), "",
            "running", 600)).unwrap();
        assert_eq!(commit_request(&mut database, 11, &append("c2", 4, json!([message(4)]),
            json!({}), "c3", "running", 700)).unwrap(), json!({"appended":false}));
        for invalid in [json!({"base_checkpoint_id":""}), json!({"checkpoint_id":null}),
                        json!({"base_message_count":-1}), json!({"messages":{}}),
                        json!({"result":[]}), json!({"rounds_used":MAX_SWARM_ROUNDS_USED + 1})] {
            let mut malformed = append("c2", 1, json!([]), json!({}), "c3", "running", 800);
            malformed.payload.extend(invalid.as_object().unwrap().clone());
            assert_eq!(commit_request(&mut database, 11, &malformed).unwrap_err().kind(),
                       io::ErrorKind::InvalidInput);
        }
    }

    fn save_running_checkpoint(
        database: &mut AuthorityDatabase,
        swarm_key: &str,
        agent_id: &str,
        content_bytes: usize,
    ) {
        commit_request(database, 11, &request("swarm.agent.save", json!({
            "swarm_key":swarm_key, "agent_id":agent_id, "status":"running",
            "messages":[{"role":"tool", "content":"x".repeat(content_bytes)}],
            "result":{}, "now_ms":200
        }), 200)).unwrap();
    }

    fn agent_page(database: &AuthorityDatabase, owner: u64, payload: Value) -> Value {
        let mut transaction = database.begin_with_identity_claim_scopes(7, owner).unwrap();
        let raw = execute(database, &mut transaction, &request("swarm.agents.page", payload, 400))
            .unwrap()
            .unwrap();
        assert!(raw.len() <= MAX_SWARM_RESPONSE_BYTES);
        decode(&raw, "swarm agent page").unwrap()
    }

    #[test]
    fn retained_checkpoints_beyond_one_response_stay_recoverable_in_bounded_reads() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (now, swarm_key) in [(100, "older"), (101, "newer")] {
            commit_request(&mut database, 11, &request("swarm.session.save", json!({
                "swarm_key":swarm_key, "specs":[{"id":"a"}], "config":{"user_id":11},
                "now_ms":now
            }), now)).unwrap();
        }
        // Retained history (four checkpoints) exceeds one response, and so
        // does the newer session alone (three checkpoints).
        let content_bytes = MAX_SWARM_RESPONSE_BYTES / 3 + 1;
        save_running_checkpoint(&mut database, "older", "a", content_bytes);
        for agent_id in ["a", "b", "c"] {
            save_running_checkpoint(&mut database, "newer", agent_id, content_bytes);
        }

        let list = commit_request(&mut database, 11,
            &request("swarm.resumable.list", json!({}), 300)).unwrap();
        assert_eq!(list, json!([
            {"swarm_key":"newer", "updated_at":101},
            {"swarm_key":"older", "updated_at":100},
        ]));

        // The complete session read keeps refusing incomplete recovery state,
        // as a deterministic bound that a retry cannot satisfy.
        let refusal = commit_request(&mut database, 11, &request("swarm.session.get",
            json!({"swarm_key":"newer"}), 301)).unwrap_err();
        assert_eq!(refusal.kind(), io::ErrorKind::OutOfMemory);
        let wire = crate::semantic_executor::map_storage_error(refusal);
        assert_eq!(wire.code(), "database_resource_exhausted");
        assert!(!wire.retryable());

        let header = commit_request(&mut database, 11, &request("swarm.session.get",
            json!({"swarm_key":"newer", "include_agents":false}), 302)).unwrap();
        assert_eq!(header["swarm_key"], "newer");
        assert_eq!(header["specs"], json!([{"id":"a"}]));
        assert_eq!(header["updated_at"], 101);
        assert!(header.get("agents").is_none());
        assert!(commit_request(&mut database, 11, &request("swarm.session.get",
            json!({"swarm_key":"newer", "include_agents":"no"}), 303)).is_err());

        let mut pages = Vec::new();
        let mut payload = json!({"swarm_key":"newer"});
        loop {
            let page = agent_page(&database, 11, payload.clone());
            let ids = page["agents"].as_array().unwrap().iter().map(|agent| {
                assert_eq!(agent["status"], "running");
                assert_eq!(agent["messages"][0]["content"].as_str().unwrap().len(), content_bytes);
                agent["agent_id"].as_str().unwrap().to_owned()
            }).collect::<Vec<_>>();
            pages.push(ids);
            let Some(next) = page["next_after_agent_id"].as_str() else { break };
            payload["after_agent_id"] = json!(next);
        }
        assert_eq!(pages, [vec!["a", "b"], vec!["c"]]);
        assert_eq!(agent_page(&database, 11, json!({"swarm_key":"older"}))["agents"]
            .as_array().unwrap().len(), 1);
        let foreign = agent_page(&database, 12, json!({"swarm_key":"newer"}));
        assert_eq!(foreign, json!({"agents":[], "next_after_agent_id":null}));
    }

    fn request(operation: &str, payload: Value, now_ms: u64) -> Request {
        Request {
            operation: operation.to_owned(),
            payload: payload.as_object().unwrap().clone(),
            now_ms,
        }
    }

    fn commit_request(
        database: &mut AuthorityDatabase,
        owner: u64,
        request: &Request,
    ) -> io::Result<Value> {
        let mut transaction = database.begin_with_identity_claim_scopes(7, owner)?;
        let response = execute(database, &mut transaction, request)?
            .ok_or_else(|| invalid_data("swarm test response is absent"))?;
        if request.mutates_state() && transaction.has_business_mutation() {
            database.commit(transaction)?;
        }
        decode(&response, "swarm test response")
    }

    #[test]
    fn tenant_global_key_claim_prevents_aliasing_without_leaking_owner_data() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let save = request(
            "swarm.session.save",
            json!({
                "swarm_key":"shared","specs":[],"config":{"user_id":11},
                "now_ms":100
            }),
            100,
        );
        assert_eq!(
            commit_request(&mut database, 11, &save).unwrap(),
            json!({"saved":true})
        );

        let mut other = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        let get = request("swarm.session.get", json!({"swarm_key":"shared"}), 101);
        assert_eq!(
            execute(&database, &mut other, &get).unwrap(),
            Some(b"null".to_vec())
        );
        assert_eq!(
            execute(&database, &mut other, &save).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        let delete = request("swarm.session.delete", json!({"swarm_key":"shared"}), 102);
        assert_eq!(
            decode::<Value>(
                &execute(&database, &mut other, &delete).unwrap().unwrap(),
                "cross-owner delete",
            )
            .unwrap(),
            json!({"deleted":false})
        );
        drop(other);

        assert_eq!(
            commit_request(&mut database, 11, &get).unwrap()["swarm_key"],
            "shared"
        );
    }

    #[test]
    fn one_thousand_agent_boundary_pages_and_delivery_updates_compact_state() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let save = request(
            "swarm.session.save",
            json!({
                "swarm_key":"paged","specs":[],"config":{"user_id":11},
                "now_ms":100
            }),
            100,
        );
        commit_request(&mut database, 11, &save).unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in 0..MAX_SWARM_AGENTS_PER_SESSION {
            let agent_id = format!("agent-{index:04}");
            let save_agent = request(
                "swarm.agent.save",
                json!({
                    "swarm_key":"paged","agent_id":agent_id,"status":"completed",
                    "messages":[{"role":"assistant","content":"done"}],
                    "result":{"final_answer":"done"},"delivered":false,
                    "now_ms":200
                }),
                200,
            );
            execute(&database, &mut transaction, &save_agent).unwrap();
        }
        database.commit(transaction).unwrap();

        let get = request("swarm.session.get", json!({"swarm_key":"paged"}), 300);
        let detail = commit_request(&mut database, 11, &get).unwrap();
        let agents = detail["agents"].as_array().unwrap();
        assert_eq!(agents.len(), MAX_SWARM_AGENTS_PER_SESSION);
        assert_eq!(agents.first().unwrap()["agent_id"], "agent-0000");
        assert_eq!(agents.last().unwrap()["agent_id"], "agent-0999");

        let ids = (0..MAX_SWARM_AGENTS_PER_SESSION)
            .map(|index| Value::String(format!("agent-{index:04}")))
            .collect::<Vec<_>>();
        let delivered = request(
            "swarm.agents.mark_delivered",
            json!({"swarm_key":"paged","agent_ids":ids}),
            301,
        );
        assert_eq!(
            commit_request(&mut database, 11, &delivered).unwrap(),
            json!({"changed":1000})
        );
        let list = request("swarm.resumable.list", json!({}), 302);
        assert_eq!(commit_request(&mut database, 11, &list).unwrap(), json!([]));
    }
}
