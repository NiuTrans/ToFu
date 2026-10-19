//! Exercise the actual exported ABI, including callback reentry and joined close.
use super::*;
use serde_json::json;
use std::sync::{atomic::AtomicUsize, mpsc, Condvar};
use std::time::Duration;

struct Host {
    calls: AtomicUsize,
    block: Mutex<bool>,
    changed: Condvar,
    entered: mpsc::Sender<()>,
    handle: AtomicU64,
    malformed: bool,
}
unsafe extern "C" fn callback(context: *mut c_void, request: *const u8, length: usize,
    output: *mut u8, capacity: usize, written: *mut usize) -> u32 {
    let host = &*(context as *const Host);
    host.calls.fetch_add(1, Ordering::SeqCst);
    let handle = host.handle.load(Ordering::SeqCst);
    if handle != 0 {
        assert_eq!(tofu_checkpoint_storage_close(handle), STATUS_BUSY);
    }
    host.entered.send(()).unwrap();
    let mut blocked = host.block.lock().unwrap();
    while *blocked { blocked = host.changed.wait(blocked).unwrap(); }
    drop(blocked);
    let request: serde_json::Value = serde_json::from_slice(std::slice::from_raw_parts(request, length)).unwrap();
    let response = if host.malformed { json!({"ok":true,"version":1,"unknown":true}) }
        else if request["operation"] == "header" { json!({"ok":true,"version":0}) }
        else { json!({"ok":true,"version":1}) };
    let bytes = serde_json::to_vec(&response).unwrap();
    assert!(bytes.len() <= capacity);
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), output, bytes.len());
    *written = bytes.len();
    STATUS_OK
}
fn host(block: bool, malformed: bool) -> (Box<Host>, mpsc::Receiver<()>) {
    let (entered, receiver) = mpsc::channel();
    (Box::new(Host { calls: AtomicUsize::new(0), block: Mutex::new(block), changed: Condvar::new(),
        entered, handle: AtomicU64::new(0), malformed }), receiver)
}
fn open(host: &Host) -> u64 {
    let mut handle = 0;
    assert_eq!(unsafe { tofu_checkpoint_storage_open(3, 7, 8192, 128, Some(callback),
        host as *const Host as *mut c_void, &mut handle) }, STATUS_OK);
    host.handle.store(handle, Ordering::SeqCst);
    handle
}
fn create(handle: u64, capacity: usize, owner: u64, deadline: u64) -> (u32, serde_json::Value) {
    let request = serde_json::to_vec(&json!({"format":"tofu.native-checkpoint-control/v1","parameters":{
        "owner_user_id":owner,"task_id":"task","epoch":"epoch","binding":"binding","token":"token",
        "header_bytes":4096,"snapshot_bytes":4096,"identity_bytes":128}})).unwrap();
    let mut output = vec![0_u8;capacity]; let mut length = 0;
    let status = unsafe { tofu_checkpoint_storage_control(handle, b"create".as_ptr(), 6, request.as_ptr(), request.len(),
        deadline, output.as_mut_ptr(), output.len(), &mut length) };
    let response = if status == STATUS_OK { serde_json::from_slice(&output[..length]).unwrap() } else { serde_json::Value::Null };
    (status, response)
}

#[test]
fn output_scope_and_deadline_refusals_precede_host_callbacks() {
    let (host, _received) = host(false, false); let handle = open(&host);
    assert_eq!(create(handle, 1, 7, u64::MAX).0, STATUS_BUFFER_SMALL);
    assert_eq!(create(handle, 8192, 7, 1).0, STATUS_EXPIRED);
    assert_eq!(create(handle, 8192, 8, u64::MAX).1["accepted"], false);
    assert_eq!(host.calls.load(Ordering::SeqCst), 0);
    assert_eq!(tofu_checkpoint_storage_close(handle), STATUS_OK);
    assert_eq!(create(handle, 8192, 7, u64::MAX).0, STATUS_UNKNOWN_HANDLE);
}

#[test]
fn malformed_host_acknowledgement_never_creates_a_capability() {
    let (host, _received) = host(false, true); let handle = open(&host);
    let response = create(handle, 8192, 7, u64::MAX);
    assert_eq!(response.0, STATUS_OK);
    assert_eq!(response.1["accepted"], false);
    assert_eq!(response.1["error"], "storage");
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
    assert_eq!(handles().lock().unwrap()[&handle].value.registry.live_capabilities().unwrap(), 0);
    assert_eq!(tofu_checkpoint_storage_close(handle), STATUS_OK);
}

#[test]
fn exported_close_withdraws_and_joins_accepted_callback_and_reentry_refuses() {
    let (host, received) = host(true, false); let handle = open(&host);
    let active = std::thread::spawn(move || create(handle, 8192, 7, u64::MAX));
    received.recv_timeout(Duration::from_secs(5)).unwrap();
    let (done, retired) = mpsc::channel();
    let closing = std::thread::spawn(move || { done.send(tofu_checkpoint_storage_close(handle)).unwrap(); });
    let started = std::time::Instant::now();
    while handles().lock().unwrap().contains_key(&handle) {
        assert!(started.elapsed() < Duration::from_secs(5));
        std::thread::sleep(Duration::from_millis(1));
    }
    assert_eq!(create(handle, 8192, 7, u64::MAX).0, STATUS_UNKNOWN_HANDLE);
    assert!(matches!(retired.try_recv(), Err(mpsc::TryRecvError::Empty)));
    *host.block.lock().unwrap() = false; host.changed.notify_all();
    assert_eq!(active.join().unwrap().1["accepted"], true);
    assert_eq!(retired.recv_timeout(Duration::from_secs(5)).unwrap(), STATUS_OK);
    closing.join().unwrap();
    assert_eq!(host.calls.load(Ordering::SeqCst), 2);
}

#[test]
fn goal_transaction_preflight_and_scope_refuse_before_any_host_io(){
    let (host,_receiver)=host(false,false);
    let value=json!({"format":FORMAT,"operation":"goal_claim","tenant_id":3,"owner_user_id":7,
        "header_bytes":4096,"snapshot_bytes":4096,"evidence_bytes":4096,"authorization":{},"actors":[]});
    for (owner,capacity,expected) in [(7,1,STATUS_BUFFER_SMALL),(8,4096,STATUS_INVALID),(7,4096,STATUS_INVALID)] {
        let bytes=serde_json::to_vec(&value).unwrap();let mut output=vec![0_u8;capacity];let mut length=123;
        let status=unsafe{tofu_checkpoint_storage_goal_transaction(3,owner,4096,128,bytes.as_ptr(),bytes.len(),
            Some(callback),host.as_ref() as *const Host as *mut c_void,output.as_mut_ptr(),output.len(),&mut length)};
        assert_eq!(status,expected);
        assert_eq!(length,if expected==STATUS_BUFFER_SMALL{4096}else{0});
        assert_eq!(host.calls.load(Ordering::SeqCst),0);
    }
}
