#[test]
fn legacy_receipt_replay_uses_exact_authenticated_payload_before_any_write() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();

    let payload =
        json!({"user_id":11,"namespace":"proof","key":"must-stay-absent","value":{"new":true}});
    let snapshot = crate::receipt::ReceiptSnapshot {
        operation: "record.put".to_owned(),
        request_digest: request_digest("record.put", payload.as_object().unwrap()),
        encoded_response: br#"{"historical":true}"#.to_vec(),
        committed_at_ms: 1234,
    };
    crate::legacy_receipt::restore(
        &mut db,
        7,
        uuid::Uuid::new_v4(),
        crate::receipt::command_receipt_key("legacy-command").unwrap(),
        snapshot,
    )
    .unwrap();
    db.configure_logical_outbox(&[73; 32], 1024 * 1024, 128 * 1024)
        .unwrap();
    let replay = request("record.put", 11, payload.clone(), Some("legacy-command"));
    assert_eq!(
        execute(&mut db, &replay, 2000).unwrap(),
        json!({"historical":true})
    );
    let foreign = request("record.put", 12, payload.clone(), Some("legacy-command"));
    assert!(execute(&mut db, &foreign, 2000).is_err());
    let mut changed = payload.clone();
    changed["user_id"] = json!(12);
    assert!(execute(
        &mut db,
        &request("record.put", 12, changed, Some("legacy-command")),
        2000
    )
    .is_err());
    let mut missing = payload.clone();
    missing.as_object_mut().unwrap().remove("user_id");
    assert!(execute(
        &mut db,
        &request("record.put", 11, missing, Some("legacy-command")),
        2000
    )
    .is_err());
    for owner in [11, 12] {
        assert_eq!(
            execute(
                &mut db,
                &request(
                    "record.get",
                    owner,
                    json!({"user_id":owner,"namespace":"proof","key":"must-stay-absent"}),
                    None
                ),
                2000
            )
            .unwrap(),
            Value::Null
        );
    }
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(
        execute(&mut db, &replay, 2001).unwrap(),
        json!({"historical":true})
    );
}
