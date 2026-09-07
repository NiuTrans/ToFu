//! Immutable, content-addressed blocks written before their referencing commit.

use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};

use uuid::Uuid;

use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::{PayloadSegment, PayloadSegmentStore};
use crate::vfs::{sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs};
use crate::FORMAT_VERSION;

const MAGIC: &[u8; 8] = b"TDBBLK01";
const HEADER_BYTES: usize = 8 + 4 + 4;
const FOOTER_BYTES: usize = 4 + 32;
pub const MAX_BLOCK_BYTES: usize = 4 * 1024 * 1024;
const MAX_BLOCK_FILE_BYTES: u64 = (HEADER_BYTES + MAX_BLOCK_BYTES + FOOTER_BYTES) as u64;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct BlockTemporaryFileCandidate {
    pub path: PathBuf,
    pub file_bytes: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct BlockTemporaryFilePlan {
    pub scanned_entries: u64,
    pub candidate: Option<BlockTemporaryFileCandidate>,
    pub more_candidates: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BlockWriteMetrics {
    pub blocks_written: u64,
    pub bytes_written: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct BlockRemovalMetrics {
    pub blocks_removed: u64,
    pub bytes_removed: u64,
}

#[derive(Clone, Copy, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct BlockId(pub [u8; 32]);

impl fmt::Debug for BlockId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_hex())
    }
}

impl BlockId {
    pub fn for_payload(payload: &[u8]) -> Self {
        let mut hasher = blake3::Hasher::new();
        hasher.update(b"tofu-db:block:v1\0");
        hasher.update(payload);
        Self(*hasher.finalize().as_bytes())
    }

    pub fn to_hex(self) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut result = String::with_capacity(64);
        for byte in self.0 {
            result.push(HEX[(byte >> 4) as usize] as char);
            result.push(HEX[(byte & 0x0f) as usize] as char);
        }
        result
    }

    fn from_hex(value: &str) -> io::Result<Self> {
        if value.len() != 64 {
            return Err(invalid_data("block filename hash length mismatch"));
        }
        let mut bytes = [0_u8; 32];
        for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
            let high = hex_nibble(pair[0])?;
            let low = hex_nibble(pair[1])?;
            bytes[index] = (high << 4) | low;
        }
        Ok(Self(bytes))
    }
}

fn hex_nibble(value: u8) -> io::Result<u8> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'a'..=b'f' => Ok(value - b'a' + 10),
        _ => Err(invalid_data("block filename contains non-canonical hex")),
    }
}

pub struct BlockStore {
    root: PathBuf,
    vfs: Arc<dyn Vfs>,
    payload_segments: Option<PayloadSegmentStore>,
    payload_manifest: RwLock<Option<PayloadManifest>>,
    blocks_written: AtomicU64,
    bytes_written: AtomicU64,
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn encoded_block(id: BlockId, payload: &[u8]) -> io::Result<Vec<u8>> {
    if payload.len() > MAX_BLOCK_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "block exceeds 4 MiB",
        ));
    }
    let mut bytes = Vec::with_capacity(HEADER_BYTES + payload.len() + FOOTER_BYTES);
    bytes.extend_from_slice(MAGIC);
    bytes.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(payload);
    let crc = crc32c::crc32c(&bytes);
    bytes.extend_from_slice(&crc.to_le_bytes());
    bytes.extend_from_slice(&id.0);
    Ok(bytes)
}

impl BlockStore {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("blocks");
        vfs.create_dir(&root)?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        let payload_segments =
            PayloadSegmentStore::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        Ok(Self {
            root,
            vfs,
            payload_segments: Some(payload_segments),
            payload_manifest: RwLock::new(None),
            blocks_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("blocks");
        if vfs.metadata(&root)? != FileKind::Directory {
            return Err(invalid_data("blocks root is not a real directory"));
        }
        let payload_segments = match vfs.metadata(&data_dir.join("payload-segments")) {
            Ok(FileKind::Directory) => Some(PayloadSegmentStore::open_with_vfs(
                data_dir,
                Arc::clone(&vfs),
            )?),
            Ok(FileKind::File) => return Err(invalid_data("payload segment root is a file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        Ok(Self {
            root,
            vfs,
            payload_segments,
            payload_manifest: RwLock::new(None),
            blocks_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
        })
    }

    fn path(&self, id: BlockId) -> PathBuf {
        let hexadecimal = id.to_hex();
        self.root
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"))
    }

    fn ensure_shard(&self, id: BlockId) -> io::Result<PathBuf> {
        let hexadecimal = id.to_hex();
        let shard = self.root.join(&hexadecimal[..2]);
        match self.vfs.create_dir(&shard) {
            Ok(()) => sync_directory_barrier(self.vfs.as_ref(), &self.root)?,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if self.vfs.metadata(&shard)? != FileKind::Directory {
                    return Err(invalid_data("block shard is not a real directory"));
                }
            }
            Err(error) => return Err(error),
        }
        Ok(shard)
    }

    pub fn put(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        match self.get(id) {
            Ok(existing) => {
                if existing != payload {
                    return Err(invalid_data("content-addressed block collision"));
                }
                return Ok(id);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        self.put_loose(payload)
    }

    /// Publishes a loose copy even when the same content is present in a
    /// segment. CONTROL catalog roots use this to avoid a lookup bootstrap
    /// cycle during reopen.
    pub(crate) fn put_loose(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        let destination = self.path(id);
        match self.vfs.metadata(&destination) {
            Ok(FileKind::File) => {
                let existing = self.read_loose_block(id, &destination)?;
                if existing != payload {
                    return Err(invalid_data("content-addressed block collision"));
                }
                return Ok(id);
            }
            Ok(FileKind::Directory) => return Err(invalid_data("block path is a directory")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let shard = self.ensure_shard(id)?;
        let temporary = shard.join(format!(".new-{}", Uuid::new_v4()));
        let encoded = encoded_block(id, payload)?;
        let encoded_len = encoded.len() as u64;
        let write_result = (|| {
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
            sync_directory_barrier(self.vfs.as_ref(), &shard)
        })();
        if write_result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        write_result?;
        self.blocks_written.fetch_add(1, Ordering::Relaxed);
        self.bytes_written.fetch_add(encoded_len, Ordering::Relaxed);
        Ok(id)
    }

    pub fn get(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let path = self.path(id);
        match self.vfs.metadata(&path) {
            Ok(FileKind::File) => return self.read_loose_block(id, &path),
            Ok(FileKind::Directory) => return Err(invalid_data("block is not a regular file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        self.get_from_payload_segments(id)?
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "content block is missing"))
    }

    fn read_loose_block(&self, id: BlockId, path: &Path) -> io::Result<Vec<u8>> {
        let maximum = HEADER_BYTES + MAX_BLOCK_BYTES + FOOTER_BYTES;
        let mut file = self.vfs.open(
            path,
            OpenRequest {
                read: true,
                ..OpenRequest::default()
            },
        )?;
        let length = file.len()?;
        if length > maximum as u64 || length < (HEADER_BYTES + FOOTER_BYTES) as u64 {
            return Err(invalid_data("block has an invalid bounded size"));
        }
        let bytes = file.read_all(maximum)?;
        if &bytes[..8] != MAGIC {
            return Err(invalid_data("block magic mismatch"));
        }
        let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        let payload_len = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        if version != FORMAT_VERSION || HEADER_BYTES + payload_len + FOOTER_BYTES != bytes.len() {
            return Err(invalid_data("block version or length mismatch"));
        }
        let crc_offset = HEADER_BYTES + payload_len;
        let stored_crc = u32::from_le_bytes(bytes[crc_offset..crc_offset + 4].try_into().unwrap());
        if crc32c::crc32c(&bytes[..crc_offset]) != stored_crc {
            return Err(invalid_data("block CRC32C mismatch"));
        }
        let stored_id = BlockId(bytes[crc_offset + 4..].try_into().unwrap());
        let payload = &bytes[HEADER_BYTES..crc_offset];
        if stored_id != id || BlockId::for_payload(payload) != id {
            return Err(invalid_data("block content hash mismatch"));
        }
        Ok(payload.to_vec())
    }

    fn get_from_payload_segments(&self, id: BlockId) -> io::Result<Option<Vec<u8>>> {
        let references = {
            let manifest = self
                .payload_manifest
                .read()
                .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
            manifest
                .as_ref()
                .map(|manifest| manifest.candidate_segments(id).copied().collect::<Vec<_>>())
                .unwrap_or_default()
        };
        if references.is_empty() {
            return Ok(None);
        }
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("CONTROL payload manifest has no payload segment directory")
        })?;
        for reference in references.iter().rev() {
            let segment = store.open_segment(reference.segment_id).map_err(|error| {
                if error.kind() == io::ErrorKind::NotFound {
                    invalid_data("CONTROL payload manifest references a missing segment")
                } else {
                    error
                }
            })?;
            validate_open_segment(&segment, reference)?;
            if let Some(payload) = segment.get(id)? {
                return Ok(Some(payload));
            }
        }
        Ok(None)
    }

    pub(crate) fn install_payload_manifest(&self, manifest: PayloadManifest) -> io::Result<()> {
        if !manifest.segments.is_empty() && self.payload_segments.is_none() {
            return Err(invalid_data(
                "payload manifest requires a payload segment directory",
            ));
        }
        *self
            .payload_manifest
            .write()
            .map_err(|_| io::Error::other("payload manifest lock was poisoned"))? = Some(manifest);
        Ok(())
    }

    pub(crate) fn verify_payload_segment(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<()> {
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("payload segment directory is unavailable for compaction")
        })?;
        let segment = store.open_segment(reference.segment_id)?;
        validate_open_segment(&segment, reference)
    }

    pub(crate) fn payload_segment_store(&self) -> io::Result<&PayloadSegmentStore> {
        self.payload_segments
            .as_ref()
            .ok_or_else(|| invalid_data("payload segment directory is unavailable for compaction"))
    }

    pub fn write_metrics(&self) -> BlockWriteMetrics {
        BlockWriteMetrics {
            blocks_written: self.blocks_written.load(Ordering::Relaxed),
            bytes_written: self.bytes_written.load(Ordering::Relaxed),
        }
    }

    #[cfg(test)]
    pub(crate) fn list_block_ids(&self, maximum_blocks: usize) -> io::Result<Vec<BlockId>> {
        let mut block_ids = Vec::new();
        self.visit_block_shards(maximum_blocks, |_, shard_ids| {
            block_ids.extend(shard_ids);
            Ok(())
        })?;
        block_ids.sort_unstable();
        if block_ids.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(invalid_data("duplicate content block path"));
        }
        Ok(block_ids)
    }

    pub(crate) fn visit_block_shards<F>(
        &self,
        maximum_blocks: usize,
        mut visitor: F,
    ) -> io::Result<()>
    where
        F: FnMut(u8, Vec<BlockId>) -> io::Result<()>,
    {
        if maximum_blocks == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "block listing bound must be positive",
            ));
        }
        let shards = self.block_shard_paths()?;
        let mut total_blocks = 0_usize;
        for (shard_byte, shard) in shards {
            let mut shard_ids = Vec::new();
            let remaining_entries = maximum_blocks - total_blocks;
            let bounded = self
                .vfs
                .read_directory_bounded(&shard, remaining_entries.saturating_add(1))?;
            if bounded.has_more || bounded.entries.len() > remaining_entries {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "block listing bound exceeded",
                ));
            }
            for path in bounded.entries {
                let name = path
                    .file_name()
                    .and_then(|value| value.to_str())
                    .ok_or_else(|| invalid_data("block filename is not UTF-8"))?;
                if name.starts_with(".new-") {
                    continue;
                }
                let hexadecimal = name
                    .strip_suffix(".blk")
                    .ok_or_else(|| invalid_data("block filename suffix is invalid"))?;
                let block_id = BlockId::from_hex(hexadecimal)?;
                if block_id.0[0] != shard_byte || self.vfs.metadata(&path)? != FileKind::File {
                    return Err(invalid_data("block path does not match its identifier"));
                }
                shard_ids.push(block_id);
                total_blocks += 1;
            }
            shard_ids.sort_unstable();
            if shard_ids.windows(2).any(|pair| pair[0] == pair[1]) {
                return Err(invalid_data("duplicate content block path"));
            }
            visitor(shard_byte, shard_ids)?;
        }
        Ok(())
    }

    pub(crate) fn loose_block_shards(&self) -> io::Result<Vec<u8>> {
        Ok(self
            .block_shard_paths()?
            .into_iter()
            .map(|(shard, _)| shard)
            .collect())
    }

    pub(crate) fn plan_temporary_file(
        &self,
        maximum_entries_scanned: usize,
    ) -> io::Result<BlockTemporaryFilePlan> {
        if maximum_entries_scanned == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "temporary block scan bound must be positive",
            ));
        }
        let shards = self.block_shard_paths()?;
        let mut scanned_entries = 0_usize;
        let mut candidate = None;
        let mut more_candidates = false;
        for (index, (_, shard_path)) in shards.iter().enumerate() {
            let remaining = maximum_entries_scanned - scanned_entries;
            if remaining == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "temporary block scan bound exceeded",
                ));
            }
            let matches = self
                .vfs
                .match_directory_prefix_bounded(shard_path, ".new-", remaining, 1)?;
            scanned_entries += matches.scanned_entries;
            for path in matches.matches {
                validate_temporary_block_path(&self.root, &path)?;
                if self.vfs.metadata(&path)? != FileKind::File {
                    return Err(invalid_data("temporary block path is not a regular file"));
                }
                let file_bytes = self
                    .vfs
                    .open(
                        &path,
                        OpenRequest {
                            read: true,
                            ..OpenRequest::default()
                        },
                    )?
                    .len()?;
                if file_bytes > MAX_BLOCK_FILE_BYTES {
                    return Err(invalid_data("temporary block file exceeds block bound"));
                }
                if candidate.is_none() {
                    candidate = Some(BlockTemporaryFileCandidate { path, file_bytes });
                } else {
                    more_candidates = true;
                }
            }
            more_candidates |= matches.has_more_matches;
            if matches.has_more_entries {
                if candidate.is_none() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "temporary block scan bound exceeded",
                    ));
                }
                more_candidates = true;
                break;
            }
            if candidate.is_some() {
                more_candidates |= index + 1 < shards.len();
                break;
            }
        }
        Ok(BlockTemporaryFilePlan {
            scanned_entries: scanned_entries as u64,
            candidate,
            more_candidates,
        })
    }

    pub(crate) fn remove_temporary_file(
        &self,
        candidate: &BlockTemporaryFileCandidate,
    ) -> io::Result<u64> {
        validate_temporary_block_path(&self.root, &candidate.path)?;
        if self.vfs.metadata(&candidate.path)? != FileKind::File {
            return Err(invalid_data("temporary block path is not a regular file"));
        }
        let file_bytes = self
            .vfs
            .open(
                &candidate.path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?
            .len()?;
        if file_bytes != candidate.file_bytes || file_bytes > MAX_BLOCK_FILE_BYTES {
            return Err(invalid_data("temporary block file size changed"));
        }
        self.vfs.remove_file(&candidate.path)?;
        sync_directory_barrier(
            self.vfs.as_ref(),
            candidate.path.parent().expect("validated shard parent"),
        )?;
        Ok(file_bytes)
    }

    fn block_shard_paths(&self) -> io::Result<Vec<(u8, PathBuf)>> {
        let bounded = self.vfs.read_directory_bounded(&self.root, 257)?;
        if bounded.has_more || bounded.entries.len() > 256 {
            return Err(invalid_data("block shard count exceeds 256"));
        }
        let mut shards = Vec::with_capacity(bounded.entries.len());
        for path in bounded.entries {
            if self.vfs.metadata(&path)? != FileKind::Directory {
                return Err(invalid_data("block root contains a non-directory entry"));
            }
            let shard_name = path
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(|| invalid_data("block shard name is not UTF-8"))?;
            if shard_name.len() != 2 {
                return Err(invalid_data("block shard name is not canonical"));
            }
            let shard = (hex_nibble(shard_name.as_bytes()[0])? << 4)
                | hex_nibble(shard_name.as_bytes()[1])?;
            shards.push((shard, path));
        }
        shards.sort_by_key(|(shard, _)| *shard);
        if shards.windows(2).any(|pair| pair[0].0 == pair[1].0) {
            return Err(invalid_data("duplicate block shard path"));
        }
        Ok(shards)
    }

    pub(crate) fn remove_blocks(&self, block_ids: &[BlockId]) -> io::Result<BlockRemovalMetrics> {
        let mut by_shard: BTreeMap<u8, Vec<BlockId>> = BTreeMap::new();
        for block_id in block_ids {
            by_shard.entry(block_id.0[0]).or_default().push(*block_id);
        }
        let mut metrics = BlockRemovalMetrics::default();
        for (_, ids) in by_shard {
            let shard = self.root.join(&ids[0].to_hex()[..2]);
            for id in ids {
                let path = self.path(id);
                let file = self.vfs.open(
                    &path,
                    OpenRequest {
                        read: true,
                        ..OpenRequest::default()
                    },
                )?;
                metrics.bytes_removed = metrics
                    .bytes_removed
                    .checked_add(file.len()?)
                    .ok_or_else(|| invalid_data("removed block byte count overflow"))?;
                self.vfs.remove_file(&path)?;
                metrics.blocks_removed = metrics
                    .blocks_removed
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("removed block count overflow"))?;
            }
            sync_directory_barrier(self.vfs.as_ref(), &shard)?;
        }
        Ok(metrics)
    }

    pub(crate) fn block_file_bytes(&self, block_id: BlockId) -> io::Result<u64> {
        self.vfs
            .open(
                &self.path(block_id),
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?
            .len()
    }
}

fn validate_temporary_block_path(root: &Path, path: &Path) -> io::Result<()> {
    let shard = path
        .parent()
        .filter(|parent| parent.parent() == Some(root))
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .ok_or_else(|| invalid_data("temporary block path escaped its shard"))?;
    if shard.len() != 2 {
        return Err(invalid_data("temporary block shard is malformed"));
    }
    hex_nibble(shard.as_bytes()[0])?;
    hex_nibble(shard.as_bytes()[1])?;
    let identifier = path
        .file_name()
        .and_then(|value| value.to_str())
        .and_then(|name| name.strip_prefix(".new-"))
        .ok_or_else(|| invalid_data("temporary block filename is malformed"))?;
    if identifier.len() != 32
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || Uuid::parse_str(identifier)
            .ok()
            .is_none_or(|uuid| uuid.simple().to_string() != identifier)
    {
        return Err(invalid_data("temporary block filename is malformed"));
    }
    Ok(())
}

fn validate_open_segment(
    segment: &PayloadSegment,
    reference: &PayloadSegmentReference,
) -> io::Result<()> {
    let metadata = segment.metadata();
    if metadata.segment_id != reference.segment_id
        || metadata.block_count != reference.block_count
        || metadata.payload_bytes != reference.payload_bytes
        || metadata.file_bytes != reference.file_bytes
        || segment.first_block_id() != reference.first_block_id
        || segment.last_block_id() != reference.last_block_id
    {
        return Err(invalid_data(
            "payload segment differs from its manifest witness",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::fs::OpenOptions;
    use std::io::{Seek, SeekFrom, Write};

    #[test]
    fn repeated_payload_reuses_one_verified_block() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put(b"large tool result").unwrap();
        let second = store.put(b"large tool result").unwrap();
        assert_eq!(first, second);
        assert_eq!(store.get(first).unwrap(), b"large tool result");
    }

    #[test]
    fn corrupted_block_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let id = store.put(b"durable").unwrap();
        let path = store.path(id);
        let mut file = OpenOptions::new().write(true).open(path).unwrap();
        file.seek(SeekFrom::Start(HEADER_BYTES as u64)).unwrap();
        file.write_all(b"X").unwrap();
        file.sync_all().unwrap();
        assert_eq!(
            store.get(id).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn malformed_temporary_block_names_fail_closed() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let block_id = store.put(b"temporary-name-shard").unwrap();
        let malformed = store
            .root
            .join(&block_id.to_hex()[..2])
            .join(".new-not-a-uuid");
        vfs.open(
            &malformed,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )
        .unwrap();
        assert_eq!(
            store.plan_temporary_file(1024).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    fn simulated_store(vfs: &DeterministicVfs) -> BlockStore {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        BlockStore::initialize_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap()
    }

    #[test]
    fn every_block_publish_operation_fails_to_absent_or_complete() -> io::Result<()> {
        let baseline_vfs = DeterministicVfs::new(None);
        let baseline_store = simulated_store(&baseline_vfs);
        baseline_vfs.arm_fault(None).unwrap();
        let id = baseline_store.put(b"referenced payload").unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncAll));
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::SyncDirectory));

        for operation_number in 1..=trace.len() as u64 {
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = store.put(b"referenced payload");
            vfs.crash().unwrap();
            let recovered = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            match recovered.get(id) {
                Ok(payload) => assert_eq!(payload, b"referenced payload"),
                Err(error) => assert_eq!(error.kind(), io::ErrorKind::NotFound),
            }
        }

        for (index, operation) in trace.iter().enumerate() {
            let action = match operation {
                Operation::Write => Some(FaultAction::ShortWrite(7)),
                Operation::SyncAll | Operation::SyncDirectory => Some(FaultAction::DropSync),
                _ => None,
            };
            let Some(action) = action else {
                continue;
            };
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))?;
            let _ = store.put(b"referenced payload");
            vfs.crash()?;
            let recovered = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            if matches!(action, FaultAction::DropSync) {
                assert_eq!(recovered.get(id)?, b"referenced payload");
            } else {
                assert_eq!(
                    recovered.get(id).unwrap_err().kind(),
                    io::ErrorKind::NotFound
                );
            }
        }
        Ok(())
    }
}
