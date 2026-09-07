//! Bounded paper-podcast authority with compact tenant-wide interruption state.
//!
//! Large scripts and metadata live in owner-bound core documents. Frequently
//! changed status and timing fields live separately, so startup interruption
//! never rewrites audio-generation payloads. A bounded tenant-global index is
//! the only cross-owner maintenance surface.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PAPER_PODCAST_ACTIVE_PER_TENANT, MAX_PAPER_PODCAST_CORE_DOCUMENT_BYTES,
    MAX_PAPER_PODCAST_META_BYTES, MAX_PAPER_PODCAST_RESPONSE_BYTES,
    MAX_PAPER_PODCAST_ROWS_PER_OWNER, MAX_PAPER_PODCAST_SCRIPT_BYTES,
    MAX_PAPER_PODCAST_STATE_DOCUMENT_BYTES, PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE,
    PAPER_PODCAST_ACTIVE_INDEX_NAMESPACE, PAPER_PODCAST_CORE_NAMESPACE,
    PAPER_PODCAST_COUNT_NAMESPACE, PAPER_PODCAST_STATE_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const CORE_IDENTITY: &str = "paper_podcast_core";
const STATE_IDENTITY: &str = "paper_podcast_state";
const COUNT_KEY: &[u8] = b"count";
const VALID_STATUSES: [&str; 6] = [
    "generating",
    "interrupted",
    "done",
    "script_only",
    "error",
    "aborted",
];

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn validate_text(value: &str, maximum: usize, required: bool) -> io::Result<()> {
    if (required && value.is_empty()) || value.chars().count() > maximum {
        return Err(invalid_input("invalid paper podcast text field"));
    }
    Ok(())
}

fn push_text(raw: &mut Vec<u8>, value: &str) -> io::Result<()> {
    raw.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("paper podcast identity is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(value.as_bytes());
    Ok(())
}

fn identity(paper_hash: &str, mode: &str, lang: &str, voice: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(paper_hash.len() + mode.len() + lang.len() + voice.len() + 8);
    for value in [paper_hash, mode, lang, voice] {
        push_text(&mut raw, value)?;
    }
    Ok(raw)
}

fn owner_key(
    tx: &AuthorityTransaction,
    owner: u64,
    namespace: &str,
    raw: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), owner, namespace, raw)
}

fn document_key(
    tx: &AuthorityTransaction,
    owner: u64,
    namespace: &str,
    key: &PodcastKey,
) -> io::Result<EntityKey> {
    owner_key(
        tx,
        owner,
        namespace,
        &identity(&key.paper_hash, &key.mode, &key.lang, &key.voice)?,
    )
}

fn count_key(tx: &AuthorityTransaction, owner: u64, namespace: &str) -> io::Result<EntityKey> {
    owner_key(tx, owner, namespace, COUNT_KEY)
}

fn active_key(tx: &AuthorityTransaction, owner: u64, key: &PodcastKey) -> io::Result<EntityKey> {
    let mut raw = owner.to_be_bytes().to_vec();
    raw.extend_from_slice(&identity(
        &key.paper_hash,
        &key.mode,
        &key.lang,
        &key.voice,
    )?);
    owner_key(
        tx,
        TENANT_GLOBAL_OWNER_ID,
        PAPER_PODCAST_ACTIVE_INDEX_NAMESPACE,
        &raw,
    )
}

fn hex(raw: &[u8]) -> String {
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn read_count(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    namespace: &str,
    maximum: usize,
) -> io::Result<usize> {
    let Some(raw) = db.entity_get(tx, &count_key(tx, owner, namespace)?)? else {
        return Ok(0);
    };
    let bytes: [u8; 8] = raw
        .try_into()
        .map_err(|_| invalid_data("paper podcast count is malformed"))?;
    let value = usize::try_from(u64::from_be_bytes(bytes))
        .map_err(|_| invalid_data("paper podcast count exceeds this platform"))?;
    if value > maximum {
        return Err(invalid_data("paper podcast count exceeds its bound"));
    }
    Ok(value)
}

fn write_count(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    namespace: &str,
    value: usize,
    maximum: usize,
) -> io::Result<()> {
    if value > maximum {
        return Err(exhausted("paper podcast count exceeds its bound"));
    }
    db.entity_put(
        tx,
        count_key(tx, owner, namespace)?,
        u64::try_from(value)
            .map_err(|_| exhausted("paper podcast count cannot be represented"))?
            .to_be_bytes()
            .to_vec(),
    )
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct PodcastKey {
    pub paper_hash: String,
    pub mode: String,
    pub lang: String,
    pub voice: String,
}

impl PodcastKey {
    fn validate(&self) -> io::Result<usize> {
        validate_text(&self.paper_hash, 128, true)?;
        validate_text(&self.mode, 64, true)?;
        validate_text(&self.lang, 32, true)?;
        validate_text(&self.voice, 256, false)?;
        Ok(self.paper_hash.len() + self.mode.len() + self.lang.len() + self.voice.len())
    }
}

#[derive(Clone, Debug)]
pub struct PodcastPut {
    pub key: PodcastKey,
    pub status: String,
    pub script: Map<String, Value>,
    pub file_path: String,
    pub duration_sec: f64,
    pub model: String,
    pub tts_model: String,
    pub meta: Map<String, Value>,
    pub created_at: u64,
    pub updated_at: u64,
    pub physical_updated_at_ms: u64,
}

#[derive(Clone, Debug)]
pub enum Request {
    Upsert(Box<PodcastPut>),
    Get(PodcastKey),
    MarkInterrupted {
        updated_at: u64,
        physical_updated_at_ms: u64,
    },
}

impl Request {
    pub fn mutates_state(&self) -> bool {
        !matches!(self, Self::Get(_))
    }

    pub fn validate(&self) -> io::Result<usize> {
        match self {
            Self::Upsert(value) => {
                let identity_bytes = value.key.validate()?;
                validate_text(&value.status, 64, true)?;
                validate_text(&value.file_path, 10_000, false)?;
                validate_text(&value.model, 512, false)?;
                validate_text(&value.tts_model, 512, false)?;
                if !VALID_STATUSES.contains(&value.status.as_str())
                    || !value.duration_sec.is_finite()
                    || !(0.0..=10_000_000.0).contains(&value.duration_sec)
                    || value.physical_updated_at_ms == 0
                {
                    return Err(invalid_input("invalid paper podcast document"));
                }
                let script_bytes = serde_json::to_vec(&value.script)
                    .map_err(|_| invalid_input("paper podcast script cannot be encoded"))?
                    .len();
                let meta_bytes = serde_json::to_vec(&value.meta)
                    .map_err(|_| invalid_input("paper podcast metadata cannot be encoded"))?
                    .len();
                if script_bytes > MAX_PAPER_PODCAST_SCRIPT_BYTES
                    || meta_bytes > MAX_PAPER_PODCAST_META_BYTES
                {
                    return Err(exhausted("paper podcast content exceeds its bound"));
                }
                Ok(identity_bytes
                    + value.status.len()
                    + value.file_path.len()
                    + value.model.len()
                    + value.tts_model.len())
            }
            Self::Get(key) => key.validate(),
            Self::MarkInterrupted {
                physical_updated_at_ms,
                ..
            } => {
                if *physical_updated_at_ms == 0 {
                    return Err(invalid_input("invalid paper podcast timestamp"));
                }
                Ok(0)
            }
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct CoreDocument {
    key: PodcastKey,
    script: Map<String, Value>,
    file_path: String,
    model: String,
    tts_model: String,
    meta: Map<String, Value>,
    created_at: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct StateDocument {
    key: PodcastKey,
    status: String,
    duration_sec: f64,
    updated_at: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct ActiveLocator {
    owner: u64,
    key: PodcastKey,
}

fn load<T: for<'de> Deserialize<'de>>(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    namespace: &str,
    logical_key: &[u8],
    owner: u64,
    maximum: usize,
) -> io::Result<Option<T>> {
    let logical = hex(logical_key);
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        db, tx, key, namespace, &logical, owner, maximum,
    )?
    else {
        return Ok(None);
    };
    serde_json::from_slice(&raw)
        .map(Some)
        .map_err(|_| invalid_data("paper podcast document is malformed"))
}

struct DocumentWrite<'a, T> {
    key: EntityKey,
    namespace: &'a str,
    logical_key: &'a [u8],
    owner: u64,
    value: &'a T,
    updated_at_ms: u64,
    maximum: usize,
}

fn store<T: Serialize>(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: DocumentWrite<'_, T>,
) -> io::Result<()> {
    let logical = hex(request.logical_key);
    let expected = db
        .entity_get(tx, &request.key)?
        .as_deref()
        .map(|raw| versioned_document::stored_document_version(raw, request.namespace, &logical))
        .transpose()?
        .unwrap_or(0);
    let raw = serde_json::to_vec(request.value)
        .map_err(|_| invalid_data("paper podcast document cannot be encoded"))?;
    if raw.len() > request.maximum {
        return Err(exhausted("paper podcast document exceeds its byte bound"));
    }
    versioned_document::put_with_blob_owner_bounded(
        db,
        tx,
        PutRequest {
            key: request.key,
            namespace: request.namespace.to_owned(),
            logical_key: logical,
            value_json: raw,
            expected_version: Some(expected),
            updated_at_ms: request.updated_at_ms,
        },
        request.owner,
        request.maximum,
    )?;
    Ok(())
}

fn read_documents(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    key: &PodcastKey,
) -> io::Result<Option<(CoreDocument, StateDocument)>> {
    let logical = identity(&key.paper_hash, &key.mode, &key.lang, &key.voice)?;
    let core: Option<CoreDocument> = load(
        db,
        tx,
        &document_key(tx, owner, PAPER_PODCAST_CORE_NAMESPACE, key)?,
        CORE_IDENTITY,
        &logical,
        owner,
        MAX_PAPER_PODCAST_CORE_DOCUMENT_BYTES,
    )?;
    let state = read_state(db, tx, owner, key)?;
    match (core, state) {
        (None, None) => Ok(None),
        (Some(core), Some(state)) if core.key == *key => Ok(Some((core, state))),
        _ => Err(invalid_data(
            "paper podcast documents are incomplete or differ",
        )),
    }
}

fn read_state(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    key: &PodcastKey,
) -> io::Result<Option<StateDocument>> {
    let logical = identity(&key.paper_hash, &key.mode, &key.lang, &key.voice)?;
    let state: Option<StateDocument> = load(
        db,
        tx,
        &document_key(tx, owner, PAPER_PODCAST_STATE_NAMESPACE, key)?,
        STATE_IDENTITY,
        &logical,
        owner,
        MAX_PAPER_PODCAST_STATE_DOCUMENT_BYTES,
    )?;
    if state.as_ref().is_some_and(|state| state.key != *key) {
        return Err(invalid_data("paper podcast state identity differs"));
    }
    Ok(state)
}

fn store_state(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    owner: u64,
    state: &StateDocument,
    physical_updated_at_ms: u64,
) -> io::Result<()> {
    let logical = identity(
        &state.key.paper_hash,
        &state.key.mode,
        &state.key.lang,
        &state.key.voice,
    )?;
    store(
        db,
        tx,
        DocumentWrite {
            key: document_key(tx, owner, PAPER_PODCAST_STATE_NAMESPACE, &state.key)?,
            namespace: STATE_IDENTITY,
            logical_key: &logical,
            owner,
            value: state,
            updated_at_ms: physical_updated_at_ms,
            maximum: MAX_PAPER_PODCAST_STATE_DOCUMENT_BYTES,
        },
    )
}

fn upsert(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    value: &PodcastPut,
) -> io::Result<Vec<u8>> {
    let owner = tx.owner_user_id();
    let previous = read_documents(db, tx, owner, &value.key)?;
    let owner_count = read_count(
        db,
        tx,
        owner,
        PAPER_PODCAST_COUNT_NAMESPACE,
        MAX_PAPER_PODCAST_ROWS_PER_OWNER,
    )?;
    if previous.is_none() && owner_count == MAX_PAPER_PODCAST_ROWS_PER_OWNER {
        return Err(exhausted("paper podcast owner capacity is exhausted"));
    }
    let old_generating = previous
        .as_ref()
        .is_some_and(|(_, state)| state.status == "generating");
    let new_generating = value.status == "generating";
    let active_count = read_count(
        db,
        tx,
        TENANT_GLOBAL_OWNER_ID,
        PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE,
        MAX_PAPER_PODCAST_ACTIVE_PER_TENANT,
    )?;
    let active_key = active_key(tx, owner, &value.key)?;
    let active_locator = db.entity_get(tx, &active_key)?;
    match (old_generating, active_locator) {
        (false, None) => {}
        (true, Some(raw)) => {
            let locator: ActiveLocator = serde_json::from_slice(&raw)
                .map_err(|_| invalid_data("paper podcast active locator is malformed"))?;
            if locator.owner != owner || locator.key != value.key {
                return Err(invalid_data(
                    "paper podcast active locator identity differs",
                ));
            }
        }
        _ => {
            return Err(invalid_data(
                "paper podcast state and active locator differ",
            ));
        }
    }
    let next_active_count = match (old_generating, new_generating) {
        (false, true) => active_count
            .checked_add(1)
            .filter(|count| *count <= MAX_PAPER_PODCAST_ACTIVE_PER_TENANT)
            .ok_or_else(|| exhausted("paper podcast active capacity is exhausted"))?,
        (true, false) => active_count
            .checked_sub(1)
            .ok_or_else(|| invalid_data("paper podcast active count underflow"))?,
        _ => active_count,
    };
    let created_at = previous
        .as_ref()
        .map_or(value.created_at, |(core, _)| core.created_at);
    let core = CoreDocument {
        key: value.key.clone(),
        script: value.script.clone(),
        file_path: value.file_path.clone(),
        model: value.model.clone(),
        tts_model: value.tts_model.clone(),
        meta: value.meta.clone(),
        created_at,
    };
    let state = StateDocument {
        key: value.key.clone(),
        status: value.status.clone(),
        duration_sec: value.duration_sec,
        updated_at: value.updated_at,
    };
    let logical = identity(
        &value.key.paper_hash,
        &value.key.mode,
        &value.key.lang,
        &value.key.voice,
    )?;
    store(
        db,
        tx,
        DocumentWrite {
            key: document_key(tx, owner, PAPER_PODCAST_CORE_NAMESPACE, &value.key)?,
            namespace: CORE_IDENTITY,
            logical_key: &logical,
            owner,
            value: &core,
            updated_at_ms: value.physical_updated_at_ms,
            maximum: MAX_PAPER_PODCAST_CORE_DOCUMENT_BYTES,
        },
    )?;
    store_state(db, tx, owner, &state, value.physical_updated_at_ms)?;
    if new_generating {
        db.entity_put(
            tx,
            active_key,
            serde_json::to_vec(&ActiveLocator {
                owner,
                key: value.key.clone(),
            })
            .map_err(|_| invalid_data("paper podcast active locator cannot be encoded"))?,
        )?;
    } else if old_generating {
        db.entity_delete(tx, active_key)?;
    }
    if next_active_count != active_count {
        write_count(
            db,
            tx,
            TENANT_GLOBAL_OWNER_ID,
            PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE,
            next_active_count,
            MAX_PAPER_PODCAST_ACTIVE_PER_TENANT,
        )?;
    }
    if previous.is_none() {
        write_count(
            db,
            tx,
            owner,
            PAPER_PODCAST_COUNT_NAMESPACE,
            owner_count + 1,
            MAX_PAPER_PODCAST_ROWS_PER_OWNER,
        )?;
    }
    Ok(br#"{"saved":true}"#.to_vec())
}

fn projection(owner: u64, core: CoreDocument, state: StateDocument) -> Value {
    json!({
        "user_id": owner,
        "paper_hash": core.key.paper_hash,
        "mode": core.key.mode,
        "lang": core.key.lang,
        "voice": core.key.voice,
        "status": state.status,
        "script_json": core.script,
        "file_path": core.file_path,
        "duration_sec": state.duration_sec,
        "model": core.model,
        "tts_model": core.tts_model,
        "meta": core.meta,
        "created_at": core.created_at,
        "updated_at": state.updated_at,
    })
}

fn encode(value: &Value) -> io::Result<Vec<u8>> {
    let raw = serde_json::to_vec(value)
        .map_err(|_| invalid_data("paper podcast response cannot be encoded"))?;
    if raw.len() > MAX_PAPER_PODCAST_RESPONSE_BYTES {
        return Err(exhausted("paper podcast response exceeds its bound"));
    }
    Ok(raw)
}

fn mark_interrupted(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    updated_at: u64,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let active_count = read_count(
        db,
        tx,
        TENANT_GLOBAL_OWNER_ID,
        PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE,
        MAX_PAPER_PODCAST_ACTIVE_PER_TENANT,
    )?;
    let (start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        PAPER_PODCAST_ACTIVE_INDEX_NAMESPACE,
        b"",
    )?;
    let rows = db.entity_scan(tx, &start, &end, MAX_PAPER_PODCAST_ACTIVE_PER_TENANT + 1)?;
    if rows.len() != active_count {
        return Err(invalid_data("paper podcast active index count differs"));
    }
    for (index_key, raw) in rows {
        let locator: ActiveLocator = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("paper podcast active locator is malformed"))?;
        if locator.owner == TENANT_GLOBAL_OWNER_ID
            || index_key != active_key(tx, locator.owner, &locator.key)?
        {
            return Err(invalid_data(
                "paper podcast active locator identity differs",
            ));
        }
        db.authorize_entity_namespace_for_owner(tx, locator.owner, PAPER_PODCAST_STATE_NAMESPACE)?;
        let Some(mut state) = read_state(db, tx, locator.owner, &locator.key)? else {
            return Err(invalid_data(
                "paper podcast active locator target is missing",
            ));
        };
        if state.status != "generating" {
            return Err(invalid_data(
                "paper podcast active locator target is not generating",
            ));
        }
        state.status = "interrupted".to_owned();
        state.updated_at = updated_at;
        store_state(db, tx, locator.owner, &state, physical_updated_at_ms)?;
        db.entity_delete(tx, index_key)?;
    }
    if active_count != 0 {
        write_count(
            db,
            tx,
            TENANT_GLOBAL_OWNER_ID,
            PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE,
            0,
            MAX_PAPER_PODCAST_ACTIVE_PER_TENANT,
        )?;
    }
    encode(&json!({"changed": active_count}))
}

pub fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        Request::Upsert(value) => upsert(db, tx, value).map(Some),
        Request::Get(key) => read_documents(db, tx, tx.owner_user_id(), key)?
            .map(|(core, state)| encode(&projection(tx.owner_user_id(), core, state)))
            .transpose(),
        Request::MarkInterrupted {
            updated_at,
            physical_updated_at_ms,
        } => mark_interrupted(db, tx, *updated_at, *physical_updated_at_ms).map(Some),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn podcast_identities_are_length_prefix_safe() {
        assert_ne!(
            identity("a", "bc", "d", "").unwrap(),
            identity("ab", "c", "d", "").unwrap()
        );
    }
}
