//! Atomic transfer, unknown-delivery and identity fences before resumed effects.
use serde_json::{json, Value};
use std::{collections::BTreeMap, sync::{Mutex, atomic::{AtomicBool, Ordering}}};
use tofu_runtime::checkpoint_journal::{Error, Handoff, Header, Journal, ResumeLineage, Scope, SnapshotWrite, Store};

type Records = BTreeMap<(u64, String), (u64, Value, Option<Value>)>;
#[derive(Default)]
struct AtomicStore {
    records: Mutex<Records>,
    refuse: AtomicBool,
    writer_dead: AtomicBool,
    fail_before: AtomicBool,
    fail_after: AtomicBool,
}
impl Store for AtomicStore {
    fn writer_process(&self) -> Option<Value> {
        Some(json!({"boot_id":"fixture","pid_namespace":1,"pid":1,"start_ticks":1}))
    }
    fn writer_quiescent(&self, _: u64, _: &str, _: &Value) -> Result<bool, Error> {
        Ok(self.writer_dead.load(Ordering::Acquire))
    }
    fn header(&self, owner: u64, task: &str) -> Result<Option<Header>, Error> {
        Ok(self.records.lock().unwrap().get(&(owner, task.into()))
            .map(|(version, value, _)| Header { version: *version, value: value.clone() }))
    }
    fn compare_exchange(&self, owner: u64, task: &str, expected: u64, header: &Value,
        snapshot: SnapshotWrite<'_>) -> Result<u64, Error> {
        let mut rows = self.records.lock().unwrap();
        let key = (owner, task.into());
        if rows.get(&key).map_or(0, |r| r.0) != expected { return Err(Error::Conflict); }
        let state = match snapshot { SnapshotWrite::Keep => rows.get(&key).and_then(|r| r.2.clone()),
            SnapshotWrite::Replace(value) => Some(value.clone()) };
        rows.insert(key, (expected + 1, header.clone(), state));
        Ok(expected + 1)
    }
    fn snapshot(&self, owner: u64, task: &str, version: u64) -> Result<Option<Value>, Error> {
        let rows = self.records.lock().unwrap();
        let row = rows.get(&(owner, task.into())).ok_or(Error::Conflict)?;
        if row.0 != version { return Err(Error::Conflict); }
        Ok(row.2.clone())
    }
    fn handoff(&self, request: Handoff<'_>) -> Result<u64, Error> {
        if self.refuse.load(Ordering::Acquire) { return Err(Error::Ineligible); }
        let mut rows = self.records.lock().unwrap();
        if self.fail_before.swap(false, Ordering::AcqRel) { return Err(Error::Storage); }
        let source = (7, request.lineage.source_task_id.clone());
        let target = (7, request.target_header["task_id"].as_str().unwrap().into());
        if rows.get(&source).map_or(0, |r| r.0) != request.source_version || rows.contains_key(&target) {
            return Err(Error::Conflict);
        }
        rows.insert(source, (request.source_version + 1, request.source_header.clone(), None));
        rows.insert(target, (1, request.target_header.clone(), Some(request.target_snapshot.clone())));
        if self.fail_after.swap(false, Ordering::AcqRel) { return Err(Error::Storage); }
        Ok(1)
    }
}
fn scope(task: &str) -> Scope { Scope { owner: 7, task_id: task.into(), epoch: "epoch".into(), binding: "binding".into() } }
fn journal<'a>(store: &'a dyn Store, task: &str) -> Journal<'a> { Journal::new(scope(task), store, 65536, 1024).unwrap() }
fn lineage() -> ResumeLineage { ResumeLineage { conversation_id: "conversation".into(), turn_id: "turn".into(),
    source_attempt_id: "old".into(), target_attempt_id: "new".into(), source_task_id: "source".into(), reserved_micro: 25 } }
fn snapshot() -> Value {
    json!({"format":"tofu.native-agent-checkpoint/v1","owner_user_id":7,"task_id":"source",
        "epoch":"epoch","binding":"binding","next_round":1,"tool_calls":1,
        "messages":[{"role":"user","content":"question"},{"role":"tool","content":"result"}],
        "usage":[{}],"round_limit":3,"tool_limit":3,"no_progress_limit":0,"failure_limit":0,
        "progress":{"codec":"sha256-token-sequence/v1","last_calls":null,"last_world":"","last_evidence":null,
            "last_failure":null,"no_progress_streak":0,"failure_streak":0},
        "shared_model_account":{"format":"tofu.native-shared-account/v1","owner_user_id":7,"root_task_id":"source",
            "tokens":5,"tool_calls":1,"control_tool_calls":0,"model_ordinal":1,"tool_ordinal":1,"complete":true,
            "limits":{"max_tokens":100,"max_tool_calls":3,"deadline":null},
            "counter_policy":{"encoding":"cl100k_base","output_field":"max_tokens","wire_model":"alpha"}},
        "host_fee_projection":{"format":"tofu.native-fee-checkpoint/v1","owner_user_id":7,"tenant_id":"",
            "task_id":"source","logical_model":"alpha","provider_id":"provider","wallet_user_id":"wallet",
            "reserved_micro":10,"rounds":[{"round":1,"cost":{"amounts":null,"price":{},"credits":{}}}]}})
}
fn seed(store: &AtomicStore) {
    let source = journal(store, "source"); let lease = source.acquire("old").unwrap();
    lease.load().unwrap(); lease.begin(0).unwrap(); lease.complete(&snapshot()).unwrap(); lease.release().unwrap();
}
#[test]
fn root_handoff_cannot_turn_actor_evidence_into_a_shared_account() {
    let store = AtomicStore::default();
    let source = journal(&store, "source"); let lease = source.acquire("old").unwrap();
    lease.load().unwrap(); lease.begin(0).unwrap();
    let mut state = snapshot();
    state["shared_model_account"]["actor_task_id"] = json!("source");
    // Even a forged root label must not make the optional actor scope a root grant.
    lease.complete(&state).unwrap(); lease.release().unwrap();
    let before = store.records.lock().unwrap().clone();
    assert!(matches!(journal(&store, "target").handoff("new", &lineage()), Err(Error::Invalid)));
    assert_eq!(*store.records.lock().unwrap(), before);
}
#[test]
fn handoff_rebinds_accounts_preserves_origin_and_reclaims_only_source_snapshot() {
    let store = AtomicStore::default(); seed(&store);
    let target = journal(&store, "target"); let lease = target.handoff("new", &lineage()).unwrap();
    let state = lease.load().unwrap().unwrap();
    assert_eq!(state["task_id"], "target"); assert_eq!(state["shared_model_account"]["root_task_id"], "target");
    assert_eq!(state["shared_model_account"]["tokens"], 5);
    assert_eq!(state["host_fee_projection"]["task_id"], "target");
    assert_eq!(state["host_fee_projection"]["reserved_micro"], 25);
    assert_eq!(state["host_fee_projection"]["rounds"][0]["task_id"], "source");
    let source = store.header(7,"source").unwrap().unwrap();
    assert_eq!(source.value["phase"], "terminal"); assert!(store.snapshot(7,"source",source.version).unwrap().is_none());
    assert_eq!(journal(&store,"source").acquire("again").err(),Some(Error::Ineligible));
    assert_eq!(target.handoff("duplicate",&lineage()).err(),Some(Error::Conflict));
    assert_eq!(journal(&store,"competitor").handoff("other",&lineage()).err(),Some(Error::Ineligible));
}
#[test]
fn ambiguous_transfer_never_reopens_or_repeats_the_frontier() {
    for after in [false,true] {
        let store = AtomicStore::default(); seed(&store);
        if after { store.fail_after.store(true,Ordering::Release); } else { store.fail_before.store(true,Ordering::Release); }
        let target = journal(&store,"target"); assert_eq!(target.handoff("new",&lineage()).err(),Some(Error::Storage));
        let source = store.header(7,"source").unwrap().unwrap();
        if after {
            assert_eq!(source.value["phase"],"terminal");
            assert_eq!(target.acquire("retry").err(),Some(Error::Ineligible));
            assert_eq!(target.handoff("retry",&lineage()).err(),Some(Error::Conflict));
        } else { assert_eq!(source.value["phase"],"completed"); assert!(store.header(7,"target").unwrap().is_none()); }
    }
}
#[test]
fn held_inflight_terminal_foreign_and_unproven_sources_refuse() {
    for fault in ["held","inflight","terminal","foreign_owner","changed_binding","unknown_usage","no_authority"] {
        let store = AtomicStore::default(); seed(&store);
        let target = match fault {
            "foreign_owner" => Journal::new(Scope { owner:8,..scope("target") },&store,65536,1024).unwrap(),
            "changed_binding" => Journal::new(Scope { binding:"foreign".into(),..scope("target") },&store,65536,1024).unwrap(),
            _ => journal(&store,"target"),
        };
        if fault=="no_authority" { store.refuse.store(true,Ordering::Release); }
        {
            let mut rows=store.records.lock().unwrap();let row=rows.get_mut(&(7,"source".into())).unwrap();
            match fault {
                "held" => row.1["lease"]=json!("other"),
                "inflight" => {row.1["phase"]=json!("inflight");row.1["lease"]=json!("other");},
                "terminal" => row.1["phase"]=json!("terminal"),
                "unknown_usage" => row.2.as_mut().unwrap()["shared_model_account"]["complete"]=json!(false),
                _ => {},
            }
        }
        assert!(target.handoff("new",&lineage()).is_err(),"{fault}");
        assert!(store.header(7,"target").unwrap().is_none(),"{fault}");
    }
}

#[test]
fn tree_fee_handoff_preserves_child_executor_and_original_billing_receiver() {
    let store = AtomicStore::default();
    let mut saved = snapshot();
    saved["shared_model_account"]["tokens"] = json!(15);
    saved["shared_model_account"]["root_tokens"] = json!(5);
    saved["host_fee_projection"]["rounds"][0]["public_round"] = json!(1);
    saved["host_fee_projection"]["child_rounds"] = json!([{
        "round":2, "task_id":"child", "source_round":1, "billable_task_id":"source",
        "usage":{"prompt_tokens":8,"completion_tokens":2},
        "cost":{"amounts":null,"price":{},"credits":{}}
    }]);
    let original_children = saved["host_fee_projection"]["child_rounds"].clone();
    let source = journal(&store, "source");
    let old = source.acquire("old").unwrap();
    old.load().unwrap(); old.begin(0).unwrap(); old.complete(&saved).unwrap(); old.release().unwrap();
    let target = journal(&store, "target");
    let next = target.handoff("new", &lineage()).unwrap();
    let retained = next.load().unwrap().unwrap();
    assert_eq!(retained["next_round"], 1);
    assert_eq!(retained["shared_model_account"]["root_task_id"], "target");
    assert_eq!(retained["shared_model_account"]["tokens"], 15);
    assert_eq!(retained["shared_model_account"]["root_tokens"], 5);
    assert_eq!(retained["host_fee_projection"]["child_rounds"], original_children);
    assert_eq!(retained["host_fee_projection"]["rounds"][0]["task_id"], "source");
    assert_eq!(retained["host_fee_projection"]["reserved_micro"], 25);
}

#[test]
fn pending_transfer_requires_dead_writer_and_keeps_original_effect_through_repeated_handoffs() {
    let store=AtomicStore::default();
    let source=journal(&store,"source");let old=source.acquire("old").unwrap();
    old.load().unwrap();old.begin(0).unwrap();
    let mut pending=snapshot();pending["tools_pending"]=json!(true);
    pending["host_fee_projection"]["wallet_user_id"]=json!("");
    pending["messages"]=json!([{"role":"user","content":"question"},{"role":"assistant","content":"","tool_calls":[
        {"id":"file","type":"function","function":{"name":"write_file","arguments":"{}"}}]}]);
    pending["tool_recovery"]=json!({"producer_task_id":"source","operation":"server-operation"});
    old.pending(&pending).unwrap();
    let target=journal(&store,"target");
    assert_eq!(target.handoff("new",&lineage()).err(),Some(Error::Ineligible));
    store.writer_dead.store(true,Ordering::Release);
    let transferred=target.handoff("new",&lineage()).unwrap();
    let loaded=transferred.load().unwrap().unwrap();
    assert_eq!(loaded["tools_pending"],true);assert_eq!(loaded["tool_recovery"],pending["tool_recovery"]);
    assert_eq!(loaded["shared_model_account"]["root_task_id"],"target");
    assert_eq!(loaded["host_fee_projection"]["rounds"][0]["task_id"],"source");
    assert_eq!(old.complete(&snapshot()).err(),Some(Error::Conflict));
    let third=journal(&store,"third");let relation=ResumeLineage {source_task_id:"target".into(),
        source_attempt_id:"new".into(),target_attempt_id:"third-attempt".into(),..lineage()};
    let again=third.handoff("again",&relation).unwrap();let saved=again.load().unwrap().unwrap();
    assert_eq!(saved["task_id"],"third");assert_eq!(saved["tool_recovery"],pending["tool_recovery"]);
    assert_eq!(saved["host_fee_projection"]["rounds"][0]["task_id"],"source");
    assert_eq!(transferred.begin(1).err(),Some(Error::Conflict));
}

#[test]
fn pending_wallet_handoff_retains_original_unsettled_debt() {
    let store=AtomicStore::default();store.writer_dead.store(true,Ordering::Release);
    let source=journal(&store,"source");let old=source.acquire("old").unwrap();old.load().unwrap();old.begin(0).unwrap();
    let mut pending=snapshot();pending["tools_pending"]=json!(true);
    pending["messages"]=json!([{"role":"user","content":"question"},{"role":"assistant","tool_calls":[
        {"id":"file","type":"function","function":{"name":"write_file","arguments":"{}"}}]}]);
    old.pending(&pending).unwrap();
    assert_eq!(journal(&store,"target").handoff("new",&lineage()).err(),Some(Error::Ineligible));
    assert!(store.header(7,"target").unwrap().is_none());
    let header=store.header(7,"source").unwrap().unwrap();assert_eq!(header.value["phase"],"tools_pending");
    assert_eq!(store.snapshot(7,"source",header.version).unwrap(),Some(pending));
}

fn wallet_pending() -> Value {
    let mut state=snapshot();state["tools_pending"]=json!(true);
    state["messages"]=json!([{"role":"user","content":"question"},{"role":"assistant","tool_calls":[
        {"id":"file","type":"function","function":{"name":"write_file","arguments":"{}"}}]}]);
    state["host_fee_projection"]["rounds"][0]["cost"]=json!({"price":{"prompt_tokens":3},
        "amounts":{"outputTokens":2},"credits":{"input":1,"output":2,"cache_read":0,"cache_write":0,"base":3,"margin":0,"total":3}});
    state["host_fee_projection"]["wallet_settlement"]=json!({"format":"tofu.native-billing/v1",
        "task_id":"source","owner_user_id":7,"wallet_user_id":"wallet","model":"alpha","reserved_micro":10,
        "roundCount":1,"componentsMicro":{"input":1,"output":2,"cache_read":0,"cache_write":0,"baseMicro":3,"marginMicro":0,"totalMicro":3}});
    state
}
#[test]
fn pending_wallet_retirement_preserves_original_debt_across_repeated_handoffs() {
    use tofu_runtime::checkpoint_journal::retired_wallet_invoice;
    let store=AtomicStore::default();store.writer_dead.store(true,Ordering::Release);
    let source=journal(&store,"source");let old=source.acquire("old").unwrap();old.load().unwrap();old.begin(0).unwrap();
    let pending=wallet_pending();old.pending(&pending).unwrap();
    let target=journal(&store,"target");let transferred=target.handoff("new",&lineage()).unwrap();
    let state=transferred.load().unwrap().unwrap();let header=store.header(7,"source").unwrap().unwrap();
    assert_eq!(retired_wallet_invoice(&header.value).unwrap(),Some(&pending["host_fee_projection"]["wallet_settlement"]));
    assert!(store.snapshot(7,"source",header.version).unwrap().is_none());
    assert_eq!(state["host_fee_projection"]["wallet_settlement"]["task_id"],"target");
    assert_eq!(state["host_fee_projection"]["wallet_settlement"]["reserved_micro"],25);
    assert_eq!(state["host_fee_projection"]["wallet_settlement"]["roundCount"],0);
    let relation=ResumeLineage {source_task_id:"target".into(),source_attempt_id:"new".into(),target_attempt_id:"third".into(),..lineage()};
    journal(&store,"third").handoff("third",&relation).unwrap();
    let second=store.header(7,"target").unwrap().unwrap();
    assert_eq!(retired_wallet_invoice(&second.value).unwrap().unwrap()["componentsMicro"]["totalMicro"],0);
    assert_eq!(store.header(7,"source").unwrap().unwrap().value,header.value);
}
#[test]
fn pending_wallet_malformed_invoice_never_retires_source() {
    for fault in ["owner","task","wallet","reserve","fee","count","tokens","missing"] {
        let store=AtomicStore::default();store.writer_dead.store(true,Ordering::Release);
        let source=journal(&store,"source");let old=source.acquire("old").unwrap();old.load().unwrap();old.begin(0).unwrap();
        let mut state=wallet_pending();
        match fault {
            "owner"=>state["host_fee_projection"]["wallet_settlement"]["owner_user_id"]=json!(8),
            "task"=>state["host_fee_projection"]["wallet_settlement"]["task_id"]=json!("other"),
            "wallet"=>state["host_fee_projection"]["wallet_settlement"]["wallet_user_id"]=json!("other"),
            "reserve"=>state["host_fee_projection"]["wallet_settlement"]["reserved_micro"]=json!(11),
            "fee"=>state["host_fee_projection"]["wallet_settlement"]["componentsMicro"]["totalMicro"]=json!(4),
            "count"=>state["host_fee_projection"]["wallet_settlement"]["roundCount"]=json!(2),
            "tokens"=>state["shared_model_account"]["tokens"]=json!(6),
            "missing"=>{state["host_fee_projection"].as_object_mut().unwrap().remove("wallet_settlement");},
            _=>unreachable!(),
        }
        old.pending(&state).unwrap();let before=store.header(7,"source").unwrap().unwrap();
        assert!(journal(&store,"target").handoff("new",&lineage()).is_err(),"{fault}");
        assert_eq!(store.header(7,"source").unwrap().unwrap().value,before.value);
        assert!(store.header(7,"target").unwrap().is_none());
    }
}

#[test]
fn wallet_credit_signed_bounds_do_not_narrow_unsigned_token_account() {
    use tofu_runtime::checkpoint_journal::handoff_wallet_invoice;
    let mut state=wallet_pending();let prompt=i64::MAX as u64 + 1;
    state["host_fee_projection"]["rounds"][0]["cost"]["price"]["prompt_tokens"]=json!(prompt);
    state["shared_model_account"]["tokens"]=json!(prompt+2);
    assert!(handoff_wallet_invoice(&state).unwrap().is_some());
    state["host_fee_projection"]["rounds"][0]["cost"]["price"]["prompt_tokens"]=json!(u64::MAX);
    assert_eq!(handoff_wallet_invoice(&state),Err(Error::Invalid));
}

#[test]
fn completed_previous_handoff_does_not_fence_a_new_generation() {
    use tofu_runtime::checkpoint_journal::validate_actor_handoff_accounts;
    let original: Value = serde_json::from_str(include_str!(
        "../../tofu-db/tests/fixtures/native_goal_actor_claim.json")).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../tofu-db/tests/fixtures/native_goal_actor_handoff.json")).unwrap();
    let mut source = original["cut"].clone();
    source["recovery"] = json!({"source_root_task_id":"earlier-root",
        "source_frontier_version":1,"pending_actors":{}});
    let verify = |cut: &Value| validate_actor_handoff_accounts(cut, &fixture["before"],
        fixture["frontiers"].as_array().unwrap(),
        &[("pending-a", "target-a"), ("pending-b", "target-b")], "target-root");
    assert_eq!(verify(&source), Ok(()));
    for damage in ["unmoved", "null", "same_root", "missing", "counter"] {
        let mut bad = source.clone();
        match damage {
            "unmoved" => bad["recovery"]["pending_actors"]["earlier-actor"] =
                source["actors"]["pending-a"].clone(),
            "null" => bad["recovery"]["pending_actors"] = Value::Null,
            "same_root" => bad["recovery"]["source_root_task_id"] = source["snapshot"]["root_task_id"].clone(),
            "missing" => { bad["recovery"].as_object_mut().unwrap().remove("pending_actors"); },
            "counter" => bad["snapshot"]["tokens"] = json!(999999),
            _ => unreachable!(),
        }
        assert!(verify(&bad).is_err(), "{damage}");
    }
}
