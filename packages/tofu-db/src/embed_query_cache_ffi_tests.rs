// Included in embed_ffi::tests to use the actual C ABI ownership fixtures.
#[test]
fn query_result_cache_ffi_capacity_and_contained_fault_keep_original_boundaries() {
    let seed=seed_authority();let handle=open_handle(&seed);
    let put=serde_json::json!({"operation":"record.put","payload":{"namespace":"cached-ffi","key":"k","value":{"v":7}},"command_id":"seed-cache","deadline_unix_ms":deadline(30000)});
    assert_eq!(execute(handle,&put)["status"],0);
    let get=serde_json::json!({"operation":"record.get","payload":{"namespace":"cached-ffi","key":"k"},"deadline_unix_ms":deadline(30000)});
    let expected=execute(handle,&get);assert_eq!(expected["payload"]["value"],serde_json::json!({"v":7}));
    let encoded=serde_json::to_vec(&get).unwrap();let mut short=[91u8;2];
    let needed=unsafe{tofudb_embed_execute(handle,encoded.as_ptr(),encoded.len(),short.as_mut_ptr(),short.len())};
    assert!(needed < -2);assert_eq!(short,[91,91]);assert_eq!(execute(handle,&get),expected);
    let fault=serde_json::json!({"operation":test_fault::PANIC_OPERATION,"payload":{},"deadline_unix_ms":deadline(30000)});
    assert_eq!(execute(handle,&fault)["error_code"],"embedded_transport_error");
    assert_eq!(execute(handle,&get)["error_code"],"embedded_transport_error");
    unsafe{tofudb_embed_close(handle)};
}

#[test]
fn query_result_cache_ffi_warm_result_cannot_bypass_watchdog_poison() {
    let seed=seed_authority();let handle=open_handle(&seed);
    let get=serde_json::json!({"operation":"record.get","payload":{"namespace":"cached-watchdog","key":"absent"},"deadline_unix_ms":deadline(30000)});
    assert_eq!(execute(handle,&get)["status"],0);
    assert_eq!(execute(handle,&get)["status"],0);
    WATCHDOG_TEST_OVERRIDE_MS.with(|budget| budget.set(200));
    let fault=serde_json::json!({"operation":test_fault::PARK_OPERATION,"payload":{},"deadline_unix_ms":deadline(60000)});
    let result=execute(handle,&fault);
    WATCHDOG_TEST_OVERRIDE_MS.with(|budget| budget.set(0));
    assert_eq!(result["error_code"],"embedded_transport_error");
    assert!(result["error_message"].as_str().unwrap().contains("watchdog"));
    assert_eq!(execute(handle,&get)["error_code"],"embedded_transport_error");
    unsafe{tofudb_embed_close(handle)};
}
