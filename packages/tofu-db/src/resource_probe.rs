//! One-shot launch resource observation and bounded daemon budget derivation.
//!
//! The production probe reads CPU affinity/quota, host/cgroup memory and volume
//! headroom once. Missing core observations select a fixed lean profile; no
//! background sampler or unbounded system-file read is created.

use std::fs::File;
use std::io::{self, Read};
use std::path::{Component, Path, PathBuf};
use std::thread;

use crate::generated_storage_v2::{MAX_CONNECTION_REUSE_TARGET, DEFAULT_FRAME_MEMORY_BYTES, FRAME_MEMORY_HEADROOM_DIVISOR};
use crate::generated_tofudb_ir::SEARCH_PROJECTION_VOLUME_FREE_PERCENT;
pub use crate::generated_tofudb_ir::{MAX_SEARCH_PROJECTION_BYTES, MIN_SEARCH_PROJECTION_BYTES};
use crate::generated_tofudb_ir::{
    MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES, PAYLOAD_SEGMENT_CACHE_BYTES,
};
use crate::protocol::MAX_FRAME_BODY_BYTES;

const MIB: u64 = 1024 * 1024;
const MAX_SYSTEM_FILE_BYTES: u64 = 16 * 1024;
const MIN_FRAME_BUDGET_BYTES: usize = MAX_FRAME_BODY_BYTES;
const LEAN_CONNECTIONS: usize = 4;

// Hot-data residency budgets. The historical fixed defaults (32 MiB block
// payloads, 16 MiB decoded entity pages) were sized for a low-end host: on a
// large host the authority thrashed the tiny caches and every miss became a
// network-volume round trip (2026-09-22 cgroup memory-pressure incident:
// 30s+ cold reads against a 210 GiB cgroup). Both budgets now scale with the
// probed memory headroom — up as well as down — between the legacy defaults
// as floors and machine-derived ceilings, so the live working set stays resident in
// the engine's own (unreclaimable on swapless hosts) heap instead of relying
// on the kernel page cache the pressure reclaim keeps evicting. Explicit
// TOFU_DB_BLOCK_CACHE_MIB / TOFU_DB_PAGE_CACHE_MIB overrides win.
const MIN_BLOCK_CACHE_BYTES: usize = 32 * MIB as usize;
const MIN_PAGE_CACHE_BYTES: usize = 16 * MIB as usize;
// Each explicit override may use at most a quarter of observed headroom;
// defaults together use 3/32, leaving room for application work and other caches.
const HOT_CACHE_OVERRIDE_HEADROOM_DIVISOR: u64 = 4;

// Write-path and segment-index budgets. Same derivation contract as the
// hot-data caches above: the floor is the measured personal default, the
// probed share scales with headroom up to a bounded ceiling, and the
// explicit byte-unit env override wins with its historical clamp.
// * overlay budget: memory-resident pending deltas before a background
//   drain; a memory-headroom commodity, floored at the 2026-09-19 measured
//   8 MiB, ceilinged at the consumer's 256 MiB structural clamp.
// * overlay WAL watermark: structural, not probed — 4/5 of the soft
//   rotation threshold (= the historical 48 MiB) so the backstop always
//   tracks the WAL geometry instead of drifting as a second magic number.
// * checkpoint materialize batch: one fenced step costs ~300 ms per 8 MiB
//   on a network mount (2026-09-22 FUSE storm), so network volumes stay at
//   the 8 MiB floor; other volumes scale to 32 MiB (the packer's own
//   steady-state segment target) with headroom.
// * payload segment index cache: parsed verified indexes, floored at the
//   legacy 64 MiB, scaling with headroom inside the consumer's 16 GiB clamp.
const MIN_OVERLAY_BUDGET_PROBED_BYTES: usize = 8 * MIB as usize;
const MAX_MATERIALIZE_BATCH_PROBED_BYTES: u64 = 32 * MIB;
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
    pub block_cache_bytes: usize,
    pub entity_page_cache_bytes: usize,
    pub overlay_budget_bytes: usize,
    pub overlay_wal_watermark_bytes: u64,
    pub checkpoint_materialize_batch_bytes: u64,
    pub payload_segment_cache_bytes: usize,
    pub snapshot: LaunchResourceSnapshot,
    pub used_lean_fallback: bool,
}

impl DaemonResourceBudget {
    /// Observation-only embedded input memory hint, distinct from owned byte
    /// accounting. Scale with observed headroom; a missing probe retains the
    /// lean raw-frame hint. This reserves no RAM and rejects no requests.
    pub fn embedded_request_memory_bytes(&self) -> usize {
        if self.used_lean_fallback {
            return self.maximum_frame_bytes;
        }
        usize::try_from(self.snapshot.memory_headroom_bytes.unwrap_or(0)
            / crate::generated_storage_v2::EMBEDDED_REQUEST_MEMORY_HEADROOM_DIVISOR)
            .unwrap_or(usize::MAX)
            .max(self.maximum_frame_bytes)
    }

    pub fn from_snapshot(snapshot: LaunchResourceSnapshot) -> Self {
        Self::from_snapshot_with_volume_class(snapshot, None)
    }

    /// Full derivation with the authority volume's classification. An
    /// unobserved class (`None`/`Unknown`) derives exactly like `Local`;
    /// only an observed `Network` mount holds the checkpoint materialize
    /// batch at its floor.
    pub fn from_snapshot_with_volume_class(
        snapshot: LaunchResourceSnapshot,
        volume_class: Option<VolumeClass>,
    ) -> Self {
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
        // Legacy readiness maximumConnections now sizes initial transport
        // reservations and idle reuse. It never rejects active socket callers.
        // Embedded queues still consume this hint pending their own audit.
        let maximum_connections = logical_cpus
            .saturating_mul(16)
            .min(memory_connections)
            .clamp(1, MAX_CONNECTION_REUSE_TARGET);
        let maximum_frame_bytes = usize::try_from(memory_headroom_bytes / FRAME_MEMORY_HEADROOM_DIVISOR as u64)
            .unwrap_or(usize::MAX)
            .max(MIN_FRAME_BUDGET_BYTES);
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
            block_cache_bytes: cache_capacity_bytes(
                memory_headroom_bytes / 16,
                MIN_BLOCK_CACHE_BYTES,
                memory_headroom_bytes,
                "TOFU_DB_BLOCK_CACHE_MIB",
            ),
            entity_page_cache_bytes: cache_capacity_bytes(
                memory_headroom_bytes / 32,
                MIN_PAGE_CACHE_BYTES,
                memory_headroom_bytes,
                "TOFU_DB_PAGE_CACHE_MIB",
            ),
            overlay_budget_bytes: overlay_budget_bytes(memory_headroom_bytes),
            overlay_wal_watermark_bytes: overlay_wal_watermark_bytes(),
            checkpoint_materialize_batch_bytes: checkpoint_materialize_batch_bytes(
                memory_headroom_bytes,
                volume_class,
            ),
            payload_segment_cache_bytes: payload_segment_cache_bytes(memory_headroom_bytes),
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
            block_cache_bytes: MIN_BLOCK_CACHE_BYTES,
            entity_page_cache_bytes: MIN_PAGE_CACHE_BYTES,
            overlay_budget_bytes: MIN_OVERLAY_BUDGET_PROBED_BYTES,
            overlay_wal_watermark_bytes: overlay_wal_watermark_bytes(),
            checkpoint_materialize_batch_bytes: MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES,
            payload_segment_cache_bytes: PAYLOAD_SEGMENT_CACHE_BYTES,
            snapshot,
            used_lean_fallback: true,
        }
    }
}

/// Resolve one hot-data cache budget: the probed share clamped between the
/// legacy floor and a headroom-derived ceiling, with an explicit MiB env override
/// winning over the probe (matching the timer-cap override convention).
fn cache_capacity_bytes(
    probed_share: u64,
    floor: usize,
    memory_headroom_bytes: u64,
    env_name: &str,
) -> usize {
    let ceiling = usize::try_from(memory_headroom_bytes / HOT_CACHE_OVERRIDE_HEADROOM_DIVISOR)
        .unwrap_or(usize::MAX)
        .max(floor);
    let probed = usize::try_from(probed_share)
        .unwrap_or(usize::MAX)
        .clamp(floor, ceiling);
    std::env::var(env_name)
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .map(|mib| mib.saturating_mul(MIB as usize).min(ceiling))
        .unwrap_or(probed)
}


/// Overlay merge byte budget: headroom/128 between the measured 8 MiB floor
/// and the consumer's 256 MiB structural clamp; the explicit byte-unit
/// override keeps the consumer's historical 1..256 MiB clamp.
fn overlay_budget_bytes(memory_headroom_bytes: u64) -> usize {
    let probed = usize::try_from(memory_headroom_bytes / 128)
        .unwrap_or(usize::MAX)
        .clamp(
            MIN_OVERLAY_BUDGET_PROBED_BYTES,
            crate::entity::OVERLAY_BUDGET_BYTES_MAX,
        );
    std::env::var("TOFU_DB_OVERLAY_BUDGET_BYTES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .map(|value| {
            value.clamp(
                crate::entity::OVERLAY_BUDGET_BYTES_MIN,
                crate::entity::OVERLAY_BUDGET_BYTES_MAX,
            )
        })
        .unwrap_or(probed)
}

/// Overlay WAL watermark: 4/5 of the soft rotation threshold (exactly the
/// historical 48 MiB default), so the backstop derives from the WAL geometry
/// by construction. The explicit override keeps the consumer's clamp, which
/// preserves ≥4 MiB of headroom under the 64 MiB active-WAL hard ceiling.
fn overlay_wal_watermark_bytes() -> u64 {
    let probed = crate::ACTIVE_LOG_ROTATE_BYTES * 4 / 5;
    std::env::var("TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(|value| {
            value.clamp(
                crate::entity::OVERLAY_WAL_WATERMARK_BYTES_MIN,
                crate::entity::OVERLAY_WAL_WATERMARK_BYTES_MAX,
            )
        })
        .unwrap_or(probed)
}

/// Per-step checkpoint materialization budget: network volumes stay at the
/// 8 MiB generated floor (one fenced step costs ~300 ms per 8 MiB there);
/// other volumes scale with headroom/256 up to 32 MiB, the packer's own
/// steady-state segment target. The explicit override keeps the consumer's
/// half-segment ceiling.
fn checkpoint_materialize_batch_bytes(
    memory_headroom_bytes: u64,
    volume_class: Option<VolumeClass>,
) -> u64 {
    let probed = match volume_class {
        Some(VolumeClass::Network) => MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES,
        _ => (memory_headroom_bytes / 256).clamp(
            MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES,
            MAX_MATERIALIZE_BATCH_PROBED_BYTES,
        ),
    };
    const CEILING: u64 = crate::payload_segment::MAX_SEGMENT_PAYLOAD_BYTES / 2;
    std::env::var("TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES")
        .ok()
        .and_then(|raw| raw.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .map(|value| value.min(CEILING))
        .unwrap_or(probed)
}

/// Verified open-segment index cache: headroom/64 between the legacy 64 MiB
/// floor and the consumer's 16 GiB clamp; the explicit override keeps the
/// consumer's exact semantics (including 0 = disabled).
fn payload_segment_cache_bytes(memory_headroom_bytes: u64) -> usize {
    let probed = usize::try_from(memory_headroom_bytes / 64)
        .unwrap_or(usize::MAX)
        .clamp(
            PAYLOAD_SEGMENT_CACHE_BYTES,
            crate::payload_segment::MAX_SEGMENT_CACHE_BUDGET_BYTES,
        );
    crate::payload_segment::segment_cache_budget_override(
        std::env::var("TOFU_DB_PAYLOAD_SEGMENT_CACHE_BYTES")
            .ok()
            .as_deref(),
    )
    .unwrap_or(probed)
}

fn timer_live_capacity(_logical_cpus: usize) -> usize {
    // Retired compatibility metric; no watcher count admission quota.
    0
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

/// Coarse durability-relevant classification of one volume. Network/fuse
/// mounts pay per-fsync latency orders of magnitude above local block
/// devices (the 2026-09-18 FUSE-storm postmortem), so the automatic WAL
/// durability policy tiers on this observation; explicit `--durability`
/// always wins over it.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum VolumeClass {
    Local,
    Network,
    #[default]
    Unknown,
}

/// Classifies the volume hosting `path`. Any observation gap — a missing
/// mount table, an unparseable entry, a non-Linux platform — reports
/// `Unknown`, and the durability policy treats `Unknown` exactly like
/// `Local` (the always-safe tier).
pub fn volume_class(path: &Path) -> VolumeClass {
    platform_volume_class(path)
}

const MAX_MOUNTS_BYTES: u64 = 64 * 1024;

#[cfg(target_os = "linux")]
fn platform_volume_class(path: &Path) -> VolumeClass {
    let canonical = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut bytes = Vec::new();
    let Ok(mut file) = File::open("/proc/mounts") else {
        return VolumeClass::Unknown;
    };
    if file
        .take(MAX_MOUNTS_BYTES + 1)
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() as u64 > MAX_MOUNTS_BYTES
    {
        return VolumeClass::Unknown;
    }
    let Ok(document) = String::from_utf8(bytes) else {
        return VolumeClass::Unknown;
    };
    classify_mounts(&document, &canonical)
}

/// Longest-mount-prefix classification over one `/proc/mounts` document;
/// pure so every edge case is a deterministic unit fixture.
#[cfg(target_os = "linux")]
fn classify_mounts(document: &str, canonical: &Path) -> VolumeClass {
    let mut best: Option<(usize, &str)> = None;
    for line in document.lines() {
        let mut fields = line.split_ascii_whitespace();
        let (Some(_source), Some(target), Some(fstype)) =
            (fields.next(), fields.next(), fields.next())
        else {
            continue;
        };
        let mount = Path::new(target);
        if canonical.starts_with(mount) && best.is_none_or(|(length, _)| target.len() > length) {
            best = Some((target.len(), fstype));
        }
    }
    match best {
        Some((_, fstype)) if is_network_fstype(fstype) => VolumeClass::Network,
        Some(_) => VolumeClass::Local,
        None => VolumeClass::Unknown,
    }
}

/// The automatic launch-time WAL durability tier for one authority path
/// when no explicit `--durability` was given.
pub fn automatic_wal_durability(path: &Path) -> crate::wal::WalDurability {
    match volume_class(path) {
        VolumeClass::Network => crate::wal::WalDurability::Normal,
        _ => crate::wal::WalDurability::Strict,
    }
}

#[cfg(target_os = "linux")]
fn is_network_fstype(fstype: &str) -> bool {
    fstype.starts_with("fuse")
        || matches!(
            fstype,
            "nfs" | "nfs4" | "cifs" | "smbfs" | "smb3" | "ceph" | "glusterfs" | "9p" | "sshfs"
        )
}

#[cfg(not(target_os = "linux"))]
fn platform_volume_class(_path: &Path) -> VolumeClass {
    VolumeClass::Unknown
}

fn minimum_present<T: Ord + Copy>(first: Option<T>, second: Option<T>) -> Option<T> {
    match (first, second) {
        (Some(first), Some(second)) => Some(first.min(second)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

/// Returns `(capacity_bytes, headroom_bytes)` for host memory, or `None`
/// when the platform offers no bounded observation. Linux reads
/// `/proc/meminfo`; macOS uses `sysctl`; Windows uses
/// `GlobalMemoryStatusEx`. A missing observation selects the lean profile.
#[cfg(target_os = "linux")]
fn host_memory_observation() -> Option<(u64, u64)> {
    let document = read_bounded(Path::new("/proc/meminfo")).ok()?;
    let total = meminfo_bytes(&document, "MemTotal:")?;
    let available = meminfo_bytes(&document, "MemAvailable:")?;
    Some((total, available.min(total)))
}

/// macOS host memory: capacity from `sysctl hw.memsize`, headroom from the
/// Mach `host_statistics64` free+inactive page counts. There is no reliable
/// sysctl node for available memory, so the Mach host port is queried
/// directly; the page size comes from `vm_page_size`.
#[cfg(target_os = "macos")]
fn host_memory_observation() -> Option<(u64, u64)> {
    const HOST_VM_INFO64: i32 = 4; // HOST_VM_INFO flavor selecting vm_statistics64
    const HOST_VM_INFO64_COUNT: u32 = 38; // sizeof(vm_statistics64_data_t)/sizeof(integer_t)
    #[repr(C)]
    struct VmStatistics64 {
        free_count: u32,
        active_count: u32,
        inactive_count: u32,
        wire_count: u32,
        zero_fill_count: u64,
        reactivations: u64,
        pageins: u64,
        pageouts: u64,
        faults: u64,
        cow_faults: u64,
        lookups: u64,
        hits: u64,
        purges: u64,
        purgeable_count: u32,
        speculative_count: u32,
        decompressions: u64,
        compressions: u64,
        swapins: u64,
        swapouts: u64,
        compressor_page_count: u32,
        throttled_count: u32,
        external_page_count: u32,
        internal_page_count: u32,
        total_uncompressed_pages_in_compressor: u64,
    }
    type MachPort = u32;
    type KernReturn = i32;
    extern "C" {
        fn mach_host_self() -> MachPort;
        fn host_statistics64(
            host: MachPort,
            flavor: i32,
            info: *mut VmStatistics64,
            count: *mut u32,
        ) -> KernReturn;
        fn vm_page_size() -> u32;
    }
    let capacity = sysctl_scalar("hw.memsize")?;
    let mut stats = std::mem::MaybeUninit::<VmStatistics64>::uninit();
    let mut count = HOST_VM_INFO64_COUNT;
    // SAFETY: `stats` is a valid out-buffer of the declared count.
    let result = unsafe {
        host_statistics64(mach_host_self(), HOST_VM_INFO64, stats.as_mut_ptr(), &mut count)
    };
    if result != 0 {
        return Some((capacity, 0));
    }
    let stats = unsafe { stats.assume_init() };
    let page_size = unsafe { vm_page_size() } as u64;
    let headroom = (stats.free_count as u64)
        .saturating_add(stats.inactive_count as u64)
        .saturating_mul(page_size)
        .min(capacity);
    Some((capacity, headroom))
}

/// Runs `sysctl -n <name>` and parses a single bounded unsigned scalar.
#[cfg(target_os = "macos")]
fn sysctl_scalar(name: &str) -> Option<u64> {
    let output = std::process::Command::new("/usr/sbin/sysctl")
        .arg("-n")
        .arg(name)
        .output()
        .ok()?;
    if !output.status.success() || output.stdout.len() as u64 > MAX_SYSTEM_FILE_BYTES {
        return None;
    }
    std::str::from_utf8(&output.stdout).ok()?.trim().parse().ok()
}

/// Windows host memory via `GlobalMemoryStatusEx`. The struct is
/// stack-allocated and bounded; the call is the canonical Win32 surface.
#[cfg(target_os = "windows")]
fn host_memory_observation() -> Option<(u64, u64)> {
    #[repr(C)]
    struct MemoryStatusEx {
        length: u32,
        memory_load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page_file: u64,
        avail_page_file: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_extended_virtual: u64,
    }
    extern "system" {
        fn GlobalMemoryStatusEx(buffer: *mut MemoryStatusEx) -> i32;
    }
    let mut status = MemoryStatusEx {
        length: std::mem::size_of::<MemoryStatusEx>() as u32,
        memory_load: 0,
        total_phys: 0,
        avail_phys: 0,
        total_page_file: 0,
        avail_page_file: 0,
        total_virtual: 0,
        avail_virtual: 0,
        avail_extended_virtual: 0,
    };
    // SAFETY: `status` is a valid out-buffer of the declared size.
    let ok = unsafe { GlobalMemoryStatusEx(&mut status) };
    if ok == 0 {
        return None;
    }
    Some((status.total_phys, status.avail_phys.min(status.total_phys)))
}

/// Other platforms expose no bounded host-memory observation; the launch
/// budget falls back lean.
#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
fn host_memory_observation() -> Option<(u64, u64)> {
    None
}

#[cfg(target_os = "linux")]
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

/// Control-group CPU quotas are a Linux container surface. Other platforms
/// have no equivalent launch-time quota, so they report no observation and
/// rely on the host probe plus the lean fallback.
#[cfg(target_os = "linux")]
fn cgroup_cpu_limit() -> Option<usize> {
    cgroup_v2_cpu_limit().or_else(cgroup_v1_cpu_limit)
}

#[cfg(not(target_os = "linux"))]
fn cgroup_cpu_limit() -> Option<usize> {
    None
}

#[cfg(target_os = "linux")]
fn cgroup_v2_cpu_limit() -> Option<usize> {
    let document = read_bounded(&unified_cgroup_directory()?.join("cpu.max")).ok()?;
    parse_cpu_quota(&document)
}

#[cfg(target_os = "linux")]
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

#[cfg(target_os = "linux")]
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

/// Control-group memory ceilings are Linux-only; see `cgroup_cpu_limit`.
#[cfg(target_os = "linux")]
fn cgroup_memory_observation() -> Option<(u64, u64)> {
    cgroup_v2_memory_observation().or_else(cgroup_v1_memory_observation)
}

#[cfg(not(target_os = "linux"))]
fn cgroup_memory_observation() -> Option<(u64, u64)> {
    None
}

#[cfg(target_os = "linux")]
fn cgroup_v2_memory_observation() -> Option<(u64, u64)> {
    let directory = unified_cgroup_directory()?;
    let maximum = read_scalar(&directory.join("memory.max"))?;
    let current = read_scalar(&directory.join("memory.current"))?;
    Some((maximum, current.min(maximum)))
}

#[cfg(target_os = "linux")]
fn cgroup_v1_memory_observation() -> Option<(u64, u64)> {
    let directory = legacy_cgroup_directory("memory")?;
    let maximum = read_scalar(&directory.join("memory.limit_in_bytes"))?;
    let current = read_scalar(&directory.join("memory.usage_in_bytes"))?;
    Some((maximum, current.min(maximum)))
}

#[cfg(target_os = "linux")]
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

#[cfg(target_os = "linux")]
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

#[cfg(target_os = "linux")]
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

#[cfg(target_os = "linux")]
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

    #[cfg(target_os = "linux")]
    #[test]
    fn mounts_classification_picks_longest_prefix_and_network_fstypes() {
        let document = "\
/dev/sda1 / ext4 rw,relatime 0 0
/dev/sdb1 /data xfs rw,relatime 0 0
nas/share /data/nested nfs4 rw,relatime 0 0
";
        assert_eq!(
            classify_mounts(document, Path::new("/data/app")),
            VolumeClass::Local
        );
        assert_eq!(
            classify_mounts(document, Path::new("/data/nested/authority")),
            VolumeClass::Network
        );
        assert_eq!(
            classify_mounts(document, Path::new("/elsewhere")),
            VolumeClass::Local,
            "only the root mount matches"
        );
        assert_eq!(
            classify_mounts("", Path::new("/data")),
            VolumeClass::Unknown
        );
        for line in [
            "fuse.sshfs",
            "fuse",
            "nfs",
            "cifs",
            "smb3",
            "ceph",
            "glusterfs",
            "9p",
        ] {
            let document = format!("remote /mnt {line} rw 0 0\n");
            assert_eq!(
                classify_mounts(&document, Path::new("/mnt/db")),
                VolumeClass::Network,
                "{line} must tier as a network volume"
            );
        }
        // A malformed line never aborts the classification.
        let document = "garbage\n/dev/sda1 / ext4 rw 0 0\n";
        assert_eq!(
            classify_mounts(document, Path::new("/x")),
            VolumeClass::Local
        );
    }

    #[test]
    fn automatic_durability_tiers_network_to_normal_and_everything_else_strict() {
        assert_eq!(
            crate::wal::WalDurability::Normal,
            match VolumeClass::Network {
                VolumeClass::Network => crate::wal::WalDurability::Normal,
                _ => crate::wal::WalDurability::Strict,
            }
        );
        // Unknown and Local stay on the always-safe strict barrier.
        assert!(matches!(
            automatic_wal_durability(Path::new("/definitely-not-a-real-path-9f3a")),
            crate::wal::WalDurability::Strict
        ));
    }

    #[test]
    fn frame_memory_threshold_and_embedded_parser_budget_scale_with_headroom() {
        let budget = |headroom| DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(64), memory_capacity_bytes: Some(256 * 1024 * MIB),
            memory_headroom_bytes: Some(headroom), volume_free_bytes: Some(1024 * MIB),
        });
        let small = budget(1024 * MIB);
        let large = budget(128 * 1024 * MIB);
        assert_eq!(small.embedded_request_memory_bytes(), 32 * MIB as usize);
        assert_eq!(large.embedded_request_memory_bytes(), 4096 * MIB as usize);
        assert_eq!(large.maximum_frame_bytes, 4096 * MIB as usize);
        let lean = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot::default());
        assert_eq!(lean.embedded_request_memory_bytes(), lean.maximum_frame_bytes);
        assert_eq!(budget(1).embedded_request_memory_bytes(), MAX_FRAME_BODY_BYTES);
        assert!(budget(u64::MAX).embedded_request_memory_bytes() >= DEFAULT_FRAME_MEMORY_BYTES);
    }

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
        assert_eq!(huge.maximum_connections, MAX_CONNECTION_REUSE_TARGET);
        assert_eq!(huge.maximum_frame_bytes, usize::try_from(u64::MAX / FRAME_MEMORY_HEADROOM_DIVISOR as u64).unwrap_or(usize::MAX));
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
    fn hot_data_cache_budgets_scale_with_headroom_and_floor_at_lean() {
        // Probe failure keeps the legacy laptop-sized defaults.
        let lean = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot::default());
        assert!(lean.used_lean_fallback);
        assert_eq!(lean.block_cache_bytes, 32 * MIB as usize);
        assert_eq!(lean.entity_page_cache_bytes, 16 * MIB as usize);

        // A pressured host still floors at the legacy defaults, never below.
        let pressured = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(8),
            memory_capacity_bytes: Some(1024 * MIB),
            memory_headroom_bytes: Some(1),
            volume_free_bytes: Some(MIN_WRITABLE_VOLUME_FREE_BYTES),
        });
        assert_eq!(pressured.block_cache_bytes, 32 * MIB as usize);
        assert_eq!(pressured.entity_page_cache_bytes, 16 * MIB as usize);

        // 8 GiB headroom: /16 = 512 MiB payloads, /32 = 256 MiB pages.
        let moderate = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(16),
            memory_capacity_bytes: Some(64 * 1024 * MIB),
            memory_headroom_bytes: Some(8 * 1024 * MIB),
            volume_free_bytes: Some(1024 * 1024 * MIB),
        });
        assert_eq!(moderate.block_cache_bytes, 512 * MIB as usize);
        assert_eq!(moderate.entity_page_cache_bytes, 256 * MIB as usize);

        // Larger hosts keep scaling instead of plateauing at 2/1 GiB.
        let roomy = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            logical_cpus: Some(64),
            memory_capacity_bytes: Some(256 * 1024 * MIB),
            memory_headroom_bytes: Some(64 * 1024 * MIB),
            volume_free_bytes: Some(1024 * 1024 * MIB),
        });
        assert_eq!(roomy.block_cache_bytes, 4096 * MIB as usize);
        assert_eq!(roomy.entity_page_cache_bytes, 2048 * MIB as usize);
        let larger = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot {
            memory_headroom_bytes: Some(128 * 1024 * MIB),
            ..roomy.snapshot.clone()
        });
        assert_eq!(larger.block_cache_bytes, roomy.block_cache_bytes * 2);
        assert_eq!(larger.entity_page_cache_bytes, roomy.entity_page_cache_bytes * 2);
    }

    #[test]
    fn write_path_budgets_derive_from_probe_volume_class_and_floors() {
        fn snapshot(headroom: u64) -> LaunchResourceSnapshot {
            LaunchResourceSnapshot {
                logical_cpus: Some(16),
                memory_capacity_bytes: Some(64 * 1024 * MIB),
                memory_headroom_bytes: Some(headroom),
                volume_free_bytes: Some(1024 * 1024 * MIB),
            }
        }

        // Probe failure keeps the measured personal defaults.
        let lean = DaemonResourceBudget::from_snapshot(LaunchResourceSnapshot::default());
        assert!(lean.used_lean_fallback);
        assert_eq!(lean.overlay_budget_bytes, 8 * MIB as usize);
        assert_eq!(lean.overlay_wal_watermark_bytes, 48 * MIB);
        assert_eq!(lean.checkpoint_materialize_batch_bytes, 8 * MIB);
        assert_eq!(lean.payload_segment_cache_bytes, 64 * MIB as usize);

        // A pressured host still floors, never below the defaults.
        let pressured = DaemonResourceBudget::from_snapshot(snapshot(1));
        assert_eq!(pressured.overlay_budget_bytes, 8 * MIB as usize);
        assert_eq!(pressured.checkpoint_materialize_batch_bytes, 8 * MIB);
        assert_eq!(pressured.payload_segment_cache_bytes, 64 * MIB as usize);

        // 8 GiB headroom on a local volume: /128 = 64 MiB overlay, /256 =
        // 32 MiB materialize batch (the packer's steady-state target),
        // /64 = 128 MiB segment index cache.
        let local = DaemonResourceBudget::from_snapshot_with_volume_class(
            snapshot(8 * 1024 * MIB),
            Some(VolumeClass::Local),
        );
        assert_eq!(local.overlay_budget_bytes, 64 * MIB as usize);
        assert_eq!(local.checkpoint_materialize_batch_bytes, 32 * MIB);
        assert_eq!(local.payload_segment_cache_bytes, 128 * MIB as usize);
        assert_eq!(local.overlay_wal_watermark_bytes, 48 * MIB);

        // Same headroom on a network volume keeps the 8 MiB materialize
        // floor: one fenced step already costs ~300 ms there.
        let network = DaemonResourceBudget::from_snapshot_with_volume_class(
            snapshot(8 * 1024 * MIB),
            Some(VolumeClass::Network),
        );
        assert_eq!(network.checkpoint_materialize_batch_bytes, 8 * MIB);
        assert_eq!(network.overlay_budget_bytes, 64 * MIB as usize);

        // An unobserved volume class derives like a local one.
        let unobserved = DaemonResourceBudget::from_snapshot(snapshot(8 * 1024 * MIB));
        assert_eq!(unobserved.checkpoint_materialize_batch_bytes, 32 * MIB);

        // 64 GiB headroom hits the bounded ceilings, never above.
        let roomy = DaemonResourceBudget::from_snapshot_with_volume_class(
            snapshot(64 * 1024 * MIB),
            Some(VolumeClass::Local),
        );
        assert_eq!(
            roomy.overlay_budget_bytes,
            crate::entity::OVERLAY_BUDGET_BYTES_MAX
        );
        assert_eq!(roomy.checkpoint_materialize_batch_bytes, 32 * MIB);
        assert_eq!(roomy.payload_segment_cache_bytes, 1024 * MIB as usize);

        // The watermark derives from the WAL rotation geometry by
        // construction, clamped to keep ≥4 MiB under the hard ceiling.
        assert_eq!(
            48 * MIB,
            crate::ACTIVE_LOG_ROTATE_BYTES * 4 / 5,
            "watermark derivation must equal the historical 48 MiB default"
        );
        assert!(
            crate::entity::OVERLAY_WAL_WATERMARK_BYTES_MAX
                <= crate::ACTIVE_LOG_MAX_BYTES - 4 * MIB
        );
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
