use super::*;
use crate::vfs::{DeterministicVfs, Vfs};
use std::{path::Path, sync::Arc};

fn database(root: &Path, vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
    vfs.create_dir(root).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    AuthorityDatabase::initialize_with_vfs(root, vfs).unwrap()
}

fn envelope(conversation_id: &str, sync_sequence: u64) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "contract": "tofu.conversation-sync.event/v1",
        "type": "turn-upsert",
        "conversationId": conversation_id,
        "syncSeq": sync_sequence,
        "occurredAt": 1_700_000_000_000_u64,
        "payload": {"text": "retained"},
    }))
    .unwrap()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn restore_payload(conversation_id: &str, sync_sequence: u64) -> Value {
    json!({
        "tenant_id": 7,
        "owner_user_id": 11,
        "conversation_id": conversation_id,
        "sync_sequence": sync_sequence,
        "change_type": "turn-upsert",
        "turn_id": "turn-1",
        "attempt_id": "attempt-1",
        "event_json_hex": hex(&envelope(conversation_id, sync_sequence)),
        "created_at_ms": 1_700_000_000_000_i64,
        "attempt_sequence": 3,
    })
}

#[test]
fn sync_event_round_trip_get_and_scan_preserves_bytes() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-events");
    let mut db = database(root, vfs);
    assert_eq!(
        execute(
            &mut db,
            7,
            11,
            "migration.conversation.sync.event.restore",
            restore_payload("conv-a", 7),
        )
        .unwrap(),
        json!({"inserted": true})
    );
    let got = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.get",
        json!({"tenant_id":7,"owner_user_id":11,"conversation_id":"conv-a","sync_sequence":7}),
    )
    .unwrap();
    // The exact caller-supplied bytes are returned verbatim (hex).
    assert_eq!(
        got["event_json_hex"],
        json!(hex(&envelope("conv-a", 7)))
    );
    let scanned = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.scan",
        json!({"tenant_id":7,"owner_user_id":11}),
    )
    .unwrap();
    assert_eq!(scanned["cursor"], Value::Null);
    assert_eq!(
        scanned["rows"],
        json!([{"tenant_id":7,"owner_user_id":11,"conversation_id":"conv-a","sync_sequence":7}])
    );
}

#[test]
fn sync_event_insert_or_match_is_idempotent_on_exact_bytes() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-events-idem");
    let mut db = database(root, vfs);
    let payload = restore_payload("conv-b", 3);
    for expected in [true, false, false] {
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.conversation.sync.event.restore",
                payload.clone(),
            )
            .unwrap(),
            json!({"inserted": expected})
        );
    }
    // Same identity but different bytes must conflict, never overwrite.
    let mut divergent = restore_payload("conv-b", 3);
    divergent["event_json_hex"] = json!(hex(b"{\"conversationId\":\"conv-b\",\"syncSeq\":3,\"occurredAt\":2,\"payload\":{\"text\":\"other\"}}"));
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        divergent,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
}

#[test]
fn sync_event_owner_scope_fence() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-events-fence");
    let mut db = database(root, vfs);
    execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        restore_payload("conv-c", 1),
    )
    .unwrap();
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        json!({"tenant_id":7,"owner_user_id":12,"conversation_id":"conv-c","sync_sequence":1,"change_type":"turn-upsert","turn_id":null,"attempt_id":null,"event_json_hex":hex(&envelope("conv-c",1)),"created_at_ms":1,"attempt_sequence":null}),
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
    let foreign = execute(
        &mut db,
        7,
        12,
        "migration.conversation.sync.event.get",
        json!({"tenant_id":7,"owner_user_id":12,"conversation_id":"conv-c","sync_sequence":1}),
    )
    .unwrap();
    assert_eq!(foreign, Value::Null);
}

#[test]
fn sync_event_scan_pages_with_cursor() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-events-page");
    let mut db = database(root, vfs);
    for index in 1..=(MIGRATION_SESSION_MAX_PAGE_ROWS + 4) as u64 {
        execute(
            &mut db,
            7,
            11,
            "migration.conversation.sync.event.restore",
            restore_payload("conv-page", index),
        )
        .unwrap();
    }
    let first = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.scan",
        json!({"tenant_id":7,"owner_user_id":11}),
    )
    .unwrap();
    assert_eq!(first["rows"].as_array().unwrap().len(), MIGRATION_SESSION_MAX_PAGE_ROWS);
    let cursor = first["cursor"].as_str().unwrap().to_owned();
    assert!(cursor.starts_with("sync-events:"));
    let second = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.scan",
        json!({"tenant_id":7,"owner_user_id":11,"cursor":cursor}),
    )
    .unwrap();
    assert_eq!(second["rows"].as_array().unwrap().len(), 4);
    assert_eq!(second["cursor"], Value::Null);
}

#[test]
fn sync_event_rejects_invalid_oversized_and_unknown_fields() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-events-invalid");
    let mut db = database(root, vfs);
    // deny_unknown_fields.
    let mut extra = restore_payload("conv-d", 1);
    extra["unexpected"] = json!(true);
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        extra,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    // Malformed hex is refused.
    let mut bad_hex = restore_payload("conv-d", 1);
    bad_hex["event_json_hex"] = json!("zz");
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        bad_hex,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    // Envelope identity must match the claimed conversation/sequence.
    let mut mismatch = restore_payload("conv-d", 2);
    mismatch["sync_sequence"] = json!(99);
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        mismatch,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    // Oversized event is refused by the byte budget.
    let mut big = restore_payload("conv-d", 5);
    big["event_json_hex"] = json!("22".repeat(MAX_MIGRATION_DOCUMENT_BYTES + 1));
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.event.restore",
        big,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
}
