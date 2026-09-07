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
}
