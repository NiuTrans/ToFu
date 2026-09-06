//! Sparse natural-key index over a continuous immutable physical stream.
//!
//! Agent events use caller-assigned sparse sequence numbers. The entity index
//! provides natural-key idempotency, ordered range reads and exact bounds;
//! each index entry points at one continuous physical stream position. Both
//! families are changed by the same Authority transaction.

use std::collections::{BTreeMap, BTreeSet};
use std::io;

use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES;
use crate::stream::{StreamEvent, StreamKey};

const INDEX_MAGIC: &[u8; 8] = b"TDBIDX01";
const META_MAGIC: &[u8; 8] = b"TDBIMT01";
const TYPE_META_MAGIC: &[u8; 8] = b"TDBTYP01";
const VERSION: u32 = 1;
const INDEX_NAMESPACE: &str = "task_event_index";
const STREAM_DOMAIN: &str = "task_event";
const TYPE_INDEX_TAG: u8 = 2;
const TYPE_CATALOG_TAG: u8 = 3;
const RETENTION_INDEX_TAG: u8 = 4;
const TYPE_AGE_INDEX_TAG: u8 = 5;
const PHYSICAL_INDEX_TAG: u8 = 6;
const RETIREMENT_QUEUE_TAG: u8 = 7;
const RETIREMENT_QUEUE_MAGIC: &[u8; 8] = b"TDBSRQ01";
const RETENTION_MAGIC: &[u8; 8] = b"TDBRET01";
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

fn retention_range(
    tenant_id: u64,
    owner_user_id: u64,
    class: RetentionClass,
    cutoff: i64,
) -> io::Result<(EntityKey, EntityKey)> {
    let prefix = vec![RETENTION_INDEX_TAG, class.tag()];
    let start = EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &prefix)?;
    let mut end = prefix;
    end.extend_from_slice(&(cutoff as u64).to_be_bytes());
    Ok((
        start,
        EntityKey::new(tenant_id, owner_user_id, INDEX_NAMESPACE, &end)?,
    ))
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

fn project_event(entry: IndexEntry, event: &StreamEvent) -> io::Result<Vec<u8>> {
    if event.created_at_ms != entry.created_at_ms
        || blake3::hash(&event.payload).as_bytes() != &entry.payload_digest
    {
        return Err(invalid_data("indexed event witness mismatch"));
    }
    let value: Value = serde_json::from_slice(&event.payload)
        .map_err(|_| invalid_data("indexed event JSON is invalid"))?;
    serde_json::to_vec(&json!({"sequence": entry.application_sequence, "event": value, "created_at_ms": entry.created_at_ms}))
        .map_err(|_| invalid_data("indexed event projection failed"))
}

fn logical_event_type(event: &StreamEvent) -> io::Result<String> {
    logical_event_type_from_json(&event.payload)
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
    database.stream_append(
        transaction,
        stream_key,
        physical_sequence,
        vec![StreamEvent::new(
            request.created_at_ms,
            &request.event_type,
            request.payload_json.clone(),
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
) -> io::Result<Vec<u8>> {
    if request.created_before_ms < 0 || request.limit == 0 || request.limit > 1_000 {
        return Err(invalid_input("event retention request is invalid"));
    }
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let selected = if request.created_before_ms == 0 {
        Vec::new()
    } else {
        let (start, end) = retention_range(
            tenant,
            owner,
            request.retention_class,
            request.created_before_ms,
        )?;
        database.entity_scan(transaction, &start, &end, request.limit)?
    };
    let mut task_deletes = BTreeMap::<String, u64>::new();
    let mut type_deletes = BTreeMap::<(String, String), [u64; 4]>::new();
    for (row_key, raw) in &selected {
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
        "deleted": selected.len(),
        "has_more": selected.len() >= request.limit || physical_has_more,
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
        if filtered && logical_event_type(event)? != indexed_type {
            return Err(invalid_data("event type index witness mismatch"));
        }
        let projected = project_event(entry, event)?;
        if output.len() + projected.len() + usize::from(offset > 0) + 1
            > MAX_TRANSACTION_IR_LITERAL_BYTES
        {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "event page exceeds 8 MiB",
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
) -> io::Result<Vec<u8>> {
    let key = metadata_key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        task_id,
    )?;
    let value = database.entity_get(transaction, &key)?;
    let result = match value {
        None => json!({"retained_count": 0, "base_cursor": 0, "next_cursor": 0}),
        Some(raw) => {
            let meta = Metadata::decode(&raw)?;
            json!({"retained_count": meta.count, "base_cursor": meta.minimum, "next_cursor": meta.maximum.checked_add(1).ok_or_else(|| invalid_data("event cursor overflow"))?})
        }
    };
    serde_json::to_vec(&result).map_err(|_| invalid_data("event bounds projection failed"))
}

pub(crate) fn latest(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
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
    project_event(entry, &event.event).map(Some)
}
