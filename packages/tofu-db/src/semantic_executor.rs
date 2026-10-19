//! Default-deny compilation of admitted storage operations into Transaction IR.
//!
//! This first executable domain preserves the three desktop egress-preference
//! operations from storage.v1. Payload validation, owner binding, canonical
//! response projection, receipts, and logical replay capture happen above the
//! physical authority; all database effects flow through `TransactionIr`.

use crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES;
use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::time::{Duration, Instant};

use base64::Engine as _;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::entity::EntityKey;
use crate::generated_storage_operations::{STORAGE_REGISTRY_VERSION, STORAGE_SCHEMA_VERSION};
use crate::generated_tofudb_ir::{
    DESKTOP_PREFERENCE_NAMESPACE, EVENT_FORMAT_VERSION, EXECUTABLE_STORAGE_OPERATIONS,
    MAX_DESKTOP_AGENT_ID_CHARACTERS, MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS,
    MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS,
    MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS,
    MAX_MODEL_ROUTING_TIMESTAMP_SECONDS, MAX_RECENT_PROJECT_PATH_CHARACTERS,
    MAX_TASK_RESULT_CACHE_FACT,
    MAX_TASK_RESULT_COST_EXPERIMENT_ROWS, MAX_TASK_RESULT_COST_EXPERIMENT_SCAN_ROWS,
    MAX_TASK_RESULT_DOCUMENT_BYTES, MAX_TASK_RESULT_RECOVERY_ROWS,
    MAX_TASK_RESULT_RECOVERY_SCAN_ROWS, MAX_TASK_RESULT_SUMMARY_ROWS,
    MAX_TASK_RESULT_SUMMARY_SCAN_ROWS, MAX_TENANT_USER_LIST_ROWS, MAX_TENANT_USER_LIST_SCAN_ROWS,
    MAX_WORKER_JOB_CLAIM_KINDS, MAX_WORKER_JOB_CLOCK_MILLISECONDS, MAX_WORKER_JOB_ERROR_BYTES,
    MAX_WORKER_JOB_FUTURE_CLOCK_SKEW_MILLISECONDS, MAX_WORKER_JOB_PAYLOAD_BYTES,
    MAX_WORKER_JOB_PRIORITY, RECORD_DOCUMENT_NAMESPACE, TASK_RESULT_CACHE_SETTINGS_CONTRACT,
    TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
};
use crate::semantic::AdmittedRequest;
use crate::transaction_ir::{
    EntityWriteCondition, IndexedStreamAppendItem, IndexedStreamRetentionClass, TransactionIr,
    TransactionIrCommandEffects, TransactionIrResult, TransactionIrStep,
};
use crate::turn_search_projection::{ConversationSearchRequest, TurnSearchProjection};

#[path = "semantic_search_backfill.rs"]
pub(crate) mod search_backfill;

#[path = "semantic_search_backfill_global.rs"]
pub(crate) mod search_backfill_global;

#[path = "semantic_project_dispatch.rs"]
mod project_dispatch;

const DESKTOP_PREFERENCE_KEY: &[u8] = b"preference";

// Resume-operation (continue/checkpoint_resume/answer_guidance) contextSnapshot
// rebind budget — mirrors the sidecar's _CTX_SNAPSHOT_MAX_BYTES.

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SemanticExecutionError {
    InvalidPayload,
    DiagnosticInvalidPayload,
    DiagnosticUnsupported,
    NotFound,
    AuthorityScopeMismatch,
    DeadlineElapsed,
    UnsupportedOperation,
    Conflict,
    TurnInProgress,
    TurnLaneAdvanced,
    TurnParentInvalid,
    TurnProjectionStale,
    TurnSupersededByHuman,
    /// Deterministic corruption fence. The payload is a bounded diagnostic
    /// naming the exact fence that fired; it travels as the response
    /// `error_message` while `error_code` stays `database_integrity`, so
    /// clients classify on the code and never parse this text.
    Integrity(String),
    StorageUnavailable,
    PayloadTooLarge,
    ResourceExhausted,
    CapacityBusy,
    PluginStorageIncompatible,
}

impl SemanticExecutionError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::InvalidPayload => "database_protocol_error",
            Self::DiagnosticInvalidPayload => "database_invalid_payload",
            Self::DiagnosticUnsupported => "database_unsupported",
            Self::NotFound => "database_not_found",
            Self::AuthorityScopeMismatch => "authority_scope_mismatch",
            Self::DeadlineElapsed => "deadline_elapsed",
            Self::UnsupportedOperation => "operation_not_implemented",
            Self::Conflict => "database_conflict",
            Self::TurnInProgress => "turn_in_progress",
            Self::TurnLaneAdvanced => "turn_lane_advanced",
            Self::TurnParentInvalid => "turn_parent_invalid",
            Self::TurnProjectionStale => "turn_projection_stale",
            Self::TurnSupersededByHuman => "turn_superseded_by_human",
            Self::Integrity(_) => "database_integrity",
            Self::StorageUnavailable => "database_unavailable",
            Self::PayloadTooLarge => "storage_payload_too_large",
            Self::ResourceExhausted => "database_resource_exhausted",
            Self::CapacityBusy => "database_busy",
            Self::PluginStorageIncompatible => "plugin_storage_incompatible",
        }
    }

    pub fn retryable(&self) -> bool {
        matches!(
            self,
            Self::StorageUnavailable | Self::ResourceExhausted | Self::CapacityBusy
        )
    }
}

/// Cap one integrity diagnostic for the wire: a single line within the
/// protocol error-message budget, so the response encoder never rejects an
/// error frame of its own. The cap counts BYTES: multibyte input must stay
/// under `MAX_ERROR_MESSAGE_BYTES` too, and truncation never splits UTF-8.
fn bounded_integrity_message(message: &str) -> String {
    const MAX_INTEGRITY_MESSAGE_BYTES: usize = 400;
    let single_line = message.split_whitespace().collect::<Vec<_>>().join(" ");
    if single_line.len() <= MAX_INTEGRITY_MESSAGE_BYTES {
        return single_line;
    }
    let mut end = MAX_INTEGRITY_MESSAGE_BYTES;
    while end > 0 && !single_line.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}...", &single_line[..end])
}

/// Build the integrity variant with a bounded diagnostic message.
pub(crate) fn integrity_failure(message: impl AsRef<str>) -> SemanticExecutionError {
    SemanticExecutionError::Integrity(bounded_integrity_message(message.as_ref()))
}

impl fmt::Display for SemanticExecutionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for SemanticExecutionError {}

pub(crate) fn map_storage_error(error: io::Error) -> SemanticExecutionError {
    if crate::engine::is_rotation_backpressure(&error) {
        return SemanticExecutionError::CapacityBusy;
    }

    if let Some(kind) = crate::turn::conflict_kind(&error) {
        return match kind {
            crate::turn::TurnConflictKind::InProgress => SemanticExecutionError::TurnInProgress,
            crate::turn::TurnConflictKind::LaneAdvanced => SemanticExecutionError::TurnLaneAdvanced,
            crate::turn::TurnConflictKind::ParentInvalid => {
                SemanticExecutionError::TurnParentInvalid
            }
            crate::turn::TurnConflictKind::ProjectionStale => {
                SemanticExecutionError::TurnProjectionStale
            }
            crate::turn::TurnConflictKind::SupersededByHuman => {
                SemanticExecutionError::TurnSupersededByHuman
            }
        };
    }
    if crate::plugin::is_incompatible(&error) {
        return SemanticExecutionError::PluginStorageIncompatible;
    }
    if error.to_string().starts_with("storage_payload_too_large:") {
        return SemanticExecutionError::PayloadTooLarge;
    }
    match error.kind() {
        io::ErrorKind::InvalidInput => SemanticExecutionError::InvalidPayload,
        io::ErrorKind::NotFound => SemanticExecutionError::NotFound,
        io::ErrorKind::InvalidData => integrity_failure(error.to_string()),
        io::ErrorKind::PermissionDenied => SemanticExecutionError::AuthorityScopeMismatch,
        io::ErrorKind::WouldBlock | io::ErrorKind::AlreadyExists => {
            SemanticExecutionError::Conflict
        }
        io::ErrorKind::OutOfMemory => {
            crate::fsync_stats::note_resource_exhaustion(&error);
            SemanticExecutionError::ResourceExhausted
        },
        io::ErrorKind::TimedOut => SemanticExecutionError::DeadlineElapsed,
        _ => SemanticExecutionError::StorageUnavailable,
    }
}

fn canonical_json(value: &Value) -> Result<Vec<u8>, SemanticExecutionError> {
    serde_json::to_vec(value).map_err(|_| SemanticExecutionError::InvalidPayload)
}

/// Consume the parsed root for every generic compiler. The same Value parser
/// retains numeric, Unicode, depth and duplicate-key admission semantics;
/// transferring the root avoids cloning every nested field before compilation.
fn payload_object(payload: &[u8]) -> Result<Map<String, Value>, SemanticExecutionError> {
    let _decode = crate::fsync_stats::StageTimer::diagnostic(
        crate::fsync_stats::STAGE_SEMANTIC_PAYLOAD_DECODE);
    let value: Value =
        serde_json::from_slice(payload).map_err(|_| SemanticExecutionError::InvalidPayload)?;
    crate::request_profile::work(crate::request_profile::Work::SemanticPayloadDecodedBytes, payload.len() as u64);
    match value {
        Value::Object(payload) => Ok(payload),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn python_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => false,
        Some(Value::Number(value)) => value.as_f64() != Some(0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
        Some(Value::Bool(true)) => true,
    }
}

fn owner_user_id(payload: &Map<String, Value>) -> Result<u64, SemanticExecutionError> {
    payload
        .get("owner_user_id")
        .and_then(Value::as_u64)
        .filter(|owner| *owner > 0)
        .ok_or(SemanticExecutionError::InvalidPayload)
}

fn positive_identity(
    payload: &Map<String, Value>,
    field: &str,
) -> Result<u64, SemanticExecutionError> {
    payload
        .get(field)
        .and_then(Value::as_u64)
        .filter(|identity| *identity > 0)
        .ok_or(SemanticExecutionError::InvalidPayload)
}

fn optional_nonnegative_integer(
    payload: &Map<String, Value>,
    field: &str,
    default: u64,
) -> Result<u64, SemanticExecutionError> {
    match payload.get(field) {
        None => Ok(default),
        Some(value) => value.as_u64().ok_or(SemanticExecutionError::InvalidPayload),
    }
}

fn lossy_nonnegative_integer(value: &Value) -> Option<u64> {
    match value {
        Value::Bool(value) => Some(u64::from(*value)),
        Value::Number(value) => value
            .as_u64()
            .or_else(|| value.as_i64().map(|number| number.max(0) as u64))
            .or_else(|| {
                value.as_f64().and_then(|number| {
                    number.is_finite().then(|| {
                        if number <= 0.0 {
                            0
                        } else if number >= u64::MAX as f64 {
                            u64::MAX
                        } else {
                            number.trunc() as u64
                        }
                    })
                })
            }),
        Value::String(value) => value.trim().parse::<i128>().ok().map(|number| {
            if number <= 0 {
                0
            } else {
                u64::try_from(number).unwrap_or(u64::MAX)
            }
        }),
        _ => None,
    }
}

fn is_python_falsy_json(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(value) => !value,
        Value::Number(value) => value.as_f64() == Some(0.0),
        Value::String(value) => value.is_empty(),
        Value::Array(value) => value.is_empty(),
        Value::Object(value) => value.is_empty(),
    }
}

fn agent_id(payload: &Map<String, Value>) -> Result<&str, SemanticExecutionError> {
    payload
        .get("agent_id")
        .and_then(Value::as_str)
        .filter(|agent| agent.chars().count() <= MAX_DESKTOP_AGENT_ID_CHARACTERS)
        .ok_or(SemanticExecutionError::InvalidPayload)
}

struct CanonicalRequest {
    bytes: Vec<u8>,
    digest: [u8; 32],
    ownerless_digest: Option<[u8; 32]>,
}

// Encode each borrowed value once. The outbox and both receipt digests consume
// the same canonical bytes; removing user_id is a byte-range omission, not a
// second cloned JSON tree or a second escaping pass. Stored bytes stay v1-compatible.
fn canonical_request(
    operation: &str,
    payload: &Map<String, Value>,
    capacity: usize,
) -> CanonicalRequest {
    let mut bytes = Vec::with_capacity(capacity);
    bytes.extend_from_slice(b"{\"operation\":");
    serde_json::to_writer(&mut bytes, operation).expect("JSON strings always encode");
    bytes.extend_from_slice(b",\"payload\":{");
    let mut owner_range = None;
    for (index, (key, value)) in payload.iter().enumerate() {
        let start = bytes.len();
        if index > 0 {
            bytes.push(b',');
        }
        serde_json::to_writer(&mut bytes, key).expect("JSON keys always encode");
        bytes.push(b':');
        serde_json::to_writer(&mut bytes, value).expect("JSON values always encode");
        if key == "user_id" {
            // For the first member omit its trailing comma, otherwise its
            // leading comma. A sole member leaves the empty object intact.
            let end = bytes.len() + usize::from(index == 0 && payload.len() > 1);
            owner_range = Some(start..end);
        }
    }
    bytes.extend_from_slice(b"}}");
    let digest = Sha256::digest(&bytes).into();
    let ownerless_digest = owner_range.map(|range| {
        let mut hasher = Sha256::new();
        hasher.update(&bytes[..range.start]);
        hasher.update(&bytes[range.end..]);
        hasher.finalize().into()
    });
    CanonicalRequest {
        bytes,
        digest,
        ownerless_digest,
    }
}

fn request_digest(operation: &str, payload: &Map<String, Value>) -> [u8; 32] {
    canonical_request(operation, payload, 0).digest
}

fn correlation_request_id(correlation_id: &[u8; 16]) -> String {
    let mut encoded = String::with_capacity(32);
    for byte in correlation_id {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn command_effects(
    admitted: AdmittedRequest<'_>,
    payload: &Map<String, Value>,
    now_unix_ms: u64,
) -> Result<TransactionIrCommandEffects, SemanticExecutionError> {
    let canonical = canonical_request(
        admitted.operation.name,
        payload,
        admitted
            .request
            .payload
            .len()
            .saturating_add(admitted.operation.name.len() + 32),
    );
    command_effects_from_canonical(admitted, payload, now_unix_ms, canonical, None)
}

fn command_effects_with_outbox(
    admitted: AdmittedRequest<'_>,
    payload: &Map<String, Value>,
    now_unix_ms: u64,
    outbox_payload: Vec<u8>,
) -> Result<TransactionIrCommandEffects, SemanticExecutionError> {
    let canonical = canonical_request(
        admitted.operation.name,
        payload,
        admitted
            .request
            .payload
            .len()
            .saturating_add(admitted.operation.name.len() + 32),
    );
    command_effects_from_canonical(
        admitted,
        payload,
        now_unix_ms,
        canonical,
        Some(outbox_payload),
    )
}

fn command_effects_from_canonical(
    admitted: AdmittedRequest<'_>,
    payload: &Map<String, Value>,
    now_unix_ms: u64,
    canonical: CanonicalRequest,
    outbox_override: Option<Vec<u8>>,
) -> Result<TransactionIrCommandEffects, SemanticExecutionError> {
    let command_id = admitted
        .request
        .command_id
        .clone()
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    // Historical v1 receipts may omit the session-injected owner. Accept
    // their alternate digest only for the authenticated owner, never on store.
    let owner_bound =
        payload.get("user_id").and_then(Value::as_u64) == Some(admitted.request.owner_id);
    Ok(TransactionIrCommandEffects {
        legacy_receipt_owner_bound: owner_bound,
        command_id,
        request_digest: canonical.digest,
        alternate_request_digest: if owner_bound {
            canonical.ownerless_digest
        } else {
            None
        },
        request_id: correlation_request_id(&admitted.request.correlation_id),
        schema_version: STORAGE_SCHEMA_VERSION,
        registry_version: STORAGE_REGISTRY_VERSION,
        committed_at_ms: now_unix_ms,
        outbox_payload: outbox_override.unwrap_or(canonical.bytes),
        store_receipt: admitted.operation.receipt_required,
    })
}

fn missing_preference() -> Value {
    json!({"present": false, "agent_id": "", "updated_at_ms": 0})
}

fn present_preference(agent_id: &str, now_unix_ms: u64) -> Value {
    json!({"present": true, "agent_id": agent_id, "updated_at_ms": now_unix_ms})
}

fn preference_key(tenant_id: u64, owner_user_id: u64) -> Result<EntityKey, SemanticExecutionError> {
    EntityKey::new(
        tenant_id,
        owner_user_id,
        DESKTOP_PREFERENCE_NAMESPACE,
        DESKTOP_PREFERENCE_KEY,
    )
    .map_err(map_storage_error)
}

fn required_text<'a>(
    payload: &'a Map<String, Value>,
    field: &str,
    maximum_characters: usize,
) -> Result<&'a str, SemanticExecutionError> {
    payload
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.chars().count() <= maximum_characters)
        .ok_or(SemanticExecutionError::InvalidPayload)
}

fn legacy_text_field(
    payload: &Map<String, Value>,
    field: &str,
    fallback: &str,
    maximum_characters: usize,
) -> Result<String, SemanticExecutionError> {
    let value = match payload.get(field) {
        None | Some(Value::Null) | Some(Value::Bool(false)) => fallback.to_owned(),
        Some(Value::String(value)) if value.is_empty() => fallback.to_owned(),
        Some(Value::Array(value)) if value.is_empty() => fallback.to_owned(),
        Some(Value::Object(value)) if value.is_empty() => fallback.to_owned(),
        Some(Value::Number(value)) if value.as_f64() == Some(0.0) => fallback.to_owned(),
        Some(Value::String(value)) => value.clone(),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(value) => {
            serde_json::to_string(value).map_err(|_| SemanticExecutionError::InvalidPayload)?
        }
    };
    Ok(value.chars().take(maximum_characters).collect())
}

fn deterministic_branch_lane_id(
    admitted: AdmittedRequest<'_>,
    conversation_id: &str,
    parent_turn_id: &str,
) -> Result<String, SemanticExecutionError> {
    let command_id = admitted
        .request
        .command_id
        .as_deref()
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db.branch-lane.v1\0");
    hasher.update(
        admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::InvalidPayload)?
            .to_le_bytes(),
    );
    hasher.update(admitted.request.owner_id.to_le_bytes());
    hasher.update(conversation_id.as_bytes());
    hasher.update([0]);
    hasher.update(parent_turn_id.as_bytes());
    hasher.update([0]);
    hasher.update(command_id.as_bytes());
    let digest = hasher.finalize();
    let mut bytes: [u8; 16] = digest[..16]
        .try_into()
        .expect("SHA-256 is at least 16 bytes");
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(format!("lane_{}", Uuid::from_bytes(bytes)))
}

fn deterministic_attempt_id(
    admitted: AdmittedRequest<'_>,
    conversation_id: &str,
    turn_id: &str,
    command_id: &str,
) -> Result<String, SemanticExecutionError> {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db.turn-attempt.v1\0");
    hasher.update(
        admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?
            .to_le_bytes(),
    );
    hasher.update(admitted.request.owner_id.to_le_bytes());
    for component in [conversation_id, turn_id, command_id] {
        hasher.update(component.as_bytes());
        hasher.update([0]);
    }
    let digest = hasher.finalize();
    let mut bytes: [u8; 16] = digest[..16]
        .try_into()
        .expect("SHA-256 is at least 16 bytes");
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(Uuid::from_bytes(bytes).to_string())
}

fn conversation_clone_identity_seed(
    admitted: &AdmittedRequest<'_>,
    source_conversation_id: &str,
    destination_conversation_id: &str,
) -> Result<[u8; 32], SemanticExecutionError> {
    let command_id = admitted
        .request
        .command_id
        .as_deref()
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db.conversation-clone-seed.v1\0");
    hasher.update(
        admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?
            .to_le_bytes(),
    );
    hasher.update(admitted.request.owner_id.to_le_bytes());
    hasher.update(source_conversation_id.as_bytes());
    hasher.update([0]);
    hasher.update(destination_conversation_id.as_bytes());
    hasher.update([0]);
    hasher.update(command_id.as_bytes());
    Ok(hasher.finalize().into())
}

fn normalized_turn_projection(mut projection: Map<String, Value>) -> Map<String, Value> {
    for key in [
        "turnId",
        "attemptId",
        "_turnId",
        "_attemptId",
        "_msgId",
        "_taskId",
        "activeTaskId",
        "role",
        "_turnActor",
        "_turnKind",
        "_turnLaneId",
        "_turnStatus",
        "_turnSettlement",
        "_projectionRevision",
        "_commandPending",
        "branches",
    ] {
        projection.remove(key);
    }
    if !projection.contains_key("providerId") {
        if let Some(provider_id) = projection.remove("provider_id") {
            projection.insert("providerId".to_owned(), provider_id);
        }
    } else {
        projection.remove("provider_id");
    }
    if !projection.contains_key("content") {
        let content = projection
            .remove("text")
            .unwrap_or_else(|| Value::String(String::new()));
        projection.insert(
            "content".to_owned(),
            if content.is_null() {
                Value::String(String::new())
            } else {
                content
            },
        );
    }
    projection
}

/// The exact same normalization as [`normalized_turn_projection`], but over
/// top-level raw JSON fields: large projection values (e.g. a 256 KiB
/// tool-result string) are never unescaped, cloned, or re-escaped at
/// admission — `RawValue` keeps the producer's verbatim bytes and the
/// canonical re-encode copies the slice (2026-09-19 turn_append_large
/// decomposition: escaped-string processing cost 277µs → 4,992µs per op
/// before this path existed). Key order is unchanged because both paths use
/// the same `Map` implementation.
fn normalized_turn_projection_raw(
    mut projection: std::collections::BTreeMap<String, Box<serde_json::value::RawValue>>,
) -> std::collections::BTreeMap<String, Box<serde_json::value::RawValue>> {
    for key in [
        "turnId",
        "attemptId",
        "_turnId",
        "_attemptId",
        "_msgId",
        "_taskId",
        "activeTaskId",
        "role",
        "_turnActor",
        "_turnKind",
        "_turnLaneId",
        "_turnStatus",
        "_turnSettlement",
        "_projectionRevision",
        "_commandPending",
        "branches",
    ] {
        projection.remove(key);
    }
    if !projection.contains_key("providerId") {
        if let Some(provider_id) = projection.remove("provider_id") {
            projection.insert("providerId".to_owned(), provider_id);
        }
    } else {
        projection.remove("provider_id");
    }
    if !projection.contains_key("content") {
        let empty_string = || {
            serde_json::from_str::<Box<serde_json::value::RawValue>>("\"\"")
                .expect("a static empty JSON string is a valid raw value")
        };
        let content = match projection.remove("text") {
            None => empty_string(),
            Some(text) if text.get() == "null" => empty_string(),
            Some(text) => text,
        };
        projection.insert("content".to_owned(), content);
    }
    projection
}

/// The append_settled admission carries two megabyte-class JSON documents
/// (`projection`, `settlement`) as nested objects. Splitting the payload
/// into a raw top-level map avoids unescaping/cloning/re-escaping them (see
/// `normalized_turn_projection_raw`); the typed map rebuilt for the
/// remaining small fields keeps their exact validation below. The intact
/// raw map ALSO feeds the receipt canonical bytes (`canonical_request_raw`)
/// so the request digest/outbox still covers the full payload.
struct AppendSettledRawFields {
    fields: Map<String, Value>,
    raw: std::collections::BTreeMap<String, Box<serde_json::value::RawValue>>,
}

fn split_append_settled_payload(
    payload: &[u8],
) -> Result<AppendSettledRawFields, SemanticExecutionError> {
    let raw: std::collections::BTreeMap<String, Box<serde_json::value::RawValue>> =
        serde_json::from_slice(payload).map_err(|_| SemanticExecutionError::InvalidPayload)?;
    let mut fields = Map::with_capacity(raw.len());
    for (key, value) in &raw {
        if key == "projection" || key == "settlement" {
            continue;
        }
        fields.insert(
            key.clone(),
            serde_json::from_str(value.get())
                .map_err(|_| SemanticExecutionError::InvalidPayload)?,
        );
    }
    Ok(AppendSettledRawFields { fields, raw })
}

/// The exact byte shape of [`canonical_request`], but over the raw top-level
/// map: values are written as their verbatim slices instead of a
/// parse-then-re-encode round trip. For the compact producers the wire
/// client emits (`json.dumps(separators=(",", ":"), ensure_ascii=False)`),
/// the bytes — and therefore the receipt digest and outbox payload — are
/// identical to the typed path; any non-canonical producer spelling stays
/// self-consistent under replay and diverges only as a fail-closed
/// receipt conflict.
fn canonical_request_raw(
    operation: &str,
    payload: &std::collections::BTreeMap<String, Box<serde_json::value::RawValue>>,
    capacity: usize,
) -> CanonicalRequest {
    let mut bytes = Vec::with_capacity(capacity);
    bytes.extend_from_slice(b"{\"operation\":");
    serde_json::to_writer(&mut bytes, operation).expect("JSON strings always encode");
    bytes.extend_from_slice(b",\"payload\":{");
    let mut owner_range = None;
    for (index, (key, value)) in payload.iter().enumerate() {
        let start = bytes.len();
        if index > 0 {
            bytes.push(b',');
        }
        serde_json::to_writer(&mut bytes, key).expect("JSON keys always encode");
        bytes.push(b':');
        bytes.extend_from_slice(value.get().as_bytes());
        if key == "user_id" {
            // For the first member omit its trailing comma, otherwise its
            // leading comma. A sole member leaves the empty object intact.
            let end = bytes.len() + usize::from(index == 0 && payload.len() > 1);
            owner_range = Some(start..end);
        }
    }
    bytes.extend_from_slice(b"}}");
    let digest = Sha256::digest(&bytes).into();
    let ownerless_digest = owner_range.map(|range| {
        let mut hasher = Sha256::new();
        hasher.update(&bytes[..range.start]);
        hasher.update(&bytes[range.end..]);
        hasher.finalize().into()
    });
    CanonicalRequest {
        bytes,
        digest,
        ownerless_digest,
    }
}

fn artifact_optional_text(
    payload: &Map<String, Value>,
    field: &str,
    maximum_characters: usize,
) -> Result<String, SemanticExecutionError> {
    match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum_characters => {
            Ok(value.clone())
        }
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn artifact_bool(
    payload: &Map<String, Value>,
    field: &str,
    default: bool,
) -> Result<bool, SemanticExecutionError> {
    match payload.get(field) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn compile_artifact(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let artifact_id = || required_text(&payload, "artifact_id", 256).map(str::to_owned);
    let (steps, result) = match admitted.operation.name {
        "artifact.create" => {
            let source_ref = payload
                .get("source_ref")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let meta = payload.get("meta").cloned().unwrap_or_else(|| json!({}));
            if !source_ref.is_object() || !meta.is_object() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let content = artifact_optional_text(
                &payload,
                "content",
                usize::MAX,
            )?;
            (
                vec![TransactionIrStep::ArtifactCreate {
                    request: crate::artifact::CreateRequest {
                        artifact_id: artifact_id()?,
                        conv_id: required_text(&payload, "conv_id", 512)?.to_owned(),
                        task_id: artifact_optional_text(&payload, "task_id", 512)?,
                        msg_id: artifact_optional_text(&payload, "msg_id", 512)?,
                        source: required_text(&payload, "source", 256)?.to_owned(),
                        source_ref,
                        format: required_text(&payload, "format", 32)?.to_owned(),
                        title: artifact_optional_text(&payload, "title", 300)?
                            .trim()
                            .to_owned(),
                        content,
                        parent_id: artifact_optional_text(&payload, "parent_id", 256)?,
                        meta,
                        created_at: payload
                            .get("created_at")
                            .and_then(Value::as_u64)
                            .ok_or(SemanticExecutionError::InvalidPayload)?,
                    },
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "artifact.get" => (
            vec![TransactionIrStep::ArtifactGet {
                artifact_id: artifact_id()?,
                include_content: artifact_bool(&payload, "include_content", false)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "artifact.list" => (
            vec![TransactionIrStep::ArtifactList {
                conversation_id: required_text(&payload, "conv_id", 512)?.to_owned(),
                include_deleted: artifact_bool(&payload, "include_deleted", false)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "artifact.delete" => (
            vec![TransactionIrStep::ArtifactDelete {
                artifact_id: artifact_id()?,
                deleted_at_ms: positive_identity(&payload, "deleted_at")?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "artifact.versions" => (
            vec![TransactionIrStep::ArtifactVersions {
                artifact_id: artifact_id()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "artifact.library" => (
            vec![TransactionIrStep::ArtifactLibrary {
                limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 50)?)
                    .ok()
                    .filter(|limit| (1..=200).contains(limit))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "artifact.pin" => (
            vec![TransactionIrStep::ArtifactPin {
                artifact_id: artifact_id()?,
                pinned: payload
                    .get("pinned")
                    .and_then(Value::as_bool)
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "tool_result_artifact.put" => {
            let owner = positive_identity(&payload, "user_id")?;
            if owner != admitted.request.owner_id {
                return Err(SemanticExecutionError::AuthorityScopeMismatch);
            }
            let content = artifact_optional_text(
                &payload,
                "content",
                crate::generated_tofudb_ir::MAX_TOOL_RESULT_ARTIFACT_BYTES,
            )?;
            if content.len() > crate::generated_tofudb_ir::MAX_TOOL_RESULT_ARTIFACT_BYTES {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            (
                vec![TransactionIrStep::ToolResultArtifactPut {
                    content,
                    media_type: artifact_optional_text(&payload, "media_type", 128)?
                        .trim()
                        .to_owned(),
                    created_at_ms: positive_identity(&payload, "created_at_ms")?,
                    expires_at_ms: positive_identity(&payload, "expires_at_ms")?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "tool_result_artifact.prune" => (
            vec![TransactionIrStep::ToolResultArtifactPrune {
                now_ms: positive_identity(&payload, "now_ms")?,
                limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 1_000)?)
                    .ok()
                    .filter(|limit| {
                        (1..=crate::generated_tofudb_ir::MAX_TOOL_RESULT_PRUNE_ROWS).contains(limit)
                    })
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "tool_result_artifact.read" | "tool_result_artifact.search" => {
            let owner = positive_identity(&payload, "user_id")?;
            if owner != admitted.request.owner_id {
                return Err(SemanticExecutionError::AuthorityScopeMismatch);
            }
            let artifact_ref = required_text(&payload, "artifact_ref", 96)?.to_owned();
            let now_ms = positive_identity(&payload, "now_ms")?;
            let selector = match payload.get("selector") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(value)) => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            if admitted.operation.name.ends_with(".read") {
                let offset = payload.get("cursor").or_else(|| payload.get("offset")).cloned().unwrap_or(json!(0));
                let limit = usize::try_from(optional_nonnegative_integer(&payload, "limit", 8192)?)
                    .ok()
                    .filter(|limit| (1..=64 * 1024).contains(limit))
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                (
                    vec![TransactionIrStep::ToolResultArtifactRead {
                        artifact_ref,
                        now_ms,
                        offset,
                        selector,
                        limit,
                        slot: 0,
                    }],
                    TransactionIrResult::SlotOrLiteral {
                        slot: 0,
                        missing: b"null".to_vec(),
                    },
                )
            } else {
                let cursor = payload.get("cursor").cloned().unwrap_or(json!(0));
                let limit = usize::try_from(optional_nonnegative_integer(&payload, "limit", 8)?)
                    .ok()
                    .filter(|limit| (1..=20).contains(limit))
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                (
                    vec![TransactionIrStep::ToolResultArtifactSearch {
                        artifact_ref,
                        query: required_text(&payload, "query", 200)?.to_owned(),
                        now_ms,
                        cursor,
                        selector,
                        limit,
                        slot: 0,
                    }],
                    TransactionIrResult::SlotOrLiteral {
                        slot: 0,
                        missing: b"null".to_vec(),
                    },
                )
            }
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let effects = if admitted.operation.kind.mutates_state() {
        let effects = if matches!(
            admitted.operation.name,
            "artifact.create" | "tool_result_artifact.put"
        ) {
            let content = payload
                .get("content")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let mut replay_payload = payload.clone();
            replay_payload.remove("content");
            replay_payload.insert(
                "content_sha256".to_owned(),
                Value::String(format!("{:x}", Sha256::digest(content.as_bytes()))),
            );
            replay_payload.insert("size_bytes".to_owned(), json!(content.len()));
            let outbox_payload = canonical_json(&json!({
                "operation": admitted.operation.name,
                "payload": replay_payload,
                "contentAuthority": "owner-bound-artifact-blob"
            }))?;
            command_effects_with_outbox(admitted, &payload, now_unix_ms, outbox_payload)?
        } else {
            command_effects(admitted, &payload, now_unix_ms)?
        };
        Some(effects)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: effects,
    })
}

fn compile_turn(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    // turn.append_settled splits its megabyte-class nested documents off as
    // raw values before the typed parse; every other op keeps the shared
    // typed admission. See split_append_settled_payload.
    let mut append_settled_raw = None;
    let payload = if admitted.operation.name == "turn.append_settled" {
        let AppendSettledRawFields { fields, raw } =
            split_append_settled_payload(&admitted.request.payload)?;
        append_settled_raw = Some(raw);
        fields
    } else {
        payload_object(&admitted.request.payload)?
    };
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    if admitted.operation.name == "turn.attempt.dispatchable.list" {
        let created_before_ms = payload
            .get("created_before_ms")
            .and_then(Value::as_u64)
            .filter(|value| *value <= MAX_WORKER_JOB_CLOCK_MILLISECONDS)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let limit = usize::try_from(optional_nonnegative_integer(&payload, "limit", 8)?)
            .ok()
            .filter(|limit| {
                (1..=crate::generated_tofudb_ir::MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY)
                    .contains(limit)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptDispatchableList {
                created_before_ms,
                limit,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"[]".to_vec(),
            },
            command_effects: None,
        });
    }
    if admitted.operation.name == "turn.search.backfill" {
        // This command schedules a GLOBAL rebuild by enumerating real
        // tenant/owner authorization scopes; it must never degenerate into
        // an authenticated-owner rebuild, so any payload identity fails
        // closed instead of narrowing or widening the sweep.
        if let Some(value) = payload.get("user_id") {
            if value.as_u64() != Some(admitted.request.owner_id) {
                return Err(SemanticExecutionError::AuthorityScopeMismatch);
            }
            return Err(SemanticExecutionError::InvalidPayload);
        }
        if let Some(value) = payload.get("tenant_id") {
            if value.as_u64() != Some(tenant_id) {
                return Err(SemanticExecutionError::AuthorityScopeMismatch);
            }
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let cursor = match payload.get("cursor") {
            None | Some(Value::Null) => Vec::new(),
            Some(Value::String(value))
                if value.chars().count()
                    <= search_backfill_global::MAX_GLOBAL_BACKFILL_CURSOR_BYTES =>
            {
                value.as_bytes().to_vec()
            }
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let max_rows = usize::try_from(optional_nonnegative_integer(&payload, "max_rows", 8)?)
            .ok()
            .filter(|limit| {
                (1..=search_backfill_global::MAX_GLOBAL_BACKFILL_CLAIM_ROWS_PER_TRANSACTION)
                    .contains(limit)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let max_bytes = usize::try_from(optional_nonnegative_integer(
            &payload,
            "max_bytes",
            2_000_000,
        )?)
        .ok()
        .filter(|limit| {
            (1..=search_backfill_global::MAX_GLOBAL_BACKFILL_SOURCE_BYTES_PER_TRANSACTION)
                .contains(limit)
        })
        .ok_or(SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnSearchBackfillGlobal {
                request: search_backfill_global::GlobalBackfillRequest {
                    cursor,
                    max_rows,
                    max_bytes,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing:
                    br#"{"scanned":0,"indexed":0,"failed":0,"nextCursor":"","remaining":false}"#
                        .to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    let user_id = positive_identity(&payload, "user_id")?;
    if user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    if admitted.operation.name == "turn.cleanup" {
        let retention_ms =
            optional_nonnegative_integer(&payload, "retention_ms", 6 * 60 * 60 * 1_000)?;
        let limit = usize::try_from(optional_nonnegative_integer(&payload, "limit", 64)?)
            .ok()
            .filter(|limit| {
                (1..=crate::generated_tofudb_ir::MAX_CLEANUP_ATTEMPTS_PER_TRANSACTION)
                    .contains(limit)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnCleanup {
                request: crate::turn::CleanupRequest {
                    retention_ms,
                    limit,
                    now_ms: now_unix_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.compact" {
        let conversation_id = required_text(&payload, "conversation_id", 256)?.to_owned();
        let expected_conversation_revision = payload
            .get("expected_conversation_revision")
            .and_then(Value::as_u64)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let summary_turn_id = required_text(&payload, "summary_turn_id", 128)?.to_owned();
        let mut summary_projection = payload
            .get("summary_projection")
            .and_then(Value::as_object)
            .cloned()
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        if summary_projection
            .get("compaction")
            .and_then(Value::as_object)
            .and_then(|compaction| compaction.get("blockId"))
            .and_then(Value::as_str)
            != Some("compaction")
        {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        if let Some(Value::Object(compaction)) = summary_projection.get_mut("compaction") {
            const COMPACTION_FIELDS: [&str; 12] = [
                "blockId",
                "archiveId",
                "conversationId",
                "trigger",
                "timestamp",
                "tokensBefore",
                "tokensAfter",
                "messagesBefore",
                "messagesAfter",
                "reductionPercent",
                "foldedToolRounds",
                "estimatedPromptTokens",
            ];
            compaction.retain(|field, _| COMPACTION_FIELDS.contains(&field.as_str()));
        }
        let mut delete_turn_ids = Vec::new();
        let mut seen_delete_ids = std::collections::BTreeSet::new();
        match payload.get("delete_turn_ids") {
            None | Some(Value::Null) => {}
            Some(Value::Array(values)) => {
                for value in values {
                    if is_python_falsy_json(value) {
                        continue;
                    }
                    let turn_id = value
                        .as_str()
                        .filter(|value| !value.is_empty() && value.chars().count() <= 128)
                        .ok_or(SemanticExecutionError::InvalidPayload)?;
                    if seen_delete_ids.insert(turn_id.to_owned()) {
                        delete_turn_ids.push(turn_id.to_owned());
                    }
                }
            }
            _ => return Err(SemanticExecutionError::InvalidPayload),
        }
        if delete_turn_ids.len() > 2_000 {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let mut projection_updates = Vec::new();
        let raw_updates = match payload.get("projection_updates") {
            None | Some(Value::Null) => &[][..],
            Some(Value::Array(values)) => values.as_slice(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        if raw_updates.len() > crate::generated_tofudb_ir::MAX_TURN_COMPACTION_PROJECTION_UPDATES {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let mut projection_bytes =
            canonical_json(&Value::Object(summary_projection.clone()))?.len();
        for value in raw_updates {
            let update = value
                .as_object()
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let turn_id = required_text(update, "turn_id", 128)?.to_owned();
            let expected_projection_revision = update
                .get("expected_projection_revision")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let projection = update
                .get("projection")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let projection_json = canonical_json(&Value::Object(projection))?;
            projection_bytes = projection_bytes
                .checked_add(projection_json.len())
                .filter(|bytes| {
                    *bytes <= crate::generated_tofudb_ir::MAX_TURN_COMPACTION_PROJECTION_BYTES
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            projection_updates.push(crate::turn::CompactProjectionUpdate {
                turn_id,
                expected_projection_revision,
                projection_json,
            });
        }
        let anchor = |field: &str| match payload.get(field) {
            None | Some(Value::Null) => Ok(None),
            Some(value) if is_python_falsy_json(value) => Ok(None),
            Some(Value::String(value)) if value.chars().count() <= 128 => Ok(Some(value.clone())),
            _ => Err(SemanticExecutionError::InvalidPayload),
        };
        let insert_after_turn_id = anchor("insert_after_turn_id")?;
        let insert_before_turn_id = anchor("insert_before_turn_id")?;
        if insert_after_turn_id.is_none() && insert_before_turn_id.is_none() {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let mut outbox_payload = payload.clone();
        outbox_payload.remove("summary_projection");
        outbox_payload.remove("projection_updates");
        outbox_payload.insert(
            "summaryProjectionSha256".to_owned(),
            Value::String(format!(
                "{:x}",
                Sha256::digest(canonical_json(&Value::Object(summary_projection.clone()))?)
            )),
        );
        outbox_payload.insert(
            "projectionUpdateCount".to_owned(),
            Value::from(projection_updates.len()),
        );
        outbox_payload.insert("projectionBytes".to_owned(), Value::from(projection_bytes));
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnCompact {
                request: crate::turn::CompactRequest {
                    conversation_id,
                    expected_conversation_revision,
                    summary_turn_id,
                    summary_projection_json: canonical_json(&Value::Object(summary_projection))?,
                    delete_turn_ids,
                    projection_updates,
                    insert_after_turn_id,
                    insert_before_turn_id,
                    now_ms: now_unix_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects_with_outbox(
                admitted,
                &payload,
                now_unix_ms,
                canonical_json(&json!({
                    "operation": admitted.operation.name,
                    "payload": outbox_payload,
                    "contentAuthority": "owner-bound-turn-blobs",
                }))?,
            )?),
        });
    }
    if admitted.operation.name == "turn.sync.prune" {
        let created_before_ms = payload
            .get("created_before_ms")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0 && *value <= MAX_WORKER_JOB_CLOCK_MILLISECONDS)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let max_rows = usize::try_from(optional_nonnegative_integer(&payload, "max_rows", 512)?)
            .ok()
            .filter(|value| (1..=20_000).contains(value))
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnSyncPrune {
                request: crate::turn::SyncPruneRequest {
                    created_before_ms,
                    max_rows,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: br#"{"deletedRows":0,"remaining":false}"#.to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.events.prune" {
        let settled_before_ms = payload
            .get("settled_before_ms")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0 && *value <= MAX_WORKER_JOB_CLOCK_MILLISECONDS)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let max_attempts =
            usize::try_from(optional_nonnegative_integer(&payload, "max_attempts", 16)?)
                .ok()
                .filter(|value| (1..=256).contains(value))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
        let max_rows = usize::try_from(optional_nonnegative_integer(&payload, "max_rows", 4_096)?)
            .ok()
            .filter(|value| {
                (1..=crate::generated_tofudb_ir::MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION)
                    .contains(value)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnEventsPrune {
                request: crate::turn::EventPruneRequest {
                    settled_before_ms,
                    max_attempts,
                    max_rows,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: br#"{"deleted_rows":0,"deleted_attempts":0,"remaining":0}"#.to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.recover" {
        let max_rows = usize::try_from(optional_nonnegative_integer(&payload, "max_rows", 8)?)
            .ok()
            .filter(|value| {
                (1..=crate::generated_tofudb_ir::MAX_RECOVERY_ROWS_PER_TRANSACTION).contains(value)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let max_bytes = usize::try_from(optional_nonnegative_integer(
            &payload,
            "max_bytes",
            2_000_000,
        )?)
        .ok()
        .filter(|value| {
            (1..=crate::generated_tofudb_ir::MAX_RECOVERY_PROJECTION_BYTES_PER_TRANSACTION)
                .contains(value)
        })
        .ok_or(SemanticExecutionError::InvalidPayload)?;
        let created_before_ms = match payload.get("created_before_ms") {
            None | Some(Value::Null) => None,
            Some(value) => Some(
                value
                    .as_u64()
                    .filter(|value| *value <= MAX_WORKER_JOB_CLOCK_MILLISECONDS)
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            ),
        };
        let mut exclude_task_ids = std::collections::BTreeSet::new();
        if let Some(Value::Array(values)) = payload.get("exclude_task_ids") {
            for value in values {
                if is_python_falsy_json(value) {
                    continue;
                }
                let task_id = value
                    .as_str()
                    .filter(|value| value.chars().count() <= 256)
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                exclude_task_ids.insert(task_id.to_owned());
            }
        }
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnRecover {
                request: crate::turn::RecoverRequest {
                    max_rows,
                    max_bytes,
                    created_before_ms,
                    exclude_task_ids,
                    now_ms: now_unix_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: br#"{"recovered":0,"remaining":0}"#.to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.timing_trace.get" {
        let task_id = required_text(&payload, "task_id", 256)?.to_owned();
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnTimingTraceGet { task_id, slot: 0 }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: None,
        });
    }
    if admitted.operation.name == "turn.perception.record" {
        let conversation_id = required_text(&payload, "conversation_id", 256)?.to_owned();
        let turn_id = required_text(&payload, "turn_id", 128)?.to_owned();
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let observation = payload
            .get("observation")
            .and_then(Value::as_object)
            .cloned()
            .filter(|observation| {
                crate::turn::perception_observation_is_valid(observation, &attempt_id)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnPerceptionRecord {
                request: crate::turn::PerceptionRecordRequest {
                    conversation_id,
                    turn_id,
                    attempt_id,
                    observation,
                    recorded_at_ms: now_unix_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: None,
        });
    }
    if admitted.operation.name == "turn.related.announce" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let values = match payload.get("turn_ids") {
            None | Some(Value::Null) => &[][..],
            Some(Value::Array(values)) => values.as_slice(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        if values.len() > crate::turn::MAX_RELATED_TURNS_PER_ANNOUNCEMENT {
            return Err(SemanticExecutionError::ResourceExhausted);
        }
        let mut turn_ids = Vec::with_capacity(values.len());
        for value in values {
            if is_python_falsy_json(value) {
                continue;
            }
            let turn_id = match value {
                Value::String(value) => value.clone(),
                Value::Bool(true) => "True".to_owned(),
                Value::Number(value) => value.to_string(),
                value => serde_json::to_string(value)
                    .map_err(|_| SemanticExecutionError::InvalidPayload)?,
            };
            if turn_id.is_empty() || turn_id.chars().count() > 128 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            turn_ids.push(turn_id);
        }
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnRelatedAnnounce {
                request: crate::turn::RelatedAnnounceRequest {
                    attempt_id,
                    turn_ids,
                    updated_at_ms: now_unix_ms,
                    committed_at_ms: now_unix_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"false".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.event.record" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let terminal = payload
            .get("terminal")
            .is_some_and(|value| !is_python_falsy_json(value));
        let slim = payload
            .get("slim")
            .is_some_and(|value| !is_python_falsy_json(value));
        let status = match payload.get("status") {
            None | Some(Value::Null) => if terminal { "failed" } else { "running" }.to_owned(),
            Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= 128 => {
                value.clone()
            }
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let task_id = match payload.get("task_id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) if value.chars().count() <= 256 => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let projection_patch = match payload.get("projection_patch") {
            None | Some(Value::Null) => None,
            Some(Value::Object(value)) => Some(value.clone()),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let projection_segments_stable = match payload.get("projection_segments_stable") {
            None => false,
            Some(Value::Bool(value)) => *value,
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        if (projection_segments_stable && projection_patch.is_none())
            || (slim && projection_patch.is_some())
        {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let object_or_empty = |field: &str| match payload.get(field) {
            None | Some(Value::Null) => Ok(Map::new()),
            Some(Value::Object(value)) => Ok(value.clone()),
            Some(value) if is_python_falsy_json(value) => Ok(Map::new()),
            _ => Err(SemanticExecutionError::InvalidPayload),
        };
        let settlement = object_or_empty("settlement")?;
        let error = object_or_empty("error")?;
        let event_payload = object_or_empty("event_payload")?;
        let event_type = match payload.get("event_type") {
            None | Some(Value::Null) => "projection_updated".to_owned(),
            Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= 128 => {
                value.clone()
            }
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let content = match payload.get("content") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let thinking = match payload.get("thinking") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let projection = payload.get("projection").cloned().unwrap_or(Value::Null);
        let logical_now = match payload.get("now") {
            None | Some(Value::Null) => now_unix_ms,
            Some(value) if is_python_falsy_json(value) => now_unix_ms,
            Some(value) => value
                .as_u64()
                .filter(|value| *value > 0)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        };
        let task_event = payload
            .get("task_event")
            .map(|value| {
                value
                    .as_object()
                    .ok_or(SemanticExecutionError::InvalidPayload)
                    .and_then(|event| compile_event_item(event, logical_now))
            })
            .transpose()?;
        let preceding_task_events = match payload.get("task_event").and_then(Value::as_object)
            .and_then(|event| event.get("preceding_events")) {
            None => Vec::new(),
            Some(Value::Array(events)) if events.len() < crate::generated_task_event_delivery::PROGRESS_BATCH_MAX_FRAMES => events.iter().map(|event| {
                event.as_object().ok_or(SemanticExecutionError::InvalidPayload)
                    .and_then(|event| compile_event_item(event, logical_now))
            }).collect::<Result<Vec<_>, _>>()?,
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        // Flow phase attempts declare their root here; the engine must prove
        // it against the immutable attempt config inside the transaction,
        // exactly like the SQL sidecar fence. Dropping this field used to
        // leave native authority without any flow-root fence.
        let flow_root_attempt_id = match payload.get("flow_root_attempt_id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) if value.chars().count() <= 128 => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnEventRecord {
                request: crate::turn::EventRecordRequest {
                    model_continuation: payload
                        .get("model_continuation")
                        .cloned()
                        .unwrap_or(Value::Null),
                    attempt_id,
                    task_id,
                    terminal,
                    status,
                    slim,
                    content,
                    thinking,
                    projection,
                    projection_patch,
                    projection_segments_stable,
                    settlement,
                    error,
                    event_type,
                    event_payload,
                    background_command_ack: payload.get("background_command_ack").cloned(),
                    task_event,
                    preceding_task_events,
                    flow_root_attempt_id,
                    now_ms: logical_now,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.attempt.dispatch_worker" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let principal = payload
            .get("principal")
            .and_then(Value::as_object)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        if principal.len() != 5
            || !["kind", "subject_id", "owner_user_id", "tenant_id", "scopes"]
                .into_iter()
                .all(|field| principal.contains_key(field))
        {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let kind = principal
            .get("kind")
            .and_then(Value::as_str)
            .filter(|value| matches!(*value, "user" | "system"))
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let subject_id = principal
            .get("subject_id")
            .and_then(Value::as_str)
            .filter(|value| {
                !value.is_empty() && value.trim() == *value && value.chars().count() <= 256
            })
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let principal_owner = principal
            .get("owner_user_id")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let tenant_label = match principal.get("tenant_id") {
            Some(Value::Null) => String::new(),
            Some(Value::String(value))
                if !value.is_empty() && value.trim() == value && value.chars().count() <= 256 =>
            {
                value.clone()
            }
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let scopes = principal
            .get("scopes")
            .and_then(Value::as_array)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let mut previous_scope: Option<&str> = None;
        for scope in scopes {
            let scope = scope
                .as_str()
                .filter(|value| {
                    !value.is_empty() && value.trim() == *value && value.chars().count() <= 128
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if previous_scope.is_some_and(|previous| previous >= scope) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            previous_scope = Some(scope);
        }
        let _ = (kind, subject_id);
        if principal_owner != user_id {
            return Err(SemanticExecutionError::Conflict);
        }
        let priority = optional_nonnegative_integer(&payload, "priority", 100)?;
        if priority > MAX_WORKER_JOB_PRIORITY as u64 {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let now_ms = worker_job_now(&payload, now_unix_ms)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptDispatchWorker {
                request: crate::turn::AttemptDispatchWorkerRequest {
                    attempt_id,
                    user_id,
                    principal: Value::Object(principal.clone()),
                    tenant_label,
                    priority: priority as u16,
                    now_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.attempt.create" {
        let conversation_id = required_text(&payload, "conversation_id", 256)?.to_owned();
        let turn_id = required_text(&payload, "turn_id", 128)?.to_owned();
        let command_id = required_text(&payload, "command_id", 256)?.to_owned();
        let operation = required_text(&payload, "operation", 64)?.to_owned();
        if !matches!(
            operation.as_str(),
            "continue" | "checkpoint_resume" | "regenerate" | "answer_guidance"
        ) {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let dispatch_mode = match payload.get("dispatch_mode") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
            Some(Value::String(value)) if value.is_empty() => String::new(),
            Some(Value::String(value)) if value == "conversation_executor" => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let expected_projection_revision = payload
            .get("expected_projection_revision")
            .and_then(Value::as_u64)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let config = match payload.get("config") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => json!({}),
            Some(Value::String(value)) if value.is_empty() => json!({}),
            Some(Value::Array(value)) if value.is_empty() => json!({}),
            Some(Value::Object(value)) if value.is_empty() => json!({}),
            Some(Value::Number(value)) if value.as_f64() == Some(0.0) => json!({}),
            Some(value) => value.clone(),
        };
        let answer = config
            .as_object()
            .and_then(|config| config.get("_humanGuidanceAnswer"));
        if operation == "answer_guidance" {
            let answer = answer
                .and_then(Value::as_object)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            required_text(answer, "guidanceId", 128)?;
            required_text(answer, "response", 32_768)?;
        } else if answer.is_some() {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let (target_actor, target_kind) = if operation == "regenerate" {
            let actor = required_text(&payload, "target_actor", 64)?.to_owned();
            let kind = required_text(&payload, "target_kind", 64)?.to_owned();
            if !matches!(actor.as_str(), "assistant" | "planner")
                || !matches!(kind.as_str(), "reply" | "plan" | "flow_node")
                || (actor == "planner") != (kind == "plan")
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (Some(actor), Some(kind))
        } else {
            if payload
                .get("target_actor")
                .is_some_and(|value| !value.is_null())
                || payload
                    .get("target_kind")
                    .is_some_and(|value| !value.is_null())
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (None, None)
        };
        let resume_anchor = match payload.get("resume_anchor") {
            None | Some(Value::Null) => None,
            Some(Value::Object(anchor)) => Some(anchor.clone()),
            Some(_) => return Err(SemanticExecutionError::InvalidPayload),
        };
        let input_update = match payload.get("input_update") {
            None | Some(Value::Null) => None,
            Some(Value::Object(update)) => Some(update.clone()),
            Some(Value::String(content)) => Some(Map::from_iter([(
                "content".to_owned(),
                Value::String(content.clone()),
            )])),
            Some(Value::Bool(content)) => Some(Map::from_iter([(
                "content".to_owned(),
                Value::String(if *content { "True" } else { "False" }.to_owned()),
            )])),
            Some(Value::Number(content)) => Some(Map::from_iter([(
                "content".to_owned(),
                Value::String(content.to_string()),
            )])),
            Some(Value::Array(content)) => Some(Map::from_iter([(
                "content".to_owned(),
                Value::String(
                    serde_json::to_string(content)
                        .map_err(|_| SemanticExecutionError::InvalidPayload)?,
                ),
            )])),
        };
        if let Some(update) = &input_update {
            if operation != "regenerate" {
                // Resume operations (continue/checkpoint_resume/answer_guidance)
                // may carry only a contextSnapshot rebind onto the submitted
                // parent's info rail — a pause → switch-model → continue
                // refreshes display facts while submitted content stays
                // regenerate-only. Mirrors the sidecar's
                // _resume_ctx_rebound_projection contract.
                let valid_resume_rebind = crate::turn::valid_resume_input_update(update);
                if !valid_resume_rebind {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
            }
        }
        let expected_input_projection_revision =
            match payload.get("expected_input_projection_revision") {
                None | Some(Value::Null) => None,
                Some(value) => value.as_u64(),
            };
        if serde_json::to_vec(&config)
            .map_err(|_| SemanticExecutionError::InvalidPayload)?
            .len()
            > MAX_LEGACY_DOCUMENT_BYTES
            || input_update.as_ref().is_some_and(|update| {
                serde_json::to_vec(update)
                    .map(|encoded| encoded.len() > MAX_LEGACY_DOCUMENT_BYTES)
                    .unwrap_or(true)
            })
        {
            return Err(SemanticExecutionError::ResourceExhausted);
        }
        let attempt_id =
            deterministic_attempt_id(admitted, &conversation_id, &turn_id, &command_id)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptCreate {
                request: crate::turn::AttemptCreateRequest {
                    conversation_id,
                    turn_id,
                    attempt_id,
                    command_id,
                    operation,
                    dispatch_mode,
                    expected_projection_revision,
                    resume_anchor,
                    config,
                    input_update,
                    expected_input_projection_revision,
                    target_actor,
                    target_kind,
                    created_at_ms: now_unix_ms,
                    committed_at_ms: now_unix_ms,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.attempt.bind" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let task_id = required_text(&payload, "task_id", 256)?.to_owned();
        let dispatch_owner_id = match payload.get("dispatch_owner_id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) if value.is_empty() => String::new(),
            Some(Value::String(value)) if value.chars().count() <= 64 => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptBind {
                attempt_id,
                task_id,
                dispatch_owner_id,
                committed_at_ms: now_unix_ms,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.attempt.start" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let task_id = required_text(&payload, "task_id", 256)?.to_owned();
        let automation_context: Option<crate::automation_events::TaskStartedContext> = match payload.get("automation_context") {
            None | Some(Value::Null) => None,
            Some(value) => Some(serde_json::from_value(value.clone()).map_err(|_| SemanticExecutionError::InvalidPayload)?),
        };
        if let Some(context) = &automation_context { context.validate().map_err(|_| SemanticExecutionError::InvalidPayload)?; }
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptStart {
                attempt_id,
                task_id,
                automation_context,
                committed_at_ms: now_unix_ms,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.attempt.claim" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let dispatch_owner_id = match payload.get("dispatch_owner_id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) if value.is_empty() => String::new(),
            Some(Value::String(value)) if value.chars().count() <= 64 => value.clone(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptClaim {
                attempt_id,
                dispatch_owner_id,
                committed_at_ms: now_unix_ms,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"false".to_vec(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "turn.attempt.get" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnAttemptGet {
                attempt_id,
                include_execution_config: match payload.get("include_execution_config") {
                    None => false, Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: None,
        });
    }
    if admitted.operation.name == "turn.events.list" {
        let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
        let after = optional_nonnegative_integer(&payload, "after", 0)?;
        let limit = optional_nonnegative_integer(&payload, "limit", 1_000)?;
        if !(1..=5_000).contains(&limit) {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let patch_mode = payload
            .get("projection_mode")
            .and_then(Value::as_str)
            .is_some_and(|mode| mode == "patch");
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::TurnEventsList {
                attempt_id,
                after,
                limit: limit as usize,
                patch_mode,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            command_effects: None,
        });
    }
    let conversation_id = required_text(&payload, "conversation_id", 256)?.to_owned();
    let (steps, result, effects) = match admitted.operation.name {
        "turn.create_pair" => {
            let command_id = required_text(&payload, "command_id", 256)?.to_owned();
            let defaulted_text = |field: &str, fallback: &str, maximum: usize| match payload
                .get(field)
            {
                None | Some(Value::Null) => Ok(fallback.to_owned()),
                Some(Value::String(value)) if value.is_empty() => Ok(fallback.to_owned()),
                Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
                _ => Err(SemanticExecutionError::InvalidPayload),
            };
            let dispatch_mode = defaulted_text("dispatch_mode", "", 64)?;
            if !matches!(dispatch_mode.as_str(), "" | "conversation_executor") {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let lane_id = defaulted_text("lane_id", "main", 256)?;
            let input_actor = defaulted_text("input_actor", "human", 32)?;
            let output_actor = defaulted_text("output_actor", "assistant", 32)?;
            if !matches!(input_actor.as_str(), "human" | "virtual_user" | "critic")
                || !matches!(
                    output_actor.as_str(),
                    "assistant" | "planner" | "critic" | "virtual_user"
                )
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let input_projection = match payload.get("input_projection") {
                Some(Value::String(content)) => {
                    Map::from_iter([("content".to_owned(), Value::String(content.clone()))])
                }
                Some(Value::Object(value)) => value.clone(),
                _ => Map::from_iter([("content".to_owned(), Value::String(String::new()))]),
            };
            let input_projection = normalized_turn_projection(input_projection);
            let config = match payload.get("config") {
                None | Some(Value::Null) => Map::new(),
                Some(Value::Object(value)) => value.clone(),
                Some(value) if is_python_falsy_json(value) => Map::new(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let logical_now = match payload.get("now") {
                None | Some(Value::Null) => now_unix_ms,
                Some(value) => value
                    .as_u64()
                    .filter(|value| *value > 0)
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            };
            let defaults = payload
                .get("conversation_defaults")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut settings = defaults
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            settings.remove("activeTaskId");
            let turn_defaults = crate::conversation_header::TurnDefaults {
                allow_create: defaults
                    .get("allowCreate")
                    .is_some_and(|value| !is_python_falsy_json(value)),
                title: defaults
                    .get("title")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .unwrap_or("New Chat")
                    .chars()
                    .take(500)
                    .collect(),
                settings_json: canonical_json(&Value::Object(settings))?,
                created_at_ms: defaults
                    .get("createdAt")
                    .and_then(Value::as_u64)
                    .filter(|value| *value > 0)
                    .unwrap_or(logical_now),
            };
            let parent_turn_id = match payload.get("parent_turn_id") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) if value.is_empty() => None,
                Some(Value::String(value)) if value.chars().count() <= 128 => Some(value.clone()),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let raw_binding = match payload.get("queue_binding") {
                None | Some(Value::Null) => Map::new(),
                Some(Value::Object(value)) => value.clone(),
                Some(value) if is_python_falsy_json(value) => Map::new(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let queue_id = raw_binding
                .get("queueId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            if queue_id.chars().count() > 256 || (!queue_id.is_empty() && input_actor != "human") {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let queue_binding = if queue_id.is_empty() {
                None
            } else {
                let message = raw_binding
                    .get("message")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_else(|| {
                        Map::from_iter([
                            (
                                "text".to_owned(),
                                Value::String(
                                    input_projection
                                        .get("content")
                                        .and_then(Value::as_str)
                                        .unwrap_or("")
                                        .to_owned(),
                                ),
                            ),
                            (
                                "_user_msg".to_owned(),
                                Value::Object(input_projection.clone()),
                            ),
                        ])
                    });
                let kind = raw_binding
                    .get("kind")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .unwrap_or("real")
                    .to_owned();
                if !matches!(kind.as_str(), "real" | "goal_continuation") {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                let priority = optional_nonnegative_integer(&raw_binding, "priority", 100)?;
                if priority > 1000 {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                Some(crate::turn::CreatePairQueueBinding {
                    queue_id,
                    message,
                    kind,
                    priority,
                    created_at_ms: optional_nonnegative_integer(
                        &raw_binding,
                        "createdAt",
                        logical_now,
                    )?,
                })
            };
            let input_presentation_id =
                defaulted_text("input_presentation_id", &format!("{command_id}:input"), 512)?;
            let output_presentation_id = defaulted_text(
                "output_presentation_id",
                &format!("{command_id}:output"),
                512,
            )?;
            let input_attempt_id = (input_actor != "human").then(|| Uuid::new_v4().to_string());
            (
                vec![TransactionIrStep::TurnCreatePair {
                    request: Box::new(crate::turn::CreatePairRequest {
                        conversation_id,
                        command_id,
                        dispatch_mode,
                        lane_id,
                        input_actor,
                        output_actor,
                        input_projection,
                        input_presentation_id,
                        output_presentation_id,
                        input_kind: defaulted_text("input_kind", "input", 128)?,
                        output_kind: defaulted_text("kind", "reply", 128)?,
                        run_id: defaulted_text("run_id", "", 256)?,
                        config,
                        parent_turn_id,
                        require_lane_idle: payload
                            .get("require_lane_idle")
                            .is_some_and(|value| !is_python_falsy_json(value)),
                        require_parent_is_lane_tail: payload
                            .get("require_parent_is_lane_tail")
                            .is_some_and(|value| !is_python_falsy_json(value)),
                        expected_parent_projection_revision: payload
                            .get("expected_parent_projection_revision")
                            .filter(|value| !value.is_null())
                            .map(|value| value.as_u64().ok_or_else(||
                                SemanticExecutionError::InvalidPayload))
                            .transpose()?,
                        require_parent_not_stopped: payload
                            .get("require_parent_not_stopped")
                            .is_some_and(|value| !is_python_falsy_json(value)),
                        reject_if_human_queued: payload
                            .get("reject_if_human_queued")
                            .is_some_and(|value| !is_python_falsy_json(value)),
                        input_turn_id: Uuid::new_v4().to_string(),
                        output_turn_id: Uuid::new_v4().to_string(),
                        input_attempt_id,
                        output_attempt_id: Uuid::new_v4().to_string(),
                        queue_binding,
                        created_at_ms: logical_now,
                        committed_at_ms: now_unix_ms,
                        defaults: turn_defaults,
                    }),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.queue.activate" | "turn.queue.cancel" => {
            let queue_id = required_text(&payload, "queue_id", 256)?.to_owned();
            let request = crate::turn::QueueTransitionRequest {
                conversation_id,
                queue_id,
                committed_at_ms: now_unix_ms,
            };
            let step = if admitted.operation.name == "turn.queue.activate" {
                TransactionIrStep::TurnQueueActivate { request, slot: 0 }
            } else {
                TransactionIrStep::TurnQueueCancel { request, slot: 0 }
            };
            (
                vec![step],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.steer.commit" => {
            let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
            let command_id = required_text(&payload, "command_id", 256)?.to_owned();
            let text = required_text(&payload, "text", 1024 * 1024)?.to_owned();
            (
                vec![TransactionIrStep::TurnSteerCommit {
                    request: crate::turn::SteerCommitRequest {
                        conversation_id,
                        attempt_id,
                        command_id,
                        text,
                        updated_at_ms: now_unix_ms,
                        committed_at_ms: now_unix_ms,
                    },
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.visible.sync" => {
            let attempt_id = required_text(&payload, "attempt_id", 128)?.to_owned();
            let root_turn_id = required_text(&payload, "root_turn_id", 128)?.to_owned();
            let live = payload
                .get("live")
                .is_some_and(|value| !is_python_falsy_json(value));
            let start_index = optional_nonnegative_integer(&payload, "start_index", 0)?;
            let default_kind = match payload.get("default_kind") {
                None | Some(Value::Null) => "flow_node".to_owned(),
                Some(Value::String(value)) if value.is_empty() => "flow_node".to_owned(),
                Some(Value::String(value)) if value.chars().count() <= 128 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let run_id = match payload.get("run_id") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(value)) if value.chars().count() <= 256 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let raw_messages = match payload.get("messages") {
                None | Some(Value::Null) => &[][..],
                Some(Value::Array(messages)) => messages.as_slice(),
                Some(value) if is_python_falsy_json(value) => &[][..],
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            if raw_messages.len() > crate::turn::MAX_VISIBLE_TURNS_PER_SYNCHRONIZATION {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            let messages = if live {
                // A live admission carries in-flight phase shells; the ghost
                // partition is a completed-import filter and must never eat
                // them (SQL sidecar parity: `_partition_visible_messages` is
                // skipped entirely for live syncs).
                raw_messages
                    .iter()
                    .map(|message| {
                        message
                            .as_object()
                            .cloned()
                            .ok_or(SemanticExecutionError::InvalidPayload)
                    })
                    .collect::<Result<Vec<_>, _>>()?
            } else {
                let mut dropped = vec![false; raw_messages.len()];
                let empty_text = |value: Option<&Value>| match value {
                    None | Some(Value::Null) | Some(Value::Bool(false)) => true,
                    Some(Value::Number(value)) => value.as_f64() == Some(0.0),
                    Some(Value::String(value)) => value.trim().is_empty(),
                    Some(Value::Array(value)) => value.is_empty(),
                    Some(Value::Object(value)) => value.is_empty(),
                    Some(Value::Bool(true)) => false,
                };
                for (index, message) in raw_messages.iter().enumerate() {
                    if let Some(message) = message.as_object() {
                        if message.get("role").and_then(Value::as_str) == Some("user")
                            && message
                                .get("_isVirtualUser")
                                .is_some_and(|value| !is_python_falsy_json(value))
                            && !message
                                .get("_flowLivePhase")
                                .is_some_and(|value| !is_python_falsy_json(value))
                            && empty_text(message.get("content"))
                        {
                            dropped[index] = true;
                        }
                    }
                }
                for (index, message) in raw_messages.iter().enumerate() {
                    if dropped[index] {
                        continue;
                    }
                    if let Some(message) = message.as_object() {
                        if message.get("role").and_then(Value::as_str) == Some("assistant")
                            && empty_text(message.get("content"))
                            && empty_text(message.get("thinking"))
                            && message.get("finishReason").and_then(Value::as_str)
                                == Some("aborted")
                            && message.get("toolRounds").is_none_or(is_python_falsy_json)
                            && index > 0
                            && dropped[index - 1]
                        {
                            dropped[index] = true;
                        }
                    }
                }
                raw_messages
                    .iter()
                    .enumerate()
                    .filter(|(index, _)| !dropped[*index])
                    .map(|(_, message)| {
                        message
                            .as_object()
                            .cloned()
                            .ok_or(SemanticExecutionError::InvalidPayload)
                    })
                    .collect::<Result<Vec<_>, _>>()?
            };
            // SQL sidecar parity: the visible window is bounded absolutely,
            // and a live sync admits exactly one in-flight phase.
            if start_index.saturating_add(messages.len() as u64)
                > crate::turn::MAX_VISIBLE_PHASE_INDEX
                || (live && messages.len() != 1)
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::TurnVisibleSync {
                    request: Box::new(crate::turn::VisibleSyncRequest {
                        conversation_id,
                        attempt_id,
                        root_turn_id,
                        messages,
                        default_kind,
                        run_id,
                        live,
                        start_index,
                        updated_at_ms: now_unix_ms,
                        committed_at_ms: now_unix_ms,
                    }),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.exists" => (
            vec![TransactionIrStep::TurnExists {
                conversation_id,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"false".to_vec(),
            },
            None,
        ),
        "turn.revision" => (
            vec![TransactionIrStep::TurnRevision {
                conversation_id,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"0".to_vec(),
            },
            None,
        ),
        "turn.timing_trace.list" => {
            let limit = usize::try_from(optional_nonnegative_integer(&payload, "limit", 30)?)
                .ok()
                .filter(|limit| {
                    (1..=crate::generated_tofudb_ir::MAX_TIMING_TRACE_ROWS_PER_QUERY)
                        .contains(limit)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let before_created_at = match payload.get("before_created_at") {
                None | Some(Value::Null) => None,
                Some(value) => Some(
                    value
                        .as_u64()
                        .filter(|value| *value <= i64::MAX as u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                ),
            };
            (
                vec![TransactionIrStep::TurnTimingTraceList {
                    conversation_id,
                    before_created_at,
                    limit,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.get" => {
            let turn_id = required_text(&payload, "turn_id", 128)?.to_owned();
            (
                vec![TransactionIrStep::TurnGet {
                    conversation_id,
                    turn_id,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.image.get" => {
            let turn_id = required_text(&payload, "turn_id", 128)?.to_owned();
            let projection_revision = payload
                .get("projection_revision")
                .and_then(Value::as_u64)
                .filter(|value| *value > 0 && *value <= i64::MAX as u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let image_index = payload
                .get("image_index")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok())
                .filter(|value| *value < crate::generated_tofudb_ir::MAX_LEGACY_TURN_IMAGES)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::TurnImageGet {
                    conversation_id,
                    turn_id,
                    projection_revision,
                    image_index,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.context.read" => {
            let turn_id = required_text(&payload, "turn_id", 128)?.to_owned();
            let replay = match payload.get("replay") {
                None => true,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let after_turn_id = match payload.get("after_turn_id") {
                None => None,
                Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= 128 => {
                    Some(value.clone())
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::TurnContextRead {
                    conversation_id, turn_id, replay, after_turn_id, slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.list" => {
            let lane_id = match payload.get("lane_id") {
                None | Some(Value::Null) => None,
                Some(value) if is_python_falsy_json(value) => None,
                Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= 256 => {
                    Some(value.clone())
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::TurnList {
                    conversation_id,
                    lane_id,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"[]".to_vec(),
                },
                None,
            )
        }
        "turn.list_delta" => {
            let since_ms = optional_nonnegative_integer(&payload, "since_ms", 0)?;
            let mut known_revisions = std::collections::BTreeMap::new();
            if let Some(Value::Object(raw_revisions)) = payload.get("known_revisions") {
                for (turn_id, revision) in raw_revisions.iter().take(2_000) {
                    if !turn_id.is_empty() && turn_id.chars().count() <= 128 {
                        if let Some(revision) = lossy_nonnegative_integer(revision) {
                            known_revisions.insert(turn_id.clone(), revision);
                        }
                    }
                }
            }
            (
                vec![TransactionIrStep::TurnListDelta {
                    conversation_id,
                    since_ms,
                    known_revisions,
                    server_now_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.sync.snapshot" => {
            let max_projection_bytes = optional_nonnegative_integer(&payload, "max_projection_bytes", 0)?;
            if max_projection_bytes != 0 && !(65536..=16777216).contains(&max_projection_bytes) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let turn_limit = optional_nonnegative_integer(&payload, "turn_limit", 0)?;
            if turn_limit > 256 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let include_artifact_hint = match payload.get("include_artifact_hint") {
                None => false,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::TurnSyncSnapshot {
                    conversation_id,
                    turn_limit: turn_limit as usize,
                    max_projection_bytes: max_projection_bytes as usize,
                    include_artifact_hint,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.sync.round_directory" => {
            let lane_id = required_text(&payload, "lane_id", 128)?.to_owned();
            let offset = optional_nonnegative_integer(&payload, "offset", 0)?;
            let limit = optional_nonnegative_integer(&payload, "limit", 50)?;
            let sync_sequence = payload.get("sync_sequence").and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let anchor_turn_id = match payload.get("anchor_turn_id") {
                None => String::new(),
                Some(Value::String(s)) if s.chars().count() <= 128 => s.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            if !(1..=100).contains(&limit) { return Err(SemanticExecutionError::InvalidPayload); }
            (vec![TransactionIrStep::TurnSyncRoundDirectory { conversation_id, lane_id, offset,
                limit: limit as usize, sync_sequence, anchor_turn_id, slot: 0 }],
                TransactionIrResult::SlotOrLiteral { slot: 0, missing: b"null".to_vec() }, None)
        }
        "turn.sync.page" => {
            let max_projection_bytes = optional_nonnegative_integer(&payload, "max_projection_bytes", 0)?;
            if max_projection_bytes != 0 && !(65536..=16777216).contains(&max_projection_bytes) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let lane_id = required_text(&payload, "lane_id", 128)?.to_owned();
            let before_ordinal =
                optional_nonnegative_integer(&payload, "before_ordinal", i64::MAX as u64)?;
            if before_ordinal > i64::MAX as u64 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let limit = optional_nonnegative_integer(&payload, "limit", 64)?;
            if !(1..=256).contains(&limit) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let sync_sequence = payload
                .get("sync_sequence")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::TurnSyncPage {
                    conversation_id,
                    lane_id,
                    before_ordinal,
                    limit: limit as usize,
                    sync_sequence,
                    max_projection_bytes: max_projection_bytes as usize,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.sync.changes" => {
            let after = optional_nonnegative_integer(&payload, "after", 0)?;
            let limit = optional_nonnegative_integer(&payload, "limit", 500)?;
            if !(1..=2_000).contains(&limit) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::TurnSyncChanges {
                    conversation_id,
                    after,
                    limit: limit as usize,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "turn.branch.create" => {
            let parent_turn_id = required_text(&payload, "parent_turn_id", 128)?.to_owned();
            let expected_projection_revision = payload
                .get("expected_projection_revision")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let lane_id =
                deterministic_branch_lane_id(admitted, &conversation_id, &parent_turn_id)?;
            let title = legacy_text_field(&payload, "title", "Branch", 200)?;
            let kind = legacy_text_field(&payload, "kind", "branch", 80)?;
            let anchor_text = legacy_text_field(&payload, "anchor_text", "", 1_000)?;
            let parent_selection = legacy_text_field(&payload, "parent_selection", "", 10_000)?;
            (
                vec![TransactionIrStep::TurnBranchCreate {
                    conversation_id,
                    parent_turn_id,
                    lane_id,
                    title,
                    kind,
                    anchor_text,
                    parent_selection,
                    expected_projection_revision,
                    updated_at_ms: now_unix_ms,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.branch.delete" => {
            let parent_turn_id = required_text(&payload, "parent_turn_id", 128)?.to_owned();
            let lane_id = required_text(&payload, "lane_id", 128)?.to_owned();
            (
                vec![TransactionIrStep::TurnBranchDelete {
                    conversation_id,
                    parent_turn_id,
                    lane_id,
                    deleted_at_ms: now_unix_ms,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.delete" => {
            let raw_turn_ids = payload
                .get("turn_ids")
                .and_then(Value::as_array)
                .filter(|turn_ids| !turn_ids.is_empty() && turn_ids.len() <= 256)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut seen = std::collections::BTreeSet::new();
            let mut turn_ids = Vec::with_capacity(raw_turn_ids.len());
            for value in raw_turn_ids {
                let turn_id = value
                    .as_str()
                    .filter(|turn_id| !turn_id.is_empty() && turn_id.chars().count() <= 128)
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                if seen.insert(turn_id.to_owned()) {
                    turn_ids.push(turn_id.to_owned());
                }
            }
            (
                vec![TransactionIrStep::TurnDelete {
                    conversation_id,
                    turn_ids,
                    deleted_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.projection.update" => {
            let turn_id = required_text(&payload, "turn_id", 128)?.to_owned();
            let expected_projection_revision = payload
                .get("expected_projection_revision")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let projection = match payload.get("projection") {
                Some(Value::Object(projection)) => normalized_turn_projection(projection.clone()),
                Some(Value::String(content)) => {
                    Map::from_iter([("content".to_owned(), Value::String(content.clone()))])
                }
                Some(Value::Bool(true)) => {
                    Map::from_iter([("content".to_owned(), Value::String("True".to_owned()))])
                }
                Some(Value::Number(number)) if number.as_f64() != Some(0.0) => {
                    Map::from_iter([("content".to_owned(), Value::String(number.to_string()))])
                }
                Some(Value::Array(values)) if !values.is_empty() => Map::from_iter([(
                    "content".to_owned(),
                    Value::String(
                        serde_json::to_string(values)
                            .map_err(|_| SemanticExecutionError::InvalidPayload)?,
                    ),
                )]),
                _ => Map::from_iter([("content".to_owned(), Value::String(String::new()))]),
            };
            (
                vec![TransactionIrStep::TurnProjectionUpdate {

                    human_guidance_answer_json: match payload.get("human_guidance_answer") {
                        None => None,
                        Some(Value::Object(answer)) => Some(canonical_json(&Value::Object(answer.clone()))?),
                        _ => return Err(SemanticExecutionError::InvalidPayload),
                    },
                    preserve_activity: match payload.get("preserve_activity") {
                        None => false,
                        Some(Value::Bool(value)) => *value,
                        _ => return Err(SemanticExecutionError::InvalidPayload),
                    },
                    conversation_id,
                    turn_id,
                    projection_json: canonical_json(&Value::Object(projection))?,
                    expected_projection_revision,
                    updated_at_ms: now_unix_ms,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "turn.append_settled" => {
            let actor = required_text(&payload, "actor", 32)?.to_owned();
            if !matches!(
                actor.as_str(),
                "human" | "assistant" | "planner" | "critic" | "virtual_user"
            ) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let status = match payload.get("status") {
                None | Some(Value::Null) => "completed".to_owned(),
                Some(Value::String(value))
                    if matches!(
                        value.as_str(),
                        "completed" | "failed" | "interrupted" | "truncated" | "superseded"
                    ) =>
                {
                    value.clone()
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let mut raw_fields = append_settled_raw
                .take()
                .expect("the append_settled admission split carries raw fields");
            // The receipt canonical bytes/digest/outbox must cover the FULL
            // payload: compute them before the big documents leave the raw
            // map (canonical_request_raw is byte-identical to the typed path
            // for the compact wire producers).
            let canonical = canonical_request_raw(
                admitted.operation.name,
                &raw_fields,
                admitted
                    .request
                    .payload
                    .len()
                    .saturating_add(admitted.operation.name.len() + 32),
            );
            let (projection_json, projection_timestamp) = match raw_fields.remove("projection") {
                None => (canonical_json(&Value::Object(Map::new()))?, None),
                Some(value) if value.get() == "null" => {
                    (canonical_json(&Value::Object(Map::new()))?, None)
                }
                Some(value) => {
                    let projection: std::collections::BTreeMap<
                        String,
                        Box<serde_json::value::RawValue>,
                    > = serde_json::from_str(value.get())
                        .map_err(|_| SemanticExecutionError::InvalidPayload)?;
                    let projection_timestamp = projection
                        .get("timestamp")
                        .map(|raw| serde_json::from_str(raw.get()))
                        .transpose()
                        .map_err(|_| SemanticExecutionError::InvalidPayload)?;
                    let projection_json =
                        serde_json::to_vec(&normalized_turn_projection_raw(projection))
                            .map_err(|_| SemanticExecutionError::InvalidPayload)?;
                    (projection_json, projection_timestamp)
                }
            };
            let settlement_json = match raw_fields.remove("settlement") {
                None => canonical_json(&json!({
                    "outcome": status, "cause": "ingested", "resumeOptions": []
                }))?,
                Some(value) if value.get() == "null" => canonical_json(&json!({
                    "outcome": status, "cause": "ingested", "resumeOptions": []
                }))?,
                Some(value) => {
                    let settlement: std::collections::BTreeMap<
                        String,
                        Box<serde_json::value::RawValue>,
                    > = serde_json::from_str(value.get())
                        .map_err(|_| SemanticExecutionError::InvalidPayload)?;
                    serde_json::to_vec(&settlement)
                        .map_err(|_| SemanticExecutionError::InvalidPayload)?
                }
            };
            let lane_id = match payload.get("lane_id") {
                None | Some(Value::Null) => "main".to_owned(),
                Some(Value::String(value)) if value.is_empty() => "main".to_owned(),
                Some(Value::String(value)) if value.chars().count() <= 256 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let payload_command_id = required_text(&payload, "command_id", 256)?.to_owned();
            let kind = match payload.get("kind") {
                None | Some(Value::Null) => "ingested".to_owned(),
                Some(Value::String(value)) if value.is_empty() => "ingested".to_owned(),
                Some(Value::String(value)) if value.chars().count() <= 128 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let run_id = match payload.get("run_id") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(value)) if value.chars().count() <= 256 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let turn_id = match payload.get("turn_id") {
                None | Some(Value::Null) => Uuid::new_v4().to_string(),
                Some(Value::String(value)) if value.is_empty() => Uuid::new_v4().to_string(),
                Some(Value::String(value)) if value.chars().count() <= 128 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let attempt_id = (actor != "human").then(|| Uuid::new_v4().to_string());
            let logical_now = match payload.get("now") {
                None | Some(Value::Null) => now_unix_ms,
                Some(value) => value
                    .as_u64()
                    .filter(|value| *value > 0)
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            };
            let created_at_ms = optional_nonnegative_integer(&payload, "created_at", logical_now)?;
            let defaults = match payload.get("conversation_defaults") {
                None | Some(Value::Null) => Map::new(),
                Some(Value::Object(value)) => value.clone(),
                Some(value) if is_python_falsy_json(value) => Map::new(),
                Some(_) => Map::new(),
            };
            let allow_create = defaults
                .get("allowCreate")
                .is_some_and(|value| !is_python_falsy_json(value));
            let mut default_settings = defaults
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            default_settings.remove("activeTaskId");
            let default_title = defaults
                .get("title")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or("New Chat")
                .chars()
                .take(500)
                .collect();
            let default_created_at_ms = defaults
                .get("createdAt")
                .and_then(Value::as_u64)
                .filter(|value| *value > 0)
                .unwrap_or(logical_now);
            (
                vec![TransactionIrStep::TurnAppendSettled {
                    conversation_id,
                    actor,
                    status,
                    projection_json,
                    projection_timestamp,
                    settlement_json,
                    lane_id,
                    command_id: payload_command_id,
                    kind,
                    run_id,
                    turn_id,
                    attempt_id,
                    created_at_ms,
                    committed_at_ms: now_unix_ms,
                    allow_create,
                    default_title,
                    default_settings_json: canonical_json(&Value::Object(default_settings))?,
                    default_created_at_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                Some(command_effects_from_canonical(
                    admitted,
                    &payload,
                    now_unix_ms,
                    canonical,
                    None,
                )?),
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: effects,
    })
}

fn optional_expected_version(
    payload: &Map<String, Value>,
) -> Result<Option<u64>, SemanticExecutionError> {
    match payload.get("expected_version") {
        None => Ok(None),
        Some(Value::Number(number)) => number
            .as_u64()
            .map(Some)
            .ok_or(SemanticExecutionError::InvalidPayload),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn record_composite_key(namespace: &str, key: &str) -> Result<Vec<u8>, SemanticExecutionError> {
    crate::versioned_document::record_composite_key(namespace, key).map_err(map_storage_error)
}

fn record_key(
    tenant_id: u64,
    owner_user_id: u64,
    namespace: &str,
    key: &str,
) -> Result<EntityKey, SemanticExecutionError> {
    crate::versioned_document::record_key(tenant_id, owner_user_id, namespace, key)
        .map_err(map_storage_error)
}

fn compile_record(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let namespace = required_text(&payload, "namespace", 128)?.to_owned();
    let namespace_for_guard = namespace.clone();
    if matches!(namespace.as_str(), crate::generated_project_dispatch_storage::HEADS | crate::generated_project_dispatch_storage::DOCUMENTS) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    if namespace == "tool_result_private" {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    if matches!(namespace.as_str(), crate::native_checkpoint_store::HEADERS | crate::native_checkpoint_store::SNAPSHOTS | crate::native_checkpoint_store::REPLAY) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    if matches!(namespace.as_str(), "automation.runs.v1" | "automation.active.v1" | "automation.history.v1" | "automation.subscriptions.v1" | "automation.event-failures.v1" | "automation.presentations.v1" | "automation.targets.v1" | "automation.skillSources.v1")
        && matches!(admitted.operation.name, "record.put" | "record.delete") {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    // Task-result reads must pass through their tenant-global owner header
    // and bounded payload codec. Generic record mutations remain forbidden.
    if namespace == "task_results" {
        return if admitted.operation.name == "record.get" {
            compile_task_result(admitted, now_unix_ms)
        } else {
            Err(SemanticExecutionError::UnsupportedOperation)
        };
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = admitted.request.owner_id;
    let (mut steps, result) = match admitted.operation.name {
        "record.get" => {
            let logical_key = required_text(&payload, "key", 512)?.to_owned();
            let key = record_key(tenant_id, owner_user_id, &namespace, &logical_key)?;
            (
                vec![TransactionIrStep::VersionedDocumentGet {
                    key,
                    namespace,
                    logical_key,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "record.list" => {

            let include_value = match payload.get("include_value") {
                None => true,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let prefix = match payload.get("prefix") {
                None => "",
                Some(Value::String(prefix)) if prefix.chars().count() <= 512 => prefix,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let limit = match payload.get("limit") {
                None => 100,
                Some(Value::Number(number)) => number
                    .as_u64()
                    .and_then(|limit| usize::try_from(limit).ok())
                    .filter(|limit| (1..=1000).contains(limit))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let cursor = |name| match payload.get(name) {
                None => Ok(""),
                Some(Value::String(value)) if value.chars().count() <= 512 && value.starts_with(prefix) => Ok(value.as_str()),
                Some(Value::String(value)) if value.is_empty() => Ok(""),
                _ => Err(SemanticExecutionError::InvalidPayload),
            };
            let after = cursor("after_key")?;
            let before = cursor("before_key")?;
            if !after.is_empty() && !before.is_empty() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let composite_prefix = record_composite_key(&namespace, prefix)?;
            let (mut start, mut end) = EntityKey::prefix_range(
                tenant_id,
                owner_user_id,
                RECORD_DOCUMENT_NAMESPACE,
                &composite_prefix,
            )
            .map_err(map_storage_error)?;
            if !after.is_empty() {
                start = record_key(tenant_id, owner_user_id, &namespace, after)?
                    .exact_range().map_err(map_storage_error)?.1;
            }
            if !before.is_empty() {
                end = record_key(tenant_id, owner_user_id, &namespace, before)?;
            }
            (
                vec![TransactionIrStep::VersionedDocumentList {

                    include_value,
                    start,
                    end,
                    namespace,
                    limit,
                    reverse: !before.is_empty(),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"[]".to_vec(),
                },
            )
        }
        "record.put" => {
            let logical_key = required_text(&payload, "key", 512)?.to_owned();
            let key = record_key(tenant_id, owner_user_id, &namespace, &logical_key)?;
            let value_json = canonical_json(payload.get("value").unwrap_or(&Value::Null))?;
            (
                vec![TransactionIrStep::VersionedDocumentPut {
                    key,
                    namespace,
                    logical_key,
                    value_json,
                    expected_version: optional_expected_version(&payload)?,
                    updated_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "record.delete" => {
            let logical_key = required_text(&payload, "key", 512)?.to_owned();
            let key = record_key(tenant_id, owner_user_id, &namespace, &logical_key)?;
            (
                vec![TransactionIrStep::VersionedDocumentDelete {
                    key,
                    namespace,
                    logical_key,
                    expected_version: optional_expected_version(&payload)?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    if namespace_for_guard == "automation.definitions.v1" && matches!(admitted.operation.name,"record.put"|"record.delete") {
        if admitted.operation.name=="record.put" && ["grant","triggerState","publicationCommand"].iter()
            .any(|field|payload.get("value").and_then(|v|v.get(*field)).is_some_and(|v|!v.is_null())) {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        steps.insert(0,TransactionIrStep::AutomationDefinitionDraftGuard {key:required_text(&payload,"key",512)?.to_owned()});
    }
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_recent_project(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let (steps, result) = match admitted.operation.name {
        "project.recent.list" => (
            vec![TransactionIrStep::RecentProjectList { slot: 0 }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"[]".to_vec(),
            },
        ),
        "project.recent.touch" => {
            let path = required_text(&payload, "project_path", MAX_RECENT_PROJECT_PATH_CHARACTERS)?
                .to_owned();
            let last_used = payload
                .get("last_used")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::RecentProjectTouch {
                    path,
                    last_used,
                    updated_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "project.recent.touch_many" => {
            let values = payload
                .get("project_paths")
                .and_then(Value::as_array)
                .filter(|paths| !paths.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut paths = Vec::with_capacity(values.len());
            let mut seen = std::collections::BTreeSet::new();
            for value in values {
                let path = value
                    .as_str()
                    .filter(|path| {
                        !path.is_empty()
                            && path.chars().count() <= MAX_RECENT_PROJECT_PATH_CHARACTERS
                    })
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                if seen.insert(path) {
                    paths.push(path.to_owned());
                }
            }
            let last_used = payload
                .get("last_used")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::RecentProjectTouchMany {
                    paths,
                    last_used,
                    updated_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "project.recent.clear" => (
            vec![TransactionIrStep::RecentProjectClear { slot: 0 }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_project_relink(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let old_path =
        required_text(&payload, "old_path", MAX_RECENT_PROJECT_PATH_CHARACTERS)?.to_owned();
    let new_path =
        required_text(&payload, "new_path", MAX_RECENT_PROJECT_PATH_CHARACTERS)?.to_owned();
    if old_path == new_path {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::ProjectRelink {
            old_path,
            new_path,
            updated_at_ms: now_unix_ms,
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: Vec::new(),
        },
        command_effects,
    })
}

fn log_aggregate_optional_text(
    payload: &Map<String, Value>,
    field: &str,
    default: &str,
    maximum: usize,
) -> Result<String, SemanticExecutionError> {
    match payload.get(field) {
        None => Ok(default.to_owned()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn compile_log_aggregate(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = admitted.request.owner_id;
    let (steps, result) = match admitted.operation.name {
        "log_aggregate.flush" => {
            let values = payload
                .get("rows")
                .and_then(Value::as_array)
                .filter(|rows| {
                    rows.len() <= crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_FLUSH_BATCH
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut rows = Vec::with_capacity(values.len());
            for value in values {
                let item = value
                    .as_object()
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                rows.push(crate::log_aggregate::FlushRow {
                    fingerprint: required_text(
                        item,
                        "fingerprint",
                        crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS,
                    )?
                    .to_owned(),
                    level: required_text(
                        item,
                        "level",
                        crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_LEVEL_CHARACTERS,
                    )?
                    .to_owned(),
                    logger: log_aggregate_optional_text(
                        item,
                        "logger",
                        "",
                        crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_LOGGER_CHARACTERS,
                    )?,
                    template: log_aggregate_optional_text(
                        item,
                        "template",
                        "",
                        crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS,
                    )?,
                    sample: log_aggregate_optional_text(
                        item,
                        "sample",
                        "",
                        crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS,
                    )?,
                    count: item
                        .get("count")
                        .and_then(Value::as_u64)
                        .filter(|count| {
                            (1..=crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_ROW_COUNT)
                                .contains(count)
                        })
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    first_seen: item
                        .get("first_seen")
                        .and_then(Value::as_u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    last_seen: item
                        .get("last_seen")
                        .and_then(Value::as_u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                });
            }
            let cutoff_ms = match payload.get("cutoff_ms") {
                None | Some(Value::Null) => None,
                Some(value) => Some(
                    value
                        .as_u64()
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                ),
            };
            let rows_json =
                serde_json::to_vec(&rows).map_err(|_| SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::LogAggregateFlush {
                    rows_json,
                    cutoff_ms,
                    updated_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "log_aggregate.query" => {
            let level = log_aggregate_optional_text(
                &payload,
                "level",
                "",
                crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_LEVEL_CHARACTERS,
            )?;
            let q = log_aggregate_optional_text(
                &payload,
                "q",
                "",
                crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS,
            )?;
            let sort = match log_aggregate_optional_text(&payload, "sort", "count", 32)?.as_str() {
                "count" => crate::log_aggregate::QuerySort::Count,
                "last_seen" => crate::log_aggregate::QuerySort::LastSeen,
                "level" => crate::log_aggregate::QuerySort::Level,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let limit = match payload.get("limit") {
                None => 100_usize,
                Some(value) => usize::try_from(
                    value
                        .as_u64()
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                )
                .ok()
                .filter(|limit| {
                    (1..=crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_QUERY_ROWS).contains(limit)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            };
            (
                vec![TransactionIrStep::LogAggregateQuery {
                    level,
                    q,
                    sort,
                    limit,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: br#"{"items":[],"total_rows":0,"total_events":0}"#.to_vec(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_plugin(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = admitted.request.owner_id;
    let (steps, result) = match admitted.operation.name {
        "plugin.register" => {
            // A missing manifest reaches execution as JSON null so the legacy
            // plugin_storage_incompatible rejection shape is preserved.
            let manifest = payload.get("manifest").cloned().unwrap_or(Value::Null);
            let manifest_json = canonical_json(&manifest)?;
            (
                vec![TransactionIrStep::PluginRegister {
                    manifest_json,
                    updated_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "plugin.manifest.get" => {
            // Legacy `_required_text`: a missing, empty, non-string, or
            // over-long namespace is a database_protocol_error.
            let namespace = match payload.get("namespace") {
                Some(Value::String(value))
                    if !value.is_empty()
                        && value.chars().count()
                            <= crate::generated_tofudb_ir::MAX_PLUGIN_NAMESPACE_CHARACTERS =>
                {
                    value.clone()
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::PluginManifestGet { namespace, slot: 0 }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        _ => {
            // Manifest-declared row operation: `plugin.<namespace>.<operation>`
            // (the namespace itself may contain dots; the operation name is
            // the final segment). The manifest — read at execution time — is
            // the only authority for the operation's existence and kind.
            let (namespace, operation) = admitted
                .operation
                .name
                .strip_prefix("plugin.")
                .and_then(|rest| rest.rsplit_once('.'))
                .filter(|(namespace, operation)| {
                    !namespace.is_empty()
                        && namespace.chars().count()
                            <= crate::generated_tofudb_ir::MAX_PLUGIN_NAMESPACE_CHARACTERS
                        && !operation.is_empty()
                        && operation.chars().count() <= 63
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::PluginRowOperation {
                    request: crate::plugin_rows::DynRequest {
                        namespace: namespace.to_owned(),
                        operation: operation.to_owned(),
                        command: admitted.operation.kind.mutates_state(),
                        payload: payload.clone(),
                        updated_at_ms: now_unix_ms,
                    },
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}
fn compile_optimizer(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let optional =
        |field: &str, maximum: usize| log_aggregate_optional_text(&payload, field, "", maximum);
    let limit = |field: &str, default: usize, maximum: usize| match payload.get(field) {
        None => Ok(default),
        Some(value) => value
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .filter(|value| (1..=maximum).contains(value))
            .ok_or(SemanticExecutionError::InvalidPayload),
    };
    let boolean = |field: &str, default: bool| match payload.get(field) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let request = match admitted.operation.name {
        "optimizer.proposal.create" => crate::optimizer::Request::ProposalCreate {
            proposal: crate::optimizer::Proposal {
                user_id: owner_user_id,
                id: required_text(&payload, "proposal_id", 128)?.to_owned(),
                created_at: required_text(&payload, "created_at", 64)?.to_owned(),
                title: optional("title", usize::MAX)?,
                rationale: optional("rationale", usize::MAX)?,
                action_type: required_text(&payload, "action_type", 256)?.to_owned(),
                action_args: required_text(&payload, "action_args", usize::MAX)?.to_owned(),
                severity: log_aggregate_optional_text(&payload, "severity", "low", 64)?,
                confidence: payload
                    .get("confidence")
                    .and_then(Value::as_f64)
                    .filter(|value| value.is_finite() && (0.0..=1.0).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                evidence: required_text(&payload, "evidence", usize::MAX)?.to_owned(),
                status: log_aggregate_optional_text(&payload, "status", "pending_review", 64)?,
                status_reason: optional("status_reason", usize::MAX)?,
            },
            updated_at_ms: now_unix_ms,
        },
        "optimizer.proposal.update" => crate::optimizer::Request::ProposalUpdate {
            id: required_text(&payload, "proposal_id", 128)?.to_owned(),
            status: required_text(&payload, "status", 64)?.to_owned(),
            reason: optional("reason", usize::MAX)?,
            updated_at_ms: now_unix_ms,
        },
        "optimizer.proposal.get" => crate::optimizer::Request::ProposalGet {
            id: required_text(&payload, "proposal_id", 128)?.to_owned(),
        },
        "optimizer.proposal.list" => crate::optimizer::Request::ProposalList {
            status: optional("status", 64)?,
            limit: limit("limit", crate::generated_tofudb_ir::DEFAULT_OPTIMIZER_LIST_ROWS, usize::MAX)?,
        },
        "optimizer.action.record" => crate::optimizer::Request::ActionRecord {
            action: crate::optimizer::Action {
                user_id: owner_user_id,
                id: required_text(&payload, "log_id", 128)?.to_owned(),
                proposal_id: required_text(&payload, "proposal_id", 128)?.to_owned(),
                applied_at: required_text(&payload, "applied_at", 64)?.to_owned(),
                expires_at: required_text(&payload, "expires_at", 64)?.to_owned(),
                pre_metric: required_text(&payload, "pre_metric", usize::MAX)?.to_owned(),
                outcome_metric: String::new(),
                outcome_recorded_at: String::new(),
                reverted_at: String::new(),
                revert_reason: String::new(),
            },
            updated_at_ms: now_unix_ms,
        },
        "optimizer.action.outcome" => crate::optimizer::Request::ActionOutcome {
            id: required_text(&payload, "log_id", 128)?.to_owned(),
            metric: required_text(&payload, "outcome_metric", usize::MAX)?.to_owned(),
            recorded_at: required_text(&payload, "recorded_at", 64)?.to_owned(),
            updated_at_ms: now_unix_ms,
        },
        "optimizer.action.revert" => crate::optimizer::Request::ActionRevert {
            id: required_text(&payload, "log_id", 128)?.to_owned(),
            reverted_at: required_text(&payload, "reverted_at", 64)?.to_owned(),
            reason: optional("reason", usize::MAX)?,
            updated_at_ms: now_unix_ms,
        },
        "optimizer.action.list" => crate::optimizer::Request::ActionList {
            include_reverted: boolean("include_reverted", false)?,
            limit: limit("limit", crate::generated_tofudb_ir::DEFAULT_OPTIMIZER_LIST_ROWS, usize::MAX)?,
        },
        "optimizer.action.expired" => crate::optimizer::Request::ActionExpired {
            now_iso: required_text(&payload, "now_iso", 64)?.to_owned(),
        },
        "optimizer.action.for_proposal" => crate::optimizer::Request::ActionForProposal {
            proposal_id: required_text(&payload, "proposal_id", 128)?.to_owned(),
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Optimizer { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_paper_document(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let command_effects = if admitted.operation.kind.mutates_state() {
        Some(command_effects(admitted, &payload, now_unix_ms)?)
    } else {
        None
    };
    let request =
        crate::paper_document::Request::parse(admitted.operation.name, payload, now_unix_ms)
            .map_err(map_storage_error)?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::PaperDocument { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_paper_build_artifact(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let request =
        crate::paper_build_artifact::Request::parse(admitted.operation.name, &payload, now_unix_ms)
            .map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload: Map<String, Value> = payload
            .iter()
            .filter(|(field, _)| !matches!(field.as_str(), "data" | "receipt"))
            .map(|(field, value)| (field.clone(), value.clone()))
            .collect();
        for field in ["data", "receipt"] {
            if let Some(value) = payload.get(field) {
                let (digest, bytes) = crate::paper_build_receipt::digest_bounded(
                    value,
                    MAX_LEGACY_DOCUMENT_BYTES,
                )
                .map_err(map_storage_error)?;
                replay_payload.insert(
                    format!("{field}_sha256"),
                    Value::String(digest.iter().map(|byte| format!("{byte:02x}")).collect()),
                );
                replay_payload.insert(format!("{field}_bytes"), Value::from(bytes));
            }
        }
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(&json!({
                "operation":admitted.operation.name,"payload":replay_payload,"contentAuthority":"owner-bound-retained-paper-build-documents"
            }))?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::PaperBuildArtifact { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_paper_artifact(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let optional_text = |field: &str, default: &str, maximum: usize| match payload.get(field) {
        None => Ok(default.to_owned()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let unsigned = |field: &str, default: Option<u64>| {
        payload
            .get(field)
            .map(|value| value.as_u64().ok_or(SemanticExecutionError::InvalidPayload))
            .transpose()?
            .or(default)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let report_scope = || -> Result<(String, String), SemanticExecutionError> {
        Ok((
            required_text(&payload, "paper_hash", 128)?.to_owned(),
            required_text(&payload, "lang", 64)?.to_owned(),
        ))
    };
    let request = match admitted.operation.name {
        "paper.report.upsert" => {
            let (paper_hash, lang) = report_scope()?;
            crate::paper_artifact::Request::ReportUpsert(Box::new(
                crate::paper_artifact::ReportPut {
                    paper_hash,
                    lang,
                    report: optional_text("report", "", 10_000_000)?,
                    model: optional_text("model", "", 512)?,
                    meta: match payload.get("meta") {
                        None => Map::new(),
                        Some(Value::Object(value)) => value.clone(),
                        _ => return Err(SemanticExecutionError::InvalidPayload),
                    },
                    created_at: unsigned("created_at", None)?,
                    physical_updated_at_ms: now_unix_ms,
                },
            ))
        }
        "paper.report.get" => {
            let (paper_hash, lang) = report_scope()?;
            crate::paper_artifact::Request::ReportGet {
                paper_hash,
                lang,
                max_report_characters: match payload.get("max_report_chars") {
                    None | Some(Value::Null) => None,
                    Some(value) => Some(
                        usize::try_from(
                            value
                                .as_u64()
                                .ok_or(SemanticExecutionError::InvalidPayload)?,
                        )
                        .map_err(|_| SemanticExecutionError::InvalidPayload)?,
                    ),
                },
            }
        }
        "paper.report.resolve" => crate::paper_artifact::Request::ReportResolve {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            preferred_lang: required_text(&payload, "preferred_lang", 64)?.to_owned(),
            fallback_lang: match payload.get("fallback_lang") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= 64 => {
                    Some(value.clone())
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            },
        },
        "paper.report.reopen" => {
            let preferred_lang = required_text(&payload, "preferred_lang", 64)?.to_owned();
            let fallback_lang = match payload.get("fallback_lang") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= 64 => {
                    Some(value.clone())
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let offered = match payload.get("sibling_langs_by_base") {
                None => Map::new(),
                Some(Value::Object(value)) => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let mut groups = BTreeMap::new();
            let mut offered_count = 0usize;
            for (base, values) in offered {
                let values = values
                    .as_array()
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                offered_count += values.len();
                if offered_count > 8 {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                let mut normalized = Vec::new();
                for value in values {
                    let lang = value
                        .as_str()
                        .map(str::trim)
                        .filter(|value| !value.is_empty() && value.chars().count() <= 64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?;
                    if !normalized.iter().any(|value| value == lang) {
                        normalized.push(lang.to_owned());
                    }
                }
                groups.insert(base, normalized);
            }
            crate::paper_artifact::Request::ReportReopen {
                paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
                preferred_lang,
                fallback_lang,
                sibling_langs_by_base: groups,
            }
        }
        "paper.report.excerpts" => {
            let offered = payload
                .get("paper_hashes")
                .and_then(Value::as_array)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if offered.len() > 40 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let mut hashes = Vec::new();
            for value in offered {
                let hash = value
                    .as_str()
                    .map(str::trim)
                    .filter(|value| !value.is_empty() && value.chars().count() <= 128)
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                if !hashes.iter().any(|value| value == hash) {
                    hashes.push(hash.to_owned());
                }
            }
            crate::paper_artifact::Request::ReportExcerpts {
                paper_hashes: hashes,
                lang: required_text(&payload, "lang", 64)?.to_owned(),
                max_report_characters: usize::try_from(unsigned("max_report_chars", None)?)
                    .map_err(|_| SemanticExecutionError::InvalidPayload)?,
            }
        }
        "paper.report.latest" => crate::paper_artifact::Request::ReportLatest {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
        },
        "paper.report.second_pass.merge" => {
            let (paper_hash, lang) = report_scope()?;
            crate::paper_artifact::Request::ReportSecondPassMerge {
                paper_hash,
                lang,
                name: required_text(&payload, "name", 64)?.to_owned(),
                entry: payload
                    .get("entry")
                    .and_then(Value::as_object)
                    .cloned()
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                physical_updated_at_ms: now_unix_ms,
            }
        }
        "paper.report.second_pass.accumulate" => {
            let (paper_hash, lang) = report_scope()?;
            let raw_usage = payload
                .get("usage")
                .and_then(Value::as_object)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut usage = BTreeMap::new();
            for key in [
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            ] {
                usage.insert(
                    key.to_owned(),
                    raw_usage
                        .get(key)
                        .map(|value| value.as_u64().ok_or(SemanticExecutionError::InvalidPayload))
                        .transpose()?
                        .unwrap_or(0),
                );
            }
            let cost = |field: &str| {
                payload
                    .get(field)
                    .map(|value| value.as_f64().ok_or(SemanticExecutionError::InvalidPayload))
                    .transpose()
                    .map(|value| value.unwrap_or(0.0))
            };
            crate::paper_artifact::Request::ReportSecondPassAccumulate {
                paper_hash,
                lang,
                name: required_text(&payload, "name", 64)?.to_owned(),
                usage,
                cost_cny: cost("costCny")?,
                cost_usd: cost("costUsd")?,
                physical_updated_at_ms: now_unix_ms,
            }
        }
        "paper.translation.upsert" => crate::paper_artifact::Request::TranslationUpsert(Box::new(
            crate::paper_artifact::TranslationPut {
                paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
                lang: required_text(&payload, "lang", 128)?.to_owned(),
                text: optional_text("text", "", 2_000_000)?,
                model: optional_text("model", "", 512)?,
                created_at: unsigned("created_at", None)?,
                physical_updated_at_ms: now_unix_ms,
            },
        )),
        "paper.translation.get" => crate::paper_artifact::Request::TranslationGet {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            lang: required_text(&payload, "lang", 128)?.to_owned(),
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        for field in ["report", "meta", "entry", "text"] {
            if let Some(value) = replay_payload.remove(field) {
                let encoded = canonical_json(&value)?;
                replay_payload.insert(
                    format!("{field}_sha256"),
                    Value::String(format!("{:x}", Sha256::digest(&encoded))),
                );
                replay_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
            }
        }
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(
                &json!({"operation": admitted.operation.name, "payload": replay_payload, "contentAuthority": "owner-bound-paper-artifact-documents"}),
            )?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::PaperArtifact { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_paper_podcast(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = if admitted.operation.name == "paper.podcast.mark_interrupted" {
        admitted.request.owner_id
    } else {
        let requested = positive_identity(&payload, "user_id")?;
        if requested != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        requested
    };
    let optional_text = |field: &str, maximum: usize| match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let unsigned = |field: &str| {
        payload
            .get(field)
            .and_then(Value::as_u64)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let podcast_key = || -> Result<crate::paper_podcast::PodcastKey, SemanticExecutionError> {
        Ok(crate::paper_podcast::PodcastKey {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            mode: required_text(&payload, "mode", 64)?.to_owned(),
            lang: required_text(&payload, "lang", 32)?.to_owned(),
            voice: optional_text("voice", 256)?,
        })
    };
    let request = match admitted.operation.name {
        "paper.podcast.upsert" => {
            let object_or_empty = |field: &str| match payload.get(field) {
                None => Ok(Map::new()),
                Some(Value::Object(value)) => Ok(value.clone()),
                _ => Err(SemanticExecutionError::InvalidPayload),
            };
            let duration_sec = payload
                .get("duration_sec")
                .and_then(Value::as_f64)
                .filter(|value| value.is_finite())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            crate::paper_podcast::Request::Upsert(Box::new(crate::paper_podcast::PodcastPut {
                key: podcast_key()?,
                status: required_text(&payload, "status", 64)?.to_owned(),
                script: object_or_empty("script")?,
                file_path: optional_text("file_path", 10_000)?,
                duration_sec,
                model: optional_text("model", 512)?,
                tts_model: optional_text("tts_model", 512)?,
                meta: object_or_empty("meta")?,
                created_at: unsigned("created_at")?,
                updated_at: unsigned("updated_at")?,
                physical_updated_at_ms: now_unix_ms,
            }))
        }
        "paper.podcast.get" => crate::paper_podcast::Request::Get(podcast_key()?),
        "paper.podcast.mark_interrupted" => crate::paper_podcast::Request::MarkInterrupted {
            updated_at: unsigned("updated_at")?,
            physical_updated_at_ms: now_unix_ms,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        for field in ["script", "meta", "file_path"] {
            if let Some(value) = replay_payload.remove(field) {
                let encoded = canonical_json(&value)?;
                replay_payload.insert(
                    format!("{field}_sha256"),
                    Value::String(format!("{:x}", Sha256::digest(&encoded))),
                );
                replay_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
            }
        }
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(&json!({
                "operation": admitted.operation.name,
                "payload": replay_payload,
                "contentAuthority": "owner-bound-paper-podcast-documents"
            }))?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::PaperPodcast { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_paper_library(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let optional_text = |field: &str, default: &str, maximum: usize| match payload.get(field) {
        None => Ok(default.to_owned()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let unsigned = |field: &str, default: Option<u64>| {
        payload
            .get(field)
            .map(|value| value.as_u64().ok_or(SemanticExecutionError::InvalidPayload))
            .transpose()?
            .or(default)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let request = match admitted.operation.name {
        "paper.library.put" => {
            crate::paper_library::Request::Put(Box::new(crate::paper_library::PaperPut {
                paper_id: required_text(&payload, "id", 256)?.to_owned(),
                title: optional_text("title", "", 1_000)?,
                pdf_url: optional_text("pdf_url", "", 10_000)?,
                pdf_filename: optional_text("pdf_filename", "", 2_000)?,
                arxiv_id: optional_text("arxiv_id", "", 256)?,
                paper_hash: optional_text("paper_hash", "", 128)?,
                parsed_text: optional_text("parsed_text", "", 20_000_000)?,
                parser_version: optional_text("parser_version", "", 256)?,
                qa_history_json: optional_text("qa_history", "[]", 10_000_000)?,
                images_json: optional_text("images", "[]", 10_000_000)?,
                babel_cache_json: optional_text("babel_cache", "{}", 10_000_000)?,
                page_count: unsigned("page_count", Some(0))?,
                folder_id: optional_text("folder_id", "", 512)?,
                created_at: unsigned("created_at", None)?,
                updated_at: unsigned("updated_at", None)?,
                physical_updated_at_ms: now_unix_ms,
            }))
        }
        "paper.library.delete" => crate::paper_library::Request::Delete {
            paper_id: required_text(&payload, "id", 256)?.to_owned(),
        },
        "paper.library.recent" => crate::paper_library::Request::Recent {
            exclude_paper_hash: optional_text("exclude_paper_hash", "", 128)?,
            limit: usize::try_from(unsigned("limit", Some(40))?)
                .map_err(|_| SemanticExecutionError::InvalidPayload)?,
        },
        "paper.library.list" | "paper.library.summaries" => crate::paper_library::Request::List {
            summaries_only: admitted.operation.name.ends_with("summaries"),
        },
        "paper.library.get" | "paper.library.reader" => crate::paper_library::Request::Get {
            paper_id: required_text(&payload, "id", 256)?.to_owned(),
            include_babel_cache: admitted.operation.name.ends_with("get"),
        },
        "paper.library.inputs" => {
            let offered = payload
                .get("arxiv_ids")
                .and_then(Value::as_array)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if offered.len() > crate::generated_tofudb_ir::MAX_PAPER_LIBRARY_FANIN_PAPERS {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let mut arxiv_ids = Vec::with_capacity(offered.len());
            for value in offered {
                let normalized = value
                    .as_str()
                    .map(str::trim)
                    .filter(|value| !value.is_empty() && value.chars().count() <= 256)
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                if !arxiv_ids.iter().any(|value| value == normalized) {
                    arxiv_ids.push(normalized.to_owned());
                }
            }
            crate::paper_library::Request::Inputs {
                arxiv_ids,
                max_text_characters: usize::try_from(unsigned("max_text_chars", Some(0))?)
                    .map_err(|_| SemanticExecutionError::InvalidPayload)?,
            }
        }
        "paper.library.identity" => {
            let include_text_length = match payload.get("include_text_length") {
                None => true,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let max_text_characters = match payload.get("max_text_chars") {
                None | Some(Value::Null) => None,
                Some(value) => Some(
                    usize::try_from(
                        value
                            .as_u64()
                            .ok_or(SemanticExecutionError::InvalidPayload)?,
                    )
                    .map_err(|_| SemanticExecutionError::InvalidPayload)?,
                ),
            };
            crate::paper_library::Request::Identity {
                paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
                max_text_characters,
                include_text_length,
            }
        }
        "paper.library.title.backfill" => crate::paper_library::Request::TitleBackfill {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            title: required_text(&payload, "title", 1_000)?.to_owned(),
            updated_at_seconds: now_unix_ms / 1_000,
            physical_updated_at_ms: now_unix_ms,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        for field in ["parsed_text", "qa_history", "images", "babel_cache"] {
            if let Some(value) = replay_payload.remove(field) {
                let encoded = canonical_json(&value)?;
                replay_payload.insert(
                    format!("{field}_sha256"),
                    Value::String(format!("{:x}", Sha256::digest(&encoded))),
                );
                replay_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
            }
        }
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(&json!({
                "operation": admitted.operation.name,
                "payload": replay_payload,
                "contentAuthority": "owner-bound-paper-library-documents",
            }))?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::PaperLibrary { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_paper(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let optional_text = |field: &str, maximum: usize| match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let request = match admitted.operation.name {
        "paper.note.list" => crate::paper::Request::NoteList {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            lang: optional_text("lang", 64)?,
        },
        "paper.note.create" => crate::paper::Request::NoteCreate {
            note_id: required_text(&payload, "id", 256)?.to_owned(),
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            lang: optional_text("lang", 64)?,
            anchor: match payload.get("anchor") {
                None => Map::new(),
                Some(Value::Object(anchor)) => anchor.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            },
            note: required_text(&payload, "note", 100_000)?.to_owned(),
            created_at: payload
                .get("created_at")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            updated_at: payload
                .get("updated_at")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            physical_updated_at_ms: now_unix_ms,
        },
        "paper.note.update" => crate::paper::Request::NoteUpdate {
            note_id: required_text(&payload, "id", 256)?.to_owned(),
            note: required_text(&payload, "note", 100_000)?.to_owned(),
            updated_at: payload
                .get("updated_at")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            physical_updated_at_ms: now_unix_ms,
        },
        "paper.note.delete" => crate::paper::Request::NoteDelete {
            note_id: required_text(&payload, "id", 256)?.to_owned(),
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        for field in ["note", "anchor"] {
            if let Some(value) = replay_payload.remove(field) {
                let encoded = canonical_json(&value)?;
                replay_payload.insert(
                    format!("{field}_sha256"),
                    Value::String(format!("{:x}", Sha256::digest(&encoded))),
                );
                replay_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
            }
        }
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(&json!({
                "operation": admitted.operation.name,
                "payload": replay_payload,
                "contentAuthority": "owner-bound-paper-note-document",
            }))?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Paper { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_raw_archive(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let unsigned = |field: &str, default: Option<u64>| -> Result<u64, SemanticExecutionError> {
        match payload.get(field) {
            None => default.ok_or(SemanticExecutionError::InvalidPayload),
            Some(value) => value.as_u64().ok_or(SemanticExecutionError::InvalidPayload),
        }
    };
    let decoded_part = |field: &str| -> Result<Vec<u8>, SemanticExecutionError> {
        let encoded = payload
            .get(field)
            .and_then(Value::as_str)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(|_| SemanticExecutionError::InvalidPayload)
    };
    let request = match admitted.operation.name {
        "raw_archive.put" => crate::raw_archive::Request::Put {
            archive_id: required_text(&payload, "archive_id", 160)?.to_owned(),
            conversation_id: required_text(&payload, "conversation_id", 128)?.to_owned(),
            turn_id: required_text(&payload, "turn_id", 128)?.to_owned(),
            attempt_id: required_text(&payload, "attempt_id", 128)?.to_owned(),
            task_id: required_text(&payload, "task_id", 256)?.to_owned(),
            round_num: unsigned("round_num", None)?,
            transport_attempt: unsigned("transport_attempt", Some(0))?,
            request_blob: decoded_part("request_blob_b64")?,
            response_blob: decoded_part("response_blob_b64")?,
            request_bytes: unsigned("request_bytes", None)?,
            response_bytes: unsigned("response_bytes", None)?,
            request_sha256: required_text(&payload, "request_sha256", 64)?.to_owned(),
            response_sha256: required_text(&payload, "response_sha256", 64)?.to_owned(),
            integrity: required_text(&payload, "integrity", 16)?.to_owned(),
            // Legacy coerces with `payload.get(key) or default`: every falsy
            // JSON value maps to the default while a truthy value of the wrong
            // shape is a protocol error.
            truncation_reason: match payload.get("truncation_reason") {
                None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
                Some(Value::Number(value)) if value.as_f64() == Some(0.0) => String::new(),
                Some(Value::String(value)) if value.is_empty() => String::new(),
                Some(Value::Array(value)) if value.is_empty() => String::new(),
                Some(Value::Object(value)) if value.is_empty() => String::new(),
                Some(Value::String(value)) => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            },
            summary: match payload.get("summary") {
                None | Some(Value::Null) | Some(Value::Bool(false)) => Map::new(),
                Some(Value::Number(value)) if value.as_f64() == Some(0.0) => Map::new(),
                Some(Value::String(value)) if value.is_empty() => Map::new(),
                Some(Value::Array(value)) if value.is_empty() => Map::new(),
                Some(Value::Object(summary)) => summary.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            },
            budget_bytes: unsigned("budget_bytes", None)?,
            min_free_bytes: unsigned("min_free_bytes", Some(0))?,
            available_free_bytes: unsigned("available_free_bytes", Some(0))?,
            created_at_ms: now_unix_ms,
        },
        "raw_archive.list" => crate::raw_archive::Request::List {
            task_id: required_text(&payload, "task_id", 256)?.to_owned(),
            round_num: payload
                .get("round_num")
                .map(|value| value.as_u64().ok_or(SemanticExecutionError::InvalidPayload))
                .transpose()?,
            limit: usize::try_from(unsigned("limit", Some(16))?)
                .map_err(|_| SemanticExecutionError::InvalidPayload)?,
        },
        "raw_archive.read" => crate::raw_archive::Request::Read {
            task_id: required_text(&payload, "task_id", 256)?.to_owned(),
            archive_id: required_text(&payload, "archive_id", 160)?.to_owned(),
            part: required_text(&payload, "part", 16)?.to_owned(),
            offset: usize::try_from(unsigned("offset", Some(0))?)
                .map_err(|_| SemanticExecutionError::InvalidPayload)?,
            limit: usize::try_from(unsigned("limit", Some(256 * 1024))?)
                .map_err(|_| SemanticExecutionError::InvalidPayload)?,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        replay_payload.remove("request_blob_b64");
        replay_payload.remove("response_blob_b64");
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(&json!({
                "operation": admitted.operation.name,
                "payload": replay_payload,
                "contentAuthority": "owner-bound-raw-archive-blob",
            }))?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::RawArchive { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_research(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let optional_text = |field: &str, maximum: usize| match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let request = match admitted.operation.name {
        "research.artifact.upsert" => {
            let lang_key = required_text(&payload, "lang_key", 64)?.to_owned();
            if !(lang_key.starts_with("survey:") || lang_key.starts_with("ideate:")) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::research::ResearchRequest::ArtifactUpsert(
                crate::research::ArtifactUpsertRequest {
                    paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
                    lang_key,
                    report: optional_text("report", 10_000_000)?,
                    model: optional_text("model", 512)?,
                    meta: payload
                        .get("meta")
                        .and_then(Value::as_object)
                        .cloned()
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    created_at: payload
                        .get("created_at")
                        .and_then(Value::as_u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    updated_at_ms: now_unix_ms,
                },
            )
        }
        "research.artifacts.get" => {
            let lang = required_text(&payload, "lang", 32)?.to_owned();
            if lang.contains(':') {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::research::ResearchRequest::ArtifactsGet {
                paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
                lang,
            }
        }
        "research.directions.list" => crate::research::ResearchRequest::DirectionsList {
            limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 50)?)
                .ok()
                .filter(|limit| (1..=1000).contains(limit))
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        },
        "research.workspace.get" => crate::research::ResearchRequest::WorkspaceGet {
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            lang: required_text(&payload, "lang", 8)?.to_owned(),
        },
        "research.workspace.put" => crate::research::ResearchRequest::WorkspacePut {
            artifact_proof: payload
                .get("artifact_proof")
                .filter(|v| !v.is_null())
                .cloned(),
            paper_hash: required_text(&payload, "paper_hash", 128)?.to_owned(),
            lang: required_text(&payload, "lang", 8)?.to_owned(),
            expected_revision: payload
                .get("expected_revision")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            updated_at_seconds: payload
                .get("updated_at")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            workspace: payload
                .get("workspace")
                .and_then(Value::as_object)
                .cloned()
                .ok_or(SemanticExecutionError::InvalidPayload)?,
            physical_updated_at_ms: now_unix_ms,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        let content_authority = if admitted.operation.name == "research.artifact.upsert" {
            let report = replay_payload
                .remove("report")
                .and_then(|value| value.as_str().map(str::to_owned))
                .unwrap_or_default();
            replay_payload.insert(
                "report_sha256".to_owned(),
                Value::String(format!("{:x}", Sha256::digest(report.as_bytes()))),
            );
            replay_payload.insert("report_bytes".to_owned(), Value::from(report.len()));
            "owner-bound-research-artifact-blob"
        } else {
            let workspace = replay_payload.remove("workspace").unwrap_or(Value::Null);
            let encoded = canonical_json(&workspace)?;
            replay_payload.insert(
                "workspace_sha256".to_owned(),
                Value::String(format!("{:x}", Sha256::digest(&encoded))),
            );
            replay_payload.insert("workspace_bytes".to_owned(), Value::from(encoded.len()));
            "owner-bound-research-workspace-blob"
        };
        let outbox_payload = canonical_json(&json!({
            "operation": admitted.operation.name,
            "payload": replay_payload,
            "contentAuthority": content_authority,
        }))?;
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            outbox_payload,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Research { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}
fn compile_knowledge(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let name = admitted.operation.name;
    let bounded_integer = |field: &str, default: u64, range: std::ops::RangeInclusive<u64>| {
        let value = optional_nonnegative_integer(&payload, field, default)?;
        if !range.contains(&value) {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        Ok(value)
    };
    let document_id = || required_text(&payload, "document_id", 128).map(str::to_owned);
    let window = |offset_default: u64| {
        Ok::<(u64, u64), SemanticExecutionError>((
            optional_nonnegative_integer(&payload, "offset", offset_default)?,
            bounded_integer("limit", 80, 1..=200)?,
        ))
    };
    if name == "knowledge.search.candidates" {
        let raw_tokens = match payload.get("tokens") {
            Some(Value::Array(tokens)) => tokens,
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        let mut tokens = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for value in raw_tokens.iter().take(32) {
            let Some(raw) = value
                .as_str()
                .filter(|raw| !raw.is_empty() && raw.chars().count() <= 128)
            else {
                return Err(SemanticExecutionError::InvalidPayload);
            };
            let folded = crate::artifact::python_casefold(raw);
            // Folded terms longer than the index term bound can never match a
            // posting, so legacy IN-matching treats them as absent.
            if folded.chars().count() <= 128 && seen.insert(folded.clone()) {
                tokens.push(folded);
            }
        }
        if tokens.is_empty() {
            return Ok(TransactionIr {
                tenant_id,
                owner_user_id,
                operation: name.to_owned(),
                operation_kind: admitted.operation.kind,
                steps: Vec::new(),
                result: TransactionIrResult::Literal(b"[]".to_vec()),
                command_effects: None,
            });
        }
        let scoped_document = match payload.get("document_id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(value)) => crate::knowledge::python_strip(value).to_owned(),
            Some(_) => return Err(SemanticExecutionError::InvalidPayload),
        };
        if scoped_document.chars().count() > 128 {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let limit = bounded_integer("limit", 80, 1..=200)?;
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id,
            operation: name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::Knowledge {
                request: crate::knowledge::KnowledgeRequest::SearchCandidates {
                    tokens,
                    document_id: scoped_document,
                    limit,
                },
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"[]".to_vec(),
            },
            command_effects: None,
        });
    }
    let request = match name {
        "knowledge.document.list" => crate::knowledge::KnowledgeRequest::DocumentList,
        "knowledge.document.get" => crate::knowledge::KnowledgeRequest::DocumentGet {
            document_id: document_id()?,
        },
        "knowledge.document.metadata" => crate::knowledge::KnowledgeRequest::DocumentMetadata {
            document_id: document_id()?,
        },
        "knowledge.document.assets" => {
            let (offset, limit) = window(0)?;
            crate::knowledge::KnowledgeRequest::DocumentAssets {
                document_id: document_id()?,
                offset,
                limit,
            }
        }
        "knowledge.document.content" => {
            let (offset, limit) = window(0)?;
            crate::knowledge::KnowledgeRequest::DocumentContent {
                document_id: document_id()?,
                offset,
                limit,
            }
        }
        "knowledge.document.patch" => {
            let updates = payload
                .get("updates")
                .and_then(Value::as_object)
                .filter(|updates| !updates.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut scope = None;
            let mut media_metadata_json = None;
            for (field, value) in updates {
                let text = value
                    .as_str()
                    .ok_or(SemanticExecutionError::InvalidPayload)?
                    .to_owned();
                match field.as_str() {
                    "scope" => scope = Some(text),
                    "media_metadata_json" => media_metadata_json = Some(text),
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                }
            }
            crate::knowledge::KnowledgeRequest::DocumentPatch {
                document_id: document_id()?,
                scope,
                media_metadata_json,
                now_unix_ms,
            }
        }
        "knowledge.document.find_digest" => {
            crate::knowledge::KnowledgeRequest::DocumentFindDigest {
                sha256: required_text(&payload, "sha256", 64)?.to_ascii_lowercase(),
            }
        }
        "knowledge.document.create" | "knowledge.document.replace" => {
            let document = crate::knowledge::validate_document(
                payload
                    .get("document")
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            )
            .map_err(map_storage_error)?;
            if name == "knowledge.document.create" {
                if required_text(&payload, "document_id", 128)? != document.id {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                crate::knowledge::KnowledgeRequest::DocumentCreate {
                    document,
                    now_unix_ms,
                }
            } else {
                crate::knowledge::KnowledgeRequest::DocumentReplace {
                    document,
                    now_unix_ms,
                }
            }
        }
        "knowledge.document.delete" => crate::knowledge::KnowledgeRequest::DocumentDelete {
            document_id: document_id()?,
        },
        "knowledge.settings.get" => crate::knowledge::KnowledgeRequest::SettingsGet,
        "knowledge.settings.patch" => {
            let optional_bool = |field: &str| match payload.get(field) {
                None => Ok(None),
                Some(Value::Bool(value)) => Ok(Some(*value)),
                Some(_) => Err(SemanticExecutionError::InvalidPayload),
            };
            crate::knowledge::KnowledgeRequest::SettingsPatch {
                enabled: optional_bool("enabled")?,
                visual_enrichment: optional_bool("visual_enrichment")?,
                now_unix_ms,
            }
        }
        "knowledge.availability" => crate::knowledge::KnowledgeRequest::Availability,
        "knowledge.asset.get" => crate::knowledge::KnowledgeRequest::AssetGet {
            asset_id: required_text(&payload, "asset_id", 128)?.to_owned(),
        },
        "knowledge.enrichment.activity" => crate::knowledge::KnowledgeRequest::EnrichmentActivity,
        "knowledge.enrichment.owners" => crate::knowledge::KnowledgeRequest::EnrichmentOwners {
            limit: bounded_integer("limit", 512, 1..=512)?,
            after_user_id: bounded_integer("after_user_id", 0, 0..=i64::MAX as u64)?,
        },
        "knowledge.asset.claim" => crate::knowledge::KnowledgeRequest::AssetClaim { now_unix_ms },
        "knowledge.asset.update" => {
            let updates_object = payload
                .get("updates")
                .and_then(Value::as_object)
                .filter(|updates| !updates.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut updates = Vec::with_capacity(updates_object.len());
            for (field, value) in updates_object {
                updates.push((
                    field.clone(),
                    value
                        .as_str()
                        .ok_or(SemanticExecutionError::InvalidPayload)?
                        .to_owned(),
                ));
            }
            // Legacy validates chunk field types only after the asset
            // existence check, so any JSON value passes through to the
            // handler here.
            let optional_chunk_text = |field: &str| {
                Ok::<Option<Value>, SemanticExecutionError>(payload.get(field).cloned())
            };
            crate::knowledge::KnowledgeRequest::AssetUpdate {
                asset_id: required_text(&payload, "asset_id", 128)?.to_owned(),
                updates,
                chunk_content: optional_chunk_text("chunk_content")?,
                chunk_search_text: optional_chunk_text("chunk_search_text")?,
                now_unix_ms,
            }
        }
        "knowledge.assets.mark_no_vision" => {
            crate::knowledge::KnowledgeRequest::AssetsMarkNoVision { now_unix_ms }
        }
        "knowledge.catalog" => {
            let filter_text = |field: &str, default: &str| match payload.get(field) {
                None | Some(Value::Null) => Ok(default.to_owned()),
                Some(Value::String(value)) if value.is_empty() => Ok(default.to_owned()),
                Some(Value::String(value)) => Ok(value.to_lowercase()),
                Some(_) => Err(SemanticExecutionError::InvalidPayload),
            };
            let query = match payload.get("query") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(value)) => crate::knowledge::python_strip(value).to_owned(),
                Some(_) => return Err(SemanticExecutionError::InvalidPayload),
            };
            if query.chars().count() > 200 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::knowledge::KnowledgeRequest::Catalog {
                page: bounded_integer("page", 1, 1..=u64::MAX)?,
                page_size: bounded_integer("page_size", 30, 1..=100)?,
                query,
                category: filter_text("category", "all")?,
                sort: filter_text("sort", "updated_desc")?,
            }
        }
        "knowledge.owner.clear" => crate::knowledge::KnowledgeRequest::OwnerClear,
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    request.validate().map_err(map_storage_error)?;
    let command_effects = if admitted.operation.kind.mutates_state() {
        let mut replay_payload = payload.clone();
        let content_authority = match name {
            "knowledge.document.create" | "knowledge.document.replace" => {
                let document = replay_payload.remove("document").unwrap_or(Value::Null);
                let encoded = canonical_json(&document)?;
                replay_payload.insert(
                    "document_sha256".to_owned(),
                    Value::String(format!("{:x}", Sha256::digest(&encoded))),
                );
                replay_payload.insert("document_bytes".to_owned(), Value::from(encoded.len()));
                "owner-bound-knowledge-document-blob"
            }
            "knowledge.asset.update" => {
                if let Some(updates) = replay_payload.remove("updates") {
                    let encoded = canonical_json(&updates)?;
                    replay_payload.insert(
                        "updates_sha256".to_owned(),
                        Value::String(format!("{:x}", Sha256::digest(&encoded))),
                    );
                    replay_payload.insert("updates_bytes".to_owned(), Value::from(encoded.len()));
                }
                for field in ["chunk_content", "chunk_search_text"] {
                    if let Some(text) = replay_payload
                        .remove(field)
                        .and_then(|value| value.as_str().map(str::to_owned))
                    {
                        replay_payload.insert(
                            format!("{field}_sha256"),
                            Value::String(format!("{:x}", Sha256::digest(text.as_bytes()))),
                        );
                        replay_payload.insert(format!("{field}_bytes"), Value::from(text.len()));
                    }
                }
                "owner-bound-knowledge-asset-blob"
            }
            _ => "owner-bound-knowledge-mutation",
        };
        Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            canonical_json(&json!({
                "operation": name,
                "payload": replay_payload,
                "contentAuthority": content_authority,
            }))?,
        )?)
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Knowledge { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_daily_cost(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = positive_identity(&payload, "user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let (steps, result) = match admitted.operation.name {
        "daily_cost.month" => {
            let year = payload
                .get("year")
                .and_then(Value::as_u64)
                .filter(|value| (1970..=9999).contains(value))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let month = payload
                .get("month")
                .and_then(Value::as_u64)
                .filter(|value| (1..=12).contains(value))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::DailyCostMonth {
                    year,
                    month,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"[]".to_vec(),
                },
            )
        }
        "daily_cost.latest" => (
            vec![TransactionIrStep::DailyCostLatest { slot: 0 }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "daily_cost.persisted_dates" => {
            let values = payload
                .get("dates")
                .and_then(Value::as_array)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut dates = Vec::with_capacity(values.len());
            let mut seen_dates = std::collections::HashSet::with_capacity(values.len());
            for value in values {
                let date = value
                    .as_str()
                    .filter(|date| crate::daily_cost::valid_date(date))
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                if seen_dates.insert(date) {
                    dates.push(date.to_owned());
                }
            }
            (
                vec![TransactionIrStep::DailyCostPersistedDates { dates, slot: 0 }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: br#"{"dates":[]}"#.to_vec(),
                },
            )
        }
        "daily_cost.upsert" => {
            let date = required_text(&payload, "date", 10)?;
            if !crate::daily_cost::valid_date(date) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let cost = payload
                .get("cost")
                .and_then(Value::as_f64)
                .filter(|value| value.is_finite() && (0.0..=1_000_000_000.0).contains(value))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let conversations = match payload.get("conversations") {
                None => serde_json::Map::new(),
                Some(Value::Object(value)) => value.clone(),
                Some(_) => return Err(SemanticExecutionError::InvalidPayload),
            };
            let conversations_json = canonical_json(&Value::Object(conversations))?;
            let computed_at = payload
                .get("computed_at")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::DailyCostUpsert {
                    date: date.to_owned(),
                    cost,
                    conversations_json,
                    computed_at,
                    updated_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "daily_cost.delete" => {
            let date = match payload.get("date") {
                None | Some(Value::Null) => None,
                Some(Value::String(date)) if crate::daily_cost::valid_date(date) => {
                    Some(date.clone())
                }
                Some(_) => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::DailyCostDelete { date, slot: 0 }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

const SCHEDULER_NUMERIC_FIELDS: &[&str] = &[
    "enabled",
    "notify_on_failure",
    "notify_on_success",
    "max_runtime",
    "run_count",
    "fail_count",
    "poll_count",
    "execution_count",
    "max_executions",
    "promotion_streak",
    "fallback_streak",
];

const SCHEDULER_TEXT_FIELDS: &[&str] = &[
    "name",
    "schedule",
    "task_type",
    "command",
    "description",
    "last_run",
    "last_result",
    "last_status",
    "created_at",
    "updated_at",
    "target_conv_id",
    "source_conv_id",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "last_execution_at",
    "last_execution_task_id",
    "last_execution_status",
    "expires_at",
    "condition_kind",
    "condition_command",
    "condition_regex",
    "promoted_at",
];

fn scheduler_task_id(payload: &Map<String, Value>) -> Result<String, SemanticExecutionError> {
    let field = if payload.contains_key("task_id") {
        "task_id"
    } else {
        "id"
    };
    required_text(
        payload,
        field,
        crate::generated_tofudb_ir::MAX_SCHEDULER_TASK_ID_CHARACTERS,
    )
    .map(str::to_owned)
}

fn scheduler_integer(value: &Value) -> Result<i64, SemanticExecutionError> {
    match value {
        Value::Bool(value) => Ok(i64::from(*value)),
        Value::Number(value) => value.as_i64().ok_or(SemanticExecutionError::InvalidPayload),
        Value::String(value) => value
            .trim()
            .parse::<i64>()
            .map_err(|_| SemanticExecutionError::InvalidPayload),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn scheduler_optional_text(
    payload: &Map<String, Value>,
    field: &str,
    maximum: usize,
) -> Result<String, SemanticExecutionError> {
    match payload.get(field) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn scheduler_truncated_text(
    payload: &Map<String, Value>,
    field: &str,
    maximum: usize,
) -> Result<String, SemanticExecutionError> {
    match payload.get(field) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(value)) => Ok(value.chars().take(maximum).collect()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn scheduler_create_document(
    payload: &Map<String, Value>,
    task_id: &str,
    owner_user_id: u64,
) -> Result<Map<String, Value>, SemanticExecutionError> {
    for (field, maximum) in [("name", 512), ("schedule", 512), ("command", 20_000)] {
        required_text(payload, field, maximum)?;
    }
    let tools_config = payload
        .get("tools_config")
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !tools_config.is_object() {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let mut document = Map::new();
    document.insert("id".to_owned(), json!(task_id));
    document.insert("user_id".to_owned(), json!(owner_user_id));
    document.insert("system_key".to_owned(), json!(""));
    for field in SCHEDULER_TEXT_FIELDS {
        let maximum = match *field {
            "name" | "schedule" => 512,
            "command" => 20_000,
            _ => 20_000,
        };
        document.insert(
            (*field).to_owned(),
            json!(scheduler_optional_text(payload, field, maximum)?),
        );
    }
    for field in SCHEDULER_NUMERIC_FIELDS {
        let value = match *field {
            "enabled" => i64::from(python_truthy(
                payload.get(*field).or(Some(&Value::Bool(true))),
            )),
            "notify_on_failure" => i64::from(python_truthy(
                payload.get(*field).or(Some(&Value::Bool(true))),
            )),
            "notify_on_success" => i64::from(python_truthy(payload.get(*field))),
            "max_runtime" => match payload.get(*field) {
                None | Some(Value::Null) => 300,
                Some(value) if is_python_falsy_json(value) => 300,
                Some(value) => scheduler_integer(value)?,
            },
            _ => match payload.get(*field) {
                None | Some(Value::Null) => 0,
                Some(value) if is_python_falsy_json(value) => 0,
                Some(value) => scheduler_integer(value)?,
            },
        };
        document.insert((*field).to_owned(), json!(value));
    }
    document.insert("tools_config".to_owned(), tools_config);
    Ok(document)
}

fn scheduler_updates(
    payload: &Map<String, Value>,
) -> Result<Map<String, Value>, SemanticExecutionError> {
    let mut updates = Map::new();
    for field in SCHEDULER_TEXT_FIELDS {
        if *field == "created_at" {
            continue;
        }
        if let Some(value) = payload.get(*field) {
            if !value.is_string() && !value.is_null() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            updates.insert((*field).to_owned(), value.clone());
        }
    }
    for field in SCHEDULER_NUMERIC_FIELDS {
        if let Some(value) = payload.get(*field) {
            updates.insert((*field).to_owned(), json!(scheduler_integer(value)?));
        }
    }
    if let Some(value) = payload.get("tools_config") {
        if !value.is_object() {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        updates.insert("tools_config".to_owned(), value.clone());
    }
    if updates.is_empty() {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    if !updates.contains_key("updated_at") {
        updates.insert(
            "updated_at".to_owned(),
            json!(scheduler_optional_text(payload, "now", 64)?),
        );
    }
    Ok(updates)
}

fn compile_scheduler(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = if admitted.operation.name == "scheduler.task.list_all" {
        admitted.request.owner_id
    } else {
        let requested = positive_identity(&payload, "user_id")?;
        if requested != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        requested
    };
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let request = match admitted.operation.name {
        "scheduler.task.create" => {
            let task_id = scheduler_task_id(&payload)?;
            crate::scheduler::Request::Create {
                document: scheduler_create_document(&payload, &task_id, owner_user_id)?,
                task_id,
                updated_at_ms: now_unix_ms.max(1),
            }
        }
        "scheduler.task.ensure" => {
            let task_id = scheduler_task_id(&payload)?;
            let system_key = required_text(
                &payload,
                "system_key",
                crate::generated_tofudb_ir::MAX_SCHEDULER_SYSTEM_KEY_CHARACTERS,
            )?
            .to_owned();
            let task_type = required_text(&payload, "task_type", 128)?.to_owned();
            let max_runtime = optional_nonnegative_integer(&payload, "max_runtime", 300)?;
            if !(1..=86_400).contains(&max_runtime) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let tools_config = payload
                .get("tools_config")
                .cloned()
                .unwrap_or_else(|| json!({}));
            if !tools_config.is_object() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let reconcile_enabled = match payload.get("reconcile_enabled") {
                None => false,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let condition_kind = scheduler_truncated_text(&payload, "condition_kind", 64)?;
            let condition_kind = if condition_kind.is_empty() {
                "llm".to_owned()
            } else {
                condition_kind
            };
            let mut definition = json!({
                "name": required_text(&payload, "name", 512)?,
                "schedule": required_text(&payload, "schedule", 512)?,
                "task_type": task_type,
                "command": required_text(&payload, "command", 20_000)?,
                "description": scheduler_truncated_text(&payload, "description", 20_000)?,
                "notify_on_failure": i64::from(python_truthy(
                    payload.get("notify_on_failure").or(Some(&Value::Bool(true)))
                )),
                "notify_on_success": i64::from(python_truthy(payload.get("notify_on_success"))),
                "max_runtime": max_runtime,
                "tools_config": tools_config,
                "condition_kind": condition_kind,
                "condition_command": scheduler_truncated_text(&payload, "condition_command", 20_000)?,
                "condition_regex": scheduler_truncated_text(&payload, "condition_regex", 20_000)?,
            })
            .as_object()
            .expect("JSON object literal")
            .clone();
            if reconcile_enabled {
                definition.insert(
                    "enabled".to_owned(),
                    json!(i64::from(python_truthy(
                        payload.get("enabled").or(Some(&Value::Bool(true)))
                    ))),
                );
            }
            let mut create_document = scheduler_create_document(&payload, &task_id, owner_user_id)?;
            create_document.insert("system_key".to_owned(), json!(system_key));
            crate::scheduler::Request::Ensure {
                task_id,
                system_key,
                create_document,
                definition,
                updated_at: scheduler_optional_text(&payload, "updated_at", 20_000)?,
                updated_at_ms: now_unix_ms.max(1),
            }
        }
        "scheduler.task.get" => crate::scheduler::Request::Get {
            task_id: scheduler_task_id(&payload)?,
        },
        "scheduler.task.list" => {
            let limit = optional_nonnegative_integer(&payload, "limit", 1_000)?;
            crate::scheduler::Request::List {
                limit: usize::try_from(limit)
                    .ok()
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                enabled_only: match payload.get("enabled_only") {
                    None => false,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
            }
        }
        "scheduler.task.list_all" => {
            let limit = optional_nonnegative_integer(&payload, "limit", 1_000)?;
            crate::scheduler::Request::ListAll {
                limit: usize::try_from(limit)
                    .ok()
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                enabled_only: match payload.get("enabled_only") {
                    None => false,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
            }
        }
        "scheduler.task.update" => crate::scheduler::Request::Update {
            task_id: scheduler_task_id(&payload)?,
            updates: scheduler_updates(&payload)?,
            updated_at_ms: now_unix_ms.max(1),
        },
        "scheduler.task.execution_lookup" => crate::scheduler::Request::ExecutionLookup {
            execution_id: required_text(&payload, "execution_task_id", 128)?.to_owned(),
        },
        "scheduler.task.settle_execution" => crate::scheduler::Request::SettleExecution {
            execution_id: required_text(&payload, "execution_task_id", 128)?.to_owned(),
            status: required_text(&payload, "status", 16)?.to_owned(),
            now: required_text(&payload, "now", 64)?.to_owned(),
            updated_at_ms: now_unix_ms.max(1),
        },
        "scheduler.task.record_result" => crate::scheduler::Request::RecordResult {
            task_id: scheduler_task_id(&payload)?,
            now: required_text(&payload, "now", 64)?.to_owned(),
            result: scheduler_truncated_text(&payload, "result", 10_000)?,
            success: python_truthy(payload.get("success")),
            updated_at_ms: now_unix_ms.max(1),
        },
        "scheduler.task.claim_due" => {
            let now = required_text(&payload, "now", 64)?.to_owned();
            let parsed_now = crate::scheduler::parse_timestamp(&now)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let lane = required_text(&payload, "lane", 16)?.to_owned();
            if !matches!(lane.as_str(), "run" | "poll") {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let minimum_interval_seconds =
                optional_nonnegative_integer(&payload, "minimum_interval_seconds", 55)?;
            if !(1..=3_600).contains(&minimum_interval_seconds) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::scheduler::Request::ClaimDue {
                task_id: scheduler_task_id(&payload)?,
                lane,
                now,
                parsed_now,
                minimum_interval_seconds,
                updated_at_ms: now_unix_ms.max(1),
            }
        }
        "scheduler.poll.append" => {
            let defaulted_text = |field: &str, default: &str, maximum: usize| {
                let value = scheduler_truncated_text(&payload, field, maximum)?;
                Ok::<String, SemanticExecutionError>(if value.is_empty() {
                    default.to_owned()
                } else {
                    value
                })
            };
            crate::scheduler::Request::PollAppend {
                task_id: scheduler_task_id(&payload)?,
                poll: json!({
                    "poll_time": scheduler_optional_text(&payload, "poll_time", 64)?,
                    "decision": defaulted_text("decision", "skip", 128)?,
                    "reason": scheduler_truncated_text(&payload, "reason", 500)?,
                    "status_snapshot": scheduler_truncated_text(&payload, "status_snapshot", 5_000)?,
                    "model": scheduler_truncated_text(&payload, "model", 120)?,
                    "tokens_used": match payload.get("tokens_used") {
                        None | Some(Value::Null) => 0,
                        Some(value) if is_python_falsy_json(value) => 0,
                        Some(value) => scheduler_integer(value)?,
                    },
                    "execution_task_id": scheduler_optional_text(&payload, "execution_task_id", 512)?,
                    "tier": defaulted_text("tier", "llm", 128)?,
                    "predicate_matched": match payload.get("predicate_matched") {
                        None => -1,
                        Some(value) => scheduler_integer(value)?,
                    },
                    "llm_agreed": match payload.get("llm_agreed") {
                        None => -1,
                        Some(value) => scheduler_integer(value)?,
                    },
                })
                .as_object()
                .expect("JSON object literal")
                .clone(),
            }
        }
        "scheduler.poll.log" => crate::scheduler::Request::PollLog {
            task_id: scheduler_task_id(&payload)?,
            limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 30)?)
                .ok()
                .filter(|limit| {
                    (1..=crate::generated_tofudb_ir::MAX_SCHEDULER_POLL_ROWS).contains(limit)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        },
        "scheduler.task.delete" => crate::scheduler::Request::Delete {
            task_id: scheduler_task_id(&payload)?,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Scheduler { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

const TIMER_TEXT_FIELDS: &[&str] = &[
    "conv_id",
    "source_task_id",
    "check_instruction",
    "check_command",
    "continuation_message",
    "status",
    "created_at",
    "updated_at",
    "triggered_at",
    "cancelled_at",
    "execution_task_id",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "condition_kind",
    "condition_command",
    "condition_regex",
    "promoted_at",
    "origin",
];

fn timer_id(payload: &Map<String, Value>) -> Result<String, SemanticExecutionError> {
    required_text(
        payload,
        "timer_id",
        crate::generated_tofudb_ir::MAX_TIMER_ID_CHARACTERS,
    )
    .map(str::to_owned)
}

fn timer_create_document(
    payload: &Map<String, Value>,
    timer_id: &str,
    owner_user_id: u64,
) -> Result<Map<String, Value>, SemanticExecutionError> {
    required_text(payload, "conv_id", 256)?;
    let instruction = scheduler_optional_text(payload, "check_instruction", 20_000)?;
    if instruction.trim().is_empty()
        && required_text(payload, "condition_command", 20_000)?.trim().is_empty()
    {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    required_text(payload, "continuation_message", 20_000)?;
    let tools_config = payload
        .get("tools_config")
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !tools_config.is_object() {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let mut document = Map::new();
    document.insert("id".to_owned(), json!(timer_id));
    document.insert("user_id".to_owned(), json!(owner_user_id));
    for field in TIMER_TEXT_FIELDS {
        let maximum = match *field {
            "conv_id" => 256,
            "condition_kind" => 64,
            _ => 20_000,
        };
        let value = if *field == "status" {
            "active".to_owned()
        } else {
            scheduler_optional_text(payload, field, maximum)?
        };
        document.insert((*field).to_owned(), json!(value));
    }
    for (field, default, minimum) in [
        ("poll_interval", 60_i64, 10_i64),
        ("max_polls", 120, 0),
        ("poll_count", 0, i64::MIN),
        ("promotion_streak", 0, i64::MIN),
        ("fallback_streak", 0, i64::MIN),
    ] {
        let value = match payload.get(field) {
            Some(value) if field == "poll_interval" || field == "max_polls" => {
                value.as_i64().ok_or(SemanticExecutionError::InvalidPayload)?
            }
            None | Some(Value::Null) => default,
            Some(value) if is_python_falsy_json(value) => 0,
            Some(value) => scheduler_integer(value)?,
        };
        if value < minimum {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        document.insert(field.to_owned(), json!(value));
    }
    document.insert("tools_config".to_owned(), tools_config);
    Ok(document)
}

fn timer_poll_document(
    payload: &Map<String, Value>,
) -> Result<Map<String, Value>, SemanticExecutionError> {
    let defaulted = |field: &str, default: &str, maximum: usize| {
        let value = scheduler_truncated_text(payload, field, maximum)?;
        Ok::<String, SemanticExecutionError>(if value.is_empty() {
            default.to_owned()
        } else {
            value
        })
    };
    Ok(json!({
        "poll_time": scheduler_optional_text(payload, "poll_time", 64)?,
        "decision": defaulted("decision", "wait", 128)?,
        "reason": scheduler_truncated_text(payload, "reason", 500)?,
        "check_output": scheduler_truncated_text(payload, "check_output", 5_000)?,
        "tokens_used": match payload.get("tokens_used") {
            None | Some(Value::Null) => 0,
            Some(value) if is_python_falsy_json(value) => 0,
            Some(value) => scheduler_integer(value)?,
        },
        "model": scheduler_truncated_text(payload, "model", 120)?,
        "poll_id": scheduler_truncated_text(payload, "poll_id", 80)?,
        "raw_output": scheduler_truncated_text(payload, "raw_output", 5_000)?,
        "tier": defaulted("tier", "llm", 128)?,
        "predicate_matched": payload.get("predicate_matched").map(scheduler_integer).transpose()?.unwrap_or(-1),
        "llm_agreed": payload.get("llm_agreed").map(scheduler_integer).transpose()?.unwrap_or(-1),
    })
    .as_object()
    .expect("JSON object literal")
    .clone())
}

fn compile_orchestration(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = if admitted.operation.name == "orchestration.run.retire_interrupted_all" {
        admitted.request.owner_id
    } else {
        let requested = positive_identity(&payload, "user_id")?;
        if requested != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        requested
    };
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Orchestration {
            request: crate::orchestration::Request {
                operation: admitted.operation.name.to_owned(),
                payload,
                now_ms: now_unix_ms,
            },
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_integration_workspace(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let internal = matches!(
        admitted.operation.name,
        "integration.workspace.claim_next"
            | "integration.workspace.peek_ready"
            | "integration.workspace.delivery_page"
            | "integration.workspace.get_integrating"
            | "integration.workspace.quarantine"
            | "integration.workspace.requeue"
            | "integration.workspace.mark_merged"
            | "integration.workspace.mark_failed"
    );
    let owner_user_id = if internal {
        admitted.request.owner_id
    } else {
        let requested = positive_identity(&payload, "user_id")?;
        if requested != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        requested
    };
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::IntegrationWorkspace {
            request: crate::integration_workspace::Request {
                operation: admitted.operation.name.to_owned(),
                payload,
                now_ms: now_unix_ms,
            },
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_swarm(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| {
            if matches!(admitted.operation.name, "swarm.agent.save" | "swarm.session.save") {
                // Full content remains in its blob document and request digest.
                // The outbox carries a compact invalidation, never another copy
                // of the resumable history inside the inline literal budget.
                let mut summary = Map::new();
                for field in ["user_id", "swarm_key", "agent_id", "status", "rounds_used", "delivered", "now_ms"] {
                    if let Some(value) = payload.get(field) { summary.insert(field.to_owned(), value.clone()); }
                }
                let outbox = canonical_json(&json!({"operation":admitted.operation.name,"payload":summary}))?;
                command_effects_with_outbox(admitted, &payload, now_unix_ms, outbox)
            } else {
                command_effects(admitted, &payload, now_unix_ms)
            }
        })
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Swarm {
            request: crate::swarm::Request {
                operation: admitted.operation.name.to_owned(),
                payload,
                now_ms: now_unix_ms,
            },
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_queue(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let internal = matches!(
        admitted.operation.name,
        "queue.autopilot.list_all" | "queue.conversations.list_all" | "queue.reap"
    );
    let owner_user_id = if internal {
        admitted.request.owner_id
    } else {
        let requested = positive_identity(&payload, "user_id")?;
        if requested != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        requested
    };
    let conversation_id = || {
        required_text(
            &payload,
            "conv_id",
            crate::generated_tofudb_ir::MAX_QUEUE_CONVERSATION_ID_CHARACTERS,
        )
        .map(str::to_owned)
    };
    let queue_id = || {
        required_text(
            &payload,
            "queue_id",
            crate::generated_tofudb_ir::MAX_QUEUE_ID_CHARACTERS,
        )
        .map(str::to_owned)
    };
    let queue_kind = |default: bool| {
        let kind = match payload.get("kind") {
            None if default => "real",
            Some(Value::String(value)) => value.as_str(),
            _ => return Err(SemanticExecutionError::InvalidPayload),
        };
        if ![
            "real",
            "goal_continuation",
            "peer_msg",
            "workflow_step",
            "autopilot",
            "background_receipt",
        ]
        .contains(&kind)
        {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        Ok(kind.to_owned())
    };
    let object = |field: &str| {
        payload
            .get(field)
            .cloned()
            .unwrap_or_else(|| json!({}))
            .as_object()
            .cloned()
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let nonnegative = |field: &str| optional_nonnegative_integer(&payload, field, 0);
    let request = match admitted.operation.name {
        "queue.autopilot.arm" => crate::queue::Request::AutopilotArm {
            conversation_id: conversation_id()?,
            queue_id: queue_id()?,
            config: object("config")?,
            created_at_ms: now_unix_ms,
        },
        "queue.autopilot.clear" => crate::queue::Request::AutopilotClear {
            conversation_id: conversation_id()?,
        },
        "queue.autopilot.get" => crate::queue::Request::AutopilotGet {
            conversation_id: conversation_id()?,
        },
        "queue.autopilot.list_all" => crate::queue::Request::AutopilotListAll,
        "queue.enqueue" => crate::queue::Request::Enqueue {
            conversation_id: conversation_id()?,
            queue_id: queue_id()?,
            kind: queue_kind(true)?,
            priority: {
                let value = optional_nonnegative_integer(&payload, "priority", 100)?;
                (value <= 1000)
                    .then_some(value)
                    .ok_or(SemanticExecutionError::InvalidPayload)?
            },
            message: object("message")?,
            config: object("config")?,
            created_at_ms: match payload.get("created_at_ms") {
                None => now_unix_ms,
                Some(_) => nonnegative("created_at_ms")?,
            },
        },
        "queue.list" => crate::queue::Request::List {
            conversation_id: conversation_id()?,
            mode: match payload.get("mode") {
                None => "full".to_owned(),
                Some(Value::String(value)) if matches!(value.as_str(),
                    "full" | "preview" | "dispatchable" | "background_pending" | "background_page") => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            },
            page_size: match payload.get("page_size") {
                None => 32,
                Some(value) => value.as_u64().filter(|value| *value > 0)
                    .and_then(|value| usize::try_from(value).ok())
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            },
            max_bytes: match payload.get("max_bytes") {
                None => crate::generated_tofudb_ir::DEFAULT_QUEUE_PAGE_BYTES,
                Some(value) => value.as_u64().filter(|value| *value > 0)
                    .and_then(|value| usize::try_from(value).ok())
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            },
            cursor: match payload.get("cursor") {
                None | Some(Value::Null) => None,
                Some(value) => Some((value.get("priority").and_then(Value::as_u64).ok_or(SemanticExecutionError::InvalidPayload)?,
                    value.get("createdAt").and_then(Value::as_u64).ok_or(SemanticExecutionError::InvalidPayload)?,
                    value.get("queueId").and_then(Value::as_str).filter(|id| !id.is_empty() && id.len() <= 256).ok_or(SemanticExecutionError::InvalidPayload)?.to_owned())),
            },
        },
        "queue.remove" => crate::queue::Request::Remove {
            conversation_id: conversation_id()?,
            queue_id: queue_id()?,
        },
        "queue.clear" => crate::queue::Request::Clear {
            conversation_id: conversation_id()?,
        },
        "queue.injection.ack" => {
            let mut queue_ids = match payload.get("queue_ids") {
                Some(Value::Array(values)) => values
                    .iter()
                    .map(|value| match value {
                        Value::String(text) if !text.is_empty() && text.chars().count() <= 256 => {
                            Ok(text.clone())
                        }
                        _ => Err(SemanticExecutionError::InvalidPayload),
                    })
                    .collect::<Result<Vec<_>, _>>()?,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            queue_ids.sort();
            queue_ids.dedup();
            crate::queue::Request::InjectionAck {
                conversation_id: conversation_id()?,
                queue_ids,
                occurred_at_ms: now_unix_ms,
            }
        }
        "queue.kind.clear" => crate::queue::Request::KindClear {
            conversation_id: conversation_id()?,
            kind: queue_kind(true)?,
        },
        "queue.dequeue" => crate::queue::Request::Dequeue {
            conversation_id: conversation_id()?,
            now_ms: nonnegative("now_ms")?,
            lease_ms: {
                let value = optional_nonnegative_integer(&payload, "lease_ms", 0)?;
                (1..=3_600_000)
                    .contains(&value)
                    .then_some(value)
                    .ok_or(SemanticExecutionError::InvalidPayload)?
            },
        },
        "queue.lease.bind" => crate::queue::Request::LeaseBind {
            queue_id: queue_id()?,
            task_id: required_text(&payload, "task_id", 256)?.to_owned(),
            now_ms: nonnegative("now_ms")?,
            lease_ms: {
                let value = optional_nonnegative_integer(&payload, "lease_ms", 0)?;
                (1..=3_600_000)
                    .contains(&value)
                    .then_some(value)
                    .ok_or(SemanticExecutionError::InvalidPayload)?
            },
        },
        "queue.lease.release" => crate::queue::Request::LeaseRelease {
            queue_id: queue_id()?,
        },
        "queue.finalize" => crate::queue::Request::Finalize {
            conversation_id: conversation_id()?,
            queue_id: queue_id()?,
        },
        "queue.depth" => crate::queue::Request::Depth {
            conversation_id: conversation_id()?,
        },
        "queue.conversations.list_all" => {
            let probe = payload.get("reap_probe_contract");
            if probe.is_some_and(|value| value.as_str() != Some("tofu.queue.reap-probe/v1"))
                || (probe.is_some() && payload.contains_key("kind"))
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::queue::Request::ConversationsListAll {
                kind: payload.get("kind").map(|_| queue_kind(false)).transpose()?,
                reap_probe: probe.is_some(),
                now_ms: probe.map(|_| nonnegative("now_ms")).transpose()?,
            }
        }
        "queue.reap" => crate::queue::Request::Reap {
            now_ms: nonnegative("now_ms")?,
            force: python_truthy(payload.get("force_reclaim")),
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Queue { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_timer(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = if admitted.operation.name == "timer.active.list_all" {
        admitted.request.owner_id
    } else {
        let requested = positive_identity(&payload, "user_id")?;
        if requested != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        requested
    };
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let request = match admitted.operation.name {
        "timer.create" => {
            let timer_id = timer_id(&payload)?;
            crate::timer::Request::Create {
                document: timer_create_document(&payload, &timer_id, owner_user_id)?,
                timer_id,
                updated_at_ms: now_unix_ms.max(1),
            }
        }
        "timer.get" => crate::timer::Request::Get {
            timer_id: timer_id(&payload)?,
        },
        "timer.list" => crate::timer::Request::List {
            limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 50)?)
                .ok()
                .filter(|limit| {
                    (1..=crate::generated_tofudb_ir::MAX_TIMER_LIST_ROWS).contains(limit)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        },
        "timer.history" => crate::timer::Request::History,
        "timer.active.list_all" => crate::timer::Request::ActiveListAll {
            limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 200)?)
                .ok()
                .filter(|limit| {
                    (0..=crate::generated_tofudb_ir::MAX_TIMER_GLOBAL_ACTIVE_ROWS).contains(limit)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        },
        "timer.active.count" => crate::timer::Request::ActiveCount,
        "timer.cancel" => crate::timer::Request::Cancel {
            timer_id: timer_id(&payload)?,
            now: scheduler_optional_text(&payload, "now", 64)?,
            updated_at_ms: now_unix_ms.max(1),
        },
        "timer.update" => {
            let numeric = ["poll_count", "promotion_streak", "fallback_streak"];
            let allowed = [
                "last_poll_at",
                "last_poll_decision",
                "last_poll_reason",
                "status",
                "updated_at",
                "triggered_at",
                "execution_task_id",
                "cancelled_at",
                "promoted_at",
                "condition_kind",
            ];
            let mut updates = Map::new();
            for field in numeric {
                if let Some(value) = payload.get(field) {
                    updates.insert(field.to_owned(), json!(scheduler_integer(value)?));
                }
            }
            for field in allowed {
                if payload.contains_key(field) {
                    updates.insert(
                        field.to_owned(),
                        json!(scheduler_optional_text(&payload, field, 20_000)?),
                    );
                }
            }
            if updates.is_empty() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::timer::Request::Update {
                timer_id: timer_id(&payload)?,
                updates,
                expected_status: payload
                    .get("expected_status")
                    .map(|_| required_text(&payload, "expected_status", 32).map(str::to_owned))
                    .transpose()?,
                updated_at_ms: now_unix_ms.max(1),
            }
        }
        "timer.poll.append" => crate::timer::Request::PollAppend {
            timer_id: timer_id(&payload)?,
            poll: timer_poll_document(&payload)?,
        },
        "timer.poll.commit" => {
            let poll = timer_poll_document(&payload)?;
            if poll["poll_time"].as_str().is_none_or(str::is_empty) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            crate::timer::Request::PollCommit {
                timer_id: timer_id(&payload)?,
                poll,
                updated_at_ms: now_unix_ms.max(1),
            }
        }
        "timer.progress" => crate::timer::Request::Progress {
            timer_id: timer_id(&payload)?,
            poll_time: required_text(&payload, "poll_time", 64)?.to_owned(),
            decision: {
                let value = scheduler_truncated_text(&payload, "decision", 128)?;
                if value.is_empty() {
                    "skipped".to_owned()
                } else {
                    value
                }
            },
            reason: scheduler_truncated_text(&payload, "reason", 500)?,
            updated_at_ms: now_unix_ms.max(1),
        },
        "timer.poll.log" => crate::timer::Request::PollLog {
            timer_id: timer_id(&payload)?,
            limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 30)?)
                .ok()
                .filter(|limit| {
                    (1..=crate::generated_tofudb_ir::MAX_TIMER_POLL_ROWS).contains(limit)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Timer { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_project_brain(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    if admitted.operation.name == "project_brain.cutover.status" {
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: admitted.request.owner_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: Vec::new(),
            result: TransactionIrResult::Literal(canonical_json(&json!({"complete": true}))?),
            command_effects: None,
        });
    }
    if admitted.operation.name == "project_brain.cutover" {
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: admitted.request.owner_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: Vec::new(),
            result: TransactionIrResult::Literal(canonical_json(&json!({
                "ok": true,
                "alreadyComplete": true,
                "projects": 0
            }))?),
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    if admitted.operation.name == "project_brain.recovery.snapshot" {
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id: admitted.request.owner_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::ProjectBrainRecoverySnapshot {
                cursor: payload
                    .get("cursor")
                    .filter(|value| !value.is_null())
                    .map(|value| {
                        serde_json::from_value(value.clone())
                            .map_err(|_| SemanticExecutionError::InvalidPayload)
                    })
                    .transpose()?,
                slot: 0,
            }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            command_effects: Some(command_effects(admitted, &payload, now_unix_ms)?),
        });
    }
    let owner_user_id = positive_identity(&payload, "owner_user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    if admitted.operation.name == "project_brain.active.list" {
        return Ok(TransactionIr {
            tenant_id,
            owner_user_id,
            operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::ProjectBrainActiveList { slot: 0 }],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            command_effects: None,
        });
    }
    let raw_project_key = required_text(&payload, "project_key", 4096)?;
    let project_key = crate::project_brain::normalize_project_key(raw_project_key)
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    let (steps, result) = if admitted.operation.name == "project_brain.get" {
        (
            vec![TransactionIrStep::ProjectBrainGet {
                project_key,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        )
    } else if admitted.operation.name == "project_brain.rebuild" {
        (
            vec![TransactionIrStep::ProjectBrainRebuild {
                project_key,
                timestamp: now_unix_ms,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        )
    } else {
        let action = match admitted.operation.name {
            "project_brain.work.start" => crate::project_brain::CommandAction::WorkStart,
            "project_brain.work.refine" => crate::project_brain::CommandAction::WorkRefine,
            "project_brain.work.change" => crate::project_brain::CommandAction::WorkChange,
            "project_brain.work.finish" => crate::project_brain::CommandAction::WorkFinish,
            "project_brain.narrative.add" => crate::project_brain::CommandAction::NarrativeAdd,
            "project_brain.checker.register" => {
                crate::project_brain::CommandAction::CheckerRegister
            }
            "project_brain.checker.result" => crate::project_brain::CommandAction::CheckerResult,
            "project_brain.decision.promote" => {
                crate::project_brain::CommandAction::DecisionPromote
            }
            "project_brain.watch.add" => crate::project_brain::CommandAction::WatchAdd,
            "project_brain.watch.update" => crate::project_brain::CommandAction::WatchUpdate,
            "project_brain.watch.delete" => crate::project_brain::CommandAction::WatchDelete,
            "project_brain.cursor.prepare" => crate::project_brain::CommandAction::CursorPrepare,
            "project_brain.cursor.confirm" => crate::project_brain::CommandAction::CursorConfirm,
            _ => return Err(SemanticExecutionError::UnsupportedOperation),
        };
        let timestamp = match payload.get("timestamp") {
            None => now_unix_ms,
            Some(value) => value
                .as_u64()
                .filter(|value| *value <= i64::MAX as u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?,
        };
        (
            vec![TransactionIrStep::ProjectBrainCommand {
                project_key,
                action,
                payload_json: canonical_json(&Value::Object(payload.clone()))?,
                timestamp,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        )
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_browser_site_observation(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = positive_identity(&payload, "owner_user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let origin = required_text(&payload, "origin", 512)?.to_owned();
    let route_family = required_text(&payload, "route_family", 512)?.to_owned();
    let operation = required_text(&payload, "operation", 64)?.to_owned();
    if !crate::browser_site_observation::valid_identity(&origin, &route_family, &operation) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let (steps, result) = match admitted.operation.name {
        "browser.site_observation.get" => {
            let now_ms = payload
                .get("now_ms")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::BrowserSiteObservationGet {
                    origin,
                    route_family,
                    operation,
                    now_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "browser.site_observation.record" => {
            let observed_at_ms = payload
                .get("observed_at_ms")
                .and_then(Value::as_u64)
                .filter(|value| *value > 0)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let outcome = required_text(&payload, "outcome", 32)?.to_owned();
            if !matches!(
                outcome.as_str(),
                "success"
                    | "not_observed"
                    | "structure_mismatch"
                    | "not_found"
                    | "auth_challenge"
                    | "rate_limited"
                    | "transient_failure"
                    | "policy_denied"
            ) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let observation_json = if outcome == "success" {
                let observation = payload
                    .get("observation")
                    .filter(|value| value.is_object())
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                let encoded = canonical_json(observation)?;
                if encoded.len() > crate::generated_tofudb_ir::MAX_BROWSER_SITE_OBSERVATION_BYTES {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                Some(encoded)
            } else {
                None
            };
            (
                vec![TransactionIrStep::BrowserSiteObservationRecord {
                    origin,
                    route_family,
                    operation,
                    outcome,
                    observed_at_ms,
                    observation_json,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn contains_private_wire_graph(value: &Value) -> bool {
    match value {
        Value::Object(object) => object
            .iter()
            .any(|(key, value)| key.starts_with("_wire_") || contains_private_wire_graph(value)),
        Value::Array(values) => values.iter().any(contains_private_wire_graph),
        _ => false,
    }
}

fn event_cursor(payload: &Map<String, Value>) -> Result<Option<u64>, SemanticExecutionError> {
    match payload.get("after_sequence") {
        None => Ok(None),
        Some(Value::Number(number)) if number.as_i64() == Some(-1) => Ok(None),
        Some(Value::Number(number)) => number
            .as_u64()
            .map(Some)
            .ok_or(SemanticExecutionError::InvalidPayload),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn event_type_filters(
    payload: &Map<String, Value>,
) -> Result<(Vec<String>, Vec<String>), SemanticExecutionError> {
    let parse =
        |field: &str, maximum_items: usize, maximum_characters: usize| match payload.get(field) {
            None | Some(Value::Null) => Ok(Vec::new()),
            Some(Value::Array(values)) if values.len() <= maximum_items => values
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .filter(|value| {
                            !value.is_empty() && value.chars().count() <= maximum_characters
                        })
                        .map(str::to_owned)
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .collect(),
            _ => Err(SemanticExecutionError::InvalidPayload),
        };
    let types = parse("types", 64, 128)?;
    let type_prefixes = parse("type_prefixes", 16, 64)?;
    if type_prefixes.iter().any(|prefix| {
        !prefix.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric()
                || byte == b'_'
                || (index > 0 && matches!(byte, b'.' | b':' | b'-'))
        })
    }) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    Ok((types, type_prefixes))
}

/// Stamp the durable event-format envelope onto one event before persistence.
///
/// Every persisted task-event row carries a single integer format `v` so a
/// future reader can distinguish "parses without error" from "semantically
/// readable". A producer running newer code than this authority marks a
/// non-essential event `ignorable`; a reader that meets a newer *required*
/// event refuses rather than silently resuming a gutted stream. Reserved for
/// the envelope, never a producer namespace: a caller-supplied `v` /
/// `ignorable` is rejected outright (the caller is not the format authority).
fn stamp_event_format_envelope(event: Value) -> Result<Value, SemanticExecutionError> {
    let mut object = match event {
        Value::Object(object) => object,
        // Non-object events (legacy scalar/Null) carry no envelope; readers
        // treat an absent envelope as the pre-versioning format (v0).
        other => return Ok(other),
    };
    if object.contains_key("v") || object.contains_key("ignorable") {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    object.insert(
        "v".to_owned(),
        Value::Number(serde_json::Number::from(EVENT_FORMAT_VERSION)),
    );
    Ok(Value::Object(object))
}

fn compile_event_item(
    payload: &Map<String, Value>,
    now_unix_ms: u64,
) -> Result<IndexedStreamAppendItem, SemanticExecutionError> {
    let task_id = required_text(payload, "task_id", 512)?.to_owned();
    let application_sequence = payload
        .get("sequence")
        .and_then(Value::as_u64)
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    if payload
        .get("stream_kind")
        .is_some_and(|kind| kind.as_str() != Some("task"))
    {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let event = payload.get("event").cloned().unwrap_or(Value::Null);
    if contains_private_wire_graph(&event) {
        return Err(SemanticExecutionError::UnsupportedOperation);
    }
    let event = stamp_event_format_envelope(event)?;
    let event_timestamp = match event.get("executionAtMs") {
        None if event.get("executionOriginAtMs").is_none()
            && event.get("executionClockId").is_none() => now_unix_ms,
        None => return Err(SemanticExecutionError::InvalidPayload),
        Some(value) => {
            let timestamp = value
                .as_u64()
                .filter(|value| *value > 0 && *value <= 9_007_199_254_740_991)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let origin = event.get("executionOriginAtMs").and_then(Value::as_u64);
            let clock_id = event.get("executionClockId").and_then(Value::as_str);
            let delta_spine = event.get("type").and_then(Value::as_str) == Some("delta")
                && event.get("executionOriginAtMs").is_none()
                && event.get("executionClockId").is_none();
            if !delta_spine && (!origin.is_some_and(|value| value > 0 && value <= timestamp)
                || !clock_id.is_some_and(|value| !value.is_empty() && value.chars().count() <= 128)
            ) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            timestamp
        }
    };
    let payload_json = canonical_json(&event)?;
    let require_attempts_live = match payload.get("require_attempts_live") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(values)) if !values.is_empty() && values.len() <= 2 => values
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .filter(|text| !text.is_empty() && text.chars().count() <= 128)
                    .map(str::to_owned)
                    .ok_or(SemanticExecutionError::InvalidPayload)
            })
            .collect::<Result<Vec<_>, _>>()?,
        _ => return Err(SemanticExecutionError::InvalidPayload),
    };
    Ok(IndexedStreamAppendItem {
        task_id,
        application_sequence,
        event_type: event
            .as_object()
            .and_then(|object| object.get("type"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(|value| value.chars().take(128).collect())
            .unwrap_or_else(|| "task_event".to_owned()),
        payload_json,
        created_at_ms: i64::try_from(event_timestamp)
            .map_err(|_| SemanticExecutionError::InvalidPayload)?,
        require_attempts_live,
    })
}

fn compile_event(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let task_id = if matches!(
        admitted.operation.name,
        "event.append_batch" | "event.inspector_summary" | "event.prune"
    ) {
        String::new()
    } else {
        required_text(&payload, "task_id", 512)?.to_owned()
    };
    let (steps, result) = match admitted.operation.name {
        "event.append" => {
            let item = compile_event_item(&payload, now_unix_ms)?;
            (
                vec![TransactionIrStep::IndexedStreamAppend {
                    task_id: item.task_id,
                    application_sequence: item.application_sequence,
                    event_type: item.event_type,
                    payload_json: item.payload_json,
                    created_at_ms: item.created_at_ms,
                    require_attempts_live: item.require_attempts_live,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "event.append_batch" => {
            let values = payload
                .get("events")
                .and_then(Value::as_array)
                .filter(|events| !events.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let items = values
                .iter()
                .map(|value| {
                    value
                        .as_object()
                        .ok_or(SemanticExecutionError::InvalidPayload)
                        .and_then(|item| compile_event_item(item, now_unix_ms))
                })
                .collect::<Result<Vec<_>, _>>()?;
            (
                vec![TransactionIrStep::IndexedStreamAppendBatch { items, slot: 0 }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "event.list" => {
            let (types, type_prefixes) = event_type_filters(&payload)?;
            let payload_types = match payload.get("payload_types") {
                None | Some(Value::Null) => None,
                Some(Value::Array(values)) if values.len() <= 64 => Some(values.iter()
                    .map(|value| value.as_str()
                        .filter(|value| !value.is_empty() && value.chars().count() <= 128)
                        .map(str::to_owned)
                        .ok_or(SemanticExecutionError::InvalidPayload))
                    .collect::<Result<Vec<_>, _>>()?),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let limit = match payload.get("limit") {
                None => 500,
                Some(Value::Number(number)) => number
                    .as_u64()
                    .and_then(|limit| usize::try_from(limit).ok())
                    .filter(|limit| (1..=1000).contains(limit))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::IndexedStreamList {
                    task_id,
                    after_sequence: event_cursor(&payload)?,
                    limit,
                    types,
                    type_prefixes,
                    payload_types,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"[]".to_vec(),
                },
            )
        }
        "event.inspector_summary" => {
            let raw_task_ids = payload
                .get("task_ids")
                .and_then(Value::as_array)
                .filter(|task_ids| !task_ids.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let root_task_ids = raw_task_ids
                .iter()
                .map(|task_id| {
                    task_id
                        .as_str()
                        .filter(|task_id| !task_id.is_empty() && task_id.chars().count() <= 512)
                        .map(str::to_owned)
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .collect::<Result<Vec<_>, _>>()?;
            (
                vec![TransactionIrStep::IndexedStreamInspectorSummary {
                    root_task_ids,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"{\"records\":[]}".to_vec(),
                },
            )
        }
        "event.prune" => {
            let created_before_ms = payload
                .get("created_before_ms")
                .and_then(Value::as_u64)
                .and_then(|value| i64::try_from(value).ok())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let limit = match payload.get("limit") {
                None => 100,
                Some(Value::Number(number)) => number
                    .as_u64()
                    .and_then(|value| usize::try_from(value).ok())
                    .filter(|value| (1..=1_000).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let retention_class = match payload.get("retention_class").and_then(Value::as_str) {
                Some("streaming") => IndexedStreamRetentionClass::Streaming,
                Some("structural") => IndexedStreamRetentionClass::Structural,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::IndexedStreamPrune {
                    created_before_ms,
                    limit,
                    retention_class,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "event.latest" => (
            vec![TransactionIrStep::IndexedStreamLatest {
                task_id,
                metadata_only: match payload.get("metadata_only") {
                    None => false,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "event.bounds" => (
            vec![TransactionIrStep::IndexedStreamBounds {
                task_id,
                types: match payload.get("types") {
                    None => None,
                    Some(Value::Array(values)) if !values.is_empty()
                        && values.len() <= crate::generated_storage_operations::EVENT_READ_GUARD_MAX_TYPES => {
                        let mut types = values.iter().map(|value| value.as_str()
                            .filter(|value| !value.is_empty() && value.chars().count() <= 128)
                            .map(str::to_owned).ok_or(SemanticExecutionError::InvalidPayload))
                            .collect::<Result<Vec<_>, _>>()?;
                        types.sort();
                        types.dedup();
                        Some(types)
                    },
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
                include_read_guard: match payload.get("include_read_guard") {
                    None => false,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id: admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_desktop(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    if now_unix_ms == 0 {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = owner_user_id(&payload)?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let key = preference_key(tenant_id, owner_user_id)?;
    let missing = canonical_json(&missing_preference())?;
    let (steps, result) = match admitted.operation.name {
        "desktop.egress_agent.get" => (
            vec![TransactionIrStep::EntityRead { key, slot: 0 }],
            TransactionIrResult::SlotOrLiteral { slot: 0, missing },
        ),
        "desktop.egress_agent.initialize" => {
            let value = canonical_json(&present_preference(agent_id(&payload)?, now_unix_ms))?;
            (
                vec![
                    TransactionIrStep::EntityRead {
                        key: key.clone(),
                        slot: 0,
                    },
                    TransactionIrStep::EntityPut {
                        key,
                        value: value.clone(),
                        condition: EntityWriteCondition::SlotMissing(0),
                    },
                ],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: value,
                },
            )
        }
        "desktop.egress_agent.set" => {
            let value = canonical_json(&present_preference(agent_id(&payload)?, now_unix_ms))?;
            (
                vec![TransactionIrStep::EntityPut {
                    key,
                    value: value.clone(),
                    condition: EntityWriteCondition::Always,
                }],
                TransactionIrResult::Literal(value),
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_conversation(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let user_id = if admitted.operation.name == "conversation.trash.prune" {
        match payload.get("user_id") {
            None => admitted.request.owner_id,
            Some(_) => positive_identity(&payload, "user_id")?,
        }
    } else {
        positive_identity(&payload, "user_id")?
    };
    if user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let (steps, result, effects) = match admitted.operation.name {
        "conversation.activity_dates" => {
            let updated_at_gte = payload
                .get("updated_at_gte")
                .and_then(Value::as_i64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let created_at_lt = match payload.get("created_at_lt") {
                None | Some(Value::Null) => None,
                Some(value) => Some(
                    value
                        .as_i64()
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                ),
            };
            let day_boundaries_ms = payload
                .get("day_boundaries_ms")
                .and_then(Value::as_array)
                .filter(|values| (2..=367).contains(&values.len()))
                .ok_or(SemanticExecutionError::InvalidPayload)?
                .iter()
                .map(|value| value.as_i64().ok_or(SemanticExecutionError::InvalidPayload))
                .collect::<Result<Vec<_>, _>>()?;
            if day_boundaries_ms.windows(2).any(|pair| pair[0] >= pair[1]) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let limit = optional_nonnegative_integer(&payload, "limit", 10_000)?;
            if !(1..=10_000).contains(&limit) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::ConversationActivityDates {
                    updated_at_gte,
                    created_at_lt,
                    day_boundaries_ms,
                    limit: limit as usize,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                None,
            )
        }
        "conversation.count" => (
            vec![TransactionIrStep::ConversationCount { slot: 0 }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            None,
        ),
        "conversation.get" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            let include_messages = payload
                .get("derive_messages")
                .is_none_or(|value| !is_python_falsy_json(value));
            let message_window = match payload.get("message_window") {
                None => 0,
                Some(Value::Number(value)) => value
                    .as_i64()
                    .filter(|value| (0..=500).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let before_sequence = match payload.get("before_sequence") {
                None | Some(Value::Null) => None,
                Some(Value::Number(value)) => value.as_i64(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            if payload.get("before_sequence").is_some()
                && !payload["before_sequence"].is_null()
                && before_sequence.is_none()
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            if before_sequence.is_some() && message_window == 0
                || !include_messages && (message_window != 0 || before_sequence.is_some())
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::ConversationGet {
                    conversation_id,
                    include_messages,
                    message_window: message_window as usize,
                    before_sequence,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
                None,
            )
        }
        "conversation.list" => {
            let catalog_page = match payload.get("catalog_page") {
                None => false,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let settings_keys = match payload.get("settings_keys") {
                None | Some(Value::Null) => None,
                Some(Value::Array(values)) => {
                    let mut keys = Vec::new();
                    for value in values {
                        let key = value
                            .as_str()
                            .filter(|key| !key.is_empty())
                            .ok_or(SemanticExecutionError::InvalidPayload)?;
                        if !keys.iter().any(|existing| existing == key) && keys.len() < 64 {
                            keys.push(key.to_owned());
                        }
                    }
                    Some(keys)
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            if catalog_page {
                if payload
                    .get("include_messages")
                    .is_some_and(|value| value != &Value::Bool(false))
                    || payload
                        .get("order_by")
                        .is_some_and(|value| value != "updated_at_desc")
                {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                let limit = optional_nonnegative_integer(&payload, "limit", 500)?;
                if !(1..=1_000).contains(&limit) {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                let folder_id = payload
                    .get("folder_id")
                    .map(|_| required_text(&payload, "folder_id", 512).map(str::to_owned))
                    .transpose()?;
                let before_updated_at_ms = match payload.get("before_updated_at") {
                    None | Some(Value::Null) => None,
                    Some(value) => Some(
                        value
                            .as_u64()
                            .ok_or(SemanticExecutionError::InvalidPayload)?,
                    ),
                };
                let before_id = match payload.get("before_id") {
                    None => String::new(),
                    Some(Value::String(value)) if value.chars().count() <= 256 => value.clone(),
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                };
                if before_updated_at_ms.is_none() && !before_id.is_empty() {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                (
                    vec![TransactionIrStep::ConversationCatalogPage {
                        folder_id,
                        before_updated_at_ms,
                        before_id,
                        limit: limit as usize,
                        settings_keys,
                        slot: 0,
                    }],
                    TransactionIrResult::SlotOrLiteral {
                        slot: 0,
                        missing: Vec::new(),
                    },
                    None,
                )
            } else {
                let project_path = payload
                    .get("project_path")
                    .map(|_| required_text(&payload, "project_path", 4_096).map(str::to_owned))
                    .transpose()?;
                let title_contains = payload
                    .get("title_contains")
                    .map(|_| required_text(&payload, "title_contains", 512).map(str::to_owned))
                    .transpose()?;
                let ids = match payload.get("ids") {
                    None | Some(Value::Null) => None,
                    Some(Value::Array(values)) => {
                        let ids = values
                            .iter()
                            .filter(|value| !is_python_falsy_json(value))
                            .map(|value| match value {
                                Value::String(value) => value.clone(),
                                Value::Bool(true) => "True".to_owned(),
                                Value::Number(value) => value.to_string(),
                                value => {
                                    serde_json::to_string(value).expect("JSON values always encode")
                                }
                            })
                            .collect::<std::collections::BTreeSet<_>>();
                        if ids.is_empty() {
                            return Ok(TransactionIr {
                                tenant_id,
                                owner_user_id: user_id,
                                operation: admitted.operation.name.to_owned(),
                                operation_kind: admitted.operation.kind,
                                steps: Vec::new(),
                                result: TransactionIrResult::Literal(b"[]".to_vec()),
                                command_effects: None,
                            });
                        }
                        Some(ids)
                    }
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                };
                let signed_filter = |field: &str| match payload.get(field) {
                    None | Some(Value::Null) => Ok(None),
                    Some(value) => value
                        .as_i64()
                        .map(Some)
                        .ok_or(SemanticExecutionError::InvalidPayload),
                };
                let updated_at_gte = signed_filter("updated_at_gte")?;
                let updated_at_gt = signed_filter("updated_at_gt")?;
                let created_at_lt = signed_filter("created_at_lt")?;
                let updated_descending = match payload.get("order_by") {
                    None => true,
                    Some(Value::String(value)) if value == "updated_at_desc" => true,
                    Some(Value::String(value)) if value == "id_asc" => false,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                };
                let include_messages = match payload.get("include_messages") {
                    None => true,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                };
                let metadata_only = !include_messages
                    && [
                        updated_at_gte.is_some(),
                        updated_at_gt.is_some(),
                        created_at_lt.is_some(),
                        ids.is_some(),
                        project_path.is_some(),
                        title_contains.is_some(),
                    ]
                    .iter()
                    .all(|present| !present);
                let default_limit = if metadata_only { 10_000 } else { 1_000 };
                let limit = optional_nonnegative_integer(&payload, "limit", default_limit)?;
                if limit > 10_000 {
                    return Err(SemanticExecutionError::InvalidPayload);
                }
                (
                    vec![TransactionIrStep::ConversationList {
                        project_path,
                        title_contains,
                        ids,
                        updated_at_gte,
                        updated_at_gt,
                        created_at_lt,
                        updated_descending,
                        settings_keys,
                        include_messages,
                        limit: limit as usize,
                        slot: 0,
                    }],
                    TransactionIrResult::SlotOrLiteral {
                        slot: 0,
                        missing: Vec::new(),
                    },
                    None,
                )
            }
        }
        "conversation.settings.update" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            let updates = payload
                .get("updates")
                .and_then(Value::as_object)
                .cloned()
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let replace = match payload.get("replace") {
                None => false,
                Some(Value::Bool(value)) => *value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let expected_settings_json = match payload.get("expected_settings") {
                Some(Value::Object(value)) => Some(canonical_json(&Value::Object(value.clone()))?),
                Some(_) if replace => return Err(SemanticExecutionError::InvalidPayload),
                _ if replace => return Err(SemanticExecutionError::InvalidPayload),
                _ => None,
            };
            let expected_revision = match payload.get("expected_rev") {
                None | Some(Value::Null) => None,
                Some(value) => Some(
                    value
                        .as_u64()
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                ),
            };
            (
                vec![TransactionIrStep::ConversationSettingsUpdate {
                    conversation_id,
                    updates_json: canonical_json(&Value::Object(updates))?,
                    replace,
                    expected_settings_json,
                    expected_revision,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.metadata.update" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            let updates = payload
                .get("updates")
                .and_then(Value::as_object)
                .filter(|updates| !updates.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if updates
                .keys()
                .any(|key| !matches!(key.as_str(), "title" | "updated_at"))
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let title = match updates.get("title") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) => Some(value.clone()),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let updated_at_ms = match updates.get("updated_at") {
                None | Some(Value::Null) => None,
                Some(value) => Some(
                    value
                        .as_u64()
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                ),
            };
            if title.is_none() && updated_at_ms.is_none() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::ConversationMetadataUpdate {
                    conversation_id,
                    title,
                    updated_at_ms,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.clone" => {
            let source_conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            let destination_conversation_id =
                required_text(&payload, "destination_conv_id", 256)?.to_owned();
            let title = match payload.get("title") {
                None | Some(Value::Null) => None,
                Some(Value::String(value)) if !value.trim().is_empty() => {
                    Some(value.trim().chars().take(500).collect())
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let identity_seed = conversation_clone_identity_seed(
                &admitted,
                &source_conversation_id,
                &destination_conversation_id,
            )?;
            (
                vec![TransactionIrStep::ConversationClone {
                    source_conversation_id,
                    destination_conversation_id,
                    title,
                    identity_seed,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.delete" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            (
                vec![TransactionIrStep::ConversationDelete {
                    conversation_id,
                    deleted_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.restore" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            (
                vec![TransactionIrStep::ConversationRestore {
                    conversation_id,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.purge" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            (
                vec![TransactionIrStep::ConversationPurge {
                    conversation_id,
                    purged_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.trash.prune" => {
            let deleted_before_ms = payload
                .get("deleted_before_ms")
                .and_then(Value::as_u64)
                .filter(|value| *value > 0)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let maximum_conversations = match payload.get("max_conversations") {
                None | Some(Value::Null) => 4,
                Some(value) => value
                    .as_u64()
                    .and_then(|value| usize::try_from(value).ok())
                    .filter(|value| (1..=64).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
            };
            (
                vec![TransactionIrStep::ConversationTrashPrune {
                    deleted_before_ms,
                    maximum_conversations,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        "conversation.create" => {
            let conversation_id = required_text(&payload, "conv_id", 256)?.to_owned();
            let settings = match payload.get("settings") {
                None => json!({}),
                Some(value) if is_python_falsy_json(value) => json!({}),
                Some(Value::Object(value)) => Value::Object(value.clone()),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let title_value = match payload.get("title") {
                None => "New Chat",
                Some(value) if is_python_falsy_json(value) => "New Chat",
                Some(Value::String(value)) => value,
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let title = title_value.chars().take(500).collect::<String>();
            let created_at_ms = optional_nonnegative_integer(&payload, "created_at", now_unix_ms)?;
            let updated_at_ms =
                optional_nonnegative_integer(&payload, "updated_at", created_at_ms)?;
            (
                vec![TransactionIrStep::ConversationCreate {
                    conversation_id,
                    title,
                    settings_json: canonical_json(&settings)?,
                    created_at_ms,
                    updated_at_ms,
                    committed_at_ms: now_unix_ms,
                }],
                TransactionIrResult::Literal(canonical_json(&json!({
                    "applied": true,
                    "rev": 0
                }))?),
                Some(command_effects(admitted, &payload, now_unix_ms)?),
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: effects,
    })
}

fn compile_compaction_archive(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let user_id = positive_identity(&payload, "user_id")?;
    if user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let optional_text = |field: &str, maximum: usize| -> Result<String, SemanticExecutionError> {
        match payload.get(field) {
            None => Ok(String::new()),
            Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
            _ => Err(SemanticExecutionError::InvalidPayload),
        }
    };
    let number = |field: &str, default: u64| -> Result<u64, SemanticExecutionError> {
        match payload.get(field) {
            None => Ok(default),
            Some(Value::Number(value)) => {
                value.as_u64().ok_or(SemanticExecutionError::InvalidPayload)
            }
            _ => Err(SemanticExecutionError::InvalidPayload),
        }
    };
    let required_number = |field: &str| -> Result<u64, SemanticExecutionError> {
        payload
            .get(field)
            .and_then(Value::as_u64)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let (steps, result, effects) = match admitted.operation.name {
        "compaction_archive.create" => {
            let archive_id = required_text(&payload, "archive_id", 128)?.to_owned();
            let conversation_id = required_text(&payload, "conversation_id", 256)?.to_owned();
            let archive_scope = optional_text("archive_scope", 1024)?;
            let messages = payload
                .get("messages")
                .filter(|value| {
                    value.as_array().is_some_and(|items| {
                        items.iter().all(|item| {
                            item.as_object().is_some_and(|message| {
                                !message.contains_key("_tofuArchivedMessageCodec")
                                    && !message.contains_key("_tofuStorageProjectionCodec")
                            })
                        })
                    })
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let receipt = match payload.get("receipt") {
                None => json!({}),
                Some(Value::Object(value)) => Value::Object(value.clone()),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let trigger = {
                let value = optional_text("trigger", 32)?;
                if value.is_empty() {
                    "force".to_owned()
                } else {
                    value
                }
            };
            let created_at_ms = number("created_at_ms", now_unix_ms)?;
            (
                vec![TransactionIrStep::CompactionArchiveCreate {
                    archive_id,
                    conversation_id,
                    archive_scope,
                    messages_json: canonical_json(messages)?,
                    summary: optional_text("summary", 200_000)?,
                    receipt_json: canonical_json(&receipt)?,
                    trigger,
                    task_id: optional_text("task_id", 512)?,
                    round_num: number("round_num", 0)?,
                    model: optional_text("model", 256)?,
                    tokens_before: number("tokens_before", 0)?,
                    tokens_after: number("tokens_after", 0)?,
                    msgs_before: number("msgs_before", 0)?,
                    msgs_after: number("msgs_after", 0)?,
                    reason: optional_text("reason", 500)?,
                    created_at_ms,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some({
                    let mut outbox_payload = payload.clone();
                    for field in ["messages", "post_messages"] {
                        if let Some(value) = outbox_payload.remove(field) {
                            let encoded = canonical_json(&value)?;
                            outbox_payload.insert(format!("{field}_sha256"),
                                Value::String(format!("{:x}", Sha256::digest(&encoded))));
                            outbox_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
                        }
                    }
                    command_effects_with_outbox(admitted, &payload, now_unix_ms,
                        canonical_json(&json!({"operation":admitted.operation.name,
                            "payload":outbox_payload, "contentAuthority":"owner-bound-compaction-archive-documents"}))?)?
                }),
            )
        }
        "compaction_archive.list" => (
            vec![TransactionIrStep::CompactionArchiveList {
                conversation_id: required_text(&payload, "conversation_id", 256)?.to_owned(),
                archive_scope: optional_text("archive_scope", 1024)?,
                limit: usize::try_from(number("limit", 200)?)
                    .ok()
                    .filter(|value| (1..=1_000).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            None,
        ),
        "compaction_archive.get" => (
            vec![TransactionIrStep::CompactionArchiveGet {
                conversation_id: required_text(&payload, "conversation_id", 256)?.to_owned(),
                archive_id: required_text(&payload, "archive_id", 128)?.to_owned(),
                archive_scope: optional_text("archive_scope", 1024)?,
                include_messages: match payload.get("include_messages") {
                    None => true,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
                include_post_messages: match payload.get("include_post_messages") {
                    None => false,
                    Some(Value::Bool(value)) => *value,
                    _ => return Err(SemanticExecutionError::InvalidPayload),
                },
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
            None,
        ),
        "compaction_archive.update_summary" => {
            let receipt_json = match payload.get("receipt") {
                None => None,
                Some(Value::Object(value)) => Some(canonical_json(&Value::Object(value.clone()))?),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let post_messages_json = match payload.get("post_messages") {
                None => None,
                Some(value)
                    if value.as_array().is_some_and(|items| {
                        items.iter().all(|item| {
                            item.as_object().is_some_and(|message| {
                                !message.contains_key("_tofuArchivedMessageCodec")
                                    && !message.contains_key("_tofuStorageProjectionCodec")
                            })
                        })
                    }) =>
                {
                    Some(canonical_json(value)?)
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::CompactionArchiveUpdateSummary {
                    archive_id: required_text(&payload, "archive_id", 128)?.to_owned(),
                    archive_scope: optional_text("archive_scope", 1024)?,
                    summary: optional_text("summary", 200_000)?,
                    tokens_after: required_number("tokens_after")?,
                    msgs_after: required_number("msgs_after")?,
                    receipt_json,
                    post_messages_json,
                    committed_at_ms: now_unix_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
                Some({
                    let mut outbox_payload = payload.clone();
                    for field in ["messages", "post_messages"] {
                        if let Some(value) = outbox_payload.remove(field) {
                            let encoded = canonical_json(&value)?;
                            outbox_payload.insert(format!("{field}_sha256"),
                                Value::String(format!("{:x}", Sha256::digest(&encoded))));
                            outbox_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
                        }
                    }
                    command_effects_with_outbox(admitted, &payload, now_unix_ms,
                        canonical_json(&json!({"operation":admitted.operation.name,
                            "payload":outbox_payload, "contentAuthority":"owner-bound-compaction-archive-documents"}))?)?
                }),
            )
        }
        "compaction_archive.delete_conversation" => (
            vec![TransactionIrStep::CompactionArchiveDeleteConversation {
                conversation_id: required_text(&payload, "conversation_id", 256)?.to_owned(),
                archive_scope: optional_text("archive_scope", 1024)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            Some({
                    let mut outbox_payload = payload.clone();
                    for field in ["messages", "post_messages"] {
                        if let Some(value) = outbox_payload.remove(field) {
                            let encoded = canonical_json(&value)?;
                            outbox_payload.insert(format!("{field}_sha256"),
                                Value::String(format!("{:x}", Sha256::digest(&encoded))));
                            outbox_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
                        }
                    }
                    command_effects_with_outbox(admitted, &payload, now_unix_ms,
                        canonical_json(&json!({"operation":admitted.operation.name,
                            "payload":outbox_payload, "contentAuthority":"owner-bound-compaction-archive-documents"}))?)?
                }),
        ),
        "compaction_archive.prune" => (
            vec![TransactionIrStep::CompactionArchivePrune {
                conversation_id: required_text(&payload, "conversation_id", 256)?.to_owned(),
                archive_scope: optional_text("archive_scope", 1024)?,
                keep: usize::try_from(number("keep", 0)?)
                    .ok()
                    .filter(|value| (1..=1_000).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            Some({
                    let mut outbox_payload = payload.clone();
                    for field in ["messages", "post_messages"] {
                        if let Some(value) = outbox_payload.remove(field) {
                            let encoded = canonical_json(&value)?;
                            outbox_payload.insert(format!("{field}_sha256"),
                                Value::String(format!("{:x}", Sha256::digest(&encoded))));
                            outbox_payload.insert(format!("{field}_bytes"), Value::from(encoded.len()));
                        }
                    }
                    command_effects_with_outbox(admitted, &payload, now_unix_ms,
                        canonical_json(&json!({"operation":admitted.operation.name,
                            "payload":outbox_payload, "contentAuthority":"owner-bound-compaction-archive-documents"}))?)?
                }),
        ),
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: effects,
    })
}

fn provider_tenant_label(payload: &Map<String, Value>) -> Result<String, SemanticExecutionError> {
    match payload.get("tenant_id") {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= 256 => Ok(value.trim().to_owned()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn provider_optional_text(
    payload: &Map<String, Value>,
    field: &str,
    maximum: usize,
) -> Result<String, SemanticExecutionError> {
    match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => {
            Ok(value.trim().to_owned())
        }
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn provider_number(
    payload: &Map<String, Value>,
    field: &str,
) -> Result<f64, SemanticExecutionError> {
    let value = payload
        .get(field)
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite() && (0.0..=32_503_680_000.0).contains(value))
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    Ok(if value == 0.0 { 0.0 } else { value })
}

fn provider_physical_milliseconds(value: f64) -> u64 {
    ((value * 1_000.0) as u64).max(1)
}

fn compile_model_continuation(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let user_id = positive_identity(&payload, "user_id")?;
    if user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let conversation_id = required_text(&payload, "conversation_id", 256)?.to_owned();
    let lane_id = required_text(&payload, "lane_id", 256)?.to_owned();
    let request = match admitted.operation.name {
        "model_continuation.get" => crate::model_continuation::Request::Get {
            conversation_id,
            lane_id,
        },
        "model_continuation.put" => {
            let projection = payload
                .get("projection")
                .and_then(Value::as_object)
                .cloned()
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            crate::model_continuation::Request::Put {
                conversation_id,
                lane_id,
                projection,
                updated_at_ms: now_unix_ms,
            }
        }
        "model_continuation.delete" => crate::model_continuation::Request::Delete {
            conversation_id,
            lane_id,
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::ModelContinuation { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}
fn compile_invocation(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let user_id = positive_identity(&payload, "user_id")?;
    if user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let request = match admitted.operation.name {
        "invocation.register_child" => crate::invocation::Request::RegisterChild {
            conversation_id: required_text(&payload, "conversation_id", 512)?.to_owned(),
            parent_task_id: required_text(&payload, "parent_task_id", 512)?.to_owned(),
            child_task_id: required_text(&payload, "child_task_id", 512)?.to_owned(),
            created_at: (now_unix_ms / 1000) as i64,
            updated_at_ms: now_unix_ms,
        },
        "invocation.resolve_child" => crate::invocation::Request::ResolveChild {
            archive_scope: required_text(&payload, "archive_scope", 512)?.to_owned(),
        },
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::Invocation { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects,
    })
}

fn compile_model_routing(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner = owner_user_id(&payload)?;
    if owner != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let tenant_label = provider_tenant_label(&payload)?;
    let updated_at = || -> Result<f64, SemanticExecutionError> {
        let value = provider_number(&payload, "updated_at")?;
        if value > MAX_MODEL_ROUTING_TIMESTAMP_SECONDS {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        Ok(value)
    };
    let secret_reference = || {
        required_text(
            &payload,
            "secret_reference",
            MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS,
        )
        .map(str::to_owned)
    };
    let object_json = |field: &str| -> Result<Vec<u8>, SemanticExecutionError> {
        let object = payload
            .get(field)
            .and_then(Value::as_object)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let encoded = canonical_json(&Value::Object(object.clone()))?;
        Ok(encoded)
    };
    let (steps, result) = match admitted.operation.name {
        "model_routing.get" => (
            vec![TransactionIrStep::ModelRoutingGet {
                tenant_label,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "model_routing.commit" => {
            let expected_revision =
                optional_nonnegative_integer(&payload, "expected_revision", u64::MAX)?;
            if expected_revision == u64::MAX {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let migration_receipt_json = match payload.get("migration_receipt") {
                None | Some(Value::Null) => None,
                Some(Value::Object(value)) => {
                    let encoded = canonical_json(&Value::Object(value.clone()))?;
                    Some(encoded)
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::ModelRoutingCommit {
                    tenant_label,
                    expected_revision,
                    document_json: object_json("document")?,
                    migration_receipt_json,
                    updated_at: updated_at()?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "model_routing.migration_receipt" => (
            vec![TransactionIrStep::ModelRoutingMigrationReceiptGet {
                tenant_label,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "model_routing.migration_receipt.put" => (
            vec![TransactionIrStep::ModelRoutingMigrationReceiptPut {
                tenant_label,
                receipt_json: object_json("migration_receipt")?,
                initial_document_json: match payload.get("document") {
                    None | Some(Value::Null) => None,
                    Some(Value::Object(document)) => {
                        let encoded = canonical_json(&Value::Object(document.clone()))?;
                        Some(encoded)
                    }
                    Some(_) => return Err(SemanticExecutionError::InvalidPayload),
                },
                updated_at: updated_at()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "model_routing.secret.put" => (
            vec![TransactionIrStep::ModelRoutingSecretPut {
                tenant_label,
                secret_reference: secret_reference()?,
                ciphertext: required_text(
                    &payload,
                    "ciphertext",
                    MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS,
                )?
                .to_owned(),
                key_hint: provider_optional_text(
                    &payload,
                    "key_hint",
                    MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS,
                )?,
                updated_at: updated_at()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "model_routing.secret.get" => (
            vec![TransactionIrStep::ModelRoutingSecretGet {
                tenant_label,
                secret_reference: secret_reference()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "model_routing.secret.list" => (
            vec![TransactionIrStep::ModelRoutingSecretList {
                tenant_label,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "model_routing.secret.delete" => (
            vec![TransactionIrStep::ModelRoutingSecretDelete {
                tenant_label,
                secret_reference: secret_reference()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "model_routing.secret.prune" => {
            let active_raw = payload
                .get("active_secret_references")
                .and_then(Value::as_array)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let active_secret_references = active_raw
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|reference| !reference.is_empty())
                .map(str::to_owned)
                .collect::<std::collections::BTreeSet<_>>();
            if active_secret_references.iter().any(|reference| {
                reference.chars().count() > MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS
            }) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::ModelRoutingSecretPrune {
                    tenant_label,
                    active_secret_references,
                    updated_before: payload
                        .get("updated_before")
                        .and_then(Value::as_f64)
                        .filter(|value| {
                            value.is_finite()
                                && (0.0..=MAX_MODEL_ROUTING_TIMESTAMP_SECONDS).contains(value)
                        })
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: owner,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn worker_job_optional_text(
    payload: &Map<String, Value>,
    field: &str,
    maximum: usize,
) -> Result<String, SemanticExecutionError> {
    match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn worker_job_now(
    payload: &Map<String, Value>,
    authority_now_ms: u64,
) -> Result<u64, SemanticExecutionError> {
    let now_ms = payload
        .get("now_ms")
        .and_then(Value::as_u64)
        .filter(|value| *value <= MAX_WORKER_JOB_CLOCK_MILLISECONDS)
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    if now_ms.saturating_sub(authority_now_ms) > MAX_WORKER_JOB_FUTURE_CLOCK_SKEW_MILLISECONDS {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    Ok(now_ms)
}

fn worker_job_request_digest(
    tenant_label: &str,
    task_kind: &str,
    payload: &Value,
) -> Result<String, SemanticExecutionError> {
    let digest: [u8; 32] = Sha256::digest(canonical_json(&json!({
        "tenantId": tenant_label,
        "taskKind": task_kind,
        "payload": payload,
    }))?)
    .into();
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    Ok(encoded)
}

fn compile_automation_run(
    admitted: AdmittedRequest<'_>, authority_now_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    if admitted.operation.name=="automation.skills.source.update" {
        // Nullable fields remain required by the closed wire contract.
        if !payload.contains_key("credential_ref") || !payload.contains_key("credential_seal") {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let request:crate::automation_skill_source::Request=serde_json::from_slice(&admitted.request.payload)
            .map_err(|_|SemanticExecutionError::InvalidPayload)?;
        request.validate(admitted.request.owner_id,authority_now_ms).map_err(map_storage_error)?;
        return Ok(TransactionIr {
            tenant_id:admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
            owner_user_id:admitted.request.owner_id,operation:admitted.operation.name.to_owned(),operation_kind:admitted.operation.kind,
            steps:vec![TransactionIrStep::AutomationSkillSourceUpdate {request,slot:0}],
            result:TransactionIrResult::SlotOrLiteral {slot:0,missing:Vec::new()},
            command_effects:Some(command_effects(admitted,&payload,authority_now_ms)?),
        });
    }
    if admitted.operation.name=="automation.project.update" {
        let request:crate::automation_project::Request=serde_json::from_slice(&admitted.request.payload)
            .map_err(|_|SemanticExecutionError::InvalidPayload)?;
        request.validate(admitted.request.owner_id,authority_now_ms).map_err(map_storage_error)?;
        return Ok(TransactionIr {
            tenant_id:admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
            owner_user_id:admitted.request.owner_id,operation:admitted.operation.name.to_owned(),operation_kind:admitted.operation.kind,
            steps:vec![TransactionIrStep::AutomationProjectUpdate {request,slot:0}],
            result:TransactionIrResult::SlotOrLiteral {slot:0,missing:Vec::new()},
            command_effects:Some(command_effects(admitted,&payload,authority_now_ms)?),
        });
    }
    if admitted.operation.name=="automation.definition.update" {
        let fields=["user_id","tenant_id","definition_key","expected_version","request_id","actor_subject_id",
            "mode","record","definition_json","actions_json","first_due_ms","now_ms"];
        if payload.len()!=fields.len() || fields.iter().any(|f|!payload.contains_key(*f)) {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let request:crate::automation_definition::Request=serde_json::from_slice(&admitted.request.payload)
            .map_err(|_|SemanticExecutionError::InvalidPayload)?;
        request.validate(admitted.request.owner_id,authority_now_ms).map_err(map_storage_error)?;
        return Ok(TransactionIr {
            tenant_id:admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
            owner_user_id:admitted.request.owner_id,operation:admitted.operation.name.to_owned(),operation_kind:admitted.operation.kind,
            steps:vec![TransactionIrStep::AutomationDefinitionUpdate {request,slot:0}],
            result:TransactionIrResult::SlotOrLiteral {slot:0,missing:Vec::new()},
            command_effects:Some(command_effects(admitted,&payload,authority_now_ms)?),
        });
    }
    if admitted.operation.name == "automation.event.settle" {
        let fields = ["user_id", "tenant_id", "job_id", "worker_id", "fencing_token",
            "expected_payload", "next_delivery", "mode", "error_kind", "now_ms"];
        if payload.len() != fields.len() || fields.iter().any(|field| !payload.contains_key(*field)) {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let request: crate::automation_events::Request = serde_json::from_slice(&admitted.request.payload)
            .map_err(|_| SemanticExecutionError::InvalidPayload)?;
        request.validate(admitted.request.owner_id, authority_now_ms).map_err(map_storage_error)?;
        return Ok(TransactionIr {
            tenant_id: admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
            owner_user_id: admitted.request.owner_id, operation: admitted.operation.name.to_owned(),
            operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::AutomationEventSettle { request, slot: 0 }],
            result: TransactionIrResult::SlotOrLiteral { slot: 0, missing: Vec::new() },
            command_effects: Some(command_effects(admitted, &payload, authority_now_ms)?),
        });
    }
    let advance = admitted.operation.name == "automation.run.advance";
    if admitted.operation.name == "automation.run.approve" {
        let request: crate::automation_approval::ApproveRequest = serde_json::from_value(Value::Object(payload.clone()))
            .map_err(|_| SemanticExecutionError::InvalidPayload)?;
        request.validate(admitted.request.owner_id, authority_now_ms)
            .map_err(|_| SemanticExecutionError::InvalidPayload)?;
        return Ok(TransactionIr {
            tenant_id: admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
            owner_user_id: admitted.request.owner_id,
            operation: admitted.operation.name.to_owned(), operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::AutomationRunApprove {request, slot: 0}],
            result: TransactionIrResult::SlotOrLiteral {slot: 0, missing: Vec::new()},
            command_effects: Some(command_effects(admitted, &payload, authority_now_ms)?),
        });
    }
    if admitted.operation.name == "automation.run.cancel" {
        let request: crate::automation_run::CancelRequest = serde_json::from_slice(&admitted.request.payload)
            .map_err(|_| SemanticExecutionError::InvalidPayload)?;
        request.validate(admitted.request.owner_id, authority_now_ms).map_err(map_storage_error)?;
        return Ok(TransactionIr {
            tenant_id: admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
            owner_user_id: admitted.request.owner_id,
            operation: admitted.operation.name.to_owned(), operation_kind: admitted.operation.kind,
            steps: vec![TransactionIrStep::AutomationRunCancel {request, slot: 0}],
            result: TransactionIrResult::SlotOrLiteral {slot: 0, missing: Vec::new()},
            command_effects: Some(command_effects(admitted, &payload, authority_now_ms)?),
        });
    }
    if !advance && admitted.operation.name != "automation.run.admit" {
        return Err(SemanticExecutionError::UnsupportedOperation);
    }
    let expected_fields = if advance {
        vec!["user_id","tenant_id","run_key","run","now_ms","expected_version","worker_id","fencing_token"]
    } else {
        vec!["user_id","tenant_id","run_key","run","now_ms","definition_key","definition_revision","definition_digest"]
    };
    if expected_fields.iter().any(|f|!payload.contains_key(*f))
        || payload.keys().any(|f| !expected_fields.contains(&f.as_str()) && (advance || f != "producer_fence")) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let request: crate::automation_run::Request = serde_json::from_slice(&admitted.request.payload)
        .map_err(|_| SemanticExecutionError::InvalidPayload)?;
    request.validate(advance, admitted.request.owner_id, authority_now_ms).map_err(map_storage_error)?;
    Ok(TransactionIr {
        tenant_id: admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(), operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::AutomationRunMutation {request, advance, slot: 0}],
        result: TransactionIrResult::SlotOrLiteral {slot: 0, missing: Vec::new()},
        command_effects: Some(command_effects(admitted, &payload, authority_now_ms)?),
    })
}

fn compile_worker_job(
    admitted: AdmittedRequest<'_>,
    authority_now_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let task_id = || required_text(&payload, "task_id", 256).map(str::to_owned);
    let worker_id = || required_text(&payload, "worker_id", 256).map(str::to_owned);
    let now_ms = || worker_job_now(&payload, authority_now_ms);
    let user_id = || -> Result<u64, SemanticExecutionError> {
        let user_id = positive_identity(&payload, "user_id")?;
        if user_id != admitted.request.owner_id {
            return Err(SemanticExecutionError::AuthorityScopeMismatch);
        }
        Ok(user_id)
    };
    let (steps, result) = match admitted.operation.name {
        "worker_job.get" => (
            vec![TransactionIrStep::WorkerJobGet {
                task_id: task_id()?,
                user_id: user_id()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "worker_job.enqueue" => {
            let task_id = task_id()?;
            let user_id = user_id()?;
            let tenant_label = worker_job_optional_text(&payload, "tenant_id", 256)?;
            let task_kind = required_text(&payload, "task_kind", 128)?.to_owned();
            if crate::generated_tofudb_ir::AUTOMATION_RESERVED_WORKER_TASK_KINDS.contains(&task_kind.as_str()) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let idempotency_key = required_text(&payload, "idempotency_key", 256)?.to_owned();
            let priority = optional_nonnegative_integer(&payload, "priority", 100)?;
            if priority > MAX_WORKER_JOB_PRIORITY as u64 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let now_ms = now_ms()?;
            let available_at_ms =
                optional_nonnegative_integer(&payload, "available_at_ms", now_ms)?;
            if available_at_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let job_payload = match payload.get("payload") {
                None => Value::Object(Map::new()),
                Some(Value::Object(value)) => Value::Object(value.clone()),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let payload_json = canonical_json(&job_payload)?;
            if payload_json.len() > MAX_WORKER_JOB_PAYLOAD_BYTES {
                return Err(SemanticExecutionError::PayloadTooLarge);
            }
            let request_digest =
                worker_job_request_digest(&tenant_label, &task_kind, &job_payload)?;
            (
                vec![TransactionIrStep::WorkerJobEnqueue {
                    task_id,
                    user_id,
                    tenant_label,
                    task_kind,
                    payload_json,
                    idempotency_key,
                    request_digest,
                    priority: priority as u16,
                    available_at_ms,
                    now_ms,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "worker_job.claim_next" => {
            let raw_kinds = payload
                .get("task_kinds")
                .and_then(Value::as_array)
                .filter(|values| !values.is_empty() && values.len() <= MAX_WORKER_JOB_CLAIM_KINDS)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut task_kinds = Vec::with_capacity(raw_kinds.len());
            for value in raw_kinds {
                let kind = value
                    .as_str()
                    .filter(|kind| !kind.is_empty() && kind.chars().count() <= 128)
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                if !task_kinds.iter().any(|existing| existing == kind) {
                    task_kinds.push(kind.to_owned());
                }
            }
            let lease_ms = optional_nonnegative_integer(&payload, "lease_ms", 60_000)?;
            if !(10_000..=300_000).contains(&lease_ms) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::WorkerJobClaimNext {
                    worker_id: worker_id()?,
                    now_ms: now_ms()?,
                    lease_ms,
                    task_kinds,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "worker_job.defer" => {
            let fields = [
                "task_id",
                "worker_id",
                "fencing_token",
                "now_ms",
                "available_at_ms",
                "replay_cursor",
                "observed_cancel_sequence",
            ];
            if payload.len() != fields.len() + usize::from(payload.contains_key("observed_wake_sequence"))
                || fields.iter().any(|f| !payload.contains_key(*f)) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let now = now_ms()?;
            let available = payload
                .get("available_at_ms")
                .and_then(Value::as_u64)
                .filter(|v| {
                    *v >= now
                        && *v <= MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        && v.saturating_sub(now) <= MAX_WORKER_JOB_FUTURE_CLOCK_SKEW_MILLISECONDS
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::WorkerJobDefer {
                    task_id: task_id()?,
                    worker_id: worker_id()?,
                    fencing_token: positive_identity(&payload, "fencing_token")?,
                    now_ms: now,
                    available_at_ms: available,
                    replay_cursor: payload
                        .get("replay_cursor")
                        .and_then(Value::as_u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    observed_cancel_sequence: payload
                        .get("observed_cancel_sequence")
                        .and_then(Value::as_u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    observed_wake_sequence: optional_nonnegative_integer(&payload, "observed_wake_sequence", 0)?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "worker_job.heartbeat" => {
            let lease_ms = optional_nonnegative_integer(&payload, "lease_ms", 60_000)?;
            if !(10_000..=300_000).contains(&lease_ms) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::WorkerJobHeartbeat {
                    task_id: task_id()?,
                    worker_id: worker_id()?,
                    fencing_token: positive_identity(&payload, "fencing_token")?,
                    now_ms: now_ms()?,
                    lease_ms,
                    replay_cursor: optional_nonnegative_integer(&payload, "replay_cursor", 0)?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "worker_job.claim_state" => (
            vec![TransactionIrStep::WorkerJobClaimState {
                task_id: task_id()?,
                worker_id: worker_id()?,
                fencing_token: positive_identity(&payload, "fencing_token")?,
                now_ms: now_ms()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "worker_job.request_cancel" => (
            vec![TransactionIrStep::WorkerJobRequestCancel {
                task_id: task_id()?,
                user_id: user_id()?,
                now_ms: now_ms()?,
                reason: worker_job_optional_text(&payload, "reason", 1000)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "worker_job.complete" => {
            let terminal_status = required_text(&payload, "terminal_status", 32)?.to_owned();
            if !matches!(
                terminal_status.as_str(),
                "succeeded" | "failed" | "cancelled"
            ) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let error = match payload.get("error") {
                None => Value::Object(Map::new()),
                Some(Value::Object(value)) => Value::Object(value.clone()),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let error_json = canonical_json(&error)?;
            if error_json.len() > MAX_WORKER_JOB_ERROR_BYTES {
                return Err(SemanticExecutionError::PayloadTooLarge);
            }
            (
                vec![TransactionIrStep::WorkerJobComplete {
                    task_id: task_id()?,
                    worker_id: worker_id()?,
                    fencing_token: positive_identity(&payload, "fencing_token")?,
                    now_ms: now_ms()?,
                    terminal_status,
                    result_ref: worker_job_optional_text(&payload, "result_ref", 1024)?,
                    replay_cursor: optional_nonnegative_integer(&payload, "replay_cursor", 0)?,
                    error_json,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, authority_now_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_task_result(
    admitted: AdmittedRequest<'_>,
    authority_now_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let owner = |required: bool| -> Result<u64, SemanticExecutionError> {
        let candidate = payload.get("user_id").and_then(Value::as_u64);
        if required && candidate.is_none() {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        Ok(candidate.unwrap_or(admitted.request.owner_id))
    };
    let bool_field = |name: &str| -> Result<bool, SemanticExecutionError> {
        match payload.get(name) {
            None => Ok(false),
            Some(Value::Bool(value)) => Ok(*value),
            _ => Err(SemanticExecutionError::InvalidPayload),
        }
    };
    let mut compact_checkpoint_outbox = None;
    let (steps, result) = match admitted.operation.name {
        "record.get" => (
            vec![TransactionIrStep::TaskResultRecordGet {
                task_id: required_text(&payload, "key", 512)?.to_owned(),
                requested_user_id: owner(false)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "task_results.checkpoint" => {
            let guarded = match payload.get("guard_contract") {
                None | Some(Value::Null) => false,
                Some(Value::String(value)) if value == TASK_RESULT_CHECKPOINT_GUARD_CONTRACT => {
                    true
                }
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            let require_parent = match payload.get("require_parent") {
                None if guarded => false,
                Some(Value::Bool(value)) if guarded => *value,
                None => false,
                Some(_) => return Err(SemanticExecutionError::InvalidPayload),
            };
            let task_id = required_text(&payload, "key", 512)?.to_owned();
            let expected_version = payload
                .get("expected_version")
                .and_then(Value::as_u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let value = payload
                .get("value")
                .and_then(Value::as_object)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if value.get("user_id").and_then(Value::as_u64) != Some(admitted.request.owner_id) {
                return Err(SemanticExecutionError::AuthorityScopeMismatch);
            }
            if guarded && value.get("task_id").and_then(Value::as_str) != Some(task_id.as_str()) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let cache_prefix_hwm = value
                .get("cache_prefix_hwm")
                .map(|value| {
                    value
                        .as_u64()
                        .filter(|value| (1..=MAX_TASK_RESULT_CACHE_FACT).contains(value))
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .transpose()?;
            let last_turn_cache_read = value
                .get("last_turn_cache_read")
                .map(|value| {
                    value
                        .as_u64()
                        .filter(|value| *value <= MAX_TASK_RESULT_CACHE_FACT)
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .transpose()?;
            let cache_settings_requested =
                cache_prefix_hwm.is_some() || last_turn_cache_read.is_some();
            match payload.get("cache_settings_contract") {
                Some(Value::String(value))
                    if value == TASK_RESULT_CACHE_SETTINGS_CONTRACT
                        && cache_settings_requested
                        && guarded
                        && require_parent => {}
                None | Some(Value::Null) if !cache_settings_requested => {}
                _ => return Err(SemanticExecutionError::InvalidPayload),
            }
            let value_json = canonical_json(&Value::Object(value.clone()))?;
            if value_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES {
                return Err(SemanticExecutionError::PayloadTooLarge);
            }
            let value_digest: [u8; 32] = Sha256::digest(&value_json).into();
            compact_checkpoint_outbox = Some(canonical_json(&json!({
                "operation": admitted.operation.name,
                "key": task_id,
                "expectedVersion": expected_version,
                "valueSha256": value_digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>(),
            }))?);
            (
                vec![TransactionIrStep::TaskResultCheckpoint {
                    task_id,
                    value_json,
                    expected_version,
                    updated_at_ms: authority_now_ms.max(1),
                    guarded,
                    require_parent,
                    cache_prefix_hwm,
                    last_turn_cache_read,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "task_results.replay_get" => {
            let requested_user_id = owner(true)?;
            (
                vec![TransactionIrStep::TaskResultReplayGet {
                    task_id: required_text(&payload, "key", 512)?.to_owned(),
                    requested_user_id,
                    include_terminal_payload: bool_field("include_terminal_payload")?,
                    include_metadata: bool_field("include_metadata")?,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "task_results.abort_requested" => {
            let requested_user_id = owner(true)?;
            (
                vec![TransactionIrStep::TaskResultAbortRequested {
                    task_id: required_text(&payload, "task_id", 512)?.to_owned(),
                    requested_user_id,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "task_results.abort" => {
            let requested_user_id = owner(true)?;
            (
                vec![TransactionIrStep::TaskResultAbort {
                    task_id: required_text(&payload, "task_id", 512)?.to_owned(),
                    requested_user_id,
                    source: required_text(&payload, "source", 128)?.to_owned(),
                    requested_at_ms: authority_now_ms.max(1),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "task_results.summary_list" => {
            let requested_user_id = payload
                .contains_key("user_id")
                .then(|| owner(true))
                .transpose()?;
            let completed_before_ms = payload
                .get("completed_before_ms")
                .map(|value| {
                    value
                        .as_u64()
                        .and_then(|value| i64::try_from(value).ok())
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .transpose()?;
            let optional_text = |name: &str, maximum: usize| {
                payload
                    .get(name)
                    .map(|_| required_text(&payload, name, maximum).map(str::to_owned))
                    .transpose()
            };
            (
                vec![TransactionIrStep::TaskResultSummaryList {
                    requested_user_id,
                    status: optional_text("status", 64)?,
                    conversation_id: optional_text("conv_id", 512)?,
                    completed_before_ms,
                    limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 100)?)
                        .ok()
                        .filter(|value| (1..=MAX_TASK_RESULT_SUMMARY_ROWS).contains(value))
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    scan_limit: usize::try_from(optional_nonnegative_integer(
                        &payload,
                        "scan_limit",
                        1000,
                    )?)
                    .ok()
                    .filter(|value| (1..=MAX_TASK_RESULT_SUMMARY_SCAN_ROWS).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                    order_by: optional_text("order_by", 32)?
                        .unwrap_or_else(|| "created_at_desc".to_owned()),
                    after_key: optional_text("after_key", 512)?.unwrap_or_default(),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "task_results.recover_running" => {
            let interrupted_reason = match payload.get("interrupted_reason") {
                None | Some(Value::Null) => "server_restart".to_owned(),
                Some(Value::String(value)) if !value.is_empty() => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::TaskResultRecoverRunning {
                    interrupted_reason,
                    maximum_rows: usize::try_from(optional_nonnegative_integer(
                        &payload, "max_rows", 32,
                    )?)
                    .ok()
                    .filter(|value| (1..=MAX_TASK_RESULT_RECOVERY_ROWS).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                    scan_limit: usize::try_from(optional_nonnegative_integer(
                        &payload,
                        "scan_limit",
                        10_000,
                    )?)
                    .ok()
                    .filter(|value| (1..=MAX_TASK_RESULT_RECOVERY_SCAN_ROWS).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                    updated_at_ms: authority_now_ms.max(1),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "task_results.recovery_pending" => (
            vec![TransactionIrStep::TaskResultRecoveryPending { slot: 0 }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "task_results.cost_experiment_scan" => {
            let requested_user_id = owner(true)?;
            let completed_at_gte = payload
                .get("completed_at_gte")
                .and_then(Value::as_u64)
                .and_then(|value| i64::try_from(value).ok())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let after_key = match payload.get("after_key") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(value)) if value.chars().count() <= 1024 => value.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            (
                vec![TransactionIrStep::TaskResultCostExperimentScan {
                    requested_user_id,
                    experiment_id: required_text(&payload, "experiment_id", 128)?.to_owned(),
                    completed_at_gte,
                    limit: usize::try_from(optional_nonnegative_integer(&payload, "limit", 5_000)?)
                        .ok()
                        .filter(|value| (1..=MAX_TASK_RESULT_COST_EXPERIMENT_ROWS).contains(value))
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    scan_limit: usize::try_from(optional_nonnegative_integer(
                        &payload,
                        "scan_limit",
                        256,
                    )?)
                    .ok()
                    .filter(|value| (1..=MAX_TASK_RESULT_COST_EXPERIMENT_SCAN_ROWS).contains(value))
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                    after_key,
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = if admitted.operation.kind.mutates_state() {
        Some(if let Some(outbox) = compact_checkpoint_outbox {
            command_effects_with_outbox(admitted, &payload, authority_now_ms, outbox)?
        } else {
            command_effects(admitted, &payload, authority_now_ms)?
        })
    } else {
        None
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn compile_tenant_user(
    admitted: AdmittedRequest<'_>,
    authority_now_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let optional_text = |field: &str, maximum: usize| -> Result<String, SemanticExecutionError> {
        match payload.get(field) {
            None => Ok(String::new()),
            Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
            _ => Err(SemanticExecutionError::InvalidPayload),
        }
    };
    let normalized_email = || -> Result<String, SemanticExecutionError> {
        Ok(required_text(&payload, "email", 320)?.trim().to_lowercase())
    };
    let role = || -> Result<String, SemanticExecutionError> {
        let value = required_text(&payload, "role", 32)?;
        matches!(value, "user" | "admin")
            .then(|| value.to_owned())
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let status = |optional: bool| -> Result<Option<String>, SemanticExecutionError> {
        match payload.get("status") {
            None if optional => Ok(None),
            Some(Value::String(value)) if optional && value.is_empty() => Ok(None),
            Some(Value::String(value))
                if matches!(value.as_str(), "active" | "suspended" | "deleted") =>
            {
                Ok(Some(value.clone()))
            }
            _ => Err(SemanticExecutionError::InvalidPayload),
        }
    };
    let (steps, result) = match admitted.operation.name {
        "tenant.user.create" => {
            let metadata = payload
                .get("metadata")
                .cloned()
                .unwrap_or_else(|| json!({}));
            if !metadata.is_object() {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::TenantUserCreate {
                    request: crate::tenant_user::CreateRequest {
                        user_id: required_text(&payload, "user_id", 256)?.to_owned(),
                        email: normalized_email()?,
                        password_hash: optional_text("password_hash", 512)?,
                        display_name: optional_text("display_name", 256)?,
                        role: role()?,
                        created_at: payload
                            .get("created_at")
                            .and_then(Value::as_u64)
                            .ok_or(SemanticExecutionError::InvalidPayload)?,
                        metadata,
                        physical_updated_at_ms: authority_now_ms.max(1),
                    },
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "tenant.user.get" => {
            let has_user_id = python_truthy(payload.get("user_id"));
            let has_email = python_truthy(payload.get("email"));
            if has_user_id == has_email {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let selector = if has_user_id {
                crate::tenant_user::Selector::UserId(
                    required_text(&payload, "user_id", 256)?.to_owned(),
                )
            } else {
                crate::tenant_user::Selector::Email(normalized_email()?)
            };
            (
                vec![TransactionIrStep::TenantUserGet { selector, slot: 0 }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "tenant.user.list" => {
            let limit = usize::try_from(optional_nonnegative_integer(&payload, "limit", 100)?)
                .ok()
                .filter(|value| (1..=MAX_TENANT_USER_LIST_ROWS).contains(value))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let offset = usize::try_from(optional_nonnegative_integer(&payload, "offset", 0)?)
                .ok()
                .filter(|value| *value <= 10_000_000)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if offset
                .checked_add(limit)
                .is_none_or(|rows| rows > MAX_TENANT_USER_LIST_SCAN_ROWS)
            {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            (
                vec![TransactionIrStep::TenantUserList {
                    request: crate::tenant_user::ListRequest {
                        status: status(true)?,
                        limit,
                        offset,
                    },
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "tenant.user.set_status" => (
            vec![TransactionIrStep::TenantUserSetStatus {
                user_id: required_text(&payload, "user_id", 256)?.to_owned(),
                status: status(false)?.unwrap(),
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "tenant.user.set_role" => (
            vec![TransactionIrStep::TenantUserSetRole {
                user_id: required_text(&payload, "user_id", 256)?.to_owned(),
                role: role()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "tenant.user.authentication" => (
            vec![TransactionIrStep::TenantUserAuthentication {
                email: normalized_email()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "tenant.user.record_login" => (
            vec![TransactionIrStep::TenantUserRecordLogin {
                user_id: required_text(&payload, "user_id", 256)?.to_owned(),
                last_login_at: payload
                    .get("last_login_at")
                    .and_then(Value::as_u64)
                    .ok_or(SemanticExecutionError::InvalidPayload)?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: admitted
            .operation
            .kind
            .mutates_state()
            .then(|| command_effects(admitted, &payload, authority_now_ms))
            .transpose()?,
    })
}

fn compile_credential(
    admitted: AdmittedRequest<'_>,
    authority_now_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let optional_trimmed = |field: &str, maximum: usize| match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => {
            Ok(value.trim().to_owned())
        }
        _ => Err(SemanticExecutionError::InvalidPayload),
    };
    let number = |object: &Map<String, Value>, field: &str| {
        object
            .get(field)
            .and_then(Value::as_f64)
            .filter(|value| {
                value.is_finite()
                    && (0.0..=crate::generated_tofudb_ir::MAX_CREDENTIAL_TIMESTAMP_SECONDS)
                        .contains(value)
            })
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let integer = |object: &Map<String, Value>, field: &str| {
        object
            .get(field)
            .and_then(Value::as_u64)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let scopes = |value: Option<&Value>, require_nonempty: bool| {
        let values = value
            .and_then(Value::as_array)
            .filter(|values| values.len() <= crate::generated_tofudb_ir::MAX_CREDENTIAL_SCOPES)
            .ok_or(SemanticExecutionError::InvalidPayload)?;
        let mut normalized = values
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .filter(|scope| !scope.is_empty() && scope.chars().count() <= 128)
                    .map(str::to_owned)
                    .ok_or(SemanticExecutionError::InvalidPayload)
            })
            .collect::<Result<Vec<_>, _>>()?;
        normalized.sort();
        normalized.dedup();
        if require_nonempty && normalized.is_empty() {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        Ok(normalized)
    };
    let boundary = || {
        Ok(crate::credential::Boundary {
            owner_user_id: owner_user_id(&payload)?,
            tenant_label: optional_trimmed("tenant_id", 256)?,
        })
    };
    let create_request = || {
        let metadata = payload
            .get("metadata")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !metadata.is_object() {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let expires_at = match payload.get("expires_at") {
            None | Some(Value::Null) => None,
            Some(_) => Some(number(&payload, "expires_at")?),
        };
        Ok(crate::credential::CreateRequest {
            credential_id: required_text(&payload, "credential_id", 128)?.to_owned(),
            boundary: boundary()?,
            account_user_id: optional_trimmed("account_user_id", 256)?,
            name: required_text(&payload, "name", 80)?.trim().to_owned(),
            prefix: required_text(&payload, "prefix", 32)?.to_owned(),
            secret_hash: required_text(&payload, "secret_hash", 64)?.to_owned(),
            scopes: scopes(payload.get("scopes"), true)?,
            rate_limit_rpm: integer(&payload, "rate_limit_rpm")?,
            rate_limit_tpd: integer(&payload, "rate_limit_tpd")?,
            created_at: number(&payload, "created_at")?,
            expires_at,
            metadata,
            physical_updated_at_ms: authority_now_ms.max(1),
        })
    };
    let credential_id = || required_text(&payload, "credential_id", 128).map(str::to_owned);
    let secret_hash = || required_text(&payload, "secret_hash", 64).map(str::to_owned);
    let result = || TransactionIrResult::SlotOrLiteral {
        slot: 0,
        missing: b"null".to_vec(),
    };
    let (steps, result) = match admitted.operation.name {
        "credential.create" => (
            vec![TransactionIrStep::CredentialCreate {
                request: create_request()?,
                slot: 0,
            }],
            result(),
        ),
        "credential.create_if_owner_empty" => (
            vec![TransactionIrStep::CredentialCreateIfOwnerEmpty {
                request: create_request()?,
                slot: 0,
            }],
            result(),
        ),
        "credential.get" => (
            vec![TransactionIrStep::CredentialGet {
                boundary: boundary()?,
                credential_id: credential_id()?,
                slot: 0,
            }],
            result(),
        ),
        "credential.list" => (
            vec![TransactionIrStep::CredentialList {
                boundary: boundary()?,
                slot: 0,
            }],
            result(),
        ),
        "credential.exists" => (
            vec![TransactionIrStep::CredentialExists {
                boundary: boundary()?,
                slot: 0,
            }],
            result(),
        ),
        "credential.authenticate" => (
            vec![TransactionIrStep::CredentialAuthenticate {
                secret_hash: secret_hash()?,
                now: number(&payload, "now")?,
                slot: 0,
            }],
            result(),
        ),
        "credential.validate" => (
            vec![TransactionIrStep::CredentialValidate {
                secret_hash: secret_hash()?,
                now: number(&payload, "now")?,
                slot: 0,
            }],
            result(),
        ),
        "credential.identify" => (
            vec![TransactionIrStep::CredentialIdentify {
                secret_hash: secret_hash()?,
                slot: 0,
            }],
            result(),
        ),
        "credential.touch" => (
            vec![TransactionIrStep::CredentialTouch {
                boundary: boundary()?,
                credential_id: credential_id()?,
                used_at: number(&payload, "used_at")?,
                touch_if_before: number(&payload, "touch_if_before")?,
                slot: 0,
            }],
            result(),
        ),
        "credential.update" => {
            let updates = payload
                .get("updates")
                .and_then(Value::as_object)
                .filter(|updates| !updates.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            const ALLOWED: [&str; 7] = [
                "name",
                "scopes",
                "rate_limit_rpm",
                "rate_limit_tpd",
                "expires_at",
                "disabled",
                "metadata",
            ];
            if updates.keys().any(|key| !ALLOWED.contains(&key.as_str())) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let name = updates
                .get("name")
                .map(|_| required_text(updates, "name", 80).map(|value| value.trim().to_owned()))
                .transpose()?;
            let scopes = updates
                .get("scopes")
                .map(|value| scopes(Some(value), false))
                .transpose()?;
            let metadata = updates.get("metadata").cloned();
            if metadata.as_ref().is_some_and(|value| !value.is_object()) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let disabled = updates
                .get("disabled")
                .map(|value| {
                    value
                        .as_bool()
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .transpose()?;
            let expires_at = updates
                .get("expires_at")
                .map(|value| {
                    if value.is_null() {
                        Ok(None)
                    } else {
                        number(updates, "expires_at").map(Some)
                    }
                })
                .transpose()?;
            let request = crate::credential::UpdateRequest {
                name,
                scopes,
                rate_limit_rpm: updates
                    .get("rate_limit_rpm")
                    .map(|_| integer(updates, "rate_limit_rpm"))
                    .transpose()?,
                rate_limit_tpd: updates
                    .get("rate_limit_tpd")
                    .map(|_| integer(updates, "rate_limit_tpd"))
                    .transpose()?,
                expires_at,
                disabled,
                metadata,
                physical_updated_at_ms: authority_now_ms.max(1),
            };
            (
                vec![TransactionIrStep::CredentialUpdate {
                    boundary: boundary()?,
                    credential_id: credential_id()?,
                    request,
                    slot: 0,
                }],
                result(),
            )
        }
        "credential.revoke" => (
            vec![TransactionIrStep::CredentialRevoke {
                boundary: boundary()?,
                credential_id: credential_id()?,
                revoked_at: number(&payload, "revoked_at")?,
                slot: 0,
            }],
            result(),
        ),
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: admitted
            .operation
            .kind
            .mutates_state()
            .then(|| command_effects(admitted, &payload, authority_now_ms))
            .transpose()?,
    })
}

fn compile_billing(
    admitted: AdmittedRequest<'_>,
    authority_now_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let text =
        |field: &str, maximum: usize, required: bool| -> Result<String, SemanticExecutionError> {
            match payload.get(field) {
                None if !required => Ok(String::new()),
                Some(Value::String(value))
                    if (!required || !value.is_empty()) && value.chars().count() <= maximum =>
                {
                    Ok(value.clone())
                }
                _ => Err(SemanticExecutionError::InvalidPayload),
            }
        };
    let unsigned = |field: &str| {
        payload
            .get(field)
            .and_then(Value::as_u64)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let signed = |field: &str| {
        payload
            .get(field)
            .and_then(Value::as_i64)
            .ok_or(SemanticExecutionError::InvalidPayload)
    };
    let user = || text("user_id", 200, true);
    let entry = || {
        Ok(crate::billing::LedgerEntry {
            id: text("id", 200, true)?,
            user_id: user()?,
            ts: unsigned("ts")?,
            amount_micro: signed("amount_micro")?,
            kind: text("kind", 64, true)?,
            ref_type: text("ref_type", 100, false)?,
            ref_id: text("ref_id", 300, false)?,
            balance_after_micro: signed("balance_after_micro")?,
            note: text("note", 4000, false)?,
        })
    };
    let result = || TransactionIrResult::SlotOrLiteral {
        slot: 0,
        missing: b"null".to_vec(),
    };
    let now = authority_now_ms / 1000;
    let (steps, result) = match admitted.operation.name {
        "billing.ledger.append" => (
            vec![TransactionIrStep::BillingLedgerAppend {
                entry: entry()?,
                slot: 0,
            }],
            result(),
        ),
        "billing.ledger.find" => (
            vec![TransactionIrStep::BillingLedgerFind {
                user_id: user()?,
                kind: text("kind", 64, true)?,
                ref_type: text("ref_type", 100, false)?,
                ref_id: text("ref_id", 300, false)?,
                slot: 0,
            }],
            result(),
        ),
        "billing.ledger.list" => {
            let limit = usize::try_from(unsigned("limit")?)
                .ok()
                .filter(|v| (1..=crate::generated_tofudb_ir::MAX_BILLING_LIST_ROWS).contains(v))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let offset = usize::try_from(unsigned("offset")?)
                .map_err(|_| SemanticExecutionError::InvalidPayload)?;
            if offset
                .checked_add(limit)
                .is_none_or(|v| v > crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS)
            {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            let raw = match payload.get("kinds") {
                None | Some(Value::Null) | Some(Value::Bool(false)) => Vec::new(),
                Some(Value::Array(v)) => v.clone(),
                _ => return Err(SemanticExecutionError::InvalidPayload),
            };
            if raw.len() > 9 {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let kinds = raw
                .into_iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .collect::<Result<std::collections::BTreeSet<_>, _>>()?;
            let since_ts = payload
                .get("since_ts")
                .map(|_| unsigned("since_ts"))
                .transpose()?;
            (
                vec![TransactionIrStep::BillingLedgerList {
                    request: crate::billing::LedgerListRequest {
                        user_id: user()?,
                        limit,
                        offset,
                        kinds,
                        since_ts,
                    },
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.ledger.recompute" => (
            vec![TransactionIrStep::BillingLedgerRecompute {
                user_id: user()?,
                slot: 0,
            }],
            result(),
        ),
        "billing.payment.find" => (
            vec![TransactionIrStep::BillingPaymentFind {
                provider: text("provider", 100, true)?,
                provider_id: text("provider_id", 300, true)?,
                slot: 0,
            }],
            result(),
        ),
        "billing.payment.list" => {
            let limit = usize::try_from(unsigned("limit")?)
                .ok()
                .filter(|value| {
                    (1..=crate::generated_tofudb_ir::MAX_BILLING_LIST_ROWS).contains(value)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let offset = usize::try_from(unsigned("offset")?)
                .ok()
                .filter(|value| *value <= 10_000_000)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if offset
                .checked_add(limit)
                .is_none_or(|value| value > crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS)
            {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            (
                vec![TransactionIrStep::BillingPaymentList {
                    request: crate::billing::PaymentListRequest {
                        user_id: text("user_id", 200, false)?,
                        provider: text("provider", 200, false)?,
                        status: text("status", 200, false)?,
                        limit,
                        offset,
                    },
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.payment.record" => {
            let provider = text("provider", 100, true)?;
            let provider_id = text("provider_id", 300, true)?;
            let payload_json = serde_json::to_vec(&Value::Object(payload.clone()))
                .map_err(|_| SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::BillingPaymentRecord {
                    provider,
                    provider_id,
                    payload_json,
                    created_at: now,
                    physical_updated_at_ms: authority_now_ms,
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.payment.settle" => {
            let payment_id = text("payment_id", 200, true)?;
            let payload_json = serde_json::to_vec(&Value::Object(payload.clone()))
                .map_err(|_| SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::BillingPaymentSettle {
                    payment_id,
                    payload_json,
                    settled_at: now,
                    physical_updated_at_ms: authority_now_ms,
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.redeem_code.apply" => (
            vec![TransactionIrStep::BillingRedeemCodeApply {
                request: crate::billing::RedeemApplyRequest {
                    code: text("code", 64, true)?,
                    user_id: user()?,
                    redeemed_at: unsigned("redeemed_at")?
                        .try_into()
                        .ok()
                        .map(|value: i64| value as u64)
                        .ok_or(SemanticExecutionError::InvalidPayload)?,
                    ledger_id: text("ledger_id", 200, true)?,
                    physical_updated_at_ms: authority_now_ms,
                },
                slot: 0,
            }],
            result(),
        ),
        "billing.redeem_codes.list" => {
            let limit = usize::try_from(unsigned("limit")?)
                .ok()
                .filter(|value| {
                    (1..=crate::generated_tofudb_ir::MAX_BILLING_LIST_ROWS).contains(value)
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let offset = usize::try_from(unsigned("offset")?)
                .ok()
                .filter(|value| *value <= 10_000_000)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            if offset
                .checked_add(limit)
                .is_none_or(|value| value > crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS)
            {
                return Err(SemanticExecutionError::ResourceExhausted);
            }
            (
                vec![TransactionIrStep::BillingRedeemCodesList {
                    request: crate::billing::RedeemListRequest {
                        batch: text("batch", 80, false)?,
                        status: text("status", 32, true)?,
                        limit,
                        offset,
                    },
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.redeem_codes.mint" => {
            let raw_codes = payload
                .get("codes")
                .and_then(Value::as_array)
                .filter(|codes| {
                    (1..=crate::generated_tofudb_ir::MAX_BILLING_REDEEM_CODES_PER_MINT)
                        .contains(&codes.len())
                })
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let codes = raw_codes
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .filter(|code| !code.is_empty() && code.chars().count() <= 64)
                        .map(str::to_owned)
                        .ok_or(SemanticExecutionError::InvalidPayload)
                })
                .collect::<Result<Vec<_>, _>>()?;
            if codes
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
                != codes.len()
            {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            let bounded_integer = |field| {
                unsigned(field)?
                    .try_into()
                    .ok()
                    .map(|value: i64| value as u64)
                    .ok_or(SemanticExecutionError::InvalidPayload)
            };
            let amount_micro = bounded_integer("amount_micro")?;
            if !(1..=10_000_000_000_000).contains(&amount_micro) {
                return Err(SemanticExecutionError::InvalidPayload);
            }
            (
                vec![TransactionIrStep::BillingRedeemCodesMint {
                    request: crate::billing::RedeemMintRequest {
                        codes,
                        amount_micro,
                        batch: text("batch", 80, true)?,
                        created_by: text("created_by", 200, false)?,
                        created_at: bounded_integer("created_at")?,
                        expires_at: bounded_integer("expires_at")?,
                        note: text("note", 200, false)?,
                        physical_updated_at_ms: authority_now_ms,
                    },
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.reserve.stale" => {
            let cutoff_ts = unsigned("cutoff_ts")?
                .try_into()
                .ok()
                .map(|value: i64| value as u64)
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let limit = usize::try_from(unsigned("limit")?)
                .ok()
                .filter(|value| (1..=10_000).contains(value))
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            (
                vec![TransactionIrStep::BillingReserveStale {
                    cutoff_ts,
                    limit,
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.wallet.get" => (
            vec![TransactionIrStep::BillingWalletGet {
                user_id: user()?,
                slot: 0,
            }],
            result(),
        ),
        "billing.wallet.apply" => {
            let occurred_at = payload
                .get("occurred_at")
                .map(|_| unsigned("occurred_at"))
                .transpose()?
                .unwrap_or(now);
            (
                vec![TransactionIrStep::BillingWalletApply {
                    request: crate::billing::WalletApplyRequest {
                        user_id: user()?,
                        amount_micro: signed("amount_micro")?,
                        kind: text("kind", 64, true)?,
                        ref_type: text("ref_type", 100, false)?,
                        ref_id: text("ref_id", 300, false)?,
                        note: text("note", 4000, false)?,
                        ledger_id: text("ledger_id", 200, true)?,
                        occurred_at,
                        allow_negative: payload.get("allow_negative") == Some(&Value::Bool(true)),
                    },
                    slot: 0,
                }],
                result(),
            )
        }
        "billing.wallet.settle" => (
            vec![TransactionIrStep::BillingWalletSettle {
                request: crate::billing::WalletSettleRequest {
                    user_id: user()?,
                    ref_id: text("ref_id", 300, true)?,
                    reserved_micro: unsigned("reserved_micro")?,
                    actual_micro: unsigned("actual_micro")?,
                    note: text("note", 4000, false)?,
                    release_id: text("release_id", 200, true)?,
                    debit_id: text("debit_id", 200, true)?,
                    occurred_at: now,
                },
                slot: 0,
            }],
            result(),
        ),
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    Ok(TransactionIr {
        tenant_id,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects: admitted
            .operation
            .kind
            .mutates_state()
            .then(|| command_effects(admitted, &payload, authority_now_ms))
            .transpose()?,
    })
}

fn validate_provider_update(field: &str, value: &Value) -> Result<Value, SemanticExecutionError> {
    let singleton = Map::from_iter([(field.to_owned(), value.clone())]);
    match field {
        "name" => required_text(&singleton, field, 80).map(|value| json!(value.trim())),
        "base_url" => required_text(&singleton, field, 500).map(|value| json!(value.trim())),
        "api_key_ciphertext" => {
            provider_optional_text(&singleton, field, 32_768).map(Value::String)
        }
        "key_hint" => provider_optional_text(&singleton, field, 64).map(Value::String),
        "thinking_format" => provider_optional_text(&singleton, field, 64).map(Value::String),
        "models" => value
            .as_array()
            .filter(|values| values.len() <= 64)
            .cloned()
            .map(Value::Array)
            .ok_or(SemanticExecutionError::InvalidPayload),
        "extra_headers" => value
            .as_object()
            .filter(|values| values.len() <= 16)
            .cloned()
            .map(Value::Object)
            .ok_or(SemanticExecutionError::InvalidPayload),
        "disabled" => value
            .as_bool()
            .map(Value::Bool)
            .ok_or(SemanticExecutionError::InvalidPayload),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn compile_provider(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner_user_id = positive_identity(&payload, "owner_user_id")?;
    if owner_user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let tenant_label = provider_tenant_label(&payload)?;
    let provider_id = || required_text(&payload, "provider_id", 128).map(str::to_owned);
    let (steps, result) = match admitted.operation.name {
        "provider.list" => (
            vec![TransactionIrStep::ProviderList {
                tenant_label,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"[]".to_vec(),
            },
        ),
        "provider.get" => (
            vec![TransactionIrStep::ProviderGet {
                tenant_label,
                provider_id: provider_id()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        ),
        "provider.create" => {
            let provider_id = provider_id()?;
            let name = required_text(&payload, "name", 80)?.trim();
            let base_url = required_text(&payload, "base_url", 500)?.trim();
            let models = payload
                .get("models")
                .and_then(Value::as_array)
                .filter(|values| values.len() <= 64)
                .cloned()
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let extra_headers = payload
                .get("extra_headers")
                .and_then(Value::as_object)
                .filter(|values| values.len() <= 16)
                .cloned()
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let created_at = provider_number(&payload, "created_at")?;
            let document = json!({
                "id": provider_id.clone(),
                "owner_user_id": owner_user_id,
                "tenant_id": tenant_label.clone(),
                "name": name,
                "base_url": base_url,
                "api_key_ciphertext": provider_optional_text(&payload, "api_key_ciphertext", 32_768)?,
                "key_hint": provider_optional_text(&payload, "key_hint", 64)?,
                "models": models,
                "extra_headers": extra_headers,
                "thinking_format": provider_optional_text(&payload, "thinking_format", 64)?,
                "disabled": false,
                "created_at": created_at,
                "updated_at": created_at,
                "last_used_at": Value::Null
            });
            (
                vec![TransactionIrStep::ProviderCreate {
                    tenant_label: document["tenant_id"].as_str().unwrap().to_owned(),
                    provider_id: provider_id.to_owned(),
                    document_json: canonical_json(&document)?,
                    physical_updated_at_ms: provider_physical_milliseconds(created_at),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        "provider.update" => {
            let provider_id = provider_id()?;
            let updates = payload
                .get("updates")
                .and_then(Value::as_object)
                .filter(|updates| !updates.is_empty())
                .ok_or(SemanticExecutionError::InvalidPayload)?;
            let mut normalized = Map::new();
            for (field, value) in updates {
                normalized.insert(field.clone(), validate_provider_update(field, value)?);
            }
            let updated_at = provider_number(&payload, "updated_at")?;
            (
                vec![TransactionIrStep::ProviderUpdate {
                    tenant_label,
                    provider_id,
                    updates_json: canonical_json(&Value::Object(normalized))?,
                    updated_at,
                    physical_updated_at_ms: provider_physical_milliseconds(updated_at),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: b"null".to_vec(),
                },
            )
        }
        "provider.delete" => (
            vec![TransactionIrStep::ProviderDelete {
                tenant_label,
                provider_id: provider_id()?,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
        ),
        "provider.touch" => {
            let used_at = provider_number(&payload, "used_at")?;
            (
                vec![TransactionIrStep::ProviderTouch {
                    tenant_label,
                    provider_id: provider_id()?,
                    used_at,
                    physical_updated_at_ms: provider_physical_milliseconds(used_at),
                    slot: 0,
                }],
                TransactionIrResult::SlotOrLiteral {
                    slot: 0,
                    missing: Vec::new(),
                },
            )
        }
        _ => return Err(SemanticExecutionError::UnsupportedOperation),
    };
    let command_effects = admitted
        .operation
        .kind
        .mutates_state()
        .then(|| command_effects(admitted, &payload, now_unix_ms))
        .transpose()?;
    Ok(TransactionIr {
        tenant_id,
        owner_user_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps,
        result,
        command_effects,
    })
}

fn validate_desktop_response(response: &[u8]) -> Result<(), SemanticExecutionError> {
    let value: Value = serde_json::from_slice(response)
        .map_err(|_| integrity_failure("desktop egress agent response is not valid JSON"))?;
    let object = value
        .as_object()
        .ok_or_else(|| integrity_failure("desktop egress agent response is not a JSON object"))?;
    if object.len() != 3
        || object.get("present").and_then(Value::as_bool).is_none()
        || object.get("agent_id").and_then(Value::as_str).is_none()
        || object
            .get("updated_at_ms")
            .and_then(Value::as_u64)
            .is_none()
    {
        return Err(integrity_failure(
            "desktop egress agent response has invalid fields",
        ));
    }
    let present = object["present"].as_bool().unwrap();
    let agent = object["agent_id"].as_str().unwrap();
    let updated_at_ms = object["updated_at_ms"].as_u64().unwrap();
    if agent.chars().count() > MAX_DESKTOP_AGENT_ID_CHARACTERS
        || (!present && (!agent.is_empty() || updated_at_ms != 0))
    {
        return Err(integrity_failure(
            "desktop egress agent response violates its presence contract",
        ));
    }
    Ok(())
}

fn compile_system(admitted: AdmittedRequest<'_>) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    if !payload.is_empty() || admitted.operation.name != "system.schema_version" {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    Ok(TransactionIr {
        tenant_id: admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::SystemSchemaVersion { slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects: None,
    })
}

fn compile_rate_limit(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let request = crate::rate_limit::RecordAndCheckRequest {
        endpoint: required_text(&payload, "endpoint", 256)?.to_owned(),
        client_key: required_text(&payload, "client_key", 512)?.to_owned(),
        event_id: required_text(&payload, "event_id", 200)?.to_owned(),
        limit: payload
            .get("limit")
            .and_then(Value::as_u64)
            .filter(|value| (1..=1_000_000).contains(value))
            .ok_or(SemanticExecutionError::InvalidPayload)?,
        per_seconds: payload
            .get("per_seconds")
            .and_then(Value::as_u64)
            .filter(|value| (1..=7 * 24 * 60 * 60).contains(value))
            .ok_or(SemanticExecutionError::InvalidPayload)?,
        now_ms: now_unix_ms,
    };
    let outbox_payload = canonical_json(&json!({
        "operation": admitted.operation.name,
        "requestDigest": request_digest(admitted.operation.name, &payload),
    }))?;
    Ok(TransactionIr {
        tenant_id: admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
        owner_user_id: admitted.request.owner_id,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::RateLimitRecordAndCheck { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects: Some(command_effects_with_outbox(
            admitted,
            &payload,
            now_unix_ms,
            outbox_payload,
        )?),
    })
}

fn system_reclaim_integer(
    payload: &Map<String, Value>,
    field: &str,
    default: u64,
    minimum: u64,
    maximum: u64,
) -> Result<u64, SemanticExecutionError> {
    let value = optional_nonnegative_integer(payload, field, default)?;
    if !(minimum..=maximum).contains(&value) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    Ok(value)
}

// The online RPC is an observation + doorbell boundary, never a physical
// lane: it asks the daemon's online collector for an expedited epoch and
// reports cumulative progress (docs/TOFUDB_ONLINE_GC.md). It performs no
// physical I/O itself, so request latency stays bounded regardless of graph
// size. The offline `collect-garbage` CLI remains the exclusive heavy hammer.
fn execute_system_reclaim(
    database: &mut AuthorityDatabase,
    admitted: AdmittedRequest<'_>,
    _budget: AuthorityGarbageCollectionBudget,
) -> Result<Vec<u8>, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    system_reclaim_integer(&payload, "max_pages", 8_192, 1, 1_048_576)?;
    system_reclaim_integer(&payload, "min_free_pages", 1_024, 0, u64::MAX)?;
    let budget_ms = system_reclaim_integer(&payload, "budget_ms", 250, 10, 60_000)?;
    let handle = database.online_garbage_collection();
    if let Some(handle) = &handle {
        handle.request_expedited_epoch();
    }
    let metrics = handle.map(|handle| handle.metrics()).unwrap_or_default();
    canonical_json(&json!({
        "reclaimed": metrics.total_reclaimed_blocks,
        "reclaimed_bytes": metrics.total_reclaimed_bytes,
        "backend": "tofu-db",
        "more_candidates": metrics.epoch_in_progress,
        "budget_ms": budget_ms,
        "offline_required": false,
        "reason_code": null,
        "statistics_complete": metrics.epochs_completed > 0,
        "epochs_completed": metrics.epochs_completed,
        "epoch_in_progress": metrics.epoch_in_progress,
    }))
}

pub fn execute_admitted_request_with_gc_budget(
    database: &mut AuthorityDatabase,
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
    authority_gc_budget: AuthorityGarbageCollectionBudget,
) -> Result<Vec<u8>, SemanticExecutionError> {
    execute_admitted_request_with_notices(
        database,
        admitted,
        now_unix_ms,
        authority_gc_budget,
        &mut Vec::new(),
    ).map(crate::transaction_ir::IrResponse::into_bytes)
}

pub(crate) fn execute_admitted_request_with_notices(
    database: &mut AuthorityDatabase,
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
    authority_gc_budget: AuthorityGarbageCollectionBudget,
    notices: &mut Vec<crate::authority::ConversationCommitNotice>,
) -> Result<crate::transaction_ir::IrResponse, SemanticExecutionError> {
    if now_unix_ms >= admitted.request.deadline_unix_ms {
        return Err(SemanticExecutionError::DeadlineElapsed);
    }
    if EXECUTABLE_STORAGE_OPERATIONS
        .binary_search(&admitted.operation.name)
        .is_err()
        && !crate::semantic::is_plugin_row_operation(admitted.operation.name)
    {
        return Err(SemanticExecutionError::UnsupportedOperation);
    }
    if admitted.operation.name == "system.reclaim" {
        return execute_system_reclaim(database, admitted, authority_gc_budget).map(crate::transaction_ir::IrResponse::unverified);
    }
    execute_admitted_request_inner(database, admitted, now_unix_ms, notices)
}

fn execute_admitted_request_inner_tofusql(
    database: &mut AuthorityDatabase,
    admitted: &AdmittedRequest<'_>,
) -> Result<Option<Vec<u8>>, SemanticExecutionError> {
    crate::tofusql::execute_tofusql_query(database, admitted)
}

pub fn execute_admitted_request(
    database: &mut AuthorityDatabase,
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<Vec<u8>, SemanticExecutionError> {
    execute_admitted_request_with_gc_budget(
        database,
        admitted,
        now_unix_ms,
        AuthorityGarbageCollectionBudget::conservative(),
    )
}

fn execute_admitted_request_inner(
    database: &mut AuthorityDatabase,
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
    notices: &mut Vec<crate::authority::ConversationCommitNotice>,
) -> Result<crate::transaction_ir::IrResponse, SemanticExecutionError> {
    if now_unix_ms >= admitted.request.deadline_unix_ms {
        return Err(SemanticExecutionError::DeadlineElapsed);
    }
    if EXECUTABLE_STORAGE_OPERATIONS
        .binary_search(&admitted.operation.name)
        .is_err()
        && !crate::semantic::is_plugin_row_operation(admitted.operation.name)
    {
        return Err(SemanticExecutionError::UnsupportedOperation);
    }
    if let Some(response) = execute_admitted_request_inner_tofusql(database, &admitted)? {
        return Ok(crate::transaction_ir::IrResponse::unverified(response));
    }
    // Remaining wall-clock budget shared by every long read loop inside the
    // IR execution (sync/event replay): the admission-time check above only
    // proves the deadline was fresh on entry; loops truncate mid-operation
    // against this budget so one replay cannot monopolize the authority
    // mutex past the caller's deadline (2026-09-18 incident).
    let deadline_budget = Duration::from_millis(
        admitted
            .request
            .deadline_unix_ms
            .saturating_sub(now_unix_ms),
    );
    if admitted.operation.name == "conversation.diagnostic" {
        let ir = crate::turn::diagnostic::compile(admitted)?;
        return crate::transaction_ir::execute_transaction_ir_with_deadline(
            database,
            &ir,
            notices,
            deadline_budget,
        )
        .map_err(crate::turn::diagnostic::map_error).map(crate::transaction_ir::IrResponse::unverified);
    }
    let ir = compile_admitted_transaction(admitted, now_unix_ms)?;
    let mut response = crate::transaction_ir::execute_transaction_ir_with_json_proof(
        database,
        &ir,
        notices,
        deadline_budget,
    )
    .map_err(map_storage_error)?;
    if admitted.operation.name.starts_with("desktop.egress_agent.") {
        validate_desktop_response(&response)?;
    } else if response.json_proven() {
        crate::request_profile::work(crate::request_profile::Work::JsonResponseProofReuse, 1);
        crate::request_profile::work(crate::request_profile::Work::JsonResponseProofBytes, response.len() as u64);
    } else {
        let _validate = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_SEMANTIC_RESPONSE_VALIDATE);
        crate::response_json::validate(&response)
            .map_err(|_| integrity_failure("transaction response is not valid JSON"))?;
    }
    response.certify_snapshot_stable(&ir);
    Ok(response)
}

fn compile_admitted_transaction(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<TransactionIr, SemanticExecutionError> {
    let _compile = crate::fsync_stats::StageTimer::diagnostic(
        crate::fsync_stats::STAGE_SEMANTIC_TRANSACTION_COMPILE);
    let ir = if admitted.operation.name == "rate_limit.record_and_check" {
        compile_rate_limit(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("integration.") {
        compile_integration_workspace(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("swarm.") {
        compile_swarm(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("system.") {
        compile_system(admitted)?
    } else if admitted.operation.name.starts_with("artifact.")
        || admitted.operation.name.starts_with("tool_result_artifact.")
    {
        compile_artifact(admitted, now_unix_ms)?
    } else if admitted
        .operation
        .name
        .starts_with("browser.site_observation.")
    {
        compile_browser_site_observation(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("compaction_archive.") {
        compile_compaction_archive(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("conversation.") {
        compile_conversation(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("turn.") {
        compile_turn(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("model_continuation.") {
        compile_model_continuation(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("invocation.") {
        compile_invocation(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("model_routing.") {
        compile_model_routing(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("provider.") {
        compile_provider(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("project.dispatch.") {
        project_dispatch::compile(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("record.") {
        compile_record(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("orchestration.")
        || admitted.operation.name.starts_with("goal.")
    {
        compile_orchestration(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("queue.") {
        compile_queue(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("scheduler.") {
        compile_scheduler(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("timer.") {
        compile_timer(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("event.") {
        compile_event(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("project_brain.") {
        compile_project_brain(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("daily_cost.") {
        compile_daily_cost(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("log_aggregate.") {
        compile_log_aggregate(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("plugin.") {
        compile_plugin(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("optimizer.") {
        compile_optimizer(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("knowledge.") {
        compile_knowledge(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("paper.podcast.") {
        compile_paper_podcast(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("paper.document.") {
        compile_paper_document(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("paper.artifact.") {
        compile_paper_build_artifact(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("paper.report.")
        || admitted.operation.name.starts_with("paper.translation.")
    {
        compile_paper_artifact(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("paper.library.") {
        compile_paper_library(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("paper.") {
        compile_paper(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("raw_archive.") {
        compile_raw_archive(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("research.") {
        compile_research(admitted, now_unix_ms)?
    } else if admitted.operation.name == "project.relink" {
        compile_project_relink(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("project.recent.") {
        compile_recent_project(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("automation.") {
        compile_automation_run(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("worker_job.") {
        compile_worker_job(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("task_results.") {
        compile_task_result(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("tenant.user.") {
        compile_tenant_user(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("billing.") {
        compile_billing(admitted, now_unix_ms)?
    } else if admitted.operation.name.starts_with("credential.") {
        compile_credential(admitted, now_unix_ms)?
    } else {
        compile_desktop(admitted, now_unix_ms)?
    };
    Ok(ir)
}

/// Only explicitly certified transactional steps enter optimistic preparation.
/// Maintenance and future IR variants keep the exclusive execution path.
pub(crate) fn compile_parallel_transaction(
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<Option<TransactionIr>, SemanticExecutionError> {
    if now_unix_ms >= admitted.request.deadline_unix_ms {
        return Err(SemanticExecutionError::DeadlineElapsed);
    }
    if admitted.operation.name == "system.reclaim"
        || admitted.operation.kind
            == crate::generated_storage_operations::StorageOperationKind::Query
    {
        return Ok(None);
    }
    if EXECUTABLE_STORAGE_OPERATIONS.binary_search(&admitted.operation.name).is_err()
        && !crate::semantic::is_plugin_row_operation(admitted.operation.name)
    {
        return Ok(None);
    }
    let ir = compile_admitted_transaction(admitted, now_unix_ms)?;
    let safe = ir.steps.iter().all(|step| {
        matches!(
            step,
            TransactionIrStep::EntityRead { .. }
                | TransactionIrStep::EntityPut { .. }
                | TransactionIrStep::EntityDelete { .. }
                | TransactionIrStep::VersionedDocumentGet { .. }
                | TransactionIrStep::VersionedDocumentList { .. }
                | TransactionIrStep::VersionedDocumentPut { .. }
                | TransactionIrStep::VersionedDocumentDelete { .. }
                | TransactionIrStep::ConversationCreate { .. }
                | TransactionIrStep::ConversationGet { .. }
                | TransactionIrStep::ConversationSettingsUpdate { .. }
                | TransactionIrStep::ConversationMetadataUpdate { .. }
                | TransactionIrStep::TurnAppendSettled { .. }
                | TransactionIrStep::TurnCreatePair { .. }
                | TransactionIrStep::TurnEventRecord { .. }
                | TransactionIrStep::TurnAttemptCreate { .. }
                | TransactionIrStep::TurnAttemptClaim { .. }
                | TransactionIrStep::TurnAttemptBind { .. }
                | TransactionIrStep::TurnAttemptStart { .. }
                | TransactionIrStep::TurnProjectionUpdate { .. }
                | TransactionIrStep::StreamAppend { .. }
                | TransactionIrStep::IndexedStreamAppend { .. }
                | TransactionIrStep::IndexedStreamAppendBatch { .. }
        )
    });
    Ok(safe.then_some(ir))
}

pub fn execute_search_admitted_request(
    projection: &TurnSearchProjection,
    admitted: AdmittedRequest<'_>,
    now_unix_ms: u64,
) -> Result<Vec<u8>, SemanticExecutionError> {
    if now_unix_ms >= admitted.request.deadline_unix_ms {
        return Err(SemanticExecutionError::DeadlineElapsed);
    }
    if admitted.operation.name != "conversation.search" {
        return Err(SemanticExecutionError::UnsupportedOperation);
    }
    let payload = payload_object(&admitted.request.payload)?;
    let user_id = positive_identity(&payload, "user_id")?;
    if user_id != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let tenant_id = admitted
        .request
        .tenant_id
        .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let query = payload
        .get("query")
        .and_then(Value::as_str)
        .ok_or(SemanticExecutionError::InvalidPayload)?;
    let limit = optional_nonnegative_integer(&payload, "limit", 50)?;
    let snippet_radius = optional_nonnegative_integer(&payload, "snippet_radius", 40)?;
    let limit = usize::try_from(limit).map_err(|_| SemanticExecutionError::InvalidPayload)?;
    let snippet_radius =
        usize::try_from(snippet_radius).map_err(|_| SemanticExecutionError::InvalidPayload)?;
    let deadline_budget = Duration::from_millis(
        admitted
            .request
            .deadline_unix_ms
            .saturating_sub(now_unix_ms),
    );
    let started_at = Instant::now();
    let page_mode = match payload.get("page") {
        None => false,
        Some(Value::Bool(value)) => *value,
        _ => return Err(SemanticExecutionError::InvalidPayload),
    };
    let cursor = match payload.get("cursor") {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) => Some(value.as_str()),
        _ => return Err(SemanticExecutionError::InvalidPayload),
    };
    let page = projection
        .search_page_until(
            &ConversationSearchRequest {
                tenant_id,
                owner_user_id: user_id,
                query: query.to_owned(),
                limit,
                snippet_radius,
            },
            cursor,
            || started_at.elapsed() >= deadline_budget,
        )
        .map_err(map_storage_error)?;
    let source_incomplete = if page_mode {
        projection
            .source_incomplete(tenant_id, user_id)
            .map_err(map_storage_error)?
    } else {
        false
    };
    let result = if page_mode {
        let incomplete = page.incomplete;
        let mut value =
            serde_json::to_value(page).map_err(|_| SemanticExecutionError::InvalidPayload)?;
        let mut reasons = Vec::new();
        if source_incomplete {
            reasons.push("oversize_source");
        }
        if incomplete {
            reasons.push("scan_continuation");
        }
        value["coverage"] = json!(if !reasons.is_empty() {
            "partial"
        } else if projection
            .owner_indexing(tenant_id, user_id)
            .map_err(map_storage_error)?
        {
            "indexing"
        } else {
            "ready"
        });
        value["coverage_reasons"] = json!(reasons);
        Ok(value)
    } else {
        serde_json::to_value(page.items)
    }
    .map_err(|_| SemanticExecutionError::InvalidPayload)?;
    canonical_json(&result)
}

#[cfg(test)]
mod tests {
    include!("legacy_receipt_semantic_tests.rs");
    include!("paper_build_artifact_tests.rs");
    include!("paper_document_tests.rs");
    include!("semantic_executor_tests.rs");

    #[test]
    fn response_proof_keeps_semantic_integrity_errors_at_envelope_depth_boundaries() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let key = crate::versioned_document::record_key(7, 11, "proof", "row").unwrap();
        let bodies = [
            (format!("{}null{}", "[".repeat(125), "]".repeat(125)), true, true),
            (format!("{}null{}", "[".repeat(126), "]".repeat(126)), true, false),
            (format!("{}null{}", "[".repeat(127), "]".repeat(127)), false, false),
            ("-0".to_owned(), true, true),
            (r#"{"first":0,"$serde_json::private::RawValue":"not json"}"#.to_owned(), false, false),
        ];
        for (body, get_valid, list_valid) in bodies {
            // Raw-body seeding exercises historical/imported representations
            // that a normal record.put canonicalizes before storing.
            let mut transaction = database.begin(7, 11).unwrap();
            crate::versioned_document::put(&database, &mut transaction,
                crate::versioned_document::PutRequest {
                    key: key.clone(), namespace: "proof".into(), logical_key: "row".into(),
                    value_json: body.into_bytes(), expected_version: None, updated_at_ms: 1,
                }).unwrap();
            database.commit(transaction).unwrap();
            for (operation, valid) in [("record.get", get_valid), ("record.list", list_valid)] {
                let query = request(operation, 11,
                    json!({"namespace":"proof", "key":"row", "prefix":"row", "limit":1}), None);
                let result = execute(&mut database, &query, 2_000);
                if valid {
                    assert!(result.is_ok(), "{operation}: {result:?}");
                } else {
                    assert_eq!(result.unwrap_err(), integrity_failure("transaction response is not valid JSON"));
                }
            }
        }
    }

    include!("turn_settlement_regression_tests.rs");
    include!("semantic_event_batch_composition_tests.rs");
}
