//! Owner-scoped Research Foundry artifacts and optimistic workspace snapshots.
//!
//! Report bodies and workspaces use bounded versioned blobs. Direction lists
//! read only compact created-time index values, so interactive catalog reads
//! never hydrate multi-megabyte reports. Every document, compact state, index,
//! count, receipt, and outbox mutation shares one Transaction IR commit.

use std::collections::BTreeMap;
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_RESEARCH_ARTIFACTS_PER_OWNER, MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES,
    MAX_RESEARCH_DIRECTION_ROWS, MAX_RESEARCH_DIRECTION_SCAN_ROWS, MAX_RESEARCH_RESPONSE_BYTES,
    MAX_RESEARCH_WORKSPACE_BYTES, RESEARCH_ARTIFACT_COUNT_NAMESPACE,
    RESEARCH_ARTIFACT_CREATED_INDEX_NAMESPACE, RESEARCH_ARTIFACT_DOCUMENT_NAMESPACE,
    RESEARCH_ARTIFACT_STATE_NAMESPACE, RESEARCH_WORKSPACE_DOCUMENT_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const ARTIFACT_LOGICAL_NAMESPACE: &str = "research_artifacts";
const WORKSPACE_LOGICAL_NAMESPACE: &str = "research_workspaces";
const COUNT_KEY: &[u8] = b"count";

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

fn push_text(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    let length = u16::try_from(value.len())
        .map_err(|_| invalid_input("research identity exceeds its encoded bound"))?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn logical_key(paper_hash: &str, language: &str) -> io::Result<String> {
    let mut key = String::with_capacity(paper_hash.len() + language.len() + 8);
    use std::fmt::Write as _;
    write!(&mut key, "{}:{paper_hash}{language}", paper_hash.len())
        .map_err(|_| invalid_data("research logical identity cannot be encoded"))?;
    Ok(key)
}

fn identity_bytes(paper_hash: &str, language: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(paper_hash.len() + language.len() + 4);
    push_text(&mut raw, paper_hash)?;
    push_text(&mut raw, language)?;
    Ok(raw)
}

fn artifact_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    paper_hash: &str,
    lang_key: &str,
) -> io::Result<EntityKey> {
    owner_key(
        transaction,
        namespace,
        &identity_bytes(paper_hash, lang_key)?,
    )
}

fn workspace_key(
    transaction: &AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
) -> io::Result<EntityKey> {
    artifact_key(
        transaction,
        RESEARCH_WORKSPACE_DOCUMENT_NAMESPACE,
        paper_hash,
        lang,
    )
}

fn created_index_key(
    transaction: &AuthorityTransaction,
    created_at: u64,
    paper_hash: &str,
    lang_key: &str,
) -> io::Result<EntityKey> {
    let identity = identity_bytes(paper_hash, lang_key)?;
    let mut raw = Vec::with_capacity(8 + identity.len());
    raw.extend_from_slice(&(u64::MAX - created_at).to_be_bytes());
    raw.extend_from_slice(&identity);
    owner_key(transaction, RESEARCH_ARTIFACT_CREATED_INDEX_NAMESPACE, &raw)
}

fn decode_count(raw: Option<Vec<u8>>) -> io::Result<u64> {
    match raw {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_le_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("research artifact count is malformed")),
    }
}

fn encode_response(value: &Value) -> io::Result<Vec<u8>> {
    let encoded = serde_json::to_vec(value)
        .map_err(|_| invalid_data("research response cannot be encoded"))?;
    if encoded.len() > MAX_RESEARCH_RESPONSE_BYTES {
        return Err(resource_exhausted("research response exceeds 64 MiB"));
    }
    Ok(encoded)
}

fn scan_created_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    maximum: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        RESEARCH_ARTIFACT_CREATED_INDEX_NAMESPACE,
        b"",
    )?;
    let mut rows = Vec::new();
    if maximum == 0 {
        return Ok(rows);
    }
    while rows.len() < maximum {
        let page_limit =
            (maximum - rows.len()).min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() {
            break;
        }
        rows.extend(page);
        let mut successor = rows.last().expect("non-empty page").0.key_bytes().to_vec();
        successor.push(0);
        cursor = owner_key(
            transaction,
            RESEARCH_ARTIFACT_CREATED_INDEX_NAMESPACE,
            &successor,
        )?;
    }
    Ok(rows)
}

#[derive(Clone, Debug)]
pub struct ArtifactUpsertRequest {
    pub paper_hash: String,
    pub lang_key: String,
    pub report: String,
    pub model: String,
    pub meta: Map<String, Value>,
    pub created_at: u64,
    pub updated_at_ms: u64,
}

#[derive(Clone, Debug)]
pub enum ResearchRequest {
    ArtifactUpsert(ArtifactUpsertRequest),
    ArtifactsGet {
        paper_hash: String,
        lang: String,
    },
    DirectionsList {
        limit: usize,
    },
    WorkspaceGet {
        paper_hash: String,
        lang: String,
    },
    WorkspacePut {
        paper_hash: String,
        lang: String,
        expected_revision: u64,
        updated_at_seconds: u64,
        workspace: Map<String, Value>,
        physical_updated_at_ms: u64,
    },
}

impl ResearchRequest {
    pub fn mutates_state(&self) -> bool {
        matches!(self, Self::ArtifactUpsert(_) | Self::WorkspacePut { .. })
    }

    pub fn validate(&self) -> io::Result<usize> {
        let literal_bytes = match self {
            Self::ArtifactUpsert(request) => {
                if request.paper_hash.is_empty()
                    || request.paper_hash.chars().count() > 128
                    || !(request.lang_key.starts_with("survey:")
                        || request.lang_key.starts_with("ideate:"))
                    || request.lang_key.chars().count() > 64
                    || request.report.chars().count() > 10_000_000
                    || request.model.chars().count() > 512
                    || request.updated_at_ms == 0
                {
                    return Err(invalid_input("invalid research artifact upsert"));
                }
                let metadata_bytes = serde_json::to_vec(&request.meta)
                    .map_err(|_| invalid_input("research artifact cannot be encoded"))?
                    .len();
                let document_bytes = request
                    .paper_hash
                    .len()
                    .checked_add(request.lang_key.len())
                    .and_then(|bytes| bytes.checked_add(request.report.len()))
                    .and_then(|bytes| bytes.checked_add(request.model.len()))
                    .and_then(|bytes| bytes.checked_add(metadata_bytes))
                    .and_then(|bytes| bytes.checked_add(512))
                    .ok_or_else(|| resource_exhausted("research artifact byte count overflow"))?;
                if document_bytes > MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES {
                    return Err(resource_exhausted(
                        "research artifact exceeds its document byte bound",
                    ));
                }
                request.paper_hash.len() + request.lang_key.len() + request.model.len()
            }
            Self::ArtifactsGet { paper_hash, lang } => {
                if paper_hash.is_empty()
                    || paper_hash.chars().count() > 128
                    || lang.is_empty()
                    || lang.chars().count() > 32
                    || lang.contains(':')
                {
                    return Err(invalid_input("invalid research read identity"));
                }
                paper_hash.len() + lang.len()
            }
            Self::WorkspaceGet { paper_hash, lang } => {
                if paper_hash.is_empty()
                    || paper_hash.chars().count() > 128
                    || lang.is_empty()
                    || lang.chars().count() > 8
                {
                    return Err(invalid_input("invalid research workspace identity"));
                }
                paper_hash.len() + lang.len()
            }
            Self::DirectionsList { limit } => {
                if !(1..=MAX_RESEARCH_DIRECTION_ROWS).contains(limit) {
                    return Err(invalid_input("invalid research direction limit"));
                }
                0
            }
            Self::WorkspacePut {
                paper_hash,
                lang,
                workspace,
                physical_updated_at_ms,
                ..
            } => {
                if paper_hash.is_empty()
                    || paper_hash.chars().count() > 128
                    || lang.is_empty()
                    || lang.chars().count() > 8
                    || *physical_updated_at_ms == 0
                {
                    return Err(invalid_input("invalid research workspace mutation"));
                }
                let bytes = serde_json::to_vec(workspace)
                    .map_err(|_| invalid_input("research workspace cannot be encoded"))?
                    .len();
                if bytes > MAX_RESEARCH_WORKSPACE_BYTES {
                    return Err(resource_exhausted(
                        "research workspace exceeds its document byte bound",
                    ));
                }
                paper_hash.len() + lang.len()
            }
        };
        Ok(literal_bytes)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ArtifactDocument {
    paper_hash: String,
    lang_key: String,
    report: String,
    model: String,
    meta: Map<String, Value>,
    created_at: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct DirectionIndexValue {
    paper_hash: String,
    lang_key: String,
    direction: String,
    created_at: u64,
    accepted: usize,
    rejected: usize,
    gate_reached: String,
    degraded: bool,
    kind: String,
    document_bytes: usize,
}

impl DirectionIndexValue {
    fn from_request(request: &ArtifactUpsertRequest, document_bytes: usize) -> io::Result<Self> {
        let direction = request
            .meta
            .get("direction")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_owned();
        if direction.chars().count() > 4096 {
            return Err(invalid_input("research direction exceeds its text bound"));
        }
        let kind = request
            .meta
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if kind.chars().count() > 64 {
            return Err(invalid_input(
                "research artifact kind exceeds its text bound",
            ));
        }
        let accepted = request
            .meta
            .get("accepted")
            .filter(|value| !value.is_null())
            .map(|value| {
                value
                    .as_array()
                    .map(Vec::len)
                    .ok_or_else(|| invalid_input("research accepted directions must be an array"))
            })
            .transpose()?
            .unwrap_or(0);
        let rejected = request
            .meta
            .get("rejected")
            .filter(|value| !value.is_null())
            .map(|value| {
                value
                    .as_array()
                    .map(Vec::len)
                    .ok_or_else(|| invalid_input("research rejected directions must be an array"))
            })
            .transpose()?
            .unwrap_or(0);
        let gate_reached = request
            .meta
            .get("gate_reached")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if gate_reached.chars().count() > 256 {
            return Err(invalid_input("research gate marker exceeds its text bound"));
        }
        Ok(Self {
            paper_hash: request.paper_hash.clone(),
            lang_key: request.lang_key.clone(),
            direction,
            created_at: request.created_at,
            accepted,
            rejected,
            gate_reached,
            degraded: request
                .meta
                .get("degraded")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            kind,
            document_bytes,
        })
    }

    fn validate(&self) -> io::Result<()> {
        if self.paper_hash.is_empty()
            || self.paper_hash.chars().count() > 128
            || !(self.lang_key.starts_with("survey:") || self.lang_key.starts_with("ideate:"))
            || self.lang_key.chars().count() > 64
            || self.direction.chars().count() > 4096
            || self.kind.chars().count() > 64
            || self.gate_reached.chars().count() > 256
            || self.document_bytes > MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES
        {
            return Err(invalid_data("research direction index is malformed"));
        }
        Ok(())
    }
}

fn read_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    lang_key: &str,
) -> io::Result<Option<DirectionIndexValue>> {
    let Some(raw) = database.entity_get(
        transaction,
        &artifact_key(
            transaction,
            RESEARCH_ARTIFACT_STATE_NAMESPACE,
            paper_hash,
            lang_key,
        )?,
    )?
    else {
        return Ok(None);
    };
    let state: DirectionIndexValue = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("research artifact state is malformed"))?;
    state.validate()?;
    if state.paper_hash != paper_hash || state.lang_key != lang_key {
        return Err(invalid_data("research artifact state identity differs"));
    }
    Ok(Some(state))
}

fn artifact_upsert(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ArtifactUpsertRequest,
) -> io::Result<Vec<u8>> {
    let prior = read_state(
        database,
        transaction,
        &request.paper_hash,
        &request.lang_key,
    )?;
    if let Some(prior) = &prior {
        database.entity_delete(
            transaction,
            created_index_key(
                transaction,
                prior.created_at,
                &prior.paper_hash,
                &prior.lang_key,
            )?,
        )?;
    } else {
        let count_key = owner_key(transaction, RESEARCH_ARTIFACT_COUNT_NAMESPACE, COUNT_KEY)?;
        let count = decode_count(database.entity_get(transaction, &count_key)?)?;
        if count >= MAX_RESEARCH_ARTIFACTS_PER_OWNER as u64 {
            return Err(resource_exhausted(
                "research artifact owner row bound is exhausted",
            ));
        }
        database.entity_put(transaction, count_key, (count + 1).to_le_bytes().to_vec())?;
    }

    let document = ArtifactDocument {
        paper_hash: request.paper_hash.clone(),
        lang_key: request.lang_key.clone(),
        report: request.report.clone(),
        model: request.model.clone(),
        meta: request.meta.clone(),
        created_at: request.created_at,
    };
    let document_json = serde_json::to_vec(&document)
        .map_err(|_| invalid_data("research artifact cannot be encoded"))?;
    if document_json.len() > MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES {
        return Err(resource_exhausted(
            "research artifact exceeds its document byte bound",
        ));
    }
    let next_state = DirectionIndexValue::from_request(request, document_json.len())?;
    let logical_key = logical_key(&request.paper_hash, &request.lang_key)?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: artifact_key(
                transaction,
                RESEARCH_ARTIFACT_DOCUMENT_NAMESPACE,
                &request.paper_hash,
                &request.lang_key,
            )?,
            namespace: ARTIFACT_LOGICAL_NAMESPACE.to_owned(),
            logical_key,
            value_json: document_json,
            expected_version: None,
            updated_at_ms: request.updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES,
    )?;
    let state_json = serde_json::to_vec(&next_state)
        .map_err(|_| invalid_data("research artifact state cannot be encoded"))?;
    database.entity_put(
        transaction,
        artifact_key(
            transaction,
            RESEARCH_ARTIFACT_STATE_NAMESPACE,
            &request.paper_hash,
            &request.lang_key,
        )?,
        state_json.clone(),
    )?;
    database.entity_put(
        transaction,
        created_index_key(
            transaction,
            request.created_at,
            &request.paper_hash,
            &request.lang_key,
        )?,
        state_json,
    )?;
    Ok(br#"{"saved":true}"#.to_vec())
}

fn get_artifact(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    lang_key: &str,
) -> io::Result<Option<ArtifactDocument>> {
    let key = artifact_key(
        transaction,
        RESEARCH_ARTIFACT_DOCUMENT_NAMESPACE,
        paper_hash,
        lang_key,
    )?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        ARTIFACT_LOGICAL_NAMESPACE,
        &logical_key(paper_hash, lang_key)?,
        transaction.owner_user_id(),
        MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES,
    )?
    else {
        return Ok(None);
    };
    let document: ArtifactDocument = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("research artifact document is malformed"))?;
    if document.paper_hash != paper_hash || document.lang_key != lang_key {
        return Err(invalid_data("research artifact document identity differs"));
    }
    Ok(Some(document))
}

fn artifacts_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
) -> io::Result<Vec<u8>> {
    let lang_keys = [format!("ideate:{lang}"), format!("survey:{lang}")];
    let mut expected_document_bytes = 4096_usize;
    for lang_key in &lang_keys {
        if let Some(state) = read_state(database, transaction, paper_hash, lang_key)? {
            expected_document_bytes = expected_document_bytes
                .checked_add(state.document_bytes)
                .filter(|bytes| *bytes <= MAX_RESEARCH_RESPONSE_BYTES)
                .ok_or_else(|| resource_exhausted("research response exceeds 64 MiB"))?;
        } else {
            let document_key = artifact_key(
                transaction,
                RESEARCH_ARTIFACT_DOCUMENT_NAMESPACE,
                paper_hash,
                lang_key,
            )?;
            if database.entity_get(transaction, &document_key)?.is_some() {
                return Err(invalid_data("research artifact compact state is missing"));
            }
        }
    }
    let mut rows = Vec::new();
    for lang_key in lang_keys {
        if let Some(document) = get_artifact(database, transaction, paper_hash, &lang_key)? {
            rows.push(json!({
                "lang_key": document.lang_key,
                "report": document.report,
                "meta": document.meta,
            }));
        }
    }
    encode_response(&Value::Array(rows))
}

fn directions_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    limit: usize,
) -> io::Result<Vec<u8>> {
    let scan_limit = limit
        .saturating_mul(2)
        .min(MAX_RESEARCH_DIRECTION_SCAN_ROWS);
    let rows = scan_created_index(database, transaction, scan_limit)?;
    let mut folded = BTreeMap::<(String, String), Value>::new();
    let mut ordered_keys = Vec::new();
    for (_, raw) in rows {
        let indexed: DirectionIndexValue = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("research direction index is malformed"))?;
        indexed.validate()?;
        if indexed.direction.is_empty() {
            continue;
        }
        let lang = indexed
            .lang_key
            .split_once(':')
            .map_or("en", |(_, lang)| lang)
            .to_owned();
        let key = (indexed.paper_hash.clone(), lang.clone());
        if !folded.contains_key(&key) {
            ordered_keys.push(key.clone());
            folded.insert(
                key.clone(),
                json!({
                    "direction": indexed.direction,
                    "lang": lang,
                    "created_at": indexed.created_at,
                    "accepted": 0,
                    "rejected": 0,
                    "gate_reached": "",
                    "degraded": false,
                    "has_survey": false,
                    "has_ideas": false,
                }),
            );
        }
        let item = folded
            .get_mut(&key)
            .and_then(Value::as_object_mut)
            .ok_or_else(|| invalid_data("research direction fold is malformed"))?;
        let current_created = item.get("created_at").and_then(Value::as_u64).unwrap_or(0);
        item.insert(
            "created_at".to_owned(),
            Value::from(current_created.max(indexed.created_at)),
        );
        if indexed.kind == "survey" {
            item.insert("has_survey".to_owned(), Value::Bool(true));
        } else if indexed.kind == "ideate" {
            item.insert("has_ideas".to_owned(), Value::Bool(true));
            item.insert("accepted".to_owned(), Value::from(indexed.accepted));
            item.insert("rejected".to_owned(), Value::from(indexed.rejected));
            item.insert("gate_reached".to_owned(), Value::from(indexed.gate_reached));
            item.insert("degraded".to_owned(), Value::Bool(indexed.degraded));
        }
    }
    let result = ordered_keys
        .into_iter()
        .filter_map(|key| folded.remove(&key))
        .take(limit)
        .collect::<Vec<_>>();
    encode_response(&Value::Array(result))
}

fn workspace_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
) -> io::Result<Option<Vec<u8>>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &workspace_key(transaction, paper_hash, lang)?,
        WORKSPACE_LOGICAL_NAMESPACE,
        &logical_key(paper_hash, lang)?,
        transaction.owner_user_id(),
        MAX_RESEARCH_WORKSPACE_BYTES,
    )
}

// Keeping the complete CAS tuple visible at this boundary makes revision and
// physical-time authority explicit to callers.
#[allow(clippy::too_many_arguments)]
fn workspace_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    paper_hash: &str,
    lang: &str,
    expected_revision: u64,
    updated_at_seconds: u64,
    workspace: &Map<String, Value>,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let key = workspace_key(transaction, paper_hash, lang)?;
    let actual_revision = database
        .entity_get(transaction, &key)?
        .as_deref()
        .map(|stored| {
            versioned_document::stored_document_version(
                stored,
                WORKSPACE_LOGICAL_NAMESPACE,
                &logical_key(paper_hash, lang)?,
            )
        })
        .transpose()?
        .unwrap_or(0);
    if actual_revision != expected_revision {
        return Err(conflict("research workspace revision advanced"));
    }
    let next_revision = actual_revision
        .checked_add(1)
        .ok_or_else(|| invalid_data("research workspace revision overflow"))?;
    let updated_at_ms = updated_at_seconds
        .checked_mul(1000)
        .ok_or_else(|| invalid_input("research workspace timestamp overflow"))?;
    let response = json!({
        "workspace": workspace,
        "revision": next_revision,
        "updated_at_ms": updated_at_ms,
    });
    let value_json = serde_json::to_vec(&response)
        .map_err(|_| invalid_data("research workspace cannot be encoded"))?;
    if value_json.len() > MAX_RESEARCH_WORKSPACE_BYTES {
        return Err(resource_exhausted(
            "research workspace exceeds its document byte bound",
        ));
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key,
            namespace: WORKSPACE_LOGICAL_NAMESPACE.to_owned(),
            logical_key: logical_key(paper_hash, lang)?,
            value_json,
            expected_version: Some(actual_revision),
            updated_at_ms: physical_updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_RESEARCH_WORKSPACE_BYTES,
    )?;
    encode_response(&response)
}

pub fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ResearchRequest,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    match request {
        ResearchRequest::ArtifactUpsert(request) => {
            artifact_upsert(database, transaction, request).map(Some)
        }
        ResearchRequest::ArtifactsGet { paper_hash, lang } => {
            artifacts_get(database, transaction, paper_hash, lang).map(Some)
        }
        ResearchRequest::DirectionsList { limit } => {
            directions_list(database, transaction, *limit).map(Some)
        }
        ResearchRequest::WorkspaceGet { paper_hash, lang } => {
            workspace_get(database, transaction, paper_hash, lang)
        }
        ResearchRequest::WorkspacePut {
            paper_hash,
            lang,
            expected_revision,
            updated_at_seconds,
            workspace,
            physical_updated_at_ms,
        } => workspace_put(
            database,
            transaction,
            paper_hash,
            lang,
            *expected_revision,
            *updated_at_seconds,
            workspace,
            *physical_updated_at_ms,
        )
        .map(Some),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encoded_artifact_identity_is_prefix_safe() {
        assert_ne!(
            identity_bytes("a", "bc").unwrap(),
            identity_bytes("ab", "c").unwrap()
        );
        assert!(identity_bytes("a", "bc").unwrap() < identity_bytes("ab", "c").unwrap());
    }

    #[test]
    fn direction_scan_crosses_the_entity_page_boundary_and_stops_at_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for sequence in 0..=MAX_RESEARCH_DIRECTION_ROWS {
            let state = DirectionIndexValue {
                paper_hash: format!("paper-{sequence:04}"),
                lang_key: "survey:en".to_owned(),
                direction: format!("direction-{sequence:04}"),
                created_at: 10_000 - sequence as u64,
                accepted: 0,
                rejected: 0,
                gate_reached: String::new(),
                degraded: false,
                kind: "survey".to_owned(),
                document_bytes: 1,
            };
            let raw = serde_json::to_vec(&state).unwrap();
            let key =
                created_index_key(&seed, state.created_at, &state.paper_hash, &state.lang_key)
                    .unwrap();
            database.entity_put(&mut seed, key, raw).unwrap();
        }
        database.commit(seed).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let encoded = directions_list(&database, &mut read, MAX_RESEARCH_DIRECTION_ROWS).unwrap();
        let rows: Vec<Value> = serde_json::from_slice(&encoded).unwrap();
        assert_eq!(rows.len(), MAX_RESEARCH_DIRECTION_ROWS);
        assert_eq!(rows[0]["direction"], "direction-0000");
        assert_eq!(rows.last().unwrap()["direction"], "direction-0999");
    }
}
