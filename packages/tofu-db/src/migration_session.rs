//! Explicit offline operator session over inherited pipes, never storage.v2.
//! One AuthorityDatabase lease excludes serve and other importers. UUID/scope
//! checks precede each request; generated limits bound frames and catalog pages.
//! Snapshot restoration preserves raw JSON and metadata. Domain-specific indexes
//! remain the responsibility of certified adapters, not generic record replay.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;
use zeroize::Zeroize;

use crate::authority::AuthorityDatabase;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::*;
use crate::migration::{restore_document, SnapshotDocument, SnapshotRestoreOutcome};
use crate::paper_build_receipt::encode_bounded;
use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
use crate::semantic::{admit_request, AuthenticatedScope};
use crate::semantic_executor::execute_admitted_request;
use crate::versioned_document;

pub const FORMAT: &str = MIGRATION_SESSION_FORMAT;
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn denied(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
fn now_ms() -> io::Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .map_err(|_| invalid("invalid operator clock"))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OperatorRequest {
    request_id: u64,
    target_authority_uuid: String,
    operation: String,
    payload: Value,
    command_id: Option<String>,
    deadline_unix_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordSnapshot {
    namespace: String,
    key: String,
    value_json: String,
    version: u64,
    updated_at_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordIdentity {
    namespace: String,
    key: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordScan {
    cursor: Option<String>,
}

fn generic_namespace(namespace: &str) -> io::Result<()> {
    if namespace.is_empty()
        || namespace.chars().count() > 128
        || MIGRATION_SESSION_EXCLUDED_NAMESPACES.contains(&namespace)
        || MIGRATION_SESSION_EXCLUDED_PREFIXES
            .iter()
            .any(|p| namespace.starts_with(p))
    {
        return Err(invalid(
            "record namespace requires its domain migration adapter",
        ));
    }
    Ok(())
}
fn snapshot_value(snapshot: SnapshotDocument) -> io::Result<Value> {
    Ok(
        json!({"namespace":snapshot.namespace,"key":snapshot.logical_key,
        "version":snapshot.version,"updated_at_ms":snapshot.updated_at_ms,
        "value_json":String::from_utf8(snapshot.value_json).map_err(|_| invalid("snapshot JSON is not UTF-8"))?}),
    )
}
fn cursor_bytes(cursor: &str) -> io::Result<Vec<u8>> {
    if cursor.len() > MAX_ENTITY_KEY_BYTES * 2 || !cursor.len().is_multiple_of(2) {
        return Err(invalid("invalid migration cursor"));
    }
    cursor
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            let value =
                std::str::from_utf8(pair).map_err(|_| invalid("invalid migration cursor"))?;
            u8::from_str_radix(value, 16).map_err(|_| invalid("invalid migration cursor"))
        })
        .collect()
}
fn key_identity(raw: &[u8]) -> io::Result<(&str, &str)> {
    let length = raw.get(..2).ok_or_else(|| invalid("invalid record key"))?;
    let length = u16::from_be_bytes(length.try_into().unwrap()) as usize;
    let namespace = raw
        .get(2..2 + length)
        .ok_or_else(|| invalid("invalid record key"))?;
    let key = raw
        .get(2 + length..)
        .ok_or_else(|| invalid("invalid record key"))?;
    Ok((
        std::str::from_utf8(namespace).map_err(|_| invalid("invalid record namespace"))?,
        std::str::from_utf8(key).map_err(|_| invalid("invalid record key"))?,
    ))
}

fn validate_operator_request(
    database: &AuthorityDatabase,
    request: &OperatorRequest,
) -> io::Result<()> {
    let expected_uuid = Uuid::parse_str(&request.target_authority_uuid)
        .map_err(|_| invalid("invalid migration authority UUID"))?;
    if expected_uuid != database.authority_uuid() {
        return Err(denied("migration target authority identity differs"));
    }
    if request.deadline_unix_ms <= now_ms()? {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "migration request expired",
        ));
    }
    Ok(())
}

fn process(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    request: OperatorRequest,
    parent_memo: &mut crate::turn::attempt_event_migration::ParentValidationMemo,
) -> io::Result<Value> {
    validate_operator_request(database, &request)?;
    let expected_uuid = database.authority_uuid();
    match request.operation.as_str() {
        "migration.record.restore" => {
            if request.command_id.is_none() {
                return Err(invalid("snapshot restore requires a command identity"));
            }
            let record: RecordSnapshot = serde_json::from_value(request.payload)
                .map_err(|_| invalid("invalid record snapshot"))?;
            generic_namespace(&record.namespace)?;
            let key =
                versioned_document::record_key(tenant, owner, &record.namespace, &record.key)?;
            let mut transaction = database.begin(tenant, owner)?;
            let outcome = restore_document(
                database,
                &mut transaction,
                expected_uuid,
                SnapshotDocument {
                    key,
                    namespace: record.namespace,
                    logical_key: record.key,
                    version: record.version,
                    updated_at_ms: record.updated_at_ms,
                    value_json: record.value_json.into_bytes(),
                    blob_owner_user_id: owner,
                },
            )?;
            if matches!(outcome, SnapshotRestoreOutcome::Inserted) {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted":matches!(outcome,SnapshotRestoreOutcome::Inserted)}))
        }
        "migration.record.get" => {
            let record: RecordIdentity = serde_json::from_value(request.payload)
                .map_err(|_| invalid("invalid record identity"))?;
            generic_namespace(&record.namespace)?;
            let key =
                versioned_document::record_key(tenant, owner, &record.namespace, &record.key)?;
            let mut transaction = database.begin(tenant, owner)?;
            match versioned_document::read_snapshot(
                database,
                &mut transaction,
                &key,
                &record.namespace,
                &record.key,
            )? {
                Some(snapshot) => snapshot_value(snapshot),
                None => Ok(Value::Null),
            }
        }
        "migration.record.scan" => {
            let scan: RecordScan = serde_json::from_value(request.payload)
                .map_err(|_| invalid("invalid record scan"))?;
            let mut start = scan
                .cursor
                .as_deref()
                .map(cursor_bytes)
                .transpose()?
                .unwrap_or_default();
            if scan.cursor.is_some() {
                key_identity(&start)?;
                if start.len() < MAX_ENTITY_KEY_BYTES {
                    start.push(0);
                } else {
                    let index = start
                        .iter()
                        .rposition(|b| *b != 255)
                        .ok_or_else(|| invalid("migration cursor has no successor"))?;
                    start[index] += 1;
                    start.truncate(index + 1);
                }
            }
            let start = EntityKey::new(tenant, owner, RECORD_DOCUMENT_NAMESPACE, &start)?;
            let end = EntityKey::new(tenant, owner, RECORD_DOCUMENT_NAMESPACE, &[255; 2])?;
            let mut transaction = database.begin(tenant, owner)?;
            let mut rows = Vec::new();
            let mut cursor = None;
            for (key, _) in database.entity_scan(
                &mut transaction,
                &start,
                &end,
                MIGRATION_SESSION_MAX_PAGE_ROWS,
            )? {
                let (namespace, logical_key) = key_identity(key.key_bytes())?;
                rows.push(json!({"namespace":namespace,"key":logical_key}));
                cursor = Some(
                    key.key_bytes()
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect::<String>(),
                );
            }
            if rows.len() < MIGRATION_SESSION_MAX_PAGE_ROWS {
                cursor = None;
            }
            let value = json!({"records":rows,"cursor":cursor});
            encode_bounded(&value, MIGRATION_SESSION_MAX_PAGE_BYTES)?;
            Ok(value)
        }
        operation if MIGRATION_SESSION_SEMANTIC_OPERATIONS.contains(&operation) => {
            let request = Request {
                correlation_id: *Uuid::new_v4().as_bytes(),
                deadline_unix_ms: request.deadline_unix_ms,
                owner_id: owner,
                tenant_id: Some(tenant),
                command_id: request.command_id,
                schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
                operation: request.operation,
                payload: serde_json::to_vec(&request.payload)
                    .map_err(|_| invalid("invalid semantic payload"))?,
            };
            let now = now_ms()?;
            let admitted = admit_request(
                &request,
                NegotiatedProtocol {
                    protocol_version: PROTOCOL_VERSION,
                    schema_id: request.schema_id,
                },
                AuthenticatedScope {
                    owner_id: owner,
                    tenant_id: Some(tenant),
                },
                now,
            )
            .map_err(|_| invalid("invalid migration semantic request"))?;
            let bytes =
                execute_admitted_request(database, admitted, now).map_err(io::Error::other)?;
            serde_json::from_slice(&bytes).map_err(|_| invalid("invalid semantic response"))
        }
        operation if MIGRATION_SESSION_COMMAND_RECEIPT_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("receipt restore requires a command identity"));
            }
            if operation.starts_with("migration.legacy.receipt.") {
                return crate::legacy_receipt::migration::execute(
                    database, tenant, operation, request.payload,
                );
            }
            let transaction = database.begin(tenant, owner)?;
            crate::receipt_migration::execute(database, transaction, operation, request.payload)
        }
        operation if MIGRATION_SESSION_BILLING_OPERATIONS.contains(&operation) => {
            if operation == "migration.billing.restore" && request.command_id.is_none() {
                return Err(invalid("billing restore requires a command identity"));
            }
            crate::billing::migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation @ ("migration.task.result.restore"
        | "migration.task.result.get"
        | "migration.task.result.scan") => {
            if operation == "migration.task.result.restore" && request.command_id.is_none() {
                return Err(invalid("task result restore requires a command identity"));
            }
            crate::task_result::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_TENANT_USER_OPERATIONS.contains(&operation) => {
            if operation == "migration.tenant.user.restore" && request.command_id.is_none() {
                return Err(invalid("tenant user restore requires a command identity"));
            }
            crate::tenant_user::migration_execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_ATTEMPT_EVENT_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("attempt event restore requires a command identity"));
            }
            crate::turn::attempt_event_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
                parent_memo,
            )
        }
        operation if MIGRATION_SESSION_ATTEMPT_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("attempt restore requires a command identity"));
            }
            crate::turn::attempt_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_STORAGE_EVENT_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("storage event restore requires a command identity"));
            }
            crate::indexed_stream::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_CONVERSATION_SYNC_EVENT_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid(
                    "conversation sync event restore requires a command identity",
                ));
            }
            crate::turn::sync_event_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_CONVERSATION_SYNC_HEAD_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid(
                    "conversation sync head restore requires a command identity",
                ));
            }
            crate::turn::sync_head_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_TRASH_OPERATIONS.contains(&operation) => {
            if matches!(
                operation,
                "migration.trash.begin" | "migration.trash.finish"
            ) && request.command_id.is_none()
            {
                return Err(invalid("trash migration command requires an identity"));
            }
            crate::conversation_header::trash_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_CONVERSATION_OPERATIONS.contains(&operation) => {
            if request.command_id.is_none() {
                return Err(invalid("conversation restore requires a command identity"));
            }
            crate::conversation_header::migration::restore(database, tenant, owner, request.payload)
        }
        operation if MIGRATION_SESSION_TURN_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("turn restore requires a command identity"));
            }
            crate::turn::migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation if MIGRATION_SESSION_CONTINUATION_OPERATIONS.contains(&operation) => {
            if operation == "migration.continuation.restore" && request.command_id.is_none() {
                return Err(invalid("continuation restore requires a command identity"));
            }
            crate::model_continuation::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_PAPER_BUILD_BUDGET_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PAPER_DOCUMENT_BUDGET_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("paper budget restore requires a command identity"));
            }
            crate::paper_budget_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_DESKTOP_EGRESS_OPERATIONS.contains(&operation) => {
            if operation == "migration.desktop.egress.restore" && request.command_id.is_none() {
                return Err(invalid(
                    "desktop egress restore requires a command identity",
                ));
            }
            crate::desktop_egress_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_DAILY_COST_OPERATIONS.contains(&operation) => {
            if operation == "migration.daily.cost.restore" && request.command_id.is_none() {
                return Err(invalid("daily cost restore requires a command identity"));
            }
            crate::daily_cost_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_RATE_LIMIT_OPERATIONS.contains(&operation) => {
            if operation == "migration.rate.limit.event.restore" && request.command_id.is_none() {
                return Err(invalid("rate limit restore requires a command identity"));
            }
            crate::rate_limit_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_ORCHESTRATION_DEFINITION_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_ORCHESTRATION_RUN_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_ORCHESTRATION_EVENT_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("orchestration restore requires a command identity"));
            }
            crate::orchestration::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_SWARM_SESSION_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_SWARM_AGENT_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("swarm restore requires a command identity"));
            }
            crate::swarm::migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation
            if MIGRATION_SESSION_INTEGRATION_WORKSPACE_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_INTEGRATION_EVENT_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_INTEGRATION_META_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("integration restore requires a command identity"));
            }
            crate::integration_workspace::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_PAPER_LIBRARY_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PAPER_REPORT_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PAPER_TRANSLATION_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PAPER_NOTE_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PAPER_PODCAST_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("paper restore requires a command identity"));
            }
            crate::paper_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation if MIGRATION_SESSION_KNOWLEDGE_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("knowledge restore requires a command identity"));
            }
            crate::knowledge_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation
            if MIGRATION_SESSION_CHAT_ARTIFACT_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_TOOL_RESULT_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("artifact restore requires a command identity"));
            }
            crate::artifact_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation if MIGRATION_SESSION_BROWSER_OBSERVATION_OPERATIONS.contains(&operation) => {
            if operation == "migration.browser.observation.restore" && request.command_id.is_none()
            {
                return Err(invalid(
                    "browser observation restore requires a command identity",
                ));
            }
            crate::browser_observation_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_MODEL_ROUTING_AUTHORITY_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_MODEL_ROUTING_SECRET_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("model routing restore requires a command identity"));
            }
            crate::model_routing_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_PLUGIN_MANIFEST_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PLUGIN_ROWS_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_PLUGIN_UNIQUE_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("plugin restore requires a command identity"));
            }
            crate::plugin_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation if MIGRATION_SESSION_PROJECT_BRAIN_OPERATIONS.contains(&operation) => {
            if operation == "migration.project.brain.project.restore"
                && request.command_id.is_none()
            {
                return Err(invalid("project brain restore requires a command identity"));
            }
            crate::project_brain_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_OPTIMIZER_OPERATIONS.contains(&operation) => {
            if operation == "migration.optimizer.restore" && request.command_id.is_none() {
                return Err(invalid("optimizer restore requires a command identity"));
            }
            crate::optimizer_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation if MIGRATION_SESSION_LOG_AGGREGATE_OPERATIONS.contains(&operation) => {
            if operation == "migration.log.aggregate.restore" && request.command_id.is_none() {
                return Err(invalid("log aggregate restore requires a command identity"));
            }
            crate::log_aggregate_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation
            if MIGRATION_SESSION_BILLING_PAYMENT_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_BILLING_REDEEM_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid(
                    "billing payment/redeem restore requires a command identity",
                ));
            }
            crate::billing::migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation
            if MIGRATION_SESSION_QUEUE_ITEM_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_QUEUE_AUTOPILOT_MARKER_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("queue restore requires a command identity"));
            }
            crate::queue_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation
            if MIGRATION_SESSION_TIMER_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_TIMER_POLL_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("timer restore requires a command identity"));
            }
            crate::timer_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation
            if MIGRATION_SESSION_SCHEDULER_TASK_OPERATIONS.contains(&operation)
                || MIGRATION_SESSION_SCHEDULER_POLL_OPERATIONS.contains(&operation) =>
        {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("scheduler restore requires a command identity"));
            }
            crate::scheduler_migration::execute(database, tenant, owner, operation, request.payload)
        }
        operation if MIGRATION_SESSION_WORKER_JOB_OPERATIONS.contains(&operation) => {
            if operation == "migration.worker.job.restore" && request.command_id.is_none() {
                return Err(invalid("worker job restore requires a command identity"));
            }
            crate::worker_job_migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_COMPACTION_ARCHIVE_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("archive restore requires a command identity"));
            }
            crate::compaction_archive::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_RAW_ARCHIVE_OPERATIONS.contains(&operation) => {
            if operation.ends_with(".restore") && request.command_id.is_none() {
                return Err(invalid("raw archive restore requires a command identity"));
            }
            crate::raw_archive::migration::execute(
                database,
                tenant,
                owner,
                operation,
                request.payload,
            )
        }
        operation if MIGRATION_SESSION_FROZEN_TRANSCRIPT_OPERATIONS.contains(&operation) => {
            if !matches!(
                operation,
                "migration.frozen.transcript.get"
                    | "migration.frozen.transcript.scan"
                    | "migration.frozen.transcript.chunk"
            ) && request.command_id.is_none()
            {
                return Err(invalid(
                    "frozen transcript mutation requires a command identity",
                ));
            }
            crate::frozen_transcript::execute(database, tenant, owner, operation, request.payload)
        }
        _ => Err(denied("operation is outside the offline migration plan")),
    }
}

fn read_frame(reader: &mut impl BufRead, frame: &mut Vec<u8>) -> io::Result<bool> {
    frame.zeroize();
    frame.clear();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if frame.is_empty() {
                Ok(false)
            } else {
                Err(invalid("truncated migration frame"))
            };
        }
        let count = available
            .iter()
            .position(|b| *b == b'\n')
            .map_or(available.len(), |n| n + 1);
        if count > MIGRATION_SESSION_MAX_FRAME_BYTES.saturating_sub(frame.len()) {
            return Err(invalid("migration frame exceeds its byte bound"));
        }
        if frame.capacity() < frame.len() + count {
            let target = (frame.len() + count)
                .max(frame.capacity().saturating_mul(2))
                .min(MIGRATION_SESSION_MAX_FRAME_BYTES);
            frame.reserve_exact(target - frame.len());
        }
        frame.extend_from_slice(&available[..count]);
        reader.consume(count);
        if frame.last() == Some(&b'\n') {
            return Ok(true);
        }
    }
}
pub fn run(
    path: &Path,
    tenant: u64,
    owner: u64,
    mut reader: impl BufRead,
    mut writer: impl Write,
) -> io::Result<()> {
    if !path.is_absolute() || tenant == 0 || owner == 0 {
        return Err(invalid(
            "migration requires an absolute authority and explicit positive scope",
        ));
    }
    let mut database = AuthorityDatabase::open(path)?;
    writer.write_all(&serde_json::to_vec(&json!({"format":FORMAT,"authority_uuid":database.authority_uuid().to_string(),"tenant_id":tenant,"owner_id":owner})).unwrap())?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    let mut frame = zeroize::Zeroizing::new(Vec::new());
    let mut previous_id = 0;
    let mut event_upload = crate::turn::attempt_event_migration::UploadState::default();
    let mut parent_memo = crate::turn::attempt_event_migration::ParentValidationMemo::default();
    let mut turn_transfer = crate::turn::migration::transfer::TransferState::default();
    let mut frozen_upload = crate::frozen_transcript::upload::UploadState::default();
    while read_frame(&mut reader, &mut frame)? {
        let request: OperatorRequest =
            serde_json::from_slice(&frame).map_err(|_| invalid("invalid migration frame"))?;
        if request.request_id <= previous_id {
            return Err(invalid("migration correlation is not monotonic"));
        }
        previous_id = request.request_id;
        let result = if MIGRATION_SESSION_ATTEMPT_EVENT_UPLOAD_OPERATIONS
            .contains(&request.operation.as_str())
        {
            validate_operator_request(&database, &request).and_then(|()| {
                if request.command_id.is_none() {
                    return Err(invalid("event upload requires a command identity"));
                }
                if frozen_upload.has_pending() || turn_transfer.has_pending() {
                    return Err(invalid("another migration upload is active"));
                }
                event_upload.execute(
                    &mut database,
                    tenant,
                    owner,
                    &request.operation,
                    request.payload,
                    &mut parent_memo,
                )
            })
        } else if MIGRATION_SESSION_FROZEN_TRANSCRIPT_UPLOAD_OPERATIONS
            .contains(&request.operation.as_str())
        {
            validate_operator_request(&database, &request).and_then(|()| {
                if request.command_id.is_none() {
                    return Err(invalid(
                        "frozen transcript upload requires a command identity",
                    ));
                }
                if event_upload.has_pending() || turn_transfer.has_pending() {
                    return Err(invalid("another migration upload is active"));
                }
                frozen_upload.execute(
                    &mut database,
                    tenant,
                    owner,
                    &request.operation,
                    request.payload,
                )
            })
        } else if request.operation.starts_with("migration.turn.transfer.") {
            validate_operator_request(&database, &request).and_then(|()| {
                if request.command_id.is_none() {
                    return Err(invalid("Turn transfer requires a command identity"));
                }
                if event_upload.has_pending() || frozen_upload.has_pending() {
                    return Err(invalid("another migration upload is active"));
                }
                turn_transfer.execute(
                    &mut database,
                    tenant,
                    owner,
                    &request.operation,
                    request.payload,
                )
            })
        } else {
            process(&mut database, tenant, owner, request, &mut parent_memo)
        };
        let response = match result {
            Ok(value) => json!({"request_id":previous_id,"ok":true,"value":value}),
            Err(error) => {
                let code = if let Some(semantic) = error.get_ref().and_then(|e| {
                    e.downcast_ref::<crate::semantic_executor::SemanticExecutionError>()
                }) {
                    semantic.code()
                } else {
                    match error.kind() {
                        io::ErrorKind::InvalidInput => "database_protocol_error",
                        io::ErrorKind::PermissionDenied => "database_protocol_error",
                        io::ErrorKind::WouldBlock => "database_conflict",
                        io::ErrorKind::TimedOut => "deadline_elapsed",
                        io::ErrorKind::InvalidData => "database_integrity_error",
                        _ => "database_unavailable",
                    }
                };
                json!({"request_id":previous_id,"ok":false,"error_code":code,"message":error.to_string()})
            }
        };
        let bytes = zeroize::Zeroizing::new(encode_bounded(
            &response,
            MIGRATION_SESSION_MAX_FRAME_BYTES - 1,
        )?);
        writer.write_all(&bytes)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/migration")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/migration"), vfs.clone()).unwrap();
        (vfs, db)
    }
    fn request(db: &AuthorityDatabase, operation: &str, payload: Value) -> OperatorRequest {
        OperatorRequest {
            request_id: 1,
            target_authority_uuid: db.authority_uuid().to_string(),
            operation: operation.into(),
            payload,
            command_id: Some(Uuid::new_v4().to_string()),
            deadline_unix_ms: now_ms().unwrap() + 60_000,
        }
    }
    fn restore(db: &mut AuthorityDatabase, key: &str, raw: &str) -> io::Result<Value> {
        let req = request(
            db,
            "migration.record.restore",
            json!({"namespace":"prefs","key":key,
            "value_json":raw,"version":1042,"updated_at_ms":123456789}),
        );
        process(db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default())
    }

    #[test]
    fn offline_snapshot_survives_reopen_and_retry_without_metadata_or_json_changes() {
        let (vfs, mut db) = prepared();
        let raw = format!(
            "{{ \"a\": 1.00, \"payload\": \"{}\" }}\n",
            "x".repeat(32 * 1024)
        );
        assert_eq!(restore(&mut db, "a", &raw).unwrap()["inserted"], true);
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/migration"), vfs).unwrap();
        assert_eq!(restore(&mut db, "a", &raw).unwrap()["inserted"], false);
        let req = request(
            &db,
            "migration.record.get",
            json!({"namespace":"prefs","key":"a"}),
        );
        let value = process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap();
        assert_eq!(value["value_json"], raw);
        assert_eq!(value["version"], 1042);
        assert_eq!(value["updated_at_ms"], 123456789);
        assert!(restore(&mut db, "a", "{}").is_err());
    }

    #[test]
    fn offline_uuid_deadline_allowlist_and_domain_boundaries_precede_writes() {
        let (_, mut db) = prepared();
        for variant in 0..4 {
            let mut req = request(
                &db,
                "migration.record.restore",
                json!({"namespace":"prefs",
                "key":"a","value_json":"{}","version":1,"updated_at_ms":1}),
            );
            match variant {
                0 => req.target_authority_uuid = Uuid::new_v4().to_string(),
                1 => req.deadline_unix_ms = 1,
                2 => req.operation = "record.put".into(),
                _ => req.payload["namespace"] = json!("task_results"),
            }
            assert!(process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).is_err());
        }
        let req = request(&db, "migration.record.scan", json!({"cursor":null}));
        assert_eq!(process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap()["records"], json!([]));
    }

    #[test]
    fn offline_catalog_pages_are_owner_scoped_and_cover_maximum_length_keys() {
        let (_, mut db) = prepared();
        for i in 0..130 {
            restore(&mut db, &format!("{i:04}"), "{}").unwrap();
        }
        let longest = "z".repeat(MAX_ENTITY_KEY_BYTES - 2 - "prefs".len());
        restore(&mut db, &longest, "{}").unwrap();
        let mut cursor = Value::Null;
        let mut count = 0;
        loop {
            let req = request(&db, "migration.record.scan", json!({"cursor":cursor}));
            let page = process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap();
            let rows = page["records"].as_array().unwrap();
            assert!(rows.len() <= MIGRATION_SESSION_MAX_PAGE_ROWS);
            count += rows.len();
            cursor = page["cursor"].clone();
            if cursor.is_null() {
                break;
            }
        }
        assert_eq!(count, 131);
        let raw = versioned_document::record_key(7, 11, "prefs", &longest).unwrap();
        let cursor: String = raw.key_bytes().iter().map(|b| format!("{b:02x}")).collect();
        let req = request(&db, "migration.record.scan", json!({"cursor":cursor}));
        assert_eq!(process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap()["records"], json!([]));
        let req = request(&db, "migration.record.scan", json!({"cursor":null}));
        assert_eq!(process(&mut db, 7, 12, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap()["records"], json!([]));
    }

    fn paper_restore_and_marker(db: &mut AuthorityDatabase) -> io::Result<()> {
        let raw =
            serde_json::to_string(&json!({"build_id": "b".repeat(32), "state": "pinned"})).unwrap();
        let req = request(
            db,
            "migration.record.restore",
            json!({"namespace":"paper.build.artifact.v1","key":"11:doc:build",
            "value_json":raw,"version":7,"updated_at_ms":424242}),
        );
        process(db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default())?;
        let marker = EntityKey::new(7, 11, "migration_paper_test", b"verified-prefix").unwrap();
        let mut transaction = db.begin(7, 11)?;
        if db.entity_get(&mut transaction, &marker)?.is_none() {
            db.entity_put(&mut transaction, marker, b"verified-prefix-1".to_vec())?;
            db.commit(transaction)?;
        }
        Ok(())
    }

    fn paper_snapshot(db: &mut AuthorityDatabase) -> Value {
        let req = request(
            db,
            "migration.record.get",
            json!({"namespace":"paper.build.artifact.v1","key":"11:doc:build"}),
        );
        process(db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap()
    }

    #[test]
    fn offline_paper_record_snapshot_survives_reopen_and_retry_without_changes() {
        let (vfs, mut db) = prepared();
        paper_restore_and_marker(&mut db).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/migration"), vfs).unwrap();
        paper_restore_and_marker(&mut db).unwrap();
        let snapshot = paper_snapshot(&mut db);
        assert_eq!(snapshot["version"], 7);
        assert_eq!(snapshot["updated_at_ms"], 424242);
        let document: Value =
            serde_json::from_str(snapshot["value_json"].as_str().unwrap()).unwrap();
        assert_eq!(
            document,
            json!({"build_id": "b".repeat(32), "state": "pinned"})
        );
        let divergent = request(
            &db,
            "migration.record.restore",
            json!({"namespace":"paper.build.artifact.v1","key":"11:doc:build",
            "value_json":"{}","version":7,"updated_at_ms":424242}),
        );
        assert!(process(&mut db, 7, 11, divergent, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).is_err());
    }

    #[test]
    fn offline_paper_document_namespaces_round_trip_and_page_independently() {
        let (_, mut db) = prepared();
        // The paper.document.budget.v1 ledger is excluded: it replays only
        // through the dedicated migration.paper.document.budget adapter.
        for (namespace, key) in [
            ("paper.document.v1", "11:doc"),
            ("paper.document.source.v1", "11:binding"),
            ("paper.document.current.v1", "11:conv"),
            ("paper.build.chunk.v1", "11:doc:build:0"),
            ("paper.build.document.v1", "11:doc"),
        ] {
            let req = request(
                &db,
                "migration.record.restore",
                json!({"namespace":namespace,"key":key,
                "value_json":"{\"v\":1}","version":1,"updated_at_ms":1}),
            );
            assert_eq!(process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap()["inserted"], true);
        }
        let mut seen = Vec::new();
        let mut cursor = Value::Null;
        loop {
            let req = request(&db, "migration.record.scan", json!({"cursor":cursor}));
            let page = process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default()).unwrap();
            for row in page["records"].as_array().unwrap() {
                seen.push((
                    row["namespace"].as_str().unwrap().to_owned(),
                    row["key"].as_str().unwrap().to_owned(),
                ));
            }
            cursor = page["cursor"].clone();
            if cursor.is_null() {
                break;
            }
        }
        assert_eq!(seen.len(), 5);
        assert!(seen.contains(&(
            "paper.build.chunk.v1".to_owned(),
            "11:doc:build:0".to_owned()
        )));
        assert!(seen.contains(&("paper.document.v1".to_owned(), "11:doc".to_owned())));
    }

    #[test]
    fn offline_paper_record_and_progress_recover_together_under_every_commit_fault() {
        use crate::vfs::{FaultAction, FaultRule, Operation};
        fn run(vfs: &Arc<DeterministicVfs>) -> io::Result<()> {
            let mut db = AuthorityDatabase::open_with_vfs(Path::new("/migration"), vfs.clone())?;
            let raw = serde_json::to_string(&json!({"state": "pinned"})).unwrap();
            let req = OperatorRequest {
                request_id: 1,
                target_authority_uuid: db.authority_uuid().to_string(),
                operation: "migration.record.restore".into(),
                payload: json!({"namespace":"paper.build.artifact.v1","key":"11:doc:build",
                "value_json":raw,"version":7,"updated_at_ms":424242}),
                command_id: Some(Uuid::new_v4().to_string()),
                deadline_unix_ms: now_ms().unwrap() + 60_000,
            };
            assert_eq!(process(&mut db, 7, 11, req, &mut crate::turn::attempt_event_migration::ParentValidationMemo::default())?["inserted"], true);
            let marker = EntityKey::new(7, 11, "migration_paper_test", b"verified-prefix").unwrap();
            let mut transaction = db.begin(7, 11)?;
            db.entity_put(&mut transaction, marker, b"verified-prefix-1".to_vec())?;
            db.commit(transaction)?;
            Ok(())
        }
        // Enumerate commit IO from a clean run on a fresh trace.
        let (trace_vfs, trace_db) = prepared();
        drop(trace_db);
        run(&trace_vfs).unwrap();
        let trace = trace_vfs.trace().unwrap();
        assert!(!trace.is_empty());
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
            if operation == &Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            if matches!(
                operation,
                Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
            ) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let (vfs, _db) = prepared();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = run(&vfs).is_ok();
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut db =
                    AuthorityDatabase::open_with_vfs(Path::new("/migration"), vfs).unwrap();
                let snapshot = paper_snapshot(&mut db);
                let marker = {
                    let mut transaction = db.begin(7, 11).unwrap();
                    db.entity_get(
                        &mut transaction,
                        &EntityKey::new(7, 11, "migration_paper_test", b"verified-prefix").unwrap(),
                    )
                    .unwrap()
                };
                assert_eq!(snapshot.is_null(), marker.is_none(), "{index}: {action:?}");
                assert!(
                    !acknowledged || !snapshot.is_null(),
                    "lost acknowledgement {index}: {action:?}"
                );
                if !snapshot.is_null() {
                    assert_eq!(snapshot["version"], 7);
                    assert_eq!(snapshot["updated_at_ms"], 424242);
                    assert_eq!(marker.unwrap(), b"verified-prefix-1");
                }
            }
        }
    }

    #[test]
    fn offline_frames_reject_truncation_and_bound_allocation() {
        let mut frame = Vec::new();
        assert!(!read_frame(&mut &b""[..], &mut frame).unwrap());
        assert!(read_frame(&mut &b"{}"[..], &mut frame).is_err());
        let oversize = vec![b'x'; MIGRATION_SESSION_MAX_FRAME_BYTES + 1];
        assert!(read_frame(
            &mut io::BufReader::with_capacity(4096, oversize.as_slice()),
            &mut frame
        )
        .is_err());
        assert!(frame.capacity() <= MIGRATION_SESSION_MAX_FRAME_BYTES);
        assert!(read_frame(&mut &b"{}\n"[..], &mut frame).unwrap());
        assert_eq!(frame, b"{}\n");
    }
}
