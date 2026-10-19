//! Bind the shared Rust checkpoint control owner to this exact TofuDB runtime.
//! The stack adapter borrows the existing Arc; acquired leases retain the same
//! lifecycle and authority through CheckpointRuntime. No extra allocation/worker.
use crate::checkpoint_runtime::CheckpointRuntime;
use std::{io, sync::Arc};
use serde_json::Value;
use tofu_runtime::checkpoint_control::{CheckpointControlRuntime, Registry as SharedRegistry};
use tofu_runtime::checkpoint_journal::{ActorHandoffSpec, Error, OwnedLease, ResumeLineage, Scope};

pub(crate) struct Registry(SharedRegistry);
impl Default for Registry {
    fn default() -> Self {
        Self(SharedRegistry::new(crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES)
            .expect("native document capacity must be positive"))
    }
}
impl Registry {
    pub(crate) fn response_bound(&self, operation: &str, bytes: &[u8]) -> io::Result<usize> {
        self.0.response_bound(operation, bytes)
    }
    pub(crate) fn execute<R: CheckpointRuntime>(&self, runtime: Arc<R>, operation: &str,
        bytes: &[u8], deadline: u64) -> io::Result<Vec<u8>> {
        self.0.execute(&RuntimePort(&runtime), operation, bytes, deadline)
    }
    #[cfg(test)]
    fn live_capabilities(&self) -> io::Result<usize> { self.0.live_capabilities() }
}
struct RuntimePort<'a, R: CheckpointRuntime>(&'a Arc<R>);
impl<R: CheckpointRuntime> CheckpointControlRuntime for RuntimePort<'_, R> {
    fn checkpoint_owner_open(&self, owner:u64)->Result<(),Error>{self.0.checkpoint_owner_open(owner)}
    fn checkpoint_lease(&self, scope:Scope, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, token:&str)->Result<OwnedLease,Error>{
        self.0.checkpoint_lease(scope,header_bytes,snapshot_bytes,identity_bytes,token)
    }
    fn checkpoint_recover(&self, scope:Scope, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, token:&str)->Result<OwnedLease,Error>{
        self.0.checkpoint_recover(scope,header_bytes,snapshot_bytes,identity_bytes,token)
    }
    fn checkpoint_handoff(&self, scope:Scope, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, token:&str, lineage:&ResumeLineage)->Result<OwnedLease,Error>{
        self.0.checkpoint_handoff(scope,header_bytes,snapshot_bytes,identity_bytes,token,lineage)
    }
    fn checkpoint_frontier(&self, owner:u64, task:&str, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize)->Result<Option<Value>,Error>{
        self.0.checkpoint_frontier(owner,task,header_bytes,snapshot_bytes,identity_bytes)
    }
    fn checkpoint_invoice(&self, owner:u64, task:&str, header_bytes:usize)->Result<Option<Value>,Error>{
        self.0.checkpoint_invoice(owner,task,header_bytes)
    }
    fn checkpoint_goal_claim(&self, owner:u64, header_bytes:usize, snapshot_bytes:usize, request:&Value, evidence:usize)->Result<Value,Error>{
        self.0.checkpoint_goal_claim(owner,header_bytes,snapshot_bytes,request,evidence)
    }
    fn checkpoint_goal_handoff(&self, owner:u64, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, specs:Vec<ActorHandoffSpec>,
        authorization:&Value,evidence:usize)->Result<Vec<OwnedLease>,Error>{
        self.0.checkpoint_goal_handoff(owner,header_bytes,snapshot_bytes,identity_bytes,specs,authorization,evidence)
    }
}
#[cfg(test)]
#[path = "embedded_checkpoint_control_tests.rs"]
mod tests;
