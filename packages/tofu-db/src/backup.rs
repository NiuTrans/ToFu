//! Explicit incremental backup generations and verified offline restore.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::backup_gc::{MarkMetrics, SpillMarkSet};
use crate::block::{BlockId, BlockRemovalMetrics, BlockStore, BlockWriteMetrics, MAX_BLOCK_BYTES};
use crate::control::{ControlFile, ControlState};
use crate::engine::Engine;
use crate::entity::visit_entity_page_graph_with_values;
use crate::history::{decode_segment, HistoryManifest};
use crate::vfs::{
    sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile,
};
use crate::wal::ActiveLog;
use crate::FORMAT_VERSION;

const MANIFEST_MAGIC: &[u8; 8] = b"TDBBKP01";
const LEGACY_MANIFEST_BYTES: usize = 8 + 4 + 16 + 8 + 8 + 32 + 8 + 32 + 1 + 32 + 4 + 32;
const MANIFEST_BYTES: usize = LEGACY_MANIFEST_BYTES + 1 + 32;
const MAX_BACKUP_GENERATIONS: usize = 4_096;
const RESTORE_MARKER: &str = "RESTORE";
const RESTORE_TEMP_PREFIX: &str = ".restore-new-";
const MAX_RESTORE_ROOT_ENTRIES: usize = 8;
const MAX_PREFLIGHT_DIRECTORIES: u64 = 4_096;
const MAX_PREFLIGHT_FILES: u64 = 20_000_000;
const RESTORE_METADATA_RESERVE_BYTES: u64 = 64 * 1024 * 1024;
const BACKUP_LOCK: &str = "BACKUP_LOCK";
const MAX_GC_PHYSICAL_BLOCKS: usize = 50_000_000;
const MAX_BACKUP_PINS: usize = 1_024;
const PRUNE_MARKER: &str = "PRUNE";
const PRUNE_MAGIC: &[u8; 8] = b"TDBPRN01";
const PRUNE_HEADER_BYTES: usize = 8 + 4 + 4;
const PRUNE_FOOTER_BYTES: usize = 4 + 32;
const MAX_PRUNE_BYTES: usize =
    PRUNE_HEADER_BYTES + MAX_BACKUP_GENERATIONS * MANIFEST_BYTES + PRUNE_FOOTER_BYTES;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackupManifest {
    pub authority_uuid: Uuid,
    pub source_control_generation: u64,
    pub durable_sequence: u64,
    pub root_hash: [u8; 32],
    pub checkpoint_sequence: u64,
    pub checkpoint_hash: [u8; 32],
    pub history_manifest_block_id: Option<BlockId>,
    pub authority_state_root: Option<Option<BlockId>>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BackupMetrics {
    pub durable_sequence: u64,
    pub copied_blocks: u64,
    pub copied_block_bytes: u64,
    pub manifest_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RestoreCapacity {
    pub required_bytes: u64,
    pub available_bytes: u64,
    pub backup_bytes: u64,
    pub retained_target_bytes: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BackupPruneMetrics {
    pub retained_generations: u32,
    pub removed_generations: u32,
    pub removed_blocks: u64,
    pub removed_block_bytes: u64,
    pub mark_references: u64,
    pub spill_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PrunePlan {
    retained: Vec<BackupManifest>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn acquire_backup_lease(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<Box<dyn VfsFile>> {
    let mut lease = vfs.open(
        &backup_dir.join(BACKUP_LOCK),
        OpenRequest {
            read: true,
            write: true,
            create: true,
            ..OpenRequest::default()
        },
    )?;
    lease.try_lock_exclusive().map_err(|error| {
        if error.kind() == io::ErrorKind::WouldBlock {
            io::Error::new(
                io::ErrorKind::WouldBlock,
                "another backup, restore, or prune operation owns this backup",
            )
        } else {
            error
        }
    })?;
    Ok(lease)
}

fn resolved_candidate(path: &Path) -> io::Result<PathBuf> {
    let mut existing = path;
    let mut suffix = Vec::new();
    while !existing.exists() {
        suffix.push(
            existing
                .file_name()
                .ok_or_else(|| invalid_input("path has no existing ancestor"))?
                .to_owned(),
        );
        existing = existing
            .parent()
            .ok_or_else(|| invalid_input("path has no existing ancestor"))?;
    }
    let mut resolved = fs::canonicalize(existing)?;
    for component in suffix.into_iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

fn require_disjoint_paths(left: &Path, right: &Path, relationship: &str) -> io::Result<()> {
    let canonical_left = resolved_candidate(left)?;
    let canonical_right = resolved_candidate(right)?;
    if canonical_left == canonical_right
        || canonical_left.starts_with(&canonical_right)
        || canonical_right.starts_with(&canonical_left)
    {
        return Err(invalid_input(relationship));
    }
    Ok(())
}

fn bounded_directory_bytes(root: &Path) -> io::Result<u64> {
    let mut pending = vec![root.to_owned()];
    let mut directory_count = 0_u64;
    let mut file_count = 0_u64;
    let mut total = 0_u64;
    while let Some(directory) = pending.pop() {
        directory_count = directory_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("capacity directory count overflow"))?;
        if directory_count > MAX_PREFLIGHT_DIRECTORIES {
            return Err(invalid_input("capacity preflight directory bound exceeded"));
        }
        for entry in fs::read_dir(&directory)? {
            let entry = entry?;
            let metadata = fs::symlink_metadata(entry.path())?;
            if metadata.file_type().is_symlink() {
                return Err(invalid_data("capacity preflight refuses symbolic links"));
            }
            if metadata.is_dir() {
                if directory_count + pending.len() as u64 >= MAX_PREFLIGHT_DIRECTORIES {
                    return Err(invalid_input("capacity preflight directory bound exceeded"));
                }
                pending.push(entry.path());
            } else if metadata.is_file() {
                file_count = file_count
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("capacity file count overflow"))?;
                if file_count > MAX_PREFLIGHT_FILES {
                    return Err(invalid_input("capacity preflight file bound exceeded"));
                }
                total = total
                    .checked_add(metadata.len())
                    .ok_or_else(|| invalid_data("capacity byte count overflow"))?;
            } else {
                return Err(invalid_data(
                    "capacity preflight found an unsupported filesystem entry",
                ));
            }
        }
    }
    Ok(total)
}

pub fn preflight_restore_capacity(
    backup_dir: &Path,
    target_dir: &Path,
) -> io::Result<RestoreCapacity> {
    let backup_bytes = bounded_directory_bytes(backup_dir)?;
    let retained_target_bytes = bounded_directory_bytes(target_dir)?;
    assess_restore_capacity(
        backup_bytes,
        retained_target_bytes,
        fs2::available_space(target_dir)?,
    )
}

fn assess_restore_capacity(
    backup_bytes: u64,
    retained_target_bytes: u64,
    available_bytes: u64,
) -> io::Result<RestoreCapacity> {
    let remaining_copy_bytes = backup_bytes.saturating_sub(retained_target_bytes);
    let temporary_bytes = (MAX_BLOCK_BYTES as u64)
        .checked_add(RESTORE_METADATA_RESERVE_BYTES)
        .ok_or_else(|| invalid_data("restore capacity reserve overflow"))?;
    let required_bytes = remaining_copy_bytes
        .checked_add(temporary_bytes)
        .ok_or_else(|| invalid_data("restore required byte count overflow"))?;
    if available_bytes < required_bytes {
        return Err(io::Error::new(
            io::ErrorKind::StorageFull,
            format!(
                "restore requires {required_bytes} available bytes but target has {available_bytes}"
            ),
        ));
    }
    Ok(RestoreCapacity {
        required_bytes,
        available_bytes,
        backup_bytes,
        retained_target_bytes,
    })
}

impl BackupManifest {
    fn from_control(state: &ControlState) -> io::Result<Self> {
        if state.durable_sequence != state.checkpoint_sequence
            || state.root_hash != state.checkpoint_hash
        {
            return Err(invalid_data(
                "backup source is not at a checkpoint boundary",
            ));
        }
        Ok(Self {
            authority_uuid: state.authority_uuid,
            source_control_generation: state.generation,
            durable_sequence: state.durable_sequence,
            root_hash: state.root_hash,
            checkpoint_sequence: state.checkpoint_sequence,
            checkpoint_hash: state.checkpoint_hash,
            history_manifest_block_id: state.history_manifest_block_id,
            authority_state_root: state.authority_state_root,
        })
    }

    fn encode(&self) -> io::Result<Vec<u8>> {
        if self.source_control_generation == 0
            || self.durable_sequence != self.checkpoint_sequence
            || self.root_hash != self.checkpoint_hash
            || (self.checkpoint_sequence == 0) != self.history_manifest_block_id.is_none()
            || (self.durable_sequence == 0 && matches!(self.authority_state_root, Some(Some(_))))
        {
            return Err(invalid_input("invalid backup manifest state"));
        }
        let mut encoded = Vec::with_capacity(MANIFEST_BYTES);
        encoded.extend_from_slice(MANIFEST_MAGIC);
        encoded.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        encoded.extend_from_slice(self.authority_uuid.as_bytes());
        encoded.extend_from_slice(&self.source_control_generation.to_le_bytes());
        encoded.extend_from_slice(&self.durable_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.root_hash);
        encoded.extend_from_slice(&self.checkpoint_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.checkpoint_hash);
        encoded.push(u8::from(self.history_manifest_block_id.is_some()));
        encoded.extend_from_slice(&self.history_manifest_block_id.unwrap_or(BlockId([0; 32])).0);
        match self.authority_state_root {
            None => {
                encoded.push(0);
                encoded.extend_from_slice(&[0; 32]);
            }
            Some(None) => {
                encoded.push(1);
                encoded.extend_from_slice(&[0; 32]);
            }
            Some(Some(root)) => {
                encoded.push(2);
                encoded.extend_from_slice(&root.0);
            }
        }
        let crc = crc32c::crc32c(&encoded);
        encoded.extend_from_slice(&crc.to_le_bytes());
        let digest = blake3::hash(&encoded);
        encoded.extend_from_slice(digest.as_bytes());
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if !matches!(encoded.len(), LEGACY_MANIFEST_BYTES | MANIFEST_BYTES)
            || &encoded[..8] != MANIFEST_MAGIC
        {
            return Err(invalid_data("backup manifest magic or length mismatch"));
        }
        let digest_offset = encoded.len() - 32;
        if blake3::hash(&encoded[..digest_offset]).as_bytes() != &encoded[digest_offset..] {
            return Err(invalid_data("backup manifest BLAKE3 mismatch"));
        }
        let crc_offset = digest_offset - 4;
        let stored_crc = u32::from_le_bytes(encoded[crc_offset..digest_offset].try_into().unwrap());
        if crc32c::crc32c(&encoded[..crc_offset]) != stored_crc {
            return Err(invalid_data("backup manifest CRC32C mismatch"));
        }
        if u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != FORMAT_VERSION {
            return Err(invalid_data("unsupported backup manifest version"));
        }
        let presence = encoded[116];
        let stored_block = BlockId(encoded[117..149].try_into().unwrap());
        let history_manifest_block_id = match presence {
            0 if stored_block == BlockId([0; 32]) => None,
            1 => Some(stored_block),
            _ => return Err(invalid_data("invalid backup history manifest presence")),
        };
        let authority_state_root = if encoded.len() == LEGACY_MANIFEST_BYTES {
            None
        } else {
            let stored_root = BlockId(encoded[150..182].try_into().unwrap());
            match encoded[149] {
                0 if stored_root == BlockId([0; 32]) => None,
                1 if stored_root == BlockId([0; 32]) => Some(None),
                2 => Some(Some(stored_root)),
                _ => return Err(invalid_data("invalid backup authority root discriminant")),
            }
        };
        let manifest = Self {
            authority_uuid: Uuid::from_bytes(encoded[12..28].try_into().unwrap()),
            source_control_generation: u64::from_le_bytes(encoded[28..36].try_into().unwrap()),
            durable_sequence: u64::from_le_bytes(encoded[36..44].try_into().unwrap()),
            root_hash: encoded[44..76].try_into().unwrap(),
            checkpoint_sequence: u64::from_le_bytes(encoded[76..84].try_into().unwrap()),
            checkpoint_hash: encoded[84..116].try_into().unwrap(),
            history_manifest_block_id,
            authority_state_root,
        };
        manifest
            .encode()
            .map_err(|_| invalid_data("invalid backup manifest state"))?;
        Ok(manifest)
    }

    fn file_name(&self) -> String {
        format!(
            "snapshot-{:016x}-{}.manifest",
            self.durable_sequence,
            BlockId(self.root_hash).to_hex()
        )
    }
}

impl PrunePlan {
    fn encode(&self) -> io::Result<Vec<u8>> {
        if self.retained.is_empty() || self.retained.len() > MAX_BACKUP_GENERATIONS {
            return Err(invalid_input("invalid prune retention set"));
        }
        let mut encoded = Vec::with_capacity(
            PRUNE_HEADER_BYTES + self.retained.len() * MANIFEST_BYTES + PRUNE_FOOTER_BYTES,
        );
        encoded.extend_from_slice(PRUNE_MAGIC);
        encoded.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        encoded.extend_from_slice(&(self.retained.len() as u32).to_le_bytes());
        for manifest in &self.retained {
            encoded.extend_from_slice(&manifest.encode()?);
        }
        let crc = crc32c::crc32c(&encoded);
        encoded.extend_from_slice(&crc.to_le_bytes());
        let digest = blake3::hash(&encoded);
        encoded.extend_from_slice(digest.as_bytes());
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < PRUNE_HEADER_BYTES + PRUNE_FOOTER_BYTES
            || encoded.len() > MAX_PRUNE_BYTES
            || &encoded[..8] != PRUNE_MAGIC
        {
            return Err(invalid_data("prune plan magic or length mismatch"));
        }
        if u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != FORMAT_VERSION {
            return Err(invalid_data("unsupported prune plan version"));
        }
        let count = u32::from_le_bytes(encoded[12..16].try_into().unwrap()) as usize;
        let expected_current = count
            .checked_mul(MANIFEST_BYTES)
            .and_then(|bytes| PRUNE_HEADER_BYTES.checked_add(bytes))
            .and_then(|bytes| bytes.checked_add(PRUNE_FOOTER_BYTES))
            .ok_or_else(|| invalid_data("prune plan length overflow"))?;
        let expected_legacy = count
            .checked_mul(LEGACY_MANIFEST_BYTES)
            .and_then(|bytes| PRUNE_HEADER_BYTES.checked_add(bytes))
            .and_then(|bytes| bytes.checked_add(PRUNE_FOOTER_BYTES))
            .ok_or_else(|| invalid_data("legacy prune plan length overflow"))?;
        let manifest_bytes = if encoded.len() == expected_current {
            MANIFEST_BYTES
        } else if encoded.len() == expected_legacy {
            LEGACY_MANIFEST_BYTES
        } else {
            0
        };
        if count == 0 || count > MAX_BACKUP_GENERATIONS || manifest_bytes == 0 {
            return Err(invalid_data("invalid prune plan bounds"));
        }
        let digest_offset = encoded.len() - 32;
        if blake3::hash(&encoded[..digest_offset]).as_bytes() != &encoded[digest_offset..] {
            return Err(invalid_data("prune plan BLAKE3 mismatch"));
        }
        let crc_offset = digest_offset - 4;
        let stored_crc = u32::from_le_bytes(encoded[crc_offset..digest_offset].try_into().unwrap());
        if crc32c::crc32c(&encoded[..crc_offset]) != stored_crc {
            return Err(invalid_data("prune plan CRC32C mismatch"));
        }
        let mut retained = Vec::with_capacity(count);
        let mut offset = PRUNE_HEADER_BYTES;
        for _ in 0..count {
            retained.push(BackupManifest::decode(
                &encoded[offset..offset + manifest_bytes],
            )?);
            offset += manifest_bytes;
        }
        let plan = Self { retained };
        plan.encode()
            .map_err(|_| invalid_data("invalid prune plan retention set"))?;
        Ok(plan)
    }
}

fn initialize_or_open_backup(
    backup_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<(BlockStore, PathBuf)> {
    if vfs.metadata(backup_dir)? != FileKind::Directory {
        return Err(invalid_input("backup path is not a directory"));
    }
    let blocks_path = backup_dir.join("blocks");
    let snapshots_path = backup_dir.join("snapshots");
    let pins_path = backup_dir.join("pins");
    let gc_runs_path = backup_dir.join("gc-runs");
    match vfs.metadata(&blocks_path) {
        Ok(FileKind::Directory) => {
            if vfs.metadata(&snapshots_path)? != FileKind::Directory {
                return Err(invalid_data("backup snapshots path is not a directory"));
            }
            if vfs.metadata(&pins_path)? != FileKind::Directory {
                return Err(invalid_data("backup pins path is not a directory"));
            }
            if vfs.metadata(&gc_runs_path)? != FileKind::Directory {
                return Err(invalid_data("backup GC run path is not a directory"));
            }
            Ok((BlockStore::open_with_vfs(backup_dir, vfs)?, snapshots_path))
        }
        Ok(FileKind::File) => Err(invalid_data("backup blocks path is not a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if !vfs.read_directory(backup_dir)?.is_empty() {
                return Err(invalid_input(
                    "new backup directory must be empty or a valid tofu-db backup",
                ));
            }
            let store = BlockStore::initialize_with_vfs(backup_dir, Arc::clone(&vfs))?;
            vfs.create_dir(&snapshots_path)?;
            vfs.create_dir(&pins_path)?;
            vfs.create_dir(&gc_runs_path)?;
            sync_directory_barrier(vfs.as_ref(), backup_dir)?;
            Ok((store, snapshots_path))
        }
        Err(error) => Err(error),
    }
}

fn open_backup(backup_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<(BlockStore, PathBuf)> {
    if vfs.metadata(backup_dir)? != FileKind::Directory {
        return Err(invalid_input("backup path is not a directory"));
    }
    let snapshots_path = backup_dir.join("snapshots");
    if vfs.metadata(&backup_dir.join("blocks"))? != FileKind::Directory
        || vfs.metadata(&snapshots_path)? != FileKind::Directory
        || vfs.metadata(&backup_dir.join("pins"))? != FileKind::Directory
        || vfs.metadata(&backup_dir.join("gc-runs"))? != FileKind::Directory
    {
        return Err(invalid_data("backup layout is incomplete"));
    }
    Ok((BlockStore::open_with_vfs(backup_dir, vfs)?, snapshots_path))
}

fn read_manifest(path: &Path, vfs: &dyn Vfs) -> io::Result<BackupManifest> {
    if vfs.metadata(path)? != FileKind::File {
        return Err(invalid_data("backup manifest is not a regular file"));
    }
    let mut file = vfs.open(
        path,
        OpenRequest {
            read: true,
            ..OpenRequest::default()
        },
    )?;
    BackupManifest::decode(&file.read_all(MANIFEST_BYTES)?)
}

fn read_optional_prune_plan(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<Option<PrunePlan>> {
    let path = backup_dir.join(PRUNE_MARKER);
    match vfs.metadata(&path) {
        Ok(FileKind::File) => {
            let mut file = vfs.open(
                &path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            PrunePlan::decode(&file.read_all(MAX_PRUNE_BYTES)?).map(Some)
        }
        Ok(FileKind::Directory) => Err(invalid_data("prune marker is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn ensure_no_prune(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    if read_optional_prune_plan(backup_dir, vfs)?.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "backup prune must be resumed before backup or restore",
        ));
    }
    Ok(())
}

fn publish_prune_plan(backup_dir: &Path, plan: &PrunePlan, vfs: &dyn Vfs) -> io::Result<()> {
    let destination = backup_dir.join(PRUNE_MARKER);
    if let Some(existing) = read_optional_prune_plan(backup_dir, vfs)? {
        if existing != *plan {
            return Err(invalid_data(
                "existing prune plan differs from requested plan",
            ));
        }
        return Ok(());
    }
    let temporary = backup_dir.join(format!(".prune-new-{}", Uuid::new_v4()));
    let encoded = plan.encode()?;
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, &destination)?;
        sync_directory_barrier(vfs, backup_dir)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result
}

fn existing_manifests(snapshots_path: &Path, vfs: &dyn Vfs) -> io::Result<Vec<BackupManifest>> {
    let entries = vfs.read_directory(snapshots_path)?;
    if entries.len() > MAX_BACKUP_GENERATIONS * 2 {
        return Err(invalid_data("backup directory entry bound exceeded"));
    }
    let mut manifests = Vec::new();
    for path in entries {
        if path.extension().and_then(|value| value.to_str()) != Some("manifest") {
            continue;
        }
        if manifests.len() == MAX_BACKUP_GENERATIONS {
            return Err(invalid_data("backup generation bound exceeded"));
        }
        manifests.push(read_manifest(&path, vfs)?);
    }
    Ok(manifests)
}

fn copy_block(source: &BlockStore, destination: &BlockStore, id: BlockId) -> io::Result<Vec<u8>> {
    let payload = source.get(id)?;
    if destination.put(&payload)? != id {
        return Err(invalid_data("backup block identity changed during copy"));
    }
    Ok(payload)
}

fn copy_entity_reachable_blocks(
    source: &BlockStore,
    destination: &BlockStore,
    root: Option<BlockId>,
) -> io::Result<()> {
    visit_entity_page_graph_with_values(
        root,
        |block_id| source.get(block_id),
        |block_id, payload| {
            if destination.put(payload)? != block_id {
                return Err(invalid_data(
                    "backup entity-page identity changed during copy",
                ));
            }
            Ok(())
        },
        |key, value| {
            visit_semantic_value_blocks(source, key, value, |block_id, payload| {
                if destination.put(payload)? != block_id {
                    return Err(invalid_data(
                        "backup semantic-block identity changed during copy",
                    ));
                }
                Ok(())
            })?;
            Ok(())
        },
    )?;
    Ok(())
}

pub(crate) fn visit_semantic_value_blocks<VisitBlock>(
    source: &BlockStore,
    key: &crate::entity::EntityKey,
    value: &[u8],
    mut visit_block: VisitBlock,
) -> io::Result<()>
where
    VisitBlock: FnMut(BlockId, &[u8]) -> io::Result<()>,
{
    if let Some(block_id) = crate::stream::stored_segment_block_reference(key, value)? {
        let payload = source.get(block_id)?;
        visit_block(block_id, &payload)?;
    }
    let reference = crate::versioned_document::stored_blob_reference(value)?
        .or(crate::receipt::stored_blob_reference(value)?)
        .or(crate::logical_outbox::stored_blob_reference(value)?)
        .or(crate::artifact::stored_blob_reference(key, value)?);
    if let Some(reference) = reference {
        crate::blob::visit_blob_graph(
            key.tenant_id(),
            key.owner_user_id(),
            reference,
            |block_id| source.get(block_id),
            |block_id, payload| visit_block(block_id, payload),
        )?;
    }
    Ok(())
}

fn copy_reachable_blocks(
    source: &BlockStore,
    destination: &BlockStore,
    manifest: &BackupManifest,
) -> io::Result<()> {
    copy_entity_reachable_blocks(source, destination, manifest.authority_state_root.flatten())?;
    let Some(history_id) = manifest.history_manifest_block_id else {
        return Ok(());
    };
    let history_bytes = copy_block(source, destination, history_id)?;
    let history = HistoryManifest::decode(&history_bytes)?;
    if history.checkpoint_sequence != manifest.checkpoint_sequence
        || history.checkpoint_hash != manifest.checkpoint_hash
    {
        return Err(invalid_data(
            "backup history root does not match snapshot manifest",
        ));
    }
    for reference in &history.segments {
        let segment_bytes = copy_block(source, destination, reference.block_id)?;
        for transaction in decode_segment(&segment_bytes, reference)? {
            for block_id in transaction.envelope.block_ids {
                copy_block(source, destination, block_id)?;
            }
        }
    }
    Ok(())
}

fn publish_manifest(
    snapshots_path: &Path,
    manifest: &BackupManifest,
    vfs: &dyn Vfs,
) -> io::Result<u64> {
    let destination = snapshots_path.join(manifest.file_name());
    publish_manifest_file(snapshots_path, &destination, manifest, ".new-", vfs)
}

fn publish_manifest_file(
    directory: &Path,
    destination: &Path,
    manifest: &BackupManifest,
    temporary_prefix: &str,
    vfs: &dyn Vfs,
) -> io::Result<u64> {
    match vfs.metadata(destination) {
        Ok(FileKind::File) => {
            if read_manifest(destination, vfs)? != *manifest {
                return Err(invalid_data("backup manifest filename collision"));
            }
            return Ok(0);
        }
        Ok(FileKind::Directory) => return Err(invalid_data("backup manifest is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let encoded = manifest.encode()?;
    let temporary = directory.join(format!("{temporary_prefix}{}", Uuid::new_v4()));
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, destination)?;
        sync_directory_barrier(vfs, directory)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result?;
    Ok(encoded.len() as u64)
}

fn restore_pin_path(
    backup_dir: &Path,
    target_dir: &Path,
    manifest: &BackupManifest,
) -> io::Result<PathBuf> {
    let mut hasher = blake3::Hasher::new();
    hasher.update(b"tofu-db:restore-pin:v1\0");
    hasher.update(target_dir.to_string_lossy().as_bytes());
    hasher.update(&manifest.encode()?);
    Ok(backup_dir
        .join("pins")
        .join(format!("restore-{}.pin", hasher.finalize().to_hex())))
}

fn publish_restore_pin(
    backup_dir: &Path,
    target_dir: &Path,
    manifest: &BackupManifest,
    vfs: &dyn Vfs,
) -> io::Result<PathBuf> {
    let pins_path = backup_dir.join("pins");
    let existing_entries = vfs.read_directory(&pins_path)?;
    if existing_entries.len() > MAX_BACKUP_PINS * 2 {
        return Err(invalid_input("backup pin directory entry bound exceeded"));
    }
    let pin_path = restore_pin_path(backup_dir, target_dir, manifest)?;
    publish_manifest_file(&pins_path, &pin_path, manifest, ".pin-new-", vfs)?;
    Ok(pin_path)
}

fn remove_restore_pin(pin_path: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    match vfs.metadata(pin_path) {
        Ok(FileKind::File) => {
            vfs.remove_file(pin_path)?;
            sync_directory_barrier(
                vfs,
                pin_path
                    .parent()
                    .ok_or_else(|| invalid_data("restore pin has no parent"))?,
            )
        }
        Ok(FileKind::Directory) => Err(invalid_data("restore pin is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn existing_pins(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<Vec<BackupManifest>> {
    let pins_path = backup_dir.join("pins");
    let entries = vfs.read_directory(&pins_path)?;
    if entries.len() > MAX_BACKUP_PINS * 2 {
        return Err(invalid_input("backup pin directory entry bound exceeded"));
    }
    let mut pins = Vec::new();
    for path in entries {
        if path.extension().and_then(|value| value.to_str()) != Some("pin") {
            continue;
        }
        if pins.len() == MAX_BACKUP_PINS {
            return Err(invalid_input("backup pin count exceeds 1024"));
        }
        pins.push(read_manifest(&path, vfs)?);
    }
    Ok(pins)
}

fn load_snapshot_history(
    source: &BlockStore,
    manifest: &BackupManifest,
) -> io::Result<Option<HistoryManifest>> {
    let Some(history_id) = manifest.history_manifest_block_id else {
        return Ok(None);
    };
    let history = HistoryManifest::decode(&source.get(history_id)?)?;
    if history.checkpoint_sequence != manifest.checkpoint_sequence
        || history.checkpoint_hash != manifest.checkpoint_hash
    {
        return Err(invalid_data(
            "backup history root does not match retained snapshot",
        ));
    }
    Ok(Some(history))
}

fn write_live_marks(
    source: &BlockStore,
    retained: &[BackupManifest],
    backup_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<(SpillMarkSet, MarkMetrics)> {
    let latest = retained
        .iter()
        .max_by_key(|manifest| {
            (
                manifest.durable_sequence,
                manifest.source_control_generation,
            )
        })
        .ok_or_else(|| invalid_input("backup GC requires a retained generation"))?;
    let mut live = SpillMarkSet::initialize(backup_dir, vfs)?;
    for manifest in retained {
        if let Some(Some(root)) = manifest.authority_state_root {
            live.insert(root)?;
        }
        if let Some(history_id) = manifest.history_manifest_block_id {
            live.insert(history_id)?;
        }
        if let Some(history) = load_snapshot_history(source, manifest)? {
            for reference in &history.segments {
                live.insert(reference.block_id)?;
                let segment_bytes = source.get(reference.block_id)?;
                for transaction in decode_segment(&segment_bytes, reference)? {
                    for block_id in transaction.envelope.block_ids {
                        live.insert(block_id)?;
                    }
                }
            }
        }
    }
    {
        let live_cell = std::cell::RefCell::new(&mut live);
        visit_entity_page_graph_with_values(
            latest.authority_state_root.flatten(),
            |block_id| source.get(block_id),
            |block_id, _| live_cell.borrow_mut().insert(block_id),
            |key, value| {
                visit_semantic_value_blocks(source, key, value, |block_id, _| {
                    live_cell.borrow_mut().insert(block_id)
                })?;
                Ok(())
            },
        )?;
    }
    let metrics = live.finish()?;
    Ok((live, metrics))
}

fn deduplicate_manifests(manifests: &mut Vec<BackupManifest>) {
    manifests.sort_by_key(|manifest| {
        (
            manifest.durable_sequence,
            manifest.source_control_generation,
            manifest.root_hash,
        )
    });
    manifests.dedup();
}

pub fn prune_backup(backup_dir: &Path, retain_latest: usize) -> io::Result<BackupPruneMetrics> {
    if !backup_dir.is_absolute() {
        return Err(invalid_input("backup path must be absolute"));
    }
    prune_backup_with_vfs(backup_dir, retain_latest, Arc::new(RealVfs))
}

pub(crate) fn prune_backup_with_vfs(
    backup_dir: &Path,
    retain_latest: usize,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BackupPruneMetrics> {
    if retain_latest == 0 || retain_latest > MAX_BACKUP_GENERATIONS {
        return Err(invalid_input("backup retention must be between 1 and 4096"));
    }
    let (blocks, snapshots_path) = open_backup(backup_dir, Arc::clone(&vfs))?;
    let _backup_lease = acquire_backup_lease(backup_dir, vfs.as_ref())?;
    let plan = if let Some(plan) = read_optional_prune_plan(backup_dir, vfs.as_ref())? {
        plan
    } else {
        let mut manifests = existing_manifests(&snapshots_path, vfs.as_ref())?;
        if manifests.is_empty() {
            return Err(invalid_data("backup has no complete snapshot manifest"));
        }
        let authority_uuid = manifests[0].authority_uuid;
        if manifests
            .iter()
            .any(|manifest| manifest.authority_uuid != authority_uuid)
        {
            return Err(invalid_data("backup contains mixed authorities"));
        }
        manifests.sort_by_key(|manifest| {
            (
                manifest.durable_sequence,
                manifest.source_control_generation,
            )
        });
        let start = manifests.len().saturating_sub(retain_latest);
        let mut retained = manifests[start..].to_vec();
        for pin in existing_pins(backup_dir, vfs.as_ref())? {
            if !manifests.contains(&pin) {
                return Err(invalid_data("backup pin references a missing generation"));
            }
            retained.push(pin);
        }
        deduplicate_manifests(&mut retained);
        let plan = PrunePlan { retained };
        publish_prune_plan(backup_dir, &plan, vfs.as_ref())?;
        plan
    };

    for retained in &plan.retained {
        let path = snapshots_path.join(retained.file_name());
        if read_manifest(&path, vfs.as_ref())? != *retained {
            return Err(invalid_data("retained backup manifest changed"));
        }
    }
    let (live, mark_metrics) =
        write_live_marks(&blocks, &plan.retained, backup_dir, Arc::clone(&vfs))?;
    let manifests = existing_manifests(&snapshots_path, vfs.as_ref())?;
    let mut removed_generations = 0_u32;
    for manifest in manifests {
        if !plan.retained.contains(&manifest) {
            vfs.remove_file(&snapshots_path.join(manifest.file_name()))?;
            removed_generations = removed_generations
                .checked_add(1)
                .ok_or_else(|| invalid_data("removed generation count overflow"))?;
        }
    }
    if removed_generations > 0 {
        sync_directory_barrier(vfs.as_ref(), &snapshots_path)?;
    }

    let mut removal_metrics = BlockRemovalMetrics::default();
    blocks.visit_block_shards(MAX_GC_PHYSICAL_BLOCKS, |shard, block_ids| {
        let live_for_shard = live.live_for_shard(shard)?;
        let unreachable: Vec<_> = block_ids
            .into_iter()
            .filter(|block_id| live_for_shard.binary_search(block_id).is_err())
            .collect();
        let removed = blocks.remove_blocks(&unreachable)?;
        removal_metrics.blocks_removed = removal_metrics
            .blocks_removed
            .checked_add(removed.blocks_removed)
            .ok_or_else(|| invalid_data("removed block count overflow"))?;
        removal_metrics.bytes_removed = removal_metrics
            .bytes_removed
            .checked_add(removed.bytes_removed)
            .ok_or_else(|| invalid_data("removed block byte count overflow"))?;
        Ok(())
    })?;
    live.cleanup()?;
    vfs.remove_file(&backup_dir.join(PRUNE_MARKER))?;
    sync_directory_barrier(vfs.as_ref(), backup_dir)?;
    Ok(BackupPruneMetrics {
        retained_generations: u32::try_from(plan.retained.len())
            .map_err(|_| invalid_data("retained generation count overflow"))?,
        removed_generations,
        removed_blocks: removal_metrics.blocks_removed,
        removed_block_bytes: removal_metrics.bytes_removed,
        mark_references: mark_metrics.references,
        spill_bytes: mark_metrics.spill_bytes,
    })
}

fn restored_control_state(manifest: &BackupManifest) -> ControlState {
    ControlState {
        generation: 1,
        durable_sequence: manifest.durable_sequence,
        authority_uuid: manifest.authority_uuid,
        root_hash: manifest.root_hash,
        checkpoint_sequence: manifest.checkpoint_sequence,
        checkpoint_hash: manifest.checkpoint_hash,
        history_manifest_block_id: manifest.history_manifest_block_id,
        authority_state_root: manifest.authority_state_root,
        payload_manifest_block_id: None,
        active_log_generation: 1,
    }
}

fn read_optional_restore_marker(
    target_dir: &Path,
    vfs: &dyn Vfs,
) -> io::Result<Option<BackupManifest>> {
    let path = target_dir.join(RESTORE_MARKER);
    match vfs.metadata(&path) {
        Ok(FileKind::File) => read_manifest(&path, vfs).map(Some),
        Ok(FileKind::Directory) => Err(invalid_data("restore marker is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn read_optional_restored_control(
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<Option<ControlState>> {
    match vfs.metadata(&target_dir.join("CONTROL")) {
        Ok(FileKind::File) => {
            let mut control = ControlFile::open_with_vfs(target_dir, vfs)?;
            control.read_current().map(Some)
        }
        Ok(FileKind::Directory) => Err(invalid_data("restored CONTROL is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn publish_restore_marker(
    target_dir: &Path,
    manifest: &BackupManifest,
    vfs: &dyn Vfs,
) -> io::Result<()> {
    let entries = vfs.read_directory(target_dir)?;
    if entries.len() > MAX_RESTORE_ROOT_ENTRIES {
        return Err(invalid_input("restore target root entry bound exceeded"));
    }
    for path in entries {
        let name = path.file_name().and_then(|value| value.to_str());
        if name.is_some_and(|value| value.starts_with(RESTORE_TEMP_PREFIX)) {
            vfs.remove_file(&path)?;
        } else {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "new restore target must be empty",
            ));
        }
    }
    sync_directory_barrier(vfs, target_dir)?;
    let destination = target_dir.join(RESTORE_MARKER);
    let temporary = target_dir.join(format!("{RESTORE_TEMP_PREFIX}{}", Uuid::new_v4()));
    let encoded = manifest.encode()?;
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, &destination)?;
        sync_directory_barrier(vfs, target_dir)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result
}

fn validate_resume_layout(target_dir: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    let entries = vfs.read_directory(target_dir)?;
    if entries.len() > MAX_RESTORE_ROOT_ENTRIES {
        return Err(invalid_input("restore target root entry bound exceeded"));
    }
    for path in entries {
        let name = path.file_name().and_then(|value| value.to_str());
        if name == Some("payload-segments") {
            if vfs.metadata(&path)? != FileKind::Directory || !vfs.read_directory(&path)?.is_empty()
            {
                return Err(invalid_data(
                    "restore payload segment directory is not empty",
                ));
            }
            continue;
        }
        if !matches!(
            name,
            Some(RESTORE_MARKER | "blocks" | "active.wal" | "CONTROL")
        ) {
            return Err(invalid_data("restore target contains an unexpected entry"));
        }
    }
    Ok(())
}

fn open_or_initialize_restore_blocks(
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BlockStore> {
    match vfs.metadata(&target_dir.join("blocks")) {
        Ok(FileKind::Directory) => BlockStore::open_with_vfs(target_dir, vfs),
        Ok(FileKind::File) => Err(invalid_data("restore blocks path is not a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            BlockStore::initialize_with_vfs(target_dir, vfs)
        }
        Err(error) => Err(error),
    }
}

fn open_or_initialize_restore_wal(target_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<ActiveLog> {
    match vfs.metadata(&target_dir.join("active.wal")) {
        Ok(FileKind::File) => {
            let log = ActiveLog::open_with_vfs(target_dir, vfs)?;
            if !log.is_empty()? {
                return Err(invalid_data("restore active WAL is not empty"));
            }
            Ok(log)
        }
        Ok(FileKind::Directory) => Err(invalid_data("restore active WAL is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            ActiveLog::initialize_with_vfs(target_dir, vfs)
        }
        Err(error) => Err(error),
    }
}

pub fn create_incremental_backup(
    engine: &mut Engine,
    backup_dir: &Path,
) -> io::Result<BackupMetrics> {
    if !backup_dir.is_absolute() {
        return Err(invalid_input("backup path must be absolute"));
    }
    require_disjoint_paths(
        engine.data_dir(),
        backup_dir,
        "backup directory must not contain or be contained by the authority",
    )?;
    if !backup_dir.exists() {
        fs::create_dir_all(backup_dir)?;
    }
    create_incremental_backup_with_vfs(engine, backup_dir, Arc::new(RealVfs))
}

pub(crate) fn create_incremental_backup_with_vfs(
    engine: &mut Engine,
    backup_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BackupMetrics> {
    engine.require_usable_authority()?;
    let (destination, snapshots_path) = initialize_or_open_backup(backup_dir, Arc::clone(&vfs))?;
    let _backup_lease = acquire_backup_lease(backup_dir, vfs.as_ref())?;
    ensure_no_prune(backup_dir, vfs.as_ref())?;
    if !engine.committed_transactions().is_empty() {
        engine.checkpoint()?;
    }
    let manifest = BackupManifest::from_control(engine.state())?;
    let existing = existing_manifests(&snapshots_path, vfs.as_ref())?;
    if existing
        .iter()
        .any(|candidate| candidate.authority_uuid != manifest.authority_uuid)
    {
        return Err(invalid_input(
            "backup directory belongs to another authority",
        ));
    }
    if existing.len() == MAX_BACKUP_GENERATIONS
        && !existing.iter().any(|candidate| candidate == &manifest)
    {
        return Err(invalid_input("backup generation limit reached"));
    }
    let before = destination.write_metrics();
    copy_reachable_blocks(engine.block_store(), &destination, &manifest)?;
    let after = destination.write_metrics();
    let manifest_bytes = publish_manifest(&snapshots_path, &manifest, vfs.as_ref())?;
    Ok(metrics(
        manifest.durable_sequence,
        before,
        after,
        manifest_bytes,
    ))
}

fn metrics(
    durable_sequence: u64,
    before: BlockWriteMetrics,
    after: BlockWriteMetrics,
    manifest_bytes: u64,
) -> BackupMetrics {
    BackupMetrics {
        durable_sequence,
        copied_blocks: after.blocks_written - before.blocks_written,
        copied_block_bytes: after.bytes_written - before.bytes_written,
        manifest_bytes,
    }
}

pub fn restore_latest_backup(backup_dir: &Path, target_dir: &Path) -> io::Result<BackupManifest> {
    if !backup_dir.is_absolute() || !target_dir.is_absolute() {
        return Err(invalid_input("backup and restore paths must be absolute"));
    }
    require_disjoint_paths(
        backup_dir,
        target_dir,
        "restore target must not contain or be contained by the backup",
    )?;
    if !target_dir.exists() {
        fs::create_dir_all(target_dir)?;
    }
    preflight_restore_capacity(backup_dir, target_dir)?;
    restore_latest_backup_with_vfs(backup_dir, target_dir, Arc::new(RealVfs))
}

pub(crate) fn restore_latest_backup_with_vfs(
    backup_dir: &Path,
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BackupManifest> {
    if vfs.metadata(target_dir)? != FileKind::Directory {
        return Err(invalid_input("restore target is not a directory"));
    }
    let (source, snapshots_path) = open_backup(backup_dir, Arc::clone(&vfs))?;
    let _backup_lease = acquire_backup_lease(backup_dir, vfs.as_ref())?;
    ensure_no_prune(backup_dir, vfs.as_ref())?;
    let manifests = existing_manifests(&snapshots_path, vfs.as_ref())?;
    let authority_uuid = manifests
        .first()
        .map(|manifest| manifest.authority_uuid)
        .ok_or_else(|| invalid_data("backup has no complete snapshot manifest"))?;
    if manifests
        .iter()
        .any(|manifest| manifest.authority_uuid != authority_uuid)
    {
        return Err(invalid_data("backup contains mixed authorities"));
    }
    let existing_marker = read_optional_restore_marker(target_dir, vfs.as_ref())?;
    let (manifest, needs_restore_marker) = if let Some(marker) = existing_marker {
        if !manifests.iter().any(|candidate| candidate == &marker) {
            return Err(invalid_data(
                "restore marker does not identify a complete backup generation",
            ));
        }
        (marker, false)
    } else if let Some(control_state) =
        read_optional_restored_control(target_dir, Arc::clone(&vfs))?
    {
        let completed = manifests
            .iter()
            .find(|candidate| restored_control_state(candidate) == control_state)
            .ok_or_else(|| invalid_data("existing target authority is not this backup"))?
            .clone();
        validate_resume_layout(target_dir, vfs.as_ref())?;
        let destination = open_or_initialize_restore_blocks(target_dir, Arc::clone(&vfs))?;
        copy_reachable_blocks(&source, &destination, &completed)?;
        let _wal = open_or_initialize_restore_wal(target_dir, Arc::clone(&vfs))?;
        let pin_path = restore_pin_path(backup_dir, target_dir, &completed)?;
        remove_restore_pin(&pin_path, vfs.as_ref())?;
        return Ok(completed);
    } else {
        let latest = manifests
            .iter()
            .max_by_key(|candidate| {
                (
                    candidate.durable_sequence,
                    candidate.source_control_generation,
                )
            })
            .unwrap()
            .clone();
        (latest, true)
    };
    let pin_path = publish_restore_pin(backup_dir, target_dir, &manifest, vfs.as_ref())?;
    if needs_restore_marker {
        publish_restore_marker(target_dir, &manifest, vfs.as_ref())?;
    }
    validate_resume_layout(target_dir, vfs.as_ref())?;

    let destination = open_or_initialize_restore_blocks(target_dir, Arc::clone(&vfs))?;
    copy_reachable_blocks(&source, &destination, &manifest)?;
    let _wal = open_or_initialize_restore_wal(target_dir, Arc::clone(&vfs))?;
    let expected_state = restored_control_state(&manifest);
    match vfs.metadata(&target_dir.join("CONTROL")) {
        Ok(FileKind::File) => {
            let mut control = ControlFile::open_with_vfs(target_dir, Arc::clone(&vfs))?;
            if control.read_current()? != expected_state {
                return Err(invalid_data(
                    "restored CONTROL does not match restore marker",
                ));
            }
        }
        Ok(FileKind::Directory) => return Err(invalid_data("restored CONTROL is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            ControlFile::initialize_state_with_vfs(target_dir, expected_state, Arc::clone(&vfs))?;
        }
        Err(error) => return Err(error),
    }
    sync_directory_barrier(vfs.as_ref(), target_dir)?;
    vfs.remove_file(&target_dir.join(RESTORE_MARKER))?;
    sync_directory_barrier(vfs.as_ref(), target_dir)?;
    remove_restore_pin(&pin_path, vfs.as_ref())?;
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::authority::AuthorityDatabase;
    use crate::entity::{EntityDatabase, EntityKey};
    use crate::stream::{StreamEvent, StreamKey};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    #[test]
    fn legacy_backup_manifest_decodes_without_an_authority_root() {
        let manifest = BackupManifest {
            authority_uuid: Uuid::from_u128(7),
            source_control_generation: 3,
            durable_sequence: 1,
            root_hash: [4; 32],
            checkpoint_sequence: 1,
            checkpoint_hash: [4; 32],
            history_manifest_block_id: Some(BlockId([5; 32])),
            authority_state_root: Some(Some(BlockId([6; 32]))),
        };
        let current = manifest.encode().unwrap();
        let mut legacy = current[..149].to_vec();
        let crc = crc32c::crc32c(&legacy);
        legacy.extend_from_slice(&crc.to_le_bytes());
        let digest = blake3::hash(&legacy);
        legacy.extend_from_slice(digest.as_bytes());
        assert_eq!(legacy.len(), LEGACY_MANIFEST_BYTES);
        let decoded = BackupManifest::decode(&legacy).unwrap();
        assert_eq!(decoded.authority_state_root, None);
        assert_eq!(decoded.durable_sequence, 1);
    }

    #[test]
    fn incremental_backup_copies_only_new_blocks_and_restores_latest_snapshot() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();

        let first = engine
            .commit_transaction(b"first", &[b"shared block"])
            .unwrap();
        let first_backup = create_incremental_backup(&mut engine, backup.path()).unwrap();
        assert_eq!(first_backup.durable_sequence, 1);
        assert_eq!(first_backup.copied_blocks, 3);
        assert!(first_backup.manifest_bytes > 0);

        engine
            .commit_transaction(b"second", &[b"shared block"])
            .unwrap();
        let second_backup = create_incremental_backup(&mut engine, backup.path()).unwrap();
        assert_eq!(second_backup.durable_sequence, 2);
        assert_eq!(second_backup.copied_blocks, 2);
        assert!(second_backup.copied_block_bytes > 0);

        let repeated = create_incremental_backup(&mut engine, backup.path()).unwrap();
        assert_eq!(repeated.copied_blocks, 0);
        assert_eq!(repeated.copied_block_bytes, 0);
        assert_eq!(repeated.manifest_bytes, 0);

        let restored_manifest = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored_manifest.durable_sequence, 2);
        assert_eq!(
            restored_manifest.authority_uuid,
            engine.state().authority_uuid
        );
        drop(engine);

        let restored = Engine::open(target.path()).unwrap();
        let transactions = restored.transaction_snapshot().unwrap();
        assert_eq!(transactions.len(), 2);
        assert_eq!(transactions[0].envelope.inline_payload, b"first");
        assert_eq!(transactions[1].envelope.inline_payload, b"second");
        assert_eq!(
            restored.read_block(first.block_ids[0]).unwrap(),
            b"shared block"
        );
    }

    #[test]
    fn backup_rehydrates_segment_only_payloads_as_portable_loose_blocks() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let committed = engine
            .commit_transaction(b"packed", &[b"segment-only-backup-payload"])
            .unwrap();
        let block_id = committed.block_ids[0];
        engine.compact_payload_blocks(&[block_id]).unwrap();
        assert_eq!(
            engine.read_block(block_id).unwrap(),
            b"segment-only-backup-payload"
        );

        create_incremental_backup(&mut engine, backup.path()).unwrap();
        restore_latest_backup(backup.path(), target.path()).unwrap();
        drop(engine);
        let restored = Engine::open(target.path()).unwrap();
        assert_eq!(restored.payload_segment_count(), 0);
        assert_eq!(
            restored.read_block(block_id).unwrap(),
            b"segment-only-backup-payload"
        );
    }

    #[test]
    fn entity_reachability_copies_all_semantic_blocks_without_history() {
        let source = tempfile::tempdir().unwrap();
        let destination = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source.path()).unwrap();
        let key = EntityKey::new(
            7,
            11,
            crate::generated_tofudb_ir::TURN_DOCUMENT_NAMESPACE,
            b"capsule-turn",
        )
        .unwrap();
        let reference = {
            let mut write = database.begin(7, 11).unwrap();
            crate::versioned_document::put(
                &database,
                &mut write,
                crate::versioned_document::PutRequest {
                    key: key.clone(),
                    namespace: "turns".to_owned(),
                    logical_key: "capsule-turn".to_owned(),
                    value_json: serde_json::to_vec(&serde_json::json!({
                        "content": "x".repeat(20 * 1024)
                    }))
                    .unwrap(),
                    expected_version: Some(0),
                    updated_at_ms: 1,
                },
            )
            .unwrap();
            let stored = database.entity_get(&mut write, &key).unwrap().unwrap();
            let reference = crate::versioned_document::stored_blob_reference(&stored)
                .unwrap()
                .unwrap();
            database.commit(write).unwrap();
            reference
        };
        let stream_key = StreamKey::new(7, 11, "events", b"semantic-edges").unwrap();
        let mut stream_write = database.begin(7, 11).unwrap();
        database
            .stream_append(
                &mut stream_write,
                stream_key,
                1,
                vec![StreamEvent {
                    created_at_ms: 2,
                    event_type: "turn.created".to_owned(),
                    payload: vec![9; 4 * 1024],
                }],
            )
            .unwrap();
        database.commit(stream_write).unwrap();

        let receipt_response: Vec<u8> = (0..8 * 1024)
            .map(|index| ((index * 131 + index / 7) % 251) as u8)
            .collect();
        let mut receipt_write = database.begin(7, 11).unwrap();
        database
            .receipt_insert(
                &mut receipt_write,
                "semantic-edge-receipt",
                "record.put",
                [7; 32],
                &receipt_response,
                3,
            )
            .unwrap();
        database.commit(receipt_write).unwrap();
        let ranges = [key.clone().exact_range().unwrap()];
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"blob-capsule", &ranges)
            .unwrap();
        database
            .entity_retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();

        let source_blocks = BlockStore::open(source.path()).unwrap();
        let destination_blocks = BlockStore::initialize(destination.path()).unwrap();
        let root = database.authority_state_root();
        copy_entity_reachable_blocks(&source_blocks, &destination_blocks, root).unwrap();
        let metrics = crate::blob::visit_blob_graph(
            7,
            11,
            reference,
            |block_id| destination_blocks.get(block_id),
            |_, _| Ok(()),
        )
        .unwrap();
        assert_eq!(metrics.block_count, 2);
        assert!(metrics.payload_bytes > 0);

        let mut semantic_blocks = Vec::new();
        visit_entity_page_graph_with_values(
            root,
            |block_id| source_blocks.get(block_id),
            |_, _| Ok(()),
            |entity_key, value| {
                visit_semantic_value_blocks(&source_blocks, entity_key, value, |block_id, _| {
                    semantic_blocks.push(block_id);
                    Ok(())
                })
            },
        )
        .unwrap();
        semantic_blocks.sort_unstable();
        semantic_blocks.dedup();
        assert!(semantic_blocks.len() >= 5);
        for block_id in &semantic_blocks {
            destination_blocks.get(*block_id).unwrap();
        }

        let mut blob_blocks = Vec::new();
        crate::blob::visit_blob_graph(
            7,
            11,
            reference,
            |block_id| source_blocks.get(block_id),
            |block_id, _| {
                blob_blocks.push(block_id);
                Ok(())
            },
        )
        .unwrap();
        let mark_directory = tempfile::tempdir().unwrap();
        fs::create_dir(mark_directory.path().join("gc-runs")).unwrap();
        let retained = [BackupManifest {
            authority_uuid: Uuid::from_u128(1),
            source_control_generation: 0,
            durable_sequence: 0,
            root_hash: [0; 32],
            checkpoint_sequence: 0,
            checkpoint_hash: [0; 32],
            history_manifest_block_id: None,
            authority_state_root: Some(root),
        }];
        let (live, _) = write_live_marks(
            &source_blocks,
            &retained,
            mark_directory.path(),
            Arc::new(RealVfs),
        )
        .unwrap();
        blob_blocks.extend(semantic_blocks);
        blob_blocks.sort_unstable();
        blob_blocks.dedup();
        for block_id in blob_blocks {
            assert!(live
                .live_for_shard(block_id.0[0])
                .unwrap()
                .binary_search(&block_id)
                .is_ok());
        }
        live.cleanup().unwrap();
    }

    #[test]
    fn backup_and_restore_preserve_persistently_pinned_entity_roots() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let key = EntityKey::new(7, 11, "conversation", b"one").unwrap();
        let mut database = EntityDatabase::initialize(source.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database
            .put(&mut seed, key.clone(), b"old".to_vec())
            .unwrap();
        database.commit(seed).unwrap();
        let mut replace = database.begin(7, 11).unwrap();
        let end = EntityKey::new(7, 11, "conversation", b"one\0").unwrap();
        database
            .stage_persistent_range_snapshot_pin(
                &mut replace,
                b"trash:one",
                &[(key.clone(), end.clone())],
            )
            .unwrap();
        database.retire_range(&mut replace, &key, &end).unwrap();
        database.commit(replace).unwrap();
        let mut remount = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(
                &mut remount,
                b"trash:one",
                &[(key.clone(), end)],
            )
            .unwrap();
        database.commit(remount).unwrap();

        create_incremental_backup(database.engine_mut(), backup.path()).unwrap();
        drop(database);
        restore_latest_backup(backup.path(), target.path()).unwrap();

        let restored = EntityDatabase::open(target.path()).unwrap();
        let mut current = restored.begin(7, 11).unwrap();
        assert_eq!(
            restored.get(&mut current, &key).unwrap(),
            Some(b"old".to_vec())
        );
        let mut pinned = restored
            .begin_persistent_snapshot(7, 11, b"trash:one")
            .unwrap()
            .unwrap();
        assert_eq!(
            restored.get(&mut pinned, &key).unwrap(),
            Some(b"old".to_vec())
        );
    }

    #[test]
    fn restore_fails_closed_when_a_referenced_backup_block_is_corrupt() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let result = engine
            .commit_transaction(b"turn", &[b"required payload"])
            .unwrap();
        create_incremental_backup(&mut engine, backup.path()).unwrap();
        drop(engine);

        let hexadecimal = result.block_ids[0].to_hex();
        fs::write(
            backup
                .path()
                .join("blocks")
                .join(&hexadecimal[..2])
                .join(format!("{hexadecimal}.blk")),
            b"corrupt",
        )
        .unwrap();
        let error = restore_latest_backup(backup.path(), target.path()).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(!target.path().join("CONTROL").exists());
    }

    #[test]
    fn restore_never_initializes_or_mutates_an_invalid_backup_source() {
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        assert!(restore_latest_backup(backup.path(), target.path()).is_err());
        assert!(fs::read_dir(backup.path()).unwrap().next().is_none());
        assert!(fs::read_dir(target.path()).unwrap().next().is_none());
    }

    #[test]
    fn capacity_preflight_accounts_for_resume_and_refuses_insufficient_space() {
        let reserve = MAX_BLOCK_BYTES as u64 + RESTORE_METADATA_RESERVE_BYTES;
        let error = assess_restore_capacity(1_000, 400, reserve + 599).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::StorageFull);
        assert!(error.to_string().contains("requires"));
        let capacity = assess_restore_capacity(1_000, 400, reserve + 600).unwrap();
        assert_eq!(capacity.required_bytes, reserve + 600);
        assert_eq!(capacity.backup_bytes, 1_000);
        assert_eq!(capacity.retained_target_bytes, 400);
    }

    #[test]
    fn backup_and_restore_reject_overlapping_directory_trees() {
        let source = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let nested_backup = source.path().join("nested-backup");
        assert!(create_incremental_backup(&mut engine, &nested_backup).is_err());
        assert_eq!(engine.state().durable_sequence, 0);
        assert!(!nested_backup.exists());

        let backup = tempfile::tempdir().unwrap();
        create_incremental_backup(&mut engine, backup.path()).unwrap();
        let nested_target = backup.path().join("nested-restore");
        assert!(restore_latest_backup(backup.path(), &nested_target).is_err());
        assert!(!nested_target.join(RESTORE_MARKER).exists());
    }

    fn three_generation_backup() -> (
        tempfile::TempDir,
        tempfile::TempDir,
        Engine,
        Vec<BackupManifest>,
    ) {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let mut manifests = Vec::new();
        for sequence in 1..=3_u8 {
            engine
                .commit_transaction(&[sequence], &[&[sequence; 16]])
                .unwrap();
            create_incremental_backup(&mut engine, backup.path()).unwrap();
            let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
            manifests = existing_manifests(&snapshots, &RealVfs).unwrap();
        }
        manifests.sort_by_key(|manifest| manifest.durable_sequence);
        (source, backup, engine, manifests)
    }

    #[test]
    fn prune_retains_latest_generation_and_collects_only_unreachable_blocks() {
        let (_source, backup, engine, manifests) = three_generation_backup();
        let blocks = BlockStore::open(backup.path()).unwrap();
        let before = blocks.list_block_ids(MAX_GC_PHYSICAL_BLOCKS).unwrap().len();
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(metrics.retained_generations, 1);
        assert_eq!(metrics.removed_generations, 2);
        assert_eq!(metrics.removed_blocks, 2);
        assert!(metrics.removed_block_bytes > 0);
        assert_eq!(
            blocks.list_block_ids(MAX_GC_PHYSICAL_BLOCKS).unwrap().len(),
            before - 2
        );

        let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
        let retained = existing_manifests(&snapshots, &RealVfs).unwrap();
        assert_eq!(retained, vec![manifests[2].clone()]);
        let repeated = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(repeated.removed_generations, 0);
        assert_eq!(repeated.removed_blocks, 0);

        let target = tempfile::tempdir().unwrap();
        let restored = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored.durable_sequence, 3);
        drop(engine);
        assert_eq!(
            Engine::open(target.path())
                .unwrap()
                .transaction_snapshot()
                .unwrap()
                .len(),
            3
        );
    }

    #[test]
    fn backup_gc_accepts_generations_before_and_after_history_compaction() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut entities = EntityDatabase::initialize(source.path()).unwrap();
        let mut write = entities.begin(7, 11).unwrap();
        entities
            .put(
                &mut write,
                EntityKey::new(7, 11, "record", b"one").unwrap(),
                b"current-authority-state".to_vec(),
            )
            .unwrap();
        entities.commit(write).unwrap();
        create_incremental_backup(entities.engine_mut(), backup.path()).unwrap();
        entities.engine_mut().commit(b"two").unwrap();
        create_incremental_backup(entities.engine_mut(), backup.path()).unwrap();
        entities.engine_mut().commit(b"three").unwrap();
        entities.engine_mut().checkpoint().unwrap();
        assert_eq!(
            entities
                .engine_mut()
                .compact_history(1)
                .unwrap()
                .retired_segments,
            2
        );
        create_incremental_backup(entities.engine_mut(), backup.path()).unwrap();

        let metrics = prune_backup(backup.path(), 2).unwrap();
        assert_eq!(metrics.retained_generations, 2);
        assert_eq!(metrics.removed_generations, 1);
        let restored = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored.durable_sequence, 3);
        let reopened = Engine::open(target.path()).unwrap();
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 1);
        assert_eq!(reopened.transaction_at(3).unwrap().unwrap().sequence, 3);
    }

    #[test]
    fn prune_preserves_a_generation_pinned_by_an_interrupted_restore() {
        let (_source, backup, _engine, manifests) = three_generation_backup();
        let pinned_target = backup
            .path()
            .parent()
            .unwrap()
            .join(format!("pinned-target-{}", Uuid::new_v4()));
        let pin_path =
            publish_restore_pin(backup.path(), &pinned_target, &manifests[0], &RealVfs).unwrap();
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(metrics.retained_generations, 2);
        let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
        let retained = existing_manifests(&snapshots, &RealVfs).unwrap();
        assert!(retained.contains(&manifests[0]));
        assert!(retained.contains(&manifests[2]));

        remove_restore_pin(&pin_path, &RealVfs).unwrap();
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(metrics.retained_generations, 1);
        assert_eq!(metrics.removed_generations, 1);
    }

    #[test]
    fn backup_lease_serializes_backup_restore_and_prune() {
        let (vfs, mut engine) = simulated_restore_source();
        let _lease = acquire_backup_lease(Path::new("/backup"), vfs.as_ref()).unwrap();
        let backup_error =
            create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone())
                .unwrap_err();
        assert_eq!(backup_error.kind(), io::ErrorKind::WouldBlock);
        let restore_error =
            restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/target"), vfs.clone())
                .unwrap_err();
        assert_eq!(restore_error.kind(), io::ErrorKind::WouldBlock);
        let prune_error = prune_backup_with_vfs(Path::new("/backup"), 1, vfs).unwrap_err();
        assert_eq!(prune_error.kind(), io::ErrorKind::WouldBlock);
    }

    fn simulated_backup_source() -> (Arc<DeterministicVfs>, Engine) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        for path in ["/source", "/backup"] {
            vfs.create_dir(Path::new(path)).unwrap();
        }
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut engine = Engine::initialize_with_vfs(Path::new("/source"), vfs.clone()).unwrap();
        engine.commit_transaction(b"first", &[b"one"]).unwrap();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        engine.commit_transaction(b"second", &[b"two"]).unwrap();
        engine.checkpoint().unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn complete_sequences(vfs: &DeterministicVfs) -> io::Result<Vec<u64>> {
        let manifests = existing_manifests(Path::new("/backup/snapshots"), vfs)?;
        Ok(manifests
            .into_iter()
            .map(|manifest| manifest.durable_sequence)
            .collect())
    }

    fn simulated_restore_source() -> (Arc<DeterministicVfs>, Engine) {
        let (vfs, mut engine) = simulated_backup_source();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        vfs.create_dir(Path::new("/target")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn simulated_three_generation_backup() -> (Arc<DeterministicVfs>, Engine) {
        let (vfs, mut engine) = simulated_restore_source();
        engine.commit_transaction(b"third", &[b"three"]).unwrap();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn assert_simulated_latest_restores(vfs: Arc<DeterministicVfs>) {
        vfs.create_dir(Path::new("/pruned-target")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let manifest = restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/pruned-target"),
            vfs.clone(),
        )
        .unwrap();
        assert_eq!(manifest.durable_sequence, 3);
        let reopened = Engine::open_with_vfs(Path::new("/pruned-target"), vfs).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 3);
    }

    #[test]
    fn backup_publication_faults_expose_only_complete_generations() {
        let (baseline_vfs, mut baseline) = simulated_backup_source();
        create_incremental_backup_with_vfs(
            &mut baseline,
            Path::new("/backup"),
            baseline_vfs.clone(),
        )
        .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::SyncDirectory));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = simulated_backup_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ =
                create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let sequences = complete_sequences(&vfs).unwrap();
            assert!(sequences.contains(&1));
            assert!(sequences
                .iter()
                .all(|sequence| *sequence == 1 || *sequence == 2));
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = simulated_backup_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ =
                create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let sequences = complete_sequences(&vfs).unwrap();
            assert!(sequences.contains(&1));
            assert!(sequences
                .iter()
                .all(|sequence| *sequence == 1 || *sequence == 2));
        }

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let (vfs, mut engine) = simulated_backup_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone())
                .unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            assert!(complete_sequences(&vfs).unwrap().contains(&2));
        }
    }

    #[test]
    fn interrupted_restore_resumes_to_the_pinned_complete_generation() {
        let (baseline_vfs, baseline_engine) = simulated_restore_source();
        restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/target"),
            baseline_vfs.clone(),
        )
        .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline_engine);
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, engine) = simulated_restore_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let restored = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            )
            .unwrap();
            assert_eq!(restored.durable_sequence, 2);
            let reopened = Engine::open_with_vfs(Path::new("/target"), vfs).unwrap();
            assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, engine) = simulated_restore_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            assert_eq!(
                restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/target"), vfs,)
                    .unwrap()
                    .durable_sequence,
                2
            );
        }

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let (vfs, engine) = simulated_restore_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let restored = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            )
            .unwrap();
            assert_eq!(restored.durable_sequence, 2);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/target"), vfs).unwrap();
            assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        }
    }

    #[test]
    fn restore_marker_prevents_a_retry_from_switching_to_a_newer_backup() {
        let (baseline_vfs, baseline_engine) = simulated_restore_source();
        restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/target"),
            baseline_vfs.clone(),
        )
        .unwrap();
        let second_write = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .enumerate()
            .filter(|(_, operation)| operation == &&Operation::Write)
            .nth(2)
            .map(|(index, _)| index as u64 + 1)
            .unwrap();
        drop(baseline_engine);

        let (vfs, mut engine) = simulated_restore_source();
        vfs.arm_fault(Some(FaultRule {
            operation_number: second_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert!(restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/target"),
            vfs.clone(),
        )
        .is_err());
        assert_eq!(
            read_optional_restore_marker(Path::new("/target"), vfs.as_ref())
                .unwrap()
                .unwrap()
                .durable_sequence,
            2
        );

        vfs.arm_fault(None).unwrap();
        engine.commit_transaction(b"third", &[b"three"]).unwrap();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        let restored =
            restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/target"), vfs.clone())
                .unwrap();
        assert_eq!(restored.durable_sequence, 2);
        drop(engine);
        let reopened = Engine::open_with_vfs(Path::new("/target"), vfs).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
    }

    #[test]
    fn prune_faults_resume_without_losing_the_latest_generation() {
        let (baseline_vfs, baseline_engine) = simulated_three_generation_backup();
        prune_backup_with_vfs(Path::new("/backup"), 1, baseline_vfs.clone()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline_engine);
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(trace.contains(&Operation::SyncDirectory));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, engine) = simulated_three_generation_backup();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone()).unwrap();
            assert_simulated_latest_restores(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, engine) = simulated_three_generation_backup();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone()).unwrap();
            assert_simulated_latest_restores(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let (vfs, engine) = simulated_three_generation_backup();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone()).unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            assert_simulated_latest_restores(vfs);
        }
    }
}
