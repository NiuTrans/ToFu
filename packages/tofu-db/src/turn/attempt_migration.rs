//! Offline terminal Attempt snapshots, independent of the active parent graph.
//! Restores exact source facts and native identity/directory/timing indexes in
//! one transaction. Live reads keep the existing active-conversation fence.

use super::*;
use crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS;
use serde::Deserialize;

const FIELDS: &[(&str, &str)] = &[
    ("attempt_id", "attemptId"),
    ("conversation_id", "conversationId"),
    ("turn_id", "turnId"),
    ("command_id", "commandId"),
    ("task_id", "taskId"),
    ("operation", "operation"),
    ("dispatch_mode", "_dispatchMode"),
    ("queue_id", "_queueId"),
    ("queue_state", "_queueState"),
    ("status", "status"),
    ("base_projection_revision", "baseProjectionRevision"),
    ("resume_anchor", "resumeAnchor"),
    ("config", "_config"),
    ("error", "_error"),
    ("timing_trace", "_timingTrace"),
    ("created_at", "createdAt"),
    ("started_at", "startedAt"),
    ("settled_at", "settledAt"),
    ("superseded_at", "supersededAt"),
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Get {
    conversation_id: String,
    attempt_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    cursor: Option<String>,
}

fn document_from_snapshot(snapshot: &Value) -> io::Result<Map<String, Value>> {
    let snapshot = snapshot
        .as_object()
        .ok_or_else(|| invalid_input("attempt snapshot is not an object"))?;
    if snapshot.len() != FIELDS.len() {
        return Err(invalid_input("attempt snapshot fields differ"));
    }
    let mut document = Map::new();
    for (source, target) in FIELDS {
        let value = snapshot
            .get(*source)
            .ok_or_else(|| invalid_input("attempt snapshot field missing"))?;
        let valid = match *source {
            "resume_anchor" | "config" | "timing_trace" => value.is_object(),
            "error" => true, // Historical errors may be a JSON string; retain the exact value.
            "base_projection_revision" | "created_at" | "settled_at" => value.as_u64().is_some(),
            "started_at" | "superseded_at" => value.is_null() || value.as_u64().is_some(),
            _ => value
                .as_str()
                .is_some_and(|text| !text.contains('\0') && text.chars().count() <= 1024),
        };
        if !valid {
            return Err(invalid_input(
                "attempt snapshot field type or bound differs",
            ));
        }
        document.insert((*target).to_owned(), value.clone());
    }
    for field in [
        "attemptId",
        "conversationId",
        "turnId",
        "commandId",
        "operation",
        "status",
    ] {
        if document[field].as_str().unwrap().is_empty() {
            return Err(invalid_input("attempt snapshot identity is empty"));
        }
    }
    if attempt_is_live(&document) {
        return Err(invalid_input("live attempts must settle before migration"));
    }
    if serde_json::to_vec(&document)?.len()
        > crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES
    {
        return Err(invalid_input("attempt snapshot exceeds document bound"));
    }
    Ok(document)
}

fn snapshot_from_document(document: &Map<String, Value>) -> io::Result<Value> {
    let mut snapshot = Map::new();
    for (source, target) in FIELDS {
        let value = document
            .get(*target)
            .cloned()
            .unwrap_or_else(|| match *source {
                "resume_anchor" | "config" | "error" | "timing_trace" => json!({}),
                "started_at" | "superseded_at" => Value::Null,
                "dispatch_mode" | "queue_id" | "queue_state" => json!(""),
                _ => Value::Null,
            });
        snapshot.insert((*source).to_owned(), value);
    }
    let value = Value::Object(snapshot);
    document_from_snapshot(&value)?;
    Ok(value)
}

fn verify_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let text = |field: &str| document[field].as_str().unwrap();
    let id = text("attemptId");
    let conversation = text("conversationId");
    let turn = text("turnId");
    let claim = database.entity_get(
        tx,
        &global_identity_claim_key(tx, ATTEMPT_ID_CLAIM_NAMESPACE, id)?,
    )?;
    if claim != Some(encode_attempt_locator(tx.owner_user_id(), conversation)?) {
        return Err(invalid_data("attempt snapshot locator differs"));
    }
    if database
        .entity_get(
            tx,
            &attempt_command_key(tx, conversation, text("commandId"))?,
        )?
        .as_deref()
        != Some(id.as_bytes())
    {
        return Err(invalid_data("attempt snapshot command index differs"));
    }
    let directory = load_attempt_turn_directory(database, tx, conversation, turn, None)?;
    let expected = attempt_turn_directory_entry(document)?;
    if directory
        .iter()
        .filter(|entry| entry.get("attemptId").and_then(Value::as_str) == Some(id))
        .collect::<Vec<_>>()
        != vec![&expected]
    {
        return Err(invalid_data("attempt snapshot Turn directory differs"));
    }
    let settled = document["settledAt"].as_u64().unwrap();
    if database.entity_get(tx, &attempt_event_retention_index_key(tx, settled, id)?)?
        != Some(attempt_event_retention_index_value(document, 1)?)
    {
        return Err(invalid_data("attempt snapshot retention index differs"));
    }
    if !text("taskId").is_empty() {
        let expected = attempt_timing_index_value(document)?;
        let at = attempt_timing_effective_at(document)?;
        for key in [
            attempt_timing_task_index_key(tx, text("taskId"), at, id)?,
            attempt_timing_conversation_index_key(
                tx,
                conversation,
                document["createdAt"].as_u64().unwrap(),
                id,
            )?,
        ] {
            if database.entity_get(tx, &key)?.as_deref() != Some(expected.as_slice()) {
                return Err(invalid_data("attempt snapshot timing index differs"));
            }
        }
    }
    if text("status") == "superseded" {
        let at = document["supersededAt"]
            .as_u64()
            .ok_or_else(|| invalid_data("superseded attempt timestamp missing"))?;
        if database
            .entity_get(tx, &attempt_superseded_index_key(tx, at, conversation, id)?)?
            .is_none()
        {
            return Err(invalid_data("attempt snapshot supersession index missing"));
        }
    }
    Ok(())
}

pub(super) fn read_snapshot(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation: &str,
    id: &str,
) -> io::Result<Option<Value>> {
    let Some(stored) = database.entity_get(tx, &attempt_key(tx, conversation, id)?)? else {
        return Ok(None);
    };
    let (_, raw) = crate::versioned_document::materialize_stored_document(
        database,
        tx.tenant_id(),
        tx.owner_user_id(),
        &stored,
        "generation_attempts",
    )?;
    let document: Map<String, Value> = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("attempt snapshot document is malformed"))?;
    let snapshot = snapshot_from_document(&document)?;
    if snapshot["attempt_id"].as_str() != Some(id)
        || snapshot["conversation_id"].as_str() != Some(conversation)
    {
        return Err(invalid_data("attempt snapshot identity differs"));
    }
    verify_indexes(database, tx, &document)?;
    Ok(Some(snapshot))
}

fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let document = document_from_snapshot(&payload)?;
    let id = document["attemptId"].as_str().unwrap();
    let conversation = document["conversationId"].as_str().unwrap();
    if let Some(existing) = read_snapshot(database, tx, conversation, id)? {
        if existing != payload {
            return Err(conflict("attempt snapshot differs"));
        }
        return Ok(false);
    }
    stage_created_attempt(
        database,
        tx,
        &document,
        0,
        document["settledAt"].as_u64().unwrap(),
    )?;
    if document["status"] == "superseded" {
        let at = document["supersededAt"]
            .as_u64()
            .ok_or_else(|| invalid_input("superseded attempt timestamp missing"))?;
        database.entity_put(
            tx,
            attempt_superseded_index_key(tx, at, conversation, id)?,
            Vec::new(),
        )?;
    }
    verify_indexes(database, tx, &document)?;
    Ok(true)
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: Scan = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid attempt scan request"))?;
    let suffix = super::migration::cursor_start("attempts", request.cursor.as_deref())?;
    let start = entity_key(tx, GENERATION_ATTEMPT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        GENERATION_ATTEMPT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (key, stored) in &page {
        let (_, raw) = crate::versioned_document::materialize_stored_document(
            database,
            tx.tenant_id(),
            tx.owner_user_id(),
            stored,
            "generation_attempts",
        )?;
        let document: Map<String, Value> =
            serde_json::from_slice(&raw).map_err(|_| invalid_data("invalid attempt catalog"))?;
        let snapshot = snapshot_from_document(&document)?;
        let id = snapshot["attempt_id"].as_str().unwrap();
        let conversation = snapshot["conversation_id"].as_str().unwrap();
        if *key != attempt_key(tx, conversation, id)? {
            return Err(invalid_data("attempt catalog key differs"));
        }
        verify_indexes(database, tx, &document)?;
        rows.push(json!({"attempt_id":id,"conversation_id":conversation}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let hex: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("attempts:{hex}"))
    } else {
        None
    };
    Ok(json!({"rows":rows,"cursor":cursor}))
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
        "migration.attempt.restore" => {
            let inserted = restore(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.attempt.get" => {
            let request: Get = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid attempt get request"))?;
            Ok(read_snapshot(
                database,
                &mut tx,
                &request.conversation_id,
                &request.attempt_id,
            )?
            .unwrap_or(Value::Null))
        }
        "migration.attempt.scan" => scan(database, &mut tx, payload),
        _ => Err(invalid_input("unknown attempt migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};
    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/attempts")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/attempts"), vfs.clone()).unwrap();
        (vfs, database)
    }
    fn snapshot(id: &str) -> Value {
        json!({"attempt_id":id,"conversation_id":"conv","turn_id":"turn",
            "command_id":format!("command-{id}"),"task_id":format!("task-{id}"),"operation":"generate",
            "dispatch_mode":"conversation_executor","queue_id":"old-queue","queue_state":"dispatched",
            "status":"completed","base_projection_revision":7,"resume_anchor":{"frontier":3},
            "config":{"model":"saved"},"error":{"historical":"kept"},"timing_trace":{"duration":1.5},
            "created_at":100,"started_at":101,"settled_at":200,"superseded_at":null})
    }
    fn import(db: &mut AuthorityDatabase, value: Value) -> io::Result<Value> {
        execute(db, 7, 11, "migration.attempt.restore", value)
    }
    fn point(db: &mut AuthorityDatabase, id: &str) -> io::Result<Value> {
        execute(
            db,
            7,
            11,
            "migration.attempt.get",
            json!({"conversation_id":"conv","attempt_id":id}),
        )
    }
    #[test]
    fn terminal_attempts_reopen_exactly_and_keep_multiple_attempts_per_turn() {
        let (vfs, mut db) = prepared();
        let original = snapshot("a");
        import(&mut db, original.clone()).unwrap();
        import(&mut db, snapshot("b")).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/attempts"), vfs).unwrap();
        assert_eq!(point(&mut db, "a").unwrap(), original);
        assert_eq!(
            import(&mut db, original.clone()).unwrap()["inserted"],
            false
        );
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            load_attempt_identity_ids(&db, &mut tx, "conv", "turn", None).unwrap(),
            vec!["a", "b"]
        );
        assert!(load_attempt_for_update(&db, &mut tx, "a")
            .unwrap()
            .is_none());
        let mut changed = original.clone();
        changed["config"] = json!({"model":"other"});
        assert!(import(&mut db, changed).is_err());
        assert_eq!(point(&mut db, "a").unwrap(), original);
        let mut live = snapshot("live");
        live["status"] = json!("running");
        assert!(import(&mut db, live).is_err());
        assert_eq!(
            execute(
                &mut db,
                7,
                12,
                "migration.attempt.get",
                json!({"conversation_id":"conv","attempt_id":"a"})
            )
            .unwrap(),
            Value::Null
        );
    }
    #[test]
    fn restored_turn_keeps_only_a_verified_terminal_attempt_pointer() {
        let (_, mut db) = prepared();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &db,
            &mut tx,
            &crate::conversation_header::CreateRequest {
                conversation_id: "conv".into(),
                title: "Conversation".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        let turn = json!({"turn_id":"turn","conversation_id":"conv","presentation_id":"","lane_id":"main",
            "parent_turn_id":null,"ordinal":1,"actor":"assistant","kind":"reply","run_id":"",
            "status":"completed","current_attempt_id":"a","projection":{"content":"result"},
            "projection_revision":8,"settlement":{"status":"completed"},"created_at":100,"updated_at":200});
        assert!(super::super::migration::execute(
            &mut db,
            7,
            11,
            "migration.turn.restore",
            turn.clone()
        )
        .is_err());
        import(&mut db, snapshot("a")).unwrap();
        super::super::migration::execute(&mut db, 7, 11, "migration.turn.restore", turn.clone())
            .unwrap();
        let read = super::super::migration::execute(
            &mut db,
            7,
            11,
            "migration.turn.get",
            json!({"id":"turn","conversation_id":"conv"}),
        )
        .unwrap();
        assert_eq!(read, turn);
        let mut other = turn.clone();
        other["turn_id"] = json!("forged-turn");
        other["ordinal"] = json!(2);
        assert!(
            super::super::migration::execute(&mut db, 7, 11, "migration.turn.restore", other)
                .is_err()
        );
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let command = attempt_command_key(&tx, "conv", "command-a").unwrap();
        db.entity_delete(&mut tx, command).unwrap();
        db.commit(tx).unwrap();
        assert!(point(&mut db, "a").is_err());
    }
    #[test]
    fn attempt_catalog_pages_independently_and_validates_cursors() {
        let (_, mut db) = prepared();
        for i in 0..129 {
            let mut value = snapshot(&format!("a{i:03}"));
            value["turn_id"] = json!(format!("turn-{i}"));
            import(&mut db, value).unwrap();
        }
        let first = execute(
            &mut db,
            7,
            11,
            "migration.attempt.scan",
            json!({"cursor":null}),
        )
        .unwrap();
        assert_eq!(first["rows"].as_array().unwrap().len(), 128);
        let second = execute(
            &mut db,
            7,
            11,
            "migration.attempt.scan",
            json!({"cursor":first["cursor"]}),
        )
        .unwrap();
        assert_eq!(second["rows"].as_array().unwrap().len(), 1);
        assert_eq!(second["cursor"], Value::Null);
        assert!(execute(
            &mut db,
            7,
            11,
            "migration.attempt.scan",
            json!({"cursor":"attempts:zz"})
        )
        .is_err());
    }
}
