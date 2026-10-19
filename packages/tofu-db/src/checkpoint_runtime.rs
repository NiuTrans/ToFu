//! Shared checkpoint callbacks on one authenticated, lifecycle-owned authority.
//! Runtime lifecycle guards join entered Store calls before driver retirement.
//! No authority open, worker, alternate backend, token attachment or journal logic.

use crate::native_checkpoint_store::NativeCheckpointStore;
use std::sync::{Arc, Mutex, RwLockReadGuard};
use crate::{authority::AuthorityDatabase, semantic::AuthenticatedScope};
use tofu_runtime::checkpoint_journal::{ActorHandoff, ActorHandoffSpec, Error, Header, Handoff, OwnedLease, ResumeLineage, Scope, SnapshotWrite, Store};

pub(crate) struct RuntimeStore<R: CheckpointRuntime> {
    runtime: Arc<R>,
    store: NativeCheckpointStore,
}
/// Transport supplies an authenticated authority and a joined close guard.
/// All journal, WAL and capability transitions remain shared across transports.
pub(crate) trait CheckpointRuntime: Send + Sync + Sized + 'static {
    fn checkpoint_guard(&self) -> Result<RwLockReadGuard<'_, ()>, Error>;
    fn checkpoint_database(&self) -> &Arc<Mutex<AuthorityDatabase>>;
    fn checkpoint_scope(&self) -> AuthenticatedScope;
    fn checkpoint_owner_open(&self,owner:u64)->Result<(),Error>{
        if owner!=self.checkpoint_scope().owner_id{return Err(Error::Invalid);}
        let _guard=self.checkpoint_guard()?;Ok(())
    }
    /// Whole Goal claim on this runtime's selected authority; no leases returned.
    fn checkpoint_goal_claim(&self, owner:u64, header_bytes:usize, snapshot_bytes:usize,
        request:&serde_json::Value, evidence_bytes:usize)->Result<serde_json::Value,Error>{
        if owner!=self.checkpoint_scope().owner_id{return Err(Error::Invalid);}
        let _guard=self.checkpoint_guard()?;
        NativeCheckpointStore::new(self.checkpoint_database().clone(),self.checkpoint_scope().tenant_id.unwrap_or(0),owner,
            header_bytes,snapshot_bytes)?.claim_goal_actors(request,evidence_bytes)
    }
    /// All callbacks retain the standard RuntimeStore lifecycle. No sibling
    /// becomes visible until the common whole-set store transaction acknowledges.
    fn checkpoint_goal_handoff(self:&Arc<Self>, owner:u64, header_bytes:usize,
        snapshot_bytes:usize, identity_bytes:usize, specs:Vec<ActorHandoffSpec>,
        authorization:&serde_json::Value, evidence_bytes:usize)->Result<Vec<OwnedLease>,Error>{
        if owner!=self.checkpoint_scope().owner_id || specs.iter().any(|spec|spec.scope.owner!=owner){return Err(Error::Invalid);}
        let guard=self.checkpoint_guard()?;
        let store=NativeCheckpointStore::new(self.checkpoint_database().clone(),self.checkpoint_scope().tenant_id.unwrap_or(0),owner,
            header_bytes,snapshot_bytes)?;
        drop(guard);
        OwnedLease::handoff_retired_actors(Arc::new(RuntimeStore {runtime:self.clone(),store}),
            specs,authorization,snapshot_bytes,identity_bytes,evidence_bytes)
    }
    /// Observe a dead writer's frontier on this selected authority; never acquire.
    fn checkpoint_frontier(&self, owner: u64, task: &str, header_bytes: usize,
        snapshot_bytes: usize, identity_bytes: usize) -> Result<Option<serde_json::Value>, Error> {
        if owner != self.checkpoint_scope().owner_id { return Err(Error::Invalid); }
        let _guard = self.checkpoint_guard()?;
        let store = NativeCheckpointStore::new(self.checkpoint_database().clone(),
            self.checkpoint_scope().tenant_id.unwrap_or(0), owner, header_bytes, snapshot_bytes)?;
        tofu_runtime::checkpoint_journal::inspect_abandoned_frontier(
            &store, owner, task, snapshot_bytes, identity_bytes)
    }

    /// A bounded debt read borrows the selected runtime and joins its shutdown.
    fn checkpoint_invoice(&self, owner: u64, task: &str, header_bytes: usize)
        -> Result<Option<serde_json::Value>, Error> {
        if owner != self.checkpoint_scope().owner_id { return Err(Error::Invalid); }
        let _guard = self.checkpoint_guard()?;
        NativeCheckpointStore::new(self.checkpoint_database().clone(), self.checkpoint_scope().tenant_id.unwrap_or(0),
            owner, header_bytes, 1)?.wallet_invoice(owner, task)
    }

    /// Bind an owned journal lease to this runtime's authenticated owner/tenant.
    /// Callers supply task/epoch/binding, token and explicit byte bounds. The
    /// shared Store and original runtime remain alive until the lease drops.
    /// Close/abandon joins entered calls; all later callbacks refuse before I/O.
    fn checkpoint_lease(
        self: &Arc<Self>,
        scope: Scope,
        header_bytes: usize,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
    ) -> Result<OwnedLease, Error> {
        self.checkpoint_lease_with_recovery(
            scope,
            header_bytes,
            snapshot_bytes,
            identity_bytes,
            token,
            false,
            None,
        )
    }
    /// Explicit abandoned-frontier recovery; the Store proves process death.
    /// Neither expiry nor absence from the capability registry grants ownership.
    fn checkpoint_recover(
        self: &Arc<Self>,
        scope: Scope,
        header_bytes: usize,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
    ) -> Result<OwnedLease, Error> {
        self.checkpoint_lease_with_recovery(
            scope,
            header_bytes,
            snapshot_bytes,
            identity_bytes,
            token,
            true,
            None,
        )
    }
    fn checkpoint_lease_with_recovery(
        self: &Arc<Self>,
        scope: Scope,
        header_bytes: usize,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
        recover: bool,
        lineage: Option<&ResumeLineage>,
    ) -> Result<OwnedLease, Error> {
        if scope.owner != self.checkpoint_scope().owner_id {
            return Err(Error::Invalid);
        }
        let guard = self.checkpoint_guard()?;
        let store = NativeCheckpointStore::new(
            self.checkpoint_database().clone(),
            self.checkpoint_scope().tenant_id.unwrap_or(0),
            self.checkpoint_scope().owner_id,
            header_bytes,
            snapshot_bytes,
        )?;
        drop(guard);
        let acquire = if recover {
            OwnedLease::recover
        } else {
            OwnedLease::acquire
        };
        let store = Arc::new(RuntimeStore {
                runtime: self.clone(),
                store,
            });
        if let Some(lineage) = lineage {
            OwnedLease::handoff(scope, store, snapshot_bytes, identity_bytes, token, lineage)
        } else { acquire(scope, store, snapshot_bytes, identity_bytes, token) }
    }
    fn checkpoint_handoff(self: &Arc<Self>, scope: Scope, header_bytes: usize,
        snapshot_bytes: usize, identity_bytes: usize, token: &str, lineage: &ResumeLineage) -> Result<OwnedLease, Error> {
        self.checkpoint_lease_with_recovery(scope, header_bytes, snapshot_bytes,
            identity_bytes, token, false, Some(lineage))
    }
}
impl<R: CheckpointRuntime> Store for RuntimeStore<R> {
    fn handoff_retired_actors(&self, actors:&[ActorHandoff], authorization:&serde_json::Value,
        evidence_bytes:usize)->Result<(),Error>{
        let _guard=self.runtime.checkpoint_guard()?;
        self.store.handoff_retired_actors(actors,authorization,evidence_bytes)
    }
    fn handoff(&self, request: Handoff<'_>) -> Result<u64, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store.handoff(request)
    }
    fn writer_process(&self) -> Option<serde_json::Value> {
        self.store.writer_process()
    }
    fn writer_quiescent(
        &self,
        owner: u64,
        task: &str,
        identity: &serde_json::Value,
    ) -> Result<bool, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store.writer_quiescent(owner, task, identity)
    }
    fn header(&self, owner: u64, task: &str) -> Result<Option<Header>, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store.header(owner, task)
    }
    fn compare_exchange(
        &self,
        owner: u64,
        task: &str,
        expected: u64,
        header: &serde_json::Value,
        snapshot: SnapshotWrite<'_>,
    ) -> Result<u64, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store
            .compare_exchange(owner, task, expected, header, snapshot)
    }
    fn snapshot(
        &self,
        owner: u64,
        task: &str,
        version: u64,
    ) -> Result<Option<serde_json::Value>, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store.snapshot(owner, task, version)
    }
}
