//! Offline deletion-marker snapshots. Publish identity claim, delta key and
//! retention key together; never advance the historical conversation clock.
//! Source attempts were already deleted by SQLite, so native attempt pins are empty.
use super::super as turn;
use super::*;
use crate::generated_tofudb_ir::TURN_TOMBSTONE_AGE_INDEX_NAMESPACE;

#[derive(Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    conversation_id: String,
    turn_id: String,
    user_id: u64,
    deleted_at: u64,
}

fn keys(tx: &AuthorityTransaction, s: &Snapshot) -> io::Result<(EntityKey, EntityKey, EntityKey)> {
    if s.user_id != tx.owner_user_id()
        || s.user_id == 0
        || !bounded_identity(&s.conversation_id, 256)
        || !bounded_identity(&s.turn_id, 256)
    {
        return Err(invalid_input("invalid tombstone identity or owner"));
    }
    Ok((
        turn::tombstone_index_key(tx, &s.conversation_id, s.deleted_at, &s.turn_id)?,
        turn::tombstone_age_index_key(tx, s.deleted_at, &s.conversation_id, &s.turn_id)?,
        turn::global_identity_claim_key(tx, TURN_ID_CLAIM_NAMESPACE, &s.turn_id)?,
    ))
}

fn get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    s: &Snapshot,
) -> io::Result<Value> {
    let (delta, age, claim) = keys(tx, s)?;
    let a = database.entity_get(tx, &delta)?;
    let b = database.entity_get(tx, &age)?;
    match (a, b) {
        (None, None) => Ok(Value::Null),
        (Some(a), Some(b)) if a == b && turn::decode_tombstone_attempt_ids(&a)?.is_empty() => {
            if database.entity_get(tx, &claim)? != Some(s.user_id.to_be_bytes().to_vec())
                || database
                    .entity_get(tx, &turn::turn_key(tx, &s.conversation_id, &s.turn_id)?)?
                    .is_some()
            {
                return Err(invalid_data(
                    "tombstone identity claim or active Turn differs",
                ));
            }
            serde_json::to_value(s).map_err(|_| invalid_data("tombstone snapshot encoding failed"))
        }
        _ => Err(invalid_data("tombstone delta and retention indexes differ")),
    }
}

pub(super) fn execute(
    database: &mut AuthorityDatabase,
    mut tx: AuthorityTransaction,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    if operation == "migration.turn.tombstone.scan" {
        let request: MigrationScanRequest = serde_json::from_value(payload)
            .map_err(|_| invalid_input("invalid tombstone scan request"))?;
        let start = turn::entity_key(
            &tx,
            TURN_TOMBSTONE_AGE_INDEX_NAMESPACE,
            &cursor_start("tombstones", request.cursor.as_deref())?,
        )?;
        let (_, end) = EntityKey::prefix_range(
            tx.tenant_id(),
            tx.owner_user_id(),
            TURN_TOMBSTONE_AGE_INDEX_NAMESPACE,
            b"",
        )?;
        let page = database.entity_scan(&mut tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
        let mut rows = Vec::with_capacity(page.len());
        for (key, _) in &page {
            let (deleted_at, conversation_id, turn_id) = turn::decode_age_index_identity(key)?;
            let s = Snapshot {
                conversation_id,
                turn_id,
                deleted_at,
                user_id: tx.owner_user_id(),
            };
            rows.push(get(database, &mut tx, &s)?);
        }
        let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
            let encoded: String = page
                .last()
                .unwrap()
                .0
                .key_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            Some(format!("tombstones:{encoded}"))
        } else {
            None
        };
        return Ok(json!({"rows": rows, "cursor": cursor}));
    }
    let s: Snapshot =
        serde_json::from_value(payload).map_err(|_| invalid_input("invalid tombstone snapshot"))?;
    let observed = get(database, &mut tx, &s)?;
    if operation == "migration.turn.tombstone.get" {
        return Ok(observed);
    }
    if operation != "migration.turn.tombstone.restore" {
        return Err(invalid_input("unknown tombstone migration operation"));
    }
    if !observed.is_null() {
        return Ok(json!({"inserted": false}));
    }
    let (delta, age, claim) = keys(&tx, &s)?;
    let active = turn::turn_key(&tx, &s.conversation_id, &s.turn_id)?;
    if database.entity_get(&mut tx, &claim)?.is_some()
        || database.entity_get(&mut tx, &active)?.is_some()
    {
        return Err(snapshot_conflict());
    }
    let empty = turn::encode_tombstone_attempt_ids(&[])?;
    database.entity_put(&mut tx, claim, s.user_id.to_be_bytes().to_vec())?;
    database.entity_put(&mut tx, delta, empty.clone())?;
    database.entity_put(&mut tx, age, empty)?;
    database.commit(tx)?;
    Ok(json!({"inserted": true}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};

    #[test]
    fn tombstone_migration_reopens_replays_and_rejects_changed_identity() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let payload =
            json!({"conversation_id":"conv", "turn_id":"gone", "user_id":11, "deleted_at":123});
        assert_eq!(
            super::super::execute(
                &mut db,
                7,
                11,
                "migration.turn.tombstone.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            super::super::execute(
                &mut db,
                7,
                11,
                "migration.turn.tombstone.get",
                payload.clone()
            )
            .unwrap(),
            payload
        );
        assert_eq!(
            super::super::execute(
                &mut db,
                7,
                11,
                "migration.turn.tombstone.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            false
        );
        let mut changed = payload.clone();
        changed["deleted_at"] = json!(124);
        assert!(
            super::super::execute(&mut db, 7, 11, "migration.turn.tombstone.restore", changed)
                .is_err()
        );
        assert!(super::super::execute(
            &mut db,
            7,
            12,
            "migration.turn.tombstone.restore",
            payload.clone()
        )
        .is_err());
        let page =
            super::super::execute(&mut db, 7, 11, "migration.turn.tombstone.scan", json!({}))
                .unwrap();
        assert_eq!(page["rows"], json!([payload]));
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let delta: Value = serde_json::from_slice(
            &turn::list_delta(
                &db,
                &mut tx,
                "conv",
                0,
                &std::collections::BTreeMap::new(),
                200,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(delta["deletedTurnIds"], json!(["gone"]));
        let mut foreign = db.begin_with_identity_claim_scopes(7, 12).unwrap();
        let delta: Value = serde_json::from_slice(
            &turn::list_delta(
                &db,
                &mut foreign,
                "conv",
                0,
                &std::collections::BTreeMap::new(),
                200,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(delta["deletedTurnIds"], json!([]));
        let s: Snapshot = serde_json::from_value(payload.clone()).unwrap();
        let (_, age, _) = keys(&tx, &s).unwrap();
        db.entity_delete(&mut tx, age).unwrap();
        db.commit(tx).unwrap();
        assert!(
            super::super::execute(&mut db, 7, 11, "migration.turn.tombstone.get", payload).is_err()
        );
    }
    #[test]
    fn tombstone_migration_faults_keep_claim_delta_and_retention_atomic() {
        use crate::vfs::{FaultAction, Operation};
        let seed = Arc::new(DeterministicVfs::new(None));
        seed.create_dir(Path::new("/fault")).unwrap();
        seed.sync_directory(Path::new("/")).unwrap();
        drop(AuthorityDatabase::initialize_with_vfs(Path::new("/fault"), seed.clone()).unwrap());
        let payload =
            json!({"conversation_id":"conv", "turn_id":"gone", "user_id":11, "deleted_at":123});
        let prepare = || {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            let db = AuthorityDatabase::open_with_vfs(Path::new("/fault"), vfs.clone()).unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, db)
        };
        let (baseline, mut db) = prepare();
        super::super::execute(
            &mut db,
            7,
            11,
            "migration.turn.tombstone.restore",
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
                    let acknowledged = super::super::execute(
                        &mut db,
                        7,
                        11,
                        "migration.turn.tombstone.restore",
                        payload.clone(),
                    )
                    .is_ok();
                    if vfs.operation_fault_fired().unwrap() {
                        exercised = Some((vfs, db, acknowledged));
                        break;
                    }
                }
                let (vfs, db, acknowledged) = exercised.expect("planned fault must execute");
                vfs.arm_fault(None).unwrap();
                drop(db);
                vfs.crash().unwrap();
                let mut db = AuthorityDatabase::open_with_vfs(Path::new("/fault"), vfs).unwrap();
                let observed = super::super::execute(
                    &mut db,
                    7,
                    11,
                    "migration.turn.tombstone.get",
                    payload.clone(),
                )
                .unwrap();
                if acknowledged {
                    assert_eq!(observed, payload);
                }
                super::super::execute(
                    &mut db,
                    7,
                    11,
                    "migration.turn.tombstone.restore",
                    payload.clone(),
                )
                .unwrap();
                assert_eq!(
                    super::super::execute(
                        &mut db,
                        7,
                        11,
                        "migration.turn.tombstone.get",
                        payload.clone()
                    )
                    .unwrap(),
                    payload
                );
            }
        }
    }
}
