//! Owner-scoped MVCC entities on immutable copy-on-write B+Tree pages.

use std::collections::BTreeMap;
use std::io;
use std::sync::{Arc, Mutex};

use crate::block::BlockId;
use crate::engine::Engine;
use crate::generated_tofudb_ir::{
    MAX_AGGREGATE_ENTITY_TRANSACTION_BYTES, MAX_ENTITY_INLINE_VALUE_BYTES, MAX_ENTITY_KEY_BYTES,
    MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES, MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS,
    MAX_ENTITY_POINT_WITNESSES, MAX_ENTITY_RANGE_ROWS, MAX_ENTITY_RANGE_WITNESSES,
    MAX_ENTITY_REACHABILITY_FRONTIER, MAX_ENTITY_REACHABILITY_PAGES, MAX_ENTITY_RETIRED_RANGES,
    MAX_ENTITY_ROOT_RANGE_MOUNTS, MAX_ENTITY_TRANSACTION_WRITES,
    MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS, MAX_PERSISTENT_ENTITY_CAPSULE_RANGES,
    MAX_PERSISTENT_ENTITY_ROOT_PINS, MAX_PERSISTENT_ENTITY_ROOT_PIN_ID_BYTES,
    MAX_PINNED_ENTITY_SNAPSHOTS, PERSISTENT_ENTITY_ROOT_PIN_CATALOG_NAMESPACE,
    PERSISTENT_ENTITY_ROOT_PIN_NAMESPACE,
};
use crate::vfs::Vfs;
use uuid::Uuid;

const PAGE_MAGIC: &[u8; 8] = b"TDBENT01";
const ROOT_MAGIC: &[u8; 8] = b"TDBROOT1";
const PAGE_VERSION: u32 = 1;
const ROOT_VERSION: u32 = 1;
const ROOT_DIRECTORY_MAGIC: &[u8; 8] = b"TDBRDIR1";
const ROOT_DIRECTORY_VERSION: u32 = 1;
const PAGE_TARGET_BYTES: usize = 16 * 1024;
const MAX_NAMESPACE_BYTES: usize = 63;
const MAX_TREE_DEPTH: usize = 16;
const MAX_RANGE_WITNESS_LEAVES: usize = 1_024;
const MAX_INTERNAL_RANGE_ROWS: usize = MAX_ENTITY_RANGE_ROWS + MAX_ENTITY_TRANSACTION_WRITES;
const POINT_WITNESS_ACCOUNTING_OVERHEAD: usize = 64;
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
}

enum RootNode {
    Page(Page),
    Directory(RootDirectory),
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
pub struct EntityTransaction {
    database_instance_id: Uuid,
    tenant_id: u64,
    owner_user_id: u64,
    additional_scope_prefixes: Vec<Vec<u8>>,
    snapshot: EntitySnapshot,
    writable: bool,
    point_witnesses: BTreeMap<Vec<u8>, Option<u64>>,
    range_witnesses: Vec<RangeWitness>,
    retired_ranges: Vec<(Vec<u8>, Vec<u8>)>,
    replacement_ranges: Vec<(Vec<u8>, Vec<u8>)>,
    mounted_ranges: Vec<RangeMount>,
    staged_reference_blocks: Vec<BlockId>,
    writes: BTreeMap<Vec<u8>, Option<Vec<u8>>>,
    snapshot_pin: SnapshotPin,
}

pub struct EntityDatabase {
    engine: Engine,
    root: Option<BlockId>,
    database_instance_id: Uuid,
    snapshot_pins: Arc<Mutex<SnapshotPinState>>,
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
    retained_transaction_bytes: usize,
}

#[derive(Debug)]
struct SnapshotPin {
    snapshot: EntitySnapshot,
    state: Arc<Mutex<SnapshotPinState>>,
    retained_transaction_bytes: usize,
}

impl SnapshotPin {
    fn reserve_transaction_bytes(&mut self, bytes: usize) -> io::Result<()> {
        let transaction_next = self
            .retained_transaction_bytes
            .checked_add(bytes)
            .ok_or_else(|| invalid_data("entity transaction byte accounting overflow"))?;
        let mut state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("entity snapshot pin registry is poisoned"))?;
        let next = state
            .retained_transaction_bytes
            .checked_add(bytes)
            .filter(|next| *next <= MAX_AGGREGATE_ENTITY_TRANSACTION_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "aggregate entity transaction byte budget is exhausted",
                )
            })?;
        state.retained_transaction_bytes = next;
        self.retained_transaction_bytes = transaction_next;
        Ok(())
    }

    fn release_transaction_bytes(&mut self, bytes: usize) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.retained_transaction_bytes = state.retained_transaction_bytes.saturating_sub(bytes);
        self.retained_transaction_bytes = self.retained_transaction_bytes.saturating_sub(bytes);
    }
}

impl Drop for SnapshotPin {
    fn drop(&mut self) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
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
        state.retained_transaction_bytes = state
            .retained_transaction_bytes
            .saturating_sub(self.retained_transaction_bytes);
    }
}

pub(crate) struct PreparedEntityCommit {
    expected_sequence: u64,
    next_root: Option<BlockId>,
    written_block_ids: Vec<BlockId>,
    root_record: Option<Vec<u8>>,
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
}

struct Tree<'a> {
    engine: &'a Engine,
}

type ScanResult = (Vec<(Vec<u8>, EntityValue)>, Vec<BlockId>);

struct PersistentPinState {
    pin_key: EntityKey,
    encoded_snapshot: Option<Vec<u8>>,
    count_key: EntityKey,
    count: usize,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn conflict(message: &str) -> io::Error {
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

fn decode_persistent_root_pin(encoded: &[u8]) -> io::Result<EntitySnapshot> {
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
    Ok(EntitySnapshot {
        sequence,
        root: Some(BlockId(root)),
    })
}

fn pin_snapshot(
    state: &Arc<Mutex<SnapshotPinState>>,
    snapshot: EntitySnapshot,
) -> io::Result<SnapshotPin> {
    let mut pins = state
        .lock()
        .map_err(|_| io::Error::other("entity snapshot pin registry is poisoned"))?;
    if pins.active_handles == MAX_PINNED_ENTITY_SNAPSHOTS {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "entity snapshot pin capacity is exhausted",
        ));
    }
    pins.active_handles += 1;
    *pins.snapshots.entry(snapshot).or_default() += 1;
    drop(pins);
    Ok(SnapshotPin {
        snapshot,
        state: Arc::clone(state),
        retained_transaction_bytes: 0,
    })
}

fn point_witness_reservation_bytes(key: &[u8]) -> usize {
    key.len() + POINT_WITNESS_ACCOUNTING_OVERHEAD
}

fn range_witness_reservation_bytes(start: &[u8], end: &[u8]) -> usize {
    start.len()
        + end.len()
        + MAX_RANGE_WITNESS_LEAVES * std::mem::size_of::<BlockId>() * 2
        + RANGE_WITNESS_ACCOUNTING_OVERHEAD
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
        bytes.extend_from_slice(&ROOT_DIRECTORY_VERSION.to_le_bytes());
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
        Ok(bytes)
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < 16
            || &bytes[..8] != ROOT_DIRECTORY_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != ROOT_DIRECTORY_VERSION
            || bytes[12] > 1
            || bytes[13] != 0
        {
            return Err(invalid_data("invalid entity root directory header"));
        }
        let has_base = bytes[12] == 1;
        let count = u16::from_le_bytes(bytes[14..16].try_into().unwrap()) as usize;
        if count == 0 || count > MAX_ENTITY_ROOT_RANGE_MOUNTS {
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
        if offset != bytes.len() {
            return Err(invalid_data("entity root directory has trailing bytes"));
        }
        let directory = Self { base_root, mounts };
        directory.validate()?;
        Ok(directory)
    }

    fn validate(&self) -> io::Result<()> {
        if self.mounts.is_empty()
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

    fn encode(&self) -> io::Result<Vec<u8>> {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(PAGE_MAGIC);
        bytes.extend_from_slice(&PAGE_VERSION.to_le_bytes());
        match self {
            Self::Leaf(entries) => {
                bytes.push(0);
                bytes.push(0);
                push_u16(&mut bytes, entries.len())?;
                for (key, entity) in entries {
                    push_u16(&mut bytes, key.len())?;
                    push_u16(
                        &mut bytes,
                        entity.value.as_ref().map_or(0, |value| value.len()),
                    )?;
                    bytes.extend_from_slice(&entity.version.to_le_bytes());
                    bytes.push(u8::from(entity.value.is_none()));
                    bytes.extend_from_slice(key);
                    if let Some(value) = &entity.value {
                        bytes.extend_from_slice(value);
                    }
                }
            }
            Self::Internal { level, children } => {
                if *level == 0 {
                    return Err(invalid_data("internal entity page has leaf level"));
                }
                bytes.push(1);
                bytes.push(*level);
                push_u16(&mut bytes, children.len())?;
                for child in children {
                    push_u16(&mut bytes, child.lower_bound.len())?;
                    bytes.extend_from_slice(&child.lower_bound);
                    bytes.extend_from_slice(&child.block_id.0);
                }
            }
        }
        Ok(bytes)
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < 16 || bytes.len() > PAGE_TARGET_BYTES || &bytes[..8] != PAGE_MAGIC {
            return Err(invalid_data("entity page magic mismatch"));
        }
        if u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != PAGE_VERSION {
            return Err(invalid_data("unsupported entity page version"));
        }
        let kind = bytes[12];
        let level = bytes[13];
        let count = u16::from_le_bytes(bytes[14..16].try_into().unwrap()) as usize;
        let mut offset = 16;
        let page = match kind {
            0 if level == 0 => {
                let mut entries = Vec::with_capacity(count);
                for _ in 0..count {
                    let key_len = read_u16(bytes, &mut offset)?;
                    let value_len = read_u16(bytes, &mut offset)?;
                    let version =
                        u64::from_le_bytes(take(bytes, &mut offset, 8)?.try_into().unwrap());
                    let tombstone = take(bytes, &mut offset, 1)?[0];
                    if key_len > MAX_ENTITY_KEY_BYTES + 82
                        || value_len > MAX_ENTITY_INLINE_VALUE_BYTES
                        || tombstone > 1
                    {
                        return Err(invalid_data("unbounded entity leaf entry"));
                    }
                    let key = take(bytes, &mut offset, key_len)?.to_vec();
                    let value = if tombstone == 1 {
                        if value_len != 0 {
                            return Err(invalid_data("entity tombstone carries bytes"));
                        }
                        None
                    } else {
                        Some(take(bytes, &mut offset, value_len)?.to_vec())
                    };
                    entries.push((key, EntityValue { version, value }));
                }
                Self::Leaf(entries)
            }
            1 if level > 0 && level as usize <= MAX_TREE_DEPTH => {
                let mut children = Vec::with_capacity(count);
                for _ in 0..count {
                    let key_len = read_u16(bytes, &mut offset)?;
                    if key_len > MAX_ENTITY_KEY_BYTES + 82 {
                        return Err(invalid_data("unbounded entity internal key"));
                    }
                    children.push(Child {
                        lower_bound: take(bytes, &mut offset, key_len)?.to_vec(),
                        block_id: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                    });
                }
                Self::Internal { level, children }
            }
            _ => return Err(invalid_data("invalid entity page kind or level")),
        };
        if offset != bytes.len() {
            return Err(invalid_data("entity page has trailing bytes"));
        }
        page.validate()?;
        Ok(page)
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
                    let pinned_root = snapshot.root.unwrap();
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
    fn load(&self, id: BlockId) -> io::Result<Page> {
        Page::decode(&self.engine.read_block(id)?)
    }

    fn load_root(&self, id: BlockId) -> io::Result<RootNode> {
        let encoded = self.engine.read_block(id)?;
        if encoded.starts_with(ROOT_DIRECTORY_MAGIC) {
            RootDirectory::decode(&encoded).map(RootNode::Directory)
        } else {
            Page::decode(&encoded).map(RootNode::Page)
        }
    }

    fn store_directory(
        &self,
        directory: &RootDirectory,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<BlockId> {
        let block_id = self.engine.write_block(&directory.encode()?)?;
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
            });
        };
        match self.load_root(root)? {
            RootNode::Page(_) => Ok(RootDirectory {
                base_root: Some(root),
                mounts: Vec::new(),
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
        page.validate()?;
        let encoded = page.encode()?;
        if encoded.len() > PAGE_TARGET_BYTES {
            return Err(invalid_data("entity page exceeds target after split"));
        }
        let block_id = self.engine.write_block(&encoded)?;
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

    fn get_root(
        &self,
        root: BlockId,
        key: &[u8],
        directory_depth: usize,
    ) -> io::Result<Option<EntityValue>> {
        if directory_depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity root directory exceeds maximum depth"));
        }
        match self.load_root(root)? {
            RootNode::Page(page) => self.get_page(page, key),
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

    fn get_page(&self, page: Page, key: &[u8]) -> io::Result<Option<EntityValue>> {
        let mut current_page = page;
        for _ in 0..=MAX_TREE_DEPTH {
            match current_page {
                Page::Leaf(entries) => {
                    return Ok(entries
                        .binary_search_by(|entry| entry.0.as_slice().cmp(key))
                        .ok()
                        .map(|index| entries[index].1.clone()));
                }
                Page::Internal { children, .. } => {
                    let index =
                        children.partition_point(|child| child.lower_bound.as_slice() <= key);
                    current_page = self.load(children[index.saturating_sub(1)].block_id)?;
                }
            }
        }
        Err(invalid_data("entity tree exceeds maximum depth"))
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
        let mut start = 0;
        while start < entries.len() {
            let mut used = 16;
            let mut end = start;
            while end < entries.len() {
                let entry = &entries[end];
                let entry_bytes =
                    2 + 2 + 8 + 1 + entry.0.len() + entry.1.value.as_ref().map_or(0, Vec::len);
                if used + entry_bytes > PAGE_TARGET_BYTES {
                    break;
                }
                used += entry_bytes;
                end += 1;
            }
            if end == start {
                return Err(invalid_data("entity entry cannot fit in one page"));
            }
            pages.push(self.store(&Page::Leaf(entries[start..end].to_vec()), written_block_ids)?);
            start = end;
        }
        Ok(pages)
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
        let mut current = Vec::new();
        let mut used = 16;
        for child in children {
            let child_bytes = 2 + child.lower_bound.len() + 32;
            if !current.is_empty() && used + child_bytes > PAGE_TARGET_BYTES {
                groups.push(current);
                current = Vec::new();
                used = 16;
            }
            if used + child_bytes > PAGE_TARGET_BYTES {
                return Err(invalid_data("entity child cannot fit in one internal page"));
            }
            used += child_bytes;
            current.push(child);
        }
        groups.push(current);
        if groups.len() > 1 && groups.last().is_some_and(|group| group.len() == 1) {
            let previous_index = groups.len() - 2;
            let child = groups
                .get_mut(previous_index)
                .and_then(Vec::pop)
                .ok_or_else(|| invalid_data("cannot balance entity internal pages"))?;
            groups.last_mut().unwrap().insert(0, child);
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
        writes: Vec<(Vec<u8>, Option<Vec<u8>>)>,
        version: u64,
    ) -> io::Result<(BlockId, Vec<BlockId>)> {
        if writes.is_empty() {
            return root
                .map(|root| (root, Vec::new()))
                .ok_or_else(|| invalid_data("empty entity batch has no root"));
        }
        let mut written_block_ids = Vec::new();
        let (mut replacements, mut level) = if let Some(root) = root {
            let root_page = self.load(root)?;
            let level = match &root_page {
                Page::Leaf(_) => 0,
                Page::Internal { level, .. } => *level,
            };
            (
                self.apply_page(root_page, &writes, version, 0, &mut written_block_ids)?,
                level,
            )
        } else {
            let entries = writes
                .into_iter()
                .map(|(key, value)| (key, EntityValue { version, value }))
                .collect();
            (self.pack_leaves(entries, &mut written_block_ids)?, 0)
        };
        while replacements.len() > 1 {
            level = level
                .checked_add(1)
                .ok_or_else(|| invalid_data("entity tree level overflow"))?;
            if level as usize > MAX_TREE_DEPTH {
                return Err(invalid_data("entity tree depth limit reached"));
            }
            replacements = self.pack_internal(level, replacements, &mut written_block_ids)?;
        }
        Ok((replacements[0].block_id, written_block_ids))
    }

    fn apply_page(
        &self,
        page: Page,
        writes: &[(Vec<u8>, Option<Vec<u8>>)],
        version: u64,
        depth: usize,
        written_block_ids: &mut Vec<BlockId>,
    ) -> io::Result<Vec<Child>> {
        if depth > MAX_TREE_DEPTH || writes.is_empty() {
            return Err(invalid_data("invalid entity batch recursion"));
        }
        match page {
            Page::Leaf(entries) => {
                let mut merged: BTreeMap<Vec<u8>, EntityValue> = entries.into_iter().collect();
                for (key, value) in writes {
                    merged.insert(
                        key.clone(),
                        EntityValue {
                            version,
                            value: value.clone(),
                        },
                    );
                }
                self.pack_leaves(merged.into_iter().collect(), written_block_ids)
            }
            Page::Internal { level, children } => {
                let mut grouped = vec![Vec::new(); children.len()];
                for (key, value) in writes {
                    let index = children
                        .partition_point(|child| child.lower_bound.as_slice() <= key.as_slice());
                    grouped[index.saturating_sub(1)].push((key.clone(), value.clone()));
                }
                let mut next_children = Vec::new();
                for (child, child_writes) in children.into_iter().zip(grouped) {
                    if child_writes.is_empty() {
                        next_children.push(child);
                    } else {
                        next_children.extend(self.apply_page(
                            self.load(child.block_id)?,
                            &child_writes,
                            version,
                            depth + 1,
                            written_block_ids,
                        )?);
                    }
                }
                self.pack_internal(level, next_children, written_block_ids)
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
                    &RootDirectory { base_root, mounts },
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

    fn scan(
        &self,
        root: Option<BlockId>,
        start: &[u8],
        end: &[u8],
        limit: usize,
    ) -> io::Result<ScanResult> {
        if start >= end || limit == 0 || limit > MAX_INTERNAL_RANGE_ROWS {
            return Err(invalid_input("invalid or unbounded entity range"));
        }
        let mut rows = Vec::new();
        let mut leaves = Vec::new();
        if let Some(root) = root {
            rows = self.scan_root(root, start, end, limit, &mut leaves, 0, false)?;
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
        directory_depth: usize,
        reverse: bool,
    ) -> io::Result<Vec<(Vec<u8>, EntityValue)>> {
        if directory_depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity root directory exceeds maximum depth"));
        }
        match self.load_root(root)? {
            RootNode::Page(_) => {
                let mut rows = Vec::new();
                if reverse {
                    self.scan_page_reverse(root, start, end, limit, &mut rows, leaves, 0)?;
                } else {
                    self.scan_page(root, start, end, limit, &mut rows, leaves, 0)?;
                }
                Ok(rows)
            }
            RootNode::Directory(directory) => {
                if leaves.len() == MAX_RANGE_WITNESS_LEAVES {
                    return Err(invalid_input("entity range witness exceeds 1024 leaves"));
                }
                leaves.push(root);
                let mut visible = BTreeMap::new();
                if let Some(base_root) = directory.base_root {
                    for (key, value) in self.scan_root(
                        base_root,
                        start,
                        end,
                        limit,
                        leaves,
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
        depth: usize,
    ) -> io::Result<()> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        match self.load(id)? {
            Page::Leaf(entries) => {
                if leaves.len() == MAX_RANGE_WITNESS_LEAVES {
                    return Err(invalid_input("entity range witness exceeds 1024 leaves"));
                }
                leaves.push(id);
                for (key, value) in entries {
                    if key.as_slice() >= start
                        && key.as_slice() < end
                        && value.value.is_some()
                        && rows.len() < limit
                    {
                        rows.push((key, value));
                    }
                }
            }
            Page::Internal { children, .. } => {
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
                        self.scan_page(child.block_id, start, end, limit, rows, leaves, depth + 1)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn scan_reverse(
        &self,
        root: Option<BlockId>,
        start: &[u8],
        end: &[u8],
        limit: usize,
    ) -> io::Result<ScanResult> {
        if start >= end || limit == 0 || limit > MAX_INTERNAL_RANGE_ROWS {
            return Err(invalid_input("invalid or unbounded reverse entity range"));
        }
        let mut rows = Vec::new();
        let mut leaves = Vec::new();
        if let Some(root) = root {
            rows = self.scan_root(root, start, end, limit, &mut leaves, 0, true)?;
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
        depth: usize,
    ) -> io::Result<()> {
        if depth > MAX_TREE_DEPTH {
            return Err(invalid_data("entity tree exceeds maximum depth"));
        }
        match self.load(id)? {
            Page::Leaf(entries) => {
                if leaves.len() == MAX_RANGE_WITNESS_LEAVES {
                    return Err(invalid_input("entity range witness exceeds 1024 leaves"));
                }
                leaves.push(id);
                for (key, value) in entries.into_iter().rev() {
                    if key.as_slice() >= start
                        && key.as_slice() < end
                        && value.value.is_some()
                        && rows.len() < limit
                    {
                        rows.push((key, value));
                    }
                }
            }
            Page::Internal { children, .. } => {
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
                            depth + 1,
                        )?;
                    }
                }
            }
        }
        Ok(())
    }
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
        Ok(Self {
            engine: Engine::initialize(data_dir)?,
            root: None,
            database_instance_id: Uuid::new_v4(),
            snapshot_pins: Default::default(),
        })
    }

    pub fn initialize_with_vfs(data_dir: &std::path::Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Ok(Self {
            engine: Engine::initialize_with_vfs(data_dir, vfs)?,
            root: None,
            database_instance_id: Uuid::new_v4(),
            snapshot_pins: Default::default(),
        })
    }

    pub fn open(data_dir: &std::path::Path) -> io::Result<Self> {
        Self::open_engine(Engine::open(data_dir)?)
    }

    pub fn open_with_vfs(data_dir: &std::path::Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Self::open_engine(Engine::open_with_vfs(data_dir, vfs)?)
    }

    fn open_engine(engine: Engine) -> io::Result<Self> {
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
            Tree { engine: &engine }.load_root(root)?;
        }
        Ok(Self {
            engine,
            root,
            database_instance_id: Uuid::new_v4(),
            snapshot_pins: Default::default(),
        })
    }

    pub fn begin(&self, tenant_id: u64, owner_user_id: u64) -> io::Result<EntityTransaction> {
        self.begin_with_additional_scope_prefixes(tenant_id, owner_user_id, Vec::new())
    }

    pub(crate) fn begin_with_additional_scope_prefixes(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        additional_scope_prefixes: Vec<Vec<u8>>,
    ) -> io::Result<EntityTransaction> {
        self.engine.require_usable_authority()?;
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input(
                "tenant and owner identities must be positive",
            ));
        }
        let snapshot = EntitySnapshot {
            sequence: self.engine.state().durable_sequence,
            root: self.root,
        };
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
            writable,
            point_witnesses: BTreeMap::new(),
            range_witnesses: Vec::new(),
            retired_ranges: Vec::new(),
            replacement_ranges: Vec::new(),
            mounted_ranges: Vec::new(),
            staged_reference_blocks: Vec::new(),
            writes: BTreeMap::new(),
            snapshot_pin: pin_snapshot(&self.snapshot_pins, snapshot)?,
        })
    }

    pub(crate) fn stage_persistent_snapshot_pin(
        &self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
    ) -> io::Result<EntitySnapshot> {
        self.engine.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
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
        &self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        self.engine.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
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
            engine: &self.engine,
        })
        .extract_ranges(source_root, &encoded_ranges)?;
        if !staged_blocks.contains(&capsule_root) {
            staged_blocks.push(capsule_root);
        }
        let additional_blocks = staged_blocks
            .iter()
            .filter(|block_id| !transaction.staged_reference_blocks.contains(block_id))
            .count();
        if transaction.staged_reference_blocks.len() + additional_blocks
            > crate::transaction::MAX_REFERENCED_BLOCKS
        {
            return Err(invalid_input(
                "persistent entity range snapshot stages too many blocks",
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
        &self,
        transaction: &mut EntityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        self.engine.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        if !transaction.writable {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
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
            engine: &self.engine,
        })
        .get(transaction.snapshot.root, pin_state.pin_key.encoded())?
        .ok_or_else(|| invalid_data("persistent entity snapshot pin disappeared"))?;
        if snapshot.sequence >= pin_record.version
            || snapshot.sequence > self.engine.state().durable_sequence
        {
            return Err(invalid_data("persistent entity root pin is not monotonic"));
        }
        let capsule_root = snapshot.root.unwrap();
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
            if transaction.staged_reference_blocks.len()
                == crate::transaction::MAX_REFERENCED_BLOCKS
            {
                return Err(invalid_input(
                    "persistent entity restore stages too many blocks",
                ));
            }
            transaction.staged_reference_blocks.push(capsule_root);
        }
        Ok(snapshot)
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
        self.engine.require_usable_authority()?;
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
        self.engine.require_usable_authority()?;
        let pin_key = persistent_root_pin_key(tenant_id, owner_user_id, pin_id)?;
        let Some(record) = (Tree {
            engine: &self.engine,
        })
        .get(self.root, pin_key.encoded())?
        else {
            return Ok(None);
        };
        let Some(encoded_snapshot) = record.value else {
            return Ok(None);
        };
        let snapshot = decode_persistent_root_pin(&encoded_snapshot)?;
        if snapshot.sequence >= record.version
            || snapshot.sequence > self.engine.state().durable_sequence
        {
            return Err(invalid_data("persistent entity root pin is not monotonic"));
        }
        Tree {
            engine: &self.engine,
        }
        .load_root(snapshot.root.unwrap())?;
        self.begin_at_snapshot(tenant_id, owner_user_id, Vec::new(), snapshot, false)
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
        self.engine.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        ensure_scope(transaction, key)?;
        if !transaction.point_witnesses.contains_key(key.encoded())
            && transaction.point_witnesses.len() >= MAX_ENTITY_POINT_WITNESSES
        {
            return Err(invalid_input("entity point witness capacity is exhausted"));
        }
        let needs_witness = !transaction.point_witnesses.contains_key(key.encoded());
        let reservation_bytes = point_witness_reservation_bytes(key.encoded());
        if needs_witness {
            transaction
                .snapshot_pin
                .reserve_transaction_bytes(reservation_bytes)?;
        }
        let staged = transaction.writes.get(key.encoded()).cloned();
        let entity = match (Tree {
            engine: &self.engine,
        })
        .get(transaction.snapshot.root, key.encoded())
        {
            Ok(entity) => entity,
            Err(error) => {
                if needs_witness {
                    transaction
                        .snapshot_pin
                        .release_transaction_bytes(reservation_bytes);
                }
                return Err(error);
            }
        };
        transaction
            .point_witnesses
            .entry(key.0.clone())
            .or_insert(entity.as_ref().map(|value| value.version));
        Ok(staged.unwrap_or_else(|| entity.and_then(|value| value.value)))
    }

    pub fn scan(
        &self,
        transaction: &mut EntityTransaction,
        start: &EntityKey,
        end: &EntityKey,
        limit: usize,
    ) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.engine.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        ensure_scan_scope(transaction, start, end)?;
        if limit == 0 || limit > MAX_ENTITY_RANGE_ROWS {
            return Err(invalid_input("invalid or unbounded entity range"));
        }
        if transaction.range_witnesses.len() >= MAX_ENTITY_RANGE_WITNESSES {
            return Err(invalid_input("entity range witness capacity is exhausted"));
        }
        let reservation_bytes = range_witness_reservation_bytes(start.encoded(), end.encoded());
        transaction
            .snapshot_pin
            .reserve_transaction_bytes(reservation_bytes)?;
        let internal_limit = limit
            .checked_add(transaction.writes.len())
            .map(|value| value.min(MAX_INTERNAL_RANGE_ROWS))
            .ok_or_else(|| invalid_input("entity range limit overflow"))?;
        let scan_result = (Tree {
            engine: &self.engine,
        })
        .scan(
            transaction.snapshot.root,
            start.encoded(),
            end.encoded(),
            internal_limit,
        );
        let (rows, leaf_ids) = match scan_result {
            Ok(result) => result,
            Err(error) => {
                transaction
                    .snapshot_pin
                    .release_transaction_bytes(reservation_bytes);
                return Err(error);
            }
        };
        transaction.range_witnesses.push(RangeWitness {
            start: start.0.clone(),
            end: end.0.clone(),
            leaf_ids,
            reverse: false,
            scan_limit: internal_limit,
        });
        let mut visible = rows
            .into_iter()
            .filter_map(|(key, value)| value.value.map(|value| (key, value)))
            .collect::<BTreeMap<_, _>>();
        for (key, value) in transaction
            .writes
            .range(start.encoded().to_vec()..end.encoded().to_vec())
        {
            match value {
                Some(value) => {
                    visible.insert(key.clone(), value.clone());
                }
                None => {
                    visible.remove(key);
                }
            }
        }
        Ok(visible
            .into_iter()
            .take(limit)
            .map(|(key, value)| (EntityKey(key), value))
            .collect())
    }

    pub fn scan_reverse(
        &self,
        transaction: &mut EntityTransaction,
        start: &EntityKey,
        end: &EntityKey,
        limit: usize,
    ) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.engine.require_usable_authority()?;
        self.ensure_transaction_instance(transaction)?;
        ensure_scan_scope(transaction, start, end)?;
        if limit == 0 || limit > MAX_ENTITY_RANGE_ROWS {
            return Err(invalid_input("invalid or unbounded reverse entity range"));
        }
        if transaction.range_witnesses.len() >= MAX_ENTITY_RANGE_WITNESSES {
            return Err(invalid_input("entity range witness capacity is exhausted"));
        }
        let reservation_bytes = range_witness_reservation_bytes(start.encoded(), end.encoded());
        transaction
            .snapshot_pin
            .reserve_transaction_bytes(reservation_bytes)?;
        let internal_limit = limit
            .checked_add(transaction.writes.len())
            .map(|value| value.min(MAX_INTERNAL_RANGE_ROWS))
            .ok_or_else(|| invalid_input("entity range limit overflow"))?;
        let scan_result = (Tree {
            engine: &self.engine,
        })
        .scan_reverse(
            transaction.snapshot.root,
            start.encoded(),
            end.encoded(),
            internal_limit,
        );
        let (rows, leaf_ids) = match scan_result {
            Ok(result) => result,
            Err(error) => {
                transaction
                    .snapshot_pin
                    .release_transaction_bytes(reservation_bytes);
                return Err(error);
            }
        };
        transaction.range_witnesses.push(RangeWitness {
            start: start.0.clone(),
            end: end.0.clone(),
            leaf_ids,
            reverse: true,
            scan_limit: internal_limit,
        });
        let mut visible = rows
            .into_iter()
            .filter_map(|(key, value)| value.value.map(|value| (key, value)))
            .collect::<BTreeMap<_, _>>();
        for (key, value) in transaction
            .writes
            .range(start.encoded().to_vec()..end.encoded().to_vec())
        {
            match value {
                Some(value) => {
                    visible.insert(key.clone(), value.clone());
                }
                None => {
                    visible.remove(key);
                }
            }
        }
        Ok(visible
            .into_iter()
            .rev()
            .take(limit)
            .map(|(key, value)| (EntityKey(key), value))
            .collect())
    }

    pub fn consolidate_one_range_mount(
        &self,
        transaction: &mut EntityTransaction,
    ) -> io::Result<Option<EntityMountConsolidationProgress>> {
        self.engine.require_usable_authority()?;
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
            engine: &self.engine,
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
            && transaction.writes.len() == MAX_ENTITY_TRANSACTION_WRITES
        {
            return Err(invalid_input(
                "entity transaction exceeds 14336 distinct writes",
            ));
        }
        let previous_bytes = transaction.writes.get(&key.0).map_or(0, |previous| {
            write_reservation_bytes(&key.0, previous.as_deref())
        });
        let next_bytes = write_reservation_bytes(&key.0, Some(&value));
        if next_bytes > previous_bytes {
            transaction
                .snapshot_pin
                .reserve_transaction_bytes(next_bytes - previous_bytes)?;
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
            && transaction.writes.len() == MAX_ENTITY_TRANSACTION_WRITES
        {
            return Err(invalid_input(
                "entity transaction exceeds 14336 distinct writes",
            ));
        }
        let previous_bytes = transaction.writes.get(&key.0).map_or(0, |previous| {
            write_reservation_bytes(&key.0, previous.as_deref())
        });
        let next_bytes = write_reservation_bytes(&key.0, None);
        if next_bytes > previous_bytes {
            transaction
                .snapshot_pin
                .reserve_transaction_bytes(next_bytes - previous_bytes)?;
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
        self.engine.require_usable_authority()?;
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
        transaction
            .snapshot_pin
            .reserve_transaction_bytes(retired_range_reservation_bytes(
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
        let Some(root_record) = prepared.root_record() else {
            return Ok(self.engine.state().durable_sequence);
        };
        let result = self.engine.commit_references_with_authority_state(
            root_record,
            prepared.block_ids(),
            prepared.authority_state_root(),
        )?;
        self.apply_prepared_commit(prepared, result.sequence)?;
        Ok(result.sequence)
    }

    pub(crate) fn prepare_commit(
        &self,
        transaction: EntityTransaction,
    ) -> io::Result<PreparedEntityCommit> {
        self.ensure_transaction_instance(&transaction)?;
        if transaction.snapshot.sequence > self.engine.state().durable_sequence {
            return Err(invalid_data("entity snapshot is from the future"));
        }
        let tree = Tree {
            engine: &self.engine,
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
        for (key, witnessed) in &transaction.point_witnesses {
            let current = tree.get(self.root, key)?.map(|value| value.version);
            if &current != witnessed {
                return Err(conflict("entity point witness changed"));
            }
        }
        for key in transaction.writes.keys() {
            let snapshot = tree
                .get(transaction.snapshot.root, key)?
                .map(|value| value.version);
            let current = tree.get(self.root, key)?.map(|value| value.version);
            if snapshot != current {
                return Err(conflict("entity write witness changed"));
            }
        }
        for witness in &transaction.range_witnesses {
            let (_, current_leaves) = if witness.reverse {
                tree.scan_reverse(self.root, &witness.start, &witness.end, witness.scan_limit)?
            } else {
                tree.scan(self.root, &witness.start, &witness.end, witness.scan_limit)?
            };
            if current_leaves != witness.leaf_ids {
                return Err(conflict("entity range witness changed"));
            }
        }
        if transaction.writes.is_empty()
            && transaction.retired_ranges.is_empty()
            && transaction.mounted_ranges.is_empty()
        {
            return Ok(PreparedEntityCommit {
                expected_sequence: self.engine.next_sequence()?,
                next_root: self.root,
                written_block_ids: Vec::new(),
                root_record: None,
            });
        }
        if transaction.writes.len() > MAX_ENTITY_TRANSACTION_WRITES {
            return Err(invalid_input(
                "entity transaction exceeds 14336 distinct writes",
            ));
        }
        let sequence = self.engine.next_sequence()?;
        let mut written_block_ids = transaction.staged_reference_blocks.clone();
        let mut directory = tree.split_root(self.root)?;
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
            let (written_root, write_blocks) = tree.apply_batch(
                directory.base_root,
                transaction.writes.into_iter().collect(),
                sequence,
            )?;
            directory.base_root = Some(written_root);
            written_block_ids.extend(write_blocks);
        } else if !retirement_changed && transaction.mounted_ranges.is_empty() {
            return Ok(PreparedEntityCommit {
                expected_sequence: sequence,
                next_root: self.root,
                written_block_ids: Vec::new(),
                root_record: None,
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
        let next_root = if directory.mounts.is_empty() {
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
        self.root = prepared.next_root;
        Ok(())
    }

    pub(crate) fn engine(&self) -> &Engine {
        &self.engine
    }

    pub(crate) const fn transaction_is_writable(transaction: &EntityTransaction) -> bool {
        transaction.writable
    }

    pub(crate) fn engine_mut(&mut self) -> &mut Engine {
        &mut self.engine
    }

    pub fn snapshot(&self) -> EntitySnapshot {
        EntitySnapshot {
            sequence: self.engine.state().durable_sequence,
            root: self.root,
        }
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
            retained_transaction_bytes: pins.retained_transaction_bytes,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transaction::{encode_family_records, FamilyRecord, FamilyRecordKind};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, OpenRequest, Operation, Vfs};
    use std::collections::BTreeSet;

    fn key(value: u16) -> EntityKey {
        EntityKey::new(7, 11, "conversation", &value.to_be_bytes()).unwrap()
    }

    #[test]
    fn snapshot_pins_are_bounded_and_release_with_transaction_lifetimes() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transactions = Vec::with_capacity(MAX_PINNED_ENTITY_SNAPSHOTS);
        for _ in 0..MAX_PINNED_ENTITY_SNAPSHOTS {
            transactions.push(database.begin(7, 11).unwrap());
        }
        assert_eq!(
            database.snapshot_pin_metrics().unwrap(),
            EntitySnapshotPinMetrics {
                active_handles: MAX_PINNED_ENTITY_SNAPSHOTS as u32,
                distinct_snapshots: 1,
                oldest_sequence: Some(0),
                retained_transaction_bytes: 0,
            }
        );
        assert_eq!(
            database.begin(7, 11).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        transactions.pop();
        transactions.push(database.begin(7, 11).unwrap());
        drop(transactions);
        assert_eq!(
            database.snapshot_pin_metrics().unwrap(),
            EntitySnapshotPinMetrics::default()
        );
    }

    #[test]
    fn point_witness_capacity_reuses_existing_keys_and_rejects_before_page_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let admitted_key = key(1);
        let mut seed = database.begin(7, 11).unwrap();
        database
            .put(&mut seed, admitted_key.clone(), b"present".to_vec())
            .unwrap();
        database.commit(seed).unwrap();

        let mut transaction = database.begin(7, 11).unwrap();
        transaction
            .point_witnesses
            .insert(admitted_key.0.clone(), None);
        for index in 0..(MAX_ENTITY_POINT_WITNESSES - 1) {
            transaction
                .point_witnesses
                .insert((index as u64).to_be_bytes().to_vec(), None);
        }
        assert_eq!(
            transaction.point_witnesses.len(),
            MAX_ENTITY_POINT_WITNESSES
        );
        assert_eq!(
            database.get(&mut transaction, &admitted_key).unwrap(),
            Some(b"present".to_vec())
        );

        vfs.arm_fault(None).unwrap();
        assert_eq!(
            database.get(&mut transaction, &key(2)).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(vfs.trace().unwrap().is_empty());
    }

    #[test]
    fn aggregate_transaction_bytes_backpressure_before_io_and_release_on_drop() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database
            .put(&mut seed, key(1), b"present".to_vec())
            .unwrap();
        database.commit(seed).unwrap();

        let rejected_key = key(2);
        let next_read_bytes = point_witness_reservation_bytes(rejected_key.encoded());
        let mut holder = database.begin(7, 11).unwrap();
        holder
            .snapshot_pin
            .reserve_transaction_bytes(MAX_AGGREGATE_ENTITY_TRANSACTION_BYTES - next_read_bytes + 1)
            .unwrap();
        let mut reader = database.begin(7, 11).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            database.get(&mut reader, &rejected_key).unwrap_err().kind(),
            io::ErrorKind::OutOfMemory
        );
        assert!(vfs.trace().unwrap().is_empty());

        drop(holder);
        assert_eq!(database.get(&mut reader, &rejected_key).unwrap(), None);
        assert_eq!(
            database
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            next_read_bytes
        );
        drop(reader);
        assert_eq!(
            database
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
    }

    #[test]
    fn replacing_a_staged_write_releases_its_value_reservation() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let entity_key = key(1);
        database
            .put(
                &mut transaction,
                entity_key.clone(),
                vec![0; MAX_ENTITY_INLINE_VALUE_BYTES],
            )
            .unwrap();
        let with_value = database
            .snapshot_pin_metrics()
            .unwrap()
            .retained_transaction_bytes;
        database.delete(&mut transaction, entity_key).unwrap();
        assert_eq!(
            with_value
                - database
                    .snapshot_pin_metrics()
                    .unwrap()
                    .retained_transaction_bytes,
            MAX_ENTITY_INLINE_VALUE_BYTES
        );
    }

    #[test]
    fn range_witness_capacity_rejects_forward_and_reverse_scans_before_page_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database
            .put(&mut seed, key(1), b"present".to_vec())
            .unwrap();
        database.commit(seed).unwrap();

        let mut transaction = database.begin(7, 11).unwrap();
        transaction.range_witnesses = (0..MAX_ENTITY_RANGE_WITNESSES)
            .map(|_| RangeWitness {
                start: Vec::new(),
                end: vec![u8::MAX],
                leaf_ids: Vec::new(),
                reverse: false,
                scan_limit: 1,
            })
            .collect();
        let start = key(0);
        let end = key(u16::MAX);

        vfs.arm_fault(None).unwrap();
        assert_eq!(
            database
                .scan(&mut transaction, &start, &end, 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(vfs.trace().unwrap().is_empty());

        vfs.arm_fault(None).unwrap();
        assert_eq!(
            database
                .scan_reverse(&mut transaction, &start, &end, 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(vfs.trace().unwrap().is_empty());
    }

    #[test]
    fn reachability_visits_each_structurally_bounded_page_without_a_global_set() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..256_u16 {
            database
                .put(&mut seed, key(index), vec![index as u8; 8 * 1024])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut visited = BTreeSet::new();
        let metrics = visit_entity_page_graph(
            database.snapshot().root,
            |block_id| database.engine.read_block(block_id),
            |block_id, _| {
                assert!(visited.insert(block_id));
                Ok(())
            },
        )
        .unwrap();
        assert!(metrics.page_count > 256);
        assert_eq!(metrics.page_count as usize, visited.len());
        assert!(metrics.payload_bytes > 2 * 1024 * 1024);
        assert!(metrics.maximum_frontier as usize <= MAX_ENTITY_REACHABILITY_FRONTIER);
    }

    #[test]
    fn persistent_snapshot_pin_survives_reopen_and_extends_reachability() {
        let directory = tempfile::tempdir().unwrap();
        let pinned_root;
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin(7, 11).unwrap();
            database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
            database.commit(seed).unwrap();
            pinned_root = database.snapshot().root.unwrap();

            let mut replace = database.begin(7, 11).unwrap();
            assert_eq!(
                database
                    .stage_persistent_snapshot_pin(&mut replace, b"conversation:one")
                    .unwrap()
                    .root,
                Some(pinned_root)
            );
            database
                .put(&mut replace, key(1), b"current".to_vec())
                .unwrap();
            database.commit(replace).unwrap();

            let mut visited = BTreeSet::new();
            visit_entity_page_graph(
                database.snapshot().root,
                |block_id| database.engine.read_block(block_id),
                |block_id, _| {
                    visited.insert(block_id);
                    Ok(())
                },
            )
            .unwrap();
            assert!(visited.contains(&pinned_root));
        }

        let mut database = EntityDatabase::open(directory.path()).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &key(1)).unwrap(),
            Some(b"current".to_vec())
        );
        let mut pinned = database
            .begin_persistent_snapshot(7, 11, b"conversation:one")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut pinned, &key(1)).unwrap(),
            Some(b"old".to_vec())
        );
        assert_eq!(
            database
                .put(&mut pinned, key(2), b"forbidden".to_vec())
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        drop(pinned);

        let mut remove = database.begin(7, 11).unwrap();
        assert!(database
            .remove_persistent_snapshot_pin(&mut remove, b"conversation:one")
            .unwrap());
        database.commit(remove).unwrap();
        assert!(database
            .begin_persistent_snapshot(7, 11, b"conversation:one")
            .unwrap()
            .is_none());
    }

    #[test]
    fn persistent_snapshot_pin_catalog_scales_past_legacy_bound_and_remains_immutable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
        database.commit(seed).unwrap();

        let mut pins = database.begin(7, 11).unwrap();
        for index in 0..(MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS * 2) {
            database
                .stage_persistent_snapshot_pin(&mut pins, format!("pin-{index}").as_bytes())
                .unwrap();
        }
        database.commit(pins).unwrap();
        let reachability = visit_entity_page_graph(
            database.root,
            |block_id| database.engine.read_block(block_id),
            |_, _| Ok(()),
        )
        .unwrap();
        assert!(reachability.page_count > MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS as u64);

        let mut replacement = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .stage_persistent_snapshot_pin(&mut replacement, b"pin-0")
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );

        let count_key = persistent_root_pin_count_key().unwrap();
        let mut force_capacity = database
            .begin_with_additional_scope_prefixes(7, 11, vec![count_key.encoded().to_vec()])
            .unwrap();
        database
            .put(
                &mut force_capacity,
                count_key,
                encode_persistent_root_pin_count(MAX_PERSISTENT_ENTITY_ROOT_PINS).unwrap(),
            )
            .unwrap();
        database.commit(force_capacity).unwrap();
        let mut overflow = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .stage_persistent_snapshot_pin(&mut overflow, b"overflow")
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn persistent_pin_catalog_upgrades_legacy_records_and_uses_bounded_point_reads() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
        database.commit(seed).unwrap();

        let legacy_pin_key = persistent_root_pin_key(7, 11, b"legacy").unwrap();
        let legacy_snapshot = database.snapshot();
        let mut legacy = database
            .begin_with_additional_scope_prefixes(7, 11, vec![legacy_pin_key.encoded().to_vec()])
            .unwrap();
        database
            .put(
                &mut legacy,
                legacy_pin_key,
                encode_persistent_root_pin(legacy_snapshot).unwrap(),
            )
            .unwrap();
        database.commit(legacy).unwrap();

        let mut upgrade = database.begin(7, 11).unwrap();
        database
            .stage_persistent_snapshot_pin(&mut upgrade, b"catalogued")
            .unwrap();
        database.commit(upgrade).unwrap();
        let count_key = persistent_root_pin_count_key().unwrap();
        let count_record = (Tree {
            engine: &database.engine,
        })
        .get(database.root, count_key.encoded())
        .unwrap()
        .unwrap()
        .value
        .unwrap();
        assert_eq!(decode_persistent_root_pin_count(&count_record).unwrap(), 2);

        vfs.arm_fault(None).unwrap();
        let mut next = database.begin(7, 11).unwrap();
        database
            .stage_persistent_snapshot_pin(&mut next, b"point-only")
            .unwrap();
        let reads = vfs
            .trace()
            .unwrap()
            .into_iter()
            .filter(|operation| operation == &Operation::Read)
            .count();
        assert!(
            reads <= 16,
            "catalogued pin admission performed {reads} reads"
        );
    }

    fn prepared_persistent_pin_database(vfs: Arc<DeterministicVfs>) -> EntityDatabase {
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
        database.commit(seed).unwrap();
        database
    }

    fn commit_persistent_pin(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        database.stage_persistent_snapshot_pin(&mut transaction, b"fault-pin")?;
        database.put(&mut transaction, key(1), b"new".to_vec())?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_persistent_pin_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        match database.snapshot().sequence {
            1 => {
                assert_eq!(
                    database.get(&mut current, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
                assert!(database
                    .begin_persistent_snapshot(7, 11, b"fault-pin")
                    .unwrap()
                    .is_none());
                assert!((Tree {
                    engine: &database.engine,
                })
                .get(
                    database.root,
                    persistent_root_pin_count_key().unwrap().encoded(),
                )
                .unwrap()
                .is_none());
            }
            2 => {
                assert_eq!(
                    database.get(&mut current, &key(1)).unwrap(),
                    Some(b"new".to_vec())
                );
                let mut pinned = database
                    .begin_persistent_snapshot(7, 11, b"fault-pin")
                    .unwrap()
                    .unwrap();
                assert_eq!(
                    database.get(&mut pinned, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
                let count = (Tree {
                    engine: &database.engine,
                })
                .get(
                    database.root,
                    persistent_root_pin_count_key().unwrap().encoded(),
                )
                .unwrap()
                .unwrap()
                .value
                .unwrap();
                assert_eq!(decode_persistent_root_pin_count(&count).unwrap(), 1);
            }
            sequence => panic!("persistent pin recovered a non-prefix sequence {sequence}"),
        }
    }

    #[test]
    fn every_persistent_snapshot_pin_commit_fault_recovers_an_atomic_prefix() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_persistent_pin_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        commit_persistent_pin(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_persistent_pin(&mut database);
            drop(database);
            assert_recovered_persistent_pin_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_persistent_pin(&mut database);
            drop(database);
            assert_recovered_persistent_pin_prefix(vfs);
        }
    }

    #[test]
    fn reachability_rejects_child_level_and_key_range_transplants() {
        let first_leaf = Page::Leaf(vec![(
            b"a".to_vec(),
            EntityValue {
                version: 1,
                value: Some(b"first".to_vec()),
            },
        )])
        .encode()
        .unwrap();
        let transplanted_leaf = Page::Leaf(vec![(
            b"c".to_vec(),
            EntityValue {
                version: 1,
                value: Some(b"second".to_vec()),
            },
        )])
        .encode()
        .unwrap();
        let first_id = BlockId::for_payload(&first_leaf);
        let transplanted_id = BlockId::for_payload(&transplanted_leaf);
        let root = Page::Internal {
            level: 1,
            children: vec![
                Child {
                    lower_bound: b"a".to_vec(),
                    block_id: first_id,
                },
                Child {
                    lower_bound: b"b".to_vec(),
                    block_id: transplanted_id,
                },
            ],
        }
        .encode()
        .unwrap();
        let root_id = BlockId::for_payload(&root);
        let blocks = BTreeMap::from([
            (root_id, root),
            (first_id, first_leaf),
            (transplanted_id, transplanted_leaf),
        ]);
        assert_eq!(
            visit_entity_page_graph(
                Some(root_id),
                |block_id| {
                    blocks
                        .get(&block_id)
                        .cloned()
                        .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
                },
                |_, _| Ok(()),
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn old_and_current_snapshots_remain_distinct_and_readable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut old = database.begin(7, 11).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        database.put(&mut write, key(1), b"new".to_vec()).unwrap();
        database.commit(write).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.snapshot_pin_metrics().unwrap(),
            EntitySnapshotPinMetrics {
                active_handles: 2,
                distinct_snapshots: 2,
                oldest_sequence: Some(0),
                retained_transaction_bytes: 0,
            }
        );
        assert_eq!(database.get(&mut old, &key(1)).unwrap(), None);
        assert_eq!(
            database.get(&mut current, &key(1)).unwrap(),
            Some(b"new".to_vec())
        );
    }

    #[test]
    fn transaction_cannot_cross_a_database_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        drop(database);
        let reopened = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(
            reopened.get(&mut transaction, &key(1)).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn cow_snapshot_survives_page_splits_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let old_snapshot;
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            let mut first = database.begin(7, 11).unwrap();
            for value in 0..200_u16 {
                database
                    .put(&mut first, key(value), vec![value as u8; 100])
                    .unwrap();
            }
            database.commit(first).unwrap();
            old_snapshot = database.snapshot();
            let mut second = database.begin(7, 11).unwrap();
            database.put(&mut second, key(50), b"new".to_vec()).unwrap();
            database.commit(second).unwrap();
            let old = Tree {
                engine: &database.engine,
            }
            .get(old_snapshot.root, key(50).encoded())
            .unwrap()
            .unwrap();
            assert_eq!(old.value.unwrap(), vec![50; 100]);
        }
        let database = EntityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut read, &key(50)).unwrap().unwrap(), b"new");
    }

    #[test]
    fn batch_cow_writes_each_live_page_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let before = database.engine.block_write_metrics();
        let mut transaction = database.begin(7, 11).unwrap();
        for value in 0..200_u16 {
            database
                .put(&mut transaction, key(value), vec![value as u8; 100])
                .unwrap();
        }
        database.commit(transaction).unwrap();
        let after = database.engine.block_write_metrics();
        assert_eq!(after.blocks_written - before.blocks_written, 3);
        assert!(after.bytes_written - before.bytes_written <= 32 * 1024);
        let first_commit = database.engine.committed_transactions().last().unwrap();
        assert_eq!(first_commit.envelope.block_ids.len(), 3);
        assert!(first_commit
            .envelope
            .block_ids
            .contains(&database.root.unwrap()));

        let before_update = database.engine.block_write_metrics();
        let mut update = database.begin(7, 11).unwrap();
        database
            .put(&mut update, key(100), b"updated".to_vec())
            .unwrap();
        database.commit(update).unwrap();
        let after_update = database.engine.block_write_metrics();
        assert_eq!(
            after_update.blocks_written - before_update.blocks_written,
            2
        );
        let update_commit = database.engine.committed_transactions().last().unwrap();
        assert_eq!(update_commit.envelope.block_ids.len(), 2);
        assert!(update_commit
            .envelope
            .block_ids
            .contains(&database.root.unwrap()));
    }

    #[test]
    fn range_retirement_drops_thousands_of_keys_with_boundary_only_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "range_retirement", &index.to_be_bytes()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..10_000_u32 {
            database
                .put(&mut seed, range_key(index), vec![index as u8; 32])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut old_snapshot = database.begin(7, 11).unwrap();

        vfs.arm_fault(None).unwrap();
        let mut retirement = database.begin(7, 11).unwrap();
        database
            .stage_persistent_snapshot_pin(&mut retirement, b"retired-range")
            .unwrap();
        database
            .retire_range(&mut retirement, &range_key(2_000), &range_key(8_000))
            .unwrap();
        database.commit(retirement).unwrap();
        let trace = vfs.trace().unwrap();
        let reads = trace
            .iter()
            .filter(|operation| operation == &&Operation::Read)
            .count();
        let writes = trace
            .iter()
            .filter(|operation| operation == &&Operation::Write)
            .count();
        assert!(reads <= 48, "range retirement performed {reads} reads");
        assert!(writes <= 32, "range retirement performed {writes} writes");

        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(1_999)).unwrap(),
            Some(vec![207; 32])
        );
        assert_eq!(database.get(&mut current, &range_key(2_000)).unwrap(), None);
        assert_eq!(database.get(&mut current, &range_key(7_999)).unwrap(), None);
        assert_eq!(
            database.get(&mut current, &range_key(8_000)).unwrap(),
            Some(vec![64; 32])
        );
        assert_eq!(
            database.get(&mut old_snapshot, &range_key(4_000)).unwrap(),
            Some(vec![160; 32])
        );
        let mut persistent = database
            .begin_persistent_snapshot(7, 11, b"retired-range")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut persistent, &range_key(4_000)).unwrap(),
            Some(vec![160; 32])
        );
    }

    #[test]
    fn range_retirement_is_root_witnessed_bounded_and_rejects_overlapping_writes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let range_key =
            |index: u16| EntityKey::new(7, 11, "range_retirement", &index.to_be_bytes()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..100_u16 {
            database
                .put(&mut seed, range_key(index), vec![index as u8])
                .unwrap();
        }
        database.commit(seed).unwrap();

        let mut stale = database.begin(7, 11).unwrap();
        database
            .retire_range(&mut stale, &range_key(10), &range_key(20))
            .unwrap();
        assert_eq!(
            database
                .put(&mut stale, range_key(15), b"forbidden".to_vec())
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            database
                .retire_range(&mut stale, &range_key(15), &range_key(25))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        for index in 0..MAX_ENTITY_RETIRED_RANGES - 1 {
            let start = 100 + index as u16 * 2;
            database
                .retire_range(&mut stale, &range_key(start), &range_key(start + 1))
                .unwrap();
        }
        assert_eq!(
            database
                .retire_range(&mut stale, &range_key(500), &range_key(501))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );

        let mut concurrent = database.begin(7, 11).unwrap();
        database
            .put(&mut concurrent, range_key(99), b"changed".to_vec())
            .unwrap();
        database.commit(concurrent).unwrap();
        assert_eq!(
            database.commit(stale).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn persistent_range_capsule_excludes_unrelated_database_pages_and_reopens() {
        let directory = tempfile::tempdir().unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "range_capsule", &index.to_be_bytes()).unwrap();
        let capsule_root;
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin(7, 11).unwrap();
            for index in 0..10_000_u32 {
                database
                    .put(&mut seed, range_key(index), vec![index as u8; 32])
                    .unwrap();
            }
            database.commit(seed).unwrap();

            let ranges = [
                (range_key(100), range_key(200)),
                (range_key(9_000), range_key(9_100)),
            ];
            let mut snapshot = database.begin(7, 11).unwrap();
            capsule_root = database
                .stage_persistent_range_snapshot_pin(&mut snapshot, b"capsule", &ranges)
                .unwrap()
                .root
                .unwrap();
            database.commit(snapshot).unwrap();

            let metrics = visit_entity_page_graph(
                Some(capsule_root),
                |block_id| database.engine.read_block(block_id),
                |_, _| Ok(()),
            )
            .unwrap();
            assert!(
                metrics.page_count <= 16,
                "range capsule retained {} pages",
                metrics.page_count
            );
        }

        let database = EntityDatabase::open(directory.path()).unwrap();
        let mut capsule = database
            .begin_persistent_snapshot(7, 11, b"capsule")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut capsule, &range_key(150)).unwrap(),
            Some(vec![150; 32])
        );
        assert_eq!(
            database.get(&mut capsule, &range_key(9_050)).unwrap(),
            Some(vec![90; 32])
        );
        assert_eq!(database.get(&mut capsule, &range_key(99)).unwrap(), None);
        assert_eq!(database.get(&mut capsule, &range_key(5_000)).unwrap(), None);
        assert_eq!(database.get(&mut capsule, &range_key(9_100)).unwrap(), None);
    }

    #[test]
    fn range_capsule_restore_mounts_in_constant_writes_and_supports_repeated_retirement() {
        let directory = tempfile::tempdir().unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "range_restore", &index.to_be_bytes()).unwrap();
        let ranges = [(range_key(2_000), range_key(8_000))];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..10_000_u32 {
            database
                .put(&mut seed, range_key(index), vec![index as u8; 32])
                .unwrap();
        }
        database.commit(seed).unwrap();

        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"first", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();

        let before_restore = database.engine.block_write_metrics();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"first", &ranges)
            .unwrap();
        database.commit(restore).unwrap();
        let after_restore = database.engine.block_write_metrics();
        assert_eq!(
            after_restore.blocks_written - before_restore.blocks_written,
            1,
            "restoring a capsule must publish only its bounded root directory"
        );

        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(5_000)).unwrap(),
            Some(vec![136; 32])
        );
        assert_eq!(
            database.get(&mut current, &range_key(1_999)).unwrap(),
            Some(vec![207; 32])
        );
        let rows = database
            .scan(&mut current, &range_key(1_998), &range_key(2_003), 5)
            .unwrap();
        assert_eq!(rows.len(), 5);
        assert_eq!(rows[2].0, range_key(2_000));
        drop(current);

        let mut override_value = database.begin(7, 11).unwrap();
        database
            .put(&mut override_value, range_key(5_000), b"newer".to_vec())
            .unwrap();
        database.commit(override_value).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(5_000)).unwrap(),
            Some(b"newer".to_vec())
        );
        drop(current);

        let mut retire_again = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire_again, b"second", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire_again, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire_again).unwrap();
        drop(database);

        let database = EntityDatabase::open(directory.path()).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut current, &range_key(5_000)).unwrap(), None);
        let mut second = database
            .begin_persistent_snapshot(7, 11, b"second")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut second, &range_key(5_000)).unwrap(),
            Some(b"newer".to_vec())
        );
    }

    #[test]
    fn range_mount_consolidation_is_bounded_and_eventually_restores_a_direct_root() {
        let directory = tempfile::tempdir().unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "mount_merge", &index.to_be_bytes()).unwrap();
        let ranges = [(range_key(500), range_key(4_500))];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..5_000_u32 {
            database
                .put(&mut seed, range_key(index), vec![index as u8; 32])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"merge", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"merge", &ranges)
            .unwrap();
        database.commit(restore).unwrap();

        let mut rounds = 0;
        loop {
            let mut consolidation = database.begin(7, 11).unwrap();
            let Some(progress) = database
                .consolidate_one_range_mount(&mut consolidation)
                .unwrap()
            else {
                break;
            };
            assert!(progress.rows_materialized as usize <= MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS);
            assert!(progress.materialized_bytes as usize <= MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES);
            database.commit(consolidation).unwrap();
            rounds += 1;
            assert!(rounds <= 8, "mount consolidation did not converge");
            if progress.mount_completed {
                break;
            }
        }
        assert_eq!(rounds, 5);
        assert!(matches!(
            (Tree {
                engine: &database.engine,
            })
            .load_root(database.root.unwrap())
            .unwrap(),
            RootNode::Page(_)
        ));
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(500)).unwrap(),
            Some(vec![244; 32])
        );
        assert_eq!(
            database.get(&mut current, &range_key(4_499)).unwrap(),
            Some(vec![147; 32])
        );
    }

    #[test]
    fn range_mount_consolidation_enforces_its_byte_budget_before_row_budget() {
        let directory = tempfile::tempdir().unwrap();
        let range_key = |index: u32| {
            let mut raw_key = vec![b'k'; 2_996];
            raw_key.extend_from_slice(&index.to_be_bytes());
            EntityKey::new(7, 11, "mount_bytes", &raw_key).unwrap()
        };
        let ranges = [(range_key(0), range_key(800))];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..800_u32 {
            database
                .put(
                    &mut seed,
                    range_key(index),
                    vec![index as u8; MAX_ENTITY_INLINE_VALUE_BYTES],
                )
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"byte-budget", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"byte-budget", &ranges)
            .unwrap();
        database.commit(restore).unwrap();

        let mut consolidation = database.begin(7, 11).unwrap();
        let progress = database
            .consolidate_one_range_mount(&mut consolidation)
            .unwrap()
            .unwrap();
        assert!(!progress.mount_completed);
        assert!(progress.rows_materialized < 800);
        assert!(progress.rows_materialized > 0);
        assert!(progress.materialized_bytes as usize <= MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES);
        database.commit(consolidation).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(0)).unwrap(),
            Some(vec![0; MAX_ENTITY_INLINE_VALUE_BYTES])
        );
        assert_eq!(
            database.get(&mut current, &range_key(799)).unwrap(),
            Some(vec![31; MAX_ENTITY_INLINE_VALUE_BYTES])
        );
    }

    fn prepared_range_restore_database(vfs: Arc<DeterministicVfs>) -> EntityDatabase {
        let mut database = prepared_persistent_pin_database(vfs);
        commit_persistent_range_retirement(&mut database).unwrap();
        database
    }

    fn commit_persistent_range_restore(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        database.stage_persistent_range_snapshot_restore(
            &mut transaction,
            b"retirement-fault",
            &[(key(1), key(2))],
        )?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_range_restore_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        match database.snapshot().sequence {
            2 => assert_eq!(database.get(&mut current, &key(1)).unwrap(), None),
            3 => assert_eq!(
                database.get(&mut current, &key(1)).unwrap(),
                Some(b"old".to_vec())
            ),
            sequence => panic!("range restore recovered a non-prefix sequence {sequence}"),
        }
    }

    #[test]
    fn every_range_restore_commit_fault_recovers_an_atomic_mounted_prefix() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_range_restore_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        commit_persistent_range_restore(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_persistent_range_restore(&mut database);
            drop(database);
            assert_recovered_range_restore_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_persistent_range_restore(&mut database);
            drop(database);
            assert_recovered_range_restore_prefix(vfs);
        }
    }

    fn commit_range_mount_consolidation(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        let progress = database
            .consolidate_one_range_mount(&mut transaction)?
            .ok_or_else(|| invalid_data("expected one mounted range"))?;
        if !progress.mount_completed || progress.rows_materialized != 1 {
            return Err(invalid_data("unexpected mount consolidation progress"));
        }
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_mount_consolidation_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        assert!(matches!(database.snapshot().sequence, 3 | 4));
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &key(1)).unwrap(),
            Some(b"old".to_vec())
        );
        match database.snapshot().sequence {
            3 => assert!(matches!(
                (Tree {
                    engine: &database.engine,
                })
                .load_root(database.root.unwrap())
                .unwrap(),
                RootNode::Directory(_)
            )),
            4 => assert!(matches!(
                (Tree {
                    engine: &database.engine,
                })
                .load_root(database.root.unwrap())
                .unwrap(),
                RootNode::Page(_)
            )),
            _ => unreachable!(),
        }
    }

    #[test]
    fn every_mount_consolidation_commit_fault_recovers_one_equivalent_root() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_range_restore_database(baseline_vfs.clone());
        commit_persistent_range_restore(&mut baseline).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        commit_range_mount_consolidation(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            commit_persistent_range_restore(&mut database).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_range_mount_consolidation(&mut database);
            drop(database);
            assert_recovered_mount_consolidation_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            commit_persistent_range_restore(&mut database).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_range_mount_consolidation(&mut database);
            drop(database);
            assert_recovered_mount_consolidation_prefix(vfs);
        }
    }

    fn commit_persistent_range_retirement(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        database.stage_persistent_range_snapshot_pin(
            &mut transaction,
            b"retirement-fault",
            &[(key(1), key(2))],
        )?;
        database.retire_range(&mut transaction, &key(1), &key(2))?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_range_retirement_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        match database.snapshot().sequence {
            1 => {
                assert_eq!(
                    database.get(&mut current, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
                assert!(database
                    .begin_persistent_snapshot(7, 11, b"retirement-fault")
                    .unwrap()
                    .is_none());
            }
            2 => {
                assert_eq!(database.get(&mut current, &key(1)).unwrap(), None);
                let mut pinned = database
                    .begin_persistent_snapshot(7, 11, b"retirement-fault")
                    .unwrap()
                    .unwrap();
                assert_eq!(
                    database.get(&mut pinned, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
            }
            sequence => panic!("range retirement recovered a non-prefix sequence {sequence}"),
        }
    }

    #[test]
    fn every_range_retirement_commit_fault_recovers_an_atomic_pinned_prefix() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_persistent_pin_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        commit_persistent_range_retirement(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_persistent_range_retirement(&mut database);
            drop(database);
            assert_recovered_range_retirement_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_persistent_range_retirement(&mut database);
            drop(database);
            assert_recovered_range_retirement_prefix(vfs);
        }
    }

    #[test]
    fn transaction_write_budget_admits_secondary_indexes_and_rejects_the_next_key() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let indexed_key =
            |index: u32| EntityKey::new(7, 11, "event_indexes", &index.to_be_bytes()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..MAX_ENTITY_TRANSACTION_WRITES as u32 {
            database
                .put(&mut transaction, indexed_key(index), vec![1])
                .unwrap();
        }
        assert_eq!(
            database
                .put(
                    &mut transaction,
                    indexed_key(MAX_ENTITY_TRANSACTION_WRITES as u32),
                    vec![1],
                )
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        database.commit(transaction).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .get(
                    &mut read,
                    &indexed_key(MAX_ENTITY_TRANSACTION_WRITES as u32 - 1),
                )
                .unwrap(),
            Some(vec![1])
        );
    }

    #[test]
    fn point_witness_prevents_lost_update() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut left = database.begin(7, 11).unwrap();
        let mut right = database.begin(7, 11).unwrap();
        database.put(&mut left, key(1), b"left".to_vec()).unwrap();
        database.put(&mut right, key(1), b"right".to_vec()).unwrap();
        database.commit(left).unwrap();
        assert_eq!(
            database.commit(right).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn leaf_range_witness_prevents_phantom() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"one".to_vec()).unwrap();
        database.put(&mut seed, key(9), b"nine".to_vec()).unwrap();
        database.commit(seed).unwrap();

        let mut scanner = database.begin(7, 11).unwrap();
        assert_eq!(
            database.scan(&mut scanner, &key(2), &key(8), 10).unwrap(),
            vec![]
        );
        let mut writer = database.begin(7, 11).unwrap();
        database
            .put(&mut writer, key(5), b"phantom".to_vec())
            .unwrap();
        database.commit(writer).unwrap();
        database
            .put(&mut scanner, key(3), b"dependent".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(scanner).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn variable_length_keys_scan_in_raw_byte_order_and_witness_phantoms() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let variable_key = |raw: &[u8]| EntityKey::new(7, 11, "records", raw).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for raw in [b"a".as_slice(), b"ab", b"b", "鲸".as_bytes()] {
            database
                .put(&mut seed, variable_key(raw), raw.to_vec())
                .unwrap();
        }
        database.commit(seed).unwrap();

        let (prefix_start, prefix_end) = EntityKey::prefix_range(7, 11, "records", b"a").unwrap();
        let mut scan = database.begin(7, 11).unwrap();
        let rows = database
            .scan(&mut scan, &prefix_start, &prefix_end, 10)
            .unwrap();
        assert_eq!(
            rows.iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"a".as_slice(), b"ab"]
        );
        let mut reverse_scan = database.begin(7, 11).unwrap();
        let reverse_rows = database
            .scan_reverse(&mut reverse_scan, &prefix_start, &prefix_end, 10)
            .unwrap();
        assert_eq!(
            reverse_rows
                .iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"ab".as_slice(), b"a"]
        );

        let mut writer = database.begin(7, 11).unwrap();
        database
            .put(&mut writer, variable_key(b"aa"), b"phantom".to_vec())
            .unwrap();
        database.commit(writer).unwrap();
        database
            .put(&mut scan, variable_key(b"dependent"), b"value".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(scan).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        database
            .put(
                &mut reverse_scan,
                variable_key(b"reverse-dependent"),
                b"value".to_vec(),
            )
            .unwrap();
        assert_eq!(
            database.commit(reverse_scan).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );

        let (binary_start, binary_end) =
            EntityKey::prefix_range(7, 11, "records", &[0xff, 0xff]).unwrap();
        assert!(binary_start < binary_end);
        assert_eq!(&binary_start.key_bytes()[..2], &[0xff, 0xff]);
        assert!(EntityKey::new(7, 11, "records", &vec![0; MAX_ENTITY_KEY_BYTES]).is_ok());
        assert!(EntityKey::new(7, 11, "records", &vec![0; MAX_ENTITY_KEY_BYTES + 1]).is_err());
    }

    #[test]
    fn transaction_reads_its_own_point_and_range_writes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let variable_key = |raw: &[u8]| EntityKey::new(7, 11, "records", raw).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for raw in [b"a".as_slice(), b"b", b"c"] {
            database
                .put(&mut seed, variable_key(raw), raw.to_vec())
                .unwrap();
        }
        database.commit(seed).unwrap();

        let mut transaction = database.begin(7, 11).unwrap();
        database
            .put(&mut transaction, variable_key(b"a"), b"new-a".to_vec())
            .unwrap();
        database
            .delete(&mut transaction, variable_key(b"b"))
            .unwrap();
        database
            .put(&mut transaction, variable_key(b"aa"), b"new-aa".to_vec())
            .unwrap();
        assert_eq!(
            database.get(&mut transaction, &variable_key(b"a")).unwrap(),
            Some(b"new-a".to_vec())
        );
        assert_eq!(
            database.get(&mut transaction, &variable_key(b"b")).unwrap(),
            None
        );
        let (start, end) = EntityKey::prefix_range(7, 11, "records", b"").unwrap();
        let rows = database.scan(&mut transaction, &start, &end, 2).unwrap();
        assert_eq!(
            rows.iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"a".as_slice(), b"aa"]
        );
        database.commit(transaction).unwrap();

        let mut delete_first = database.begin(7, 11).unwrap();
        database
            .delete(&mut delete_first, variable_key(b"a"))
            .unwrap();
        let rows = database.scan(&mut delete_first, &start, &end, 2).unwrap();
        assert_eq!(
            rows.iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"aa".as_slice(), b"c"]
        );
    }

    #[test]
    fn transaction_owner_scope_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let owner_a = EntityKey::new(1, 10, "settings", b"theme").unwrap();
        let owner_b = EntityKey::new(1, 20, "settings", b"theme").unwrap();
        let mut write = database.begin(1, 10).unwrap();
        database
            .put(&mut write, owner_a.clone(), b"dark".to_vec())
            .unwrap();
        assert_eq!(
            database
                .put(&mut write, owner_b.clone(), b"light".to_vec())
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        database.commit(write).unwrap();
        let mut read = database.begin(1, 10).unwrap();
        assert_eq!(database.get(&mut read, &owner_a).unwrap().unwrap(), b"dark");
        assert_eq!(
            database.get(&mut read, &owner_b).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn additional_scope_allows_its_prefix_scan_without_authorizing_adjacent_namespace() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let allowed = EntityKey::new(7, 99, "global_a", b"one").unwrap();
        let adjacent = EntityKey::new(7, 99, "global_b", b"secret").unwrap();
        let mut seed = database.begin(7, 99).unwrap();
        database
            .put(&mut seed, allowed.clone(), b"visible".to_vec())
            .unwrap();
        database
            .put(&mut seed, adjacent.clone(), b"hidden".to_vec())
            .unwrap();
        database.commit(seed).unwrap();

        let prefix = EntityKey::new(7, 99, "global_a", b"").unwrap();
        let mut scoped = database
            .begin_with_additional_scope_prefixes(7, 11, vec![prefix.encoded().to_vec()])
            .unwrap();
        let (start, end) = EntityKey::prefix_range(7, 99, "global_a", b"").unwrap();
        let rows = database.scan(&mut scoped, &start, &end, 10).unwrap();
        assert_eq!(rows, vec![(allowed, b"visible".to_vec())]);
        assert_eq!(
            database.get(&mut scoped, &adjacent).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn multiplexed_transaction_recovers_entity_root_with_other_families() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            let mut write = database.begin(7, 11).unwrap();
            database
                .put(&mut write, key(1), b"durable".to_vec())
                .unwrap();
            database.commit(write).unwrap();

            let sequence = database.engine.next_sequence().unwrap();
            let root = database.root.unwrap();
            let entity_record = encode_root(sequence, Some(root));
            let inline = encode_family_records(&[
                FamilyRecord {
                    kind: FamilyRecordKind::EntityRoot,
                    payload: &entity_record,
                },
                FamilyRecord {
                    kind: FamilyRecordKind::CommandReceipt,
                    payload: b"receipt",
                },
            ])
            .unwrap();
            database
                .engine
                .commit_references_with_authority_state(&inline, &[root], Some(root))
                .unwrap();
        }
        let database = EntityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &key(1)).unwrap().unwrap(),
            b"durable"
        );
        assert_eq!(database.snapshot().sequence, 2);
    }

    #[test]
    fn current_entity_root_reopens_without_reading_superseded_history_segments() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let first_history;
        {
            let mut database =
                EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                    .unwrap();
            let mut first = database.begin(7, 11).unwrap();
            database.put(&mut first, key(1), b"first".to_vec()).unwrap();
            database.commit(first).unwrap();
            database.engine.checkpoint().unwrap();
            first_history = database.engine.history_segment_block_ids()[0];

            let mut second = database.begin(7, 11).unwrap();
            database
                .put(&mut second, key(2), b"second".to_vec())
                .unwrap();
            database.commit(second).unwrap();
            database.engine.checkpoint().unwrap();
            assert_eq!(database.engine.history_segment_block_ids().len(), 2);
        }

        let hexadecimal = first_history.to_hex();
        let path = std::path::Path::new("/data")
            .join("blocks")
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"));
        let mut file = vfs
            .open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(16, b"X").unwrap();
        file.sync_all().unwrap();

        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &key(1)).unwrap(),
            Some(b"first".to_vec())
        );
        assert_eq!(
            database.get(&mut read, &key(2)).unwrap(),
            Some(b"second".to_vec())
        );
        assert_eq!(
            database.engine.transaction_at(1).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn open_reads_only_the_root_and_descendants_fail_lazily() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let child_id;
        let child_key;
        {
            let mut database =
                EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                    .unwrap();
            let mut seed = database.begin(7, 11).unwrap();
            for index in 0..64_u16 {
                database
                    .put(&mut seed, key(index), vec![index as u8; 8 * 1024])
                    .unwrap();
            }
            database.commit(seed).unwrap();
            database.engine.checkpoint().unwrap();
            let root = database.snapshot().root.unwrap();
            let Page::Internal { children, .. } =
                Page::decode(&database.engine.read_block(root).unwrap()).unwrap()
            else {
                panic!("seeded entity root is not internal");
            };
            child_id = children[0].block_id;
            child_key = EntityKey(children[0].lower_bound.clone());
        }

        let hexadecimal = child_id.to_hex();
        let path = std::path::Path::new("/data")
            .join("blocks")
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"));
        let mut file = vfs
            .open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(16, b"X").unwrap();
        file.sync_all().unwrap();

        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &child_key).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn single_key_commit_read_work_is_bounded_by_tree_depth() {
        const MAXIMUM_POINT_COMMIT_READS: usize = 32;
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..256_u16 {
            database
                .put(&mut seed, key(index), vec![index as u8; 8 * 1024])
                .unwrap();
        }
        database.commit(seed).unwrap();

        vfs.arm_fault(None).unwrap();
        let mut update = database.begin(7, 11).unwrap();
        database
            .put(&mut update, key(127), b"updated".to_vec())
            .unwrap();
        database.commit(update).unwrap();
        let reads = vfs
            .trace()
            .unwrap()
            .into_iter()
            .filter(|operation| operation == &crate::vfs::Operation::Read)
            .count();
        assert!(
            reads <= MAXIMUM_POINT_COMMIT_READS,
            "single-key commit performed {reads} page/file reads"
        );
    }
}
