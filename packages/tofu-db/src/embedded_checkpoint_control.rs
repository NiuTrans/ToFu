//! Checkpoint C ABI command ownership over the existing embedded worker port.
//! Registry owns volatile capability IDs only; OwnedLease owns all transitions.
//! Buffer admission precedes effects; snapshots are decoded only by the worker.
use crate::embed::EmbeddedRuntime;
use serde::Deserialize;
use serde_json::{Value, json, value::RawValue};
use std::{
    collections::BTreeMap,
    io,
    sync::{Arc, LazyLock, Mutex},
};
use tofu_runtime::checkpoint_journal::{Error, OwnedLease, Scope};

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
struct Entry {
    lease: Arc<OwnedLease>,
    response_bytes: usize,
    finish: Mutex<Option<(tofu_runtime::task::TaskStatus, bool)>>,
}
#[derive(Default)]
struct Entries {
    next: u64,
    live: BTreeMap<u64, Arc<Entry>>,
}
#[derive(Default)]
pub(super) struct Registry {
    entries: Mutex<Entries>,
}
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "invalid native checkpoint control request",
    )
}
fn parse<'a>(operation: &str, bytes: &'a [u8]) -> io::Result<Payload<'a>> {
    let p: Payload<'a> = serde_json::from_slice(bytes).map_err(|_| invalid())?;
    if p.format != "tofu.native-checkpoint-control/v1"
        || (operation == "create") != p.parameters.is_some()
        || (operation != "create") != p.capability_id.is_some()
        || p.capability_id == Some(0)
        || (operation == "begin") != p.round.is_some()
        || (operation == "complete") != p.state.is_some()
        || (operation == "finish") != p.status.is_some()
        || (operation == "finish") != p.suspended.is_some()
        || p.status
            .as_ref()
            .is_some_and(|s| !["done", "error", "aborted"].contains(&s.as_str()))
        || ![
            "create", "load", "begin", "complete", "seal", "release", "forget", "finish",
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
    pub(super) fn response_bound(&self, operation: &str, bytes: &[u8]) -> io::Result<usize> {
        let p = parse(operation, bytes)?;
        if let Some(b) = p.parameters {
            if b.snapshot_bytes == 0
                || b.header_bytes == 0
                || b.identity_bytes == 0
                || b.snapshot_bytes > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES
                || b.header_bytes > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES
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
    pub(super) fn execute(
        &self,
        runtime: Arc<EmbeddedRuntime>,
        operation: &str,
        bytes: &[u8],
        deadline: u64,
    ) -> io::Result<Vec<u8>> {
        let p = parse(operation, bytes)?;
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
        let result: Result<Option<Value>, Error> = if let Some(b) = p.parameters {
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
            let lease = runtime.checkpoint_lease(
                Scope {
                    owner: b.owner_user_id,
                    task_id: b.task_id,
                    epoch: b.epoch,
                    binding: b.binding,
                },
                b.header_bytes,
                b.snapshot_bytes,
                b.identity_bytes,
                &b.token,
            );
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
                            }),
                        );
                    id = Some(next);
                    Ok(None)
                }
                Err(error) => Err(error),
            }
        } else {
            let lease = {
                let entries = self
                    .entries
                    .lock()
                    .map_err(|_| io::Error::other("checkpoint capability registry poisoned"))?;
                entries.live.get(&id.unwrap()).cloned()
            };
            match lease {
                None => Err(Error::Ineligible),
                Some(entry) => match operation {
                    "load" => entry.lease.load(),
                    "begin" => entry.lease.begin(p.round.unwrap()).map(|_| None),
                    "complete" => {
                        let state =
                            serde_json::from_str(p.state.unwrap().get()).map_err(|_| invalid())?;
                        entry.lease.complete(&state).map(|_| None)
                    }
                    "seal" => entry.lease.seal().map(|_| None),
                    "release" => entry.lease.release().map(|_| None),
                    "finish" => {
                        use tofu_runtime::task::TaskStatus;
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
#[path = "embedded_checkpoint_control_tests.rs"]
mod tests;
