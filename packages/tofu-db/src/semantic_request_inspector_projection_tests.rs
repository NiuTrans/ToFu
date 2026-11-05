// Real native semantic read/reopen parity for the optional request-list view.
#[test]
fn request_inspector_projection_is_owned_and_replay_safe() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let cases: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/request_inspector_projection.json")).unwrap();
    for (index, case) in cases.as_array().unwrap().iter().take(3).enumerate() {
        execute(&mut database, &request("event.append", 11,
            json!({"task_id": "inspector", "sequence": index, "event": case["event"]}),
            Some(&format!("append-inspector-{index}"))), 1000 + index as u64).unwrap();
    }
    let full_read = request("event.list", 11, json!({"task_id": "inspector"}), None);
    let original = execute(&mut database, &full_read, 2000).unwrap();
    let view = crate::generated_request_inspector_projection::PROJECTION;
    let projected_read = request("event.list", 11,
        json!({"task_id": "inspector", "payload_projection": view}), None);
    let projected = execute(&mut database, &projected_read, 2000).unwrap();
    assert_eq!(projected[0]["event"], cases[0]["expected"]);
    assert_eq!(projected[1], original[1]);
    assert_eq!(projected[2], original[2]);
    for index in 0..3 {
        assert_eq!(projected[index]["sequence"], original[index]["sequence"]);
        assert_eq!(projected[index]["created_at_ms"], original[index]["created_at_ms"]);
    }
    let selected = request("event.list", 11,
        json!({"task_id": "inspector", "payload_projection": view,
               "after_sequence": 0, "limit": 1}), None);
    assert_eq!(execute(&mut database, &selected, 2000).unwrap(), json!([original[1]]));
    let omitted = request("event.list", 11,
        json!({"task_id": "inspector", "payload_projection": view,
               "types": ["messages_snapshot"], "limit": 1, "payload_types": []}), None);
    assert_eq!(execute(&mut database, &omitted, 2000).unwrap()[0]["event"],
        json!({"type": "messages_snapshot"}));
    let foreign = request("event.list", 12,
        json!({"task_id": "inspector", "payload_projection": view}), None);
    assert_eq!(execute(&mut database, &foreign, 2000).unwrap(), json!([]));
    for invalid in [Value::Null, json!(true), json!(0), json!([]), json!({}), json!("unknown")] {
        let invalid_read = request("event.list", 11,
            json!({"task_id": "inspector", "payload_projection": invalid}), None);
        assert!(matches!(execute(&mut database, &invalid_read, 2000),
            Err(SemanticExecutionError::InvalidPayload)));
    }
    assert_eq!(execute(&mut database, &full_read, 2001).unwrap(), original);
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(execute(&mut database, &full_read, 3000).unwrap(), original);
    assert_eq!(execute(&mut database, &projected_read, 3000).unwrap(), projected);
}
