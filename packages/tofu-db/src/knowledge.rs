//! Owner-scoped knowledge corpus: documents, chunks, assets, search postings,
//! consent settings, and the visual-enrichment claim queue.
//!
//! Physical layout mirrors the legacy SQLite schema one-to-one so every legacy
//! query plan has an exact ordered equivalent:
//!
//! - `knowledge_document_documents` — versioned document metadata records
//!   (blob-spilling), keyed by length-prefixed document id. Asset counts are
//!   computed at read time like the legacy `_DOCUMENT_WITH_COUNTS` subqueries.
//! - `knowledge_chunk_documents` — versioned chunk records keyed by
//!   (document id, big-endian ordinal); each record retains its deduplicated
//!   casefolded term list so postings can be torn down exactly on delete.
//! - `knowledge_asset_documents` — versioned asset records keyed by
//!   length-prefixed asset id. The record denormalizes `document_id` (the
//!   legacy column) so owner-wide aggregates scan one namespace.
//! - `knowledge_asset_ordinals` — (document id, big-endian ordinal) → asset
//!   id, reproducing `ORDER BY ordinal` per-document asset reads.
//! - `knowledge_chunk_asset_links` — (document id, chunk ordinal, reference
//!   ordinal, asset id) → relation, reproducing the link table's
//!   `ORDER BY chunk_ordinal, ordinal, asset_id` scan order.
//! - `knowledge_asset_chunk_reverse` — (asset id, document id, chunk ordinal)
//!   reverse links for the enriched-chunk rewrite in `asset.update`.
//! - `knowledge_terms` — (term, document id, chunk ordinal) postings; the
//!   value repeats (document id, ordinal) so scans never parse keys.
//! - `knowledge_documents_by_sha256` — digest → creation-ordered
//!   (sequence, document id) list. Creation order matches legacy rowid order,
//!   so `find_digest` returns the same row SQLite's unspecified `fetch_one`
//!   deterministically returns. Duplicates are only reachable through
//!   `document.replace` (create dedupes by digest).
//! - `knowledge_settings` — owner consent row plus the document creation
//!   sequence counter.
//! - `knowledge_enrichment_queue` — derived claim queue keyed by
//!   (kind rank, created_at bits, document id, ordinal) so ascending scan
//!   order equals the legacy claim `ORDER BY`. Entries exist exactly for
//!   assets in the pending/running class.
//! - `knowledge_enrichment_owners` — TENANT-GLOBAL derived index: an entry
//!   exists exactly while the owner has visual enrichment enabled AND at
//!   least one pending/running asset on a library/shared document, which is
//!   precisely the legacy `enrichment.owners` predicate. Every mutation that
//!   can change that predicate delta-adjusts the entry; enabling visual
//!   enrichment re-establishes the count with one bounded scan.
//!
//! Legacy divergence notes (contract-sanctioned fail-closed posture):
//! - Scan bounds (`MAX_KNOWLEDGE_*`) reject with resource-exhausted where
//!   SQLite would grind through unbounded rows.
//! - Truthy non-scalar `query`/`category`/`sort`/`document_id` filter values
//!   (JSON arrays/objects) are rejected; legacy would stringify Python reprs.
//! - `document.create` reusing an existing id with a different digest is a
//!   typed conflict; legacy surfaces a raw SQLite integrity error.

use std::collections::{HashMap, HashSet};
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    KNOWLEDGE_ASSET_CHUNK_REVERSE_NAMESPACE, KNOWLEDGE_ASSET_DOCUMENT_NAMESPACE,
    KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE, KNOWLEDGE_CHUNK_ASSET_LINK_NAMESPACE,
    KNOWLEDGE_CHUNK_DOCUMENT_NAMESPACE, KNOWLEDGE_DIGEST_INDEX_NAMESPACE,
    KNOWLEDGE_DOCUMENT_NAMESPACE, KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
    KNOWLEDGE_ENRICHMENT_QUEUE_NAMESPACE, KNOWLEDGE_SETTINGS_NAMESPACE, KNOWLEDGE_TERM_NAMESPACE,
    MAX_KNOWLEDGE_ASSET_DOCUMENT_BYTES, MAX_KNOWLEDGE_ASSET_ID_CHARACTERS,
    MAX_KNOWLEDGE_ASSET_KIND_CHARACTERS, MAX_KNOWLEDGE_ASSET_METADATA_CHARACTERS,
    MAX_KNOWLEDGE_ASSET_TEXT_CHARACTERS, MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT,
    MAX_KNOWLEDGE_CAPTION_CHARACTERS, MAX_KNOWLEDGE_CATALOG_PAGE_SIZE,
    MAX_KNOWLEDGE_CATALOG_QUERY_CHARACTERS, MAX_KNOWLEDGE_CATALOG_SCAN_DOCUMENTS,
    MAX_KNOWLEDGE_CHUNK_CONTENT_CHARACTERS, MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES,
    MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT, MAX_KNOWLEDGE_CLAIM_SCAN_ROWS, MAX_KNOWLEDGE_DOCUMENT_BYTES,
    MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS, MAX_KNOWLEDGE_DOCUMENT_KIND_CHARACTERS,
    MAX_KNOWLEDGE_ENRICHMENT_ERROR_CHARACTERS, MAX_KNOWLEDGE_ENRICHMENT_MODEL_CHARACTERS,
    MAX_KNOWLEDGE_ENRICHMENT_OWNER_ROWS, MAX_KNOWLEDGE_JSON_ARRAY_CHARACTERS,
    MAX_KNOWLEDGE_LOCATION_CHARACTERS, MAX_KNOWLEDGE_MEDIA_METADATA_CHARACTERS,
    MAX_KNOWLEDGE_METHOD_CHARACTERS, MAX_KNOWLEDGE_MIME_TYPE_CHARACTERS,
    MAX_KNOWLEDGE_NAME_CHARACTERS, MAX_KNOWLEDGE_OWNER_ASSET_ROWS, MAX_KNOWLEDGE_PAGE_ROWS,
    MAX_KNOWLEDGE_RELATION_CHARACTERS, MAX_KNOWLEDGE_SCOPE_CHARACTERS,
    MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN, MAX_KNOWLEDGE_SEARCH_TEXT_CHARACTERS,
    MAX_KNOWLEDGE_SEARCH_TOKENS, MAX_KNOWLEDGE_SECTION_CHARACTERS, MAX_KNOWLEDGE_STATUS_CHARACTERS,
    MAX_KNOWLEDGE_STORED_NAME_CHARACTERS, MAX_KNOWLEDGE_TERM_CHARACTERS,
    MAX_KNOWLEDGE_TERMS_PER_CHUNK, MAX_TRANSACTION_IR_LITERAL_BYTES,
};
use crate::versioned_document::{self, PutRequest};

const DOCUMENT_LOGICAL_NAMESPACE: &str = "knowledge_documents";
const CHUNK_LOGICAL_NAMESPACE: &str = "knowledge_chunks";
const ASSET_LOGICAL_NAMESPACE: &str = "knowledge_assets";
const SETTINGS_KEY: &[u8] = b"settings";
const DOCUMENT_SEQUENCE_KEY: &[u8] = b"document_sequence";
const CLAIM_STALE_SECONDS: f64 = 1800.0;

const ENRICHMENT_STATUSES: [&str; 6] = [
    "not_requested",
    "pending",
    "running",
    "ready",
    "no_vision",
    "failed",
];
const DOCUMENT_SCOPES: [&str; 4] = ["draft", "library", "attachment", "shared"];
const DOCUMENT_CATEGORIES: [&str; 10] = [
    "all",
    "pdf",
    "document",
    "spreadsheet",
    "presentation",
    "image",
    "email",
    "ebook",
    "text",
    "other",
];
const DOCUMENT_SORTS: [&str; 4] = ["updated_desc", "created_desc", "name_asc", "size_desc"];
const MUTABLE_ASSET_FIELDS: [&str; 6] = [
    "caption",
    "ocr_text",
    "description",
    "enrichment_status",
    "enrichment_model",
    "enrichment_error",
];

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn is_scoped(scope: &str) -> bool {
    matches!(scope, "library" | "shared")
}

fn in_queue_class(status: &str) -> bool {
    matches!(status, "pending" | "running")
}

fn in_issue_class(status: &str) -> bool {
    matches!(status, "no_vision" | "failed")
}

fn kind_rank(kind: &str) -> u8 {
    match kind {
        "image" => 0,
        "figure" => 1,
        "table" => 2,
        _ => 3,
    }
}

fn category_of(kind: &str) -> &'static str {
    match kind.to_ascii_lowercase().as_str() {
        ".pdf" => "pdf",
        ".doc" | ".docx" | ".odt" | ".rtf" => "document",
        ".xls" | ".xlsx" | ".ods" | ".csv" | ".tsv" => "spreadsheet",
        ".ppt" | ".pptx" | ".odp" => "presentation",
        ".png" | ".jpg" | ".jpeg" | ".gif" | ".webp" | ".bmp" => "image",
        ".eml" => "email",
        ".epub" => "ebook",
        ".txt" | ".md" | ".markdown" | ".json" | ".jsonl" | ".xml" | ".html" | ".htm" | ".yaml"
        | ".yml" | ".toml" | ".ini" | ".cfg" | ".rst" | ".log" | ".tex" | ".bib" | ".srt"
        | ".vtt" | ".sql" | ".py" | ".js" | ".ts" | ".java" | ".c" | ".cpp" | ".h" | ".hpp"
        | ".go" | ".rs" | ".rb" | ".php" | ".sh" | ".bash" | ".zsh" | ".css" | ".scss"
        | ".less" | ".r" | ".m" | ".swift" => "text",
        _ => "other",
    }
}

fn is_python_whitespace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

pub(crate) fn python_strip(value: &str) -> &str {
    value.trim_matches(is_python_whitespace)
}

fn python_casefold(value: &str) -> String {
    crate::artifact::python_casefold(value)
}

fn seconds(now_unix_ms: u64) -> f64 {
    now_unix_ms as f64 / 1000.0
}

// ---------------------------------------------------------------------------
// Physical key encodings. Length-prefixed text keeps composite keys
// prefix-safe and preserves UTF-8 byte order, which equals SQLite text order.
// ---------------------------------------------------------------------------

fn push_text(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    let length = u16::try_from(value.len())
        .map_err(|_| invalid_input("knowledge identity exceeds its encoded bound"))?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn read_text(input: &[u8]) -> io::Result<(String, usize)> {
    if input.len() < 2 {
        return Err(invalid_data("knowledge index value is truncated"));
    }
    let length = u16::from_be_bytes([input[0], input[1]]) as usize;
    if input.len() < 2 + length {
        return Err(invalid_data("knowledge index value is truncated"));
    }
    let text = String::from_utf8(input[2..2 + length].to_vec())
        .map_err(|_| invalid_data("knowledge index value is not UTF-8"))?;
    Ok((text, 2 + length))
}

fn owner_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn document_raw(document_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(document_id.len() + 2);
    push_text(&mut raw, document_id)?;
    Ok(raw)
}

fn parse_chunk_key(raw: &[u8]) -> io::Result<(String, u64)> {
    let (document_id, consumed) = read_text(raw)?;
    let ordinal = raw
        .get(consumed..)
        .filter(|tail| tail.len() == 4)
        .map(|tail| u32::from_be_bytes(tail.try_into().expect("4-byte tail")))
        .ok_or_else(|| invalid_data("knowledge chunk key is truncated"))?;
    Ok((document_id, u64::from(ordinal)))
}

fn chunk_raw(document_id: &str, ordinal: u64) -> io::Result<Vec<u8>> {
    let ordinal = u32::try_from(ordinal)
        .map_err(|_| invalid_input("knowledge chunk ordinal exceeds its encoded bound"))?;
    let mut raw = document_raw(document_id)?;
    raw.extend_from_slice(&ordinal.to_be_bytes());
    Ok(raw)
}

fn asset_raw(asset_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(asset_id.len() + 2);
    push_text(&mut raw, asset_id)?;
    Ok(raw)
}

fn ordinal_raw(document_id: &str, ordinal: u64) -> io::Result<Vec<u8>> {
    chunk_raw(document_id, ordinal)
}

fn link_raw(
    document_id: &str,
    chunk_ordinal: u64,
    reference_ordinal: u64,
    asset_id: &str,
) -> io::Result<Vec<u8>> {
    let chunk_ordinal = u32::try_from(chunk_ordinal)
        .map_err(|_| invalid_input("knowledge chunk ordinal exceeds its encoded bound"))?;
    let reference_ordinal = u32::try_from(reference_ordinal).map_err(|_| {
        invalid_input("knowledge asset reference ordinal exceeds its encoded bound")
    })?;
    let mut raw = document_raw(document_id)?;
    raw.extend_from_slice(&chunk_ordinal.to_be_bytes());
    raw.extend_from_slice(&reference_ordinal.to_be_bytes());
    push_text(&mut raw, asset_id)?;
    Ok(raw)
}

fn reverse_raw(asset_id: &str, document_id: &str, chunk_ordinal: u64) -> io::Result<Vec<u8>> {
    let chunk_ordinal = u32::try_from(chunk_ordinal)
        .map_err(|_| invalid_input("knowledge chunk ordinal exceeds its encoded bound"))?;
    let mut raw = asset_raw(asset_id)?;
    push_text(&mut raw, document_id)?;
    raw.extend_from_slice(&chunk_ordinal.to_be_bytes());
    Ok(raw)
}

fn term_raw(term: &str, document_id: &str, chunk_ordinal: u64) -> io::Result<Vec<u8>> {
    let chunk_ordinal = u32::try_from(chunk_ordinal)
        .map_err(|_| invalid_input("knowledge chunk ordinal exceeds its encoded bound"))?;
    let mut raw = Vec::with_capacity(term.len() + document_id.len() + 8);
    push_text(&mut raw, term)?;
    push_text(&mut raw, document_id)?;
    raw.extend_from_slice(&chunk_ordinal.to_be_bytes());
    Ok(raw)
}

fn queue_raw(kind: &str, created_at: f64, document_id: &str, ordinal: u64) -> io::Result<Vec<u8>> {
    let ordinal = u32::try_from(ordinal)
        .map_err(|_| invalid_input("knowledge asset ordinal exceeds its encoded bound"))?;
    // created_at is validated finite and non-negative with -0.0 normalized, so
    // IEEE-754 bit order equals numeric order for every reachable value.
    let mut raw = Vec::with_capacity(document_id.len() + 16);
    raw.push(kind_rank(kind));
    raw.extend_from_slice(&created_at.to_bits().to_be_bytes());
    push_text(&mut raw, document_id)?;
    raw.extend_from_slice(&ordinal.to_be_bytes());
    Ok(raw)
}

fn document_chunk_logical_key(document_id: &str, ordinal: u64) -> String {
    format!("{}:{document_id}:{ordinal}", document_id.len())
}

fn document_entity_key(
    transaction: &AuthorityTransaction,
    document_id: &str,
) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        KNOWLEDGE_DOCUMENT_NAMESPACE,
        &document_raw(document_id)?,
    )
}

fn chunk_entity_key(
    transaction: &AuthorityTransaction,
    document_id: &str,
    ordinal: u64,
) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        KNOWLEDGE_CHUNK_DOCUMENT_NAMESPACE,
        &chunk_raw(document_id, ordinal)?,
    )
}

fn asset_entity_key(transaction: &AuthorityTransaction, asset_id: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        KNOWLEDGE_ASSET_DOCUMENT_NAMESPACE,
        &asset_raw(asset_id)?,
    )
}

fn owner_index_entity_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
        KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
        &transaction.owner_user_id().to_be_bytes(),
    )
}

// ---------------------------------------------------------------------------
// Bounded scans. Completeness is fail-closed: a scan that reaches its bound
// with more rows pending is a resource-exhausted error, never silent
// truncation.
// ---------------------------------------------------------------------------

fn scan_prefix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    maximum: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        prefix,
    )?;
    let mut rows = Vec::new();
    loop {
        let page_limit = crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() {
            return Ok(rows);
        }
        let overflow = rows.len() + page.len() > maximum;
        let mut successor = page.last().expect("non-empty page").0.key_bytes().to_vec();
        successor.push(0);
        let full_page = page.len() == page_limit;
        rows.extend(page);
        if overflow {
            return Err(resource_exhausted("knowledge scan exceeds its row bound"));
        }
        if !full_page {
            return Ok(rows);
        }
        cursor = owner_key(transaction, namespace, &successor)?;
    }
}

fn scan_tenant_global_prefix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    maximum: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
        namespace,
        prefix,
    )?;
    let mut rows = Vec::new();
    loop {
        let page_limit = crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() {
            return Ok(rows);
        }
        let overflow = rows.len() + page.len() > maximum;
        let mut successor = page.last().expect("non-empty page").0.key_bytes().to_vec();
        successor.push(0);
        let full_page = page.len() == page_limit;
        rows.extend(page);
        if overflow {
            return Err(resource_exhausted(
                "knowledge tenant-global scan exceeds its row bound",
            ));
        }
        if !full_page {
            return Ok(rows);
        }
        cursor = EntityKey::new(
            transaction.tenant_id(),
            crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            namespace,
            &successor,
        )?;
    }
}

// ---------------------------------------------------------------------------
// Stored records.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Deserialize, Serialize)]
struct DocumentRecord {
    id: String,
    sha256: String,
    name: String,
    stored_name: String,
    kind: String,
    size_bytes: u64,
    method: String,
    warnings_json: String,
    text_chars: u64,
    chunk_count: u64,
    pages: u64,
    scope: String,
    media_metadata_json: String,
    created_at: f64,
    updated_at: f64,
    sequence: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct AssetRecord {
    id: String,
    document_id: String,
    ordinal: u64,
    kind: String,
    stored_name: String,
    mime_type: String,
    sha256: String,
    size_bytes: u64,
    width: u64,
    height: u64,
    page: u64,
    pages_json: String,
    bbox_json: String,
    caption: String,
    ocr_text: String,
    description: String,
    enrichment_status: String,
    enrichment_model: String,
    enrichment_error: String,
    metadata_json: String,
    created_at: f64,
    updated_at: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ChunkRecord {
    ordinal: u64,
    section: String,
    location: String,
    content: String,
    search_text: String,
    terms: Vec<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
struct SettingsRecord {
    enabled: bool,
    visual_enrichment: bool,
    updated_at: f64,
}

// ---------------------------------------------------------------------------
// Compile-facing validated payloads: a structural port of legacy
// `_validated_document`. Validation is shared by compile and by
// `KnowledgeRequest::validate` so execution only sees proven shapes.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub(crate) struct ValidatedAssetRef {
    pub(crate) id: String,
    pub(crate) relation: String,
}

#[derive(Clone, Debug)]
pub(crate) struct ValidatedChunk {
    pub(crate) section: String,
    pub(crate) location: String,
    pub(crate) content: String,
    pub(crate) search_text: String,
    pub(crate) refs: Vec<ValidatedAssetRef>,
    pub(crate) terms: Vec<String>,
}

#[derive(Clone, Debug)]
pub(crate) struct ValidatedAsset {
    pub(crate) id: String,
    pub(crate) kind: String,
    pub(crate) stored_name: String,
    pub(crate) mime_type: String,
    pub(crate) sha256: String,
    pub(crate) size_bytes: u64,
    pub(crate) width: u64,
    pub(crate) height: u64,
    pub(crate) page: u64,
    pub(crate) pages_json: String,
    pub(crate) bbox_json: String,
    pub(crate) caption: String,
    pub(crate) ocr_text: String,
    pub(crate) description: String,
    pub(crate) enrichment_status: String,
    pub(crate) enrichment_model: String,
    pub(crate) enrichment_error: String,
    pub(crate) metadata_json: String,
    pub(crate) created_at: f64,
    pub(crate) updated_at: f64,
}

#[derive(Clone, Debug)]
pub struct ValidatedDocument {
    pub(crate) id: String,
    pub(crate) sha256: String,
    pub(crate) name: String,
    pub(crate) stored_name: String,
    pub(crate) kind: String,
    pub(crate) size_bytes: u64,
    pub(crate) method: String,
    pub(crate) warnings_json: String,
    pub(crate) text_chars: u64,
    pub(crate) chunk_count: u64,
    pub(crate) pages: u64,
    pub(crate) scope: String,
    pub(crate) media_metadata_json: String,
    pub(crate) created_at: f64,
    pub(crate) updated_at: f64,
    pub(crate) chunks: Vec<ValidatedChunk>,
    pub(crate) assets: Vec<ValidatedAsset>,
}

fn nested_text(
    row: &serde_json::Map<String, Value>,
    key: &str,
    maximum: usize,
    default: Option<&str>,
) -> io::Result<String> {
    let value = match row.get(key) {
        None => match default {
            Some(default) => return Ok(default.to_owned()),
            None => return Err(invalid_input("knowledge field is missing")),
        },
        Some(value) => value,
    };
    let text = value
        .as_str()
        .ok_or_else(|| invalid_input("knowledge field is not text"))?;
    if default.is_none() && text.is_empty() {
        return Err(invalid_input("knowledge field is empty"));
    }
    if text.chars().count() > maximum {
        return Err(invalid_input("knowledge field exceeds its character bound"));
    }
    Ok(text.to_owned())
}

fn nested_integer(
    row: &serde_json::Map<String, Value>,
    key: &str,
    default: Option<u64>,
) -> io::Result<u64> {
    match row.get(key) {
        None => default.ok_or_else(|| invalid_input("knowledge integer field is missing")),
        Some(value) => value
            .as_u64()
            .ok_or_else(|| invalid_input("knowledge integer field is invalid")),
    }
}

fn nested_number(row: &serde_json::Map<String, Value>, key: &str) -> io::Result<f64> {
    let value = row
        .get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| invalid_input("knowledge number field is invalid"))?;
    if !value.is_finite() || value < 0.0 {
        return Err(invalid_input("knowledge number field is invalid"));
    }
    // Normalize -0.0 so index bit order and SQLite's numeric equality agree.
    Ok(if value == 0.0 { 0.0 } else { value })
}

fn json_array_text(
    row: &serde_json::Map<String, Value>,
    key: &str,
    default: &str,
) -> io::Result<String> {
    let value = nested_text(row, key, MAX_KNOWLEDGE_JSON_ARRAY_CHARACTERS, Some(default))?;
    match serde_json::from_str::<Value>(&value) {
        Ok(decoded) if decoded.is_array() => Ok(value),
        _ => Err(invalid_input("knowledge JSON field is not an array")),
    }
}

fn json_object_text(
    row: &serde_json::Map<String, Value>,
    key: &str,
    default: &str,
    maximum: usize,
) -> io::Result<String> {
    let value = nested_text(row, key, maximum, Some(default))?;
    match serde_json::from_str::<Value>(&value) {
        Ok(decoded) if decoded.is_object() => Ok(value),
        _ => Err(invalid_input("knowledge JSON field is not an object")),
    }
}

fn safe_stored_name(row: &serde_json::Map<String, Value>) -> io::Result<String> {
    let value = nested_text(
        row,
        "stored_name",
        MAX_KNOWLEDGE_STORED_NAME_CHARACTERS,
        None,
    )?;
    if value.contains('/') || value.contains('\\') || value == "." || value == ".." {
        return Err(invalid_input("knowledge stored_name is unsafe"));
    }
    Ok(value)
}

fn sha256_digest(text: String) -> io::Result<String> {
    let digest = text.to_lowercase();
    if digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(digest)
    } else {
        Err(invalid_input(
            "knowledge sha256 is not a lowercase hex digest",
        ))
    }
}

fn search_terms(search_text: &str) -> io::Result<Vec<String>> {
    let mut terms = Vec::new();
    let mut seen = HashSet::new();
    let mut raw_count = 0_usize;
    for raw_term in search_text.split(is_python_whitespace) {
        if raw_term.is_empty() {
            continue;
        }
        raw_count += 1;
        if raw_count > MAX_KNOWLEDGE_TERMS_PER_CHUNK {
            return Err(invalid_input("knowledge chunk has too many search terms"));
        }
        let term = python_casefold(raw_term);
        if term.is_empty() || term.chars().count() > MAX_KNOWLEDGE_TERM_CHARACTERS {
            return Err(invalid_input("knowledge search term is invalid"));
        }
        if seen.insert(term.clone()) {
            terms.push(term);
        }
    }
    Ok(terms)
}

pub(crate) fn validate_document(source: &Value) -> io::Result<ValidatedDocument> {
    let source = source
        .as_object()
        .ok_or_else(|| invalid_input("knowledge document is not an object"))?;
    let document_id = nested_text(source, "id", MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS, None)?;
    let digest = sha256_digest(nested_text(source, "sha256", 64, None)?)?;
    let raw_chunks = source
        .get("chunks")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_input("knowledge chunks must be an array"))?;
    let raw_assets = source
        .get("assets")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_input("knowledge assets must be an array"))?;
    if raw_chunks.len() > MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT {
        return Err(resource_exhausted(
            "knowledge document exceeds its chunk row bound",
        ));
    }
    if raw_assets.len() > MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT {
        return Err(resource_exhausted(
            "knowledge document exceeds its asset row bound",
        ));
    }

    let mut assets = Vec::with_capacity(raw_assets.len());
    let mut asset_ids = HashSet::with_capacity(raw_assets.len());
    for (expected_ordinal, raw_asset) in raw_assets.iter().enumerate() {
        let raw_asset = raw_asset
            .as_object()
            .ok_or_else(|| invalid_input("knowledge asset is not an object"))?;
        let asset_id = nested_text(raw_asset, "id", MAX_KNOWLEDGE_ASSET_ID_CHARACTERS, None)?;
        if !asset_ids.insert(asset_id.clone()) {
            return Err(invalid_input("knowledge asset id is duplicated"));
        }
        let ordinal = nested_integer(raw_asset, "ordinal", None)?;
        if ordinal != expected_ordinal as u64 {
            return Err(invalid_input("knowledge asset ordinals must be contiguous"));
        }
        let status = nested_text(
            raw_asset,
            "enrichment_status",
            MAX_KNOWLEDGE_STATUS_CHARACTERS,
            Some("not_requested"),
        )?;
        if !ENRICHMENT_STATUSES.contains(&status.as_str()) {
            return Err(invalid_input("knowledge enrichment status is invalid"));
        }
        let asset_digest = sha256_digest(nested_text(raw_asset, "sha256", 64, None)?)?;
        assets.push(ValidatedAsset {
            id: asset_id,
            kind: nested_text(raw_asset, "kind", MAX_KNOWLEDGE_ASSET_KIND_CHARACTERS, None)?,
            stored_name: safe_stored_name(raw_asset)?,
            mime_type: nested_text(
                raw_asset,
                "mime_type",
                MAX_KNOWLEDGE_MIME_TYPE_CHARACTERS,
                None,
            )?,
            sha256: asset_digest,
            size_bytes: nested_integer(raw_asset, "size_bytes", None)?,
            width: nested_integer(raw_asset, "width", Some(0))?,
            height: nested_integer(raw_asset, "height", Some(0))?,
            page: nested_integer(raw_asset, "page", Some(0))?,
            pages_json: json_array_text(raw_asset, "pages_json", "[]")?,
            bbox_json: json_array_text(raw_asset, "bbox_json", "[]")?,
            caption: nested_text(
                raw_asset,
                "caption",
                MAX_KNOWLEDGE_CAPTION_CHARACTERS,
                Some(""),
            )?,
            ocr_text: nested_text(
                raw_asset,
                "ocr_text",
                MAX_KNOWLEDGE_ASSET_TEXT_CHARACTERS,
                Some(""),
            )?,
            description: nested_text(
                raw_asset,
                "description",
                MAX_KNOWLEDGE_ASSET_TEXT_CHARACTERS,
                Some(""),
            )?,
            enrichment_status: status,
            enrichment_model: nested_text(
                raw_asset,
                "enrichment_model",
                MAX_KNOWLEDGE_ENRICHMENT_MODEL_CHARACTERS,
                Some(""),
            )?,
            enrichment_error: nested_text(
                raw_asset,
                "enrichment_error",
                MAX_KNOWLEDGE_ENRICHMENT_ERROR_CHARACTERS,
                Some(""),
            )?,
            metadata_json: json_object_text(
                raw_asset,
                "metadata_json",
                "{}",
                MAX_KNOWLEDGE_ASSET_METADATA_CHARACTERS,
            )?,
            created_at: nested_number(raw_asset, "created_at")?,
            updated_at: nested_number(raw_asset, "updated_at")?,
        });
    }

    let mut chunks = Vec::with_capacity(raw_chunks.len());
    for (expected_ordinal, raw_chunk) in raw_chunks.iter().enumerate() {
        let raw_chunk = raw_chunk
            .as_object()
            .ok_or_else(|| invalid_input("knowledge chunk is not an object"))?;
        let ordinal = nested_integer(raw_chunk, "ordinal", None)?;
        if ordinal != expected_ordinal as u64 {
            return Err(invalid_input("knowledge chunk ordinals must be contiguous"));
        }
        let raw_refs = match raw_chunk.get("assets") {
            None => &Vec::new(),
            Some(value) => value
                .as_array()
                .ok_or_else(|| invalid_input("knowledge chunk assets must be an array"))?,
        };
        let mut references = Vec::with_capacity(raw_refs.len());
        let mut seen_references = HashSet::with_capacity(raw_refs.len());
        for raw_reference in raw_refs {
            let raw_reference = raw_reference
                .as_object()
                .ok_or_else(|| invalid_input("knowledge asset reference is not an object"))?;
            let asset_id =
                nested_text(raw_reference, "id", MAX_KNOWLEDGE_ASSET_ID_CHARACTERS, None)?;
            let relation = nested_text(
                raw_reference,
                "relation",
                MAX_KNOWLEDGE_RELATION_CHARACTERS,
                Some("evidence"),
            )?;
            if !asset_ids.contains(&asset_id) {
                return Err(invalid_input("knowledge chunk references an unknown asset"));
            }
            if !seen_references.insert((asset_id.clone(), relation.clone())) {
                return Err(invalid_input("knowledge asset reference is duplicated"));
            }
            references.push(ValidatedAssetRef {
                id: asset_id,
                relation,
            });
        }
        let search_text = nested_text(
            raw_chunk,
            "search_text",
            MAX_KNOWLEDGE_SEARCH_TEXT_CHARACTERS,
            None,
        )?;
        chunks.push(ValidatedChunk {
            section: nested_text(
                raw_chunk,
                "section",
                MAX_KNOWLEDGE_SECTION_CHARACTERS,
                Some(""),
            )?,
            location: nested_text(
                raw_chunk,
                "location",
                MAX_KNOWLEDGE_LOCATION_CHARACTERS,
                Some(""),
            )?,
            content: nested_text(
                raw_chunk,
                "content",
                MAX_KNOWLEDGE_CHUNK_CONTENT_CHARACTERS,
                None,
            )?,
            terms: search_terms(&search_text)?,
            search_text,
            refs: references,
        });
    }

    let chunk_count = nested_integer(source, "chunk_count", None)?;
    if chunk_count != chunks.len() as u64 {
        return Err(invalid_input("knowledge chunk_count does not match chunks"));
    }
    let scope = nested_text(
        source,
        "scope",
        MAX_KNOWLEDGE_SCOPE_CHARACTERS,
        Some("library"),
    )?;
    if !DOCUMENT_SCOPES.contains(&scope.as_str()) {
        return Err(invalid_input("knowledge document scope is invalid"));
    }
    Ok(ValidatedDocument {
        id: document_id,
        sha256: digest,
        name: nested_text(source, "name", MAX_KNOWLEDGE_NAME_CHARACTERS, None)?,
        stored_name: safe_stored_name(source)?,
        kind: nested_text(source, "kind", MAX_KNOWLEDGE_DOCUMENT_KIND_CHARACTERS, None)?,
        size_bytes: nested_integer(source, "size_bytes", None)?,
        method: nested_text(source, "method", MAX_KNOWLEDGE_METHOD_CHARACTERS, None)?,
        warnings_json: json_array_text(source, "warnings_json", "[]")?,
        text_chars: nested_integer(source, "text_chars", None)?,
        chunk_count,
        pages: nested_integer(source, "pages", Some(0))?,
        scope,
        media_metadata_json: json_object_text(
            source,
            "media_metadata_json",
            "{}",
            MAX_KNOWLEDGE_MEDIA_METADATA_CHARACTERS,
        )?,
        created_at: nested_number(source, "created_at")?,
        updated_at: nested_number(source, "updated_at")?,
        chunks,
        assets,
    })
}

impl ValidatedDocument {
    fn invariants_hold(&self) -> bool {
        !self.id.is_empty()
            && self.id.chars().count() <= MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS
            && self.sha256.len() == 64
            && self.chunk_count == self.chunks.len() as u64
            && self.chunks.len() <= MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT
            && self.assets.len() <= MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT
            && DOCUMENT_SCOPES.contains(&self.scope.as_str())
            && self
                .assets
                .iter()
                .all(|asset| ENRICHMENT_STATUSES.contains(&asset.enrichment_status.as_str()))
    }
}

// ---------------------------------------------------------------------------
// Record I/O.
// ---------------------------------------------------------------------------

fn read_document_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Option<DocumentRecord>> {
    let key = document_entity_key(transaction, document_id)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        DOCUMENT_LOGICAL_NAMESPACE,
        document_id,
        transaction.owner_user_id(),
        MAX_KNOWLEDGE_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let record: DocumentRecord = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("knowledge document record is malformed"))?;
    if record.id != document_id {
        return Err(invalid_data("knowledge document record identity differs"));
    }
    Ok(Some(record))
}

fn write_document_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    record: &DocumentRecord,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(record)
        .map_err(|_| invalid_data("knowledge document record cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_entity_key(transaction, &record.id)?,
            namespace: DOCUMENT_LOGICAL_NAMESPACE.to_owned(),
            logical_key: record.id.clone(),
            value_json,
            expected_version: None,
            updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_KNOWLEDGE_DOCUMENT_BYTES,
    )
    .map(|_| ())
}

fn read_asset_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    asset_id: &str,
) -> io::Result<Option<AssetRecord>> {
    let key = asset_entity_key(transaction, asset_id)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        ASSET_LOGICAL_NAMESPACE,
        asset_id,
        transaction.owner_user_id(),
        MAX_KNOWLEDGE_ASSET_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let record: AssetRecord = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("knowledge asset record is malformed"))?;
    if record.id != asset_id {
        return Err(invalid_data("knowledge asset record identity differs"));
    }
    Ok(Some(record))
}

fn write_asset_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    record: &AssetRecord,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(record)
        .map_err(|_| invalid_data("knowledge asset record cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: asset_entity_key(transaction, &record.id)?,
            namespace: ASSET_LOGICAL_NAMESPACE.to_owned(),
            logical_key: record.id.clone(),
            value_json,
            expected_version: None,
            updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_KNOWLEDGE_ASSET_DOCUMENT_BYTES,
    )
    .map(|_| ())
}

fn read_chunk_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
    ordinal: u64,
) -> io::Result<Option<ChunkRecord>> {
    let key = chunk_entity_key(transaction, document_id, ordinal)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        CHUNK_LOGICAL_NAMESPACE,
        &document_chunk_logical_key(document_id, ordinal),
        transaction.owner_user_id(),
        MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let record: ChunkRecord = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("knowledge chunk record is malformed"))?;
    if record.ordinal != ordinal {
        return Err(invalid_data("knowledge chunk record identity differs"));
    }
    Ok(Some(record))
}

fn write_chunk_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
    record: &ChunkRecord,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(record)
        .map_err(|_| invalid_data("knowledge chunk record cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: chunk_entity_key(transaction, document_id, record.ordinal)?,
            namespace: CHUNK_LOGICAL_NAMESPACE.to_owned(),
            logical_key: document_chunk_logical_key(document_id, record.ordinal),
            value_json,
            expected_version: None,
            updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES,
    )
    .map(|_| ())
}

fn read_settings(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<SettingsRecord>> {
    let key = owner_key(transaction, KNOWLEDGE_SETTINGS_NAMESPACE, SETTINGS_KEY)?;
    let Some(raw) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let record: SettingsRecord = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("knowledge settings record is malformed"))?;
    Ok(Some(record))
}

fn write_settings(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    record: &SettingsRecord,
) -> io::Result<()> {
    let raw = serde_json::to_vec(record)
        .map_err(|_| invalid_data("knowledge settings record cannot be encoded"))?;
    database.entity_put(
        transaction,
        owner_key(transaction, KNOWLEDGE_SETTINGS_NAMESPACE, SETTINGS_KEY)?,
        raw,
    )
}

fn next_document_sequence(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let key = owner_key(
        transaction,
        KNOWLEDGE_SETTINGS_NAMESPACE,
        DOCUMENT_SEQUENCE_KEY,
    )?;
    let current = match database.entity_get(transaction, &key)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().expect("length checked")),
        Some(_) => return Err(invalid_data("knowledge document sequence is malformed")),
    };
    let next = current
        .checked_add(1)
        .ok_or_else(|| invalid_data("knowledge document sequence overflow"))?;
    database.entity_put(transaction, key, next.to_le_bytes().to_vec())?;
    Ok(current)
}

// ---------------------------------------------------------------------------
// Derived index maintenance. Every helper preserves the invariant that the
// queue holds exactly the pending/running assets and that the tenant-global
// owner entry exists exactly for (visual_enrichment AND scoped pending/
// running count > 0).
// ---------------------------------------------------------------------------

fn queue_insert(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    asset: &AssetRecord,
) -> io::Result<()> {
    if !in_queue_class(&asset.enrichment_status) {
        return Ok(());
    }
    let raw = queue_raw(
        &asset.kind,
        asset.created_at,
        &asset.document_id,
        asset.ordinal,
    )?;
    database.entity_put(
        transaction,
        owner_key(transaction, KNOWLEDGE_ENRICHMENT_QUEUE_NAMESPACE, &raw)?,
        asset.id.as_bytes().to_vec(),
    )
}

fn queue_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    asset: &AssetRecord,
) -> io::Result<()> {
    let raw = queue_raw(
        &asset.kind,
        asset.created_at,
        &asset.document_id,
        asset.ordinal,
    )?;
    database.entity_delete(
        transaction,
        owner_key(transaction, KNOWLEDGE_ENRICHMENT_QUEUE_NAMESPACE, &raw)?,
    )
}

fn decode_owner_index(raw: &[u8]) -> io::Result<u64> {
    if raw.len() != 16 {
        return Err(invalid_data(
            "knowledge enrichment owner index is malformed",
        ));
    }
    Ok(u64::from_le_bytes(
        raw[8..16].try_into().expect("length checked"),
    ))
}

fn adjust_owner_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    delta: i64,
) -> io::Result<()> {
    if delta == 0 {
        return Ok(());
    }
    let visual_enrichment = read_settings(database, transaction)?
        .map(|settings| settings.visual_enrichment)
        .unwrap_or(false);
    let key = owner_index_entity_key(transaction)?;
    if !visual_enrichment {
        // Without consent the owner is never eligible; the entry is already
        // absent by invariant, so the delete is a defensive no-op.
        database.entity_delete(transaction, key)?;
        return Ok(());
    }
    let current = match database.entity_get(transaction, &key)? {
        None => 0,
        Some(raw) => decode_owner_index(&raw)?,
    };
    let next = i64::try_from(current)
        .ok()
        .and_then(|current| current.checked_add(delta))
        .filter(|next| *next >= 0)
        .ok_or_else(|| invalid_data("knowledge enrichment owner count underflow"))?;
    if next > 0 {
        let mut value = Vec::with_capacity(16);
        value.extend_from_slice(&transaction.owner_user_id().to_be_bytes());
        value.extend_from_slice(&(next as u64).to_le_bytes());
        database.entity_put(transaction, key, value)?;
    } else {
        database.entity_delete(transaction, key)?;
    }
    Ok(())
}

fn resync_owner_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<()> {
    let scoped = scoped_document_ids(database, transaction)?;
    let mut count = 0_u64;
    for asset in scan_owner_assets(database, transaction)? {
        if scoped.contains(&asset.document_id) && in_queue_class(&asset.enrichment_status) {
            count = count
                .checked_add(1)
                .ok_or_else(|| resource_exhausted("knowledge owner asset count overflow"))?;
        }
    }
    let key = owner_index_entity_key(transaction)?;
    if count > 0 {
        let mut value = Vec::with_capacity(16);
        value.extend_from_slice(&transaction.owner_user_id().to_be_bytes());
        value.extend_from_slice(&count.to_le_bytes());
        database.entity_put(transaction, key, value)?;
    } else {
        database.entity_delete(transaction, key)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Enumeration helpers.
// ---------------------------------------------------------------------------

fn list_document_records(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<DocumentRecord>> {
    let rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_DOCUMENT_NAMESPACE,
        b"",
        MAX_KNOWLEDGE_CATALOG_SCAN_DOCUMENTS,
    )?;
    let mut records = Vec::with_capacity(rows.len());
    for (key, _) in rows {
        let (document_id, _) = read_text(key.key_bytes())
            .map_err(|_| invalid_data("knowledge document key is malformed"))?;
        let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
            database,
            transaction,
            &key,
            DOCUMENT_LOGICAL_NAMESPACE,
            &document_id,
            transaction.owner_user_id(),
            MAX_KNOWLEDGE_DOCUMENT_BYTES,
        )?
        else {
            return Err(invalid_data("knowledge document catalog row is missing"));
        };
        records.push(
            serde_json::from_slice(&raw)
                .map_err(|_| invalid_data("knowledge document record is malformed"))?,
        );
    }
    Ok(records)
}

fn scoped_document_ids(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<HashSet<String>> {
    let records = list_document_records(database, transaction)?;
    Ok(records
        .into_iter()
        .filter(|record| is_scoped(&record.scope))
        .map(|record| record.id)
        .collect())
}

fn list_document_assets(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Vec<AssetRecord>> {
    let prefix = document_raw(document_id)?;
    let rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE,
        &prefix,
        MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT,
    )?;
    let mut assets = Vec::with_capacity(rows.len());
    for (_, asset_id) in rows {
        let asset_id = String::from_utf8(asset_id)
            .map_err(|_| invalid_data("knowledge asset ordinal index is not UTF-8"))?;
        let record = read_asset_record(database, transaction, &asset_id)?.ok_or_else(|| {
            invalid_data("knowledge asset ordinal index references a missing asset")
        })?;
        if record.document_id != document_id {
            return Err(invalid_data(
                "knowledge asset ordinal index references a foreign asset",
            ));
        }
        assets.push(record);
    }
    Ok(assets)
}

fn scan_owner_assets(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<AssetRecord>> {
    let rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_ASSET_DOCUMENT_NAMESPACE,
        b"",
        MAX_KNOWLEDGE_OWNER_ASSET_ROWS,
    )?;
    let mut assets = Vec::with_capacity(rows.len());
    for (key, _) in rows {
        let asset_id = read_text(key.key_bytes())
            .map(|(asset_id, _)| asset_id)
            .map_err(|_| invalid_data("knowledge asset key is malformed"))?;
        let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
            database,
            transaction,
            &key,
            ASSET_LOGICAL_NAMESPACE,
            &asset_id,
            transaction.owner_user_id(),
            MAX_KNOWLEDGE_ASSET_DOCUMENT_BYTES,
        )?
        else {
            return Err(invalid_data("knowledge asset catalog row is missing"));
        };
        assets.push(
            serde_json::from_slice(&raw)
                .map_err(|_| invalid_data("knowledge asset record is malformed"))?,
        );
    }
    Ok(assets)
}

struct AssetCounts {
    total: u64,
    pending: u64,
    issues: u64,
}

fn counts_by_document(assets: &[AssetRecord]) -> HashMap<String, AssetCounts> {
    let mut counts: HashMap<String, AssetCounts> = HashMap::new();
    for asset in assets {
        let entry = counts
            .entry(asset.document_id.clone())
            .or_insert(AssetCounts {
                total: 0,
                pending: 0,
                issues: 0,
            });
        entry.total += 1;
        if in_queue_class(&asset.enrichment_status) {
            entry.pending += 1;
        }
        if in_issue_class(&asset.enrichment_status) {
            entry.issues += 1;
        }
    }
    counts
}

fn document_asset_counts(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<AssetCounts> {
    let assets = list_document_assets(database, transaction, document_id)?;
    let mut counts = AssetCounts {
        total: 0,
        pending: 0,
        issues: 0,
    };
    for asset in &assets {
        counts.total += 1;
        if in_queue_class(&asset.enrichment_status) {
            counts.pending += 1;
        }
        if in_issue_class(&asset.enrichment_status) {
            counts.issues += 1;
        }
    }
    Ok(counts)
}

// ---------------------------------------------------------------------------
// JSON projections (legacy row shapes).
// ---------------------------------------------------------------------------

fn metadata_json(record: &DocumentRecord, counts: &AssetCounts) -> Value {
    json!({
        "id": record.id,
        "sha256": record.sha256,
        "name": record.name,
        "stored_name": record.stored_name,
        "kind": record.kind,
        "size_bytes": record.size_bytes,
        "method": record.method,
        "warnings_json": record.warnings_json,
        "text_chars": record.text_chars,
        "chunk_count": record.chunk_count,
        "pages": record.pages,
        "scope": record.scope,
        "media_metadata_json": record.media_metadata_json,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "asset_count": counts.total,
        "pending_asset_count": counts.pending,
        "asset_issue_count": counts.issues,
    })
}

fn asset_json(record: &AssetRecord) -> Value {
    json!({
        "id": record.id,
        "ordinal": record.ordinal,
        "kind": record.kind,
        "stored_name": record.stored_name,
        "mime_type": record.mime_type,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "width": record.width,
        "height": record.height,
        "page": record.page,
        "pages_json": record.pages_json,
        "bbox_json": record.bbox_json,
        "caption": record.caption,
        "ocr_text": record.ocr_text,
        "description": record.description,
        "enrichment_status": record.enrichment_status,
        "enrichment_model": record.enrichment_model,
        "enrichment_error": record.enrichment_error,
        "metadata_json": record.metadata_json,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    })
}

fn asset_projection_json(record: &AssetRecord, document_name: &str) -> Value {
    json!({
        "id": record.id,
        "ordinal": record.ordinal,
        "kind": record.kind,
        "stored_name": record.stored_name,
        "mime_type": record.mime_type,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "width": record.width,
        "height": record.height,
        "page": record.page,
        "pages_json": record.pages_json,
        "bbox_json": record.bbox_json,
        "caption": record.caption,
        "ocr_text": record.ocr_text,
        "description": record.description,
        "enrichment_status": record.enrichment_status,
        "enrichment_model": record.enrichment_model,
        "enrichment_error": record.enrichment_error,
        "metadata_json": record.metadata_json,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "document_id": record.document_id,
        "document_name": document_name,
    })
}

fn chunk_json(record: &ChunkRecord, refs: &[ValidatedAssetRef]) -> Value {
    json!({
        "ordinal": record.ordinal,
        "section": record.section,
        "location": record.location,
        "content": record.content,
        "search_text": record.search_text,
        "assets": refs.iter().map(|reference| json!({
            "id": reference.id,
            "relation": reference.relation,
        })).collect::<Vec<_>>(),
    })
}

fn settings_json(record: Option<&SettingsRecord>) -> Value {
    json!({
        "enabled": record.is_some_and(|settings| settings.enabled),
        "visual_enrichment": record.is_some_and(|settings| settings.visual_enrichment),
    })
}

fn encode_response(value: &Value) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data("knowledge response cannot be encoded"))
}

fn load_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Option<Value>> {
    let Some(record) = read_document_record(database, transaction, document_id)? else {
        return Ok(None);
    };
    let counts = document_asset_counts(database, transaction, document_id)?;
    Ok(Some(metadata_json(&record, &counts)))
}

fn list_chunk_refs(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
    chunk_ordinal: u64,
) -> io::Result<Vec<ValidatedAssetRef>> {
    let mut prefix = document_raw(document_id)?;
    prefix.extend_from_slice(
        &u32::try_from(chunk_ordinal)
            .map_err(|_| invalid_input("knowledge chunk ordinal exceeds its encoded bound"))?
            .to_be_bytes(),
    );
    let rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_CHUNK_ASSET_LINK_NAMESPACE,
        &prefix,
        MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN,
    )?;
    let mut refs = Vec::with_capacity(rows.len());
    for (_, value) in rows {
        let (asset_id, consumed) = read_text(&value)?;
        let relation = String::from_utf8(value[consumed..].to_vec())
            .map_err(|_| invalid_data("knowledge asset link relation is not UTF-8"))?;
        refs.push(ValidatedAssetRef {
            id: asset_id,
            relation,
        });
    }
    Ok(refs)
}

// ---------------------------------------------------------------------------
// Document lifecycle: shared insert/cascade used by create/replace/delete.
// ---------------------------------------------------------------------------

fn insert_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &ValidatedDocument,
    sequence: u64,
    now_unix_ms: u64,
) -> io::Result<()> {
    let document_record = DocumentRecord {
        id: document.id.clone(),
        sha256: document.sha256.clone(),
        name: document.name.clone(),
        stored_name: document.stored_name.clone(),
        kind: document.kind.clone(),
        size_bytes: document.size_bytes,
        method: document.method.clone(),
        warnings_json: document.warnings_json.clone(),
        text_chars: document.text_chars,
        chunk_count: document.chunk_count,
        pages: document.pages,
        scope: document.scope.clone(),
        media_metadata_json: document.media_metadata_json.clone(),
        created_at: document.created_at,
        updated_at: document.updated_at,
        sequence,
    };
    write_document_record(database, transaction, &document_record, now_unix_ms)?;

    let mut queue_contribution = 0_i64;
    for (ordinal, asset) in document.assets.iter().enumerate() {
        let record = AssetRecord {
            id: asset.id.clone(),
            document_id: document.id.clone(),
            ordinal: ordinal as u64,
            kind: asset.kind.clone(),
            stored_name: asset.stored_name.clone(),
            mime_type: asset.mime_type.clone(),
            sha256: asset.sha256.clone(),
            size_bytes: asset.size_bytes,
            width: asset.width,
            height: asset.height,
            page: asset.page,
            pages_json: asset.pages_json.clone(),
            bbox_json: asset.bbox_json.clone(),
            caption: asset.caption.clone(),
            ocr_text: asset.ocr_text.clone(),
            description: asset.description.clone(),
            enrichment_status: asset.enrichment_status.clone(),
            enrichment_model: asset.enrichment_model.clone(),
            enrichment_error: asset.enrichment_error.clone(),
            metadata_json: asset.metadata_json.clone(),
            created_at: asset.created_at,
            updated_at: asset.updated_at,
        };
        write_asset_record(database, transaction, &record, now_unix_ms)?;
        database.entity_put(
            transaction,
            owner_key(
                transaction,
                KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE,
                &ordinal_raw(&document.id, ordinal as u64)?,
            )?,
            asset.id.as_bytes().to_vec(),
        )?;
        if in_queue_class(&record.enrichment_status) {
            queue_insert(database, transaction, &record)?;
            if is_scoped(&document.scope) {
                queue_contribution += 1;
            }
        }
    }

    for (ordinal, chunk) in document.chunks.iter().enumerate() {
        let record = ChunkRecord {
            ordinal: ordinal as u64,
            section: chunk.section.clone(),
            location: chunk.location.clone(),
            content: chunk.content.clone(),
            search_text: chunk.search_text.clone(),
            terms: chunk.terms.clone(),
        };
        write_chunk_record(database, transaction, &document.id, &record, now_unix_ms)?;
        for (reference_ordinal, reference) in chunk.refs.iter().enumerate() {
            let mut value = Vec::with_capacity(reference.id.len() + reference.relation.len() + 2);
            push_text(&mut value, &reference.id)?;
            value.extend_from_slice(reference.relation.as_bytes());
            database.entity_put(
                transaction,
                owner_key(
                    transaction,
                    KNOWLEDGE_CHUNK_ASSET_LINK_NAMESPACE,
                    &link_raw(
                        &document.id,
                        ordinal as u64,
                        reference_ordinal as u64,
                        &reference.id,
                    )?,
                )?,
                value,
            )?;
            database.entity_put(
                transaction,
                owner_key(
                    transaction,
                    KNOWLEDGE_ASSET_CHUNK_REVERSE_NAMESPACE,
                    &reverse_raw(&reference.id, &document.id, ordinal as u64)?,
                )?,
                Vec::new(),
            )?;
        }
        for term in &chunk.terms {
            let mut value = Vec::with_capacity(document.id.len() + 6);
            push_text(&mut value, &document.id)?;
            value.extend_from_slice(&(ordinal as u32).to_be_bytes());
            database.entity_put(
                transaction,
                owner_key(
                    transaction,
                    KNOWLEDGE_TERM_NAMESPACE,
                    &term_raw(term, &document.id, ordinal as u64)?,
                )?,
                value,
            )?;
        }
    }

    digest_index_insert(
        database,
        transaction,
        &document.sha256,
        sequence,
        &document.id,
    )?;
    adjust_owner_index(database, transaction, queue_contribution)?;
    Ok(())
}

fn digest_index_insert(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &str,
    sequence: u64,
    document_id: &str,
) -> io::Result<()> {
    let key = owner_key(
        transaction,
        KNOWLEDGE_DIGEST_INDEX_NAMESPACE,
        digest.as_bytes(),
    )?;
    let mut entries = match database.entity_get(transaction, &key)? {
        None => Vec::new(),
        Some(raw) => decode_digest_index(&raw)?,
    };
    let position = entries
        .binary_search_by_key(&sequence, |(sequence, _)| *sequence)
        .unwrap_or_else(|position| position);
    entries.insert(position, (sequence, document_id.to_owned()));
    database.entity_put(transaction, key, encode_digest_index(&entries)?)
}

fn digest_index_remove(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &str,
    document_id: &str,
) -> io::Result<()> {
    let key = owner_key(
        transaction,
        KNOWLEDGE_DIGEST_INDEX_NAMESPACE,
        digest.as_bytes(),
    )?;
    let Some(raw) = database.entity_get(transaction, &key)? else {
        return Err(invalid_data("knowledge digest index entry is missing"));
    };
    let mut entries = decode_digest_index(&raw)?;
    let before = entries.len();
    entries.retain(|(_, id)| id != document_id);
    if entries.len() == before {
        return Err(invalid_data("knowledge digest index entry is missing"));
    }
    if entries.is_empty() {
        database.entity_delete(transaction, key)
    } else {
        database.entity_put(transaction, key, encode_digest_index(&entries)?)
    }
}

fn decode_digest_index(raw: &[u8]) -> io::Result<Vec<(u64, String)>> {
    let mut entries = Vec::new();
    let mut cursor = 0_usize;
    while cursor < raw.len() {
        if raw.len() < cursor + 8 {
            return Err(invalid_data("knowledge digest index is malformed"));
        }
        let sequence =
            u64::from_be_bytes(raw[cursor..cursor + 8].try_into().expect("length checked"));
        let (document_id, consumed) = read_text(&raw[cursor + 8..])?;
        entries.push((sequence, document_id));
        cursor += 8 + consumed;
    }
    Ok(entries)
}

fn encode_digest_index(entries: &[(u64, String)]) -> io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    for (sequence, document_id) in entries {
        raw.extend_from_slice(&sequence.to_be_bytes());
        push_text(&mut raw, document_id)?;
    }
    Ok(raw)
}

fn cascade_delete_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &DocumentRecord,
    assets: &[AssetRecord],
) -> io::Result<()> {
    let mut queue_delta = 0_i64;
    for asset in assets {
        if in_queue_class(&asset.enrichment_status) {
            queue_delete(database, transaction, asset)?;
            if is_scoped(&document.scope) {
                queue_delta -= 1;
            }
        }
        // Reverse links for this asset.
        let reverse_prefix = asset_raw(&asset.id)?;
        let reverse_rows = scan_prefix(
            database,
            transaction,
            KNOWLEDGE_ASSET_CHUNK_REVERSE_NAMESPACE,
            &reverse_prefix,
            MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN,
        )?;
        for (key, _) in reverse_rows {
            database.entity_delete(transaction, key)?;
        }
        database.entity_delete(
            transaction,
            owner_key(
                transaction,
                KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE,
                &ordinal_raw(&document.id, asset.ordinal)?,
            )?,
        )?;
        versioned_document::delete(
            database,
            transaction,
            asset_entity_key(transaction, &asset.id)?,
            ASSET_LOGICAL_NAMESPACE,
            &asset.id,
            None,
        )?;
    }
    // Chunks and their postings.
    let chunk_prefix = document_raw(&document.id)?;
    let chunk_rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_CHUNK_DOCUMENT_NAMESPACE,
        &chunk_prefix,
        MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT,
    )?;
    for (chunk_key, _) in chunk_rows {
        let (chunk_document_id, chunk_ordinal) = parse_chunk_key(chunk_key.key_bytes())?;
        let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
            database,
            transaction,
            &chunk_key,
            CHUNK_LOGICAL_NAMESPACE,
            &document_chunk_logical_key(&chunk_document_id, chunk_ordinal),
            transaction.owner_user_id(),
            MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES,
        )?
        else {
            return Err(invalid_data(
                "knowledge chunk row is missing during cascade",
            ));
        };
        let chunk: ChunkRecord = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("knowledge chunk record is malformed"))?;
        for term in &chunk.terms {
            database.entity_delete(
                transaction,
                owner_key(
                    transaction,
                    KNOWLEDGE_TERM_NAMESPACE,
                    &term_raw(term, &document.id, chunk.ordinal)?,
                )?,
            )?;
        }
        versioned_document::delete(
            database,
            transaction,
            chunk_key,
            CHUNK_LOGICAL_NAMESPACE,
            &document_chunk_logical_key(&document.id, chunk.ordinal),
            None,
        )?;
    }
    // Forward links for the whole document.
    let link_rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_CHUNK_ASSET_LINK_NAMESPACE,
        &chunk_prefix,
        MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN,
    )?;
    for (key, _) in link_rows {
        database.entity_delete(transaction, key)?;
    }
    digest_index_remove(database, transaction, &document.sha256, &document.id)?;
    versioned_document::delete(
        database,
        transaction,
        document_entity_key(transaction, &document.id)?,
        DOCUMENT_LOGICAL_NAMESPACE,
        &document.id,
        None,
    )?;
    adjust_owner_index(database, transaction, queue_delta)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Request surface.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub enum KnowledgeRequest {
    DocumentList,
    DocumentGet {
        document_id: String,
    },
    DocumentMetadata {
        document_id: String,
    },
    DocumentAssets {
        document_id: String,
        offset: u64,
        limit: u64,
    },
    DocumentContent {
        document_id: String,
        offset: u64,
        limit: u64,
    },
    DocumentPatch {
        document_id: String,
        scope: Option<String>,
        media_metadata_json: Option<String>,
        now_unix_ms: u64,
    },
    DocumentFindDigest {
        sha256: String,
    },
    DocumentCreate {
        document: ValidatedDocument,
        now_unix_ms: u64,
    },
    DocumentReplace {
        document: ValidatedDocument,
        now_unix_ms: u64,
    },
    DocumentDelete {
        document_id: String,
    },
    SettingsGet,
    SettingsPatch {
        enabled: Option<bool>,
        visual_enrichment: Option<bool>,
        now_unix_ms: u64,
    },
    Availability,
    AssetGet {
        asset_id: String,
    },
    EnrichmentActivity,
    EnrichmentOwners {
        limit: u64,
    },
    AssetClaim {
        now_unix_ms: u64,
    },
    AssetUpdate {
        asset_id: String,
        updates: Vec<(String, String)>,
        chunk_content: Option<Value>,
        chunk_search_text: Option<Value>,
        now_unix_ms: u64,
    },
    AssetsMarkNoVision {
        now_unix_ms: u64,
    },
    Catalog {
        page: u64,
        page_size: u64,
        query: String,
        category: String,
        sort: String,
    },
    SearchCandidates {
        tokens: Vec<String>,
        document_id: String,
        limit: u64,
    },
    OwnerClear,
}

fn valid_document_id(document_id: &str) -> bool {
    !document_id.is_empty() && document_id.chars().count() <= MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS
}

fn valid_asset_id(asset_id: &str) -> bool {
    !asset_id.is_empty() && asset_id.chars().count() <= MAX_KNOWLEDGE_ASSET_ID_CHARACTERS
}

fn valid_window(offset: u64, limit: u64) -> bool {
    (1..=MAX_KNOWLEDGE_PAGE_ROWS as u64).contains(&limit) && offset <= u32::MAX as u64
}

impl KnowledgeRequest {
    pub fn mutates_state(&self) -> bool {
        matches!(
            self,
            Self::DocumentCreate { .. }
                | Self::DocumentReplace { .. }
                | Self::DocumentDelete { .. }
                | Self::DocumentPatch { .. }
                | Self::SettingsPatch { .. }
                | Self::AssetClaim { .. }
                | Self::AssetUpdate { .. }
                | Self::AssetsMarkNoVision { .. }
                | Self::OwnerClear
        )
    }

    pub fn validate(&self) -> io::Result<usize> {
        let literal_bytes = match self {
            Self::DocumentList
            | Self::SettingsGet
            | Self::Availability
            | Self::EnrichmentActivity
            | Self::OwnerClear => 0,
            Self::DocumentGet { document_id }
            | Self::DocumentMetadata { document_id }
            | Self::DocumentDelete { document_id, .. } => {
                if !valid_document_id(document_id) {
                    return Err(invalid_input("invalid knowledge document identity"));
                }
                document_id.len()
            }
            Self::DocumentAssets {
                document_id,
                offset,
                limit,
            }
            | Self::DocumentContent {
                document_id,
                offset,
                limit,
            } => {
                if !valid_document_id(document_id) || !valid_window(*offset, *limit) {
                    return Err(invalid_input("invalid knowledge document window"));
                }
                document_id.len() + 16
            }
            Self::DocumentPatch {
                document_id,
                scope,
                media_metadata_json,
                now_unix_ms,
            } => {
                let scope_valid = scope
                    .as_deref()
                    .is_none_or(|scope| DOCUMENT_SCOPES.contains(&scope));
                let metadata_valid = media_metadata_json.as_deref().is_none_or(|metadata| {
                    metadata.chars().count() <= MAX_KNOWLEDGE_MEDIA_METADATA_CHARACTERS
                        && serde_json::from_str::<Value>(metadata)
                            .is_ok_and(|decoded| decoded.is_object())
                });
                if !valid_document_id(document_id)
                    || !scope_valid
                    || !metadata_valid
                    || (scope.is_none() && media_metadata_json.is_none())
                    || *now_unix_ms == 0
                {
                    return Err(invalid_input("invalid knowledge document patch"));
                }
                document_id.len()
                    + scope.as_deref().map_or(0, str::len)
                    + media_metadata_json.as_deref().map_or(0, str::len)
            }
            Self::DocumentFindDigest { sha256 } => {
                if sha256.len() != 64
                    || !sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                {
                    return Err(invalid_input("invalid knowledge document sha256"));
                }
                64
            }
            Self::DocumentCreate { document, .. } | Self::DocumentReplace { document, .. } => {
                if !document.invariants_hold() {
                    return Err(invalid_input("invalid knowledge document payload"));
                }
                document.id.len() + document.sha256.len() + 256
            }
            Self::SettingsPatch { now_unix_ms, .. } => {
                // An empty patch is a legacy-sanctioned no-op upsert.
                if *now_unix_ms == 0 {
                    return Err(invalid_input("invalid knowledge settings patch"));
                }
                2
            }
            Self::AssetGet { asset_id } => {
                if !valid_asset_id(asset_id) {
                    return Err(invalid_input("invalid knowledge asset identity"));
                }
                asset_id.len()
            }
            Self::EnrichmentOwners { limit } => {
                if !(1..=MAX_KNOWLEDGE_ENRICHMENT_OWNER_ROWS as u64).contains(limit) {
                    return Err(invalid_input("invalid knowledge enrichment owner limit"));
                }
                8
            }
            Self::AssetClaim { now_unix_ms } | Self::AssetsMarkNoVision { now_unix_ms } => {
                if *now_unix_ms == 0 {
                    return Err(invalid_input("invalid knowledge enrichment mutation"));
                }
                0
            }
            Self::AssetUpdate {
                asset_id,
                updates,
                chunk_content,
                chunk_search_text,
                now_unix_ms,
            } => {
                if !valid_asset_id(asset_id) || updates.is_empty() || *now_unix_ms == 0 {
                    return Err(invalid_input("invalid knowledge asset update"));
                }
                let mut bytes = asset_id.len();
                for (field, value) in updates {
                    let maximum = if matches!(field.as_str(), "description" | "ocr_text") {
                        MAX_KNOWLEDGE_ASSET_TEXT_CHARACTERS
                    } else {
                        MAX_KNOWLEDGE_ENRICHMENT_ERROR_CHARACTERS
                    };
                    if !MUTABLE_ASSET_FIELDS.contains(&field.as_str())
                        || value.chars().count() > maximum
                        || (field == "enrichment_status"
                            && !ENRICHMENT_STATUSES.contains(&value.as_str()))
                    {
                        return Err(invalid_input("invalid knowledge asset update field"));
                    }
                    bytes = bytes
                        .checked_add(field.len() + value.len())
                        .ok_or_else(|| resource_exhausted("knowledge update byte overflow"))?;
                }
                for optional in [chunk_content, chunk_search_text].into_iter().flatten() {
                    match optional {
                        Value::Null => {}
                        Value::String(text) => {
                            bytes = bytes.checked_add(text.len()).ok_or_else(|| {
                                resource_exhausted("knowledge update byte overflow")
                            })?;
                        }
                        _ => {}
                    }
                }
                bytes
            }
            Self::Catalog {
                page,
                page_size,
                query,
                category,
                sort,
            } => {
                if *page == 0
                    || !(1..=MAX_KNOWLEDGE_CATALOG_PAGE_SIZE as u64).contains(page_size)
                    || query.chars().count() > MAX_KNOWLEDGE_CATALOG_QUERY_CHARACTERS
                    || !DOCUMENT_CATEGORIES.contains(&category.as_str())
                    || !DOCUMENT_SORTS.contains(&sort.as_str())
                {
                    return Err(invalid_input("invalid knowledge catalogue filter"));
                }
                query.len() + category.len() + sort.len() + 24
            }
            Self::SearchCandidates {
                tokens,
                document_id,
                limit,
            } => {
                if tokens.is_empty()
                    || tokens.len() > MAX_KNOWLEDGE_SEARCH_TOKENS
                    || !(1..=MAX_KNOWLEDGE_PAGE_ROWS as u64).contains(limit)
                    || document_id.chars().count() > MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS
                {
                    return Err(invalid_input("invalid knowledge search request"));
                }
                let mut bytes = document_id.len() + 16;
                for token in tokens {
                    if token.is_empty() || token.chars().count() > MAX_KNOWLEDGE_TERM_CHARACTERS {
                        return Err(invalid_input("invalid knowledge search token"));
                    }
                    bytes = bytes
                        .checked_add(token.len())
                        .ok_or_else(|| resource_exhausted("knowledge search byte overflow"))?;
                }
                bytes
            }
        };
        if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
            return Err(resource_exhausted(
                "knowledge request exceeds the IR literal budget",
            ));
        }
        Ok(literal_bytes)
    }
}

// ---------------------------------------------------------------------------
// Operation implementations.
// ---------------------------------------------------------------------------

fn document_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let records = list_document_records(database, transaction)?;
    let owner_assets = scan_owner_assets(database, transaction)?;
    let counts = counts_by_document(&owner_assets);
    let mut scoped: Vec<&DocumentRecord> = records
        .iter()
        .filter(|record| is_scoped(&record.scope))
        .collect();
    scoped.sort_by(|left, right| {
        right
            .created_at
            .partial_cmp(&left.created_at)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| right.id.cmp(&left.id))
    });
    let empty = AssetCounts {
        total: 0,
        pending: 0,
        issues: 0,
    };
    let rows = scoped
        .into_iter()
        .map(|record| metadata_json(record, counts.get(&record.id).unwrap_or(&empty)))
        .collect::<Vec<_>>();
    encode_response(&Value::Array(rows))
}

fn document_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(record) = read_document_record(database, transaction, document_id)? else {
        return Ok(None);
    };
    let counts = document_asset_counts(database, transaction, document_id)?;
    let assets = list_document_assets(database, transaction, document_id)?;
    let chunk_prefix = document_raw(document_id)?;
    let chunk_rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_CHUNK_DOCUMENT_NAMESPACE,
        &chunk_prefix,
        MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT,
    )?;
    let mut chunks = Vec::with_capacity(chunk_rows.len());
    for (chunk_key, _) in chunk_rows {
        let (chunk_document_id, chunk_ordinal) = parse_chunk_key(chunk_key.key_bytes())?;
        let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
            database,
            transaction,
            &chunk_key,
            CHUNK_LOGICAL_NAMESPACE,
            &document_chunk_logical_key(&chunk_document_id, chunk_ordinal),
            transaction.owner_user_id(),
            MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES,
        )?
        else {
            return Err(invalid_data("knowledge chunk row is missing"));
        };
        let chunk: ChunkRecord = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("knowledge chunk record is malformed"))?;
        let refs = list_chunk_refs(database, transaction, document_id, chunk.ordinal)?;
        chunks.push(chunk_json(&chunk, &refs));
    }
    let mut document = metadata_json(&record, &counts);
    let object = document
        .as_object_mut()
        .ok_or_else(|| invalid_data("knowledge document projection is malformed"))?;
    object.insert("chunks".to_owned(), Value::Array(chunks));
    object.insert(
        "assets".to_owned(),
        Value::Array(assets.iter().map(asset_json).collect()),
    );
    encode_response(&document).map(Some)
}

fn document_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    load_metadata(database, transaction, document_id)?
        .map(|metadata| encode_response(&metadata))
        .transpose()
}

fn document_assets(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
    offset: u64,
    limit: u64,
) -> io::Result<Option<Vec<u8>>> {
    if read_document_record(database, transaction, document_id)?.is_none() {
        return Ok(None);
    }
    let prefix = document_raw(document_id)?;
    let rows = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE,
        &prefix,
        MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT,
    )?;
    let mut assets = Vec::new();
    for (_, asset_id) in rows
        .iter()
        .skip(usize::try_from(offset).unwrap_or(usize::MAX))
        .take(limit as usize)
    {
        let asset_id = String::from_utf8(asset_id.clone())
            .map_err(|_| invalid_data("knowledge asset ordinal index is not UTF-8"))?;
        let record = read_asset_record(database, transaction, &asset_id)?.ok_or_else(|| {
            invalid_data("knowledge asset ordinal index references a missing asset")
        })?;
        assets.push(asset_json(&record));
    }
    encode_response(&Value::Array(assets)).map(Some)
}

fn document_content(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
    offset: u64,
    limit: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some(record) = read_document_record(database, transaction, document_id)? else {
        return Ok(None);
    };
    let counts = document_asset_counts(database, transaction, document_id)?;
    let mut chunks = Vec::new();
    if offset <= u32::MAX as u64 {
        let upper = offset.saturating_add(limit).min(u32::MAX as u64 + 1);
        for ordinal in offset..upper {
            let Some(chunk) = read_chunk_record(database, transaction, document_id, ordinal)?
            else {
                // Legacy range-selects ordinals >= offset; a gap simply
                // contributes no row. Validated documents are contiguous, so
                // the first gap ends the page.
                break;
            };
            let refs = list_chunk_refs(database, transaction, document_id, ordinal)?;
            chunks.push(chunk_json(&chunk, &refs));
            if chunks.len() as u64 >= limit {
                break;
            }
        }
    }
    let total = record.chunk_count;
    let response = json!({
        "document": metadata_json(&record, &counts),
        "chunks": chunks,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total_items": total,
            "has_more": offset + chunks_len(&chunks) < total,
        },
    });
    encode_response(&response).map(Some)
}

fn chunks_len(chunks: &[Value]) -> u64 {
    chunks.len() as u64
}

fn document_patch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
    scope: Option<&str>,
    media_metadata_json: Option<&str>,
    now_unix_ms: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some(mut record) = read_document_record(database, transaction, document_id)? else {
        return Ok(None);
    };
    let now = seconds(now_unix_ms);
    let mut assets = list_document_assets(database, transaction, document_id)?;
    let contribution_before = contribution(&record.scope, &assets);

    if let Some(scope) = scope {
        record.scope = scope.to_owned();
    }
    if let Some(metadata) = media_metadata_json {
        record.media_metadata_json = metadata.to_owned();
    }
    record.updated_at = now;

    if matches!(scope, Some("library" | "shared")) {
        let settings = read_settings(database, transaction)?;
        match settings {
            None => {
                write_settings(
                    database,
                    transaction,
                    &SettingsRecord {
                        enabled: true,
                        visual_enrichment: false,
                        updated_at: now,
                    },
                )?;
            }
            Some(settings) if settings.visual_enrichment => {
                for asset in &mut assets {
                    if matches!(
                        asset.enrichment_status.as_str(),
                        "not_requested" | "no_vision" | "failed"
                    ) {
                        asset.enrichment_status = "pending".to_owned();
                        asset.enrichment_error = String::new();
                        asset.updated_at = now;
                        write_asset_record(database, transaction, asset, now_unix_ms)?;
                        queue_insert(database, transaction, asset)?;
                    }
                }
            }
            _ => {}
        }
    } else if scope == Some("attachment") {
        for asset in &mut assets {
            if asset.enrichment_status == "pending" {
                queue_delete(database, transaction, asset)?;
                asset.enrichment_status = "not_requested".to_owned();
                asset.updated_at = now;
                write_asset_record(database, transaction, asset, now_unix_ms)?;
            }
        }
    }

    write_document_record(database, transaction, &record, now_unix_ms)?;
    let contribution_after = contribution(&record.scope, &assets);
    adjust_owner_index(
        database,
        transaction,
        contribution_after - contribution_before,
    )?;
    load_metadata(database, transaction, document_id)?
        .map(|metadata| encode_response(&metadata))
        .transpose()
}

fn contribution(scope: &str, assets: &[AssetRecord]) -> i64 {
    if !is_scoped(scope) {
        return 0;
    }
    assets
        .iter()
        .filter(|asset| in_queue_class(&asset.enrichment_status))
        .count() as i64
}

fn document_find_digest(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &str,
) -> io::Result<Option<Vec<u8>>> {
    let key = owner_key(
        transaction,
        KNOWLEDGE_DIGEST_INDEX_NAMESPACE,
        digest.as_bytes(),
    )?;
    let Some(raw) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let entries = decode_digest_index(&raw)?;
    let Some((_, document_id)) = entries.first() else {
        return Ok(None);
    };
    load_metadata(database, transaction, document_id)?
        .map(|metadata| encode_response(&metadata))
        .transpose()
}

fn document_create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &ValidatedDocument,
    now_unix_ms: u64,
) -> io::Result<Vec<u8>> {
    let digest_key = owner_key(
        transaction,
        KNOWLEDGE_DIGEST_INDEX_NAMESPACE,
        document.sha256.as_bytes(),
    )?;
    if let Some(raw) = database.entity_get(transaction, &digest_key)? {
        let entries = decode_digest_index(&raw)?;
        if let Some((_, existing_id)) = entries.first() {
            let metadata = load_metadata(database, transaction, existing_id)?.ok_or_else(|| {
                invalid_data("knowledge digest index references a missing document")
            })?;
            return encode_response(&json!({
                "created": false,
                "document": metadata,
            }));
        }
    }
    if read_document_record(database, transaction, &document.id)?.is_some() {
        return Err(conflict(
            "knowledge document id already exists with a different digest",
        ));
    }
    let sequence = next_document_sequence(database, transaction)?;
    insert_document(database, transaction, document, sequence, now_unix_ms)?;
    if document.scope == "library" && read_settings(database, transaction)?.is_none() {
        write_settings(
            database,
            transaction,
            &SettingsRecord {
                enabled: true,
                visual_enrichment: false,
                updated_at: seconds(now_unix_ms),
            },
        )?;
    }
    let metadata = load_metadata(database, transaction, &document.id)?
        .ok_or_else(|| invalid_data("knowledge document disappeared after create"))?;
    encode_response(&json!({
        "created": true,
        "document": metadata,
    }))
}

fn document_replace(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &ValidatedDocument,
    now_unix_ms: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some(current) = read_document_record(database, transaction, &document.id)? else {
        return Ok(None);
    };
    let old_assets = list_document_assets(database, transaction, &document.id)?;
    let old_names = old_assets
        .iter()
        .map(|asset| asset.stored_name.clone())
        .collect::<Vec<_>>();
    // The cascade subtracts the old contribution and the insert adds the new
    // one, so the owner index sees exactly the net replace delta.
    cascade_delete_document(database, transaction, &current, &old_assets)?;
    insert_document(
        database,
        transaction,
        document,
        current.sequence,
        now_unix_ms,
    )?;
    let metadata = load_metadata(database, transaction, &document.id)?
        .ok_or_else(|| invalid_data("knowledge document disappeared after replace"))?;
    let mut response = metadata;
    let object = response
        .as_object_mut()
        .ok_or_else(|| invalid_data("knowledge document projection is malformed"))?;
    object.insert(
        "_replaced_asset_names".to_owned(),
        Value::Array(old_names.into_iter().map(Value::String).collect()),
    );
    encode_response(&response).map(Some)
}

fn document_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_id: &str,
) -> io::Result<Vec<u8>> {
    let Some(record) = read_document_record(database, transaction, document_id)? else {
        return encode_response(&json!({
            "deleted": false,
            "document": Value::Null,
        }));
    };
    let counts = document_asset_counts(database, transaction, document_id)?;
    let assets = list_document_assets(database, transaction, document_id)?;
    let mut document = metadata_json(&record, &counts);
    let object = document
        .as_object_mut()
        .ok_or_else(|| invalid_data("knowledge document projection is malformed"))?;
    object.insert(
        "assets".to_owned(),
        Value::Array(
            assets
                .iter()
                .map(|asset| json!({ "stored_name": asset.stored_name }))
                .collect(),
        ),
    );
    cascade_delete_document(database, transaction, &record, &assets)?;
    encode_response(&json!({
        "deleted": true,
        "document": document,
    }))
}

fn settings_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let settings = read_settings(database, transaction)?;
    encode_response(&settings_json(settings.as_ref()))
}

fn settings_patch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    enabled: Option<bool>,
    visual_enrichment: Option<bool>,
    now_unix_ms: u64,
) -> io::Result<Vec<u8>> {
    let now = seconds(now_unix_ms);
    let current = read_settings(database, transaction)?.unwrap_or(SettingsRecord {
        enabled: false,
        visual_enrichment: false,
        updated_at: now,
    });
    let next = SettingsRecord {
        enabled: enabled.unwrap_or(current.enabled),
        visual_enrichment: visual_enrichment.unwrap_or(current.visual_enrichment),
        updated_at: now,
    };
    write_settings(database, transaction, &next)?;
    if visual_enrichment == Some(true) {
        let scoped = scoped_document_ids(database, transaction)?;
        for mut asset in scan_owner_assets(database, transaction)? {
            if scoped.contains(&asset.document_id)
                && matches!(
                    asset.enrichment_status.as_str(),
                    "not_requested" | "no_vision" | "failed"
                )
            {
                asset.enrichment_status = "pending".to_owned();
                asset.enrichment_error = String::new();
                asset.updated_at = now;
                write_asset_record(database, transaction, &asset, now_unix_ms)?;
                queue_insert(database, transaction, &asset)?;
            }
        }
        resync_owner_index(database, transaction)?;
    } else if visual_enrichment == Some(false) {
        let scoped = scoped_document_ids(database, transaction)?;
        for mut asset in scan_owner_assets(database, transaction)? {
            if scoped.contains(&asset.document_id) && asset.enrichment_status == "pending" {
                queue_delete(database, transaction, &asset)?;
                asset.enrichment_status = "not_requested".to_owned();
                asset.updated_at = now;
                write_asset_record(database, transaction, &asset, now_unix_ms)?;
            }
        }
        database.entity_delete(transaction, owner_index_entity_key(transaction)?)?;
    }
    encode_response(&json!({
        "enabled": next.enabled,
        "visual_enrichment": next.visual_enrichment,
    }))
}

fn availability(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let enabled = read_settings(database, transaction)?
        .map(|settings| settings.enabled)
        .unwrap_or(false);
    let mut available = false;
    if enabled {
        let records = list_document_records(database, transaction)?;
        available = records.iter().any(|record| is_scoped(&record.scope));
    }
    encode_response(&json!({ "available": available }))
}

fn asset_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    asset_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(asset) = read_asset_record(database, transaction, asset_id)? else {
        return Ok(None);
    };
    let Some(document) = read_document_record(database, transaction, &asset.document_id)? else {
        return Err(invalid_data(
            "knowledge asset references a missing document",
        ));
    };
    encode_response(&asset_projection_json(&asset, &document.name)).map(Some)
}

fn enrichment_activity(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let scoped = scoped_document_ids(database, transaction)?;
    let mut pending_assets = 0_u64;
    let mut asset_issues = 0_u64;
    for asset in scan_owner_assets(database, transaction)? {
        if !scoped.contains(&asset.document_id) {
            continue;
        }
        if in_queue_class(&asset.enrichment_status) {
            pending_assets += 1;
        }
        if in_issue_class(&asset.enrichment_status) {
            asset_issues += 1;
        }
    }
    let settings = read_settings(database, transaction)?;
    encode_response(&json!({
        "pending_assets": pending_assets,
        "asset_issues": asset_issues,
        "visual_enrichment": settings.is_some_and(|settings| settings.visual_enrichment),
    }))
}

fn enrichment_owners(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    limit: u64,
) -> io::Result<Vec<u8>> {
    let rows = scan_tenant_global_prefix(
        database,
        transaction,
        KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
        b"",
        MAX_KNOWLEDGE_CATALOG_SCAN_DOCUMENTS,
    )?;
    let mut owners = Vec::new();
    for (_, value) in rows {
        if value.len() != 16 {
            return Err(invalid_data(
                "knowledge enrichment owner index is malformed",
            ));
        }
        let count = decode_owner_index(&value)?;
        if count == 0 {
            continue;
        }
        let owner = u64::from_be_bytes(value[0..8].try_into().expect("length checked"));
        owners.push(owner);
        if owners.len() as u64 >= limit {
            break;
        }
    }
    encode_response(&Value::Array(owners.into_iter().map(Value::from).collect()))
}

fn asset_claim(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    now_unix_ms: u64,
) -> io::Result<Option<Vec<u8>>> {
    let now = seconds(now_unix_ms);
    let stale_before = now - CLAIM_STALE_SECONDS;
    let entries = scan_prefix(
        database,
        transaction,
        KNOWLEDGE_ENRICHMENT_QUEUE_NAMESPACE,
        b"",
        MAX_KNOWLEDGE_CLAIM_SCAN_ROWS,
    )?;
    for (_, asset_id) in entries {
        let asset_id = String::from_utf8(asset_id)
            .map_err(|_| invalid_data("knowledge enrichment queue value is not UTF-8"))?;
        let Some(mut asset) = read_asset_record(database, transaction, &asset_id)? else {
            return Err(invalid_data(
                "knowledge enrichment queue references a missing asset",
            ));
        };
        let eligible = asset.enrichment_status == "pending"
            || (asset.enrichment_status == "running" && asset.updated_at < stale_before);
        if !eligible {
            continue;
        }
        let Some(document) = read_document_record(database, transaction, &asset.document_id)?
        else {
            return Err(invalid_data(
                "knowledge asset references a missing document",
            ));
        };
        if !is_scoped(&document.scope) {
            continue;
        }
        asset.enrichment_status = "running".to_owned();
        asset.enrichment_error = String::new();
        asset.updated_at = now;
        write_asset_record(database, transaction, &asset, now_unix_ms)?;
        return encode_response(&asset_projection_json(&asset, &document.name)).map(Some);
    }
    Ok(None)
}

fn asset_update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    asset_id: &str,
    updates: &[(String, String)],
    chunk_content: Option<&Value>,
    chunk_search_text: Option<&Value>,
    now_unix_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some(mut asset) = read_asset_record(database, transaction, asset_id)? else {
        return encode_response(&json!({ "updated": false }));
    };
    let Some(document) = read_document_record(database, transaction, &asset.document_id)? else {
        return Err(invalid_data(
            "knowledge asset references a missing document",
        ));
    };
    let now = seconds(now_unix_ms);
    let was_in_queue = in_queue_class(&asset.enrichment_status);
    for (field, value) in updates {
        match field.as_str() {
            "caption" => asset.caption = value.clone(),
            "ocr_text" => asset.ocr_text = value.clone(),
            "description" => asset.description = value.clone(),
            "enrichment_status" => asset.enrichment_status = value.clone(),
            "enrichment_model" => asset.enrichment_model = value.clone(),
            "enrichment_error" => asset.enrichment_error = value.clone(),
            _ => return Err(invalid_input("knowledge asset update field is immutable")),
        }
    }
    asset.updated_at = now;
    let is_in_queue = in_queue_class(&asset.enrichment_status);
    match (was_in_queue, is_in_queue) {
        (true, false) => queue_delete(database, transaction, &asset)?,
        (false, true) => queue_insert(database, transaction, &asset)?,
        _ => {}
    }
    write_asset_record(database, transaction, &asset, now_unix_ms)?;
    if is_scoped(&document.scope) {
        adjust_owner_index(
            database,
            transaction,
            is_in_queue as i64 - was_in_queue as i64,
        )?;
    }

    let content = match chunk_content {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        Some(_) => {
            return Err(invalid_input(
                "knowledge enriched chunk content is not text",
            ));
        }
    };
    let new_search_text = match chunk_search_text {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        Some(_) => {
            return Err(invalid_input(
                "knowledge enriched chunk search text is not text",
            ));
        }
    };
    if content.is_some() || new_search_text.is_some() {
        // Legacy rewrites every chunk linked to the asset inside its own
        // document; the term postings are rebuilt only when search_text is
        // supplied.
        let reverse_prefix = asset_raw(asset_id)?;
        let reverse_rows = scan_prefix(
            database,
            transaction,
            KNOWLEDGE_ASSET_CHUNK_REVERSE_NAMESPACE,
            &reverse_prefix,
            MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN,
        )?;
        for (key, _) in reverse_rows {
            let (chunk_document_id, chunk_ordinal) = parse_reverse_key(key.key_bytes(), asset_id)?;
            if chunk_document_id != asset.document_id {
                return Err(invalid_data(
                    "knowledge reverse link references a foreign document",
                ));
            }
            let Some(mut chunk) =
                read_chunk_record(database, transaction, &chunk_document_id, chunk_ordinal)?
            else {
                return Err(invalid_data(
                    "knowledge reverse link references a missing chunk",
                ));
            };
            if let Some(text) = &content {
                chunk.content = text.clone();
            }
            if let Some(text) = &new_search_text {
                chunk.search_text = text.clone();
                let terms = search_terms(&chunk.search_text)?;
                for term in &chunk.terms {
                    database.entity_delete(
                        transaction,
                        owner_key(
                            transaction,
                            KNOWLEDGE_TERM_NAMESPACE,
                            &term_raw(term, &chunk_document_id, chunk_ordinal)?,
                        )?,
                    )?;
                }
                for term in &terms {
                    let mut value = Vec::with_capacity(chunk_document_id.len() + 6);
                    push_text(&mut value, &chunk_document_id)?;
                    value.extend_from_slice(&(chunk_ordinal as u32).to_be_bytes());
                    database.entity_put(
                        transaction,
                        owner_key(
                            transaction,
                            KNOWLEDGE_TERM_NAMESPACE,
                            &term_raw(term, &chunk_document_id, chunk_ordinal)?,
                        )?,
                        value,
                    )?;
                }
                chunk.terms = terms;
            }
            write_chunk_record(
                database,
                transaction,
                &chunk_document_id,
                &chunk,
                now_unix_ms,
            )?;
        }
    }
    encode_response(&json!({
        "updated": true,
        "asset": asset_projection_json(&asset, &document.name),
    }))
}

fn parse_reverse_key(key_bytes: &[u8], asset_id: &str) -> io::Result<(String, u64)> {
    let prefix_length = 2 + asset_id.len();
    if key_bytes.len() < prefix_length {
        return Err(invalid_data("knowledge reverse link key is truncated"));
    }
    let (document_id, consumed) = read_text(&key_bytes[prefix_length..])?;
    let rest = &key_bytes[prefix_length + consumed..];
    if rest.len() != 4 {
        return Err(invalid_data("knowledge reverse link key is truncated"));
    }
    Ok((
        document_id,
        u32::from_be_bytes(rest.try_into().expect("length checked")) as u64,
    ))
}

fn assets_mark_no_vision(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    now_unix_ms: u64,
) -> io::Result<Vec<u8>> {
    let now = seconds(now_unix_ms);
    let scoped = scoped_document_ids(database, transaction)?;
    let mut changed = 0_i64;
    for mut asset in scan_owner_assets(database, transaction)? {
        if scoped.contains(&asset.document_id) && asset.enrichment_status == "pending" {
            queue_delete(database, transaction, &asset)?;
            asset.enrichment_status = "no_vision".to_owned();
            asset.enrichment_error = "No configured vision model".to_owned();
            asset.updated_at = now;
            write_asset_record(database, transaction, &asset, now_unix_ms)?;
            changed += 1;
        }
    }
    adjust_owner_index(database, transaction, -changed)?;
    encode_response(&json!({ "changed": changed }))
}

fn catalog_name_matches(name: &str, folded_needle: &[u32]) -> bool {
    // Legacy: LOWER(d.name) LIKE '%<casefold(query)>%' ESCAPE '!' under
    // ENABLE_ICU — ASCII-only LOWER, then per-codepoint simple folding of
    // both LIKE operands.
    if folded_needle.is_empty() {
        return true;
    }
    let folded: Vec<u32> = name
        .chars()
        .map(|character| {
            crate::generated_unicode_simple_fold::simple_case_fold(
                character.to_ascii_lowercase() as u32
            )
        })
        .collect();
    folded_needle.len() <= folded.len()
        && folded
            .windows(folded_needle.len())
            .any(|window| window == folded_needle)
}

fn catalog(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    page: u64,
    page_size: u64,
    query: &str,
    category: &str,
    sort: &str,
) -> io::Result<Vec<u8>> {
    let records = list_document_records(database, transaction)?;
    let owner_assets = scan_owner_assets(database, transaction)?;
    let counts = counts_by_document(&owner_assets);
    let scoped: Vec<&DocumentRecord> = records
        .iter()
        .filter(|record| is_scoped(&record.scope))
        .collect();

    let folded_needle: Vec<u32> = python_casefold(query)
        .chars()
        .map(|character| crate::generated_unicode_simple_fold::simple_case_fold(character as u32))
        .collect();
    let mut filtered: Vec<&DocumentRecord> = scoped
        .iter()
        .copied()
        .filter(|record| catalog_name_matches(&record.name, &folded_needle))
        .filter(|record| category == "all" || category_of(&record.kind) == category)
        .collect();
    let total_items = filtered.len() as u64;
    let total_pages = std::cmp::max(1, total_items.saturating_add(page_size - 1) / page_size);
    let bounded_page = page.min(total_pages);
    let offset = (bounded_page - 1).saturating_mul(page_size);
    match sort {
        "created_desc" => filtered.sort_by(|left, right| {
            right
                .created_at
                .partial_cmp(&left.created_at)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| right.id.cmp(&left.id))
        }),
        "name_asc" => filtered.sort_by(|left, right| {
            left.name
                .to_ascii_lowercase()
                .cmp(&right.name.to_ascii_lowercase())
                .then_with(|| left.id.cmp(&right.id))
        }),
        "size_desc" => filtered.sort_by(|left, right| {
            right
                .size_bytes
                .cmp(&left.size_bytes)
                .then_with(|| right.id.cmp(&left.id))
        }),
        _ => filtered.sort_by(|left, right| {
            right
                .updated_at
                .partial_cmp(&left.updated_at)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| right.id.cmp(&left.id))
        }),
    }
    let empty = AssetCounts {
        total: 0,
        pending: 0,
        issues: 0,
    };
    let documents = filtered
        .iter()
        .skip(offset as usize)
        .take(page_size as usize)
        .map(|record| {
            let mut row = metadata_json(record, counts.get(&record.id).unwrap_or(&empty));
            let object = row
                .as_object_mut()
                .expect("metadata projection is an object");
            object.insert(
                "category".to_owned(),
                Value::String(category_of(&record.kind).to_owned()),
            );
            row
        })
        .collect::<Vec<_>>();

    let mut totals = json!({
        "documents": scoped.len() as u64,
        "chunks": 0_u64,
        "assets": 0_u64,
        "pending_assets": 0_u64,
        "asset_issues": 0_u64,
        "text_chars": 0_u64,
        "size_bytes": 0_u64,
    });
    let totals_object = totals
        .as_object_mut()
        .expect("totals projection is an object");
    let mut chunks_total = 0_u64;
    let mut text_chars_total = 0_u64;
    let mut size_bytes_total = 0_u64;
    for record in &scoped {
        chunks_total = chunks_total.saturating_add(record.chunk_count);
        text_chars_total = text_chars_total.saturating_add(record.text_chars);
        size_bytes_total = size_bytes_total.saturating_add(record.size_bytes);
    }
    totals_object.insert("chunks".to_owned(), Value::from(chunks_total));
    totals_object.insert("text_chars".to_owned(), Value::from(text_chars_total));
    totals_object.insert("size_bytes".to_owned(), Value::from(size_bytes_total));
    let scoped_ids: HashSet<&str> = scoped.iter().map(|record| record.id.as_str()).collect();
    let mut assets_total = 0_u64;
    let mut pending_total = 0_u64;
    let mut issues_total = 0_u64;
    for asset in &owner_assets {
        if !scoped_ids.contains(asset.document_id.as_str()) {
            continue;
        }
        assets_total += 1;
        if in_queue_class(&asset.enrichment_status) {
            pending_total += 1;
        }
        if in_issue_class(&asset.enrichment_status) {
            issues_total += 1;
        }
    }
    totals_object.insert("assets".to_owned(), Value::from(assets_total));
    totals_object.insert("pending_assets".to_owned(), Value::from(pending_total));
    totals_object.insert("asset_issues".to_owned(), Value::from(issues_total));

    let mut facet_counts: HashMap<&'static str, u64> = HashMap::new();
    for record in &scoped {
        *facet_counts.entry(category_of(&record.kind)).or_insert(0) += 1;
    }
    let mut facets = facet_counts.into_iter().collect::<Vec<_>>();
    facets.sort_by(|left, right| right.1.cmp(&left.1).then_with(|| left.0.cmp(right.0)));
    let facets = facets
        .into_iter()
        .map(|(facet_category, count)| json!({ "category": facet_category, "count": count }))
        .collect::<Vec<_>>();

    let settings = read_settings(database, transaction)?;
    let enabled = settings.is_some_and(|settings| settings.enabled);
    let available = enabled && scoped.len() as u64 > 0;
    encode_response(&json!({
        "enabled": enabled,
        "visual_enrichment": settings.is_some_and(|settings| settings.visual_enrichment),
        "available": available,
        "documents": documents,
        "totals": totals,
        "facets": facets,
        "pagination": {
            "page": bounded_page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_previous": bounded_page > 1,
            "has_next": bounded_page < total_pages,
        },
        "filters": {
            "query": query,
            "category": category,
            "sort": sort,
        },
    }))
}
fn search_candidates(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tokens: &[String],
    document_id: &str,
    limit: u64,
) -> io::Result<Vec<u8>> {
    if tokens.is_empty() {
        return encode_response(&Value::Array(Vec::new()));
    }
    let mut matched: HashMap<(String, u64), u64> = HashMap::new();
    let mut document_cache: HashMap<String, Option<DocumentRecord>> = HashMap::new();
    let mut scanned = 0_usize;
    for token in tokens {
        let mut prefix = Vec::with_capacity(token.len() + 2);
        push_text(&mut prefix, token)?;
        let remaining = MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN
            .checked_sub(scanned)
            .ok_or_else(|| resource_exhausted("knowledge search exceeds its posting scan bound"))?;
        let rows = scan_prefix(
            database,
            transaction,
            KNOWLEDGE_TERM_NAMESPACE,
            &prefix,
            remaining,
        )?;
        scanned = scanned.saturating_add(rows.len());
        for (_, value) in rows {
            let (candidate_document_id, consumed) = read_text(&value)?;
            if value.len() != consumed + 4 {
                return Err(invalid_data("knowledge search posting is malformed"));
            }
            let chunk_ordinal =
                u32::from_be_bytes(value[consumed..].try_into().expect("length checked")) as u64;
            if !document_id.is_empty() {
                if candidate_document_id != document_id {
                    continue;
                }
            } else {
                let document = match document_cache.get(&candidate_document_id) {
                    Some(cached) => cached,
                    None => {
                        document_cache.insert(
                            candidate_document_id.clone(),
                            read_document_record(database, transaction, &candidate_document_id)?,
                        );
                        document_cache
                            .get(&candidate_document_id)
                            .expect("cache entry just inserted")
                    }
                };
                let Some(document) = document else {
                    continue;
                };
                if !is_scoped(&document.scope) {
                    continue;
                }
            }
            *matched
                .entry((candidate_document_id, chunk_ordinal))
                .or_insert(0) += 1;
        }
    }
    if matched.is_empty() {
        return encode_response(&Value::Array(Vec::new()));
    }
    // Candidate ranking: matched_terms DESC, document updated_at DESC,
    // document_id ASC, chunk_ordinal ASC — the legacy GROUP BY ORDER BY.
    let mut candidates = Vec::with_capacity(matched.len());
    for ((candidate_document_id, chunk_ordinal), matched_terms) in matched {
        let document = match document_cache.get(&candidate_document_id) {
            Some(cached) => cached.clone(),
            None => read_document_record(database, transaction, &candidate_document_id)?,
        };
        let Some(document) = document else {
            continue;
        };
        candidates.push((document, chunk_ordinal, matched_terms));
    }
    candidates.sort_by(|left, right| {
        right
            .2
            .cmp(&left.2)
            .then_with(|| {
                right
                    .0
                    .updated_at
                    .partial_cmp(&left.0.updated_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then_with(|| left.0.id.cmp(&right.0.id))
            .then_with(|| left.1.cmp(&right.1))
    });
    candidates.truncate(limit as usize);

    let mut rows = Vec::with_capacity(candidates.len());
    for (document, chunk_ordinal, matched_terms) in candidates {
        let Some(chunk) = read_chunk_record(database, transaction, &document.id, chunk_ordinal)?
        else {
            return Err(invalid_data("knowledge search candidate chunk is missing"));
        };
        let next = read_chunk_record(database, transaction, &document.id, chunk_ordinal + 1)?;
        let refs = list_chunk_refs(database, transaction, &document.id, chunk_ordinal)?;
        let mut assets = Vec::with_capacity(refs.len());
        for reference in refs {
            // Legacy joins the link to the asset row; a link whose asset is
            // gone contributes no row.
            if let Some(asset) = read_asset_record(database, transaction, &reference.id)? {
                assets.push(asset_json(&asset));
            }
        }
        rows.push(json!({
            "document_id": document.id,
            "name": document.name,
            "kind": document.kind,
            "ordinal": chunk_ordinal,
            "section": chunk.section,
            "location": chunk.location,
            "content": chunk.content,
            "next_content": next.as_ref().map_or("", |chunk| chunk.content.as_str()),
            "next_section": next.as_ref().map_or("", |chunk| chunk.section.as_str()),
            "assets": assets,
            "matched_terms": matched_terms,
            "bm25_score": 0,
        }));
    }
    encode_response(&Value::Array(rows))
}

fn owner_clear(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let records = list_document_records(database, transaction)?;
    let deleted_documents = records.len() as u64;
    for record in &records {
        let assets = list_document_assets(database, transaction, &record.id)?;
        cascade_delete_document(database, transaction, record, &assets)?;
    }
    database.entity_delete(
        transaction,
        owner_key(transaction, KNOWLEDGE_SETTINGS_NAMESPACE, SETTINGS_KEY)?,
    )?;
    database.entity_delete(
        transaction,
        owner_key(
            transaction,
            KNOWLEDGE_SETTINGS_NAMESPACE,
            DOCUMENT_SEQUENCE_KEY,
        )?,
    )?;
    database.entity_delete(transaction, owner_index_entity_key(transaction)?)?;
    encode_response(&json!({ "deleted_documents": deleted_documents }))
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &KnowledgeRequest,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        KnowledgeRequest::DocumentList => document_list(database, transaction).map(Some),
        KnowledgeRequest::DocumentGet { document_id } => {
            document_get(database, transaction, document_id)
        }
        KnowledgeRequest::DocumentMetadata { document_id } => {
            document_metadata(database, transaction, document_id)
        }
        KnowledgeRequest::DocumentAssets {
            document_id,
            offset,
            limit,
        } => document_assets(database, transaction, document_id, *offset, *limit),
        KnowledgeRequest::DocumentContent {
            document_id,
            offset,
            limit,
        } => document_content(database, transaction, document_id, *offset, *limit),
        KnowledgeRequest::DocumentPatch {
            document_id,
            scope,
            media_metadata_json,
            now_unix_ms,
        } => document_patch(
            database,
            transaction,
            document_id,
            scope.as_deref(),
            media_metadata_json.as_deref(),
            *now_unix_ms,
        ),
        KnowledgeRequest::DocumentFindDigest { sha256 } => {
            document_find_digest(database, transaction, sha256)
        }
        KnowledgeRequest::DocumentCreate {
            document,
            now_unix_ms,
        } => document_create(database, transaction, document, *now_unix_ms).map(Some),
        KnowledgeRequest::DocumentReplace {
            document,
            now_unix_ms,
        } => document_replace(database, transaction, document, *now_unix_ms),
        KnowledgeRequest::DocumentDelete { document_id } => {
            document_delete(database, transaction, document_id).map(Some)
        }
        KnowledgeRequest::SettingsGet => settings_get(database, transaction).map(Some),
        KnowledgeRequest::SettingsPatch {
            enabled,
            visual_enrichment,
            now_unix_ms,
        } => settings_patch(
            database,
            transaction,
            *enabled,
            *visual_enrichment,
            *now_unix_ms,
        )
        .map(Some),
        KnowledgeRequest::Availability => availability(database, transaction).map(Some),
        KnowledgeRequest::AssetGet { asset_id } => asset_get(database, transaction, asset_id),
        KnowledgeRequest::EnrichmentActivity => {
            enrichment_activity(database, transaction).map(Some)
        }
        KnowledgeRequest::EnrichmentOwners { limit } => {
            enrichment_owners(database, transaction, *limit).map(Some)
        }
        KnowledgeRequest::AssetClaim { now_unix_ms } => {
            asset_claim(database, transaction, *now_unix_ms)
        }
        KnowledgeRequest::AssetUpdate {
            asset_id,
            updates,
            chunk_content,
            chunk_search_text,
            now_unix_ms,
        } => asset_update(
            database,
            transaction,
            asset_id,
            updates,
            chunk_content.as_ref(),
            chunk_search_text.as_ref(),
            *now_unix_ms,
        )
        .map(Some),
        KnowledgeRequest::AssetsMarkNoVision { now_unix_ms } => {
            assets_mark_no_vision(database, transaction, *now_unix_ms).map(Some)
        }
        KnowledgeRequest::Catalog {
            page,
            page_size,
            query,
            category,
            sort,
        } => catalog(
            database,
            transaction,
            *page,
            *page_size,
            query,
            category,
            sort,
        )
        .map(Some),
        KnowledgeRequest::SearchCandidates {
            tokens,
            document_id,
            limit,
        } => search_candidates(database, transaction, tokens, document_id, *limit).map(Some),
        KnowledgeRequest::OwnerClear => owner_clear(database, transaction).map(Some),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn valid_document() -> Value {
        json!({
            "id": "doc-1",
            "sha256": "a".repeat(64),
            "name": "Spec.PDF",
            "stored_name": "spec.pdf",
            "kind": ".PDF",
            "size_bytes": 12,
            "method": "text",
            "warnings_json": "[]",
            "text_chars": 11,
            "chunk_count": 2,
            "pages": 1,
            "scope": "library",
            "media_metadata_json": "{}",
            "created_at": 10.0,
            "updated_at": 11.0,
            "chunks": [
                {
                    "ordinal": 0,
                    "section": "Intro",
                    "location": "page 1",
                    "content": "Hello World",
                    "search_text": "Hello WORLD hello  SIGMA \u{1c}sigma",
                    "assets": [{"id": "asset-1", "relation": "evidence"}],
                },
                {
                    "ordinal": 1,
                    "section": "Body",
                    "location": "page 1",
                    "content": "Second",
                    "search_text": "second",
                    "assets": [],
                },
            ],
            "assets": [
                {
                    "id": "asset-1",
                    "ordinal": 0,
                    "kind": "image",
                    "stored_name": "a.png",
                    "mime_type": "image/png",
                    "sha256": "b".repeat(64),
                    "size_bytes": 5,
                    "width": 1,
                    "height": 1,
                    "page": 0,
                    "pages_json": "[]",
                    "bbox_json": "[]",
                    "caption": "",
                    "ocr_text": "",
                    "description": "",
                    "enrichment_status": "pending",
                    "enrichment_model": "",
                    "enrichment_error": "",
                    "metadata_json": "{}",
                    "created_at": 9.0,
                    "updated_at": 9.0,
                },
            ],
        })
    }

    #[test]
    fn validate_document_accepts_the_legacy_shape_and_derives_terms() {
        let document = validate_document(&valid_document()).unwrap();
        assert_eq!(document.id, "doc-1");
        assert_eq!(document.kind, ".PDF");
        assert_eq!(document.chunks.len(), 2);
        assert_eq!(document.assets.len(), 1);
        // Python whitespace split (incl. U+001C), full casefold, dedupe
        // preserving first occurrence.
        assert_eq!(
            document.chunks[0].terms,
            vec!["hello".to_owned(), "world".to_owned(), "sigma".to_owned()]
        );
        assert_eq!(document.chunks[0].refs.len(), 1);
        assert_eq!(document.chunks[0].refs[0].relation, "evidence");
    }

    #[test]
    fn validate_document_rejects_the_legacy_protocol_errors() {
        let mut bad = valid_document();
        bad["sha256"] = json!("ABCDEF");
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["stored_name"] = json!("../escape");
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["chunks"][0]["ordinal"] = json!(7);
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["chunk_count"] = json!(9);
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["scope"] = json!("everywhere");
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["assets"][0]["enrichment_status"] = json!("exploded");
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["chunks"][0]["assets"] = json!([{"id": "ghost", "relation": "evidence"}]);
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["chunks"][0]["assets"] = json!([
            {"id": "asset-1", "relation": "evidence"},
            {"id": "asset-1", "relation": "evidence"},
        ]);
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["warnings_json"] = json!("{}");
        assert!(validate_document(&bad).is_err());

        let mut bad = valid_document();
        bad["media_metadata_json"] = json!("[]");
        assert!(validate_document(&bad).is_err());
    }

    #[test]
    fn category_mapping_matches_the_legacy_case_expression() {
        assert_eq!(category_of(".PDF"), "pdf");
        assert_eq!(category_of(".docx"), "document");
        assert_eq!(category_of(".CSV"), "spreadsheet");
        assert_eq!(category_of(".ppt"), "presentation");
        assert_eq!(category_of(".JPEG"), "image");
        assert_eq!(category_of(".eml"), "email");
        assert_eq!(category_of(".epub"), "ebook");
        assert_eq!(category_of(".md"), "text");
        assert_eq!(category_of(".bin"), "other");
    }

    #[test]
    fn queue_key_order_matches_the_legacy_claim_order_by() {
        // (kind rank, created_at, document_id, ordinal) ascending.
        let image_late = queue_raw("image", 20.0, "doc", 0).unwrap();
        let figure_early = queue_raw("figure", 1.0, "doc", 0).unwrap();
        assert!(image_late < figure_early);
        let figure_a = queue_raw("figure", 1.0, "a", 0).unwrap();
        let figure_b = queue_raw("figure", 1.0, "b", 0).unwrap();
        assert!(figure_a < figure_b);
        let figure_ord = queue_raw("figure", 1.0, "a", 1).unwrap();
        assert!(figure_a < figure_ord);
        let table = queue_raw("table", 1.0, "a", 0).unwrap();
        assert!(figure_a < table);
        let other = queue_raw("attachment", 1.0, "a", 0).unwrap();
        assert!(table < other);
    }

    #[test]
    fn digest_index_keeps_creation_order() {
        let mut entries = vec![(7_u64, "older".to_owned())];
        let position = entries
            .binary_search_by_key(&9_u64, |(sequence, _)| *sequence)
            .unwrap_or_else(|position| position);
        entries.insert(position, (9, "newer".to_owned()));
        let raw = encode_digest_index(&entries).unwrap();
        let decoded = decode_digest_index(&raw).unwrap();
        assert_eq!(decoded[0].1, "older");
        assert_eq!(decoded[1].1, "newer");
    }

    #[test]
    fn catalog_name_matching_reproduces_icu_like_on_lowered_names() {
        let needle = |query: &str| {
            python_casefold(query)
                .chars()
                .map(|character| {
                    crate::generated_unicode_simple_fold::simple_case_fold(character as u32)
                })
                .collect::<Vec<_>>()
        };
        assert!(catalog_name_matches("Spec.PDF", &needle("spec")));
        // LOWER is ASCII-only; LIKE's ICU fold still matches Greek.
        assert!(catalog_name_matches("\u{391}\u{3b2}", &needle("\u{3b1}")));
        // Full casefold expands sharp-s in the needle but the simple-fold
        // text side never does, mirroring the legacy ICU LIKE quirk.
        assert!(!catalog_name_matches("stra\u{df}e", &needle("strasse")));
        assert!(!catalog_name_matches("abc", &needle("abcd")));
    }

    #[test]
    fn search_request_validation_bounds_tokens_and_limits() {
        let request = KnowledgeRequest::SearchCandidates {
            tokens: vec!["alpha".to_owned()],
            document_id: String::new(),
            limit: 80,
        };
        assert!(request.validate().is_ok());
        let request = KnowledgeRequest::SearchCandidates {
            tokens: vec!["a".repeat(200)],
            document_id: String::new(),
            limit: 80,
        };
        assert!(request.validate().is_err());
        let request = KnowledgeRequest::SearchCandidates {
            tokens: vec!["alpha".to_owned()],
            document_id: String::new(),
            limit: 0,
        };
        assert!(request.validate().is_err());
    }
}
