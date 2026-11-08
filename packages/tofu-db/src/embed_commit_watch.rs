//! Cancellable embedded commit watches borrow the same authority condition as
//! daemon clients. Tokens own only a cancellation flag and that condition Arc;
//! they retain no durable state, worker thread, authority lock or notification.
use super::{EmbedHandle, fail, write_c_buffer};
use crate::{protocol::Request, semantic::AuthenticatedScope, worker_control::WorkerControl};
use serde::Deserialize;
use std::{sync::{Arc, atomic::{AtomicBool, Ordering}}, panic::{catch_unwind, AssertUnwindSafe}, time::{SystemTime, UNIX_EPOCH}};

pub struct WatchToken {
    control: Arc<WorkerControl>,
    scope: AuthenticatedScope,
    cancelled: AtomicBool,
    poisoned: Arc<AtomicBool>,
}

fn contain_panic(payload: Box<dyn std::any::Any + Send>, poisoned: &AtomicBool) {
    poisoned.store(true, Ordering::Release);
    crate::engine_log::error("panic_containment", &format!(
        "tofu-db engine panic contained in embedded commit watch: {}", super::panic_message(payload)));
}

/// # Safety
/// `handle` stays live until creation returns. A returned token is freed once,
/// after execute and cancel calls have left; cancellation may race execution.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_watch_open(handle: *mut EmbedHandle) -> *mut WatchToken {
    if handle.is_null() { return std::ptr::null_mut(); }
    let handle = unsafe { &*handle };
    match catch_unwind(AssertUnwindSafe(|| {
        if handle.poisoned.load(Ordering::Acquire) { return std::ptr::null_mut(); }
        match handle.runtime.commit_watch_owner() {
            Ok((control, scope)) => Box::into_raw(Box::new(WatchToken { control, scope, cancelled: AtomicBool::new(false), poisoned: handle.poisoned.clone() })),
            Err(_) => std::ptr::null_mut(),
        }
    })) {
        Ok(token) => token,
        Err(payload) => { contain_panic(payload, &handle.poisoned); std::ptr::null_mut() }
    }
}

/// # Safety
/// `token` is live and cannot be freed until this call returns.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_watch_cancel(token: *mut WatchToken) {
    if token.is_null() { return; }
    let token = unsafe { &*token };
    if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
        token.cancelled.store(true, Ordering::Release);
        token.control.notify_cancel();
    })) { contain_panic(payload, &token.poisoned); }
}

/// # Safety
/// `token` is freed exactly once, after its execute/cancel borrowers join.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_watch_free(token: *mut WatchToken) {
    if token.is_null() { return; }
    let poisoned = unsafe { &*token }.poisoned.clone();
    if let Err(payload) = catch_unwind(AssertUnwindSafe(|| drop(unsafe { Box::from_raw(token) }))) {
        contain_panic(payload, &poisoned);
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WatchRequest {
    operation: String,
    payload: serde_json::Value,
    deadline_unix_ms: u64,
}

/// # Safety
/// The token is live for the call; request/output point to readable/writable
/// buffers of the supplied lengths. Only cancellation may run concurrently.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_watch_execute(token: *mut WatchToken, request: *const u8,
    request_len: usize, out: *mut u8, out_cap: usize) -> isize {
    if token.is_null() || request.is_null() { return fail(out, out_cap, "missing embedded watch token/request"); }
    let outcome = catch_unwind(AssertUnwindSafe(|| -> std::io::Result<Vec<u8>> {
        let token = unsafe { &*token };
        if token.poisoned.load(Ordering::Acquire) { return Err(std::io::Error::other("embedded runtime is poisoned")); }
        let input: WatchRequest = serde_json::from_slice(unsafe { std::slice::from_raw_parts(request, request_len) })?;
        let request = Request { correlation_id: *uuid::Uuid::new_v4().as_bytes(),
            operation: input.operation, payload: serde_json::to_vec(&input.payload)?,
            deadline_unix_ms: input.deadline_unix_ms, command_id: None,
            owner_id: token.scope.owner_id, tenant_id: token.scope.tenant_id,
            schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION };
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64;
        let response = token.control.respond_cancelled(&request, request.schema_id, now, token.scope, Some(&token.cancelled));
        serde_json::to_vec(&serde_json::json!({
            "status": response.status, "error_code": response.error_code,
            "error_message": response.error_message, "retryable": response.retryable,
            "payload": if response.status == 0 { serde_json::from_slice::<serde_json::Value>(&response.payload)? } else { serde_json::Value::Null },
        })).map_err(std::io::Error::other)
    }));
    match outcome {
        Ok(Ok(bytes)) => write_c_buffer(out, out_cap, &bytes),
        Ok(Err(error)) => fail(out, out_cap, &error.to_string()),
        Err(payload) => {
            contain_panic(payload, &unsafe { &*token }.poisoned);
            fail(out, out_cap, "embedded commit watch panicked")
        },
    }
}
