//! One-shot launch resource observation and bounded daemon budget derivation.
//!
//! The production probe reads CPU affinity/quota, host/cgroup memory and volume
//! headroom once. Missing core observations select a fixed lean profile; no
//! background sampler or unbounded system-file read is created.

use std::fs::File;
use std::io::{self, Read};
use std::path::{Component, Path, PathBuf};
use std::thread;

use crate::generated_storage_v2::{MAX_CONNECTIONS, MAX_IN_FLIGHT_FRAME_BYTES};
use crate::generated_tofudb_ir::SEARCH_PROJECTION_VOLUME_FREE_PERCENT;
pub use crate::generated_tofudb_ir::{MAX_SEARCH_PROJECTION_BYTES, MIN_SEARCH_PROJECTION_BYTES};
use crate::protocol::MAX_FRAME_BODY_BYTES;

const MIB: u64 = 1024 * 1024;
const MAX_SYSTEM_FILE_BYTES: u64 = 16 * 1024;
const MIN_FRAME_BUDGET_BYTES: usize = MAX_FRAME_BODY_BYTES;
const LEAN_CONNECTIONS: usize = 4;
const BYTES_PER_CONNECTION: u64 = 16 * MIB;
pub const CONNECTION_STACK_BYTES: usize = 1024 * 1024;
pub use crate::generated_storage_v2::MIN_WRITABLE_VOLUME_FREE_BYTES;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LaunchResourceSnapshot {
    pub logical_cpus: Option<usize>,
    pub memory_capacity_bytes: Option<u64>,
    pub memory_headroom_bytes: Option<u64>,
    pub volume_free_bytes: Option<u64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DaemonResourceBudget {
    pub maximum_connections: usize,
    pub maximum_frame_bytes: usize,
    pub connection_stack_bytes: usize,
    pub search_projection_maximum_bytes_per_owner: u64,
    pub timer_live_capacity: usize,
    pub snapshot: LaunchResourceSnapshot,
    pub used_lean_fallback: bool,
}

impl DaemonResourceBudget {
    pub fn from_snapshot(snapshot: LaunchResourceSnapshot) -> Self {
        let Some(logical_cpus) = snapshot.logical_cpus.filter(|value| *value > 0) else {
            return Self::lean(snapshot);
        };
        if snapshot.memory_capacity_bytes.is_none() || snapshot.volume_free_bytes.is_none() {
            return Self::lean(snapshot);
        }
        let Some(memory_headroom_bytes) = snapshot.memory_headroom_bytes else {
            return Self::lean(snapshot);
        };
        let memory_connections = usize::try_from(memory_headroom_bytes / BYTES_PER_CONNECTION)
            .unwrap_or(usize::MAX)
            .max(1);
        let maximum_connections = logical_cpus
            .saturating_mul(4)
            .min(memory_connections)
            .clamp(1, MAX_CONNECTIONS);
        let maximum_frame_bytes = usize::try_from(memory_headroom_bytes / 32)
            .unwrap_or(usize::MAX)
            .clamp(MIN_FRAME_BUDGET_BYTES, MAX_IN_FLIGHT_FRAME_BYTES);
        Self {
            maximum_connections,
            maximum_frame_bytes,
            connection_stack_bytes: CONNECTION_STACK_BYTES,
            search_projection_maximum_bytes_per_owner: snapshot
                .volume_free_bytes
                .unwrap_or(MIN_SEARCH_PROJECTION_BYTES)
                .saturating_mul(SEARCH_PROJECTION_VOLUME_FREE_PERCENT)
                .saturating_div(100)
                .clamp(MIN_SEARCH_PROJECTION_BYTES, MAX_SEARCH_PROJECTION_BYTES),
            timer_live_capacity: timer_live_capacity(logical_cpus),
            snapshot,
            used_lean_fallback: false,
        }
    }

    pub fn has_volume_pressure(&self) -> bool {
        self.snapshot
            .volume_free_bytes
            .is_some_and(|bytes| bytes < MIN_WRITABLE_VOLUME_FREE_BYTES)
    }

    fn lean(snapshot: LaunchResourceSnapshot) -> Self {
        Self {
            maximum_connections: LEAN_CONNECTIONS,
            maximum_frame_bytes: 16 * MIB as usize,
            connection_stack_bytes: CONNECTION_STACK_BYTES,
            search_projection_maximum_bytes_per_owner: MIN_SEARCH_PROJECTION_BYTES,
            timer_live_capacity: timer_live_capacity(4),
            snapshot,
            used_lean_fallback: true,
        }
    }
}

fn timer_live_capacity(logical_cpus: usize) -> usize {
    let probed = logical_cpus.saturating_mul(2).clamp(8, 16);
    std::env::var("TOFU_TIMER_LIVE_CAP")
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(probed)
        .min(crate::generated_tofudb_ir::MAX_ACTIVE_TIMERS_PER_OWNER_HARD_CEILING)
}

pub fn probe_launch_resources(volume_path: &Path) -> LaunchResourceSnapshot {
    let affinity_cpus = thread::available_parallelism()
        .ok()
        .map(|value| value.get());
    let cgroup_cpus = cgroup_cpu_limit();
    let logical_cpus = minimum_present(affinity_cpus, cgroup_cpus);
    let host_memory = host_memory_observation();
    let cgroup_memory = cgroup_memory_observation();
    let memory_capacity_bytes = minimum_present(
        host_memory.map(|value| value.0),
        cgroup_memory.map(|value| value.0),
    );
    let memory_headroom_bytes = minimum_present(
        host_memory.map(|value| value.1),
        cgroup_memory.map(|value| value.0.saturating_sub(value.1)),
    );
    let volume_free_bytes = fs2::available_space(volume_path).ok();
    LaunchResourceSnapshot {
        logical_cpus,
        memory_capacity_bytes,
        memory_headroom_bytes,
        volume_free_bytes,
    }
}

fn minimum_present<T: Ord + Copy>(first: Option<T>, second: Option<T>) -> Option<T> {
    match (first, second) {
        (Some(first), Some(second)) => Some(first.min(second)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

fn host_memory_observation() -> Option<(u64, u64)> {
    let document = read_bounded(Path::new("/proc/meminfo")).ok()?;
    let total = meminfo_bytes(&document, "MemTotal:")?;
    let available = meminfo_bytes(&document, "MemAvailable:")?;
    Some((total, available.min(total)))
}

fn meminfo_bytes(document: &str, field: &str) -> Option<u64> {
    let line = document.lines().find(|line| line.starts_with(field))?;
    let kibibytes = line[field.len()..]
        .trim()
        .strip_suffix(" kB")?
        .trim()
        .parse::<u64>()
        .ok()?;
    kibibytes.checked_mul(1024)
}

fn cgroup_cpu_limit() -> Option<usize> {
    cgroup_v2_cpu_limit().or_else(cgroup_v1_cpu_limit)
}

fn cgroup_v2_cpu_limit() -> Option<usize> {
    let document = read_bounded(&unified_cgroup_directory()?.join("cpu.max")).ok()?;
    parse_cpu_quota(&document)
}

fn parse_cpu_quota(document: &str) -> Option<usize> {
    let mut fields = document.split_ascii_whitespace();
    let quota = fields.next()?;
    let period = fields.next()?.parse::<u64>().ok()?;
    if quota == "max" || period == 0 || fields.next().is_some() {
        return None;
    }
    let quota = quota.parse::<u64>().ok()?;
    usize::try_from(quota.saturating_add(period - 1) / period)
        .ok()
        .filter(|value| *value > 0)
}

fn cgroup_v1_cpu_limit() -> Option<usize> {
    let directory = legacy_cgroup_directory("cpu")?;
    let quota = read_bounded(&directory.join("cpu.cfs_quota_us"))
        .ok()?
        .trim()
        .parse::<i64>()
        .ok()?;
    let period = read_scalar(&directory.join("cpu.cfs_period_us"))?;
    if quota <= 0 || period == 0 {
        return None;
    }
    usize::try_from((quota as u64).saturating_add(period - 1) / period)
        .ok()
        .filter(|value| *value > 0)
}

fn cgroup_memory_observation() -> Option<(u64, u64)> {
    cgroup_v2_memory_observation().or_else(cgroup_v1_memory_observation)
}

fn cgroup_v2_memory_observation() -> Option<(u64, u64)> {
    let directory = unified_cgroup_directory()?;
    let maximum = read_scalar(&directory.join("memory.max"))?;
    let current = read_scalar(&directory.join("memory.current"))?;
    Some((maximum, current.min(maximum)))
}

fn cgroup_v1_memory_observation() -> Option<(u64, u64)> {
    let directory = legacy_cgroup_directory("memory")?;
    let maximum = read_scalar(&directory.join("memory.limit_in_bytes"))?;
    let current = read_scalar(&directory.join("memory.usage_in_bytes"))?;
    Some((maximum, current.min(maximum)))
}

fn unified_cgroup_directory() -> Option<PathBuf> {
    let membership = read_bounded(Path::new("/proc/self/cgroup")).ok()?;
    let relative = membership.lines().find_map(|line| {
        let mut fields = line.splitn(3, ':');
        if fields.next()? == "0" && fields.next()?.is_empty() {
            sanitize_cgroup_path(fields.next()?)
        } else {
            None
        }
    })?;
    Some(Path::new("/sys/fs/cgroup").join(relative))
}

fn legacy_cgroup_directory(controller: &str) -> Option<PathBuf> {
    let membership = read_bounded(Path::new("/proc/self/cgroup")).ok()?;
    let relative = membership.lines().find_map(|line| {
        let mut fields = line.splitn(3, ':');
        fields.next()?;
        let controllers = fields.next()?;
        if controllers
            .split(',')
            .any(|candidate| candidate == controller)
        {
            sanitize_cgroup_path(fields.next()?)
        } else {
            None
        }
    })?;
    Some(Path::new("/sys/fs/cgroup").join(controller).join(relative))
}

fn sanitize_cgroup_path(raw: &str) -> Option<PathBuf> {
    let mut result = PathBuf::new();
    for component in Path::new(raw).components() {
        match component {
            Component::RootDir => {}
            Component::Normal(value) => result.push(value),
            _ => return None,
        }
    }
    Some(result)
}

fn read_scalar(path: &Path) -> Option<u64> {
    let value = read_bounded(path).ok()?;
    let value = value.trim();
    if value == "max" {
        None
    } else {
        value.parse().ok()
    }
}

fn read_bounded(path: &Path) -> io::Result<String> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take(MAX_SYSTEM_FILE_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_SYSTEM_FILE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "system resource file exceeded its bound",
        ));
    }
    String::from_utf8(bytes)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid system resource file"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budgets_fall_back_lean_and_clamp_observed_extremes() {
        let missing = LaunchResourceSnapshot::default();
        let lean = DaemonResourceBudget::from_snapshot(missing);
        assert!(lean.used_lean_fallback);
        assert_eq!(lean.maximum_connections, LEAN_CONNECTIONS);
        assert_eq!(lean.maximum_frame_bytes, 16 * MIB as usize);
        assert_eq!(
            lean.search_projection_maximum_bytes_per_owner,
            MIN_SEARCH_PROJECTION_BYTES
        );

        let pressured = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(64),
            memory_capacity_bytes: Some(1024 * MIB),
            memory_headroom_bytes: Some(1),
            volume_free_bytes: Some(MIN_WRITABLE_VOLUME_FREE_BYTES),
        });
        assert!(!pressured.used_lean_fallback);
        assert_eq!(pressured.maximum_connections, 1);
        assert_eq!(pressured.maximum_frame_bytes, MAX_FRAME_BODY_BYTES);

        let huge = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(usize::MAX),
            memory_capacity_bytes: Some(u64::MAX),
            memory_headroom_bytes: Some(u64::MAX),
            volume_free_bytes: Some(u64::MAX),
        });
        assert_eq!(huge.maximum_connections, MAX_CONNECTIONS);
        assert_eq!(huge.maximum_frame_bytes, MAX_IN_FLIGHT_FRAME_BYTES);
        assert_eq!(
            huge.search_projection_maximum_bytes_per_owner,
            MAX_SEARCH_PROJECTION_BYTES
        );

        let disk_pressure = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(1),
            memory_capacity_bytes: Some(1024 * MIB),
            memory_headroom_bytes: Some(512 * MIB),
            volume_free_bytes: Some(MIN_WRITABLE_VOLUME_FREE_BYTES - 1),
        });
        assert!(disk_pressure.has_volume_pressure());
    }

    #[test]
    fn parsing_rejects_overflow_and_path_traversal() {
        assert_eq!(
            meminfo_bytes("MemTotal: 8192 kB\n", "MemTotal:"),
            Some(8 * MIB)
        );
        assert!(meminfo_bytes("MemTotal: 18446744073709551615 kB", "MemTotal:").is_none());
        assert_eq!(
            sanitize_cgroup_path("/user.slice/tofu"),
            Some(PathBuf::from("user.slice/tofu"))
        );
        assert!(sanitize_cgroup_path("/../../authority").is_none());
        assert_eq!(parse_cpu_quota("150000 100000"), Some(2));
        assert_eq!(parse_cpu_quota("max 100000"), None);
        assert_eq!(parse_cpu_quota("1 0"), None);
    }
}
