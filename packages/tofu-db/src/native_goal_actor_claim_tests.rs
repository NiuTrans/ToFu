//! Real-authority atomic retirement/fee tests. The frozen quote/cut fixture is
//! produced by Python's shared budget and native fee owners; these tests exercise
//! storage composition, not application dispatch or subsequent actor execution.
use super::*;
use tofu_runtime::checkpoint_journal::{Journal, Scope};

fn invoke(database: &mut AuthorityDatabase, operation: &str, payload: Value, write: bool) -> Value {
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, AuthenticatedScope};
    let request = Request { correlation_id:[3;16], deadline_unix_ms:10000000000,owner_id:7,tenant_id:Some(3),
        command_id:write.then(||payload["command_id"].as_str().map(str::to_owned).unwrap_or_else(||uuid::Uuid::new_v4().to_string())),
        schema_id:crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,operation:operation.into(),
        payload:serde_json::to_vec(&payload).unwrap() };
    let admitted = admit_request(&request,NegotiatedProtocol {protocol_version:PROTOCOL_VERSION,schema_id:request.schema_id},
        AuthenticatedScope {owner_id:7,tenant_id:Some(3)},1000).unwrap();
    decode(&crate::semantic_executor::execute_admitted_request(database,admitted,1000)
        .unwrap_or_else(|error|panic!("{operation}: {error:?}"))).unwrap()
}
fn bound(database: &mut AuthorityDatabase, attempt: &str, task: &str) {
    assert_eq!(invoke(database,"turn.attempt.claim",json!({"attempt_id":attempt,"dispatch_owner_id":"test","user_id":7}),true),true);
    invoke(database,"turn.attempt.bind",json!({"attempt_id":attempt,"task_id":task,"dispatch_owner_id":"test","user_id":7}),true);
    assert_eq!(invoke(database,"turn.attempt.start",json!({"attempt_id":attempt,"task_id":task,"user_id":7}),true)["status"],"running");
}
fn scope(task: &str) -> Scope { Scope {owner:7,task_id:task.into(),epoch:"epoch".into(),binding:"binding".into()} }
struct Writer(std::process::Child);
impl Drop for Writer { fn drop(&mut self) { let _=self.0.kill();let _=self.0.wait(); } }
struct Case { _directory:tempfile::TempDir, store:NativeCheckpointStore, request:Value, writer:Writer }
#[path = "native_goal_claim_replay_tests.rs"]
mod replica;
#[path = "native_goal_actor_handoff_tests.rs"]
mod consumption;
impl Case {
    fn kill(&mut self) { self.writer.0.kill().unwrap();self.writer.0.wait().unwrap(); }
    fn snapshot(&self) -> Value {
        inspect_state(&self.store,&self.request)
    }
}
fn inspect_state(store:&NativeCheckpointStore,request:&Value) -> Value {
        let mut database=store.authority.lock().unwrap();
        let wallet=invoke(&mut database,"billing.wallet.get",json!({"user_id":"wallet-7"}),false);
        let mut remaining=1048576;let mut tx=database.begin_with_identity_claim_scopes(3,7).unwrap();
        let claim=goal::event(&database,&mut tx,"goal_source-root","3",request["source_cursor"].as_u64().unwrap(),&mut remaining).unwrap();
        drop(tx);drop(database);
        let mut actors=Vec::new();
        for task in ["pending-a","pending-b"] {
            let h=store.header(7,task).unwrap().unwrap();
            actors.push(json!({"header":h.value,"version":h.version,"snapshot":store.snapshot(7,task,h.version).unwrap()}));
        }
        json!({"wallet":wallet,"claim":claim,"actors":actors})
}
fn fixture() -> Case {
    let directory=tempfile::tempdir().unwrap();let database=AuthorityDatabase::initialize(directory.path()).unwrap();
    fixture_with_database(directory,database)
}
fn fixture_with_database(directory:tempfile::TempDir,database:AuthorityDatabase) -> Case {
    fixture_with_setup(directory,database,|_|{})
}
fn fixture_with_setup(directory:tempfile::TempDir,database:AuthorityDatabase,
    before_native: impl FnOnce(&mut AuthorityDatabase)) -> Case {
    fixture_with_fee_setup(directory,database,before_native,false)
}
fn fixture_with_fee_setup(directory:tempfile::TempDir,mut database:AuthorityDatabase,
    before_native: impl FnOnce(&mut AuthorityDatabase), cost_only:bool) -> Case {
    let mut fixture:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_claim.json")).unwrap();
    if cost_only {
        fixture["invoice"]["format"]=json!("tofu.native-aggregate-cost/v1");
        fixture["invoice"].as_object_mut().unwrap().remove("wallet_user_id");
        fixture["invoice"].as_object_mut().unwrap().remove("reserved_micro");
    }
    let mut tx=database.begin_with_identity_claim_scopes(3,7).unwrap();
    for owner in 2..=7 {
        crate::tenant_user::create(&database,&mut tx,&crate::tenant_user::CreateRequest {
            user_id:format!("wallet-{owner}"),email:format!("fixture-{owner}@example.test"),password_hash:"fixture".into(),
            display_name:"fixture".into(),role:"user".into(),created_at:1000,metadata:json!({}),physical_updated_at_ms:1000,
        }).unwrap();
    }
    assert_eq!(crate::tenant_user::account_id_for_owner(&database,&mut tx,7).unwrap().as_deref(),Some("wallet-7"));
    for (kind,amount,reference) in [("topup",200000,"payment"),("reserve",-100000,"reserve"),("native_admission",0,"reserve")] {
        if cost_only && kind != "topup" {continue;}
        crate::billing::wallet_apply(&database,&mut tx,&crate::billing::WalletApplyRequest {
            user_id:"wallet-7".into(),amount_micro:amount,kind:kind.into(),ref_type:reference.into(),ref_id:"source-root".into(),
            note:"fixture".into(),ledger_id:format!("fixture-{kind}"),occurred_at:1000,allow_negative:false,
        }).unwrap();
    }
    database.commit(tx).unwrap();
    let source=invoke(&mut database,"turn.create_pair",json!({"conversation_id":"conversation","user_id":7,
        "command_id":"source","input_projection":{"content":"Preserve work"},
        "config":{"_goalObjective":"Preserve work","_turnPrincipalTenantId":"3"},
        "conversation_defaults":{"allowCreate":true,"title":"Atomic claim","settings":{}}}),true);
    let source_attempt=source["attempt"]["attemptId"].as_str().unwrap();let source_turn=source["turn"]["turnId"].as_str().unwrap();
    bound(&mut database,source_attempt,"source-root");
    let start=|run:&str| json!({"run_id":run,"conversation_id":"conversation","objective":"Preserve work",
        "user_id":7,"tenant_id":"3","definition":{"nodes":[]},"policy":fixture["goal_policy"]});
    invoke(&mut database,"goal.run.start",start("goal_source-root"),true);
    let page=invoke(&mut database,"orchestration.event.page",json!({"run_id":"goal_source-root","user_id":7,"tenant_id":"3","cursor":0}),false);
    let mut cursor=page["next_cursor"].as_u64().unwrap();let launch_sequence=cursor;
    let append=|db:&mut AuthorityDatabase,seq:u64,event:Value| {
        invoke(db,"orchestration.event.append",json!({"run_id":"goal_source-root","user_id":7,"tenant_id":"3","sequence":seq,"event":event}),true);
    };
    let mut launch=json!({"type":"execution_journal_start","launch_metadata":{
        "format":"tofu.goal-execution-launch/v2","run_id":"goal_source-root","task_id":"source-root","turn_id":source_turn,
        "attempt_id":source_attempt,"conversation_id":"conversation","owner_user_id":7,"tenant_id":"3",
        "launch_id":"launch","objective":"Preserve work","launch":{"model":"alpha"}}});
    if cost_only {launch["launch_metadata"]["fee_account"]=json!({"mode":"cost"});}
    append(&mut database,cursor,launch);cursor+=1;
    let mut actors=Vec::new();
    for task in ["pending-a","pending-b","finished-c"] {
        let birth=invoke(&mut database,"invocation.register_child",json!({"user_id":7,"conversation_id":"conversation",
            "parent_task_id":"source-root","child_task_id":task}),true);
        let binding=json!({"format":"tofu.flow.invocation-completion/v1","nonce":"a".repeat(32),"identity":birth,"account_root_task_id":"source-root"});
        let admission=json!({"type":"execution_effect_started","key":format!("root:agent:{task}:0:0"),"kind":"agent",
            "input_hash":"a".repeat(64),"invocation_id":task,"invocation_completion":binding});
        append(&mut database,cursor,admission.clone());
        let mut record=invoke(&mut database,"record.get",json!({"namespace":"task_results","key":task,"user_id":7}),false);
        if task=="finished-c" {
            let receipt=json!({"kind":"agent","input_hash":"a".repeat(64),"invocation_id":task,"invocation_completion":binding,
                "execution_account":fixture["cut"]["snapshot"],"result":{"output":"Preserved completion"}});
            record["value"]["flow_invocation_completion"]=json!({"admission":admission,"receipt":receipt,"native_fees":fixture["completed_fees"]});
            invoke(&mut database,"task_results.checkpoint",json!({"key":task,"value":record["value"],"expected_version":record["version"],
                "guard_contract":crate::generated_tofudb_ir::TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,"require_parent":true}),true);
            record=invoke(&mut database,"record.get",json!({"namespace":"task_results","key":task,"user_id":7}),false);
        }
        let mut actor=json!({"event_sequence":cursor,"registration_version":record["version"]});
        if task!="finished-c" {actor["checkpoint_version"]=json!(3);}
        actors.push(actor);cursor+=1;
    }
    let account_sequence=cursor;
    append(&mut database,cursor,json!({"type":"execution_account_frontier","launch_id":"launch","frontier":fixture["cut"]}));cursor+=1;
    invoke(&mut database,"orchestration.run.update_status",json!({"run_id":"goal_source-root","user_id":7,"tenant_id":"3","status":"error",
        "error":{"kind":"worker_lost","source":"orchestration.startup_recovery"}}),true);
    invoke(&mut database,"turn.event.record",json!({"attempt_id":source_attempt,"task_id":"source-root","user_id":7,"terminal":true,
        "status":"interrupted","slim":false,"content":"","thinking":"checkpoint","projection":{"content":"","thinking":"checkpoint","toolRounds":[],"segments":[]},
        "settlement":{"outcome":"interrupted","cause":"server_restart","resumeOptions":[]},"error":{},"event_type":"aborted","event_payload":{},"now":1000}),true);
    let command=format!("goal-recovery-{:x}",Sha256::digest(b"goal_source-root"));
    let target=invoke(&mut database,"turn.create_pair",json!({"conversation_id":"conversation","user_id":7,"command_id":command,
        "input_actor":"virtual_user","input_kind":"continuation","parent_turn_id":source_turn,"input_projection":{"content":"Continue"},
        "config":{"_goalObjective":"Preserve work","_turnPrincipalTenantId":"3","_goalRecoverySourceRunId":"goal_source-root"}}),true);
    let target_attempt=target["attempt"]["attemptId"].as_str().unwrap();bound(&mut database,target_attempt,"target-root");
    invoke(&mut database,"goal.run.start",start("goal_target-root"),true);
    let request=json!({"format":"tofu.native-goal-actor-claim/v1","tenant_id":"3","source_run_id":"goal_source-root",
        "target_attempt_id":target_attempt,"source_cursor":cursor,"launch_sequence":launch_sequence,"account_sequence":account_sequence,
        "actors":actors,"invoice":fixture["invoice"]});
    before_native(&mut database);
    let writer=Writer(std::process::Command::new("/bin/sleep").arg("300").spawn().unwrap());
    let mut store=NativeCheckpointStore::new(Arc::new(Mutex::new(database)),3,7,4096,1048576).unwrap();
    let mut identity=crate::native_checkpoint_store::writer::capture().unwrap();
    let stat=std::fs::read_to_string(format!("/proc/{}/stat",writer.0.id())).unwrap();
    identity["pid"]=json!(writer.0.id());identity["start_ticks"]=json!(stat.rsplit_once(')').unwrap().1.split_whitespace().nth(19).unwrap().parse::<u64>().unwrap());
    store.writer_process=Some(identity);
    for task in ["pending-a","pending-b"] {
        let journal=Journal::new(scope(task),&store,1048576,1024).unwrap();let lease=journal.acquire("source").unwrap();
        lease.load().unwrap();lease.begin(0).unwrap();lease.pending(&fixture["states"][task]).unwrap();
    }
    store.writer_process=crate::native_checkpoint_store::writer::capture();
    Case {_directory:directory,store,request,writer}
}

#[test]
fn whole_goal_claim_retires_all_sources_and_settles_original_wallet_once() {
    let mut case=fixture();let before=case.snapshot();case.kill();
    let claim=case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap();
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap(),claim);
    let after=case.snapshot();assert_eq!(after["claim"],claim);
    assert_eq!(after["wallet"]["balance_micro"],177221);
    let headers_before=serde_json::to_vec(&before["actors"].as_array().unwrap().iter().map(|v|&v["header"]).collect::<Vec<_>>()).unwrap().len();
    let headers_after=serde_json::to_vec(&after["actors"].as_array().unwrap().iter().map(|v|&v["header"]).collect::<Vec<_>>()).unwrap().len();
    println!("goal_claim_growth pending=2 completed=1 header_delta_bytes={} claim_event_bytes={} extra_snapshot_bytes=0 new_workers=0",
        headers_after as i64-headers_before as i64,serde_json::to_vec(&claim).unwrap().len());
    for index in 0..2 {
        assert_eq!(after["actors"][index]["header"]["phase"],"terminal");
        assert_eq!(after["actors"][index]["snapshot"],before["actors"][index]["snapshot"]);
        assert_eq!(after["actors"][index]["version"],4);
    }
    assert_eq!(Journal::new(scope("pending-a"),&case.store,1048576,1024).unwrap().recover("stolen").err(),Some(Error::Ineligible));
}

#[test]
fn final_wallet_refusal_rolls_back_every_source_retirement() {
    let mut case=fixture();case.kill();case.request["invoice"]["reserved_micro"]=json!(99999);
    let before=case.snapshot();assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024),Err(Error::Ineligible));
    assert_eq!(case.snapshot(),before);
}

#[test]
fn live_writer_and_partial_actor_inventory_never_freeze_or_charge() {
    let mut case=fixture();let before=case.snapshot();
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024),Err(Error::Ineligible));
    assert_eq!(case.snapshot(),before);case.kill();case.request["actors"].as_array_mut().unwrap().remove(1);
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024),Err(Error::Ineligible));
    assert_eq!(case.snapshot(),before);
}

#[test]
fn prepared_claim_rechecks_registered_child_versions_at_commit() {
    let mut case=fixture();case.kill();
    let request:Request=serde_json::from_value(case.request.clone()).unwrap();
    let prepared=case.store.prepare_goal_claim(&request,8*1024*1024).unwrap();
    {
        let mut db=case.store.authority.lock().unwrap();
        let record=invoke(&mut db,"record.get",json!({"namespace":"task_results","key":"finished-c","user_id":7}),false);
        let mut value=record["value"].clone();value["status"]=json!("done");
        invoke(&mut db,"task_results.checkpoint",json!({"key":"finished-c","value":value,"expected_version":record["version"],
            "guard_contract":crate::generated_tofudb_ir::TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,"require_parent":true}),true);
    }
    let before=case.snapshot();
    assert_eq!(case.store.commit_goal_claim(&request,"a".repeat(64).as_str(),prepared,8*1024*1024),Err(Error::Conflict));
    assert_eq!(case.snapshot(),before);
}

#[test]
fn configured_outbox_captures_one_atomic_goal_claim_without_snapshot_copies() {
    let mut case=fixture();case.kill();
    case.store.authority.lock().unwrap().configure_logical_outbox(&[72;32],4*1024*1024,128*1024).unwrap();
    let claim=case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap();
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap(),claim);
    let pending=case.store.authority.lock().unwrap().logical_outbox_pending_bounded(3,7,100,8*1024*1024).unwrap();
    assert_eq!(pending.len(),1);assert_eq!(pending[0].record.identity.operation,GOAL_CLAIM_OPERATION);
    let cipher=LogicalOutboxCipher::new(&[72;32]);
    let value:Value=serde_json::from_slice(&cipher.open(&pending[0].record).unwrap()).unwrap();
    assert_eq!(value["claim"],claim);assert_eq!(value["request"],case.request);
    assert!(value.get("snapshots").is_none());
}

#[test]
fn aggregate_native_fee_union_retains_payers_and_refuses_conflicting_prefixes() {
    let fixture:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_claim.json")).unwrap();
    let mut old=actor_fee_evidence_from_checkpoint(&fixture["states"]["pending-a"],7,"source-root","pending-a","3").unwrap();
    let mut current=old.clone();current["root_task_id"]=json!("successor");current["actor_task_id"]=json!("new-actor");
    current["tokens"]=json!(20400);
    let mut fresh=current["rounds"][0].clone();fresh["round"]=json!(2);fresh["producer_task_id"]=json!("new-actor");fresh["payer_task_id"]=json!("successor");
    current["rounds"].as_array_mut().unwrap().push(fresh);
    let mut cut=fixture["cut"].clone();let counters=cut["actors"]["pending-a"].clone();let root=cut["actors"]["source-root"].clone();
    cut["snapshot"]["root_task_id"]=json!("successor");cut["snapshot"]["tokens"]=json!(20400);
    cut["actors"]=json!({"successor":root,"new-actor":counters});cut["actors"]["new-actor"]["parent_task_id"]=json!("successor");
    cut["actors"]["new-actor"]["tokens"]=json!(20400);cut["closed_tokens"]=json!(0);
    let mut invoice=fixture["invoice"].clone();invoice["task_id"]=json!("successor");invoice["roundCount"]=json!(1);
    for (credit,component) in [("input","input"),("output","output"),("cache_read","cache_read"),("cache_write","cache_write"),
        ("base","baseMicro"),("margin","marginMicro"),("total","totalMicro")] {
        invoice["componentsMicro"][component]=old["rounds"][0]["quote"]["credits"][credit].clone();
    }
    assert_eq!(validate_actor_fee_evidence(&old,7,"source-root","pending-a"),Ok(()));
    assert_eq!(validate_actor_fee_evidence(&current,7,"successor","new-actor"),Ok(()));
    assert_eq!(validate_aggregate_actor_invoice(&cut,&[old.clone(),current.clone()],&invoice),Ok(()));
    old["rounds"][0]["quote"]["price"]["input"]=json!(123.0);
    assert_eq!(validate_aggregate_actor_invoice(&cut,&[old,current],&invoice),Err(Error::Invalid));
}

#[test]
fn changed_or_omitted_wallet_and_tenant_cannot_retire_original_debt() {
    for (field,value) in [("wallet_user_id",json!("wallet-6")),("wallet_user_id",json!(""))] {
        let mut case=fixture();case.kill();case.request["invoice"][field]=value;
        if case.request["invoice"][field]=="" {case.request["invoice"]["reserved_micro"]=json!(0);}
        let before=case.snapshot();
        assert!(case.store.claim_goal_actors(&case.request,8*1024*1024).is_err());
        assert_eq!(case.snapshot(),before);
    }
    let mut case=fixture();case.kill();let before=case.snapshot();case.request["tenant_id"]=json!("foreign");
    assert!(case.store.claim_goal_actors(&case.request,8*1024*1024).is_err());
    assert_eq!(case.snapshot(),before);
}

#[test]
fn claim_input_is_closed_and_evidence_reads_are_bounded() {
    let mut case=fixture();case.kill();let before=case.snapshot();
    for field in ["extra","checkpoint_version"] {
        let mut changed=case.request.clone();
        if field=="extra" {changed[field]=json!(true);} else {changed["actors"][0][field]=Value::Null;}
        assert_eq!(case.store.claim_goal_actors(&changed,8*1024*1024),Err(Error::Invalid));
    }
    assert!(case.store.claim_goal_actors(&case.request,1024).is_err());
    assert_eq!(case.snapshot(),before);
}

#[test]
fn source_cursor_native_version_and_target_execution_are_rechecked() {
    for change in ["source","native","target"] {
        let mut case=fixture();case.kill();
        let request:Request=serde_json::from_value(case.request.clone()).unwrap();
        let prepared=case.store.prepare_goal_claim(&request,8*1024*1024).unwrap();
        if change=="native" {
            Journal::new(scope("pending-b"),&case.store,1048576,1024).unwrap().recover("other").unwrap();
        } else {
            let mut db=case.store.authority.lock().unwrap();
            let run=if change=="source" {"goal_source-root"} else {"goal_target-root"};
            let page=invoke(&mut db,"orchestration.event.page",json!({"run_id":run,"user_id":7,"tenant_id":"3","cursor":0}),false);
            invoke(&mut db,"orchestration.event.append",json!({"run_id":run,"user_id":7,"tenant_id":"3",
                "sequence":page["next_cursor"],"event":{"type":if change=="source" {"updated"} else {"execution_journal_ready"}}}),true);
        }
        let before=case.snapshot();
        assert!(case.store.commit_goal_claim(&request,&"a".repeat(64),prepared,8*1024*1024).is_err(),"{change}");
        assert_eq!(case.snapshot(),before,"{change}");
    }
}

#[test]
fn forged_ordinary_claim_event_has_no_native_retirement_authority() {
    let mut case=fixture();case.kill();
    let digest=format!("{:x}",Sha256::digest(serde_json::to_vec(&case.request).unwrap()));
    {
        let mut db=case.store.authority.lock().unwrap();
        invoke(&mut db,"orchestration.event.append",json!({"run_id":"goal_source-root","user_id":7,"tenant_id":"3",
            "sequence":case.request["source_cursor"],"event":{"type":"execution_recovery_claim","native_actor_claim":{
                "request_sha256":digest,"invoice":case.request["invoice"],"target_attempt_id":case.request["target_attempt_id"],
                "actors":[{"task_id":"pending-a","version":3},{"task_id":"pending-b","version":3}]}}}),true);
    }
    let before=case.snapshot();assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024),Err(Error::Conflict));
    assert_eq!(case.snapshot(),before);
}

#[test]
fn lost_response_reopen_returns_same_receipt_without_second_debit() {
    let mut case=fixture();case.kill();
    let claim=case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap();let after=case.snapshot();
    let Case {_directory:directory,store,request,writer}=case;drop(store);
    let database=AuthorityDatabase::open(directory.path()).unwrap();
    let reopened=NativeCheckpointStore::new(Arc::new(Mutex::new(database)),3,7,4096,1048576).unwrap();
    let case=Case {_directory:directory,store:reopened,request,writer};
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap(),claim);
    assert_eq!(case.snapshot(),after);
}

#[test]
fn wal_write_short_write_and_sync_faults_never_split_retirement_wallet_and_claim() {
    use crate::vfs::{DeterministicVfs,FaultAction,Operation,Vfs};
    for (operation,action) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Operation::Write,FaultAction::ShortWrite(7)),(Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other))] {
        let vfs=Arc::new(DeterministicVfs::new(None));let path=std::path::Path::new("/goal-claim");
        vfs.create_dir(path).unwrap();vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let database=AuthorityDatabase::initialize_with_vfs(path,vfs.clone() as Arc<dyn Vfs>).unwrap();
        let mut case=fixture_with_database(tempfile::tempdir().unwrap(),database);case.kill();let before=case.snapshot();
        vfs.arm_operation_fault(operation,1,action).unwrap();
        let acknowledged=case.store.claim_goal_actors(&case.request,8*1024*1024).is_ok();
        assert!(vfs.operation_fault_fired().unwrap(),"{operation:?}");
        let Case {_directory:directory,store,request,writer}=case;drop(store);vfs.crash().unwrap();
        let database=AuthorityDatabase::open_with_vfs(path,vfs as Arc<dyn Vfs>).unwrap();
        let store=NativeCheckpointStore::new(Arc::new(Mutex::new(database)),3,7,4096,1048576).unwrap();
        let case=Case {_directory:directory,store,request,writer};let after=case.snapshot();
        if after["claim"].is_null() {
            assert!(!acknowledged);assert_eq!(after,before);
        } else {
            assert_eq!(after["wallet"]["balance_micro"],177221);
            for index in 0..2 {
                assert_eq!(after["actors"][index]["header"]["phase"],"terminal");
                assert_eq!(after["actors"][index]["snapshot"],before["actors"][index]["snapshot"]);
            }
            assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024).unwrap(),after["claim"]);
            assert_eq!(case.snapshot(),after);
        }
    }
}

#[test]
fn chunked_latest_account_witness_checks_digest_before_retirement() {
    use base64::Engine;
    for corrupt in [false,true] {
        let mut case=fixture();case.kill();
        let fixture:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_claim.json")).unwrap();
        let body=serde_json::to_vec(&json!({"type":"execution_account_frontier","launch_id":"launch","frontier":fixture["cut"]})).unwrap();
        let digest=if corrupt {"0".repeat(64)} else {format!("{:x}",Sha256::digest(&body))};
        let chunks=body.chunks(400).collect::<Vec<_>>();let mut cursor=case.request["source_cursor"].as_u64().unwrap();
        {
            let mut db=case.store.authority.lock().unwrap();
            for (index,chunk) in chunks.iter().enumerate() {
                invoke(&mut db,"orchestration.event.append",json!({"run_id":"goal_source-root","user_id":7,"tenant_id":"3",
                    "sequence":cursor,"event":{"type":"execution_journal_payload_chunk","sha256":digest,"index":index,
                        "data":base64::engine::general_purpose::STANDARD.encode(chunk)}}),true);cursor+=1;
            }
            invoke(&mut db,"orchestration.event.append",json!({"run_id":"goal_source-root","user_id":7,"tenant_id":"3",
                "sequence":cursor,"event":{"type":"execution_account_frontier","goal_execution_archive":"tofu.goal-execution-launch/v2",
                    "payload_ref":{"chunks":chunks.len(),"byte_length":body.len(),"sha256":digest}}}),true);
        }
        case.request["account_sequence"]=json!(cursor);case.request["source_cursor"]=json!(cursor+1);
        let before=case.snapshot();let result=case.store.claim_goal_actors(&case.request,8*1024*1024);
        if corrupt {assert_eq!(result,Err(Error::Invalid));assert_eq!(case.snapshot(),before);} else {assert!(result.is_ok(),"{result:?}");}
    }
}

#[test]
fn aggregate_refuses_malformed_cut_unknown_usage_and_uncovered_root_spend() {
    let fixture:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_claim.json")).unwrap();
    let mut fees=vec![fixture["completed_fees"].clone()];
    for task in ["pending-a","pending-b"] {fees.push(actor_fee_evidence_from_checkpoint(&fixture["states"][task],7,"source-root",task,"3").unwrap());}
    assert_eq!(validate_aggregate_actor_invoice(&fixture["cut"],&fees,&fixture["invoice"]),Ok(()));
    for pointer in ["/snapshot/complete","/snapshot/model_ordinal","/actors/pending-a/tool_ordinal","/child_ordinal"] {
        let mut cut=fixture["cut"].clone();*cut.pointer_mut(pointer).unwrap()=json!("malformed");
        assert_eq!(validate_aggregate_actor_invoice(&cut,&fees,&fixture["invoice"]),Err(Error::Invalid),"{pointer}");
    }
    for (field,value) in [("usage_known",json!(false)),("root_tokens",json!(1))] {
        let mut cut=fixture["cut"].clone();cut["snapshot"][field]=value;
        assert_eq!(validate_aggregate_actor_invoice(&cut,&fees,&fixture["invoice"]),Err(Error::Ineligible));
    }
    let mut state=fixture["states"]["pending-a"].clone();state["usage"][0]=json!({});
    assert_eq!(actor_fee_evidence_from_checkpoint(&state,7,"source-root","pending-a","3"),Err(Error::Invalid));
}

#[test]
fn cost_only_claim_requires_immutable_mode_and_preserves_nonzero_cost(){
    let mut pair=replica::paired_fee(true,true);let wallet=pair.case.snapshot()["wallet"].clone();
    pair.case.kill();let receipt=pair.case.store.claim_goal_actors(&pair.case.request,8*1024*1024).unwrap();
    assert_eq!(pair.case.store.claim_goal_actors(&pair.case.request,8*1024*1024).unwrap(),receipt);
    assert!(pair.case.request["invoice"]["componentsMicro"]["totalMicro"].as_u64().unwrap()>0);
    assert_eq!(pair.case.snapshot()["wallet"],wallet);
    let record=replica::pending(&pair.case.store).pop().unwrap();
    for _ in 0..2 {pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,&record).unwrap();}
    assert_eq!(inspect_state(&pair.target,&pair.case.request),pair.case.snapshot());
    assert_eq!(pair.case.store.wallet_invoice(7,"pending-a").unwrap().unwrap()["wallet_settlement"],Value::Null);
}

#[test]
fn cost_only_claim_cannot_downgrade_legacy_paid_source(){
    let mut case=fixture();case.kill();let before=case.snapshot();
    case.request["invoice"]["format"]=json!("tofu.native-aggregate-cost/v1");
    case.request["invoice"].as_object_mut().unwrap().remove("wallet_user_id");
    case.request["invoice"].as_object_mut().unwrap().remove("reserved_micro");
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024),Err(Error::Ineligible));
    assert_eq!(case.snapshot(),before);
}

#[test]
fn cost_only_claim_rolls_back_if_original_root_has_financial_activity(){
    let directory=tempfile::tempdir().unwrap();let database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut case=fixture_with_fee_setup(directory,database,|db|{
        let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
        crate::billing::wallet_apply(db,&mut tx,&crate::billing::WalletApplyRequest {
            user_id:"wallet-7".into(),amount_micro:-1,kind:"reserve".into(),ref_type:"reserve".into(),
            ref_id:"source-root".into(),note:"conflicting admission".into(),ledger_id:"conflict".into(),
            occurred_at:1001,allow_negative:false}).unwrap();
        db.commit(tx).unwrap();
    },true);
    case.kill();let before=case.snapshot();
    assert_eq!(case.store.claim_goal_actors(&case.request,8*1024*1024),Err(Error::Ineligible));
    assert_eq!(case.snapshot(),before);
}
