//! Explicit frame versus existing per-step execution: exact logical state,
//! replay, receipt/outbox atomicity, real reopen and actual work attribution.
use super::*;
use crate::generated_storage_operations::StorageOperationKind;
use crate::transaction_ir::{TransactionIr, TransactionIrCommandEffects, TransactionIrResult,
    IndexedStreamAppendItem, prepare_execution_frame_ir_with_deadline, prepare_transaction_ir_with_deadline};
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

const CONVERSATION: &str = "execution-frame-conversation";
const ATTEMPT: &str = "execution-frame-attempt";
const TASK: &str = "execution-frame-task";
const TURN: &str = "execution-frame-turn";

fn seed(database: &mut AuthorityDatabase, bytes: usize) -> u64 {
    seed_with_evidence(database, "s".repeat(bytes))
}

fn seed_with_evidence(database: &mut AuthorityDatabase, evidence: String) -> u64 {
    let projection = json!({"content":"before", "thinking":"", "largeStableEvidence":evidence});
    seed_projection(database, projection, true)
}

fn seed_projection(database: &mut AuthorityDatabase, projection: Value, runtime: bool) -> u64 {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(database, &mut tx, &AppendSettledRequest {
        conversation_id: CONVERSATION.into(), actor:"assistant".into(), status:"pending".into(),
        projection_json: serde_json::to_vec(&projection).unwrap(), projection_timestamp:None,
        settlement_json:b"{}".to_vec(), lane_id:"main".into(), command_id:"seed-frame".into(),
        kind:"reply".into(), run_id:String::new(), turn_id:TURN.into(), attempt_id:Some(ATTEMPT.into()),
        created_at_ms:1000, committed_at_ms:1000, defaults:TurnDefaults { allow_create:true,
            title:"Frame".into(), settings_json:b"{}".to_vec(), created_at_ms:1000 },
    }).unwrap();
    database.commit(tx).unwrap();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    attempt_bind(database, &mut tx, ATTEMPT, TASK, "", 1100).unwrap();
    database.commit(tx).unwrap();
    let mut first = request(0, 0);
    first.projection_patch = None;
    first.projection = projection;
    first.task_event = None;
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let response: Value = serde_json::from_slice(&event_record_mode(database, &mut tx, &first, runtime).unwrap()).unwrap();
    database.commit(tx).unwrap();
    response["projection_revision"].as_u64().unwrap()
}

fn request(revision: u64, index: usize) -> EventRecordRequest {
    EventRecordRequest { model_continuation:Value::Null, attempt_id:ATTEMPT.into(), task_id:TASK.into(),
        terminal:false, status:"running".into(), slim:false, content:String::new(), thinking:String::new(),
        projection:Value::Null, projection_patch:Some(json!({"version":1, "baseRevision":revision,
            "targetRevision":revision+1, "operations":[
                {"op":"append_text", "path":["content"], "value":format!("{}汉字😀\n\\\"", "delta".repeat(24))},
                {"op":"set", "path":["phase"], "value":{"ordinal":index,"name":"working"}}
            ]}).as_object().unwrap().clone()), projection_segments_stable:false,
        settlement:Map::new(), error:Map::new(), event_type:"projection_updated".into(),
        event_payload:Map::from_iter([("phase".into(), json!(index))]), background_command_ack:None,
        task_event:Some(IndexedStreamAppendItem { task_id:TASK.into(), application_sequence:index as u64,
            event_type:"phase".into(), payload_json:serde_json::to_vec(&json!({"type":"phase","ordinal":index})).unwrap(),
            created_at_ms:2000+index as i64, require_attempts_live:Vec::new() }),
        preceding_task_events:Vec::new(), flow_root_attempt_id:String::new(), now_ms:2000+index as u64 }
}

fn ir(revision: u64, count: usize, receipts: bool) -> TransactionIr {
    let requests: Vec<_> = (0..count).map(|index| request(revision+index as u64,index)).collect();
    let payload = serde_json::to_vec(&json!({"format":"tofu.experimental-execution-frame/v1",
        "user_id":11,"attempt_id":ATTEMPT,"task_id":TASK,
        "events":requests.iter().map(|r|json!({"patch":r.projection_patch,"event":r.event_payload,
            "now":r.now_ms,"raw":r.task_event.as_ref().map(|e|&e.payload_json)})).collect::<Vec<_>>() })).unwrap();
    TransactionIr { tenant_id:7, owner_user_id:11, operation:"turn.event.record".into(),
        operation_kind:StorageOperationKind::Command,
        steps:requests.into_iter().enumerate().map(|(slot,request)|TransactionIrStep::TurnEventRecord {request,slot}).collect(),
        result:TransactionIrResult::SlotOrLiteral {slot:count-1,missing:b"null".to_vec()},
        command_effects:Some(TransactionIrCommandEffects { legacy_receipt_owner_bound:true,
            command_id:"atomic-frame".into(), request_digest:Sha256::digest(&payload).into(), alternate_request_digest:None,
            request_id:"atomic-frame-request".into(), schema_version:1, registry_version:1,
            committed_at_ms:2000+count as u64, outbox_payload:payload, store_receipt:receipts }) }
}

fn execute(database: &mut AuthorityDatabase, ir: &TransactionIr, frame: bool) -> io::Result<Vec<u8>> {
    let prepared = if frame { prepare_execution_frame_ir_with_deadline(database,ir,Duration::MAX)? }
        else { prepare_transaction_ir_with_deadline(database,ir,Duration::MAX)? };
    if let Some(tx) = prepared.transaction { database.commit(tx)?; }
    Ok(prepared.response.into_bytes())
}

fn state(database: &AuthorityDatabase) -> Value {
    let mut tx = database.begin_with_identity_claim_scopes(7,11).unwrap();
    let parse = |bytes:Vec<u8>|serde_json::from_slice::<Value>(&bytes).unwrap();
    let turn = parse(get(database,&mut tx,CONVERSATION,TURN).unwrap().unwrap());
    let attempt = parse(attempt_get(database,&mut tx,ATTEMPT).unwrap().unwrap());
    let events = parse(events_list(database,&mut tx,ATTEMPT,0,1000,true,&mut ||false).unwrap().unwrap());
    let hydrated = parse(events_list(database,&mut tx,ATTEMPT,0,1000,false,&mut ||false).unwrap().unwrap());
    let sync = parse(sync_changes(database,&mut tx,CONVERSATION,0,1000,&mut ||false).unwrap().unwrap());
    let header = parse(crate::conversation_header::get(database,&mut tx,CONVERSATION,false,0,None,&mut ||false).unwrap().unwrap());
    let raw = parse(crate::indexed_stream::list(database,&mut tx,&crate::indexed_stream::ListRequest {
        task_id:TASK.into(), after_sequence:None, limit:1000, types:Vec::new(),type_prefixes:Vec::new(),payload_types:None }).unwrap());
    json!({"turn":turn,"attempt":attempt,"events":events,"hydrated":hydrated,"sync":sync,"header":header,"raw":raw})
}

#[test]
fn execution_frame_exact_slots_replay_header_and_reopen_match_each_step() {
    for (bytes, text_only) in [(0,false),(128*1024,false),(128*1024,true)] {
      for count in [1,8,64] {
        let a=tempfile::tempdir().unwrap();let b=tempfile::tempdir().unwrap();
        let mut baseline=AuthorityDatabase::initialize(a.path()).unwrap();
        let mut candidate=AuthorityDatabase::initialize(b.path()).unwrap();
        let revision=seed(&mut baseline,bytes);assert_eq!(seed(&mut candidate,bytes),revision);
        let mut command=ir(revision,count,false);
        if text_only {
            for step in &mut command.steps {
                if let TransactionIrStep::TurnEventRecord{request,..}=step {
                    request.projection_patch.as_mut().unwrap()["operations"].as_array_mut().unwrap().truncate(1);
                }
            }
        }
        // Non-monotonic logical clocks retain the final input, not max(now).
        if count>1 { if let TransactionIrStep::TurnEventRecord{request,..}=&mut command.steps[count-1] {request.now_ms=1999;} }
        command.validate().unwrap();
        let mut original=baseline.begin_with_identity_claim_scopes(7,11).unwrap();
        let expected:Vec<_>=command.steps.iter().map(|step|match step {
            TransactionIrStep::TurnEventRecord{request,slot}=>(*slot,event_record(&baseline,&mut original,request).unwrap()),
            _=>unreachable!(), }).collect();
        baseline.commit(original).unwrap();
        let mut folded=candidate.begin_with_identity_claim_scopes(7,11).unwrap();
        assert_eq!(record(&candidate,&mut folded,&command.steps,&mut ||false).unwrap(),expected);
        candidate.commit(folded).unwrap();
        let expected=state(&baseline);assert_eq!(state(&candidate),expected,"count={count}");
        drop(candidate);let reopened=AuthorityDatabase::open(b.path()).unwrap();
        assert_eq!(state(&reopened),expected,"reopen count={count}, bytes={bytes}, text_only={text_only}");
      }
    }
}

#[test]
fn execution_frame_error_abandons_prefix_and_forbids_effect_boundaries() {
    let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision=seed(&mut database,16*1024);let before=state(&database);
    for kind in ["revision","terminal","tool_structure","owner","deadline","mixed_attempt","later_read"] {
        let mut command=ir(revision,3,false);
        if let TransactionIrStep::TurnEventRecord{request,..}=&mut command.steps[1] {
            match kind {
                "revision"=>{request.projection_patch.as_mut().unwrap().insert("baseRevision".into(),json!(0));},
                "terminal"=>{request.terminal=true;request.status="completed".into();},
                "tool_structure"=>{request.projection_patch.as_mut().unwrap()["operations"]=json!([{"op":"set","path":["toolRounds"],"value":[]}]);},
                "mixed_attempt"=>request.attempt_id="different-attempt".into(),
                _=>{},
            }
        }
        if kind=="owner" {command.owner_user_id=12;}
        if kind=="later_read" {command.steps.push(TransactionIrStep::TurnAttemptGet{attempt_id:ATTEMPT.into(),slot:3,include_execution_config:false});}
        let budget=if kind=="deadline" {Duration::ZERO} else {Duration::MAX};
        let result=prepare_execution_frame_ir_with_deadline(&mut database,&command,budget);
        if kind=="owner" {
            let prepared=result.unwrap();assert!(prepared.transaction.is_none());
            assert_eq!(serde_json::from_slice::<Value>(&prepared.response).unwrap()["applied"],false);
        } else {assert!(result.is_err(),"{kind}");}
        assert_eq!(state(&database),before,"failed {kind} changed state");
        assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes,0);
    }
}

fn seeded_vfs()->(Arc<DeterministicVfs>,AuthorityDatabase,u64) {
    let vfs=Arc::new(DeterministicVfs::new(None));vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database=AuthorityDatabase::initialize_with_vfs(Path::new("/data"),vfs.clone()).unwrap();
    let revision=seed(&mut database,4096);database.configure_logical_outbox(&[72;32],1024*1024,128*1024).unwrap();
    vfs.arm_fault(None).unwrap();(vfs,database,revision)
}

#[test]
fn execution_frame_receipt_retry_and_every_commit_fault_keep_atomic_state() {
    let (vfs,mut baseline,revision)=seeded_vfs();let before=state(&baseline);vfs.arm_fault(None).unwrap();
    let command=ir(revision,3,true);let expected_response=execute(&mut baseline,&command,true).unwrap();
    let trace=vfs.trace().unwrap();let after=state(&baseline);
    let bytes=baseline.wal_len().unwrap();assert_eq!(execute(&mut baseline,&command,true).unwrap(),expected_response);
    assert_eq!(baseline.wal_len().unwrap(),bytes,"receipt retry appended a frame");
    assert_eq!(baseline.logical_outbox_pending(7,11,10).unwrap().len(),1);
    drop(baseline);
    for (index,operation) in trace.iter().enumerate() {
        if !matches!(operation,Operation::Write|Operation::SyncData|Operation::SyncAll|Operation::SyncDirectory) {continue;}
        for action in [FaultAction::ErrorBefore(io::ErrorKind::Other),FaultAction::ShortWrite(7),FaultAction::DropSync] {
            if !matches!(operation,Operation::Write) && matches!(action,FaultAction::ShortWrite(_)) {continue;}
            if matches!(operation,Operation::Write) && matches!(action,FaultAction::DropSync) {continue;}
            let (vfs,mut database,_)=seeded_vfs();
            let occurrence=trace[..=index].iter().filter(|observed|*observed==operation).count();
            vfs.arm_operation_fault(*operation,occurrence,action).unwrap();
            let result=execute(&mut database,&command,true);
            assert!(vfs.operation_fault_fired().unwrap(),"fault {index} {operation:?} must fire");
            // Cut the durable image before Engine::Drop can catch CONTROL up
            // or drain buffered writes. Cleanup cannot strengthen the proof.
            let crash_image=Arc::new(vfs.fork_durable().unwrap());
            drop(database);
            let mut reopened=AuthorityDatabase::open_with_vfs(Path::new("/data"),crash_image).unwrap();
            let actual=state(&reopened);
            assert!(actual==before || actual==after,"partial frame at {index} {operation:?}");
            if result.is_ok() {assert_eq!(actual,after,"acknowledged frame lost");}
            reopened.configure_logical_outbox(&[72;32],1024*1024,128*1024).unwrap();
            let pending=reopened.logical_outbox_pending(7,11,10).unwrap();
            assert_eq!(pending.len(),usize::from(actual==after),"outbox detached from frame");
            assert_eq!(execute(&mut reopened,&command,true).unwrap(),expected_response);
            assert_eq!(state(&reopened),after);
            assert_eq!(reopened.logical_outbox_pending(7,11,10).unwrap().len(),1);
        }
    }
}

#[test]
fn execution_frame_competing_preparations_keep_only_the_committed_replay() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed(&mut database, 128 * 1024);
    let command = ir(revision, 8, false);
    let prepared = prepare_execution_frame_ir_with_deadline(&mut database, &command, Duration::MAX).unwrap();
    // Another ordinary request wins after frame preparation. The original
    // owner/current/Turn/header/sequence reads remain OCC witnesses.
    let mut competing = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    event_record(&database, &mut competing, &request(revision, 0)).unwrap();
    database.commit(competing).unwrap();
    let committed = state(&database);
    let error = database.commit(prepared.transaction.unwrap()).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    assert_eq!(state(&database), committed);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    drop(database);
    assert_eq!(state(&AuthorityDatabase::open(directory.path()).unwrap()), committed);
}

#[test]
fn execution_frame_size_witness_matches_encoding_and_abandons_invalid_delta() {
    let mut projection = json!({"content":"start\n汉字😀", "thinking":"", "phase":null})
        .as_object().unwrap().clone();
    let mut witness = ProjectionBytes::new(&projection).unwrap();
    for value in ["", "plain", "汉字😀", "\n\r\t\0\\\""] {
        for operation in ["set", "append_text"] {
            let patch = json!({"version":1,"operations":[{"op":operation,"path":["content"],"value":value}]})
                .as_object().unwrap().clone();
            witness.apply(&patch).unwrap();
            projection = crate::turn_projection_patch::apply_projection_patch_owned(projection,&patch).unwrap();
            assert_eq!(witness.total,serde_json::to_vec(&projection).unwrap().len());
        }
    }
    let directory=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision=seed(&mut database,0);let before=state(&database);
    let mut command=ir(revision,3,false);
    if let TransactionIrStep::TurnEventRecord{request,..}=&mut command.steps[1] {
        request.projection_patch.as_mut().unwrap()["operations"]=json!([
            {"op":"set","path":["content"],"value":7},
            {"op":"append_text","path":["content"],"value":"invalid"}]);
    }
    assert!(prepare_execution_frame_ir_with_deadline(&mut database,&command,Duration::MAX).is_err());
    assert_eq!(state(&database),before);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes,0);
}

#[test]
fn execution_frame_rejects_admitted_guidance_that_ordinary_step_repairs() {
    let directory=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision=seed(&mut database,128*1024);
    // This state is accepted by the ordinary API: preservation sees the old
    // empty tool history, so a newly supplied receipt is not normalized yet.
    let mut insert=request(revision,0);
    insert.task_event=None;
    insert.projection_patch.as_mut().unwrap()["operations"]=json!([
        {"op":"set","path":["toolRounds"],"value":[{"guidanceId":"g",
            "guidanceAnswerCommandId":"r","guidanceResponse":"answer","status":"running"}]}]);
    let mut tx=database.begin_with_identity_claim_scopes(7,11).unwrap();
    event_record(&database,&mut tx,&insert).unwrap();database.commit(tx).unwrap();
    let admitted=state(&database);
    assert_eq!(admitted["turn"]["projection"]["toolRounds"][0]["status"],"running");
    let command=ir(revision+1,3,false);
    let error=prepare_execution_frame_ir_with_deadline(&mut database,&command,Duration::MAX)
        .err().expect("a text-only frame must refuse required guidance repair");
    assert_eq!(error.kind(),io::ErrorKind::InvalidInput);
    assert_eq!(state(&database),admitted);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes,0);
    execute(&mut database,&command,false).unwrap();
    let ordinary=state(&database);
    assert_eq!(ordinary["turn"]["projection"]["toolRounds"][0]["status"],"completed");
    require_guidance_repair_free_projection(ordinary["turn"]["projection"].as_object().unwrap()).unwrap();
}

#[test]
#[cfg(target_os = "linux")]
fn execution_frame_sigkill_child() {
    let Ok(path)=std::env::var("TOFU_EXECUTION_FRAME_TEST_DIRECTORY") else {return;};
    let path=Path::new(&path);
    let mut database=AuthorityDatabase::open(path).unwrap();
    database.configure_logical_outbox(&[72;32],1024*1024,128*1024).unwrap();
    let revision=state(&database)["turn"]["projectionRevision"].as_u64().unwrap();
    let response=execute(&mut database,&ir(revision,8,true),true).unwrap();
    std::fs::write(path.join("frame-ack"),response).unwrap();
    loop {std::thread::park();}
}

#[test]
#[cfg(target_os = "linux")]
fn execution_frame_ack_and_receipt_survive_sigkill_without_drop() {
    struct Child(std::process::Child);
    impl Drop for Child {
        fn drop(&mut self) {let _=self.0.kill();let _=self.0.wait();}
    }
    let directory=tempfile::tempdir().unwrap();
    let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision=seed(&mut database,128*1024);drop(database);
    let mut child=Child(std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact","turn::execution_frame::tests::execution_frame_sigkill_child","--nocapture"])
        .env("TOFU_EXECUTION_FRAME_TEST_DIRECTORY",directory.path())
        .stdout(std::process::Stdio::null()).spawn().unwrap());
    let deadline=std::time::Instant::now()+Duration::from_secs(30);
    while !directory.path().join("frame-ack").exists() {
        assert!(child.0.try_wait().unwrap().is_none(),"child exited before durable ACK");
        assert!(std::time::Instant::now()<deadline,"child did not reach durable ACK");
        std::thread::sleep(Duration::from_millis(10));
    }
    child.0.kill().unwrap();assert!(!child.0.wait().unwrap().success());
    let mut reopened=AuthorityDatabase::open(directory.path()).unwrap();
    reopened.configure_logical_outbox(&[72;32],1024*1024,128*1024).unwrap();
    let actual=state(&reopened);
    assert_eq!(actual["turn"]["projectionRevision"],revision+8);
    assert_eq!(actual["raw"].as_array().unwrap().len(),8);
    let wal=reopened.wal_len().unwrap();
    assert_eq!(execute(&mut reopened,&ir(revision,8,true),true).unwrap(),
        std::fs::read(directory.path().join("frame-ack")).unwrap());
    assert_eq!(reopened.wal_len().unwrap(),wal);
    assert_eq!(state(&reopened),actual);
    assert_eq!(reopened.logical_outbox_pending(7,11,10).unwrap().len(),1);
}

#[test]
#[ignore = "explicit profile-on work-count control, not a wall-time performance gate"]
fn execution_frame_work_counts() {
    assert!(crate::request_profile::enabled(),"set TOFU_DB_REQUEST_PROFILE_EVERY=1");
    for count in [1,8,64] {
        let mut expected=None;
        for frame in [false,true] {
            let directory=tempfile::tempdir().unwrap();let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
            let revision=seed(&mut database,256*1024);let command=ir(revision,count,false);
            let before=crate::request_profile::snapshot();let wal_before=database.wal_len().unwrap();
            let mut guard=crate::request_profile::RequestGuard::start("turn.event.record").unwrap();
            execute(&mut database,&command,frame).unwrap();guard.finish("ok");drop(guard);
            let after=crate::request_profile::snapshot();
            let aggregate=|value:Value|value["operations"].as_array().unwrap().iter()
                .find(|value|value["operation"]=="turn.event.record").cloned().unwrap_or(json!({}));
            let a=aggregate(before);let b=aggregate(after);let mut work=Map::new();
            for (name,value) in b["work"].as_object().unwrap() {
                work.insert(name.clone(),json!(value.as_u64().unwrap()-a["work"][name].as_u64().unwrap_or(0)));
            }
            let actual=state(&database);assert_eq!(actual["raw"].as_array().unwrap().len(),count);
            if let Some(expected)=&expected {assert_eq!(&actual,expected);} else {expected=Some(actual);}
            println!("FRAME_WORK {}",json!({"events":count,"folded":frame,"work":work,
                "wal_bytes":database.wal_len().unwrap()-wal_before}));
        }
    }
}

#[test]
#[cfg(target_os = "linux")]
#[ignore = "explicit release/profile-off single process timing leg; coordinate a quiet window"]
fn execution_frame_timing_leg() {
    assert!(!cfg!(debug_assertions),"use the release test binary");
    for variable in ["TOFU_DB_REQUEST_PROFILE_EVERY","TOFU_DB_RECOVERY_PROFILE"] {
        assert_eq!(std::env::var(variable).as_deref(),Ok("0"),"explicitly disable {variable}");
    }
    assert!(!cfg!(feature="recovery-profile"),"use a build without recovery profiling");
    assert!(!cfg!(feature="allocator-diagnostics"),"use a build without allocator diagnostics");
    assert!(!crate::request_profile::enabled(),"timing requires profile-off");
    let count:usize=std::env::var("TOFU_FRAME_EVENTS").unwrap().parse().unwrap();
    assert!([1,8,64].contains(&count));
    let frame=match std::env::var("TOFU_FRAME_FOLDED").unwrap().as_str() {
        "0"=>false,"1"=>true,_=>panic!("TOFU_FRAME_FOLDED must be 0 or 1"),
    };
    let fixture=std::env::var("TOFU_FRAME_FIXTURE").unwrap();
    let evidence=match fixture.as_str() {
        "compressible"=>"s".repeat(256*1024),
        "high_entropy"=>{
            const SYMBOLS:&[u8;64]=b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
            let mut evidence=String::with_capacity(256*1024);
            for counter in 0_u64..8192 {
                let mut digest=Sha256::new();
                digest.update(b"tofu.execution-frame.prefix.v1");
                digest.update(counter.to_le_bytes());
                for byte in digest.finalize() {evidence.push(SYMBOLS[usize::from(byte&63)] as char);}
            }
            evidence
        }
        _=>panic!("TOFU_FRAME_FIXTURE must be compressible or high_entropy"),
    };
    let prefix_sha256=format!("{:x}",Sha256::digest(evidence.as_bytes()));
    fn cpu_ns()->u128 {
        let mut time=libc::timespec{tv_sec:0,tv_nsec:0};
        assert_eq!(unsafe {libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID,&mut time)},0);
        time.tv_sec as u128*1_000_000_000+time.tv_nsec as u128
    }
    let mut samples=Vec::new();let mut signature=None;
    for _ in 0..12 {
        let directory=tempfile::tempdir().unwrap();
        let mut database=AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_wal_durability(crate::wal::WalDurability::Strict).unwrap();
        let revision=seed_with_evidence(&mut database,evidence.clone());let command=ir(revision,count,false);
        let wal_before=database.wal_len().unwrap();
        let cpu_before=cpu_ns();let started=std::time::Instant::now();
        let response=execute(&mut database,&command,frame).unwrap();
        let elapsed_ns=started.elapsed().as_nanos();let cpu_elapsed=cpu_ns()-cpu_before;
        let wal_bytes=database.wal_len().unwrap()-wal_before;
        // All fixture creation, encoding/oracles and reopen are outside the
        // measured prepare+strict-commit interval, equally in both arms.
        let actual=state(&database);assert_eq!(actual["raw"].as_array().unwrap().len(),count);
        drop(database);
        let reopened=AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(state(&reopened),actual);
        let current=json!({
            "state_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&actual).unwrap())),
            "response_sha256":format!("{:x}",Sha256::digest(response))});
        if let Some(expected)=&signature {assert_eq!(&current,expected);} else {signature=Some(current);}
        samples.push(json!({"elapsed_ns":elapsed_ns,"process_cpu_ns":cpu_elapsed,"wal_bytes":wal_bytes}));
    }
    println!("FRAME_TIMING {}",json!({"events":count,"folded":frame,"fixture":fixture,"prefix_sha256":prefix_sha256,"samples":samples,
        "signature":signature,"projection_stable_bytes":256*1024,"strict":true,
        "request_profile_every":0,"recovery_profile":0,
        "measured":"one IR prepare + one strict commit","reopened_each_sample":true}));
}

#[path = "turn_execution_frame_runtime_tests.rs"]
mod runtime_tests;
