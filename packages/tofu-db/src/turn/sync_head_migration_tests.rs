use super::*;
use crate::vfs::{DeterministicVfs, Vfs};
use std::{path::Path, sync::Arc};

fn database(root: &Path, vfs: Arc<DeterministicVfs>) -> AuthorityDatabase {
    vfs.create_dir(root).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    AuthorityDatabase::initialize_with_vfs(root, vfs).unwrap()
}

fn restore_payload(conversation_id: &str, sync_sequence: u64) -> Value {
    json!({
        "tenant_id": 7,
        "owner_user_id": 11,
        "conversation_id": conversation_id,
        "sync_sequence": sync_sequence,
        "updated_at_ms": 100,
    })
}

#[test]
fn sync_head_round_trip_get_and_scan() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-heads");
    let mut db = database(root, vfs);
    assert_eq!(
        execute(
            &mut db,
            7,
            11,
            "migration.conversation.sync.head.restore",
            restore_payload("conv-a", 42),
        )
        .unwrap(),
        json!({"inserted": true})
    );
    let got = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.get",
        json!({"tenant_id":7,"owner_user_id":11,"conversation_id":"conv-a"}),
    )
    .unwrap();
    assert_eq!(
        got,
        json!({"tenant_id":7,"owner_user_id":11,"conversation_id":"conv-a","sync_sequence":42})
    );
    let scanned = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.scan",
        json!({"tenant_id":7,"owner_user_id":11}),
    )
    .unwrap();
    assert_eq!(scanned["cursor"], Value::Null);
    assert_eq!(
        scanned["rows"],
        json!([{"tenant_id":7,"owner_user_id":11,"conversation_id":"conv-a","sync_sequence":42}])
    );
}

#[test]
fn sync_head_insert_or_match_is_idempotent() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-heads-idem");
    let mut db = database(root, vfs);
    let payload = restore_payload("conv-b", 9);
    for expected in [true, false, false] {
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.conversation.sync.head.restore",
                payload.clone(),
            )
            .unwrap(),
            json!({"inserted": expected})
        );
    }
    // Divergent sequence on the same identity must conflict, never overwrite.
    let mut divergent = restore_payload("conv-b", 10);
    divergent["sync_sequence"] = json!(10);
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.restore",
        divergent,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
}

#[test]
fn sync_head_owner_scope_fence() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-heads-fence");
    let mut db = database(root, vfs);
    execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.restore",
        restore_payload("conv-c", 5),
    )
    .unwrap();
    // Mismatched tenant/owner scope in the payload is refused.
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.restore",
        json!({"tenant_id":7,"owner_user_id":12,"conversation_id":"conv-c","sync_sequence":5,"updated_at_ms":1}),
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
    // A different owner cannot read the row through the scoped namespace.
    let foreign = execute(
        &mut db,
        7,
        12,
        "migration.conversation.sync.head.get",
        json!({"tenant_id":7,"owner_user_id":12,"conversation_id":"conv-c"}),
    )
    .unwrap();
    assert_eq!(foreign, Value::Null);
}

#[test]
fn sync_head_scan_pages_with_cursor() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-heads-page");
    let mut db = database(root, vfs);
    for index in 0..(MIGRATION_SESSION_MAX_PAGE_ROWS + 4) {
        execute(
            &mut db,
            7,
            11,
            "migration.conversation.sync.head.restore",
            restore_payload(&format!("conv-{index:04}"), index as u64 + 1),
        )
        .unwrap();
    }
    let first = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.scan",
        json!({"tenant_id":7,"owner_user_id":11}),
    )
    .unwrap();
    assert_eq!(first["rows"].as_array().unwrap().len(), MIGRATION_SESSION_MAX_PAGE_ROWS);
    let cursor = first["cursor"].as_str().unwrap().to_owned();
    assert!(cursor.starts_with("sync-heads:"));
    let second = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.scan",
        json!({"tenant_id":7,"owner_user_id":11,"cursor":cursor}),
    )
    .unwrap();
    assert_eq!(second["rows"].as_array().unwrap().len(), 4);
    assert_eq!(second["cursor"], Value::Null);
}

#[test]
fn sync_head_rejects_invalid_and_unknown_fields() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    let root = Path::new("/sync-heads-invalid");
    let mut db = database(root, vfs);
    // deny_unknown_fields: an extra field is refused.
    let mut extra = restore_payload("conv-d", 1);
    extra["unexpected"] = json!(true);
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.restore",
        extra,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    // Empty conversation identity is refused.
    let error = execute(
        &mut db,
        7,
        11,
        "migration.conversation.sync.head.restore",
        restore_payload("", 1),
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
}
