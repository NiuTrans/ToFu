//! Whole consumer fault tests over an authenticated native prefix and an exact
//! application baseline. This does not certify routing the mixed app stream.
use super::*;
use super::super::replica;
use crate::native_checkpoint_store::NativeCheckpointSink;
use crate::outbox_publisher::{LogicalOutboxSink,LogicalOutboxPublishBudget};
use crate::outbox_sink::EngineLogicalOutboxSink;
use crate::vfs::{DeterministicVfs,FaultAction,Operation,Vfs};

struct Replicated {group:Group,sink:NativeCheckpointSink,source_vfs:Arc<DeterministicVfs>,target_vfs:Arc<DeterministicVfs>,_archive:tempfile::TempDir}
fn replicated()->Replicated {replicated_bound(false)}
fn replicated_bound(bound:bool)->Replicated { replicated_fee(bound,false) }
fn replicated_fee(bound:bool,cost_only:bool)->Replicated {
    let replica::Pair {mut case,target,source_vfs,target_vfs,origin}=replica::paired_fee(false,cost_only);
    let directory=tempfile::tempdir().unwrap();
    let archive=EngineLogicalOutboxSink::initialize(directory.path(),3,7,BOUND as u64).unwrap();
    let mut sink=NativeCheckpointSink::new(target,archive,LogicalOutboxCipher::new(&[72;32]),origin.clone()).unwrap();
    case.store.publish_pending(&mut sink,LogicalOutboxPublishBudget::new(100,BOUND).unwrap()).unwrap();
    case.kill();case.store.claim_goal_actors(&case.request,BOUND).unwrap();
    case.store.publish_pending(&mut sink,LogicalOutboxPublishBudget::new(100,BOUND).unwrap()).unwrap();
    let (target,archive)=sink.into_parts();
    let Case {_directory,store,request,writer}=case;drop(store);
    let database=AuthorityDatabase::open_with_vfs(std::path::Path::new(replica::REPLICA_PATH),source_vfs.clone() as Arc<dyn Vfs>).unwrap();
    assert!(!database.logical_outbox_is_configured());
    let store=replica::replica_store(database);
    // Apply the identical deterministic application preparation before enabling
    // the next native publication. No semantic-stream routing is inferred.
    let mut authorization=prepare_target(&mut store.authority.lock().unwrap(),&request);
    assert_eq!(prepare_target(&mut target.authority.lock().unwrap(),&request),authorization);
    if bound {
        let mut target_authorization=authorization.clone();
        let source=append_bound(&mut store.authority.lock().unwrap(),&request,&mut authorization,None);
        assert_eq!(source,append_bound(&mut target.authority.lock().unwrap(),&request,&mut target_authorization,None));
        assert_eq!(authorization,target_authorization);
    }
    store.authority.lock().unwrap().configure_logical_outbox(&[72;32],BOUND as u64,128*1024).unwrap();
    let case=Case {_directory,store,request,writer};
    Replicated {group:Group {case,authorization},sink:NativeCheckpointSink::new(target,archive,LogicalOutboxCipher::new(&[72;32]),origin).unwrap(),
        source_vfs,target_vfs,_archive:directory}
}
pub(super) fn state(store:&NativeCheckpointStore,authorization:&Value)->Value{
    let source=inspect_state(store,&authorization["claim_request"]);
    let targets:Vec<_>=["target-a","target-b"].iter().map(|task|{
        store.header(7,task).unwrap().map(|h|json!({"version":h.version,"header":h.value,"snapshot":store.snapshot(7,task,h.version).unwrap()}))
    }).collect();
    let db=store.authority.lock().unwrap();let mut tx=db.begin_with_identity_claim_scopes(3,7).unwrap();
    let mut events=Vec::new();
    for index in 0..3 {events.push(goal::event(&db,&mut tx,"goal_target-root","3",authorization["target_cursor"].as_u64().unwrap()+index,&mut BOUND.clone()).unwrap());}
    json!({"source":source,"targets":targets,"events":events})
}
fn capture(group:&Group)->SealedLogicalOutboxRecord{
    let leases=perform(group).unwrap();assert_eq!(leases.len(),2);drop(leases);
    let records=replica::pending(&group.case.store);assert_eq!(records.len(),1);records[0].clone()
}

#[test]
fn whole_actor_publication_survives_lost_ack_and_archived_older_target_progress(){
    let mut pair=replicated();let record=capture(&pair.group);
    let clear=LogicalOutboxCipher::new(&[72;32]).open(&record).unwrap();
    let mutation:Value=serde_json::from_slice(&clear).unwrap();
    assert!(mutation["actors"].as_array().unwrap().iter().all(|actor|actor.get("snapshot").is_none()));
    println!("goal_actor_handoff_outbox clear_bytes={} sealed_bytes={}",clear.len(),record.ciphertext.len());
    if let Some(path)=std::env::var_os("TOFU_TEST_GOAL_HANDOFF_CAPTURE"){
        std::fs::write(&path,&clear).unwrap();
        let receipt=event(&pair.group,pair.group.authorization["target_cursor"].as_u64().unwrap()+2).unwrap();
        std::fs::write(std::path::Path::new(&path).with_extension("receipt.json"),serde_json::to_vec(&receipt).unwrap()).unwrap();
    }
    pair.sink.append_durable(&record).unwrap();
    assert_eq!(replica::pending(&pair.group.case.store).len(),1);
    pair.group.case.store.publish_pending(&mut pair.sink,LogicalOutboxPublishBudget::new(100,BOUND).unwrap()).unwrap();
    assert!(replica::pending(&pair.group.case.store).is_empty());
    for task in ["target-a","target-b"] {
        let header=pair.group.case.store.header(7,task).unwrap().unwrap();
        pair.group.case.store.compare_exchange(7,task,header.version,&header.value,SnapshotWrite::Keep).unwrap();
    }
    pair.group.case.store.publish_pending(&mut pair.sink,LogicalOutboxPublishBudget::new(100,BOUND).unwrap()).unwrap();
    pair.sink.append_durable(&record).unwrap();
    let (target,_)=pair.sink.into_parts();
    assert_eq!(state(&target,&pair.group.authorization),state(&pair.group.case.store,&pair.group.authorization));
    assert!(Journal::new(scope("target-a"),&target,1048576,1024).unwrap().recover("promote").is_err());
}

#[test]
fn passive_whole_consumer_requires_budget_prefix_and_nonforwarding_authority(){
    for damage in ["bound", "no_bound", "prefix", "outbox"] {
        let pair=replicated();let record=capture(&pair.group);let (mut target,archive)=pair.sink.into_parts();
        let before=state(&target,&pair.group.authorization);
        match damage {
            "bound"=>target.goal_claim_replay_bytes=Some(64),
            "no_bound"=>target.goal_claim_replay_bytes=None,
            "prefix"=>{
                let mut db=target.authority.lock().unwrap();let mut tx=db.begin(3,7).unwrap();
                let key=versioned_document::record_key(3,7,REPLAY,"pending-b").unwrap();
                versioned_document::delete(&db,&mut tx,key,REPLAY,"pending-b",None).unwrap();db.commit(tx).unwrap();
            },
            _=>target.authority.lock().unwrap().configure_logical_outbox(&[72;32],BOUND as u64,128*1024).unwrap(),
        }
        assert!(target.replay_for_archive(&LogicalOutboxCipher::new(&[72;32]),
            &pair.group.case.store.source_uuid().unwrap(),&record,archive.archive_uuid()).is_err(),"{damage}");
        assert_eq!(state(&target,&pair.group.authorization),before,"{damage}");
    }
}

#[test]
fn authenticated_handoff_changes_never_consume_any_partial_actor_set(){
    for damage in ["context","prefix","target","account","missing","extra"] {
        let pair=replicated();let record=capture(&pair.group);let (target,_)=pair.sink.into_parts();
        let before=state(&target,&pair.group.authorization);
        let changed=replica::reseal(&record,|value|match damage{
            "context"=>value["context_sha256"]=json!("0".repeat(64)),
            "prefix"=>value["actors"][1]["source_version"]=json!(999),
            "target"=>value["actors"][1]["target_header"]["task_id"]=json!("foreign"),
            "account"=>value["authorization"]["account_frontiers"][1]["snapshot"]["tokens"]=json!(1),
            "missing"=>{value["actors"].as_array_mut().unwrap().pop();},_=>value["extra"]=json!(true),
        });
        // Source journals are archive-bound, so retain that same archive identity
        // when testing authenticated malformed content, rather than failing only
        // on the unrelated direct-replay/archive mismatch.
        let db=target.authority.lock().unwrap();let mut tx=db.begin(3,7).unwrap();
        let (receipt,_)=target.handoff_document(&db,&mut tx,REPLAY,"pending-a",4096).unwrap().unwrap();
        let archive=uuid::Uuid::parse_str(receipt["archive_uuid"].as_str().unwrap()).unwrap();drop(tx);drop(db);
        assert!(target.replay_for_archive(&LogicalOutboxCipher::new(&[72;32]),&pair.group.case.store.source_uuid().unwrap(),&changed,archive).is_err(),"{damage}");
        assert_eq!(state(&target,&pair.group.authorization),before,"{damage}");
    }
}

#[test]
fn source_capture_failure_keeps_all_original_snapshots_and_no_consumption_events(){
    let pair=replicated();let before=state(&pair.group.case.store,&pair.group.authorization);
    pair.group.case.store.authority.lock().unwrap().configure_logical_outbox(&[71;32],BOUND as u64,128*1024).unwrap();
    assert!(perform(&pair.group).is_err());assert_eq!(state(&pair.group.case.store,&pair.group.authorization),before);
}

#[test]
fn source_handoff_wal_faults_never_split_new_leases_account_cuts_and_outbox(){
    for (operation,action) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Operation::Write,FaultAction::ShortWrite(7)),(Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other))]{
        let pair=replicated();let before=state(&pair.group.case.store,&pair.group.authorization);
        pair.source_vfs.arm_operation_fault(operation,1,action).unwrap();
        let acknowledged=perform(&pair.group).is_ok();assert!(pair.source_vfs.operation_fault_fired().unwrap());
        let Replicated {group,source_vfs,..}=pair;let Group {case,authorization}=group;
        let Case {_directory,store,request,writer}=case;drop(store);source_vfs.crash().unwrap();
        let mut db=AuthorityDatabase::open_with_vfs(std::path::Path::new(replica::REPLICA_PATH),source_vfs as Arc<dyn Vfs>).unwrap();
        db.configure_logical_outbox(&[72;32],BOUND as u64,128*1024).unwrap();
        let group=Group {case:Case {_directory,store:replica::replica_store(db),request,writer},authorization};
        let after=state(&group.case.store,&group.authorization);
        if after["events"][2].is_null(){assert!(!acknowledged);assert_eq!(after,before);assert!(replica::pending(&group.case.store).is_empty());
            perform(&group).unwrap();
        }else{
            assert!(after["targets"].as_array().unwrap().iter().all(|row|!row.is_null()));
            assert!(after["source"]["actors"].as_array().unwrap().iter().all(|row|row["snapshot"].is_null()));
            assert_eq!(after["events"][0]["frontier"],group.authorization["account_frontiers"][0]);
            assert_eq!(after["events"][1]["frontier"],group.authorization["account_frontiers"][1]);
        }
        assert_eq!(replica::pending(&group.case.store).len(),1);
        assert_eq!(state(&group.case.store,&group.authorization)["source"]["wallet"],before["source"]["wallet"]);
    }
}

#[test]
fn passive_handoff_wal_faults_reopen_before_or_after_the_entire_actor_set(){
    for (operation,action) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Operation::Write,FaultAction::ShortWrite(7)),(Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other))]{
        let mut pair=replicated();let record=capture(&pair.group);
        let (target,archive)=pair.sink.into_parts();let before=state(&target,&pair.group.authorization);
        pair.sink=NativeCheckpointSink::new(target,archive,LogicalOutboxCipher::new(&[72;32]),pair.group.case.store.source_uuid().unwrap()).unwrap();
        pair.target_vfs.arm_operation_fault(operation,1,action).unwrap();
        let acknowledged=pair.sink.append_durable(&record).is_ok();assert!(pair.target_vfs.operation_fault_fired().unwrap());
        let (target,archive)=pair.sink.into_parts();drop(target);pair.target_vfs.crash().unwrap();
        let target=replica::replica_store(AuthorityDatabase::open_with_vfs(std::path::Path::new(replica::REPLICA_PATH),pair.target_vfs as Arc<dyn Vfs>).unwrap());
        let after=state(&target,&pair.group.authorization);let expected=state(&pair.group.case.store,&pair.group.authorization);
        if after["events"][2].is_null(){assert!(!acknowledged);assert_eq!(after,before);}else{assert_eq!(after,expected);}
        let mut sink=NativeCheckpointSink::new(target,archive,LogicalOutboxCipher::new(&[72;32]),pair.group.case.store.source_uuid().unwrap()).unwrap();
        sink.append_durable(&record).unwrap();let (target,_)=sink.into_parts();assert_eq!(state(&target,&pair.group.authorization),expected);
    }
}

#[test]
fn bound_witness_replays_and_retries_with_the_same_protected_header(){
    let mut pair=replicated_bound(true);let record=capture(&pair.group);
    pair.sink.append_durable(&record).unwrap();
    pair.sink.append_durable(&record).unwrap();
    pair.group.case.store.publish_pending(&mut pair.sink,LogicalOutboxPublishBudget::new(100,BOUND).unwrap()).unwrap();
    let (target,_)=pair.sink.into_parts();
    assert_eq!(state(&target,&pair.group.authorization),state(&pair.group.case.store,&pair.group.authorization));
    assert!(target.header(7,"pending-a").unwrap().unwrap().value["goal_actor_handoff"]["witness_sha256"].is_string());
}

#[test]
fn bound_witness_wal_faults_never_publish_a_partial_consumption(){
    for operation in [Operation::Write,Operation::SyncData] {
        let pair=replicated_bound(true);let before=state(&pair.group.case.store,&pair.group.authorization);
        pair.source_vfs.arm_operation_fault(operation,1,FaultAction::ErrorBefore(io::ErrorKind::Other)).unwrap();
        let acknowledged=perform(&pair.group).is_ok();assert!(pair.source_vfs.operation_fault_fired().unwrap());
        let Replicated {group,source_vfs,..}=pair;let Group {case,authorization}=group;
        let Case {_directory,store,request,writer}=case;drop(store);source_vfs.crash().unwrap();
        let mut db=AuthorityDatabase::open_with_vfs(std::path::Path::new(replica::REPLICA_PATH),source_vfs as Arc<dyn Vfs>).unwrap();
        db.configure_logical_outbox(&[72;32],BOUND as u64,128*1024).unwrap();
        let group=Group {case:Case {_directory,store:replica::replica_store(db),request,writer},authorization};
        let after=state(&group.case.store,&group.authorization);
        if after["events"][2].is_null(){assert!(!acknowledged);assert_eq!(after,before);perform(&group).unwrap();}
        let complete=state(&group.case.store,&group.authorization);
        for (source,row) in complete["source"]["actors"].as_array().unwrap().iter().zip(complete["events"][2]["actors"].as_array().unwrap()) {
            assert_eq!(source["header"]["goal_actor_handoff"]["witness_sha256"],replay::digest(row,BOUND).unwrap());
        }
    }
}

#[test]
fn cost_only_whole_handoff_publication_retries_without_financial_mutation(){
    let mut pair=replicated_fee(false,true);let before=pair.group.case.snapshot()["wallet"].clone();
    let record=capture(&pair.group);
    for _ in 0..2 {pair.sink.append_durable(&record).unwrap();}
    let (target,_)=pair.sink.into_parts();
    assert_eq!(state(&pair.group.case.store,&pair.group.authorization),state(&target,&pair.group.authorization));
    assert_eq!(pair.group.case.snapshot()["wallet"],before);
}
