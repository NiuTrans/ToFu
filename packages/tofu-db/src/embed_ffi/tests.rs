//! FFI hardening tests: panic containment, handle poisoning, the execute
//! watchdog, and borrowed request slice representation. The worker-side faults come from
//! `embed::test_fault` and fire by operation name, so the rest of the test
//! process (including a concurrently running embedded test) never cross-fires.

use super::*;

include!("../embed_query_cache_ffi_tests.rs");

#[test]
fn timed_reply_preserves_owned_outcomes_after_timeout_retirement() {
    let (late, retired) = reply::fresh::<JobOutcome>();
    assert!(matches!(retired.recv_timeout(Duration::ZERO), Err(std::sync::mpsc::RecvTimeoutError::Timeout)));
    drop(retired);
    let returned = late.send(JobOutcome::Profiled(Box::new(TimedJobOutcome {
        outcome: JobOutcome::Responded(Ok(vec![11, 22, 33])),
        ready_at: Some(std::time::Instant::now()),
    }))).err().expect("late reply returns its owned allocation");
    assert!(matches!(returned.0, JobOutcome::Profiled(ref reply) if matches!(reply.outcome, JobOutcome::Responded(Ok(ref bytes)) if bytes == &[11, 22, 33])));
    drop(returned);
    let (next, receiver) = reply::fresh::<JobOutcome>();
    assert!(next.send(JobOutcome::Panicked("fault".into())).is_ok());
    assert!(matches!(receiver.recv_timeout(Duration::ZERO).unwrap().receive(), Err(message) if message == "fault"));
    let fault = TimedJobOutcome { outcome: JobOutcome::Responded(Err(io::Error::other("source fault"))), ready_at: None };
    assert!(matches!(fault.receive(), JobOutcome::Responded(Err(error)) if error.to_string() == "source fault"));
}
use crate::authority::AuthorityDatabase;
use crate::embed::test_fault;
use std::ffi::CString;

const SECRET: &[u8; 34] = b"embed-ffi-hardening-test-secret-01";
const OUT_CAP: usize = 1 << 20;

struct Seed {
    _directory: tempfile::TempDir,
    _projection_parent: tempfile::TempDir,
    data_dir: CString,
    projection_dir: CString,
}

fn seed_authority() -> Seed {
    let directory = tempfile::tempdir().unwrap();
    {
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = authority.begin(7, 11).unwrap();
        authority
            .entity_put(
                &mut transaction,
                crate::entity::EntityKey::new(7, 11, "seed", b"k").unwrap(),
                b"seeded".to_vec(),
            )
            .unwrap();
        authority.commit(transaction).unwrap();
        drop(authority);
    }
    let projection_parent = tempfile::tempdir().unwrap();
    let projection = projection_parent.path().join("projection");
    Seed {
        data_dir: CString::new(directory.path().to_str().unwrap()).unwrap(),
        projection_dir: CString::new(projection.to_str().unwrap()).unwrap(),
        _directory: directory,
        _projection_parent: projection_parent,
    }
}

fn open_handle(seed: &Seed) -> *mut EmbedHandle {
    let mut secret = *SECRET;
    let mut error = [0_u8; 4096];
    let handle = unsafe {
        tofudb_embed_open(
            seed.data_dir.as_ptr(),
            11,
            7,
            seed.projection_dir.as_ptr(),
            secret.as_mut_ptr(),
            secret.len(),
            error.as_mut_ptr(),
            error.len(),
        )
    };
    assert!(
        !handle.is_null(),
        "open failed: {}",
        String::from_utf8_lossy(&error)
            .trim_matches(char::from(0))
            .to_owned()
    );
    handle
}

fn execute(handle: *mut EmbedHandle, request: &serde_json::Value) -> serde_json::Value {
    let request = serde_json::to_vec(request).unwrap();
    let mut out = vec![0_u8; OUT_CAP];
    let written = unsafe {
        tofudb_embed_execute(handle, request.as_ptr(), request.len(), out.as_mut_ptr(), out.len())
    };
    assert!(written > 0, "execute returned {written}");
    out.truncate(written as usize);
    serde_json::from_slice(&out).unwrap()
}

fn deadline(millis_from_now: u64) -> u64 {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    now + millis_from_now
}

#[test]
fn open_panic_is_contained_and_returns_no_handle() {
    let directory = tempfile::tempdir().unwrap();
    let marked = directory.path().join(test_fault::OPEN_PANIC_DIR_MARKER);
    std::fs::create_dir_all(&marked).unwrap();
    let data_dir = CString::new(marked.to_str().unwrap()).unwrap();
    let mut secret = *SECRET;
    let mut error = [0_u8; 4096];
    let handle = unsafe {
        tofudb_embed_open(
            data_dir.as_ptr(),
            11,
            0,
            std::ptr::null(),
            secret.as_mut_ptr(),
            secret.len(),
            error.as_mut_ptr(),
            error.len(),
        )
    };
    assert!(handle.is_null(), "a contained open panic returns no handle");
    let text = String::from_utf8_lossy(&error)
        .trim_matches(char::from(0))
        .to_owned();
    assert!(
        text.contains("panic contained in embedded open"),
        "unexpected open error: {text}"
    );
    // The caller's secret buffer was still zeroized on the panic path.
    assert_eq!(secret, [0_u8; 34]);
}

#[test]
fn execute_panic_is_contained_and_poisons_the_handle() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let panicked = execute(
        handle,
        &serde_json::json!({
            "operation": test_fault::PANIC_OPERATION,
            "payload": {},
            "deadline_unix_ms": deadline(30_000),
        }),
    );
    assert_eq!(panicked["error_code"], "embedded_transport_error");
    assert!(
        panicked["error_message"]
            .as_str()
            .unwrap()
            .contains("panic contained in embedded execute"),
        "unexpected envelope: {panicked}"
    );
    assert!(
        panicked["error_message"]
            .as_str()
            .unwrap()
            .contains("forced panic"),
        "the panic payload text feeds the diagnostic: {panicked}"
    );
    // The handle is poisoned: later executes fail fast with the same code.
    let after = execute(
        handle,
        &serde_json::json!({
            "operation": "record.get",
            "payload": {"namespace": "embed", "key": "k/1", "user_id": 11},
            "deadline_unix_ms": deadline(30_000),
        }),
    );
    assert_eq!(after["error_code"], "embedded_transport_error");
    assert!(
        after["error_message"].as_str().unwrap().contains("poisoned"),
        "unexpected envelope: {after}"
    );
    // Close on a poisoned handle frees it without engine operations; it
    // must return promptly instead of joining the unknown-state engine.
    unsafe { tofudb_embed_close(handle) };
}

#[test]
fn watchdog_test_budget_is_isolated_from_other_callers() {
    WATCHDOG_TEST_OVERRIDE_MS.with(|budget| budget.set(200));
    assert_eq!(watchdog_budget(deadline(120_000)), Duration::from_millis(200));
    let independent = std::thread::spawn(|| watchdog_budget(deadline(120_000)))
        .join().unwrap();
    WATCHDOG_TEST_OVERRIDE_MS.with(|budget| budget.set(0));
    assert!(independent > Duration::from_secs(100));
    assert!(watchdog_budget(deadline(120_000)) > Duration::from_secs(100));
}

#[test]
fn watchdog_timeout_poisons_the_handle_and_the_process_survives() {
    // The synchronous caller alone receives this fault-injection budget.
    WATCHDOG_TEST_OVERRIDE_MS.with(|budget| budget.set(200));
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let accounting = unsafe { Arc::clone(&(*handle).input_memory) };
    let wedged = execute(
        handle,
        &serde_json::json!({
            "operation": test_fault::PARK_OPERATION,
            "payload": {},
            "deadline_unix_ms": deadline(60_000),
        }),
    );
    assert_eq!(wedged["error_code"], "embedded_transport_error");
    let message = wedged["error_message"].as_str().unwrap().to_owned();
    assert!(message.contains("watchdog"), "unexpected envelope: {wedged}");
    assert!(message.contains("poisoned"), "unexpected envelope: {wedged}");
    let after = execute(
        handle,
        &serde_json::json!({
            "operation": "record.get",
            "payload": {"namespace": "embed", "key": "k/1", "user_id": 11},
            "deadline_unix_ms": deadline(30_000),
        }),
    );
    assert_eq!(after["error_code"], "embedded_transport_error");
    assert!(
        after["error_message"].as_str().unwrap().contains("poisoned"),
        "unexpected envelope: {after}"
    );
    // Prompt return although the abandoned worker is still parked: close
    // frees the handle and never joins the wedged worker.
    assert_eq!(accounting.bytes.load(Ordering::Acquire), 2);
    unsafe { tofudb_embed_close(handle) };
    assert_eq!(accounting.bytes.load(Ordering::Acquire), 2);
    WATCHDOG_TEST_OVERRIDE_MS.with(|budget| budget.set(0));
}

#[test]
fn normal_operation_within_budget_succeeds() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let put = execute(
        handle,
        &serde_json::json!({
            "operation": "record.put",
            "payload": {
                "namespace": "embed", "key": "k/ffi",
                "value": {"n": 1}, "user_id": 11,
            },
            "command_id": "ffi-cmd-1",
            "deadline_unix_ms": deadline(30_000),
        }),
    );
    assert_eq!(put["status"], 0, "put failed: {put}");
    let get = execute(
        handle,
        &serde_json::json!({
            "operation": "record.get",
            "payload": {"namespace": "embed", "key": "k/ffi", "user_id": 11},
            "deadline_unix_ms": deadline(30_000),
        }),
    );
    assert_eq!(get["status"], 0, "get failed: {get}");
    assert_eq!(get["payload"]["value"], serde_json::json!({"n": 1}));
    unsafe { tofudb_embed_close(handle) };
}

#[test]
fn closing_one_authority_preserves_live_values_other_authority_and_reopen() {
    struct HandleGuard(*mut EmbedHandle);
    impl Drop for HandleGuard {
        fn drop(&mut self) { unsafe { tofudb_embed_close(self.0); } }
    }
    let seeds = [seed_authority(), seed_authority()];
    let first = HandleGuard(open_handle(&seeds[0]));
    let second = HandleGuard(open_handle(&seeds[1]));
    let mut live = vec![0_u8; 2 * 1024 * 1024];
    for (index, byte) in live.iter_mut().enumerate() { *byte = (index % 251) as u8; }
    let live_digest = blake3::hash(&live);
    let put = |index| serde_json::json!({
        "operation": "record.put", "command_id": "close-owner-proof",
        "deadline_unix_ms": deadline(30_000),
        "payload": {"namespace": "close-owner", "key": "same-key",
                    "value": {"index": index}, "user_id": 11},
    });
    let original = execute(first.0, &put(0));
    let survivor = execute(second.0, &put(1));
    assert_eq!(original["status"], 0);
    assert_eq!(survivor["status"], 0);
    drop(first);
    assert_eq!(blake3::hash(&live), live_digest);
    let replay = execute(second.0, &put(1));
    assert_eq!(replay["status"], 0);
    assert_eq!(replay["payload"], survivor["payload"]);
    let reopened = HandleGuard(open_handle(&seeds[0]));
    let read = execute(reopened.0, &serde_json::json!({
        "operation": "record.get", "deadline_unix_ms": deadline(30_000),
        "payload": {"namespace": "close-owner", "key": "same-key", "user_id": 11},
    }));
    assert_eq!(read["status"], 0);
    assert_eq!(read["payload"]["value"], serde_json::json!({"index": 0}));
    assert_eq!(read["payload"]["version"], 1);
    drop(second);
    assert_eq!(blake3::hash(&live), live_digest);
}

#[test]
fn unaddressable_request_slice_is_rejected_before_dispatch() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let request = [b'x'];
    let mut out = vec![0_u8; OUT_CAP];
    let written = unsafe { tofudb_embed_execute(handle, request.as_ptr(), usize::MAX, out.as_mut_ptr(), out.len()) };
    assert!(written > 0);
    out.truncate(written as usize);
    let envelope:serde_json::Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(envelope["error_code"],"embedded_transport_error");
    assert!(envelope["error_message"].as_str().unwrap().contains("addressable"));
    unsafe { tofudb_embed_close(handle) };
}

#[test]
fn independent_calls_execute_concurrently_on_one_handle() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    // Two test operations rendezvous inside execute_json. A serial worker
    // cannot satisfy the rendezvous and returns the contained timeout panic.
    let address = handle as usize;
    let calls: Vec<_> = (0..2).map(|_| std::thread::spawn(move || {
        execute(address as *mut EmbedHandle, &serde_json::json!({
            "operation": test_fault::PARALLEL_OPERATION,
            "payload": {}, "deadline_unix_ms": deadline(10_000),
        }))
    })).collect();
    for call in calls {
        let response = call.join().unwrap();
        assert_ne!(response["error_code"], "embedded_transport_error", "{response}");
    }
    unsafe { tofudb_embed_close(handle); }
}

#[test]
fn owned_input_bytes_track_actual_capacity_without_a_quota() {
    let accounting = Arc::new(InputMemoryAccounting::default());
    let inputs = [vec![0u8; 4096], vec![0u8; 8192]];
    let first = OwnedInputLease::claim(&accounting, inputs[0].capacity()).unwrap();
    let second = OwnedInputLease::claim(&accounting, inputs[1].capacity()).unwrap();
    assert_eq!(accounting.bytes.load(Ordering::Acquire), 12_288);
    assert_eq!(accounting.peak_bytes.load(Ordering::Acquire), 12_288);
    drop(first);
    assert_eq!(accounting.bytes.load(Ordering::Acquire), 8192);
    drop(second);
    assert_eq!(accounting.bytes.load(Ordering::Acquire), 0);
    // Integer overflow is a failed accounting invariant, not memory pressure.
    accounting.bytes.store(usize::MAX - 3, Ordering::Release);
    assert!(OwnedInputLease::claim(&accounting, 4).is_err());
    assert_eq!(accounting.bytes.load(Ordering::Acquire), usize::MAX - 3);
    accounting.bytes.store(0, Ordering::Release);
}

fn wait_for_input_release(accounting: &InputMemoryAccounting) {
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while accounting.bytes.load(Ordering::Acquire) != 0 {
        assert!(std::time::Instant::now() < deadline, "embedded input lease did not retire");
        std::thread::sleep(Duration::from_millis(1));
    }
}

#[test]
fn raw_json_payload_preserves_canonical_receipt_replay_and_invalid_envelopes() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let until = deadline(30_000);
    let request = format!(r#"{{"operation":"record.put","command_id":"raw-json-replay","deadline_unix_ms":{until},"payload":{{ "namespace":"ffi", "key":"raw", "value":{{"text":"\u4e2d","fraction":0.125}} }} }}"#);
    let mut out = vec![0; OUT_CAP];
    let written = unsafe { tofudb_embed_execute(handle, request.as_ptr(), request.len(), out.as_mut_ptr(), out.len()) };
    assert!(written > 0);
    let first: serde_json::Value = serde_json::from_slice(&out[..written as usize]).unwrap();
    assert_eq!(first["status"], 0);
    let replay = execute(handle, &serde_json::json!({"operation":"record.put",
        "command_id":"raw-json-replay", "deadline_unix_ms":until,
        "payload":{"value":{"fraction":0.125,"text":"中"},"key":"raw","namespace":"ffi"}}));
    assert_eq!(replay, first);
    let conflict = execute(handle, &serde_json::json!({"operation":"record.put",
        "command_id":"raw-json-replay", "deadline_unix_ms":until,
        "payload":{"value":{"fraction":0.25,"text":"中"},"key":"raw","namespace":"ffi"}}));
    assert_ne!(conflict["status"], 0);
    let denied = execute(handle, &serde_json::json!({"operation":"record.get",
        "deadline_unix_ms":until,"payload":null}));
    assert_ne!(denied["status"], 0);
    unsafe { tofudb_embed_close(handle) };
}

#[test]
fn large_payload_reaches_semantic_shape_validation_without_poisoning_handle() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let request = serde_json::json!({"operation":"record.put", "command_id":"oversized-raw",
        "deadline_unix_ms":deadline(30_000), "payload":"x".repeat(70 * 1024 * 1024)});
    let response = execute(handle, &request);
    assert_eq!(response["error_code"], "database_protocol_error");
    let healthy = execute(handle, &serde_json::json!({"operation":"system.health",
        "deadline_unix_ms":deadline(30_000), "payload":{}}));
    assert_eq!(healthy["status"], 0, "oversize input must not poison a healthy handle");
    unsafe { tofudb_embed_close(handle) };
}


#[test]
fn large_checkpoint_retains_exact_content_replay_and_cold_owner_scope() {
    use sha2::{Digest, Sha256};
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let content = "C".repeat(crate::embed::MAX_EMBEDDED_PAYLOAD_BYTES + 1024)
        + "\nComplete original tail 🧪e\u{301}";
    let request = serde_json::json!({"operation":"task_results.checkpoint",
        "command_id":"large-inline-checkpoint", "deadline_unix_ms":deadline(120_000),
        "payload":{"key":"large-inline", "expected_version":0,
            "guard_contract":"tofu.task-results.checkpoint.guard/v1", "require_parent":false,
            "value":{"task_id":"large-inline", "conv_id":"", "user_id":11,
                "status":"done", "content":content, "metadata":"{}"}}});
    let first = execute(handle, &request);
    assert_eq!(first["status"], 0, "{first}");
    assert_eq!(execute(handle, &request), first, "same command must replay one commit");
    let mut foreign_request = request.clone();
    foreign_request["command_id"] = serde_json::json!("foreign-large-checkpoint");
    foreign_request["payload"]["key"] = serde_json::json!("foreign-large");
    foreign_request["payload"]["value"]["task_id"] = serde_json::json!("foreign-large");
    foreign_request["payload"]["value"]["user_id"] = serde_json::json!(12);
    assert_eq!(execute(handle, &foreign_request)["status"], 403);
    unsafe { tofudb_embed_close(handle) };
    let handle = open_handle(&seed);
    let query = serde_json::to_vec(&serde_json::json!({"operation":"task_results.replay_get",
        "deadline_unix_ms":deadline(120_000), "payload":{"key":"large-inline",
            "user_id":11, "include_terminal_payload":true}})).unwrap();
    let mut out = vec![0_u8; content.len() + OUT_CAP];
    let written = unsafe { tofudb_embed_execute(handle, query.as_ptr(), query.len(), out.as_mut_ptr(), out.len()) };
    assert!(written > 0);
    let response: serde_json::Value = serde_json::from_slice(&out[..written as usize]).unwrap();
    assert_eq!(response["status"], 0);
    let retained = response["payload"]["content"].as_str().unwrap();
    assert_eq!(retained.len(), content.len());
    assert_eq!(Sha256::digest(retained.as_bytes()), Sha256::digest(content.as_bytes()));
    assert_eq!(response["payload"]["version"], 1);
    let foreign = execute(handle, &serde_json::json!({"operation":"task_results.replay_get",
        "deadline_unix_ms":deadline(30_000), "payload":{"key":"large-inline",
            "user_id":12, "include_terminal_payload":true}}));
    assert_eq!(foreign["status"], 0);
    assert!(foreign["payload"].is_null());
    unsafe { tofudb_embed_close(handle) };
}


#[test]
fn large_checkpoint_commits_while_other_owned_input_exceeds_the_lean_hint() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let accounting = unsafe { Arc::clone(&(*handle).input_memory) };
    let lean = crate::resource_probe::DaemonResourceBudget::from_snapshot(Default::default());
    let held_input = vec![0u8; lean.embedded_request_memory_bytes() + 1];
    let held = OwnedInputLease::claim(&accounting, held_input.capacity()).unwrap();
    let committed = execute(handle, &serde_json::json!({"operation":"task_results.checkpoint",
        "command_id":"accepted-large-checkpoint", "deadline_unix_ms":deadline(30_000),
        "payload":{"key":"accepted-large", "expected_version":0,
            "value":{"user_id":11, "content":"x".repeat(crate::embed::MAX_EMBEDDED_PAYLOAD_BYTES + 1024)}}}));
    let retained = execute(handle, &serde_json::json!({"operation":"task_results.replay_get",
        "deadline_unix_ms":deadline(30_000), "payload":{"key":"accepted-large", "user_id":11}}));
    assert_eq!(committed["status"], 0, "{committed}");
    assert_eq!(retained["status"], 0, "{retained}");
    assert!(!retained["payload"].is_null());
    drop(held);
    drop(held_input);
    wait_for_input_release(&accounting);
    unsafe { tofudb_embed_close(handle) };
}


#[test]
fn sixty_four_independent_calls_enter_one_idle_target_without_queued_admission() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    // Replace only the idle-reuse target before the handle has any callers.
    // No eager workers exist; the normal FFI submit path must create every
    // busy carrier needed to rendezvous inside actual native dispatch.
    let (sender, receiver) = crate::embed_job_queue::delivery::<Job>(1);
    unsafe {
        *(*handle).jobs.get_mut().unwrap() = Some(sender);
        (*handle).worker_receiver = receiver;
    }
    let started = std::time::Instant::now();
    let address = handle as usize;
    let calls: Vec<_> = (0..64).map(|_| std::thread::spawn(move || {
        execute(address as *mut EmbedHandle, &serde_json::json!({
            "operation": test_fault::DEMAND_BURST_OPERATION,
            "payload": {"padding":"x".repeat(256 * 1024)}, "deadline_unix_ms": deadline(20_000),
        }))
    })).collect();
    let responses: Vec<_> = calls.into_iter().map(|call| call.join().unwrap()).collect();
    let poisoned = unsafe { (*handle).poisoned.load(Ordering::Acquire) };
    let accounting = unsafe { Arc::clone(&(*handle).input_memory) };
    wait_for_input_release(&accounting);
    let peak = accounting.peak_bytes.load(Ordering::Acquire);
    unsafe { tofudb_embed_close(handle); }
    assert!(peak >= 64 * 256 * 1024);
    assert!(!poisoned);
    assert!(responses.iter().all(|response| response["error_code"] != "embedded_transport_error"));
    eprintln!("embedded-demand-burst callers=64 idle_reuse_target=1 peak_owned_input_bytes={peak} settled_ms={:.2}", started.elapsed().as_secs_f64()*1000.0);
}


#[test]
fn unstarted_worker_failure_releases_job_bytes_without_poisoning_other_calls() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    FAIL_NEXT_DEMAND_SPAWN.with(|flag| flag.set(true));
    let failed = execute(handle, &serde_json::json!({
        "operation": "system.schema_version", "payload": {},
        "deadline_unix_ms": deadline(10_000),
    }));
    let bytes_after_failure = unsafe { (&(*handle).input_memory).bytes.load(Ordering::Acquire) };
    let poisoned = unsafe { (*handle).poisoned.load(Ordering::Acquire) };
    let next = execute(handle, &serde_json::json!({
        "operation": "system.schema_version", "payload": {},
        "deadline_unix_ms": deadline(10_000),
    }));
    unsafe { tofudb_embed_close(handle); }
    assert_eq!(failed["error_code"], "embedded_transport_error");
    assert!(failed["error_message"].as_str().unwrap().contains("could not start"));
    assert_eq!(bytes_after_failure, 0);
    assert!(!poisoned);
    assert!(next["error_code"].is_null(), "{next}");
}


#[test]
fn input_allocation_failure_never_dispatches_or_poisons_the_handle() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let accounting = unsafe { Arc::clone(&(*handle).input_memory) };
    let command = serde_json::json!({"operation":"record.put",
        "command_id":"retry-unallocated-input", "deadline_unix_ms":deadline(30_000),
        "payload":{"namespace":"embed", "key":"allocation-failure", "user_id":11,
            "value":{"n":1}}});
    FAIL_NEXT_INPUT_ALLOCATION.with(|flag| flag.set(true));
    let failed = execute(handle, &command);
    assert_eq!(failed["error_code"], "embedded_transport_error");
    assert!(failed["error_message"].as_str().unwrap().contains("input allocation failed"));
    assert_eq!(accounting.bytes.load(Ordering::Acquire), 0);
    assert!(!unsafe { (*handle).poisoned.load(Ordering::Acquire) });
    let read = serde_json::json!({"operation":"record.get", "deadline_unix_ms":deadline(30_000),
        "payload":{"namespace":"embed", "key":"allocation-failure", "user_id":11}});
    let missing = execute(handle, &read);
    assert_eq!(missing["status"], 0);
    assert!(missing["payload"].is_null(), "allocation failure dispatched a durable command");
    let retried = execute(handle, &command);
    assert_eq!(retried["status"], 0, "{retried}");
    assert_eq!(execute(handle, &read)["payload"]["value"], serde_json::json!({"n":1}));
    wait_for_input_release(&accounting);
    unsafe { tofudb_embed_close(handle); }
}

#[cfg(feature = "allocator-diagnostics")]
#[test]
fn allocator_collection_preserves_two_live_authorities_receipts_and_reopen() {
    let seeds = [seed_authority(), seed_authority()];
    let handles = [open_handle(&seeds[0]), open_handle(&seeds[1])];
    let mut live = vec![0_u8; 2 * 1024 * 1024];
    for (index, byte) in live.iter_mut().enumerate() { *byte = (index % 251) as u8; }
    let live_digest = blake3::hash(&live);
    let puts: Vec<_> = (0..2).map(|index| serde_json::json!({
        "operation": "record.put",
        "payload": {"namespace": "allocator-probe", "key": "same-key", "value": {"index": index}, "user_id": 11},
        "command_id": "allocator-probe-receipt",
        "deadline_unix_ms": deadline(30_000),
    })).collect();
    let receipts: Vec<_> = (0..2).map(|index| execute(handles[index], &puts[index])).collect();
    assert!(receipts.iter().all(|reply| reply["status"] == 0));
    assert_eq!(tofudb_embed_collect_allocator(2), -1);
    for force in [0, 1] {
        assert_eq!(tofudb_embed_collect_allocator(force), 1);
        assert_eq!(blake3::hash(&live), live_digest);
        for index in 0..2 {
            let replay = execute(handles[index], &puts[index]);
            assert_eq!(replay["payload"], receipts[index]["payload"]);
        }
    }
    for handle in handles { unsafe { tofudb_embed_close(handle); } }
    drop(live);
    assert_eq!(tofudb_embed_collect_allocator(1), 1);
    for index in 0..2 {
        let handle = open_handle(&seeds[index]);
        let read = execute(handle, &serde_json::json!({
            "operation": "record.get",
            "payload": {"namespace": "allocator-probe", "key": "same-key", "user_id": 11},
            "deadline_unix_ms": deadline(30_000),
        }));
        assert_eq!(read["status"], 0);
        assert_eq!(read["payload"]["value"], serde_json::json!({"index": index}));
        assert_eq!(read["payload"]["version"], 1);
        unsafe { tofudb_embed_close(handle); }
    }
}


#[test]
fn registered_ffi_commands_wait_without_carriers_and_release_input_on_completion_or_poison() {
    struct HandleGuard(*mut EmbedHandle);
    impl Drop for HandleGuard {fn drop(&mut self){unsafe{tofudb_embed_close(self.0);}}}
    for cancel in [false,true] {
        let seed=seed_authority();let owned=HandleGuard(open_handle(&seed));
        let address=owned.0 as usize;
        let handle=unsafe{&*owned.0};let gate=handle.runtime.command_admission();
        std::thread::scope(|threads| {
            let crate::authority_admission::Admission::Entered(first)=gate.acquire(std::time::Instant::now()+Duration::from_secs(10),&mut ||true).unwrap() else {panic!("empty FIFO")};
            let calls:Vec<_>=(0..64).map(|index|threads.spawn(move||execute(address as *mut EmbedHandle,&serde_json::json!({
                "operation":"record.put","command_id":format!("queued-{index}"),"deadline_unix_ms":deadline(30_000),
                "payload":{"namespace":"ffi-queued","key":index.to_string(),"value":index}
            })))).collect();
            let end=std::time::Instant::now()+Duration::from_secs(10);
            while handle.phase_registry.active_count()!=64 && std::time::Instant::now()<end {std::thread::sleep(Duration::from_millis(1));}
            assert_eq!(handle.phase_registry.active_count(),64,"all commands must enter real phase registry");
            assert!(handle.input_memory.bytes.load(Ordering::Acquire)>0);
            if cancel {poison(handle);}drop(first);
            for call in calls {
                let response=call.join().unwrap();
                if cancel {assert_ne!(response["status"],0);} else {assert_eq!(response["status"],0,"{response}");}
            }
        });
        let end=std::time::Instant::now()+Duration::from_secs(5);
        while (handle.phase_registry.active_count()!=0 || handle.input_memory.bytes.load(Ordering::Acquire)!=0) && std::time::Instant::now()<end {std::thread::sleep(Duration::from_millis(1));}
        assert_eq!(handle.phase_registry.active_count(),0);assert_eq!(handle.input_memory.bytes.load(Ordering::Acquire),0);
        assert_eq!(gate.waiting_count(),0);
    }
}

#[test]
fn large_embedded_query_payload_has_no_private_byte_quota() {
    let seed = seed_authority();
    let handle = open_handle(&seed);
    let response = execute(handle, &serde_json::json!({"operation":"record.get",
        "deadline_unix_ms":deadline(30_000), "payload":{"namespace":"ffi",
        "key":"absent-large","padding":"x".repeat(70 * 1024 * 1024)}}));
    assert_eq!(response["status"],0);
    assert!(response["payload"].is_null());
    unsafe { tofudb_embed_close(handle) };
}
