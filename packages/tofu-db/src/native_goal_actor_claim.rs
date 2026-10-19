//! Atomic whole-Goal actor retirement on the selected native authority.
//! Preparation uses immutable Goal witnesses, registered completions and native
//! dead-writer observations. Commit rechecks every version, accepted ancestry and
//! writer death, settles original debt, retains snapshots and appends one claim.
//! This internal port grants no live capability or permission to replay a tool.
use super::*;
use crate::{authority::AuthorityTransaction, orchestration::goal_claim as goal};
use serde_json::json;
use std::collections::BTreeMap;
use std::sync::LazyLock;
use tofu_runtime::checkpoint_journal::inspect_abandoned_frontier;
#[cfg(test)]
use tofu_runtime::checkpoint_journal::{actor_fee_evidence_from_checkpoint,validate_actor_fee_evidence,validate_aggregate_actor_invoice};
#[path = "native_goal_claim_replay.rs"]
mod replay;
pub(super) use replay::OPERATION as GOAL_CLAIM_OPERATION;
use replay::ClaimReplay;
#[cfg(test)]
use tofu_runtime::checkpoint_goal_policy as fee_account;
#[path = "native_goal_actor_handoff.rs"]
mod consumption;
#[cfg(test)]
#[path = "native_goal_preparation.rs"]
mod preparation;
pub(super) use consumption::OPERATION as GOAL_HANDOFF_OPERATION;
use tofu_runtime::checkpoint_goal_claim::{self as shared_claim,Request,parse_request};
#[path="native_goal_read.rs"]
mod read;
use read::Reads;
struct Prepared {
    context: Value, records: BTreeMap<String, u64>, pending: BTreeMap<String, Value>,
    replay: Option<ClaimReplay>,
}
#[cfg(all(test, target_os = "linux"))]
#[path = "native_goal_actor_claim_tests.rs"]
mod tests;
fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, Error> {
    value[key].as_str().filter(|value| !value.is_empty() && value.chars().count() <= 512).ok_or(Error::Invalid)
}
fn decode(bytes: &[u8]) -> Result<Value, Error> { serde_json::from_slice(bytes).map_err(|_| Error::Invalid) }


impl NativeCheckpointStore {
    fn goal_context(&self, database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
        request: &Request, remaining: &mut usize) -> Result<Value, Error> {
        goal::context(database, transaction, &request.source_run_id, &request.target_attempt_id,
            &request.tenant_id, request.source_cursor, request.launch_sequence,
            request.account_sequence, remaining).map_err(map_io)
    }

    fn prepare_goal_claim(&self, request: &Request, evidence_bytes: usize) -> Result<Prepared, Error> {
        self.prepare_goal_claim_with_replay(request, evidence_bytes, None)
    }
    fn prepare_goal_claim_with_replay(&self, request: &Request, evidence_bytes: usize,
        replay: Option<ClaimReplay>) -> Result<Prepared, Error> {
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        if replay.is_some() && database.logical_outbox_is_configured() { return Err(Error::Ineligible); }
        let mut transaction = database.begin_with_identity_claim_scopes(self.tenant, self.owner).map_err(map_io)?;
        let mut remaining = evidence_bytes;
        let context = self.goal_context(&database, &mut transaction, request, &mut remaining)?;
        if let Some(proof) = &replay {
            if replay::digest(&context, evidence_bytes)? != proof.context_sha256 { return Err(Error::Conflict); }
        }
        let mut prepared=shared_claim::prepare_records(&mut Reads{store:self,
            reads:goal::Reads{database:&database,transaction:&mut transaction}},request,context,remaining)?;
        drop(transaction);drop(database);
        shared_claim::collect_frontiers(&mut prepared,request,self.owner,|task| {
            if let Some(proof)=&replay {self.passive_goal_frontier(task,proof)} else {
                inspect_abandoned_frontier(self,self.owner,task,self.snapshot_bytes,self.header_bytes)?
                    .ok_or(Error::Ineligible)
            }
        })?;
        shared_claim::finish(&prepared,request)?;
        let shared_claim::Prepared{context,records,pending,..}=prepared;
        Ok(Prepared { context, records, pending, replay })
    }

    /// Internal explicit composition only. No C ABI/dispatch admission is exposed
    /// until claim-consuming actor leases and application stream routing exist.
    pub fn claim_goal_actors(&self, payload: &Value, evidence_bytes: usize) -> Result<Value, Error> {
        let (request, digest) = parse_request(payload, evidence_bytes)?;
        if let Some(claim) = self.existing_goal_claim(&request, &digest, evidence_bytes)? { return Ok(claim); }
        let prepared = self.prepare_goal_claim(&request, evidence_bytes)?;
        self.commit_goal_claim(&request, &digest, prepared, evidence_bytes)
    }

    fn commit_goal_claim(&self, request: &Request, digest: &str, prepared: Prepared,
        evidence_bytes: usize) -> Result<Value, Error> {
        let mut database = self.authority.lock().map_err(|_| Error::Storage)?;
        if prepared.replay.is_some() && database.logical_outbox_is_configured() { return Err(Error::Ineligible); }
        let mut transaction = database.begin_with_identity_claim_scopes(self.tenant, self.owner).map_err(map_io)?;
        let mut remaining = evidence_bytes;
        if self.goal_context(&database, &mut transaction, &request, &mut remaining)? != prepared.context {
            return Err(Error::Conflict);
        }
        for (task, version) in &prepared.records {
            if crate::task_result::owned_record_version(&database, &mut transaction, task).map_err(map_io)? != Some(*version) {
                return Err(Error::Conflict);
            }
        }
        let now = if let Some(proof) = &prepared.replay { proof.updated_at_ms } else {
            u64::try_from(SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| Error::Storage)?
                .as_millis()).map_err(|_| Error::Storage)?.max(1)
        };
        let mut actors = Vec::new();
        for (task, observed) in &prepared.pending {
            if let Some(proof) = &prepared.replay {
                self.validate_goal_replay_prefix(&database, &mut transaction, task,
                    observed["version"].as_u64().ok_or(Error::Invalid)?, proof)?;
            } else if self.handoff_document(&database, &mut transaction, REPLAY, task, self.header_bytes)?.is_some() {
                return Err(Error::Ineligible);
            }
            let (mut header, version) = self.handoff_document(&database, &mut transaction, HEADERS, task, self.header_bytes)?
                .ok_or(Error::Conflict)?;
            if prepared.replay.is_none() && !crate::native_checkpoint_store::writer::quiescent(&header["writer_process"])? {
                return Err(Error::Ineligible);
            }
            header=shared_claim::retire_header(self.owner,task,&tofu_runtime::checkpoint_journal::Header{value:header,version},observed,request,digest)?;
            let key = versioned_document::record_key(self.tenant, self.owner, HEADERS, task).map_err(map_io)?;
            versioned_document::put_known_version_returning_envelope(&database, &mut transaction, PutRequest {
                key, namespace:HEADERS.into(), logical_key:task.clone(), value_json:encode(&header,self.header_bytes)?,
                expected_version:Some(version), updated_at_ms:now,
            }, version).map_err(map_io)?;
            if let Some(proof) = &prepared.replay {
                self.write_goal_replay_receipt(&database, &mut transaction, task,
                    version.checked_add(1).ok_or(Error::Invalid)?, proof)?;
            }
            // Keep the exact paid snapshot until a later accepted claim consumer
            // atomically installs its successor; retirement is never completion.
            actors.push(json!({"task_id":task,"source_version":version,"version":version.checked_add(1).ok_or(Error::Invalid)?}));
        }
        self.settle_goal_claim(&database, &mut transaction, &request.invoice, now)?;
        let claim=shared_claim::receipt(request,digest,&prepared.context,actors)?;
        if prepared.replay.as_ref().is_some_and(|proof| proof.claim != claim) { return Err(Error::Conflict); }
        goal::append_claim(&database, &mut transaction, &request.source_run_id, &request.tenant_id,
            request.source_cursor, claim.clone(), now).map_err(map_io)?;
        if database.logical_outbox_is_configured() {
            self.capture_goal_claim(&database, &mut transaction, request, &prepared.context, &claim, now, evidence_bytes)?;
        }
        database.commit(transaction).map_err(map_io)?;
        durable(database, claim)
    }

    fn existing_goal_claim(&self, request: &Request, digest: &str, evidence_bytes: usize) -> Result<Option<Value>, Error> {
        let database = self.authority.lock().map_err(|_| Error::Storage)?;
        let mut transaction = database.begin_with_identity_claim_scopes(self.tenant, self.owner).map_err(map_io)?;
        let mut remaining = evidence_bytes;
        let event = goal::event(&database, &mut transaction, &request.source_run_id, &request.tenant_id,
            request.source_cursor, &mut remaining).map_err(map_io)?;
        let result = match event {
            None => None,
            Some(event) if event["type"] == "execution_recovery_claim"
                && event["native_actor_claim"]["request_sha256"] == digest => {
                let actors=shared_claim::existing_actors(&event,request,digest)?;
                for actor in actors {
                    let task = text(actor,"task_id")?;
                    if self.handoff_document(&database,&mut transaction,REPLAY,task,self.header_bytes)?.is_some() {
                        return Err(Error::Ineligible);
                    }
                    let (header,version) = self.handoff_document(&database,&mut transaction,HEADERS,task,self.header_bytes)?
                        .ok_or(Error::Conflict)?;
                    remaining = remaining.checked_sub(encode(&header, remaining)?.len()).ok_or(Error::Oversize)?;
                    shared_claim::validate_existing_header(&tofu_runtime::checkpoint_journal::Header{value:header,version},actor,request,digest)?;
                }
                Some(event)
            },
            Some(_) => return Err(Error::Conflict),
        };
        drop(transaction); durable(database, result)
    }

    fn settle_goal_claim(&self, database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
        invoice: &Value, now: u64) -> Result<(), Error> {
        let Some(intent)=shared_claim::settlement(&mut Reads{store:self,reads:goal::Reads{database,transaction}},invoice)? else {return Ok(());};
        let result = crate::billing::wallet_settle(database, transaction, &crate::billing::WalletSettleRequest {
            user_id:text(&intent,"user_id")?.into(),ref_id:text(&intent,"ref_id")?.into(),
            reserved_micro:intent["reserved_micro"].as_u64().ok_or(Error::Invalid)?,actual_micro:intent["actual_micro"].as_u64().ok_or(Error::Invalid)?,
            note:intent["note"].as_str().ok_or(Error::Invalid)?.into(),release_id:text(&intent,"release_id")?.into(),
            debit_id:text(&intent,"debit_id")?.into(),occurred_at:now,
        }).map_err(map_io)?;
        if decode(&result)?["applied"] != true { return Err(Error::Conflict); }
        Ok(())
    }
}
