//! Accepted ancestry, atomic composite replication and private snapshot retirement.
use super::*;
use serde_json::json;
use tofu_runtime::checkpoint_journal::{Journal, ResumeLineage, Scope};

fn invoke(database: &mut AuthorityDatabase, operation: &str, payload: Value, write: bool) -> Value {
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, AuthenticatedScope};
    let request = Request { correlation_id: [3;16], deadline_unix_ms: 10000000000,
        owner_id:7,tenant_id:Some(3),command_id:write.then(||payload["command_id"].as_str()
            .map(str::to_owned).unwrap_or_else(||uuid::Uuid::new_v4().to_string())),
        schema_id:crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation:operation.into(),payload:serde_json::to_vec(&payload).unwrap() };
    let admitted=admit_request(&request,NegotiatedProtocol {protocol_version:PROTOCOL_VERSION,
        schema_id:request.schema_id},AuthenticatedScope {owner_id:7,tenant_id:Some(3)},1000).unwrap();
    serde_json::from_slice(&crate::semantic_executor::execute_admitted_request(database,admitted,1000)
        .unwrap_or_else(|error|panic!("{operation}: {error:?}"))).unwrap()
}
fn bound(database:&mut AuthorityDatabase,attempt:&str,task:&str) {
    assert_eq!(invoke(database,"turn.attempt.claim",json!({"attempt_id":attempt,"dispatch_owner_id":"test","user_id":7}),true),true);
    invoke(database,"turn.attempt.bind",json!({"attempt_id":attempt,"task_id":task,"dispatch_owner_id":"test","user_id":7}),true);
    assert_eq!(invoke(database,"turn.attempt.start",json!({"attempt_id":attempt,"task_id":task,"user_id":7}),true)["status"],"running");
}
fn accepted(database:&mut AuthorityDatabase) -> ResumeLineage {
    let pair=invoke(database,"turn.create_pair",json!({"conversation_id":"conversation","user_id":7,
        "command_id":"create","input_projection":{"content":"question"},"config":{},
        "conversation_defaults":{"allowCreate":true,"title":"Handoff","settings":{}}}),true);
    let source=pair["attempt"]["attemptId"].as_str().unwrap();let turn=pair["turn"]["turnId"].as_str().unwrap();
    bound(database,source,"task");
    invoke(database,"turn.event.record",json!({"attempt_id":source,"task_id":"task","user_id":7,"terminal":true,
        "status":"interrupted","slim":false,"content":"","thinking":"checkpoint",
        "projection":{"content":"","thinking":"checkpoint","toolRounds":[],"segments":[]},
        "settlement":{"outcome":"interrupted","cause":"user_abort","resumeOptions":[
            {"operation":"continue","anchor":{"type":"replay_only","contentChars":0}}]},
        "error":{},"event_type":"aborted","event_payload":{},"now":1000}),true);
    let current=invoke(database,"turn.get",json!({"conversation_id":"conversation","turn_id":turn,"user_id":7}),false);
    let target=invoke(database,"turn.attempt.create",json!({"conversation_id":"conversation","turn_id":turn,"user_id":7,
        "command_id":"continue","operation":"continue","expected_projection_revision":current["projectionRevision"],
        "config":{"_resumeSource":{"taskId":"forged"}}}),true);
    let target_id=target["attempt"]["attemptId"].as_str().unwrap();bound(database,target_id,"target");
    ResumeLineage { conversation_id:"conversation".into(),turn_id:turn.into(),source_attempt_id:source.into(),
        target_attempt_id:target_id.into(),source_task_id:"task".into(),reserved_micro:0 }
}
fn scope(task:&str)->Scope { Scope {owner:7,task_id:task.into(),epoch:"epoch".into(),binding:"binding".into()} }
fn state()->Value {json!({"format":"tofu.native-agent-checkpoint/v1","owner_user_id":7,"task_id":"task",
    "epoch":"epoch","binding":"binding","next_round":1,"tool_calls":1,"messages":[{"role":"user","content":"question"},
    {"role":"tool","content":"x".repeat(32768)}],"usage":[{}],"round_limit":3,"tool_limit":3,"no_progress_limit":0,"failure_limit":0,
    "progress":{"codec":"sha256-token-sequence/v1","last_calls":null,"last_world":"","last_evidence":null,"last_failure":null,
        "no_progress_streak":0,"failure_streak":0}})}
fn store(database:AuthorityDatabase)->NativeCheckpointStore {
    NativeCheckpointStore::new(Arc::new(Mutex::new(database)),3,7,4096,1048576).unwrap()
}
fn seed(source:&NativeCheckpointStore) {
    let journal=Journal::new(scope("task"),source,1048576,1024).unwrap();let lease=journal.acquire("source").unwrap();
    lease.load().unwrap();lease.begin(0).unwrap();lease.complete(&state()).unwrap();lease.release().unwrap();
}
#[test]
fn handoff_composite_replays_atomically_and_cannot_promote_the_passive_target() {
    let directory=tempfile::tempdir().unwrap();let mirror=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();let lineage=accepted(&mut database);
    database.configure_logical_outbox(&[72;32],4*1024*1024,128*1024).unwrap();let source=store(database);seed(&source);
    let journal=Journal::new(scope("target"),&source,1048576,1024).unwrap();let lease=journal.handoff("target",&lineage).unwrap();
    let expected=lease.load().unwrap().unwrap();assert_eq!(expected["task_id"],"target");
    let snapshot_delta=serde_json::to_vec(&expected).unwrap().len() as i64-serde_json::to_vec(&state()).unwrap().len() as i64;
    assert_eq!(snapshot_delta,2); // Exact difference of these explicit task ID lengths.
    let database=source.authority.lock().unwrap();let origin=database.authority_uuid().to_string();
    let pending=database.logical_outbox_pending(3,7,8).unwrap();drop(database);assert_eq!(pending.len(),5);
    let transfer=serde_json::from_slice::<Value>(&LogicalOutboxCipher::new(&[72;32]).open(&pending[4].record).unwrap()).unwrap();
    let composite_bytes=serde_json::to_vec(&transfer["handoff_source"]).unwrap().len();
    println!("handoff_growth snapshot_delta_bytes={snapshot_delta} retired_source_outbox_bytes={composite_bytes} new_workers=0");
    let target=store(AuthorityDatabase::initialize(mirror.path()).unwrap());let cipher=LogicalOutboxCipher::new(&[72;32]);
    for frame in &pending {target.replay(&cipher,&origin,&frame.record).unwrap();}
    let source_header=target.header(7,"task").unwrap().unwrap();assert_eq!(source_header.value["phase"],"terminal");
    assert!(target.snapshot(7,"task",source_header.version).unwrap().is_none());
    let header=target.header(7,"target").unwrap().unwrap();assert_eq!(header.version,1);
    assert_eq!(target.snapshot(7,"target",1).unwrap(),Some(expected));
    assert_eq!(target.replay(&cipher,&origin,&pending[4].record),Ok(1));
    assert_eq!(target.writer_quiescent(7,"target",&header.value["writer_process"]),Ok(false));
    assert_eq!(Journal::new(scope("target"),&target,1048576,1024).unwrap().recover("stolen").err(),Some(Error::Ineligible));
    let source_header=source.header(7,"task").unwrap().unwrap();assert_eq!(source_header.value["phase"],"terminal");
    assert!(source.snapshot(7,"task",source_header.version).unwrap().is_none());
    // A lost source ACK can redeliver its older release after the composite
    // retirement. Only exact immutable archive proof may acknowledge that prefix.
    use crate::native_checkpoint_store::NativeCheckpointSink;
    use crate::outbox_publisher::LogicalOutboxSink;
    use crate::outbox_sink::EngineLogicalOutboxSink;
    let mirror=tempfile::tempdir().unwrap();let archive=tempfile::tempdir().unwrap();
    let mirror=store(AuthorityDatabase::initialize(mirror.path()).unwrap());
    let archive=EngineLogicalOutboxSink::initialize(archive.path(),3,7,1048576).unwrap();
    let mut sink=NativeCheckpointSink::new(mirror,archive,LogicalOutboxCipher::new(&[72;32]),origin).unwrap();
    for frame in &pending {sink.append_durable(&frame.record).unwrap();}
    sink.append_durable(&pending[3].record).unwrap();
    let (mirror,_)=sink.into_parts();let retired=mirror.header(7,"task").unwrap().unwrap();
    assert_eq!(retired.version,5);assert_eq!(retired.value["phase"],"terminal");
    assert_eq!(mirror.header(7,"target").unwrap().unwrap().version,1);
}
#[test]
fn forged_accepted_relation_refuses_without_mutating_either_journal() {
    for fault in ["source_attempt","target_attempt","conversation","turn","source_task","superseded"] {
        let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut lineage=accepted(&mut database);
        if fault=="superseded" {
            invoke(&mut database,"turn.event.record",json!({"attempt_id":lineage.target_attempt_id,"task_id":"target",
                "user_id":7,"terminal":true,"status":"interrupted","projection":{"content":"","thinking":"checkpoint"},
                "settlement":{"outcome":"interrupted","cause":"user_abort","resumeOptions":[
                    {"operation":"continue","anchor":{"type":"replay_only","contentChars":0}}]},"now":1000}),true);
            let current=invoke(&mut database,"turn.get",json!({"conversation_id":"conversation","turn_id":lineage.turn_id,"user_id":7}),false);
            invoke(&mut database,"turn.attempt.create",json!({"conversation_id":"conversation","turn_id":lineage.turn_id,"user_id":7,
                "command_id":"supersede","operation":"continue","expected_projection_revision":current["projectionRevision"]}),true);
        }
        let source=store(database);seed(&source);
        match fault {"source_attempt"=>lineage.source_attempt_id="foreign".into(),"target_attempt"=>lineage.target_attempt_id="foreign".into(),
            "conversation"=>lineage.conversation_id="foreign".into(),"turn"=>lineage.turn_id="foreign".into(),
            "source_task"=>lineage.source_task_id="foreign".into(),_=>{}};
        let before=source.header(7,"task").unwrap().unwrap();
        assert!(Journal::new(scope("target"),&source,1048576,1024).unwrap().handoff("new",&lineage).is_err(),"{fault}");
        let after=source.header(7,"task").unwrap().unwrap();assert_eq!(after.version,before.version);assert_eq!(after.value,before.value);
        assert_eq!(source.snapshot(7,"task",after.version).unwrap(),Some(state()));assert!(source.header(7,"target").unwrap().is_none());
    }
}

#[test]
fn handoff_write_and_sync_faults_never_reopen_a_split_source_target() {
    use crate::vfs::{DeterministicVfs,FaultAction,Operation,Vfs};
    for (operation,action,may_succeed) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other),false),
        (Operation::Write,FaultAction::ShortWrite(7),true),
        (Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other),false)] {
            let vfs=Arc::new(DeterministicVfs::new(None));
            let path=std::path::Path::new("/handoff");vfs.create_dir(path).unwrap();
            vfs.sync_directory(std::path::Path::new("/")).unwrap();
            let mut database=AuthorityDatabase::initialize_with_vfs(path,vfs.clone() as Arc<dyn Vfs>).unwrap();
            let lineage=accepted(&mut database);let source=store(database);seed(&source);
            vfs.arm_operation_fault(operation,1,action).unwrap();
            let journal=Journal::new(scope("target"),&source,1048576,1024).unwrap();
            let accepted=journal.handoff("new",&lineage).is_ok();
            if !may_succeed {assert!(!accepted);}
            drop(journal);drop(source);
            let recovered=store(AuthorityDatabase::open_with_vfs(path,vfs.clone() as Arc<dyn Vfs>).unwrap());
            let source=recovered.header(7,"task").unwrap().unwrap();
            let target=recovered.header(7,"target").unwrap();
            match target {
                None=>{assert!(!accepted);assert_eq!(source.value["phase"],"completed");
                    assert_eq!(recovered.snapshot(7,"task",source.version).unwrap(),Some(state()));},
                Some(target)=>{assert_eq!(source.value["phase"],"terminal");assert_eq!(target.value["phase"],"completed");
                    assert!(recovered.snapshot(7,"task",source.version).unwrap().is_none());
                    let snapshot=recovered.snapshot(7,"target",target.version).unwrap().unwrap();
                    assert_eq!(snapshot["task_id"],"target");assert_eq!(snapshot["messages"],state()["messages"]);},
            }
    }
}

fn attach_wallet_invoice(pending: &mut Value) {
        pending["shared_model_account"]=json!({"format":"tofu.native-shared-account/v1","owner_user_id":7,
            "root_task_id":"task","tokens":5,"tool_calls":0,"control_tool_calls":0,"model_ordinal":1,
            "tool_ordinal":0,"complete":true,"limits":{"max_tokens":100,"max_tool_calls":3,"deadline":null},
            "counter_policy":{"encoding":"cl100k_base","output_field":"max_tokens","wire_model":"alpha"}});
        pending["host_fee_projection"]=json!({"format":"tofu.native-fee-checkpoint/v1","owner_user_id":7,"tenant_id":"",
            "task_id":"task","logical_model":"alpha","provider_id":"provider","wallet_user_id":"wallet","reserved_micro":10,
            "rounds":[{"round":1,"cost":{"price":{"prompt_tokens":3},"amounts":{"outputTokens":2},
                "credits":{"input":1,"output":2,"cache_read":0,"cache_write":0,"base":3,"margin":0,"total":3}}}],
            "wallet_settlement":{"format":"tofu.native-billing/v1","task_id":"task","owner_user_id":7,"wallet_user_id":"wallet",
                "model":"alpha","reserved_micro":10,"roundCount":1,"componentsMicro":{"input":1,"output":2,"cache_read":0,
                    "cache_write":0,"baseMicro":3,"marginMicro":0,"totalMicro":3}}});
}

#[test]
#[cfg(target_os="linux")]
fn pending_handoff_rechecks_dead_writer_and_replays_without_promoting_passive_state() {
    for wallet in [false, true] {
    struct Writer(std::process::Child);
    impl Drop for Writer { fn drop(&mut self) { let _=self.0.kill();let _=self.0.wait(); } }
    let mut writer=Writer(std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap());
    let mut identity=crate::native_checkpoint_store::writer::capture().unwrap();
    let raw=std::fs::read_to_string(format!("/proc/{}/stat",writer.0.id())).unwrap();
    identity["pid"]=json!(writer.0.id());
    identity["start_ticks"]=json!(raw.rsplit_once(')').unwrap().1.split_whitespace().nth(19).unwrap().parse::<u64>().unwrap());
    let directory=tempfile::tempdir().unwrap();let mirror=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();let lineage=accepted(&mut database);
    database.configure_logical_outbox(&[72;32],4*1024*1024,128*1024).unwrap();
    let mut source=store(database);source.writer_process=Some(identity);
    let mut pending=state();pending["tools_pending"]=json!(true);
    pending["messages"]=json!([{"role":"user","content":"question"},{"role":"assistant","content":"paid response","tool_calls":[
        {"id":"file","type":"function","function":{"name":"write_file","arguments":"{}"}}]}]);
    pending["tool_recovery"]=json!({"producer_task_id":"task","operation":"exact-operation"});
    if wallet { attach_wallet_invoice(&mut pending); }
    {
        let journal=Journal::new(scope("task"),&source,1048576,1024).unwrap();let lease=journal.acquire("source").unwrap();
        lease.load().unwrap();lease.begin(0).unwrap();lease.pending(&pending).unwrap();
    }
    source.writer_process=crate::native_checkpoint_store::writer::capture();
    let header=source.header(7,"task").unwrap().unwrap();let mut retired=header.value.clone();
    retired["phase"]=json!("terminal");retired["lease"]=Value::Null;
    let mut target=header.value.clone();target["task_id"]=json!("target");target["lease"]=json!("target");target["lease_generation"]=json!(1);
    target["writer_process"]=source.writer_process.clone().unwrap();
    let mut transferred=pending.clone();transferred["task_id"]=json!("target");
    // Bypass the Journal precheck: the storage transaction must independently
    // reject an accepted lineage while the original writer can still execute.
    assert_eq!(source.handoff(Handoff {lineage:&lineage,source_version:header.version,
        source_header:&retired,target_header:&target,target_snapshot:&transferred}),Err(Error::Ineligible));
    assert!(source.header(7,"target").unwrap().is_none());
    writer.0.kill().unwrap();writer.0.wait().unwrap();
    let journal=Journal::new(scope("target"),&source,1048576,1024).unwrap();let lease=journal.handoff("new",&lineage).unwrap();
    let expected=lease.load().unwrap().unwrap();assert_eq!(expected["tools_pending"],true);
    assert_eq!(expected["tool_recovery"],pending["tool_recovery"]);
    let database=source.authority.lock().unwrap();let origin=database.authority_uuid().to_string();
    let frames=database.logical_outbox_pending(3,7,8).unwrap();drop(database);assert_eq!(frames.len(),4);
    let target=store(AuthorityDatabase::initialize(mirror.path()).unwrap());let cipher=LogicalOutboxCipher::new(&[72;32]);
    for frame in &frames {target.replay(&cipher,&origin,&frame.record).unwrap();}
    assert_eq!(target.replay(&cipher,&origin,&frames[3].record),Ok(1));
    assert_eq!(target.snapshot(7,"target",1).unwrap(),Some(expected));
    let header=target.header(7,"task").unwrap().unwrap();assert_eq!(header.value["phase"],"terminal");
    assert!(target.snapshot(7,"task",header.version).unwrap().is_none());
    assert_eq!(target.wallet_invoice(7,"task"),Err(Error::Ineligible));
    let invoice=source.wallet_invoice(7,"task").unwrap().unwrap();
    assert_eq!(invoice["wallet_settlement"],pending["host_fee_projection"]["wallet_settlement"]);
    assert_eq!(header.value.get("wallet_settlement"),pending["host_fee_projection"].get("wallet_settlement"));
    if wallet { assert_eq!(invoice["wallet_settlement"]["componentsMicro"]["totalMicro"],3); }
    assert_eq!(Journal::new(scope("target"),&target,1048576,1024).unwrap().recover("stolen").err(),Some(Error::Ineligible));
}
}

#[test]
#[cfg(target_os="linux")]
fn pending_wallet_invoice_and_target_remain_atomic_across_write_and_sync_faults() {
    use crate::vfs::{DeterministicVfs,FaultAction,Operation,Vfs};
    for (operation,action) in [(Operation::Write,FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Operation::Write,FaultAction::ShortWrite(7)),
        (Operation::SyncData,FaultAction::ErrorBefore(io::ErrorKind::Other))] {
        let vfs=Arc::new(DeterministicVfs::new(None));let path=std::path::Path::new("/wallet-handoff");
        vfs.create_dir(path).unwrap();vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database=AuthorityDatabase::initialize_with_vfs(path,vfs.clone() as Arc<dyn Vfs>).unwrap();
        let lineage=accepted(&mut database);let mut source=store(database);
        struct Child(std::process::Child);
        impl Drop for Child {fn drop(&mut self){let _=self.0.kill();let _=self.0.wait();}}
        let mut writer=Child(std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap());
        let mut identity=crate::native_checkpoint_store::writer::capture().unwrap();
        let stat=std::fs::read_to_string(format!("/proc/{}/stat",writer.0.id())).unwrap();
        identity["pid"]=json!(writer.0.id());identity["start_ticks"]=json!(stat.rsplit_once(')').unwrap().1
            .split_whitespace().nth(19).unwrap().parse::<u64>().unwrap());
        source.writer_process=Some(identity);
        let mut pending=state();pending["tools_pending"]=json!(true);attach_wallet_invoice(&mut pending);
        pending["messages"]=json!([{"role":"user","content":"question"},{"role":"assistant","tool_calls":[
            {"id":"file","type":"function","function":{"name":"write_file","arguments":"{}"}}]}]);
        {
            let journal=Journal::new(scope("task"),&source,1048576,1024).unwrap();let lease=journal.acquire("source").unwrap();
            lease.load().unwrap();lease.begin(0).unwrap();lease.pending(&pending).unwrap();
        }
        writer.0.kill().unwrap();writer.0.wait().unwrap();source.writer_process=crate::native_checkpoint_store::writer::capture();
        vfs.arm_operation_fault(operation,1,action).unwrap();
        let acknowledged=Journal::new(scope("target"),&source,1048576,1024).unwrap().handoff("new",&lineage).is_ok();
        drop(source);
        let reopened=store(AuthorityDatabase::open_with_vfs(path,vfs.clone() as Arc<dyn Vfs>).unwrap());
        let retired=reopened.header(7,"task").unwrap().unwrap();
        match reopened.header(7,"target").unwrap() {
            None=>{
                assert!(!acknowledged);assert_eq!(retired.value["phase"],"tools_pending");
                assert!(retired.value.get("wallet_settlement").is_none());
                assert_eq!(reopened.snapshot(7,"task",retired.version).unwrap(),Some(pending));
            },
            Some(target)=>{
                assert_eq!(retired.value["phase"],"terminal");assert_eq!(target.value["phase"],"tools_pending");
                assert_eq!(retired.value["wallet_settlement"],pending["host_fee_projection"]["wallet_settlement"]);
                assert!(reopened.snapshot(7,"task",retired.version).unwrap().is_none());
                let next=reopened.snapshot(7,"target",target.version).unwrap().unwrap();
                assert_eq!(next["host_fee_projection"]["wallet_settlement"]["componentsMicro"]["totalMicro"],0);
                assert_eq!(reopened.wallet_invoice(7,"task").unwrap().unwrap()["wallet_settlement"],retired.value["wallet_settlement"]);
            },
        }
    }
}
