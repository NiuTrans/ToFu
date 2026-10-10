// Standalone cross-compile syntax/type check for the platform branches added
// to resource_probe.rs. Compiled with `--emit=metadata` against each target's
// std (no C dependencies), so it validates cfg gating + API signatures only.

#[cfg(target_os = "macos")]
fn host_memory_observation() -> Option<(u64, u64)> {
    const HOST_VM_INFO64: i32 = 4;
    const HOST_VM_INFO64_COUNT: u32 = 38;
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

#[cfg(target_os = "macos")]
fn sysctl_scalar(name: &str) -> Option<u64> {
    let output = std::process::Command::new("/usr/sbin/sysctl")
        .arg("-n")
        .arg(name)
        .output()
        .ok()?;
    if !output.status.success() || output.stdout.len() as u64 > 16 * 1024 {
        return None;
    }
    std::str::from_utf8(&output.stdout).ok()?.trim().parse().ok()
}

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
    let ok = unsafe { GlobalMemoryStatusEx(&mut status) };
    if ok == 0 {
        return None;
    }
    Some((status.total_phys, status.avail_phys.min(status.total_phys)))
}

#[cfg(target_os = "linux")]
fn host_memory_observation() -> Option<(u64, u64)> {
    // Linux branch is covered by the in-tree unit tests; stub keeps `main` valid.
    Some((0, 0))
}

#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
fn host_memory_observation() -> Option<(u64, u64)> {
    None
}

fn main() {
    let _ = host_memory_observation();
}
