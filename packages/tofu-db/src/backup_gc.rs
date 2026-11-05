//! Bounded on-volume mark runs for backup garbage collection. Segment
//! intersection reads each run whole once per window of segments; a shard
//! read reads each run's header and only that shard's slice.

use std::io;
use std::ops::Range;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::block::BlockId;
use crate::vfs::{sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, Vfs, VfsFile};
use crate::FORMAT_VERSION;

const RUN_MAGIC: &[u8; 8] = b"TDBMRK01";
const RUN_HEADER_BYTES: usize = 8 + 4 + 4;
const RUN_FOOTER_BYTES: usize = 4 + 32;
const IDS_PER_RUN: usize = 65_536;
const MAX_RUNS: usize = 511;
const MAX_LIVE_IDS_PER_SHARD: usize = 1_000_000;
const RUN_MAX_BYTES: usize = RUN_HEADER_BYTES + IDS_PER_RUN * 32 + RUN_FOOTER_BYTES;
const _: () = assert!(MAX_RUNS * RUN_MAX_BYTES <= 1024 * 1024 * 1024);
/// A block ID's first byte names its loose shard.
const SHARDS: usize = u8::MAX as usize + 1;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct MarkMetrics {
    pub references: u64,
    pub run_count: u32,
    pub spill_bytes: u64,
}

/// A completed run, its footer BLAKE3 digest as written and, per shard, where
/// that shard's IDs sit in the run and the BLAKE3 digest of those bytes as
/// written. Only the set that wrote a run reads it back (initialization wipes
/// stale runs), so the index stays in memory, out of reach of the on-volume
/// bytes it verifies.
struct SpillRun {
    path: PathBuf,
    digest: [u8; 32],
    /// Shard `s` owns the run's IDs `shard_starts[s]..shard_starts[s + 1]`.
    shard_starts: [u32; SHARDS + 1],
    shard_digests: [[u8; 32]; SHARDS],
}

/// Byte range of one shard's IDs within an encoded run.
fn shard_byte_range(shard_starts: &[u32; SHARDS + 1], shard: usize) -> Range<usize> {
    RUN_HEADER_BYTES + shard_starts[shard] as usize * 32
        ..RUN_HEADER_BYTES + shard_starts[shard + 1] as usize * 32
}

pub(crate) struct SpillMarkSet {
    directory: PathBuf,
    vfs: Arc<dyn Vfs>,
    buffer: Vec<BlockId>,
    runs: Vec<SpillRun>,
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
        let mut shard_starts = [0_u32; SHARDS + 1];
        for block_id in &self.buffer {
            shard_starts[usize::from(block_id.0[0]) + 1] += 1;
        }
        for shard in 0..SHARDS {
            shard_starts[shard + 1] += shard_starts[shard];
        }
        let shard_digests = std::array::from_fn(|shard| {
            *blake3::hash(&encoded[shard_byte_range(&shard_starts, shard)]).as_bytes()
        });
        self.runs.push(SpillRun {
            path: destination,
            digest: encoded[encoded.len() - 32..].try_into().unwrap(),
            shard_starts,
            shard_digests,
        });
        self.metrics.run_count = u32::try_from(self.runs.len())
            .map_err(|_| invalid_data("backup GC run count overflow"))?;
        self.buffer.clear();
        Ok(())
    }

    /// One shard's sorted unique IDs across all completed runs. Every run's
    /// header is checked, and each run contributes only its slice for
    /// `shard`, read at the recorded offset and verified against the digest
    /// recorded when the run was written.
    pub(crate) fn live_for_shard(&self, shard: u8) -> io::Result<Vec<BlockId>> {
        let shard = usize::from(shard);
        let ids = self
            .runs
            .iter()
            .map(|run| (run.shard_starts[shard + 1] - run.shard_starts[shard]) as usize)
            .sum::<usize>();
        if ids > MAX_LIVE_IDS_PER_SHARD {
            return Err(invalid_input("backup GC live shard bound exceeded"));
        }
        let mut live = Vec::with_capacity(ids);
        let mut header = [0_u8; RUN_HEADER_BYTES];
        let mut bytes = Vec::new();
        for run in &self.runs {
            let mut file = self.vfs.open(
                &run.path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            read_run_bytes(file.as_mut(), 0, &mut header)?;
            if header != encode_run_header(run.shard_starts[SHARDS]) {
                return Err(invalid_data("backup GC run header is invalid"));
            }
            let range = shard_byte_range(&run.shard_starts, shard);
            if range.is_empty() {
                continue;
            }
            bytes.resize(range.len(), 0);
            read_run_bytes(file.as_mut(), range.start, &mut bytes)?;
            if blake3::hash(&bytes).as_bytes() != &run.shard_digests[shard] {
                return Err(invalid_data("backup GC run shard BLAKE3 mismatch"));
            }
            live.extend(
                bytes
                    .as_chunks::<32>()
                    .0
                    .iter()
                    .map(|chunk| BlockId(*chunk)),
            );
        }
        live.sort_unstable();
        live.dedup();
        Ok(live)
    }

    /// Intersect one sorted segment index with every completed mark run.
    pub(crate) fn retain_live_ids(&self, block_ids: &mut Vec<BlockId>) -> io::Result<()> {
        self.retain_live_ids_per_segment(std::slice::from_mut(block_ids))
    }

    /// Intersect each sorted segment index of one scan window with every
    /// completed mark run, reading and verifying each run once for the whole
    /// window. At most one run is decoded at once; besides the caller's IDs,
    /// membership costs one flag per ID, independent of the number of hash
    /// partitions represented.
    pub(crate) fn retain_live_ids_per_segment(
        &self,
        segments: &mut [Vec<BlockId>],
    ) -> io::Result<()> {
        if !self.buffer.is_empty()
            || segments.iter().any(|block_ids| {
                block_ids.len() > IDS_PER_RUN || block_ids.windows(2).any(|pair| pair[0] >= pair[1])
            })
        {
            return Err(invalid_input(
                "GC intersection requires completed marks and bounded sorted unique ID sets",
            ));
        }
        if segments.iter().all(Vec::is_empty) {
            return Ok(());
        }
        let mut retained = segments
            .iter()
            .map(|block_ids| vec![false; block_ids.len()])
            .collect::<Vec<_>>();
        for run in &self.runs {
            let mut file = self.vfs.open(
                &run.path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            let encoded = file.read_all(RUN_MAX_BYTES)?;
            let live_ids = decode_run(&encoded)?;
            // A valid run other than the one this set wrote (a swapped or
            // replaced file) would silently change liveness.
            if encoded[encoded.len() - 32..] != run.digest {
                return Err(invalid_data("backup GC run is not the run this set wrote"));
            }
            for (block_ids, retained) in segments.iter().zip(&mut retained) {
                let mut candidate = 0;
                for live_id in &live_ids {
                    while candidate < block_ids.len() && block_ids[candidate] < *live_id {
                        candidate += 1;
                    }
                    if candidate == block_ids.len() {
                        break;
                    }
                    if block_ids[candidate] == *live_id {
                        retained[candidate] = true;
                        candidate += 1;
                    }
                }
            }
        }
        // Keep every caller's candidate set intact if any mark read fails.
        for (block_ids, retained) in segments.iter_mut().zip(retained) {
            let mut index = 0;
            block_ids.retain(|_| {
                let keep = retained[index];
                index += 1;
                keep
            });
        }
        Ok(())
    }

    pub(crate) fn cleanup(&self) -> io::Result<()> {
        for run in &self.runs {
            match self.vfs.remove_file(&run.path) {
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

fn encode_run_header(count: u32) -> [u8; RUN_HEADER_BYTES] {
    let mut header = [0_u8; RUN_HEADER_BYTES];
    header[..8].copy_from_slice(RUN_MAGIC);
    header[8..12].copy_from_slice(&FORMAT_VERSION.to_le_bytes());
    header[12..].copy_from_slice(&count.to_le_bytes());
    header
}

fn read_run_bytes(file: &mut dyn VfsFile, offset: usize, bytes: &mut [u8]) -> io::Result<()> {
    file.read_exact_at(offset as u64, bytes)
        .map_err(|error| match error.kind() {
            io::ErrorKind::UnexpectedEof => invalid_data("backup GC run is truncated"),
            _ => error,
        })
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
    encoded.extend_from_slice(&encode_run_header(block_ids.len() as u32));
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
    use std::collections::BTreeSet;
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;
    use crate::vfs::{BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs};

    /// Counts the bytes every file read returns.
    struct ReadCountingVfs {
        inner: Arc<DeterministicVfs>,
        bytes_read: Arc<AtomicU64>,
    }

    struct ReadCountingFile {
        inner: Box<dyn VfsFile>,
        bytes_read: Arc<AtomicU64>,
    }

    impl Vfs for ReadCountingVfs {
        fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
            Ok(Box::new(ReadCountingFile {
                inner: self.inner.open(path, request)?,
                bytes_read: Arc::clone(&self.bytes_read),
            }))
        }
        fn create_dir(&self, path: &Path) -> io::Result<()> {
            self.inner.create_dir(path)
        }
        fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
            self.inner.rename(source, destination)
        }
        fn remove_file(&self, path: &Path) -> io::Result<()> {
            self.inner.remove_file(path)
        }
        fn metadata(&self, path: &Path) -> io::Result<FileKind> {
            self.inner.metadata(path)
        }
        fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
            self.inner.read_directory(path)
        }
        fn read_directory_bounded(
            &self,
            path: &Path,
            maximum: usize,
        ) -> io::Result<BoundedDirectoryEntries> {
            self.inner.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(
            &self,
            path: &Path,
            prefix: &str,
            maximum: usize,
            matches: usize,
        ) -> io::Result<BoundedDirectoryMatches> {
            self.inner
                .match_directory_prefix_bounded(path, prefix, maximum, matches)
        }
        fn sync_directory(&self, path: &Path) -> io::Result<()> {
            self.inner.sync_directory(path)
        }
    }

    impl VfsFile for ReadCountingFile {
        fn len(&self) -> io::Result<u64> {
            self.inner.len()
        }
        fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
            let bytes = self.inner.read_all(maximum)?;
            self.bytes_read
                .fetch_add(bytes.len() as u64, Ordering::Relaxed);
            Ok(bytes)
        }
        fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.inner.read_exact_at(offset, bytes)?;
            self.bytes_read
                .fetch_add(bytes.len() as u64, Ordering::Relaxed);
            Ok(())
        }
        fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
            self.inner.write_all_at(offset, bytes)
        }
        fn set_len(&mut self, length: u64) -> io::Result<()> {
            self.inner.set_len(length)
        }
        fn sync_data(&mut self) -> io::Result<()> {
            self.inner.sync_data()
        }
        fn sync_all(&mut self) -> io::Result<()> {
            self.inner.sync_all()
        }
        fn try_lock_exclusive(&mut self) -> io::Result<()> {
            self.inner.try_lock_exclusive()
        }
    }

    fn deterministic_backup() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/backup")).unwrap();
        vfs.create_dir(Path::new("/backup/gc-runs")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.sync_directory(Path::new("/backup")).unwrap();
        vfs
    }

    fn splitmix64(state: &mut u64) -> u64 {
        *state = state.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut mixed = *state;
        mixed = (mixed ^ (mixed >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        mixed = (mixed ^ (mixed >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        mixed ^ (mixed >> 31)
    }

    /// Every shard's answer from decoding each run in full, the whole-run
    /// read path that shard-ranged reads must reproduce exactly.
    fn whole_run_shards(marks: &SpillMarkSet, run_count: u32) -> Vec<Vec<BlockId>> {
        let mut shards = vec![Vec::new(); 256];
        for index in 0..run_count {
            let path = marks.directory.join(format!("run-{index:04}.mark"));
            let mut file = marks
                .vfs
                .open(
                    &path,
                    OpenRequest {
                        read: true,
                        ..OpenRequest::default()
                    },
                )
                .unwrap();
            let run = decode_run(&file.read_all(RUN_MAX_BYTES).unwrap()).unwrap();
            for (shard, live) in shards.iter_mut().enumerate() {
                let start = run.partition_point(|block_id| usize::from(block_id.0[0]) < shard);
                let end = run.partition_point(|block_id| usize::from(block_id.0[0]) <= shard);
                live.extend_from_slice(&run[start..end]);
            }
        }
        for live in &mut shards {
            live.sort_unstable();
            live.dedup();
        }
        shards
    }

    /// A loose scan asks for all 256 shards of a mark; together those calls
    /// read each spilled ID once, plus each run's header once per shard.
    #[test]
    fn all_shard_reads_together_read_each_spilled_id_once() {
        const RUNS: u64 = 8;
        let vfs = Arc::new(ReadCountingVfs {
            inner: deterministic_backup(),
            bytes_read: Arc::default(),
        });
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        let inserts = (RUNS - 1) * IDS_PER_RUN as u64 + IDS_PER_RUN as u64 / 2;
        for value in 0..inserts {
            let mut id = [0_u8; 32];
            id[0] = value as u8;
            id[1..9].copy_from_slice(&value.to_be_bytes());
            marks.insert(BlockId(id)).unwrap();
        }
        let metrics = marks.finish().unwrap();
        assert_eq!(u64::from(metrics.run_count), RUNS);
        vfs.bytes_read.store(0, Ordering::Relaxed);
        let mut live = 0;
        for shard in 0..=u8::MAX {
            let shard_live = marks.live_for_shard(shard).unwrap();
            assert!(shard_live.iter().all(|block_id| block_id.0[0] == shard));
            live += shard_live.len() as u64;
        }
        assert_eq!(live, inserts);
        let bytes_read = vfs.bytes_read.load(Ordering::Relaxed);
        let header_bytes = u64::from(metrics.run_count) * (SHARDS * RUN_HEADER_BYTES) as u64;
        assert!(
            bytes_read <= metrics.spill_bytes + header_bytes,
            "256 shard reads read {bytes_read} bytes of {} run bytes in {} runs",
            metrics.spill_bytes,
            metrics.run_count,
        );
        marks.cleanup().unwrap();
    }

    /// Random marks with duplicate inserts within and across runs, empty
    /// shards, a shard filling a whole run and a set without runs.
    #[test]
    fn shard_reads_match_whole_run_reads_for_random_marks() {
        let every_shard = (0..=u8::MAX).collect::<Vec<_>>();
        let cases: [(u64, Vec<u8>, usize); 6] = [
            (1, Vec::new(), 0),
            (2, vec![0x42], 1),
            (3, vec![0x42], IDS_PER_RUN + 1),
            (4, vec![0x00, 0x01, 0x80, 0xfe, 0xff], IDS_PER_RUN + 4_321),
            (5, every_shard.clone(), 2 * IDS_PER_RUN + 777),
            (6, every_shard, 5_000),
        ];
        for (seed, shards, inserts) in cases {
            let vfs = deterministic_backup();
            let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs).unwrap();
            let mut state = seed;
            let mut inserted = Vec::with_capacity(inserts);
            for _ in 0..inserts {
                let block_id = if !inserted.is_empty() && splitmix64(&mut state) % 4 == 0 {
                    inserted[(splitmix64(&mut state) % inserted.len() as u64) as usize]
                } else {
                    let mut id = [0_u8; 32];
                    for word in id.chunks_mut(8) {
                        word.copy_from_slice(&splitmix64(&mut state).to_le_bytes());
                    }
                    id[0] = shards[(splitmix64(&mut state) % shards.len() as u64) as usize];
                    BlockId(id)
                };
                marks.insert(block_id).unwrap();
                inserted.push(block_id);
            }
            let metrics = marks.finish().unwrap();
            assert_eq!(metrics.references, inserts as u64);
            let mut expected = vec![Vec::new(); 256];
            for block_id in inserted.iter().copied().collect::<BTreeSet<_>>() {
                expected[usize::from(block_id.0[0])].push(block_id);
            }
            let whole_run = whole_run_shards(&marks, metrics.run_count);
            for shard in 0..=u8::MAX {
                let live = marks.live_for_shard(shard).unwrap();
                assert_eq!(
                    live,
                    whole_run[usize::from(shard)],
                    "seed {seed} shard {shard}"
                );
                assert_eq!(
                    live,
                    expected[usize::from(shard)],
                    "seed {seed} shard {shard}"
                );
                assert_eq!(
                    live.is_empty(),
                    !shards.contains(&shard),
                    "seed {seed} shard {shard}"
                );
            }
            marks.cleanup().unwrap();
        }
    }

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

    /// A full run and a 1,000-ID run, each with IDs in every shard.
    fn two_run_marks(vfs: Arc<dyn Vfs>) -> SpillMarkSet {
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs).unwrap();
        for value in 0..(IDS_PER_RUN + 1_000) as u64 {
            let mut id = [0_u8; 32];
            id[0] = value as u8;
            id[1..9].copy_from_slice(&value.to_be_bytes());
            marks.insert(BlockId(id)).unwrap();
        }
        assert_eq!(marks.finish().unwrap().run_count, 2);
        marks
    }

    fn run_bytes(vfs: &DeterministicVfs, path: &Path) -> Vec<u8> {
        let mut file = vfs
            .open(
                path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.read_all(RUN_MAX_BYTES).unwrap()
    }

    fn rewrite_run(vfs: &DeterministicVfs, path: &Path, bytes: &[u8]) {
        let mut file = vfs
            .open(
                path,
                OpenRequest {
                    write: true,
                    create: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.set_len(bytes.len() as u64).unwrap();
        file.write_all_at(0, bytes).unwrap();
    }

    #[test]
    fn shard_reads_fail_closed_on_corrupt_truncated_swapped_or_missing_runs() {
        let vfs = deterministic_backup();
        let marks = two_run_marks(vfs.clone());
        let intact = (0..=u8::MAX)
            .map(|shard| marks.live_for_shard(shard).unwrap())
            .collect::<Vec<_>>();
        let (first, second) = (marks.runs[0].path.clone(), marks.runs[1].path.clone());
        let (first_bytes, second_bytes) = (run_bytes(&vfs, &first), run_bytes(&vfs, &second));
        let invalid = |shard: u8| marks.live_for_shard(shard).unwrap_err().kind();
        let exact = |shard: u8| marks.live_for_shard(shard).unwrap() == intact[usize::from(shard)];

        let mut renamed = first_bytes.clone();
        renamed[..8].copy_from_slice(b"invalid!");
        rewrite_run(&vfs, &first, &renamed);
        for shard in [0, 7, 255] {
            assert_eq!(invalid(shard), io::ErrorKind::InvalidData);
        }
        rewrite_run(&vfs, &first, &first_bytes);

        let mut flipped = first_bytes.clone();
        flipped[shard_byte_range(&marks.runs[0].shard_starts, 7).start + 3] ^= 0x20;
        rewrite_run(&vfs, &first, &flipped);
        assert_eq!(invalid(7), io::ErrorKind::InvalidData);
        // Every other shard's bytes are verified on their own and unchanged.
        assert!(exact(6) && exact(8));
        rewrite_run(&vfs, &first, &first_bytes);

        let cut = shard_byte_range(&marks.runs[1].shard_starts, 200).start + 16;
        rewrite_run(&vfs, &second, &second_bytes[..cut]);
        assert_eq!(invalid(200), io::ErrorKind::InvalidData);
        assert_eq!(invalid(255), io::ErrorKind::InvalidData);
        assert!(exact(199));
        rewrite_run(&vfs, &second, &second_bytes);

        rewrite_run(&vfs, &first, &second_bytes);
        for shard in [0, 128, 255] {
            assert_eq!(invalid(shard), io::ErrorKind::InvalidData);
        }
        rewrite_run(&vfs, &first, &first_bytes);

        vfs.remove_file(&second).unwrap();
        assert_eq!(invalid(0), io::ErrorKind::NotFound);
        rewrite_run(&vfs, &second, &second_bytes);
        assert!((0..=u8::MAX).all(exact));
        marks.cleanup().unwrap();
    }

    #[test]
    fn every_failed_shard_read_operation_fails_the_shard() {
        use crate::vfs::{FaultAction, FaultRule, Operation};

        let vfs = deterministic_backup();
        let marks = two_run_marks(vfs.clone());
        vfs.arm_fault(None).unwrap();
        let expected = marks.live_for_shard(0x5a).unwrap();
        let trace = vfs.trace().unwrap();
        // Per run: open, header, shard slice.
        assert_eq!(
            trace,
            [Operation::Open, Operation::Read, Operation::Read].repeat(2)
        );
        for index in 0..trace.len() {
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            assert_eq!(
                marks.live_for_shard(0x5a).unwrap_err().kind(),
                io::ErrorKind::Interrupted
            );
        }
        vfs.arm_fault(None).unwrap();
        assert_eq!(marks.live_for_shard(0x5a).unwrap(), expected);
        marks.cleanup().unwrap();
    }

    #[test]
    fn shard_bound_fails_before_reading_and_empty_slices_read_only_headers() {
        use crate::vfs::Operation;

        let vfs = deterministic_backup();
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        for value in 0..=MAX_LIVE_IDS_PER_SHARD as u64 {
            let mut id = [0_u8; 32];
            id[1..9].copy_from_slice(&value.to_be_bytes());
            marks.insert(BlockId(id)).unwrap();
        }
        let runs = marks.finish().unwrap().run_count as usize;
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            marks.live_for_shard(0).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(vfs.trace().unwrap().is_empty());
        assert!(marks.live_for_shard(1).unwrap().is_empty());
        assert_eq!(
            vfs.trace().unwrap(),
            [Operation::Open, Operation::Read].repeat(runs)
        );
        marks.cleanup().unwrap();
    }

    fn random_id(state: &mut u64) -> BlockId {
        let mut id = [0_u8; 32];
        for word in id.chunks_mut(8) {
            word.copy_from_slice(&splitmix64(state).to_le_bytes());
        }
        BlockId(id)
    }

    fn sorted_unique(mut block_ids: Vec<BlockId>) -> Vec<BlockId> {
        block_ids.sort_unstable();
        block_ids.dedup();
        block_ids
    }

    /// Each segment's IDs that `live` holds, in order.
    fn live_subsets(window: &[Vec<BlockId>], live: impl Fn(&BlockId) -> bool) -> Vec<Vec<BlockId>> {
        window
            .iter()
            .map(|block_ids| block_ids.iter().copied().filter(|id| live(id)).collect())
            .collect()
    }

    /// A window of the full catalog width (16 segments) against a mark of
    /// several runs opens and reads each run once, whole, so it reads no more
    /// than the spill itself.
    #[test]
    fn window_intersection_reads_each_run_once() {
        use crate::vfs::Operation;

        const RUNS: u64 = 5;
        let inner = deterministic_backup();
        let vfs = Arc::new(ReadCountingVfs {
            inner: inner.clone(),
            bytes_read: Arc::default(),
        });
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        let mut state = 0x5eed;
        let inserted = (0..(RUNS - 1) * IDS_PER_RUN as u64 + 1)
            .map(|_| random_id(&mut state))
            .collect::<Vec<_>>();
        for block_id in &inserted {
            marks.insert(*block_id).unwrap();
        }
        let metrics = marks.finish().unwrap();
        assert_eq!(u64::from(metrics.run_count), RUNS);
        let window = (0..16)
            .map(|segment| {
                sorted_unique(
                    inserted
                        .iter()
                        .skip(segment)
                        .step_by(211)
                        .copied()
                        .chain((0..300).map(|_| random_id(&mut state)))
                        .collect(),
                )
            })
            .collect::<Vec<_>>();
        let live = inserted.iter().copied().collect::<BTreeSet<_>>();
        let expected = live_subsets(&window, |id| live.contains(id));
        inner.arm_fault(None).unwrap();
        vfs.bytes_read.store(0, Ordering::Relaxed);
        let mut actual = window.clone();
        marks.retain_live_ids_per_segment(&mut actual).unwrap();
        let bytes_read = vfs.bytes_read.load(Ordering::Relaxed);
        assert_eq!(actual, expected);
        assert_eq!(
            inner.trace().unwrap(),
            [Operation::Open, Operation::Read].repeat(RUNS as usize)
        );
        let header_bytes = u64::from(metrics.run_count) * RUN_HEADER_BYTES as u64;
        assert!(
            bytes_read <= metrics.spill_bytes + header_bytes,
            "a 16-segment window read {bytes_read} bytes of {} run bytes in {} runs",
            metrics.spill_bytes,
            metrics.run_count,
        );
        marks.cleanup().unwrap();
    }

    /// Random marks (duplicate inserts within and across runs, or no run at
    /// all) against random 16-segment windows with an empty segment, the ID
    /// extremes, IDs in no run, IDs shared by several segments and, for even
    /// seeds, one full-size segment. Each segment keeps exactly the IDs some
    /// run holds, which is also its answer when intersected alone.
    #[test]
    fn window_intersection_matches_per_segment_answers_for_random_ids() {
        let cases: [(u64, usize); 4] = [
            (21, 0),
            (22, 5_000),
            (23, IDS_PER_RUN + 1),
            (24, 3 * IDS_PER_RUN + 4_321),
        ];
        for (seed, inserts) in cases {
            let vfs = deterministic_backup();
            let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs).unwrap();
            let mut state = seed;
            let mut inserted = Vec::with_capacity(inserts);
            for _ in 0..inserts {
                let block_id = if !inserted.is_empty() && splitmix64(&mut state) % 4 == 0 {
                    inserted[(splitmix64(&mut state) % inserted.len() as u64) as usize]
                } else {
                    random_id(&mut state)
                };
                marks.insert(block_id).unwrap();
                inserted.push(block_id);
            }
            let metrics = marks.finish().unwrap();
            let live = inserted.iter().copied().collect::<BTreeSet<_>>();
            let whole_run = whole_run_shards(&marks, metrics.run_count)
                .into_iter()
                .flatten()
                .collect::<BTreeSet<_>>();
            assert_eq!(whole_run, live, "seed {seed}");

            let mut window = vec![Vec::new(), vec![BlockId([0; 32]), BlockId([0xff; 32])]];
            while window.len() < 16 {
                let length = if window.len() == 15 && seed % 2 == 0 {
                    IDS_PER_RUN
                } else {
                    (splitmix64(&mut state) % 2_000) as usize
                };
                let earlier = window.iter().flatten().copied().collect::<Vec<_>>();
                let mut block_ids = BTreeSet::new();
                while block_ids.len() < length {
                    let pick = splitmix64(&mut state);
                    block_ids.insert(match pick % 4 {
                        0 | 1 if !inserted.is_empty() => {
                            inserted[(pick >> 8) as usize % inserted.len()]
                        }
                        2 if !earlier.is_empty() => earlier[(pick >> 8) as usize % earlier.len()],
                        _ => random_id(&mut state),
                    });
                }
                window.push(block_ids.into_iter().collect());
            }
            let distinct = window.iter().flatten().collect::<BTreeSet<_>>();
            assert!(window.iter().map(Vec::len).sum::<usize>() > distinct.len());
            assert!(distinct.iter().any(|id| !live.contains(*id)));
            assert_eq!(inserts == 0, !distinct.iter().any(|id| live.contains(*id)));

            let expected = live_subsets(&window, |id| live.contains(id));
            let mut actual = window.clone();
            marks.retain_live_ids_per_segment(&mut actual).unwrap();
            assert_eq!(actual, expected, "seed {seed}");
            for (block_ids, expected) in window.iter().zip(&expected) {
                let mut alone = block_ids.clone();
                marks.retain_live_ids(&mut alone).unwrap();
                assert_eq!(&alone, expected, "seed {seed}");
            }
            marks.cleanup().unwrap();
        }
    }

    /// Every failed read operation, and a corrupt, truncated, swapped or
    /// missing run, fails the whole window and leaves every segment intact.
    #[test]
    fn window_intersection_fails_closed_and_keeps_every_segment_intact() {
        use crate::vfs::{FaultAction, FaultRule, Operation};

        let vfs = deterministic_backup();
        let marks = two_run_marks(vfs.clone());
        // two_run_marks marks the values below `marked` with this layout.
        let marked = (IDS_PER_RUN + 1_000) as u64;
        let id_for = |value: u64| {
            let mut id = [0_u8; 32];
            id[0] = value as u8;
            id[1..9].copy_from_slice(&value.to_be_bytes());
            BlockId(id)
        };
        let window = vec![
            Vec::new(),
            sorted_unique((0..3_000).step_by(3).map(id_for).collect()),
            sorted_unique((marked - 1_500..marked + 500).map(id_for).collect()),
        ];
        let expected = live_subsets(&window, |id| {
            u64::from_be_bytes(id.0[1..9].try_into().unwrap()) < marked
        });
        vfs.arm_fault(None).unwrap();
        let mut actual = window.clone();
        marks.retain_live_ids_per_segment(&mut actual).unwrap();
        assert_eq!(actual, expected);
        let trace = vfs.trace().unwrap();
        assert_eq!(trace, [Operation::Open, Operation::Read].repeat(2));
        for index in 0..trace.len() {
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let mut actual = window.clone();
            assert_eq!(
                marks
                    .retain_live_ids_per_segment(&mut actual)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::Interrupted
            );
            assert_eq!(actual, window);
        }
        vfs.arm_fault(None).unwrap();
        let failed = || {
            let mut actual = window.clone();
            let kind = marks
                .retain_live_ids_per_segment(&mut actual)
                .unwrap_err()
                .kind();
            assert_eq!(actual, window, "a failed window keeps every candidate");
            kind
        };
        let (first, second) = (marks.runs[0].path.clone(), marks.runs[1].path.clone());
        let (first_bytes, second_bytes) = (run_bytes(&vfs, &first), run_bytes(&vfs, &second));
        // Magic, an ID, the CRC32C and the BLAKE3 footer.
        for (path, original, offset) in [
            (&first, &first_bytes, 3),
            (&first, &first_bytes, RUN_HEADER_BYTES + 7 * 32 + 5),
            (&second, &second_bytes, second_bytes.len() - 33),
            (&second, &second_bytes, second_bytes.len() - 1),
        ] {
            let mut flipped = original.clone();
            flipped[offset] ^= 0x20;
            rewrite_run(&vfs, path, &flipped);
            assert_eq!(
                failed(),
                io::ErrorKind::InvalidData,
                "flipped byte {offset}"
            );
            rewrite_run(&vfs, path, original);
        }
        rewrite_run(&vfs, &second, &second_bytes[..second_bytes.len() - 1]);
        assert_eq!(failed(), io::ErrorKind::InvalidData);
        rewrite_run(&vfs, &second, &second_bytes);
        // Valid on its own, but not the run this set wrote there.
        rewrite_run(&vfs, &first, &second_bytes);
        assert_eq!(failed(), io::ErrorKind::InvalidData);
        rewrite_run(&vfs, &first, &first_bytes);
        vfs.remove_file(&second).unwrap();
        assert_eq!(failed(), io::ErrorKind::NotFound);
        rewrite_run(&vfs, &second, &second_bytes);
        let mut actual = window.clone();
        marks.retain_live_ids_per_segment(&mut actual).unwrap();
        assert_eq!(actual, expected);
        marks.cleanup().unwrap();
    }

    /// One oversized, unsorted or duplicated segment, or marks still
    /// buffering, fail the whole window before any read and keep every
    /// segment; a window of empty segments reads nothing.
    #[test]
    fn window_intersection_validates_every_segment_before_reading() {
        let vfs = deterministic_backup();
        let mut marks = two_run_marks(vfs.clone());
        let id_for = |value: u32| {
            let mut id = [0_u8; 32];
            id[1..5].copy_from_slice(&value.to_be_bytes());
            BlockId(id)
        };
        let valid = vec![id_for(1), id_for(2)];
        let oversized = (0..=IDS_PER_RUN as u32).map(id_for).collect::<Vec<_>>();
        vfs.arm_fault(None).unwrap();
        for invalid in [
            vec![id_for(2), id_for(1)],
            vec![id_for(1), id_for(1)],
            oversized,
        ] {
            let window = vec![valid.clone(), Vec::new(), invalid];
            let mut actual = window.clone();
            assert_eq!(
                marks
                    .retain_live_ids_per_segment(&mut actual)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::InvalidInput
            );
            assert_eq!(actual, window);
        }
        let mut empty: Vec<Vec<BlockId>> = vec![Vec::new(); 16];
        marks.retain_live_ids_per_segment(&mut empty).unwrap();
        marks.insert(id_for(3)).unwrap();
        let window = vec![valid];
        let mut actual = window.clone();
        assert_eq!(
            marks
                .retain_live_ids_per_segment(&mut actual)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(actual, window);
        assert!(vfs.trace().unwrap().is_empty());
        marks.cleanup().unwrap();
    }
}
