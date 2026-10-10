//! Offline orchestration definition/run/event snapshot restoration for the
//! exclusive operator importer.
//!
//! The source keeps definitions in `orchestration_definitions`, run headers in
//! `orchestration_runs`, and the bounded event history in
//! `orchestration_run_events`. The native authority separates the immutable
//! run core from the mutable lifecycle state, claims every identity
//! tenant-globally, and maintains the created/status/orchestration/goal/active
//! indexes and the per-owner counts. The live operations allocate identities
//! and stamp `updated_at`/`finished_at` with the server clock, and the Goal
//! lifecycle derives `goal_*` from transitions, so a replay through them could
//! not preserve exact source timestamps or a settled Goal outcome. This
//! adapter rebuilds the native documents, claims, indexes and counts in the
//! same authority transaction, preserving the source timestamps and the
//! importer-resolved Goal state verbatim. Insert-or-match keeps replays
//! idempotent and fails closed on a divergent or foreign occupant; get
//! reconstructs the exact source snapshot and scan enumerates the
//! owner-scoped catalog independently. Entry points: the
//! `migration.orchestration.{definition,run,event}.*` operations dispatched
//! from `migration_session`.

use serde_json::{json, Value};
use std::io;

use super::*;
use crate::generated_tofudb_ir::{MAX_ENTITY_KEY_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS};

fn migration_invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "orchestration snapshot already differs",
    )
}
fn occupied() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "orchestration identity already occupies the target",
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DefinitionRestore {
    id: String,
    tenant_label: String,
    name: String,
    definition: Value,
    created_at_ms: u64,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RunRestore {
    id: String,
    tenant_label: String,
    orch_id: String,
    name: String,
    definition: Value,
    input: String,
    created_by: String,
    created_at: u64,
    status: String,
    final_text: String,
    error: Value,
    updated_at: u64,
    finished_at: u64,
    next_event_sequence: u64,
    #[serde(default)]
    continuity_pending: bool,
    goal_status: String,
    goal_reason: String,
    goal_policy: Value,
    goal_outcome: Value,
    /// Full terminal stop report; absent on pre-stop-report snapshots.
    #[serde(default)]
    goal_stop_report: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventRestore {
    run_id: String,
    tenant_label: String,
    sequence: u64,
    payload: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IdentityRequest {
    id: String,
    tenant_label: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventIdentity {
    run_id: String,
    tenant_label: String,
    sequence: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventScan {
    run_id: String,
    tenant_label: String,
    after_sequence: Option<u64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogScan {
    tenant_label: String,
    cursor: Option<String>,
}

fn valid_label(label: &str) -> bool {
    label.chars().count() <= MAX_ORCHESTRATION_TENANT_LABEL_CHARACTERS
}

fn valid_definition_restore(request: &DefinitionRestore) -> bool {
    valid_text(&request.id, MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS)
        && valid_label(&request.tenant_label)
        && request.definition.is_object()
        && request.created_at_ms > 0
        && request.updated_at_ms >= request.created_at_ms
}

fn valid_run_restore(request: &RunRestore) -> bool {
    valid_text(&request.id, MAX_ORCHESTRATION_RUN_ID_CHARACTERS)
        && valid_label(&request.tenant_label)
        && request.orch_id.chars().count() <= 512
        && request.name.chars().count() <= 512
        && request.definition.is_object()
        && request.input.chars().count() <= MAX_ORCHESTRATION_RUN_CORE_BYTES
        && request.created_by.chars().count() <= 256
        && STATUSES.contains(&request.status.as_str())
        && request.created_at > 0
        && request.updated_at >= request.created_at
        && request.next_event_sequence <= MAX_ORCHESTRATION_EVENTS_PER_RUN as u64
        && request.goal_status.chars().count() <= 64
        && request.goal_reason.chars().count() <= 128
        && request.goal_policy.is_object()
        && request.goal_outcome.is_object()
        && request.goal_stop_report.chars().count() <= MAX_GOAL_STOP_REPORT_CHARACTERS
}

fn run_core(request: &RunRestore, owner: u64) -> RunCore {
    RunCore {
        id: request.id.clone(),
        owner,
        tenant_label: request.tenant_label.clone(),
        orch_id: request.orch_id.clone(),
        name: request.name.clone(),
        definition: request.definition.clone(),
        input: request.input.clone(),
        created_by: request.created_by.clone(),
        created_at: request.created_at,
    }
}

fn run_state(request: &RunRestore) -> RunState {
    RunState {
        continuity_pending: request.continuity_pending,
        status: request.status.clone(),
        final_text: request.final_text.clone(),
        error: request.error.clone(),
        updated_at: request.updated_at,
        finished_at: request.finished_at,
        next_event_sequence: request.next_event_sequence,
        goal_status: request.goal_status.clone(),
        goal_reason: request.goal_reason.clone(),
        goal_policy: request.goal_policy.clone(),
        goal_outcome: request.goal_outcome.clone(),
        goal_stop_report: request.goal_stop_report.clone(),
    }
}

fn run_snapshot(core: &RunCore, state: &RunState) -> Value {
    json!({
        "id": core.id,
        "tenant_label": core.tenant_label,
        "orch_id": core.orch_id,
        "name": core.name,
        "definition": core.definition,
        "input": core.input,
        "created_by": core.created_by,
        "created_at": core.created_at,
        "status": state.status,
        "final_text": state.final_text,
        "error": state.error,
        "updated_at": state.updated_at,
        "finished_at": state.finished_at,
        "next_event_sequence": state.next_event_sequence,
        "continuity_pending": state.continuity_pending,
        "goal_status": state.goal_status,
        "goal_reason": state.goal_reason,
        "goal_policy": state.goal_policy,
        "goal_outcome": state.goal_outcome,
        "goal_stop_report": state.goal_stop_report,
    })
}

fn restore_definition(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: DefinitionRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration definition restore request"))?;
    if !valid_definition_restore(&request) {
        return Err(migration_invalid(
            "invalid orchestration definition restore request",
        ));
    }
    let owner = tx.owner_user_id();
    if let Some(raw) = database.entity_get(tx, &definition_claim_key(tx, &request.id)?)? {
        let claim: Identity = decode(&raw, "definition claim")?;
        if !verify_identity(&claim, &request.id, owner, &request.tenant_label)? {
            return Err(occupied());
        }
    } else if database
        .entity_get(tx, &definition_key(tx, &request.id)?)?
        .is_some()
    {
        return Err(occupied());
    }
    let document = Definition {
        id: request.id.clone(),
        owner,
        tenant_label: request.tenant_label.clone(),
        name: request.name.clone(),
        definition: request.definition.clone(),
        created_at_ms: request.created_at_ms,
        updated_at_ms: request.updated_at_ms,
    };
    if let Some(existing) =
        read_definition(database, tx, &request.id, owner, &request.tenant_label)?
    {
        let matches = existing.name == document.name
            && existing.definition == document.definition
            && existing.created_at_ms == document.created_at_ms
            && existing.updated_at_ms == document.updated_at_ms;
        if !matches {
            return Err(snapshot_conflict());
        }
        // The owner updated-desc index and count must agree with the document.
        verify_definition_index(database, tx, &existing)?;
        return Ok(false);
    }
    let count_key = definition_count_key(tx)?;
    let count = read_count(database, tx, count_key.clone())?;
    if count >= MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER as u64 {
        return Err(exhausted("orchestration definition capacity reached"));
    }
    put_global_document(
        database,
        tx,
        definition_key(tx, &request.id)?,
        GlobalDocumentSpec {
            namespace: DEFINITION_LOGICAL_NAMESPACE,
            logical_key: &request.id,
            maximum_bytes: MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES,
        },
        &document,
        request.updated_at_ms,
    )?;
    database.entity_put(
        tx,
        definition_claim_key(tx, &request.id)?,
        encode(
            &Identity {
                id: request.id.clone(),
                owner,
                tenant_label: request.tenant_label.clone(),
            },
            "definition claim",
        )?,
    )?;
    database.entity_put(
        tx,
        definition_index_key(
            tx,
            &request.tenant_label,
            request.updated_at_ms,
            &request.id,
        )?,
        encode(
            &Identity {
                id: request.id.clone(),
                owner,
                tenant_label: request.tenant_label.clone(),
            },
            "definition index",
        )?,
    )?;
    write_count(database, tx, count_key, count + 1)?;
    let stored = read_definition(database, tx, &request.id, owner, &request.tenant_label)?
        .ok_or_else(|| invalid_data("orchestration definition is missing"))?;
    if stored.name != document.name
        || stored.definition != document.definition
        || stored.created_at_ms != document.created_at_ms
        || stored.updated_at_ms != document.updated_at_ms
    {
        return Err(invalid_data("orchestration definition readback differs"));
    }
    verify_definition_index(database, tx, &stored)?;
    Ok(true)
}

fn verify_definition_index(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Definition,
) -> io::Result<()> {
    let key = definition_index_key(
        tx,
        &document.tenant_label,
        document.updated_at_ms,
        &document.id,
    )?;
    let raw = database
        .entity_get(tx, &key)?
        .ok_or_else(|| invalid_data("orchestration definition index is missing"))?;
    let identity: Identity = decode(&raw, "definition index")?;
    if !verify_identity(
        &identity,
        &document.id,
        document.owner,
        &document.tenant_label,
    )? {
        return Err(invalid_data("orchestration definition index differs"));
    }
    Ok(())
}

fn get_definition(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: IdentityRequest = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration definition get request"))?;
    if !valid_text(&request.id, MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS)
        || !valid_label(&request.tenant_label)
    {
        return Err(migration_invalid(
            "invalid orchestration definition identity",
        ));
    }
    let Some(document) = read_definition(
        database,
        tx,
        &request.id,
        tx.owner_user_id(),
        &request.tenant_label,
    )?
    else {
        return Ok(Value::Null);
    };
    Ok(json!({
        "id": document.id,
        "tenant_label": document.tenant_label,
        "name": document.name,
        "definition": document.definition,
        "created_at_ms": document.created_at_ms,
        "updated_at_ms": document.updated_at_ms,
    }))
}

fn restore_run(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RunRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration run restore request"))?;
    if !valid_run_restore(&request) {
        return Err(migration_invalid(
            "invalid orchestration run restore request",
        ));
    }
    let owner = tx.owner_user_id();
    if let Some(raw) = database.entity_get(tx, &run_claim_key(tx, &request.id)?)? {
        let claim: Identity = decode(&raw, "orchestration run claim")?;
        if !verify_identity(&claim, &request.id, owner, &request.tenant_label)? {
            return Err(occupied());
        }
    } else if database
        .entity_get(tx, &run_core_key(tx, &request.id)?)?
        .is_some()
        || database
            .entity_get(tx, &run_state_key(tx, &request.id)?)?
            .is_some()
    {
        return Err(occupied());
    }
    let core = run_core(&request, owner);
    let state = run_state(&request);
    if state.continuity_pending && !is_restart_interrupted_goal(&core, &state) {
        return Err(migration_invalid("pending continuity requires a restart-interrupted Chat Goal"));
    }
    if let Some((existing_core, existing_state)) =
        read_run(database, tx, &request.id, owner, &request.tenant_label)?
    {
        if run_snapshot(&existing_core, &existing_state) != run_snapshot(&core, &state) {
            return Err(snapshot_conflict());
        }
        verify_run_indexes(database, tx, &existing_core, &existing_state)?;
        return Ok(false);
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
        &core,
        core.created_at,
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
        &state,
        state.updated_at.max(1),
    )?;
    database.entity_put(
        tx,
        run_claim_key(tx, &core.id)?,
        encode(
            &Identity {
                id: core.id.clone(),
                owner,
                tenant_label: core.tenant_label.clone(),
            },
            "run claim",
        )?,
    )?;
    add_run_indexes(database, tx, &core, &state)?;
    write_count(database, tx, count_key, count + 1)?;
    let (stored_core, stored_state) =
        read_run(database, tx, &request.id, owner, &request.tenant_label)?
            .ok_or_else(|| invalid_data("orchestration run is missing"))?;
    if run_snapshot(&stored_core, &stored_state) != run_snapshot(&core, &state) {
        return Err(invalid_data("orchestration run readback differs"));
    }
    verify_run_indexes(database, tx, &stored_core, &stored_state)?;
    Ok(true)
}

fn verify_run_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    core: &RunCore,
    state: &RunState,
) -> io::Result<()> {
    let index = run_index(core);
    let encoded = encode(&index, "orchestration run index")?;
    let created = run_created_index_key(tx, &core.tenant_label, core.created_at, &core.id)?;
    expect_index(database, tx, &created, true, &encoded)?;
    let status_key = run_status_index_key(
        tx,
        core.owner,
        &core.tenant_label,
        &state.status,
        core.created_at,
        &core.id,
    )?;
    expect_index(database, tx, &status_key, true, &encoded)?;
    let orch_key = run_orch_index_key(
        tx,
        &core.tenant_label,
        &core.orch_id,
        core.created_at,
        &core.id,
    )?;
    expect_index(database, tx, &orch_key, true, &encoded)?;
    let goal = core.created_by == GOAL_CREATED_BY;
    let goal_created = goal_created_index_key(
        tx,
        &core.tenant_label,
        &core.orch_id,
        core.created_at,
        &core.id,
    )?;
    expect_index(database, tx, &goal_created, goal, &encoded)?;
    expect_index(database, tx, &pending_continuity_key(tx, &index)?,
        state.continuity_pending, &encoded)?;
    let active = !is_terminal(&state.status);
    let active_key = active_index_key(tx, &index)?;
    expect_index(database, tx, &active_key, active, &encoded)?;
    let goal_active = goal_active_key(tx, core.owner, &core.tenant_label, &core.orch_id)?;
    expect_index(database, tx, &goal_active, active && goal, &encoded)?;
    Ok(())
}

fn expect_index(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    present: bool,
    encoded: &[u8],
) -> io::Result<()> {
    match (database.entity_get(tx, key)?, present) {
        (Some(raw), true) if raw == encoded => Ok(()),
        (None, false) => Ok(()),
        _ => Err(invalid_data("orchestration run index differs")),
    }
}

fn get_run(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: IdentityRequest = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration run get request"))?;
    if !valid_text(&request.id, MAX_ORCHESTRATION_RUN_ID_CHARACTERS)
        || !valid_label(&request.tenant_label)
    {
        return Err(migration_invalid("invalid orchestration run identity"));
    }
    let Some((core, state)) = read_run(
        database,
        tx,
        &request.id,
        tx.owner_user_id(),
        &request.tenant_label,
    )?
    else {
        return Ok(Value::Null);
    };
    Ok(run_snapshot(&core, &state))
}

fn restore_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: EventRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration event restore request"))?;
    if !valid_text(&request.run_id, MAX_ORCHESTRATION_RUN_ID_CHARACTERS)
        || !valid_label(&request.tenant_label)
        || request.sequence >= MAX_ORCHESTRATION_EVENTS_PER_RUN as u64
    {
        return Err(migration_invalid(
            "invalid orchestration event restore request",
        ));
    }
    let owner = tx.owner_user_id();
    let Some((core, mut state)) =
        read_run(database, tx, &request.run_id, owner, &request.tenant_label)?
    else {
        return Err(migration_invalid("orchestration event run is not restored"));
    };
    let key = event_key(tx, &core.id, request.sequence)?;
    if let Some(existing) = get_global_document::<EventDocument>(
        database,
        tx,
        &key,
        EVENT_LOGICAL_NAMESPACE,
        &format!("{}:{}", core.id, request.sequence),
        MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
    )? {
        let matches = existing.run_id == core.id
            && existing.owner == core.owner
            && existing.tenant_label == core.tenant_label
            && existing.sequence == request.sequence
            && existing.payload == request.payload;
        if !matches {
            return Err(snapshot_conflict());
        }
        return Ok(false);
    }
    if closes_continuity_election(&request.payload) {
        state.continuity_pending = false;
        write_run_state(database, tx, &core, &state.status.clone(), &state, state.updated_at)?;
        database.entity_delete(tx, pending_continuity_key(tx, &run_index(&core))?)?;
    }
    let document = EventDocument {
        run_id: core.id.clone(),
        owner: core.owner,
        tenant_label: core.tenant_label.clone(),
        sequence: request.sequence,
        payload: request.payload.clone(),
    };
    put_global_document(
        database,
        tx,
        key,
        GlobalDocumentSpec {
            namespace: EVENT_LOGICAL_NAMESPACE,
            logical_key: &format!("{}:{}", core.id, request.sequence),
            maximum_bytes: MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
        },
        &document,
        state.updated_at.max(1),
    )?;
    // The restored run state's next_event_sequence must already cover this
    // event; the importer resolves it from the source history.
    if request.sequence >= state.next_event_sequence {
        return Err(invalid_data(
            "orchestration event sequence exceeds the restored frontier",
        ));
    }
    Ok(true)
}

fn get_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: EventIdentity = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration event get request"))?;
    if !valid_text(&request.run_id, MAX_ORCHESTRATION_RUN_ID_CHARACTERS)
        || !valid_label(&request.tenant_label)
    {
        return Err(migration_invalid("invalid orchestration event identity"));
    }
    let owner = tx.owner_user_id();
    if read_run(database, tx, &request.run_id, owner, &request.tenant_label)?.is_none() {
        return Ok(Value::Null);
    }
    let key = event_key(tx, &request.run_id, request.sequence)?;
    let Some(document) = get_global_document::<EventDocument>(
        database,
        tx,
        &key,
        EVENT_LOGICAL_NAMESPACE,
        &format!("{}:{}", request.run_id, request.sequence),
        MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
    )?
    else {
        return Ok(Value::Null);
    };
    if document.run_id != request.run_id
        || document.owner != owner
        || document.tenant_label != request.tenant_label
        || document.sequence != request.sequence
    {
        return Err(invalid_data("orchestration event identity differs"));
    }
    Ok(json!({
        "run_id": document.run_id,
        "tenant_label": document.tenant_label,
        "sequence": document.sequence,
        "payload": document.payload,
    }))
}

fn cursor_start(table: &str, cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let Some(encoded) = cursor.strip_prefix(table) else {
        return Err(migration_invalid("invalid orchestration catalog cursor"));
    };
    let mut raw = encoded.as_bytes().to_vec();
    if raw.is_empty() || raw.len() > MAX_ENTITY_KEY_BYTES {
        return Err(migration_invalid("invalid orchestration catalog cursor"));
    }
    raw.push(0);
    Ok(raw)
}

fn scan_definitions(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: CatalogScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration definition scan request"))?;
    if !valid_label(&request.tenant_label) {
        return Err(migration_invalid("invalid orchestration tenant label"));
    }
    let suffix = cursor_start("orchestration_definition:", request.cursor.as_deref())?;
    let start = global_key(tx, ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("orchestration definition catalog key is malformed"))?;
        // Only this owner/label's definitions belong to the catalog.
        match read_definition(database, tx, id, tx.owner_user_id(), &request.tenant_label)? {
            Some(document) => {
                verify_definition_index(database, tx, &document)?;
                rows.push(json!({"id": id}));
            }
            None => continue,
        }
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("orchestration definition catalog page is malformed"))?;
        Some(format!("orchestration_definition:{last}"))
    } else {
        None
    };
    // Count self-proof is only sound for a complete single-page catalog;
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none() && cursor.is_none() {
        let count = read_count(database, tx, definition_count_key(tx)?)?;
        if rows.len() as u64 != count {
            return Err(invalid_data(
                "orchestration definition count and catalog differ",
            ));
        }
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn scan_runs(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: CatalogScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration run scan request"))?;
    if !valid_label(&request.tenant_label) {
        return Err(migration_invalid("invalid orchestration tenant label"));
    }
    let suffix = cursor_start("orchestration_run:", request.cursor.as_deref())?;
    let start = global_key(tx, ORCHESTRATION_RUN_CORE_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        ORCHESTRATION_RUN_CORE_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("orchestration run catalog key is malformed"))?;
        match read_run(database, tx, id, tx.owner_user_id(), &request.tenant_label)? {
            Some((core, state)) => {
                verify_run_indexes(database, tx, &core, &state)?;
                rows.push(json!({"id": id}));
            }
            None => continue,
        }
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("orchestration run catalog page is malformed"))?;
        Some(format!("orchestration_run:{last}"))
    } else {
        None
    };
    // Count self-proof is only sound for a complete single-page catalog;
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none() && cursor.is_none() {
        let count = read_count(database, tx, run_count_key(tx)?)?;
        if rows.len() as u64 != count {
            return Err(invalid_data("orchestration run count and catalog differ"));
        }
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn scan_events(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: EventScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid orchestration event scan request"))?;
    if !valid_text(&request.run_id, MAX_ORCHESTRATION_RUN_ID_CHARACTERS)
        || !valid_label(&request.tenant_label)
    {
        return Err(migration_invalid(
            "invalid orchestration event scan request",
        ));
    }
    let owner = tx.owner_user_id();
    let first = request
        .after_sequence
        .map(|sequence| sequence + 1)
        .unwrap_or(0);
    let start = event_key(tx, &request.run_id, first)?;
    let (_, end) = event_range(tx, &request.run_id)?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let bytes = physical.key_bytes();
        if bytes.len() < 2 + 8 {
            return Err(invalid_data("orchestration event catalog key is malformed"));
        }
        let run_len = u16::from_be_bytes(bytes[..2].try_into().unwrap()) as usize;
        if bytes.len() != 2 + run_len + 8 {
            return Err(invalid_data("orchestration event catalog key is malformed"));
        }
        let sequence = u64::from_be_bytes(bytes[2 + run_len..].try_into().unwrap());
        // Decode through the same versioned-document envelope the live event
        // page path returns, not the raw stored bytes.
        let document = get_global_document::<EventDocument>(
            database,
            tx,
            physical,
            EVENT_LOGICAL_NAMESPACE,
            &format!("{}:{sequence}", request.run_id),
            MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES,
        )?
        .ok_or_else(|| invalid_data("orchestration event catalog target is missing"))?;
        if document.run_id != request.run_id
            || document.owner != owner
            || document.tenant_label != request.tenant_label
            || document.sequence != sequence
        {
            return Err(invalid_data("orchestration event catalog identity differs"));
        }
        rows.push(json!({"run_id": document.run_id, "sequence": sequence}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        rows.last()
            .and_then(|row| row.get("sequence").and_then(Value::as_u64))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor.map(Value::from).unwrap_or(Value::Null)}))
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
        "migration.orchestration.definition.restore" => {
            let inserted = restore_definition(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.orchestration.definition.get" => get_definition(database, &mut tx, payload),
        "migration.orchestration.definition.scan" => scan_definitions(database, &mut tx, payload),
        "migration.orchestration.run.restore" => {
            let inserted = restore_run(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.orchestration.run.get" => get_run(database, &mut tx, payload),
        "migration.orchestration.run.scan" => scan_runs(database, &mut tx, payload),
        "migration.orchestration.event.restore" => {
            let inserted = restore_event(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.orchestration.event.get" => get_event(database, &mut tx, payload),
        "migration.orchestration.event.scan" => scan_events(database, &mut tx, payload),
        _ => Err(migration_invalid(
            "unknown orchestration migration operation",
        )),
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
        vfs.create_dir(Path::new("/orch")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/orch"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn definition(id: &str, name: &str, updated: u64) -> Value {
        json!({
            "id": id,
            "tenant_label": "",
            "name": name,
            "definition": {"name": name, "steps": []},
            "created_at_ms": 100,
            "updated_at_ms": updated,
        })
    }

    fn run(id: &str, status: &str, created_by: &str, created: u64, updated: u64) -> Value {
        json!({
            "id": id,
            "tenant_label": "",
            "orch_id": "orch-1",
            "name": "run",
            "definition": {"name": "run"},
            "input": "objective",
            "created_by": created_by,
            "created_at": created,
            "status": status,
            "final_text": "",
            "error": Value::Null,
            "updated_at": updated,
            "finished_at": 0,
            "next_event_sequence": 0,
            "goal_status": "",
            "goal_reason": "",
            "goal_policy": {},
            "goal_outcome": {},
        })
    }

    #[test]
    fn definition_restore_preserves_exact_state_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.orchestration.definition.restore",
                definition("def-a", "alpha", 200),
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/orch"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.orchestration.definition.restore",
                definition("def-a", "alpha", 200),
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.orchestration.definition.get",
            json!({"id": "def-a", "tenant_label": ""}),
        )
        .unwrap();
        assert_eq!(get["name"], "alpha");
        assert_eq!(get["updated_at_ms"], 200);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.orchestration.definition.restore",
            definition("def-a", "beta", 200),
        )
        .is_err());
    }

    #[test]
    fn run_restore_rebuilds_indexes_and_catalog_agrees_with_count() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.orchestration.run.restore",
            run("run-a", "running", "user", 100, 200),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.orchestration.run.restore",
            run("run-b", "done", "user", 110, 210),
        )
        .unwrap();
        let get = execute(
            &mut database,
            7,
            11,
            "migration.orchestration.run.get",
            json!({"id": "run-a", "tenant_label": ""}),
        )
        .unwrap();
        assert_eq!(get["status"], "running");
        assert_eq!(get["created_at"], 100);
        let scan = execute(
            &mut database,
            7,
            11,
            "migration.orchestration.run.scan",
            json!({"tenant_label": "", "cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(scan["rows"].as_array().unwrap().len(), 2);
        // A foreign owner sees an empty run catalog.
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.orchestration.run.scan",
                json!({"tenant_label": "", "cursor": Value::Null}),
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn event_restore_is_bound_by_the_run_frontier() {
        let (_vfs, mut database) = prepared();
        let mut run_doc = run("run-a", "running", "user", 100, 200);
        run_doc["next_event_sequence"] = json!(1);
        execute(
            &mut database,
            7,
            11,
            "migration.orchestration.run.restore",
            run_doc,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.orchestration.event.restore",
                json!({"run_id": "run-a", "tenant_label": "", "sequence": 0,
                       "payload": {"type": "started"}}),
            )
            .unwrap()["inserted"],
            true
        );
        // A sequence beyond the restored frontier fails closed.
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.orchestration.event.restore",
            json!({"run_id": "run-a", "tenant_label": "", "sequence": 5,
                   "payload": {"type": "other"}}),
        )
        .is_err());
        let scan = execute(
            &mut database,
            7,
            11,
            "migration.orchestration.event.scan",
            json!({"run_id": "run-a", "tenant_label": ""}),
        )
        .unwrap();
        assert_eq!(scan["rows"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn invalid_payloads_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"id": "", "tenant_label": "", "name": "a", "definition": {},
                   "created_at_ms": 1, "updated_at_ms": 1}),
            json!({"id": "a", "tenant_label": "", "name": "a", "definition": {},
                   "created_at_ms": 0, "updated_at_ms": 1}),
            json!({"id": "a", "tenant_label": "", "name": "a", "definition": {},
                   "created_at_ms": 1, "updated_at_ms": 1, "extra": 0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.orchestration.definition.restore",
                payload,
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.orchestration.definition.scan",
                json!({"tenant_label": "", "cursor": Value::Null}),
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
