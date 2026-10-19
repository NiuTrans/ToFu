//! Bounded active commit log with length twins, CRC32C, and BLAKE3 chaining.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crate::vfs::{
    sync_all_barrier, sync_data_barrier, sync_directory_barrier, OpenRequest, RealVfs, Vfs, VfsFile,
};
use crate::{ACTIVE_LOG_MAX_BYTES, FORMAT_VERSION};

/// Durability barrier policy for the active log.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum WalDurability {
    /// Every append is followed by a data barrier before acknowledgement.
    #[default]
    Strict,
    /// Appends return after the write; a bounded background flusher or an
    /// explicit publication point (checkpoint, catalog publication, clean
    /// shutdown) performs the barrier. On power failure an acknowledged
    /// prefix bounded by the flush interval may be lost; recovery truncates
    /// the unsynced tail exactly like a torn record, and command receipts
    /// resolve acknowledged-but-lost operations through ordinary replay.
    Normal,
}

const MIN_WAL_FLUSH_INTERVAL: Duration = Duration::from_millis(1);
pub const MAX_WAL_FLUSH_INTERVAL: Duration = Duration::from_millis(1_000);
/// Longest a foreground caller may park on the barrier worker before the
/// wait is classified (retry-safe: receipts resolve the ambiguous commit).
pub const MAX_BARRIER_WAIT: Duration = Duration::from_secs(30);

const MAGIC: &[u8; 8] = b"TDBWAL01";
const HASH_HEADER_BYTES: usize = 8 + 4 + 8 + 32 + 4;
const FIXED_BODY_BYTES: usize = HASH_HEADER_BYTES + 4 + 32;
const FRAME_PREFIX_BYTES: usize = 4 + HASH_HEADER_BYTES;
const FRAME_TRAILER_BYTES: usize = 4 + 32 + 4;
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

/// One CONTROL-selected predecessor/successor chain. The active offsets stay
/// local to the successor; the extra fields describe the sealed prefix for
/// replay and operator reporting without changing tail-truncation ownership.
pub(crate) struct SelectedWalRecovery {
    pub(crate) recovery: Recovery,
    pub(crate) predecessor_record_count: usize,
    pub(crate) valid_bytes: u64,
}

/// Read-only validation shared by engine open and offline verification.
/// A sealed predecessor must match its exact terminal witness; only the
/// successor may carry an unconfirmed torn tail. No file is truncated here.
pub(crate) fn scan_selected_generation_chain(
    data_dir: &Path,
    state: &crate::control::ControlState,
    active: &mut ActiveLog,
    vfs: std::sync::Arc<dyn Vfs>,
) -> io::Result<SelectedWalRecovery> {
    let mut predecessor_records = Vec::new();
    let (base_sequence, base_hash) = match &state.sealed_log {
        Some(sealed) => {
            let mut predecessor = ActiveLog::open_generation_with_vfs(
                data_dir, sealed.generation, vfs,
            )?;
            let prefix = predecessor.scan(state.checkpoint_sequence,
                state.checkpoint_hash, sealed.terminal_sequence)?;
            if prefix.truncated_bytes != 0 || prefix.records.last().is_none_or(|record| {
                record.sequence != sealed.terminal_sequence || record.record_hash != sealed.terminal_hash
            }) {
                return Err(io::Error::new(io::ErrorKind::InvalidData,
                    "sealed WAL does not match its exact CONTROL terminal witness"));
            }
            predecessor_records = prefix.records;
            (sealed.terminal_sequence, sealed.terminal_hash)
        }
        None => (state.checkpoint_sequence, state.checkpoint_hash),
    };
    let predecessor_record_count = predecessor_records.len();
    let predecessor_bytes = predecessor_records.last().map_or(0, |record| record.end_offset);
    let mut recovery = active.scan(base_sequence, base_hash, state.durable_sequence)?;
    let valid_bytes = predecessor_bytes.checked_add(
        recovery.records.last().map_or(0, |record| record.end_offset),
    ).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "selected WAL byte count overflow"))?;
    predecessor_records.append(&mut recovery.records);
    recovery.records = predecessor_records;
    Ok(SelectedWalRecovery { recovery, predecessor_record_count, valid_bytes })
}

pub struct ActiveLog {
    path: PathBuf,
    file: Box<dyn VfsFile>,
    /// Cached logical length of the active log. Appends are the only writers
    /// after open, so the in-memory value is authoritative and the hot path
    /// avoids an fstat syscall per record.
    len: u64,
    durability: WalDurability,
    /// Bytes written since the last successful data barrier in `Normal` mode;
    /// always zero in `Strict` mode.
    unsynced_bytes: u64,
    /// Shared barrier coordinator. In `Strict` mode an attached barrier takes
    /// over the per-append fdatasync (the caller waits on its witness
    /// instead); without one, `Strict` appends fsync inline. `Normal` mode
    /// uses the same barrier without waiting (bounded-RPO via the driver's
    /// idle backstop); without one its barrier is deferred to publication
    /// points.
    barrier: Option<WalBarrier>,
}

#[derive(Debug)]
struct FlushError {
    kind: io::ErrorKind,
    message: String,
}

#[derive(Debug, Default)]
struct WalBarrierState {
    path: PathBuf,
    requested_len: u64,
    synced_len: u64,
    stop: bool,
    closing: bool,
    error: Option<FlushError>,
    writes: write_buffer::WriteBuffer,
}

/// Shared observation point between the append path, durability waiters, and
/// the barrier worker. The worker never touches the engine's file handle: it
/// opens the log path independently, so its fdatasync covers every byte the
/// engine wrote through any descriptor (POSIX same-file semantics) without
/// entering the authority critical section. Waiters park on `synced` until
/// their witnessed length is covered, which gives strict durability the
/// group-barrier effect: one fsync covers every commit posted during it.
#[derive(Debug, Default)]
pub struct WalBarrierShared {
    state: Mutex<WalBarrierState>,
    wake: Condvar,
    synced: Condvar,
}

/// Cloneable handle to one shared barrier; every clone observes the same
/// state and wakes the same worker.
#[derive(Debug, Default, Clone)]
pub struct WalBarrier {
    shared: Arc<WalBarrierShared>,
}

impl std::ops::Deref for WalBarrier {
    type Target = WalBarrierShared;

    fn deref(&self) -> &WalBarrierShared {
        &self.shared
    }
}

/// A byte prefix of one immutable WAL generation. Capture while holding the
/// authority mutex; waiting may outlive a checkpoint installing its successor.
#[derive(Debug, Clone)]
pub struct WalWitness {
    path: PathBuf,
    len: u64,
}

impl WalBarrierShared {
    #[cfg(test)]
    pub(crate) fn buffered_for_test(capacity: usize) -> WalBarrier {
        let barrier = WalBarrier::default();
        barrier.state.lock().unwrap().writes.capacity = capacity;
        barrier
    }

    fn queue_write(&self, path: &Path, offset: u64, next_len: u64, parts: &[&[u8]]) -> io::Result<bool> {
        let mut state = self.state.lock().map_err(|_| io::Error::other("WAL barrier state poisoned"))?;
        if state.path != path { return Err(io::Error::other("WAL write buffer belongs to another generation")); }
        if state.stop || state.closing { return Err(io::Error::other("WAL barrier is closed")); }
        if let Some(error) = &state.error { return Err(io::Error::new(error.kind, error.message.clone())); }
        if !state.writes.queue(offset, parts)? { return Ok(false); }
        state.requested_len = state.requested_len.max(next_len);
        drop(state);
        self.wake.notify_all();
        Ok(true)
    }

    pub fn write_buffer_metrics(&self) -> serde_json::Value {
        let Ok(state) = self.state.lock() else { return serde_json::json!({"available":false}); };
        let writes = &state.writes;
        serde_json::json!({"available":true,"contract":crate::generated_storage_v2::WAL_WRITE_BUFFER_CONTRACT,
            "enabled":writes.capacity != 0,"failed":state.error.is_some(),"closing":state.closing,"capacityBytes":writes.capacity,
            "retainedFrameBytes":writes.retained_bytes,"peakFrameBytes":writes.peak_bytes,
            "queuedFrames":writes.queued_frames,"writtenFrames":writes.written_frames,
            "writtenBatches":writes.written_batches,"writtenBytes":writes.written_bytes,
            "writeNanos":writes.write_nanos,"synchronousFallbacks":writes.synchronous_fallbacks,
            "scope":"one barrier lifetime; queued/inflight exact frame bytes exclude queue/allocator metadata; writes do not credit durability; byte budget controls reuse not admission"})
    }

    /// Witness posted by the append path after each write: the barrier must
    /// cover at least this length. Wakes the worker immediately so a strict
    /// commit's barrier starts without waiting for the idle backstop.
    pub fn note_appended(&self, len: u64) {
        if let Ok(mut state) = self.state.lock() {
            if len > state.requested_len {
                state.requested_len = len;
            }
        }
        self.wake.notify_all();
    }

    fn note_synced(&self, path: &Path, len: u64) {
        if let Ok(mut state) = self.state.lock() {
            // A worker can finish the retired generation after CONTROL has
            // published its successor. Its bytes cannot cover the new WAL.
            if state.path == path {
                state.synced_len = state.synced_len.max(len);
            }
        }
        self.synced.notify_all();
    }

    /// Deterministic worker tests drive the real file sync without a racing
    /// background thread, then publish the same witness as `run_wal_barrier`.
    #[cfg(test)]
    pub(crate) fn sync_for_test(&self, vfs: &dyn Vfs) -> io::Result<()> {
        let (path, len, frames) = {
            let mut state = self.state.lock().unwrap();
            (state.path.clone(), state.requested_len, state.writes.take())
        };
        let count = frames.len();
        let bytes = frames.iter().map(|f| f.bytes.len()).sum::<usize>();
        let mut wrote = false;
        let mut batches = 0;
        let result = (|| {
            let mut file = vfs.open(&path, OpenRequest { write: true, ..Default::default() })?;
            batches = write_buffer::write_frames(file.as_mut(), &frames)?;
            wrote = true;
            file.sync_data()
        })();
        drop(frames);
        self.state.lock().unwrap().writes.release_counts(bytes, count, wrote, batches, 0);
        match result {
            Ok(()) => { self.note_synced(&path, len); Ok(()) }
            Err(error) => { let returned = io::Error::new(error.kind(), error.to_string()); self.note_flush_error(&path, error); Err(returned) }
        }
    }

    fn note_flush_error(&self, path: &Path, error: io::Error) {
        if let Ok(mut state) = self.state.lock() {
            // Rotation can also retire the old path before its worker opens
            // it. The durable checkpoint already covers that old generation.
            if state.path == path && state.error.is_none() {
                state.error = Some(FlushError {
                    kind: error.kind(),
                    message: error.to_string(),
                });
            }
        }
        self.synced.notify_all();
    }

    pub fn witness(&self, len: u64) -> io::Result<WalWitness> {
        let state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("WAL barrier state poisoned"))?;
        Ok(WalWitness {
            path: state.path.clone(),
            len,
        })
    }

    fn synced_len(&self) -> u64 {
        self.state.lock().map_or(0, |state| state.synced_len)
    }

    fn note_rotated(&self, path: PathBuf) {
        if let Ok(mut state) = self.state.lock() {
            if state.path != path {
                if state.writes.retained_bytes != 0 {
                    state.error = Some(FlushError { kind: io::ErrorKind::InvalidData,
                        message: "WAL source changed before queued writes completed".into() });
                    drop(state);
                    self.synced.notify_all();
                    return;
                }
                state.path = path;
                state.requested_len = 0;
                state.synced_len = 0;
            }
        }
        // Engine installs a successor only AFTER synchronizing the complete
        // old WAL and publishing its checkpoint or sealed ownership in CONTROL.
        // Old witnesses remain durable in history or the referenced predecessor.
        self.synced.notify_all();
        self.wake.notify_all();
    }

    /// A barrier failure is persistent: every later caller must observe it,
    /// because the pending prefix may never reach stable storage.
    #[cfg(test)]
    pub(crate) fn inject_background_error_for_test(&self, error: io::Error) {
        let path = self.state.lock().unwrap().path.clone();
        self.note_flush_error(&path, error);
    }

    /// A cached owned reply performs no I/O and must never wait for a driver.
    /// Contention/poison/error means no health proof; ordinary dispatch remains.
    pub(crate) fn try_healthy(&self) -> bool {
        self.state.try_lock().is_ok_and(|state| state.error.is_none())
    }

    pub fn barrier_error(&self) -> Option<io::Error> {
        let state = self.state.lock().ok()?;
        let error = state.error.as_ref()?;
        Some(io::Error::new(error.kind, error.message.clone()))
    }

    /// Parks the caller until the barrier covers `len`, an error is latched,
    /// or the deadline passes. Waiters never hold the authority mutex: the
    /// worker syncs through an independent descriptor, so the wait cannot
    /// deadlock the engine.
    pub fn wait_synced(&self, len: u64, deadline: std::time::Instant) -> io::Result<()> {
        self.wait_witness(&self.witness(len)?, deadline)
    }

    pub fn wait_witness(
        &self,
        witness: &WalWitness,
        deadline: std::time::Instant,
    ) -> io::Result<()> {
        let mut guard = self
            .state
            .lock()
            .map_err(|_| io::Error::other("WAL barrier state poisoned"))?;
        loop {
            if let Some(error) = guard.error.as_ref() {
                return Err(io::Error::new(error.kind, error.message.clone()));
            }
            if guard.path != witness.path || guard.synced_len >= witness.len {
                return Ok(());
            }
            let now = std::time::Instant::now();
            if now >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "WAL barrier wait exceeded its deadline",
                ));
            }
            let (next, _) = self
                .synced
                .wait_timeout(guard, deadline.saturating_duration_since(now))
                .map_err(|_| io::Error::other("WAL barrier state poisoned"))?;
            guard = next;
        }
    }
}

/// Owns the barrier worker thread. The driver flushes the current log path
/// at the greatest witnessed length, with `interval` as the idle backstop
/// that bounds Normal-mode loss in wall time.
pub struct WalBarrierDriver {
    barrier: WalBarrier,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl WalBarrierDriver {
    pub fn start(
        vfs: Arc<dyn Vfs>,
        path: PathBuf,
        interval: Duration,
    ) -> io::Result<(WalBarrier, Self)> {
        Self::start_with_write_buffer(vfs, path, interval, 0)
    }

    /// Reuses the same launch-time resource observation as transport/cache sizing.
    pub fn capacity_for_headroom(headroom_bytes: Option<u64>) -> usize {
        use crate::generated_storage_v2::{WAL_WRITE_BUFFER_HEADROOM_DIVISOR, WAL_WRITE_BUFFER_FALLBACK_BYTES};
        usize::try_from(headroom_bytes.map(|bytes| bytes / WAL_WRITE_BUFFER_HEADROOM_DIVISOR)
            .unwrap_or(WAL_WRITE_BUFFER_FALLBACK_BYTES)).unwrap_or(usize::MAX)
    }

    pub fn start_with_write_buffer(vfs: Arc<dyn Vfs>, path: PathBuf, interval: Duration, capacity_bytes: usize) -> io::Result<(WalBarrier, Self)> {
        if !(MIN_WAL_FLUSH_INTERVAL..=MAX_WAL_FLUSH_INTERVAL).contains(&interval) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WAL flush interval must be between 1 ms and 1000 ms",
            ));
        }
        let barrier = WalBarrier::default();
        barrier.note_rotated(path);
        if vfs.supports_buffered_wal_writes() {
            barrier.state.lock().map_err(|_| io::Error::other("WAL barrier state poisoned"))?.writes.capacity = capacity_bytes;
        }
        let worker_barrier = barrier.clone();
        let worker = thread::Builder::new()
            .name("tofu-db-wal-barrier".to_owned())
            .spawn(move || run_wal_barrier(vfs, worker_barrier, interval))?;
        Ok((
            barrier.clone(),
            Self {
                barrier,
                worker: Mutex::new(Some(worker)),
            },
        ))
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        let drain = match self.barrier.state.lock() {
            Ok(mut state) => {
                state.closing = true;
                Ok((state.writes.capacity != 0 && state.requested_len > state.synced_len)
                    .then_some(state.requested_len))
            }
            Err(_) => Err(io::Error::other("WAL barrier state poisoned during close")),
        };
        // Join even on poison; unknown ownership cannot report a drained close.
        let drained = drain.and_then(|target| target.map(|len| self.barrier.wait_synced(len,
            std::time::Instant::now() + MAX_BARRIER_WAIT)).transpose());
        if let Ok(mut state) = self.barrier.state.lock() {
            state.stop = true;
        }
        self.barrier.wake.notify_all();
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("WAL barrier state poisoned"))?
            .take();
        if let Some(worker) = worker {
            worker
                .join()
                .map_err(|_| io::Error::other("WAL barrier worker panicked"))?;
        }
        if let Ok(mut state) = self.barrier.state.lock() {
            state.writes.pending.clear();
            state.writes.retained_bytes = 0;
        }
        drained.map(|_| ())
    }
}

impl Drop for WalBarrierDriver {
    fn drop(&mut self) {
        let _ = self.stop_and_join();
    }
}

fn run_wal_barrier(vfs: Arc<dyn Vfs>, barrier: WalBarrier, interval: Duration) {
    run_wal_barrier_with_sync_hook(vfs, barrier, interval, || {});
}

// The hook makes the notification window after a real sync deterministic in
// tests. The production closure is empty and is eliminated during compilation.
fn run_wal_barrier_with_sync_hook(
    vfs: Arc<dyn Vfs>, barrier: WalBarrier, interval: Duration,
    mut after_sync: impl FnMut(),
) {
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_WAL_BARRIER);
    let mut handle: Option<(PathBuf, Box<dyn VfsFile>)> = None;
    loop {
        let (path, requested_len, synced_len, stop, failed, frames) = match barrier.state.lock() {
            Ok(mut state) => {
                let failed = state.error.is_some();
                let frames = if failed { Default::default() } else { state.writes.take() };
                (state.path.clone(), state.requested_len, state.synced_len, state.stop, failed, frames)
            },
            Err(_) => return,
        };
        if stop { return; }
        if !failed && requested_len > synced_len {
            let frame_count = frames.len();
            let frame_bytes = frames.iter().map(|f| f.bytes.len()).sum::<usize>();
            let mut batches_written = 0;
            let mut write_nanos = 0;
            let mut wrote = false;
            let result = (|| -> io::Result<()> {
                if handle.as_ref().is_none_or(|(handle_path, _)| *handle_path != path) {
                    handle = Some((path.clone(), vfs.open(&path, OpenRequest { write: true, ..Default::default() })?));
                }
                let file = &mut handle.as_mut().expect("opened WAL worker handle").1;
                let started = std::time::Instant::now();
                let written = write_buffer::write_frames(file.as_mut(), &frames);
                write_nanos = started.elapsed().as_nanos() as u64;
                batches_written = written?;
                wrote = true;
                let _sync = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL_BARRIER_SYNC);
                file.sync_data()
            })();
            // Budget credit follows actual frame deallocation, including the
            // complete write+sync interval; inflight frames cannot hide RAM.
            drop(frames);
            if let Ok(mut state) = barrier.state.lock() {
                state.writes.release_counts(frame_bytes, frame_count, wrote, batches_written, write_nanos);
            }
            match result {
                Ok(()) => barrier.note_synced(&path, requested_len),
                Err(error) => barrier.note_flush_error(&path, error),
            }
        }
        after_sync();
        let guard = match barrier.state.lock() {
            Ok(state) => state,
            Err(_) => return,
        };
        if guard.stop {
            return;
        }
        // Notifications arriving during fsync are not remembered by Condvar.
        // Check its predicate under the same mutex used by note_appended before
        // atomically releasing it to sleep. A latched I/O failure must not spin.
        if guard.error.is_none() && guard.requested_len > guard.synced_len {
            let _reschedule = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL_BARRIER_RESCHEDULE);
            continue;
        }
        let (guard, _) = barrier
            .wake
            .wait_timeout(guard, interval)
            .unwrap_or_else(|error| error.into_inner());
        if guard.stop {
            return;
        }
    }
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

/// Shared field layout for persisted frames and allocation-free hash checks.
fn record_hash_header(
    sequence: u64,
    parent_hash: [u8; 32],
    payload_len: usize,
) -> io::Result<[u8; HASH_HEADER_BYTES]> {
    if payload_len > MAX_TRANSACTION_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "transaction exceeds 8 MiB",
        ));
    }
    let mut header = [0_u8; HASH_HEADER_BYTES];
    header[..8].copy_from_slice(MAGIC);
    header[8..12].copy_from_slice(&FORMAT_VERSION.to_le_bytes());
    header[12..20].copy_from_slice(&sequence.to_le_bytes());
    header[20..52].copy_from_slice(&parent_hash);
    header[52..56].copy_from_slice(&(payload_len as u32).to_le_bytes());
    Ok(header)
}

/// Original frame split into fixed framing and the borrowed immutable payload.
/// CRC and chain hash cover exactly the original bytes; only their copy is gone.
fn encode_frame_parts(sequence: u64, parent_hash: [u8; 32], payload: &[u8])
    -> io::Result<([u8; FRAME_PREFIX_BYTES], [u8; FRAME_TRAILER_BYTES], [u8; 32])> {
    let header = record_hash_header(sequence, parent_hash, payload.len())?;
    let body_len = FIXED_BODY_BYTES.checked_add(payload.len())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "transaction length overflow"))?;
    let (crc, hash) = calculate_frame_checksums(&header, payload);
    let mut leading = [0; FRAME_PREFIX_BYTES];
    leading[..4].copy_from_slice(&(body_len as u32).to_le_bytes());
    leading[4..].copy_from_slice(&header);
    let mut trailing = [0; FRAME_TRAILER_BYTES];
    trailing[..4].copy_from_slice(&crc.to_le_bytes());
    trailing[4..36].copy_from_slice(&hash);
    trailing[36..].copy_from_slice(&(body_len as u32).to_le_bytes());
    Ok((leading, trailing, hash))
}

fn calculate_frame_checksums(header: &[u8; HASH_HEADER_BYTES], payload: &[u8]) -> (u32, [u8; 32]) {
    let crc = crc32c::crc32c_append(crc32c::crc32c(header), payload);
    let mut hasher = blake3::Hasher::new();
    hasher.update(header);
    hasher.update(payload);
    hasher.update(&crc.to_le_bytes());
    let hash = *hasher.finalize().as_bytes();
    (crc, hash)
}

fn encode(sequence: u64, parent_hash: [u8; 32], payload: &[u8]) -> io::Result<(Vec<u8>, [u8; 32])> {
    let header = record_hash_header(sequence, parent_hash, payload.len())?;
    let body_len = FIXED_BODY_BYTES.checked_add(payload.len()).ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "transaction length overflow")
    })?;
    let mut record = Vec::with_capacity(body_len + 8);
    record.extend_from_slice(&(body_len as u32).to_le_bytes());
    record.extend_from_slice(&header);
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
    let header = record_hash_header(sequence, parent_hash, payload.len())?;
    Ok(calculate_frame_checksums(&header, payload).1)
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
        Ok(Self {
            path,
            file,
            len: 0,
            durability: WalDurability::default(),
            unsynced_bytes: 0,
            barrier: None,
        })
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
        let len = file.len()?;
        if len > ACTIVE_LOG_MAX_BYTES {
            return Err(invalid_data("active WAL exceeds the 64 MiB recovery bound"));
        }
        Ok(Self {
            path,
            file,
            len,
            durability: WalDurability::default(),
            unsynced_bytes: 0,
            barrier: None,
        })
    }

    /// Current logical length; the witness strict-mode callers park on.
    pub fn len(&self) -> u64 {
        self.len
    }

    pub fn durability(&self) -> WalDurability {
        self.durability
    }

    pub fn set_durability(&mut self, durability: WalDurability) {
        self.durability = durability;
    }

    /// Bytes not known to be covered by any barrier, whether performed by
    /// the engine or observed by the barrier worker. The worker's
    /// independent fdatasync covers the same inode, so its progress counts.
    /// Reuse an orphan only after witnessing both its empty inode and name.
    /// A previous create may have failed before its directory barrier.
    pub(crate) fn resync_empty_generation(&mut self, data_dir: &Path, vfs: &dyn Vfs) -> io::Result<()> {
        if !self.is_empty()? {
            return Err(invalid_data("orphan successor WAL is not empty"));
        }
        sync_all_barrier(self.file.as_mut())?;
        sync_directory_barrier(vfs, data_dir)
    }

    pub fn unsynced_bytes(&self) -> u64 {
        match &self.barrier {
            Some(barrier) => self.len.saturating_sub(barrier.synced_len()),
            None => self.unsynced_bytes,
        }
    }

    pub fn attach_barrier(&mut self, barrier: &WalBarrier) {
        barrier.note_rotated(self.path.clone());
        // Bytes written before the attachment predate the worker: post the
        // current length as one witnessed append so a barrier wait on a
        // pre-attachment witness (e.g. a read command's `wal_len()`) is
        // covered by a real fsync instead of parking until its deadline on
        // a length no append will ever request.
        barrier.note_appended(self.len);
        self.barrier = Some(barrier.clone());
    }

    pub(crate) fn buffered_writes_enabled(&self) -> bool {
        // Unknown state may own unwritten frames. Preserve the failed barrier
        // rather than detaching it and trusting an inline fsync on empty bytes.
        self.barrier.as_ref().is_some_and(|b| b.state.lock().map_or(true, |s| s.writes.capacity != 0))
    }

    pub fn detach_barrier(&mut self) {
        let must_drain = self.buffered_writes_enabled();
        if must_drain && self.sync_pending().is_err() { return; }
        if let Some(barrier) = self.barrier.take() {
            // Preserve the worker's observed barrier progress so the engine
            // side counter stays honest after detachment.
            self.unsynced_bytes = self.len.saturating_sub(barrier.synced_len());
        }
    }

    /// Performs the deferred data barrier and parks the caller until it
    /// lands. With an attached barrier this waits on the worker (which is
    /// already syncing at the requested length); without one it fsyncs the
    /// engine's own handle inline. A barrier failure is ambiguous exactly
    /// like a failed CONTROL write and is latched by the engine.
    pub fn sync_pending(&mut self) -> io::Result<u64> {
        let pending = self.unsynced_bytes();
        if pending == 0 {
            return Ok(0);
        }
        if let Some(barrier) = &self.barrier {
            barrier.wait_synced(self.len, std::time::Instant::now() + MAX_BARRIER_WAIT)?;
            return Ok(pending);
        }
        sync_data_barrier(self.file.as_mut())?;
        self.unsynced_bytes = 0;
        Ok(pending)
    }

    pub fn append(
        &mut self,
        sequence: u64,
        parent_hash: [u8; 32],
        payload: &[u8],
    ) -> io::Result<[u8; 32]> {
        let (leading, trailing, hash) = encode_frame_parts(sequence, parent_hash, payload)?;
        let record_len = leading.len() + payload.len() + trailing.len();
        let current_len = self.len;
        let next_len = current_len
            .checked_add(record_len as u64)
            .ok_or_else(|| invalid_data("active WAL length overflow"))?;
        if next_len > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::ResourceBusy,
                "active WAL rotation required",
            ));
        }
        if self.durability == WalDurability::Strict {
            if let Some(barrier) = &self.barrier {
                if barrier.queue_write(&self.path, current_len, next_len, &[&leading, payload, &trailing])? {
                    crate::fsync_stats::note_wal_encoded_bytes(next_len - current_len);
                    self.len = next_len;
                    return Ok(hash);
                }
            }
        }
        self.file.write_all_vectored_at(current_len, &mut [
            io::IoSlice::new(&leading), io::IoSlice::new(payload), io::IoSlice::new(&trailing),
        ])?;
        self.note_appended(next_len - current_len, next_len)?;
        self.len = next_len;
        Ok(hash)
    }

    fn note_appended(&mut self, record_bytes: u64, next_len: u64) -> io::Result<()> {
        crate::fsync_stats::note_wal_encoded_bytes(record_bytes);
        match self.durability {
            WalDurability::Strict => {
                // An attached barrier performs the fdatasync on its own
                // thread; the commit path only posts the witness and the
                // waiter parks outside the authority mutex. Without one,
                // `Strict` keeps its inline per-append barrier.
                if let Some(barrier) = &self.barrier {
                    barrier.note_appended(next_len);
                    Ok(())
                } else {
                    sync_data_barrier(self.file.as_mut())
                }
            }
            WalDurability::Normal => {
                // The barrier is deferred by definition: without an attached
                // worker it is bounded by publication points, with one it is
                // bounded in wall time by the driver's idle backstop.
                self.unsynced_bytes = self.unsynced_bytes.saturating_add(record_bytes);
                if let Some(barrier) = &self.barrier {
                    barrier.note_appended(next_len);
                }
                Ok(())
            }
        }
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
        let current_len = self.len;
        let next_len = current_len
            .checked_add(bytes.len() as u64)
            .ok_or_else(|| invalid_data("active WAL length overflow"))?;
        if next_len > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::ResourceBusy,
                "active WAL rotation required",
            ));
        }
        self.file.write_all_at(current_len, &bytes)?;
        self.note_appended(next_len - current_len, next_len)?;
        self.len = next_len;
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
        self.recover_with_mode(checkpoint_sequence, checkpoint_hash, durable_sequence, true)
    }

    /// Read-only chain walk for the offline verifier: identical framing,
    /// hash-chain and durable-witness checks, but never truncates the file.
    pub(crate) fn scan(
        &mut self,
        checkpoint_sequence: u64,
        checkpoint_hash: [u8; 32],
        durable_sequence: u64,
    ) -> io::Result<Recovery> {
        self.recover_with_mode(
            checkpoint_sequence,
            checkpoint_hash,
            durable_sequence,
            false,
        )
    }

    fn recover_with_mode(
        &mut self,
        checkpoint_sequence: u64,
        checkpoint_hash: [u8; 32],
        durable_sequence: u64,
        truncate: bool,
    ) -> io::Result<Recovery> {
        if checkpoint_sequence > durable_sequence {
            return Err(invalid_data(
                "WAL checkpoint sequence exceeds durable sequence",
            ));
        }
        if self.buffered_writes_enabled() { self.sync_pending()?; }
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
        // A clean record boundary is not proof of completeness: an entire
        // acknowledged suffix can be absent while every remaining CRC/hash
        // still verifies. Reject before block replay or any tail truncation.
        if recovered_sequence < durable_sequence {
            return Err(invalid_data(format!(
                "CONTROL points beyond active WAL: checkpoint={checkpoint_sequence}, durable={durable_sequence}, recovered={recovered_sequence}, valid_bytes={offset}, invalid_tail={invalid_tail}"
            )));
        }
        let truncated_bytes = (bytes.len() - offset) as u64;
        if truncate && truncated_bytes > 0 {
            self.file.set_len(offset as u64)?;
            sync_all_barrier(self.file.as_mut())?;
            self.len = offset as u64;
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
        Ok(self.len == 0)
    }

    pub fn would_exceed(&self, payload: &[u8], limit: u64) -> io::Result<bool> {
        if limit == 0 || limit > ACTIVE_LOG_MAX_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "active WAL admission limit is invalid",
            ));
        }
        // The record length is fully determined by the payload length
        // (encode appends a fixed overhead), so admission must not pay for
        // framing, CRC32C, and BLAKE3 over the whole payload on the hot path.
        if payload.len() > MAX_TRANSACTION_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction exceeds 8 MiB",
            ));
        }
        let record_bytes = WAL_RECORD_OVERHEAD_BYTES
            .checked_add(payload.len())
            .ok_or_else(|| invalid_data("WAL record length overflow"))?;
        Ok(self
            .len
            .checked_add(record_bytes as u64)
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
        for payload in payloads {
            if payload.len() > MAX_TRANSACTION_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "transaction exceeds 8 MiB",
                ));
            }
            encoded_bytes = encoded_bytes
                .checked_add(WAL_RECORD_OVERHEAD_BYTES + payload.len())
                .ok_or_else(|| invalid_data("WAL group length overflow"))?;
            if encoded_bytes > MAX_GROUP_ENCODED_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "WAL group exceeds 8 MiB encoded bound",
                ));
            }
        }
        Ok(self
            .len
            .checked_add(encoded_bytes as u64)
            .is_none_or(|next| next > limit))
    }

    pub fn truncate(&mut self, end_offset: u64) -> io::Result<()> {
        if end_offset > self.len {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WAL truncation offset exceeds file length",
            ));
        }
        if self.buffered_writes_enabled() { self.sync_pending()?; }
        self.file.set_len(end_offset)?;
        sync_all_barrier(self.file.as_mut())?;
        self.len = end_offset;
        Ok(())
    }
}

pub(crate) fn active_log_path(data_dir: &Path, generation: u64) -> io::Result<PathBuf> {
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
    fn barrier_rechecks_writes_arriving_between_sync_and_sleep() {
        let (vfs, mut log) = durable_simulated_log();
        let barrier = WalBarrier::default();
        log.attach_barrier(&barrier);
        let first = log.append(1, [0; 32], b"first covered prefix").unwrap();
        let (reached, wait_reached) = std::sync::mpsc::sync_channel(0);
        let (resume, wait_resume) = std::sync::mpsc::sync_channel(0);
        let worker_barrier = barrier.clone();
        let worker_vfs = vfs.clone();
        let worker = thread::spawn(move || {
            let mut pause = Some(wait_resume);
            run_wal_barrier_with_sync_hook(worker_vfs, worker_barrier,
                Duration::from_secs(1), move || {
                    if let Some(resume) = pause.take() {
                        reached.send(()).unwrap();
                        resume.recv_timeout(Duration::from_secs(5)).unwrap();
                    }
                });
        });
        let driver = WalBarrierDriver { barrier: barrier.clone(), worker: Mutex::new(Some(worker)) };
        wait_reached.recv_timeout(Duration::from_secs(5)).unwrap();
        log.append(2, first, b"arrival while worker cannot receive wakeup").unwrap();
        let witness = barrier.witness(log.len()).unwrap();
        resume.send(()).unwrap();
        let result = barrier.wait_witness(&witness,
            std::time::Instant::now() + Duration::from_millis(250));
        driver.stop_and_join().unwrap();
        assert!(result.is_ok(), "queued write must not wait for the idle interval: {result:?}");
        vfs.crash().unwrap();
        let mut recovered = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.recover(0).unwrap().records.len(), 2);
    }

    #[test]
    fn retired_wal_flush_cannot_acknowledge_unsynced_successor_after_crash() {
        let (vfs, mut old) = durable_simulated_log();
        let barrier = WalBarrier::default();
        old.attach_barrier(&barrier);
        old.append(1, [0; 32], &[1; 4096]).unwrap();
        // Deterministically pause the worker after it captured the old file
        // and length, then install the successor before completing that flush.
        let old_len = old.len();
        let mut next =
            ActiveLog::initialize_generation_with_vfs(Path::new("/data"), 2, vfs.clone()).unwrap();
        next.attach_barrier(&barrier);
        next.append(1, [0; 32], b"must have its own barrier")
            .unwrap();
        old.file.sync_data().unwrap();
        barrier.note_synced(old.path(), old_len);
        assert_eq!(
            barrier
                .wait_synced(next.len(), std::time::Instant::now())
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
        next.file.sync_data().unwrap();
        barrier.note_synced(next.path(), next.len());
        barrier
            .wait_synced(next.len(), std::time::Instant::now())
            .unwrap();
        drop(old);
        drop(next);
        vfs.crash().unwrap();
        let recovered = ActiveLog::open_generation_with_vfs(Path::new("/data"), 2, vfs)
            .unwrap()
            .recover(1)
            .unwrap();
        assert_eq!(recovered.records[0].payload, b"must have its own barrier");
    }

    #[test]
    fn checkpoint_covers_old_waiter_without_waiting_for_successor_length() {
        let barrier = WalBarrier::default();
        barrier.note_rotated(PathBuf::from("active.wal"));
        barrier.note_appended(4096);
        let witness = barrier.witness(4096).unwrap();
        barrier.note_synced(Path::new("active.wal"), 4096);
        // Checkpoint publication covers the prefix before installing the new
        // path. A delayed response still belongs to the captured generation.
        barrier.note_rotated(PathBuf::from("active-2.wal"));
        barrier
            .wait_witness(&witness, std::time::Instant::now())
            .unwrap();
        barrier.note_flush_error(Path::new("active.wal"), io::ErrorKind::NotFound.into());
        assert!(barrier.barrier_error().is_none());
        barrier.note_flush_error(Path::new("active-2.wal"), io::ErrorKind::Other.into());
        assert!(barrier
            .wait_witness(&witness, std::time::Instant::now())
            .is_err());
    }

    #[test]
    fn incremental_hash_matches_persisted_frames_across_payload_boundaries_and_crash() {
        let (vfs, mut log) = durable_simulated_log();
        let lengths = [
            0,
            1,
            7,
            55,
            56,
            63,
            64,
            65,
            967,
            968,
            969,
            1023,
            1024,
            1025,
            4095,
            4096,
            4097,
            4 * 1024 * 1024,
            MAX_TRANSACTION_BYTES,
        ];
        let mut parent = [0; 32];
        let mut expected = Vec::new();
        for (index, length) in lengths.into_iter().enumerate() {
            let sequence = index as u64 + 1;
            let payload: Vec<u8> = (0..length).map(|offset| (offset % 251) as u8).collect();
            let hash = calculate_record_hash(sequence, parent, &payload).unwrap();
            let (original_frame, original_hash) = encode(sequence, parent, &payload).unwrap();
            assert_eq!(hash, original_hash, "payload={length}");
            let (leading, trailing, vectored_hash) = encode_frame_parts(sequence, parent, &payload).unwrap();
            let reconstructed: Vec<u8> = leading.into_iter().chain(payload.iter().copied()).chain(trailing).collect();
            assert_eq!(reconstructed, original_frame, "frame payload={length}");
            assert_eq!(vectored_hash, original_hash, "vectored hash payload={length}");
            assert_eq!(hash, log.append(sequence, parent, &payload).unwrap());
            expected.push((sequence, hash, length));
            parent = hash;
        }
        drop(log);
        vfs.crash().unwrap();
        let recovered = ActiveLog::open_with_vfs(Path::new("/data"), vfs)
            .unwrap()
            .recover(expected.len() as u64)
            .unwrap();
        assert_eq!(recovered.truncated_bytes, 0);
        assert_eq!(recovered.records.len(), expected.len());
        for (record, (sequence, hash, length)) in recovered.records.iter().zip(expected) {
            assert_eq!(
                (record.sequence, record.record_hash, record.payload.len()),
                (sequence, hash, length)
            );
            assert!(record
                .payload
                .iter()
                .enumerate()
                .all(|(offset, byte)| *byte == (offset % 251) as u8));
        }
        assert_eq!(
            calculate_record_hash(1, [0; 32], &vec![0; MAX_TRANSACTION_BYTES + 1])
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
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
    fn recovery_refuses_clean_eof_below_control_without_mutating_evidence() {
        for retained_records in [0, 1] {
            let (vfs, mut log) = durable_simulated_log();
            let hash = log.append(1, [0; 32], b"first acknowledged record").unwrap();
            let prefix = if retained_records == 0 { 0 } else { log.file.len().unwrap() };
            log.append(2, hash, b"missing acknowledged record").unwrap();
            log.file.set_len(prefix).unwrap();
            log.file.sync_all().unwrap();
            vfs.crash().unwrap();
            drop(log);
            let mut log = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
            for truncate in [false, true] {
                let error = log.recover_with_mode(0, [0; 32], 2, truncate).unwrap_err();
                assert_eq!(error.kind(), io::ErrorKind::InvalidData);
                assert!(error.to_string().contains("durable=2"));
                assert_eq!(log.file.len().unwrap(), prefix);
            }
        }
    }

    #[test]
    fn vectored_append_torn_frame_boundaries_preserve_acknowledged_prefix_after_crash() {
        let payload = b"unacknowledged payload";
        let payload_end = FRAME_PREFIX_BYTES + payload.len();
        let frame_len = payload_end + FRAME_TRAILER_BYTES;
        for cut in [1, 4, FRAME_PREFIX_BYTES - 1, FRAME_PREFIX_BYTES,
            FRAME_PREFIX_BYTES + 1, payload_end - 1, payload_end,
            payload_end + 1, frame_len - 1] {
            let (vfs, mut log) = durable_simulated_log();
            let first_hash = log.append(1, [0; 32], b"acknowledged prefix").unwrap();
            let acknowledged_len = log.len();
            vfs.arm_operation_fault(Operation::Write, 1, FaultAction::ShortWrite(cut)).unwrap();
            assert_eq!(log.append(2, first_hash, payload).unwrap_err().kind(), io::ErrorKind::WriteZero);
            assert!(vfs.operation_fault_fired().unwrap(), "short write must fire: cut={cut}");
            assert_eq!(log.len(), acknowledged_len, "failed append cannot advance logical length");
            vfs.arm_fault(None).unwrap();
            // An ambient kernel flush may persist bytes even after write fails.
            // Force that outcome; recovery must reject every partial frame.
            sync_data_barrier(log.file.as_mut()).unwrap();
            drop(log);
            vfs.crash().unwrap();
            let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let recovery = reopened.recover(0).unwrap();
            assert_eq!(recovery.truncated_bytes, cut as u64, "cut={cut}");
            assert_eq!(recovery.records.len(), 1, "cut={cut}");
            assert_eq!(recovery.records[0].payload, b"acknowledged prefix");
            assert_eq!(recovery.records[0].record_hash, first_hash);
            assert_eq!(reopened.len(), acknowledged_len);
            reopened.append(2, first_hash, b"retry after recovery").unwrap();
            drop(reopened);
            vfs.crash().unwrap();
            let mut reopened = ActiveLog::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let records = reopened.recover(0).unwrap().records;
            assert_eq!(records.len(), 2, "cut={cut}");
            assert_eq!(records[1].payload, b"retry after recovery");
        }
    }

    #[test]
    fn append_faults_recover_only_absent_or_complete_record() {
        let (baseline_vfs, mut baseline) = durable_simulated_log();
        baseline.append(1, [0; 32], b"one").unwrap();
        // Append sizes the write from the cached in-memory length, so no
        // Metadata syscall appears on the record hot path.
        let trace = baseline_vfs.trace().unwrap();
        assert_eq!(
            trace,
            vec![Operation::Write, Operation::SyncData, Operation::SyncData]
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

#[path = "wal_write_buffer.rs"]
mod write_buffer;

#[cfg(test)]
#[path = "wal_buffer_tests.rs"]
mod buffer_tests;

#[path = "wal_publication.rs"]
mod publication;
pub(crate) use publication::PublicationWalCover;

#[cfg(test)]
#[path = "wal_query_cache_health_tests.rs"]
mod wal_query_cache_health_tests;
