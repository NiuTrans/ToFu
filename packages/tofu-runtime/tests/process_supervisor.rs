#![cfg(target_os = "linux")]
use std::{
    collections::BTreeMap,
    path::Path,
    time::{Duration, Instant},
};
use tofu_runtime::process_supervisor::{execute, Error, Request};
fn request(script: &str) -> Request {
    Request {
        executable: "/bin/sh".into(),
        arguments: vec!["-c".into(), script.into()],
        directory: "/tmp".into(),
        environment: BTreeMap::from([("PATH".into(), "/usr/bin:/bin".into())]),
        stdout_bytes: 1024,
        stderr_bytes: 1024,
        timeout: Duration::from_secs(3),
        termination_grace: Duration::from_millis(20),
        poll_interval: Duration::from_millis(10),
        config_bytes: 65536,
        limits: None,
    }
}
fn worker() -> &'static Path {
    Path::new(env!("CARGO_BIN_EXE_tofu-native"))
}
fn assert_gone(pid: u32) {
    assert!(
        !Path::new(&format!("/proc/{pid}")).exists(),
        "owned descendant {pid} remains"
    );
}
#[test]
fn captures_both_streams_and_independent_exit_status() {
    let value = execute(
        worker(),
        &request("printf 'hello'; printf 'error' >&2; exit 7"),
        || false,
    )
    .unwrap();
    assert_eq!(value.stdout.clone(), b"hello");
    assert_eq!(value.stderr.clone(), b"error");
    assert_eq!(value.metadata["exit_code"], 7);
    assert_eq!(value.metadata["cancelled"], false);
    assert_eq!(value.metadata["timed_out"], false);
    assert_eq!(value.metadata["reaped"], true);
}
#[test]
fn caps_captures_but_continues_draining_both_pipes() {
    let mut req = request("head -c 1048576 /dev/zero; head -c 1048576 /dev/zero >&2");
    req.stdout_bytes = 31;
    req.stderr_bytes = 17;
    let value = execute(worker(), &req, || false).unwrap();
    assert_eq!(value.stdout.clone().len(), 31);
    assert_eq!(value.stderr.clone().len(), 17);
    assert_eq!(value.metadata["stdout_total"], 1048576);
    assert_eq!(value.metadata["stderr_total"], 1048576);
    assert_eq!(value.metadata["stdout_truncated"], true);
    assert_eq!(value.metadata["stderr_truncated"], true);
    assert_eq!(value.metadata["exit_code"], 0);
}
#[test]
fn normal_exit_reaps_leftover_process_group() {
    let value = execute(worker(), &request("sleep 30 & echo $!; exit 0"), || false).unwrap();
    let pid = String::from_utf8(value.stdout.clone())
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_gone(pid);
    assert_eq!(value.metadata["exit_code"], 0);
}
#[test]
fn reaps_double_forked_session_escape() {
    let script = "/usr/bin/python3 -c 'import os,time; p=os.fork();\nif p: os.waitpid(p,0); time.sleep(0.1); os._exit(0)\nos.setsid(); q=os.fork();\nif q: os._exit(0)\nprint(os.getpid(),flush=True); time.sleep(30)'";
    let value = execute(worker(), &request(script), || false).unwrap();
    let pid = String::from_utf8(value.stdout.clone())
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_gone(pid);
    assert_eq!(value.metadata["exit_code"], 0);
}
#[test]
fn timeout_kills_term_ignoring_child_then_reaps() {
    let mut req = request("trap '' TERM; echo $$; while :; do sleep 1; done");
    req.timeout = Duration::from_millis(80);
    let value = execute(worker(), &req, || false).unwrap();
    let pid = String::from_utf8(value.stdout.clone())
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_gone(pid);
    assert_eq!(value.metadata["timed_out"], true);
    assert_eq!(value.metadata["cancelled"], false);
    assert_eq!(value.metadata["signal"], 9);
}
#[test]
fn caller_cancellation_waits_for_process_exit() {
    let req = request("trap '' TERM; echo $$; while :; do sleep 1; done");
    let started = Instant::now();
    let value = execute(worker(), &req, || {
        started.elapsed() > Duration::from_millis(100)
    })
    .unwrap();
    let pid = String::from_utf8(value.stdout.clone())
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_gone(pid);
    assert_eq!(value.metadata["cancelled"], true);
    assert_eq!(value.metadata["timed_out"], false);
}
#[test]
fn rejects_before_spawn_and_worker_reports_launch_failure() {
    let mut req = request("exit 0");
    assert!(matches!(
        execute(worker(), &req, || true),
        Err(Error::StoppedBeforeLaunch)
    ));
    req.timeout = Duration::from_nanos(1);
    assert!(matches!(
        execute(worker(), &req, || false),
        Err(Error::InvalidRequest)
    ));
    req.timeout = Duration::from_secs(3);
    req.config_bytes = 1;
    assert!(matches!(
        execute(worker(), &req, || false),
        Err(Error::InvalidRequest)
    ));
    req.config_bytes = 65536;
    req.executable = "/no-such-tofu-executable".into();
    assert!(matches!(
        execute(worker(), &req, || false),
        Err(Error::WorkerFailed)
    ));
}
#[test]
fn large_result_protocol_is_fully_drained() {
    let mut req = request("head -c 524288 /dev/zero");
    req.stdout_bytes = 524288;
    let value = execute(worker(), &req, || false).unwrap();
    assert_eq!(value.stdout.clone().len(), 524288);
    assert_eq!(value.metadata["stdout_truncated"], false);
}

struct Scratch(std::path::PathBuf);
impl Scratch {
    fn new() -> Self {
        use std::os::unix::fs::DirBuilderExt;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "tofu-native-process-{}-{nonce}",
            std::process::id()
        ));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .unwrap();
        Self(path)
    }
    fn file(&self) -> std::path::PathBuf {
        self.0.join("pids")
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn read_pids(path: &Path) -> Option<Vec<u32>> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.split_whitespace().map(|s| s.parse().unwrap()).collect())
}
#[test]
fn callback_panic_still_joins_worker_and_descendants() {
    let scratch = Scratch::new();
    let path = scratch.file();
    let req = request(&format!(
        "trap '' TERM; echo $$ $PPID > '{}'; while :; do sleep 1; done",
        path.display()
    ));
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        execute(worker(), &req, || {
            if read_pids(&path).is_some_and(|p| p.len() == 2) {
                panic!("injected caller failure");
            }
            false
        })
    }));
    assert!(result.is_err());
    for pid in read_pids(&path).unwrap() {
        assert_gone(pid);
    }
}
#[test]
fn timeout_and_cancellation_remain_independent() {
    let mut req = request("trap '' TERM; echo $$; while :; do sleep 1; done");
    req.timeout = Duration::from_millis(80);
    req.termination_grace = Duration::from_millis(150);
    let start = Instant::now();
    let value = execute(worker(), &req, || {
        start.elapsed() >= Duration::from_millis(110)
    })
    .unwrap();
    assert_eq!(value.metadata["timed_out"], true);
    assert_eq!(value.metadata["cancelled"], true);
    let pid = String::from_utf8(value.stdout)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_gone(pid);
}
#[test]
fn parent_scope_abort_reaps_before_resource_release() {
    use serde_json::{json, Value};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use tofu_runtime::{
        execution_contract::{ExecutionPhase as Phase, ResourceName},
        session::{Registry as Sessions, ReleaseAck},
        tool_contract::*,
        tool_execution::Scope,
    };
    struct Allow;
    impl Authorization for Allow {
        fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
            owner == 7
        }
    }
    struct Process {
        req: Request,
        path: std::path::PathBuf,
        releases: Arc<AtomicUsize>,
    }
    impl Handler for Process {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            let path = self.path.clone();
            let releases = self.releases.clone();
            context
                .runtime
                .bind_resource(
                    ResourceName::ToolEnvironment,
                    0,
                    false,
                    Box::new(move |_| {
                        for pid in read_pids(&path).unwrap() {
                            assert_gone(pid);
                        }
                        releases.fetch_add(1, Ordering::SeqCst);
                        Ok(ReleaseAck::Released)
                    }),
                    Box::new(|| {}),
                )
                .unwrap();
            let output = execute(worker(), &self.req, || {
                context.runtime.checkpoint().is_err()
            })
            .unwrap();
            assert_eq!(output.metadata["reaped"], true);
            Ok(output.metadata)
        }
    }
    let scratch = Scratch::new();
    let path = scratch.file();
    let req = request(&format!(
        "trap '' TERM; echo $$ $PPID > '{}'; while :; do sleep 1; done",
        path.display()
    ));
    let sessions = Sessions::new();
    let parent = sessions
        .start("process-parent", "task", 7, "", None)
        .unwrap();
    let scope = Arc::new(
        Scope::with_parent(
            &sessions,
            7,
            "process-child",
            "",
            None,
            parent.clone(),
            "process-parent",
        )
        .unwrap(),
    );
    let releases = Arc::new(AtomicUsize::new(0));
    let catalog=Catalog::compile(7,"epoch",&json!({"work":{"name":"work","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}}})).unwrap();
    let call = catalog
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let registry = Registry::new(
        7,
        "epoch",
        vec![(
            "work".into(),
            Arc::new(Process {
                req,
                path: path.clone(),
                releases: releases.clone(),
            }),
        )],
    )
    .unwrap();
    let thread =
        std::thread::spawn(move || registry.dispatch_scoped(call, &Allow, &scope).unwrap());
    let until = Instant::now() + Duration::from_secs(2);
    while read_pids(&path).is_none() {
        assert!(Instant::now() < until);
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(releases.load(Ordering::SeqCst), 0);
    parent.request_cancel("user_stop").unwrap();
    let outcome = thread.join().unwrap();
    assert!(matches!(outcome.error, Some(DispatchError::Cancelled)));
    assert!(outcome.value.is_none());
    assert_eq!(releases.load(Ordering::SeqCst), 1);
    assert!(outcome.receipt.invariants_satisfied);
    parent.settle(Phase::Cancelled, "user_stop").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}

fn python_request(script: &str) -> Request {
    let mut req = request("");
    req.executable = "/usr/bin/python3".into();
    req.arguments = vec!["-c".into(), script.into()];
    req
}
fn limits() -> tofu_runtime::process_boundary::ResourceLimits {
    tofu_runtime::process_boundary::ResourceLimits {
        address_space_bytes: 64 * 1024 * 1024,
        cpu_seconds: 2,
        file_bytes: 4096,
        file_descriptors: 32,
    }
}
#[test]
fn exec_boundary_applies_limits_without_changing_parent_policy() {
    let resources = [
        libc::RLIMIT_AS,
        libc::RLIMIT_CPU,
        libc::RLIMIT_FSIZE,
        libc::RLIMIT_NOFILE,
        libc::RLIMIT_CORE,
    ];
    let read = || {
        resources.map(|resource| {
            let mut limit: libc::rlimit = unsafe { std::mem::zeroed() };
            assert_eq!(unsafe { libc::getrlimit(resource, &mut limit) }, 0);
            (limit.rlim_cur, limit.rlim_max)
        })
    };
    let before = read();
    let before_priv = unsafe { libc::prctl(libc::PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) };
    let mut req=python_request("import ctypes,json,resource; print(json.dumps({'limits':[resource.getrlimit(r) for r in [resource.RLIMIT_AS,resource.RLIMIT_CPU,resource.RLIMIT_FSIZE,resource.RLIMIT_NOFILE,resource.RLIMIT_CORE]],'no_new_privs':ctypes.CDLL(None).prctl(39,0,0,0,0)}))");
    req.limits = Some(limits());
    let output = execute(worker(), &req, || false).unwrap();
    let result: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        result["limits"],
        serde_json::json!([[67108864, 67108864], [2, 2], [4096, 4096], [32, 32], [0, 0]])
    );
    assert_eq!(result["no_new_privs"], 1);
    assert_eq!(output.metadata["exit_code"], 0);
    assert_eq!(read(), before);
    assert_eq!(
        unsafe { libc::prctl(libc::PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) },
        before_priv
    );
}
#[test]
fn descriptor_above_child_limit_is_not_inherited() {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let file = std::fs::File::open("/dev/null").unwrap();
    // F_DUPFD deliberately creates an inheritable descriptor above child NOFILE.
    let high = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD, 4096) };
    assert!(high >= 4096);
    let descriptor = unsafe { OwnedFd::from_raw_fd(high) };
    assert_eq!(
        unsafe { libc::fcntl(high, libc::F_GETFD) } & libc::FD_CLOEXEC,
        0
    );
    let mut req=python_request(&format!("import os;\ntry: os.fstat({high})\nexcept OSError as e: print(e.errno)\nelse: raise RuntimeError('inherited descriptor')"));
    req.limits = Some(limits());
    let output = execute(worker(), &req, || false).unwrap();
    assert_eq!(output.stdout, b"9\n");
    assert_eq!(output.metadata["exit_code"], 0);
    // Parent ownership and flags are untouched by sealing in the forked child.
    assert_eq!(
        unsafe { libc::fcntl(descriptor.as_raw_fd(), libc::F_GETFD) } & libc::FD_CLOEXEC,
        0
    );
}
#[test]
fn tool_exec_seals_descriptors_even_for_direct_worker_entry() {
    use std::io::Write;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::process::{Command, Stdio};
    let file = std::fs::File::open("/dev/null").unwrap();
    let high = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD, 4096) };
    assert!(high >= 4096);
    let _descriptor = unsafe { OwnedFd::from_raw_fd(high) };
    let script=format!("import os;\ntry: os.fstat({high})\nexcept OSError as e: print(e.errno)\nelse: raise RuntimeError('inherited descriptor')");
    let config = serde_json::json!({"format":tofu_runtime::process_supervisor::REQUEST_FORMAT,"executable":"/usr/bin/python3","arguments":["-c",script],"directory":"/tmp","environment":{},"stdout_bytes":1024,"stderr_bytes":1024,"timeout_ms":3000,"grace_ms":20,"poll_ms":10,"limits":null});
    let mut child = Command::new(worker())
        .args([
            "process-worker",
            tofu_runtime::process_supervisor::REQUEST_FORMAT,
            "65536",
        ])
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(&serde_json::to_vec(&config).unwrap())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let header = u32::from_be_bytes(output.stdout[..4].try_into().unwrap()) as usize;
    let metadata: serde_json::Value =
        serde_json::from_slice(&output.stdout[4..4 + header]).unwrap();
    assert_eq!(metadata["reaped"], true);
    assert_eq!(&output.stdout[4 + header..], b"9\n");
}
#[test]
fn memory_limit_blocks_large_allocation() {
    let mut req=python_request("import sys;\ntry: data=bytearray(128*1024*1024)\nexcept MemoryError: print('bounded'); sys.exit(3)\nelse: sys.exit(99)");
    req.limits = Some(limits());
    let output = execute(worker(), &req, || false).unwrap();
    assert_eq!(output.stdout, b"bounded\n");
    assert_eq!(output.metadata["exit_code"], 3);
    assert_eq!(output.metadata["timed_out"], false);
}
#[test]
fn file_size_limit_prevents_output_growth() {
    let scratch = Scratch::new();
    let mut req=python_request("import os,sys;\ntry:\n f=open('bounded-output','wb');f.write(b'x'*65536);f.flush()\nexcept OSError as e: print(e.errno);sys.exit(4)\nelse:sys.exit(99)");
    req.directory = scratch.0.clone();
    req.limits = Some(limits());
    let output = execute(worker(), &req, || false).unwrap();
    assert_eq!(output.stdout, b"27\n");
    assert_eq!(output.metadata["exit_code"], 4);
    assert!(
        std::fs::metadata(scratch.0.join("bounded-output"))
            .unwrap()
            .len()
            <= 4096
    );
}
#[test]
fn cpu_limit_is_distinct_from_wall_timeout() {
    let mut req = python_request("while True: pass");
    let mut budget = limits();
    budget.cpu_seconds = 1;
    req.limits = Some(budget);
    let output = execute(worker(), &req, || false).unwrap();
    assert_eq!(output.metadata["signal"], libc::SIGKILL);
    assert_eq!(output.metadata["timed_out"], false);
    assert_eq!(output.metadata["cancelled"], false);
    assert_eq!(output.metadata["reaped"], true);
}
#[test]
fn descriptor_limit_bounds_new_opens() {
    let mut req=python_request("import os;fds=[];\ntry:\n while True: fds.append(os.open('/dev/null',os.O_RDONLY))\nexcept OSError as e: print(e.errno, len(fds))");
    req.limits = Some(limits());
    let output = execute(worker(), &req, || false).unwrap();
    let text = String::from_utf8(output.stdout).unwrap();
    let fields: Vec<_> = text.split_whitespace().collect();
    assert_eq!(fields[0], "24");
    assert!(fields[1].parse::<usize>().unwrap() <= 29);
    assert_eq!(output.metadata["exit_code"], 0);
}
#[test]
fn invalid_or_unavailable_limits_refuse_execution() {
    let mut req = request("exit 0");
    let mut budget = limits();
    budget.cpu_seconds = 0;
    req.limits = Some(budget);
    assert!(matches!(
        execute(worker(), &req, || false),
        Err(Error::InvalidRequest)
    ));
    budget.cpu_seconds = 1;
    budget.file_descriptors = u64::MAX;
    req.limits = Some(budget);
    assert!(matches!(
        execute(worker(), &req, || false),
        Err(Error::InvalidRequest)
    ));
    budget.file_descriptors = u64::MAX - 1;
    req.limits = Some(budget);
    assert!(matches!(
        execute(worker(), &req, || false),
        Err(Error::WorkerFailed)
    ));
}

#[test]
fn protocol_skew_refuses_before_tool_launch() {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let scratch = Scratch::new();
    let path = scratch.file();
    for (argument, body) in [
        ("65536", "tofu.native-process-request/v1"),
        (
            tofu_runtime::process_supervisor::REQUEST_FORMAT,
            "tofu.native-process-request/v999",
        ),
    ] {
        let config = serde_json::json!({"format":body,"executable":"/bin/sh","arguments":["-c","touch \"$1\"","tofu-test",path],"directory":"/tmp","environment":{},"stdout_bytes":1024,"stderr_bytes":1024,"timeout_ms":3000,"grace_ms":20,"poll_ms":10,"limits":null});
        let mut child = Command::new(worker())
            .args(["process-worker", argument, "65536"])
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        // A version refusal may close stdin before the writer; both races refuse.
        let _write = child
            .stdin
            .take()
            .unwrap()
            .write_all(&serde_json::to_vec(&config).unwrap());
        let output = child.wait_with_output().unwrap();
        assert!(!output.status.success());
        assert!(!path.exists());
        assert!(output.stdout.is_empty());
    }
}
