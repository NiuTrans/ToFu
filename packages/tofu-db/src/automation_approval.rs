//! Owned permission approval and immutable worker dispatch authorization.
//! One transaction commits the reviewed-byte proof plus the canonical wake.
//! It grants no executor lease and never changes the run's frozen principal.
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::automation_run::{self as runs, conflict, digest, encode, integrity, invalid};
use crate::worker_job;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io;

const RUNS: &str = crate::generated_tofudb_ir::AUTOMATION_RUN_NAMESPACE;
const MAX_SAFE: u64 = crate::generated_tofudb_ir::MAX_AUTOMATION_SAFE_INTEGER;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ApproveRequest {
    user_id: u64,
    tenant_id: String,
    run_key: String,
    now_ms: u64,
    request_id: String,
    expected_version: u64,
    step_id: String,
    actor_principal: Value,
    arguments_json: String,
    action_json: String,
}

impl ApproveRequest {
    fn prefix(&self) -> String {
        format!(
            "{}:{}:",
            self.user_id,
            &digest(self.tenant_id.as_bytes())[..32]
        )
    }

    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        encode(&serde_json::to_value(self).map_err(|_| invalid("invalid approval"))?)
            .map(|v| v.len())
    }

    fn request_digest(&self) -> io::Result<String> {
        let mut value = serde_json::to_value(self).map_err(|_| invalid("invalid approval"))?;
        value
            .as_object_mut()
            .ok_or_else(|| invalid("invalid approval"))?
            .remove("now_ms");
        Ok(digest(&encode(&value)?))
    }

    pub(crate) fn validate(&self, owner: u64, now: u64) -> io::Result<()> {
        let slug = |s: &str, max: usize, colon: bool| {
            let b = s.as_bytes();
            !b.is_empty()
                && b.len() <= max
                && b[0].is_ascii_alphanumeric()
                && b.iter()
                    .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(c) || colon && *c == b':')
        };
        if self.user_id == 0
            || self.user_id != owner
            || self.user_id > MAX_SAFE
            || self.tenant_id.chars().count() > 256
            || self.run_key.chars().count() > 512
            || !self.run_key.starts_with(&self.prefix())
            || self.now_ms == 0
            || self.now_ms > MAX_SAFE
            || self.now_ms.saturating_sub(now) > 86_400_000
            || self.expected_version == 0
            || self.expected_version > MAX_SAFE
            || !slug(&self.request_id, 160, true)
            || !slug(&self.step_id, 64, false)
            || self.arguments_json.chars().count()
                > crate::generated_tofudb_ir::MAX_AUTOMATION_RECORD_BYTES
            || self.action_json.chars().count()
                > crate::generated_tofudb_ir::MAX_AUTOMATION_RECORD_BYTES
        {
            return Err(invalid("invalid automation approval input"));
        }
        let canonical_tenant = if self.tenant_id.is_empty() {
            Value::Null
        } else {
            json!(self.tenant_id)
        };
        let p = &self.actor_principal;
        runs::validate_record_bounds(p)?;
        if p["tenant_id"] != canonical_tenant
            || p["subject_id"].as_str().is_none_or(|s| s != s.trim())
            || p["scopes"].as_array().is_none_or(|scopes| {
                scopes
                    .iter()
                    .any(|s| s.as_str().is_none_or(|s| s != s.trim()))
            })
        {
            return Err(invalid("approval actor is not canonical"));
        }
        runs::validate_principal(&json!({"principal":p,"actions":{}}), owner, &self.tenant_id)?;
        if !p["scopes"]
            .as_array()
            .is_some_and(|scopes| scopes.iter().any(|s| s == "automations:manage"))
        {
            return Err(invalid("approval actor lacks management authority"));
        }
        Ok(())
    }
}

pub(crate) fn approve(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    req: &ApproveRequest,
) -> io::Result<Vec<u8>> {
    let current = runs::read(db, tx, RUNS, &req.run_key)?
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "automation run does not exist"))?;
    let run = &current["value"];
    let job_id = format!("automation-run:{}", digest(req.run_key.as_bytes()));
    if req.run_key != format!("{}{}", req.prefix(), runs::text(run, "id", 160)?)
        || run["ownerId"] != req.user_id
        || run["tenantId"].as_str().unwrap_or("") != req.tenant_id
        || run["jobId"] != job_id
    {
        return Err(invalid("automation approval escaped its owner"));
    }
    let previous = &run["steps"][&req.step_id]["approval"];
    if !previous.is_null() {
        if previous["requestId"] != req.request_id
            || previous["requestDigest"] != req.request_digest()?
        {
            return Err(conflict("approval request evidence changed"));
        }
        return encode(&json!({"accepted":true,"alreadyApproved":true,"run":current}));
    }
    if current["version"].as_u64() != Some(req.expected_version) {
        return Err(conflict("automation approval revision changed"));
    }
    let step = run["snapshot"]["steps"]
        .as_array()
        .and_then(|steps| steps.iter().find(|s| s["id"] == req.step_id))
        .ok_or_else(|| conflict("approval step does not exist"))?;
    let action_id = step["action"]
        .as_str()
        .ok_or_else(|| integrity("invalid approval action"))?;
    let action = &run["actions"][action_id];
    runs::validate_principal(
        &json!({"principal":req.actor_principal,
        "actions":{action_id:action}}),
        req.user_id,
        &req.tenant_id,
    )?;
    let state = &run["steps"][&req.step_id];
    if run["status"] != "waiting_permission"
        || state["status"] != "waiting_permission"
        || run["snapshot"]["policy"]["permissionMode"] != "manual"
        || !matches!(action["effect"].as_str(), Some("write" | "agent"))
        || !run["cancelCommand"].is_null()
        || !state["arguments"].is_object()
        || run["deadlineAt"]
            .as_f64()
            .is_none_or(|d| req.now_ms as f64 / 1000.0 >= d)
    {
        return Err(conflict(
            "approval requires a current unexpired permission wait",
        ));
    }
    let args: Value = serde_json::from_str(&req.arguments_json)
        .map_err(|_| invalid("invalid reviewed arguments"))?;
    let reviewed: Value =
        serde_json::from_str(&req.action_json).map_err(|_| invalid("invalid reviewed action"))?;
    if args != state["arguments"] || reviewed != *action {
        return Err(conflict("reviewed arguments or action changed"));
    }
    let mut updated = run.clone();
    let now = req.now_ms as f64 / 1000.0;
    updated["steps"][&req.step_id]["approval"] = json!({
        "requestId":req.request_id,"requestDigest":req.request_digest()?,
        "actorSubjectId":req.actor_principal["subject_id"],"approvedAt":now,
        "argumentsDigest":digest(req.arguments_json.as_bytes()),"actionDigest":digest(req.action_json.as_bytes()),
        "effectKey":state["effectKey"]});
    updated["steps"][&req.step_id]["status"] = json!("pending");
    updated["status"] = json!("running");
    updated["updatedAt"] = json!(now);
    runs::validate_record_bounds(&updated)?;
    let job = worker_job::wake_owned(db, tx, &job_id, req.user_id, &req.tenant_id, req.now_ms)?
        .ok_or_else(|| conflict("approval requires a canonical job"))?;
    let job: Value = serde_json::from_slice(&job).map_err(|_| integrity("invalid worker job"))?;
    if job["cancelSequence"].as_u64().is_none_or(|n| n != 0) {
        return Err(conflict("approval requires a noncancelled canonical job"));
    }
    let saved = runs::put(
        db,
        tx,
        RUNS,
        &req.run_key,
        &updated,
        req.expected_version,
        req.now_ms,
    )?;
    runs::update_index(db, tx, &req.prefix(), &req.run_key, &updated, req.now_ms)?;
    encode(&json!({"accepted":true,"alreadyApproved":false,"run":saved}))
}

pub(crate) fn validate_progress(current: &Value, updated: &Value) -> io::Result<()> {
    let before = current["steps"]
        .as_object()
        .ok_or_else(|| invalid("invalid approval steps"))?;
    let after = updated["steps"]
        .as_object()
        .ok_or_else(|| invalid("invalid approval steps"))?;
    if before.len() != after.len() || before.keys().any(|id| !after.contains_key(id)) {
        return Err(invalid("worker cannot add or remove frozen steps"));
    }
    let definitions = current["snapshot"]["steps"]
        .as_array()
        .ok_or_else(|| invalid("invalid definition steps"))?;
    for (id, previous) in before {
        let state = &updated["steps"][id];
        let step = definitions
            .iter()
            .find(|step| step["id"] == *id)
            .ok_or_else(|| invalid("step escaped its frozen definition"))?;
        let action = &current["actions"][step["action"]
            .as_str()
            .ok_or_else(|| invalid("invalid action ID"))?];
        if matches!(action["effect"].as_str(), Some("write" | "agent"))
            && matches!(
                previous["status"].as_str(),
                Some("dispatching" | "waiting_agent" | "reconciliation_required" | "succeeded")
            )
            && matches!(
                state["status"].as_str(),
                Some("pending" | "waiting_permission" | "skipped")
            )
        {
            return Err(invalid("external intent cannot become dispatchable again"));
        }
        if previous.get("approval") != state.get("approval")
            || previous["effectKey"] != state["effectKey"]
        {
            return Err(invalid("worker cannot change approval or effect identity"));
        }
        if !previous["approval"].is_null() && previous["arguments"] != state["arguments"] {
            return Err(invalid("approved arguments changed"));
        }
    }
    if current["snapshot"]["policy"]["permissionMode"] == "manual" {
        let steps = current["snapshot"]["steps"]
            .as_array()
            .ok_or_else(|| invalid("invalid definition steps"))?;
        for step in steps {
            let state = &updated["steps"][step["id"]
                .as_str()
                .ok_or_else(|| invalid("invalid step ID"))?];
            let action = &current["actions"][step["action"]
                .as_str()
                .ok_or_else(|| invalid("invalid action ID"))?];
            if matches!(action["effect"].as_str(), Some("write" | "agent"))
                && matches!(
                    state["status"].as_str(),
                    Some("dispatching" | "waiting_agent" | "reconciliation_required" | "succeeded")
                )
                && state["approval"].is_null()
            {
                return Err(invalid("manual external effects require owned approval"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn waiting() -> Value {
        json!({"steps":{"write":{"status":"waiting_permission","effectKey":"effect", "arguments":{"snapshotId":"owned"}}},
            "snapshot":{"policy":{"permissionMode":"manual"},"steps":[{"id":"write","action":"skills.activate"}]},
            "actions":{"skills.activate":{"effect":"write"}}})
    }

    #[test]
    fn worker_cannot_introduce_null_or_forged_permission_proof() {
        let current = waiting();
        for proof in [Value::Null, json!({"requestId":"forged"})] {
            let mut updated = current.clone();
            updated["steps"]["write"]["approval"] = proof;
            assert!(validate_progress(&current, &updated).is_err());
        }
        let mut extra = current.clone();
        extra["steps"]["extra"] = json!({"approval":{"requestId":"forged"}});
        assert!(validate_progress(&current, &extra).is_err());
    }

    #[test]
    fn recovery_preserves_grant_arguments_and_effect_identity() {
        let mut current = waiting();
        current["steps"]["write"]["approval"] = json!({"requestId":"owned"});
        current["steps"]["write"]["status"] = json!("dispatching");
        let mut recovered = current.clone();
        recovered["steps"]["write"]["status"] = json!("reconciliation_required");
        assert!(validate_progress(&current, &recovered).is_ok());
        for (field, value) in [
            ("arguments", json!({"snapshotId":"other"})),
            ("effectKey", json!("other")),
            ("approval", Value::Null),
            ("status", json!("pending")),
            ("status", json!("waiting_permission")),
            ("status", json!("skipped")),
        ] {
            let mut changed = recovered.clone();
            changed["steps"]["write"][field] = value;
            assert!(validate_progress(&current, &changed).is_err());
        }
    }
}
