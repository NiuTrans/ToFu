//! Bounded owner-scoped paper library authority.
//!
//! Large parsed text and auxiliary JSON live in one immutable-blob-capable
//! core document. Frequently rewritten metadata lives in a separate compact
//! state document, so title recovery and index maintenance never copy paper
//! bodies. Compact chronological/hash/arXiv indexes bound every query before
//! body hydration.

use std::collections::BTreeSet;
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_PAPER_LIBRARY_CORE_DOCUMENT_BYTES, MAX_PAPER_LIBRARY_FANIN_PAPERS,
    MAX_PAPER_LIBRARY_FANIN_TEXT_CHARACTERS, MAX_PAPER_LIBRARY_QA_TEXT_CHARACTERS,
    MAX_PAPER_LIBRARY_RESPONSE_BYTES, MAX_PAPER_LIBRARY_ROWS_PER_OWNER,
    MAX_PAPER_LIBRARY_STATE_DOCUMENT_BYTES, PAPER_LIBRARY_ARXIV_INDEX_NAMESPACE,
    PAPER_LIBRARY_CORE_NAMESPACE, PAPER_LIBRARY_COUNT_NAMESPACE,
    PAPER_LIBRARY_HASH_INDEX_NAMESPACE, PAPER_LIBRARY_REPORT_PRESENCE_NAMESPACE,
    PAPER_LIBRARY_STATE_NAMESPACE, PAPER_LIBRARY_UPDATED_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest as VersionedPutRequest};

const CORE_IDENTITY: &str = "paper_library_core";
const STATE_IDENTITY: &str = "paper_library_state";
const COUNT_KEY: &[u8] = b"count";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
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

fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("paper library index identity exceeds its bound"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}

fn validate_text(value: &str, maximum: usize, required: bool) -> io::Result<()> {
    if (required && value.is_empty()) || value.chars().count() > maximum {
        return Err(invalid_input("invalid paper library text field"));
    }
    Ok(())
}

fn core_key(transaction: &AuthorityTransaction, paper_id: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        PAPER_LIBRARY_CORE_NAMESPACE,
        paper_id.as_bytes(),
    )
}

fn state_key(transaction: &AuthorityTransaction, paper_id: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        PAPER_LIBRARY_STATE_NAMESPACE,
        paper_id.as_bytes(),
    )
}

fn count_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(transaction, PAPER_LIBRARY_COUNT_NAMESPACE, COUNT_KEY)
}

fn updated_index_key(
    transaction: &AuthorityTransaction,
    updated_at: u64,
    paper_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(10 + paper_id.len());
    raw.extend_from_slice(&(!updated_at).to_be_bytes());
    push_text(&mut raw, paper_id)?;
    owner_key(transaction, PAPER_LIBRARY_UPDATED_INDEX_NAMESPACE, &raw)
}

fn scoped_index_prefix(value: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(value.len() + 2);
    push_text(&mut raw, value)?;
    Ok(raw)
}

fn scoped_index_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    value: &str,
    updated_at: u64,
    paper_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = scoped_index_prefix(value)?;
    raw.extend_from_slice(&(!updated_at).to_be_bytes());
    push_text(&mut raw, paper_id)?;
    owner_key(transaction, namespace, &raw)
}

fn report_presence_key(
    transaction: &AuthorityTransaction,
    paper_hash: &str,
) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        PAPER_LIBRARY_REPORT_PRESENCE_NAMESPACE,
        paper_hash.as_bytes(),
    )
}

#[derive(Clone, Debug)]
pub struct PaperPut {
    pub paper_id: String,
    pub title: String,
    pub pdf_url: String,
    pub pdf_filename: String,
    pub arxiv_id: String,
    pub paper_hash: String,
    pub parsed_text: String,
    pub parser_version: String,
    pub qa_history_json: String,
    pub images_json: String,
    pub babel_cache_json: String,
    pub page_count: u64,
    pub folder_id: String,
    pub created_at: u64,
    pub updated_at: u64,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug)]
pub enum Request {
    Put(Box<PaperPut>),
    Delete {
        paper_id: String,
    },
    Recent {
        exclude_paper_hash: String,
        limit: usize,
    },
    List {
        summaries_only: bool,
    },
    Get {
        paper_id: String,
        include_babel_cache: bool,
    },
    Inputs {
        arxiv_ids: Vec<String>,
        max_text_characters: usize,
    },
    Identity {
        paper_hash: String,
        max_text_characters: Option<usize>,
        include_text_length: bool,
    },
    TitleBackfill {
        paper_hash: String,
        title: String,
        updated_at_seconds: u64,
        physical_updated_at_ms: u64,
    },
}

impl Request {
    pub fn mutates_state(&self) -> bool {
        matches!(
            self,
            Self::Put(_) | Self::Delete { .. } | Self::TitleBackfill { .. }
        )
    }

    pub fn validate(&self) -> io::Result<usize> {
        match self {
            Self::Put(request) => {
                let PaperPut {
                    paper_id,
                    title,
                    pdf_url,
                    pdf_filename,
                    arxiv_id,
                    paper_hash,
                    parsed_text,
                    parser_version,
                    qa_history_json,
                    images_json,
                    babel_cache_json,
                    folder_id,
                    physical_updated_at_ms,
                    ..
                } = request.as_ref();
                validate_text(paper_id, 256, true)?;
                validate_text(title, 1_000, false)?;
                validate_text(pdf_url, 10_000, false)?;
                validate_text(pdf_filename, 2_000, false)?;
                validate_text(arxiv_id, 256, false)?;
                validate_text(paper_hash, 128, false)?;
                validate_text(parsed_text, 20_000_000, false)?;
                validate_text(parser_version, 256, false)?;
                validate_text(qa_history_json, 10_000_000, false)?;
                validate_text(images_json, 10_000_000, false)?;
                validate_text(babel_cache_json, 10_000_000, false)?;
                validate_text(folder_id, 512, false)?;
                if *physical_updated_at_ms == 0 {
                    return Err(invalid_input("invalid paper library update timestamp"));
                }
                // Body strings are separately bounded blob inputs. Only compact
                // routing/state metadata consumes the shared IR literal budget.
                [
                    paper_id.len(),
                    title.len(),
                    pdf_url.len(),
                    pdf_filename.len(),
                    arxiv_id.len(),
                    paper_hash.len(),
                    parser_version.len(),
                    folder_id.len(),
                ]
                .into_iter()
                .try_fold(0usize, usize::checked_add)
                .ok_or_else(|| exhausted("paper library metadata byte count overflow"))
            }
            Self::Delete { paper_id } | Self::Get { paper_id, .. } => {
                validate_text(paper_id, 256, true)?;
                Ok(paper_id.len())
            }
            Self::Recent {
                exclude_paper_hash,
                limit,
            } => {
                validate_text(exclude_paper_hash, 128, false)?;
                if !(1..=200).contains(limit) {
                    return Err(invalid_input("invalid paper library recent limit"));
                }
                Ok(exclude_paper_hash.len())
            }
            Self::List { .. } => Ok(0),
            Self::Inputs {
                arxiv_ids,
                max_text_characters,
            } => {
                if arxiv_ids.len() > MAX_PAPER_LIBRARY_FANIN_PAPERS
                    || *max_text_characters > MAX_PAPER_LIBRARY_FANIN_TEXT_CHARACTERS
                {
                    return Err(invalid_input("invalid paper library input bound"));
                }
                let mut seen = BTreeSet::new();
                let mut bytes = 0usize;
                for arxiv_id in arxiv_ids {
                    validate_text(arxiv_id, 256, true)?;
                    if arxiv_id.trim() != arxiv_id || !seen.insert(arxiv_id) {
                        return Err(invalid_input(
                            "paper library arXiv identities are not normalized",
                        ));
                    }
                    bytes = bytes
                        .checked_add(arxiv_id.len())
                        .ok_or_else(|| exhausted("paper library input identities overflow"))?;
                }
                Ok(bytes)
            }
            Self::Identity {
                paper_hash,
                max_text_characters,
                include_text_length,
            } => {
                validate_text(paper_hash, 128, true)?;
                if max_text_characters
                    .is_some_and(|value| value > MAX_PAPER_LIBRARY_QA_TEXT_CHARACTERS)
                    || (!*include_text_length && *max_text_characters != Some(0))
                {
                    return Err(invalid_input("invalid paper library identity projection"));
                }
                Ok(paper_hash.len())
            }
            Self::TitleBackfill {
                paper_hash,
                title,
                physical_updated_at_ms,
                ..
            } => {
                validate_text(paper_hash, 128, true)?;
                validate_text(title, 1_000, true)?;
                if title.trim().is_empty() || *physical_updated_at_ms == 0 {
                    return Err(invalid_input("invalid paper library title backfill"));
                }
                Ok(paper_hash.len() + title.len())
            }
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct LibraryCore {
    id: String,
    parsed_text: String,
    qa_history_json: String,
    images_json: String,
    babel_cache_json: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct LibraryState {
    id: String,
    title: String,
    pdf_url: String,
    pdf_filename: String,
    arxiv_id: String,
    paper_hash: String,
    parser_version: String,
    page_count: u64,
    folder_id: String,
    created_at: u64,
    updated_at: u64,
    core_bytes: u64,
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<usize> {
    let Some(raw) = database.entity_get(transaction, &count_key(transaction)?)? else {
        return Ok(0);
    };
    let count = usize::try_from(u64::from_le_bytes(
        raw.try_into()
            .map_err(|_| invalid_data("paper library count is malformed"))?,
    ))
    .map_err(|_| invalid_data("paper library count overflows this platform"))?;
    if count > MAX_PAPER_LIBRARY_ROWS_PER_OWNER {
        return Err(invalid_data("paper library count exceeds its bound"));
    }
    Ok(count)
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    count: usize,
) -> io::Result<()> {
    if count > MAX_PAPER_LIBRARY_ROWS_PER_OWNER {
        return Err(exhausted("paper library owner capacity is exhausted"));
    }
    database.entity_put(
        transaction,
        count_key(transaction)?,
        u64::try_from(count)
            .map_err(|_| invalid_data("paper library count overflow"))?
            .to_le_bytes()
            .to_vec(),
    )
}

fn load_document<T: for<'de> Deserialize<'de>>(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    identity: &str,
    logical_key: &str,
    maximum_bytes: usize,
) -> io::Result<Option<T>> {
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        key,
        identity,
        logical_key,
        transaction.owner_user_id(),
        maximum_bytes,
    )?
    else {
        return Ok(None);
    };
    serde_json::from_slice(&raw)
        .map(Some)
        .map_err(|_| invalid_data("paper library document is malformed"))
}

fn load_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_id: &str,
) -> io::Result<Option<LibraryState>> {
    let state: Option<LibraryState> = load_document(
        database,
        transaction,
        &state_key(transaction, paper_id)?,
        STATE_IDENTITY,
        paper_id,
        MAX_PAPER_LIBRARY_STATE_DOCUMENT_BYTES,
    )?;
    if let Some(value) = &state {
        if value.id != paper_id
            || validate_text(&value.id, 256, true).is_err()
            || validate_text(&value.title, 1_000, false).is_err()
            || validate_text(&value.pdf_url, 10_000, false).is_err()
            || validate_text(&value.pdf_filename, 2_000, false).is_err()
            || validate_text(&value.arxiv_id, 256, false).is_err()
            || validate_text(&value.paper_hash, 128, false).is_err()
            || validate_text(&value.parser_version, 256, false).is_err()
            || validate_text(&value.folder_id, 512, false).is_err()
            || value.core_bytes == 0
            || value.core_bytes > MAX_PAPER_LIBRARY_CORE_DOCUMENT_BYTES as u64
        {
            return Err(invalid_data("paper library state violates its contract"));
        }
    }
    Ok(state)
}

fn load_core(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_id: &str,
) -> io::Result<LibraryCore> {
    let core: LibraryCore = load_document(
        database,
        transaction,
        &core_key(transaction, paper_id)?,
        CORE_IDENTITY,
        paper_id,
        MAX_PAPER_LIBRARY_CORE_DOCUMENT_BYTES,
    )?
    .ok_or_else(|| invalid_data("paper library core is missing"))?;
    if core.id != paper_id
        || validate_text(&core.id, 256, true).is_err()
        || validate_text(&core.parsed_text, 20_000_000, false).is_err()
        || validate_text(&core.qa_history_json, 10_000_000, false).is_err()
        || validate_text(&core.images_json, 10_000_000, false).is_err()
        || validate_text(&core.babel_cache_json, 10_000_000, false).is_err()
    {
        return Err(invalid_data("paper library core violates its contract"));
    }
    Ok(core)
}

struct DocumentWrite<'a, T> {
    key: EntityKey,
    identity: &'a str,
    logical_key: &'a str,
    value: &'a T,
    expected_version: Option<u64>,
    updated_at_ms: u64,
    maximum_bytes: usize,
}

fn store_document<T: Serialize>(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: DocumentWrite<'_, T>,
) -> io::Result<usize> {
    let raw = serde_json::to_vec(request.value)
        .map_err(|_| invalid_data("paper library document cannot be encoded"))?;
    if raw.len() > request.maximum_bytes {
        return Err(exhausted("paper library document exceeds its byte bound"));
    }
    let bytes = raw.len();
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        VersionedPutRequest {
            key: request.key,
            namespace: request.identity.to_owned(),
            logical_key: request.logical_key.to_owned(),
            value_json: raw,
            expected_version: request.expected_version,
            updated_at_ms: request.updated_at_ms,
        },
        transaction.owner_user_id(),
        request.maximum_bytes,
    )?;
    Ok(bytes)
}

fn stored_version(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
    identity: &str,
    logical_key: &str,
) -> io::Result<u64> {
    let stored = database
        .entity_get(transaction, key)?
        .ok_or_else(|| invalid_data("paper library document disappeared"))?;
    versioned_document::stored_document_version(&stored, identity, logical_key)
}

fn delete_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    state: &LibraryState,
) -> io::Result<()> {
    for key in [
        updated_index_key(transaction, state.updated_at, &state.id)?,
        scoped_index_key(
            transaction,
            PAPER_LIBRARY_HASH_INDEX_NAMESPACE,
            &state.paper_hash,
            state.updated_at,
            &state.id,
        )?,
        scoped_index_key(
            transaction,
            PAPER_LIBRARY_ARXIV_INDEX_NAMESPACE,
            &state.arxiv_id,
            state.updated_at,
            &state.id,
        )?,
    ] {
        database.entity_delete(transaction, key)?;
    }
    Ok(())
}

fn put_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    state: &LibraryState,
) -> io::Result<()> {
    let id = state.id.as_bytes().to_vec();
    database.entity_put(
        transaction,
        updated_index_key(transaction, state.updated_at, &state.id)?,
        id.clone(),
    )?;
    database.entity_put(
        transaction,
        scoped_index_key(
            transaction,
            PAPER_LIBRARY_HASH_INDEX_NAMESPACE,
            &state.paper_hash,
            state.updated_at,
            &state.id,
        )?,
        id.clone(),
    )?;
    database.entity_put(
        transaction,
        scoped_index_key(
            transaction,
            PAPER_LIBRARY_ARXIV_INDEX_NAMESPACE,
            &state.arxiv_id,
            state.updated_at,
            &state.id,
        )?,
        id,
    )
}

fn paper_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Vec<u8>> {
    let Request::Put(request) = request else {
        return Err(invalid_input("paper library request is not a put"));
    };
    let PaperPut {
        paper_id,
        title,
        pdf_url,
        pdf_filename,
        arxiv_id,
        paper_hash,
        parsed_text,
        parser_version,
        qa_history_json,
        images_json,
        babel_cache_json,
        page_count,
        folder_id,
        created_at,
        updated_at,
        physical_updated_at_ms,
    } = request.as_ref();
    let previous = load_state(database, transaction, paper_id)?;
    let count = read_count(database, transaction)?;
    if previous.is_none() && count >= MAX_PAPER_LIBRARY_ROWS_PER_OWNER {
        return Err(exhausted("paper library owner capacity is exhausted"));
    }
    let core = LibraryCore {
        id: paper_id.clone(),
        parsed_text: parsed_text.clone(),
        qa_history_json: qa_history_json.clone(),
        images_json: images_json.clone(),
        babel_cache_json: babel_cache_json.clone(),
    };
    let core_entity_key = core_key(transaction, paper_id)?;
    let core_version = previous
        .as_ref()
        .map(|_| {
            stored_version(
                database,
                transaction,
                &core_entity_key,
                CORE_IDENTITY,
                paper_id,
            )
        })
        .transpose()?;
    let core_bytes = store_document(
        database,
        transaction,
        DocumentWrite {
            key: core_entity_key,
            identity: CORE_IDENTITY,
            logical_key: paper_id,
            value: &core,
            expected_version: Some(core_version.unwrap_or(0)),
            updated_at_ms: *physical_updated_at_ms,
            maximum_bytes: MAX_PAPER_LIBRARY_CORE_DOCUMENT_BYTES,
        },
    )?;
    let state = LibraryState {
        id: paper_id.clone(),
        title: title.clone(),
        pdf_url: pdf_url.clone(),
        pdf_filename: pdf_filename.clone(),
        arxiv_id: arxiv_id.clone(),
        paper_hash: paper_hash.clone(),
        parser_version: parser_version.clone(),
        page_count: *page_count,
        folder_id: folder_id.clone(),
        created_at: previous
            .as_ref()
            .map_or(*created_at, |value| value.created_at),
        updated_at: *updated_at,
        core_bytes: u64::try_from(core_bytes)
            .map_err(|_| invalid_data("paper library core size overflow"))?,
    };
    let state_entity_key = state_key(transaction, paper_id)?;
    let state_version = previous
        .as_ref()
        .map(|_| {
            stored_version(
                database,
                transaction,
                &state_entity_key,
                STATE_IDENTITY,
                paper_id,
            )
        })
        .transpose()?;
    if let Some(previous) = &previous {
        delete_indexes(database, transaction, previous)?;
    }
    store_document(
        database,
        transaction,
        DocumentWrite {
            key: state_entity_key,
            identity: STATE_IDENTITY,
            logical_key: paper_id,
            value: &state,
            expected_version: Some(state_version.unwrap_or(0)),
            updated_at_ms: *physical_updated_at_ms,
            maximum_bytes: MAX_PAPER_LIBRARY_STATE_DOCUMENT_BYTES,
        },
    )?;
    put_indexes(database, transaction, &state)?;
    if previous.is_none() {
        write_count(database, transaction, count + 1)?;
    }
    Ok(br#"{"saved":true}"#.to_vec())
}

fn paper_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_id: &str,
) -> io::Result<Vec<u8>> {
    let Some(state) = load_state(database, transaction, paper_id)? else {
        return Ok(br#"{"deleted":false}"#.to_vec());
    };
    delete_indexes(database, transaction, &state)?;
    versioned_document::delete(
        database,
        transaction,
        state_key(transaction, paper_id)?,
        STATE_IDENTITY,
        paper_id,
        None,
    )?;
    versioned_document::delete(
        database,
        transaction,
        core_key(transaction, paper_id)?,
        CORE_IDENTITY,
        paper_id,
        None,
    )?;
    let count = read_count(database, transaction)?;
    write_count(
        database,
        transaction,
        count
            .checked_sub(1)
            .ok_or_else(|| invalid_data("paper library count underflow"))?,
    )?;
    Ok(br#"{"deleted":true}"#.to_vec())
}

fn scan_ids(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    maximum: usize,
) -> io::Result<Vec<String>> {
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        prefix,
    )?;
    let mut ids = Vec::new();
    while ids.len() < maximum {
        let limit = (maximum - ids.len()).min(MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, limit)?;
        if page.is_empty() {
            break;
        }
        for (_, raw) in &page {
            let id = std::str::from_utf8(raw)
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| invalid_data("paper library index identity is malformed"))?;
            validate_text(id, 256, true)
                .map_err(|_| invalid_data("paper library index identity is malformed"))?;
            ids.push(id.to_owned());
        }
        let mut next = page
            .last()
            .expect("nonempty paper library page")
            .0
            .key_bytes()
            .to_vec();
        next.push(0);
        cursor = owner_key(transaction, namespace, &next)?;
    }
    Ok(ids)
}

fn has_report(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
) -> io::Result<bool> {
    Ok(database
        .entity_get(transaction, &report_presence_key(transaction, paper_hash)?)?
        .is_some())
}

fn parse_json_or(raw: &str, fallback: Value) -> io::Result<Value> {
    let value: Value = serde_json::from_str(raw)
        .map_err(|_| invalid_data("paper library auxiliary JSON is malformed"))?;
    Ok(match &value {
        Value::Null => fallback,
        Value::Bool(false) => fallback,
        Value::Number(number) if number.as_f64() == Some(0.0) => fallback,
        Value::String(value) if value.is_empty() => fallback,
        Value::Array(value) if value.is_empty() => fallback,
        Value::Object(value) if value.is_empty() => fallback,
        _ => value,
    })
}

fn summary_value(state: &LibraryState, has_report: bool) -> Value {
    json!({
        "id": state.id,
        "title": state.title,
        "pdfUrl": state.pdf_url,
        "pdfFilename": state.pdf_filename,
        "arxivId": state.arxiv_id,
        "paperHash": state.paper_hash,
        "pageCount": state.page_count,
        "folderId": state.folder_id,
        "createdAt": state.created_at,
        "updatedAt": state.updated_at,
        "hasReport": has_report,
    })
}

fn detail_value(
    state: &LibraryState,
    core: &LibraryCore,
    has_report: bool,
    include_babel_cache: bool,
) -> io::Result<Value> {
    let mut value = summary_value(state, has_report);
    let object = value
        .as_object_mut()
        .expect("paper library summary is an object");
    object.insert(
        "parsedText".to_owned(),
        Value::String(core.parsed_text.clone()),
    );
    object.insert(
        "qaHistory".to_owned(),
        parse_json_or(&core.qa_history_json, Value::Array(Vec::new()))?,
    );
    object.insert(
        "images".to_owned(),
        parse_json_or(&core.images_json, Value::Array(Vec::new()))?,
    );
    object.insert(
        "parserVersion".to_owned(),
        Value::String(state.parser_version.clone()),
    );
    if include_babel_cache {
        object.insert(
            "babelCache".to_owned(),
            parse_json_or(&core.babel_cache_json, json!({}))?,
        );
    }
    Ok(value)
}

fn encode_response(value: &Value) -> io::Result<Vec<u8>> {
    let encoded = serde_json::to_vec(value)
        .map_err(|_| invalid_data("paper library response cannot be encoded"))?;
    if encoded.len() > MAX_PAPER_LIBRARY_RESPONSE_BYTES {
        return Err(exhausted("paper library response exceeds 64 MiB"));
    }
    Ok(encoded)
}

fn paper_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    summaries_only: bool,
) -> io::Result<Vec<u8>> {
    let count = read_count(database, transaction)?;
    let ids = scan_ids(
        database,
        transaction,
        PAPER_LIBRARY_UPDATED_INDEX_NAMESPACE,
        b"",
        count + 1,
    )?;
    if ids.len() != count {
        return Err(invalid_data("paper library index count differs"));
    }
    let mut rows = Vec::with_capacity(ids.len());
    let mut projected_bytes = 2usize;
    for id in ids {
        let state = load_state(database, transaction, &id)?
            .ok_or_else(|| invalid_data("paper library index target is missing"))?;
        let report = has_report(database, transaction, &state.paper_hash)?;
        let value = if summaries_only {
            summary_value(&state, report)
        } else {
            let core_bytes = usize::try_from(state.core_bytes)
                .map_err(|_| invalid_data("paper library core size is malformed"))?;
            projected_bytes = projected_bytes
                .checked_add(core_bytes)
                .filter(|bytes| *bytes <= MAX_PAPER_LIBRARY_RESPONSE_BYTES)
                .ok_or_else(|| exhausted("paper library response exceeds 64 MiB"))?;
            detail_value(
                &state,
                &load_core(database, transaction, &id)?,
                report,
                true,
            )?
        };
        rows.push(value);
    }
    encode_response(&Value::Array(rows))
}

fn paper_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_id: &str,
    include_babel_cache: bool,
) -> io::Result<Option<Vec<u8>>> {
    let Some(state) = load_state(database, transaction, paper_id)? else {
        return Ok(None);
    };
    let report = has_report(database, transaction, &state.paper_hash)?;
    encode_response(&detail_value(
        &state,
        &load_core(database, transaction, paper_id)?,
        report,
        include_babel_cache,
    )?)
    .map(Some)
}

fn paper_recent(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    exclude_paper_hash: &str,
    limit: usize,
) -> io::Result<Vec<u8>> {
    let count = read_count(database, transaction)?;
    let ids = scan_ids(
        database,
        transaction,
        PAPER_LIBRARY_UPDATED_INDEX_NAMESPACE,
        b"",
        count + 1,
    )?;
    if ids.len() != count {
        return Err(invalid_data("paper library index count differs"));
    }
    let mut rows = Vec::with_capacity(limit);
    for id in ids {
        let state = load_state(database, transaction, &id)?
            .ok_or_else(|| invalid_data("paper library index target is missing"))?;
        if state.paper_hash != exclude_paper_hash && !state.title.is_empty() {
            rows.push(json!({"title": state.title, "arxiv_id": state.arxiv_id}));
            if rows.len() == limit {
                break;
            }
        }
    }
    encode_response(&Value::Array(rows))
}

fn paper_inputs(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    arxiv_ids: &[String],
    max_text_characters: usize,
) -> io::Result<Vec<u8>> {
    let mut matches = Vec::<LibraryState>::new();
    for arxiv_id in arxiv_ids {
        let prefix = scoped_index_prefix(arxiv_id)?;
        let ids = scan_ids(
            database,
            transaction,
            PAPER_LIBRARY_ARXIV_INDEX_NAMESPACE,
            &prefix,
            MAX_PAPER_LIBRARY_ROWS_PER_OWNER + 1,
        )?;
        if ids.len() > MAX_PAPER_LIBRARY_ROWS_PER_OWNER {
            return Err(invalid_data("paper library arXiv index exceeds its bound"));
        }
        for id in ids {
            let state = load_state(database, transaction, &id)?
                .ok_or_else(|| invalid_data("paper library arXiv index target is missing"))?;
            if state.arxiv_id != *arxiv_id {
                return Err(invalid_data("paper library arXiv index scope differs"));
            }
            matches.push(state);
        }
    }
    matches.sort_by(|left, right| {
        right
            .updated_at
            .cmp(&left.updated_at)
            .then_with(|| left.id.cmp(&right.id))
    });
    let mut rows = Vec::with_capacity(matches.len());
    for state in matches {
        let core = load_core(database, transaction, &state.id)?;
        let parsed_text = core
            .parsed_text
            .chars()
            .take(max_text_characters)
            .collect::<String>();
        rows.push(json!({
            "id": state.id,
            "title": state.title,
            "arxivId": state.arxiv_id,
            "paperHash": state.paper_hash,
            "parsedText": parsed_text,
            "parsedTextLength": core.parsed_text.chars().count(),
            "parserVersion": state.parser_version,
            "pageCount": state.page_count,
            "folderId": state.folder_id,
            "createdAt": state.created_at,
            "updatedAt": state.updated_at,
        }));
    }
    encode_response(&Value::Array(rows))
}

fn paper_identity(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    max_text_characters: Option<usize>,
    include_text_length: bool,
) -> io::Result<Option<Vec<u8>>> {
    let ids = scan_ids(
        database,
        transaction,
        PAPER_LIBRARY_HASH_INDEX_NAMESPACE,
        &scoped_index_prefix(paper_hash)?,
        1,
    )?;
    let Some(id) = ids.first() else {
        return Ok(None);
    };
    let state = load_state(database, transaction, id)?
        .ok_or_else(|| invalid_data("paper library hash index target is missing"))?;
    if state.paper_hash != paper_hash {
        return Err(invalid_data("paper library hash index scope differs"));
    }
    let core = load_core(database, transaction, id)?;
    let parsed_text = match max_text_characters {
        None => core.parsed_text.clone(),
        Some(maximum) => core.parsed_text.chars().take(maximum).collect(),
    };
    let parsed_text_length = if include_text_length {
        core.parsed_text.chars().count()
    } else {
        0
    };
    encode_response(&json!({
        "title": state.title,
        "arxiv_id": state.arxiv_id,
        "parsed_text": parsed_text,
        "parsed_text_length": parsed_text_length,
    }))
    .map(Some)
}

fn is_placeholder_title(title: &str) -> bool {
    let normalized = title.trim().to_lowercase();
    normalized.is_empty() || normalized.starts_with("arxiv:") || normalized.starts_with("arxiv ")
}

fn title_backfill(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    title: &str,
    updated_at_seconds: u64,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let ids = scan_ids(
        database,
        transaction,
        PAPER_LIBRARY_HASH_INDEX_NAMESPACE,
        &scoped_index_prefix(paper_hash)?,
        MAX_PAPER_LIBRARY_ROWS_PER_OWNER + 1,
    )?;
    if ids.len() > MAX_PAPER_LIBRARY_ROWS_PER_OWNER {
        return Err(invalid_data("paper library hash index exceeds its bound"));
    }
    let normalized_title = title.trim();
    let mut states = Vec::with_capacity(ids.len());
    for id in ids {
        let state = load_state(database, transaction, &id)?
            .ok_or_else(|| invalid_data("paper library hash index target is missing"))?;
        if state.paper_hash != paper_hash {
            return Err(invalid_data("paper library hash index scope differs"));
        }
        states.push(state);
    }
    let authoritative = states
        .iter()
        .find(|state| !is_placeholder_title(&state.title))
        .map(|state| state.title.trim().to_owned());
    let mut updated = 0usize;
    for mut state in states {
        if !is_placeholder_title(&state.title) {
            continue;
        }
        let key = state_key(transaction, &state.id)?;
        let version = stored_version(database, transaction, &key, STATE_IDENTITY, &state.id)?;
        delete_indexes(database, transaction, &state)?;
        state.title = normalized_title.to_owned();
        state.updated_at = updated_at_seconds;
        store_document(
            database,
            transaction,
            DocumentWrite {
                key,
                identity: STATE_IDENTITY,
                logical_key: &state.id,
                value: &state,
                expected_version: Some(version),
                updated_at_ms: physical_updated_at_ms,
                maximum_bytes: MAX_PAPER_LIBRARY_STATE_DOCUMENT_BYTES,
            },
        )?;
        put_indexes(database, transaction, &state)?;
        updated += 1;
    }
    encode_response(&json!({
        "title": authoritative.unwrap_or_else(|| normalized_title.to_owned()),
        "updated": updated,
    }))
}

pub fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        Request::Put(_) => paper_put(database, transaction, request).map(Some),
        Request::Delete { paper_id } => paper_delete(database, transaction, paper_id).map(Some),
        Request::Recent {
            exclude_paper_hash,
            limit,
        } => paper_recent(database, transaction, exclude_paper_hash, *limit).map(Some),
        Request::List { summaries_only } => {
            paper_list(database, transaction, *summaries_only).map(Some)
        }
        Request::Get {
            paper_id,
            include_babel_cache,
        } => paper_get(database, transaction, paper_id, *include_babel_cache),
        Request::Inputs {
            arxiv_ids,
            max_text_characters,
        } => paper_inputs(database, transaction, arxiv_ids, *max_text_characters).map(Some),
        Request::Identity {
            paper_hash,
            max_text_characters,
            include_text_length,
        } => paper_identity(
            database,
            transaction,
            paper_hash,
            *max_text_characters,
            *include_text_length,
        ),
        Request::TitleBackfill {
            paper_hash,
            title,
            updated_at_seconds,
            physical_updated_at_ms,
        } => title_backfill(
            database,
            transaction,
            paper_hash,
            title,
            *updated_at_seconds,
            *physical_updated_at_ms,
        )
        .map(Some),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chronological_and_scoped_indexes_are_prefix_safe() {
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let transaction = database.begin(7, 11).unwrap();
        assert!(
            updated_index_key(&transaction, 20, "a").unwrap()
                < updated_index_key(&transaction, 10, "z").unwrap()
        );
        assert_ne!(
            scoped_index_prefix("a").unwrap(),
            scoped_index_prefix("a\0").unwrap()
        );
        assert_ne!(
            scoped_index_prefix("ab").unwrap(),
            scoped_index_prefix("a").unwrap()
        );
    }

    #[test]
    fn large_core_does_not_consume_the_ir_literal_budget() {
        let request = Request::Put(Box::new(PaperPut {
            paper_id: "paper".to_owned(),
            title: "title".to_owned(),
            pdf_url: String::new(),
            pdf_filename: String::new(),
            arxiv_id: "2401.00001".to_owned(),
            paper_hash: "hash".to_owned(),
            parsed_text: "x".repeat(9 * 1024 * 1024),
            parser_version: String::new(),
            qa_history_json: "[]".to_owned(),
            images_json: "[]".to_owned(),
            babel_cache_json: "{}".to_owned(),
            page_count: 0,
            folder_id: String::new(),
            created_at: 1,
            updated_at: 1,
            physical_updated_at_ms: 1,
        }));
        assert!(request.validate().unwrap() < 8 * 1024 * 1024);
    }
}
