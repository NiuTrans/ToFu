//! Actual shared semantic execution must certify every shortcut and refuse
//! stale, foreign, time-dependent, closed, poisoned or uncertain sources.
use super::*;
fn open() -> (tempfile::TempDir, EmbeddedRuntime) {
    let directory = tempfile::tempdir().unwrap();
    drop(AuthorityDatabase::initialize(directory.path()).unwrap());
    let mut secret = [7;32];
    let runtime = EmbeddedRuntime::open(directory.path(), 11, Some(7), None, &mut secret).unwrap();
    (directory, runtime)
}
fn execute(runtime: &EmbeddedRuntime, operation: &str, payload: Value) -> Value {
    runtime.execute_json(&EmbeddedRequest { operation: operation.into(), payload: &payload,
        command_id: (operation == "record.put" || operation == "record.delete").then(|| uuid::Uuid::new_v4().to_string()),
        deadline_unix_ms: now_unix_ms()+10000 }).unwrap()
}
fn key() -> Value { json!({"namespace":"cache-test","key":"one","user_id":11}) }
fn cached(runtime: &EmbeddedRuntime, payload: &Value) -> Option<Value> {
    runtime.try_cached_json("record.get", &serde_json::to_vec(payload).unwrap(), None, now_unix_ms()+10000, || true)
        .map(|result| serde_json::from_slice(&result.unwrap()).unwrap())
}
fn warm(runtime: &EmbeddedRuntime) -> Value {
    let mut payload = key(); payload["value"] = json!({"v":1});
    assert_eq!(execute(runtime,"record.put",payload)["status"],0);
    let answer = execute(runtime,"record.get",key()); assert_eq!(answer["status"],0);
    assert_eq!(execute(runtime,"record.get",key()),answer);
    assert_eq!(cached(runtime,&key()).as_ref(),Some(&answer)); answer
}
#[test]
fn query_result_cache_tracks_writes_deletes_missing_results_and_ir_policy() {
    let (_directory,runtime) = open(); let first = warm(&runtime);
    let mut payload = key(); payload["value"] = json!({"v":2});
    assert_eq!(execute(&runtime,"record.put",payload)["status"],0);
    assert!(cached(&runtime,&key()).is_none());
    let second = execute(&runtime,"record.get",key());
    assert_ne!(second,first); assert_eq!(second["payload"]["value"],json!({"v":2}));
    assert_eq!(cached(&runtime,&key()),Some(second));
    assert_eq!(execute(&runtime,"record.delete",key())["status"],0);
    assert!(cached(&runtime,&key()).is_none());
    let missing = execute(&runtime,"record.get",key()); assert!(missing["payload"].is_null());
    assert_eq!(cached(&runtime,&key()),Some(missing));
    let task = json!({"namespace":"task_results","key":"absent","user_id":11});
    execute(&runtime,"record.get",task.clone()); assert!(cached(&runtime,&task).is_none());
    let payload = serde_json::to_vec(&key()).unwrap();
    assert!(runtime.try_cached_json("record.get",&payload,None,1, || true).is_none());
    assert!(runtime.try_cached_json("record.get",&payload,Some("explicit"),now_unix_ms()+10000, || true).is_none());
}
#[test]
fn query_result_cache_preserves_scope_admission_and_no_permanent_pins() {
    let (_directory,runtime) = open(); warm(&runtime);
    let payload=serde_json::to_vec(&key()).unwrap();
    let lookup=crate::query_result_cache::Lookup {owner_id:11,tenant_id:Some(7),schema_id:STORAGE_SCHEMA_VERSION,operation:"record.get",payload:&payload};
    let view=runtime.dispatch_bindings.published_queries.as_ref().unwrap();
    assert!(view.try_cached(lookup).is_some());
    assert!(view.try_cached(crate::query_result_cache::Lookup{owner_id:12,..lookup}).is_none());
    assert!(view.try_cached(crate::query_result_cache::Lookup{tenant_id:Some(8),..lookup}).is_none());
    assert_eq!(runtime.database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles,0);
    assert!(runtime.try_cached_json("record.get",&payload,None,now_unix_ms()+10000,||false).is_none());
    let cold=serde_json::to_vec(&json!({"namespace":"cache-test","key":"never-read","user_id":11})).unwrap();
    assert!(runtime.try_cached_json("record.get",&cold,None,now_unix_ms()+10000,||panic!("cold lookup inspected transport")).is_none());
    *runtime.closed.lock().unwrap()=true;
    assert!(cached(&runtime,&key()).is_none());
    *runtime.closed.lock().unwrap()=false;
}
#[test]
fn query_result_cache_wal_error_and_authority_poison_refuse_reuse() {
    let (_directory,runtime) = open(); warm(&runtime);
    runtime.dispatch_bindings.wal_barrier.as_ref().unwrap().inject_background_error_for_test(io::Error::other("cache WAL fault"));
    assert!(cached(&runtime,&key()).is_none());
    runtime.disarm_drop(); drop(runtime);
    let (_directory,runtime) = open(); warm(&runtime);
    assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{
        let _guard=runtime.database.lock().unwrap();panic!("authority poison");
    })).is_err());
    assert!(cached(&runtime,&key()).is_none()); runtime.disarm_drop();
}

#[test]
fn query_result_cache_concurrent_writer_ack_invalidates_and_held_results_are_immutable() {
    let (_directory, runtime) = open(); warm(&runtime);
    let bytes=serde_json::to_vec(&key()).unwrap();
    let old=runtime.dispatch_bindings.published_queries.as_ref().unwrap().try_cached(
        crate::query_result_cache::Lookup {owner_id:11,tenant_id:Some(7),schema_id:STORAGE_SCHEMA_VERSION,operation:"record.get",payload:&bytes}).unwrap();
    std::thread::scope(|scope| {
        let (acked, receive_ack)=std::sync::mpsc::channel();
        let (read, receive_read)=std::sync::mpsc::channel();
        let runtime=&runtime;
        let writer=scope.spawn(move || {
            for version in 2..66 {
                let mut payload=key();payload["value"]=json!({"v":version});
                assert_eq!(execute(runtime,"record.put",payload)["status"],0);
                acked.send(version).unwrap();
                receive_read.recv_timeout(std::time::Duration::from_secs(10)).unwrap();
            }
        });
        for expected in 2..66 {
            assert_eq!(receive_ack.recv_timeout(std::time::Duration::from_secs(10)).unwrap(),expected);
            assert!(cached(runtime,&key()).is_none(),"previous snapshot survived acknowledged write");
            let answer=execute(runtime,"record.get",key());
            assert_eq!(answer["payload"]["value"],json!({"v":expected}));
            assert_eq!(cached(runtime,&key()),Some(answer));read.send(()).unwrap();
        }
        writer.join().unwrap();
    });
    let old:Value=serde_json::from_slice(&old.into_response()).unwrap();
    assert_eq!(old["value"],json!({"v":1}));
    let closed=runtime.closed.lock().unwrap();
    assert!(cached(&runtime,&key()).is_none(),"caller must not wait on lifecycle mutex");
    drop(closed);
    assert!(cached(&runtime,&key()).is_some());
}

#[test]
fn query_result_cache_snapshot_tracks_acknowledged_turns_and_exact_window_identity() {
    let (_directory, runtime) = open();
    let query = |limit| json!({"conversation_id":"cached-history","user_id":11,"turn_limit":limit});
    let lookup = |payload: &Value| runtime.try_cached_json("turn.sync.snapshot",
        &serde_json::to_vec(payload).unwrap(), None, now_unix_ms()+10000, || true)
        .map(|result| serde_json::from_slice::<Value>(&result.unwrap()).unwrap());
    let read = |payload: Value| execute(&runtime, "turn.sync.snapshot", payload);
    let missing = read(query(1));
    assert_eq!(missing["status"], 0);
    assert!(missing["payload"].is_null());
    assert_eq!(read(query(1)), missing);
    assert_eq!(lookup(&query(1)), Some(missing));
    for ordinal in 0..3 {
        let payload = json!({"conversation_id":"cached-history","user_id":11,
            "actor":"human","status":"completed","projection":{"content":format!("turn {ordinal}")},
            "lane_id":"main","command_id":format!("append-{ordinal}"),
            "turn_id":format!("turn-{ordinal}"),"created_at":1000+ordinal,"now":1000+ordinal,
            "conversation_defaults":{"allowCreate":true,"title":"History","createdAt":1000,"settings":{}}});
        let appended = runtime.execute_json(&EmbeddedRequest { operation:"turn.append_settled".into(),
            payload:&payload, command_id:Some(format!("receipt-{ordinal}")), deadline_unix_ms:now_unix_ms()+10000 }).unwrap();
        assert_eq!(appended["status"], 0, "{appended}");
        assert!(lookup(&query(1)).is_none(), "acknowledged append left an older snapshot reusable");
        let answer = read(query(1));
        assert_eq!(answer["status"], 0, "{answer}");
        assert_eq!(answer["payload"]["turns"].as_array().unwrap().len(), 1);
        assert_eq!(answer["payload"]["turns"][0]["turnId"], format!("turn-{ordinal}"));
        assert_eq!(answer["payload"]["turnWindow"]["totalTurns"], ordinal+1);
        assert_eq!(answer["payload"]["turnWindow"]["hasMore"], ordinal != 0);
        assert!(lookup(&query(1)).is_none(), "first read after a write must not refresh a copied snapshot");
        assert_eq!(read(query(1)), answer);
        assert_eq!(lookup(&query(1)), Some(answer));
    }
    assert!(lookup(&query(2)).is_none(), "different window was incorrectly reused");
    let wider = read(query(2));
    assert_eq!(wider["payload"]["turns"].as_array().unwrap().len(), 2);
    assert_eq!(read(query(2)), wider);
    assert_eq!(lookup(&query(2)), Some(wider));
    let bytes = serde_json::to_vec(&query(1)).unwrap();
    assert!(runtime.try_cached_json("turn.sync.snapshot", &bytes, None, 1, || true).is_none());
    assert!(runtime.try_cached_json("turn.sync.snapshot", &bytes, Some("command"), now_unix_ms()+10000, || true).is_none());
    let view = runtime.dispatch_bindings.published_queries.as_ref().unwrap();
    let identity = crate::query_result_cache::Lookup {owner_id:12,tenant_id:Some(7),schema_id:STORAGE_SCHEMA_VERSION,
        operation:"turn.sync.snapshot",payload:&bytes};
    assert!(view.try_cached(identity).is_none());
    assert!(view.try_cached(crate::query_result_cache::Lookup {owner_id:11,tenant_id:Some(8),..identity}).is_none());
    assert_eq!(runtime.database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
}
