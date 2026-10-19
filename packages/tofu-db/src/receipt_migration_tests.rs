//! Receipt recovery checks through the same lookup used by live commands.
use super::*;
use crate::receipt::{command_receipt_key, encode_receipt_response};
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};
use std::{path::Path, sync::Arc};

fn request(command_id: &str, raw: &[u8], compressed: bool) -> Value {
    let encoded = if compressed {
        encode_receipt_response(raw).unwrap()
    } else {
        raw.to_vec()
    };
    json!({"tenant_id":7,"owner_user_id":11,"command_key":hex(&command_receipt_key(command_id).unwrap()),
           "operation":"record.put","request_digest":hex(&[3;32]),
           "response_hex":hex(&encoded),"committed_at_ms":123456})
}

fn run(db: &mut AuthorityDatabase, operation: &str, payload: Value) -> io::Result<Value> {
    let tx = db.begin(7, 11)?;
    execute(db, tx, operation, payload)
}

fn identity(payload: &Value) -> Value {
    json!({"tenant_id":payload["tenant_id"],"owner_user_id":payload["owner_user_id"],
           "command_key":payload["command_key"]})
}

#[test]
fn scoped_receipt_snapshot_reopens_replays_exact_response_and_fences_reuse() {
    for (count, compressed) in [(32, false), (2_000, false), (100_000, true)] {
        let directory = tempfile::tempdir().unwrap();
        let raw = serde_json::to_vec(&json!({"retained":"历史".repeat(count)})).unwrap();
        let payload = request("historical-command", &raw, compressed);
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        assert_eq!(
            run(
                &mut db,
                "migration.command.receipt.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(db);
        let mut db = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(
            run(&mut db, "migration.command.receipt.get", identity(&payload)).unwrap(),
            payload
        );
        assert_eq!(
            run(
                &mut db,
                "migration.command.receipt.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            false
        );
        let mut tx = db.begin(7, 11).unwrap();
        assert_eq!(
            db.receipt_lookup(&mut tx, "historical-command", "record.put", [3; 32])
                .unwrap(),
            Some(raw)
        );
        assert_eq!(
            db.receipt_lookup(&mut tx, "historical-command", "record.put", [4; 32])
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
        for (tenant, owner) in [(7, 12), (8, 11)] {
            let mut foreign = db.begin(tenant, owner).unwrap();
            assert!(db
                .receipt_lookup(&mut foreign, "historical-command", "record.put", [3; 32])
                .unwrap()
                .is_none());
        }
        let mut changed = payload.clone();
        changed["committed_at_ms"] = json!(123457);
        assert_eq!(
            run(&mut db, "migration.command.receipt.restore", changed)
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
        let mut foreign = payload.clone();
        foreign["owner_user_id"] = json!(12);
        assert_eq!(
            run(&mut db, "migration.command.receipt.restore", foreign)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            run(&mut db, "migration.command.receipt.get", identity(&payload)).unwrap(),
            payload
        );
    }
}

#[test]
fn receipt_snapshot_catalog_pages_and_bounds_are_owner_scoped() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let payload = request("unused", b"null", false);
    for index in 0..130 {
        let mut row = payload.clone();
        row["command_key"] = json!(format!("{index:064x}"));
        run(&mut db, "migration.command.receipt.restore", row).unwrap();
    }
    let page = run(
        &mut db,
        "migration.command.receipt.scan",
        json!({"tenant_id":7,"owner_user_id":11,"cursor":null}),
    )
    .unwrap();
    assert_eq!(page["rows"].as_array().unwrap().len(), 128);
    let next = run(
        &mut db,
        "migration.command.receipt.scan",
        json!({"tenant_id":7,"owner_user_id":11,"cursor":page["cursor"]}),
    )
    .unwrap();
    assert_eq!(next["rows"].as_array().unwrap().len(), 2);
    assert!(next["cursor"].is_null());
    for field in ["command_key", "request_digest", "response_hex"] {
        let mut invalid = payload.clone();
        invalid[field] = json!("ZZ");
        assert!(run(&mut db, "migration.command.receipt.restore", invalid).is_err());
    }
    let mut oversized = payload.clone();
    oversized["response_hex"] = json!(hex(b"tofu.receipt.unknown.v9\0payload"));
    assert!(run(&mut db, "migration.command.receipt.restore", oversized).is_err());
    let mut corrupt = payload;
    corrupt["response_hex"] = json!(hex(b"not JSON"));
    assert!(run(&mut db, "migration.command.receipt.restore", corrupt).is_err());
    assert!(run(
        &mut db,
        "migration.command.receipt.scan",
        json!({"tenant_id":7,"owner_user_id":11,"cursor":"receipts:bad"})
    )
    .is_err());
}

#[test]
fn receipt_snapshot_faults_preserve_complete_blob_and_live_replay() {
    let seed = Arc::new(DeterministicVfs::new(None));
    seed.create_dir(Path::new("/fault")).unwrap();
    seed.sync_directory(Path::new("/")).unwrap();
    drop(AuthorityDatabase::initialize_with_vfs(Path::new("/fault"), seed.clone()).unwrap());
    let raw = serde_json::to_vec(&json!({"retained":"x".repeat(12_000)})).unwrap();
    let payload = request("fault-command", &raw, false);
    let prepare = || {
        let vfs = Arc::new(seed.fork_durable().unwrap());
        let db = AuthorityDatabase::open_with_vfs(Path::new("/fault"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, db)
    };
    let (baseline, mut db) = prepare();
    run(
        &mut db,
        "migration.command.receipt.restore",
        payload.clone(),
    )
    .unwrap();
    let trace = baseline.trace().unwrap();
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
                let acknowledged = run(
                    &mut db,
                    "migration.command.receipt.restore",
                    payload.clone(),
                )
                .is_ok();
                if vfs.operation_fault_fired().unwrap() {
                    exercised = Some((vfs, db, acknowledged));
                    break;
                }
            }
            let (vfs, db, acknowledged) = exercised.expect("planned receipt fault must execute");
            vfs.arm_fault(None).unwrap();
            drop(db);
            vfs.crash().unwrap();
            let mut db = AuthorityDatabase::open_with_vfs(Path::new("/fault"), vfs).unwrap();
            let observed =
                run(&mut db, "migration.command.receipt.get", identity(&payload)).unwrap();
            assert!(observed.is_null() || observed == payload);
            if acknowledged {
                assert_eq!(observed, payload);
            }
            run(
                &mut db,
                "migration.command.receipt.restore",
                payload.clone(),
            )
            .unwrap();
            let mut tx = db.begin(7, 11).unwrap();
            assert_eq!(
                db.receipt_lookup(&mut tx, "fault-command", "record.put", [3; 32])
                    .unwrap(),
                Some(raw.clone())
            );
        }
    }
}

#[test]
fn large_plain_receipt_restores_reopens_and_replays_complete_owner_scoped_bytes() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let raw = serde_json::to_vec(&json!({"value":"x".repeat(9*1024*1024)})).unwrap();
    let payload = request("large-plain-command",&raw,false);
    assert_eq!(run(&mut db,"migration.command.receipt.restore",payload.clone()).unwrap()["inserted"],true);
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(run(&mut db,"migration.command.receipt.restore",payload).unwrap()["inserted"],false);
    let mut tx = db.begin(7,11).unwrap();
    assert_eq!(db.receipt_lookup(&mut tx,"large-plain-command","record.put",[3;32]).unwrap(),Some(raw));
    assert_eq!(db.receipt_lookup(&mut tx,"large-plain-command","record.put",[4;32]).unwrap_err().kind(),io::ErrorKind::WouldBlock);
    let mut foreign = db.begin(7,12).unwrap();
    assert!(db.receipt_lookup(&mut foreign,"large-plain-command","record.put",[3;32]).unwrap().is_none());
}
