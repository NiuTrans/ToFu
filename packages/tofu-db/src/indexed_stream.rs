//! Sparse natural-key index over a continuous immutable physical stream.
//!
//! Agent events use caller-assigned sparse sequence numbers. The entity index
//! provides natural-key idempotency, ordered range reads and exact bounds;
//! each index entry points at one continuous physical stream position. Both
//! families are changed by the same Authority transaction.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

#[path = "indexed_stream/migration.rs"]
pub(crate) mod migration;

#[cfg(test)]
#[path = "indexed_stream/read_guard_tests.rs"]
mod read_guard_tests;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{MAX_STREAM_EVENT_BYTES, MAX_TRANSACTION_IR_LITERAL_BYTES};
use crate::stream::{StreamEvent, StreamKey};
use crate::versioned_document::{
    materialize_stored_document_bounded, put_with_blob_owner_bounded, PutRequest,
};

const INDEX_MAGIC: &[u8; 8] = b"TDBIDX01";
const META_MAGIC: &[u8; 8] = b"TDBIMT01";
const TYPE_META_MAGIC: &[u8; 8] = b"TDBTYP01";
const VERSION: u32 = 1;
const INDEX_NAMESPACE: &str = "task_event_index";
const STREAM_DOMAIN: &str = "task_event";

/// Existing encoders are the source of composition costs. This is a soft
/// estimate: existing tree/overlay state, duplicate rows and large-event blob
/// indirection can change actual carried bytes. Engine admission stays final.
fn append_composition_metadata_bytes(task_id: &str, event_type: &str) -> io::Result<usize> {
    let entry = IndexEntry { application_sequence: 1, physical_sequence: 1,
        created_at_ms: 1, payload_digest: [0; 32] };
    let retention = RetentionEntry { task_id: task_id.to_owned(),
        event_type: event_type.to_owned(), kind_class: 0, index: entry }.encode()?;
    let writes = [
        (index_key(1, 1, task_id, 1)?, entry.encode().len()),
        (physical_index_key(1, 1, task_id, 1)?, entry.encode().len()),
        (metadata_key(1, 1, task_id)?, Metadata::default().encode().len()),
        (retention_key(1, 1, RetentionClass::Streaming, 1, task_id, 1)?, retention.len()),
        (type_index_key(1, 1, task_id, event_type, 1)?, entry.encode().len()),
        (type_age_key(1, 1, task_id, event_type, 1, 1)?, entry.encode().len()),
        (type_catalog_key(1, 1, task_id, event_type)?, TypeMetadata::default().encode().len()),
    ];
    let indexed_bytes: usize = writes.iter().map(|(key, value_bytes)|
        8 + key.encoded().len() + value_bytes).sum();
    Ok(indexed_bytes + crate::stream::append_composition_catalog_bytes(
        &stream_key(1, 1, task_id)?)?)
}

pub(crate) fn event_batch_composition_budget() -> serde_json::Value {
    // These one-character synthetic identities are valid without source IO.
    let sample = append_composition_metadata_bytes("t", "x").expect("valid format sample");
    let task_copies = append_composition_metadata_bytes("tt", "x").expect("valid format sample") - sample;
    let type_copies = append_composition_metadata_bytes("t", "xx").expect("valid format sample") - sample;
    let (segment_header, event_header) = crate::stream::append_composition_segment_overhead(STREAM_DOMAIN);
    let envelope = serde_json::to_vec(&serde_json::json!({"v": crate::generated_tofudb_ir::EVENT_FORMAT_VERSION}))
        .expect("integer event format envelope");
    // For a nonempty object, stamping replaces neither producer fields nor
    // bytes: add comma plus the interior of {"v":N}. Empty/scalar costs less.
    let envelope_bytes = envelope.len() - 1;
    let cap = crate::transaction::MAX_CARRIED_PAYLOAD_BYTES;
    serde_json::json!({
        "contract": crate::generated_storage_v2::NATIVE_EVENT_BATCH_COMPOSITION_CONTRACT,
        "maximumCarriedPayloadBytes": cap,
        "transactionHeadroomBytes": cap / 4,
        "streamSegmentHeaderBytes": segment_header,
        "streamEventHeaderBytes": event_header,
        "eventEnvelopeBytes": envelope_bytes,
        "metadataBaseBytes": sample - task_copies - type_copies,
        "metadataTaskIdCopies": task_copies,
        "metadataTypeCopies": type_copies,
    })
}
const TYPE_INDEX_TAG: u8 = 2;
const TYPE_CATALOG_TAG: u8 = 3;
const RETENTION_INDEX_TAG: u8 = 4;
const TYPE_AGE_INDEX_TAG: u8 = 5;
const PHYSICAL_INDEX_TAG: u8 = 6;
const RETIREMENT_QUEUE_TAG: u8 = 7;
const PAYLOAD_DOCUMENT_TAG: u8 = 8;
const RETENTION_SWEEP_TAG: u8 = 9;
// Binary prefix cannot collide with legacy JSON payloads. Physical stream caps stay unchanged.
const PAYLOAD_REFERENCE_MAGIC: &[u8; 8] = b"\0TDBEV01";
const RETIREMENT_QUEUE_MAGIC: &[u8; 8] = b"TDBSRQ01";
const RETENTION_MAGIC: &[u8; 8] = b"TDBRET01";
const RETENTION_SWEEP_MAGIC: &[u8; 8] = b"TDBRSW01";
// Retention sweep slicing (2026-09-22 deadlock): deleted index entries stay
// in the entity tree as OCC tombstones until their pages are rewritten, so a
// churn-heavy class accumulates a tombstone field no later write reclaims.
// A bounded retention scan that must cross it walks one tombstone leaf per
// step without collecting a single live row, trips the 1,024-leaf range
// witness, and fails closed with database_resource_exhausted — determinism
// that locked streaming retention out entirely. The sweep therefore advances
// a durable per-class watermark in adaptive time slices: one bounded slice
// per call, halved while a slice overflows the witness (dense tombstones),
// doubled after a drained slice (empty ancient history), and persisted so
// the crossed region is never rescanned. Tombstones need no deletion — a
// slice that yields fewer than `limit` live rows is provably swept.
// A cold cursor probes with one hour, then widens ×8 per drained slice up
// to the ~35-year cap, so one call skips dead history in a handful of seeks
// while any realistic cutoff (retention horizons are days) stays reachable
// inside the skip budget. The small first step matters: a WIDE cold slice
// over a dense tombstone field overflows the witness and every halving
// retry re-walks the same prefix (≥1,024 leaves, ~7 s per attempt on the
// production FUSE volume), so the whole chain could outlive the caller's
// deadline and lose the uncommitted cursor; a narrow start never trips the
// dense field at all, and the ×8 growth recovers the stride immediately.
const RETENTION_SWEEP_INITIAL_SLICE_MS: i64 = 3_600_000;
const RETENTION_SWEEP_MAX_SLICE_MS: i64 = 1_i64 << 40;
const RETENTION_SWEEP_MIN_SLICE_MS: i64 = 60_000;
const RETENTION_SWEEP_MAX_SKIP_ATTEMPTS: usize = 13;
// Hard per-call budget: the authority watchdog's wedge threshold is 10 s;
// sweep calls must commit well under it even on a cold FUSE page cache.
const PRUNE_CALL_TIME_BUDGET: Duration = Duration::from_millis(5_000);
// The post-delete reconciliation pass is O(distinct task / type entries), not
// O(rows), but one batch is already bounded by `entity_scan(...,
// request.limit)` (<= 1000 rows, hence <= 2000 distinct task/type entries),
// and the named 1,000-distinct-task budget test certifies that envelope within
// the hard write budget. A tighter entry cap is therefore not a safety net but
// a contract break: 256 silently truncated a valid 1,000-row batch to 128
// rows (`event_prune_admits_one_thousand_distinct_tasks_within_hard_write_budget`).
const RETENTION_SWEEP_MAX_SHRINK_ATTEMPTS: usize = 12;
const MAX_FILTER_EVENT_TYPES: usize = 1_000;
const MAX_INSPECTOR_TASKS: usize = 1_000;
const STRUCTURAL_EVENT_TYPES: [&str; 6] = [
    "flow_iteration",
    "messages_snapshot",
    "round_end",
    "round_start",
    "round_usage",
    "tool_wire_projection",
];
const LEGACY_FLOW_EVENT_PREFIX: &str = "endpoint_";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct IndexEntry {
    application_sequence: u64,
    physical_sequence: u64,
    created_at_ms: i64,
    payload_digest: [u8; 32],
}

#[derive(Clone, Copy, Default)]
struct Metadata {
    count: u64,
    minimum: u64,
    maximum: u64,
}

#[derive(Clone, Copy, Default)]
struct TypeMetadata {
    count: u64,
    first_created_at_ms: i64,
    request_count: u64,
    state_count: u64,
    legacy_count: u64,
}

pub(crate) struct AppendRequest {
    pub task_id: String,
    pub application_sequence: u64,
    pub event_type: String,
    pub payload_json: Vec<u8>,
    pub created_at_ms: i64,
}

pub(crate) struct ListRequest {
    pub task_id: String,
    pub after_sequence: Option<u64>,
    pub limit: usize,
    pub types: Vec<String>,
    pub type_prefixes: Vec<String>,
    pub payload_types: Option<Vec<String>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RetentionClass {
    Streaming,
    Structural,
}

impl RetentionClass {
    const fn tag(self) -> u8 {
        match self {
            Self::Streaming => 0,
            Self::Structural => 1,
        }
    }
}

pub(crate) struct PruneRequest {
    pub created_before_ms: i64,
    pub limit: usize,
    pub retention_class: RetentionClass,
    /// Wall-clock now of the request that compiled this prune. The sweep
    /// watermark may never advance past it: retention entries are stamped
    /// with the daemon's clock at append time, so a sweep that ran ahead of
    /// now would skip the region where every later append lands, leaking
    /// those events from retention forever (the differential's far-future
    /// cutoff made exactly that divergence).
    pub now_unix_ms: u64,
}

struct RetentionEntry {
    task_id: String,
    event_type: String,
    kind_class: u8,
    index: IndexEntry,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn identity_prefix(task_id: &str) -> io::Result<Vec<u8>> {
    let task = task_id.as_bytes();
    let length = u16::try_from(task.len()).map_err(|_| invalid_input("task id is too long"))?;
    let mut bytes = Vec::with_capacity(2 + task.len());
    bytes.extend_from_slice(&length.to_be_bytes());
    bytes.extend_from_slice(task);
    Ok(bytes)
}

fn index_key(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut bytes = Vec::from([0]);
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    bytes.extend_from_slice(&sequence.to_be_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn index_range(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    after: Option<u64>,
) -> io::Result<(EntityKey, EntityKey)> {
    let mut prefix = Vec::from([0]);
    prefix.extend_from_slice(&identity_prefix(task_id)?);
    let (_, end) = EntityKey::prefix_range(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)?;
    if let Some(after) = after {
        let next = after
            .checked_add(1)
            .ok_or_else(|| invalid_input("event cursor is exhausted"))?;
        prefix.extend_from_slice(&next.to_be_bytes());
    }
    Ok((
        EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)?,
        end,
    ))
}

fn metadata_key(tenant_id: u64, owner_user_id: u64, task_id: &str) -> io::Result<EntityKey> {
    let mut bytes = Vec::from([1]);
    bytes.extend_from_slice(task_id.as_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn metadata_range(
    tenant_id: u64,
    owner_user_id: u64,
    task_prefix: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    let mut bytes = Vec::from([1]);
    bytes.extend_from_slice(task_prefix.as_bytes());
    EntityKey::prefix_range(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn physical_index_prefix(task_id: &str) -> io::Result<Vec<u8>> {
    let mut bytes = vec![PHYSICAL_INDEX_TAG];
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    Ok(bytes)
}

fn physical_index_key(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    physical_sequence: u64,
) -> io::Result<EntityKey> {
    let mut bytes = physical_index_prefix(task_id)?;
    bytes.extend_from_slice(&physical_sequence.to_be_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn physical_index_range(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    let prefix = physical_index_prefix(task_id)?;
    EntityKey::prefix_range(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)
}

fn retirement_queue_key(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
) -> io::Result<EntityKey> {
    let mut bytes = vec![RETIREMENT_QUEUE_TAG];
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn retirement_queue_range(
    tenant_id: u64,
    owner_user_id: u64,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        INDEX_NAMESPACE,
        &[RETIREMENT_QUEUE_TAG],
    )
}

fn encode_retirement_queue_target(target: u64) -> io::Result<Vec<u8>> {
    if target == 0 {
        return Err(invalid_input("stream retirement target is zero"));
    }
    let mut bytes = Vec::with_capacity(20);
    bytes.extend_from_slice(RETIREMENT_QUEUE_MAGIC);
    bytes.extend_from_slice(&VERSION.to_le_bytes());
    bytes.extend_from_slice(&target.to_le_bytes());
    Ok(bytes)
}

fn decode_retirement_queue_target(bytes: &[u8]) -> io::Result<u64> {
    if bytes.len() != 20
        || &bytes[..8] != RETIREMENT_QUEUE_MAGIC
        || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
    {
        return Err(invalid_data("invalid stream retirement queue target"));
    }
    let target = u64::from_le_bytes(bytes[12..20].try_into().unwrap());
    if target == 0 {
        return Err(invalid_data("stream retirement queue target is zero"));
    }
    Ok(target)
}

fn retirement_queue_task_id(key: &EntityKey) -> io::Result<String> {
    let bytes = key.key_bytes();
    if bytes.first() != Some(&RETIREMENT_QUEUE_TAG) || bytes.len() < 3 {
        return Err(invalid_data("invalid stream retirement queue key"));
    }
    let length = u16::from_be_bytes(bytes[1..3].try_into().unwrap()) as usize;
    if bytes.len() != 3 + length {
        return Err(invalid_data("invalid stream retirement queue identity"));
    }
    String::from_utf8(bytes[3..].to_vec())
        .map_err(|_| invalid_data("stream retirement queue identity is not UTF-8"))
}

fn type_index_prefix(task_id: &str, event_type: &str) -> io::Result<Vec<u8>> {
    let event_type_bytes = event_type.as_bytes();
    let event_type_length = u16::try_from(event_type_bytes.len())
        .map_err(|_| invalid_input("event type is too long"))?;
    let mut bytes = Vec::from([TYPE_INDEX_TAG]);
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    bytes.extend_from_slice(&event_type_length.to_be_bytes());
    bytes.extend_from_slice(event_type_bytes);
    Ok(bytes)
}

fn type_index_key(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    event_type: &str,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut bytes = type_index_prefix(task_id, event_type)?;
    bytes.extend_from_slice(&sequence.to_be_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn type_index_range(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    event_type: &str,
    after: Option<u64>,
) -> io::Result<(EntityKey, EntityKey)> {
    let prefix = type_index_prefix(task_id, event_type)?;
    let (_, end) = EntityKey::prefix_range(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)?;
    let start = match after {
        Some(after) => {
            let next = after
                .checked_add(1)
                .ok_or_else(|| invalid_input("event cursor is exhausted"))?;
            type_index_key(tenant_id, owner_user_id, task_id, event_type, next)?
        }
        None => EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)?,
    };
    Ok((start, end))
}

fn type_catalog_key(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    event_type: &str,
) -> io::Result<EntityKey> {
    let mut bytes = Vec::from([TYPE_CATALOG_TAG]);
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    bytes.extend_from_slice(event_type.as_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn type_catalog_range(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    event_type_prefix: &str,
) -> io::Result<(EntityKey, EntityKey, usize)> {
    let mut bytes = Vec::from([TYPE_CATALOG_TAG]);
    let task_prefix = identity_prefix(task_id)?;
    bytes.extend_from_slice(&task_prefix);
    bytes.extend_from_slice(event_type_prefix.as_bytes());
    let (start, end) = EntityKey::prefix_range(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)?;
    Ok((start, end, 1 + task_prefix.len()))
}

fn retention_class(event_type: &str) -> RetentionClass {
    if event_type.is_empty() || STRUCTURAL_EVENT_TYPES.contains(&event_type) {
        RetentionClass::Structural
    } else {
        RetentionClass::Streaming
    }
}

fn retention_key(
    tenant_id: u64,
    owner_user_id: u64,
    class: RetentionClass,
    created_at_ms: i64,
    task_id: &str,
    sequence: u64,
) -> io::Result<EntityKey> {
    if created_at_ms <= 0 {
        return Err(invalid_input("event retention timestamp is invalid"));
    }
    let mut bytes = vec![RETENTION_INDEX_TAG, class.tag()];
    bytes.extend_from_slice(&(created_at_ms as u64).to_be_bytes());
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    bytes.extend_from_slice(&sequence.to_be_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

/// One sweep slice `[floor_ms, cutoff_ms)`: a zero floor is the class prefix
/// itself (the whole timeline so far).
fn retention_window_range(
    tenant_id: u64,
    owner_user_id: u64,
    class: RetentionClass,
    floor_ms: i64,
    cutoff: i64,
) -> io::Result<(EntityKey, EntityKey)> {
    if floor_ms < 0 || cutoff <= floor_ms {
        return Err(invalid_input("event retention sweep window is invalid"));
    }
    let mut start = vec![RETENTION_INDEX_TAG, class.tag()];
    if floor_ms > 0 {
        start.extend_from_slice(&(floor_ms as u64).to_be_bytes());
    }
    let mut end = vec![RETENTION_INDEX_TAG, class.tag()];
    end.extend_from_slice(&(cutoff as u64).to_be_bytes());
    Ok((
        EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &start)?,
        EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &end)?,
    ))
}

fn retention_sweep_cursor_key(
    tenant_id: u64,
    owner_user_id: u64,
    class: RetentionClass,
) -> io::Result<EntityKey> {
    EntityKey::new(
        tenant_id,
        owner_user_id,
        INDEX_NAMESPACE,
        &[RETENTION_SWEEP_TAG, class.tag()],
    )
}

fn encode_retention_sweep_cursor(swept_through_ms: i64, slice_ms: i64) -> io::Result<Vec<u8>> {
    if swept_through_ms <= 0 || !(RETENTION_SWEEP_MIN_SLICE_MS..=RETENTION_SWEEP_MAX_SLICE_MS).contains(&slice_ms) {
        return Err(invalid_input("event retention sweep cursor is invalid"));
    }
    let mut bytes = Vec::with_capacity(24);
    bytes.extend_from_slice(RETENTION_SWEEP_MAGIC);
    bytes.extend_from_slice(&swept_through_ms.to_be_bytes());
    bytes.extend_from_slice(&slice_ms.to_be_bytes());
    Ok(bytes)
}

fn decode_retention_sweep_cursor(raw: &[u8]) -> io::Result<(i64, i64)> {
    if raw.len() != 24 || &raw[..8] != RETENTION_SWEEP_MAGIC {
        return Err(invalid_data("invalid event retention sweep cursor"));
    }
    let swept_through_ms = i64::from_be_bytes(raw[8..16].try_into().unwrap());
    let slice_ms = i64::from_be_bytes(raw[16..24].try_into().unwrap());
    if swept_through_ms <= 0
        || !(RETENTION_SWEEP_MIN_SLICE_MS..=RETENTION_SWEEP_MAX_SLICE_MS).contains(&slice_ms)
    {
        return Err(invalid_data("event retention sweep cursor is out of bounds"));
    }
    Ok((swept_through_ms, slice_ms))
}

fn type_age_prefix(task_id: &str, event_type: &str) -> io::Result<Vec<u8>> {
    let event_type_bytes = event_type.as_bytes();
    let event_type_length = u16::try_from(event_type_bytes.len())
        .map_err(|_| invalid_input("event type is too long"))?;
    let mut bytes = vec![TYPE_AGE_INDEX_TAG];
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    bytes.extend_from_slice(&event_type_length.to_be_bytes());
    bytes.extend_from_slice(event_type_bytes);
    Ok(bytes)
}

fn type_age_key(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    event_type: &str,
    created_at_ms: i64,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut bytes = type_age_prefix(task_id, event_type)?;
    bytes.extend_from_slice(&(created_at_ms as u64).to_be_bytes());
    bytes.extend_from_slice(&sequence.to_be_bytes());
    EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &bytes)
}

fn type_age_range(
    tenant_id: u64,
    owner_user_id: u64,
    task_id: &str,
    event_type: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    let prefix = type_age_prefix(task_id, event_type)?;
    EntityKey::prefix_range(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)
}

fn stream_key(tenant_id: u64, owner_user_id: u64, task_id: &str) -> io::Result<StreamKey> {
    StreamKey::new(tenant_id, owner_user_id, STREAM_DOMAIN, task_id.as_bytes())
}

impl IndexEntry {
    fn encode(self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(68);
        bytes.extend_from_slice(INDEX_MAGIC);
        bytes.extend_from_slice(&VERSION.to_le_bytes());
        bytes.extend_from_slice(&self.application_sequence.to_le_bytes());
        bytes.extend_from_slice(&self.physical_sequence.to_le_bytes());
        bytes.extend_from_slice(&self.created_at_ms.to_le_bytes());
        bytes.extend_from_slice(&self.payload_digest);
        bytes
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() != 68
            || &bytes[..8] != INDEX_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        {
            return Err(invalid_data("invalid indexed-stream entry"));
        }
        let value = Self {
            application_sequence: u64::from_le_bytes(bytes[12..20].try_into().unwrap()),
            physical_sequence: u64::from_le_bytes(bytes[20..28].try_into().unwrap()),
            created_at_ms: i64::from_le_bytes(bytes[28..36].try_into().unwrap()),
            payload_digest: bytes[36..68].try_into().unwrap(),
        };
        if value.physical_sequence == 0 || value.created_at_ms <= 0 {
            return Err(invalid_data("invalid indexed-stream position or clock"));
        }
        Ok(value)
    }
}

impl Metadata {
    fn encode(self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(36);
        bytes.extend_from_slice(META_MAGIC);
        bytes.extend_from_slice(&VERSION.to_le_bytes());
        bytes.extend_from_slice(&self.count.to_le_bytes());
        bytes.extend_from_slice(&self.minimum.to_le_bytes());
        bytes.extend_from_slice(&self.maximum.to_le_bytes());
        bytes
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() != 36
            || &bytes[..8] != META_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        {
            return Err(invalid_data("invalid indexed-stream metadata"));
        }
        let value = Self {
            count: u64::from_le_bytes(bytes[12..20].try_into().unwrap()),
            minimum: u64::from_le_bytes(bytes[20..28].try_into().unwrap()),
            maximum: u64::from_le_bytes(bytes[28..36].try_into().unwrap()),
        };
        if value.count == 0 || value.minimum > value.maximum {
            return Err(invalid_data("inconsistent indexed-stream metadata"));
        }
        Ok(value)
    }
}

impl TypeMetadata {
    fn encode(self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(52);
        bytes.extend_from_slice(TYPE_META_MAGIC);
        bytes.extend_from_slice(&VERSION.to_le_bytes());
        bytes.extend_from_slice(&self.count.to_le_bytes());
        bytes.extend_from_slice(&self.first_created_at_ms.to_le_bytes());
        bytes.extend_from_slice(&self.request_count.to_le_bytes());
        bytes.extend_from_slice(&self.state_count.to_le_bytes());
        bytes.extend_from_slice(&self.legacy_count.to_le_bytes());
        bytes
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() != 52
            || &bytes[..8] != TYPE_META_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        {
            return Err(invalid_data("invalid event type metadata"));
        }
        let value = Self {
            count: u64::from_le_bytes(bytes[12..20].try_into().unwrap()),
            first_created_at_ms: i64::from_le_bytes(bytes[20..28].try_into().unwrap()),
            request_count: u64::from_le_bytes(bytes[28..36].try_into().unwrap()),
            state_count: u64::from_le_bytes(bytes[36..44].try_into().unwrap()),
            legacy_count: u64::from_le_bytes(bytes[44..52].try_into().unwrap()),
        };
        if value.count == 0
            || value.first_created_at_ms <= 0
            || value.request_count > value.count
            || value.state_count > value.count
            || value.legacy_count > value.count
            || value
                .request_count
                .checked_add(value.state_count)
                .and_then(|count| count.checked_add(value.legacy_count))
                .is_none_or(|classified| classified > value.count)
        {
            return Err(invalid_data("inconsistent event type metadata"));
        }
        Ok(value)
    }
}

impl RetentionEntry {
    fn encode(&self) -> io::Result<Vec<u8>> {
        let task = self.task_id.as_bytes();
        let event_type = self.event_type.as_bytes();
        let task_length =
            u16::try_from(task.len()).map_err(|_| invalid_input("task id is too long"))?;
        let type_length =
            u16::try_from(event_type.len()).map_err(|_| invalid_input("event type is too long"))?;
        let mut bytes = Vec::with_capacity(17 + task.len() + event_type.len() + 68);
        bytes.extend_from_slice(RETENTION_MAGIC);
        bytes.extend_from_slice(&VERSION.to_le_bytes());
        bytes.extend_from_slice(&task_length.to_le_bytes());
        bytes.extend_from_slice(&type_length.to_le_bytes());
        bytes.push(self.kind_class);
        bytes.extend_from_slice(task);
        bytes.extend_from_slice(event_type);
        bytes.extend_from_slice(&self.index.encode());
        Ok(bytes)
    }

    fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < 17 + 68
            || &bytes[..8] != RETENTION_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        {
            return Err(invalid_data("invalid event retention entry"));
        }
        let task_length = u16::from_le_bytes(bytes[12..14].try_into().unwrap()) as usize;
        let type_length = u16::from_le_bytes(bytes[14..16].try_into().unwrap()) as usize;
        let kind_class = bytes[16];
        let task_end = 17_usize
            .checked_add(task_length)
            .ok_or_else(|| invalid_data("event retention length overflow"))?;
        let type_end = task_end
            .checked_add(type_length)
            .ok_or_else(|| invalid_data("event retention length overflow"))?;
        if kind_class > 3 || type_end.checked_add(68) != Some(bytes.len()) {
            return Err(invalid_data("invalid event retention entry length"));
        }
        let task_id = std::str::from_utf8(&bytes[17..task_end])
            .map_err(|_| invalid_data("event retention task id is not UTF-8"))?
            .to_owned();
        let event_type = std::str::from_utf8(&bytes[task_end..type_end])
            .map_err(|_| invalid_data("event retention type is not UTF-8"))?
            .to_owned();
        Ok(Self {
            task_id,
            event_type,
            kind_class,
            index: IndexEntry::decode(&bytes[type_end..])?,
        })
    }
}

fn payload_document_key(
    tenant: u64,
    owner: u64,
    task_id: &str,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut bytes = vec![PAYLOAD_DOCUMENT_TAG];
    bytes.extend_from_slice(&identity_prefix(task_id)?);
    bytes.extend_from_slice(&sequence.to_be_bytes());
    EntityKey::new(tenant, owner, INDEX_NAMESPACE, &bytes)
}

fn payload_logical_key(task_id: &str, sequence: u64) -> String {
    // JSON tuple retains unambiguous identity even when task IDs contain separators.
    serde_json::to_string(&(task_id, sequence)).unwrap()
}

fn materialize_event_payload<'a>(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    entry: IndexEntry,
    event: &'a StreamEvent,
) -> io::Result<Cow<'a, [u8]>> {
    let payload = if event.payload.starts_with(PAYLOAD_REFERENCE_MAGIC) {
        if event.payload.len() != 16
            || event.payload[8..] != entry.application_sequence.to_be_bytes()
        {
            return Err(invalid_data("indexed event payload reference differs"));
        }
        let key = payload_document_key(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            task_id,
            entry.application_sequence,
        )?;
        let stored = database
            .entity_get(transaction, &key)?
            .ok_or_else(|| invalid_data("indexed event payload document is missing"))?;
        let (logical_key, raw) = materialize_stored_document_bounded(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &stored,
            INDEX_NAMESPACE,
            MAX_TRANSACTION_IR_LITERAL_BYTES,
        )?;
        if logical_key != payload_logical_key(task_id, entry.application_sequence) {
            return Err(invalid_data(
                "indexed event payload document identity differs",
            ));
        }
        Cow::Owned(raw)
    } else {
        Cow::Borrowed(event.payload.as_slice())
    };
    if event.created_at_ms != entry.created_at_ms
        || blake3::hash(&payload).as_bytes() != &entry.payload_digest
    {
        return Err(invalid_data("indexed event witness mismatch"));
    }
    Ok(payload)
}

// An accepted large event must fit a one-row replay page, including its envelope.
// Count normalized JSON without allocating another full serialized copy.
fn validate_large_replay_page(request: &AppendRequest) -> io::Result<()> {
    struct ByteBudget(usize);
    impl io::Write for ByteBudget {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0 = self
                .0
                .checked_sub(bytes.len())
                .ok_or_else(|| invalid_input("indexed event replay page exceeds 8 MiB"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let value: Value = serde_json::from_slice(&request.payload_json)
        .map_err(|_| invalid_input("indexed event JSON is invalid"))?;
    serde_json::to_writer(&mut ByteBudget(MAX_TRANSACTION_IR_LITERAL_BYTES - 2),
        &json!({"sequence": request.application_sequence, "event": value, "created_at_ms": request.created_at_ms}))
        .map_err(|_| invalid_input("indexed event replay page exceeds 8 MiB"))
}

fn project_event(entry: IndexEntry, payload: &[u8]) -> io::Result<Vec<u8>> {
    let value: Value = serde_json::from_slice(payload)
        .map_err(|_| invalid_data("indexed event JSON is invalid"))?;
    serde_json::to_vec(&json!({"sequence": entry.application_sequence, "event": value, "created_at_ms": entry.created_at_ms}))
        .map_err(|_| invalid_data("indexed event projection failed"))
}

// The authenticated physical stream already carries the normalized type and
// producer timestamp. An omitted payload is not a claim about its contents;
// full replay still materializes and verifies the payload digest below.
fn project_event_metadata(entry: IndexEntry, event: &StreamEvent) -> io::Result<Vec<u8>> {
    if event.created_at_ms != entry.created_at_ms {
        return Err(invalid_data("indexed event timestamp witness mismatch"));
    }
    serde_json::to_vec(&json!({"sequence": entry.application_sequence,
        "event": {"type": event.event_type}, "created_at_ms": entry.created_at_ms}))
        .map_err(|_| invalid_data("indexed event metadata projection failed"))
}

fn logical_event_type_from_json(payload: &[u8]) -> io::Result<String> {
    let value: Value = serde_json::from_slice(payload)
        .map_err(|_| invalid_data("indexed event JSON is invalid"))?;
    Ok(value
        .as_object()
        .and_then(|object| object.get("type"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(128)
        .collect())
}

fn logical_event_kind_from_json(payload: &[u8]) -> io::Result<String> {
    let value: Value = serde_json::from_slice(payload)
        .map_err(|_| invalid_data("indexed event JSON is invalid"))?;
    Ok(value
        .as_object()
        .and_then(|object| object.get("kind"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(128)
        .collect())
}

pub(crate) fn append(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AppendRequest,
) -> io::Result<Vec<u8>> {
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let index_key = index_key(
        tenant,
        owner,
        &request.task_id,
        request.application_sequence,
    )?;
    if request.payload_json.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(invalid_input("indexed event payload exceeds 8 MiB"));
    }
    let digest = *blake3::hash(&request.payload_json).as_bytes();
    let logical_type = logical_event_type_from_json(&request.payload_json)?;
    if let Some(existing) = database.entity_get(transaction, &index_key)? {
        let entry = IndexEntry::decode(&existing)?;
        if entry.application_sequence != request.application_sequence
            || entry.payload_digest != digest
        {
            return Err(conflict("event sequence has a conflicting payload"));
        }
        return Ok(serde_json::to_vec(&json!({"inserted": false, "task_id": request.task_id, "sequence": request.application_sequence})).unwrap());
    }
    let stream_key = stream_key(tenant, owner, &request.task_id)?;
    let physical_sequence = database.transaction_stream_next_sequence(transaction, &stream_key)?;
    let metadata_key = metadata_key(tenant, owner, &request.task_id)?;
    let current = database
        .entity_get(transaction, &metadata_key)?
        .as_deref()
        .map(Metadata::decode)
        .transpose()?
        .unwrap_or_default();
    let metadata = if current.count == 0 {
        Metadata {
            count: 1,
            minimum: request.application_sequence,
            maximum: request.application_sequence,
        }
    } else {
        Metadata {
            count: current
                .count
                .checked_add(1)
                .ok_or_else(|| invalid_data("event count overflow"))?,
            minimum: current.minimum.min(request.application_sequence),
            maximum: current.maximum.max(request.application_sequence),
        }
    };
    let physical_payload = if request.payload_json.len() > MAX_STREAM_EVENT_BYTES {
        validate_large_replay_page(request)?;
        put_with_blob_owner_bounded(
            database,
            transaction,
            PutRequest {
                key: payload_document_key(
                    tenant,
                    owner,
                    &request.task_id,
                    request.application_sequence,
                )?,
                namespace: INDEX_NAMESPACE.to_owned(),
                logical_key: payload_logical_key(&request.task_id, request.application_sequence),
                value_json: request.payload_json.clone(),
                expected_version: Some(0),
                updated_at_ms: u64::try_from(request.created_at_ms)
                    .map_err(|_| invalid_input("event retention timestamp is invalid"))?,
            },
            owner,
            MAX_TRANSACTION_IR_LITERAL_BYTES,
        )?;
        let mut reference = PAYLOAD_REFERENCE_MAGIC.to_vec();
        reference.extend_from_slice(&request.application_sequence.to_be_bytes());
        reference
    } else {
        request.payload_json.clone()
    };
    database.stream_append(
        transaction,
        stream_key,
        physical_sequence,
        vec![StreamEvent::new(
            request.created_at_ms,
            &request.event_type,
            physical_payload,
        )?],
    )?;
    let entry = IndexEntry {
        application_sequence: request.application_sequence,
        physical_sequence,
        created_at_ms: request.created_at_ms,
        payload_digest: digest,
    };
    database.entity_put(transaction, index_key, entry.encode())?;
    database.entity_put(
        transaction,
        physical_index_key(tenant, owner, &request.task_id, physical_sequence)?,
        entry.encode(),
    )?;
    let event_kind = logical_event_kind_from_json(&request.payload_json)?;
    let kind_class = if logical_type == "messages_snapshot" {
        match event_kind.as_str() {
            "request" => 1,
            "state" => 2,
            "" => 3,
            _ => 0,
        }
    } else {
        0
    };
    let class = retention_class(&logical_type);
    database.entity_put(
        transaction,
        retention_key(
            tenant,
            owner,
            class,
            request.created_at_ms,
            &request.task_id,
            request.application_sequence,
        )?,
        RetentionEntry {
            task_id: request.task_id.clone(),
            event_type: logical_type.clone(),
            kind_class,
            index: entry,
        }
        .encode()?,
    )?;
    if !logical_type.is_empty() {
        database.entity_put(
            transaction,
            type_index_key(
                tenant,
                owner,
                &request.task_id,
                &logical_type,
                request.application_sequence,
            )?,
            entry.encode(),
        )?;
        database.entity_put(
            transaction,
            type_age_key(
                tenant,
                owner,
                &request.task_id,
                &logical_type,
                request.created_at_ms,
                request.application_sequence,
            )?,
            entry.encode(),
        )?;
        let catalog_key = type_catalog_key(tenant, owner, &request.task_id, &logical_type)?;
        let current_type = database
            .entity_get(transaction, &catalog_key)?
            .as_deref()
            .map(TypeMetadata::decode)
            .transpose()?
            .unwrap_or_default();
        let messages_snapshot = logical_type == "messages_snapshot";
        let next_type = TypeMetadata {
            count: current_type
                .count
                .checked_add(1)
                .ok_or_else(|| invalid_data("event type count overflow"))?,
            first_created_at_ms: if current_type.count == 0 {
                request.created_at_ms
            } else {
                current_type.first_created_at_ms.min(request.created_at_ms)
            },
            request_count: current_type.request_count
                + u64::from(messages_snapshot && event_kind == "request"),
            state_count: current_type.state_count
                + u64::from(messages_snapshot && event_kind == "state"),
            legacy_count: current_type.legacy_count
                + u64::from(messages_snapshot && event_kind.is_empty()),
        };
        database.entity_put(transaction, catalog_key, next_type.encode())?;
    }
    database.entity_put(transaction, metadata_key, metadata.encode())?;
    Ok(serde_json::to_vec(&json!({"inserted": true, "task_id": request.task_id, "sequence": request.application_sequence})).unwrap())
}

fn entry_sequence(key: &EntityKey) -> io::Result<u64> {
    let bytes = key.key_bytes();
    let sequence = bytes
        .get(bytes.len().saturating_sub(8)..)
        .ok_or_else(|| invalid_data("event index key has no sequence"))?;
    Ok(u64::from_be_bytes(sequence.try_into().unwrap()))
}

pub(crate) fn prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &PruneRequest,
    deadline_elapsed: &dyn Fn() -> bool,
) -> io::Result<Vec<u8>> {
    if request.created_before_ms < 0 || request.limit == 0 || request.limit > 1_000 {
        return Err(invalid_input("event retention request is invalid"));
    }
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    // Authority-friendliness budget for the scan and delete phases (the
    // reconciliation pass is separately bounded; see the incident note at
    // the delete loop).
    let prune_started = Instant::now();
    let over_budget =
        || prune_started.elapsed() >= PRUNE_CALL_TIME_BUDGET || deadline_elapsed();
    let now_ms = i64::try_from(request.now_unix_ms).unwrap_or(i64::MAX);
    // Deletion horizon vs sweep ceiling: every row older than
    // `created_before_ms` is deletable (the caller's cutoff, however far
    // in the future), but the durable watermark may only claim ground up to
    // `now` — retention entries are stamped with the daemon's clock at
    // append time, so a watermark ahead of now would skip the very region
    // later appends land in and leak them from retention forever (the
    // differential's far-future cutoff made exactly that divergence).
    let cutoff = request.created_before_ms;
    let sweep_ceiling = cutoff.min(now_ms);
    let cursor_key = retention_sweep_cursor_key(tenant, owner, request.retention_class)?;
    let (mut swept_through_ms, mut slice_ms) =
        match database.entity_get(transaction, &cursor_key)? {
            Some(raw) => {
                let (swept, persisted_slice) = decode_retention_sweep_cursor(&raw)?;
                // Never trust a wide persisted slice: it encodes the LAST
                // call's density estimate, and tombstone density moves. A
                // huge persisted slice (e.g. chained up during a backlog
                // crisis) would overflow on its first walk and pay one
                // ~1,024-leaf rewalk per halving retry before every later
                // call — deterministic deadline livelock (observed on the
                // production FUSE volume). One hour is the safe reopening
                // width: dense live rows stop the scan within rows, and
                // empty spans regrow ×8 inside the call regardless.
                (swept, persisted_slice.min(RETENTION_SWEEP_INITIAL_SLICE_MS))
            }
            None => (0, RETENTION_SWEEP_INITIAL_SLICE_MS),
        };
    if swept_through_ms > now_ms {
        // The clock ran backwards past a previously persisted watermark:
        // later appends may already sit below it. Rescan from the class
        // prefix once rather than leaking them; the forward sweep then
        // re-establishes the watermark below the (lower) now.
        swept_through_ms = 0;
        slice_ms = RETENTION_SWEEP_INITIAL_SLICE_MS;
    }
    let mut selected: Vec<(EntityKey, Vec<u8>)> = Vec::new();
    let mut has_more_rows = false;
    let sweep_start_ms = swept_through_ms;
    if cutoff > 0 && swept_through_ms < cutoff {
        // One call chains bounded slices while they prove EMPTY (zero live
        // rows; tombstones never count and need no deletion), widening ×8 so
        // dead history is skipped in a few seeks. The first slice holding
        // live rows becomes the batch; the sweep then VERIFIES the rest of
        // the window is empty (the legacy authority's has_more contract)
        // without growing the batch past the caller's limit:
        // * a FULL slice (≥ limit rows) always ends the call with
        //   has_more — in or out of verification;
        // * the first DRAINED slice with rows (1..limit) is the batch and
        //   advances the watermark; verification mode then continues
        //   through empty slices to the ceiling so a fully swept window
        //   reports has_more=false like the legacy drain;
        // * any further rows found in verification prove has_more and end
        //   the call WITHOUT joining the batch — one bounded batch per
        //   call, and the next call resumes at the watermark;
        // * ground past `now` is scanned for deletions but never claimed:
        //   the advance clamps to the sweep ceiling and the call ends;
        // * a slice that overflows the range witness on a dense tombstone
        //   field is halved and retried inside the same transaction; the
        //   floor keeps whatever progress the call already made (only a
        //   completely progress-free call surfaces the error) — the error
        //   must never discard committed cursor ground and livelock the
        //   sweep against a slow volume.
        let mut skip_attempts = 0;
        let mut shrink_attempts = 0;
        let mut verifying = false;
        loop {
            if over_budget() {
                break;
            }
            let slice_end = swept_through_ms.saturating_add(slice_ms).min(cutoff);
            let (start, end) = retention_window_range(
                tenant,
                owner,
                request.retention_class,
                swept_through_ms,
                slice_end,
            )?;
            match database.entity_scan(transaction, &start, &end, request.limit) {
                Ok(rows) => {
                    let full = rows.len() >= request.limit;
                    let had_rows = !rows.is_empty();
                    if full {
                        // A full slice in verification only proves there are
                        // more rows; it must never grow the batch. Before
                        // verification the batch is still empty, so `extend`
                        // is an assignment in effect but stays correct if a
                        // future slice setup ever seeds `selected`.
                        if !verifying {
                            selected.extend(rows);
                        }
                        has_more_rows = true;
                        break;
                    }
                    if had_rows {
                        if verifying {
                            has_more_rows = true;
                            break;
                        }
                        selected.extend(rows);
                        verifying = true;
                    }
                    if slice_end > sweep_ceiling {
                        swept_through_ms = sweep_ceiling;
                        break;
                    }
                    swept_through_ms = slice_end;
                    slice_ms = (if had_rows {
                        slice_ms.saturating_mul(2)
                    } else {
                        slice_ms.saturating_mul(8)
                    })
                    .min(RETENTION_SWEEP_MAX_SLICE_MS);
                    if swept_through_ms >= sweep_ceiling {
                        break;
                    }
                    skip_attempts += 1;
                    if skip_attempts >= RETENTION_SWEEP_MAX_SKIP_ATTEMPTS {
                        break;
                    }
                }
                Err(error)
                    if error.kind() == io::ErrorKind::OutOfMemory
                        && slice_ms > RETENTION_SWEEP_MIN_SLICE_MS
                        && shrink_attempts < RETENTION_SWEEP_MAX_SHRINK_ATTEMPTS =>
                {
                    shrink_attempts += 1;
                    slice_ms = (slice_ms / 2).max(RETENTION_SWEEP_MIN_SLICE_MS);
                }
                Err(error) if error.kind() == io::ErrorKind::OutOfMemory => {
                    if selected.is_empty() && swept_through_ms == sweep_start_ms {
                        return Err(error);
                    }
                    break;
                }
                Err(error) => return Err(error),
            }
        }
    }
    let mut task_deletes = BTreeMap::<String, u64>::new();
    let mut type_deletes = BTreeMap::<(String, String), [u64; 4]>::new();
    // Authority-friendliness budget (2026-09-22 wedge-recycle incident): the
    // authority-contention watchdog recycles the daemon once a single hold
    // wedges the mutex past its threshold, and one catch-up sweep batch
    // against a FUSE-backed authority measured 45-57 s — every recycle
    // aborted the transaction and the sweep with it. The scan and delete
    // phases are therefore bounded by a hard internal budget far below the
    // wedge threshold and the caller's deadline; unprocessed rows stay in
    // the index for the next call, which is exactly the existing has_more
    // resumption. The post-delete reconciliation pass below stays within the
    // same envelope because the batch is bounded by `request.limit`. The maps
    // already carry precisely the processed rows, so a truncated batch commits
    // consistently.
    let mut truncated = false;
    let mut processed_rows = 0_usize;
    for (row_key, raw) in &selected {
        if truncated {
            break;
        }
        let retained = RetentionEntry::decode(raw)?;
        let expected_retention_key = retention_key(
            tenant,
            owner,
            request.retention_class,
            retained.index.created_at_ms,
            &retained.task_id,
            retained.index.application_sequence,
        )?;
        let primary_key = index_key(
            tenant,
            owner,
            &retained.task_id,
            retained.index.application_sequence,
        )?;
        if retention_class(&retained.event_type) != request.retention_class
            || retained.index.created_at_ms >= request.created_before_ms
            || row_key != &expected_retention_key
            || database.entity_get(transaction, &primary_key)?.as_deref()
                != Some(retained.index.encode().as_slice())
        {
            return Err(invalid_data("event retention index witness mismatch"));
        }
        database.entity_delete(transaction, row_key.clone())?;
        database.entity_delete(transaction, primary_key)?;
        database.entity_delete(
            transaction,
            payload_document_key(
                tenant,
                owner,
                &retained.task_id,
                retained.index.application_sequence,
            )?,
        )?;
        let physical_key = physical_index_key(
            tenant,
            owner,
            &retained.task_id,
            retained.index.physical_sequence,
        )?;
        database.entity_delete(transaction, physical_key)?;
        *task_deletes.entry(retained.task_id.clone()).or_default() += 1;
        if !retained.event_type.is_empty() {
            let typed_key = type_index_key(
                tenant,
                owner,
                &retained.task_id,
                &retained.event_type,
                retained.index.application_sequence,
            )?;
            let age_key = type_age_key(
                tenant,
                owner,
                &retained.task_id,
                &retained.event_type,
                retained.index.created_at_ms,
                retained.index.application_sequence,
            )?;
            let encoded_index = retained.index.encode();
            if database.entity_get(transaction, &typed_key)?.as_deref()
                != Some(encoded_index.as_slice())
                || database.entity_get(transaction, &age_key)?.as_deref()
                    != Some(encoded_index.as_slice())
            {
                return Err(invalid_data("event retention secondary index mismatch"));
            }
            database.entity_delete(transaction, typed_key)?;
            database.entity_delete(transaction, age_key)?;
            let counts = type_deletes
                .entry((retained.task_id.clone(), retained.event_type.clone()))
                .or_default();
            counts[0] += 1;
            counts[retained.kind_class as usize] += u64::from(retained.kind_class != 0);
        }
        processed_rows += 1;
        truncated = over_budget();
    }
    if truncated && processed_rows < selected.len() {
        // The batch ran out of its authority-friendliness budget with rows
        // unprocessed: roll the watermark
        // back to the first unprocessed row's timestamp so nothing below it is
        // ever claimed. Deleted rows below that point are gone from the
        // retention index, so the next call's inclusive rescan sees exactly
        // the unprocessed remainder. `truncated` only turns true after at
        // least one row is processed, so `selected[processed_rows]` exists.
        let retained = RetentionEntry::decode(&selected[processed_rows].1)?;
        swept_through_ms = retained.index.created_at_ms.min(swept_through_ms);
    }
    if cutoff > 0 && swept_through_ms > 0 && swept_through_ms != sweep_start_ms {
        database.entity_put(
            transaction,
            cursor_key,
            encode_retention_sweep_cursor(swept_through_ms, slice_ms)?,
        )?;
    }

    for (task_id, deleted) in task_deletes {
        let key = metadata_key(tenant, owner, &task_id)?;
        let current = database
            .entity_get(transaction, &key)?
            .ok_or_else(|| invalid_data("event retention metadata is missing"))?;
        let current = Metadata::decode(&current)?;
        let remaining = current
            .count
            .checked_sub(deleted)
            .ok_or_else(|| invalid_data("event retention count underflow"))?;
        if remaining == 0 {
            database.entity_delete(transaction, key)?;
        } else {
            let (range_start, range_end) = index_range(tenant, owner, &task_id, None)?;
            let minimum = database
                .entity_scan(transaction, &range_start, &range_end, 1)?
                .first()
                .map(|(key, _)| entry_sequence(key))
                .transpose()?
                .ok_or_else(|| invalid_data("retained event minimum is missing"))?;
            let maximum = database
                .entity_scan_reverse(transaction, &range_start, &range_end, 1)?
                .first()
                .map(|(key, _)| entry_sequence(key))
                .transpose()?
                .ok_or_else(|| invalid_data("retained event maximum is missing"))?;
            database.entity_put(
                transaction,
                key,
                Metadata {
                    count: remaining,
                    minimum,
                    maximum,
                }
                .encode(),
            )?;
        }
        let stream = stream_key(tenant, owner, &task_id)?;
        let retain_from_sequence = if remaining == 0 {
            database.transaction_stream_next_sequence(transaction, &stream)?
        } else {
            let (physical_start, physical_end) = physical_index_range(tenant, owner, &task_id)?;
            database
                .entity_scan(transaction, &physical_start, &physical_end, 1)?
                .first()
                .map(|(_, value)| IndexEntry::decode(value))
                .transpose()?
                .map(|entry| entry.physical_sequence)
                .ok_or_else(|| invalid_data("retained event physical minimum is missing"))?
        };
        let queue_key = retirement_queue_key(tenant, owner, &task_id)?;
        // Deletion cannot make the earliest surviving physical position move
        // backwards, so a newer target supersedes an unprocessed older target.
        // Avoiding a point read here preserves the 1,000-task prune budget.
        database.entity_put(
            transaction,
            queue_key,
            encode_retirement_queue_target(retain_from_sequence)?,
        )?;
    }

    for ((task_id, event_type), deleted) in type_deletes {
        let catalog_key = type_catalog_key(tenant, owner, &task_id, &event_type)?;
        let current = database
            .entity_get(transaction, &catalog_key)?
            .ok_or_else(|| invalid_data("event retention type metadata is missing"))?;
        let current = TypeMetadata::decode(&current)?;
        let count = current
            .count
            .checked_sub(deleted[0])
            .ok_or_else(|| invalid_data("event type count underflow"))?;
        if count == 0 {
            database.entity_delete(transaction, catalog_key)?;
            continue;
        }
        let (age_start, age_end) = type_age_range(tenant, owner, &task_id, &event_type)?;
        let first = database
            .entity_scan(transaction, &age_start, &age_end, 1)?
            .first()
            .map(|(_, value)| IndexEntry::decode(value))
            .transpose()?
            .ok_or_else(|| invalid_data("retained event type minimum is missing"))?;
        database.entity_put(
            transaction,
            catalog_key,
            TypeMetadata {
                count,
                first_created_at_ms: first.created_at_ms,
                request_count: current
                    .request_count
                    .checked_sub(deleted[1])
                    .ok_or_else(|| invalid_data("event request count underflow"))?,
                state_count: current
                    .state_count
                    .checked_sub(deleted[2])
                    .ok_or_else(|| invalid_data("event state count underflow"))?,
                legacy_count: current
                    .legacy_count
                    .checked_sub(deleted[3])
                    .ok_or_else(|| invalid_data("legacy event count underflow"))?,
            }
            .encode(),
        )?;
    }
    let (queue_start, queue_end) = retirement_queue_range(tenant, owner)?;
    let queued = database.entity_scan(transaction, &queue_start, &queue_end, 2)?;
    let mut physical_has_more = queued.len() > 1;
    if let Some((queue_key, encoded_target)) = queued.first() {
        let task_id = retirement_queue_task_id(queue_key)?;
        let target = decode_retirement_queue_target(encoded_target)?;
        let progress = database.stream_retire_prefix(
            transaction,
            &stream_key(tenant, owner, &task_id)?,
            target,
        )?;
        if progress.has_more {
            physical_has_more = true;
        } else if progress.retained_from_sequence == target {
            database.entity_delete(transaction, queue_key.clone())?;
        } else {
            return Err(invalid_data("stream retirement stopped before its target"));
        }
    }
    serde_json::to_vec(&json!({
        "deleted": processed_rows,
        "has_more": truncated
            || selected.len() >= request.limit
            || has_more_rows
            || swept_through_ms < sweep_ceiling
            || physical_has_more,
        "index_mode": "tier_partial_v2",
    }))
    .map_err(|_| invalid_data("event retention projection failed"))
}

pub(crate) fn append_batch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    requests: &[AppendRequest],
) -> io::Result<Vec<u8>> {
    if requests.is_empty() || requests.len() > 500 {
        return Err(invalid_input("indexed event batch is invalid"));
    }
    let mut results = Vec::with_capacity(requests.len());
    let mut inserted = 0_usize;
    for request in requests {
        let encoded = append(database, transaction, request)?;
        let result: Value = serde_json::from_slice(&encoded)
            .map_err(|_| invalid_data("indexed event append result is invalid"))?;
        inserted += usize::from(result["inserted"] == true);
        results.push(result);
    }
    serde_json::to_vec(&json!({
        "results": results,
        "inserted": inserted,
        "deduplicated": requests.len() - inserted,
        // Fenced appends report per-row rejections here; keep the key present
        // on the unfenced path so both storage authorities share one shape
        // (the SQL sidecar's `_event_append_batch` always emits it).
        "rejected": 0,
    }))
    .map_err(|_| invalid_data("indexed event batch projection failed"))
}

fn next_type_entry(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    event_type: &str,
    after_sequence: Option<u64>,
) -> io::Result<Option<IndexEntry>> {
    let (start, end) = type_index_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        task_id,
        event_type,
        after_sequence,
    )?;
    database
        .entity_scan(transaction, &start, &end, 1)?
        .into_iter()
        .next()
        .map(|(_, raw)| IndexEntry::decode(&raw))
        .transpose()
}

fn filtered_entries(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
) -> io::Result<Vec<(String, IndexEntry)>> {
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let mut event_types = request.types.iter().cloned().collect::<BTreeSet<_>>();
    for prefix in &request.type_prefixes {
        let (start, end, type_offset) =
            type_catalog_range(tenant, owner, &request.task_id, prefix)?;
        let rows = database.entity_scan(transaction, &start, &end, MAX_FILTER_EVENT_TYPES)?;
        // Without an unbounded look-ahead, reaching the catalog budget cannot
        // prove that every matching type was enumerated. Fail closed instead
        // of returning a silently incomplete page.
        if rows.len() == MAX_FILTER_EVENT_TYPES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "event type catalog exceeds 1,000 matching types",
            ));
        }
        for (key, value) in rows {
            TypeMetadata::decode(&value)?;
            let raw_type = key
                .key_bytes()
                .get(type_offset..)
                .ok_or_else(|| invalid_data("invalid event type catalog key"))?;
            let event_type = std::str::from_utf8(raw_type)
                .map_err(|_| invalid_data("event type catalog key is not UTF-8"))?;
            if event_type.is_empty() || !event_type.starts_with(prefix) {
                return Err(invalid_data("event type catalog key escaped its prefix"));
            }
            event_types.insert(event_type.to_owned());
        }
    }

    let mut cursors = event_types
        .into_iter()
        .map(|event_type| {
            let entry = next_type_entry(
                database,
                transaction,
                &request.task_id,
                &event_type,
                request.after_sequence,
            )?;
            Ok((event_type, entry))
        })
        .collect::<io::Result<Vec<_>>>()?;
    let mut selected = Vec::with_capacity(request.limit);
    while selected.len() < request.limit {
        let Some((cursor_index, _)) = cursors
            .iter()
            .enumerate()
            .filter_map(|(index, (_, entry))| entry.map(|entry| (index, entry)))
            .min_by_key(|(_, entry)| entry.application_sequence)
        else {
            break;
        };
        let (event_type, current) = &mut cursors[cursor_index];
        let entry = current
            .take()
            .ok_or_else(|| invalid_data("event type cursor disappeared"))?;
        selected.push((event_type.clone(), entry));
        *current = next_type_entry(
            database,
            transaction,
            &request.task_id,
            event_type,
            Some(entry.application_sequence),
        )?;
    }
    Ok(selected)
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
) -> io::Result<Vec<u8>> {
    if request.after_sequence == Some(u64::MAX) {
        return Ok(b"[]".to_vec());
    }
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let filtered = !request.types.is_empty() || !request.type_prefixes.is_empty();
    let typed_entries = if filtered {
        filtered_entries(database, transaction, request)?
    } else {
        let (start, end) = index_range(tenant, owner, &request.task_id, request.after_sequence)?;
        database
            .entity_scan(transaction, &start, &end, request.limit)?
            .into_iter()
            .map(|(_, raw)| IndexEntry::decode(&raw).map(|entry| (String::new(), entry)))
            .collect::<io::Result<Vec<_>>>()?
    };
    let key = stream_key(tenant, owner, &request.task_id)?;
    let positions = typed_entries
        .iter()
        .map(|(_, entry)| entry.physical_sequence)
        .collect::<BTreeSet<_>>();
    if positions.len() != typed_entries.len() {
        return Err(invalid_data("indexed events reuse a physical position"));
    }
    let events = if positions.is_empty() {
        Default::default()
    } else {
        database.stream_read_positions_in_transaction(transaction, &key, &positions)?
    };
    let mut output = Vec::from(b"[".as_slice());
    for (offset, (indexed_type, entry)) in typed_entries.into_iter().enumerate() {
        let event = events
            .get(&entry.physical_sequence)
            .ok_or_else(|| invalid_data("indexed event is missing"))?;
        let include_payload = request.payload_types.as_ref()
            .is_none_or(|types| types.contains(&event.event_type));
        let projected = if include_payload {
            let payload = materialize_event_payload(
                database, transaction, &request.task_id, entry, event)?;
            if filtered && logical_event_type_from_json(&payload)? != indexed_type {
                return Err(invalid_data("event type index witness mismatch"));
            }
            project_event(entry, &payload)?
        } else {
            if filtered && event.event_type != indexed_type {
                return Err(invalid_data("event type index witness mismatch"));
            }
            project_event_metadata(entry, event)?
        };
        if output.len() + projected.len() + usize::from(offset > 0) + 1
            > MAX_TRANSACTION_IR_LITERAL_BYTES
        {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "storage_payload_too_large: event page exceeds 8 MiB",
            ));
        }
        if offset > 0 {
            output.push(b',');
        }
        output.extend_from_slice(&projected);
    }
    output.push(b']');
    Ok(output)
}

fn merge_type_metadata(total: &mut TypeMetadata, value: TypeMetadata) -> io::Result<()> {
    total.count = total
        .count
        .checked_add(value.count)
        .ok_or_else(|| invalid_data("inspector event count overflow"))?;
    total.request_count = total
        .request_count
        .checked_add(value.request_count)
        .ok_or_else(|| invalid_data("inspector request count overflow"))?;
    total.state_count = total
        .state_count
        .checked_add(value.state_count)
        .ok_or_else(|| invalid_data("inspector state count overflow"))?;
    total.legacy_count = total
        .legacy_count
        .checked_add(value.legacy_count)
        .ok_or_else(|| invalid_data("inspector legacy count overflow"))?;
    total.first_created_at_ms = if total.first_created_at_ms == 0 {
        value.first_created_at_ms
    } else {
        total.first_created_at_ms.min(value.first_created_at_ms)
    };
    Ok(())
}

fn task_type_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<TypeMetadata> {
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let mut total = TypeMetadata::default();
    for event_type in STRUCTURAL_EVENT_TYPES {
        if let Some(raw) = database.entity_get(
            transaction,
            &type_catalog_key(tenant, owner, task_id, event_type)?,
        )? {
            merge_type_metadata(&mut total, TypeMetadata::decode(&raw)?)?;
        }
    }
    let (start, end, _) = type_catalog_range(tenant, owner, task_id, LEGACY_FLOW_EVENT_PREFIX)?;
    let legacy_flow_types =
        database.entity_scan(transaction, &start, &end, MAX_FILTER_EVENT_TYPES)?;
    if legacy_flow_types.len() == MAX_FILTER_EVENT_TYPES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "inspector legacy flow type catalog exceeds 1,000 entries",
        ));
    }
    for (_, raw) in legacy_flow_types {
        merge_type_metadata(&mut total, TypeMetadata::decode(&raw)?)?;
    }
    Ok(total)
}

pub(crate) fn inspector_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    root_task_ids: &[String],
) -> io::Result<Vec<u8>> {
    if root_task_ids.is_empty() || root_task_ids.len() > 100 {
        return Err(invalid_input("inspector task roots are invalid"));
    }
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let mut task_ids = BTreeSet::new();
    for root_task_id in root_task_ids {
        if database
            .entity_get(transaction, &metadata_key(tenant, owner, root_task_id)?)?
            .is_some()
        {
            task_ids.insert(root_task_id.clone());
        }
        let child_prefix = format!("{root_task_id}#agent:");
        let (start, end) = metadata_range(tenant, owner, &child_prefix)?;
        let children = database.entity_scan(transaction, &start, &end, MAX_INSPECTOR_TASKS)?;
        if children.len() == MAX_INSPECTOR_TASKS {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "inspector child task catalog reaches 1,000 entries",
            ));
        }
        for (key, raw) in children {
            Metadata::decode(&raw)?;
            let key_bytes = key.key_bytes();
            if key_bytes.first() != Some(&1) {
                return Err(invalid_data("inspector child task key has the wrong tag"));
            }
            let task_id = std::str::from_utf8(&key_bytes[1..])
                .map_err(|_| invalid_data("inspector child task id is not UTF-8"))?;
            if !task_id.starts_with(&child_prefix) || task_id.len() == child_prefix.len() {
                return Err(invalid_data("inspector child task escaped its prefix"));
            }
            task_ids.insert(task_id.to_owned());
            if task_ids.len() > MAX_INSPECTOR_TASKS {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "inspector task expansion exceeds 1,000 entries",
                ));
            }
        }
    }

    let mut records = Vec::new();
    for task_id in task_ids {
        let summary = task_type_summary(database, transaction, &task_id)?;
        if summary.count == 0 {
            continue;
        }
        records.push(json!({
            "task_id": task_id,
            "request_count": summary.request_count,
            "state_count": summary.state_count,
            "legacy_count": summary.legacy_count,
            "event_count": summary.count,
            "first_event_at_ms": summary.first_created_at_ms,
        }));
    }
    serde_json::to_vec(&json!({"records": records}))
        .map_err(|_| invalid_data("inspector summary projection failed"))
}

pub(crate) fn bounds(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    include_read_guard: bool,
    types: Option<&[String]>,
) -> io::Result<Vec<u8>> {
    let key = metadata_key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        task_id,
    )?;
    let value = database.entity_get(transaction, &key)?;
    let whole_count = value.as_ref().map(|raw| Metadata::decode(raw).map(|meta| meta.count))
        .transpose()?.unwrap_or(0);
    let mut result = match value {
        None => json!({"retained_count": 0, "base_cursor": 0, "next_cursor": 0}),
        Some(raw) => {
            let meta = Metadata::decode(&raw)?;
            json!({"retained_count": meta.count, "base_cursor": meta.minimum, "next_cursor": meta.maximum.checked_add(1).ok_or_else(|| invalid_data("event cursor overflow"))?})
        }
    };
    let scope = types.map(|types| types.iter().cloned().collect::<BTreeSet<_>>()
        .into_iter().collect::<Vec<_>>());
    if let Some(types) = &scope {
        if types.is_empty() || types.len() > crate::generated_storage_operations::EVENT_READ_GUARD_MAX_TYPES
            || types.iter().any(|value| value.is_empty() || value.chars().count() > 128) {
            return Err(invalid_input("invalid event guard types"));
        }
        let mut count = 0_u64;
        let mut minimum = u64::MAX;
        let mut maximum = 0_u64;
        for event_type in types {
            let type_key = type_catalog_key(transaction.tenant_id(), transaction.owner_user_id(), task_id, event_type)?;
            let Some(raw) = database.entity_get(transaction, &type_key)? else { continue; };
            let meta = TypeMetadata::decode(&raw)?;
            if meta.count == 0 { continue; }
            count = count.checked_add(meta.count).ok_or_else(|| invalid_data("event scope count overflow"))?;
            let (start, end) = type_index_range(transaction.tenant_id(), transaction.owner_user_id(), task_id, event_type, None)?;
            let first = database.entity_scan(transaction, &start, &end, 1)?;
            let last = database.entity_scan_reverse(transaction, &start, &end, 1)?;
            let first = first.first().ok_or_else(|| invalid_data("event scope minimum is missing"))?;
            let last = last.first().ok_or_else(|| invalid_data("event scope maximum is missing"))?;
            let first = IndexEntry::decode(&first.1)?.application_sequence;
            let last = IndexEntry::decode(&last.1)?.application_sequence;
            if first > last { return Err(invalid_data("event scope bounds are invalid")); }
            minimum = minimum.min(first);
            maximum = maximum.max(last);
        }
        if count > whole_count { return Err(invalid_data("event scope count exceeds source")); }
        result = if count == 0 {
            json!({"retained_count": 0, "base_cursor": 0, "next_cursor": 0})
        } else {
            json!({"retained_count": count, "base_cursor": minimum,
                "next_cursor": maximum.checked_add(1).ok_or_else(|| invalid_data("event cursor overflow"))?})
        };
    }
    if include_read_guard {
        // Appends cannot reuse a physical position, including after all-row
        // retirement. Retention alone strictly decreases the retained count.
        // Bind the stamp to this authority lease: reopen/restore cannot recycle
        // a previously admitted source identity; replicas share that epoch.
        let stream = stream_key(transaction.tenant_id(), transaction.owner_user_id(), task_id)?;
        let append_position = database.transaction_stream_next_sequence(transaction, &stream)?;
        let revision = if scope.is_some() {
            // Canonical appends consume exactly one physical position. A and
            // R both increase for unselected rows, so A-(R-S) stays stable.
            // Pruning unselected rows increases the revision; pruning selected
            // rows changes the scoped bounds, including empty/reinsert ABA.
            let selected_count = result["retained_count"].as_u64().unwrap();
            append_position.checked_sub(whole_count - selected_count)
                .ok_or_else(|| invalid_data("event scope append watermark is invalid"))?
        } else { append_position };
        let mut identity = json!([
            database.authority_uuid().to_string(), database.event_read_epoch().to_string(),
            transaction.tenant_id(), transaction.owner_user_id(), task_id,
            revision, result,
        ]);
        if let Some(types) = &scope { identity.as_array_mut().unwrap().push(json!(types)); }
        let identity = serde_json::to_vec(&identity)
            .map_err(|_| invalid_data("event read identity projection failed"))?;
        result["read_guard"] = json!({
            "contract": crate::generated_storage_operations::EVENT_READ_GUARD_CONTRACT,
            "stamp": blake3::hash(&identity).to_hex().to_string(),
        });
        if let Some(types) = &scope {
            result["read_guard"]["contract"] = json!(crate::generated_storage_operations::EVENT_READ_GUARD_SCOPED_CONTRACT);
            result["read_guard"]["types"] = json!(types);
        }
    }
    serde_json::to_vec(&result).map_err(|_| invalid_data("event bounds projection failed"))
}

pub(crate) fn latest(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    metadata_only: bool,
) -> io::Result<Option<Vec<u8>>> {
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let meta_key = metadata_key(tenant, owner, task_id)?;
    let Some(raw_meta) = database.entity_get(transaction, &meta_key)? else {
        return Ok(None);
    };
    let meta = Metadata::decode(&raw_meta)?;
    let raw = database
        .entity_get(
            transaction,
            &index_key(tenant, owner, task_id, meta.maximum)?,
        )?
        .ok_or_else(|| invalid_data("latest event index is missing"))?;
    let entry = IndexEntry::decode(&raw)?;
    let page = database.stream_read_in_transaction(
        transaction,
        &stream_key(tenant, owner, task_id)?,
        entry.physical_sequence,
        1,
    )?;
    let event = page
        .events
        .first()
        .filter(|event| event.sequence == entry.physical_sequence)
        .ok_or_else(|| invalid_data("latest indexed event is missing"))?;
    if metadata_only {
        return project_event_metadata(entry, &event.event).map(Some);
    }
    let payload = materialize_event_payload(database, transaction, task_id, entry, &event.event)?;
    project_event(entry, &payload).map(Some)
}

#[cfg(test)]
#[path = "indexed_stream/payload_tests.rs"]
mod payload_tests;

#[cfg(test)]
#[path = "indexed_stream/prune_tests.rs"]
mod prune_tests;
