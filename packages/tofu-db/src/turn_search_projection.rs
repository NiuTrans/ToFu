//! Independent, disposable conversation-search projection.
//!
//! This module owns a separate Tofu-DB authority directory. Projection pages
//! are built under an invisible generation and become queryable in one atomic
//! header swap, so a long rebuild never exposes a partial conversation. Small
//! per-chunk n-gram summaries reject most candidates before any text chunk is
//! materialized. The source authority remains the sole owner of durable user
//! state; this store may always be deleted and rebuilt from its dirty set.

use std::io;
use std::path::Path;
use std::sync::Arc;

use blake3::Hasher;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_conversation_search::{CHUNK_CHARACTERS, SOURCE_PAGE_ROWS};
use crate::vfs::Vfs;

#[path = "turn_search_projection_recovery.rs"]
mod recovery;

#[path = "turn_search_posting_codec.rs"]
mod posting_codec;

#[path = "turn_search_generation_dictionary.rs"]
mod generation_dictionary;

#[path = "turn_search_query.rs"]
mod query;
pub use query::ConversationSearchPage;

const HEADER_NAMESPACE: &str = "search_header_v1";
const ACTIVE_NAMESPACE: &str = "search_active_v1";
const BUILD_NAMESPACE: &str = "search_build_v1";
const TURN_META_NAMESPACE: &str = "search_turn_meta_v1";
const TURN_TEXT_NAMESPACE: &str = "search_turn_text_v1";
const USAGE_NAMESPACE: &str = "search_usage_v1";
const USAGE_KEY: &[u8] = b"logical_bytes";
// Owner-level gram posting index: one packed row per width-1/2/3 gram digest
// with stable IDs for the conversation generations whose
// title, id, or some chunk emitted it. A query resolves its candidate
// conversations through a handful of prefix scans here instead of opening
// every conversation's meta namespace. The index is a candidate filter only:
// every candidate still runs the exact legacy per-conversation verification.
const GRAM_NAMESPACE: &str = "search_gram_postings_v1";
// Per-owner lifecycle of the posting index, durable beside the format marker
// (that namespace is the one a versioned wipe never retires): absent on
// legacy projections (reader walks), BUILDING after a versioned wipe until
// the first complete backfill (reader walks), READY to serve candidate sets,
// or DEGRADED after an emission tripped the owner budget (reader walks;
// sticky until the next versioned wipe).
const GRAM_STATE_KEY: &[u8] = b"gram_index";
const GRAM_STATE_BUILDING: u8 = 0;
const GRAM_STATE_READY: u8 = 1;
const GRAM_STATE_DEGRADED: u8 = 2;
const GRAM_RETIRE_SCAN_BATCH: usize = 64;
// Conversation generations whose gram emission overflowed a write bound are
// listed here so the reader treats them as unconditional candidates (the
// exact legacy walk verifies them); the rest of the owner keeps the index.
const GRAM_EXCLUSION_NAMESPACE: &str = "search_gram_excluded_v1";
// One full entity-scan page; an owner with this many excluded generations
// takes the unfiltered walk (the index is not earning its keep there).
const GRAM_EXCLUSION_SCAN_CAP: usize =
    crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
// Posting lists preserve the existing 255-generation selectivity boundary,
// encoding stable owner-scoped IDs as canonical delta varints; a gram in more conversations promotes to a universal marker the
// reader treats as unfilterable for that gram.
const GRAM_LIST_MAX_ENTRIES: usize = 255;
// Emission/removal bounds keep every projection transaction far below the
// engine's 14,336-distinct-write and 6,144-point-witness ceilings: an append
// emits at most this many posting updates before excluding the conversation
// (one read-modify-write each, plus the page's ordinary reads), and a
// generation retirement retracts at most this many (leftovers stay charged
// and linger as false candidates until the next versioned wipe).
const GRAM_APPEND_OP_CAP: usize = 5_500;
const GRAM_REMOVE_OP_CAP: usize = 6_000;
const FORMAT_VERSION: u8 = crate::generated_conversation_search::PROJECTION_FORMAT_VERSION;
const BLOOM_BYTES: usize = 2_048;
const BLOOM_HASHES: usize = 3;
const TEXT_CHUNK_BYTES: usize = 6_000;
const MAX_SEARCH_TEXT_BYTES: usize = CHUNK_CHARACTERS * 4;
const MAX_ID_BYTES: usize = 512;
const MAX_GENERATION_BYTES: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TurnSearchDocument {
    pub turn_id: String,
    pub ordinal: u64,
    pub search_text: String,
    pub chunk_index: u32,
    pub role: String,
    pub match_field: String,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct ConversationSearchHit {
    pub id: String,
    pub snippet: String,
    pub role: Option<String>,
    pub match_field: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConversationSearchRequest {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub query: String,
    pub limit: usize,
    pub snippet_radius: usize,
}

pub(crate) struct OpenTurnSearchProjection {
    pub(crate) database: AuthorityDatabase,
    maximum_logical_bytes_per_owner: u64,
    // Runtime-only: persisted coverage is not proof that this launch has
    // validated the source UUID or checked the authority's pending work.
    admission_source: Option<uuid::Uuid>,
    admitted_owners: std::collections::BTreeSet<(u64, u64)>,
}

#[derive(Clone)]
struct Header {
    conversation_id: String,
    generation: String,
    updated_at_ms: u64,
    generation_bytes: u64,
    title: String,
    source_incomplete: bool,
    // Derived from exact generation accounting on decode, never persisted.
    has_chunks: bool,
}

struct Build {
    conversation_id: String,
    generation: String,
    updated_at_ms: u64,
    generation_bytes: u64,
    title: String,
    source_incomplete: bool,
}

struct TurnMeta {
    conversation_id: String,
    generation: String,
    turn_id: String,
    ordinal: u64,
    chunk_count: u16,
    bloom: [u8; BLOOM_BYTES],
    chunk_index: u32,
    role: String,
    match_field: String,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn validate_identity(value: &str, name: &str, maximum: usize) -> io::Result<()> {
    if value.is_empty() || value.len() > maximum || value.as_bytes().contains(&0) {
        return Err(invalid_input(name));
    }
    Ok(())
}

fn digest(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Hasher::new();
    hasher.update(domain);
    for part in parts {
        hasher.update(&(*part).len().to_be_bytes());
        hasher.update(part);
    }
    *hasher.finalize().as_bytes()
}

fn conversation_digest(conversation_id: &str) -> [u8; 32] {
    digest(
        b"tofu-db:turn-search:conversation:v1\0",
        &[conversation_id.as_bytes()],
    )
}

fn generation_digest(conversation_id: &str, generation: &str) -> [u8; 32] {
    digest(
        b"tofu-db:turn-search:generation:v1\0",
        &[conversation_id.as_bytes(), generation.as_bytes()],
    )
}

fn turn_digest(turn_id: &str) -> [u8; 32] {
    digest(b"tofu-db:turn-search:turn:v1\0", &[turn_id.as_bytes()])
}

fn gram_digest(gram: &[u8]) -> [u8; 32] {
    digest(b"tofu-db:turn-search:gram-posting:v1\0", &[gram])
}

enum GramList {
    Omni,
    Entries(Vec<u64>),
}

fn decode_gram_list(input: &[u8]) -> io::Result<GramList> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search gram posting list version is unsupported"));
    }
    match input.get(1) {
        Some(1) if input.len() == 2 => Ok(GramList::Omni),
        Some(0) => Ok(GramList::Entries(posting_codec::decode(
            &input[2..],
            crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES as usize - 2,
            GRAM_LIST_MAX_ENTRIES,
        )?)),
        _ => Err(invalid_data("search gram posting list is malformed")),
    }
}

fn encode_gram_entries(entries: &[u64]) -> io::Result<Vec<u8>> {
    if entries.len() > GRAM_LIST_MAX_ENTRIES {
        return Err(invalid_data("search gram posting list exceeds entry bound"));
    }
    let encoded = posting_codec::encode(entries,
        crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES as usize - 2)?;
    let mut output = Vec::with_capacity(2 + encoded.len());
    output.extend_from_slice(&[FORMAT_VERSION, 0]);
    output.extend_from_slice(&encoded);
    Ok(output)
}

fn gram_state_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(transaction, "search_format", GRAM_STATE_KEY)
}

fn read_gram_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<u8>> {
    let stored_key = gram_state_key(transaction)?;
    match database.entity_get(transaction, &stored_key)? {
        None => Ok(None),
        Some(raw) if raw.len() == 1 && raw[0] <= GRAM_STATE_DEGRADED => Ok(Some(raw[0])),
        Some(_) => Err(invalid_data("search gram index state is malformed")),
    }
}

// Posting values are candidate hints, encoded by posting_codec using stable
// generation_dictionary IDs. Exact verification re-reads title, id and text.

fn key(transaction: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn build_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        BUILD_NAMESPACE,
        &conversation_digest(conversation_id),
    )
}

fn active_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        ACTIVE_NAMESPACE,
        &conversation_digest(conversation_id),
    )
}

fn generation_prefix(conversation_id: &str, generation: &str) -> Vec<u8> {
    generation_digest(conversation_id, generation).to_vec()
}

fn generation_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    conversation_id: &str,
    generation: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &generation_prefix(conversation_id, generation),
    )
}

fn header_key(
    transaction: &AuthorityTransaction,
    updated_at_ms: u64,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(8 + conversation_id.len() + 1);
    raw.extend_from_slice(&(u64::MAX - updated_at_ms).to_be_bytes());
    raw.extend(conversation_id.as_bytes().iter().map(|byte| !byte));
    raw.push(u8::MAX);
    key(transaction, HEADER_NAMESPACE, &raw)
}

fn turn_meta_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    generation: &str,
    ordinal: u64,
    turn_id: &str,
    chunk_index: u32,
) -> io::Result<EntityKey> {
    let mut raw = generation_prefix(conversation_id, generation);
    raw.extend_from_slice(&ordinal.to_be_bytes());
    raw.extend_from_slice(turn_id.as_bytes());
    raw.push(0);
    raw.extend_from_slice(&chunk_index.to_be_bytes());
    key(transaction, TURN_META_NAMESPACE, &raw)
}

fn turn_text_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    generation: &str,
    ordinal: u64,
    turn_id: &str,
    chunk_index: u32,
    chunk: u16,
) -> io::Result<EntityKey> {
    let mut raw = generation_prefix(conversation_id, generation);
    raw.extend_from_slice(&ordinal.to_be_bytes());
    raw.extend_from_slice(&turn_digest(turn_id));
    raw.extend_from_slice(&chunk_index.to_be_bytes());
    raw.extend_from_slice(&chunk.to_be_bytes());
    key(transaction, TURN_TEXT_NAMESPACE, &raw)
}

fn push_string(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    let length =
        u16::try_from(value.len()).map_err(|_| invalid_input("search identity too long"))?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn read_string(input: &mut &[u8]) -> io::Result<String> {
    if input.len() < 2 {
        return Err(invalid_data("search projection string is truncated"));
    }
    let length = u16::from_be_bytes(input[..2].try_into().unwrap()) as usize;
    *input = &input[2..];
    if input.len() < length {
        return Err(invalid_data("search projection string is truncated"));
    }
    let value = std::str::from_utf8(&input[..length])
        .map_err(|_| invalid_data("search projection string is not UTF-8"))?
        .to_owned();
    *input = &input[length..];
    Ok(value)
}

fn take_u64(input: &mut &[u8]) -> io::Result<u64> {
    if input.len() < 8 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u64::from_be_bytes(input[..8].try_into().unwrap());
    *input = &input[8..];
    Ok(value)
}

fn take_u16(input: &mut &[u8]) -> io::Result<u16> {
    if input.len() < 2 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u16::from_be_bytes(input[..2].try_into().unwrap());
    *input = &input[2..];
    Ok(value)
}

fn finish_decode(input: &[u8]) -> io::Result<()> {
    if input.is_empty() {
        Ok(())
    } else {
        Err(invalid_data("search projection record has trailing bytes"))
    }
}

fn encode_header(header: &Header) -> io::Result<Vec<u8>> {
    let mut output = vec![FORMAT_VERSION];
    output.extend_from_slice(&header.updated_at_ms.to_be_bytes());
    output.extend_from_slice(&header.generation_bytes.to_be_bytes());
    push_string(&mut output, &header.conversation_id)?;
    push_string(&mut output, &header.generation)?;
    push_string(&mut output, &header.title)?;
    output.push(u8::from(header.source_incomplete));
    Ok(output)
}

fn reserved_header_bytes(encoded_bytes: usize, id_bytes: usize) -> io::Result<u64> {
    encoded_bytes
        .checked_mul(3)
        .and_then(|n| id_bytes.checked_mul(2).and_then(|ids| n.checked_add(ids)))
        .and_then(|n| n.checked_add(128))
        .map(|n| n as u64)
        .ok_or_else(|| exhausted("search header reservation overflow"))
}

fn decode_header(mut input: &[u8]) -> io::Result<Header> {
    let encoded_bytes = input.len();
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search header version is unsupported"));
    }
    input = &input[1..];
    let updated_at_ms = take_u64(&mut input)?;
    let generation_bytes = take_u64(&mut input)?;
    let conversation_id = read_string(&mut input)?;
    let generation = read_string(&mut input)?;
    let title = read_string(&mut input)?;
    let source_incomplete = match input.first() {
        Some(0) => false,
        Some(1) => true,
        _ => return Err(invalid_data("invalid search source coverage")),
    };
    input = &input[1..];
    finish_decode(input)?;
    let chunk_bytes = generation_bytes
        .checked_sub(reserved_header_bytes(encoded_bytes, conversation_id.len())?)
        .ok_or_else(|| invalid_data("search header generation size is invalid"))?;
    Ok(Header {
        conversation_id,
        generation,
        updated_at_ms,
        generation_bytes,
        title,
        source_incomplete,
        has_chunks: chunk_bytes != 0,
    })
}

fn encode_build(build: &Build) -> io::Result<Vec<u8>> {
    encode_header(&Header {
        conversation_id: build.conversation_id.clone(),
        generation: build.generation.clone(),
        updated_at_ms: build.updated_at_ms,
        generation_bytes: build.generation_bytes,
        title: build.title.clone(),
        source_incomplete: build.source_incomplete,
        has_chunks: false, // Encoding only; derived when the header is read.
    })
}

fn decode_build(input: &[u8]) -> io::Result<Build> {
    let header = decode_header(input)?;
    Ok(Build {
        conversation_id: header.conversation_id,
        generation: header.generation,
        updated_at_ms: header.updated_at_ms,
        generation_bytes: header.generation_bytes,
        title: header.title,
        source_incomplete: header.source_incomplete,
    })
}

fn encode_meta(meta: &TurnMeta) -> io::Result<Vec<u8>> {
    let mut output = vec![FORMAT_VERSION];
    output.extend_from_slice(&meta.ordinal.to_be_bytes());
    output.extend_from_slice(&meta.chunk_count.to_be_bytes());
    output.extend_from_slice(&meta.bloom);
    push_string(&mut output, &meta.conversation_id)?;
    push_string(&mut output, &meta.generation)?;
    push_string(&mut output, &meta.turn_id)?;
    output.extend_from_slice(&meta.chunk_index.to_be_bytes());
    push_string(&mut output, &meta.role)?;
    push_string(&mut output, &meta.match_field)?;
    Ok(output)
}

fn decode_meta(mut input: &[u8]) -> io::Result<TurnMeta> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search turn metadata version is unsupported"));
    }
    input = &input[1..];
    let ordinal = take_u64(&mut input)?;
    let chunk_count = take_u16(&mut input)?;
    if usize::from(chunk_count) > MAX_SEARCH_TEXT_BYTES.div_ceil(TEXT_CHUNK_BYTES) {
        return Err(invalid_data("search chunk count exceeds its bound"));
    }
    if input.len() < BLOOM_BYTES {
        return Err(invalid_data("search turn bloom is truncated"));
    }
    let mut bloom = [0; BLOOM_BYTES];
    bloom.copy_from_slice(&input[..BLOOM_BYTES]);
    input = &input[BLOOM_BYTES..];
    let conversation_id = read_string(&mut input)?;
    let generation = read_string(&mut input)?;
    let turn_id = read_string(&mut input)?;
    if input.len() < 4 {
        return Err(invalid_data("search chunk index is truncated"));
    }
    let chunk_index = u32::from_be_bytes(input[..4].try_into().unwrap());
    input = &input[4..];
    let role = read_string(&mut input)?;
    let match_field = read_string(&mut input)?;
    finish_decode(input)?;
    Ok(TurnMeta {
        conversation_id,
        generation,
        turn_id,
        ordinal,
        chunk_count,
        bloom,
        chunk_index,
        role,
        match_field,
    })
}

// Unicode boundaries stream in constant space instead of materializing two
// O(text length) vectors for each n-gram width.
fn gram_ranges(value: &str, width: usize) -> impl Iterator<Item = (usize, usize)> + '_ {
    let starts = value.char_indices().map(|(index, _)| index);
    let ends = value
        .char_indices()
        .map(|(index, _)| index)
        .chain(std::iter::once(value.len()))
        .skip(width);
    starts.zip(ends)
}

#[derive(Clone, Copy)]
struct CachedGram {
    bytes: [u8; 12], // Three Unicode scalars, each at most four UTF-8 bytes.
    length: u8,
}

const GRAM_CACHE_ENTRIES: usize = 256;

fn visit_gram_widths(value: &str, widths: &[usize], mut consume: impl FnMut(&[u8])) {
    // An exact, direct-mapped cache: collisions evict, never skip a distinct
    // gram. Its 3,328 stack bytes die with this chunk, regardless of text size.
    let mut cache = [CachedGram {
        bytes: [0; 12],
        length: 0,
    }; GRAM_CACHE_ENTRIES];
    for &width in widths {
        for (start, end) in gram_ranges(value, width) {
            let gram = &value.as_bytes()[start..end];
            let hash = gram.iter().fold(2_166_136_261_u32, |hash, byte| {
                (hash ^ u32::from(*byte)).wrapping_mul(16_777_619)
            });
            let slot = (hash ^ (hash >> 16)) as usize % GRAM_CACHE_ENTRIES;
            let cached = &mut cache[slot];
            if cached.length as usize == gram.len() && cached.bytes[..gram.len()] == *gram {
                continue;
            }
            cached.bytes[..gram.len()].copy_from_slice(gram);
            cached.length = gram.len() as u8;
            consume(gram);
        }
    }
}

fn visit_cached_grams(value: &str, consume: impl FnMut(&[u8])) {
    visit_gram_widths(value, &[2, 3], consume);
}

// The posting index also covers width-1 grams so single-character query words
// stay selective; the bloom path keeps its width-2/3 discipline.
fn visit_posting_grams(value: &str, consume: impl FnMut(&[u8])) {
    visit_gram_widths(value, &[1, 2, 3], consume);
}

// Read-modify-write one conversation-generation into one gram's packed
// posting list, charging the row growth against the owner budget. The first
// update that would push the owner past its budget stops emission with
// `tripped` set, and the caller durably marks the index DEGRADED so readers
// fall back to the exact full walk with complete results. Promoting a list
// to the universal marker is charged zero: the shrinkage stays reserved
// (conservative), which keeps every later removal delta-free as well.
#[allow(clippy::too_many_arguments)]
fn add_gram_posting(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    gram: &[u8; 32],
    generation_id: u64,
    base_bytes: u64,
    maximum_logical_bytes_per_owner: u64,
    gram_delta: &mut i64,
    tripped: &mut bool,
) -> io::Result<()> {
    let posting_key = key(transaction, GRAM_NAMESPACE, gram)?;
    let existing = database.entity_get(transaction, &posting_key)?;
    let had_row = existing.is_some();
    let (mut entries, present_bytes) = match existing {
        None => (Vec::new(), 0usize),
        Some(raw) => match decode_gram_list(&raw)? {
            GramList::Omni => return Ok(()),
            GramList::Entries(entries) => (entries, raw.len()),
        },
    };
    if entries.contains(&generation_id) {
        return Ok(());
    }
    let (value, value_bytes) = if entries.len() >= GRAM_LIST_MAX_ENTRIES {
        (vec![FORMAT_VERSION, 1], 2usize)
    } else {
        entries.push(generation_id);
        entries.sort_unstable();
        let value = encode_gram_entries(&entries)?;
        let value_bytes = value.len();
        (value, value_bytes)
    };
    // Exact telescoping: a row's lifetime charges sum to key + final value,
    // which is exactly what deleting the emptied row frees. Promoting to the
    // universal marker charges zero (the shrinkage stays reserved, and the
    // marker is terminal), so every asymmetry is conservative.
    let charge = if had_row {
        (value_bytes as i64 - present_bytes as i64).max(0) as u64
    } else {
        posting_key.key_bytes().len() as u64 + value_bytes as u64
    };
    if base_bytes
        .checked_add_signed(*gram_delta)
        .and_then(|n| n.checked_add(charge))
        .filter(|n| *n <= maximum_logical_bytes_per_owner)
        .is_none()
    {
        *tripped = true;
        return Ok(());
    }
    *gram_delta += charge as i64;
    database.entity_put(transaction, posting_key, value)?;
    Ok(())
}

// Retract one conversation-generation from one gram's packed posting list.
// Universal lists stay universal (conservative false candidates), missing
// rows are tolerated (partial emission), and an emptied list row is deleted.
fn remove_gram_posting(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    gram: &[u8; 32],
    generation_id: u64,
    gram_delta: &mut i64,
) -> io::Result<()> {
    let posting_key = key(transaction, GRAM_NAMESPACE, gram)?;
    let Some(raw) = database.entity_get(transaction, &posting_key)? else {
        return Ok(());
    };
    let GramList::Entries(mut entries) = decode_gram_list(&raw)? else {
        return Ok(());
    };
    let Ok(position) = entries.binary_search(&generation_id) else {
        return Ok(());
    };
    entries.remove(position);
    let key_bytes = posting_key.key_bytes().len() as i64;
    if entries.is_empty() {
        database.entity_delete(transaction, posting_key)?;
        *gram_delta -= key_bytes + raw.len() as i64;
    } else {
        let value = encode_gram_entries(&entries)?;
        *gram_delta -= (raw.len() - value.len()) as i64;
        database.entity_put(transaction, posting_key, value)?;
    }
    Ok(())
}

// Per-append emission state: dedups gram updates inside the call, enforces
// the write bound, and records whether the conversation overflowed into an
// exclusion or the owner budget tripped into a degradation.
struct GramEmission {
    excluded: bool,
    tripped: bool,
    ops: usize,
    seen: std::collections::HashSet<[u8; 32]>,
}

impl GramEmission {
    fn new() -> Self {
        Self {
            excluded: false,
            tripped: false,
            ops: 0,
            seen: std::collections::HashSet::new(),
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn emit_text_grams(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    lowered: &str,
    generation_digest: &[u8; 32],
    base_bytes: u64,
    maximum_logical_bytes_per_owner: u64,
    gram_delta: &mut i64,
    emission: &mut GramEmission,
) -> io::Result<()> {
    if emission.excluded || emission.tripped {
        return Ok(());
    }
    let generation_id = generation_dictionary::lookup(database, transaction, generation_digest)?
        .ok_or_else(|| invalid_data("search generation dictionary assignment is missing"))?;
    let mut grams = Vec::new();
    visit_posting_grams(lowered, |gram| grams.push(gram_digest(gram)));
    for gram in grams {
        if !emission.seen.insert(gram) {
            continue;
        }
        emission.ops += 1;
        if emission.ops > GRAM_APPEND_OP_CAP {
            let exclusion_key = key(transaction, GRAM_EXCLUSION_NAMESPACE, generation_digest)?;
            *gram_delta += exclusion_key.key_bytes().len() as i64;
            database.entity_put(transaction, exclusion_key, Vec::new())?;
            emission.excluded = true;
            return Ok(());
        }
        add_gram_posting(
            database,
            transaction,
            &gram,
            generation_id,
            base_bytes,
            maximum_logical_bytes_per_owner,
            gram_delta,
            &mut emission.tripped,
        )?;
        if emission.tripped {
            return Ok(());
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn retract_text_grams(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    lowered: &str,
    generation_digest: &[u8; 32],
    gram_delta: &mut i64,
    ops: &mut usize,
    seen: &mut std::collections::HashSet<[u8; 32]>,
) -> io::Result<()> {
    let generation_id = generation_dictionary::lookup(database, transaction, generation_digest)?
        .ok_or_else(|| invalid_data("search generation dictionary assignment is missing"))?;
    let mut grams = Vec::new();
    visit_posting_grams(lowered, |gram| grams.push(gram_digest(gram)));
    for gram in grams {
        if *ops >= GRAM_REMOVE_OP_CAP {
            return Ok(());
        }
        if !seen.insert(gram) {
            continue;
        }
        *ops += 1;
        remove_gram_posting(database, transaction, &gram, generation_id, gram_delta)?;
    }
    Ok(())
}

fn bloom_add(bloom: &mut [u8; BLOOM_BYTES], value: &str) {
    visit_cached_grams(value, |gram| {
        let hash = digest(b"tofu-db:turn-search:gram:v1\0", &[gram]);
        for index in 0..BLOOM_HASHES {
            let offset = index * 2;
            let bit =
                u16::from_be_bytes([hash[offset], hash[offset + 1]]) as usize % (BLOOM_BYTES * 8);
            bloom[bit / 8] |= 1 << (bit % 8);
        }
    });
}

fn bloom_query_bits(value: &str) -> Vec<usize> {
    let width = if value.chars().count() >= 3 { 3 } else { 2 };
    let mut bits = Vec::new();
    for (start, end) in gram_ranges(value, width) {
        let hash = digest(
            b"tofu-db:turn-search:gram:v1\0",
            &[&value.as_bytes()[start..end]],
        );
        for index in 0..BLOOM_HASHES {
            let offset = index * 2;
            let bit =
                u16::from_be_bytes([hash[offset], hash[offset + 1]]) as usize % (BLOOM_BYTES * 8);
            bits.push(bit);
        }
    }
    bits
}

fn bloom_might_contain(bloom: &[u8; BLOOM_BYTES], bits: &[usize]) -> bool {
    bits.iter()
        .all(|bit| bloom[bit / 8] & (1 << (bit % 8)) != 0)
}

fn usage_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(transaction, USAGE_NAMESPACE, USAGE_KEY)
}

fn partial_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let stored_key = key(transaction, USAGE_NAMESPACE, b"partial_conversations")?;
    match database.entity_get(transaction, &stored_key)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_be_bytes(raw.try_into().unwrap())),
        _ => Err(invalid_data("search coverage counter is malformed")),
    }
}

fn replace_partial(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    old: bool,
    new: bool,
) -> io::Result<()> {
    if old == new {
        return Ok(());
    }
    let count = partial_count(database, transaction)?;
    let count = count
        .checked_sub(u64::from(old))
        .and_then(|n| n.checked_add(u64::from(new)))
        .ok_or_else(|| invalid_data("search coverage counter overflow"))?;
    let stored_key = key(transaction, USAGE_NAMESPACE, b"partial_conversations")?;
    database.entity_put(transaction, stored_key, count.to_be_bytes().to_vec())
}

fn read_usage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    match database.entity_get(transaction, &usage_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_be_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("search projection usage is malformed")),
    }
}

fn write_usage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    value: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        usage_key(transaction)?,
        value.to_be_bytes().to_vec(),
    )
}

impl OpenTurnSearchProjection {
    /// Fence all owners before publishing the projection to daemon/embedded
    /// request dispatch. Maintenance validates each source binding and refreshes
    /// coverage under the projection mutex before admitting that owner. No
    /// authority scan or per-owner startup work is performed on the ready path.
    pub(crate) fn require_source_admission(&mut self, source: uuid::Uuid) {
        self.admission_source = Some(source);
        self.admitted_owners.clear();
    }

    pub(crate) fn owner_admitted(&self, tenant: u64, owner: u64) -> bool {
        self.admission_source.is_none() || self.admitted_owners.contains(&(tenant, owner))
    }

    /// Graceful-stop hook mirroring the main authority: merge any pending
    /// overlay deltas, then retire the projection engine's active WAL
    /// window so the next daemon open does not replay it.
    pub(crate) fn checkpoint_wal_window_for_shutdown(&mut self) -> io::Result<u64> {
        self.database.merge_entity_overlay_fully()?;
        self.database.checkpoint_wal_window()
    }

    pub(crate) fn owner_binding_in(
        &self,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<Vec<u8>> {
        let stored_key = key(transaction, "search_format", b"version")?;
        let value = self
            .database
            .entity_get(transaction, &stored_key)?
            .unwrap_or_default();
        Ok(value.get(2..).unwrap_or_default().to_vec())
    }

    pub(crate) fn owner_indexing(&self, tenant: u64, owner: u64) -> io::Result<bool> {
        if !self.owner_admitted(tenant, owner) {
            return Ok(true);
        }
        let mut transaction = self.database.begin(tenant, owner)?;
        let stored_key = key(&transaction, USAGE_NAMESPACE, b"indexing")?;
        Ok(self
            .database
            .entity_get(&mut transaction, &stored_key)?
            .as_deref()
            != Some(&[0]))
    }

    pub(crate) fn set_owner_indexing(
        &mut self,
        tenant: u64,
        owner: u64,
        indexing: bool,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let stored_key = key(&transaction, USAGE_NAMESPACE, b"indexing")?;
        if let Some(source) = self.admission_source {
            let format_key = key(&transaction, "search_format", b"version")?;
            let marker = self.database.entity_get(&mut transaction, &format_key)?;
            if !marker.as_ref().is_some_and(|bytes| {
                bytes.len() == 34
                    && bytes[0] == FORMAT_VERSION
                    && bytes[1] == 1
                    && bytes[2..18] == *source.as_bytes()
            }) || read_gram_state(&self.database, &mut transaction)?.is_none()
            {
                return Err(invalid_data(
                    "search source has not been prepared for this launch",
                ));
            }
        }
        let value = vec![u8::from(indexing)];
        if self
            .database
            .entity_get(&mut transaction, &stored_key)?
            .as_ref()
            == Some(&value)
        {
            self.admitted_owners.insert((tenant, owner));
            return Ok(());
        }
        self.database
            .entity_put(&mut transaction, stored_key, value)?;
        self.database.commit(transaction)?;
        self.admitted_owners.insert((tenant, owner));
        Ok(())
    }

    pub(crate) fn is_published(
        &self,
        tenant: u64,
        owner: u64,
        conversation: &str,
        generation: &str,
    ) -> io::Result<bool> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let stored_key = active_key(&transaction, conversation)?;
        let Some(raw) = self.database.entity_get(&mut transaction, &stored_key)? else {
            return Ok(false);
        };
        let header = decode_header(&raw)?;
        if header.conversation_id != conversation {
            return Err(invalid_data("search header identity mismatch"));
        }
        Ok(header.generation == generation)
    }
    pub(crate) fn source_incomplete(&self, tenant: u64, owner: u64) -> io::Result<bool> {
        if !self.owner_admitted(tenant, owner) {
            return Ok(false);
        }
        let mut transaction = self.database.begin(tenant, owner)?;
        Ok(partial_count(&self.database, &mut transaction)? > 0)
    }

    pub(crate) fn mark_rebuild_incomplete(
        &mut self,
        tenant: u64,
        owner: u64,
        conversation: &str,
        generation: &str,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let stored_key = build_key(&transaction, conversation)?;
        let raw = self
            .database
            .entity_get(&mut transaction, &stored_key)?
            .ok_or_else(|| invalid_input("search rebuild was not begun"))?;
        let mut build = decode_build(&raw)?;
        if build.conversation_id != conversation || build.generation != generation {
            return Err(invalid_input("search rebuild generation is stale"));
        }
        if build.source_incomplete {
            return Ok(());
        }
        build.source_incomplete = true;
        self.database
            .entity_put(&mut transaction, stored_key, encode_build(&build)?)?;
        self.database.commit(transaction)?;
        Ok(())
    }
    /// Reset only this owner's disposable projection on a source/activation
    /// mismatch. Pending tokens survive retries; acknowledged tokens are reusable
    /// only while the authority still names them as its active queue consumer.
    /// Returns (activation, needs_authority_acknowledgement).
    pub(crate) fn prepare_owner(
        &mut self,
        tenant: u64,
        owner: u64,
        source: uuid::Uuid,
        authority_activation: Option<uuid::Uuid>,
    ) -> io::Result<(uuid::Uuid, bool)> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let format_key = key(&transaction, "search_format", b"version")?;
        match self.database.entity_get(&mut transaction, &format_key)? {
            Some(bytes)
                if bytes.len() == 34
                    && bytes[0] == FORMAT_VERSION
                    && bytes[2..18] == *source.as_bytes() =>
            {
                // A marker without gram-index state predates the posting
                // index: fall through to the same versioned wipe so the next
                // backfill rebuilds every conversation with postings.
                if read_gram_state(&self.database, &mut transaction)?.is_some() {
                    let activation = uuid::Uuid::from_slice(&bytes[18..])
                        .map_err(|_| invalid_data("invalid search activation"))?;
                    match bytes[1] {
                        0 => return Ok((activation, true)),
                        1 if authority_activation == Some(activation) => {
                            return Ok((activation, false));
                        }
                        1 => {} // Another projection consumed shared dirty work: fence this one.
                        _ => return Err(invalid_data("invalid search format state")),
                    }
                }
            }
            // Legacy UUID-only markers cannot prove queue-consumer continuity.
            Some(bytes) if [18, 34].contains(&bytes.len())
                && matches!(bytes[0], 2 | 3 | FORMAT_VERSION)
                && bytes[1] <= 1 => {}
            Some(_) => return Err(invalid_data("unsupported search projection format marker")),
            None => {}
        }
        self.admitted_owners.remove(&(tenant, owner));
        let activation = uuid::Uuid::new_v4();
        for namespace in [
            HEADER_NAMESPACE,
            ACTIVE_NAMESPACE,
            BUILD_NAMESPACE,
            TURN_META_NAMESPACE,
            TURN_TEXT_NAMESPACE,
            USAGE_NAMESPACE,
            GRAM_NAMESPACE,
            GRAM_EXCLUSION_NAMESPACE,
            generation_dictionary::NAMESPACE,
        ] {
            let (start, end) = EntityKey::prefix_range(tenant, owner, namespace, b"")?;
            self.database
                .entity_retire_range(&mut transaction, &start, &end)?;
        }
        self.database.entity_put(
            &mut transaction,
            format_key,
            [
                &[FORMAT_VERSION, 0],
                source.as_bytes().as_slice(),
                activation.as_bytes().as_slice(),
            ]
            .concat(),
        )?;
        let gram_key = gram_state_key(&transaction)?;
        self.database
            .entity_put(&mut transaction, gram_key, vec![GRAM_STATE_BUILDING])?;
        self.database.commit(transaction)?;
        Ok((activation, true))
    }

    #[cfg(test)]
    pub(crate) fn gram_index_state_for_test(
        &self,
        tenant: u64,
        owner: u64,
    ) -> io::Result<Option<u8>> {
        let mut transaction = self.database.begin(tenant, owner)?;
        read_gram_state(&self.database, &mut transaction)
    }

    /// Publish the posting index once the first complete backfill has drained.
    /// Only the BUILDING state advances; a DEGRADED index stays degraded until
    /// the next versioned wipe, and a legacy owner without state is untouched
    /// (its prepare_owner wipe writes BUILDING first).
    pub(crate) fn mark_gram_index_ready(&mut self, tenant: u64, owner: u64) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        if read_gram_state(&self.database, &mut transaction)? == Some(GRAM_STATE_BUILDING) {
            let stored_key = gram_state_key(&transaction)?;
            self.database
                .entity_put(&mut transaction, stored_key, vec![GRAM_STATE_READY])?;
            self.database.commit(transaction)?;
        }
        Ok(())
    }

    pub(crate) fn acknowledge_owner_backfill(
        &mut self,
        tenant: u64,
        owner: u64,
        source: uuid::Uuid,
        activation: uuid::Uuid,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let format_key = key(&transaction, "search_format", b"version")?;
        let mut marker = self
            .database
            .entity_get(&mut transaction, &format_key)?
            .ok_or_else(|| invalid_data("search activation was not prepared"))?;
        if marker.len() != 34
            || marker[0] != FORMAT_VERSION
            || marker[1] > 1
            || marker[2..18] != *source.as_bytes()
            || marker[18..] != *activation.as_bytes()
        {
            return Err(invalid_data("search activation acknowledgement is stale"));
        }
        marker[1] = 1;
        self.database
            .entity_put(&mut transaction, format_key, marker)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub(crate) fn set_conversation_title(
        &mut self,
        tenant: u64,
        owner: u64,
        conversation: &str,
        generation: &str,
        title: &str,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let build_key = build_key(&transaction, conversation)?;
        let raw = self
            .database
            .entity_get(&mut transaction, &build_key)?
            .ok_or_else(|| invalid_input("search rebuild was not begun"))?;
        let mut build = decode_build(&raw)?;
        if build.conversation_id != conversation || build.generation != generation {
            return Err(invalid_input("search rebuild generation is stale"));
        }
        let old_title_bytes = build.title.len() as u64 * 3;
        let new_title_bytes = title.len() as u64 * 3;
        let next_usage = read_usage(&self.database, &mut transaction)?
            .checked_sub(old_title_bytes)
            .and_then(|n| n.checked_add(new_title_bytes))
            .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| exhausted("search title exceeds its resource budget"))?;
        build.generation_bytes = build
            .generation_bytes
            .checked_sub(old_title_bytes)
            .and_then(|n| n.checked_add(new_title_bytes))
            .ok_or_else(|| invalid_data("search header usage overflow"))?;
        build.title = title.to_owned();
        write_usage(&self.database, &mut transaction, next_usage)?;
        self.database
            .entity_put(&mut transaction, build_key, encode_build(&build)?)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn initialize(data_dir: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database = AuthorityDatabase::initialize(data_dir)?;
                // The projection is a derived index rebuilt in the
                // background: the synchronous tree path serves it better
                // than the foreground write overlay.
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
        })
    }

    pub fn open(data_dir: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database = recovery::open(data_dir).map_err(|error| {
                    io::Error::new(
                        error.kind(),
                        format!("search projection startup failed: {error}"),
                    )
                })?;
                // Reopens use the same synchronous tree path as fresh indexes
                // and the fault-injected VFS owner, never the source overlay.
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
        })
    }

    pub fn initialize_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database = AuthorityDatabase::initialize_with_vfs(data_dir, vfs)?;
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
        })
    }

    pub fn open_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database = AuthorityDatabase::open_with_vfs(data_dir, vfs)?;
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
        })
    }

    pub fn begin_conversation_rebuild(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
        updated_at_ms: u64,
    ) -> io::Result<()> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        validate_identity(
            generation,
            "invalid search generation",
            MAX_GENERATION_BYTES,
        )?;
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let build_key = build_key(&transaction, conversation_id)?;
        let mut usage = read_usage(&self.database, &mut transaction)?;
        if let Some(raw) = self.database.entity_get(&mut transaction, &build_key)? {
            let previous = decode_build(&raw)?;
            if previous.conversation_id != conversation_id {
                return Err(invalid_data("search build digest collision"));
            }
            usage = usage
                .checked_sub(previous.generation_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            let mut gram_delta = 0i64;
            self.retire_generation_postings(
                &mut transaction,
                conversation_id,
                &previous.generation,
                None,
                &mut gram_delta,
            )?;
            usage = usage
                .checked_add_signed(gram_delta)
                .ok_or_else(|| invalid_data("search gram posting usage underflow"))?;
            for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE] {
                let (start, end) = generation_range(
                    &transaction,
                    namespace,
                    conversation_id,
                    &previous.generation,
                )?;
                self.database
                    .entity_retire_range(&mut transaction, &start, &end)?;
            }
        }
        let mut build = Build {
            conversation_id: conversation_id.to_owned(),
            generation: generation.to_owned(),
            updated_at_ms,
            generation_bytes: 0,
            title: String::new(),
            source_incomplete: false,
        };
        build.generation_bytes =
            reserved_header_bytes(encode_build(&build)?.len(), conversation_id.len())?;
        usage = usage
            .checked_add(build.generation_bytes)
            .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| exhausted("search header exceeds its resource budget"))?;
        let (_, dictionary_bytes) = generation_dictionary::allocate(
            &self.database, &mut transaction, &generation_digest(conversation_id, generation),
            self.maximum_logical_bytes_per_owner - usage,
        )?;
        usage = usage.checked_add(dictionary_bytes)
            .ok_or_else(|| exhausted("search dictionary usage overflow"))?;
        self.database
            .entity_put(&mut transaction, build_key, encode_build(&build)?)?;
        write_usage(&self.database, &mut transaction, usage)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn append_conversation_page(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
        documents: &[TurnSearchDocument],
    ) -> io::Result<()> {
        if documents.len() > SOURCE_PAGE_ROWS {
            return Err(invalid_input("search projection page exceeds eight turns"));
        }
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let build_key = build_key(&transaction, conversation_id)?;
        let raw = self
            .database
            .entity_get(&mut transaction, &build_key)?
            .ok_or_else(|| invalid_input("search rebuild was not begun"))?;
        let mut build = decode_build(&raw)?;
        if build.conversation_id != conversation_id || build.generation != generation {
            return Err(invalid_input("search rebuild generation is stale"));
        }
        let usage = read_usage(&self.database, &mut transaction)?;
        let gram_state = read_gram_state(&self.database, &mut transaction)?;
        let generation_digest = generation_digest(conversation_id, generation);
        let mut emission = GramEmission::new();
        if gram_state != Some(GRAM_STATE_DEGRADED) {
            let exclusion_key = key(&transaction, GRAM_EXCLUSION_NAMESPACE, &generation_digest)?;
            emission.excluded = self
                .database
                .entity_get(&mut transaction, &exclusion_key)?
                .is_some();
        } else {
            emission.excluded = true;
        }
        let mut gram_delta = 0i64;
        let mut added_bytes = 0u64;
        for document in documents {
            validate_identity(&document.turn_id, "invalid search turn ID", MAX_ID_BYTES)?;
            if document.search_text.len() > MAX_SEARCH_TEXT_BYTES
                || document.search_text.chars().count() > CHUNK_CHARACTERS
                || !matches!(document.role.as_str(), "user" | "assistant")
                || !matches!(
                    document.match_field.as_str(),
                    "content" | "thinking" | "originalContent" | "translatedContent"
                )
            {
                return Err(invalid_input("invalid bounded search chunk"));
            }
            let lowered = document.search_text.to_lowercase();
            let mut bloom = [0; BLOOM_BYTES];
            bloom_add(&mut bloom, &lowered);
            let chunks: Vec<&[u8]> = document
                .search_text
                .as_bytes()
                .chunks(TEXT_CHUNK_BYTES)
                .collect();
            let chunk_count = u16::try_from(chunks.len())
                .map_err(|_| invalid_input("search text has too many chunks"))?;
            let meta = TurnMeta {
                conversation_id: conversation_id.to_owned(),
                generation: generation.to_owned(),
                turn_id: document.turn_id.clone(),
                ordinal: document.ordinal,
                chunk_count,
                bloom,
                chunk_index: document.chunk_index,
                role: document.role.clone(),
                match_field: document.match_field.clone(),
            };
            let meta_key = turn_meta_key(
                &transaction,
                conversation_id,
                generation,
                document.ordinal,
                &document.turn_id,
                document.chunk_index,
            )?;
            if self
                .database
                .entity_get(&mut transaction, &meta_key)?
                .is_some()
            {
                return Err(invalid_input("search projection page repeats a turn"));
            }
            let meta_bytes = encode_meta(&meta)?;
            added_bytes = added_bytes
                .checked_add((meta_key.key_bytes().len() + meta_bytes.len()) as u64)
                .ok_or_else(|| exhausted("search projection usage overflow"))?;
            self.database
                .entity_put(&mut transaction, meta_key, meta_bytes)?;
            for (index, chunk) in chunks.into_iter().enumerate() {
                let chunk_key = turn_text_key(
                    &transaction,
                    conversation_id,
                    generation,
                    document.ordinal,
                    &document.turn_id,
                    document.chunk_index,
                    index as u16,
                )?;
                added_bytes = added_bytes
                    .checked_add((chunk_key.key_bytes().len() + chunk.len()) as u64)
                    .ok_or_else(|| exhausted("search projection usage overflow"))?;
                self.database
                    .entity_put(&mut transaction, chunk_key, chunk.to_vec())?;
            }
            emit_text_grams(
                &self.database,
                &mut transaction,
                &lowered,
                &generation_digest,
                usage
                    .checked_add(added_bytes)
                    .ok_or_else(|| exhausted("search projection usage overflow"))?,
                self.maximum_logical_bytes_per_owner,
                &mut gram_delta,
                &mut emission,
            )?;
        }
        if emission.tripped {
            let stored_key = gram_state_key(&transaction)?;
            self.database.entity_put(
                &mut transaction,
                stored_key,
                vec![GRAM_STATE_DEGRADED],
            )?;
        }
        let next_usage = usage
            .checked_add(added_bytes)
            .and_then(|n| n.checked_add_signed(gram_delta))
            .filter(|value| *value <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| {
                exhausted("conversation search projection reached its resource budget")
            })?;
        build.generation_bytes = build
            .generation_bytes
            .checked_add(added_bytes)
            .ok_or_else(|| exhausted("search projection generation size overflow"))?;
        self.database
            .entity_put(&mut transaction, build_key, encode_build(&build)?)?;
        write_usage(&self.database, &mut transaction, next_usage)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn finalize_conversation_rebuild(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let build_key = build_key(&transaction, conversation_id)?;
        let build = decode_build(
            &self
                .database
                .entity_get(&mut transaction, &build_key)?
                .ok_or_else(|| invalid_input("search rebuild was not begun"))?,
        )?;
        if build.conversation_id != conversation_id || build.generation != generation {
            return Err(invalid_input("search rebuild generation is stale"));
        }
        let mut usage = read_usage(&self.database, &mut transaction)?;
        let active_key = active_key(&transaction, conversation_id)?;
        let mut old_incomplete = false;
        if let Some(raw) = self.database.entity_get(&mut transaction, &active_key)? {
            let old = decode_header(&raw)?;
            old_incomplete = old.source_incomplete;
            if old.conversation_id != conversation_id {
                return Err(invalid_data("search active-header digest collision"));
            }
            let old_header_key = header_key(&transaction, old.updated_at_ms, conversation_id)?;
            self.database
                .entity_delete(&mut transaction, old_header_key)?;
            usage = usage
                .checked_sub(old.generation_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            let mut gram_delta = 0i64;
            self.retire_generation_postings(
                &mut transaction,
                conversation_id,
                &old.generation,
                Some(&old.title),
                &mut gram_delta,
            )?;
            usage = usage
                .checked_add_signed(gram_delta)
                .ok_or_else(|| invalid_data("search gram posting usage underflow"))?;
            for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE] {
                let (start, end) =
                    generation_range(&transaction, namespace, conversation_id, &old.generation)?;
                self.database
                    .entity_retire_range(&mut transaction, &start, &end)?;
            }
        }
        replace_partial(
            &self.database,
            &mut transaction,
            old_incomplete,
            build.source_incomplete,
        )?;
        // Title and identity postings publish atomically with the generation
        // they describe (document postings were emitted page by page).
        let generation_digest = generation_digest(conversation_id, generation);
        let mut gram_delta = 0i64;
        if read_gram_state(&self.database, &mut transaction)? != Some(GRAM_STATE_DEGRADED) {
            let exclusion_key = key(&transaction, GRAM_EXCLUSION_NAMESPACE, &generation_digest)?;
            let mut emission = GramEmission::new();
            emission.excluded = self
                .database
                .entity_get(&mut transaction, &exclusion_key)?
                .is_some();
            for lowered in [
                build.title.to_lowercase(),
                conversation_id.to_lowercase(),
            ] {
                emit_text_grams(
                    &self.database,
                    &mut transaction,
                    &lowered,
                    &generation_digest,
                    usage,
                    self.maximum_logical_bytes_per_owner,
                    &mut gram_delta,
                    &mut emission,
                )?;
                if emission.tripped {
                    let stored_key = gram_state_key(&transaction)?;
                    self.database.entity_put(
                        &mut transaction,
                        stored_key,
                        vec![GRAM_STATE_DEGRADED],
                    )?;
                    break;
                }
            }
        }
        usage = usage
            .checked_add_signed(gram_delta)
            .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| exhausted("search gram postings exceed their resource budget"))?;
        let header = Header {
            conversation_id: conversation_id.to_owned(),
            generation: generation.to_owned(),
            updated_at_ms: build.updated_at_ms,
            generation_bytes: build.generation_bytes,
            title: build.title.clone(),
            source_incomplete: build.source_incomplete,
            has_chunks: false, // Encoding only; derived when the header is read.
        };
        let new_header_key = header_key(&transaction, build.updated_at_ms, conversation_id)?;
        let encoded_header = encode_header(&header)?;
        self.database
            .entity_put(&mut transaction, new_header_key, encoded_header.clone())?;
        self.database
            .entity_put(&mut transaction, active_key, encoded_header)?;
        self.database.entity_delete(&mut transaction, build_key)?;
        write_usage(&self.database, &mut transaction, usage)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn remove_conversation(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
    ) -> io::Result<bool> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let active_key = active_key(&transaction, conversation_id)?;
        let active = self.database.entity_get(&mut transaction, &active_key)?;
        let mut removed = false;
        let mut usage = read_usage(&self.database, &mut transaction)?;
        if let Some(raw) = active {
            let header = decode_header(&raw)?;
            replace_partial(
                &self.database,
                &mut transaction,
                header.source_incomplete,
                false,
            )?;
            if header.conversation_id != conversation_id {
                return Err(invalid_data("search active-header digest collision"));
            }
            let candidate_key = header_key(&transaction, header.updated_at_ms, conversation_id)?;
            self.database
                .entity_delete(&mut transaction, candidate_key)?;
            self.database.entity_delete(&mut transaction, active_key)?;
            usage = usage
                .checked_sub(header.generation_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            let mut gram_delta = 0i64;
            self.retire_generation_postings(
                &mut transaction,
                conversation_id,
                &header.generation,
                Some(&header.title),
                &mut gram_delta,
            )?;
            usage = usage
                .checked_add_signed(gram_delta)
                .ok_or_else(|| invalid_data("search gram posting usage underflow"))?;
            for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE] {
                let (range_start, range_end) =
                    generation_range(&transaction, namespace, conversation_id, &header.generation)?;
                self.database
                    .entity_retire_range(&mut transaction, &range_start, &range_end)?;
            }
            removed = true;
        }
        if removed {
            write_usage(&self.database, &mut transaction, usage)?;
            self.database.commit(transaction)?;
        }
        Ok(removed)
    }

    /// Retract every gram posting of one generation: its exclusion marker
    /// (always, so markers never outlive their generation), its title/id
    /// postings (emitted at finalize, so only active generations have them),
    /// and the per-document postings recomputed from each reassembled chunk
    /// text. Runs inside the same transaction that retires the generation's
    /// meta and text ranges, so postings and content move atomically. A
    /// DEGRADED index keeps its frozen rows and their usage (the next
    /// versioned wipe reclaims both); the write bound may leave charged
    /// leftovers, which only ever become false candidates.
    fn retire_generation_postings(
        &self,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        generation: &str,
        title: Option<&str>,
        gram_delta: &mut i64,
    ) -> io::Result<()> {
        let generation_digest = generation_digest(conversation_id, generation);
        let exclusion_key = key(transaction, GRAM_EXCLUSION_NAMESPACE, &generation_digest)?;
        if self
            .database
            .entity_get(transaction, &exclusion_key)?
            .is_some()
        {
            *gram_delta -= exclusion_key.key_bytes().len() as i64;
            self.database.entity_delete(transaction, exclusion_key)?;
        }
        // Retraction always runs, even for a DEGRADED index: it alone keeps
        // stored rows and charged usage in lockstep (emission is what the
        // degraded state gates). Only the transaction write bound may leave
        // charged leftovers behind.
        let mut ops = 0usize;
        let mut seen = std::collections::HashSet::new();
        if let Some(title) = title {
            retract_text_grams(
                &self.database,
                transaction,
                &title.to_lowercase(),
                &generation_digest,
                gram_delta,
                &mut ops,
                &mut seen,
            )?;
            retract_text_grams(
                &self.database,
                transaction,
                &conversation_id.to_lowercase(),
                &generation_digest,
                gram_delta,
                &mut ops,
                &mut seen,
            )?;
        }
        let (mut start, end) = generation_range(
            transaction,
            TURN_META_NAMESPACE,
            conversation_id,
            generation,
        )?;
        loop {
            let rows =
                self.database
                    .entity_scan(transaction, &start, &end, GRAM_RETIRE_SCAN_BATCH)?;
            let row_count = rows.len();
            for (_, raw) in &rows {
                let meta = decode_meta(raw)?;
                if meta.conversation_id != conversation_id || meta.generation != generation {
                    return Err(invalid_data("search generation digest collision"));
                }
                // Text chunk loads spend point witnesses from the same
                // per-transaction budget as the retraction reads; charge them
                // so a gram-diverse mega generation stops with charged
                // leftovers instead of failing its publish.
                ops = ops.saturating_add(usize::from(meta.chunk_count));
                let text = self.load_text(transaction, &meta)?;
                retract_text_grams(
                    &self.database,
                    transaction,
                    &text.to_lowercase(),
                    &generation_digest,
                    gram_delta,
                    &mut ops,
                    &mut seen,
                )?;
            }
            if row_count < GRAM_RETIRE_SCAN_BATCH {
                *gram_delta -= generation_dictionary::retire(
                    &self.database, transaction, &generation_digest,
                )? as i64;
                return Ok(());
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = key(transaction, TURN_META_NAMESPACE, &next)?;
        }
    }

    fn load_text(
        &self,
        transaction: &mut AuthorityTransaction,
        meta: &TurnMeta,
    ) -> io::Result<String> {
        let mut text = Vec::new();
        for chunk in 0..meta.chunk_count {
            let chunk_key = turn_text_key(
                transaction,
                &meta.conversation_id,
                &meta.generation,
                meta.ordinal,
                &meta.turn_id,
                meta.chunk_index,
                chunk,
            )?;
            let bytes = self
                .database
                .entity_get(transaction, &chunk_key)?
                .ok_or_else(|| invalid_data("search projection text chunk is missing"))?;
            text.extend_from_slice(&bytes);
            if text.len() > MAX_SEARCH_TEXT_BYTES {
                return Err(invalid_data("search projection text exceeds its bound"));
            }
        }
        String::from_utf8(text).map_err(|_| invalid_data("search projection text is not UTF-8"))
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn cached_grams_preserve_exact_bloom_bits_with_unicode_and_collisions() {
        fn reference(value: &str) -> [u8; super::BLOOM_BYTES] {
            let chars: Vec<char> = value.chars().collect();
            let mut bloom = [0_u8; super::BLOOM_BYTES];
            for width in [2, 3] {
                for window in chars.windows(width) {
                    let gram: String = window.iter().collect();
                    let hash = super::digest(b"tofu-db:turn-search:gram:v1\0", &[gram.as_bytes()]);
                    for index in 0..super::BLOOM_HASHES {
                        let bit = u16::from_be_bytes([hash[index * 2], hash[index * 2 + 1]])
                            as usize
                            % (super::BLOOM_BYTES * 8);
                        bloom[bit / 8] |= 1 << (bit % 8);
                    }
                }
            }
            bloom
        }
        let mixed: String = (0..20_000)
            .map(|i| ['a', '中', '🙂', '\0', '\u{301}', 'Ω', '\n', 'z'][(i * i + i / 7) % 8])
            .collect();
        for value in [
            String::new(),
            "a".into(),
            "中🙂".into(),
            "0123456789abcdef".repeat(1_000),
            mixed,
        ] {
            let mut actual = [0_u8; super::BLOOM_BYTES];
            super::bloom_add(&mut actual, &value);
            assert_eq!(actual, reference(&value));
        }
    }

    #[test]
    fn repetitive_search_text_has_bounded_scratch_and_hash_work() {
        assert!(std::mem::size_of::<super::CachedGram>() * super::GRAM_CACHE_ENTRIES <= 4096);
        let value = "0123456789abcdef".repeat(1_000);
        let mut hashes = 0;
        super::visit_cached_grams(&value, |_| hashes += 1);
        assert!(
            hashes < value.len() / 4,
            "repeated grams were hashed {hashes} times"
        );
    }

    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::sync::Arc;
    use tempfile::tempdir;

    const BUDGET: u64 = 16 * 1024 * 1024;

    fn document(turn_id: &str, ordinal: u64, text: &str) -> TurnSearchDocument {
        TurnSearchDocument {
            turn_id: turn_id.to_owned(),
            ordinal,
            search_text: text.to_owned(),
            chunk_index: 0,
            role: "user".into(),
            match_field: "content".into(),
        }
    }

    fn request(owner: u64, query: &str) -> ConversationSearchRequest {
        ConversationSearchRequest {
            tenant_id: 7,
            owner_user_id: owner,
            query: query.to_owned(),
            limit: 50,
            snippet_radius: 10,
        }
    }

    fn replace(
        projection: &mut OpenTurnSearchProjection,
        owner: u64,
        conversation: &str,
        generation: &str,
        updated_at: u64,
        documents: &[TurnSearchDocument],
    ) {
        projection
            .begin_conversation_rebuild(7, owner, conversation, generation, updated_at)
            .unwrap();
        for page in documents.chunks(8) {
            projection
                .append_conversation_page(7, owner, conversation, generation, page)
                .unwrap();
        }
        projection
            .finalize_conversation_rebuild(7, owner, conversation, generation)
            .unwrap();
    }

    #[test]
    fn startup_admission_blocks_foreign_source_before_any_worker_runs() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let other_source = uuid::Uuid::new_v4();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, activation)
            .unwrap();
        replace(
            &mut projection,
            11,
            "secret",
            "g1",
            100,
            &[document("t1", 1, "old secret")],
        );
        projection.set_owner_indexing(7, 11, false).unwrap();
        assert_eq!(projection.search(&request(11, "secret")).unwrap().len(), 1);

        projection.require_source_admission(other_source);
        assert!(projection
            .search(&request(11, "secret"))
            .unwrap()
            .is_empty());
        assert!(projection.owner_indexing(7, 11).unwrap());
        assert!(projection.set_owner_indexing(7, 11, false).is_err());
        let (activation, pending) = projection.prepare_owner(7, 11, other_source, None).unwrap();
        assert!(pending);
        projection
            .acknowledge_owner_backfill(7, 11, other_source, activation)
            .unwrap();
        projection.set_owner_indexing(7, 11, true).unwrap();
        assert!(projection
            .search(&request(11, "secret"))
            .unwrap()
            .is_empty());
        replace(
            &mut projection,
            11,
            "new",
            "g2",
            200,
            &[document("t2", 1, "new phrase")],
        );
        projection.set_owner_indexing(7, 11, false).unwrap();
        assert_eq!(
            projection.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
        assert!(!projection.owner_indexing(7, 11).unwrap());
    }

    #[test]
    fn startup_admission_rechecks_stale_ready_flags_per_owner() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let mut activations = Vec::new();
        for owner in [11, 22] {
            let (activation, _) = projection.prepare_owner(7, owner, source, None).unwrap();
            projection
                .acknowledge_owner_backfill(7, owner, source, activation)
                .unwrap();
            activations.push(activation);
            replace(
                &mut projection,
                owner,
                "shared-id",
                "g1",
                100,
                &[document("t1", 1, "needle")],
            );
            projection.set_owner_indexing(7, owner, false).unwrap();
        }
        projection.checkpoint_wal_window_for_shutdown().unwrap();
        drop(projection);
        let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        projection.require_source_admission(source);
        for owner in [11, 22] {
            assert!(projection.owner_indexing(7, owner).unwrap());
            assert!(projection
                .search(&request(owner, "needle"))
                .unwrap()
                .is_empty());
        }
        assert!(
            !projection
                .prepare_owner(7, 11, source, Some(activations[0]))
                .unwrap()
                .1
        );
        projection.set_owner_indexing(7, 11, true).unwrap();
        assert_eq!(projection.search(&request(11, "needle")).unwrap().len(), 1);
        assert!(projection.owner_indexing(7, 11).unwrap());
        assert!(projection
            .search(&request(22, "needle"))
            .unwrap()
            .is_empty());
        projection.set_owner_indexing(7, 11, false).unwrap();
        assert!(!projection.owner_indexing(7, 11).unwrap());
        projection.set_owner_indexing(7, 22, false).unwrap();
        assert!(!projection.owner_indexing(7, 22).unwrap());
    }

    #[test]
    fn phrase_multi_turn_and_owner_order_semantics_match_contract() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "older",
            "g1",
            100,
            &[document("t1", 1, "the quick brown fox")],
        );
        replace(
            &mut projection,
            11,
            "newer",
            "g2",
            200,
            &[
                document("t2", 1, "alpha here"),
                document("t3", 2, "gamma there"),
            ],
        );
        replace(
            &mut projection,
            12,
            "private",
            "g3",
            300,
            &[document("t4", 1, "fox")],
        );

        let fox = projection.search(&request(11, "FOX")).unwrap();
        assert_eq!(
            fox.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["older"]
        );
        assert!(fox[0].snippet.contains("fox"));
        let across_turns = projection.search(&request(11, "alpha gamma")).unwrap();
        assert_eq!(
            across_turns,
            [ConversationSearchHit {
                id: "newer".to_owned(),
                snippet: "…alpha here…".to_owned(),
                role: Some("user".into()),
                match_field: "content".into()
            }]
        );
        assert!(projection.search(&request(11, "a")).unwrap().is_empty());
    }

    #[test]
    fn generation_is_invisible_until_atomic_publish_and_retracts_old_text() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "conversation",
            "old",
            100,
            &[document("t1", 1, "old phrase")],
        );
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "new", 200)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "new",
                &[document("t1", 1, "new phrase")],
            )
            .unwrap();
        assert_eq!(
            projection.search(&request(11, "old phrase")).unwrap().len(),
            1
        );
        assert!(projection
            .search(&request(11, "new phrase"))
            .unwrap()
            .is_empty());
        projection
            .finalize_conversation_rebuild(7, 11, "conversation", "new")
            .unwrap();
        assert!(projection
            .search(&request(11, "old phrase"))
            .unwrap()
            .is_empty());
        assert_eq!(
            projection.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
        drop(projection);
        let reopened = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        assert_eq!(
            reopened.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
    }

    #[test]
    fn phrase_pass_precedes_word_pass_and_equal_timestamps_sort_id_descending() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "z-word-only",
            "g1",
            300,
            &[
                document("t1", 1, "alpha apart"),
                document("t2", 2, "gamma apart"),
            ],
        );
        replace(
            &mut projection,
            11,
            "a-phrase",
            "g2",
            100,
            &[document("t3", 1, "literal alpha gamma phrase")],
        );
        for id in ["a", "aa", "b"] {
            replace(
                &mut projection,
                11,
                id,
                id,
                200,
                &[document(id, 1, "same_time_literal_%")],
            );
        }
        let mixed = projection.search(&request(11, "alpha gamma")).unwrap();
        assert_eq!(
            mixed.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["a-phrase", "z-word-only"]
        );
        let tied = projection.search(&request(11, "literal_%")).unwrap();
        assert_eq!(
            tied.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["b", "aa", "a"]
        );
    }

    #[test]
    fn query_deadline_stops_before_projection_scan() {
        let directory = tempfile::tempdir().unwrap();
        let projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let error = projection
            .search_until(&request(7, "deadline"), || true)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn logical_budget_rejects_page_without_exposing_it() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), 1024).unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g1", 100)
            .unwrap();
        let error = projection
            .append_conversation_page(7, 11, "conversation", "g1", &[document("t1", 1, "bounded")])
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
        assert!(projection
            .search(&request(11, "bounded"))
            .unwrap()
            .is_empty());
    }

    #[test]
    fn owner_activation_fences_old_index_without_rebuilding_unchanged_launches() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (a, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, a)
            .unwrap();
        replace(
            &mut projection,
            11,
            "old",
            "g",
            1,
            &[document("t", 1, "oldneedle")],
        );
        let (other, _) = projection.prepare_owner(7, 22, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 22, source, other)
            .unwrap();
        replace(
            &mut projection,
            22,
            "other",
            "g",
            1,
            &[document("t", 1, "otherneedle")],
        );
        drop(projection);
        let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        assert_eq!(
            projection.prepare_owner(7, 11, source, Some(a)).unwrap(),
            (a, false)
        );
        assert_eq!(
            projection.search(&request(11, "oldneedle")).unwrap().len(),
            1
        );
        let (next_a, pending) = projection
            .prepare_owner(7, 11, source, Some(uuid::Uuid::new_v4()))
            .unwrap();
        assert!(pending);
        assert_ne!(
            next_a, a,
            "A→B→A must generate a new activation, not recycle A's identity"
        );
        assert!(projection
            .search(&request(11, "oldneedle"))
            .unwrap()
            .is_empty());
        assert!(projection
            .acknowledge_owner_backfill(7, 11, source, a)
            .is_err());
        assert_eq!(
            projection
                .prepare_owner(7, 22, source, Some(other))
                .unwrap(),
            (other, false)
        );
        assert_eq!(
            projection
                .search(&request(22, "otherneedle"))
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn legacy_uuid_only_marker_rebuilds_once() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, activation)
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let format_key = key(&transaction, "search_format", b"version").unwrap();
        projection
            .database
            .entity_put(
                &mut transaction,
                format_key,
                [&[FORMAT_VERSION, 1], source.as_bytes().as_slice()].concat(),
            )
            .unwrap();
        projection.database.commit(transaction).unwrap();
        let (next, pending) = projection.prepare_owner(7, 11, source, None).unwrap();
        assert!(pending);
        assert_ne!(next, activation);
        projection
            .acknowledge_owner_backfill(7, 11, source, next)
            .unwrap();
        assert_eq!(
            projection.prepare_owner(7, 11, source, Some(next)).unwrap(),
            (next, false)
        );
    }

    #[test]
    fn disposable_upgrade_binds_source_and_preserves_other_owners() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let first_source = uuid::Uuid::new_v4();
        let (activation, pending) = projection.prepare_owner(7, 11, first_source, None).unwrap();
        assert!(pending);
        assert_eq!(
            projection.prepare_owner(7, 11, first_source, None).unwrap(),
            (activation, true)
        );
        projection
            .acknowledge_owner_backfill(7, 11, first_source, activation)
            .unwrap();
        replace(
            &mut projection,
            11,
            "local",
            "g",
            1,
            &[document("t", 1, "needle")],
        );
        replace(
            &mut projection,
            12,
            "foreign",
            "g",
            1,
            &[document("t", 1, "needle")],
        );
        assert!(
            !projection
                .prepare_owner(7, 11, first_source, Some(activation))
                .unwrap()
                .1
        );
        assert!(
            projection
                .prepare_owner(7, 11, uuid::Uuid::new_v4(), Some(activation))
                .unwrap()
                .1
        );
        assert!(projection
            .search(&request(11, "needle"))
            .unwrap()
            .is_empty());
        assert_eq!(projection.search(&request(12, "needle")).unwrap().len(), 1);
        let reopened = OpenTurnSearchProjection::open(directory.path(), BUDGET);
        assert!(
            reopened.is_err(),
            "live projection lease must remain exclusive"
        );
    }

    #[test]
    fn digest_posting_upgrade_retires_only_the_selected_owner_and_stays_pending() {
        for old_version in [2_u8, 3] {
            let directory = tempdir().unwrap();
            let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
            let source = uuid::Uuid::new_v4();
            let (old_activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
            projection.acknowledge_owner_backfill(7, 11, source, old_activation).unwrap();
            replace(&mut projection, 11, "local", "g", 1, &[document("t", 1, "oldneedle")]);
            replace(&mut projection, 12, "foreign", "g", 1, &[document("t", 1, "foreignneedle")]);
            let mut tx = projection.database.begin(7, 11).unwrap();
            let format_key = key(&tx, "search_format", b"version").unwrap();
            projection.database.entity_put(&mut tx, format_key,
                [&[old_version, 1], source.as_bytes().as_slice(), old_activation.as_bytes().as_slice()].concat()).unwrap();
            // A real old packed-digest posting must be retired without decoding
            // it as an ID list. The other owner's dictionary must survive.
            let posting_key = key(&tx, GRAM_NAMESPACE, &gram_digest(b"old")).unwrap();
            projection.database.entity_put(&mut tx, posting_key,
                [&[old_version, 0], generation_digest("local", "g").as_slice()].concat()).unwrap();
            projection.database.commit(tx).unwrap();
            drop(projection);
            let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
            let (activation, pending) = projection.prepare_owner(7, 11, source, Some(old_activation)).unwrap();
            assert!(pending);
            assert_ne!(activation, old_activation);
            assert!(projection.search(&request(11, "oldneedle")).unwrap().is_empty());
            assert_eq!(projection.search(&request(12, "foreignneedle")).unwrap().len(), 1);
            assert_eq!(gram_row_count(&projection, 11), 0);
            let mut tx = projection.database.begin(7, 11).unwrap();
            assert_eq!(generation_dictionary::lookup(&projection.database, &mut tx,
                &generation_digest("local", "g")).unwrap(), None);
            assert_eq!(read_usage(&projection.database, &mut tx).unwrap(), 0);
            drop(tx);
            drop(projection);
            let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
            assert_eq!(projection.prepare_owner(7, 11, source, Some(old_activation)).unwrap(), (activation, true));
            assert!(projection.acknowledge_owner_backfill(7, 11, source, old_activation).is_err());
            replace(&mut projection, 11, "local", "new", 2, &[document("t", 1, "newneedle")]);
            projection.acknowledge_owner_backfill(7, 11, source, activation).unwrap();
            assert_eq!(projection.prepare_owner(7, 11, source, Some(activation)).unwrap(), (activation, false));
            assert_eq!(projection.search(&request(11, "newneedle")).unwrap().len(), 1);
        }
    }

    #[test]
    fn empty_headers_and_titles_consume_the_owner_budget_and_are_reclaimed() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), 4096).unwrap();
        let mut inserted = 0;
        for index in 0..100 {
            let id = format!("header-{index}");
            match projection.begin_conversation_rebuild(7, 11, &id, "g", index) {
                Ok(()) => projection
                    .finalize_conversation_rebuild(7, 11, &id, "g")
                    .unwrap(),
                Err(error) => {
                    assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
                    break;
                }
            }
            inserted += 1;
        }
        assert!(inserted > 0 && inserted < 100);
        for index in 0..inserted {
            projection
                .remove_conversation(7, 11, &format!("header-{index}"))
                .unwrap();
        }
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let leftover = read_usage(&projection.database, &mut transaction).unwrap();
        if leftover != 0 {
            let (start, end) = EntityKey::prefix_range(7, 11, GRAM_NAMESPACE, b"").unwrap();
            let rows = projection
                .database
                .entity_scan(&mut transaction, &start, &end, 1000)
                .unwrap();
            eprintln!("leftover usage {leftover}, {} gram rows", rows.len());
            let (start, end) =
                EntityKey::prefix_range(7, 11, GRAM_EXCLUSION_NAMESPACE, b"").unwrap();
            let rows = projection
                .database
                .entity_scan(&mut transaction, &start, &end, 1000)
                .unwrap();
            eprintln!("{} exclusion rows", rows.len());
        }
        assert_eq!(leftover, generation_dictionary::retained_counter_bytes(&projection.database, &mut transaction).unwrap());
        drop(transaction);
        projection
            .begin_conversation_rebuild(7, 11, "title", "g", 1)
            .unwrap();
        assert_eq!(
            projection
                .set_conversation_title(7, 11, "title", "g", &"x".repeat(2000))
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    fn gram_list_for(
        projection: &OpenTurnSearchProjection,
        owner: u64,
        gram: &str,
    ) -> io::Result<Option<GramList>> {
        let mut transaction = projection.database.begin(7, owner)?;
        let posting_key =
            key(&transaction, GRAM_NAMESPACE, &gram_digest(gram.as_bytes()))?;
        match projection
            .database
            .entity_get(&mut transaction, &posting_key)?
        {
            None => Ok(None),
            Some(raw) => Ok(Some(decode_gram_list(&raw)?)),
        }
    }

    fn gram_contains(
        projection: &OpenTurnSearchProjection,
        owner: u64,
        gram: &str,
        conversation_id: &str,
        generation: &str,
    ) -> bool {
        let digest = generation_digest(conversation_id, generation);
        match gram_list_for(projection, owner, gram).unwrap() {
            Some(GramList::Entries(entries)) => {
                let mut transaction = projection.database.begin(7, owner).unwrap();
                generation_dictionary::lookup(&projection.database, &mut transaction, &digest)
                    .unwrap().is_some_and(|id| entries.contains(&id))
            },
            Some(GramList::Omni) => panic!("gram {gram} unexpectedly universal"),
            None => false,
        }
    }

    fn set_gram_state(projection: &mut OpenTurnSearchProjection, owner: u64, state: u8) {
        let mut transaction = projection.database.begin(7, owner).unwrap();
        let stored_key = gram_state_key(&transaction).unwrap();
        projection
            .database
            .entity_put(&mut transaction, stored_key, vec![state])
            .unwrap();
        projection.database.commit(transaction).unwrap();
    }

    fn gram_row_count(projection: &OpenTurnSearchProjection, owner: u64) -> usize {
        let mut transaction = projection.database.begin(7, owner).unwrap();
        let (start, end) = EntityKey::prefix_range(7, owner, GRAM_NAMESPACE, b"").unwrap();
        let mut total = 0;
        let mut start = start;
        loop {
            let rows = projection
                .database
                .entity_scan(&mut transaction, &start, &end, 1000)
                .unwrap();
            total += rows.len();
            if rows.len() < 1000 {
                return total;
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = key(&transaction, GRAM_NAMESPACE, &next).unwrap();
        }
    }

    #[test]
    fn gram_postings_follow_the_generation_lifecycle() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g1", 100)
            .unwrap();
        projection
            .set_conversation_title(7, 11, "conversation", "g1", "alpha title")
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "g1",
                &[
                    document("t1", 1, "needle body"),
                    document("t2", 2, "second turn"),
                ],
            )
            .unwrap();
        projection
            .finalize_conversation_rebuild(7, 11, "conversation", "g1")
            .unwrap();
        for gram in ["eed", "tur", "alp", "con", "n"] {
            assert!(
                gram_contains(&projection, 11, gram, "conversation", "g1"),
                "{gram} posting missing after build"
            );
        }
        assert!(matches!(
            gram_list_for(&projection, 11, "zzq").unwrap(),
            None
        ));
        // A replacement generation retracts the old postings and emits its
        // own in the same transaction.
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g2", 200)
            .unwrap();
        projection
            .set_conversation_title(7, 11, "conversation", "g2", "other")
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "g2",
                &[document("t3", 1, "fresh content")],
            )
            .unwrap();
        projection
            .finalize_conversation_rebuild(7, 11, "conversation", "g2")
            .unwrap();
        assert!(!gram_contains(&projection, 11, "eed", "conversation", "g1"));
        assert!(gram_contains(&projection, 11, "esh", "conversation", "g2"));
        assert!(gram_contains(&projection, 11, "oth", "conversation", "g2"));
        assert!(!gram_contains(&projection, 11, "alp", "conversation", "g2"));
        // Removal retracts the rest.
        projection
            .remove_conversation(7, 11, "conversation")
            .unwrap();
        assert_eq!(gram_row_count(&projection, 11), 0);
        let mut transaction = projection.database.begin(7, 11).unwrap();
        assert_eq!(
            read_usage(&projection.database, &mut transaction).unwrap(),
            generation_dictionary::retained_counter_bytes(&projection.database, &mut transaction).unwrap()
        );
    }

    #[test]
    fn unfinished_build_postings_retract_on_rebuild() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g1", 100)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "g1",
                &[document("t1", 1, "abandoned text")],
            )
            .unwrap();
        assert!(gram_contains(&projection, 11, "ban", "conversation", "g1"));
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g2", 200)
            .unwrap();
        assert!(!gram_contains(&projection, 11, "ban", "conversation", "g1"));
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "g2",
                &[document("t2", 1, "replacement text")],
            )
            .unwrap();
        projection
            .finalize_conversation_rebuild(7, 11, "conversation", "g2")
            .unwrap();
        assert!(gram_contains(&projection, 11, "pla", "conversation", "g2"));
        assert_eq!(projection.search(&request(11, "abandoned")).unwrap().len(), 0);
        assert_eq!(projection.search(&request(11, "replacement")).unwrap().len(), 1);
    }

    #[test]
    fn gram_emission_budget_trip_degrades_and_the_walk_still_answers() {
        let directory = tempdir().unwrap();
        // Room for one conversation's content plus a bounded slice of its
        // postings; the emission must trip into DEGRADED, never overspend.
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), 12 * 1024).unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g", 100)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "g",
                &[document(
                    "t1",
                    1,
                    &(0..200)
                        .map(|i| format!("distinct{i} search"))
                        .collect::<Vec<_>>()
                        .join(" "),
                )],
            )
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        assert_eq!(
            read_gram_state(&projection.database, &mut transaction).unwrap(),
            Some(GRAM_STATE_DEGRADED)
        );
        assert!(
            read_usage(&projection.database, &mut transaction).unwrap() <= 12 * 1024
        );
        drop(transaction);
        projection
            .finalize_conversation_rebuild(7, 11, "conversation", "g")
            .unwrap();
        // Degraded means the exact legacy walk, with the same answer.
        assert_eq!(
            projection.search(&request(11, "distinct42")).unwrap().len(),
            1
        );
        projection
            .remove_conversation(7, 11, "conversation")
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        assert_eq!(
            read_usage(&projection.database, &mut transaction).unwrap(),
            generation_dictionary::retained_counter_bytes(&projection.database, &mut transaction).unwrap(),
            "retraction frees charged postings even after a degradation"
        );
    }

    #[test]
    fn common_gram_list_promotes_to_universal_and_search_stays_exact() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        for index in 0..(GRAM_LIST_MAX_ENTRIES + 5) {
            replace(
                &mut projection,
                11,
                &format!("conversation-{index:03}"),
                "g",
                index as u64,
                &[document("t1", 1, "commonterm payload")],
            );
        }
        assert!(matches!(
            gram_list_for(&projection, 11, "omm").unwrap(),
            Some(GramList::Omni)
        ));
        set_gram_state(&mut projection, 11, GRAM_STATE_READY);
        let hits = projection.search(&request(11, "commonterm")).unwrap();
        assert_eq!(hits.len(), 50);
        assert!(hits.iter().all(|hit| hit.snippet.contains("commonterm")));
    }

    #[test]
    fn gram_list_codec_rejects_malformed_values() {
        let mut oversized = vec![FORMAT_VERSION, 0];
        oversized.extend_from_slice(&posting_codec::encode(
            &(1..=GRAM_LIST_MAX_ENTRIES as u64 + 1).collect::<Vec<_>>(), 8192,
        ).unwrap());
        for raw in [
            vec![0],
            vec![FORMAT_VERSION],
            vec![FORMAT_VERSION, 2],
            vec![FORMAT_VERSION, 1, 0],
            vec![FORMAT_VERSION, 0, 1, 0],
            oversized,
        ] {
            assert!(decode_gram_list(&raw).is_err(), "accepted {raw:?}");
        }
        let mut entries = vec![1_u64, 2, 3];
        assert!(matches!(
            decode_gram_list(&encode_gram_entries(&entries).unwrap()).unwrap(),
            GramList::Entries(_)
        ));
        entries.swap(0, 2);
        assert!(encode_gram_entries(&entries).is_err());
    }

    fn diverse_document(turn_id: &str, ordinal: u64, seed: u64, chars: usize) -> TurnSearchDocument {
        // A deterministic high-diversity alphabet stream: thousands of
        // distinct grams per document, to force posting-write overflow.
        let mut value = String::new();
        let mut state = seed;
        while value.chars().count() < chars {
            state = state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            value.push(
                char::from_u32(0x20 + (state >> 33) as u32 % 0x5F)
                    .unwrap_or('x'),
            );
        }
        document(turn_id, ordinal, &value)
    }

    #[test]
    fn overflowing_page_excludes_one_conversation_and_keeps_the_index() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "huge", "g", 100)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "huge",
                "g",
                &(0..4u64)
                    .map(|i| diverse_document(&format!("t{i}"), i, 77 + i, 4_096))
                    .collect::<Vec<_>>(),
            )
            .unwrap();
        projection
            .finalize_conversation_rebuild(7, 11, "huge", "g")
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let exclusion_key = key(
            &transaction,
            GRAM_EXCLUSION_NAMESPACE,
            &generation_digest("huge", "g"),
        )
        .unwrap();
        assert!(
            projection
                .database
                .entity_get(&mut transaction, &exclusion_key)
                .unwrap()
                .is_some(),
            "overflowing append must exclude the conversation generation"
        );
        drop(transaction);
        replace(
            &mut projection,
            11,
            "small",
            "g",
            200,
            &[document("t1", 1, "needle")],
        );
        set_gram_state(&mut projection, 11, GRAM_STATE_READY);
        // The indexed conversation resolves through postings; the excluded
        // one is always walked and still answers.
        assert_eq!(projection.search(&request(11, "needle")).unwrap().len(), 1);
        let huge_text_probe = {
            let text: String = {
                let mut state = 77u64;
                let mut value = String::new();
                while value.chars().count() < 64 {
                    state = state
                        .wrapping_mul(6_364_136_223_846_793_005)
                        .wrapping_add(1_442_695_040_888_963_407);
                    value.push(
                        char::from_u32(0x20 + (state >> 33) as u32 % 0x5F)
                            .unwrap_or('x'),
                    );
                }
                value
            };
            text.chars().skip(3).take(12).collect::<String>()
        };
        assert_eq!(
            projection.search(&request(11, &huge_text_probe)).unwrap().len(),
            1,
            "excluded conversation must still answer through the exact walk"
        );
        // Rebuilding the excluded generation with fittable content clears the
        // exclusion marker.
        replace(&mut projection, 11, "huge", "g2", 300, &[document("t9", 1, "needle")]);
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let exclusion_key = key(
            &transaction,
            GRAM_EXCLUSION_NAMESPACE,
            &generation_digest("huge", "g2"),
        )
        .unwrap();
        assert!(
            projection
                .database
                .entity_get(&mut transaction, &exclusion_key)
                .unwrap()
                .is_none()
        );
        assert!(gram_contains(&projection, 11, "eed", "huge", "g2"));
        assert_eq!(
            projection
                .search(&request(11, &huge_text_probe))
                .unwrap()
                .len(),
            0
        );
    }

    fn prepared_simulated_projection(vfs: Arc<DeterministicVfs>) -> OpenTurnSearchProjection {
        vfs.create_dir(Path::new("/search")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize_with_vfs(Path::new("/search"), vfs, BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "conversation",
            "old",
            100,
            &[document("turn", 1, "old durable phrase")],
        );
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "new", 200)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "new",
                &[document("turn", 1, "new durable phrase")],
            )
            .unwrap();
        projection
            .mark_rebuild_incomplete(7, 11, "conversation", "new")
            .unwrap();
        projection
    }

    fn assert_recovered_generation(vfs: Arc<DeterministicVfs>) {
        vfs.arm_fault(None).unwrap();
        vfs.crash().unwrap();
        let projection =
            OpenTurnSearchProjection::open_with_vfs(Path::new("/search"), vfs, BUDGET).unwrap();
        let old = projection
            .search(&request(11, "old durable phrase"))
            .unwrap();
        let new = projection
            .search(&request(11, "new durable phrase"))
            .unwrap();
        assert_eq!(
            projection.source_incomplete(7, 11).unwrap(),
            !new.is_empty()
        );
        assert!(!projection.source_incomplete(7, 12).unwrap());
        assert!(
            (old.len() == 1 && new.is_empty()) || (old.is_empty() && new.len() == 1),
            "recovery exposed neither or both projection generations"
        );
    }

    #[test]
    fn every_publish_io_fault_recovers_exactly_one_complete_generation() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_simulated_projection(Arc::clone(&baseline_vfs));
        baseline_vfs.arm_fault(None).unwrap();
        baseline
            .finalize_conversation_rebuild(7, 11, "conversation", "new")
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut projection = prepared_simulated_projection(Arc::clone(&vfs));
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = projection.finalize_conversation_rebuild(7, 11, "conversation", "new");
            drop(projection);
            assert_recovered_generation(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut projection = prepared_simulated_projection(Arc::clone(&vfs));
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = projection.finalize_conversation_rebuild(7, 11, "conversation", "new");
            drop(projection);
            assert_recovered_generation(vfs);
        }
    }
    #[test]
    fn gram_dense_rebuilds_stay_within_transaction_reference_boundary() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), 4 * 1024 * 1024 * 1024).unwrap();
        let mut state = 0xd5a1_804b_56c2_9041_u64;
        for index in 0..128_u64 {
            let mut text = String::with_capacity(32 * 1024);
            for _ in 0..32 * 1024 {
                state ^= state << 13; state ^= state >> 7; state ^= state << 17;
                text.push(b"0123456789abcdef"[(state & 15) as usize] as char);
            }
            if index >= 35 {
                let root = projection.database.authority_state_root();
                let mut leaves=0_u64;let mut leaf_bytes=0_u64;let mut posting_rows=0_u64;let mut posting_bytes=0_u64;let mut max_posting=0_usize;

                let metrics=crate::entity::visit_entity_page_graph_with_values(
                    root,
                    |id| projection.database.read_block_for_test(id),
                    |_, bytes| { if bytes.starts_with(b"TDBENT01") && bytes[12]==0 { leaves+=1;leaf_bytes+=bytes.len() as u64; } Ok(()) },
                    |key, bytes| {
                        if key.namespace()==GRAM_NAMESPACE {
                            posting_rows+=1;posting_bytes+=bytes.len() as u64;max_posting=max_posting.max(bytes.len());
                            decode_gram_list(bytes)?;
                        }
                        Ok(())
                    },
                ).unwrap();
                eprintln!("dense_search_physical_tree before_conversation={index} pages={} leaves={leaves} leaf_bytes={leaf_bytes} posting_rows={posting_rows} posting_bytes={posting_bytes} max_posting_bytes={max_posting}",metrics.page_count);
            }
            let before = crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_STORE_PAGE);
            let conversation = format!("dense-{index:04}");
            projection.begin_conversation_rebuild(7, 11, &conversation, "g1", index + 1).unwrap();
            let documents: Vec<_> = text.as_bytes().chunks(CHUNK_CHARACTERS).enumerate().map(|(chunk, bytes)| {
                let mut value = document("t1", 1, std::str::from_utf8(bytes).unwrap());
                value.chunk_index = chunk as u32;
                value
            }).collect();
            let result = documents.chunks(8).try_for_each(|page| {
                projection.append_conversation_page(7, 11, &conversation, "g1", page)
            });
            let pages = crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_STORE_PAGE) - before;
            eprintln!("dense_search_reference_probe conversation={index} stored_pages={pages} append_error={:?}", result.as_ref().err());
            result.unwrap_or_else(|error| panic!("dense conversation {index} failed after {pages} page stores: {error}"));
            projection.finalize_conversation_rebuild(7, 11, &conversation, "g1").unwrap();
        }
        set_gram_state(&mut projection, 11, GRAM_STATE_READY);
        assert_eq!(projection.search(&request(11, "dense-")).unwrap().len(), 50);
        assert_eq!(projection.search(&request(11, "dense-0127")).unwrap().len(), 1);
        projection.remove_conversation(7, 11, "dense-0127").unwrap();
        assert!(projection.search(&request(11, "dense-0127")).unwrap().is_empty());
        drop(projection);
        let projection = OpenTurnSearchProjection::open(directory.path(), 4 * 1024 * 1024 * 1024).unwrap();
        assert_eq!(projection.search(&request(11, "dense-0126")).unwrap().len(), 1);
        assert!(projection.search(&request(11, "dense-0127")).unwrap().is_empty());
    }

}

#[cfg(test)]
mod shutdown_checkpoint_tests {
    use std::io;

    #[test]
    fn shutdown_checkpoint_retires_the_projection_wal_window() {
        let directory = tempfile::tempdir().unwrap();
        let mut projection =
            super::OpenTurnSearchProjection::initialize(directory.path(), 1 << 20).unwrap();
        let error = projection
            .checkpoint_wal_window_for_shutdown()
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);

        projection.set_owner_indexing(7, 11, true).unwrap();
        projection.checkpoint_wal_window_for_shutdown().unwrap();

        // The retired window stays retired.
        let error = projection
            .checkpoint_wal_window_for_shutdown()
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        // Content survives its own checkpoint.
        assert!(projection.owner_indexing(7, 11).unwrap());
    }

}

// Public runtime owns capacity admission and retryable deferred opening.
pub use crate::turn_search_runtime::TurnSearchProjection;
