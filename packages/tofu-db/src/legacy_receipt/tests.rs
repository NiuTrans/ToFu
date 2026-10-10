use super::*;
use crate::receipt::encode_receipt_response;
use crate::vfs::{DeterministicVfs, Vfs};
use std::{path::Path, sync::Arc};

fn snapshot(raw: &[u8]) -> ReceiptSnapshot {
    ReceiptSnapshot {
        operation: "record.put".to_owned(),
        request_digest: [3; 32],
        encoded_response: encode_receipt_response(raw).unwrap(),
        committed_at_ms: 1234,
    }
}

#[test]
fn exact_global_replay_requires_owner_witness_and_survives_reopen() {
    for size in [10, 12_000, 100_000] {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/legacy")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/legacy"), vfs.clone()).unwrap();
        let source = Uuid::new_v4();
        let raw = serde_json::to_vec(&serde_json::json!({"old":"x".repeat(size)})).unwrap();
        let key = command_receipt_key("old-command").unwrap();
        assert!(restore(&mut db, 7, source, key, snapshot(&raw)).unwrap());
        assert!(!restore(&mut db, 7, source, key, snapshot(&raw)).unwrap());
        assert!(restore(&mut db, 7, Uuid::new_v4(), key, snapshot(&raw)).is_err());
        assert!(lookup(&db, 7, 11, "old-command", "record.put", [3; 32], false).is_err());
        assert!(lookup(&db, 7, 12, "old-command", "record.put", [4; 32], true).is_err());
        assert!(
            lookup(&db, 8, 11, "old-command", "record.put", [3; 32], true)
                .unwrap()
                .is_none()
        );
        drop(db);
        vfs.crash().unwrap();
        let db = AuthorityDatabase::open_with_vfs(Path::new("/legacy"), vfs).unwrap();
        assert_eq!(
            lookup(&db, 7, 11, "old-command", "record.put", [3; 32], true).unwrap(),
            Some(raw)
        );
    }
}

#[test]
fn source_accounting_and_receipt_share_one_commit_and_enforce_bounds() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/legacy")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/legacy"), vfs).unwrap();
    let source = Uuid::new_v4();
    let first = command_receipt_key("first").unwrap();
    restore(&mut db, 7, source, first, snapshot(b"{}")).unwrap();
    let mut tx = db.begin(7, LEGACY_RECEIPT_OWNER_ID).unwrap();
    let mut encoded = b"TDBLRS01".to_vec();
    encoded.extend_from_slice(source.as_bytes());
    encoded.extend_from_slice(&MAX_LEGACY_RECEIPT_ROWS.to_le_bytes());
    encoded.extend_from_slice(&2_u64.to_le_bytes());
    db.entity_put(&mut tx, metadata_key(7).unwrap(), encoded)
        .unwrap();
    db.commit(tx).unwrap();
    assert!(restore(
        &mut db,
        7,
        source,
        command_receipt_key("next").unwrap(),
        snapshot(b"{}")
    )
    .is_err());
    assert!(lookup(&db, 7, 11, "next", "record.put", [3; 32], true)
        .unwrap()
        .is_none());
    let mut tx = db.begin(7, LEGACY_RECEIPT_OWNER_ID).unwrap();
    let mut encoded = b"TDBLRS01".to_vec();
    encoded.extend_from_slice(source.as_bytes());
    encoded.extend_from_slice(&1_u64.to_le_bytes());
    encoded.extend_from_slice(&MAX_LEGACY_RECEIPT_STORED_BYTES.to_le_bytes());
    db.entity_put(&mut tx, metadata_key(7).unwrap(), encoded)
        .unwrap();
    db.commit(tx).unwrap();
    assert!(restore(
        &mut db,
        7,
        source,
        command_receipt_key("next").unwrap(),
        snapshot(b"{}")
    )
    .is_err());
    assert!(lookup(&db, 7, 11, "next", "record.put", [3; 32], true)
        .unwrap()
        .is_none());
}

#[test]
fn legacy_receipt_atomic_source_metadata_and_blob_survive_faults() {
    use crate::vfs::{FaultAction, Operation, Vfs};
    let seed = Arc::new(DeterministicVfs::new(None));
    seed.create_dir(Path::new("/fault")).unwrap();
    seed.sync_directory(Path::new("/")).unwrap();
    drop(AuthorityDatabase::initialize_with_vfs(Path::new("/fault"), seed.clone()).unwrap());
    let source = Uuid::new_v4();
    let raw = serde_json::to_vec(&serde_json::json!({"history":"x".repeat(12_000)})).unwrap();
    let key = command_receipt_key("fault-command").unwrap();
    let prepare = || {
        let vfs = Arc::new(seed.fork_durable().unwrap());
        let db = AuthorityDatabase::open_with_vfs(Path::new("/fault"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, db)
    };
    let (baseline, mut db) = prepare();
    restore(&mut db, 7, source, key, snapshot(&raw)).unwrap();
    let trace = baseline.trace().unwrap();
    assert!(trace.contains(&Operation::Write));
    assert!(trace.iter().any(|op| matches!(
        op,
        Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
    )));
    drop(db);
    for (index, operation) in trace.iter().enumerate() {
        let occurrence = trace[..=index].iter().filter(|op| *op == operation).count();
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(3));
        }
        if matches!(
            operation,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let mut exercised = None;
            for _ in 0..16 {
                let (vfs, mut db) = prepare();
                vfs.arm_operation_fault(*operation, occurrence, action)
                    .unwrap();
                let acknowledged = restore(&mut db, 7, source, key, snapshot(&raw)).is_ok();
                if vfs.operation_fault_fired().unwrap() {
                    exercised = Some((vfs, db, acknowledged));
                    break;
                }
            }
            let (vfs, db, acknowledged) =
                exercised.expect("planned legacy receipt fault must execute");
            vfs.arm_fault(None).unwrap();
            drop(db);
            vfs.crash().unwrap();
            let mut db = AuthorityDatabase::open_with_vfs(Path::new("/fault"), vfs).unwrap();
            let observed =
                lookup(&db, 7, 11, "fault-command", "record.put", [3; 32], true).unwrap();
            if acknowledged {
                assert_eq!(observed, Some(raw.clone()));
            } else {
                assert!(observed.is_none() || observed == Some(raw.clone()));
            }
            restore(&mut db, 7, source, key, snapshot(&raw)).unwrap();
            assert_eq!(
                lookup(&db, 7, 11, "fault-command", "record.put", [3; 32], true).unwrap(),
                Some(raw.clone())
            );
            let mut tx = db.begin(7, LEGACY_RECEIPT_OWNER_ID).unwrap();
            let metadata = db
                .entity_get(&mut tx, &metadata_key(7).unwrap())
                .unwrap()
                .unwrap();
            assert_eq!(
                decode_metadata(&metadata).unwrap(),
                (source, 1, snapshot(&raw).encoded_response.len() as u64)
            );
        }
    }
}

#[test]
fn legacy_receipts_do_not_alias_existing_tenant_global_receipts() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/scope")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/scope"), vfs).unwrap();
    let mut tx = db
        .begin(7, crate::conversation_header::TENANT_GLOBAL_OWNER_ID)
        .unwrap();
    db.receipt_insert(
        &mut tx,
        "shared-command",
        "record.put",
        [3; 32],
        br#"{"native":true}"#,
        1234,
    )
    .unwrap();
    db.commit(tx).unwrap();
    restore(
        &mut db,
        7,
        Uuid::new_v4(),
        command_receipt_key("shared-command").unwrap(),
        snapshot(br#"{"legacy":true}"#),
    )
    .unwrap();
    assert_eq!(
        lookup(&db, 7, 11, "shared-command", "record.put", [3; 32], true).unwrap(),
        Some(br#"{"legacy":true}"#.to_vec())
    );
    let mut tx = db
        .begin(7, crate::conversation_header::TENANT_GLOBAL_OWNER_ID)
        .unwrap();
    assert_eq!(
        db.receipt_lookup(&mut tx, "shared-command", "record.put", [3; 32])
            .unwrap(),
        Some(br#"{"native":true}"#.to_vec())
    );
}

#[test]
fn operator_legacy_catalog_is_exact_paged_and_source_fenced() {
    use crate::receipt_migration::hex;
    use serde_json::{json, Value};
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let source = Uuid::new_v4();
    let base = json!({"tenant_id":7,"source_authority_uuid":source.to_string()});
    let mut expected = Vec::new();
    for index in 0..130 {
        let key = hex(&command_receipt_key(&format!("old-{index}")).unwrap());
        let mut row = base.clone();
        row.as_object_mut().unwrap().extend(
            json!({"command_key":key,"operation":"record.put",
            "request_digest":hex(&[3;32]),"response_hex":"7b7d","committed_at_ms":1234})
            .as_object()
            .unwrap()
            .clone(),
        );
        assert_eq!(
            migration::execute(&mut db, 7, "migration.legacy.receipt.restore", row.clone())
                .unwrap()["inserted"],
            true
        );
        let mut identity = base.clone();
        identity["command_key"] = json!(key);
        assert_eq!(
            migration::execute(&mut db, 7, "migration.legacy.receipt.get", identity).unwrap(),
            row
        );
        expected.push(key);
    }
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    let mut query = base.clone();
    query["cursor"] = Value::Null;
    let first =
        migration::execute(&mut db, 7, "migration.legacy.receipt.scan", query.clone()).unwrap();
    assert_eq!(first["rows"].as_array().unwrap().len(), 128);
    query["cursor"] = first["cursor"].clone();
    let second =
        migration::execute(&mut db, 7, "migration.legacy.receipt.scan", query.clone()).unwrap();
    assert_eq!(second["rows"].as_array().unwrap().len(), 2);
    assert!(second["cursor"].is_null());
    let keys: Vec<_> = first["rows"]
        .as_array()
        .unwrap()
        .iter()
        .chain(second["rows"].as_array().unwrap())
        .map(|r| r["command_key"].as_str().unwrap().to_owned())
        .collect();
    expected.sort();
    assert_eq!(keys, expected);
    let stats =
        migration::execute(&mut db, 7, "migration.legacy.receipt.stats", base.clone()).unwrap();
    assert_eq!(stats["rows"], 130);
    assert_eq!(stats["stored_bytes"], 260);
    assert!(
        migration::execute(&mut db, 8, "migration.legacy.receipt.stats", base.clone()).is_err()
    );
    query["source_authority_uuid"] = json!(Uuid::new_v4().to_string());
    assert!(migration::execute(&mut db, 7, "migration.legacy.receipt.scan", query).is_err());
    let mut malformed = base.clone();
    malformed["owner_user_id"] = json!(11);
    assert!(migration::execute(&mut db, 7, "migration.legacy.receipt.stats", malformed).is_err());
    assert!(migration::execute(&mut db, 7, "migration.legacy.receipt.restore", base).is_err());
}
