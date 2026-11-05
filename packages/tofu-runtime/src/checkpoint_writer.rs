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

/// Returns (state, num_threads, start_ticks) from one /proc/<pid>/stat line.
fn process_stat(raw: &str) -> io::Result<(&str, u64, u64)> {
    // comm may contain spaces or parentheses; the final ')' closes field 2.
    let (_, fields) = raw
        .rsplit_once(')')
        .ok_or_else(|| io::Error::other("malformed process stat"))?;
    let mut fields = fields.split_whitespace();
    let state = fields
        .next()
        .ok_or_else(|| io::Error::other("missing process state"))?;
    let threads = fields
        .nth(16)
        .ok_or_else(|| io::Error::other("missing process thread count"))?
        .parse::<u64>()
        .map_err(io::Error::other)?;
    let start = fields
        .nth(1)
        .ok_or_else(|| io::Error::other("missing process start"))?
        .parse::<u64>()
        .map_err(io::Error::other)?;
    if start == 0 {
        return Err(io::Error::other("invalid process start"));
    }
    Ok((state, threads, start))
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
        let (_, _, start) = process_stat(&raw).ok()?;
        Some(json!({"boot_id":boot,"pid_namespace":namespace,
                    "pid":pid,"start_ticks":start}))
    }
    #[cfg(not(target_os = "linux"))]
    {
        None
    }
}

pub fn quiescent(identity: &Value) -> Result<bool, Error> {
    quiescent_with(identity, true)
}

fn quiescent_with(identity: &Value, use_pidfd: bool) -> Result<bool, Error> {
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
        let raw_fd = if use_pidfd {
            unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) }
        } else {
            -1
        };
        let pidfd_observed = if raw_fd >= 0 {
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
            true
        } else {
            if use_pidfd {
                match io::Error::last_os_error().raw_os_error() {
                    Some(libc::ESRCH) => return Ok(true),
                    // Linux before 5.3 lacks pidfd_open.
                    Some(libc::ENOSYS) => {}
                    _ => return Err(Error::Storage),
                }
            }
            // kill(2) existence is kernel evidence that hidepid cannot hide.
            if !signal_target_exists(pid as libc::pid_t)? {
                return Ok(true);
            }
            false
        };
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(raw) => {
                let (state, threads, current_start) =
                    process_stat(&raw).map_err(|_| Error::Storage)?;
                // A recycled PID proves that the captured process is gone.
                // Without pidfd, a zombie leader proves group exit only after
                // its kernel thread count drops to the leader itself.
                Ok(current_start != start
                    || (!pidfd_observed && matches!(state, "Z" | "X") && threads <= 1))
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(_) => Err(Error::Storage),
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (identity, use_pidfd);
        Ok(false)
    }
}

#[cfg(target_os = "linux")]
fn signal_target_exists(pid: libc::pid_t) -> Result<bool, Error> {
    if unsafe { libc::kill(pid, 0) } == 0 {
        return Ok(true);
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Ok(false),
        Some(libc::EPERM) => Ok(true),
        _ => Err(Error::Storage),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stat_handles_parentheses_and_never_accepts_missing_start() {
        let raw = format!(
            "23 (worker (name)) Z {} 3 0 456 0",
            vec!["0"; 16].join(" ")
        );
        assert_eq!(process_stat(&raw).unwrap(), ("Z", 3, 456));
        assert!(process_stat("23 (worker) S 0").is_err());
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn live_writer_foreign_kernel_and_pid_namespace_do_not_grant_recovery() {
        let identity = capture().unwrap();
        for use_pidfd in [true, false] {
            assert_eq!(quiescent_with(&identity, use_pidfd), Ok(false));
            let mut foreign = identity.clone();
            foreign["boot_id"] = json!("other-boot");
            assert_eq!(quiescent_with(&foreign, use_pidfd), Ok(false));
            foreign = identity.clone();
            foreign["pid_namespace"] = json!(0);
            assert_eq!(quiescent_with(&foreign, use_pidfd), Ok(false));
        }
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
        if std::env::var_os("TOFU_CHECKPOINT_WRITER_TEST_EXIT_LEADER").is_some() {
            extern "C" fn exit_calling_thread(_: libc::c_int) {
                unsafe { libc::syscall(libc::SYS_exit, 0) };
            }
            // Only the group leader exits; this test thread keeps the group alive.
            unsafe {
                libc::signal(libc::SIGUSR1, exit_calling_thread as libc::sighandler_t);
                let pid = libc::getpid();
                assert_eq!(libc::syscall(libc::SYS_tgkill, pid, pid, libc::SIGUSR1), 0);
            }
        }
        std::fs::rename(temporary, path).unwrap();
        std::thread::sleep(std::time::Duration::from_secs(30));
    }

    #[cfg(target_os = "linux")]
    struct Child(std::process::Child);

    #[cfg(target_os = "linux")]
    impl Drop for Child {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[cfg(target_os = "linux")]
    fn spawn_writer(ready: &std::path::Path, exit_leader: bool) -> (Child, Value) {
        let mut command = std::process::Command::new(std::env::current_exe().unwrap());
        command
            .args(["--exact", "checkpoint_writer::tests::writer_crash_child", "--nocapture"])
            .env("TOFU_CHECKPOINT_WRITER_TEST_READY", ready)
            .stdout(std::process::Stdio::null());
        if exit_leader {
            command.env("TOFU_CHECKPOINT_WRITER_TEST_EXIT_LEADER", "1");
        }
        let mut child = Child(command.spawn().unwrap());
        let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !ready.exists() {
            assert!(child.0.try_wait().unwrap().is_none());
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        let identity = serde_json::from_slice(&std::fs::read(ready).unwrap()).unwrap();
        (child, identity)
    }

    #[cfg(target_os = "linux")]
    fn wait_until_quiescent(identity: &Value, use_pidfd: bool) {
        let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !quiescent_with(identity, use_pidfd).unwrap() {
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn pidfd_requires_real_exit_and_distinguishes_a_recycled_pid_identity() {
        let directory = tempfile::tempdir().unwrap();
        let (mut child, identity) = spawn_writer(&directory.path().join("ready"), false);
        let mut recycled = identity.clone();
        recycled["start_ticks"] = json!(identity["start_ticks"].as_u64().unwrap() + 1);
        for use_pidfd in [true, false] {
            assert_eq!(quiescent_with(&identity, use_pidfd), Ok(false));
            assert_eq!(quiescent_with(&recycled, use_pidfd), Ok(true));
        }
        child.0.kill().unwrap();
        // An exited but unreaped process is observed through its pidfd too.
        for use_pidfd in [true, false] {
            wait_until_quiescent(&identity, use_pidfd);
        }
        child.0.wait().unwrap();
        for use_pidfd in [true, false] {
            assert_eq!(quiescent_with(&identity, use_pidfd), Ok(true));
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn zombie_leader_with_live_threads_is_not_writer_death() {
        let directory = tempfile::tempdir().unwrap();
        let (mut child, identity) = spawn_writer(&directory.path().join("ready"), true);
        let stat_path = format!("/proc/{}/stat", child.0.id());
        let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
        loop {
            let raw = std::fs::read_to_string(&stat_path).unwrap();
            let (state, threads, _) = process_stat(&raw).unwrap();
            if state == "Z" {
                assert!(threads > 1, "the writer's surviving thread must keep its group alive");
                break;
            }
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        for use_pidfd in [true, false] {
            assert_eq!(quiescent_with(&identity, use_pidfd), Ok(false));
        }
        child.0.kill().unwrap();
        for use_pidfd in [true, false] {
            wait_until_quiescent(&identity, use_pidfd);
        }
        child.0.wait().unwrap();
        for use_pidfd in [true, false] {
            assert_eq!(quiescent_with(&identity, use_pidfd), Ok(true));
        }
    }
}
