//! Explicit native checkpoint target + immutable outbox archive publication.
//! Only archived byte-identical records may use a verified older target prefix.
//! Source fetch/ordered ACK lives in Store; no source writer survives sink I/O.
use super::NativeCheckpointStore;
use crate::{
    logical_outbox::{LogicalOutboxCipher, SealedLogicalOutboxRecord},
    outbox_publisher::{DurableLogicalOutboxReceipt, LogicalOutboxSink},
    outbox_sink::EngineLogicalOutboxSink,
};
use std::io;
use tofu_runtime::checkpoint_journal::Error;

pub struct NativeCheckpointSink {
    target: NativeCheckpointStore,
    archive: EngineLogicalOutboxSink,
    cipher: LogicalOutboxCipher,
    source_uuid: String,
}
fn storage_error(error: Error) -> io::Error {
    let kind = match error {
        Error::Conflict | Error::Ineligible => io::ErrorKind::WouldBlock,
        Error::Invalid => io::ErrorKind::InvalidData,
        Error::Oversize => io::ErrorKind::OutOfMemory,
        Error::Storage | Error::Poisoned => io::ErrorKind::Other,
    };
    io::Error::new(kind, "checkpoint publication refused")
}
impl NativeCheckpointSink {
    pub fn new(
        target: NativeCheckpointStore,
        archive: EngineLogicalOutboxSink,
        cipher: LogicalOutboxCipher,
        source_uuid: String,
    ) -> io::Result<Self> {
        let status = archive.status();
        if target.scope() != (status.tenant_id, status.owner_user_id)
            || uuid::Uuid::parse_str(&source_uuid).is_err()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "checkpoint sink scope differs",
            ));
        }
        Ok(Self {
            target,
            archive,
            cipher,
            source_uuid,
        })
    }
    pub(super) fn scope(&self) -> (u64, u64) {
        self.target.scope()
    }
    pub(super) fn source_uuid(&self) -> &str {
        &self.source_uuid
    }
    pub fn into_parts(self) -> (NativeCheckpointStore, EngineLogicalOutboxSink) {
        (self.target, self.archive)
    }
}
impl LogicalOutboxSink for NativeCheckpointSink {
    fn append_durable(
        &mut self,
        record: &SealedLogicalOutboxRecord,
    ) -> io::Result<DurableLogicalOutboxReceipt> {
        if let Some(proof) = self.archive.prove_record(record)? {
            self.target
                .replay_archived(&self.cipher, &self.source_uuid, &proof)
                .map_err(storage_error)?;
        } else {
            self.target
                .replay_for_archive(
                    &self.cipher,
                    &self.source_uuid,
                    record,
                    self.archive.archive_uuid(),
                )
                .map_err(storage_error)?;
        }
        self.archive.append_durable(record)
    }
    fn is_restart_required(&self) -> bool {
        self.archive.is_restart_required() || self.target.is_restart_required()
    }
}
