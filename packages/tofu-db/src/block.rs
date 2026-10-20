//! Immutable, content-addressed blocks written before their referencing commit.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::Duration;

use uuid::Uuid;

use crate::generated_tofudb_ir::{
    MAX_BLOCK_PAYLOAD_CACHE_ENTRIES, MAX_BLOCK_PAYLOAD_CACHE_RECENCY_RECORDS,
};
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::{PayloadSegment, PayloadSegmentStore};
use crate::vfs::{sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile, VfsPositionedReader};
use crate::FORMAT_VERSION;

const MAGIC: &[u8; 8] = b"TDBBLK01";
const HEADER_BYTES: usize = 8 + 4 + 4;
const FOOTER_BYTES: usize = 4 + 32;
pub const MAX_BLOCK_BYTES: usize = 4 * 1024 * 1024;
const MAX_BLOCK_FILE_BYTES: u64 = (HEADER_BYTES + MAX_BLOCK_BYTES + FOOTER_BYTES) as u64;

/// Union bloom filter over every block ID packed into a live payload
/// segment. Content hashes are uniformly random, so a segment's
/// first/last ID range overlaps virtually every probe: without this
/// filter, every durable-read probe of a fresh block scans every
/// published segment — O(segments) per probe, and segments accumulate
/// one per checkpoint batch, i.e. O(commits) over the store's lifetime.
/// The filter has no false negatives for currently packed blocks, so a
/// miss proves the block is not segment-resident and the scan is
/// skipped; false positives only cost the scan the pre-filter code
/// always paid. In-memory only, built from the manifest's segment
/// indexes at open and re-built (geometric growth, amortized O(1) per
/// insert) whenever insertions saturate its capacity — a bloom filter
/// cannot be resized in place, and an over-full filter degenerates to
/// the pre-filter O(segments) scan. Sized at 16 bits per packed block
/// (1M blocks ≈ 2 MiB) with ~11 hash rounds for a sub-0.1%
/// false-positive rate at capacity. Segment removals (merge, GC) leave
/// stale bits — harmless false positives only, absorbed by the same
/// capacity headroom.
const PACKED_FILTER_MIN_BITS: usize = 1 << 20;
const PACKED_FILTER_BITS_PER_BLOCK: usize = 16;
const PACKED_FILTER_ROUNDS: u64 = 11;

struct PackedBlockFilter {
    words: Vec<u64>,
    mask: usize,
    built: bool,
    /// Insertions since the last build; compared against
    /// `capacity_blocks` to trigger the geometric rebuild.
    insertions: usize,
    capacity_blocks: usize,
    revision: u64,
    /// Negative answers cover exactly this process-local catalog publication.
    covered_catalog_publication: u64,
}

impl PackedBlockFilter {
    fn empty() -> Self {
        Self {
            words: Vec::new(),
            mask: 0,
            built: false,
            insertions: 0,
            capacity_blocks: 0,
            revision: 0,
            covered_catalog_publication: 0,
        }
    }

    /// Builds a filter with room for `blocks` currently packed IDs plus
    /// an equal share of future insertions, so rebuilds stay geometric.
    fn with_capacity(blocks: usize) -> Self {
        let bits = blocks
            .saturating_mul(2)
            .saturating_mul(PACKED_FILTER_BITS_PER_BLOCK)
            .max(PACKED_FILTER_MIN_BITS)
            .next_power_of_two();
        Self {
            words: vec![0; bits / 64],
            mask: bits - 1,
            built: true,
            insertions: 0,
            capacity_blocks: bits / PACKED_FILTER_BITS_PER_BLOCK,
            revision: 0,
            covered_catalog_publication: 0,
        }
    }

    fn saturated(&self) -> bool {
        self.insertions >= self.capacity_blocks
    }

    /// Double hashing off the content hash itself: the ID is already a
    /// uniform digest, so slicing two u64 words yields independent
    /// bases. The odd step guarantees a full period mod 2^k.
    fn hashes(id: BlockId) -> (u64, u64) {
        let base = u64::from_le_bytes(id.0[0..8].try_into().unwrap());
        let step = u64::from_le_bytes(id.0[8..16].try_into().unwrap()) | 1;
        (base, step)
    }

    fn insert(&mut self, id: BlockId) {
        if self.words.is_empty() {
            return;
        }
        self.insertions += 1;
        let (mut position, step) = Self::hashes(id);
        for _ in 0..PACKED_FILTER_ROUNDS {
            let bit = (position as usize) & self.mask;
            self.words[bit / 64] |= 1 << (bit % 64);
            position = position.wrapping_add(step);
        }
    }

    fn may_contain(&self, id: BlockId) -> bool {
        if self.words.is_empty() {
            return false;
        }
        let (mut position, step) = Self::hashes(id);
        for _ in 0..PACKED_FILTER_ROUNDS {
            let bit = (position as usize) & self.mask;
            if self.words[bit / 64] & (1 << (bit % 64)) == 0 {
                return false;
            }
            position = position.wrapping_add(step);
        }
        true
    }
}

/// Per-open exact locations for a bounded subset of active-WAL block IDs.
/// Dropped after replay; never a durable index or a cross-open authority.
#[derive(Default)]
pub(crate) struct CarriedRecoveryLookup {
    locations: BTreeMap<BlockId, (Option<PayloadSegmentReference>, bool)>,
    complete: bool,
}

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
    /// Loose block files physically written and fsynced.
    pub blocks_written: u64,
    pub bytes_written: u64,
    /// Payloads staged into the pending set (memory-only until the next
    /// checkpoint materializes them). `bytes_staged` uses the deterministic
    /// encoded length (header + payload + footer), matching `bytes_written`.
    pub blocks_staged: u64,
    pub bytes_staged: u64,
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
        for (index, pair) in value.as_bytes().as_chunks::<2>().0.iter().enumerate() {
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

/// Default byte budget for the verified block payload cache. Content is
/// immutable and addressed by its hash, so a hit always returns the exact
/// bytes the identifier names; the bound keeps the cache inside the personal
/// resource budget on long-lived authorities.
const DEFAULT_BLOCK_CACHE_BYTES: usize = 32 * 1024 * 1024;

/// Byte-bounded LRU over verified block payloads. Population happens only
/// after the full on-disk verification path (magic, version, CRC32C, content
/// hash) has run, never at write time, so a corruption test that rewrites a
/// block behind the store still observes the failure on its next read.
struct BlockPayloadCache {
    entries: HashMap<BlockId, (Arc<Vec<u8>>, u64)>,
    lru: VecDeque<(BlockId, u64)>,
    resident_bytes: usize,
    capacity_bytes: usize,
    tick: u64,
}

impl BlockPayloadCache {
    fn new(capacity_bytes: usize) -> Self {
        Self {
            entries: HashMap::new(),
            lru: VecDeque::new(),
            resident_bytes: 0,
            capacity_bytes,
            tick: 0,
        }
    }

    fn get(&mut self, id: BlockId) -> Option<Arc<Vec<u8>>> {
        let (payload, _) = self.entries.get(&id)?;
        let payload = Arc::clone(payload);
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.entries.insert(id, (Arc::clone(&payload), tick));
        self.compact_recency();
        self.lru.push_back((id, tick));
        Some(payload)
    }

    /// Read-only lookup for probe paths: a hit skips the recency update a
    /// full `get` would perform, and a miss never needed it.
    fn peek_shared(&self, id: BlockId) -> Option<Arc<Vec<u8>>> {
        self.entries
            .get(&id)
            .map(|(payload, _)| Arc::clone(payload))
    }

    fn insert_shared(&mut self, id: BlockId, payload: Arc<Vec<u8>>) {
        if self.capacity_bytes == 0 || payload.capacity() > self.capacity_bytes {
            return;
        }
        if let Some((existing, _)) = self.entries.remove(&id) {
            self.resident_bytes -= existing.capacity();
        }
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.resident_bytes += payload.capacity();
        self.entries.insert(id, (payload, tick));
        self.compact_recency();
        self.lru.push_back((id, tick));
        self.evict_to_budget();
    }

    fn insert(&mut self, id: BlockId, payload: &[u8]) {
        if self.capacity_bytes == 0 || payload.len() > self.capacity_bytes {
            return;
        }
        self.insert_shared(id, Arc::new(payload.to_vec()));
    }

    fn remove(&mut self, id: BlockId) {
        if let Some((payload, _)) = self.entries.remove(&id) {
            self.resident_bytes -= payload.capacity();
        }
        self.compact_recency();
    }

    fn clear(&mut self) {
        self.entries.clear();
        self.lru.clear();
        self.resident_bytes = 0;
    }

    fn set_capacity_bytes(&mut self, capacity_bytes: usize) {
        self.capacity_bytes = capacity_bytes;
        if capacity_bytes == 0 {
            self.entries = HashMap::new();
            self.lru = VecDeque::new();
            self.resident_bytes = 0;
            return;
        }
        self.evict_to_budget();
    }

    fn compact_recency(&mut self) {
        let limit = self
            .entries
            .len()
            .saturating_mul(2)
            .min(MAX_BLOCK_PAYLOAD_CACHE_RECENCY_RECORDS);
        if self.lru.len() >= limit {
            self.lru.retain(|(id, previous_tick)| {
                self.entries
                    .get(id)
                    .is_some_and(|(_, current_tick)| current_tick == previous_tick)
            });
        }
    }

    fn evict_to_budget(&mut self) {
        while self.resident_bytes > self.capacity_bytes
            || self.entries.len() > MAX_BLOCK_PAYLOAD_CACHE_ENTRIES
        {
            let Some((candidate, candidate_tick)) = self.lru.pop_front() else {
                break;
            };
            match self.entries.get(&candidate) {
                Some((_, tick)) if *tick == candidate_tick => {
                    if let Some((payload, _)) = self.entries.remove(&candidate) {
                        self.resident_bytes -= payload.capacity();
                    }
                }
                _ => continue,
            }
        }
        self.compact_recency();
    }
}

/// Immutable source whose identity was computed before private capture.
/// The borrow forbids mutation until capture; fields have no public setters.
pub(crate) struct PreparedBlockSource<'a> { id: BlockId, encoded: &'a [u8] }
impl<'a> PreparedBlockSource<'a> {
    pub(crate) fn new(encoded: &'a [u8]) -> io::Result<Self> {
        if encoded.len() > MAX_BLOCK_BYTES {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "prepared block exceeds maximum size"));
        }
        Ok(Self { id: BlockId::for_payload(encoded), encoded })
    }
    pub(crate) fn id(&self) -> BlockId { self.id }
    pub(crate) fn capture(self) -> PreparedBlockPayload {
        PreparedBlockPayload { id: self.id, bytes: Arc::new(self.encoded.to_vec()) }
    }

    /// Hash the exact owned immutable allocation before entering publication.
    /// A later mutable Arc clone receives a different allocation through COW.
    pub(crate) fn capture_shared(bytes: Arc<Vec<u8>>) -> io::Result<PreparedBlockPayload> {
        let id = PreparedBlockSource::new(bytes.as_slice())?.id();
        Ok(PreparedBlockPayload { id, bytes })
    }
}

/// Content identity is bound to this immutable allocation. Shared readers
/// can clone the Arc; mutation of a clone creates a different allocation.
/// Only PreparedBlockSource capture methods mint a payload for publication.
pub(crate) struct PreparedBlockPayload { id: BlockId, bytes: Arc<Vec<u8>> }
impl PreparedBlockPayload {
    pub(crate) fn id(&self) -> BlockId { self.id }
    pub(crate) fn shared(&self) -> Arc<Vec<u8>> { Arc::clone(&self.bytes) }
}

enum DeferredBlockInput<'a> { Bytes(Cow<'a, [u8]>), Prepared(&'a PreparedBlockPayload) }
impl AsRef<[u8]> for DeferredBlockInput<'_> {
    fn as_ref(&self) -> &[u8] { match self { Self::Bytes(bytes) => bytes.as_ref(), Self::Prepared(payload) => payload.bytes.as_slice() } }
}
impl DeferredBlockInput<'_> {
    fn len(&self) -> usize { self.as_ref().len() }
    fn id(&self) -> BlockId { match self { Self::Bytes(bytes) => BlockId::for_payload(bytes), Self::Prepared(payload) => payload.id } }
    fn into_shared(self) -> Arc<Vec<u8>> { match self { Self::Bytes(bytes) => Arc::new(bytes.into_owned()), Self::Prepared(payload) => payload.shared() } }
}

/// WAL-backed blocks not yet owned by an ordinary file or indexed history.
/// Synchronous checkpoints materialize them; online rotation publishes verified
/// locations inside immutable history before releasing these RAM copies.
/// `sync_only` names identifier-only files requiring a barrier before CONTROL.
/// Staged memory remains accounted across covered-prefix and live-window maps.
#[derive(Default)]
struct PendingBlocks {
    payloads: BTreeMap<BlockId, Arc<Vec<u8>>>,
    /// Covered-prefix staging transferred at rotation prepare. Indexed history
    /// publication releases its carried blocks; unpublished residuals return to
    /// the live map. In-flight pack plans retain their own Arc buffers.
    rotation_draining: BTreeMap<BlockId, Arc<Vec<u8>>>,
    sync_only: BTreeSet<BlockId>,
    /// Running staged-byte total for the packer's load gate. Maintained
    /// incrementally at every mutation: rescanning the whole set per call
    /// made the gate O(pending) per pack tick on large-write workloads.
    staged_bytes: u64,
}

impl PendingBlocks {
    /// Current staged (not-yet-durable) payload bytes, including the
    /// rotation-draining prefix. Feeds the packer's defer-under-load gate.
    fn total_staged_bytes(&self) -> u64 {
        self.staged_bytes
    }

    fn remove(&mut self, id: BlockId) {
        for removed in [
            self.payloads.remove(&id),
            self.rotation_draining.remove(&id),
        ]
        .into_iter()
        .flatten()
        {
            self.staged_bytes = self.staged_bytes.saturating_sub(removed.len() as u64);
        }
        self.sync_only.remove(&id);
    }

    fn insert_staged(&mut self, id: BlockId, payload: Arc<Vec<u8>>) -> bool {
        if self.payloads.contains_key(&id) {
            return false;
        }
        self.staged_bytes = self.staged_bytes.saturating_add(payload.len() as u64);
        self.payloads.insert(id, payload);
        true
    }

    /// Subtracts the byte total of every staged payload in `ids` and drops
    /// them; used by batch retire/flush paths.
    fn retire_staged(&mut self, ids: &BTreeSet<BlockId>) {
        for id in ids {
            self.remove(*id);
        }
    }
}

/// One off-authority payload-segment pack. A background worker builds the
/// segment file while commits keep staging; only an authority-locked caller
/// may publish the manifest reference and retire the staged copies. While a
/// pack is recorded here its block IDs are excluded from foreground
/// materialization snapshots, so a staged payload is never routed into two
/// segments. The state is in-memory only: after a crash the unpublished
/// segment file is an orphan and the WAL-carried payloads are re-staged.
enum BackgroundPack {
    Creating {
        segment_id: Uuid,
        ids: BTreeSet<BlockId>,
    },
    Completed {
        reference: PayloadSegmentReference,
        packed: BTreeSet<BlockId>,
    },
    Publishing {
        segment_id: Uuid,
        merge_id: Option<Uuid>,
    },
}

impl BackgroundPack {
    fn ids(&self) -> Option<&BTreeSet<BlockId>> {
        match self {
            Self::Creating { ids, .. } => Some(ids),
            Self::Completed { packed, .. } => Some(packed),
            Self::Publishing { .. } => None,
        }
    }

    fn segment_id(&self) -> Uuid {
        match self {
            Self::Creating { segment_id, .. } => *segment_id,
            Self::Completed { reference, .. } => reference.segment_id,
            Self::Publishing { segment_id, .. } => *segment_id,
        }
    }
}

/// In-memory pack slots for one store: the sole in-flight reservation plus
/// the one the checkpoint drain most recently stole. Both transitions are
/// guarded by the same mutex so a completion can never race the abandonment
/// record it is checked against.
#[derive(Default)]
struct BackgroundPackSlots {
    active: Option<BackgroundPack>,
    /// Set by `fail_active_background_pack` when the drain's wait budget ran
    /// out with a create still in flight. The worker finishing that create
    /// must hear "stale" (its payloads were already packed by the
    /// foreground), never a hard mismatch error: a spurious failure round
    /// backs the worker off exactly while WAL retirement is behind, and the
    /// built segment would leak as an orphan.
    abandoned: Option<Uuid>,
}

/// Verdict of `BlockStore::complete_background_pack`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BackgroundPackCompletion {
    /// The reservation was still active; the segment awaits publication.
    Published,
    /// The checkpoint drain already packed these payloads in the foreground
    /// after its wait budget ran out. The segment is redundant: the caller
    /// reclaims its file and counts a discard, never a failure round.
    Stale,
}

/// Holds the sole pack reservation while the authority publishes or aborts.
/// The IDs move to the caller; this guard adds no payload/ID-set copy.
pub(crate) struct BackgroundPackPublication<'a> {
    store: &'a BlockStore,
    segment_id: Uuid,
}

type BackgroundPackHandoff<'a> = (
    PayloadSegmentReference,
    BTreeSet<BlockId>,
    BackgroundPackPublication<'a>,
);

impl Drop for BackgroundPackPublication<'_> {
    fn drop(&mut self) {
        let _ = self.store.fail_background_pack(self.segment_id);
    }
}

/// One live worker per store. The worker owns this registration, so spawn
/// failure, normal exit, and unwinding all restore synchronous preparation.
pub(crate) struct BackgroundPackerRegistration(Arc<BlockStore>);

impl Drop for BackgroundPackerRegistration {
    fn drop(&mut self) {
        self.0
            .background_packer_running
            .store(false, Ordering::Release);
    }
}

/// Immutable source framing is checked when opening. Each point read still
/// verifies its exact content ID. Handles share the existing cache budget at
/// one slot per maximum-sized history block, and never retain full sources.
struct HistorySourceHandle {
    file: HistorySourceReader,
    source_bytes: u32,
}
enum HistorySourceReader {
    Positioned(Arc<dyn VfsPositionedReader>),
    Cursor(Mutex<Box<dyn VfsFile>>),
}
impl HistorySourceReader {
    fn len(&self) -> io::Result<u64> {
        match self {
            Self::Positioned(file) => file.len(),
            Self::Cursor(file) => file.lock()
                .map_err(|_| io::Error::other("history source file lock poisoned"))?.len(),
        }
    }
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        match self {
            Self::Positioned(file) => {
                let _read = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_HISTORY_SOURCE_READ);
                file.read_exact_at(offset, bytes)
            },
            Self::Cursor(file) => {
                let waiting = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_HISTORY_SOURCE_WAIT);
                let mut file = file.lock().map_err(|_| io::Error::other("history source file lock poisoned"))?;
                drop(waiting);
                let _read = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_HISTORY_SOURCE_READ);
                file.read_exact_at(offset, bytes)
            },
        }
    }
}
#[derive(Default)]
struct HistorySourceHandles {
    entries: BTreeMap<BlockId, (Arc<HistorySourceHandle>, u64)>,
    clock: u64,
}
impl HistorySourceHandles {
    fn trim(&mut self, maximum: usize) {
        while self.entries.len() > maximum {
            let id = self.entries.iter().min_by_key(|(_, (_, age))| age).map(|(id, _)| *id).unwrap();
            self.entries.remove(&id);
        }
    }
    fn get(&mut self, id: BlockId) -> Option<Arc<HistorySourceHandle>> {
        self.clock = self.clock.saturating_add(1);
        self.entries.get_mut(&id).map(|(handle, age)| { *age = self.clock; Arc::clone(handle) })
    }
}

pub struct BlockStore {
    root: PathBuf,
    vfs: Arc<dyn Vfs>,
    cache: RwLock<BlockPayloadCache>,
    payload_segments: Option<PayloadSegmentStore>,
    payload_manifest: RwLock<Option<PayloadManifest>>,
    history_payloads: RwLock<Option<Arc<crate::history_payload::Catalog>>>,
    history_source_handles: Mutex<HistorySourceHandles>,
    history_source_handle_limit: AtomicUsize,
    payload_routing: RwLock<crate::payload_routing::PayloadRouting>,
    read_hints: RwLock<crate::payload_hints::PayloadHints>,
    packed_filter: RwLock<PackedBlockFilter>,
    next_payload_catalog_publication: AtomicU64,
    installed_payload_catalog_publication: AtomicU64,
    pending: Mutex<PendingBlocks>,
    background_pack: Mutex<BackgroundPackSlots>,
    background_pack_signal: Condvar,
    background_pack_revision: AtomicU64,
    foreground_drain_requested: AtomicBool,
    background_packer_running: AtomicBool,
    background_drain_requested: AtomicBool,
    blocks_written: AtomicU64,
    bytes_written: AtomicU64,
    blocks_staged: AtomicU64,
    bytes_staged: AtomicU64,
    relink_fence: Mutex<RelinkFence>,
}

/// Online-GC relink fence (docs/TOFUDB_ONLINE_GC.md rule S7). Every
/// reference-without-write decision (dedupe probe hit, payload-cache hit,
/// idempotent loose republish) records its block ID here between an epoch's
/// mark snapshot and its deletion holds, so the online collector never
/// deletes a block that a commit silently re-referenced after the mark.
/// All such decisions serialise under the authority mutex; the fence is
/// in-memory only and never survives a process restart (a restarted daemon
/// starts a fresh epoch that confirms nothing until its second mark).
#[derive(Default)]
struct RelinkFence {
    ids: HashSet<BlockId>,
    overflow: bool,
}

/// Hard bound on relink-fence membership between two epoch holds. Overflow
/// never loses a re-reference: it flips the conservative flag and every
/// deletion batch defers until the next epoch re-arms the fence.
pub(crate) const MAX_RELINK_FENCE_IDS: usize = 16_384;

impl RelinkFence {
    fn note(&mut self, id: BlockId) {
        if self.overflow || self.ids.contains(&id) {
            return;
        }
        if self.ids.len() >= MAX_RELINK_FENCE_IDS {
            self.overflow = true;
            return;
        }
        self.ids.insert(id);
    }

    fn rearm(&mut self) {
        self.ids.clear();
        self.overflow = false;
    }
}

/// Snapshot of fence state consulted by one online-GC deletion hold.
#[derive(Default)]
pub(crate) struct RelinkFenceSnapshot {
    pub overflow: bool,
    pub ids: HashSet<BlockId>,
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

/// Staged-payload snapshot for checkpoint materialization: every staged
/// payload paired with its block ID, plus the fsync-only recovery marks.
type PendingCheckpointSnapshot = (Vec<(BlockId, Arc<Vec<u8>>)>, Vec<BlockId>);
/// Outcome of one bounded dedupe probe.
enum DedupeProbe {
    /// Payload bytes resolved through the candidate segments or the loose
    /// path; callers still byte-compare against the incoming payload.
    Present(Vec<u8>),
    /// Neither a validated segment index nor the loose path holds the id.
    /// A union-filter hit that no candidate scan can confirm is a bloom
    /// false positive, never proof of existence: the caller stages the
    /// payload (idempotent by content ID), so a write can never reference
    /// a block that durable storage does not actually hold.
    Missing,
    }


impl BlockStore {
    pub(crate) fn register_background_packer(
        self: &Arc<Self>,
    ) -> io::Result<BackgroundPackerRegistration> {
        self.background_packer_running
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| {
                io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "background packer already running",
                )
            })?;
        Ok(BackgroundPackerRegistration(Arc::clone(self)))
    }

    pub(crate) fn background_packer_running(&self) -> bool {
        self.background_packer_running.load(Ordering::Acquire)
    }

    pub(crate) fn request_background_drain(&self) {
        self.background_drain_requested.store(true, Ordering::Release);
        let _ = self.wake_background_packer();
    }

    pub(crate) fn background_drain_requested(&self) -> bool {
        self.background_drain_requested.load(Ordering::Acquire)
    }

    /// The rotation install has fenced writers until payload publication.
    /// Unlike early background draining, this work is on the foreground path.
    pub(crate) fn request_foreground_drain(&self) {
        self.foreground_drain_requested.store(true, Ordering::Release);
        self.request_background_drain();
    }

    pub(crate) fn foreground_drain_requested(&self) -> bool {
        self.foreground_drain_requested.load(Ordering::Acquire)
    }

    pub(crate) fn finish_background_drain(&self) {
        self.foreground_drain_requested.store(false, Ordering::Release);
        self.background_drain_requested.store(false, Ordering::Release);
    }

    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("blocks");
        vfs.create_dir(&root)?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        let payload_segments =
            PayloadSegmentStore::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        let read_hints = crate::payload_hints::PayloadHints::new(payload_segments.routing_budget_bytes());
        Ok(Self {
            root,
            vfs,
            cache: RwLock::new(BlockPayloadCache::new(DEFAULT_BLOCK_CACHE_BYTES)),
            payload_segments: Some(payload_segments),
            read_hints: RwLock::new(read_hints),
            payload_manifest: RwLock::new(None),
            history_payloads: RwLock::new(None),
            history_source_handles: Mutex::new(HistorySourceHandles::default()),
            history_source_handle_limit: AtomicUsize::new(DEFAULT_BLOCK_CACHE_BYTES / MAX_BLOCK_BYTES),
            payload_routing: RwLock::new(Default::default()),
            packed_filter: RwLock::new(PackedBlockFilter::empty()),
            next_payload_catalog_publication: AtomicU64::new(0),
            installed_payload_catalog_publication: AtomicU64::new(0),
            pending: Mutex::new(PendingBlocks::default()),
            background_pack: Mutex::new(BackgroundPackSlots::default()),
            background_pack_signal: Condvar::new(),
            background_pack_revision: AtomicU64::new(0),
            foreground_drain_requested: AtomicBool::new(false),
            background_packer_running: AtomicBool::new(false),
            background_drain_requested: AtomicBool::new(false),
            blocks_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
            blocks_staged: AtomicU64::new(0),
            bytes_staged: AtomicU64::new(0),
            relink_fence: Mutex::new(RelinkFence::default()),
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
        let read_hints = crate::payload_hints::PayloadHints::load(data_dir, vfs.as_ref(),
            payload_segments.as_ref().map_or(0, |store| store.routing_budget_bytes()));
        Ok(Self {
            root,
            vfs,
            cache: RwLock::new(BlockPayloadCache::new(DEFAULT_BLOCK_CACHE_BYTES)),
            payload_segments,
            read_hints: RwLock::new(read_hints),
            payload_manifest: RwLock::new(None),
            history_payloads: RwLock::new(None),
            history_source_handles: Mutex::new(HistorySourceHandles::default()),
            history_source_handle_limit: AtomicUsize::new(DEFAULT_BLOCK_CACHE_BYTES / MAX_BLOCK_BYTES),
            payload_routing: RwLock::new(Default::default()),
            packed_filter: RwLock::new(PackedBlockFilter::empty()),
            next_payload_catalog_publication: AtomicU64::new(0),
            installed_payload_catalog_publication: AtomicU64::new(0),
            pending: Mutex::new(PendingBlocks::default()),
            background_pack: Mutex::new(BackgroundPackSlots::default()),
            background_pack_signal: Condvar::new(),
            background_pack_revision: AtomicU64::new(0),
            foreground_drain_requested: AtomicBool::new(false),
            background_packer_running: AtomicBool::new(false),
            background_drain_requested: AtomicBool::new(false),
            blocks_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
            blocks_staged: AtomicU64::new(0),
            bytes_staged: AtomicU64::new(0),
            relink_fence: Mutex::new(RelinkFence::default()),
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

    fn note_relink(&self, id: BlockId) {
        let mut fence = self
            .relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        fence.note(id);
    }

    /// Online-GC epoch boundary (docs/TOFUDB_ONLINE_GC.md rule S7): re-arms
    /// the fence under the epoch's mark-snapshot hold so it accumulates
    /// exactly the re-references decided after that snapshot.
    pub(crate) fn begin_relink_observation(&self) {
        self.relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .rearm();
    }

    /// Fence state for one online-GC deletion hold. A poisoned lock is
    /// recovered as overflow, which conservatively defers the batch.
    pub(crate) fn relink_fence_snapshot(&self) -> RelinkFenceSnapshot {
        let fence = self
            .relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        RelinkFenceSnapshot {
            overflow: fence.overflow,
            ids: fence.ids.clone(),
        }
    }

    pub fn put(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        match self.get(id) {
            Ok(existing) => {
                if existing != payload {
                    return Err(invalid_data("content-addressed block collision"));
                }
                self.note_relink(id);
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
                self.pending
                    .lock()
                    .map_err(|_| io::Error::other("pending block set poisoned"))?
                    .remove(id);
                self.note_relink(id);
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
        self.pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .remove(id);
        Ok(id)
    }

    /// Stages the payload in memory without touching the disk. The engine
    /// carries staged payloads in the WAL envelope, so the commit's WAL fsync
    /// is the durability point; the next checkpoint materializes every staged
    /// block (packed into payload segments where the catalog admits them,
    /// loose files otherwise — see `flush_pending_subset`).
    /// Reads resolve staged payloads through `get`, so GC planning,
    /// compaction, and backup observe committed content through the same
    /// read path as the durable store.
    /// Returns `(id, true)` when the payload is not yet durable on disk
    /// (newly staged or staged by an earlier write in the same checkpoint
    /// window), `(id, false)` when durable content already exists. The
    /// engine must carry the payload in its WAL record whenever this returns
    /// `true`.
    pub(crate) fn put_deferred(&self, payload: &[u8]) -> io::Result<(BlockId, bool)> {
        self.put_deferred_content(Cow::Borrowed(payload), true)
    }

    /// Blob writes already supply complete content for their WAL witness.
    /// Reuse verified cached/packed copies, but do not issue a network stat
    /// merely to discover an optional loose duplicate. A loose duplicate is
    /// safely carried again and ordinary materialization verifies/reuses it.
    pub(crate) fn put_deferred_blob(&self, payload: &[u8]) -> io::Result<(BlockId, bool)> {
        self.put_deferred_content(Cow::Borrowed(payload), false)
    }

    pub(crate) fn put_deferred_prepared(&self, payload: &PreparedBlockPayload) -> io::Result<(BlockId, bool)> {
        self.put_deferred_input(DeferredBlockInput::Prepared(payload), false)
    }

    fn put_deferred_content(&self, payload: Cow<'_, [u8]>, probe_loose: bool) -> io::Result<(BlockId, bool)> {
        self.put_deferred_input(DeferredBlockInput::Bytes(payload), probe_loose)
    }

    fn put_deferred_input(&self, payload: DeferredBlockInput<'_>, probe_loose: bool) -> io::Result<(BlockId, bool)> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PUT_DEFERRED);
        let id = payload.id();
        {
            let pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            if pending.payloads.contains_key(&id)
                || pending.rotation_draining.contains_key(&id)
            {
                return Ok((id, true));
            }
        }
        // Durable probe that mirrors `get` without re-locking the pending set
        // or taking the cache write lock on the miss path. The early pending
        // check above already proved the block is not staged, and `get`'s
        // pending re-probe can only observe the same answer.
        let cache_hit = {
            let cache = self
                .cache
                .read()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
            cache.peek_shared(id)
        };
        if let Some(existing) = cache_hit {
            if existing.as_slice() != payload.as_ref() {
                return Err(invalid_data("content-addressed block collision"));
            }
            self.note_relink(id);
            return Ok((id, false));
        }
        let existing = if probe_loose { self.probe_dedupe(id)? }
            else {
                crate::request_profile::work(crate::request_profile::Work::ColdHistoryDedupeBypass, 1);
                self.probe_packed_dedupe(id)?
            };
        match existing {
            DedupeProbe::Present(existing) => {
                if existing.as_slice() != payload.as_ref() {
                    return Err(invalid_data("content-addressed block collision"));
                }
                self.cache
                    .write()
                    .map_err(|_| io::Error::other("block cache lock was poisoned"))?
                    .insert(id, &existing);
                self.note_relink(id);
                return Ok((id, false));
            }
            // The probe stages on any unconfirmed filter hit: restaging an
            // already-packed block costs WAL bandwidth, while trusting the
            // bloom filter would reference a block that may not exist.
            DedupeProbe::Missing => {}
        }
        let stage_probe = std::time::Instant::now();
        self.stage_pending_input(id, payload)?;
        crate::fsync_stats::note_stage_elapsed(
            crate::fsync_stats::STAGE_PUT_STAGE,
            stage_probe.elapsed().as_nanos() as u64,
        );
        Ok((id, true))
    }

    /// Stages a payload the caller encoded for the first time (copy-on-write
    /// tree pages, freshly built documents, root directories). The durable
    /// dedup probe is skipped: such content can only coincidentally match an
    /// existing block, staging stays idempotent through the pending-set entry
    /// API, and a duplicate carry costs WAL bandwidth, never correctness.
    /// Fresh-page semantics with identity/allocation captured off authority.
    /// Preserve pending/rotation staging; never add a durable dedup probe.
    pub(crate) fn put_deferred_fresh_prepared(&self, payload: &PreparedBlockPayload) -> io::Result<(BlockId, bool)> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PUT_DEFERRED);
        self.stage_pending_input(payload.id, DeferredBlockInput::Prepared(payload))?;
        Ok((payload.id, true))
    }

    pub(crate) fn put_deferred_fresh(&self, payload: &[u8]) -> io::Result<(BlockId, bool)> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PUT_DEFERRED);
        let id = BlockId::for_payload(payload);
        self.stage_pending_payload(id, Cow::Borrowed(payload))?;
        Ok((id, true))
    }

    /// Owned-input variant of `put_deferred_fresh`; see that method for the
    /// freshness contract.
    pub(crate) fn put_deferred_fresh_owned(&self, payload: Vec<u8>) -> io::Result<(BlockId, bool)> {
        if payload.len() > MAX_BLOCK_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "owned staged block exceeds maximum size",
            ));
        }
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PUT_DEFERRED);
        let mut payload = payload;
        if payload.capacity() > payload.len() {
            payload.shrink_to_fit();
        }
        let id = BlockId::for_payload(&payload);
        self.stage_pending_payload(id, Cow::Owned(payload))?;
        Ok((id, true))
    }
    /// Stages one checkpoint-history block reconstructed from the durable
    /// active WAL. Unlike a new user block, its encoded bytes need not be
    /// carried in another WAL record. Engine bounds this to one pending
    /// history segment and must materialize it before retiring its source WAL.
    /// Takes the encoder's allocation directly; staging never duplicates the
    /// complete history segment while holding the pending-block mutex.
    pub(crate) fn stage_checkpoint_history(&self, payload: Vec<u8>) -> io::Result<BlockId> {
        if payload.len() > MAX_BLOCK_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "checkpoint history exceeds segment bound",
            ));
        }
        let mut payload = payload;
        if payload.capacity() > payload.len() {
            payload.shrink_to_fit();
        }
        let (id, pending) = self.put_deferred_content(Cow::Owned(payload), true)?;
        if pending {
            self.wake_background_packer()?;
        }
        Ok(id)
    }

    /// Re-stages a WAL-carried payload during recovery. The caller reaches
    /// this only after the durable read path failed (missing or torn file),
    /// so no trustworthy on-disk copy exists; the checkpoint's materialize
    /// step publishes a complete replacement. The WAL record that supplied
    /// the payload is already durable, so nothing is lost if the process
    /// crashes before that checkpoint.
    pub(crate) fn restore_loose(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        self.stage_pending_payload(id, Cow::Borrowed(payload))?;
        Ok(id)
    }

    /// Recover a carried WAL block without re-packing a verified published
    /// copy on every reopen. Only catalogued segments are probed; loose-file
    /// misses still stage directly from WAL, avoiding per-block loose I/O.
    /// Membership filters alone never authorize dropping the recovery copy.
    pub(crate) fn restore_carried(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        match self.get_from_history(id) {
            Ok(Some(existing)) if existing == payload => return Ok(id),
            Ok(Some(_)) => return Err(invalid_data("carried and history block differ")),
            Ok(None) => {}
            Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {}
            Err(error) => return Err(error),
        }
        if self.packed_filter_may_contain(id)? {
            match self.get_from_payload_segments(id) {
                Ok(Some(existing)) => {
                    if existing != payload {
                        return Err(invalid_data("carried and catalogued block differ"));
                    }
                    return Ok(id);
                }
                Ok(None) => {}
                Err(error) if matches!(error.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {}
                Err(error) => return Err(error),
            }
        }
        self.restore_loose(payload)
    }

    fn payload_catalog_snapshot(&self) -> io::Result<(Option<PayloadManifest>, u64)> {
        let manifest = self.payload_manifest.read()
            .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
        Ok((manifest.clone(), self.installed_payload_catalog_publication.load(Ordering::Acquire)))
    }

    #[cfg(test)]
    pub(crate) fn prepare_carried_recovery(
        &self, ids: BTreeSet<BlockId>,
    ) -> io::Result<CarriedRecoveryLookup> {
        self.prepare_carried_recovery_until(ids, &|| false)
    }

    pub(crate) fn prepare_carried_recovery_until(
        &self, ids: BTreeSet<BlockId>, should_stop: &dyn Fn() -> bool,
    ) -> io::Result<CarriedRecoveryLookup> {
        if ids.len() > crate::generated_tofudb_ir::MAX_RECOVERY_PACKED_LOCATIONS {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "recovery location budget exceeded"));
        }
        let mut lookup = CarriedRecoveryLookup {
            locations: ids.into_iter().map(|id| (id, (None, false))).collect(),
            complete: true,
        };
        let (manifest, publication) = self.payload_catalog_snapshot()?;
        if let Some(manifest) = manifest {
            let mut verified_routes = crate::payload_routing::PayloadRouting::default();
            let mut filter = self.scan_packed_filter_until(&manifest, Some(&mut lookup), Some(&mut verified_routes), should_stop);
            if should_stop() { return Err(io::Error::new(io::ErrorKind::Interrupted, "recovery catalog scan cancelled")); }
            filter.covered_catalog_publication = publication;
            let store = self.payload_segments.as_ref();
            let routing = crate::payload_routing::PayloadRouting::build(&manifest, store, &verified_routes,
                store.map_or(0, |store| store.routing_budget_bytes()), false);
            let current = self.payload_manifest.read().map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
            if current.as_ref() == Some(&manifest)
                && self.installed_payload_catalog_publication.load(Ordering::Acquire) == publication {
                *self.payload_routing.write().map_err(|_| io::Error::other("payload routing lock was poisoned"))? = routing;
            }
            drop(current);
            lookup.complete = filter.built;
            *self.packed_filter.write()
                .map_err(|_| io::Error::other("packed block filter lock was poisoned"))? = filter;
        }
        Ok(lookup)
    }

    pub(crate) fn restore_carried_with_lookup(
        &self, payload: &[u8], lookup: &mut CarriedRecoveryLookup,
    ) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        match self.get_from_history(id) {
            Ok(Some(existing)) if existing == payload => return Ok(id),
            Ok(Some(_)) => return Err(invalid_data("carried and history block differ")),
            Ok(None) => {}
            Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {}
            Err(error) => return Err(error),
        }
        let Some((reference, verified)) = lookup.locations.get_mut(&id) else {
            return self.restore_carried(payload);
        };
        if *verified { return Ok(id); }
        if let Some(reference) = reference {
            match self.get_from_payload_segment_references(id, std::slice::from_ref(reference)) {
                Ok(Some(existing)) => {
                    if existing != payload {
                        return Err(invalid_data("carried and catalogued block differ"));
                    }
                    *verified = true;
                    return Ok(id);
                }
                Ok(None) => {}
                Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {}
                Err(error) => return Err(error),
            }
        } else if !lookup.complete {
            return self.restore_carried(payload);
        }
        let restored = self.restore_loose(payload)?;
        *verified = true;
        Ok(restored)
    }

    /// The exclusive startup pass already located these WAL references in
    /// verified segment indexes. Reuse that exact location even when the
    /// bounded process-wide routing cache cannot retain the entire catalog.
    pub(crate) fn get_for_recovery(
        &self, id: BlockId, lookup: &CarriedRecoveryLookup,
    ) -> io::Result<Vec<u8>> {
        if let Some(payload) = self.pending_payload(id)? {
            return Ok((*payload).clone());
        }
        if let Some((Some(reference), _)) = lookup.locations.get(&id) {
            if let Some(payload) = self.get_from_payload_segment_references(id, std::slice::from_ref(reference))? {
                self.cache.write().map_err(|_| io::Error::other("block cache lock was poisoned"))?
                    .insert(id, &payload);
                return Ok(payload);
            }
        }
        // Overflow or an incomplete index scan retains the normal verified
        // fallback. A lookup never fabricates existence or skips byte checks.
        self.get(id)
    }

    fn stage_pending_payload(&self, id: BlockId, payload: Cow<'_, [u8]>) -> io::Result<()> {
        self.stage_pending_input(id, DeferredBlockInput::Bytes(payload))
    }

    fn stage_pending_input(&self, id: BlockId, payload: DeferredBlockInput<'_>) -> io::Result<()> {
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        // Content addressing makes the covered generation's copy THE block:
        // window-era restaging of the same bytes (fresh CoW pages skip the
        // durable probe) must not duplicate it across the two generations,
        // or the pack batch fails its duplicate-ID validation forever.
        if pending.rotation_draining.contains_key(&id) {
            return Ok(());
        }
        let payload_len = payload.len();
        if pending.insert_staged(id, payload.into_shared()) {
            self.blocks_staged.fetch_add(1, Ordering::Relaxed);
            self.bytes_staged.fetch_add(
                (HEADER_BYTES + payload_len + FOOTER_BYTES) as u64,
                Ordering::Relaxed,
            );
        }
        Ok(())
    }

    /// Marks a loose file that read back complete as needing an fsync before
    /// the next checkpoint retires the WAL records that reference it.
    /// Recovery marks every replayed block this way because the file may
    /// predate the last power event.
    pub(crate) fn stage_pending_sync(&self, id: BlockId) -> io::Result<()> {
        let catalog = self.history_payloads.read().map_err(|_| io::Error::other("history payload catalog lock poisoned"))?.clone();
        if let Some(catalog) = catalog {
            if catalog.root.lookup(self, id)?.is_some() { return Ok(()); }
        }
        self.pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .sync_only
            .insert(id);
        Ok(())
    }

    /// Returns the staged copy of a not-yet-durable block, if any. Commit
    /// paths use this to carry pending payloads in their WAL envelopes.
    #[inline]
    pub(crate) fn pending_payload(&self, id: BlockId) -> io::Result<Option<Arc<Vec<u8>>>> {
        if crate::request_profile::enabled() {
            self.pending_payload_profiled(id)
        } else {
            self.pending_payload_measured::<false>(id)
        }
    }

    // Keep measured timer frames out of callers on the ordinary read path.
    #[cold]
    #[inline(never)]
    fn pending_payload_profiled(&self, id: BlockId) -> io::Result<Option<Arc<Vec<u8>>>> {
        self.pending_payload_measured::<true>(id)
    }

    #[inline(always)]
    fn pending_payload_measured<const MEASURE: bool>(&self, id: BlockId) -> io::Result<Option<Arc<Vec<u8>>>> {
        let waiting = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_PENDING_WAIT);
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        drop(waiting);
        Ok(pending
            .payloads
            .get(&id)
            .or_else(|| pending.rotation_draining.get(&id))
            .cloned())
    }

    /// Materializes every staged payload as a loose file and fsyncs all
    /// pending blocks plus their shard directories in one batch. Called by
    /// oversized commits that spill back to identifier-only references; the
    /// checkpoint path materializes staged blocks into payload segments
    /// instead and only routes the catalog-overflow minority through the
    /// loose batch. On error nothing is cleared, so the next attempt
    /// retries the full set idempotently.
    pub(crate) fn flush_pending(&self) -> io::Result<()> {
        let materialize: BTreeSet<BlockId> = {
            let pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            pending
                .payloads
                .keys()
                .chain(pending.rotation_draining.keys())
                .copied()
                .collect()
        };
        self.flush_pending_subset(&materialize)
    }

    /// Includes every staged payload, even when a background reservation owns
    /// its materialization. A completed segment is pending until publication.
    /// Staged-not-durable byte total for the packer's load gate.
    pub(crate) fn pending_staged_bytes(&self) -> io::Result<u64> {
        Ok(self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .total_staged_bytes())
    }

    pub(crate) fn has_pending_payloads(&self) -> io::Result<bool> {
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        Ok(!pending.payloads.is_empty() || !pending.rotation_draining.is_empty())
    }

    /// True while the covered prefix still owns staged blocks: the rotation
    /// write fence and the install defer both key off this set and nothing
    /// else, so window-commit staging never extends either.
    pub(crate) fn has_rotation_drain_pending(&self) -> io::Result<bool> {
        Ok(!self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .rotation_draining
            .is_empty())
    }

    /// Rotation prepare rotates the current staged set into the covered
    /// generation (see `rotation_draining`). Idempotent for an empty set;
    /// a still-draining predecessor (deferred install re-prepare) is an
    /// error, never silently merged across covered snapshots.
    pub(crate) fn rotate_pending_for_rotation(&self) -> io::Result<()> {
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        if !pending.rotation_draining.is_empty() {
            return Err(invalid_data(
                "rotation drain generation still owns staged blocks",
            ));
        }
        pending.rotation_draining = std::mem::take(&mut pending.payloads);
        Ok(())
    }

    /// Defer/failure handback: the covered generation rejoins the live set
    /// whole, exactly like `restore_rotation_snapshot` for transactions.
    pub(crate) fn restore_rotation_pending(&self) -> io::Result<()> {
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        for (id, payload) in std::mem::take(&mut pending.rotation_draining) {
            pending.payloads.entry(id).or_insert(payload);
        }
        Ok(())
    }

    /// Snapshot of staged (not yet durable) payloads and fsync-only marks
    /// for checkpoint materialization. Payloads come back sorted by block
    /// ID, so per-shard groups are already in segment index order. Blocks
    /// owned by an in-flight or completed-but-unpublished background pack
    /// are excluded: the background segment is their materialization, and
    /// routing them into a second segment would publish duplicate content.
    /// Lock order is background_pack before pending everywhere.
    pub(crate) fn pending_checkpoint_snapshot(&self) -> io::Result<PendingCheckpointSnapshot> {
        let background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        if matches!(background.active, Some(BackgroundPack::Publishing { .. })) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "background pack publication owns materialization",
            ));
        }
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        Ok((
            pending
                .payloads
                .iter()
                .chain(pending.rotation_draining.iter())
                .filter(|(id, _)| {
                    !background
                        .active
                        .as_ref()
                        .and_then(BackgroundPack::ids)
                        .is_some_and(|ids| ids.contains(id))
                })
                .map(|(id, payload)| (*id, Arc::clone(payload)))
                .collect(),
            pending.sync_only.iter().copied().collect(),
        ))
    }

    /// Reserves a staged batch for an off-authority segment build. Returns
    /// the fresh segment ID and the sorted batch (sorted IDs are already in
    /// segment index order). At most one pack is in flight at a time; the
    /// batch stays staged until `complete_background_pack` hands it to an
    /// authority-locked publication. Batches stopped by the `maximum_bytes`
    /// budget always qualify; `minimum_bytes` only filters undersized tail
    /// batches, which stay with the foreground checkpoint path. A lone
    /// payload larger than the budget is skipped (the foreground loose
    /// path owns it).
    pub(crate) fn begin_background_pack(
        &self,
        minimum_bytes: u64,
        maximum_bytes: u64,
    ) -> io::Result<Option<(Uuid, Vec<BlockId>)>> {
        self.begin_background_pack_with_id_source(minimum_bytes, maximum_bytes, Uuid::new_v4)
    }

    /// Fault probes and replay need the same catalog block hash/shard layout.
    /// Production still allocates a fresh UUID only after the batch qualifies.
    #[cfg(test)]
    pub(crate) fn begin_background_pack_with_test_id(
        &self, minimum_bytes: u64, maximum_bytes: u64, segment_id: Uuid,
    ) -> io::Result<Option<(Uuid, Vec<BlockId>)>> {
        self.begin_background_pack_with_id_source(minimum_bytes, maximum_bytes, || segment_id)
    }

    fn begin_background_pack_with_id_source(
        &self, minimum_bytes: u64, maximum_bytes: u64, next_id: impl FnOnce() -> Uuid,
    ) -> io::Result<Option<(Uuid, Vec<BlockId>)>> {
        if minimum_bytes == 0 || minimum_bytes > maximum_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "background pack byte bounds are invalid",
            ));
        }
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        if background.active.is_some() {
            return Ok(None);
        }
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        let mut ids = Vec::new();
        let mut bytes = 0_u64;
        let mut stopped_on_byte_cap = false;
        // The covered rotation generation drains first; ordinary staged
        // blocks follow only when no drain generation owns the batch.
        for (id, payload) in pending
            .rotation_draining
            .iter()
            .chain(pending.payloads.iter())
        {
            if ids.len() >= crate::payload_segment::MAX_SEGMENT_BLOCKS {
                break;
            }
            let len = payload.len() as u64;
            if bytes + len > maximum_bytes {
                if ids.is_empty() {
                    // A lone payload larger than the segment budget cannot
                    // join a capped batch; the foreground loose path owns it.
                    continue;
                }
                // The batch is as full as the byte budget allows: that alone
                // qualifies it, even below `minimum_bytes` — the minimum only
                // filters out undersized *tail* batches.
                stopped_on_byte_cap = true;
                break;
            }
            ids.push(*id);
            bytes += len;
        }
        if bytes < minimum_bytes && !stopped_on_byte_cap {
            return Ok(None);
        }
        let segment_id = next_id();
        background.active = Some(BackgroundPack::Creating {
            segment_id,
            ids: ids.iter().copied().collect(),
        });
        Ok(Some((segment_id, ids)))
    }

    /// Records the durably created segment for foreground publication. The
    /// caller has already fsynced the segment file and verified it against
    /// its reference, so the publication path never rebuilds it. A create
    /// whose reservation the checkpoint drain already stole returns
    /// `Stale`: its payloads reached durable storage through the foreground,
    /// so its segment is redundant and must be discarded quietly.
    pub(crate) fn complete_background_pack(
        &self,
        segment_id: Uuid,
        reference: PayloadSegmentReference,
    ) -> io::Result<BackgroundPackCompletion> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        let completion = match background.active.as_ref() {
            Some(BackgroundPack::Creating {
                segment_id: active,
                ids,
            }) if *active == segment_id => {
                let packed = ids.clone();
                background.active =
                    Some(BackgroundPack::Completed { reference, packed });
                BackgroundPackCompletion::Published
            }
            _ if background.abandoned == Some(segment_id) => {
                background.abandoned = None;
                BackgroundPackCompletion::Stale
            }
            _ => {
                return Err(invalid_data(
                    "background pack completion does not match the active pack",
                ))
            }
        };
        self.background_pack_revision
            .fetch_add(1, Ordering::Release);
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(completion)
    }

    /// Releases a failed or stale pack reservation; its payloads stay staged
    /// for the next (possibly foreground) materialization attempt.
    pub(crate) fn fail_background_pack(&self, segment_id: Uuid) -> io::Result<()> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        if background
            .active
            .as_ref()
            .is_some_and(|pack| pack.segment_id() == segment_id)
        {
            background.active = None;
        }
        self.background_pack_revision
            .fetch_add(1, Ordering::Release);
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(())
    }

    /// Abandons whatever pack reservation is active. The checkpoint drain
    /// uses this after its wait budget runs out; the same staged payloads
    /// are then routed by the foreground materialization step. A stolen
    /// create is remembered so its late completion is a quiet `Stale`
    /// verdict instead of a spurious hard mismatch.
    pub(crate) fn fail_active_background_pack(&self) -> io::Result<()> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        if let Some(BackgroundPack::Creating { segment_id, .. }) =
            background.active.take()
        {
            background.abandoned = Some(segment_id);
        }
        self.background_pack_revision
            .fetch_add(1, Ordering::Release);
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(())
    }

    /// Hands the finished pack to the authority-locked publication path.
    pub(crate) fn take_completed_background_pack(
        &self,
    ) -> io::Result<Option<BackgroundPackHandoff<'_>>> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        match background.active.take() {
            Some(BackgroundPack::Completed { reference, packed }) => {
                let segment_id = reference.segment_id;
                background.active = Some(BackgroundPack::Publishing { segment_id, merge_id: None });
                Ok(Some((
                    reference,
                    packed,
                    BackgroundPackPublication {
                        store: self,
                        segment_id,
                    },
                )))
            }
            other => {
                background.active = other;
                Ok(None)
            }
        }
    }

    /// Reclaims only a completed pack that no publisher has taken. The worker
    /// calls this before releasing its registration on exit. The same handoff
    /// guard excludes concurrent publication; staged/WAL source bytes remain.
    pub(crate) fn discard_completed_background_pack(&self) -> io::Result<Option<u64>> {
        let Some((reference, _packed, _publication)) = self.take_completed_background_pack()?
        else {
            return Ok(None);
        };
        self.payload_segment_store()?
            .remove_segment(reference.segment_id)
            .map(Some)
    }

    /// Test observation of a reservation still owned by an active create.
    #[cfg(test)]
    pub(crate) fn background_pack_creating(&self) -> io::Result<bool> {
        Ok(matches!(
            self.background_pack
                .lock()
                .map_err(|_| io::Error::other("background pack state poisoned"))?
                .active,
            Some(BackgroundPack::Creating { .. })
        ))
    }

    pub(crate) fn background_pack_revision(&self) -> u64 {
        self.background_pack_revision.load(Ordering::Acquire)
    }

    /// Signals new history work or shutdown. The revision changes under the
    /// wait mutex, so a notification between reservation and sleep is retained.
    pub(crate) fn wake_background_packer(&self) -> io::Result<()> {
        let background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        self.background_pack_revision
            .fetch_add(1, Ordering::Release);
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(())
    }

    /// Waits only while no publication, work arrival, or shutdown notification
    /// followed the worker's attempt. Returns whether the revision changed.
    /// The existing idle timeout bounds retries for ordinary sub-MiB payloads.
    pub(crate) fn wait_for_background_pack_change(
        &self,
        observed: u64,
        timeout: Duration,
    ) -> io::Result<bool> {
        let background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        let _ = self
            .background_pack_signal
            .wait_timeout_while(background, timeout, |_| {
                self.background_pack_revision() == observed
            })
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        Ok(self.background_pack_revision() != observed)
    }

    /// Waits for a background create to finish or fail. Callers bound their
    /// total wait with their own retry budget.
    pub(crate) fn wait_for_background_pack(&self, timeout: Duration) -> io::Result<()> {
        let background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        // Completion can precede this lock after the caller checks progress.
        // Checking under the condition mutex retains that notification.
        if matches!(background.active, Some(BackgroundPack::Creating { .. })) {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_WAIT);
            let _ = self
                .background_pack_signal
                .wait_timeout(background, timeout)
                .map_err(|_| io::Error::other("background pack state poisoned"))?;
        }
        Ok(())
    }

    /// Segment IDs whose files must survive orphan planning although no
    /// manifest references them yet.
    pub(crate) fn unpublished_background_segment_ids(&self) -> io::Result<Vec<Uuid>> {
        let background = self.background_pack.lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        let mut ids = Vec::with_capacity(2);
        if let Some(pack) = &background.active {
            ids.push(pack.segment_id());
            if let BackgroundPack::Publishing { merge_id: Some(id), .. } = pack {
                ids.push(*id);
            }
        }
        Ok(ids)
    }

    pub(crate) fn reserve_background_merge(&self, segment_id: Uuid) -> io::Result<Uuid> {
        let mut background = self.background_pack.lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        match &mut background.active {
            Some(BackgroundPack::Publishing { segment_id: active, merge_id })
                if *active == segment_id && merge_id.is_none() => {
                    let id = Uuid::new_v4();
                    *merge_id = Some(id);
                    Ok(id)
                }
            _ => Err(io::Error::other("merge publication reservation is unavailable")),
        }
    }

    /// Drops staged payloads whose blocks are now durable inside a published
    /// payload segment. No loose file was ever written for them; a matching
    /// fsync-only mark (recovery stages both when the loose read failed) is
    /// covered by the segment as well.
    pub(crate) fn retire_staged_payloads(&self, ids: &BTreeSet<BlockId>) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PACK_RETIRE);
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        pending.retire_staged(ids);
        pending.sync_only.retain(|id| !ids.contains(id));
        Ok(())
    }

    /// Materializes the named staged payloads as loose files and fsyncs them
    /// together with every fsync-only mark in one batch, then clears the
    /// covered pending entries. Checkpoint routes only payloads that could
    /// not join a payload segment through here; the packed majority is
    /// retired by `retire_staged_payloads` without touching loose storage.
    /// On error nothing is cleared, so the next attempt retries the full
    /// set idempotently.
    pub(crate) fn flush_pending_subset(&self, materialize: &BTreeSet<BlockId>) -> io::Result<()> {
        self.flush_pending_selection(materialize, true)
    }

    /// A reference-only commit needs durability for its own references, not
    /// the unrelated WAL-carried backlog or its recovery sync marks.
    pub(crate) fn flush_pending_references(&self, references: &BTreeSet<BlockId>) -> io::Result<()> {
        self.flush_pending_selection(references, false)
    }

    fn flush_pending_selection(
        &self, materialize: &BTreeSet<BlockId>, include_all_sync_marks: bool,
    ) -> io::Result<()> {
        let (payloads, sync_only) = {
            let pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            (
                pending
                    .payloads
                    .iter()
                    .chain(pending.rotation_draining.iter())
                    .filter(|(id, _)| materialize.contains(*id))
                    .map(|(id, payload)| (*id, Arc::clone(payload)))
                    .collect::<Vec<_>>(),
                pending.sync_only.iter().copied()
                    .filter(|id| include_all_sync_marks || materialize.contains(id))
                    .collect::<Vec<_>>(),
            )
        };
        if payloads.is_empty() && sync_only.is_empty() {
            return Ok(());
        }
        let mut ids: BTreeSet<BlockId> = sync_only.into_iter().collect();
        for (id, payload) in &payloads {
            // A complete loose file is content-pinned by the atomic rename,
            // so rewriting it would be wasted IO; only absent files are
            // materialized. Either way the file joins the fsync set.
            match self.vfs.metadata(&self.path(*id)) {
                Ok(FileKind::File) => {
                    // A filename is not a complete-copy witness. Optional
                    // dedupe/recovery may have staged valid WAL bytes over a
                    // damaged loose duplicate; never retire those bytes based
                    // only on existence or a successful sync of that duplicate.
                    let existing = self.read_loose_block(*id, &self.path(*id))?;
                    if existing.as_slice() != payload.as_slice() {
                        return Err(invalid_data("content-addressed block collision"));
                    }
                    self.note_relink(*id);
                }
                Ok(FileKind::Directory) => {
                    return Err(invalid_data("block path is a directory"));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    self.write_loose_unsynced(*id, payload)?;
                }
                Err(error) => return Err(error),
            }
            ids.insert(*id);
        }
        let ids: Vec<BlockId> = ids.into_iter().collect();
        self.sync_loose_blocks(&ids)?;
        let flushed: BTreeSet<BlockId> = ids.iter().copied().collect();
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        // Only payloads this call actually materialized as loose files may
        // be dropped. A staged payload can also carry an fsync-only mark
        // (recovery stages both when the loose read failed); clearing it
        // just because its mark was synced would lose the only copy when an
        // incremental checkpoint step defers it to a later batch.
        pending.retire_staged(&payloads.iter().map(|(id, _)| *id).collect());
        pending.sync_only.retain(|id| !flushed.contains(id));
        Ok(())
    }

    fn write_loose_unsynced(&self, id: BlockId, payload: &[u8]) -> io::Result<()> {
        let destination = self.path(id);
        match self.vfs.metadata(&destination) {
            Ok(FileKind::Directory) => return Err(invalid_data("block path is a directory")),
            Ok(_) => {}
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
            self.vfs.rename(&temporary, &destination)
        })();
        if write_result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        write_result?;
        self.blocks_written.fetch_add(1, Ordering::Relaxed);
        self.bytes_written.fetch_add(encoded_len, Ordering::Relaxed);
        Ok(())
    }

    /// Fsyncs loose block files plus their shard directories. Missing files
    /// are skipped: payload-segment GC may have already collected a block
    /// whose content lives on elsewhere, and re-checkpointing after an
    /// interrupted attempt must stay idempotent.
    pub(crate) fn sync_loose_blocks(&self, ids: &[BlockId]) -> io::Result<()> {
        let mut shards = BTreeSet::new();
        for id in ids {
            let path = self.path(*id);
            match self.vfs.metadata(&path) {
                Ok(FileKind::File) => {}
                Ok(FileKind::Directory) => {
                    return Err(invalid_data("block path is a directory"));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            }
            let mut file = self.vfs.open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )?;
            sync_all_barrier(file.as_mut())?;
            shards.insert(path.parent().unwrap_or(self.root.as_path()).to_path_buf());
        }
        for shard in &shards {
            sync_directory_barrier(self.vfs.as_ref(), shard)?;
        }
        Ok(())
    }

    /// Shared-handle variant of [`BlockStore::get`]: pending and cached
    /// payloads are returned by `Arc` without a deep copy, and verified disk
    /// reads are cached once and shared. Verification semantics are
    /// identical to `get` — the only difference is who owns the buffer.
    #[inline]
    pub(crate) fn get_shared(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        if crate::request_profile::enabled() {
            self.get_shared_profiled(id)
        } else {
            self.get_shared_measured::<false>(id)
        }
    }

    // Keep measured timer frames out of callers on the ordinary read path.
    #[cold]
    #[inline(never)]
    fn get_shared_profiled(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.get_shared_measured::<true>(id)
    }

    #[inline(always)]
    fn get_shared_measured<const MEASURE: bool>(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        let _read = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_SHARED_READ);
        let pending = {
            let _pending = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_GET_PENDING);
            self.pending_payload_measured::<MEASURE>(id)?
        };
        if let Some(payload) = pending {
            return Ok(payload);
        }
        {
            let waiting = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_WAIT);
            let mut cache = self
                .cache
                .write()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
            drop(waiting);
            let _access = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_ACCESS);
            if let Some(payload) = cache.get(id) {
                return Ok(payload);
            }
        }
        let payload = self.get_uncached(id)?;
        let shared = Arc::new(payload);
        let waiting = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_WAIT);
        let mut cache = self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
        drop(waiting);
        let _access = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_ACCESS);
        cache.insert_shared(id, Arc::clone(&shared));
        Ok(shared)
    }

    /// Index metadata must resolve through ordinary storage, never through
    /// the history index it defines. Share the existing byte-bounded cache.
    #[inline]
    pub(crate) fn get_materialized_shared(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        if crate::request_profile::enabled() {
            self.get_materialized_shared_profiled(id)
        } else {
            self.get_materialized_shared_measured::<false>(id)
        }
    }

    // Keep measured timer frames out of callers on the ordinary read path.
    #[cold]
    #[inline(never)]
    fn get_materialized_shared_profiled(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.get_materialized_shared_measured::<true>(id)
    }

    #[inline(always)]
    fn get_materialized_shared_measured<const MEASURE: bool>(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        let _read = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_SHARED_READ);
        let pending = {
            let _pending = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_GET_PENDING);
            self.pending_payload_measured::<MEASURE>(id)?
        };
        if let Some(payload) = pending { return Ok(payload); }
        {
            let waiting = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_WAIT);
            let mut cache = self.cache.write()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
            drop(waiting);
            let _access = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_ACCESS);
            if let Some(payload) = cache.get(id) { return Ok(payload); }
        }
        let payload = Arc::new(self.get_uncached_materialized(id)?);
        let waiting = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_WAIT);
        let mut cache = self.cache.write().map_err(|_| io::Error::other("block cache lock was poisoned"))?;
        drop(waiting);
        let _access = crate::fsync_stats::StageTimer::conditional::<MEASURE>(crate::fsync_stats::STAGE_BLOCK_CACHE_ACCESS);
        cache.insert_shared(id, Arc::clone(&payload));
        Ok(payload)
    }

    /// Maintenance streaming must not evict foreground nodes with complete
    /// history sources. Each caller owns this bounded temporary allocation.
    pub(crate) fn get_materialized_uncached(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        if let Some(payload) = self.pending_payload(id)? { return Ok(payload); }
        self.get_uncached_materialized(id).map(Arc::new)
    }

    pub(crate) fn install_history_payload_catalog(
        &self, manifest: &crate::history::HistoryManifest,
        root: Option<crate::history_payload::RoutingRoot>,
    ) -> io::Result<()> {
        if manifest.payload_index_root_id.is_some() != root.is_some() {
            return Err(invalid_data("history routing root presence mismatch"));
        }
        *self.history_payloads.write().map_err(|_| io::Error::other("history payload catalog lock poisoned"))? =
            root.map(|root| Arc::new(crate::history_payload::Catalog::new(manifest, root)));
        let sources: BTreeSet<_> = manifest.segments.iter().map(|reference| reference.block_id).collect();
        self.history_source_handles.lock().map_err(|_| io::Error::other("history source cache lock poisoned"))?
            .entries.retain(|id, _| sources.contains(id));
        Ok(())
    }

    pub(crate) fn history_payload_metadata_ids(&self, manifest: &crate::history::HistoryManifest) -> io::Result<Vec<BlockId>> {
        let selection = HistoryPayloadMetadataSelection::capture(manifest, u64::MAX)?;
        let mut ids = Vec::new();
        self.visit_history_payload_metadata_selection(selection, |id| { ids.push(id); Ok(()) })?;
        Ok(ids)
    }

    /// Eager readers and off-authority GC share one current-source decode and
    /// page enumeration. GC streams IDs into its existing bounded mark set.
    pub(crate) fn visit_history_payload_metadata_selection(
        &self, selection: HistoryPayloadMetadataSelection,
        mut visitor: impl FnMut(BlockId) -> io::Result<()>,
    ) -> io::Result<()> {
        for id in selection.index_ids { visitor(id)?; }
        if let Some(root_id) = selection.payload_root_id {
            visitor(root_id)?;
            let root = crate::history_payload::RoutingRoot::decode(&self.get_materialized_shared(root_id)?)?;
            for id in root.page_ids() { visitor(id)?; }
        }
        Ok(())
    }

    /// Called only after CONTROL owns the indexed history copies. In-flight
    /// pack plans keep their Arc buffers; their optional publication may still
    /// complete, but no data is lost when these RAM copies disappear.
    pub(crate) fn release_history_carried_payloads(&self, ids: &BTreeSet<BlockId>) -> io::Result<()> {
        let mut pending = self.pending.lock().map_err(|_| io::Error::other("pending block set poisoned"))?;
        for id in ids {
            pending.remove(*id);
        }
        Ok(())
    }

    fn get_from_history(&self, id: BlockId) -> io::Result<Option<Vec<u8>>> {
        let _resolve = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_HISTORY_RESOLVE);
        let catalog = self.history_payloads.read().map_err(|_| io::Error::other("history payload catalog lock poisoned"))?.clone();
        let Some(catalog) = catalog else { return Ok(None); };
        let mut found = None;
        let mut copy_error = None;
        catalog.root.visit_locations(self, id, &mut |location| {
            if !catalog.sources.contains(&location.source) {
                return Err(invalid_data("history payload source is outside the published manifest"));
            }
            match self.read_history_location(id, location) {
                Ok(payload) => { found = Some(payload); Ok(true) }
                Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {
                    copy_error = Some(error); Ok(false)
                }
                Err(error) => Err(error),
            }
        })?;
        if found.is_some() { return Ok(found); }
        if let Some(error) = copy_error { return Err(error); }
        Ok(None)
    }

    fn open_history_source(&self, source: BlockId, source_bytes: u32) -> io::Result<Arc<HistorySourceHandle>> {
        {
            let mut cache = self.history_source_handles.lock().map_err(|_| io::Error::other("history source cache lock poisoned"))?;
            cache.trim(self.history_source_handle_limit.load(Ordering::Relaxed));
            if let Some(handle) = cache.get(source) {
                if handle.source_bytes != source_bytes { return Err(invalid_data("history payload source size witness mismatch")); }
                return Ok(handle);
            }
        }
        // Opening and framing I/O never hold the cache mutex. Racing opens may
        // share the winner's verified handle; unused descriptors drop here.
        let file = match self.vfs.open_positioned_reader(&self.path(source))? {
            Some(file) => HistorySourceReader::Positioned(file),
            None => HistorySourceReader::Cursor(Mutex::new(self.vfs.open(
                &self.path(source), OpenRequest { read: true, ..OpenRequest::default() })?)),
        };
        let expected = HEADER_BYTES as u64 + u64::from(source_bytes) + FOOTER_BYTES as u64;
        if file.len()? != expected { return Err(invalid_data("history payload source size mismatch")); }
        let mut header = [0_u8; HEADER_BYTES]; file.read_exact_at(0, &mut header)?;
        let mut source_id = [0_u8; 32]; file.read_exact_at(expected - 32, &mut source_id)?;
        if &header[..8] != MAGIC || u32::from_le_bytes(header[8..12].try_into().unwrap()) != FORMAT_VERSION
            || u32::from_le_bytes(header[12..16].try_into().unwrap()) != source_bytes || source_id != source.0 {
            return Err(invalid_data("history payload source witness mismatch"));
        }
        let handle = Arc::new(HistorySourceHandle { file, source_bytes });
        let mut cache = self.history_source_handles.lock().map_err(|_| io::Error::other("history source cache lock poisoned"))?;
        let maximum = self.history_source_handle_limit.load(Ordering::Relaxed);
        if maximum != 0 {
            if let Some(existing) = cache.get(source) {
                if existing.source_bytes != source_bytes { return Err(invalid_data("history payload source size witness mismatch")); }
                return Ok(existing);
            }
            cache.clock = cache.clock.saturating_add(1);
            let age = cache.clock; cache.entries.insert(source, (Arc::clone(&handle), age)); cache.trim(maximum);
        }
        Ok(handle)
    }

    fn read_history_location(&self, id: BlockId, location: crate::history_payload::PayloadLocation) -> io::Result<Vec<u8>> {
        if location.offset.checked_add(location.length).is_none_or(|end| end > location.source_bytes) {
            return Err(invalid_data("history payload location exceeds its source"));
        }
        let mut payload = vec![0; location.length as usize];
        let opened = {
            let _open = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_HISTORY_SOURCE_OPEN);
            self.open_history_source(location.source, location.source_bytes)
        };
        match opened {
            Ok(handle) => {
                let result = handle.file.read_exact_at(HEADER_BYTES as u64 + u64::from(location.offset), &mut payload);
                if let Err(error) = result {
                    self.history_source_handles.lock().map_err(|_| io::Error::other("history source cache lock poisoned"))?
                        .entries.remove(&location.source);
                    return Err(if error.kind() == io::ErrorKind::UnexpectedEof { invalid_data("history payload source was truncated") } else { error });
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let source = self.get_materialized_shared(location.source)?;
                if source.len() != location.source_bytes as usize { return Err(invalid_data("packed history source size mismatch")); }
                payload.copy_from_slice(&source[location.offset as usize..(location.offset + location.length) as usize]);
            }
            Err(error) => return Err(error),
        }
        let _verify = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_CONTENT_VERIFY);
        if BlockId::for_payload(&payload) != id {
            self.history_source_handles.lock().map_err(|_| io::Error::other("history source cache lock poisoned"))?
                .entries.remove(&location.source);
            return Err(invalid_data("history payload content hash mismatch"));
        }
        Ok(payload)
    }

    pub fn get(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOCK_GET);
        // Staged payloads are the freshest copy of a not-yet-materialized
        // block; content addressing pins them to the same identifier the
        // durable paths below would resolve.
        let pending_probe = std::time::Instant::now();
        let pending_hit = self.pending_payload(id)?;
        crate::fsync_stats::note_stage_elapsed(
            crate::fsync_stats::STAGE_GET_PENDING,
            pending_probe.elapsed().as_nanos() as u64,
        );
        if let Some(payload) = pending_hit {
            return Ok((*payload).clone());
        }
        let cache_probe = std::time::Instant::now();
        let cache_hit = {
            let mut cache = self
                .cache
                .write()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
            cache.get(id)
        };
        crate::fsync_stats::note_stage_elapsed(
            crate::fsync_stats::STAGE_GET_CACHE,
            cache_probe.elapsed().as_nanos() as u64,
        );
        if let Some(payload) = cache_hit {
            return Ok((*payload).clone());
        }
        let uncached_probe = std::time::Instant::now();
        let uncached = self.get_uncached(id);
        let uncached_nanos = uncached_probe.elapsed().as_nanos() as u64;
        match &uncached {
            Ok(_) => crate::fsync_stats::note_stage_elapsed(
                crate::fsync_stats::STAGE_GET_UNCACHED_OK,
                uncached_nanos,
            ),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                crate::fsync_stats::note_stage_elapsed(
                    crate::fsync_stats::STAGE_GET_UNCACHED_MISS,
                    uncached_nanos,
                )
            }
            Err(_) => {}
        }
        let payload = uncached?;
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .insert(id, &payload);
        Ok(payload)
    }

    /// Resolves a block that is neither staged nor cached. The packed
    /// union filter is consulted first: a filter miss proves (no false
    /// negatives) the block is not segment-resident, so the O(segments)
    /// manifest scan is skipped and only the loose path is probed. A
    /// filter hit falls through to the manifest scan; a scan that
    /// yields no payload (bloom false positive) falls back to the loose
    /// probe as well. Content addressing makes the segment and loose
    /// copies interchangeable — both paths verify the payload hash on
    /// read.
    fn get_uncached(&self, id: BlockId) -> io::Result<Vec<u8>> {
        match self.get_from_history(id) {
            Ok(Some(payload)) => return Ok(payload),
            Ok(None) => {}
            Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {
                return self.get_uncached_materialized(id).or(Err(error));
            }
            Err(error) => return Err(error),
        }
        self.get_uncached_materialized(id)
    }

    fn get_uncached_materialized(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let _resolve = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_MATERIALIZED_RESOLVE);
        if let Some(payload) = self.get_from_read_hint(id) {
            return Ok(payload);
        }
        // A clean open has not traversed the packed catalog. Recent roots and
        // checkpoint manifests are usually loose; resolve them directly before
        // considering a segment scan. A corrupt loose duplicate still falls
        // through to the existing verified packed-copy path.
        let filter_built = self.packed_filter.read()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?.built;
        if !filter_built {
            let path = self.path(id);
            match self.vfs.metadata(&path) {
                Ok(FileKind::File) => match self.read_loose_block(id, &path) {
                    Ok(payload) => return Ok(payload),
                    Err(error) if matches!(error.kind(), io::ErrorKind::InvalidData | io::ErrorKind::NotFound) => {},
                    Err(error) => return Err(error),
                },
                Ok(FileKind::Directory) => {}, // The packed copy may still be valid.
                Err(error) if error.kind() == io::ErrorKind::NotFound => {},
                Err(error) => return Err(error),
            }
        }
        let candidates = if self.packed_filter_may_contain(id)? {
            self.payload_segment_candidates(id)?
        } else {
            Vec::new()
        };
        if !candidates.is_empty() {
            // The snapshot may name a segment a concurrent publication just
            // retired; probing it must converge like every other catalog
            // read, not surface the transient race as durable corruption.
            if let Some(payload) = self.get_from_snapshot_candidates(id, &candidates)? {
                return Ok(payload);
            }
        }
        let path = self.path(id);
        match self.vfs.metadata(&path) {
            Ok(FileKind::File) => self.read_loose_block(id, &path),
            Ok(FileKind::Directory) => Err(invalid_data("block is not a regular file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                if candidates.is_empty() {
                    Err(io::Error::new(
                        io::ErrorKind::NotFound,
                        format!("content block {} is missing", id.to_hex()),
                    ))
                } else {
                    self.get_from_payload_segments(id)?.ok_or_else(|| {
                        io::Error::new(io::ErrorKind::NotFound, format!("content block {} is missing", id.to_hex()))
                    })
                }
            }
            Err(error) => Err(error),
        }
    }

    /// Strict packed-membership probe for dedupe: an *unbuilt* filter means
    /// "no catalog installed yet", which proves nothing about this id — the
    /// conservative full-scan fallback in `packed_filter_may_contain` would
    /// otherwise misreport it as packed and block all staging forever. A
    /// built-filter hit stays advisory: `probe_dedupe` must still resolve
    /// the payload bytes before claiming membership, because bloom hits
    /// carry false positives.
    fn packed_filter_definitely_contains(&self, id: BlockId) -> io::Result<bool> {
        let filter = self
            .packed_filter
            .read()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        Ok(filter.built && (filter.may_contain(id) || !self.filter_covers_installed_catalog(&filter)))
    }

    /// Dedupe membership probe behind the write path. The union filter is
    /// only a negative oracle: a hit selects candidate segments whose
    /// validated indexes must actually yield the payload. Every candidate
    /// is scanned (no truncation window) so a true hit beyond any hot
    /// prefix still resolves as `Present`; a hit no candidate confirms is
    /// a bloom false positive and degrades to `Missing`, which restages —
    /// existence is claimed only on bytes, never on the filter alone.
    fn probe_packed_dedupe(&self, id: BlockId) -> io::Result<DedupeProbe> {
        // Complete new Blob bytes already supply their exact WAL recovery
        // witness. Cold history dedup is optional: keep verified warm/packed
        // reuse and carry a complete copy when history is not resident.
        if let Some(payload) = self.get_from_read_hint(id) {
            return Ok(DedupeProbe::Present(payload));
        }
        let filter_hit = self.packed_filter_definitely_contains(id)?;
        let candidates = if filter_hit {
            self.payload_segment_candidates(id)?
        } else {
            Vec::new()
        };
        if !candidates.is_empty() {
            // Same publication race as `get_uncached`: a retired candidate
            // must never turn one dedupe probe into durable corruption.
            if let Some(payload) = self.get_from_snapshot_candidates(id, &candidates)? {
                return Ok(DedupeProbe::Present(payload));
            }
        }
        Ok(DedupeProbe::Missing)
    }

    fn probe_dedupe(&self, id: BlockId) -> io::Result<DedupeProbe> {
        // Generic block publication retains its original source-validation
        // reads and errors; only complete-payload optional Blob dedup omits it.
        if let Some(payload) = self.get_from_history(id)? {
            return Ok(DedupeProbe::Present(payload));
        }
        if let present @ DedupeProbe::Present(_) = self.probe_packed_dedupe(id)? {
            return Ok(present);
        }
        let path = self.path(id);
        match self.vfs.metadata(&path) {
            Ok(FileKind::File) => self.read_loose_block(id, &path).map(DedupeProbe::Present),
            Ok(FileKind::Directory) => Err(invalid_data("block is not a regular file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(DedupeProbe::Missing),
            Err(error) => Err(error),
        }
    }

    /// Overrides the payload cache byte budget. Capacity zero disables
    /// caching entirely; embedders with tighter memory budgets lower this
    /// before serving traffic.
    pub fn set_cache_capacity_bytes(&self, capacity_bytes: usize) -> io::Result<()> {
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .set_capacity_bytes(capacity_bytes);
        let maximum = capacity_bytes / MAX_BLOCK_BYTES;
        self.history_source_handle_limit.store(maximum, Ordering::Relaxed);
        self.history_source_handles.lock().map_err(|_| io::Error::other("history source cache lock poisoned"))?.trim(maximum);
        Ok(())
    }

    /// Overrides the verified open-segment index cache byte budget from the
    /// probed launch budget. A store without a payload-segment directory yet
    /// accepts the value as a no-op; the budget is applied again on any later
    /// open that has one.
    pub fn set_segment_index_cache_bytes(&self, budget_bytes: usize) -> io::Result<()> {
        if let Some(store) = self.payload_segments.as_ref() {
            store.set_open_segment_cache_budget_bytes(budget_bytes)?;
            if let Ok(mut hints) = self.read_hints.write() {
                hints.resize_with_saved(self.root.parent().expect("blocks have an authority parent"),
                    self.vfs.as_ref(), store.routing_budget_bytes());
            }
            let manifest = self.payload_manifest.read().map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
            if let Some(manifest) = manifest.as_ref() {
                let mut routing = self.payload_routing.write().map_err(|_| io::Error::other("payload routing lock was poisoned"))?;
                *routing = crate::payload_routing::PayloadRouting::build(manifest, Some(store), &routing, store.routing_budget_bytes(), false);
            }
        }
        Ok(())
    }

    fn read_loose_block(&self, id: BlockId, path: &Path) -> io::Result<Vec<u8>> {
        let _read = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_LOOSE_READ);
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
        let bytes = file.read_all_with_observed_length(maximum, length)?;
        // A short read after the observed length must be an integrity error,
        // never an out-of-bounds header access.
        if bytes.len() < HEADER_BYTES + FOOTER_BYTES {
            return Err(invalid_data("block has an invalid bounded size"));
        }
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
        let _verify = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_CONTENT_VERIFY);
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

    fn packed_filter_may_contain(&self, id: BlockId) -> io::Result<bool> {
        let filter = self
            .packed_filter
            .read()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        // Unbuilt means the manifest was never installed (or its segments
        // could not be indexed): fall back to the conservative full scan.
        Ok(!filter.built || filter.may_contain(id) || !self.filter_covers_installed_catalog(&filter))
    }

    fn filter_covers_installed_catalog(&self, filter: &PackedBlockFilter) -> bool {
        let publication = self.installed_payload_catalog_publication.load(Ordering::Acquire);
        let complete = publication != 0 && filter.covered_catalog_publication == publication;
        if !complete {
            crate::fsync_stats::note_stage_elapsed(crate::fsync_stats::STAGE_PACK_FILTER_INCOMPLETE, 0);
        }
        complete
    }

    /// Records block IDs that just became segment-resident. Insertions
    /// before the first manifest install are skipped on purpose: that
    /// install builds the filter from the manifest's segment indexes,
    /// which already cover everything packed so far. When insertions
    /// saturate the filter's capacity it is rebuilt from the installed
    /// manifest with doubled headroom (a bloom filter cannot grow in
    /// place, and an over-full filter would degenerate to the pre-filter
    /// O(segments) scan). Scanning runs outside the reader lock; installation
    /// checks the catalog and insertion count to preserve concurrent IDs. A
    /// segment whose publish later fails is an orphan the manifest never
    /// references, so its IDs never needed filter bits.
    pub(crate) fn note_packed(&self, ids: &[BlockId]) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PACK_FILTER);
        let mut guard = self
            .packed_filter
            .write()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        if !guard.built {
            return Ok(());
        }
        if guard.saturated() {
            let revision = guard.revision;
            let (manifest, publication) = self.payload_catalog_snapshot()?;
            drop(guard);
            let mut rebuilt = match manifest.as_ref() {
                Some(manifest) => self.scan_packed_filter(manifest, None, None),
                None => PackedBlockFilter::empty(),
            };
            rebuilt.covered_catalog_publication = publication;
            guard = self.packed_filter.write()
                .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
            let current = self.payload_manifest.read()
                .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
            // Do not lose IDs inserted while the scan ran. A changed view or
            // insertion count leaves the conservative old filter for retry.
            if guard.revision == revision && *current == manifest && rebuilt.built
                && self.installed_payload_catalog_publication.load(Ordering::Acquire) == publication {
                *guard = rebuilt;
                guard.revision = revision;
            }
        }
        for id in ids {
            guard.insert(*id);
        }
        guard.revision = guard.revision.wrapping_add(1);
        Ok(())
    }

    /// Scans the manifest's segment indexes into a fresh union filter.
    /// An unreadable segment yields an unbuilt filter: probes keep the
    /// pre-filter scan path, which surfaces that corruption exactly
    /// where it always did.
    fn scan_packed_filter(
        &self, manifest: &PayloadManifest,
        recovery: Option<&mut CarriedRecoveryLookup>,
        routes: Option<&mut crate::payload_routing::PayloadRouting>,
    ) -> PackedBlockFilter {
        self.scan_packed_filter_until(manifest, recovery, routes, &|| false)
    }

    fn scan_packed_filter_until(
        &self, manifest: &PayloadManifest,
        mut recovery: Option<&mut CarriedRecoveryLookup>,
        mut routes: Option<&mut crate::payload_routing::PayloadRouting>,
        should_stop: &dyn Fn() -> bool,
    ) -> PackedBlockFilter {
        let total_blocks = manifest
            .segments
            .iter()
            .map(|reference| reference.block_count as usize)
            .sum();
        let mut filter = PackedBlockFilter::with_capacity(total_blocks);
        if let Some(store) = self.payload_segments.as_ref() {
            let mut remaining_route_bytes = store.routing_budget_bytes();
            for reference in &manifest.segments {
                if should_stop() { return PackedBlockFilter::empty(); }
                match store.open_segment(reference.segment_id) {
                    Ok(segment) => {
                        if validate_open_segment(&segment, reference).is_err()
                            || store.remember_segment_membership(&segment).is_err()
                        {
                            return PackedBlockFilter::empty();
                        }
                        if let Some(routes) = routes.as_deref_mut() {
                            routes.remember_verified(*reference, &segment, &mut remaining_route_bytes);
                        }
                        for id in segment.block_ids() {
                            filter.insert(id);
                            if let Some(lookup) = recovery.as_deref_mut() {
                                if let Some(location) = lookup.locations.get_mut(&id) {
                                    *location = (Some(*reference), false);
                                }
                            }
                        }
                    }
                    Err(_) => return PackedBlockFilter::empty(),
                }
            }
        }
        filter
    }

    /// Builds the packed union filter from the manifest's segment
    /// indexes, once per process. Later growth is handled by the
    /// saturation rebuild in `note_packed`.
    fn build_packed_filter_once(&self, manifest: &PayloadManifest, publication: u64) -> io::Result<()> {
        let mut guard = self
            .packed_filter
            .write()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        if guard.built {
            return Ok(());
        }
        let mut filter = self.scan_packed_filter(manifest, None, None);
        filter.covered_catalog_publication = publication;
        *guard = filter;
        Ok(())
    }
    fn payload_segment_candidates(&self, id: BlockId) -> io::Result<Vec<PayloadSegmentReference>> {
        let manifest = self
            .payload_manifest
            .read()
            .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
        if manifest.is_none() { return Ok(Vec::new()); }
        Ok(self.payload_routing.read().map_err(|_| io::Error::other("payload routing lock was poisoned"))?.candidates(id))
    }

    fn get_from_payload_segments(&self, id: BlockId) -> io::Result<Option<Vec<u8>>> {
        let references = self.payload_segment_candidates(id)?;
        self.get_from_snapshot_candidates(id, &references)
    }

    /// Probes an already-copied candidate snapshot. A catalog publication
    /// can retire one of its sources after the copy, or install the segment
    /// holding this block between the copy and the open; both races are
    /// transient, so a changed fresh view retries once. An unchanged view
    /// keeps the original verdict: a broken catalog stays corruption.
    /// One retry bounds work under continuous publication.
    fn get_from_snapshot_candidates(
        &self,
        id: BlockId,
        candidates: &[PayloadSegmentReference],
    ) -> io::Result<Option<Vec<u8>>> {
        let result = self.get_from_payload_segment_references(id, candidates);
        if matches!(result, Ok(Some(_))) { return result; }
        let latest = self.payload_segment_candidates(id)?;
        if latest != candidates {
            self.get_from_payload_segment_references(id, &latest)
        } else {
            result
        }
    }

    fn open_catalogued_payload_segment(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<Arc<PayloadSegment>> {
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("CONTROL payload manifest has no payload segment directory")
        })?;
        let segment = store
            .open_segment_cached(reference.segment_id)
            .map_err(|error| {
                if error.kind() == io::ErrorKind::NotFound {
                    invalid_data("CONTROL payload manifest references a missing segment")
                } else {
                    error
                }
            })?;
        validate_open_segment(&segment, reference)?;
        Ok(segment)
    }

    /// Exact membership for maintenance planning. A negative filter result
    /// avoids segment I/O; positive results still require a validated index.
    /// Reuses the bounded index cache without materializing block payloads or
    /// retaining a catalog-wide set of packed IDs.
    pub(crate) fn has_catalogued_payload_block(&self, id: BlockId) -> io::Result<bool> {
        if !self.packed_filter_may_contain(id)? {
            return Ok(false);
        }
        for reference in self.payload_segment_candidates(id)?.iter().rev() {
            if self
                .open_catalogued_payload_segment(reference)?
                .contains(id)
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn get_from_payload_segment_references(
        &self,
        id: BlockId,
        references: &[PayloadSegmentReference],
    ) -> io::Result<Option<Vec<u8>>> {
        if references.is_empty() {
            return Ok(None);
        }
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("CONTROL payload manifest has no payload segment directory")
        })?;
        for reference in references.iter().rev() {
            if !store.cached_segment_may_contain(reference, id)? {
                continue;
            }
            let segment = self.open_catalogued_payload_segment(reference)?;
            if let Some(payload) = store.get_with_handle(&segment, id)? {
                if let Ok(mut hints) = self.read_hints.write() {
                    hints.remember(id, reference.segment_id);
                }
                return Ok(Some(payload));
            }
        }
        Ok(None)
    }

    fn get_from_read_hint(&self, id: BlockId) -> Option<Vec<u8>> {
        let reference = {
            let manifest = self.payload_manifest.read().ok()?;
            self.read_hints.read().ok()?.candidate(id, manifest.as_ref()?)?
        };
        // A stale/corrupt hint only loses its fast path. The ordinary read
        // below remains responsible for the final integrity/error verdict.
        self.get_from_payload_segment_references(id, &[reference]).ok().flatten()
    }

    pub(crate) fn save_read_hints(&self) -> io::Result<()> {
        if let Ok(mut hints) = self.read_hints.write() {
            hints.save(self.root.parent().expect("blocks have an authority parent"), self.vfs.as_ref())?;
        }
        Ok(())
    }

    pub(crate) fn install_payload_manifest(&self, manifest: PayloadManifest) -> io::Result<()> {
        self.install_payload_manifest_inner(manifest, false)
    }

    /// Currently installed payload catalog, cloned under the read lock. The
    /// online GC scans segment windows against this snapshot without taking
    /// the authority mutex (docs/TOFUDB_ONLINE_GC.md).
    pub(crate) fn installed_payload_manifest(&self) -> io::Result<Option<PayloadManifest>> {
        Ok(self
            .payload_manifest
            .read()
            .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?
            .clone())
    }

    pub(crate) fn install_recovery_payload_manifest(&self, manifest: PayloadManifest) -> io::Result<()> {
        self.install_payload_manifest_inner(manifest, true)
    }

    fn install_payload_manifest_inner(&self, manifest: PayloadManifest, defer_filter: bool) -> io::Result<()> {
        if !manifest.segments.is_empty() && self.payload_segments.is_none() {
            return Err(invalid_data(
                "payload manifest requires a payload segment directory",
            ));
        }
        let publication = self.next_payload_catalog_publication.fetch_update(
            Ordering::AcqRel, Ordering::Relaxed, |value| value.checked_add(1),
        ).map_err(|_| invalid_data("payload catalog cache publication counter exhausted"))? + 1;
        if !defer_filter { self.build_packed_filter_once(&manifest, publication)?; }
        let store = self.payload_segments.as_ref();
        let routing = {
            let previous = self.payload_routing.read().map_err(|_| io::Error::other("payload routing lock was poisoned"))?;
            crate::payload_routing::PayloadRouting::build(&manifest, store, &previous,
                store.map_or(0, |store| store.routing_budget_bytes()), !defer_filter)
        };
        // The manifest read lock also fences its routes. Readers cannot see
        // routes from another publication generation, even during retirement.
        {
            let mut current = self.payload_manifest.write().map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
            let mut routes = self.payload_routing.write().map_err(|_| io::Error::other("payload routing lock was poisoned"))?;
            // Zero is an invalidation fence: a concurrent negative probe is
            // conservative throughout the catalog/routes replacement. The
            // reserved ID is unique even if independent publishers overlap.
            self.installed_payload_catalog_publication.store(0, Ordering::Release);
            *current = Some(manifest);
            *routes = routing;
            self.installed_payload_catalog_publication.store(publication, Ordering::Release);
        }
        // A new manifest may drop segments that earlier reads resolved; stale
        // cache entries would outlive the files they came from. Compaction is
        // an infrequent background event, so a full clear is the safe bound.
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .clear();
        if let Some(store) = self.payload_segments.as_ref() {
            store.close_read_handles();
        }
        Ok(())
    }

    /// Segment files no manifest references that must still survive orphan
    /// reclamation: unpublished background builds plus deferred retirements.
    pub(crate) fn protected_segment_ids(&self) -> io::Result<Vec<Uuid>> {
        let mut ids = self.unpublished_background_segment_ids()?;
        if let Some(store) = self.payload_segments.as_ref() {
            ids.extend(store.deferred_retirement_ids()?);
        }
        Ok(ids)
    }

    /// Reclaims deferred segment retirements whose floor has expired.
    /// Returns the reclaimed byte total; a missing segment directory or an
    /// out-of-band-deleted file is a no-op, never a failure.
    pub(crate) fn sweep_deferred_segment_retirements(
        &self,
        delay: std::time::Duration,
    ) -> io::Result<u64> {
        let Some(store) = self.payload_segments.as_ref() else {
            return Ok(0);
        };
        store.sweep_deferred_retirement_with(delay)
    }

    pub(crate) fn verify_payload_segment(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<()> {
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("payload segment directory is unavailable for compaction")
        })?;
        let segment = store.open_segment(reference.segment_id)?;
        validate_open_segment(&segment, reference)?;
        // Publication already verifies this newly packed index. Retain its
        // positive locations now so the first post-checkpoint open does not
        // have to rediscover pages that were previously only WAL-staged.
        if let Ok(mut hints) = self.read_hints.write() {
            for id in segment.block_ids() {
                hints.remember(id, reference.segment_id);
            }
        }
        Ok(())
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
            blocks_staged: self.blocks_staged.load(Ordering::Relaxed),
            bytes_staged: self.bytes_staged.load(Ordering::Relaxed),
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
        {
            let mut pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            for block_id in block_ids {
                pending.remove(*block_id);
            }
        }
        let mut by_shard: BTreeMap<u8, Vec<BlockId>> = BTreeMap::new();
        for block_id in block_ids {
            by_shard.entry(block_id.0[0]).or_default().push(*block_id);
        }
        let mut metrics = BlockRemovalMetrics::default();
        for (_, ids) in by_shard {
            let shard = self.root.join(&ids[0].to_hex()[..2]);
            // Pending-only blocks never created the shard directory; syncing
            // it would fail closed on a directory that does not exist.
            let mut removed_file = false;
            for id in ids {
                let path = self.path(id);
                // A block that was only ever staged (pending) has no loose
                // file; removal of its in-memory copy above already retired
                // it, so a missing file is not an error.
                match self.vfs.open(
                    &path,
                    OpenRequest {
                        read: true,
                        ..OpenRequest::default()
                    },
                ) {
                    Ok(file) => {
                        metrics.bytes_removed = metrics
                            .bytes_removed
                            .checked_add(file.len()?)
                            .ok_or_else(|| invalid_data("removed block byte count overflow"))?;
                        self.vfs.remove_file(&path)?;
                        removed_file = true;
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error),
                }
                self.cache
                    .write()
                    .map_err(|_| io::Error::other("block cache lock was poisoned"))?
                    .remove(id);
                metrics.blocks_removed = metrics
                    .blocks_removed
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("removed block count overflow"))?;
            }
            if removed_file {
                sync_directory_barrier(self.vfs.as_ref(), &shard)?;
            }
        }
        Ok(metrics)
    }

    pub(crate) fn block_file_bytes(&self, block_id: BlockId) -> io::Result<u64> {
        // A staged payload has no loose file yet; its encoded length is
        // deterministic, so size-derived planning (payload compaction victim
        // budgets) works on pending blocks exactly as on materialized ones.
        if let Some(payload) = self.pending_payload(block_id)? {
            return Ok((HEADER_BYTES + payload.len() + FOOTER_BYTES) as u64);
        }
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
    struct PausingHistoryReader {
        inner: Arc<dyn VfsPositionedReader>,
        entered: std::sync::mpsc::Sender<()>,
        release: Arc<(Mutex<bool>, Condvar)>,
    }
    impl VfsPositionedReader for PausingHistoryReader {
        fn len(&self) -> io::Result<u64> { self.inner.len() }
        fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.entered.send(()).map_err(|_| io::Error::other("read probe receiver closed"))?;
            let (lock, wake) = &*self.release;
            let mut released = lock.lock().unwrap();
            while !*released {
                let (next, result) = wake.wait_timeout(released, Duration::from_secs(5)).unwrap();
                released = next;
                if result.timed_out() { return Err(io::Error::new(io::ErrorKind::TimedOut, "read probe release elapsed")); }
            }
            self.inner.read_exact_at(offset, bytes)
        }
    }

    #[cfg(unix)]
    #[test]
    fn same_history_source_allows_two_actual_reads_before_either_completes() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        let mut source = vec![17; 4096]; source.extend_from_slice(&vec![29; 4096]);
        let source_id = store.put(&source).unwrap();
        let original = store.open_history_source(source_id, source.len() as u32).unwrap();
        let HistorySourceReader::Positioned(reader) = &original.file else { panic!("plain Unix source must use positioned reads") };
        let (entered, receive) = std::sync::mpsc::channel();
        let release = Arc::new((Mutex::new(false), Condvar::new()));
        // Ordinary open checked framing. Pause only the real read operation;
        // cache/source witnesses and per-payload content checks still run.
        let handle = Arc::new(HistorySourceHandle { source_bytes: source.len() as u32,
            file: HistorySourceReader::Positioned(Arc::new(PausingHistoryReader {
                inner: Arc::clone(reader), entered, release: Arc::clone(&release),
            })),
        });
        store.history_source_handles.lock().unwrap().entries.insert(source_id, (handle, 0));
        let workers = [17, 29].into_iter().enumerate().map(|(index, value)| {
            let store = Arc::clone(&store);
            std::thread::spawn(move || {
                let expected = vec![value; 4096];
                let location = crate::history_payload::PayloadLocation { source: source_id,
                    source_bytes: 8192, offset: (index * 4096) as u32, length: 4096 };
                let read = store.read_history_location(BlockId::for_payload(&expected), location)?;
                assert_eq!(read, expected);
                Ok::<_, io::Error>(())
            })
        }).collect::<Vec<_>>();
        let first = receive.recv_timeout(Duration::from_secs(2));
        let second = receive.recv_timeout(Duration::from_secs(2));
        *release.0.lock().unwrap() = true; release.1.notify_all();
        for worker in workers { worker.join().unwrap().unwrap(); }
        assert!(first.is_ok() && second.is_ok(), "both reads must enter I/O without serial source ownership");
    }

    #[test]
    fn positioned_source_read_fault_corruption_and_truncation_fail_closed() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap(); vfs.sync_directory(Path::new("/")).unwrap();
        let store = BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let payload = vec![19; 4096]; let source_id = store.put(&payload).unwrap();
        let location = crate::history_payload::PayloadLocation {
            source: source_id, source_bytes: 4096, offset: 0, length: 4096,
        };
        let id = BlockId::for_payload(&payload);
        assert_eq!(store.read_history_location(id, location).unwrap(), payload);
        vfs.arm_fault(Some(FaultRule { operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::Other) })).unwrap();
        assert_eq!(store.read_history_location(id, location).unwrap_err().kind(), io::ErrorKind::Other);
        assert!(store.history_source_handles.lock().unwrap().entries.is_empty());
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.read_history_location(id, location).unwrap(), payload);
        let mut file = vfs.open(&store.path(source_id), OpenRequest { write: true, ..OpenRequest::default() }).unwrap();
        file.write_all_at(HEADER_BYTES as u64, &[27]).unwrap();
        assert_eq!(store.read_history_location(id, location).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert!(store.history_source_handles.lock().unwrap().entries.is_empty());
        file.write_all_at(HEADER_BYTES as u64, &[19]).unwrap();
        assert_eq!(store.read_history_location(id, location).unwrap(), payload);
        file.set_len(HEADER_BYTES as u64 + 1024).unwrap();
        assert_eq!(store.read_history_location(id, location).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn staged_byte_counter_tracks_stage_flush_and_retire_exactly() {
        let vfs = Arc::new(crate::vfs::DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let store = BlockStore::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        let payload_a = b"staged-a".to_vec();
        let payload_b = b"staged-bb".to_vec();
        let (id_a, _) = store.put_deferred(&payload_a).unwrap();
        let (id_b, _) = store.put_deferred(&payload_b).unwrap();
        assert_eq!(
            store.pending_staged_bytes().unwrap(),
            (payload_a.len() + payload_b.len()) as u64
        );
        let mut flush = std::collections::BTreeSet::new();
        flush.insert(id_a);
        store.flush_pending_subset(&flush).unwrap();
        assert_eq!(
            store.pending_staged_bytes().unwrap(),
            payload_b.len() as u64
        );
        let mut retire = std::collections::BTreeSet::new();
        retire.insert(id_b);
        store.retire_staged_payloads(&retire).unwrap();
        assert_eq!(store.pending_staged_bytes().unwrap(), 0);
    }


    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::fs::OpenOptions;
    use std::io::{Seek, SeekFrom, Write};

    #[test]
    fn verified_payload_cache_hits_bound_recency_and_preserve_lru() {
        let first = BlockId::for_payload(b"aaaa");
        let second = BlockId::for_payload(b"bbbb");
        let third = BlockId::for_payload(b"cccc");
        let mut cache = BlockPayloadCache::new(8);
        cache.insert(first, b"aaaa");
        cache.insert(second, b"bbbb");
        for _ in 0..100_000 {
            assert_eq!(cache.get(first).unwrap().as_slice(), b"aaaa");
        }
        assert!(cache.lru.len() <= cache.entries.len() * 2);
        assert!(cache.lru.capacity() <= MAX_BLOCK_PAYLOAD_CACHE_RECENCY_RECORDS);
        cache.insert(third, b"cccc");
        assert!(cache.get(second).is_none());
        assert_eq!(cache.get(first).unwrap().as_slice(), b"aaaa");
        cache.set_capacity_bytes(4);
        assert!(cache.get(third).is_none());
        assert_eq!(cache.get(first).unwrap().as_slice(), b"aaaa");
        cache.remove(first);
        assert!(cache.get(first).is_none());
        assert_eq!(cache.resident_bytes, 0);
    }

    #[test]
    fn payload_cache_metadata_is_bounded_even_without_payload_bytes() {
        let mut cache = BlockPayloadCache::new(1);
        for value in 0..MAX_BLOCK_PAYLOAD_CACHE_ENTRIES + 2 {
            let mut id = [0_u8; 32];
            id[..8].copy_from_slice(&(value as u64).to_le_bytes());
            // Cache policy is independent of content verification, which
            // belongs to the caller. Even zero-byte entries need a count cap.
            cache.insert_shared(BlockId(id), Arc::new(Vec::new()));
        }
        assert_eq!(cache.entries.len(), MAX_BLOCK_PAYLOAD_CACHE_ENTRIES);
        assert_eq!(cache.resident_bytes, 0);
        assert!(cache.lru.len() <= MAX_BLOCK_PAYLOAD_CACHE_RECENCY_RECORDS);
        assert!(cache.lru.capacity() <= MAX_BLOCK_PAYLOAD_CACHE_RECENCY_RECORDS);
        cache.set_capacity_bytes(0);
        assert_eq!(cache.entries.capacity(), 0);
        assert_eq!(cache.lru.capacity(), 0);
        cache.insert(BlockId([7; 32]), b"");
        cache.insert_shared(BlockId([8; 32]), Arc::new(Vec::new()));
        assert!(cache.entries.is_empty());
        assert!(cache.lru.is_empty());
    }

    #[test]
    fn shared_payload_cache_charges_capacity_and_releases_evicted_handles() {
        let mut payload = Vec::with_capacity(1024);
        payload.extend_from_slice(b"short");
        let id = BlockId::for_payload(&payload);
        let payload = Arc::new(payload);
        let mut cache = BlockPayloadCache::new(512);
        cache.insert_shared(id, Arc::clone(&payload));
        assert!(cache.get(id).is_none());
        assert_eq!(Arc::strong_count(&payload), 1);
        cache.set_capacity_bytes(payload.capacity());
        cache.insert_shared(id, Arc::clone(&payload));
        assert_eq!(cache.resident_bytes, payload.capacity());
        assert_eq!(Arc::strong_count(&payload), 2);
        cache.insert(BlockId::for_payload(b"x"), b"x");
        assert!(cache.get(id).is_none());
        assert_eq!(Arc::strong_count(&payload), 1);
        assert_eq!(cache.resident_bytes, 1);
    }

    #[test]
    fn packed_filter_rebuild_before_batch_publication_cannot_hide_durable_blocks() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let make = |payload: &[u8]| {
            let id = BlockId::for_payload(payload);
            let segment = store.payload_segment_store().unwrap()
                .create(Uuid::new_v4(), vec![id], |_| Ok(payload.to_vec())).unwrap();
            let reference = PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap();
            (id, reference)
        };
        let (base_id, base) = make(b"published base");
        store.install_payload_manifest(PayloadManifest::new(vec![base]).unwrap()).unwrap();
        let (future_id, future) = make(b"durable batch awaiting catalog publication");
        // History promotion builds several segments before its atomic publish.
        // A saturation rebuild sees only the old catalog, losing early hints.
        store.note_packed(&[future_id]).unwrap();
        let capacity = store.packed_filter.read().unwrap().capacity_blocks;
        for _ in 0..capacity { store.note_packed(&[base_id]).unwrap(); }
        store.note_packed(&[base_id]).unwrap();
        assert!(!store.packed_filter.read().unwrap().may_contain(future_id), "the regression needs a dropped unpublished hint");
        store.install_payload_manifest(PayloadManifest::new(vec![base, future]).unwrap()).unwrap();
        assert!(store.has_catalogued_payload_block(future_id).unwrap(), "a stale filter must never disprove membership in a new catalog");
        assert_eq!(store.get(future_id).unwrap(), b"durable batch awaiting catalog publication");
    }

    #[test]
    fn packed_filter_has_no_false_negatives_and_bounded_false_positives() {
        let mut filter = PackedBlockFilter::with_capacity(1_000);
        let mut packed = Vec::new();
        for index in 0..1_000_u64 {
            let id = BlockId::for_payload(format!("packed-{index}").as_bytes());
            filter.insert(id);
            packed.push(id);
        }
        for id in &packed {
            assert!(filter.may_contain(*id));
        }
        let mut false_positives = 0;
        for index in 0..10_000_u64 {
            let id = BlockId::for_payload(format!("fresh-{index}").as_bytes());
            if filter.may_contain(id) {
                false_positives += 1;
            }
        }
        assert!(
            false_positives < 100,
            "false-positive rate above 1%: {false_positives}"
        );
    }

    #[test]
    fn packed_filter_reports_saturation_only_at_capacity() {
        let mut filter = PackedBlockFilter::with_capacity(0);
        assert!(!filter.saturated());
        let capacity = filter.capacity_blocks;
        assert!(capacity >= PACKED_FILTER_MIN_BITS / PACKED_FILTER_BITS_PER_BLOCK);
        for index in 0..capacity as u64 {
            filter.insert(BlockId::for_payload(&index.to_le_bytes()));
        }
        assert!(filter.saturated());
    }

    #[test]
    fn packed_membership_checks_exact_indexes_after_filter_hits() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let mut payloads = [
            b"membership-a".to_vec(),
            b"membership-b".to_vec(),
            b"membership-c".to_vec(),
        ];
        payloads.sort_by_key(|payload| BlockId::for_payload(payload));
        let ids = payloads
            .iter()
            .map(|payload| store.put(payload).unwrap())
            .collect::<Vec<_>>();
        let packed_ids = vec![ids[0], ids[2]];
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), packed_ids.clone(), |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap())
            .unwrap();
        store.remove_blocks(&packed_ids).unwrap();
        assert!(store.has_catalogued_payload_block(ids[0]).unwrap());
        assert!(store.has_catalogued_payload_block(ids[2]).unwrap());
        // A filter may retain bits from a publication that did not become
        // current. The loose middle ID also lies inside the catalog range.
        store.note_packed(&[ids[1]]).unwrap();
        assert!(store.packed_filter_may_contain(ids[1]).unwrap());
        assert!(!store.has_catalogued_payload_block(ids[1]).unwrap());
        assert_eq!(store.get(ids[1]).unwrap(), payloads[1]);
        let missing = BlockId::for_payload(b"membership-never-written");
        assert!(!store.packed_filter_may_contain(missing).unwrap());
        vfs.arm_fault(None).unwrap();
        assert!(!store.has_catalogued_payload_block(missing).unwrap());
        assert!(vfs.trace().unwrap().is_empty());
    }

    #[test]
    fn blob_staging_avoids_loose_miss_io_and_reuses_verified_packed_content() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"blob content carried by WAL";
        vfs.arm_fault(None).unwrap();
        let (id, carried) = store.put_deferred_blob(payload).unwrap();
        assert!(carried);
        assert!(vfs.trace().unwrap().is_empty());
        store.flush_pending().unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.put_deferred_blob(payload).unwrap(), (id, true));
        assert!(vfs.trace().unwrap().is_empty());
        store.flush_pending().unwrap();
        let segment = store.payload_segment_store().unwrap()
            .create(Uuid::new_v4(), vec![id], |id| store.get(id)).unwrap();
        let reference = PayloadSegmentReference::new(segment.metadata(),
            segment.first_block_id(), segment.last_block_id()).unwrap();
        store.install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap()).unwrap();
        store.remove_blocks(&[id]).unwrap();
        assert_eq!(store.put_deferred_blob(payload).unwrap(), (id, false));
        assert!(store.pending_payload(id).unwrap().is_none());
        assert_eq!(store.get(id).unwrap(), payload);
    }

    #[test]
    fn dedupe_probe_stages_on_unconfirmed_filter_hit() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let packed_payload = b"packed-for-filter-hit".to_vec();
        let packed_id = store.put(&packed_payload).unwrap();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![packed_id], |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap())
            .unwrap();
        store.remove_blocks(&[packed_id]).unwrap();
        // A stale filter bit (or plain bloom false positive) claims a block
        // that no segment index and no loose file actually holds. The write
        // path must not trust that claim: staging is idempotent, a missing
        // block is not.
        let fresh_payload = b"fresh-filter-misclaim".to_vec();
        let fresh_id = BlockId::for_payload(&fresh_payload);
        store.note_packed(&[fresh_id]).unwrap();
        assert!(store.packed_filter_definitely_contains(fresh_id).unwrap());
        assert!(matches!(
            store.probe_dedupe(fresh_id).unwrap(),
            DedupeProbe::Missing
        ));
        let (staged_id, staged) = store.put_deferred(&fresh_payload).unwrap();
        assert_eq!(staged_id, fresh_id);
        assert!(staged, "unconfirmed filter hit must restage the payload");
        store.flush_pending().unwrap();
        assert_eq!(store.get(fresh_id).unwrap(), fresh_payload);
    }

    #[test]
    fn stale_candidate_snapshot_converges_after_segment_retirement() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"stale-snapshot-converges".to_vec();
        let id = store.put(&payload).unwrap();
        // Pack the block into the first catalog generation.
        let first = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
            .unwrap();
        let first_reference = PayloadSegmentReference::new(
            first.metadata(),
            first.first_block_id(),
            first.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![first_reference]).unwrap())
            .unwrap();
        store.remove_blocks(&[id]).unwrap();
        // The reader copies its candidate snapshot here.
        let snapshot = store.payload_segment_candidates(id).unwrap();
        assert_eq!(snapshot, vec![first_reference]);
        // A publication replaces the segment and retires the snapshot's
        // source before the reader's open: exactly the pack-adoption and
        // segment-GC ordering (install first, reclaim second).
        let second = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
            .unwrap();
        let second_reference = PayloadSegmentReference::new(
            second.metadata(),
            second.first_block_id(),
            second.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![second_reference]).unwrap())
            .unwrap();
        store
            .payload_segment_store()
            .unwrap()
            .remove_segment(first_reference.segment_id)
            .unwrap();
        // Probing the stale snapshot converges through the fresh view
        // instead of surfacing the transient race as durable corruption.
        assert_eq!(
            store.get_from_snapshot_candidates(id, &snapshot).unwrap(),
            Some(payload.clone())
        );
        // Both first-probe call sites share the convergence, and the plain
        // read path is unaffected.
        assert!(matches!(
            store.probe_dedupe(id).unwrap(),
            DedupeProbe::Present(_)
        ));
        assert_eq!(store.get(id).unwrap(), payload);
    }

    #[test]
    fn stale_candidate_snapshot_converges_to_a_later_segment() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"joins-after-snapshot".to_vec();
        let id = store.put(&payload).unwrap();
        // Snapshot before any catalog names the block.
        let snapshot = store.payload_segment_candidates(id).unwrap();
        assert!(snapshot.is_empty());
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap())
            .unwrap();
        store.remove_blocks(&[id]).unwrap();
        // A block that joined the catalog after the snapshot resolves
        // through the fresh view instead of reading as missing.
        assert_eq!(
            store.get_from_snapshot_candidates(id, &snapshot).unwrap(),
            Some(payload)
        );
    }

    #[test]
    fn unchanged_catalog_keeps_missing_segment_corruption() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"missing-segment-stays-corruption".to_vec();
        let id = store.put(&payload).unwrap();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap())
            .unwrap();
        store.remove_blocks(&[id]).unwrap();
        let snapshot = store.payload_segment_candidates(id).unwrap();
        // The catalog still names the segment when its file disappears:
        // durable corruption, not a publication race — the integrity fence
        // must not soften.
        store
            .payload_segment_store()
            .unwrap()
            .remove_segment(reference.segment_id)
            .unwrap();
        let error = store.get_from_snapshot_candidates(id, &snapshot).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("missing segment"));
        assert_eq!(
            store.get(id).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn deferred_retirement_keeps_stale_snapshots_resolvable_until_sweep() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"parked-segment-stays-readable".to_vec();
        let id = store.put(&payload).unwrap();
        let first = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
            .unwrap();
        let first_reference = PayloadSegmentReference::new(
            first.metadata(),
            first.first_block_id(),
            first.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![first_reference]).unwrap())
            .unwrap();
        store.remove_blocks(&[id]).unwrap();
        // The reader's pre-publication snapshot.
        let snapshot = store.payload_segment_candidates(id).unwrap();
        assert_eq!(snapshot, vec![first_reference]);
        // Publish the replacement, then park the retired source.
        let second = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
            .unwrap();
        let second_reference = PayloadSegmentReference::new(
            second.metadata(),
            second.first_block_id(),
            second.last_block_id(),
        )
        .unwrap();
        store
            .install_payload_manifest(PayloadManifest::new(vec![second_reference]).unwrap())
            .unwrap();
        let parked_bytes = store
            .payload_segment_store()
            .unwrap()
            .defer_segment_retirement(first_reference.segment_id)
            .unwrap();
        assert_eq!(parked_bytes, first_reference.file_bytes);
        // The stale snapshot resolves against the parked file directly —
        // no changed-view retry, no integrity fence.
        assert_eq!(
            store
                .get_from_payload_segment_references(id, &snapshot)
                .unwrap(),
            Some(payload.clone())
        );
        assert!(store
            .protected_segment_ids()
            .unwrap()
            .contains(&first_reference.segment_id));
        // The time floor holds the park; an unexpired floor reclaims nothing.
        let reclaimed = store
            .payload_segment_store()
            .unwrap()
            .sweep_deferred_retirement_with(std::time::Duration::from_secs(3600))
            .unwrap();
        assert_eq!(reclaimed, 0);
        assert_eq!(
            store
                .get_from_payload_segment_references(id, &snapshot)
                .unwrap(),
            Some(payload.clone())
        );
        // Once the floor expires the sweep unlinks; the stale snapshot then
        // converges through the changed-view retry (the 2026-09-21 fix),
        // so reads never fence either way.
        let reclaimed = store
            .payload_segment_store()
            .unwrap()
            .sweep_deferred_retirement_with(std::time::Duration::ZERO)
            .unwrap();
        assert_eq!(reclaimed, parked_bytes);
        assert!(store
            .payload_segment_store()
            .unwrap()
            .deferred_retirement_ids()
            .unwrap()
            .is_empty());
        assert_eq!(
            store.get_from_snapshot_candidates(id, &snapshot).unwrap(),
            Some(payload)
        );
    }

    #[test]
    fn deferred_retirement_overflow_reclaims_oldest_immediately() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let segment_store = store.payload_segment_store().unwrap();
        let mut ids = Vec::new();
        for index in 0..(crate::payload_segment::MAX_DEFERRED_RETIREMENTS + 1) {
            let payload = format!("overflow-park-{index}").into_bytes();
            let block_id = store.put(&payload).unwrap();
            let segment = segment_store
                .create(Uuid::new_v4(), vec![block_id], |id| store.get(id))
                .unwrap();
            segment_store
                .defer_segment_retirement(segment.metadata().segment_id)
                .unwrap();
            ids.push((segment.metadata().segment_id, segment.metadata().file_bytes));
        }
        // The bound holds: the oldest park was physically reclaimed, the
        // remaining entries stay parked.
        let parked = segment_store.deferred_retirement_ids().unwrap();
        assert_eq!(parked.len(), crate::payload_segment::MAX_DEFERRED_RETIREMENTS);
        assert!(!parked.contains(&ids[0].0));
        assert_eq!(
            segment_store.remove_segment(ids[0].0).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        // Everything still parked sweeps cleanly.
        let reclaimed = segment_store
            .sweep_deferred_retirement_with(std::time::Duration::ZERO)
            .unwrap();
        let expected: u64 = ids[1..].iter().map(|(_, bytes)| *bytes).sum();
        assert_eq!(reclaimed, expected);
        assert!(segment_store.deferred_retirement_ids().unwrap().is_empty());
    }

    #[test]
    fn carried_recovery_reuses_verified_packs_but_not_filter_false_positives() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"durable-packed-recovery";
        let id = store.put(payload).unwrap();
        let segment = store.payload_segment_store().unwrap()
            .create(Uuid::new_v4(), vec![id], |id| store.get(id)).unwrap();
        let reference = PayloadSegmentReference::new(segment.metadata(),
            segment.first_block_id(), segment.last_block_id()).unwrap();
        store.install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap()).unwrap();
        store.remove_blocks(&[id]).unwrap();
        assert_eq!(store.restore_carried(payload).unwrap(), id);
        assert!(store.pending_payload(id).unwrap().is_none());
        let missing = b"carried-only-recovery";
        let missing_id = BlockId::for_payload(missing);
        store.note_packed(&[missing_id]).unwrap();
        assert_eq!(store.restore_carried(missing).unwrap(), missing_id);
        assert_eq!(store.pending_payload(missing_id).unwrap().unwrap().as_slice(), missing);
    }

    #[test]
    fn carried_recovery_lookup_repairs_invalid_files_but_propagates_io_errors() {
        for kind in [io::ErrorKind::InvalidData, io::ErrorKind::NotFound, io::ErrorKind::Interrupted] {
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            let payload = b"WAL remains the repair authority";
            let id = BlockId::for_payload(payload);
            let segment = store.payload_segment_store().unwrap()
                .create(Uuid::new_v4(), vec![id], |_| Ok(payload.to_vec())).unwrap();
            let reference = PayloadSegmentReference::new(segment.metadata(),
                segment.first_block_id(), segment.last_block_id()).unwrap();
            store.install_recovery_payload_manifest(PayloadManifest::new(vec![reference]).unwrap()).unwrap();
            let mut lookup = store.prepare_carried_recovery(BTreeSet::from([id])).unwrap();
            assert!(lookup.complete);
            assert_eq!(lookup.locations[&id].0, Some(reference));
            vfs.arm_fault(Some(FaultRule { operation_number: 1,
                action: FaultAction::ErrorBefore(kind) })).unwrap();
            let result = store.restore_carried_with_lookup(payload, &mut lookup);
            assert!(!vfs.trace().unwrap().is_empty(), "fault must actually be reached");
            if kind == io::ErrorKind::Interrupted {
                assert_eq!(result.unwrap_err().kind(), kind);
                assert!(!lookup.locations[&id].1);
                assert!(store.pending_payload(id).unwrap().is_none());
            } else {
                assert_eq!(result.unwrap(), id);
                assert_eq!(store.pending_payload(id).unwrap().unwrap().as_slice(), payload);
            }
        }
    }

    #[test]
    fn carried_recovery_location_admission_is_bounded() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let ids = (0..=crate::generated_tofudb_ir::MAX_RECOVERY_PACKED_LOCATIONS)
            .map(|i| BlockId::for_payload(&i.to_le_bytes())).collect();
        assert_eq!(store.prepare_carried_recovery(ids).err().unwrap().kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn overlapping_segment_catalog_skips_verified_negative_indexes() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let mut references = Vec::new();
        let mut target = None;
        for segment_number in 0..64 {
            let payloads: std::collections::BTreeMap<_, _> = (0..32)
                .map(|block_number| {
                    let bytes = format!("catalog-{segment_number}-{block_number}").into_bytes();
                    (BlockId::for_payload(&bytes), bytes)
                }).collect();
            if segment_number == 0 {
                let (id, bytes) = payloads.iter().nth(16).unwrap();
                target = Some((*id, bytes.clone()));
            }
            let segment = store.payload_segment_store().unwrap()
                .create(Uuid::new_v4(), payloads.keys().copied().collect(),
                    |id| Ok(payloads[&id].clone())).unwrap();
            references.push(PayloadSegmentReference::new(segment.metadata(),
                segment.first_block_id(), segment.last_block_id()).unwrap());
        }
        let manifest = PayloadManifest::new(references).unwrap();
        store.install_payload_manifest(manifest.clone()).unwrap();
        let (id, bytes) = target.unwrap();
        // A freshly published segment has never served a payload read. Its
        // already-required verification must seed the next launch's hints.
        let reference = manifest.segments.iter().find(|reference| {
            store.payload_segment_store().unwrap().open_segment(reference.segment_id)
                .unwrap().contains(id)
        }).unwrap();
        store.verify_payload_segment(reference).unwrap();
        store.save_read_hints().unwrap();
        let first_open = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap();
        first_open.install_recovery_payload_manifest(manifest.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(first_open.get(id).unwrap(), bytes);
        let publication_opens = vfs.trace().unwrap().iter()
            .filter(|op| **op == Operation::Open).count();
        assert!(publication_opens <= 2, "new pack required {publication_opens} opens before its first read");
        assert!(store.payload_segment_candidates(id).unwrap().len() <= 2);
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.get_from_payload_segments(id).unwrap(), Some(bytes.clone()));
        let opens = vfs.trace().unwrap().iter()
            .filter(|operation| **operation == Operation::Open).count();
        assert!(opens < 12, "negative segment probes reopened {opens} files");
        store.save_read_hints().unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap();
        reopened.install_recovery_payload_manifest(manifest.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(reopened.get(id).unwrap(), bytes);
        let opens = vfs.trace().unwrap().iter()
            .filter(|operation| **operation == Operation::Open).count();
        assert!(opens <= 2, "persisted point-read hint reopened {opens} segments");
        // A validly encoded hint that names a wrong but current segment must
        // fall back to the actual payload, not claim absence or return bytes.
        let wrong = manifest.segments.iter().find(|reference| {
            reference.may_contain(id) && reopened.payload_segment_store().unwrap()
                .open_segment(reference.segment_id).unwrap().get(id).unwrap().is_none()
        }).unwrap();
        reopened.read_hints.write().unwrap().remember(id, wrong.segment_id);
        reopened.cache.write().unwrap().clear();
        assert_eq!(reopened.get(id).unwrap(), bytes);
        store.set_segment_index_cache_bytes(0).unwrap();
        assert!(store.payload_segment_candidates(id).unwrap().len() > 50);
        assert_eq!(store.get_from_payload_segments(id).unwrap(), Some(bytes.clone()));
        let missing_payload = b"WAL-only block";
        let missing_id = BlockId::for_payload(missing_payload);
        vfs.arm_fault(None).unwrap();
        let mut lookup = store.prepare_carried_recovery(BTreeSet::from([id, missing_id])).unwrap();
        let catalog_opens = vfs.trace().unwrap().iter()
            .filter(|operation| **operation == Operation::Open).count();
        assert_eq!(catalog_opens, 64, "recovery must read each verified segment index only once");
        assert!(lookup.complete);
        assert!(lookup.locations[&id].0.is_some());
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.get_for_recovery(id, &lookup).unwrap(), bytes);
        let recovery_opens = vfs.trace().unwrap().iter()
            .filter(|operation| **operation == Operation::Open).count();
        assert!(recovery_opens <= 2, "known recovery location reopened {recovery_opens} segments");
        assert!(lookup.locations[&missing_id].0.is_none());
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.restore_carried_with_lookup(&bytes, &mut lookup).unwrap(), id);
        assert!(store.pending_payload(id).unwrap().is_none());
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.restore_carried_with_lookup(&bytes, &mut lookup).unwrap(), id);
        assert_eq!(store.restore_carried_with_lookup(missing_payload, &mut lookup).unwrap(), missing_id);
        assert!(vfs.trace().unwrap().is_empty(), "verified and WAL-only blocks must not rescan the catalog");
    }

    #[test]
    fn packed_filter_gates_segment_scans_without_hiding_packed_blocks() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let pack = |store: &BlockStore, payload: &[u8]| {
            let id = BlockId::for_payload(payload);
            store
                .stage_pending_payload(id, Cow::Borrowed(payload))
                .unwrap();
            let segment = store
                .payload_segment_store()
                .unwrap()
                .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
                .unwrap();
            store.retire_staged_payloads(&BTreeSet::from([id])).unwrap();
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )
            .unwrap();
            (id, reference)
        };
        let (first_id, first_reference) = pack(&store, b"packed payload one");
        store
            .install_payload_manifest(PayloadManifest::new(vec![first_reference]).unwrap())
            .unwrap();

        // A fresh probe with a built filter provably misses every segment:
        // no segment file may be opened for it.
        let opens_before = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Open)
            .count();
        let missing = BlockId::for_payload(b"never written");
        assert_eq!(
            store.get(missing).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        let opens_after = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Open)
            .count();
        assert_eq!(opens_before, opens_after);

        // The packed block itself keeps no loose file; it must resolve
        // through the filter-hit path.
        assert_eq!(store.get(first_id).unwrap(), b"packed payload one");

        // Blocks packed after the first install rely on note_packed
        // registration; without it the gate would hide them entirely.
        let (second_id, second_reference) = pack(&store, b"packed payload two");
        store.note_packed(&[second_id]).unwrap();
        store
            .install_payload_manifest(
                PayloadManifest::new(vec![first_reference, second_reference]).unwrap(),
            )
            .unwrap();
        assert_eq!(store.get(second_id).unwrap(), b"packed payload two");
    }

    #[test]
    fn owned_staging_reuses_exact_allocations_and_bounds_retained_capacity() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let payload = vec![23; 4096];
        let allocation = payload.as_ptr();
        let (id, pending) = store.put_deferred_fresh_owned(payload).unwrap();
        assert!(pending);
        let retained = store.pending_payload(id).unwrap().unwrap();
        assert_eq!(retained.as_ptr(), allocation);
        assert_eq!(retained.capacity(), retained.len());
        let mut excess_capacity = Vec::with_capacity(64 * 1024);
        excess_capacity.extend_from_slice(&vec![24; 4096]);
        let (second, _) = store.put_deferred_fresh_owned(excess_capacity).unwrap();
        let second_payload = store.pending_payload(second).unwrap().unwrap();
        assert_eq!(second_payload.capacity(), second_payload.len());
        assert_eq!(
            store.put_deferred_fresh_owned(vec![23; 4096]).unwrap(),
            (id, true)
        );
        assert_eq!(
            store.pending_payload(id).unwrap().unwrap().as_ptr(),
            allocation
        );
        assert_eq!(store.blocks_staged.load(Ordering::Relaxed), 2);
        assert_eq!(
            store
                .put_deferred_fresh_owned(vec![0; MAX_BLOCK_BYTES + 1])
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(store.blocks_staged.load(Ordering::Relaxed), 2);
        store.flush_pending().unwrap();
        assert_eq!(store.put_deferred(&vec![23; 4096]).unwrap(), (id, false));
        assert!(store.pending_payload(id).unwrap().is_none());
    }

    #[test]
    fn prepared_publication_keeps_identity_and_allocation_through_rotation_and_dedup() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let mut original = vec![37; 128 * 1024];
        let prepared = PreparedBlockSource::new(&original).unwrap().capture();
        let identity = BlockId::for_payload(&original);
        let allocation = prepared.shared();
        original[0] = 99;
        let mut changed_reader = prepared.shared();
        Arc::make_mut(&mut changed_reader)[1] = 98;
        assert_ne!(changed_reader.as_ptr(), allocation.as_ptr());
        assert_eq!(store.put_deferred_prepared(&prepared).unwrap(), (identity, true));
        assert!(Arc::ptr_eq(&store.pending_payload(identity).unwrap().unwrap(), &allocation));
        assert_eq!(store.get(identity).unwrap(), vec![37; 128 * 1024]);
        store.rotate_pending_for_rotation().unwrap();
        assert_eq!(store.put_deferred_prepared(&prepared).unwrap(), (identity, true));
        assert!(Arc::ptr_eq(&store.pending_payload(identity).unwrap().unwrap(), &allocation));
        assert_eq!(store.blocks_staged.load(Ordering::Relaxed), 1);
        store.restore_rotation_pending().unwrap();
        store.flush_pending().unwrap();
        // Blob publication deliberately carries an optional loose duplicate
        // again; only a verified cached/packed copy suppresses WAL carrying.
        assert_eq!(store.put_deferred_prepared(&prepared).unwrap(), (identity, true));
        assert!(Arc::ptr_eq(&store.pending_payload(identity).unwrap().unwrap(), &allocation));
        store.flush_pending().unwrap();
        store.set_cache_capacity_bytes(256 * 1024).unwrap();
        assert_eq!(store.get(identity).unwrap(), vec![37; 128 * 1024]);
        assert_eq!(store.put_deferred_prepared(&prepared).unwrap(), (identity, false));
        assert!(store.pending_payload(identity).unwrap().is_none());
        assert_eq!(store.get(identity).unwrap(), vec![37; 128 * 1024]);
        assert!(matches!(PreparedBlockSource::new(&vec![0; MAX_BLOCK_BYTES + 1]), Err(error) if error.kind() == io::ErrorKind::InvalidInput));
    }

    #[test]
    fn fresh_prepared_publication_keeps_the_captured_arc_and_fresh_semantics() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let original = Arc::new(vec![37; 4096]);
        let expected = BlockId::for_payload(&original);
        let captured = PreparedBlockSource::capture_shared(Arc::clone(&original)).unwrap();
        assert!(Arc::ptr_eq(&captured.shared(), &original));
        let mut changed = Arc::clone(&original);
        Arc::make_mut(&mut changed)[0] = 99;
        assert_ne!(BlockId::for_payload(&changed), expected);
        assert_eq!(store.put_deferred_fresh_prepared(&captured).unwrap(), (expected, true));
        assert!(Arc::ptr_eq(&store.pending_payload(expected).unwrap().unwrap(), &original));
        store.rotate_pending_for_rotation().unwrap();
        store.put_deferred_fresh_prepared(&captured).unwrap();
        assert_eq!(store.blocks_staged.load(Ordering::Relaxed), 1);
        store.restore_rotation_pending().unwrap();
        store.flush_pending().unwrap();
        store.set_cache_capacity_bytes(65536).unwrap();
        assert_eq!(store.get(expected).unwrap(), *original);
        // Fresh pages deliberately stage even if a verified durable copy exists.
        assert_eq!(store.put_deferred_fresh_prepared(&captured).unwrap(), (expected, true));
        assert!(Arc::ptr_eq(&store.pending_payload(expected).unwrap().unwrap(), &original));
        assert!(matches!(PreparedBlockSource::capture_shared(Arc::new(vec![0; MAX_BLOCK_BYTES + 1])),
            Err(error) if error.kind() == io::ErrorKind::InvalidInput));
    }

    #[test]
    fn checkpoint_history_staging_retains_the_encoder_allocation() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let payload = vec![17; MAX_BLOCK_BYTES];
        let allocation = payload.as_ptr();
        let id = store.stage_checkpoint_history(payload).unwrap();
        let retained = store.pending_payload(id).unwrap().unwrap();
        assert_eq!(retained.as_ptr(), allocation);
        assert_eq!(retained.len(), MAX_BLOCK_BYTES);
        // Repeated content does not replace the first allocation or charge
        // another staged segment against the authority's residency budget.
        assert_eq!(
            store
                .stage_checkpoint_history(vec![17; MAX_BLOCK_BYTES])
                .unwrap(),
            id
        );
        assert_eq!(
            store.pending_payload(id).unwrap().unwrap().as_ptr(),
            allocation
        );
        assert_eq!(store.blocks_staged.load(Ordering::Relaxed), 1);
        assert_eq!(
            store
                .stage_checkpoint_history(vec![0; MAX_BLOCK_BYTES + 1])
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn background_pack_wake_before_wait_preserves_the_notification() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let before = store.background_pack_revision();
        store.wake_background_packer().unwrap();
        assert!(store
            .wait_for_background_pack_change(before, Duration::ZERO)
            .unwrap());
        let unchanged = store.background_pack_revision();
        assert!(!store
            .wait_for_background_pack_change(unchanged, Duration::ZERO)
            .unwrap());
        store.stage_checkpoint_history(b"history".to_vec()).unwrap();
        assert!(store
            .wait_for_background_pack_change(unchanged, Duration::ZERO)
            .unwrap());
        let (id, _) = store
            .begin_background_pack(1, MAX_BLOCK_BYTES as u64)
            .unwrap()
            .unwrap();
        let reserved = store.background_pack_revision();
        store.fail_background_pack(id).unwrap();
        assert!(store
            .wait_for_background_pack_change(reserved, Duration::ZERO)
            .unwrap());
    }

    #[test]
    fn checkpoint_wait_observes_completion_or_failure_before_sleep() {
        for completed in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
            store.put_deferred(b"waiting payload").unwrap();
            let (segment_id, ids) = store.begin_background_pack(1, 1024).unwrap().unwrap();
            if completed {
                let segment = store
                    .payload_segment_store()
                    .unwrap()
                    .create(segment_id, ids.clone(), |id| store.get(id))
                    .unwrap();
                let reference = PayloadSegmentReference::new(
                    segment.metadata(),
                    segment.first_block_id(),
                    segment.last_block_id(),
                )
                .unwrap();
                store.note_packed(&ids).unwrap();
                store
                    .complete_background_pack(segment_id, reference)
                    .unwrap();
            } else {
                store.fail_background_pack(segment_id).unwrap();
            }
            // Both states still own unpublished payloads. Neither has an active
            // create to wait for, even if the notification preceded this call.
            assert!(store.has_pending_payloads().unwrap());
            let waiting_store = Arc::clone(&store);
            let (sender, receiver) = std::sync::mpsc::channel();
            let waiter = std::thread::spawn(move || {
                let result = waiting_store.wait_for_background_pack(Duration::from_secs(30));
                sender.send(result).unwrap();
            });
            let result = receiver.recv_timeout(Duration::from_secs(2));
            // Release and join even on regression, before asserting the result.
            store.wake_background_packer().unwrap();
            waiter.join().unwrap();
            result
                .expect("finished reservation must not enter a timed wait")
                .unwrap();
        }
    }

    #[test]
    fn background_pack_reservation_is_exclusive_and_recoverable() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put_deferred(b"alpha").unwrap().0;
        let second = store.put_deferred(b"beta").unwrap().0;

        // Too small to reserve: the foreground keeps sub-threshold batches.
        assert!(store.begin_background_pack(1024, 2048).unwrap().is_none());

        let (segment_id, reserved) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .expect("staged batch is reservable");
        assert_eq!(
            reserved.iter().copied().collect::<BTreeSet<_>>(),
            [first, second].into_iter().collect::<BTreeSet<_>>()
        );
        // One pack at a time, and the reserved blocks leave the foreground
        // snapshot so no second segment duplicates them.
        assert!(store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .is_none());
        assert!(store.pending_checkpoint_snapshot().unwrap().0.is_empty());
        assert!(store.background_pack_creating().unwrap());

        // Failure releases the same payloads for the next attempt.
        store.fail_background_pack(segment_id).unwrap();
        assert!(!store.background_pack_creating().unwrap());
        let (segment_id, reserved) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .unwrap();
        assert_eq!(reserved.len(), 2);

        // Completion hands the batch to foreground publication exactly once.
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, reserved.clone(), |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store.note_packed(&reserved).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
        assert!(store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .is_none());
        assert!(store.pending_checkpoint_snapshot().unwrap().0.is_empty());
        let (published_reference, packed, publication) = store
            .take_completed_background_pack()
            .unwrap()
            .expect("finished pack awaits publication");
        assert_eq!(published_reference.segment_id, segment_id);
        assert_eq!(packed, reserved.iter().copied().collect::<BTreeSet<_>>());
        assert!(store.take_completed_background_pack().unwrap().is_none());
        assert!(store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .is_none());
        assert_eq!(
            store.unpublished_background_segment_ids().unwrap(),
            vec![segment_id]
        );
        assert_eq!(
            store.pending_checkpoint_snapshot().unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        // A failed publication releases admission but leaves every source
        // payload available for retry; no durable state was retired.
        drop(publication);
        assert!(store
            .unpublished_background_segment_ids()
            .unwrap()
            .is_empty());
        let (_, retry_ids) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .unwrap();
        assert_eq!(retry_ids, reserved);
    }

    #[test]
    fn stolen_pack_completion_is_stale_not_a_hard_mismatch() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put_deferred(&vec![3_u8; 1024]).unwrap().0;
        let second = store.put_deferred(&vec![4_u8; 1024]).unwrap().0;

        let (segment_id, reserved) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .unwrap();
        // The worker is mid-create when the checkpoint drain's wait budget
        // runs out: the drain steals the payloads and packs them in the
        // foreground.
        store.fail_active_background_pack().unwrap();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, reserved.clone(), |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        // The late completion is a quiet stale verdict, never the hard
        // mismatch that used to surface as a failed round.
        assert_eq!(
            store
                .complete_background_pack(segment_id, reference)
                .unwrap(),
            BackgroundPackCompletion::Stale
        );
        // Its redundant segment file is reclaimable and no payload is lost.
        assert!(
            store
                .payload_segment_store()
                .unwrap()
                .remove_segment(segment_id)
                .unwrap()
                > 0
        );
        assert_eq!(store.get(first).unwrap(), vec![3_u8; 1024]);
        assert_eq!(store.get(second).unwrap(), vec![4_u8; 1024]);
        // The reservation slot is free again immediately — no backoff state
        // survives to delay the next pack.
        let (_, retry_ids) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .unwrap();
        assert_eq!(retry_ids, reserved);
    }

    #[test]
    fn unknown_pack_completion_remains_a_hard_mismatch() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        store.put_deferred(&vec![5_u8; 1024]).unwrap();
        let (_, reserved) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .unwrap();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), reserved.clone(), |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        // A completion that was never reserved and never stolen stays a
        // contract violation — the stale path must not swallow real bugs.
        let error = store
            .complete_background_pack(reference.segment_id, reference)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn background_pack_byte_cap_still_qualifies_below_minimum() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put_deferred(&vec![1_u8; 5 * 1024 * 1024]).unwrap().0;
        let second = store.put_deferred(&vec![2_u8; 5 * 1024 * 1024]).unwrap().0;

        // 5 MiB + 5 MiB cannot both fit the 8 MiB budget: the batch stops
        // on the byte cap and qualifies without reaching the minimum.
        let (_, reserved) = store
            .begin_background_pack(8 * 1024 * 1024, 8 * 1024 * 1024)
            .unwrap()
            .expect("byte-capped batch is a full batch");
        assert_eq!(reserved.len(), 1);
        assert!(reserved.contains(&first) || reserved.contains(&second));
    }

    #[test]
    fn background_pack_skips_oversized_lone_payloads() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let oversized = store.put_deferred(&vec![7_u8; 10 * 1024 * 1024]).unwrap().0;
        let small_a = store.put_deferred(&vec![8_u8; 1024 * 1024]).unwrap().0;
        let small_b = store.put_deferred(&vec![9_u8; 1024 * 1024]).unwrap().0;

        let (_, reserved) = store
            .begin_background_pack(1024 * 1024, 8 * 1024 * 1024)
            .unwrap()
            .expect("small payloads form a reservable batch");
        assert!(!reserved.is_empty());
        assert!(!reserved.contains(&oversized));
        let reserved_set: BTreeSet<_> = reserved.iter().copied().collect();
        assert!(reserved_set.is_subset(&[small_a, small_b].into_iter().collect()));
        // The oversized payload stays staged for the foreground loose path.
        assert!(store
            .pending_checkpoint_snapshot()
            .unwrap()
            .0
            .iter()
            .any(|(id, _)| *id == oversized));
    }
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

    #[test]
    fn staged_reference_flush_rejects_corrupt_duplicate_without_retiring_valid_payloads() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"complete carried source";
        let id = store.put_loose(payload).unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        let mut file = vfs.open(&store.path(id), OpenRequest { write: true, ..OpenRequest::default() }).unwrap();
        file.write_all_at(HEADER_BYTES as u64, b"X").unwrap();
        file.sync_all().unwrap();
        assert_eq!(store.put_deferred_blob(payload).unwrap(), (id, true));
        assert_eq!(store.flush_pending_references(&BTreeSet::from([id])).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert_eq!(store.pending_payload(id).unwrap().unwrap().as_slice(), payload);
        assert_eq!(store.read_loose_block(id, &store.path(id)).unwrap_err().kind(), io::ErrorKind::InvalidData);
        // The caller must repair explicitly; failure did not silently rewrite.
        file.write_all_at(HEADER_BYTES as u64, &payload[..1]).unwrap();
        file.sync_all().unwrap();
        store.begin_relink_observation();
        store.flush_pending_references(&BTreeSet::from([id])).unwrap();
        assert!(store.pending_payload(id).unwrap().is_none());
        assert!(store.relink_fence_snapshot().ids.contains(&id));
        drop(store);
        vfs.crash().unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs)).unwrap();
        assert_eq!(reopened.get(id).unwrap(), payload);
    }

    #[test]
    fn staged_reference_flush_directory_and_sync_failures_keep_all_payloads() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let candidates: [&[u8]; 2] = [b"duplicate source survives sync failure", b"directory collision has no complete-copy witness"];
        let (payload, directory_payload) = if BlockId::for_payload(candidates[0]) < BlockId::for_payload(candidates[1]) {
            (candidates[0], candidates[1])
        } else { (candidates[1], candidates[0]) };
        let id = store.put_loose(payload).unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        assert_eq!(store.put_deferred_blob(payload).unwrap(), (id, true));
        vfs.arm_operation_fault(Operation::SyncAll, 1, FaultAction::ErrorBefore(io::ErrorKind::Other)).unwrap();
        assert!(store.flush_pending_references(&BTreeSet::from([id])).is_err());
        assert!(vfs.operation_fault_fired().unwrap());
        assert_eq!(store.pending_payload(id).unwrap().unwrap().as_slice(), payload);
        vfs.arm_fault(None).unwrap();
        let directory_id = BlockId::for_payload(directory_payload);
        assert!(id < directory_id, "valid duplicate must be processed before the failing directory");
        let shard = store.ensure_shard(directory_id).unwrap();
        vfs.create_dir(&store.path(directory_id)).unwrap();
        vfs.sync_directory(&shard).unwrap();
        assert_eq!(store.put_deferred_blob(directory_payload).unwrap(), (directory_id, true));
        store.begin_relink_observation();
        assert_eq!(store.flush_pending_references(&BTreeSet::from([id, directory_id])).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert!(store.pending_payload(id).unwrap().is_some());
        assert!(store.pending_payload(directory_id).unwrap().is_some());
        assert!(store.relink_fence_snapshot().ids.contains(&id), "first duplicate completed fresh verification before the batch failed");
    }

    #[test]
    fn rotation_staged_reference_flush_survives_power_loss() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payload = b"reference-only commit during rotation";
        let id = store.put_deferred(payload).unwrap().0;
        store.rotate_pending_for_rotation().unwrap();
        store.flush_pending_references(&BTreeSet::from([id])).unwrap();
        drop(store);
        vfs.crash().unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs)).unwrap();
        assert_eq!(reopened.get(id).unwrap(), payload);
    }

    fn simulated_store(vfs: &DeterministicVfs) -> BlockStore {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        BlockStore::initialize_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap()
    }

    #[test]
    fn repeated_reads_serve_from_payload_cache() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let id = store.put(b"cached payload").unwrap();
        let reads_after_put = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(store.get(id).unwrap(), b"cached payload");
        assert_eq!(store.get(id).unwrap(), b"cached payload");
        assert_eq!(store.get(id).unwrap(), b"cached payload");
        let reads_after_gets = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(reads_after_gets, reads_after_put + 1);
    }

    #[test]
    fn cache_evicts_oldest_payloads_within_byte_budget() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_cache_capacity_bytes(16).unwrap();
        let first = store.put(b"0123456789").unwrap();
        let second = store.put(b"abcdefghij").unwrap();
        assert_eq!(store.get(first).unwrap(), b"0123456789");
        assert_eq!(store.get(second).unwrap(), b"abcdefghij");
        let reads_before = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        // `first` was evicted when `second` landed; reading it again must go
        // back to disk, while `second` stays cached.
        assert_eq!(store.get(second).unwrap(), b"abcdefghij");
        assert_eq!(store.get(first).unwrap(), b"0123456789");
        let reads_after = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(reads_after, reads_before + 1);
    }

    #[test]
    fn removed_blocks_leave_the_payload_cache() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let id = store.put(b"reclaimable").unwrap();
        assert_eq!(store.get(id).unwrap(), b"reclaimable");
        store.remove_blocks(&[id]).unwrap();
        assert_eq!(store.get(id).unwrap_err().kind(), io::ErrorKind::NotFound);
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

    /// A duplicate packed beyond the 16-candidate dedupe window is still
    /// recognized as catalogued (never restaged), while full reads keep
    /// resolving the deep original.
    #[test]
    fn deep_duplicate_restages_but_full_reads_stay_complete() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let shared = b"deep-duplicate-payload".to_vec();
        let shared_id = BlockId::for_payload(&shared);
        // Seventeen wide-range segments whose [first,last] windows all contain
        // the shared id; the shared block itself sits in the segment with the
        // lowest canonical key, beyond the window's hash-wise end.
        let mut straddlers: Vec<(BlockId, BlockId, Vec<u8>, Vec<u8>)> = Vec::new();
        let mut nonce = 0_u32;
        while straddlers.len() < 17 {
            nonce += 1;
            let low_payload = format!("dedupe-window-low-{nonce}").into_bytes();
            let high_payload = format!("dedupe-window-high-{nonce}").into_bytes();
            let low = BlockId::for_payload(&low_payload);
            let high = BlockId::for_payload(&high_payload);
            if low < shared_id && shared_id < high {
                straddlers.push((low, high, low_payload, high_payload));
            }
        }
        let mut references = Vec::new();
        let mut packed_ids = vec![shared_id];
        // Give the original segment the smallest first_block_id of all
        // candidates, so it sorts beyond the 16-candidate hash-wise window.
        let mut floor_nonce = 0_u32;
        let floor_payload = loop {
            floor_nonce += 1;
            let candidate = format!("dedupe-window-floor-{floor_nonce}").into_bytes();
            let candidate_id = BlockId::for_payload(&candidate);
            if straddlers.iter().all(|(low, _, _, _)| candidate_id < *low) {
                break candidate;
            }
        };
        let floor_id = BlockId::for_payload(&floor_payload);
        packed_ids.push(floor_id);
        // The shared block exists only inside the packed segment — never as a
        // loose copy — so the windowed probe genuinely cannot reach it.
        let mut offline: std::collections::HashMap<BlockId, Vec<u8>> =
            std::collections::HashMap::new();
        offline.insert(floor_id, floor_payload);
        offline.insert(shared_id, shared.clone());
        let original_segment = store
            .payload_segment_store()
            .unwrap()
            .create(
                Uuid::new_v4(),
                vec![floor_id, shared_id],
                |key| {
                    offline
                        .get(&key)
                        .cloned()
                        .ok_or_else(|| invalid_data("offline payload missing"))
                },
            )
            .unwrap();
        references.push(
            PayloadSegmentReference::new(
                original_segment.metadata(),
                original_segment.first_block_id(),
                original_segment.last_block_id(),
            )
            .unwrap(),
        );
        for (low, high, low_payload, high_payload) in &straddlers {
            store.put(low_payload).unwrap();
            store.put(high_payload).unwrap();
            packed_ids.push(*low);
            packed_ids.push(*high);
            let segment = store
                .payload_segment_store()
                .unwrap()
                .create(Uuid::new_v4(), vec![*low, *high], |key| store.get(key))
                .unwrap();
            references.push(
                PayloadSegmentReference::new(
                    segment.metadata(),
                    segment.first_block_id(),
                    segment.last_block_id(),
                )
                .unwrap(),
            );
        }
        store
            .install_payload_manifest(PayloadManifest::new(references).unwrap())
            .unwrap();
        store.note_packed(&packed_ids).unwrap();
        // The bounded probe window must not sweep, but it also must not
        // restage: the packed filter proves the deep duplicate is already
        // catalogued, so the put reports existing without a byte re-read.
        // Staged chunks stay disjoint from published segments (the invariant
        // the checkpoint pack merge relies on).
        assert_eq!(
            store
                .put_deferred_content(std::borrow::Cow::Borrowed(&shared), true)
                .unwrap(),
            (shared_id, false)
        );
        // Full reads keep complete coverage and resolve the deep original.
        assert_eq!(store.get(shared_id).unwrap(), shared);
        // Repeat probes answer the same way and never restage.
        assert_eq!(
            store
                .put_deferred_content(std::borrow::Cow::Borrowed(&shared), true)
                .unwrap(),
            (shared_id, false)
        );
    }
}

/// Immutable metadata-source selection copied only from the selected manifest.
/// It never enumerates another currently installed root or proves existence.
pub(crate) struct HistoryPayloadMetadataSelection {
    payload_root_id: Option<BlockId>,
    index_ids: Vec<BlockId>,
}
impl HistoryPayloadMetadataSelection {
    pub(crate) fn capture(
        manifest: &crate::history::HistoryManifest, maximum_snapshot_bytes: u64,
    ) -> io::Result<Self> {
        let count = manifest.segments.iter().filter(|entry| entry.payload_index_block_id.is_some()).count();
        let resident_bytes = |capacity: usize| -> Option<u64> {
            capacity.checked_mul(std::mem::size_of::<BlockId>())?
                .checked_add(std::mem::size_of::<Self>())?.try_into().ok()
        };
        if resident_bytes(count).is_none_or(|bytes| bytes > maximum_snapshot_bytes) {
            return Err(io::Error::new(io::ErrorKind::WouldBlock, "GC metadata selection exceeds workspace budget"));
        }
        let mut index_ids = Vec::new();
        index_ids.try_reserve_exact(count).map_err(|_|
            io::Error::new(io::ErrorKind::WouldBlock, "GC metadata selection allocation unavailable"))?;
        if resident_bytes(index_ids.capacity()).is_none_or(|bytes| bytes > maximum_snapshot_bytes) {
            return Err(io::Error::new(io::ErrorKind::WouldBlock, "GC metadata selection exceeds workspace budget"));
        }
        index_ids.extend(manifest.segments.iter().filter_map(|entry| entry.payload_index_block_id));
        Ok(Self { payload_root_id: manifest.payload_index_root_id, index_ids })
    }
    pub(crate) fn resident_bytes(&self) -> u64 {
        (std::mem::size_of::<Self>() + self.index_ids.capacity() * std::mem::size_of::<BlockId>()) as u64
    }
}
