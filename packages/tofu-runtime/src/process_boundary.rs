//! Linux exec-time descriptor and per-process resource boundary.
//! Installed only in post-fork children. No parent limits/signals are changed;
//! namespace/cgroup/filesystem/network authority remains external composition.
use serde_core::ser::SerializeMap;
use std::io;

#[derive(Clone, Copy, Debug)]
pub struct ResourceLimits {
    pub address_space_bytes: u64,
    pub cpu_seconds: u64,
    pub file_bytes: u64,
    pub file_descriptors: u64,
}
impl ResourceLimits {
    pub fn validate(self) -> io::Result<Self> {
        for value in [
            self.address_space_bytes,
            self.cpu_seconds,
            self.file_bytes,
            self.file_descriptors,
        ] {
            let native = value as libc::rlim_t;
            if value == 0 || native as u64 != value || native == libc::RLIM_INFINITY {
                return Err(io::Error::from(io::ErrorKind::InvalidInput));
            }
        }
        Ok(self)
    }
    pub(crate) fn parse(value: &serde_json::Value) -> io::Result<Option<Self>> {
        if value.is_null() {
            return Ok(None);
        }
        let object = value
            .as_object()
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        if object.len() != 4 {
            return Err(io::Error::from(io::ErrorKind::InvalidInput));
        }
        let number = |key| {
            object
                .get(key)
                .and_then(|v| v.as_u64())
                .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))
        };
        Ok(Some(
            Self {
                address_space_bytes: number("address_space_bytes")?,
                cpu_seconds: number("cpu_seconds")?,
                file_bytes: number("file_bytes")?,
                file_descriptors: number("file_descriptors")?,
            }
            .validate()?,
        ))
    }
}
impl serde_core::Serialize for ResourceLimits {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(4))?;
        map.serialize_entry("address_space_bytes", &self.address_space_bytes)?;
        map.serialize_entry("cpu_seconds", &self.cpu_seconds)?;
        map.serialize_entry("file_bytes", &self.file_bytes)?;
        map.serialize_entry("file_descriptors", &self.file_descriptors)?;
        map.end()
    }
}

/// Post-fork/pre-exec only: this code performs fixed-buffer computation and
/// async-signal-safe Linux syscalls, without allocation, locking or unwinding.
pub(crate) fn install(limits: Option<ResourceLimits>) -> io::Result<()> {
    seal_descriptors()?;
    // SAFETY: changes only the calling child; no privilege gain on later exec.
    if unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) } != 0 {
        return Err(io::Error::last_os_error());
    }
    set_limit(libc::RLIMIT_CORE as _, 0)?;
    if let Some(limits) = limits {
        set_limit(libc::RLIMIT_AS as _, limits.address_space_bytes)?;
        set_limit(libc::RLIMIT_CPU as _, limits.cpu_seconds)?;
        set_limit(libc::RLIMIT_FSIZE as _, limits.file_bytes)?;
        // Inventory happens before lowering NOFILE, including handles above it.
        set_limit(libc::RLIMIT_NOFILE as _, limits.file_descriptors)?;
    }
    Ok(())
}
fn set_limit(resource: libc::c_int, value: u64) -> io::Result<()> {
    let limit = libc::rlimit {
        rlim_cur: value as libc::rlim_t,
        rlim_max: value as libc::rlim_t,
    };
    // SAFETY: live initialized rlimit passed to the current child's syscall.
    if unsafe { libc::setrlimit(resource as _, &limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
fn seal_descriptors() -> io::Result<()> {
    // Mark rather than close: std::process owns an exec-error pipe that must
    // remain writable until exec succeeds. CLOEXEC closes it and all unrelated
    // descriptors atomically at exec while preserving spawn failure reporting.
    // SAFETY: Linux syscall constants and descriptor interval; no borrowed memory.
    if unsafe { libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, 4u32) } == 0 {
        return Ok(());
    }
    let error = io::Error::last_os_error();
    if !matches!(error.raw_os_error(), Some(libc::ENOSYS | libc::EINVAL)) {
        return Err(error);
    }
    // Linux 5.10 lacks CLOSE_RANGE_CLOEXEC. Inventory inside the post-fork child
    // avoids a parent-thread snapshot race and descriptor-number range caps.
    mark_from_proc()
}
fn mark_from_proc() -> io::Result<()> {
    // SAFETY: constant NUL-terminated procfs path and open flags.
    let directory = unsafe {
        libc::open(
            c"/proc/self/fd".as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if directory < 0 {
        return Err(io::Error::last_os_error());
    }
    let result = mark_directory(directory);
    // SAFETY: this function owns the opened directory exactly once.
    unsafe {
        libc::close(directory);
    }
    result
}
fn mark_directory(directory: libc::c_int) -> io::Result<()> {
    let mut buffer = [0u8; 8192];
    loop {
        // SAFETY: syscall receives a writable live fixed buffer. linux_dirent64
        // has d_reclen at offset 16 and NUL-terminated d_name at offset 19.
        let count = unsafe {
            libc::syscall(
                libc::SYS_getdents64,
                directory,
                buffer.as_mut_ptr(),
                buffer.len(),
            )
        };
        if count < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if count == 0 {
            return Ok(());
        }
        let count =
            usize::try_from(count).map_err(|_| io::Error::from(io::ErrorKind::InvalidData))?;
        if count > buffer.len() {
            return Err(io::Error::from(io::ErrorKind::InvalidData));
        }
        let mut offset = 0;
        while offset < count {
            let record = buffer
                .get(offset..count)
                .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
            if record.len() < 20 {
                return Err(io::Error::from(io::ErrorKind::InvalidData));
            }
            let length = u16::from_ne_bytes([record[16], record[17]]) as usize;
            if length < 20 || length > record.len() {
                return Err(io::Error::from(io::ErrorKind::InvalidData));
            }
            let names = &record[19..length];
            let end = names
                .iter()
                .position(|c| *c == 0)
                .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
            mark_name(&names[..end])?;
            offset += length;
        }
    }
}
fn mark_name(name: &[u8]) -> io::Result<()> {
    if name == b"." || name == b".." {
        return Ok(());
    }
    let mut fd: libc::c_int = 0;
    if name.is_empty() {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    for byte in name {
        if !byte.is_ascii_digit() {
            return Err(io::Error::from(io::ErrorKind::InvalidData));
        }
        fd = fd
            .checked_mul(10)
            .and_then(|v| v.checked_add((byte - b'0') as _))
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
    }
    if fd < 3 {
        return Ok(());
    }
    // SAFETY: child inventory is stable; only this single post-fork thread runs.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    // Most runtime handles are already CLOEXEC. Preserve that proof without a
    // second flags syscall; only actual inheritable handles need modification.
    if flags & libc::FD_CLOEXEC == 0
        && unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{os::unix::process::CommandExt, process::Command};
    #[test]
    fn proc_inventory_preserves_exec_error_reporting() {
        let mut command = Command::new("/definitely-missing-tofu-boundary-executable");
        // SAFETY: force the older-kernel implementation in the post-fork child.
        unsafe {
            command.pre_exec(mark_from_proc);
        }
        let error = command.spawn().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
    }
    #[test]
    fn default_boundary_preserves_exec_error_reporting() {
        let mut command = Command::new("/definitely-missing-tofu-boundary-executable");
        // SAFETY: installation is post-fork only, never on the test parent.
        unsafe {
            command.pre_exec(|| install(None));
        }
        let error = command.spawn().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
    }
}
