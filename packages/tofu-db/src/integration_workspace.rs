//! Durable Git-integration workspace queue and bounded project event history.
//!
//! Workspace documents are tenant-global and carry their owner because the
//! single integration worker claims work across owners. Public operations
//! verify the authenticated owner. Covering ready/integrating indexes and one
//! exact project claim replace table scans and anti-joins on the hot path.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    INTEGRATION_ACTIVE_COUNT_NAMESPACE, INTEGRATION_EVENT_COUNT_NAMESPACE,
    INTEGRATION_EVENT_NAMESPACE, INTEGRATION_EVENT_SEQUENCE_NAMESPACE,
    INTEGRATION_INTEGRATING_INDEX_NAMESPACE, INTEGRATION_NATURAL_CLAIM_NAMESPACE,
    INTEGRATION_PROJECT_ACTIVE_CLAIM_NAMESPACE, INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE,
    INTEGRATION_READY_INDEX_NAMESPACE, INTEGRATION_ROW_LOCATOR_NAMESPACE,
    INTEGRATION_ROW_SEQUENCE_NAMESPACE, INTEGRATION_STALE_SECONDS,
    INTEGRATION_WORKSPACE_COUNT_NAMESPACE, INTEGRATION_WORKSPACE_NAMESPACE, MAX_ENTITY_RANGE_ROWS,
    MAX_INTEGRATION_ACTIVE_WORKSPACES, MAX_INTEGRATION_EVENTS_PER_PROJECT,
    MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS, MAX_INTEGRATION_RESPONSE_BYTES,
    MAX_INTEGRATION_STATUS_EVENTS, MAX_INTEGRATION_STATUS_ROWS, MAX_INTEGRATION_TASK_ID_CHARACTERS,
    MAX_INTEGRATION_WORKSPACES_PER_OWNER, MAX_INTEGRATION_WORKSPACE_DOCUMENT_BYTES,
};

const SEQUENCE_KEY: &[u8] = b"sequence";
const ACTIVE_COUNT_KEY: &[u8] = b"active";

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
    let bytes = serde_json::to_vec(value)
        .map_err(|_| invalid_data(&format!("{name} cannot be encoded")))?;
    if bytes.len() > MAX_INTEGRATION_WORKSPACE_DOCUMENT_BYTES {
        return Err(exhausted("integration document exceeds 16 KiB"));
    }
    Ok(bytes)
}
fn decode<T: for<'de> Deserialize<'de>>(raw: &[u8], name: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| invalid_data(&format!("{name} is malformed")))
}
fn bounded_response(value: &Value) -> io::Result<Option<Vec<u8>>> {
    let bytes = serde_json::to_vec(value)
        .map_err(|_| invalid_data("integration response cannot be encoded"))?;
    if bytes.len() > MAX_INTEGRATION_RESPONSE_BYTES {
        return Err(exhausted("integration response exceeds 8 MiB"));
    }
    Ok(Some(bytes))
}
fn text<'a>(
    payload: &'a Map<String, Value>,
    field: &str,
    maximum: usize,
    required: bool,
) -> io::Result<&'a str> {
    match payload.get(field) {
        Some(Value::String(value))
            if (!required || !value.is_empty()) && value.chars().count() <= maximum =>
        {
            Ok(value)
        }
        None if !required => Ok(""),
        _ => Err(invalid_input("invalid integration text field")),
    }
}
fn optional_text(payload: &Map<String, Value>, field: &str, maximum: usize) -> io::Result<String> {
    match payload.get(field) {
        None => Ok(String::new()),
        Some(value) => value
            .as_str()
            .filter(|value| value.chars().count() <= maximum)
            .map(ToOwned::to_owned)
            .ok_or_else(|| invalid_input("invalid integration optional text field")),
    }
}
fn integer(payload: &Map<String, Value>, field: &str, minimum: u64) -> io::Result<u64> {
    payload
        .get(field)
        .and_then(Value::as_u64)
        .filter(|value| *value >= minimum && *value <= i64::MAX as u64)
        .ok_or_else(|| invalid_input("invalid integration integer field"))
}
fn number(payload: &Map<String, Value>, field: &str) -> io::Result<f64> {
    payload
        .get(field)
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite() && *value >= 0.0)
        .ok_or_else(|| invalid_input("invalid integration number field"))
}
fn global_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, raw)
}
fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("integration key text exceeds its bound"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}
fn natural_raw(owner: u64, project_root: &str, task_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = owner.to_be_bytes().to_vec();
    push_text(&mut raw, project_root)?;
    push_text(&mut raw, task_id)?;
    Ok(raw)
}
fn workspace_key(
    tx: &AuthorityTransaction,
    owner: u64,
    project_root: &str,
    task_id: &str,
) -> io::Result<EntityKey> {
    global_key(
        tx,
        INTEGRATION_WORKSPACE_NAMESPACE,
        &natural_raw(owner, project_root, task_id)?,
    )
}
fn natural_claim_key(
    tx: &AuthorityTransaction,
    owner: u64,
    project_root: &str,
    task_id: &str,
) -> io::Result<EntityKey> {
    global_key(
        tx,
        INTEGRATION_NATURAL_CLAIM_NAMESPACE,
        &natural_raw(owner, project_root, task_id)?,
    )
}
fn row_locator_key(tx: &AuthorityTransaction, row_id: u64) -> io::Result<EntityKey> {
    global_key(tx, INTEGRATION_ROW_LOCATOR_NAMESPACE, &row_id.to_be_bytes())
}
fn sequence_key(tx: &AuthorityTransaction, namespace: &str) -> io::Result<EntityKey> {
    global_key(tx, namespace, SEQUENCE_KEY)
}
fn active_count_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    global_key(tx, INTEGRATION_ACTIVE_COUNT_NAMESPACE, ACTIVE_COUNT_KEY)
}
fn owner_count_key(tx: &AuthorityTransaction, owner: u64) -> io::Result<EntityKey> {
    global_key(
        tx,
        INTEGRATION_WORKSPACE_COUNT_NAMESPACE,
        &owner.to_be_bytes(),
    )
}
fn ordered_f64(value: f64) -> io::Result<[u8; 8]> {
    if !value.is_finite() || value < 0.0 {
        return Err(invalid_input("integration timestamp is invalid"));
    }
    Ok(if value == 0.0 { 0.0 } else { value }
        .to_bits()
        .to_be_bytes())
}
fn queue_index_key(
    tx: &AuthorityTransaction,
    namespace: &str,
    workspace: &Workspace,
) -> io::Result<EntityKey> {
    let mut raw = ordered_f64(workspace.updated_at)?.to_vec();
    raw.extend_from_slice(&workspace.id.to_be_bytes());
    global_key(tx, namespace, &raw)
}
fn project_claim_key(tx: &AuthorityTransaction, project_root: &str) -> io::Result<EntityKey> {
    global_key(
        tx,
        INTEGRATION_PROJECT_ACTIVE_CLAIM_NAMESPACE,
        project_root.as_bytes(),
    )
}
fn project_index_prefix(owner: u64, project_root: &str) -> io::Result<Vec<u8>> {
    let mut raw = owner.to_be_bytes().to_vec();
    push_text(&mut raw, project_root)?;
    Ok(raw)
}
fn project_updated_key(tx: &AuthorityTransaction, workspace: &Workspace) -> io::Result<EntityKey> {
    let mut raw = project_index_prefix(workspace.owner_user_id, &workspace.project_root)?;
    let ordered_updated_at = u64::from_be_bytes(ordered_f64(workspace.updated_at)?);
    raw.extend_from_slice(&(!ordered_updated_at).to_be_bytes());
    raw.extend_from_slice(&(!workspace.id).to_be_bytes());
    global_key(tx, INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE, &raw)
}
fn project_updated_range(
    tx: &AuthorityTransaction,
    owner: u64,
    project_root: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE,
        &project_index_prefix(owner, project_root)?,
    )
}
fn event_prefix(owner: u64, project_root: &str) -> io::Result<Vec<u8>> {
    project_index_prefix(owner, project_root)
}
fn event_key(
    tx: &AuthorityTransaction,
    owner: u64,
    project_root: &str,
    event_id: u64,
) -> io::Result<EntityKey> {
    let mut raw = event_prefix(owner, project_root)?;
    raw.extend_from_slice(&(!event_id).to_be_bytes());
    global_key(tx, INTEGRATION_EVENT_NAMESPACE, &raw)
}
fn event_range(
    tx: &AuthorityTransaction,
    owner: u64,
    project_root: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        INTEGRATION_EVENT_NAMESPACE,
        &event_prefix(owner, project_root)?,
    )
}
fn event_count_key(
    tx: &AuthorityTransaction,
    owner: u64,
    project_root: &str,
) -> io::Result<EntityKey> {
    global_key(
        tx,
        INTEGRATION_EVENT_COUNT_NAMESPACE,
        &event_prefix(owner, project_root)?,
    )
}
fn read_u64(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    name: &str,
) -> io::Result<u64> {
    database
        .entity_get(tx, key)?
        .map(|raw| {
            raw.try_into()
                .map(u64::from_be_bytes)
                .map_err(|_| invalid_data(&format!("{name} is malformed")))
        })
        .transpose()
        .map(|value| value.unwrap_or(0))
}
fn write_u64(
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
fn next_sequence(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<u64> {
    let key = sequence_key(tx, namespace)?;
    let current = read_u64(database, tx, &key, "integration sequence")?;
    let next = current
        .checked_add(1)
        .ok_or_else(|| invalid_data("integration sequence overflow"))?;
    write_u64(database, tx, key, next)?;
    Ok(next)
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
        if page_len < page_limit || rows.len() == limit {
            break;
        }
        start = next
            .ok_or_else(|| invalid_data("integration pagination lost continuation"))?
            .1;
    }
    Ok(rows)
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct Locator {
    id: u64,
    owner_user_id: u64,
    project_root: String,
    task_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Workspace {
    id: u64,
    owner_user_id: u64,
    project_root: String,
    task_id: String,
    title: String,
    workspace_path: String,
    managed: bool,
    state: String,
    base_sha: String,
    checkpoint_sha: String,
    candidate_sha: String,
    error: String,
    origin_raw: String,
    created_at: f64,
    updated_at: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct IntegrationEvent {
    id: u64,
    owner_user_id: u64,
    project_root: String,
    task_id: String,
    kind: String,
    message: String,
    detail: String,
    created_at: f64,
}

#[derive(Clone, Debug)]
pub struct Request {
    pub operation: String,
    pub payload: Map<String, Value>,
    pub now_ms: u64,
}

impl Request {
    pub(crate) fn validate(&self) -> io::Result<usize> {
        let bytes = serde_json::to_vec(&self.payload)
            .map_err(|_| invalid_input("integration request cannot be encoded"))?
            .len();
        if !self.operation.starts_with("integration.")
            || self.operation.len() > 80
            || bytes > MAX_INTEGRATION_RESPONSE_BYTES
            || self.now_ms == 0
        {
            return Err(invalid_input("integration request exceeds its bound"));
        }
        Ok(bytes)
    }
    pub(crate) fn mutates_state(&self) -> bool {
        !matches!(
            self.operation.as_str(),
            "integration.status"
                | "integration.workspace.get"
                | "integration.workspace.get_integrating"
                | "integration.workspace.peek_ready"
        )
    }
}

fn locator(workspace: &Workspace) -> Locator {
    Locator {
        id: workspace.id,
        owner_user_id: workspace.owner_user_id,
        project_root: workspace.project_root.clone(),
        task_id: workspace.task_id.clone(),
    }
}
fn read_workspace(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    project_root: &str,
    task_id: &str,
) -> io::Result<Option<Workspace>> {
    database
        .entity_get(tx, &workspace_key(tx, owner, project_root, task_id)?)?
        .map(|raw| decode(&raw, "integration workspace"))
        .transpose()
}
fn read_workspace_by_id(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    row_id: u64,
) -> io::Result<Option<Workspace>> {
    let Some(raw) = database.entity_get(tx, &row_locator_key(tx, row_id)?)? else {
        return Ok(None);
    };
    let locator: Locator = decode(&raw, "integration row locator")?;
    if locator.id != row_id {
        return Err(invalid_data("integration row locator identity mismatch"));
    }
    let workspace = read_workspace(
        database,
        tx,
        locator.owner_user_id,
        &locator.project_root,
        &locator.task_id,
    )?
    .ok_or_else(|| invalid_data("integration row locator has no workspace"))?;
    if workspace.id != row_id {
        return Err(invalid_data("integration workspace row id mismatch"));
    }
    Ok(Some(workspace))
}
fn is_active(state: &str) -> bool {
    matches!(state, "ready" | "integrating")
}
fn remove_workspace_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    workspace: &Workspace,
) -> io::Result<()> {
    database.entity_delete(tx, project_updated_key(tx, workspace)?)?;
    if workspace.state == "ready" {
        database.entity_delete(
            tx,
            queue_index_key(tx, INTEGRATION_READY_INDEX_NAMESPACE, workspace)?,
        )?;
    }
    if workspace.state == "integrating" {
        database.entity_delete(
            tx,
            queue_index_key(tx, INTEGRATION_INTEGRATING_INDEX_NAMESPACE, workspace)?,
        )?;
        let claim_key = project_claim_key(tx, &workspace.project_root)?;
        let current = database.entity_get(tx, &claim_key)?;
        let expected_workspace_id = workspace.id.to_be_bytes();
        if current.as_deref() == Some(expected_workspace_id.as_slice()) {
            database.entity_delete(tx, claim_key)?;
        } else {
            return Err(invalid_data("integration project claim is inconsistent"));
        }
    }
    Ok(())
}
fn install_workspace_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    workspace: &Workspace,
) -> io::Result<()> {
    let encoded_locator = encode(&locator(workspace), "integration locator")?;
    database.entity_put(
        tx,
        project_updated_key(tx, workspace)?,
        encoded_locator.clone(),
    )?;
    if workspace.state == "ready" {
        database.entity_put(
            tx,
            queue_index_key(tx, INTEGRATION_READY_INDEX_NAMESPACE, workspace)?,
            encoded_locator.clone(),
        )?;
    }
    if workspace.state == "integrating" {
        let claim_key = project_claim_key(tx, &workspace.project_root)?;
        if database.entity_get(tx, &claim_key)?.is_some() {
            return Err(conflict("project already has an active integration"));
        }
        database.entity_put(tx, claim_key, workspace.id.to_be_bytes().to_vec())?;
        database.entity_put(
            tx,
            queue_index_key(tx, INTEGRATION_INTEGRATING_INDEX_NAMESPACE, workspace)?,
            encoded_locator,
        )?;
    }
    Ok(())
}
fn write_workspace(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    old: Option<&Workspace>,
    workspace: &Workspace,
) -> io::Result<()> {
    if let Some(old) = old {
        remove_workspace_indexes(database, tx, old)?;
    }
    let old_active = old.is_some_and(|value| is_active(&value.state));
    let new_active = is_active(&workspace.state);
    if old_active != new_active {
        let key = active_count_key(tx)?;
        let count = read_u64(database, tx, &key, "integration active count")?;
        let next = if new_active {
            if count >= MAX_INTEGRATION_ACTIVE_WORKSPACES as u64 {
                return Err(exhausted("integration active queue capacity reached"));
            }
            count + 1
        } else {
            count
                .checked_sub(1)
                .ok_or_else(|| invalid_data("integration active count underflow"))?
        };
        write_u64(database, tx, key, next)?;
    }
    database.entity_put(
        tx,
        workspace_key(
            tx,
            workspace.owner_user_id,
            &workspace.project_root,
            &workspace.task_id,
        )?,
        encode(workspace, "integration workspace")?,
    )?;
    install_workspace_indexes(database, tx, workspace)
}
fn origin_document(raw: &str) -> Value {
    serde_json::from_str::<Value>(raw)
        .ok()
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}
fn workspace_projection(workspace: &Workspace) -> Value {
    json!({
        "id":workspace.id,
        "user_id":workspace.owner_user_id,
        "project_root":workspace.project_root,
        "task_id":workspace.task_id,
        "title":workspace.title,
        "workspace_path":workspace.workspace_path,
        "managed":u8::from(workspace.managed),
        "state":workspace.state,
        "base_sha":workspace.base_sha,
        "checkpoint_sha":workspace.checkpoint_sha,
        "candidate_sha":workspace.candidate_sha,
        "error":workspace.error,
        "origin":origin_document(&workspace.origin_raw),
        "created_at":workspace.created_at,
        "updated_at":workspace.updated_at,
    })
}

fn worker_workspace_projection(workspace: &Workspace) -> Value {
    let mut projection = workspace_projection(workspace);
    projection["origin"] = json!({});
    projection
}
fn event_projection(event: &IntegrationEvent) -> Value {
    json!({
        "id":event.id,"user_id":event.owner_user_id,
        "project_root":event.project_root,"task_id":event.task_id,
        "kind":event.kind,"message":event.message,"detail":event.detail,
        "created_at":event.created_at,
    })
}

struct EventAppend<'a> {
    owner: u64,
    project_root: &'a str,
    task_id: &'a str,
    kind: &'a str,
    message: &'a str,
    detail: &'a str,
    now: f64,
}

fn append_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    append: EventAppend<'_>,
) -> io::Result<()> {
    let event_id = next_sequence(database, tx, INTEGRATION_EVENT_SEQUENCE_NAMESPACE)?;
    let event = IntegrationEvent {
        id: event_id,
        owner_user_id: append.owner,
        project_root: append.project_root.to_owned(),
        task_id: append.task_id.to_owned(),
        kind: append.kind.to_owned(),
        message: append.message.chars().take(500).collect(),
        detail: append.detail.chars().take(4000).collect(),
        created_at: append.now,
    };
    database.entity_put(
        tx,
        event_key(tx, append.owner, append.project_root, event_id)?,
        encode(&event, "integration event")?,
    )?;
    let count_key = event_count_key(tx, append.owner, append.project_root)?;
    let count = read_u64(database, tx, &count_key, "integration event count")? + 1;
    if count > MAX_INTEGRATION_EVENTS_PER_PROJECT as u64 {
        let (start, end) = event_range(tx, append.owner, append.project_root)?;
        let rows =
            database.entity_scan(tx, &start, &end, MAX_INTEGRATION_EVENTS_PER_PROJECT + 1)?;
        if rows.len() != MAX_INTEGRATION_EVENTS_PER_PROJECT + 1 {
            return Err(invalid_data("integration event count/index mismatch"));
        }
        database.entity_delete(tx, rows.last().unwrap().0.clone())?;
        write_u64(
            database,
            tx,
            count_key,
            MAX_INTEGRATION_EVENTS_PER_PROJECT as u64,
        )?;
    } else {
        write_u64(database, tx, count_key, count)?;
    }
    Ok(())
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request.operation.as_str() {
        "integration.workspace.register" => register(database, tx, request),
        "integration.workspace.get" => get(database, tx, request),
        "integration.workspace.save_checkpoint" => checkpoint(database, tx, request),
        "integration.workspace.submit" => submit(database, tx, request),
        "integration.workspace.retry" => retry(database, tx, request),
        "integration.workspace.discard" => discard(database, tx, request),
        "integration.workspace.set_meta" => set_meta(database, tx, request),
        "integration.workspace.claim_next" => claim_next(database, tx, request),
        "integration.workspace.peek_ready" => peek_ready(database, tx, request),
        "integration.workspace.get_integrating" => get_integrating(database, tx, request),
        "integration.workspace.quarantine" => cas_state(database, tx, request, "quarantined"),
        "integration.workspace.requeue" => cas_state(database, tx, request, "ready"),
        "integration.workspace.mark_merged" => cas_state(database, tx, request, "merged"),
        "integration.workspace.mark_failed" => cas_state(database, tx, request, "failed"),
        "integration.event.record" => record_event(database, tx, request),
        "integration.status" => status(database, tx, request),
        _ => Err(invalid_input("unknown integration operation")),
    }
}

fn public_identity(request: &Request) -> io::Result<(u64, &str, &str)> {
    Ok((
        integer(&request.payload, "user_id", 1)?,
        text(
            &request.payload,
            "project_root",
            MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS,
            true,
        )?,
        text(
            &request.payload,
            "task_id",
            MAX_INTEGRATION_TASK_ID_CHARACTERS,
            true,
        )?,
    ))
}
fn required_workspace(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    project_root: &str,
    task_id: &str,
) -> io::Result<Workspace> {
    read_workspace(database, tx, owner, project_root, task_id)?
        .ok_or_else(|| conflict("unknown integration task"))
}

fn register(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let (owner, project_root, task_id) = public_identity(request)?;
    let title = optional_text(&request.payload, "title", 1000)?;
    let workspace_path = text(&request.payload, "workspace_path", 4096, true)?.to_owned();
    let base_sha = text(&request.payload, "base_sha", 200, true)?.to_owned();
    let origin_raw = optional_text(&request.payload, "origin_json", 4000)?;
    let managed = request
        .payload
        .get("managed")
        .and_then(Value::as_bool)
        .ok_or_else(|| invalid_input("invalid integration managed flag"))?;
    let now = number(&request.payload, "now")?;
    let existing = read_workspace(database, tx, owner, project_root, task_id)?;
    if existing.as_ref().is_some_and(|workspace| {
        matches!(
            workspace.state.as_str(),
            "ready" | "integrating" | "merged" | "discarded"
        )
    }) {
        return Err(conflict("integration record is immutable or terminal"));
    }
    let workspace = if let Some(old) = &existing {
        Workspace {
            title,
            workspace_path,
            managed,
            state: "running".to_owned(),
            base_sha,
            checkpoint_sha: String::new(),
            candidate_sha: String::new(),
            error: String::new(),
            origin_raw: if origin_raw.is_empty() {
                old.origin_raw.clone()
            } else {
                origin_raw
            },
            updated_at: now,
            ..old.clone()
        }
    } else {
        let count_key = owner_count_key(tx, owner)?;
        let count = read_u64(database, tx, &count_key, "integration owner count")?;
        if count >= MAX_INTEGRATION_WORKSPACES_PER_OWNER as u64 {
            return Err(exhausted("integration workspace owner capacity reached"));
        }
        let id = next_sequence(database, tx, INTEGRATION_ROW_SEQUENCE_NAMESPACE)?;
        write_u64(database, tx, count_key, count + 1)?;
        let workspace = Workspace {
            id,
            owner_user_id: owner,
            project_root: project_root.to_owned(),
            task_id: task_id.to_owned(),
            title,
            workspace_path,
            managed,
            state: "running".to_owned(),
            base_sha,
            checkpoint_sha: String::new(),
            candidate_sha: String::new(),
            error: String::new(),
            origin_raw,
            created_at: now,
            updated_at: now,
        };
        let encoded_locator = encode(&locator(&workspace), "integration locator")?;
        database.entity_put(
            tx,
            natural_claim_key(tx, owner, project_root, task_id)?,
            id.to_be_bytes().to_vec(),
        )?;
        database.entity_put(tx, row_locator_key(tx, id)?, encoded_locator)?;
        workspace
    };
    write_workspace(database, tx, existing.as_ref(), &workspace)?;
    append_event(
        database,
        tx,
        EventAppend {
            owner,
            project_root,
            task_id,
            kind: "registered",
            message: "Writer workspace registered",
            detail: &workspace.workspace_path,
            now,
        },
    )?;
    bounded_response(&json!({"ok":true}))
}

fn get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let (owner, project_root, task_id) = public_identity(request)?;
    bounded_response(
        &read_workspace(database, tx, owner, project_root, task_id)?
            .map(|workspace| workspace_projection(&workspace))
            .unwrap_or(Value::Null),
    )
}

fn transition_public(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    action: &str,
) -> io::Result<Option<Vec<u8>>> {
    let (owner, project_root, task_id) = public_identity(request)?;
    let now = number(&request.payload, "now")?;
    let mut workspace = required_workspace(database, tx, owner, project_root, task_id)?;
    let old = workspace.clone();
    match action {
        "checkpoint" => {
            if matches!(workspace.state.as_str(), "ready" | "integrating")
                || matches!(workspace.state.as_str(), "discarded" | "merged")
                || !matches!(
                    workspace.state.as_str(),
                    "running" | "checkpointed" | "quarantined" | "failed"
                )
            {
                return Err(conflict(
                    "workspace cannot be checkpointed in its current state",
                ));
            }
            workspace.checkpoint_sha =
                text(&request.payload, "checkpoint_sha", 200, true)?.to_owned();
            let base = optional_text(&request.payload, "base_sha", 200)?;
            if !base.is_empty() {
                workspace.base_sha = base;
            }
            workspace.state = "checkpointed".to_owned();
            workspace.error.clear();
            workspace.updated_at = now;
            write_workspace(database, tx, Some(&old), &workspace)?;
            append_event(
                database,
                tx,
                EventAppend {
                    owner,
                    project_root,
                    task_id,
                    kind: "checkpointed",
                    message: &format!(
                        "Checkpoint {} captured without staging the workspace",
                        workspace
                            .checkpoint_sha
                            .chars()
                            .take(12)
                            .collect::<String>()
                    ),
                    detail: "",
                    now,
                },
            )?;
        }
        "submit" => {
            if workspace.state != "checkpointed" || workspace.checkpoint_sha.is_empty() {
                return Err(conflict(
                    "only a freshly checkpointed workspace can be submitted",
                ));
            }
            workspace.state = "ready".to_owned();
            workspace.error.clear();
            workspace.updated_at = now;
            write_workspace(database, tx, Some(&old), &workspace)?;
            append_event(
                database,
                tx,
                EventAppend {
                    owner,
                    project_root,
                    task_id,
                    kind: "submitted",
                    message: "Checkpoint entered the deterministic integration queue",
                    detail: "",
                    now,
                },
            )?;
        }
        "retry" => {
            if !matches!(workspace.state.as_str(), "quarantined" | "failed")
                || workspace.checkpoint_sha.is_empty()
            {
                return Err(conflict("workspace cannot be retried in its current state"));
            }
            workspace.state = "ready".to_owned();
            workspace.error.clear();
            workspace.updated_at = now;
            write_workspace(database, tx, Some(&old), &workspace)?;
            append_event(
                database,
                tx,
                EventAppend {
                    owner,
                    project_root,
                    task_id,
                    kind: "retried",
                    message: "Quarantined checkpoint returned to the queue",
                    detail: "",
                    now,
                },
            )?;
        }
        "discard" => {
            if workspace.state == "integrating" || workspace.state == "merged" {
                return Err(conflict(
                    "workspace cannot be discarded in its current state",
                ));
            }
            if workspace.state == "discarded" {
                return bounded_response(&json!({"ok":true,"changed":false}));
            }
            workspace.state = "discarded".to_owned();
            workspace.updated_at = now;
            write_workspace(database, tx, Some(&old), &workspace)?;
            append_event(
                database,
                tx,
                EventAppend {
                    owner,
                    project_root,
                    task_id,
                    kind: "discarded",
                    message: "Workspace discarded; refs and worktree kept for forensics",
                    detail: "",
                    now,
                },
            )?;
            return bounded_response(&json!({"ok":true,"changed":true}));
        }
        _ => return Err(invalid_input("unknown integration transition")),
    }
    bounded_response(&json!({"ok":true}))
}
fn checkpoint(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    transition_public(database, tx, request, "checkpoint")
}
fn submit(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    transition_public(database, tx, request, "submit")
}
fn retry(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    transition_public(database, tx, request, "retry")
}
fn discard(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    transition_public(database, tx, request, "discard")
}

fn set_meta(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let (owner, project_root, task_id) = public_identity(request)?;
    let patch_raw = text(&request.payload, "patch_json", 4000, true)?;
    let patch = serde_json::from_str::<Value>(patch_raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("invalid integration patch_json"))?;
    let mut workspace = required_workspace(database, tx, owner, project_root, task_id)?;
    let old = workspace.clone();
    let mut origin = origin_document(&workspace.origin_raw)
        .as_object()
        .cloned()
        .unwrap_or_default();
    origin.extend(patch);
    workspace.origin_raw = serde_json::to_string(&origin)
        .map_err(|_| invalid_data("integration origin cannot be encoded"))?;
    // Meta updates intentionally do not advance the workspace queue clock.
    write_workspace(database, tx, Some(&old), &workspace)?;
    bounded_response(&json!({"ok":true,"origin":origin}))
}

fn queue_rows(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<Vec<(EntityKey, Locator)>> {
    let (start, end) =
        EntityKey::prefix_range(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, b"")?;
    let rows = scan_paged(
        database,
        tx,
        start,
        &end,
        MAX_INTEGRATION_ACTIVE_WORKSPACES + 1,
    )?;
    if rows.len() > MAX_INTEGRATION_ACTIVE_WORKSPACES {
        return Err(exhausted("integration active index exceeds its bound"));
    }
    rows.into_iter()
        .map(|(key, raw)| Ok((key, decode(&raw, "integration queue locator")?)))
        .collect()
}
fn claimable_ready(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
) -> io::Result<Option<Workspace>> {
    for (_key, locator) in queue_rows(database, tx, INTEGRATION_READY_INDEX_NAMESPACE)? {
        if database
            .entity_get(tx, &project_claim_key(tx, &locator.project_root)?)?
            .is_some()
        {
            continue;
        }
        let workspace = read_workspace_by_id(database, tx, locator.id)?
            .ok_or_else(|| invalid_data("integration ready locator is missing"))?;
        if workspace.state != "ready" {
            return Err(invalid_data("integration ready index state mismatch"));
        }
        return Ok(Some(workspace));
    }
    Ok(None)
}
fn stale_integrating(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    now: f64,
) -> io::Result<Vec<Workspace>> {
    let cutoff = now - INTEGRATION_STALE_SECONDS;
    if cutoff <= 0.0 {
        return Ok(Vec::new());
    }
    let mut stale = Vec::new();
    for (_key, locator) in queue_rows(database, tx, INTEGRATION_INTEGRATING_INDEX_NAMESPACE)? {
        let workspace = read_workspace_by_id(database, tx, locator.id)?
            .ok_or_else(|| invalid_data("integration active locator is missing"))?;
        if workspace.state != "integrating" {
            return Err(invalid_data("integration active index state mismatch"));
        }
        if workspace.updated_at < cutoff {
            stale.push(workspace);
        }
    }
    stale.sort_by(|left, right| {
        left.updated_at
            .total_cmp(&right.updated_at)
            .then(left.id.cmp(&right.id))
    });
    Ok(stale)
}
fn claim_next(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let now = number(&request.payload, "now")?;
    for mut workspace in stale_integrating(database, tx, now)? {
        let old = workspace.clone();
        workspace.state = "ready".to_owned();
        workspace.error = "Recovered an interrupted integration".to_owned();
        workspace.updated_at = now;
        write_workspace(database, tx, Some(&old), &workspace)?;
    }
    let Some(mut workspace) = claimable_ready(database, tx)? else {
        return bounded_response(&Value::Null);
    };
    let old = workspace.clone();
    workspace.state = "integrating".to_owned();
    workspace.updated_at = now;
    write_workspace(database, tx, Some(&old), &workspace)?;
    bounded_response(&worker_workspace_projection(&workspace))
}
fn peek_ready(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    if let Some(workspace) = claimable_ready(database, tx)? {
        return bounded_response(&worker_workspace_projection(&workspace));
    }
    if request.payload.get("now").is_some() {
        let now = number(&request.payload, "now")?;
        if let Some(workspace) = stale_integrating(database, tx, now)?.into_iter().next() {
            return bounded_response(&worker_workspace_projection(&workspace));
        }
    }
    bounded_response(&Value::Null)
}
fn get_integrating(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let row_id = integer(&request.payload, "row_id", 1)?;
    bounded_response(
        &read_workspace_by_id(database, tx, row_id)?
            .filter(|workspace| workspace.state == "integrating")
            .map(|workspace| worker_workspace_projection(&workspace))
            .unwrap_or(Value::Null),
    )
}
fn cas_state(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    target: &str,
) -> io::Result<Option<Vec<u8>>> {
    let row_id = integer(&request.payload, "row_id", 1)?;
    let now = number(&request.payload, "now")?;
    let error = optional_text(&request.payload, "error", 4000)?;
    let candidate = optional_text(&request.payload, "candidate_sha", 200)?;
    if target == "merged" && candidate.is_empty() {
        return Err(invalid_input("integration candidate_sha is required"));
    }
    let Some(mut workspace) = read_workspace_by_id(database, tx, row_id)? else {
        return bounded_response(&json!({"changed":false}));
    };
    if workspace.state != "integrating" {
        return bounded_response(&json!({"changed":false}));
    }
    let old = workspace.clone();
    workspace.state = target.to_owned();
    workspace.error = if target == "merged" {
        String::new()
    } else {
        error.clone()
    };
    workspace.updated_at = now;
    if target == "merged" {
        workspace.candidate_sha = candidate.clone();
    }
    write_workspace(database, tx, Some(&old), &workspace)?;
    let event = match target {
        "quarantined" => Some(("quarantined", "Checkpoint needs attention", error.as_str())),
        "merged" => Some(("merged", "Checkpoint integrated into candidate", "")),
        "failed" => Some(("failed", "Integration worker failed", error.as_str())),
        _ => None,
    };
    if let Some((kind, message, detail)) = event {
        let message = if target == "merged" {
            format!(
                "Checkpoint integrated into candidate {}",
                candidate.chars().take(12).collect::<String>()
            )
        } else {
            message.to_owned()
        };
        append_event(
            database,
            tx,
            EventAppend {
                owner: workspace.owner_user_id,
                project_root: &workspace.project_root,
                task_id: &workspace.task_id,
                kind,
                message: &message,
                detail,
                now,
            },
        )?;
    }
    bounded_response(&json!({"changed":true}))
}
fn record_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let owner = integer(&request.payload, "user_id", 1)?;
    let project_root = text(
        &request.payload,
        "project_root",
        MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS,
        true,
    )?;
    let task_id = optional_text(
        &request.payload,
        "task_id",
        MAX_INTEGRATION_TASK_ID_CHARACTERS,
    )?;
    let kind = text(&request.payload, "kind", 200, true)?;
    let message = optional_text(&request.payload, "message", 500)?;
    let detail = optional_text(&request.payload, "detail", 4000)?;
    let now = number(&request.payload, "now")?;
    append_event(
        database,
        tx,
        EventAppend {
            owner,
            project_root,
            task_id: &task_id,
            kind,
            message: &message,
            detail: &detail,
            now,
        },
    )?;
    bounded_response(&json!({"ok":true}))
}
fn status(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    let owner = integer(&request.payload, "user_id", 1)?;
    let project_root = text(
        &request.payload,
        "project_root",
        MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS,
        true,
    )?;
    let (start, end) = project_updated_range(tx, owner, project_root)?;
    let workspace_rows = scan_paged(database, tx, start, &end, MAX_INTEGRATION_STATUS_ROWS + 1)?;
    if workspace_rows.len() > MAX_INTEGRATION_STATUS_ROWS {
        return Err(exhausted(
            "integration status workspace rows exceed their bound",
        ));
    }
    let mut workspaces = Vec::with_capacity(workspace_rows.len());
    for (_, raw) in workspace_rows {
        let locator: Locator = decode(&raw, "integration project index")?;
        let workspace = read_workspace_by_id(database, tx, locator.id)?
            .ok_or_else(|| invalid_data("integration project index has no workspace"))?;
        if workspace.owner_user_id != owner || workspace.project_root != project_root {
            return Err(invalid_data("integration project index leaks owner scope"));
        }
        workspaces.push(workspace_projection(&workspace));
    }
    let (event_start, event_end) = event_range(tx, owner, project_root)?;
    let event_rows =
        database.entity_scan(tx, &event_start, &event_end, MAX_INTEGRATION_STATUS_EVENTS)?;
    let mut events = Vec::with_capacity(event_rows.len());
    for (_, raw) in event_rows {
        let event: IntegrationEvent = decode(&raw, "integration event")?;
        if event.owner_user_id != owner || event.project_root != project_root {
            return Err(invalid_data("integration event index leaks owner scope"));
        }
        events.push(event_projection(&event));
    }
    bounded_response(&json!({"rows":workspaces,"events":events}))
}
