//! Explicit child launch mapping for native tool/process adapters.
//! This owner validates launch inputs and clears ambient environment. Permission,
//! sandbox selection, capacity admission and kill/reap remain execution owners.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, PartialEq, Eq)]
pub enum LaunchError {
    InvalidEnvironmentName,
    InvalidEnvironmentValue,
    InvalidArgument,
    InvalidWorkingDirectory,
    ExecutableUnavailable,
}

/// An admitted mapping is immutable; errors never contain credentials or argv.
/// This is a launch-input proof, never permission or a sandbox admission proof.
pub struct Launch {
    executable: PathBuf,
    arguments: Vec<String>,
    directory: PathBuf,
    environment: BTreeMap<String, String>,
}
impl Launch {
    pub fn validate(
        executable: &str,
        arguments: Vec<String>,
        directory: PathBuf,
        environment: BTreeMap<String, String>,
    ) -> Result<Self, LaunchError> {
        for (name, value) in &environment {
            if name.is_empty() || name.contains(['=', '\0']) {
                return Err(LaunchError::InvalidEnvironmentName);
            }
            if value.contains('\0') {
                return Err(LaunchError::InvalidEnvironmentValue);
            }
        }
        if executable.is_empty()
            || executable.contains('\0')
            || arguments.iter().any(|arg| arg.contains('\0'))
        {
            return Err(LaunchError::InvalidArgument);
        }
        // Require the owning adapter to resolve its explicit working directory.
        // Never derive one from the server's current directory.
        if !directory.is_absolute() || !directory.is_dir() {
            return Err(LaunchError::InvalidWorkingDirectory);
        }
        let executable = resolve(executable, &directory, &environment)
            .ok_or(LaunchError::ExecutableUnavailable)?;
        Ok(Self {
            executable,
            arguments,
            directory,
            environment,
        })
    }
    pub fn executable(&self) -> &Path {
        &self.executable
    }
    /// The execution owner must install sandbox/lifecycle policy before spawning.
    /// No implicit shell, inherited stdin, or process-global environment mutation.
    pub fn command(&self) -> Command {
        let mut command = Command::new(&self.executable);
        command
            .args(&self.arguments)
            .current_dir(&self.directory)
            .env_clear()
            .envs(&self.environment)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    }
}

#[cfg(unix)]
fn usable(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    if !path.metadata().is_ok_and(|metadata| metadata.is_file()) {
        return false;
    }
    let Ok(name) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    // SAFETY: the NUL-terminated path remains live for access(2). Match the
    // retained Python os.access/shutil.which policy for the actual Unix identity,
    // including ACL/traversal checks; mode bits alone can choose an unusable file.
    unsafe { libc::access(name.as_ptr(), libc::X_OK) == 0 }
}
#[cfg(not(unix))]
fn usable(_: &Path) -> bool {
    false
}

fn resolve(name: &str, cwd: &Path, environment: &BTreeMap<String, String>) -> Option<PathBuf> {
    let path = Path::new(name);
    if path.is_absolute() || path.components().count() > 1 || name.contains('/') {
        let candidate = if path.is_absolute() {
            path.to_path_buf()
        } else {
            cwd.join(path)
        };
        return usable(&candidate).then_some(candidate);
    }
    // Unix os.defpath compatibility. No fallback to the server's ambient PATH.
    let paths = environment
        .get("PATH")
        .map(String::as_str)
        .unwrap_or("/bin:/usr/bin");
    for entry in paths.split(':') {
        let entry = Path::new(entry);
        let base = if entry.is_absolute() {
            entry.to_path_buf()
        } else {
            cwd.join(entry)
        };
        let candidate = base.join(name);
        if usable(&candidate) {
            return Some(candidate);
        }
    }
    None
}
