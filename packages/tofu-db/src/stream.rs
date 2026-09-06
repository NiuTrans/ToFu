//! Owner-scoped immutable stream segments with atomic append and bounded paging.

use std::collections::{BTreeMap, BTreeSet};
use std::io;

use crate::block::BlockId;
use crate::engine::Engine;
use crate::entity::{EntityDatabase, EntityKey, EntityTransaction};
use crate::generated_tofudb_ir::{
    MAX_STREAM_APPEND_BYTES, MAX_STREAM_APPEND_EVENTS, MAX_STREAM_EVENT_BYTES,
    MAX_STREAM_READ_BYTES, MAX_STREAM_READ_EVENTS,
};
use crate::transaction::{decode_family_records, FamilyRecordKind};

const SEGMENT_MAGIC: &[u8; 8] = b"TDBSEG01";
const COMMIT_MAGIC: &[u8; 8] = b"TDBSCM01";
const VERSION: u32 = 1;
const SEGMENT_FIXED_BYTES: usize = 8 + 4 + 8 + 8 + 1 + 2 + 1 + 8 + 4;
const COMMIT_FIXED_BYTES: usize = 8 + 4 + 8 + 8 + 1 + 2 + 1 + 8 + 4 + 2 + 2;
const COMMIT_SEGMENT_BYTES: usize = 32 + 4 + 8 + 8;
const EVENT_FIXED_BYTES: usize = 8 + 1 + 4;
const MAX_DOMAIN_BYTES: usize = 63;
const MAX_STREAM_ID_BYTES: usize = 512;
const MAX_EVENT_TYPE_BYTES: usize = 63;
const SEGMENT_TARGET_BYTES: usize = 2 * 1024 * 1024;
const CATALOG_NAMESPACE: &str = "stream_catalog";
const CATALOG_META_MAGIC: &[u8; 8] = b"TDBSMT01";
const CATALOG_SEGMENT_MAGIC: &[u8; 8] = b"TDBSIX01";
const CATALOG_METADATA_VERSION: u32 = 2;
pub(crate) const MAX_STREAM_SEGMENT_RETIREMENTS_PER_CALL: usize = 128;

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct StreamKey {
    tenant_id: u64,
    owner_user_id: u64,
    domain: String,
    stream_id: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StreamEvent {
    pub created_at_ms: i64,
    pub event_type: String,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SequencedEvent {
    pub sequence: u64,
    pub event: StreamEvent,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StreamPage {
    pub events: Vec<SequencedEvent>,
    pub next_sequence: u64,
    pub end_of_stream: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StreamAppendResult {
    pub transaction_sequence: u64,
    pub first_sequence: u64,
    pub last_sequence: u64,
    pub segment_count: u16,
}

pub(crate) struct PreparedStreamAppend {
    expected_engine_sequence: u64,
    key: StreamKey,
    first_sequence: u64,
    last_sequence: u64,
    event_count: u32,
    segment_count: u16,
    segments: Vec<SegmentReference>,
    block_ids: Vec<BlockId>,
    commit_record: Vec<u8>,
}

impl PreparedStreamAppend {
    pub(crate) fn block_ids(&self) -> &[BlockId] {
        &self.block_ids
    }

    pub(crate) fn commit_record(&self) -> &[u8] {
        &self.commit_record
    }

    pub(crate) fn committed_result(&self, committed_sequence: u64) -> StreamAppendResult {
        StreamAppendResult {
            transaction_sequence: committed_sequence,
            first_sequence: self.first_sequence,
            last_sequence: self.last_sequence,
            segment_count: self.segment_count,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SegmentReference {
    block_id: BlockId,
    event_count: u32,
    minimum_created_at_ms: i64,
    maximum_created_at_ms: i64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct CatalogMetadata {
    retained_from_sequence: u64,
    end_sequence: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct StreamRetirementProgress {
    pub retired_segments: usize,
    pub retained_from_sequence: u64,
    pub has_more: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StreamCommit {
    key: StreamKey,
    first_sequence: u64,
    event_count: u32,
    segments: Vec<SegmentReference>,
}

#[derive(Default)]
pub struct StreamCatalog {
    streams: BTreeMap<StreamKey, Vec<SegmentReference>>,
    event_counts: BTreeMap<StreamKey, u64>,
    observed_engine_sequence: u64,
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

fn take<'a>(bytes: &'a [u8], offset: &mut usize, length: usize) -> io::Result<&'a [u8]> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| invalid_data("stream offset overflow"))?;
    let value = bytes
        .get(*offset..end)
        .ok_or_else(|| invalid_data("truncated stream record"))?;
    *offset = end;
    Ok(value)
}

fn ensure_scope(tenant_id: u64, owner_user_id: u64, key: &StreamKey) -> io::Result<()> {
    if tenant_id != key.tenant_id || owner_user_id != key.owner_user_id {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "stream is outside the requested owner scope",
        ));
    }
    Ok(())
}

impl StreamKey {
    pub fn new(
        tenant_id: u64,
        owner_user_id: u64,
        domain: &str,
        stream_id: &[u8],
    ) -> io::Result<Self> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input(
                "tenant and owner identities must be positive",
            ));
        }
        if domain.is_empty()
            || domain.len() > MAX_DOMAIN_BYTES
            || stream_id.is_empty()
            || stream_id.len() > MAX_STREAM_ID_BYTES
        {
            return Err(invalid_input("stream domain or identity exceeds its bound"));
        }
        Ok(Self {
            tenant_id,
            owner_user_id,
            domain: domain.to_owned(),
            stream_id: stream_id.to_vec(),
        })
    }
}

fn catalog_identity(key: &StreamKey) -> io::Result<Vec<u8>> {
    let stream_id_length = u16::try_from(key.stream_id.len())
        .map_err(|_| invalid_input("stream identity is too long"))?;
    let mut encoded = Vec::with_capacity(1 + key.domain.len() + 2 + key.stream_id.len());
    encoded.push(key.domain.len() as u8);
    encoded.extend_from_slice(key.domain.as_bytes());
    encoded.extend_from_slice(&stream_id_length.to_be_bytes());
    encoded.extend_from_slice(&key.stream_id);
    Ok(encoded)
}

pub(crate) fn catalog_metadata_key(key: &StreamKey) -> io::Result<EntityKey> {
    let mut raw = vec![0];
    raw.extend_from_slice(&catalog_identity(key)?);
    EntityKey::new(key.tenant_id, key.owner_user_id, CATALOG_NAMESPACE, &raw)
}

fn catalog_segment_key(key: &StreamKey, first_sequence: u64) -> io::Result<EntityKey> {
    if first_sequence == 0 {
        return Err(invalid_input("stream catalog segment sequence is zero"));
    }
    let mut raw = vec![1];
    raw.extend_from_slice(&catalog_identity(key)?);
    raw.extend_from_slice(&first_sequence.to_be_bytes());
    EntityKey::new(key.tenant_id, key.owner_user_id, CATALOG_NAMESPACE, &raw)
}

fn catalog_segment_range(key: &StreamKey) -> io::Result<(EntityKey, EntityKey)> {
    let mut raw = vec![1];
    raw.extend_from_slice(&catalog_identity(key)?);
    EntityKey::prefix_range(key.tenant_id, key.owner_user_id, CATALOG_NAMESPACE, &raw)
}

fn catalog_segment_first_sequence(key: &StreamKey, entity_key: &EntityKey) -> io::Result<u64> {
    let mut expected_prefix = vec![1];
    expected_prefix.extend_from_slice(&catalog_identity(key)?);
    let raw = entity_key.key_bytes();
    if raw.len() != expected_prefix.len() + 8 || !raw.starts_with(&expected_prefix) {
        return Err(invalid_data("persisted stream segment key is invalid"));
    }
    Ok(u64::from_be_bytes(
        raw[expected_prefix.len()..].try_into().unwrap(),
    ))
}

fn encode_catalog_metadata(metadata: CatalogMetadata) -> io::Result<Vec<u8>> {
    if metadata.end_sequence == 0
        || metadata.retained_from_sequence == 0
        || metadata.retained_from_sequence > metadata.end_sequence.saturating_add(1)
    {
        return Err(invalid_input("stream catalog metadata bounds are invalid"));
    }
    let mut encoded = Vec::with_capacity(28);
    encoded.extend_from_slice(CATALOG_META_MAGIC);
    encoded.extend_from_slice(&CATALOG_METADATA_VERSION.to_le_bytes());
    encoded.extend_from_slice(&metadata.retained_from_sequence.to_le_bytes());
    encoded.extend_from_slice(&metadata.end_sequence.to_le_bytes());
    Ok(encoded)
}

fn decode_catalog_metadata(encoded: &[u8]) -> io::Result<CatalogMetadata> {
    if encoded.len() < 12 || &encoded[..8] != CATALOG_META_MAGIC {
        return Err(invalid_data("invalid persisted stream metadata"));
    }
    let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
    let metadata = match (version, encoded.len()) {
        (VERSION, 20) => CatalogMetadata {
            retained_from_sequence: 1,
            end_sequence: u64::from_le_bytes(encoded[12..20].try_into().unwrap()),
        },
        (CATALOG_METADATA_VERSION, 28) => CatalogMetadata {
            retained_from_sequence: u64::from_le_bytes(encoded[12..20].try_into().unwrap()),
            end_sequence: u64::from_le_bytes(encoded[20..28].try_into().unwrap()),
        },
        _ => return Err(invalid_data("invalid persisted stream metadata")),
    };
    if metadata.end_sequence == 0
        || metadata.retained_from_sequence == 0
        || metadata.retained_from_sequence > metadata.end_sequence.saturating_add(1)
    {
        return Err(invalid_data("persisted stream metadata bounds are invalid"));
    }
    Ok(metadata)
}

fn encode_catalog_segment(reference: &SegmentReference) -> Vec<u8> {
    let mut encoded = Vec::with_capacity(64);
    encoded.extend_from_slice(CATALOG_SEGMENT_MAGIC);
    encoded.extend_from_slice(&VERSION.to_le_bytes());
    encoded.extend_from_slice(&reference.block_id.0);
    encoded.extend_from_slice(&reference.event_count.to_le_bytes());
    encoded.extend_from_slice(&reference.minimum_created_at_ms.to_le_bytes());
    encoded.extend_from_slice(&reference.maximum_created_at_ms.to_le_bytes());
    encoded
}

fn decode_catalog_segment(encoded: &[u8]) -> io::Result<SegmentReference> {
    if encoded.len() != 64
        || &encoded[..8] != CATALOG_SEGMENT_MAGIC
        || u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != VERSION
    {
        return Err(invalid_data("invalid persisted stream segment"));
    }
    let reference = SegmentReference {
        block_id: BlockId(encoded[12..44].try_into().unwrap()),
        event_count: u32::from_le_bytes(encoded[44..48].try_into().unwrap()),
        minimum_created_at_ms: i64::from_le_bytes(encoded[48..56].try_into().unwrap()),
        maximum_created_at_ms: i64::from_le_bytes(encoded[56..64].try_into().unwrap()),
    };
    if reference.event_count == 0
        || reference.event_count as usize > MAX_STREAM_APPEND_EVENTS
        || reference.minimum_created_at_ms > reference.maximum_created_at_ms
    {
        return Err(invalid_data("persisted stream segment metadata is invalid"));
    }
    Ok(reference)
}

pub(crate) fn stored_segment_block_reference(
    key: &EntityKey,
    stored: &[u8],
) -> io::Result<Option<BlockId>> {
    if key.namespace() != CATALOG_NAMESPACE {
        return Ok(None);
    }
    match key.key_bytes().first() {
        Some(0) => {
            decode_catalog_metadata(stored)?;
            Ok(None)
        }
        Some(1) => Ok(Some(decode_catalog_segment(stored)?.block_id)),
        _ => Err(invalid_data("persisted stream catalog key is invalid")),
    }
}

pub(crate) fn persisted_event_count(
    entities: &EntityDatabase,
    transaction: &mut EntityTransaction,
    key: &StreamKey,
) -> io::Result<u64> {
    entities
        .get(transaction, &catalog_metadata_key(key)?)?
        .as_deref()
        .map(decode_catalog_metadata)
        .transpose()
        .map(|value| value.map_or(0, |metadata| metadata.end_sequence))
}

fn persisted_sequence_bounds(
    entities: &EntityDatabase,
    transaction: &mut EntityTransaction,
    key: &StreamKey,
) -> io::Result<(u64, u64)> {
    entities
        .get(transaction, &catalog_metadata_key(key)?)?
        .as_deref()
        .map(decode_catalog_metadata)
        .transpose()
        .map(|value| {
            value.map_or((1, 0), |metadata| {
                (metadata.retained_from_sequence, metadata.end_sequence)
            })
        })
}

pub(crate) fn retire_persisted_prefix(
    entities: &EntityDatabase,
    transaction: &mut EntityTransaction,
    key: &StreamKey,
    retain_from_sequence: u64,
) -> io::Result<StreamRetirementProgress> {
    let (current_base, stream_end) = persisted_sequence_bounds(entities, transaction, key)?;
    if stream_end == 0
        || retain_from_sequence < current_base
        || retain_from_sequence > stream_end.saturating_add(1)
    {
        return Err(invalid_input(
            "stream retirement cursor is outside retained bounds",
        ));
    }
    if retain_from_sequence == current_base {
        return Ok(StreamRetirementProgress {
            retired_segments: 0,
            retained_from_sequence: current_base,
            has_more: false,
        });
    }
    let (start, end) = catalog_segment_range(key)?;
    let rows = entities.scan(
        transaction,
        &start,
        &end,
        MAX_STREAM_SEGMENT_RETIREMENTS_PER_CALL,
    )?;
    let mut retired_segments = 0;
    let mut next_base = current_base;
    let mut has_more = false;
    for (entity_key, encoded) in rows {
        let first = catalog_segment_first_sequence(key, &entity_key)?;
        let reference = decode_catalog_segment(&encoded)?;
        let segment_end = first
            .checked_add(reference.event_count as u64 - 1)
            .ok_or_else(|| invalid_data("stream catalog sequence overflow"))?;
        if segment_end < current_base {
            return Err(invalid_data(
                "stream catalog contains a fully retired segment",
            ));
        }
        if first > next_base {
            return Err(invalid_data(
                "stream catalog segment sequence is not contiguous",
            ));
        }
        if segment_end >= retain_from_sequence {
            next_base = retain_from_sequence;
            break;
        }
        if retired_segments == MAX_STREAM_SEGMENT_RETIREMENTS_PER_CALL {
            has_more = true;
            break;
        }
        entities.delete(transaction, entity_key)?;
        retired_segments += 1;
        next_base = segment_end
            .checked_add(1)
            .ok_or_else(|| invalid_data("stream catalog sequence overflow"))?;
    }
    if next_base < retain_from_sequence && !has_more {
        if next_base != stream_end.saturating_add(1) {
            has_more = true;
        } else {
            next_base = retain_from_sequence;
        }
    }
    entities.put(
        transaction,
        catalog_metadata_key(key)?,
        encode_catalog_metadata(CatalogMetadata {
            retained_from_sequence: next_base,
            end_sequence: stream_end,
        })?,
    )?;
    Ok(StreamRetirementProgress {
        retired_segments,
        retained_from_sequence: next_base,
        has_more,
    })
}

fn persisted_segment_at_or_before(
    entities: &EntityDatabase,
    transaction: &mut EntityTransaction,
    key: &StreamKey,
    sequence: u64,
) -> io::Result<Option<(u64, SegmentReference)>> {
    let (start, stream_end) = catalog_segment_range(key)?;
    let end = match sequence.checked_add(1) {
        Some(next) => catalog_segment_key(key, next)?,
        None => stream_end,
    };
    entities
        .scan_reverse(transaction, &start, &end, 1)?
        .into_iter()
        .next()
        .map(|(entity_key, encoded)| {
            Ok((
                catalog_segment_first_sequence(key, &entity_key)?,
                decode_catalog_segment(&encoded)?,
            ))
        })
        .transpose()
}

pub(crate) fn read_persisted(
    engine: &Engine,
    entities: &EntityDatabase,
    transaction: &mut EntityTransaction,
    key: &StreamKey,
    from_sequence: u64,
    limit: usize,
) -> io::Result<StreamPage> {
    if from_sequence == 0 || limit == 0 || limit > MAX_STREAM_READ_EVENTS {
        return Err(invalid_input("stream page request is invalid or unbounded"));
    }
    let (retained_from_sequence, stream_end) =
        persisted_sequence_bounds(entities, transaction, key)?;
    if from_sequence < retained_from_sequence {
        return Err(invalid_input("stream cursor refers to retired history"));
    }
    if from_sequence > stream_end.saturating_add(1) {
        return Err(invalid_input("stream cursor is beyond the current end"));
    }
    if from_sequence == stream_end.saturating_add(1) {
        return Ok(StreamPage {
            events: Vec::new(),
            next_sequence: from_sequence,
            end_of_stream: true,
        });
    }
    let (first_segment_sequence, first_reference) =
        persisted_segment_at_or_before(entities, transaction, key, from_sequence)?
            .ok_or_else(|| invalid_data("persisted stream has no first segment"))?;
    let first_segment_end = first_segment_sequence
        .checked_add(first_reference.event_count as u64 - 1)
        .ok_or_else(|| invalid_data("persisted stream segment sequence overflow"))?;
    if from_sequence > first_segment_end {
        return Err(invalid_data("persisted stream segment sequence has a gap"));
    }
    let (_, range_end) = catalog_segment_range(key)?;
    let range_start = catalog_segment_key(key, first_segment_sequence)?;
    let rows = entities.scan(
        transaction,
        &range_start,
        &range_end,
        MAX_STREAM_READ_EVENTS,
    )?;
    let mut events = Vec::new();
    let mut returned_bytes = 0_usize;
    let mut expected_segment_sequence = first_segment_sequence;
    for (entity_key, encoded) in rows {
        let segment_sequence = catalog_segment_first_sequence(key, &entity_key)?;
        let reference = decode_catalog_segment(&encoded)?;
        if segment_sequence != expected_segment_sequence {
            return Err(invalid_data(
                "persisted stream segment sequence is not contiguous",
            ));
        }
        let decoded = decode_segment(
            &engine.read_block(reference.block_id)?,
            key,
            segment_sequence,
        )?;
        if decoded.len() != reference.event_count as usize
            || decoded.iter().map(|event| event.created_at_ms).min()
                != Some(reference.minimum_created_at_ms)
            || decoded.iter().map(|event| event.created_at_ms).max()
                != Some(reference.maximum_created_at_ms)
        {
            return Err(invalid_data("persisted stream segment witness mismatch"));
        }
        for (offset, event) in decoded.into_iter().enumerate() {
            let sequence = segment_sequence + offset as u64;
            if sequence < from_sequence {
                continue;
            }
            let event_bytes = event.encoded_len();
            if events.len() == limit
                || (!events.is_empty() && returned_bytes + event_bytes > MAX_STREAM_READ_BYTES)
            {
                let next_sequence = events
                    .last()
                    .map_or(from_sequence, |last: &SequencedEvent| last.sequence + 1);
                return Ok(StreamPage {
                    events,
                    next_sequence,
                    end_of_stream: false,
                });
            }
            returned_bytes += event_bytes;
            events.push(SequencedEvent { sequence, event });
        }
        expected_segment_sequence = segment_sequence
            .checked_add(reference.event_count as u64)
            .ok_or_else(|| invalid_data("persisted stream segment sequence overflow"))?;
        if expected_segment_sequence > stream_end {
            break;
        }
    }
    let next_sequence = events
        .last()
        .map_or(from_sequence, |event| event.sequence + 1);
    if next_sequence <= stream_end {
        return Err(invalid_data(
            "persisted stream directory ended before its cursor",
        ));
    }
    Ok(StreamPage {
        events,
        next_sequence,
        end_of_stream: true,
    })
}

pub(crate) fn read_persisted_positions(
    engine: &Engine,
    entities: &EntityDatabase,
    transaction: &mut EntityTransaction,
    key: &StreamKey,
    positions: &BTreeSet<u64>,
) -> io::Result<BTreeMap<u64, StreamEvent>> {
    let (retained_from_sequence, stream_end) =
        persisted_sequence_bounds(entities, transaction, key)?;
    if positions.is_empty()
        || positions.len() > MAX_STREAM_READ_EVENTS
        || positions.first() == Some(&0)
        || positions
            .first()
            .is_some_and(|position| *position < retained_from_sequence)
        || positions
            .last()
            .is_some_and(|position| *position > stream_end)
    {
        return Err(invalid_input("stream point-read positions are invalid"));
    }
    let mut references = BTreeMap::<u64, SegmentReference>::new();
    for position in positions {
        if references
            .range(..=*position)
            .next_back()
            .is_some_and(|(first, reference)| {
                first.saturating_add(reference.event_count as u64) > *position
            })
        {
            continue;
        }
        let (first, reference) =
            persisted_segment_at_or_before(entities, transaction, key, *position)?
                .ok_or_else(|| invalid_data("persisted stream point has no segment"))?;
        let end = first
            .checked_add(reference.event_count as u64 - 1)
            .ok_or_else(|| invalid_data("persisted stream segment sequence overflow"))?;
        if *position > end {
            return Err(invalid_data(
                "persisted stream point falls in a segment gap",
            ));
        }
        references.insert(first, reference);
    }
    let mut selected = BTreeMap::new();
    let mut selected_bytes = 0_usize;
    for (first, reference) in references {
        let decoded = decode_segment(&engine.read_block(reference.block_id)?, key, first)?;
        if decoded.len() != reference.event_count as usize
            || decoded.iter().map(|event| event.created_at_ms).min()
                != Some(reference.minimum_created_at_ms)
            || decoded.iter().map(|event| event.created_at_ms).max()
                != Some(reference.maximum_created_at_ms)
        {
            return Err(invalid_data("persisted stream segment witness mismatch"));
        }
        let end = first
            .checked_add(reference.event_count as u64 - 1)
            .ok_or_else(|| invalid_data("persisted stream segment sequence overflow"))?;
        for position in positions.range(first..=end) {
            let event = decoded[(*position - first) as usize].clone();
            selected_bytes = selected_bytes
                .checked_add(event.encoded_len())
                .filter(|bytes| *bytes <= MAX_STREAM_READ_BYTES)
                .ok_or_else(|| invalid_input("stream point-read exceeds 8 MiB"))?;
            selected.insert(*position, event);
        }
    }
    if selected.len() != positions.len() {
        return Err(invalid_data(
            "persisted stream point-read position is missing",
        ));
    }
    Ok(selected)
}

pub(crate) fn prepare_persisted_append(
    engine: &Engine,
    key: &StreamKey,
    committed_event_count: u64,
    expected_next_sequence: u64,
    events: &[StreamEvent],
) -> io::Result<PreparedStreamAppend> {
    let actual_next_sequence = committed_event_count
        .checked_add(1)
        .ok_or_else(|| invalid_data("stream sequence overflow"))?;
    if expected_next_sequence != actual_next_sequence {
        return Err(conflict("stream expected-position witness changed"));
    }
    if events.is_empty() || events.len() > MAX_STREAM_APPEND_EVENTS {
        return Err(invalid_input("stream append event count is invalid"));
    }
    let append_bytes = events.iter().try_fold(0_usize, |total, event| {
        event.validate()?;
        total
            .checked_add(event.encoded_len())
            .ok_or_else(|| invalid_input("stream append byte count overflow"))
    })?;
    if append_bytes > MAX_STREAM_APPEND_BYTES {
        return Err(invalid_input("stream append exceeds 8 MiB"));
    }
    actual_next_sequence
        .checked_add(events.len() as u64)
        .ok_or_else(|| invalid_input("stream append would exhaust its cursor space"))?;
    let mut segments = Vec::new();
    let mut start = 0;
    let mut first_sequence = actual_next_sequence;
    while start < events.len() {
        let mut used = segment_header_len(key);
        let mut end = start;
        while end < events.len() && used + events[end].encoded_len() <= SEGMENT_TARGET_BYTES {
            used += events[end].encoded_len();
            end += 1;
        }
        if end == start {
            return Err(invalid_input("stream event cannot fit in one segment"));
        }
        let segment_events = &events[start..end];
        let block_id = engine.write_block(&encode_segment(key, first_sequence, segment_events)?)?;
        segments.push(SegmentReference {
            block_id,
            event_count: segment_events.len() as u32,
            minimum_created_at_ms: segment_events
                .iter()
                .map(|event| event.created_at_ms)
                .min()
                .unwrap(),
            maximum_created_at_ms: segment_events
                .iter()
                .map(|event| event.created_at_ms)
                .max()
                .unwrap(),
        });
        first_sequence = first_sequence
            .checked_add(segment_events.len() as u64)
            .ok_or_else(|| invalid_data("stream sequence overflow"))?;
        start = end;
    }
    let event_count = events.len() as u32;
    let last_sequence = actual_next_sequence
        .checked_add(event_count as u64 - 1)
        .ok_or_else(|| invalid_input("stream append would exhaust its cursor space"))?;
    let segment_count = u16::try_from(segments.len())
        .map_err(|_| invalid_input("stream append creates too many segments"))?;
    let commit = StreamCommit {
        key: key.clone(),
        first_sequence: actual_next_sequence,
        event_count,
        segments: segments.clone(),
    };
    let block_ids = segments.iter().map(|segment| segment.block_id).collect();
    Ok(PreparedStreamAppend {
        expected_engine_sequence: engine.state().durable_sequence,
        key: key.clone(),
        first_sequence: actual_next_sequence,
        last_sequence,
        event_count,
        segment_count,
        segments,
        block_ids,
        commit_record: commit.encode()?,
    })
}

impl StreamEvent {
    pub fn new(created_at_ms: i64, event_type: &str, payload: Vec<u8>) -> io::Result<Self> {
        let event = Self {
            created_at_ms,
            event_type: event_type.to_owned(),
            payload,
        };
        event.validate()?;
        Ok(event)
    }

    fn validate(&self) -> io::Result<()> {
        if self.event_type.is_empty()
            || self.event_type.len() > MAX_EVENT_TYPE_BYTES
            || self.payload.len() > MAX_STREAM_EVENT_BYTES
        {
            return Err(invalid_input(
                "stream event type or payload exceeds its bound",
            ));
        }
        Ok(())
    }

    pub(crate) fn encoded_len(&self) -> usize {
        EVENT_FIXED_BYTES + self.event_type.len() + self.payload.len()
    }
}

fn segment_header_len(key: &StreamKey) -> usize {
    SEGMENT_FIXED_BYTES + key.domain.len() + key.stream_id.len()
}

fn encode_segment(
    key: &StreamKey,
    first_sequence: u64,
    events: &[StreamEvent],
) -> io::Result<Vec<u8>> {
    if events.is_empty() || events.len() > MAX_STREAM_APPEND_EVENTS {
        return Err(invalid_input("stream segment event count is invalid"));
    }
    for event in events {
        event.validate()?;
    }
    let capacity =
        segment_header_len(key) + events.iter().map(StreamEvent::encoded_len).sum::<usize>();
    if capacity > SEGMENT_TARGET_BYTES {
        return Err(invalid_input("stream segment exceeds 2 MiB"));
    }
    let mut encoded = Vec::with_capacity(capacity);
    encoded.extend_from_slice(SEGMENT_MAGIC);
    encoded.extend_from_slice(&VERSION.to_le_bytes());
    encoded.extend_from_slice(&key.tenant_id.to_le_bytes());
    encoded.extend_from_slice(&key.owner_user_id.to_le_bytes());
    encoded.push(key.domain.len() as u8);
    encoded.extend_from_slice(&(key.stream_id.len() as u16).to_le_bytes());
    encoded.push(0);
    encoded.extend_from_slice(&first_sequence.to_le_bytes());
    encoded.extend_from_slice(&(events.len() as u32).to_le_bytes());
    encoded.extend_from_slice(key.domain.as_bytes());
    encoded.extend_from_slice(&key.stream_id);
    for event in events {
        encoded.extend_from_slice(&event.created_at_ms.to_le_bytes());
        encoded.push(event.event_type.len() as u8);
        encoded.extend_from_slice(&(event.payload.len() as u32).to_le_bytes());
        encoded.extend_from_slice(event.event_type.as_bytes());
        encoded.extend_from_slice(&event.payload);
    }
    Ok(encoded)
}

fn decode_segment(
    encoded: &[u8],
    expected_key: &StreamKey,
    expected_first_sequence: u64,
) -> io::Result<Vec<StreamEvent>> {
    if encoded.len() < SEGMENT_FIXED_BYTES
        || encoded.len() > SEGMENT_TARGET_BYTES
        || &encoded[..8] != SEGMENT_MAGIC
    {
        return Err(invalid_data("stream segment magic or size mismatch"));
    }
    let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
    let tenant_id = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
    let owner_user_id = u64::from_le_bytes(encoded[20..28].try_into().unwrap());
    let domain_len = encoded[28] as usize;
    let stream_id_len = u16::from_le_bytes(encoded[29..31].try_into().unwrap()) as usize;
    let reserved = encoded[31];
    let first_sequence = u64::from_le_bytes(encoded[32..40].try_into().unwrap());
    let event_count = u32::from_le_bytes(encoded[40..44].try_into().unwrap()) as usize;
    if version != VERSION
        || tenant_id != expected_key.tenant_id
        || owner_user_id != expected_key.owner_user_id
        || domain_len == 0
        || domain_len > MAX_DOMAIN_BYTES
        || stream_id_len == 0
        || stream_id_len > MAX_STREAM_ID_BYTES
        || reserved != 0
        || first_sequence != expected_first_sequence
        || event_count == 0
        || event_count > MAX_STREAM_APPEND_EVENTS
    {
        return Err(invalid_data("stream segment header mismatch"));
    }
    let mut offset = SEGMENT_FIXED_BYTES;
    let domain = take(encoded, &mut offset, domain_len)?;
    let stream_id = take(encoded, &mut offset, stream_id_len)?;
    if domain != expected_key.domain.as_bytes() || stream_id != expected_key.stream_id {
        return Err(invalid_data("stream segment key mismatch"));
    }
    let mut events = Vec::with_capacity(event_count);
    for _ in 0..event_count {
        let created_at_ms = i64::from_le_bytes(take(encoded, &mut offset, 8)?.try_into().unwrap());
        let event_type_len = take(encoded, &mut offset, 1)?[0] as usize;
        let payload_len =
            u32::from_le_bytes(take(encoded, &mut offset, 4)?.try_into().unwrap()) as usize;
        if event_type_len == 0
            || event_type_len > MAX_EVENT_TYPE_BYTES
            || payload_len > MAX_STREAM_EVENT_BYTES
        {
            return Err(invalid_data("stream event exceeds its encoded bound"));
        }
        let event_type = std::str::from_utf8(take(encoded, &mut offset, event_type_len)?)
            .map_err(|_| invalid_data("stream event type is not UTF-8"))?
            .to_owned();
        let payload = take(encoded, &mut offset, payload_len)?.to_vec();
        events.push(StreamEvent {
            created_at_ms,
            event_type,
            payload,
        });
    }
    if offset != encoded.len() {
        return Err(invalid_data("stream segment has trailing bytes"));
    }
    Ok(events)
}

impl StreamCommit {
    fn encode(&self) -> io::Result<Vec<u8>> {
        if self.event_count == 0 || self.segments.is_empty() || self.first_sequence == 0 {
            return Err(invalid_input(
                "stream commit is empty or has an invalid sequence",
            ));
        }
        let capacity = COMMIT_FIXED_BYTES
            + self.key.domain.len()
            + self.key.stream_id.len()
            + self.segments.len() * COMMIT_SEGMENT_BYTES;
        let mut encoded = Vec::with_capacity(capacity);
        encoded.extend_from_slice(COMMIT_MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.key.tenant_id.to_le_bytes());
        encoded.extend_from_slice(&self.key.owner_user_id.to_le_bytes());
        encoded.push(self.key.domain.len() as u8);
        encoded.extend_from_slice(&(self.key.stream_id.len() as u16).to_le_bytes());
        encoded.push(0);
        encoded.extend_from_slice(&self.first_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.event_count.to_le_bytes());
        encoded.extend_from_slice(&(self.segments.len() as u16).to_le_bytes());
        encoded.extend_from_slice(&0_u16.to_le_bytes());
        encoded.extend_from_slice(self.key.domain.as_bytes());
        encoded.extend_from_slice(&self.key.stream_id);
        for segment in &self.segments {
            encoded.extend_from_slice(&segment.block_id.0);
            encoded.extend_from_slice(&segment.event_count.to_le_bytes());
            encoded.extend_from_slice(&segment.minimum_created_at_ms.to_le_bytes());
            encoded.extend_from_slice(&segment.maximum_created_at_ms.to_le_bytes());
        }
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Option<Self>> {
        if !encoded.starts_with(COMMIT_MAGIC) {
            return Ok(None);
        }
        if encoded.len() < COMMIT_FIXED_BYTES {
            return Err(invalid_data("truncated stream commit"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let tenant_id = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
        let owner_user_id = u64::from_le_bytes(encoded[20..28].try_into().unwrap());
        let domain_len = encoded[28] as usize;
        let stream_id_len = u16::from_le_bytes(encoded[29..31].try_into().unwrap()) as usize;
        let reserved = encoded[31];
        let first_sequence = u64::from_le_bytes(encoded[32..40].try_into().unwrap());
        let event_count = u32::from_le_bytes(encoded[40..44].try_into().unwrap());
        let segment_count = u16::from_le_bytes(encoded[44..46].try_into().unwrap()) as usize;
        let reserved_tail = u16::from_le_bytes(encoded[46..48].try_into().unwrap());
        let expected_len = COMMIT_FIXED_BYTES
            .checked_add(domain_len)
            .and_then(|length| length.checked_add(stream_id_len))
            .and_then(|length| length.checked_add(segment_count * COMMIT_SEGMENT_BYTES))
            .ok_or_else(|| invalid_data("stream commit length overflow"))?;
        if version != VERSION
            || tenant_id == 0
            || owner_user_id == 0
            || domain_len == 0
            || domain_len > MAX_DOMAIN_BYTES
            || stream_id_len == 0
            || stream_id_len > MAX_STREAM_ID_BYTES
            || reserved != 0
            || reserved_tail != 0
            || first_sequence == 0
            || event_count == 0
            || event_count as usize > MAX_STREAM_APPEND_EVENTS
            || segment_count == 0
            || segment_count > MAX_STREAM_APPEND_EVENTS
            || expected_len != encoded.len()
        {
            return Err(invalid_data("invalid or unbounded stream commit"));
        }
        let mut offset = COMMIT_FIXED_BYTES;
        let domain = std::str::from_utf8(take(encoded, &mut offset, domain_len)?)
            .map_err(|_| invalid_data("stream domain is not UTF-8"))?;
        let stream_id = take(encoded, &mut offset, stream_id_len)?;
        let key = StreamKey::new(tenant_id, owner_user_id, domain, stream_id)
            .map_err(|_| invalid_data("stream commit key is invalid"))?;
        let mut segments = Vec::with_capacity(segment_count);
        let mut summed_events = 0_u64;
        for _ in 0..segment_count {
            let block_id = BlockId(take(encoded, &mut offset, 32)?.try_into().unwrap());
            let segment_events =
                u32::from_le_bytes(take(encoded, &mut offset, 4)?.try_into().unwrap());
            let minimum_created_at_ms =
                i64::from_le_bytes(take(encoded, &mut offset, 8)?.try_into().unwrap());
            let maximum_created_at_ms =
                i64::from_le_bytes(take(encoded, &mut offset, 8)?.try_into().unwrap());
            if segment_events == 0
                || segment_events as usize > MAX_STREAM_APPEND_EVENTS
                || minimum_created_at_ms > maximum_created_at_ms
            {
                return Err(invalid_data("stream commit segment metadata is invalid"));
            }
            summed_events = summed_events
                .checked_add(segment_events as u64)
                .ok_or_else(|| invalid_data("stream event count overflow"))?;
            segments.push(SegmentReference {
                block_id,
                event_count: segment_events,
                minimum_created_at_ms,
                maximum_created_at_ms,
            });
        }
        if offset != encoded.len() || summed_events != event_count as u64 {
            return Err(invalid_data("stream commit event count mismatch"));
        }
        Ok(Some(Self {
            key,
            first_sequence,
            event_count,
            segments,
        }))
    }
}

impl StreamCatalog {
    pub fn rebuild(engine: &Engine) -> io::Result<Self> {
        let mut catalog = Self::default();
        let transactions = engine.transaction_snapshot()?;
        for transaction in &transactions {
            let inline_payload = &transaction.envelope.inline_payload;
            let family_records = decode_family_records(inline_payload)?;
            let multiplexed = family_records.is_some();
            let mut commits = Vec::new();
            if let Some(records) = family_records {
                for record in records {
                    if record.kind == FamilyRecordKind::StreamCommit {
                        commits.push(StreamCommit::decode(record.payload)?.ok_or_else(|| {
                            invalid_data("transaction stream family record has the wrong format")
                        })?);
                    }
                }
            } else if let Some(commit) = StreamCommit::decode(inline_payload)? {
                commits.push(commit);
            }
            for commit in commits {
                let referenced: Vec<_> = commit
                    .segments
                    .iter()
                    .map(|segment| segment.block_id)
                    .collect();
                let references_match = if multiplexed {
                    referenced
                        .iter()
                        .all(|block_id| transaction.envelope.block_ids.contains(block_id))
                } else {
                    referenced == transaction.envelope.block_ids
                };
                if !references_match {
                    return Err(invalid_data("stream commit block references do not match"));
                }
                let expected = catalog
                    .event_count(&commit.key)
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("stream segment sequence overflow"))?;
                if commit.first_sequence != expected {
                    return Err(invalid_data("stream commit sequence is not contiguous"));
                }
                let mut segment_sequence = commit.first_sequence;
                for reference in &commit.segments {
                    let events = decode_segment(
                        &engine.read_block(reference.block_id)?,
                        &commit.key,
                        segment_sequence,
                    )?;
                    let minimum = events
                        .iter()
                        .map(|event| event.created_at_ms)
                        .min()
                        .unwrap();
                    let maximum = events
                        .iter()
                        .map(|event| event.created_at_ms)
                        .max()
                        .unwrap();
                    if events.len() != reference.event_count as usize
                        || minimum != reference.minimum_created_at_ms
                        || maximum != reference.maximum_created_at_ms
                    {
                        return Err(invalid_data("stream segment metadata witness mismatch"));
                    }
                    segment_sequence = segment_sequence
                        .checked_add(events.len() as u64)
                        .ok_or_else(|| invalid_data("stream segment sequence overflow"))?;
                }
                catalog
                    .streams
                    .entry(commit.key.clone())
                    .or_default()
                    .extend(commit.segments);
                catalog.event_counts.insert(
                    commit.key,
                    expected
                        .checked_add(commit.event_count as u64 - 1)
                        .ok_or_else(|| invalid_data("stream sequence overflow"))?,
                );
            }
        }
        catalog.observed_engine_sequence = engine.state().durable_sequence;
        Ok(catalog)
    }

    pub fn event_count(&self, key: &StreamKey) -> u64 {
        self.event_counts.get(key).copied().unwrap_or(0)
    }

    pub fn next_sequence(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
    ) -> io::Result<u64> {
        ensure_scope(tenant_id, owner_user_id, key)?;
        self.event_count(key)
            .checked_add(1)
            .ok_or_else(|| invalid_data("stream sequence overflow"))
    }

    pub fn append(
        &mut self,
        engine: &mut Engine,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
        expected_next_sequence: u64,
        events: &[StreamEvent],
    ) -> io::Result<StreamAppendResult> {
        let prepared = self.prepare_append(
            engine,
            tenant_id,
            owner_user_id,
            key,
            expected_next_sequence,
            events,
        )?;
        let transaction =
            engine.commit_references(prepared.commit_record(), prepared.block_ids())?;
        Ok(self
            .apply_committed_appends(vec![prepared], transaction.sequence)?
            .remove(0))
    }

    pub(crate) fn prepare_append(
        &self,
        engine: &Engine,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
        expected_next_sequence: u64,
        events: &[StreamEvent],
    ) -> io::Result<PreparedStreamAppend> {
        ensure_scope(tenant_id, owner_user_id, key)?;
        if self.observed_engine_sequence != engine.state().durable_sequence {
            return Err(conflict("stream catalog engine witness changed"));
        }
        prepare_persisted_append(
            engine,
            key,
            self.event_count(key),
            expected_next_sequence,
            events,
        )
    }

    pub(crate) fn apply_committed_appends(
        &mut self,
        prepared_appends: Vec<PreparedStreamAppend>,
        committed_sequence: u64,
    ) -> io::Result<Vec<StreamAppendResult>> {
        let expected_transaction_sequence = self
            .observed_engine_sequence
            .checked_add(1)
            .ok_or_else(|| invalid_data("stream engine sequence overflow"))?;
        if committed_sequence != expected_transaction_sequence {
            return Err(invalid_data("stream sequencer changed during commit"));
        }
        let mut keys = std::collections::BTreeSet::new();
        for prepared in &prepared_appends {
            let expected_next_sequence = self
                .event_count(&prepared.key)
                .checked_add(1)
                .ok_or_else(|| invalid_data("stream sequence overflow"))?;
            if prepared.expected_engine_sequence != self.observed_engine_sequence
                || !keys.insert(prepared.key.clone())
                || prepared.first_sequence != expected_next_sequence
            {
                return Err(conflict("prepared stream witness changed"));
            }
        }
        let mut results = Vec::with_capacity(prepared_appends.len());
        for prepared in prepared_appends {
            let last_sequence = prepared
                .first_sequence
                .checked_add(prepared.event_count as u64 - 1)
                .ok_or_else(|| invalid_data("stream sequence overflow"))?;
            let segment_count = u16::try_from(prepared.block_ids.len())
                .map_err(|_| invalid_data("stream segment count overflow"))?;
            self.streams
                .entry(prepared.key.clone())
                .or_default()
                .extend(prepared.segments);
            self.event_counts.insert(prepared.key, last_sequence);
            results.push(StreamAppendResult {
                transaction_sequence: committed_sequence,
                first_sequence: prepared.first_sequence,
                last_sequence,
                segment_count,
            });
        }
        self.observed_engine_sequence = committed_sequence;
        Ok(results)
    }

    pub fn read(
        &self,
        engine: &Engine,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
        from_sequence: u64,
        limit: usize,
    ) -> io::Result<StreamPage> {
        ensure_scope(tenant_id, owner_user_id, key)?;
        if from_sequence == 0 || limit == 0 || limit > MAX_STREAM_READ_EVENTS {
            return Err(invalid_input("stream page request is invalid or unbounded"));
        }
        let stream_end = self.event_count(key);
        if from_sequence > stream_end.saturating_add(1) {
            return Err(invalid_input("stream cursor is beyond the current end"));
        }
        let mut events: Vec<SequencedEvent> = Vec::new();
        let mut returned_bytes = 0_usize;
        let mut segment_first = 1_u64;
        for reference in self.streams.get(key).into_iter().flatten() {
            let segment_end = segment_first
                .checked_add(reference.event_count as u64 - 1)
                .ok_or_else(|| invalid_data("stream segment sequence overflow"))?;
            if segment_end < from_sequence {
                segment_first = segment_end + 1;
                continue;
            }
            let decoded =
                decode_segment(&engine.read_block(reference.block_id)?, key, segment_first)?;
            for (offset, event) in decoded.into_iter().enumerate() {
                let sequence = segment_first + offset as u64;
                if sequence < from_sequence {
                    continue;
                }
                let event_bytes = event.event_type.len() + event.payload.len();
                if events.len() == limit
                    || (!events.is_empty() && returned_bytes + event_bytes > MAX_STREAM_READ_BYTES)
                {
                    let next_sequence = match events.last() {
                        Some(last) => last
                            .sequence
                            .checked_add(1)
                            .ok_or_else(|| invalid_data("stream page sequence overflow"))?,
                        None => from_sequence,
                    };
                    return Ok(StreamPage {
                        events,
                        next_sequence,
                        end_of_stream: false,
                    });
                }
                returned_bytes += event_bytes;
                events.push(SequencedEvent { sequence, event });
            }
            segment_first = segment_end + 1;
        }
        let next_sequence = match events.last() {
            Some(event) => event
                .sequence
                .checked_add(1)
                .ok_or_else(|| invalid_data("stream page sequence overflow"))?,
            None => from_sequence,
        };
        Ok(StreamPage {
            events,
            next_sequence,
            end_of_stream: next_sequence > stream_end,
        })
    }

    pub fn read_positions(
        &self,
        engine: &Engine,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
        positions: &BTreeSet<u64>,
    ) -> io::Result<BTreeMap<u64, StreamEvent>> {
        ensure_scope(tenant_id, owner_user_id, key)?;
        if positions.is_empty()
            || positions.len() > MAX_STREAM_READ_EVENTS
            || positions.first().is_some_and(|position| *position == 0)
            || positions
                .last()
                .is_some_and(|position| *position > self.event_count(key))
        {
            return Err(invalid_input("stream point-read positions are invalid"));
        }
        let mut selected = BTreeMap::new();
        let mut selected_bytes = 0_usize;
        let mut segment_first = 1_u64;
        for reference in self.streams.get(key).into_iter().flatten() {
            let segment_end = segment_first
                .checked_add(reference.event_count as u64 - 1)
                .ok_or_else(|| invalid_data("stream segment sequence overflow"))?;
            let overlaps = positions
                .range(segment_first..=segment_end)
                .next()
                .is_some();
            if overlaps {
                let decoded =
                    decode_segment(&engine.read_block(reference.block_id)?, key, segment_first)?;
                for position in positions.range(segment_first..=segment_end) {
                    let event = decoded
                        .get((*position - segment_first) as usize)
                        .ok_or_else(|| invalid_data("stream point-read offset is missing"))?
                        .clone();
                    selected_bytes = selected_bytes
                        .checked_add(event.encoded_len())
                        .filter(|bytes| *bytes <= MAX_STREAM_READ_BYTES)
                        .ok_or_else(|| invalid_input("stream point-read exceeds 8 MiB"))?;
                    selected.insert(*position, event);
                }
            }
            segment_first = segment_end + 1;
        }
        if selected.len() != positions.len() {
            return Err(invalid_data("stream point-read position is missing"));
        }
        Ok(selected)
    }
}

impl PreparedStreamAppend {
    pub(crate) fn stage_persisted_catalog(
        &self,
        entities: &EntityDatabase,
        transaction: &mut EntityTransaction,
    ) -> io::Result<()> {
        let last_sequence = self.last_sequence;
        let retained_from_sequence = entities
            .get(transaction, &catalog_metadata_key(&self.key)?)?
            .as_deref()
            .map(decode_catalog_metadata)
            .transpose()?
            .map_or(1, |metadata| metadata.retained_from_sequence);
        entities.put(
            transaction,
            catalog_metadata_key(&self.key)?,
            encode_catalog_metadata(CatalogMetadata {
                retained_from_sequence,
                end_sequence: last_sequence,
            })?,
        )?;
        let mut first_sequence = self.first_sequence;
        for reference in &self.segments {
            entities.put(
                transaction,
                catalog_segment_key(&self.key, first_sequence)?,
                encode_catalog_segment(reference),
            )?;
            first_sequence = first_sequence
                .checked_add(reference.event_count as u64)
                .ok_or_else(|| invalid_data("stream catalog sequence overflow"))?;
        }
        if first_sequence
            != last_sequence
                .checked_add(1)
                .ok_or_else(|| invalid_data("stream catalog sequence overflow"))?
        {
            return Err(invalid_data("stream catalog segment count mismatch"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transaction::{encode_family_records, FamilyRecord};

    fn event(index: usize, payload_bytes: usize) -> StreamEvent {
        StreamEvent::new(
            index as i64,
            "tool_delta",
            vec![(index % 251) as u8; payload_bytes],
        )
        .unwrap()
    }

    #[test]
    fn catalog_metadata_v2_preserves_legacy_cursor_and_retirement_bounds() {
        let mut legacy = Vec::new();
        legacy.extend_from_slice(CATALOG_META_MAGIC);
        legacy.extend_from_slice(&VERSION.to_le_bytes());
        legacy.extend_from_slice(&9_u64.to_le_bytes());
        assert_eq!(
            decode_catalog_metadata(&legacy).unwrap(),
            CatalogMetadata {
                retained_from_sequence: 1,
                end_sequence: 9,
            }
        );
        let encoded = encode_catalog_metadata(CatalogMetadata {
            retained_from_sequence: 4,
            end_sequence: 9,
        })
        .unwrap();
        assert_eq!(encoded.len(), 28);
        assert_eq!(
            decode_catalog_metadata(&encoded).unwrap(),
            CatalogMetadata {
                retained_from_sequence: 4,
                end_sequence: 9,
            }
        );
    }

    #[test]
    fn segmented_stream_rebuilds_and_pages_after_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let key = StreamKey::new(7, 11, "task_event", b"task-1").unwrap();
        let events: Vec<_> = (0..20).map(|index| event(index, 150_000)).collect();
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            let mut catalog = StreamCatalog::rebuild(&engine).unwrap();
            let result = catalog
                .append(&mut engine, 7, 11, &key, 1, &events)
                .unwrap();
            assert_eq!(result.first_sequence, 1);
            assert_eq!(result.last_sequence, 20);
            assert!(result.segment_count >= 2);
            let metrics = engine.block_write_metrics();
            assert_eq!(metrics.blocks_written, result.segment_count as u64);
            assert!(metrics.bytes_written < 6_000_000);
        }
        let engine = Engine::open(directory.path()).unwrap();
        let catalog = StreamCatalog::rebuild(&engine).unwrap();
        assert_eq!(catalog.event_count(&key), 20);
        let first = catalog.read(&engine, 7, 11, &key, 1, 7).unwrap();
        assert_eq!(first.events.len(), 7);
        assert_eq!(first.next_sequence, 8);
        assert!(!first.end_of_stream);
        let second = catalog
            .read(&engine, 7, 11, &key, first.next_sequence, 100)
            .unwrap();
        assert_eq!(second.events.len(), 13);
        assert_eq!(second.events[0].sequence, 8);
        assert!(second.end_of_stream);
        assert_eq!(second.next_sequence, 21);
    }

    #[test]
    fn owner_scope_and_expected_position_fail_before_commit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        let mut catalog = StreamCatalog::rebuild(&engine).unwrap();
        let mut stale_catalog = StreamCatalog::rebuild(&engine).unwrap();
        let key = StreamKey::new(1, 2, "turn", b"conversation-1").unwrap();
        catalog
            .append(&mut engine, 1, 2, &key, 1, &[event(1, 8)])
            .unwrap();
        let sequence = engine.state().durable_sequence;
        let error = catalog
            .append(&mut engine, 1, 2, &key, 1, &[event(2, 8)])
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert_eq!(engine.state().durable_sequence, sequence);
        let error = stale_catalog
            .append(&mut engine, 1, 2, &key, 1, &[event(2, 8)])
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        let error = catalog.read(&engine, 1, 3, &key, 1, 10).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
    }

    #[test]
    fn page_byte_budget_stops_before_large_allocation() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        let mut catalog = StreamCatalog::rebuild(&engine).unwrap();
        let key = StreamKey::new(1, 2, "turn", b"conversation-1").unwrap();
        let oversized: Vec<_> = (0..40).map(|index| event(index, 250_000)).collect();
        assert!(catalog
            .append(&mut engine, 1, 2, &key, 1, &oversized)
            .is_err());

        let first: Vec<_> = (0..24).map(|index| event(index, 250_000)).collect();
        catalog.append(&mut engine, 1, 2, &key, 1, &first).unwrap();
        let second: Vec<_> = (24..48).map(|index| event(index, 250_000)).collect();
        catalog
            .append(&mut engine, 1, 2, &key, 25, &second)
            .unwrap();
        let page = catalog.read(&engine, 1, 2, &key, 1, 1_000).unwrap();
        assert_eq!(page.events.len(), 33);
        assert_eq!(page.next_sequence, 34);
        assert!(!page.end_of_stream);
    }

    #[test]
    fn multiplexed_transaction_recovers_stream_commit_with_other_families() {
        let directory = tempfile::tempdir().unwrap();
        let key = StreamKey::new(7, 11, "task_event", b"task-1").unwrap();
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            let events = [event(1, 32)];
            let segment = encode_segment(&key, 1, &events).unwrap();
            let block_id = engine.write_block(&segment).unwrap();
            let stream_record = StreamCommit {
                key: key.clone(),
                first_sequence: 1,
                event_count: 1,
                segments: vec![SegmentReference {
                    block_id,
                    event_count: 1,
                    minimum_created_at_ms: 1,
                    maximum_created_at_ms: 1,
                }],
            }
            .encode()
            .unwrap();
            let inline = encode_family_records(&[
                FamilyRecord {
                    kind: FamilyRecordKind::StreamCommit,
                    payload: &stream_record,
                },
                FamilyRecord {
                    kind: FamilyRecordKind::CommandReceipt,
                    payload: b"receipt",
                },
            ])
            .unwrap();
            engine.commit_references(&inline, &[block_id]).unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        let catalog = StreamCatalog::rebuild(&engine).unwrap();
        assert_eq!(catalog.event_count(&key), 1);
        let page = catalog.read(&engine, 7, 11, &key, 1, 10).unwrap();
        assert_eq!(page.events.len(), 1);
        assert_eq!(page.events[0].event.payload, vec![1; 32]);
    }
}
