//! Explicit cross-process filesystem certification for an empty operator path.

use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};
use std::time::{Duration, Instant};

use uuid::Uuid;

use crate::engine::{BatchTransaction, Engine};
use crate::generated_tofudb_ir::{
    FILESYSTEM_CERTIFICATION_MAXIMUM_RETAINED_ENTRIES, FILESYSTEM_CERTIFICATION_PAYLOAD_BLOCK_BYTES,
};

pub const CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS: i32 = 86;

const FIRST_INLINE: &[u8] = b"tofu-db.certification.first";
const SECOND_INLINE: &[u8] = b"tofu-db.certification.second";
const POST_REOPEN_INLINE: &[u8] = b"tofu-db.certification.post-reopen";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FilesystemCertification {
    pub authority_uuid: Uuid,
    pub durable_sequence: u64,
    pub checkpoint_sequence: u64,
    pub payload_segment_count: u32,
    pub payload_block_bytes: u64,
    pub write_process_micros: u64,
    pub first_reopen_micros: u64,
    pub post_reopen_commit_micros: u64,
    pub second_reopen_process_micros: u64,
    pub total_micros: u64,
    pub retained_file_count: u64,
    pub retained_file_bytes: u64,
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn require_status(status: ExitStatus, expected: i32, stage: &str) -> io::Result<()> {
    if status.code() == Some(expected) {
        return Ok(());
    }
    Err(io::Error::other(format!(
        "filesystem certification {stage} process returned {status}"
    )))
}

fn elapsed_micros(duration: Duration) -> u64 {
    u64::try_from(duration.as_micros()).unwrap_or(u64::MAX)
}

fn retained_store_footprint(data_dir: &Path) -> io::Result<(u64, u64)> {
    let mut pending = vec![PathBuf::from(data_dir)];
    let mut scanned_entries = 0_usize;
    let mut file_count = 0_u64;
    let mut file_bytes = 0_u64;
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(directory)? {
            scanned_entries = scanned_entries.checked_add(1).ok_or_else(|| {
                invalid_data("filesystem certification retained entry count overflow")
            })?;
            if scanned_entries > FILESYSTEM_CERTIFICATION_MAXIMUM_RETAINED_ENTRIES {
                return Err(invalid_data(
                    "filesystem certification retained entry bound exceeded",
                ));
            }
            let entry = entry?;
            let metadata = fs::symlink_metadata(entry.path())?;
            if metadata.file_type().is_symlink() {
                return Err(invalid_data(
                    "filesystem certification store unexpectedly contains a symlink",
                ));
            }
            if metadata.is_dir() {
                pending.push(entry.path());
            } else if metadata.is_file() {
                file_count = file_count.checked_add(1).ok_or_else(|| {
                    invalid_data("filesystem certification retained file count overflow")
                })?;
                file_bytes = file_bytes.checked_add(metadata.len()).ok_or_else(|| {
                    invalid_data("filesystem certification retained byte count overflow")
                })?;
            } else {
                return Err(invalid_data(
                    "filesystem certification store contains an unsupported entry",
                ));
            }
        }
    }
    Ok((file_count, file_bytes))
}

fn block_payload() -> Vec<u8> {
    (0..FILESYSTEM_CERTIFICATION_PAYLOAD_BLOCK_BYTES)
        .map(|index| ((index as u64 * 131 + index as u64 / 251) & 0xff) as u8)
        .collect()
}

fn verify_first_two(engine: &Engine) -> io::Result<()> {
    if engine.state().durable_sequence != 2 || engine.state().checkpoint_sequence != 2 {
        return Err(invalid_data(
            "filesystem certification checkpoint witness mismatch",
        ));
    }
    let snapshot = engine.transaction_snapshot()?;
    if snapshot.len() != 2
        || snapshot[0].sequence != 1
        || snapshot[0].envelope.inline_payload != FIRST_INLINE
        || snapshot[0].envelope.block_ids.len() != 1
        || snapshot[1].sequence != 2
        || snapshot[1].envelope.inline_payload != SECOND_INLINE
        || !snapshot[1].envelope.block_ids.is_empty()
    {
        return Err(invalid_data(
            "filesystem certification transaction projection mismatch",
        ));
    }
    if engine.payload_segment_count() != 1
        || engine.read_block(snapshot[0].envelope.block_ids[0])? != block_payload()
    {
        return Err(invalid_data(
            "filesystem certification payload segment or random read mismatch",
        ));
    }
    Ok(())
}

/// Hidden child stage. The caller intentionally exits without dropping the
/// engine after this returns, proving recovery does not depend on destructors.
pub fn write_stage(data_dir: &Path) -> io::Result<()> {
    let mut engine = Engine::initialize(data_dir)?;
    let lock_error = Engine::open(data_dir).err().ok_or_else(|| {
        io::Error::other("filesystem certification lock admitted a second authority")
    })?;
    if lock_error.kind() != io::ErrorKind::WouldBlock {
        return Err(io::Error::new(
            lock_error.kind(),
            format!("filesystem certification lock failed unexpectedly: {lock_error}"),
        ));
    }
    let payload = block_payload();
    engine.commit_batch(&[
        BatchTransaction {
            inline_payload: FIRST_INLINE.to_vec(),
            block_payloads: vec![payload],
        },
        BatchTransaction {
            inline_payload: SECOND_INLINE.to_vec(),
            block_payloads: Vec::new(),
        },
    ])?;
    engine.checkpoint()?;
    let block_id = engine.transaction_snapshot()?[0].envelope.block_ids[0];
    let compaction = engine.compact_payload_blocks(&[block_id])?;
    if compaction.blocks_packed != 1
        || compaction.payload_bytes != FILESYSTEM_CERTIFICATION_PAYLOAD_BLOCK_BYTES as u64
        || compaction.loose_bytes_reclaimed <= compaction.payload_bytes
        || engine.payload_segment_count() != 1
    {
        return Err(invalid_data(
            "filesystem certification payload compaction mismatch",
        ));
    }
    Ok(())
}

/// Hidden second child stage used after the parent has reopened and appended.
pub fn read_stage(data_dir: &Path) -> io::Result<()> {
    let engine = Engine::open(data_dir)?;
    if engine.state().durable_sequence != 3 {
        return Err(invalid_data(
            "filesystem certification post-reopen sequence mismatch",
        ));
    }
    let snapshot = engine.transaction_snapshot()?;
    if snapshot.len() != 3
        || snapshot[2].envelope.inline_payload != POST_REOPEN_INLINE
        || engine.payload_segment_count() != 1
        || engine.read_block(snapshot[0].envelope.block_ids[0])? != block_payload()
    {
        return Err(invalid_data(
            "filesystem certification post-reopen projection mismatch",
        ));
    }
    Ok(())
}

/// Runs only on an explicit empty path. The resulting store is retained as
/// auditable evidence; this function never deletes or overwrites a prior path.
pub fn certify_filesystem(data_dir: &Path) -> io::Result<FilesystemCertification> {
    let total_started = Instant::now();
    let executable = env::current_exe()?;
    let write_started = Instant::now();
    let write_status = Command::new(&executable)
        .arg("__certify-write")
        .arg("--data-dir")
        .arg(data_dir)
        .status()?;
    let write_process_micros = elapsed_micros(write_started.elapsed());
    require_status(
        write_status,
        CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS,
        "write",
    )?;

    let first_reopen_started = Instant::now();
    let mut engine = Engine::open(data_dir)?;
    verify_first_two(&engine)?;
    let first_reopen_micros = elapsed_micros(first_reopen_started.elapsed());
    let authority_uuid = engine.state().authority_uuid;
    let post_reopen_commit_started = Instant::now();
    engine.commit(POST_REOPEN_INLINE)?;
    let post_reopen_commit_micros = elapsed_micros(post_reopen_commit_started.elapsed());
    let durable_sequence = engine.state().durable_sequence;
    let checkpoint_sequence = engine.state().checkpoint_sequence;
    let payload_segment_count = engine.payload_segment_count() as u32;
    drop(engine);

    let second_reopen_started = Instant::now();
    let read_status = Command::new(executable)
        .arg("__certify-read")
        .arg("--data-dir")
        .arg(data_dir)
        .status()?;
    let second_reopen_process_micros = elapsed_micros(second_reopen_started.elapsed());
    require_status(read_status, 0, "read")?;
    let (retained_file_count, retained_file_bytes) = retained_store_footprint(data_dir)?;
    Ok(FilesystemCertification {
        authority_uuid,
        durable_sequence,
        checkpoint_sequence,
        payload_segment_count,
        payload_block_bytes: FILESYSTEM_CERTIFICATION_PAYLOAD_BLOCK_BYTES as u64,
        write_process_micros,
        first_reopen_micros,
        post_reopen_commit_micros,
        second_reopen_process_micros,
        total_micros: elapsed_micros(total_started.elapsed()),
        retained_file_count,
        retained_file_bytes,
    })
}
