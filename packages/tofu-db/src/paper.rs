//! Owner-scoped paper-domain authority.
//!
//! This first slice owns paper notes. Note bodies use bounded versioned
//! documents while compact paper/language/created indexes make ordered lists
//! independent of note size. Identity, count, document, index, receipt, and
//! outbox changes are committed through one Transaction IR transaction.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PAPER_NOTES_PER_OWNER, MAX_PAPER_NOTE_DOCUMENT_BYTES, MAX_PAPER_NOTE_RESPONSE_BYTES,
    PAPER_NOTE_COUNT_NAMESPACE, PAPER_NOTE_DOCUMENT_NAMESPACE, PAPER_NOTE_LIST_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const NOTE_LOGICAL_NAMESPACE: &str = "paper_notes";
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
            .map_err(|_| invalid_input("paper note identity exceeds its encoded bound"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}

fn document_key(transaction: &AuthorityTransaction, note_id: &str) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        PAPER_NOTE_DOCUMENT_NAMESPACE,
        note_id.as_bytes(),
    )
}

fn count_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(transaction, PAPER_NOTE_COUNT_NAMESPACE, COUNT_KEY)
}

fn list_prefix(paper_hash: &str, lang: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(paper_hash.len() + lang.len() + 4);
    push_text(&mut raw, paper_hash)?;
    push_text(&mut raw, lang)?;
    Ok(raw)
}

fn list_index_key(
    transaction: &AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
    created_at: u64,
    note_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = list_prefix(paper_hash, lang)?;
    raw.extend_from_slice(&created_at.to_be_bytes());
    push_text(&mut raw, note_id)?;
    owner_key(transaction, PAPER_NOTE_LIST_INDEX_NAMESPACE, &raw)
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<usize> {
    let Some(raw) = database.entity_get(transaction, &count_key(transaction)?)? else {
        return Ok(0);
    };
    let bytes: [u8; 8] = raw
        .try_into()
        .map_err(|_| invalid_data("paper note count is malformed"))?;
    let count = usize::try_from(u64::from_le_bytes(bytes))
        .map_err(|_| invalid_data("paper note count overflows this platform"))?;
    if count > MAX_PAPER_NOTES_PER_OWNER {
        return Err(invalid_data("paper note count exceeds its bound"));
    }
    Ok(count)
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    count: usize,
) -> io::Result<()> {
    if count > MAX_PAPER_NOTES_PER_OWNER {
        return Err(exhausted("paper note owner capacity is exhausted"));
    }
    database.entity_put(
        transaction,
        count_key(transaction)?,
        u64::try_from(count)
            .map_err(|_| invalid_data("paper note count overflow"))?
            .to_le_bytes()
            .to_vec(),
    )
}

#[derive(Clone, Debug)]
// The domain prefix keeps Transaction IR diagnostics and pattern matches
// self-describing; these variants intentionally share one note vocabulary.
#[allow(clippy::enum_variant_names)]
pub enum Request {
    NoteList {
        paper_hash: String,
        lang: String,
    },
    NoteCreate {
        note_id: String,
        paper_hash: String,
        lang: String,
        anchor: Map<String, Value>,
        note: String,
        created_at: u64,
        updated_at: u64,
        physical_updated_at_ms: u64,
    },
    NoteUpdate {
        note_id: String,
        note: String,
        updated_at: u64,
        physical_updated_at_ms: u64,
    },
    NoteDelete {
        note_id: String,
    },
}

impl Request {
    pub fn mutates_state(&self) -> bool {
        !matches!(self, Self::NoteList { .. })
    }

    pub fn validate(&self) -> io::Result<usize> {
        match self {
            Self::NoteList { paper_hash, lang } => {
                validate_paper_language(paper_hash, lang)?;
                Ok(paper_hash.len() + lang.len())
            }
            Self::NoteCreate {
                note_id,
                paper_hash,
                lang,
                anchor,
                note,
                physical_updated_at_ms,
                ..
            } => {
                validate_note_id(note_id)?;
                validate_paper_language(paper_hash, lang)?;
                if note.is_empty() || note.chars().count() > 100_000 || *physical_updated_at_ms == 0
                {
                    return Err(invalid_input("invalid paper note create"));
                }
                let anchor_bytes = serde_json::to_vec(anchor)
                    .map_err(|_| invalid_input("paper note anchor cannot be encoded"))?
                    .len();
                if anchor_bytes > MAX_PAPER_NOTE_DOCUMENT_BYTES {
                    return Err(exhausted("paper note anchor exceeds its byte bound"));
                }
                Ok(note_id.len() + paper_hash.len() + lang.len() + anchor_bytes)
            }
            Self::NoteUpdate {
                note_id,
                note,
                physical_updated_at_ms,
                ..
            } => {
                validate_note_id(note_id)?;
                if note.is_empty() || note.chars().count() > 100_000 || *physical_updated_at_ms == 0
                {
                    return Err(invalid_input("invalid paper note update"));
                }
                Ok(note_id.len())
            }
            Self::NoteDelete { note_id } => {
                validate_note_id(note_id)?;
                Ok(note_id.len())
            }
        }
    }
}

fn validate_note_id(note_id: &str) -> io::Result<()> {
    if note_id.is_empty() || note_id.chars().count() > 256 {
        return Err(invalid_input("invalid paper note identity"));
    }
    Ok(())
}

fn validate_paper_language(paper_hash: &str, lang: &str) -> io::Result<()> {
    if paper_hash.is_empty() || paper_hash.chars().count() > 128 || lang.chars().count() > 64 {
        return Err(invalid_input("invalid paper note scope"));
    }
    Ok(())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct NoteDocument {
    id: String,
    paper_hash: String,
    lang: String,
    anchor: Map<String, Value>,
    note: String,
    created_at: u64,
    updated_at: u64,
}

impl NoteDocument {
    fn validate(&self, expected_id: &str) -> io::Result<()> {
        validate_note_id(&self.id).map_err(|_| invalid_data("paper note identity is malformed"))?;
        validate_paper_language(&self.paper_hash, &self.lang)
            .map_err(|_| invalid_data("paper note scope is malformed"))?;
        if self.id != expected_id || self.note.is_empty() || self.note.chars().count() > 100_000 {
            return Err(invalid_data("paper note document is malformed"));
        }
        Ok(())
    }

    fn public_value(&self) -> Value {
        json!({
            "id": self.id,
            "paper_hash": self.paper_hash,
            "lang": self.lang,
            "anchor": self.anchor,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        })
    }
}

fn get_note(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    note_id: &str,
) -> io::Result<Option<NoteDocument>> {
    let key = document_key(transaction, note_id)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        NOTE_LOGICAL_NAMESPACE,
        note_id,
        transaction.owner_user_id(),
        MAX_PAPER_NOTE_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let document: NoteDocument = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("paper note document is malformed"))?;
    document.validate(note_id)?;
    Ok(Some(document))
}

fn put_note(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &NoteDocument,
    expected_version: Option<u64>,
    physical_updated_at_ms: u64,
) -> io::Result<()> {
    let raw = serde_json::to_vec(document)
        .map_err(|_| invalid_data("paper note document cannot be encoded"))?;
    if raw.len() > MAX_PAPER_NOTE_DOCUMENT_BYTES {
        return Err(exhausted("paper note document exceeds its byte bound"));
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, &document.id)?,
            namespace: NOTE_LOGICAL_NAMESPACE.to_owned(),
            logical_key: document.id.clone(),
            value_json: raw,
            expected_version,
            updated_at_ms: physical_updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_PAPER_NOTE_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn note_create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: NoteDocument,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    if get_note(database, transaction, &document.id)?.is_some() {
        return Err(invalid_data("paper note identity already exists"));
    }
    let count = read_count(database, transaction)?;
    write_count(
        database,
        transaction,
        count
            .checked_add(1)
            .ok_or_else(|| exhausted("paper note owner capacity is exhausted"))?,
    )?;
    put_note(
        database,
        transaction,
        &document,
        Some(0),
        physical_updated_at_ms,
    )?;
    database.entity_put(
        transaction,
        list_index_key(
            transaction,
            &document.paper_hash,
            &document.lang,
            document.created_at,
            &document.id,
        )?,
        document.id.as_bytes().to_vec(),
    )?;
    Ok(br#"{"saved":true}"#.to_vec())
}

fn note_update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    note_id: &str,
    note: &str,
    updated_at: u64,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some(mut document) = get_note(database, transaction, note_id)? else {
        return Ok(br#"{"updated":false}"#.to_vec());
    };
    document.note = note.to_owned();
    document.updated_at = updated_at;
    let key = document_key(transaction, note_id)?;
    let current = database
        .entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("paper note disappeared during update"))?;
    let version =
        versioned_document::stored_document_version(&current, NOTE_LOGICAL_NAMESPACE, note_id)?;
    put_note(
        database,
        transaction,
        &document,
        Some(version),
        physical_updated_at_ms,
    )?;
    Ok(br#"{"updated":true}"#.to_vec())
}

fn note_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    note_id: &str,
) -> io::Result<Vec<u8>> {
    let Some(document) = get_note(database, transaction, note_id)? else {
        return Ok(br#"{"deleted":false}"#.to_vec());
    };
    versioned_document::delete(
        database,
        transaction,
        document_key(transaction, note_id)?,
        NOTE_LOGICAL_NAMESPACE,
        note_id,
        None,
    )?;
    database.entity_delete(
        transaction,
        list_index_key(
            transaction,
            &document.paper_hash,
            &document.lang,
            document.created_at,
            note_id,
        )?,
    )?;
    let count = read_count(database, transaction)?;
    let next = count
        .checked_sub(1)
        .ok_or_else(|| invalid_data("paper note count underflow"))?;
    write_count(database, transaction, next)?;
    Ok(br#"{"deleted":true}"#.to_vec())
}

fn note_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
) -> io::Result<Vec<u8>> {
    let prefix = list_prefix(paper_hash, lang)?;
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PAPER_NOTE_LIST_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut note_ids = Vec::new();
    while note_ids.len() < MAX_PAPER_NOTES_PER_OWNER {
        let page_limit = (MAX_PAPER_NOTES_PER_OWNER - note_ids.len())
            .min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() {
            break;
        }
        for (_, raw) in &page {
            let note_id = std::str::from_utf8(raw)
                .map_err(|_| invalid_data("paper note index is malformed"))?
                .to_owned();
            validate_note_id(&note_id)
                .map_err(|_| invalid_data("paper note index is malformed"))?;
            note_ids.push(note_id);
        }
        let mut successor = page.last().expect("non-empty page").0.key_bytes().to_vec();
        successor.push(0);
        cursor = owner_key(transaction, PAPER_NOTE_LIST_INDEX_NAMESPACE, &successor)?;
    }
    let mut rows = Vec::with_capacity(note_ids.len());
    for note_id in note_ids {
        let document = get_note(database, transaction, &note_id)?
            .ok_or_else(|| invalid_data("paper note index document is missing"))?;
        if document.paper_hash != paper_hash || document.lang != lang {
            return Err(invalid_data("paper note index scope differs"));
        }
        rows.push(document.public_value());
    }
    let encoded = serde_json::to_vec(&rows)
        .map_err(|_| invalid_data("paper note response cannot be encoded"))?;
    if encoded.len() > MAX_PAPER_NOTE_RESPONSE_BYTES {
        return Err(exhausted("paper note response exceeds 64 MiB"));
    }
    Ok(encoded)
}

pub fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        Request::NoteList { paper_hash, lang } => {
            note_list(database, transaction, paper_hash, lang).map(Some)
        }
        Request::NoteCreate {
            note_id,
            paper_hash,
            lang,
            anchor,
            note,
            created_at,
            updated_at,
            physical_updated_at_ms,
        } => note_create(
            database,
            transaction,
            NoteDocument {
                id: note_id.clone(),
                paper_hash: paper_hash.clone(),
                lang: lang.clone(),
                anchor: anchor.clone(),
                note: note.clone(),
                created_at: *created_at,
                updated_at: *updated_at,
            },
            *physical_updated_at_ms,
        )
        .map(Some),
        Request::NoteUpdate {
            note_id,
            note,
            updated_at,
            physical_updated_at_ms,
        } => note_update(
            database,
            transaction,
            note_id,
            note,
            *updated_at,
            *physical_updated_at_ms,
        )
        .map(Some),
        Request::NoteDelete { note_id } => note_delete(database, transaction, note_id).map(Some),
    }
}

// ---------------------------------------------------------------------------
// Offline migration adapter. The source `paper_notes` row carries no version
// column, so the migration snapshot is exactly the note fields; the internal
// physical timestamp is derived deterministically from the snapshot content
// so an identical replay decodes to an identical envelope and is read-only.
// Insert-or-match fails closed on a divergent occupant, get reconstructs the
// exact source snapshot, and scan enumerates the owner-scoped note catalog
// independently of the source.
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NoteRestoreRequest {
    id: String,
    paper_hash: String,
    lang: String,
    anchor: Map<String, Value>,
    note: String,
    created_at: u64,
    updated_at: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NoteGetRequest {
    id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NoteScanRequest {
    cursor: Option<String>,
}

fn note_snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "paper note snapshot differs")
}

fn note_snapshot_document(request: &NoteRestoreRequest) -> NoteDocument {
    NoteDocument {
        id: request.id.clone(),
        paper_hash: request.paper_hash.clone(),
        lang: request.lang.clone(),
        anchor: request.anchor.clone(),
        note: request.note.clone(),
        created_at: request.created_at,
        updated_at: request.updated_at,
    }
}

/// The deterministic internal timestamp derived from the snapshot content:
/// an identical replay encodes to the identical native envelope, so the
/// insert-or-match check below stays byte-exact and read-only.
fn note_deterministic_updated_at_ms(document: &NoteDocument) -> io::Result<u64> {
    use sha2::{Digest, Sha256};
    let raw = serde_json::to_vec(document)
        .map_err(|_| invalid_data("paper note document cannot be encoded"))?;
    let digest = Sha256::digest(raw);
    Ok(u64::from_be_bytes(digest[..8].try_into().unwrap()) | 1)
}

fn parse_note_restore(payload: Value) -> io::Result<NoteRestoreRequest> {
    let request: NoteRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper note restore request"))?;
    validate_note_id(&request.id)?;
    validate_paper_language(&request.paper_hash, &request.lang)?;
    if request.note.is_empty()
        || request.note.chars().count() > 100_000
        || request.created_at == 0
        || request.updated_at == 0
        || request.updated_at < request.created_at
    {
        return Err(invalid_input("invalid paper note snapshot metadata"));
    }
    let anchor_bytes = serde_json::to_vec(&request.anchor)
        .map_err(|_| invalid_input("paper note anchor cannot be encoded"))?
        .len();
    if anchor_bytes > MAX_PAPER_NOTE_DOCUMENT_BYTES {
        return Err(exhausted("paper note anchor exceeds its byte bound"));
    }
    Ok(request)
}

/// Verify the decoded document, its list index, and the owner count all
/// agree with the snapshot; any divergence fails closed.
fn verify_note_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &NoteDocument,
) -> io::Result<()> {
    let observed = get_note(database, transaction, &document.id)?
        .ok_or_else(|| invalid_data("paper note snapshot document is missing"))?;
    if observed.id != document.id
        || observed.paper_hash != document.paper_hash
        || observed.lang != document.lang
        || observed.anchor != document.anchor
        || observed.note != document.note
        || observed.created_at != document.created_at
        || observed.updated_at != document.updated_at
    {
        return Err(invalid_data("paper note snapshot readback differs"));
    }
    let index = database.entity_get(
        transaction,
        &list_index_key(
            transaction,
            &document.paper_hash,
            &document.lang,
            document.created_at,
            &document.id,
        )?,
    )?;
    if index.as_deref() != Some(document.id.as_bytes()) {
        return Err(invalid_data("paper note list index differs"));
    }
    if read_count(database, transaction)? == 0 {
        return Err(invalid_data("paper note count is missing"));
    }
    Ok(())
}

fn note_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request = parse_note_restore(payload)?;
    let document = note_snapshot_document(&request);
    if let Some(existing) = get_note(database, transaction, &document.id)? {
        if existing.id != document.id
            || existing.paper_hash != document.paper_hash
            || existing.lang != document.lang
            || existing.anchor != document.anchor
            || existing.note != document.note
            || existing.created_at != document.created_at
            || existing.updated_at != document.updated_at
        {
            return Err(note_snapshot_conflict());
        }
        verify_note_restore(database, transaction, &document)?;
        return Ok(false);
    }
    let count = read_count(database, transaction)?;
    write_count(
        database,
        transaction,
        count
            .checked_add(1)
            .ok_or_else(|| exhausted("paper note owner capacity is exhausted"))?,
    )?;
    put_note(
        database,
        transaction,
        &document,
        Some(0),
        note_deterministic_updated_at_ms(&document)?,
    )?;
    database.entity_put(
        transaction,
        list_index_key(
            transaction,
            &document.paper_hash,
            &document.lang,
            document.created_at,
            &document.id,
        )?,
        document.id.as_bytes().to_vec(),
    )?;
    verify_note_restore(database, transaction, &document)?;
    Ok(true)
}

fn note_snapshot_value(document: &NoteDocument) -> Value {
    json!({
        "id": document.id,
        "paper_hash": document.paper_hash,
        "lang": document.lang,
        "anchor": document.anchor,
        "note": document.note,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    })
}

fn note_migration_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: NoteGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper note get request"))?;
    validate_note_id(&request.id)?;
    match get_note(database, transaction, &request.id)? {
        None => Ok(Value::Null),
        Some(document) => Ok(note_snapshot_value(&document)),
    }
}

fn note_migration_scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: NoteScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid paper note scan request"))?;
    let mut suffix = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("paper_note:")
            .ok_or_else(|| invalid_input("invalid paper note catalog cursor"))?;
        if encoded.is_empty() || encoded.len() > 256 {
            return Err(invalid_input("invalid paper note catalog cursor"));
        }
        suffix = encoded.as_bytes().to_vec();
        suffix.push(0);
    }
    let start = owner_key(transaction, PAPER_NOTE_DOCUMENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PAPER_NOTE_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(
        transaction,
        &start,
        &end,
        crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS,
    )?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, _) in &page {
        let id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("paper note catalog key is malformed"))?;
        validate_note_id(id).map_err(|_| invalid_data("paper note catalog key is malformed"))?;
        // The catalog revalidates its target rather than trusting the key.
        get_note(database, transaction, id)?
            .ok_or_else(|| invalid_data("paper note catalog target is missing"))?;
        rows.push(json!({"id": id}));
    }
    let cursor = if page.len() == crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("paper note catalog page is malformed"))?;
        Some(format!("paper_note:{last}"))
    } else {
        None
    };
    // The owner count must agree with the enumerated catalog on a full
    // single-page pass; a later page holds only that page's rows, so
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none()
        && cursor.is_none()
        && rows.len() != read_count(database, transaction)?
    {
        return Err(invalid_data("paper note count and catalog differ"));
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn note_migration_execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.paper.note.restore" => {
            let inserted = note_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.paper.note.get" => note_migration_get(database, &mut transaction, payload),
        "migration.paper.note.scan" => note_migration_scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown paper note migration operation")),
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn note_index_identity_is_prefix_safe_and_chronological() {
        let first = list_prefix("a", "bc").unwrap();
        let second = list_prefix("ab", "c").unwrap();
        assert_ne!(first, second);
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let transaction = database.begin(7, 11).unwrap();
        assert!(
            list_index_key(&transaction, "paper", "en", 1, "z").unwrap()
                < list_index_key(&transaction, "paper", "en", 2, "a").unwrap()
        );
    }

    #[test]
    fn note_list_crosses_entity_page_boundary() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        for sequence in 0..=crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS {
            let note_id = format!("note-{sequence:04}");
            let document = NoteDocument {
                id: note_id.clone(),
                paper_hash: "paper".to_owned(),
                lang: "en".to_owned(),
                anchor: Map::new(),
                note: "body".to_owned(),
                created_at: u64::try_from(sequence).unwrap(),
                updated_at: u64::try_from(sequence).unwrap(),
            };
            put_note(&database, &mut write, &document, Some(0), 1).unwrap();
            let index_key = list_index_key(
                &write,
                &document.paper_hash,
                &document.lang,
                document.created_at,
                &note_id,
            )
            .unwrap();
            database
                .entity_put(&mut write, index_key, note_id.into_bytes())
                .unwrap();
        }
        write_count(
            &database,
            &mut write,
            crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS + 1,
        )
        .unwrap();
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let encoded = note_list(&database, &mut read, "paper", "en").unwrap();
        let rows: Vec<Value> = serde_json::from_slice(&encoded).unwrap();
        assert_eq!(
            rows.len(),
            crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS + 1
        );
        assert_eq!(rows.first().unwrap()["id"], "note-0000");
        assert_eq!(rows.last().unwrap()["id"], "note-1000");
    }

    fn note_snapshot(id: &str, created: u64) -> Value {
        json!({
            "id": id,
            "paper_hash": "paper",
            "lang": "en",
            "anchor": {"page": 1},
            "note": format!("body-{id}"),
            "created_at": created,
            "updated_at": created,
        })
    }

    #[test]
    fn note_migration_restore_preserves_exact_state_and_is_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        assert_eq!(
            note_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.note.restore",
                note_snapshot("n1", 100),
            )
            .unwrap()["inserted"],
            true
        );
        // Identical replay is read-only.
        assert_eq!(
            note_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.note.restore",
                note_snapshot("n1", 100),
            )
            .unwrap()["inserted"],
            false
        );
        let get = note_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.note.get",
            json!({"id": "n1"}),
        )
        .unwrap();
        assert_eq!(get["note"], "body-n1");
        assert_eq!(get["created_at"], 100);
        // A divergent occupant fails closed.
        let mut divergent = note_snapshot("n1", 100);
        divergent["note"] = json!("changed");
        assert!(note_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.note.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn note_migration_scan_enumerates_owner_catalog_and_checks_count() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (id, created) in [("a", 1u64), ("b", 2), ("c", 3)] {
            note_migration_execute(
                &mut database,
                7,
                11,
                "migration.paper.note.restore",
                note_snapshot(id, created),
            )
            .unwrap();
        }
        let page = note_migration_execute(
            &mut database,
            7,
            11,
            "migration.paper.note.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(page["cursor"], Value::Null);
        // Another owner sees an empty catalog.
        assert_eq!(
            note_migration_execute(
                &mut database,
                7,
                12,
                "migration.paper.note.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
