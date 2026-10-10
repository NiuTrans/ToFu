//! Exclusive composition of CONTROL publication and WAL durability.

#[path = "engine_history_compaction.rs"]
mod history_compaction;
pub(crate) use history_compaction::{encode_history_compaction, PreparedHistoryCompaction, HistoryCompactionOutcome};
#[path = "engine_rotation_publication.rs"]
mod rotation_publication;
pub(crate) use rotation_publication::{publish_rotation_publication, RotationPublication, PreparedRotationPublication};

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use uuid::Uuid;

use crate::at_rest;
use crate::block::{BlockId, BlockStore, BlockWriteMetrics, MAX_BLOCK_BYTES};
use crate::control::{ControlFile, ControlState};
use crate::history::{
    decode_segment, transaction_history_bytes, visit_segments, HistoryManifest,
    HistorySegmentEncoder, HistorySegmentReference, HISTORY_SEGMENT_MAX_BYTES,
};
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference, MAX_PAYLOAD_SEGMENTS};
use crate::payload_segment::{
    PayloadSegmentOrphanCandidate, PayloadSegmentOrphanPlan, MAX_SEGMENT_BLOCKS,
    MAX_SEGMENT_PAYLOAD_BYTES,
};
use crate::transaction::{TransactionEnvelope, MAX_CARRIED_PAYLOAD_BYTES};
use crate::vfs::{sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile};
use crate::wal::{
    ActiveLog, WalBarrier, WalBarrierDriver, WalDurability, MAX_GROUP_ENCODED_BYTES,
    MAX_GROUP_TRANSACTIONS, WAL_RECORD_OVERHEAD_BYTES,
};
use crate::{ACTIVE_LOG_MAX_BYTES, ACTIVE_LOG_ROTATE_BYTES};

const MAX_STAGED_HISTORY_SEGMENTS: usize =
    ACTIVE_LOG_MAX_BYTES as usize / (HISTORY_SEGMENT_MAX_BYTES / 2) + 1;

pub struct Engine {
    data_dir: PathBuf,
    _lease: Box<dyn VfsFile>,
    /// Alternating checksummed CONTROL slots behind their own mutex so a
    /// prepared catalog publication can fsync outside the authority
    /// critical section. Every publication still serializes here.
    control: Arc<Mutex<ControlFile>>,
    /// Set while a prepared catalog publication fsyncs outside the authority
    /// mutex (prepare → publish → finish). Synchronous publishers refuse
    /// with WouldBlock instead of queueing a CONTROL write behind an
    /// unlocked publication; foreground commits never publish and are never
    /// delayed by the lane.
    async_publication_in_flight: bool,
    wal: ActiveLog,
    wal_retirement: Arc<crate::wal_retirement::WalRetirement>,
    blocks: Arc<BlockStore>,
    blob_chunk_encoder: Arc<crate::blob::ChunkEncoder>,
    vfs: Arc<dyn Vfs>,
    state: ControlState,
    history_manifest: Option<HistoryManifest>,
    payload_manifest: Option<PayloadManifest>,
    committed: Vec<CommittedTransaction>,
    rotation_read_batches: Vec<Arc<Vec<CommittedTransaction>>>,
    // Immutable full history segments prepared while their source WAL is
    // still authoritative. Only checkpoint publishes these references.
    staged_history: Vec<HistorySegmentReference>,
    staged_history_transactions: usize,
    unstaged_history_bytes: usize,
    history_preparation: Option<HistorySegmentEncoder>,
    rotation_threshold_bytes: u64,
    /// Per-step staged-payload materialization budget. The default resolves
    /// the explicit operator override or the generated 8 MiB floor; the
    /// authority applies the probe-derived value at open
    /// (`set_checkpoint_materialize_batch_bytes`).
    pub(crate) checkpoint_materialize_batch_bytes: u64,
    /// Shared restart latch: the writer persona sets it under the authority
    /// mutex; read replicas observe it lock-free (fail-fast hint only —
    /// replica correctness rests on content-verified immutable pages).
    restart_required: std::sync::Arc<std::sync::atomic::AtomicBool>,
    control_dirty: bool,
    background_pack_wait_rounds: u32,
    #[cfg(test)]
    checkpoint_segment_test_ids: Option<std::vec::IntoIter<Uuid>>,
    wal_durability: WalDurability,
    wal_barrier: Option<WalBarrier>,
    wal_barrier_driver: Option<WalBarrierDriver>,
    /// Set while a rotation checkpoint's segment encoding runs outside the
    /// authority mutex (prepare → encode → finish). Synchronous checkpoint
    /// callers refuse with WouldBlock; history preparation freezes so the
    /// staged snapshot stays consistent for the install merge.
    rotation_in_flight: bool,
    /// Final install closes the window until every window payload is durable.
    rotation_install_fenced: bool,
    /// A prepared rotation waiting for the daemon's rotation driver. Present
    /// only when the driver is attached (`attach_rotation_driver`).
    pending_rotation: Option<Box<PreparedRotationCheckpoint>>,
    /// A fully encoded rotation whose install deferred (publication in
    /// flight or covered drain incomplete): parked whole — covered snapshot
    /// AND its encoded segments — so the retry installs without re-encoding
    /// the prefix on every publication round trip (2026-09-20 swarm-p99).
    /// While present, fresh prepares answer Busy and the driver retries
    /// this install first.
    deferred_rotation: Option<(Box<PreparedRotationCheckpoint>, RotationSegmentOutcome)>,
    rotation_driver_attached: bool,
    /// Event-driven wakeup for the rotation driver: preparing a rotation
    /// signals it, replacing the driver's idle poll with an immediate start
    /// (the driver's bounded park stays as the liveness fallback).
    rotation_wakeup: Option<std::sync::Arc<crate::rotation_barrier::RotationBarrier>>,
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

/// Working state for one checkpoint's pending-block materialization:
/// the candidate catalog, staged payloads routed to segments, the spill
/// set routed to loose files, and merged-away segment files to reclaim
/// after the catalog publish is durable.
struct CheckpointPackPlan {
    references: Vec<PayloadSegmentReference>,
    packed: BTreeSet<BlockId>,
    fallback: BTreeSet<BlockId>,
    merged_away: Vec<Uuid>,
    changed: bool,
}

/// Default staged-payload byte budget for one incremental materialization
/// step. Bounding each step's segment writes and fsyncs keeps foreground
/// latency out of the 300 ms range when a large staged backlog accumulates;
/// idle-time steps drain the backlog so the checkpoint path finds little or
/// nothing left to pack. The budget is a soft target: every step routes at
/// least one chunk so repeated calls always make progress.
pub use crate::generated_tofudb_ir::MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES;

/// Operator override for the per-batch payload materialization budget (and
/// therefore the payload segment size target). The generated 8 MiB default
/// bounds live checkpoint commit stalls; an operator running a bulk offline
/// import on a host with real headroom may widen it so segments pack fat
/// instead of filling the bounded catalog with tiny segments. The value is
/// clamped to half one segment's payload budget, read once per process, and
/// anything unparsable falls back to the generated default.
pub fn checkpoint_materialize_batch_bytes() -> u64 {
    static VALUE: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    *VALUE.get_or_init(|| {
        const CEILING: u64 = crate::payload_segment::MAX_SEGMENT_PAYLOAD_BYTES / 2;
        std::env::var("TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES")
            .ok()
            .and_then(|raw| raw.parse::<u64>().ok())
            .filter(|value| *value > 0)
            .map(|value| value.min(CEILING))
            .unwrap_or(MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)
    })
}

/// Bounded wait for one in-flight background segment create before the
/// checkpoint drain abandons the reservation and packs the same staged
/// payloads synchronously (e.g. after a worker death).
use crate::generated_tofudb_ir::{
    BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS, BACKGROUND_PACK_WAIT_ROUND_MILLISECONDS,
};
const BACKGROUND_PACK_WAIT_ROUND: std::time::Duration =
    std::time::Duration::from_millis(BACKGROUND_PACK_WAIT_ROUND_MILLISECONDS);

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
    /// The loose block file is published (renamed into place) but not yet
    /// fsynced; durability arrives with the WAL record that carries its
    /// payload.
    BlockPublished(BlockId),
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
    /// True when the envelope carries its block payloads (version 3) and the
    /// loose files may be written without fsync.
    deferred: bool,
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
        let mut carried_bytes = 0_usize;
        for payload in &self.block_payloads {
            carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
            })?;
        }
        let deferred =
            !self.block_payloads.is_empty() && carried_bytes <= MAX_CARRIED_PAYLOAD_BYTES;
        let envelope = TransactionEnvelope {
            block_ids: block_ids.clone(),
            inline_payload: self.inline_payload,
            authority_state_update: None,
        };
        let encoded_envelope = if deferred {
            let carried = envelope
                .block_ids
                .iter()
                .zip(&self.block_payloads)
                .map(|(_, payload)| Some(payload.as_slice()))
                .collect::<Vec<_>>();
            envelope.encode_with_carried(&carried, None)?
        } else {
            envelope.encode()?
        };
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
            deferred,
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

/// Frozen mark inputs for one online-GC epoch; see
/// `Engine::capture_online_gc_mark_snapshot`.
pub(crate) struct OnlineGcMarkSnapshot {
    pub authority_state_root: Option<BlockId>,
    pub pinned_roots: Vec<BlockId>,
    pub history_manifest_block_id: Option<BlockId>,
    pub history_segments: Vec<HistorySegmentReference>,
    pub history_payload_metadata_ids: Vec<BlockId>,
    pub selected_history_metadata: Option<crate::block::HistoryPayloadMetadataSelection>,
    pub history_metadata_resident_bytes: u64,
    pub staged_history: Vec<HistorySegmentReference>,
    pub committed_block_ids: Vec<BlockId>,
    pub payload_manifest_block_id: Option<BlockId>,
    pub durable_sequence: u64,
    pub generation: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SegmentConsolidationMetrics {
    pub victims: u32,
    pub replacement_segment_id: Option<Uuid>,
    pub blocks_retained: u32,
    pub retired_file_bytes: u64,
    pub replacement_file_bytes: u64,
    pub manifest_block_id: BlockId,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommittedTransaction {
    pub sequence: u64,
    pub record_hash: [u8; 32],
    pub envelope: TransactionEnvelope,
    /// Exact bytes this transaction was durably logged with, carried payload
    /// copies included, so history segments rebuild loose blocks the same way
    /// WAL replay does without a re-encode at checkpoint time.
    pub encoded_envelope: Vec<u8>,
}

/// Borrowed logical metadata; no raw logged-byte ownership is promised.
pub(crate) struct TransactionMetadataRef<'a> {
    pub sequence: u64,
    pub record_hash: [u8; 32],
    pub envelope: &'a TransactionEnvelope,
}

pub(crate) fn acquire_lease(data_dir: &Path, vfs: &dyn Vfs) -> io::Result<Box<dyn VfsFile>> {
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

impl Drop for Engine {
    /// Clean shutdown catches a lagging CONTROL up when it is safe to do so,
    /// restoring the strict fail-closed boundary for identifier-only records
    /// and shortening the next replay. Failure only means the next open
    /// replays the WAL, so the result is intentionally ignored; an ambiguous
    /// earlier publication (restart_required) is never retried here.
    fn drop(&mut self) {
        self.wal.detach_barrier();
        if let Some(driver) = self.wal_barrier_driver.take() {
            let _ = driver.stop_and_join();
        }
        self.wal_barrier = None;
        if self.control_dirty && !self.restart_required.load(std::sync::atomic::Ordering::Relaxed) {
            if let Err(error) = self.flush_dirty_control() {
                // No-panic/no-retry semantics stay: the failure only means
                // the next open replays the WAL, but it is now observable.
                crate::fsync_stats::note_drop_flush_failure();
                crate::engine_log::warn(
                    "drop_flush",
                    &format!("Engine::drop CONTROL flush failed: {error}"),
                );
            }
        }
        if let Err(error) = self.blocks.save_read_hints() {
            crate::engine_log::warn("read_hints", &format!("optional read hints could not be saved: {error}"));
        }
    }
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

    /// Applies the optional at-rest key to an already-built VFS. The real
    /// open path wraps [`RealVfs`] via [`at_rest::authority_vfs`]; the
    /// encrypted fault-injection cell wraps the deterministic simulator so
    /// the same commit/checkpoint/crash cases replay identical operation
    /// traces through the encryption boundary. Returns the encrypting
    /// wrapper so the caller can witness the key identity.
    fn wrap_vfs_with_key(
        data_dir: &Path,
        inner: Arc<dyn Vfs>,
        key: Option<at_rest::AtRestKey>,
    ) -> (Arc<dyn Vfs>, Option<Arc<at_rest::EncryptingVfs>>) {
        match key {
            Some(key) => {
                let encrypting = Arc::new(at_rest::EncryptingVfs::new(inner, data_dir, key));
                let vfs: Arc<dyn Vfs> = encrypting.clone();
                (vfs, Some(encrypting))
            }
            None => (inner, None),
        }
    }

    /// Initializes an authority behind the opt-in at-rest encryption seam.
    /// A supplied key wraps the real VFS and publishes its identity witness;
    /// `None` keeps the plaintext real VFS. The deterministic simulator is
    /// never wrapped, so fault injection is untouched.
    pub fn initialize_with_key(
        data_dir: &Path,
        key: Option<at_rest::AtRestKey>,
    ) -> io::Result<Self> {
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
        let (vfs, encrypting) = Self::wrap_vfs_with_key(data_dir, Arc::new(RealVfs), key);
        let engine = Self::initialize_with_vfs(data_dir, vfs)?;
        // Publish the key-identity witness only after the authority exists,
        // so initialization still starts from an empty directory.
        if let Some(encrypting) = encrypting.as_ref() {
            encrypting.witness_key_identity(true)?;
        }
        Ok(engine)
    }

    /// Like [`Engine::initialize_with_key`] but wraps the supplied VFS
    /// (the deterministic simulator) instead of the real one. The simulator
    /// paths are in-memory absolute paths, so the wrapper's data-dir
    /// prefix classification applies unchanged.
    pub fn initialize_with_vfs_and_key(
        data_dir: &Path,
        inner: Arc<dyn Vfs>,
        key: Option<at_rest::AtRestKey>,
    ) -> io::Result<Self> {
        let (vfs, encrypting) = Self::wrap_vfs_with_key(data_dir, inner, key);
        let engine = Self::initialize_with_vfs(data_dir, vfs)?;
        if let Some(encrypting) = encrypting.as_ref() {
            encrypting.witness_key_identity(true)?;
        }
        Ok(engine)
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
        crate::fastpath::require_classic_authority(data_dir, vfs.as_ref())?;
        let authority = Uuid::new_v4();
        let mut control = ControlFile::initialize_with_vfs(data_dir, authority, Arc::clone(&vfs))?;
        let wal = ActiveLog::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        let blocks = Arc::new(BlockStore::initialize_with_vfs(data_dir, Arc::clone(&vfs))?);
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        let state = control.read_current()?;
        let control = Arc::new(Mutex::new(control));
        Ok(Self {
            data_dir: data_dir.to_owned(),
            _lease: lease,
            control,
            wal,
            wal_retirement: Arc::new(crate::wal_retirement::WalRetirement::new(
                data_dir,
                Arc::clone(&vfs),
                state.active_log_generation,
            )),
            blocks,
            vfs,
            state,
            history_manifest: None,
            payload_manifest: None,
            committed: Vec::new(),
            staged_history: Vec::new(),
            staged_history_transactions: 0,
            history_preparation: None,
            unstaged_history_bytes: 0,
            rotation_threshold_bytes: ACTIVE_LOG_ROTATE_BYTES,
            checkpoint_materialize_batch_bytes: checkpoint_materialize_batch_bytes(),
            restart_required: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            control_dirty: false,
            background_pack_wait_rounds: BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS,
            #[cfg(test)]
            checkpoint_segment_test_ids: None,
            wal_durability: WalDurability::default(),
            wal_barrier: None,
            wal_barrier_driver: None,
            async_publication_in_flight: false,
            rotation_read_batches: Vec::new(),
            rotation_in_flight: false,
            rotation_install_fenced: false,
            pending_rotation: None,
            deferred_rotation: None,
            rotation_driver_attached: false,
            rotation_wakeup: None,
            blob_chunk_encoder: Arc::default(),
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    /// Opens an authority behind the opt-in at-rest encryption seam. A
    /// supplied key wraps the real VFS and witnesses its identity against the
    /// stored `encryption.keyid`; a wrong or absent key fails closed before
    /// any payload is touched. `None` keeps the plaintext real VFS.
    pub fn open_with_key(data_dir: &Path, key: Option<at_rest::AtRestKey>) -> io::Result<Self> {
        let (vfs, encrypting) = Self::wrap_vfs_with_key(data_dir, Arc::new(RealVfs), key);
        if let Some(encrypting) = encrypting.as_ref() {
            encrypting.witness_key_identity(false)?;
        }
        Self::open_with_vfs(data_dir, vfs)
    }

    /// Like [`Engine::open_with_key`] but wraps the supplied VFS (the
    /// deterministic simulator) instead of the real one.
    pub fn open_with_vfs_and_key(
        data_dir: &Path,
        inner: Arc<dyn Vfs>,
        key: Option<at_rest::AtRestKey>,
    ) -> io::Result<Self> {
        let (vfs, encrypting) = Self::wrap_vfs_with_key(data_dir, inner, key);
        if let Some(encrypting) = encrypting.as_ref() {
            encrypting.witness_key_identity(false)?;
        }
        Self::open_with_vfs(data_dir, vfs)
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        if vfs.metadata(data_dir)? != FileKind::Directory {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "tofu-db data path is not a real directory",
            ));
        }
        let lease = acquire_lease(data_dir, vfs.as_ref())?;
        crate::fastpath::require_classic_authority(data_dir, vfs.as_ref())?;
        let mut control = ControlFile::open_with_vfs(data_dir, Arc::clone(&vfs))?;
        let mut state = control.read_current()?;
        // A damaged newer CONTROL slot must not make an older live WAL look
        // authoritative while confirmed successor bytes are silently ignored.
        // Empty prestaged/orphan files are safe; any unreferenced nonempty
        // successor is an ambiguous authority and requires explicit recovery.
        if let Some(next_generation) = state.active_log_generation.checked_add(1) {
            let path = crate::wal::active_log_path(data_dir, next_generation)?;
            match vfs.metadata(&path) {
                Ok(FileKind::File) => {
                    let successor = ActiveLog::open_generation_with_vfs(data_dir, next_generation, Arc::clone(&vfs))?;
                    if !successor.is_empty()? {
                        return Err(io::Error::new(io::ErrorKind::InvalidData,
                            "unreferenced successor WAL contains records; CONTROL outcome is uncertain"));
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
                Ok(_) => return Err(io::Error::new(io::ErrorKind::InvalidData, "successor WAL path is not a file")),
            }
        }
        let mut wal = ActiveLog::open_generation_with_vfs(
            data_dir,
            state.active_log_generation,
            Arc::clone(&vfs),
        )?;
        // Validate the entire acknowledged WAL prefix before opening payload
        // catalogs. Delay even unconfirmed-tail truncation until replay admits
        // all durable references, preserving evidence on any failed open.
        let selected = crate::wal::scan_selected_generation_chain(
            data_dir, &state, &mut wal, Arc::clone(&vfs),
        )?;
        let predecessor_record_count = selected.predecessor_record_count;
        let mut recovery = selected.recovery;
        let blocks = Arc::new(BlockStore::open_with_vfs(data_dir, Arc::clone(&vfs))?);
        let payload_manifest = match state.payload_manifest_block_id {
            Some(manifest_block_id) => {
                let manifest = PayloadManifest::decode(&blocks.get(manifest_block_id)?)?;
                blocks.install_recovery_payload_manifest(manifest.clone())?;
                Some(manifest)
            }
            None => None,
        };
        // Decode one bounded WAL record at a time; retain only IDs, not a
        // second copy of carried payloads. Overflow keeps the conservative
        // per-block recovery path, never drops a WAL reference.
        let mut recovery_ids = BTreeSet::new();
        recovery_ids.extend(state.history_manifest_block_id);
        recovery_ids.extend(state.authority_state_root.flatten());
        for record in &recovery.records {
            if recovery_ids.len() == crate::generated_tofudb_ir::MAX_RECOVERY_PACKED_LOCATIONS { break; }
            // Replay below remains the authority for malformed/ambiguous tails.
            let Ok((envelope, _, _)) = TransactionEnvelope::decode_with_carried(&record.payload) else { break; };
            for id in &envelope.block_ids {
                if recovery_ids.len() < crate::generated_tofudb_ir::MAX_RECOVERY_PACKED_LOCATIONS {
                    recovery_ids.insert(*id);
                }
            }
            if recovery_ids.len() < crate::generated_tofudb_ir::MAX_RECOVERY_PACKED_LOCATIONS {
                if let Some(Some(root)) = envelope.authority_state_update { recovery_ids.insert(root); }
            }
        }
        let mut carried_recovery = if recovery.records.is_empty() {
            // A checkpointed authority has no WAL payloads to classify. Load
            // its roots by verified point reads; catalog size must not tax open.
            Default::default()
        } else {
            blocks.prepare_carried_recovery(recovery_ids)?
        };
        let history_manifest = match state.history_manifest_block_id {
            Some(manifest_block_id) => {
                let manifest = HistoryManifest::decode(&blocks.get_for_recovery(manifest_block_id, &carried_recovery)?)?;
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
        if let Some(manifest) = &history_manifest {
            let routing = manifest.payload_index_root_id.map(|id| {
                crate::history_payload::RoutingRoot::decode(&blocks.get_materialized_shared(id)?)
            }).transpose()?;
            blocks.install_history_payload_catalog(manifest, routing)?;
        }

        let mut keep_records = recovery.records.len();
        let mut committed = Vec::with_capacity(recovery.records.len());
        for (index, record) in recovery.records.iter().enumerate() {
            let (envelope, carried, authority_state_carried) =
                TransactionEnvelope::decode_with_carried(&record.payload)?;
            let mut truncate_here = false;
            for (slot, block_id) in envelope.block_ids.iter().enumerate() {
                match carried.get(slot).and_then(Option::as_ref) {
                    // Reuse a byte-verified published segment copy; otherwise
                    // stage the carried payload without probing loose files.
                    // Restaging an already packed copy would create another
                    // segment on every read-only daemon reopen.
                    Some(payload) => {
                        let restored = blocks.restore_carried_with_lookup(payload, &mut carried_recovery)?;
                        if restored != *block_id {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                "carried payload does not match its block reference",
                            ));
                        }
                    }
                    None => match blocks.get_for_recovery(*block_id, &carried_recovery) {
                        Ok(_) => {}
                        Err(error)
                            if error.kind() == io::ErrorKind::NotFound
                                || error.kind() == io::ErrorKind::InvalidData =>
                        {
                            // Identifier-only references (spilled or
                            // pre-published blocks) have no carried bytes;
                            // a miss beyond the durable point truncates the
                            // ambiguous tail, while a durable transaction
                            // stays fail-closed.
                            if record.sequence > state.durable_sequence {
                                keep_records = index;
                                truncate_here = true;
                                break;
                            }
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                format!(
                                    "durable transaction {} references a missing block",
                                    record.sequence
                                ),
                            ));
                        }
                        Err(error) => return Err(error),
                    },
                }
            }
            if truncate_here {
                break;
            }
            if let Some(Some(root)) = envelope.authority_state_update {
                match authority_state_carried.as_ref() {
                    // Same catalog-aware recovery as ordinary block slots;
                    // loose-file reads stay off the carried recovery path.
                    Some(payload) => {
                        let restored = blocks.restore_carried_with_lookup(payload, &mut carried_recovery)?;
                        if restored != root {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                "carried payload does not match its authority root",
                            ));
                        }
                    }
                    None => match blocks.get_for_recovery(root, &carried_recovery) {
                        Ok(_) => {}
                        Err(error)
                            if error.kind() == io::ErrorKind::NotFound
                                || error.kind() == io::ErrorKind::InvalidData =>
                        {
                            if record.sequence > state.durable_sequence {
                                keep_records = index;
                                break;
                            }
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                format!(
                                    "durable transaction {} references a missing authority root",
                                    record.sequence
                                ),
                            ));
                        }
                        Err(error) => return Err(error),
                    },
                }
                blocks.stage_pending_sync(root)?;
            }
            // Every replayed loose file may predate the last power event, so
            // the next checkpoint re-fsyncs all of them before its CONTROL
            // publication can retire these WAL records.
            for block_id in &envelope.block_ids {
                blocks.stage_pending_sync(*block_id)?;
            }
            committed.push(CommittedTransaction {
                sequence: record.sequence,
                record_hash: record.record_hash,
                envelope,
                encoded_envelope: record.payload.clone(),
            });
        }
        drop(carried_recovery);
        if keep_records != recovery.records.len() || recovery.truncated_bytes > 0 {
            if keep_records < predecessor_record_count {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "sealed WAL replay lost its durable prefix"));
            }
            let end_offset = if keep_records == predecessor_record_count {
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
        // CONTROL's logical root is derived from authenticated transactions.
        // An older catalog-publication bug could persist a current sequence
        // and WAL hash alongside a stale authority root. Replay the already
        // verified active-WAL witness even when the sequence did not advance.
        let recovered_authority_state_root = committed
            .iter()
            .rev()
            .find_map(|transaction| transaction.envelope.authority_state_update.map(Some))
            .unwrap_or(state.authority_state_root);
        if recovered_sequence > state.durable_sequence
            || recovered_authority_state_root != state.authority_state_root
        {
            let recovered_hash = recovery.records.last()
                .map_or(state.root_hash, |record| record.record_hash);
            let next = ControlState {
                generation: state.generation.checked_add(1)
                    .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?,
                durable_sequence: recovered_sequence,
                authority_uuid: state.authority_uuid,
                root_hash: recovered_hash,
                checkpoint_sequence: state.checkpoint_sequence,
                checkpoint_hash: state.checkpoint_hash,
                history_manifest_block_id: state.history_manifest_block_id,
                payload_manifest_block_id: state.payload_manifest_block_id,
                authority_state_root: recovered_authority_state_root,
                active_log_generation: state.active_log_generation,
                sealed_log: state.sealed_log.clone(),
            };
            control.publish(&next)?;
            state = next;
        }
        let control = Arc::new(Mutex::new(control));
        let mut engine = Self {
            data_dir: data_dir.to_owned(),
            _lease: lease,
            control,
            wal,
            wal_retirement: Arc::new(crate::wal_retirement::WalRetirement::new(
                data_dir,
                Arc::clone(&vfs),
                // A sealed WAL is still authoritative. Queue only its own
                // predecessor for cleanup until recovery installs this prefix.
                state.sealed_log.as_ref().map_or(state.active_log_generation, |sealed| sealed.generation),
            )),
            blocks,
            vfs,
            state,
            history_manifest,
            payload_manifest,
            unstaged_history_bytes: committed.iter().map(transaction_history_bytes).sum(),
            committed,
            staged_history: Vec::new(),
            staged_history_transactions: 0,
            history_preparation: None,
            rotation_threshold_bytes: ACTIVE_LOG_ROTATE_BYTES,
            checkpoint_materialize_batch_bytes: checkpoint_materialize_batch_bytes(),
            restart_required: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            control_dirty: false,
            background_pack_wait_rounds: BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS,
            #[cfg(test)]
            checkpoint_segment_test_ids: None,
            wal_durability: WalDurability::default(),
            wal_barrier: None,
            wal_barrier_driver: None,
            async_publication_in_flight: false,
            rotation_read_batches: Vec::new(),
            rotation_in_flight: false,
            rotation_install_fenced: false,
            pending_rotation: None,
            deferred_rotation: None,
            rotation_driver_attached: false,
            rotation_wakeup: None,
            blob_chunk_encoder: Arc::default(),
        };
        // Reopen completes the recorded predecessor before exposing a writer.
        // Successor transactions remain active and are never checkpointed or
        // retired as a side effect of finishing their predecessor.
        if let Some(sealed) = engine.state.sealed_log.clone() {
            let split = engine.committed.partition_point(|transaction| transaction.sequence <= sealed.terminal_sequence);
            let successor = engine.committed.split_off(split);
            engine.unstaged_history_bytes = engine.committed.iter().map(transaction_history_bytes).sum();
            engine.rotation_in_flight = true;
            let snapshot = match engine.prepare_rotation_checkpoint_inner()? {
                RotationCheckpointPreparation::Ready(snapshot) => snapshot,
                _ => return Err(io::Error::other("sealed WAL recovery did not prepare its prefix")),
            };
            engine.committed = successor;
            engine.unstaged_history_bytes = engine.committed.iter().map(transaction_history_bytes).sum();
            let outcome = encode_rotation_segments(&snapshot);
            engine.finish_rotation_checkpoint(snapshot, outcome)?;
        }
        Ok(engine)
    }

    pub fn require_usable_authority(&self) -> io::Result<()> {
        if self.restart_required.load(std::sync::atomic::Ordering::Relaxed) {
            return Err(io::Error::other(
                "tofu-db authority outcome is uncertain; close and reopen the engine",
            ));
        }
        // A failed background WAL barrier is persistent: the pending prefix
        // may never reach stable storage, so every later caller fails closed
        // exactly like an ambiguous foreground barrier.
        if let Some(barrier) = &self.wal_barrier {
            if let Some(error) = barrier.barrier_error() {
                return Err(io::Error::new(
                    error.kind(),
                    format!("tofu-db WAL background barrier failed; close and reopen the engine: {error}"),
                ));
            }
        }
        Ok(())
    }

    fn publish_control(&mut self, next: &ControlState) -> io::Result<()> {
        crate::fsync_stats::note_publish_control();
        // CONTROL must never reference an unsynced WAL prefix. In `Normal`
        // durability the per-commit barrier is deferred to the background
        // flusher, so every publication performs it here first (a no-op in
        // `Strict` mode); a barrier failure is as ambiguous as a publication
        // failure.
        if let Err(error) = self.wal.sync_pending() {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
            return Err(error);
        }
        // Synchronous publishers refuse to queue a CONTROL write behind an
        // unlocked (prepared) publication; callers defer and retry. Busy is
        // not a durability ambiguity, so it never latches restart-required.
        if self.async_publication_in_flight {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "tofu-db catalog publication in flight; defer CONTROL publication",
            ));
        }
        let publish_result = self
            .control
            .lock()
            .map_err(|_| io::Error::other("tofu-db CONTROL lock is poisoned"))
            .and_then(|mut control| control.publish(next));
        if let Err(error) = publish_result {
            // A failed CONTROL write or fsync can have reached stable storage even
            // when the caller observes an error. The in-memory generation must not
            // be used for another publication until recovery selects the authority.
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
            return Err(error);
        }
        Ok(())
    }

    pub fn is_restart_required(&self) -> bool {
        self.restart_required.load(std::sync::atomic::Ordering::Relaxed)
    }

    /// Shared handle to the restart latch for read-replica liveness checks.
    pub(crate) fn restart_flag(&self) -> std::sync::Arc<std::sync::atomic::AtomicBool> {
        std::sync::Arc::clone(&self.restart_required)
    }

    pub fn wal_durability(&self) -> WalDurability {
        self.wal_durability
    }

    pub fn wal_unsynced_bytes(&self) -> u64 {
        self.wal.unsynced_bytes()
    }

    /// Switches the active-log barrier policy. `Strict` is the default and is
    /// the only mode initialization and reopen select implicitly; `Normal`
    /// defers the per-commit barrier to the background flusher and explicit
    /// publication points, capping the acknowledged-loss window at the flush
    /// interval (bounded-RPO consent is a deployment-level decision, never a
    /// silent default).
    pub fn set_wal_durability(&mut self, durability: WalDurability) -> io::Result<()> {
        self.require_usable_authority()?;
        if durability == WalDurability::Strict {
            self.stop_wal_flusher()?;
        }
        self.wal.set_durability(durability);
        self.wal_durability = durability;
        if durability == WalDurability::Strict {
            // Downgrading to strict catches every deferred byte up
            // immediately, restoring the per-commit barrier contract.
            self.sync_wal_pending()?;
        }
        Ok(())
    }

    /// Attaches the shared barrier coordinator. In `Strict` mode the barrier
    /// takes over the per-append fdatasync: commits post their witness and a
    /// caller waits outside the authority mutex, so concurrent commits share
    /// one fsync (group-barrier pipelining). In `Normal` mode the same
    /// barrier bounds the loss window through its driver's idle backstop.
    pub fn attach_wal_barrier(&mut self, barrier: &WalBarrier) -> io::Result<()> {
        self.require_usable_authority()?;
        self.wal.attach_barrier(barrier);
        self.wal_barrier = Some(barrier.clone());
        Ok(())
    }

    /// Starts an engine-owned barrier worker (Normal mode's bounded-RPO
    /// backstop). The worker fdatasyncs the log through an independent
    /// descriptor outside the authority critical section; the engine's own
    /// publication points still sync first, so durability ordering never
    /// depends on the thread.
    pub fn start_wal_flusher(&mut self, interval: std::time::Duration) -> io::Result<()> {
        self.require_usable_authority()?;
        if self.wal_durability != WalDurability::Normal {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WAL flusher requires Normal durability mode",
            ));
        }
        if self.wal_barrier.is_some() {
            return Ok(());
        }
        let (barrier, driver) = WalBarrierDriver::start(
            Arc::clone(&self.vfs),
            self.wal.path().to_path_buf(),
            interval,
        )?;
        self.attach_wal_barrier(&barrier)?;
        self.wal_barrier_driver = Some(driver);
        Ok(())
    }

    pub fn stop_wal_flusher(&mut self) -> io::Result<()> {
        self.wal.detach_barrier();
        if let Some(driver) = self.wal_barrier_driver.take() {
            driver.stop_and_join()?;
        }
        self.wal_barrier = None;
        Ok(())
    }

    /// Current active-log length. Before releasing the authority mutex, pair
    /// it with `WalBarrier::witness` so a concurrent rotation cannot reinterpret
    /// this offset against a different generation.
    pub fn wal_len(&self) -> u64 {
        self.wal.len()
    }

    /// Whether a rotation checkpoint currently owns the rotation lane. The
    /// server layer reads this (with the WAL length) to park appends for the
    /// install window instead of rejecting them.
    pub(crate) fn rotation_in_flight(&self) -> bool {
        self.rotation_in_flight
    }

    /// Test-facing view of the covered generation's staged-block drain.
    #[cfg(test)]
    pub(crate) fn has_rotation_drain_pending_for_test(&self) -> bool {
        self.blocks.has_rotation_drain_pending().unwrap_or(true)
    }

    /// Test-only seam for integration-level rotation tests outside this
    /// module; the field itself stays private so thresholds stay deliberate.
    #[cfg(test)]
    pub(crate) fn publish_payload_manifest_for_test(
        &mut self,
        manifest: PayloadManifest,
    ) -> io::Result<BlockId> {
        self.publish_payload_manifest(manifest, &[])
    }

    pub(crate) fn set_rotation_threshold_bytes_for_test(&mut self, bytes: u64) {
        self.rotation_threshold_bytes = bytes;
    }

    /// The active-log path for the daemon-owned barrier driver.
    pub fn wal_path(&self) -> io::Result<PathBuf> {
        Ok(self.wal.path().to_path_buf())
    }

    pub fn wal_barrier(&self) -> Option<WalBarrier> {
        self.wal_barrier.clone()
    }

    /// Performs the deferred data barrier now; a no-op in `Strict` mode. A
    /// barrier failure is ambiguous and latches restart-required exactly like
    /// a failed foreground sync.
    pub fn sync_wal_pending(&mut self) -> io::Result<u64> {
        match self.wal.sync_pending() {
            Ok(flushed) => Ok(flushed),
            Err(error) => {
                self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
                Err(error)
            }
        }
    }

    /// Deferred commits advance the in-memory durable point without a CONTROL
    /// publication. Structural transitions (WAL rotation, manifest changes)
    /// must first catch the published durable point up in a standalone
    /// ordinary publication so every CONTROL transition stays explicable
    /// under the slot state-machine rules.
    fn flush_dirty_control(&mut self) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_FLUSH_DIRTY);
        if !self.control_dirty {
            return Ok(());
        }
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        self.control_dirty = false;
        Ok(())
    }

    pub(crate) fn online_rotation_blocks_writes(&self) -> bool {
        // Encoding may overlap new commits. Installing retires their WAL too,
        // so close the finite window through indexed publication. Keep
        // the fence even when the driver takes the parked install for retry.
        self.rotation_install_fenced
    }

    /// Start the background payload drain once the active WAL crosses half
    /// the rotation window. The quiet-gated packer otherwise lets ~32 MiB of
    /// staged blocks accumulate while the foreground stays hot, and the
    /// rotation install then waits out that whole backlog as one fenced
    /// multi-second storm at the hard ceiling (2026-09-22 FUSE bench: a
    /// ~1.5 s swarm stall, packer publishing 8 segments/33 MiB inside the
    /// measured window). With the drain requested this early, the paced
    /// packer retires the backlog across the WAL's second half and the
    /// rotation's drain check passes promptly. The flag is idempotent and
    /// clears at install completion; the packer's adaptive pacing still
    /// rate-limits its fsync storm against the foreground.
    const EARLY_DRAIN_WAL_DIVISOR: u64 = 2;

    pub(crate) fn require_write_admission(&self) -> io::Result<()> {
        self.require_usable_authority()?;
        if !self.rotation_in_flight
            && self.wal.len() >= self.rotation_threshold_bytes / Self::EARLY_DRAIN_WAL_DIVISOR
        {
            self.blocks.request_background_drain();
        }
        if self.online_rotation_blocks_writes() {
            // Quiesce writes while the worker drains the covered WAL. Without
            // this fence new staged pages can perpetually overtake rotation.
            return Err(rotation_backpressure());
        }
        Ok(())
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
        self.require_write_admission()?;
        let mut block_ids = Vec::with_capacity(block_payloads.len());
        let mut carried: Vec<Option<Vec<u8>>> = Vec::with_capacity(block_payloads.len());
        let mut carried_bytes = 0_usize;
        for payload in block_payloads {
            let (block_id, pending) = self.blocks.put_deferred(payload)?;
            if pending {
                carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                })?;
                carried.push(Some(payload.to_vec()));
            } else {
                carried.push(None);
            }
            checkpoint(CommitStage::BlockPublished(block_id))?;
            block_ids.push(block_id);
        }
        let envelope = TransactionEnvelope {
            block_ids: block_ids.clone(),
            inline_payload: inline_payload.to_vec(),
            authority_state_update: None,
        };
        let encoded = {
            let _encode_timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_ENCODE);
            if carried_bytes > MAX_CARRIED_PAYLOAD_BYTES {
                // Oversized commits spill: fsync the pending loose blocks now and
                // log identifier-only references, exactly the pre-deferred
                // behavior, instead of growing an unbounded WAL record.
                self.flush_deferred_blocks(&block_ids, None)?;
                envelope.encode()?
            } else if carried_bytes > 0 {
                let borrowed = carried
                    .iter()
                    .map(|slot| slot.as_deref())
                    .collect::<Vec<_>>();
                envelope.encode_with_carried(&borrowed, None)?
            } else {
                envelope.encode()?
            }
        };
        let envelope = encoded;
        let _checkpoint_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CHECKPOINT_IF);
        // Foreground commits never adopt a completed background pack: adoption
        // publishes the payload catalog through the fsync-bearing CONTROL
        // slots, and that publication latency would land on this commit's
        // tail. Adoption is owned by idle maintenance
        // (`publish_completed_pack_if_capacity`), soft-rotation drain, and the
        // checkpoint drain loop; an unadopted pack is durability-neutral
        // because its payloads remain visible from staged copies and are
        // recovered from their carried WAL copies.
        self.checkpoint_if_needed(&envelope)?;
        drop(_checkpoint_timer);
        let sequence = self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })?;
        crate::fsync_stats::note_wal_append();
        let root_hash = {
            let _wal_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL);
            match self.wal.append(sequence, self.state.root_hash, &envelope) {
                Ok(root_hash) => root_hash,
                Err(error) => {
                    // A failed append may have left a complete or torn record on
                    // disk. Recovery must select/truncate that outcome before the
                    // live file can accept another transaction. A hard-ceiling
                    // rejection with a rotation in flight is backpressure, not
                    // corruption.
                    return Err(classify_wal_append_error(
                        self.rotation_in_flight,
                        &self.restart_required,
                        error,
                    ));
                }
            }
        };
        checkpoint(CommitStage::WalDurable(sequence))?;
        // The generation only advances when CONTROL is actually published;
        // deferred commits move durable_sequence/root_hash in memory and let
        // recovery replay the WAL when CONTROL lags, exactly the crash path
        // exercised by the durability-boundary fault tests. Publication is
        // amortized to checkpoint, manifest updates, and explicit flushes.
        let next = ControlState {
            generation: self.state.generation,
            durable_sequence: sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root: self.state.authority_state_root,
            active_log_generation: self.state.active_log_generation,
            sealed_log: self.state.sealed_log.clone(),
        };
        {
            let _tail_timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_COMMIT_TAIL);
            self.state = next;
            self.control_dirty = true;
            self.record_committed(CommittedTransaction {
                sequence,
                record_hash: root_hash,
                envelope: TransactionEnvelope {
                    block_ids: block_ids.clone(),
                    inline_payload: inline_payload.to_vec(),
                    authority_state_update: None,
                },
                encoded_envelope: envelope,
            });
        }
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
        self.require_write_admission()?;
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
        let needs_rotation = self
            .wal
            .batch_would_exceed(&encoded_envelopes, self.rotation_threshold_bytes)?;
        // Validate encoded group admission and hard-cap pressure before staging.
        let must_rotate = self
            .wal
            .batch_would_exceed(&encoded_envelopes, ACTIVE_LOG_MAX_BYTES)?;
        let upcoming_wal_bytes = encoded_envelopes
            .iter()
            .map(|envelope| envelope.len() + WAL_RECORD_OVERHEAD_BYTES)
            .sum();
        self.prepare_history_for_rotation(upcoming_wal_bytes)?;
        if !self.committed.is_empty() && needs_rotation {
            self.drain_staged_then_checkpoint(must_rotate)?;
        }

        for transaction in transactions {
            for payload in &transaction.block_payloads {
                if transaction.deferred {
                    self.blocks.put_deferred(payload)?;
                } else {
                    self.blocks.put(payload)?;
                }
            }
        }

        let first_sequence = self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })?;
        let authority_state_root = transactions.iter().try_fold(
            self.state.authority_state_root,
            |current, transaction| {
                let envelope =
                    TransactionEnvelope::decode_skipping_carried(&transaction.encoded_envelope)?;
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
                    return Err(classify_wal_append_error(
                        self.rotation_in_flight,
                        &self.restart_required,
                        error,
                    ));
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
            generation: self.state.generation,
            durable_sequence: final_sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash: final_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root,
            active_log_generation: self.state.active_log_generation,
            sealed_log: self.state.sealed_log.clone(),
        };
        let mut results = Vec::with_capacity(transactions.len());
        for (index, (transaction, record_hash)) in transactions.iter().zip(hashes).enumerate() {
            let sequence = first_sequence + index as u64;
            let envelope =
                TransactionEnvelope::decode_skipping_carried(&transaction.encoded_envelope)?;
            self.record_committed(CommittedTransaction {
                sequence,
                record_hash,
                envelope,
                encoded_envelope: transaction.encoded_envelope.clone(),
            });
            results.push(CommitResult {
                sequence,
                block_ids: transaction.block_ids.clone(),
            });
        }
        self.state = next;
        self.control_dirty = true;
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

    /// Shared-handle read for hot decoded-page loads; see
    /// `BlockStore::get_shared`. Verification semantics match `read_block`.
    pub(crate) fn read_block_shared(&self, block_id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.require_usable_authority()?;
        self.blocks.get_shared(block_id)
    }

    pub fn write_block(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.require_write_admission()?;
        let (block_id, _) = self.blocks.put_deferred(payload)?;
        Ok(block_id)
    }

    /// Full blob payloads remain WAL-carried unless a verified cached/packed
    /// copy exists. Optional loose-file deduplication never delays this write.
    pub(crate) fn write_blob_block(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.require_write_admission()?;
        let (block_id, _) = self.blocks.put_deferred_blob(payload)?;
        Ok(block_id)
    }

    /// Publish an internally captured immutable payload. Durable/pending
    /// dedup and WAL carrying follow the same path as ordinary blob writes.
    pub(crate) fn write_prepared_blob_block(&self, payload: &crate::block::PreparedBlockPayload) -> io::Result<BlockId> {
        self.require_write_admission()?;
        let (block_id, _) = self.blocks.put_deferred_prepared(payload)?;
        Ok(block_id)
    }

    pub(crate) fn blob_chunk_encoder(&self) -> &crate::blob::ChunkEncoder {
        &self.blob_chunk_encoder
    }

    pub(crate) fn shared_blob_encoder(&self) -> Arc<crate::blob::ChunkEncoder> {
        Arc::clone(&self.blob_chunk_encoder)
    }

    /// Publish a caller-just-encoded block whose content is known new (the
    /// copy-on-write page and root-directory paths). Skips the durable dedup
    /// probe; see `BlockStore::put_deferred_fresh`.
    /// Captured immutable fresh pages retain ordinary admission and WAL staging.
    pub(crate) fn write_block_fresh_prepared(&self, payload: &crate::block::PreparedBlockPayload) -> io::Result<BlockId> {
        self.require_write_admission()?;
        let (id, _) = self.blocks.put_deferred_fresh_prepared(payload)?;
        Ok(id)
    }

    pub(crate) fn write_block_fresh(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.require_write_admission()?;
        let (block_id, _) = self.blocks.put_deferred_fresh(payload)?;
        Ok(block_id)
    }

    /// Owned variant of `write_block_fresh` for encoded pages.
    pub(crate) fn write_block_fresh_owned(&self, payload: Vec<u8>) -> io::Result<BlockId> {
        self.require_write_admission()?;
        let (block_id, _) = self.blocks.put_deferred_fresh_owned(payload)?;
        Ok(block_id)
    }

    /// Materializes and fsyncs every pending staged block in one batch.
    /// Called by oversized commits that spill back to identifier-only
    /// references; the checkpoint path uses `materialize_deferred_blocks`.
    fn flush_deferred_blocks(
        &self, block_ids: &[BlockId], authority_root: Option<BlockId>,
    ) -> io::Result<()> {
        crate::fsync_stats::note_flush_deferred();
        let references = block_ids.iter().copied().chain(authority_root).collect();
        self.blocks.flush_pending_references(&references)
    }

    /// Materializes pending staged blocks before WAL retirement. Sorted blocks
    /// across hash shards share bounded segments, rather than one file per shard.
    /// The extended catalog is durable before staged copies are retired. A full
    /// catalog merges its smallest fitting segment; otherwise blocks
    /// spill to the loose batch along with fsync-only recovery marks.
    fn materialize_deferred_blocks(&mut self) -> io::Result<()> {
        crate::fsync_stats::note_flush_deferred();
        // The checkpoint retires the active WAL right after this call, so
        // every staged payload must reach durable storage here: each step
        // drains everything still owned by the foreground. Payloads owned by
        // an in-flight background pack count as pending until their manifest
        // reference is published; the drain adopts the finished segment,
        // waits (bounded) while its create runs, and falls back to packing
        // the same payloads synchronously once the wait budget runs out.
        let mut wait_rounds = 0_u32;
        loop {
            self.adopt_background_pack()?;
            // A prepared publisher needs the caller to release the authority
            // borrow before adoption can finish. Surface WouldBlock; waiting
            // here would hold the very lock needed to make progress.
            let remaining = self.materialize_deferred_blocks_step(u64::MAX)?;
            if remaining == 0 && !self.blocks.has_pending_payloads()? {
                return Ok(());
            }
            if remaining == 0 {
                wait_rounds += 1;
                if wait_rounds > self.background_pack_wait_rounds {
                    self.blocks.fail_active_background_pack()?;
                    continue;
                }
                self.blocks
                    .wait_for_background_pack(BACKGROUND_PACK_WAIT_ROUND)?;
            }
        }
    }

    /// Publishes a background-packed payload segment, if one finished. Runs
    /// on the commit/checkpoint path or through the capacity-gated idle
    /// maintenance entry point. Returns false when no pack had completed.
    fn adopt_background_pack(&mut self) -> io::Result<bool> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PACK_ADOPT);
        let blocks = Arc::clone(&self.blocks);
        let Some((reference, packed, _publication)) = blocks.take_completed_background_pack()?
        else {
            return Ok(false);
        };
        let existing = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        let mut plan = CheckpointPackPlan {
            references: existing.to_vec(),
            packed: BTreeSet::new(),
            fallback: BTreeSet::new(),
            merged_away: Vec::new(),
            changed: false,
        };
        if plan.references.len() < MAX_PAYLOAD_SEGMENTS {
            plan.references.push(reference);
            plan.packed = packed;
            plan.changed = true;
        } else {
            // Catalog full: the pre-built segment cannot join. Re-route the
            // same staged payloads through the synchronous merge/loose path;
            // orphan GC reclaims the unused background segment file.
            let chunk_ids: Vec<BlockId> = packed.iter().copied().collect();
            self.materialize_checkpoint_chunk(&chunk_ids, reference.payload_bytes, &mut plan)?;
        }
        if plan.changed {
            let manifest = PayloadManifest::new(plan.references)?;
            self.publish_payload_manifest(manifest, &[])?;
            for segment_id in plan.merged_away {
                self.blocks
                    .payload_segment_store()?
                    .defer_segment_retirement(segment_id)?;
            }
            self.blocks.retire_staged_payloads(&plan.packed)?;
        }
        self.blocks.flush_pending_subset(&plan.fallback)?;
        Ok(true)
    }

    /// One idle maintenance publication; a full catalog stays with the
    /// foreground merge/loose fallback rather than extending this round.
    pub(crate) fn publish_completed_pack_if_capacity(&mut self) -> io::Result<bool> {
        self.require_usable_authority()?;
        if self.payload_segment_count() >= MAX_PAYLOAD_SEGMENTS {
            return Ok(false);
        }
        self.adopt_background_pack()
    }

    /// Phase 1 of the mutex-free catalog publication (holds the authority
    /// mutex, performs no fsync): reserves the lane, takes the completed
    /// pack, and precomputes the manifest and the exact CONTROL transition
    /// chain. The caller then runs [`publish_prepared_pack_adoption`]
    /// WITHOUT the authority mutex and finally [`Self::finish_pack_adoption`]
    /// under it. Foreground commits proceed through the whole window; only
    /// CONTROL publishers defer.
    pub(crate) fn try_prepare_pack_adoption(&mut self) -> io::Result<PackAdoptionPreparation> {
        self.require_usable_authority()?;
        if self.async_publication_in_flight {
            return Ok(PackAdoptionPreparation::Busy);
        }
        let Some((reference, packed, publication)) =
            self.blocks.take_completed_background_pack()?
        else {
            return Ok(PackAdoptionPreparation::Empty);
        };
        self.async_publication_in_flight = true;
        // The pack-slot release moves from the guard's Drop to
        // finish_pack_adoption (both outcomes); forgetting the guard is the
        // explicit handoff, and the fault tests pin its release on every
        // return path.
        std::mem::forget(publication);
        let segment_id = reference.segment_id;
        let result = self.prepare_pack_adoption_inner(reference, packed);
        if result.is_err() {
            self.async_publication_in_flight = false;
            let _ = self.blocks.fail_background_pack(segment_id);
        }
        result
    }

    fn prepare_pack_adoption_inner(
        &self,
        reference: PayloadSegmentReference,
        packed: BTreeSet<BlockId>,
    ) -> io::Result<PackAdoptionPreparation> {
        let existing = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        let mut references = existing.to_vec();
        let segment_id = reference.segment_id;
        // Reserve the publication lane using metadata only. Full-catalog
        // merge reads, sealing and retirement belong to the unlocked phase.
        let overflow = if references.len() >= MAX_PAYLOAD_SEGMENTS {
            Some((reference, self.blocks.reserve_background_merge(segment_id)?))
        } else {
            references.push(reference);
            None
        };
        let manifest = PayloadManifest::new(references)?;
        let manifest_bytes = manifest.encode()?;
        let manifest_block_id = BlockId::for_payload(&manifest_bytes);
        // Mirror the synchronous chain exactly: optional dirty catch-up,
        // manifest reference, slot stabilization, each generation+1.
        let mut generation = self.state.generation;
        let catchup = if self.control_dirty {
            generation = generation.checked_add(1).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
            })?;
            Some(ControlState {
                generation,
                ..self.state.clone()
            })
        } else {
            None
        };
        generation = generation.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
        })?;
        let manifest_next = ControlState {
            generation,
            payload_manifest_block_id: Some(manifest_block_id),
            ..self.state.clone()
        };
        generation = generation.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
        })?;
        let stabilize_next = ControlState {
            generation,
            ..manifest_next.clone()
        };
        // Normal durability defers the WAL barrier: this publication must not
        // reference an unsynced prefix, so the publish phase fdatasyncs the
        // log through an independent descriptor (Strict is covered by the
        // per-commit barrier invariant and needs no cover).
        let wal_cover = if self.wal_durability == WalDurability::Normal {
            Some((self.wal.path().to_path_buf(), Arc::clone(&self.vfs)))
        } else {
            None
        };
        Ok(PackAdoptionPreparation::Ready(Box::new(
            PreparedPackAdoption {
                segment_id,
                overflow,
                packed,
                manifest,
                manifest_bytes,
                manifest_block_id,
                catchup,
                manifest_next,
                stabilize_next,
                blocks: Arc::clone(&self.blocks),
                control: Arc::clone(&self.control),
                wal_cover,
            },
        )))
    }

    /// Phase 3 (holds the authority mutex, performs no filesystem I/O): records
    /// the published catalog and reconciles with every commit that advanced
    /// the durable point during the unlocked window — their sequence/root
    /// win and simply mark CONTROL dirty again; they are never rolled back.
    pub(crate) fn finish_pack_adoption(
        &mut self,
        prepared: Box<PreparedPackAdoption>,
        outcome: io::Result<()>,
    ) -> io::Result<bool> {
        self.async_publication_in_flight = false;
        let _ = self.blocks.fail_background_pack(prepared.segment_id);
        match outcome {
            Err(error) => {
                // The CONTROL chain may have partially landed; the in-memory
                // generation is unusable until recovery, exactly like a
                // synchronous publication failure.
                self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
                Err(error)
            }
            Ok(()) => {
                let advanced = self.state.durable_sequence
                    != prepared.stabilize_next.durable_sequence
                    || self.state.root_hash != prepared.stabilize_next.root_hash
                    || self.state.authority_state_root
                        != prepared.stabilize_next.authority_state_root;
                // Publication owns only the physical catalog and CONTROL
                // generation. Every logical field stays with the live state:
                // merging a prepared authority root with a newer WAL sequence
                // would make recovery skip acknowledged entity mutations.
                self.state.generation = prepared.stabilize_next.generation;
                self.state.payload_manifest_block_id =
                    prepared.stabilize_next.payload_manifest_block_id;
                self.control_dirty = advanced;
                self.payload_manifest = Some(prepared.manifest);
                Ok(true)
            }
        }
    }

    /// Incremental checkpoint step: packs staged payloads into payload
    /// segments until roughly `byte_budget` payload bytes have been routed,
    /// publishes the extended catalog atomically through the CONTROL slots,
    /// and retires the packed staged copies. Returns the staged payload
    /// bytes still awaiting materialization. The active WAL is untouched,
    /// so a crash anywhere in the step is recovered from the carried WAL
    /// copies of the not-yet-packed payloads, while packed batches are
    /// durable through their published manifest. Callers chasing a bounded
    /// foreground latency can drain a large staged backlog with repeated
    /// steps before checkpointing.
    pub fn materialize_staged_blocks_step(&mut self, byte_budget: u64) -> io::Result<u64> {
        self.require_usable_authority()?;
        crate::fsync_stats::note_flush_deferred();
        self.materialize_deferred_blocks_step(byte_budget)
    }

    fn materialize_deferred_blocks_step(&mut self, byte_budget: u64) -> io::Result<u64> {
        let (payloads, _) = self.blocks.pending_checkpoint_snapshot()?;
        let total_bytes = payloads
            .iter()
            .map(|(_, payload)| payload.len() as u64)
            .sum::<u64>();
        if payloads.is_empty() {
            // Nothing staged: only fsync-only recovery marks can remain, and
            // those always take the loose path because their files exist.
            self.blocks.flush_pending_subset(&BTreeSet::new())?;
            return Ok(0);
        }
        if self.blocks.payload_segment_store().is_err() {
            // Legacy data directory without the payload-segments tree: the
            // loose batch is the only materialization available.
            self.blocks
                .flush_pending_subset(&payloads.iter().map(|(id, _)| *id).collect())?;
            return Ok(0);
        }
        let mut sorted: Vec<(BlockId, u64)> = payloads
            .iter()
            .map(|(id, payload)| (*id, payload.len() as u64))
            .collect();
        sorted.sort_unstable();
        let existing = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        let mut plan = CheckpointPackPlan {
            references: existing.to_vec(),
            packed: BTreeSet::new(),
            fallback: BTreeSet::new(),
            merged_away: Vec::new(),
            changed: false,
        };
        let mut chunk_ids: Vec<BlockId> = Vec::new();
        let mut chunk_bytes = 0_u64;
        let mut processed_bytes = 0_u64;
        for (id, len) in sorted {
            if !chunk_ids.is_empty()
                && (chunk_ids.len() >= MAX_SEGMENT_BLOCKS
                    || chunk_bytes + len > MAX_SEGMENT_PAYLOAD_BYTES
                    || processed_bytes + chunk_bytes >= byte_budget)
            {
                self.materialize_checkpoint_chunk(&chunk_ids, chunk_bytes, &mut plan)?;
                processed_bytes += chunk_bytes;
                chunk_ids.clear();
                chunk_bytes = 0;
                if processed_bytes >= byte_budget {
                    // Budget spent: leave the remaining sorted payloads
                    // staged; their WAL records still carry the bytes, and
                    // the next step resumes where this one stopped.
                    break;
                }
            }
            chunk_ids.push(id);
            chunk_bytes += len;
        }
        if !chunk_ids.is_empty() {
            self.materialize_checkpoint_chunk(&chunk_ids, chunk_bytes, &mut plan)?;
            processed_bytes += chunk_bytes;
        }
        if plan.changed {
            let manifest = PayloadManifest::new(plan.references)?;
            let _timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_STEP_MANIFEST);
            self.publish_payload_manifest(manifest, &[])?;
            drop(_timer);
            for segment_id in plan.merged_away {
                self.blocks
                    .payload_segment_store()?
                    .defer_segment_retirement(segment_id)?;
            }
            self.blocks.retire_staged_payloads(&plan.packed)?;
        }
        self.blocks.flush_pending_subset(&plan.fallback)?;
        Ok(total_bytes.saturating_sub(processed_bytes))
    }

    #[cfg(test)]
    fn set_checkpoint_segment_ids_for_test(&mut self, ids: Vec<Uuid>) {
        assert!(ids.len() <= 64, "bounded fixture ID sequence");
        self.checkpoint_segment_test_ids = Some(ids.into_iter());
    }

    #[inline]
    fn next_checkpoint_segment_id(&mut self) -> Uuid {
        #[cfg(test)]
        if let Some(ids) = self.checkpoint_segment_test_ids.as_mut() {
            return ids.next().expect("fixture checkpoint ID sequence exhausted");
        }
        Uuid::new_v4()
    }

    /// Routes one chunk of staged payloads: a fresh payload segment while
    /// the catalog has headroom, a merge with the smallest fitting segment once the
    /// catalog is full, or the loose batch when the union would exceed one
    /// segment's bounds. Pending chunk payloads are disjoint from every
    /// published segment because staging only happens when the durable read
    /// path misses.
    fn materialize_checkpoint_chunk(
        &mut self,
        chunk_ids: &[BlockId],
        chunk_payload_bytes: u64,
        plan: &mut CheckpointPackPlan,
    ) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_STEP_CHUNK);
        let can_grow = plan.references.len() < MAX_PAYLOAD_SEGMENTS;
        if can_grow {
            let segment_id = self.next_checkpoint_segment_id();
            let segment = self.blocks.payload_segment_store()?.create(
                segment_id,
                chunk_ids.to_vec(),
                |block_id| self.blocks.get(block_id),
            )?;
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )?;
            self.blocks.verify_payload_segment(&reference)?;
            self.blocks.note_packed(chunk_ids)?;
            plan.references.push(reference);
            plan.packed.extend(chunk_ids.iter().copied());
            plan.changed = true;
            return Ok(());
        }
        if plan.references.is_empty() {
            plan.fallback.extend(chunk_ids.iter().copied());
            return Ok(());
        }
        // Catalog order is hash order, not age or size. Repeatedly merging
        // slot zero rewrites an ever-growing segment under the authority lock.
        // Choose the least historical payload that can fit this chunk using
        // already-validated metadata only (no candidate file reads).
        let candidate = plan.references.iter().enumerate()
            .filter(|(_, reference)| {
                reference.block_count as usize + chunk_ids.len() <= MAX_SEGMENT_BLOCKS
                    && reference.payload_bytes + chunk_payload_bytes <= MAX_SEGMENT_PAYLOAD_BYTES
            })
            .min_by_key(|(_, reference)| (reference.payload_bytes, reference.block_count));
        let Some((merge_index, &oldest)) = candidate else {
            plan.fallback.extend(chunk_ids.iter().copied());
            return Ok(());
        };
        let oldest_segment = self
            .blocks
            .payload_segment_store()?
            .open_segment(oldest.segment_id)?;
        let mut merged_ids: Vec<BlockId> = oldest_segment.block_ids().collect();
        merged_ids.extend_from_slice(chunk_ids);
        merged_ids.sort_unstable();
        // Recovery can restage a block that a published segment already
        // holds (its WAL-carried copy outlives the packed catalog), so the
        // chunk may overlap the oldest segment. Content addressing makes
        // the copies interchangeable; drop the duplicates instead of
        // failing the seal on them.
        merged_ids.dedup();
        let segment_id = self.next_checkpoint_segment_id();
        let segment =
            self.blocks
                .payload_segment_store()?
                .create(segment_id, merged_ids, |block_id| {
                    // A WAL-restaged overlap wins, preserving recovery's
                    // repair semantics. Historical IDs already have an exact
                    // verified segment: never search the whole catalog per ID.
                    if self.blocks.pending_payload(block_id)?.is_some() {
                        return self.blocks.get(block_id);
                    }
                    self.blocks.payload_segment_store()?
                        .get_with_handle(&oldest_segment, block_id)?
                        .ok_or_else(|| io::Error::new(
                            io::ErrorKind::InvalidData,
                            "checkpoint merge source block is missing"))
                })?;
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )?;
        self.blocks.verify_payload_segment(&reference)?;
        // The merged-away segment's IDs were registered when first
        // packed (or by the open-time filter rebuild); only the chunk is new.
        self.blocks.note_packed(chunk_ids)?;
        plan.references[merge_index] = reference;
        plan.merged_away.push(oldest.segment_id);
        plan.packed.extend(chunk_ids.iter().copied());
        plan.changed = true;
        Ok(())
    }

    pub fn block_write_metrics(&self) -> BlockWriteMetrics {
        self.blocks.write_metrics()
    }

    pub(crate) fn block_store(&self) -> &BlockStore {
        &self.blocks
    }

    /// Starts the existing off-authority worker with both payload packing and
    /// this engine's bounded, post-checkpoint WAL retirement handoff.
    pub fn start_background_packer(&self) -> io::Result<crate::background_pack::BackgroundPacker> {
        self.start_background_packer_paced(0)
    }

    /// `pace_bytes_per_second` bounds the packer's segment-write rate (0 =
    /// unpaced); see `BackgroundPacker::start_paced`.
    pub fn start_background_packer_paced(
        &self,
        pace_bytes_per_second: u64,
    ) -> io::Result<crate::background_pack::BackgroundPacker> {
        self.require_usable_authority()?;
        crate::background_pack::BackgroundPacker::start_paced(
            Arc::clone(&self.blocks),
            Some(Arc::clone(&self.wal_retirement)),
            pace_bytes_per_second,
            self.checkpoint_materialize_batch_bytes,
        )
    }

    /// Starts the packer under the adaptive feedback controller (no
    /// operator rate): it tiers the segment-write rate from foreground
    /// commit activity and WAL stage latency, starting at
    /// `adaptive_start_rate` (None = unpaced).
    pub fn start_background_packer_adaptive(
        &self,
        adaptive_start_rate: Option<u64>,
    ) -> io::Result<crate::background_pack::BackgroundPacker> {
        self.require_usable_authority()?;
        crate::background_pack::BackgroundPacker::start_with_pacing(
            Arc::clone(&self.blocks),
            Some(Arc::clone(&self.wal_retirement)),
            None,
            true,
            adaptive_start_rate,
            self.checkpoint_materialize_batch_bytes,
        )
    }

    /// Shared payload store; authority work uses start_background_packer.
    pub fn block_store_shared(&self) -> Arc<BlockStore> {
        Arc::clone(&self.blocks)
    }

    /// Applies the probe-derived per-step materialization budget at
    /// authority open (`AuthorityDatabase::configure_write_path_budgets`).
    /// Bounds match the operator override: 1..=half one segment's payload
    /// budget. Background packers started afterwards inherit the value.
    pub fn set_checkpoint_materialize_batch_bytes(
        &mut self,
        batch_bytes: u64,
    ) -> io::Result<()> {
        const CEILING: u64 = crate::payload_segment::MAX_SEGMENT_PAYLOAD_BYTES / 2;
        if batch_bytes == 0 || batch_bytes > CEILING {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "checkpoint materialize batch must be within 1..=half a segment payload budget",
            ));
        }
        self.checkpoint_materialize_batch_bytes = batch_bytes;
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn set_background_pack_wait_rounds_for_test(&mut self, rounds: u32) {
        self.background_pack_wait_rounds = rounds;
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

    /// Uncaptured active buffer. Exact reads and snapshots also include the
    /// immutable batches retained by an in-flight rotation.
    pub fn committed_transactions(&self) -> &[CommittedTransaction] {
        &self.committed
    }

    pub(crate) fn has_active_transactions(&self) -> bool {
        !self.rotation_read_batches.is_empty() || !self.committed.is_empty()
    }

    fn active_transaction_batches(&self) -> impl Iterator<Item = &[CommittedTransaction]> {
        self.rotation_read_batches.iter().map(|batch| batch.as_slice())
            .chain(std::iter::once(self.committed.as_slice()))
    }

    fn active_transactions(&self) -> impl Iterator<Item = &CommittedTransaction> {
        self.active_transaction_batches().flat_map(|batch| batch.iter())
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
        transactions.extend(self.active_transactions().cloned());
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

    /// Retain every segment containing a transaction at or after the
    /// authority owner's durable replay boundary. Policy cannot prune it.
    pub(crate) fn history_retention_for_replay(&self, maximum: usize, first: u64) -> usize {
        self.history_manifest.as_ref().map_or(maximum, |manifest| {
            maximum.max(manifest.segments.iter().filter(|segment| segment.last_sequence >= first).count())
        })
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

    pub(crate) fn require_physical_maintenance_available(&self) -> io::Result<()> {
        if self.async_publication_in_flight {
            return Err(io::Error::new(io::ErrorKind::WouldBlock,
                "background publication owns materialization"));
        }
        Ok(())
    }

    pub(crate) fn payload_segment_block_ids(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<Vec<BlockId>> {
        self.require_physical_maintenance_available()?;
        self.blocks.verify_payload_segment(reference)?;
        Ok(self
            .blocks
            .payload_segment_store()?
            .open_segment(reference.segment_id)?
            .block_ids()
            .collect())
    }

    pub(crate) fn plan_payload_segment_orphan(&self) -> io::Result<PayloadSegmentOrphanPlan> {
        let mut referenced_ids = self
            .payload_segment_references()
            .iter()
            .map(|reference| reference.segment_id)
            .collect::<BTreeSet<_>>();
        // Background-packed and deferred-retired segments are durable but
        // not manifest-referenced; both stay protected while parked.
        let unpublished = self.blocks.protected_segment_ids()?;
        let skip_temporary = !unpublished.is_empty();
        referenced_ids.extend(unpublished);
        if referenced_ids.len() >= crate::generated_tofudb_ir::MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "payload manifest exceeds orphan scan admission bound",
            ));
        }
        // GC rounds drive retirement progress even when publications pause.
        let _ = self
            .blocks
            .sweep_deferred_segment_retirements(crate::payload_segment::segment_retirement_delay());
        self.blocks
            .payload_segment_store()?
            .plan_orphan_file(&referenced_ids, skip_temporary)
    }

    pub(crate) fn remove_payload_segment_orphan(
        &self,
        candidate: &PayloadSegmentOrphanCandidate,
    ) -> io::Result<u64> {
        self.require_physical_maintenance_available()?;
        if candidate.segment_id.is_none()
            && !self.blocks.unpublished_background_segment_ids()?.is_empty()
        {
            return Err(io::Error::new(io::ErrorKind::WouldBlock,
                "background pack owns temporary segment files"));
        }
        if candidate.segment_id.is_some_and(|segment_id| {
            self.payload_segment_references()
                .iter()
                .any(|reference| reference.segment_id == segment_id)
        }) || candidate.segment_id.is_some_and(|segment_id| {
            self.blocks
                .protected_segment_ids()
                .is_ok_and(|ids| ids.contains(&segment_id))
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

    /// Packs one bounded victim set, publishes its catalog root, stabilizes
    /// both CONTROL fallback slots, and only then reclaims the loose copies.
    pub fn compact_payload_blocks(
        &mut self,
        block_ids: &[BlockId],
    ) -> io::Result<PayloadCompactionMetrics> {
        self.require_usable_authority()?;
        self.require_physical_maintenance_available()?;
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
        let existing_segments = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        if existing_segments.len() >= MAX_PAYLOAD_SEGMENTS {
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
        self.blocks.note_packed(&sorted_ids)?;
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
        self.require_physical_maintenance_available()?;
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
            .defer_segment_retirement(segment_id)?;
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

    /// Eager source-validated snapshot for backup, verification and salvage.
    /// History routing metadata reads complete here before returning inputs.
    /// The online collector uses the explicitly off-authority variant below.
    pub(crate) fn capture_online_gc_mark_snapshot(
        &self,
        pinned_roots: Vec<BlockId>,
    ) -> io::Result<OnlineGcMarkSnapshot> {
        self.capture_online_gc_snapshot_inputs(pinned_roots, None)
    }

    /// GC-only capture: metadata source reads belong to off-authority marks.
    /// Other snapshot consumers retain the original eager validation boundary.
    pub(crate) fn capture_online_gc_mark_snapshot_off_authority(
        &self, pinned_roots: Vec<BlockId>, maximum_snapshot_bytes: u64,
    ) -> io::Result<OnlineGcMarkSnapshot> {
        self.capture_online_gc_snapshot_inputs(pinned_roots, Some(maximum_snapshot_bytes))
    }

    fn capture_online_gc_snapshot_inputs(
        &self, pinned_roots: Vec<BlockId>, maximum_snapshot_bytes: Option<u64>,
    ) -> io::Result<OnlineGcMarkSnapshot> {
        let authority_state_root = self
            .state
            .authority_state_root
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "online GC refuses a legacy CONTROL root",
                )
            })?;
        let committed_block_ids = self
            .committed
            .iter()
            .flat_map(|transaction| transaction.envelope.block_ids.iter().copied())
            .collect();
        let (selected_history_metadata, history_metadata_resident_bytes, history_payload_metadata_ids) =
            match (maximum_snapshot_bytes, self.history_manifest.as_ref()) {
                (Some(budget), Some(manifest)) => {
                    let selected = crate::block::HistoryPayloadMetadataSelection::capture(manifest, budget)?;
                    let bytes = selected.resident_bytes();
                    (Some(selected), bytes, Vec::new())
                }
                (None, Some(manifest)) =>
                    (None, 0, self.blocks.history_payload_metadata_ids(manifest)?),
                (_, None) => (None, 0, Vec::new()),
            };
        Ok(OnlineGcMarkSnapshot {
            authority_state_root,
            pinned_roots,
            history_manifest_block_id: self.state.history_manifest_block_id,
            history_segments: self
                .history_manifest
                .as_ref()
                .map_or_else(Vec::new, |manifest| manifest.segments.clone()),
            history_payload_metadata_ids,
            selected_history_metadata,
            history_metadata_resident_bytes,
            staged_history: self.staged_history.clone(),
            committed_block_ids,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            durable_sequence: self.state.durable_sequence,
            generation: self.state.generation,
        })
    }

    /// Publishes an online segment rewrite whose replacement was built and
    /// verified off-mutex by the online collector (docs/TOFUDB_ONLINE_GC.md).
    /// Re-validates catalog currency and the relink fence under the hold;
    /// contention or any re-reference of an excluded block defers the whole
    /// rewrite with `WouldBlock` so a later epoch retries from fresh marks.
    pub(crate) fn finish_segment_reclaim(
        &mut self,
        segment_id: Uuid,
        expected: &PayloadSegmentReference,
        retained_ids: &[BlockId],
        replacement: Option<PayloadSegmentReference>,
    ) -> io::Result<PayloadSegmentGcMetrics> {
        self.require_usable_authority()?;
        self.require_physical_maintenance_available()?;
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
        if &current.segments[position] != expected {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "payload segment catalog moved during the online rewrite",
            ));
        }
        let retired_segment = self
            .blocks
            .payload_segment_store()?
            .open_segment(segment_id)?;
        let all_block_ids = retired_segment.block_ids().collect::<Vec<_>>();
        let mut retained_ids = retained_ids.to_vec();
        retained_ids.sort_unstable();
        if retained_ids.windows(2).any(|pair| pair[0] == pair[1])
            || retained_ids
                .iter()
                .any(|block_id| all_block_ids.binary_search(block_id).is_err())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "online segment reclaim retained set is not a unique segment subset",
            ));
        }
        if retained_ids.len() == all_block_ids.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "online segment reclaim refuses a fully-live no-op",
            ));
        }
        let fence = self.blocks.relink_fence_snapshot();
        if fence.overflow {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "relink fence overflow; defer the rewrite to the next epoch",
            ));
        }
        let retained_set = retained_ids.iter().copied().collect::<BTreeSet<_>>();
        if all_block_ids
            .iter()
            .filter(|block_id| !retained_set.contains(*block_id))
            .any(|block_id| fence.ids.contains(block_id))
        {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "relinked block inside the reclaim victim; defer to the next epoch",
            ));
        }
        let mut references = current.segments.clone();
        let (replacement_segment_id, replacement_file_bytes) = match replacement {
            Some(reference) => {
                // Mixed-shard segments are legal. Removing the smallest
                // block changes the shard hint; publishing a canonical
                // manifest rebuilds lookup routing for the replacement.
                references[position] = reference;
                (Some(reference.segment_id), reference.file_bytes)
            }
            None => {
                references.remove(position);
                (None, 0)
            }
        };
        let manifest_block_id =
            self.publish_payload_manifest(PayloadManifest::new(references)?, &[])?;
        let retired_file_bytes = self
            .blocks
            .payload_segment_store()?
            .defer_segment_retirement(segment_id)?;
        if retired_file_bytes != expected.file_bytes {
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

    /// Publishes one off-mutex-built consolidation: several small or
    /// dead-heavy segments merge into one replacement, freeing catalog
    /// slots (the 4,096-entry ceiling is a hard data-plane bound).
    /// Re-validates every victim's catalog currency and the relink fence
    /// over the union of excluded blocks under the hold; contention or any
    /// re-reference defers with `WouldBlock` for a later epoch.
    pub(crate) fn finish_segment_consolidation(
        &mut self,
        victims: &[PayloadSegmentReference],
        retained_per_victim: &[Vec<BlockId>],
        replacement: Option<PayloadSegmentReference>,
    ) -> io::Result<SegmentConsolidationMetrics> {
        self.require_usable_authority()?;
        self.require_physical_maintenance_available()?;
        if victims.len() < 2 || victims.len() != retained_per_victim.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "segment consolidation requires at least two matched victims",
            ));
        }
        let current = self
            .payload_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no payload manifest"))?;
        let mut positions = Vec::with_capacity(victims.len());
        for victim in victims {
            let position = current
                .segments
                .iter()
                .position(|reference| reference.segment_id == victim.segment_id)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::NotFound, "payload segment is not current")
                })?;
            if &current.segments[position] != victim {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "payload segment catalog moved during the consolidation",
                ));
            }
            positions.push(position);
        }
        let fence = self.blocks.relink_fence_snapshot();
        if fence.overflow {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "relink fence overflow; defer the consolidation to the next epoch",
            ));
        }
        let store = self.blocks.payload_segment_store()?;
        let mut all_excluded_are_clean = true;
        let mut total_retained = 0_usize;
        for (victim, retained_ids) in victims.iter().zip(retained_per_victim.iter()) {
            let all_block_ids = store.open_segment(victim.segment_id)?.block_ids().collect::<Vec<_>>();
            let mut retained_ids = retained_ids.clone();
            retained_ids.sort_unstable();
            if retained_ids.windows(2).any(|pair| pair[0] == pair[1])
                || retained_ids
                    .iter()
                    .any(|block_id| all_block_ids.binary_search(block_id).is_err())
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "consolidation retained set is not a unique segment subset",
                ));
            }
            let retained_set = retained_ids.iter().copied().collect::<BTreeSet<_>>();
            if all_block_ids
                .iter()
                .filter(|block_id| !retained_set.contains(*block_id))
                .any(|block_id| fence.ids.contains(block_id))
            {
                all_excluded_are_clean = false;
                break;
            }
            total_retained += retained_ids.len();
        }
        if !all_excluded_are_clean {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "relinked block inside a consolidation victim; defer to the next epoch",
            ));
        }
        if replacement.is_some() && total_retained == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "consolidation replacement requires retained blocks",
            ));
        }
        let first = positions[0];
        let mut references = current.segments.clone();
        // Remove victims back-to-front so positions stay valid.
        let mut sorted_positions = positions.clone();
        sorted_positions.sort_unstable_by(|a, b| b.cmp(a));
        for position in sorted_positions {
            references.remove(position);
        }
        if let Some(reference) = replacement {
            references.insert(first.min(references.len()), reference);
        }
        let manifest_block_id =
            self.publish_payload_manifest(PayloadManifest::new(references)?, &[])?;
        let store = self.blocks.payload_segment_store()?;
        let mut retired_file_bytes = 0_u64;
        for victim in victims {
            let bytes = store.remove_segment(victim.segment_id)?;
            if bytes != victim.file_bytes {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "retired payload segment size changed after verification",
                ));
            }
            retired_file_bytes += bytes;
        }
        Ok(SegmentConsolidationMetrics {
            victims: victims.len() as u32,
            replacement_segment_id: replacement.map(|reference| reference.segment_id),
            blocks_retained: total_retained as u32,
            retired_file_bytes,
            replacement_file_bytes: replacement.map_or(0, |reference| reference.file_bytes),
            manifest_block_id,
        })
    }

    /// Publishes an online loose-block compaction whose segment was built
    /// and verified off-mutex. Re-validates catalog capacity under the hold;
    /// loose victims are removed only after the catalog root is durable.
    pub(crate) fn finish_payload_compaction(
        &mut self,
        sorted_ids: &[BlockId],
        built_reference: PayloadSegmentReference,
    ) -> io::Result<PayloadCompactionMetrics> {
        self.require_usable_authority()?;
        self.require_physical_maintenance_available()?;
        if sorted_ids.is_empty() || sorted_ids.len() > MAX_SEGMENT_BLOCKS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction victim count is outside its bounded range",
            ));
        }
        if sorted_ids.windows(2).any(|pair| pair[0] >= pair[1]) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "online compaction victims are not sorted and unique",
            ));
        }
        let existing_segments = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        if existing_segments.len() >= MAX_PAYLOAD_SEGMENTS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction catalog capacity is exhausted",
            ));
        }
        self.blocks.note_packed(sorted_ids)?;
        let mut references = existing_segments.to_vec();
        references.push(built_reference);
        let manifest = PayloadManifest::new(references)?;
        let manifest_block_id = self.publish_payload_manifest(manifest, sorted_ids)?;
        let removed = self.blocks.remove_blocks(sorted_ids)?;
        if removed.blocks_removed != sorted_ids.len() as u64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "payload compaction did not reclaim every loose victim",
            ));
        }
        Ok(PayloadCompactionMetrics {
            segment_id: built_reference.segment_id,
            blocks_packed: sorted_ids.len() as u32,
            payload_bytes: built_reference.payload_bytes,
            segment_file_bytes: built_reference.file_bytes,
            loose_bytes_reclaimed: removed.bytes_removed,
            manifest_block_id,
        })
    }

    fn publish_payload_manifest(
        &mut self,
        manifest: PayloadManifest,
        forbidden_loose_removals: &[BlockId],
    ) -> io::Result<BlockId> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CATALOG_PUBLISH);
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
        self.flush_dirty_control()?;
        {
            let _timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CATALOG_WRITE);
            self.blocks.put_loose(&encoded)?;
        }
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
        self.control_dirty = false;
        let install_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CATALOG_INSTALL);
        if let Err(error) = self.blocks.install_payload_manifest(manifest.clone()) {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
            return Err(error);
        }
        drop(install_timer);
        self.payload_manifest = Some(manifest);
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CATALOG_CONTROL);
        self.stabilize_control_slots()?;
        // Best-effort deferred-retirement sweep: the publication is already
        // durable, so a failed unlink retries on a later round (or as an
        // orphan after a restart) instead of latching restart-required.
        let _ = self
            .blocks
            .sweep_deferred_segment_retirements(crate::payload_segment::segment_retirement_delay());
        Ok(manifest_block_id)
    }

    #[cfg(test)]
    pub(crate) fn visit_retained_transaction_blocks(
        &self,
        visitor: impl FnMut(BlockId) -> io::Result<()>,
    ) -> io::Result<()> {
        self.visit_retained_blocks(true, visitor)
    }

    /// Stable transaction roots during an exclusive GC session. The payload
    /// catalog changes during physical reclamation and is protected separately
    /// on every round; semantic references to the same ID remain visited here.
    pub(crate) fn visit_gc_stable_transaction_blocks(
        &self,
        visitor: impl FnMut(BlockId) -> io::Result<()>,
    ) -> io::Result<()> {
        self.visit_retained_blocks(false, visitor)
    }

    fn visit_retained_blocks(
        &self,
        include_payload_catalog: bool,
        mut visitor: impl FnMut(BlockId) -> io::Result<()>,
    ) -> io::Result<()> {
        self.require_usable_authority()?;
        if let Some(manifest_id) = self.state.history_manifest_block_id {
            visitor(manifest_id)?;
        }
        if include_payload_catalog {
            if let Some(manifest_id) = self.state.payload_manifest_block_id {
                visitor(manifest_id)?;
            }
        }
        if let Some(manifest) = &self.history_manifest {
            for id in self.blocks.history_payload_metadata_ids(manifest)? { visitor(id)?; }
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
        for reference in &self.staged_history {
            visitor(reference.block_id)?;
        }
        for transaction in self.active_transactions() {
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
        self.control_dirty = false;
        Ok(())
    }

    /// Visit an exact contiguous suffix in commit order. Each required history
    /// segment is authenticated and decoded once, with one bounded source/vector
    /// alive at a time; active WAL transactions are borrowed. History scans do
    /// not populate the foreground block cache. Missing/pruned required history
    /// fails closed, and a callback error stops before loading another segment.
    pub(crate) fn visit_transaction_metadata_from<F>(&self, first_sequence: u64, mut visitor: F) -> io::Result<()>
    where F: for<'a> FnMut(TransactionMetadataRef<'a>) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_HISTORY_RANGE);
        self.require_usable_authority()?;
        if first_sequence == 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "transaction suffix must start after sequence zero"));
        }
        let head = self.state.durable_sequence;
        if first_sequence > head { return Ok(()); }
        let mut expected = first_sequence;
        if expected <= self.state.checkpoint_sequence {
            let manifest = self.history_manifest.as_ref().ok_or_else(||
                io::Error::new(io::ErrorKind::InvalidData, "required transaction suffix has no history manifest"))?;
            let start = manifest.segments.partition_point(|reference| reference.last_sequence < expected);
            for reference in &manifest.segments[start..] {
                if reference.first_sequence > expected {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "required transaction suffix history is missing"));
                }
                let source = self.blocks.get_materialized_uncached(reference.block_id)?;
                let transactions = crate::history::decode_segment_views(&source, reference)?;
                for transaction in transactions.iter().filter(|transaction| transaction.sequence >= first_sequence) {
                    if transaction.sequence != expected {
                        return Err(io::Error::new(io::ErrorKind::InvalidData, "required transaction suffix contains a sequence gap"));
                    }
                    visitor(TransactionMetadataRef { sequence: transaction.sequence,
                        record_hash: transaction.record_hash, envelope: &transaction.envelope })?;
                    if expected == head { return Ok(()); }
                    expected += 1; // expected < head, including a possible u64::MAX head.
                }
            }
            if expected <= self.state.checkpoint_sequence {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "required transaction suffix history ends early"));
            }
        }
        for batch in self.active_transaction_batches() {
            let Some(first) = batch.first() else { continue; };
            let offset = usize::try_from(expected.saturating_sub(first.sequence)).map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "active transaction suffix offset overflow")
            })?;
            let Some(suffix) = batch.get(offset..) else { continue; };
            for transaction in suffix {
                if transaction.sequence != expected {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "active transaction suffix contains a sequence gap"));
                }
                visitor(TransactionMetadataRef { sequence: transaction.sequence,
                        record_hash: transaction.record_hash, envelope: &transaction.envelope })?;
                if expected == head { return Ok(()); }
                expected += 1;
            }
        }
        Err(io::Error::new(io::ErrorKind::InvalidData, "active transaction suffix ends early"))
    }

    /// Loads at most one bounded history segment for an exact sequence.
    pub fn transaction_at(&self, sequence: u64) -> io::Result<Option<CommittedTransaction>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_HISTORY_LOOKUP);
        self.require_usable_authority()?;
        if sequence == 0 || sequence > self.state.durable_sequence {
            return Ok(None);
        }
        if sequence > self.state.checkpoint_sequence {
            for batch in self.active_transaction_batches() {
                let Some(first) = batch.first() else { continue; };
                let Some(index) = sequence.checked_sub(first.sequence).and_then(|offset| usize::try_from(offset).ok()) else { continue; };
                if let Some(transaction) = batch.get(index).filter(|transaction| transaction.sequence == sequence) {
                    return Ok(Some(transaction.clone()));
                }
            }
            return Err(io::Error::new(io::ErrorKind::InvalidData, "active transaction sequence is missing"));
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
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CHECKPOINT_TOTAL);
        self.require_usable_authority()?;
        if self.rotation_in_flight {
            // A prepared rotation owns the history pipeline; synchronous
            // callers defer exactly like a catalog publication lane hit.
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "tofu-db rotation checkpoint in flight; defer checkpoint",
            ));
        }
        if self.committed.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "checkpoint requires active committed transactions",
            ));
        }
        // The next publication may retire one more WAL only after its
        // predecessor has been reclaimed. Failed cleanup retains backpressure.
        self.wal_retirement.reclaim()?;
        // A partial encoder is only a cache of WAL-owned bytes. Complete its
        // bounded segment before draining blocks and retiring that source WAL.
        if self.history_preparation.is_some() {
            self.stage_history_segment_step(usize::MAX)?;
        }
        // Retiring the active WAL below makes history segments the only
        // record of these transactions. Segments preserve the exact logged
        // bytes (carried payloads included), but materializing staged blocks
        // first keeps the block store authoritative on its own and bounds
        // how long recovery would need the carried copies.
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_DRAIN);
            self.materialize_deferred_blocks()?;
        }
        self.flush_dirty_control()?;
        crate::fsync_stats::note_checkpoint();
        crate::fsync_stats::note_checkpoint_history_tail(self.unstaged_history_bytes as u64);
        let _segments_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_SEGMENTS);
        let mut new_references = self.staged_history.clone();
        let parent_hash = new_references
            .last()
            .map_or(self.state.checkpoint_hash, |reference| {
                reference.terminal_hash
            });
        let remaining = &self.committed[self.staged_history_transactions..];
        if !remaining.is_empty() {
            visit_segments(remaining, parent_hash, |segment| {
                let block_id = self.blocks.put(&segment.encoded)?;
                new_references.push(HistorySegmentReference {
                    block_id,
                payload_index_block_id: None,
                    first_sequence: segment.first_sequence,
                    last_sequence: segment.last_sequence,
                    parent_hash: segment.parent_hash,
                    terminal_hash: segment.terminal_hash,
                });
                Ok(())
            })?;
        }
        drop(_segments_timer);
        let mut segments = self
            .history_manifest
            .as_ref()
            .map_or_else(Vec::new, |manifest| manifest.segments.clone());
        segments.extend(new_references);
        let manifest = HistoryManifest {
            checkpoint_sequence: self.state.durable_sequence,
            checkpoint_hash: self.state.root_hash,
            payload_index_root_id: self.history_manifest.as_ref().and_then(|manifest| manifest.payload_index_root_id),
            segments,
        };
        let manifest_block_id = {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_MANIFEST);
            self.blocks.put_loose(&manifest.encode()?)?
        };
        let next_active_generation = self
            .state
            .active_log_generation
            .checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "WAL generation overflow"))?;
        let _wal_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_WAL);
        let next_wal = match ActiveLog::initialize_generation_with_vfs(
            &self.data_dir,
            next_active_generation,
            Arc::clone(&self.vfs),
        ) {
            Ok(log) => log,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let mut log = ActiveLog::open_generation_with_vfs(
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
                log.resync_empty_generation(&self.data_dir, self.vfs.as_ref())?;
                log
            }
            Err(error) => return Err(error),
        };
        let previous_wal_generation = self.state.active_log_generation;
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
                sealed_log: None,
            };
        drop(_wal_timer);
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_CONTROL);
            self.publish_control(&next)?;
        }
        self.control_dirty = false;
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_SWAP);
            let mut next_wal = next_wal;
            next_wal.set_durability(self.wal_durability);
            if let Some(barrier) = &self.wal_barrier {
                next_wal.attach_barrier(barrier);
            }
            self.wal = next_wal;
        }
        self.state = next;
        self.history_manifest = Some(manifest);
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_CLEAR);
            self.committed.clear();
            self.staged_history.clear();
        }
        self.staged_history_transactions = 0;
        self.unstaged_history_bytes = 0;
        self.wal_retirement.retire(previous_wal_generation)?;
        self.blocks.wake_background_packer()?;
        Ok(self.state.checkpoint_sequence)
    }

    /// Phase 1 of the mutex-free rotation checkpoint (holds the authority
    /// mutex, performs no large fsync): reserves the rotation lane, finishes
    /// any partial history encoder, and moves the covered prefix out as a
    /// snapshot. Online draining fences new writes; offline/manual callers
    /// can still accumulate window commits for the final segment. The daemon
    /// prepares rotations this way; engine-only users keep the synchronous
    /// soft-rotation checkpoint.
    pub(crate) fn try_prepare_rotation_checkpoint(
        &mut self,
    ) -> io::Result<RotationCheckpointPreparation> {
        self.require_usable_authority()?;
        if self.rotation_in_flight || self.deferred_rotation.is_some() {
            return Ok(RotationCheckpointPreparation::Busy);
        }
        if self.committed.is_empty() {
            return Ok(RotationCheckpointPreparation::Empty);
        }
        if self.rotation_driver_attached {
            if self.async_publication_in_flight
                || (self.blocks.background_packer_running() && !self.wal_retirement.ready_for_rotation()?) {
                return Ok(RotationCheckpointPreparation::Busy);
            }
        }
        if self.rotation_driver_attached && self.blocks.background_packer_running() {
            self.blocks.request_background_drain();
            // Rotate the staged set into the covered generation: the fence
            // and the install defer key off it alone, so window-commit
            // staging during the encode never extends the writer stall
            // (2026-09-20 swarm-p99 root fix).
            self.blocks.rotate_pending_for_rotation()?;
        }
        self.rotation_in_flight = true;
        let result = self.prepare_rotation_checkpoint_inner();
        if result.is_err() {
            self.rotation_in_flight = false;
            let _ = self.blocks.restore_rotation_pending();
        }
        result
    }

    /// Durable ownership handover, before any predecessor encoding. The
    /// successor starts empty and chains from the sealed terminal witness;
    /// no transaction bytes are copied to it.
    fn seal_wal_for_checkpoint(&mut self) -> io::Result<()> {
        let _stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_GENERATION_HANDOVER);
        if self.state.sealed_log.is_some() {
            return Err(rotation_backpressure());
        }
        self.flush_dirty_control()?;
        let generation = self.state.active_log_generation.checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "WAL generation overflow"))?;
        let mut successor = match ActiveLog::initialize_generation_with_vfs(
            &self.data_dir, generation, Arc::clone(&self.vfs),
        ) {
            Ok(log) => log,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let mut log = ActiveLog::open_generation_with_vfs(&self.data_dir, generation, Arc::clone(&self.vfs))?;
                log.resync_empty_generation(&self.data_dir, self.vfs.as_ref())?;
                log
            }
            Err(error) => return Err(error),
        };
        let next = ControlState {
            generation: self.state.generation.checked_add(1)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?,
            active_log_generation: generation,
            sealed_log: Some(crate::control::SealedLog {
                generation: self.state.active_log_generation,
                terminal_sequence: self.state.durable_sequence,
                terminal_hash: self.state.root_hash,
            }),
            ..self.state.clone()
        };
        self.publish_control(&next)?;
        successor.set_durability(self.wal_durability);
        if let Some(barrier) = &self.wal_barrier { successor.attach_barrier(barrier); }
        self.wal = successor;
        self.state = next;
        self.control_dirty = false;
        Ok(())
    }

    fn prepare_rotation_checkpoint_inner(
        &mut self,
    ) -> io::Result<RotationCheckpointPreparation> {
        // Resolve artifact ownership before moving transaction buffers. A
        // sealed installation keeps its current successor; a fresh handover
        // prepares exactly the old active generation's successor.
        let next_active_log_generation = if self.state.sealed_log.is_some() {
            self.state.active_log_generation
        } else {
            self.state.active_log_generation.checked_add(1)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "WAL generation overflow"))?
        };
        // The next publication may retire one more WAL only after its
        // predecessor has been reclaimed. Failed cleanup retains backpressure.
        if !self.rotation_driver_attached || !self.blocks.background_packer_running() {
            self.wal_retirement.reclaim()?;
        }
        // A partial encoder is only a cache of WAL-owned bytes. Complete its
        // bounded segment before the covered prefix moves out.
        if self.history_preparation.is_some() {
            let _hold = crate::authority_watch::AuthorityHoldTimer::probe("rotation.partial");
            self.stage_history_segment_step(usize::MAX)?;
        }
        let parent_hash = self
            .staged_history
            .last()
            .map_or(self.state.checkpoint_hash, |reference| {
                reference.terminal_hash
            });
        if self.rotation_driver_attached && self.state.sealed_log.is_none() {
            let _hold = crate::authority_watch::AuthorityHoldTimer::probe("rotation.handover");
            self.seal_wal_for_checkpoint()?;
        }
        let covered = Arc::new(std::mem::take(&mut self.committed));
        self.rotation_read_batches.push(Arc::clone(&covered));
        Ok(RotationCheckpointPreparation::Ready(Box::new(
            PreparedRotationCheckpoint {
                staged_history: std::mem::take(&mut self.staged_history),
                staged_history_transactions: std::mem::replace(
                    &mut self.staged_history_transactions,
                    0,
                ),
                unstaged_history_bytes: std::mem::replace(
                    &mut self.unstaged_history_bytes,
                    0,
                ),
                covered,
                parent_hash,
                history_manifest_segments: self
                    .history_manifest
                    .as_ref()
                    .map_or_else(Vec::new, |manifest| manifest.segments.clone()),
                history_payload_index_root_id: self.history_manifest.as_ref()
                    .and_then(|manifest| manifest.payload_index_root_id),
                blocks: Arc::clone(&self.blocks),
                data_dir: self.data_dir.clone(),
                vfs: Arc::clone(&self.vfs),
                sealed_log: self.state.sealed_log.clone(),
                next_active_log_generation,
            },
        )))
    }

    /// Phase 3 (holds the authority mutex): online callers defer pending
    /// payloads to the pack worker; offline callers drain synchronously.
    /// Publishes the history
    /// manifest through CONTROL, swaps and retires the WAL — exactly the
    /// synchronous checkpoint tail. On a phase-2 failure the covered prefix
    /// moves back and the authority fails closed, identical to a synchronous
    /// checkpoint failure.
    pub(crate) fn finish_rotation_checkpoint(
        &mut self,
        snapshot: Box<PreparedRotationCheckpoint>,
        outcome: io::Result<RotationSegmentOutcome>,
    ) -> io::Result<u64> {
        let result = self.finish_rotation_checkpoint_inner(snapshot, outcome);
        if let Err(error) = &result {
            if error.kind() != io::ErrorKind::WouldBlock || self.deferred_rotation.is_none() {
                self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
                // No further reads are admitted after a terminal install error.
                // A partially published transition is recovered only from disk;
                // reader pins must not retain the failed snapshot until reopen.
                self.rotation_read_batches.clear();
            }
        }
        result
    }

    fn finish_rotation_checkpoint_inner(
        &mut self,
        snapshot: Box<PreparedRotationCheckpoint>,
        outcome: io::Result<RotationSegmentOutcome>,
    ) -> io::Result<u64> {
        if snapshot.sealed_log.is_some() {
            return self.finish_predecessor_checkpoint(snapshot, outcome);
        }
        self.rotation_in_flight = false;
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                let _ = self.blocks.restore_rotation_pending();
                self.restore_rotation_snapshot(snapshot);
                self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
                return Err(error);
            }
        };
        if self.rotation_driver_attached && self.blocks.background_packer_running() {
            self.rotation_install_fenced = true;
        }
        if self.rotation_driver_attached && self.blocks.background_packer_running()
            && self.async_publication_in_flight
        {
            self.blocks.request_background_drain();
            // A concurrent CONTROL publisher must finish before this CONTROL
            // transition. Pending data itself is owned by indexed history and
            // never requires a packer drain. Retry installs
            // this exact snapshot+outcome instead of re-encoding the prefix
            // on every publication round trip (2026-09-20 swarm-p99). The
            // in-flight flag RESTORES here so history staging stays frozen
            // for the whole parked period — window-era staged segments
            // chaining from the pre-install checkpoint hash orphaned the
            // manifest's hash chain (integrity failure under load).
            self.rotation_in_flight = true;
            self.deferred_rotation = Some((snapshot, outcome));
            return Err(io::Error::new(io::ErrorKind::WouldBlock,
                "background publication owns materialization; rotation deferred"));
        }
        // Every carried byte in the covered prefix has an immutable indexed
        // history copy. Identifier-only references were durable before their
        // commit. The active WAL may retire without a duplicate materialization.
        let PreparedRotationCheckpoint {
            covered,
            staged_history,
            staged_history_transactions,
            unstaged_history_bytes: _,
            parent_hash: _,
            history_manifest_segments,
            blocks: _,
            ..
        } = *snapshot;
        debug_assert!(staged_history_transactions <= covered.len());
        {
            let _stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_DIRTY_CONTROL);
            self.flush_dirty_control()?;
        }
        crate::fsync_stats::note_checkpoint();
        crate::fsync_stats::note_checkpoint_history_tail(self.unstaged_history_bytes as u64);
        // Final small segment covering the window commits (usually tiny).
        let mut outcome = outcome;
        let window = {
            let _capture = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_WINDOW_CAPTURE);
            crate::fsync_stats::note_rotation_window(self.committed.len(), self.unstaged_history_bytes);
            std::mem::take(&mut self.committed)
        };
        if !window.is_empty() {
            let _stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_WINDOW);
            append_rotation_suffix(&self.blocks, &window, self.state.checkpoint_hash, &mut outcome)?;
        }
        let RotationSegmentOutcome { new_references, payload_index_root_id,
            payload_routing, indexed_payload_ids } = outcome;
        let mut segments = history_manifest_segments;
        segments.extend(new_references);
        let manifest = HistoryManifest {
            checkpoint_sequence: self.state.durable_sequence,
            checkpoint_hash: self.state.root_hash,
            payload_index_root_id,
            segments,
        };
        let manifest_block_id = {
            let _stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_MANIFEST);
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_MANIFEST);
            self.blocks.put_loose(&manifest.encode()?)?
        };
        let next_active_generation = self
            .state
            .active_log_generation
            .checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "WAL generation overflow"))?;
        let wal_stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_WAL);
        let next_wal = match ActiveLog::initialize_generation_with_vfs(
            &self.data_dir,
            next_active_generation,
            Arc::clone(&self.vfs),
        ) {
            Ok(log) => log,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let mut log = ActiveLog::open_generation_with_vfs(
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
                log.resync_empty_generation(&self.data_dir, self.vfs.as_ref())?;
                log
            }
            Err(error) => return Err(error),
        };
        drop(wal_stage);
        let previous_wal_generation = self.state.active_log_generation;
        let next = ControlState {
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
            sealed_log: None,
        };
        {
            let _stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_CONTROL);
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_CONTROL);
            self.publish_control(&next)?;
        }
        self.control_dirty = false;
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_SWAP);
            let mut next_wal = next_wal;
            next_wal.set_durability(self.wal_durability);
            if let Some(barrier) = &self.wal_barrier {
                next_wal.attach_barrier(barrier);
            }
            self.wal = next_wal;
        }
        let _release = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_RELEASE);
        self.state = next;
        self.blocks.install_history_payload_catalog(&manifest, payload_routing)?;
        self.blocks.release_history_carried_payloads(&indexed_payload_ids)?;
        self.blocks.restore_rotation_pending()?;
        self.history_manifest = Some(manifest);
        self.staged_history_transactions = 0;
        self.unstaged_history_bytes = 0;
        self.wal_retirement.retire(previous_wal_generation)?;
        self.blocks.wake_background_packer()?;
        self.blocks.finish_background_drain();
        self.rotation_read_batches.clear();
        self.rotation_install_fenced = false;
        Ok(self.state.checkpoint_sequence)
    }

    /// Install only the immutable predecessor. Successor commits keep their
    /// WAL and active buffer, so encoding never creates an admission fence.
    fn finish_predecessor_checkpoint(
        &mut self,
        snapshot: Box<PreparedRotationCheckpoint>,
        outcome: io::Result<RotationSegmentOutcome>,
    ) -> io::Result<u64> {
        let _stage = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_PREDECESSOR_INSTALL);
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                let _ = self.blocks.restore_rotation_pending();
                self.restore_rotation_snapshot(snapshot);
                self.rotation_in_flight = false;
                return Err(error);
            }
        };
        if self.async_publication_in_flight {
            // The publisher owns CONTROL, not the successor WAL. Keep the
            // encoded predecessor parked while successor commits continue;
            // finish_pack_adoption reconciles their newer sequence/root before
            // the retried install publishes the current state. The hard WAL
            // ceiling still bounds writes if the publisher remains stalled.
            self.deferred_rotation = Some((snapshot, outcome));
            return Err(io::Error::new(io::ErrorKind::WouldBlock,
                "background publication owns materialization; rotation deferred"));
        }
        let sealed = snapshot.sealed_log.as_ref().unwrap();
        if self.state.sealed_log.as_ref() != Some(sealed) {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "checkpoint lost its sealed WAL ownership"));
        }
        self.flush_dirty_control()?;
        crate::fsync_stats::note_checkpoint();
        // Zero residual encoding under authority: all successor bytes remain
        // WAL-owned. This metric remains comparable to the legacy install.
        crate::fsync_stats::note_checkpoint_history_tail(0);
        let RotationSegmentOutcome { new_references, payload_index_root_id,
            payload_routing, indexed_payload_ids } = outcome;
        let mut segments = snapshot.history_manifest_segments;
        segments.extend(new_references);
        let manifest = HistoryManifest {
            checkpoint_sequence: sealed.terminal_sequence,
            checkpoint_hash: sealed.terminal_hash,
            payload_index_root_id,
            segments,
        };
        let manifest_block_id = {
            let _timer = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_MANIFEST);
            self.blocks.put_loose(&manifest.encode()?)?
        };
        let next = ControlState {
            generation: self.state.generation.checked_add(1)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?,
            checkpoint_sequence: sealed.terminal_sequence,
            checkpoint_hash: sealed.terminal_hash,
            history_manifest_block_id: Some(manifest_block_id),
            sealed_log: None,
            ..self.state.clone()
        };
        {
            let _timer = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_CONTROL);
            self.publish_control(&next)?;
        }
        self.state = next;
        self.control_dirty = false;
        let _release = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_RELEASE);
        self.blocks.install_history_payload_catalog(&manifest, payload_routing)?;
        self.blocks.release_history_carried_payloads(&indexed_payload_ids)?;
        self.blocks.restore_rotation_pending()?;
        self.history_manifest = Some(manifest);
        self.wal_retirement.retire(sealed.generation)?;
        self.blocks.wake_background_packer()?;
        self.blocks.finish_background_drain();
        self.rotation_read_batches.clear();
        self.rotation_in_flight = false;
        self.rotation_install_fenced = false;
        Ok(self.state.checkpoint_sequence)
    }

    fn restore_rotation_snapshot(&mut self, snapshot: Box<PreparedRotationCheckpoint>) {
        let PreparedRotationCheckpoint {
            covered,
            staged_history,
            staged_history_transactions,
            unstaged_history_bytes,
            ..
        } = *snapshot;
        self.rotation_read_batches.clear();
        // Only diagnostic/test retention can leave another Arc at rollback.
        let mut committed = Arc::try_unwrap(covered).unwrap_or_else(|shared| (*shared).clone());
        committed.extend(self.committed.drain(..));
        self.committed = committed;
        self.staged_history = staged_history;
        self.staged_history_transactions = staged_history_transactions;
        self.unstaged_history_bytes += unstaged_history_bytes;
    }

    /// Marks the engine's soft-rotation driver attachment: when present, the
    /// soft-rotation path prepares an async rotation instead of running the
    /// synchronous checkpoint; when absent (engine-only users, tests), the
    /// Real publication handoff fixture for admission/deadline tests. Uses the
    /// production pack reservation, durable segment and CONTROL preparation;
    /// never manufactures a fence by changing private state flags.
    #[cfg(test)]
    pub(crate) fn prepare_pack_publication_for_test(&mut self) -> Box<PreparedPackAdoption> {
        self.write_block(b"test publication handoff").unwrap();
        let (segment_id, reserved) = self.blocks.begin_background_pack(1, checkpoint_materialize_batch_bytes()).unwrap().unwrap();
        let segment = self.blocks.payload_segment_store().unwrap()
            .create(segment_id, reserved.clone(), |id| self.blocks.get(id)).unwrap();
        let reference = PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap();
        self.blocks.verify_payload_segment(&reference).unwrap();
        self.blocks.note_packed(&reserved).unwrap();
        self.blocks.complete_background_pack(segment_id, reference).unwrap();
        match self.try_prepare_pack_adoption().unwrap() {
            PackAdoptionPreparation::Ready(prepared) => prepared,
            _ => panic!("completed pack must prepare a CONTROL publication"),
        }
    }

    /// established synchronous path is unchanged.
    pub(crate) fn attach_rotation_driver(&mut self) {
        self.rotation_driver_attached = true;
    }

    /// Registers the wakeup barrier signaled every time a rotation becomes
    /// ready for the driver (see `rotation_wakeup`).
    pub(crate) fn attach_rotation_wakeup(
        &mut self,
        wakeup: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
    ) {
        self.rotation_wakeup = Some(wakeup);
    }

    fn signal_rotation_wakeup(&self) {
        if let Some(wakeup) = &self.rotation_wakeup {
            wakeup.signal_install();
        }
    }

    /// Hands a prepared rotation to the daemon's rotation driver.
    /// Work handed to the rotation driver: a fresh snapshot to encode, or a
    /// parked (snapshot, encoded-segments) pair to install directly.
    pub(crate) fn take_prepared_rotation(
        &mut self,
    ) -> io::Result<Option<RotationWork>> {
        if let Some((snapshot, outcome)) = self.deferred_rotation.take() {
            // The in-flight guard travels with the retry: handing the parked
            // pair out without it let a second rotation prepare while the
            // first was mid-install (double covered-prefix installs violated
            // history sequencing, 2026-09-20). Already true while parked;
            // pinned here so the guard can never be lost on this path.
            self.rotation_in_flight = true;
            return Ok(Some(RotationWork::Deferred(snapshot, outcome)));
        }
        // A drain must progress even when every writer is parked at the WAL
        // ceiling: it cannot rely on another successful foreground append.
        if self.pending_rotation.is_none() && self.blocks.background_drain_requested() {
            if let RotationCheckpointPreparation::Ready(prepared) =
                self.try_prepare_rotation_checkpoint()?
            {
                self.pending_rotation = Some(prepared);
            }
        }
        Ok(self.pending_rotation.take().map(RotationWork::Fresh))
    }

    /// Publishes a suffix-only history manifest without touching the active WAL.
    /// The immutable retired segments remain available to retained backups until
    /// their explicit GC; normal open reads only this new bounded manifest.
    pub fn compact_history(&mut self, maximum_retained_segments: usize) -> io::Result<HistoryCompactionMetrics> {
        match self.try_prepare_history_compaction(maximum_retained_segments)? {
            Some(prepared) => {
                let outcome = encode_history_compaction(&prepared);
                self.finish_history_compaction(prepared, outcome)
            }
            None => {
                let current = self.history_manifest.as_ref().ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "history is empty"))?;
                Ok(HistoryCompactionMetrics { retained_first_sequence: current.segments[0].first_sequence,
                    retained_segments: current.segments.len() as u32, retired_segments: 0 })
            }
        }
    }

    fn checkpoint_if_needed(&mut self, encoded_transaction: &[u8]) -> io::Result<()> {
        let needs_rotation = self
            .wal
            .would_exceed(encoded_transaction, self.rotation_threshold_bytes)?;
        let must_rotate = self
            .wal
            .would_exceed(encoded_transaction, ACTIVE_LOG_MAX_BYTES)?;
        self.prepare_history_for_rotation(encoded_transaction.len() + WAL_RECORD_OVERHEAD_BYTES)?;
        if !self.committed.is_empty() && needs_rotation {
            self.drain_staged_then_checkpoint(must_rotate)?;
        }
        Ok(())
    }

    fn record_committed(&mut self, transaction: CommittedTransaction) {
        // Active WAL admission bounds the aggregate well below usize::MAX.
        self.unstaged_history_bytes += transaction_history_bytes(&transaction);
        self.committed.push(transaction);
    }

    fn prepare_history_for_rotation(&mut self, upcoming_wal_bytes: usize) -> io::Result<()> {
        // A single pending history segment gives the worker bounded memory,
        // but it needs the full WAL window to drain between foreground calls.
        // Starting only near rotation would pile the unprepared suffix back
        // onto checkpoint. Short prefixes below one segment still do no work.
        if self.blocks.background_packer_running() {
            return self.stage_full_history_segment();
        }
        // Preparation writes one history segment while each foreground commit
        // adds its own WAL bytes. Start with enough room for that arriving
        // work, using the current request rather than a fixed early window.
        // One full segment of minimum headroom absorbs framing/packing slack;
        // a large request starts earlier, up to the whole rotation window.
        // (Two segments of lead was measured and reverted: p50 cost with no
        // p99 gain — pacing already keeps the encoder empty in practice.)
        let preparation_lead = self
            .rotation_threshold_bytes
            .saturating_mul(upcoming_wal_bytes as u64)
            .div_ceil(HISTORY_SEGMENT_MAX_BYTES as u64)
            .max(HISTORY_SEGMENT_MAX_BYTES as u64)
            .min(self.rotation_threshold_bytes);
        let preparation_start = self.rotation_threshold_bytes - preparation_lead;
        if self.history_preparation.is_some()
            || !self.staged_history.is_empty()
            || self.unstaged_history_bytes as u64 >= preparation_start
        {
            self.stage_full_history_segment()?;
        }
        Ok(())
    }

    /// With a live packer, hash/copy toward 256 KiB per commit while retaining
    /// one at-most-4-MiB encoder or pending history block. Large records remain
    /// indivisible. Worker absence finishes a full segment synchronously;
    /// checkpoint always finishes a partial encoder before retiring its WAL.
    /// Deficit pacing bounds one preparation step: above the cheap fixed
    /// floor, each commit stages a bounded share of the outstanding deficit
    /// so multi-writer bursts reach rotation fully pre-staged instead of
    /// draining tens of MiB synchronously there. Capped well below the
    /// 4 MiB segment so no single commit pays a rotation-sized stall.
    const PREPARATION_DEFICIT_DIVISOR: usize = 8;
    const PREPARATION_MAX_STEP_BYTES: usize = 1024 * 1024;

    fn stage_full_history_segment(&mut self) -> io::Result<()> {
        if self.rotation_in_flight {
            // Frozen while a rotation's covered snapshot owns the history
            // pipeline; window commits queue into the install's final
            // segment instead of being staged twice.
            return Ok(());
        }
        let byte_target = if self.blocks.background_packer_running() {
            self.unstaged_history_bytes
                .saturating_div(Self::PREPARATION_DEFICIT_DIVISOR)
                .clamp(
                    crate::generated_tofudb_ir::HISTORY_PREPARATION_TARGET_BYTES,
                    Self::PREPARATION_MAX_STEP_BYTES,
                )
        } else {
            usize::MAX
        };
        self.stage_history_segment_step(byte_target)
    }

    fn stage_history_segment_step(&mut self, byte_target: usize) -> io::Result<()> {
        if self.unstaged_history_bytes < HISTORY_SEGMENT_MAX_BYTES {
            return Ok(());
        }
        if let Some(reference) = self.staged_history.last() {
            if self.blocks.pending_payload(reference.block_id)?.is_some() {
                return Ok(());
            }
        }
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_HISTORY_STAGE);
        // Full greedy segments contain at least half the maximum. The active
        // WAL ceiling bounds their unpublished metadata independently of steps.
        if self.staged_history.len() >= MAX_STAGED_HISTORY_SEGMENTS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "staged history limit exceeded",
            ));
        }
        let remaining = &self.committed[self.staged_history_transactions..];
        if self.history_preparation.is_none() {
            let parent_hash = self
                .staged_history
                .last()
                .map_or(self.state.checkpoint_hash, |reference| {
                    reference.terminal_hash
                });
            self.history_preparation = Some(HistorySegmentEncoder::new(remaining, parent_hash)?);
        }
        let encoder = self
            .history_preparation
            .as_mut()
            .expect("preparation encoder was initialized above");
        if !encoder.advance(remaining, byte_target)? {
            return Ok(());
        }
        let encoder = self
            .history_preparation
            .take()
            .expect("preparation encoder survives advance");
        let count = encoder.transaction_count();
        let segment = encoder.finish()?;
        let block_id = if self.blocks.background_packer_running() {
            self.blocks.stage_checkpoint_history(segment.encoded)?
        } else {
            self.blocks.put(&segment.encoded)?
        };
        self.unstaged_history_bytes -= remaining[..count]
            .iter()
            .map(transaction_history_bytes)
            .sum::<usize>();
        self.staged_history_transactions += count;
        self.staged_history.push(HistorySegmentReference {
            block_id,
                payload_index_block_id: None,
            first_sequence: segment.first_sequence,
            last_sequence: segment.last_sequence,
            parent_hash: segment.parent_hash,
            terminal_hash: segment.terminal_hash,
        });
        Ok(())
    }

    /// Bounds the foreground cost of a WAL rotation. A full checkpoint first
    /// materializes every staged block; a large backlog made the triggering
    /// commit stall for hundreds of milliseconds. Running one bounded
    /// incremental step per commit spreads that work across the commits that
    /// cross the soft threshold, and the checkpoint itself (which retires
    /// the active WAL) runs only once the backlog is empty and its
    /// materialize step is a no-op. The active WAL grows past the soft
    /// rotation threshold while draining; every transaction in it stays
    /// recoverable from its carried payload copies until the checkpoint
    /// retires it, so durability semantics are unchanged.
    fn drain_staged_then_checkpoint(&mut self, must_rotate: bool) -> io::Result<()> {
        if must_rotate {
            if self.rotation_driver_attached {
                // With the rotation driver attached the hard ceiling becomes
                // classified retryable backpressure instead of a synchronous
                // checkpoint stall: prepare the async rotation if none is in
                // flight, let the driver complete it, and reject this append
                // (before any mutation) so its client retries idempotently.
                // A rejected append adds no WAL bytes, so the ceiling stays
                // intact until the install swaps generations.
                if !self.rotation_in_flight {
                    if let RotationCheckpointPreparation::Ready(prepared) =
                        self.try_prepare_rotation_checkpoint()?
                    {
                        self.pending_rotation = Some(prepared);
                        self.signal_rotation_wakeup();
                    }
                }
                return Err(rotation_backpressure());
            }
            // A bounded step may leave a backlog larger than the remaining WAL
            // headroom. Finish the durable handoff before admitting this append.
            let _timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_MATERIALIZE);
            self.checkpoint()?;
            return Ok(());
        }
        if self.rotation_driver_attached {
            self.blocks.request_background_drain();
            if let RotationCheckpointPreparation::Ready(prepared) =
                self.try_prepare_rotation_checkpoint()?
            {
                self.pending_rotation = Some(prepared);
                self.signal_rotation_wakeup();
                return Err(rotation_backpressure());
            }
            return Ok(());
        }
        self.adopt_background_pack()?;
        // Soft rotation must not collapse incremental history preparation
        // back into one foreground encode-and-pack stall. Allow its existing
        // worker to publish the completed segment; hard admission above still
        // finishes everything synchronously before exceeding the WAL ceiling.
        if self.history_preparation.is_some() {
            return Ok(());
        }
        if self.blocks.background_packer_running() {
            if let Some(reference) = self.staged_history.last() {
                if self.blocks.pending_payload(reference.block_id)?.is_some() {
                    return Ok(());
                }
            }
        }
        let drained = {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_DRAIN_STEP);
            match self.materialize_deferred_blocks_step(self.checkpoint_materialize_batch_bytes) {
                Ok(drained) => drained,
                // The adoption driver's in-flight publication owns the
                // pending set this round; the next commit retries.
                Err(error) if is_publication_owns_materialization(&error) => {
                    return Ok(())
                }
                Err(error) => return Err(error),
            }
        };
        // The foreground snapshot excludes background reservations, including
        // completed files whose catalog publication is still pending. They
        // must not turn a soft threshold into a blocking checkpoint.
        if drained == 0 && !self.blocks.has_pending_payloads()? {
            let _timer = crate::fsync_stats::StageTimer::new(
                crate::fsync_stats::STAGE_CK_MATERIALIZE,
            );
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
        self.require_write_admission()?;
        // Referenced blocks may still be pending (staged without fsync by
        // write_block/put_deferred). Carrying their payloads in the WAL
        // record keeps the record authoritative across a crash that loses
        // the unsynced loose files; blocks that are already durable stay
        // identifier-only. Hold shared handles until encoding completes rather
        // than deep-copying every COW page and authority root into temporary
        // buffers. These handles are transaction-local, not cache entries;
        // the existing carried-byte admission and spill bounds still apply.
        let _carried_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CARRIED);
        let mut carried: Vec<Option<Arc<Vec<u8>>>> = Vec::with_capacity(block_ids.len());
        let mut carried_bytes = 0_usize;
        for block_id in block_ids {
            match self.blocks.pending_payload(*block_id)? {
                Some(payload) => {
                    carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                    })?;
                    carried.push(Some(payload));
                }
                None => {
                    // Already-durable references stay identifier-only; the
                    // read preserves commit-time detection of a missing
                    // block.
                    self.blocks.get(*block_id)?;
                    carried.push(None);
                }
            }
        }
        let mut authority_state_carried = None;
        // Entity commits already include their final root in block_ids. Replay
        // restores those slots before resolving the authority root, so carrying
        // that same immutable page again buys no durability. Keep the root
        // identifier/witness, but charge and encode its bytes only once. A root
        // outside block_ids still needs its independent carried slot.
        if let Some(Some(root)) = authority_state_update
            .filter(|root| root.is_some_and(|root| !block_ids.contains(&root)))
        {
            match self.blocks.pending_payload(root)? {
                Some(payload) => {
                    carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                    })?;
                    authority_state_carried = Some(payload);
                }
                None => {
                    self.blocks.get(root)?;
                }
            }
        }
        drop(_carried_timer);
        let envelope = TransactionEnvelope {
            block_ids: block_ids.to_vec(),
            inline_payload: inline_payload.to_vec(),
            authority_state_update,
        };
        if carried_bytes > MAX_CARRIED_PAYLOAD_BYTES {
            // Oversized references spill: fsync the pending loose blocks now
            // and log identifier-only references instead of growing an
            // unbounded WAL record.
            self.flush_deferred_blocks(block_ids, authority_state_update.flatten())?;
            self.commit_envelope(envelope, Vec::new(), None)
        } else {
            self.commit_envelope(envelope, carried, authority_state_carried)
        }
    }

    fn commit_envelope(
        &mut self,
        envelope: TransactionEnvelope,
        carried: Vec<Option<Arc<Vec<u8>>>>,
        authority_state_carried: Option<Arc<Vec<u8>>>,
    ) -> io::Result<CommitResult> {
        let encode_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_ENCODE);
        let encoded = if carried.iter().any(Option::is_some) || authority_state_carried.is_some() {
            let borrowed = carried
                .iter()
                .map(|slot| slot.as_deref().map(Vec::as_slice))
                .collect::<Vec<_>>();
            envelope.encode_with_carried(
                &borrowed,
                authority_state_carried.as_deref().map(Vec::as_slice),
            )?
        } else {
            envelope.encode()?
        };
        drop(encode_timer);
        let _checkpoint_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CHECKPOINT_IF);
        // Foreground commits never adopt a completed background pack: see the
        // commit path above for the ownership/durability contract.
        self.checkpoint_if_needed(&encoded)?;
        drop(_checkpoint_timer);
        let sequence = self.next_sequence()?;
        let _wal_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL);
        let root_hash = match self.wal.append(sequence, self.state.root_hash, &encoded) {
            Ok(root_hash) => root_hash,
            Err(error) => {
                // Reference commits use pre-published immutable blocks, but
                // their WAL append has the same ambiguous complete/torn-tail
                // outcome as every other commit path. Recovery must select it
                // before this authority can assign another sequence. A
                // hard-ceiling rejection with a rotation in flight is
                // backpressure, not corruption.
                return Err(classify_wal_append_error(
                    self.rotation_in_flight,
                    &self.restart_required,
                    error,
                ));
            }
        };
        let next = ControlState {
            generation: self.state.generation,
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
            sealed_log: self.state.sealed_log.clone(),
        };
        let _tail_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_COMMIT_TAIL);
        self.state = next;
        self.control_dirty = true;
        self.record_committed(CommittedTransaction {
            sequence,
            record_hash: root_hash,
            envelope: envelope.clone(),
            encoded_envelope: encoded,
        });
        Ok(CommitResult {
            sequence,
            block_ids: envelope.block_ids,
        })
    }
}

/// Frozen covered prefix of a rotation checkpoint. The bulk segment
/// encoding runs against this snapshot WITHOUT the authority mutex while
/// new commits flow into a fresh buffer. Online rotations seal the predecessor
/// before capture and leave successor commits WAL-owned. Manual rotations
/// retain the legacy final-window checkpoint semantics.
pub(crate) struct PreparedRotationCheckpoint {
    covered: Arc<Vec<CommittedTransaction>>,
    staged_history: Vec<HistorySegmentReference>,
    staged_history_transactions: usize,
    unstaged_history_bytes: usize,
    parent_hash: [u8; 32],
    history_manifest_segments: Vec<HistorySegmentReference>,
    history_payload_index_root_id: Option<BlockId>,
    blocks: Arc<BlockStore>,
    /// Install-time artifact coordinates captured under the prepare hold so
    /// the rotation driver can pre-stage them outside the authority mutex:
    /// the next-generation WAL file create+dir-sync would otherwise land in
    /// the install's fsync chain (2026-09-22 FUSE: ~460 ms install hold).
    data_dir: std::path::PathBuf,
    vfs: Arc<dyn crate::vfs::Vfs>,
    next_active_log_generation: u64,
    sealed_log: Option<crate::control::SealedLog>,
}

/// Pre-create the next-generation WAL file off the authority mutex. The
/// install's `AlreadyExists` arm reuses an empty pre-staged file, so this is
/// idempotent; failures only mean the install pays the create in-hold, as
/// before (logged and tolerated by the driver).
pub(crate) fn prestage_rotation_wal(prepared: &PreparedRotationCheckpoint) -> io::Result<()> {
    if prepared.sealed_log.is_some() { return Ok(()); }
    match crate::wal::ActiveLog::initialize_generation_with_vfs(
        &prepared.data_dir,
        prepared.next_active_log_generation,
        Arc::clone(&prepared.vfs),
    ) {
        Ok(_) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(error),
    }
}

/// Whether an error is the background pack's in-flight publication owning
/// the pending set; opportunistic foreground work defers this round. A
/// synchronous drain must return WouldBlock so adoption can acquire authority.
fn is_publication_owns_materialization(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::WouldBlock
        && error.to_string().contains("publication owns materialization")
}

/// Classified retryable backpressure for a hard-ceiling append rejected
/// while a rotation checkpoint is in flight. Maps to `database_busy` with
/// the retryable flag on the wire; the command carries no mutation yet, so
/// a client retry is naturally idempotent.
pub(crate) fn rotation_backpressure() -> io::Error {
    io::Error::new(
        io::ErrorKind::ResourceBusy,
        "tofu-db: rotation checkpoint in flight; retry after install",
    )
}

pub(crate) fn is_rotation_backpressure(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::ResourceBusy
        && (error
            .to_string()
            .contains("rotation checkpoint in flight")
            || error.to_string().contains("active WAL rotation required"))
}

/// Classifies a WAL append failure: with a rotation in flight the hard
/// ceiling is expected backpressure (the driver completes the rotation and
/// the retried command proceeds), never restart-required corruption.
fn classify_wal_append_error(
    rotation_in_flight: bool,
    restart_required: &std::sync::Arc<std::sync::atomic::AtomicBool>,
    error: io::Error,
) -> io::Error {
    if rotation_in_flight && error.kind() == io::ErrorKind::ResourceBusy {
        return rotation_backpressure();
    }
    restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
    error
}

/// Rotation work handed to the driver (see
/// [`Engine::take_prepared_rotation`]): `Fresh` needs
/// [`encode_rotation_segments`]; `Deferred` installs the parked pair
/// directly — never re-encode a parked rotation.
pub(crate) enum RotationWork {
    Fresh(Box<PreparedRotationCheckpoint>),
    Deferred(Box<PreparedRotationCheckpoint>, RotationSegmentOutcome),
}

/// Outcome of [`Engine::try_prepare_rotation_checkpoint`].
#[derive(Debug)]
pub(crate) enum RotationCheckpointPreparation {
    /// Nothing committed is waiting to checkpoint.
    Empty,
    /// Another rotation (or its lane) is already in flight.
    Busy,
    /// The covered prefix is frozen and ready for unlocked encoding.
    Ready(Box<PreparedRotationCheckpoint>),
}

impl std::fmt::Debug for PreparedRotationCheckpoint {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PreparedRotationCheckpoint")
            .field("covered", &self.covered.len())
            .field("staged_history", &self.staged_history.len())
            .field("staged_history_transactions", &self.staged_history_transactions)
            .finish_non_exhaustive()
    }
}

pub(crate) struct RotationSegmentOutcome {
    new_references: Vec<HistorySegmentReference>,
    payload_index_root_id: Option<BlockId>,
    payload_routing: Option<crate::history_payload::RoutingRoot>,
    indexed_payload_ids: BTreeSet<BlockId>,
}

/// Phase 2 of the mutex-free rotation checkpoint: encode the covered prefix
/// into history segment blocks. Durable but unreferenced blocks on failure
/// are orphans for GC; nothing is published here.
pub(crate) fn encode_rotation_segments(
    snapshot: &PreparedRotationCheckpoint,
) -> io::Result<RotationSegmentOutcome> {
    snapshot.blocks.flush_pending_subset(&BTreeSet::new())?;
    let mut new_references = Vec::new();
    let mut locations = BTreeMap::new();
    let mut indexed_payload_ids = BTreeSet::new();
    for reference in &snapshot.staged_history {
        let encoded = snapshot.blocks.get(reference.block_id)?;
        let (reference, index) = crate::history_payload::persist_segment(&snapshot.blocks, &encoded, reference.clone())?;
        locations.extend(index.locations());
        indexed_payload_ids.extend(index.entries.iter().map(|entry| entry.0));
        indexed_payload_ids.insert(reference.block_id);indexed_payload_ids.extend(reference.payload_index_block_id);
        new_references.push(reference);
    }
    let remaining = &snapshot.covered[snapshot.staged_history_transactions..];
    if !remaining.is_empty() {
        visit_segments(remaining, snapshot.parent_hash, |segment| {
            let block_id = BlockId::for_payload(&segment.encoded);
            let reference = HistorySegmentReference {
                block_id,
                payload_index_block_id: None,
                first_sequence: segment.first_sequence,
                last_sequence: segment.last_sequence,
                parent_hash: segment.parent_hash,
                terminal_hash: segment.terminal_hash,
            };
            let (reference, index) = crate::history_payload::persist_segment(&snapshot.blocks, &segment.encoded, reference)?;
            locations.extend(index.locations());
            indexed_payload_ids.extend(index.entries.iter().map(|entry| entry.0));
            indexed_payload_ids.insert(reference.block_id);indexed_payload_ids.extend(reference.payload_index_block_id);
            new_references.push(reference);
            Ok(())
        })?;
    }
    let previous = snapshot.history_payload_index_root_id.map(|id| {
        crate::history_payload::RoutingRoot::decode(&snapshot.blocks.get_materialized_shared(id)?)
    }).transpose()?;
    let payload_routing = if previous.is_some() || !locations.is_empty() {
        Some(previous.unwrap_or_default().append(&snapshot.blocks, &locations)?)
    } else { None };
    let payload_index_root_id = payload_routing.as_ref().map(|root| snapshot.blocks.put_loose(&root.encode()?)).transpose()?;
    Ok(RotationSegmentOutcome { new_references, payload_index_root_id, payload_routing, indexed_payload_ids })
}

fn append_rotation_suffix(
    blocks: &BlockStore,
    window: &[CommittedTransaction],
    parent_fallback: [u8; 32],
    outcome: &mut RotationSegmentOutcome,
) -> io::Result<()> {
    let _window = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_WINDOW_ENCODE);
    let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_SEGMENTS);
    let parent = outcome.new_references.last().map_or(parent_fallback, |reference| reference.terminal_hash);
    let mut locations = BTreeMap::new();
    visit_segments(window, parent, |segment| {
        let reference = HistorySegmentReference {
            block_id: BlockId::for_payload(&segment.encoded),
            payload_index_block_id: None,
            first_sequence: segment.first_sequence,
            last_sequence: segment.last_sequence,
            parent_hash: segment.parent_hash,
            terminal_hash: segment.terminal_hash,
        };
        let (reference, index) = crate::history_payload::persist_segment(blocks, &segment.encoded, reference)?;
        locations.extend(index.locations());
        outcome.indexed_payload_ids.extend(index.entries.iter().map(|entry| entry.0));
        outcome.indexed_payload_ids.insert(reference.block_id);
        outcome.indexed_payload_ids.extend(reference.payload_index_block_id);
        outcome.new_references.push(reference);
        Ok(())
    })?;
    if !locations.is_empty() {
        let root = outcome.payload_routing.take().unwrap_or_default().append(blocks, &locations)?;
        outcome.payload_index_root_id = Some(blocks.put_loose(&root.encode()?)?);
        outcome.payload_routing = Some(root);
    }
    Ok(())
}

/// Outcome of [`Engine::try_prepare_pack_adoption`].
pub(crate) enum PackAdoptionPreparation {
    /// No completed pack is waiting.
    Empty,
    /// Another unlocked publication is already in flight.
    Busy,
    /// Publication is reserved; bulk I/O is ready for the unlocked phase.
    Ready(Box<PreparedPackAdoption>),
}

/// A prepared pack adoption: every input the unlocked publish phase needs,
/// owned independently of the engine (no borrow), so it can cross the
/// authority-mutex boundary.
pub(crate) struct PreparedPackAdoption {
    segment_id: Uuid,
    overflow: Option<(PayloadSegmentReference, Uuid)>,
    packed: BTreeSet<BlockId>,
    manifest: PayloadManifest,
    manifest_bytes: Vec<u8>,
    manifest_block_id: BlockId,
    catchup: Option<ControlState>,
    manifest_next: ControlState,
    stabilize_next: ControlState,
    blocks: Arc<BlockStore>,
    control: Arc<Mutex<ControlFile>>,
    wal_cover: Option<(PathBuf, Arc<dyn Vfs>)>,
}

/// Phase 2, the whole fsync chain of a catalog adoption, driven WITHOUT the
/// authority mutex: Normal-mode WAL cover, durable manifest block, then the
/// CONTROL chain (optional dirty catch-up, manifest reference, slot
/// stabilization), serialized by the CONTROL mutex. Failure at any point is
/// ambiguous in the same way a synchronous publication failure is and must
/// be handed to [`Engine::finish_pack_adoption`] unchanged.
pub(crate) fn publish_prepared_pack_adoption(
    prepared: &mut PreparedPackAdoption,
) -> io::Result<()> {
    let mut retired = Vec::with_capacity(2);
    if let Some((incoming, merge_id)) = prepared.overflow {
        let store = prepared.blocks.payload_segment_store()?;
        let candidate = prepared.manifest.segments.iter().enumerate()
            .filter(|(_, old)| {
                old.block_count as usize + incoming.block_count as usize <= MAX_SEGMENT_BLOCKS
                    && old.payload_bytes + incoming.payload_bytes <= MAX_SEGMENT_PAYLOAD_BYTES
            })
            .min_by_key(|(_, old)| (old.payload_bytes, old.block_count))
            .map(|(index, old)| (index, *old));
        if let Some((index, old)) = candidate {
            let source = store.open_segment(old.segment_id)?;
            let added = store.open_segment(incoming.segment_id)?;
            let mut ids: Vec<_> = source.block_ids().chain(added.block_ids()).collect();
            ids.sort_unstable();
            ids.dedup();
            let merged = store.create(merge_id, ids, |id| {
                // Incoming WAL-carried copies retain recovery repair priority.
                let handle = if added.contains(id) { &added } else { &source };
                store.get_with_handle(handle, id)?.ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "pack merge source block is missing")
                })
            })?;
            let reference = PayloadSegmentReference::new(
                merged.metadata(), merged.first_block_id(), merged.last_block_id(),
            )?;
            prepared.blocks.verify_payload_segment(&reference)?;
            let mut references = prepared.manifest.segments.clone();
            references[index] = reference;
            prepared.manifest = PayloadManifest::new(references)?;
            retired.push(old.segment_id);
        } else {
            // The physical catalog is bounded. If no union fits, the existing
            // durable loose-block representation is the lossless overflow path.
            // Its entire write/fsync batch runs outside the authority mutex.
            prepared.blocks.flush_pending_subset(&prepared.packed)?;
        }
        retired.push(incoming.segment_id);
        prepared.manifest_bytes = prepared.manifest.encode()?;
        prepared.manifest_block_id = BlockId::for_payload(&prepared.manifest_bytes);
        prepared.manifest_next.payload_manifest_block_id = Some(prepared.manifest_block_id);
        prepared.stabilize_next.payload_manifest_block_id = Some(prepared.manifest_block_id);
    }
    if let Some((path, vfs)) = &prepared.wal_cover {
        let mut file = vfs.open(
            path,
            OpenRequest {
                write: true,
                ..OpenRequest::default()
            },
        )?;
        file.sync_data()?;
    }
    let mut control = prepared
        .control
        .lock()
        .map_err(|_| io::Error::other("tofu-db CONTROL lock is poisoned"))?;
    if let Some(catchup) = &prepared.catchup {
        control.publish(catchup)?;
    }
    prepared.blocks.put_loose(&prepared.manifest_bytes)?;
    control.publish(&prepared.manifest_next)?;
    control.publish(&prepared.stabilize_next)?;
    drop(control);
    // Both CONTROL slots now reference the replacement. Install the shared
    // read catalog before reclaiming files; racing readers retry its new view.
    prepared.blocks.install_payload_manifest(prepared.manifest.clone())?;
    prepared.blocks.retire_staged_payloads(&prepared.packed)?;
    prepared.blocks.flush_pending_subset(&BTreeSet::new())?;
    // Retired sources are parked, not unlinked: a reader holding the
    // pre-install candidate snapshot can still resolve every manifest
    // version it may reference. The sweep reclaims earlier retirements
    // whose time floor has expired.
    for id in retired {
        prepared
            .blocks
            .payload_segment_store()?
            .defer_segment_retirement(id)?;
    }
    prepared
        .blocks
        .sweep_deferred_segment_retirements(crate::payload_segment::segment_retirement_delay())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    include!("engine_tests.rs");
}
