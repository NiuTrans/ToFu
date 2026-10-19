//! A passive authority starts from the exact prior application prefix, then
//! receives authenticated native updates. No live application/financial data.
use super::*;
use crate::vfs::{DeterministicVfs,FaultAction,Operation,Vfs};
const EVIDENCE_BYTES:usize=8*1024*1024;
pub(super) const REPLICA_PATH:&str="/goal-claim-replica";
pub(super) struct Pair { pub(super) case:Case,pub(super) target:NativeCheckpointStore,pub(super) source_vfs:Arc<DeterministicVfs>,pub(super) target_vfs:Arc<DeterministicVfs>,pub(super) origin:String }
pub(super) fn pending(store:&NativeCheckpointStore) -> Vec<SealedLogicalOutboxRecord> {
    store.authority.lock().unwrap().logical_outbox_pending_bounded(3,7,100,EVIDENCE_BYTES).unwrap().into_iter().map(|entry|entry.record).collect()
}
pub(super) fn replica_store(database:AuthorityDatabase) -> NativeCheckpointStore {
    NativeCheckpointStore::new(Arc::new(Mutex::new(database)),3,7,4096,1048576).unwrap()
        .with_goal_claim_replay_budget(EVIDENCE_BYTES).unwrap()
}
pub(super) fn paired(replay_prefix:bool) -> Pair { paired_fee(replay_prefix,false) }
pub(super) fn paired_fee(replay_prefix:bool,cost_only:bool) -> Pair {
    let vfs=Arc::new(DeterministicVfs::new(None));let path=std::path::Path::new(REPLICA_PATH);
    vfs.create_dir(path).unwrap();vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let database=AuthorityDatabase::initialize_with_vfs(path,vfs.clone() as Arc<dyn Vfs>).unwrap();
    let mut prior=None;
    let case=fixture_with_fee_setup(tempfile::tempdir().unwrap(),database,|db| {
        // A private exact baseline, before outbox configuration/native actors.
        // Application-wide semantic stream routing is a separate composition.
        prior=Some(Arc::new(vfs.fork_durable().unwrap()));
        db.configure_logical_outbox(&[72;32],EVIDENCE_BYTES as u64,128*1024).unwrap();
    },cost_only);
    let target_vfs=prior.unwrap();
    let target=replica_store(AuthorityDatabase::open_with_vfs(path,target_vfs.clone() as Arc<dyn Vfs>).unwrap());
    let origin=case.store.source_uuid().unwrap();
    if replay_prefix {
        let records=pending(&case.store);assert_eq!(records.len(),6);
        for record in records {target.replay(&LogicalOutboxCipher::new(&[72;32]),&origin,&record).unwrap();}
        assert_eq!(inspect_state(&target,&case.request),case.snapshot());
    }
    Pair {case,target,source_vfs:vfs,target_vfs,origin}
}
fn claim(pair:&mut Pair) -> SealedLogicalOutboxRecord {
    pair.case.kill();pair.case.store.claim_goal_actors(&pair.case.request,EVIDENCE_BYTES).unwrap();
    let records=pending(&pair.case.store);assert_eq!(records.len(),7);records.last().unwrap().clone()
}
pub(super) fn reseal(record:&SealedLogicalOutboxRecord, edit:impl FnOnce(&mut Value)) -> SealedLogicalOutboxRecord {
    let cipher=LogicalOutboxCipher::new(&[72;32]);
    let mut value:Value=serde_json::from_slice(&cipher.open(record).unwrap()).unwrap();edit(&mut value);
    let bytes=serde_json::to_vec(&value).unwrap();let mut identity=record.identity.clone();
    identity.request_digest=Sha256::digest(&bytes).into();cipher.seal(identity,&bytes).unwrap()
}

#[test]
fn aggregate_replay_copies_all_actors_wallet_and_claim_exactly_once_without_promotion() {
    let mut pair=paired(true);let record=claim(&mut pair);let expected=pair.case.snapshot();
    let cipher=LogicalOutboxCipher::new(&[72;32]);
    for _ in 0..2 {
        assert_eq!(pair.target.replay(&cipher,&pair.origin,&record).unwrap(),pair.case.request["source_cursor"].as_u64().unwrap()+1);
        assert_eq!(inspect_state(&pair.target,&pair.case.request),expected);
    }
    assert_eq!(pair.target.claim_goal_actors(&pair.case.request,EVIDENCE_BYTES),Err(Error::Ineligible));
    assert_eq!(Journal::new(scope("pending-a"),&pair.target,1048576,1024).unwrap().recover("promote").err(),Some(Error::Ineligible));
    assert_eq!(pair.target.wallet_invoice(7,"pending-a"),Err(Error::Ineligible));
    assert_eq!(inspect_state(&pair.target,&pair.case.request),expected);
    println!("goal_claim_outbox clear_bytes={} sealed_bytes={}",cipher.open(&record).unwrap().len(),record.ciphertext.len());
    // Optional exported fixture lets the independent JSON Schema implementation
    // validate actual native capture bytes, including its external references.
    if let Some(path)=std::env::var_os("TOFU_TEST_GOAL_CLAIM_CAPTURE") {
        std::fs::write(path,cipher.open(&record).unwrap()).unwrap();
    }
}

#[test]
fn aggregate_replay_requires_explicit_budget_and_an_exact_authenticated_prefix() {
    let mut pair=paired(true);let record=claim(&mut pair);let before=inspect_state(&pair.target,&pair.case.request);
    let cipher=LogicalOutboxCipher::new(&[72;32]);
    pair.target.goal_claim_replay_bytes=None;
    assert_eq!(pair.target.replay(&cipher,&pair.origin,&record),Err(Error::Ineligible));
    pair.target.goal_claim_replay_bytes=Some(10);
    assert_eq!(pair.target.replay(&cipher,&pair.origin,&record),Err(Error::Oversize));
    pair.target.goal_claim_replay_bytes=Some(EVIDENCE_BYTES);
    assert!(pair.target.replay(&LogicalOutboxCipher::new(&[71;32]),&pair.origin,&record).is_err());
    assert!(pair.target.replay(&cipher,&uuid::Uuid::new_v4().to_string(),&record).is_err());
    assert_eq!(inspect_state(&pair.target,&pair.case.request),before);
    pair.target.authority.lock().unwrap().configure_logical_outbox(&[72;32],EVIDENCE_BYTES as u64,128*1024).unwrap();
    assert_eq!(pair.target.replay(&cipher,&pair.origin,&record),Err(Error::Ineligible));
    assert_eq!(inspect_state(&pair.target,&pair.case.request),before);
}

#[test]
fn aggregate_replay_refuses_authenticated_changes_without_partial_retirement() {
    for change in ["context","invoice","claim","inventory","extra"] {
        let mut pair=paired(true);let record=claim(&mut pair);let before=inspect_state(&pair.target,&pair.case.request);
        let changed=reseal(&record,|value|match change {
            "context"=>value["context_sha256"]=json!("0".repeat(64)),
            "invoice"=>value["request"]["invoice"]["reserved_micro"]=json!(99999),
            "claim"=>value["claim"]["native_actor_claim"]["actors"][0]["version"]=json!(5),
            "inventory"=>{value["request"]["actors"].as_array_mut().unwrap().remove(0);},
            _=>value["extra"]=json!(true),
        });
        assert!(pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,&changed).is_err(),"{change}");
        assert_eq!(inspect_state(&pair.target,&pair.case.request),before,"{change}");
    }
}

#[test]
fn aggregate_replay_detects_changed_registered_evidence_and_missing_actor_prefix() {
    let mut pair=paired(true);let record=claim(&mut pair);
    {
        let mut db=pair.target.authority.lock().unwrap();
        let row=invoke(&mut db,"record.get",json!({"namespace":"task_results","key":"finished-c","user_id":7}),false);
        let mut value=row["value"].clone();value["status"]=json!("done");
        invoke(&mut db,"task_results.checkpoint",json!({"key":"finished-c","value":value,"expected_version":row["version"],
            "guard_contract":crate::generated_tofudb_ir::TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,"require_parent":true}),true);
    }
    let before=inspect_state(&pair.target,&pair.case.request);
    assert!(pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,&record).is_err());
    assert_eq!(inspect_state(&pair.target,&pair.case.request),before);
    let mut pair=paired(false);let record=claim(&mut pair);
    for row in pending(&pair.case.store).iter().take(3) {pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,row).unwrap();}
    assert!(pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,&record).is_err());
    let mut db=pair.target.authority.lock().unwrap();
    assert_eq!(invoke(&mut db,"billing.wallet.get",json!({"user_id":"wallet-7"}),false)["balance_micro"],100000);
    drop(db);assert_eq!(pair.target.header(7,"pending-a").unwrap().unwrap().value["phase"],"tools_pending");
    assert!(pair.target.header(7,"pending-b").unwrap().is_none());
}

#[test]
fn aggregate_replay_write_and_sync_faults_reopen_before_or_after_the_whole_decision() {
    for (operation,action) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Operation::Write,FaultAction::ShortWrite(7)),(Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other))] {
        let mut pair=paired(true);let before=inspect_state(&pair.target,&pair.case.request);let record=claim(&mut pair);
        pair.target_vfs.arm_operation_fault(operation,1,action).unwrap();
        let acknowledged=pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,&record).is_ok();
        assert!(pair.target_vfs.operation_fault_fired().unwrap(),"{operation:?}");
        let Pair {case,target,target_vfs,origin,..}=pair;drop(target);target_vfs.crash().unwrap();
        let target=replica_store(AuthorityDatabase::open_with_vfs(std::path::Path::new(REPLICA_PATH),target_vfs as Arc<dyn Vfs>).unwrap());
        let after=inspect_state(&target,&case.request);
        if after["claim"].is_null() {assert!(!acknowledged);assert_eq!(after,before);} else {assert_eq!(after,case.snapshot());}
        target.replay(&LogicalOutboxCipher::new(&[72;32]),&origin,&record).unwrap();
        assert_eq!(inspect_state(&target,&case.request),case.snapshot());
    }
}

#[test]
fn source_outbox_capture_failure_rolls_back_retirements_wallet_and_goal_receipt() {
    let mut pair=paired(true);pair.case.kill();let before=pair.case.snapshot();let records=pending(&pair.case.store);
    // Existing source key identity makes capture fail at the end of the claim,
    // after all candidate business writes have been staged in the transaction.
    pair.case.store.authority.lock().unwrap().configure_logical_outbox(&[71;32],EVIDENCE_BYTES as u64,128*1024).unwrap();
    assert!(pair.case.store.claim_goal_actors(&pair.case.request,EVIDENCE_BYTES).is_err());
    assert_eq!(pair.case.snapshot(),before);
    pair.case.store.authority.lock().unwrap().configure_logical_outbox(&[72;32],EVIDENCE_BYTES as u64,128*1024).unwrap();
    assert_eq!(pending(&pair.case.store),records);
}

#[test]
fn aggregate_replay_lost_response_survives_target_reopen() {
    let mut pair=paired(true);let record=claim(&mut pair);
    pair.target.replay(&LogicalOutboxCipher::new(&[72;32]),&pair.origin,&record).unwrap();
    let Pair {case,target,target_vfs,origin,..}=pair;drop(target);
    let target=replica_store(AuthorityDatabase::open_with_vfs(std::path::Path::new(REPLICA_PATH),target_vfs as Arc<dyn Vfs>).unwrap());
    target.replay(&LogicalOutboxCipher::new(&[72;32]),&origin,&record).unwrap();
    assert_eq!(inspect_state(&target,&case.request),case.snapshot());
}

#[test]
fn source_wal_faults_never_separate_claim_from_encrypted_outbox_capture() {
    for (operation,action) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Operation::Write,FaultAction::ShortWrite(7)),(Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other))] {
        let mut pair=paired(false);pair.case.kill();let before=pair.case.snapshot();
        pair.source_vfs.arm_operation_fault(operation,1,action).unwrap();
        let acknowledged=pair.case.store.claim_goal_actors(&pair.case.request,EVIDENCE_BYTES).is_ok();
        assert!(pair.source_vfs.operation_fault_fired().unwrap(),"{operation:?}");
        let Pair {case,source_vfs,..}=pair;
        let Case {_directory:directory,store,request,writer}=case;drop(store);source_vfs.crash().unwrap();
        let mut database=AuthorityDatabase::open_with_vfs(std::path::Path::new(REPLICA_PATH),source_vfs as Arc<dyn Vfs>).unwrap();
        database.configure_logical_outbox(&[72;32],EVIDENCE_BYTES as u64,128*1024).unwrap();
        let case=Case {_directory:directory,store:replica_store(database),request,writer};let after=case.snapshot();
        if after["claim"].is_null() {
            assert!(!acknowledged);assert_eq!(after,before);assert_eq!(pending(&case.store).len(),6);
        } else {
            assert_eq!(pending(&case.store).len(),7);
            assert_eq!(after["wallet"]["balance_micro"],177221);
            for actor in after["actors"].as_array().unwrap() {assert_eq!(actor["header"]["phase"],"terminal");}
        }
        let claim=case.store.claim_goal_actors(&case.request,EVIDENCE_BYTES).unwrap();
        let records=pending(&case.store);assert_eq!(records.len(),7);
        let captured:Value=serde_json::from_slice(&LogicalOutboxCipher::new(&[72;32]).open(records.last().unwrap()).unwrap()).unwrap();
        assert_eq!(captured["claim"],claim);
    }
}

#[test]
fn aggregate_publisher_recovers_lost_ack_and_verified_older_receipts() {
    use crate::native_checkpoint_store::NativeCheckpointSink;
    use crate::outbox_publisher::{LogicalOutboxSink,LogicalOutboxPublishBudget};
    use crate::outbox_sink::EngineLogicalOutboxSink;
    let Pair {mut case,target,origin,..}=paired(false);let directory=tempfile::tempdir().unwrap();
    let archive=EngineLogicalOutboxSink::initialize(directory.path(),3,7,EVIDENCE_BYTES as u64).unwrap();
    let mut sink=NativeCheckpointSink::new(target,archive,LogicalOutboxCipher::new(&[72;32]),origin).unwrap();
    let budget=||LogicalOutboxPublishBudget::new(100,EVIDENCE_BYTES).unwrap();
    case.store.publish_pending(&mut sink,budget()).unwrap();assert!(pending(&case.store).is_empty());
    case.kill();let claim=case.store.claim_goal_actors(&case.request,EVIDENCE_BYTES).unwrap();let record=pending(&case.store).pop().unwrap();
    // The target and archive commit, but the source does not receive the ACK.
    sink.append_durable(&record).unwrap();assert_eq!(pending(&case.store).len(),1);
    case.store.publish_pending(&mut sink,budget()).unwrap();assert!(pending(&case.store).is_empty());
    for task in ["pending-a","pending-b"] {
        let header=case.store.header(7,task).unwrap().unwrap();
        case.store.compare_exchange(7,task,header.version,&header.value,SnapshotWrite::Keep).unwrap();
    }
    case.store.publish_pending(&mut sink,budget()).unwrap();
    assert_eq!(case.store.claim_goal_actors(&case.request,EVIDENCE_BYTES).unwrap(),claim);
    sink.append_durable(&record).unwrap();
    let (target,_)=sink.into_parts();assert_eq!(inspect_state(&target,&case.request),case.snapshot());
}
