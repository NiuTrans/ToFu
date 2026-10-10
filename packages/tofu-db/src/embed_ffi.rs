//! C ABI for the embedded in-process transport (`embed.rs`).
//!
//! Four entry points, one owning handle, caller-owned buffers:
//!
//! * `tofudb_embed_open` — mirrors `tofu-db serve` startup for an existing
//!   authority (no listener); returns an opaque handle or null (error text
//!   in the caller's buffer). The caller's secret buffer is zeroized by the
//!   callee on every path.
//! * `tofudb_embed_execute` — one storage.v2 operation through
//!   `server::dispatch_one_request` (identical semantics to the framed
//!   daemon). Request is one JSON object
//!   `{operation, payload, command_id?, deadline_unix_ms}`; the response
//!   JSON `{status, error_code, error_message, retryable, payload}` is
//!   written into the caller's buffer. Returns the response length, or the
//!   negated required capacity when the buffer is too small, or -1 on
//!   handle/protocol failure (an error envelope is still written).
//! * `tofudb_embed_close` — graceful stop (drivers joined, WAL windows
//!   retired) and handle destruction; null-safe, exactly-once.
//!
//! Threading: the handle is `Send + Sync`; concurrent callers use owned,
//! demand-created workers. Only idle reuse scales from launch capacity.
//! Owned input bytes are observed without an aggregate admission quota.
//! Preparation and reads run concurrently; commits use the authority sequencer.
//! The caller must quiesce all execute calls before close/abandon frees a handle;
//! the Python wrapper enforces this without serializing active executions.
//!
//! Hardening contract (2026-09-22):
//!
//! * Panic containment: every entry point runs under
//!   `std::panic::catch_unwind`. A panic crossing the FFI boundary is
//!   undefined behavior for the caller, so it is contained, reported as the
//!   standard `embedded_transport_error` envelope, and mirrored to stderr
//!   as one `{"source":"tofu-db","target":"panic_containment",...}` line.
//!   A panic inside open yields no handle; a panic inside execute or a
//!   watchdog timeout POISONS the handle: later executes fail fast with the
//!   same envelope, and close/abandon free the handle (and, once the worker
//!   releases it, the runtime allocation) without running any engine
//!   operation on the unknown-state engine.
//! * Execute watchdog: shared dispatch runs on demand-created owned workers; the FFI caller waits with a bounded budget (request
//!   `deadline_unix_ms` + [`WATCHDOG_GRACE`], floored at
//!   [`WATCHDOG_FLOOR`], capped at [`WATCHDOG_MAX_BUDGET`]). A wedged engine
//!   (deadlock, not crash) can therefore never hang the host's calling
//!   thread forever. The abandoned worker is detached (never joined in the
//!   FFI path) and never touches the caller's response buffer: results
//!   travel back through a channel, so a late worker result is dropped with
//!   its channel, never written to freed memory.

use std::ffi::{c_char, CStr};
use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use zeroize::Zeroize;

#[cfg(feature = "native-runtime-checkpoints")]
#[path = "embedded_checkpoint_control.rs"]
mod checkpoint;

#[path = "embed_reply.rs"]
mod reply;
#[path = "embed_envelope.rs"]
mod envelope;
use crate::embed::EmbeddedRuntime;
use crate::embed_job_queue::{JobReceiver, JobSender};

/// Grace added to the request's own remaining deadline when deriving the
/// watchdog budget: the engine answers `deadline_elapsed` at the deadline,
/// so a healthy request needs only this much slack beyond it.
const WATCHDOG_GRACE: Duration = Duration::from_secs(30);
/// Lower bound of the watchdog budget, covering requests whose deadline is
/// already near or past (the engine may still answer them promptly).
const WATCHDOG_FLOOR: Duration = Duration::from_secs(120);
/// Upper bound of the watchdog budget: deadlines are host-chosen and this
/// deployment clamps RPC deadlines to 120 s, so nothing legitimate comes
/// near it; it exists so a pathological far-future deadline still leaves
/// the wait bounded.
const WATCHDOG_MAX_BUDGET: Duration = Duration::from_secs(3600);
/// FFI request-envelope headroom above the payload cap: the JSON envelope
/// carries the operation/command id/deadline fields and JSON punctuation
/// around the payload text. Raw payload admission and the encoded dispatch
/// both enforce the wire byte cap; the Value API uses capped serialization.
const MAX_EMBEDDED_ENVELOPE_BYTES: usize = 64 * 1024;

fn watchdog_budget(deadline_unix_ms: u64) -> Duration {
    #[cfg(test)]
    {
        let override_ms = WATCHDOG_TEST_OVERRIDE_MS.with(std::cell::Cell::get);
        if override_ms > 0 {
            return Duration::from_millis(override_ms);
        }
    }
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .and_then(|elapsed| u64::try_from(elapsed.as_millis()).ok())
        .unwrap_or(0);
    let remaining = Duration::from_millis(deadline_unix_ms.saturating_sub(now_ms));
    (remaining + WATCHDOG_GRACE).clamp(WATCHDOG_FLOOR, WATCHDOG_MAX_BUDGET)
}

// execute_inner reads the test budget on its synchronous caller thread before
// enqueueing work. A short-budget fault test must never affect another caller.
#[cfg(test)]
thread_local! {
    static WATCHDOG_TEST_OVERRIDE_MS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

struct Job {
    #[cfg(feature = "native-runtime-checkpoints")]
    checkpoint: Option<Arc<checkpoint::Registry>>,
    enqueued_at: Option<std::time::Instant>,
    operation: String,
    payload: Vec<u8>,
    command_id: Option<String>,
    deadline_unix_ms: u64,
    respond: std::sync::mpsc::Sender<JobOutcome>,
    _memory: OwnedInputLease,
}

enum JobOutcome {
    Responded(io::Result<Vec<u8>>),
    Panicked(String),
    Profiled(Box<TimedJobOutcome>),
}

/// Only owned data crosses the watchdog boundary. The optional timestamp is
/// set immediately before send and consumed after recv, never by a late worker
/// through a caller buffer. Diagnostics off performs no timestamp clock read.
struct TimedJobOutcome {
    outcome: JobOutcome,
    ready_at: Option<std::time::Instant>,
}

impl JobOutcome {
    fn timestamped(outcome: Self) -> Self {
        if !crate::request_profile::enabled() { return outcome; }
        let mut reply = Box::new(TimedJobOutcome { outcome, ready_at: None });
        reply.ready_at = Some(std::time::Instant::now());
        Self::Profiled(reply)
    }

    fn receive(self) -> Result<io::Result<Vec<u8>>, String> {
        match self {
            Self::Responded(result) => Ok(result),
            Self::Panicked(message) => Err(message),
            Self::Profiled(reply) => reply.receive().receive(),
        }
    }
}

impl TimedJobOutcome {
    fn receive(self) -> JobOutcome {
        if let Some(start) = self.ready_at {
            crate::fsync_stats::note_stage_elapsed(
                crate::fsync_stats::STAGE_EMBEDDED_REPLY_HANDOFF,
                start.elapsed().as_nanos() as u64,
            );
        }
        self.outcome
    }
}

/// The owning FFI handle. `runtime` is shared with the execute worker so a
/// wedged worker can never hold a reference into freed memory; the runtime
/// is only dropped when the last Arc goes away.
pub struct EmbedHandle {
    #[cfg(feature = "native-runtime-checkpoints")]
    checkpoints: std::sync::OnceLock<Arc<checkpoint::Registry>>,
    runtime: Arc<EmbeddedRuntime>,
    poisoned: Arc<AtomicBool>,
    input_memory: Arc<InputMemoryAccounting>,
    jobs: Mutex<Option<JobSender<Job>>>,
    worker_receiver: JobReceiver<Job>,
}

/// Observe the actual owned encoded payload; decoded values and the caller's
/// borrowed envelope are distinct allocations. No estimate is a request gate.
#[derive(Default)]
struct InputMemoryAccounting {
    bytes: std::sync::atomic::AtomicUsize,
    peak_bytes: std::sync::atomic::AtomicUsize,
}
struct OwnedInputLease {
    bytes: usize,
    accounting: Arc<InputMemoryAccounting>,
}
impl OwnedInputLease {
    fn claim(accounting: &Arc<InputMemoryAccounting>, bytes: usize) -> io::Result<Self> {
        let previous = accounting.bytes.fetch_update(Ordering::AcqRel, Ordering::Acquire,
            |current| current.checked_add(bytes))
            .map_err(|_| io::Error::other("embedded input byte accounting overflow"))?;
        accounting.peak_bytes.fetch_max(previous + bytes, Ordering::Relaxed);
        Ok(Self { bytes, accounting: Arc::clone(accounting) })
    }
}
impl Drop for OwnedInputLease {
    fn drop(&mut self) {
        self.accounting.bytes.fetch_sub(self.bytes, Ordering::AcqRel);
    }
}

#[cfg(test)]
thread_local! {
    static FAIL_NEXT_DEMAND_SPAWN: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
    static FAIL_NEXT_INPUT_ALLOCATION: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

fn spawn_demand_worker(handle: &EmbedHandle, job: Job) -> io::Result<std::thread::JoinHandle<()>> {
    #[cfg(test)]
    if FAIL_NEXT_DEMAND_SPAWN.with(|flag| flag.replace(false)) {
        return Err(io::Error::other("injected demand worker creation failure"));
    }
    let runtime = Arc::clone(&handle.runtime);
    let jobs = handle.worker_receiver.clone();
    let poisoned = Arc::clone(&handle.poisoned);
    std::thread::Builder::new()
        .name("tofu-db-embed-demand".to_owned())
        .stack_size(handle.runtime.resource_budget.connection_stack_bytes)
        .spawn(move || run_worker(runtime, jobs, poisoned, Some(job)))
}

fn run_worker(
    runtime: Arc<EmbeddedRuntime>,
    jobs: JobReceiver<Job>,
    poisoned: Arc<AtomicBool>,
    mut first_job: Option<Job>,
) {
    loop {
        let job = match first_job.take().map(Ok).unwrap_or_else(|| jobs.recv()) {
            Err(crate::embed_job_queue::ReceiveError::Poisoned) => {
                poisoned.store(true, Ordering::Release);
                break;
            }
            outcome => outcome,
        };
        let Ok(job) = job else {
            break;
        };
        let Job {
            #[cfg(feature = "native-runtime-checkpoints")]
            checkpoint,
            enqueued_at,
            operation,
            payload,
            command_id,
            deadline_unix_ms,
            respond,
            _memory,
        } = job;
        if poisoned.load(Ordering::Acquire) {
            let _ = respond.send(JobOutcome::timestamped(JobOutcome::Responded(Err(io::Error::other(
                "embedded handle is poisoned",
            )))));
            continue;
        }
        let queue_nanos = enqueued_at.map(|start| start.elapsed().as_nanos() as u64);
        if let Some(nanos) = queue_nanos {
            crate::fsync_stats::note_stage_elapsed(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_WAIT, nanos);
        }
        let _queue_profile = crate::request_profile::QueueContext::with_reply_reuse(queue_nanos, false);
        let execute = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WORKER_EXECUTE);
        let outcome = match catch_unwind(AssertUnwindSafe(|| {
            #[cfg(feature = "native-runtime-checkpoints")]
            if let Some(registry) = checkpoint {
                return registry.execute(runtime.clone(), &operation, &payload, deadline_unix_ms);
            }
            runtime.execute_encoded_json_with_alive(operation, payload, command_id, deadline_unix_ms,
                &mut || !poisoned.load(Ordering::Acquire))
        })) {
            Ok(result) => JobOutcome::Responded(result),
            Err(payload) => {
                poisoned.store(true, Ordering::Release);
                runtime.disarm_drop();
                JobOutcome::Panicked(panic_message(payload))
            }
        };
        drop(execute);
        // The allocator experiment also tests a nonblocking completion hint.
        // It leaves reservations, poison and completed reply ownership intact.
        let _ = jobs.prepare_for_next_job();
        // A late result only touches an owned channel, never the caller buffer.
        let _deliver = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_RESPONSE_DELIVER);
        let _ = respond.send(JobOutcome::timestamped(outcome));
    }
}

fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    let payload = match payload.downcast::<String>() {
        Ok(message) => return *message,
        Err(payload) => payload,
    };
    match payload.downcast::<&'static str>() {
        Ok(message) => (*message).to_owned(),
        Err(_) => "non-string panic payload".to_owned(),
    }
}

fn write_c_buffer(out: *mut u8, out_cap: usize, bytes: &[u8]) -> isize {
    if out.is_null() || out_cap < bytes.len() {
        return -(bytes.len() as isize);
    }
    // SAFETY: the caller guarantees `out` is writable for `out_cap` bytes
    // and `bytes.len() <= out_cap` per the check above.
    unsafe { std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len()) };
    bytes.len() as isize
}

fn fail(out: *mut u8, out_cap: usize, message: &str) -> isize {
    let payload = serde_json::json!({
        "status": -1,
        "error_code": "embedded_transport_error",
        "error_message": message,
        "retryable": false,
        "payload": null,
    });
    write_c_buffer(out, out_cap, payload.to_string().as_bytes())
}

fn read_c_path(pointer: *const c_char) -> std::io::Result<String> {
    if pointer.is_null() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "path pointer is null",
        ));
    }
    // SAFETY: the caller guarantees a live NUL-terminated string.
    let text = unsafe { CStr::from_ptr(pointer) }
        .to_str()
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error.to_string()))?;
    if text.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "path must be nonempty",
        ));
    }
    Ok(text.to_owned())
}

/// Marks the handle poisoned (idempotent): later executes fail fast, and
/// close/abandon free the handle without engine operations.
fn poison(handle: &EmbedHandle) {
    handle.poisoned.store(true, Ordering::Release);
    handle.runtime.disarm_drop();
}

/// Frees one handle exactly once. A healthy handle runs its graceful or
/// ungraceful stop; a poisoned handle skips every engine operation (the
/// runtime's Drop is disarmed — see `embed.rs`) and only the allocation is
/// released. The execute worker is signaled by dropping its job channel and
/// is never joined (FFI-path contract).
fn teardown(handle: *mut EmbedHandle, graceful: bool) {
    if handle.is_null() {
        return;
    }
    // SAFETY: the caller passes the live handle exactly once.
    let handle = unsafe { Box::from_raw(handle) };
    // Stop taking jobs first: the worker exits after its current job (or
    // never, when wedged — it is detached by contract and only ever holds
    // an Arc, so teardown itself can never block on it).
    if let Ok(mut jobs) = handle.jobs.lock() {
        jobs.take();
    }
    if handle.poisoned.load(Ordering::Relaxed) {
        // Poisoned: engine state is unknown. Disarm the runtime Drop so the
        // allocation is freed without a graceful stop that could wedge or
        // re-panic. A wedged worker still holds one Arc; that bounded leak
        // (one runtime per poisoned handle) is the documented cost of never
        // joining the abandoned worker.
        handle.runtime.disarm_drop();
        return;
    }
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        if graceful {
            handle.runtime.close()
        } else {
            handle.runtime.close_ungraceful()
        }
    }));
    if let Err(payload) = outcome {
        crate::engine_log::error(
            "panic_containment",
            &format!(
                "tofu-db engine panic contained in embedded close: {}",
                panic_message(payload)
            ),
        );
        // The stop may not have completed; do not run it again on Drop.
        handle.runtime.disarm_drop();
    }
}

/// # Safety
/// `data_dir`/`projection_dir` are NUL-terminated UTF-8 paths owned by the
/// caller (`projection_dir` may be null); `secret` points to `secret_len`
/// readable AND writable bytes (consumed and zeroized by the callee);
/// `error` is a writable buffer of `error_cap`. The returned handle must be
/// destroyed exactly once with `tofudb_embed_close`.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_open(
    data_dir: *const c_char,
    owner_id: u64,
    tenant_id: i64,
    projection_dir: *const c_char,
    secret: *mut u8,
    secret_len: usize,
    error: *mut u8,
    error_cap: usize,
) -> *mut EmbedHandle {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        open_inner(data_dir, owner_id, tenant_id, projection_dir, secret, secret_len)
    }));
    // The caller's secret buffer is zeroized on every path, panic included.
    if !secret.is_null() && secret_len > 0 {
        // SAFETY: caller guarantees `secret_len` writable bytes.
        unsafe { std::slice::from_raw_parts_mut(secret, secret_len) }.zeroize();
    }
    match outcome {
        Ok(Ok(runtime)) => {
            let runtime = Arc::new(runtime);
            let budget = runtime.resource_budget;
            let idle_reuse_target = budget.snapshot.logical_cpus.unwrap_or(1)
                .saturating_mul(2).min(budget.maximum_connections).max(1);
            let (sender, receiver) = crate::embed_job_queue::delivery::<Job>(idle_reuse_target);
            let poisoned = Arc::new(AtomicBool::new(false));
            Box::into_raw(Box::new(EmbedHandle {
                #[cfg(feature = "native-runtime-checkpoints")]
                checkpoints: std::sync::OnceLock::new(),
                runtime, poisoned, jobs: Mutex::new(Some(sender)), worker_receiver: receiver,
                input_memory: Arc::default(),
            }))
        }

        Ok(Err(error_value)) => {
            write_open_error(error, error_cap, &error_value.to_string());
            std::ptr::null_mut()
        }
        Err(payload) => {
            // A contained panic never returns a live handle.
            let message = panic_message(payload);
            crate::engine_log::error(
                "panic_containment",
                &format!("tofu-db engine panic contained in embedded open: {message}"),
            );
            write_open_error(
                error,
                error_cap,
                &format!("tofu-db engine panic contained in embedded open: {message}"),
            );
            std::ptr::null_mut()
        }
    }
}

/// Actual linked Rust allocation strategy: 0 = caller/default, 1 = mimalloc.
/// Read-only build metadata; no handle, allocation, storage or process override.
#[no_mangle]
pub extern "C" fn tofudb_embed_allocator_kind() -> u32 {
    u32::from(cfg!(feature = "embedded-mimalloc"))
}

/// Experimental allocator attribution only; not exported by normal builds.
/// Calls this library's linked allocator, without touching an authority handle,
/// changing allocator options, or collecting Python/host allocator instances.
/// `force`: 0 = ordinary collection, 1 = aggressive collection; other values
/// return -1 without collecting. Returns 1 after invocation, 0 on caught panic.
/// Collection may be expensive and is never invoked automatically.
#[cfg(feature = "allocator-diagnostics")]
#[no_mangle]
pub extern "C" fn tofudb_embed_collect_allocator(force: u32) -> i32 {
    if force > 1 { return -1; }
    catch_unwind(AssertUnwindSafe(|| {
        extern "C" { fn mi_collect(force: bool); }
        // SAFETY: exact mimalloc public C signature; the feature selects that
        // same linked allocator. It reclaims free pages, never live objects.
        unsafe { mi_collect(force == 1); }
        1
    })).unwrap_or(0)
}

/// Optional aggregate live/peak requested Rust bytes for this linked library.
/// No allocator/authority operation, payload, owner identity or new allocation.
#[cfg(feature = "allocator-diagnostics")]
#[no_mangle]
pub extern "C" fn tofudb_embed_allocator_live_bytes() -> u64 {
    crate::EMBEDDED_ALLOCATOR.current_bytes()
}
#[cfg(feature = "allocator-diagnostics")]
#[no_mangle]
pub extern "C" fn tofudb_embed_allocator_peak_bytes() -> u64 {
    crate::EMBEDDED_ALLOCATOR.peak_bytes()
}
#[cfg(feature = "allocator-diagnostics")]
#[no_mangle]
pub extern "C" fn tofudb_embed_allocator_version() -> i32 {
    extern "C" { fn mi_version() -> i32; }
    // SAFETY: read-only version of the same linked mimalloc C implementation.
    unsafe { mi_version() }
}

/// Build identity of the loaded library, independent of any adjacent executable.
/// # Safety
/// `out` must be null or writable for `out_cap` bytes.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_source_sha256(out: *mut u8, out_cap: usize) -> isize {
    catch_unwind(AssertUnwindSafe(|| {
        write_c_buffer(out, out_cap, env!("TOFUDB_SOURCE_SHA256").as_bytes())
    })).unwrap_or(-1)
}

fn write_open_error(error: *mut u8, error_cap: usize, text: &str) {
    if !error.is_null() && error_cap > 0 {
        let len = text.len().min(error_cap.saturating_sub(1));
        // SAFETY: caller guarantees a writable error buffer.
        unsafe {
            std::ptr::copy_nonoverlapping(text.as_ptr(), error, len);
            *error.add(len) = 0;
        }
    }
}

fn open_inner(
    data_dir: *const c_char,
    owner_id: u64,
    tenant_id: i64,
    projection_dir: *const c_char,
    secret: *mut u8,
    secret_len: usize,
) -> std::io::Result<EmbeddedRuntime> {
    if secret.is_null() || secret_len == 0 || secret_len > 4096 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "invalid secret buffer",
        ));
    }
    // SAFETY: the caller guarantees `secret_len` readable bytes.
    let mut secret_bytes = unsafe { std::slice::from_raw_parts(secret, secret_len) }.to_vec();
    let opened = (|| {
        let data_dir = read_c_path(data_dir)?;
        let projection = if projection_dir.is_null() {
            None
        } else {
            Some(read_c_path(projection_dir)?)
        };
        let tenant = if tenant_id > 0 {
            Some(tenant_id as u64)
        } else {
            None
        };
        EmbeddedRuntime::open(
            std::path::Path::new(&data_dir),
            owner_id,
            tenant,
            projection.as_deref().map(std::path::Path::new),
            &mut secret_bytes,
        )
    })();
    secret_bytes.zeroize();
    opened
}

/// # Safety
/// `handle` comes from `tofudb_embed_open`; `request` points to
/// `request_len` readable JSON bytes; `out` is writable for `out_cap`.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_execute(
    handle: *mut EmbedHandle,
    request: *const u8,
    request_len: usize,
    out: *mut u8,
    out_cap: usize,
) -> isize {
    match catch_unwind(AssertUnwindSafe(|| unsafe {
        execute_inner(handle, request, request_len, out, out_cap)
    })) {
        Ok(result) => result,
        Err(payload) => {
            let message = panic_message(payload);
            crate::engine_log::error(
                "panic_containment",
                &format!("tofu-db engine panic contained in embedded execute: {message}"),
            );
            // A panic in the FFI shim leaves the handle's state unknown:
            // fail every later call fast instead of trusting it.
            if !handle.is_null() {
                // SAFETY: the caller guarantees a live handle.
                poison(unsafe { &*handle });
            }
            fail(
                out,
                out_cap,
                &format!("tofu-db engine panic contained in embedded execute: {message}"),
            )
        }
    }
}

/// Read-only feature negotiation; never opens storage or starts a worker.
#[no_mangle]
pub extern "C" fn tofudb_embed_checkpoint_supported() -> u32 {
    u32::from(cfg!(feature = "native-runtime-checkpoints"))
}

/// Checkpoint control on this exact embedded handle, through its owned workers.
/// Capacity admission happens before lease effects; callers must never grow-and-
/// replay a one-shot load. Close joins host calls and forget drops only memory.
/// # Safety
/// Same live-handle and readable/writable buffer contract as tofudb_embed_execute.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_checkpoint_execute(
    handle: *mut EmbedHandle,
    request: *const u8,
    request_len: usize,
    out: *mut u8,
    out_cap: usize,
) -> isize {
    match catch_unwind(AssertUnwindSafe(|| unsafe {
        execute_mode(handle, request, request_len, out, out_cap, true)
    })) {
        Ok(result) => result,
        Err(payload) => {
            let message = panic_message(payload);
            crate::engine_log::error(
                "panic_containment",
                &format!("tofu-db engine panic contained in embedded execute: {message}"),
            );
            // A panic in the FFI shim leaves the handle's state unknown:
            // fail every later call fast instead of trusting it.
            if !handle.is_null() {
                // SAFETY: the caller guarantees a live handle.
                poison(unsafe { &*handle });
            }
            fail(
                out,
                out_cap,
                &format!("tofu-db engine panic contained in embedded execute: {message}"),
            )
        }
    }
}


unsafe fn execute_inner(
    handle: *mut EmbedHandle,
    request: *const u8,
    request_len: usize,
    out: *mut u8,
    out_cap: usize,
) -> isize {
    unsafe { execute_mode(handle, request, request_len, out, out_cap, false) }
}

unsafe fn execute_mode(
    handle: *mut EmbedHandle, request: *const u8, request_len: usize,
    out: *mut u8, out_cap: usize, checkpoint_mode: bool,
) -> isize {
    if handle.is_null() || request.is_null() || request_len == 0 {
        return fail(out, out_cap, "invalid embedded execute arguments");
    }
    if request_len > crate::generated_storage_v2::MAX_STREAMED_REQUEST_PAYLOAD_BYTES
        + MAX_EMBEDDED_ENVELOPE_BYTES {
        return fail(
            out,
            out_cap,
            "embedded request exceeds the storage.v2 payload envelope bound",
        );
    }
    // SAFETY: the caller guarantees a live handle and `request_len` bytes.
    let handle = unsafe { &*handle };
    let request_bytes = unsafe { std::slice::from_raw_parts(request, request_len) };
    if handle.poisoned.load(Ordering::Relaxed) {
        return fail(
            out,
            out_cap,
            "embedded handle is poisoned by a contained panic or watchdog timeout",
        );
    }
    let _decode = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_ENVELOPE_DECODE);
    // Validate every field and collect only recognized borrowed raw values.
    // Decode their types after all duplicate keys have settled (last wins).
    let parsed: envelope::RequestEnvelope<'_> =
        match serde_json::from_slice(request_bytes) {
            Ok(value) => value,
            Err(error) => return fail(out, out_cap, &format!("invalid request JSON: {error}")),
        };
    let operation = parsed.operation.and_then(|value|
        serde_json::from_str::<String>(value.get()).ok());
    let deadline = parsed.deadline_unix_ms.and_then(|value|
        serde_json::from_str::<u64>(value.get()).ok());
    let (operation, deadline_unix_ms) = match (operation, deadline) {
        (Some(operation), Some(deadline)) => (operation, deadline),
        _ => return fail(out, out_cap, "request requires operation and deadline_unix_ms"),
    };
    let payload = parsed.payload.map_or("{}", |value| value.get());
    if payload.len() > crate::embed::payload_limit_for_operation(&operation) {
        return fail(out, out_cap, "embedded request payload exceeds the storage.v2 payload bound");
    }
    let source_payload = payload.as_bytes();
    let mut payload = Vec::new();
    let allocation_bytes = source_payload.len();
    #[cfg(test)]
    let allocation_bytes = if FAIL_NEXT_INPUT_ALLOCATION.with(|flag| flag.replace(false)) {
        usize::MAX // Exercise the actual fallible vector reservation path.
    } else { allocation_bytes };
    if let Err(error) = payload.try_reserve_exact(allocation_bytes) {
        return fail(out, out_cap, &format!("embedded request input allocation failed: {error}"));
    }
    payload.extend_from_slice(source_payload);
    let memory = match OwnedInputLease::claim(&handle.input_memory, payload.capacity()) {
        Ok(memory) => memory,
        Err(error) => return fail(out, out_cap, &error.to_string()),
    };
    let command_id = parsed.command_id.and_then(|value|
        serde_json::from_str::<String>(value.get()).ok());
    drop(_decode);
    #[cfg(not(feature = "native-runtime-checkpoints"))]
    if checkpoint_mode { return fail(out, out_cap, "native checkpoints are not enabled"); }
    #[cfg(feature = "native-runtime-checkpoints")]
    let checkpoint = if checkpoint_mode {
        if command_id.is_some() { return fail(out, out_cap, "checkpoint commands do not accept command_id"); }
        let registry = handle.checkpoints.get_or_init(|| Arc::new(checkpoint::Registry::default())).clone();
        let needed = match registry.response_bound(&operation, &payload) {
            Ok(needed) => needed,
            Err(error) => return fail(out, out_cap, &error.to_string()),
        };
        if out.is_null() || out_cap < needed {
            return fail(out, out_cap, "checkpoint response capacity is insufficient before effects");
        }
        Some(registry)
    } else { None };
    let budget = watchdog_budget(deadline_unix_ms);
    let (respond, outcome_rx) = reply::fresh::<JobOutcome>();
    let job = Job {
        #[cfg(feature = "native-runtime-checkpoints")]
        checkpoint,
        enqueued_at: crate::request_profile::enabled().then(std::time::Instant::now),
        operation,
        payload,
        command_id,
        deadline_unix_ms,
        respond,
        _memory: memory,
    };
    let _submit = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_JOB_SUBMIT);
    let handle_lock = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_HANDLE_LOCK);
    let submitted = match handle.jobs.lock() {
        Ok(jobs) => {
            drop(handle_lock);
            match jobs.as_ref() {
                Some(sender) => sender.try_handoff(job),
                None => {
                    return fail(out, out_cap, "embedded runtime is closed");
                }
            }
        }
        Err(_) => {
            drop(handle_lock);
            return fail(out, out_cap, "embedded job channel lock is poisoned");
        }
    };
    drop(_submit);
    match submitted {
        Ok(Some(job)) => {
            if let Err(error) = spawn_demand_worker(handle, job) {
                // The unstarted closure drops its exclusively owned job and
                // memory lease. It has never been published to another worker.
                return fail(out, out_cap, &format!("embedded execute worker could not start: {error}"));
            }
        }
        Ok(None) => {},
        Err(_) => {
            poison(handle);
            return fail(out, out_cap, "embedded execute worker terminated");
        }
    }
    let outcome = {
        let _wait = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WAIT_RESPONSE);
        outcome_rx.recv_timeout(budget).map(JobOutcome::receive)
    };
    match outcome {
        Ok(Ok(Ok(response))) => {
            let _copy = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_RESPONSE_COPY);
            write_c_buffer(out, out_cap, &response)
        }
        Ok(Ok(Err(error))) => fail(out, out_cap, &error.to_string()),
        Ok(Err(message)) => {
            crate::engine_log::error(
                "panic_containment",
                &format!("tofu-db engine panic contained in embedded execute: {message}"),
            );
            poison(handle);
            fail(
                out,
                out_cap,
                &format!("tofu-db engine panic contained in embedded execute: {message}"),
            )
        }
        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
            crate::engine_log::error(
                "embedded_watchdog",
                &format!(
                    "embedded execute exceeded its watchdog budget of {budget:?}; \
                     the abandoned worker is detached and the handle is poisoned"
                ),
            );
            poison(handle);
            // The receiver is dropped here: a late worker result fails its
            // send and is freed with the channel, so the abandoned worker
            // can never write into the caller's buffer.
            fail(
                out,
                out_cap,
                &format!(
                    "embedded execute watchdog fired after {budget:?}; \
                     the engine is wedged and the handle is poisoned"
                ),
            )
        }
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
            poison(handle);
            fail(out, out_cap, "embedded execute worker terminated")
        }
    }
}

/// # Safety
/// `handle` comes from `tofudb_embed_open` and is destroyed here exactly
/// once; calling with null is a no-op.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_close(handle: *mut EmbedHandle) {
    if catch_unwind(AssertUnwindSafe(|| teardown(handle, true))).is_err() {
        crate::engine_log::error(
            "panic_containment",
            "tofu-db engine panic contained in embedded close teardown",
        );
    }
}

/// Ungraceful teardown (crash-recovery measurement): drivers join but the
/// graceful WAL-window retirement is skipped, so the next open replays the
/// full pre-crash window like a power loss.
///
/// # Safety
/// Same exactly-once handle contract as `tofudb_embed_close`.
#[no_mangle]
pub unsafe extern "C" fn tofudb_embed_abandon(handle: *mut EmbedHandle) {
    if catch_unwind(AssertUnwindSafe(|| teardown(handle, false))).is_err() {
        crate::engine_log::error(
            "panic_containment",
            "tofu-db engine panic contained in embedded abandon teardown",
        );
    }
}

#[cfg(test)]
mod tests;

#[cfg(all(test, not(feature = "native-runtime-checkpoints")))]
mod checkpoint_feature_tests {
    #[test]
    fn checkpoint_feature_negotiation_is_disabled_without_the_explicit_feature() {
        assert_eq!(super::tofudb_embed_checkpoint_supported(), 0);
    }
}
