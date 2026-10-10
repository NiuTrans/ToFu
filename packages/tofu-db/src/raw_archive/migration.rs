//! Private offline archive snapshots and independent catalog/statistics reads.
//! Publication reuses the live atomic index writer, preserving exact compressed
//! bytes and detached owner history while refusing quota-driven content loss.

use super::*;
use crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS;

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
#[derive(Deserialize)]
#[serde(deny_unknown_fields, tag = "scope", rename_all = "snake_case")]
enum Stats {
    Owner,
    Tenant,
    Conversation { conversation_id: String },
    Attempt { attempt_id: String },
}

fn request(document: &ArchiveDocument) -> io::Result<Request> {
    let decode = |text: &str| {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(text)
            .map_err(|_| invalid_input("raw archive snapshot has invalid base64"))?;
        if base64::engine::general_purpose::STANDARD.encode(&bytes) != text {
            return Err(invalid_input(
                "raw archive snapshot base64 is not canonical",
            ));
        }
        Ok(bytes)
    };
    let request_blob = decode(&document.request_blob_b64)?;
    let response_blob = decode(&document.response_blob_b64)?;
    if document.stored_bytes != (request_blob.len() + response_blob.len()) as u64 {
        return Err(invalid_input(
            "raw archive snapshot stored byte count differs",
        ));
    }
    let request = Request::Put {
        archive_id: document.archive_id.clone(),
        conversation_id: document.conversation_id.clone(),
        turn_id: document.turn_id.clone(),
        attempt_id: document.attempt_id.clone(),
        task_id: document.task_id.clone(),
        round_num: document.round_num,
        transport_attempt: document.transport_attempt,
        request_blob,
        response_blob,
        request_bytes: document.request_bytes,
        response_bytes: document.response_bytes,
        request_sha256: document.request_sha256.clone(),
        response_sha256: document.response_sha256.clone(),
        integrity: document.integrity.clone(),
        truncation_reason: document.truncation_reason.clone(),
        summary: document.summary.clone(),
        budget_bytes: MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES,
        min_free_bytes: 0,
        available_free_bytes: 0,
        created_at_ms: document.created_at_ms,
    };
    request.validate()?;
    Ok(request)
}

fn verify_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    doc: &ArchiveDocument,
) -> io::Result<()> {
    let metadata = serde_json::to_vec(&doc.public_value(None))?;
    let removal = serde_json::to_vec(&RemovalRecord {
        archive_id: doc.archive_id.clone(),
        task_id: doc.task_id.clone(),
        attempt_id: doc.attempt_id.clone(),
        round_num: doc.round_num,
        created_at_ms: doc.created_at_ms,
        stored_bytes: doc.stored_bytes,
    })?;
    for (namespace, prefix, expected) in [
        (
            RAW_ARCHIVE_TASK_INDEX_NAMESPACE,
            index_prefix(&doc.task_id, None)?,
            metadata.clone(),
        ),
        (
            RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE,
            index_prefix(&doc.task_id, Some(doc.round_num))?,
            metadata,
        ),
        (
            RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE,
            attempt_index_prefix(&doc.attempt_id)?,
            doc.archive_id.as_bytes().to_vec(),
        ),
        (
            RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            conversation_index_prefix(&doc.conversation_id)?,
            removal,
        ),
    ] {
        let key = index_key(tx, namespace, &prefix, doc.created_at_ms, &doc.archive_id)?;
        if database.entity_get(tx, &key)? != Some(expected) {
            return Err(invalid_data("raw archive snapshot covering index differs"));
        }
    }
    if database.entity_get(tx, &claim_key(tx, &doc.archive_id)?)?
        != Some(tx.owner_user_id().to_be_bytes().to_vec())
    {
        return Err(invalid_data("raw archive snapshot identity claim differs"));
    }
    Ok(())
}

fn read_snapshot(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Value>> {
    validate_text(id, 160)?;
    let Some(document) = load_document(database, tx, id)? else {
        return Ok(None);
    };
    if document.archive_id != id {
        return Err(invalid_data("raw archive snapshot identity differs"));
    }
    request(&document)?;
    verify_indexes(database, tx, &document)?;
    Ok(Some(serde_json::to_value(document)?))
}

fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let document: ArchiveDocument = serde_json::from_value(payload.clone())
        .map_err(|_| invalid_input("invalid raw archive snapshot"))?;
    if serde_json::to_value(&document)? != payload {
        return Err(invalid_input("raw archive snapshot fields differ"));
    }
    let request = request(&document)?;
    if let Some(existing) = read_snapshot(database, tx, &document.archive_id)? {
        if existing != payload {
            return Err(conflict("raw archive snapshot differs"));
        }
        return Ok(false);
    }
    put_contents(database, tx, &request, true)?;
    if read_snapshot(database, tx, &document.archive_id)? != Some(payload) {
        return Err(invalid_data("restored raw archive snapshot differs"));
    }
    Ok(true)
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: Scan = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid raw archive scan request"))?;
    let mut suffix = Vec::new();
    if let Some(cursor) = request.cursor {
        let id = cursor
            .strip_prefix("raw_archives:")
            .ok_or_else(|| invalid_input("invalid raw archive cursor"))?;
        validate_text(id, 160)?;
        suffix.extend_from_slice(id.as_bytes());
        suffix.push(0);
    }
    let start = owner_key(tx, RAW_ARCHIVE_DOCUMENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        RAW_ARCHIVE_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (key, _) in &page {
        let id = std::str::from_utf8(key.key_bytes())
            .map_err(|_| invalid_data("invalid raw archive catalog identity"))?;
        if read_snapshot(database, tx, id)?.is_none() {
            return Err(invalid_data("raw archive catalog snapshot missing"));
        }
        rows.push(json!({"archive_id":id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        rows.last()
            .map(|row| format!("raw_archives:{}", row["archive_id"].as_str().unwrap()))
    } else {
        None
    };
    Ok(json!({"rows":rows,"cursor":cursor}))
}

fn stats(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: Stats = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid raw archive statistics request"))?;
    match request {
        Stats::Conversation { conversation_id } => {
            validate_text(&conversation_id, 128)?;
            let (count, bytes) = read_conversation_stats(database, tx, &conversation_id)?;
            Ok(json!({"count":count,"stored_bytes":bytes}))
        }
        Stats::Owner => {
            let key = counter_key(tx, RAW_ARCHIVE_OWNER_COUNT_NAMESPACE, b"count")?;
            Ok(json!({"count":read_u64(database, tx, &key, "owner count")?}))
        }
        Stats::Tenant => {
            let key = global_key(tx, TENANT_USAGE_NAMESPACE, b"bytes")?;
            Ok(json!({"stored_bytes":read_u64(database, tx, &key, "tenant usage")?}))
        }
        Stats::Attempt { attempt_id } => {
            validate_text(&attempt_id, 128)?;
            let key = counter_key(
                tx,
                RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
                attempt_id.as_bytes(),
            )?;
            Ok(json!({"stored_bytes":read_u64(database, tx, &key, "Attempt usage")?}))
        }
    }
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
        "migration.raw.archive.restore" => {
            let inserted = restore(database, &mut tx, payload)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.raw.archive.get" => {
            let request: Get = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid raw archive get request"))?;
            Ok(read_snapshot(database, &mut tx, &request.archive_id)?.unwrap_or(Value::Null))
        }
        "migration.raw.archive.scan" => scan(database, &mut tx, payload),
        "migration.raw.archive.stats" => stats(database, &mut tx, payload),
        _ => Err(invalid_input("unknown raw archive migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/raw-migration")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let db = AuthorityDatabase::initialize_with_vfs(Path::new("/raw-migration"), vfs.clone())
            .unwrap();
        (vfs, db)
    }
    fn snapshot(id: &str) -> Value {
        // These are deliberately opaque historical compressed bytes; import
        // must preserve them even when the live decoder would report an error.
        json!({"archive_id":id,"conversation_id":"detached","turn_id":"old-turn",
            "attempt_id":"old-attempt","task_id":"old-task","round_num":3,"transport_attempt":2,
            "request_blob_b64":"eJwDAAAAAAE=","response_blob_b64":"",
            "request_bytes":91,"response_bytes":92,"stored_bytes":8,
            "request_sha256":"a".repeat(64),"response_sha256":"b".repeat(64),
            "integrity":"partial","truncation_reason":"transport_interrupted",
            "summary":{"text":"historic","nested":{"value":1}},"created_at_ms":123})
    }
    fn call(db: &mut AuthorityDatabase, op: &str, payload: Value) -> io::Result<Value> {
        execute(db, 7, 11, &format!("migration.raw.archive.{op}"), payload)
    }
    #[test]
    fn detached_snapshots_reopen_exactly_with_indexes_and_counters() {
        let (vfs, mut db) = prepared();
        let original = snapshot("a");
        assert_eq!(
            call(&mut db, "restore", original.clone()).unwrap(),
            json!({"inserted":true})
        );
        assert_eq!(
            call(&mut db, "restore", original.clone()).unwrap(),
            json!({"inserted":false})
        );
        let mut different = original.clone();
        different["summary"]["text"] = json!("different");
        assert_eq!(
            call(&mut db, "restore", different).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        call(&mut db, "restore", snapshot("b")).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/raw-migration"), vfs).unwrap();
        assert_eq!(
            call(&mut db, "get", json!({"archive_id":"a"})).unwrap(),
            original
        );
        assert_eq!(
            call(&mut db, "scan", json!({})).unwrap(),
            json!({"rows":[{"archive_id":"a"},{"archive_id":"b"}],"cursor":null})
        );
        for (scope, expected) in [
            (json!({"scope":"owner"}), json!({"count":2})),
            (json!({"scope":"tenant"}), json!({"stored_bytes":16})),
            (
                json!({"scope":"conversation","conversation_id":"detached"}),
                json!({"count":2,"stored_bytes":16}),
            ),
            (
                json!({"scope":"attempt","attempt_id":"old-attempt"}),
                json!({"stored_bytes":16}),
            ),
        ] {
            assert_eq!(call(&mut db, "stats", scope).unwrap(), expected);
        }
        assert_eq!(
            execute(
                &mut db,
                7,
                12,
                "migration.raw.archive.get",
                json!({"archive_id":"a"})
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut db,
                7,
                12,
                "migration.raw.archive.restore",
                snapshot("a")
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::AlreadyExists
        );
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            super::super::execute(
                &db,
                &mut tx,
                &request(&serde_json::from_value(snapshot("c")).unwrap()).unwrap()
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::NotFound
        );
    }
    #[test]
    fn quota_refusal_never_publishes_scrubbed_source_or_partial_indexes() {
        for namespace in [
            RAW_ARCHIVE_TENANT_USAGE_NAMESPACE,
            RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE,
        ] {
            let (_, mut db) = prepared();
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let (key, capacity) = if namespace == RAW_ARCHIVE_TENANT_USAGE_NAMESPACE {
                (
                    global_key(&tx, namespace, b"bytes").unwrap(),
                    MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES,
                )
            } else {
                (
                    counter_key(&tx, namespace, b"old-attempt").unwrap(),
                    MAX_RAW_ARCHIVE_STORED_BYTES as u64,
                )
            };
            write_u64(&db, &mut tx, key, capacity).unwrap();
            db.commit(tx).unwrap();
            assert_eq!(
                call(&mut db, "restore", snapshot("a")).unwrap_err().kind(),
                io::ErrorKind::OutOfMemory
            );
            assert_eq!(
                call(&mut db, "get", json!({"archive_id":"a"})).unwrap(),
                Value::Null
            );
            assert_eq!(
                call(&mut db, "stats", json!({"scope":"owner"})).unwrap(),
                json!({"count":0})
            );
        }
    }
    #[test]
    fn malformed_snapshots_and_corrupt_covering_indexes_fail_closed() {
        let (_, mut db) = prepared();
        for (field, value) in [
            ("stored_bytes", json!(9)),
            ("extra", json!(true)),
            ("request_blob_b64", json!("not-base64")),
        ] {
            let mut bad = snapshot("a");
            bad[field] = value;
            assert!(call(&mut db, "restore", bad).is_err());
        }
        call(&mut db, "restore", snapshot("a")).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let key = index_key(
            &tx,
            RAW_ARCHIVE_TASK_INDEX_NAMESPACE,
            &index_prefix("old-task", None).unwrap(),
            123,
            "a",
        )
        .unwrap();
        db.entity_put(&mut tx, key, b"{}".to_vec()).unwrap();
        db.commit(tx).unwrap();
        assert!(call(&mut db, "get", json!({"archive_id":"a"})).is_err());
        assert!(call(&mut db, "restore", snapshot("a")).is_err());
        assert!(call(&mut db, "scan", json!({})).is_err());
        assert!(call(&mut db, "scan", json!({"cursor":"attempts:a"})).is_err());
    }
}
