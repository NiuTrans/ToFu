//! Immutable payload-segment framing, bounded index admission, and random reads.
//!
//! This module owns physical pack files; `payload_manifest` and `CONTROL` own
//! membership publication. Normal engine open never scans this directory.

use std::collections::BTreeSet;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::block::{BlockId, MAX_BLOCK_BYTES};
use crate::generated_tofudb_ir::MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED;
use crate::vfs::{
    sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile,
};
use crate::FORMAT_VERSION;

const HEADER_MAGIC: &[u8; 8] = b"TDBSEG01";
const TRAILER_MAGIC: &[u8; 8] = b"TDBSGEND";
const HEADER_BYTES: usize = 48;
const INDEX_ENTRY_BYTES: usize = 48;
const TRAILER_PREFIX_BYTES: usize = 28;
const TRAILER_BYTES: usize = TRAILER_PREFIX_BYTES + 32;
pub const MAX_SEGMENT_PAYLOAD_BYTES: u64 = 256 * 1024 * 1024;
pub const MAX_SEGMENT_BLOCKS: usize = 65_536;
pub const MAX_SEGMENT_INDEX_BYTES: usize = MAX_SEGMENT_BLOCKS * INDEX_ENTRY_BYTES;
pub const MAX_SEGMENT_FILE_BYTES: u64 = HEADER_BYTES as u64
    + MAX_SEGMENT_PAYLOAD_BYTES
    + MAX_SEGMENT_INDEX_BYTES as u64
    + TRAILER_BYTES as u64;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PayloadSegmentOrphanCandidate {
    pub file_name: String,
    pub segment_id: Option<Uuid>,
    pub file_bytes: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct PayloadSegmentOrphanPlan {
    pub scanned_files: u32,
    pub candidate: Option<PayloadSegmentOrphanCandidate>,
    pub more_candidates: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PayloadSegmentMetadata {
    pub segment_id: Uuid,
    pub block_count: u32,
    pub payload_bytes: u64,
    pub file_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct IndexEntry {
    block_id: BlockId,
    payload_offset: u64,
    payload_len: u32,
    payload_crc32c: u32,
}

pub struct PayloadSegment {
    path: PathBuf,
    vfs: Arc<dyn Vfs>,
    metadata: PayloadSegmentMetadata,
    entries: Vec<IndexEntry>,
}

impl PayloadSegment {
    pub fn metadata(&self) -> PayloadSegmentMetadata {
        self.metadata
    }

    pub fn contains(&self, block_id: BlockId) -> bool {
        self.entries
            .binary_search_by_key(&block_id, |entry| entry.block_id)
            .is_ok()
    }

    pub fn first_block_id(&self) -> BlockId {
        self.entries[0].block_id
    }

    pub fn last_block_id(&self) -> BlockId {
        self.entries[self.entries.len() - 1].block_id
    }

    pub fn block_ids(&self) -> impl ExactSizeIterator<Item = BlockId> + '_ {
        self.entries.iter().map(|entry| entry.block_id)
    }

    pub fn get(&self, block_id: BlockId) -> io::Result<Option<Vec<u8>>> {
        let Ok(position) = self
            .entries
            .binary_search_by_key(&block_id, |entry| entry.block_id)
        else {
            return Ok(None);
        };
        let entry = self.entries[position];
        let mut payload = vec![0_u8; entry.payload_len as usize];
        let mut file = open_read(self.vfs.as_ref(), &self.path)?;
        file.read_exact_at(entry.payload_offset, &mut payload)?;
        if crc32c::crc32c(&payload) != entry.payload_crc32c
            || BlockId::for_payload(&payload) != block_id
        {
            return Err(invalid_data("payload segment block integrity mismatch"));
        }
        Ok(Some(payload))
    }
}

pub struct PayloadSegmentStore {
    root: PathBuf,
    vfs: Arc<dyn Vfs>,
}

impl PayloadSegmentStore {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("payload-segments");
        vfs.create_dir(&root)?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        Ok(Self { root, vfs })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("payload-segments");
        if vfs.metadata(&root)? != FileKind::Directory {
            return Err(invalid_data("payload segment root is not a real directory"));
        }
        Ok(Self { root, vfs })
    }

    pub fn create<F>(
        &self,
        segment_id: Uuid,
        mut block_ids: Vec<BlockId>,
        mut read_payload: F,
    ) -> io::Result<PayloadSegment>
    where
        F: FnMut(BlockId) -> io::Result<Vec<u8>>,
    {
        if block_ids.is_empty() || block_ids.len() > MAX_SEGMENT_BLOCKS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment block count is outside its bounded range",
            ));
        }
        block_ids.sort_unstable();
        if block_ids.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment contains duplicate block IDs",
            ));
        }

        let destination = self.path(segment_id);
        match self.vfs.metadata(&destination) {
            Ok(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "payload segment ID already exists",
                ));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let temporary = self.root.join(format!(".new-{}", Uuid::new_v4().simple()));
        let write_result = (|| {
            let mut file = self.vfs.open(
                &temporary,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )?;
            let header = encode_header(segment_id, block_ids.len() as u32, 0);
            file.write_all_at(0, &header)?;

            let mut entries = Vec::with_capacity(block_ids.len());
            let mut payload_bytes = 0_u64;
            for block_id in block_ids {
                let payload = read_payload(block_id)?;
                if payload.len() > MAX_BLOCK_BYTES || BlockId::for_payload(&payload) != block_id {
                    return Err(invalid_data("payload segment source block is invalid"));
                }
                let payload_len = payload.len() as u64;
                payload_bytes = payload_bytes.checked_add(payload_len).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "payload segment size overflow")
                })?;
                if payload_bytes > MAX_SEGMENT_PAYLOAD_BYTES {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "payload segment exceeds the 256 MiB victim bound",
                    ));
                }
                let payload_offset = HEADER_BYTES as u64 + payload_bytes - payload_len;
                file.write_all_at(payload_offset, &payload)?;
                entries.push(IndexEntry {
                    block_id,
                    payload_offset,
                    payload_len: payload.len() as u32,
                    payload_crc32c: crc32c::crc32c(&payload),
                });
            }

            let header = encode_header(segment_id, entries.len() as u32, payload_bytes);
            file.write_all_at(0, &header)?;
            let index = encode_index(&entries);
            let index_offset = HEADER_BYTES as u64 + payload_bytes;
            file.write_all_at(index_offset, &index)?;
            let trailer = encode_trailer(&header, &index, index_offset);
            file.write_all_at(index_offset + index.len() as u64, &trailer)?;
            sync_all_barrier(file.as_mut())?;
            self.vfs.rename(&temporary, &destination)?;
            sync_directory_barrier(self.vfs.as_ref(), &self.root)
        })();
        if write_result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        write_result?;
        self.open_segment(segment_id)
    }

    pub fn open_segment(&self, segment_id: Uuid) -> io::Result<PayloadSegment> {
        let path = self.path(segment_id);
        if self.vfs.metadata(&path)? != FileKind::File {
            return Err(invalid_data("payload segment is not a regular file"));
        }
        let mut file = open_read(self.vfs.as_ref(), &path)?;
        let file_bytes = file.len()?;
        if file_bytes < (HEADER_BYTES + INDEX_ENTRY_BYTES + TRAILER_BYTES) as u64
            || file_bytes > MAX_SEGMENT_FILE_BYTES
        {
            return Err(invalid_data(
                "payload segment file size is outside its bound",
            ));
        }
        let mut header = [0_u8; HEADER_BYTES];
        file.read_exact_at(0, &mut header)?;
        let (stored_id, block_count, payload_bytes) = decode_header(&header)?;
        if stored_id != segment_id || block_count == 0 || block_count as usize > MAX_SEGMENT_BLOCKS
        {
            return Err(invalid_data(
                "payload segment header identity or count mismatch",
            ));
        }

        let mut trailer = [0_u8; TRAILER_BYTES];
        file.read_exact_at(file_bytes - TRAILER_BYTES as u64, &mut trailer)?;
        let index_offset = u64::from_le_bytes(trailer[8..16].try_into().unwrap());
        let index_bytes = u64::from_le_bytes(trailer[16..24].try_into().unwrap());
        let expected_index_bytes = block_count as u64 * INDEX_ENTRY_BYTES as u64;
        let expected_index_offset = (HEADER_BYTES as u64)
            .checked_add(payload_bytes)
            .ok_or_else(|| invalid_data("payload segment layout overflow"))?;
        let expected_file_bytes = index_offset
            .checked_add(index_bytes)
            .and_then(|value| value.checked_add(TRAILER_BYTES as u64))
            .ok_or_else(|| invalid_data("payload segment layout overflow"))?;
        if &trailer[..8] != TRAILER_MAGIC
            || index_bytes != expected_index_bytes
            || index_bytes > MAX_SEGMENT_INDEX_BYTES as u64
            || payload_bytes > MAX_SEGMENT_PAYLOAD_BYTES
            || index_offset != expected_index_offset
            || expected_file_bytes != file_bytes
        {
            return Err(invalid_data("payload segment trailer layout mismatch"));
        }
        let mut index = vec![0_u8; index_bytes as usize];
        file.read_exact_at(index_offset, &mut index)?;
        let stored_crc = u32::from_le_bytes(trailer[24..28].try_into().unwrap());
        if stored_crc != crc32c::crc32c(&index)
            || trailer[28..] != trailer_digest(&header, &index, &trailer[..28])
        {
            return Err(invalid_data("payload segment index integrity mismatch"));
        }
        let entries = decode_index(&index, payload_bytes)?;
        Ok(PayloadSegment {
            path,
            vfs: Arc::clone(&self.vfs),
            metadata: PayloadSegmentMetadata {
                segment_id,
                block_count,
                payload_bytes,
                file_bytes,
            },
            entries,
        })
    }

    pub(crate) fn remove_segment(&self, segment_id: Uuid) -> io::Result<u64> {
        let path = self.path(segment_id);
        match self.vfs.metadata(&path) {
            Ok(FileKind::File) => {}
            Ok(FileKind::Directory) => {
                return Err(invalid_data("payload segment path is a directory"));
            }
            Err(error) => return Err(error),
        }
        let file_bytes = open_read(self.vfs.as_ref(), &path)?.len()?;
        if file_bytes > MAX_SEGMENT_FILE_BYTES {
            return Err(invalid_data("payload segment removal size exceeds bound"));
        }
        self.vfs.remove_file(&path)?;
        sync_directory_barrier(self.vfs.as_ref(), &self.root)?;
        Ok(file_bytes)
    }

    pub(crate) fn plan_orphan_file(
        &self,
        referenced_segment_ids: &BTreeSet<Uuid>,
    ) -> io::Result<PayloadSegmentOrphanPlan> {
        if referenced_segment_ids.len() >= MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED {
            return Err(invalid_data(
                "payload manifest exceeds orphan scan admission bound",
            ));
        }
        let bounded = self
            .vfs
            .read_directory_bounded(&self.root, MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED)?;
        let mut candidate = None;
        let mut more_candidates = bounded.has_more;
        for path in &bounded.entries {
            let file_name = path
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(|| invalid_data("payload segment filename is not UTF-8"))?;
            let segment_id = parse_complete_segment_name(file_name)?;
            let is_temporary = parse_temporary_segment_name(file_name)?;
            if segment_id.is_none() && !is_temporary {
                return Err(invalid_data(
                    "payload segment directory has an unknown entry",
                ));
            }
            if segment_id.is_some_and(|id| referenced_segment_ids.contains(&id)) {
                continue;
            }
            if self.vfs.metadata(path)? != FileKind::File {
                return Err(invalid_data("payload segment orphan is not a regular file"));
            }
            let file_bytes = open_read(self.vfs.as_ref(), path)?.len()?;
            if file_bytes > MAX_SEGMENT_FILE_BYTES {
                return Err(invalid_data(
                    "payload segment orphan exceeds the file bound",
                ));
            }
            let next = PayloadSegmentOrphanCandidate {
                file_name: file_name.to_owned(),
                segment_id,
                file_bytes,
            };
            if candidate.is_some() {
                more_candidates = true;
            } else {
                candidate = Some(next);
            }
        }
        Ok(PayloadSegmentOrphanPlan {
            scanned_files: bounded.entries.len() as u32,
            candidate,
            more_candidates,
        })
    }

    pub(crate) fn remove_orphan_file(
        &self,
        candidate: &PayloadSegmentOrphanCandidate,
    ) -> io::Result<u64> {
        let path = self.root.join(&candidate.file_name);
        if path.parent() != Some(self.root.as_path())
            || (parse_complete_segment_name(&candidate.file_name)? != candidate.segment_id)
            || (candidate.segment_id.is_none()
                && !parse_temporary_segment_name(&candidate.file_name)?)
        {
            return Err(invalid_data("payload segment orphan identity changed"));
        }
        if self.vfs.metadata(&path)? != FileKind::File {
            return Err(invalid_data("payload segment orphan is not a regular file"));
        }
        let file_bytes = open_read(self.vfs.as_ref(), &path)?.len()?;
        if file_bytes != candidate.file_bytes || file_bytes > MAX_SEGMENT_FILE_BYTES {
            return Err(invalid_data("payload segment orphan size changed"));
        }
        self.vfs.remove_file(&path)?;
        sync_directory_barrier(self.vfs.as_ref(), &self.root)?;
        Ok(file_bytes)
    }

    fn path(&self, segment_id: Uuid) -> PathBuf {
        self.root.join(format!("{}.pseg", segment_id.simple()))
    }
}

fn parse_complete_segment_name(file_name: &str) -> io::Result<Option<Uuid>> {
    let Some(identifier) = file_name.strip_suffix(".pseg") else {
        return Ok(None);
    };
    parse_simple_uuid(identifier)
        .map(Some)
        .ok_or_else(|| invalid_data("payload segment filename is malformed"))
}

fn parse_temporary_segment_name(file_name: &str) -> io::Result<bool> {
    let Some(identifier) = file_name.strip_prefix(".new-") else {
        return Ok(false);
    };
    if parse_simple_uuid(identifier).is_none() {
        return Err(invalid_data(
            "payload segment temporary filename is malformed",
        ));
    }
    Ok(true)
}

fn parse_simple_uuid(identifier: &str) -> Option<Uuid> {
    if identifier.len() != 32
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return None;
    }
    let parsed = Uuid::parse_str(identifier).ok()?;
    (parsed.simple().to_string() == identifier).then_some(parsed)
}

fn open_read(vfs: &dyn Vfs, path: &Path) -> io::Result<Box<dyn VfsFile>> {
    vfs.open(
        path,
        OpenRequest {
            read: true,
            ..OpenRequest::default()
        },
    )
}

fn encode_header(segment_id: Uuid, block_count: u32, payload_bytes: u64) -> [u8; HEADER_BYTES] {
    let mut header = [0_u8; HEADER_BYTES];
    header[..8].copy_from_slice(HEADER_MAGIC);
    header[8..12].copy_from_slice(&FORMAT_VERSION.to_le_bytes());
    header[12..16].copy_from_slice(&(HEADER_BYTES as u32).to_le_bytes());
    header[16..32].copy_from_slice(segment_id.as_bytes());
    header[32..36].copy_from_slice(&block_count.to_le_bytes());
    header[36..40].copy_from_slice(&(INDEX_ENTRY_BYTES as u32).to_le_bytes());
    header[40..48].copy_from_slice(&payload_bytes.to_le_bytes());
    header
}

fn decode_header(header: &[u8; HEADER_BYTES]) -> io::Result<(Uuid, u32, u64)> {
    if &header[..8] != HEADER_MAGIC
        || u32::from_le_bytes(header[8..12].try_into().unwrap()) != FORMAT_VERSION
        || u32::from_le_bytes(header[12..16].try_into().unwrap()) != HEADER_BYTES as u32
        || u32::from_le_bytes(header[36..40].try_into().unwrap()) != INDEX_ENTRY_BYTES as u32
    {
        return Err(invalid_data("payload segment header format mismatch"));
    }
    Ok((
        Uuid::from_bytes(header[16..32].try_into().unwrap()),
        u32::from_le_bytes(header[32..36].try_into().unwrap()),
        u64::from_le_bytes(header[40..48].try_into().unwrap()),
    ))
}

fn encode_index(entries: &[IndexEntry]) -> Vec<u8> {
    let mut index = Vec::with_capacity(entries.len() * INDEX_ENTRY_BYTES);
    for entry in entries {
        index.extend_from_slice(&entry.block_id.0);
        index.extend_from_slice(&entry.payload_offset.to_le_bytes());
        index.extend_from_slice(&entry.payload_len.to_le_bytes());
        index.extend_from_slice(&entry.payload_crc32c.to_le_bytes());
    }
    index
}

fn decode_index(index: &[u8], payload_bytes: u64) -> io::Result<Vec<IndexEntry>> {
    let mut entries = Vec::with_capacity(index.len() / INDEX_ENTRY_BYTES);
    let mut expected_offset = HEADER_BYTES as u64;
    for encoded in index.chunks_exact(INDEX_ENTRY_BYTES) {
        let entry = IndexEntry {
            block_id: BlockId(encoded[..32].try_into().unwrap()),
            payload_offset: u64::from_le_bytes(encoded[32..40].try_into().unwrap()),
            payload_len: u32::from_le_bytes(encoded[40..44].try_into().unwrap()),
            payload_crc32c: u32::from_le_bytes(encoded[44..48].try_into().unwrap()),
        };
        if entry.payload_len as usize > MAX_BLOCK_BYTES || entry.payload_offset != expected_offset {
            return Err(invalid_data("payload segment index has an invalid extent"));
        }
        expected_offset = expected_offset
            .checked_add(entry.payload_len as u64)
            .ok_or_else(|| invalid_data("payload segment extent overflow"))?;
        entries.push(entry);
    }
    if expected_offset != HEADER_BYTES as u64 + payload_bytes
        || entries
            .windows(2)
            .any(|pair| pair[0].block_id >= pair[1].block_id)
    {
        return Err(invalid_data(
            "payload segment index order or coverage mismatch",
        ));
    }
    Ok(entries)
}

fn encode_trailer(header: &[u8], index: &[u8], index_offset: u64) -> [u8; TRAILER_BYTES] {
    let mut trailer = [0_u8; TRAILER_BYTES];
    trailer[..8].copy_from_slice(TRAILER_MAGIC);
    trailer[8..16].copy_from_slice(&index_offset.to_le_bytes());
    trailer[16..24].copy_from_slice(&(index.len() as u64).to_le_bytes());
    trailer[24..28].copy_from_slice(&crc32c::crc32c(index).to_le_bytes());
    let digest = trailer_digest(header, index, &trailer[..28]);
    trailer[28..].copy_from_slice(&digest);
    trailer
}

fn trailer_digest(header: &[u8], index: &[u8], trailer_prefix: &[u8]) -> [u8; 32] {
    let mut hasher = blake3::Hasher::new();
    hasher.update(b"tofu-db:payload-segment:v1\0");
    hasher.update(header);
    hasher.update(index);
    hasher.update(trailer_prefix);
    *hasher.finalize().as_bytes()
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::collections::BTreeMap;
    use std::fs::OpenOptions;
    use std::io::{Seek, SeekFrom, Write};

    fn simulated_store(vfs: &DeterministicVfs) -> PayloadSegmentStore {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        PayloadSegmentStore::initialize_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap()
    }

    fn fixture() -> (Uuid, Vec<BlockId>, BTreeMap<BlockId, Vec<u8>>) {
        let segment_id = Uuid::from_bytes([7; 16]);
        let payloads = [b"alpha".as_slice(), b"", b"payload-three"];
        let by_id: BTreeMap<_, _> = payloads
            .into_iter()
            .map(|payload| (BlockId::for_payload(payload), payload.to_vec()))
            .collect();
        (segment_id, by_id.keys().copied().collect(), by_id)
    }

    #[test]
    fn segment_round_trip_uses_bounded_random_reads() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        let segment = store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        assert_eq!(segment.metadata().block_count, 3);
        for id in ids {
            assert_eq!(segment.get(id).unwrap().unwrap(), payloads[&id]);
        }
        assert_eq!(segment.get(BlockId([9; 32])).unwrap(), None);
    }

    #[test]
    fn malformed_source_and_duplicate_ids_fail_closed() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let id = BlockId::for_payload(b"expected");
        assert_eq!(
            store
                .create(Uuid::from_bytes([1; 16]), vec![id, id], |_| Ok(Vec::new()))
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            store
                .create(Uuid::from_bytes([2; 16]), vec![id], |_| Ok(
                    b"wrong".to_vec()
                ))
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn orphan_plan_accepts_only_bounded_engine_owned_filenames() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let temporary_name = format!(".new-{}", Uuid::from_bytes([3; 16]).simple());
        let temporary_path = store.root.join(&temporary_name);
        let mut temporary = vfs
            .open(
                &temporary_path,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        temporary.write_all_at(0, b"partial-segment").unwrap();
        sync_all_barrier(temporary.as_mut()).unwrap();
        sync_directory_barrier(&vfs, &store.root).unwrap();

        let plan = store.plan_orphan_file(&BTreeSet::new()).unwrap();
        assert_eq!(plan.scanned_files, 1);
        let candidate = plan.candidate.unwrap();
        assert_eq!(candidate.file_name, temporary_name);
        assert_eq!(candidate.segment_id, None);
        assert_eq!(store.remove_orphan_file(&candidate).unwrap(), 15);
        assert!(store
            .plan_orphan_file(&BTreeSet::new())
            .unwrap()
            .candidate
            .is_none());

        let unknown_path = store.root.join("foreign-file");
        vfs.open(
            &unknown_path,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )
        .unwrap();
        assert_eq!(
            store.plan_orphan_file(&BTreeSet::new()).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn payload_and_index_corruption_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let store = PayloadSegmentStore::initialize(directory.path()).unwrap();
        let (first_id, ids, payloads) = fixture();
        let first = store
            .create(first_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        let target = BlockId::for_payload(b"alpha");
        let target_offset = first.entries[first
            .entries
            .binary_search_by_key(&target, |entry| entry.block_id)
            .unwrap()]
        .payload_offset;
        let mut file = OpenOptions::new()
            .write(true)
            .open(store.path(first_id))
            .unwrap();
        file.seek(SeekFrom::Start(target_offset)).unwrap();
        file.write_all(b"X").unwrap();
        file.sync_all().unwrap();
        assert_eq!(
            first.get(target).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );

        let second_id = Uuid::from_bytes([8; 16]);
        let second = store
            .create(second_id, ids, |id| Ok(payloads[&id].clone()))
            .unwrap();
        let index_offset = HEADER_BYTES as u64 + second.metadata().payload_bytes;
        let mut file = OpenOptions::new()
            .write(true)
            .open(store.path(second_id))
            .unwrap();
        file.seek(SeekFrom::Start(index_offset)).unwrap();
        file.write_all(b"X").unwrap();
        file.sync_all().unwrap();
        assert_eq!(
            store.open_segment(second_id).err().unwrap().kind(),
            io::ErrorKind::InvalidData
        );

        let third_id = Uuid::from_bytes([9; 16]);
        let third = store
            .create(third_id, payloads.keys().copied().collect(), |id| {
                Ok(payloads[&id].clone())
            })
            .unwrap();
        let trailer_offset = third.metadata().file_bytes - TRAILER_BYTES as u64;
        let mut file = OpenOptions::new()
            .write(true)
            .open(store.path(third_id))
            .unwrap();
        file.seek(SeekFrom::Start(trailer_offset + 8)).unwrap();
        file.write_all(&u64::MAX.to_le_bytes()).unwrap();
        file.sync_all().unwrap();
        assert_eq!(
            store.open_segment(third_id).err().unwrap().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn every_publish_fault_recovers_an_absent_or_complete_segment() -> io::Result<()> {
        let baseline_vfs = DeterministicVfs::new(None);
        let baseline = simulated_store(&baseline_vfs);
        let (segment_id, ids, payloads) = fixture();
        baseline_vfs.arm_fault(None)?;
        baseline.create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))?;
        let trace = baseline_vfs.trace()?;
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncAll));
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::SyncDirectory));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(3))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }

        for (operation_number, action) in cases {
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))?;
            let _ = store.create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()));
            vfs.crash()?;
            vfs.arm_fault(None)?;
            let recovered =
                PayloadSegmentStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            match recovered.open_segment(segment_id) {
                Ok(segment) => {
                    for id in &ids {
                        assert_eq!(segment.get(*id)?.unwrap(), payloads[id]);
                    }
                }
                Err(error) => assert_eq!(error.kind(), io::ErrorKind::NotFound),
            }
        }
        Ok(())
    }
}
