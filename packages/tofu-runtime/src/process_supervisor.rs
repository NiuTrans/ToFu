//! Linux tool process supervision behind a dedicated single-thread worker.
//! The worker alone changes subreaper/signal policy. Parent ownership, explicit
//! launch mapping and bounded captures survive cancellation and callback panic.
//! Permission, sandbox/cgroup admission and production tool registration remain
//! composition responsibilities; this is not an untrusted execution boundary.
use crate::{
    process_boundary::{self, ResourceLimits},
    process_launch::Launch,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

pub const REQUEST_FORMAT: &str = "tofu.native-process-request/v1";
pub const RESULT_FORMAT: &str = "tofu.native-process-result/v1";

static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn stop_signal(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}

pub struct Request {
    pub executable: String,
    pub arguments: Vec<String>,
    pub directory: PathBuf,
    pub environment: BTreeMap<String, String>,
    pub stdout_bytes: usize,
    pub stderr_bytes: usize,
    pub timeout: Duration,
    pub termination_grace: Duration,
    /// Explicit cancellation observation interval, not resident thread count.
    pub poll_interval: Duration,
    pub config_bytes: usize,
    /// Explicit per-process caps; tree-wide containment remains external.
    pub limits: Option<ResourceLimits>,
}
/// Raw captured bytes remain byte buffers across the worker boundary.
/// Metadata contains only lifecycle/count facts, never launch credentials.
pub struct Output {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub metadata: Value,
}
#[derive(Debug)]
pub enum Error {
    InvalidRequest,
    StoppedBeforeLaunch,
    Io(io::ErrorKind),
    WorkerFailed,
    Protocol,
}
impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Self::Io(e.kind())
    }
}

/// Run one owned worker synchronously; the caller must use its admitted blocking
/// execution lane. `stopped` can observe the owning ToolScope checkpoint. This
/// function never returns until the worker exits, including unwinding paths.
pub fn execute(
    worker: &Path,
    request: &Request,
    mut stopped: impl FnMut() -> bool,
) -> Result<Output, Error> {
    if stopped() {
        return Err(Error::StoppedBeforeLaunch);
    }
    let interval = interval_ms(request.poll_interval)?;
    if !worker.is_absolute()
        || request.timeout.as_millis() == 0
        || request.config_bytes == 0
        || request.config_bytes.checked_add(1).is_none()
    {
        return Err(Error::InvalidRequest);
    }
    if let Some(limits) = request.limits {
        limits.validate().map_err(|_| Error::InvalidRequest)?;
    }
    let limit = request
        .stdout_bytes
        .checked_add(request.stderr_bytes)
        .and_then(|v| v.checked_add(4100))
        .ok_or(Error::InvalidRequest)?;
    // Serialize borrowed launch fields through an admitted writer. No duplicate
    // environment tree or over-budget serialized configuration is constructed.
    let config = configuration(request, interval)?;
    let mut command = Command::new(worker);
    command
        .arg("process-worker")
        .arg(REQUEST_FORMAT)
        .arg(request.config_bytes.to_string())
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let parent = std::process::id() as libc::pid_t;
    // SAFETY: the post-fork hook performs only async-signal-safe Linux syscalls.
    unsafe {
        command.pre_exec(move || {
            process_boundary::install(None)?;
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                return Err(io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            Ok(())
        });
    }
    let mut guard = WorkerGuard {
        child: command.spawn()?,
        finished: false,
    };
    let worker_exit = pidfd(guard.child.id())?;
    let mut stdout = guard.child.stdout.take().ok_or(Error::Protocol)?;
    nonblocking(stdout.as_raw_fd())?;
    // stdin exists only for this bounded configuration transfer, then EOF.
    guard
        .child
        .stdin
        .take()
        .ok_or(Error::Protocol)?
        .write_all(&config)?;
    drop(config);
    let mut capture = Capture::new(limit);
    let mut requested = false;
    loop {
        if stopped() && !requested {
            // The owned PID is unreaped until after the loop; it cannot be reused.
            signal(guard.child.id() as libc::pid_t, libc::SIGTERM)?;
            requested = true;
        }
        capture.drain(&mut stdout)?;
        if exited(guard.child.id())? {
            break;
        }
        let descriptors = [worker_exit.as_raw_fd(), stdout.as_raw_fd()];
        poll(&descriptors[..if capture.eof { 1 } else { 2 }], interval)?;
    }
    let status = guard.child.wait()?;
    guard.finished = true;
    while !capture.eof {
        let before = capture.total;
        capture.drain(&mut stdout)?;
        if !capture.eof && capture.total == before {
            return Err(Error::Protocol);
        }
    }
    if !status.success() {
        return Err(Error::WorkerFailed);
    }
    if capture.total != capture.bytes.len() as u64 {
        return Err(Error::Protocol);
    }
    if capture.bytes.len() < 4 {
        return Err(Error::Protocol);
    }
    let header_length =
        u32::from_be_bytes(capture.bytes[..4].try_into().map_err(|_| Error::Protocol)?) as usize;
    if header_length > 4096 {
        return Err(Error::Protocol);
    }
    let header_end = header_length.checked_add(4).ok_or(Error::Protocol)?;
    let header = capture.bytes.get(4..header_end).ok_or(Error::Protocol)?;
    let mut metadata: Value = serde_json::from_slice(header).map_err(|_| Error::Protocol)?;
    if !metadata.is_object()
        || metadata.get("format").and_then(Value::as_str) != Some(RESULT_FORMAT)
        || metadata.get("reaped") != Some(&Value::Bool(true))
    {
        return Err(Error::Protocol);
    }
    let length = |field| {
        metadata[field]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok())
            .ok_or(Error::Protocol)
    };
    let out_length = length("stdout_length")?;
    let err_length = length("stderr_length")?;
    let split = header_end.checked_add(out_length).ok_or(Error::Protocol)?;
    if out_length > request.stdout_bytes
        || err_length > request.stderr_bytes
        || split.checked_add(err_length) != Some(capture.bytes.len())
    {
        return Err(Error::Protocol);
    }
    let stderr = capture.bytes.split_off(split);
    capture.bytes.drain(..header_end);
    // Preserve caller Stop even when it races worker completion/serialization.
    let cancelled = requested || stopped() || metadata["cancelled"] == true;
    metadata["cancelled"] = Value::Bool(cancelled);
    Ok(Output {
        stdout: capture.bytes,
        stderr,
        metadata,
    })
}
fn write_report(mut metadata: Value, stdout: &[u8], stderr: &[u8]) -> io::Result<()> {
    metadata["format"] = json!(RESULT_FORMAT);
    metadata["stdout_length"] = json!(stdout.len());
    metadata["stderr_length"] = json!(stderr.len());
    let header = serde_json::to_vec(&metadata)?;
    if header.len() > 4096 {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    let mut output = std::io::stdout().lock();
    output.write_all(&(header.len() as u32).to_be_bytes())?;
    output.write_all(&header)?;
    output.write_all(stdout)?;
    output.write_all(stderr)?;
    output.flush()
}

struct BoundedWriter {
    bytes: Vec<u8>,
    limit: usize,
}
impl Write for BoundedWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(io::Error::from(io::ErrorKind::InvalidInput));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
struct Configuration<'a> {
    request: &'a Request,
    interval: i32,
    timeout_ms: u64,
    grace_ms: u64,
}
impl serde_core::Serialize for Configuration<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde_core::ser::SerializeMap;
        let r = self.request;
        let mut map = serializer.serialize_map(Some(11))?;
        map.serialize_entry("format", REQUEST_FORMAT)?;
        map.serialize_entry("executable", &r.executable)?;
        map.serialize_entry("arguments", &r.arguments)?;
        map.serialize_entry("directory", &r.directory)?;
        map.serialize_entry("environment", &r.environment)?;
        map.serialize_entry("stdout_bytes", &r.stdout_bytes)?;
        map.serialize_entry("stderr_bytes", &r.stderr_bytes)?;
        map.serialize_entry("timeout_ms", &self.timeout_ms)?;
        map.serialize_entry("grace_ms", &self.grace_ms)?;
        map.serialize_entry("poll_ms", &self.interval)?;
        map.serialize_entry("limits", &r.limits)?;
        map.end()
    }
}
fn configuration(request: &Request, interval: i32) -> Result<Vec<u8>, Error> {
    let config = Configuration {
        request,
        interval,
        timeout_ms: u64::try_from(request.timeout.as_millis())
            .map_err(|_| Error::InvalidRequest)?,
        grace_ms: u64::try_from(request.termination_grace.as_millis())
            .map_err(|_| Error::InvalidRequest)?,
    };
    let mut writer = BoundedWriter {
        bytes: Vec::new(),
        limit: request.config_bytes,
    };
    serde_json::to_writer(&mut writer, &config).map_err(|_| Error::InvalidRequest)?;
    Ok(writer.bytes)
}
struct WorkerGuard {
    child: Child,
    finished: bool,
}
impl Drop for WorkerGuard {
    fn drop(&mut self) {
        if !self.finished {
            // Close output first: a worker finishing cleanup cannot block writing
            // an unobserved result. SIGTERM lets it reap its tool descendants.
            self.child.stdout.take();
            self.child.stdin.take();
            let _ = signal(self.child.id() as libc::pid_t, libc::SIGTERM);
            let _ = self.child.wait();
        }
    }
}

struct Capture {
    bytes: Vec<u8>,
    total: u64,
    limit: usize,
    eof: bool,
}
impl Capture {
    fn new(limit: usize) -> Self {
        Self {
            bytes: Vec::new(),
            total: 0,
            limit,
            eof: false,
        }
    }
    fn drain(&mut self, reader: &mut impl Read) -> io::Result<()> {
        let mut buffer = [0u8; 16384];
        // Bound each drain so a flooding producer cannot starve Stop observation.
        for _ in 0..16 {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    self.eof = true;
                    break;
                }
                Ok(n) => {
                    self.total = self.total.saturating_add(n as u64);
                    let keep = n.min(self.limit.saturating_sub(self.bytes.len()));
                    self.bytes.extend_from_slice(&buffer[..keep]);
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
}
fn nonblocking(fd: libc::c_int) -> io::Result<()> {
    // SAFETY: borrowed live descriptor; flags change without transferring ownership.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
fn pidfd(pid: u32) -> io::Result<OwnedFd> {
    // SAFETY: Linux returns a fresh owned descriptor for our unreaped child.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd as _) })
}
fn interval_ms(interval: Duration) -> Result<i32, Error> {
    let ms = i32::try_from(interval.as_millis()).map_err(|_| Error::InvalidRequest)?;
    if ms <= 0 {
        Err(Error::InvalidRequest)
    } else {
        Ok(ms)
    }
}
fn poll(fds: &[libc::c_int], interval: i32) -> io::Result<()> {
    if fds.len() > 3 {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }
    let mut descriptors = [libc::pollfd {
        fd: -1,
        events: libc::POLLIN,
        revents: 0,
    }; 3];
    for (slot, fd) in descriptors.iter_mut().zip(fds) {
        slot.fd = *fd;
    }
    // SAFETY: the fixed buffer remains live and writable throughout the syscall.
    if unsafe { libc::poll(descriptors.as_mut_ptr(), fds.len() as _, interval) } < 0 {
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
    }
    Ok(())
}
fn signal(pid: libc::pid_t, sig: libc::c_int) -> io::Result<()> {
    // SAFETY: caller owns an unreaped PID or its reserved process group.
    if unsafe { libc::kill(pid, sig) } < 0 {
        let e = io::Error::last_os_error();
        if e.raw_os_error() != Some(libc::ESRCH) {
            return Err(e);
        }
    }
    Ok(())
}
fn exited(pid: u32) -> io::Result<bool> {
    // SAFETY: initialized siginfo output; WNOWAIT reserves PID until owned wait.
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    if unsafe {
        libc::waitid(
            libc::P_PID,
            pid,
            &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { info.si_pid() } != 0)
}
fn visit_children(mut visit: impl FnMut(libc::pid_t) -> io::Result<()>) -> io::Result<usize> {
    use std::io::BufReader;
    let id = std::process::id();
    let file = std::fs::File::open(format!("/proc/{id}/task/{id}/children"))?;
    let reader = BufReader::with_capacity(8192, file);
    let mut number: libc::pid_t = 0;
    let mut has_number = false;
    let mut count = 0;
    for byte in reader.bytes().chain(std::iter::once(Ok(b' '))) {
        let byte = byte?;
        if byte.is_ascii_whitespace() {
            if has_number {
                if number <= 0 {
                    return Err(io::Error::from(io::ErrorKind::InvalidData));
                }
                visit(number)?;
                count += 1;
                number = 0;
                has_number = false;
            }
        } else if byte.is_ascii_digit() {
            number = number
                .checked_mul(10)
                .and_then(|n| n.checked_add((byte - b'0') as _))
                .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
            has_number = true;
        } else {
            return Err(io::Error::from(io::ErrorKind::InvalidData));
        }
    }
    Ok(count)
}
struct ToolGuard {
    child: Child,
    reaped: bool,
}
impl ToolGuard {
    fn finish(&mut self) -> io::Result<std::process::ExitStatus> {
        // Kill the reserved group BEFORE reaping its leader; avoid PID/PGID reuse.
        signal(-(self.child.id() as libc::pid_t), libc::SIGKILL)?;
        let status = self.child.wait()?;
        self.reaped = true;
        reap_adopted()?;
        Ok(status)
    }
}
fn reap_adopted() -> io::Result<()> {
    loop {
        // Every listed PID is our unreaped direct child. Killing ancestors causes
        // escaped grandchildren to be adopted here on the next iteration.
        visit_children(|child| signal(child, libc::SIGKILL))?;
        let mut status = 0;
        // SAFETY: only the dedicated worker has this process-wide child ownership.
        let waited = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
        if waited < 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ECHILD) {
                return Ok(());
            }
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if waited == 0 {
            poll(&[], 1)?;
        }
    }
}
impl Drop for ToolGuard {
    fn drop(&mut self) {
        loop {
            let result = if !self.reaped {
                self.finish().map(|_| ())
            } else {
                reap_adopted()
            };
            if result.is_ok() {
                break;
            }
            // Exceptional OS/procfs errors cannot authorize abandonment. Retain
            // ownership and retry cleanup; no terminal result is published.
            std::thread::sleep(Duration::from_millis(100));
        }
    }
}

/// Internal entry point for a fresh, single-threaded worker process only.
/// No application process may install this process-wide signal/subreaper policy.
pub fn worker_main() -> Result<(), Box<dyn std::error::Error>> {
    use std::os::unix::process::ExitStatusExt;
    if std::fs::read_dir("/proc/self/task")?.count() != 1 || visit_children(|_| Ok(()))? != 0 {
        return Err("process worker must have exclusive child ownership".into());
    }
    // SAFETY: this dedicated single-threaded worker owns all descendants/signals.
    unsafe {
        if libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0 {
            return Err(io::Error::last_os_error().into());
        }
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = stop_signal as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        if libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut()) != 0 {
            return Err(io::Error::last_os_error().into());
        }
        // exec preserves ignored dispositions. Never inherit automatic child
        // reaping, which would invalidate reserved PID/group ownership.
        action.sa_sigaction = libc::SIG_DFL;
        if libc::sigaction(libc::SIGCHLD, &action, std::ptr::null_mut()) != 0 {
            return Err(io::Error::last_os_error().into());
        }
    }
    // Put the version before the numeric bound: pre-version workers reject it
    // before reading any tool request, so skew cannot silently omit new limits.
    if std::env::args().nth(2).as_deref() != Some(REQUEST_FORMAT) {
        return Err("unsupported process worker format".into());
    }
    let limit: usize = std::env::args()
        .nth(3)
        .ok_or("missing config bound")?
        .parse()?;
    let mut config = Vec::new();
    std::io::stdin()
        .take(
            u64::try_from(limit)?
                .checked_add(1)
                .ok_or("config bound overflow")?,
        )
        .read_to_end(&mut config)?;
    if config.len() > limit {
        return Err("config bound exceeded".into());
    }
    let v: Value = serde_json::from_slice(&config)?;
    if v.get("format").and_then(Value::as_str) != Some(REQUEST_FORMAT) {
        return Err("unsupported process request format".into());
    }
    let str_field = |name| v[name].as_str().ok_or("invalid process config");
    let num = |name| v[name].as_u64().ok_or("invalid process budget");
    let arguments = v["arguments"]
        .as_array()
        .ok_or("invalid arguments")?
        .iter()
        .map(|v| v.as_str().map(String::from).ok_or("invalid argument"))
        .collect::<Result<Vec<_>, _>>()?;
    let environment = v["environment"]
        .as_object()
        .ok_or("invalid environment")?
        .iter()
        .map(|(k, v)| {
            v.as_str()
                .map(|v| (k.clone(), v.to_string()))
                .ok_or("invalid environment value")
        })
        .collect::<Result<BTreeMap<_, _>, _>>()?;
    let launch = Launch::validate(
        str_field("executable")?,
        arguments,
        PathBuf::from(str_field("directory")?),
        environment,
    )
    .map_err(|_| "invalid child mapping")?;
    let interval = i32::try_from(num("poll_ms")?)?;
    if interval <= 0 || num("timeout_ms")? == 0 {
        return Err("invalid process timeout".into());
    }
    let timeout = Duration::from_millis(num("timeout_ms")?);
    let grace = Duration::from_millis(num("grace_ms")?);
    let mut stdout = Capture::new(usize::try_from(num("stdout_bytes")?)?);
    let mut stderr = Capture::new(usize::try_from(num("stderr_bytes")?)?);
    let limits = ResourceLimits::parse(v.get("limits").ok_or("missing process limits field")?)?;
    drop(v);
    drop(config);
    if STOP.load(Ordering::Relaxed) {
        write_report(
            json!({"reaped":true,"cancelled":true,"timed_out":false,
            "stdout_total":0,"stderr_total":0,"stdout_truncated":false,"stderr_truncated":false,
            "exit_code":null,"signal":null}),
            &[],
            &[],
        )?;
        return Ok(());
    }
    let mut command = launch.command();
    command.process_group(0);
    // SAFETY: boundary installation uses only fixed buffers and Linux syscalls
    // in the forked child; supervisor resource limits remain unchanged.
    unsafe {
        command.pre_exec(move || process_boundary::install(limits));
    }
    let mut guard = ToolGuard {
        child: command.spawn()?,
        reaped: false,
    };
    let tool_exit = pidfd(guard.child.id())?;
    let mut out = guard.child.stdout.take().ok_or("missing stdout")?;
    let mut err = guard.child.stderr.take().ok_or("missing stderr")?;
    nonblocking(out.as_raw_fd())?;
    nonblocking(err.as_raw_fd())?;
    let started = Instant::now();
    let mut stopping = None;
    let mut cancelled = false;
    let mut timed_out = false;
    loop {
        cancelled |= STOP.load(Ordering::Relaxed);
        timed_out |= started.elapsed() >= timeout;
        if (cancelled || timed_out) && stopping.is_none() {
            signal(-(guard.child.id() as libc::pid_t), libc::SIGTERM)?;
            stopping = Some(Instant::now());
        }
        stdout.drain(&mut out)?;
        stderr.drain(&mut err)?;
        if exited(guard.child.id())? || stopping.is_some_and(|at| at.elapsed() >= grace) {
            break;
        }
        let remain = stopping
            .map(|at| grace.saturating_sub(at.elapsed()))
            .unwrap_or_else(|| timeout.saturating_sub(started.elapsed()));
        let wait = interval.min(i32::try_from(remain.as_millis()).unwrap_or(i32::MAX));
        let mut fds = [tool_exit.as_raw_fd(), -1, -1];
        let mut count = 1;
        if !stdout.eof {
            fds[count] = out.as_raw_fd();
            count += 1;
        }
        if !stderr.eof {
            fds[count] = err.as_raw_fd();
            count += 1;
        }
        poll(&fds[..count], wait)?;
    }
    let status = guard.finish()?;
    while !stdout.eof || !stderr.eof {
        let before = stdout.total.saturating_add(stderr.total);
        stdout.drain(&mut out)?;
        stderr.drain(&mut err)?;
        if (!stdout.eof || !stderr.eof) && before == stdout.total.saturating_add(stderr.total) {
            return Err("output still owned outside the reaped process tree".into());
        }
    }
    cancelled |= STOP.load(Ordering::Relaxed);
    write_report(
        json!({"reaped":true,"cancelled":cancelled,"timed_out":timed_out,
        "exit_code":status.code(),"signal":status.signal(),
        "stdout_total":stdout.total,"stderr_total":stderr.total,
        "stdout_truncated":stdout.total > stdout.bytes.len() as u64,
        "stderr_truncated":stderr.total > stderr.bytes.len() as u64}),
        &stdout.bytes,
        &stderr.bytes,
    )?;
    Ok(())
}
