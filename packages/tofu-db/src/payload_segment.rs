//! Immutable payload-segment framing, bounded index admission, and random reads.
//!
//! This module owns physical pack files; `payload_manifest` and `CONTROL` own
//! membership publication. Normal engine open never scans this directory.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};

use uuid::Uuid;

use crate::block::{BlockId, MAX_BLOCK_BYTES};
use crate::generated_tofudb_ir::{
    MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED, MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS,
    MAX_PAYLOAD_SEGMENT_CACHE_SEGMENTS, MAX_PAYLOAD_SEGMENT_FILTER_BYTES,
    MIN_PAYLOAD_SEGMENT_FILTER_BITS, PAYLOAD_SEGMENT_CACHE_BYTES,
    PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR, PAYLOAD_SEGMENT_FILTER_BITS_PER_BLOCK, PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS,
};
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
// Bounded syscall coalescing, not a storage page-size target. Large blocks
// bypass this buffer; framing, checksums and all durability barriers are unchanged.
const SEGMENT_WRITE_BUFFER_BYTES: usize = 256 * 1024;
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

/// Reconstructible negative membership filter over one verified index.
/// Full content hashes provide independent bit positions; positive probes
/// still use the exact sorted index. It neither stores nor trusts file data.
#[derive(Clone)]
struct SegmentMembershipFilter {
    words: Vec<u64>,
    mask: usize,
}

impl SegmentMembershipFilter {
    fn from_entries(entries: &[IndexEntry]) -> Self {
        let bits = (entries.len() * PAYLOAD_SEGMENT_FILTER_BITS_PER_BLOCK)
            .max(MIN_PAYLOAD_SEGMENT_FILTER_BITS)
            .next_power_of_two();
        assert!(bits / 8 <= MAX_PAYLOAD_SEGMENT_FILTER_BYTES);
        let mut filter = Self {
            words: vec![0; bits / 64],
            mask: bits - 1,
        };
        for entry in entries {
            for round in 0..PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS {
                let bit = filter.position(entry.block_id, round);
                filter.words[bit / 64] |= 1 << (bit % 64);
            }
        }
        filter
    }

    fn position(&self, id: BlockId, round: usize) -> usize {
        let offset = round * 8;
        (u64::from_le_bytes(id.0[offset..offset + 8].try_into().unwrap()) as usize) & self.mask
    }

    fn may_contain(&self, id: BlockId) -> bool {
        (0..PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS).all(|round| {
            let bit = self.position(id, round);
            self.words[bit / 64] & (1 << (bit % 64)) != 0
        })
    }

    fn allocated_bytes(&self) -> usize {
        self.words.capacity() * std::mem::size_of::<u64>()
    }
}

pub struct PayloadSegment {
    path: PathBuf,
    vfs: Arc<dyn Vfs>,
    metadata: PayloadSegmentMetadata,
    entries: Vec<IndexEntry>,
    membership_filter: SegmentMembershipFilter,
}

impl PayloadSegment {
    pub fn metadata(&self) -> PayloadSegmentMetadata {
        self.metadata
    }

    pub fn contains(&self, block_id: BlockId) -> bool {
        self.entry_for_block(block_id).is_some()
    }

    pub(crate) fn entry_for_block(&self, block_id: BlockId) -> Option<IndexEntry> {
        if !self.membership_filter.may_contain(block_id) {
            return None;
        }
        self.entries
            .binary_search_by_key(&block_id, |entry| entry.block_id)
            .ok()
            .map(|position| self.entries[position])
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
        let Some(entry) = self.entry_for_block(block_id) else {
            return Ok(None);
        };
        let mut payload = vec![0_u8; entry.payload_len as usize];
        let mut file = open_read(self.vfs.as_ref(), &self.path)?;
        file.read_exact_at(entry.payload_offset, &mut payload)?;
        verify_entry_payload(entry, block_id, payload).map(Some)
    }
}

/// Every payload fetch re-verifies its own CRC32C and content hash; cached
/// handles and cached indexes never skip per-read integrity.
fn verify_entry_payload(
    entry: IndexEntry,
    block_id: BlockId,
    payload: Vec<u8>,
) -> io::Result<Vec<u8>> {
    if crc32c::crc32c(&payload) != entry.payload_crc32c
        || BlockId::for_payload(&payload) != block_id
    {
        return Err(invalid_data("payload segment block integrity mismatch"));
    }
    Ok(payload)
}

/// Default index-byte budget for the open-segment cache. Cached entries are
/// verified (header, trailer layout, index digest) at open time; every
/// payload fetch still re-verifies its own CRC32C and content hash, so the
/// cache only elides repeated index reads and digest recomputation. The
/// bound keeps parsed indexes inside the personal resource budget.
const OPEN_SEGMENT_CACHE_BYTES: usize = PAYLOAD_SEGMENT_CACHE_BYTES;
/// Hard ceiling for the open-segment cache byte budget, shared by the
/// launch probe's derived value and the explicit operator override.
pub(crate) const MAX_SEGMENT_CACHE_BUDGET_BYTES: usize = 16 * 1024 * 1024 * 1024;
/// Operator override for the open-segment cache byte budget, clamped to
/// 16 GiB. The generated default keeps parsed indexes inside the personal
/// resource budget; an operator on a host with real headroom (bulk
/// migration, benchmarks) widens it explicitly. Unparsable values fall back
/// to the default; a parsed 0 disables the cache.
pub(crate) fn segment_cache_budget_override(raw: Option<&str>) -> Option<usize> {
    raw.and_then(|value| value.parse::<usize>().ok())
        .map(|value| value.min(MAX_SEGMENT_CACHE_BUDGET_BYTES))
}
fn open_segment_cache_bytes() -> usize {
    segment_cache_budget_override(
        std::env::var("TOFU_DB_PAYLOAD_SEGMENT_CACHE_BYTES")
            .ok()
            .as_deref(),
    )
    .unwrap_or(OPEN_SEGMENT_CACHE_BYTES)
}
/// Count bound beside the byte budget: an all-tiny-segments catalog would
/// otherwise hold one entry per catalog slot with negligible byte pressure.
const OPEN_SEGMENT_CACHE_MAX_SEGMENTS: usize = MAX_PAYLOAD_SEGMENT_CACHE_SEGMENTS;
const _: () =
    assert!(OPEN_SEGMENT_CACHE_MAX_SEGMENTS <= crate::payload_manifest::MAX_PAYLOAD_SEGMENTS);
const _: () = assert!(MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS > OPEN_SEGMENT_CACHE_MAX_SEGMENTS);
const _: () =
    assert!(PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS > 0 && PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS <= 4);

/// Byte-bounded LRU over verified open segments, keyed by segment ID.
/// Segments are immutable once sealed and IDs are fresh per creation, so a
/// hit always describes the exact bytes the ID names; `remove_segment`
/// evicts so a retired file is never resurrected through the cache.
struct VerifiedSegmentMembership {
    metadata: PayloadSegmentMetadata,
    first: BlockId,
    last: BlockId,
    filter: SegmentMembershipFilter,
}

struct OpenSegmentCache {
    entries: HashMap<Uuid, (Arc<PayloadSegment>, u64)>,
    lru: VecDeque<(Uuid, u64)>,
    resident_bytes: usize,
    tick: u64,
    budget_bytes: usize,
    // Retain cheap negative witnesses independently of full-index eviction.
    // One quarter of the existing byte budget, never an extra cache budget.
    memberships: HashMap<Uuid, VerifiedSegmentMembership>,
    membership_order: VecDeque<Uuid>,
    membership_bytes: usize,
}

impl OpenSegmentCache {
    fn new() -> Self {
        Self {
            entries: HashMap::new(),
            lru: VecDeque::new(),
            resident_bytes: 0,
            tick: 0,
            budget_bytes: open_segment_cache_bytes(),
            memberships: HashMap::new(),
            membership_order: VecDeque::new(),
            membership_bytes: 0,
        }
    }

    fn remember_membership(&mut self, segment: &PayloadSegment) {
        let id = segment.metadata.segment_id;
        if self.memberships.contains_key(&id) {
            return;
        }
        let bytes = segment.membership_filter.allocated_bytes();
        let budget = self.budget_bytes / PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR;
        if bytes > budget {
            return;
        }
        while self.membership_bytes + bytes > budget
            || self.memberships.len() >= OPEN_SEGMENT_CACHE_MAX_SEGMENTS
        {
            let Some(oldest) = self.membership_order.pop_front() else { break; };
            if let Some(old) = self.memberships.remove(&oldest) {
                self.membership_bytes -= old.filter.allocated_bytes();
            }
        }
        self.memberships.insert(id, VerifiedSegmentMembership {
            metadata: segment.metadata,
            first: segment.first_block_id(),
            last: segment.last_block_id(),
            filter: segment.membership_filter.clone(),
        });
        self.membership_order.push_back(id);
        self.membership_bytes += bytes;
    }

    fn may_contain(
        &self,
        reference: &crate::payload_manifest::PayloadSegmentReference,
        block_id: BlockId,
    ) -> io::Result<bool> {
        let Some(cached) = self.memberships.get(&reference.segment_id) else {
            return Ok(true); // Unknown is never evidence of absence.
        };
        if cached.metadata.block_count != reference.block_count
            || cached.metadata.payload_bytes != reference.payload_bytes
            || cached.metadata.file_bytes != reference.file_bytes
            || cached.first != reference.first_block_id
            || cached.last != reference.last_block_id
        {
            return Err(invalid_data("payload segment differs from its manifest witness"));
        }
        Ok(cached.filter.may_contain(block_id))
    }

    fn entry_bytes(segment: &PayloadSegment) -> usize {
        segment.entries.capacity() * std::mem::size_of::<IndexEntry>()
            + segment.membership_filter.allocated_bytes()
    }

    fn note_recency(&mut self, segment_id: Uuid, tick: u64) {
        // Compact before pushing, so VecDeque never allocates beyond the
        // declared record bound. Current ticks preserve exact LRU order.
        if self.lru.len() >= MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS {
            self.lru.retain(|(id, previous_tick)| {
                self.entries
                    .get(id)
                    .is_some_and(|(_, current_tick)| current_tick == previous_tick)
            });
        }
        self.lru.push_back((segment_id, tick));
    }

    fn get(&mut self, segment_id: Uuid) -> Option<Arc<PayloadSegment>> {
        let (segment, _) = self.entries.get(&segment_id)?;
        let segment = Arc::clone(segment);
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.entries
            .insert(segment_id, (Arc::clone(&segment), tick));
        self.note_recency(segment_id, tick);
        Some(segment)
    }

    fn insert(&mut self, segment: Arc<PayloadSegment>) {
        self.remember_membership(&segment);
        let segment_id = segment.metadata.segment_id;
        let bytes = Self::entry_bytes(&segment);
        if bytes > self.budget_bytes - self.budget_bytes / PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR {
            return;
        }
        if let Some((existing, _)) = self.entries.remove(&segment_id) {
            self.resident_bytes -= Self::entry_bytes(&existing);
        }
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.resident_bytes += bytes;
        self.entries.insert(segment_id, (segment, tick));
        self.note_recency(segment_id, tick);
        self.evict_to_budget();
    }

    fn remove(&mut self, segment_id: Uuid) {
        if let Some(old) = self.memberships.remove(&segment_id) {
            self.membership_bytes -= old.filter.allocated_bytes();
            self.membership_order.retain(|id| *id != segment_id);
        }
        if let Some((segment, _)) = self.entries.remove(&segment_id) {
            self.resident_bytes -= Self::entry_bytes(&segment);
        }
    }

    fn evict_to_budget(&mut self) {
        while self.resident_bytes > self.budget_bytes - self.budget_bytes / PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR
            || self.entries.len() > OPEN_SEGMENT_CACHE_MAX_SEGMENTS
        {
            let Some((candidate, candidate_tick)) = self.lru.pop_front() else {
                break;
            };
            match self.entries.get(&candidate) {
                Some((_, tick)) if *tick == candidate_tick => {
                    if let Some((segment, _)) = self.entries.remove(&candidate) {
                        self.resident_bytes -= Self::entry_bytes(&segment);
                    }
                }
                _ => continue,
            }
        }
    }
}

/// At most this many segment read handles stay open at once. Catalogued
/// segments are sealed and content-addressed, so one handle serves every
/// later point read; reopening the file per block multiplies network- and
/// FUSE-filesystem round trips (WAL recovery replays thousands of block
/// fetches, and each `open` is a remote metadata round trip there). The
/// bound is small and independent of the index cache: desktop process file
/// descriptor budgets (macOS 256) must absorb it beside the existing
/// working set, and handles hold no payload bytes.
const MAX_OPEN_SEGMENT_READ_HANDLES: usize = 32;

/// Tiny LRU of open segment read handles, keyed by segment ID. Segment IDs
/// are fresh per creation and bytes are immutable per ID, so a cached
/// handle can never observe different content; retirement evicts by ID.
struct ReadHandleCache {
    handles: HashMap<Uuid, Box<dyn VfsFile>>,
    lru: VecDeque<Uuid>,
}

impl ReadHandleCache {
    fn new() -> Self {
        Self {
            handles: HashMap::new(),
            lru: VecDeque::new(),
        }
    }

    fn take(&mut self, segment_id: Uuid) -> Option<Box<dyn VfsFile>> {
        self.lru.retain(|id| *id != segment_id);
        self.handles.remove(&segment_id)
    }

    fn put(&mut self, segment_id: Uuid, handle: Box<dyn VfsFile>) {
        self.lru.retain(|id| *id != segment_id);
        self.handles.insert(segment_id, handle);
        self.lru.push_back(segment_id);
        while self.handles.len() > MAX_OPEN_SEGMENT_READ_HANDLES {
            if let Some(evicted) = self.lru.pop_front() {
                self.handles.remove(&evicted);
            }
        }
    }

    fn remove(&mut self, segment_id: Uuid) {
        self.handles.remove(&segment_id);
        self.lru.retain(|id| *id != segment_id);
    }

    fn clear(&mut self) {
        self.handles.clear();
        self.lru.clear();
    }
}

/// Deferred retirement of manifest-unreferenced segments. A catalog
/// publication retires a segment the moment the new manifest installs;
/// readers holding a pre-publication candidate snapshot can still race the
/// unlink (the changed-view retry converges the race, but every converged
/// hit is still a wasted open on a network mount). Parking happens only
/// after the new manifest is durable and installed, so a time-floor park
/// makes every manifest version any live reader can hold resolvable by
/// construction. The set is in-memory only: a restart drops it, and the
/// unreferenced files become ordinary orphan-GC candidates — safe, because
/// no stale-snapshot reader survives a restart.
#[derive(Default)]
struct DeferredRetirement {
    entries: HashMap<Uuid, DeferredRetirementEntry>,
}

#[derive(Clone, Copy)]
struct DeferredRetirementEntry {
    parked_at: std::time::Instant,
    file_bytes: u64,
}

/// Hard bound on parked retirements: one entry per adoption round at the
/// observed round rate, so a stalled sweeper can never grow the set (or its
/// disk footprint) without limit. Overflow evicts the oldest entry back to
/// the caller, which physically reclaims it immediately — exactly the
/// pre-deferral behavior for that one file.
pub(crate) const MAX_DEFERRED_RETIREMENTS: usize = 64;

pub(crate) fn segment_retirement_delay() -> std::time::Duration {
    std::env::var("TOFU_DB_SEGMENT_RETIREMENT_DELAY_MS")
        .ok()
        .and_then(|raw| raw.parse::<u64>().ok())
        .map(std::time::Duration::from_millis)
        .unwrap_or(std::time::Duration::from_secs(60))
}

impl DeferredRetirement {
    /// Parks the segment and returns the oldest parked id when the bound
    /// forced an eviction (the caller physically reclaims that one).
    fn park(&mut self, segment_id: Uuid, file_bytes: u64) -> Option<Uuid> {
        if self.entries.contains_key(&segment_id) {
            return None;
        }
        let mut evicted = None;
        while self.entries.len() >= MAX_DEFERRED_RETIREMENTS {
            let Some(oldest) = self
                .entries
                .iter()
                .min_by_key(|(_, entry)| entry.parked_at)
                .map(|(id, _)| *id)
            else {
                break;
            };
            self.entries.remove(&oldest);
            evicted = Some(oldest);
        }
        self.entries.insert(
            segment_id,
            DeferredRetirementEntry {
                parked_at: std::time::Instant::now(),
                file_bytes,
            },
        );
        evicted
    }

    /// Ids whose files must survive orphan planning although no manifest
    /// references them any more.
    fn protected_ids(&self) -> Vec<Uuid> {
        self.entries.keys().copied().collect()
    }

    /// Ids newly eligible for physical removal: the park already happened
    /// after the manifest that dropped the segment installed, so the time
    /// floor alone bounds how long a pre-install snapshot reader can race.
    fn eligible(&self, delay: std::time::Duration) -> Vec<Uuid> {
        let now = std::time::Instant::now();
        self.entries
            .iter()
            .filter(|(_, entry)| now.duration_since(entry.parked_at) >= delay)
            .map(|(id, _)| *id)
            .collect()
    }

    fn take(&mut self, segment_id: Uuid) -> Option<DeferredRetirementEntry> {
        self.entries.remove(&segment_id)
    }
}

pub struct PayloadSegmentStore {
    root: PathBuf,
    vfs: Arc<dyn Vfs>,
    open_cache: RwLock<OpenSegmentCache>,
    read_handles: Mutex<ReadHandleCache>,
    deferred_retirement: Mutex<DeferredRetirement>,
}

impl PayloadSegmentStore {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("payload-segments");
        vfs.create_dir(&root)?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        Ok(Self {
            root,
            vfs,
            open_cache: RwLock::new(OpenSegmentCache::new()),
            read_handles: Mutex::new(ReadHandleCache::new()),
            deferred_retirement: Mutex::new(DeferredRetirement::default()),
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("payload-segments");
        if vfs.metadata(&root)? != FileKind::Directory {
            return Err(invalid_data("payload segment root is not a real directory"));
        }
        Ok(Self {
            root,
            vfs,
            open_cache: RwLock::new(OpenSegmentCache::new()),
            read_handles: Mutex::new(ReadHandleCache::new()),
            deferred_retirement: Mutex::new(DeferredRetirement::default()),
        })
    }

    /// Point-read through a reusable open handle. Hot paths (WAL recovery,
    /// backup, post-eviction reads) fetch many blocks from a handful of
    /// catalogued segments; per-block `open` calls dominate those paths on
    /// network filesystems. Integrity verification is identical to
    /// `PayloadSegment::get`.
    pub(crate) fn get_with_handle(
        &self,
        segment: &PayloadSegment,
        block_id: BlockId,
    ) -> io::Result<Option<Vec<u8>>> {
        let Some(entry) = segment.entry_for_block(block_id) else {
            return Ok(None);
        };
        // Lease a handle out of the cache. Opening and reading a cold file
        // must not serialize unrelated reads behind a global cache mutex.
        // Idle handles are capped at 32; leased handles are bounded by the
        // admitted request/maintenance workers and close on every error path.
        let cached = self.read_handles.lock()
            .map_err(|_| io::Error::other("segment read-handle lock was poisoned"))?
            .take(segment.metadata.segment_id);
        let mut handle = match cached {
            Some(handle) => handle,
            None => open_read(self.vfs.as_ref(), &segment.path)?,
        };
        let mut payload = vec![0_u8; entry.payload_len as usize];
        handle.read_exact_at(entry.payload_offset, &mut payload)?;
        let payload = verify_entry_payload(entry, block_id, payload)?;
        self.read_handles.lock()
            .map_err(|_| io::Error::other("segment read-handle lock was poisoned"))?
            .put(segment.metadata.segment_id, handle);
        Ok(Some(payload))
    }

    /// Drop every cached handle (segment files may have been retired outside
    /// `remove_segment`, e.g. manifest replacement followed by orphan GC).
    pub(crate) fn close_read_handles(&self) {
        if let Ok(mut handles) = self.read_handles.lock() {
            handles.clear();
        }
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
            let mut write_buffer = Vec::with_capacity(SEGMENT_WRITE_BUFFER_BYTES);
            let mut write_offset = HEADER_BYTES as u64;
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
                if payload.len() > SEGMENT_WRITE_BUFFER_BYTES - write_buffer.len() {
                    if !write_buffer.is_empty() {
                        file.write_all_at(write_offset, &write_buffer)?;
                        write_offset += write_buffer.len() as u64;
                        write_buffer.clear();
                    }
                    if payload.len() > SEGMENT_WRITE_BUFFER_BYTES {
                        file.write_all_at(write_offset, &payload)?;
                        write_offset += payload_len;
                    } else {
                        write_buffer.extend_from_slice(&payload);
                    }
                } else {
                    write_buffer.extend_from_slice(&payload);
                }
                entries.push(IndexEntry {
                    block_id,
                    payload_offset,
                    payload_len: payload.len() as u32,
                    payload_crc32c: crc32c::crc32c(&payload),
                });
            }

            if !write_buffer.is_empty() {
                file.write_all_at(write_offset, &write_buffer)?;
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
        let membership_filter = SegmentMembershipFilter::from_entries(&entries);
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
            membership_filter,
        })
    }

    /// Read-path open with a verified-segment cache. Behaves exactly like
    /// `open_segment` on a miss; a hit replays the previously verified
    /// metadata and index without touching the disk. Payload fetches through
    /// the returned segment still verify CRC32C and content hash per read,
    /// so integrity failures surface identically. GC admission, compaction,
    /// and integrity tooling keep calling `open_segment` so they always
    /// re-verify the on-disk framing.
    pub(crate) fn remember_segment_membership(&self, segment: &PayloadSegment) -> io::Result<()> {
        self.open_cache.write()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .remember_membership(segment);
        Ok(())
    }

    pub(crate) fn cached_segment_may_contain(
        &self,
        reference: &crate::payload_manifest::PayloadSegmentReference,
        block_id: BlockId,
    ) -> io::Result<bool> {
        self.open_cache.read()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .may_contain(reference, block_id)
    }

    /// Probe-derived open-segment cache budget applied at authority open
    /// (`AuthorityDatabase::configure_write_path_budgets`). The new budget
    /// binds from the next admission, exactly like the block payload cache's
    /// `set_cache_capacity_bytes`.
    pub(crate) fn routing_budget_bytes(&self) -> usize {
        self.open_cache.read().map(|cache| cache.budget_bytes / 8).unwrap_or(0)
    }

    pub fn set_open_segment_cache_budget_bytes(&self, budget_bytes: usize) -> io::Result<()> {
        self.open_cache
            .write()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .budget_bytes = budget_bytes;
        Ok(())
    }

    pub fn open_segment_cached(&self, segment_id: Uuid) -> io::Result<Arc<PayloadSegment>> {
        if let Some(segment) = self
            .open_cache
            .write()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .get(segment_id)
        {
            return Ok(segment);
        }
        let segment = Arc::new(self.open_segment(segment_id)?);
        self.open_cache
            .write()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .insert(Arc::clone(&segment));
        Ok(segment)
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
        self.open_cache
            .write()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .remove(segment_id);
        if let Ok(mut handles) = self.read_handles.lock() {
            handles.remove(segment_id);
        }
        let file_bytes = open_read(self.vfs.as_ref(), &path)?.len()?;
        if file_bytes > MAX_SEGMENT_FILE_BYTES {
            return Err(invalid_data("payload segment removal size exceeds bound"));
        }
        self.vfs.remove_file(&path)?;
        sync_directory_barrier(self.vfs.as_ref(), &self.root)?;
        Ok(file_bytes)
    }

    /// Parks a manifest-retired segment instead of unlinking it in the
    /// publication path. Returns the removed byte count the caller's metrics
    /// and size checks expect from `remove_segment`; the physical unlink
    /// happens in `sweep_deferred_retirement`. A zero delay keeps the
    /// pre-deferral behavior byte for byte (immediate removal).
    pub(crate) fn defer_segment_retirement(&self, segment_id: Uuid) -> io::Result<u64> {
        if segment_retirement_delay().is_zero() {
            return self.remove_segment(segment_id);
        }
        let path = self.path(segment_id);
        let file_bytes = match self.vfs.metadata(&path) {
            Ok(FileKind::File) => open_read(self.vfs.as_ref(), &path)?.len()?,
            Ok(FileKind::Directory) => {
                return Err(invalid_data("payload segment path is a directory"));
            }
            Err(error) => return Err(error),
        };
        if file_bytes > MAX_SEGMENT_FILE_BYTES {
            return Err(invalid_data("payload segment removal size exceeds bound"));
        }
        let evicted = self
            .deferred_retirement
            .lock()
            .map_err(|_| io::Error::other("deferred retirement lock was poisoned"))?
            .park(segment_id, file_bytes);
        if let Some(evicted_id) = evicted {
            // The bound forces immediate reclamation of the oldest park,
            // exactly the pre-deferral behavior for that one file. A file
            // already gone out of band is not an error.
            match self.remove_segment(evicted_id) {
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        Ok(file_bytes)
    }

    /// Segment ids parked by [`Self::defer_segment_retirement`]: orphan
    /// planning and orphan removal must treat them as protected, exactly
    /// like unpublished background segments.
    pub(crate) fn deferred_retirement_ids(&self) -> io::Result<Vec<Uuid>> {
        Ok(self
            .deferred_retirement
            .lock()
            .map_err(|_| io::Error::other("deferred retirement lock was poisoned"))?
            .protected_ids())
    }

    /// Deterministic test clock: move existing parks into the past without
    /// relaxing production retirement grace or changing any durable bytes.
    #[cfg(test)]
    pub(crate) fn age_deferred_retirements_for_test(
        &self, age: std::time::Duration,
    ) -> io::Result<usize> {
        let parked_at = std::time::Instant::now().checked_sub(age)
            .ok_or_else(|| invalid_data("test retirement age exceeds clock range"))?;
        let mut registry = self.deferred_retirement.lock()
            .map_err(|_| io::Error::other("deferred retirement lock was poisoned"))?;
        for entry in registry.entries.values_mut() { entry.parked_at = parked_at; }
        Ok(registry.entries.len())
    }

    /// Unlinks every parked segment whose retirement predates the current
    /// manifest install and whose time floor expired. A file that vanished
    /// out of band is simply dropped from the set (its payloads live in the
    /// replacement segment); other errors keep the entry for the next round.
    /// Returns the reclaimed byte total.
    /// Sweeps with an explicit floor: production callers pass the
    /// configured delay, tests pass zero, and neither mutates the
    /// process-wide environment.
    pub(crate) fn sweep_deferred_retirement_with(
        &self,
        delay: std::time::Duration,
    ) -> io::Result<u64> {
        let eligible = self
            .deferred_retirement
            .lock()
            .map_err(|_| io::Error::other("deferred retirement lock was poisoned"))?
            .eligible(delay);
        let mut reclaimed = 0_u64;
        for segment_id in eligible {
            let file_bytes = match self.remove_segment(segment_id) {
                Ok(bytes) => bytes,
                Err(error) if error.kind() == io::ErrorKind::NotFound => self
                    .deferred_retirement
                    .lock()
                    .map_err(|_| io::Error::other("deferred retirement lock was poisoned"))?
                    .take(segment_id)
                    .map(|entry| entry.file_bytes)
                    .unwrap_or(0),
                Err(error) => return Err(error),
            };
            self.deferred_retirement
                .lock()
                .map_err(|_| io::Error::other("deferred retirement lock was poisoned"))?
                .take(segment_id);
            reclaimed = reclaimed
                .checked_add(file_bytes)
                .ok_or_else(|| invalid_data("deferred retirement byte count overflow"))?;
        }
        Ok(reclaimed)
    }

    pub(crate) fn plan_orphan_file(
        &self,
        referenced_segment_ids: &BTreeSet<Uuid>,
        skip_temporary: bool,
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
            // A temporary filename is independent of its output UUID. The
            // engine defers these candidates while a worker owns any build.
            if is_temporary && skip_temporary {
                more_candidates = true;
                continue;
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
    for encoded in index.as_chunks::<INDEX_ENTRY_BYTES>().0 {
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
    fn repeated_cached_reads_keep_recency_metadata_bounded() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        store.open_segment_cached(segment_id).unwrap();
        vfs.arm_fault(None).unwrap();
        for _ in 0..(OPEN_SEGMENT_CACHE_MAX_SEGMENTS * 4) {
            let segment = store.open_segment_cached(segment_id).unwrap();
            assert!(ids.iter().all(|id| segment.contains(*id)));
        }
        assert!(vfs.trace().unwrap().is_empty());
        let cache = store.open_cache.read().unwrap();
        assert_eq!(cache.entries.len(), 1);
        assert!(
            cache.lru.len() <= MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS,
            "{} recency records retained for one cached segment",
            cache.lru.len()
        );
        assert!(cache.lru.capacity() <= MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS);
    }

    #[test]
    fn repeated_reads_reuse_one_open_handle() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        let segment = store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        let count_opens = || {
            vfs.trace()
                .unwrap()
                .iter()
                .filter(|operation| **operation == Operation::Open)
                .count()
        };
        vfs.arm_fault(None).unwrap();
        for _ in 0..8 {
            for id in &ids {
                assert_eq!(
                    store.get_with_handle(&segment, *id).unwrap().as_ref(),
                    Some(&payloads[id])
                );
            }
        }
        assert_eq!(
            count_opens(),
            1,
            "24 reads through one segment must reuse a single open handle"
        );
        // A filter-negative probe still opens nothing.
        assert_eq!(
            store
                .get_with_handle(&segment, BlockId([9; 32]))
                .unwrap(),
            None
        );
        assert_eq!(count_opens(), 1);
    }

    #[test]
    fn read_handles_stay_bounded_and_evict_lru() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let total = MAX_OPEN_SEGMENT_READ_HANDLES + 1;
        let mut segments = Vec::new();
        for index in 0..total {
            let payload = format!("handle-eviction-{index}").into_bytes();
            let id = BlockId::for_payload(&payload);
            let segment_id = Uuid::from_bytes([index as u8 + 1; 16]);
            let segment = store
                .create(segment_id, vec![id], |_| Ok(payload.clone()))
                .unwrap();
            segments.push((segment, id, payload));
        }
        for (segment, id, payload) in &segments {
            assert_eq!(
                store.get_with_handle(segment, *id).unwrap().as_ref(),
                Some(payload)
            );
        }
        let handles = store.read_handles.lock().unwrap();
        assert_eq!(handles.handles.len(), MAX_OPEN_SEGMENT_READ_HANDLES);
        assert!(
            !handles
                .handles
                .contains_key(&segments[0].0.metadata().segment_id),
            "the least recently used handle must be evicted"
        );
        drop(handles);
        // The retained most-recent handle still serves without an open.
        vfs.arm_fault(None).unwrap();
        let last = &segments[total - 1];
        assert_eq!(
            store
                .get_with_handle(&last.0, last.1)
                .unwrap()
                .as_ref(),
            Some(&last.2)
        );
        assert_eq!(
            vfs.trace()
                .unwrap()
                .iter()
                .filter(|operation| **operation == Operation::Open)
                .count(),
            0,
            "a retained handle must not reopen the segment file"
        );
    }

    #[test]
    fn remove_segment_evicts_its_read_handle() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        let segment = store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        store
            .get_with_handle(&segment, ids[0])
            .unwrap()
            .unwrap();
        assert!(store
            .read_handles
            .lock()
            .unwrap()
            .handles
            .contains_key(&segment_id));
        store.remove_segment(segment_id).unwrap();
        assert!(!store
            .read_handles
            .lock()
            .unwrap()
            .handles
            .contains_key(&segment_id));
    }

    #[test]
    fn segment_filter_rejects_misses_without_weakening_exact_membership() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payloads: BTreeMap<_, _> = (0..1024_u32)
            .map(|value| {
                let payload = format!("segment-filter-live-{value}").into_bytes();
                (BlockId::for_payload(&payload), payload)
            })
            .collect();
        let mut segment = store
            .create(
                Uuid::from_bytes([21; 16]),
                payloads.keys().copied().collect(),
                |id| Ok(payloads[&id].clone()),
            )
            .unwrap();
        for (id, payload) in &payloads {
            assert!(segment.contains(*id));
            assert_eq!(segment.get(*id).unwrap().as_ref(), Some(payload));
        }
        let missing: Vec<_> = (0..10_000_u32)
            .map(|value| BlockId::for_payload(format!("segment-filter-missing-{value}").as_bytes()))
            .collect();
        let false_positives = missing
            .iter()
            .filter(|id| segment.membership_filter.may_contain(**id))
            .count();
        assert!(false_positives < missing.len() / 20);
        vfs.arm_fault(None).unwrap();
        for id in &missing {
            assert!(!segment.contains(*id));
            assert!(segment.get(*id).unwrap().is_none());
        }
        assert!(vfs.trace().unwrap().is_empty());
        // Even an entirely saturated filter may only add exact-index work;
        // it must never invent a block or trigger payload I/O for an absent ID.
        segment.membership_filter.words.fill(u64::MAX);
        for id in &missing {
            assert!(!segment.contains(*id));
            assert!(segment.get(*id).unwrap().is_none());
        }
        assert!(vfs.trace().unwrap().is_empty());
    }

    fn cache_test_segment(segment_id: u128, block_count: usize) -> Arc<PayloadSegment> {
        let entries: Vec<_> = (0..block_count)
            .map(|index| {
                let mut bytes = [0_u8; 32];
                bytes[..8].copy_from_slice(&(index as u64).to_be_bytes());
                IndexEntry {
                    block_id: BlockId(bytes),
                    payload_offset: HEADER_BYTES as u64,
                    payload_len: 0,
                    payload_crc32c: 0,
                }
            })
            .collect();
        let membership_filter = SegmentMembershipFilter::from_entries(&entries);
        // Pure cache-policy fixture: no cache operation may read the path.
        Arc::new(PayloadSegment {
            path: PathBuf::from("/cache-policy-only.pseg"),
            vfs: Arc::new(DeterministicVfs::new(None)),
            metadata: PayloadSegmentMetadata {
                segment_id: Uuid::from_u128(segment_id),
                block_count: block_count as u32,
                payload_bytes: 0,
                file_bytes: (HEADER_BYTES + block_count * INDEX_ENTRY_BYTES + TRAILER_BYTES) as u64,
            },
            entries,
            membership_filter,
        })
    }

    #[test]
    fn bounded_recency_preserves_lru_eviction_and_removal() {
        let mut cache = OpenSegmentCache::new();
        for id in 1..=OPEN_SEGMENT_CACHE_MAX_SEGMENTS as u128 {
            cache.insert(cache_test_segment(id, 1));
        }
        let hot = Uuid::from_u128(1);
        for _ in 0..MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS * 4 {
            assert!(cache.get(hot).is_some());
        }
        cache.insert(cache_test_segment(
            OPEN_SEGMENT_CACHE_MAX_SEGMENTS as u128 + 1,
            1,
        ));
        assert!(cache.get(Uuid::from_u128(2)).is_none());
        assert!(cache.get(hot).is_some());
        assert!(cache.get(Uuid::from_u128(3)).is_some());
        assert_eq!(cache.entries.len(), OPEN_SEGMENT_CACHE_MAX_SEGMENTS);
        assert!(cache.lru.len() <= MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS);
        assert!(cache.lru.capacity() <= MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS);
        cache.remove(hot);
        assert!(cache.get(hot).is_none());
    }

    #[test]
    fn largest_indexes_and_filters_share_the_existing_cache_byte_budget() {
        let largest = cache_test_segment(1, MAX_SEGMENT_BLOCKS);
        assert!(largest.membership_filter.allocated_bytes() <= MAX_PAYLOAD_SEGMENT_FILTER_BYTES);
        assert!(largest.block_ids().all(|id| largest.contains(id)));
        let bytes = OpenSegmentCache::entry_bytes(&largest);
        assert!(bytes > largest.entries.len() * INDEX_ENTRY_BYTES);
        let admitted = (OPEN_SEGMENT_CACHE_BYTES - OPEN_SEGMENT_CACHE_BYTES / PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR) / bytes;
        let mut cache = OpenSegmentCache::new();
        cache.insert(largest);
        for id in 2..=admitted as u128 + 2 {
            cache.insert(cache_test_segment(id, MAX_SEGMENT_BLOCKS));
            assert!(cache.resident_bytes + cache.membership_bytes <= OPEN_SEGMENT_CACHE_BYTES);
        }
        assert_eq!(cache.entries.len(), admitted);
        assert!(cache.get(Uuid::from_u128(1)).is_none());
        assert!(cache.get(Uuid::from_u128(admitted as u128 + 2)).is_some());
    }

    #[test]
    fn negative_membership_survives_index_eviction_without_extra_io() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        let segment = store.create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone())).unwrap();
        let reference = crate::payload_manifest::PayloadSegmentReference::new(
            segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap();
        store.open_cache.write().unwrap().budget_bytes = 1024;
        store.open_segment_cached(segment_id).unwrap();
        {
            let mut cache = store.open_cache.write().unwrap();
            // Real LRU pressure evicts full indexes while compact witnesses fit.
            for id in 1000..1020 {
                cache.insert(cache_test_segment(id, 1));
            }
            assert!(cache.get(segment_id).is_none());
            assert!(cache.memberships.contains_key(&segment_id));
            assert!(cache.resident_bytes + cache.membership_bytes <= cache.budget_bytes);
        }
        vfs.arm_fault(None).unwrap();
        for id in ids {
            assert!(store.cached_segment_may_contain(&reference, id).unwrap());
        }
        assert!(!store.cached_segment_may_contain(&reference, BlockId::for_payload(b"never written")).unwrap());
        assert!(vfs.trace().unwrap().is_empty());
        let mut wrong = reference;
        wrong.file_bytes += 1;
        assert!(store.cached_segment_may_contain(&wrong, BlockId::for_payload(b"never written")).is_err());
        let mut cache = store.open_cache.write().unwrap();
        cache.remove(segment_id);
        assert!(cache.may_contain(&reference, BlockId::for_payload(b"never written")).unwrap());
        assert!(!cache.memberships.contains_key(&segment_id));
        assert!(!cache.membership_order.contains(&segment_id));
    }

    #[test]
    fn independent_membership_cache_stays_within_partition_under_pressure() {
        let mut cache = OpenSegmentCache::new();
        cache.budget_bytes = 4096;
        for id in 1..100 {
            cache.insert(cache_test_segment(id, 128));
            assert!(cache.resident_bytes + cache.membership_bytes <= cache.budget_bytes);
            assert!(cache.membership_bytes <= cache.budget_bytes / PAYLOAD_SEGMENT_MEMBERSHIP_BUDGET_DIVISOR);
            assert_eq!(cache.memberships.len(), cache.membership_order.len());
        }
        assert!(!cache.memberships.contains_key(&Uuid::from_u128(1)));
        assert!(cache.memberships.contains_key(&Uuid::from_u128(99)));
    }

    #[test]
    fn coalesced_segment_writes_preserve_exact_bytes_and_bounded_calls() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let payloads: BTreeMap<_, _> = (0..128_u32)
            .map(|value| {
                let mut payload = vec![7; 8192];
                payload[..4].copy_from_slice(&value.to_le_bytes());
                (BlockId::for_payload(&payload), payload)
            })
            .collect();
        vfs.arm_fault(None).unwrap();
        let segment = store
            .create(
                Uuid::from_bytes([19; 16]),
                payloads.keys().copied().collect(),
                |id| Ok(payloads[&id].clone()),
            )
            .unwrap();
        let writes = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|op| **op == Operation::Write)
            .count();
        // Four coalesced payload writes plus two headers, index and trailer,
        // versus 132 writes in the per-block implementation.
        assert_eq!(writes, 8);
        assert_eq!(
            segment.metadata().file_bytes,
            (HEADER_BYTES + 128 * 8192 + 128 * INDEX_ENTRY_BYTES + TRAILER_BYTES) as u64
        );
        drop(segment);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let segment = store.open_segment(Uuid::from_bytes([19; 16])).unwrap();
        for (id, payload) in payloads {
            assert_eq!(segment.get(id).unwrap().unwrap(), payload);
        }
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

        let active = store.plan_orphan_file(&BTreeSet::new(), true).unwrap();
        assert!(active.candidate.is_none());
        assert!(active.more_candidates);
        let plan = store.plan_orphan_file(&BTreeSet::new(), false).unwrap();
        assert_eq!(plan.scanned_files, 1);
        let candidate = plan.candidate.unwrap();
        assert_eq!(candidate.file_name, temporary_name);
        assert_eq!(candidate.segment_id, None);
        assert_eq!(store.remove_orphan_file(&candidate).unwrap(), 15);
        assert!(store
            .plan_orphan_file(&BTreeSet::new(), false)
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
            store.plan_orphan_file(&BTreeSet::new(), false).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn open_segment_cache_budget_is_runtime_adjustable() {
        let directory = tempfile::tempdir().unwrap();
        let store = PayloadSegmentStore::initialize(directory.path()).unwrap();
        let default_budget = store.open_cache.read().unwrap().budget_bytes;
        assert_eq!(default_budget, open_segment_cache_bytes());
        store
            .set_open_segment_cache_budget_bytes(3 * 1024 * 1024 * 1024)
            .unwrap();
        assert_eq!(
            store.open_cache.read().unwrap().budget_bytes,
            3 * 1024 * 1024 * 1024
        );
        // Zero keeps its documented "cache disabled" meaning.
        store.set_open_segment_cache_budget_bytes(0).unwrap();
        assert_eq!(store.open_cache.read().unwrap().budget_bytes, 0);
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
        let (segment_id, _, mut payloads) = fixture();
        // Exercise both buffer flushes and the larger-than-buffer direct path
        // under every error, torn write and lost durability barrier.
        for (byte, length) in [
            (31, 130 * 1024),
            (32, 140 * 1024),
            (33, SEGMENT_WRITE_BUFFER_BYTES + 1),
            (34, 120 * 1024),
        ] {
            let payload = vec![byte; length];
            payloads.insert(BlockId::for_payload(&payload), payload);
        }
        let ids: Vec<_> = payloads.keys().copied().collect();
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

#[cfg(test)]
mod budget_override_tests {
    #[test]
    fn budget_override_parses_clamps_and_falls_back() {
        assert_eq!(super::segment_cache_budget_override(None), None);
        assert_eq!(super::segment_cache_budget_override(Some("not-a-number")), None);
        assert_eq!(super::segment_cache_budget_override(Some("-1")), None);
        assert_eq!(super::segment_cache_budget_override(Some("0")), Some(0));
        assert_eq!(super::segment_cache_budget_override(Some("268435456")), Some(268435456));
        // Beyond the ceiling clamps; overflowing the integer falls back.
        assert_eq!(
            super::segment_cache_budget_override(Some("21474836480")),
            Some(16 * 1024 * 1024 * 1024)
        );
        assert_eq!(super::segment_cache_budget_override(Some("99999999999999999999")), None);
    }
}
