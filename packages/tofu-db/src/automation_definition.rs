//! Atomic prepared publication, event subscriptions and canonical clock jobs.
//! Entry points: Request/execute and guard_draft. Uses the existing automation
//! owned-record ports and worker authority; interprets no rule or cron language.

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::automation_run::{
    conflict, digest, encode, integrity, invalid, put, read, text, validate_principal,
};
use crate::generated_tofudb_ir::*;
use crate::{versioned_document as records, worker_job};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    sync::LazyLock,
};

static CATALOG: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(AUTOMATION_ACTION_CATALOG_JSON)
        .expect("generated action catalogue is valid JSON")
});

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    user_id: u64,
    tenant_id: String,
    definition_key: String,
    expected_version: u64,
    request_id: String,
    actor_subject_id: String,
    mode: String,
    record: Value,
    definition_json: Option<String>,
    actions_json: Option<String>,
    first_due_ms: Option<u64>,
    now_ms: u64,
}

fn fields(value: &Value, required: &[&str]) -> io::Result<()> {
    if !value
        .as_object()
        .is_some_and(|o| o.len() == required.len() && required.iter().all(|k| o.contains_key(*k)))
    {
        return Err(invalid("invalid prepared publication fields"));
    }
    Ok(())
}
fn entity_id(value: &str) -> bool {
    let b = value.as_bytes();
    !b.is_empty()
        && b.len() <= 160
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(c))
}
fn command_id(value: &str) -> bool {
    let b = value.as_bytes();
    !b.is_empty()
        && b.len() <= 160
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"_.:-".contains(c))
}
fn bounded_record(value: &Value) -> io::Result<()> {
    let mut pending = vec![(value, 0usize)];
    let mut nodes = 0;
    while let Some((node, depth)) = pending.pop() {
        nodes += 1;
        if depth > MAX_AUTOMATION_RECORD_DEPTH || nodes > MAX_AUTOMATION_RECORD_NODES {
            return Err(invalid("publication JSON bounds exceeded"));
        }
        match node {
            Value::Array(a) => pending.extend(a.iter().map(|v| (v, depth + 1))),
            Value::Object(o) => pending.extend(o.values().map(|v| (v, depth + 1))),
            Value::Number(n)
                if n.as_u64().is_some_and(|n| n > MAX_AUTOMATION_SAFE_INTEGER)
                    || n.as_i64()
                        .is_some_and(|n| n < -(MAX_AUTOMATION_SAFE_INTEGER as i64)) =>
            {
                return Err(invalid("publication integer exceeds the JSON bound"));
            }
            _ => (),
        }
    }
    Ok(())
}

fn subscription_key(prefix: &str, definition: &Value) -> io::Result<String> {
    let project = definition["projectId"]
        .as_str()
        .filter(|v| !v.is_empty())
        .map(|v| digest(v.as_bytes()))
        .unwrap_or("*".into());
    Ok(format!(
        "{}{}:{}:{}:{}",
        prefix,
        text(&definition["trigger"], "event", 160)?,
        text(&definition["trigger"], "scope", 160)?,
        project,
        text(definition, "id", 160)?
    ))
}

impl Request {
    fn prefix(&self) -> String {
        format!(
            "{}:{}:",
            self.user_id,
            &digest(self.tenant_id.as_bytes())[..32]
        )
    }
    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        serde_json::to_vec(self)
            .map(|v| v.len())
            .map_err(|_| invalid("publication cannot be encoded"))
    }
    pub(crate) fn validate(&self, owner: u64, authority_now: u64) -> io::Result<()> {
        if self.user_id == 0
            || self.user_id != owner
            || self.user_id > MAX_AUTOMATION_SAFE_INTEGER
            || self.expected_version > MAX_AUTOMATION_SAFE_INTEGER
            || self.tenant_id.chars().count() > 256
            || self.now_ms == 0
            || self.now_ms > MAX_AUTOMATION_SAFE_INTEGER
            || self.now_ms.saturating_sub(authority_now) > 86_400_000
            || !command_id(&self.request_id)
            || !self.definition_key.starts_with(&self.prefix())
            || !entity_id(&self.definition_key[self.prefix().len()..])
            || self.actor_subject_id.is_empty()
            || self.actor_subject_id.chars().count() > 256
            || self.literal_bytes()? > MAX_AUTOMATION_PUBLICATION_REQUEST_BYTES
            || self
                .first_due_ms
                .is_some_and(|n| n == 0 || n > MAX_AUTOMATION_SAFE_INTEGER)
        {
            return Err(invalid("invalid automation publication identity or clock"));
        }
        if matches!(self.mode.as_str(), "pause" | "retire") {
            if self.expected_version == 0
                || !self.record.is_null()
                || self.definition_json.is_none()
                || !self
                    .definition_json
                    .as_ref()
                    .is_some_and(|v| v.len() >= 2 && v.len() <= MAX_AUTOMATION_DEFINITION_BYTES)
                || !self.actions_json.as_ref().is_some_and(|v| {
                    v.len() >= 2 && v.chars().count() <= MAX_AUTOMATION_RECORD_BYTES
                })
                || self.first_due_ms.is_some()
            {
                return Err(invalid("invalid retirement fields"));
            }
            return Ok(());
        }
        if self.mode != "publish" {
            return Err(invalid("unsupported publication mode"));
        }
        bounded_record(&self.record)?;
        fields(&self.record, &["definition", "compiled", "grant"])?;
        fields(
            &self.record["compiled"],
            &["digest", "stepOrder", "actions"],
        )?;
        fields(&self.record["grant"], &["principal", "approvedBy"])?;
        let definition_json = self
            .definition_json
            .as_deref()
            .ok_or_else(|| invalid("prepared JSON is required"))?;
        let actions_json = self
            .actions_json
            .as_deref()
            .ok_or_else(|| invalid("prepared action JSON is required"))?;
        let definition: Value =
            serde_json::from_str(definition_json).map_err(|_| invalid("invalid prepared JSON"))?;
        let actions: Value = serde_json::from_str(actions_json)
            .map_err(|_| invalid("invalid prepared action JSON"))?;
        let id = text(&definition, "id", 160)?;
        if !entity_id(id)
            || self.definition_key != format!("{}{}", self.prefix(), id)
            || !definition["enabled"].is_boolean()
            || definition != self.record["definition"]
            || actions != self.record["compiled"]["actions"]
            || definition_json.len() > MAX_AUTOMATION_DEFINITION_BYTES
            || actions_json.chars().count() > MAX_AUTOMATION_RECORD_BYTES
            || self.record["compiled"]["digest"]
                != digest(format!("{}\n{}", definition_json, actions_json).as_bytes())
            || encode(&self.record)?.len()
                > MAX_AUTOMATION_RECORD_BYTES - AUTOMATION_PUBLICATION_METADATA_RESERVE_BYTES
        {
            return Err(invalid("invalid prepared definition binding"));
        }
        match definition["trigger"]["kind"].as_str() {
            Some("manual" | "schedule") => (),
            Some("event")
                if matches!(
                    definition["trigger"]["event"].as_str(),
                    Some("task.started" | "skills.updated")
                ) && matches!(
                    definition["trigger"]["scope"].as_str(),
                    Some("all" | "human" | "automation")
                ) =>
            {
                ()
            }
            _ => return Err(invalid("invalid prepared trigger")),
        }
        let steps = definition["steps"]
            .as_array()
            .filter(|a| !a.is_empty() && a.len() <= MAX_AUTOMATION_STEPS)
            .ok_or_else(|| invalid("invalid prepared step count"))?;
        let order = self.record["compiled"]["stepOrder"]
            .as_array()
            .ok_or_else(|| invalid("invalid dependency order"))?;
        let positions: BTreeMap<&str, usize> = order
            .iter()
            .enumerate()
            .map(|(i, s)| {
                s.as_str()
                    .map(|s| (s, i))
                    .ok_or_else(|| invalid("invalid dependency order"))
            })
            .collect::<io::Result<_>>()?;
        let ids: BTreeSet<&str> = steps
            .iter()
            .map(|s| text(s, "id", 160))
            .collect::<io::Result<_>>()?;
        if ids.len() != steps.len()
            || order.len() != steps.len()
            || positions.len() != order.len()
            || positions.keys().copied().collect::<BTreeSet<_>>() != ids
        {
            return Err(invalid("invalid dependency order"));
        }
        let mut action_ids = BTreeSet::new();
        for step in steps {
            let step_id = text(step, "id", 160)?;
            action_ids.insert(text(step, "action", 160)?);
            for parent in step["after"]
                .as_array()
                .ok_or_else(|| invalid("invalid dependencies"))?
            {
                let p = parent
                    .as_str()
                    .ok_or_else(|| invalid("invalid dependency"))?;
                if !positions.get(p).is_some_and(|n| *n < positions[step_id]) {
                    return Err(invalid("invalid dependency order"));
                }
            }
        }
        let actions = actions
            .as_object()
            .ok_or_else(|| invalid("invalid prepared actions"))?;
        if actions.keys().map(String::as_str).collect::<BTreeSet<_>>() != action_ids {
            return Err(invalid("invalid prepared actions"));
        }
        let mut scopes = BTreeSet::from(["automations:read", "automations:execute"]);
        for (id, action) in actions {
            if !CATALOG["actions"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v["id"] == *id && v == action))
            {
                return Err(invalid("publication requires closed action versions"));
            }
            for s in action["requiredScopes"]
                .as_array()
                .ok_or_else(|| invalid("invalid action scopes"))?
            {
                scopes.insert(s.as_str().ok_or_else(|| invalid("invalid action scope"))?);
            }
        }
        let principal = &self.record["grant"]["principal"];
        validate_principal(
            &json!({"principal":principal,"actions":actions}),
            self.user_id,
            &self.tenant_id,
        )?;
        if principal["kind"] != "system"
            || principal["subject_id"] != format!("automation:{}:{}", self.user_id, id)
            || principal["scopes"] != json!(scopes.iter().copied().collect::<Vec<_>>())
        {
            return Err(invalid("publication requires an exact scoped grant"));
        }
        if text(&self.record["grant"], "approvedBy", 256)? != self.actor_subject_id {
            return Err(invalid("publication actor differs from its approval"));
        }
        Ok(())
    }
    fn request_digest(&self) -> io::Result<String> {
        let mut basis = json!({"definition_key":self.definition_key,"expected_version":self.expected_version,
            "mode":self.mode,"actorSubjectId":self.actor_subject_id,"definitionJson":self.definition_json,"actionsJson":self.actions_json});
        if !self.record.is_null() {
            basis["stepOrder"] = self.record["compiled"]["stepOrder"].clone();
            basis["grant"] = self.record["grant"].clone();
        }
        Ok(digest(&encode(&basis)?))
    }
}

pub(crate) fn guard_draft(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &str,
) -> io::Result<()> {
    if let Some(row) = read(db, tx, AUTOMATION_DEFINITION_NAMESPACE, key)? {
        if ["grant", "triggerState", "publicationCommand"]
            .iter()
            .any(|f| !row["value"][*f].is_null())
        {
            return Err(invalid("published definitions require atomic publication"));
        }
    }
    Ok(())
}

pub(crate) fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    req: &Request,
) -> io::Result<Vec<u8>> {
    let current = read(db, tx, AUTOMATION_DEFINITION_NAMESPACE, &req.definition_key)?;
    let input_digest = req.request_digest()?;
    if let Some(row) = &current {
        let old = &row["value"];
        if old["ownerId"] != req.user_id
            || old["tenantId"].as_str().unwrap_or("") != req.tenant_id
            || req.definition_key != format!("{}{}", req.prefix(), text(old, "id", 160)?)
        {
            return Err(invalid("publication escaped its owner"));
        }
        let command = &old["publicationCommand"];
        if command["id"] == req.request_id && command["digest"] != input_digest {
            return Err(conflict("publication request evidence changed"));
        }
        if row["version"] == req.expected_version + 1
            && command["id"] == req.request_id
            && command["digest"] == input_digest
        {
            return encode(row);
        }
    }
    if current
        .as_ref()
        .and_then(|v| v["version"].as_u64())
        .unwrap_or(0)
        != req.expected_version
    {
        return Err(conflict("definition revision changed"));
    }
    if matches!(req.mode.as_str(), "pause" | "retire") && current.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "definition does not exist",
        ));
    }
    if req.mode=="publish" {
        crate::automation_project::require_active(db,tx,&req.tenant_id,req.record["definition"]["projectId"].as_str())?;
    }
    let mut last_clock_failure = current
        .as_ref()
        .filter(|v| !v["value"]["grant"].is_null())
        .and_then(|v| v["value"].get("lastClockFailure"))
        .cloned();
    if let Some(old) = &current {
        if let Some(key) = old["value"]["triggerState"]["subscriptionKey"].as_str() {
            if key != subscription_key(&req.prefix(), &old["value"]["definition"])? {
                return Err(integrity("stored subscription identity is inconsistent"));
            }
            if let Some(pointer) = read(db, tx, AUTOMATION_SUBSCRIPTION_NAMESPACE, key)? {
                records::delete(
                    db,
                    tx,
                    records::record_key(
                        tx.tenant_id(),
                        tx.owner_user_id(),
                        AUTOMATION_SUBSCRIPTION_NAMESPACE,
                        key,
                    )?,
                    AUTOMATION_SUBSCRIPTION_NAMESPACE,
                    key,
                    pointer["version"].as_u64(),
                )?;
            }
        }
        if let Some(id) = old["value"]["triggerState"]["clockJobId"].as_str() {
            let expected_id = format!(
                "automation-clock:{}",
                digest(format!("{}:{}", req.definition_key, old["version"]).as_bytes())
            );
            if id != expected_id {
                return Err(integrity("stored clock identity is inconsistent"));
            }
            {
                let job = worker_job::get(db, tx, id, req.user_id)?
                    .ok_or_else(|| integrity("published clock job is missing"))?;
                let job: Value =
                    serde_json::from_slice(&job).map_err(|_| integrity("invalid clock job"))?;
                if job["tenantId"] != req.tenant_id
                    || job["taskKind"] != "automation-clock"
                    || job["payload"]["definitionKey"] != req.definition_key
                    || job["payload"]["definitionRevision"] != old["version"]
                    || job["payload"]["definitionId"] != old["value"]["id"]
                    || job["payload"]["definitionDigest"] != old["value"]["compiled"]["digest"]
                    || job["payload"]["principal"] != old["value"]["grant"]["principal"]
                    || job["idempotencyKey"] != id
                {
                    return Err(invalid("clock escaped its owner"));
                }
                if job["status"] == "failed" {
                    let kind = job["error"]["kind"]
                        .as_str()
                        .unwrap_or("automation_clock_failed")
                        .chars()
                        .take(MAX_AUTOMATION_CLOCK_FAILURE_KIND_CHARACTERS)
                        .collect::<String>();
                    last_clock_failure = Some(json!({"jobId":id,"revision":old["version"],
                        "failedAtMs":job["terminalAtMs"],"kind":kind}));
                }
                worker_job::retire_automation_clock(
                    db,
                    tx,
                    id,
                    req.user_id,
                    &req.tenant_id,
                    &req.definition_key,
                    old["version"]
                        .as_u64()
                        .ok_or_else(|| integrity("invalid clock revision"))?,
                )?;
            }
        }
    }
    let mut value = if matches!(req.mode.as_str(), "pause" | "retire") {
        let mut v = current
            .as_ref()
            .ok_or_else(|| integrity("missing retired definition"))?["value"]
            .clone();
        let inactive: Value = serde_json::from_str(
            req.definition_json
                .as_deref()
                .ok_or_else(|| invalid("inactive source is missing"))?,
        )
        .map_err(|_| invalid("invalid inactive source"))?;
        let mut expected = v["definition"].clone();
        expected["enabled"] = json!(false);
        let actions: Value = serde_json::from_str(
            req.actions_json
                .as_deref()
                .ok_or_else(|| invalid("inactive actions are missing"))?,
        )
        .map_err(|_| invalid("invalid inactive actions"))?;
        if inactive != expected || actions != v["compiled"]["actions"] {
            return Err(invalid("state change modified the definition or actions"));
        }
        if req.mode == "retire" {
            v["deleted"] = json!(true);
        }
        v["definition"] = inactive;
        v["compiled"]["digest"] = json!(digest(
            format!(
                "{}\n{}",
                req.definition_json.as_deref().unwrap_or(""),
                req.actions_json.as_deref().unwrap_or("")
            )
            .as_bytes()
        ));
        v
    } else {
        let mut v = req.record.clone();
        v["schemaVersion"] = json!(1);
        v["id"] = v["definition"]["id"].clone();
        v["ownerId"] = json!(req.user_id);
        v["tenantId"] = v["grant"]["principal"]["tenant_id"].clone();
        v["deleted"] = json!(false);
        v["grant"]["approvedAt"] = json!(req.now_ms as f64 / 1000.0);
        v
    };
    if let Some(object) = value.as_object_mut() {
        object.remove("lastClockFailure");
    }
    if let Some(failure) = last_clock_failure {
        value["lastClockFailure"] = failure;
    }
    value["updatedAt"] = json!(req.now_ms as f64 / 1000.0);
    value["publicationCommand"] = json!({"id":req.request_id,"digest":input_digest,
        "expectedVersion":req.expected_version,"requestedBy":req.actor_subject_id,"requestedAt":req.now_ms as f64/1000.0});
    value["_commitDigest"] = json!(input_digest);
    let mut state = json!({"subscriptionKey":null,"clockJobId":null});
    let active = value["deleted"] != true && value["definition"]["enabled"] == true;
    if active && value["definition"]["trigger"]["kind"] == "event" {
        state["subscriptionKey"] = json!(subscription_key(&req.prefix(), &value["definition"])?);
    }
    if active && value["definition"]["trigger"]["kind"] == "schedule" {
        if !req.first_due_ms.is_some_and(|due| due > req.now_ms) {
            return Err(invalid("enabled schedule requires a future UTC slot"));
        }
        state["clockJobId"] = json!(format!(
            "automation-clock:{}",
            digest(format!("{}:{}", req.definition_key, req.expected_version + 1).as_bytes())
        ));
    }
    if req.first_due_ms.is_some() && state["clockJobId"].is_null() {
        return Err(invalid("only enabled schedules may enqueue a clock"));
    }
    value["triggerState"] = state;
    if let Some(key) = value["triggerState"]["subscriptionKey"].as_str() {
        let pointer = json!({"id":value["id"],"ownerId":value["ownerId"],"tenantId":value["tenantId"],
            "definitionRevision":req.expected_version+1,"definitionDigest":value["compiled"]["digest"],
            "trigger":value["definition"]["trigger"],"projectId":value["definition"]["projectId"]});
        put(
            db,
            tx,
            AUTOMATION_SUBSCRIPTION_NAMESPACE,
            key,
            &pointer,
            0,
            req.now_ms,
        )?;
    }
    if let Some(id) = value["triggerState"]["clockJobId"].as_str() {
        let payload = json!({"contract":AUTOMATION_CLOCK_JOB_CONTRACT,"schemaVersion":1,"definitionId":value["id"],"definitionRevision":req.expected_version+1,
            "definitionDigest":value["compiled"]["digest"],"definitionKey":req.definition_key,
            "publishedAtMs":req.now_ms,"firstDueMs":req.first_due_ms,"principal":value["grant"]["principal"]});
        let request_digest = digest(&encode(
            &json!({"tenantId":req.tenant_id,"taskKind":"automation-clock","payload":payload}),
        )?);
        worker_job::enqueue(
            db,
            tx,
            worker_job::EnqueueRequest {
                task_id: id.into(),
                user_id: req.user_id,
                tenant_id: req.tenant_id.clone(),
                task_kind: "automation-clock".into(),
                payload,
                idempotency_key: id.into(),
                request_digest,
                priority: 200,
                available_at_ms: req.first_due_ms.unwrap_or(0),
                now_ms: req.now_ms,
            },
        )?;
    }
    encode(&put(
        db,
        tx,
        AUTOMATION_DEFINITION_NAMESPACE,
        &req.definition_key,
        &value,
        req.expected_version,
        req.now_ms,
    )?)
}
