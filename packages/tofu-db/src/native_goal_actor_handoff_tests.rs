//! Actual common owned leases over one native actor/account consumption transaction.
use super::*;
use tofu_runtime::checkpoint_journal::{ActorHandoffSpec,OwnedLease,validate_actor_handoff_accounts};

const BOUND:usize=8*1024*1024;
struct Group {case:Case,authorization:Value}
#[path = "native_goal_actor_handoff_replay_tests.rs"]
mod publication;
#[path = "native_goal_actor_bridge_tests.rs"]
mod bridge;
fn prepare(mut case:Case)->Group {
    case.kill();case.store.claim_goal_actors(&case.request,BOUND).unwrap();
    let authorization=prepare_target(&mut case.store.authority.lock().unwrap(),&case.request);
    Group {case,authorization}
}
fn prepare_target(db:&mut AuthorityDatabase,request:&Value)->Value {
    let fixture:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_handoff.json")).unwrap();
    let attempt=invoke(db,"turn.attempt.get",json!({"attempt_id":request["target_attempt_id"],"user_id":7}),false);
    let page=invoke(db,"orchestration.event.page",json!({"run_id":"goal_target-root","user_id":7,"tenant_id":"3","cursor":0}),false);
    let mut cursor=page["next_cursor"].as_u64().unwrap();let launch_sequence=cursor;
    let append=|db:&mut AuthorityDatabase,seq:u64,event:Value|invoke(db,"orchestration.event.append",
        json!({"run_id":"goal_target-root","user_id":7,"tenant_id":"3","sequence":seq,"event":event}),true);
    let cost_only=fee_account::cost_only(&request["invoice"]);
    let mut launch=json!({"type":"execution_journal_start","parent_run_id":"goal_source-root","launch_metadata":{
        "format":"tofu.goal-execution-launch/v2","run_id":"goal_target-root","task_id":"target-root","turn_id":attempt["turnId"],
        "attempt_id":request["target_attempt_id"],"conversation_id":"conversation","owner_user_id":7,"tenant_id":"3",
        "launch_id":"target-launch","objective":"Preserve work","launch":{"model":"alpha"}}});
    if cost_only {launch["launch_metadata"]["fee_account"]=json!({"mode":"cost"});}
    append(db,cursor,launch);cursor+=1;
    append(db,cursor,json!({"type":"execution_journal_ready"}));cursor+=1;
    let mut bindings=Vec::new();
    for (source,target) in [("pending-a","target-a"),("pending-b","target-b")] {
        invoke(db,"invocation.register_child",json!({"user_id":7,"conversation_id":"conversation",
            "parent_task_id":"target-root","child_task_id":target}),true);
        let row=invoke(db,"record.get",json!({"namespace":"task_results","key":target,"user_id":7}),false);
        bindings.push(json!({"source_task_id":source,"target_task_id":target,"target_registration_version":row["version"]}));
    }
    let account_sequence=cursor;
    append(db,cursor,json!({"type":"execution_account_frontier","launch_id":"target-launch","frontier":fixture["before"]}));cursor+=1;
    let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
    for (kind,amount) in [("reserve",-50000),("native_admission",0)] {
        if cost_only {continue;}
        crate::billing::wallet_apply(&db,&mut tx,&crate::billing::WalletApplyRequest {user_id:"wallet-7".into(),amount_micro:amount,
            kind:kind.into(),ref_type:"reserve".into(),ref_id:"target-root".into(),note:"handoff fixture".into(),
            ledger_id:format!("target-{kind}"),occurred_at:1001,allow_negative:false}).unwrap();
    }
    if !cost_only {db.commit(tx).unwrap();}
    json!({"format":"tofu.native-goal-actor-handoff/v1","claim_request":request,
        "target_launch_sequence":launch_sequence,"target_account_sequence":account_sequence,"target_cursor":cursor,
        "target_reserved_micro":if cost_only {0}else{50000},"actors":bindings,"account_frontiers":fixture["frontiers"]})
}
fn specs()->Vec<ActorHandoffSpec>{
    [("pending-a","target-a"),("pending-b","target-b")].into_iter().map(|(source,target)|ActorHandoffSpec {
        scope:scope(target),source_task_id:source.into(),source_root_task_id:"source-root".into(),
        target_root_task_id:"target-root".into(),token:format!("resume-{target}")}).collect()
}
fn perform(group:&Group)->Result<Vec<OwnedLease>,Error>{
    let store=NativeCheckpointStore::new(Arc::clone(&group.case.store.authority),3,7,4096,1048576).unwrap();
    OwnedLease::handoff_retired_actors(Arc::new(store),specs(),&group.authorization,1048576,1024,BOUND)
}
fn event(group:&Group,sequence:u64)->Option<Value>{
    let db=group.case.store.authority.lock().unwrap();let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
    goal::event(&db,&mut tx,"goal_target-root","3",sequence,&mut BOUND.clone()).unwrap()
}

#[test]
fn shared_python_account_batch_preserves_every_native_paid_counter(){
    let source:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_claim.json")).unwrap();
    let fixture:Value=serde_json::from_str(include_str!("../tests/fixtures/native_goal_actor_handoff.json")).unwrap();
    let cuts=fixture["frontiers"].as_array().unwrap();
    assert_eq!(validate_actor_handoff_accounts(&source["cut"],&fixture["before"],cuts,
        &[("pending-a","target-a"),("pending-b","target-b")],"target-root"),Ok(()));
    for field in ["tokens","tool_calls","model_ordinal","tool_ordinal"] {
        let mut altered=cuts.clone();altered[1]["snapshot"][field]=json!(999999);
        assert!(validate_actor_handoff_accounts(&source["cut"],&fixture["before"],&altered,
            &[("pending-a","target-a"),("pending-b","target-b")],"target-root").is_err());
    }
}

#[test]
fn whole_claim_consumer_returns_common_leases_only_after_all_actor_and_account_writes(){
    let group=prepare(fixture());let before=group.case.snapshot();let leases=perform(&group).unwrap();
    assert_eq!(leases.len(),2);
    for (index,(source,target)) in [("pending-a","target-a"),("pending-b","target-b")].iter().enumerate(){
        let state=leases[index].load().unwrap().unwrap();
        assert_eq!(state["task_id"],*target);assert_eq!(state["shared_model_account"]["root_task_id"],"target-root");
        assert_eq!(state["shared_model_account"]["actor_task_id"],*target);
        for field in ["messages","usage","tool_calls","progress","round_limit","tool_limit"] {assert_eq!(state[field],before["actors"][index]["snapshot"][field]);}
        assert_eq!(state["host_fee_projection"]["rounds"],before["actors"][index]["snapshot"]["host_fee_projection"]["rounds"]);
        let old=group.case.store.header(7,source).unwrap().unwrap();
        assert!(group.case.store.snapshot(7,source,old.version).unwrap().is_none());
        assert_eq!(old.value["goal_actor_handoff"]["target_task_id"],*target);
        assert_eq!(leases[index].begin(1),Err(Error::Ineligible));
    }
    let cursor=group.authorization["target_cursor"].as_u64().unwrap();
    for index in 0..2 {assert_eq!(event(&group,cursor+index).unwrap()["frontier"],group.authorization["account_frontiers"][index as usize]);}
    assert_eq!(event(&group,cursor+2).unwrap()["type"],"execution_recovery_consumed");
    assert_eq!(group.case.snapshot()["wallet"],before["wallet"]);
    assert!(perform(&group).is_err());
    assert_eq!(group.case.store.claim_goal_actors(&group.case.request,BOUND).unwrap(),before["claim"]);
}

#[test]
fn invalid_whole_consumer_never_partially_creates_a_successor(){
    for damage in ["invoice","account","registration","membership","cursor","extra"] {
        let mut group=prepare(fixture());let before=group.case.snapshot();
        match damage {
            "invoice"=>group.authorization["target_reserved_micro"]=json!(49999),
            "account"=>group.authorization["account_frontiers"][1]["actors"]["target-b"]["tokens"]=json!(9),
            "registration"=>group.authorization["actors"][1]["target_registration_version"]=json!(999),
            "membership"=>group.authorization["actors"].as_array_mut().unwrap().pop().map(|_|()).unwrap(),
            "cursor"=>group.authorization["target_cursor"]=json!(999),_=>group.authorization["extra"]=json!(true),
        }
        assert!(perform(&group).is_err(),"{damage}");assert_eq!(group.case.snapshot(),before,"{damage}");
        for task in ["target-a","target-b"] {assert!(group.case.store.header(7,task).unwrap().is_none());}
    }
}

#[test]
fn whole_consumer_refuses_invalid_spec_sets_and_explicit_byte_exhaustion(){
    for damage in ["missing", "duplicate", "root", "owner", "token", "bytes"] {
        let group=prepare(fixture());let before=publication::state(&group.case.store,&group.authorization);
        let mut candidates=specs();
        match damage {
            "missing"=>{candidates.pop();},
            "duplicate"=>candidates[1].source_task_id="pending-a".into(),
            "root"=>candidates[1].target_root_task_id="other-root".into(),
            "owner"=>candidates[1].scope.owner=8,
            "token"=>candidates[1].token.clear(),_=>(),
        }
        let store=NativeCheckpointStore::new(Arc::clone(&group.case.store.authority),3,7,4096,1048576).unwrap();
        assert!(OwnedLease::handoff_retired_actors(Arc::new(store),candidates,&group.authorization,
            1048576,1024,if damage=="bytes" {10}else{BOUND}).is_err(),"{damage}");
        assert_eq!(publication::state(&group.case.store,&group.authorization),before,"{damage}");
    }
}

fn prepare_bound(group:&mut Group,damage:Option<&str>)->Vec<(u64,Value)>{
    append_bound(&mut group.case.store.authority.lock().unwrap(),&group.case.request,&mut group.authorization,damage)
}
fn append_bound(db:&mut AuthorityDatabase,request:&Value,authorization:&mut Value,damage:Option<&str>)->Vec<(u64,Value)>{
    let mut prepared=Vec::new();
    let mut cursor=authorization["target_cursor"].as_u64().unwrap();
    for (index,binding) in authorization["actors"].as_array().unwrap().iter().enumerate(){
        let target=binding["target_task_id"].as_str().unwrap();
        let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
        let original=request["actors"].as_array().unwrap().iter().find_map(|actor|{
            let event=goal::logical_event(&db,&mut tx,"goal_source-root","3",actor["event_sequence"].as_u64().unwrap(),&mut BOUND.clone()).unwrap();
            (event["invocation_id"]==binding["source_task_id"]).then_some(event)
        }).unwrap();drop(tx);
        let mut event=json!({"type":"execution_effect_prepared","key":original["key"],"kind":"agent",
            "input_hash":original["input_hash"],"invocation_id":target,"invocation_completion":{
                "format":"tofu.flow.invocation-completion/v1","nonce":"12345678901234567890123456789012",
                "account_root_task_id":"target-root","identity":{"user_id":7,"conversation_id":"conversation",
                    "parent_task_id":"target-root","child_task_id":target,"archive_scope":format!("invocation:7:{target}")}}});
        if index==1 {match damage {
            Some("missing")=>continue,
            Some("hash")=>event["input_hash"]=json!("f".repeat(64)),
            Some("nonce")=>event["invocation_completion"]["nonce"]=json!("invalid"),
            Some("owner")=>event["invocation_completion"]["identity"]["user_id"]=json!(8),
            Some("key")=>event["key"]=json!("another-key"),
            _=>(),
        }}
        invoke(db,"orchestration.event.append",json!({"run_id":"goal_target-root","user_id":7,
            "tenant_id":"3","sequence":cursor,"event":event}),true);
        prepared.push((cursor,event));cursor+=1;
    }
    authorization["target_cursor"]=json!(cursor);prepared
}

#[test]
fn bound_preparation_is_authenticated_by_the_retired_native_header(){
    let mut group=prepare(fixture());let prepared=prepare_bound(&mut group,None);
    let leases=perform(&group).unwrap();let cursor=group.authorization["target_cursor"].as_u64().unwrap()+3;
    let receipt=event(&group,cursor-1).unwrap();
    let mut request:Request=serde_json::from_value(group.case.request.clone()).unwrap();
    request.source_run_id="goal_target-root".into();request.source_cursor=cursor;
    request.launch_sequence=group.authorization["target_launch_sequence"].as_u64().unwrap();
    request.invoice["task_id"]=json!("target-root");
    for ((sequence,preparation),row) in prepared.iter().zip(receipt["actors"].as_array().unwrap()){
        assert_eq!(&row["preparation"],preparation);
        let header=group.case.store.header(7,row["source_task_id"].as_str().unwrap()).unwrap().unwrap();
        assert_eq!(header.value["goal_actor_handoff"]["witness_sha256"],replay::digest(row,BOUND).unwrap());
        let db=group.case.store.authority.lock().unwrap();let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
        assert_eq!(group.case.store.incoming_goal_actor_witness(&db,&mut tx,&request,preparation,*sequence,&mut BOUND.clone()).unwrap(),*row);
        let mut changed=preparation.clone();changed["invocation_completion"]["nonce"]=json!("b".repeat(32));
        assert!(group.case.store.incoming_goal_actor_witness(&db,&mut tx,&request,&changed,*sequence,&mut BOUND.clone()).is_err());
    }
    let mut started=prepared[0].1.clone();started["type"]=json!("execution_effect_started");
    let mut db=group.case.store.authority.lock().unwrap();
    invoke(&mut db,"orchestration.event.append",json!({"run_id":"goal_target-root","user_id":7,
        "tenant_id":"3","sequence":cursor,"event":started}),true);
    request.source_cursor+=1;
    let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
    assert!(group.case.store.incoming_goal_actor_witness(&db,&mut tx,&request,&prepared[0].1,prepared[0].0,&mut BOUND.clone()).is_err());
    assert_eq!(group.case.store.incoming_goal_actor_witness(&db,&mut tx,&request,&started,cursor,&mut BOUND.clone()).unwrap(),receipt["actors"][0]);
    drop(tx);drop(db);drop(leases);
}

#[test]
fn bound_preparation_refuses_changed_identity_and_partial_membership_atomically(){
    for damage in ["missing","hash","nonce","owner","key"] {
        let mut group=prepare(fixture());prepare_bound(&mut group,Some(damage));
        let before=publication::state(&group.case.store,&group.authorization);
        assert!(perform(&group).is_err(),"{damage}");
        assert_eq!(publication::state(&group.case.store,&group.authorization),before,"{damage}");
    }
}

#[test]
fn ordinary_consumption_event_cannot_manufacture_a_native_preparation_witness(){
    let mut group=prepare(fixture());let prepared=prepare_bound(&mut group,None);
    let before=group.case.snapshot();let cursor=group.authorization["target_cursor"].as_u64().unwrap();
    let actors:Vec<_>=group.authorization["actors"].as_array().unwrap().iter().zip(&prepared).map(|(binding,(_,event))|{
        let mut row=binding.clone();row["preparation"]=event.clone();
        row["effect_origin"]=json!({"task_id":binding["source_task_id"],"parent_task_id":"source-root"});row
    }).collect();
    let fake=json!({"type":"execution_recovery_consumed","source_run_id":"goal_source-root",
        "source_cursor":group.case.request["source_cursor"],"request_sha256":replay::digest(&group.authorization,BOUND).unwrap(),"actors":actors});
    let mut request:Request=serde_json::from_value(group.case.request.clone()).unwrap();
    request.source_run_id="goal_target-root".into();request.source_cursor=cursor+1;
    request.launch_sequence=group.authorization["target_launch_sequence"].as_u64().unwrap();request.invoice["task_id"]=json!("target-root");
    let mut db=group.case.store.authority.lock().unwrap();
    invoke(&mut db,"orchestration.event.append",json!({"run_id":"goal_target-root","user_id":7,"tenant_id":"3","sequence":cursor,"event":fake}),true);
    let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
    for (sequence,event) in prepared {
        assert_eq!(group.case.store.incoming_goal_actor_witness(&db,&mut tx,&request,&event,sequence,&mut BOUND.clone()),Err(Error::Ineligible));
    }
    drop(tx);drop(db);
    assert_eq!(group.case.snapshot(),before);
    assert!(group.case.store.header(7,"target-a").unwrap().is_none());
}
