//! Bounded handoff bytes and pure validation invoked inside the host transaction.
//! The same lineage/retirement owner serves TofuDB; no Python policy or SQL here.
use super::*;
use crate::checkpoint_lineage::{validate_resume_lineage, validate_resume_target, Retirement};

#[derive(Serialize)]
struct Source<'a> { task_id: &'a str, expected_version: u64, header: &'a Value }
#[derive(Serialize)]
struct Request<'a> {
    #[serde(flatten)] base: super::Request<'a>,
    source: Source<'a>, resume: &'a ResumeLineage,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnedSource { task_id: String, expected_version: u64, header: Value }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnedRequest {
    format: String, operation: String, tenant_id: u64, owner_user_id: u64,
    task_id: String, header_bytes: usize, snapshot_bytes: usize, expected_version: u64,
    header: Value, snapshot: Value, source: OwnedSource, resume: ResumeLineage,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Observations {
    accepted: Value, source_attempt: Value, turn: Value,
    source_header: Value, source_version: u64, source_snapshot: Option<Value>,
}
impl CallbackStore {
    pub(super) fn handoff_callback(&self, request: journal::Handoff<'_>) -> Result<u64, Error> {
        validate_resume_target(self.owner, &request)?;
        let target = request.target_header["task_id"].as_str().ok_or(Error::Invalid)?;
        self.task(self.owner, target)?;
        self.task(self.owner, &request.lineage.source_task_id)?;
        self.value(request.source_header, &request.lineage.source_task_id, self.header_bytes)?;
        self.value(request.target_header, target, self.header_bytes)?;
        self.value(request.target_snapshot, target, self.snapshot_bytes)?;
        let request = Request {
            base: super::Request {format:FORMAT, operation:"handoff", tenant_id:self.tenant,
                owner_user_id:self.owner, task_id:target, header_bytes:self.header_bytes,
                snapshot_bytes:self.snapshot_bytes, expected_version:0,
                header:Some(request.target_header), snapshot:Some(request.target_snapshot)},
            source: Source {task_id:&request.lineage.source_task_id, expected_version:request.source_version,
                header:request.source_header}, resume:request.lineage,
        };
        let response = self.send(&request, 0)?;
        if response.version != Some(1) || response.value.is_some() { return Err(Error::Storage); }
        Ok(1)
    }
}

pub(crate) fn validate(tenant: u64, owner: u64, document_bytes: usize, identity_bytes: usize,
    request: &[u8], observations: &[u8]) -> Result<(), Error> {
    let request: OwnedRequest = serde_json::from_slice(request).map_err(|_| Error::Invalid)?;
    let observed: Observations = serde_json::from_slice(observations).map_err(|_| Error::Invalid)?;
    let identity = |value: &str| !value.is_empty() && value.len() <= identity_bytes;
    if request.format != FORMAT || request.operation != "handoff" || request.tenant_id != tenant
        || owner == 0 || request.owner_user_id != owner || request.expected_version != 0
        || request.header_bytes == 0 || request.header_bytes > document_bytes
        || request.snapshot_bytes == 0 || request.snapshot_bytes > document_bytes
        || !identity(&request.task_id) || request.source.task_id != request.resume.source_task_id
        || [&request.resume.source_task_id, &request.resume.target_attempt_id, &request.resume.source_attempt_id,
            &request.resume.conversation_id, &request.resume.turn_id].iter().any(|s| !identity(s))
        || request.header["task_id"] != request.task_id || request.resume.reserved_micro > i64::MAX as u64 {
        return Err(Error::Invalid);
    }
    for value in [&request.header, &request.source.header, &observed.source_header] { encode(value, request.header_bytes)?; }
    encode(&request.snapshot, request.snapshot_bytes)?;
    if let Some(snapshot) = &observed.source_snapshot { encode(snapshot, request.snapshot_bytes)?; }
    let handoff = journal::Handoff {lineage:&request.resume, source_version:request.source.expected_version,
        source_header:&request.source.header, target_header:&request.header, target_snapshot:&request.snapshot};
    validate_resume_target(owner, &handoff)?;
    validate_resume_lineage(&request.resume, &request.task_id, &observed.accepted, &observed.source_attempt, &observed.turn)?;
    let retirement = Retirement::new(owner, &request.source.task_id, request.source.expected_version,
        &request.source.header, request.header["phase"].as_str().ok_or(Error::Invalid)?, true)?;
    let header = Header {version:observed.source_version, value:observed.source_header};
    retirement.validate_header(&header)?;
    retirement.validate_snapshot(&header, observed.source_snapshot.as_ref())
}
