//! Atomic acknowledged-frontier handoff on the selected owned authority.
//! Rechecks accepted Turn/Attempt lineage in the transaction, retires source,
//! creates target, reclaims source snapshot and captures one composite outbox.
use super::*;
use crate::authority::AuthorityTransaction;

#[cfg(test)]
#[path = "native_checkpoint_handoff_tests.rs"]
mod tests;

impl NativeCheckpointStore {
    /// Read only acknowledged active-authority debt; passive copies cannot settle.
    pub fn wallet_invoice(&self, owner: u64, task: &str) -> Result<Option<Value>, Error> {
        self.task(owner, task)?;
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        let mut transaction = database.begin(self.tenant, self.owner).map_err(map_io)?;
        if self.handoff_document(&database, &mut transaction, REPLAY, task, self.header_bytes)?.is_some() {
            return Err(Error::Ineligible);
        }
        let state = self.handoff_document(&database, &mut transaction, HEADERS, task, self.header_bytes)?
            .map(|(header, _)| {
                tofu_runtime::checkpoint_journal::wallet_invoice_view(owner, task, &header)
            }).transpose()?;
        drop(transaction);
        durable(database, state)
    }

    pub(super) fn handoff_document(&self, database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
        namespace: &str, task: &str, maximum: usize) -> Result<Option<(Value, u64)>, Error> {
        let key = versioned_document::record_key(self.tenant, self.owner, namespace, task).map_err(map_io)?;
        versioned_document::get_value_with_version_bounded(database, transaction, &key, namespace, task, maximum)
            .map_err(map_io)?.map(|(bytes, version)|
                serde_json::from_slice(&bytes).map(|value| (value, version)).map_err(|_| Error::Invalid)).transpose()
    }

    pub(super) fn retire_handoff_source(&self, database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction, source: &RetiredSource, phase: &str,
        prove_writer_death: bool, now: u64) -> Result<(), Error> {
        self.task(self.owner, &source.task_id)?;
        let retirement = tofu_runtime::checkpoint_lineage::Retirement::new(self.owner, &source.task_id,
            source.expected_version, &source.header, phase, prove_writer_death)?;
        let (actual, version) = self.handoff_document(database, transaction, HEADERS,
            &source.task_id, self.header_bytes)?.ok_or(Error::Conflict)?;
        let observed = tofu_runtime::checkpoint_journal::Header {version, value:actual};
        retirement.validate_header(&observed)?;
        let snapshot = if phase == "tools_pending" {
            self.handoff_document(database, transaction, SNAPSHOTS, &source.task_id, self.snapshot_bytes)?
                .map(|(value, _)| value)
        } else { None };
        retirement.validate_snapshot(&observed, snapshot.as_ref())?;
        let key = versioned_document::record_key(self.tenant, self.owner, HEADERS, &source.task_id).map_err(map_io)?;
        versioned_document::put_known_version_returning_envelope(database, transaction, PutRequest {
            key, namespace: HEADERS.into(), logical_key: source.task_id.clone(),
            value_json: encode(&source.header, self.header_bytes)?, expected_version: Some(version), updated_at_ms: now,
        }, version).map_err(map_io)?;
        // A terminal source cannot load this reconstructible journal snapshot.
        // Durable conversation, model continuation and task result stay intact.
        let key = versioned_document::record_key(self.tenant, self.owner, SNAPSHOTS, &source.task_id).map_err(map_io)?;
        versioned_document::delete(database, transaction, key, SNAPSHOTS, &source.task_id, None).map_err(map_io)?;
        Ok(())
    }

    pub(super) fn handoff_atomic(&self, request: Handoff<'_>) -> Result<u64, Error> {
        let target = request.target_header["task_id"].as_str().ok_or(Error::Invalid)?;
        self.task(self.owner, target)?;
        let lineage = request.lineage;
        self.task(self.owner, &lineage.source_task_id)?;
        tofu_runtime::checkpoint_lineage::validate_resume_target(self.owner, &request)?;
        let mut database = self.authority.lock().map_err(|_| Error::Storage)?;
        // Attempt lookup uses the tenant-global identity locator, then checks
        // its owner before reading the owned document. Writes stay owner-scoped.
        let mut transaction = database.begin_with_identity_claim_scopes(self.tenant, self.owner).map_err(map_io)?;
        let read_attempt = |transaction: &mut AuthorityTransaction, id: &str, config: bool| -> Result<Value, Error> {
            let bytes = crate::turn::attempt_get_with_config(&database, transaction, id, config)
                .map_err(map_io)?.ok_or(Error::Ineligible)?;
            serde_json::from_slice(&bytes).map_err(|_| Error::Invalid)
        };
        let accepted = read_attempt(&mut transaction, &lineage.target_attempt_id, true)?;
        let source_attempt = read_attempt(&mut transaction, &lineage.source_attempt_id, false)?;
        let turn_bytes = crate::turn::get(&database, &mut transaction, &lineage.conversation_id,
            &lineage.turn_id).map_err(map_io)?.ok_or(Error::Ineligible)?;
        let turn: Value = serde_json::from_slice(&turn_bytes).map_err(|_| Error::Invalid)?;
        tofu_runtime::checkpoint_lineage::validate_resume_lineage(lineage, target, &accepted, &source_attempt, &turn)?;
        for task in [lineage.source_task_id.as_str(), target] {
            if self.handoff_document(&database, &mut transaction, REPLAY, task, self.header_bytes)?.is_some() {
                return Err(Error::Ineligible);
            }
        }
        if self.handoff_document(&database, &mut transaction, HEADERS, target, self.header_bytes)?.is_some()
            || self.handoff_document(&database, &mut transaction, SNAPSHOTS, target, self.snapshot_bytes)?.is_some()
        { return Err(Error::Conflict); }
        let now = u64::try_from(SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| Error::Storage)?
            .as_millis()).map_err(|_| Error::Storage)?.max(1);
        let source = RetiredSource { task_id: lineage.source_task_id.clone(), expected_version: request.source_version,
            header: request.source_header.clone() };
        self.retire_handoff_source(&database, &mut transaction, &source,
            request.target_header["phase"].as_str().ok_or(Error::Invalid)?, true, now)?;
        for (namespace, value, maximum) in [(HEADERS, request.target_header, self.header_bytes),
            (SNAPSHOTS, request.target_snapshot, self.snapshot_bytes)] {
            let key = versioned_document::record_key(self.tenant, self.owner, namespace, target).map_err(map_io)?;
            versioned_document::put(&database, &mut transaction, PutRequest {
                key, namespace: namespace.into(), logical_key: target.into(), value_json: encode(value, maximum)?,
                expected_version: Some(0), updated_at_ms: now,
            }).map_err(map_io)?;
        }
        if database.logical_outbox_is_configured() {
            let mutation = Mutation { format: FORMAT, source_uuid: database.authority_uuid().to_string(),
                owner_user_id: self.owner, task_id: target, expected_version: 0, header: request.target_header,
                snapshot: Some(request.target_snapshot), handoff_source: Some(&source), updated_at_ms: now };
            let payload = encode(&mutation, MAX_LOGICAL_PAYLOAD_BYTES)?;
            database.logical_outbox_capture(&mut transaction, LogicalOutboxCapture {
                schema_version: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
                registry_version: crate::generated_storage_operations::STORAGE_REGISTRY_VERSION,
                operation: OPERATION.into(), request_id: uuid::Uuid::new_v4().to_string(),
                request_digest: Sha256::digest(&payload).into(), command_id: None, clear_payload: payload, committed_at_ms: now,
            }).map_err(map_io)?;
        }
        database.commit(transaction).map_err(map_io)?;
        durable(database, 1)
    }
}
