use super::*;

#[test]
fn byte_window_returns_newest_suffix_and_drains_every_ordinal_once() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    for index in 0..4 {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let bytes = if index == 1 { 100_000 } else { 40_000 };
        append_settled(&database, &mut transaction, &AppendSettledRequest {
            conversation_id: "byte-window".to_owned(), actor: "assistant".to_owned(),
            status: "completed".to_owned(),
            projection_json: serde_json::to_vec(&json!({"content": "界".repeat(bytes / 3)})).unwrap(),
            projection_timestamp: None, settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(), command_id: format!("byte-command-{index}"),
            kind: "reply".to_owned(), run_id: String::new(), turn_id: format!("byte-turn-{index}"),
            attempt_id: None, created_at_ms: 1, committed_at_ms: 1,
            defaults: TurnDefaults { allow_create: true, title: "Byte window".to_owned(),
                settings_json: b"{}".to_vec(), created_at_ms: 1 },
        }).unwrap();
        database.commit(transaction).unwrap();
    }
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let snapshot: Value = serde_json::from_slice(&sync_snapshot(
        &database, &mut transaction, "byte-window", 96, 65536, false, &mut || false,
    ).unwrap().unwrap()).unwrap();
    assert_eq!(snapshot["turns"].as_array().unwrap().len(), 1);
    assert_eq!(snapshot["turns"][0]["ordinal"], json!(3));
    assert_eq!(snapshot["turnWindow"]["totalTurns"], json!(4));
    assert_eq!(snapshot["turnWindow"]["hasMore"], json!(true));
    let head = snapshot["syncSequence"].as_u64().unwrap();
    let mut before = snapshot["turnWindow"]["nextBeforeOrdinal"].as_u64().unwrap();
    let mut seen = vec![3_u64];
    loop {
        let page: Value = serde_json::from_slice(&sync_page(
            &database, &mut transaction, "byte-window", "main", before, 64,
            head, 65536, &mut || false,
        ).unwrap().unwrap()).unwrap();
        let turns = page["turns"].as_array().unwrap();
        assert_eq!(turns.len(), 1); // the oversized turn still makes progress
        seen.push(turns[0]["ordinal"].as_u64().unwrap());
        if page["hasMore"] == json!(false) { break; }
        before = page["nextBeforeOrdinal"].as_u64().unwrap();
    }
    assert_eq!(seen, vec![3, 2, 1, 0]);
    let full: Value = serde_json::from_slice(&sync_snapshot(
        &database, &mut transaction, "byte-window", 96, 0, false, &mut || false,
    ).unwrap().unwrap()).unwrap();
    assert_eq!(full["turns"].as_array().unwrap().len(), 4);
    let stale: Value = serde_json::from_slice(&sync_page(
        &database, &mut transaction, "byte-window", "main", 3, 64,
        head + 1, 65536, &mut || false,
    ).unwrap().unwrap()).unwrap();
    assert_eq!(stale["stale"], json!(true));
    let mut other = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    assert!(sync_snapshot(&database, &mut other, "byte-window", 96, 65536, false,
        &mut || false).unwrap().is_none());
}

#[test]
fn round_directory_is_exact_paginated_owner_head_scoped_and_body_free() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let actors = ["assistant", "human", "assistant", "critic", "virtual_user", "assistant", "assistant"];
    for (i, actor) in actors.iter().enumerate() {
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(&db, &mut tx, &AppendSettledRequest {
            conversation_id: "ranks".into(), actor: (*actor).into(), status: "completed".into(),
            projection_json: serde_json::to_vec(&json!({"content": "界".repeat(40_000)})).unwrap(),
            projection_timestamp: None, settlement_json: b"{}".to_vec(), lane_id: "main".into(),
            command_id: format!("rank-command-{i}"), kind: "reply".into(), run_id: String::new(),
            turn_id: format!("rank-turn-{i}"), attempt_id: None, created_at_ms: 1, committed_at_ms: 1,
            defaults: TurnDefaults { allow_create: true, title: "Ranks".into(), settings_json: b"{}".to_vec(), created_at_ms: 1 },
        }).unwrap();
        db.commit(tx).unwrap();
    }
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let head = sync_head(&db, &mut tx, "ranks").unwrap();
    // A body reader would now fail; compact directory must remain functional.
    let key = turn_key(&tx, "ranks", "rank-turn-1").unwrap();
    db.entity_put(&mut tx, key, b"invalid-body".to_vec()).unwrap();
    let page: Value = serde_json::from_slice(&sync_round_directory(&db, &mut tx,
        "ranks", "main", 1, 1, head, "rank-turn-6", &mut || false).unwrap().unwrap()).unwrap();
    assert_eq!(page["totalRounds"], 3);
    assert_eq!(page["entries"][0]["turnId"], "rank-turn-3");
    assert_eq!(page["entries"][0]["roundNumber"], 2);
    assert_eq!(page["activeRound"], 3);
    assert_eq!(page["nextOffset"], 2);
    let stale: Value = serde_json::from_slice(&sync_round_directory(&db, &mut tx,
        "ranks", "main", 0, 100, head + 1, "", &mut || false).unwrap().unwrap()).unwrap();
    assert_eq!(stale["stale"], true); assert_eq!(stale["entries"], json!([]));
    let empty: Value = serde_json::from_slice(&sync_round_directory(&db, &mut tx,
        "ranks", "other-lane", 0, 50, head, "", &mut || false).unwrap().unwrap()).unwrap();
    assert_eq!(empty["totalRounds"], 0);
    let mut other = db.begin_with_identity_claim_scopes(7, 12).unwrap();
    assert!(sync_round_directory(&db, &mut other, "ranks", "main", 0, 50,
        head, "", &mut || false).unwrap().is_none());
    assert!(sync_round_directory(&db, &mut tx, "ranks", "main", 0, 50,
        head, "", &mut || true).is_err());
    drop(tx);
    // Reopen a clean transaction, downgrade one legacy head, then use only the
    // explicit offline upgrader. It preserves the replay sequence.
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let key = lane_compaction_index_key(&tx, "ranks", "main", 1).unwrap();
    let mut metadata: Value = serde_json::from_slice(&db.entity_get(&mut tx, &key).unwrap().unwrap()).unwrap();
    metadata.as_object_mut().unwrap().remove("actor");
    db.entity_put(&mut tx, key, serde_json::to_vec(&metadata).unwrap()).unwrap();
    db.commit(tx).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let legacy: Value = serde_json::from_slice(&sync_round_directory(&db, &mut tx,
        "ranks", "main", 0, 50, head, "", &mut || false).unwrap().unwrap()).unwrap();
    assert_eq!(legacy["totalRounds"], 3);
    assert_eq!(legacy["entries"][0]["turnId"], "rank-turn-1");
    assert_eq!(sync_head(&db, &mut tx, "ranks").unwrap(), head);
    let legacy_key = lane_compaction_index_key(&tx, "ranks", "main", 1).unwrap();
    let stored_head: Value = serde_json::from_slice(&db.entity_get(&mut tx,
        &legacy_key).unwrap().unwrap()).unwrap();
    assert!(stored_head.get("actor").is_none(), "read must not backfill durable state");
    // Only absence is legacy. A malformed actor and a changed revision stay fenced.
    let legacy_key = lane_compaction_index_key(&tx, "ranks", "main", 1).unwrap();
    let mut corrupt = stored_head.clone(); corrupt["actor"] = json!(7);
    db.entity_put(&mut tx, legacy_key.clone(), serde_json::to_vec(&corrupt).unwrap()).unwrap();
    assert!(sync_round_directory(&db, &mut tx, "ranks", "main", 0, 50,
        head, "", &mut || false).is_err());
    corrupt = stored_head; corrupt["projectionRevision"] = json!(999999);
    db.entity_put(&mut tx, legacy_key, serde_json::to_vec(&corrupt).unwrap()).unwrap();
    assert!(sync_round_directory(&db, &mut tx, "ranks", "main", 0, 50,
        head, "", &mut || false).is_err());
    drop(tx);
    let mut cursor = Value::Null; let mut repaired = 0;
    loop {
        let result = migration::execute(&mut db, 7, 11,
            "migration.turn.round_heads.upgrade", json!({"cursor": cursor, "limit": 2})).unwrap();
        repaired += result["repaired"].as_u64().unwrap();
        cursor = result["cursor"].clone(); if cursor.is_null() { break; }
    }
    assert_eq!(repaired, 1);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(sync_head(&db, &mut tx, "ranks").unwrap(), head);
    let page: Value = serde_json::from_slice(&sync_round_directory(&db, &mut tx,
        "ranks", "main", 2, 1, head, "", &mut || false).unwrap().unwrap()).unwrap();
    assert_eq!(page["totalRounds"], 3); assert_eq!(page["entries"][0]["turnId"], "rank-turn-4");
}
