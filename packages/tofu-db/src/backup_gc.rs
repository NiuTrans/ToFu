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
        // A failed flush retains pending marks. Retry it before admitting
        // another reference so repeated budget/I/O errors cannot grow memory.
        if self.buffer.len() >= IDS_PER_RUN {
            self.flush()?;
        }
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

    /// Intersect one sorted segment index with every completed mark run.
    /// At most one run is decoded at once; membership is bounded by one run's
    /// ID capacity, independent of the number of hash partitions represented.
    pub(crate) fn retain_live_ids(&self, block_ids: &mut Vec<BlockId>) -> io::Result<()> {
        if !self.buffer.is_empty()
            || block_ids.len() > IDS_PER_RUN
            || block_ids.windows(2).any(|pair| pair[0] >= pair[1])
        {
            return Err(invalid_input(
                "GC intersection requires completed marks and one bounded sorted unique ID set",
            ));
        }
        if block_ids.is_empty() {
            return Ok(());
        }
        let mut retained = vec![false; block_ids.len()];
        for path in &self.runs {
            let mut file = self.vfs.open(
                path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            let run = decode_run(&file.read_all(RUN_MAX_BYTES)?)?;
            let mut candidate = 0;
            for live_id in run {
                while candidate < block_ids.len() && block_ids[candidate] < live_id {
                    candidate += 1;
                }
                if candidate == block_ids.len() {
                    break;
                }
                if block_ids[candidate] == live_id {
                    retained[candidate] = true;
                    candidate += 1;
                }
            }
        }
        // Keep the caller's candidate set intact if any mark read fails.
        let mut index = 0;
        block_ids.retain(|_| {
            let keep = retained[index];
            index += 1;
            keep
        });
        Ok(())
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
    for chunk in encoded[RUN_HEADER_BYTES..crc_offset].as_chunks::<32>().0 {
        block_ids.push(BlockId(*chunk));
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
    fn exact_intersection_spans_runs_and_hashes_with_bounded_atomic_reads() {
        use crate::vfs::{FaultAction, FaultRule, Operation};

        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/backup")).unwrap();
        vfs.create_dir(Path::new("/backup/gc-runs")).unwrap();
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        let id_for = |value: u32| {
            let mut id = [0_u8; 32];
            id[0] = (value % 256) as u8;
            id[1..5].copy_from_slice(&value.to_be_bytes());
            BlockId(id)
        };
        for value in 0..70_000 {
            marks.insert(id_for(value)).unwrap();
        }
        assert_eq!(marks.finish().unwrap().run_count, 2);
        let mut candidates = (0..100_000).step_by(2).map(id_for).collect::<Vec<_>>();
        candidates.sort_unstable();
        let mut expected = (0..70_000).step_by(2).map(id_for).collect::<Vec<_>>();
        expected.sort_unstable();
        vfs.arm_fault(None).unwrap();
        let mut actual = candidates.clone();
        marks.retain_live_ids(&mut actual).unwrap();
        assert_eq!(actual, expected);
        let trace = vfs.trace().unwrap();
        assert_eq!(
            trace
                .iter()
                .filter(|operation| **operation == Operation::Read)
                .count(),
            2
        );
        for index in 0..trace.len() {
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let mut actual = candidates.clone();
            assert_eq!(
                marks.retain_live_ids(&mut actual).unwrap_err().kind(),
                io::ErrorKind::Interrupted
            );
            assert_eq!(actual, candidates);
        }
        vfs.arm_fault(None).unwrap();
        let mut oversized = (0..=IDS_PER_RUN)
            .map(|value| id_for(value as u32))
            .collect::<Vec<_>>();
        oversized.sort_unstable();
        assert_eq!(
            marks.retain_live_ids(&mut oversized).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let mut unsorted = vec![id_for(2), id_for(1)];
        assert_eq!(
            marks.retain_live_ids(&mut unsorted).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(vfs.trace().unwrap().is_empty());
        marks.cleanup().unwrap();
    }

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

    /// Release-tree certification at the full on-volume budget: exactly
    /// MAX_RUNS × IDS_PER_RUN marks (33,488,896 ≈ 33.4 million) must fit the
    /// default 1 GiB spill limit, read back exact per shard, and refuse one
    /// more run. Ignored in the default suite because it writes 1 GiB;
    /// run explicitly with `cargo test --release -- --ignored`.
    #[test]
    #[ignore = "full-budget certification writes 1 GiB of spill runs"]
    fn spill_set_certifies_full_budget_scale() {
        let directory = tempfile::tempdir().unwrap();
        let backup = directory.path().join("backup");
        std::fs::create_dir(&backup).unwrap();
        std::fs::create_dir(backup.join("gc-runs")).unwrap();
        let vfs = Arc::new(crate::vfs::RealVfs);
        let mut marks = SpillMarkSet::initialize(&backup, vfs).unwrap();
        let total = (IDS_PER_RUN * MAX_RUNS) as u64;
        for value in 0..total {
            let mut id = [0_u8; 32];
            id[0] = (value % 256) as u8;
            id[1..9].copy_from_slice(&value.to_be_bytes());
            id[9..17].copy_from_slice(&(value ^ 0x5a5a_5a5a_5a5a_5a5a).to_be_bytes());
            marks.insert(crate::block::BlockId(id)).unwrap();
        }
        let metrics = marks.finish().unwrap();
        assert_eq!(metrics.references, total);
        assert_eq!(metrics.run_count as usize, MAX_RUNS);
        assert!(metrics.spill_bytes <= 1024 * 1024 * 1024);
        assert!(
            metrics.spill_bytes as usize > (MAX_RUNS - 1) * RUN_MAX_BYTES,
            "full-budget run must actually spill near the 1 GiB ceiling"
        );
        for shard in [0_u8, 128, 255] {
            let live = marks.live_for_shard(shard).unwrap();
            let expected = total / 256;
            assert_eq!(live.len() as u64, expected);
            assert!(live.windows(2).all(|pair| pair[0] < pair[1]));
            assert!(live.iter().all(|id| id.0[0] == shard));
            let first_value = u64::from(shard);
            assert_eq!(
                u64::from_be_bytes(live[0].0[1..9].try_into().unwrap()),
                first_value
            );
        }
        // The budget is exact: one further full buffer cannot spill.
        for value in 0..(IDS_PER_RUN - 1) as u64 {
            let mut id = [0xff_u8; 32];
            id[1..9].copy_from_slice(&value.to_be_bytes());
            marks.insert(crate::block::BlockId(id)).unwrap();
        }
        let error = marks.insert(crate::block::BlockId([0xff; 32])).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        marks.cleanup().unwrap();
    }

    #[test]
    fn spill_budget_rejection_remains_bounded_on_repeated_insert() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::create_dir(directory.path().join("gc-runs")).unwrap();
        let mut marks = SpillMarkSet::initialize_with_spill_limit(
            directory.path(),
            Arc::new(crate::vfs::RealVfs),
            RUN_MAX_BYTES as u64,
        )
        .unwrap();
        for value in 0..(2 * IDS_PER_RUN - 1) as u64 {
            let mut id = [0_u8; 32];
            id[..8].copy_from_slice(&value.to_be_bytes());
            marks.insert(BlockId(id)).unwrap();
        }
        assert_eq!(
            marks.insert(BlockId([0xfe; 32])).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let references = marks.metrics.references;
        for _ in 0..8 {
            assert_eq!(
                marks.insert(BlockId([0xff; 32])).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
            assert_eq!(marks.buffer.len(), IDS_PER_RUN);
            assert_eq!(marks.metrics.references, references);
        }
        assert!(marks.metrics.spill_bytes <= RUN_MAX_BYTES as u64);
        marks.cleanup().unwrap();
    }

    #[test]
    fn spill_io_failure_retry_preserves_bounds_and_durable_marks() {
        use crate::vfs::{FaultAction, FaultRule};

        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/backup")).unwrap();
        vfs.create_dir(Path::new("/backup/gc-runs")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.sync_directory(Path::new("/backup")).unwrap();
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        let id_for = |value: u64| {
            let mut id = [0_u8; 32];
            id[..8].copy_from_slice(&value.to_be_bytes());
            BlockId(id)
        };
        for value in 0..(IDS_PER_RUN - 1) as u64 {
            marks.insert(id_for(value)).unwrap();
        }
        for _ in 0..4 {
            vfs.arm_fault(Some(FaultRule {
                operation_number: 1,
                action: FaultAction::ErrorBefore(io::ErrorKind::StorageFull),
            }))
            .unwrap();
            assert_eq!(
                marks
                    .insert(id_for((IDS_PER_RUN - 1) as u64))
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::StorageFull,
            );
            assert_eq!(marks.buffer.len(), IDS_PER_RUN);
            assert_eq!(marks.metrics.references, IDS_PER_RUN as u64);
        }
        vfs.arm_fault(None).unwrap();
        let final_id = BlockId([0xff; 32]);
        marks.insert(final_id).unwrap();
        let metrics = marks.finish().unwrap();
        assert_eq!(metrics.references, IDS_PER_RUN as u64 + 1);
        assert_eq!(metrics.run_count, 2);
        assert!(metrics.spill_bytes <= 2 * RUN_MAX_BYTES as u64);
        vfs.crash().unwrap();
        assert_eq!(
            marks.live_for_shard(0).unwrap(),
            (0..IDS_PER_RUN as u64).map(id_for).collect::<Vec<_>>()
        );
        assert_eq!(marks.live_for_shard(0xff).unwrap(), vec![final_id]);
        marks.cleanup().unwrap();
    }
}
