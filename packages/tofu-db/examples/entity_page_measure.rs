//! Measure the production entity write/merge/checkpoint/reopen lifecycle.
//! Usage: entity_page_measure <existing-authority-parent> [rows] [value-bytes].
//! Reports staging work (not physical device writes) and complete exact readback.
//! All authority bytes live in a fresh, self-cleaning directory on that volume.
use std::{io, path::PathBuf, time::Instant};
use tofu_db::entity::{EntityDatabase, EntityKey};

fn key(index: u32) -> EntityKey {
    EntityKey::new(7, 11, "lifecycle_index",
        format!("conversation/0123456789abcdef0123456789abcdef/attempt/0123456789abcdef/entity/{index:08}").as_bytes())
        .expect("bounded benchmark key")
}

fn value(index: u32, bytes: usize) -> Vec<u8> {
    (0..bytes)
        .map(|offset| index.wrapping_mul(17).wrapping_add(offset as u32 * 13) as u8)
        .collect()
}

fn main() -> io::Result<()> {
    let arguments: Vec<_> = std::env::args().collect();
    let parent = arguments
        .get(1)
        .map(PathBuf::from)
        .filter(|path| path.is_dir())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "an existing authority parent is required",
            )
        })?;
    let rows: u32 = arguments
        .get(2)
        .map_or(Ok(12_000), |v| v.parse())
        .ok()
        .filter(|n| (256..=100_000).contains(n))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "rows must be 256..100000"))?;
    let bytes: usize = arguments
        .get(3)
        .map_or(Ok(64), |v| v.parse())
        .ok()
        .filter(|n| *n <= 1024)
        .ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "value bytes must be 0..1024")
        })?;
    let directory = tempfile::tempdir_in(parent)?;
    let mut database = EntityDatabase::initialize(directory.path())?;
    let started = Instant::now();
    for first in (0..rows).step_by(256) {
        let mut transaction = database.begin(7, 11)?;
        for index in first..(first + 256).min(rows) {
            database.put(&mut transaction, key(index), value(index, bytes))?;
        }
        database.commit(transaction)?;
    }
    database.merge_overlay_fully()?;
    let seed_ms = started.elapsed().as_secs_f64() * 1000.;
    // Keep the seed snapshot alive across updates and deletes; old values remain
    // observable while the current tree's compact pages are published.
    let mut old_reader = database.begin(7, 11)?;
    let started = Instant::now();
    for first in (0..rows).step_by(256) {
        let mut transaction = database.begin(7, 11)?;
        for index in first..(first + 256).min(rows) {
            if index % 5 == 0 {
                database.delete(&mut transaction, key(index))?;
            } else if index % 3 == 0 {
                database.put(&mut transaction, key(index), value(index + rows, bytes))?;
            }
        }
        database.commit(transaction)?;
    }
    database.merge_overlay_fully()?;
    let churn_ms = started.elapsed().as_secs_f64() * 1000.;
    for index in (0..rows).step_by(113) {
        if database.get(&mut old_reader, &key(index))? != Some(value(index, bytes)) {
            return Err(io::Error::other("pinned old value mismatch"));
        }
    }
    drop(old_reader);
    let work = database.writer_engine_mut()?.block_write_metrics();
    let started = Instant::now();
    database.writer_engine_mut()?.checkpoint()?;
    drop(database);
    let checkpoint_close_ms = started.elapsed().as_secs_f64() * 1000.;
    let started = Instant::now();
    let database = EntityDatabase::open(directory.path())?;
    let reopen_ms = started.elapsed().as_secs_f64() * 1000.;
    database.set_page_cache_capacity_bytes(0)?;
    let started = Instant::now();
    let mut read = database.begin(7, 11)?;
    let mut digest = blake3::Hasher::new();
    for index in 0..rows {
        let actual = database.get(&mut read, &key(index))?;
        let expected = if index % 5 == 0 {
            None
        } else {
            Some(value(
                if index % 3 == 0 { index + rows } else { index },
                bytes,
            ))
        };
        if actual != expected {
            return Err(io::Error::other("reopened entity mismatch"));
        }
        digest.update(&index.to_le_bytes());
        digest.update(&[u8::from(actual.is_some())]);
        if let Some(actual) = actual {
            digest.update(&actual);
        }
    }
    let readback_ms = started.elapsed().as_secs_f64() * 1000.;
    println!(
        "{}",
        serde_json::json!({"rows":rows,"valueBytes":bytes,"seedMs":seed_ms,
        "churnMs":churn_ms,"checkpointCloseMs":checkpoint_close_ms,"reopenMs":reopen_ms,
        "readbackWithoutDecodedCacheMs":readback_ms,"stagedBlocks":work.blocks_staged,
        "stagedBytes":work.bytes_staged,"looseBlocksWritten":work.blocks_written,
        "looseBytesWritten":work.bytes_written,"exactRows":rows,
        "digest":digest.finalize().to_hex().to_string(),
        "scope":"entity-lifecycle;staging-counters-not-device-writes;no-capacity-SLO-claim"})
    );
    Ok(())
}
