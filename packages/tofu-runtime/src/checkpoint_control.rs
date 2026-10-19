//! Shared bounded checkpoint control for an explicitly owned storage runtime.
//! Registry owns volatile capability IDs only; OwnedLease owns all transitions.
//! Buffer admission precedes effects; snapshots are decoded only by the worker.
#[path = "checkpoint_control/runtime.rs"]
mod runtime;
pub use runtime::CheckpointControlRuntime;
use serde::Deserialize;
use serde_json::{json, value::RawValue, Value};
use std::{
    collections::BTreeMap,
    io,
    sync::{Arc, LazyLock, Mutex},
};
use crate::checkpoint_journal::{Error, OwnedLease, ResumeLineage, Scope};
#[path = "checkpoint_control/goal.rs"]
mod goal;
use goal::{GoalClaim,GoalHandoff};

static CONTRACT: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../contracts/native_checkpoint_control_v1.schema.json"
    ))
    .expect("checkpoint control contract")
});
fn headroom() -> usize {
    CONTRACT["limits"]["responseHeadroomBytes"]
        .as_u64()
        .expect("response headroom") as usize
}
fn present<'de, T: Deserialize<'de>, D: serde::Deserializer<'de>>(
    decoder: D,
) -> Result<Option<T>, D::Error> {
    T::deserialize(decoder).map(Some)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Payload<'a> {
    format: &'a str,
    #[serde(default, deserialize_with = "present")]
    capability_id: Option<u64>,
    #[serde(default, deserialize_with = "present")]
    parameters: Option<Binding>,
    #[serde(default, deserialize_with = "present")]
    resume: Option<ResumeBinding>,
    #[serde(default, deserialize_with = "present")]
    invoice: Option<InvoiceLookup>,
    #[serde(default, deserialize_with = "present")]
    frontier: Option<FrontierLookup>,
    #[serde(default, deserialize_with = "present")]
    goal_claim: Option<GoalClaim>,
    #[serde(default, deserialize_with = "present")]
    goal_handoff: Option<GoalHandoff>,
    #[serde(default, deserialize_with = "present")]
    round: Option<u64>,
    #[serde(default, deserialize_with = "present")]
    status: Option<String>,
    #[serde(default, deserialize_with = "present")]
    suspended: Option<bool>,
    #[serde(borrow, default, deserialize_with = "present")]
    state: Option<&'a RawValue>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    owner_user_id: u64,
    task_id: String,
    epoch: String,
    binding: String,
    token: String,
    header_bytes: usize,
    snapshot_bytes: usize,
    identity_bytes: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResumeBinding {
    conversation_id: String,
    turn_id: String,
    source_attempt_id: String,
    target_attempt_id: String,
    source_task_id: String,
    reserved_micro: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InvoiceLookup {
    owner_user_id: u64,
    task_id: String,
    header_bytes: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FrontierLookup {
    owner_user_id: u64,
    task_id: String,
    header_bytes: usize,
    snapshot_bytes: usize,
    identity_bytes: usize,
}
struct Entry {
    lease: Arc<OwnedLease>,
    response_bytes: usize,
    finish: Mutex<Option<(crate::task::TaskStatus, bool)>>,
    group_digest: Option<Arc<str>>,
}
#[derive(Default)]
struct Entries {
    next: u64,
    live: BTreeMap<u64, Arc<Entry>>,
    // A successful delivery receipt exists only until a member is first used.
    // Entries and receipts share this registry/runtime lifetime, not a new log.
    groups: BTreeMap<String, Vec<u64>>,
}
pub struct Registry {
    entries: Mutex<Entries>,
    document_maximum_bytes: usize,
}
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "invalid native checkpoint control request",
    )
}
fn parse<'a>(operation: &str, bytes: &'a [u8], document_maximum_bytes: usize) -> io::Result<Payload<'a>> {
    let p: Payload<'a> = serde_json::from_slice(bytes).map_err(|_| invalid())?;
    if p.format != "tofu.native-checkpoint-control/v1"
        || matches!(operation, "create" | "recover" | "handoff") != p.parameters.is_some()
        || !matches!(operation, "create" | "recover" | "handoff" | "invoice" | "frontier" | "goal_claim" | "goal_handoff") != p.capability_id.is_some()
        || (operation == "invoice") != p.invoice.is_some()
        || (operation == "frontier") != p.frontier.is_some()
        || (operation == "goal_claim") != p.goal_claim.is_some()
        || (operation == "goal_handoff") != p.goal_handoff.is_some()
        || p.goal_claim.as_ref().is_some_and(|query|!query.valid(document_maximum_bytes))
        || p.goal_handoff.as_ref().is_some_and(|query|!query.valid(document_maximum_bytes))
        || p.frontier.as_ref().is_some_and(|query| query.owner_user_id == 0 || query.task_id.is_empty()
            || query.identity_bytes == 0 || query.header_bytes == 0 || query.snapshot_bytes == 0
            || query.header_bytes > document_maximum_bytes
            || query.snapshot_bytes > document_maximum_bytes)
        || p.invoice.as_ref().is_some_and(|query| query.owner_user_id == 0 || query.task_id.is_empty()
            || query.header_bytes == 0 || query.header_bytes > document_maximum_bytes)
        || (operation == "handoff") != p.resume.is_some()
        || p.resume.as_ref().is_some_and(|value| value.reserved_micro > i64::MAX as u64)
        || p.capability_id == Some(0)
        || (operation == "begin") != p.round.is_some()
        || matches!(operation, "complete" | "pending") != p.state.is_some()
        || (operation == "finish") != p.status.is_some()
        || (operation == "finish") != p.suspended.is_some()
        || p.status
            .as_ref()
            .is_some_and(|s| !["done", "error", "aborted"].contains(&s.as_str()))
        || ![
            "create", "recover", "handoff", "load", "begin", "pending", "complete", "seal", "release", "forget", "finish", "invoice", "frontier", "goal_claim", "goal_handoff",
        ]
        .contains(&operation)
    {
        return Err(invalid());
    }
    Ok(p)
}
fn code(error: Error) -> &'static str {
    match error {
        Error::Invalid => "invalid",
        Error::Conflict => "conflict",
        Error::Ineligible => "ineligible",
        Error::Storage => "storage",
        Error::Poisoned => "poisoned",
        Error::Oversize => "oversize",
    }
}
impl Registry {
    /// The selected storage authority supplies its existing document limit.
    pub fn new(document_maximum_bytes: usize) -> io::Result<Self> {
        if document_maximum_bytes == 0 || document_maximum_bytes.checked_add(headroom()).is_none() { return Err(invalid()); }
        Ok(Self { entries: Mutex::new(Entries::default()), document_maximum_bytes })
    }
    /// Volatile capability count for lifecycle and resource observation.
    pub fn live_capabilities(&self) -> io::Result<usize> {
        self.entries.lock().map(|entries| entries.live.len())
            .map_err(|_| io::Error::other("checkpoint capability registry poisoned"))
    }
    pub fn response_bound(&self, operation: &str, bytes: &[u8]) -> io::Result<usize> {
        let p = parse(operation, bytes, self.document_maximum_bytes)?;
        if let Some(query)=p.goal_claim {return query.evidence_bytes.checked_add(headroom()).ok_or_else(invalid);}
        if let Some(query)=p.goal_handoff {return query.response_bound();}
        if let Some(query) = p.invoice {
            return query.header_bytes.checked_add(headroom()).ok_or_else(invalid);
        }
        if let Some(query) = p.frontier {
            return query.snapshot_bytes.checked_add(query.header_bytes)
                .and_then(|bytes| bytes.checked_add(headroom())).ok_or_else(invalid);
        }
        if let Some(b) = p.parameters {
            if b.snapshot_bytes == 0
                || b.header_bytes == 0
                || b.identity_bytes == 0
                || b.snapshot_bytes > self.document_maximum_bytes
                || b.header_bytes > self.document_maximum_bytes
            {
                return Err(invalid());
            }
            return b.snapshot_bytes.checked_add(headroom()).ok_or_else(invalid);
        }
        let entries = self
            .entries
            .lock()
            .map_err(|_| io::Error::other("checkpoint capability registry poisoned"))?;
        Ok(entries
            .live
            .get(&p.capability_id.unwrap())
            .map_or(headroom(), |entry| entry.response_bytes))
    }
    pub fn execute<R: CheckpointControlRuntime>(
        &self,
        runtime: &R,
        operation: &str,
        bytes: &[u8],
        deadline: u64,
    ) -> io::Result<Vec<u8>> {
        let p = parse(operation, bytes, self.document_maximum_bytes)?;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_millis();
        if now >= u128::from(deadline) {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "checkpoint deadline elapsed before effects",
            ));
        }
        let mut id = p.capability_id;
        let result: Result<Option<Value>, Error> = if let Some(query)=p.goal_claim {
            runtime.checkpoint_goal_claim(query.owner_user_id,query.header_bytes,query.snapshot_bytes,
                &query.request,query.evidence_bytes).map(Some)
        } else if let Some(query)=p.goal_handoff {
            self.execute_goal_handoff(runtime,query).map(Some)
        } else if let Some(query) = p.frontier {
            runtime.checkpoint_frontier(query.owner_user_id, &query.task_id,
                query.header_bytes, query.snapshot_bytes, query.identity_bytes)
        } else if let Some(query) = p.invoice {
            runtime.checkpoint_invoice(query.owner_user_id, &query.task_id, query.header_bytes)
        } else if let Some(b) = p.parameters {
            let next = {
                let mut entries = self
                    .entries
                    .lock()
                    .map_err(|_| io::Error::other("checkpoint capability registry poisoned"))?;
                entries.next = entries
                    .next
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("checkpoint capability ID exhausted"))?;
                entries.next
            };
            let acquire = if operation == "recover" {
                R::checkpoint_recover
            } else {
                R::checkpoint_lease
            };
            let scope = Scope {
                    owner: b.owner_user_id,
                    task_id: b.task_id,
                    epoch: b.epoch,
                    binding: b.binding,
                };
            let lease = if let Some(resume) = p.resume {
                runtime.checkpoint_handoff(scope, b.header_bytes, b.snapshot_bytes, b.identity_bytes,
                    &b.token, &ResumeLineage { conversation_id: resume.conversation_id, turn_id: resume.turn_id,
                        source_attempt_id: resume.source_attempt_id, target_attempt_id: resume.target_attempt_id,
                        source_task_id: resume.source_task_id, reserved_micro: resume.reserved_micro })
            } else { acquire(runtime, scope, b.header_bytes, b.snapshot_bytes, b.identity_bytes, &b.token) };
            match lease {
                Ok(lease) => {
                    self.entries
                        .lock()
                        .map_err(|_| io::Error::other("checkpoint capability registry poisoned"))?
                        .live
                        .insert(
                            next,
                            Arc::new(Entry {
                                lease: Arc::new(lease),
                                response_bytes: b.snapshot_bytes + headroom(),
                                finish: Mutex::new(None),
                                group_digest: None,
                            }),
                        );
                    id = Some(next);
                    Ok(None)
                }
                Err(error) => Err(error),
            }
        } else {
            let lease = {
                let mut entries = self
                    .entries
                    .lock()
                    .map_err(|_| io::Error::other("checkpoint capability registry poisoned"))?;
                let entry=entries.live.get(&id.unwrap()).cloned();
                if let Some(digest)=entry.as_ref().and_then(|entry|entry.group_digest.as_ref()) {
                    entries.groups.remove(digest.as_ref());
                }
                entry
            };
            match lease {
                None => Err(Error::Ineligible),
                Some(entry) => match operation {
                    "load" => entry.lease.load(),
                    "begin" => entry.lease.begin(p.round.unwrap()).map(|_| None),
                    "complete" | "pending" => {
                        let state =
                            serde_json::from_str(p.state.unwrap().get()).map_err(|_| invalid())?;
                        if operation == "pending" { entry.lease.pending(&state).map(|_| None) }
                        else { entry.lease.complete(&state).map(|_| None) }
                    }
                    "seal" => entry.lease.seal().map(|_| None),
                    "release" => entry.lease.release().map(|_| None),
                    "finish" => {
                        use crate::task::TaskStatus;
                        let status = match p.status.as_deref().unwrap() {
                            "done" => TaskStatus::Done,
                            "error" => TaskStatus::Error,
                            "aborted" => TaskStatus::Aborted,
                            _ => return Err(invalid()),
                        };
                        let candidate = (status, p.suspended.unwrap());
                        let mut frozen = entry
                            .finish
                            .lock()
                            .map_err(|_| io::Error::other("checkpoint finish scope poisoned"))?;
                        if frozen.is_some_and(|saved| saved != candidate) {
                            Err(Error::Invalid)
                        } else {
                            *frozen = Some(candidate);
                            entry.lease.finish(candidate.0, candidate.1).map(|_| None)
                        }
                    }
                    "forget" => {
                        self.entries
                            .lock()
                            .map_err(|_| {
                                io::Error::other("checkpoint capability registry poisoned")
                            })?
                            .live
                            .remove(&id.unwrap());
                        Ok(None)
                    }
                    _ => return Err(invalid()),
                },
            }
        };
        let (accepted, state, error) = match result {
            Ok(state) => (true, state, None),
            Err(error) => (false, None, Some(code(error))),
        };
        serde_json::to_vec(&json!({"format":"tofu.native-checkpoint-control/v1","accepted":accepted,"capability_id":id,"state":state,"error":error})).map_err(io::Error::other)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preflight_consumes_authority_capacity_without_allocating_capabilities() {
        assert!(Registry::new(0).is_err());
        assert!(Registry::new(usize::MAX).is_err());
        for maximum in [1024usize, 8 << 20] {
            let registry = Registry::new(maximum).unwrap();
            let mut request = json!({"format":"tofu.native-checkpoint-control/v1", "parameters":{
                "owner_user_id":7,"task_id":"task","epoch":"epoch","binding":"binding","token":"token",
                "header_bytes":maximum,"snapshot_bytes":maximum,"identity_bytes":1024}});
            assert_eq!(registry.response_bound("create", &serde_json::to_vec(&request).unwrap()).unwrap(),
                maximum + headroom());
            request["parameters"]["snapshot_bytes"] = json!(maximum + 1);
            assert!(registry.response_bound("create", &serde_json::to_vec(&request).unwrap()).is_err());
            assert_eq!(registry.live_capabilities().unwrap(), 0);
        }
    }
}
