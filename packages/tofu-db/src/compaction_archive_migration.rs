//! Exact offline root-compaction archive restore and independent enumeration.
//! Entry points are private migration-session operations. Live archive writes
//! keep their own admission; this adapter preserves the source payload count
//! and post-compaction transcript without emitting a new compaction event.

use super::*;
use crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS;
use serde::{Deserialize, Serialize};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    archive_id: String,
    archive_scope: String,
    conversation_id: String,
    messages: Value,
    post_messages: Option<Value>,
    summary: String,
    receipt: Value,
    trigger: String,
    task_id: String,
    round_num: u64,
    model: String,
    tokens_before: u64,
    tokens_after: u64,
    msgs_before: u64,
    msgs_after: u64,
    reason: String,
    payload_size: u64,
    created_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Get {
    archive_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    cursor: Option<String>,
}

fn messages_valid(value: &Value) -> bool {
    value.as_array().is_some_and(|items| {
        items.iter().all(|item| {
            item.as_object().is_some_and(|object| {
                !object.contains_key("_tofuArchivedMessageCodec")
                    && !object.contains_key("_tofuStorageProjectionCodec")
            })
        })
    })
}

fn text_valid(text: &str, maximum: usize, required: bool) -> bool {
    (!required || !text.is_empty()) && !text.contains('\0') && text.chars().count() <= maximum
}

fn read_snapshot(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Value>> {
    let Some((core, _)) = load_value(database, tx, COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE, id)?
    else {
        return Ok(None);
    };
    let mut core = core_object(core)?;
    let conversation = string_field(&core, "conversation_id")?.to_owned();
    if string_field(&core, "archive_id")? != id {
        return Err(invalid_data("archive snapshot identity differs"));
    }
    let claim = database
        .entity_get(tx, &global_claim_key(tx, id)?)?
        .ok_or_else(|| invalid_data("archive snapshot claim missing"))?;
    let archive_scope = archive_scope_of(&core)?;
    if !claim_matches(&claim, tx.owner_user_id(), &archive_scope, &conversation) {
        return Err(invalid_data("archive snapshot claim differs"));
    }
    let index = index_key(
        tx,
        &conversation,
        &archive_scope,
        u64_field(&core, "created_at_ms")?,
        id,
    )?;
    if database.entity_get(tx, &index)?.as_deref() != Some(id.as_bytes()) {
        return Err(invalid_data("archive snapshot chronological index differs"));
    }
    scan_index(database, tx, &conversation, &archive_scope)?;
    for (field, namespace) in [
        ("messages", COMPACTION_ARCHIVE_MESSAGES_NAMESPACE),
        ("summary", COMPACTION_ARCHIVE_SUMMARY_NAMESPACE),
        ("receipt", COMPACTION_ARCHIVE_RECEIPT_NAMESPACE),
    ] {
        let (value, _) = load_value(database, tx, namespace, id)?
            .ok_or_else(|| invalid_data("archive snapshot part missing"))?;
        core.insert(field.to_owned(), value);
    }
    let post = load_value(database, tx, COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE, id)?
        .map(|(value, _)| value)
        .unwrap_or(Value::Null);
    core.insert("post_messages".to_owned(), post);
    core.insert("archive_scope".to_owned(), Value::String(String::new()));
    Ok(Some(Value::Object(core)))
}

fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: Snapshot = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid compaction archive snapshot"))?;
    if !request.archive_scope.is_empty() {
        return Err(invalid_input(
            "child archive migration requires its invocation adapter",
        ));
    }
    if !text_valid(&request.archive_id, 128, true)
        || !text_valid(&request.conversation_id, 256, true)
        || !text_valid(&request.summary, 200_000, false)
        || !text_valid(&request.trigger, 32, true)
        || !text_valid(&request.task_id, 512, false)
        || !text_valid(&request.model, 256, false)
        || !text_valid(&request.reason, 500, false)
        || !messages_valid(&request.messages)
        || !request.receipt.is_object()
        || request
            .post_messages
            .as_ref()
            .is_some_and(|p| !messages_valid(p))
    {
        return Err(invalid_input("invalid compaction archive snapshot fields"));
    }
    let snapshot = serde_json::to_value(&request)?;
    if let Some(existing) = read_snapshot(database, tx, &request.archive_id)? {
        if existing != snapshot {
            return Err(conflict("archive snapshot differs"));
        }
        return Ok(false);
    }
    let post = request
        .post_messages
        .as_ref()
        .map(serde_json::to_vec)
        .transpose()?;
    if post
        .as_ref()
        .is_some_and(|bytes| bytes.len() > MAX_TRANSACTION_IR_LITERAL_BYTES)
    {
        return Err(invalid_input("archive post messages exceed bound"));
    }
    create_contents(
        database,
        tx,
        &CreateRequest {
            archive_id: request.archive_id.clone(),
            conversation_id: request.conversation_id,
            // The migration lane admits root archives only; scoped child
            // archives fail closed at admission.
            archive_scope: String::new(),
            messages_json: serde_json::to_vec(&request.messages)?,
            summary: request.summary,
            receipt_json: serde_json::to_vec(&request.receipt)?,
            trigger: request.trigger,
            task_id: request.task_id,
            round_num: request.round_num,
            model: request.model,
            tokens_before: request.tokens_before,
            tokens_after: request.tokens_after,
            msgs_before: request.msgs_before,
            msgs_after: request.msgs_after,
            reason: request.reason,
            created_at_ms: request.created_at_ms,
            committed_at_ms: request.created_at_ms,
        },
    )?;
    let (mut core, version) = load_value(
        database,
        tx,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        &request.archive_id,
    )?
    .unwrap();
    core["payload_size"] = json!(request.payload_size);
    put_value(
        database,
        tx,
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        &request.archive_id,
        &core,
        version,
        request.created_at_ms,
    )?;
    if let Some(value) = request.post_messages {
        put_value(
            database,
            tx,
            COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE,
            &request.archive_id,
            &value,
            0,
            request.created_at_ms,
        )?;
    }
    if read_snapshot(database, tx, &request.archive_id)? != Some(snapshot) {
        return Err(invalid_data("restored archive snapshot differs"));
    }
    Ok(true)
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: Scan = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid archive scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor {
        let id = cursor
            .strip_prefix("compaction_archives:")
            .filter(|id| text_valid(id, 128, true))
            .ok_or_else(|| invalid_input("invalid archive cursor"))?;
        start.extend_from_slice(id.as_bytes());
        start.push(0);
    }
    let start = key(tx, COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE, &start)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("invalid archive catalog identity"))?;
        if read_snapshot(database, tx, id)?.is_none() {
            return Err(invalid_data("archive catalog snapshot missing"));
        }
        rows.push(json!({"archive_id": id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        rows.last()
            .and_then(|row| row["archive_id"].as_str())
            .map(|id| format!("compaction_archives:{id}"))
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
        "migration.compaction.archive.restore" => {
            let inserted = restore(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.compaction.archive.get" => {
            let request: Get = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid archive get request"))?;
            Ok(read_snapshot(database, &mut tx, &request.archive_id)?.unwrap_or(Value::Null))
        }
        "migration.compaction.archive.scan" => scan(database, &mut tx, payload),
        _ => Err(invalid_input(
            "unknown compaction archive migration operation",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/archives")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/archives"), vfs.clone()).unwrap();
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut tx,
            &crate::conversation_header::CreateRequest {
                conversation_id: "conv".into(),
                title: "History".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(tx).unwrap();
        (vfs, database)
    }
    fn snapshot(id: &str) -> Value {
        json!({"archive_id":id,"archive_scope":"","conversation_id":"conv",
            "messages":[{"role":"user","content":"历史消息"}],
            "post_messages":[{"role":"assistant","content":"摘要"}],
            "summary":"compressed summary","receipt":{"status":"complete","count":7},
            "trigger":"force","task_id":"task","round_num":3,"model":"test-model",
            "tokens_before":800,"tokens_after":20,"msgs_before":10,"msgs_after":2,
            "reason":"budget","payload_size":17,"created_at_ms":1234})
    }
    fn import(database: &mut AuthorityDatabase, value: Value) -> io::Result<Value> {
        execute(
            database,
            7,
            11,
            "migration.compaction.archive.restore",
            value,
        )
    }
    fn get(database: &mut AuthorityDatabase, id: &str) -> io::Result<Value> {
        execute(
            database,
            7,
            11,
            "migration.compaction.archive.get",
            json!({"archive_id":id}),
        )
    }
    #[test]
    fn archive_snapshot_reopens_exactly_and_conflicts_without_mutation() {
        let (vfs, mut database) = prepared();
        let value = snapshot("a");
        assert_eq!(
            import(&mut database, value.clone()).unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/archives"), vfs).unwrap();
        assert_eq!(get(&mut database, "a").unwrap(), value);
        assert_eq!(
            import(&mut database, value.clone()).unwrap()["inserted"],
            false
        );
        for field in [
            "receipt",
            "messages",
            "post_messages",
            "summary",
            "payload_size",
            "created_at_ms",
        ] {
            let mut changed = value.clone();
            changed[field] = match field {
                "receipt" => json!({"status":"other"}),
                "messages" | "post_messages" => json!([]),
                "summary" => json!("other"),
                _ => json!(999),
            };
            assert!(import(&mut database, changed).is_err());
            assert_eq!(get(&mut database, "a").unwrap(), value);
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.compaction.archive.get",
                json!({"archive_id":"a"})
            )
            .unwrap(),
            Value::Null
        );
        assert!(execute(
            &mut database,
            7,
            12,
            "migration.compaction.archive.restore",
            value
        )
        .is_err());
    }
    #[test]
    fn archive_snapshot_rejects_unknown_scope_and_checks_derived_indexes() {
        let (_, mut database) = prepared();
        let mut value = snapshot("a");
        value["archive_scope"] = json!("child");
        assert!(import(&mut database, value).is_err());
        assert_eq!(get(&mut database, "a").unwrap(), Value::Null);
        import(&mut database, snapshot("a")).unwrap();
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        database
            .entity_delete(
                &mut tx,
                index_key(&database.begin(7, 11).unwrap(), "conv", "", 1234, "a").unwrap(),
            )
            .unwrap();
        database.commit(tx).unwrap();
        assert!(get(&mut database, "a").is_err());
        assert!(import(&mut database, snapshot("a")).is_err());
    }
    #[test]
    fn detached_archive_is_preserved_but_live_reads_require_parent() {
        let (_, mut database) = prepared();
        let mut value = snapshot("orphan");
        value["conversation_id"] = json!("deleted-parent");
        import(&mut database, value.clone()).unwrap();
        assert_eq!(get(&mut database, "orphan").unwrap(), value);
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(super::super::get(
            &database,
            &mut tx,
            &GetRequest {
                conversation_id: "deleted-parent".into(),
                archive_id: "orphan".into(),
                archive_scope: String::new(),
                include_messages: true,
                include_post_messages: true,
            }
        )
        .is_err());
        crate::conversation_header::create(
            &database,
            &mut tx,
            &crate::conversation_header::CreateRequest {
                conversation_id: "deleted-parent".into(),
                title: "Restored".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(tx).unwrap();
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let visible = super::super::get(
            &database,
            &mut tx,
            &GetRequest {
                conversation_id: "deleted-parent".into(),
                archive_id: "orphan".into(),
                archive_scope: String::new(),
                include_messages: true,
                include_post_messages: true,
            },
        )
        .unwrap()
        .unwrap();
        let visible: Value = serde_json::from_slice(&visible).unwrap();
        assert_eq!(visible["messages"], value["messages"]);
        assert_eq!(visible["archive"]["payloadSize"], value["payload_size"]);
    }

    #[test]
    fn archive_catalog_has_bounded_independent_continuation() {
        let (_, mut database) = prepared();
        for i in 0..129 {
            import(&mut database, snapshot(&format!("a{i:03}"))).unwrap();
        }
        let first = execute(
            &mut database,
            7,
            11,
            "migration.compaction.archive.scan",
            json!({"cursor":null}),
        )
        .unwrap();
        assert_eq!(first["rows"].as_array().unwrap().len(), 128);
        let second = execute(
            &mut database,
            7,
            11,
            "migration.compaction.archive.scan",
            json!({"cursor":first["cursor"]}),
        )
        .unwrap();
        assert_eq!(second["rows"], json!([{"archive_id":"a128"}]));
        assert_eq!(second["cursor"], Value::Null);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.compaction.archive.scan",
            json!({"cursor":"wrong:a"})
        )
        .is_err());
    }
}
