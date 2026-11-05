//! Immutable payload-segment framing, bounded index admission, and random reads.
//!
//! This module owns physical pack files; `payload_manifest` and `CONTROL` own
//! membership publication. Normal engine open never scans this directory.

use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};

use uuid::Uuid;

use crate::block::{BlockId, MAX_BLOCK_BYTES};
use crate::generated_tofudb_ir::{
    MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED, MAX_PAYLOAD_SEGMENT_CACHE_RECENCY_RECORDS,
    MAX_PAYLOAD_SEGMENT_CACHE_SEGMENTS, MAX_PAYLOAD_SEGMENT_FILTER_BYTES,
    MIN_PAYLOAD_SEGMENT_FILTER_BITS, PAYLOAD_SEGMENT_CACHE_BYTES,
    PAYLOAD_SEGMENT_FILTER_BITS_PER_BLOCK, PAYLOAD_SEGMENT_FILTER_HASH_ROUNDS,
};
use crate::vfs::{
    sync_all_barrier, sync_directory_barrier, DirectoryEntry, FileKind, OpenRequest, RealVfs, Vfs,
    VfsFile,
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
/// Negative witnesses cover a full catalog plus segments verified for a
/// publication that has not installed yet, so an in-flight publication never
/// displaces a catalogued witness. Every catalog install trims them back to
/// the installed catalog.
const MAX_MEMBERSHIP_WITNESSES: usize = 2 * crate::payload_manifest::MAX_PAYLOAD_SEGMENTS;
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
    // Negative witnesses outlive full-index eviction and cover the whole
    // installed catalog: every packed segment spans the content-ID space, so
    // a segment without a witness costs a complete index read on each cold
    // miss. Bounded by `MAX_MEMBERSHIP_WITNESSES`, not by the index byte
    // budget; catalog installation forgets dropped ones.
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
        // An explicit zero budget disables all segment caching, witnesses too.
        if self.budget_bytes == 0 {
            return;
        }
        let bytes = segment.membership_filter.allocated_bytes();
        while self.memberships.len() >= MAX_MEMBERSHIP_WITNESSES {
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

    /// Keep only witnesses of the installed catalog. Dropped segments can
    /// never be read through the catalog again.
    fn retain_memberships(&mut self, referenced: &HashSet<Uuid>) {
        self.memberships.retain(|segment_id, _| referenced.contains(segment_id));
        let memberships = &self.memberships;
        self.membership_order.retain(|segment_id| memberships.contains_key(segment_id));
        self.membership_bytes = memberships
            .values()
            .map(|membership| membership.filter.allocated_bytes())
            .sum();
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

    /// A resident verified index without refreshing its recency: one
    /// whole-store pass must not reorder the read path's working set.
    fn peek(&self, segment_id: Uuid) -> Option<Arc<PayloadSegment>> {
        self.entries.get(&segment_id).map(|(segment, _)| Arc::clone(segment))
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
        if bytes > self.budget_bytes {
            return;
        }
        if let Some((existing, _)) = self.entries.remove(&segment_id) {
            self.resident_bytes -= Self::entry_bytes(&existing);
        }
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.resident_bytes += bytes;
        #[cfg(test)]
        read_path_counters::note_index_admission();
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
        while self.resident_bytes > self.budget_bytes
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

/// Idle segment read handles kept open at once. Catalogued segments are
/// sealed and content-addressed, so one handle serves every later point
/// read; reopening the file per block multiplies network- and
/// FUSE-filesystem round trips (each `open` plus its `statx` is a remote
/// metadata round trip of roughly 1-2 ms there, and a live 20k-segment
/// catalog made those round trips the daemon's dominant blocking I/O). The
/// floor keeps desktop descriptor budgets (macOS 256) safe beside the
/// existing working set; above it the bound follows the process descriptor
/// limit, never memory, and handles hold no payload bytes.
const MIN_OPEN_SEGMENT_READ_HANDLES: usize = 32;
const MAX_OPEN_SEGMENT_READ_HANDLES: usize = 4096;
/// Idle handles may hold at most this fraction of the soft descriptor limit.
const OPEN_SEGMENT_READ_HANDLE_DESCRIPTOR_DIVISOR: u64 = 16;
/// Recently retired IDs whose in-flight leases must not be re-cached.
const RETIRED_READ_HANDLE_IDS: usize = 1024;

fn open_segment_read_handle_capacity() -> usize {
    read_handle_capacity_for_descriptor_limit(soft_descriptor_limit())
}

fn read_handle_capacity_for_descriptor_limit(limit: Option<u64>) -> usize {
    let Some(limit) = limit else {
        return MIN_OPEN_SEGMENT_READ_HANDLES;
    };
    usize::try_from(limit / OPEN_SEGMENT_READ_HANDLE_DESCRIPTOR_DIVISOR)
        .unwrap_or(usize::MAX)
        .clamp(MIN_OPEN_SEGMENT_READ_HANDLES, MAX_OPEN_SEGMENT_READ_HANDLES)
}

#[cfg(unix)]
fn soft_descriptor_limit() -> Option<u64> {
    let mut limit = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
    // The kernel fills the caller-owned structure only on success.
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return None;
    }
    if limit.rlim_cur == libc::RLIM_INFINITY {
        return Some(u64::MAX);
    }
    u64::try_from(limit.rlim_cur).ok()
}

#[cfg(not(unix))]
fn soft_descriptor_limit() -> Option<u64> {
    None
}

/// LRU of open segment read handles, keyed by segment ID. Segment IDs are
/// fresh per creation and bytes are immutable per ID, so a cached handle can
/// never observe different content; retirement evicts by ID. Recency uses
/// ticks with lazy deletion so a lease/return pair stays O(1) at thousands
/// of handles.
struct ReadHandleCache {
    handles: HashMap<Uuid, (Box<dyn VfsFile>, u64)>,
    recency: VecDeque<(Uuid, u64)>,
    tick: u64,
    capacity: usize,
    // A reader may hold a leased handle while its segment is removed; the
    // late return must close it instead of pinning an unlinked file.
    retired: HashSet<Uuid>,
    retired_order: VecDeque<Uuid>,
}

impl ReadHandleCache {
    fn new(capacity: usize) -> Self {
        Self {
            handles: HashMap::new(),
            recency: VecDeque::new(),
            tick: 0,
            capacity: capacity.max(1),
            retired: HashSet::new(),
            retired_order: VecDeque::new(),
        }
    }

    fn take(&mut self, segment_id: Uuid) -> Option<Box<dyn VfsFile>> {
        self.handles.remove(&segment_id).map(|(handle, _)| handle)
    }

    fn put(&mut self, segment_id: Uuid, handle: Box<dyn VfsFile>) {
        if self.retired.contains(&segment_id) {
            return;
        }
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.handles.insert(segment_id, (handle, tick));
        if self.recency.len() >= self.capacity.saturating_mul(4).max(64) {
            let handles = &self.handles;
            self.recency.retain(|(id, recorded)| {
                handles.get(id).is_some_and(|(_, current)| current == recorded)
            });
        }
        self.recency.push_back((segment_id, tick));
        while self.handles.len() > self.capacity {
            let Some((candidate, recorded)) = self.recency.pop_front() else {
                break;
            };
            if self
                .handles
                .get(&candidate)
                .is_some_and(|(_, current)| *current == recorded)
            {
                self.handles.remove(&candidate);
            }
        }
    }

    fn remove(&mut self, segment_id: Uuid) {
        self.handles.remove(&segment_id);
        if self.retired.insert(segment_id) {
            self.retired_order.push_back(segment_id);
            while self.retired_order.len() > RETIRED_READ_HANDLE_IDS {
                if let Some(oldest) = self.retired_order.pop_front() {
                    self.retired.remove(&oldest);
                }
            }
        }
    }

    fn retain(&mut self, keep: impl Fn(&Uuid) -> bool) {
        self.handles.retain(|segment_id, _| keep(segment_id));
        if self.handles.is_empty() {
            self.recency.clear();
        }
    }

    #[cfg(test)]
    fn set_capacity(&mut self, capacity: usize) {
        self.capacity = capacity.max(1);
        while self.handles.len() > self.capacity {
            let Some((candidate, recorded)) = self.recency.pop_front() else {
                break;
            };
            if self
                .handles
                .get(&candidate)
                .is_some_and(|(_, current)| *current == recorded)
            {
                self.handles.remove(&candidate);
            }
        }
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
            read_handles: Mutex::new(ReadHandleCache::new(open_segment_read_handle_capacity())),
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
            read_handles: Mutex::new(ReadHandleCache::new(open_segment_read_handle_capacity())),
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
        // Idle handles follow the descriptor-derived bound; leased handles are
        // bounded by the admitted request/maintenance workers and close on
        // every error path.
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

    /// Close idle handles of segments the installed catalog no longer
    /// references: a manifest replacement may be followed by orphan GC that
    /// unlinks them outside `remove_segment`. Handles of still-referenced
    /// segments name the same immutable bytes and stay warm.
    pub(crate) fn retain_read_handles(&self, referenced: &HashSet<Uuid>) {
        if let Ok(mut handles) = self.read_handles.lock() {
            handles.retain(|segment_id| referenced.contains(segment_id));
        }
    }

    #[cfg(test)]
    pub(crate) fn set_read_handle_capacity(&self, capacity: usize) {
        self.read_handles.lock().unwrap().set_capacity(capacity);
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

    /// Verifying open for GC admission, compaction, packing and integrity
    /// tooling: the path is re-checked and the on-disk framing re-verified on
    /// every call. An idle cached handle for the same immutable ID saves the
    /// open round trip; a freshly opened handle is not cached, so whole-catalog
    /// maintenance scans never evict the read path's hot handles.
    pub fn open_segment(&self, segment_id: Uuid) -> io::Result<PayloadSegment> {
        self.open_verified_segment(segment_id, SegmentHandleUse::Verification)
    }

    fn open_verified_segment(
        &self,
        segment_id: Uuid,
        purpose: SegmentHandleUse,
    ) -> io::Result<PayloadSegment> {
        let (path, metadata, entries) = self.read_verified_index(segment_id, purpose)?;
        let membership_filter = SegmentMembershipFilter::from_entries(&entries);
        Ok(PayloadSegment {
            path,
            vfs: Arc::clone(&self.vfs),
            metadata,
            entries,
            membership_filter,
        })
    }

    /// Reads and fully verifies one segment's framing and index; see
    /// `open_segment` for the path check and handle-cache participation.
    fn read_verified_index(
        &self,
        segment_id: Uuid,
        purpose: SegmentHandleUse,
    ) -> io::Result<(PathBuf, PayloadSegmentMetadata, Vec<IndexEntry>)> {
        let path = self.path(segment_id);
        let leased = self
            .read_handles
            .lock()
            .map_err(|_| io::Error::other("segment read-handle lock was poisoned"))?
            .take(segment_id);
        let reused = leased.is_some();
        if !(reused && purpose == SegmentHandleUse::PointRead)
            && self.vfs.metadata(&path)? != FileKind::File
        {
            return Err(invalid_data("payload segment is not a regular file"));
        }
        let mut file = match leased {
            Some(handle) => handle,
            None => open_read(self.vfs.as_ref(), &path)?,
        };
        let (metadata, entries) = verify_segment_framing(segment_id, file.as_mut())?;
        #[cfg(test)]
        read_path_counters::note_index_read(entries.len() * INDEX_ENTRY_BYTES);
        if reused || purpose == SegmentHandleUse::PointRead {
            self.read_handles
                .lock()
                .map_err(|_| io::Error::other("segment read-handle lock was poisoned"))?
                .put(segment_id, file);
        }
        Ok((path, metadata, entries))
    }
}

/// Why a verified segment open runs; decides handle-cache participation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SegmentHandleUse {
    /// Maintenance and integrity paths: re-check the path every time and
    /// never let a scan displace the read path's hot handles.
    Verification,
    /// The verified-index cache miss of a point read, immediately followed by
    /// a payload fetch through the same handle.
    PointRead,
}

fn verify_segment_framing(
    segment_id: Uuid,
    file: &mut dyn VfsFile,
) -> io::Result<(PayloadSegmentMetadata, Vec<IndexEntry>)> {
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
    Ok((
        PayloadSegmentMetadata {
            segment_id,
            block_count,
            payload_bytes,
            file_bytes,
        },
        entries,
    ))
}

impl PayloadSegmentStore {
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

    /// Candidates not excluded by a retained negative witness, in their
    /// original order, under one read-lock acquisition. Unknown segments stay
    /// candidates; a witness that disagrees with its reference is an error.
    pub(crate) fn segments_that_may_contain(
        &self,
        references: &[crate::payload_manifest::PayloadSegmentReference],
        block_id: BlockId,
    ) -> io::Result<Vec<crate::payload_manifest::PayloadSegmentReference>> {
        let cache = self.open_cache.read()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?;
        #[cfg(test)]
        read_path_counters::note_witness_checks(references.len());
        let mut candidates = Vec::new();
        for reference in references {
            if cache.may_contain(reference, block_id)? {
                candidates.push(*reference);
            }
        }
        Ok(candidates)
    }

    /// Forget witnesses of segments outside the installed catalog.
    pub(crate) fn retain_segment_memberships(&self, referenced: &HashSet<Uuid>) -> io::Result<()> {
        self.open_cache.write()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .retain_memberships(referenced);
        Ok(())
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
        let segment = Arc::new(self.open_verified_segment(segment_id, SegmentHandleUse::PointRead)?);
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
        if let Some(segment_id) = candidate.segment_id {
            if let Ok(mut handles) = self.read_handles.lock() {
                handles.remove(segment_id);
            }
        }
        self.vfs.remove_file(&path)?;
        sync_directory_barrier(self.vfs.as_ref(), &self.root)?;
        Ok(file_bytes)
    }

    /// Every orphan in the payload directory, for the online collector's
    /// two-observation confirmation. Unlike the offline round's single
    /// [`Self::plan_orphan_file`] candidate, this observation runs outside
    /// the authority mutex: a file that vanishes while being sized is
    /// skipped, and the removal hold re-validates every reference before
    /// [`Self::unlink_orphan_file`]. The whole directory is observed (its
    /// listing is the only thing held), and each candidate is still
    /// classified and sized by its own stat and open. `before_entry` runs
    /// ahead of each entry's stat and open, so a caller can stop between
    /// file operations.
    pub(crate) fn observe_orphan_files(
        &self,
        referenced_segment_ids: &BTreeSet<Uuid>,
        skip_temporary: bool,
        mut before_entry: impl FnMut() -> io::Result<()>,
    ) -> io::Result<Vec<PayloadSegmentOrphanCandidate>> {
        let listed = self.vfs.read_directory_entries(&self.root)?;
        let mut candidates = Vec::new();
        for DirectoryEntry { path, .. } in &listed {
            before_entry()?;
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
            if (is_temporary && skip_temporary)
                || segment_id.is_some_and(|id| referenced_segment_ids.contains(&id))
            {
                continue;
            }
            if let Some(file_bytes) = self.orphan_file_bytes(path)? {
                candidates.push(PayloadSegmentOrphanCandidate {
                    file_name: file_name.to_owned(),
                    segment_id,
                    file_bytes,
                });
            }
        }
        Ok(candidates)
    }

    /// Size of one orphan file, or `None` when it no longer exists.
    fn orphan_file_bytes(&self, path: &Path) -> io::Result<Option<u64>> {
        match self.vfs.metadata(path) {
            Ok(FileKind::File) => {}
            Ok(FileKind::Directory) => {
                return Err(invalid_data("payload segment orphan is not a regular file"));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        }
        let file_bytes = match open_read(self.vfs.as_ref(), path) {
            Ok(file) => file.len()?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if file_bytes > MAX_SEGMENT_FILE_BYTES {
            return Err(invalid_data("payload segment orphan exceeds the file bound"));
        }
        Ok(Some(file_bytes))
    }

    /// Unlinks one observed orphan whose name and size are unchanged, or
    /// returns `None` when it vanished or changed. The caller re-validated
    /// every reference under its authority hold; one [`Self::sync_root`]
    /// barrier then makes a whole batch of unlinks durable.
    pub(crate) fn unlink_orphan_file(
        &self,
        candidate: &PayloadSegmentOrphanCandidate,
    ) -> io::Result<Option<u64>> {
        let path = self.root.join(&candidate.file_name);
        if path.parent() != Some(self.root.as_path())
            || (parse_complete_segment_name(&candidate.file_name)? != candidate.segment_id)
            || (candidate.segment_id.is_none()
                && !parse_temporary_segment_name(&candidate.file_name)?)
        {
            return Err(invalid_data("payload segment orphan identity changed"));
        }
        if self.orphan_file_bytes(&path)? != Some(candidate.file_bytes) {
            return Ok(None);
        }
        if let Some(segment_id) = candidate.segment_id {
            self.open_cache
                .write()
                .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
                .remove(segment_id);
            if let Ok(mut handles) = self.read_handles.lock() {
                handles.remove(segment_id);
            }
        }
        match self.vfs.remove_file(&path) {
            Ok(()) => Ok(Some(candidate.file_bytes)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Durability barrier for the unlinks made by [`Self::unlink_orphan_file`].
    pub(crate) fn sync_root(&self) -> io::Result<()> {
        sync_directory_barrier(self.vfs.as_ref(), &self.root)
    }

    fn path(&self, segment_id: Uuid) -> PathBuf {
        self.root.join(format!("{}.pseg", segment_id.simple()))
    }
}

/// Packed [`CatalogLocationTable`] entry: the leading block-ID bits, then the
/// segment's catalog ordinal and the entry's index position. The ordinal and
/// position widths are the catalog and segment-format bounds; the ID prefix
/// keeps the remaining 36 bits, so sorting entries sorts them by prefix.
const LOCATION_POSITION_BITS: u32 = 16;
const LOCATION_ORDINAL_BITS: u32 = 12;
const LOCATION_PREFIX_MASK: u64 = u64::MAX << (LOCATION_ORDINAL_BITS + LOCATION_POSITION_BITS);
const _: () = assert!(MAX_SEGMENT_BLOCKS <= 1 << LOCATION_POSITION_BITS);
const _: () = assert!(crate::payload_manifest::MAX_PAYLOAD_SEGMENTS <= 1 << LOCATION_ORDINAL_BITS);

/// Exact block locations of one payload catalog for a single whole-store pass
/// (docs/TOFUDB_BLOCK_READ_PATH.md, "Catalog location table for whole-store
/// passes"). One 8-byte entry per catalogued block, sorted by ID prefix, so a
/// lookup is a binary search whose cost does not depend on the segment count.
/// An entry is only a position: the reader confirms the full ID against the
/// segment index and the payload by CRC32C and content hash, so a prefix
/// collision or a stale entry costs reads, never wrong bytes. Segments whose
/// index could not be verified are absent; their blocks take the exact path.
pub(crate) struct CatalogLocationTable {
    segments: Vec<crate::payload_manifest::PayloadSegmentReference>,
    entries: Vec<u64>,
    /// Start of each leading-ID-byte run in `entries`, then the end.
    byte_starts: Vec<usize>,
    unindexed_segments: usize,
}

impl CatalogLocationTable {
    fn prefix(block_id: BlockId) -> u64 {
        u64::from_be_bytes(block_id.0[..8].try_into().unwrap()) & LOCATION_PREFIX_MASK
    }

    /// Every catalogued position whose ID prefix matches `block_id`.
    pub(crate) fn candidates(
        &self,
        block_id: BlockId,
    ) -> impl Iterator<Item = (&crate::payload_manifest::PayloadSegmentReference, u32)> + '_ {
        let prefix = Self::prefix(block_id);
        let leading = usize::from(block_id.0[0]);
        let run = &self.entries[self.byte_starts[leading]..self.byte_starts[leading + 1]];
        let start = run.partition_point(|entry| entry & LOCATION_PREFIX_MASK < prefix);
        run[start..]
            .iter()
            .take_while(move |entry| *entry & LOCATION_PREFIX_MASK == prefix)
            .map(|entry| {
                let ordinal = (entry >> LOCATION_POSITION_BITS) as usize & ((1 << LOCATION_ORDINAL_BITS) - 1);
                let position = (entry & ((1 << LOCATION_POSITION_BITS) - 1)) as u32;
                (&self.segments[ordinal], position)
            })
    }

    pub(crate) fn located_blocks(&self) -> usize {
        self.entries.len()
    }

    pub(crate) fn unindexed_segments(&self) -> usize {
        self.unindexed_segments
    }

    pub(crate) fn resident_bytes(&self) -> usize {
        self.entries.capacity() * std::mem::size_of::<u64>()
            + self.byte_starts.capacity() * std::mem::size_of::<usize>()
            + self.segments.capacity()
                * std::mem::size_of::<crate::payload_manifest::PayloadSegmentReference>()
    }

    /// Points every entry with `block_id`'s prefix at the next position of
    /// its segment, as a damaged table would.
    #[cfg(test)]
    pub(crate) fn misplace_for_test(&mut self, block_id: BlockId) -> usize {
        let prefix = Self::prefix(block_id);
        let mut misplaced = 0;
        for entry in self.entries.iter_mut().filter(|entry| **entry & LOCATION_PREFIX_MASK == prefix) {
            let ordinal = (*entry >> LOCATION_POSITION_BITS) as usize & ((1 << LOCATION_ORDINAL_BITS) - 1);
            let count = u64::from(self.segments[ordinal].block_count);
            let position = *entry & ((1 << LOCATION_POSITION_BITS) - 1);
            *entry = (*entry & !((1 << LOCATION_POSITION_BITS) - 1)) | ((position + 1) % count);
            misplaced += 1;
        }
        misplaced
    }
}

impl PayloadSegmentStore {
    /// Streams the verified index of every segment of `catalog` once into a
    /// [`CatalogLocationTable`]. An index resident in the verified-index cache
    /// is reused without refreshing its recency; any other index is read and
    /// fully verified like `open_segment`, and admitted to none of the index,
    /// witness or handle caches. A segment whose index cannot be read,
    /// verified or matched to its reference is left out of the table: the
    /// exact catalog path serves its blocks and reports that same failure if
    /// a traversal needs one of them. `check` runs before each segment and
    /// between sort steps; its error aborts the build.
    pub(crate) fn build_location_table(
        &self,
        catalog: &[crate::payload_manifest::PayloadSegmentReference],
        mut check: impl FnMut() -> io::Result<()>,
    ) -> io::Result<CatalogLocationTable> {
        if catalog.len() > 1 << LOCATION_ORDINAL_BITS {
            return Err(invalid_data("payload catalog exceeds its segment bound"));
        }
        let capacity = catalog.iter().map(|reference| reference.block_count as usize).sum();
        let mut entries = Vec::with_capacity(capacity);
        let mut unindexed_segments = 0;
        for (ordinal, reference) in catalog.iter().enumerate() {
            check()?;
            let indexed = self.with_verified_index(reference, |index| {
                entries.extend(index.iter().enumerate().map(|(position, entry)| {
                    CatalogLocationTable::prefix(entry.block_id)
                        | (ordinal as u64) << LOCATION_POSITION_BITS
                        | position as u64
                }));
            });
            if indexed.is_err() {
                unindexed_segments += 1;
            }
        }
        let byte_starts = sort_locations(&mut entries, &mut check)?;
        Ok(CatalogLocationTable {
            segments: catalog.to_vec(),
            entries,
            byte_starts,
            unindexed_segments,
        })
    }

    /// Reads `block_id` from the index position a [`CatalogLocationTable`]
    /// attributed to `reference`, without `open_segment_cached`'s whole-index
    /// read or cache admission. The entry comes from a resident verified index
    /// (recency untouched) or from one positioned read of the on-disk entry,
    /// bounded to the payload region; the payload passes CRC32C and the
    /// content hash. `None` means the position holds another block (an ID
    /// prefix collision). An error is no verdict about the block: the caller
    /// resolves it through the exact catalog path.
    pub(crate) fn read_located(
        &self,
        reference: &crate::payload_manifest::PayloadSegmentReference,
        position: u32,
        block_id: BlockId,
    ) -> io::Result<Option<Vec<u8>>> {
        #[cfg(test)]
        read_path_counters::note_location_candidate();
        if position >= reference.block_count {
            return Err(invalid_data("payload segment location exceeds its index"));
        }
        let mut leased = None;
        let entry = match self.resident_index(reference)? {
            Some(segment) => segment.entries[position as usize],
            None => {
                let file = leased.insert(self.lease_read_handle(reference.segment_id)?);
                read_index_entry(file.as_mut(), reference, position)?
            }
        };
        if entry.block_id != block_id {
            if let Some(file) = leased {
                self.return_read_handle(reference.segment_id, file)?;
            }
            return Ok(None);
        }
        let mut file = match leased {
            Some(file) => file,
            None => self.lease_read_handle(reference.segment_id)?,
        };
        let mut payload = vec![0_u8; entry.payload_len as usize];
        file.read_exact_at(entry.payload_offset, &mut payload)?;
        let payload = verify_entry_payload(entry, block_id, payload)?;
        self.return_read_handle(reference.segment_id, file)?;
        Ok(Some(payload))
    }

    fn with_verified_index<R>(
        &self,
        reference: &crate::payload_manifest::PayloadSegmentReference,
        visit: impl FnOnce(&[IndexEntry]) -> R,
    ) -> io::Result<R> {
        if let Some(segment) = self.resident_index(reference)? {
            return Ok(visit(&segment.entries));
        }
        let (_, metadata, entries) =
            self.read_verified_index(reference.segment_id, SegmentHandleUse::Verification)?;
        if !index_matches_reference(&metadata, &entries, reference) {
            return Err(invalid_data("payload segment differs from its manifest witness"));
        }
        Ok(visit(&entries))
    }

    /// The resident verified index of `reference`'s segment, recency
    /// untouched. One that disagrees with the reference is an error, exactly
    /// as `validate_open_segment` reports it on the exact path.
    fn resident_index(
        &self,
        reference: &crate::payload_manifest::PayloadSegmentReference,
    ) -> io::Result<Option<Arc<PayloadSegment>>> {
        let resident = self
            .open_cache
            .read()
            .map_err(|_| io::Error::other("open segment cache lock was poisoned"))?
            .peek(reference.segment_id);
        match resident {
            Some(segment) if !index_matches_reference(&segment.metadata, &segment.entries, reference) => {
                Err(invalid_data("payload segment differs from its manifest witness"))
            }
            resident => Ok(resident),
        }
    }

    /// Leases a catalogued segment's idle read handle or opens a new one. A
    /// leased handle closes on every error path; `return_read_handle` makes
    /// it idle again under the descriptor-derived bound, as point reads do.
    fn lease_read_handle(&self, segment_id: Uuid) -> io::Result<Box<dyn VfsFile>> {
        let cached = self
            .read_handles
            .lock()
            .map_err(|_| io::Error::other("segment read-handle lock was poisoned"))?
            .take(segment_id);
        match cached {
            Some(handle) => Ok(handle),
            None => open_read(self.vfs.as_ref(), &self.path(segment_id)),
        }
    }

    fn return_read_handle(&self, segment_id: Uuid, handle: Box<dyn VfsFile>) -> io::Result<()> {
        self.read_handles
            .lock()
            .map_err(|_| io::Error::other("segment read-handle lock was poisoned"))?
            .put(segment_id, handle);
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn resident_index_ids_for_test(&self) -> BTreeSet<Uuid> {
        self.open_cache.read().unwrap().entries.keys().copied().collect()
    }
}

/// Sorts packed locations in place and returns where each leading ID byte's
/// run starts. A counting pass and an in-place (American flag) partition by
/// the leading byte precede 256 independent run sorts, so `check` runs between
/// bounded steps rather than only after one catalog-wide sort.
fn sort_locations(
    entries: &mut [u64],
    check: &mut impl FnMut() -> io::Result<()>,
) -> io::Result<Vec<usize>> {
    let mut starts = vec![0_usize; 257];
    for entry in entries.iter() {
        starts[(entry >> 56) as usize + 1] += 1;
    }
    for byte in 0..256 {
        starts[byte + 1] += starts[byte];
    }
    let mut next = starts.clone();
    for byte in 0..256 {
        check()?;
        while next[byte] < starts[byte + 1] {
            let target = (entries[next[byte]] >> 56) as usize;
            if target == byte {
                next[byte] += 1;
            } else {
                entries.swap(next[byte], next[target]);
                next[target] += 1;
            }
        }
    }
    for byte in 0..256 {
        check()?;
        entries[starts[byte]..starts[byte + 1]].sort_unstable();
    }
    Ok(starts)
}

fn index_matches_reference(
    metadata: &PayloadSegmentMetadata,
    entries: &[IndexEntry],
    reference: &crate::payload_manifest::PayloadSegmentReference,
) -> bool {
    metadata.segment_id == reference.segment_id
        && metadata.block_count == reference.block_count
        && metadata.payload_bytes == reference.payload_bytes
        && metadata.file_bytes == reference.file_bytes
        && entries.first().map(|entry| entry.block_id) == Some(reference.first_block_id)
        && entries.last().map(|entry| entry.block_id) == Some(reference.last_block_id)
}

/// One index entry read at its position. The whole index was verified when
/// the position was learned; this re-read is bounded to the payload region so
/// later damage can address nothing outside it, and the payload checks reject
/// any other change.
fn read_index_entry(
    file: &mut dyn VfsFile,
    reference: &crate::payload_manifest::PayloadSegmentReference,
    position: u32,
) -> io::Result<IndexEntry> {
    let index_offset = HEADER_BYTES as u64 + reference.payload_bytes;
    let mut encoded = [0_u8; INDEX_ENTRY_BYTES];
    file.read_exact_at(index_offset + u64::from(position) * INDEX_ENTRY_BYTES as u64, &mut encoded)?;
    let entry = decode_index_entry(&encoded);
    if entry.payload_len as usize > MAX_BLOCK_BYTES
        || entry.payload_offset < HEADER_BYTES as u64
        || entry
            .payload_offset
            .checked_add(u64::from(entry.payload_len))
            .is_none_or(|end| end > index_offset)
    {
        return Err(invalid_data("payload segment index entry has an invalid extent"));
    }
    Ok(entry)
}

/// Per-thread read-path work counters for executable cost specifications.
#[cfg(test)]
pub(crate) mod read_path_counters {
    use std::cell::Cell;

    thread_local! {
        static WITNESS_CHECKS: Cell<u64> = const { Cell::new(0) };
        static LOCATION_CANDIDATES: Cell<u64> = const { Cell::new(0) };
        static INDEX_READS: Cell<u64> = const { Cell::new(0) };
        static INDEX_READ_BYTES: Cell<u64> = const { Cell::new(0) };
        static INDEX_ADMISSIONS: Cell<u64> = const { Cell::new(0) };
    }

    pub(crate) fn note_witness_checks(checks: usize) {
        WITNESS_CHECKS.with(|count| count.set(count.get() + checks as u64));
    }

    pub(crate) fn note_location_candidate() {
        LOCATION_CANDIDATES.with(|count| count.set(count.get() + 1));
    }

    pub(crate) fn note_index_read(index_bytes: usize) {
        INDEX_READS.with(|count| count.set(count.get() + 1));
        INDEX_READ_BYTES.with(|count| count.set(count.get() + index_bytes as u64));
    }

    pub(crate) fn note_index_admission() {
        INDEX_ADMISSIONS.with(|count| count.set(count.get() + 1));
    }

    /// (whole segment indexes read and verified, their index bytes,
    /// verified-index cache admissions) since the last call.
    pub(crate) fn take_index_work() -> (u64, u64, u64) {
        (
            INDEX_READS.with(|count| count.replace(0)),
            INDEX_READ_BYTES.with(|count| count.replace(0)),
            INDEX_ADMISSIONS.with(|count| count.replace(0)),
        )
    }

    /// (segment witness checks, location-table candidates) since the last call.
    pub(crate) fn take() -> (u64, u64) {
        (
            WITNESS_CHECKS.with(|count| count.replace(0)),
            LOCATION_CANDIDATES.with(|count| count.replace(0)),
        )
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

fn decode_index_entry(encoded: &[u8; INDEX_ENTRY_BYTES]) -> IndexEntry {
    IndexEntry {
        block_id: BlockId(encoded[..32].try_into().unwrap()),
        payload_offset: u64::from_le_bytes(encoded[32..40].try_into().unwrap()),
        payload_len: u32::from_le_bytes(encoded[40..44].try_into().unwrap()),
        payload_crc32c: u32::from_le_bytes(encoded[44..48].try_into().unwrap()),
    }
}

fn decode_index(index: &[u8], payload_bytes: u64) -> io::Result<Vec<IndexEntry>> {
    let mut entries = Vec::with_capacity(index.len() / INDEX_ENTRY_BYTES);
    let mut expected_offset = HEADER_BYTES as u64;
    for encoded in index.as_chunks::<INDEX_ENTRY_BYTES>().0 {
        let entry = decode_index_entry(encoded);
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

    fn count(vfs: &DeterministicVfs, wanted: Operation) -> usize {
        vfs.trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == wanted)
            .count()
    }

    #[test]
    fn read_handle_capacity_follows_the_descriptor_limit() {
        assert_eq!(read_handle_capacity_for_descriptor_limit(None), MIN_OPEN_SEGMENT_READ_HANDLES);
        // macOS's default 256 keeps the historical desktop bound.
        assert_eq!(read_handle_capacity_for_descriptor_limit(Some(256)), MIN_OPEN_SEGMENT_READ_HANDLES);
        assert_eq!(read_handle_capacity_for_descriptor_limit(Some(1024)), 64);
        assert_eq!(read_handle_capacity_for_descriptor_limit(Some(1 << 20)), MAX_OPEN_SEGMENT_READ_HANDLES);
        assert_eq!(read_handle_capacity_for_descriptor_limit(Some(u64::MAX)), MAX_OPEN_SEGMENT_READ_HANDLES);
        let capacity = open_segment_read_handle_capacity();
        assert!((MIN_OPEN_SEGMENT_READ_HANDLES..=MAX_OPEN_SEGMENT_READ_HANDLES).contains(&capacity));
    }

    #[test]
    fn point_read_miss_reuses_the_verifying_open_handle() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        // Creation re-verifies through a fresh handle without caching it.
        assert!(!store.read_handles.lock().unwrap().handles.contains_key(&segment_id));
        vfs.arm_fault(None).unwrap();
        let segment = store.open_segment_cached(segment_id).unwrap();
        for id in &ids {
            assert_eq!(store.get_with_handle(&segment, *id).unwrap().as_ref(), Some(&payloads[id]));
        }
        assert_eq!(count(&vfs, Operation::Open), 1, "the index verification handle serves the payload reads");
        // One path check before the cold open plus the handle's length probe.
        assert_eq!(count(&vfs, Operation::Metadata), 2);
    }

    #[test]
    fn verification_open_reuses_idle_handles_but_never_caches_scans() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        let segment = store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        store.get_with_handle(&segment, ids[0]).unwrap().unwrap();
        vfs.arm_fault(None).unwrap();
        let reverified = store.open_segment(segment_id).unwrap();
        assert_eq!(reverified.metadata(), segment.metadata());
        assert_eq!(count(&vfs, Operation::Open), 0, "an idle handle names the same immutable bytes");
        assert_eq!(
            count(&vfs, Operation::Metadata),
            2,
            "verification still re-checks the path, then measures the handle"
        );
        assert!(count(&vfs, Operation::Read) >= 3, "framing is re-read and re-verified");
        assert!(store.read_handles.lock().unwrap().handles.contains_key(&segment_id));

        let payload = b"scanned-only".to_vec();
        let scanned_id = Uuid::from_bytes([9; 16]);
        store
            .create(scanned_id, vec![BlockId::for_payload(&payload)], |_| Ok(payload.clone()))
            .unwrap();
        store.open_segment(scanned_id).unwrap();
        assert!(
            !store.read_handles.lock().unwrap().handles.contains_key(&scanned_id),
            "a maintenance scan must not displace the read path's hot handles"
        );
    }

    #[test]
    fn verification_open_detects_a_corrupt_file_behind_a_cached_handle() {
        let directory = tempfile::tempdir().unwrap();
        let store = PayloadSegmentStore::initialize(directory.path()).unwrap();
        let (segment_id, ids, payloads) = fixture();
        let segment = store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        store.get_with_handle(&segment, ids[0]).unwrap().unwrap();
        // Rewrite in place so the cached handle's inode observes the damage.
        let path = store.path(segment_id);
        let mut bytes = std::fs::read(&path).unwrap();
        *bytes.last_mut().unwrap() ^= 0xff;
        let mut file = OpenOptions::new().write(true).open(&path).unwrap();
        file.write_all(&bytes).unwrap();
        file.sync_all().unwrap();
        match store.open_segment(segment_id) {
            Err(error) => assert_eq!(error.kind(), io::ErrorKind::InvalidData),
            Ok(_) => panic!("a damaged trailer must fail verification through a reused handle"),
        }
    }

    #[test]
    fn retired_segment_lease_is_closed_instead_of_recached() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (segment_id, ids, payloads) = fixture();
        let segment = store
            .create(segment_id, ids.clone(), |id| Ok(payloads[&id].clone()))
            .unwrap();
        store.get_with_handle(&segment, ids[0]).unwrap().unwrap();
        let leased = store.read_handles.lock().unwrap().take(segment_id).unwrap();
        store.remove_segment(segment_id).unwrap();
        store.read_handles.lock().unwrap().put(segment_id, leased);
        assert!(
            !store.read_handles.lock().unwrap().handles.contains_key(&segment_id),
            "a late lease return must not pin an unlinked segment"
        );
    }

    #[test]
    fn catalog_replacement_keeps_referenced_handles_warm() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let mut segments = Vec::new();
        for index in 0..2_u8 {
            let payload = format!("catalog-retain-{index}").into_bytes();
            let id = BlockId::for_payload(&payload);
            let segment_id = Uuid::from_bytes([index + 40; 16]);
            let segment = store
                .create(segment_id, vec![id], |_| Ok(payload.clone()))
                .unwrap();
            store.get_with_handle(&segment, id).unwrap().unwrap();
            segments.push(segment_id);
        }
        let referenced: HashSet<Uuid> = [segments[1]].into_iter().collect();
        store.retain_read_handles(&referenced);
        let handles = store.read_handles.lock().unwrap();
        assert!(!handles.handles.contains_key(&segments[0]));
        assert!(handles.handles.contains_key(&segments[1]));
    }

    #[test]
    fn read_handles_stay_bounded_and_evict_lru() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_read_handle_capacity(MIN_OPEN_SEGMENT_READ_HANDLES);
        let total = MIN_OPEN_SEGMENT_READ_HANDLES + 1;
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
        assert_eq!(handles.handles.len(), MIN_OPEN_SEGMENT_READ_HANDLES);
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
    fn largest_indexes_and_their_filters_share_the_index_byte_budget() {
        let largest = cache_test_segment(1, MAX_SEGMENT_BLOCKS);
        assert!(largest.membership_filter.allocated_bytes() <= MAX_PAYLOAD_SEGMENT_FILTER_BYTES);
        assert!(largest.block_ids().all(|id| largest.contains(id)));
        let bytes = OpenSegmentCache::entry_bytes(&largest);
        assert!(bytes > largest.entries.len() * INDEX_ENTRY_BYTES);
        let admitted = OPEN_SEGMENT_CACHE_BYTES / bytes;
        let mut cache = OpenSegmentCache::new();
        cache.insert(largest);
        for id in 2..=admitted as u128 + 2 {
            cache.insert(cache_test_segment(id, MAX_SEGMENT_BLOCKS));
            assert!(cache.resident_bytes <= OPEN_SEGMENT_CACHE_BYTES);
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
            assert!(cache.resident_bytes <= cache.budget_bytes);
        }
        vfs.arm_fault(None).unwrap();
        for id in ids {
            assert_eq!(store.segments_that_may_contain(&[reference], id).unwrap(), vec![reference]);
        }
        assert!(store.segments_that_may_contain(&[reference], BlockId::for_payload(b"never written")).unwrap().is_empty());
        assert!(vfs.trace().unwrap().is_empty());
        let mut wrong = reference;
        wrong.file_bytes += 1;
        assert!(store.segments_that_may_contain(&[wrong], BlockId::for_payload(b"never written")).is_err());
        let mut cache = store.open_cache.write().unwrap();
        cache.remove(segment_id);
        assert!(cache.may_contain(&reference, BlockId::for_payload(b"never written")).unwrap());
        assert!(!cache.memberships.contains_key(&segment_id));
        assert!(!cache.membership_order.contains(&segment_id));
    }

    /// Negative witnesses cover the installed catalog whatever the index byte
    /// budget: a segment without one costs a full index read on every cold
    /// miss. A count bound with room for in-flight publications, not the
    /// budget, bounds them; installing a catalog forgets the segments it no
    /// longer references.
    #[test]
    fn membership_witnesses_cover_the_catalog_outside_the_index_byte_budget() {
        let mut cache = OpenSegmentCache::new();
        cache.budget_bytes = 4096;
        for id in 1..100 {
            cache.insert(cache_test_segment(id, 128));
            assert!(cache.resident_bytes <= cache.budget_bytes);
            assert_eq!(cache.memberships.len(), id as usize);
            assert_eq!(cache.memberships.len(), cache.membership_order.len());
        }
        assert!(cache.entries.len() < 99, "full indexes still obey the byte budget");
        let filter_bytes = cache_test_segment(1, 128).membership_filter.allocated_bytes();
        assert_eq!(cache.membership_bytes, 99 * filter_bytes);
        let referenced: HashSet<Uuid> = [Uuid::from_u128(7), Uuid::from_u128(99)].into_iter().collect();
        cache.retain_memberships(&referenced);
        assert_eq!(cache.memberships.len(), 2);
        assert_eq!(cache.membership_order.len(), 2);
        assert_eq!(cache.membership_bytes, 2 * filter_bytes);
        assert!(cache.memberships.contains_key(&Uuid::from_u128(7)));
        // A full catalog plus a publication in flight keeps every witness.
        let catalog = crate::payload_manifest::MAX_PAYLOAD_SEGMENTS as u128;
        for id in 1000..1000 + catalog {
            cache.remember_membership(&cache_test_segment(id, 1));
        }
        assert!(cache.memberships.contains_key(&Uuid::from_u128(7)));
        assert!(cache.memberships.contains_key(&Uuid::from_u128(99)));
        for id in 1000 + catalog..1000 + MAX_MEMBERSHIP_WITNESSES as u128 {
            cache.remember_membership(&cache_test_segment(id, 1));
        }
        assert_eq!(cache.memberships.len(), MAX_MEMBERSHIP_WITNESSES);
        assert!(!cache.memberships.contains_key(&Uuid::from_u128(7)), "the count bound evicts the oldest");
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
    fn location_sort_equals_a_full_sort_and_records_leading_byte_runs() {
        let mut state = 0x2545_f491_4f6c_dd1d_u64;
        let mut entries: Vec<u64> = (0..10_000)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                state
            })
            .collect();
        let unsorted = entries.clone();
        let mut expected = entries.clone();
        expected.sort_unstable();
        let mut checks = 0;
        let starts = sort_locations(&mut entries, &mut || {
            checks += 1;
            Ok(())
        })
        .unwrap();
        assert_eq!(entries, expected);
        assert_eq!(checks, 512, "one check per partition and per run sort");
        assert_eq!((starts[0], starts[256]), (0, entries.len()));
        for byte in 0..256 {
            assert!(entries[starts[byte]..starts[byte + 1]].iter().all(|entry| (entry >> 56) as usize == byte));
        }
        let mut calls = 0;
        let stopped = sort_locations(&mut unsorted.clone(), &mut || {
            calls += 1;
            if calls == 300 { Err(io::Error::from(io::ErrorKind::WouldBlock)) } else { Ok(()) }
        });
        assert_eq!(stopped.unwrap_err().kind(), io::ErrorKind::WouldBlock);
    }

    /// Entries are keyed by a 36-bit ID prefix, so two blocks can share one.
    /// Every candidate is confirmed by the full ID in the segment index; a
    /// position holding another block yields nothing, never its bytes.
    #[test]
    fn location_candidates_resolve_prefix_collisions_by_the_full_index_id() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let (first_id, ids, payloads) = fixture();
        let first = store.create(first_id, ids.clone(), |id| Ok(payloads[&id].clone())).unwrap();
        let neighbour_payload = b"colliding-neighbour".to_vec();
        let neighbour = BlockId::for_payload(&neighbour_payload);
        let second_id = Uuid::from_bytes([8; 16]);
        let second = store.create(second_id, vec![neighbour], |_| Ok(neighbour_payload.clone())).unwrap();
        let reference = |segment: &PayloadSegment| {
            crate::payload_manifest::PayloadSegmentReference::new(
                segment.metadata(), segment.first_block_id(), segment.last_block_id()).unwrap()
        };
        let target = ids[1];
        let target_position = first.entries.iter().position(|entry| entry.block_id == target).unwrap() as u64;
        // The neighbour's entry is filed under the target's prefix and sorts first.
        let prefix = CatalogLocationTable::prefix(target);
        let mut entries = vec![prefix | 1 << LOCATION_POSITION_BITS | target_position, prefix];
        let byte_starts = sort_locations(&mut entries, &mut || Ok(())).unwrap();
        let table = CatalogLocationTable {
            segments: vec![reference(&second), reference(&first)],
            entries,
            byte_starts,
            unindexed_segments: 0,
        };
        let candidates: Vec<_> = table
            .candidates(target)
            .map(|(reference, position)| (reference.segment_id, position))
            .collect();
        assert_eq!(candidates, vec![(second_id, 0), (first_id, target_position as u32)]);
        read_path_counters::take();
        assert_eq!(store.read_located(&reference(&second), 0, target).unwrap(), None);
        assert_eq!(
            store.read_located(&reference(&first), target_position as u32, target).unwrap(),
            Some(payloads[&target].clone())
        );
        assert_eq!(read_path_counters::take(), (0, 2));
        let absent = BlockId::for_payload(b"absent from both segments");
        assert_eq!(store.read_located(&reference(&first), target_position as u32, absent).unwrap(), None);
        assert_eq!(
            store.read_located(&reference(&first), 3, target).unwrap_err().kind(),
            io::ErrorKind::InvalidData,
            "a position outside the index is an error, never a guess"
        );
        assert!(store.resident_index_ids_for_test().is_empty(), "located reads admit no index");
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

/// Paired-volume measurement for the descriptor-derived read-handle cache:
/// run the ignored test with `TOFU_DB_SEGMENT_BENCH_DIR` on the volume under
/// study (and the same binary built from the previous source for the
/// baseline). Figures live in `docs/TOFUDB_FASTPATH.md`.
#[cfg(test)]
mod segment_read_bench {
    use super::*;
    use std::time::Instant;

    fn env_usize(name: &str, default: usize) -> usize {
        std::env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
    }

    #[test]
    #[ignore]
    fn segment_point_read_bench() {
        let Some(root) = std::env::var_os("TOFU_DB_SEGMENT_BENCH_DIR") else { return; };
        let segments = env_usize("TOFU_DB_SEGMENT_BENCH_SEGMENTS", 1024);
        let reads = env_usize("TOFU_DB_SEGMENT_BENCH_READS", 20_000);
        let directory = PathBuf::from(root).join(format!("bench-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&directory).unwrap();
        let mut catalog = Vec::with_capacity(segments);
        {
            let store = PayloadSegmentStore::initialize(&directory).unwrap();
            let mut seed = 0x9e37_79b9_7f4a_7c15_u64;
            for segment_number in 0..segments {
                let payloads: std::collections::BTreeMap<BlockId, Vec<u8>> = (0..64)
                    .map(|block_number| {
                        let mut bytes = vec![0_u8; 4096];
                        for chunk in bytes.chunks_mut(8) {
                            seed ^= seed << 13;
                            seed ^= seed >> 7;
                            seed ^= seed << 17;
                            chunk.copy_from_slice(&seed.to_le_bytes()[..chunk.len()]);
                        }
                        bytes[..8].copy_from_slice(&(segment_number as u64 * 64 + block_number).to_le_bytes());
                        (BlockId::for_payload(&bytes), bytes)
                    })
                    .collect();
                let segment_id = Uuid::new_v4();
                store
                    .create(segment_id, payloads.keys().copied().collect(), |id| Ok(payloads[&id].clone()))
                    .unwrap();
                catalog.push((segment_id, payloads.keys().copied().collect::<Vec<_>>()));
            }
        }
        let store = PayloadSegmentStore::open(&directory).unwrap();
        let mut state = 0x2545_f491_4f6c_dd1d_u64;
        let mut next = || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        let started = Instant::now();
        for _ in 0..reads {
            let (segment_id, ids) = &catalog[(next() % catalog.len() as u64) as usize];
            let id = ids[(next() % ids.len() as u64) as usize];
            let segment = store.open_segment_cached(*segment_id).unwrap();
            assert!(store.get_with_handle(&segment, id).unwrap().is_some());
        }
        let point_reads = started.elapsed();
        let started = Instant::now();
        for _ in 0..2 {
            for (segment_id, _) in &catalog {
                store.open_segment(*segment_id).unwrap();
            }
        }
        let verification = started.elapsed();
        eprintln!(
            "segment_read_bench segments={} reads={} point_read_us={:.1} verification_open_us={:.1}",
            segments,
            reads,
            point_reads.as_secs_f64() * 1e6 / reads as f64,
            verification.as_secs_f64() * 1e6 / (2 * catalog.len()) as f64,
        );
        drop(store);
        std::fs::remove_dir_all(&directory).unwrap();
    }
}
