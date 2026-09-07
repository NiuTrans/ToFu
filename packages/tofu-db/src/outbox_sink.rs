//! Durable, owner-bound logical-outbox sink built on the certified engine log.
//!
//! One explicit sink directory belongs to one tenant/owner stream. Each sealed
//! record is the payload of exactly one Engine transaction, so CONTROL/WAL
//! durability, bounded recovery, checkpoint rotation, and process leasing are
//! reused without creating a second filesystem protocol. Exact-sequence lookup
//! makes retries idempotent while reading at most one 4 MiB history segment.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::engine::Engine;
use crate::logical_outbox::{SealedLogicalOutboxRecord, MAX_ENCODED_LOGICAL_OUTBOX_BYTES};
use crate::outbox_publisher::{DurableLogicalOutboxReceipt, LogicalOutboxSink};
use crate::vfs::Vfs;
use crate::ACTIVE_LOG_MAX_BYTES;

const MAGIC: &[u8; 8] = b"TDBSNK01";
const VERSION: u32 = 1;
const HEADER_BYTES: usize = 8 + 4 + 8 + 8 + 8 + 32 + 8 + 4;
pub(crate) const MIN_FREE_RESERVE_BYTES: u64 = 2 * ACTIVE_LOG_MAX_BYTES;
pub const MAX_SINK_LOGICAL_BYTES: u64 = 1024 * 1024 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EngineLogicalOutboxSinkStatus {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub durable_sequence: u64,
    pub logical_bytes: u64,
}

struct SinkEntry {
    tenant_id: u64,
    owner_user_id: u64,
    sequence: u64,
    event_id: [u8; 32],
    cumulative_bytes: u64,
    record_encoded: Vec<u8>,
}

pub struct EngineLogicalOutboxSink {
    engine: Engine,
    tenant_id: u64,
    owner_user_id: u64,
    logical_bytes: u64,
    max_logical_bytes: u64,
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

fn validate_configuration(
    tenant_id: u64,
    owner_user_id: u64,
    max_logical_bytes: u64,
) -> io::Result<()> {
    if tenant_id == 0
        || owner_user_id == 0
        || max_logical_bytes < HEADER_BYTES as u64 + 1
        || max_logical_bytes > MAX_SINK_LOGICAL_BYTES
    {
        return Err(invalid_input("invalid logical outbox sink configuration"));
    }
    Ok(())
}

pub(crate) fn preflight_real_capacity(path: &Path, max_logical_bytes: u64) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(invalid_input("logical outbox sink path must be absolute"));
    }
    let probe = if path.exists() {
        path
    } else {
        path.parent()
            .ok_or_else(|| invalid_input("logical outbox sink has no parent volume"))?
    };
    assess_capacity(max_logical_bytes, fs2::available_space(probe)?)?;
    Ok(())
}

pub(crate) fn assess_capacity(max_logical_bytes: u64, available_bytes: u64) -> io::Result<u64> {
    let required = max_logical_bytes
        .checked_add(MIN_FREE_RESERVE_BYTES)
        .ok_or_else(|| invalid_input("logical outbox sink capacity overflow"))?;
    if available_bytes < required {
        return Err(storage_full(
            "logical outbox sink volume lacks configured capacity and recovery reserve",
        ));
    }
    Ok(required)
}

impl SinkEntry {
    fn encoded_len(record_bytes: usize) -> io::Result<usize> {
        HEADER_BYTES
            .checked_add(record_bytes)
            .filter(|length| *length <= HEADER_BYTES + MAX_ENCODED_LOGICAL_OUTBOX_BYTES)
            .ok_or_else(|| invalid_input("logical outbox sink entry length overflow"))
    }

    fn encode(&self) -> io::Result<Vec<u8>> {
        let record = SealedLogicalOutboxRecord::decode(&self.record_encoded)?;
        if self.tenant_id == 0
            || self.owner_user_id == 0
            || self.sequence == 0
            || self.cumulative_bytes == 0
            || record.identity.tenant_id != self.tenant_id
            || record.identity.owner_user_id != self.owner_user_id
            || record.identity.sequence != self.sequence
            || record.event_id != self.event_id
        {
            return Err(invalid_input("logical outbox sink entry identity differs"));
        }
        let mut encoded = Vec::with_capacity(Self::encoded_len(self.record_encoded.len())?);
        encoded.extend_from_slice(MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.tenant_id.to_le_bytes());
        encoded.extend_from_slice(&self.owner_user_id.to_le_bytes());
        encoded.extend_from_slice(&self.sequence.to_le_bytes());
        encoded.extend_from_slice(&self.event_id);
        encoded.extend_from_slice(&self.cumulative_bytes.to_le_bytes());
        encoded.extend_from_slice(&(self.record_encoded.len() as u32).to_le_bytes());
        encoded.extend_from_slice(&self.record_encoded);
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < HEADER_BYTES || !encoded.starts_with(MAGIC) {
            return Err(invalid_data("invalid logical outbox sink entry"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let tenant_id = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
        let owner_user_id = u64::from_le_bytes(encoded[20..28].try_into().unwrap());
        let sequence = u64::from_le_bytes(encoded[28..36].try_into().unwrap());
        let event_id = encoded[36..68].try_into().unwrap();
        let cumulative_bytes = u64::from_le_bytes(encoded[68..76].try_into().unwrap());
        let record_bytes = u32::from_le_bytes(encoded[76..80].try_into().unwrap()) as usize;
        if version != VERSION
            || tenant_id == 0
            || owner_user_id == 0
            || sequence == 0
            || cumulative_bytes == 0
            || Self::encoded_len(record_bytes)
                .map_err(|_| invalid_data("invalid logical outbox sink entry length"))?
                != encoded.len()
            || cumulative_bytes < encoded.len() as u64
        {
            return Err(invalid_data("invalid logical outbox sink entry header"));
        }
        let record_encoded = encoded[HEADER_BYTES..].to_vec();
        let record = SealedLogicalOutboxRecord::decode(&record_encoded)?;
        if record.identity.tenant_id != tenant_id
            || record.identity.owner_user_id != owner_user_id
            || record.identity.sequence != sequence
            || record.event_id != event_id
        {
            return Err(invalid_data("logical outbox sink record identity differs"));
        }
        Ok(Self {
            tenant_id,
            owner_user_id,
            sequence,
            event_id,
            cumulative_bytes,
            record_encoded,
        })
    }
}

impl EngineLogicalOutboxSink {
    pub fn initialize(
        data_dir: &Path,
        tenant_id: u64,
        owner_user_id: u64,
        max_logical_bytes: u64,
    ) -> io::Result<Self> {
        validate_configuration(tenant_id, owner_user_id, max_logical_bytes)?;
        preflight_real_capacity(data_dir, max_logical_bytes)?;
        let engine = Engine::initialize(data_dir)?;
        Ok(Self {
            engine,
            tenant_id,
            owner_user_id,
            logical_bytes: 0,
            max_logical_bytes,
            real_capacity_path: Some(data_dir.to_owned()),
        })
    }

    pub fn open(
        data_dir: &Path,
        tenant_id: u64,
        owner_user_id: u64,
        max_logical_bytes: u64,
    ) -> io::Result<Self> {
        validate_configuration(tenant_id, owner_user_id, max_logical_bytes)?;
        if !data_dir.is_absolute() {
            return Err(invalid_input("logical outbox sink path must be absolute"));
        }
        let sink = Self::from_engine(
            Engine::open(data_dir)?,
            tenant_id,
            owner_user_id,
            max_logical_bytes,
            Some(data_dir.to_owned()),
        )?;
        let remaining_bytes = sink
            .max_logical_bytes
            .checked_sub(sink.logical_bytes)
            .ok_or_else(|| invalid_data("logical outbox sink exceeds configured capacity"))?;
        assess_capacity(remaining_bytes, fs2::available_space(data_dir)?)?;
        Ok(sink)
    }

    pub fn initialize_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        tenant_id: u64,
        owner_user_id: u64,
        max_logical_bytes: u64,
    ) -> io::Result<Self> {
        validate_configuration(tenant_id, owner_user_id, max_logical_bytes)?;
        let engine = Engine::initialize_with_vfs(data_dir, vfs)?;
        Ok(Self {
            engine,
            tenant_id,
            owner_user_id,
            logical_bytes: 0,
            max_logical_bytes,
            real_capacity_path: None,
        })
    }

    pub fn open_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        tenant_id: u64,
        owner_user_id: u64,
        max_logical_bytes: u64,
    ) -> io::Result<Self> {
        validate_configuration(tenant_id, owner_user_id, max_logical_bytes)?;
        Self::from_engine(
            Engine::open_with_vfs(data_dir, vfs)?,
            tenant_id,
            owner_user_id,
            max_logical_bytes,
            None,
        )
    }

    fn from_engine(
        engine: Engine,
        tenant_id: u64,
        owner_user_id: u64,
        max_logical_bytes: u64,
        real_capacity_path: Option<PathBuf>,
    ) -> io::Result<Self> {
        let logical_bytes = match engine.transaction_at(engine.state().durable_sequence)? {
            None => 0,
            Some(transaction) => {
                if !transaction.envelope.block_ids.is_empty() {
                    return Err(invalid_data(
                        "logical outbox sink transaction references blocks",
                    ));
                }
                let entry = SinkEntry::decode(&transaction.envelope.inline_payload)?;
                if entry.tenant_id != tenant_id
                    || entry.owner_user_id != owner_user_id
                    || entry.sequence != transaction.sequence
                    || entry.cumulative_bytes > max_logical_bytes
                {
                    return Err(invalid_data(
                        "logical outbox sink scope or capacity differs",
                    ));
                }
                entry.cumulative_bytes
            }
        };
        Ok(Self {
            engine,
            tenant_id,
            owner_user_id,
            logical_bytes,
            max_logical_bytes,
            real_capacity_path,
        })
    }

    pub fn status(&self) -> EngineLogicalOutboxSinkStatus {
        EngineLogicalOutboxSinkStatus {
            tenant_id: self.tenant_id,
            owner_user_id: self.owner_user_id,
            durable_sequence: self.engine.state().durable_sequence,
            logical_bytes: self.logical_bytes,
        }
    }

    pub fn checkpoint(&mut self) -> io::Result<u64> {
        self.engine.checkpoint()
    }

    fn receipt(record: &SealedLogicalOutboxRecord) -> DurableLogicalOutboxReceipt {
        DurableLogicalOutboxReceipt {
            tenant_id: record.identity.tenant_id,
            owner_user_id: record.identity.owner_user_id,
            sequence: record.identity.sequence,
            event_id: record.event_id,
        }
    }

    fn verify_existing(&self, record: &SealedLogicalOutboxRecord) -> io::Result<()> {
        let transaction = self
            .engine
            .transaction_at(record.identity.sequence)?
            .ok_or_else(|| invalid_data("logical outbox sink sequence is missing"))?;
        if !transaction.envelope.block_ids.is_empty() {
            return Err(invalid_data(
                "logical outbox sink transaction references blocks",
            ));
        }
        let entry = SinkEntry::decode(&transaction.envelope.inline_payload)?;
        if entry.record_encoded != record.encode()? {
            return Err(invalid_data(
                "logical outbox sink sequence contains another record",
            ));
        }
        Ok(())
    }
}

impl LogicalOutboxSink for EngineLogicalOutboxSink {
    fn append_durable(
        &mut self,
        record: &SealedLogicalOutboxRecord,
    ) -> io::Result<DurableLogicalOutboxReceipt> {
        record.validate()?;
        if record.identity.tenant_id != self.tenant_id
            || record.identity.owner_user_id != self.owner_user_id
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "logical outbox record is outside the sink owner scope",
            ));
        }
        let next_sequence = self.engine.next_sequence()?;
        if record.identity.sequence < next_sequence {
            self.verify_existing(record)?;
            return Ok(Self::receipt(record));
        }
        if record.identity.sequence != next_sequence {
            return Err(invalid_data("logical outbox sink sequence has a gap"));
        }
        let record_encoded = record.encode()?;
        let entry_bytes = SinkEntry::encoded_len(record_encoded.len())? as u64;
        let next_logical_bytes = self
            .logical_bytes
            .checked_add(entry_bytes)
            .filter(|bytes| *bytes <= self.max_logical_bytes)
            .ok_or_else(|| storage_full("logical outbox sink reached its byte ceiling"))?;
        if let Some(path) = &self.real_capacity_path {
            let required = entry_bytes
                .checked_add(MIN_FREE_RESERVE_BYTES)
                .ok_or_else(|| invalid_input("logical outbox sink capacity overflow"))?;
            if fs2::available_space(path)? < required {
                return Err(storage_full(
                    "logical outbox sink volume lacks append and recovery headroom",
                ));
            }
        }
        let entry = SinkEntry {
            tenant_id: self.tenant_id,
            owner_user_id: self.owner_user_id,
            sequence: record.identity.sequence,
            event_id: record.event_id,
            cumulative_bytes: next_logical_bytes,
            record_encoded,
        }
        .encode()?;
        let committed_sequence = self.engine.commit(&entry)?;
        if committed_sequence != record.identity.sequence {
            return Err(invalid_data(
                "logical outbox sink committed another sequence",
            ));
        }
        self.logical_bytes = next_logical_bytes;
        Ok(Self::receipt(record))
    }

    fn is_restart_required(&self) -> bool {
        self.engine.is_restart_required()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logical_outbox::{LogicalOutboxCipher, LogicalOutboxIdentity};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    fn record(sequence: u64, payload: &[u8]) -> SealedLogicalOutboxRecord {
        LogicalOutboxCipher::new(&[51; 32])
            .seal(
                LogicalOutboxIdentity {
                    sequence,
                    tenant_id: 7,
                    owner_user_id: 11,
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: format!("request-{sequence}"),
                    request_digest: [sequence as u8; 32],
                    command_id: Some(format!("command-{sequence}")),
                    committed_at_ms: 5_000 + sequence,
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

    fn initialized(vfs: Arc<DeterministicVfs>) -> EngineLogicalOutboxSink {
        let sink = EngineLogicalOutboxSink::initialize_with_vfs(
            Path::new("/sink"),
            vfs.clone(),
            7,
            11,
            1024 * 1024,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        sink
    }

    #[test]
    fn real_sink_reopens_and_exact_retries_are_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let sink_path = directory.path().join("sink");
        let first = record(1, b"first");
        let second = record(2, b"second");
        let status;
        {
            let mut sink =
                EngineLogicalOutboxSink::initialize(&sink_path, 7, 11, 1024 * 1024).unwrap();
            sink.append_durable(&first).unwrap();
            sink.append_durable(&second).unwrap();
            sink.checkpoint().unwrap();
            assert_eq!(
                sink.append_durable(&first).unwrap(),
                EngineLogicalOutboxSink::receipt(&first)
            );
            status = sink.status();
        }
        let mut reopened = EngineLogicalOutboxSink::open(&sink_path, 7, 11, 1024 * 1024).unwrap();
        assert_eq!(reopened.status(), status);
        assert_eq!(
            reopened.append_durable(&second).unwrap(),
            EngineLogicalOutboxSink::receipt(&second)
        );
        assert_eq!(
            reopened
                .append_durable(&record(1, b"different"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        drop(reopened);
        let wrong_scope = match EngineLogicalOutboxSink::open(&sink_path, 7, 12, 1024 * 1024) {
            Ok(_) => panic!("sink accepted a different owner scope"),
            Err(error) => error,
        };
        assert_eq!(wrong_scope.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn sink_rejects_gaps_cross_owner_records_and_capacity_overflow() {
        let vfs = prepared_vfs();
        let mut sink = initialized(vfs.clone());
        assert_eq!(
            sink.append_durable(&record(2, b"gap")).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        let mut cross_owner = record(1, b"wrong owner");
        cross_owner.identity.owner_user_id = 12;
        assert_eq!(
            sink.append_durable(&cross_owner).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        let first = record(1, b"first");
        sink.append_durable(&first).unwrap();
        let exact_capacity = sink.status().logical_bytes;
        drop(sink);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened =
            EngineLogicalOutboxSink::open_with_vfs(Path::new("/sink"), vfs, 7, 11, exact_capacity)
                .unwrap();
        assert_eq!(
            reopened
                .append_durable(&record(2, b"second"))
                .unwrap_err()
                .kind(),
            io::ErrorKind::StorageFull
        );
    }

    #[test]
    fn capacity_preflight_reserves_two_active_log_windows() {
        let configured = 1024 * 1024;
        let required = configured + MIN_FREE_RESERVE_BYTES;
        assert_eq!(assess_capacity(configured, required).unwrap(), required);
        assert_eq!(
            assess_capacity(configured, required - 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::StorageFull
        );
    }

    #[test]
    fn every_append_fault_recovers_an_empty_or_complete_sink_prefix() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = initialized(baseline_vfs.clone());
        let first = record(1, b"payload");
        baseline.append_durable(&first).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for error_kind in [io::ErrorKind::Interrupted, io::ErrorKind::StorageFull] {
            for operation_number in 1..=trace.len() as u64 {
                let vfs = prepared_vfs();
                let mut sink = initialized(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number,
                    action: FaultAction::ErrorBefore(error_kind),
                }))
                .unwrap();
                let _ = sink.append_durable(&first);
                drop(sink);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let reopened = EngineLogicalOutboxSink::open_with_vfs(
                    Path::new("/sink"),
                    vfs,
                    7,
                    11,
                    1024 * 1024,
                )
                .unwrap();
                assert!(reopened.status().durable_sequence <= 1);
                if reopened.status().durable_sequence == 1 {
                    reopened.verify_existing(&first).unwrap();
                }
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
            let reopened =
                EngineLogicalOutboxSink::open_with_vfs(Path::new("/sink"), vfs, 7, 11, 1024 * 1024)
                    .unwrap();
            assert!(reopened.status().durable_sequence <= 1);
            if reopened.status().durable_sequence == 1 {
                reopened.verify_existing(&first).unwrap();
            }
        }
    }

    #[test]
    fn every_lost_append_sync_preserves_a_returned_durable_receipt() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = initialized(baseline_vfs.clone());
        let first = record(1, b"payload");
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
            let reopened =
                EngineLogicalOutboxSink::open_with_vfs(Path::new("/sink"), vfs, 7, 11, 1024 * 1024)
                    .unwrap();
            assert_eq!(reopened.status().durable_sequence, 1);
            reopened.verify_existing(&first).unwrap();
        }
    }
}
