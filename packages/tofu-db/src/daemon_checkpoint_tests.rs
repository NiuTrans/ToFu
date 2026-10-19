//! Transport admission and instance fencing over the real selected authority.
use super::*;
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION as SCHEMA;

fn request(operation: &str, payload: serde_json::Value) -> Request {
    Request {correlation_id:[1;16], deadline_unix_ms:u64::MAX, owner_id:7, tenant_id:Some(3),
        command_id:None, schema_id:SCHEMA, operation:operation.into(), payload:serde_json::to_vec(&payload).unwrap()}
}
fn control(port: &DaemonCheckpointControl, operation: &str, payload: serde_json::Value, capacity: usize) -> Request {
    request(CONTROL, json!({"format":FORMAT,"runtime_id":port.runtime_id,
        "operation":operation,"payload":payload,"response_bytes":capacity}))
}
fn binding() -> serde_json::Value {
    json!({"format":"tofu.native-checkpoint-control/v1","parameters":{
        "owner_user_id":7,"task_id":"daemon-task","epoch":"epoch","binding":"model-tools-account",
        "token":"lease","header_bytes":4096,"snapshot_bytes":1048576,"identity_bytes":1024}})
}
fn cap(id: u64) -> serde_json::Value {
    json!({"format":"tofu.native-checkpoint-control/v1","capability_id":id})
}
fn result(port: &DaemonCheckpointControl, request: &Request) -> serde_json::Value {
    let response=port.respond(request,SCHEMA,1);assert_eq!(response.status,0,"{:?}",response.error_message);
    serde_json::from_slice(&response.payload).unwrap()
}

#[test]
fn checkpoint_daemon_capability_reflects_build_and_refuses_foreign_envelopes() {
    let directory=tempfile::tempdir().unwrap();
    let database=Arc::new(Mutex::new(crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap()));
    let port=DaemonCheckpointControl::new(database,AuthenticatedScope{owner_id:7,tenant_id:Some(3)},Arc::new(AtomicBool::new(false)));
    let query=request(CAPABILITIES,json!({}));
    let row=result(&port,&query);
    assert_eq!(row["available"],cfg!(feature="native-runtime-checkpoints"));
    assert_eq!(row["format"],FORMAT);
    assert_eq!(row["maximum_response_bytes"], crate::server::MAX_STREAMED_RESPONSE_BYTES);
    for field in ["owner","tenant","schema","command","deadline","payload"] {
        let mut forged=query.clone();
        match field {"owner"=>forged.owner_id=8,"tenant"=>forged.tenant_id=None,
            "schema"=>forged.schema_id+=1,"command"=>forged.command_id=Some("receipt".into()),
            "deadline"=>forged.deadline_unix_ms=1,_=>forged.payload=b"{\"unexpected\":true}".to_vec()}
        assert_ne!(port.respond(&forged,SCHEMA,1).status,0,"{field}");
    }
}

#[cfg(feature="native-runtime-checkpoints")]
#[test]
fn checkpoint_daemon_full_journal_capacity_uses_the_existing_stream_representation() {
    let directory=tempfile::tempdir().unwrap();
    let database=Arc::new(Mutex::new(crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap()));
    let port=DaemonCheckpointControl::new(database,AuthenticatedScope{owner_id:7,tenant_id:Some(3)},Arc::new(AtomicBool::new(false)));
    let bound=crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES;
    let capacity=bound+4096;
    assert!(capacity>crate::generated_storage_v2::MAX_PAYLOAD_BYTES);
    let mut parameters=binding();parameters["parameters"]["snapshot_bytes"]=json!(bound);
    let created=result(&port,&control(&port,"create",parameters,capacity));
    assert_eq!(created["accepted"],true);
    let id=created["capability_id"].as_u64().unwrap();
    assert_ne!(port.respond(&control(&port,"load",cap(id),capacity-1),SCHEMA,1).status,0);
    assert_eq!(result(&port,&control(&port,"load",cap(id),capacity))["accepted"],true);
}

#[cfg(feature="native-runtime-checkpoints")]
#[test]
fn checkpoint_daemon_preflight_preserves_one_shot_load_and_same_authority_lease() {
    let directory=tempfile::tempdir().unwrap();
    let database=Arc::new(Mutex::new(crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap()));
    let stopping=Arc::new(AtomicBool::new(false));
    let port=DaemonCheckpointControl::new(database.clone(),AuthenticatedScope{owner_id:7,tenant_id:Some(3)},stopping.clone());
    let capacity=1048576+4096;
    let small=control(&port,"create",binding(),4096);
    assert_ne!(port.respond(&small,SCHEMA,1).status,0);
    let created=result(&port,&control(&port,"create",binding(),capacity));
    assert_eq!(created["accepted"],true);
    let id=created["capability_id"].as_u64().unwrap();
    assert_ne!(port.respond(&control(&port,"load",cap(id),4096),SCHEMA,1).status,0);
    let loaded=result(&port,&control(&port,"load",cap(id),capacity));
    assert_eq!(loaded["accepted"],true);assert!(loaded["state"].is_null());
    assert_eq!(result(&port,&control(&port,"load",cap(id),capacity))["error"],"ineligible");
    // Another registry can restart IDs, but cannot accept this instance's envelope.
    let next=DaemonCheckpointControl::new(database,AuthenticatedScope{owner_id:7,tenant_id:Some(3)},stopping.clone());
    assert_ne!(next.respond(&control(&port,"forget",cap(id),capacity),SCHEMA,1).status,0);
    let mut begin=cap(id);begin["round"]=json!(0);
    assert_eq!(result(&port,&control(&port,"begin",begin,capacity))["accepted"],true);
    stopping.store(true,Ordering::Release);
    assert!(!port.available());
    assert_eq!(result(&port,&control(&port,"seal",cap(id),capacity))["error"],"storage");
}
