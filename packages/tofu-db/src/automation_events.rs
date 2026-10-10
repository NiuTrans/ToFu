//! Bounded task-start event transport inside the existing Turn start transaction.
//! Entry points: TaskStartedContext::validate and enqueue_task_start. Subscription
//! probes use owned record indexes; no timer, model or second queue is created.
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::automation_run::{digest, encode, invalid};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::*;
use crate::{versioned_document as records, worker_job};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TaskStartedContext {
    pub tenant_id: String,
    pub origin: String,
    pub automation_chain: Vec<String>,
    #[serde(
        default,
        deserialize_with = "optional_project",
        skip_serializing_if = "Option::is_none"
    )]
    pub project_id: Option<String>,
}
fn optional_project<'de, D: serde::Deserializer<'de>>(
    value: D,
) -> Result<Option<String>, D::Error> {
    String::deserialize(value).map(Some)
}
impl TaskStartedContext {
    pub(crate) fn validate(&self) -> io::Result<()> {
        if self.tenant_id.chars().count() > MAX_AUTOMATION_EVENT_TENANT_CHARACTERS
            || !matches!(self.origin.as_str(), "human" | "automation")
            || self.automation_chain.len() > MAX_AUTOMATION_EVENT_ANCESTORS
            || self.automation_chain.iter().any(|v| {
                v.is_empty() || v.chars().count() > MAX_AUTOMATION_EVENT_ANCESTOR_CHARACTERS
            })
            || self.project_id.as_ref().is_some_and(|v| {
                v.is_empty() || v.chars().count() > MAX_AUTOMATION_EVENT_PROJECT_CHARACTERS
            })
        {
            return Err(invalid("invalid automation task-start context"));
        }
        Ok(())
    }
}

pub(crate) fn enqueue_task_start(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    context: &TaskStartedContext,
    attempt_id: &str,
    task_id: &str,
    conversation_id: &str,
    turn_id: &str,
    started_at_ms: u64,
) -> io::Result<()> {
    context.validate()?;
    let owner = tx.owner_user_id();
    let prefix = format!("{}:{}:", owner, &digest(context.tenant_id.as_bytes())[..32]);
    let mut projects = vec!["*".to_owned()];
    if let Some(project) = &context.project_id {
        projects.push(digest(project.as_bytes()));
    }
    let mut subscribed = false;
    for scope in ["all", context.origin.as_str()] {
        for project in &projects {
            let key = format!("{prefix}task.started:{scope}:{project}:");
            let composite = records::record_composite_key(AUTOMATION_SUBSCRIPTION_NAMESPACE, &key)?;
            let (start, end) = EntityKey::prefix_range(
                tx.tenant_id(),
                owner,
                RECORD_DOCUMENT_NAMESPACE,
                &composite,
            )?;
            let rows: Value = serde_json::from_slice(&records::list(
                db,
                tx,
                &start,
                &end,
                AUTOMATION_SUBSCRIPTION_NAMESPACE,
                1,
            )?)
            .map_err(|_| invalid("malformed subscription probe"))?;
            if rows.as_array().is_some_and(|rows| !rows.is_empty()) {
                subscribed = true;
                break;
            }
        }
        if subscribed {
            break;
        }
    }
    if !subscribed {
        return Ok(());
    }
    let (job_id, payload) = task_payload(
        owner,
        context,
        attempt_id,
        task_id,
        conversation_id,
        turn_id,
        started_at_ms,
    );
    let request_digest = digest(&encode(
        &json!({"tenantId":context.tenant_id,"taskKind":"automation-event","payload":payload}),
    )?);
    worker_job::enqueue(
        db,
        tx,
        worker_job::EnqueueRequest {
            task_id: job_id.clone(),
            user_id: owner,
            tenant_id: context.tenant_id.clone(),
            task_kind: "automation-event".into(),
            payload,
            idempotency_key: job_id,
            request_digest,
            priority: AUTOMATION_EVENT_JOB_PRIORITY,
            available_at_ms: started_at_ms,
            now_ms: started_at_ms,
        },
    )?;
    Ok(())
}

fn task_payload(
    owner: u64,
    context: &TaskStartedContext,
    attempt_id: &str,
    task_id: &str,
    conversation_id: &str,
    turn_id: &str,
    started_at_ms: u64,
) -> (String, Value) {
    let prefix = format!("{}:{}:", owner, &digest(context.tenant_id.as_bytes())[..32]);
    let event_id = format!(
        "task-start:{}",
        digest(format!("{attempt_id}:{task_id}").as_bytes())
    );
    let job_id = format!(
        "automation-event:{}",
        digest(format!("{prefix}{event_id}").as_bytes())
    );
    let mut event = json!({"id":event_id,"kind":"task.started","occurredAt":started_at_ms as f64/1000.0,
        "origin":context.origin,"automationChain":context.automation_chain,
        "evidence":{"startedAt":started_at_ms as f64/1000.0,"taskId":task_id,
            "attemptId":attempt_id,"turnId":turn_id,"conversationId":conversation_id}});
    if let Some(project) = &context.project_id {
        event["projectId"] = json!(project);
    }
    let tenant_value = if context.tenant_id.is_empty() {
        Value::Null
    } else {
        json!(context.tenant_id)
    };
    let payload = json!({"contract":AUTOMATION_EVENT_JOB_CONTRACT,"schemaVersion":1,
        "principal":{"kind":"system","subject_id":job_id,"owner_user_id":owner,"tenant_id":tenant_value,
            "scopes":["automations:execute","automations:read"]},"event":event,"delivery":{"index":0,"afterKey":"","processed":0}});
    (job_id, payload)
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeliveryCursor {
    pub index: usize,
    pub after_key: String,
    pub processed: u64,
}
impl Default for DeliveryCursor {
    fn default() -> Self {
        Self {
            index: 0,
            after_key: String::new(),
            processed: 0,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub user_id: u64,
    pub tenant_id: String,
    pub job_id: String,
    pub worker_id: String,
    pub fencing_token: u64,
    pub expected_payload: Value,
    pub next_delivery: Option<DeliveryCursor>,
    pub mode: String,
    pub error_kind: String,
    pub now_ms: u64,
}
fn cursor(payload: &Value) -> io::Result<DeliveryCursor> {
    match payload.get("delivery") {
        None => Ok(DeliveryCursor::default()),
        Some(v) => {
            serde_json::from_value(v.clone()).map_err(|_| invalid("invalid delivery cursor"))
        }
    }
}
fn prefixes(owner: u64, context: &TaskStartedContext) -> Vec<String> {
    let base = format!(
        "{}:{}:task.started:",
        owner,
        &digest(context.tenant_id.as_bytes())[..32]
    );
    let mut projects = vec!["*".to_owned()];
    if let Some(p) = &context.project_id {
        projects.push(digest(p.as_bytes()));
    }
    let mut ranges = Vec::new();
    for scope in ["all", context.origin.as_str()] {
        for project in &projects { ranges.push(format!("{base}{scope}:{project}:")); }
    }
    ranges
}
impl Request {
    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        Ok(
            encode(&serde_json::to_value(self).map_err(|_| invalid("event request encoding"))?)?
                .len(),
        )
    }
    fn source(&self) -> io::Result<(Vec<String>, DeliveryCursor)> {
        let event = &self.expected_payload["event"];
        let ev = &event["evidence"];
        let text = |key: &str, max: usize| crate::automation_run::text(ev, key, max);
        let attempt = text("attemptId", 128)?;
        let task = text("taskId", 256)?;
        let conv = text("conversationId", 256)?;
        let turn = text("turnId", 128)?;
        let started = ev["startedAt"]
            .as_f64()
            .filter(|v| {
                v.is_finite() && *v > 0.0 && *v <= MAX_AUTOMATION_SAFE_INTEGER as f64 / 1000.0
            })
            .ok_or_else(|| invalid("invalid actual event start"))?;
        let mut raw = json!({"tenantId":self.tenant_id,"origin":event["origin"],"automationChain":event["automationChain"]});
        if let Some(p) = event.get("projectId") {
            raw["projectId"] = p.clone();
        }
        let context: TaskStartedContext =
            serde_json::from_value(raw).map_err(|_| invalid("invalid event context"))?;
        context.validate()?;
        let (id, mut expected) = task_payload(
            self.user_id,
            &context,
            attempt,
            task,
            conv,
            turn,
            (started * 1000.0).round() as u64,
        );
        let mut actual = self.expected_payload.clone();
        actual
            .as_object_mut()
            .ok_or_else(|| invalid("invalid event payload"))?
            .remove("delivery");
        expected.as_object_mut().unwrap().remove("delivery");
        if id != self.job_id || actual != expected {
            return Err(invalid("event escaped its frozen producer identity"));
        }
        let ranges = prefixes(self.user_id, &context);
        let cur = cursor(&self.expected_payload)?;
        if cur.index > ranges.len()
            || cur.processed > MAX_AUTOMATION_SAFE_INTEGER
            || cur.after_key.chars().count() > 512
            || (cur.index == ranges.len() && !cur.after_key.is_empty())
            || (!cur.after_key.is_empty() && !cur.after_key.starts_with(&ranges[cur.index]))
        {
            return Err(invalid("event cursor escaped its scope"));
        }
        Ok((ranges, cur))
    }
    pub(crate) fn validate(&self, owner: u64, authority_now: u64) -> io::Result<()> {
        if self.user_id != owner
            || owner == 0
            || owner > MAX_AUTOMATION_SAFE_INTEGER
            || self.worker_id.is_empty()
            || self.worker_id.chars().count() > 200
            || self.fencing_token == 0
            || self.fencing_token > MAX_AUTOMATION_SAFE_INTEGER
            || self.now_ms == 0
            || self.now_ms > MAX_AUTOMATION_SAFE_INTEGER
            || self.now_ms.saturating_sub(authority_now) > MAX_AUTOMATION_EVENT_CLOCK_SKEW_MS
            || self.literal_bytes()? > MAX_AUTOMATION_EVENT_REQUEST_BYTES
            || self.error_kind.chars().count() > MAX_AUTOMATION_EVENT_ERROR_CHARACTERS
        {
            return Err(invalid("invalid event settlement envelope"));
        }
        let (ranges, cur) = self.source()?;
        if self.mode == "advance" {
            let next = self
                .next_delivery
                .as_ref()
                .ok_or_else(|| invalid("missing next cursor"))?;
            let delta = next
                .processed
                .checked_sub(cur.processed)
                .ok_or_else(|| invalid("event cursor regressed"))?;
            let same = next.index == cur.index
                && cur.index < ranges.len()
                && next.after_key.starts_with(&ranges[cur.index])
                && next.after_key > cur.after_key
                && delta > 0;
            let following = next.index == cur.index + 1
                && next.index <= ranges.len()
                && next.after_key.is_empty();
            if delta > MAX_AUTOMATION_EVENT_BATCH_ROWS as u64
                || next.processed > MAX_AUTOMATION_SAFE_INTEGER
                || next.after_key.chars().count() > 512
                || !(same || following)
                || !self.error_kind.is_empty()
            {
                return Err(invalid("invalid bounded event advance"));
            }
        } else if !matches!(self.mode.as_str(), "succeeded" | "cancelled" | "failed")
            || self.next_delivery.is_some()
            || (self.mode == "succeeded" && cur.index != ranges.len())
            || (self.error_kind.is_empty() == (self.mode == "failed"))
        {
            return Err(invalid("invalid event retirement"));
        }
        Ok(())
    }
}
pub(crate) fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    req: &Request,
) -> io::Result<Vec<u8>> {
    let Some(raw) = worker_job::get(db, tx, &req.job_id, req.user_id)? else {
        if req.mode == "advance" {
            return encode(&json!({"ok":false,"error":"producer_retired"}));
        }
        if req.mode == "failed" {
            let key = format!(
                "{}:{}:{}",
                req.user_id,
                &digest(req.tenant_id.as_bytes())[..32],
                req.job_id
            );
            if let Some(failure) =
                crate::automation_run::read(db, tx, AUTOMATION_EVENTFAILURE_NAMESPACE, &key)?
            {
                if failure["value"]["kind"] != req.error_kind {
                    return Err(crate::automation_run::conflict(
                        "event failure evidence changed",
                    ));
                }
            }
        }
        return encode(&json!({"ok":true,"applied":false,"retired":true}));
    };
    let job: Value =
        serde_json::from_slice(&raw).map_err(|_| invalid("malformed event transport"))?;
    if job["taskKind"] != "automation-event"
        || job["tenantId"] != req.tenant_id
        || job["idempotencyKey"] != req.job_id
    {
        return Err(invalid("event transport escaped its owner"));
    }
    let mut current = job["payload"].clone();
    if current.get("delivery").is_none() {
        current["delivery"] = json!(DeliveryCursor::default());
    }
    let mut expected = req.expected_payload.clone();
    if expected.get("delivery").is_none() {
        expected["delivery"] = json!(DeliveryCursor::default());
    }
    if req.mode == "advance" {
        let mut replay = expected.clone();
        replay["delivery"] = json!(req.next_delivery);
        if current == replay {
            return encode(&json!({"ok":true,"applied":false,"retired":false,"payload":current}));
        }
    }
    if current != expected {
        return Err(crate::automation_run::conflict("event cursor changed"));
    }
    let proof: Value = serde_json::from_slice(&worker_job::claim_state(
        db,
        tx,
        &req.job_id,
        &req.worker_id,
        req.fencing_token,
        req.now_ms,
    )?)
    .map_err(|_| invalid("malformed event claim proof"))?;
    if proof["ok"] != true {
        return encode(&json!({"ok":false,"error":"producer_fence_lost"}));
    }
    let cancelled = proof["cancelSequence"].as_u64().unwrap_or(0) > 0;
    if cancelled && req.mode != "cancelled" {
        return encode(&json!({"ok":false,"error":"cancellation_pending"}));
    }
    if !cancelled && req.mode == "cancelled" {
        return Err(invalid("event cancellation requires owner intent"));
    }
    if req.mode == "advance" {
        current["delivery"] = json!(req.next_delivery);
        worker_job::settle_automation_event(
            db,
            tx,
            &req.job_id,
            req.user_id,
            &req.tenant_id,
            &job["payload"],
            Some(&current),
            req.now_ms,
        )?;
        return encode(&json!({"ok":true,"applied":true,"retired":false,"payload":current}));
    }
    if req.mode == "failed" {
        let key = format!(
            "{}:{}:{}",
            req.user_id,
            &digest(req.tenant_id.as_bytes())[..32],
            req.job_id
        );
        crate::automation_run::put(
            db,
            tx,
            AUTOMATION_EVENTFAILURE_NAMESPACE,
            &key,
            &json!({"id":req.job_id,"ownerId":req.user_id,"tenantId":req.tenant_id,"event":expected["event"],
                "delivery":expected["delivery"],"kind":req.error_kind,"failedAt":req.now_ms as f64/1000.0}),
            0,
            req.now_ms,
        )?;
    }
    worker_job::settle_automation_event(
        db,
        tx,
        &req.job_id,
        req.user_id,
        &req.tenant_id,
        &job["payload"],
        None,
        req.now_ms,
    )?;
    encode(&json!({"ok":true,"applied":true,"retired":true}))
}
