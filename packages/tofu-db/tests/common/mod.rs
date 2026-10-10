//! Candidate selection for process-level release tests; never silently falls
//! back when an explicit certification executable is invalid.

use std::path::PathBuf;

pub fn native_binary() -> PathBuf {
    let path = std::env::var_os("TOFUDB_CERTIFICATION_BIN")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_tofu-db")));
    assert!(path.is_absolute(), "certification executable must be an absolute path");
    assert!(path.is_file(), "certification executable does not exist: {}", path.display());
    path
}
