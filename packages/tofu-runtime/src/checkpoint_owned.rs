//! Owned callback composition for the existing Journal/Lease transition owner.
//! An Arc retains the caller's exact Store; only lease metadata survives calls.
//! Each call reconstructs a borrowed Lease with its original volatile flags.
//! No public attachment, takeover, history cache, worker or automatic release.
use super::{journal_error, Error, Journal, Lease, LeaseState, Policy, Recovery, Scope, Store};
use crate::agent_checkpoint::{Disposition, Identity, Port, View};
use crate::task::TerminalPersistence;
use serde_json::Value;
use std::borrow::Cow;
use std::sync::{atomic::AtomicBool, Arc};
#[path = "checkpoint_actor_handoff.rs"]
mod actor_handoff;
pub use actor_handoff::{ActorHandoff, ActorHandoffSpec};

/// Owns one acquired lease without borrowing a stack-local Journal.
/// Calls retain the same flags; durable CAS remains the concurrency authority.
/// Drop deliberately leaves the durable fence in place.
pub struct OwnedLease {
    scope: Scope,
    store: Arc<dyn Store>,
    snapshot_bytes: usize,
    identity_bytes: usize,
    state: Arc<LeaseState>,
}
impl OwnedLease {
    pub fn handoff(scope: Scope, store: Arc<dyn Store>, snapshot_bytes: usize,
        identity_bytes: usize, token: &str, lineage: &super::ResumeLineage) -> Result<Self, Error> {
        let journal = Journal::new(scope, store.as_ref(), snapshot_bytes, identity_bytes)?;
        let state = journal.handoff(token, lineage)?.state.clone();
        let scope = journal.scope.into_owned();
        Ok(Self { scope, store, snapshot_bytes, identity_bytes, state })
    }
    pub fn acquire(
        scope: Scope,
        store: Arc<dyn Store>,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
    ) -> Result<Self, Error> {
        Self::acquire_with_recovery(scope, store, snapshot_bytes, identity_bytes, token, false)
    }
    pub fn recover(
        scope: Scope,
        store: Arc<dyn Store>,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
    ) -> Result<Self, Error> {
        Self::acquire_with_recovery(scope, store, snapshot_bytes, identity_bytes, token, true)
    }
    fn acquire_with_recovery(
        scope: Scope,
        store: Arc<dyn Store>,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
        recover: bool,
    ) -> Result<Self, Error> {
        let journal = Journal::new(scope, store.as_ref(), snapshot_bytes, identity_bytes)?;
        let state = if recover {
            journal.recover(token)?
        } else {
            journal.acquire(token)?
        }
        .state
        .clone();
        let scope = journal.scope.into_owned();
        Ok(Self {
            scope,
            store,
            snapshot_bytes,
            identity_bytes,
            state,
        })
    }
    fn with_lease<T>(&self, call: impl FnOnce(Lease<'_, '_>) -> T) -> Result<T, Error> {
        // Scope/budgets were admitted at acquisition and stay immutable.
        // Borrow metadata on callbacks rather than allocate its strings again.
        let journal = Journal {
            scope: Cow::Borrowed(&self.scope),
            store: self.store.as_ref(),
            snapshot_bytes: self.snapshot_bytes,
            identity_bytes: self.identity_bytes,
        };
        Ok(call(Lease {
            journal: &journal,
            state: self.state.clone(),
        }))
    }
    pub fn load(&self) -> Result<Option<Value>, Error> {
        self.with_lease(|lease| lease.load())?
    }
    pub fn begin(&self, round: u64) -> Result<(), Error> {
        self.with_lease(|lease| lease.begin(round))?
    }
    pub fn complete(&self, state: &Value) -> Result<(), Error> {
        self.with_lease(|lease| lease.complete(state))?
    }
    pub fn pending(&self, state: &Value) -> Result<(), Error> {
        self.with_lease(|lease| lease.pending(state))?
    }
    /// The caller freezes disposition before terminal debt retries. Only a
    /// committed Suspend plus Aborted may preserve the completed frontier.
    pub fn finish(&self, status: crate::task::TaskStatus, suspended: bool) -> Result<(), Error> {
        use crate::task::TaskStatus;
        if !matches!(
            status,
            TaskStatus::Done | TaskStatus::Error | TaskStatus::Aborted
        ) {
            return Err(Error::Invalid);
        }
        self.with_lease(|lease| {
            lease.journal.finish_owned(
                &lease.state.token,
                lease.state.generation,
                suspended && status == TaskStatus::Aborted,
            )
        })?
    }
    pub fn release(&self) -> Result<(), Error> {
        self.with_lease(|lease| lease.release())?
    }
    pub fn seal(&self) -> Result<(), Error> {
        self.with_lease(|lease| lease.seal())?
    }
}

/// Agent port with an owned Store and explicit thread-safe recovery callback.
/// Recovery retains its own financial/time authority and joined lifecycle.
pub struct OwnedPolicy {
    lease: OwnedLease,
    recovery: Arc<dyn Recovery + Send + Sync>,
    suspend: bool,
    suspended: Arc<AtomicBool>,
}
impl OwnedPolicy {
    pub fn new(
        lease: OwnedLease,
        recovery: Arc<dyn Recovery + Send + Sync>,
        suspend: bool,
    ) -> Self {
        Self {
            lease,
            recovery,
            suspend,
            suspended: Arc::new(AtomicBool::new(false)),
        }
    }
    fn with_policy<T>(&self, call: impl FnOnce(&Policy<'_, '_>) -> T) -> Result<T, Error> {
        self.lease.with_lease(|lease| {
            let policy = Policy {
                lease,
                recovery: self.recovery.as_ref(),
                suspend: self.suspend,
                suspended: self.suspended.clone(),
            };
            call(&policy)
        })
    }
    /// Only the execution owner may release after a joined committed Suspend.
    pub fn release(&self) -> Result<(), Error> {
        self.lease.release()
    }
    pub fn terminal_persistence(
        &self,
        delegate: Arc<dyn TerminalPersistence>,
    ) -> Result<Arc<dyn TerminalPersistence>, Error> {
        self.with_policy(|policy| policy.terminal_persistence(self.lease.store.clone(), delegate))?
    }
}
impl Port for OwnedPolicy {
    fn binding(&self) -> &str {
        &self.lease.scope.binding
    }
    fn load(&self, identity: Identity<'_>) -> Result<Option<Value>, Value> {
        self.with_policy(|policy| policy.load(identity))
            .map_err(journal_error)?
    }
    fn begin(&self, identity: Identity<'_>, round: usize) -> Result<(), Value> {
        self.with_policy(|policy| policy.begin(identity, round))
            .map_err(journal_error)?
    }
    fn complete(&self, state: View<'_>) -> Result<Disposition, Value> {
        self.with_policy(|policy| policy.complete(state))
            .map_err(journal_error)?
    }
    fn pending(&self, state: View<'_>) -> Result<(), Value> {
        self.with_policy(|policy| policy.pending(state)).map_err(journal_error)?
    }
    fn reconcile(&self, identity: Identity<'_>, state: &Value)
        -> Result<Vec<crate::agent_checkpoint::ReconciledTool>, Value> {
        self.with_policy(|policy| policy.reconcile(identity, state)).map_err(journal_error)?
    }
}
