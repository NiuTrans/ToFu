//! Shared accepted-attempt and source-retirement validation for native journals.
//! Storage supplies owner-filtered observations under its transaction locks.
//! These rules never open authority or write rows; pending retirement uses the
//! common kernel writer-death proof and preserves the original frozen invoice.
use crate::checkpoint_journal::{Error, Handoff, Header, ResumeLineage};
use serde_json::Value;

pub fn validate_resume_target(owner: u64, request: &Handoff<'_>) -> Result<(), Error> {
    let target = request.target_header["task_id"].as_str().ok_or(Error::Invalid)?;
    let lineage = request.lineage;
        if request.target_header["owner_user_id"].as_u64() != Some(owner)
            || request.target_snapshot["owner_user_id"].as_u64() != Some(owner)
            || request.target_snapshot["task_id"] != target || target == lineage.source_task_id
            || request.source_header["task_id"] != lineage.source_task_id
            || !matches!(request.target_header["phase"].as_str(), Some("completed" | "tools_pending"))
            || (request.target_header["phase"] == "tools_pending")
                != (request.target_snapshot.get("tools_pending") == Some(&Value::Bool(true)))
            || request.target_header["lease_generation"] != 1
            || request.target_header["lease"].as_str().is_none_or(str::is_empty)
            || request.target_header["epoch"] != request.source_header["epoch"]
            || request.target_header["binding"] != request.source_header["binding"]
            || request.target_header["next_round"] != request.source_header["next_round"]
        { return Err(Error::Invalid); }
    Ok(())
}

pub fn validate_resume_lineage(lineage: &ResumeLineage, target: &str, accepted: &Value,
    source_attempt: &Value, turn: &Value) -> Result<(), Error> {
        let receipt = &accepted["config"]["_resumeSource"];
        if accepted["attemptId"] != lineage.target_attempt_id || accepted["taskId"] != target
            || accepted["conversationId"] != lineage.conversation_id || accepted["turnId"] != lineage.turn_id
            || accepted["status"] != "running"
            || !matches!(accepted["operation"].as_str(), Some("continue" | "checkpoint_resume" | "answer_guidance"))
            || source_attempt["attemptId"] != lineage.source_attempt_id
            || source_attempt["taskId"] != lineage.source_task_id
            || source_attempt["conversationId"] != lineage.conversation_id || source_attempt["turnId"] != lineage.turn_id
            || !matches!(source_attempt["status"].as_str(), Some("interrupted" | "truncated" | "failed"))
            || receipt.as_object().is_none_or(|fields| fields.len() != 4)
            || receipt["attemptId"] != lineage.source_attempt_id || receipt["taskId"] != lineage.source_task_id
            || receipt["status"] != source_attempt["status"] || receipt["cause"].as_str().is_none()
            || turn["turnId"] != lineage.turn_id || turn["conversationId"] != lineage.conversation_id
            || turn["currentAttemptId"] != lineage.target_attempt_id || turn["status"] != "running"
        { return Err(Error::Ineligible); }
    Ok(())
}

pub struct Retirement<'a> {
    expected_version: u64,
    header: &'a Value,
    phase: &'a str,
    prove_writer_death: bool,
}
impl<'a> Retirement<'a> {
    pub fn new(owner: u64, task: &'a str, expected_version: u64, header: &'a Value,
        phase: &'a str, prove_writer_death: bool) -> Result<Self, Error> {
        if expected_version == 0 || header["task_id"] != task
            || header["owner_user_id"].as_u64() != Some(owner)
            || header["phase"] != "terminal" || !header["lease"].is_null() {
            return Err(Error::Invalid);
        }
        Ok(Self {expected_version,header,phase,prove_writer_death})
    }
    /// Call before reading the potentially large saved snapshot.
    pub fn validate_header(&self, observed: &Header) -> Result<(), Error> {
        if observed.version != self.expected_version { return Err(Error::Conflict); }
        let actual = &observed.value;
        if actual["phase"] != self.phase { return Err(Error::Ineligible); }
        match self.phase {
            "completed" if actual["lease"].is_null() => {},
            "tools_pending" if actual["lease"].as_str().is_some_and(|v| !v.is_empty()) => {
                let writer = actual.get("writer_process").ok_or(Error::Ineligible)?;
                if self.prove_writer_death && !crate::checkpoint_writer::quiescent(writer)? {
                    return Err(Error::Ineligible);
                }
            },
            _ => return Err(Error::Ineligible),
        }
        Ok(())
    }
    /// Complete validation against the same transaction-fenced header.
    pub fn validate_snapshot(&self, observed: &Header, snapshot: Option<&Value>) -> Result<(), Error> {
        let mut actual = observed.value.clone();
        if self.phase == "tools_pending" {
            let snapshot = snapshot.ok_or(Error::Ineligible)?;
            if let Some(invoice) = crate::checkpoint_journal::handoff_wallet_invoice(snapshot)? {
                actual["wallet_settlement"] = invoice;
            }
        }
        actual["phase"] = Value::String("terminal".into());
        actual["lease"] = Value::Null;
        if actual != *self.header { return Err(Error::Conflict); }
        Ok(())
    }
}
