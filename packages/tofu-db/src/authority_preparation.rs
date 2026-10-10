//! Pinned command preparation with private, accounted blob bytes. Publication
//! happens only inside AuthorityDatabase::commit, after entity OCC validation.
//! Dependencies: immutable entity snapshots, blob codec, shared verified reads.
use super::*;
use crate::blob::{BlobStagingIo, ChunkEncoder};
use crate::entity::{EntityBlockIo, SnapshotPin};
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};

pub(super) struct PreparedBlobStore {
    blocks: Arc<crate::block::BlockStore>,
    restart_required: Arc<AtomicBool>,
    encoder: Arc<ChunkEncoder>,
    state: Mutex<PreparedBlobState>,
}

// Existing node/key/Arc allowance plus the identity retained in the payload.
const PREPARED_BLOCK_METADATA_BYTES: usize = 128 + std::mem::size_of::<BlockId>();

struct PreparedBlobState {
    payloads: BTreeMap<BlockId, crate::block::PreparedBlockPayload>,
    memory: SnapshotPin,
}

impl PreparedBlobStore {
    fn check_live(&self) -> io::Result<()> {
        if self.restart_required.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "prepared authority requires close and reopen",
            ));
        }
        Ok(())
    }

    fn reserve_capture(&self, capture: &LogicalOutboxCapture) -> io::Result<()> {
        let bytes = capture
            .clear_payload
            .len()
            .saturating_add(capture.operation.len())
            .saturating_add(capture.request_id.len())
            .saturating_add(capture.command_id.as_ref().map_or(0, String::len))
            .saturating_add(256);
        self.state
            .lock()
            .map_err(|_| io::Error::other("prepared blob store is poisoned"))?
            .memory
            .reserve_transaction_bytes(bytes)
    }

    pub(super) fn publish(&self, engine: &Engine) -> io::Result<()> {
        self.check_live()?;
        let _publish = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_PREPARED_BLOB_PUBLISH);
        let state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("prepared blob store is poisoned"))?;
        for (expected, bytes) in &state.payloads {
            if engine.write_prepared_blob_block(bytes)? != *expected {
                return Err(invalid_data(
                    "prepared blob identity differs at publication",
                ));
            }
        }
        Ok(())
    }
}

impl BlobStagingIo for PreparedBlobStore {
    fn encode_chunk(&self, raw: &[u8]) -> io::Result<Vec<u8>> {
        self.check_live()?;
        self.encoder.encode(raw)
    }

    fn stage_block(&self, encoded: &[u8]) -> io::Result<BlockId> {
        self.check_live()?;
        let source = crate::block::PreparedBlockSource::new(encoded)?;
        let id = source.id();
        let mut state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("prepared blob store is poisoned"))?;
        if !state.payloads.contains_key(&id) {
            state
                .memory
                .reserve_transaction_bytes(encoded.len().saturating_add(PREPARED_BLOCK_METADATA_BYTES))?;
            state.payloads.insert(id, source.capture());
        }
        Ok(id)
    }
}

impl EntityBlockIo for PreparedBlobStore {
    fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.check_live()?;
        if let Some(bytes) = self
            .state
            .lock()
            .map_err(|_| io::Error::other("prepared blob store is poisoned"))?
            .payloads
            .get(&id)
            .map(crate::block::PreparedBlockPayload::shared)
        {
            return Ok(bytes);
        }
        self.blocks.get_shared(id)
    }
    fn write_fresh(&self, _: &[u8]) -> io::Result<BlockId> {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "preparation cannot publish tree pages",
        ))
    }
    fn write_fresh_owned(&self, _: Vec<u8>) -> io::Result<BlockId> {
        self.write_fresh(&[])
    }
}

impl AuthorityDatabase {
    /// Capture under the writer lock; all subsequent logical evaluation can
    /// run independently. Private buffers never become GC-visible or durable
    /// until the writer validates and commits the prepared transaction.
    pub(crate) fn build_preparation_replica(&self) -> io::Result<Self> {
        self.require_write_admission()?;
        let engine = self.entities.writer_engine()?;
        let mut replica = self.build_read_replica()?;
        replica.preparation = Some(Arc::new(PreparedBlobStore {
            blocks: engine.block_store_shared(),
            restart_required: engine.restart_flag(),
            encoder: engine.shared_blob_encoder(),
            state: Mutex::new(PreparedBlobState {
                payloads: BTreeMap::new(),
                memory: replica.entities.preparation_memory_lease()?,
            }),
        }));
        Ok(replica)
    }

    pub(crate) fn is_preparing_transaction(&self) -> bool {
        self.preparation.is_some()
    }

    pub(crate) fn defer_index_update(
        &self,
        transaction: &mut AuthorityTransaction,
        mutation: crate::conversation_header::recency::DeferredMutation,
    ) -> io::Result<()> {
        let preparation = self
            .preparation
            .as_ref()
            .ok_or_else(|| invalid_input("index deferral requires a preparation snapshot"))?;
        preparation
            .state
            .lock()
            .map_err(|_| io::Error::other("prepared blob store is poisoned"))?
            .memory
            .reserve_transaction_bytes(mutation.retained_bytes())?;
        transaction.deferred_indexes.push(mutation);
        Ok(())
    }

    /// Reads of the same index flush pending maintenance against the existing
    /// snapshot (retaining ordinary OCC). Commit flushes after validation and
    /// rebasing, against the sequencer's current head.
    pub(crate) fn flush_deferred_indexes(
        &self,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<()> {
        let mutations = std::mem::take(&mut transaction.deferred_indexes);
        // A batched create may have staged all its documents already. Bring
        // counts/activity coverage current before recency backfill reads them.
        for mutation in &mutations {
            if let crate::conversation_header::recency::DeferredMutation::Created {
                conversation_id, created_at_ms, updated_at_ms,
            } = mutation {
                crate::conversation_header::apply_created_metadata(self, transaction,
                    conversation_id, *created_at_ms, *updated_at_ms)?;
            }
        }
        for mutation in mutations {
            if !matches!(mutation, crate::conversation_header::recency::DeferredMutation::Created { .. }) {
                mutation.apply(self, transaction)?;
            }
        }
        Ok(())
    }

    pub(crate) fn capture_outbox_for_ir(
        &self,
        transaction: &mut AuthorityTransaction,
        capture: LogicalOutboxCapture,
    ) -> io::Result<()> {
        if let Some(preparation) = &self.preparation {
            if transaction.logical_outbox_record.is_some()
                || transaction.deferred_outbox_capture.is_some()
                || transaction.content_free_diagnostic_outbox_exemption
            {
                return Err(invalid_input("invalid deferred logical outbox capture"));
            }
            preparation.reserve_capture(&capture)?;
            transaction.deferred_outbox_capture = Some(capture);
            Ok(())
        } else {
            self.logical_outbox_capture(transaction, capture)
                .map(|_| ())
        }
    }

    pub(super) fn blob_staging_backend(&self) -> io::Result<&dyn BlobStagingIo> {
        match self.preparation.as_ref() {
            Some(preparation) => Ok(preparation.as_ref()),
            None => Ok(self.entities.writer_engine()?),
        }
    }

    pub(super) fn blob_read_backend(&self) -> &dyn EntityBlockIo {
        match self.preparation.as_ref() {
            Some(preparation) => preparation.as_ref(),
            None => self.entities.backend(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

    fn authority() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        db.set_write_overlay_for_test(true);
        (vfs, db)
    }
    fn key() -> EntityKey {
        EntityKey::new(1, 1, "prepared", b"row").unwrap()
    }
    fn prepared(db: &AuthorityDatabase) -> (AuthorityTransaction, BlobReference) {
        let replica = db.build_preparation_replica().unwrap();
        let mut tx = replica.begin(1, 1).unwrap();
        let blob = replica
            .stage_blob(&mut tx, &mut b"private payload".as_slice(), 100)
            .unwrap();
        replica
            .entity_put(&mut tx, key(), b"published".to_vec())
            .unwrap();
        (tx, blob)
    }

    #[test]
    fn preparation_has_no_writes_and_owns_pin_and_memory_until_commit_or_abort() {
        let (vfs, mut db) = authority();
        for commit in [false, true] {
            vfs.arm_fault(None).unwrap();
            let (tx, blob) = prepared(&db);
            assert!(!vfs.trace().unwrap().contains(&Operation::Write));
            let metrics = db.snapshot_pin_metrics().unwrap();
            assert_eq!(
                metrics.active_handles, 1,
                "facade drop must retain the registered snapshot"
            );
            assert!(metrics.retained_transaction_bytes > 0);
            assert!(db.blob_reader(1, 1, blob).is_err());
            if commit {
                db.commit(tx).unwrap();
                assert_eq!(
                    db.blob_reader(1, 1, blob)
                        .unwrap()
                        .next_chunk()
                        .unwrap()
                        .unwrap(),
                    b"private payload"
                );
            } else {
                drop(tx);
            }
            let released = db.snapshot_pin_metrics().unwrap();
            assert_eq!(released.active_handles, 0);
            assert_eq!(released.retained_transaction_bytes, 0);
        }
    }

    #[test]
    fn publication_reuses_every_private_block_allocation_and_reopens_exact_content() {
        let (vfs, mut db) = authority();
        let (transaction, blob) = prepared(&db);
        let allocations: Vec<_> = transaction.prepared_blobs.as_ref().unwrap().state.lock().unwrap()
            .payloads.iter().map(|(id, bytes)| (*id, bytes.shared())).collect();
        assert!(!allocations.is_empty());
        db.commit(transaction).unwrap();
        let blocks = db.block_store_shared();
        for (id, bytes) in allocations {
            let published = blocks.pending_payload(id).unwrap().expect("new private bytes remain WAL-carried");
            assert!(Arc::ptr_eq(&published, &bytes), "publication must retain the captured allocation");
        }
        assert_eq!(db.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
        let durable = Arc::new(vfs.fork_durable().unwrap());
        let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
        assert_eq!(reopened.blob_reader(1, 1, blob).unwrap().next_chunk().unwrap().unwrap(), b"private payload");
    }

    #[test]
    fn conflict_discards_private_blob_without_publication() {
        let (vfs, mut db) = authority();
        let (tx, blob) = prepared(&db);
        let mut other = db.begin(1, 1).unwrap();
        db.entity_put(&mut other, key(), b"winner".to_vec())
            .unwrap();
        db.commit(other).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(db.commit(tx).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert!(!vfs.trace().unwrap().contains(&Operation::Write));
        assert!(db.blob_reader(1, 1, blob).is_err());
        assert_eq!(db.snapshot_pin_metrics().unwrap().active_handles, 0);
    }

    #[test]
    fn prepared_blob_commit_faults_recover_atomic_acknowledged_state() {
        let (vfs, mut db) = authority();
        let (tx, _) = prepared(&db);
        vfs.arm_fault(None).unwrap();
        db.commit(tx).unwrap();
        let trace = vfs.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let (vfs, mut db) = authority();
                let (tx, blob) = prepared(&db);
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = db.commit(tx).is_ok();
                let durable = Arc::new(vfs.fork_durable().unwrap());
                let reopened =
                    AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
                let mut read = reopened.begin(1, 1).unwrap();
                let row = reopened.entity_get(&mut read, &key()).unwrap();
                if acknowledged {
                    assert_eq!(row, Some(b"published".to_vec()));
                }
                if row.is_some() {
                    assert_eq!(
                        reopened
                            .blob_reader(1, 1, blob)
                            .unwrap()
                            .next_chunk()
                            .unwrap()
                            .unwrap(),
                        b"private payload"
                    );
                }
            }
        }
    }

    fn capture(index: u8) -> LogicalOutboxCapture {
        LogicalOutboxCapture {
            schema_version: 1,
            registry_version: 1,
            operation: "record.put".to_owned(),
            request_id: format!("request-{index}"),
            request_digest: [index; 32],
            command_id: Some(format!("command-{index}")),
            committed_at_ms: 1,
            clear_payload: br#"{"value":"synthetic"}"#.to_vec(),
        }
    }

    fn prepared_with_outbox(
        db: &AuthorityDatabase,
        index: u8,
    ) -> (AuthorityTransaction, BlobReference) {
        let replica = db.build_preparation_replica().unwrap();
        let mut tx = replica.begin(1, 1).unwrap();
        let blob = replica
            .stage_blob(&mut tx, &mut b"private payload".as_slice(), 100)
            .unwrap();
        replica
            .entity_put(
                &mut tx,
                EntityKey::new(1, 1, "outbox-business", &[index]).unwrap(),
                b"published".to_vec(),
            )
            .unwrap();
        replica
            .receipt_insert(
                &mut tx,
                &format!("command-{index}"),
                "record.put",
                [index; 32],
                br#"{"ok":true}"#,
                1,
            )
            .unwrap();
        replica
            .capture_outbox_for_ir(&mut tx, capture(index))
            .unwrap();
        (tx, blob)
    }

    #[test]
    fn outbox_sequence_allocation_does_not_conflict_independent_prepared_business_writes() {
        let (_, mut db) = authority();
        db.configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let transactions: Vec<_> = (0..12)
            .map(|index| prepared_with_outbox(&db, index).0)
            .collect();
        for tx in transactions {
            db.commit(tx).unwrap();
        }
        let pending = db.logical_outbox_pending(1, 1, 20).unwrap();
        assert_eq!(pending.len(), 12);
        for (index, item) in pending.iter().enumerate() {
            assert_eq!(item.record.identity.sequence, index as u64 + 1);
            assert_eq!(
                item.record.identity.command_id,
                Some(format!("command-{index}"))
            );
        }
        assert_eq!(db.snapshot_pin_metrics().unwrap().active_handles, 0);
        assert_eq!(
            db.snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
    }

    #[test]
    fn deferred_outbox_validates_business_conflicts_before_allocating_a_sequence() {
        let (_, mut db) = authority();
        db.configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let first = prepared_with_outbox(&db, 1).0;
        let duplicate = prepared_with_outbox(&db, 1).0;
        db.commit(first).unwrap();
        let wal_before = db.wal_len().unwrap();
        assert_eq!(
            db.commit(duplicate).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(db.wal_len().unwrap(), wal_before);
        assert_eq!(db.logical_outbox_pending(1, 1, 20).unwrap().len(), 1);
        let next = prepared_with_outbox(&db, 2).0;
        db.commit(next).unwrap();
        let pending = db.logical_outbox_pending(1, 1, 20).unwrap();
        assert_eq!(pending[1].record.identity.sequence, 2);
    }

    #[test]
    fn deferred_outbox_receipt_blob_and_business_publish_atomically_under_every_io_fault() {
        fn setup() -> (
            Arc<DeterministicVfs>,
            AuthorityDatabase,
            AuthorityTransaction,
            BlobReference,
        ) {
            let (vfs, mut db) = authority();
            db.configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)
                .unwrap();
            let first = prepared_with_outbox(&db, 1).0;
            let (second, blob) = prepared_with_outbox(&db, 2);
            db.commit(first).unwrap();
            (vfs, db, second, blob)
        }
        let (vfs, mut db, tx, _) = setup();
        vfs.arm_fault(None).unwrap();
        db.commit(tx).unwrap();
        let trace = vfs.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let (vfs, mut db, tx, blob) = setup();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = db.commit(tx).is_ok();
                let durable = Arc::new(vfs.fork_durable().unwrap());
                let mut reopened =
                    AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
                reopened
                    .configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)
                    .unwrap();
                let pending = reopened.logical_outbox_pending(1, 1, 20).unwrap();
                assert!(!pending.is_empty(), "previous acknowledgement lost");
                let mut read = reopened.begin(1, 1).unwrap();
                let row = reopened
                    .entity_get(
                        &mut read,
                        &EntityKey::new(1, 1, "outbox-business", &[2]).unwrap(),
                    )
                    .unwrap();
                if acknowledged {
                    assert_eq!(pending.len(), 2);
                }
                assert_eq!(
                    row.is_some(),
                    pending.len() == 2,
                    "business/outbox atomicity"
                );
                let receipt = reopened
                    .receipt_lookup(&mut read, "command-2", "record.put", [2; 32])
                    .unwrap();
                assert_eq!(
                    receipt.is_some(),
                    row.is_some(),
                    "receipt/business atomicity"
                );
                if let Some(receipt) = receipt {
                    assert_eq!(receipt, br#"{"ok":true}"#);
                }
                if row.is_some() {
                    assert_eq!(
                        reopened
                            .blob_reader(1, 1, blob)
                            .unwrap()
                            .next_chunk()
                            .unwrap()
                            .unwrap(),
                        b"private payload"
                    );
                }
            }
        }
    }
}
