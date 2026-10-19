//! Retired-executor observations for exact unresolved local file occurrences.
//! Called only by Turn retirement before its atomic settlement/projection write.
//! Contract: docs/contracts/TOOL_EFFECT_RECOVERY.md; no query or effect is executed.
use serde_json::{json, Map, Value};
use std::collections::HashMap;

fn hex(value: &Value, size: usize) -> bool {
    value.as_str().is_some_and(|s| s.len() == size && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)))
}

pub(super) fn record_interrupted_file_effects(projection: &mut Map<String, Value>,
    conversation: &str, turn: &str, attempt: &str, task: &str) {
    if [conversation, turn, attempt, task].iter().any(|s| s.is_empty()) { return; }
    let Some(rounds) = projection.get_mut("toolRounds").and_then(Value::as_array_mut) else { return; };
    let mut counts = HashMap::new();
    for row in rounds.iter() {
        if let Some(operation) = row.get("executionEffect").and_then(|e| e.get("operationId")).and_then(Value::as_str) {
            *counts.entry(operation.to_owned()).or_insert(0usize) += 1;
        }
    }
    for row in rounds {
        if row["toolName"] != "write_file" || row["taskId"] != task || row["attemptId"] != attempt
            || !row["toolContent"].is_null() || row["_providerAttemptDiscarded"] == true
            || row.get("toolModelContent").is_some_and(|v| !v.is_null() && v.as_array().is_none_or(|a| !a.is_empty()))
        { continue; }
        let effect = &row["executionEffect"];
        if effect["scope"] != "attempt" || effect["recovery"] != "reconcile"
            || !matches!(effect["state"].as_str(), Some("entered" | "unknown")) { continue; }
        let operation = &effect["operationId"];
        let reference = &effect["inspection"];
        let publication = &effect["reconciliation"];
        if !hex(operation, 32) || counts.get(operation.as_str().unwrap_or("")) != Some(&1)
            || reference["operationId"] != *operation || reference["conversationId"] != conversation
            || reference["turnId"] != turn || !hex(&reference["ownerScope"], 24)
            || publication["kind"] != "project_file_write" || publication["taskId"] != task { continue; }
        let arguments = json!({"operation_id": operation, "turn_id": turn}).to_string();
        let action = format!("Call inspect_tool_effect({arguments}). Do not repeat the write automatically.");
        // The shared typed tool-error projection bounds nextAction to 300 characters.
        // Preserve the gap instead of publishing an instruction with a clipped identity.
        if action.chars().count() > 300 { continue; }
        let content = json!({"status":"error", "code":"tool_effect_unknown", "retryable":false,
            "message":"The producing executor was retired before a tool response was captured. File publication remains unknown. This is an interruption observation, not a handler response, publication proof, task completion or approval.",
            "nextAction":action});
        row["toolContent"] = Value::String(content.to_string());
        row["status"] = Value::String("aborted".into());
        row["executionEffect"]["state"] = Value::String("unknown".into());
    }
}
