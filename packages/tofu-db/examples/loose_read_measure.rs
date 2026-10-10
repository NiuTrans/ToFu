//! Paired real-filesystem read measurement; uses only a private temporary file.
//! Run: cargo run --example loose_read_measure -- <existing-base-directory>.
//! Compares the former duplicate length query with observed-length reuse.
//! This measures warm VFS reads, excluding block verification and application RPC.

use std::io;
use std::path::PathBuf;
use std::time::Instant;
use tofu_db::vfs::{OpenRequest, RealVfs, Vfs};

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(f64::total_cmp);
    (values[values.len() / 2 - 1] + values[values.len() / 2]) / 2.0
}

fn main() -> io::Result<()> {
    let base = std::env::args_os().nth(1).map(PathBuf::from)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "pass an existing base directory"))?;
    let directory = tempfile::Builder::new().prefix(".tofu-loose-read-measure-").tempdir_in(&base)?;
    let path = directory.path().join("probe");
    let payload = vec![b'x'; 64 * 1024];
    std::fs::write(&path, &payload)?;
    std::fs::File::open(&path)?.sync_all()?;
    let expected = blake3::hash(&payload);
    let mut pairs = Vec::new();
    for sample in 0..3 {
        for observed in if sample % 2 == 0 { [false, true] } else { [true, false] } {
            let mut file = RealVfs.open(&path, OpenRequest { read: true, ..OpenRequest::default() })?;
            let mut elapsed = Vec::new();
            for index in 0..144 {
                let started = Instant::now();
                let length = file.len()?;
                let bytes = if observed {
                    file.read_all_with_observed_length(payload.len(), length)?
                } else {
                    file.read_all(payload.len())?
                };
                let milliseconds = started.elapsed().as_secs_f64() * 1000.0;
                assert_eq!(bytes.len(), payload.len());
                assert_eq!(blake3::hash(&bytes), expected);
                if index >= 16 { elapsed.push(milliseconds); }
            }
            let med = median(&mut elapsed);
            pairs.push(serde_json::json!({
                "sample": sample, "observedLength": observed, "medianMs": med,
                "p95Ms": elapsed[(elapsed.len() * 95) / 100], "samples": elapsed,
            }));
        }
    }
    println!("{}", serde_json::json!({
        "contract": "tofu.loose-read-measure/v1", "payloadBytes": payload.len(),
        "outputDigest": expected.to_hex().to_string(), "pairs": pairs,
        "scope": "warm real VFS read with identical handle-length validation",
        "excludes": ["block verification", "cold cache", "application RPC", "whole task latency"],
    }));
    Ok(())
}
