//! Versioned checkpoint ownership and round fences over an explicit store.
//! Journal/Lease own transitions; Store owns atomic durable CAS and byte storage.
//! No time-based lease stealing, backend selection, task retry or financial credit.
use serde_json::{Value, json};
use std::borrow::Cow;
use std::sync::{
    Arc, LazyLock,
    atomic::{AtomicBool, Ordering},
};

#[path = "checkpoint_owned.rs"]
mod owned;
pub use owned::{OwnedLease, OwnedPolicy};

static HEADER_SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../contracts/native_agent_journal_v1.schema.json"
    ))
    .expect("journal contract")
});
static SNAPSHOT_SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../contracts/native_agent_checkpoint_v1.schema.json"
    ))
    .expect("checkpoint contract")
});

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Invalid,
    Conflict,
    Ineligible,
    Storage,
    Poisoned,
    Oversize,
}

/// Identity is explicit even when an adapter encodes it into a record key.
#[derive(Clone)]
pub struct Scope {
    pub owner: u64,
    pub task_id: String,
    pub epoch: String,
    pub binding: String,
}
pub struct Header {
    pub version: u64,
    pub value: Value,
}

/// Keep must never require reading/re-encoding history on a metadata transition.
pub enum SnapshotWrite<'a> {
    Keep,
    Replace(&'a Value),
}

/// Atomicity includes header and snapshot; success means durable acknowledgement.
/// Missing records have version zero. Every successful CAS increments exactly once.
/// snapshot must read the requested version or return Conflict, never stale bytes.
/// Errors/unknown delivery must not be translated to success. Caller joins all I/O.
pub trait Store: Send + Sync {
    fn header(&self, owner: u64, task_id: &str) -> Result<Option<Header>, Error>;
    fn compare_exchange(
        &self,
        owner: u64,
        task_id: &str,
        expected: u64,
        header: &Value,
        snapshot: SnapshotWrite<'_>,
    ) -> Result<u64, Error>;
    fn snapshot(&self, owner: u64, task_id: &str, version: u64) -> Result<Option<Value>, Error>;
}

pub struct Journal<'a> {
    scope: Cow<'a, Scope>,
    store: &'a dyn Store,
    snapshot_bytes: usize,
    identity_bytes: usize,
}
pub struct Lease<'a, 's> {
    journal: &'a Journal<'s>,
    state: Arc<LeaseState>,
}
struct LeaseState {
    token: String,
    poisoned: AtomicBool,
    loading: AtomicBool,
    ready: AtomicBool,
    generation: u64,
}

/// Financial and elapsed-time restoration remains an explicitly trusted owner.
pub trait Recovery {
    fn restore(
        &self,
        identity: crate::agent_checkpoint::Identity<'_>,
        state: &Value,
    ) -> Result<(), Value>;
}

/// Composes the existing Agent checkpoint port with one acquired journal lease.
/// Caller releases only after a committed Suspend has joined, or seals after
/// terminal handling. No automatic Drop release can reopen an uncertain run.
pub struct Policy<'a, 's> {
    pub lease: Lease<'a, 's>,
    pub recovery: &'a dyn Recovery,
    pub suspend: bool,
    suspended: Arc<AtomicBool>,
}
impl<'a, 's> Policy<'a, 's> {
    pub fn new(lease: Lease<'a, 's>, recovery: &'a dyn Recovery, suspend: bool) -> Self {
        Self {
            lease,
            recovery,
            suspend,
            suspended: Arc::new(AtomicBool::new(false)),
        }
    }
    /// Bind the exact owned Store used to acquire this lease. Task still owns
    /// candidate freezing, settlement and persistence/publication retry debt.
    pub fn terminal_persistence(
        &self,
        store: Arc<dyn Store>,
        delegate: Arc<dyn crate::task::TerminalPersistence>,
    ) -> Result<Arc<dyn crate::task::TerminalPersistence>, Error> {
        if !std::ptr::eq(self.lease.journal.store, store.as_ref()) {
            return Err(Error::Invalid);
        }
        Ok(Arc::new(FencedPersistence {
            scope: self.lease.journal.scope.as_ref().clone(),
            store,
            token: self.lease.state.token.clone(),
            generation: self.lease.state.generation,
            snapshot_bytes: self.lease.journal.snapshot_bytes,
            identity_bytes: self.lease.journal.identity_bytes,
            suspended: self.suspended.clone(),
            delegate,
        }))
    }

    fn identity(&self, identity: crate::agent_checkpoint::Identity<'_>) -> Result<(), Value> {
        let scope = &self.lease.journal.scope;
        if identity.owner != scope.owner
            || identity.task_id != scope.task_id
            || identity.epoch != scope.epoch
            || identity.binding != scope.binding
        {
            return Err(journal_error(Error::Invalid));
        }
        Ok(())
    }
}
fn journal_error(error: Error) -> Value {
    let code = match error {
        Error::Invalid => "invalid",
        Error::Conflict => "conflict",
        Error::Ineligible => "ineligible",
        Error::Storage => "storage",
        Error::Poisoned => "poisoned",
        Error::Oversize => "oversize",
    };
    json!({"kind":"checkpoint_journal","code":code})
}
impl crate::agent_checkpoint::Port for Policy<'_, '_> {
    fn binding(&self) -> &str {
        &self.lease.journal.scope.binding
    }
    fn load(
        &self,
        identity: crate::agent_checkpoint::Identity<'_>,
    ) -> Result<Option<Value>, Value> {
        self.identity(identity)?;
        let state = self.lease.load().map_err(journal_error)?;
        if let Some(state) = &state {
            if let Err(error) = self.recovery.restore(identity, state) {
                self.lease.state.poisoned.store(true, Ordering::Release);
                return Err(error);
            }
        }
        Ok(state)
    }
    fn begin(
        &self,
        identity: crate::agent_checkpoint::Identity<'_>,
        round: usize,
    ) -> Result<(), Value> {
        self.identity(identity)?;
        self.lease.begin(round as u64).map_err(journal_error)
    }
    fn complete(
        &self,
        state: crate::agent_checkpoint::View<'_>,
    ) -> Result<crate::agent_checkpoint::Disposition, Value> {
        self.identity(state.identity)?;
        self.lease
            .complete(&state.capture())
            .map_err(journal_error)?;
        if self.suspend {
            self.suspended.store(true, Ordering::Release);
        }
        Ok(if self.suspend {
            crate::agent_checkpoint::Disposition::Suspend
        } else {
            crate::agent_checkpoint::Disposition::Continue
        })
    }
}

impl<'s> Journal<'s> {
    pub fn new(
        scope: Scope,
        store: &'s dyn Store,
        snapshot_bytes: usize,
        identity_bytes: usize,
    ) -> Result<Self, Error> {
        if scope.owner == 0
            || scope.task_id.is_empty()
            || scope.epoch.is_empty()
            || scope.binding.is_empty()
            || snapshot_bytes == 0
            || identity_bytes == 0
            || scope
                .task_id
                .len()
                .saturating_add(scope.epoch.len())
                .saturating_add(scope.binding.len())
                > identity_bytes
        {
            return Err(Error::Invalid);
        }
        Ok(Self {
            scope: Cow::Owned(scope),
            store,
            snapshot_bytes,
            identity_bytes,
        })
    }
    fn read(&self) -> Result<Option<Header>, Error> {
        let record = self.store.header(self.scope.owner, &self.scope.task_id)?;
        if let Some(record) = &record {
            let h = &record.value;
            if record.version == 0
                || !crate::agent_checkpoint::valid(&HEADER_SCHEMA, h)
                || h["owner_user_id"].as_u64() != Some(self.scope.owner)
                || h["task_id"] != self.scope.task_id
                || h["epoch"] != self.scope.epoch
                || h["binding"] != self.scope.binding
                || h["lease"]
                    .as_str()
                    .is_some_and(|v| v.len() > self.identity_bytes)
                || h["lease_generation"]
                    .as_u64()
                    .is_none_or(|v| v > record.version)
                || (!h["lease"].is_null() && h["lease_generation"] == 0)
                || (h["phase"] == "new" && h["next_round"] != 0)
                || (h["phase"] == "completed" && h["next_round"] == 0)
                || (h["phase"] == "inflight" && h["lease"].is_null())
            {
                return Err(Error::Invalid);
            }
        }
        Ok(record)
    }
    fn commit(&self, record: &Header, snapshot: SnapshotWrite<'_>) -> Result<u64, Error> {
        let expected = record.version.checked_add(1).ok_or(Error::Invalid)?;
        let version = self.store.compare_exchange(
            self.scope.owner,
            &self.scope.task_id,
            record.version,
            &record.value,
            snapshot,
        )?;
        if version != expected {
            return Err(Error::Storage);
        }
        Ok(version)
    }
    /// A crashed active lease remains fenced. A trusted recovery authority must
    /// prove prior owner quiescence before any separate takeover implementation.
    pub fn acquire<'a>(&'a self, token: &str) -> Result<Lease<'a, 's>, Error> {
        if token.is_empty() || token.len() > self.identity_bytes {
            return Err(Error::Invalid);
        }
        let mut record = match self.read()? {
            Some(record) => record,
            None => Header {
                version: 0,
                value: json!({"format":"tofu.native-agent-journal/v1",
                "owner_user_id":self.scope.owner,"task_id":self.scope.task_id,"epoch":self.scope.epoch,
                "binding":self.scope.binding,"next_round":0,"phase":"new","lease":null,"lease_generation":0}),
            },
        };
        if !record.value["lease"].is_null()
            || !matches!(record.value["phase"].as_str(), Some("new" | "completed"))
        {
            return Err(Error::Ineligible);
        }
        let generation = record.version.checked_add(1).ok_or(Error::Invalid)?;
        record.value["lease"] = json!(token);
        record.value["lease_generation"] = json!(generation);
        self.commit(&record, SnapshotWrite::Keep)?;
        Ok(Lease {
            journal: self,
            state: Arc::new(LeaseState {
                token: token.to_owned(),
                poisoned: AtomicBool::new(false),
                loading: AtomicBool::new(false),
                ready: AtomicBool::new(false),
                generation,
            }),
        })
    }
    /// Shared terminal debt fence for owned callbacks and Task persistence.
    /// Reread own durable terminal after unknown delivery; never replay work.
    fn finish_owned(&self, token: &str, generation: u64, suspended: bool) -> Result<(), Error> {
        let mut record = self.read()?.ok_or(Error::Ineligible)?;
        if record.value["lease"] != token
            || record.value["lease_generation"].as_u64() != Some(generation)
        {
            return Err(Error::Conflict);
        }
        if suspended {
            if record.value["phase"] != "completed" {
                return Err(Error::Ineligible);
            }
        } else if record.value["phase"] != "terminal" {
            record.value["phase"] = json!("terminal");
            self.commit(&record, SnapshotWrite::Keep)?;
        }
        Ok(())
    }
    fn validate_snapshot(&self, state: &Value, frontier: u64) -> Result<(), Error> {
        if !crate::agent_checkpoint::valid(&SNAPSHOT_SCHEMA, state)
            || state["owner_user_id"].as_u64() != Some(self.scope.owner)
            || state["task_id"] != self.scope.task_id
            || state["epoch"] != self.scope.epoch
            || state["binding"] != self.scope.binding
            || state["next_round"].as_u64() != Some(frontier)
            || state["tool_calls"]
                .as_u64()
                .is_none_or(|n| n < frontier || Some(n) > state["tool_limit"].as_u64())
            || state["round_limit"].as_u64().is_none_or(|n| frontier >= n)
            || state
                .get("model_account")
                .is_some_and(|a| a["last_ordinal"].as_u64() != Some(frontier))
            || state["usage"].as_array().map(|v| v.len() as u64) != Some(frontier)
            || state["messages"]
                .as_array()
                .and_then(|v| v.last())
                .map(|v| &v["role"])
                != Some(&json!("tool"))
        {
            return Err(Error::Invalid);
        }
        struct Bound {
            count: usize,
            limit: usize,
        }
        impl std::io::Write for Bound {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.count = self
                    .count
                    .checked_add(bytes.len())
                    .filter(|v| *v <= self.limit)
                    .ok_or_else(|| std::io::Error::other("checkpoint byte bound"))?;
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        serde_json::to_writer(
            Bound {
                count: 0,
                limit: self.snapshot_bytes,
            },
            state,
        )
        .map_err(|_| Error::Oversize)
    }
}
impl Lease<'_, '_> {
    fn owned(&self) -> Result<Header, Error> {
        if self.state.poisoned.load(Ordering::Acquire) {
            return Err(Error::Poisoned);
        }
        let result = self
            .journal
            .read()
            .and_then(|r| r.ok_or(Error::Conflict))
            .and_then(|r| {
                if r.value["lease"] == self.state.token
                    && r.value["lease_generation"].as_u64() == Some(self.state.generation)
                {
                    Ok(r)
                } else {
                    Err(Error::Conflict)
                }
            });
        if result.is_err() {
            self.state.poisoned.store(true, Ordering::Release);
        }
        result
    }
    fn commit(&self, record: &Header, snapshot: SnapshotWrite<'_>) -> Result<(), Error> {
        let result = self.journal.commit(record, snapshot).map(|_| ());
        if result.is_err() {
            self.state.poisoned.store(true, Ordering::Release);
        }
        result
    }
    /// Caller must restore pricing/time authority before handing this state to Agent.
    pub fn load(&self) -> Result<Option<Value>, Error> {
        if self.state.loading.swap(true, Ordering::AcqRel) {
            return Err(Error::Ineligible);
        }
        let record = self.owned()?;
        let result = match record.value["phase"].as_str() {
            Some("new") => Ok(None),
            Some("completed") => {
                let state = self
                    .journal
                    .store
                    .snapshot(
                        self.journal.scope.owner,
                        &self.journal.scope.task_id,
                        record.version,
                    )
                    .and_then(|s| s.ok_or(Error::Invalid));
                let result = state.and_then(|s| {
                    self.journal.validate_snapshot(
                        &s,
                        record.value["next_round"].as_u64().ok_or(Error::Invalid)?,
                    )?;
                    Ok(Some(s))
                });
                if result.is_err() {
                    self.state.poisoned.store(true, Ordering::Release);
                }
                result
            }
            _ => Err(Error::Ineligible),
        };
        if result.is_ok() {
            self.state.ready.store(true, Ordering::Release);
        } else {
            self.state.poisoned.store(true, Ordering::Release);
        }
        result
    }
    pub fn begin(&self, round: u64) -> Result<(), Error> {
        if !self.state.ready.load(Ordering::Acquire) {
            return Err(Error::Ineligible);
        }
        let mut record = self.owned()?;
        if record.value["next_round"].as_u64() != Some(round)
            || !matches!(record.value["phase"].as_str(), Some("new" | "completed"))
        {
            return Err(Error::Ineligible);
        }
        record.value["phase"] = json!("inflight");
        self.commit(&record, SnapshotWrite::Keep)
    }
    pub fn complete(&self, state: &Value) -> Result<(), Error> {
        let mut record = self.owned()?;
        if record.value["phase"] != "inflight" {
            return Err(Error::Ineligible);
        }
        let next = record.value["next_round"]
            .as_u64()
            .and_then(|v| v.checked_add(1))
            .ok_or(Error::Invalid)?;
        self.journal.validate_snapshot(state, next)?;
        record.value["phase"] = json!("completed");
        record.value["next_round"] = json!(next);
        self.commit(&record, SnapshotWrite::Replace(state))
    }
    /// Explicit release only after the execution owner has joined all effects.
    /// Dropping this handle intentionally leaves its durable lease fenced.
    pub fn release(&self) -> Result<(), Error> {
        if self.state.loading.load(Ordering::Acquire) && !self.state.ready.load(Ordering::Acquire) {
            return Err(Error::Ineligible);
        }
        let mut record = self.owned()?;
        if !matches!(record.value["phase"].as_str(), Some("new" | "completed")) {
            return Err(Error::Ineligible);
        }
        record.value["lease"] = Value::Null;
        self.commit(&record, SnapshotWrite::Keep)
    }
    /// Terminal publication cannot be replaced by recovery or a later begin.
    pub fn seal(&self) -> Result<(), Error> {
        if self.state.loading.load(Ordering::Acquire) && !self.state.ready.load(Ordering::Acquire) {
            return Err(Error::Ineligible);
        }
        let mut record = self.owned()?;
        if record.value["phase"] == "terminal" {
            return Err(Error::Ineligible);
        }
        record.value["phase"] = json!("terminal");
        self.commit(&record, SnapshotWrite::Keep)
    }
}

/// Only scope/lease metadata and owned ports survive the execution stack.
/// No history copy, second task state machine, timer or automatic lease release.
struct FencedPersistence {
    scope: Scope,
    store: Arc<dyn Store>,
    token: String,
    generation: u64,
    snapshot_bytes: usize,
    identity_bytes: usize,
    suspended: Arc<AtomicBool>,
    delegate: Arc<dyn crate::task::TerminalPersistence>,
}
impl crate::task::TerminalPersistence for FencedPersistence {
    fn persist_once(
        &self,
        candidate: &crate::task::TerminalCandidate,
    ) -> Result<(), crate::task::PortError> {
        use crate::task::{PortError, TaskStatus};
        if candidate.identity.owner_user_id != self.scope.owner
            || candidate.identity.task_id != self.scope.task_id
            || !candidate.receipt.outcome.is_terminal()
            || !matches!(
                candidate.status,
                TaskStatus::Done | TaskStatus::Error | TaskStatus::Aborted
            )
        {
            return Err(PortError);
        }
        let journal = Journal::new(
            self.scope.clone(),
            self.store.as_ref(),
            self.snapshot_bytes,
            self.identity_bytes,
        )
        .map_err(|_| PortError)?;
        journal
            .finish_owned(
                &self.token,
                self.generation,
                self.suspended.load(Ordering::Acquire) && candidate.status == TaskStatus::Aborted,
            )
            .map_err(|_| PortError)?;
        // A failed/unknown seal never reaches durable task publication. A retry
        // rereads the same terminal lease and never re-executes the Task work.
        self.delegate.persist_once(candidate)
    }
}
