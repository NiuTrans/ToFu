use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};
use sha2::{Digest, Sha256};
use std::{path::Path, sync::Arc};
fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/trash")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let db = AuthorityDatabase::initialize_with_vfs(Path::new("/trash"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, db)
}
fn source() -> Value {
    json!({"header":{"id":"retired","user_id":11,"title":"Retained","settings":{},"created_at_ms":100,"updated_at_ms":200,"rev":37},
           "deleted_at_ms":900,"msg_count":1,"turn_count":1,"frozen":null})
}
fn turn(id: &str, ordinal: u64) -> Value {
    json!({"turn_id":id,"conversation_id":"retired","presentation_id":"","lane_id":"main","parent_turn_id":null,
           "ordinal":ordinal,"actor":"assistant","kind":"reply","run_id":"","status":"completed","current_attempt_id":null,
           "projection":{"content":"durable history","timestamp":1786362450651.2947},"projection_revision":7,
           "settlement":{"outcome":"completed"},"created_at":150,"updated_at":300})
}
fn command(db: &mut AuthorityDatabase, operation: &str, payload: Value) -> io::Result<Value> {
    execute(db, 7, 11, operation, payload)
}
fn seed(db: &mut AuthorityDatabase) {
    command(db, "migration.trash.begin", source()).unwrap();
    crate::turn::migration::execute(db, 7, 11, "migration.turn.restore", turn("old-turn", 1))
        .unwrap();
}
fn finish_one(db: &mut AuthorityDatabase) -> io::Result<Value> {
    command(
        db,
        "migration.trash.finish",
        json!({"conversation_id":"retired"}),
    )
}
fn assert_complete(db: &mut AuthorityDatabase) {
    require_no_pending(db, 7, 11).unwrap();
    assert_eq!(
        command(db, "migration.trash.scan", json!({"cursor":null})).unwrap()
            ["pending_conversation_id"],
        Value::Null
    );
    let view = get(db, 7, 11, "retired").unwrap();
    assert_eq!(view["complete"], true);
    assert_eq!(view["deleted_at_ms"], 900);
    assert_eq!(view["header"]["rev"], 37);
    assert_eq!(view["header"]["updated_at"], 200);
    assert_eq!(view["header"]["msg_count"], 1);
    assert_eq!(view["turn_count"], 1);
    assert_eq!(
        command(
            db,
            "migration.trash.turn.get",
            json!({"conversation_id":"retired","id":"old-turn"})
        )
        .unwrap(),
        turn("old-turn", 1)
    );
    let mut live = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(load_header(db, &mut live, "retired").unwrap().is_none());
    assert_eq!(
        crate::turn::migration::scan_conversation(db, &mut live, "retired", None).unwrap()["rows"],
        json!([])
    );
}
#[test]
fn staged_trash_seals_committed_history_and_restores_after_reopen() {
    let (vfs, mut db) = prepared();
    seed(&mut db);
    assert!(require_no_pending(&db, 7, 11).is_err());
    assert_eq!(
        command(&mut db, "migration.trash.scan", json!({"cursor":null})).unwrap()
            ["pending_conversation_id"],
        "retired"
    );
    require_no_pending(&db, 7, 12).unwrap();
    assert!(command(&mut db, "migration.trash.begin", {
        let mut s = source();
        s["deleted_at_ms"] = json!(901);
        s
    })
    .is_err());
    finish_one(&mut db).unwrap();
    assert_complete(&mut db);
    assert_eq!(
        command(&mut db, "migration.trash.begin", source()).unwrap()["complete"],
        true
    );
    drop(db);
    vfs.crash().unwrap();
    let mut db = AuthorityDatabase::open_with_vfs(Path::new("/trash"), vfs).unwrap();
    finish_one(&mut db).unwrap();
    assert_complete(&mut db);
    assert_eq!(get(&db, 7, 12, "retired").unwrap(), Value::Null);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    super::super::restore(
        &mut db,
        &mut tx,
        &RestoreRequest {
            conversation_id: "retired".to_owned(),
            committed_at_ms: 1000,
        },
    )
    .unwrap();
    db.commit(tx).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let history: Value = serde_json::from_slice(
        &super::super::get(&db, &mut tx, "retired", true, 0, None, &mut || false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(history["messages"][0]["content"], "durable history");
    assert_eq!(history["metadata"]["rev"], 38);
}
#[test]
fn missing_extra_live_or_changed_source_cannot_publish_a_capsule() {
    let (_, mut db) = prepared();
    command(&mut db, "migration.trash.begin", source()).unwrap();
    assert!(finish_one(&mut db).is_err());
    let mut changed = source();
    changed["header"]["rev"] = json!(38);
    assert!(command(&mut db, "migration.trash.begin", changed).is_err());
    assert!(execute(&mut db, 7, 12, "migration.trash.begin", source()).is_err());
    crate::turn::migration::execute(
        &mut db,
        7,
        11,
        "migration.turn.restore",
        turn("old-turn", 1),
    )
    .unwrap();
    crate::turn::migration::execute(&mut db, 7, 11, "migration.turn.restore", turn("extra", 2))
        .unwrap();
    assert!(finish_one(&mut db).is_err());
    assert!(require_no_pending(&db, 7, 11).is_err());
    assert_eq!(
        command(&mut db, "migration.trash.scan", json!({"cursor":null})).unwrap()["rows"],
        json!([])
    );
}
#[test]
fn frozen_history_is_verified_and_pinned_before_staging_is_released() {
    let (vfs, mut db) = prepared();
    let raw = br#"[{"role":"assistant","content":"frozen history"}]"#;
    let hash = format!("{:x}", Sha256::digest(raw));
    let metadata = json!({"conversation_id":"retired","source_sha256":hash,"source_bytes":raw.len(),"created_at_ms":100});
    let mut s = source();
    s["turn_count"] = json!(0);
    s["frozen"] = json!({"source":metadata,"complete":true,"messages":1,"decoded_bytes":raw.len(),
        "pages":[{"start":0,"count":1,"bytes":raw.len(),"sha256":hash}]});
    command(&mut db, "migration.trash.begin", s.clone()).unwrap();
    crate::frozen_transcript::execute(
        &mut db,
        7,
        11,
        "migration.frozen.transcript.begin",
        metadata,
    )
    .unwrap();
    assert!(finish_one(&mut db).is_err());
    crate::frozen_transcript::execute(&mut db,7,11,"migration.frozen.transcript.restore",json!({"conversation_id":"retired","page_index":0,"start":0,"page_json":std::str::from_utf8(raw).unwrap()})).unwrap();
    crate::frozen_transcript::execute(&mut db,7,11,"migration.frozen.transcript.finish",json!({"conversation_id":"retired","page_count":1,"message_count":1,"decoded_bytes":raw.len()})).unwrap();
    finish_one(&mut db).unwrap();
    drop(db);
    vfs.crash().unwrap();
    let mut db = AuthorityDatabase::open_with_vfs(Path::new("/trash"), vfs).unwrap();
    assert_eq!(get(&db, 7, 11, "retired").unwrap()["frozen"], s["frozen"]);
    command(&mut db, "migration.trash.begin", s).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    super::super::restore(
        &mut db,
        &mut tx,
        &RestoreRequest {
            conversation_id: "retired".to_owned(),
            committed_at_ms: 1000,
        },
    )
    .unwrap();
    db.commit(tx).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let history: Value = serde_json::from_slice(
        &super::super::get(&db, &mut tx, "retired", true, 0, None, &mut || false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        history["messages"],
        serde_json::from_slice::<Value>(raw).unwrap()
    );
}
#[test]
fn begin_seal_and_pin_faults_resume_without_losing_history_or_serving_partial_state() {
    for finishing in [false, true] {
        let (seed_vfs, mut seed_db) = prepared();
        if finishing {
            seed(&mut seed_db);
        }
        drop(seed_db);
        let prepare = || {
            let vfs = Arc::new(seed_vfs.fork_durable().unwrap());
            let db = AuthorityDatabase::open_with_vfs(Path::new("/trash"), vfs.clone()).unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, db)
        };
        let (baseline, mut db) = prepare();
        if finishing {
            finish_one(&mut db).unwrap();
        } else {
            command(&mut db, "migration.trash.begin", source()).unwrap();
        }
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
                    let result = if finishing {
                        finish_one(&mut db)
                    } else {
                        command(&mut db, "migration.trash.begin", source())
                    };
                    if vfs.operation_fault_fired().unwrap() {
                        exercised = Some((vfs, db, result.is_ok()));
                        break;
                    }
                }
                let (vfs, db, acknowledged) = exercised.expect("every planned fault must execute");
                vfs.arm_fault(None).unwrap();
                drop(db);
                vfs.crash().unwrap();
                let mut db = AuthorityDatabase::open_with_vfs(Path::new("/trash"), vfs).unwrap();
                if finishing {
                    if acknowledged {
                        assert_complete(&mut db);
                    }
                    finish_one(&mut db).unwrap();
                    assert_complete(&mut db);
                } else {
                    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
                    let header_exists = load_header(&db, &mut tx, "retired").unwrap().is_some();
                    assert_eq!(header_exists, require_no_pending(&db, 7, 11).is_err());
                    if acknowledged {
                        assert!(header_exists);
                    }
                    command(&mut db, "migration.trash.begin", source()).unwrap();
                    crate::turn::migration::execute(
                        &mut db,
                        7,
                        11,
                        "migration.turn.restore",
                        turn("old-turn", 1),
                    )
                    .unwrap();
                    finish_one(&mut db).unwrap();
                    assert_complete(&mut db);
                }
            }
        }
    }
}

#[test]
fn pinned_count_and_retention_index_are_independently_verified() {
    for corrupt_count in [true, false] {
        let (_, mut db) = prepared();
        seed(&mut db);
        finish_one(&mut db).unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        if corrupt_count {
            let key = trash_metadata_key(&tx, "retired").unwrap();
            let mut metadata: Value =
                serde_json::from_slice(&db.entity_get(&mut tx, &key).unwrap().unwrap()).unwrap();
            metadata["turnCount"] = json!(0);
            db.entity_put(&mut tx, key, serde_json::to_vec(&metadata).unwrap())
                .unwrap();
        } else {
            let key = trash_age_index_key(&tx, "retired", 900).unwrap();
            db.entity_delete(&mut tx, key).unwrap();
        }
        db.commit(tx).unwrap();
        assert!(get(&db, 7, 11, "retired").is_err());
        assert!(command(&mut db, "migration.trash.begin", source()).is_err());
    }
}
