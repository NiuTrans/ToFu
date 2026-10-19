//! Counted model authorization over one explicitly selected account authority.
//! Entry: Admission and Lease. Callers provide owner/task-bound, joined ports.
//! No tokenizer, HTTP, routing, pricing policy, account mirror or retry lives here.
use crate::{model_account::Counts, session::ExecutionSession};
use serde_json::Value;
/// Read-only stop/deadline observation. It retains wake channels and a clock,
/// never the parent session or its terminal/resource authority.
#[derive(Clone)]
pub struct Observation(crate::session::StopObserver);
impl Observation {
    pub(crate) fn new(session: &ExecutionSession) -> Self {
        Self(session.stop_observer())
    }
    pub fn is_stopped(&self) -> bool {
        let facts = self.0.facts();
        facts.cancellation_requested
            || facts.closing
            || facts.deadline_remaining_nanos.is_some_and(|n| n <= 0)
    }
    pub fn deadline_remaining(&self) -> Option<std::time::Duration> {
        self.0.facts().deadline_remaining_nanos.map(|nanos| {
            let nanos = nanos.max(0) as u128;
            std::time::Duration::new(
                (nanos / 1_000_000_000).min(u64::MAX as u128) as u64,
                (nanos % 1_000_000_000) as u32,
            )
        })
    }
    pub async fn cancelled(&self) {
        if self.is_stopped() {
            return;
        }
        let deadline = async {
            if let Some(nanos) = self.0.facts().deadline_remaining_nanos {
                if nanos <= 0 {
                    return;
                }
                let nanos = nanos as u128;
                let duration = std::time::Duration::new(
                    (nanos / 1_000_000_000).min(u64::MAX as u128) as u64,
                    (nanos % 1_000_000_000) as u32,
                );
                tokio::time::sleep(duration).await;
            } else {
                std::future::pending::<()>().await;
            }
        };
        tokio::select! { _ = self.0.stopped() => {}, _ = deadline => {} }
    }
}

/// Select one authority before constructing a counted provider. Unknown delivery
/// must remain fenced by that authority; callbacks never grant route credentials.
pub trait Admission: Send + Sync {
    fn authorize(&self, owner: u64, task: &str) -> Result<(), Value>;
    fn reserve(
        &self,
        owner: u64,
        task: &str,
        counts: Counts,
        observation: &Observation,
    ) -> Result<Box<dyn Lease>, Value>;
}
/// One physical request reservation. Output cannot exceed the counted proposal.
/// Dispatch intent is acknowledged before HTTP; all calls must join physical I/O.
/// Failed/unknown dispatch may have committed and must never become an unsent
/// refund. Settlement failures retain unknown usage at the selected authority.
pub trait Lease: Send {
    fn output_tokens(&self) -> u64;
    fn mark_dispatched(&mut self, observation: &Observation) -> Result<(), Value>;
    /// Complete, trusted per-attempt headers after acknowledged dispatch intent.
    /// None retains configured headers. Returned headers replace them and must
    /// never enter usage/account/checkpoint evidence.
    fn mark_dispatched_with_headers(&mut self, observation: &Observation) -> Result<Option<reqwest::header::HeaderMap>, Value> {
        self.mark_dispatched(observation)?;
        Ok(None)
    }
    fn settle(&mut self, usage: Option<&Value>, unsent: bool) -> Result<(), Value>;
    /// Optional owned fee projection after budget settlement. It grants no
    /// wallet authority and must be delivered through the existing round port.
    fn settle_with_cost(&mut self,usage:Option<&Value>,unsent:bool) -> Result<Option<Box<Value>>,Value> {
        self.settle(usage,unsent)?;
        Ok(None)
    }

}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn observation_never_retains_parent_resource_authority() {
        use crate::{
            execution_contract::ResourceName,
            session::{Registry, ReleaseAck},
        };
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        let session = Registry::new().start("root", "task", 7, "", None).unwrap();
        let weak = Arc::downgrade(&session);
        let released = Arc::new(AtomicBool::new(false));
        let flag = released.clone();
        session
            .hold(
                ResourceName::Route,
                0,
                false,
                Box::new(move |_| {
                    flag.store(true, Ordering::Release);
                    Ok(ReleaseAck::Released)
                }),
            )
            .unwrap();
        let observation = Observation::new(&session);
        drop(session);
        assert!(weak.upgrade().is_none());
        assert!(released.load(Ordering::Acquire));
        assert!(observation.is_stopped());
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            tokio::time::timeout(std::time::Duration::from_secs(1), observation.cancelled())
                .await
                .unwrap();
        });
    }
    #[test]
    fn observation_wait_inherits_fixed_deadline_without_cancelling_parent() {
        let session = crate::session::Registry::new()
            .start(
                "root",
                "task",
                7,
                "",
                Some(std::time::Duration::from_millis(20)),
            )
            .unwrap();
        let observation = Observation::new(&session);
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            tokio::time::timeout(std::time::Duration::from_secs(1), observation.cancelled())
                .await
                .unwrap();
        });
        assert!(observation.is_stopped());
        assert!(!session.snapshot().unwrap().cancel_requested);
    }
}
