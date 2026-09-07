//! Payload-size-independent task-result checkpoints, replay, and abort fencing.
//!
//! A compact tenant-global header is the semantic version authority. Full
//! payloads and replay metadata are separate blob-capable documents; owner-
//! local covering summaries serve scans without touching either blob.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::conversation_header::{TaskResultCacheFacts, TaskResultParentSnapshot};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_DOCUMENT_NAMESPACE, MAX_ENTITY_KEY_BYTES, MAX_TASK_RESULT_CACHE_FACT,
    MAX_TASK_RESULT_DOCUMENT_BYTES, TASK_RESULT_CACHE_SETTINGS_CONTRACT,
    TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, TASK_RESULT_COST_EXPERIMENT_NAMESPACE,
    TASK_RESULT_DOCUMENT_NAMESPACE, TASK_RESULT_HEADER_NAMESPACE, TASK_RESULT_LIVE_NAMESPACE,
    TASK_RESULT_REPLAY_NAMESPACE, TASK_RESULT_SUMMARY_NAMESPACE, TASK_RESULT_SUMMARY_PAGE_ROWS,
};
use crate::versioned_document::{self, PutRequest};

const FULL_LOGICAL_NAMESPACE: &str = "task_results";
const REPLAY_LOGICAL_NAMESPACE: &str = "task_result_replay";
const MAX_TASK_ID_CHARACTERS: usize = 512;
const MAX_CONVERSATION_ID_CHARACTERS: usize = 512;
const MAX_STATUS_CHARACTERS: usize = 64;
const MAX_ABORT_SOURCE_CHARACTERS: usize = 128;
const MAX_EXPERIMENT_ID_CHARACTERS: usize = 128;
const MAX_INTERRUPTED_REASON_CHARACTERS: usize = 64;
const DOCUMENT_ENVELOPE_OVERHEAD_BYTES: usize = 4096;
const MAX_COST_EXPERIMENT_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "task result checkpoint conflict")
}

fn text_within(value: &str, maximum: usize, allow_empty: bool) -> bool {
    (allow_empty || !value.is_empty()) && value.chars().count() <= maximum
}

fn encode<T: Serialize>(value: &T, message: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data(message))
}

struct BoundedByteCounter {
    bytes: usize,
    maximum: usize,
}

impl io::Write for BoundedByteCounter {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(buffer.len())
            .filter(|bytes| *bytes <= self.maximum)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "task result cost-experiment response exceeds 8 MiB",
                )
            })?;
        Ok(buffer.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn encoded_size_bounded<T: Serialize>(value: &T, maximum: usize) -> io::Result<usize> {
    let mut counter = BoundedByteCounter { bytes: 0, maximum };
    serde_json::to_writer(&mut counter, value).map_err(|error| {
        if error.is_io() {
            io::Error::new(
                io::ErrorKind::OutOfMemory,
                "task result cost-experiment response exceeds 8 MiB",
            )
        } else {
            invalid_data("task result cost-experiment outcome cannot be encoded")
        }
    })?;
    Ok(counter.bytes)
}

fn scoped_key(
    transaction: &AuthorityTransaction,
    owner_user_id: u64,
    namespace: &str,
    task_id: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        owner_user_id,
        namespace,
        task_id.as_bytes(),
    )
}

fn document_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    scoped_key(
        transaction,
        TENANT_GLOBAL_OWNER_ID,
        TASK_RESULT_DOCUMENT_NAMESPACE,
        task_id,
    )
}

fn header_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    scoped_key(
        transaction,
        TENANT_GLOBAL_OWNER_ID,
        TASK_RESULT_HEADER_NAMESPACE,
        task_id,
    )
}

fn replay_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    scoped_key(
        transaction,
        transaction.owner_user_id(),
        TASK_RESULT_REPLAY_NAMESPACE,
        task_id,
    )
}

fn summary_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    scoped_key(
        transaction,
        transaction.owner_user_id(),
        TASK_RESULT_SUMMARY_NAMESPACE,
        task_id,
    )
}

fn live_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    scoped_key(
        transaction,
        transaction.owner_user_id(),
        TASK_RESULT_LIVE_NAMESPACE,
        task_id,
    )
}

fn cost_index_prefix(experiment_id: &str) -> Vec<u8> {
    let digest: [u8; 32] = Sha256::digest(experiment_id.as_bytes()).into();
    let mut prefix = Vec::with_capacity(33);
    prefix.extend_from_slice(&digest);
    prefix.push(0);
    prefix
}

fn cost_index_key(
    transaction: &AuthorityTransaction,
    experiment_id: &str,
    task_id: &str,
) -> io::Result<EntityKey> {
    let mut key = cost_index_prefix(experiment_id);
    key.extend_from_slice(task_id.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TASK_RESULT_COST_EXPERIMENT_NAMESPACE,
        &key,
    )
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Header {
    task_id: String,
    owner_user_id: u64,
    version: u64,
    updated_at_ms: u64,
    payload_bytes: u64,
    payload_sha256: String,
    conv_id: String,
    status: String,
    created_at: i64,
    completed_at: i64,
    abort_requested_at: u64,
    abort_source: String,
    #[serde(default)]
    interrupted_reason: String,
    #[serde(default)]
    cost_experiment_id: String,
}

impl Header {
    fn validate(&self, task_id: &str) -> io::Result<()> {
        if self.task_id != task_id
            || !text_within(&self.task_id, MAX_TASK_ID_CHARACTERS, false)
            || self.owner_user_id == 0
            || self.version == 0
            || self.updated_at_ms == 0
            || self.payload_bytes > MAX_TASK_RESULT_DOCUMENT_BYTES as u64
            || (!self.payload_sha256.is_empty()
                && (self.payload_sha256.len() != 64
                    || !self
                        .payload_sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit())))
            || !text_within(&self.conv_id, MAX_CONVERSATION_ID_CHARACTERS, true)
            || !text_within(&self.status, MAX_STATUS_CHARACTERS, true)
            || !text_within(&self.abort_source, MAX_ABORT_SOURCE_CHARACTERS, true)
            || !text_within(
                &self.interrupted_reason,
                MAX_INTERRUPTED_REASON_CHARACTERS,
                true,
            )
            || !text_within(&self.cost_experiment_id, MAX_EXPERIMENT_ID_CHARACTERS, true)
        {
            return Err(invalid_data("task result header is malformed"));
        }
        Ok(())
    }

    fn summary(&self) -> Summary {
        Summary {
            key: self.task_id.clone(),
            task_id: self.task_id.clone(),
            conv_id: self.conv_id.clone(),
            user_id: self.owner_user_id,
            status: self.status.clone(),
            created_at: self.created_at,
            completed_at: self.completed_at,
            version: self.version,
            updated_at_ms: self.updated_at_ms,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ReplayProjection {
    metadata: Value,
    error: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Summary {
    key: String,
    task_id: String,
    conv_id: String,
    user_id: u64,
    status: String,
    created_at: i64,
    completed_at: i64,
    version: u64,
    updated_at_ms: u64,
}

impl Summary {
    fn validate(&self) -> io::Result<()> {
        if self.key != self.task_id
            || !text_within(&self.task_id, MAX_TASK_ID_CHARACTERS, false)
            || !text_within(&self.conv_id, MAX_CONVERSATION_ID_CHARACTERS, true)
            || !text_within(&self.status, MAX_STATUS_CHARACTERS, true)
            || self.user_id == 0
            || self.version == 0
            || self.updated_at_ms == 0
        {
            return Err(invalid_data("task result summary is malformed"));
        }
        Ok(())
    }
}

fn read_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<Option<Header>> {
    let Some(raw) = database.entity_get(transaction, &header_key(transaction, task_id)?)? else {
        return Ok(None);
    };
    let header: Header =
        serde_json::from_slice(&raw).map_err(|_| invalid_data("task result header is not JSON"))?;
    header.validate(task_id)?;
    Ok(Some(header))
}

fn read_replay_projection(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<ReplayProjection> {
    let replay = versioned_document::get_with_blob_owner_bounded(
        database,
        transaction,
        &replay_key(transaction, task_id)?,
        REPLAY_LOGICAL_NAMESPACE,
        task_id,
        transaction.owner_user_id(),
        MAX_TASK_RESULT_DOCUMENT_BYTES + DOCUMENT_ENVELOPE_OVERHEAD_BYTES,
    )?
    .ok_or_else(|| invalid_data("task result replay projection is missing"))?;
    serde_json::from_slice::<Value>(&replay)
        .ok()
        .and_then(|envelope| envelope.get("value").cloned())
        .and_then(|value| serde_json::from_value(value).ok())
        .ok_or_else(|| invalid_data("task result replay projection is malformed"))
}

fn write_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    header: &Header,
) -> io::Result<()> {
    header.validate(&header.task_id)?;
    database.entity_put(
        transaction,
        header_key(transaction, &header.task_id)?,
        encode(header, "task result header cannot be encoded")?,
    )
}

fn write_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    header: &Header,
) -> io::Result<()> {
    let summary = header.summary();
    summary.validate()?;
    database.entity_put(
        transaction,
        summary_key(transaction, &header.task_id)?,
        encode(&summary, "task result summary cannot be encoded")?,
    )
}

fn value_text(value: Option<&Value>, default: &str) -> io::Result<String> {
    match value {
        None | Some(Value::Null) => Ok(default.to_owned()),
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err(invalid_input("task result text projection is invalid")),
    }
}

fn value_clock(value: Option<&Value>) -> io::Result<i64> {
    match value {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Number(value)) => value
            .as_i64()
            .ok_or_else(|| invalid_input("task result clock is invalid")),
        Some(Value::String(value)) => value
            .parse()
            .map_err(|_| invalid_input("task result clock is invalid")),
        _ => Err(invalid_input("task result clock is invalid")),
    }
}

fn metadata_object(value: Option<&Value>) -> Value {
    match value {
        Some(Value::Object(value)) => Value::Object(value.clone()),
        Some(Value::String(value)) => serde_json::from_str::<Value>(value)
            .ok()
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({})),
        _ => json!({}),
    }
}

fn cost_experiment(metadata: &Value) -> Option<(&Map<String, Value>, String)> {
    let outcome = metadata.as_object()?.get("costExperiment")?.as_object()?;
    if outcome.is_empty() {
        return None;
    }
    let experiment_id = outcome
        .get("experimentId")
        .or_else(|| outcome.get("experiment_id"))?
        .as_str()?
        .to_owned();
    text_within(&experiment_id, MAX_EXPERIMENT_ID_CHARACTERS, false)
        .then_some((outcome, experiment_id))
}

fn write_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    previous: Option<&Header>,
    header: &Header,
) -> io::Result<()> {
    if let Some(previous) = previous {
        if !previous.cost_experiment_id.is_empty()
            && previous.cost_experiment_id != header.cost_experiment_id
        {
            database.entity_delete(
                transaction,
                cost_index_key(transaction, &previous.cost_experiment_id, &header.task_id)?,
            )?;
        }
    }
    if matches!(header.status.as_str(), "pending" | "running") {
        database.entity_put(
            transaction,
            live_key(transaction, &header.task_id)?,
            header.owner_user_id.to_le_bytes().to_vec(),
        )?;
    } else {
        database.entity_delete(transaction, live_key(transaction, &header.task_id)?)?;
    }
    if !header.cost_experiment_id.is_empty() {
        database.entity_put(
            transaction,
            cost_index_key(transaction, &header.cost_experiment_id, &header.task_id)?,
            Vec::new(),
        )?;
    }
    Ok(())
}

fn payload_digest(value_json: &[u8]) -> String {
    let digest: [u8; 32] = Sha256::digest(value_json).into();
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn checkpoint_response(task_id: &str, version: u64, updated_at_ms: u64) -> io::Result<Vec<u8>> {
    encode(
        &json!({"key": task_id, "version": version, "updated_at_ms": updated_at_ms}),
        "task result checkpoint response failed",
    )
}

pub(crate) struct CheckpointGuard {
    pub require_parent: bool,
    pub cache_prefix_hwm: Option<u64>,
    pub last_turn_cache_read: Option<u64>,
}

impl CheckpointGuard {
    fn cache_settings_requested(&self) -> bool {
        self.cache_prefix_hwm.is_some() || self.last_turn_cache_read.is_some()
    }

    fn cache_facts(&self) -> TaskResultCacheFacts {
        TaskResultCacheFacts {
            cache_prefix_hwm: self.cache_prefix_hwm,
            last_turn_cache_read: self.last_turn_cache_read,
        }
    }
}

fn guarded_checkpoint_response(
    task_id: &str,
    version: u64,
    updated_at_ms: u64,
    owned: bool,
    guard: &CheckpointGuard,
    cache_settings_committed: bool,
    cache_facts: Option<TaskResultCacheFacts>,
) -> io::Result<Vec<u8>> {
    let mut response = json!({
        "key": task_id,
        "version": version,
        "updated_at_ms": updated_at_ms,
        "owned": owned,
        "guard_contract": TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    });
    if guard.cache_settings_requested() {
        response["cache_settings_contract"] = json!(TASK_RESULT_CACHE_SETTINGS_CONTRACT);
        response["cache_settings_committed"] = json!(cache_settings_committed);
        if let Some(facts) = cache_facts.filter(|_| cache_settings_committed) {
            if let Some(value) = facts.cache_prefix_hwm {
                response["cache_prefix_hwm"] = json!(value);
            }
            if let Some(value) = facts.last_turn_cache_read {
                response["last_turn_cache_read"] = json!(value);
            }
        }
    }
    encode(&response, "guarded task result checkpoint response failed")
}

pub(crate) fn checkpoint(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    mut value_json: Vec<u8>,
    expected_version: u64,
    updated_at_ms: u64,
    guard: Option<&CheckpointGuard>,
) -> io::Result<Vec<u8>> {
    if !text_within(task_id, MAX_TASK_ID_CHARACTERS, false)
        || value_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES
        || updated_at_ms == 0
    {
        return Err(invalid_input("invalid task result checkpoint"));
    }
    let mut incoming: Map<String, Value> = serde_json::from_slice::<Value>(&value_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("task result checkpoint is not an object"))?;
    if incoming.get("user_id").and_then(Value::as_u64) != Some(transaction.owner_user_id()) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "task result checkpoint owner differs from authority scope",
        ));
    }
    if incoming
        .get("task_id")
        .is_some_and(|value| value.as_str() != Some(task_id))
    {
        return Err(invalid_input("task result checkpoint identity differs"));
    }

    let mut parent_snapshot: Option<TaskResultParentSnapshot> = None;
    if let Some(guard) = guard {
        if incoming.get("task_id").and_then(Value::as_str) != Some(task_id) {
            return Err(invalid_input(
                "guarded task result checkpoint identity differs",
            ));
        }
        for fact in [guard.cache_prefix_hwm, guard.last_turn_cache_read]
            .into_iter()
            .flatten()
        {
            if !(1..=MAX_TASK_RESULT_CACHE_FACT).contains(&fact) {
                return Err(invalid_input("invalid task result cache fact"));
            }
        }
        let conv_id = value_text(incoming.get("conv_id"), "")?;
        if guard.require_parent {
            if !text_within(&conv_id, MAX_CONVERSATION_ID_CHARACTERS, false) {
                return Err(invalid_input(
                    "guarded task result checkpoint requires a parent",
                ));
            }
            if guard.cache_settings_requested() {
                parent_snapshot = crate::conversation_header::task_result_parent_snapshot(
                    database,
                    transaction,
                    &conv_id,
                )?;
                if parent_snapshot.is_none() {
                    return guarded_checkpoint_response(task_id, 0, 0, false, guard, false, None);
                }
            } else if !crate::conversation_header::is_active(database, transaction, &conv_id)? {
                return guarded_checkpoint_response(task_id, 0, 0, false, guard, false, None);
            }
        }
    }

    let current = read_header(database, transaction, task_id)?;
    if let Some(header) = &current {
        if header.owner_user_id != transaction.owner_user_id() {
            if let Some(guard) = guard {
                return guarded_checkpoint_response(task_id, 0, 0, false, guard, false, None);
            }
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "task result checkpoint owner differs from authority scope",
            ));
        }
        let incoming_status = value_text(incoming.get("status"), "")?;
        if let Some(guard) = guard {
            if header.status == "interrupted"
                || matches!(incoming_status.as_str(), "pending" | "running")
                    && !matches!(header.status.as_str(), "" | "pending" | "running")
            {
                return guarded_checkpoint_response(task_id, 0, 0, false, guard, false, None);
            }
        }
        if header.abort_requested_at != 0 {
            incoming.insert(
                "abort_requested_at".to_owned(),
                json!(header.abort_requested_at),
            );
            incoming.insert("abort_source".to_owned(), json!(header.abort_source));
            value_json = serde_json::to_vec(&Value::Object(incoming.clone()))
                .map_err(|_| invalid_input("task result checkpoint cannot be encoded"))?;
            if value_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES {
                return Err(invalid_input(
                    "task result checkpoint plus abort tombstone exceeds 64 MiB",
                ));
            }
        }
        let digest = payload_digest(&value_json);
        if !header.payload_sha256.is_empty()
            && header.payload_sha256 == digest
            && header.payload_bytes == value_json.len() as u64
        {
            if let Some(guard) = guard {
                let cache_facts = if guard.cache_settings_requested() {
                    Some(crate::conversation_header::merge_task_result_cache_facts(
                        database,
                        transaction,
                        parent_snapshot.take().ok_or_else(|| {
                            invalid_data("task result parent snapshot is missing")
                        })?,
                        guard.cache_facts(),
                        true,
                        updated_at_ms,
                    )?)
                } else {
                    None
                };
                return guarded_checkpoint_response(
                    task_id,
                    header.version,
                    header.updated_at_ms,
                    true,
                    guard,
                    guard.cache_settings_requested(),
                    cache_facts,
                );
            }
            return checkpoint_response(task_id, header.version, header.updated_at_ms);
        }
        if header.version != expected_version {
            return Err(conflict());
        }
    } else if expected_version != 0 {
        return Err(conflict());
    }

    let conv_id = value_text(incoming.get("conv_id"), "")?;
    let status = value_text(incoming.get("status"), "")?;
    if !text_within(&conv_id, MAX_CONVERSATION_ID_CHARACTERS, true)
        || !text_within(&status, MAX_STATUS_CHARACTERS, true)
    {
        return Err(invalid_input("task result summary projection is too large"));
    }
    let version = current.as_ref().map_or(Ok(1), |header| {
        header
            .version
            .checked_add(1)
            .ok_or_else(|| invalid_data("task result version overflow"))
    })?;
    let metadata = metadata_object(incoming.get("metadata"));
    let cost_experiment_id = cost_experiment(&metadata)
        .map(|(_, experiment_id)| experiment_id)
        .unwrap_or_default();
    let replay_json = encode(
        &ReplayProjection {
            metadata,
            error: incoming.get("error").cloned().unwrap_or(Value::Null),
        },
        "task result replay projection cannot be encoded",
    )?;
    if replay_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES {
        return Err(invalid_input(
            "task result replay projection exceeds 64 MiB",
        ));
    }
    let header = Header {
        task_id: task_id.to_owned(),
        owner_user_id: transaction.owner_user_id(),
        version,
        updated_at_ms,
        payload_bytes: value_json.len() as u64,
        payload_sha256: payload_digest(&value_json),
        conv_id,
        status,
        created_at: value_clock(incoming.get("created_at"))?,
        completed_at: value_clock(incoming.get("completed_at"))?,
        abort_requested_at: current
            .as_ref()
            .map_or(0, |header| header.abort_requested_at),
        abort_source: current
            .as_ref()
            .map_or_else(String::new, |header| header.abort_source.clone()),
        interrupted_reason: current
            .as_ref()
            .map_or_else(String::new, |header| header.interrupted_reason.clone()),
        cost_experiment_id,
    };

    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, task_id)?,
            namespace: FULL_LOGICAL_NAMESPACE.to_owned(),
            logical_key: task_id.to_owned(),
            value_json,
            expected_version: None,
            updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_TASK_RESULT_DOCUMENT_BYTES,
    )?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: replay_key(transaction, task_id)?,
            namespace: REPLAY_LOGICAL_NAMESPACE.to_owned(),
            logical_key: task_id.to_owned(),
            value_json: replay_json,
            expected_version: None,
            updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_TASK_RESULT_DOCUMENT_BYTES,
    )?;
    write_header(database, transaction, &header)?;
    write_summary(database, transaction, &header)?;
    write_indexes(database, transaction, current.as_ref(), &header)?;
    if let Some(guard) = guard {
        let cache_facts = if guard.cache_settings_requested() {
            Some(crate::conversation_header::merge_task_result_cache_facts(
                database,
                transaction,
                parent_snapshot
                    .take()
                    .ok_or_else(|| invalid_data("task result parent snapshot is missing"))?,
                guard.cache_facts(),
                false,
                updated_at_ms,
            )?)
        } else {
            None
        };
        return guarded_checkpoint_response(
            task_id,
            version,
            updated_at_ms,
            true,
            guard,
            guard.cache_settings_requested(),
            cache_facts,
        );
    }
    checkpoint_response(task_id, version, updated_at_ms)
}

pub(crate) fn replay_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    requested_user_id: u64,
    include_terminal_payload: bool,
    include_metadata: bool,
) -> io::Result<Option<Vec<u8>>> {
    if requested_user_id != transaction.owner_user_id() {
        return Ok(None);
    }
    let Some(header) = read_header(database, transaction, task_id)? else {
        return Ok(None);
    };
    if header.owner_user_id != transaction.owner_user_id() {
        return Ok(None);
    }
    let replay = read_replay_projection(database, transaction, task_id)?;
    let metadata = if include_metadata {
        replay.metadata
    } else {
        let source = replay.metadata.as_object();
        Value::Object(
            ["finishReason", "model", "requestId"]
                .into_iter()
                .filter_map(|field| {
                    source
                        .and_then(|metadata| metadata.get(field))
                        .cloned()
                        .map(|value| (field.to_owned(), value))
                })
                .collect(),
        )
    };
    let mut result = json!({
        "task_id": task_id,
        "conv_id": header.conv_id,
        "user_id": header.owner_user_id,
        "status": if header.status.is_empty() { "running" } else { &header.status },
        "error": replay.error,
        "metadata": metadata,
        "created_at": header.created_at,
        "completed_at": header.completed_at,
        "version": header.version,
        "updated_at_ms": header.updated_at_ms,
    });
    if include_terminal_payload {
        let full = versioned_document::get_with_blob_owner_bounded(
            database,
            transaction,
            &document_key(transaction, task_id)?,
            FULL_LOGICAL_NAMESPACE,
            task_id,
            TENANT_GLOBAL_OWNER_ID,
            MAX_TASK_RESULT_DOCUMENT_BYTES + DOCUMENT_ENVELOPE_OVERHEAD_BYTES,
        )?
        .ok_or_else(|| invalid_data("task result payload is missing"))?;
        let value: Value = serde_json::from_slice::<Value>(&full)
            .ok()
            .and_then(|envelope| envelope.get("value").cloned())
            .ok_or_else(|| invalid_data("task result payload is malformed"))?;
        result["content"] = value.get("content").cloned().unwrap_or(json!(""));
        result["thinking"] = value.get("thinking").cloned().unwrap_or(json!(""));
    }
    encode(&result, "task result replay response cannot be encoded").map(Some)
}

pub(crate) fn abort(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    requested_user_id: u64,
    source: &str,
    requested_at_ms: u64,
) -> io::Result<Vec<u8>> {
    if requested_user_id != transaction.owner_user_id() {
        return encode(
            &json!({"signaled": false, "changed": false}),
            "abort response failed",
        );
    }
    let Some(mut header) = read_header(database, transaction, task_id)? else {
        return encode(
            &json!({"signaled": false, "changed": false}),
            "abort response failed",
        );
    };
    if header.owner_user_id != transaction.owner_user_id()
        || !matches!(header.status.as_str(), "pending" | "running")
    {
        return encode(
            &json!({"signaled": false, "changed": false}),
            "abort response failed",
        );
    }
    if header.abort_requested_at != 0 {
        return encode(
            &json!({"signaled": true, "changed": false}),
            "abort response failed",
        );
    }
    if !text_within(source, MAX_ABORT_SOURCE_CHARACTERS, false) || requested_at_ms == 0 {
        return Err(invalid_input("invalid task result abort"));
    }
    header.version = header
        .version
        .checked_add(1)
        .ok_or_else(|| invalid_data("task result version overflow"))?;
    header.updated_at_ms = requested_at_ms;
    header.abort_requested_at = requested_at_ms;
    header.abort_source = source.to_owned();
    header.payload_sha256.clear();
    write_header(database, transaction, &header)?;
    write_summary(database, transaction, &header)?;
    encode(
        &json!({"signaled": true, "changed": true, "version": header.version,
                "requested_at_ms": requested_at_ms}),
        "abort response failed",
    )
}

pub(crate) fn abort_requested(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    requested_user_id: u64,
) -> io::Result<Vec<u8>> {
    let requested = requested_user_id == transaction.owner_user_id()
        && read_header(database, transaction, task_id)?.is_some_and(|header| {
            header.owner_user_id == transaction.owner_user_id() && header.abort_requested_at != 0
        });
    encode(
        &json!({"requested": requested}),
        "abort-requested response failed",
    )
}

pub(crate) struct RecoverRunningRequest {
    pub interrupted_reason: String,
    pub maximum_rows: usize,
    pub scan_limit: usize,
    pub updated_at_ms: u64,
}

pub(crate) fn recover_running(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RecoverRunningRequest,
) -> io::Result<Vec<u8>> {
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TASK_RESULT_LIVE_NAMESPACE,
        b"",
    )?;
    let mut recovered = Vec::new();
    let mut scanned = 0_usize;
    let mut last_task_id = String::new();
    let mut exhausted = false;
    while scanned < request.scan_limit && recovered.len() < request.maximum_rows {
        let page_limit = (request.scan_limit - scanned)
            .min(request.maximum_rows - recovered.len())
            .min(TASK_RESULT_SUMMARY_PAGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() {
            exhausted = true;
            break;
        }
        for (key, owner_bytes) in &page {
            scanned += 1;
            if owner_bytes.as_slice() != transaction.owner_user_id().to_le_bytes() {
                return Err(invalid_data("task result live index owner is malformed"));
            }
            let task_id = std::str::from_utf8(key.key_bytes())
                .map_err(|_| invalid_data("task result live index key is malformed"))?;
            let mut header = read_header(database, transaction, task_id)?
                .ok_or_else(|| invalid_data("task result live index header is missing"))?;
            if header.owner_user_id != transaction.owner_user_id()
                || !matches!(header.status.as_str(), "pending" | "running")
            {
                return Err(invalid_data("task result live index is stale"));
            }
            header.version = header
                .version
                .checked_add(1)
                .ok_or_else(|| invalid_data("task result version overflow"))?;
            header.updated_at_ms = request.updated_at_ms;
            header.status = "interrupted".to_owned();
            header.interrupted_reason = request.interrupted_reason.clone();
            if header.completed_at == 0 {
                header.completed_at = i64::try_from(request.updated_at_ms)
                    .map_err(|_| invalid_input("task result recovery clock is invalid"))?;
            }
            header.payload_sha256.clear();
            write_header(database, transaction, &header)?;
            write_summary(database, transaction, &header)?;
            database.entity_delete(transaction, live_key(transaction, task_id)?)?;
            recovered.push(json!({
                "taskId": header.task_id,
                "conversationId": header.conv_id,
            }));
            last_task_id = task_id.to_owned();
        }
        let last_key = page
            .last()
            .expect("a nonempty task-result live page has a last key")
            .0
            .key_bytes();
        cursor = start_after(transaction, TASK_RESULT_LIVE_NAMESPACE, last_key)?;
        if page.len() < page_limit {
            exhausted = true;
            break;
        }
    }
    encode(
        &json!({
            "recovered": recovered,
            "scanned": scanned,
            "nextKey": last_task_id,
            "remaining": !exhausted,
        }),
        "task result recovery response cannot be encoded",
    )
}

pub(crate) struct CostExperimentScanRequest {
    pub requested_user_id: u64,
    pub experiment_id: String,
    pub completed_at_gte: i64,
    pub limit: usize,
    pub scan_limit: usize,
    pub after_key: String,
}

pub(crate) fn cost_experiment_scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CostExperimentScanRequest,
) -> io::Result<Vec<u8>> {
    if request.requested_user_id != transaction.owner_user_id() {
        return encode(
            &json!({
                "records": [], "invalid": 0, "scanned": 0, "capped": false,
                "exhausted": true, "next_cursor": "",
            }),
            "task result cost-experiment response cannot be encoded",
        );
    }
    let prefix = cost_index_prefix(&request.experiment_id);
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TASK_RESULT_COST_EXPERIMENT_NAMESPACE,
        &prefix,
    )?;
    let mut logical_start = prefix.clone();
    if !request.after_key.is_empty() {
        logical_start.extend_from_slice(request.after_key.as_bytes());
        logical_start.push(0);
    }
    let cursor = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TASK_RESULT_COST_EXPERIMENT_NAMESPACE,
        &logical_start,
    )?;
    let rows = database.entity_scan(transaction, &cursor, &end, request.scan_limit)?;
    let scanned = rows.len();
    let next_cursor = rows
        .last()
        .and_then(|(key, _)| key.key_bytes().strip_prefix(prefix.as_slice()))
        .and_then(|task_id| std::str::from_utf8(task_id).ok())
        .unwrap_or_default()
        .to_owned();
    let probe = if let Some((last, _)) = rows.last() {
        let next = start_after(
            transaction,
            TASK_RESULT_COST_EXPERIMENT_NAMESPACE,
            last.key_bytes(),
        )?;
        !database
            .entity_scan(transaction, &next, &end, 1)?
            .is_empty()
    } else {
        false
    };
    let mut invalid = 0_usize;
    let mut records = Vec::new();
    let mut response_bytes = 256_usize;
    for (key, _) in rows {
        let Some(task_bytes) = key.key_bytes().strip_prefix(prefix.as_slice()) else {
            invalid += 1;
            continue;
        };
        let Ok(task_id) = std::str::from_utf8(task_bytes) else {
            invalid += 1;
            continue;
        };
        let Some(header) = read_header(database, transaction, task_id)? else {
            invalid += 1;
            continue;
        };
        if header.owner_user_id != transaction.owner_user_id()
            || header.cost_experiment_id != request.experiment_id
            || header.updated_at_ms < request.completed_at_gte as u64
            || header.completed_at < request.completed_at_gte
        {
            continue;
        }
        let replay = match read_replay_projection(database, transaction, task_id) {
            Ok(replay) => replay,
            Err(_) => {
                invalid += 1;
                continue;
            }
        };
        let Some((outcome, exact_experiment_id)) = cost_experiment(&replay.metadata) else {
            invalid += 1;
            continue;
        };
        if exact_experiment_id != request.experiment_id {
            continue;
        }
        let conversation_key = EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            CONVERSATION_DOCUMENT_NAMESPACE,
            header.conv_id.as_bytes(),
        )?;
        if header.conv_id.is_empty()
            || database
                .entity_get(transaction, &conversation_key)?
                .is_none()
        {
            continue;
        }
        let remaining_bytes = MAX_COST_EXPERIMENT_RESPONSE_BYTES
            .checked_sub(response_bytes)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "task result cost-experiment response exceeds 8 MiB",
                )
            })?;
        let outcome_bytes = encoded_size_bounded(outcome, remaining_bytes)?;
        response_bytes = response_bytes
            .checked_add(outcome_bytes)
            .and_then(|bytes| bytes.checked_add(task_id.len()))
            .and_then(|bytes| bytes.checked_add(header.conv_id.len()))
            .and_then(|bytes| bytes.checked_add(128))
            .filter(|bytes| *bytes <= MAX_COST_EXPERIMENT_RESPONSE_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "task result cost-experiment response exceeds 8 MiB",
                )
            })?;
        records.push(json!({
            "task_id": task_id,
            "conv_id": header.conv_id,
            "completed_at": header.completed_at,
            "outcome": outcome,
        }));
    }
    records.sort_by(|left, right| {
        let left_key = (
            left["completed_at"].as_i64().unwrap_or_default(),
            left["task_id"].as_str().unwrap_or_default(),
        );
        let right_key = (
            right["completed_at"].as_i64().unwrap_or_default(),
            right["task_id"].as_str().unwrap_or_default(),
        );
        right_key.cmp(&left_key)
    });
    let capped = records.len() > request.limit;
    records.truncate(request.limit);
    encode(
        &json!({
            "records": records,
            "invalid": invalid,
            "scanned": scanned,
            "capped": capped,
            "exhausted": !probe,
            "next_cursor": if probe { next_cursor } else { String::new() },
        }),
        "task result cost-experiment response cannot be encoded",
    )
}

pub(crate) struct SummaryListRequest {
    pub requested_user_id: Option<u64>,
    pub status: Option<String>,
    pub conversation_id: Option<String>,
    pub completed_before_ms: Option<i64>,
    pub limit: usize,
    pub scan_limit: usize,
    pub order_by: String,
    pub after_key: String,
}

pub(crate) fn summary_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &SummaryListRequest,
) -> io::Result<Vec<u8>> {
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TASK_RESULT_SUMMARY_NAMESPACE,
        b"",
    )?;
    let mut cursor = if request.after_key.is_empty() {
        EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            TASK_RESULT_SUMMARY_NAMESPACE,
            b"",
        )?
    } else {
        summary_start_after(transaction, request.after_key.as_bytes())?
    };
    let mut rows = Vec::with_capacity(request.scan_limit);
    let mut exhausted = false;
    while rows.len() < request.scan_limit {
        let page_limit = (request.scan_limit - rows.len()).min(TASK_RESULT_SUMMARY_PAGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if page.is_empty() {
            exhausted = true;
            break;
        }
        cursor = summary_start_after(
            transaction,
            page.last()
                .expect("a nonempty summary page has a last key")
                .0
                .key_bytes(),
        )?;
        let page_was_short = page.len() < page_limit;
        rows.extend(page);
        if page_was_short {
            exhausted = true;
            break;
        }
    }
    let capped = !exhausted
        && !database
            .entity_scan(transaction, &cursor, &end, 1)?
            .is_empty();
    let scanned = rows.len();
    let mut invalid = 0_usize;
    let mut summaries = Vec::new();
    for (_, raw) in rows.drain(..) {
        let summary: Summary = match serde_json::from_slice(&raw) {
            Ok(summary) => summary,
            Err(_) => {
                invalid += 1;
                continue;
            }
        };
        if summary.validate().is_err() {
            invalid += 1;
            continue;
        }
        if request
            .requested_user_id
            .is_some_and(|user_id| user_id != summary.user_id)
            || request
                .status
                .as_ref()
                .is_some_and(|status| status != &summary.status)
            || request
                .conversation_id
                .as_ref()
                .is_some_and(|conversation_id| conversation_id != &summary.conv_id)
            || request
                .completed_before_ms
                .is_some_and(|before| summary.completed_at == 0 || summary.completed_at >= before)
        {
            continue;
        }
        summaries.push(summary);
    }
    match request.order_by.as_str() {
        "created_at_desc" => summaries.sort_by(|left, right| {
            (right.created_at, &right.key).cmp(&(left.created_at, &left.key))
        }),
        "completed_at_asc" => summaries.sort_by(|left, right| {
            (left.completed_at, &left.key).cmp(&(right.completed_at, &right.key))
        }),
        "updated_at_asc" => summaries.sort_by(|left, right| {
            (left.updated_at_ms, &left.key).cmp(&(right.updated_at_ms, &right.key))
        }),
        _ => return Err(invalid_input("invalid task result summary order")),
    }
    summaries.truncate(request.limit);
    encode(
        &json!({"records": summaries, "scanned": scanned, "invalid": invalid, "capped": capped}),
        "task result summary response cannot be encoded",
    )
}

fn summary_start_after(
    transaction: &AuthorityTransaction,
    key_bytes: &[u8],
) -> io::Result<EntityKey> {
    start_after(transaction, TASK_RESULT_SUMMARY_NAMESPACE, key_bytes)
}

fn start_after(
    transaction: &AuthorityTransaction,
    namespace: &str,
    key_bytes: &[u8],
) -> io::Result<EntityKey> {
    if key_bytes.len() < MAX_ENTITY_KEY_BYTES {
        let mut exclusive_start = key_bytes.to_vec();
        exclusive_start.push(0);
        EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            namespace,
            &exclusive_start,
        )
    } else {
        Ok(EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            namespace,
            key_bytes,
        )?
        .1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn guarded_checkpoint_parent_witness_rejects_a_concurrent_lifecycle_change() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut setup = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut setup,
            &crate::conversation_header::CreateRequest {
                conversation_id: "parent".to_owned(),
                title: "Parent".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(setup).unwrap();

        let mut checkpoint_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        checkpoint(
            &database,
            &mut checkpoint_transaction,
            "task",
            serde_json::to_vec(&json!({
                "task_id": "task", "user_id": 11, "conv_id": "parent",
                "status": "running",
            }))
            .unwrap(),
            0,
            2,
            Some(&CheckpointGuard {
                require_parent: true,
                cache_prefix_hwm: None,
                last_turn_cache_read: None,
            }),
        )
        .unwrap();

        let mut lifecycle_transaction = database.begin(7, 11).unwrap();
        let parent_key = EntityKey::new(7, 11, CONVERSATION_DOCUMENT_NAMESPACE, b"parent").unwrap();
        database
            .entity_delete(&mut lifecycle_transaction, parent_key)
            .unwrap();
        database.commit(lifecycle_transaction).unwrap();
        assert_eq!(
            database.commit(checkpoint_transaction).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );

        let mut verify = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(read_header(&database, &mut verify, "task")
            .unwrap()
            .is_none());
    }

    #[test]
    fn cost_experiment_scan_rejects_an_oversized_aggregate() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, CONVERSATION_DOCUMENT_NAMESPACE, b"conversation").unwrap(),
                b"present".to_vec(),
            )
            .unwrap();
        checkpoint(
            &database,
            &mut transaction,
            "oversized-outcome",
            serde_json::to_vec(&json!({
                "task_id": "oversized-outcome",
                "user_id": 11,
                "conv_id": "conversation",
                "status": "completed",
                "completed_at": 1_500,
                "metadata": {"costExperiment": {
                    "experimentId": "experiment", "detail": "z".repeat(9 * 1024 * 1024)
                }},
            }))
            .unwrap(),
            0,
            2_000,
            None,
        )
        .unwrap();
        database.commit(transaction).unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let error = cost_experiment_scan(
            &database,
            &mut transaction,
            &CostExperimentScanRequest {
                requested_user_id: 11,
                experiment_id: "experiment".to_owned(),
                completed_at_gte: 0,
                limit: 10,
                scan_limit: 256,
                after_key: String::new(),
            },
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
    }

    #[test]
    fn live_recovery_and_cost_indexes_avoid_full_payload_scans_and_rekey() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, CONVERSATION_DOCUMENT_NAMESPACE, b"conversation").unwrap(),
                b"present".to_vec(),
            )
            .unwrap();
        checkpoint(
            &database,
            &mut transaction,
            "completed-task",
            serde_json::to_vec(&json!({
                "task_id": "completed-task",
                "user_id": 11,
                "conv_id": "conversation",
                "status": "completed",
                "completed_at": 1_500,
                "content": "x".repeat(20_000),
                "metadata": {"costExperiment": {
                    "experimentId": "experiment-a", "arm": "control"
                }},
            }))
            .unwrap(),
            0,
            2_000,
            None,
        )
        .unwrap();
        checkpoint(
            &database,
            &mut transaction,
            "running-task",
            serde_json::to_vec(&json!({
                "task_id": "running-task",
                "user_id": 11,
                "conv_id": "conversation",
                "status": "running",
                "content": "y".repeat(20_000),
            }))
            .unwrap(),
            0,
            2_001,
            None,
        )
        .unwrap();
        database.commit(transaction).unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let running_document_key = document_key(&transaction, "running-task").unwrap();
        let document_before = database
            .entity_get(&mut transaction, &running_document_key)
            .unwrap()
            .unwrap();
        let scan: Value = serde_json::from_slice(
            &cost_experiment_scan(
                &database,
                &mut transaction,
                &CostExperimentScanRequest {
                    requested_user_id: 11,
                    experiment_id: "experiment-a".to_owned(),
                    completed_at_gte: 1_000,
                    limit: 10,
                    scan_limit: 256,
                    after_key: String::new(),
                },
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(scan["scanned"], 1);
        assert_eq!(scan["records"][0]["task_id"], "completed-task");
        let recovered: Value = serde_json::from_slice(
            &recover_running(
                &database,
                &mut transaction,
                &RecoverRunningRequest {
                    interrupted_reason: "server_restart".to_owned(),
                    maximum_rows: 32,
                    scan_limit: 10_000,
                    updated_at_ms: 3_000,
                },
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(recovered["recovered"][0]["taskId"], "running-task");
        assert_eq!(recovered["remaining"], false);
        database.commit(transaction).unwrap();

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let running_document_key = document_key(&transaction, "running-task").unwrap();
        assert_eq!(
            database
                .entity_get(&mut transaction, &running_document_key)
                .unwrap()
                .unwrap(),
            document_before
        );
        let replay: Value = serde_json::from_slice(
            &replay_get(
                &database,
                &mut transaction,
                "running-task",
                11,
                false,
                false,
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(replay["status"], "interrupted");
        assert_eq!(replay["version"], 2);

        checkpoint(
            &database,
            &mut transaction,
            "completed-task",
            serde_json::to_vec(&json!({
                "task_id": "completed-task",
                "user_id": 11,
                "conv_id": "conversation",
                "status": "completed",
                "completed_at": 2_500,
                "metadata": {"costExperiment": {
                    "experimentId": "experiment-b", "arm": "treatment"
                }},
            }))
            .unwrap(),
            1,
            3_100,
            None,
        )
        .unwrap();
        let old_scan: Value = serde_json::from_slice(
            &cost_experiment_scan(
                &database,
                &mut transaction,
                &CostExperimentScanRequest {
                    requested_user_id: 11,
                    experiment_id: "experiment-a".to_owned(),
                    completed_at_gte: 0,
                    limit: 10,
                    scan_limit: 256,
                    after_key: String::new(),
                },
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(old_scan["records"], json!([]));
        assert_eq!(old_scan["scanned"], 0);
    }

    #[test]
    fn summary_scan_crosses_the_one_thousand_row_entity_page_bound() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..=1_000 {
            let task_id = format!("task-{index:04}");
            write_summary(
                &database,
                &mut transaction,
                &Header {
                    task_id,
                    owner_user_id: 11,
                    version: 1,
                    updated_at_ms: 1,
                    payload_bytes: 2,
                    payload_sha256: "0".repeat(64),
                    conv_id: "conversation".to_owned(),
                    status: "running".to_owned(),
                    created_at: index,
                    completed_at: 0,
                    abort_requested_at: 0,
                    abort_source: String::new(),
                    interrupted_reason: String::new(),
                    cost_experiment_id: String::new(),
                },
            )
            .unwrap();
        }
        database.commit(transaction).unwrap();

        let mut transaction = database.begin(7, 11).unwrap();
        let response = summary_list(
            &database,
            &mut transaction,
            &SummaryListRequest {
                requested_user_id: Some(11),
                status: None,
                conversation_id: Some("conversation".to_owned()),
                completed_before_ms: None,
                limit: 1_000,
                scan_limit: 10_000,
                order_by: "created_at_desc".to_owned(),
                after_key: String::new(),
            },
        )
        .unwrap();
        let response: Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(response["scanned"], 1_001);
        assert_eq!(response["records"].as_array().unwrap().len(), 1_000);
        assert_eq!(response["records"][0]["key"], "task-1000");
        assert_eq!(response["capped"], false);
    }
}
