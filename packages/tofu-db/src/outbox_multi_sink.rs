//! One Engine-backed logical-outbox sink for multiple explicit owner scopes.
//!
//! The sink stores its routing catalog, aggregate byte witness, per-owner
//! sequence witnesses, and exact historical records in the authority entity
//! tree under one explicitly supplied administrative scope. This keeps reopen
//! bounded to at most 64 point reads and avoids one database, thread, or cache
//! per user. Large sealed records use the existing content-addressed blob path.

use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::logical_outbox::{
    SealedLogicalOutboxRecord, StoredLogicalOutboxRecord, MAX_ENCODED_LOGICAL_OUTBOX_BYTES,
    MAX_INLINE_LOGICAL_OUTBOX_BYTES,
};
use crate::outbox_publisher::{
    DurableLogicalOutboxReceipt, LogicalOutboxOwnerScope, LogicalOutboxSink,
};
use crate::outbox_relay::MAX_OUTBOX_RELAY_OWNER_SCOPES;
use crate::outbox_sink::{assess_capacity, preflight_real_capacity, MIN_FREE_RESERVE_BYTES};
use crate::vfs::Vfs;

const NAMESPACE: &str = "logical_outbox_multi_sink";
const CONFIG_KEY: &[u8] = b"config";
const TOTAL_BYTES_KEY: &[u8] = b"total_bytes";
const OWNER_STATE_PREFIX: &[u8] = b"owner:";
const RECORD_PREFIX: &[u8] = b"record:";
const CONFIG_MAGIC: &[u8; 8] = b"TDBMSC01";
const STATE_MAGIC: &[u8; 8] = b"TDBMSS01";
const VERSION: u32 = 1;
const CONFIG_HEADER_BYTES: usize = 8 + 4 + 8 + 8 + 8 + 4;
const CONFIG_SCOPE_BYTES: usize = 16;
const STATE_BYTES: usize = 8 + 4 + 8 + 8;
const RECORD_ACCOUNTING_OVERHEAD: u64 = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MultiplexedEngineLogicalOutboxSinkConfig {
    pub administrative_scope: LogicalOutboxOwnerScope,
    pub source_scopes: Vec<LogicalOutboxOwnerScope>,
    pub max_logical_bytes: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct OwnerState {
    durable_sequence: u64,
    logical_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MultiplexedEngineLogicalOutboxSinkStatus {
    pub configured_owner_scopes: usize,
    pub active_owner_scopes: usize,
    pub durable_records: u64,
    pub logical_bytes: u64,
}

pub struct MultiplexedEngineLogicalOutboxSink {
    database: AuthorityDatabase,
    config: MultiplexedEngineLogicalOutboxSinkConfig,
    owner_states: BTreeMap<LogicalOutboxOwnerScope, OwnerState>,
    total_logical_bytes: u64,
    real_capacity_path: Option<PathBuf>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn storage_full(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::StorageFull, message)
}

impl MultiplexedEngineLogicalOutboxSinkConfig {
    fn canonicalized(mut self) -> io::Result<Self> {
        LogicalOutboxOwnerScope::new(
            self.administrative_scope.tenant_id,
            self.administrative_scope.owner_user_id,
        )?;
        if self.source_scopes.is_empty()
            || self.source_scopes.len() > MAX_OUTBOX_RELAY_OWNER_SCOPES
            || self.max_logical_bytes <= RECORD_ACCOUNTING_OVERHEAD
            || self.max_logical_bytes > crate::outbox_sink::MAX_SINK_LOGICAL_BYTES
        {
            return Err(invalid_input(
                "invalid multiplexed logical outbox sink configuration",
            ));
        }
        self.source_scopes.sort_unstable();
        let mut unique = BTreeSet::new();
        for scope in &self.source_scopes {
            LogicalOutboxOwnerScope::new(scope.tenant_id, scope.owner_user_id)?;
            if !unique.insert(*scope) {
                return Err(invalid_input(
                    "multiplexed logical outbox sink scopes must be unique",
                ));
            }
        }
        Ok(self)
    }

    fn encode(&self) -> io::Result<Vec<u8>> {
        let count = u32::try_from(self.source_scopes.len())
            .map_err(|_| invalid_input("multiplexed sink scope count overflow"))?;
        let mut encoded =
            Vec::with_capacity(CONFIG_HEADER_BYTES + self.source_scopes.len() * CONFIG_SCOPE_BYTES);
        encoded.extend_from_slice(CONFIG_MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.administrative_scope.tenant_id.to_le_bytes());
        encoded.extend_from_slice(&self.administrative_scope.owner_user_id.to_le_bytes());
        encoded.extend_from_slice(&self.max_logical_bytes.to_le_bytes());
        encoded.extend_from_slice(&count.to_le_bytes());
        for scope in &self.source_scopes {
            encoded.extend_from_slice(&scope.tenant_id.to_le_bytes());
            encoded.extend_from_slice(&scope.owner_user_id.to_le_bytes());
        }
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < CONFIG_HEADER_BYTES || !encoded.starts_with(CONFIG_MAGIC) {
            return Err(invalid_data("invalid multiplexed sink configuration"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let administrative_scope = LogicalOutboxOwnerScope {
            tenant_id: u64::from_le_bytes(encoded[12..20].try_into().unwrap()),
            owner_user_id: u64::from_le_bytes(encoded[20..28].try_into().unwrap()),
        };
        let max_logical_bytes = u64::from_le_bytes(encoded[28..36].try_into().unwrap());
        let count = u32::from_le_bytes(encoded[36..40].try_into().unwrap()) as usize;
        let expected = CONFIG_HEADER_BYTES
            .checked_add(
                count
                    .checked_mul(CONFIG_SCOPE_BYTES)
                    .ok_or_else(|| invalid_data("multiplexed sink scope size overflow"))?,
            )
            .ok_or_else(|| invalid_data("multiplexed sink config size overflow"))?;
        if version != VERSION
            || count == 0
            || count > MAX_OUTBOX_RELAY_OWNER_SCOPES
            || expected != encoded.len()
        {
            return Err(invalid_data(
                "invalid multiplexed sink configuration header",
            ));
        }
        let mut source_scopes = Vec::with_capacity(count);
        for chunk in encoded[CONFIG_HEADER_BYTES..].chunks_exact(CONFIG_SCOPE_BYTES) {
            source_scopes.push(LogicalOutboxOwnerScope {
                tenant_id: u64::from_le_bytes(chunk[..8].try_into().unwrap()),
                owner_user_id: u64::from_le_bytes(chunk[8..].try_into().unwrap()),
            });
        }
        Self {
            administrative_scope,
            source_scopes,
            max_logical_bytes,
        }
        .canonicalized()
        .map_err(|_| invalid_data("invalid multiplexed sink configuration values"))
    }
}

impl OwnerState {
    fn encode(self) -> Vec<u8> {
        let mut encoded = Vec::with_capacity(STATE_BYTES);
        encoded.extend_from_slice(STATE_MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.durable_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.logical_bytes.to_le_bytes());
        encoded
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() != STATE_BYTES || !encoded.starts_with(STATE_MAGIC) {
            return Err(invalid_data("invalid multiplexed sink owner state"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let state = Self {
            durable_sequence: u64::from_le_bytes(encoded[12..20].try_into().unwrap()),
            logical_bytes: u64::from_le_bytes(encoded[20..28].try_into().unwrap()),
        };
        if version != VERSION || (state.durable_sequence == 0) != (state.logical_bytes == 0) {
            return Err(invalid_data("invalid multiplexed sink owner state values"));
        }
        Ok(state)
    }
}

fn administrative_key(
    config: &MultiplexedEngineLogicalOutboxSinkConfig,
    key: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        config.administrative_scope.tenant_id,
        config.administrative_scope.owner_user_id,
        NAMESPACE,
        key,
    )
}

fn owner_state_key(
    config: &MultiplexedEngineLogicalOutboxSinkConfig,
    scope: LogicalOutboxOwnerScope,
) -> io::Result<EntityKey> {
    let mut key = Vec::with_capacity(OWNER_STATE_PREFIX.len() + 16);
    key.extend_from_slice(OWNER_STATE_PREFIX);
    key.extend_from_slice(&scope.tenant_id.to_be_bytes());
    key.extend_from_slice(&scope.owner_user_id.to_be_bytes());
    administrative_key(config, &key)
}

fn record_key(
    config: &MultiplexedEngineLogicalOutboxSinkConfig,
    scope: LogicalOutboxOwnerScope,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut key = Vec::with_capacity(RECORD_PREFIX.len() + 24);
    key.extend_from_slice(RECORD_PREFIX);
    key.extend_from_slice(&scope.tenant_id.to_be_bytes());
    key.extend_from_slice(&scope.owner_user_id.to_be_bytes());
    key.extend_from_slice(&sequence.to_be_bytes());
    administrative_key(config, &key)
}

fn decode_counter(encoded: Option<Vec<u8>>) -> io::Result<u64> {
    match encoded {
        Some(encoded) if encoded.len() == 8 => Ok(u64::from_le_bytes(encoded.try_into().unwrap())),
        Some(_) => Err(invalid_data("invalid multiplexed sink total byte witness")),
        None => Err(invalid_data(
            "multiplexed sink total byte witness is missing",
        )),
    }
}

impl MultiplexedEngineLogicalOutboxSink {
    pub fn initialize(
        data_dir: &Path,
        config: MultiplexedEngineLogicalOutboxSinkConfig,
    ) -> io::Result<Self> {
        let config = config.canonicalized()?;
        preflight_real_capacity(data_dir, config.max_logical_bytes)?;
        Self::initialize_database(
            AuthorityDatabase::initialize(data_dir)?,
            config,
            Some(data_dir.to_owned()),
        )
    }

    pub fn open(
        data_dir: &Path,
        config: MultiplexedEngineLogicalOutboxSinkConfig,
    ) -> io::Result<Self> {
        let config = config.canonicalized()?;
        if !data_dir.is_absolute() {
            return Err(invalid_input("multiplexed sink path must be absolute"));
        }
        let sink = Self::open_database(
            AuthorityDatabase::open(data_dir)?,
            config,
            Some(data_dir.to_owned()),
        )?;
        let remaining = sink
            .config
            .max_logical_bytes
            .checked_sub(sink.total_logical_bytes)
            .ok_or_else(|| invalid_data("multiplexed sink exceeds configured capacity"))?;
        assess_capacity(remaining, fs2::available_space(data_dir)?)?;
        Ok(sink)
    }

    pub fn initialize_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        config: MultiplexedEngineLogicalOutboxSinkConfig,
    ) -> io::Result<Self> {
        Self::initialize_database(
            AuthorityDatabase::initialize_with_vfs(data_dir, vfs)?,
            config.canonicalized()?,
            None,
        )
    }

    pub fn open_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        config: MultiplexedEngineLogicalOutboxSinkConfig,
    ) -> io::Result<Self> {
        Self::open_database(
            AuthorityDatabase::open_with_vfs(data_dir, vfs)?,
            config.canonicalized()?,
            None,
        )
    }

    fn initialize_database(
        mut database: AuthorityDatabase,
        config: MultiplexedEngineLogicalOutboxSinkConfig,
        real_capacity_path: Option<PathBuf>,
    ) -> io::Result<Self> {
        let mut transaction = database.begin(
            config.administrative_scope.tenant_id,
            config.administrative_scope.owner_user_id,
        )?;
        database.entity_put(
            &mut transaction,
            administrative_key(&config, CONFIG_KEY)?,
            config.encode()?,
        )?;
        database.entity_put(
            &mut transaction,
            administrative_key(&config, TOTAL_BYTES_KEY)?,
            0_u64.to_le_bytes().to_vec(),
        )?;
        database.commit(transaction)?;
        Ok(Self {
            database,
            config,
            owner_states: BTreeMap::new(),
            total_logical_bytes: 0,
            real_capacity_path,
        })
    }

    fn open_database(
        database: AuthorityDatabase,
        config: MultiplexedEngineLogicalOutboxSinkConfig,
        real_capacity_path: Option<PathBuf>,
    ) -> io::Result<Self> {
        let mut transaction = database.begin(
            config.administrative_scope.tenant_id,
            config.administrative_scope.owner_user_id,
        )?;
        let stored_config = database
            .entity_get(&mut transaction, &administrative_key(&config, CONFIG_KEY)?)?
            .ok_or_else(|| invalid_data("multiplexed sink configuration is missing"))?;
        if MultiplexedEngineLogicalOutboxSinkConfig::decode(&stored_config)? != config {
            return Err(invalid_data("multiplexed sink configuration differs"));
        }
        let total_logical_bytes = decode_counter(database.entity_get(
            &mut transaction,
            &administrative_key(&config, TOTAL_BYTES_KEY)?,
        )?)?;
        let mut owner_states = BTreeMap::new();
        let mut summed_bytes = 0_u64;
        for scope in &config.source_scopes {
            let Some(encoded) =
                database.entity_get(&mut transaction, &owner_state_key(&config, *scope)?)?
            else {
                continue;
            };
            let state = OwnerState::decode(&encoded)?;
            summed_bytes = summed_bytes
                .checked_add(state.logical_bytes)
                .ok_or_else(|| invalid_data("multiplexed sink owner byte sum overflow"))?;
            owner_states.insert(*scope, state);
        }
        if summed_bytes != total_logical_bytes || total_logical_bytes > config.max_logical_bytes {
            return Err(invalid_data(
                "multiplexed sink aggregate byte witness differs",
            ));
        }
        Ok(Self {
            database,
            config,
            owner_states,
            total_logical_bytes,
            real_capacity_path,
        })
    }

    pub fn status(&self) -> MultiplexedEngineLogicalOutboxSinkStatus {
        MultiplexedEngineLogicalOutboxSinkStatus {
            configured_owner_scopes: self.config.source_scopes.len(),
            active_owner_scopes: self.owner_states.len(),
            durable_records: self
                .owner_states
                .values()
                .map(|state| state.durable_sequence)
                .sum(),
            logical_bytes: self.total_logical_bytes,
        }
    }

    fn begin_administrative(&self) -> io::Result<AuthorityTransaction> {
        self.database.begin(
            self.config.administrative_scope.tenant_id,
            self.config.administrative_scope.owner_user_id,
        )
    }

    fn stored_record(
        &self,
        transaction: &mut AuthorityTransaction,
        scope: LogicalOutboxOwnerScope,
        sequence: u64,
    ) -> io::Result<StoredLogicalOutboxRecord> {
        let encoded = self
            .database
            .entity_get(transaction, &record_key(&self.config, scope, sequence)?)?
            .ok_or_else(|| invalid_data("multiplexed sink record is missing"))?;
        StoredLogicalOutboxRecord::decode(&encoded)
    }

    fn materialize_record(&self, stored: &StoredLogicalOutboxRecord) -> io::Result<Vec<u8>> {
        match stored {
            StoredLogicalOutboxRecord::Inline(encoded) => Ok(encoded.clone()),
            StoredLogicalOutboxRecord::Blob(reference) => {
                let mut reader = self.database.reachable_blob_reader(
                    self.config.administrative_scope.tenant_id,
                    self.config.administrative_scope.owner_user_id,
                    *reference,
                )?;
                let mut encoded = Vec::with_capacity(reference.logical_bytes as usize);
                while let Some(chunk) = reader.next_chunk()? {
                    encoded.extend_from_slice(&chunk);
                    if encoded.len() > MAX_ENCODED_LOGICAL_OUTBOX_BYTES {
                        return Err(invalid_data("multiplexed sink blob exceeds its bound"));
                    }
                }
                if encoded.len() != reference.logical_bytes as usize {
                    return Err(invalid_data("multiplexed sink blob length differs"));
                }
                Ok(encoded)
            }
        }
    }

    fn verify_existing(
        &self,
        scope: LogicalOutboxOwnerScope,
        record: &SealedLogicalOutboxRecord,
    ) -> io::Result<()> {
        let mut transaction = self.begin_administrative()?;
        let stored = self.stored_record(&mut transaction, scope, record.identity.sequence)?;
        if self.materialize_record(&stored)? != record.encode()? {
            return Err(invalid_data(
                "multiplexed sink sequence contains another record",
            ));
        }
        Ok(())
    }

    fn receipt(record: &SealedLogicalOutboxRecord) -> DurableLogicalOutboxReceipt {
        DurableLogicalOutboxReceipt {
            tenant_id: record.identity.tenant_id,
            owner_user_id: record.identity.owner_user_id,
            sequence: record.identity.sequence,
            event_id: record.event_id,
        }
    }
}

impl LogicalOutboxSink for MultiplexedEngineLogicalOutboxSink {
    fn append_durable(
        &mut self,
        record: &SealedLogicalOutboxRecord,
    ) -> io::Result<DurableLogicalOutboxReceipt> {
        record.validate()?;
        let scope = LogicalOutboxOwnerScope {
            tenant_id: record.identity.tenant_id,
            owner_user_id: record.identity.owner_user_id,
        };
        if self.config.source_scopes.binary_search(&scope).is_err() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "logical outbox record is outside configured source scopes",
            ));
        }
        let current = self.owner_states.get(&scope).copied().unwrap_or_default();
        if record.identity.sequence <= current.durable_sequence {
            self.verify_existing(scope, record)?;
            return Ok(Self::receipt(record));
        }
        if current.durable_sequence.checked_add(1) != Some(record.identity.sequence) {
            return Err(invalid_data("multiplexed sink owner sequence has a gap"));
        }
        let record_encoded = record.encode()?;
        let charged_bytes = (record_encoded.len() as u64)
            .checked_add(RECORD_ACCOUNTING_OVERHEAD)
            .ok_or_else(|| invalid_input("multiplexed sink record charge overflow"))?;
        let next_owner = OwnerState {
            durable_sequence: record.identity.sequence,
            logical_bytes: current
                .logical_bytes
                .checked_add(charged_bytes)
                .ok_or_else(|| invalid_data("multiplexed sink owner bytes overflow"))?,
        };
        let next_total = self
            .total_logical_bytes
            .checked_add(charged_bytes)
            .filter(|bytes| *bytes <= self.config.max_logical_bytes)
            .ok_or_else(|| storage_full("multiplexed sink reached its byte ceiling"))?;
        if let Some(path) = &self.real_capacity_path {
            let required = charged_bytes
                .checked_add(MIN_FREE_RESERVE_BYTES)
                .ok_or_else(|| invalid_input("multiplexed sink capacity overflow"))?;
            if fs2::available_space(path)? < required {
                return Err(storage_full(
                    "multiplexed sink volume lacks append and recovery headroom",
                ));
            }
        }

        let mut transaction = self.begin_administrative()?;
        let witnessed_owner = self
            .database
            .entity_get(&mut transaction, &owner_state_key(&self.config, scope)?)?
            .map(|encoded| OwnerState::decode(&encoded))
            .transpose()?
            .unwrap_or_default();
        let witnessed_total = decode_counter(self.database.entity_get(
            &mut transaction,
            &administrative_key(&self.config, TOTAL_BYTES_KEY)?,
        )?)?;
        if witnessed_owner != current || witnessed_total != self.total_logical_bytes {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "multiplexed sink state changed concurrently",
            ));
        }
        let stored = if record_encoded.len() <= MAX_INLINE_LOGICAL_OUTBOX_BYTES {
            StoredLogicalOutboxRecord::Inline(record_encoded)
        } else {
            let mut reader = record_encoded.as_slice();
            StoredLogicalOutboxRecord::Blob(self.database.stage_blob(
                &mut transaction,
                &mut reader,
                record_encoded.len() as u64,
            )?)
        };
        self.database.entity_put(
            &mut transaction,
            record_key(&self.config, scope, record.identity.sequence)?,
            stored.encode()?,
        )?;
        self.database.entity_put(
            &mut transaction,
            owner_state_key(&self.config, scope)?,
            next_owner.encode(),
        )?;
        self.database.entity_put(
            &mut transaction,
            administrative_key(&self.config, TOTAL_BYTES_KEY)?,
            next_total.to_le_bytes().to_vec(),
        )?;
        self.database.commit(transaction)?;
        self.owner_states.insert(scope, next_owner);
        self.total_logical_bytes = next_total;
        Ok(Self::receipt(record))
    }

    fn is_restart_required(&self) -> bool {
        self.database.is_restart_required()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logical_outbox::{LogicalOutboxCipher, LogicalOutboxIdentity};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    fn scope(owner_user_id: u64) -> LogicalOutboxOwnerScope {
        LogicalOutboxOwnerScope::new(7, owner_user_id).unwrap()
    }

    fn config(max_logical_bytes: u64) -> MultiplexedEngineLogicalOutboxSinkConfig {
        MultiplexedEngineLogicalOutboxSinkConfig {
            administrative_scope: LogicalOutboxOwnerScope::new(99, 101).unwrap(),
            source_scopes: vec![scope(11), scope(12)],
            max_logical_bytes,
        }
    }

    fn record(owner_user_id: u64, sequence: u64, payload: &[u8]) -> SealedLogicalOutboxRecord {
        LogicalOutboxCipher::new(&[61; 32])
            .seal(
                LogicalOutboxIdentity {
                    sequence,
                    tenant_id: 7,
                    owner_user_id,
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: format!("request-{owner_user_id}-{sequence}"),
                    request_digest: [sequence as u8; 32],
                    command_id: Some(format!("command-{owner_user_id}-{sequence}")),
                    committed_at_ms: 6_000 + sequence,
                },
                payload,
            )
            .unwrap()
    }

    fn prepared_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/sink")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn initialized(vfs: Arc<DeterministicVfs>) -> MultiplexedEngineLogicalOutboxSink {
        let sink = MultiplexedEngineLogicalOutboxSink::initialize_with_vfs(
            Path::new("/sink"),
            vfs.clone(),
            config(1024 * 1024),
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        sink
    }

    #[test]
    fn multiple_owner_sequences_and_large_records_reopen_without_history_scan() {
        let directory = tempfile::tempdir().unwrap();
        let sink_path = directory.path().join("sink");
        let first_a = record(11, 1, b"first-a");
        let first_b = record(12, 1, &vec![7; 32 * 1024]);
        let second_a = record(11, 2, b"second-a");
        let status;
        {
            let mut sink =
                MultiplexedEngineLogicalOutboxSink::initialize(&sink_path, config(1024 * 1024))
                    .unwrap();
            sink.append_durable(&first_a).unwrap();
            sink.append_durable(&first_b).unwrap();
            sink.append_durable(&second_a).unwrap();
            assert_eq!(
                sink.append_durable(&first_a).unwrap(),
                MultiplexedEngineLogicalOutboxSink::receipt(&first_a)
            );
            status = sink.status();
        }
        assert_eq!(status.configured_owner_scopes, 2);
        assert_eq!(status.active_owner_scopes, 2);
        assert_eq!(status.durable_records, 3);
        let mut reopened =
            MultiplexedEngineLogicalOutboxSink::open(&sink_path, config(1024 * 1024)).unwrap();
        assert_eq!(reopened.status(), status);
        assert_eq!(
            reopened.append_durable(&first_b).unwrap(),
            MultiplexedEngineLogicalOutboxSink::receipt(&first_b)
        );
        assert_eq!(
            reopened
                .append_durable(&record(11, 1, b"different"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(
            reopened
                .append_durable(&record(11, 4, b"gap"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(
            reopened
                .append_durable(&record(13, 1, b"outside"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn configuration_and_aggregate_capacity_fail_closed() {
        let vfs = prepared_vfs();
        let first = record(11, 1, b"first");
        let exact_charge = first.encoded_len().unwrap() as u64 + RECORD_ACCOUNTING_OVERHEAD;
        let mut sink = MultiplexedEngineLogicalOutboxSink::initialize_with_vfs(
            Path::new("/sink"),
            vfs.clone(),
            config(exact_charge),
        )
        .unwrap();
        sink.append_durable(&first).unwrap();
        assert_eq!(
            sink.append_durable(&record(12, 1, b"second"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::StorageFull
        );
        drop(sink);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let different = MultiplexedEngineLogicalOutboxSink::open_with_vfs(
            Path::new("/sink"),
            vfs,
            config(exact_charge + 1),
        );
        let error = match different {
            Ok(_) => panic!("multiplexed sink accepted a different durable configuration"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn every_append_fault_recovers_all_multi_owner_indexes_atomically() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = initialized(baseline_vfs.clone());
        let first = record(11, 1, &vec![5; 16 * 1024]);
        baseline.append_durable(&first).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut sink = initialized(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = sink.append_durable(&first);
            drop(sink);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut reopened = MultiplexedEngineLogicalOutboxSink::open_with_vfs(
                Path::new("/sink"),
                vfs,
                config(1024 * 1024),
            )
            .unwrap();
            assert!(reopened.status().durable_records <= 1);
            if reopened.status().durable_records == 1 {
                reopened.append_durable(&first).unwrap();
            }
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut sink = initialized(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = sink.append_durable(&first);
            drop(sink);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = MultiplexedEngineLogicalOutboxSink::open_with_vfs(
                Path::new("/sink"),
                vfs,
                config(1024 * 1024),
            )
            .unwrap();
            assert!(reopened.status().durable_records <= 1);
        }
    }

    #[test]
    fn every_returned_receipt_survives_one_lost_sync() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = initialized(baseline_vfs.clone());
        let first = record(11, 1, b"payload");
        baseline.append_durable(&first).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let vfs = prepared_vfs();
            let mut sink = initialized(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            sink.append_durable(&first).unwrap();
            drop(sink);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut reopened = MultiplexedEngineLogicalOutboxSink::open_with_vfs(
                Path::new("/sink"),
                vfs,
                config(1024 * 1024),
            )
            .unwrap();
            assert_eq!(reopened.status().durable_records, 1);
            reopened.append_durable(&first).unwrap();
        }
    }
}
