//! Bounded on-volume mark runs for backup garbage collection.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::block::BlockId;
use crate::vfs::{sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, Vfs};
use crate::FORMAT_VERSION;

const RUN_MAGIC: &[u8; 8] = b"TDBMRK01";
const RUN_HEADER_BYTES: usize = 8 + 4 + 4;
const RUN_FOOTER_BYTES: usize = 4 + 32;
const IDS_PER_RUN: usize = 65_536;
const MAX_RUNS: usize = 511;
const MAX_LIVE_IDS_PER_SHARD: usize = 1_000_000;
const RUN_MAX_BYTES: usize = RUN_HEADER_BYTES + IDS_PER_RUN * 32 + RUN_FOOTER_BYTES;
const _: () = assert!(MAX_RUNS * RUN_MAX_BYTES <= 1024 * 1024 * 1024);

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct MarkMetrics {
    pub references: u64,
    pub run_count: u32,
    pub spill_bytes: u64,
}

pub(crate) struct SpillMarkSet {
    directory: PathBuf,
    vfs: Arc<dyn Vfs>,
    buffer: Vec<BlockId>,
    runs: Vec<PathBuf>,
    maximum_runs: usize,
    metrics: MarkMetrics,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

impl SpillMarkSet {
    pub(crate) fn initialize(backup_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Self::initialize_with_spill_limit(backup_dir, vfs, 1024 * 1024 * 1024)
    }

    pub(crate) fn initialize_with_spill_limit(
        backup_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_spill_bytes: u64,
    ) -> io::Result<Self> {
        let maximum_runs = usize::try_from(maximum_spill_bytes / RUN_MAX_BYTES as u64)
            .unwrap_or(usize::MAX)
            .min(MAX_RUNS);
        if maximum_runs == 0 {
            return Err(invalid_input(
                "garbage-collection spill budget cannot hold one mark run",
            ));
        }
        let directory = backup_dir.join("gc-runs");
        if vfs.metadata(&directory)? != FileKind::Directory {
            return Err(invalid_data("backup GC run path is not a directory"));
        }
        let entries = vfs.read_directory(&directory)?;
        if entries.len() > MAX_RUNS * 2 {
            return Err(invalid_input("backup GC stale-run bound exceeded"));
        }
        for path in &entries {
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(|| invalid_data("backup GC run filename is not UTF-8"))?;
            if (!name.starts_with("run-") || !name.ends_with(".mark"))
                && !name.starts_with(".run-new-")
            {
                return Err(invalid_data(
                    "backup GC run directory contains an unknown entry",
                ));
            }
            if vfs.metadata(path)? != FileKind::File {
                return Err(invalid_data("backup GC run entry is not a regular file"));
            }
            vfs.remove_file(path)?;
        }
        if !entries.is_empty() {
            sync_directory_barrier(vfs.as_ref(), &directory)?;
        }
        Ok(Self {
            directory,
            vfs,
            buffer: Vec::with_capacity(IDS_PER_RUN),
            runs: Vec::new(),
            maximum_runs,
            metrics: MarkMetrics::default(),
        })
    }

    pub(crate) fn insert(&mut self, block_id: BlockId) -> io::Result<()> {
        self.metrics.references = self
            .metrics
            .references
            .checked_add(1)
            .ok_or_else(|| invalid_data("backup GC reference count overflow"))?;
        self.buffer.push(block_id);
        if self.buffer.len() == IDS_PER_RUN {
            self.flush()?;
        }
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> io::Result<MarkMetrics> {
        if !self.buffer.is_empty() {
            self.flush()?;
        }
        Ok(self.metrics)
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.runs.len() == self.maximum_runs {
            return Err(invalid_input("garbage-collection spill budget exceeded"));
        }
        self.buffer.sort_unstable();
        self.buffer.dedup();
        let run_index = self.runs.len();
        let destination = self.directory.join(format!("run-{run_index:04}.mark"));
        let temporary = self.directory.join(format!(".run-new-{}", Uuid::new_v4()));
        let encoded = encode_run(&self.buffer)?;
        let result = (|| {
            let mut file = self.vfs.open(
                &temporary,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )?;
            file.write_all_at(0, &encoded)?;
            sync_all_barrier(file.as_mut())?;
            self.vfs.rename(&temporary, &destination)?;
            sync_directory_barrier(self.vfs.as_ref(), &self.directory)
        })();
        if result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        result?;
        self.metrics.spill_bytes = self
            .metrics
            .spill_bytes
            .checked_add(encoded.len() as u64)
            .ok_or_else(|| invalid_data("backup GC spill byte count overflow"))?;
        self.runs.push(destination);
        self.metrics.run_count = u32::try_from(self.runs.len())
            .map_err(|_| invalid_data("backup GC run count overflow"))?;
        self.buffer.clear();
        Ok(())
    }

    pub(crate) fn live_for_shard(&self, shard: u8) -> io::Result<Vec<BlockId>> {
        let mut live = Vec::new();
        for path in &self.runs {
            let mut file = self.vfs.open(
                path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            let run = decode_run(&file.read_all(RUN_MAX_BYTES)?)?;
            let start = run.partition_point(|block_id| block_id.0[0] < shard);
            let end = run.partition_point(|block_id| block_id.0[0] <= shard);
            if live.len() + end - start > MAX_LIVE_IDS_PER_SHARD {
                return Err(invalid_input("backup GC live shard bound exceeded"));
            }
            live.extend_from_slice(&run[start..end]);
        }
        live.sort_unstable();
        live.dedup();
        Ok(live)
    }

    pub(crate) fn cleanup(&self) -> io::Result<()> {
        for path in &self.runs {
            match self.vfs.remove_file(path) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        if !self.runs.is_empty() {
            sync_directory_barrier(self.vfs.as_ref(), &self.directory)?;
        }
        Ok(())
    }
}

fn encode_run(block_ids: &[BlockId]) -> io::Result<Vec<u8>> {
    if block_ids.is_empty()
        || block_ids.len() > IDS_PER_RUN
        || block_ids.windows(2).any(|pair| pair[0] >= pair[1])
    {
        return Err(invalid_input(
            "backup GC run is empty, oversized, or unsorted",
        ));
    }
    let mut encoded =
        Vec::with_capacity(RUN_HEADER_BYTES + block_ids.len() * 32 + RUN_FOOTER_BYTES);
    encoded.extend_from_slice(RUN_MAGIC);
    encoded.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    encoded.extend_from_slice(&(block_ids.len() as u32).to_le_bytes());
    for block_id in block_ids {
        encoded.extend_from_slice(&block_id.0);
    }
    let crc = crc32c::crc32c(&encoded);
    encoded.extend_from_slice(&crc.to_le_bytes());
    let digest = blake3::hash(&encoded);
    encoded.extend_from_slice(digest.as_bytes());
    Ok(encoded)
}

fn decode_run(encoded: &[u8]) -> io::Result<Vec<BlockId>> {
    if encoded.len() < RUN_HEADER_BYTES + 32 + RUN_FOOTER_BYTES
        || encoded.len() > RUN_MAX_BYTES
        || &encoded[..8] != RUN_MAGIC
        || u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != FORMAT_VERSION
    {
        return Err(invalid_data("backup GC run header is invalid"));
    }
    let count = u32::from_le_bytes(encoded[12..16].try_into().unwrap()) as usize;
    let expected = count
        .checked_mul(32)
        .and_then(|bytes| RUN_HEADER_BYTES.checked_add(bytes))
        .and_then(|bytes| bytes.checked_add(RUN_FOOTER_BYTES))
        .ok_or_else(|| invalid_data("backup GC run length overflow"))?;
    if count == 0 || count > IDS_PER_RUN || expected != encoded.len() {
        return Err(invalid_data("backup GC run length mismatch"));
    }
    let digest_offset = encoded.len() - 32;
    if blake3::hash(&encoded[..digest_offset]).as_bytes() != &encoded[digest_offset..] {
        return Err(invalid_data("backup GC run BLAKE3 mismatch"));
    }
    let crc_offset = digest_offset - 4;
    if crc32c::crc32c(&encoded[..crc_offset])
        != u32::from_le_bytes(encoded[crc_offset..digest_offset].try_into().unwrap())
    {
        return Err(invalid_data("backup GC run CRC32C mismatch"));
    }
    let mut block_ids = Vec::with_capacity(count);
    for chunk in encoded[RUN_HEADER_BYTES..crc_offset].chunks_exact(32) {
        block_ids.push(BlockId(chunk.try_into().unwrap()));
    }
    if block_ids.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Err(invalid_data("backup GC run is not strictly sorted"));
    }
    Ok(block_ids)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::DeterministicVfs;

    #[test]
    fn spill_runs_deduplicate_and_partition_without_retaining_the_full_set() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/backup")).unwrap();
        vfs.create_dir(Path::new("/backup/gc-runs")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.sync_directory(Path::new("/backup")).unwrap();
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs).unwrap();
        for value in 0..70_000_u32 {
            let mut id = [0_u8; 32];
            id[0] = (value % 2) as u8;
            id[1..5].copy_from_slice(&value.to_be_bytes());
            marks.insert(BlockId(id)).unwrap();
            marks.insert(BlockId(id)).unwrap();
        }
        let metrics = marks.finish().unwrap();
        assert_eq!(metrics.references, 140_000);
        assert_eq!(metrics.run_count, 3);
        assert!(metrics.spill_bytes > 0);
        assert_eq!(marks.live_for_shard(0).unwrap().len(), 35_000);
        assert_eq!(marks.live_for_shard(1).unwrap().len(), 35_000);
        marks.cleanup().unwrap();
    }
}
