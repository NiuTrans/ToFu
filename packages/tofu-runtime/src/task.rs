//! Native task execution/terminal publication over explicit side-effect ports.
//! Session owns live resource settlement; task owns one immutable terminal
//! candidate and retryable persistence/publication debt. No storage dialects.
use crate::execution_contract::{
    ExecutionPhase as Phase, TASK_REPLAY_EVENT_TYPE_FIELD, TASK_REPLAY_TERMINAL_EVENT_TYPES,
};
use crate::session::{ExecutionSession, SessionError, TerminalReceipt};
use serde_json::{json, Value};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Mutex};

#[derive(Clone, Debug)]
pub struct TaskIdentity {
    pub task_id: String,
    pub owner_user_id: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TaskStatus {
    Pending,
    Running,
    Done,
    Error,
    Aborted,
}
impl TaskStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Running => "running",
            Self::Done => "done",
            Self::Error => "error",
            Self::Aborted => "aborted",
        }
    }
}
#[derive(Clone, Debug)]
pub struct TerminalCandidate {
    pub identity: TaskIdentity,
    pub status: TaskStatus,
    pub result: Option<Value>,
    pub error: Option<Value>,
    pub receipt: Arc<TerminalReceipt>,
}
impl TerminalCandidate {
    pub fn event(&self) -> Value {
        debug_assert!(TASK_REPLAY_TERMINAL_EVENT_TYPES.contains(&self.status.as_str()));
        let mut event = json!({"status":self.status.as_str()});
        event[TASK_REPLAY_EVENT_TYPE_FIELD] = Value::String(self.status.as_str().into());
        if let Some(error) = &self.error {
            event["error"] = error.clone();
        }
        if self.status == TaskStatus::Done {
            if let Some(result) = &self.result {
                event["result"] = result.clone();
            }
        }
        event
    }
}
#[derive(Clone, Debug)]
pub struct PortError;
pub trait TerminalPersistence: Send + Sync {
    /// Idempotently commit the owner's frozen task candidate before visibility.
    fn persist_once(&self, candidate: &TerminalCandidate) -> Result<(), PortError>;
}
pub trait TerminalPublisher: Send + Sync {
    /// Delivery retries must not allocate a second terminal event identity.
    fn publish_once(&self, candidate: &TerminalCandidate, event: &Value) -> Result<(), PortError>;
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskError {
    Unauthorized,
    InvalidIdentity,
    Busy,
    AlreadyTerminal,
    PersistencePending,
    DeliveryPending,
    StatePoisoned,
    Session(SessionError),
}
impl From<SessionError> for TaskError {
    fn from(value: SessionError) -> Self {
        Self::Session(value)
    }
}
struct State {
    status: TaskStatus,
    aborted: bool,
    executing: bool,
    terminalizing: bool,
    candidate: Option<Arc<TerminalCandidate>>,
    persisted: bool,
    delivered: bool,
}
pub struct Task {
    identity: TaskIdentity,
    session: Arc<ExecutionSession>,
    state: Mutex<State>,
    persistence: Option<Arc<dyn TerminalPersistence>>,
    publisher: Arc<dyn TerminalPublisher>,
}
impl Task {
    pub fn new(
        identity: TaskIdentity,
        session: Arc<ExecutionSession>,
        persistence: Option<Arc<dyn TerminalPersistence>>,
        publisher: Arc<dyn TerminalPublisher>,
    ) -> Result<Self, TaskError> {
        if identity.owner_user_id == 0 || identity.task_id.trim().is_empty() {
            return Err(TaskError::InvalidIdentity);
        }
        if !session.belongs_to(identity.owner_user_id, &identity.task_id) {
            return Err(TaskError::Unauthorized);
        }
        Ok(Self {
            identity,
            session,
            state: Mutex::new(State {
                status: TaskStatus::Pending,
                aborted: false,
                executing: false,
                terminalizing: false,
                candidate: None,
                persisted: false,
                delivered: false,
            }),
            persistence,
            publisher,
        })
    }
    pub(crate) fn execution_session(&self, owner: u64) -> Result<Arc<ExecutionSession>, TaskError> {
        self.authorize(owner)?;
        Ok(self.session.clone())
    }
    fn authorize(&self, owner: u64) -> Result<(), TaskError> {
        if owner != self.identity.owner_user_id {
            Err(TaskError::Unauthorized)
        } else {
            Ok(())
        }
    }
    pub fn status(&self, owner: u64) -> Result<TaskStatus, TaskError> {
        self.authorize(owner)?;
        Ok(self
            .state
            .lock()
            .map_err(|_| TaskError::StatePoisoned)?
            .status)
    }
    pub fn request_abort(&self, owner: u64, cause: &str) -> Result<bool, TaskError> {
        self.authorize(owner)?;
        let mut state = self.state.lock().map_err(|_| TaskError::StatePoisoned)?;
        if state.terminalizing || state.candidate.is_some() || state.delivered {
            return Ok(false);
        }
        state.aborted = true;
        self.session.request_cancel(cause)?;
        Ok(true)
    }
    /// Native task driver. Provider/tool composition supplies one execution port;
    /// a retry of terminal debt never calls it again.
    pub fn run<F>(&self, owner: u64, execute: F) -> Result<Arc<TerminalCandidate>, TaskError>
    where
        F: FnOnce(&ExecutionSession) -> Result<Value, Value>,
    {
        self.authorize(owner)?;
        {
            let mut state = self.state.lock().map_err(|_| TaskError::StatePoisoned)?;
            if state.executing || state.terminalizing {
                return Err(TaskError::Busy);
            }
            if state.status != TaskStatus::Pending || state.candidate.is_some() {
                return Err(TaskError::AlreadyTerminal);
            }
            state.status = TaskStatus::Running;
            state.executing = true;
        }
        let output = catch_unwind(AssertUnwindSafe(|| -> Result<Option<Value>, Value> {
            let snapshot = self
                .session
                .snapshot()
                .map_err(|_| json!({"kind":"generic","message":"execution state unavailable"}))?;
            if snapshot.cancel_requested
                || snapshot
                    .deadline_remaining_nanos
                    .is_some_and(|remaining| remaining <= 0)
            {
                self.session.request_cancel("deadline_or_cancel").map_err(
                    |_| json!({"kind":"generic","message":"execution cancellation failed"}),
                )?;
                return Ok(None);
            }
            match self.session.advance(Phase::Dispatching) {
                Ok(_) => execute(&self.session).map(Some),
                Err(_) => Err(json!({"kind":"generic","message":"execution dispatch failed"})),
            }
        }));
        self.state
            .lock()
            .map_err(|_| TaskError::StatePoisoned)?
            .executing = false;
        match output {
            Ok(Ok(value)) => self.finish(owner, value, None),
            Ok(Err(error)) => self.finish(owner, None, Some(error)),
            Err(_) => self.finish(
                owner,
                None,
                Some(json!({"kind":"generic","message":"native task execution panicked"})),
            ),
        }
    }
    pub fn finish(
        &self,
        owner: u64,
        result: Option<Value>,
        error: Option<Value>,
    ) -> Result<Arc<TerminalCandidate>, TaskError> {
        self.authorize(owner)?;
        let (candidate, aborted, persisted) = {
            let mut state = self.state.lock().map_err(|_| TaskError::StatePoisoned)?;
            if state.delivered {
                return Err(TaskError::AlreadyTerminal);
            }
            if state.terminalizing || state.executing {
                return Err(TaskError::Busy);
            }
            state.terminalizing = true;
            (state.candidate.clone(), state.aborted, state.persisted)
        };
        let attempt = self.finish_owned(candidate, aborted, persisted, result, error);
        self.state
            .lock()
            .map_err(|_| TaskError::StatePoisoned)?
            .terminalizing = false;
        attempt
    }
    fn finish_owned(
        &self,
        candidate: Option<Arc<TerminalCandidate>>,
        aborted: bool,
        mut persisted: bool,
        result: Option<Value>,
        mut error: Option<Value>,
    ) -> Result<Arc<TerminalCandidate>, TaskError> {
        let candidate = match candidate {
            Some(candidate) => candidate,
            None => {
                let snapshot = self.session.snapshot()?;
                let aborted = aborted
                    || snapshot.cancel_requested
                    || snapshot
                        .deadline_remaining_nanos
                        .is_some_and(|remaining| remaining <= 0);
                let requested = if error.is_some() {
                    Phase::Failed
                } else if aborted {
                    Phase::Cancelled
                } else {
                    Phase::Completed
                };
                let cause = error
                    .as_ref()
                    .and_then(|value| value.get("kind"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let receipt = self.session.settle(requested, cause)?;
                if receipt.outcome == Phase::Failed && error.is_none() {
                    error = Some(
                        json!({"kind":"generic","message":"execution failed before task terminal publication"}),
                    );
                }
                let cancelled = matches!(receipt.outcome, Phase::Cancelled | Phase::TimedOut);
                let status = if error.is_some() {
                    TaskStatus::Error
                } else if aborted || cancelled {
                    TaskStatus::Aborted
                } else {
                    TaskStatus::Done
                };
                let candidate = Arc::new(TerminalCandidate {
                    identity: self.identity.clone(),
                    status,
                    result: if status == TaskStatus::Done {
                        result
                    } else {
                        None
                    },
                    error,
                    receipt,
                });
                self.state
                    .lock()
                    .map_err(|_| TaskError::StatePoisoned)?
                    .candidate = Some(Arc::clone(&candidate));
                candidate
            }
        };
        if !persisted {
            if let Some(port) = &self.persistence {
                if !matches!(
                    catch_unwind(AssertUnwindSafe(|| port.persist_once(&candidate))),
                    Ok(Ok(()))
                ) {
                    return Err(TaskError::PersistencePending);
                }
            }
            let mut state = self.state.lock().map_err(|_| TaskError::StatePoisoned)?;
            state.persisted = true;
            state.status = candidate.status;
            persisted = true;
        }
        debug_assert!(persisted);
        let event = candidate.event();
        if !matches!(
            catch_unwind(AssertUnwindSafe(|| self
                .publisher
                .publish_once(&candidate, &event))),
            Ok(Ok(()))
        ) {
            return Err(TaskError::DeliveryPending);
        }
        self.state
            .lock()
            .map_err(|_| TaskError::StatePoisoned)?
            .delivered = true;
        Ok(candidate)
    }
}
