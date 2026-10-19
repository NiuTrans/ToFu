//! Actual checkpoint C ABI over the same Goal/native/wallet fixture. Authorities
//! are opened sequentially; no server, backend substitution or tool replay.
use super::*;
use crate::embed_ffi::{EmbedHandle,tofudb_embed_open,tofudb_embed_close,tofudb_embed_checkpoint_execute};
use std::ffi::CString;

struct Client(*mut EmbedHandle);
// The public C ABI accepts concurrent calls. Tests join all scoped callers
// before this sole owner closes the handle.
unsafe impl Sync for Client {}
impl Client {
    fn open(path:&std::path::Path)->Self {
        let path=CString::new(path.to_str().unwrap()).unwrap();let mut secret=[82_u8;32];let mut error=vec![0_u8;4096];
        let pointer=unsafe {tofudb_embed_open(path.as_ptr(),7,3,std::ptr::null(),secret.as_mut_ptr(),secret.len(),error.as_mut_ptr(),error.len())};
        assert!(!pointer.is_null(),"{}",String::from_utf8_lossy(&error));Self(pointer)
    }
    fn call(&self,operation:&str,payload:Value,capacity:usize,expired:bool)->Value {
        let deadline=if expired {1}else{SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64+30000};
        let request=serde_json::to_vec(&json!({"operation":operation,"payload":payload,"deadline_unix_ms":deadline})).unwrap();
        let mut output=vec![0_u8;capacity];
        let count=unsafe{tofudb_embed_checkpoint_execute(self.0,request.as_ptr(),request.len(),output.as_mut_ptr(),output.len())};
        assert!(count>=0,"checkpoint response {count}");serde_json::from_slice(&output[..count as usize]).unwrap()
    }
}
impl Drop for Client {fn drop(&mut self){unsafe{tofudb_embed_close(self.0)}}}
fn claim(request:&Value)->Value{json!({"format":"tofu.native-checkpoint-control/v1","goal_claim":{
    "owner_user_id":7,"header_bytes":4096,"snapshot_bytes":1048576,"evidence_bytes":BOUND,"request":request}})}
fn group(authorization:&Value)->Value{json!({"format":"tofu.native-checkpoint-control/v1","goal_handoff":{
    "owner_user_id":7,"header_bytes":4096,"snapshot_bytes":1048576,"identity_bytes":1024,"evidence_bytes":BOUND,
    "source_root_task_id":"source-root","target_root_task_id":"target-root","authorization":authorization,
    "actors":[{"source_task_id":"pending-a","task_id":"target-a","epoch":"epoch","binding":"binding","token":"target-a-token"},
        {"source_task_id":"pending-b","task_id":"target-b","epoch":"epoch","binding":"binding","token":"target-b-token"}]}})}
fn prepared()->(tempfile::TempDir,Value){
    let mut case=fixture();case.kill();let Case {_directory,store,request,writer}=case;drop(store);drop(writer);
    let client=Client::open(_directory.path());
    let payload=claim(&request);
    assert_ne!(client.call("goal_claim",payload.clone(),4096,false)["accepted"],true);
    let first=client.call("goal_claim",payload.clone(),BOUND+4096,false);assert_eq!(first["accepted"],true);
    assert_eq!(first["capability_id"],Value::Null);
    assert_eq!(client.call("goal_claim",payload,BOUND+4096,false),first);drop(client);
    let mut database=AuthorityDatabase::open(_directory.path()).unwrap();
    let authorization=prepare_target(&mut database,&request);drop(database);
    (_directory,group(&authorization))
}

#[test]
fn goal_cabi_claim_and_whole_handoff_reuse_one_capability_set_after_lost_response(){
    let (directory,payload)=prepared();let client=Client::open(directory.path());
    let first=client.call("goal_handoff",payload.clone(),BOUND+4096,false);
    assert_eq!(first["accepted"],true);assert_eq!(first["capability_id"],Value::Null);
    let rows=first["state"]["leases"].as_array().unwrap();assert_eq!(rows.len(),2);assert_ne!(rows[0]["capability_id"],rows[1]["capability_id"]);
    println!("goal_cabi_group response_bytes={} receipt_digest_bytes=64 receipt_id_bytes={}",
        serde_json::to_vec(&first).unwrap().len(),rows.len()*std::mem::size_of::<u64>());
    // A lost response returns the same handles and does not allocate fresh leases.
    assert_eq!(client.call("goal_handoff",payload.clone(),BOUND+4096,false),first);
    for (index,row) in rows.iter().enumerate(){
        let command=json!({"format":"tofu.native-checkpoint-control/v1","capability_id":row["capability_id"]});
        let loaded=client.call("load",command.clone(),1048576+4096,false);
        assert_eq!(loaded["accepted"],true);assert_eq!(loaded["state"]["task_id"],row["task_id"]);
        assert_eq!(loaded["state"]["shared_model_account"]["root_task_id"],"target-root");
        assert_eq!(loaded["state"]["tools_pending"],true);
        assert_eq!(client.call("goal_handoff",payload.clone(),BOUND+4096,false)["accepted"],false);
        let mut begin=command.clone();begin["round"]=json!(1);
        assert_eq!(client.call("begin",begin,1048576+4096,false)["error"],"ineligible");
        assert_eq!(client.call("forget",command,1048576+4096,false)["accepted"],true,"actor {index}");
    }
}

#[test]
fn concurrent_goal_cabi_requests_never_publish_distinct_lease_sets(){
    let (directory,payload)=prepared();let client=Client::open(directory.path());
    let barrier=std::sync::Barrier::new(2);
    let results=std::thread::scope(|scope|{
        let call=||{barrier.wait();client.call("goal_handoff",payload.clone(),BOUND+4096,false)};
        let first=scope.spawn(call);let second=scope.spawn(call);[first.join().unwrap(),second.join().unwrap()]
    });
    let accepted=results.iter().find(|result|result["accepted"]==true).unwrap();
    for result in &results {if result["accepted"]==true{assert_eq!(result,accepted);}}
    assert_eq!(&client.call("goal_handoff",payload,BOUND+4096,false),accepted);
}

#[test]
fn grouped_leases_and_claims_refuse_after_runtime_retirement(){
    let (directory,payload)=prepared();
    let mut secret=[82_u8;32];
    let runtime=Arc::new(crate::embed::EmbeddedRuntime::open(
        directory.path(),7,Some(3),None,&mut secret).unwrap());
    let authorization=&payload["goal_handoff"]["authorization"];
    let leases=runtime.checkpoint_goal_handoff(7,4096,1048576,1024,
        specs(),authorization,BOUND).unwrap();
    runtime.close().unwrap();
    for lease in leases {assert_eq!(lease.load(),Err(Error::Storage));}
    assert_eq!(runtime.checkpoint_goal_claim(7,4096,1048576,
        &authorization["claim_request"],BOUND),Err(Error::Storage));
    assert!(matches!(runtime.checkpoint_goal_handoff(7,4096,1048576,1024,
        specs(),authorization,BOUND),Err(Error::Storage)));
}

#[test]
fn goal_cabi_preflight_owner_and_deadline_refusals_leave_complete_handoff_available(){
    for damage in ["capacity","expired","owner","missing_actor","null","extra"] {
        let (directory,payload)=prepared();let client=Client::open(directory.path());let mut bad=payload.clone();
        match damage {
            "owner"=>bad["goal_handoff"]["owner_user_id"]=json!(8),
            "missing_actor"=>{bad["goal_handoff"]["actors"].as_array_mut().unwrap().pop();},
            "null"=>bad["goal_handoff"]["evidence_bytes"]=Value::Null,
            "extra"=>bad["goal_handoff"]["ignore_authority"]=json!(true),_=>(),
        }
        assert_ne!(client.call("goal_handoff",bad,if damage=="capacity"{4096}else{BOUND+4096},damage=="expired")["accepted"],true,"{damage}");
        assert_eq!(client.call("goal_handoff",payload,BOUND+4096,false)["accepted"],true,"{damage}");
    }
}

#[test]
fn export_goal_bridge_fixture_only_to_explicit_new_test_directory(){
    let Some(destination)=std::env::var_os("TOFU_TEST_GOAL_BRIDGE_FIXTURE") else{return;};
    let destination=std::path::PathBuf::from(destination);assert!(!destination.exists());
    let (directory,payload)=prepared();
    fn copy_tree(source:&std::path::Path,target:&std::path::Path){
        std::fs::create_dir(target).unwrap();
        for row in std::fs::read_dir(source).unwrap(){let row=row.unwrap();let kind=row.file_type().unwrap();
            if kind.is_dir(){copy_tree(&row.path(),&target.join(row.file_name()));}
            else{assert!(kind.is_file());std::fs::copy(row.path(),target.join(row.file_name())).unwrap();}
        }
    }
    std::fs::create_dir(&destination).unwrap();
    copy_tree(directory.path(),&destination.join("authority"));
    std::fs::write(destination.join("request.json"),serde_json::to_vec(&payload).unwrap()).unwrap();
}
