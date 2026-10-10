//! Canonical live execution/resource settlement, independent of task persistence.
//! Registry owns identity membership; callbacks run outside state locks. A single
//! terminal receipt wins, while concurrent terminalizers await its publication.
use crate::execution_contract::{
    ExecutionPhase as Phase, ResourceDisposition, ResourceName, MAX_RESOURCES_PER_EXECUTION,
};
use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

pub type Clock = Arc<dyn Fn() -> Duration + Send + Sync>;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionError {
    InvalidIdentity,
    InvalidDeadline,
    DuplicateIdentity,
    RegressedPhase,
    TerminalRequiresSettlement,
    Settling,
    DuplicateResource,
    ResourceCapacity,
    ReentrantSettlement,
    StatePoisoned,
}
#[derive(Clone, Copy, Debug)]
pub enum ReleaseAck {
    Released,
    Deferred,
}
#[derive(Clone, Debug)]
pub struct ReleaseError;
pub type Release = Box<dyn FnOnce(&SettlementContext) -> Result<ReleaseAck, ReleaseError> + Send>;
#[derive(Clone, Debug)]
pub struct SettlementContext {
    pub execution_id: String,
    pub kind: String,
    pub owner_user_id: u64,
    pub outcome: Phase,
    pub cause: String,
    pub dispatch_started: bool,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TerminalReceipt {
    pub execution_id: String,
    pub kind: String,
    pub requested_outcome: Phase,
    pub outcome: Phase,
    pub cause: String,
    pub invariants_satisfied: bool,
    pub resource_dispositions: Vec<(ResourceName, ResourceDisposition)>,
    pub duration: Duration,
}
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub kind: String,
    pub phase: Phase,
    pub age: Duration,
    pub phase_age: Duration,
    pub heartbeat_age: Duration,
    pub deadline_remaining_nanos: Option<i128>,
    pub dispatch_started: bool,
    pub cancel_requested: bool,
    pub held_resources: Vec<ResourceName>,
}
struct Resource {
    name: ResourceName,
    release: Release,
    order: i32,
    recoverable: bool,
}
struct State {
    phase: Phase,
    phase_at: Duration,
    heartbeat_at: Duration,
    dispatch_started: bool,
    cancel_requested: bool,
    cancel_reason: String,
    resources: Vec<Resource>,
    held: Vec<ResourceName>,
    settling_thread: Option<ThreadId>,
    receipt: Option<Arc<TerminalReceipt>>,
}
#[derive(Clone)]
pub struct Registry {
    members: Arc<Mutex<BTreeMap<String, Weak<ExecutionSession>>>>,
    clock: Clock,
}
/// Observation owns wake channels/clock only, never the parent's resource scope.
#[derive(Clone)]
pub(crate) struct StopObserver {
    cancellation: tokio::sync::watch::Receiver<bool>,
    closing: tokio::sync::watch::Receiver<bool>,
    clock: Clock,
    deadline: Option<Duration>,
}
pub(crate) struct StopFacts {
    pub cancellation_requested: bool,
    pub closing: bool,
    pub deadline_remaining_nanos: Option<i128>,
}
impl StopObserver {
    pub fn facts(&self) -> StopFacts {
        StopFacts {
            cancellation_requested: *self.cancellation.borrow(),
            closing: *self.closing.borrow() || self.closing.has_changed().is_err(),
            deadline_remaining_nanos: self
                .deadline
                .map(|deadline| deadline.as_nanos() as i128 - (self.clock)().as_nanos() as i128),
        }
    }
    pub async fn stopped(&self) {
        let mut cancellation = self.cancellation.clone();
        let mut closing = self.closing.clone();
        loop {
            if *cancellation.borrow_and_update() || *closing.borrow_and_update() {
                return;
            }
            tokio::select! {changed=cancellation.changed()=>{if changed.is_err(){return;}},changed=closing.changed()=>{if changed.is_err(){return;}}}
        }
    }
}
pub struct ExecutionSession {
    execution_id: String,
    kind: String,
    owner_user_id: u64,
    request_id: String,
    created: Duration,
    deadline: Option<Duration>,
    clock: Clock,
    registry: Weak<Mutex<BTreeMap<String, Weak<ExecutionSession>>>>,
    state: Mutex<State>,
    finished: Condvar,
    cancellation: tokio::sync::watch::Sender<bool>,
    settlement_started: tokio::sync::watch::Sender<bool>,
}
fn clip(value: &str, length: usize) -> String {
    value.chars().take(length).collect()
}
fn terminal(phase: Phase) -> bool {
    phase.is_terminal()
}

impl Registry {
    pub fn new() -> Self {
        let start = Instant::now();
        Self::with_clock(Arc::new(move || start.elapsed()))
    }
    pub fn with_clock(clock: Clock) -> Self {
        Self {
            members: Arc::new(Mutex::new(BTreeMap::new())),
            clock,
        }
    }
    pub fn start(
        &self,
        id: &str,
        kind: &str,
        owner_user_id: u64,
        request_id: &str,
        deadline: Option<Duration>,
    ) -> Result<Arc<ExecutionSession>, SessionError> {
        self.start_internal(id, kind, owner_user_id, request_id, deadline.map(|value| (value, None)))
    }
    /// Start an observed budget without granting observation-to-entry time.
    /// Depleted budget creates a cancelled session for normal task settlement.
    pub fn start_observed(
        &self, id: &str, kind: &str, owner_user_id: u64, request_id: &str,
        remaining: Duration, observed_at: Instant,
    ) -> Result<Arc<ExecutionSession>, SessionError> {
        self.start_internal(id, kind, owner_user_id, request_id, Some((remaining, Some(observed_at))))
    }
    fn start_internal(
        &self, id: &str, kind: &str, owner_user_id: u64, request_id: &str,
        deadline: Option<(Duration, Option<Instant>)>,
    ) -> Result<Arc<ExecutionSession>, SessionError> {
        if id.trim().is_empty() || owner_user_id == 0 {
            return Err(SessionError::InvalidIdentity);
        }
        let now = (self.clock)();
        let observed_at = deadline.and_then(|(_, observed)| observed);
        let deadline = match deadline.map(|(remaining, _)| remaining) {
            Some(value) if value.is_zero() && observed_at.is_none() => return Err(SessionError::InvalidDeadline),
            Some(value) => Some(
                now.checked_add(value.saturating_sub(observed_at.map_or(Duration::ZERO, |at| at.elapsed())))
                    .ok_or(SessionError::InvalidDeadline)?,
            ),
            None => None,
        };
        let mut members = self
            .members
            .lock()
            .map_err(|_| SessionError::StatePoisoned)?;
        if members.get(id).and_then(Weak::upgrade).is_some() {
            return Err(SessionError::DuplicateIdentity);
        }
        members.retain(|_, member| member.strong_count() != 0);
        let session = Arc::new(ExecutionSession {
            execution_id: id.into(),
            kind: clip(if kind.is_empty() { "execution" } else { kind }, 64),
            owner_user_id,
            request_id: clip(request_id, 128),
            created: now,
            deadline,
            clock: Arc::clone(&self.clock),
            registry: Arc::downgrade(&self.members),
            state: Mutex::new(State {
                phase: Phase::Created,
                phase_at: now,
                heartbeat_at: now,
                dispatch_started: false,
                cancel_requested: false,
                cancel_reason: String::new(),
                resources: Vec::new(),
                held: Vec::new(),
                settling_thread: None,
                receipt: None,
            }),
            finished: Condvar::new(),
            cancellation: tokio::sync::watch::channel(false).0,
            settlement_started: tokio::sync::watch::channel(false).0,
        });
        members.insert(id.into(), Arc::downgrade(&session));
        drop(members);
        if observed_at.is_some() && deadline.is_some_and(|at| at <= now) {
            session.request_cancel("execution_deadline_exceeded")?;
        }
        Ok(session)
    }
    pub fn snapshots(&self, limit: usize) -> Result<Vec<Snapshot>, SessionError> {
        let sessions: Vec<_> = self
            .members
            .lock()
            .map_err(|_| SessionError::StatePoisoned)?
            .values()
            .filter_map(Weak::upgrade)
            .take(limit.clamp(1, 1024))
            .collect();
        sessions
            .into_iter()
            .map(|session| session.snapshot())
            .collect()
    }
    pub fn active_count(&self) -> Result<usize, SessionError> {
        Ok(self
            .members
            .lock()
            .map_err(|_| SessionError::StatePoisoned)?
            .values()
            .filter(|session| session.strong_count() != 0)
            .count())
    }
    pub fn reconcile_deadlines(&self) -> Result<usize, SessionError> {
        let sessions: Vec<_> = self
            .members
            .lock()
            .map_err(|_| SessionError::StatePoisoned)?
            .values()
            .filter_map(Weak::upgrade)
            .collect();
        let now = (self.clock)();
        let mut changed = 0;
        for session in sessions {
            if session.deadline.is_some_and(|deadline| deadline <= now)
                && session.request_cancel("execution_deadline_exceeded")?
            {
                changed += 1;
            }
        }
        Ok(changed)
    }
}
impl Default for Registry {
    fn default() -> Self {
        Self::new()
    }
}
impl ExecutionSession {
    pub(crate) fn stop_observer(&self) -> StopObserver {
        StopObserver {
            cancellation: self.cancellation.subscribe(),
            closing: self.settlement_started.subscribe(),
            clock: Arc::clone(&self.clock),
            deadline: self.deadline,
        }
    }
    pub fn belongs_to(&self, owner: u64, execution_id: &str) -> bool {
        owner == self.owner_user_id && execution_id == self.execution_id
    }
    pub fn request_id(&self) -> &str {
        &self.request_id
    }
    pub fn advance(&self, phase: Phase) -> Result<bool, SessionError> {
        let mut state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
        if terminal(state.phase) {
            return Ok(false);
        }
        if terminal(phase) {
            return Err(SessionError::TerminalRequiresSettlement);
        }
        if phase.lifecycle_order() < state.phase.lifecycle_order() {
            return Err(SessionError::RegressedPhase);
        }
        let changed = phase != state.phase;
        let now = (self.clock)();
        if changed {
            state.phase = phase;
            state.phase_at = now;
        }
        state.heartbeat_at = now;
        if phase == Phase::Dispatching {
            state.dispatch_started = true;
        }
        Ok(changed)
    }
    pub fn heartbeat(&self) -> Result<(), SessionError> {
        let mut state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
        if state.receipt.is_none() {
            state.heartbeat_at = (self.clock)();
        }
        Ok(())
    }
    pub fn request_cancel(&self, cause: &str) -> Result<bool, SessionError> {
        let mut state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
        if state.receipt.is_some() {
            return Ok(false);
        }
        let first = !state.cancel_requested;
        state.cancel_requested = true;
        self.cancellation.send_replace(true);
        if first {
            state.cancel_reason = clip(if cause.is_empty() { "cancelled" } else { cause }, 96);
        }
        Ok(first)
    }
    /// Await the monotonic cancellation signal without a polling worker.
    pub async fn cancelled(&self) {
        let mut receiver = self.cancellation.subscribe();
        loop {
            if *receiver.borrow_and_update() {
                return;
            }
            if receiver.changed().await.is_err() {
                return;
            }
        }
    }
    /// Await terminalization intent, before resource callbacks may join children.
    pub async fn settlement_started(&self) {
        let mut receiver = self.settlement_started.subscribe();
        loop {
            if *receiver.borrow_and_update() {
                return;
            }
            if receiver.changed().await.is_err() {
                return;
            }
        }
    }
    pub fn hold(
        &self,
        name: ResourceName,
        order: i32,
        recoverable: bool,
        release: Release,
    ) -> Result<(), SessionError> {
        let mut state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
        if state.receipt.is_some() || state.phase == Phase::Settling {
            return Err(SessionError::Settling);
        }
        if state.resources.iter().any(|resource| resource.name == name) {
            return Err(SessionError::DuplicateResource);
        }
        if state.resources.len() >= MAX_RESOURCES_PER_EXECUTION {
            return Err(SessionError::ResourceCapacity);
        }
        state.held.push(name);
        state.resources.push(Resource {
            name,
            release,
            order,
            recoverable,
        });
        Ok(())
    }
    pub fn bind_acquired(
        &self,
        name: ResourceName,
        order: i32,
        recoverable: bool,
        release: Release,
        rollback: Box<dyn FnOnce() + Send>,
        phase: Option<Phase>,
    ) -> Result<(), SessionError> {
        if let Err(error) = self.hold(name, order, recoverable, release) {
            // Failed unbound rollback cannot hide the original registration
            // failure; the adapter's durable/TTL recovery still owns its debt.
            let _ = catch_unwind(AssertUnwindSafe(rollback));
            return Err(error);
        }
        if let Some(phase) = phase {
            if let Err(error) = self.advance(phase) {
                self.settle(Phase::Failed, &format!("{}_bind_failed", name.as_str()))?;
                return Err(error);
            }
        }
        Ok(())
    }
    pub fn settle(
        &self,
        outcome: Phase,
        cause: &str,
    ) -> Result<Arc<TerminalReceipt>, SessionError> {
        if !terminal(outcome) {
            return Err(SessionError::TerminalRequiresSettlement);
        }
        let current_thread = std::thread::current().id();
        let (mut resources, context) = {
            let mut state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
            loop {
                if let Some(receipt) = &state.receipt {
                    return Ok(Arc::clone(receipt));
                }
                match state.settling_thread {
                    Some(owner) if owner == current_thread => {
                        return Err(SessionError::ReentrantSettlement)
                    }
                    Some(_) => {
                        state = self
                            .finished
                            .wait(state)
                            .map_err(|_| SessionError::StatePoisoned)?;
                    }
                    None => break,
                }
            }
            state.settling_thread = Some(current_thread);
            state.phase = Phase::Settling;
            state.phase_at = (self.clock)();
            let context = SettlementContext {
                execution_id: self.execution_id.clone(),
                kind: self.kind.clone(),
                owner_user_id: self.owner_user_id,
                outcome,
                cause: clip(cause, 128),
                dispatch_started: state.dispatch_started,
            };
            (std::mem::take(&mut state.resources), context)
        };
        resources.sort_by(|a, b| b.order.cmp(&a.order).then(a.name.cmp(&b.name)));
        self.settlement_started.send_replace(true);
        let mut dispositions = Vec::with_capacity(resources.len());
        for resource in resources {
            let result = catch_unwind(AssertUnwindSafe(|| (resource.release)(&context)));
            let disposition = match result {
                Ok(Ok(ReleaseAck::Released)) => ResourceDisposition::Released,
                Ok(Ok(ReleaseAck::Deferred)) => ResourceDisposition::Deferred,
                _ if resource.recoverable => ResourceDisposition::Deferred,
                _ => ResourceDisposition::Failed,
            };
            dispositions.push((resource.name, disposition));
            self.state
                .lock()
                .map_err(|_| SessionError::StatePoisoned)?
                .held
                .retain(|name| *name != resource.name);
        }
        let valid = dispositions
            .iter()
            .all(|(_, disposition)| *disposition != ResourceDisposition::Failed);
        let now = (self.clock)();
        let receipt = Arc::new(TerminalReceipt {
            execution_id: self.execution_id.clone(),
            kind: self.kind.clone(),
            requested_outcome: outcome,
            outcome: if valid { outcome } else { Phase::Failed },
            cause: if !valid && context.cause.is_empty() {
                "terminal_resource_invariant_failed".into()
            } else {
                context.cause
            },
            invariants_satisfied: valid,
            resource_dispositions: dispositions,
            duration: now.saturating_sub(self.created),
        });
        {
            let mut state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
            state.phase = receipt.outcome;
            state.phase_at = now;
            state.heartbeat_at = now;
            state.receipt = Some(Arc::clone(&receipt));
            state.settling_thread = None;
        }
        self.finished.notify_all();
        if let Some(registry) = self.registry.upgrade() {
            let mut members = registry.lock().map_err(|_| SessionError::StatePoisoned)?;
            if members
                .get(&self.execution_id)
                .is_some_and(|member| std::ptr::eq(member.as_ptr(), self))
            {
                members.remove(&self.execution_id);
            }
        }
        Ok(receipt)
    }
    pub fn snapshot(&self) -> Result<Snapshot, SessionError> {
        let state = self.state.lock().map_err(|_| SessionError::StatePoisoned)?;
        let now = (self.clock)();
        let mut held_resources = state.held.clone();
        held_resources.sort();
        Ok(Snapshot {
            kind: self.kind.clone(),
            phase: state.phase,
            age: now.saturating_sub(self.created),
            phase_age: now.saturating_sub(state.phase_at),
            heartbeat_age: now.saturating_sub(state.heartbeat_at),
            deadline_remaining_nanos: self
                .deadline
                .map(|deadline| deadline.as_nanos() as i128 - now.as_nanos() as i128),
            dispatch_started: state.dispatch_started,
            cancel_requested: state.cancel_requested,
            held_resources,
        })
    }
}

impl Drop for ExecutionSession {
    fn drop(&mut self) {
        // Abandoned native scopes release live resources as cancellation;
        // they do not publish a durable task verdict or successful output.
        let _ = self.settle(Phase::Cancelled, "execution_session_dropped");
    }
}
