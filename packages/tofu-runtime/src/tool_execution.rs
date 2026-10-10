//! One tool occurrence's live scope, cooperative stop and terminal settlement.
//! Reuses ExecutionSession resource ownership; no detached workers, task status,
//! durable result storage or provider retry policy lives here.
use crate::{
    execution_contract::{ExecutionPhase as Phase, ResourceName},
    session::{ExecutionSession, Registry, Release, SessionError, TerminalReceipt},
    tool_contract::DispatchError,
};
use serde_json::Value;
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    Ready,
    Running,
    Settling,
    Terminal,
}
pub struct Scope {
    owner: u64,
    id: String,
    session: Arc<ExecutionSession>,
    state: Mutex<State>,
    parent: Option<crate::session::StopObserver>,
    parent_id: Option<String>,
}
#[derive(Debug)]
pub enum StartError {
    WrongOwner,
    AlreadyDispatched,
    Session(SessionError),
}
#[derive(Debug)]
pub struct Outcome {
    pub value: Option<Value>,
    pub error: Option<DispatchError>,
    pub receipt: Arc<TerminalReceipt>,
    pub cancellation_requested: bool,
    pub deadline_exceeded: bool,
    pub parent_closing: bool,
}
impl Outcome {
    pub fn into_result(self) -> Result<Value, DispatchError> {
        match (self.value, self.error) {
            (Some(value), None) => Ok(value),
            (_, Some(error)) => Err(error),
            _ => Err(DispatchError::ResourceInvariant),
        }
    }
}
impl Scope {
    pub fn new(
        registry: &Registry,
        owner: u64,
        id: &str,
        request_id: &str,
        deadline: Option<Duration>,
    ) -> Result<Self, SessionError> {
        Ok(Self {
            owner,
            id: id.into(),
            session: registry.start(id, "tool", owner, request_id, deadline)?,
            state: Mutex::new(State::Ready),
            parent: None,
            parent_id: None,
        })
    }
    pub fn with_parent(
        registry: &Registry,
        owner: u64,
        id: &str,
        request_id: &str,
        deadline: Option<Duration>,
        parent: Arc<ExecutionSession>,
        parent_id: &str,
    ) -> Result<Self, SessionError> {
        if !parent.belongs_to(owner, parent_id)
            || matches!(parent.snapshot()?.phase, Phase::Settling)
            || parent.snapshot()?.phase.is_terminal()
        {
            return Err(SessionError::InvalidIdentity);
        }
        let mut scope = Self::new(registry, owner, id, request_id, deadline)?;
        scope.parent = Some(parent.stop_observer());
        scope.parent_id = Some(parent_id.into());
        Ok(scope)
    }
    fn observations(&self) -> Result<(crate::session::Snapshot, bool), SessionError> {
        let mut snapshot = self.session.snapshot()?;
        let mut parent_closing = false;
        if let Some(parent) = &self.parent {
            let parent = parent.facts();
            snapshot.cancel_requested |= parent.cancellation_requested;
            parent_closing = parent.closing;
            snapshot.deadline_remaining_nanos = match (
                snapshot.deadline_remaining_nanos,
                parent.deadline_remaining_nanos,
            ) {
                (Some(child), Some(parent)) => Some(child.min(parent)),
                (child, parent) => child.or(parent),
            };
        }
        Ok((snapshot, parent_closing))
    }
    async fn wait_for_stop(&self) {
        let (snapshot, parent_closing) = match self.observations() {
            Ok(facts) => facts,
            Err(_) => return,
        };
        if parent_closing
            || snapshot.cancel_requested
            || snapshot
                .deadline_remaining_nanos
                .is_some_and(|remaining| remaining <= 0)
        {
            return;
        }
        let deadline = async {
            if let Some(nanos) = snapshot.deadline_remaining_nanos {
                let nanos = nanos as u128;
                let duration = Duration::new(
                    (nanos / 1_000_000_000).min(u64::MAX as u128) as u64,
                    (nanos % 1_000_000_000) as u32,
                );
                if let Some(deadline) = tokio::time::Instant::now().checked_add(duration) {
                    tokio::time::sleep_until(deadline).await;
                } else {
                    std::future::pending::<()>().await;
                }
            } else {
                std::future::pending::<()>().await;
            }
        };
        let parent = async {
            if let Some(parent) = &self.parent {
                parent.stopped().await;
            } else {
                std::future::pending::<()>().await;
            }
        };
        tokio::select! {_ = self.session.cancelled()=>{},_ = parent=>{},_ = deadline=>{}}
    }
    pub fn request_cancel(&self, owner: u64, cause: &str) -> Result<bool, StartError> {
        if owner != self.owner {
            return Err(StartError::WrongOwner);
        }
        let state = self
            .state
            .lock()
            .map_err(|_| StartError::Session(SessionError::StatePoisoned))?;
        if matches!(*state, State::Settling | State::Terminal) {
            return Ok(false);
        }
        self.session
            .request_cancel(cause)
            .map_err(StartError::Session)
    }
    pub fn bind_resource(
        &self,
        name: ResourceName,
        order: i32,
        recoverable: bool,
        release: Release,
        rollback: Box<dyn FnOnce() + Send>,
    ) -> Result<(), SessionError> {
        self.session
            .bind_acquired(name, order, recoverable, release, rollback, None)
    }
    pub fn snapshot(&self) -> Result<crate::session::Snapshot, SessionError> {
        self.observations().map(|(snapshot, _)| snapshot)
    }
    pub(crate) fn run<F>(&self, owner: u64, execute: F) -> Result<Outcome, StartError>
    where
        F: FnOnce(Context<'_>) -> Result<Value, DispatchError>,
    {
        if owner != self.owner || !self.session.belongs_to(owner, &self.id) {
            return Err(StartError::WrongOwner);
        }
        {
            let mut state = self
                .state
                .lock()
                .map_err(|_| StartError::Session(SessionError::StatePoisoned))?;
            if *state != State::Ready {
                return Err(StartError::AlreadyDispatched);
            }
            *state = State::Running;
        }
        let (snapshot, parent_closing) = self.observations().map_err(StartError::Session)?;
        let stopped = parent_closing
            || snapshot.cancel_requested
            || snapshot
                .deadline_remaining_nanos
                .is_some_and(|remaining| remaining <= 0);
        let result = if stopped {
            None
        } else {
            Some(
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    execute(Context { scope: self })
                }))
                .unwrap_or(Err(DispatchError::Panicked)),
            )
        };
        // Freeze observed stop facts before releasing resources. A late Stop cannot
        // rewrite a candidate while its terminal cleanup is already in progress.
        let snapshot = {
            let mut state = self
                .state
                .lock()
                .map_err(|_| StartError::Session(SessionError::StatePoisoned))?;
            *state = State::Settling;
            self.observations().map_err(StartError::Session)?
        };
        let (snapshot, parent_closing) = snapshot;
        let cancellation_requested = snapshot.cancel_requested;
        let deadline_exceeded = snapshot
            .deadline_remaining_nanos
            .is_some_and(|remaining| remaining <= 0);
        let (mut value, mut error) = match result {
            Some(Ok(value)) => (Some(value), None),
            Some(Err(error)) => (None, Some(error)),
            None => (None, None),
        };
        let (requested, cause) = if parent_closing && error.is_none() {
            error = Some(DispatchError::ParentClosing);
            (Phase::Cancelled, "tool_parent_closing")
        } else if matches!(error, Some(DispatchError::ParentClosing)) {
            (Phase::Cancelled, "tool_parent_closing")
        } else if matches!(error, Some(DispatchError::Cancelled)) {
            (Phase::Cancelled, "tool_cancelled")
        } else if matches!(error, Some(DispatchError::TimedOut)) {
            (Phase::TimedOut, "tool_deadline_exceeded")
        } else if error.is_some() {
            (Phase::Failed, "tool_handler_failed")
        } else if deadline_exceeded {
            error = Some(DispatchError::TimedOut);
            (Phase::TimedOut, "tool_deadline_exceeded")
        } else if cancellation_requested {
            error = Some(DispatchError::Cancelled);
            (Phase::Cancelled, "tool_cancelled")
        } else {
            (Phase::Completed, "tool_completed")
        };
        let receipt = self
            .session
            .settle(requested, cause)
            .map_err(StartError::Session)?;
        if !receipt.invariants_satisfied && error.is_none() {
            error = Some(DispatchError::ResourceInvariant)
        }
        if error.is_some() || receipt.outcome != Phase::Completed {
            value = None;
        }
        *self
            .state
            .lock()
            .map_err(|_| StartError::Session(SessionError::StatePoisoned))? = State::Terminal;
        Ok(Outcome {
            value,
            error,
            receipt,
            cancellation_requested,
            deadline_exceeded,
            parent_closing,
        })
    }
}
#[derive(Clone, Copy)]
pub struct Context<'a> {
    scope: &'a Scope,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stop {
    Cancelled,
    TimedOut,
    StateUnavailable,
    ParentClosing,
}
impl Context<'_> {
    pub(crate) fn parent_id(&self) -> Option<&str> {
        self.scope.parent_id.as_deref()
    }
    pub(crate) fn ensure_active(&self) -> Result<(), DispatchError> {
        self.checkpoint().map_err(|stop| match stop {
            Stop::Cancelled => DispatchError::Cancelled,
            Stop::TimedOut => DispatchError::TimedOut,
            Stop::StateUnavailable => DispatchError::ResourceInvariant,
            Stop::ParentClosing => DispatchError::ParentClosing,
        })
    }
    pub(crate) fn begin_dispatch(&self) -> Result<(), DispatchError> {
        self.ensure_active()?;
        if self
            .scope
            .session
            .advance(Phase::Dispatching)
            .map_err(DispatchError::Session)?
        {
            Ok(())
        } else {
            Err(DispatchError::ResourceInvariant)
        }
    }
    pub fn checkpoint(&self) -> Result<(), Stop> {
        let (snapshot, parent_closing) = self
            .scope
            .observations()
            .map_err(|_| Stop::StateUnavailable)?;
        if parent_closing {
            return Err(Stop::ParentClosing);
        }
        if snapshot
            .deadline_remaining_nanos
            .is_some_and(|remaining| remaining <= 0)
        {
            return Err(Stop::TimedOut);
        }
        if snapshot.cancel_requested {
            return Err(Stop::Cancelled);
        }
        Ok(())
    }
    pub fn request_cancel(&self, cause: &str) -> Result<bool, StartError> {
        self.scope.request_cancel(self.scope.owner, cause)
    }
    pub async fn cancelled(&self) {
        self.scope.wait_for_stop().await;
    }
    pub fn heartbeat(&self) -> Result<(), SessionError> {
        self.scope.session.heartbeat()
    }
    pub fn bind_resource(
        &self,
        name: ResourceName,
        order: i32,
        recoverable: bool,
        release: Release,
        rollback: Box<dyn FnOnce() + Send>,
    ) -> Result<(), SessionError> {
        self.scope
            .bind_resource(name, order, recoverable, release, rollback)
    }
    pub fn snapshot(&self) -> Result<crate::session::Snapshot, SessionError> {
        self.scope.snapshot()
    }
}
