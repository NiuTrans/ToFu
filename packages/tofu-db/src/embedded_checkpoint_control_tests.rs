//! Real C ABI capabilities, one-shot buffer admission and durable lease fences.
use super::*;
use serde_json::{json, Value};
use std::sync::LazyLock;
static CONTRACT: LazyLock<Value> = LazyLock::new(|| serde_json::from_str(include_str!(
    "../../../contracts/native_checkpoint_control_v1.schema.json")).unwrap());
fn headroom() -> usize { CONTRACT["limits"]["responseHeadroomBytes"].as_u64().unwrap() as usize }
fn parse(operation: &str, bytes: &[u8]) -> io::Result<usize> {
    Registry::default().response_bound(operation, bytes)
}
use crate::embed::EmbeddedRuntime;
use crate::embed_ffi::{
    EmbedHandle, tofudb_embed_checkpoint_execute, tofudb_embed_close, tofudb_embed_execute,
    tofudb_embed_open,
};
use std::ffi::CString;
fn deadline() -> u64 {
    (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis()
        + 30000) as u64
}
struct Handle {
    pointer: *mut EmbedHandle,
    directory: tempfile::TempDir,
}
impl Handle {
    fn open() -> Self {
        let directory = tempfile::tempdir().unwrap();
        drop(crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap());
        Self::reopen(directory)
    }
    fn reopen(directory: tempfile::TempDir) -> Self {
        let path = CString::new(directory.path().to_str().unwrap()).unwrap();
        let mut secret = b"checkpoint-cabi-test-secret-0000001".to_vec();
        let mut error = vec![0; 4096];
        let pointer = unsafe {
            tofudb_embed_open(
                path.as_ptr(),
                7,
                3,
                std::ptr::null(),
                secret.as_mut_ptr(),
                secret.len(),
                error.as_mut_ptr(),
                error.len(),
            )
        };
        assert!(!pointer.is_null(), "{}", String::from_utf8_lossy(&error));
        assert!(secret.iter().all(|b| *b == 0));
        Self { pointer, directory }
    }
    fn call(&self, operation: &str, payload: Value, capacity: usize) -> Value {
        self.at(operation, payload, capacity, deadline(), true)
    }
    fn at(
        &self,
        operation: &str,
        payload: Value,
        capacity: usize,
        at: u64,
        checkpoint: bool,
    ) -> Value {
        let request = serde_json::to_vec(
            &json!({"operation":operation,"payload":payload,"deadline_unix_ms":at}),
        )
        .unwrap();
        let mut out = vec![0; capacity];
        let written = unsafe {
            if checkpoint {
                tofudb_embed_checkpoint_execute(
                    self.pointer,
                    request.as_ptr(),
                    request.len(),
                    out.as_mut_ptr(),
                    out.len(),
                )
            } else {
                tofudb_embed_execute(
                    self.pointer,
                    request.as_ptr(),
                    request.len(),
                    out.as_mut_ptr(),
                    out.len(),
                )
            }
        };
        assert!(
            written >= 0,
            "C ABI failed without a complete envelope: {written}"
        );
        serde_json::from_slice(&out[..written as usize]).unwrap()
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe { tofudb_embed_close(self.pointer) };
        self.pointer = std::ptr::null_mut();
    }
}
fn parameters() -> Value {
    json!({"owner_user_id":7,"task_id":"task","epoch":"epoch","binding":"model-tools-account","token":"caller","header_bytes":4096,"snapshot_bytes":1048576,"identity_bytes":1024})
}
fn payload(id: u64) -> Value {
    json!({"format":"tofu.native-checkpoint-control/v1","capability_id":id})
}
fn create(handle: &Handle, params: Value) -> Value {
    handle.call(
        "create",
        json!({"format":"tofu.native-checkpoint-control/v1","parameters":params}),
        1048576 + headroom(),
    )
}
fn state() -> Value {
    json!({"format":"tofu.native-agent-checkpoint/v1","owner_user_id":7,"task_id":"task","epoch":"epoch","binding":"model-tools-account","next_round":1,"tool_calls":1,"messages":[{"role":"user","content":"question"},{"role":"tool","content":"x".repeat(65536)}],"usage":[{}],"round_limit":3,"tool_limit":3,"no_progress_limit":0,"failure_limit":0,"progress":{"codec":"sha256-token-sequence/v1","last_calls":null,"last_world":"","last_evidence":null,"last_failure":null,"no_progress_streak":0,"failure_streak":0}})
}
#[test]
fn checkpoint_cabi_resumes_same_authority_and_old_capabilities_never_rebind() {
    let handle = Handle::open();
    let first = create(&handle, parameters());
    assert_eq!(first["accepted"], true);
    let id = first["capability_id"].as_u64().unwrap();
    let capacity = 1048576 + headroom();
    assert_eq!(
        handle.call("load", payload(id), capacity)["state"],
        Value::Null
    );
    assert_eq!(
        handle.call("load", payload(id), capacity)["error"],
        "ineligible"
    );
    let mut begin = payload(id);
    begin["round"] = json!(0);
    assert_eq!(handle.call("begin", begin, capacity)["accepted"], true);
    let mut complete = payload(id);
    complete["state"] = state();
    assert_eq!(
        handle.call("complete", complete, capacity)["accepted"],
        true
    );
    assert_eq!(
        handle.call("release", payload(id), capacity)["accepted"],
        true
    );
    assert_eq!(
        handle.call("forget", payload(id), capacity)["accepted"],
        true
    );
    let second = create(&handle, parameters());
    let next = second["capability_id"].as_u64().unwrap();
    assert!(next > id);
    assert_eq!(
        handle.call("load", payload(id), capacity)["error"],
        "ineligible"
    );
    assert_eq!(
        handle.call("load", payload(next), capacity)["state"],
        state()
    );
    assert_eq!(
        handle.call("seal", payload(next), capacity)["accepted"],
        true
    );
    assert_eq!(
        handle.call("forget", payload(next), capacity)["accepted"],
        true
    );
}
#[test]
fn checkpoint_cabi_small_buffers_refuse_before_acquisition_and_one_shot_load() {
    let handle = Handle::open();
    let command = json!({"format":"tofu.native-checkpoint-control/v1","parameters":parameters()});
    assert_eq!(handle.call("create", command, headroom())["status"], -1);
    let created = create(&handle, parameters());
    assert_eq!(created["accepted"], true);
    let id = created["capability_id"].as_u64().unwrap();
    assert_eq!(handle.call("load", payload(id), headroom())["status"], -1);
    assert_eq!(
        handle.call("load", payload(id), 1048576 + headroom())["accepted"],
        true
    );
    assert_eq!(
        handle.call("release", payload(id), 1048576 + headroom())["accepted"],
        true
    );
}
#[test]
fn checkpoint_cabi_foreign_owner_unknown_fields_and_expiry_cannot_grant_effects() {
    let handle = Handle::open();
    let mut foreign = parameters();
    foreign["owner_user_id"] = json!(8);
    assert_eq!(create(&handle, foreign)["error"], "invalid");
    let mut invalid =
        json!({"format":"tofu.native-checkpoint-control/v1","parameters":parameters()});
    invalid["capability_id"] = Value::Null;
    assert_eq!(
        handle.call("create", invalid, 1048576 + headroom())["status"],
        -1
    );
    let command = json!({"format":"tofu.native-checkpoint-control/v1","parameters":parameters()});
    assert_eq!(
        handle.at("create", command.clone(), 1048576 + headroom(), 1, true)["status"],
        -1
    );
    assert_ne!(
        handle.at("create", command, 1048576 + headroom(), deadline(), false)["status"],
        0
    );
    let created = create(&handle, parameters());
    assert_eq!(created["accepted"], true);
}
#[test]
fn checkpoint_cabi_forget_and_handle_close_never_release_inflight_lease() {
    let mut handle = Handle::open();
    let created = create(&handle, parameters());
    let id = created["capability_id"].as_u64().unwrap();
    let cap = 1048576 + headroom();
    assert_eq!(handle.call("load", payload(id), cap)["accepted"], true);
    let mut begin = payload(id);
    begin["round"] = json!(0);
    assert_eq!(handle.call("begin", begin, cap)["accepted"], true);
    assert_eq!(handle.call("forget", payload(id), cap)["accepted"], true);
    unsafe { tofudb_embed_close(handle.pointer) };
    handle.pointer = std::ptr::null_mut();
    let path = handle.directory.path().to_path_buf();
    let mut secret = b"checkpoint-cabi-test-secret-0000001".to_vec();
    let runtime = EmbeddedRuntime::open(&path, 7, Some(3), None, &mut secret).unwrap();
    assert!(matches!(
        Arc::new(runtime).checkpoint_lease(
            Scope {
                owner: 7,
                task_id: "task".into(),
                epoch: "epoch".into(),
                binding: "model-tools-account".into()
            },
            4096,
            1048576,
            1024,
            "later"
        ),
        Err(Error::Ineligible)
    ));
}
#[test]
fn checkpoint_control_schema_and_borrowed_parser_agree_on_closed_commands() {
    for operation in [
        "create", "load", "begin", "complete", "seal", "release", "forget", "finish",
    ] {
        let mut value = if operation == "create" {
            json!({"format":"tofu.native-checkpoint-control/v1","parameters":parameters()})
        } else {
            payload(1)
        };
        if operation == "begin" {
            value["round"] = json!(0);
        }
        if operation == "complete" {
            value["state"] = state();
        }
        if operation == "finish" {
            value["status"] = json!("aborted");
            value["suspended"] = json!(true);
        }
        assert!(CONTRACT["$defs"][operation].is_object());
        assert!(parse(operation, &serde_json::to_vec(&value).unwrap()).is_ok());
        value["unknown"] = json!(true);
        assert!(parse(operation, &serde_json::to_vec(&value).unwrap()).is_err());
    }
}

#[test]
fn checkpoint_cabi_finish_freezes_candidate_and_retries_without_new_generation() {
    for (status, suspended) in [
        ("done", false),
        ("error", true),
        ("aborted", false),
        ("aborted", true),
    ] {
        let handle = Handle::open();
        let id = create(&handle, parameters())["capability_id"]
            .as_u64()
            .unwrap();
        let capacity = 1048576 + headroom();
        assert_eq!(handle.call("load", payload(id), capacity)["accepted"], true);
        let mut begin = payload(id);
        begin["round"] = json!(0);
        assert_eq!(handle.call("begin", begin, capacity)["accepted"], true);
        let mut complete = payload(id);
        complete["state"] = state();
        assert_eq!(
            handle.call("complete", complete, capacity)["accepted"],
            true
        );
        let mut finish = payload(id);
        finish["status"] = json!(status);
        finish["suspended"] = json!(suspended);
        assert_eq!(
            handle.call("finish", finish.clone(), capacity)["accepted"],
            true
        );
        assert_eq!(
            handle.call("finish", finish.clone(), capacity)["accepted"],
            true
        );
        finish["suspended"] = json!(!suspended);
        assert_eq!(handle.call("finish", finish, capacity)["error"], "invalid");
        if status == "aborted" && suspended {
            assert_eq!(
                handle.call("release", payload(id), capacity)["accepted"],
                true
            );
            assert_eq!(create(&handle, parameters())["accepted"], true);
        } else {
            assert_eq!(
                handle.call("release", payload(id), capacity)["error"],
                "ineligible"
            );
            assert_eq!(create(&handle, parameters())["error"], "ineligible");
        }
    }
}

#[test]
fn checkpoint_registry_retains_only_live_metadata_and_reclaims_forgotten_capabilities() {
    let handle=Handle::open();let created=create(&handle,parameters());let id=created["capability_id"].as_u64().unwrap();
    let registry=unsafe {&*handle.pointer}.checkpoints.get().unwrap();
    assert_eq!(registry.live_capabilities().unwrap(),1);
    assert_eq!(handle.call("forget",payload(id),1048576+headroom())["accepted"],true);
    assert!(registry.live_capabilities().unwrap() == 0);
    println!("checkpoint_registry_inline_bytes={} registry_lazy_slot_bytes={}",
        std::mem::size_of::<Registry>(),std::mem::size_of::<std::sync::OnceLock<Arc<Registry>>>());
}

#[test]
fn checkpoint_cabi_pending_batch_preserves_paid_state_and_blocks_next_begin() {
    let handle=Handle::open();
    let id=create(&handle,parameters())["capability_id"].as_u64().unwrap();
    let capacity=1048576+headroom();
    assert_eq!(handle.call("load",payload(id),capacity)["accepted"],true);
    let mut begin=payload(id);begin["round"]=json!(0);
    assert_eq!(handle.call("begin",begin,capacity)["accepted"],true);
    let mut saved=state();saved["tools_pending"]=json!(true);
    saved["messages"][1]=json!({"role":"assistant","tool_calls":[{"id":"write-1","type":"function","function":{"name":"write_file","arguments":"{}"}}]});
    let mut pending=payload(id);pending["state"]=saved.clone();
    assert_eq!(handle.call("pending",pending,capacity)["accepted"],true);
    assert_eq!(handle.call("release",payload(id),capacity)["error"],"ineligible");
    let mut begin=payload(id);begin["round"]=json!(1);
    assert_eq!(handle.call("begin",begin.clone(),capacity)["error"],"ineligible");
    saved.as_object_mut().unwrap().remove("tools_pending");
    saved["messages"].as_array_mut().unwrap().push(json!({"role":"tool","tool_call_id":"write-1","content":"observed"}));
    let mut changed=saved.clone();changed["usage"]=json!([{"total_tokens":0}]);
    let mut complete=payload(id);complete["state"]=changed;
    assert_eq!(handle.call("complete",complete.clone(),capacity)["error"],"invalid");
    complete["state"]=saved;
    assert_eq!(handle.call("complete",complete,capacity)["accepted"],true);
    assert_eq!(handle.call("begin",begin,capacity)["accepted"],true);
}

#[test]
fn invoice_read_cannot_acquire_a_lease_or_cross_owner_scope() {
    let handle=Handle::open();let capacity=4096+headroom();
    let query=json!({"format":"tofu.native-checkpoint-control/v1", "invoice":{"owner_user_id":7,"task_id":"task","header_bytes":4096}});
    let missing=handle.call("invoice",query.clone(),capacity);
    assert_eq!(missing["accepted"],true);assert!(missing["state"].is_null());assert!(missing["capability_id"].is_null());
    let created=create(&handle,parameters());assert_eq!(created["accepted"],true);
    let found=handle.call("invoice",query.clone(),capacity);
    assert_eq!(found["accepted"],true);assert_eq!(found["state"]["phase"],"new");
    assert!(found["state"]["wallet_settlement"].is_null());assert!(found["capability_id"].is_null());
    let mut foreign=query;foreign["invoice"]["owner_user_id"]=json!(8);
    assert_eq!(handle.call("invoice",foreign,capacity)["accepted"],false);
    let registry=unsafe {&*handle.pointer}.checkpoints.get().unwrap();
    assert_eq!(registry.live_capabilities().unwrap(),1);
}

#[test]
fn frontier_preflight_has_closed_bounds_and_never_allocates_a_capability() {
    let handle=Handle::open();let capacity=4096+1048576+headroom();
    let query=json!({"format":"tofu.native-checkpoint-control/v1", "frontier":{
        "owner_user_id":7,"task_id":"task","header_bytes":4096,"snapshot_bytes":1048576,"identity_bytes":1024}});
    let missing=handle.call("frontier",query.clone(),capacity);
    assert_eq!(missing["accepted"],true);assert!(missing["state"].is_null());assert!(missing["capability_id"].is_null());
    let created=create(&handle,parameters());assert_eq!(created["capability_id"],1);
    assert_eq!(handle.call("frontier",query.clone(),capacity)["accepted"],false);
    for field in ["header_bytes","snapshot_bytes","identity_bytes","owner_user_id"] {
        let mut invalid=query.clone();invalid["frontier"][field]=json!(0);
        assert!(parse("frontier",&serde_json::to_vec(&invalid).unwrap()).is_err(),"{field}");
    }
    for extra in ["capability_id","parameters","invoice","resume"] {
        let mut invalid=query.clone();invalid[extra]=json!(1);
        assert!(parse("frontier",&serde_json::to_vec(&invalid).unwrap()).is_err(),"{extra}");
    }
    let mut foreign=query;foreign["frontier"]["owner_user_id"]=json!(8);
    assert_eq!(handle.call("frontier",foreign,capacity)["accepted"],false);
    let registry=unsafe {&*handle.pointer}.checkpoints.get().unwrap();
    assert_eq!(registry.live_capabilities().unwrap(),1);
}
