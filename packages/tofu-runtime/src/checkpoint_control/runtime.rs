//! Backend-neutral port for the existing control and journal owners.
//! The authority supplies durable transactions, identity/lineage proof and a
//! joined lifetime. This interface never opens a store or selects a backend.
use crate::checkpoint_journal::{ActorHandoffSpec, Error, OwnedLease, ResumeLineage, Scope};
use serde_json::Value;

pub trait CheckpointControlRuntime: Send + Sync {
    fn checkpoint_owner_open(&self, owner: u64) -> Result<(), Error>;
    fn checkpoint_lease(&self, scope: Scope, header_bytes: usize, snapshot_bytes: usize,
        identity_bytes: usize, token: &str) -> Result<OwnedLease, Error>;
    fn checkpoint_recover(&self, scope: Scope, header_bytes: usize, snapshot_bytes: usize,
        identity_bytes: usize, token: &str) -> Result<OwnedLease, Error>;
    fn checkpoint_handoff(&self, scope: Scope, header_bytes: usize, snapshot_bytes: usize,
        identity_bytes: usize, token: &str, lineage: &ResumeLineage) -> Result<OwnedLease, Error>;
    fn checkpoint_frontier(&self, owner: u64, task: &str, header_bytes: usize,
        snapshot_bytes: usize, identity_bytes: usize) -> Result<Option<Value>, Error>;
    fn checkpoint_invoice(&self, owner: u64, task: &str, header_bytes: usize) -> Result<Option<Value>, Error>;
    fn checkpoint_goal_claim(&self, owner: u64, header_bytes: usize, snapshot_bytes: usize,
        request: &Value, evidence_bytes: usize) -> Result<Value, Error>;
    fn checkpoint_goal_handoff(&self, owner: u64, header_bytes: usize, snapshot_bytes: usize,
        identity_bytes: usize, specs: Vec<ActorHandoffSpec>, authorization: &Value,
        evidence_bytes: usize) -> Result<Vec<OwnedLease>, Error>;
}
