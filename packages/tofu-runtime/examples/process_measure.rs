//! Disposable native supervision measurement; not application admission.
#[cfg(target_os = "linux")]
fn main() {
    use std::{
        collections::BTreeMap,
        path::Path,
        time::{Duration, Instant},
    };
    use tofu_runtime::process_supervisor::{execute, Request};
    let args: Vec<_> = std::env::args().collect();
    let count: usize = if args[2] == "parent-loss" {
        1
    } else {
        args[2].parse().unwrap()
    };
    let mut req = Request {
        executable: "/bin/true".into(),
        arguments: vec![],
        directory: "/tmp".into(),
        environment: BTreeMap::new(),
        stdout_bytes: 128,
        stderr_bytes: 128,
        timeout: Duration::from_secs(3),
        termination_grace: Duration::from_millis(20),
        poll_interval: Duration::from_millis(10),
        config_bytes: 65536,
        limits: None,
    };
    if args[2] == "parent-loss" {
        req.executable = "/bin/sh".into();
        req.arguments = vec![
            "-c".into(),
            "trap '' TERM; echo $$ $PPID > \"$1\"; while :; do sleep 1; done".into(),
            "tofu-test".into(),
            args[3].clone(),
        ];
        req.timeout = Duration::from_secs(30);
    }
    let mut samples = Vec::with_capacity(count);
    for _ in 0..count {
        let start = Instant::now();
        let result = execute(Path::new(&args[1]), &req, || false).unwrap();
        assert_eq!(result.metadata["exit_code"], 0);
        samples.push(start.elapsed().as_nanos() as u64);
    }
    // Separate measurement mode reads OS resource counters after all samples.
    let mut own: libc::rusage = unsafe { std::mem::zeroed() };
    let mut children: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe {
        assert_eq!(libc::getrusage(libc::RUSAGE_SELF, &mut own), 0);
        assert_eq!(libc::getrusage(libc::RUSAGE_CHILDREN, &mut children), 0);
    }
    let micros = |t: libc::timeval| t.tv_sec * 1_000_000 + t.tv_usec;
    // VmHWM resets at exec; rusage can retain the launcher's pre-exec high-water.
    let status = std::fs::read_to_string("/proc/self/status").unwrap();
    let own_hwm_kib: u64 = status
        .lines()
        .find_map(|line| {
            line.strip_prefix("VmHWM:")
                .and_then(|value| value.split_whitespace().next())
                .and_then(|value| value.parse().ok())
        })
        .unwrap();
    println!(
        "{}",
        serde_json::json!({"samples_ns":samples,"self_peak_rss_kib":own.ru_maxrss,"self_vm_hwm_kib":own_hwm_kib,
        "child_peak_rss_kib":children.ru_maxrss,"self_cpu_us":micros(own.ru_utime)+micros(own.ru_stime),
        "children_cpu_us":micros(children.ru_utime)+micros(children.ru_stime),"completed":count})
    );
}
#[cfg(not(target_os = "linux"))]
fn main() {
    panic!("Linux supervision measurement only");
}
