//! Offline swarm session/agent snapshot restoration for the exclusive
//! operator importer.
//!
//! The source keeps one `swarm_sessions` row per session and one
//! `swarm_agents` row per agent. The native authority splits the session into
//! a versioned document plus a compact lifecycle state, claims the swarm key
//! tenant-globally, and derives the resumable-session index and the per-agent
//! counts from agent state. The live `swarm.session.save`/`swarm.agent.save`
//! operations always stamp `updated_at` with the server clock and recompute
//! `resumable_agents`, so a replay through them could not preserve exact
//! source timestamps. This adapter rebuilds the native document, state, claim,
//! counts and resumable index in the same authority transaction, preserving
//! the source `created_at`/`updated_at`/`status`/`delivered` values verbatim
//! and deriving `resumable_agents` from the restored agent states exactly as
//! the live paths do. Insert-or-match keeps replays idempotent and fails
//! closed on a divergent or foreign occupant; get reconstructs the exact
//! source snapshot and scan enumerates the owner-scoped catalog independently.
//! Entry points: the `migration.swarm.session.*` and `migration.swarm.agent.*`
//! operations dispatched from `migration_session`.

use serde_json::{json, Value};
use std::io;

use super::*;
use crate::generated_tofudb_ir::{MAX_ENTITY_KEY_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS};

fn valid_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.chars().count() <= maximum
}

fn migration_invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "swarm snapshot already differs")
}
fn occupied() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "swarm identity already occupies the target",
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SessionRestore {
    swarm_key: String,
    conv_id: String,
    task_id: String,
    status: String,
    specs: Value,
    config: Value,
    created_at: u64,
    updated_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AgentRestore {
    swarm_key: String,
    agent_id: String,
    role: String,
    objective: String,
    status: String,
    messages: Value,
    result: Value,
    rounds_used: u64,
    delivered: bool,
    updated_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SessionIdentity {
    swarm_key: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AgentIdentity {
    swarm_key: String,
    agent_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogScan {
    cursor: Option<String>,
}

fn valid_session_restore(request: &SessionRestore) -> bool {
    valid_text(&request.swarm_key, MAX_SWARM_KEY_CHARACTERS)
        && request.conv_id.chars().count() <= 4096
        && request.task_id.chars().count() <= 4096
        && matches!(
            request.status.as_str(),
            SESSION_RUNNING | SESSION_TERMINATED | SESSION_QUARANTINED
        )
        && request.specs.is_array()
        && request.config.is_object()
        && request.created_at > 0
        && request.updated_at >= request.created_at
}

fn valid_agent_restore(request: &AgentRestore) -> bool {
    valid_text(&request.swarm_key, MAX_SWARM_KEY_CHARACTERS)
        && valid_text(&request.agent_id, MAX_SWARM_AGENT_ID_CHARACTERS)
        && request.role.chars().count() <= 4096
        && request.objective.chars().count() <= MAX_SWARM_OBJECTIVE_CHARACTERS
        && request.status.chars().count() <= 64
        && request.messages.is_array()
        && request.result.is_object()
        && request.rounds_used <= MAX_SWARM_ROUNDS_USED
        && request.updated_at > 0
}

fn session_snapshot(request: &SessionRestore) -> Value {
    json!({
        "swarm_key": request.swarm_key,
        "conv_id": request.conv_id,
        "task_id": request.task_id,
        "status": request.status,
        "specs": request.specs,
        "config": request.config,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
    })
}

fn agent_snapshot(request: &AgentRestore) -> Value {
    json!({
        "swarm_key": request.swarm_key,
        "agent_id": request.agent_id,
        "role": request.role,
        "objective": request.objective,
        "status": request.status,
        "messages": request.messages,
        "result": request.result,
        "rounds_used": request.rounds_used,
        "delivered": request.delivered,
        "updated_at": request.updated_at,
    })
}

fn read_claim_owner(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
) -> io::Result<Option<u64>> {
    match database.entity_get(tx, &session_claim_key(tx, swarm_key)?)? {
        None => Ok(None),
        Some(raw) => {
            let claim: SwarmClaim = decode(&raw, "swarm session claim")?;
            if claim.swarm_key != swarm_key {
                return Err(invalid_data("swarm session claim identity is inconsistent"));
            }
            Ok(Some(claim.owner_user_id))
        }
    }
}

fn restore_session(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: SessionRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid swarm session restore request"))?;
    if !valid_session_restore(&request) {
        return Err(migration_invalid("invalid swarm session restore request"));
    }
    let owner = tx.owner_user_id();
    match read_claim_owner(database, tx, &request.swarm_key)? {
        Some(claim_owner) if claim_owner == owner => {}
        Some(_) => return Err(occupied()),
        None => {
            // A session document/state without a claim is foreign live state.
            if read_session_document(database, tx, &request.swarm_key)?.is_some()
                || read_session_state(database, tx, &request.swarm_key)?.is_some()
            {
                return Err(occupied());
            }
        }
    }
    let document = SessionDocument {
        swarm_key: request.swarm_key.clone(),
        conv_id: request.conv_id.clone(),
        task_id: request.task_id.clone(),
        specs: request.specs.clone(),
        config: request.config.clone(),
    };
    let existing_document = read_session_document(database, tx, &request.swarm_key)?;
    let existing_state = read_session_state(database, tx, &request.swarm_key)?;
    if existing_document.is_some() != existing_state.is_some() {
        return Err(invalid_data("swarm session document/state split"));
    }
    if let (Some(old_document), Some(old_state)) = (&existing_document, &existing_state) {
        // Insert-or-match against the exact decoded snapshot the native get
        // path returns; a divergent occupant fails closed.
        let matches = old_document.swarm_key == document.swarm_key
            && old_document.conv_id == document.conv_id
            && old_document.task_id == document.task_id
            && old_document.specs == document.specs
            && old_document.config == document.config
            && old_state.status == request.status
            && old_state.created_at == request.created_at
            && old_state.updated_at == request.updated_at;
        if !matches {
            return Err(snapshot_conflict());
        }
        // The derived resumable index must agree with the stored state.
        verify_resumable_index(database, tx, &request.swarm_key, old_state)?;
        return Ok(false);
    }
    // Fresh insert: claim, document, state, count, resumable index commit
    // together in this authority transaction.
    database.entity_put(
        tx,
        session_claim_key(tx, &request.swarm_key)?,
        encode(
            &SwarmClaim {
                swarm_key: request.swarm_key.clone(),
                owner_user_id: owner,
            },
            "swarm session claim",
        )?,
    )?;
    let count_key = session_count_key(tx)?;
    let count = read_count(database, tx, &count_key)?;
    if count >= MAX_SWARM_SESSIONS_PER_OWNER as u64 {
        return Err(exhausted("swarm session capacity reached"));
    }
    // The session is restored before its agents; resumable_agents starts at
    // the count of already-restored resumable agents for this session.
    let resumable_agents = read_existing_resumable_agent_count(database, tx, &request.swarm_key)?;
    let state = SessionState {
        status: request.status.clone(),
        created_at: request.created_at,
        updated_at: request.updated_at,
        resumable_agents,
    };
    put_document(
        database,
        tx,
        PutRequest {
            key: session_document_key(tx, &request.swarm_key)?,
            namespace: SESSION_LOGICAL_NAMESPACE.to_owned(),
            logical_key: request.swarm_key.clone(),
            value_json: Vec::new(),
            expected_version: None,
            updated_at_ms: request.updated_at.max(1),
        },
        &document,
        MAX_SWARM_SESSION_DOCUMENT_BYTES,
    )?;
    write_count(database, tx, count_key, count + 1)?;
    replace_resumable_index(database, tx, &request.swarm_key, None, Some(&state))?;
    database.entity_put(
        tx,
        session_state_key(tx, &request.swarm_key)?,
        encode(&state, "swarm session state")?,
    )?;
    // Read back through the decode path before publication.
    let stored_document = read_session_document(database, tx, &request.swarm_key)?
        .ok_or_else(|| invalid_data("swarm session document is missing"))?;
    if stored_document.swarm_key != document.swarm_key
        || stored_document.conv_id != document.conv_id
        || stored_document.task_id != document.task_id
        || stored_document.specs != document.specs
        || stored_document.config != document.config
    {
        return Err(invalid_data("swarm session document readback differs"));
    }
    verify_resumable_index(database, tx, &request.swarm_key, &state)?;
    Ok(true)
}

fn verify_resumable_index(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    swarm_key: &str,
    state: &SessionState,
) -> io::Result<()> {
    let present = database
        .entity_get(tx, &resumable_index_key(tx, state.updated_at, swarm_key)?)?
        .is_some();
    if present != session_is_resumable(state) {
        return Err(invalid_data("swarm resumable index differs"));
    }
    Ok(())
}

fn get_session(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: SessionIdentity = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid swarm session get request"))?;
    if !valid_text(&request.swarm_key, MAX_SWARM_KEY_CHARACTERS) {
        return Err(migration_invalid("invalid swarm session identity"));
    }
    match read_claim_owner(database, tx, &request.swarm_key)? {
        Some(owner) if owner == tx.owner_user_id() => {}
        Some(_) => return Ok(Value::Null),
        None => return Ok(Value::Null),
    }
    let Some(document) = read_session_document(database, tx, &request.swarm_key)? else {
        return Ok(Value::Null);
    };
    let state = read_session_state(database, tx, &request.swarm_key)?
        .ok_or_else(|| invalid_data("swarm session document has no state"))?;
    if document.swarm_key != request.swarm_key {
        return Err(invalid_data("swarm session identity is inconsistent"));
    }
    Ok(json!({
        "swarm_key": document.swarm_key,
        "conv_id": document.conv_id,
        "task_id": document.task_id,
        "status": state.status,
        "specs": document.specs,
        "config": document.config,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }))
}

fn restore_agent(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: AgentRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid swarm agent restore request"))?;
    if !valid_agent_restore(&request) {
        return Err(migration_invalid("invalid swarm agent restore request"));
    }
    let owner = tx.owner_user_id();
    match read_claim_owner(database, tx, &request.swarm_key)? {
        Some(claim_owner) if claim_owner == owner => {}
        Some(_) => return Err(occupied()),
        None => return Err(migration_invalid("swarm agent session is not restored")),
    }
    let old_state = read_agent_state(database, tx, &request.swarm_key, &request.agent_id)?;
    let old_core = read_agent_core(database, tx, &request.swarm_key, &request.agent_id)?;
    if old_state.is_some() != old_core.is_some() {
        return Err(invalid_data("swarm agent core/state split"));
    }
    let core = AgentCore {
        swarm_key: request.swarm_key.clone(),
        agent_id: request.agent_id.clone(),
        role: request.role.clone(),
        objective: request.objective.clone(),
        messages: request.messages.clone(),
        result: request.result.clone(),
    };
    if let (Some(existing_core), Some(existing_state)) = (&old_core, &old_state) {
        let matches = existing_core.swarm_key == core.swarm_key
            && existing_core.agent_id == core.agent_id
            && existing_core.role == core.role
            && existing_core.objective == core.objective
            && existing_core.messages == core.messages
            && existing_core.result == core.result
            && existing_state.status == request.status
            && existing_state.rounds_used == request.rounds_used
            && existing_state.delivered == request.delivered
            && existing_state.updated_at == request.updated_at;
        if !matches {
            return Err(snapshot_conflict());
        }
        return Ok(false);
    }
    let count_key = agent_count_key(tx, &request.swarm_key)?;
    let count = read_count(database, tx, &count_key)?;
    if count >= MAX_SWARM_AGENTS_PER_SESSION as u64 {
        return Err(exhausted("swarm agent capacity reached"));
    }
    write_count(database, tx, count_key, count + 1)?;
    let state = AgentState {
        status: request.status.clone(),
        rounds_used: request.rounds_used,
        delivered: request.delivered,
        updated_at: request.updated_at,
    };
    put_document(
        database,
        tx,
        PutRequest {
            key: agent_key(
                tx,
                SWARM_AGENT_CORE_NAMESPACE,
                &request.swarm_key,
                &request.agent_id,
            )?,
            namespace: AGENT_CORE_LOGICAL_NAMESPACE.to_owned(),
            logical_key: format!("{}:{}", request.swarm_key, request.agent_id),
            value_json: Vec::new(),
            expected_version: None,
            updated_at_ms: request.updated_at.max(1),
        },
        &core,
        MAX_SWARM_AGENT_CORE_BYTES,
    )?;
    database.entity_put(
        tx,
        agent_key(
            tx,
            SWARM_AGENT_STATE_NAMESPACE,
            &request.swarm_key,
            &request.agent_id,
        )?,
        encode(&state, "swarm agent state")?,
    )?;
    // A newly resumable agent advances the session's derived resumable count
    // exactly as the live agent save path does.
    if agent_is_resumable(&state) {
        update_session_state(database, tx, &request.swarm_key, |session| {
            session.resumable_agents = session
                .resumable_agents
                .checked_add(1)
                .ok_or_else(|| invalid_data("swarm resumable count overflow"))?;
            Ok(())
        })?;
    }
    // Read back through the decode path before publication.
    let stored_core = read_agent_core(database, tx, &request.swarm_key, &request.agent_id)?
        .ok_or_else(|| invalid_data("swarm agent core is missing"))?;
    if stored_core.swarm_key != core.swarm_key
        || stored_core.agent_id != core.agent_id
        || stored_core.role != core.role
        || stored_core.objective != core.objective
        || stored_core.messages != core.messages
        || stored_core.result != core.result
    {
        return Err(invalid_data("swarm agent core readback differs"));
    }
    Ok(true)
}

fn get_agent(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: AgentIdentity = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid swarm agent get request"))?;
    if !valid_text(&request.swarm_key, MAX_SWARM_KEY_CHARACTERS)
        || !valid_text(&request.agent_id, MAX_SWARM_AGENT_ID_CHARACTERS)
    {
        return Err(migration_invalid("invalid swarm agent identity"));
    }
    match read_claim_owner(database, tx, &request.swarm_key)? {
        Some(owner) if owner == tx.owner_user_id() => {}
        _ => return Ok(Value::Null),
    }
    let Some(core) = read_agent_core(database, tx, &request.swarm_key, &request.agent_id)? else {
        return Ok(Value::Null);
    };
    let state = read_agent_state(database, tx, &request.swarm_key, &request.agent_id)?
        .ok_or_else(|| invalid_data("swarm agent core has no state"))?;
    if core.swarm_key != request.swarm_key || core.agent_id != request.agent_id {
        return Err(invalid_data("swarm agent identity is inconsistent"));
    }
    Ok(json!({
        "swarm_key": core.swarm_key,
        "agent_id": core.agent_id,
        "role": core.role,
        "objective": core.objective,
        "status": state.status,
        "messages": core.messages,
        "result": core.result,
        "rounds_used": state.rounds_used,
        "delivered": state.delivered,
        "updated_at": state.updated_at,
    }))
}

fn cursor_start(table: &str, cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let Some(encoded) = cursor.strip_prefix(table) else {
        return Err(migration_invalid("invalid swarm catalog cursor"));
    };
    let mut raw = encoded.as_bytes().to_vec();
    if raw.is_empty() || raw.len() > MAX_ENTITY_KEY_BYTES {
        return Err(migration_invalid("invalid swarm catalog cursor"));
    }
    raw.push(0);
    Ok(raw)
}

fn scan_sessions(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: CatalogScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid swarm session scan request"))?;
    let suffix = cursor_start("swarm_session:", request.cursor.as_deref())?;
    let start = owner_key(tx, SWARM_SESSION_STATE_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        SWARM_SESSION_STATE_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, raw) in &page {
        let swarm_key = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("swarm session catalog key is malformed"))?;
        let state: SessionState = decode(raw, "swarm session state")?;
        if read_claim_owner(database, tx, swarm_key)? != Some(tx.owner_user_id()) {
            return Err(invalid_data("swarm session catalog claim differs"));
        }
        if read_session_document(database, tx, swarm_key)?.is_none() {
            return Err(invalid_data("swarm session catalog has no document"));
        }
        verify_resumable_index(database, tx, swarm_key, &state)?;
        rows.push(json!({"swarm_key": swarm_key}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("swarm_key").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("swarm session catalog page is malformed"))?;
        Some(format!("swarm_session:{last}"))
    } else {
        None
    };
    // The owner session count must agree with the enumerated catalog.
    if cursor.is_none() {
        let count = read_count(database, tx, &session_count_key(tx)?)?;
        if rows.len() as u64 != count {
            return Err(invalid_data("swarm session count and catalog differ"));
        }
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn scan_agents(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: CatalogScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid swarm agent scan request"))?;
    let suffix = cursor_start("swarm_agent:", request.cursor.as_deref())?;
    let start = owner_key(tx, SWARM_AGENT_STATE_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        SWARM_AGENT_STATE_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    let mut last_key = None;
    for (physical, raw) in &page {
        let key_bytes = physical.key_bytes();
        // Agent keys are `<u16 swarm_key len><swarm_key><agent_id>`; split off
        // the length-prefixed session prefix to recover the agent identity.
        if key_bytes.len() < 2 {
            return Err(invalid_data("swarm agent catalog key is malformed"));
        }
        let swarm_len = u16::from_be_bytes(key_bytes[..2].try_into().unwrap()) as usize;
        if key_bytes.len() < 2 + swarm_len {
            return Err(invalid_data("swarm agent catalog key is malformed"));
        }
        let swarm_key = std::str::from_utf8(&key_bytes[2..2 + swarm_len])
            .map_err(|_| invalid_data("swarm agent catalog key is malformed"))?;
        let agent_id = std::str::from_utf8(&key_bytes[2 + swarm_len..])
            .map_err(|_| invalid_data("swarm agent catalog key is malformed"))?;
        let _: AgentState = decode(raw, "swarm agent state")?;
        if read_claim_owner(database, tx, swarm_key)? != Some(tx.owner_user_id()) {
            return Err(invalid_data("swarm agent catalog claim differs"));
        }
        if read_agent_core(database, tx, swarm_key, agent_id)?.is_none() {
            return Err(invalid_data("swarm agent catalog has no core"));
        }
        rows.push(json!({"swarm_key": swarm_key, "agent_id": agent_id}));
        last_key = Some(key_bytes.to_vec());
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = last_key.ok_or_else(|| invalid_data("swarm agent catalog page is malformed"))?;
        Some(format!(
            "swarm_agent:{}",
            std::str::from_utf8(&last)
                .map_err(|_| invalid_data("swarm agent catalog page is malformed"))?
        ))
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
    let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.swarm.session.restore" => {
            let inserted = restore_session(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.swarm.session.get" => get_session(database, &mut tx, payload),
        "migration.swarm.session.scan" => scan_sessions(database, &mut tx, payload),
        "migration.swarm.agent.restore" => {
            let inserted = restore_agent(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.swarm.agent.get" => get_agent(database, &mut tx, payload),
        "migration.swarm.agent.scan" => scan_agents(database, &mut tx, payload),
        _ => Err(migration_invalid("unknown swarm migration operation")),
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
        vfs.create_dir(Path::new("/swarm")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/swarm"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn session(key: &str, status: &str, created: u64, updated: u64) -> Value {
        json!({
            "swarm_key": key,
            "conv_id": format!("conv-{key}"),
            "task_id": format!("task-{key}"),
            "status": status,
            "specs": [{"role": "researcher"}],
            "config": {"user_id": 11},
            "created_at": created,
            "updated_at": updated,
        })
    }

    fn agent(key: &str, agent_id: &str, status: &str, delivered: bool, updated: u64) -> Value {
        json!({
            "swarm_key": key,
            "agent_id": agent_id,
            "role": "researcher",
            "objective": "find evidence",
            "status": status,
            "messages": [{"role": "user", "content": "go"}],
            "result": {"summary": "done"},
            "rounds_used": 3,
            "delivered": delivered,
            "updated_at": updated,
        })
    }

    #[test]
    fn session_restore_preserves_exact_state_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.swarm.session.restore",
                session("swarm-a", "running", 100, 200),
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/swarm"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.swarm.session.restore",
                session("swarm-a", "running", 100, 200),
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.swarm.session.get",
            json!({"swarm_key": "swarm-a"}),
        )
        .unwrap();
        assert_eq!(get["swarm_key"], "swarm-a");
        assert_eq!(get["status"], "running");
        assert_eq!(get["created_at"], 100);
        assert_eq!(get["updated_at"], 200);
        assert_eq!(get["config"], json!({"user_id": 11}));
        // A divergent occupant fails closed.
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.swarm.session.restore",
            session("swarm-a", "terminated", 100, 200),
        )
        .is_err());
        // A foreign owner cannot see the session.
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.swarm.session.get",
                json!({"swarm_key": "swarm-a"}),
            )
            .unwrap(),
            Value::Null
        );
    }

    #[test]
    fn agent_restore_derives_session_resumable_count_and_catalogs_agree() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.swarm.session.restore",
            session("swarm-a", "running", 100, 200),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.swarm.agent.restore",
            agent("swarm-a", "agent-1", "completed", false, 210),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.swarm.agent.restore",
            agent("swarm-a", "agent-2", "completed", true, 220),
        )
        .unwrap();
        let get = execute(
            &mut database,
            7,
            11,
            "migration.swarm.agent.get",
            json!({"swarm_key": "swarm-a", "agent_id": "agent-1"}),
        )
        .unwrap();
        assert_eq!(get["status"], "completed");
        assert_eq!(get["delivered"], false);
        assert_eq!(get["updated_at"], 210);
        // The undelivered completed agent is resumable; the resumable index
        // must list the session.
        let session_get = execute(
            &mut database,
            7,
            11,
            "migration.swarm.session.get",
            json!({"swarm_key": "swarm-a"}),
        )
        .unwrap();
        assert_eq!(session_get["swarm_key"], "swarm-a");
        // Session and agent catalogs agree with their counts.
        let sessions = execute(
            &mut database,
            7,
            11,
            "migration.swarm.session.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(sessions["rows"].as_array().unwrap().len(), 1);
        let agents = execute(
            &mut database,
            7,
            11,
            "migration.swarm.agent.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(agents["rows"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn invalid_payloads_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"swarm_key": "", "conv_id": "", "task_id": "", "status": "running",
                   "specs": [], "config": {}, "created_at": 1, "updated_at": 1}),
            json!({"swarm_key": "k", "conv_id": "", "task_id": "", "status": "bogus",
                   "specs": [], "config": {}, "created_at": 1, "updated_at": 1}),
            json!({"swarm_key": "k", "conv_id": "", "task_id": "", "status": "running",
                   "specs": [], "config": {}, "created_at": 0, "updated_at": 1}),
            json!({"swarm_key": "k", "conv_id": "", "task_id": "", "status": "running",
                   "specs": [], "config": {}, "created_at": 1, "updated_at": 1, "extra": 0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.swarm.session.restore",
                payload,
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.swarm.session.scan",
                json!({"cursor": Value::Null}),
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
