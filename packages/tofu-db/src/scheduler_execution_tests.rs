//! Indexed execution settlement preserves owner, stale-call, backfill and OCC fences.
use super::*;

fn definition(id: &str, owner: u64, kind: &str, execution: &str) -> Map<String, Value> {
    json!({"id":id,"user_id":owner,"system_key":"","created_at":"2026-10-13T00:00:00",
        "enabled":0,"name":id,"task_type":kind,"command":"run","schedule":"* * * * *",
        "last_execution_task_id":execution,"last_execution_status":"running"}).as_object().unwrap().clone()
}
fn settle(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction, execution: &str) -> Value {
    let raw=execute(database,transaction,&Request::SettleExecution{execution_id:execution.into(),status:"ok".into(),now:"now".into(),updated_at_ms:99}).unwrap().unwrap();
    serde_json::from_slice(&raw).unwrap()
}

#[test]
fn execution_settlement_backfills_once_and_preserves_foreign_and_stale_records() {
    let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut tx=database.begin_with_identity_claim_scopes(7,11).unwrap();
    for (id,kind) in [("agent","agent"),("command","command")] {
        let doc=definition(id,11,kind,"old");create_task_state(&database,&mut tx,id,&doc,1).unwrap();
        // Represent a legacy document before execution-index introduction.
        if let Some(key)=execution_key(&tx,&doc).unwrap(){database.entity_delete(&mut tx,key).unwrap();}
    }
    database.commit(tx).unwrap();
    let mut foreign=database.begin_with_identity_claim_scopes(7,12).unwrap();assert_eq!(settle(&database,&mut foreign,"old"),json!({"changed":0}));database.commit(foreign).unwrap();
    let mut current=database.begin_with_identity_claim_scopes(7,11).unwrap();assert_eq!(settle(&database,&mut current,"old"),json!({"changed":1}));
    assert_eq!(read_document(&database,&mut current,"agent").unwrap().unwrap()["last_execution_status"],"ok");
    assert_eq!(read_document(&database,&mut current,"command").unwrap().unwrap()["last_execution_status"],"running");database.commit(current).unwrap();
    let mut update=database.begin_with_identity_claim_scopes(7,11).unwrap();
    execute(&database,&mut update,&Request::Update{task_id:"agent".into(),updates:json!({"last_execution_task_id":"new","last_execution_status":"running"}).as_object().unwrap().clone(),updated_at_ms:100}).unwrap();database.commit(update).unwrap();
    let mut stale=database.begin_with_identity_claim_scopes(7,11).unwrap();assert_eq!(settle(&database,&mut stale,"old"),json!({"changed":0}));
    assert_eq!(read_document(&database,&mut stale,"agent").unwrap().unwrap()["last_execution_status"],"running");
    assert_eq!(settle(&database,&mut stale,"new"),json!({"changed":1}));database.commit(stale).unwrap();
    drop(database);let database=AuthorityDatabase::open(directory.path()).unwrap();
    let mut read=database.begin_with_identity_claim_scopes(7,11).unwrap();assert_eq!(read_document(&database,&mut read,"agent").unwrap().unwrap()["last_execution_status"],"ok");
}

#[test]
fn execution_settlement_cannot_commit_across_replaced_execution() {
    let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut tx=database.begin_with_identity_claim_scopes(7,11).unwrap();let doc=definition("agent",11,"agent","old");create_task_state(&database,&mut tx,"agent",&doc,1).unwrap();database.commit(tx).unwrap();
    let mut delayed=database.begin_with_identity_claim_scopes(7,11).unwrap();assert_eq!(settle(&database,&mut delayed,"old"),json!({"changed":1}));
    let mut newer=database.begin_with_identity_claim_scopes(7,11).unwrap();execute(&database,&mut newer,&Request::Update{task_id:"agent".into(),updates:json!({"last_execution_task_id":"new"}).as_object().unwrap().clone(),updated_at_ms:100}).unwrap();database.commit(newer).unwrap();
    assert!(database.commit(delayed).is_err());
}

#[test]
fn execution_index_growth_is_one_entry_per_eligible_task_and_one_owner_marker() {
    let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let started=std::time::Instant::now();let mut tx=database.begin_with_identity_claim_scopes(7,11).unwrap();
    for index in 0..256 {let id=format!("task-{index}");let doc=definition(&id,11,"agent","current");create_task_state(&database,&mut tx,&id,&doc,1).unwrap();}
    database.commit(tx).unwrap();let mut settle_tx=database.begin_with_identity_claim_scopes(7,11).unwrap();assert_eq!(settle(&database,&mut settle_tx,"missing"),json!({"changed":0}));database.commit(settle_tx).unwrap();
    let mut read=database.begin_with_identity_claim_scopes(7,11).unwrap();let (start,end)=namespace_range(&read,SCHEDULER_TASK_EXECUTION_INDEX_NAMESPACE).unwrap();let rows=scan_task_index(&database,&mut read,start,&end,0).unwrap();
    assert_eq!(rows.len(),257);let bytes:usize=rows.iter().map(|(key,value)|key.key_bytes().len()+value.len()).sum();
    eprintln!("scheduler execution index: 256 eligible definitions, {} entries, {} encoded key/value bytes, {:?} create and backfill",rows.len(),bytes,started.elapsed());
    drop(read);let mut delete=database.begin_with_identity_claim_scopes(7,11).unwrap();execute(&database,&mut delete,&Request::Delete{task_id:"task-0".into()}).unwrap();database.commit(delete).unwrap();
    let mut read=database.begin_with_identity_claim_scopes(7,11).unwrap();let (start,end)=namespace_range(&read,SCHEDULER_TASK_EXECUTION_INDEX_NAMESPACE).unwrap();assert_eq!(scan_task_index(&database,&mut read,start,&end,0).unwrap().len(),256);
}


#[test]
fn execution_lookup_refuses_unknown_legacy_absence_without_mutating_index() {
    let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut tx=database.begin_with_identity_claim_scopes(7,11).unwrap();
    let lookup=Request::ExecutionLookup{execution_id:"old".into()};assert!(!lookup.mutates_state());
    let raw=execute(&database,&mut tx,&lookup).unwrap().unwrap();assert_eq!(serde_json::from_slice::<Value>(&raw).unwrap(),json!({"indexed":true,"matched":false}));
    let marker=owner_key(&tx,SCHEDULER_TASK_EXECUTION_INDEX_NAMESPACE,b"initialized-v1").unwrap();assert!(database.entity_get(&mut tx,&marker).unwrap().is_none());
    let doc=definition("legacy",11,"agent","old");create_task_state(&database,&mut tx,"legacy",&doc,1).unwrap();
    let raw=execute(&database,&mut tx,&lookup).unwrap().unwrap();assert_eq!(serde_json::from_slice::<Value>(&raw).unwrap(),json!({"indexed":false,"matched":false}));
    assert!(database.entity_get(&mut tx,&marker).unwrap().is_none());database.commit(tx).unwrap();
    let mut initialized=database.begin_with_identity_claim_scopes(7,11).unwrap();assert_eq!(settle(&database,&mut initialized,"old"),json!({"changed":1}));database.commit(initialized).unwrap();
    let mut read=database.begin_with_identity_claim_scopes(7,11).unwrap();let raw=execute(&database,&mut read,&lookup).unwrap().unwrap();assert_eq!(serde_json::from_slice::<Value>(&raw).unwrap(),json!({"indexed":true,"matched":true}));
    let missing=execute(&database,&mut read,&Request::ExecutionLookup{execution_id:"missing".into()}).unwrap().unwrap();assert_eq!(serde_json::from_slice::<Value>(&missing).unwrap(),json!({"indexed":true,"matched":false}));
}
