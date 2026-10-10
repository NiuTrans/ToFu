//! Atomic automation occurrence/job admission and live-lease-fenced progress.
//! Uses existing record and worker authorities; recovery lists compact active
//! pointers instead of terminal run bodies. No execution or policy lives here.

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::generated_tofudb_ir::{
    AUTOMATION_ACTIVE_EFFECT_STEP_STATES, AUTOMATION_CANCELLABLE_PENDING_STEP_STATES,
    AUTOMATION_TERMINAL_RUN_STATES,
};
use crate::{versioned_document as records, worker_job};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io;

const DEFINITIONS: &str = crate::generated_tofudb_ir::AUTOMATION_DEFINITION_NAMESPACE;
const RUNS: &str = crate::generated_tofudb_ir::AUTOMATION_RUN_NAMESPACE;
const ACTIVE: &str = crate::generated_tofudb_ir::AUTOMATION_ACTIVE_NAMESPACE;
const HISTORY: &str = crate::generated_tofudb_ir::AUTOMATION_HISTORY_NAMESPACE;
const IMMUTABLE: &[&str] = &[
    "contract",
    "id",
    "ownerId",
    "tenantId",
    "definitionId",
    "definitionRevision",
    "definitionDigest",
    "snapshot",
    "actions",
    "stepOrder",
    "trigger",
    "triggerDigest",
    "automationChain",
    "createdAt",
    "deadlineAt",
    "jobId",
    "principal",
];

pub(crate) fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
pub(crate) fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}
pub(crate) fn integrity(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
pub(crate) fn digest(value: &[u8]) -> String {
    format!("{:x}", Sha256::digest(value))
}
pub(crate) fn encode(value: &Value) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid("automation JSON cannot be encoded"))
}
pub(crate) fn text<'a>(value: &'a Value, field: &str, limit: usize) -> io::Result<&'a str> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.chars().count() <= limit)
        .ok_or_else(|| invalid("invalid automation text field"))
}
fn terminal(value: &Value) -> bool {
    value["status"]
        .as_str()
        .is_some_and(|s| AUTOMATION_TERMINAL_RUN_STATES.contains(&s))
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CancelRequest {
    user_id: u64,
    tenant_id: String,
    run_key: String,
    now_ms: u64,
    request_id: String,
    reason: String,
}

impl CancelRequest {
    fn prefix(&self) -> String {
        format!(
            "{}:{}:",
            self.user_id,
            &digest(self.tenant_id.as_bytes())[..32]
        )
    }

    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        serde_json::to_vec(self)
            .map(|b| b.len())
            .map_err(|_| invalid("cancellation cannot be encoded"))
    }

    pub(crate) fn validate(&self, owner: u64, authority_now: u64) -> io::Result<()> {
        let id = self.request_id.as_bytes();
        if self.user_id == 0
            || self.user_id != owner
            || self.user_id > 9_007_199_254_740_991
            || self.tenant_id.chars().count() > 256
            || self.run_key.chars().count() > 512
            || !self.run_key.starts_with(&self.prefix())
            || self.now_ms == 0
            || self.now_ms > 9_007_199_254_740_991
            || self.now_ms.saturating_sub(authority_now) > 86_400_000
            || id.is_empty()
            || id.len() > 160
            || !id[0].is_ascii_alphanumeric()
            || !id
                .iter()
                .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(b))
            || self.reason.is_empty()
            || self.reason.chars().count() > 200
            || self.reason.contains('\0')
        {
            return Err(invalid("invalid automation cancellation input"));
        }
        Ok(())
    }
}

pub(crate) fn cancel(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    req: &CancelRequest,
) -> io::Result<Vec<u8>> {
    let current = read(db, tx, RUNS, &req.run_key)?
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "automation run does not exist"))?;
    let run = &current["value"];
    let job_id = format!("automation-run:{}", digest(req.run_key.as_bytes()));
    if req.run_key != format!("{}{}", req.prefix(), text(run, "id", 160)?)
        || run["ownerId"] != req.user_id
        || run["tenantId"].as_str().unwrap_or("") != req.tenant_id
        || run["jobId"] != job_id
    {
        return Err(invalid("automation cancellation escaped its owner"));
    }
    let command = &run["cancelCommand"];
    if !command.is_null() && command["id"] == req.request_id && command["reason"] != req.reason {
        return Err(conflict("automation cancellation request evidence changed"));
    }
    if terminal(run) {
        return encode(&json!({"accepted":false,"alreadyTerminal":true,"run":current}));
    }
    if !command.is_null() {
        let job = worker_job::get(db, tx, &job_id, req.user_id)?
            .ok_or_else(|| integrity("automation cancellation has no durable job"))?;
        let job: Value =
            serde_json::from_slice(&job).map_err(|_| integrity("malformed worker job"))?;
        if job["cancelSequence"].as_u64().unwrap_or(0) == 0 {
            return Err(integrity(
                "automation cancellation has no durable worker command",
            ));
        }
        return encode(&json!({"accepted":true,"alreadyRequested":true,"run":current}));
    }
    let result = worker_job::request_cancel(db, tx, &job_id, req.user_id, req.now_ms, &req.reason)?
        .ok_or_else(|| integrity("an active automation has no durable job"))?;
    let result: Value =
        serde_json::from_slice(&result).map_err(|_| integrity("malformed worker cancellation"))?;
    if result["accepted"] != true || result["job"]["tenantId"] != req.tenant_id {
        return Err(integrity(
            "an active automation has no cancellable durable job",
        ));
    }
    let mut cancelled = run.clone();
    let active = run["steps"]
        .as_object()
        .ok_or_else(|| integrity("automation steps are malformed"))?
        .values()
        .any(|s| {
            s["status"]
                .as_str()
                .is_some_and(|v| AUTOMATION_ACTIVE_EFFECT_STEP_STATES.contains(&v))
        });
    if active && result["job"]["status"] == "cancelled" {
        return Err(integrity(
            "an active external effect cannot have a queued job",
        ));
    }
    let now = req.now_ms as f64 / 1000.0;
    cancelled["status"] = json!(if active { "cancelling" } else { "cancelled" });
    cancelled["cancelReason"] = json!(req.reason);
    cancelled["updatedAt"] = json!(now);
    cancelled["cancelCommand"] = json!({"id":req.request_id,"reason":req.reason,"requestedAt":now});
    if !active {
        cancelled["completedAt"] = json!(now);
    }
    for state in cancelled["steps"]
        .as_object_mut()
        .ok_or_else(|| integrity("automation steps are malformed"))?
        .values_mut()
    {
        if state["status"]
            .as_str()
            .is_some_and(|v| AUTOMATION_CANCELLABLE_PENDING_STEP_STATES.contains(&v))
        {
            state["status"] = json!("cancelled");
        }
    }
    let saved = put(
        db,
        tx,
        RUNS,
        &req.run_key,
        &cancelled,
        current["version"]
            .as_u64()
            .ok_or_else(|| integrity("automation revision is malformed"))?,
        req.now_ms,
    )?;
    update_index(db, tx, &req.prefix(), &req.run_key, &cancelled, req.now_ms)?;
    encode(&json!({"accepted":true,"alreadyRequested":false,"run":saved}))
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ProducerFence {
    job_id: String,
    worker_id: String,
    token: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    user_id: u64,
    tenant_id: String,
    run_key: String,
    run: Value,
    now_ms: u64,
    #[serde(default)]
    definition_key: Option<String>,
    #[serde(default)]
    definition_revision: Option<u64>,
    #[serde(default)]
    definition_digest: Option<String>,
    #[serde(default)]
    expected_version: Option<u64>,
    #[serde(default)]
    worker_id: Option<String>,
    #[serde(default)]
    fencing_token: Option<u64>,
    #[serde(default)]
    producer_fence: Option<ProducerFence>,
}

pub(crate) fn validate_record_bounds(run: &Value) -> io::Result<()> {
    let mut pending = vec![(run, 1usize)];
    let mut nodes = 0usize;
    while let Some((value, depth)) = pending.pop() {
        nodes += 1;
        if nodes > crate::generated_tofudb_ir::MAX_AUTOMATION_RECORD_NODES
            || depth > crate::generated_tofudb_ir::MAX_AUTOMATION_RECORD_DEPTH {
            return Err(invalid("automation JSON exceeds bounds"));
        }
        match value {
            Value::Array(values) => pending.extend(values.iter().map(|v| (v, depth + 1))),
            Value::Object(values) => pending.extend(values.values().map(|v| (v, depth + 1))),
            Value::Number(n)
                if n.as_u64().is_some_and(|v| v > crate::generated_tofudb_ir::MAX_AUTOMATION_SAFE_INTEGER)
                    || n.as_i64().is_some_and(|v| v < -(crate::generated_tofudb_ir::MAX_AUTOMATION_SAFE_INTEGER as i64)) =>
            {
                return Err(invalid("automation integer exceeds bound"))
            }
            _ => (),
        }
    }
    if encode(run)?.len() > crate::generated_tofudb_ir::MAX_AUTOMATION_RECORD_BYTES {
        return Err(invalid("automation record exceeds byte bound"));
    }
    Ok(())
}

impl Request {
    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        serde_json::to_vec(self)
            .map(|bytes| bytes.len())
            .map_err(|_| invalid("automation request cannot be encoded"))
    }

    fn prefix(&self) -> String {
        format!(
            "{}:{}:",
            self.user_id,
            &digest(self.tenant_id.as_bytes())[..32]
        )
    }
    fn job_id(&self) -> String {
        format!("automation-run:{}", digest(self.run_key.as_bytes()))
    }
    pub(crate) fn validate(
        &self,
        advance: bool,
        authority_owner: u64,
        authority_now: u64,
    ) -> io::Result<()> {
        validate_record_bounds(&self.run)?;
        let run = &self.run;
        for f in ["id", "definitionId"] {
            let s = text(run, f, 160)?;
            if !s.as_bytes().first().is_some_and(u8::is_ascii_alphanumeric)
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
            {
                return Err(invalid("invalid automation entity id"));
            }
        }
        for f in ["definitionDigest", "triggerDigest"] {
            let s = text(run, f, 64)?;
            if s.len() != 64
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err(invalid("invalid automation evidence digest"));
            }
        }
        for f in ["createdAt", "updatedAt", "deadlineAt"] {
            if !run[f]
                .as_f64()
                .is_some_and(|n| n.is_finite() && n > 0.0 && n <= 9_007_199_254_740.0)
            {
                return Err(invalid("invalid automation timestamp"));
            }
        }
        if !run["tenantId"].is_null() && !run["tenantId"].is_string()
            || !run["definitionRevision"].as_u64().is_some_and(|n| n > 0)
            || !matches!(
                run["status"].as_str(),
                Some(
                    "queued"
                        | "running"
                        | "waiting_agent"
                        | "waiting_permission"
                        | "reconciliation_required"
                        | "cancelling"
                        | "succeeded"
                        | "failed"
                        | "cancelled"
                )
            )
            || ["snapshot", "actions", "steps", "trigger", "principal"]
                .iter()
                .any(|f| !run[*f].is_object())
            || !run["stepOrder"]
                .as_array()
                .is_some_and(|a| !a.is_empty() && a.len() <= 64 && a.iter().all(Value::is_string))
            || !run["automationChain"]
                .as_array()
                .is_some_and(|a| a.len() <= 16 && a.iter().all(Value::is_string))
            || !(run
                .get("fence")
                .is_some_and(|v| v.is_null() || v.is_object()))
        {
            return Err(invalid("automation run shape differs from its contract"));
        }

        let run = &self.run;
        if self.user_id == 0
            || self.user_id != authority_owner
            || self.tenant_id.chars().count() > 256
            || self.now_ms == 0
            || self.now_ms > 9_007_199_254_740_991
            || self.now_ms.saturating_sub(authority_now) > 86_400_000
            || !run.is_object()
            || encode(run)?.len() > 524_288
            || run["ownerId"].as_u64() != Some(self.user_id)
            || run["tenantId"].as_str().unwrap_or("") != self.tenant_id
            || run["contract"] != "tofu.automation-run/v1"
            || self.run_key != self.prefix() + text(run, "id", 160)?
            || run["jobId"] != self.job_id()
        {
            return Err(invalid("automation mutation identity or bound is invalid"));
        }
        if let Some(fence) = &self.producer_fence {
            if advance
                || fence.job_id.is_empty()
                || fence.job_id.chars().count() > 200
                || fence.worker_id.is_empty()
                || fence.worker_id.chars().count() > 200
                || fence.token == 0
                || fence.token > 9_007_199_254_740_991
            {
                return Err(invalid("invalid automation producer fence"));
            }
        }
        if advance {
            if self.definition_key.is_some()
                || self.definition_revision.is_some()
                || self.definition_digest.is_some()
                || self.expected_version.unwrap_or(0) == 0
                || self.fencing_token.unwrap_or(0) == 0
                || !self
                    .worker_id
                    .as_ref()
                    .is_some_and(|s| !s.is_empty() && s.chars().count() <= 200)
            {
                return Err(invalid("invalid automation progress fields"));
            }
        } else if run["steps"].as_object().is_some_and(|steps|
            steps.values().any(|state| state.get("approval").is_some())) {
            return Err(invalid("initial automation state cannot carry approval"));
        } else if self.expected_version.is_some()
            || self.worker_id.is_some()
            || self.fencing_token.is_some()
            || self.definition_key.as_ref()
                != Some(&(self.prefix() + text(run, "definitionId", 160)?))
            || self.definition_revision.unwrap_or(0) == 0
            || !self
                .definition_digest
                .as_ref()
                .is_some_and(|s| s.len() == 64)
        {
            return Err(invalid("invalid automation admission fields"));
        }
        Ok(())
    }
}

pub(crate) fn read(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    ns: &str,
    key: &str,
) -> io::Result<Option<Value>> {
    let physical = records::record_key(tx.tenant_id(), tx.owner_user_id(), ns, key)?;
    records::get(db, tx, &physical, ns, key)?
        .map(|bytes| {
            serde_json::from_slice(&bytes).map_err(|_| integrity("automation record is malformed"))
        })
        .transpose()
}
pub(crate) fn put(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    ns: &str,
    key: &str,
    value: &Value,
    version: u64,
    now: u64,
) -> io::Result<Value> {
    let result = records::put(
        db,
        tx,
        records::PutRequest {
            key: records::record_key(tx.tenant_id(), tx.owner_user_id(), ns, key)?,
            namespace: ns.into(),
            logical_key: key.into(),
            value_json: encode(value)?,
            expected_version: Some(version),
            updated_at_ms: now,
        },
    )?;
    let mut envelope: Value =
        serde_json::from_slice(&result).map_err(|_| integrity("automation put is malformed"))?;
    envelope["value"] = value.clone();
    Ok(envelope)
}
pub(crate) fn update_index(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    prefix: &str,
    run_key: &str,
    run: &Value,
    now_ms: u64,
) -> io::Result<()> {
    let pointer = json!({"id":run["id"], "ownerId":run["ownerId"], "tenantId":run["tenantId"],
        "definitionId":run["definitionId"], "jobId":run["jobId"], "status":run["status"], "createdAt":run["createdAt"]});
    let active = read(db, tx, ACTIVE, run_key)?;
    if terminal(run) {
        if let Some(row) = active {
            records::delete(
                db,
                tx,
                records::record_key(tx.tenant_id(), tx.owner_user_id(), ACTIVE, run_key)?,
                ACTIVE,
                run_key,
                row["version"].as_u64(),
            )?;
        }
    } else {
        put(
            db,
            tx,
            ACTIVE,
            run_key,
            &pointer,
            active
                .as_ref()
                .and_then(|v| v["version"].as_u64())
                .unwrap_or(0),
            now_ms,
        )?;
    }
    let at = run["createdAt"]
        .as_f64()
        .filter(|v| v.is_finite() && *v > 0.0 && *v < 9_007_199_254_740.0)
        .ok_or_else(|| invalid("invalid automation creation timestamp"))?;
    let history_key = format!(
        "{}{}:{:016}:{}",
        prefix,
        text(run, "definitionId", 160)?,
        (at * 1000.0) as u64,
        text(run, "id", 160)?
    );
    let history = read(db, tx, HISTORY, &history_key)?;
    put(
        db,
        tx,
        HISTORY,
        &history_key,
        &pointer,
        history
            .as_ref()
            .and_then(|v| v["version"].as_u64())
            .unwrap_or(0),
        now_ms,
    )?;
    Ok(())
}

pub(crate) fn validate_principal(run: &Value, owner: u64, tenant: &str) -> io::Result<()> {
    let p = run["principal"]
        .as_object()
        .ok_or_else(|| invalid("automation grant is missing"))?;
    let fields = ["kind", "subject_id", "owner_user_id", "tenant_id", "scopes"];
    if p.len() != fields.len()
        || fields.iter().any(|f| !p.contains_key(*f))
        || !matches!(p["kind"].as_str(), Some("user" | "system"))
        || p["owner_user_id"].as_u64() != Some(owner)
        || p["tenant_id"].as_str().unwrap_or("") != tenant
    {
        return Err(invalid("automation grant identity differs"));
    }
    text(&run["principal"], "subject_id", 256)?;
    let scopes = p["scopes"]
        .as_array()
        .ok_or_else(|| invalid("automation scopes are missing"))?;
    let mut previous = "";
    for s in scopes {
        let s = s
            .as_str()
            .filter(|s| !s.is_empty() && s.chars().count() <= 128)
            .ok_or_else(|| invalid("invalid automation scope"))?;
        if s == "admin" || s <= previous {
            return Err(invalid(
                "automation scopes must be canonical and restricted",
            ));
        }
        previous = s;
    }
    let has = |scope: &str| scopes.iter().any(|s| s.as_str() == Some(scope));
    if !has("automations:read") || !has("automations:execute") {
        return Err(invalid("automation grant lacks execution scopes"));
    }
    let actions = run["actions"]
        .as_object()
        .ok_or_else(|| invalid("automation actions are missing"))?;
    for action in actions.values() {
        let required = action["requiredScopes"]
            .as_array()
            .ok_or_else(|| invalid("automation required scopes are missing"))?;
        for scope in required {
            if !scope.as_str().is_some_and(has) {
                return Err(invalid("automation action exceeds its grant"));
            }
        }
    }
    Ok(())
}

pub(crate) fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    req: &Request,
    advance: bool,
) -> io::Result<Vec<u8>> {
    let job_id = req.job_id();
    let existing = read(db, tx, RUNS, &req.run_key)?;
    if advance {
        if let Some(ref row) = existing {
            if row["version"].as_u64() == req.expected_version.map(|v| v + 1)
                && row["value"] == req.run
            {
                return encode(row);
            }
        }
        let worker = req
            .worker_id
            .as_deref()
            .ok_or_else(|| invalid("missing worker"))?;
        let fence = req.fencing_token.ok_or_else(|| invalid("missing fence"))?;
        let job = worker_job::get(db, tx, &job_id, req.user_id)?
            .ok_or_else(|| conflict("automation job is missing"))?;
        let job: Value = serde_json::from_slice(&job).map_err(|_| integrity("malformed job"))?;
        let proof: Value = serde_json::from_slice(&worker_job::claim_state(
            db, tx, &job_id, worker, fence, req.now_ms,
        )?)
        .map_err(|_| integrity("malformed claim proof"))?;
        if job["tenantId"] != req.tenant_id
            || proof["ok"] != true
            || req.run["fence"] != json!({"jobId":job_id,"workerId":worker,"token":fence})
        {
            return Err(conflict("automation claim is stale"));
        }
        let existing = existing.ok_or_else(|| conflict("automation run is missing"))?;
        if terminal(&existing["value"]) {
            return Err(conflict("automation is terminal"));
        }
        if existing["version"].as_u64() != req.expected_version {
            return Err(conflict("automation run revision changed"));
        }
        if IMMUTABLE
            .iter()
            .any(|f| existing["value"][*f] != req.run[*f])
        {
            return Err(invalid("automation immutable evidence changed"));
        }
        crate::automation_approval::validate_progress(&existing["value"], &req.run)?;
        crate::automation_presentation::publish(db, tx, &req.prefix(), &existing["value"], &req.run, req.now_ms)?;
        if existing["value"]["cancelCommand"] != req.run["cancelCommand"]
            || (!existing["value"]["cancelCommand"].is_null()
                && existing["value"]["cancelReason"] != req.run["cancelReason"])
        {
            return Err(invalid("automation cancellation evidence changed"));
        }
        if !existing["value"]["cancelCommand"].is_null() {
            if !matches!(req.run["status"].as_str(), Some("cancelling" | "cancelled")) {
                return Err(conflict(
                    "owner cancellation requires cancellation settlement",
                ));
            }
            if req.run["status"] == "cancelled"
                && req.run["steps"]
                    .as_object()
                    .ok_or_else(|| invalid("automation steps are malformed"))?
                    .values()
                    .any(|state| {
                        state["status"].as_str().is_some_and(|status| {
                            AUTOMATION_ACTIVE_EFFECT_STEP_STATES.contains(&status)
                        })
                    })
            {
                return Err(invalid(
                    "cancellation cannot settle an unresolved external effect",
                ));
            }
        }
        if proof["cancelSequence"].as_u64().unwrap_or(0) > 0
            && !matches!(
                req.run["status"].as_str(),
                Some("cancelling" | "cancelled" | "failed")
            )
        {
            return Err(conflict("automation cancellation requires settlement"));
        }
        let saved = put(
            db,
            tx,
            RUNS,
            &req.run_key,
            &req.run,
            req.expected_version.unwrap_or(0),
            req.now_ms,
        )?;
        update_index(db, tx, &req.prefix(), &req.run_key, &req.run, req.now_ms)?;
        return encode(&saved);
    }
    if let Some(existing) = existing {
        if existing["value"]["triggerDigest"] != req.run["triggerDigest"] {
            return Err(conflict("automation occurrence evidence changed"));
        }
        let job = worker_job::get(db, tx, &job_id, req.user_id)?
            .ok_or_else(|| integrity("accepted automation has no durable job"))?;
        let job: Value = serde_json::from_slice(&job).map_err(|_| integrity("malformed job"))?;
        return encode(&json!({"created":false,"run":existing,"job":job}));
    }
    if req.run["status"] != "queued" || !req.run["fence"].is_null() {
        return Err(invalid("a new automation must be queued without a claim"));
    }
    validate_principal(&req.run, req.user_id, &req.tenant_id)?;
    let definition = read(
        db,
        tx,
        DEFINITIONS,
        req.definition_key
            .as_deref()
            .ok_or_else(|| invalid("missing definition key"))?,
    )?
    .ok_or_else(|| conflict("automation definition is missing"))?;
    let d = &definition["value"];
    if definition["version"].as_u64() != req.definition_revision
        || d["compiled"]["digest"].as_str() != req.definition_digest.as_deref()
        || req.run["definitionRevision"].as_u64() != req.definition_revision
        || req.run["definitionDigest"].as_str() != req.definition_digest.as_deref()
        || d["deleted"] == true
        || (d["definition"]["enabled"] != true && req.run["trigger"]["kind"] != "manual")
    {
        return Err(conflict("automation definition changed before admission"));
    }
    crate::automation_project::require_active(db,tx,&req.tenant_id,d["definition"]["projectId"].as_str())?;
    if !d["definition"]["projectId"].is_null() && req.run["trigger"]["projectId"]!=d["definition"]["projectId"] {
        return Err(invalid("automation trigger escaped its project binding"));
    }
    if req.run["snapshot"] != d["definition"]
        || req.run["actions"] != d["compiled"]["actions"]
        || req.run["stepOrder"] != d["compiled"]["stepOrder"]
        || req.run["principal"] != d["grant"]["principal"]
    {
        return Err(invalid(
            "automation snapshot differs from admitted definition",
        ));
    }
    if req.run["trigger"]["kind"] == "schedule" {
        let fence = req
            .producer_fence
            .as_ref()
            .ok_or_else(|| conflict("schedule producer is missing"))?;
        if d["triggerState"]["clockJobId"] != fence.job_id {
            return Err(conflict("schedule producer revision changed"));
        }
        let job = worker_job::get(db, tx, &fence.job_id, req.user_id)?
            .ok_or_else(|| conflict("schedule producer is missing"))?;
        let job: Value =
            serde_json::from_slice(&job).map_err(|_| integrity("malformed clock job"))?;
        let proof: Value = serde_json::from_slice(&worker_job::claim_state(
            db,
            tx,
            &fence.job_id,
            &fence.worker_id,
            fence.token,
            req.now_ms,
        )?)
        .map_err(|_| integrity("malformed producer claim"))?;
        if job["taskKind"] != "automation-clock"
            || job["tenantId"] != req.tenant_id
            || job["payload"]["definitionKey"].as_str() != req.definition_key.as_deref()
            || job["payload"]["definitionRevision"].as_u64() != req.definition_revision
            || proof["ok"] != true
            || proof["cancelSequence"].as_u64().unwrap_or(0) != 0
        {
            return Err(conflict("schedule producer claim is stale"));
        }
    } else if req.run["trigger"]["kind"] == "task.started"
        && (req.producer_fence.is_some()
            || req.run["trigger"]["id"].as_str().is_some_and(|id| id.starts_with("task-start:"))) {
        let fence = req.producer_fence.as_ref()
            .ok_or_else(|| conflict("canonical task-start producer is missing"))?;
        let job = worker_job::get(db, tx, &fence.job_id, req.user_id)?
            .ok_or_else(|| conflict("task-start producer is missing"))?;
        let job: Value = serde_json::from_slice(&job).map_err(|_| integrity("malformed event job"))?;
        let proof: Value = serde_json::from_slice(&worker_job::claim_state(
            db, tx, &fence.job_id, &fence.worker_id, fence.token, req.now_ms,
        )?).map_err(|_| integrity("malformed event producer claim"))?;
        if job["taskKind"] != "automation-event" || job["tenantId"] != req.tenant_id
            || job["payload"]["event"] != req.run["trigger"] || proof["ok"] != true
            || proof["cancelSequence"].as_u64().unwrap_or(0) != 0 {
            return Err(conflict("task-start producer claim is stale"));
        }
    } else if req.producer_fence.is_some() {
        return Err(invalid("only schedule or canonical task-start admission carries a producer fence"));
    }
    let saved = put(db, tx, RUNS, &req.run_key, &req.run, 0, req.now_ms)?;
    let payload = json!({"contract":"tofu.automation-job/v1","runId":req.run["id"],"principal":req.run["principal"]});
    let task_kind = "automation-run".to_string();
    let request_digest = digest(&encode(
        &json!({"tenantId":req.tenant_id,"taskKind":task_kind,"payload":payload}),
    )?);
    let job = worker_job::enqueue(
        db,
        tx,
        worker_job::EnqueueRequest {
            task_id: job_id.clone(),
            user_id: req.user_id,
            tenant_id: req.tenant_id.clone(),
            task_kind,
            payload,
            idempotency_key: job_id,
            request_digest,
            priority: 200,
            available_at_ms: req.now_ms,
            now_ms: req.now_ms,
        },
    )?;
    let job: Value = serde_json::from_slice(&job).map_err(|_| integrity("malformed enqueue"))?;
    update_index(db, tx, &req.prefix(), &req.run_key, &req.run, req.now_ms)?;
    encode(&json!({"created":true,"run":saved,"job":job["job"]}))
}
