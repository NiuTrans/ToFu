//! Versioned checkpoint ownership and round fences over an explicit store.
//! Journal/Lease own transitions; Store owns atomic durable CAS and byte storage.
//! No time-based lease stealing, backend selection, task retry or financial credit.
use serde_json::{json, Value};
use std::borrow::Cow;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, LazyLock,
};

#[path = "checkpoint_owned.rs"]
mod owned;
pub use owned::{ActorHandoff, ActorHandoffSpec, OwnedLease, OwnedPolicy};
#[path = "checkpoint_wallet.rs"]
mod wallet;
pub use wallet::{handoff_wallet_invoice, retired_wallet_invoice, wallet_invoice_view};
#[path = "checkpoint_actor_fees.rs"]
mod actor_fees;
pub use actor_fees::{actor_fee_evidence_from_checkpoint, validate_actor_fee_evidence, validate_aggregate_actor_invoice};
#[path = "checkpoint_actor_accounts.rs"]
mod actor_accounts;
pub use actor_accounts::validate_actor_handoff_accounts;

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

/// Read one abandoned paid frontier without creating a lease or capability.
/// This observation is not takeover authority: a later writer must revalidate
/// the version and accepted domain lineage before restoring any account.
pub fn inspect_abandoned_frontier(store: &dyn Store, owner: u64, task_id: &str,
    snapshot_bytes: usize, identity_bytes: usize) -> Result<Option<Value>, Error> {
    if owner == 0 || task_id.is_empty() || snapshot_bytes == 0 || identity_bytes == 0
        || task_id.len() > identity_bytes {
        return Err(Error::Invalid);
    }
    let Some(first) = store.header(owner, task_id)? else { return Ok(None); };
    let field = |key: &str| first.value[key].as_str().map(str::to_owned).ok_or(Error::Invalid);
    let journal = Journal::new(Scope { owner, task_id: task_id.into(),
        epoch: field("epoch")?, binding: field("binding")? }, store, snapshot_bytes, identity_bytes)?;
    let header = journal.read()?.ok_or(Error::Conflict)?;
    if header.version != first.version || header.value != first.value { return Err(Error::Conflict); }
    if !matches!(header.value["phase"].as_str(), Some("completed" | "tools_pending")) {
        return Err(Error::Ineligible);
    }
    let writer = header.value.get("writer_process").ok_or(Error::Ineligible)?;
    if !store.writer_quiescent(owner, task_id, writer)? { return Err(Error::Ineligible); }
    let snapshot = store.snapshot(owner, task_id, header.version)?.ok_or(Error::Invalid)?;
    journal.validate_frontier(&snapshot, header.value["next_round"].as_u64().ok_or(Error::Invalid)?,
        header.value["phase"] == "tools_pending")?;
    let current = journal.read()?.ok_or(Error::Conflict)?;
    if current.version != header.version || current.value != header.value { return Err(Error::Conflict); }
    Ok(Some(json!({"format":"tofu.native-checkpoint-frontier/v1", "owner_user_id":owner,
        "task_id":task_id,"epoch":journal.scope.epoch,"binding":journal.scope.binding,
        "version":header.version,"lease_generation":header.value["lease_generation"],
        "phase":header.value["phase"],"snapshot":snapshot})))
}

/// Immutable accepted-attempt relation; storage rechecks it in the write transaction.
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResumeLineage {
    pub conversation_id: String,
    pub turn_id: String,
    pub source_attempt_id: String,
    pub target_attempt_id: String,
    pub source_task_id: String,
    pub reserved_micro: u64,
}

/// One atomic source retirement and target creation, including snapshot and outbox.
pub struct Handoff<'a> {
    pub lineage: &'a ResumeLineage,
    pub source_version: u64,
    pub source_header: &'a Value,
    pub target_header: &'a Value,
    pub target_snapshot: &'a Value,
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
    /// Commit the complete claimed actor set and its account observations once.
    /// Acknowledgement authorizes only these prepared leases. Default is deny.
    fn handoff_retired_actors(&self, _actors: &[ActorHandoff], _authorization: &Value,
        _evidence_bytes: usize) -> Result<(), Error> { Err(Error::Ineligible) }
    fn handoff(&self, _request: Handoff<'_>) -> Result<u64, Error> {
        Err(Error::Ineligible)
    }
    /// Captured by the trusted storage owner, never accepted from task input.
    /// Stores without a process-death proof retain the ordinary lease fence.
    fn writer_process(&self) -> Option<Value> {
        None
    }
    fn writer_quiescent(&self, _owner: u64, _task: &str, _identity: &Value) -> Result<bool, Error> {
        Ok(false)
    }
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
    fn reconcile(&self, _identity: crate::agent_checkpoint::Identity<'_>, _state: &Value)
        -> Result<Vec<crate::agent_checkpoint::ReconciledTool>, Value> {
        Err(journal_error(Error::Ineligible))
    }
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
/// Settlement may append one observation per saved call and advance progress.
/// It cannot rewrite paid usage, original limits, deadlines, account origins or
/// the provider's message. All other persisted authority remains byte-value equal.
fn validate_reconciled_prefix(saved: &Value, completed: &Value) -> Result<(), Error> {
    let before = saved["messages"].as_array().ok_or(Error::Invalid)?;
    let calls = crate::agent_checkpoint::pending_calls(before).ok_or(Error::Invalid)?;
    validate_pending_tool_consumption(saved.get("shared_model_account"),
        completed.get("shared_model_account"), calls.len() as u64)?;
    for (key, value) in saved.as_object().ok_or(Error::Invalid)? {
        if !matches!(key.as_str(), "messages" | "progress" | "tools_pending" | "shared_model_account")
            && completed.get(key) != Some(value) { return Err(Error::Invalid); }
    }
    for (key, value) in completed.as_object().ok_or(Error::Invalid)? {
        if !matches!(key.as_str(), "messages" | "progress" | "shared_model_account")
            && saved.get(key) != Some(value) { return Err(Error::Invalid); }
    }
    let after = completed["messages"].as_array().ok_or(Error::Invalid)?;
    if after.len() != before.len() + calls.len() || after[..before.len()] != before[..] {
        return Err(Error::Invalid);
    }
    for (call, result) in calls.iter().zip(&after[before.len()..]) {
        if result["role"] != "tool" || result["tool_call_id"] != call["id"]
            || result.get("content").is_none() || result.get("caller") != call.get("caller") {
            return Err(Error::Invalid);
        }
    }
    Ok(())
}

/// Only spent root tool quota can advance after a paid file batch. Tokens,
/// prices, model/observer ordinals, origins and limits remain exactly equal.
fn validate_pending_tool_consumption(before: Option<&Value>, after: Option<&Value>, calls: u64) -> Result<(), Error> {
    if before == after { return Ok(()); }
    let mut before = before.and_then(Value::as_object).ok_or(Error::Invalid)?.clone();
    let mut after = after.and_then(Value::as_object).ok_or(Error::Invalid)?.clone();
    let mut delta = None;
    for field in ["tool_calls", "control_tool_calls"] {
        let old = before.remove(field).and_then(|v| v.as_u64()).ok_or(Error::Invalid)?;
        let new = after.remove(field).and_then(|v| v.as_u64()).ok_or(Error::Invalid)?;
        let increment = new.checked_sub(old).filter(|n| *n <= calls).ok_or(Error::Invalid)?;
        if delta.is_some_and(|n| n != increment) { return Err(Error::Invalid); }
        delta = Some(increment);
        if after.get("limits").and_then(|v| v["max_tool_calls"].as_u64()).is_some_and(|limit| new > limit) {
            return Err(Error::Invalid);
        }
    }
    if before != after { return Err(Error::Invalid); }
    Ok(())
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
    fn pending(&self, state: crate::agent_checkpoint::View<'_>) -> Result<(), Value> {
        self.identity(state.identity)?;
        self.lease.pending(&state.capture_pending()).map_err(journal_error)
    }
    fn reconcile(&self, identity: crate::agent_checkpoint::Identity<'_>, state: &Value)
        -> Result<Vec<crate::agent_checkpoint::ReconciledTool>, Value> {
        self.identity(identity)?;
        let record = self.lease.owned().map_err(journal_error)?;
        if record.value["phase"] != "tools_pending" {
            return Err(journal_error(Error::Ineligible));
        }
        let saved = self.lease.journal.store.snapshot(identity.owner, identity.task_id, record.version)
            .map_err(journal_error)?.ok_or_else(|| journal_error(Error::Invalid))?;
        if saved != *state { return Err(journal_error(Error::Invalid)); }
        self.recovery.reconcile(identity, state)
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
        if let Some(record) = &record { self.validate_header(record)?; }
        Ok(record)
    }
    fn validate_header(&self, record: &Header) -> Result<(), Error> {
        let h = &record.value;
        retired_wallet_invoice(h)?;
        if let Some(claim) = h.get("goal_actor_claim") {
            if h["phase"] != "terminal" || !h["lease"].is_null()
                || claim["root_task_id"] == h["task_id"]
                || claim["source_run_id"] != format!("goal_{}", claim["root_task_id"].as_str().unwrap_or("")) {
                return Err(Error::Invalid);
            }
        }
        if h.get("goal_actor_handoff").is_some() && (h.get("goal_actor_claim").is_none()
            || h["phase"]!="terminal" || !h["lease"].is_null()
            || h["goal_actor_handoff"]["target_task_id"]==h["task_id"]) {return Err(Error::Invalid);}
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
            || (matches!(h["phase"].as_str(), Some("completed" | "tools_pending")) && h["next_round"] == 0)
            || (matches!(h["phase"].as_str(), Some("inflight" | "tools_pending")) && h["lease"].is_null())
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
    /// Validate supplied retained bytes only. This does not observe writer death,
    /// access storage, grant a lease or authorize recovery/promotion. Passive
    /// replication must separately bind these bytes to its authenticated prefix.
    pub fn validate_retained_frontier(&self, header: &Header, snapshot: &Value) -> Result<(), Error> {
        self.validate_header(header)?;
        if !matches!(header.value["phase"].as_str(), Some("completed" | "tools_pending")) {
            return Err(Error::Ineligible);
        }
        self.validate_frontier(snapshot, header.value["next_round"].as_u64().ok_or(Error::Invalid)?,
            header.value["phase"] == "tools_pending")
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
        self.acquire_with_recovery(token, false)
    }
    /// Explicit recovery after the Store proves the previous writer cannot run.
    /// Never recover an inflight/terminal or legacy unproven frontier. The same
    /// CAS advances generation without reading or rewriting retained history.
    pub fn recover<'a>(&'a self, token: &str) -> Result<Lease<'a, 's>, Error> {
        self.acquire_with_recovery(token, true)
    }
    /// Transfer a released completed frontier or a paid pending frontier whose
    /// writer is proven dead. Storage rechecks lineage and source retirement atomically. An
    /// ambiguous result remains fenced; this method never retries acquisition.
    pub fn handoff<'a>(&'a self, token: &str, lineage: &ResumeLineage) -> Result<Lease<'a, 's>, Error> {
        if token.is_empty() || token.len() > self.identity_bytes
            || lineage.source_task_id == self.scope.task_id
            || [&lineage.conversation_id, &lineage.turn_id, &lineage.source_attempt_id,
                &lineage.target_attempt_id, &lineage.source_task_id].iter().any(|v| v.is_empty())
            || lineage.source_attempt_id == lineage.target_attempt_id
        { return Err(Error::Invalid); }
        if self.read()?.is_some() { return Err(Error::Conflict); }
        let source = Journal::new(Scope { task_id: lineage.source_task_id.clone(),
            ..self.scope.clone().into_owned() }, self.store, self.snapshot_bytes, self.identity_bytes)?;
        let mut header = source.read()?.ok_or(Error::Ineligible)?;
        let pending = header.value["phase"] == "tools_pending";
        if pending {
            let writer = header.value.get("writer_process").ok_or(Error::Ineligible)?;
            if header.value["lease"].is_null()
                || !self.store.writer_quiescent(self.scope.owner, &lineage.source_task_id, writer)? {
                return Err(Error::Ineligible);
            }
        } else if header.value["phase"] != "completed" || !header.value["lease"].is_null() {
            return Err(Error::Ineligible);
        }
        let frontier = header.value["next_round"].as_u64().ok_or(Error::Invalid)?;
        let mut snapshot = self.store.snapshot(self.scope.owner, &lineage.source_task_id,
            header.version)?.ok_or(Error::Ineligible)?;
        source.validate_frontier(&snapshot, frontier, pending)?;
        let invoice = if pending { handoff_wallet_invoice(&snapshot)? } else { None };
        if pending != (snapshot.get("tools_pending") == Some(&Value::Bool(true))) {
            return Err(Error::Invalid);
        }
        snapshot["task_id"] = json!(self.scope.task_id);
        if let Some(account) = snapshot.get_mut("shared_model_account") {
            if account.get("actor_task_id").is_some() {
                return Err(Error::Invalid);
            }
            if account["root_task_id"] != lineage.source_task_id
                || account["owner_user_id"].as_u64() != Some(self.scope.owner)
                || account["complete"] != true { return Err(Error::Invalid); }
            account["root_task_id"] = json!(self.scope.task_id);
        }
        if let Some(fees) = snapshot.get_mut("host_fee_projection") {
            if fees["task_id"] != lineage.source_task_id { return Err(Error::Invalid); }
            for round in fees["rounds"].as_array_mut().ok_or(Error::Invalid)? {
                if round.get("task_id").is_none() { round["task_id"] = json!(lineage.source_task_id); }
            }
            fees["task_id"] = json!(self.scope.task_id);
            fees["reserved_micro"] = json!(lineage.reserved_micro);
            // The source invoice keeps its own executor and reservation. A new
            // executor must capture its own invoice after settling fresh work.
            fees.as_object_mut().ok_or(Error::Invalid)?.remove("wallet_settlement");
            if let Some(original) = &invoice {
                let mut next = original.clone();
                next["task_id"] = json!(self.scope.task_id);
                next["reserved_micro"] = json!(lineage.reserved_micro);
                next["roundCount"] = json!(0);
                for amount in next["componentsMicro"].as_object_mut().ok_or(Error::Invalid)?.values_mut() {
                    *amount = json!(0);
                }
                fees["wallet_settlement"] = next;
            }
        }
        self.validate_frontier(&snapshot, frontier, pending)?;
        let mut target = header.value.clone();
        target["task_id"] = json!(self.scope.task_id);
        target["lease"] = json!(token);
        target["lease_generation"] = json!(1);
        if let Some(writer) = self.store.writer_process() { target["writer_process"] = writer; }
        else { target.as_object_mut().ok_or(Error::Invalid)?.remove("writer_process"); }
        if !crate::agent_checkpoint::valid(&HEADER_SCHEMA, &target) { return Err(Error::Invalid); }
        header.value["phase"] = json!("terminal");
        header.value["lease"] = Value::Null;
        if let Some(invoice) = invoice { header.value["wallet_settlement"] = invoice; }
        retired_wallet_invoice(&header.value)?;
        let version = self.store.handoff(Handoff { lineage, source_version: header.version,
            source_header: &header.value, target_header: &target, target_snapshot: &snapshot })?;
        if version != 1 { return Err(Error::Storage); }
        Ok(Lease { journal: self, state: Arc::new(LeaseState { token: token.into(),
            poisoned: AtomicBool::new(false), loading: AtomicBool::new(false),
            ready: AtomicBool::new(false), generation: 1 }) })
    }
    fn acquire_with_recovery<'a>(
        &'a self,
        token: &str,
        recover: bool,
    ) -> Result<Lease<'a, 's>, Error> {
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
        if !matches!(record.value["phase"].as_str(), Some("new" | "completed"))
            && !(recover && record.value["phase"] == "tools_pending") {
            return Err(Error::Ineligible);
        }
        if recover {
            let identity = record
                .value
                .get("writer_process")
                .ok_or(Error::Ineligible)?;
            if record.value["lease"].is_null()
                || !self
                    .store
                    .writer_quiescent(self.scope.owner, &self.scope.task_id, identity)?
            {
                return Err(Error::Ineligible);
            }
        } else if !record.value["lease"].is_null() {
            return Err(Error::Ineligible);
        }
        let generation = record.version.checked_add(1).ok_or(Error::Invalid)?;
        record.value["lease"] = json!(token);
        record.value["lease_generation"] = json!(generation);
        if let Some(identity) = self.store.writer_process() {
            record.value["writer_process"] = identity;
        } else {
            record
                .value
                .as_object_mut()
                .ok_or(Error::Invalid)?
                .remove("writer_process");
        }
        if !crate::agent_checkpoint::valid(&HEADER_SCHEMA, &record.value) {
            return Err(Error::Invalid);
        }
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
        self.validate_frontier(state, frontier, false)
    }
    fn validate_frontier(&self, state: &Value, frontier: u64, pending: bool) -> Result<(), Error> {
        if !crate::agent_checkpoint::valid(&SNAPSHOT_SCHEMA, state)
            || (state["tools_pending"] == true) != pending
            || state["owner_user_id"].as_u64() != Some(self.scope.owner)
            || state["task_id"] != self.scope.task_id
            || state["epoch"] != self.scope.epoch
            || state["binding"] != self.scope.binding
            || state["next_round"].as_u64() != Some(frontier)
            || state["tool_calls"].as_u64().is_none_or(|n| {
                n < frontier || state["tool_limit"].as_u64().is_some_and(|limit| n > limit)
            })
            || state["round_limit"].as_u64().is_some_and(|n| frontier >= n)
            || state
                .get("model_account")
                .is_some_and(|a| a["last_ordinal"].as_u64() != Some(frontier))
            || state["usage"].as_array().map(|v| v.len() as u64) != Some(frontier)
            || (if pending {
                state["messages"].as_array()
                    .and_then(|messages| crate::agent_checkpoint::pending_calls(messages))
                    .is_none_or(|calls| state["tool_calls"].as_u64().is_none_or(|count| calls.len() as u64 > count))
            } else {
                state["messages"].as_array().and_then(|v| v.last()).map(|v| &v["role"]) != Some(&json!("tool"))
            })
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
            Some("completed" | "tools_pending") => {
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
                    self.journal.validate_frontier(
                        &s,
                        record.value["next_round"].as_u64().ok_or(Error::Invalid)?,
                        record.value["phase"] == "tools_pending",
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
        let pending = record.value["phase"] == "tools_pending";
        if record.value["phase"] != "inflight" && !pending {
            return Err(Error::Ineligible);
        }
        let next = record.value["next_round"]
            .as_u64()
            .and_then(|v| v.checked_add(u64::from(!pending)))
            .ok_or(Error::Invalid)?;
        self.journal.validate_snapshot(state, next)?;
        if pending {
            let saved = self.journal.store.snapshot(self.journal.scope.owner,
                &self.journal.scope.task_id, record.version)?.ok_or(Error::Invalid)?;
            self.journal.validate_frontier(&saved, next, true)?;
            validate_reconciled_prefix(&saved, state)?;
        }
        record.value["phase"] = json!("completed");
        record.value["next_round"] = json!(next);
        self.commit(&record, SnapshotWrite::Replace(state))
    }
    /// Commit the paid response and entire unresolved batch before any tool.
    /// A lost ACK poisons this lease; neither begin nor release can bypass it.
    pub fn pending(&self, state: &Value) -> Result<(), Error> {
        if !self.state.ready.load(Ordering::Acquire) { return Err(Error::Ineligible); }
        let mut record = self.owned()?;
        if record.value["phase"] != "inflight" { return Err(Error::Ineligible); }
        let next = record.value["next_round"].as_u64()
            .and_then(|round| round.checked_add(1)).ok_or(Error::Invalid)?;
        self.journal.validate_frontier(state, next, true)?;
        record.value["phase"] = json!("tools_pending");
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
