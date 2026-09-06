//! Durable, owner-scoped swarm sessions and resumable agent checkpoints.
//!
//! Large session and agent conversation bodies use versioned blob documents.
//! Compact lifecycle state owns delivery and resumability indexes so result
//! acknowledgement never rewrites message history. A tenant-global exact
//! swarm-key claim prevents cross-owner aliasing; every data read is owner local.

use std::collections::BTreeSet;
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_SWARM_AGENTS_PER_SESSION, MAX_SWARM_AGENT_CORE_BYTES,
    MAX_SWARM_AGENT_IDS_PER_DELIVERY, MAX_SWARM_AGENT_ID_CHARACTERS, MAX_SWARM_KEY_CHARACTERS,
    MAX_SWARM_OBJECTIVE_CHARACTERS, MAX_SWARM_RESPONSE_BYTES, MAX_SWARM_ROUNDS_USED,
    MAX_SWARM_SESSIONS_PER_OWNER, MAX_SWARM_SESSION_DOCUMENT_BYTES, SWARM_AGENT_CORE_NAMESPACE,
    SWARM_AGENT_COUNT_NAMESPACE, SWARM_AGENT_STATE_NAMESPACE, SWARM_RESUMABLE_INDEX_NAMESPACE,
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
fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
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
        return Err(exhausted("swarm response exceeds 8 MiB"));
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
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct AgentState {
    status: String,
    rounds_used: u64,
    delivered: bool,
    updated_at: u64,
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
            "swarm.session.get" | "swarm.resumable.list"
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
fn project_agent(core: &AgentCore, state: &AgentState, include_updated: bool) -> Value {
    let mut value = json!({
        "agent_id":core.agent_id,
        "role":core.role,
        "objective":core.objective,
        "status":state.status,
        "messages":core.messages,
        "result":core.result,
        "rounds_used":state.rounds_used,
        "delivered":state.delivered,
    });
    if include_updated {
        value["updated_at"] = json!(state.updated_at);
    }
    value
}
fn load_agents(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    include_updated: bool,
) -> io::Result<Vec<Value>> {
    let (start, end) = agent_range(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key)?;
    let states = scan_paged(database, tx, start, &end, MAX_SWARM_AGENTS_PER_SESSION + 1)?;
    if states.len() > MAX_SWARM_AGENTS_PER_SESSION {
        return Err(exhausted("swarm agent count exceeds its bound"));
    }
    let mut agents = Vec::with_capacity(states.len());
    for (key, raw) in states {
        let agent_id = decode_agent_id(&key, swarm_key)?;
        let state: AgentState = decode(&raw, "swarm agent state")?;
        let core = read_agent_core(database, tx, swarm_key, &agent_id)?
            .ok_or_else(|| invalid_data("swarm agent state has no core"))?;
        if core.swarm_key != swarm_key || core.agent_id != agent_id {
            return Err(invalid_data("swarm agent identity is inconsistent"));
        }
        agents.push(project_agent(&core, &state, include_updated));
    }
    Ok(agents)
}
fn project_session(
    document: &SessionDocument,
    state: &SessionState,
    agents: Vec<Value>,
    include_times: bool,
) -> Value {
    let mut value = json!({
        "swarm_key":document.swarm_key,
        "conv_id":document.conv_id,
        "task_id":document.task_id,
        "status":state.status,
        "specs":document.specs,
        "config":document.config,
        "agents":agents,
    });
    if include_times {
        value["created_at"] = json!(state.created_at);
        value["updated_at"] = json!(state.updated_at);
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
        "swarm.agents.mark_delivered" => mark_delivered(database, tx, request),
        "swarm.session.get" => session_get(database, tx, request),
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
            return Err(exhausted("swarm session capacity reached"));
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
        return Err(exhausted("swarm agent count exceeds its bound"));
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
        return Err(exhausted("swarm agent count exceeds its bound"));
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
            return Err(exhausted("swarm agent capacity reached"));
        }
        write_count(database, tx, count_key, count + 1)?;
    }
    let state = AgentState {
        status,
        rounds_used,
        delivered: delivered
            .unwrap_or_else(|| old_state.as_ref().is_some_and(|state| state.delivered)),
        updated_at: now,
    };
    let core = AgentCore {
        swarm_key: swarm_key.to_owned(),
        agent_id: agent_id.to_owned(),
        role,
        objective,
        messages,
        result,
    };
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
        &core,
        MAX_SWARM_AGENT_CORE_BYTES,
    )?;
    database.entity_put(
        tx,
        agent_key(tx, SWARM_AGENT_STATE_NAMESPACE, swarm_key, agent_id)?,
        encode(&state, "swarm agent state")?,
    )?;
    let old_resumable = old_state.as_ref().is_some_and(agent_is_resumable);
    let new_resumable = agent_is_resumable(&state);
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
    bounded_response(&json!({"saved":true}))
}

fn mark_delivered(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
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
    bounded_response(&json!({"changed":changed}))
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
    let agents = load_agents(database, tx, swarm_key, true)?;
    bounded_response(&project_session(&document, &state, agents, true))
}

fn resumable_list(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<Option<Vec<u8>>> {
    let (start, end) = resumable_range(tx)?;
    let rows = scan_paged(database, tx, start, &end, MAX_SWARM_SESSIONS_PER_OWNER + 1)?;
    if rows.len() > MAX_SWARM_SESSIONS_PER_OWNER {
        return Err(exhausted("swarm resumable session count exceeds its bound"));
    }
    let mut sessions = Vec::with_capacity(rows.len());
    for (index_key, raw) in rows {
        let swarm_key = std::str::from_utf8(&raw)
            .map_err(|_| invalid_data("swarm resumable index is malformed"))?;
        let document = read_session_document(database, tx, swarm_key)?
            .ok_or_else(|| invalid_data("swarm resumable index has no session"))?;
        let state = read_session_state(database, tx, swarm_key)?
            .ok_or_else(|| invalid_data("swarm resumable index has no state"))?;
        if !session_is_resumable(&state)
            || resumable_index_key(tx, state.updated_at, swarm_key)? != index_key
        {
            return Err(invalid_data("swarm resumable index is inconsistent"));
        }
        let agents = load_agents(database, tx, swarm_key, false)?;
        if !agents.iter().any(|agent| {
            let status = agent.get("status").and_then(Value::as_str).unwrap_or("");
            matches!(status, "pending" | "running" | "retrying")
                || (status == "completed"
                    && !agent
                        .get("delivered")
                        .and_then(Value::as_bool)
                        .unwrap_or(false))
        }) {
            return Err(invalid_data("swarm resumable count has no eligible agent"));
        }
        sessions.push(project_session(&document, &state, agents, false));
    }
    bounded_response(&Value::Array(sessions))
}

#[cfg(test)]
mod tests {
    use super::*;

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
        if request.mutates_state() {
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
