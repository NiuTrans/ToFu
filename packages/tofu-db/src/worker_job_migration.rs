//! Offline worker-job snapshot restoration for the exclusive operator
//! importer.
//!
//! The source keeps one row per job in `storage_worker_jobs` with a
//! (user_id, idempotency_key) uniqueness rule. The native tenant-global
//! authority stores a blob-capable versioned job envelope, an idempotency
//! claim, a per-kind queued index feeding a fixed 1001-priority summary, and
//! a per-kind lease index. Live enqueue stamps status/timestamps from the
//! server clock and always queues, so replaying through it cannot preserve
//! the exact source status, fencing, attempt, lease, result, or timestamps
//! and is not insert-or-match idempotent. This adapter restores the envelope
//! with the exact source fields, claims the idempotency key, and rebuilds
//! the queued/lease indexes and the priority summary in the same authority
//! transaction: queued rows recompute the summary from the full restored
//! index content; running rows join the lease index; terminal rows carry no
//! index. Insert-or-match fails closed on a divergent occupant, get
//! reconstructs the exact source snapshot, and scan enumerates the
//! owner-scoped idempotency catalog independently. Entry points: the
//! `migration.worker.job.*` operations dispatched from `migration_session`.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_WORKER_JOB_CLOCK_MILLISECONDS, MAX_WORKER_JOB_ERROR_BYTES, MAX_WORKER_JOB_PAYLOAD_BYTES,
    MAX_WORKER_JOB_PRIORITY, MIGRATION_SESSION_MAX_PAGE_ROWS, WORKER_JOB_DOCUMENT_NAMESPACE,
    WORKER_JOB_IDEMPOTENCY_NAMESPACE, WORKER_JOB_LEASE_INDEX_NAMESPACE,
    WORKER_JOB_QUEUED_INDEX_NAMESPACE, WORKER_JOB_QUEUED_SUMMARY_NAMESPACE,
};
use crate::versioned_document::{self, SnapshotDocument, SnapshotRestoreOutcome};

const LOGICAL_NAMESPACE: &str = "storage_worker_jobs";
const SUMMARY_MAGIC: &[u8; 8] = b"WJQSUM01";
const PRIORITY_COUNT: usize = MAX_WORKER_JOB_PRIORITY + 1;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "worker job snapshot differs")
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

fn job_key(tx: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_DOCUMENT_NAMESPACE,
        task_id.as_bytes(),
    )
}
fn idempotency_key(tx: &AuthorityTransaction, user_id: u64, key: &str) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(10 + key.len());
    raw.extend_from_slice(&user_id.to_be_bytes());
    push_text(&mut raw, key)?;
    EntityKey::new(
        tx.tenant_id(),
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
fn queued_key(tx: &AuthorityTransaction, job: &Job) -> io::Result<EntityKey> {
    let mut raw = queued_priority_prefix(&job.task_kind, job.priority)?;
    raw.extend_from_slice(&job.available_at_ms.to_be_bytes());
    raw.extend_from_slice(&job.created_at_ms.to_be_bytes());
    raw.extend_from_slice(job.task_id.as_bytes());
    EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_QUEUED_INDEX_NAMESPACE,
        &raw,
    )
}
fn lease_key(tx: &AuthorityTransaction, job: &Job) -> io::Result<EntityKey> {
    let raw = crate::worker_job::lease_index_raw(
        &job.task_kind,
        &job.lease_authority_epoch,
        job.lease_deadline_ms,
        job.priority,
        job.created_at_ms,
        &job.task_id,
    )?;
    EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_LEASE_INDEX_NAMESPACE,
        &raw,
    )
}
fn summary_key(tx: &AuthorityTransaction, task_kind: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_QUEUED_SUMMARY_NAMESPACE,
        task_kind.as_bytes(),
    )
}

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
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
    #[serde(default, skip_serializing_if = "String::is_empty")]
    lease_authority_epoch: String,
    fencing_token: u64,
    attempt_no: u64,
    heartbeat_at_ms: u64,
    cancel_sequence: u64,
    #[serde(default)]
    wake_sequence: u64,
    cancel_requested_at_ms: u64,
    cancel_reason: String,
    replay_cursor: u64,
    result_ref: String,
    error: Value,
    created_at_ms: u64,
    updated_at_ms: u64,
    terminal_at_ms: u64,
}
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
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
    #[serde(default, skip_serializing_if = "String::is_empty")]
    lease_authority_epoch: String,
    created_at_ms: u64,
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
            || !crate::worker_job::valid_lease_epoch(&self.lease_authority_epoch)
            || self.request_digest.len() != 64
            || !self
                .request_digest
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
            || !crate::generated_tofudb_ir::WORKER_JOB_STATES.contains(&self.status.as_str())
            || usize::from(self.priority) > MAX_WORKER_JOB_PRIORITY
            || !text_within(&self.claim_owner, 256, true)
            || !text_within(&self.cancel_reason, 1000, true)
            || !text_within(&self.result_ref, 1024, true)
            || self.wake_sequence > MAX_WORKER_JOB_CLOCK_MILLISECONDS
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
    fn index_entry(&self) -> IndexEntry {
        IndexEntry {
            task_id: self.task_id.clone(),
            task_kind: self.task_kind.clone(),
            priority: self.priority,
            available_at_ms: self.available_at_ms,
            lease_deadline_ms: self.lease_deadline_ms,
            lease_authority_epoch: self.lease_authority_epoch.clone(),
            created_at_ms: self.created_at_ms,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    task_id: String,
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
    #[serde(default, skip_serializing_if = "String::is_empty")]
    lease_authority_epoch: String,
    fencing_token: u64,
    attempt_no: u64,
    heartbeat_at_ms: u64,
    cancel_sequence: u64,
    #[serde(default)]
    wake_sequence: u64,
    cancel_requested_at_ms: u64,
    cancel_reason: String,
    replay_cursor: u64,
    result_ref: String,
    error: Value,
    created_at_ms: u64,
    updated_at_ms: u64,
    terminal_at_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    task_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn job_of(request: &RestoreRequest, owner: u64) -> Job {
    Job {
        task_id: request.task_id.clone(),
        user_id: owner,
        tenant_id: request.tenant_id.clone(),
        task_kind: request.task_kind.clone(),
        payload: request.payload.clone(),
        idempotency_key: request.idempotency_key.clone(),
        request_digest: request.request_digest.clone(),
        status: request.status.clone(),
        priority: request.priority,
        available_at_ms: request.available_at_ms,
        claim_owner: request.claim_owner.clone(),
        lease_deadline_ms: request.lease_deadline_ms,
        lease_authority_epoch: request.lease_authority_epoch.clone(),
        fencing_token: request.fencing_token,
        attempt_no: request.attempt_no,
        heartbeat_at_ms: request.heartbeat_at_ms,
        cancel_sequence: request.cancel_sequence,
        wake_sequence: request.wake_sequence,
        cancel_requested_at_ms: request.cancel_requested_at_ms,
        cancel_reason: request.cancel_reason.clone(),
        replay_cursor: request.replay_cursor,
        result_ref: request.result_ref.clone(),
        error: request.error.clone(),
        created_at_ms: request.created_at_ms,
        updated_at_ms: request.updated_at_ms,
        terminal_at_ms: request.terminal_at_ms,
    }
}

fn read_stored_job(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<Option<Job>> {
    let Some(raw) = versioned_document::get_with_blob_owner(
        database,
        tx,
        &job_key(tx, task_id)?,
        LOGICAL_NAMESPACE,
        task_id,
        TENANT_GLOBAL_OWNER_ID,
    )?
    else {
        return Ok(None);
    };
    let job: Job = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|envelope| envelope.get("value").cloned())
        .and_then(|value| serde_json::from_value(value).ok())
        .ok_or_else(|| invalid_data("worker job document envelope is malformed"))?;
    Ok(Some(job))
}

fn read_summary(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    task_kind: &str,
) -> io::Result<Vec<u64>> {
    let Some(raw) = database.entity_get(tx, &summary_key(tx, task_kind)?)? else {
        return Ok(vec![u64::MAX; PRIORITY_COUNT]);
    };
    if raw.len() != SUMMARY_MAGIC.len() + PRIORITY_COUNT * 8
        || &raw[..SUMMARY_MAGIC.len()] != SUMMARY_MAGIC
    {
        return Err(invalid_data("worker job queue summary is malformed"));
    }
    Ok(raw[SUMMARY_MAGIC.len()..]
        .as_chunks::<8>()
        .0
        .iter()
        .map(|chunk| u64::from_be_bytes(*chunk))
        .collect())
}
fn write_summary(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    task_kind: &str,
    summary: &[u64],
) -> io::Result<()> {
    if summary.len() != PRIORITY_COUNT {
        return Err(invalid_data("worker job queue summary length differs"));
    }
    let key = summary_key(tx, task_kind)?;
    if summary.iter().all(|value| *value == u64::MAX) {
        return database.entity_delete(tx, key);
    }
    let mut raw = Vec::with_capacity(SUMMARY_MAGIC.len() + PRIORITY_COUNT * 8);
    raw.extend_from_slice(SUMMARY_MAGIC);
    for value in summary {
        raw.extend_from_slice(&value.to_be_bytes());
    }
    database.entity_put(tx, key, raw)
}

fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
    expected_uuid: uuid::Uuid,
) -> io::Result<bool> {
    let request: RestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid worker job restore request"))?;
    let owner = tx.owner_user_id();
    let job = job_of(&request, owner);
    job.validate()
        .map_err(|_| invalid_input("invalid worker job snapshot metadata"))?;
    let expected_claim = IdempotencyEntry {
        user_id: owner,
        idempotency_key: job.idempotency_key.clone(),
        task_id: job.task_id.clone(),
    };
    let claim_key = idempotency_key(tx, owner, &job.idempotency_key)?;
    match database.entity_get(tx, &claim_key)? {
        Some(raw) => {
            let existing: IdempotencyEntry = serde_json::from_slice(&raw)
                .map_err(|_| invalid_data("worker job idempotency entry is malformed"))?;
            // Insert-or-match against the exact stored claim and document; a
            // divergent occupant fails closed instead of overwriting.
            if existing != expected_claim
                || read_stored_job(database, tx, &job.task_id)?.as_ref() != Some(&job)
            {
                return Err(snapshot_conflict());
            }
            return Ok(false);
        }
        None => {
            if read_stored_job(database, tx, &job.task_id)?.is_some() {
                return Err(invalid_data("worker job document has no idempotency claim"));
            }
        }
    }
    match crate::migration::restore_document(
        database,
        tx,
        expected_uuid,
        SnapshotDocument {
            key: job_key(tx, &job.task_id)?,
            namespace: LOGICAL_NAMESPACE.into(),
            logical_key: job.task_id.clone(),
            version: 1,
            updated_at_ms: job.updated_at_ms.max(1),
            value_json: serde_json::to_vec(&job)
                .map_err(|_| invalid_data("worker job document cannot be encoded"))?,
            blob_owner_user_id: TENANT_GLOBAL_OWNER_ID,
        },
    )? {
        SnapshotRestoreOutcome::Inserted => {}
        SnapshotRestoreOutcome::AlreadyMatches => {
            return Err(invalid_data("worker job document exists without its claim"));
        }
    }
    database.entity_put(
        tx,
        claim_key,
        serde_json::to_vec(&expected_claim)
            .map_err(|_| invalid_data("worker job idempotency entry cannot be encoded"))?,
    )?;
    // Rebuild the derived indexes in the same transaction: queued rows join
    // the queued index and refresh the priority summary; running rows join
    // the lease index; terminal rows carry no index.
    match job.status.as_str() {
        "queued" | "suspended" => {
            database.entity_put(
                tx,
                queued_key(tx, &job)?,
                serde_json::to_vec(&job.index_entry())
                    .map_err(|_| invalid_data("worker job queued index cannot be encoded"))?,
            )?;
            let mut summary = read_summary(database, tx, &job.task_kind)?;
            summary[usize::from(job.priority)] =
                summary[usize::from(job.priority)].min(job.available_at_ms);
            write_summary(database, tx, &job.task_kind, &summary)?;
        }
        "running" => {
            database.entity_put(
                tx,
                lease_key(tx, &job)?,
                serde_json::to_vec(&job.index_entry())
                    .map_err(|_| invalid_data("worker job lease index cannot be encoded"))?,
            )?;
        }
        _ => {}
    }
    if read_stored_job(database, tx, &job.task_id)?.as_ref() != Some(&job) {
        return Err(invalid_data("worker job snapshot readback differs"));
    }
    Ok(true)
}

fn get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: GetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid worker job get request"))?;
    let Some(job) = read_stored_job(database, tx, &request.task_id)? else {
        return Ok(Value::Null);
    };
    if job.user_id != tx.owner_user_id() {
        return Ok(Value::Null);
    }
    Ok(serde_json::to_value(&job).map_err(|_| invalid_data("worker job get cannot be encoded"))?)
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid worker job scan request"))?;
    let mut suffix = tx.owner_user_id().to_be_bytes().to_vec();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("worker_job:")
            .ok_or_else(|| invalid_input("invalid worker job catalog cursor"))?;
        if encoded.is_empty() || encoded.len() > u16::MAX as usize {
            return Err(invalid_input("invalid worker job catalog cursor"));
        }
        push_text(&mut suffix, encoded)?;
        suffix.push(0);
    }
    let start = EntityKey::new(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_IDEMPOTENCY_NAMESPACE,
        &suffix,
    )?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        WORKER_JOB_IDEMPOTENCY_NAMESPACE,
        &tx.owner_user_id().to_be_bytes(),
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (_, raw) in &page {
        let entry: IdempotencyEntry = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("worker job idempotency entry is malformed"))?;
        if entry.user_id != tx.owner_user_id() {
            return Err(invalid_data("worker job catalog crosses owner scope"));
        }
        // Cross-check the independent claim catalog against its document.
        if read_stored_job(database, tx, &entry.task_id)?.is_none() {
            return Err(invalid_data("worker job claim target is missing"));
        }
        rows.push(json!({"task_id": entry.task_id, "idempotency_key": entry.idempotency_key}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("idempotency_key").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("worker job catalog page is malformed"))?;
        Some(format!("worker_job:{last}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let expected_uuid = database.authority_uuid();
    let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.worker.job.restore" => {
            let inserted = restore(database, &mut transaction, payload, expected_uuid)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.worker.job.get" => get(database, &mut transaction, payload),
        "migration.worker.job.scan" => scan(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown worker job migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn snapshot(task_id: &str, status: &str) -> Value {
        json!({
            "task_id": task_id,
            "tenant_id": "tenant-a",
            "task_kind": "background-command",
            "payload": {"command": "echo ok"},
            "idempotency_key": format!("idem-{task_id}"),
            "request_digest": "a".repeat(64),
            "status": status,
            "priority": 100,
            "available_at_ms": 1_000,
            "claim_owner": if status == "running" { "worker-1" } else { "" },
            "lease_deadline_ms": if status == "running" { 61_000 } else { 0 },
            "fencing_token": if status == "running" { 3 } else { 0 },
            "attempt_no": if status == "running" { 2 } else { 0 },
            "heartbeat_at_ms": 0,
            "cancel_sequence": 0,
            "cancel_requested_at_ms": 0,
            "cancel_reason": "",
            "replay_cursor": 0,
            "result_ref": if status == "succeeded" { "artifact://result" } else { "" },
            "error": {},
            "created_at_ms": 900,
            "updated_at_ms": 1_000,
            "terminal_at_ms": if status == "succeeded" { 2_000 } else { 0 },
        })
    }

    #[test]
    fn restored_source_epoch_is_preserved_but_cannot_grant_target_authority() {
        let (_, mut database) = prepared();
        database.set_worker_lease_test_time(100_000);
        let mut payload = snapshot("foreign-epoch", "running");
        payload["lease_authority_epoch"] = json!("ffffffffffffffffffffffffffffffff");
        payload["lease_deadline_ms"] = json!(900_000);
        execute(
            &mut database,
            7,
            11,
            "migration.worker.job.restore",
            payload.clone(),
        )
        .unwrap();
        let stored = execute(
            &mut database,
            7,
            11,
            "migration.worker.job.get",
            json!({"task_id":"foreign-epoch"}),
        )
        .unwrap();
        assert_eq!(
            stored["lease_authority_epoch"],
            payload["lease_authority_epoch"]
        );
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let proof: Value = serde_json::from_slice(
            &crate::worker_job::claim_state(&database, &mut tx, "foreign-epoch", "worker-1", 3, 1)
                .unwrap(),
        )
        .unwrap();
        assert_eq!(proof["ok"], false);
        let claimed: Value = serde_json::from_slice(
            &crate::worker_job::claim_next(
                &database,
                &mut tx,
                "target-worker",
                1,
                10_000,
                &["background-command".into()],
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(claimed["fencingToken"], 4);
        assert_eq!(claimed["leaseDeadlineMs"], 110_000);
        database.commit(tx).unwrap();
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let payload = snapshot("job-1", "running");
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.worker.job.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.worker.job.restore",
                payload
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.worker.job.get",
            json!({"task_id": "job-1"}),
        )
        .unwrap();
        assert_eq!(get["status"], "running");
        assert_eq!(get["lease_deadline_ms"], 61_000);
        assert_eq!(get["fencing_token"], 3);
        assert_eq!(get["attempt_no"], 2);
        assert_eq!(get["created_at_ms"], 900);
        let mut divergent = snapshot("job-1", "running");
        divergent["attempt_no"] = json!(9);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.worker.job.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn queued_lease_indexes_and_summary_rebuilt_atomically() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.worker.job.restore",
            snapshot("job-1", "queued"),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.worker.job.restore",
            snapshot("job-2", "running"),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.worker.job.restore",
            snapshot("job-3", "succeeded"),
        )
        .unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let queued = read_stored_job(&database, &mut transaction, "job-1")
            .unwrap()
            .unwrap();
        let queued_entity = queued_key(&transaction, &queued).unwrap();
        assert!(database
            .entity_get(&mut transaction, &queued_entity)
            .unwrap()
            .is_some());
        let running = read_stored_job(&database, &mut transaction, "job-2")
            .unwrap()
            .unwrap();
        let lease_entity = lease_key(&transaction, &running).unwrap();
        assert!(database
            .entity_get(&mut transaction, &lease_entity)
            .unwrap()
            .is_some());
        let summary = read_summary(&database, &mut transaction, "background-command").unwrap();
        assert_eq!(summary[100], 1_000);
        // A terminal job carries no queued or lease index.
        let terminal = read_stored_job(&database, &mut transaction, "job-3")
            .unwrap()
            .unwrap();
        let terminal_queued = queued_key(&transaction, &terminal).unwrap();
        assert!(database
            .entity_get(&mut transaction, &terminal_queued)
            .unwrap()
            .is_none());
    }

    #[test]
    fn scan_enumerates_owner_scoped_claim_catalog() {
        let (_vfs, mut database) = prepared();
        for id in ["job-1", "job-2", "job-3"] {
            execute(
                &mut database,
                7,
                11,
                "migration.worker.job.restore",
                snapshot(id, "queued"),
            )
            .unwrap();
        }
        let page = execute(
            &mut database,
            7,
            11,
            "migration.worker.job.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(page["cursor"], Value::Null);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.worker.job.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.worker.job.get",
                json!({"task_id": "job-1"})
            )
            .unwrap(),
            Value::Null
        );
    }

    #[test]
    fn suspended_restore_rebuilds_ready_index_and_resumes_with_new_fence() {
        let (vfs, mut database) = prepared();
        let mut payload = snapshot("waiting-job", "suspended");
        payload["available_at_ms"] = json!(2_000);
        payload["claim_owner"] = json!("previous-worker");
        payload["fencing_token"] = json!(3);
        payload["attempt_no"] = json!(2);
        payload["replay_cursor"] = json!(7);
        execute(
            &mut database,
            7,
            11,
            "migration.worker.job.restore",
            payload,
        )
        .unwrap();
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut claim = |now| {
            database.set_worker_lease_test_time(now);
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let bytes = crate::worker_job::claim_next(
                &database,
                &mut transaction,
                "resuming-worker",
                now,
                10_000,
                &["background-command".to_owned()],
            )
            .unwrap();
            if bytes.is_some() {
                database.commit(transaction).unwrap();
            }
            bytes
                .map(|bytes| serde_json::from_slice::<Value>(&bytes).unwrap())
                .unwrap_or(Value::Null)
        };
        assert_eq!(claim(1_999), Value::Null);
        let resumed = claim(2_000);
        assert_eq!(resumed["taskId"], "waiting-job");
        assert_eq!(resumed["status"], "running");
        assert_eq!(resumed["fencingToken"], 4);
        assert_eq!(resumed["attempt"], 3);
        assert_eq!(resumed["replayCursor"], 7);
    }

    #[test]
    fn invalid_metadata_fails_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"task_id":"","tenant_id":"","task_kind":"k","payload":{},"idempotency_key":"i","request_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"queued","priority":1,"available_at_ms":1,"claim_owner":"","lease_deadline_ms":0,"fencing_token":0,"attempt_no":0,"heartbeat_at_ms":0,"cancel_sequence":0,"cancel_requested_at_ms":0,"cancel_reason":"","replay_cursor":0,"result_ref":"","error":{},"created_at_ms":1,"updated_at_ms":1,"terminal_at_ms":0}),
            json!({"task_id":"j","tenant_id":"","task_kind":"k","payload":{},"idempotency_key":"i","request_digest":"not-hex","status":"queued","priority":1,"available_at_ms":1,"claim_owner":"","lease_deadline_ms":0,"fencing_token":0,"attempt_no":0,"heartbeat_at_ms":0,"cancel_sequence":0,"cancel_requested_at_ms":0,"cancel_reason":"","replay_cursor":0,"result_ref":"","error":{},"created_at_ms":1,"updated_at_ms":1,"terminal_at_ms":0}),
            json!({"task_id":"j","tenant_id":"","task_kind":"k","payload":{},"idempotency_key":"i","request_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"bogus","priority":1,"available_at_ms":1,"claim_owner":"","lease_deadline_ms":0,"fencing_token":0,"attempt_no":0,"heartbeat_at_ms":0,"cancel_sequence":0,"cancel_requested_at_ms":0,"cancel_reason":"","replay_cursor":0,"result_ref":"","error":{},"created_at_ms":1,"updated_at_ms":1,"terminal_at_ms":0}),
            json!({"task_id":"j","tenant_id":"","task_kind":"k","payload":{},"idempotency_key":"i","request_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"queued","priority":1001,"available_at_ms":1,"claim_owner":"","lease_deadline_ms":0,"fencing_token":0,"attempt_no":0,"heartbeat_at_ms":0,"cancel_sequence":0,"cancel_requested_at_ms":0,"cancel_reason":"","replay_cursor":0,"result_ref":"","error":{},"created_at_ms":1,"updated_at_ms":1,"terminal_at_ms":0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.worker.job.restore",
                payload
            )
            .is_err());
        }
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.worker.job.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
