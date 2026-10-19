//! Private callback Store for a caller-authenticated, selected SQL authority.
//! Shared Registry/OwnedLease own transitions. The host durably commits each CAS
//! on the entering thread. Close joins callbacks before their context can retire.
#[path = "generated_checkpoint_storage_abi.rs"]
mod contract;
#[path = "checkpoint_storage_store.rs"]
mod store;
use contract::*;
use crate::checkpoint_control::Registry;
use std::{collections::HashMap, ffi::c_void, panic::{catch_unwind, AssertUnwindSafe},
    sync::{atomic::{AtomicU64, Ordering}, Arc, Mutex, OnceLock}};

pub type Callback = unsafe extern "C" fn(*mut c_void, *const u8, usize, *mut u8, usize, *mut usize) -> u32;
type Joined = crate::joined_calls::Joined<Runtime, STATUS_UNKNOWN_HANDLE, STATUS_INTERNAL>;
struct Runtime {
    registry: Registry,
    port: store::RuntimePort,
    request_bytes: usize,
}
static HANDLES: OnceLock<Mutex<HashMap<u64, Arc<Joined>>>> = OnceLock::new();
static NEXT: AtomicU64 = AtomicU64::new(1);
thread_local! {
    // Host callbacks must not recursively enter control or close this handle.
    static ENTERED: std::cell::RefCell<Vec<u64>> = const { std::cell::RefCell::new(Vec::new()) };
}
fn handles() -> &'static Mutex<HashMap<u64, Arc<Joined>>> { HANDLES.get_or_init(Default::default) }
fn call(work: impl FnOnce() -> Result<(), u32>) -> u32 {
    catch_unwind(AssertUnwindSafe(work)).unwrap_or(Err(STATUS_INTERNAL)).err().unwrap_or(STATUS_OK)
}
struct Invocation(u64);
impl Invocation {
    fn enter(id: u64) -> Result<Self, u32> {
        ENTERED.with(|active| {
            let mut active = active.borrow_mut();
            if active.contains(&id) { return Err(STATUS_BUSY); }
            active.push(id);
            Ok(Self(id))
        })
    }
}
impl Drop for Invocation {
    fn drop(&mut self) { ENTERED.with(|active| { assert_eq!(active.borrow_mut().pop(), Some(self.0)); }); }
}

#[no_mangle]
pub extern "C" fn tofu_checkpoint_storage_abi_version() -> u32 { ABI_VERSION }

/// # Safety
/// Output and callback/context pointers obey the published synchronous ABI.
#[no_mangle]
pub unsafe extern "C" fn tofu_checkpoint_storage_open(tenant: u64, owner: u64,
    document_bytes: usize, identity_bytes: usize, callback: Option<Callback>,
    context: *mut c_void, handle: *mut u64) -> u32 {
    call(|| {
        if handle.is_null() { return Err(STATUS_INVALID); }
        *handle = 0;
        let callback = callback.ok_or(STATUS_INVALID)?;
        let registry = Registry::new(document_bytes).map_err(|_| STATUS_INVALID)?;
        if owner == 0 || identity_bytes == 0 || identity_bytes > document_bytes { return Err(STATUS_INVALID); }
        let request_bytes = document_bytes.checked_mul(REQUEST_DOCUMENT_SLOTS)
            .and_then(|n| identity_bytes.checked_mul(REQUEST_IDENTITY_ESCAPE_SLOTS).and_then(|i| n.checked_add(i)))
            .and_then(|n| n.checked_add(ENVELOPE_BYTES)).filter(|n| *n <= isize::MAX as usize)
            .ok_or(STATUS_INVALID)?;
        let port = store::RuntimePort::new(tenant, owner, document_bytes, identity_bytes, callback, context);
        let id = NEXT.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| n.checked_add(1)).map_err(|_| STATUS_INTERNAL)?;
        handles().lock().map_err(|_| STATUS_INTERNAL)?.insert(id, Arc::new(Joined::new(Runtime { registry, port, request_bytes })));
        *handle = id;
        Ok(())
    })
}

/// # Safety
/// All declared input/output buffers remain valid and non-overlapping until return.
#[no_mangle]
pub unsafe extern "C" fn tofu_checkpoint_storage_control(handle: u64,
    operation: *const u8, operation_length: usize, request: *const u8, request_length: usize,
    deadline_unix_ms: u64, response: *mut u8, response_capacity: usize, response_length: *mut usize) -> u32 {
    call(|| {
        if operation.is_null() || request.is_null() || response.is_null() || response_length.is_null()
            || operation_length == 0 || operation_length > 32 || response_capacity > isize::MAX as usize {
            return Err(STATUS_INVALID);
        }
        *response_length = 0;
        let _invocation = Invocation::enter(handle)?;
        let runtime = handles().lock().map_err(|_| STATUS_INTERNAL)?.get(&handle).cloned().ok_or(STATUS_UNKNOWN_HANDLE)?;
        let lease = runtime.enter()?;
        let runtime = &lease.0.value;
        if request_length == 0 || request_length > runtime.request_bytes { return Err(STATUS_INVALID); }
        let operation = std::str::from_utf8(std::slice::from_raw_parts(operation, operation_length)).map_err(|_| STATUS_INVALID)?;
        let request = std::slice::from_raw_parts(request, request_length);
        let required = runtime.registry.response_bound(operation, request).map_err(|_| STATUS_INVALID)?;
        if response_capacity < required { *response_length = required; return Err(STATUS_BUFFER_SMALL); }
        let bytes = runtime.registry.execute(&runtime.port, operation, request, deadline_unix_ms)
            .map_err(|e| if e.kind() == std::io::ErrorKind::TimedOut { STATUS_EXPIRED } else { STATUS_INVALID })?;
        if bytes.len() > response_capacity { return Err(STATUS_INTERNAL); }
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), response, bytes.len());
        *response_length = bytes.len();
        Ok(())
    })
}

#[no_mangle]
pub extern "C" fn tofu_checkpoint_storage_close(handle: u64) -> u32 {
    call(|| {
        let _invocation = Invocation::enter(handle)?;
        let runtime = handles().lock().map_err(|_| STATUS_INTERNAL)?.remove(&handle).ok_or(STATUS_UNKNOWN_HANDLE)?;
        runtime.close()
    })
}

/// Validate observations while the caller holds the actual SQL transaction locks.
/// # Safety
/// Input buffers are valid for their declared lengths. The caller retains this
/// library through its joined outer control call; no handle or callback is entered.
#[no_mangle]
pub unsafe extern "C" fn tofu_checkpoint_storage_validate_handoff(tenant: u64, owner: u64,
    document_bytes: usize, identity_bytes: usize, request: *const u8, request_length: usize,
    observations: *const u8, observations_length: usize) -> u32 {
    call(|| {
        let maximum = document_bytes.checked_mul(REQUEST_DOCUMENT_SLOTS)
            .and_then(|n| identity_bytes.checked_mul(REQUEST_IDENTITY_ESCAPE_SLOTS).and_then(|i| n.checked_add(i)))
            .and_then(|n| n.checked_add(ENVELOPE_BYTES)).filter(|n| *n <= isize::MAX as usize).ok_or(STATUS_INVALID)?;
        if request.is_null() || observations.is_null() || document_bytes == 0 || identity_bytes == 0
            || identity_bytes > document_bytes || request_length == 0 || request_length > maximum
            || observations_length == 0 || observations_length > maximum { return Err(STATUS_INVALID); }
        store::validate(tenant, owner, document_bytes, identity_bytes,
            std::slice::from_raw_parts(request, request_length), std::slice::from_raw_parts(observations, observations_length))
            .map_err(|e| match e {
                crate::checkpoint_journal::Error::Invalid => STATUS_INVALID,
                crate::checkpoint_journal::Error::Conflict => STATUS_CONFLICT,
                crate::checkpoint_journal::Error::Ineligible => STATUS_INELIGIBLE,
                crate::checkpoint_journal::Error::Oversize => STATUS_OVERSIZE,
                _ => STATUS_INTERNAL,
            })
    })
}

/// Execute Rust Goal policy using IO on an already-entered SQL transaction.
/// # Safety
/// The outer joined control retains this code; callback/context and all buffers
/// remain valid through this synchronous call, including a SQL writer thread.
#[no_mangle]
pub unsafe extern "C" fn tofu_checkpoint_storage_goal_transaction(tenant:u64,owner:u64,
    document_bytes:usize,identity_bytes:usize,request:*const u8,request_length:usize,
    callback:Option<Callback>,context:*mut c_void,response:*mut u8,response_capacity:usize,response_length:*mut usize)->u32{
    call(||{
        if response_length.is_null(){return Err(STATUS_INVALID);}*response_length=0;
        let maximum=document_bytes.checked_mul(REQUEST_DOCUMENT_SLOTS)
            .and_then(|n|identity_bytes.checked_mul(REQUEST_IDENTITY_ESCAPE_SLOTS).and_then(|i|n.checked_add(i)))
            .and_then(|n|n.checked_add(ENVELOPE_BYTES)).filter(|n|*n<=isize::MAX as usize).ok_or(STATUS_INVALID)?;
        if document_bytes==0 || identity_bytes==0 || identity_bytes>document_bytes || request.is_null()
            || response.is_null() || request_length==0 || request_length>maximum || response_capacity>isize::MAX as usize{return Err(STATUS_INVALID);}
        // Conservative output preflight before the first observation or mutation.
        if response_capacity<document_bytes{*response_length=document_bytes;return Err(STATUS_BUFFER_SMALL);}
        let bytes=store::goal_transaction(tenant,owner,document_bytes,identity_bytes,std::slice::from_raw_parts(request,request_length),
            callback.ok_or(STATUS_INVALID)?,context).map_err(|error|match error{
                crate::checkpoint_journal::Error::Invalid=>STATUS_INVALID,crate::checkpoint_journal::Error::Conflict=>STATUS_CONFLICT,
                crate::checkpoint_journal::Error::Ineligible=>STATUS_INELIGIBLE,crate::checkpoint_journal::Error::Oversize=>STATUS_OVERSIZE,_=>STATUS_INTERNAL})?;
        std::ptr::copy_nonoverlapping(bytes.as_ptr(),response,bytes.len());*response_length=bytes.len();Ok(())
    })
}

#[cfg(test)]
#[path = "checkpoint_storage_abi_tests.rs"]
mod tests;
