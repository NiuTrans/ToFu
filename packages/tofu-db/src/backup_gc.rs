//! On-volume mark spill for garbage collection. A set appends its sorted runs
//! to one file and grows with the marks it is given: no run count or byte
//! budget caps it. A shard read opens that file once and reads each run's
//! header and only that shard's slice; segment intersection reads each run
//! whole once per window of segments.

use std::io;
use std::ops::Range;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::block::BlockId;
use crate::vfs::{sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, Vfs, VfsFile};
use crate::FORMAT_VERSION;

const RUN_MAGIC: &[u8; 8] = b"TDBMRK01";
const RUN_HEADER_BYTES: usize = 8 + 4 + 4;
const RUN_FOOTER_BYTES: usize = 4 + 32;
const IDS_PER_RUN: usize = 65_536;
const RUN_MAX_BYTES: usize = RUN_HEADER_BYTES + IDS_PER_RUN * 32 + RUN_FOOTER_BYTES;
/// A block ID's first byte names its loose shard.
const SHARDS: usize = u8::MAX as usize + 1;
/// Every run of one set, appended back to back.
const SPILL_FILE_NAME: &str = "spill.mark";

/// Whether `name` is a mark-spill entry: the spill file, or a per-run file or
/// temporary of the earlier one-file-per-run layout, which the first set
/// initialized after an upgrade wipes like any other stale spill.
pub(crate) fn is_spill_entry_name(name: &str) -> bool {
    name == SPILL_FILE_NAME
        || (name.starts_with("run-") && name.ends_with(".mark"))
        || name.starts_with(".run-new-")
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct MarkMetrics {
    pub references: u64,
    pub run_count: u32,
    pub spill_bytes: u64,
}

/// A completed run: where its encoding starts in the spill file, its footer
/// BLAKE3 digest as written and, per shard, where that shard's IDs sit in the
/// run and the BLAKE3 digest of those bytes as written. Only the set that
/// wrote a run reads it back (initialization wipes stale spills), so the
/// index stays in memory, out of reach of the on-volume bytes it verifies.
struct SpillRun {
    offset: u64,
    digest: [u8; 32],
    /// Shard `s` owns the run's IDs `shard_starts[s]..shard_starts[s + 1]`.
    shard_starts: [u32; SHARDS + 1],
    shard_digests: [[u8; 32]; SHARDS],
}

impl SpillRun {
    fn encoded_bytes(&self) -> usize {
        RUN_HEADER_BYTES + self.shard_starts[SHARDS] as usize * 32 + RUN_FOOTER_BYTES
    }
}

/// Byte range of one shard's IDs within an encoded run.
fn shard_byte_range(shard_starts: &[u32; SHARDS + 1], shard: usize) -> Range<usize> {
    RUN_HEADER_BYTES + shard_starts[shard] as usize * 32
        ..RUN_HEADER_BYTES + shard_starts[shard + 1] as usize * 32
}

pub(crate) struct SpillMarkSet {
    directory: PathBuf,
    path: PathBuf,
    vfs: Arc<dyn Vfs>,
    buffer: Vec<BlockId>,
    runs: Vec<SpillRun>,
    /// A flush may have created the spill file; cleanup then removes it.
    spill_opened: bool,
    metrics: MarkMetrics,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

impl SpillMarkSet {
    /// The spill is sized by the marks themselves (at most 32 bytes per
    /// insert, plus 52 bytes per run): it holds whatever the reachable set
    /// needs, and only the volume's actual free space limits it.
    pub(crate) fn initialize(backup_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let directory = backup_dir.join("gc-runs");
        if vfs.metadata(&directory)? != FileKind::Directory {
            return Err(invalid_data("backup GC run path is not a directory"));
        }
        let entries = vfs.read_directory(&directory)?;
        for path in &entries {
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(|| invalid_data("backup GC run filename is not UTF-8"))?;
            if !is_spill_entry_name(name) {
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
            path: directory.join(SPILL_FILE_NAME),
            directory,
            vfs,
            buffer: Vec::with_capacity(IDS_PER_RUN),
            runs: Vec::new(),
            spill_opened: false,
            metrics: MarkMetrics::default(),
        })
    }

    pub(crate) fn insert(&mut self, block_id: BlockId) -> io::Result<()> {
        // A failed flush retains pending marks. Retry it before admitting
        // another reference so repeated I/O errors cannot grow memory.
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
        self.buffer.sort_unstable();
        self.buffer.dedup();
        let encoded = encode_run(&self.buffer)?;
        // Each run starts where the last recorded one ends. A failed append
        // is rewritten in place by its retry, and no read reaches bytes past
        // the last recorded run.
        let offset = self.metrics.spill_bytes;
        let spill_bytes = offset
            .checked_add(encoded.len() as u64)
            .ok_or_else(|| invalid_data("backup GC spill byte count overflow"))?;
        self.spill_opened = true;
        let mut file = self.vfs.open(
            &self.path,
            OpenRequest {
                write: true,
                create: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(offset, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        drop(file);
        if self.runs.is_empty() {
            // The first run names the file; later runs only extend it.
            sync_directory_barrier(self.vfs.as_ref(), &self.directory)?;
        }
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
            offset,
            digest: encoded[encoded.len() - 32..].try_into().unwrap(),
            shard_starts,
            shard_digests,
        });
        self.metrics.spill_bytes = spill_bytes;
        self.metrics.run_count = u32::try_from(self.runs.len())
            .map_err(|_| invalid_data("backup GC run count overflow"))?;
        self.buffer.clear();
        Ok(())
    }

    /// One shard's sorted unique IDs across all completed runs, from one open
    /// of the spill file. Every run's header is checked, and each run
    /// contributes only its slice for `shard`, read at the recorded offset and
    /// verified against the digest recorded when the run was written.
    pub(crate) fn live_for_shard(&self, shard: u8) -> io::Result<Vec<BlockId>> {
        let shard = usize::from(shard);
        if self.runs.is_empty() {
            return Ok(Vec::new());
        }
        let ids = self
            .runs
            .iter()
            .map(|run| (run.shard_starts[shard + 1] - run.shard_starts[shard]) as usize)
            .sum::<usize>();
        let mut live = Vec::with_capacity(ids);
        let mut file = self.open_spill()?;
        let mut header = [0_u8; RUN_HEADER_BYTES];
        let mut bytes = Vec::new();
        for run in &self.runs {
            read_run_bytes(file.as_mut(), run.offset, &mut header)?;
            if header != encode_run_header(run.shard_starts[SHARDS]) {
                return Err(invalid_data("backup GC run header is invalid"));
            }
            let range = shard_byte_range(&run.shard_starts, shard);
            if range.is_empty() {
                continue;
            }
            bytes.resize(range.len(), 0);
            read_run_bytes(file.as_mut(), run.offset + range.start as u64, &mut bytes)?;
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
        if !self.runs.is_empty() {
            let mut file = self.open_spill()?;
            let mut encoded = Vec::new();
            for run in &self.runs {
                encoded.resize(run.encoded_bytes(), 0);
                read_run_bytes(file.as_mut(), run.offset, &mut encoded)?;
                let live_ids = decode_run(&encoded)?;
                // A valid run other than the one this set wrote (a swapped or
                // replaced region) would silently change liveness.
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
        if !self.spill_opened {
            return Ok(());
        }
        match self.vfs.remove_file(&self.path) {
            Ok(()) => sync_directory_barrier(self.vfs.as_ref(), &self.directory),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        }
    }

    fn open_spill(&self) -> io::Result<Box<dyn VfsFile>> {
        self.vfs.open(
            &self.path,
            OpenRequest {
                read: true,
                ..OpenRequest::default()
            },
        )
    }
}

fn encode_run_header(count: u32) -> [u8; RUN_HEADER_BYTES] {
    let mut header = [0_u8; RUN_HEADER_BYTES];
    header[..8].copy_from_slice(RUN_MAGIC);
    header[8..12].copy_from_slice(&FORMAT_VERSION.to_le_bytes());
    header[12..].copy_from_slice(&count.to_le_bytes());
    header
}

fn read_run_bytes(file: &mut dyn VfsFile, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
    file.read_exact_at(offset, bytes)
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
    /// read path that shard-ranged reads must reproduce exactly. It walks the
    /// spill file from its start by each run's encoded count, so it also
    /// checks that the runs lie back to back where the set recorded them and
    /// that the file holds nothing else.
    fn whole_run_shards(marks: &SpillMarkSet) -> Vec<Vec<BlockId>> {
        let mut shards = vec![Vec::new(); SHARDS];
        if marks.runs.is_empty() {
            return shards;
        }
        let spill = marks.open_spill().unwrap().read_all(usize::MAX).unwrap();
        let mut offset = 0;
        for run in &marks.runs {
            assert_eq!(run.offset, offset as u64);
            let count = u32::from_le_bytes(spill[offset + 12..offset + 16].try_into().unwrap());
            let end = offset + RUN_HEADER_BYTES + count as usize * 32 + RUN_FOOTER_BYTES;
            for block_id in decode_run(&spill[offset..end]).unwrap() {
                shards[usize::from(block_id.0[0])].push(block_id);
            }
            offset = end;
        }
        assert_eq!(offset, spill.len());
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
            let whole_run = whole_run_shards(&marks);
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

    /// Release-tree certification past the size the spill was once capped
    /// at (511 runs, 1 GiB): 512 full runs of distinct marks spill into one
    /// file and read back exact per shard. Ignored in the default suite
    /// because it writes more than 1 GiB; run explicitly with
    /// `cargo test --release -- --ignored`.
    #[test]
    #[ignore = "certification writes more than 1 GiB of spill runs"]
    fn spill_set_certifies_marks_past_one_gib() {
        const RUNS: usize = 512;
        let directory = tempfile::tempdir().unwrap();
        let backup = directory.path().join("backup");
        std::fs::create_dir(&backup).unwrap();
        std::fs::create_dir(backup.join("gc-runs")).unwrap();
        let vfs = Arc::new(crate::vfs::RealVfs);
        let mut marks = SpillMarkSet::initialize(&backup, vfs).unwrap();
        let total = (IDS_PER_RUN * RUNS) as u64;
        for value in 0..total {
            let mut id = [0_u8; 32];
            id[0] = (value % 256) as u8;
            id[1..9].copy_from_slice(&value.to_be_bytes());
            id[9..17].copy_from_slice(&(value ^ 0x5a5a_5a5a_5a5a_5a5a).to_be_bytes());
            marks.insert(crate::block::BlockId(id)).unwrap();
        }
        let metrics = marks.finish().unwrap();
        assert_eq!(metrics.references, total);
        assert_eq!(metrics.run_count as usize, RUNS);
        assert_eq!(metrics.spill_bytes, (RUNS * RUN_MAX_BYTES) as u64);
        assert!(metrics.spill_bytes > 1024 * 1024 * 1024);
        let entries = || {
            std::fs::read_dir(backup.join("gc-runs"))
                .unwrap()
                .map(|entry| entry.unwrap().file_name().into_string().unwrap())
                .collect::<Vec<_>>()
        };
        assert_eq!(entries(), [SPILL_FILE_NAME]);
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
        marks.cleanup().unwrap();
        assert!(entries().is_empty());
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

    /// Each operation of a spill append fails in turn (an error before it, a
    /// torn write, or one lost sync), for the run that creates the spill file
    /// and for a run appended after another. A failed append records nothing
    /// and keeps its marks, the runs recorded before it stay exact (also
    /// across a crash), its retry rewrites the run in place, and after a crash
    /// every recorded run reads back exact.
    #[test]
    fn spill_appends_survive_every_fault_and_crash() {
        use crate::vfs::{FaultAction, FaultRule, Operation};

        let id_for = |value: u64| {
            let mut id = [0_u8; 32];
            id[0] = value as u8;
            id[1..9].copy_from_slice(&value.to_be_bytes());
            BlockId(id)
        };
        let exact = |marks: &SpillMarkSet, values: u64| {
            let mut expected = vec![Vec::new(); SHARDS];
            for value in 0..values {
                expected[value as usize % SHARDS].push(id_for(value));
            }
            for live in &mut expected {
                live.sort_unstable();
            }
            (0..=u8::MAX).all(|shard| {
                marks.live_for_shard(shard).unwrap() == expected[usize::from(shard)]
            })
        };
        // `recorded` marks fill the runs before the faulted one, which holds
        // the 1,000 marks `finish` flushes.
        for recorded in [0, IDS_PER_RUN as u64] {
            let prepare = || {
                let vfs = deterministic_backup();
                let mut marks =
                    SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
                for value in 0..recorded + 1_000 {
                    marks.insert(id_for(value)).unwrap();
                }
                vfs.arm_fault(None).unwrap();
                (vfs, marks)
            };
            let (vfs, mut marks) = prepare();
            marks.finish().unwrap();
            let trace = vfs.trace().unwrap();
            let mut expected_trace = vec![
                Operation::Open,
                Operation::Write,
                Operation::SyncAll,
                Operation::SyncAll,
            ];
            if recorded == 0 {
                // Only the first run publishes the file's name.
                expected_trace.extend([Operation::SyncDirectory, Operation::SyncDirectory]);
            }
            assert_eq!(trace, expected_trace);
            for (index, operation) in trace.iter().enumerate() {
                let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
                match operation {
                    Operation::Write => {
                        actions.push(FaultAction::ShortWrite(RUN_HEADER_BYTES + 500 * 32));
                    }
                    Operation::SyncAll | Operation::SyncDirectory => {
                        actions.push(FaultAction::DropSync);
                    }
                    _ => {}
                }
                for action in actions {
                    let lost_sync = action == FaultAction::DropSync;
                    for crash_before_retry in [false, true] {
                        if lost_sync && crash_before_retry {
                            continue;
                        }
                        let case = format!(
                            "{recorded} recorded, {action:?} at {operation:?}, \
                             crash before retry {crash_before_retry}"
                        );
                        let (vfs, mut marks) = prepare();
                        vfs.arm_fault(Some(FaultRule {
                            operation_number: index as u64 + 1,
                            action,
                        }))
                        .unwrap();
                        let result = marks.finish();
                        if lost_sync {
                            // The barrier's second sync covers one lost sync.
                            result.unwrap();
                        } else {
                            assert!(result.is_err(), "{case}");
                            assert_eq!(marks.runs.len(), usize::from(recorded > 0), "{case}");
                            assert_eq!(marks.buffer.len(), 1_000, "{case}");
                            if crash_before_retry {
                                vfs.crash().unwrap();
                            }
                            assert!(exact(&marks, recorded), "{case}");
                            vfs.arm_fault(None).unwrap();
                            marks.finish().unwrap();
                        }
                        vfs.crash().unwrap();
                        assert!(exact(&marks, recorded + 1_000), "{case}");
                        marks.cleanup().unwrap();
                    }
                }
            }
        }
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

    fn spill_file(vfs: &DeterministicVfs, marks: &SpillMarkSet) -> Vec<u8> {
        vfs.open(
            &marks.path,
            OpenRequest {
                read: true,
                ..OpenRequest::default()
            },
        )
        .unwrap()
        .read_all(usize::MAX)
        .unwrap()
    }

    fn replace_spill(vfs: &DeterministicVfs, marks: &SpillMarkSet, bytes: &[u8]) {
        let mut file = vfs
            .open(
                &marks.path,
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

    /// A run valid on its own, with the count and shard layout of `run` but
    /// one ID of shard 0x10 changed: not the run the set wrote there.
    fn forged_run(spill: &[u8], run: &SpillRun) -> Vec<u8> {
        let start = run.offset as usize;
        let mut block_ids = decode_run(&spill[start..start + run.encoded_bytes()]).unwrap();
        block_ids[run.shard_starts[0x10] as usize].0[31] ^= 1;
        encode_run(&block_ids).unwrap()
    }

    #[test]
    fn shard_reads_fail_closed_on_corrupt_truncated_swapped_or_missing_runs() {
        let vfs = deterministic_backup();
        let marks = two_run_marks(vfs.clone());
        let intact = (0..=u8::MAX)
            .map(|shard| marks.live_for_shard(shard).unwrap())
            .collect::<Vec<_>>();
        let spill = spill_file(&vfs, &marks);
        let second = marks.runs[1].offset as usize;
        let invalid = |shard: u8| marks.live_for_shard(shard).unwrap_err().kind();
        let exact = |shard: u8| marks.live_for_shard(shard).unwrap() == intact[usize::from(shard)];

        // A corrupt header of either run fails every shard.
        for start in [0, second] {
            let mut renamed = spill.clone();
            renamed[start..start + 8].copy_from_slice(b"invalid!");
            replace_spill(&vfs, &marks, &renamed);
            for shard in [0, 7, 255] {
                assert_eq!(invalid(shard), io::ErrorKind::InvalidData);
            }
        }

        let mut flipped = spill.clone();
        flipped[shard_byte_range(&marks.runs[0].shard_starts, 7).start + 3] ^= 0x20;
        replace_spill(&vfs, &marks, &flipped);
        assert_eq!(invalid(7), io::ErrorKind::InvalidData);
        // Every other shard's bytes are verified on their own and unchanged.
        assert!(exact(6) && exact(8));

        let cut = second + shard_byte_range(&marks.runs[1].shard_starts, 200).start + 16;
        replace_spill(&vfs, &marks, &spill[..cut]);
        assert_eq!(invalid(200), io::ErrorKind::InvalidData);
        assert_eq!(invalid(255), io::ErrorKind::InvalidData);
        assert!(exact(199));

        // The second run's bytes where the first was written carry another
        // count, so the first run's header fails every shard.
        let mut moved = spill.clone();
        moved[..spill.len() - second].copy_from_slice(&spill[second..]);
        replace_spill(&vfs, &marks, &moved);
        for shard in [0, 128, 255] {
            assert_eq!(invalid(shard), io::ErrorKind::InvalidData);
        }

        // A valid run with the second run's count and shard layout fails
        // only the shard whose bytes it changed.
        let mut swapped = spill.clone();
        swapped[second..].copy_from_slice(&forged_run(&spill, &marks.runs[1]));
        replace_spill(&vfs, &marks, &swapped);
        assert_eq!(invalid(0x10), io::ErrorKind::InvalidData);
        assert!(exact(0x0f) && exact(0x11));

        vfs.remove_file(&marks.path).unwrap();
        assert_eq!(invalid(0), io::ErrorKind::NotFound);
        replace_spill(&vfs, &marks, &spill);
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
        // One open, then per run its header and the shard's slice.
        assert_eq!(
            trace,
            [
                Operation::Open,
                Operation::Read,
                Operation::Read,
                Operation::Read,
                Operation::Read,
            ]
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

    /// A shard of more than a million IDs across 16 runs reads back whole (no
    /// per-shard bound applies), and a shard whose slice is empty in every run
    /// reads only the run headers after one open.
    #[test]
    fn dense_shards_read_back_whole_and_empty_slices_read_only_headers() {
        use crate::vfs::Operation;

        const DENSE_IDS: u64 = 1_000_001;
        let vfs = deterministic_backup();
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        for value in 0..DENSE_IDS {
            let mut id = [0_u8; 32];
            id[1..9].copy_from_slice(&value.to_be_bytes());
            marks.insert(BlockId(id)).unwrap();
        }
        let runs = marks.finish().unwrap().run_count as usize;
        assert_eq!(runs, 16);
        let live = marks.live_for_shard(0).unwrap();
        assert_eq!(live.len() as u64, DENSE_IDS);
        assert!(live.windows(2).all(|pair| pair[0] < pair[1]));
        vfs.arm_fault(None).unwrap();
        assert!(marks.live_for_shard(1).unwrap().is_empty());
        let mut expected_trace = vec![Operation::Open];
        expected_trace.extend([Operation::Read].repeat(runs));
        assert_eq!(vfs.trace().unwrap(), expected_trace);
        marks.cleanup().unwrap();
    }

    /// Initialization durably wipes an earlier set's spill in either layout
    /// (the spill file, or per-run files and their temporaries) and refuses a
    /// directory holding any other entry, leaving that entry in place.
    #[test]
    fn initialization_wipes_stale_spills_and_refuses_unknown_entries() {
        let vfs = deterministic_backup();
        let runs = Path::new("/backup/gc-runs");
        let create = |name: &str| {
            let mut file = vfs
                .open(
                    &runs.join(name),
                    OpenRequest {
                        write: true,
                        create_new: true,
                        ..OpenRequest::default()
                    },
                )
                .unwrap();
            file.write_all_at(0, b"stale").unwrap();
            sync_all_barrier(file.as_mut()).unwrap();
            sync_directory_barrier(vfs.as_ref(), runs).unwrap();
        };
        let refused = || {
            SpillMarkSet::initialize(Path::new("/backup"), vfs.clone())
                .err()
                .map(|error| error.kind())
        };
        for name in [
            SPILL_FILE_NAME,
            "run-0000.mark",
            "run-0510.mark",
            ".run-new-6f1c3a52-9d0e-4b7a-8c21-5e4f3d2a1b09",
        ] {
            create(name);
        }
        let mut marks = SpillMarkSet::initialize(Path::new("/backup"), vfs.clone()).unwrap();
        vfs.crash().unwrap();
        assert!(vfs.read_directory(runs).unwrap().is_empty());
        marks.insert(BlockId([7; 32])).unwrap();
        marks.finish().unwrap();
        assert_eq!(marks.live_for_shard(7).unwrap(), [BlockId([7; 32])]);
        marks.cleanup().unwrap();
        for name in ["run-0000.mark.partial", "spill.mark.old", "notes"] {
            create(name);
            assert_eq!(refused(), Some(io::ErrorKind::InvalidData), "{name}");
            assert_eq!(vfs.metadata(&runs.join(name)).unwrap(), FileKind::File);
            vfs.remove_file(&runs.join(name)).unwrap();
        }
        vfs.create_dir(&runs.join("run-0001.mark")).unwrap();
        assert_eq!(refused(), Some(io::ErrorKind::InvalidData));
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
    /// several runs opens the spill once and reads each run once, whole, so
    /// it reads no more than the spill itself.
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
        let mut expected_trace = vec![Operation::Open];
        expected_trace.extend([Operation::Read].repeat(RUNS as usize));
        assert_eq!(inner.trace().unwrap(), expected_trace);
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
            marks.finish().unwrap();
            let live = inserted.iter().copied().collect::<BTreeSet<_>>();
            let whole_run = whole_run_shards(&marks)
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
        assert_eq!(trace, [Operation::Open, Operation::Read, Operation::Read]);
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
        let spill = spill_file(&vfs, &marks);
        // Magic and an ID of the first run, the CRC32C and the BLAKE3 footer
        // of the second.
        for offset in [
            3,
            RUN_HEADER_BYTES + 7 * 32 + 5,
            spill.len() - 33,
            spill.len() - 1,
        ] {
            let mut flipped = spill.clone();
            flipped[offset] ^= 0x20;
            replace_spill(&vfs, &marks, &flipped);
            assert_eq!(
                failed(),
                io::ErrorKind::InvalidData,
                "flipped byte {offset}"
            );
        }
        replace_spill(&vfs, &marks, &spill[..spill.len() - 1]);
        assert_eq!(failed(), io::ErrorKind::InvalidData);
        // Valid on its own, but not the run this set wrote there.
        let mut swapped = spill.clone();
        let second = marks.runs[1].offset as usize;
        swapped[second..].copy_from_slice(&forged_run(&spill, &marks.runs[1]));
        replace_spill(&vfs, &marks, &swapped);
        assert_eq!(failed(), io::ErrorKind::InvalidData);
        vfs.remove_file(&marks.path).unwrap();
        assert_eq!(failed(), io::ErrorKind::NotFound);
        replace_spill(&vfs, &marks, &spill);
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
