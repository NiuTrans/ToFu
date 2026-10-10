//! Physical claim and revocation authority regressions; no live storage.
use super::*;
use crate::vfs::{DeterministicVfs, Vfs};
use std::path::Path;
use std::sync::Arc;

fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/integration-claims")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database = AuthorityDatabase::initialize_with_vfs(
        Path::new("/integration-claims"), vfs.clone(),
    ).unwrap();
    (vfs, database)
}
fn registration(owner: u64, task: &str, path: &str) -> Value {
    json!({"user_id":owner,"project_root":"/repo","task_id":task,
        "workspace_path":path,"managed":true,"base_sha":"base","now":1.0})
}
fn request(operation: &str, payload: Value) -> Request {
    Request { operation: format!("integration.workspace.{operation}"),
        payload: payload.as_object().unwrap().clone(), now_ms: 1000 }
}
fn call(database: &mut AuthorityDatabase, operation: &str, payload: Value) -> io::Result<Value> {
    let owner = payload["user_id"].as_u64().unwrap_or(11);
    let mut tx = database.begin_with_identity_claim_scopes(7, owner)?;
    let req = request(operation, payload);
    let result = execute(database, &mut tx, &req)?.unwrap();
    if req.mutates_state() && tx.has_business_mutation() { database.commit(tx)?; }
    Ok(serde_json::from_slice(&result).unwrap())
}

// Seed old-format rows without the new claim invariant to exercise upgrade
// admission. All transactions stay under ordinary engine limits.
fn seed_history(database: &mut AuthorityDatabase, count: u64, with_claims: bool) {
    for start in (1..=count).step_by(64) {
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        if with_claims { ensure_checkout_claims(database, &mut tx).unwrap(); }
        for id in start..=(start + 63).min(count) {
            let workspace = Workspace {
                id, owner_user_id:11, project_root:"/repo".into(), task_id:format!("task-{id}"),
                title:String::new(), workspace_path:format!("/checkout/{id}"), managed:true,
                state:"running".into(), base_sha:"base".into(), checkpoint_sha:String::new(),
                candidate_sha:String::new(), error:String::new(), origin_raw:"{}".into(),
                created_at:1.0, updated_at:1.0,
            };
            if with_claims {
                write_workspace(database, &mut tx, None, &workspace).unwrap();
            } else {
                let key = workspace_key(&tx, 11, "/repo", &workspace.task_id).unwrap();
                database.entity_put(&mut tx, key, encode(&workspace, "workspace").unwrap()).unwrap();
                install_workspace_indexes(database, &mut tx, &workspace).unwrap();
            }
            let key = row_locator_key(&tx, id).unwrap();
            database.entity_put(&mut tx, key, encode(&locator(&workspace), "locator").unwrap()).unwrap();
            let key = natural_claim_key(&tx, 11, "/repo", &workspace.task_id).unwrap();
            write_u64(database, &mut tx, key, id).unwrap();
        }
        let key = owner_count_key(&tx, 11).unwrap();
        write_u64(database, &mut tx, key, (start + 63).min(count)).unwrap();
        let key = sequence_key(&tx, INTEGRATION_ROW_SEQUENCE_NAMESPACE).unwrap();
        write_u64(database, &mut tx, key, (start + 63).min(count)).unwrap();
        database.commit(tx).unwrap();
    }
}

#[test]
fn checkout_claim_registration_exceeds_old_6144_witness_history_and_survives_reopen() {
    let (vfs, mut database) = prepared();
    seed_history(&mut database, 3200, true);
    call(&mut database, "register", registration(12, "new", "/checkout/new")).unwrap();
    assert_eq!(call(&mut database, "register", registration(12, "steal", "/checkout/3200"))
        .unwrap_err().kind(), io::ErrorKind::AlreadyExists);
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/integration-claims"), vfs).unwrap();
    call(&mut database, "register", registration(12, "after-reopen", "/checkout/reopen")).unwrap();
    assert_eq!(call(&mut database, "register", registration(13, "steal", "/checkout/new"))
        .unwrap_err().kind(), io::ErrorKind::AlreadyExists);
}

#[test]
fn checkout_legacy_bootstrap_is_complete_or_fails_closed_at_fixed_bound() {
    for count in [128, 129] {
        let (_vfs, mut database) = prepared();
        seed_history(&mut database, count, false);
        let result = call(&mut database, "register", registration(12, "new", "/checkout/new"));
        if count == 128 {
            result.unwrap();
            assert_eq!(call(&mut database, "register", registration(12, "steal", "/checkout/128"))
                .unwrap_err().kind(), io::ErrorKind::AlreadyExists);
        } else {
            let error = result.unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
            assert!(error.to_string().contains("bounded offline backfill"));
            let mut tx = database.begin_with_identity_claim_scopes(7, 12).unwrap();
            let key = checkout_claims_ready_key(&tx).unwrap();
            assert!(database.entity_get(&mut tx, &key).unwrap().is_none());
            assert!(read_workspace(&database, &mut tx, 12, "/repo", "new").unwrap().is_none());
        }
    }
}

#[test]
fn checkout_concurrent_owners_cannot_commit_the_same_path() {
    let (_vfs, mut database) = prepared();
    call(&mut database, "register", registration(11, "bootstrap", "/checkout/bootstrap")).unwrap();
    let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let mut second = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    execute(&database, &mut first, &request("register", registration(11, "one", "/checkout/shared"))).unwrap();
    execute(&database, &mut second, &request("register", registration(12, "two", "/checkout/shared"))).unwrap();
    database.commit(first).unwrap();
    assert!(database.commit(second).is_err());
    assert_eq!(call(&mut database, "register", registration(12, "two", "/checkout/shared"))
        .unwrap_err().kind(), io::ErrorKind::AlreadyExists);
}

#[test]
fn checkout_foreign_claim_is_never_released_on_discard() {
    let (_vfs, mut database) = prepared();
    call(&mut database, "register", registration(11, "one", "/checkout/one")).unwrap();
    call(&mut database, "register", registration(12, "two", "/checkout/two")).unwrap();
    // Inject inconsistent ownership; a mutation must fail without deleting it.
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let one = checkout_claim_key(&tx, "/checkout/one").unwrap();
    let two = checkout_claim_key(&tx, "/checkout/two").unwrap();
    let foreign = database.entity_get(&mut tx, &two).unwrap().unwrap();
    database.entity_put(&mut tx, one.clone(), foreign.clone()).unwrap();
    database.commit(tx).unwrap();
    assert!(call(&mut database, "discard", registration(11, "one", "/checkout/one")).is_err());
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(database.entity_get(&mut tx, &one).unwrap(), Some(foreign));
    assert_eq!(read_workspace(&database, &mut tx, 11, "/repo", "one").unwrap().unwrap().state, "running");
}

#[test]
fn denied_legacy_ready_and_stale_integrating_are_not_claimable() {
    for state in ["ready", "integrating"] {
        let (_vfs, mut database) = prepared();
        let input = registration(11, "revoked", "/checkout/revoked");
        call(&mut database, "register", input.clone()).unwrap();
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let old = read_workspace(&database, &mut tx, 11, "/repo", "revoked").unwrap().unwrap();
        let mut revoked = old.clone();
        revoked.state = state.into();
        revoked.checkpoint_sha = "checkpoint".into();
        revoked.origin_raw = r#"{"delivery":{"taskOutcome":"cancelled"}}"#.into();
        write_workspace(&database, &mut tx, Some(&old), &revoked).unwrap();
        database.commit(tx).unwrap();
        assert_eq!(call(&mut database, "peek_ready", json!({"now":1000.0})).unwrap(), Value::Null);
        assert_eq!(call(&mut database, "claim_next", json!({"now":1000.0})).unwrap(), Value::Null);
        assert_eq!(call(&mut database, "get_integrating", json!({"row_id":old.id})).unwrap(), Value::Null);
        let error = call(&mut database, "submit", input.clone()).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
    }
}

#[test]
fn denial_survives_restart_and_conflicts_with_inflight_claim_transaction() {
    let (vfs, mut database) = prepared();
    let input = registration(11, "revoked", "/checkout/revoked");
    call(&mut database, "register", input.clone()).unwrap();
    let mut checkpoint = input.clone();
    checkpoint["checkpoint_sha"] = json!("checkpoint");
    call(&mut database, "save_checkpoint", checkpoint).unwrap();
    call(&mut database, "submit", input.clone()).unwrap();
    let mut inflight = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let claim = execute(&database, &mut inflight, &request("claim_next", json!({"now":2.0}))).unwrap();
    assert!(serde_json::from_slice::<Value>(&claim.unwrap()).unwrap().is_object());
    let mut revoked = input.clone();
    revoked["patch_json"] = json!(r#"{"delivery":{"taskOutcome":"cancelled"}}"#);
    revoked["now"] = json!(3.0);
    call(&mut database, "set_meta", revoked.clone()).unwrap();
    assert!(database.commit(inflight).is_err());
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/integration-claims"), vfs).unwrap();
    for patch in [r#"{"delivery":{}}"#, r#"{"delivery":{"taskOutcome":"completed"}}"#] {
        revoked["patch_json"] = json!(patch);
        call(&mut database, "set_meta", revoked.clone()).unwrap();
    }
    assert_eq!(call(&mut database, "get", input.clone()).unwrap()["origin"]["delivery"]["taskOutcome"], "cancelled");
    for operation in ["register", "submit", "retry"] {
        assert_eq!(call(&mut database, operation, input.clone()).unwrap_err().kind(), io::ErrorKind::AlreadyExists);
    }
    assert_eq!(call(&mut database, "claim_next", json!({"now":1000.0})).unwrap(), Value::Null);
}

#[test]
fn checkout_claim_supports_paths_larger_than_entity_keys() {
    let (_vfs, mut database) = prepared();
    let path = format!("/{}", "x".repeat(4095));
    call(&mut database, "register", registration(11, "long", &path)).unwrap();
    assert_eq!(call(&mut database, "register", registration(12, "other", &path))
        .unwrap_err().kind(), io::ErrorKind::AlreadyExists);
}

#[test]
fn delivery_page_stays_a_read_only_internal_query() {
    let (_vfs, mut database) = prepared();
    call(&mut database, "register", registration(11, "row", "/checkout/row")).unwrap();
    let request = request("delivery_page", json!({"after_id":0,"limit":128}));
    assert!(!request.mutates_state());
    let mut tx = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    let result = execute(&database, &mut tx, &request).unwrap().unwrap();
    assert!(!tx.has_business_mutation());
    let page: Value = serde_json::from_slice(&result).unwrap();
    assert_eq!(page["rows"][0]["user_id"], 11);
    assert_eq!(page["hasMore"], false);
}

fn backfill_state(database: &AuthorityDatabase) -> (u64, u64) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let cursor = checkout_claims_cursor_key(&tx).unwrap();
    let ready = checkout_claims_ready_key(&tx).unwrap();
    (read_u64(database, &mut tx, &cursor, "cursor").unwrap(),
        read_u64(database, &mut tx, &ready, "ready").unwrap())
}

#[test]
fn offline_checkout_backfill_migrates_3200_legacy_rows_across_reopens() {
    let (vfs, mut database) = prepared();
    seed_history(&mut database, 3200, false);
    let mut processed = 0;
    let mut rounds = 0;
    loop {
        let page = call(&mut database, "delivery_page", json!({"after_id":processed,"limit":128})).unwrap();
        let ids: Vec<u64> = page["rows"].as_array().unwrap().iter()
            .map(|row| row["id"].as_u64().unwrap()).collect();
        assert_eq!(ids, ((processed as u64 + 1)..=(processed as u64 + 128).min(3200)).collect::<Vec<_>>(),
            "recovery pagination lost rows after batch {rounds}");
        let progress = database.backfill_integration_checkout_claims(7, 12, 128).unwrap();
        assert!(progress.processed_rows <= 128);
        assert!(progress.committed);
        processed += progress.processed_rows;
        rounds += 1;
        assert_eq!(progress.after_id, processed as u64);
        if progress.complete { break; }
        assert_eq!(call(&mut database, "register", registration(12, "new", "/checkout/new"))
            .unwrap_err().kind(), io::ErrorKind::OutOfMemory);
        assert_eq!(call(&mut database, "discard", registration(11, "task-1", "/checkout/1"))
            .unwrap_err().kind(), io::ErrorKind::OutOfMemory);
        drop(database);
        vfs.crash().unwrap();
        database = AuthorityDatabase::open_with_vfs(Path::new("/integration-claims"), vfs.clone()).unwrap();
        assert_eq!(backfill_state(&database), (processed as u64, 0));
    }
    assert_eq!((processed, rounds), (3200, 26)); // full batches require an empty-suffix proof
    assert_eq!(backfill_state(&database), (3200, 1));
    let repeated = database.backfill_integration_checkout_claims(7, 13, 1).unwrap();
    assert!(repeated.complete && !repeated.committed);
    assert_eq!(repeated.processed_rows, 0);
    assert_eq!(database.backfill_integration_checkout_claims(7, 13, 128).unwrap(), repeated);
    call(&mut database, "register", registration(12, "new", "/checkout/new")).unwrap();
    for id in [1, 128, 129, 3200] {
        assert_eq!(call(&mut database, "register", registration(13, "steal", &format!("/checkout/{id}")))
            .unwrap_err().kind(), io::ErrorKind::AlreadyExists);
    }
    // A separate tenant's empty catalog never certifies this tenant's cursor.
    let separate = database.backfill_integration_checkout_claims(8, 11, 128).unwrap();
    assert!(separate.complete);
    assert_eq!(separate.after_id, 0);
    assert_eq!(backfill_state(&database), (3200, 1));
}

#[test]
fn offline_checkout_backfill_rejects_duplicate_history_without_advancing_or_erasing_claims() {
    for duplicate_id in [2, 130] { // both same-batch and cross-batch conflicts
        let (_vfs, mut database) = prepared();
        seed_history(&mut database, 130, false);
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let mut duplicate = read_workspace_by_id(&database, &mut tx, duplicate_id).unwrap().unwrap();
        duplicate.workspace_path = "/checkout/1".into();
        let key = workspace_key(&tx, 11, "/repo", &duplicate.task_id).unwrap();
        database.entity_put(&mut tx, key, encode(&duplicate, "workspace").unwrap()).unwrap();
        database.commit(tx).unwrap();
        if duplicate_id > 128 {
            database.backfill_integration_checkout_claims(7, 12, 128).unwrap();
        }
        let before = backfill_state(&database);
        for _ in 0..2 {
            assert_eq!(database.backfill_integration_checkout_claims(7, 12, 128).unwrap_err().kind(),
                io::ErrorKind::AlreadyExists);
            assert_eq!(backfill_state(&database), before);
        }
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let key = checkout_claim_key(&tx, "/checkout/1").unwrap();
        let claim = database.entity_get(&mut tx, &key).unwrap();
        if duplicate_id > 128 {
            assert_eq!(decode::<CheckoutClaim>(&claim.unwrap(), "claim").unwrap().locator.id, 1);
            let uncommitted = checkout_claim_key(&tx, "/checkout/129").unwrap();
            assert!(database.entity_get(&mut tx, &uncommitted).unwrap().is_none());
        } else { assert!(claim.is_none()); }
    }
}

#[test]
fn offline_checkout_backfill_never_overwrites_foreign_claim_and_allows_discarded_history() {
    let (_vfs, mut database) = prepared();
    seed_history(&mut database, 129, false);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let foreign = CheckoutClaim { workspace_path: "/checkout/129".into(), locator: Locator {
        id: 900, owner_user_id: 12, project_root: "/foreign".into(), task_id: "foreign".into(),
    }};
    let key = checkout_claim_key(&tx, "/checkout/129").unwrap();
    let bytes = encode(&foreign, "claim").unwrap();
    database.entity_put(&mut tx, key.clone(), bytes.clone()).unwrap();
    database.commit(tx).unwrap();
    database.backfill_integration_checkout_claims(7, 11, 128).unwrap();
    assert_eq!(database.backfill_integration_checkout_claims(7, 11, 128).unwrap_err().kind(),
        io::ErrorKind::AlreadyExists);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(database.entity_get(&mut tx, &key).unwrap(), Some(bytes.clone()));
    assert_eq!(backfill_state(&database), (128, 0));
    // Offline historical fixture: a discarded predecessor must not erase the
    // retained foreign claim. No automatic conflict repair is provided.
    let mut row = read_workspace_by_id(&database, &mut tx, 129).unwrap().unwrap();
    row.state = "discarded".into();
    let row_key = workspace_key(&tx, 11, "/repo", &row.task_id).unwrap();
    database.entity_put(&mut tx, row_key, encode(&row, "workspace").unwrap()).unwrap();
    database.commit(tx).unwrap();
    assert!(database.backfill_integration_checkout_claims(7, 11, 128).unwrap().complete);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(database.entity_get(&mut tx, &key).unwrap(), Some(bytes));
}

#[test]
fn offline_checkout_backfill_bounds_scope_and_partial_fence_do_not_relax_live_admission() {
    let (_vfs, mut database) = prepared();
    seed_history(&mut database, 2, false);
    for (tenant, owner, rows) in [(0, 11, 128), (7, 0, 128), (7, 11, 0), (7, 11, 129)] {
        assert_eq!(database.backfill_integration_checkout_claims(tenant, owner, rows).unwrap_err().kind(),
            io::ErrorKind::InvalidInput);
    }
    assert_eq!(backfill_state(&database), (0, 0));
    database.backfill_integration_checkout_claims(7, 11, 1).unwrap();
    assert_eq!(call(&mut database, "register", registration(12, "new", "/checkout/new"))
        .unwrap_err().kind(), io::ErrorKind::OutOfMemory);
    assert_eq!(backfill_state(&database), (1, 0));
    assert!(database.backfill_integration_checkout_claims(7, 11, 128).unwrap().complete);
}

#[test]
fn offline_checkout_backfill_commit_conflicts_are_atomic() {
    let (_vfs, mut database) = prepared();
    seed_history(&mut database, 129, false);
    let mut inflight = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let staged = backfill_checkout_claims(&database, &mut inflight, 128).unwrap();
    assert_eq!(staged.after_id, 128);
    // Competing maintenance publishes the same progress; stale batch cannot commit.
    database.backfill_integration_checkout_claims(7, 12, 128).unwrap();
    assert!(database.commit(inflight).is_err());
    assert_eq!(backfill_state(&database), (128, 0));
    let mut inflight = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(backfill_checkout_claims(&database, &mut inflight, 128).unwrap().complete);
    let mut other = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    let foreign = CheckoutClaim { workspace_path: "/checkout/129".into(), locator: Locator {
        id: 900, owner_user_id: 12, project_root: "/other".into(), task_id: "other".into(),
    }};
    let key = checkout_claim_key(&other, "/checkout/129").unwrap();
    let bytes = encode(&foreign, "claim").unwrap();
    database.entity_put(&mut other, key.clone(), bytes.clone()).unwrap();
    database.commit(other).unwrap();
    assert!(database.commit(inflight).is_err());
    assert_eq!(backfill_state(&database), (128, 0));
    let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(database.entity_get(&mut read, &key).unwrap(), Some(bytes));
}

#[test]
fn offline_checkout_backfill_every_commit_fault_recovers_atomic_cursor_claim_and_readiness() {
    use crate::vfs::{FaultAction, FaultRule, Operation};
    for finishing in [false, true] {
        let (seed, mut database) = prepared();
        seed_history(&mut database, 129, false);
        if finishing { database.backfill_integration_checkout_claims(7, 11, 128).unwrap(); }
        drop(database);
        let baseline_vfs = Arc::new(seed.fork_durable().unwrap());
        let mut baseline = AuthorityDatabase::open_with_vfs(
            Path::new("/integration-claims"), baseline_vfs.clone(),
        ).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.backfill_integration_checkout_claims(7, 11, 128).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
            if matches!(operation, Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let mut database = AuthorityDatabase::open_with_vfs(
                    Path::new("/integration-claims"), vfs.clone(),
                ).unwrap();
                vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
                let acknowledged = database.backfill_integration_checkout_claims(7, 11, 128).is_ok();
                drop(database);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut database = AuthorityDatabase::open_with_vfs(
                    Path::new("/integration-claims"), vfs,
                ).unwrap();
                let before = if finishing { (128, 0) } else { (0, 0) };
                let after = if finishing { (129, 1) } else { (128, 0) };
                let recovered = backfill_state(&database);
                assert!(recovered == before || recovered == after, "{operation:?} {action:?}");
                if acknowledged { assert_eq!(recovered, after); }
                let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
                for id in [1, 128, 129] {
                    let key = checkout_claim_key(&read, &format!("/checkout/{id}")).unwrap();
                    assert_eq!(database.entity_get(&mut read, &key).unwrap().is_some(), id <= recovered.0);
                }
                if recovered.1 == 0 {
                    assert_eq!(call(&mut database, "register", registration(12, "new", "/checkout/new"))
                        .unwrap_err().kind(), io::ErrorKind::OutOfMemory);
                }
                while !database.backfill_integration_checkout_claims(7, 11, 128).unwrap().complete {}
                assert_eq!(backfill_state(&database), (129, 1));
            }
        }
    }
}
