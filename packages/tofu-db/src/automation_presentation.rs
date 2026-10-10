//! Identity-only publication of producer-bound time outputs in run progress.
//! Canonical attempt reads prove ownership and physical timing; immutable run
//! steps remain the content authority. No additional messages or queue.

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::automation_run::{digest, invalid, put, read, text};
use serde_json::{json, Value};
use std::io;

pub(crate) fn publish(
    db: &AuthorityDatabase, tx: &mut AuthorityTransaction, prefix: &str,
    previous: &Value, run: &Value, now: u64,
) -> io::Result<()> {
    let event = &run["trigger"];
    if !event["id"].as_str().is_some_and(|v| v.starts_with("task-start:")) {
        return Ok(());
    }
    if event["kind"] != "task.started" {
        return Err(invalid("presentation producer kind differs"));
    }
    for step in run["snapshot"]["steps"].as_array().ok_or_else(|| invalid("missing steps"))? {
        if step["action"] != "task.show_time" { continue; }
        let id = text(step, "id", 64)?;
        let current = &run["steps"][id];
        let old = &previous["steps"][id];
        if old["status"] == "succeeded" {
            if current != old { return Err(invalid("published time output is immutable")); }
            continue;
        }
        if current["status"] != "succeeded" { continue; }
        let evidence = &event["evidence"];
        let attempt_id = text(evidence, "attemptId", 128)?;
        let task = text(evidence, "taskId", 200)?;
        let conversation = text(evidence, "conversationId", 256)?;
        text(evidence, "turnId", 256)?;
        let expected_id = format!("task-start:{}", digest(format!("{attempt_id}:{task}").as_bytes()));
        let effect = text(current, "effectKey", 256)?;
        if event["id"] != expected_id || old["status"] != "dispatching" || old["effectKey"] != effect {
            return Err(invalid("presentation requires its committed producer intent"));
        }
        let raw = crate::turn::attempt_get(db,tx,attempt_id)?
            .ok_or_else(|| invalid("presentation source attempt is not owned"))?;
        let attempt: Value = serde_json::from_slice(&raw).map_err(|_| invalid("malformed attempt"))?;
        if ["attemptId","taskId","turnId","conversationId"].iter().any(|k| attempt[*k] != evidence[*k])
            || !attempt["startedAt"].as_u64().is_some_and(|ms| ms > 0 && evidence["startedAt"].as_f64() == Some(ms as f64 / 1000.0))
            || evidence["startedAt"] != event["occurredAt"] {
            return Err(invalid("presentation has no matching owned physical start"));
        }
        let output = &current["output"];
        let zone = current["arguments"].get("timezone").and_then(Value::as_str).unwrap_or("UTC");
        if !output.as_object().is_some_and(|v| v.len() == 3)
            || output["startedAt"] != evidence["startedAt"] || output["timezone"] != zone {
            return Err(invalid("presentation output differs from frozen start evidence"));
        }
        text(output,"text",200)?;
        text(output,"timezone",100)?;
        let identity = digest(effect.as_bytes());
        let key = format!("{prefix}{}:{}:{identity}", digest(conversation.as_bytes()), digest(attempt_id.as_bytes()));
        let pointer = json!({"id":identity,"runId":run["id"],"stepId":id,
            "ownerId":run["ownerId"],"tenantId":run["tenantId"],"conversationId":conversation,
            "attemptId":attempt_id,"turnId":evidence["turnId"]});
        let namespace = crate::generated_tofudb_ir::AUTOMATION_PRESENTATION_NAMESPACE;
        if let Some(existing) = read(db,tx,namespace,&key)? {
            if existing["value"] != pointer { return Err(invalid("presentation identity collision")); }
        } else { put(db,tx,namespace,&key,&pointer,0,now)?; }
    }
    Ok(())
}
