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

use crate::generated_tofudb_ir::BLOCK_PAYLOAD_CACHE_ENTRY_OVERHEAD_BYTES;
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::{CatalogLocationTable, PayloadSegment, PayloadSegmentStore};
use crate::vfs::{
    hyphenated_temporary_name, is_hyphenated_temporary_name, sync_all_barrier, sync_directory_barrier, FileKind,
    OpenRequest, RealVfs, Vfs,
};
#[cfg(test)]
use crate::vfs::{VfsFile, VfsPositionedReader};
use crate::FORMAT_VERSION;

const MAGIC: &[u8; 8] = b"TDBBLK01";
const HEADER_BYTES: usize = 8 + 4 + 4;
const FOOTER_BYTES: usize = 4 + 32;
/// Smallest loose block file the read path accepts (an empty payload).
pub(crate) const MIN_BLOCK_FILE_BYTES: u64 = (HEADER_BYTES + FOOTER_BYTES) as u64;
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
///
/// Negative answers are trusted only for a catalog publication whose every
/// segment the filter covers, tracked per segment. Every publication keeps
/// that true: `verify_payload_segment` inserts each segment a publisher
/// builds, and the install reads any catalogued segment still uncovered
/// (docs/TOFUDB_BLOCK_READ_PATH.md, "Packed filter across publications").
const PACKED_FILTER_MIN_BITS: usize = 1 << 20;
const PACKED_FILTER_BITS_PER_BLOCK: usize = 16;
const PACKED_FILTER_ROUNDS: u64 = 11;

struct PackedBlockFilter {
    words: Vec<u64>,
    mask: usize,
    built: bool,
    /// Insertions that set a bit since the last build, compared against
    /// `capacity_blocks` to trigger the geometric rebuild. Re-inserting a
    /// block the filter already holds (a merged or rewritten segment) sets
    /// no bit and adds no load.
    insertions: usize,
    capacity_blocks: usize,
    revision: u64,
    /// Negative answers cover exactly this process-local catalog publication.
    covered_catalog_publication: u64,
    /// Segments whose every block ID the bits hold, with the exact reference
    /// their verified index matched.
    covered_segments: HashMap<Uuid, PayloadSegmentReference>,
    /// Covered segments outside the installed catalog, oldest first: verified
    /// for a publication that has not installed yet, or never will.
    uncatalogued_segments: VecDeque<Uuid>,
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
            covered_segments: HashMap::new(),
            uncatalogued_segments: VecDeque::new(),
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
            covered_segments: HashMap::new(),
            uncatalogued_segments: VecDeque::new(),
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
        let (mut position, step) = Self::hashes(id);
        let mut added = false;
        for _ in 0..PACKED_FILTER_ROUNDS {
            let bit = (position as usize) & self.mask;
            let word = &mut self.words[bit / 64];
            added |= *word & (1 << (bit % 64)) == 0;
            *word |= 1 << (bit % 64);
            position = position.wrapping_add(step);
        }
        if added {
            self.insertions += 1;
        }
    }

    fn covers_segment(&self, reference: &PayloadSegmentReference) -> bool {
        self.covered_segments.get(&reference.segment_id) == Some(reference)
    }

    fn covers_catalog(&self, catalog: &PayloadManifest) -> bool {
        self.built && catalog.segments.iter().all(|reference| self.covers_segment(reference))
    }

    /// Inserts every block ID of one verified segment index. Records of
    /// segments outside the installed catalog are bounded by one catalog
    /// (`MAX_PAYLOAD_SEGMENTS`, the most one publication can add); forgetting
    /// the oldest record keeps its bits and only costs an index read if that
    /// segment is ever installed.
    fn cover_segment(&mut self, reference: &PayloadSegmentReference, ids: impl Iterator<Item = BlockId>) {
        if !self.built || self.covers_segment(reference) {
            return;
        }
        for id in ids {
            self.insert(id);
        }
        self.covered_segments.insert(reference.segment_id, *reference);
        self.uncatalogued_segments.push_back(reference.segment_id);
        if self.uncatalogued_segments.len() > crate::payload_manifest::MAX_PAYLOAD_SEGMENTS {
            if let Some(oldest) = self.uncatalogued_segments.pop_front() {
                self.covered_segments.remove(&oldest);
            }
        }
        self.revision = self.revision.wrapping_add(1);
    }

    /// Keeps the coverage records of the installed catalog and of segments
    /// still awaiting publication. Dropped segments keep their bits: harmless
    /// positives until the next rebuild.
    fn retain_catalog(&mut self, catalog: &HashSet<Uuid>) {
        self.uncatalogued_segments.retain(|segment_id| !catalog.contains(segment_id));
        let pending: HashSet<Uuid> = self.uncatalogued_segments.iter().copied().collect();
        self.covered_segments
            .retain(|segment_id, _| catalog.contains(segment_id) || pending.contains(segment_id));
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
    history: Option<(Arc<crate::history_payload::Catalog>, crate::history_payload::RecoveryLookup)>,
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

/// What one online-GC deletion hold did with one loose block file.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum LooseRemoval {
    /// The hold's validation kept the file.
    Kept,
    /// The file was already gone.
    Missing,
    /// The hold unlinked the file; its shard directory still needs a barrier.
    Unlinked { bytes: u64 },
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

/// Byte-bounded exact LRU over verified block payloads. Population happens
/// only after the full on-disk verification path (magic, version, CRC32C,
/// content hash) has run, never at write time, so a corruption test that
/// rewrites a block behind the store still observes the failure on its next
/// read.
///
/// Each entry is charged its payload's allocated capacity plus a fixed
/// metadata overhead (slot, index entry, shared-vector header), so the one
/// launch-derived byte budget bounds small and zero-byte payloads too. A
/// separate count cap left a multi-GiB budget unreachable: typical blocks are
/// 2-4 KiB, so 65,536 entries used about 1-2% of it and a larger working set
/// thrashed. Recency is an intrusive doubly linked list over a slot slab:
/// access, insertion and eviction are constant time, with no recency records
/// to compact.
struct BlockPayloadCache {
    index: HashMap<BlockId, u32>,
    slots: Vec<CacheSlot>,
    free_slots: Vec<u32>,
    newest: u32,
    oldest: u32,
    resident_bytes: usize,
    capacity_bytes: usize,
}

const NO_SLOT: u32 = u32::MAX;

struct CacheSlot {
    id: BlockId,
    payload: Option<Arc<Vec<u8>>>,
    /// Toward the most recently used end; `NO_SLOT` for the newest entry.
    newer: u32,
    /// Toward the least recently used end; `NO_SLOT` for the oldest entry.
    older: u32,
}

fn cache_charge(payload: &Vec<u8>) -> usize {
    payload
        .capacity()
        .saturating_add(BLOCK_PAYLOAD_CACHE_ENTRY_OVERHEAD_BYTES)
}

impl BlockPayloadCache {
    fn new(capacity_bytes: usize) -> Self {
        Self {
            index: HashMap::new(),
            slots: Vec::new(),
            free_slots: Vec::new(),
            newest: NO_SLOT,
            oldest: NO_SLOT,
            resident_bytes: 0,
            capacity_bytes,
        }
    }

    fn get(&mut self, id: BlockId) -> Option<Arc<Vec<u8>>> {
        let slot = *self.index.get(&id)?;
        self.move_to_newest(slot);
        self.slots[slot as usize].payload.clone()
    }

    /// Read-only lookup for probe paths: a hit skips the recency update a
    /// full `get` would perform, and a miss never needed it.
    fn peek_shared(&self, id: BlockId) -> Option<Arc<Vec<u8>>> {
        let slot = *self.index.get(&id)?;
        self.slots[slot as usize].payload.clone()
    }

    fn insert_shared(&mut self, id: BlockId, payload: Arc<Vec<u8>>) {
        let charge = cache_charge(&payload);
        if self.capacity_bytes == 0 || charge > self.capacity_bytes {
            return;
        }
        if let Some(&slot) = self.index.get(&id) {
            let previous = self.slots[slot as usize].payload.replace(payload);
            self.resident_bytes -= previous.as_deref().map_or(0, cache_charge);
            self.resident_bytes += charge;
            self.move_to_newest(slot);
        } else {
            // Evict before admitting so neither the budget nor the slab ever
            // exceeds its bound, even transiently.
            while self.oldest != NO_SLOT
                && (self.resident_bytes.saturating_add(charge) > self.capacity_bytes
                    || (self.free_slots.is_empty() && self.slots.len() >= NO_SLOT as usize))
            {
                self.evict_oldest();
            }
            let slot = match self.free_slots.pop() {
                Some(slot) => {
                    self.slots[slot as usize] = CacheSlot { id, payload: Some(payload), newer: NO_SLOT, older: NO_SLOT };
                    slot
                }
                None => {
                    self.slots.push(CacheSlot { id, payload: Some(payload), newer: NO_SLOT, older: NO_SLOT });
                    (self.slots.len() - 1) as u32
                }
            };
            self.link_newest(slot);
            self.index.insert(id, slot);
            self.resident_bytes += charge;
        }
        self.evict_to_budget();
    }

    fn insert(&mut self, id: BlockId, payload: &[u8]) {
        if self.capacity_bytes == 0
            || payload.len().saturating_add(BLOCK_PAYLOAD_CACHE_ENTRY_OVERHEAD_BYTES) > self.capacity_bytes
        {
            return;
        }
        self.insert_shared(id, Arc::new(payload.to_vec()));
    }

    fn remove(&mut self, id: BlockId) {
        if let Some(slot) = self.index.remove(&id) {
            self.release_slot(slot);
        }
    }

    fn clear(&mut self) {
        self.index.clear();
        self.slots.clear();
        self.free_slots.clear();
        self.newest = NO_SLOT;
        self.oldest = NO_SLOT;
        self.resident_bytes = 0;
    }

    fn set_capacity_bytes(&mut self, capacity_bytes: usize) {
        self.capacity_bytes = capacity_bytes;
        if capacity_bytes == 0 {
            self.index = HashMap::new();
            self.slots = Vec::new();
            self.free_slots = Vec::new();
            self.newest = NO_SLOT;
            self.oldest = NO_SLOT;
            self.resident_bytes = 0;
            return;
        }
        self.evict_to_budget();
    }

    fn evict_to_budget(&mut self) {
        while self.resident_bytes > self.capacity_bytes && self.oldest != NO_SLOT {
            self.evict_oldest();
        }
    }

    fn evict_oldest(&mut self) {
        let slot = self.oldest;
        if slot == NO_SLOT {
            return;
        }
        let id = self.slots[slot as usize].id;
        self.index.remove(&id);
        self.release_slot(slot);
    }

    /// Unlinks an already de-indexed slot and returns it to the free list.
    fn release_slot(&mut self, slot: u32) {
        self.unlink(slot);
        let released = self.slots[slot as usize].payload.take();
        self.resident_bytes -= released.as_deref().map_or(0, cache_charge);
        self.free_slots.push(slot);
    }

    fn unlink(&mut self, slot: u32) {
        let (newer, older) = {
            let entry = &self.slots[slot as usize];
            (entry.newer, entry.older)
        };
        if newer == NO_SLOT {
            self.newest = older;
        } else {
            self.slots[newer as usize].older = older;
        }
        if older == NO_SLOT {
            self.oldest = newer;
        } else {
            self.slots[older as usize].newer = newer;
        }
        let entry = &mut self.slots[slot as usize];
        entry.newer = NO_SLOT;
        entry.older = NO_SLOT;
    }

    fn link_newest(&mut self, slot: u32) {
        let previous = self.newest;
        {
            let entry = &mut self.slots[slot as usize];
            entry.newer = NO_SLOT;
            entry.older = previous;
        }
        if previous == NO_SLOT {
            self.oldest = slot;
        } else {
            self.slots[previous as usize].newer = slot;
        }
        self.newest = slot;
    }

    fn move_to_newest(&mut self, slot: u32) {
        if self.newest != slot {
            self.unlink(slot);
            self.link_newest(slot);
        }
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.index.len()
    }

    #[cfg(test)]
    fn oldest_first(&self) -> Vec<BlockId> {
        let mut order = Vec::with_capacity(self.index.len());
        let mut slot = self.oldest;
        while slot != NO_SLOT {
            order.push(self.slots[slot as usize].id);
            slot = self.slots[slot as usize].newer;
        }
        order
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
    pub(crate) fn byte_len(&self) -> usize { self.bytes.len() }
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

/// One hash shard's share of a loose batch: staged payloads to materialize
/// and fsync-only marks, published together by one directory barrier.
#[derive(Default)]
struct LooseShardBatch {
    payloads: Vec<(BlockId, Arc<Vec<u8>>)>,
    sync_only: Vec<BlockId>,
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
    file: crate::vfs::SharedReadFile,
    source_bytes: u32,
}
impl HistorySourceHandle {
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        read_history_source_at(&self.file, offset, bytes)
    }
}
fn read_history_source_at(file: &crate::vfs::SharedReadFile, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
    file.read_exact_at_staged(offset, bytes, crate::fsync_stats::STAGE_BLOCK_HISTORY_SOURCE_WAIT,
        crate::fsync_stats::STAGE_BLOCK_HISTORY_SOURCE_READ)
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
    /// Exact block locations of one catalog publication, shared by foreground
    /// reads, dedupe and membership probes and whole-store passes
    /// (docs/TOFUDB_BLOCK_READ_PATH.md, "Published catalog location table").
    /// Reconstructible memory: the first traversal pass publishes the table it
    /// builds, and the maintenance worker derives each later publication's
    /// table off the authority mutex.
    catalog_locations: RwLock<Option<PublishedCatalogLocations>>,
    /// The table the published one replaced. The next derivation writes into
    /// it once no reader holds it, so the published table and this spare
    /// alternate as the two allocations every derivation reuses.
    retired_catalog_locations: Mutex<Option<Arc<CatalogLocationTable>>>,
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
    publication_windows: Mutex<PublicationWindows>,
    /// Hash shards (one per first content-ID byte) this store created whose
    /// root barrier has not succeeded yet. The next writer into such a shard
    /// repeats the barrier even though the directory already exists.
    unpublished_shards: [AtomicBool; 256],
}

/// Loose blocks written inside open off-mutex publication windows: a WAL
/// rotation from preparation to installation, and a history-compaction or
/// pack-adoption publication (docs/TOFUDB_ONLINE_GC.md rule S9). A window
/// makes its blocks reachable from engine state only when it installs, so
/// every online-GC mark captured while it is open includes the IDs
/// `put_loose` handled since it opened, plus its seed. Closing a window
/// drops its set; each set holds the loose blocks of one window.
#[derive(Default)]
struct PublicationWindows {
    next: u64,
    open: BTreeMap<u64, HashSet<BlockId>>,
}

/// Verified reads for one whole-store pass that resolves blocks by ID: online
/// GC marks and backup-prune marks (docs/TOFUDB_BLOCK_READ_PATH.md, "Catalog
/// location table for whole-store passes"). Construction streams every segment
/// index of the installed catalog once into a [`CatalogLocationTable`]. A
/// catalogued block then costs one binary search, at most one positioned
/// index-entry read and its payload read, where `get_traversal` walks every
/// segment witness and verifies a whole index per verified-index cache miss.
/// Staged and cached payloads are reused as by `get_traversal`, and nothing
/// is admitted to or reordered in the payload, verified-index or witness
/// caches. A block the table does not serve (not catalogued when the reader
/// was built, moved since, or a located read that failed) is read from its
/// loose file, then through the exact `get_uncached` path, which alone may
/// report it missing.
pub(crate) struct TraversalReader<'a> {
    blocks: &'a BlockStore,
    locations: Option<(&'a PayloadSegmentStore, Arc<CatalogLocationTable>)>,
}

impl TraversalReader<'_> {
    pub(crate) fn get(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let blocks = self.blocks;
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOCK_GET);
        if let Some(payload) = blocks.pending_payload(id)? {
            return Ok((*payload).clone());
        }
        let cached = blocks
            .cache
            .read()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .peek_shared(id);
        if let Some(payload) = cached {
            return Ok((*payload).clone());
        }
        let Some((store, locations)) = self.locations.as_ref() else {
            return blocks.get_uncached_recorded(id);
        };
        for (_, reference, position) in locations.candidates(id) {
            // Any verified copy is the block; a failed located read only
            // loses this fast path.
            if let Ok(Some(payload)) = store.read_located(reference, position, id) {
                return Ok(payload);
            }
        }
        // Blocks written since the catalog was captured are mostly loose;
        // reading them directly skips the catalog-wide candidate walk.
        let path = blocks.path(id);
        if matches!(blocks.vfs.metadata(&path), Ok(FileKind::File)) {
            if let Ok(payload) = blocks.read_loose_block(id, &path) {
                return Ok(payload);
            }
        }
        blocks.get_uncached_recorded(id)
    }

    /// (blocks the location table holds, catalogued segments it left out,
    /// resident table bytes); all zero without a catalog.
    pub(crate) fn location_table_shape(&self) -> (usize, usize, usize) {
        self.locations.as_ref().map_or((0, 0, 0), |(_, locations)| {
            (locations.located_blocks(), locations.unindexed_segments(), locations.resident_bytes())
        })
    }

    #[cfg(test)]
    pub(crate) fn location_table_mut_for_test(&mut self) -> Option<&mut CatalogLocationTable> {
        // A private copy: the published table stays intact.
        self.locations.as_mut().map(|(_, locations)| Arc::make_mut(locations))
    }
}

/// The published catalog location table and which of its segments the
/// installed catalog still names.
#[derive(Clone)]
struct PublishedCatalogLocations {
    /// Catalog publication the table was built or derived for. Only while it
    /// is the installed publication may the table prove a block absent.
    publication: u64,
    table: Arc<CatalogLocationTable>,
    /// Per table ordinal, whether the catalog of `live_publication` names the
    /// segment. Every install recomputes it with its swap, so a table a later
    /// catalog made stale still locates the blocks of the segments it kept.
    live_publication: u64,
    live_segments: Arc<[bool]>,
}

/// What the published catalog location table says about one block.
enum PublishedCatalogAnswer<T> {
    /// A segment the installed catalog names holds the block.
    Held(T),
    /// No segment of the installed catalog, this publication, holds it.
    Absent(u64),
    /// No usable table, or it cannot decide: the exact catalog path does.
    Unknown,
}

#[cfg(test)]
thread_local! {
    static CATALOG_LOCATIONS_DISABLED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Makes this thread skip catalog location tables, a traversal reader's own
/// and the published one, so a specification can compare a pass against the
/// exact catalog path alone.
#[cfg(test)]
pub(crate) fn set_catalog_locations_disabled_for_test(disabled: bool) {
    CATALOG_LOCATIONS_DISABLED.with(|flag| flag.set(disabled));
}

#[cfg(test)]
fn catalog_locations_disabled() -> bool {
    CATALOG_LOCATIONS_DISABLED.with(std::cell::Cell::get)
}

#[cfg(not(test))]
fn catalog_locations_disabled() -> bool {
    false
}

/// Online-GC relink fence (docs/TOFUDB_ONLINE_GC.md rule S7). Every
/// reference-without-write decision (dedupe probe hit, payload-cache hit,
/// idempotent loose republish) records its block ID here between an epoch's
/// mark snapshot and its last deletion hold, so the online collector never
/// deletes a block that a commit silently re-referenced after the mark.
/// All such decisions serialise under the authority mutex; the fence is
/// in-memory only and never survives a process restart (a restarted daemon
/// starts a fresh epoch that confirms nothing until its second mark).
/// The set is exact and uncapped: it holds each block re-referenced inside
/// one open epoch window once, and is dropped when the window closes.
#[derive(Default)]
struct RelinkFence {
    /// `Some` from an epoch's capture hold until its last deletion hold.
    observed: Option<HashSet<BlockId>>,
}

impl RelinkFence {
    fn note(&mut self, id: BlockId) {
        if let Some(observed) = self.observed.as_mut() {
            observed.insert(id);
        }
    }

    fn observed(&self) -> io::Result<&HashSet<BlockId>> {
        self.observed.as_ref().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::WouldBlock,
                "no online-GC relink observation is open; defer to the next epoch",
            )
        })
    }
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
            catalog_locations: RwLock::new(None),
            retired_catalog_locations: Mutex::new(None),
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
            publication_windows: Mutex::new(PublicationWindows::default()),
            unpublished_shards: [const { AtomicBool::new(false) }; 256],
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
            catalog_locations: RwLock::new(None),
            retired_catalog_locations: Mutex::new(None),
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
            publication_windows: Mutex::new(PublicationWindows::default()),
            unpublished_shards: [const { AtomicBool::new(false) }; 256],
        })
    }

    fn path(&self, id: BlockId) -> PathBuf {
        let hexadecimal = id.to_hex();
        self.root
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"))
    }

    fn shard_directory(&self, id: BlockId) -> PathBuf {
        self.root.join(&id.to_hex()[..2])
    }

    fn ensure_shard(&self, id: BlockId) -> io::Result<PathBuf> {
        let shard = self.shard_directory(id);
        let unpublished = &self.unpublished_shards[usize::from(id.0[0])];
        match self.vfs.create_dir(&shard) {
            Ok(()) => unpublished.store(true, Ordering::Release),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if self.vfs.metadata(&shard)? != FileKind::Directory {
                    return Err(invalid_data("block shard is not a real directory"));
                }
                if !unpublished.load(Ordering::Acquire) {
                    return Ok(shard);
                }
            }
            Err(error) => return Err(error),
        }
        // Only a failed barrier leaves the mark, so an existing shard keeps
        // one history-independent operation sequence.
        sync_directory_barrier(self.vfs.as_ref(), &self.root)?;
        unpublished.store(false, Ordering::Release);
        Ok(shard)
    }

    fn note_relink(&self, id: BlockId) {
        let mut fence = self
            .relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        fence.note(id);
    }

    /// Online-GC epoch boundary (docs/TOFUDB_ONLINE_GC.md rule S7): opens an
    /// empty fence under the epoch's mark-snapshot hold so it accumulates
    /// exactly the re-references decided after that snapshot.
    pub(crate) fn begin_relink_observation(&self) {
        self.relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .observed = Some(HashSet::new());
    }

    /// Closes the epoch window after its last deletion hold and releases
    /// the fence. Nothing is recorded until the next capture opens an empty
    /// one, which is all any later hold could consult.
    pub(crate) fn end_relink_observation(&self) {
        self.relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .observed = None;
    }

    /// Opens one online-GC publication window (rule S9) seeded with the
    /// blocks it already owns; the returned token closes it.
    pub(crate) fn open_publication_window(&self, seed: impl IntoIterator<Item = BlockId>) -> u64 {
        let mut windows = self
            .publication_windows
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let window = windows.next;
        windows.next += 1;
        windows.open.insert(window, seed.into_iter().collect());
        window
    }

    pub(crate) fn close_publication_window(&self, window: u64) {
        self.publication_windows
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .open
            .remove(&window);
    }

    /// Every block an open publication window holds, for one online-GC
    /// capture under the authority hold.
    pub(crate) fn publication_window_block_ids(&self) -> Vec<BlockId> {
        let windows = self
            .publication_windows
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let mut ids = windows.open.values().flatten().copied().collect::<Vec<_>>();
        ids.sort_unstable();
        ids.dedup();
        ids
    }

    fn note_publication_window_write(&self, id: BlockId) {
        let mut windows = self
            .publication_windows
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        for ids in windows.open.values_mut() {
            ids.insert(id);
        }
    }

    /// Online-GC deletion-hold filter: drops every ID re-referenced since the
    /// open window's capture. `WouldBlock` when no window is open.
    pub(crate) fn retain_unrelinked(&self, block_ids: &mut Vec<BlockId>) -> io::Result<()> {
        let fence = self
            .relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let observed = fence.observed()?;
        block_ids.retain(|id| !observed.contains(id));
        Ok(())
    }

    /// Whether any of `block_ids` was re-referenced since the open window's
    /// capture. `WouldBlock` when no window is open.
    pub(crate) fn any_relinked(&self, block_ids: impl IntoIterator<Item = BlockId>) -> io::Result<bool> {
        let fence = self
            .relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let observed = fence.observed()?;
        Ok(block_ids.into_iter().any(|id| observed.contains(&id)))
    }

    /// Entries of the open relink window; `None` while no window is open.
    #[cfg(test)]
    pub(crate) fn relink_fence_len_for_test(&self) -> Option<usize> {
        self.relink_fence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .observed
            .as_ref()
            .map(HashSet::len)
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

    /// Whether this store already holds `id` as a published loose file or a
    /// catalogued segment entry, decided without reading the payload. Loose
    /// publication renames a synced temporary file, so a present file is
    /// complete. Like an idempotent `put`, a positive answer is a
    /// reference-without-write decision and records into the relink fence.
    pub(crate) fn retain_held_block(&self, id: BlockId) -> io::Result<bool> {
        let held = match self.vfs.metadata(&self.path(id)) {
            Ok(FileKind::File) => true,
            Ok(FileKind::Directory) => return Err(invalid_data("block path is a directory")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                self.has_catalogued_payload_block(id)?
            }
            Err(error) => return Err(error),
        };
        if held {
            self.note_relink(id);
        }
        Ok(held)
    }

    /// Publishes a loose copy even when the same content is present in a
    /// segment. CONTROL catalog roots use this to avoid a lookup bootstrap
    /// cycle during reopen.
    pub(crate) fn put_loose(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        // Before the file can appear, so no scan lists an unregistered one.
        self.note_publication_window_write(id);
        let destination = self.path(id);
        match self.vfs.metadata(&destination) {
            // An online-GC hold may unlink a redundant copy between the stat
            // and the open of a writer off the mutex: publish it again.
            Ok(FileKind::File) => match self.read_loose_block(id, &destination) {
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                existing => {
                    if existing? != payload {
                        return Err(invalid_data("content-addressed block collision"));
                    }
                    self.pending
                        .lock()
                        .map_err(|_| io::Error::other("pending block set poisoned"))?
                        .remove(id);
                    self.note_relink(id);
                    return Ok(id);
                }
            },
            Ok(FileKind::Directory) => return Err(invalid_data("block path is a directory")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let shard = self.ensure_shard(id)?;
        let temporary = new_temporary_block_path(&shard);
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
        let mut lookup = CarriedRecoveryLookup {
            locations: ids.into_iter().map(|id| (id, (None, false))).collect(),
            complete: true,
            history: None,
        };
        let (manifest, publication) = self.payload_catalog_snapshot()?;
        if let Some(manifest) = manifest {
            let mut verified_routes = crate::payload_routing::PayloadRouting::default();
            let mut filter = self.scan_packed_filter_until(&manifest, Some(&mut lookup), Some(&mut verified_routes), should_stop);
            if should_stop() { return Err(io::Error::new(io::ErrorKind::Interrupted, "recovery catalog scan cancelled")); }
            filter.covered_catalog_publication = publication;
            let store = self.payload_segments.as_ref();
            let routing = crate::payload_routing::PayloadRouting::build(&manifest, store, &verified_routes,
                &Default::default(), store.map_or(0, |store| store.routing_budget_bytes()), false);
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
        self.prepare_history_recovery_until(&mut lookup, should_stop)?;
        Ok(lookup)
    }

    /// The history root is installed after packed-reference preparation.
    /// Bind this volatile lookup to that exact catalog for this exclusive open.
    pub(crate) fn prepare_history_recovery_until(
        &self, lookup: &mut CarriedRecoveryLookup, should_stop: &dyn Fn() -> bool,
    ) -> io::Result<()> {
        let catalog = self.history_payloads.read()
            .map_err(|_| io::Error::other("history payload catalog lock poisoned"))?.clone();
        lookup.history = match catalog {
            Some(catalog) => {
                let prepared = catalog.prepare_recovery_lookup(
                    self, lookup.locations.keys().copied(), should_stop)?;
                Some((catalog, prepared))
            }
            None => None,
        };
        Ok(())
    }

    pub(crate) fn restore_carried_with_lookup(
        &self, payload: &[u8], lookup: &mut CarriedRecoveryLookup,
    ) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        // This proof is scoped to the exclusive open and to content bytes,
        // including verified historical copies. Check it before consulting
        // history: that lookup can read cold routing pages and source files.
        if lookup.locations.get(&id).is_some_and(|(_, verified)| *verified) {
            return Ok(id);
        }
        match self.get_from_history_with_lookup(id, Some(lookup)) {
            Ok(Some(existing)) if existing == payload => {
                if let Some((_, verified)) = lookup.locations.get_mut(&id) { *verified = true; }
                return Ok(id);
            }
            Ok(Some(_)) => return Err(invalid_data("carried and history block differ")),
            Ok(None) => {}
            Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {}
            Err(error) => return Err(error),
        }
        let Some((reference, verified)) = lookup.locations.get_mut(&id) else {
            return self.restore_carried(payload);
        };
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
            let restored = self.restore_carried(payload)?;
            *verified = true;
            return Ok(restored);
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
        // An unselected reference or incomplete index scan retains the verified
        // fallback. A lookup never fabricates existence or skips byte checks.
        if let Some(payload) = self.cache.read()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?.peek_shared(id) {
            return Ok((*payload).clone());
        }
        let payload = self.get_uncached_with_history_lookup(id, Some(lookup))?;
        self.cache.write().map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .insert(id, &payload);
        Ok(payload)
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
            if catalog.lookup(self, id)?.is_some() { return Ok(()); }
        }
        self.stage_pending_loose_sync(id)
    }

    pub(crate) fn stage_pending_sync_for_recovery(
        &self, id: BlockId, lookup: &CarriedRecoveryLookup,
    ) -> io::Result<()> {
        let current = self.history_payloads.read()
            .map_err(|_| io::Error::other("history payload catalog lock poisoned"))?.clone();
        if let (Some(current), Some((catalog, prepared))) = (current, &lookup.history) {
            if Arc::ptr_eq(&current, catalog) {
                let mut held = false;
                if let Some(result) = prepared.visit_locations(id, &mut |_| { held = true; Ok(true) }) {
                    result?;
                    return if held { Ok(()) } else { self.stage_pending_loose_sync(id) };
                }
            }
        }
        self.stage_pending_sync(id)
    }

    fn stage_pending_loose_sync(&self, id: BlockId) -> io::Result<()> {
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

    /// Drops every ID with a staged (WAL-carried, not yet materialized) copy.
    /// Staging writes complete bytes without deciding that a durable copy
    /// exists, so the relink fence never sees it; online-GC deletion holds
    /// call this before consulting the fence, because materialization records
    /// the fence before it retires a staged copy (docs/TOFUDB_ONLINE_GC.md
    /// rule S10).
    pub(crate) fn retain_unstaged(&self, block_ids: &mut Vec<BlockId>) -> io::Result<()> {
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        block_ids.retain(|id| {
            !pending.payloads.contains_key(id) && !pending.rotation_draining.contains_key(id)
        });
        Ok(())
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
        let mut shards: BTreeMap<PathBuf, LooseShardBatch> = BTreeMap::new();
        let mut materialized = BTreeSet::new();
        for (id, payload) in payloads {
            if materialized.insert(id) {
                shards.entry(self.shard_directory(id)).or_default().payloads.push((id, payload));
            }
        }
        let mut flushed = materialized.clone();
        for id in sync_only {
            if flushed.insert(id) {
                shards.entry(self.shard_directory(id)).or_default().sync_only.push(id);
            }
        }
        self.publish_loose_shards(&shards)?;
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        // Only payloads this call actually materialized as loose files may
        // be dropped. A staged payload can also carry an fsync-only mark
        // (recovery stages both when the loose read failed); clearing it
        // just because its mark was synced would lose the only copy when an
        // incremental checkpoint step defers it to a later batch.
        pending.retire_staged(&materialized);
        pending.sync_only.retain(|id| !flushed.contains(id));
        Ok(())
    }

    /// Hash shards are independent directories, so a VFS that admits
    /// concurrent file publication runs every shard but the first on its own
    /// scoped thread while the caller takes the first; a refused spawn runs
    /// its shard on the caller instead. Every shard finishes before the first
    /// error in shard order is returned, and a worker panic resumes here.
    fn publish_loose_shards(&self, shards: &BTreeMap<PathBuf, LooseShardBatch>) -> io::Result<()> {
        if shards.len() < 2 || !self.vfs.supports_concurrent_file_publication() {
            return shards
                .iter()
                .try_for_each(|(directory, batch)| self.publish_loose_shard(directory, batch));
        }
        let component = crate::fsync_stats::current_component();
        let mut shards = shards.iter();
        let (first_directory, first_batch) = shards.next().expect("two or more shards");
        std::thread::scope(|scope| {
            let workers = shards
                .map(|(directory, batch)| {
                    std::thread::Builder::new()
                        .name("tofu-db-loose".to_owned())
                        .spawn_scoped(scope, move || {
                            crate::fsync_stats::set_current_component(component);
                            self.publish_loose_shard(directory, batch)
                        })
                        .map_err(|_| (directory, batch))
                })
                .collect::<Vec<_>>();
            let mut outcome = self.publish_loose_shard(first_directory, first_batch);
            for worker in workers {
                let result = match worker {
                    Ok(handle) => handle
                        .join()
                        .unwrap_or_else(|panic| std::panic::resume_unwind(panic)),
                    Err((directory, batch)) => self.publish_loose_shard(directory, batch),
                };
                if outcome.is_ok() {
                    outcome = result;
                }
            }
            outcome
        })
    }

    /// Materializes one shard's absent payloads, syncs its verified existing
    /// copies and fsync-only marks, then publishes the shard directory once.
    /// A copy an online-GC hold unlinks after its stat (this runs off the
    /// mutex for rotations and adoptions) takes the absent-file path; a mark
    /// it unlinks is skipped, since GC collects only blocks whose content
    /// lives on elsewhere and re-checkpointing must stay idempotent.
    fn publish_loose_shard(&self, directory: &Path, batch: &LooseShardBatch) -> io::Result<()> {
        let mut shard_ready = false;
        let mut publish_directory = false;
        for (id, payload) in &batch.payloads {
            let path = self.path(*id);
            // A filename is not a complete-copy witness. Optional
            // dedupe/recovery may have staged valid WAL bytes over a
            // damaged loose duplicate; never retire those bytes based
            // only on existence or a successful sync of that duplicate.
            let existing = match self.vfs.metadata(&path) {
                Ok(FileKind::File) => match self.read_loose_block(*id, &path) {
                    Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                    existing => Some(existing?),
                },
                Ok(FileKind::Directory) => {
                    return Err(invalid_data("block path is a directory"));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                Err(error) => return Err(error),
            };
            match existing {
                // A complete loose file is content-pinned by the atomic
                // rename, so rewriting it would be wasted IO. Its name may
                // still await the directory barrier of an interrupted batch,
                // and an older engine published names before syncing.
                Some(existing) => {
                    if existing.as_slice() != payload.as_slice() {
                        return Err(invalid_data("content-addressed block collision"));
                    }
                    self.note_relink(*id);
                    publish_directory |= self.sync_loose_file(&path)?;
                }
                None => {
                    if !shard_ready {
                        self.ensure_shard(*id)?;
                        shard_ready = true;
                    }
                    let encoded_len = self.write_loose_synced(*id, payload, directory)?;
                    self.blocks_written.fetch_add(1, Ordering::Relaxed);
                    self.bytes_written.fetch_add(encoded_len, Ordering::Relaxed);
                    publish_directory = true;
                }
            }
        }
        for id in &batch.sync_only {
            let path = self.path(*id);
            match self.vfs.metadata(&path) {
                Ok(FileKind::File) => {}
                Ok(FileKind::Directory) => {
                    return Err(invalid_data("block path is a directory"));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            }
            publish_directory |= self.sync_loose_file(&path)?;
        }
        if publish_directory {
            sync_directory_barrier(self.vfs.as_ref(), directory)?;
        }
        Ok(())
    }

    /// Writes one complete block file and syncs it on the creating handle
    /// before the rename publishes its name, so a published name never has
    /// unsynced content. The caller owns the shard directory barrier.
    fn write_loose_synced(&self, id: BlockId, payload: &[u8], shard: &Path) -> io::Result<u64> {
        let temporary = new_temporary_block_path(shard);
        let encoded = encoded_block(id, payload)?;
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
            self.vfs.rename(&temporary, &self.path(id))
        })();
        if write_result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        write_result?;
        Ok(encoded.len() as u64)
    }

    /// Syncs one complete loose file; `false` when an online-GC hold unlinked
    /// it before the open.
    fn sync_loose_file(&self, path: &Path) -> io::Result<bool> {
        let mut file = match self.vfs.open(
            path,
            OpenRequest {
                write: true,
                ..OpenRequest::default()
            },
        ) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error),
        };
        sync_all_barrier(file.as_mut())?;
        Ok(true)
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
        self.get_from_history_with_lookup(id, None)
    }

    fn get_from_history_with_lookup(
        &self, id: BlockId, lookup: Option<&CarriedRecoveryLookup>,
    ) -> io::Result<Option<Vec<u8>>> {
        let _resolve = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOCK_HISTORY_RESOLVE);
        let catalog = self.history_payloads.read().map_err(|_| io::Error::other("history payload catalog lock poisoned"))?.clone();
        let Some(catalog) = catalog else { return Ok(None); };
        let mut found = None;
        let mut copy_error = None;
        let mut visit = |location: crate::history_payload::PayloadLocation| {
            match self.read_history_location(id, location) {
                Ok(payload) => { found = Some(payload); Ok(true) }
                Err(error) if matches!(error.kind(), io::ErrorKind::NotFound | io::ErrorKind::InvalidData) => {
                    copy_error = Some(error); Ok(false)
                }
                Err(error) => Err(error),
            }
        };
        let prepared = lookup.and_then(|lookup| lookup.history.as_ref())
            .filter(|(captured, _)| Arc::ptr_eq(captured, &catalog));
        match prepared.and_then(|(_, prepared)| prepared.visit_locations(id, &mut visit)) {
            Some(result) => result?,
            None => catalog.visit_locations(self, id, &mut visit)?,
        }
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
        let file = crate::vfs::SharedReadFile::open(self.vfs.as_ref(), &self.path(source))?;
        let expected = HEADER_BYTES as u64 + u64::from(source_bytes) + FOOTER_BYTES as u64;
        if file.len()? != expected { return Err(invalid_data("history payload source size mismatch")); }
        let mut header = [0_u8; HEADER_BYTES]; read_history_source_at(&file, 0, &mut header)?;
        let mut source_id = [0_u8; 32]; read_history_source_at(&file, expected - 32, &mut source_id)?;
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
                let result = handle.read_exact_at(HEADER_BYTES as u64 + u64::from(location.offset), &mut payload);
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
        let payload = self.get_uncached_recorded(id)?;
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .insert(id, &payload);
        Ok(payload)
    }

    /// Verified read for one pass over the store (online GC marks, backup
    /// copies and segment rewrites). Staged and already-cached payloads are
    /// reused, but a cache hit does not refresh recency and a disk read is
    /// never admitted: one pass over more blocks than the cache holds would
    /// otherwise evict the whole foreground working set. Verification is
    /// identical to `get`.
    pub(crate) fn get_traversal(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOCK_GET);
        if let Some(payload) = self.pending_payload(id)? {
            return Ok((*payload).clone());
        }
        let cached = self
            .cache
            .read()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .peek_shared(id);
        if let Some(payload) = cached {
            return Ok((*payload).clone());
        }
        self.get_uncached_recorded(id)
    }

    #[cfg(test)]
    pub(crate) fn cache_contains_for_test(&self, id: BlockId) -> bool {
        self.cache.read().unwrap().peek_shared(id).is_some()
    }

    /// See [`TraversalReader`]. `check` runs before each catalogued segment
    /// index is streamed and between table sort steps; its error aborts.
    /// A stale published table is first derived to the installed catalog (one
    /// pass over the table instead of every index), and a current one is
    /// reused; otherwise the reader builds one and publishes it for
    /// foreground reads.
    pub(crate) fn traversal_reader(
        &self,
        check: impl FnMut() -> io::Result<()>,
    ) -> io::Result<TraversalReader<'_>> {
        let store = match self.payload_segments.as_ref() {
            Some(store) if !catalog_locations_disabled() => store,
            _ => return Ok(TraversalReader { blocks: self, locations: None }),
        };
        // A failed derivation leaves the build below to answer.
        let _ = self.advance_catalog_locations();
        if let Some(table) = self.current_catalog_locations() {
            if table.unindexed_segments() == 0 {
                return Ok(TraversalReader { blocks: self, locations: Some((store, table)) });
            }
        }
        let table = self.build_and_publish_catalog_locations(store, check, "traversal")?;
        Ok(TraversalReader { blocks: self, locations: table.map(|table| (store, table)) })
    }

    /// Builds and publishes the store's first catalog location table, unless
    /// one was already published; returns whether it built one. The online GC
    /// driver calls this off the authority mutex whenever an epoch is due, so
    /// the first table never waits for an epoch's capture, which defers while
    /// foreground holds contend for the mutex (docs/TOFUDB_BLOCK_READ_PATH.md,
    /// "Published catalog location table"). Later tables are derived.
    pub(crate) fn publish_first_catalog_locations(
        &self,
        check: impl FnMut() -> io::Result<()>,
    ) -> io::Result<bool> {
        let Some(store) = self.payload_segments.as_ref() else {
            return Ok(false);
        };
        if catalog_locations_disabled()
            || self.catalog_locations.read()
                .map_err(|_| io::Error::other("catalog location lock was poisoned"))?
                .is_some()
        {
            return Ok(false);
        }
        Ok(self.build_and_publish_catalog_locations(store, check, "first")?.is_some())
    }

    /// Builds the location table of the installed catalog from every segment
    /// index and publishes it; `None` without a nonempty catalog.
    fn build_and_publish_catalog_locations(
        &self,
        store: &PayloadSegmentStore,
        check: impl FnMut() -> io::Result<()>,
        source: &str,
    ) -> io::Result<Option<Arc<CatalogLocationTable>>> {
        let (catalog, publication) = self.payload_catalog_snapshot()?;
        let Some(catalog) = catalog.filter(|catalog| !catalog.segments.is_empty()) else {
            return Ok(None);
        };
        let started = std::time::Instant::now();
        let table = Arc::new(store.build_location_table(&catalog.segments, check)?);
        self.publish_catalog_locations(publication, &table, source, started);
        Ok(Some(table))
    }

    #[cfg(test)]
    pub(crate) fn published_catalog_locations_for_test(&self) -> Option<(u64, Arc<CatalogLocationTable>)> {
        self.catalog_locations.read().unwrap().as_ref()
            .map(|published| (published.publication, Arc::clone(&published.table)))
    }

    #[cfg(test)]
    pub(crate) fn installed_catalog_publication_for_test(&self) -> u64 {
        self.installed_payload_catalog_publication.load(Ordering::Acquire)
    }

    #[cfg(test)]
    pub(crate) fn published_catalog_answer_for_test(&self, id: BlockId) -> &'static str {
        match self.locate_in_published_catalog(id) {
            PublishedCatalogAnswer::Held(_) => "held",
            PublishedCatalogAnswer::Absent(_) => "absent",
            PublishedCatalogAnswer::Unknown => "unknown",
        }
    }

    /// The published location table if it describes the installed catalog.
    /// The table is an optimization: a poisoned lock only means no table.
    fn current_catalog_locations(&self) -> Option<Arc<CatalogLocationTable>> {
        let published = self.catalog_locations.read().ok()?.clone()?;
        let installed = self.installed_payload_catalog_publication.load(Ordering::Acquire);
        (published.publication == installed).then_some(published.table)
    }

    /// Publishes `table`, built or derived for catalog `publication`, unless
    /// the published table already describes the installed catalog and this
    /// one does not. Publication numbers order installs only per publisher,
    /// so currency, not recency, decides. A poisoned lock publishes nothing.
    fn publish_catalog_locations(
        &self,
        publication: u64,
        table: &Arc<CatalogLocationTable>,
        source: &str,
        started: std::time::Instant,
    ) {
        if publication == 0 {
            return;
        }
        let Ok(mut published) = self.catalog_locations.write() else {
            return;
        };
        let installed = self.installed_payload_catalog_publication.load(Ordering::Acquire);
        if publication != installed
            && published.as_ref().is_some_and(|current| current.publication == installed)
        {
            return;
        }
        let replaced = published.replace(PublishedCatalogLocations {
            publication,
            table: Arc::clone(table),
            live_publication: publication,
            live_segments: vec![true; table.segments().len()].into(),
        });
        drop(published);
        if let Some(replaced) = replaced {
            let previous_spare = self.retired_catalog_locations.lock().ok()
                .and_then(|mut retired| retired.replace(replaced.table));
            // Released outside the lock: it may be the last handle.
            drop(previous_spare);
        }
        crate::engine_log::info("catalog_locations", &format!(
            "step=published source={source} publication={publication} installed={installed} \
             located_blocks={} unindexed_segments={} resident_bytes={} elapsed_ms={}",
            table.located_blocks(), table.unindexed_segments(), table.resident_bytes(),
            started.elapsed().as_millis(),
        ));
    }

    /// Whether the published table is a catalog behind, so that
    /// `advance_catalog_locations` has a derivation to do.
    pub(crate) fn catalog_location_derivation_due(&self) -> bool {
        let installed = self.installed_payload_catalog_publication.load(Ordering::Acquire);
        self.payload_segments.is_some()
            && installed != 0
            && self.catalog_locations.read().ok().is_some_and(|published| {
                published.as_ref().is_some_and(|published| published.publication != installed)
            })
    }

    /// Derives the published location table of the installed catalog from
    /// the table of an earlier publication: one sequential pass over the old
    /// table plus the indexes of segments it does not hold. The maintenance
    /// worker calls this off the authority mutex; returns whether it derived.
    /// Without a published table there is nothing to derive from; the first
    /// whole-store pass publishes the table it builds (`traversal_reader`).
    pub(crate) fn advance_catalog_locations(&self) -> io::Result<bool> {
        let Some(store) = self.payload_segments.as_ref() else {
            return Ok(false);
        };
        let published = self.catalog_locations.read()
            .map_err(|_| io::Error::other("catalog location lock was poisoned"))?
            .clone();
        let Some(PublishedCatalogLocations { publication, table: previous, .. }) = published else {
            return Ok(false);
        };
        let installed = self.installed_payload_catalog_publication.load(Ordering::Acquire);
        if installed == 0 || installed == publication {
            return Ok(false);
        }
        let (manifest, captured) = self.payload_catalog_snapshot()?;
        if captured == 0 {
            return Ok(false);
        }
        let started = std::time::Instant::now();
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CATALOG_LOCATION_DERIVE);
        let catalog = manifest.as_ref().map_or(&[][..], |manifest| manifest.segments.as_slice());
        // A spare a reader still holds is left to that reader, which frees
        // it; this derivation then allocates.
        let spare = self.retired_catalog_locations.lock().ok()
            .and_then(|mut retired| retired.take())
            .and_then(|table| Arc::try_unwrap(table).ok());
        let derived = Arc::new(store.derive_location_table(&previous, catalog, spare)?);
        drop(previous);
        self.publish_catalog_locations(captured, &derived, "derive", started);
        Ok(true)
    }

    /// Resolves `id` through the published table. A located payload passes
    /// CRC32C and its content hash.
    fn locate_in_published_catalog(&self, id: BlockId) -> PublishedCatalogAnswer<Vec<u8>> {
        self.resolve_in_published_catalog(id, |store, reference, position| {
            store.read_located(reference, position, id)
        })
    }

    /// One block lookup through the published table. `probe` answers one
    /// candidate: `Ok(Some)` when it holds the block, `Ok(None)` when its
    /// position names another block, and an error is no verdict. Any
    /// published table may locate a block, through the segments the installed
    /// catalog still names (`live_segments`); `Absent` needs the table of the
    /// installed publication, every catalogued segment indexed, every
    /// candidate answered and no install during the lookup. Anything else
    /// leaves the verdict to the exact catalog path.
    fn resolve_in_published_catalog<T>(
        &self,
        id: BlockId,
        mut probe: impl FnMut(&PayloadSegmentStore, &PayloadSegmentReference, u32) -> io::Result<Option<T>>,
    ) -> PublishedCatalogAnswer<T> {
        use crate::fsync_stats::{
            note_stage_elapsed, STAGE_CATALOG_LOCATION_ABSENT, STAGE_CATALOG_LOCATION_HIT,
            STAGE_CATALOG_LOCATION_UNKNOWN,
        };
        let Some(store) = self.payload_segments.as_ref() else {
            return PublishedCatalogAnswer::Unknown;
        };
        if catalog_locations_disabled() {
            return PublishedCatalogAnswer::Unknown;
        }
        let started = std::time::Instant::now();
        let unknown = || {
            note_stage_elapsed(STAGE_CATALOG_LOCATION_UNKNOWN, started.elapsed().as_nanos() as u64);
            PublishedCatalogAnswer::Unknown
        };
        let installed = || self.installed_payload_catalog_publication.load(Ordering::Acquire);
        let Some(published) = self.catalog_locations.read().ok().and_then(|published| published.clone()) else {
            return unknown();
        };
        let current = published.publication == installed();
        if !current && published.live_publication != installed() {
            return unknown();
        }
        let mut undecided = !current || published.table.unindexed_segments() > 0;
        for (ordinal, reference, position) in published.table.candidates(id) {
            if !current && !published.live_segments[ordinal] {
                continue;
            }
            match probe(store, reference, position) {
                Ok(Some(held)) => {
                    note_stage_elapsed(STAGE_CATALOG_LOCATION_HIT, started.elapsed().as_nanos() as u64);
                    return PublishedCatalogAnswer::Held(held);
                }
                Ok(None) => {}
                Err(_) => undecided = true,
            }
        }
        if undecided || published.publication != installed() {
            return unknown();
        }
        note_stage_elapsed(STAGE_CATALOG_LOCATION_ABSENT, started.elapsed().as_nanos() as u64);
        PublishedCatalogAnswer::Absent(published.publication)
    }

    fn get_uncached_recorded(&self, id: BlockId) -> io::Result<Vec<u8>> {
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
        uncached
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
        self.get_uncached_with_history_lookup(id, None)
    }

    fn get_uncached_with_history_lookup(
        &self, id: BlockId, lookup: Option<&CarriedRecoveryLookup>,
    ) -> io::Result<Vec<u8>> {
        match self.get_from_history_with_lookup(id, lookup) {
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
        match self.locate_in_published_catalog(id) {
            PublishedCatalogAnswer::Held(payload) => return Ok(payload),
            PublishedCatalogAnswer::Absent(publication) => {
                return self.get_loose_after_catalog(id, Some(publication));
            }
            PublishedCatalogAnswer::Unknown => {}
        }
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
        let excluded_at = self.packed_filter_excludes(id)?;
        let candidates = if excluded_at.is_none() {
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
        self.get_loose_after_catalog(id, excluded_at)
    }

    /// The loose copy of a block the packed path did not resolve, given the
    /// publication whose catalog the packed filter proved the block absent
    /// from (`excluded_at`), if it did. Online-GC holds unlink loose copies
    /// of catalogued blocks (a published compaction's copies, and any copy
    /// whose segment the installed catalog names), and this reader holds no
    /// lock, so a copy can vanish before the stat or between the stat and
    /// the open. Either loose miss consults the installed catalog once more,
    /// unless the filter excluded the block under the publication that is
    /// still installed: then no catalogued segment holds it, so the miss is
    /// final. That proof keeps cold misses off the catalog.
    fn get_loose_after_catalog(&self, id: BlockId, excluded_at: Option<u64>) -> io::Result<Vec<u8>> {
        let missing = || io::Error::new(io::ErrorKind::NotFound, format!("content block {} is missing", id.to_hex()));
        let path = self.path(id);
        match self.vfs.metadata(&path) {
            Ok(FileKind::File) => match self.read_loose_block(id, &path) {
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                read => return read,
            },
            Ok(FileKind::Directory) => return Err(invalid_data("block is not a regular file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let catalogued = match excluded_at {
            Some(publication) => {
                publication != self.installed_payload_catalog_publication.load(Ordering::Acquire)
                    && self.packed_filter_may_contain(id)?
            }
            None => true,
        };
        if catalogued {
            self.get_from_payload_segments(id)?.ok_or_else(missing)
        } else {
            Err(missing())
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
        let filter_hit = self.packed_filter_definitely_contains(id)?;
        // Writers probe under the authority mutex: a filter hit is resolved
        // through the published table before any hint or catalog walk.
        if filter_hit {
            match self.locate_in_published_catalog(id) {
                PublishedCatalogAnswer::Held(payload) => return Ok(DedupeProbe::Present(payload)),
                PublishedCatalogAnswer::Absent(_) => return Ok(DedupeProbe::Missing),
                PublishedCatalogAnswer::Unknown => {}
            }
        }
        if let Some(payload) = self.get_from_read_hint(id) {
            return Ok(DedupeProbe::Present(payload));
        }
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
                *routing = crate::payload_routing::PayloadRouting::build(manifest, Some(store), &routing,
                    &Default::default(), store.routing_budget_bytes(), false);
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
        if length > maximum as u64 || length < MIN_BLOCK_FILE_BYTES {
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
        Ok(self.packed_filter_excludes(id)?.is_none())
    }

    /// The installed catalog publication whose segments the packed filter
    /// proves `id` absent from. `None` on a filter hit, for a filter that does
    /// not cover the installed catalog, and for an unbuilt one: the manifest
    /// was never installed (or its segments could not be indexed), so callers
    /// fall back to the conservative full scan.
    fn packed_filter_excludes(&self, id: BlockId) -> io::Result<Option<u64>> {
        let filter = self
            .packed_filter
            .read()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        if !filter.built || filter.may_contain(id) || !self.filter_covers_installed_catalog(&filter) {
            return Ok(None);
        }
        Ok(Some(filter.covered_catalog_publication))
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
                        filter.covered_segments.insert(reference.segment_id, *reference);
                    }
                    Err(_) => return PackedBlockFilter::empty(),
                }
            }
        }
        filter
    }

    /// Makes negative filter answers cover `manifest`, which `publication` is
    /// about to install. The first catalog of a process is scanned whole;
    /// afterwards only segments the filter does not cover yet are read,
    /// normally none, because `verify_payload_segment` covers each segment a
    /// publisher builds. No index is read under the filter lock, and every
    /// index read seeds `routes`, so the install does not read it again. An
    /// unreadable or mismatched segment leaves this publication uncovered:
    /// its lookups keep the exact routing and index probes, and the next
    /// publication retries the segment. Later growth is handled by the
    /// saturation rebuild in `note_packed`.
    fn cover_packed_filter(
        &self,
        manifest: &PayloadManifest,
        publication: u64,
        routes: &mut crate::payload_routing::PayloadRouting,
    ) -> io::Result<()> {
        let poisoned = || io::Error::other("packed block filter lock was poisoned");
        if !self.packed_filter.read().map_err(|_| poisoned())?.built {
            let mut scanned = self.scan_packed_filter(manifest, None, Some(&mut *routes));
            let mut filter = self.packed_filter.write().map_err(|_| poisoned())?;
            if !filter.built {
                scanned.covered_catalog_publication = publication;
                *filter = scanned;
                return Ok(());
            }
            // A concurrent publication built the filter first; extend it.
        }
        let uncovered: Vec<PayloadSegmentReference> = {
            let filter = self.packed_filter.read().map_err(|_| poisoned())?;
            manifest.segments.iter().filter(|reference| !filter.covers_segment(reference)).copied().collect()
        };
        if let Some(store) = self.payload_segments.as_ref() {
            let mut remaining_route_bytes = store.routing_budget_bytes();
            for reference in &uncovered {
                let Ok(segment) = store.open_segment(reference.segment_id) else { return Ok(()); };
                if validate_open_segment(&segment, reference).is_err() {
                    return Ok(());
                }
                // Optional witness, as after every verified index read.
                let _ = store.remember_segment_membership(&segment);
                routes.remember_verified(*reference, &segment, &mut remaining_route_bytes);
                self.packed_filter.write().map_err(|_| poisoned())?.cover_segment(reference, segment.block_ids());
            }
        }
        let mut filter = self.packed_filter.write().map_err(|_| poisoned())?;
        if filter.covers_catalog(manifest) {
            filter.covered_catalog_publication = publication;
        }
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
        Ok(self.catalogued_payload_segment(id)?.is_some())
    }

    /// The installed catalog's segment whose validated index holds `id`, by
    /// the same exact probe. Segments are immutable under fresh IDs, so the
    /// answer stays true for as long as a catalog names that segment.
    pub(crate) fn catalogued_payload_segment(&self, id: BlockId) -> io::Result<Option<Uuid>> {
        if !self.packed_filter_may_contain(id)? {
            return Ok(None);
        }
        match self.resolve_in_published_catalog(id, |store, reference, position| {
            Ok(store.located_entry_names(reference, position, id)?.then_some(reference.segment_id))
        }) {
            PublishedCatalogAnswer::Held(segment_id) => return Ok(Some(segment_id)),
            PublishedCatalogAnswer::Absent(_) => return Ok(None),
            PublishedCatalogAnswer::Unknown => {}
        }
        let candidates = self.payload_segment_candidates(id)?;
        if candidates.is_empty() {
            return Ok(None);
        }
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("CONTROL payload manifest has no payload segment directory")
        })?;
        for reference in store.segments_that_may_contain(&candidates, id)?.iter().rev() {
            if self
                .open_catalogued_payload_segment(reference)?
                .contains(id)
            {
                return Ok(Some(reference.segment_id));
            }
        }
        Ok(None)
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
        for reference in store.segments_that_may_contain(references, id)?.iter().rev() {
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
        let mut verified_routes = crate::payload_routing::PayloadRouting::default();
        if !defer_filter { self.cover_packed_filter(&manifest, publication, &mut verified_routes)?; }
        let store = self.payload_segments.as_ref();
        let routing = {
            let previous = self.payload_routing.read().map_err(|_| io::Error::other("payload routing lock was poisoned"))?;
            crate::payload_routing::PayloadRouting::build(&manifest, store, &previous, &verified_routes,
                store.map_or(0, |store| store.routing_budget_bytes()), !defer_filter)
        };
        let referenced_segments: HashSet<Uuid> = manifest
            .segments
            .iter()
            .map(|reference| reference.segment_id)
            .collect();
        let dropped_segments: Vec<PayloadSegmentReference>;
        let added_segments: Vec<PayloadSegmentReference>;
        // The manifest read lock also fences its routes. Readers cannot see
        // routes from another publication generation, even during retirement.
        {
            let mut current = self.payload_manifest.write().map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
            let mut routes = self.payload_routing.write().map_err(|_| io::Error::other("payload routing lock was poisoned"))?;
            let previous_segments = current.as_ref().map_or(&[][..], |previous| previous.segments.as_slice());
            dropped_segments = previous_segments
                .iter()
                .filter(|reference| !referenced_segments.contains(&reference.segment_id))
                .copied()
                .collect();
            let previously_referenced: HashSet<Uuid> = previous_segments
                .iter()
                .map(|reference| reference.segment_id)
                .collect();
            added_segments = manifest
                .segments
                .iter()
                .filter(|reference| !previously_referenced.contains(&reference.segment_id))
                .copied()
                .collect();
            // Zero is an invalidation fence: a concurrent negative probe is
            // conservative throughout the catalog/routes replacement. The
            // reserved ID is unique even if independent publishers overlap.
            self.installed_payload_catalog_publication.store(0, Ordering::Release);
            *current = Some(manifest);
            *routes = routing;
            self.installed_payload_catalog_publication.store(publication, Ordering::Release);
            // Ordered with the swap, so liveness always describes the
            // catalog it names. A poisoned lock leaves the previous liveness,
            // which no longer matches this publication and so answers nothing.
            if let Ok(mut published) = self.catalog_locations.write() {
                if let Some(published) = published.as_mut() {
                    published.live_segments = published.table.segments().iter()
                        .map(|reference| referenced_segments.contains(&reference.segment_id))
                        .collect();
                    published.live_publication = publication;
                }
            }
        }
        // Coverage bookkeeping only; a poisoned lock fails every later probe.
        if let Ok(mut filter) = self.packed_filter.write() {
            filter.retain_catalog(&referenced_segments);
        }
        // Cached payloads are content-addressed and verified, so they never go
        // stale; what a dropped segment can invalidate is the cache's use as a
        // durable-existence proof by `put` and `put_deferred` dedup. Only a
        // catalog that drops a previously referenced segment (compaction,
        // segment GC) can remove the last durable copy of a cached block, and
        // loose-block GC evicts its own IDs. Pure additions (background pack
        // publication, adoption) therefore keep the cache warm, and a drop
        // evicts only IDs that no segment added by the same catalog holds: a
        // full-catalog adoption merge re-homes every block of the segment it
        // replaces. Any uncertainty keeps the original whole-cache clear.
        if !dropped_segments.is_empty()
            && self.evict_payloads_of_dropped_segments(&dropped_segments, &added_segments).is_err()
        {
            self.cache
                .write()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?
                .clear();
        }
        if let Some(store) = self.payload_segments.as_ref() {
            store.retain_read_handles(&referenced_segments);
            // Optional witnesses: a poisoned lock only costs later index reads.
            let _ = store.retain_segment_memberships(&referenced_segments);
        }
        Ok(())
    }

    /// Evicts every cached payload of the dropped segments that is absent from
    /// all added segments. Both sides use verified indexes and exact
    /// membership; dropped files are still present because retirement parks
    /// them only after installation.
    fn evict_payloads_of_dropped_segments(
        &self,
        dropped: &[PayloadSegmentReference],
        added: &[PayloadSegmentReference],
    ) -> io::Result<()> {
        let store = self.payload_segment_store()?;
        let mut survivors = Vec::with_capacity(added.len());
        for reference in added {
            let segment = store.open_segment_cached(reference.segment_id)?;
            validate_open_segment(&segment, reference)?;
            survivors.push(segment);
        }
        let mut evicted = Vec::new();
        for reference in dropped {
            let segment = store.open_segment(reference.segment_id)?;
            validate_open_segment(&segment, reference)?;
            evicted.extend(segment.block_ids().filter(|id| {
                !survivors.iter().any(|survivor| survivor.contains(*id))
            }));
        }
        let mut cache = self
            .cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
        for id in evicted {
            cache.remove(id);
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
        // The verified index also yields the negative witness that keeps cold
        // reads of other blocks from re-reading this index.
        let _ = store.remember_segment_membership(&segment);
        // Every publisher verifies each segment it adds, so the install that
        // publishes it finds it covered and keeps negative answers current.
        // A poisoned lock only leaves the segment for that install to read.
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PACK_FILTER);
        if let Ok(mut filter) = self.packed_filter.write() {
            filter.cover_segment(reference, segment.block_ids());
        }
        drop(_timer);
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

    /// Every loose block ID, sorted. `maximum_blocks` is the fixture's own
    /// ceiling: a store holding more fails, so a test states its size.
    #[cfg(test)]
    pub(crate) fn list_block_ids(&self, maximum_blocks: usize) -> io::Result<Vec<BlockId>> {
        let mut block_ids = Vec::new();
        self.visit_block_shards(|_, shard_ids| {
            block_ids.extend(shard_ids);
            Ok(())
        })?;
        if block_ids.len() > maximum_blocks {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "loose blocks exceed the fixture ceiling",
            ));
        }
        block_ids.sort_unstable();
        if block_ids.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(invalid_data("duplicate content block path"));
        }
        Ok(block_ids)
    }

    /// Hands `visitor` each loose shard's sorted block IDs, shard by shard.
    /// Only one shard's entries are held at a time, so no store size bounds
    /// or fails the listing, and entry kinds come from the directory listing
    /// rather than a stat per file (docs/TOFUDB_ONLINE_GC.md, "Loose listing
    /// without a store-wide total").
    pub(crate) fn visit_block_shards<F>(&self, mut visitor: F) -> io::Result<()>
    where
        F: FnMut(u8, Vec<BlockId>) -> io::Result<()>,
    {
        for (shard_byte, shard) in self.block_shard_paths()? {
            let mut shard_ids = Vec::new();
            for entry in self.vfs.read_directory_entries(&shard)? {
                let name = entry
                    .path
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
                if block_id.0[0] != shard_byte {
                    return Err(invalid_data("block path does not match its identifier"));
                }
                let kind = match entry.kind {
                    Some(kind) => kind,
                    None => self.vfs.metadata(&entry.path)?,
                };
                if kind != FileKind::File {
                    return Err(invalid_data("block path does not match its identifier"));
                }
                shard_ids.push(block_id);
            }
            shard_ids.sort_unstable();
            if shard_ids.windows(2).any(|pair| pair[0] == pair[1]) {
                return Err(invalid_data("duplicate content block path"));
            }
            visitor(shard_byte, shard_ids)?;
        }
        Ok(())
    }

    pub(crate) fn plan_temporary_file(&self) -> io::Result<BlockTemporaryFilePlan> {
        let shards = self.block_shard_paths()?;
        let mut scanned_entries = 0_usize;
        let mut candidate = None;
        let mut more_candidates = false;
        for (index, (_, shard_path)) in shards.iter().enumerate() {
            // Streams every entry: the scan retains at most one match, so the
            // number of entries it passes needs no bound.
            let matches = self
                .vfs
                .match_directory_prefix_bounded(shard_path, ".new-", usize::MAX, 1)?;
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
            // Without a scan bound, more entries remain only after a match.
            if matches.has_more_entries {
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
            // Pending-only blocks never created the shard directory; syncing
            // it would fail closed on a directory that does not exist.
            let mut removed_file = false;
            for &id in &ids {
                if let LooseRemoval::Unlinked { bytes } = self.unlink_loose_noting_absent(id, &mut Vec::new())? {
                    metrics.bytes_removed = metrics
                        .bytes_removed
                        .checked_add(bytes)
                        .ok_or_else(|| invalid_data("removed block byte count overflow"))?;
                    removed_file = true;
                }
                metrics.blocks_removed = metrics
                    .blocks_removed
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("removed block count overflow"))?;
            }
            if removed_file {
                self.sync_block_shard(ids[0])?;
            }
        }
        Ok(metrics)
    }

    /// One online-GC deletion hold's unit (docs/TOFUDB_ONLINE_GC.md, "One
    /// file per deletion hold"): `remove_blocks` for a single block, without
    /// the shard barrier, which the caller runs off the authority mutex once
    /// per shard (`sync_block_shard`). A crash before that barrier can only
    /// bring back the file: garbage that later epochs confirm again, or a
    /// loose copy of a block a published segment holds. Before any error
    /// returns, `absent` holds the block when this call unlinked its file,
    /// found it missing, or failed an unlink after which the file is gone: a
    /// later fresh write can recreate exactly that name (rule S10).
    pub(crate) fn remove_block_noting_absent(
        &self,
        id: BlockId,
        absent: &mut Vec<BlockId>,
    ) -> io::Result<LooseRemoval> {
        self.pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .remove(id);
        self.unlink_loose_noting_absent(id, absent)
    }

    /// Makes the unlinks in `id`'s shard directory durable.
    pub(crate) fn sync_block_shard(&self, id: BlockId) -> io::Result<()> {
        sync_directory_barrier(self.vfs.as_ref(), &self.root.join(&id.to_hex()[..2]))
    }

    fn unlink_loose_noting_absent(&self, id: BlockId, absent: &mut Vec<BlockId>) -> io::Result<LooseRemoval> {
        let path = self.path(id);
        // A block that was only ever staged (pending) has no loose file; the
        // caller's removal of its in-memory copy already retired it, so a
        // missing file is not an error.
        let removal = match self.vfs.open(
            &path,
            OpenRequest {
                read: true,
                ..OpenRequest::default()
            },
        ) {
            Ok(file) => {
                let bytes = file.len()?;
                if let Err(error) = self.vfs.remove_file(&path) {
                    if !matches!(self.vfs.metadata(&path), Ok(FileKind::File)) {
                        absent.push(id);
                    }
                    return Err(error);
                }
                absent.push(id);
                LooseRemoval::Unlinked { bytes }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                absent.push(id);
                LooseRemoval::Missing
            }
            Err(error) => return Err(error),
        };
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .remove(id);
        Ok(removal)
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
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| invalid_data("temporary block filename is malformed"))?;
    // Exactly the names engine writers leave in a shard: a loose-block
    // writer's staging file, or an at-rest re-encryption rename's.
    if !is_hyphenated_temporary_name(name, TEMPORARY_BLOCK_PREFIX)
        && !crate::at_rest::is_reencrypt_temporary_name(name)
    {
        return Err(invalid_data("temporary block filename is malformed"));
    }
    Ok(())
}

/// Loose-block writers stage each file under this prefix in its shard, sync
/// it and rename it into place.
const TEMPORARY_BLOCK_PREFIX: &str = ".new-";

fn new_temporary_block_path(shard: &Path) -> PathBuf {
    shard.join(temporary_block_file_name(Uuid::new_v4()))
}

/// The staging name of a loose-block write; collectors accept exactly this
/// spelling (`validate_temporary_block_path`).
pub(crate) fn temporary_block_file_name(identifier: Uuid) -> String {
    hyphenated_temporary_name(TEMPORARY_BLOCK_PREFIX, identifier)
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
#[path = "block_loose_batch_tests.rs"]
mod loose_batch_tests;

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
        let crate::vfs::SharedReadFile::Positioned(reader) = &original.file else { panic!("plain Unix source must use positioned reads") };
        let (entered, receive) = std::sync::mpsc::channel();
        let release = Arc::new((Mutex::new(false), Condvar::new()));
        // Ordinary open checked framing. Pause only the real read operation;
        // cache/source witnesses and per-payload content checks still run.
        let handle = Arc::new(HistorySourceHandle { source_bytes: source.len() as u32,
            file: crate::vfs::SharedReadFile::Positioned(Arc::new(PausingHistoryReader {
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

    const ENTRY: usize = BLOCK_PAYLOAD_CACHE_ENTRY_OVERHEAD_BYTES;

    fn numbered_id(value: u64) -> BlockId {
        let mut id = [0_u8; 32];
        id[..8].copy_from_slice(&value.to_le_bytes());
        BlockId(id)
    }

    #[test]
    fn verified_payload_cache_hits_keep_metadata_constant_and_preserve_lru() {
        let first = BlockId::for_payload(b"aaaa");
        let second = BlockId::for_payload(b"bbbb");
        let third = BlockId::for_payload(b"cccc");
        let mut cache = BlockPayloadCache::new(2 * (4 + ENTRY));
        cache.insert(first, b"aaaa");
        cache.insert(second, b"bbbb");
        for _ in 0..100_000 {
            assert_eq!(cache.get(first).unwrap().as_slice(), b"aaaa");
        }
        assert_eq!(cache.slots.len(), 2, "hits never allocate recency metadata");
        assert_eq!(cache.oldest_first(), vec![second, first]);
        cache.insert(third, b"cccc");
        assert!(cache.get(second).is_none());
        assert_eq!(cache.get(first).unwrap().as_slice(), b"aaaa");
        cache.set_capacity_bytes(4 + ENTRY);
        assert!(cache.get(third).is_none());
        assert_eq!(cache.get(first).unwrap().as_slice(), b"aaaa");
        cache.remove(first);
        assert!(cache.get(first).is_none());
        assert_eq!(cache.resident_bytes, 0);
        assert_eq!(cache.len(), 0);
    }

    #[test]
    fn payload_cache_metadata_is_bounded_even_without_payload_bytes() {
        // Cache policy is independent of content verification, which belongs
        // to the caller. Zero-byte entries are bounded by the per-entry charge.
        let mut cache = BlockPayloadCache::new(100 * ENTRY);
        for value in 0..1_000 {
            cache.insert_shared(numbered_id(value), Arc::new(Vec::new()));
        }
        assert_eq!(cache.len(), 100);
        assert_eq!(cache.resident_bytes, 100 * ENTRY);
        assert_eq!(cache.slots.len(), 100, "evicted slots are reused, not appended");
        assert!(cache.get(numbered_id(899)).is_none());
        assert!(cache.get(numbered_id(900)).is_some());
        cache.set_capacity_bytes(0);
        assert_eq!(cache.index.capacity(), 0);
        assert_eq!(cache.slots.capacity(), 0);
        assert_eq!(cache.free_slots.capacity(), 0);
        cache.insert(BlockId([7; 32]), b"");
        cache.insert_shared(BlockId([8; 32]), Arc::new(Vec::new()));
        assert_eq!(cache.len(), 0);
        assert!(cache.slots.is_empty());
    }

    #[test]
    fn shared_payload_cache_charges_capacity_and_releases_evicted_handles() {
        let mut payload = Vec::with_capacity(1024);
        payload.extend_from_slice(b"short");
        let id = BlockId::for_payload(&payload);
        let payload = Arc::new(payload);
        let mut cache = BlockPayloadCache::new(1024);
        cache.insert_shared(id, Arc::clone(&payload));
        assert!(cache.get(id).is_none(), "allocated capacity plus overhead exceeds the budget");
        assert_eq!(Arc::strong_count(&payload), 1);
        cache.set_capacity_bytes(payload.capacity() + ENTRY);
        cache.insert_shared(id, Arc::clone(&payload));
        assert_eq!(cache.resident_bytes, payload.capacity() + ENTRY);
        assert_eq!(Arc::strong_count(&payload), 2);
        cache.insert(BlockId::for_payload(b"x"), b"x");
        assert!(cache.get(id).is_none());
        assert_eq!(Arc::strong_count(&payload), 1);
        assert_eq!(cache.resident_bytes, 1 + ENTRY);
    }

    #[test]
    fn byte_budget_admits_small_blocks_beyond_the_former_count_cap() {
        // 2026-10-27: live loose blocks were p50 2,120 B, and the former
        // 65,536-entry cap held ~1-2% of an 11.5 GB budget.
        let blocks = 200_000_u64;
        let mut cache = BlockPayloadCache::new(blocks as usize * (64 + ENTRY));
        for value in 0..blocks {
            cache.insert_shared(numbered_id(value), Arc::new(vec![0_u8; 64]));
        }
        assert_eq!(cache.len(), blocks as usize);
        assert!(cache.get(numbered_id(0)).is_some(), "the oldest small block is still resident");
        cache.insert_shared(numbered_id(blocks), Arc::new(vec![0_u8; 64]));
        assert!(cache.get(numbered_id(1)).is_none(), "the byte budget evicts the least recent block");
        assert_eq!(cache.resident_bytes, blocks as usize * (64 + ENTRY));
    }

    #[test]
    fn payload_cache_matches_a_reference_lru_under_mixed_operations() {
        let mut seed = 0x2545_f491_4f6c_dd1d_u64;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let capacity = 6 * (48 + ENTRY);
        let mut cache = BlockPayloadCache::new(capacity);
        // Reference: oldest-first order with byte accounting.
        let mut model: Vec<(BlockId, usize)> = Vec::new();
        let charge = |length: usize| length + ENTRY;
        for _ in 0..20_000 {
            let id = numbered_id(next() % 24);
            match next() % 5 {
                0 | 1 => {
                    let length = (next() % 96) as usize;
                    cache.insert_shared(id, Arc::new(vec![1_u8; length]));
                    if charge(length) <= capacity {
                        model.retain(|(existing, _)| *existing != id);
                        model.push((id, length));
                        while model.iter().map(|(_, bytes)| charge(*bytes)).sum::<usize>() > capacity {
                            model.remove(0);
                        }
                    }
                }
                2 => {
                    let hit = cache.get(id).map(|payload| payload.len());
                    let position = model.iter().position(|(existing, _)| *existing == id);
                    assert_eq!(hit, position.map(|index| model[index].1));
                    if let Some(index) = position {
                        let entry = model.remove(index);
                        model.push(entry);
                    }
                }
                3 => {
                    let peeked = cache.peek_shared(id).map(|payload| payload.len());
                    assert_eq!(peeked, model.iter().find(|(existing, _)| *existing == id).map(|(_, bytes)| *bytes));
                }
                _ => {
                    cache.remove(id);
                    model.retain(|(existing, _)| *existing != id);
                }
            }
            assert_eq!(cache.oldest_first(), model.iter().map(|(id, _)| *id).collect::<Vec<_>>());
            assert_eq!(cache.resident_bytes, model.iter().map(|(_, bytes)| charge(*bytes)).sum::<usize>());
        }
    }

    #[test]
    fn catalog_addition_keeps_cached_payloads_but_dropping_a_segment_clears_them() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let segments = store.payload_segment_store().unwrap();
        let mut references = Vec::new();
        let mut ids = Vec::new();
        for index in 0..2_u8 {
            let payload = format!("catalog-cache-{index}").into_bytes();
            let id = BlockId::for_payload(&payload);
            let segment = segments
                .create(Uuid::from_bytes([index + 60; 16]), vec![id], |_| Ok(payload.clone()))
                .unwrap();
            references.push(
                PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                    .unwrap(),
            );
            ids.push(id);
        }
        store
            .install_payload_manifest(PayloadManifest::new(vec![references[0]]).unwrap())
            .unwrap();
        store.get(ids[0]).unwrap();
        let cached = |store: &BlockStore| store.cache.read().unwrap().peek_shared(ids[0]).is_some();
        assert!(cached(&store));
        store
            .install_payload_manifest(PayloadManifest::new(vec![references[0], references[1]]).unwrap())
            .unwrap();
        assert!(cached(&store), "a pure catalog addition keeps verified payloads warm");
        store
            .install_payload_manifest(PayloadManifest::new(vec![references[1]]).unwrap())
            .unwrap();
        assert!(
            !cached(&store),
            "a dropped segment may have held the last durable copy dedup would trust"
        );
    }

    /// A full catalog adopts each background pack by merging it into an
    /// existing segment, which the replacement catalog drops. Every block of
    /// the dropped segment lives on in the merged one, so clearing the whole
    /// payload cache on that drop (2026-10-28: once per adoption) only forced
    /// cold reads. A block whose last catalogued copy really is dropped still
    /// leaves the cache and stops proving existence to dedup.
    #[test]
    fn dropping_a_segment_evicts_only_payloads_without_a_surviving_added_copy() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let segments = store.payload_segment_store().unwrap();
        let payload = |name: &str| name.as_bytes().to_vec();
        let make = |seed: u8, names: &[&str]| {
            let payloads: std::collections::BTreeMap<BlockId, Vec<u8>> = names
                .iter()
                .map(|name| (BlockId::for_payload(name.as_bytes()), payload(name)))
                .collect();
            let segment = segments
                .create(Uuid::from_bytes([seed; 16]), payloads.keys().copied().collect(), |id| {
                    Ok(payloads[&id].clone())
                })
                .unwrap();
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                .unwrap()
        };
        let merged_away = make(81, &["re-homed", "retired"]);
        let untouched = make(82, &["untouched"]);
        let merged = make(83, &["re-homed", "incoming"]);
        store.install_payload_manifest(PayloadManifest::new(vec![merged_away, untouched]).unwrap()).unwrap();
        for name in ["re-homed", "retired", "untouched"] {
            assert_eq!(store.get(BlockId::for_payload(name.as_bytes())).unwrap(), payload(name));
        }
        store.install_payload_manifest(PayloadManifest::new(vec![merged, untouched]).unwrap()).unwrap();
        let cached = |name: &str| {
            store.cache.read().unwrap().peek_shared(BlockId::for_payload(name.as_bytes())).is_some()
        };
        assert!(cached("re-homed"), "a block the added segment holds stays warm across the drop");
        assert!(cached("untouched"), "blocks of unchanged segments stay warm");
        assert!(!cached("retired"), "a block whose last catalogued copy was dropped leaves the cache");
        assert_eq!(store.put_deferred(b"retired").unwrap(), (BlockId::for_payload(b"retired"), true),
            "dedup must restage content whose only copy was dropped");
        assert_eq!(store.put_deferred(b"re-homed").unwrap(), (BlockId::for_payload(b"re-homed"), false));
    }

    /// Builds `segments` catalogued segments whose content IDs, like every
    /// production pack/merge output, span the whole ID space, so neither the
    /// first/last range nor 16-bit prefix routing can exclude any of them.
    /// Returns one probe ID per segment, ordered by segment.
    fn full_range_catalog(store: &BlockStore, segments: usize, blocks: usize)
        -> (Vec<PayloadSegmentReference>, Vec<BlockId>)
    {
        let segment_store = store.payload_segment_store().unwrap();
        let mut references = Vec::with_capacity(segments);
        let mut probes = Vec::with_capacity(segments);
        for segment_number in 0..segments {
            let payloads: std::collections::BTreeMap<BlockId, Vec<u8>> = (0..blocks)
                .map(|block_number| {
                    let bytes = format!("catalog-{segment_number}-{block_number}").into_bytes();
                    (BlockId::for_payload(&bytes), bytes)
                })
                .collect();
            let mut segment_id = [0xc5_u8; 16];
            segment_id[..8].copy_from_slice(&(segment_number as u64).to_be_bytes());
            let segment = segment_store
                .create(Uuid::from_bytes(segment_id), payloads.keys().copied().collect(), |id| {
                    Ok(payloads[&id].clone())
                })
                .unwrap();
            references.push(
                PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                    .unwrap(),
            );
            probes.push(*payloads.keys().nth(blocks / 2).unwrap());
        }
        (references, probes)
    }

    /// Production shape on 2026-10-28: a full catalog of full-range segments
    /// and an index/filter budget that holds the membership filters of only
    /// part of it. A cold packed read verified the index of every candidate
    /// segment without a retained filter, so one miss read gigabytes of
    /// indexes. Retained negative filters for the whole installed catalog keep
    /// a cold read to its owning segment plus rare filter false positives.
    #[test]
    fn cold_packed_reads_verify_a_bounded_number_of_segment_indexes() {
        const SEGMENTS: usize = 128;
        const BLOCKS: usize = 512;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, probes) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        // 512 blocks x 16 bits = 1 KiB per filter: a 128 KiB index/filter
        // budget keeps the filters of 32 of the 128 segments under the
        // previous one-quarter membership share.
        store.set_segment_index_cache_bytes(128 * 1024).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        let reads = |vfs: &DeterministicVfs| {
            vfs.trace().unwrap().iter().filter(|operation| **operation == Operation::Read).count()
        };
        let before = reads(&vfs);
        for id in &probes {
            assert!(store.get(*id).unwrap().starts_with(b"catalog-"));
        }
        let per_cold_read = (reads(&vfs) - before) as f64 / probes.len() as f64;
        // One owning segment costs three framing reads plus the payload read.
        assert!(
            per_cold_read <= 8.0,
            "cold packed read verified {per_cold_read:.1} reads per block across {SEGMENTS} segments"
        );
    }

    /// Production shape on 2026-10-30: the routing share of the index budget
    /// indexes a few dozen of 4,091 full-range segments, and every catalog
    /// publication re-verified the index of each remaining segment only to
    /// recompute a route that still could not fit (about 5.4 GB per install).
    #[test]
    fn catalog_install_reads_only_segments_whose_route_is_unknown() {
        const SEGMENTS: usize = 48;
        const BLOCKS: usize = 512;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (mut references, probes) = full_range_catalog(&store, SEGMENTS + 1, BLOCKS);
        let added = references.pop().unwrap();
        // About 510 distinct prefixes per segment cost ~128 KiB of route budget:
        // a 2 MiB index budget routes two segments and leaves the 256-byte
        // minimum for every other one, which therefore stays fallback.
        store.set_segment_index_cache_bytes(2 * 1024 * 1024).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        let reads = |vfs: &DeterministicVfs| {
            vfs.trace().unwrap().iter().filter(|operation| **operation == Operation::Read).count()
        };
        let before = reads(&vfs);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        assert_eq!(reads(&vfs) - before, 0, "an unchanged catalog re-read segment indexes");
        references.push(added);
        let before = reads(&vfs);
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        // Header, trailer and index of the one added segment.
        assert_eq!(reads(&vfs) - before, 3, "a one-segment publication re-read other segment indexes");
        assert!(
            store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()),
            "the same index read covers the packed filter"
        );
        for id in &probes {
            assert!(store.get(*id).unwrap().starts_with(b"catalog-"));
        }
    }

    /// A negative filter answer is exact only for its own publication. Loose
    /// compaction packs a block, installs the catalog, then removes the loose
    /// file; a lookup whose negative answer predates that install and whose
    /// loose probe follows the removal must still find the packed block.
    #[test]
    fn negative_answer_rechecks_a_catalog_published_after_it() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (mut catalog, _) = full_range_catalog(&store, 4, 16);
        store.install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap()).unwrap();
        let payload = b"loose until compaction packs it".to_vec();
        let id = store.put(&payload).unwrap();
        let excluded_at = store.packed_filter_excludes(id).unwrap();
        assert!(excluded_at.is_some(), "a covering filter excludes a loose block");
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(catalog_segment_id(9), vec![id], |_| Ok(payload.clone()))
            .unwrap();
        let reference =
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        catalog.push(reference);
        store.install_payload_manifest(PayloadManifest::new(catalog).unwrap()).unwrap();
        store.remove_blocks(&[id]).unwrap();
        assert_eq!(store.get_loose_after_catalog(id, excluded_at).unwrap(), payload);
        // Under an unchanged catalog the answer stays exact and touches no segment.
        let missing = BlockId::for_payload(b"never written");
        let current = store.packed_filter_excludes(missing).unwrap();
        assert!(current.is_some());
        crate::payload_segment::read_path_counters::take();
        assert_eq!(
            store.get_loose_after_catalog(missing, current).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        assert_eq!(crate::payload_segment::read_path_counters::take().0, 0);
    }


    type RaceHook = Box<dyn FnOnce() + Send>;

    /// Runs a hook right before the next stat or open of one armed path: an
    /// online-GC hold that publishes or unlinks between two steps of a reader
    /// or writer running off the authority mutex.
    struct LooseRaceVfs {
        inner: DeterministicVfs,
        before_metadata: Mutex<Option<(PathBuf, RaceHook)>>,
        before_open: Mutex<Option<(PathBuf, RaceHook)>>,
    }

    impl LooseRaceVfs {
        fn new() -> Arc<Self> {
            let inner = DeterministicVfs::new(None);
            inner.create_dir(Path::new("/data")).unwrap();
            inner.sync_directory(Path::new("/")).unwrap();
            Arc::new(Self { inner, before_metadata: Mutex::new(None), before_open: Mutex::new(None) })
        }

        fn arm_before_metadata(&self, path: PathBuf, hook: impl FnOnce() + Send + 'static) {
            *self.before_metadata.lock().unwrap() = Some((path, Box::new(hook)));
        }

        fn arm_before_open(&self, path: PathBuf, hook: impl FnOnce() + Send + 'static) {
            *self.before_open.lock().unwrap() = Some((path, Box::new(hook)));
        }

        /// The unlink of an online-GC hold right before the next open of `path`.
        fn unlink_before_open(&self, path: PathBuf) {
            let inner = self.inner.clone();
            self.arm_before_open(path.clone(), move || inner.remove_file(&path).unwrap());
        }

        fn fire(armed: &Mutex<Option<(PathBuf, RaceHook)>>, path: &Path) {
            let hook = {
                let mut armed = armed.lock().unwrap();
                match armed.take() {
                    Some((armed_path, hook)) if armed_path == path => Some(hook),
                    other => {
                        *armed = other;
                        None
                    }
                }
            };
            if let Some(hook) = hook {
                hook();
            }
        }
    }

    impl Vfs for LooseRaceVfs {
        fn supports_buffered_wal_writes(&self) -> bool {
            self.inner.supports_buffered_wal_writes()
        }
        fn filesystem_capacity(&self, path: &Path) -> io::Result<crate::vfs::FilesystemCapacity> {
            self.inner.filesystem_capacity(path)
        }
        fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
            Self::fire(&self.before_open, path);
            self.inner.open(path, request)
        }
        fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
            self.inner.open_positioned_reader(path)
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
            Self::fire(&self.before_metadata, path);
            self.inner.metadata(path)
        }
        fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
            self.inner.read_directory(path)
        }
        fn read_directory_entries(&self, path: &Path) -> io::Result<Vec<crate::vfs::DirectoryEntry>> {
            self.inner.read_directory_entries(path)
        }
        fn read_directory_bounded(
            &self,
            path: &Path,
            maximum: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
            self.inner.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(
            &self,
            path: &Path,
            prefix: &str,
            maximum: usize,
            matches: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
            self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
        }
        fn sync_directory(&self, path: &Path) -> io::Result<()> {
            self.inner.sync_directory(path)
        }
    }

    /// A store whose every read reaches the disk paths under test.
    fn race_store() -> (Arc<LooseRaceVfs>, Arc<BlockStore>) {
        let vfs = LooseRaceVfs::new();
        let store = Arc::new(BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap());
        store.set_cache_capacity_bytes(0).unwrap();
        (vfs, store)
    }

    /// A verified, unpublished segment holding exactly `payload`.
    fn segment_holding(store: &BlockStore, segment_number: usize, payload: &[u8]) -> PayloadSegmentReference {
        let id = BlockId::for_payload(payload);
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(catalog_segment_id(segment_number), vec![id], |_| Ok(payload.to_vec()))
            .unwrap();
        let reference =
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        reference
    }

    /// Read order for a block that is both loose and packed while the catalog
    /// holding it stays installed (docs/TOFUDB_ONLINE_GC.md, "Redundant loose
    /// copies of catalogued blocks"). Under a covering filter every read path
    /// resolves the segment and never opens the loose file. Before the filter
    /// is built (a clean open) `get` tries the loose file first, and a copy
    /// unlinked between that stat and its open falls through to the same
    /// segment. No path reports the block missing.
    #[test]
    fn a_catalogued_block_reads_through_its_segment_whatever_happens_to_its_loose_copy() {
        let (vfs, store) = race_store();
        let (mut catalog, _) = full_range_catalog(&store, 4, 16);
        let payload = b"both loose and packed".to_vec();
        let id = store.put_loose(&payload).unwrap();
        let path = store.path(id);
        catalog.push(segment_holding(&store, 9, &payload));
        let manifest = PayloadManifest::new(catalog).unwrap();
        store.install_payload_manifest(manifest.clone()).unwrap();
        let loose_opened = Arc::new(AtomicBool::new(false));
        let arm_unlink = |opened: &Arc<AtomicBool>| {
            let inner = vfs.inner.clone();
            let opened = Arc::clone(opened);
            let unlinked = path.clone();
            vfs.arm_before_open(path.clone(), move || {
                opened.store(true, Ordering::SeqCst);
                inner.remove_file(&unlinked).unwrap();
            });
        };
        arm_unlink(&loose_opened);
        assert_eq!(store.get(id).unwrap(), payload);
        assert_eq!(store.get_traversal(id).unwrap(), payload);
        assert_eq!(*store.get_shared(id).unwrap(), payload);
        assert_eq!(*store.get_materialized_shared(id).unwrap(), payload);
        assert_eq!(store.traversal_reader(|| Ok(())).unwrap().get(id).unwrap(), payload);
        assert!(
            !loose_opened.load(Ordering::SeqCst),
            "a covering filter never sends a catalogued block to its loose copy"
        );

        let reopened = BlockStore::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        reopened.set_cache_capacity_bytes(0).unwrap();
        reopened.install_recovery_payload_manifest(manifest).unwrap();
        assert!(!reopened.packed_filter.read().unwrap().built, "fixture: a clean open builds no filter");
        assert_eq!(reopened.get(id).unwrap(), payload);
        assert!(loose_opened.load(Ordering::SeqCst), "an unbuilt filter tries the loose copy first");
        assert_eq!(vfs.inner.metadata(&path).unwrap_err().kind(), io::ErrorKind::NotFound);
        assert_eq!(reopened.get(id).unwrap(), payload);
        assert_eq!(reopened.get_traversal(id).unwrap(), payload);
        assert_eq!(reopened.traversal_reader(|| Ok(())).unwrap().get(id).unwrap(), payload);
    }

    /// A reader whose catalog view predates the publication that packed a
    /// block can find the block's loose copy at its stat and lose it before
    /// its open: loose compaction removes each copy right after it publishes,
    /// and the online collector removes every loose copy of a catalogued
    /// block. Such a miss consults the installed catalog like a missing file
    /// does. Before the fix the open's NotFound reached the reader.
    #[test]
    fn a_loose_copy_unlinked_between_a_readers_stat_and_open_resolves_through_the_new_catalog() {
        let (vfs, store) = race_store();
        let (mut catalog, _) = full_range_catalog(&store, 4, 16);
        store.install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap()).unwrap();
        let payload = b"loose until a publication packs it".to_vec();
        let id = store.put_loose(&payload).unwrap();
        assert!(store.packed_filter_excludes(id).unwrap().is_some(), "fixture: a covering filter excludes it");
        let path = store.path(id);
        {
            let store = Arc::clone(&store);
            let inner = vfs.inner.clone();
            let payload = payload.clone();
            let unlinked = path.clone();
            vfs.arm_before_open(path.clone(), move || {
                catalog.push(segment_holding(&store, 9, &payload));
                store.install_payload_manifest(PayloadManifest::new(catalog).unwrap()).unwrap();
                inner.remove_file(&unlinked).unwrap();
            });
        }
        assert_eq!(store.get(id).unwrap(), payload);
        assert_eq!(vfs.inner.metadata(&path).unwrap_err().kind(), io::ErrorKind::NotFound, "the copy went after the stat");
    }

    /// The same race when the filter cannot exclude the block (stale bits of
    /// retired segments, or a false positive) and no catalogued segment was a
    /// candidate, so the packed path probed nothing: a publication that packs
    /// the block and unlinks its copy before the reader's stat must still be
    /// consulted. Before the fix an empty candidate list made the miss final.
    #[test]
    fn a_loose_miss_with_no_catalog_candidate_rechecks_a_later_publication() {
        let (vfs, store) = race_store();
        let (mut catalog, _) = full_range_catalog(&store, 4, 16);
        store.install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap()).unwrap();
        let payload = b"loose behind a filter false positive".to_vec();
        let id = store.put_loose(&payload).unwrap();
        store.note_packed(&[id]).unwrap();
        assert_eq!(store.packed_filter_excludes(id).unwrap(), None, "fixture: the filter cannot exclude it");
        assert!(store.payload_segment_candidates(id).unwrap().is_empty(), "fixture: no segment is a candidate");
        let path = store.path(id);
        {
            let store = Arc::clone(&store);
            let inner = vfs.inner.clone();
            let payload = payload.clone();
            let unlinked = path.clone();
            vfs.arm_before_metadata(path.clone(), move || {
                catalog.push(segment_holding(&store, 9, &payload));
                store.install_payload_manifest(PayloadManifest::new(catalog).unwrap()).unwrap();
                inner.remove_file(&unlinked).unwrap();
            });
        }
        assert_eq!(store.get(id).unwrap(), payload);
    }

    /// Writers off the authority mutex (a rotation's encode, a pack adoption's
    /// publication, history promotion's loose fallback) stat a loose file and
    /// then open it, while an online-GC hold may unlink a redundant copy
    /// between the two. `put_loose` then publishes the file like a missing
    /// one. Before the fix the open's NotFound failed the writer.
    #[test]
    fn put_loose_publishes_again_a_copy_unlinked_after_its_stat() {
        let (vfs, store) = race_store();
        let payload = b"republished by an off-mutex writer".to_vec();
        let id = store.put_loose(&payload).unwrap();
        vfs.unlink_before_open(store.path(id));
        assert_eq!(store.put_loose(&payload).unwrap(), id);
        drop(store);
        vfs.inner.crash().unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.inner.clone())).unwrap();
        assert_eq!(reopened.get(id).unwrap(), payload);
    }

    /// A staged block's materialization takes the missing-file path when its
    /// loose copy goes between the stat and the open.
    #[test]
    fn flushing_a_staged_block_materializes_a_copy_unlinked_after_its_stat() {
        let (vfs, store) = race_store();
        let payload = b"staged over an existing loose copy".to_vec();
        let id = store.put_loose(&payload).unwrap();
        assert_eq!(store.put_deferred_fresh(&payload).unwrap(), (id, true));
        vfs.unlink_before_open(store.path(id));
        store.flush_pending_subset(&BTreeSet::from([id])).unwrap();
        assert!(store.pending_payload(id).unwrap().is_none());
        drop(store);
        vfs.inner.crash().unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.inner.clone())).unwrap();
        assert_eq!(reopened.get(id).unwrap(), payload);
    }

    /// An fsync-only mark skips a copy that goes between the stat and the
    /// open, exactly as it skips one the stat already finds missing.
    #[test]
    fn flushing_a_sync_mark_skips_a_copy_unlinked_after_its_stat() {
        let (vfs, store) = race_store();
        let id = store.put_loose(b"synced while an online-GC hold unlinks it").unwrap();
        store.stage_pending_sync(id).unwrap();
        assert!(store.pending.lock().unwrap().sync_only.contains(&id));
        vfs.unlink_before_open(store.path(id));
        store.flush_pending_subset(&BTreeSet::new()).unwrap();
        assert!(!store.pending.lock().unwrap().sync_only.contains(&id));
    }
    /// An install that cannot read a segment the filter does not cover leaves
    /// its publication uncovered: lookups keep the exact catalog path and stay
    /// correct, and the next publication reads the segment and covers again.
    #[test]
    fn unreadable_uncovered_segment_leaves_its_publication_on_exact_probes() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (mut catalog, _) = full_range_catalog(&store, 4, 16);
        store.install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap()).unwrap();
        // A segment this store never verified, like one whose coverage record
        // a saturation rebuild dropped.
        let payload = b"published while its index was unreadable".to_vec();
        let id = BlockId::for_payload(&payload);
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(catalog_segment_id(9), vec![id], |_| Ok(payload.clone()))
            .unwrap();
        catalog.push(
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                .unwrap(),
        );
        let covered = |store: &BlockStore| store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap());
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        store.install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap()).unwrap();
        assert_eq!(vfs.trace().unwrap().first(), Some(&Operation::Metadata), "the fault must hit the cover read");
        assert!(!covered(&store));
        let missing = BlockId::for_payload(b"never written");
        assert!(store.packed_filter_may_contain(missing).unwrap(), "an uncovered publication excludes nothing");
        assert_eq!(store.get(missing).unwrap_err().kind(), io::ErrorKind::NotFound);
        assert_eq!(store.get(id).unwrap(), payload);
        store.install_payload_manifest(PayloadManifest::new(catalog).unwrap()).unwrap();
        assert!(covered(&store));
        assert!(!store.packed_filter_may_contain(missing).unwrap());
        assert!(store.packed_filter_may_contain(id).unwrap());
    }

    /// Live shape on 2026-10-31: about 4,091 full-range catalogued segments and
    /// a payload catalog publication every few seconds (checkpoint packs,
    /// adoption merges, GC rewrites and drops, history promotion batches).
    /// Negative packed-filter answers covered only the first publication, so
    /// after any later one every cold read of a loose block, every lookup of a
    /// missing block and every dedupe probe of new content checked one witness
    /// per catalogued segment and verified the whole index of each witness
    /// false positive into the verified-index cache. Every publication now
    /// keeps the filter covering the catalog it installs: those lookups touch
    /// no segment, while every catalogued block still passes the filter.
    #[test]
    fn cold_loose_reads_skip_the_catalog_after_successive_publications() {
        const SEGMENTS: usize = 256;
        const BLOCKS: usize = 64;
        const LOOKUPS: usize = 32;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (mut catalog, packed_probes) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        // Full indexes of about ten segments fit; witnesses cover the catalog.
        store.set_segment_index_cache_bytes(32 * 1024).unwrap();
        let segment_store = store.payload_segment_store().unwrap();
        let create = |seed: usize, payloads: &BTreeMap<BlockId, Vec<u8>>| {
            let segment = segment_store
                .create(catalog_segment_id(seed), payloads.keys().copied().collect(), |id| {
                    Ok(payloads[&id].clone())
                })
                .unwrap();
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                .unwrap()
        };
        let fresh = |label: &str, count: usize| -> BTreeMap<BlockId, Vec<u8>> {
            (0..count)
                .map(|number| {
                    let bytes = format!("{label}-{number}").into_bytes();
                    (BlockId::for_payload(&bytes), bytes)
                })
                .collect()
        };
        let contents = |reference: &PayloadSegmentReference| -> BTreeMap<BlockId, Vec<u8>> {
            let segment = segment_store.open_segment(reference.segment_id).unwrap();
            segment.block_ids().map(|id| (id, segment.get(id).unwrap().unwrap())).collect()
        };
        let publish = |catalog: &[PayloadSegmentReference]| {
            store.install_payload_manifest(PayloadManifest::new(catalog.to_vec()).unwrap()).unwrap();
            store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap())
        };
        let mut coverage = vec![("initial", publish(&catalog))];
        // Checkpoint pack: a fresh segment verified and noted before publication.
        let checkpoint = fresh("checkpoint-pack", BLOCKS);
        catalog.push(create(SEGMENTS, &checkpoint));
        store.verify_payload_segment(catalog.last().unwrap()).unwrap();
        store.note_packed(&checkpoint.keys().copied().collect::<Vec<_>>()).unwrap();
        coverage.push(("checkpoint pack", publish(&catalog)));
        // Full-catalog adoption: the packer verified and noted its pack; the
        // merged copy that replaces the smallest segment is only verified.
        let incoming = fresh("adopted-pack", 8);
        store.verify_payload_segment(&create(SEGMENTS + 1, &incoming)).unwrap();
        store.note_packed(&incoming.keys().copied().collect::<Vec<_>>()).unwrap();
        let mut merged = contents(&catalog[0]);
        merged.extend(incoming);
        catalog[0] = create(SEGMENTS + 2, &merged);
        store.verify_payload_segment(&catalog[0]).unwrap();
        coverage.push(("adoption merge", publish(&catalog)));
        // GC rewrite: a verified subset replaces a segment.
        let mut survivors = contents(&catalog[1]);
        let dead: Vec<BlockId> = survivors.keys().copied().step_by(2).collect();
        for id in &dead {
            survivors.remove(id);
        }
        catalog[1] = create(SEGMENTS + 3, &survivors);
        store.verify_payload_segment(&catalog[1]).unwrap();
        coverage.push(("gc rewrite", publish(&catalog)));
        // GC consolidation that drops a segment without a replacement.
        catalog.remove(2);
        coverage.push(("gc drop", publish(&catalog)));
        // History promotion: a verified and noted batch published together.
        for (seed, label) in [(SEGMENTS + 4, "history-a"), (SEGMENTS + 5, "history-b")] {
            let batch = fresh(label, BLOCKS);
            catalog.push(create(seed, &batch));
            store.verify_payload_segment(catalog.last().unwrap()).unwrap();
            store.note_packed(&batch.keys().copied().collect::<Vec<_>>()).unwrap();
        }
        coverage.push(("history promotion", publish(&catalog)));

        let loose: Vec<(BlockId, Vec<u8>)> = (0..LOOKUPS)
            .map(|number| {
                let payload = format!("loose-after-publications-{number}").into_bytes();
                (store.put(&payload).unwrap(), payload)
            })
            .collect();
        // One block of each of the last original segments, which no
        // publication touched and no read hint names.
        let packed: Vec<(BlockId, Vec<u8>)> = (SEGMENTS - LOOKUPS..SEGMENTS)
            .map(|segment_number| {
                let id = packed_probes[segment_number];
                (id, catalog_layout(segment_number, BLOCKS, id).2)
            })
            .collect();
        store.cache.write().unwrap().clear();
        // [witness checks, whole indexes read, their bytes, verified-index
        // cache admissions, VFS opens, VFS reads]
        let work = |run: &mut dyn FnMut()| -> [u64; 6] {
            crate::payload_segment::read_path_counters::take();
            crate::payload_segment::read_path_counters::take_index_work();
            vfs.arm_fault(None).unwrap();
            run();
            let (witness_checks, _) = crate::payload_segment::read_path_counters::take();
            let (index_reads, index_bytes, admissions) =
                crate::payload_segment::read_path_counters::take_index_work();
            let trace = vfs.trace().unwrap();
            let count = |kind: Operation| trace.iter().filter(|operation| **operation == kind).count() as u64;
            [witness_checks, index_reads, index_bytes, admissions, count(Operation::Open), count(Operation::Read)]
        };
        let loose_work = work(&mut || {
            for (id, payload) in &loose {
                assert_eq!(&store.get(*id).unwrap(), payload);
            }
        });
        let missing_work = work(&mut || {
            for number in 0..LOOKUPS {
                let id = BlockId::for_payload(format!("never-written-{number}").as_bytes());
                assert_eq!(store.get(id).unwrap_err().kind(), io::ErrorKind::NotFound);
            }
        });
        let dedupe_work = work(&mut || {
            for number in 0..LOOKUPS {
                assert!(store.put_deferred(format!("fresh-dedupe-{number}").as_bytes()).unwrap().1);
            }
        });
        let packed_work = work(&mut || {
            for (id, payload) in &packed {
                assert_eq!(&store.get(*id).unwrap(), payload);
            }
        });
        let per_lookup = |work: [u64; 6]| {
            let lookups = LOOKUPS as f64;
            format!(
                "witness_checks={:.1} index_reads={:.2} index_bytes={:.0} index_admissions={:.2} opens={:.2} reads={:.2}",
                work[0] as f64 / lookups, work[1] as f64 / lookups, work[2] as f64 / lookups,
                work[3] as f64 / lookups, work[4] as f64 / lookups, work[5] as f64 / lookups,
            )
        };
        eprintln!(
            "cold_reads_after_publications segments={} coverage={coverage:?}\n  loose: {}\n  missing: {}\n  dedupe: {}\n  packed: {}",
            catalog.len(), per_lookup(loose_work), per_lookup(missing_work), per_lookup(dedupe_work),
            per_lookup(packed_work),
        );
        for reference in &catalog {
            for id in segment_store.open_segment(reference.segment_id).unwrap().block_ids() {
                assert!(store.packed_filter_may_contain(id).unwrap(), "a catalogued block failed the packed filter");
            }
        }
        assert!(coverage.iter().all(|(_, covered)| *covered), "a publication left the filter stale: {coverage:?}");
        for (label, work) in [("loose", loose_work), ("missing", missing_work), ("dedupe", dedupe_work)] {
            assert_eq!(work[0], 0, "{label} lookups checked segment witnesses");
            assert_eq!((work[1], work[3]), (0, 0), "{label} lookups read or admitted segment indexes");
        }
        // A loose read opens and reads only its own file; misses and dedupe
        // probes of new content open nothing.
        assert_eq!((loose_work[4], loose_work[5]), (LOOKUPS as u64, LOOKUPS as u64));
        assert_eq!((missing_work[4], missing_work[5]), (0, 0));
        assert_eq!((dedupe_work[4], dedupe_work[5]), (0, 0));
    }

    fn catalog_segment_id(segment_number: usize) -> Uuid {
        let mut segment_id = [0xc5_u8; 16];
        segment_id[..8].copy_from_slice(&(segment_number as u64).to_be_bytes());
        Uuid::from_bytes(segment_id)
    }

    /// (index position, payload offset, payload) of `id` in segment
    /// `segment_number` of a `full_range_catalog` of `blocks`-block segments.
    fn catalog_layout(segment_number: usize, blocks: usize, id: BlockId) -> (u64, u64, Vec<u8>) {
        let mut payloads: Vec<_> = (0..blocks)
            .map(|block_number| {
                let bytes = format!("catalog-{segment_number}-{block_number}").into_bytes();
                (BlockId::for_payload(&bytes), bytes)
            })
            .collect();
        payloads.sort_by_key(|(candidate, _)| *candidate);
        let mut offset = 48_u64;
        for (position, (candidate, bytes)) in payloads.into_iter().enumerate() {
            if candidate == id {
                return (position as u64, offset, bytes);
            }
            offset += bytes.len() as u64;
        }
        panic!("block is not in catalog segment {segment_number}");
    }

    fn flip_segment_byte(vfs: &DeterministicVfs, segment_id: Uuid, offset: u64) {
        let path = Path::new("/data/payload-segments").join(format!("{}.pseg", segment_id.simple()));
        let mut file = vfs
            .open(&path, OpenRequest { read: true, write: true, ..OpenRequest::default() })
            .unwrap();
        let mut byte = [0_u8; 1];
        file.read_exact_at(offset, &mut byte).unwrap();
        file.write_all_at(offset, &[byte[0] ^ 0x5a]).unwrap();
        sync_all_barrier(file.as_mut()).unwrap();
    }

    /// Live shape on 2026-10-31 (docs/TOFUDB_BLOCK_READ_PATH.md, "Catalog
    /// location table for whole-store passes"): each lookup of the exact
    /// catalog path checks one witness per catalogued segment and verifies the
    /// owning segment's whole index on every verified-index cache miss. A
    /// traversal reader reads each index once, then resolves a block with one
    /// table candidate, one index-entry read and one payload read, and admits
    /// nothing to the verified-index or payload caches.
    #[test]
    fn traversal_reader_resolves_catalogued_blocks_with_one_candidate_and_two_reads() {
        const SEGMENTS: usize = 48;
        const BLOCKS: usize = 512;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, probes) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        store.set_segment_index_cache_bytes(128 * 1024).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        let reads = |vfs: &DeterministicVfs| {
            vfs.trace().unwrap().iter().filter(|operation| **operation == Operation::Read).count()
        };
        let before = reads(&vfs);
        let reader = store.traversal_reader(|| Ok(())).unwrap();
        assert_eq!(reads(&vfs) - before, 3 * SEGMENTS, "header, trailer and index of each segment, once");
        let (located, unindexed, resident_bytes) = reader.location_table_shape();
        assert_eq!((located, unindexed), (SEGMENTS * BLOCKS, 0));
        assert!(resident_bytes >= 8 * SEGMENTS * BLOCKS);
        crate::payload_segment::read_path_counters::take();
        let before = reads(&vfs);
        for id in &probes {
            assert!(reader.get(*id).unwrap().starts_with(b"catalog-"));
        }
        let located_reads = reads(&vfs) - before;
        assert_eq!(located_reads, 2 * probes.len(), "one index entry and one payload read per block");
        assert_eq!(crate::payload_segment::read_path_counters::take(), (0, probes.len() as u64));
        assert!(store.payload_segment_store().unwrap().resident_index_ids_for_test().is_empty());
        assert!(probes.iter().all(|id| !store.cache_contains_for_test(*id)));
        drop(reader);
        set_catalog_locations_disabled_for_test(true);
        let before = reads(&vfs);
        for id in &probes {
            assert!(store.get_traversal(*id).unwrap().starts_with(b"catalog-"));
        }
        let exact_reads = reads(&vfs) - before;
        set_catalog_locations_disabled_for_test(false);
        let (witness_checks, _) = crate::payload_segment::read_path_counters::take();
        assert!(witness_checks >= (SEGMENTS * probes.len()) as u64);
        assert!(exact_reads >= 4 * probes.len(), "a whole verified index per cold block");
        println!("traversal_reader located_reads={located_reads} exact_reads={exact_reads} \
            exact_witness_checks={witness_checks} table_bytes={resident_bytes} blocks={}", probes.len());
    }

    /// Damage after a reader learned a position never yields other bytes and
    /// never hides a block: a corrupt payload or index entry ends in the exact
    /// path's typed integrity error, a misplaced table entry still resolves
    /// through the exact path, and a segment whose index fails verification
    /// while the table is built is left to the exact path, which reports it.
    #[test]
    fn traversal_reader_damage_never_yields_other_bytes_or_drops_a_block() {
        const SEGMENTS: usize = 8;
        const BLOCKS: usize = 64;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, probes) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        let index_offset = |segment_number: usize| {
            let reference = references
                .iter()
                .find(|reference| reference.segment_id == catalog_segment_id(segment_number))
                .unwrap();
            48 + reference.payload_bytes
        };
        // Byte 33 of an entry is inside its payload offset: the ID still matches.
        let (position, _, _) = catalog_layout(0, BLOCKS, probes[0]);
        flip_segment_byte(&vfs, catalog_segment_id(0), index_offset(0) + position * 48 + 33);
        let mut reader = store.traversal_reader(|| Ok(())).unwrap();
        // The reader's fallbacks below must be the exact path, not the table
        // this reader just published.
        set_catalog_locations_disabled_for_test(true);
        assert_eq!(reader.location_table_shape().1, 1, "the unverifiable index stays out of the table");
        assert_eq!(reader.get(probes[0]).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let (_, payload_offset, _) = catalog_layout(1, BLOCKS, probes[1]);
        flip_segment_byte(&vfs, catalog_segment_id(1), payload_offset);
        assert_eq!(reader.get(probes[1]).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let (position, _, _) = catalog_layout(2, BLOCKS, probes[2]);
        flip_segment_byte(&vfs, catalog_segment_id(2), index_offset(2) + position * 48 + 33);
        assert_eq!(reader.get(probes[2]).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let (_, _, expected) = catalog_layout(3, BLOCKS, probes[3]);
        assert_eq!(reader.location_table_mut_for_test().unwrap().misplace_for_test(probes[3]), 1);
        crate::payload_segment::read_path_counters::take();
        assert_eq!(reader.get(probes[3]).unwrap(), expected);
        let (witness_checks, candidates) = crate::payload_segment::read_path_counters::take();
        assert_eq!(candidates, 1);
        assert!(witness_checks > 0, "the exact path resolved the misplaced block");

        for (segment_number, id) in probes.iter().enumerate().skip(4) {
            assert_eq!(reader.get(*id).unwrap(), catalog_layout(segment_number, BLOCKS, *id).2);
        }
        set_catalog_locations_disabled_for_test(false);
    }

    /// Blocks written or re-homed after the reader's table was built resolve
    /// through the loose file or the exact catalog path; only that path
    /// reports a block missing.
    #[test]
    fn traversal_reader_follows_blocks_published_or_moved_after_its_table() {
        const BLOCKS: usize = 32;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, probes) = full_range_catalog(&store, 2, BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        let reader = store.traversal_reader(|| Ok(())).unwrap();
        let segment_store = store.payload_segment_store().unwrap();

        let loose = store.put_loose(b"written-after-the-table").unwrap();
        assert_eq!(reader.get(loose).unwrap(), b"written-after-the-table");

        let packed_payload = b"packed-after-the-table".to_vec();
        let packed = BlockId::for_payload(&packed_payload);
        let added = segment_store
            .create(catalog_segment_id(7), vec![packed], |_| Ok(packed_payload.clone()))
            .unwrap();
        let added = PayloadSegmentReference::new(added.metadata(), added.first_block_id(), added.last_block_id())
            .unwrap();
        let moved_payloads: BTreeMap<BlockId, Vec<u8>> = (0..BLOCKS)
            .map(|block_number| {
                let bytes = format!("catalog-0-{block_number}").into_bytes();
                (BlockId::for_payload(&bytes), bytes)
            })
            .collect();
        let moved = segment_store
            .create(catalog_segment_id(8), moved_payloads.keys().copied().collect(), |id| {
                Ok(moved_payloads[&id].clone())
            })
            .unwrap();
        let moved = PayloadSegmentReference::new(moved.metadata(), moved.first_block_id(), moved.last_block_id())
            .unwrap();
        let retained = *references.iter().find(|reference| reference.segment_id == catalog_segment_id(1)).unwrap();
        store.install_payload_manifest(PayloadManifest::new(vec![retained, added, moved]).unwrap()).unwrap();
        segment_store.remove_segment(catalog_segment_id(0)).unwrap();

        assert_eq!(reader.get(packed).unwrap(), packed_payload);
        assert_eq!(reader.get(probes[0]).unwrap(), moved_payloads[&probes[0]]);
        assert!(reader.get(probes[1]).unwrap().starts_with(b"catalog-1-"));
        let missing = BlockId::for_payload(b"never written");
        assert_eq!(reader.get(missing).unwrap_err().kind(), io::ErrorKind::NotFound);
    }

    fn segment_reads(vfs: &DeterministicVfs) -> usize {
        vfs.trace().unwrap().iter().filter(|operation| **operation == Operation::Read).count()
    }

    /// Live shape on 2026-11-06 (docs/TOFUDB_BLOCK_READ_PATH.md, "Published
    /// catalog location table"): a foreground cold read of a packed block
    /// copied and sorted one candidate per catalogued segment, checked every
    /// segment witness and verified the whole index of its owner and of each
    /// witness false positive missing from the verified-index cache. With the
    /// published table it costs one candidate, one index-entry read and one
    /// payload read; no witness is checked and no index is read or admitted.
    #[test]
    fn foreground_cold_packed_reads_resolve_through_the_published_location_table() {
        const SEGMENTS: usize = 48;
        const BLOCKS: usize = 512;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, probes) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        store.set_segment_index_cache_bytes(128 * 1024).unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        drop(store.traversal_reader(|| Ok(())).unwrap());
        let (published, table) = store.published_catalog_locations_for_test().unwrap();
        assert_eq!(published, store.installed_catalog_publication_for_test());
        assert_eq!(table.located_blocks(), SEGMENTS * BLOCKS);

        crate::payload_segment::read_path_counters::take();
        crate::payload_segment::read_path_counters::take_index_work();
        let before = segment_reads(&vfs);
        for id in &probes {
            assert!(store.get(*id).unwrap().starts_with(b"catalog-"));
        }
        let located_reads = segment_reads(&vfs) - before;
        assert_eq!(located_reads, 2 * probes.len(), "one index entry and one payload read per block");
        assert_eq!(crate::payload_segment::read_path_counters::take(), (0, probes.len() as u64));
        assert_eq!(crate::payload_segment::read_path_counters::take_index_work(), (0, 0, 0));
        assert!(store.payload_segment_store().unwrap().resident_index_ids_for_test().is_empty());

        set_catalog_locations_disabled_for_test(true);
        let before = segment_reads(&vfs);
        for id in &probes {
            assert!(store.get(*id).unwrap().starts_with(b"catalog-"));
        }
        let exact_reads = segment_reads(&vfs) - before;
        set_catalog_locations_disabled_for_test(false);
        let (witness_checks, _) = crate::payload_segment::read_path_counters::take();
        let (index_reads, index_bytes, _) = crate::payload_segment::read_path_counters::take_index_work();
        assert!(witness_checks >= (SEGMENTS * probes.len()) as u64);
        assert!(index_reads >= probes.len() as u64, "a whole verified index per cold block");
        println!("foreground located_reads={located_reads} exact_reads={exact_reads} \
            exact_witness_checks={witness_checks} exact_index_reads={index_reads} \
            exact_index_bytes={index_bytes} blocks={}", probes.len());
    }

    /// A current table proves a block absent from the installed catalog: the
    /// read goes straight to the loose copy and a missing block is reported
    /// without a witness walk or an index read, also before the packed filter
    /// is built (a clean open).
    #[test]
    fn a_current_location_table_sends_uncatalogued_blocks_to_their_loose_copy() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, _) = full_range_catalog(&store, 8, 64);
        let manifest = PayloadManifest::new(references).unwrap();
        let loose = store.put_loose(b"loose beside the catalog").unwrap();
        drop(store);
        let reopened = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap();
        reopened.set_cache_capacity_bytes(0).unwrap();
        reopened.install_recovery_payload_manifest(manifest).unwrap();
        assert!(!reopened.packed_filter.read().unwrap().built, "fixture: a clean open builds no filter");
        let missing = BlockId::for_payload(b"never written");
        crate::payload_segment::read_path_counters::take();
        assert_eq!(reopened.get(missing).unwrap_err().kind(), io::ErrorKind::NotFound);
        assert!(crate::payload_segment::read_path_counters::take().0 >= 8, "without a table a miss walks every witness");

        drop(reopened.traversal_reader(|| Ok(())).unwrap());
        crate::payload_segment::read_path_counters::take_index_work();
        assert_eq!(reopened.get(loose).unwrap(), b"loose beside the catalog");
        assert_eq!(reopened.get(missing).unwrap_err().kind(), io::ErrorKind::NotFound);
        assert_eq!(crate::payload_segment::read_path_counters::take(), (0, 0));
        assert_eq!(crate::payload_segment::read_path_counters::take_index_work().0, 0);
    }

    /// docs/TOFUDB_BLOCK_READ_PATH.md, "Published catalog location table": a
    /// derivation writes into the table the published one replaced, once no
    /// reader holds it, so the published table and its spare alternate
    /// between two allocations instead of mapping a fresh one per
    /// publication. Before the change every derivation allocated exactly its
    /// entries; here the catalog that shrinks at the third derivation keeps
    /// the larger allocation of the first. A table a reader still holds, as a
    /// whole-store pass holds its table for its whole mark, is never written:
    /// that derivation allocates.
    #[test]
    fn derivations_write_into_the_retired_table_once_no_reader_holds_it() {
        const BLOCKS: usize = 32;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_cache_capacity_bytes(0).unwrap();
        let (references, _) = full_range_catalog(&store, 6, BLOCKS);
        let derive = |segments: usize| {
            store.install_payload_manifest(PayloadManifest::new(references[..segments].to_vec()).unwrap()).unwrap();
            assert!(store.advance_catalog_locations().unwrap());
            let (_, table) = store.published_catalog_locations_for_test().unwrap();
            let fresh = store.payload_segment_store().unwrap()
                .build_location_table(&references[..segments], || Ok(())).unwrap();
            assert_eq!(table.located_entries_for_test(), fresh.located_entries_for_test());
            table
        };
        store.install_payload_manifest(PayloadManifest::new(references[..4].to_vec()).unwrap()).unwrap();
        drop(store.traversal_reader(|| Ok(())).unwrap());
        let first = derive(5);
        let (first_allocation, first_capacity) = (first.entries_allocation_for_test(), first.entries_capacity_for_test());
        assert_eq!(first_capacity, 5 * BLOCKS, "no spare yet: the first derivation allocates its entries");
        drop(first);
        drop(derive(6));
        let third = derive(3);
        assert_eq!(
            (third.entries_allocation_for_test(), third.entries_capacity_for_test()),
            (first_allocation, first_capacity),
            "the third derivation writes into the first one's allocation"
        );
        let held_entries = third.located_entries_for_test();
        drop(derive(4));
        let fifth = derive(2);
        assert_ne!(fifth.entries_allocation_for_test(), third.entries_allocation_for_test());
        assert_eq!(third.located_entries_for_test(), held_entries, "a table a reader holds is never written");
    }

    /// Each publication's table is derived from the previous one: kept
    /// segments' entries are renumbered, a dropped segment's entries leave and
    /// only segments the old table did not index are read. The result locates
    /// exactly what a fresh build of the installed catalog locates. Until it
    /// is derived the stale table answers nothing, so no read can resolve
    /// through a segment the installed catalog no longer names.
    #[test]
    fn derived_catalog_locations_equal_a_fresh_build_of_each_publication() {
        const BLOCKS: usize = 64;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_cache_capacity_bytes(0).unwrap();
        let (references, probes) = full_range_catalog(&store, 4, BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        assert!(!store.advance_catalog_locations().unwrap(), "nothing to derive from before a pass publishes");
        drop(store.traversal_reader(|| Ok(())).unwrap());
        assert!(!store.advance_catalog_locations().unwrap(), "a current table needs no derivation");

        // An adoption-style merge re-homes segment 0 into segment 8, and
        // segment 7 adds new content.
        let segment_store = store.payload_segment_store().unwrap();
        let added_payload = b"packed-after-the-table".to_vec();
        let added = segment_holding(&store, 7, &added_payload);
        let moved_payloads: BTreeMap<BlockId, Vec<u8>> = (0..BLOCKS)
            .map(|block_number| {
                let bytes = format!("catalog-0-{block_number}").into_bytes();
                (BlockId::for_payload(&bytes), bytes)
            })
            .collect();
        let moved = segment_store
            .create(catalog_segment_id(8), moved_payloads.keys().copied().collect(), |id| {
                Ok(moved_payloads[&id].clone())
            })
            .unwrap();
        let moved = PayloadSegmentReference::new(moved.metadata(), moved.first_block_id(), moved.last_block_id())
            .unwrap();
        let mut catalog = references[1..].to_vec();
        catalog.extend([added, moved]);
        store.install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap()).unwrap();
        segment_store.remove_segment(catalog_segment_id(0)).unwrap();

        crate::payload_segment::read_path_counters::take();
        assert_eq!(store.get(probes[0]).unwrap(), moved_payloads[&probes[0]]);
        let (witness_checks, candidates) = crate::payload_segment::read_path_counters::take();
        assert!(witness_checks > 0 && candidates == 0, "a segment the installed catalog dropped never answers");

        crate::payload_segment::read_path_counters::take_index_work();
        assert!(store.advance_catalog_locations().unwrap());
        let (index_reads, _, _) = crate::payload_segment::read_path_counters::take_index_work();
        assert!(index_reads <= 2, "only the two added segments' indexes may be read, not {index_reads}");
        let (derived_publication, derived) = store.published_catalog_locations_for_test().unwrap();
        assert_eq!(derived_publication, store.installed_catalog_publication_for_test());
        let fresh = segment_store.build_location_table(&catalog, || Ok(())).unwrap();
        assert_eq!(derived.located_entries_for_test(), fresh.located_entries_for_test());
        assert_eq!((derived.located_blocks(), derived.unindexed_segments()), (4 * BLOCKS + 1, 0));

        crate::payload_segment::read_path_counters::take();
        assert_eq!(store.get(probes[0]).unwrap(), moved_payloads[&probes[0]]);
        assert_eq!(store.get(BlockId::for_payload(&added_payload)).unwrap(), added_payload);
        assert!(store.get(probes[1]).unwrap().starts_with(b"catalog-1-"));
        assert_eq!(crate::payload_segment::read_path_counters::take(), (0, 3));

        // Dropping the rest derives an empty table that still proves absence.
        store.install_payload_manifest(PayloadManifest::new(Vec::new()).unwrap()).unwrap();
        assert!(store.advance_catalog_locations().unwrap());
        let (_, empty) = store.published_catalog_locations_for_test().unwrap();
        assert_eq!((empty.located_blocks(), empty.unindexed_segments()), (0, 0));
    }

    /// A table a later catalog made stale still locates the blocks of the
    /// segments that catalog kept, through reads and membership probes. The
    /// segment it dropped never answers, so dedupe restages content whose only
    /// copy it held, and only the derived table proves a block absent.
    #[test]
    fn stale_location_tables_locate_kept_segments_and_never_prove_absence() {
        const BLOCKS: usize = 32;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_cache_capacity_bytes(0).unwrap();
        let (references, probes) = full_range_catalog(&store, 3, BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        drop(store.traversal_reader(|| Ok(())).unwrap());
        let (built_for, _) = store.published_catalog_locations_for_test().unwrap();
        let added_payload = b"added-after-the-table".to_vec();
        let added_id = BlockId::for_payload(&added_payload);
        let added = segment_holding(&store, 7, &added_payload);
        store.install_payload_manifest(PayloadManifest::new(vec![references[1], references[2], added]).unwrap()).unwrap();
        assert_ne!(built_for, store.installed_catalog_publication_for_test(), "fixture: the table is stale");

        assert_eq!(store.published_catalog_answer_for_test(probes[1]), "held");
        crate::payload_segment::read_path_counters::take();
        assert!(store.get(probes[1]).unwrap().starts_with(b"catalog-1-"));
        assert_eq!(store.catalogued_payload_segment(probes[2]).unwrap(), Some(catalog_segment_id(2)));
        assert_eq!(crate::payload_segment::read_path_counters::take(), (0, 2), "no catalog walk");
        let missing = BlockId::for_payload(b"never written");
        for id in [probes[0], added_id, missing] {
            assert_eq!(store.published_catalog_answer_for_test(id), "unknown");
        }
        assert_eq!(store.get(added_id).unwrap(), added_payload);
        assert_eq!(store.get(probes[0]).unwrap_err().kind(), io::ErrorKind::NotFound);
        let (_, _, dropped_only_copy) = catalog_layout(0, BLOCKS, probes[0]);
        assert_eq!(store.put_deferred(&dropped_only_copy).unwrap(), (probes[0], true),
            "dedupe restages content whose only segment the installed catalog dropped");

        assert!(store.advance_catalog_locations().unwrap());
        assert_eq!(store.published_catalog_answer_for_test(added_id), "held");
        for id in [probes[0], missing] {
            assert_eq!(store.published_catalog_answer_for_test(id), "absent");
        }
    }

    /// Writers probe dedupe under the authority mutex, and every online-GC
    /// scan probes the membership of each live loose block. Both resolve a
    /// catalogued block through the published table: one candidate each, no
    /// witness walk, no whole-index read. The exact catalog path, the control
    /// below, walks every witness per probe and verifies whole indexes.
    #[test]
    fn dedupe_and_membership_probes_resolve_through_the_published_table() {
        const SEGMENTS: usize = 16;
        const BLOCKS: usize = 64;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_cache_capacity_bytes(0).unwrap();
        let (references, probes) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        store.set_segment_index_cache_bytes(16 * 1024).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        drop(store.traversal_reader(|| Ok(())).unwrap());
        let probe_all = |store: &BlockStore| {
            for (segment_number, id) in probes.iter().enumerate() {
                assert_eq!(store.catalogued_payload_segment(*id).unwrap(), Some(catalog_segment_id(segment_number)));
                let (_, _, bytes) = catalog_layout(segment_number, BLOCKS, *id);
                assert_eq!(store.put_deferred(&bytes).unwrap(), (*id, false), "a packed copy dedupes");
            }
        };
        crate::payload_segment::read_path_counters::take();
        crate::payload_segment::read_path_counters::take_index_work();
        probe_all(&store);
        assert_eq!(crate::payload_segment::read_path_counters::take(), (0, 2 * SEGMENTS as u64));
        assert_eq!(crate::payload_segment::read_path_counters::take_index_work(), (0, 0, 0));
        let fresh = b"content no segment holds".to_vec();
        assert_eq!(store.put_deferred(&fresh).unwrap(), (BlockId::for_payload(&fresh), true));

        set_catalog_locations_disabled_for_test(true);
        probe_all(&store);
        set_catalog_locations_disabled_for_test(false);
        let (witness_checks, _) = crate::payload_segment::read_path_counters::take();
        let (index_reads, index_bytes, _) = crate::payload_segment::read_path_counters::take_index_work();
        assert!(witness_checks >= 2 * (SEGMENTS * SEGMENTS) as u64, "{witness_checks}");
        assert!(index_reads > 0);
        println!("probes={} located_candidates={} exact_witness_checks={witness_checks} \
            exact_index_reads={index_reads} exact_index_bytes={index_bytes}", 2 * SEGMENTS, 2 * SEGMENTS);
    }

    /// The first table is built once, from every index of the installed
    /// catalog; any published table, even a stale one, is derived from
    /// instead of rebuilt.
    #[test]
    fn the_first_catalog_location_table_is_built_once() {
        const SEGMENTS: usize = 4;
        const BLOCKS: usize = 16;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        assert!(!store.publish_first_catalog_locations(|| Ok(())).unwrap(), "no catalog, no table");
        let (references, _) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        assert!(store.publish_first_catalog_locations(|| Ok(())).unwrap());
        let (publication, table) = store.published_catalog_locations_for_test().unwrap();
        assert_eq!(publication, store.installed_catalog_publication_for_test());
        assert_eq!(table.located_blocks(), SEGMENTS * BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references[1..].to_vec()).unwrap()).unwrap();
        let before = segment_reads(&vfs);
        assert!(!store.publish_first_catalog_locations(|| Ok(())).unwrap());
        assert_eq!(segment_reads(&vfs) - before, 0, "a stale table is never rebuilt");
    }

    /// A whole-store pass reuses the published table while it describes the
    /// installed catalog, derives a stale one, and builds, then publishes, a
    /// table only when none was published.
    #[test]
    fn traversal_readers_share_the_current_published_location_table() {
        const SEGMENTS: usize = 6;
        const BLOCKS: usize = 32;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (references, _) = full_range_catalog(&store, SEGMENTS, BLOCKS);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        let before = segment_reads(&vfs);
        drop(store.traversal_reader(|| Ok(())).unwrap());
        assert_eq!(segment_reads(&vfs) - before, 3 * SEGMENTS);
        let before = segment_reads(&vfs);
        let reader = store.traversal_reader(|| Ok(())).unwrap();
        assert_eq!(segment_reads(&vfs) - before, 0, "the current published table is reused");
        assert_eq!(reader.location_table_shape().0, SEGMENTS * BLOCKS);
        drop(reader);

        store.install_payload_manifest(PayloadManifest::new(references[1..].to_vec()).unwrap()).unwrap();
        let before = segment_reads(&vfs);
        drop(store.traversal_reader(|| Ok(())).unwrap());
        assert_eq!(segment_reads(&vfs) - before, 0, "a stale table is derived: a drop reads no index");
        let (published, table) = store.published_catalog_locations_for_test().unwrap();
        assert_eq!(published, store.installed_catalog_publication_for_test());
        assert_eq!(table.located_blocks(), (SEGMENTS - 1) * BLOCKS);
    }

    /// Paired cold-read measurement for full-range catalogs on a real volume
    /// (docs/TOFUDB_BLOCK_READ_PATH.md, "Catalog-wide negative witnesses").
    /// `TOFU_DB_COLD_READ_BENCH_DIR` selects the volume; the defaults keep the
    /// 2026-10-28 production ratios: membership filters of under a third of
    /// the catalog fit the old quarter budget and full indexes of about 3%.
    /// It reports per-read wall time plus this process's `rchar`/`syscr`
    /// deltas for cold reads, warm reads, reads after only the payload cache
    /// is cleared, and reads after a catalog replaces one segment with a
    /// merged copy.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore]
    fn cold_packed_read_measurement() {
        let Some(root) = std::env::var_os("TOFU_DB_COLD_READ_BENCH_DIR") else { return; };
        let env = |name: &str, default: usize| {
            std::env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
        };
        let segments = env("TOFU_DB_COLD_READ_BENCH_SEGMENTS", 512);
        let blocks = env("TOFU_DB_COLD_READ_BENCH_BLOCKS", 8_192);
        let reads = env("TOFU_DB_COLD_READ_BENCH_READS", 200);
        let filter_bytes = (blocks * 16).next_power_of_two() / 8;
        let budget = env("TOFU_DB_COLD_READ_BENCH_BUDGET", 4 * filter_bytes * segments * 29 / 100);
        let directory = std::path::PathBuf::from(root).join(format!("cold-read-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let (references, probes) = {
            let store = BlockStore::initialize(&directory).unwrap();
            full_range_catalog(&store, segments, blocks)
        };
        let io = || {
            let text = std::fs::read_to_string("/proc/self/io").unwrap();
            let field = |name: &str| text.lines().find_map(|line| line.strip_prefix(name))
                .and_then(|value| value.trim().parse::<u64>().ok()).unwrap();
            (field("rchar:"), field("syscr:"))
        };
        let store = BlockStore::open(&directory).unwrap();
        store.set_cache_capacity_bytes(1 << 30).unwrap();
        store.set_segment_index_cache_bytes(budget).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        // Distinct segments in a fixed pseudo-random order: no cold read can
        // reuse a payload or index another cold read already brought in.
        let mut state = 0x2545_f491_4f6c_dd1d_u64;
        let mut chosen = probes.clone();
        for index in (1..chosen.len()).rev() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            chosen.swap(index, (state % (index as u64 + 1)) as usize);
        }
        chosen.truncate(reads);
        let reads = chosen.len();
        let pass = |label: &str| {
            let (rchar, syscr) = io();
            let started = std::time::Instant::now();
            for id in &chosen {
                assert!(store.get(*id).unwrap().starts_with(b"catalog-"));
            }
            let elapsed = started.elapsed();
            let (rchar_after, syscr_after) = io();
            eprintln!(
                "cold_packed_read {label} segments={segments} blocks={blocks} reads={reads} budget={budget} \
                 us_per_read={:.1} read_bytes_per_read={} read_calls_per_read={:.1}",
                elapsed.as_secs_f64() * 1e6 / reads as f64,
                (rchar_after - rchar) / reads as u64,
                (syscr_after - syscr) as f64 / reads as f64,
            );
        };
        pass("cold");
        pass("warm");
        // Engine caches cold, operating-system page cache warm: the state a
        // long-running daemon reaches after its payload cache loses a block.
        store.cache.write().unwrap().clear();
        pass("engine_cold_os_warm");
        // A full catalog adopts a pack by merging it into its smallest segment.
        let segment_store = store.payload_segment_store().unwrap();
        let victim = references[0];
        let source = segment_store.open_segment(victim.segment_id).unwrap();
        let mut merged_ids: Vec<_> = source.block_ids().collect();
        let incoming = b"cold-read-incoming".to_vec();
        merged_ids.push(BlockId::for_payload(&incoming));
        merged_ids.sort_unstable();
        let merged = segment_store
            .create(Uuid::new_v4(), merged_ids, |id| {
                if id == BlockId::for_payload(&incoming) { Ok(incoming.clone()) }
                else { Ok(segment_store.get_with_handle(&source, id).unwrap().unwrap()) }
            })
            .unwrap();
        let mut replacement = references.clone();
        replacement[0] = PayloadSegmentReference::new(merged.metadata(), merged.first_block_id(), merged.last_block_id())
            .unwrap();
        store.verify_payload_segment(&replacement[0]).unwrap();
        store.install_payload_manifest(PayloadManifest::new(replacement).unwrap()).unwrap();
        pass("after_merge_drop");
        drop(store);
        std::fs::remove_dir_all(&directory).unwrap();
    }

    /// Paired real-volume measurement of foreground cold reads through the
    /// published catalog location table versus the exact catalog path
    /// (docs/TOFUDB_BLOCK_READ_PATH.md, "Published catalog location table").
    /// `TOFU_DB_COLD_READ_BENCH_DIR` selects the volume. The verified-index
    /// cache holds 29% of the index bytes, the live 2026-11-06 ratio (3.2 GB
    /// of about 11 GB). Each pass reads one distinct block per segment in a
    /// fixed pseudo-random order, and the two passes read disjoint blocks, so
    /// neither warms the other's payload pages; the exact pass runs second
    /// and so inherits any page-cache warmth of the shared indexes.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore]
    fn published_location_table_cold_read_measurement() {
        let Some(root) = std::env::var_os("TOFU_DB_COLD_READ_BENCH_DIR") else { return; };
        let env = |name: &str, default: usize| {
            std::env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
        };
        let segments = env("TOFU_DB_COLD_READ_BENCH_SEGMENTS", 1_024);
        let blocks = env("TOFU_DB_COLD_READ_BENCH_BLOCKS", 8_192);
        let reads = env("TOFU_DB_COLD_READ_BENCH_READS", 200).min(segments);
        let directory = std::path::PathBuf::from(root).join(format!("located-read-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let built = std::time::Instant::now();
        let references = {
            let store = BlockStore::initialize(&directory).unwrap();
            full_range_catalog(&store, segments, blocks).0
        };
        let catalog_seconds = built.elapsed().as_secs_f64();
        let index_bytes: usize = references.iter().map(|reference| reference.block_count as usize * 48).sum();
        let store = BlockStore::open(&directory).unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        store.set_segment_index_cache_bytes(index_bytes * 29 / 100).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        let started = std::time::Instant::now();
        drop(store.traversal_reader(|| Ok(())).unwrap());
        let table_build = started.elapsed();
        let (_, table) = store.published_catalog_locations_for_test().unwrap();
        let mut order: Vec<usize> = (0..segments).collect();
        let mut state = 0x2545_f491_4f6c_dd1d_u64;
        for index in (1..order.len()).rev() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            order.swap(index, (state % (index as u64 + 1)) as usize);
        }
        order.truncate(reads);
        let io = || {
            let text = std::fs::read_to_string("/proc/self/io").unwrap();
            let field = |name: &str| text.lines().find_map(|line| line.strip_prefix(name))
                .and_then(|value| value.trim().parse::<u64>().ok()).unwrap();
            (field("rchar:"), field("syscr:"))
        };
        let pass = |label: &str, block_number: usize, exact: bool| {
            set_catalog_locations_disabled_for_test(exact);
            crate::payload_segment::read_path_counters::take();
            let (rchar, syscr) = io();
            let started = std::time::Instant::now();
            for segment_number in &order {
                let bytes = format!("catalog-{segment_number}-{block_number}").into_bytes();
                assert_eq!(store.get(BlockId::for_payload(&bytes)).unwrap(), bytes);
            }
            let elapsed = started.elapsed();
            let (rchar_after, syscr_after) = io();
            let (witness_checks, _) = crate::payload_segment::read_path_counters::take();
            set_catalog_locations_disabled_for_test(false);
            eprintln!(
                "located_read {label} segments={segments} blocks={blocks} reads={reads} \
                 us_per_read={:.1} read_bytes_per_read={} read_calls_per_read={:.1} witness_checks_per_read={:.1}",
                elapsed.as_secs_f64() * 1e6 / reads as f64,
                (rchar_after - rchar) / reads as u64,
                (syscr_after - syscr) as f64 / reads as f64,
                witness_checks as f64 / reads as f64,
            );
        };
        eprintln!(
            "located_read setup segments={segments} blocks={blocks} catalog_build_s={catalog_seconds:.1} \
             table_build_ms={} table_bytes={} index_bytes={index_bytes}",
            table_build.as_millis(), table.resident_bytes(),
        );
        pass("located", 1, false);
        pass("exact", 2, true);
        pass("located_again", 3, false);
        drop(table);
        drop(store);
        std::fs::remove_dir_all(&directory).unwrap();
    }

    /// Paired real-volume measurement of concurrent cold packed reads
    /// (docs/TOFUDB_BLOCK_READ_PATH.md, "Shared segment read handles"). With
    /// the payload cache disabled and a published location table, every read
    /// is a located read of a few hot segments by many threads at once, the
    /// shape that queued live connection threads on the read-handle cache.
    /// Reports throughput and this process's CPU time and context switches
    /// (`getrusage`, terminated threads included) per pass.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore]
    fn concurrent_packed_read_measurement() {
        let Some(root) = std::env::var_os("TOFU_DB_COLD_READ_BENCH_DIR") else { return; };
        let env = |name: &str, default: usize| {
            std::env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
        };
        let segments = env("TOFU_DB_COLD_READ_BENCH_SEGMENTS", 256);
        let blocks = env("TOFU_DB_COLD_READ_BENCH_BLOCKS", 4_096);
        let hot = env("TOFU_DB_CONCURRENT_READ_BENCH_HOT_SEGMENTS", 16).clamp(1, segments);
        let threads = env("TOFU_DB_CONCURRENT_READ_BENCH_THREADS", 64);
        let reads = env("TOFU_DB_CONCURRENT_READ_BENCH_READS", 2_000);
        let directory = std::path::PathBuf::from(root).join(format!("concurrent-read-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let references = {
            let store = BlockStore::initialize(&directory).unwrap();
            full_range_catalog(&store, segments, blocks).0
        };
        let index_bytes: usize = references.iter().map(|reference| reference.block_count as usize * 48).sum();
        let store = BlockStore::open(&directory).unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        store.set_segment_index_cache_bytes(index_bytes * 29 / 100).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        drop(store.traversal_reader(|| Ok(())).unwrap());
        let usage = || {
            // SAFETY: getrusage fills the caller-owned structure.
            let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
            assert_eq!(unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) }, 0);
            let micros = |time: libc::timeval| time.tv_sec as i64 * 1_000_000 + time.tv_usec as i64;
            (micros(usage.ru_utime) + micros(usage.ru_stime), usage.ru_nvcsw as i64, usage.ru_nivcsw as i64)
        };
        let pass = |label: &str| {
            let (cpu, voluntary, involuntary) = usage();
            let started = std::time::Instant::now();
            std::thread::scope(|scope| {
                for thread in 0..threads {
                    let store = &store;
                    scope.spawn(move || {
                        let mut state = 0x9e37_79b9_7f4a_7c15_u64 ^ (thread as u64 + 1).wrapping_mul(0x2545_f491_4f6c_dd1d);
                        for _ in 0..reads {
                            state ^= state << 13;
                            state ^= state >> 7;
                            state ^= state << 17;
                            let segment_number = (state % hot as u64) as usize;
                            let block_number = ((state >> 32) % blocks as u64) as usize;
                            let bytes = format!("catalog-{segment_number}-{block_number}").into_bytes();
                            assert_eq!(store.get(BlockId::for_payload(&bytes)).unwrap(), bytes);
                        }
                    });
                }
            });
            let elapsed = started.elapsed().as_secs_f64();
            let (cpu_after, voluntary_after, involuntary_after) = usage();
            let total = (threads * reads) as f64;
            eprintln!(
                "concurrent_packed_read {label} segments={segments} blocks={blocks} hot={hot} threads={threads} \
                 reads={} reads_per_s={:.0} cpu_us_per_read={:.1} voluntary_switches_per_read={:.2} \
                 involuntary_switches_per_read={:.2}",
                threads * reads,
                total / elapsed,
                (cpu_after - cpu) as f64 / total,
                (voluntary_after - voluntary) as f64 / total,
                (involuntary_after - involuntary) as f64 / total,
            );
        };
        pass("first");
        pass("second");
        drop(store);
        std::fs::remove_dir_all(&directory).unwrap();
    }

    /// Real-volume cold lookups after a later catalog publication
    /// (docs/TOFUDB_BLOCK_READ_PATH.md, "Packed filter across publications").
    /// On a full-range catalog under `TOFU_DB_COLD_READ_BENCH_DIR`, installed
    /// and then extended by one verified checkpoint pack, it reports per lookup
    /// the wall time, this process's `rchar`/`syscr` deltas and the segment
    /// witness checks of cold loose reads, missing-block lookups and dedupe
    /// probes of new content. The index budget keeps the 2026-10-28 production
    /// ratio of `cold_packed_read_measurement`.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore]
    fn cold_loose_lookup_after_publication_measurement() {
        let Some(root) = std::env::var_os("TOFU_DB_COLD_READ_BENCH_DIR") else { return; };
        let env = |name: &str, default: usize| {
            std::env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
        };
        // The live catalog on 2026-10-31; one more segment still fits the bound.
        let segments = env("TOFU_DB_COLD_READ_BENCH_SEGMENTS", 4_091);
        let blocks = env("TOFU_DB_COLD_READ_BENCH_BLOCKS", 1_024);
        let lookups = env("TOFU_DB_COLD_READ_BENCH_READS", 200);
        let filter_bytes = (blocks * 16).next_power_of_two() / 8;
        let budget = env("TOFU_DB_COLD_READ_BENCH_BUDGET", 4 * filter_bytes * segments * 29 / 100);
        let directory = std::path::PathBuf::from(root).join(format!("cold-loose-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let (mut references, _) = {
            let store = BlockStore::initialize(&directory).unwrap();
            full_range_catalog(&store, segments, blocks)
        };
        let io = || {
            let text = std::fs::read_to_string("/proc/self/io").unwrap();
            let field = |name: &str| text.lines().find_map(|line| line.strip_prefix(name))
                .and_then(|value| value.trim().parse::<u64>().ok()).unwrap();
            (field("rchar:"), field("syscr:"))
        };
        let store = BlockStore::open(&directory).unwrap();
        store.set_cache_capacity_bytes(1 << 30).unwrap();
        store.set_segment_index_cache_bytes(budget).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        let segment_store = store.payload_segment_store().unwrap();
        let packed: BTreeMap<BlockId, Vec<u8>> = (0..blocks)
            .map(|number| {
                let bytes = format!("bench-checkpoint-{number}").into_bytes();
                (BlockId::for_payload(&bytes), bytes)
            })
            .collect();
        let segment = segment_store
            .create(Uuid::new_v4(), packed.keys().copied().collect(), |id| Ok(packed[&id].clone()))
            .unwrap();
        let reference =
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        store.note_packed(&packed.keys().copied().collect::<Vec<_>>()).unwrap();
        references.push(reference);
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        let loose: Vec<BlockId> = (0..lookups)
            .map(|number| store.put(format!("bench-loose-{number}").as_bytes()).unwrap())
            .collect();
        store.cache.write().unwrap().clear();
        let pass = |label: &str, run: &mut dyn FnMut()| {
            crate::payload_segment::read_path_counters::take();
            let (rchar, syscr) = io();
            let started = std::time::Instant::now();
            run();
            let elapsed = started.elapsed();
            let (rchar_after, syscr_after) = io();
            let (witness_checks, _) = crate::payload_segment::read_path_counters::take();
            eprintln!(
                "cold_loose_lookup {label} segments={} blocks={blocks} lookups={lookups} budget={budget} \
                 us_per_lookup={:.1} read_bytes_per_lookup={} read_calls_per_lookup={:.1} witness_checks_per_lookup={:.1}",
                segments + 1,
                elapsed.as_secs_f64() * 1e6 / lookups as f64,
                (rchar_after - rchar) / lookups as u64,
                (syscr_after - syscr) as f64 / lookups as f64,
                witness_checks as f64 / lookups as f64,
            );
        };
        pass("loose_cold", &mut || {
            for id in &loose {
                assert!(store.get(*id).unwrap().starts_with(b"bench-loose-"));
            }
        });
        pass("missing", &mut || {
            for number in 0..lookups {
                let id = BlockId::for_payload(format!("bench-missing-{number}").as_bytes());
                assert_eq!(store.get(id).unwrap_err().kind(), io::ErrorKind::NotFound);
            }
        });
        pass("dedupe_new", &mut || {
            for number in 0..lookups {
                assert!(store.put_deferred(format!("bench-dedupe-{number}").as_bytes()).unwrap().1);
            }
        });
        drop(store);
        std::fs::remove_dir_all(&directory).unwrap();
    }

    /// Real-volume measurement for the traversal reader (docs/TOFUDB_BLOCK_READ_PATH.md,
    /// "Catalog location table for whole-store passes"). On a full-range
    /// catalog under `TOFU_DB_TRAVERSAL_BENCH_DIR` it reports, per block and
    /// with this process's `rchar`/`syscr`: cold exact-path reads of one block
    /// per segment, the location-table build, and cold located reads of other
    /// blocks of the same segments. The index cache budget keeps the 2026-10-28
    /// production ratio of `cold_packed_read_measurement`.
    #[cfg(target_os = "linux")]
    #[test]
    #[ignore]
    fn traversal_reader_measurement() {
        let Some(root) = std::env::var_os("TOFU_DB_TRAVERSAL_BENCH_DIR") else { return; };
        let env = |name: &str, default: usize| {
            std::env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
        };
        let segments = env("TOFU_DB_TRAVERSAL_BENCH_SEGMENTS", 256);
        let blocks = env("TOFU_DB_TRAVERSAL_BENCH_BLOCKS", 8_192);
        let filter_bytes = (blocks * 16).next_power_of_two() / 8;
        let budget = 4 * filter_bytes * segments * 29 / 100;
        let directory = std::path::PathBuf::from(root).join(format!("traversal-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let (references, _) = {
            let store = BlockStore::initialize(&directory).unwrap();
            full_range_catalog(&store, segments, blocks)
        };
        let io = || {
            let text = std::fs::read_to_string("/proc/self/io").unwrap();
            let field = |name: &str| text.lines().find_map(|line| line.strip_prefix(name))
                .and_then(|value| value.trim().parse::<u64>().ok()).unwrap();
            (field("rchar:"), field("syscr:"))
        };
        let store = BlockStore::open(&directory).unwrap();
        store.set_cache_capacity_bytes(0).unwrap();
        store.set_segment_index_cache_bytes(budget).unwrap();
        store.install_payload_manifest(PayloadManifest::new(references).unwrap()).unwrap();
        let probes = |block_number: usize| -> Vec<BlockId> {
            (0..segments)
                .map(|segment_number| BlockId::for_payload(format!("catalog-{segment_number}-{block_number}").as_bytes()))
                .collect()
        };
        let report = |label: &str, count: usize, run: &mut dyn FnMut()| {
            let (rchar, syscr) = io();
            let started = std::time::Instant::now();
            run();
            let elapsed = started.elapsed();
            let (rchar_after, syscr_after) = io();
            eprintln!(
                "traversal_reader {label} segments={segments} blocks={blocks} count={count} budget={budget} \
                 us_per_item={:.1} read_bytes_per_item={} read_calls_per_item={:.1}",
                elapsed.as_secs_f64() * 1e6 / count as f64,
                (rchar_after - rchar) / count as u64,
                (syscr_after - syscr) as f64 / count as f64,
            );
        };
        let exact = probes(blocks / 4);
        report("exact_cold_block", exact.len(), &mut || {
            for id in &exact {
                assert!(store.get_traversal(*id).unwrap().starts_with(b"catalog-"));
            }
        });
        let mut reader = None;
        report("table_build_segment", segments, &mut || {
            reader = Some(store.traversal_reader(|| Ok(())).unwrap());
        });
        let reader = reader.unwrap();
        let located = probes(3 * blocks / 4);
        report("located_cold_block", located.len(), &mut || {
            for id in &located {
                assert!(reader.get(*id).unwrap().starts_with(b"catalog-"));
            }
        });
        report("located_repeat_block", located.len(), &mut || {
            for id in &located {
                assert!(reader.get(*id).unwrap().starts_with(b"catalog-"));
            }
        });
        eprintln!("traversal_reader table_bytes={}", reader.location_table_shape().2);
        drop(reader);
        drop(store);
        std::fs::remove_dir_all(&directory).unwrap();
    }

    /// Paired measurement for the block payload cache budget and catalog
    /// invalidation (docs/TOFUDB_BLOCK_READ_PATH.md). Deterministic VFS:
    /// a miss is exactly one payload read reaching the filesystem layer.
    #[test]
    #[ignore]
    fn block_cache_working_set_measurement() {
        let segments_total = 160_u8;
        let blocks_per_segment = 1_024_usize;
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let segment_store = store.payload_segment_store().unwrap();
        let mut seed = 0x9e37_79b9_7f4a_7c15_u64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut references = Vec::new();
        let mut catalog_ids = Vec::new();
        for segment_number in 0..segments_total {
            let payloads: std::collections::BTreeMap<BlockId, Vec<u8>> = (0..blocks_per_segment)
                .map(|block_number| {
                    let mut bytes = vec![0_u8; 2_048];
                    for chunk in bytes.chunks_mut(8) {
                        chunk.copy_from_slice(&next().to_le_bytes()[..chunk.len()]);
                    }
                    bytes[..8].copy_from_slice(
                        &((segment_number as u64) << 32 | block_number as u64).to_le_bytes(),
                    );
                    (BlockId::for_payload(&bytes), bytes)
                })
                .collect();
            let mut segment_id = [0_u8; 16];
            segment_id[0] = 0xb0;
            segment_id[15] = segment_number;
            let segment = segment_store
                .create(Uuid::from_bytes(segment_id), payloads.keys().copied().collect(), |id| {
                    Ok(payloads[&id].clone())
                })
                .unwrap();
            references.push(
                PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())
                    .unwrap(),
            );
            if segment_number + 1 < segments_total {
                catalog_ids.extend(payloads.keys().copied());
            }
        }
        let initial = references[..references.len() - 1].to_vec();
        store.install_payload_manifest(PayloadManifest::new(initial).unwrap()).unwrap();
        store.set_cache_capacity_bytes(1 << 30).unwrap();
        let payload_reads = |vfs: &DeterministicVfs| {
            vfs.trace().unwrap().iter().filter(|operation| **operation == Operation::Read).count()
        };
        let mut pass = |store: &BlockStore, reads: usize| -> (usize, std::time::Duration) {
            vfs.arm_fault(None).unwrap();
            let started = std::time::Instant::now();
            for _ in 0..reads {
                let id = catalog_ids[(next() % catalog_ids.len() as u64) as usize];
                assert_eq!(store.get(id).unwrap().len(), 2_048);
            }
            (payload_reads(&vfs), started.elapsed())
        };
        let (warm_misses, _) = pass(&store, catalog_ids.len());
        let (steady_misses, steady_elapsed) = pass(&store, 200_000);
        store.install_payload_manifest(PayloadManifest::new(references.clone()).unwrap()).unwrap();
        let (after_addition_misses, after_elapsed) = pass(&store, 200_000);
        eprintln!(
            "block_cache_working_set blocks={} warm_reads={} warm_misses={} steady_reads=200000 steady_misses={} \
             steady_us_per_read={:.2} after_catalog_addition_misses={} after_us_per_read={:.2}",
            catalog_ids.len(),
            catalog_ids.len(),
            warm_misses,
            steady_misses,
            steady_elapsed.as_secs_f64() * 1e6 / 200_000.0,
            after_addition_misses,
            after_elapsed.as_secs_f64() * 1e6 / 200_000.0,
        );
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
        store.verify_payload_segment(&future).unwrap();
        store.note_packed(&[future_id]).unwrap();
        let mut filler = 0_u64;
        while !store.packed_filter.read().unwrap().saturated() {
            filler += 1;
            store.note_packed(&[BlockId::for_payload(&filler.to_le_bytes())]).unwrap();
        }
        store.note_packed(&[base_id]).unwrap();
        assert!(!store.packed_filter.read().unwrap().may_contain(future_id), "the regression needs a dropped unpublished hint");
        assert!(!store.packed_filter.read().unwrap().covers_segment(&future), "the rebuild must forget what it dropped");
        store.install_payload_manifest(PayloadManifest::new(vec![base, future]).unwrap()).unwrap();
        assert!(store.has_catalogued_payload_block(future_id).unwrap(), "a stale filter must never disprove membership in a new catalog");
        assert_eq!(store.get(future_id).unwrap(), b"durable batch awaiting catalog publication");
        // The install re-read the one segment the rebuild dropped, so negative
        // answers cover the new catalog again.
        assert!(store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()));
        assert!(store.packed_filter.read().unwrap().may_contain(future_id));
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

    /// Saturation counts distinct blocks, so every publication can re-insert
    /// the whole segments it adds (merged copies re-home blocks the filter
    /// already holds) without triggering early full-catalog rebuilds.
    #[test]
    fn packed_filter_reports_saturation_only_at_capacity() {
        let mut filter = PackedBlockFilter::with_capacity(0);
        assert!(!filter.saturated());
        let capacity = filter.capacity_blocks as u64;
        assert!(capacity as usize >= PACKED_FILTER_MIN_BITS / PACKED_FILTER_BITS_PER_BLOCK);
        let id = |index: u64| BlockId::for_payload(&index.to_le_bytes());
        for index in 0..capacity - 1 {
            filter.insert(id(index));
        }
        for index in 0..capacity - 1 {
            filter.insert(id(index));
        }
        assert!(!filter.saturated(), "re-inserting held blocks added load");
        let mut next = capacity - 1;
        while !filter.saturated() {
            filter.insert(id(next));
            next += 1;
        }
        // Only distinct blocks whose bits were all set already (filter false
        // positives, rare at this load) leave the count unchanged.
        assert!(next - capacity < capacity / 1000, "{} distinct inserts did not count", next - capacity);
    }

    #[test]
    fn packed_filter_coverage_records_are_bounded_by_one_catalog() {
        let mut filter = PackedBlockFilter::with_capacity(0);
        let reference = |number: usize| {
            let mut segment_id = [0x7e_u8; 16];
            segment_id[..8].copy_from_slice(&(number as u64).to_be_bytes());
            let id = BlockId::for_payload(format!("coverage-{number}").as_bytes());
            (PayloadSegmentReference {
                segment_id: Uuid::from_bytes(segment_id),
                shard: 0,
                block_count: 1,
                payload_bytes: 1,
                file_bytes: 1,
                first_block_id: id,
                last_block_id: id,
            }, id)
        };
        let bound = crate::payload_manifest::MAX_PAYLOAD_SEGMENTS;
        for number in 0..bound + 3 {
            let (reference, id) = reference(number);
            filter.cover_segment(&reference, std::iter::once(id));
        }
        assert_eq!(filter.uncatalogued_segments.len(), bound);
        assert_eq!(filter.covered_segments.len(), bound);
        let (forgotten, forgotten_id) = reference(0);
        assert!(!filter.covers_segment(&forgotten), "the oldest unpublished record is forgotten");
        assert!(filter.may_contain(forgotten_id), "forgetting a record never removes its bits");
        // Installing a catalog keeps its records, drops the records of
        // segments it no longer references and keeps pending ones.
        let installed: HashSet<Uuid> = (3..=4).map(|number| reference(number).0.segment_id).collect();
        filter.retain_catalog(&installed);
        assert!(filter.covers_segment(&reference(3).0) && filter.covers_segment(&reference(4).0));
        assert_eq!((filter.uncatalogued_segments.len(), filter.covered_segments.len()), (bound - 2, bound));
        filter.retain_catalog(&HashSet::from([reference(5).0.segment_id]));
        assert!(!filter.covers_segment(&reference(3).0), "a dropped segment keeps no coverage record");
        assert!(filter.covers_segment(&reference(bound + 2).0), "a pending segment keeps its record");
        assert_eq!((filter.uncatalogued_segments.len(), filter.covered_segments.len()), (bound - 3, bound - 2));
        // An unbuilt filter records nothing: it holds no bits yet.
        let mut unbuilt = PackedBlockFilter::empty();
        let (reference, id) = reference(7);
        unbuilt.cover_segment(&reference, std::iter::once(id));
        assert!(unbuilt.covered_segments.is_empty() && !unbuilt.covers_segment(&reference));
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
    fn carried_recovery_covers_every_wal_reference_beyond_the_old_location_cap() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let ids: BTreeSet<_> = (0_usize..=65_536)
            .map(|i| BlockId::for_payload(&i.to_le_bytes())).collect();
        let lookup = store.prepare_carried_recovery(ids.clone()).unwrap();
        assert_eq!(lookup.locations.keys().copied().collect::<BTreeSet<_>>(), ids);
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
        // Segments read earlier keep idle handles, so recovery may open fewer.
        assert!(catalog_opens <= 64, "recovery must read each verified segment index only once");
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

    /// Only names an engine writer mints are collectable; every other `.new-*`
    /// entry, near-miss spellings of a UUID included, fails closed.
    #[test]
    fn malformed_temporary_block_names_fail_closed() {
        let identifier = Uuid::from_bytes([0x3c; 16]);
        let hyphenated = identifier.hyphenated().to_string();
        for name in [
            ".new-not-a-uuid".to_owned(),
            format!(".new-{}", identifier.simple()),
            format!(".new-{}", hyphenated.to_uppercase()),
            format!(".new-{}", identifier.braced()),
            format!(".new-{hyphenated}.blk"),
            format!(".new-ren-{}", identifier.simple()),
            format!(".new-witness-{hyphenated}"),
        ] {
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            let block_id = store.put(b"temporary-name-shard").unwrap();
            create_simulated_file(&vfs, &store.root.join(&block_id.to_hex()[..2]).join(&name));
            let error = store.plan_temporary_file().unwrap_err();
            assert_eq!(
                (error.kind(), error.to_string()),
                (io::ErrorKind::InvalidData, "temporary block filename is malformed".to_owned()),
                "{name}"
            );
        }
    }

    /// Fails the first rename of `write` and then the writer's cleanup unlink,
    /// leaving the exact staging file a crash between staging and rename
    /// leaves. A no-fault run on an identically built store numbers the rename.
    fn leave_writer_staging_file(
        open: impl Fn(&DeterministicVfs) -> BlockStore,
        write: impl Fn(&BlockStore) -> io::Result<()>,
    ) -> (DeterministicVfs, BlockStore) {
        let rename = {
            let vfs = DeterministicVfs::new(None);
            let store = open(&vfs);
            vfs.arm_fault(None).unwrap();
            write(&store).unwrap();
            let trace = vfs.trace().unwrap();
            trace.iter().position(|operation| *operation == Operation::Rename).unwrap() + 1
        };
        let vfs = DeterministicVfs::new(None);
        let store = open(&vfs);
        let failure = FaultAction::ErrorBefore(io::ErrorKind::Other);
        vfs.arm_faults(&[
            FaultRule { operation_number: rename as u64, action: failure },
            FaultRule { operation_number: rename as u64 + 1, action: failure },
        ])
        .unwrap();
        write(&store).unwrap_err();
        let trace = vfs.trace().unwrap();
        assert_eq!(trace.get(rename - 1), Some(&Operation::Rename));
        assert_eq!(trace.get(rename), Some(&Operation::RemoveFile), "the cleanup unlink must fail too");
        vfs.arm_fault(None).unwrap();
        (vfs, store)
    }

    fn assert_staging_file_is_collected(store: &BlockStore, payload: &[u8], name_prefix: &str) {
        let shard = store.root.join(&BlockId::for_payload(payload).to_hex()[..2]);
        let candidate = store
            .plan_temporary_file()
            .unwrap()
            .candidate
            .expect("the writer's staging file is a collection candidate");
        assert_eq!(candidate.path.parent(), Some(shard.as_path()));
        let name = candidate.path.file_name().unwrap().to_str().unwrap();
        assert!(name.starts_with(name_prefix), "{name}");
        assert_eq!(store.remove_temporary_file(&candidate).unwrap(), candidate.file_bytes);
        assert!(store.plan_temporary_file().unwrap().candidate.is_none());
    }

    /// Both loose writers stage under a name the collectors accept, so a
    /// staging file an interrupted write leaves never stops collection.
    #[test]
    fn staging_files_left_by_the_loose_writers_are_collected() {
        let payload = b"interrupted between staging and rename".to_vec();
        let (_vfs, store) = leave_writer_staging_file(simulated_store, |store| {
            store.put_loose(&payload).map(drop)
        });
        assert_staging_file_is_collected(&store, &payload, ".new-");

        let (_vfs, store) = leave_writer_staging_file(simulated_store, |store| {
            let (id, staged) = store.put_deferred_fresh(&payload)?;
            assert!(staged);
            store.flush_pending_subset(&BTreeSet::from([id]))
        });
        assert_staging_file_is_collected(&store, &payload, ".new-");
    }

    /// At-rest encryption re-encrypts every loose publication through its own
    /// staging file beside the destination; an interrupted one is collected.
    #[test]
    fn staging_files_left_by_the_at_rest_rename_are_collected() {
        let encrypted_store = |vfs: &DeterministicVfs| {
            vfs.create_dir(Path::new("/data")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let encrypting = crate::at_rest::EncryptingVfs::new(
                Arc::new(vfs.clone()),
                Path::new("/data"),
                crate::at_rest::AtRestKey::from_key([0x5e; 32]),
            );
            BlockStore::initialize_with_vfs(Path::new("/data"), Arc::new(encrypting)).unwrap()
        };
        let payload = b"interrupted inside the at-rest rename".to_vec();
        let (_vfs, store) = leave_writer_staging_file(encrypted_store, |store| {
            store.put_loose(&payload).map(drop)
        });
        assert_staging_file_is_collected(&store, &payload, ".new-ren-");
    }

    /// Payloads whose content-addressed blocks all land in `shard`.
    fn payloads_in_shard(shard: u8, count: usize) -> Vec<Vec<u8>> {
        (0_u32..)
            .map(|value| format!("loose-listing-{shard:02x}-{value}").into_bytes())
            .filter(|payload| BlockId::for_payload(payload).0[0] == shard)
            .take(count)
            .collect()
    }

    fn create_simulated_file(vfs: &DeterministicVfs, path: &Path) {
        let mut file = vfs
            .open(
                path,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(0, b"loose-listing-entry").unwrap();
    }

    /// The retired 20,000,000-entry store-wide listing total at test scale;
    /// these fixtures list more entries than it, which used to fail closed.
    const RETIRED_LISTING_TOTAL_AT_TEST_SCALE: usize = 16;

    #[test]
    fn block_listing_holds_one_shard_at_a_time_past_any_store_wide_total() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let mut expected = BTreeMap::<u8, Vec<BlockId>>::new();
        for shard in [0x00, 0x5a, 0xa5, 0xff] {
            for payload in payloads_in_shard(shard, 10) {
                expected
                    .entry(shard)
                    .or_default()
                    .push(store.put_loose(&payload).unwrap());
            }
        }
        expected.values_mut().for_each(|ids| ids.sort_unstable());
        let mut visited = BTreeMap::new();
        store
            .visit_block_shards(|shard, block_ids| {
                assert!(visited.insert(shard, block_ids).is_none());
                Ok(())
            })
            .unwrap();
        // Each callback held its own shard's 10 IDs, never the store's 40.
        assert_eq!(visited, expected);
        assert!(visited.values().map(Vec::len).sum::<usize>() > RETIRED_LISTING_TOTAL_AT_TEST_SCALE);
    }

    #[test]
    fn block_listing_takes_entry_kinds_from_the_directory_without_a_stat_per_file() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payloads = payloads_in_shard(0x3c, 12);
        let mut expected: Vec<_> = payloads
            .iter()
            .map(|payload| store.put_loose(payload).unwrap())
            .collect();
        expected.sort_unstable();
        create_simulated_file(&vfs, &store.root.join("3c").join(".new-in-progress"));
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.list_block_ids(expected.len()).unwrap(), expected);
        let metadata_calls = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Metadata)
            .count();
        // Only the shard root's entry is classified by a metadata call; the
        // 12 block files and the skipped temporary are not.
        assert_eq!(metadata_calls, 1);
    }

    #[test]
    fn block_listing_refuses_or_skips_odd_entries_exactly() {
        let shard = 0x6e;
        let payloads = payloads_in_shard(shard, 2);
        let named = BlockId::for_payload(&payloads[1]).to_hex();
        let foreign = BlockId::for_payload(&payloads_in_shard(shard ^ 1, 1)[0]).to_hex();
        let listing = |prepare: &dyn Fn(&DeterministicVfs, &Path)| {
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            let listed = store.put_loose(&payloads[0]).unwrap();
            prepare(&vfs, &store.root.join(format!("{shard:02x}")));
            store
                .list_block_ids(1)
                .map(|ids| assert_eq!(ids, vec![listed]))
                .map_err(|error| (error.kind(), error.to_string()))
        };
        let refused = |message: &str| Err((io::ErrorKind::InvalidData, message.to_owned()));
        assert_eq!(
            listing(&|vfs, shard_path| create_simulated_file(vfs, &shard_path.join(".new-torn"))),
            Ok(())
        );
        assert_eq!(
            listing(&|vfs, shard_path| {
                create_simulated_file(vfs, &shard_path.join(format!("{named}.tmp")))
            }),
            refused("block filename suffix is invalid")
        );
        assert_eq!(
            listing(&|vfs, shard_path| {
                create_simulated_file(vfs, &shard_path.join(format!("{}.blk", &named[..63])))
            }),
            refused("block filename hash length mismatch")
        );
        assert_eq!(
            listing(&|vfs, shard_path| {
                create_simulated_file(vfs, &shard_path.join(format!("{foreign}.blk")))
            }),
            refused("block path does not match its identifier")
        );
        assert_eq!(
            listing(&|vfs, shard_path| {
                vfs.create_dir(&shard_path.join(format!("{named}.blk"))).unwrap()
            }),
            refused("block path does not match its identifier")
        );
    }

    #[cfg(unix)]
    #[test]
    fn block_listing_refuses_a_symbolic_link_named_like_a_block() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let payloads = payloads_in_shard(0x81, 2);
        let target = store.path(store.put(&payloads[0]).unwrap());
        let link = store.path(BlockId::for_payload(&payloads[1]));
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let error = store.list_block_ids(2).unwrap_err();
        assert_eq!(
            (error.kind(), error.to_string()),
            (
                io::ErrorKind::InvalidData,
                "VFS path may not be a symbolic link".to_owned()
            )
        );
    }

    #[test]
    fn temporary_file_scan_reaches_a_candidate_past_any_store_wide_total() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        for payload in payloads_in_shard(0x00, 2 * RETIRED_LISTING_TOTAL_AT_TEST_SCALE) {
            store.put_loose(&payload).unwrap();
        }
        store.put_loose(&payloads_in_shard(0xff, 1)[0]).unwrap();
        let temporary = store
            .root
            .join("ff")
            .join(temporary_block_file_name(Uuid::from_bytes([0x3c; 16])));
        create_simulated_file(&vfs, &temporary);
        let plan = store.plan_temporary_file().unwrap();
        assert_eq!(plan.candidate.map(|candidate| candidate.path), Some(temporary));
        // Shard 00's 32 entries, then the temporary, which sorts first in ff.
        assert_eq!(plan.scanned_entries, 2 * RETIRED_LISTING_TOTAL_AT_TEST_SCALE as u64 + 1);
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
        assert!(store.any_relinked([id]).unwrap());
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
        assert!(store.any_relinked([id]).unwrap(), "first duplicate completed fresh verification before the batch failed");
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
        // Room for exactly one ten-byte payload and its entry overhead.
        store
            .set_cache_capacity_bytes(16 + BLOCK_PAYLOAD_CACHE_ENTRY_OVERHEAD_BYTES)
            .unwrap();
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
    fn traversal_reads_reuse_cached_payloads_without_admission_or_recency() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        // Room for exactly two ten-byte payloads and their entry overhead.
        store
            .set_cache_capacity_bytes(2 * (16 + BLOCK_PAYLOAD_CACHE_ENTRY_OVERHEAD_BYTES))
            .unwrap();
        let ids = [b"0123456789", b"abcdefghij", b"klmnopqrst", b"uvwxyz0123"]
            .map(|payload| store.put(payload).unwrap());
        let disk_reads = || {
            vfs.trace().unwrap().iter().filter(|operation| **operation == Operation::Read).count()
        };
        let cached = |id| store.cache.read().unwrap().peek_shared(id).is_some();
        assert_eq!(store.get(ids[0]).unwrap(), b"0123456789");
        assert_eq!(store.get(ids[1]).unwrap(), b"abcdefghij");
        let reads = disk_reads();
        // A cached copy serves the traversal without another disk read.
        assert_eq!(store.get_traversal(ids[0]).unwrap(), b"0123456789");
        assert_eq!(disk_reads(), reads);
        // Verified disk reads are returned but never admitted.
        assert_eq!(store.get_traversal(ids[2]).unwrap(), b"klmnopqrst");
        assert_eq!(store.get_traversal(ids[2]).unwrap(), b"klmnopqrst");
        assert_eq!(disk_reads(), reads + 2);
        assert!(cached(ids[0]) && cached(ids[1]) && !cached(ids[2]));
        // The traversal hit left `ids[0]` least recently used.
        assert_eq!(store.get(ids[3]).unwrap(), b"uvwxyz0123");
        assert!(!cached(ids[0]) && cached(ids[1]) && cached(ids[3]));
        let (staged, pending) = store.put_deferred(b"staged-only").unwrap();
        assert!(pending);
        let reads = disk_reads();
        assert_eq!(store.get_traversal(staged).unwrap(), b"staged-only");
        assert_eq!(disk_reads(), reads);
        assert!(!cached(staged));
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
        let count = manifest.segments.iter().filter(|entry| entry.payload_index_block_id.is_some()).count()
            .checked_add(manifest.manifest_page_ids.len())
            .ok_or_else(|| invalid_data("history metadata count overflow"))?;
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
        index_ids.extend(manifest.manifest_page_ids.iter().copied());
        index_ids.extend(manifest.segments.iter().filter_map(|entry| entry.payload_index_block_id));
        Ok(Self { payload_root_id: manifest.payload_index_root_id, index_ids })
    }
    #[cfg(test)]
    pub(crate) fn resident_bytes(&self) -> u64 {
        (std::mem::size_of::<Self>() + self.index_ids.capacity() * std::mem::size_of::<BlockId>()) as u64
    }
}
