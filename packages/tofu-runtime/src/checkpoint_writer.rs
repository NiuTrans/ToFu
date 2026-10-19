//! Shared Linux process-death evidence for abandoned journal recovery.
//! Captured boot/PID-namespace/start identity belongs to the storage owner.
//! Missing/inaccessible evidence refuses; elapsed time never grants takeover.
use serde_json::{json, Value};
use std::io;
use crate::checkpoint_journal::Error;

#[cfg(target_os = "linux")]
fn kernel_scope() -> io::Result<(String, u64)> {
    use std::os::unix::fs::MetadataExt;
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let boot = boot.trim().to_owned();
    // NSpid is expressed from the mounted procfs view through the caller's
    // namespace. A parent-namespace procfs view cannot prove PID identity here.
    let status = std::fs::read_to_string("/proc/self/status")?;
    let pids = status
        .lines()
        .find_map(|line| line.strip_prefix("NSpid:"))
        .ok_or_else(|| io::Error::other("PID namespace view unavailable"))?;
    let pids: Vec<_> = pids.split_whitespace().collect();
    if pids.len() != 1 || pids[0].parse::<u32>().ok() != Some(std::process::id()) {
        return Err(io::Error::other(
            "procfs PID view differs from the caller namespace",
        ));
    }
    let namespace = std::fs::metadata("/proc/self/ns/pid")?.ino();
    if boot.is_empty() || namespace == 0 {
        return Err(io::Error::other(
            "checkpoint writer kernel scope unavailable",
        ));
    }
    Ok((boot, namespace))
}

fn process_stat(raw: &str) -> io::Result<(&str, u64)> {
    // comm may contain spaces or parentheses; the final ')' closes field 2.
    let (_, fields) = raw
        .rsplit_once(')')
        .ok_or_else(|| io::Error::other("malformed process stat"))?;
    let mut fields = fields.split_whitespace();
    let state = fields
        .next()
        .ok_or_else(|| io::Error::other("missing process state"))?;
    let start = fields
        .nth(18)
        .ok_or_else(|| io::Error::other("missing process start"))?
        .parse::<u64>()
        .map_err(io::Error::other)?;
    if start == 0 {
        return Err(io::Error::other("invalid process start"));
    }
    Ok((state, start))
}

pub fn capture() -> Option<Value> {
    #[cfg(target_os = "linux")]
    {
        let (boot, namespace) = kernel_scope().ok()?;
        let raw = std::fs::read_to_string("/proc/self/stat").ok()?;
        let pid = raw.split_whitespace().next()?.parse::<u32>().ok()?;
        if pid == 0 {
            return None;
        }
        let (_, start) = process_stat(&raw).ok()?;
        Some(json!({"boot_id":boot,"pid_namespace":namespace,
                    "pid":pid,"start_ticks":start}))
    }
    #[cfg(not(target_os = "linux"))]
    {
        None
    }
}

pub fn quiescent(identity: &Value) -> Result<bool, Error> {
    #[cfg(target_os = "linux")]
    {
        let (boot, namespace) = kernel_scope().map_err(|_| Error::Storage)?;
        if identity["boot_id"].as_str() != Some(boot.as_str())
            || identity["pid_namespace"].as_u64() != Some(namespace)
        {
            return Ok(false);
        }
        let pid = identity["pid"]
            .as_u64()
            .filter(|p| *p > 0 && *p <= i32::MAX as u64)
            .ok_or(Error::Invalid)?;
        let start = identity["start_ticks"]
            .as_u64()
            .filter(|s| *s > 0)
            .ok_or(Error::Invalid)?;
        // pidfd bypasses procfs hidepid: ENOENT from a hidden /proc directory
        // alone is never death evidence. Poll waits for the entire thread group
        // to exit, rather than trusting a zombie group leader's stat state.
        use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
        let raw_fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) };
        if raw_fd < 0 {
            return if io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
                Ok(true)
            } else {
                Err(Error::Storage)
            };
        }
        let process = unsafe { OwnedFd::from_raw_fd(raw_fd as libc::c_int) };
        let mut event = libc::pollfd {
            fd: process.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let polled = unsafe { libc::poll(&mut event, 1, 0) };
        if polled < 0 {
            return Err(Error::Storage);
        }
        if event.revents & libc::POLLIN != 0 {
            return Ok(true);
        }
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(raw) => {
                let (_, current_start) = process_stat(&raw).map_err(|_| Error::Storage)?;
                // A recycled PID proves that the captured process is gone.
                Ok(current_start != start)
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(_) => Err(Error::Storage),
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = identity;
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stat_handles_parentheses_and_never_accepts_missing_start() {
        let raw = format!("23 (worker (name)) S {} 456 0", vec!["0"; 18].join(" "));
        assert_eq!(process_stat(&raw).unwrap(), ("S", 456));
        assert!(process_stat("23 (worker) S 0").is_err());
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn live_writer_foreign_kernel_and_pid_namespace_do_not_grant_recovery() {
        let identity = capture().unwrap();
        assert_eq!(quiescent(&identity), Ok(false));
        let mut foreign = identity.clone();
        foreign["boot_id"] = json!("other-boot");
        assert_eq!(quiescent(&foreign), Ok(false));
        foreign = identity;
        foreign["pid_namespace"] = json!(0);
        assert_eq!(quiescent(&foreign), Ok(false));
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn writer_crash_child() {
        let Ok(path) = std::env::var("TOFU_CHECKPOINT_WRITER_TEST_READY") else {
            return;
        };
        let path = std::path::PathBuf::from(path);
        let temporary = path.with_extension("tmp");
        std::fs::write(&temporary, serde_json::to_vec(&capture().unwrap()).unwrap()).unwrap();
        std::fs::rename(temporary, path).unwrap();
        std::thread::sleep(std::time::Duration::from_secs(30));
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn pidfd_requires_real_exit_and_distinguishes_a_recycled_pid_identity() {
        struct Child(std::process::Child);
        impl Drop for Child {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
        let directory = tempfile::tempdir().unwrap();
        let ready = directory.path().join("ready");
        let mut child = Child(
            std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "checkpoint_writer::tests::writer_crash_child",
                    "--nocapture",
                ])
                .env("TOFU_CHECKPOINT_WRITER_TEST_READY", &ready)
                .stdout(std::process::Stdio::null())
                .spawn()
                .unwrap(),
        );
        let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !ready.exists() {
            assert!(child.0.try_wait().unwrap().is_none());
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        let identity: Value = serde_json::from_slice(&std::fs::read(&ready).unwrap()).unwrap();
        assert_eq!(quiescent(&identity), Ok(false));
        let mut recycled = identity.clone();
        recycled["start_ticks"] = json!(identity["start_ticks"].as_u64().unwrap() + 1);
        assert_eq!(quiescent(&recycled), Ok(true));
        child.0.kill().unwrap();
        // An exited but unreaped process is observed through its pidfd too.
        let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !quiescent(&identity).unwrap() {
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        child.0.wait().unwrap();
        assert_eq!(quiescent(&identity), Ok(true));
    }
}
