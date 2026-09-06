//! Tenant-global durable worker-job queue, leases, cancellation, and fencing.
//!
//! Jobs are globally claimable inside one authenticated numeric tenant while
//! retaining their explicit user owner. Queue selection is bounded by a
//! fixed 1001-priority summary per task kind; it never sorts or scans an
//! unbounded eligible set.

use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_WORKER_JOB_CLOCK_MILLISECONDS, MAX_WORKER_JOB_ERROR_BYTES, MAX_WORKER_JOB_PAYLOAD_BYTES,
    MAX_WORKER_JOB_PRIORITY, WORKER_JOB_DOCUMENT_NAMESPACE, WORKER_JOB_IDEMPOTENCY_NAMESPACE,
    WORKER_JOB_LEASE_INDEX_NAMESPACE, WORKER_JOB_QUEUED_INDEX_NAMESPACE,
    WORKER_JOB_QUEUED_SUMMARY_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "storage_worker_jobs";
const GLOBAL_BLOB_OWNER_USER_ID: u64 = TENANT_GLOBAL_OWNER_ID;
const SUMMARY_MAGIC: &[u8; 8] = b"WJQSUM01";
const PRIORITY_COUNT: usize = MAX_WORKER_JOB_PRIORITY + 1;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}

fn text_within(value: &str, maximum: usize, allow_empty: bool) -> bool {
    (allow_empty || !value.is_empty()) && value.chars().count() <= maximum
}

fn encoded_json_bytes(value: &Value) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_input("worker job JSON cannot be encoded"))
}

fn push_text(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    output.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("worker job index text is too long"))?
            .to_be_bytes(),
    );
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn job_key(transaction: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_DOCUMENT_NAMESPACE,
        task_id.as_bytes(),
    )
}

fn idempotency_key(
    transaction: &AuthorityTransaction,
    user_id: u64,
    key: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(10 + key.len());
    raw.extend_from_slice(&user_id.to_be_bytes());
    push_text(&mut raw, key)?;
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_IDEMPOTENCY_NAMESPACE,
        &raw,
    )
}

fn task_kind_prefix(task_kind: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(2 + task_kind.len());
    push_text(&mut raw, task_kind)?;
    Ok(raw)
}

fn queued_priority_prefix(task_kind: &str, priority: u16) -> io::Result<Vec<u8>> {
    let mut raw = task_kind_prefix(task_kind)?;
    raw.extend_from_slice(&priority.to_be_bytes());
    Ok(raw)
}

fn queued_key(transaction: &AuthorityTransaction, job: &Job) -> io::Result<EntityKey> {
    let mut raw = queued_priority_prefix(&job.task_kind, job.priority)?;
    raw.extend_from_slice(&job.available_at_ms.to_be_bytes());
    raw.extend_from_slice(&job.created_at_ms.to_be_bytes());
    raw.extend_from_slice(job.task_id.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_QUEUED_INDEX_NAMESPACE,
        &raw,
    )
}

fn lease_key(transaction: &AuthorityTransaction, job: &Job) -> io::Result<EntityKey> {
    let mut raw = task_kind_prefix(&job.task_kind)?;
    raw.extend_from_slice(&job.lease_deadline_ms.to_be_bytes());
    raw.extend_from_slice(&job.priority.to_be_bytes());
    raw.extend_from_slice(&job.created_at_ms.to_be_bytes());
    raw.extend_from_slice(job.task_id.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_LEASE_INDEX_NAMESPACE,
        &raw,
    )
}

fn summary_key(transaction: &AuthorityTransaction, task_kind: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_QUEUED_SUMMARY_NAMESPACE,
        task_kind.as_bytes(),
    )
}

fn namespace_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        namespace,
        prefix,
    )
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Job {
    task_id: String,
    user_id: u64,
    tenant_id: String,
    task_kind: String,
    payload: Value,
    idempotency_key: String,
    request_digest: String,
    status: String,
    priority: u16,
    available_at_ms: u64,
    claim_owner: String,
    lease_deadline_ms: u64,
    fencing_token: u64,
    attempt_no: u64,
    heartbeat_at_ms: u64,
    cancel_sequence: u64,
    cancel_requested_at_ms: u64,
    cancel_reason: String,
    replay_cursor: u64,
    result_ref: String,
    error: Value,
    created_at_ms: u64,
    updated_at_ms: u64,
    terminal_at_ms: u64,
}

impl Job {
    fn validate(&self) -> io::Result<()> {
        let timestamp_valid = |value: u64| value <= MAX_WORKER_JOB_CLOCK_MILLISECONDS;
        if !text_within(&self.task_id, 256, false)
            || self.user_id == 0
            || !text_within(&self.tenant_id, 256, true)
            || !text_within(&self.task_kind, 128, false)
            || !self.payload.is_object()
            || encoded_json_bytes(&self.payload)?.len() > MAX_WORKER_JOB_PAYLOAD_BYTES
            || !text_within(&self.idempotency_key, 256, false)
            || self.request_digest.len() != 64
            || !self
                .request_digest
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
            || !matches!(
                self.status.as_str(),
                "queued" | "running" | "succeeded" | "failed" | "cancelled"
            )
            || usize::from(self.priority) > MAX_WORKER_JOB_PRIORITY
            || !text_within(&self.claim_owner, 256, true)
            || !text_within(&self.cancel_reason, 1000, true)
            || !text_within(&self.result_ref, 1024, true)
            || !self.error.is_object()
            || encoded_json_bytes(&self.error)?.len() > MAX_WORKER_JOB_ERROR_BYTES
            || ![
                self.available_at_ms,
                self.lease_deadline_ms,
                self.heartbeat_at_ms,
                self.cancel_requested_at_ms,
                self.created_at_ms,
                self.updated_at_ms,
                self.terminal_at_ms,
            ]
            .into_iter()
            .all(timestamp_valid)
        {
            return Err(invalid_data("worker job document is malformed"));
        }
        Ok(())
    }

    fn response(&self) -> Value {
        json!({
            "taskId": self.task_id,
            "userId": self.user_id,
            "tenantId": self.tenant_id,
            "taskKind": self.task_kind,
            "payload": self.payload,
            "idempotencyKey": self.idempotency_key,
            "status": self.status,
            "priority": self.priority,
            "availableAtMs": self.available_at_ms,
            "claimOwner": self.claim_owner,
            "leaseDeadlineMs": self.lease_deadline_ms,
            "fencingToken": self.fencing_token,
            "attempt": self.attempt_no,
            "heartbeatAtMs": self.heartbeat_at_ms,
            "cancelSequence": self.cancel_sequence,
            "cancelRequestedAtMs": self.cancel_requested_at_ms,
            "cancelReason": self.cancel_reason,
            "replayCursor": self.replay_cursor,
            "resultRef": self.result_ref,
            "error": self.error,
            "createdAtMs": self.created_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "terminalAtMs": self.terminal_at_ms,
        })
    }

    fn index_entry(&self) -> IndexEntry {
        IndexEntry {
            task_id: self.task_id.clone(),
            task_kind: self.task_kind.clone(),
            priority: self.priority,
            available_at_ms: self.available_at_ms,
            lease_deadline_ms: self.lease_deadline_ms,
            created_at_ms: self.created_at_ms,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct IdempotencyEntry {
    user_id: u64,
    idempotency_key: String,
    task_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct IndexEntry {
    task_id: String,
    task_kind: String,
    priority: u16,
    available_at_ms: u64,
    lease_deadline_ms: u64,
    created_at_ms: u64,
}

fn encode<T: Serialize>(value: &T, message: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data(message))
}

fn read_job(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<Option<Job>> {
    let Some(raw) = versioned_document::get_with_blob_owner(
        database,
        transaction,
        &job_key(transaction, task_id)?,
        LOGICAL_NAMESPACE,
        task_id,
        GLOBAL_BLOB_OWNER_USER_ID,
    )?
    else {
        return Ok(None);
    };
    let job: Job = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|envelope| envelope.get("value").cloned())
        .and_then(|value| serde_json::from_value(value).ok())
        .ok_or_else(|| invalid_data("worker job document envelope is malformed"))?;
    job.validate()?;
    if job.task_id != task_id {
        return Err(invalid_data("worker job key identity differs"));
    }
    Ok(Some(job))
}

fn put_job(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    job: &Job,
) -> io::Result<()> {
    job.validate()?;
    versioned_document::put_with_blob_owner(
        database,
        transaction,
        PutRequest {
            key: job_key(transaction, &job.task_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: job.task_id.clone(),
            value_json: encode(job, "worker job document cannot be encoded")?,
            expected_version: None,
            updated_at_ms: job.updated_at_ms.max(1),
        },
        GLOBAL_BLOB_OWNER_USER_ID,
    )?;
    Ok(())
}

fn read_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_kind: &str,
) -> io::Result<Vec<u64>> {
    let Some(raw) = database.entity_get(transaction, &summary_key(transaction, task_kind)?)? else {
        return Ok(vec![u64::MAX; PRIORITY_COUNT]);
    };
    if raw.len() != SUMMARY_MAGIC.len() + PRIORITY_COUNT * 8
        || &raw[..SUMMARY_MAGIC.len()] != SUMMARY_MAGIC
    {
        return Err(invalid_data("worker job queue summary is malformed"));
    }
    Ok(raw[SUMMARY_MAGIC.len()..]
        .chunks_exact(8)
        .map(|chunk| u64::from_be_bytes(chunk.try_into().unwrap()))
        .collect())
}

fn write_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_kind: &str,
    summary: &[u64],
) -> io::Result<()> {
    if summary.len() != PRIORITY_COUNT {
        return Err(invalid_data("worker job queue summary length differs"));
    }
    let key = summary_key(transaction, task_kind)?;
    if summary.iter().all(|value| *value == u64::MAX) {
        return database.entity_delete(transaction, key);
    }
    let mut raw = Vec::with_capacity(SUMMARY_MAGIC.len() + PRIORITY_COUNT * 8);
    raw.extend_from_slice(SUMMARY_MAGIC);
    for value in summary {
        raw.extend_from_slice(&value.to_be_bytes());
    }
    database.entity_put(transaction, key, raw)
}

fn put_queued_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    job: &Job,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        queued_key(transaction, job)?,
        encode(
            &job.index_entry(),
            "worker job queued index cannot be encoded",
        )?,
    )?;
    let mut summary = read_summary(database, transaction, &job.task_kind)?;
    let priority = usize::from(job.priority);
    summary[priority] = summary[priority].min(job.available_at_ms);
    write_summary(database, transaction, &job.task_kind, &summary)
}

fn first_queued_at_priority(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_kind: &str,
    priority: u16,
) -> io::Result<Option<(EntityKey, IndexEntry)>> {
    let prefix = queued_priority_prefix(task_kind, priority)?;
    let (start, end) = namespace_range(transaction, WORKER_JOB_QUEUED_INDEX_NAMESPACE, &prefix)?;
    let Some((key, raw)) = database
        .entity_scan(transaction, &start, &end, 1)?
        .into_iter()
        .next()
    else {
        return Ok(None);
    };
    let entry: IndexEntry = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("worker job queued index is malformed"))?;
    if entry.task_kind != task_kind
        || entry.priority != priority
        || queued_key_from_entry(transaction, &entry)? != key
    {
        return Err(invalid_data("worker job queued index identity differs"));
    }
    Ok(Some((key, entry)))
}

fn queued_key_from_entry(
    transaction: &AuthorityTransaction,
    entry: &IndexEntry,
) -> io::Result<EntityKey> {
    let mut raw = queued_priority_prefix(&entry.task_kind, entry.priority)?;
    raw.extend_from_slice(&entry.available_at_ms.to_be_bytes());
    raw.extend_from_slice(&entry.created_at_ms.to_be_bytes());
    raw.extend_from_slice(entry.task_id.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_QUEUED_INDEX_NAMESPACE,
        &raw,
    )
}

fn remove_queued_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    job: &Job,
) -> io::Result<()> {
    database.entity_delete(transaction, queued_key(transaction, job)?)?;
    let mut summary = read_summary(database, transaction, &job.task_kind)?;
    let priority = usize::from(job.priority);
    summary[priority] =
        first_queued_at_priority(database, transaction, &job.task_kind, job.priority)?
            .map_or(u64::MAX, |(_, entry)| entry.available_at_ms);
    write_summary(database, transaction, &job.task_kind, &summary)
}

fn put_lease_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    job: &Job,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        lease_key(transaction, job)?,
        encode(
            &job.index_entry(),
            "worker job lease index cannot be encoded",
        )?,
    )
}

fn first_lease(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_kind: &str,
) -> io::Result<Option<(EntityKey, IndexEntry)>> {
    let prefix = task_kind_prefix(task_kind)?;
    let (start, end) = namespace_range(transaction, WORKER_JOB_LEASE_INDEX_NAMESPACE, &prefix)?;
    let Some((key, raw)) = database
        .entity_scan(transaction, &start, &end, 1)?
        .into_iter()
        .next()
    else {
        return Ok(None);
    };
    let entry: IndexEntry = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("worker job lease index is malformed"))?;
    let probe = Job {
        task_id: entry.task_id.clone(),
        user_id: 1,
        tenant_id: String::new(),
        task_kind: entry.task_kind.clone(),
        payload: json!({}),
        idempotency_key: "probe".to_owned(),
        request_digest: "0".repeat(64),
        status: "running".to_owned(),
        priority: entry.priority,
        available_at_ms: entry.available_at_ms,
        claim_owner: "probe".to_owned(),
        lease_deadline_ms: entry.lease_deadline_ms,
        fencing_token: 1,
        attempt_no: 1,
        heartbeat_at_ms: 0,
        cancel_sequence: 0,
        cancel_requested_at_ms: 0,
        cancel_reason: String::new(),
        replay_cursor: 0,
        result_ref: String::new(),
        error: json!({}),
        created_at_ms: entry.created_at_ms,
        updated_at_ms: 0,
        terminal_at_ms: 0,
    };
    if entry.task_kind != task_kind || lease_key(transaction, &probe)? != key {
        return Err(invalid_data("worker job lease index identity differs"));
    }
    Ok(Some((key, entry)))
}

pub(crate) struct EnqueueRequest {
    pub task_id: String,
    pub user_id: u64,
    pub tenant_id: String,
    pub task_kind: String,
    pub payload: Value,
    pub idempotency_key: String,
    pub request_digest: String,
    pub priority: u16,
    pub available_at_ms: u64,
    pub now_ms: u64,
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    user_id: u64,
) -> io::Result<Option<Vec<u8>>> {
    read_job(database, transaction, task_id)?
        .filter(|job| job.user_id == user_id)
        .map(|job| encoded_json_bytes(&job.response()))
        .transpose()
}

pub(crate) fn enqueue(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: EnqueueRequest,
) -> io::Result<Vec<u8>> {
    let idem_key = idempotency_key(transaction, request.user_id, &request.idempotency_key)?;
    if let Some(raw) = database.entity_get(transaction, &idem_key)? {
        let entry: IdempotencyEntry = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("worker job idempotency entry is malformed"))?;
        if entry.user_id != request.user_id
            || entry.idempotency_key != request.idempotency_key
            || !text_within(&entry.task_id, 256, false)
        {
            return Err(invalid_data("worker job idempotency identity differs"));
        }
        let job = read_job(database, transaction, &entry.task_id)?
            .ok_or_else(|| invalid_data("worker job idempotency target is missing"))?;
        if job.user_id != request.user_id || job.idempotency_key != request.idempotency_key {
            return Err(invalid_data("worker job idempotency target differs"));
        }
        if job.request_digest != request.request_digest {
            return Err(conflict(
                "worker job idempotency key was reused for another request",
            ));
        }
        return encoded_json_bytes(&json!({"created": false, "job": job.response()}));
    }
    if read_job(database, transaction, &request.task_id)?.is_some() {
        return Err(conflict("worker job task ID already exists"));
    }
    let job = Job {
        task_id: request.task_id,
        user_id: request.user_id,
        tenant_id: request.tenant_id,
        task_kind: request.task_kind,
        payload: request.payload,
        idempotency_key: request.idempotency_key,
        request_digest: request.request_digest,
        status: "queued".to_owned(),
        priority: request.priority,
        available_at_ms: request.available_at_ms,
        claim_owner: String::new(),
        lease_deadline_ms: 0,
        fencing_token: 0,
        attempt_no: 0,
        heartbeat_at_ms: 0,
        cancel_sequence: 0,
        cancel_requested_at_ms: 0,
        cancel_reason: String::new(),
        replay_cursor: 0,
        result_ref: String::new(),
        error: json!({}),
        created_at_ms: request.now_ms,
        updated_at_ms: request.now_ms,
        terminal_at_ms: 0,
    };
    put_job(database, transaction, &job)?;
    database.entity_put(
        transaction,
        idem_key,
        encode(
            &IdempotencyEntry {
                user_id: job.user_id,
                idempotency_key: job.idempotency_key.clone(),
                task_id: job.task_id.clone(),
            },
            "worker job idempotency entry cannot be encoded",
        )?,
    )?;
    put_queued_index(database, transaction, &job)?;
    encoded_json_bytes(&json!({"created": true, "job": job.response()}))
}

pub(crate) fn claim_next(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    worker_id: &str,
    now_ms: u64,
    lease_ms: u64,
    task_kinds: &[String],
) -> io::Result<Option<Vec<u8>>> {
    let mut expired: Option<IndexEntry> = None;
    for task_kind in task_kinds {
        let Some((_, entry)) = first_lease(database, transaction, task_kind)? else {
            continue;
        };
        if entry.lease_deadline_ms > now_ms {
            continue;
        }
        let order = (
            entry.lease_deadline_ms,
            entry.priority,
            entry.created_at_ms,
            entry.task_id.as_str(),
        );
        if expired.as_ref().is_none_or(|current| {
            order
                < (
                    current.lease_deadline_ms,
                    current.priority,
                    current.created_at_ms,
                    current.task_id.as_str(),
                )
        }) {
            expired = Some(entry);
        }
    }

    let mut queued: Option<IndexEntry> = None;
    if expired.is_none() {
        for task_kind in task_kinds {
            let summary = read_summary(database, transaction, task_kind)?;
            let Some((priority, _)) = summary
                .iter()
                .enumerate()
                .find(|(_, available_at_ms)| **available_at_ms <= now_ms)
            else {
                continue;
            };
            let Some((_, entry)) =
                first_queued_at_priority(database, transaction, task_kind, priority as u16)?
            else {
                return Err(invalid_data("worker job queue summary target is missing"));
            };
            if entry.available_at_ms != summary[priority] || entry.available_at_ms > now_ms {
                return Err(invalid_data(
                    "worker job queue summary differs from its index",
                ));
            }
            let order = (
                entry.priority,
                entry.available_at_ms,
                entry.created_at_ms,
                entry.task_id.as_str(),
            );
            if queued.as_ref().is_none_or(|current| {
                order
                    < (
                        current.priority,
                        current.available_at_ms,
                        current.created_at_ms,
                        current.task_id.as_str(),
                    )
            }) {
                queued = Some(entry);
            }
        }
    }

    let Some(selected) = expired.as_ref().or(queued.as_ref()) else {
        return Ok(None);
    };
    let mut job = read_job(database, transaction, &selected.task_id)?
        .ok_or_else(|| invalid_data("worker job claim index target is missing"))?;
    if job.task_kind != selected.task_kind
        || job.priority != selected.priority
        || job.created_at_ms != selected.created_at_ms
    {
        return Err(invalid_data("worker job claim index target differs"));
    }
    if expired.is_some() {
        if job.status != "running" || job.lease_deadline_ms != selected.lease_deadline_ms {
            return Err(invalid_data("expired worker job lease target differs"));
        }
        database.entity_delete(transaction, lease_key(transaction, &job)?)?;
    } else {
        if job.status != "queued" || job.available_at_ms != selected.available_at_ms {
            return Err(invalid_data("queued worker job target differs"));
        }
        remove_queued_index(database, transaction, &job)?;
    }
    job.status = "running".to_owned();
    job.claim_owner = worker_id.to_owned();
    job.lease_deadline_ms = now_ms
        .checked_add(lease_ms)
        .ok_or_else(|| invalid_input("worker job lease deadline overflows"))?;
    job.fencing_token = job
        .fencing_token
        .checked_add(1)
        .ok_or_else(|| invalid_data("worker job fencing token overflows"))?;
    job.attempt_no = job
        .attempt_no
        .checked_add(1)
        .ok_or_else(|| invalid_data("worker job attempt number overflows"))?;
    job.heartbeat_at_ms = now_ms;
    job.updated_at_ms = now_ms;
    put_job(database, transaction, &job)?;
    put_lease_index(database, transaction, &job)?;
    encoded_json_bytes(&job.response()).map(Some)
}

fn live_claim_matches(job: &Job, worker_id: &str, fence: u64, now_ms: u64) -> bool {
    job.status == "running"
        && job.claim_owner == worker_id
        && job.fencing_token == fence
        && job.lease_deadline_ms > now_ms
}

pub(crate) struct HeartbeatRequest<'a> {
    pub task_id: &'a str,
    pub worker_id: &'a str,
    pub fence: u64,
    pub now_ms: u64,
    pub lease_ms: u64,
    pub replay_cursor: u64,
}

pub(crate) fn heartbeat(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: HeartbeatRequest<'_>,
) -> io::Result<Vec<u8>> {
    let Some(mut job) = read_job(database, transaction, request.task_id)? else {
        return Ok(br#"{"ok":false,"error":"stale_fence"}"#.to_vec());
    };
    if !live_claim_matches(&job, request.worker_id, request.fence, request.now_ms) {
        return Ok(br#"{"ok":false,"error":"stale_fence"}"#.to_vec());
    }
    database.entity_delete(transaction, lease_key(transaction, &job)?)?;
    let deadline = request
        .now_ms
        .checked_add(request.lease_ms)
        .ok_or_else(|| invalid_input("worker job lease deadline overflows"))?;
    job.lease_deadline_ms = job.lease_deadline_ms.max(deadline);
    job.heartbeat_at_ms = job.heartbeat_at_ms.max(request.now_ms);
    job.replay_cursor = job.replay_cursor.max(request.replay_cursor);
    job.updated_at_ms = request.now_ms;
    put_job(database, transaction, &job)?;
    put_lease_index(database, transaction, &job)?;
    encoded_json_bytes(&json!({"ok": true, "job": job.response()}))
}

pub(crate) fn claim_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    worker_id: &str,
    fence: u64,
    now_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some(job) = read_job(database, transaction, task_id)? else {
        return Ok(br#"{"ok":false,"error":"stale_fence"}"#.to_vec());
    };
    if !live_claim_matches(&job, worker_id, fence, now_ms) {
        return Ok(br#"{"ok":false,"error":"stale_fence"}"#.to_vec());
    }
    encoded_json_bytes(&json!({
        "ok": true,
        "cancelSequence": job.cancel_sequence,
        "cancelRequestedAtMs": job.cancel_requested_at_ms,
        "cancelReason": job.cancel_reason,
        "leaseDeadlineMs": job.lease_deadline_ms,
        "replayCursor": job.replay_cursor,
    }))
}

pub(crate) fn request_cancel(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    user_id: u64,
    now_ms: u64,
    reason: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(mut job) = read_job(database, transaction, task_id)? else {
        return Ok(None);
    };
    if job.user_id != user_id {
        return Ok(None);
    }
    if matches!(job.status.as_str(), "succeeded" | "failed" | "cancelled") {
        return encoded_json_bytes(&json!({
            "accepted": false,
            "alreadyTerminal": true,
            "job": job.response(),
        }))
        .map(Some);
    }
    if job.cancel_requested_at_ms > 0 {
        return encoded_json_bytes(&json!({
            "accepted": true,
            "alreadyRequested": true,
            "job": job.response(),
        }))
        .map(Some);
    }
    if job.status == "queued" {
        remove_queued_index(database, transaction, &job)?;
        job.status = "cancelled".to_owned();
        job.terminal_at_ms = now_ms;
    }
    job.cancel_sequence = job
        .cancel_sequence
        .checked_add(1)
        .ok_or_else(|| invalid_data("worker job cancel sequence overflows"))?;
    job.cancel_requested_at_ms = now_ms;
    job.cancel_reason = reason.to_owned();
    job.updated_at_ms = now_ms;
    put_job(database, transaction, &job)?;
    encoded_json_bytes(&json!({
        "accepted": true,
        "alreadyRequested": false,
        "job": job.response(),
    }))
    .map(Some)
}

pub(crate) struct CompleteRequest<'a> {
    pub task_id: &'a str,
    pub worker_id: &'a str,
    pub fence: u64,
    pub now_ms: u64,
    pub terminal_status: &'a str,
    pub result_ref: &'a str,
    pub replay_cursor: u64,
    pub error: Value,
}

pub(crate) fn complete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: CompleteRequest<'_>,
) -> io::Result<Vec<u8>> {
    let Some(mut job) = read_job(database, transaction, request.task_id)? else {
        return Ok(br#"{"ok":false,"error":"stale_fence_or_cancelled"}"#.to_vec());
    };
    if !live_claim_matches(&job, request.worker_id, request.fence, request.now_ms)
        || (job.cancel_requested_at_ms > 0 && request.terminal_status != "cancelled")
    {
        return Ok(br#"{"ok":false,"error":"stale_fence_or_cancelled"}"#.to_vec());
    }
    database.entity_delete(transaction, lease_key(transaction, &job)?)?;
    job.status = request.terminal_status.to_owned();
    job.result_ref = request.result_ref.to_owned();
    job.error = request.error;
    job.replay_cursor = job.replay_cursor.max(request.replay_cursor);
    job.lease_deadline_ms = 0;
    job.updated_at_ms = request.now_ms;
    job.terminal_at_ms = request.now_ms;
    put_job(database, transaction, &job)?;
    encoded_json_bytes(&json!({"ok": true, "job": job.response()}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    #[test]
    fn claim_read_work_is_constant_after_five_hundred_twelve_queued_jobs() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 41).unwrap();
        for index in 0..512_u64 {
            enqueue(
                &database,
                &mut seed,
                EnqueueRequest {
                    task_id: format!("bounded-job-{index:04}"),
                    user_id: 41,
                    tenant_id: "tenant-a".to_owned(),
                    task_kind: "bounded-kind".to_owned(),
                    payload: json!({"index": index}),
                    idempotency_key: format!("bounded-key-{index:04}"),
                    request_digest: format!("{index:064x}"),
                    priority: 100,
                    available_at_ms: 1_000 + index,
                    now_ms: 1_000,
                },
            )
            .unwrap();
        }
        database.commit(seed).unwrap();

        vfs.arm_fault(None).unwrap();
        let mut claim = database.begin_with_identity_claim_scopes(7, 90).unwrap();
        let selected = claim_next(
            &database,
            &mut claim,
            "bounded-worker",
            2_000,
            60_000,
            &["bounded-kind".to_owned()],
        )
        .unwrap()
        .unwrap();
        let selected: Value = serde_json::from_slice(&selected).unwrap();
        assert_eq!(selected["taskId"], "bounded-job-0000");
        let reads = vfs
            .trace()
            .unwrap()
            .into_iter()
            .filter(|operation| operation == &Operation::Read)
            .count();
        assert!(reads <= 32, "claim performed {reads} physical reads");
        database.commit(claim).unwrap();
    }
}
