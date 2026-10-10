//! Caller-owned per-round admission/accounting over native physical outcomes.
//! One run ledger certifies joined reservation settlement; no price, routing,
//! usage normalization, refund or durable authority is invented here.
use crate::{
    agent::ProviderError,
    execution_contract::ResourceName,
    provider::StreamResult,
    session::{ExecutionSession, ReleaseAck, ReleaseError},
};
use serde_json::{json, Value};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc,
};
/// Returned preserves all provider facts; it does not certify positive finish
/// or complete usage. Owners must not refund finite authority from unknown usage.
pub enum Evidence<'a> {
    Returned(&'a StreamResult),
    Failed(&'a Value),
    Stopped,
    NotDispatched,
    Abandoned,
}
type Settle = Box<dyn for<'a> FnOnce(Evidence<'a>) -> Result<(), Value> + Send>;
pub struct Permit {
    settle: Option<Settle>,
    ledger: Option<Arc<State>>,
    messages: Option<Vec<Value>>,
}
impl Permit {
    pub fn new(settle: Settle) -> Self {
        Self {
            settle: Some(settle),
            ledger: None,
            messages: None,
        }
    }
    /// Trusted round preparation replaces only canonical history. Agent validates
    /// its retained-byte bound before dispatch; route/catalog authority is unchanged.
    pub fn with_messages(mut self, messages: Vec<Value>) -> Self {
        self.messages = Some(messages);
        self
    }
    pub(crate) fn take_messages(&mut self) -> Option<Vec<Value>> {
        self.messages.take()
    }
    pub(crate) fn attach(mut self, ledger: &Ledger) -> Self {
        ledger.0.pending.fetch_add(1, Ordering::SeqCst);
        self.ledger = Some(ledger.0.clone());
        self
    }
    fn finish(&mut self, evidence: Evidence<'_>) -> Result<(), Value> {
        let Some(callback) = self.settle.take() else {
            return Ok(());
        };
        let result = catch_unwind(AssertUnwindSafe(|| callback(evidence))).unwrap_or_else(|_| {
            Err(json!({"kind":"round_settlement","message":"native round settlement panicked"}))
        });
        if let Some(state) = self.ledger.take() {
            if result.is_err() {
                state.failed.store(true, Ordering::SeqCst);
            }
            state.pending.fetch_sub(1, Ordering::SeqCst);
        }
        result
    }
    pub(crate) fn settle(
        mut self,
        result: &Result<StreamResult, ProviderError>,
    ) -> Result<(), Value> {
        self.finish(match result {
            Ok(value) => Evidence::Returned(value),
            Err(ProviderError::Stopped) => Evidence::Stopped,
            Err(ProviderError::Failed(error)) => Evidence::Failed(error),
        })
    }
    pub(crate) fn not_dispatched(mut self) -> Result<(), Value> {
        self.finish(Evidence::NotDispatched)
    }
}
impl Drop for Permit {
    fn drop(&mut self) {
        let _ = self.finish(Evidence::Abandoned);
    }
}
pub struct Context<'a> {
    pub owner: u64,
    pub task_id: &'a str,
    pub round: usize,
    pub(crate) session: &'a Arc<ExecutionSession>,
    pub messages: &'a [Value],
}
impl Context<'_> {
    pub fn snapshot(&self) -> Result<crate::session::Snapshot, crate::session::SessionError> {
        self.session.snapshot()
    }
    pub async fn cancelled(&self) {
        let observer = self.session.stop_observer();
        let facts = observer.facts();
        let deadline = async {
            if let Some(nanos) = facts.deadline_remaining_nanos {
                if nanos <= 0 {
                    return;
                }
                let n = nanos as u128;
                let duration = std::time::Duration::new(
                    (n / 1_000_000_000).min(u64::MAX as u128) as u64,
                    (n % 1_000_000_000) as u32,
                );
                if let Some(at) = tokio::time::Instant::now().checked_add(duration) {
                    tokio::time::sleep_until(at).await;
                    return;
                }
            }
            std::future::pending::<()>().await;
        };
        tokio::select! {_=observer.stopped()=>{},_=deadline=>{}}
    }
    pub fn request_cancel(&self, cause: &str) -> Result<bool, crate::session::SessionError> {
        self.session.request_cancel(cause)
    }
}
pub trait Admission {
    /// Reservation construction must roll back failed acquisition. Waiting
    /// implementations observe parent Stop/deadline and quiesce before return.
    fn reserve(&self, context: Context<'_>) -> Result<Permit, Value>;
}
struct State {
    pending: AtomicUsize,
    failed: AtomicBool,
}
pub(crate) struct Ledger(Arc<State>);
impl Ledger {
    pub(crate) fn bind(session: &ExecutionSession) -> Result<Self, Value> {
        let state = Arc::new(State {
            pending: AtomicUsize::new(0),
            failed: AtomicBool::new(false),
        });
        let release = state.clone();
        session.bind_acquired(ResourceName::ModelRoute,0,false,Box::new(move |_|{
            if release.pending.load(Ordering::SeqCst)==0 && !release.failed.load(Ordering::SeqCst){Ok(ReleaseAck::Released)}else{Err(ReleaseError)}
        }),Box::new(||{}),None).map_err(|_|json!({"kind":"round_admission_binding","message":"native round resource binding refused"}))?;
        Ok(Self(state))
    }
}
