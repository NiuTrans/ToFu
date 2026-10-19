//! Offline integration workspace/event snapshot restoration for the exclusive
//! operator importer.
//!
//! The source keeps one `integration_workspaces` row per task, a separate
//! `integration_workspace_meta` row carrying the merged origin document, and a
//! bounded `integration_events` history per project. The native authority
//! stores the workspace as a tenant-global document keyed by the natural
//! `(owner, project_root, task_id)` triple, claims that identity and the row
//! locator, and maintains the ready/integrating queue indexes, the per-project
//! updated index, the active-workspace count, the per-owner workspace count and
//! the row/event sequences. The live operations allocate the row id from the
//! sequence and stamp `updated_at` with the worker clock, so a replay through
//! them could not preserve the exact source id or timestamps. This adapter
//! rebuilds the native document, claims, indexes, counts and sequences in the
//! same authority transaction, preserving the source id and timestamps
//! verbatim and folding the meta origin into the workspace document exactly as
//! `integration.workspace.set_meta` does. Insert-or-match keeps replays
//! idempotent and fails closed on a divergent or foreign occupant; get
//! reconstructs the exact source snapshot and scan enumerates the
//! owner-scoped catalog independently. Entry points: the
//! `migration.integration.{workspace,event,meta}.*` operations dispatched from
//! `migration_session`.

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
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "integration snapshot already differs",
    )
}
fn occupied() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "integration identity already occupies the target",
    )
}

const WORKSPACE_STATES: [&str; 8] = [
    "running",
    "checkpointed",
    "ready",
    "integrating",
    "quarantined",
    "failed",
    "merged",
    "discarded",
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkspaceRestore {
    id: u64,
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
    origin: Value,
    created_at: f64,
    updated_at: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MetaRestore {
    project_root: String,
    task_id: String,
    origin: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventRestore {
    id: u64,
    project_root: String,
    task_id: String,
    kind: String,
    message: String,
    detail: String,
    created_at: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkspaceIdentity {
    project_root: String,
    task_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventIdentity {
    id: u64,
    project_root: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventScan {
    project_root: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogScan {
    cursor: Option<String>,
}

fn valid_identity(project_root: &str, task_id: &str) -> bool {
    valid_text(project_root, MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS)
        && valid_text(task_id, MAX_INTEGRATION_TASK_ID_CHARACTERS)
}

fn valid_f64(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

fn valid_workspace_restore(request: &WorkspaceRestore) -> bool {
    request.id > 0
        && valid_identity(&request.project_root, &request.task_id)
        && request.title.chars().count() <= 1000
        && valid_text(&request.workspace_path, 4096)
        && WORKSPACE_STATES.contains(&request.state.as_str())
        && request.base_sha.chars().count() <= 200
        && request.checkpoint_sha.chars().count() <= 200
        && request.candidate_sha.chars().count() <= 200
        && request.error.chars().count() <= 4000
        && request.origin.is_object()
        && valid_f64(request.created_at)
        && valid_f64(request.updated_at)
        && request.created_at > 0.0
}

fn origin_raw_from_value(origin: &Value) -> io::Result<String> {
    serde_json::to_string(origin)
        .map_err(|_| migration_invalid("integration origin cannot be encoded"))
}

fn workspace_from_restore(request: &WorkspaceRestore, owner: u64) -> io::Result<Workspace> {
    Ok(Workspace {
        id: request.id,
        owner_user_id: owner,
        project_root: request.project_root.clone(),
        task_id: request.task_id.clone(),
        title: request.title.clone(),
        workspace_path: request.workspace_path.clone(),
        managed: request.managed,
        state: request.state.clone(),
        base_sha: request.base_sha.clone(),
        checkpoint_sha: request.checkpoint_sha.clone(),
        candidate_sha: request.candidate_sha.clone(),
        error: request.error.clone(),
        origin_raw: origin_raw_from_value(&request.origin)?,
        created_at: request.created_at,
        updated_at: request.updated_at,
    })
}

fn workspace_snapshot(workspace: &Workspace) -> Value {
    json!({
        "id": workspace.id,
        "project_root": workspace.project_root,
        "task_id": workspace.task_id,
        "title": workspace.title,
        "workspace_path": workspace.workspace_path,
        "managed": workspace.managed,
        "state": workspace.state,
        "base_sha": workspace.base_sha,
        "checkpoint_sha": workspace.checkpoint_sha,
        "candidate_sha": workspace.candidate_sha,
        "error": workspace.error,
        "origin": origin_document(&workspace.origin_raw),
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
    })
}

fn restore_workspace(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: WorkspaceRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration workspace restore request"))?;
    if !valid_workspace_restore(&request) {
        return Err(migration_invalid(
            "invalid integration workspace restore request",
        ));
    }
    let owner = tx.owner_user_id();
    if let Some(raw) = database.entity_get(
        tx,
        &natural_claim_key(tx, owner, &request.project_root, &request.task_id)?,
    )? {
        if raw.len() != 8 {
            return Err(invalid_data("integration natural claim is malformed"));
        }
        let claimed = u64::from_be_bytes(raw.try_into().unwrap());
        if claimed != request.id {
            return Err(occupied());
        }
    } else if database
        .entity_get(
            tx,
            &workspace_key(tx, owner, &request.project_root, &request.task_id)?,
        )?
        .is_some()
    {
        return Err(occupied());
    }
    let workspace = workspace_from_restore(&request, owner)?;
    if let Some(existing) =
        read_workspace(database, tx, owner, &request.project_root, &request.task_id)?
    {
        if workspace_snapshot(&existing) != workspace_snapshot(&workspace) {
            return Err(snapshot_conflict());
        }
        verify_workspace_indexes(database, tx, &existing)?;
        return Ok(false);
    }
    // A workspace id must never collide with a live allocation: advance the
    // tenant-global row sequence past the restored id.
    if let Some(existing) = read_workspace_by_id(database, tx, request.id)? {
        let _ = existing;
        return Err(occupied());
    }
    let count_key = owner_count_key(tx, owner)?;
    let count = read_u64(database, tx, &count_key, "integration owner count")?;
    let next_count = count.checked_add(1)
        .ok_or_else(|| invalid_data("integration owner count overflow"))?;
    write_workspace(database, tx, None, &workspace)?;
    database.entity_put(
        tx,
        natural_claim_key(tx, owner, &request.project_root, &request.task_id)?,
        request.id.to_be_bytes().to_vec(),
    )?;
    database.entity_put(
        tx,
        row_locator_key(tx, request.id)?,
        encode(&locator(&workspace), "integration locator")?,
    )?;
    write_u64(database, tx, count_key, next_count)?;
    advance_sequence_past(database, tx, INTEGRATION_ROW_SEQUENCE_NAMESPACE, request.id)?;
    let stored = read_workspace(database, tx, owner, &request.project_root, &request.task_id)?
        .ok_or_else(|| invalid_data("integration workspace is missing"))?;
    if workspace_snapshot(&stored) != workspace_snapshot(&workspace) {
        return Err(invalid_data("integration workspace readback differs"));
    }
    verify_workspace_indexes(database, tx, &stored)?;
    Ok(true)
}

fn advance_sequence_past(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    floor: u64,
) -> io::Result<()> {
    let key = sequence_key(tx, namespace)?;
    let current = read_u64(database, tx, &key, "integration sequence")?;
    if current < floor {
        write_u64(database, tx, key, floor)?;
    }
    Ok(())
}

fn verify_workspace_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    workspace: &Workspace,
) -> io::Result<()> {
    let encoded_locator = encode(&locator(workspace), "integration locator")?;
    let updated_key = project_updated_key(tx, workspace)?;
    expect_locator_index(database, tx, &updated_key, true, &encoded_locator)?;
    let ready_key = queue_index_key(tx, INTEGRATION_READY_INDEX_NAMESPACE, workspace)?;
    expect_locator_index(
        database,
        tx,
        &ready_key,
        workspace.state == "ready",
        &encoded_locator,
    )?;
    let integrating_key = queue_index_key(tx, INTEGRATION_INTEGRATING_INDEX_NAMESPACE, workspace)?;
    expect_locator_index(
        database,
        tx,
        &integrating_key,
        workspace.state == "integrating",
        &encoded_locator,
    )?;
    // The integrating project claim points at this workspace id.
    let claim_key = project_claim_key(tx, &workspace.project_root)?;
    let claim = database.entity_get(tx, &claim_key)?;
    let expected_claim = if workspace.state == "integrating" {
        Some(workspace.id.to_be_bytes().to_vec())
    } else {
        None
    };
    match (claim, expected_claim) {
        (Some(raw), Some(expected)) if raw == expected => {}
        (None, None) => {}
        (Some(_), None) => {
            // A foreign live integrating claim must not be disturbed.
            return Err(invalid_data("integration project claim differs"));
        }
        (None, Some(_)) => {
            return Err(invalid_data("integration project claim is missing"));
        }
        _ => return Err(invalid_data("integration project claim differs")),
    }
    // The natural claim and row locator must resolve back to this workspace.
    let natural_key = natural_claim_key(
        tx,
        workspace.owner_user_id,
        &workspace.project_root,
        &workspace.task_id,
    )?;
    let natural = database.entity_get(tx, &natural_key)?;
    if natural.as_deref() != Some(workspace.id.to_be_bytes().as_slice()) {
        return Err(invalid_data("integration natural claim differs"));
    }
    Ok(())
}

fn expect_locator_index(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    present: bool,
    encoded_locator: &[u8],
) -> io::Result<()> {
    match (database.entity_get(tx, key)?, present) {
        (Some(raw), true) if raw == encoded_locator => Ok(()),
        (None, false) => Ok(()),
        _ => Err(invalid_data("integration workspace index differs")),
    }
}

fn get_workspace(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: WorkspaceIdentity = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration workspace get request"))?;
    if !valid_identity(&request.project_root, &request.task_id) {
        return Err(migration_invalid("invalid integration workspace identity"));
    }
    let Some(workspace) = read_workspace(
        database,
        tx,
        tx.owner_user_id(),
        &request.project_root,
        &request.task_id,
    )?
    else {
        return Ok(Value::Null);
    };
    Ok(workspace_snapshot(&workspace))
}

fn restore_meta(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: MetaRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration meta restore request"))?;
    if !valid_identity(&request.project_root, &request.task_id) || !request.origin.is_object() {
        return Err(migration_invalid(
            "invalid integration meta restore request",
        ));
    }
    let owner = tx.owner_user_id();
    let Some(mut workspace) =
        read_workspace(database, tx, owner, &request.project_root, &request.task_id)?
    else {
        return Err(migration_invalid(
            "integration meta workspace is not restored",
        ));
    };
    // Fold the meta origin into the workspace document exactly as the live
    // set_meta path merges a patch into the existing origin.
    let mut origin = origin_document(&workspace.origin_raw);
    let origin_object = origin
        .as_object_mut()
        .ok_or_else(|| invalid_data("integration origin is not an object"))?;
    let patch = request
        .origin
        .as_object()
        .ok_or_else(|| migration_invalid("invalid integration meta origin"))?;
    for (key, value) in patch {
        origin_object.insert(key.clone(), value.clone());
    }
    let new_raw = origin_raw_from_value(&origin)?;
    if new_raw == workspace.origin_raw {
        return Ok(false);
    }
    let old = workspace.clone();
    workspace.origin_raw = new_raw;
    // Meta updates intentionally do not advance the workspace queue clock.
    write_workspace(database, tx, Some(&old), &workspace)?;
    Ok(true)
}

fn restore_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: EventRestore = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration event restore request"))?;
    if request.id == 0
        || !valid_identity(&request.project_root, &request.task_id)
        || request.kind.is_empty()
        || request.kind.chars().count() > 128
        || !valid_f64(request.created_at)
    {
        return Err(migration_invalid(
            "invalid integration event restore request",
        ));
    }
    let owner = tx.owner_user_id();
    let event = IntegrationEvent {
        id: request.id,
        owner_user_id: owner,
        project_root: request.project_root.clone(),
        task_id: request.task_id.clone(),
        kind: request.kind.clone(),
        message: request.message.chars().take(500).collect(),
        detail: request.detail.chars().take(4000).collect(),
        created_at: request.created_at,
    };
    let key = event_key(tx, owner, &request.project_root, request.id)?;
    if let Some(raw) = database.entity_get(tx, &key)? {
        let existing: IntegrationEvent = decode(&raw, "integration event")?;
        if existing.id != event.id
            || existing.owner_user_id != event.owner_user_id
            || existing.project_root != event.project_root
            || existing.task_id != event.task_id
            || existing.kind != event.kind
            || existing.message != event.message
            || existing.detail != event.detail
            || existing.created_at != event.created_at
        {
            return Err(snapshot_conflict());
        }
        return Ok(false);
    }
    database.entity_put(tx, key, encode(&event, "integration event")?)?;
    let count_key = event_count_key(tx, owner, &request.project_root)?;
    let count = read_u64(database, tx, &count_key, "integration event count")?;
    write_u64(database, tx, count_key, count.checked_add(1)
        .ok_or_else(|| invalid_data("integration owner count overflow"))?)?;
    advance_sequence_past(
        database,
        tx,
        INTEGRATION_EVENT_SEQUENCE_NAMESPACE,
        request.id,
    )?;
    Ok(true)
}

fn get_event(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: EventIdentity = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration event get request"))?;
    if request.id == 0
        || !valid_text(
            &request.project_root,
            MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS,
        )
    {
        return Err(migration_invalid("invalid integration event identity"));
    }
    let owner = tx.owner_user_id();
    let key = event_key(tx, owner, &request.project_root, request.id)?;
    let Some(raw) = database.entity_get(tx, &key)? else {
        return Ok(Value::Null);
    };
    let event: IntegrationEvent = decode(&raw, "integration event")?;
    if event.id != request.id
        || event.owner_user_id != owner
        || event.project_root != request.project_root
    {
        return Err(invalid_data("integration event identity differs"));
    }
    Ok(event_projection(&event))
}

fn scan_events(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: EventScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration event scan request"))?;
    if !valid_text(
        &request.project_root,
        MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS,
    ) {
        return Err(migration_invalid("invalid integration event scan request"));
    }
    let owner = tx.owner_user_id();
    let (start, end) = event_range(tx, owner, &request.project_root)?;
    let page = database.entity_scan(tx, &start, &end, MAX_INTEGRATION_EVENTS_PER_PROJECT + 1)?;
    if page.len() > MAX_INTEGRATION_EVENTS_PER_PROJECT {
        return Err(invalid_data("integration event history exceeds its bound"));
    }
    let mut rows = Vec::with_capacity(page.len());
    for (_, raw) in &page {
        let event: IntegrationEvent = decode(raw, "integration event")?;
        if event.owner_user_id != owner || event.project_root != request.project_root {
            return Err(invalid_data("integration event catalog identity differs"));
        }
        rows.push(event_projection(&event));
    }
    let count = read_u64(
        database,
        tx,
        &event_count_key(tx, owner, &request.project_root)?,
        "integration event count",
    )?;
    if rows.len() as u64 != count {
        return Err(invalid_data("integration event count and catalog differ"));
    }
    Ok(json!({"rows": rows, "cursor": Value::Null}))
}

fn cursor_start(table: &str, cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let Some(encoded) = cursor.strip_prefix(table) else {
        return Err(migration_invalid("invalid integration catalog cursor"));
    };
    let mut raw = encoded.as_bytes().to_vec();
    if raw.is_empty() || raw.len() > MAX_ENTITY_KEY_BYTES {
        return Err(migration_invalid("invalid integration catalog cursor"));
    }
    raw.push(0);
    Ok(raw)
}

fn scan_workspaces(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: CatalogScan = serde_json::from_value(payload)
        .map_err(|_| migration_invalid("invalid integration workspace scan request"))?;
    let owner = tx.owner_user_id();
    let suffix = cursor_start("integration_workspace:", request.cursor.as_deref())?;
    // The owner workspace catalog is enumerated over the natural-identity
    // prefix `<owner><project_root><task_id>` in the workspace namespace.
    let mut prefix = owner.to_be_bytes().to_vec();
    prefix.extend_from_slice(&suffix);
    let start = global_key(tx, INTEGRATION_WORKSPACE_NAMESPACE, &prefix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        INTEGRATION_WORKSPACE_NAMESPACE,
        &owner.to_be_bytes(),
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    let mut last_raw = None;
    for (physical, raw) in &page {
        let workspace: Workspace = decode(raw, "integration workspace")?;
        if workspace.owner_user_id != owner {
            return Err(invalid_data("integration workspace catalog owner differs"));
        }
        if physical != &workspace_key(tx, owner, &workspace.project_root, &workspace.task_id)? {
            return Err(invalid_data(
                "integration workspace catalog identity differs",
            ));
        }
        verify_workspace_indexes(database, tx, &workspace)?;
        rows.push(json!({
            "project_root": workspace.project_root,
            "task_id": workspace.task_id,
        }));
        last_raw = Some(physical.key_bytes().to_vec());
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = last_raw
            .ok_or_else(|| invalid_data("integration workspace catalog page is malformed"))?;
        // Strip the leading 8-byte owner prefix to form the resume cursor.
        let suffix = last
            .get(8..)
            .ok_or_else(|| invalid_data("integration workspace catalog page is malformed"))?;
        let text = std::str::from_utf8(suffix)
            .map_err(|_| invalid_data("integration workspace catalog page is malformed"))?;
        Some(format!("integration_workspace:{text}"))
    } else {
        None
    };
    // Count self-proof is only sound for a complete single-page catalog;
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none() {
        if cursor.is_none() {
            let count = read_u64(
                database,
                tx,
                &owner_count_key(tx, owner)?,
                "integration owner count",
            )?;
            if rows.len() as u64 != count {
                return Err(invalid_data(
                    "integration workspace count and catalog differ",
                ));
            }
        }
    }
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
        "migration.integration.workspace.restore" => {
            let inserted = restore_workspace(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.integration.workspace.get" => get_workspace(database, &mut tx, payload),
        "migration.integration.workspace.scan" => scan_workspaces(database, &mut tx, payload),
        "migration.integration.meta.restore" => {
            let inserted = restore_meta(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.integration.event.restore" => {
            let inserted = restore_event(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.integration.event.get" => get_event(database, &mut tx, payload),
        "migration.integration.event.scan" => scan_events(database, &mut tx, payload),
        _ => Err(migration_invalid("unknown integration migration operation")),
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
        vfs.create_dir(Path::new("/integ")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/integ"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn workspace(id: u64, task: &str, state: &str, updated: f64) -> Value {
        json!({
            "id": id,
            "project_root": "/repo",
            "task_id": task,
            "title": format!("title-{task}"),
            "workspace_path": format!("/repo/.worktrees/{id}-{task}"),
            "managed": true,
            "state": state,
            "base_sha": "base",
            "checkpoint_sha": "",
            "candidate_sha": "",
            "error": "",
            "origin": {"source": "writer"},
            "created_at": 100.0,
            "updated_at": updated,
        })
    }

    #[test]
    fn workspace_restore_preserves_exact_state_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.integration.workspace.restore",
                workspace(1, "task-a", "ready", 200.0),
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/integ"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.integration.workspace.restore",
                workspace(1, "task-a", "ready", 200.0),
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.integration.workspace.get",
            json!({"project_root": "/repo", "task_id": "task-a"}),
        )
        .unwrap();
        assert_eq!(get["id"], 1);
        assert_eq!(get["state"], "ready");
        assert_eq!(get["updated_at"], 200.0);
        assert_eq!(get["origin"], json!({"source": "writer"}));
        // A divergent occupant fails closed.
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.integration.workspace.restore",
            workspace(1, "task-a", "failed", 200.0),
        )
        .is_err());
    }

    #[test]
    fn workspace_catalog_agrees_with_count_and_is_owner_scoped() {
        let (_vfs, mut database) = prepared();
        for (id, task) in [(1, "task-a"), (2, "task-b"), (3, "task-c")] {
            execute(
                &mut database,
                7,
                11,
                "migration.integration.workspace.restore",
                workspace(id, task, "running", 100.0 + id as f64),
            )
            .unwrap();
        }
        let scan = execute(
            &mut database,
            7,
            11,
            "migration.integration.workspace.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(scan["rows"].as_array().unwrap().len(), 3);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.integration.workspace.scan",
                json!({"cursor": Value::Null}),
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn meta_restore_merges_origin_without_queue_clock_advance() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.integration.workspace.restore",
            workspace(1, "task-a", "ready", 200.0),
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.integration.meta.restore",
                json!({"project_root": "/repo", "task_id": "task-a",
                       "origin": {"extra": "field"}}),
            )
            .unwrap()["inserted"],
            true
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.integration.workspace.get",
            json!({"project_root": "/repo", "task_id": "task-a"}),
        )
        .unwrap();
        assert_eq!(get["origin"], json!({"source": "writer", "extra": "field"}));
        // The queue clock is not advanced by a meta merge.
        assert_eq!(get["updated_at"], 200.0);
    }

    #[test]
    fn event_restore_advances_sequence_and_catalog_matches_count() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.integration.workspace.restore",
            workspace(1, "task-a", "running", 100.0),
        )
        .unwrap();
        for id in 1..=3u64 {
            assert_eq!(
                execute(
                    &mut database,
                    7,
                    11,
                    "migration.integration.event.restore",
                    json!({"id": id, "project_root": "/repo", "task_id": "task-a",
                           "kind": "registered", "message": "m", "detail": "d",
                           "created_at": 100.0 + id as f64}),
                )
                .unwrap()["inserted"],
                true
            );
        }
        let scan = execute(
            &mut database,
            7,
            11,
            "migration.integration.event.scan",
            json!({"project_root": "/repo"}),
        )
        .unwrap();
        assert_eq!(scan["rows"].as_array().unwrap().len(), 3);
        let get = execute(
            &mut database,
            7,
            11,
            "migration.integration.event.get",
            json!({"id": 2, "project_root": "/repo"}),
        )
        .unwrap();
        assert_eq!(get["id"], 2);
        assert_eq!(get["kind"], "registered");
    }

    #[test]
    fn invalid_payloads_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"id": 0, "project_root": "/r", "task_id": "t", "title": "",
                   "workspace_path": "/w", "managed": true, "state": "running",
                   "base_sha": "", "checkpoint_sha": "", "candidate_sha": "", "error": "",
                   "origin": {}, "created_at": 1.0, "updated_at": 1.0}),
            json!({"id": 1, "project_root": "/r", "task_id": "t", "title": "",
                   "workspace_path": "/w", "managed": true, "state": "bogus",
                   "base_sha": "", "checkpoint_sha": "", "candidate_sha": "", "error": "",
                   "origin": {}, "created_at": 1.0, "updated_at": 1.0}),
            json!({"id": 1, "project_root": "/r", "task_id": "t", "title": "",
                   "workspace_path": "/w", "managed": true, "state": "running",
                   "base_sha": "", "checkpoint_sha": "", "candidate_sha": "", "error": "",
                   "origin": {}, "created_at": 1.0, "updated_at": 1.0, "extra": 0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.integration.workspace.restore",
                payload,
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.integration.workspace.scan",
                json!({"cursor": Value::Null}),
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
