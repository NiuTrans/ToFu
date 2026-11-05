//! Attempt-event retention as exact point deletes: prepared commits rebase
//! beside unrelated writes, stay inside the overlay envelope, and a row that
//! becomes referenced or live between preparation and apply is never deleted.
use super::*;
use super::tests::{append_live_attempt, live_projection_request};

const TENANT: u64 = 7;
const OWNER: u64 = 11;
const ATTEMPT: &str = "prune-attempt";
const CONVERSATION: &str = "prune-conversation";

fn request(max_rows: usize) -> EventPruneRequest {
    EventPruneRequest { settled_before_ms: 10, max_attempts: 16, max_rows }
}

fn decoded(bytes: Vec<u8>) -> Value {
    serde_json::from_slice(&bytes).unwrap()
}

/// A deferred write after any merge republishes the root directory, which
/// tombstone reclaim makes part of every range witness. Steady state between
/// merges starts after it, exactly as in production.
fn unrelated_write(database: &mut AuthorityDatabase, label: &str) {
    let mut write = database.begin(TENANT, OWNER).unwrap();
    database.entity_put(&mut write,
        EntityKey::new(TENANT, OWNER, "prune-unrelated", label.as_bytes()).unwrap(),
        label.as_bytes().to_vec()).unwrap();
    database.commit(write).unwrap();
}

/// One settled Attempt with three events and every sync reference released,
/// on the production write path (overlay, tombstone reclaim).
fn settled_unreferenced_attempt(directory: &std::path::Path) -> AuthorityDatabase {
    let mut database = AuthorityDatabase::initialize(directory).unwrap();
    database.set_write_overlay_for_test(true);
    database.set_entity_tombstone_reclaim_override_for_test(Some(true));
    let mut seed = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    append_live_attempt(&database, &mut seed, CONVERSATION, "prune-turn", ATTEMPT);
    database.commit(seed).unwrap();
    let mut bind = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    attempt_bind(&database, &mut bind, ATTEMPT, "projection-head-task", "", 2).unwrap();
    database.commit(bind).unwrap();
    let mut live = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    event_record(&database, &mut live, &live_projection_request(ATTEMPT, "partial", 3)).unwrap();
    database.commit(live).unwrap();
    let mut settle = live_projection_request(ATTEMPT, "final", 4);
    settle.terminal = true;
    settle.status = "completed".to_owned();
    settle.settlement = json!({"outcome": "completed", "cause": "provider_finished",
        "resumeOptions": []}).as_object().unwrap().clone();
    let mut terminal = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    event_record(&database, &mut terminal, &settle).unwrap();
    database.commit(terminal).unwrap();
    let mut release = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    sync_prune(&database, &mut release,
        &SyncPruneRequest { created_before_ms: 10, max_rows: 100 }).unwrap();
    database.commit(release).unwrap();
    unrelated_write(&mut database, "steady-state");
    database
}

fn prepare(database: &AuthorityDatabase, request: &EventPruneRequest) -> (AuthorityTransaction, Value) {
    let replica = database.build_preparation_replica().unwrap();
    let mut prepared = replica.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    let response = decoded(events_prune(&replica, &mut prepared, request).unwrap());
    // The transaction keeps its own snapshot pin after the replica retires.
    drop(replica);
    (prepared, response)
}

fn retained_events(database: &AuthorityDatabase) -> usize {
    let mut read = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    decoded(events_list(database, &mut read, ATTEMPT, 0, 100, true, &mut || false).unwrap().unwrap())
        .as_array().unwrap().len()
}

fn event_head(database: &AuthorityDatabase) -> u64 {
    let mut read = database.begin(TENANT, OWNER).unwrap();
    let key = attempt_event_head_key(&read, ATTEMPT).unwrap();
    decode_u64(database.entity_get(&mut read, &key).unwrap(), "head").unwrap()
}

#[test]
fn prepared_attempt_event_prune_rebases_beside_unrelated_writes() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = settled_unreferenced_attempt(directory.path());
    let (prepared, response) = prepare(&database, &request(4_096));
    assert_eq!(response, json!({"deleted_rows": 3, "deleted_attempts": 1, "remaining": 0}));
    assert_eq!(retained_events(&database), 3, "preparation published nothing");
    unrelated_write(&mut database, "concurrent");
    // A structural range retirement refuses any moved root; point deletes
    // validate only their own witnesses and commit after an unrelated write.
    database.commit(prepared).unwrap();
    assert_eq!(retained_events(&database), 0);
    assert_eq!(event_head(&database), 0);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}

#[test]
fn prepared_attempt_event_prune_never_deletes_rows_that_became_referenced() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = settled_unreferenced_attempt(directory.path());
    let (prepared, response) = prepare(&database, &request(4_096));
    assert_eq!(response["deleted_rows"], 3);
    let mut reference = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    reference_attempt_events_for_test(&database, &mut reference, ATTEMPT, CONVERSATION, 99, 2).unwrap();
    database.commit(reference).unwrap();
    let refused = database.commit(prepared).err().expect("a stale prune was published");
    assert_eq!(refused.kind(), io::ErrorKind::WouldBlock, "{refused}");
    assert_eq!(retained_events(&database), 3);
    assert_eq!(event_head(&database), 3);
    // The writer retry evaluates the same predicate on current state.
    let mut retry = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    assert_eq!(decoded(events_prune(&database, &mut retry, &request(4_096)).unwrap()),
        json!({"deleted_rows": 0, "deleted_attempts": 0, "remaining": 1}));
    assert!(!retry.has_business_mutation());
    drop(retry);
    assert_eq!(retained_events(&database), 3);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}

/// Rewrite one stored entity with its own bytes: a new committed version of
/// exactly the state the prepared prune read.
fn rewrite(database: &mut AuthorityDatabase, key: impl FnOnce(&mut AuthorityTransaction,
    &AuthorityDatabase) -> EntityKey) {
    let mut write = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    let key = key(&mut write, database);
    let stored = database.entity_get(&mut write, &key).unwrap().expect("witnessed entity exists");
    database.entity_put(&mut write, key, stored).unwrap();
    database.commit(write).unwrap();
}

#[test]
fn prepared_attempt_event_prune_never_publishes_over_a_changed_prunability_input() {
    // Everything that decides whether a row is prunable, and every prepared
    // row itself, is a witness of the prepared transaction.
    type Change = fn(&mut AuthorityDatabase);
    let changes: [(&str, Change); 6] = [
        ("reference", |database| {
            let mut reference = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
            reference_attempt_events_for_test(database, &mut reference, ATTEMPT, CONVERSATION, 99, 2).unwrap();
            database.commit(reference).unwrap();
        }),
        ("attempt", |database| rewrite(database, |tx, database|
            load_attempt_for_update(database, tx, ATTEMPT).unwrap().unwrap().key)),
        ("owning turn", |database| rewrite(database, |tx, _|
            turn_key(tx, CONVERSATION, "prune-turn").unwrap())),
        ("retention cursor", |database| rewrite(database, |tx, database| {
            let attempt = load_attempt_for_update(database, tx, ATTEMPT).unwrap().unwrap();
            let settled = terminal_attempt_settled_at(&attempt.document).unwrap().unwrap();
            attempt_event_retention_index_key(tx, settled, ATTEMPT).unwrap()
        })),
        ("event head", |database| rewrite(database, |tx, _| attempt_event_head_key(tx, ATTEMPT).unwrap())),
        ("prepared row", |database| rewrite(database, |tx, _| attempt_event_key(tx, ATTEMPT, 2).unwrap())),
    ];
    for (label, change) in changes {
        let directory = tempfile::tempdir().unwrap();
        let mut database = settled_unreferenced_attempt(directory.path());
        let (prepared, response) = prepare(&database, &request(4_096));
        assert_eq!(response["deleted_rows"], 3, "{label}");
        change(&mut database);
        let refused = database.commit(prepared).err()
            .unwrap_or_else(|| panic!("a stale prune was published over a changed {label}"));
        assert_eq!(refused.kind(), io::ErrorKind::WouldBlock, "{label}: {refused}");
        assert_eq!(retained_events(&database), 3, "{label}");
        assert_eq!(event_head(&database), 3, "{label}");
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0, "{label}");
    }
}

#[test]
fn a_prepared_prune_candidate_can_never_become_live_again() {
    // The live predicate inputs are the Attempt status and the owning Turn's
    // live projection head, which requires a live Turn and its live current
    // Attempt. A terminal Attempt refuses reactivation, so a settled stream
    // can only gain protection through a retained sync reference.
    let directory = tempfile::tempdir().unwrap();
    let database = settled_unreferenced_attempt(directory.path());
    let (prepared, _) = prepare(&database, &request(4_096));
    let mut revive = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
    let loaded = load_attempt_for_update(&database, &mut revive, ATTEMPT).unwrap().unwrap();
    let mut live = loaded.document.clone();
    live.insert("status".to_owned(), Value::String("running".to_owned()));
    live.remove("settledAt");
    let refused = store_attempt_document(&database, &mut revive, ATTEMPT, &loaded, &live, 5)
        .err().expect("a terminal Attempt became live");
    assert_eq!(refused.to_string(), "terminal attempt cannot become live");
    drop(revive);
    drop(prepared);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}

#[test]
fn attempt_event_point_deletes_stop_at_the_overlay_envelope_and_resume() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = settled_unreferenced_attempt(directory.path());
    // Retention never decodes event values, so synthetic rows extend the stream.
    let total = 6_000_u64;
    let mut extend = database.begin(TENANT, OWNER).unwrap();
    for sequence in 4..=total {
        let key = attempt_event_key(&extend, ATTEMPT, sequence).unwrap();
        database.entity_put(&mut extend, key, b"{}".to_vec()).unwrap();
    }
    let head_key = attempt_event_head_key(&extend, ATTEMPT).unwrap();
    database.entity_put(&mut extend, head_key, total.to_le_bytes().to_vec()).unwrap();
    database.commit(extend).unwrap();
    unrelated_write(&mut database, "extended");
    let (mut deleted, mut transactions) = (0, 0);
    loop {
        let mut prune = database.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let response = decoded(events_prune(&database, &mut prune,
            &request(crate::generated_tofudb_ir::MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION)).unwrap());
        // The IR still attaches its receipt to the same overlay delta.
        assert!(database.entity_overlay_delta_room(&prune) >= max_receipt_write_bytes(&prune).unwrap(),
            "{response}");
        database.commit(prune).unwrap();
        transactions += 1;
        deleted += response["deleted_rows"].as_u64().unwrap();
        if response["remaining"] == 0 {
            assert_eq!(response["deleted_attempts"], 1);
            break;
        }
        assert_eq!(response, json!({"deleted_rows": response["deleted_rows"],
            "deleted_attempts": 0, "remaining": 1}));
        assert!(transactions < 10, "retention made no progress: {response}");
    }
    assert_eq!(deleted, total);
    assert!(transactions > 1, "one transaction cannot carry {total} point deletes");
    assert_eq!(retained_events(&database), 0);
    assert_eq!(event_head(&database), 0);
}
