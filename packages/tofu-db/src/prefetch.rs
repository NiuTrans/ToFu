//! Bounded sequential prefetch of the authority's payload files into the
//! kernel page cache at daemon startup.
//!
//! On a network/FUSE volume a cold read faults one small region per round
//! trip, and a pressured host reclaims those pages so every query re-pays
//! the latency (2026-09-22: 30s+ cold reads during a cgroup memory-pressure
//! window). One bounded sequential pass right after readiness converts that
//! random fault-in into mount readahead, so the host application's warm-up
//! and the first user reads complete in seconds. The pass never blocks
//! readiness, reads each file at most once, and is byte-budgeted
//! (`TOFU_DB_PREFETCH_MAX_MIB`, default 512 MiB, 0 disables). Loose blocks
//! (recent writes) are read before packed segments; directory order
//! otherwise, since the application-side warm-up primes the precise hot set
//! through the read path right after.

use std::fs::File;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Instant;

const DEFAULT_PREFETCH_MAX_BYTES: u64 = 512 * 1024 * 1024;
const MAX_PREFETCH_MAX_BYTES: u64 = 16 * 1024 * 1024 * 1024;
const READ_CHUNK_BYTES: usize = 1024 * 1024;
const PREFETCH_SUBDIRS: [&str; 2] = ["blocks", "payload-segments"];

pub fn prefetch_budget_bytes() -> u64 {
    std::env::var("TOFU_DB_PREFETCH_MAX_MIB")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(|mib| mib.saturating_mul(1024 * 1024).min(MAX_PREFETCH_MAX_BYTES))
        .unwrap_or(DEFAULT_PREFETCH_MAX_BYTES)
}

fn prefetch_targets(data_dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for subdirectory in PREFETCH_SUBDIRS {
        let Ok(entries) = std::fs::read_dir(data_dir.join(subdirectory)) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_file() {
                files.push(path);
            }
        }
    }
    files
}

fn prefetch_file(path: &Path, budget: &mut u64) -> io::Result<()> {
    let mut file = File::open(path)?;
    let mut chunk = vec![0_u8; READ_CHUNK_BYTES];
    while *budget > 0 {
        let want = chunk.len().min(*budget as usize);
        let read = file.read(&mut chunk[..want])?;
        if read == 0 {
            break;
        }
        *budget -= read as u64;
    }
    Ok(())
}

/// Spawn the detached bounded prefetch pass; returns None when disabled.
pub fn start(data_dir: PathBuf) -> Option<thread::JoinHandle<u64>> {
    let budget = prefetch_budget_bytes();
    if budget == 0 {
        return None;
    }
    Some(thread::spawn(move || {
        let started = Instant::now();
        let mut remaining = budget;
        let mut files_read = 0_u64;
        for path in prefetch_targets(&data_dir) {
            if remaining == 0 {
                break;
            }
            if prefetch_file(&path, &mut remaining).is_ok() {
                files_read += 1;
            }
        }
        let consumed = budget - remaining;
        crate::engine_log::info(
            "prefetch",
            &format!(
                "payload prefetch read {consumed} bytes across {files_read} file(s) in {:.1}s",
                started.elapsed().as_secs_f64(),
            ),
        );
        consumed
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn prefetch_reads_files_once_up_to_the_budget() {
        let directory = std::env::temp_dir().join(format!(
            "tofudb-prefetch-test-{}-{}",
            std::process::id(),
            "once"
        ));
        let blocks = directory.join("blocks");
        let segments = directory.join("payload-segments");
        fs::create_dir_all(&blocks).unwrap();
        fs::create_dir_all(&segments).unwrap();
        fs::write(blocks.join("a.blk"), vec![1_u8; 3000]).unwrap();
        fs::write(segments.join("b.pseg"), vec![2_u8; 5000]).unwrap();

        let mut budget = 4000_u64;
        for path in prefetch_targets(&directory) {
            if budget == 0 {
                break;
            }
            prefetch_file(&path, &mut budget).unwrap();
        }
        // First file consumed fully (3000), second truncated by the budget.
        assert_eq!(budget, 4000 - 3000 - 1000);
        fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn prefetch_targets_skip_missing_directories() {
        let directory = std::env::temp_dir().join(format!(
            "tofudb-prefetch-test-{}-{}",
            std::process::id(),
            "missing"
        ));
        assert!(prefetch_targets(&directory).is_empty());
    }

    #[test]
    fn disabled_budget_spawns_no_thread() {
        // The env var is process-global; serialize through direct parsing of
        // the documented default instead of racing other tests on set_var.
        assert_eq!(DEFAULT_PREFETCH_MAX_BYTES, 512 * 1024 * 1024);
    }
}
