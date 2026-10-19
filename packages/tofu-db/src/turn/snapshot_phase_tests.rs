use super::*;

fn seed(database: &mut AuthorityDatabase) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(database, &mut tx, &AppendSettledRequest {
        conversation_id: "phase-conv".into(), actor: "assistant".into(),
        status: "pending".into(), projection_json: b"{}".to_vec(),
        projection_timestamp: None, settlement_json: b"{}".to_vec(), lane_id: "main".into(),
        command_id: "phase-seed".into(), kind: "reply".into(), run_id: String::new(),
        turn_id: "phase-turn".into(), attempt_id: Some("phase-attempt".into()),
        created_at_ms: 1000, committed_at_ms: 1000,
        defaults: TurnDefaults {allow_create: true, title: "Phase".into(),
            settings_json: b"{}".to_vec(), created_at_ms: 1000},
    }).unwrap();
    attempt_bind(database, &mut tx, "phase-attempt", "phase-task", "", 1100).unwrap();
    attempt_start(database, &mut tx, "phase-attempt", "phase-task", 1200).unwrap();
    database.commit(tx).unwrap();
}

fn snapshot(database: &AuthorityDatabase, tx: &mut AuthorityTransaction) -> Value {
    serde_json::from_slice(&sync_snapshot(database, tx, "phase-conv", 96, 65536,
        false, &mut || false).unwrap().unwrap()).unwrap()
}

fn emit(database: &mut AuthorityDatabase, payload: Value, revision: u64) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_attempt_event(database, &mut tx, AttemptEventAppend {
        conversation_id: "phase-conv", turn_id: "phase-turn", attempt_id: "phase-attempt",
        projection_revision: revision, event_type: "phase", payload,
        occurred_at_ms: 1300, publish_conversation_sync: true,
    }).unwrap();
    database.commit(tx).unwrap();
}

#[test]
fn native_snapshot_restores_current_phase_and_latest_clear_never_revives_history() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let initial = snapshot(&db, &mut tx);
    let revision = initial["turns"][0]["projectionRevision"].as_u64().unwrap();
    drop(tx);
    let phase = json!({"phase": "working", "detailKey": "stream.phase.workerStarting"});
    emit(&mut db, json!({"phase": phase}), revision);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let restored = snapshot(&db, &mut tx);
    assert_eq!(restored["activeAttemptPhase"], json!({"turnId": "phase-turn",
        "attemptId": "phase-attempt", "projectionRevision": revision, "phase": phase}));
    drop(tx);
    for body in [json!({"phase": null}), json!({}),
        json!({"phase": {"phase": "working", "detail": "x".repeat(40_000)}})] {
        emit(&mut db, body, revision);
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(snapshot(&db, &mut tx).get("activeAttemptPhase").is_none());
    }
    emit(&mut db, json!({"phase": phase}), revision + 1);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(snapshot(&db, &mut tx).get("activeAttemptPhase").is_none());
    let mut foreign = db.begin_with_identity_claim_scopes(7, 12).unwrap();
    assert!(sync_snapshot(&db, &mut foreign, "phase-conv", 96, 65536, false,
        &mut || false).unwrap().is_none());
}

#[test]
fn native_snapshot_phase_missing_head_and_noncurrent_attempt_are_not_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db);
    emit(&mut db, json!({"phase": {"phase": "waiting_model"}}), 0);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let current = snapshot(&db, &mut tx);
    let turns = current["turns"].as_array().unwrap();
    let attempts = &current["attempts"];
    for status in ["completed", "superseded", "interrupted", "pending"] {
        let mut changed = attempts.clone();
        changed[0]["status"] = json!(status);
        assert!(active_attempt_phase(&db, &mut tx, "phase-conv", turns, &changed).unwrap().is_none());
    }
    for (key, value) in [("status", "completed"), ("laneId", "child"), ("currentAttemptId", "other")] {
        let mut changed = turns.clone();
        changed[0][key] = json!(value);
        assert!(active_attempt_phase(&db, &mut tx, "phase-conv", &changed, attempts).unwrap().is_none());
    }
    let head_key = attempt_event_head_key(&tx, "phase-attempt").unwrap();
    let head = decode_u64(db.entity_get(&mut tx, &head_key).unwrap(), "head").unwrap();
    let event_key = attempt_event_key(&tx, "phase-attempt", head).unwrap();
    db.entity_delete(&mut tx, event_key).unwrap();
    assert!(snapshot(&db, &mut tx).get("activeAttemptPhase").is_none());
}
