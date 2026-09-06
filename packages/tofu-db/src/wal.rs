//! Bounded active commit log with length twins, CRC32C, and BLAKE3 chaining.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::vfs::{
    sync_all_barrier, sync_data_barrier, sync_directory_barrier, OpenRequest, RealVfs, Vfs, VfsFile,
};
use crate::{ACTIVE_LOG_MAX_BYTES, FORMAT_VERSION};

const MAGIC: &[u8; 8] = b"TDBWAL01";
const FIXED_BODY_BYTES: usize = 8 + 4 + 8 + 32 + 4 + 4 + 32;
pub const WAL_RECORD_OVERHEAD_BYTES: usize = FIXED_BODY_BYTES + 8;
pub const MAX_TRANSACTION_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_GROUP_TRANSACTIONS: usize = 64;
pub const MAX_GROUP_ENCODED_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WalRecord {
    pub sequence: u64,
    pub parent_hash: [u8; 32],
    pub record_hash: [u8; 32],
    pub payload: Vec<u8>,
    pub end_offset: u64,
}

#[derive(Debug)]
pub struct Recovery {
    pub records: Vec<WalRecord>,
    pub truncated_bytes: u64,
}

pub struct ActiveLog {
    path: PathBuf,
    file: Box<dyn VfsFile>,
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn encode(sequence: u64, parent_hash: [u8; 32], payload: &[u8]) -> io::Result<(Vec<u8>, [u8; 32])> {
    if payload.len() > MAX_TRANSACTION_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "transaction exceeds 8 MiB",
        ));
    }
    let body_len = FIXED_BODY_BYTES.checked_add(payload.len()).ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "transaction length overflow")
    })?;
    let mut record = Vec::with_capacity(body_len + 8);
    record.extend_from_slice(&(body_len as u32).to_le_bytes());
    record.extend_from_slice(MAGIC);
    record.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    record.extend_from_slice(&sequence.to_le_bytes());
    record.extend_from_slice(&parent_hash);
    record.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    record.extend_from_slice(payload);
    let crc = crc32c::crc32c(&record[4..]);
    record.extend_from_slice(&crc.to_le_bytes());
    let hash = *blake3::hash(&record[4..]).as_bytes();
    record.extend_from_slice(&hash);
    record.extend_from_slice(&(body_len as u32).to_le_bytes());
    Ok((record, hash))
}

pub(crate) fn calculate_record_hash(
    sequence: u64,
    parent_hash: [u8; 32],
    payload: &[u8],
) -> io::Result<[u8; 32]> {
    encode(sequence, parent_hash, payload).map(|(_, hash)| hash)
}

fn decode_body(
    body: &[u8],
    expected_sequence: u64,
    expected_parent: [u8; 32],
    end_offset: u64,
) -> io::Result<WalRecord> {
    if body.len() < FIXED_BODY_BYTES || &body[..8] != MAGIC {
        return Err(invalid_data("invalid WAL record header"));
    }
    let version = u32::from_le_bytes(body[8..12].try_into().unwrap());
    let sequence = u64::from_le_bytes(body[12..20].try_into().unwrap());
    let parent_hash: [u8; 32] = body[20..52].try_into().unwrap();
    let payload_len = u32::from_le_bytes(body[52..56].try_into().unwrap()) as usize;
    if version != FORMAT_VERSION || sequence != expected_sequence || parent_hash != expected_parent
    {
        return Err(invalid_data(
            "WAL version, sequence, or parent witness mismatch",
        ));
    }
    if payload_len > MAX_TRANSACTION_BYTES || 56 + payload_len + 4 + 32 != body.len() {
        return Err(invalid_data("invalid WAL payload length"));
    }
    let crc_offset = 56 + payload_len;
    let stored_crc = u32::from_le_bytes(body[crc_offset..crc_offset + 4].try_into().unwrap());
    if crc32c::crc32c(&body[..crc_offset]) != stored_crc {
        return Err(invalid_data("WAL CRC32C mismatch"));
    }
    let hash_offset = crc_offset + 4;
    let stored_hash: [u8; 32] = body[hash_offset..].try_into().unwrap();
    if *blake3::hash(&body[..hash_offset]).as_bytes() != stored_hash {
        return Err(invalid_data("WAL BLAKE3 mismatch"));
    }
    Ok(WalRecord {
        sequence,
        parent_hash,
        record_hash: stored_hash,
        payload: body[56..crc_offset].to_vec(),
        end_offset,
    })
}

impl ActiveLog {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        Self::initialize_generation_with_vfs(data_dir, 1, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Self::initialize_generation_with_vfs(data_dir, 1, vfs)
    }

    pub fn initialize_generation_with_vfs(
        data_dir: &Path,
        generation: u64,
        vfs: Arc<dyn Vfs>,
    ) -> io::Result<Self> {
        let path = active_log_path(data_dir, generation)?;
        let mut file = vfs.open(
            &path,
            OpenRequest {
                read: true,
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        sync_all_barrier(file.as_mut())?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        Ok(Self { path, file })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_generation_with_vfs(data_dir, 1, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Self::open_generation_with_vfs(data_dir, 1, vfs)
    }

    pub fn open_generation_with_vfs(
        data_dir: &Path,
        generation: u64,
        vfs: Arc<dyn Vfs>,
    ) -> io::Result<Self> {
        let path = active_log_path(data_dir, generation)?;
        let file = vfs.open(
            &path,
            OpenRequest {
                read: true,
                write: true,
                ..OpenRequest::default()
            },
        )?;
        if file.len()? > ACTIVE_LOG_MAX_BYTES {
            return Err(invalid_data("active WAL exceeds the 64 MiB recovery bound"));
        }
        Ok(Self { path, file })
    }

    pub fn append(
        &mut self,
        sequence: u64,
        parent_hash: [u8; 32],
        payload: &[u8],
    ) -> io::Result<[u8; 32]> {
        let (record, hash) = encode(sequence, parent_hash, payload)?;
        let current_len = self.file.len()?;
        let next_len = current_len
            .checked_add(record.len() as u64)
            .ok_or_else(|| invalid_data("active WAL length overflow"))?;
        if next_len > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::other("active WAL rotation required"));
        }
        self.file.write_all_at(current_len, &record)?;
        sync_data_barrier(self.file.as_mut())?;
        Ok(hash)
    }

    /// Appends one durability group with a single write and durability barrier.
    /// Every record remains independently framed and hash chained so recovery
    /// can retain only a complete prefix when the write is torn.
    pub fn append_batch(
        &mut self,
        first_sequence: u64,
        parent_hash: [u8; 32],
        payloads: &[&[u8]],
    ) -> io::Result<Vec<[u8; 32]>> {
        if payloads.is_empty() || payloads.len() > MAX_GROUP_TRANSACTIONS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WAL group must contain between 1 and 64 transactions",
            ));
        }
        let mut bytes = Vec::new();
        let mut hashes = Vec::with_capacity(payloads.len());
        let mut parent = parent_hash;
        for (index, payload) in payloads.iter().enumerate() {
            let sequence = first_sequence
                .checked_add(index as u64)
                .ok_or_else(|| invalid_data("WAL group sequence overflow"))?;
            let (record, hash) = encode(sequence, parent, payload)?;
            let next_group_bytes = bytes
                .len()
                .checked_add(record.len())
                .ok_or_else(|| invalid_data("WAL group length overflow"))?;
            if next_group_bytes > MAX_GROUP_ENCODED_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "WAL group exceeds 8 MiB encoded bound",
                ));
            }
            bytes.extend_from_slice(&record);
            hashes.push(hash);
            parent = hash;
        }
        let current_len = self.file.len()?;
        let next_len = current_len
            .checked_add(bytes.len() as u64)
            .ok_or_else(|| invalid_data("active WAL length overflow"))?;
        if next_len > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::other("active WAL rotation required"));
        }
        self.file.write_all_at(current_len, &bytes)?;
        sync_data_barrier(self.file.as_mut())?;
        Ok(hashes)
    }

    pub fn recover(&mut self, durable_sequence: u64) -> io::Result<Recovery> {
        self.recover_from(0, [0; 32], durable_sequence)
    }

    pub fn recover_from(
        &mut self,
        checkpoint_sequence: u64,
        checkpoint_hash: [u8; 32],
        durable_sequence: u64,
    ) -> io::Result<Recovery> {
        if checkpoint_sequence > durable_sequence {
            return Err(invalid_data(
                "WAL checkpoint sequence exceeds durable sequence",
            ));
        }
        let file_len = self.file.len()?;
        if file_len > ACTIVE_LOG_MAX_BYTES {
            return Err(invalid_data("active WAL exceeds the 64 MiB recovery bound"));
        }
        let bytes = self.file.read_all(ACTIVE_LOG_MAX_BYTES as usize)?;
        let mut offset = 0_usize;
        let mut previous_sequence = checkpoint_sequence;
        let mut parent = checkpoint_hash;
        let mut records = Vec::new();
        let mut invalid_tail = false;
        while offset < bytes.len() {
            if bytes.len() - offset < 4 {
                invalid_tail = true;
                break;
            }
            let body_len =
                u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap()) as usize;
            let total_len = match body_len.checked_add(8) {
                Some(length) => length,
                None => {
                    invalid_tail = true;
                    break;
                }
            };
            if body_len < FIXED_BODY_BYTES
                || total_len > ACTIVE_LOG_MAX_BYTES as usize
                || bytes.len() - offset < total_len
            {
                invalid_tail = true;
                break;
            }
            let suffix_offset = offset + 4 + body_len;
            let suffix =
                u32::from_le_bytes(bytes[suffix_offset..suffix_offset + 4].try_into().unwrap())
                    as usize;
            if suffix != body_len {
                invalid_tail = true;
                break;
            }
            let Some(expected_sequence) = previous_sequence.checked_add(1) else {
                invalid_tail = true;
                break;
            };
            match decode_body(
                &bytes[offset + 4..suffix_offset],
                expected_sequence,
                parent,
                (offset + total_len) as u64,
            ) {
                Ok(record) => {
                    parent = record.record_hash;
                    previous_sequence = record.sequence;
                    records.push(record);
                    offset += total_len;
                }
                Err(_) => {
                    invalid_tail = true;
                    break;
                }
            }
        }
        let recovered_sequence = records
            .last()
            .map_or(checkpoint_sequence, |record| record.sequence);
        if invalid_tail && recovered_sequence < durable_sequence {
            return Err(invalid_data(format!(
                "WAL corruption precedes durable sequence {durable_sequence}"
            )));
        }
        let truncated_bytes = (bytes.len() - offset) as u64;
        if truncated_bytes > 0 {
            self.file.set_len(offset as u64)?;
            sync_all_barrier(self.file.as_mut())?;
        }
        Ok(Recovery {
            records,
            truncated_bytes,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn is_empty(&self) -> io::Result<bool> {
        self.file.is_empty()
    }

    pub fn would_exceed(&self, payload: &[u8], limit: u64) -> io::Result<bool> {
        if limit == 0 || limit > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "active WAL admission limit is invalid",
            ));
        }
        let (record, _) = encode(1, [0; 32], payload)?;
        Ok(self
            .file
            .len()?
            .checked_add(record.len() as u64)
            .is_none_or(|next| next > limit))
    }

    pub fn batch_would_exceed(&self, payloads: &[&[u8]], limit: u64) -> io::Result<bool> {
        if limit == 0 || limit > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "active WAL admission limit is invalid",
            ));
        }
        if payloads.is_empty() || payloads.len() > MAX_GROUP_TRANSACTIONS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WAL group must contain between 1 and 64 transactions",
            ));
        }
        let mut encoded_bytes = 0_usize;
        let mut parent = [0; 32];
        for (index, payload) in payloads.iter().enumerate() {
            let (record, hash) = encode(index as u64 + 1, parent, payload)?;
            encoded_bytes = encoded_bytes
                .checked_add(record.len())
                .ok_or_else(|| invalid_data("WAL group length overflow"))?;
            if encoded_bytes > MAX_GROUP_ENCODED_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "WAL group exceeds 8 MiB encoded bound",
                ));
            }
            parent = hash;
        }
        Ok(self
            .file
            .len()?
            .checked_add(encoded_bytes as u64)
            .is_none_or(|next| next > limit))
    }

    pub fn truncate(&mut self, end_offset: u64) -> io::Result<()> {
        if end_offset > self.file.len()? {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WAL truncation offset exceeds file length",
            ));
        }
        self.file.set_len(end_offset)?;
        sync_all_barrier(self.file.as_mut())
    }
}

fn active_log_path(data_dir: &Path, generation: u64) -> io::Result<PathBuf> {
    if generation == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "active WAL generation must be positive",
        ));
    }
    if generation == 1 {
        Ok(data_dir.join("active.wal"))
    } else {
        Ok(data_dir.join(format!("active-{generation:016x}.wal")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    fn durable_simulated_log() -> (Arc<DeterministicVfs>, ActiveLog) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let log = ActiveLog::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, log)
    }

    #[test]
    fn recovery_truncates_only_unconfirmed_torn_tail() {
        let directory = tempfile::tempdir().unwrap();
        let mut log = ActiveLog::initialize(directory.path()).unwrap();
        let first = log.append(1, [0; 32], b"one").unwrap();
        let end = log.file.len().unwrap();
        log.file.write_all_at(end, &[12, 0, 0]).unwrap();
        log.file.sync_all().unwrap();
        let recovery = log.recover(1).unwrap();
        assert_eq!(recovery.records.len(), 1);
        assert_eq!(recovery.records[0].record_hash, first);
        assert_eq!(recovery.truncated_bytes, 3);
    }

    #[test]
    fn recovery_refuses_loss_below_control_witness() {
        let directory = tempfile::tempdir().unwrap();
        let mut log = ActiveLog::initialize(directory.path()).unwrap();
        log.append(1, [0; 32], b"one").unwrap();
        log.append(2, *blake3::hash(b"wrong parent").as_bytes(), b"two")
            .unwrap();
        let error = log.recover(2).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn append_faults_recover_only_absent_or_complete_record() {
        let (baseline_vfs, mut baseline) = durable_simulated_log();
        baseline.append(1, [0; 32], b"one").unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert_eq!(
            trace,
            vec![
                Operation::Metadata,
                Operation::Write,
                Operation::SyncData,
                Operation::SyncData
            ]
        );

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut log) = durable_simulated_log();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Other),
            }))
            .unwrap();
            assert!(log.append(1, [0; 32], b"one").is_err());
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let recovery = reopened.recover(0).unwrap();
            assert!(recovery.records.is_empty() || recovery.records[0].payload == b"one");
            assert!(recovery.records.len() <= 1);
        }

        for (index, operation) in trace.iter().enumerate() {
            let action = match operation {
                Operation::Write => Some(FaultAction::ShortWrite(7)),
                Operation::SyncData => Some(FaultAction::DropSync),
                _ => None,
            };
            let Some(action) = action else {
                continue;
            };
            let (vfs, mut log) = durable_simulated_log();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let append_result = log.append(1, [0; 32], b"one");
            if matches!(action, FaultAction::ShortWrite(_)) {
                assert!(append_result.is_err());
            } else {
                assert!(append_result.is_ok());
            }
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let records = reopened.recover(0).unwrap().records;
            if matches!(action, FaultAction::DropSync) {
                assert_eq!(records.len(), 1);
                assert_eq!(records[0].payload, b"one");
            } else {
                assert!(records.is_empty());
            }
        }
    }

    #[test]
    fn synced_append_survives_a_crash() {
        let (vfs, mut log) = durable_simulated_log();
        let hash = log.append(1, [0; 32], b"one").unwrap();
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let recovery = reopened.recover(0).unwrap();
        assert_eq!(recovery.records.len(), 1);
        assert_eq!(recovery.records[0].record_hash, hash);
        assert_eq!(recovery.records[0].payload, b"one");
    }

    #[test]
    fn generated_log_recovers_from_a_checkpoint_chain() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut log =
            ActiveLog::initialize_generation_with_vfs(Path::new("/data"), 2, vfs.clone()).unwrap();
        let checkpoint_hash = [7; 32];
        let record_hash = log
            .append(42, checkpoint_hash, b"after checkpoint")
            .unwrap();
        vfs.crash().unwrap();
        let mut reopened = ActiveLog::open_generation_with_vfs(Path::new("/data"), 2, vfs).unwrap();
        let recovery = reopened.recover_from(41, checkpoint_hash, 42).unwrap();
        assert_eq!(recovery.records.len(), 1);
        assert_eq!(recovery.records[0].sequence, 42);
        assert_eq!(recovery.records[0].record_hash, record_hash);
    }
}
