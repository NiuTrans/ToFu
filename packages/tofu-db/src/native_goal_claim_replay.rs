//! One authenticated passive copy of the native actor + billing + Goal decision.
//! Uses the active claim transaction, with explicit passive prefix proof in place
//! of local writer death. No replay path grants leases, promotion or tool rights.
use super::*;
use tofu_runtime::checkpoint_journal::{Header, Journal, Scope};

pub(in super::super) const OPERATION: &str = "native_checkpoint.goal_actor_claim";
static MUTATION_SCHEMA: LazyLock<Value> = LazyLock::new(||
    serde_json::from_str(include_str!("../../../contracts/native_goal_claim_outbox_v1.schema.json")).expect("Goal claim outbox contract"));
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Mutation {
    format: String, source_uuid: String, request: Value, context_sha256: String,
    claim: Value, updated_at_ms: u64,
}
pub(super) struct ClaimReplay {
    source_uuid: String, archive_uuid: Option<uuid::Uuid>, mutation_sha256: String,
    pub(super) context_sha256: String, pub(super) claim: Value, pub(super) updated_at_ms: u64,
}
pub(super) fn digest(value: &Value, maximum: usize) -> Result<String, Error> {
    tofu_runtime::checkpoint_goal_codec::digest(value,maximum)
}
fn archive_matches(receipt: &Value, expected: Option<uuid::Uuid>) -> bool {
    match expected {
        Some(id) => receipt["archive_uuid"].as_str() == Some(id.to_string().as_str()),
        None => receipt.get("archive_uuid").is_none(),
    }
}

impl NativeCheckpointStore {
    pub(super) fn capture_goal_claim(&self, database: &AuthorityDatabase, tx: &mut AuthorityTransaction,
        request: &Request, context: &Value, claim: &Value, now: u64, evidence_bytes: usize) -> Result<(), Error> {
        let mutation = Mutation { format: MUTATION_SCHEMA["$id"].as_str().expect("mutation format").into(), source_uuid: database.authority_uuid().to_string(),
            request: serde_json::to_value(request).map_err(|_| Error::Invalid)?,
            context_sha256: digest(context, evidence_bytes)?, claim: claim.clone(), updated_at_ms: now };
        let payload = encode(&mutation, evidence_bytes.min(MAX_LOGICAL_PAYLOAD_BYTES))?;
        database.logical_outbox_capture(tx, LogicalOutboxCapture {
            schema_version: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
            registry_version: crate::generated_storage_operations::STORAGE_REGISTRY_VERSION,
            operation: OPERATION.into(), request_id: uuid::Uuid::new_v4().to_string(),
            request_digest: Sha256::digest(&payload).into(), command_id: None,
            clear_payload: payload, committed_at_ms: now,
        }).map_err(map_io)?;
        Ok(())
    }

    pub(super) fn validate_goal_replay_prefix(&self, database: &AuthorityDatabase,
        tx: &mut AuthorityTransaction, task: &str, version: u64, proof: &ClaimReplay) -> Result<(), Error> {
        let (receipt, _) = self.handoff_document(database, tx, REPLAY, task, self.header_bytes)?
            .ok_or(Error::Conflict)?;
        if receipt["source_uuid"] != proof.source_uuid || receipt["version"].as_u64() != Some(version)
            || !archive_matches(&receipt, proof.archive_uuid) { return Err(Error::Conflict); }
        Ok(())
    }

    pub(super) fn passive_goal_frontier(&self, task: &str, proof: &ClaimReplay) -> Result<Value, Error> {
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        if database.logical_outbox_is_configured() { return Err(Error::Ineligible); }
        let mut tx = database.begin(self.tenant, self.owner).map_err(map_io)?;
        let (header, version) = self.handoff_document(&database, &mut tx, HEADERS, task, self.header_bytes)?
            .ok_or(Error::Conflict)?;
        self.validate_goal_replay_prefix(&database, &mut tx, task, version, proof)?;
        let (snapshot, _) = self.handoff_document(&database, &mut tx, SNAPSHOTS, task, self.snapshot_bytes)?
            .ok_or(Error::Conflict)?;
        let scope = Scope { owner:self.owner, task_id:task.into(), epoch:header["epoch"].as_str().ok_or(Error::Invalid)?.into(),
            binding:header["binding"].as_str().ok_or(Error::Invalid)?.into() };
        // Pure validation touches neither Store nor writer-process observation.
        Journal::new(scope, self, self.snapshot_bytes, self.header_bytes)?.validate_retained_frontier(
            &Header { version, value:header.clone() }, &snapshot)?;
        let frontier = json!({"format":"tofu.native-checkpoint-frontier/v1","owner_user_id":self.owner,
            "task_id":task,"epoch":header["epoch"],"binding":header["binding"],"version":version,
            "lease_generation":header["lease_generation"],"phase":header["phase"],"snapshot":snapshot});
        drop(tx); durable(database, frontier)
    }

    pub(super) fn write_goal_replay_receipt(&self, database: &AuthorityDatabase, tx: &mut AuthorityTransaction,
        task: &str, version: u64, proof: &ClaimReplay) -> Result<(), Error> {
        let mut receipt = json!({"source_uuid":proof.source_uuid,"version":version,"digest":proof.mutation_sha256});
        if let Some(id) = proof.archive_uuid { receipt["archive_uuid"] = json!(id.to_string()); }
        let key = versioned_document::record_key(self.tenant,self.owner,REPLAY,task).map_err(map_io)?;
        versioned_document::put(database,tx,PutRequest { key,namespace:REPLAY.into(),logical_key:task.into(),
            value_json:encode(&receipt,self.header_bytes)?,expected_version:None,updated_at_ms:proof.updated_at_ms }).map_err(map_io)?;
        Ok(())
    }

    fn replayed_goal_claim(&self, request: &Request, request_sha256: &str,
        proof: &ClaimReplay, verified_old: bool, evidence_bytes: usize) -> Result<bool, Error> {
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        if database.logical_outbox_is_configured() { return Err(Error::Ineligible); }
        let mut tx = database.begin_with_identity_claim_scopes(self.tenant,self.owner).map_err(map_io)?;
        let mut remaining = evidence_bytes;
        let Some(event) = goal::event(&database,&mut tx,&request.source_run_id,&request.tenant_id,
            request.source_cursor,&mut remaining).map_err(map_io)? else { drop(tx);return durable(database,false); };
        if event != proof.claim { return Err(Error::Conflict); }
        let claim = &event["native_actor_claim"];
        let actors = claim["actors"].as_array().filter(|rows| !rows.is_empty()).ok_or(Error::Invalid)?;
        if event["type"] != "execution_recovery_claim" || claim["request_sha256"] != request_sha256
            || claim["invoice"] != request.invoice || claim["target_attempt_id"] != request.target_attempt_id
            || actors.len() != request.actors.iter().filter(|actor| actor.checkpoint_version.is_some()).count() {
            return Err(Error::Conflict);
        }
        for actor in actors {
            let task = text(actor,"task_id")?;
            let (header, version) = self.handoff_document(&database,&mut tx,HEADERS,task,self.header_bytes)?.ok_or(Error::Conflict)?;
            let (receipt, _) = self.handoff_document(&database,&mut tx,REPLAY,task,self.header_bytes)?.ok_or(Error::Conflict)?;
            remaining = remaining.checked_sub(encode(&header,remaining)?.len()).ok_or(Error::Oversize)?;
            remaining = remaining.checked_sub(encode(&receipt,remaining)?.len()).ok_or(Error::Oversize)?;
            let claimed_version = actor["version"].as_u64().ok_or(Error::Invalid)?;
            if version < claimed_version || (version > claimed_version && !verified_old)
                || receipt["source_uuid"] != proof.source_uuid || receipt["version"].as_u64() != Some(version)
                || !archive_matches(&receipt,proof.archive_uuid)
                || (version == claimed_version && receipt["digest"] != proof.mutation_sha256)
                || header["phase"] != "terminal" || !header["lease"].is_null()
                || header["goal_actor_claim"] != json!({"source_run_id":request.source_run_id,"source_cursor":request.source_cursor,
                    "root_task_id":request.invoice["task_id"],"target_attempt_id":request.target_attempt_id,"request_sha256":request_sha256}) {
                return Err(Error::Conflict);
            }
        }
        drop(tx);durable(database,true)
    }

    /// Called only after the shared replay entry authenticates scope, schema,
    /// registry, operation and ciphertext digest. The caller pins source UUID.
    pub(in super::super) fn replay_goal_claim_clear(&self, clear: &[u8], source_uuid: &str,
        record: &SealedLogicalOutboxRecord, archive_uuid: Option<uuid::Uuid>, verified_old: bool,
        evidence_bytes: usize) -> Result<u64, Error> {
        if evidence_bytes == 0 || clear.len() > evidence_bytes { return Err(Error::Oversize); }
        let mutation: Mutation = serde_json::from_slice(clear).map_err(|_| Error::Invalid)?;
        if mutation.format != MUTATION_SCHEMA["$id"] || mutation.source_uuid != source_uuid || uuid::Uuid::parse_str(source_uuid).is_err()
            || mutation.updated_at_ms == 0 || mutation.updated_at_ms != record.identity.committed_at_ms {
            return Err(Error::Invalid);
        }
        let (request, request_sha256) = parse_request(&mutation.request,evidence_bytes)?;
        let next = request.source_cursor.checked_add(1).ok_or(Error::Invalid)?;
        let proof = ClaimReplay {source_uuid:source_uuid.into(),archive_uuid,
            mutation_sha256:record.identity.request_digest.iter().map(|byte|format!("{byte:02x}")).collect(),
            context_sha256:mutation.context_sha256,claim:mutation.claim,updated_at_ms:mutation.updated_at_ms};
        if self.replayed_goal_claim(&request,&request_sha256,&proof,verified_old,evidence_bytes)? { return Ok(next); }
        let prepared = self.prepare_goal_claim_with_replay(&request,evidence_bytes,Some(proof))?;
        self.commit_goal_claim(&request,&request_sha256,prepared,evidence_bytes)?;
        Ok(next)
    }
}
