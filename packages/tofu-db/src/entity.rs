//! Owner-scoped MVCC entities on immutable copy-on-write B+Tree pages.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::io;
use std::sync::{Arc, Mutex};

#[path = "entity_query_view.rs"]
mod query_view;
#[cfg(test)]
#[path = "entity_query_view_tests.rs"]
mod query_view_tests;
pub(crate) use query_view::PublishedEntityReads;

use crate::block::BlockId;
use crate::engine::Engine;
use crate::entity_overlay::{EntityOverlayView, OverlayPosition};
use crate::generated_tofudb_ir::{
    MAX_ENTITY_INLINE_VALUE_BYTES, MAX_ENTITY_KEY_BYTES,
    MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES, MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS,
    MAX_ENTITY_RANGE_ROWS,
    MAX_ENTITY_REACHABILITY_FRONTIER, MAX_ENTITY_REACHABILITY_PAGES, MAX_ENTITY_RETIRED_RANGES,
    MAX_ENTITY_ROOT_RANGE_MOUNTS,
    MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS, MAX_PERSISTENT_ENTITY_CAPSULE_RANGES,
    MAX_PERSISTENT_ENTITY_ROOT_PINS, MAX_PERSISTENT_ENTITY_ROOT_PIN_ID_BYTES,
    PERSISTENT_ENTITY_ROOT_PIN_CATALOG_NAMESPACE,
    PERSISTENT_ENTITY_ROOT_PIN_NAMESPACE,
};
use crate::vfs::Vfs;
use uuid::Uuid;

#[path = "entity_page_codec.rs"]
mod page_codec;
use page_codec::LeafFrame;

const PAGE_MAGIC: &[u8; 8] = b"TDBENT01";
const ROOT_MAGIC: &[u8; 8] = b"TDBROOT1";
const ROOT_VERSION: u32 = 2;
const ROOT_DIRECTORY_MAGIC: &[u8; 8] = b"TDBRDIR1";
const ROOT_DIRECTORY_VERSION: u32 = 1;
/// Fresh pages pack to this target. Pages up to PAGE_MAX_BYTES (the original
/// pack target) stay readable and writable so trees written before the target
/// tightened need no migration, and one maximum-size entry always fits.
const PAGE_PACK_TARGET_BYTES: usize = crate::generated_tofudb_ir::ENTITY_PAGE_TARGET_BYTES;
const PAGE_MAX_BYTES: usize = crate::generated_tofudb_ir::MAX_ENTITY_PAGE_BYTES;
const MIN_INTERNAL_SPLIT_CHILDREN: usize =
    crate::generated_tofudb_ir::MIN_ENTITY_INTERNAL_SPLIT_CHILDREN;
const MAX_NAMESPACE_BYTES: usize = 63;
const MAX_TREE_DEPTH: usize = 16;
/// Overlay scan slack: the merge trigger (`merge_overlay_if_needed`) fires
/// at this many pending delta entries, so any single scanned range holds at
/// most this many overlay entries and exact scan coverage always fits.
const OVERLAY_MAX_SCAN_ENTRIES: usize = 12_000;

/// Overlay merge-trigger bounds. The defaults are the measured values
/// (2026-09-19 FUSE stage run: four 8 µs batch applies across 1,575
/// commits, no merge-attributable tail); operator overrides
/// (`TOFU_DB_OVERLAY_BUDGET_BYTES`, `TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES`)
/// are clamped so the watermark always leaves ≥4 MiB of headroom under the
/// 64 MiB active-WAL hard ceiling. Malformed values fall back to the
/// defaults; both are read once at authority open, like the overlay switch.
const OVERLAY_BUDGET_BYTES_DEFAULT: usize = 8 * 1024 * 1024;
pub(crate) const OVERLAY_BUDGET_BYTES_MIN: usize = 1024 * 1024;
pub(crate) const OVERLAY_BUDGET_BYTES_MAX: usize = 256 * 1024 * 1024;
const OVERLAY_WAL_WATERMARK_BYTES_DEFAULT: u64 = 48 * 1024 * 1024;
pub(crate) const OVERLAY_WAL_WATERMARK_BYTES_MIN: u64 = 8 * 1024 * 1024;
pub(crate) const OVERLAY_WAL_WATERMARK_BYTES_MAX: u64 = 60 * 1024 * 1024;

pub(crate) fn parse_overlay_budget_bytes(value: Option<String>) -> usize {
    value
        .and_then(|value| value.parse::<usize>().ok())
        .map(|value| value.clamp(OVERLAY_BUDGET_BYTES_MIN, OVERLAY_BUDGET_BYTES_MAX))
        .unwrap_or(OVERLAY_BUDGET_BYTES_DEFAULT)
}

pub(crate) fn parse_overlay_wal_watermark_bytes(value: Option<String>) -> u64 {
    value
        .and_then(|value| value.parse::<u64>().ok())
        .map(|value| {
            value.clamp(
                OVERLAY_WAL_WATERMARK_BYTES_MIN,
                OVERLAY_WAL_WATERMARK_BYTES_MAX,
            )
        })
        .unwrap_or(OVERLAY_WAL_WATERMARK_BYTES_DEFAULT)
}

fn overlay_budget_bytes() -> usize {
    parse_overlay_budget_bytes(std::env::var("TOFU_DB_OVERLAY_BUDGET_BYTES").ok())
}

fn overlay_wal_watermark_bytes() -> u64 {
    parse_overlay_wal_watermark_bytes(
        std::env::var("TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES").ok(),
    )
}

/// Deferred-application write overlay selection (the W1 write-amplification
/// root fix, `docs/TOFUDB_WRITE_PATH_OVERLAY.md`). Default: ENABLED — the
/// overlay is the write path since 2026-09-21: the replay-stranding defect
/// is fixed, the per-test mode triage is complete, the lib suite passes in
/// both configurations, and the former sole blocker
/// (`full_catalog_adoption_reclaims_bounded_files_and_recovers`, an
/// off-by-two orphan-scan admission bound against a full catalog with an
/// in-flight pack publication) is fixed by the 4099-entry scan window.
/// `TOFU_DB_WRITE_OVERLAY=0` remains an undocumented emergency escape
/// hatch, never a user tuning path.
pub(crate) fn parse_write_overlay_enabled(value: Option<String>) -> bool {
    value.map(|value| value != "0").unwrap_or(true)
}

pub(crate) fn write_overlay_enabled() -> bool {
    parse_write_overlay_enabled(std::env::var("TOFU_DB_WRITE_OVERLAY").ok())
}

/// Physical tombstone reclamation in the leaf-repack path (L1 of
/// `docs/TOFUDB_TOMBSTONE_COMPACTION.md`). Default: DISABLED.
///
/// When enabled, a repack drops *untouched* tombstones whose commit version is
/// below the oldest live snapshot pin, so churn-heavy time-ordered indexes
/// stop accumulating dead leaves and their traversal/storage cost. Range
/// witnesses retain all visited leaves without a count quota. Batch writes
/// themselves are never reclaimed
/// here (their version is the current commit sequence).
///
/// This is a deliberate, bounded semantic change, not a pure optimization: a
/// transaction that witnessed key K's ABSENCE and commits after K was
/// inserted, deleted, and physically reclaimed no longer receives a conflict.
/// Final-state serializability is preserved (the commit-time state matches the
/// transaction's assumption); pinned snapshots are unaffected because they
/// hold COW roots. The named contract test
/// (`coalesced_leaf_deletes_preserve_absence_witnesses_and_untouched_references`)
/// pins the old behavior, so this stays opt-in until the OCC conflict matrix
/// and a soak validate the new boundary. `TOFU_DB_TOMBSTONE_RECLAIM=1` enables
/// it; `0` or unset keeps byte-identical behavior.
pub(crate) fn parse_tombstone_reclaim_enabled(value: Option<String>) -> bool {
    value.map(|value| value == "1").unwrap_or(false)
}

fn tombstone_reclaim_enabled() -> bool {
    use std::sync::OnceLock;
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| {
        parse_tombstone_reclaim_enabled(std::env::var("TOFU_DB_TOMBSTONE_RECLAIM").ok())
    })
}

/// Watermark for L1 tombstone reclamation passed down the repack path.
///
/// `0` means "reclaim nothing" (`version < 0` is never true), which keeps the
/// default path byte-identical. When enabled it is the oldest live snapshot
/// pin, or `u64::MAX` when no pins exist (every tombstone is reclaimable).
fn tombstone_reclaim_below(oldest_live_pin: u64) -> u64 {
    if tombstone_reclaim_enabled() {
        oldest_live_pin
    } else {
        0
    }
}

impl EntityDatabase {
    /// Effective reclaim watermark for this database: the per-database test
    /// override wins over the global `TOFU_DB_TOMBSTONE_RECLAIM` flag.
    fn effective_tombstone_reclaim_below(&self, oldest_live_pin: u64) -> u64 {
        match self.tombstone_reclaim_override {
            Some(true) => oldest_live_pin,
            Some(false) => 0,
            None => tombstone_reclaim_below(oldest_live_pin),
        }
    }

    /// Whether tombstone reclamation is enabled for this database (global
    /// flag or override); the maintenance round short-circuits when off.
    pub(crate) fn tombstone_reclaim_is_enabled(&self) -> bool {
        self.tombstone_reclaim_override
            .unwrap_or_else(tombstone_reclaim_enabled)
    }

    #[cfg(test)]
    pub(crate) fn set_tombstone_reclaim_override_for_test(&mut self, enabled: Option<bool>) {
        self.tombstone_reclaim_override = enabled;
    }
}
const POINT_WITNESS_ACCOUNTING_OVERHEAD: usize = 64 + std::mem::size_of::<RetainedPointRead>();
const RANGE_WITNESS_ACCOUNTING_OVERHEAD: usize = 128;
const WRITE_ACCOUNTING_OVERHEAD: usize = 64;
const RETIRED_RANGE_ACCOUNTING_OVERHEAD: usize = 64;
const PERSISTENT_ROOT_PIN_MAGIC: &[u8; 8] = b"TDBPIN01";
const PERSISTENT_ROOT_PIN_VERSION: u32 = 1;
const PERSISTENT_ROOT_PIN_COUNT_MAGIC: &[u8; 8] = b"TDBPCNT1";
const PERSISTENT_ROOT_PIN_COUNT_VERSION: u32 = 1;
const AUTHORITY_INTERNAL_ID: u64 = u64::MAX;
const PERSISTENT_ROOT_PIN_KEY_IDENTITY_BYTES: usize = 16;
const PERSISTENT_ROOT_PIN_VALUE_BYTES: usize = 8 + 4 + 8 + 32;
const PERSISTENT_ROOT_PIN_COUNT_VALUE_BYTES: usize = 8 + 4 + 8;

// ── Snapshot read replica (2026-09-18, docs/STORAGE_TOFUDB.md) ──
//
// Every wire query used to execute inside the single authority mutex, so a
// slow mount turned one writer's I/O latency into a global reader convoy.
// The read replica persona pins the current immutable root under a brief
// authority hold, then reads OUTSIDE the mutex: pages are content-addressed
// and verified per read, so a pinned snapshot's reachable pages are stable
// without any lock. Writes keep the authority mutex as their certification
// anchor.

/// Block I/O backend for entity-tree reads and copy-on-write page staging.
///
/// `Engine` implements it for the writer persona; the replica persona
/// answers verified reads through the shared `BlockStore` and hard-denies
/// writes, so a replicated `Tree` can never stage a page off-mutex.
pub trait EntityBlockIo {
    fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>>;
    fn write_fresh(&self, payload: &[u8]) -> io::Result<BlockId>;
    fn write_fresh_owned(&self, payload: Vec<u8>) -> io::Result<BlockId>;
}

impl EntityBlockIo for Engine {
    fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.read_block_shared(id)
    }
    fn write_fresh(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.write_block_fresh(payload)
    }
    fn write_fresh_owned(&self, payload: Vec<u8>) -> io::Result<BlockId> {
        self.write_block_fresh_owned(payload)
    }
}

/// Replica-side backend: verified reads through the shared block store,
/// hard-denied writes. Liveness follows the engine's restart-required
/// verdict through the shared flag captured at replica build time; a stale
/// snapshot read is never a correctness hazard (content-verified immutable
/// pages), but reading from a poisoned authority fails closed all the same.
pub(crate) struct ReplicaBlockIo {
    blocks: Arc<crate::block::BlockStore>,
    restart_required: Arc<std::sync::atomic::AtomicBool>,
}

impl ReplicaBlockIo {
    fn check_live(&self) -> io::Result<()> {
        if self.restart_required.load(std::sync::atomic::Ordering::Relaxed) {
            return Err(io::Error::other(
                "tofu-db authority outcome is uncertain; close and reopen the engine",
            ));
        }
        Ok(())
    }
}

impl EntityBlockIo for ReplicaBlockIo {
    fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.check_live()?;
        self.blocks.get_shared(id)
    }
    fn write_fresh(&self, _payload: &[u8]) -> io::Result<BlockId> {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "entity read replica cannot stage pages",
        ))
    }
    fn write_fresh_owned(&self, _payload: Vec<u8>) -> io::Result<BlockId> {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "entity read replica cannot stage pages",
        ))
    }
}

/// A bounded physical merge capture. Its writer-class snapshot registration
/// protects old source pages while preparation runs outside authority admission.
/// No private page is shared or durable before the root fence is validated.
pub(crate) struct OverlayMergePreparation {
    database_instance_id: Uuid,
    root: Option<BlockId>,
    overlay: crate::entity_overlay::EntityOverlay,
    source: ReplicaBlockIo,
    page_cache: Arc<std::sync::RwLock<DecodedPageCache>>,
    pin: Arc<SnapshotPin>,
    reclaim_below: u64,
    limit: usize,
}

struct PrivateOverlayPages {
    source: ReplicaBlockIo,
    state: Mutex<PrivateOverlayPageState>,
}

struct PrivateOverlayPageState {
    pages: BTreeMap<BlockId, Arc<Vec<u8>>>,
    memory: SnapshotPin,
}

impl PrivateOverlayPageState {
    fn reserve_memory(&mut self, bytes: usize) -> io::Result<()> {
        self.memory.reserve_transaction_bytes(bytes)
    }
}

impl EntityBlockIo for PrivateOverlayPages {
    fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.source.check_live()?;
        if let Some(bytes) = self.state.lock().map_err(|_| io::Error::other("private merge pages poisoned"))?.pages.get(&id).cloned() {
            return Ok(bytes);
        }
        self.source.read_verified(id)
    }
    fn write_fresh(&self, bytes: &[u8]) -> io::Result<BlockId> {
        self.write_fresh_owned(bytes.to_vec())
    }
    fn write_fresh_owned(&self, bytes: Vec<u8>) -> io::Result<BlockId> {
        self.source.check_live()?;
        let id = BlockId::for_payload(&bytes);
        let mut state = self.state.lock().map_err(|_| io::Error::other("private merge pages poisoned"))?;
        if !state.pages.contains_key(&id) {
            state.reserve_memory(bytes.capacity().saturating_add(128))?;
            state.pages.insert(id, Arc::new(bytes));
        }
        Ok(id)
    }
}

pub(crate) struct PreparedOverlayMerge {
    capture: OverlayMergePreparation,
    // Keep all preparation reservations alive through publication/discard.
    _pages: PrivateOverlayPages,
    payloads: Vec<(BlockId, crate::block::PreparedBlockPayload)>,
    boundary: OverlayPosition,
    root_with_tail: Option<BlockId>,
    root_without_tail: Option<BlockId>,
    written_ids: Vec<BlockId>,
    carried_bytes: usize,
    actual_entries: usize,
    consumed_positions: usize,
    superseded_positions: usize,
    limit: usize,
}

impl OverlayMergePreparation {
    /// Authenticated old-page reads and new-page encoding happen only here,
    /// with no authority mutex or physical publication lane held.
    pub(crate) fn encode(self) -> io::Result<PreparedOverlayMerge> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_PREPARE_ENCODE);
        let mut limit = self.limit;
        loop {
            let pages = PrivateOverlayPages {
                source: ReplicaBlockIo { blocks: Arc::clone(&self.source.blocks), restart_required: Arc::clone(&self.source.restart_required) },
                state: Mutex::new(PrivateOverlayPageState { pages: BTreeMap::new(), memory: self.pin.lease() }),
            };
            let chunk = self.overlay.merge_chunk(limit, EntityDatabase::MERGE_CHUNK_ENTRIES)
                .ok_or_else(|| invalid_data("captured overlay prefix is empty"))?;
            let actual_entries = chunk.entries.len();
            let batch_bytes = chunk.entries.iter().fold(0_usize, |bytes, (key, value, _)|
                bytes.saturating_add(key.capacity()).saturating_add(value.as_ref().map_or(0, Vec::capacity)).saturating_add(64));
            pages.state.lock().map_err(|_| io::Error::other("private merge pages poisoned"))?.reserve_memory(batch_bytes)?;
            let tree = Tree { backend: &pages, page_cache: Some(self.page_cache.as_ref()) };
            let mut directory = tree.split_root(self.root)?;
            let (base, mut written_ids) = {
                let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_APPLY_BATCH);
                tree.apply_batch(directory.base_root, chunk.entries,
                    TombstoneReclaim { below: self.reclaim_below, mounts: &directory.mounts })?
            };
            directory.base_root = base;
            // Prepare both outcomes: new foreground suffixes may arrive after
            // capture. Only the publication hold decides whether a cursor is
            // still required; it never performs another old-page tree walk.
            directory.materialized = Some(chunk.position.clone());
            directory.validate()?;
            let root_with_tail = Some(tree.store_directory(&directory, &mut written_ids)?);
            directory.materialized = None;
            let root_without_tail = if directory.mounts.is_empty() { base }
                else { Some(tree.store_directory(&directory, &mut written_ids)?) };
            written_ids.extend(root_with_tail);
            written_ids.extend(root_without_tail);
            written_ids.sort_unstable();
            written_ids.dedup();
            let carried_bytes = {
                let state = pages.state.lock().map_err(|_| io::Error::other("private merge pages poisoned"))?;
                written_ids.iter().filter_map(|id| state.pages.get(id)).try_fold(0_usize, |total, bytes|
                    total.checked_add(bytes.len()).ok_or_else(|| invalid_data("private merge byte count overflow")))?
            };
            if u32::try_from(written_ids.len()).is_ok()
                && carried_bytes <= crate::transaction::MAX_CARRIED_PAYLOAD_BYTES {
                let payloads = {
                    let mut state = pages.state.lock().map_err(|_| io::Error::other("private merge pages poisoned"))?;
                    state.reserve_memory(written_ids.len().checked_mul(
                        std::mem::size_of::<(BlockId, crate::block::PreparedBlockPayload)>())
                        .ok_or_else(|| invalid_data("private merge payload metadata overflow"))?)?;
                    let mut payloads = Vec::with_capacity(written_ids.len());
                    for id in &written_ids {
                        if let Some(bytes) = state.pages.get(id) {
                            let payload = crate::block::PreparedBlockSource::capture_shared(Arc::clone(bytes))?;
                            if payload.id() != *id {
                                return Err(invalid_data("private merge page identity changed"));
                            }
                            payloads.push((*id, payload));
                        }
                    }
                    payloads
                };
                return Ok(PreparedOverlayMerge { capture: self, _pages: pages, payloads, boundary: chunk.position,
                    root_with_tail, root_without_tail, written_ids, carried_bytes, actual_entries,
                    consumed_positions: chunk.consumed_positions, superseded_positions: chunk.superseded_positions, limit });
            }
            if limit == 1 {
                return Err(io::Error::new(io::ErrorKind::WouldBlock, "private merge needs the bounded foreground spill path"));
            }
            limit = (limit / 2).max(1);
            // Dropping pages returns the attempt's aggregate memory reservations.
        }
    }
}

#[cfg(test)]
const LEGACY_AGGREGATE_TRANSACTION_BYTES: usize = 160 * 1024 * 1024;
#[cfg(test)]
const LEGACY_SNAPSHOT_HANDLE_LIMIT: usize = 64;

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct EntityKey(Vec<u8>);

impl EntityKey {
    pub fn new(
        tenant_id: u64,
        owner_user_id: u64,
        namespace: &str,
        key: &[u8],
    ) -> io::Result<Self> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input(
                "tenant and owner identities must be positive",
            ));
        }
        if namespace.is_empty()
            || namespace.len() > MAX_NAMESPACE_BYTES
            || key.len() > MAX_ENTITY_KEY_BYTES
        {
            return Err(invalid_input("entity namespace or key exceeds its bound"));
        }
        // The key is the final field and therefore needs no length prefix.
        // Keeping raw key bytes last is essential: B+Tree ordering and range
        // witnesses must follow key-byte order, not key-length order.
        let mut encoded = Vec::with_capacity(8 + 8 + 1 + namespace.len() + key.len());
        encoded.extend_from_slice(&tenant_id.to_be_bytes());
        encoded.extend_from_slice(&owner_user_id.to_be_bytes());
        encoded.push(namespace.len() as u8);
        encoded.extend_from_slice(namespace.as_bytes());
        encoded.extend_from_slice(key);
        Ok(Self(encoded))
    }

    pub fn encoded(&self) -> &[u8] {
        &self.0
    }

    pub fn tenant_id(&self) -> u64 {
        u64::from_be_bytes(self.0[..8].try_into().unwrap())
    }

    pub fn owner_user_id(&self) -> u64 {
        u64::from_be_bytes(self.0[8..16].try_into().unwrap())
    }

    pub(crate) fn namespace(&self) -> &str {
        let namespace_bytes = self.0[16] as usize;
        std::str::from_utf8(&self.0[17..17 + namespace_bytes])
            .expect("EntityKey constructors require a UTF-8 namespace")
    }

    pub fn key_bytes(&self) -> &[u8] {
        let namespace_bytes = self.0[16] as usize;
        &self.0[17 + namespace_bytes..]
    }

    pub fn prefix_range(
        tenant_id: u64,
        owner_user_id: u64,
        namespace: &str,
        key_prefix: &[u8],
    ) -> io::Result<(Self, Self)> {
        let start = Self::new(tenant_id, owner_user_id, namespace, key_prefix)?;
        let mut end = start.0.clone();
        let successor_index = end
            .iter()
            .rposition(|byte| *byte != u8::MAX)
            .ok_or_else(|| invalid_input("entity prefix has no lexical successor"))?;
        // Tenant and owner are positive big-endian integers, and namespace
        // length is at most 63, so a successor always exists inside this
        // owner scope even when every raw prefix byte is 0xff.
        if successor_index < 16 {
            return Err(invalid_input("entity prefix successor escapes owner scope"));
        }
        end[successor_index] += 1;
        end.truncate(successor_index + 1);
        Ok((start, Self(end)))
    }

    pub fn exact_range(self) -> io::Result<(Self, Self)> {
        if self.key_bytes().len() == MAX_ENTITY_KEY_BYTES {
            return Err(invalid_input("exact entity range key exceeds its bound"));
        }
        let mut end = self.0.clone();
        end.push(0);
        Ok((self, Self(end)))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct EntityValue {
    version: u64,
    value: Option<Vec<u8>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Child {
    lower_bound: Vec<u8>,
    block_id: BlockId,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum Page {
    Leaf(Vec<(Vec<u8>, EntityValue)>),
    Internal { level: u8, children: Vec<Child> },
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct RangeMount {
    start: Vec<u8>,
    end: Vec<u8>,
    root: BlockId,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct RootDirectory {
    base_root: Option<BlockId>,
    mounts: Vec<RangeMount>,
    materialized: Option<OverlayPosition>,
}

#[derive(Clone)]
enum RootNode {
    Page(Page),
    Directory(RootDirectory),
}

impl RootNode {
    fn owned_allocation_bytes(&self) -> usize {
        std::mem::size_of::<Self>().saturating_add(match self {
            Self::Page(Page::Leaf(entries)) => entries.capacity()
                .saturating_mul(std::mem::size_of::<(Vec<u8>, EntityValue)>())
                .saturating_add(entries.iter().fold(0usize, |total, (key, value)|
                    total.saturating_add(key.capacity())
                        .saturating_add(value.value.as_ref().map_or(0, Vec::capacity)))),
            Self::Page(Page::Internal { children, .. }) => children.capacity()
                .saturating_mul(std::mem::size_of::<Child>())
                .saturating_add(children.iter().fold(0usize, |total, child|
                    total.saturating_add(child.lower_bound.capacity()))),
            Self::Directory(directory) => directory.mounts.capacity()
                .saturating_mul(std::mem::size_of::<RangeMount>())
                .saturating_add(directory.mounts.iter().fold(0usize, |total, mount|
                    total.saturating_add(mount.start.capacity()).saturating_add(mount.end.capacity())))
                .saturating_add(directory.materialized.as_ref().and_then(|position| position.key.as_ref())
                    .map_or(0, Vec::capacity)),
        })
    }
}

/// Default decoded entity-page cache budget.  Decoded pages expand to roughly
/// four times their encoded size once per-entry Vec/String overhead lands, so
/// 16 MiB resident covers the hot root-to-leaf paths of personal-mode
/// authorities without leaving the personal resource budget.
const DEFAULT_PAGE_CACHE_BYTES: usize = 16 * 1024 * 1024;
const PAGE_CACHE_DECODED_EXPANSION: usize = 4;
const PAGE_CACHE_ENTRY_OVERHEAD_BYTES: usize = 256;

/// Byte-bounded CLOCK cache over verified, decoded immutable tree nodes.
/// Hits share the cache lock and only set an eviction hint; they never mutate
/// a global recency queue. Misses verify before publication, as with the block
/// payload cache. Content-addressed nodes require no invalidation.
struct DecodedPageCacheEntry {
    node: Arc<RootNode>,
    charge: usize,
    referenced: std::sync::atomic::AtomicBool,
}

struct DecodedPageCache {
    entries: HashMap<BlockId, DecodedPageCacheEntry>,
    clock: VecDeque<BlockId>,
    resident_bytes: usize,
    capacity_bytes: usize,
}

impl DecodedPageCache {
    fn new(capacity_bytes: usize) -> Self {
        Self { entries: HashMap::new(), clock: VecDeque::new(),
            resident_bytes: 0, capacity_bytes }
    }

    fn get(&self, id: BlockId) -> Option<Arc<RootNode>> {
        let entry = self.entries.get(&id)?;
        // This bit is only a cache replacement hint, never a publication fence.
        entry.referenced.store(true, std::sync::atomic::Ordering::Relaxed);
        Some(Arc::clone(&entry.node))
    }

    fn insert(&mut self, id: BlockId, node: Arc<RootNode>, encoded_len: usize) {
        let charge = encoded_len.saturating_mul(PAGE_CACHE_DECODED_EXPANSION)
            .saturating_add(PAGE_CACHE_ENTRY_OVERHEAD_BYTES)
            .max(node.owned_allocation_bytes().saturating_add(PAGE_CACHE_ENTRY_OVERHEAD_BYTES));
        if charge > self.capacity_bytes || self.entries.contains_key(&id) { return; }
        // Evict before admitting the verified miss, so a recently referenced
        // old page cannot immediately evict the page we have just decoded.
        self.evict_to_budget(self.capacity_bytes - charge);
        self.entries.insert(id, DecodedPageCacheEntry {
            node, charge, referenced: std::sync::atomic::AtomicBool::new(false),
        });
        self.clock.push_back(id);
        self.resident_bytes += charge;
    }

    fn set_capacity_bytes(&mut self, capacity_bytes: usize) {
        self.capacity_bytes = capacity_bytes;
        self.evict_to_budget(capacity_bytes);
    }

    fn evict_to_budget(&mut self, budget: usize) {
        // Each resident has exactly one clock entry. With exclusive ownership,
        // at most two visits per resident suffice, even if every bit was set.
        while self.resident_bytes > budget {
            let Some(id) = self.clock.pop_front() else { break; };
            let Some(entry) = self.entries.get(&id) else { continue; };
            if entry.referenced.swap(false, std::sync::atomic::Ordering::Relaxed) {
                self.clock.push_back(id);
            } else if let Some(entry) = self.entries.remove(&id) {
                self.resident_bytes -= entry.charge;
            }
        }
        if self.entries.is_empty() { self.clock = VecDeque::new(); }
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct EntitySnapshot {
    pub sequence: u64,
    pub root: Option<BlockId>,
}

#[derive(Clone, Debug)]
struct RangeWitness {
    start: Vec<u8>,
    end: Vec<u8>,
    leaf_ids: Vec<BlockId>,
    reverse: bool,
    scan_limit: usize,
}

#[derive(Debug)]
enum RetainedPointRead {
    NotRetained,
    Missing,
    Value(Box<[u8]>),
}

#[derive(Debug)]
struct PointWitness {
    version: Option<u64>,
    read: RetainedPointRead,
}

impl PointWitness {
    fn version_only(version: Option<u64>) -> Self {
        Self { version, read: RetainedPointRead::NotRetained }
    }

    fn discard_read(&mut self) -> usize {
        match std::mem::replace(&mut self.read, RetainedPointRead::NotRetained) {
            RetainedPointRead::Value(bytes) => bytes.len(),
            _ => 0,
        }
    }
}

#[derive(Debug)]
pub struct EntityTransaction {
    database_instance_id: Uuid,
    tenant_id: u64,
    owner_user_id: u64,
    additional_scope_prefixes: Vec<Vec<u8>>,
    snapshot: EntitySnapshot,
    overlay_view: EntityOverlayView,
    writable: bool,
    // The version always survives value eviction and remains the OCC witness.
    // Retained values belong only to this immutable snapshot, never a new request.
    point_witnesses: BTreeMap<Vec<u8>, PointWitness>,
    range_witnesses: Vec<RangeWitness>,
    retired_ranges: Vec<(Vec<u8>, Vec<u8>)>,
    replacement_ranges: Vec<(Vec<u8>, Vec<u8>)>,
    mounted_ranges: Vec<RangeMount>,
    staged_reference_blocks: Vec<BlockId>,
    writes: BTreeMap<Vec<u8>, Option<Vec<u8>>>,
    snapshot_pin: SnapshotPin,
}

impl EntityTransaction {
    /// Account ownership without evicting values at an artificial quota.
    fn reserve_transaction_bytes(&mut self, bytes: usize) -> io::Result<()> {
        self.snapshot_pin.reserve_transaction_bytes(bytes)
    }

    fn discard_point_read(&mut self, key: &[u8]) {
        if let Some(witness) = self.point_witnesses.get_mut(key) {
            let released = witness.discard_read();
            if released > 0 { self.snapshot_pin.release_transaction_bytes(released); }
        }
    }
}

enum EntityPersona {
    Writer(Engine),
    Replica(ReplicaCore),
}

/// Frozen read-side state captured under the authority-mutex hold at replica
/// build: the shared verified block store, the shared restart verdict, and
/// the pinned `(durable_sequence, root)` the replica serves.
struct ReplicaCore {
    blocks: ReplicaBlockIo,
    captured: EntitySnapshot,
    authority_uuid: Uuid,
}

pub struct EntityDatabase {
    persona: EntityPersona,
    root: Option<BlockId>,
    database_instance_id: Uuid,
    snapshot_pins: Arc<SnapshotPinRegistry>,
    page_cache: Arc<std::sync::RwLock<DecodedPageCache>>,
    /// Advisory tombstone-dense windows reported by scans, shared with read
    /// replicas (the search projection reads through one); the maintenance
    /// worker drains them from the writer persona.
    compaction_hints: Arc<Mutex<CompactionHints>>,
    /// Per-database override of the global `TOFU_DB_TOMBSTONE_RECLAIM` flag
    /// (test-only; production uses the global default/flag). `None` defers
    /// to `tombstone_reclaim_enabled()`.
    tombstone_reclaim_override: Option<bool>,
    /// Registered reader pin owned by the replica persona; transactions lease
    /// it instead of consuming fresh registry handles. `None` on the writer.
    replica_pin: Option<Arc<SnapshotPin>>,
    query_publisher: Option<Arc<PublishedEntityReads>>,
    /// Deferred-application write overlay, shared with read replicas. With
    /// `overlay_enabled`, plain-writes commits install their mutations here
    /// (one WAL delta record) instead of rewriting the tree per commit; a
    /// bounded merge applies them as one batch at checkpoint time. See
    /// `entity_overlay.rs` for the correctness rules.
    pub(crate) overlay: Arc<std::sync::RwLock<crate::entity_overlay::EntityOverlay>>,
    /// Lock-free mirror of the overlay's trigger counters (bytes/entries):
    /// the per-commit merge trigger reads these two atomics instead of
    /// queueing on the overlay read lock behind a running merge's write
    /// lock (2026-09-20: ~48 µs/append of pure trigger-check contention).
    overlay_trigger_bytes: std::sync::atomic::AtomicUsize,
    overlay_trigger_entries: std::sync::atomic::AtomicUsize,
    pub(crate) overlay_enabled: bool,
    /// Merge trigger bounds, from the measured defaults or the bounded
    /// operator overrides (`TOFU_DB_OVERLAY_BUDGET_BYTES` /
    /// `TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES`; see `merge_overlay_if_needed`).
    pub(crate) overlay_budget_bytes: usize,
    pub(crate) overlay_wal_watermark_bytes: u64,
    /// AIMD entry target for the next overlay merge chunk; see
    /// `MERGE_CHUNK_TIME_TARGET`. Lock-free so the background drain, the
    /// commit-path backstop, and read replicas never queue on it.
    merge_chunk_entries_adaptive: std::sync::atomic::AtomicUsize,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct EntitySnapshotPinMetrics {
    pub active_handles: u32,
    pub distinct_snapshots: u32,
    pub oldest_sequence: Option<u64>,
    pub retained_transaction_bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EntityMountConsolidationProgress {
    pub rows_materialized: u32,
    pub materialized_bytes: u64,
    pub mount_completed: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct EntityCompactionProgress {
    pub pages_rewritten: u32,
    pub entries_reclaimed: u32,
    /// Remaining window to resume from; `None` when the range is fully swept.
    pub resume: Option<(Vec<u8>, Vec<u8>)>,
}

#[derive(Clone, Copy)]
struct TombstoneReclaim<'a> {
    below: u64,
    mounts: &'a [RangeMount],
}

struct ReclaimBudget {
    leaves_left: usize,
    reclaimed: u32,
    /// First unvisited key once the leaf budget is spent mid-range.
    resume: Option<Vec<u8>>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct EntityPageReachabilityMetrics {
    pub page_count: u64,
    pub payload_bytes: u64,
    pub maximum_frontier: u32,
}

struct ReachabilityNode {
    block_id: BlockId,
    expected_level: Option<u8>,
    lower_bound: Option<Vec<u8>>,
    upper_bound: Option<Vec<u8>>,
    discover_persistent_roots: bool,
}

struct PageFragment {
    child: Child,
    level: u8,
}

#[derive(Debug, Default)]
struct SnapshotPinState {
    active_handles: usize,
    snapshots: BTreeMap<EntitySnapshot, usize>,
}
#[path = "entity_pin_registry.rs"]
mod pin_registry;
pub(crate) use pin_registry::SnapshotPinRegistry;
#[cfg(test)]
#[path = "entity_pin_registry_tests.rs"]
mod pin_registry_tests;

#[derive(Debug)]
pub(crate) struct SnapshotPin {
    snapshot: EntitySnapshot,
    state: Arc<SnapshotPinRegistry>,
    retained_transaction_bytes: usize,
    /// `false` for leases: a lease shares one registered root pin and never
    /// unregisters it on drop; its witness-byte reservations still return to
    /// the shared budget. Registration lifetime is owned by the root pin.
    registered: bool,
    /// A prepared transaction can outlive its snapshot facade. Keep the
    /// registered root alive until every leased transaction and buffer drops.
    _registration: Option<Arc<SnapshotPin>>,
}

impl SnapshotPin {
    fn repin(&mut self, snapshot: EntitySnapshot) -> io::Result<()> {
        if !self.registered { return Err(invalid_input("cannot repin a replica lease")); }
        let mut state = self.state.lock().map_err(|_| io::Error::other("snapshot pin registry is poisoned"))?;
        if !state.snapshots.contains_key(&self.snapshot) {
            return Err(invalid_data("snapshot pin is missing"));
        }
        if snapshot == self.snapshot { return Ok(()); }
        let next_count = state.snapshots.get(&snapshot).copied().unwrap_or(0).checked_add(1)
            .ok_or_else(|| invalid_data("snapshot root reference accounting overflow"))?;
        let count = state.snapshots.get_mut(&self.snapshot).unwrap();
        *count -= 1;
        if *count == 0 { state.snapshots.remove(&self.snapshot); }
        state.snapshots.insert(snapshot, next_count);
        self.snapshot = snapshot;
        Ok(())
    }

    /// Re-lease of an already-registered snapshot within one replica
    /// request: no new registry handle, so N transactions cost one pin slot.
    /// The parent registration survives until the last escaped lease drops.
    fn lease(self: &Arc<Self>) -> SnapshotPin {
        SnapshotPin {
            snapshot: self.snapshot,
            state: Arc::clone(&self.state),
            retained_transaction_bytes: 0,
            registered: false,
            _registration: Some(Arc::clone(self)),
        }
    }
    pub(crate) fn reserve_transaction_bytes(&mut self, bytes: usize) -> io::Result<()> {
        let next = self.retained_transaction_bytes.checked_add(bytes)
            .ok_or_else(|| invalid_data("entity transaction byte accounting overflow"))?;
        self.state.reserve_bytes(bytes)?;
        self.retained_transaction_bytes = next;
        Ok(())
    }

    fn release_transaction_bytes(&mut self, bytes: usize) {
        if self.state.release_bytes(bytes) {
            self.retained_transaction_bytes = self.retained_transaction_bytes.saturating_sub(bytes);
        }
    }
}

impl Drop for SnapshotPin {
    fn drop(&mut self) {
        let _release = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_SNAPSHOT_PIN_RELEASE);
        if !self.state.release_bytes(self.retained_transaction_bytes) { return; }
        if !self.registered { return; }
        let Ok(mut state) = self.state.lock() else { return; };
        let remove_snapshot = match state.snapshots.get_mut(&self.snapshot) {
            Some(count) if *count > 1 => {
                *count -= 1;
                false
            }
            Some(_) => true,
            None => return,
        };
        if remove_snapshot {
            state.snapshots.remove(&self.snapshot);
        }
        state.active_handles = state.active_handles.saturating_sub(1);
    }
}

pub(crate) struct PreparedEntityCommit {
    expected_sequence: u64,
    next_root: Option<BlockId>,
    written_block_ids: Vec<BlockId>,
    root_record: Option<Vec<u8>>,
    /// Deferred-application delta (`Some` on the overlay path): installed in
    /// the in-memory overlay at apply time and encoded as the envelope's
    /// `EntityOverlay` family record. On the legacy tree path it is `None`.
    overlay_delta: Option<BTreeMap<Vec<u8>, Option<Vec<u8>>>>,
}

impl PreparedEntityCommit {
    pub(crate) fn block_ids(&self) -> &[BlockId] {
        &self.written_block_ids
    }

    pub(crate) fn root_record(&self) -> Option<&[u8]> {
        self.root_record.as_deref()
    }

    pub(crate) const fn authority_state_root(&self) -> Option<BlockId> {
        self.next_root
    }

    /// The envelope record carrying the deferred delta, when this commit
    /// used the overlay path.
    pub(crate) fn overlay_record(&self) -> Option<Vec<u8>> {
        self.overlay_delta
            .as_ref()
            .map(crate::entity_overlay::encode_overlay_delta)
    }
}

struct Tree<'a> {
    backend: &'a dyn EntityBlockIo,
    page_cache: Option<&'a std::sync::RwLock<DecodedPageCache>>,
}

type ScanResult = (Vec<(Vec<u8>, EntityValue)>, Vec<BlockId>);

// ── Tombstone compaction hints (L2, docs/TOFUDB_TOMBSTONE_COMPACTION.md) ──
// A bounded scan that crosses many dead leaves (every in-range entry a
// tombstone) is the exact signature that burns witness budget and walk
// latency. The scan already paid the walk, so it reports the window into a
// small advisory registry; the maintenance worker then vacuums precisely
// the regions that slow scans, in bounded slices. Hints are advisory,
// coalesced by overlap, capped, and lost on restart (the next slow scan
// re-issues them).
const COMPACTION_HINT_THRESHOLD_LEAVES: u32 = 8;
const COMPACTION_HINT_CAPACITY: usize = 64;

#[derive(Debug, Default)]
struct CompactionHints {
    /// Coalesced (start, end) -> dead leaves observed in the window.
    hints: BTreeMap<(Vec<u8>, Vec<u8>), u32>,
}

impl CompactionHints {
    fn record(&mut self, start: &[u8], end: &[u8], dead_leaves: u32) {
        if start >= end || dead_leaves == 0 {
            return;
        }
        let mut start = start.to_vec();
        let mut end = end.to_vec();
        let mut total = dead_leaves;
        // Merge every overlapping or adjacent hint into one window.
        let overlapping: Vec<(Vec<u8>, Vec<u8>)> = self
            .hints
            .keys()
            .filter(|(hint_start, hint_end)| {
                hint_start.as_slice() <= end.as_slice()
                    && start.as_slice() <= hint_end.as_slice()
            })
            .cloned()
            .collect();
        for key in overlapping {
            if let Some(count) = self.hints.remove(&key) {
                start = start.min(key.0.clone());
                end = end.max(key.1.clone());
                total = total.saturating_add(count);
            }
        }
        if self.hints.len() >= COMPACTION_HINT_CAPACITY {
            // At capacity: replace the sparsest hint when the new one is
            // denser; otherwise drop the new hint (both are advisory).
            let sparsest = self
                .hints
                .iter()
                .min_by_key(|(_, count)| **count)
                .map(|(key, count)| (key.clone(), *count));
            match sparsest {
                Some((key, count)) if total > count => {
                    self.hints.remove(&key);
                }
                _ => return,
            }
        }
        self.hints.insert((start, end), total);
    }

    fn take_densest(&mut self) -> Option<(Vec<u8>, Vec<u8>, u32)> {
        let densest = self
            .hints
            .iter()
            .max_by_key(|(_, count)| **count)
            .map(|(key, _)| key.clone());
        densest.and_then(|key| self.hints.remove(&key).map(|count| (key.0, key.1, count)))
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.hints.len()
    }
}

struct PersistentPinState {
    pin_key: EntityKey,
    encoded_snapshot: Option<Vec<u8>>,
    count_key: EntityKey,
    count: usize,
}

pub(crate) fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub(crate) fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn conflict(message: &str) -> io::Error {
    use crate::request_profile::{work, Work};
    match message {
        "entity point witness changed" => work(Work::PointConflict, 1),
        "entity write witness changed" => work(Work::BlindWriteConflict, 1),
        "entity overlay range witness changed" | "entity range witness changed" => work(Work::RangeConflict, 1),
        "entity root-structure witness changed" => work(Work::RootConflict, 1),
        _ => {},
    }
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn validate_persistent_root_pin_identity(
    tenant_id: u64,
    owner_user_id: u64,
    pin_id: &[u8],
) -> io::Result<()> {
    if tenant_id == 0
        || owner_user_id == 0
        || tenant_id == AUTHORITY_INTERNAL_ID
        || owner_user_id == AUTHORITY_INTERNAL_ID
        || pin_id.is_empty()
        || pin_id.len() > MAX_PERSISTENT_ENTITY_ROOT_PIN_ID_BYTES
    {
        return Err(invalid_input("invalid persistent entity root pin identity"));
    }
    Ok(())
}

fn persistent_root_pin_key(
    tenant_id: u64,
    owner_user_id: u64,
    pin_id: &[u8],
) -> io::Result<EntityKey> {
    validate_persistent_root_pin_identity(tenant_id, owner_user_id, pin_id)?;
    let mut key = Vec::with_capacity(PERSISTENT_ROOT_PIN_KEY_IDENTITY_BYTES + pin_id.len());
    key.extend_from_slice(&tenant_id.to_be_bytes());
    key.extend_from_slice(&owner_user_id.to_be_bytes());
    key.extend_from_slice(pin_id);
    EntityKey::new(
        AUTHORITY_INTERNAL_ID,
        AUTHORITY_INTERNAL_ID,
        PERSISTENT_ENTITY_ROOT_PIN_NAMESPACE,
        &key,
    )
}

fn persistent_root_pin_range() -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        AUTHORITY_INTERNAL_ID,
        AUTHORITY_INTERNAL_ID,
        PERSISTENT_ENTITY_ROOT_PIN_NAMESPACE,
        b"",
    )
}

fn persistent_root_pin_count_key() -> io::Result<EntityKey> {
    EntityKey::new(
        AUTHORITY_INTERNAL_ID,
        AUTHORITY_INTERNAL_ID,
        PERSISTENT_ENTITY_ROOT_PIN_CATALOG_NAMESPACE,
        b"count",
    )
}

fn encode_persistent_root_pin_count(count: usize) -> io::Result<Vec<u8>> {
    if count > MAX_PERSISTENT_ENTITY_ROOT_PINS {
        return Err(invalid_input(
            "persistent entity root pin count exceeds its bound",
        ));
    }
    let mut encoded = Vec::with_capacity(PERSISTENT_ROOT_PIN_COUNT_VALUE_BYTES);
    encoded.extend_from_slice(PERSISTENT_ROOT_PIN_COUNT_MAGIC);
    encoded.extend_from_slice(&PERSISTENT_ROOT_PIN_COUNT_VERSION.to_le_bytes());
    encoded.extend_from_slice(
        &u64::try_from(count)
            .map_err(|_| invalid_input("persistent entity root pin count overflow"))?
            .to_le_bytes(),
    );
    Ok(encoded)
}

fn decode_persistent_root_pin_count(encoded: &[u8]) -> io::Result<usize> {
    if encoded.len() != PERSISTENT_ROOT_PIN_COUNT_VALUE_BYTES
        || &encoded[..8] != PERSISTENT_ROOT_PIN_COUNT_MAGIC
        || u32::from_le_bytes(encoded[8..12].try_into().unwrap())
            != PERSISTENT_ROOT_PIN_COUNT_VERSION
    {
        return Err(invalid_data(
            "invalid persistent entity root pin count record",
        ));
    }
    let count = usize::try_from(u64::from_le_bytes(encoded[12..20].try_into().unwrap()))
        .map_err(|_| invalid_data("persistent entity root pin count overflow"))?;
    if count > MAX_PERSISTENT_ENTITY_ROOT_PINS {
        return Err(invalid_data(
            "persistent entity root pin count exceeds its bound",
        ));
    }
    Ok(count)
}

fn is_persistent_root_pin_key(encoded: &[u8]) -> io::Result<bool> {
    if encoded.len() < 17 {
        return Ok(false);
    }
    let internal_id = AUTHORITY_INTERNAL_ID.to_be_bytes();
    if encoded[..8] != internal_id || encoded[8..16] != internal_id {
        return Ok(false);
    }
    let namespace_length = encoded[16] as usize;
    let namespace_end = 17_usize
        .checked_add(namespace_length)
        .ok_or_else(|| invalid_data("persistent root pin key length overflow"))?;
    let namespace = encoded
        .get(17..namespace_end)
        .ok_or_else(|| invalid_data("truncated authority-internal entity key"))?;
    if namespace != PERSISTENT_ENTITY_ROOT_PIN_NAMESPACE.as_bytes() {
        return Ok(false);
    }
    let identity = encoded
        .get(namespace_end..)
        .ok_or_else(|| invalid_data("truncated persistent root pin identity"))?;
    let pin_id_length = identity
        .len()
        .saturating_sub(PERSISTENT_ROOT_PIN_KEY_IDENTITY_BYTES);
    if identity.len() < PERSISTENT_ROOT_PIN_KEY_IDENTITY_BYTES
        || pin_id_length == 0
        || pin_id_length > MAX_PERSISTENT_ENTITY_ROOT_PIN_ID_BYTES
    {
        return Err(invalid_data("invalid persistent root pin key"));
    }
    let tenant_id = u64::from_be_bytes(identity[..8].try_into().unwrap());
    let owner_user_id = u64::from_be_bytes(identity[8..16].try_into().unwrap());
    if tenant_id == 0
        || owner_user_id == 0
        || tenant_id == AUTHORITY_INTERNAL_ID
        || owner_user_id == AUTHORITY_INTERNAL_ID
    {
        return Err(invalid_data("invalid persistent root pin owner"));
    }
    Ok(true)
}

fn encode_persistent_root_pin(snapshot: EntitySnapshot) -> io::Result<Vec<u8>> {
    let root = snapshot
        .root
        .ok_or_else(|| invalid_input("cannot persistently pin an empty entity snapshot"))?;
    if snapshot.sequence == 0 {
        return Err(invalid_input("cannot persistently pin sequence zero"));
    }
    let mut encoded = Vec::with_capacity(PERSISTENT_ROOT_PIN_VALUE_BYTES);
    encoded.extend_from_slice(PERSISTENT_ROOT_PIN_MAGIC);
    encoded.extend_from_slice(&PERSISTENT_ROOT_PIN_VERSION.to_le_bytes());
    encoded.extend_from_slice(&snapshot.sequence.to_le_bytes());
    encoded.extend_from_slice(&root.0);
    Ok(encoded)
}

/// A decoded persistent root pin: the on-disk record always names exactly
/// one tree root, so the decoded form is non-optional by construction
/// (unlike `EntitySnapshot.root`, where an empty tree is `None`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PersistentRootPin {
    sequence: u64,
    root: BlockId,
}

fn decode_persistent_root_pin(encoded: &[u8]) -> io::Result<PersistentRootPin> {
    if encoded.len() != PERSISTENT_ROOT_PIN_VALUE_BYTES
        || &encoded[..8] != PERSISTENT_ROOT_PIN_MAGIC
        || u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != PERSISTENT_ROOT_PIN_VERSION
    {
        return Err(invalid_data("invalid persistent entity root pin record"));
    }
    let sequence = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
    if sequence == 0 {
        return Err(invalid_data("persistent entity root pin has sequence zero"));
    }
    let mut root = [0_u8; 32];
    root.copy_from_slice(&encoded[20..52]);
    Ok(PersistentRootPin {
        sequence,
        root: BlockId(root),
    })
}

fn pin_snapshot(
    state: &Arc<SnapshotPinRegistry>,
    snapshot: EntitySnapshot,
) -> io::Result<SnapshotPin> {
    let mut pins = state.lock()
        .map_err(|_| io::Error::other("entity snapshot pin registry is poisoned"))?;
    // Root reachability is authoritative; counts observe ownership, not admission.
    let active_handles = pins.active_handles.checked_add(1)
        .ok_or_else(|| invalid_data("snapshot handle accounting overflow"))?;
    let references = pins.snapshots.get(&snapshot).copied().unwrap_or(0).checked_add(1)
        .ok_or_else(|| invalid_data("snapshot root reference accounting overflow"))?;
    pins.snapshots.insert(snapshot, references);
    pins.active_handles = active_handles;
    drop(pins);
    Ok(SnapshotPin {
        snapshot, state: Arc::clone(state), retained_transaction_bytes: 0,
        registered: true, _registration: None,
    })
}

fn point_witness_reservation_bytes(key: &[u8]) -> usize {
    key.len() + POINT_WITNESS_ACCOUNTING_OVERHEAD
}

fn range_witness_reservation_bytes(start: &[u8], end: &[u8], leaf_capacity: usize) -> io::Result<usize> {
    leaf_capacity.checked_mul(std::mem::size_of::<BlockId>())
        .and_then(|bytes| bytes.checked_add(start.len()))
        .and_then(|bytes| bytes.checked_add(end.len()))
        .and_then(|bytes| bytes.checked_add(RANGE_WITNESS_ACCOUNTING_OVERHEAD))
        .ok_or_else(|| invalid_data("range witness byte accounting overflow"))
}

fn write_reservation_bytes(key: &[u8], value: Option<&[u8]>) -> usize {
    key.len() + value.map_or(0, <[u8]>::len) + WRITE_ACCOUNTING_OVERHEAD
}

fn retired_range_reservation_bytes(start: &[u8], end: &[u8]) -> usize {
    start.len() + end.len() + RETIRED_RANGE_ACCOUNTING_OVERHEAD
}

fn ranges_fully_cover(
    ranges: &[(Vec<u8>, Vec<u8>)],
    lower_bound: Option<&[u8]>,
    upper_bound: Option<&[u8]>,
) -> bool {
    lower_bound.is_some_and(|lower| {
        ranges.iter().any(|(start, end)| {
            lower >= start.as_slice() && upper_bound.is_some_and(|upper| upper <= end.as_slice())
        })
    })
}

fn ranges_overlap(
    ranges: &[(Vec<u8>, Vec<u8>)],
    lower_bound: Option<&[u8]>,
    upper_bound: Option<&[u8]>,
) -> bool {
    ranges.iter().any(|(start, end)| {
        upper_bound.is_none_or(|upper| upper > start.as_slice())
            && lower_bound.is_none_or(|lower| lower < end.as_slice())
    })
}

fn ensure_scope(transaction: &EntityTransaction, key: &EntityKey) -> io::Result<()> {
    let in_primary_scope = key.tenant_id() == transaction.tenant_id
        && key.owner_user_id() == transaction.owner_user_id;
    let in_additional_scope = transaction
        .additional_scope_prefixes
        .iter()
        .any(|prefix| key.encoded().starts_with(prefix));
    if !in_primary_scope && !in_additional_scope {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "entity key is outside the transaction owner scope",
        ));
    }
    Ok(())
}

fn ensure_scan_scope(
    transaction: &EntityTransaction,
    start: &EntityKey,
    end: &EntityKey,
) -> io::Result<()> {
    ensure_scope(transaction, start)?;
    if ensure_scope(transaction, end).is_ok() {
        return Ok(());
    }
    let covers_one_additional_prefix = transaction.additional_scope_prefixes.iter().any(|prefix| {
        if !start.encoded().starts_with(prefix) {
            return false;
        }
        let mut successor = prefix.clone();
        let Some(successor_index) = successor.iter().rposition(|byte| *byte != u8::MAX) else {
            return false;
        };
        if successor_index < 16 {
            return false;
        }
        successor[successor_index] += 1;
        successor.truncate(successor_index + 1);
        end.encoded() == successor
    });
    if covers_one_additional_prefix {
        Ok(())
    } else {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "entity scan range is outside the transaction owner scope",
        ))
    }
}

fn push_u16(bytes: &mut Vec<u8>, value: usize) -> io::Result<()> {
    let value = u16::try_from(value).map_err(|_| invalid_input("length exceeds u16"))?;
    bytes.extend_from_slice(&value.to_le_bytes());
    Ok(())
}

fn read_u16(bytes: &[u8], offset: &mut usize) -> io::Result<usize> {
    let end = offset
        .checked_add(2)
        .ok_or_else(|| invalid_data("offset overflow"))?;
    let value = bytes
        .get(*offset..end)
        .ok_or_else(|| invalid_data("truncated entity page"))?;
    *offset = end;
    Ok(u16::from_le_bytes(value.try_into().unwrap()) as usize)
}

fn take<'a>(bytes: &'a [u8], offset: &mut usize, length: usize) -> io::Result<&'a [u8]> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| invalid_data("offset overflow"))?;
    let result = bytes
        .get(*offset..end)
        .ok_or_else(|| invalid_data("truncated entity page"))?;
    *offset = end;
    Ok(result)
}

impl RootDirectory {
    fn encode(&self) -> io::Result<Vec<u8>> {
        self.validate()?;
        let mut bytes = Vec::new();
        bytes.extend_from_slice(ROOT_DIRECTORY_MAGIC);
        bytes.extend_from_slice(&(if self.materialized.is_some() { 2_u32 } else { ROOT_DIRECTORY_VERSION }).to_le_bytes());
        bytes.push(u8::from(self.base_root.is_some()));
        bytes.push(0);
        push_u16(&mut bytes, self.mounts.len())?;
        if let Some(base_root) = self.base_root {
            bytes.extend_from_slice(&base_root.0);
        }
        for mount in &self.mounts {
            push_u16(&mut bytes, mount.start.len())?;
            push_u16(&mut bytes, mount.end.len())?;
            bytes.extend_from_slice(&mount.start);
            bytes.extend_from_slice(&mount.end);
            bytes.extend_from_slice(&mount.root.0);
        }
        if let Some(position) = &self.materialized {
            bytes.extend_from_slice(&position.sequence.to_le_bytes());
            push_u16(&mut bytes, position.key.as_ref().map_or(0, Vec::len))?;
            if let Some(key) = &position.key { bytes.extend_from_slice(key); }
        }
        Ok(bytes)
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < 16
            || &bytes[..8] != ROOT_DIRECTORY_MAGIC
            || !matches!(u32::from_le_bytes(bytes[8..12].try_into().unwrap()), 1 | 2)
            || bytes[12] > 1
            || bytes[13] != 0
        {
            return Err(invalid_data("invalid entity root directory header"));
        }
        let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        let has_base = bytes[12] == 1;
        let count = u16::from_le_bytes(bytes[14..16].try_into().unwrap()) as usize;
        if (count == 0 && version == 1) || count > MAX_ENTITY_ROOT_RANGE_MOUNTS {
            return Err(invalid_data("invalid entity root range mount count"));
        }
        let mut offset = 16;
        let base_root = if has_base {
            Some(BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()))
        } else {
            None
        };
        let mut mounts = Vec::with_capacity(count);
        for _ in 0..count {
            let start_length = read_u16(bytes, &mut offset)?;
            let end_length = read_u16(bytes, &mut offset)?;
            mounts.push(RangeMount {
                start: take(bytes, &mut offset, start_length)?.to_vec(),
                end: take(bytes, &mut offset, end_length)?.to_vec(),
                root: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
            });
        }
        let materialized = if version == 2 {
            let sequence = u64::from_le_bytes(take(bytes, &mut offset, 8)?.try_into().unwrap());
            let key_len = read_u16(bytes, &mut offset)?;
            let key = if key_len == 0 { None } else { Some(take(bytes, &mut offset, key_len)?.to_vec()) };
            Some(OverlayPosition { sequence, key })
        } else { None };
        if offset != bytes.len() {
            return Err(invalid_data("entity root directory has trailing bytes"));
        }
        let directory = Self { base_root, mounts, materialized };
        directory.validate()?;
        Ok(directory)
    }

    fn validate(&self) -> io::Result<()> {
        if (self.mounts.is_empty() && self.materialized.is_none())
            || self.mounts.len() > MAX_ENTITY_ROOT_RANGE_MOUNTS
            || self.mounts.iter().any(|mount| {
                mount.start.is_empty()
                    || mount.end.is_empty()
                    || mount.start >= mount.end
                    || mount.start.len() > MAX_ENTITY_KEY_BYTES + 82
                    || mount.end.len() > MAX_ENTITY_KEY_BYTES + 82
            })
            || self
                .mounts
                .windows(2)
                .any(|pair| pair[0].end > pair[1].start)
        {
            return Err(invalid_data("invalid entity root range mounts"));
        }
        if self.materialized.as_ref().and_then(|position| position.key.as_ref())
            .is_some_and(|key| key.is_empty() || key.len() > MAX_ENTITY_KEY_BYTES + 82) {
            return Err(invalid_data("invalid materialization key"));
        }
        Ok(())
    }
}

impl Page {
    fn first_key_ref(&self) -> io::Result<&[u8]> {
        match self {
            Self::Leaf(entries) => entries
                .first()
                .map(|entry| entry.0.as_slice())
                .ok_or_else(|| invalid_data("empty entity leaf")),
            Self::Internal { children, .. } => children
                .first()
                .map(|child| child.lower_bound.as_slice())
                .ok_or_else(|| invalid_data("empty entity internal page")),
        }
    }

    fn level(&self) -> u8 {
        match self {
            Self::Leaf(_) => 0,
            Self::Internal { level, .. } => *level,
        }
    }

    fn last_key(&self) -> io::Result<&[u8]> {
        match self {
            Self::Leaf(entries) => entries
                .last()
                .map(|entry| entry.0.as_slice())
                .ok_or_else(|| invalid_data("empty entity leaf")),
            Self::Internal { children, .. } => children
                .last()
                .map(|child| child.lower_bound.as_slice())
                .ok_or_else(|| invalid_data("empty entity internal page")),
        }
    }

    fn first_key(&self) -> io::Result<Vec<u8>> {
        self.first_key_ref().map(<[u8]>::to_vec)
    }

    fn validate(&self) -> io::Result<()> {
        match self {
            Self::Leaf(entries) => {
                if entries.is_empty() || entries.windows(2).any(|pair| pair[0].0 >= pair[1].0) {
                    return Err(invalid_data("entity leaf keys are empty or unsorted"));
                }
            }
            Self::Internal { children, .. } => {
                if children.is_empty()
                    || children
                        .windows(2)
                        .any(|pair| pair[0].lower_bound >= pair[1].lower_bound)
                {
                    return Err(invalid_data("entity children are empty or unsorted"));
                }
            }
        }
        Ok(())
    }
}

pub(crate) fn visit_entity_page_graph_with_values<ReadBlock, VisitPage, VisitValue>(
    root: Option<BlockId>,
    mut read_block: ReadBlock,
    mut visit_page: VisitPage,
    mut visit_value: VisitValue,
) -> io::Result<EntityPageReachabilityMetrics>
where
    ReadBlock: FnMut(BlockId) -> io::Result<Vec<u8>>,
    VisitPage: FnMut(BlockId, &[u8]) -> io::Result<()>,
    VisitValue: FnMut(&EntityKey, &[u8]) -> io::Result<()>,
{
    let Some(root) = root else {
        return Ok(EntityPageReachabilityMetrics::default());
    };
    let mut pending = vec![ReachabilityNode {
        block_id: root,
        expected_level: None,
        lower_bound: None,
        upper_bound: None,
        discover_persistent_roots: true,
    }];
    let persistent_pin_count_key = persistent_root_pin_count_key()?;
    let mut declared_persistent_pin_count = None;
    let mut discovered_persistent_pin_count = 0_usize;
    let mut metrics = EntityPageReachabilityMetrics {
        maximum_frontier: 1,
        ..EntityPageReachabilityMetrics::default()
    };
    while let Some(node) = pending.pop() {
        if metrics.page_count == MAX_ENTITY_REACHABILITY_PAGES as u64 {
            return Err(invalid_input("entity reachability page bound exceeded"));
        }
        let encoded = read_block(node.block_id)?;
        if encoded.starts_with(ROOT_DIRECTORY_MAGIC) {
            if node.expected_level.is_some()
                || node.lower_bound.is_some()
                || node.upper_bound.is_some()
            {
                return Err(invalid_data(
                    "entity root directory appears below a B+Tree page",
                ));
            }
            let directory = RootDirectory::decode(&encoded)?;
            for mount in directory.mounts.iter().rev() {
                pending.push(ReachabilityNode {
                    block_id: mount.root,
                    expected_level: None,
                    lower_bound: None,
                    upper_bound: None,
                    discover_persistent_roots: false,
                });
            }
            if let Some(base_root) = directory.base_root {
                pending.push(ReachabilityNode {
                    block_id: base_root,
                    expected_level: None,
                    lower_bound: None,
                    upper_bound: None,
                    discover_persistent_roots: node.discover_persistent_roots,
                });
            }
            if pending.len() > MAX_ENTITY_REACHABILITY_FRONTIER {
                return Err(invalid_input("entity reachability frontier bound exceeded"));
            }
            visit_page(node.block_id, &encoded)?;
            metrics.page_count = metrics
                .page_count
                .checked_add(1)
                .ok_or_else(|| invalid_data("entity reachability page count overflow"))?;
            metrics.payload_bytes = metrics
                .payload_bytes
                .checked_add(encoded.len() as u64)
                .ok_or_else(|| invalid_data("entity reachability byte count overflow"))?;
            metrics.maximum_frontier = metrics.maximum_frontier.max(
                u32::try_from(pending.len())
                    .map_err(|_| invalid_data("entity reachability frontier overflow"))?,
            );
            continue;
        }
        let page = Page::decode(&encoded)?;
        let first_key = page.first_key_ref()?;
        let last_key = page.last_key()?;
        if node
            .expected_level
            .is_some_and(|expected| expected != page.level())
            || node
                .lower_bound
                .as_deref()
                .is_some_and(|expected| first_key != expected)
            || node
                .upper_bound
                .as_deref()
                .is_some_and(|upper| last_key >= upper)
        {
            return Err(invalid_data(
                "entity reachability level or key-range witness mismatch",
            ));
        }
        if let Page::Leaf(entries) = &page {
            for (key, entity) in entries {
                if let Some(value) = entity.value.as_deref() {
                    visit_value(&EntityKey(key.clone()), value)?;
                }
            }
        }
        match &page {
            Page::Internal { level, children } => {
                let child_level = level - 1;
                for index in (0..children.len()).rev() {
                    let child = &children[index];
                    let upper_bound = children
                        .get(index + 1)
                        .map(|next| next.lower_bound.clone())
                        .or_else(|| node.upper_bound.clone());
                    pending.push(ReachabilityNode {
                        block_id: child.block_id,
                        expected_level: Some(child_level),
                        lower_bound: Some(child.lower_bound.clone()),
                        upper_bound,
                        discover_persistent_roots: node.discover_persistent_roots,
                    });
                }
            }
            Page::Leaf(entries) if node.discover_persistent_roots => {
                for (key, value) in entries {
                    let Some(encoded_value) = value.value.as_deref() else {
                        continue;
                    };
                    if key.as_slice() == persistent_pin_count_key.encoded() {
                        if declared_persistent_pin_count.is_some() {
                            return Err(invalid_data(
                                "duplicate persistent entity root pin count record",
                            ));
                        }
                        declared_persistent_pin_count =
                            Some(decode_persistent_root_pin_count(encoded_value)?);
                        continue;
                    }
                    if !is_persistent_root_pin_key(key)? {
                        continue;
                    }
                    discovered_persistent_pin_count = discovered_persistent_pin_count
                        .checked_add(1)
                        .ok_or_else(|| invalid_data("persistent entity root pin count overflow"))?;
                    if discovered_persistent_pin_count > MAX_PERSISTENT_ENTITY_ROOT_PINS {
                        return Err(invalid_data("persistent entity root pin bound exceeded"));
                    }
                    let snapshot = decode_persistent_root_pin(encoded_value)?;
                    if snapshot.sequence >= value.version {
                        return Err(invalid_data("persistent entity root pin is not monotonic"));
                    }
                    let pinned_root = snapshot.root;
                    pending.push(ReachabilityNode {
                        block_id: pinned_root,
                        expected_level: None,
                        lower_bound: None,
                        upper_bound: None,
                        discover_persistent_roots: false,
                    });
                }
            }
            Page::Leaf(_) => {}
        }
        if pending.len() > MAX_ENTITY_REACHABILITY_FRONTIER {
            return Err(invalid_input("entity reachability frontier bound exceeded"));
        }
        visit_page(node.block_id, &encoded)?;
        metrics.page_count = metrics
            .page_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("entity reachability page count overflow"))?;
        metrics.payload_bytes = metrics
            .payload_bytes
            .checked_add(encoded.len() as u64)
            .ok_or_else(|| invalid_data("entity reachability byte count overflow"))?;
        metrics.maximum_frontier = metrics.maximum_frontier.max(
            u32::try_from(pending.len())
                .map_err(|_| invalid_data("entity reachability frontier overflow"))?,
        );
    }
    match declared_persistent_pin_count {
        Some(declared) if declared != discovered_persistent_pin_count => {
            return Err(invalid_data(
                "persistent entity root pin count does not match its catalog",
            ));
        }
        None if discovered_persistent_pin_count > MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS => {
            return Err(invalid_data(
                "legacy persistent entity root pin count exceeds its bound",
            ));
        }
        _ => {}
    }
    Ok(metrics)
}

#[cfg(test)]
fn visit_entity_page_graph<ReadBlock, VisitPage>(
    root: Option<BlockId>,
    read_block: ReadBlock,
    visit_page: VisitPage,
) -> io::Result<EntityPageReachabilityMetrics>
where
    ReadBlock: FnMut(BlockId) -> io::Result<Vec<u8>>,
    VisitPage: FnMut(BlockId, &[u8]) -> io::Result<()>,
{
    visit_entity_page_graph_with_values(root, read_block, visit_page, |_, _| Ok(()))
}

impl<'a> Tree<'a> {
    /// Loads a root node through the decoded-page cache.  A miss reads and
    /// fully verifies the block through the engine, decodes it, and only then
    /// populates the cache; a failed verify/decode populates nothing.
    fn load_node(&self, id: BlockId) -> io::Result<Arc<RootNode>> {
        self.load_version_node(id, &mut None)
    }

    fn load_version_node(&self, id: BlockId,
        cached: &mut Option<std::sync::RwLockReadGuard<'_, DecodedPageCache>>,
    ) -> io::Result<Arc<RootNode>> {
        let _timer = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_LOAD_NODE);
        if let Some(node) = cached.as_ref().and_then(|cache| cache.get(id)) {
            return Ok(node);
        }
        drop(cached.take());
        if let Some(cache) = self.page_cache {
            let waiting = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ENTITY_PAGE_CACHE_READ_WAIT);
            let guard = cache
                .read()
                .map_err(|_| io::Error::other("entity page cache lock was poisoned"))?;
            drop(waiting);
            let hit = guard.get(id);
            if let Some(node) = hit {
                return Ok(node);
            }
        }
        let encoded = {
            let _read = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ENTITY_PAGE_SOURCE_READ);
            self.backend.read_verified(id)?
        };
        let decoding = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ENTITY_PAGE_DECODE);
        let node = if encoded.starts_with(ROOT_DIRECTORY_MAGIC) {
            RootNode::Directory(RootDirectory::decode(&encoded)?)
        } else {
            RootNode::Page(Page::decode(&encoded)?)
        };
        drop(decoding);
        let node = Arc::new(node);
        if let Some(cache) = self.page_cache {
            let waiting = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ENTITY_PAGE_CACHE_WRITE_WAIT);
            let mut guard = cache
                .write()
                .map_err(|_| io::Error::other("entity page cache lock was poisoned"))?;
            drop(waiting);
            guard.insert(id, Arc::clone(&node), encoded.len());
        }
        Ok(node)
    }

    fn load(&self, id: BlockId) -> io::Result<Page> {
        match &*self.load_node(id)? {
            RootNode::Page(page) => Ok(page.clone()),
            RootNode::Directory(_) => Err(invalid_data("entity page magic mismatch")),
        }
    }

    fn load_root(&self, id: BlockId) -> io::Result<RootNode> {
        Ok((*self.load_node(id)?).clone())
    }

    fn store_directory(
        &self,
        directory: &RootDirectory,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<BlockId> {
        let block_id = self.backend.write_fresh(&directory.encode()?)?;
        if !written_block_ids.contains(&block_id) {
            written_block_ids.push(block_id);
        }
        Ok(block_id)
    }

    fn split_root(&self, root: Option<BlockId>) -> io::Result<RootDirectory> {
        let Some(root) = root else {
            return Ok(RootDirectory {
                base_root: None,
                mounts: Vec::new(),
                materialized: None,
            });
        };
        match self.load_root(root)? {
            RootNode::Page(_) => Ok(RootDirectory {
                base_root: Some(root),
                mounts: Vec::new(),
                materialized: None,
            }),
            RootNode::Directory(directory) => Ok(directory),
        }
    }

    fn subtract_mount_ranges(
        &self,
        mounts: Vec<RangeMount>,
        retired_ranges: &[(Vec<u8>, Vec<u8>)],
    ) -> io::Result<(Vec<RangeMount>, bool)> {
        let mut retained = Vec::new();
        let mut changed = false;
        for mount in mounts {
            let mut fragments = vec![(mount.start.clone(), mount.end.clone())];
            for (retired_start, retired_end) in retired_ranges {
                let mut next = Vec::new();
                for (start, end) in fragments {
                    if retired_start.as_slice() >= end.as_slice()
                        || retired_end.as_slice() <= start.as_slice()
                    {
                        next.push((start, end));
                        continue;
                    }
                    changed = true;
                    if start.as_slice() < retired_start.as_slice() {
                        next.push((start, retired_start.clone()));
                    }
                    if retired_end.as_slice() < end.as_slice() {
                        next.push((retired_end.clone(), end));
                    }
                }
                fragments = next;
            }
            for (start, end) in fragments {
                retained.push(RangeMount {
                    start,
                    end,
                    root: mount.root,
                });
            }
        }
        if retained.len() > MAX_ENTITY_ROOT_RANGE_MOUNTS {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "entity retirement would exceed the range mount bound",
            ));
        }
        Ok((retained, changed))
    }

    fn store(&self, page: &Page, written_block_ids: &mut Vec<BlockId>) -> io::Result<Child> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_STORE_PAGE);
        page.validate()?;
        let encoded = page.encode()?;
        if encoded.len() > PAGE_MAX_BYTES {
            return Err(invalid_data("entity page exceeds maximum after split"));
        }
        let block_id = self.backend.write_fresh_owned(encoded)?;
        if !written_block_ids.contains(&block_id) {
            written_block_ids.push(block_id);
        }
        Ok(Child {
            lower_bound: page.first_key()?,
            block_id,
        })
    }

    fn get(&self, root: Option<BlockId>, key: &[u8]) -> io::Result<Option<EntityValue>> {
        let Some(root) = root else {
            return Ok(None);
        };
        self.get_root(root, key, 0)
    }

    /// Sorted OCC probes share each root-to-leaf path and borrow values. A
    /// tombstone is present for version validation, so it masks lower mounts.
    fn versions(&self, root: Option<BlockId>, keys: &[&[u8]]) -> io::Result<Vec<Option<u64>>> {
        debug_assert!(keys.windows(2).all(|pair| pair[0] < pair[1]));
        let mut versions = vec![None; keys.len()];
        let targets: Vec<_> = keys.iter().enumerate().map(|(index, key)| (*key, index)).collect();
        if let Some(root) = root.filter(|_| !targets.is_empty()) {
            // A warm batch borrows one cache read lock. The first miss releases
            // it before source I/O/cache insertion and disables borrowing for
            // this batch, so neither slow storage nor a pending writer is held
            // behind repeated read-lock acquisition.
            let mut cached = if let Some(cache) = self.page_cache {
                let _waiting = crate::fsync_stats::StageTimer::diagnostic(
                    crate::fsync_stats::STAGE_ENTITY_PAGE_CACHE_READ_WAIT);
                Some(cache.read().map_err(|_| io::Error::other("entity page cache lock was poisoned"))?)
            } else { None };
            self.versions_root(root, &targets, &mut versions, 0, &mut cached)?;
        }
        Ok(versions)
    }

    fn versions_root(&self, root: BlockId, targets: &[(&[u8], usize)],
        versions: &mut [Option<u64>], directory_depth: usize,
        cached: &mut Option<std::sync::RwLockReadGuard<'_, DecodedPageCache>>) -> io::Result<()> {
        if targets.iter().all(|(_, index)| versions[*index].is_some()) { return Ok(()); }
        if directory_depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity root directory exceeds maximum depth"));
        }
        match &*self.load_version_node(root, cached)? {
            RootNode::Page(page) => self.versions_page(page, targets, versions, 0, cached),
            RootNode::Directory(directory) => {
                if let Some(base) = directory.base_root {
                    self.versions_root(base, targets, versions, directory_depth + 1, cached)?;
                }
                for mount in &directory.mounts {
                    let first = targets.partition_point(|(key, _)| *key < mount.start.as_slice());
                    let last = targets.partition_point(|(key, _)| *key < mount.end.as_slice());
                    self.versions_root(mount.root, &targets[first..last], versions, directory_depth + 1, cached)?;
                }
                Ok(())
            }
        }
    }

    fn versions_page(&self, page: &Page, targets: &[(&[u8], usize)],
        versions: &mut [Option<u64>], depth: usize,
        cached: &mut Option<std::sync::RwLockReadGuard<'_, DecodedPageCache>>) -> io::Result<()> {
        if depth > MAX_TREE_DEPTH { return Err(invalid_data("entity tree exceeds maximum depth")); }
        match page {
            Page::Leaf(entries) => {
                for (key, index) in targets {
                    if versions[*index].is_none() {
                        versions[*index] = entries.binary_search_by(|entry| entry.0.as_slice().cmp(key))
                            .ok().map(|position| entries[position].1.version);
                    }
                }
                Ok(())
            }
            Page::Internal { children, .. } => {
                let mut first = 0;
                while first < targets.len() {
                    let child = children.partition_point(|child| child.lower_bound.as_slice() <= targets[first].0)
                        .saturating_sub(1);
                    let last = if let Some(next) = children.get(child + 1) {
                        first + targets[first..].partition_point(|(key, _)| *key < next.lower_bound.as_slice())
                    } else { targets.len() };
                    let group = &targets[first..last];
                    if group.iter().any(|(_, index)| versions[*index].is_none()) {
                        match &*self.load_version_node(children[child].block_id, cached)? {
                            RootNode::Page(page) => self.versions_page(page, group, versions, depth + 1, cached)?,
                            RootNode::Directory(_) => return Err(invalid_data("entity page magic mismatch")),
                        }
                    }
                    first = last;
                }
                Ok(())
            }
        }
    }

    fn get_root(
        &self,
        root: BlockId,
        key: &[u8],
        directory_depth: usize,
    ) -> io::Result<Option<EntityValue>> {
        if directory_depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity root directory exceeds maximum depth"));
        }
        match &*self.load_node(root)? {
            RootNode::Page(page) => self.get_page(page, key, 0),
            RootNode::Directory(directory) => {
                if let Some(base_root) = directory.base_root {
                    if let Some(value) = self.get_root(base_root, key, directory_depth + 1)? {
                        return Ok(Some(value));
                    }
                }
                let index = directory
                    .mounts
                    .partition_point(|mount| mount.start.as_slice() <= key);
                let Some(mount) = index
                    .checked_sub(1)
                    .and_then(|index| directory.mounts.get(index))
                else {
                    return Ok(None);
                };
                if key < mount.end.as_slice() {
                    self.get_root(mount.root, key, directory_depth + 1)
                } else {
                    Ok(None)
                }
            }
        }
    }

    fn get_page(&self, page: &Page, key: &[u8], depth: usize) -> io::Result<Option<EntityValue>> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        match page {
            Page::Leaf(entries) => Ok(entries
                .binary_search_by(|entry| entry.0.as_slice().cmp(key))
                .ok()
                .map(|index| entries[index].1.clone())),
            Page::Internal { children, .. } => {
                let index = children.partition_point(|child| child.lower_bound.as_slice() <= key);
                match &*self.load_node(children[index.saturating_sub(1)].block_id)? {
                    RootNode::Page(child) => self.get_page(child, key, depth + 1),
                    RootNode::Directory(_) => Err(invalid_data("entity page magic mismatch")),
                }
            }
        }
    }

    fn flush_leaf_frame(
        &self,
        buffer: &mut LeafFrame,
        pages: &mut Vec<Child>,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<()> {
        let Some((lower_bound, encoded)) = buffer.finish() else { return Ok(()); };
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_STORE_PAGE);
        let block_id = self.backend.write_fresh_owned(encoded)?;
        if !written_block_ids.contains(&block_id) {
            written_block_ids.push(block_id);
        }
        pages.push(Child {
            lower_bound,
            block_id,
        });
        Ok(())
    }

    fn push_leaf_frame_entry(
        &self,
        key: &[u8],
        version: u64,
        value: Option<&[u8]>,
        buffer: &mut LeafFrame,
        pages: &mut Vec<Child>,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<()> {
        if buffer.would_overflow(key, value) {
            self.flush_leaf_frame(buffer, pages, written_block_ids)?;
        }
        buffer.push(key, version, value)
    }

    fn pack_leaves(
        &self,
        entries: Vec<(Vec<u8>, EntityValue)>,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<Vec<Child>> {
        if entries.is_empty() {
            return Err(invalid_data("cannot store an empty entity leaf"));
        }
        let mut pages = Vec::new();
        let mut buffer = LeafFrame::new();
        for (key, entity) in entries {
            self.push_leaf_frame_entry(
                &key,
                entity.version,
                entity.value.as_deref(),
                &mut buffer,
                &mut pages,
                written_block_ids,
            )?;
        }
        self.flush_leaf_frame(&mut buffer, &mut pages, written_block_ids)?;
        Ok(pages)
    }

    /// Tombstone reclamation predicate (2026-09-22 physical compaction, L1):
    /// an entry is reclaimable exactly when it is a tombstone whose version
    /// lies below the caller's watermark (the oldest live pin sequence — no
    /// open snapshot predates it; the OCC boundary is documented in
    /// `docs/TOFUDB_TOMBSTONE_COMPACTION.md`). `0` disables reclamation.
    fn reclaimable_tombstone(
        key: &[u8],
        value: Option<&[u8]>,
        version: u64,
        reclaim_below: TombstoneReclaim<'_>,
    ) -> bool {
        value.is_none() && version < reclaim_below.below
            && !reclaim_below.mounts.iter().any(|mount| mount.start.as_slice() <= key && key < mount.end.as_slice())
    }

    /// Merges a sorted write batch over an existing sorted leaf and frames the
    /// result directly, replacing equal keys and preserving OCC versions of
    /// untouched entries without materializing owned copies. Sub-watermark
    /// tombstones are reclaimed while repacking: an untouched reclaimable
    /// entry is dropped, and a reclaimable delete in the write batch removes
    /// the underlying entry instead of staging a new tombstone (the delete
    /// fact is preserved — the key leaves the tree — never a resurrection).
    fn append_merged_leaf_entries(
        &self,
        entries: &[(Vec<u8>, EntityValue)],
        writes: &[(Vec<u8>, Option<Vec<u8>>, u64)],
        reclaim_below: TombstoneReclaim<'_>,
        buffer: &mut LeafFrame,
        pages: &mut Vec<Child>,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<()> {
        let mut write_index = 0;
        for (key, value) in entries {
            while write_index < writes.len() && writes[write_index].0.as_slice() < key.as_slice() {
                let (write_key, write_value, write_version) = &writes[write_index];
                if !Self::reclaimable_tombstone(
                    write_key,
                    write_value.as_deref(),
                    *write_version,
                    reclaim_below,
                ) {
                    self.push_leaf_frame_entry(
                        write_key,
                        *write_version,
                        write_value.as_deref(),
                        buffer,
                        pages,
                        written_block_ids,
                    )?;
                }
                write_index += 1;
            }
            if write_index < writes.len() && writes[write_index].0.as_slice() == key.as_slice() {
                let (write_key, write_value, write_version) = &writes[write_index];
                if !Self::reclaimable_tombstone(
                    write_key,
                    write_value.as_deref(),
                    *write_version,
                    reclaim_below,
                ) {
                    self.push_leaf_frame_entry(
                        write_key,
                        *write_version,
                        write_value.as_deref(),
                        buffer,
                        pages,
                        written_block_ids,
                    )?;
                }
                // Either way the write replaces the existing entry: a kept
                // write supersedes it; a reclaimed delete removes it.
                write_index += 1;
                continue;
            }
            if !Self::reclaimable_tombstone(
                key,
                value.value.as_deref(),
                value.version,
                reclaim_below,
            ) {
                self.push_leaf_frame_entry(
                    key,
                    value.version,
                    value.value.as_deref(),
                    buffer,
                    pages,
                    written_block_ids,
                )?;
            }
        }
        while write_index < writes.len() {
            let (write_key, write_value, write_version) = &writes[write_index];
            if !Self::reclaimable_tombstone(
                write_key,
                write_value.as_deref(),
                *write_version,
                reclaim_below,
            ) {
                self.push_leaf_frame_entry(
                    write_key,
                    *write_version,
                    write_value.as_deref(),
                    buffer,
                    pages,
                    written_block_ids,
                )?;
            }
            write_index += 1;
        }
        Ok(())
    }

    fn pack_internal(
        &self,
        level: u8,
        children: Vec<Child>,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<Vec<Child>> {
        if level == 0 || children.is_empty() {
            return Err(invalid_data("cannot pack an undersized internal level"));
        }
        let mut groups: Vec<Vec<Child>> = Vec::new();
        let mut current: Vec<Child> = Vec::new();
        let mut used = 16;
        let mut expanded = 16;
        for child in children {
            let mut child_bytes = page_codec::internal_entry_bytes(
                current.last().map_or(&[][..], |previous| previous.lower_bound.as_slice()),
                &child.lower_bound);
            let expanded_child_bytes = 34 + child.lower_bound.len();
            if current.len() >= MIN_INTERNAL_SPLIT_CHILDREN
                && (used + child_bytes > PAGE_PACK_TARGET_BYTES
                    || expanded + expanded_child_bytes > crate::generated_tofudb_ir::MAX_ENTITY_PAGE_EXPANDED_BYTES)
            {
                groups.push(current);
                current = Vec::new();
                used = 16;
                expanded = 16;
                child_bytes = page_codec::internal_entry_bytes(&[], &child.lower_bound);
            }
            if used + child_bytes > PAGE_MAX_BYTES
                || expanded + expanded_child_bytes > crate::generated_tofudb_ir::MAX_ENTITY_PAGE_EXPANDED_BYTES {
                return Err(invalid_data("entity child cannot fit in one internal page"));
            }
            used += child_bytes;
            expanded += expanded_child_bytes;
            current.push(child);
        }
        groups.push(current);
        if groups.len() > 1 && groups.last().is_some_and(|group| group.len() == 1) {
            let mut tail = groups.pop().unwrap();
            let previous = groups.last_mut().unwrap();
            if previous.len() > MIN_INTERNAL_SPLIT_CHILDREN {
                tail.insert(0, previous.pop().unwrap());
                groups.push(tail);
            } else {
                // Three maximum-size child separators fit the format ceiling.
                // Merging avoids stealing the only child from a preceding
                // page when the target is smaller than two large separators.
                previous.extend(tail);
            }
        }
        let mut pages = Vec::with_capacity(groups.len());
        for group in groups {
            pages.push(self.store(
                &Page::Internal {
                    level,
                    children: group,
                },
                written_block_ids,
            )?);
        }
        Ok(pages)
    }

    fn apply_batch(
        &self,
        root: Option<BlockId>,
        writes: Vec<(Vec<u8>, Option<Vec<u8>>, u64)>,
        reclaim_below: TombstoneReclaim<'_>,
    ) -> io::Result<(Option<BlockId>, Vec<BlockId>)> {
        if writes.is_empty() {
            return Ok((root, Vec::new()));
        }
        let mut written_block_ids = Vec::new();
        let (mut replacements, mut level) = if let Some(root) = root {
            let root_node = self.load_node(root)?;
            let root_page = match &*root_node {
                RootNode::Page(page) => page,
                RootNode::Directory(_) => {
                    return Err(invalid_data("entity page magic mismatch"));
                }
            };
            let level = match root_page {
                Page::Leaf(_) => 0,
                Page::Internal { level, .. } => *level,
            };
            (
                self.apply_page(
                    root_page,
                    &writes,
                    reclaim_below,
                    0,
                    &mut written_block_ids,
                )?,
                level,
            )
        } else {
            let entries: Vec<(Vec<u8>, EntityValue)> = writes
                .into_iter()
                .filter(|(key, value, version)| {
                    !Self::reclaimable_tombstone(
                        key,
                        value.as_deref(),
                        *version,
                        reclaim_below,
                    )
                })
                .map(|(key, value, version)| (key, EntityValue { version, value }))
                .collect();
            if entries.is_empty() {
                (Vec::new(), 0)
            } else {
                (self.pack_leaves(entries, &mut written_block_ids)?, 0)
            }
        };
        if replacements.is_empty() {
            // Reclamation (or an all-reclaimable-delete batch) emptied the
            // tree: the root becomes None, matching the fresh-database
            // state.
            return Ok((None, written_block_ids));
        }
        while replacements.len() > 1 {
            level = level
                .checked_add(1)
                .ok_or_else(|| invalid_data("entity tree level overflow"))?;
            if level as usize > MAX_TREE_DEPTH {
                return Err(invalid_data("entity tree depth limit reached"));
            }
            replacements = self.pack_internal(level, replacements, &mut written_block_ids)?;
        }
        Ok((Some(replacements[0].block_id), written_block_ids))
    }

    fn apply_page(
        &self,
        page: &Page,
        writes: &[(Vec<u8>, Option<Vec<u8>>, u64)],
        reclaim_below: TombstoneReclaim<'_>,
        depth: usize,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<Vec<Child>> {
        if depth > MAX_TREE_DEPTH || writes.is_empty() {
            return Err(invalid_data("invalid entity batch recursion"));
        }
        match page {
            Page::Leaf(entries) => {
                let mut pages = Vec::new();
                let mut buffer = LeafFrame::new();
                self.append_merged_leaf_entries(
                    entries,
                    writes,
                    reclaim_below,
                    &mut buffer,
                    &mut pages,
                    written_block_ids,
                )?;
                self.flush_leaf_frame(&mut buffer, &mut pages, written_block_ids)?;
                Ok(pages)
            }
            Page::Internal { level, children } => {
                if depth == MAX_TREE_DEPTH {
                    return Err(invalid_data("invalid entity batch recursion"));
                }
                // The sorted write batch maps to a sparse, nondecreasing run
                // of child indexes; only those children are reloaded and
                // repacked, the rest pass through by reference.
                let mut next_children = Vec::with_capacity(children.len());
                let mut leaf_buffer = LeafFrame::new();
                let mut write_index = 0;
                for (child_index, child) in children.iter().enumerate() {
                    let group_start = write_index;
                    while write_index < writes.len() && {
                        let index = children.partition_point(|candidate| {
                            candidate.lower_bound.as_slice() <= writes[write_index].0.as_slice()
                        });
                        index.saturating_sub(1) == child_index
                    } {
                        write_index += 1;
                    }
                    if group_start == write_index {
                        self.flush_leaf_frame(
                            &mut leaf_buffer,
                            &mut next_children,
                            written_block_ids,
                        )?;
                        next_children.push(child.clone());
                        continue;
                    }
                    let child_node = self.load_node(child.block_id)?;
                    let child_page = match &*child_node {
                        RootNode::Page(page) => page,
                        RootNode::Directory(_) => {
                            return Err(invalid_data("entity page magic mismatch"));
                        }
                    };
                    if let Page::Leaf(entries) = child_page {
                        // Repack an adjacent modified run before storing pages.
                        // Sub-watermark tombstones are reclaimed here (see
                        // append_merged_leaf_entries); a run that reclaims
                        // down to nothing drops its child reference.
                        self.append_merged_leaf_entries(
                            entries,
                            &writes[group_start..write_index],
                            reclaim_below,
                            &mut leaf_buffer,
                            &mut next_children,
                            written_block_ids,
                        )?;
                    } else {
                        self.flush_leaf_frame(
                            &mut leaf_buffer,
                            &mut next_children,
                            written_block_ids,
                        )?;
                        next_children.extend(self.apply_page(
                            child_page,
                            &writes[group_start..write_index],
                            reclaim_below,
                            depth + 1,
                            written_block_ids,
                        )?);
                    }
                }
                self.flush_leaf_frame(&mut leaf_buffer, &mut next_children, written_block_ids)?;
                if next_children.is_empty() {
                    // Reclamation emptied this subtree; the caller drops the
                    // reference to it.
                    return Ok(Vec::new());
                }
                self.pack_internal(*level, next_children, written_block_ids)
            }
        }
    }

    /// Tombstone-compaction walker (L2): descends the subtree covering
    /// `[start, end)`, repacks every visited leaf whose entries include a
    /// sub-watermark tombstone (dropping exactly those entries), and
    /// collapses children that empty out. Stops between leaves once the
    /// leaf budget is spent and records the first unvisited key, so one
    /// call is always a bounded slice. Returns (replacements, changed) in
    /// the same shape as `apply_page` so the caller can cascade the root.
    /// `lower_bound` is the subtree's separator key as stored by its parent
    /// (the tree invariant "child.lower_bound == subtree's first key"), so
    /// unchanged subtrees echo it without a node load.
    fn reclaim_subtree(
        &self,
        root: BlockId,
        lower_bound: &[u8],
        start: &[u8],
        end: &[u8],
        reclaim_below: TombstoneReclaim<'_>,
        budget: &mut ReclaimBudget,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<(Vec<Child>, bool)> {
        if budget.leaves_left == 0 {
            if budget.resume.is_none() {
                budget.resume = Some(lower_bound.max(start).to_vec());
            }
            return Ok((
                vec![Child {
                    lower_bound: lower_bound.to_vec(),
                    block_id: root,
                }],
                false,
            ));
        }
        let node = self.load_node(root)?;
        match &*node {
            RootNode::Directory(_) => Err(invalid_data(
                "tombstone compaction must be called on a base root page",
            )),
            RootNode::Page(Page::Leaf(entries)) => {
                budget.leaves_left -= 1;
                let mut kept: Vec<(Vec<u8>, EntityValue)> = Vec::with_capacity(entries.len());
                let mut reclaimed_here = 0_u32;
                for (key, value) in entries {
                    if Self::reclaimable_tombstone(
                        key,
                        value.value.as_deref(),
                        value.version,
                        reclaim_below,
                    ) {
                        reclaimed_here += 1;
                    } else {
                        kept.push((key.clone(), value.clone()));
                    }
                }
                if reclaimed_here == 0 {
                    return Ok((
                        vec![Child {
                            lower_bound: lower_bound.to_vec(),
                            block_id: root,
                        }],
                        false,
                    ));
                }
                budget.reclaimed = budget.reclaimed.saturating_add(reclaimed_here);
                if kept.is_empty() {
                    return Ok((Vec::new(), true));
                }
                let children = self.pack_leaves(kept, written_block_ids)?;
                Ok((children, true))
            }
            RootNode::Page(Page::Internal { level, children }) => {
                let mut next_children = Vec::with_capacity(children.len());
                let mut changed = false;
                for (index, child) in children.iter().enumerate() {
                    let child_upper = children
                        .get(index + 1)
                        .map(|next| next.lower_bound.as_slice())
                        .unwrap_or(end);
                    let overlaps = child_upper > start && child.lower_bound.as_slice() < end;
                    if !overlaps {
                        next_children.push(child.clone());
                        continue;
                    }
                    let (replacements, child_changed) = self.reclaim_subtree(
                        child.block_id,
                        &child.lower_bound,
                        start,
                        end,
                        reclaim_below,
                        budget,
                        written_block_ids,
                    )?;
                    changed |= child_changed;
                    next_children.extend(replacements);
                }
                if !changed {
                    return Ok((
                        vec![Child {
                            lower_bound: lower_bound.to_vec(),
                            block_id: root,
                        }],
                        false,
                    ));
                }
                if next_children.is_empty() {
                    return Ok((Vec::new(), true));
                }
                let packed = self.pack_internal(*level, next_children, written_block_ids)?;
                Ok((packed, true))
            }
        }
    }

    fn retire_ranges(
        &self,
        root: BlockId,
        ranges: &[(Vec<u8>, Vec<u8>)],
    ) -> io::Result<(Option<BlockId>, Vec<BlockId>, bool)> {
        let page = self.load(root)?;
        let level = page.level();
        let mut written_block_ids = Vec::new();
        let (mut replacements, changed) =
            self.retire_page(root, page, ranges, None, None, 0, &mut written_block_ids)?;
        if !changed {
            return Ok((Some(root), written_block_ids, false));
        }
        if replacements.is_empty() {
            return Ok((None, written_block_ids, true));
        }
        let mut next_level = level;
        while replacements.len() > 1 {
            next_level = next_level
                .checked_add(1)
                .ok_or_else(|| invalid_data("entity tree level overflow"))?;
            if next_level as usize > MAX_TREE_DEPTH {
                return Err(invalid_data("entity tree depth limit reached"));
            }
            replacements = self.pack_internal(next_level, replacements, &mut written_block_ids)?;
        }
        Ok((Some(replacements[0].block_id), written_block_ids, true))
    }

    fn extract_ranges(
        &self,
        root: BlockId,
        ranges: &[(Vec<u8>, Vec<u8>)],
    ) -> io::Result<(BlockId, Vec<BlockId>)> {
        match self.load_root(root)? {
            RootNode::Page(page) => self.extract_page_ranges(root, page, ranges),
            RootNode::Directory(directory) => {
                let mut mounts = Vec::new();
                for mount in directory.mounts {
                    for (start, end) in ranges {
                        let intersection_start = mount.start.as_slice().max(start.as_slice());
                        let intersection_end = mount.end.as_slice().min(end.as_slice());
                        if intersection_start < intersection_end {
                            mounts.push(RangeMount {
                                start: intersection_start.to_vec(),
                                end: intersection_end.to_vec(),
                                root: mount.root,
                            });
                        }
                    }
                }
                mounts.sort_by(|left, right| left.start.cmp(&right.start));
                let mut coalesced: Vec<RangeMount> = Vec::new();
                for mount in mounts {
                    if let Some(previous) = coalesced.last_mut() {
                        if previous.root == mount.root && previous.end == mount.start {
                            previous.end = mount.end;
                            continue;
                        }
                    }
                    coalesced.push(mount);
                }
                let mounts = coalesced;
                if mounts.len() > MAX_ENTITY_ROOT_RANGE_MOUNTS {
                    return Err(io::Error::new(
                        io::ErrorKind::WouldBlock,
                        "entity range capsule would exceed the root mount bound",
                    ));
                }
                let mut written_block_ids = Vec::new();
                let base_root = if let Some(base_root) = directory.base_root {
                    match self.extract_ranges(base_root, ranges) {
                        Ok((root, blocks)) => {
                            written_block_ids.extend(blocks);
                            Some(root)
                        }
                        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                        Err(error) => return Err(error),
                    }
                } else {
                    None
                };
                if base_root.is_none() && mounts.is_empty() {
                    return Err(io::Error::new(
                        io::ErrorKind::NotFound,
                        "persistent entity range snapshot is empty",
                    ));
                }
                if mounts.is_empty() {
                    return Ok((base_root.unwrap(), written_block_ids));
                }
                let capsule_root = self.store_directory(
                    &RootDirectory { base_root, mounts, materialized: None },
                    &mut written_block_ids,
                )?;
                Ok((capsule_root, written_block_ids))
            }
        }
    }

    fn extract_page_ranges(
        &self,
        root: BlockId,
        page: Page,
        ranges: &[(Vec<u8>, Vec<u8>)],
    ) -> io::Result<(BlockId, Vec<BlockId>)> {
        let mut written_block_ids = Vec::new();
        let mut fragments =
            self.extract_page(root, page, ranges, None, None, 0, &mut written_block_ids)?;
        if fragments.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "persistent entity range snapshot is empty",
            ));
        }
        let target_level = fragments
            .iter()
            .map(|fragment| fragment.level)
            .max()
            .unwrap();
        for fragment in &mut fragments {
            while fragment.level < target_level {
                let level = fragment.level + 1;
                fragment.child = self.store(
                    &Page::Internal {
                        level,
                        children: vec![fragment.child.clone()],
                    },
                    &mut written_block_ids,
                )?;
                fragment.level = level;
            }
        }
        let mut children = fragments
            .into_iter()
            .map(|fragment| fragment.child)
            .collect::<Vec<_>>();
        if children.len() == 1 {
            return Ok((children[0].block_id, written_block_ids));
        }
        let mut root_level = target_level
            .checked_add(1)
            .ok_or_else(|| invalid_data("entity range snapshot level overflow"))?;
        if root_level as usize > MAX_TREE_DEPTH {
            return Err(invalid_data("entity range snapshot depth limit reached"));
        }
        children = self.pack_internal(root_level, children, &mut written_block_ids)?;
        while children.len() > 1 {
            root_level = root_level
                .checked_add(1)
                .ok_or_else(|| invalid_data("entity range snapshot level overflow"))?;
            if root_level as usize > MAX_TREE_DEPTH {
                return Err(invalid_data("entity range snapshot depth limit reached"));
            }
            children = self.pack_internal(root_level, children, &mut written_block_ids)?;
        }
        Ok((children[0].block_id, written_block_ids))
    }

    #[allow(clippy::too_many_arguments)]
    fn extract_page(
        &self,
        id: BlockId,
        page: Page,
        ranges: &[(Vec<u8>, Vec<u8>)],
        lower_bound: Option<&[u8]>,
        upper_bound: Option<&[u8]>,
        depth: usize,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<Vec<PageFragment>> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        let level = page.level();
        if ranges_fully_cover(ranges, lower_bound, upper_bound) {
            return Ok(vec![PageFragment {
                child: Child {
                    lower_bound: page.first_key()?,
                    block_id: id,
                },
                level,
            }]);
        }
        if !ranges_overlap(ranges, lower_bound, upper_bound) {
            return Ok(Vec::new());
        }
        match page {
            Page::Leaf(entries) => {
                let retained = entries
                    .into_iter()
                    .filter(|(key, _)| {
                        ranges.iter().any(|(start, end)| {
                            key.as_slice() >= start.as_slice() && key.as_slice() < end.as_slice()
                        })
                    })
                    .collect::<Vec<_>>();
                if retained.is_empty() {
                    return Ok(Vec::new());
                }
                self.pack_leaves(retained, written_block_ids)
                    .map(|children| {
                        children
                            .into_iter()
                            .map(|child| PageFragment { child, level: 0 })
                            .collect()
                    })
            }
            Page::Internal { children, .. } => {
                let mut fragments = Vec::new();
                for (index, child) in children.iter().enumerate() {
                    let child_upper = children
                        .get(index + 1)
                        .map(|next| next.lower_bound.as_slice())
                        .or(upper_bound);
                    if ranges_fully_cover(ranges, Some(child.lower_bound.as_slice()), child_upper) {
                        fragments.push(PageFragment {
                            child: child.clone(),
                            level: level - 1,
                        });
                    } else if ranges_overlap(
                        ranges,
                        Some(child.lower_bound.as_slice()),
                        child_upper,
                    ) {
                        fragments.extend(self.extract_page(
                            child.block_id,
                            self.load(child.block_id)?,
                            ranges,
                            Some(child.lower_bound.as_slice()),
                            child_upper,
                            depth + 1,
                            written_block_ids,
                        )?);
                    }
                }
                Ok(fragments)
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn retire_page(
        &self,
        id: BlockId,
        page: Page,
        ranges: &[(Vec<u8>, Vec<u8>)],
        lower_bound: Option<&[u8]>,
        upper_bound: Option<&[u8]>,
        depth: usize,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<(Vec<Child>, bool)> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        if ranges_fully_cover(ranges, lower_bound, upper_bound) {
            return Ok((Vec::new(), true));
        }
        if !ranges_overlap(ranges, lower_bound, upper_bound) {
            return Ok((
                vec![Child {
                    lower_bound: page.first_key()?,
                    block_id: id,
                }],
                false,
            ));
        }
        match page {
            Page::Leaf(entries) => {
                let original_len = entries.len();
                let retained = entries
                    .into_iter()
                    .filter(|(key, _)| {
                        !ranges.iter().any(|(start, end)| {
                            key.as_slice() >= start.as_slice() && key.as_slice() < end.as_slice()
                        })
                    })
                    .collect::<Vec<_>>();
                if retained.len() == original_len {
                    return Ok((
                        vec![Child {
                            lower_bound: retained[0].0.clone(),
                            block_id: id,
                        }],
                        false,
                    ));
                }
                if retained.is_empty() {
                    Ok((Vec::new(), true))
                } else {
                    self.pack_leaves(retained, written_block_ids)
                        .map(|children| (children, true))
                }
            }
            Page::Internal { level, children } => {
                let mut next_children = Vec::new();
                let mut changed = false;
                for (index, child) in children.iter().enumerate() {
                    let child_upper = children
                        .get(index + 1)
                        .map(|next| next.lower_bound.as_slice())
                        .or(upper_bound);
                    if ranges_fully_cover(ranges, Some(child.lower_bound.as_slice()), child_upper) {
                        changed = true;
                        continue;
                    }
                    if !ranges_overlap(ranges, Some(child.lower_bound.as_slice()), child_upper) {
                        next_children.push(child.clone());
                        continue;
                    }
                    let child_page = self.load(child.block_id)?;
                    let (replacements, child_changed) = self.retire_page(
                        child.block_id,
                        child_page,
                        ranges,
                        Some(&child.lower_bound),
                        child_upper,
                        depth + 1,
                        written_block_ids,
                    )?;
                    changed |= child_changed;
                    next_children.extend(replacements);
                }
                if !changed {
                    return Ok((
                        vec![Child {
                            lower_bound: next_children[0].lower_bound.clone(),
                            block_id: id,
                        }],
                        false,
                    ));
                }
                if next_children.is_empty() {
                    Ok((Vec::new(), true))
                } else {
                    self.pack_internal(level, next_children, written_block_ids)
                        .map(|children| (children, true))
                }
            }
        }
    }

    /// Physical leaf rewrites can belong to another logical range or to an
    /// overlay merge. Resolve versions only inside the witnessed range before
    /// treating such a rewrite as a conflict. Retained tombstones preserve ABA;
    /// mounted directories conservatively retain structural validation.
    fn range_versions_unchanged(
        &self, witness: &RangeWitness, current_leaves: &[BlockId],
        snapshot: EntitySnapshot, head: EntitySnapshot,
        before: &EntityOverlayView, after: &EntityOverlayView,
    ) -> io::Result<bool> {
        for id in current_leaves {
            match &*self.load_node(*id)? {
                RootNode::Directory(directory) if directory.mounts.is_empty() => {},
                RootNode::Page(Page::Leaf(entries)) => {
                    // Include tombstones and the boundary leaf's whole range:
                    // inserts/updates/deletes after the snapshot still conflict.
                    if entries.iter().any(|(key, value)| key.as_slice() >= witness.start.as_slice()
                        && key.as_slice() < witness.end.as_slice() && value.version > snapshot.sequence) {
                        return Ok(false);
                    }
                },
                _ => return Ok(false),
            }
        }
        for id in &witness.leaf_ids {
            match &*self.load_node(*id)? {
                RootNode::Directory(directory) if directory.mounts.is_empty() => {},
                RootNode::Page(Page::Leaf(entries)) => {
                    // Retire/replace may physically remove an old row. Check
                    // its original logical version too, with at most one
                    // page's key/version vectors live at a time.
                    let keys: Vec<_> = entries.iter().filter(|(key, _)|
                        key.as_slice() >= witness.start.as_slice() && key.as_slice() < witness.end.as_slice())
                        .map(|(key, value)| (key.as_slice(), value.version)).collect();
                    let raw_keys: Vec<_> = keys.iter().map(|(key, _)| *key).collect();
                    let current = self.versions(head.root, &raw_keys)?;
                    for ((key, version), current) in keys.into_iter().zip(current) {
                        let expected = before.resolve_version(key, snapshot.sequence).or(Some(version));
                        let actual = after.resolve_version(key, head.sequence).or(current);
                        if actual != expected { return Ok(false); }
                    }
                },
                _ => return Ok(false),
            }
        }
        self.overlay_range_versions_unchanged(witness, snapshot, head, before, after)
    }

    fn overlay_range_versions_unchanged(
        &self, witness: &RangeWitness, snapshot: EntitySnapshot, head: EntitySnapshot,
        before: &EntityOverlayView, after: &EntityOverlayView,
    ) -> io::Result<bool> {
        // A structural range retirement can remove rows that existed only
        // in the snapshot overlay. They have no old tree leaf to inspect.
        let mut pending = before.range(&witness.start, &witness.end, snapshot.sequence, false);
        loop {
            let keys: Vec<_> = pending.by_ref().take(256).map(|(key, _)| key.as_slice()).collect();
            if keys.is_empty() { break; }
            let current = self.versions(head.root, &keys)?;
            for (key, current) in keys.into_iter().zip(current) {
                if after.resolve_version(key, head.sequence).or(current)
                    != before.resolve_version(key, snapshot.sequence) { return Ok(false); }
            }
        }
        Ok(true)
    }

    fn scan(
        &self,
        root: Option<BlockId>,
        start: &[u8],
        end: &[u8],
        limit: usize,
        dead_leaves: &mut u32,
    ) -> io::Result<ScanResult> {
        if start >= end || limit == 0 {
            return Err(invalid_input("invalid or unbounded entity range"));
        }
        let mut rows = Vec::new();
        let mut leaves = Vec::new();
        if let Some(root) = root {
            rows = self.scan_root(root, start, end, limit, &mut leaves, dead_leaves, 0, false)?;
        }
        Ok((rows, leaves))
    }

    #[allow(clippy::too_many_arguments)]
    fn scan_root(
        &self,
        root: BlockId,
        start: &[u8],
        end: &[u8],
        limit: usize,
        leaves: &mut Vec<BlockId>,
        dead_leaves: &mut u32,
        directory_depth: usize,
        reverse: bool,
    ) -> io::Result<Vec<(Vec<u8>, EntityValue)>> {
        if directory_depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity root directory exceeds maximum depth"));
        }
        let node = self.load_node(root)?;
        match &*node {
            RootNode::Page(_) => {
                let mut rows = Vec::new();
                if reverse {
                    self.scan_page_reverse(root, start, end, limit, &mut rows, leaves, dead_leaves, 0)?;
                } else {
                    self.scan_page(root, start, end, limit, &mut rows, leaves, dead_leaves, 0)?;
                }
                Ok(rows)
            }
            RootNode::Directory(directory) => {
                leaves.try_reserve(1).map_err(|error|
                    io::Error::new(io::ErrorKind::OutOfMemory, error))?;
                leaves.push(root);
                let mut visible = BTreeMap::new();
                if let Some(base_root) = directory.base_root {
                    for (key, value) in self.scan_root(
                        base_root,
                        start,
                        end,
                        limit,
                        leaves,
                        dead_leaves,
                        directory_depth + 1,
                        reverse,
                    )? {
                        visible.insert(key, value);
                    }
                }
                let mounts: Box<dyn Iterator<Item = &RangeMount>> = if reverse {
                    Box::new(directory.mounts.iter().rev())
                } else {
                    Box::new(directory.mounts.iter())
                };
                for mount in mounts {
                    // Limit-aware early exit (2026-09-22 retention deadlock):
                    // mounts are stored in ascending, non-overlapping order
                    // (RootDirectory::validate), and the merge answer is the
                    // best `limit` keys of the union. Once the window is
                    // full, a source whose whole range lies at or beyond the
                    // window's current cut cannot displace any observable
                    // row, and neither can any later source in the same
                    // iteration order — scanning it would only grow the
                    // witness despite lying outside the requested window,
                    // which deadlocked bounded retention prunes against a
                    // large mounted backlog. Equality is safe: a key equal
                    // to the cut is the cut row's own duplicate, which
                    // base_overrides / non-overlap already settle, and a
                    // scan that returns fewer than `limit` rows never exits
                    // early, so range-exhaustion proofs keep their full
                    // witness. The traversal decision depends only on
                    // content visible to both the recording and the
                    // commit-time verification scan, so witness comparison
                    // stays deterministic; a change beyond the observed
                    // window cannot alter the top-`limit` answer.
                    if visible.len() >= limit {
                        let beyond = if reverse {
                            visible
                                .keys()
                                .next()
                                .is_some_and(|cut| mount.end.as_slice() <= cut.as_slice())
                        } else {
                            visible
                                .keys()
                                .next_back()
                                .is_some_and(|cut| mount.start.as_slice() >= cut.as_slice())
                        };
                        if beyond {
                            break;
                        }
                    }
                    let mount_start = start.max(mount.start.as_slice());
                    let mount_end = end.min(mount.end.as_slice());
                    if mount_start >= mount_end {
                        continue;
                    }
                    for (key, value) in self.scan_root(
                        mount.root,
                        mount_start,
                        mount_end,
                        limit,
                        leaves,
                        dead_leaves,
                        directory_depth + 1,
                        reverse,
                    )? {
                        let base_overrides = if let Some(base_root) = directory.base_root {
                            self.get_root(base_root, &key, directory_depth + 1)?
                                .is_some()
                        } else {
                            false
                        };
                        if !base_overrides {
                            visible.insert(key, value);
                        }
                    }
                    // Keep the merge window tight: rows outside the best
                    // `limit` can never reach the caller, and a tight cut is
                    // what unlocks the early exit above.
                    while visible.len() > limit {
                        if reverse {
                            visible.pop_first();
                        } else {
                            visible.pop_last();
                        }
                    }
                }
                let iterator: Box<dyn Iterator<Item = (Vec<u8>, EntityValue)>> = if reverse {
                    Box::new(visible.into_iter().rev())
                } else {
                    Box::new(visible.into_iter())
                };
                Ok(iterator.take(limit).collect())
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn scan_page(
        &self,
        id: BlockId,
        start: &[u8],
        end: &[u8],
        limit: usize,
        rows: &mut Vec<(Vec<u8>, EntityValue)>,
        leaves: &mut Vec<BlockId>,
        dead_leaves: &mut u32,
        depth: usize,
    ) -> io::Result<()> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        match &*self.load_node(id)? {
            RootNode::Page(Page::Leaf(entries)) => {
                leaves.try_reserve(1).map_err(|error|
                    io::Error::new(io::ErrorKind::OutOfMemory, error))?;
                leaves.push(id);
                let mut tombstones_in_range = 0_u32;
                let mut live_in_range = 0_u32;
                for (key, value) in entries {
                    if key.as_slice() >= start && key.as_slice() < end {
                        if value.value.is_some() {
                            live_in_range += 1;
                            if rows.len() < limit {
                                rows.push((key.clone(), value.clone()));
                            }
                        } else {
                            tombstones_in_range += 1;
                        }
                    }
                }
                // A leaf whose in-range entries are all tombstones is dead
                // weight on every scan that crosses it; count it for the
                // compaction-hint channel (L2).
                if tombstones_in_range > 0 && live_in_range == 0 {
                    *dead_leaves = dead_leaves.saturating_add(1);
                }
            }
            RootNode::Page(Page::Internal { children, .. }) => {
                for (index, child) in children.iter().enumerate() {
                    if rows.len() == limit {
                        break;
                    }
                    let upper = children
                        .get(index + 1)
                        .map(|next| next.lower_bound.as_slice());
                    let overlaps = upper.is_none_or(|bound| bound > start)
                        && child.lower_bound.as_slice() < end;
                    if overlaps {
                        self.scan_page(child.block_id, start, end, limit, rows, leaves, dead_leaves, depth + 1)?;
                    }
                }
            }
            RootNode::Directory(_) => return Err(invalid_data("entity page magic mismatch")),
        }
        Ok(())
    }

    fn scan_reverse(
        &self,
        root: Option<BlockId>,
        start: &[u8],
        end: &[u8],
        limit: usize,
        dead_leaves: &mut u32,
    ) -> io::Result<ScanResult> {
        if start >= end || limit == 0 {
            return Err(invalid_input("invalid or unbounded reverse entity range"));
        }
        let mut rows = Vec::new();
        let mut leaves = Vec::new();
        if let Some(root) = root {
            rows = self.scan_root(root, start, end, limit, &mut leaves, dead_leaves, 0, true)?;
        }
        Ok((rows, leaves))
    }

    #[allow(clippy::too_many_arguments)]
    fn scan_page_reverse(
        &self,
        id: BlockId,
        start: &[u8],
        end: &[u8],
        limit: usize,
        rows: &mut Vec<(Vec<u8>, EntityValue)>,
        leaves: &mut Vec<BlockId>,
        dead_leaves: &mut u32,
        depth: usize,
    ) -> io::Result<()> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        match &*self.load_node(id)? {
            RootNode::Page(Page::Leaf(entries)) => {
                leaves.try_reserve(1).map_err(|error|
                    io::Error::new(io::ErrorKind::OutOfMemory, error))?;
                leaves.push(id);
                let mut tombstones_in_range = 0_u32;
                let mut live_in_range = 0_u32;
                for (key, value) in entries.iter().rev() {
                    if key.as_slice() >= start && key.as_slice() < end {
                        if value.value.is_some() {
                            live_in_range += 1;
                            if rows.len() < limit {
                                rows.push((key.clone(), value.clone()));
                            }
                        } else {
                            tombstones_in_range += 1;
                        }
                    }
                }
                if tombstones_in_range > 0 && live_in_range == 0 {
                    *dead_leaves = dead_leaves.saturating_add(1);
                }
            }
            RootNode::Page(Page::Internal { children, .. }) => {
                for (index, child) in children.iter().enumerate().rev() {
                    if rows.len() == limit {
                        break;
                    }
                    let upper = children
                        .get(index + 1)
                        .map(|next| next.lower_bound.as_slice());
                    let overlaps = upper.is_none_or(|bound| bound > start)
                        && child.lower_bound.as_slice() < end;
                    if overlaps {
                        self.scan_page_reverse(
                            child.block_id,
                            start,
                            end,
                            limit,
                            rows,
                            leaves,
                            dead_leaves,
                            depth + 1,
                        )?;
                    }
                }
            }
            RootNode::Directory(_) => return Err(invalid_data("entity page magic mismatch")),
        }
        Ok(())
    }
}

fn is_complete_root_record(payload: &[u8]) -> bool {
    payload.len() >= 12 && &payload[..8] == ROOT_MAGIC
        && u32::from_le_bytes(payload[8..12].try_into().unwrap()) == ROOT_VERSION
}

fn encode_root(sequence: u64, root: Option<BlockId>) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(8 + 4 + 8 + 1 + 32);
    bytes.extend_from_slice(ROOT_MAGIC);
    bytes.extend_from_slice(&ROOT_VERSION.to_le_bytes());
    bytes.extend_from_slice(&sequence.to_le_bytes());
    bytes.push(u8::from(root.is_some()));
    if let Some(root) = root {
        bytes.extend_from_slice(&root.0);
    }
    bytes
}

impl EntityDatabase {
    pub(crate) fn authorize_additional_scope_prefix(
        &self,
        transaction: &mut EntityTransaction,
        prefix: Vec<u8>,
    ) -> io::Result<()> {
        if prefix.len() < 17
            || u64::from_be_bytes(prefix[..8].try_into().unwrap()) != transaction.tenant_id
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "additional entity scope escaped its transaction tenant",
            ));
        }
        if !transaction
            .additional_scope_prefixes
            .iter()
            .any(|existing| existing == &prefix)
        {
            transaction.additional_scope_prefixes.push(prefix);
        }
        Ok(())
    }

    pub fn initialize(data_dir: &std::path::Path) -> io::Result<Self> {
        Self::initialize_with_engine(Engine::initialize(data_dir)?)
    }

    pub(crate) fn initialize_with_engine(engine: Engine) -> io::Result<Self> {
        Ok(Self {
            persona: EntityPersona::Writer(engine),
            root: None,
            database_instance_id: Uuid::new_v4(),
            snapshot_pins: Default::default(),
            compaction_hints: Default::default(),
            tombstone_reclaim_override: None,
            page_cache: Arc::new(std::sync::RwLock::new(DecodedPageCache::new(DEFAULT_PAGE_CACHE_BYTES))),
            replica_pin: None,
            query_publisher: None,
            overlay: Arc::new(std::sync::RwLock::new(Default::default())),
            overlay_trigger_bytes: std::sync::atomic::AtomicUsize::new(0),
            overlay_trigger_entries: std::sync::atomic::AtomicUsize::new(0),
            overlay_enabled: write_overlay_enabled(),
            overlay_budget_bytes: overlay_budget_bytes(),
            overlay_wal_watermark_bytes: overlay_wal_watermark_bytes(),
            merge_chunk_entries_adaptive: std::sync::atomic::AtomicUsize::new(
                Self::MERGE_CHUNK_ENTRIES_INITIAL,
            ),
        })
    }

    pub fn initialize_with_vfs(data_dir: &std::path::Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Ok(Self {
            persona: EntityPersona::Writer(Engine::initialize_with_vfs(data_dir, vfs)?),
            root: None,
            database_instance_id: Uuid::new_v4(),
            snapshot_pins: Default::default(),
            compaction_hints: Default::default(),
            tombstone_reclaim_override: None,
            page_cache: Arc::new(std::sync::RwLock::new(DecodedPageCache::new(DEFAULT_PAGE_CACHE_BYTES))),
            replica_pin: None,
            query_publisher: None,
            overlay: Arc::new(std::sync::RwLock::new(Default::default())),
            overlay_trigger_bytes: std::sync::atomic::AtomicUsize::new(0),
            overlay_trigger_entries: std::sync::atomic::AtomicUsize::new(0),
            overlay_enabled: write_overlay_enabled(),
            overlay_budget_bytes: overlay_budget_bytes(),
            overlay_wal_watermark_bytes: overlay_wal_watermark_bytes(),
            merge_chunk_entries_adaptive: std::sync::atomic::AtomicUsize::new(
                Self::MERGE_CHUNK_ENTRIES_INITIAL,
            ),
        })
    }

    pub fn open(data_dir: &std::path::Path) -> io::Result<Self> {
        Self::open_engine(Engine::open(data_dir)?)
    }

    /// Open with the authority's at-rest key, like [`Engine::open_with_key`].
    pub fn open_with_key(
        data_dir: &std::path::Path,
        key: Option<crate::at_rest::AtRestKey>,
    ) -> io::Result<Self> {
        Self::open_engine(Engine::open_with_key(data_dir, key)?)
    }

    pub fn open_with_vfs(data_dir: &std::path::Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Self::open_engine(Engine::open_with_vfs(data_dir, vfs)?)
    }

    pub(crate) fn open_engine(engine: Engine) -> io::Result<Self> {
        let durable_sequence = engine.state().durable_sequence;
        let root = match engine.state().authority_state_root {
            Some(root) => root,
            None if durable_sequence == 0 => None,
            None => engine
                .transaction_at(durable_sequence)?
                .ok_or_else(|| invalid_data("latest authority transaction is missing"))?
                .envelope
                .authority_state_update
                .ok_or_else(|| invalid_data("latest transaction has no authority state root"))?,
        };
        if let Some(root) = root {
            Tree {
                backend: &engine,
                page_cache: None,
            }
            .load_root(root)?;
        }
        let overlay_enabled = write_overlay_enabled();
        let database = Self {
            persona: EntityPersona::Writer(engine),
            root,
            database_instance_id: Uuid::new_v4(),
            snapshot_pins: Default::default(),
            compaction_hints: Default::default(),
            tombstone_reclaim_override: None,
            page_cache: Arc::new(std::sync::RwLock::new(DecodedPageCache::new(DEFAULT_PAGE_CACHE_BYTES))),
            replica_pin: None,
            query_publisher: None,
            overlay: Arc::new(std::sync::RwLock::new(Default::default())),
            overlay_trigger_bytes: std::sync::atomic::AtomicUsize::new(0),
            overlay_trigger_entries: std::sync::atomic::AtomicUsize::new(0),
            overlay_enabled,
            overlay_budget_bytes: overlay_budget_bytes(),
            overlay_wal_watermark_bytes: overlay_wal_watermark_bytes(),
            merge_chunk_entries_adaptive: std::sync::atomic::AtomicUsize::new(
                Self::MERGE_CHUNK_ENTRIES_INITIAL,
            ),
        };
        // Replayed deltas are committed data regardless of the write-mode
        // flag: reads merge the store whenever it is non-empty.
        database.replay_overlay_deltas()?;
        Ok(database)
    }

    /// Overrides the decoded entity-page cache budget.  The default serves
    /// personal-mode authorities; embedders with tighter memory budgets lower
    /// this before serving traffic, and tests use it to exercise eviction.
    pub fn set_page_cache_capacity_bytes(&self, capacity_bytes: usize) -> io::Result<()> {
        self.page_cache
            .write()
            .map_err(|_| io::Error::other("entity page cache lock was poisoned"))?
            .set_capacity_bytes(capacity_bytes);
        Ok(())
    }

    // ── Persona helpers (writer / read replica) ──

    /// The block backend behind every tree walk: the live engine for the
    /// writer, the shared verified block store for a read replica.
    pub(crate) fn backend(&self) -> &dyn EntityBlockIo {
        match &self.persona {
            EntityPersona::Writer(engine) => engine,
            EntityPersona::Replica(core) => &core.blocks,
        }
    }

    /// Persona-safe liveness gate for read paths. The writer consults the
    /// engine (restart latch plus WAL-barrier health); the replica checks
    /// the shared restart flag captured at build time.
    pub(crate) fn require_usable_authority(&self) -> io::Result<()> {
        match &self.persona {
            EntityPersona::Writer(engine) => engine.require_usable_authority(),
            EntityPersona::Replica(core) => core.blocks.check_live(),
        }
    }

    /// The snapshot a new transaction pins: the live head for the writer,
    /// the build-time frozen head for a replica.
    pub fn current_snapshot(&self) -> EntitySnapshot {
        match &self.persona {
            EntityPersona::Writer(engine) => EntitySnapshot {
                sequence: engine.state().durable_sequence,
                root: self.root,
            },
            EntityPersona::Replica(core) => core.captured,
        }
    }

    pub(crate) fn block_store_shared(&self) -> Arc<crate::block::BlockStore> {
        match &self.persona {
            EntityPersona::Writer(engine) => engine.block_store_shared(),
            EntityPersona::Replica(core) => Arc::clone(&core.blocks.blocks),
        }
    }

    /// Stable durable identity of the authority this database serves.
    pub(crate) fn authority_uuid(&self) -> Uuid {
        match &self.persona {
            EntityPersona::Writer(engine) => engine.state().authority_uuid,
            EntityPersona::Replica(core) => core.authority_uuid,
        }
    }

    pub(crate) fn is_restart_required(&self) -> bool {
        match &self.persona {
            EntityPersona::Writer(engine) => engine.is_restart_required(),
            EntityPersona::Replica(core) => core
                .blocks
                .restart_required
                .load(std::sync::atomic::Ordering::Relaxed),
        }
    }

    /// Blob open-time atomic-commit witness. The writer persona clones the
    /// current committed-transaction list; the replica returns `None`, which
    /// selects reachable-open semantics (owner-scope check and per-chunk
    /// content verification are unchanged; the staged-blob witness is
    /// skipped). Sound because blocks are content-addressed and blob
    /// references are in practice derived from tree state.
    pub(crate) fn commit_witness(
        &self,
    ) -> io::Result<Option<Vec<crate::engine::CommittedTransaction>>> {
        match &self.persona {
            EntityPersona::Writer(engine) => Ok(Some(engine.transaction_snapshot()?)),
            EntityPersona::Replica(_) => Ok(None),
        }
    }

    /// `true` when this database is a snapshot read replica.
    pub(crate) fn is_read_replica(&self) -> bool {
        matches!(self.persona, EntityPersona::Replica(_))
    }

    /// Builds the read-replica persona under the caller's authority-mutex
    /// hold: a handful of Arc clones plus one registered reader pin —
    /// microseconds, never any mount I/O. The pin registers BEFORE the hold
    /// is released, so authority GC can never reclaim the snapshot's pages
    /// in the gap. Each live replica owns its root until its final lease drops.
    pub(crate) fn build_read_replica(&self) -> io::Result<EntityDatabase> {
        let engine = self.writer_engine()?;
        engine.require_usable_authority()?;
        let captured = self.current_snapshot();
        let pin = pin_snapshot(&self.snapshot_pins, captured)?;
        Ok(EntityDatabase {
            persona: EntityPersona::Replica(ReplicaCore {
                blocks: ReplicaBlockIo {
                    blocks: engine.block_store_shared(),
                    restart_required: engine.restart_flag(),
                },
                captured,
                authority_uuid: engine.state().authority_uuid,
            }),
            root: self.root,
            database_instance_id: self.database_instance_id,
            snapshot_pins: Arc::clone(&self.snapshot_pins),
            compaction_hints: Arc::clone(&self.compaction_hints),
            tombstone_reclaim_override: self.tombstone_reclaim_override,
            page_cache: Arc::clone(&self.page_cache),
            replica_pin: Some(Arc::new(pin)),
            query_publisher: None,
            overlay: Arc::new(std::sync::RwLock::new(self.overlay.read()
                .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?.clone())),
            overlay_trigger_bytes: std::sync::atomic::AtomicUsize::new(
                self.overlay_trigger_bytes
                    .load(std::sync::atomic::Ordering::Acquire),
            ),
            overlay_trigger_entries: std::sync::atomic::AtomicUsize::new(
                self.overlay_trigger_entries
                    .load(std::sync::atomic::Ordering::Acquire),
            ),
            overlay_enabled: self.overlay_enabled,
            overlay_budget_bytes: self.overlay_budget_bytes,
            overlay_wal_watermark_bytes: self.overlay_wal_watermark_bytes,
            merge_chunk_entries_adaptive: std::sync::atomic::AtomicUsize::new(
                self.merge_chunk_entries_adaptive
                    .load(std::sync::atomic::Ordering::Acquire),
            ),
        })
    }

    pub fn begin(&self, tenant_id: u64, owner_user_id: u64) -> io::Result<EntityTransaction> {
        self.begin_with_additional_scope_prefixes(tenant_id, owner_user_id, Vec::new())
    }

    pub(crate) fn preparation_memory_lease(&self) -> io::Result<SnapshotPin> {
        self.replica_pin.as_ref().map(SnapshotPin::lease)
            .ok_or_else(|| invalid_input("preparation requires a pinned snapshot"))
    }

    pub(crate) fn begin_with_additional_scope_prefixes(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        additional_scope_prefixes: Vec<Vec<u8>>,
    ) -> io::Result<EntityTransaction> {
        self.require_usable_authority()?;
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input(
                "tenant and owner identities must be positive",
            ));
        }
        let snapshot = self.current_snapshot();
        self.begin_at_snapshot(
            tenant_id,
            owner_user_id,
            additional_scope_prefixes,
            snapshot,
            true,
        )
    }

    fn begin_at_snapshot(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        additional_scope_prefixes: Vec<Vec<u8>>,
        snapshot: EntitySnapshot,
        writable: bool,
    ) -> io::Result<EntityTransaction> {
        Ok(EntityTransaction {
            database_instance_id: self.database_instance_id,
            tenant_id,
            owner_user_id,
            additional_scope_prefixes,
            snapshot,
            overlay_view: if writable {
                self.overlay.read().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                    .view(snapshot.sequence)
            } else {
                EntityOverlayView::default()
            },
            writable,
            point_witnesses: BTreeMap::new(),
            range_witnesses: Vec::new(),
            retired_ranges: Vec::new(),
            replacement_ranges: Vec::new(),
            mounted_ranges: Vec::new(),
            staged_reference_blocks: Vec::new(),
            writes: BTreeMap::new(),
            // Replica transactions lease the one root pin registered at
            // build; writer transactions register their own handle.
            snapshot_pin: match self.replica_pin.as_ref() {
                Some(pin) if pin.snapshot == snapshot => pin.lease(),
                _ => pin_snapshot(
                    &self.snapshot_pins,
                    snapshot,
                )?,
            },
        })
    }

    pub(crate) fn stage_persistent_snapshot_pin(
        &mut self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
    ) -> io::Result<EntitySnapshot> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        // A pin must capture every committed mutation, not only the merged
        // tree prefix: flush the overlay first (no-op when the snapshot is
        // stale or the overlay is empty).
        self.flush_overlay_for_materialization(transaction)?;
        let snapshot = transaction.snapshot;
        let encoded_snapshot = encode_persistent_root_pin(snapshot)?;
        let pin_state = self.persistent_pin_state(transaction, pin_id)?;
        if let Some(value) = pin_state.encoded_snapshot {
            if value == encoded_snapshot {
                self.put(
                    transaction,
                    pin_state.count_key,
                    encode_persistent_root_pin_count(pin_state.count)?,
                )?;
                return Ok(snapshot);
            }
            return Err(conflict(
                "persistent entity root pin identity already exists",
            ));
        }
        self.ensure_persistent_pin_capacity(pin_state.count)?;
        self.put(transaction, pin_state.pin_key, encoded_snapshot)?;
        self.put(
            transaction,
            pin_state.count_key,
            encode_persistent_root_pin_count(pin_state.count + 1)?,
        )?;
        Ok(snapshot)
    }

    pub(crate) fn stage_persistent_range_snapshot_pin(
        &mut self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        self.flush_overlay_for_materialization(transaction)?;
        if ranges.is_empty() || ranges.len() > MAX_PERSISTENT_ENTITY_CAPSULE_RANGES {
            return Err(invalid_input("invalid persistent entity snapshot ranges"));
        }
        let mut encoded_ranges = Vec::with_capacity(ranges.len());
        for (start, end) in ranges {
            ensure_scope(transaction, start)?;
            ensure_scope(transaction, end)?;
            if start >= end {
                return Err(invalid_input("invalid persistent entity snapshot range"));
            }
            encoded_ranges.push((start.0.clone(), end.0.clone()));
        }
        encoded_ranges.sort_by(|left, right| left.0.cmp(&right.0));
        if encoded_ranges.windows(2).any(|pair| pair[0].1 > pair[1].0) {
            return Err(invalid_input("persistent entity snapshot ranges overlap"));
        }
        let pin_state = self.persistent_pin_state(transaction, pin_id)?;
        if pin_state.encoded_snapshot.is_some() {
            return Err(conflict(
                "persistent entity root pin identity already exists",
            ));
        }
        self.ensure_persistent_pin_capacity(pin_state.count)?;
        let source_root = transaction
            .snapshot
            .root
            .ok_or_else(|| invalid_input("cannot snapshot ranges from an empty entity tree"))?;
        let (capsule_root, mut staged_blocks) = (Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        })
        .extract_ranges(source_root, &encoded_ranges)?;
        if !staged_blocks.contains(&capsule_root) {
            staged_blocks.push(capsule_root);
        }
        let additional_blocks = staged_blocks
            .iter()
            .filter(|block_id| !transaction.staged_reference_blocks.contains(block_id))
            .count();
        if transaction.staged_reference_blocks.len().checked_add(additional_blocks)
            .and_then(|count| u32::try_from(count).ok()).is_none()
        {
            return Err(invalid_input(
                "persistent snapshot block count exceeds its u32 representation",
            ));
        }
        for block_id in staged_blocks {
            if !transaction.staged_reference_blocks.contains(&block_id) {
                transaction.staged_reference_blocks.push(block_id);
            }
        }
        let snapshot = EntitySnapshot {
            sequence: transaction.snapshot.sequence,
            root: Some(capsule_root),
        };
        self.put(
            transaction,
            pin_state.pin_key,
            encode_persistent_root_pin(snapshot)?,
        )?;
        self.put(
            transaction,
            pin_state.count_key,
            encode_persistent_root_pin_count(pin_state.count + 1)?,
        )?;
        Ok(snapshot)
    }

    pub(crate) fn stage_persistent_range_snapshot_restore(
        &mut self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        self.flush_overlay_for_materialization(transaction)?;
        if ranges.is_empty() || ranges.len() > MAX_PERSISTENT_ENTITY_CAPSULE_RANGES {
            return Err(invalid_input("invalid persistent entity restore ranges"));
        }
        let pin_state = self.persistent_pin_state(transaction, pin_id)?;
        let encoded_snapshot = pin_state
            .encoded_snapshot
            .as_deref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "snapshot pin not found"))?;
        let snapshot = decode_persistent_root_pin(encoded_snapshot)?;
        let pin_record = (Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        })
        .get(transaction.snapshot.root, pin_state.pin_key.encoded())?
        .ok_or_else(|| invalid_data("persistent entity snapshot pin disappeared"))?;
        if snapshot.sequence >= pin_record.version
            || snapshot.sequence > self.writer_engine()?.state().durable_sequence
        {
            return Err(invalid_data("persistent entity root pin is not monotonic"));
        }
        let capsule_root = snapshot.root;
        let mut mounts = Vec::with_capacity(ranges.len());
        for (start, end) in ranges {
            ensure_scope(transaction, start)?;
            ensure_scope(transaction, end)?;
            if start >= end {
                return Err(invalid_input("invalid persistent entity restore range"));
            }
            mounts.push(RangeMount {
                start: start.0.clone(),
                end: end.0.clone(),
                root: capsule_root,
            });
        }
        mounts.sort_by(|left, right| left.start.cmp(&right.start));
        if mounts.windows(2).any(|pair| pair[0].end > pair[1].start)
            || transaction.mounted_ranges.len() + mounts.len() > MAX_ENTITY_ROOT_RANGE_MOUNTS
            || mounts.iter().any(|mount| {
                transaction
                    .mounted_ranges
                    .iter()
                    .any(|existing| existing.start < mount.end && mount.start < existing.end)
                    || transaction.retired_ranges.iter().any(|(start, end)| {
                        start.as_slice() < mount.end.as_slice()
                            && mount.start.as_slice() < end.as_slice()
                    })
            })
        {
            return Err(invalid_input(
                "persistent entity restore ranges overlap or exceed capacity",
            ));
        }
        transaction.mounted_ranges.extend(mounts);
        transaction
            .mounted_ranges
            .sort_by(|left, right| left.start.cmp(&right.start));
        if !transaction.staged_reference_blocks.contains(&capsule_root) {
            if transaction.staged_reference_blocks.len().checked_add(1)
                .and_then(|count| u32::try_from(count).ok()).is_none()
            {
                return Err(invalid_input(
                    "persistent restore block count exceeds its u32 representation",
                ));
            }
            transaction.staged_reference_blocks.push(capsule_root);
        }
        Ok(EntitySnapshot {
            sequence: snapshot.sequence,
            root: Some(snapshot.root),
        })
    }

    fn persistent_pin_state(
        &self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
    ) -> io::Result<PersistentPinState> {
        let pin_key =
            persistent_root_pin_key(transaction.tenant_id, transaction.owner_user_id, pin_id)?;
        let count_key = persistent_root_pin_count_key()?;
        for scope in [pin_key.encoded(), count_key.encoded()] {
            if !transaction
                .additional_scope_prefixes
                .iter()
                .any(|existing| existing == scope)
            {
                transaction.additional_scope_prefixes.push(scope.to_vec());
            }
        }
        let encoded_snapshot = self.get(transaction, &pin_key)?;
        if let Some(encoded) = encoded_snapshot.as_deref() {
            decode_persistent_root_pin(encoded)?;
        }
        let count = if let Some(encoded_count) = self.get(transaction, &count_key)? {
            decode_persistent_root_pin_count(&encoded_count)?
        } else {
            let (range_start, range_end) = persistent_root_pin_range()?;
            for scope in [range_start.encoded(), range_end.encoded()] {
                if !transaction
                    .additional_scope_prefixes
                    .iter()
                    .any(|existing| existing == scope)
                {
                    transaction.additional_scope_prefixes.push(scope.to_vec());
                }
            }
            let pins = self.scan(
                transaction,
                &range_start,
                &range_end,
                MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS + 1,
            )?;
            for (key, value) in &pins {
                if !is_persistent_root_pin_key(key.encoded())? {
                    return Err(invalid_data(
                        "persistent root pin range contains another namespace",
                    ));
                }
                decode_persistent_root_pin(value)?;
            }
            pins.len()
        };
        Ok(PersistentPinState {
            pin_key,
            encoded_snapshot,
            count_key,
            count,
        })
    }

    fn ensure_persistent_pin_capacity(&self, pin_count: usize) -> io::Result<()> {
        if pin_count >= MAX_PERSISTENT_ENTITY_ROOT_PINS {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "persistent entity root pin capacity is exhausted",
            ));
        }
        Ok(())
    }

    pub(crate) fn remove_persistent_snapshot_pin(
        &self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
    ) -> io::Result<bool> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        let pin_state = self.persistent_pin_state(transaction, pin_id)?;
        let existed = pin_state.encoded_snapshot.is_some();
        if existed {
            let next_count = pin_state
                .count
                .checked_sub(1)
                .ok_or_else(|| invalid_data("persistent entity root pin count underflow"))?;
            self.delete(transaction, pin_state.pin_key)?;
            self.put(
                transaction,
                pin_state.count_key,
                encode_persistent_root_pin_count(next_count)?,
            )?;
        }
        Ok(existed)
    }

    pub(crate) fn begin_persistent_snapshot(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        pin_id: &[u8],
    ) -> io::Result<Option<EntityTransaction>> {
        self.require_usable_authority()?;
        let pin_key = persistent_root_pin_key(tenant_id, owner_user_id, pin_id)?;
        // Read the pin through the overlay-merged view at the current durable
        // sequence: a pin write/removal sitting in the deferred-application
        // overlay must be visible to snapshot acquisition (the
        // conversation.purge regression — a raw tree read kept serving the
        // pre-removal pin).
        let record = {
            let resolved = if self.overlay_has_entries()? {
                let overlay = self
                    .overlay
                    .read()
                    .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
                overlay.view(self.current_snapshot().sequence).resolve_versioned(
                    pin_key.encoded(),
                    self.writer_engine()?.state().durable_sequence,
                )
            } else {
                None
            };
            match resolved {
                Some((version, value)) => Some(EntityValue { version, value }),
                None => (Tree {
                    backend: self.backend(),
                    page_cache: Some(self.page_cache.as_ref()),
                })
                .get(self.root, pin_key.encoded())?,
            }
        };
        let Some(record) = record else {
            return Ok(None);
        };
        let Some(encoded_snapshot) = record.value else {
            return Ok(None);
        };
        let snapshot = decode_persistent_root_pin(&encoded_snapshot)?;
        if snapshot.sequence >= record.version
            || snapshot.sequence > self.writer_engine()?.state().durable_sequence
        {
            return Err(invalid_data("persistent entity root pin is not monotonic"));
        }
        Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        }
        .load_root(snapshot.root)?;
        self.begin_at_snapshot(
            tenant_id,
            owner_user_id,
            Vec::new(),
            EntitySnapshot {
                sequence: snapshot.sequence,
                root: Some(snapshot.root),
            },
            false,
        )
        .map(Some)
    }

    fn ensure_transaction_instance(&self, transaction: &EntityTransaction) -> io::Result<()> {
        if transaction.database_instance_id != self.database_instance_id {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "entity transaction belongs to a different database instance",
            ));
        }
        Ok(())
    }

    pub fn get(
        &self,
        transaction: &mut EntityTransaction,
        key: &EntityKey,
    ) -> io::Result<Option<Vec<u8>>> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        ensure_scope(transaction, key)?;
        crate::request_profile::work(crate::request_profile::Work::EntityGet, 1);
        let witness = transaction.point_witnesses.get(key.encoded());
        let needs_witness = witness.is_none();
        if let Some(witness) = witness {
            if let Some(staged) = transaction.writes.get(key.encoded()) {
                return Ok(staged.clone());
            }
            if !matches!(witness.read, RetainedPointRead::NotRetained) {
                crate::request_profile::work(crate::request_profile::Work::EntityReadReuse, 1);
            }
            match &witness.read {
                RetainedPointRead::Missing => return Ok(None),
                RetainedPointRead::Value(bytes) => return Ok(Some(bytes.to_vec())),
                RetainedPointRead::NotRetained => {}
            }
        }
        // Reuse returns above; partition actual point preparation into source
        // resolution and witness/value retention without changing their authority.
        let _prepare = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_ENTITY_POINT_PREPARE);
        let reservation_bytes = point_witness_reservation_bytes(key.encoded());
        if needs_witness {
            transaction.reserve_transaction_bytes(reservation_bytes)?;
        }
        let staged = transaction.writes.get(key.encoded()).cloned();
        // A first read after put/delete still witnesses the original version,
        // including a pending WAL delta. The staged value wins only afterward.
        let resolving = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_ENTITY_POINT_RESOLVE);
        let resolved = transaction.overlay_view.resolve_versioned(
            key.encoded(), transaction.snapshot.sequence);
        crate::request_profile::work(if resolved.is_some() {
            crate::request_profile::Work::EntityOverlayRead
        } else { crate::request_profile::Work::EntityTreeRead }, 1);
        let entity = match resolved {
            Some((version, value)) => Some(EntityValue { version, value }),
            None => match (Tree {
                backend: self.backend(),
                page_cache: Some(self.page_cache.as_ref()),
            }).get(transaction.snapshot.root, key.encoded()) {
                Ok(entity) => entity,
                Err(error) => {
                    if needs_witness {
                        transaction.snapshot_pin.release_transaction_bytes(reservation_bytes);
                    }
                    return Err(error);
                }
            },
        };
        drop(resolving);
        let _retaining = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_ENTITY_POINT_RETAIN);
        let witness = transaction.point_witnesses.entry(key.0.clone())
            .or_insert_with(|| PointWitness::version_only(entity.as_ref().map(|value| value.version)));
        if let Some(staged) = staged { return Ok(staged); }
        let value = entity.and_then(|value| value.value);
        witness.read = match &value {
            None => RetainedPointRead::Missing,
            Some(bytes) => match transaction.snapshot_pin.reserve_transaction_bytes(bytes.len()) {
                Ok(()) => {
                    let retained = bytes.clone().into_boxed_slice();
                    crate::request_profile::work(
                        crate::request_profile::Work::EntityPointRetainedBytes, bytes.len() as u64);
                    RetainedPointRead::Value(retained)
                },
                Err(error) => return Err(error),
            },
        };
        Ok(value)
    }

    pub fn scan(&self, transaction: &mut EntityTransaction, start: &EntityKey,
        end: &EntityKey, limit: usize) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.scan_visible(transaction, start, end, limit, false)
    }

    pub fn scan_reverse(&self, transaction: &mut EntityTransaction, start: &EntityKey,
        end: &EntityKey, limit: usize) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.scan_visible(transaction, start, end, limit, true)
    }

    /// Merge ordered cursors without copying a matching overlay range. The
    /// tree window grows only when masks consume it; the final exact tree
    /// window remains the OCC witness, including range exhaustion.
    fn scan_visible(&self, transaction: &mut EntityTransaction, start: &EntityKey,
        end: &EntityKey, limit: usize, reverse: bool) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        ensure_scan_scope(transaction, start, end)?;
        if limit == 0 || limit > MAX_ENTITY_RANGE_ROWS {
            return Err(invalid_input("invalid or unbounded entity range"));
        }
        let previous_witness_count = transaction.range_witnesses.len();
        let mut reserved_bytes = 0_usize;
        let result = (|| {
            let tree = Tree { backend: self.backend(), page_cache: Some(self.page_cache.as_ref()) };
            let mut cursor_start = start.0.clone();
            let mut cursor_end = end.0.clone();
            let mut window_limit = limit;
            let mut visible = Vec::with_capacity(limit);
            let mut total_dead_leaves = 0_u32;
            loop {
                let mut dead_leaves = 0;
                let (rows, leaf_ids) = if reverse {
                    tree.scan_reverse(transaction.snapshot.root, &cursor_start, &cursor_end, window_limit, &mut dead_leaves)?
                } else {
                    tree.scan(transaction.snapshot.root, &cursor_start, &cursor_end, window_limit, &mut dead_leaves)?
                };
                total_dead_leaves = total_dead_leaves.saturating_add(dead_leaves);
                let exhausted = rows.len() < window_limit;
                let mut window_start = cursor_start.clone();
                let mut window_end = cursor_end.clone();
                if !exhausted {
                    let cut = &rows.last().unwrap().0;
                    if reverse { window_start = cut.clone(); }
                    else { window_end = cut.clone(); window_end.push(0); }
                }
                {
                    let overlay = transaction.overlay_view.range(&window_start, &window_end, transaction.snapshot.sequence, reverse);
                    let staged = transaction.writes.range(window_start.clone()..window_end.clone());
                    let staged: Box<dyn Iterator<Item = (&Vec<u8>, &Option<Vec<u8>>)>> =
                        if reverse { Box::new(staged.rev()) } else { Box::new(staged) };
                    let mut sources: Vec<std::iter::Peekable<Box<dyn Iterator<Item = (&Vec<u8>, &Option<Vec<u8>>)>>>> = vec![
                        Box::new(rows.iter().map(|(key, value)| (key, &value.value))) as Box<dyn Iterator<Item = _>>,
                        Box::new(overlay), staged,
                    ].into_iter().map(Iterator::peekable).collect();
                    while visible.len() < limit {
                        let next_key = sources.iter_mut().filter_map(|source| source.peek().map(|(key, _)| *key))
                            .min_by(|left, right| if reverse { right.cmp(left) } else { left.cmp(right) });
                        let Some(key) = next_key else { break; };
                        let mut value = None;
                        // Later sources override earlier sources at an equal key.
                        for source in &mut sources {
                            if source.peek().is_some_and(|(candidate, _)| *candidate == key) {
                                value = source.next().unwrap().1.as_ref();
                            }
                        }
                        if let Some(value) = value { visible.push((EntityKey(key.clone()), value.clone())); }
                    }
                }
                // Validation repeats this raw snapshot window before masking.
                let witness = RangeWitness {
                    start: cursor_start.clone(), end: cursor_end.clone(), leaf_ids,
                    reverse, scan_limit: window_limit,
                };
                let bytes = range_witness_reservation_bytes(&witness.start, &witness.end, witness.leaf_ids.capacity())?;
                let next_reserved = reserved_bytes.checked_add(bytes)
                    .ok_or_else(|| invalid_data("range scan byte accounting overflow"))?;
                transaction.reserve_transaction_bytes(bytes)?;
                reserved_bytes = next_reserved;
                transaction.range_witnesses.try_reserve(1).map_err(|error|
                    io::Error::new(io::ErrorKind::OutOfMemory, error))?;
                transaction.range_witnesses.push(witness);
                let interval_consumed = if reverse { window_start <= cursor_start }
                    else { window_end >= cursor_end };
                if visible.len() == limit || exhausted || interval_consumed {
                    if total_dead_leaves >= COMPACTION_HINT_THRESHOLD_LEAVES {
                        self.record_compaction_hint(start.encoded(), end.encoded(), total_dead_leaves);
                    }
                    return Ok(visible);
                }
                // Move past consumed keys; do not re-read an expanding prefix.
                if reverse { cursor_end = window_start; }
                else { cursor_start = window_end; }
                window_limit = MAX_ENTITY_RANGE_ROWS;
            }
        })();
        if result.is_err() {
            transaction.range_witnesses.truncate(previous_witness_count);
            if previous_witness_count == 0 { transaction.range_witnesses = Vec::new(); }
            transaction.snapshot_pin.release_transaction_bytes(reserved_bytes);
        }
        result
    }

    pub fn consolidate_one_range_mount(
        &self,
        transaction: &mut EntityTransaction,
    ) -> io::Result<Option<EntityMountConsolidationProgress>> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        if !transaction.point_witnesses.is_empty()
            || !transaction.range_witnesses.is_empty()
            || !transaction.retired_ranges.is_empty()
            || !transaction.replacement_ranges.is_empty()
            || !transaction.mounted_ranges.is_empty()
            || !transaction.writes.is_empty()
        {
            return Err(invalid_input(
                "range mount consolidation requires a fresh transaction",
            ));
        }
        let tree = Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        };
        let directory = tree.split_root(transaction.snapshot.root)?;
        let owner_prefix = [
            transaction.tenant_id.to_be_bytes(),
            transaction.owner_user_id.to_be_bytes(),
        ]
        .concat();
        let Some(mount) = directory
            .mounts
            .into_iter()
            .find(|mount| mount.start.starts_with(&owner_prefix))
        else {
            return Ok(None);
        };
        let scan_limit = MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS
            .checked_add(1)
            .filter(|limit| *limit <= MAX_ENTITY_RANGE_ROWS)
            .ok_or_else(|| invalid_data("invalid range mount consolidation row bound"))?;
        let (candidates, _) = tree.scan(
            transaction.snapshot.root,
            &mount.start,
            &mount.end,
            scan_limit,
            &mut 0,
        )?;
        let mut selected = Vec::new();
        let mut materialized_bytes = 0_usize;
        for (key, value) in candidates.iter().take(MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS) {
            let next_bytes = materialized_bytes
                .checked_add(key.len())
                .and_then(|bytes| bytes.checked_add(value.value.as_ref().map_or(0, Vec::len)))
                .ok_or_else(|| invalid_data("range mount consolidation byte overflow"))?;
            if next_bytes > MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES {
                break;
            }
            materialized_bytes = next_bytes;
            selected.push((key.clone(), value.value.clone().unwrap()));
        }
        if !candidates.is_empty() && selected.is_empty() {
            return Err(invalid_data(
                "one entity row exceeds the mount consolidation byte budget",
            ));
        }
        let selected_count = selected.len();
        let mount_completed = candidates.len() < scan_limit && selected_count == candidates.len();
        let replacement_end = if mount_completed {
            mount.end.clone()
        } else {
            let mut exclusive = selected.last().unwrap().0.clone();
            exclusive.push(0);
            exclusive
        };
        let start = EntityKey(mount.start);
        let end = EntityKey(replacement_end);
        self.retire_range(transaction, &start, &end)?;
        transaction
            .replacement_ranges
            .push((start.0.clone(), end.0.clone()));
        for (key, value) in selected {
            self.put(transaction, EntityKey(key), value)?;
        }
        Ok(Some(EntityMountConsolidationProgress {
            rows_materialized: u32::try_from(selected_count)
                .map_err(|_| invalid_data("range mount consolidation row count overflow"))?,
            materialized_bytes: u64::try_from(materialized_bytes)
                .map_err(|_| invalid_data("range mount consolidation byte count overflow"))?,
            mount_completed,
        }))
    }

    pub fn put(
        &self,
        transaction: &mut EntityTransaction,
        key: EntityKey,
        value: Vec<u8>,
    ) -> io::Result<()> {
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        ensure_scope(transaction, &key)?;
        if transaction
            .retired_ranges
            .iter()
            .any(|(start, end)| key.encoded() >= start.as_slice() && key.encoded() < end.as_slice())
            && !transaction.replacement_ranges.iter().any(|(start, end)| {
                key.encoded() >= start.as_slice() && key.encoded() < end.as_slice()
            })
        {
            return Err(invalid_input("entity write overlaps a retired range"));
        }
        if value.len() > MAX_ENTITY_INLINE_VALUE_BYTES {
            return Err(invalid_input(
                "entity value exceeds 8 KiB; use a blob reference",
            ));
        }
        if !transaction.writes.contains_key(&key.0)
            && u32::try_from(transaction.writes.len()).is_ok_and(|count| count == u32::MAX)
        {
            return Err(invalid_input(
                "entity overlay write count exceeds its u32 representation",
            ));
        }
        transaction.discard_point_read(key.encoded());
        let previous_bytes = transaction.writes.get(&key.0).map_or(0, |previous| {
            write_reservation_bytes(&key.0, previous.as_deref())
        });
        let next_bytes = write_reservation_bytes(&key.0, Some(&value));
        if next_bytes > previous_bytes {
            transaction.reserve_transaction_bytes(next_bytes - previous_bytes)?;
        }
        transaction.writes.insert(key.0, Some(value));
        if previous_bytes > next_bytes {
            transaction
                .snapshot_pin
                .release_transaction_bytes(previous_bytes - next_bytes);
        }
        Ok(())
    }

    pub fn delete(&self, transaction: &mut EntityTransaction, key: EntityKey) -> io::Result<()> {
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        ensure_scope(transaction, &key)?;
        if transaction
            .retired_ranges
            .iter()
            .any(|(start, end)| key.encoded() >= start.as_slice() && key.encoded() < end.as_slice())
            && !transaction.replacement_ranges.iter().any(|(start, end)| {
                key.encoded() >= start.as_slice() && key.encoded() < end.as_slice()
            })
        {
            return Err(invalid_input("entity write overlaps a retired range"));
        }
        if !transaction.writes.contains_key(&key.0)
            && u32::try_from(transaction.writes.len()).is_ok_and(|count| count == u32::MAX)
        {
            return Err(invalid_input(
                "entity overlay write count exceeds its u32 representation",
            ));
        }
        transaction.discard_point_read(key.encoded());
        let previous_bytes = transaction.writes.get(&key.0).map_or(0, |previous| {
            write_reservation_bytes(&key.0, previous.as_deref())
        });
        let next_bytes = write_reservation_bytes(&key.0, None);
        if next_bytes > previous_bytes {
            transaction.reserve_transaction_bytes(next_bytes - previous_bytes)?;
        }
        transaction.writes.insert(key.0, None);
        if previous_bytes > next_bytes {
            transaction
                .snapshot_pin
                .release_transaction_bytes(previous_bytes - next_bytes);
        }
        Ok(())
    }

    pub fn retire_range(
        &self,
        transaction: &mut EntityTransaction,
        start: &EntityKey,
        end: &EntityKey,
    ) -> io::Result<()> {
        self.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        ensure_scope(transaction, start)?;
        ensure_scope(transaction, end)?;
        if start >= end {
            return Err(invalid_input("invalid entity retirement range"));
        }
        if transaction.retired_ranges.len() == MAX_ENTITY_RETIRED_RANGES {
            return Err(invalid_input(
                "entity retirement range capacity is exhausted",
            ));
        }
        if transaction
            .retired_ranges
            .iter()
            .any(|(existing_start, existing_end)| {
                existing_start.as_slice() == start.encoded()
                    && existing_end.as_slice() == end.encoded()
            })
        {
            return Ok(());
        }
        if transaction
            .retired_ranges
            .iter()
            .any(|(existing_start, existing_end)| {
                existing_start.as_slice() < end.encoded()
                    && start.encoded() < existing_end.as_slice()
            })
        {
            return Err(invalid_input("entity retirement ranges overlap"));
        }
        if transaction
            .writes
            .keys()
            .any(|key| key.as_slice() >= start.encoded() && key.as_slice() < end.encoded())
            || transaction.mounted_ranges.iter().any(|mount| {
                start.encoded() < mount.end.as_slice() && mount.start.as_slice() < end.encoded()
            })
        {
            return Err(invalid_input(
                "entity write or mount overlaps a retired range",
            ));
        }
        transaction.reserve_transaction_bytes(retired_range_reservation_bytes(
                start.encoded(),
                end.encoded(),
            ))?;
        let insertion = transaction
            .retired_ranges
            .partition_point(|(existing_start, _)| existing_start.as_slice() < start.encoded());
        transaction
            .retired_ranges
            .insert(insertion, (start.0.clone(), end.0.clone()));
        Ok(())
    }

    pub fn commit(&mut self, transaction: EntityTransaction) -> io::Result<u64> {
        let prepared = self.prepare_commit(transaction)?;
        if let Some(overlay_record) = prepared.overlay_record() {
            // Deferred-application path: the commit's durable record is the
            // EntityOverlay family envelope, exactly like
            // `AuthorityDatabase::commit`. An early return here used to drop
            // plain-writes commits from direct `EntityDatabase::commit`
            // callers entirely — the delta never reached the WAL or the
            // in-memory overlay (the 2026-09-20 stream-cursor investigation:
            // corruption-injection reads kept serving the pre-corruption
            // value because the corrupting commit had vanished).
            let mut builder = crate::transaction::FamilyTransactionBuilder::default();
            builder.add_record(
                crate::transaction::FamilyRecordKind::EntityOverlay,
                overlay_record,
                std::iter::empty(),
            )?;
            builder.add_block_references(prepared.block_ids().iter().copied())?;
            let envelope = builder.prepare()?;
            let committed = self
                .writer_engine_mut()?
                .commit_references_with_authority_state(
                    &envelope.inline_payload,
                    &envelope.block_ids,
                    prepared.authority_state_root(),
                )?;
            self.apply_prepared_commit(prepared, committed.sequence)?;
            return Ok(committed.sequence);
        }
        let Some(root_record) = prepared.root_record() else {
            return Ok(self.writer_engine()?.state().durable_sequence);
        };
        let result = self.writer_engine_mut()?.commit_references_with_authority_state(
            root_record,
            prepared.block_ids(),
            prepared.authority_state_root(),
        )?;
        self.apply_prepared_commit(prepared, result.sequence)?;
        Ok(result.sequence)
    }

    pub(crate) fn prepare_commit(
        &mut self,
        transaction: EntityTransaction,
    ) -> io::Result<PreparedEntityCommit> {
        self.ensure_transaction_instance(&transaction)?;
        if transaction.snapshot.sequence > self.writer_engine()?.state().durable_sequence {
            return Err(invalid_data("entity snapshot is from the future"));
        }
        let plain_writes = !transaction.writes.is_empty()
            && transaction.retired_ranges.is_empty()
            && transaction.mounted_ranges.is_empty();
        if self.overlay_enabled
            && plain_writes
        {
            // The EntityOverlay delta must fit the 256 KiB family inline
            // bound alongside the envelope's other records; an oversized
            // mutation set takes the exact legacy tree path (whose
            // spill-to-reference mechanism covers it) instead of failing
            // closed at envelope build time. The estimate matches
            // encode_overlay_delta byte for byte; the reserve leaves 64 KiB
            // for receipt (≤7 KiB inline), outbox and stream records.
            const OVERLAY_ENVELOPE_RESERVE_BYTES: usize = 64 * 1024;
            let delta_bytes: usize = 4
                + transaction
                    .writes
                    .iter()
                    .map(|(key, value)| {
                        8 + key.len() + value.as_ref().map_or(4, |value| value.len())
                    })
                    .sum::<usize>();
            if delta_bytes + OVERLAY_ENVELOPE_RESERVE_BYTES
                <= crate::transaction::MAX_INLINE_BYTES
            {
                // Validate stale snapshots against the logical tree + overlay
                // head. Independent prepared transactions must not force a full
                // physical drain merely because another transaction committed.
                self.validate_overlay_transaction(&transaction)?;
                {
                    let _ta_prepare_merge = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_PREPARE_MERGE);
                    self.merge_overlay_if_needed()?;
                }
                let _ta_prepare_fast = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_PREPARE_FAST);
                let sequence = self.writer_engine()?.next_sequence()?;
                let mut written_block_ids = transaction.staged_reference_blocks.clone();
                let mut next_root = self.root;
                if !transaction.writes.is_empty() {
                    let tree = Tree { backend: self.backend(), page_cache: Some(self.page_cache.as_ref()) };
                    let mut directory = tree.split_root(self.root)?;
                    if directory.materialized.is_none() {
                        // Persist coverage before the first deferred mutation.
                        // WAL retirement must never make this root appear complete.
                        let overlay = self.overlay.read()
                            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
                        directory.materialized = Some(if overlay.is_empty() {
                            OverlayPosition { sequence: sequence - 1, key: None }
                        } else { overlay.materialized_position() });
                        next_root = Some(tree.store_directory(&directory, &mut written_block_ids)?);
                    }
                }
                written_block_ids.sort_unstable();
                written_block_ids.dedup();
                return Ok(PreparedEntityCommit {
                    expected_sequence: sequence,
                    next_root,
                    written_block_ids,
                    root_record: None,
                    overlay_delta: Some(transaction.writes.into_iter().collect()),
                });
            }
        }
        // Every other path is tree-synchronous (legacy writes, structural
        // transactions, oversized writes, or writes when the overlay flag is
        // off). A non-empty overlay must merge first: the tree root may only
        // advance once it contains every committed mutation, and the legacy
        // witness verification below then runs against the fully merged
        // tree, exactly like a pre-overlay commit.
        let was_current = transaction.snapshot.root == self.root
            && transaction.snapshot.sequence == self.writer_engine()?.state().durable_sequence;
        let overlay_nonempty = !self
            .overlay
            .read()
            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
            .is_empty();
        let mut transaction = transaction;
        if overlay_nonempty {
            self.merge_overlay_fully()?;
            if was_current {
                // The merge changed only the tree's physical layout, never
                // its logical content (per-key versions are preserved), so
                // the witnesses this transaction recorded at its current
                // snapshot stay valid by construction — the same skip the
                // legacy path applies when the snapshot root still is the
                // current root.
                transaction.snapshot = self.current_snapshot();
            }
        }
        self.prepare_commit_inner(transaction)
    }

    /// Called only by the exclusive authority sequencer, immediately before
    /// staging commit-owned metadata. Validate the complete business read/write
    /// set before adopting the head; structural mutations cannot be rebased.
    pub(crate) fn rebase_validated_for_commit(
        &self,
        transaction: &mut EntityTransaction,
    ) -> io::Result<()> {
        self.ensure_transaction_instance(transaction)?;
        if !transaction.retired_ranges.is_empty() || !transaction.mounted_ranges.is_empty() {
            return Err(conflict(
                "structural transaction requires exclusive preparation",
            ));
        }
        if transaction.snapshot.sequence > self.writer_engine()?.state().durable_sequence {
            return Err(invalid_data("entity snapshot is from the future"));
        }
        self.validate_overlay_transaction(transaction)?;
        let head = self.current_snapshot();
        transaction.overlay_view = self
            .overlay
            .read()
            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
            .view(head.sequence);
        transaction.snapshot = head;
        // The registered old snapshot remains pinned through commit. The new
        // head is protected by the exclusive writer borrow and cannot escape.
        Ok(())
    }

    fn validate_overlay_transaction(&self, transaction: &EntityTransaction) -> io::Result<()> {
        let head = self.current_snapshot();
        if transaction.snapshot == head {
            return Ok(());
        }
        let overlay = self
            .overlay
            .read()
            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
            .view(head.sequence);
        let tree = Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        };
        let versions_at = |view: &EntityOverlayView, snapshot: EntitySnapshot, keys: &[&[u8]]| {
            let mut versions: Vec<_> = keys.iter().map(|key| view.resolve_version(key, snapshot.sequence)).collect();
            let missing: Vec<_> = keys.iter().enumerate().filter(|(index, _)| versions[*index].is_none())
                .map(|(index, key)| (index, *key)).collect();
            let tree_keys: Vec<_> = missing.iter().map(|(_, key)| *key).collect();
            for ((index, _), version) in missing.into_iter().zip(tree.versions(snapshot.root, &tree_keys)?) {
                versions[index] = version;
            }
            Ok::<_, io::Error>(versions)
        };
        let mut keys: Vec<_> = transaction.point_witnesses.keys().chain(transaction.writes.keys())
            .map(Vec::as_slice).collect();
        keys.sort_unstable();
        keys.dedup();
        let head_versions = {
            let _versions = crate::fsync_stats::StageTimer::diagnostic(
                crate::fsync_stats::STAGE_OVERLAY_VALIDATE_HEAD_VERSIONS);
            versions_at(&overlay, head, &keys)?
        };
        let head_version = |key: &[u8]| head_versions[keys.binary_search(&key).expect("validation key collected")];
        for (key, witnessed) in &transaction.point_witnesses {
            if head_version(key) != witnessed.version {
                return Err(conflict("entity point witness changed"));
            }
        }
        let blind_keys: Vec<_> = transaction.writes.keys()
            .filter(|key| !transaction.point_witnesses.contains_key(*key)).map(Vec::as_slice).collect();
        let snapshot_versions = {
            let _versions = crate::fsync_stats::StageTimer::diagnostic(
                crate::fsync_stats::STAGE_OVERLAY_VALIDATE_ORIGINAL_BLIND_VERSIONS);
            versions_at(&transaction.overlay_view, transaction.snapshot, &blind_keys)?
        };
        for (key, version) in blind_keys.into_iter().zip(snapshot_versions) {
            if version != head_version(key) {
                return Err(conflict("entity write witness changed"));
            }
        }
        let range_validation = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_OVERLAY_VALIDATE_RANGE_WITNESSES);
        for witness in &transaction.range_witnesses {
            if overlay.changed_in_range_since(
                &witness.start,
                &witness.end,
                transaction.snapshot.sequence,
            ) {
                return Err(conflict("entity overlay range witness changed"));
            }
            let (_, current_leaves) = if witness.reverse {
                tree.scan_reverse(
                    head.root,
                    &witness.start,
                    &witness.end,
                    witness.scan_limit,
                    &mut 0,
                )?
            } else {
                tree.scan(
                    head.root,
                    &witness.start,
                    &witness.end,
                    witness.scan_limit,
                    &mut 0,
                )?
            };
            let unchanged = if current_leaves != witness.leaf_ids {
                !self.tombstone_reclaim_override.unwrap_or_else(tombstone_reclaim_enabled)
                    && tree.range_versions_unchanged(witness, &current_leaves,
                        transaction.snapshot, head, &transaction.overlay_view, &overlay)?
            } else {
                // Physical roots can return to empty after retiring a range;
                // snapshot-overlay rows still require disappearance checks.
                tree.overlay_range_versions_unchanged(witness, transaction.snapshot, head,
                    &transaction.overlay_view, &overlay)?
            };
            if !unchanged { return Err(conflict("entity range witness changed")); }
        }
        drop(range_validation);
        Ok(())
    }

    fn prepare_commit_inner(
        &mut self,
        transaction: EntityTransaction,
    ) -> io::Result<PreparedEntityCommit> {
        let tree = Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        };
        if (!transaction.retired_ranges.is_empty() || !transaction.mounted_ranges.is_empty())
            && transaction.snapshot.root != self.root
        {
            return Err(conflict("entity root-structure witness changed"));
        }
        if transaction.writes.iter().any(|(key, _)| {
            transaction.retired_ranges.iter().any(|(start, end)| {
                key.as_slice() >= start.as_slice()
                    && key.as_slice() < end.as_slice()
                    && !transaction.replacement_ranges.iter().any(
                        |(replacement_start, replacement_end)| {
                            key.as_slice() >= replacement_start.as_slice()
                                && key.as_slice() < replacement_end.as_slice()
                        },
                    )
            })
        }) {
            return Err(invalid_input("entity write overlaps a retired range"));
        }
        if transaction.replacement_ranges.iter().any(|replacement| {
            !transaction
                .retired_ranges
                .iter()
                .any(|retired| retired == replacement)
        }) {
            return Err(invalid_data(
                "entity replacement range has no matching retirement",
            ));
        }
        // Roots are content-addressed and immutable: while the transaction's
        // snapshot root still is the current root, every comparison below
        // would re-read the exact content the witnesses were recorded from
        // and pass by construction, so the re-verification is skipped.  Any
        // intervening commit that changed observable content produced a
        // different root and takes the full verification path.
        if transaction.snapshot.root != self.root {
            for (key, witnessed) in &transaction.point_witnesses {
                let current = tree.get(self.root, key)?.map(|value| value.version);
                if current != witnessed.version {
                    return Err(conflict("entity point witness changed"));
                }
            }
            for key in transaction.writes.keys() {
                let snapshot = match transaction.overlay_view.resolve_versioned(key, transaction.snapshot.sequence) {
                    Some((version, _)) => Some(version),
                    None => tree.get(transaction.snapshot.root, key)?.map(|value| value.version),
                };
                let current = tree.get(self.root, key)?.map(|value| value.version);
                if snapshot != current {
                    return Err(conflict("entity write witness changed"));
                }
            }
            for witness in &transaction.range_witnesses {
                let (_, current_leaves) = if witness.reverse {
                    tree.scan_reverse(self.root, &witness.start, &witness.end, witness.scan_limit, &mut 0)?
                } else {
                    tree.scan(self.root, &witness.start, &witness.end, witness.scan_limit, &mut 0)?
                };
                if current_leaves != witness.leaf_ids {
                    return Err(conflict("entity range witness changed"));
                }
            }
        }
        if transaction.writes.is_empty()
            && transaction.retired_ranges.is_empty()
            && transaction.mounted_ranges.is_empty()
        {
            return Ok(PreparedEntityCommit {
                expected_sequence: self.writer_engine()?.next_sequence()?,
                next_root: self.root,
                written_block_ids: Vec::new(),
                root_record: None,
                overlay_delta: None,
            });
        }
        if u32::try_from(transaction.writes.len()).is_err() {
            return Err(invalid_input(
                "entity overlay write count exceeds its u32 representation",
            ));
        }
        let sequence = self.writer_engine()?.next_sequence()?;
        let mut written_block_ids = transaction.staged_reference_blocks.clone();
        let mut directory = tree.split_root(self.root)?;
        directory.materialized = None;
        let mut retirement_changed = false;
        if !transaction.retired_ranges.is_empty() {
            if let Some(base_root) = directory.base_root {
                let (retired_root, retired_blocks, changed) =
                    tree.retire_ranges(base_root, &transaction.retired_ranges)?;
                directory.base_root = retired_root;
                written_block_ids.extend(retired_blocks);
                retirement_changed = changed;
            }
            let (retained_mounts, mounts_changed) =
                tree.subtract_mount_ranges(directory.mounts, &transaction.retired_ranges)?;
            directory.mounts = retained_mounts;
            retirement_changed |= mounts_changed;
        }
        if !transaction.writes.is_empty() {
            let reclaim_below = self
                .effective_tombstone_reclaim_below(self.oldest_live_pin_sequence()?);
            let (written_root, write_blocks) = {
                let _timer =
                    crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_APPLY_BATCH);
                // Write-path tombstone hygiene (L1): the repack already
                // touches these runs, so reclaiming sub-watermark
                // tombstones costs no extra I/O. The watermark is the
                // oldest live pin: no open snapshot predates a reclaimed
                // entry (OCC boundary in docs/TOFUDB_TOMBSTONE_COMPACTION.md).
                tree.apply_batch(
                    directory.base_root,
                    transaction
                        .writes
                        .into_iter()
                        .map(|(key, value)| (key, value, sequence))
                        .collect(),
                    TombstoneReclaim { below: reclaim_below, mounts: &directory.mounts },
                )?
            };
            directory.base_root = written_root;
            written_block_ids.extend(write_blocks);
        } else if !retirement_changed && transaction.mounted_ranges.is_empty() {
            return Ok(PreparedEntityCommit {
                expected_sequence: sequence,
                next_root: self.root,
                written_block_ids: Vec::new(),
                root_record: None,
                overlay_delta: None,
            });
        }
        if directory.mounts.len() + transaction.mounted_ranges.len() > MAX_ENTITY_ROOT_RANGE_MOUNTS
            || transaction.mounted_ranges.iter().any(|mount| {
                directory
                    .mounts
                    .iter()
                    .any(|existing| existing.start < mount.end && mount.start < existing.end)
            })
        {
            return Err(invalid_input(
                "persistent entity restore overlaps an active mount or exceeds capacity",
            ));
        }
        directory.mounts.extend(transaction.mounted_ranges);
        directory
            .mounts
            .sort_by(|left, right| left.start.cmp(&right.start));
        let next_root = if directory.mounts.is_empty() && directory.materialized.is_none() {
            directory.base_root
        } else {
            directory.validate()?;
            Some(tree.store_directory(&directory, &mut written_block_ids)?)
        };
        if let Some(root) = next_root {
            if !written_block_ids.contains(&root) {
                written_block_ids.push(root);
            }
        }
        written_block_ids.sort_unstable();
        written_block_ids.dedup();
        if let Some(root_id) = next_root {
            if !written_block_ids.contains(&root_id) {
                return Err(invalid_data(
                    "entity root was not staged by its transaction",
                ));
            }
        }
        Ok(PreparedEntityCommit {
            expected_sequence: sequence,
            next_root,
            written_block_ids,
            root_record: Some(encode_root(sequence, next_root)),
            overlay_delta: None,
        })
    }

    pub(crate) fn apply_prepared_commit(
        &mut self,
        prepared: PreparedEntityCommit,
        committed_sequence: u64,
    ) -> io::Result<()> {
        if committed_sequence != prepared.expected_sequence {
            return Err(invalid_data("entity sequencer changed during commit"));
        }
        let publication = self.begin_query_publication();
        self.root = prepared.next_root;
        if prepared.root_record.is_some() {
            self.overlay.write().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                .note_merged(OverlayPosition { sequence: committed_sequence, key: None });
        }
        if let Some(delta) = prepared.overlay_delta {
            let (mut delta_bytes, mut delta_entries) = (0_usize, 0_usize);
            for (key, value) in &delta {
                delta_bytes += key.len() + 16 + value.as_ref().map_or(0, Vec::len);
                delta_entries += 1;
            }
            self.overlay
                .write()
                .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                .apply_delta(committed_sequence, delta);
            self.overlay_trigger_bytes
                .fetch_add(delta_bytes, std::sync::atomic::Ordering::AcqRel);
            self.overlay_trigger_entries
                .fetch_add(delta_entries, std::sync::atomic::Ordering::AcqRel);
        }
        publication.finish(self);
        Ok(())
    }

    /// Test/operator switch for the deferred-application write overlay. The
    /// daemon selects it with `TOFU_DB_WRITE_OVERLAY` at open; tests toggle
    /// it explicitly per authority.
    pub(crate) fn set_write_overlay_enabled(&mut self, enabled: bool) {
        self.overlay_enabled = enabled;
    }

    /// Probe-derived overlay merge triggers applied by the authority at open
    /// (`AuthorityDatabase::configure_write_path_budgets`). Any explicit env
    /// override was already folded into the supplied values by the launch
    /// probe's budget derivation.
    pub(crate) fn set_overlay_trigger_budgets(
        &mut self,
        budget_bytes: usize,
        wal_watermark_bytes: u64,
    ) {
        self.overlay_budget_bytes = budget_bytes;
        self.overlay_wal_watermark_bytes = wal_watermark_bytes;
    }

    /// Test-only override for the overlay merge byte budget (operator knob
    /// `TOFU_DB_OVERLAY_BUDGET_BYTES`), so trigger behavior can be exercised
    /// without process-global env state.
    #[cfg(test)]
    pub(crate) fn set_overlay_budget_for_test(&mut self, bytes: usize) {
        self.overlay_budget_bytes = bytes;
    }

    /// Whether the overlay holds any committed-but-unmerged entries (drives
    /// read merging independent of the write-mode flag).
    pub(crate) fn overlay_has_entries(&self) -> io::Result<bool> {
        Ok(!self
            .overlay
            .read()
            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
            .is_empty())
    }

    /// Smallest sequence any live snapshot pin can still observe, or
    /// `u64::MAX` when no pins exist. Overlay pruning defers to it exactly
    /// like authority GC defers to pins.
    /// Record one tombstone-dense scan window (advisory, bounded, coalesced).
    /// Called by scan/scan_reverse when a window crosses enough dead leaves;
    /// never on the error path and never blocking.
    fn record_compaction_hint(&self, start: &[u8], end: &[u8], dead_leaves: u32) {
        let Ok(mut hints) = self.compaction_hints.lock() else {
            return;
        };
        hints.record(start, end, dead_leaves);
    }

    /// Maintenance boundary: hand one window back to the advisory registry
    /// (requeueing another scope's hint or a partially swept window).
    pub(crate) fn record_entity_tombstone_compaction_hint(
        &self,
        start: &[u8],
        end: &[u8],
        dead_leaves: u32,
    ) -> io::Result<()> {
        let mut hints = self
            .compaction_hints
            .lock()
            .map_err(|_| io::Error::other("entity compaction hint registry is poisoned"))?;
        hints.record(start, end, dead_leaves);
        Ok(())
    }

    /// Maintenance boundary: pop the densest outstanding compaction hint.
    /// Hints are advisory and loss-free — the next slow scan re-issues them.
    pub(crate) fn take_tombstone_compaction_hint(
        &self,
    ) -> io::Result<Option<(Vec<u8>, Vec<u8>, u32)>> {
        let mut hints = self
            .compaction_hints
            .lock()
            .map_err(|_| io::Error::other("entity compaction hint registry is poisoned"))?;
        Ok(hints.take_densest())
    }

    #[cfg(test)]
    fn tombstone_compaction_hint_count(&self) -> usize {
        self.compaction_hints.lock().map(|hints| hints.len()).unwrap_or(0)
    }

    /// One bounded tombstone-compaction slice over `[start, end)`: repacks
    /// up to `max_leaves` pages, reclaiming every tombstone below the oldest
    /// live pin, commits the rewrite like an overlay merge (one WAL root
    /// record + staged blocks — crash-atomic), and reports where to resume.
    /// Mounted range capsules are left to the existing consolidation path.
    pub(crate) fn reclaim_tombstones_in_range(
        &mut self,
        start: &[u8],
        end: &[u8],
        max_leaves: usize,
    ) -> io::Result<EntityCompactionProgress> {
        if start >= end || max_leaves == 0 {
            return Err(invalid_input("tombstone compaction range is invalid"));
        }
        let Some(root) = self.root else {
            return Ok(EntityCompactionProgress {
                pages_rewritten: 0,
                entries_reclaimed: 0,
                resume: None,
            });
        };
        let tree = Tree {
            backend: self.backend(),
            page_cache: Some(self.page_cache.as_ref()),
        };
        let reclaim_below = self
            .effective_tombstone_reclaim_below(self.oldest_live_pin_sequence()?);
        let mut budget = ReclaimBudget {
            leaves_left: max_leaves,
            reclaimed: 0,
            resume: None,
        };
        let mut written_block_ids = Vec::new();
        let mut directory = tree.split_root(Some(root))?;
        let overlay = self.overlay.read().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
        if !overlay.is_empty() { directory.materialized = Some(overlay.materialized_position()); }
        drop(overlay);
        let Some(base_root) = directory.base_root else {
            return Ok(EntityCompactionProgress {
                pages_rewritten: 0,
                entries_reclaimed: 0,
                resume: None,
            });
        };
        let (replacements, changed) = tree.reclaim_subtree(
            base_root,
            start,
            start,
            end,
            TombstoneReclaim { below: reclaim_below, mounts: &directory.mounts },
            &mut budget,
            &mut written_block_ids,
        )?;
        if !changed {
            return Ok(EntityCompactionProgress {
                pages_rewritten: 0,
                entries_reclaimed: 0,
                resume: budget.resume.map(|key| (key, end.to_vec())),
            });
        }
        directory.base_root = match replacements.len() {
            0 => None,
            1 => Some(replacements[0].block_id),
            _ => {
                let mut level = 1_u8;
                let mut children = replacements;
                loop {
                    children = tree.pack_internal(level, children, &mut written_block_ids)?;
                    if children.len() == 1 {
                        break Some(children[0].block_id);
                    }
                    level = level
                        .checked_add(1)
                        .ok_or_else(|| invalid_data("entity tree level overflow"))?;
                    if level as usize > MAX_TREE_DEPTH {
                        return Err(invalid_data("entity tree depth limit reached"));
                    }
                }
            }
        };
        let next_root = if directory.mounts.is_empty() && directory.materialized.is_none() {
            directory.base_root
        } else { Some(tree.store_directory(&directory, &mut written_block_ids)?) };
        if let Some(root_id) = next_root {
            if !written_block_ids.contains(&root_id) {
                written_block_ids.push(root_id);
            }
        }
        written_block_ids.sort_unstable();
        written_block_ids.dedup();
        let _publish_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_PUBLISH);
        let root_record = encode_root(self.writer_engine()?.next_sequence()?, next_root);
        let committed = self
            .writer_engine_mut()?
            .commit_references_with_authority_state(
                &root_record,
                &written_block_ids,
                next_root,
            )?;
        let publication = self.begin_query_publication();
        self.root = next_root;
        if directory.materialized.is_none() {
            self.overlay.write().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                .note_merged(OverlayPosition { sequence: committed.sequence, key: None });
        }
        publication.finish(self);
        Ok(EntityCompactionProgress {
            pages_rewritten: (max_leaves - budget.leaves_left) as u32,
            entries_reclaimed: budget.reclaimed,
            resume: budget.resume.map(|key| (key, end.to_vec())),
        })
    }

    fn oldest_live_pin_sequence(&self) -> io::Result<u64> {
        let pins = self
            .snapshot_pins
            .lock()
            .map_err(|_| io::Error::other("entity snapshot pin registry is poisoned"))?;
        Ok(pins
            .snapshots
            .keys()
            .map(|snapshot| snapshot.sequence)
            .min()
            .unwrap_or(u64::MAX))
    }

    /// First transaction still needed to recover this root. Partial coverage
    /// retains the entire boundary transaction, including its uncovered keys.
    pub(crate) fn overlay_replay_first_sequence(&self) -> io::Result<u64> {
        let overlay = self.overlay.read().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
        if overlay.is_empty() {
            return Ok(self.writer_engine()?.state().durable_sequence.saturating_add(1));
        }
        let position = overlay.materialized_position();
        Ok(position.sequence.saturating_add(u64::from(position.key.is_none())).max(1))
    }

    /// Apply every committed-but-unmerged delta to the tree as ONE batch
    /// with per-key original commit versions, committed like an ordinary
    /// entity transaction (WAL record + group barrier). Crash anywhere in
    /// this operation replays to exactly the pre-merge or post-merge state:
    /// the deltas are durable independently of this physical publication,
    /// and recovery resumes from the persisted sequence/key coverage.
    /// Merge one bounded chunk: scan at most `MERGE_CHUNK_ENTRIES` oldest delta
    /// positions into an adaptive number of distinct keys, producing a page set
    /// that always fits the transaction
    /// block-reference budget (the budget the unbounded batch exceeded in
    /// the 2026-09-19 bench failure). Chunks commit in sequence order;
    /// `note_merged` advances the watermark per chunk.
    const MERGE_CHUNK_ENTRIES: usize = 2_048;

    /// Latency-adaptive chunk sizing (AIMD): one chunk commits under the
    /// authority mutex, so on a slow mount a full 2,048-entry merge
    /// serializes every foreground writer behind its page reads/writes and
    /// commit fsync (2026-09-22 FUSE swarm bench: ~0.5-1 s per chunk on a
    /// cold tree, swarm write p99 455-489 ms — one serialized chunk).
    /// The chunk wall time is measured per merge and the distinct-key target
    /// halves past the target window, grows back ×1.5 well under it, and
    /// never leaves [MERGE_CHUNK_ENTRIES_MIN, MERGE_CHUNK_ENTRIES]. Fast
    /// mounts keep full chunks (throughput); slow mounts bound any single
    /// serialized merge window to roughly the target instead of seconds.
    // A reopened tree is cold: the previous process's fast-cache timings
    // cannot justify a maximum-size first hold. Grow from an observed slice,
    // and let slow mounts shrink all the way to one indivisible entry.
    const MERGE_CHUNK_ENTRIES_INITIAL: usize = 64;
    const MERGE_CHUNK_ENTRIES_MIN: usize = 1;
    const MERGE_CHUNK_TIME_TARGET: std::time::Duration =
        std::time::Duration::from_millis(25);

    /// Pure AIMD step, kept separate for deterministic tests.
    fn next_merge_chunk_entries(
        current: usize,
        elapsed: std::time::Duration,
    ) -> usize {
        if elapsed > Self::MERGE_CHUNK_TIME_TARGET {
            (current / 2).max(Self::MERGE_CHUNK_ENTRIES_MIN)
        } else if elapsed < Self::MERGE_CHUNK_TIME_TARGET / 2 {
            (current + (current / 2).max(1)).min(Self::MERGE_CHUNK_ENTRIES)
        } else {
            current
        }
    }

    pub(crate) fn prepare_overlay_merge(&self) -> io::Result<Option<OverlayMergePreparation>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_PREPARE_CAPTURE);
        let engine = self.writer_engine()?;
        engine.require_write_admission()?;
        let overlay = self.overlay.read().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?.clone();
        if overlay.entries() == 0 { return Ok(None); }
        let pin = pin_snapshot(&self.snapshot_pins, self.current_snapshot())?;
        let reclaim_below = self.effective_tombstone_reclaim_below(self.oldest_live_pin_sequence()?);
        Ok(Some(OverlayMergePreparation { database_instance_id: self.database_instance_id,
            root: self.root, overlay, source: ReplicaBlockIo { blocks: engine.block_store_shared(), restart_required: engine.restart_flag() },
            page_cache: Arc::clone(&self.page_cache), pin: Arc::new(pin), reclaim_below,
            limit: self.merge_chunk_entries_adaptive.load(std::sync::atomic::Ordering::Acquire) }))
    }

    pub(crate) fn finish_overlay_merge(&mut self, prepared: PreparedOverlayMerge) -> io::Result<bool> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_PREPARE_PUBLISH);
        let publication_started = std::time::Instant::now();
        self.writer_engine()?.require_write_admission()?;
        if self.database_instance_id != prepared.capture.database_instance_id || self.root != prepared.capture.root {
            crate::fsync_stats::note_stage_elapsed(crate::fsync_stats::STAGE_OVERLAY_PREPARE_STALE, 0);
            return Err(io::Error::new(io::ErrorKind::WouldBlock, "prepared overlay root changed before publication"));
        }
        let has_tail = self.overlay.read().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?.has_work_after(&prepared.boundary);
        let next_root = if has_tail { prepared.root_with_tail } else { prepared.root_without_tail };
        // Root identity was checked before any private block enters shared
        // staging. Normal engine relink accounting and WAL confirmation own
        // publication; a stale prepare never pollutes shared payload storage.
        {
            for (id, payload) in &prepared.payloads {
                if self.writer_engine()?.write_block_fresh_prepared(payload)? != *id {
                    return Err(invalid_data("private merge page identity changed"));
                }
            }
        }
        let record = encode_root(self.writer_engine()?.next_sequence()?, next_root);
        let committed = self.writer_engine_mut()?.commit_references_with_authority_state(&record, &prepared.written_ids, next_root)?;
        let publication = self.begin_query_publication();
        self.root = next_root;
        let mut overlay = self.overlay.write().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
        overlay.note_merged(if has_tail { prepared.boundary } else { OverlayPosition { sequence: committed.sequence, key: None } });
        self.overlay_trigger_bytes.store(overlay.bytes(), std::sync::atomic::Ordering::Release);
        self.overlay_trigger_entries.store(overlay.entries(), std::sync::atomic::Ordering::Release);
        crate::fsync_stats::note_overlay_merge(prepared.actual_entries, prepared.carried_bytes,
            prepared.written_ids.len(), prepared.consumed_positions, prepared.superseded_positions);
        // The 25-ms AIMD target bounds serialized publication. Cold encoding
        // and waiting to reacquire authority do not justify starving a fast
        // publisher with single-entry slices.
        let elapsed = publication_started.elapsed();
        let next = if prepared.actual_entries >= prepared.limit || elapsed > Self::MERGE_CHUNK_TIME_TARGET {
            Self::next_merge_chunk_entries(prepared.limit, elapsed)
        } else { prepared.limit };
        self.merge_chunk_entries_adaptive.store(next, std::sync::atomic::Ordering::Release);
        drop(overlay);
        publication.finish(self);
        Ok(true)
    }

    pub fn merge_overlay(&mut self) -> io::Result<bool> {
        self.merge_overlay_chunk()
    }

    fn merge_overlay_chunk(&mut self) -> io::Result<bool> {
        let started = std::time::Instant::now();
        let current = self.merge_chunk_entries_adaptive.load(std::sync::atomic::Ordering::Acquire);
        let outcome = self.merge_overlay_chunk_with_limit(current);
        let elapsed = started.elapsed();
        // Only a full slice proves capacity for growth. A nearly empty or
        // highly overwritten overlay is not evidence that a large future
        // slice fits. Slow errors still reduce the next admission size.
        let next = match &outcome {
            Ok(Some((limit, actual))) if *actual >= *limit || elapsed > Self::MERGE_CHUNK_TIME_TARGET =>
                Self::next_merge_chunk_entries(*limit, elapsed),
            Ok(Some((limit, _))) => *limit,
            Err(_) if elapsed > Self::MERGE_CHUNK_TIME_TARGET =>
                Self::next_merge_chunk_entries(current, elapsed),
            _ => current,
        };
        self.merge_chunk_entries_adaptive.store(next, std::sync::atomic::Ordering::Release);
        outcome.map(|slice| slice.is_some())
    }

    fn merge_overlay_chunk_with_limit(&mut self, mut chunk_entries: usize) -> io::Result<Option<(usize, usize)>> {
        self.writer_engine()?.require_write_admission()?;
        // Bound both page count and actual staged bytes. Crossing the
        // carried-WAL byte ceiling would otherwise fsync hundreds of loose
        // pages synchronously under authority, turning one merge into a
        // multi-second DolphinFS stall. A single indivisible entry retains
        // the existing spill path rather than making its data unmergeable.
        let (actual_entries, boundary, directory, written_block_ids, next_root, carried_bytes, consumed_positions, superseded_positions) = loop {
            let select_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_SELECT);
            let chunk = self.overlay.read()
                .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                .merge_chunk(chunk_entries, Self::MERGE_CHUNK_ENTRIES);
            let Some(crate::entity_overlay::OverlayMergeChunk {
                entries: batch, position: boundary, consumed_positions, superseded_positions,
            }) = chunk else { return Ok(None); };
            drop(select_timer);
            let actual_entries = batch.len();
            let tree = Tree { backend: self.backend(), page_cache: Some(self.page_cache.as_ref()) };
            let mut directory = tree.split_root(self.root)?;
            let reclaim_below = self.effective_tombstone_reclaim_below(self.oldest_live_pin_sequence()?);
            let (written_root, mut written_block_ids) = {
                let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_APPLY_BATCH);
                tree.apply_batch(directory.base_root, batch, TombstoneReclaim { below: reclaim_below, mounts: &directory.mounts })?
            };
            directory.base_root = written_root;
            directory.materialized = if self.overlay.read()
                .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?.has_work_after(&boundary) {
                Some(boundary.clone())
            } else { None };
            let next_root = if directory.mounts.is_empty() && directory.materialized.is_none() {
                directory.base_root
            } else {
                directory.validate()?;
                Some(tree.store_directory(&directory, &mut written_block_ids)?)
            };
            written_block_ids.extend(next_root);
            written_block_ids.sort_unstable();
            written_block_ids.dedup();
            let blocks = self.writer_engine()?.block_store_shared();
            let carry_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_CARRY_BUDGET);
            let mut carried_bytes = 0_usize;
            for id in &written_block_ids {
                if let Some(payload) = blocks.pending_payload(*id)? {
                    carried_bytes = carried_bytes.checked_add(payload.len())
                        .ok_or_else(|| invalid_data("overlay carried-byte count overflow"))?;
                }
            }
            drop(carry_timer);
            let fits_references = u32::try_from(written_block_ids.len()).is_ok();
            let fits_carried = carried_bytes <= crate::transaction::MAX_CARRIED_PAYLOAD_BYTES;
            if fits_references && (fits_carried || chunk_entries == 1) {
                break (actual_entries, boundary, directory, written_block_ids, next_root, carried_bytes, consumed_positions, superseded_positions);
            }
            if chunk_entries == 1 {
                return Err(invalid_data("single overlay merge entry exceeds the transaction block budget"));
            }
            chunk_entries = (chunk_entries / 2).max(1);
        };
        let root_record = encode_root(self.writer_engine()?.next_sequence()?, next_root);
        let committed = self.writer_engine_mut()?.commit_references_with_authority_state(
            &root_record, &written_block_ids, next_root)?;
        let publication = self.begin_query_publication();
        self.root = next_root;
        let mut overlay = self.overlay.write()
            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
        overlay.note_merged(if directory.materialized.is_some() { boundary }
            else { OverlayPosition { sequence: committed.sequence, key: None } });
        self.overlay_trigger_bytes.store(overlay.bytes(), std::sync::atomic::Ordering::Release);
        self.overlay_trigger_entries.store(overlay.entries(), std::sync::atomic::Ordering::Release);
        crate::fsync_stats::note_overlay_merge(actual_entries, carried_bytes, written_block_ids.len(), consumed_positions, superseded_positions);
        drop(overlay);
        publication.finish(self);
        Ok(Some((chunk_entries, actual_entries)))
    }

    /// Drain all pending chunks (used by budget triggers, maintenance,
    /// backup, GC, and tests).
    pub fn merge_overlay_fully(&mut self) -> io::Result<()> {
        while self.merge_overlay_chunk()? {}
        Ok(())
    }

    /// Merge when the overlay or the active WAL approaches its bound. The
    /// overlay budget bounds memory and replay cost; the WAL watermark keeps
    /// delta records well ahead of rotation so a checkpoint never publishes
    /// with an un-merged overlay in the common path (history replay covers
    /// the rest).
    fn merge_overlay_if_needed(&mut self) -> io::Result<()> {
        // Lock-free trigger check: two atomic loads, never the overlay
        // lock (a running merge holds its write lock for the whole batch).
        let overlay_bytes = self
            .overlay_trigger_bytes
            .load(std::sync::atomic::Ordering::Acquire);
        let overlay_entries = self
            .overlay_trigger_entries
            .load(std::sync::atomic::Ordering::Acquire);
        if overlay_bytes >= self.overlay_budget_bytes
            || overlay_entries >= OVERLAY_MAX_SCAN_ENTRIES
            || self.writer_engine()?.wal_len() >= self.overlay_wal_watermark_bytes
        {
            if self.writer_engine()?.wal_len() >= self.overlay_wal_watermark_bytes {
                crate::fsync_stats::note_overlay_wal_pressure(overlay_entries);
            }
            // Hard backstop, bounded (2026-09-21 FUSE brownout): this used
            // to run a FULL overlay merge inside the foreground commit —
            // seconds-to-minutes inside the authority hold on a degraded
            // mount, wedging every other request behind its wire deadline.
            // The commit path now merges ONE bounded chunk per commit (the
            // identical slice the background drain commits): 2,048 entries
            // per chunk drains far faster than one commit appends, so the
            // backlog still converges, amortized across commits; a burst
            // that outpaces the chunk rate degrades to the certified
            // active-WAL-ceiling rotation path instead of one unbounded
            // in-commit merge.
            self.merge_overlay_chunk()?;
        }
        Ok(())
    }

    /// Tree-materializing structural operations (range capsule extraction)
    /// read their blocks from `transaction.snapshot.root`, which stays empty
    /// or stale while committed content sits in the write overlay. When the
    /// transaction snapshots the current head, flush the overlay first and
    /// refresh the snapshot — the merge preserves logical content, so the
    /// transaction's witnesses stay valid (the same skip `prepare_commit`
    /// applies to its own flush-first). Stale views materialize their own
    /// captured immutable blocks into a private pinned root.
    fn flush_overlay_for_materialization(
        &mut self,
        transaction: &mut EntityTransaction,
    ) -> io::Result<()> {
        let was_current = transaction.snapshot == self.current_snapshot();
        if was_current {
            self.merge_overlay_fully()?;
            let snapshot = self.current_snapshot();
            transaction.snapshot_pin.repin(snapshot)?;
            transaction.snapshot = snapshot;
        } else {
            // A stale snapshot may still own pre-merge deltas after the
            // writer drained them. Materialize that captured view privately;
            // publishing a capsule must never omit its immutable overlay.
            let batch = transaction.overlay_view.materialization_batch();
            if !batch.is_empty() {
                let tree = Tree { backend: self.backend(), page_cache: Some(&self.page_cache) };
                let mut directory = tree.split_root(transaction.snapshot.root)?;
                let (base, mut blocks) = tree.apply_batch(directory.base_root, batch, TombstoneReclaim { below: 0, mounts: &directory.mounts })?;
                directory.base_root = base;
                directory.materialized = None;
                let root = if directory.mounts.is_empty() { base }
                    else { Some(tree.store_directory(&directory, &mut blocks)?) };
                let snapshot = EntitySnapshot { root, sequence: transaction.snapshot.sequence };
                transaction.snapshot_pin.repin(snapshot)?;
                transaction.snapshot = snapshot;
                transaction.staged_reference_blocks.extend(blocks);
            }
        }
        transaction.overlay_view = EntityOverlayView::default();
        Ok(())
    }

    /// Rebuild the exact uncovered WAL suffix, using partial root coverage
    /// when present and complete-root records otherwise. Legacy roots keep
    /// their original recovery boundary; new publications never infer full
    /// materialization from root equality.
    fn replay_overlay_deltas(&self) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_REPLAY);
        let engine = self.writer_engine()?;
        let head = engine.state().durable_sequence;
        let directory = Tree { backend: self.backend(), page_cache: Some(self.page_cache.as_ref()) }
            .split_root(self.root)?;
        if let Some(position) = directory.materialized {
            if position.sequence > head { return Err(invalid_data("materialization position is beyond durable head")); }
            let mut deltas = Vec::new();
            if let Some(first) = position.sequence.checked_add(u64::from(position.key.is_none())) {
                engine.visit_transaction_metadata_from(first.max(1), |transaction| {
                    let sequence = transaction.sequence;
                    if let Some(records) = crate::transaction::decode_family_records(&transaction.envelope.inline_payload)? {
                        for record in records {
                            if record.kind == crate::transaction::FamilyRecordKind::EntityOverlay {
                                let mut delta = crate::entity_overlay::decode_overlay_delta(record.payload)?;
                                delta.retain(|key, _| !position.covers(sequence, key));
                                if !delta.is_empty() { deltas.push((sequence, delta)); }
                            }
                        }
                    }
                    Ok(())
                })?;
            }
            let mut overlay = self.overlay.write().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
            overlay.note_merged(position);
            for (sequence, delta) in deltas { overlay.apply_delta(sequence, delta); }
            self.overlay_trigger_bytes.store(overlay.bytes(), std::sync::atomic::Ordering::Release);
            self.overlay_trigger_entries.store(overlay.entries(), std::sync::atomic::Ordering::Release);
            return Ok(());
        }
        if head == 0 || head <= engine.state().checkpoint_sequence {
            // Legacy complete roots need no history I/O. Every newly deferred
            // mutation now publishes an explicit root coverage cursor above,
            // so indexed retirement cannot use this legacy shortcut.
            self.overlay.write().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?
                .note_merged(OverlayPosition { sequence: head, key: None });
            return Ok(());
        }
        // WAL retirement is a physical transition, not evidence that entity
        // deltas reached the tree. Follow retained history through the actual
        // complete-root boundary even when the active WAL is empty.
        let mut deltas = Vec::new();
        for sequence in (1..=head).rev() {
            let Some(transaction) = engine.transaction_at(sequence)? else {
                if !deltas.is_empty() {
                    return Err(invalid_data("unmaterialized legacy overlay history is missing"));
                }
                // A complete legacy tree may have explicitly pruned history.
                break;
            };
            match transaction.envelope.authority_state_update {
                None => {}
                Some(root) if root == self.root => {}
                Some(_) => break,
            }
            if is_complete_root_record(&transaction.envelope.inline_payload) { break; }
            if let Some(records) = crate::transaction::decode_family_records(&transaction.envelope.inline_payload)? {
                if records.iter().any(|record| record.kind == crate::transaction::FamilyRecordKind::EntityRoot && is_complete_root_record(record.payload)) { break; }
                for record in records {
                    if record.kind == crate::transaction::FamilyRecordKind::EntityOverlay {
                        deltas.push((sequence, crate::entity_overlay::decode_overlay_delta(record.payload)?));
                    }
                }
            }
        }
        deltas.sort_unstable_by_key(|(sequence, _)| *sequence);
        let mut overlay = self
            .overlay
            .write()
            .map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
        let covered = deltas.first().map_or(head, |(sequence, _)| sequence.saturating_sub(1));
        overlay.note_merged(OverlayPosition { sequence: covered, key: None });
        for (sequence, delta) in deltas {
            overlay.apply_delta(sequence, delta);
        }
        self.overlay_trigger_bytes
            .store(overlay.bytes(), std::sync::atomic::Ordering::Release);
        self.overlay_trigger_entries
            .store(overlay.entries(), std::sync::atomic::Ordering::Release);
        Ok(())
    }

    pub(crate) fn writer_engine(&self) -> io::Result<&Engine> {
        match &self.persona {
            EntityPersona::Writer(engine) => Ok(engine),
            EntityPersona::Replica(_) => Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "entity read replica has no writer engine",
            )),
        }
    }

    pub(crate) const fn transaction_is_writable(transaction: &EntityTransaction) -> bool {
        transaction.writable
    }

    pub fn writer_engine_mut(&mut self) -> io::Result<&mut Engine> {
        match &mut self.persona {
            EntityPersona::Writer(engine) => Ok(engine),
            EntityPersona::Replica(_) => Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "entity read replica has no writer engine",
            )),
        }
    }

    pub fn snapshot(&self) -> EntitySnapshot {
        self.current_snapshot()
    }

    /// Distinct snapshots currently pinned by transient reader/writer pins.
    /// The online garbage collector marks the union of these roots with the
    /// current root so reclamation never deletes a page any live snapshot can
    /// still read (docs/TOFUDB_ONLINE_GC.md, rule S2). Persistent root pins
    /// are not listed here: they are durable and are discovered by the
    /// reachability walk itself.
    pub(crate) fn pinned_snapshots(&self) -> io::Result<Vec<EntitySnapshot>> {
        let pins = self
            .snapshot_pins
            .lock()
            .map_err(|_| io::Error::other("entity snapshot pin registry is poisoned"))?;
        Ok(pins.snapshots.keys().copied().collect())
    }

    /// Pending physical work and immutable bytes still owned by readers are
    /// separate counters. A partial block may retain its covered prefix.
    pub(crate) fn overlay_memory_bytes(&self) -> io::Result<(usize, usize)> {
        let overlay = self.overlay.read().map_err(|_| io::Error::other("entity overlay lock is poisoned"))?;
        Ok((overlay.bytes(), overlay.resident_bytes()))
    }

    pub fn snapshot_pin_metrics(&self) -> io::Result<EntitySnapshotPinMetrics> {
        let pins = self
            .snapshot_pins
            .lock()
            .map_err(|_| io::Error::other("entity snapshot pin registry is poisoned"))?;
        Ok(EntitySnapshotPinMetrics {
            active_handles: u32::try_from(pins.active_handles)
                .map_err(|_| invalid_data("entity snapshot handle count overflow"))?,
            distinct_snapshots: u32::try_from(pins.snapshots.len())
                .map_err(|_| invalid_data("entity snapshot count overflow"))?,
            oldest_sequence: pins
                .snapshots
                .keys()
                .map(|snapshot| snapshot.sequence)
                .min(),
            retained_transaction_bytes: self.snapshot_pins.retained_bytes(),
        })
    }
}

#[cfg(test)]
mod tests {
    include!("entity_tests.rs");
    include!("entity_tombstone_tests.rs");

    #[test]
    fn private_overlay_encoding_crosses_retired_byte_quota_and_releases_reservations() {
        for just_below_retired_threshold in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            database.set_write_overlay_enabled(true);
            let mut transaction = database.begin(7, 11).unwrap();
            database.put(&mut transaction, key(0), vec![9; 512]).unwrap();
            database.commit(transaction).unwrap();
            let preparation = database.prepare_overlay_merge().unwrap().unwrap();
            let chunk = preparation.overlay.merge_chunk(preparation.limit, EntityDatabase::MERGE_CHUNK_ENTRIES).unwrap();
            let batch_bytes = chunk.entries.iter().fold(0_usize, |bytes, (key, value, _)|
                bytes + key.capacity() + value.as_ref().map_or(0, Vec::capacity) + 64);
            let pressure_bytes = if just_below_retired_threshold {
                LEGACY_AGGREGATE_TRANSACTION_BYTES - batch_bytes - 1
            } else { LEGACY_AGGREGATE_TRANSACTION_BYTES };
            // Record the retired threshold without allocating a huge
            // fixture. This lease represents concurrent foreground buffers.
            let mut pressure = preparation.pin.lease();
            pressure.reserve_transaction_bytes(pressure_bytes).unwrap();
            let prepared = preparation.encode().unwrap();
            assert!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes > pressure_bytes);
            assert!(database.finish_overlay_merge(prepared).unwrap());
            assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, pressure_bytes);
            assert_eq!(database.overlay.read().unwrap().entries(), 0);
            drop(pressure);
            assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
            assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
            let mut transaction = database.begin(7, 11).unwrap();
            database.put(&mut transaction, key(1), vec![8; 512]).unwrap();
            database.commit(transaction).unwrap();
            let prepared = database.prepare_overlay_merge().unwrap().unwrap().encode().unwrap();
            assert!(database.finish_overlay_merge(prepared).unwrap());
            assert_eq!(database.overlay.read().unwrap().entries(), 0);
            assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
            let mut read = database.begin(7, 11).unwrap();
            assert_eq!(database.get(&mut read, &key(0)).unwrap(), Some(vec![9; 512]));
            assert_eq!(database.get(&mut read, &key(1)).unwrap(), Some(vec![8; 512]));
        }
    }
}

#[cfg(test)]
mod optimistic_tests {
    include!("entity_optimistic_tests.rs");
}

#[cfg(test)]
#[path = "entity_read_reuse_tests.rs"]
mod read_reuse_tests;

#[cfg(test)]
#[path = "entity_range_demand_tests.rs"]
mod range_demand_tests;
