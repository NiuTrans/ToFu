//! Explicit launch-time Linux capacity snapshot; no globals, workers or timers.
//! Composition captures once for its selected volume and shares the immutable
//! result. Cgroup ancestor ceilings and current headroom constrain host values.
use std::{
    collections::BTreeSet,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
};
const INPUT_BYTES: u64 = 1024 * 1024;
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub host_cpus: usize,
    pub affinity_cpus: Option<usize>,
    pub quota_cpus: Option<usize>,
    pub effective_cpus: usize,
    pub memory_capacity_bytes: Option<u64>,
    pub memory_available_bytes: Option<u64>,
    pub volume_total_bytes: Option<u64>,
    pub volume_available_bytes: Option<u64>,
}
#[derive(Clone, Copy, Debug)]
pub struct Demand {
    pub worker_bytes: usize,
    pub job_bytes: usize,
    pub queued_jobs_per_worker: usize,
    /// Share of measured headroom granted to this composition's pool.
    pub memory_divisor: usize,
    /// Estimated CPU occupancy per worker, in thousandths of one CPU.
    /// CPU work uses 1000; lower values require measured waiting workloads.
    pub cpu_units_per_worker: usize,
}
#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    InvalidDemand,
    NoCapacity,
    Overflow,
    Closed,
    StateUnavailable,
}
impl Snapshot {
    pub fn executor_budget(&self, demand: Demand) -> Result<crate::executor::Budget, Error> {
        let units = self
            .effective_cpus
            .max(1)
            .checked_mul(1000)
            .ok_or(Error::Overflow)?;
        self.budget_with_cpu_units(demand, units)
    }
    pub(crate) fn budget_with_cpu_units(
        &self,
        demand: Demand,
        cpu_units: usize,
    ) -> Result<crate::executor::Budget, Error> {
        if demand.worker_bytes == 0
            || demand.job_bytes == 0
            || demand.queued_jobs_per_worker == 0
            || demand.memory_divisor == 0
            || demand.cpu_units_per_worker == 0
            || demand.cpu_units_per_worker > 1000
        {
            return Err(Error::InvalidDemand);
        }
        let unit = demand
            .job_bytes
            .checked_mul(demand.queued_jobs_per_worker)
            .and_then(|n| n.checked_add(demand.worker_bytes))
            .ok_or(Error::Overflow)?;
        let workers = match self.memory_available_bytes {
            Some(bytes) => (cpu_units / demand.cpu_units_per_worker).min(
                usize::try_from(bytes / demand.memory_divisor as u64 / unit as u64)
                    .map_err(|_| Error::Overflow)?,
            ),
            None => usize::from(cpu_units >= demand.cpu_units_per_worker),
        };
        // Probe values size idle reuse, never refuse a new composition.
        let workers = workers.max(1);
        let jobs = if self.memory_available_bytes.is_none() {
            1
        } else {
            workers
                .checked_mul(demand.queued_jobs_per_worker)
                .ok_or(Error::Overflow)?
        };
        Ok(crate::executor::Budget {
            workers,
            max_inflight_jobs: jobs,
            max_inflight_bytes: jobs.checked_mul(demand.job_bytes).ok_or(Error::Overflow)?,
        })
    }
    pub fn tool_budget(&self, demand: Demand) -> Result<crate::tool_scheduler::Budget, Error> {
        let budget = self.executor_budget(demand)?;
        Ok(crate::tool_scheduler::Budget {
            workers: budget.workers,
            inflight_calls: budget.max_inflight_jobs,
            inflight_bytes: budget.max_inflight_bytes,
        })
    }
    pub fn to_value(&self) -> serde_json::Value {
        serde_json::json!({"host_cpus":self.host_cpus,"affinity_cpus":self.affinity_cpus,"quota_cpus":self.quota_cpus,"effective_cpus":self.effective_cpus,"memory_capacity_bytes":self.memory_capacity_bytes,"memory_available_bytes":self.memory_available_bytes,"volume_total_bytes":self.volume_total_bytes,"volume_available_bytes":self.volume_available_bytes})
    }
}
fn read(path: &Path) -> Option<String> {
    let mut value = String::new();
    File::open(path)
        .ok()?
        .take(INPUT_BYTES + 1)
        .read_to_string(&mut value)
        .ok()?;
    (value.len() as u64 <= INPUT_BYTES).then_some(value)
}
fn positive(s: &str) -> Option<u64> {
    s.trim()
        .parse::<u64>()
        .ok()
        .filter(|n| *n > 0 && *n < (1 << 60))
}
fn minimum(a: Option<u64>, b: Option<u64>) -> Option<u64> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, b) => a.or(b),
    }
}
fn decode(s: &str) -> String {
    s.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
}
/// Resolve mounted membership roots, then include every visible ancestor.
/// Absolute roots/members and component-aware prefix checks forbid traversal.
pub fn cgroup_directories(membership: &str, mounts: &str, controller: &str) -> Vec<PathBuf> {
    let mut result = BTreeSet::new();
    for line in mounts.lines() {
        let Some((before, after)) = line.split_once(" - ") else {
            continue;
        };
        let fields: Vec<_> = before.split_whitespace().collect();
        let fs: Vec<_> = after.split_whitespace().collect();
        if fields.len() < 5 || fs.len() < 3 {
            continue;
        }
        let unified = fs[0] == "cgroup2";
        if !unified && (fs[0] != "cgroup" || !fs[2].split(',').any(|s| s == controller)) {
            continue;
        }
        let root = PathBuf::from(decode(fields[3]));
        let mount = PathBuf::from(decode(fields[4]));
        if !safe_absolute(&root) || !safe_absolute(&mount) {
            continue;
        }
        for member in membership.lines() {
            let parts: Vec<_> = member.splitn(3, ':').collect();
            if parts.len() != 3 || unified != parts[1].is_empty() {
                continue;
            }
            if !unified && !parts[1].split(',').any(|s| s == controller) {
                continue;
            }
            let path = Path::new(parts[2]);
            if !safe_absolute(path) {
                continue;
            }
            let Ok(relative) = path.strip_prefix(&root) else {
                continue;
            };
            let mut current = mount.join(relative);
            loop {
                result.insert(current.clone());
                if current == mount || !current.pop() {
                    break;
                }
            }
        }
    }
    result.into_iter().collect()
}
fn safe_absolute(path: &Path) -> bool {
    path.is_absolute()
        && !path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
}
fn quota(directory: &Path) -> Option<usize> {
    let v2 = read(&directory.join("cpu.max"));
    if let Some(value) = v2 {
        let fields: Vec<_> = value.split_whitespace().collect();
        if fields.len() == 2 {
            if fields[0] == "max" {
                return None;
            }
            if let (Some(q), Some(p)) = (positive(fields[0]), positive(fields[1])) {
                return usize::try_from((q / p).max(1)).ok();
            }
        }
    }
    let q = positive(&read(&directory.join("cpu.cfs_quota_us"))?)?;
    let p = positive(&read(&directory.join("cpu.cfs_period_us"))?)?;
    usize::try_from((q / p).max(1)).ok()
}
/// Injectable proc paths expose the same resolver to fixture and live probes.
pub fn probe_linux(proc_root: &Path, volume: &Path) -> Snapshot {
    let host_cpus = host_cpus();
    let affinity_cpus = affinity();
    let membership_input = read(&proc_root.join("self/cgroup"));
    let mounts_input = read(&proc_root.join("self/mountinfo"));
    let hierarchy_known = membership_input.is_some() && mounts_input.is_some();
    let membership = membership_input.unwrap_or_default();
    let mounts = mounts_input.unwrap_or_default();
    let cpu_directories = cgroup_directories(&membership, &mounts, "cpu");
    let quota_cpus = cpu_directories.iter().filter_map(|d| quota(d)).min();
    let effective_cpus = if hierarchy_known && !cpu_directories.is_empty() {
        [Some(host_cpus), affinity_cpus, quota_cpus]
            .into_iter()
            .flatten()
            .min()
            .unwrap_or(1)
            .max(1)
    } else {
        1
    };
    let meminfo = read(&proc_root.join("meminfo")).unwrap_or_default();
    let field = |name: &str| -> Option<u64> {
        meminfo.lines().find_map(|line| {
            let (key, value) = line.split_once(':')?;
            if key != name {
                return None;
            }
            let fields: Vec<_> = value.split_whitespace().collect();
            if fields.len() != 2 || fields[1] != "kB" {
                return None;
            }
            fields[0].parse::<u64>().ok()?.checked_mul(1024)
        })
    };
    let mut capacity = field("MemTotal");
    let mut available = field("MemAvailable");
    for directory in cgroup_directories(&membership, &mounts, "memory") {
        let limit = read(&directory.join("memory.max"))
            .or_else(|| read(&directory.join("memory.limit_in_bytes")))
            .and_then(|s| s.trim().parse::<u64>().ok().filter(|n| *n < (1 << 60)));
        let current = read(&directory.join("memory.current"))
            .or_else(|| read(&directory.join("memory.usage_in_bytes")))
            .and_then(|s| s.trim().parse::<u64>().ok());
        capacity = minimum(capacity, limit);
        // Missing current usage cannot establish cgroup headroom: fail lean.
        if let Some(limit) = limit {
            available = minimum(
                available,
                Some(current.map(|n| limit.saturating_sub(n)).unwrap_or(0)),
            );
        }
    }
    if let (Some(headroom), Some(limit)) = (available, capacity) {
        available = Some(headroom.min(limit));
    }
    let (volume_total_bytes, volume_available_bytes) = volume_bytes(volume);
    Snapshot {
        host_cpus,
        affinity_cpus,
        quota_cpus,
        effective_cpus,
        memory_capacity_bytes: capacity,
        memory_available_bytes: available,
        volume_total_bytes,
        volume_available_bytes,
    }
}
#[cfg(target_os = "linux")]
fn host_cpus() -> usize {
    usize::try_from(unsafe { libc::sysconf(libc::_SC_NPROCESSORS_ONLN) })
        .ok()
        .filter(|n| *n > 0)
        .unwrap_or(1)
}
#[cfg(not(target_os = "linux"))]
fn host_cpus() -> usize {
    1
}
#[cfg(target_os = "linux")]
fn affinity() -> Option<usize> {
    let mut bytes = 128;
    while bytes <= 65536 {
        let mut mask = vec![0usize; bytes / std::mem::size_of::<usize>()];
        let status = unsafe { libc::sched_getaffinity(0, bytes, mask.as_mut_ptr().cast()) };
        if status == 0 {
            return Some(mask.iter().map(|n| n.count_ones() as usize).sum::<usize>())
                .filter(|n| *n > 0);
        }
        if std::io::Error::last_os_error().raw_os_error() != Some(libc::EINVAL) {
            return None;
        }
        bytes *= 2;
    }
    None
}
#[cfg(not(target_os = "linux"))]
fn affinity() -> Option<usize> {
    None
}
#[cfg(unix)]
// statvfs fields are platform-sized; widening is needed on 32-bit Unix.
#[allow(clippy::unnecessary_cast)]
fn volume_bytes(path: &Path) -> (Option<u64>, Option<u64>) {
    use std::os::unix::ffi::OsStrExt;
    let Ok(path) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return (None, None);
    };
    let mut value = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), value.as_mut_ptr()) } != 0 {
        return (None, None);
    }
    let value = unsafe { value.assume_init() };
    (
        (value.f_blocks as u64).checked_mul(value.f_frsize as u64),
        (value.f_bavail as u64).checked_mul(value.f_frsize as u64),
    )
}
#[cfg(not(unix))]
fn volume_bytes(_: &Path) -> (Option<u64>, Option<u64>) {
    (None, None)
}
