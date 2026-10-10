//! Native journal persistence on a caller-owned TofuDB authority.
//! Store owns scoped atomic header/snapshot CAS; journal owns all transitions.
//! No files, runtime backend choice, new workers, lease takeover or retry policy.
use crate::{
    authority::AuthorityDatabase,
    versioned_document::{self, PutRequest},
};
use serde_json::Value;
use std::{
    io,
    sync::{Arc, Mutex, MutexGuard},
    time::{Instant, SystemTime, UNIX_EPOCH},
};
use tofu_runtime::checkpoint_journal::{Error, Header, SnapshotWrite, Store};

use super::{HEADERS, REPLAY, SNAPSHOTS};
use crate::logical_outbox::{
    LogicalOutboxCapture, LogicalOutboxCipher, SealedLogicalOutboxRecord, MAX_LOGICAL_PAYLOAD_BYTES,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub struct NativeCheckpointStore {
    authority: Arc<Mutex<AuthorityDatabase>>,
    tenant: u64,
    owner: u64,
    header_bytes: usize,
    snapshot_bytes: usize,
}
impl NativeCheckpointStore {
    /// Supply the selected shared authority and explicit launch-derived bounds.
    /// The caller authenticates this binding; each method rechecks its owner.
    pub fn new(
        authority: Arc<Mutex<AuthorityDatabase>>,
        tenant: u64,
        owner: u64,
        header_bytes: usize,
        snapshot_bytes: usize,
    ) -> Result<Self, Error> {
        if owner == 0
            || header_bytes == 0
            || snapshot_bytes == 0
            || header_bytes > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES
            || snapshot_bytes > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES
        {
            return Err(Error::Invalid);
        }
        // The storage codec requires sorted Map serialization. Other consumers
        // may unify provider-json-order; refuse that unsupported embedding.
        let mut order = serde_json::Map::new();
        order.insert("z".into(), Value::Null);
        order.insert("a".into(), Value::Null);
        if order.keys().next().is_some_and(|key| key == "z") {
            return Err(Error::Invalid);
        }
        // Unsupported deferred durability refuses before acquiring a lease.
        authority.lock().map_err(|_| Error::Storage)?
            .wal_durability_witness().map_err(map_io)?;
        Ok(Self {
            authority,
            tenant,
            owner,
            header_bytes,
            snapshot_bytes,
        })
    }
    fn task(&self, owner: u64, task: &str) -> Result<(), Error> {
        if owner != self.owner || task.is_empty() {
            return Err(Error::Invalid);
        }
        versioned_document::record_key(self.tenant, owner, HEADERS, task).map_err(map_io)?;
        Ok(())
    }
}
fn map_io(error: io::Error) -> Error {
    match error.kind() {
        io::ErrorKind::WouldBlock => Error::Conflict,
        io::ErrorKind::InvalidInput
        | io::ErrorKind::InvalidData
        | io::ErrorKind::PermissionDenied => Error::Invalid,
        _ => Error::Storage,
    }
}
/// Capture the immutable WAL generation while holding authority, then wait
/// without its writer. Reads/retries must not certify an unflushed terminal fence.
fn durable<T>(database: MutexGuard<'_, AuthorityDatabase>, value: T) -> Result<T, Error> {
    let witness = database.wal_durability_witness().map_err(map_io)?;
    drop(database);
    if let Some((barrier, witness)) = witness {
        barrier.wait_witness(&witness, Instant::now() + crate::wal::MAX_BARRIER_WAIT)
            .map_err(map_io)?;
    }
    Ok(value)
}
fn encode<T: Serialize + ?Sized>(value: &T, maximum: usize) -> Result<Vec<u8>, Error> {
    struct Bytes {
        value: Vec<u8>,
        maximum: usize,
        failure: Option<Error>,
    }
    impl io::Write for Bytes {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            let length = self
                .value
                .len()
                .checked_add(bytes.len())
                .filter(|n| *n <= self.maximum)
                .ok_or_else(|| {
                    self.failure = Some(Error::Oversize);
                    io::Error::other("checkpoint bytes")
                })?;
            self.value
                .try_reserve(length - self.value.len())
                .map_err(|error| {
                    self.failure = Some(Error::Storage);
                    io::Error::other(error)
                })?;
            self.value.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let mut output = Bytes {
        value: Vec::new(),
        maximum,
        failure: None,
    };
    serde_json::to_writer(&mut output, value)
        .map_err(|_| output.failure.unwrap_or(Error::Invalid))?;
    Ok(output.value)
}
impl Store for NativeCheckpointStore {
    fn header(&self, owner: u64, task: &str) -> Result<Option<Header>, Error> {
        self.task(owner, task)?;
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        let mut transaction = database.begin(self.tenant, owner).map_err(map_io)?;
        let key =
            versioned_document::record_key(self.tenant, owner, HEADERS, task).map_err(map_io)?;
        let value = versioned_document::get_value_with_version_bounded(
            &database,
            &mut transaction,
            &key,
            HEADERS,
            task,
            self.header_bytes,
        )
        .map_err(map_io)?
        .map(|(bytes, version)| {
            let value = serde_json::from_slice(&bytes).map_err(|_| Error::Invalid)?;
            Ok(Header { version, value })
        })
        .transpose()?;
        drop(transaction);
        durable(database, value)
    }
    fn compare_exchange(
        &self,
        owner: u64,
        task: &str,
        expected: u64,
        header: &Value,
        snapshot: SnapshotWrite<'_>,
    ) -> Result<u64, Error> {
        self.task(owner, task)?;
        if header["owner_user_id"].as_u64() != Some(owner) || header["task_id"] != task {
            return Err(Error::Invalid);
        }
        let next = expected.checked_add(1).ok_or(Error::Invalid)?;
        let header_json = encode(header, self.header_bytes)?;
        let snapshot_value = match &snapshot {
            SnapshotWrite::Keep => None,
            SnapshotWrite::Replace(value) => Some(*value),
        };
        let snapshot_json = match snapshot {
            SnapshotWrite::Keep => None,
            SnapshotWrite::Replace(value) => {
                if value["owner_user_id"].as_u64() != Some(owner) || value["task_id"] != task {
                    return Err(Error::Invalid);
                }
                Some(encode(value, self.snapshot_bytes)?)
            }
        };
        let mut database = self.authority.lock().map_err(|_| Error::Storage)?;
        let mut transaction = database.begin(self.tenant, owner).map_err(map_io)?;
        let key =
            versioned_document::record_key(self.tenant, owner, HEADERS, task).map_err(map_io)?;
        let actual = versioned_document::get_value_with_version_bounded(
            &database,
            &mut transaction,
            &key,
            HEADERS,
            task,
            self.header_bytes,
        )
        .map_err(map_io)?
        .map_or(0, |v| v.1);
        if actual != expected {
            return Err(Error::Conflict);
        }
        let now = u64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| Error::Storage)?
                .as_millis(),
        )
        .map_err(|_| Error::Storage)?
        .max(1);
        let capture = if database.logical_outbox_is_configured() {
            let mutation = Mutation {
                format: FORMAT,
                source_uuid: database.authority_uuid().to_string(),
                owner_user_id: owner,
                task_id: task,
                expected_version: expected,
                header,
                snapshot: snapshot_value,
                updated_at_ms: now,
            };
            let clear_payload = encode(&mutation, MAX_LOGICAL_PAYLOAD_BYTES)?;
            let digest = Sha256::digest(&clear_payload).into();
            Some(LogicalOutboxCapture {
                schema_version: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
                registry_version: crate::generated_storage_operations::STORAGE_REGISTRY_VERSION,
                operation: OPERATION.into(),
                request_id: uuid::Uuid::new_v4().to_string(),
                request_digest: digest,
                command_id: None,
                committed_at_ms: now,
                clear_payload,
            })
        } else {
            None
        };
        versioned_document::put_known_version_returning_envelope(
            &database,
            &mut transaction,
            PutRequest {
                key,
                namespace: HEADERS.into(),
                logical_key: task.into(),
                value_json: header_json,
                expected_version: Some(expected),
                updated_at_ms: now,
            },
            actual,
        )
        .map_err(map_io)?;
        if let Some(value_json) = snapshot_json {
            let key = versioned_document::record_key(self.tenant, owner, SNAPSHOTS, task)
                .map_err(map_io)?;
            versioned_document::put(
                &database,
                &mut transaction,
                PutRequest {
                    key,
                    namespace: SNAPSHOTS.into(),
                    logical_key: task.into(),
                    value_json,
                    expected_version: None,
                    updated_at_ms: now,
                },
            )
            .map_err(map_io)?;
        }
        if let Some(capture) = capture {
            database
                .logical_outbox_capture(&mut transaction, capture)
                .map_err(map_io)?;
        }
        // The authority retains WAL/fsync, OCC, blob and configured-outbox fences.
        // A refusal or uncertain commit is never converted to successful CAS.
        database.commit(transaction).map_err(map_io)?;
        durable(database, next)
    }
    fn snapshot(&self, owner: u64, task: &str, version: u64) -> Result<Option<Value>, Error> {
        self.task(owner, task)?;
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        let mut transaction = database.begin(self.tenant, owner).map_err(map_io)?;
        let key =
            versioned_document::record_key(self.tenant, owner, HEADERS, task).map_err(map_io)?;
        let actual = versioned_document::get_value_with_version_bounded(
            &database,
            &mut transaction,
            &key,
            HEADERS,
            task,
            self.header_bytes,
        )
        .map_err(map_io)?
        .map_or(0, |v| v.1);
        if actual != version || actual == 0 {
            return Err(Error::Conflict);
        }
        let key =
            versioned_document::record_key(self.tenant, owner, SNAPSHOTS, task).map_err(map_io)?;
        let value = versioned_document::get_value_with_version_bounded(
            &database,
            &mut transaction,
            &key,
            SNAPSHOTS,
            task,
            self.snapshot_bytes,
        )
        .map_err(map_io)?
        .map(|(bytes, _)| serde_json::from_slice(&bytes).map_err(|_| Error::Invalid))
        .transpose()?;
        drop(transaction);
        durable(database, value)
    }
}

#[cfg(test)]
#[path = "native_checkpoint_store_tests.rs"]
mod tests;

const FORMAT: &str = "tofu.native-checkpoint-mutation/v1";
const OPERATION: &str = "native_checkpoint.compare_exchange";
#[derive(Serialize)]
struct Mutation<'a> {
    format: &'a str,
    source_uuid: String,
    owner_user_id: u64,
    task_id: &'a str,
    expected_version: u64,
    header: &'a Value,
    snapshot: Option<&'a Value>,
    updated_at_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReceivedMutation {
    format: String,
    source_uuid: String,
    owner_user_id: u64,
    task_id: String,
    expected_version: u64,
    header: Value,
    snapshot: Value,
    updated_at_ms: u64,
}
impl NativeCheckpointStore {
    /// Explicit passive-target replay. Cipher authenticates the source; source_uuid
    /// is pinned by trusted composition. Target must not have its own outbox.
    /// Current-version redelivery is idempotent. Older/gapped deliveries refuse:
    /// caller resumes from its confirmed per-task version, never guesses a prefix.
    pub fn replay(
        &self,
        cipher: &LogicalOutboxCipher,
        source_uuid: &str,
        record: &SealedLogicalOutboxRecord,
    ) -> Result<u64, Error> {
        self.replay_bound(cipher, source_uuid, record, None, false)
    }
    pub(super) fn scope(&self) -> (u64, u64) {
        (self.tenant, self.owner)
    }
    pub(super) fn replay_archived(
        &self,
        cipher: &LogicalOutboxCipher,
        source_uuid: &str,
        proof: &crate::outbox_sink::VerifiedArchiveRecord<'_>,
    ) -> Result<u64, Error> {
        self.replay_bound(
            cipher,
            source_uuid,
            proof.record(),
            Some(proof.archive_uuid()),
            true,
        )
    }
    pub(super) fn replay_for_archive(
        &self,
        cipher: &LogicalOutboxCipher,
        source_uuid: &str,
        record: &SealedLogicalOutboxRecord,
        archive_uuid: uuid::Uuid,
    ) -> Result<u64, Error> {
        self.replay_bound(cipher, source_uuid, record, Some(archive_uuid), false)
    }
    fn replay_bound(
        &self,
        cipher: &LogicalOutboxCipher,
        source_uuid: &str,
        record: &SealedLogicalOutboxRecord,
        archive_uuid: Option<uuid::Uuid>,
        verified_old: bool,
    ) -> Result<u64, Error> {
        if record.identity.tenant_id != self.tenant
            || record.identity.owner_user_id != self.owner
            || record.identity.operation != OPERATION
            || record.identity.schema_version
                != crate::generated_storage_operations::STORAGE_SCHEMA_VERSION
            || record.identity.registry_version
                != crate::generated_storage_operations::STORAGE_REGISTRY_VERSION
        {
            return Err(Error::Invalid);
        }
        let clear = cipher.open(record).map_err(map_io)?;
        if <[u8; 32]>::from(Sha256::digest(&clear)) != record.identity.request_digest {
            return Err(Error::Invalid);
        }
        let mutation: ReceivedMutation =
            serde_json::from_slice(&clear).map_err(|_| Error::Invalid)?;
        self.task(mutation.owner_user_id, &mutation.task_id)?;
        if mutation.format != FORMAT
            || mutation.source_uuid != source_uuid
            || uuid::Uuid::parse_str(source_uuid).is_err()
            || mutation.updated_at_ms == 0
            || mutation.updated_at_ms != record.identity.committed_at_ms
            || mutation.header["owner_user_id"].as_u64() != Some(self.owner)
            || mutation.header["task_id"] != mutation.task_id
            || (!mutation.snapshot.is_null()
                && (mutation.snapshot["owner_user_id"].as_u64() != Some(self.owner)
                    || mutation.snapshot["task_id"] != mutation.task_id))
        {
            return Err(Error::Invalid);
        }
        let next = mutation
            .expected_version
            .checked_add(1)
            .ok_or(Error::Invalid)?;
        let header_json = encode(&mutation.header, self.header_bytes)?;
        let snapshot_json = if mutation.snapshot.is_null() {
            None
        } else {
            Some(encode(&mutation.snapshot, self.snapshot_bytes)?)
        };
        let mut database = self.authority.lock().map_err(|_| Error::Storage)?;
        if database.logical_outbox_is_configured() {
            return Err(Error::Invalid);
        }
        let mut transaction = database.begin(self.tenant, self.owner).map_err(map_io)?;
        let key =
            versioned_document::record_key(self.tenant, self.owner, HEADERS, &mutation.task_id)
                .map_err(map_io)?;
        let current = versioned_document::get_value_with_version_bounded(
            &database,
            &mut transaction,
            &key,
            HEADERS,
            &mutation.task_id,
            self.header_bytes,
        )
        .map_err(map_io)?;
        let actual = current.as_ref().map_or(0, |v| v.1);
        let receipt_key =
            versioned_document::record_key(self.tenant, self.owner, REPLAY, &mutation.task_id)
                .map_err(map_io)?;
        let receipt = versioned_document::get_value_with_version_bounded(
            &database,
            &mut transaction,
            &receipt_key,
            REPLAY,
            &mutation.task_id,
            self.header_bytes,
        )
        .map_err(map_io)?;
        let digest = record
            .identity
            .request_digest
            .iter()
            .map(|v| format!("{v:02x}"))
            .collect::<String>();
        let mut expected_receipt =
            serde_json::json!({"source_uuid":source_uuid,"version":next,"digest":digest});
        if let Some(archive_uuid) = archive_uuid {
            expected_receipt["archive_uuid"] = Value::String(archive_uuid.to_string());
        }
        let previous = receipt
            .as_ref()
            .map(|v| serde_json::from_slice::<Value>(&v.0).map_err(|_| Error::Invalid))
            .transpose()?;
        let archive_matches = previous.as_ref().is_some_and(|value| match archive_uuid {
            Some(id) => value["archive_uuid"].as_str() == Some(id.to_string().as_str()),
            None => value.get("archive_uuid").is_none(),
        });
        if verified_old
            && actual > next
            && archive_matches
            && previous.as_ref().is_some_and(|value| {
                value["source_uuid"] == source_uuid && value["version"].as_u64() == Some(actual)
            })
        {
            drop(transaction);
            return durable(database, next);
        }
        if actual == next && previous.as_ref() == Some(&expected_receipt) {
            drop(transaction);
            return durable(database, next);
        }
        if actual != mutation.expected_version
            || (actual == 0 && previous.is_some())
            || (actual != 0
                && previous.as_ref().is_none_or(|v| {
                    v["source_uuid"] != source_uuid
                        || v["version"].as_u64() != Some(actual)
                        || !archive_matches
                }))
        {
            return Err(Error::Conflict);
        }
        versioned_document::put_known_version_returning_envelope(
            &database,
            &mut transaction,
            PutRequest {
                key,
                namespace: HEADERS.into(),
                logical_key: mutation.task_id.clone(),
                value_json: header_json,
                expected_version: Some(actual),
                updated_at_ms: mutation.updated_at_ms,
            },
            actual,
        )
        .map_err(map_io)?;
        if let Some(value_json) = snapshot_json {
            let key = versioned_document::record_key(
                self.tenant,
                self.owner,
                SNAPSHOTS,
                &mutation.task_id,
            )
            .map_err(map_io)?;
            versioned_document::put(
                &database,
                &mut transaction,
                PutRequest {
                    key,
                    namespace: SNAPSHOTS.into(),
                    logical_key: mutation.task_id.clone(),
                    value_json,
                    expected_version: None,
                    updated_at_ms: mutation.updated_at_ms,
                },
            )
            .map_err(map_io)?;
        }
        versioned_document::put(
            &database,
            &mut transaction,
            PutRequest {
                key: receipt_key,
                namespace: REPLAY.into(),
                logical_key: mutation.task_id,
                value_json: encode(&expected_receipt, self.header_bytes)?,
                expected_version: None,
                updated_at_ms: mutation.updated_at_ms,
            },
        )
        .map_err(map_io)?;
        database.commit(transaction).map_err(map_io)?;
        durable(database, next)
    }
}

impl NativeCheckpointStore {
    pub fn is_restart_required(&self) -> bool {
        self.authority
            .lock()
            .map_or(true, |database| database.is_restart_required() || database.wal_durability_witness().is_err())
    }
    pub fn source_uuid(&self) -> Result<String, Error> {
        Ok(self
            .authority
            .lock()
            .map_err(|_| Error::Storage)?
            .authority_uuid()
            .to_string())
    }
    pub fn publish_pending(
        &self,
        sink: &mut super::publisher::NativeCheckpointSink,
        budget: crate::outbox_publisher::LogicalOutboxPublishBudget,
    ) -> Result<crate::outbox_publisher::LogicalOutboxPublishResult, Error> {
        crate::outbox_publisher::LogicalOutboxPublishBudget::new(
            budget.max_records,
            budget.max_bytes,
        )
        .map_err(map_io)?;
        if sink.source_uuid() != self.source_uuid()? || sink.scope() != (self.tenant, self.owner) {
            return Err(Error::Invalid);
        }
        let pending = {
            let database = self.authority.lock().map_err(|_| Error::Storage)?;
            let pending = database
                .logical_outbox_pending_bounded(
                    self.tenant,
                    self.owner,
                    budget.max_records,
                    budget.max_bytes,
                )
                .map_err(map_io)?;
            durable(database, pending)?
        };
        let result =
            crate::outbox_publisher::publish_batch(sink, &pending, budget).map_err(map_io)?;
        let mut database = self.authority.lock().map_err(|_| Error::Storage)?;
        for receipt in &result.receipts {
            database
                .logical_outbox_acknowledge(
                    self.tenant,
                    self.owner,
                    receipt.sequence,
                    receipt.event_id,
                )
                .map_err(map_io)?;
        }
        durable(database, result)
    }
}
