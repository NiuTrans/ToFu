//! Cgroup namespace/ancestor fixtures and resource-sensitive native admission.
use std::{
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
};
use tofu_runtime::capacity::{cgroup_directories, probe_linux, Demand, Error, Snapshot};
static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "tofu-capacity-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn write(&self, path: &str, text: &str) {
        let path = self.0.join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    fn setup(&self, kind: &str, member: &str, root: &str) {
        self.write("proc/self/cgroup", member);
        self.write(
            "proc/self/mountinfo",
            &format!(
                "1 0 0:1 {root} {} rw - {kind} cgroup rw,cpu,memory\n",
                self.0.join("group").display()
            ),
        );
        self.write(
            "proc/meminfo",
            "MemTotal: 32768 kB\nMemAvailable: 16384 kB\n",
        );
        fs::create_dir_all(self.0.join("group/child")).unwrap();
    }
    fn probe(&self) -> Snapshot {
        probe_linux(&self.0.join("proc"), &self.0)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
#[test]
fn unified_membership_observes_ancestor_quota_and_memory_headroom() {
    let f = Fixture::new();
    f.setup("cgroup2", "0::/namespace/child\n", "/namespace");
    f.write("group/cpu.max", "150000 100000");
    f.write("group/child/cpu.max", "max 100000");
    f.write("group/memory.max", "8388608");
    f.write("group/memory.current", "7340032");
    f.write("group/child/memory.max", "max");
    f.write("group/child/memory.current", "0");
    let s = f.probe();
    assert_eq!(s.quota_cpus, Some(1));
    assert_eq!(s.effective_cpus, 1);
    assert_eq!(s.memory_capacity_bytes, Some(8388608));
    assert_eq!(s.memory_available_bytes, Some(1048576));
    assert!(s.volume_available_bytes.unwrap() <= s.volume_total_bytes.unwrap());
    f.write("group/memory.current", "9000000");
    assert_eq!(f.probe().memory_available_bytes, Some(0));
}
#[test]
fn legacy_controller_and_unlimited_sentinel_are_handled() {
    let f = Fixture::new();
    f.setup("cgroup", "2:cpu,memory:/child\n", "/");
    f.write("group/cpu.cfs_quota_us", "250000");
    f.write("group/cpu.cfs_period_us", "100000");
    f.write("group/memory.limit_in_bytes", "9223372036854771712");
    f.write("group/child/memory.limit_in_bytes", "4194304");
    f.write("group/child/memory.usage_in_bytes", "1048576");
    let s = f.probe();
    assert_eq!(s.quota_cpus, Some(2));
    assert_eq!(s.memory_capacity_bytes, Some(4194304));
    assert_eq!(s.memory_available_bytes, Some(3145728));
}
#[test]
fn missing_or_malformed_usage_does_not_invent_available_memory() {
    let f = Fixture::new();
    f.setup("cgroup2", "0::/child\n", "/");
    f.write("group/memory.max", "4194304");
    assert_eq!(f.probe().memory_available_bytes, Some(0));
    f.write("group/memory.current", "invalid");
    assert_eq!(f.probe().memory_available_bytes, Some(0));
    fs::remove_file(f.0.join("proc/meminfo")).unwrap();
    fs::remove_file(f.0.join("group/memory.max")).unwrap();
    assert_eq!(f.probe().memory_available_bytes, None);
}
#[test]
fn escaped_mounts_and_component_prefixes_do_not_escape_root() {
    let dirs = cgroup_directories(
        "0::/root/child\n",
        "1 0 0:1 /root /mount\\040space rw - cgroup2 cgroup rw\n",
        "cpu",
    );
    assert_eq!(
        dirs,
        vec![
            PathBuf::from("/mount space"),
            PathBuf::from("/mount space/child")
        ]
    );
    for membership in ["0::/root-other/child", "0::/root/../escape", "0::relative"] {
        assert!(cgroup_directories(
            membership,
            "1 0 0:1 /root /mounted rw - cgroup2 cgroup rw",
            "cpu"
        )
        .is_empty());
    }
}
fn snapshot(cpus: usize, bytes: Option<u64>) -> Snapshot {
    Snapshot {
        host_cpus: cpus,
        affinity_cpus: Some(cpus),
        quota_cpus: None,
        effective_cpus: cpus,
        memory_capacity_bytes: bytes,
        memory_available_bytes: bytes,
        volume_total_bytes: None,
        volume_available_bytes: None,
    }
}
fn demand() -> Demand {
    Demand {
        worker_bytes: 1024,
        job_bytes: 256,
        queued_jobs_per_worker: 4,
        memory_divisor: 2,
        cpu_units_per_worker: 1000,
    }
}
#[test]
fn native_idle_budget_scales_with_probe_and_falls_back_lean_without_headroom() {
    let small = snapshot(2, Some(8192)).executor_budget(demand()).unwrap();
    assert_eq!(small.workers, 2);
    assert_eq!(small.max_inflight_jobs, 8);
    let tool = snapshot(2, Some(8192)).tool_budget(demand()).unwrap();
    assert_eq!(
        (tool.workers, tool.inflight_calls, tool.inflight_bytes),
        (2, 8, 2048)
    );
    let large = snapshot(128, Some(1048576))
        .executor_budget(demand())
        .unwrap();
    assert_eq!(large.workers, 128);
    assert_eq!(large.max_inflight_jobs, 512);
    assert_eq!(large.max_inflight_bytes, 131072);
    let pressured = snapshot(128, Some(8192)).executor_budget(demand()).unwrap();
    assert_eq!(pressured.workers, 2);
    assert!(snapshot(128, Some(0)).executor_budget(demand()).is_ok());
    let lean = snapshot(128, None).executor_budget(demand()).unwrap();
    assert_eq!((lean.workers, lean.max_inflight_jobs), (1, 1));
    let invalid = Demand {
        job_bytes: usize::MAX,
        ..demand()
    };
    assert!(matches!(
        snapshot(128, Some(10000)).executor_budget(invalid),
        Err(Error::Overflow)
    ));
}
#[test]
fn real_linux_probe_and_owned_executor_close() {
    let s = probe_linux(Path::new("/proc"), Path::new("/tmp"));
    assert!(s.effective_cpus >= 1);
    assert!(s.effective_cpus <= s.host_cpus);
    if let Some(n) = s.affinity_cpus {
        assert!(s.effective_cpus <= n)
    }
    let budget = snapshot(2, Some(8192)).executor_budget(demand()).unwrap();
    let mut pool = tofu_runtime::executor::Executor::new(budget).unwrap();
    let jobs = (0..8)
        .map(|i| pool.submit(256, move || i).unwrap())
        .collect::<Vec<_>>();
    for (i, job) in jobs.into_iter().enumerate() {
        assert_eq!(job.wait().unwrap(), i)
    }
    pool.close();
    assert_eq!(pool.snapshot().unwrap().inflight_bytes, 0);
}

#[test]
fn unreadable_hierarchy_and_oversized_proc_input_fall_back_lean() {
    let f = Fixture::new();
    f.setup("cgroup2", "0::/outside/child\n", "/namespace");
    assert_eq!(f.probe().effective_cpus, 1);
    f.write("proc/self/mountinfo", &"x".repeat(1024 * 1024 + 1));
    assert_eq!(f.probe().effective_cpus, 1);
    fs::remove_file(f.0.join("proc/self/cgroup")).unwrap();
    assert_eq!(f.probe().effective_cpus, 1);
}

#[test]
fn zero_cgroup_memory_limit_is_known_exhaustion() {
    let f = Fixture::new();
    f.setup("cgroup2", "0::/child\n", "/");
    f.write("group/memory.max", "0");
    f.write("group/memory.current", "0");
    let s = f.probe();
    assert_eq!(s.memory_capacity_bytes, Some(0));
    assert_eq!(s.memory_available_bytes, Some(0));
    assert!(s.executor_budget(demand()).is_ok());
}
