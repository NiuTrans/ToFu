//! Complete scheduled-task feeds retain identity, integrity and OCC after quotas retire.
use super::*;

fn document(index: usize) -> Map<String, Value> {
    json!({"id": format!("task-{index:05}"), "user_id": 11, "system_key": "",
           "created_at": "2026-10-10T00:00:00", "enabled": 1,
           "name": format!("task {index}"), "task_type": "command",
           "command": "x".repeat(4500), "schedule": "* * * * *"})
        .as_object().unwrap().clone()
}

#[test]
fn complete_catalog_exceeds_count_page_and_response_quotas_with_late_occ() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_enabled(true);
    let started = std::time::Instant::now();
    for batch in 0..9 {
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in batch*250..((batch+1)*250).min(2105) {
            let doc = document(index);
            create_task_state(&database, &mut tx, doc["id"].as_str().unwrap(), &doc, 1000+index as u64).unwrap();
        }
        database.commit(tx).unwrap();
    }
    let mut stale = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let encoded = execute(&database, &mut stale, &Request::List { limit: 0, enabled_only: false }).unwrap().unwrap();
    assert!(encoded.len() > 8*1024*1024);
    let rows: Value = serde_json::from_slice(&encoded).unwrap();
    assert_eq!(rows.as_array().unwrap().len(), 2105);
    assert_eq!(rows[0]["id"], "task-02104");
    assert_eq!(rows[2104]["id"], "task-00000");
    assert_eq!(read_count(&database, &mut stale).unwrap(), 2105);
    eprintln!("scheduler demand: 2105 tasks, {} response bytes, {:?}", encoded.len(), started.elapsed());
    let mut projection = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let selected = execute(&database, &mut projection, &Request::List { limit: 2001, enabled_only: true }).unwrap().unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&selected).unwrap().as_array().unwrap().len(), 2001);
    drop(projection);
    let mut global = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    let all = execute(&database, &mut global, &Request::ListAll { limit: 0, enabled_only: true }).unwrap().unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&all).unwrap().as_array().unwrap().len(), 2105);
    let owned = execute(&database, &mut global, &Request::List { limit: 0, enabled_only: false }).unwrap().unwrap();
    assert_eq!(owned, b"[]");
    assert_eq!(create_task_state(&database, &mut global, "task-00000", &document(0), 9000).unwrap_err().kind(), io::ErrorKind::AlreadyExists);
    drop(global);
    // The matching legacy definition is in the last index window.
    let mut adoption = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let mut builtin = document(0);
    builtin.insert("id".into(), json!("default-candidate"));
    builtin.insert("system_key".into(), json!("large.builtin"));
    let adopted = execute(&database, &mut adoption, &Request::Ensure {
        task_id: "default-candidate".into(), system_key: "large.builtin".into(),
        create_document: builtin,
        definition: json!({"name":"task 0", "task_type":"command"}).as_object().unwrap().clone(),
        updated_at: "2026-10-10T00:00:01".into(), updated_at_ms: 8500,
    }).unwrap().unwrap();
    let adopted: Value = serde_json::from_slice(&adopted).unwrap();
    assert_eq!(adopted["created"], false);
    assert_eq!(adopted["task"]["id"], "task-00000");
    assert_eq!(read_count(&database, &mut adoption).unwrap(), 2105);
    database.commit(adoption).unwrap();
    let mut change = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    execute(&database, &mut change, &Request::Update {
        task_id: "task-00000".into(), updates: json!({"name":"changed"}).as_object().unwrap().clone(), updated_at_ms: 9000,
    }).unwrap();
    database.commit(change).unwrap();
    database.entity_put(&mut stale, EntityKey::new(7, 11, "scheduler_dependent", b"check").unwrap(), vec![1]).unwrap();
    assert_eq!(database.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    let mut deletion = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    execute(&database, &mut deletion, &Request::Delete { task_id: "task-02104".into() }).unwrap();
    assert_eq!(read_count(&database, &mut deletion).unwrap(), 2104);
    database.commit(deletion).unwrap();
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
}

#[test]
fn count_overflow_has_no_staged_document_or_identity_effects() {
    let directory = tempfile::tempdir().unwrap();
    let database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let count = count_key(&tx).unwrap();
    database.entity_put(&mut tx, count, u64::MAX.to_le_bytes().to_vec()).unwrap();
    let result = create_task_state(&database, &mut tx, "task-00000", &document(0), 1);
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::InvalidData);
    let key = task_key(&tx, "task-00000").unwrap();
    assert!(database.entity_get(&mut tx, &key).unwrap().is_none());
    let claim = claim_key(&tx, "task-00000").unwrap();
    assert!(database.entity_get(&mut tx, &claim).unwrap().is_none());
}
