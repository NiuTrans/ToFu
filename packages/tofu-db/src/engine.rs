//! Exclusive composition of CONTROL publication and WAL durability.

use std::collections::BTreeSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::block::{BlockId, BlockStore, BlockWriteMetrics, MAX_BLOCK_BYTES};
use crate::control::{ControlFile, ControlState};
use crate::history::{build_segments, decode_segment, HistoryManifest, HistorySegmentReference};
use crate::payload_manifest::{
    PayloadManifest, PayloadSegmentReference, MAX_PAYLOAD_SEGMENTS, MAX_PAYLOAD_SEGMENTS_PER_SHARD,
};
use crate::payload_segment::{
    PayloadSegmentOrphanCandidate, PayloadSegmentOrphanPlan, MAX_SEGMENT_BLOCKS,
    MAX_SEGMENT_PAYLOAD_BYTES,
};
use crate::transaction::TransactionEnvelope;
use crate::vfs::{sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile};
use crate::wal::{
    ActiveLog, MAX_GROUP_ENCODED_BYTES, MAX_GROUP_TRANSACTIONS, WAL_RECORD_OVERHEAD_BYTES,
};
use crate::{ACTIVE_LOG_MAX_BYTES, ACTIVE_LOG_ROTATE_BYTES};

pub struct Engine {
    data_dir: PathBuf,
    _lease: Box<dyn VfsFile>,
    control: ControlFile,
    wal: ActiveLog,
    blocks: BlockStore,
    vfs: Arc<dyn Vfs>,
    state: ControlState,
    history_manifest: Option<HistoryManifest>,
    payload_manifest: Option<PayloadManifest>,
    committed: Vec<CommittedTransaction>,
    rotation_threshold_bytes: u64,
    restart_required: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct HistoryCompactionMetrics {
    pub retained_first_sequence: u64,
    pub retained_segments: u32,
    pub retired_segments: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PayloadCompactionMetrics {
    pub segment_id: Uuid,
    pub blocks_packed: u32,
    pub payload_bytes: u64,
    pub segment_file_bytes: u64,
    pub loose_bytes_reclaimed: u64,
    pub manifest_block_id: BlockId,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PayloadSegmentGcMetrics {
    pub retired_segment_id: Uuid,
    pub replacement_segment_id: Option<Uuid>,
    pub blocks_retained: u32,
    pub blocks_retired: u32,
    pub retired_file_bytes: u64,
    pub replacement_file_bytes: u64,
    pub manifest_block_id: BlockId,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommitStage {
    BlockDurable(BlockId),
    WalDurable(u64),
    ControlDurable(u64),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommitResult {
    pub sequence: u64,
    pub block_ids: Vec<BlockId>,
}

/// Owned input for one member of a bounded durability group. Callers may
/// prepare these outside the sequencer; the engine validates the complete
/// group before publishing any of its transaction records.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BatchTransaction {
    pub inline_payload: Vec<u8>,
    pub block_payloads: Vec<Vec<u8>>,
}

pub(crate) struct PreparedBatchTransaction {
    block_payloads: Vec<Vec<u8>>,
    block_ids: Vec<BlockId>,
    encoded_envelope: Vec<u8>,
    logical_payload_bytes: usize,
    resident_bytes: usize,
}

impl BatchTransaction {
    pub(crate) fn prepare(self) -> io::Result<PreparedBatchTransaction> {
        if self
            .block_payloads
            .iter()
            .any(|payload| payload.len() > MAX_BLOCK_BYTES)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "batch transaction block exceeds 4 MiB",
            ));
        }
        let mut logical_payload_bytes = self.inline_payload.len();
        for payload in &self.block_payloads {
            logical_payload_bytes = logical_payload_bytes
                .checked_add(payload.len())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                })?;
        }
        if logical_payload_bytes > MAX_GROUP_ENCODED_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction logical payload exceeds 8 MiB",
            ));
        }
        let block_ids = self
            .block_payloads
            .iter()
            .map(|payload| BlockId::for_payload(payload))
            .collect::<Vec<_>>();
        let encoded_envelope = TransactionEnvelope {
            block_ids: block_ids.clone(),
            inline_payload: self.inline_payload,
            authority_state_update: None,
        }
        .encode()?;
        let wal_encoded_bytes = encoded_envelope
            .len()
            .checked_add(WAL_RECORD_OVERHEAD_BYTES)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "transaction WAL size overflow")
            })?;
        if wal_encoded_bytes > MAX_GROUP_ENCODED_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction encoded WAL exceeds 8 MiB",
            ));
        }
        let resident_bytes = std::mem::size_of::<PreparedBatchTransaction>()
            .checked_add(encoded_envelope.capacity())
            .and_then(|bytes| {
                bytes.checked_add(
                    self.block_payloads
                        .capacity()
                        .saturating_mul(std::mem::size_of::<Vec<u8>>()),
                )
            })
            .and_then(|bytes| {
                self.block_payloads
                    .iter()
                    .try_fold(bytes, |total, payload| {
                        total.checked_add(payload.capacity())
                    })
            })
            .and_then(|bytes| {
                bytes.checked_add(
                    block_ids
                        .capacity()
                        .saturating_mul(std::mem::size_of::<BlockId>()),
                )
            })
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "transaction resident size overflow",
                )
            })?;
        Ok(PreparedBatchTransaction {
            block_payloads: self.block_payloads,
            block_ids,
            encoded_envelope,
            logical_payload_bytes,
            resident_bytes,
        })
    }
}

impl PreparedBatchTransaction {
    pub(crate) fn logical_payload_bytes(&self) -> usize {
        self.logical_payload_bytes
    }

    pub(crate) fn wal_encoded_bytes(&self) -> usize {
        self.encoded_envelope.len() + WAL_RECORD_OVERHEAD_BYTES
    }

    pub(crate) fn resident_bytes(&self) -> usize {
        self.resident_bytes
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommittedTransaction {
    pub sequence: u64,
    pub record_hash: [u8; 32],
    pub envelope: TransactionEnvelope,
}

fn acquire_lease(data_dir: &Path, vfs: &dyn Vfs) -> io::Result<Box<dyn VfsFile>> {
    let mut lease = vfs.open(
        &data_dir.join("LOCK"),
        OpenRequest {
            read: true,
            write: true,
            create: true,
            ..OpenRequest::default()
        },
    )?;
    if let Err(error) = lease.try_lock_exclusive() {
        if error.kind() == io::ErrorKind::WouldBlock {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                format!("another tofu-db process owns the data directory: {error}"),
            ));
        }
        return Err(error);
    }
    Ok(lease)
}

impl Engine {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        if data_dir.exists() {
            if fs::read_dir(data_dir)?.next().is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "tofu-db initialization requires an empty explicit directory",
                ));
            }
        } else {
            fs::create_dir_all(data_dir)?;
        }
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        if vfs.metadata(data_dir)? != FileKind::Directory {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "tofu-db data path is not a real directory",
            ));
        }
        if !vfs.read_directory(data_dir)?.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "tofu-db initialization requires an empty explicit directory",
            ));
        }
        let lease = acquire_lease(data_dir, vfs.as_ref())?;
        let authority = Uuid::new_v4();
        let mut control = ControlFile::initialize_with_vfs(data_dir, authority, Arc::clone(&vfs))?;
        let wal = ActiveLog::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        let blocks = BlockStore::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        let state = control.read_current()?;
        Ok(Self {
            data_dir: data_dir.to_owned(),
            _lease: lease,
            control,
            wal,
            blocks,
            vfs,
            state,
            history_manifest: None,
            payload_manifest: None,
            committed: Vec::new(),
            rotation_threshold_bytes: ACTIVE_LOG_ROTATE_BYTES,
            restart_required: false,
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        if vfs.metadata(data_dir)? != FileKind::Directory {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "tofu-db data path is not a real directory",
            ));
        }
        let lease = acquire_lease(data_dir, vfs.as_ref())?;
        let mut control = ControlFile::open_with_vfs(data_dir, Arc::clone(&vfs))?;
        let mut state = control.read_current()?;
        let mut wal = ActiveLog::open_generation_with_vfs(
            data_dir,
            state.active_log_generation,
            Arc::clone(&vfs),
        )?;
        let blocks = BlockStore::open_with_vfs(data_dir, Arc::clone(&vfs))?;
        let payload_manifest = match state.payload_manifest_block_id {
            Some(manifest_block_id) => {
                let manifest = PayloadManifest::decode(&blocks.get(manifest_block_id)?)?;
                blocks.install_payload_manifest(manifest.clone())?;
                Some(manifest)
            }
            None => None,
        };
        let history_manifest = match state.history_manifest_block_id {
            Some(manifest_block_id) => {
                let manifest = HistoryManifest::decode(&blocks.get(manifest_block_id)?)?;
                if manifest.checkpoint_sequence != state.checkpoint_sequence
                    || manifest.checkpoint_hash != state.checkpoint_hash
                {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "CONTROL checkpoint does not match history manifest",
                    ));
                }
                Some(manifest)
            }
            None => None,
        };
        let mut recovery = wal.recover_from(
            state.checkpoint_sequence,
            state.checkpoint_hash,
            state.durable_sequence,
        )?;
        let mut keep_records = recovery.records.len();
        let mut committed = Vec::with_capacity(recovery.records.len());
        for (index, record) in recovery.records.iter().enumerate() {
            let envelope = TransactionEnvelope::decode(&record.payload)?;
            for block_id in &envelope.block_ids {
                match blocks.get(*block_id) {
                    Ok(_) => {}
                    Err(error)
                        if error.kind() == io::ErrorKind::NotFound
                            && record.sequence > state.durable_sequence =>
                    {
                        keep_records = index;
                        break;
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            format!(
                                "durable transaction {} references a missing block",
                                record.sequence
                            ),
                        ));
                    }
                    Err(error) => return Err(error),
                }
            }
            if keep_records != recovery.records.len() {
                break;
            }
            committed.push(CommittedTransaction {
                sequence: record.sequence,
                record_hash: record.record_hash,
                envelope,
            });
        }
        if keep_records != recovery.records.len() {
            let end_offset = if keep_records == 0 {
                0
            } else {
                recovery.records[keep_records - 1].end_offset
            };
            wal.truncate(end_offset)?;
            recovery.records.truncate(keep_records);
            committed.truncate(keep_records);
        }
        let recovered_sequence = recovery
            .records
            .last()
            .map_or(state.checkpoint_sequence, |record| record.sequence);
        if recovered_sequence < state.durable_sequence {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "CONTROL points beyond active WAL",
            ));
        }
        let witnessed_hash = if state.durable_sequence == state.checkpoint_sequence {
            state.checkpoint_hash
        } else {
            let relative_index = state
                .durable_sequence
                .checked_sub(state.checkpoint_sequence)
                .and_then(|distance| distance.checked_sub(1))
                .and_then(|index| usize::try_from(index).ok())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL WAL witness overflow")
                })?;
            recovery
                .records
                .get(relative_index)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL WAL witness is missing")
                })?
                .record_hash
        };
        if witnessed_hash != state.root_hash {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "CONTROL root does not match its durable WAL sequence",
            ));
        }
        if recovered_sequence > state.durable_sequence {
            let recovered_hash = recovery.records.last().unwrap().record_hash;
            let next = ControlState {
                generation: state.generation + 1,
                durable_sequence: recovered_sequence,
                authority_uuid: state.authority_uuid,
                root_hash: recovered_hash,
                checkpoint_sequence: state.checkpoint_sequence,
                checkpoint_hash: state.checkpoint_hash,
                history_manifest_block_id: state.history_manifest_block_id,
                payload_manifest_block_id: state.payload_manifest_block_id,
                authority_state_root: committed
                    .iter()
                    .filter(|transaction| transaction.sequence > state.durable_sequence)
                    .fold(state.authority_state_root, |current, transaction| {
                        transaction
                            .envelope
                            .authority_state_update
                            .map(Some)
                            .unwrap_or(current)
                    }),
                active_log_generation: state.active_log_generation,
            };
            control.publish(&next)?;
            state = next;
        }
        Ok(Self {
            data_dir: data_dir.to_owned(),
            _lease: lease,
            control,
            wal,
            blocks,
            vfs,
            state,
            history_manifest,
            payload_manifest,
            committed,
            rotation_threshold_bytes: ACTIVE_LOG_ROTATE_BYTES,
            restart_required: false,
        })
    }

    pub fn require_usable_authority(&self) -> io::Result<()> {
        if self.restart_required {
            return Err(io::Error::other(
                "tofu-db authority outcome is uncertain; close and reopen the engine",
            ));
        }
        Ok(())
    }

    fn publish_control(&mut self, next: &ControlState) -> io::Result<()> {
        if let Err(error) = self.control.publish(next) {
            // A failed CONTROL write or fsync can have reached stable storage even
            // when the caller observes an error. The in-memory generation must not
            // be used for another publication until recovery selects the authority.
            self.restart_required = true;
            return Err(error);
        }
        Ok(())
    }

    pub fn is_restart_required(&self) -> bool {
        self.restart_required
    }

    pub fn commit(&mut self, payload: &[u8]) -> io::Result<u64> {
        Ok(self.commit_transaction(payload, &[])?.sequence)
    }

    pub fn commit_transaction(
        &mut self,
        inline_payload: &[u8],
        block_payloads: &[&[u8]],
    ) -> io::Result<CommitResult> {
        self.commit_transaction_with_checkpoint(inline_payload, block_payloads, |_| Ok(()))
    }

    fn commit_transaction_with_checkpoint<F>(
        &mut self,
        inline_payload: &[u8],
        block_payloads: &[&[u8]],
        mut checkpoint: F,
    ) -> io::Result<CommitResult>
    where
        F: FnMut(CommitStage) -> io::Result<()>,
    {
        self.require_usable_authority()?;
        let mut block_ids = Vec::with_capacity(block_payloads.len());
        for payload in block_payloads {
            let block_id = self.blocks.put(payload)?;
            checkpoint(CommitStage::BlockDurable(block_id))?;
            block_ids.push(block_id);
        }
        let envelope = TransactionEnvelope {
            block_ids: block_ids.clone(),
            inline_payload: inline_payload.to_vec(),
            authority_state_update: None,
        }
        .encode()?;
        self.checkpoint_if_needed(&envelope)?;
        let sequence = self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })?;
        let root_hash = match self.wal.append(sequence, self.state.root_hash, &envelope) {
            Ok(root_hash) => root_hash,
            Err(error) => {
                // A failed append may have left a complete or torn record on
                // disk. Recovery must select/truncate that outcome before the
                // live file can accept another transaction.
                self.restart_required = true;
                return Err(error);
            }
        };
        checkpoint(CommitStage::WalDurable(sequence))?;
        let next = ControlState {
            generation: self.state.generation.checked_add(1).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "CONTROL generation overflow")
            })?,
            durable_sequence: sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root: self.state.authority_state_root,
            active_log_generation: self.state.active_log_generation,
        };
        // A transaction is acknowledged only after both the log and the new
        // CONTROL generation have reached durable storage.
        self.publish_control(&next)?;
        self.state = next;
        self.committed.push(CommittedTransaction {
            sequence,
            record_hash: root_hash,
            envelope: TransactionEnvelope::decode(&envelope)?,
        });
        checkpoint(CommitStage::ControlDurable(sequence))?;
        Ok(CommitResult {
            sequence,
            block_ids,
        })
    }

    /// Commits up to 64 already-prepared transactions with one WAL durability
    /// barrier and one CONTROL publication. A crash exposes the whole group or
    /// a prefix that was never acknowledged; successful return acknowledges
    /// every member atomically through the final sequence witness.
    pub fn commit_batch(
        &mut self,
        transactions: &[BatchTransaction],
    ) -> io::Result<Vec<CommitResult>> {
        self.require_usable_authority()?;
        let prepared = transactions
            .iter()
            .cloned()
            .map(BatchTransaction::prepare)
            .collect::<io::Result<Vec<_>>>()?;
        let prepared_references = prepared.iter().collect::<Vec<_>>();
        self.commit_prepared_batch(&prepared_references)
    }

    pub(crate) fn commit_prepared_batch(
        &mut self,
        transactions: &[&PreparedBatchTransaction],
    ) -> io::Result<Vec<CommitResult>> {
        self.require_usable_authority()?;
        if transactions.is_empty() || transactions.len() > MAX_GROUP_TRANSACTIONS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "commit group must contain between 1 and 64 transactions",
            ));
        }

        let mut logical_payload_bytes = 0_usize;
        for transaction in transactions {
            logical_payload_bytes = logical_payload_bytes
                .checked_add(transaction.logical_payload_bytes())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "commit group size overflow")
                })?;
            if logical_payload_bytes > MAX_GROUP_ENCODED_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "commit group logical payload exceeds 8 MiB",
                ));
            }
        }
        let encoded_envelopes = transactions
            .iter()
            .map(|transaction| transaction.encoded_envelope.as_slice())
            .collect::<Vec<_>>();
        if !self.committed.is_empty()
            && self
                .wal
                .batch_would_exceed(&encoded_envelopes, self.rotation_threshold_bytes)?
        {
            self.checkpoint()?;
        } else {
            // This also applies the encoded 8 MiB group admission bound before
            // immutable blocks are created when no checkpoint is needed.
            self.wal
                .batch_would_exceed(&encoded_envelopes, ACTIVE_LOG_MAX_BYTES)?;
        }

        for transaction in transactions {
            for payload in &transaction.block_payloads {
                self.blocks.put(payload)?;
            }
        }

        let first_sequence = self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })?;
        let authority_state_root = transactions.iter().try_fold(
            self.state.authority_state_root,
            |current, transaction| {
                let envelope = TransactionEnvelope::decode(&transaction.encoded_envelope)?;
                Ok::<_, io::Error>(envelope.authority_state_update.map(Some).unwrap_or(current))
            },
        )?;
        let hashes =
            match self
                .wal
                .append_batch(first_sequence, self.state.root_hash, &encoded_envelopes)
            {
                Ok(hashes) => hashes,
                Err(error) => {
                    self.restart_required = true;
                    return Err(error);
                }
            };
        let final_sequence = first_sequence
            .checked_add(transactions.len() as u64 - 1)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
            })?;
        let final_hash = *hashes
            .last()
            .ok_or_else(|| io::Error::other("WAL returned an empty group"))?;
        let next = ControlState {
            generation: self.state.generation.checked_add(1).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "CONTROL generation overflow")
            })?,
            durable_sequence: final_sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash: final_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root,
            active_log_generation: self.state.active_log_generation,
        };
        self.publish_control(&next)?;

        let mut results = Vec::with_capacity(transactions.len());
        for (index, (transaction, record_hash)) in transactions.iter().zip(hashes).enumerate() {
            let sequence = first_sequence + index as u64;
            let envelope = TransactionEnvelope::decode(&transaction.encoded_envelope)?;
            self.committed.push(CommittedTransaction {
                sequence,
                record_hash,
                envelope,
            });
            results.push(CommitResult {
                sequence,
                block_ids: transaction.block_ids.clone(),
            });
        }
        self.state = next;
        Ok(results)
    }

    pub fn state(&self) -> &ControlState {
        &self.state
    }
    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn read_block(&self, block_id: BlockId) -> io::Result<Vec<u8>> {
        self.require_usable_authority()?;
        self.blocks.get(block_id)
    }

    pub fn write_block(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.require_usable_authority()?;
        self.blocks.put(payload)
    }

    pub fn block_write_metrics(&self) -> BlockWriteMetrics {
        self.blocks.write_metrics()
    }

    pub(crate) fn block_store(&self) -> &BlockStore {
        &self.blocks
    }

    pub(crate) fn vfs(&self) -> Arc<dyn Vfs> {
        Arc::clone(&self.vfs)
    }

    pub fn next_sequence(&self) -> io::Result<u64> {
        self.require_usable_authority()?;
        self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })
    }

    pub fn committed_transactions(&self) -> &[CommittedTransaction] {
        &self.committed
    }

    #[cfg(test)]
    pub(crate) fn history_segment_block_ids(&self) -> Vec<BlockId> {
        self.history_manifest
            .as_ref()
            .map(|manifest| {
                manifest
                    .segments
                    .iter()
                    .map(|reference| reference.block_id)
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn transaction_snapshot(&self) -> io::Result<Vec<CommittedTransaction>> {
        self.require_usable_authority()?;
        let mut transactions = Vec::with_capacity(self.committed.len());
        if let Some(manifest) = &self.history_manifest {
            for reference in &manifest.segments {
                transactions.extend(decode_segment(
                    &self.blocks.get(reference.block_id)?,
                    reference,
                )?);
            }
        }
        transactions.extend(self.committed.iter().cloned());
        Ok(transactions)
    }

    pub fn retained_history_first_sequence(&self) -> Option<u64> {
        self.history_manifest
            .as_ref()
            .and_then(|manifest| manifest.segments.first())
            .map(|segment| segment.first_sequence)
    }

    pub fn history_segment_count(&self) -> usize {
        self.history_manifest
            .as_ref()
            .map_or(0, |manifest| manifest.segments.len())
    }

    pub fn payload_segment_count(&self) -> usize {
        self.payload_manifest
            .as_ref()
            .map_or(0, |manifest| manifest.segments.len())
    }

    pub(crate) fn payload_segment_references(&self) -> &[PayloadSegmentReference] {
        self.payload_manifest
            .as_ref()
            .map_or(&[], |manifest| manifest.segments.as_slice())
    }

    pub(crate) fn payload_segment_block_ids(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<Vec<BlockId>> {
        self.blocks.verify_payload_segment(reference)?;
        Ok(self
            .blocks
            .payload_segment_store()?
            .open_segment(reference.segment_id)?
            .block_ids()
            .collect())
    }

    pub(crate) fn plan_payload_segment_orphan(&self) -> io::Result<PayloadSegmentOrphanPlan> {
        let referenced_ids = self
            .payload_segment_references()
            .iter()
            .map(|reference| reference.segment_id)
            .collect::<BTreeSet<_>>();
        self.blocks
            .payload_segment_store()?
            .plan_orphan_file(&referenced_ids)
    }

    pub(crate) fn remove_payload_segment_orphan(
        &self,
        candidate: &PayloadSegmentOrphanCandidate,
    ) -> io::Result<u64> {
        if candidate.segment_id.is_some_and(|segment_id| {
            self.payload_segment_references()
                .iter()
                .any(|reference| reference.segment_id == segment_id)
        }) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment orphan became manifest-referenced",
            ));
        }
        self.blocks
            .payload_segment_store()?
            .remove_orphan_file(candidate)
    }

    #[cfg(test)]
    pub(crate) fn create_orphan_payload_segment_for_test(
        &self,
        block_id: BlockId,
    ) -> io::Result<Uuid> {
        let segment_id = Uuid::new_v4();
        self.blocks
            .payload_segment_store()?
            .create(segment_id, vec![block_id], |candidate| {
                self.blocks.get(candidate)
            })?;
        Ok(segment_id)
    }

    /// Packs one hash-shard victim set, publishes its catalog root, stabilizes
    /// both CONTROL fallback slots, and only then reclaims the loose copies.
    pub fn compact_payload_blocks(
        &mut self,
        block_ids: &[BlockId],
    ) -> io::Result<PayloadCompactionMetrics> {
        self.require_usable_authority()?;
        if block_ids.is_empty() || block_ids.len() > MAX_SEGMENT_BLOCKS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction victim count is outside its bounded range",
            ));
        }
        let mut sorted_ids = block_ids.to_vec();
        sorted_ids.sort_unstable();
        if sorted_ids.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction victims contain duplicate block IDs",
            ));
        }
        let shard = sorted_ids[0].0[0];
        if sorted_ids.iter().any(|block_id| block_id.0[0] != shard) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction victims must share one hash shard",
            ));
        }
        let existing_segments = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        if existing_segments.len() >= MAX_PAYLOAD_SEGMENTS
            || existing_segments
                .iter()
                .filter(|reference| reference.shard == shard)
                .count()
                >= MAX_PAYLOAD_SEGMENTS_PER_SHARD
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction catalog capacity is exhausted",
            ));
        }

        let mut victim_bytes = 0_u64;
        for block_id in &sorted_ids {
            victim_bytes = victim_bytes
                .checked_add(self.blocks.block_file_bytes(*block_id)?)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "payload victim size overflow")
                })?;
            if victim_bytes > MAX_SEGMENT_PAYLOAD_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "payload compaction victims exceed 256 MiB",
                ));
            }
        }

        let segment_id = Uuid::new_v4();
        let segment = self.blocks.payload_segment_store()?.create(
            segment_id,
            sorted_ids.clone(),
            |block_id| self.blocks.get(block_id),
        )?;
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )?;
        self.blocks.verify_payload_segment(&reference)?;
        let mut references = existing_segments.to_vec();
        references.push(reference);
        let manifest = PayloadManifest::new(references)?;
        let manifest_block_id = self.publish_payload_manifest(manifest, &sorted_ids)?;
        let removed = self.blocks.remove_blocks(&sorted_ids)?;
        if removed.blocks_removed != sorted_ids.len() as u64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "payload compaction did not reclaim every loose victim",
            ));
        }
        Ok(PayloadCompactionMetrics {
            segment_id,
            blocks_packed: sorted_ids.len() as u32,
            payload_bytes: segment.metadata().payload_bytes,
            segment_file_bytes: segment.metadata().file_bytes,
            loose_bytes_reclaimed: removed.bytes_removed,
            manifest_block_id,
        })
    }

    /// Rewrites one partially-live segment or retires one fully-dead segment.
    /// The caller must derive `live_block_ids` from the authority reachability
    /// walk while holding the engine's exclusive maintenance boundary.
    pub(crate) fn collect_payload_segment(
        &mut self,
        segment_id: Uuid,
        live_block_ids: &[BlockId],
    ) -> io::Result<PayloadSegmentGcMetrics> {
        self.require_usable_authority()?;
        let current = self
            .payload_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no payload manifest"))?;
        let position = current
            .segments
            .iter()
            .position(|reference| reference.segment_id == segment_id)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "payload segment is not current")
            })?;
        let retired_reference = current.segments[position];
        self.blocks.verify_payload_segment(&retired_reference)?;
        let retired_segment = self
            .blocks
            .payload_segment_store()?
            .open_segment(segment_id)?;
        let all_block_ids = retired_segment.block_ids().collect::<Vec<_>>();
        let mut retained_ids = live_block_ids.to_vec();
        retained_ids.sort_unstable();
        if retained_ids.windows(2).any(|pair| pair[0] == pair[1])
            || retained_ids
                .iter()
                .any(|block_id| all_block_ids.binary_search(block_id).is_err())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment GC live set is not a unique segment subset",
            ));
        }
        if retained_ids.len() == all_block_ids.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment GC refuses a fully-live no-op",
            ));
        }

        let mut references = current.segments.clone();
        let (replacement_segment_id, replacement_file_bytes) = if retained_ids.is_empty() {
            references.remove(position);
            (None, 0)
        } else {
            let replacement_id = Uuid::new_v4();
            let replacement = self.blocks.payload_segment_store()?.create(
                replacement_id,
                retained_ids.clone(),
                |block_id| {
                    retired_segment.get(block_id)?.ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "payload segment GC source block is missing",
                        )
                    })
                },
            )?;
            let replacement_reference = PayloadSegmentReference::new(
                replacement.metadata(),
                replacement.first_block_id(),
                replacement.last_block_id(),
            )?;
            self.blocks.verify_payload_segment(&replacement_reference)?;
            references[position] = replacement_reference;
            (Some(replacement_id), replacement.metadata().file_bytes)
        };
        let manifest_block_id =
            self.publish_payload_manifest(PayloadManifest::new(references)?, &[])?;
        let retired_file_bytes = self
            .blocks
            .payload_segment_store()?
            .remove_segment(segment_id)?;
        if retired_file_bytes != retired_reference.file_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "retired payload segment size changed after verification",
            ));
        }
        Ok(PayloadSegmentGcMetrics {
            retired_segment_id: segment_id,
            replacement_segment_id,
            blocks_retained: retained_ids.len() as u32,
            blocks_retired: (all_block_ids.len() - retained_ids.len()) as u32,
            retired_file_bytes,
            replacement_file_bytes,
            manifest_block_id,
        })
    }

    fn publish_payload_manifest(
        &mut self,
        manifest: PayloadManifest,
        forbidden_loose_removals: &[BlockId],
    ) -> io::Result<BlockId> {
        let encoded = manifest.encode()?;
        let manifest_block_id = BlockId::for_payload(&encoded);
        if forbidden_loose_removals
            .binary_search(&manifest_block_id)
            .is_ok()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "payload manifest root overlaps its loose victim set",
            ));
        }
        self.blocks.put_loose(&encoded)?;
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                payload_manifest_block_id: Some(manifest_block_id),
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        if let Err(error) = self.blocks.install_payload_manifest(manifest.clone()) {
            self.restart_required = true;
            return Err(error);
        }
        self.payload_manifest = Some(manifest);
        self.stabilize_control_slots()?;
        Ok(manifest_block_id)
    }

    pub(crate) fn visit_retained_transaction_blocks(
        &self,
        mut visitor: impl FnMut(BlockId) -> io::Result<()>,
    ) -> io::Result<()> {
        self.require_usable_authority()?;
        if let Some(manifest_id) = self.state.history_manifest_block_id {
            visitor(manifest_id)?;
        }
        if let Some(manifest_id) = self.state.payload_manifest_block_id {
            visitor(manifest_id)?;
        }
        if let Some(manifest) = &self.history_manifest {
            for reference in &manifest.segments {
                visitor(reference.block_id)?;
                for transaction in decode_segment(&self.blocks.get(reference.block_id)?, reference)?
                {
                    for block_id in transaction.envelope.block_ids {
                        visitor(block_id)?;
                    }
                }
            }
        }
        for transaction in &self.committed {
            for block_id in &transaction.envelope.block_ids {
                visitor(*block_id)?;
            }
        }
        Ok(())
    }

    pub(crate) fn stabilize_control_slots(&mut self) -> io::Result<()> {
        self.require_usable_authority()?;
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        Ok(())
    }

    /// Loads at most one bounded history segment for an exact sequence.
    pub fn transaction_at(&self, sequence: u64) -> io::Result<Option<CommittedTransaction>> {
        self.require_usable_authority()?;
        if sequence == 0 || sequence > self.state.durable_sequence {
            return Ok(None);
        }
        if sequence > self.state.checkpoint_sequence {
            let index = sequence
                .checked_sub(self.state.checkpoint_sequence)
                .and_then(|offset| offset.checked_sub(1))
                .and_then(|offset| usize::try_from(offset).ok())
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "active sequence offset overflow",
                    )
                })?;
            return self.committed.get(index).cloned().map(Some).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "active transaction sequence is missing",
                )
            });
        }
        let manifest = self.history_manifest.as_ref().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "checkpoint transaction has no history manifest",
            )
        })?;
        if sequence < manifest.segments[0].first_sequence {
            return Ok(None);
        }
        let index = manifest
            .segments
            .partition_point(|reference| reference.last_sequence < sequence);
        let reference = manifest.segments.get(index).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "checkpoint transaction sequence is missing",
            )
        })?;
        if sequence < reference.first_sequence {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "checkpoint history contains a sequence gap",
            ));
        }
        let transactions = decode_segment(&self.blocks.get(reference.block_id)?, reference)?;
        let transaction_index =
            usize::try_from(sequence - reference.first_sequence).map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "history transaction offset overflow",
                )
            })?;
        transactions
            .get(transaction_index)
            .cloned()
            .map(Some)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "history transaction sequence is missing",
                )
            })
    }

    pub fn checkpoint(&mut self) -> io::Result<u64> {
        self.require_usable_authority()?;
        if self.committed.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "checkpoint requires active committed transactions",
            ));
        }
        let unstored = build_segments(&self.committed, self.state.checkpoint_hash)?;
        let mut new_references = Vec::with_capacity(unstored.len());
        for segment in unstored {
            let block_id = self.blocks.put(&segment.encoded)?;
            new_references.push(HistorySegmentReference {
                block_id,
                first_sequence: segment.first_sequence,
                last_sequence: segment.last_sequence,
                parent_hash: segment.parent_hash,
                terminal_hash: segment.terminal_hash,
            });
        }
        let mut segments = self
            .history_manifest
            .as_ref()
            .map_or_else(Vec::new, |manifest| manifest.segments.clone());
        segments.extend(new_references);
        let manifest = HistoryManifest {
            checkpoint_sequence: self.state.durable_sequence,
            checkpoint_hash: self.state.root_hash,
            segments,
        };
        let manifest_block_id = self.blocks.put(&manifest.encode()?)?;
        let next_active_generation = self
            .state
            .active_log_generation
            .checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "WAL generation overflow"))?;
        let next_wal = match ActiveLog::initialize_generation_with_vfs(
            &self.data_dir,
            next_active_generation,
            Arc::clone(&self.vfs),
        ) {
            Ok(log) => log,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let log = ActiveLog::open_generation_with_vfs(
                    &self.data_dir,
                    next_active_generation,
                    Arc::clone(&self.vfs),
                )?;
                if !log.is_empty()? {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "orphan next-generation WAL is not empty",
                    ));
                }
                log
            }
            Err(error) => return Err(error),
        };
        let previous_wal_path = self.wal.path().to_owned();
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                durable_sequence: self.state.durable_sequence,
                authority_uuid: self.state.authority_uuid,
                root_hash: self.state.root_hash,
                checkpoint_sequence: self.state.durable_sequence,
                checkpoint_hash: self.state.root_hash,
                history_manifest_block_id: Some(manifest_block_id),
                payload_manifest_block_id: self.state.payload_manifest_block_id,
                authority_state_root: self.state.authority_state_root,
                active_log_generation: next_active_generation,
            };
        self.publish_control(&next)?;
        self.wal = next_wal;
        self.state = next;
        self.history_manifest = Some(manifest);
        self.committed.clear();
        if self.vfs.remove_file(&previous_wal_path).is_ok() {
            let _ = self.vfs.sync_directory(&self.data_dir);
        }
        Ok(self.state.checkpoint_sequence)
    }

    /// Publishes a suffix-only history manifest without touching the active WAL.
    /// The immutable retired segments remain available to retained backups until
    /// their explicit GC; normal open reads only this new bounded manifest.
    pub fn compact_history(
        &mut self,
        maximum_retained_segments: usize,
    ) -> io::Result<HistoryCompactionMetrics> {
        self.require_usable_authority()?;
        if maximum_retained_segments == 0
            || maximum_retained_segments > crate::history::MAX_HISTORY_SEGMENTS
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history retained-segment bound is invalid",
            ));
        }
        if !self.committed.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "history compaction requires an empty active WAL; checkpoint first",
            ));
        }
        if self.state.authority_state_root.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history compaction requires a current authority state root",
            ));
        }
        let current = self
            .history_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "history is empty"))?;
        let first_retained_index = current
            .segments
            .len()
            .saturating_sub(maximum_retained_segments);
        if first_retained_index == 0 {
            return Ok(HistoryCompactionMetrics {
                retained_first_sequence: current.segments[0].first_sequence,
                retained_segments: current.segments.len() as u32,
                retired_segments: 0,
            });
        }
        let segments = current.segments[first_retained_index..].to_vec();
        let manifest = HistoryManifest {
            checkpoint_sequence: current.checkpoint_sequence,
            checkpoint_hash: current.checkpoint_hash,
            segments,
        };
        let manifest_block_id = self.blocks.put(&manifest.encode()?)?;
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                history_manifest_block_id: Some(manifest_block_id),
                payload_manifest_block_id: self.state.payload_manifest_block_id,
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        self.history_manifest = Some(manifest);
        Ok(HistoryCompactionMetrics {
            retained_first_sequence: self.retained_history_first_sequence().unwrap(),
            retained_segments: self.history_manifest.as_ref().unwrap().segments.len() as u32,
            retired_segments: first_retained_index as u32,
        })
    }

    fn checkpoint_if_needed(&mut self, encoded_transaction: &[u8]) -> io::Result<()> {
        if !self.committed.is_empty()
            && self
                .wal
                .would_exceed(encoded_transaction, self.rotation_threshold_bytes)?
        {
            self.checkpoint()?;
        }
        Ok(())
    }

    pub fn commit_references(
        &mut self,
        inline_payload: &[u8],
        block_ids: &[BlockId],
    ) -> io::Result<CommitResult> {
        self.commit_references_with_state_update(inline_payload, block_ids, None)
    }

    pub(crate) fn commit_references_with_authority_state(
        &mut self,
        inline_payload: &[u8],
        block_ids: &[BlockId],
        authority_state_root: Option<BlockId>,
    ) -> io::Result<CommitResult> {
        self.commit_references_with_state_update(
            inline_payload,
            block_ids,
            Some(authority_state_root),
        )
    }

    fn commit_references_with_state_update(
        &mut self,
        inline_payload: &[u8],
        block_ids: &[BlockId],
        authority_state_update: Option<Option<BlockId>>,
    ) -> io::Result<CommitResult> {
        self.require_usable_authority()?;
        for block_id in block_ids {
            self.blocks.get(*block_id)?;
        }
        if let Some(Some(root)) = authority_state_update {
            self.blocks.get(root)?;
        }
        self.commit_envelope(TransactionEnvelope {
            block_ids: block_ids.to_vec(),
            inline_payload: inline_payload.to_vec(),
            authority_state_update,
        })
    }

    fn commit_envelope(&mut self, envelope: TransactionEnvelope) -> io::Result<CommitResult> {
        let encoded = envelope.encode()?;
        self.checkpoint_if_needed(&encoded)?;
        let sequence = self.next_sequence()?;
        let root_hash = match self.wal.append(sequence, self.state.root_hash, &encoded) {
            Ok(root_hash) => root_hash,
            Err(error) => {
                // Reference commits use pre-published immutable blocks, but
                // their WAL append has the same ambiguous complete/torn-tail
                // outcome as every other commit path. Recovery must select it
                // before this authority can assign another sequence.
                self.restart_required = true;
                return Err(error);
            }
        };
        let next = ControlState {
            generation: self.state.generation.checked_add(1).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "CONTROL generation overflow")
            })?,
            durable_sequence: sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root: envelope
                .authority_state_update
                .map(Some)
                .unwrap_or(self.state.authority_state_root),
            active_log_generation: self.state.active_log_generation,
        };
        self.publish_control(&next)?;
        self.state = next;
        self.committed.push(CommittedTransaction {
            sequence,
            record_hash: root_hash,
            envelope: envelope.clone(),
        });
        Ok(CommitResult {
            sequence,
            block_ids: envelope.block_ids,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    fn initialized_simulated_engine() -> (Arc<DeterministicVfs>, Engine) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn prepared_simulated_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn checkpointed_authority_history(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let root = engine.write_block(b"authority-root").unwrap();
        engine
            .commit_references_with_authority_state(b"one", &[], Some(root))
            .unwrap();
        engine.checkpoint().unwrap();
        for payload in [b"two".as_slice(), b"three"] {
            engine.commit(payload).unwrap();
            engine.checkpoint().unwrap();
        }
        vfs.arm_fault(None).unwrap();
        engine
    }

    fn is_sync(operation: &Operation) -> bool {
        matches!(
            operation,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        )
    }

    fn batch(count: usize) -> Vec<BatchTransaction> {
        (0..count)
            .map(|index| BatchTransaction {
                inline_payload: format!("turn-{index:02}").into_bytes(),
                block_payloads: Vec::new(),
            })
            .collect()
    }

    #[test]
    fn committed_sequence_survives_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let authority;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            authority = engine.state().authority_uuid;
            assert_eq!(engine.commit(b"transaction-ir-placeholder").unwrap(), 1);
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.state().authority_uuid, authority);
    }

    #[test]
    fn every_single_lost_initialization_sync_leaves_a_reopenable_store() {
        let baseline_vfs = prepared_simulated_vfs();
        let baseline =
            Engine::initialize_with_vfs(Path::new("/data"), baseline_vfs.clone()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let vfs = prepared_simulated_vfs();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(
                reopened.state().durable_sequence,
                0,
                "lost sync at {operation:?}"
            );
        }
    }

    #[test]
    fn repeated_checkpoints_rotate_wal_and_preserve_lazy_history() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            engine.commit(b"one").unwrap();
            assert_eq!(engine.checkpoint().unwrap(), 1);
            assert_eq!(engine.state().active_log_generation, 2);
            engine.commit(b"two").unwrap();
            assert_eq!(engine.checkpoint().unwrap(), 2);
            assert_eq!(engine.state().active_log_generation, 3);
            engine.commit(b"three").unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 3);
        assert_eq!(engine.state().checkpoint_sequence, 2);
        assert_eq!(engine.committed_transactions().len(), 1);
        let snapshot = engine.transaction_snapshot().unwrap();
        assert_eq!(snapshot.len(), 3);
        assert_eq!(snapshot[0].envelope.inline_payload, b"one");
        assert_eq!(snapshot[1].envelope.inline_payload, b"two");
        assert_eq!(snapshot[2].envelope.inline_payload, b"three");
    }

    #[test]
    fn explicit_history_compaction_publishes_a_bounded_suffix() {
        let vfs = prepared_simulated_vfs();
        let mut engine = checkpointed_authority_history(vfs.clone());
        let metrics = engine.compact_history(1).unwrap();
        assert_eq!(metrics.retained_first_sequence, 3);
        assert_eq!(metrics.retained_segments, 1);
        assert_eq!(metrics.retired_segments, 2);
        assert!(engine.transaction_at(1).unwrap().is_none());
        assert!(engine.transaction_at(2).unwrap().is_none());
        assert_eq!(
            engine
                .transaction_at(3)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"three"
        );
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        reopened.commit(b"four").unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        assert_eq!(reopened.transaction_at(4).unwrap().unwrap().sequence, 4);
    }

    #[test]
    fn every_history_compaction_fault_recovers_old_or_new_manifest() {
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline = checkpointed_authority_history(baseline_vfs.clone());
        baseline.compact_history(1).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.iter().any(is_sync));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                cases.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
            if is_sync(operation) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let vfs = prepared_simulated_vfs();
            let mut engine = checkpointed_authority_history(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_history(1);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 3);
            let first = recovered.retained_history_first_sequence().unwrap();
            assert!(first == 1 || first == 3);
            assert_eq!(recovered.transaction_at(3).unwrap().unwrap().sequence, 3);
        }
    }

    #[test]
    fn exact_transaction_lookup_reads_active_and_checkpointed_history() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        for payload in [b"one".as_slice(), b"two"] {
            engine.commit(payload).unwrap();
        }
        engine.checkpoint().unwrap();
        engine.commit(b"three").unwrap();
        for (sequence, payload) in [(1, b"one".as_slice()), (2, b"two"), (3, b"three")] {
            let transaction = engine.transaction_at(sequence).unwrap().unwrap();
            assert_eq!(transaction.sequence, sequence);
            assert_eq!(transaction.envelope.inline_payload, payload);
        }
        assert!(engine.transaction_at(0).unwrap().is_none());
        assert!(engine.transaction_at(4).unwrap().is_none());
    }

    #[test]
    fn commit_rotates_before_the_active_wal_soft_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.rotation_threshold_bytes = 500;
        engine.commit(&[1; 200]).unwrap();
        assert_eq!(engine.state().active_log_generation, 1);
        engine.commit(&[2; 200]).unwrap();
        assert_eq!(engine.state().checkpoint_sequence, 1);
        assert_eq!(engine.state().active_log_generation, 2);
        assert_eq!(engine.transaction_snapshot().unwrap().len(), 2);
    }

    #[test]
    fn failed_control_publication_requires_reopen_before_more_authority_work() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"first").unwrap();
        let control_sync_operation = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .rposition(|operation| operation == &Operation::SyncAll)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        let existing_block = engine.write_block(b"existing").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: control_sync_operation,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit(b"first").unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());

        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(engine.checkpoint().is_err());
        assert!(engine.transaction_snapshot().is_err());
        assert!(engine.read_block(existing_block).is_err());
        assert!(vfs.trace().unwrap().is_empty());

        drop(engine);
        vfs.crash().unwrap();
        let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(!recovered.is_restart_required());
        assert!(recovered.state().durable_sequence <= 1);
        recovered.commit(b"after recovery").unwrap();
    }

    #[test]
    fn every_checkpoint_io_error_recovers_the_same_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"checkpoint me").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncAll));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = engine.checkpoint();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            let snapshot = recovered.transaction_snapshot().unwrap();
            assert_eq!(snapshot.len(), 1);
            assert_eq!(snapshot[0].envelope.inline_payload, b"checkpoint me");
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = engine.checkpoint();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            assert_eq!(recovered.transaction_snapshot().unwrap().len(), 1);
        }
    }

    #[test]
    fn every_single_lost_commit_sync_preserves_the_acknowledged_transaction() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let committed = engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .unwrap();
            assert_eq!(committed.sequence, 1, "lost sync at {operation:?}");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            let transaction = &recovered.committed_transactions()[0];
            assert_eq!(transaction.envelope.inline_payload, b"turn.append");
            assert_eq!(
                recovered
                    .read_block(transaction.envelope.block_ids[0])
                    .unwrap(),
                b"large result"
            );
        }
    }

    #[test]
    fn every_single_lost_checkpoint_sync_preserves_the_published_generation() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"checkpoint me").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            assert_eq!(
                engine.checkpoint().unwrap(),
                1,
                "lost sync at {operation:?}"
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().checkpoint_sequence, 1);
            assert_eq!(recovered.state().active_log_generation, 2);
            assert_eq!(
                recovered.transaction_snapshot().unwrap()[0]
                    .envelope
                    .inline_payload,
                b"checkpoint me"
            );
        }
    }

    #[test]
    fn second_process_cannot_share_authority() {
        let directory = tempfile::tempdir().unwrap();
        let _engine = Engine::initialize(directory.path()).unwrap();
        let error = Engine::open(directory.path()).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn deterministic_vfs_enforces_the_engine_authority_lease() {
        let (vfs, _engine) = initialized_simulated_engine();
        let error = Engine::open_with_vfs(Path::new("/data"), vfs)
            .err()
            .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn complete_lost_ack_record_promotes_sequence_and_authority_root() {
        let directory = tempfile::tempdir().unwrap();
        let authority_root = {
            let engine = Engine::initialize(directory.path()).unwrap();
            engine.write_block(b"authority-root").unwrap()
        };
        {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            let payload = TransactionEnvelope {
                block_ids: vec![authority_root],
                inline_payload: b"possibly-committed".to_vec(),
                authority_state_update: Some(Some(authority_root)),
            }
            .encode()
            .unwrap();
            wal.append(1, [0; 32], &payload).unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.state().generation, 2);
        assert_eq!(
            engine.state().authority_state_root,
            Some(Some(authority_root))
        );
    }

    #[test]
    fn blocks_are_durable_before_their_commit_is_acknowledged() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            let result = engine
                .commit_transaction(b"turn.append", &[b"tool result"])
                .unwrap();
            block_id = result.block_ids[0];
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.read_block(block_id).unwrap(), b"tool result");
    }

    #[test]
    fn crashes_at_durability_boundaries_never_expose_partial_transaction() {
        for crash_stage in [
            CommitStage::BlockDurable(BlockId([0; 32])),
            CommitStage::WalDurable(1),
            CommitStage::ControlDurable(1),
        ] {
            let directory = tempfile::tempdir().unwrap();
            {
                let mut engine = Engine::initialize(directory.path()).unwrap();
                let error = engine
                    .commit_transaction_with_checkpoint(
                        b"turn.append",
                        &[b"large result"],
                        |stage| {
                            let should_crash = match (crash_stage, stage) {
                                (CommitStage::BlockDurable(_), CommitStage::BlockDurable(_)) => {
                                    true
                                }
                                (expected, actual) => expected == actual,
                            };
                            if should_crash {
                                Err(io::Error::new(
                                    io::ErrorKind::Interrupted,
                                    "simulated crash",
                                ))
                            } else {
                                Ok(())
                            }
                        },
                    )
                    .unwrap_err();
                assert_eq!(error.kind(), io::ErrorKind::Interrupted);
            }
            let engine = Engine::open(directory.path()).unwrap();
            let expected_sequence = match crash_stage {
                CommitStage::BlockDurable(_) => 0,
                CommitStage::WalDurable(_) | CommitStage::ControlDurable(_) => 1,
            };
            assert_eq!(engine.state().durable_sequence, expected_sequence);
        }
    }

    #[test]
    fn durable_transaction_with_missing_block_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            block_id = engine
                .commit_transaction(b"turn.append", &[b"required block"])
                .unwrap()
                .block_ids[0];
        }
        let hexadecimal = block_id.to_hex();
        fs::remove_file(
            directory
                .path()
                .join("blocks")
                .join(&hexadecimal[..2])
                .join(format!("{hexadecimal}.blk")),
        )
        .unwrap();
        let error = Engine::open(directory.path()).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("references a missing block"));
    }

    #[test]
    fn unconfirmed_transaction_with_missing_block_is_truncated() {
        let directory = tempfile::tempdir().unwrap();
        {
            let _engine = Engine::initialize(directory.path()).unwrap();
        }
        let missing = BlockId::for_payload(b"never persisted");
        let payload = TransactionEnvelope {
            block_ids: vec![missing],
            inline_payload: b"turn.append".to_vec(),
            authority_state_update: None,
        }
        .encode()
        .unwrap();
        {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            wal.append(1, [0; 32], &payload).unwrap();
        }
        {
            let engine = Engine::open(directory.path()).unwrap();
            assert_eq!(engine.state().durable_sequence, 0);
        }
        assert_eq!(
            fs::metadata(directory.path().join("active.wal"))
                .unwrap()
                .len(),
            0
        );
    }

    #[test]
    fn every_commit_io_error_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncData));
        assert!(trace.contains(&Operation::SyncAll));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            assert!(engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert!(recovered.state().durable_sequence <= 1);
            if recovered.state().durable_sequence == 1 {
                let transaction = &recovered.committed_transactions()[0];
                assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                assert_eq!(transaction.envelope.block_ids.len(), 1);
                assert_eq!(
                    recovered
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"large result"
                );
            }
        }
    }

    #[test]
    fn every_commit_short_write_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            assert!(engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert!(recovered.state().durable_sequence <= 1);
            if recovered.state().durable_sequence == 1 {
                let transaction = &recovered.committed_transactions()[0];
                assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                assert_eq!(transaction.envelope.block_ids.len(), 1);
                assert_eq!(
                    recovered
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"large result"
                );
            }
        }
    }

    #[test]
    fn sixty_four_transactions_share_one_wal_and_control_barrier() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let initial_generation = engine.state().generation;
        let results = engine.commit_batch(&batch(64)).unwrap();
        assert_eq!(results.len(), 64);
        assert_eq!(results[0].sequence, 1);
        assert_eq!(results[63].sequence, 64);
        assert_eq!(engine.state().durable_sequence, 64);
        assert_eq!(engine.state().generation, initial_generation + 1);
        let trace = vfs.trace().unwrap();
        assert_eq!(
            trace
                .iter()
                .filter(|operation| operation == &&Operation::SyncData)
                .count(),
            2
        );

        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state().durable_sequence, 64);
        let snapshot = recovered.transaction_snapshot().unwrap();
        assert_eq!(snapshot.len(), 64);
        assert_eq!(snapshot[0].envelope.inline_payload, b"turn-00");
        assert_eq!(snapshot[63].envelope.inline_payload, b"turn-63");
    }

    #[test]
    fn batch_block_roots_are_durable_and_returned_per_transaction() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let transactions = vec![
            BatchTransaction {
                inline_payload: b"conversation.turn.append".to_vec(),
                block_payloads: vec![b"large-turn".to_vec()],
            },
            BatchTransaction {
                inline_payload: b"task.event.append".to_vec(),
                block_payloads: vec![b"tool-result".to_vec(), b"large-turn".to_vec()],
            },
        ];
        let results = engine.commit_batch(&transactions).unwrap();
        assert_eq!(results[0].block_ids.len(), 1);
        assert_eq!(results[1].block_ids.len(), 2);
        assert_eq!(results[0].block_ids[0], results[1].block_ids[1]);

        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state().durable_sequence, 2);
        assert_eq!(
            recovered.read_block(results[0].block_ids[0]).unwrap(),
            b"large-turn"
        );
        assert_eq!(
            recovered.read_block(results[1].block_ids[0]).unwrap(),
            b"tool-result"
        );
    }

    #[test]
    fn batch_rotates_before_crossing_the_active_wal_soft_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.rotation_threshold_bytes = 500;
        engine.commit(&[1; 200]).unwrap();
        let transactions = vec![
            BatchTransaction {
                inline_payload: vec![2; 200],
                block_payloads: Vec::new(),
            },
            BatchTransaction {
                inline_payload: vec![3; 200],
                block_payloads: Vec::new(),
            },
        ];
        let results = engine.commit_batch(&transactions).unwrap();
        assert_eq!(results[0].sequence, 2);
        assert_eq!(results[1].sequence, 3);
        assert_eq!(engine.state().checkpoint_sequence, 1);
        assert_eq!(engine.state().active_log_generation, 2);
        assert_eq!(engine.transaction_snapshot().unwrap().len(), 3);
    }

    #[test]
    fn batch_admission_rejects_unbounded_work_before_writes() {
        let (vfs, mut engine) = initialized_simulated_engine();
        assert_eq!(
            engine.commit_batch(&[]).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            engine.commit_batch(&batch(65)).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized = (0..64)
            .map(|_| BatchTransaction {
                inline_payload: vec![7; 131_072],
                block_payloads: Vec::new(),
            })
            .collect::<Vec<_>>();
        assert_eq!(
            engine.commit_batch(&oversized).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized_blocks = vec![BatchTransaction {
            inline_payload: Vec::new(),
            block_payloads: vec![
                vec![1; MAX_BLOCK_BYTES],
                vec![2; MAX_BLOCK_BYTES],
                vec![3; 1],
            ],
        }];
        assert_eq!(
            engine.commit_batch(&oversized_blocks).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(!vfs
            .trace()
            .unwrap()
            .iter()
            .any(|operation| matches!(operation, Operation::Write)));
        assert_eq!(engine.state().durable_sequence, 0);
    }

    #[test]
    fn every_single_lost_batch_sync_preserves_the_acknowledged_group() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let results = engine.commit_batch(&batch(64)).unwrap();
            assert_eq!(results.len(), 64, "lost sync at {operation:?}");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(
                recovered.state().durable_sequence,
                64,
                "lost sync at {operation:?}"
            );
        }
    }

    #[test]
    fn every_batch_write_fault_recovers_only_an_unacknowledged_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(127),
            }))
            .unwrap();
            assert!(engine.commit_batch(&batch(64)).is_err());
            assert!(engine.is_restart_required());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let durable = recovered.state().durable_sequence as usize;
            assert!(durable <= 64);
            for (index, transaction) in recovered.committed_transactions().iter().enumerate() {
                assert_eq!(
                    transaction.envelope.inline_payload,
                    format!("turn-{index:02}").as_bytes()
                );
            }
        }
    }

    #[test]
    fn every_batch_io_error_recovers_only_an_unacknowledged_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let operation_count = baseline_vfs.trace().unwrap().len() as u64;

        for operation_number in 1..=operation_count {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            assert!(engine.commit_batch(&batch(64)).is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let durable = recovered.state().durable_sequence as usize;
            assert!(durable <= 64);
            assert_eq!(recovered.committed_transactions().len(), durable);
            for (index, transaction) in recovered.committed_transactions().iter().enumerate() {
                assert_eq!(
                    transaction.envelope.inline_payload,
                    format!("turn-{index:02}").as_bytes()
                );
            }
        }
    }

    #[test]
    fn failed_wal_append_requires_reopen_before_more_authority_work() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(2)).unwrap();
        let first_write = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (single_baseline_vfs, mut single_baseline) = initialized_simulated_engine();
        single_baseline.commit(b"single").unwrap();
        let single_first_write = single_baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        vfs.arm_fault(Some(FaultRule {
            operation_number: first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit_batch(&batch(2)).unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(vfs.trace().unwrap().is_empty());

        let (vfs, mut engine) = initialized_simulated_engine();
        vfs.arm_fault(Some(FaultRule {
            operation_number: single_first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit(b"single").unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit_batch(&batch(2)).is_err());
        assert!(vfs.trace().unwrap().is_empty());

        let (reference_baseline_vfs, mut reference_baseline) = initialized_simulated_engine();
        let reference_block = reference_baseline.write_block(b"reference").unwrap();
        reference_baseline_vfs.arm_fault(None).unwrap();
        reference_baseline
            .commit_references(b"reference-commit", &[reference_block])
            .unwrap();
        let reference_first_write = reference_baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        let reference_block = engine.write_block(b"reference").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: reference_first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine
                .commit_references(b"reference-commit", &[reference_block])
                .unwrap_err()
                .kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(vfs.trace().unwrap().is_empty());
    }

    fn engine_with_compaction_victim() -> (Arc<DeterministicVfs>, Engine, BlockId) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let result = engine
            .commit_transaction(b"payload-reference", &[b"large-agent-tool-payload"])
            .unwrap();
        let block_id = result.block_ids[0];
        vfs.arm_fault(None).unwrap();
        (vfs, engine, block_id)
    }

    fn two_payloads_in_one_shard() -> (Vec<u8>, Vec<u8>) {
        let mut first_by_shard = std::collections::BTreeMap::<u8, Vec<u8>>::new();
        for value in 0..10_000_u32 {
            let payload = format!("segment-gc-payload-{value}").into_bytes();
            let shard = BlockId::for_payload(&payload).0[0];
            if let Some(first) = first_by_shard.remove(&shard) {
                return (first, payload);
            }
            first_by_shard.insert(shard, payload);
        }
        panic!("failed to find two fixture payloads in one hash shard");
    }

    fn engine_with_partially_live_segment() -> (
        Arc<DeterministicVfs>,
        Engine,
        Uuid,
        BlockId,
        Vec<u8>,
        BlockId,
    ) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let (live_payload, dead_payload) = two_payloads_in_one_shard();
        let committed = engine
            .commit_transaction(b"live-segment-reference", &[live_payload.as_slice()])
            .unwrap();
        let live_block_id = committed.block_ids[0];
        let dead_block_id = engine.write_block(&dead_payload).unwrap();
        let compacted = engine
            .compact_payload_blocks(&[live_block_id, dead_block_id])
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (
            vfs,
            engine,
            compacted.segment_id,
            live_block_id,
            live_payload,
            dead_block_id,
        )
    }

    #[test]
    fn payload_compaction_stabilizes_catalog_before_reclaim_and_reopens() {
        let (vfs, mut engine, block_id) = engine_with_compaction_victim();
        let metrics = engine.compact_payload_blocks(&[block_id]).unwrap();
        assert_eq!(metrics.blocks_packed, 1);
        assert_eq!(engine.payload_segment_count(), 1);
        assert!(!engine
            .blocks
            .list_block_ids(100)
            .unwrap()
            .contains(&block_id));
        assert_eq!(
            engine.read_block(block_id).unwrap(),
            b"large-agent-tool-payload"
        );
        assert_eq!(
            engine.write_block(b"large-agent-tool-payload").unwrap(),
            block_id
        );
        assert!(!engine
            .blocks
            .list_block_ids(100)
            .unwrap()
            .contains(&block_id));

        let mut control_file = vfs
            .open(
                Path::new("/data/CONTROL"),
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        control_file.write_all_at(4096 + 300, &[0xff]).unwrap();
        control_file.sync_all().unwrap();
        control_file.sync_all().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 1);
        assert_eq!(reopened.state().generation, 3);
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(
            reopened.read_block(block_id).unwrap(),
            b"large-agent-tool-payload"
        );
    }

    #[test]
    fn every_payload_compaction_fault_preserves_the_referenced_block() {
        let (baseline_vfs, mut baseline, baseline_block) = engine_with_compaction_victim();
        baseline.compact_payload_blocks(&[baseline_block]).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(
            trace
                .iter()
                .filter(|operation| **operation == Operation::Write)
                .count()
                >= 3
        );

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(5))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut engine, block_id) = engine_with_compaction_victim();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_payload_blocks(&[block_id]);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 1);
            assert_eq!(
                reopened.read_block(block_id).unwrap(),
                b"large-agent-tool-payload"
            );
        }
    }

    #[test]
    fn payload_segment_gc_rewrites_partial_and_retires_fully_dead_segments() {
        let (vfs, mut engine, segment_id, live_id, live_payload, dead_id) =
            engine_with_partially_live_segment();
        let rewritten = engine
            .collect_payload_segment(segment_id, &[live_id])
            .unwrap();
        assert_eq!(rewritten.blocks_retained, 1);
        assert_eq!(rewritten.blocks_retired, 1);
        assert!(rewritten.replacement_segment_id.is_some());
        assert_eq!(engine.read_block(live_id).unwrap(), live_payload);
        assert_eq!(
            engine.read_block(dead_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(live_id).unwrap(), live_payload);

        let (vfs, mut engine) = initialized_simulated_engine();
        let orphan_id = engine.write_block(b"fully-dead-segment").unwrap();
        let compacted = engine.compact_payload_blocks(&[orphan_id]).unwrap();
        let retired = engine
            .collect_payload_segment(compacted.segment_id, &[])
            .unwrap();
        assert_eq!(retired.blocks_retired, 1);
        assert_eq!(retired.replacement_segment_id, None);
        assert_eq!(engine.payload_segment_count(), 0);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 0);
        assert_eq!(
            reopened.read_block(orphan_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn every_payload_segment_gc_fault_preserves_all_live_blocks() {
        let (baseline_vfs, mut baseline, segment_id, live_id, _, _) =
            engine_with_partially_live_segment();
        baseline
            .collect_payload_segment(segment_id, &[live_id])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(5))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut engine, segment_id, live_id, live_payload, _) =
                engine_with_partially_live_segment();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.collect_payload_segment(segment_id, &[live_id]);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 1);
            assert_eq!(reopened.read_block(live_id).unwrap(), live_payload);
        }
    }
}
