use super::*;
use crate::vfs::{DeterministicVfs, Vfs};
use std::{path::Path, sync::Arc};

fn database(root: &Path, vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
    vfs.create_dir(root).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    AuthorityDatabase::initialize_with_vfs(root, vfs).unwrap()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn event(sequence: u64) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "type": "log",
        "sequence": sequence,
        "payload": {"line": format!("event-{sequence}")},
    }))
    .unwrap()
}

fn restore_payload(task_id: &str, sequence: u64) -> Value {
    json!({
        "tenant_id": 7,
        "owner_user_id": 11,
        "task_id": task_id,
        "sequence": sequence,
        "event_type": "log",
        "payload_json_hex": hex(&event(sequence)),
        "created_at_ms": 1_700_000_000_000_i64 + sequence as i64,
    })
}

#[test]
fn storage_event_round_trip_get_and_scan_preserves_bytes() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/storage-events");
    let mut db = database(root, vfs);
    assert_eq!(
        execute(
            &mut db,
            7,
            11,
            "migration.storage.event.restore",
            restore_payload("task-a", 5),
        )
        .unwrap()["inserted"],
        json!(true)
    );
    let got = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.get",
        json!({"tenant_id":7,"owner_user_id":11,"task_id":"task-a","sequence":5}),
    )
    .unwrap();
    // The exact caller-supplied payload bytes are returned verbatim (hex).
    assert_eq!(got["payload_json_hex"], json!(hex(&event(5))));
    let scanned = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.scan",
        json!({"tenant_id":7,"owner_user_id":11}),
    )
    .unwrap();
    assert_eq!(scanned["cursor"], Value::Null);
    assert_eq!(
        scanned["rows"],
        json!([{"tenant_id":7,"owner_user_id":11,"task_id":"task-a","sequence":5}])
    );
}

#[test]
fn storage_event_insert_or_match_is_idempotent_on_exact_bytes() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/storage-events-idem");
    let mut db = database(root, vfs);
    let payload = restore_payload("task-b", 2);
    for expected in [true, false, false] {
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.storage.event.restore",
                payload.clone(),
            )
            .unwrap()["inserted"],
            json!(expected)
        );
    }
    // Same (task, sequence) with different bytes must conflict, never overwrite.
    let mut divergent = restore_payload("task-b", 2);
    divergent["payload_json_hex"] = json!(hex(b"{\"type\":\"log\",\"payload\":{\"line\":\"other\"}}"));
    let error = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore",
        divergent,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
}

#[test]
fn storage_event_owner_scope_fence() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/storage-events-fence");
    let mut db = database(root, vfs);
    execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore",
        restore_payload("task-c", 1),
    )
    .unwrap();
    // Mismatched tenant/owner scope in the payload is refused.
    let error = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore",
        json!({"tenant_id":7,"owner_user_id":12,"task_id":"task-c","sequence":1,"event_type":"log","payload_json_hex":hex(&event(1)),"created_at_ms":1}),
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
    // A different owner cannot read the row through the scoped namespace.
    let foreign = execute(
        &mut db,
        7,
        12,
        "migration.storage.event.get",
        json!({"tenant_id":7,"owner_user_id":12,"task_id":"task-c","sequence":1}),
    )
    .unwrap();
    assert_eq!(foreign, Value::Null);
}

#[test]
fn storage_event_scan_pages_with_cursor() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/storage-events-page");
    let mut db = database(root, vfs);
    for index in 1..=(MIGRATION_SESSION_MAX_PAGE_ROWS + 4) as u64 {
        execute(
            &mut db,
            7,
            11,
            "migration.storage.event.restore",
            restore_payload("task-page", index),
        )
        .unwrap();
    }
    let first = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.scan",
        json!({"tenant_id":7,"owner_user_id":11}),
    )
    .unwrap();
    assert_eq!(first["rows"].as_array().unwrap().len(), MIGRATION_SESSION_MAX_PAGE_ROWS);
    let cursor = first["cursor"].as_str().unwrap().to_owned();
    assert!(cursor.starts_with("storage-events:"));
    let second = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.scan",
        json!({"tenant_id":7,"owner_user_id":11,"cursor":cursor}),
    )
    .unwrap();
    assert_eq!(second["rows"].as_array().unwrap().len(), 4);
    assert_eq!(second["cursor"], Value::Null);
}

#[test]
fn storage_event_rejects_invalid_oversized_and_unknown_fields() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/storage-events-invalid");
    let mut db = database(root, vfs);
    // deny_unknown_fields.
    let mut extra = restore_payload("task-d", 1);
    extra["unexpected"] = json!(true);
    let error = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore",
        extra,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    // Malformed hex is refused.
    let mut bad_hex = restore_payload("task-d", 1);
    bad_hex["payload_json_hex"] = json!("zz");
    let error = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore",
        bad_hex,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    // Oversized event is refused by the byte budget.
    let mut big = restore_payload("task-d", 9);
    big["payload_json_hex"] = json!("22".repeat(crate::generated_tofudb_ir::MAX_MIGRATION_DOCUMENT_BYTES + 1));
    let error = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore",
        big,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
}

#[test]
fn storage_event_large_payload_export_preserves_exact_bytes_and_clock() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let mut db = database(Path::new("/large-event-export"), vfs);
    let raw = format!("{{ \"type\": \"messages_snapshot\", \"text\": \"{}\" }}", "x".repeat(300 * 1024));
    let mut input = restore_payload("large", 7);
    input["payload_json_hex"] = json!(hex(raw.as_bytes()));
    input["event_type"] = json!("messages_snapshot");
    input["created_at_ms"] = json!(123);
    execute(&mut db, 7, 11, "migration.storage.event.restore", input.clone()).unwrap();
    let got = execute(&mut db, 7, 11, "migration.storage.event.get",
        json!({"tenant_id":7,"owner_user_id":11,"task_id":"large","sequence":7})).unwrap();
    assert_eq!(got["payload_json_hex"], input["payload_json_hex"]);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(read(&db, &mut tx, "large", 7).unwrap().unwrap().0.created_at_ms, 123);
    drop(tx);
    assert_eq!(execute(&mut db, 7, 11, "migration.storage.event.restore", input).unwrap()["inserted"], false);
}

#[test]
fn storage_event_batch_restores_atomically_and_replays_idempotently() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/storage-events-batch");
    let mut db = database(root, vfs);
    let rows = vec![
        restore_payload("task-a", 1),
        restore_payload("task-a", 2),
        restore_payload("task-b", 1),
    ];
    let inserted = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore_batch",
        json!({"rows": rows}),
    )
    .unwrap();
    assert_eq!(inserted["results"].as_array().unwrap().len(), 3);
    // Idempotent replay: the same batch matches without writes.
    let replay = execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore_batch",
        json!({"rows": rows}),
    )
    .unwrap();
    assert!(replay["results"]
        .as_array()
        .unwrap()
        .iter()
        .all(|row| row["inserted"] == json!(false)));
    // Atomicity: a scope-violating row fails the whole batch and nothing
    // from that batch is readable afterwards.
    let mut bad = restore_payload("task-c", 1);
    bad["tenant_id"] = json!(8);
    assert!(execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore_batch",
        json!({"rows": [restore_payload("task-c", 0), bad]}),
    )
    .is_err());
    assert_eq!(
        execute(
            &mut db,
            7,
            11,
            "migration.storage.event.get",
            json!({"tenant_id":7,"owner_user_id":11,"task_id":"task-c","sequence":0}),
        )
        .unwrap(),
        Value::Null
    );
    // Bounded ranges fail closed.
    assert!(execute(
        &mut db,
        7,
        11,
        "migration.storage.event.restore_batch",
        json!({"rows": []}),
    )
    .is_err());
}
