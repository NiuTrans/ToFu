//! Callback byte transport and authenticated Store; never a second journal.
use super::{contract::*, Callback};
use crate::checkpoint_control::CheckpointControlRuntime;
use crate::checkpoint_journal::{self as journal, ActorHandoffSpec, Error, Header, OwnedLease,
    ResumeLineage, Scope, SnapshotWrite, Store};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{ffi::c_void, io::Write, sync::Arc};

struct Host { callback: Callback, context: *mut c_void }
// The ABI contract requires a thread-safe callback/context, valid through joined
// close. No dereference occurs here: only the host callback consumes its pointer.
unsafe impl Send for Host {}
unsafe impl Sync for Host {}
pub(super) struct RuntimePort {
    host: Arc<Host>, tenant: u64, owner: u64, document_bytes: usize,
    identity_bytes: usize, writer: Option<Value>,
}
struct CallbackStore {
    host: Arc<Host>, tenant: u64, owner: u64, header_bytes: usize,
    snapshot_bytes: usize, identity_bytes: usize, writer: Option<Value>,
}
#[derive(Serialize)]
struct Request<'a> {
    format: &'static str, operation: &'static str, tenant_id: u64, owner_user_id: u64,
    task_id: &'a str, header_bytes: usize, snapshot_bytes: usize, expected_version: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    header: Option<&'a Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    snapshot: Option<&'a Value>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Response {
    ok: bool,
    #[serde(default)] version: Option<u64>,
    #[serde(default)] value: Option<Value>,
    #[serde(default)] error: Option<String>,
}
struct BoundedBytes { bytes: Vec<u8>, maximum: usize }
impl Write for BoundedBytes {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.maximum - self.bytes.len() { return Err(std::io::Error::other("checkpoint byte bound")); }
        self.bytes.try_reserve(bytes.len()).map_err(std::io::Error::other)?;
        self.bytes.extend_from_slice(bytes); Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
}
fn encode(value: &impl Serialize, maximum: usize) -> Result<Vec<u8>, Error> {
    let mut out = BoundedBytes { bytes: Vec::new(), maximum };
    serde_json::to_writer(&mut out, value).map_err(|_| Error::Oversize)?;
    Ok(out.bytes)
}
impl CallbackStore {
    fn task(&self, owner: u64, task: &str) -> Result<(), Error> {
        if owner != self.owner || task.is_empty() || task.len() > self.identity_bytes { return Err(Error::Invalid); }
        Ok(())
    }
    fn invoke(&self, operation: &'static str, task: &str, expected: u64,
        header: Option<&Value>, snapshot: Option<&Value>, response_bytes: usize) -> Result<Response, Error> {
        let request = Request { format: FORMAT, operation, tenant_id: self.tenant, owner_user_id: self.owner,
            task_id: task, header_bytes: self.header_bytes, snapshot_bytes: self.snapshot_bytes,
            expected_version: expected, header, snapshot };
        self.send(&request, response_bytes)
    }
    fn send(&self, request: &impl Serialize, response_bytes: usize) -> Result<Response, Error> {
        let bound = self.header_bytes.checked_mul(2).and_then(|n| n.checked_add(self.snapshot_bytes))
            .and_then(|n| self.identity_bytes.checked_mul(REQUEST_IDENTITY_ESCAPE_SLOTS).and_then(|i| n.checked_add(i)))
            .and_then(|n| n.checked_add(ENVELOPE_BYTES)).ok_or(Error::Oversize)?;
        self.send_bounded(request,response_bytes,bound)
    }
    fn send_bounded(&self,request:&impl Serialize,response_bytes:usize,bound:usize)->Result<Response,Error>{
        let bytes = encode(&request, bound)?;
        let capacity = response_bytes.checked_add(ENVELOPE_BYTES).ok_or(Error::Oversize)?;
        let mut response: Vec<u8> = Vec::new();
        response.try_reserve_exact(capacity).map_err(|_| Error::Storage)?;
        let mut length = 0;
        let status = unsafe { (self.host.callback)(self.host.context, bytes.as_ptr(), bytes.len(),
            response.as_mut_ptr(), capacity, &mut length) };
        if status != STATUS_OK || length == 0 || length > capacity { return Err(Error::Storage); }
        // The synchronous host must initialize exactly its acknowledged prefix.
        // Avoid clearing the entire caller-sized snapshot capacity on every read.
        unsafe { response.set_len(length); }
        let result: Response = serde_json::from_slice(&response).map_err(|_| Error::Storage)?;
        if result.ok {
            if result.error.is_some() { return Err(Error::Storage); }
            Ok(result)
        } else {
            if result.version.is_some() || result.value.is_some() { return Err(Error::Storage); }
            Err(match result.error.as_deref() {
                Some("invalid") => Error::Invalid, Some("conflict") => Error::Conflict,
                Some("ineligible") => Error::Ineligible, Some("oversize") => Error::Oversize,
                _ => Error::Storage,
            })
        }
    }
    fn value(&self, value: &Value, task: &str, maximum: usize) -> Result<(), Error> {
        if value["owner_user_id"].as_u64() != Some(self.owner) || value["task_id"] != task { return Err(Error::Invalid); }
        encode(value, maximum).map(|_| ())
    }
}
#[path = "checkpoint_storage_handoff.rs"]
mod handoff;
pub(super) use handoff::validate;
#[path="checkpoint_storage_goal.rs"]
mod goal;
pub(super) use goal::execute as goal_transaction;

impl Store for CallbackStore {
    fn handoff_retired_actors(&self,actors:&[journal::ActorHandoff],authorization:&Value,evidence:usize)->Result<(),Error>{
        let value=self.goal_callback("goal_handoff",authorization,actors,evidence)?;
        if value!=serde_json::json!({}){return Err(Error::Storage);}Ok(())
    }
    fn handoff(&self, request: journal::Handoff<'_>) -> Result<u64, Error> {
        self.handoff_callback(request)
    }
    fn writer_process(&self) -> Option<Value> { self.writer.clone() }
    fn writer_quiescent(&self, owner: u64, task: &str, identity: &Value) -> Result<bool, Error> {
        self.task(owner, task)?;
        crate::checkpoint_writer::quiescent(identity)
    }
    fn header(&self, owner: u64, task: &str) -> Result<Option<Header>, Error> {
        self.task(owner, task)?;
        let response = self.invoke("header", task, 0, None, None, self.header_bytes)?;
        match (response.version, response.value) {
            (Some(0), None) => Ok(None),
            (Some(version), Some(value)) if version > 0 && version <= i64::MAX as u64 => {
                self.value(&value, task, self.header_bytes)?;
                Ok(Some(Header { version, value }))
            }
            _ => Err(Error::Storage),
        }
    }
    fn compare_exchange(&self, owner: u64, task: &str, expected: u64,
        header: &Value, snapshot: SnapshotWrite<'_>) -> Result<u64, Error> {
        self.task(owner, task)?;
        let next = expected.checked_add(1).filter(|n| *n <= i64::MAX as u64).ok_or(Error::Invalid)?;
        self.value(header, task, self.header_bytes)?;
        let snapshot = match snapshot { SnapshotWrite::Keep => None, SnapshotWrite::Replace(value) => {
            self.value(value, task, self.snapshot_bytes)?; Some(value)
        }};
        let response = self.invoke("cas", task, expected, Some(header), snapshot, 0)?;
        if response.version != Some(next) || response.value.is_some() { return Err(Error::Storage); }
        Ok(next)
    }
    fn snapshot(&self, owner: u64, task: &str, version: u64) -> Result<Option<Value>, Error> {
        self.task(owner, task)?;
        let response = self.invoke("snapshot", task, version, None, None, self.snapshot_bytes)?;
        if response.version != Some(version) { return Err(Error::Conflict); }
        if let Some(value) = &response.value { self.value(value, task, self.snapshot_bytes)?; }
        Ok(response.value)
    }
}
impl RuntimePort {
    pub(super) fn new(tenant: u64, owner: u64, document_bytes: usize, identity_bytes: usize,
        callback: Callback, context: *mut c_void) -> Self {
        Self { host: Arc::new(Host { callback, context }), tenant, owner, document_bytes,
            identity_bytes, writer: crate::checkpoint_writer::capture() }
    }
    fn store(&self, owner: u64, header_bytes: usize, snapshot_bytes: usize, identity_bytes: usize) -> Result<Arc<CallbackStore>, Error> {
        self.checkpoint_owner_open(owner)?;
        if header_bytes == 0 || snapshot_bytes == 0 || identity_bytes == 0 || header_bytes > self.document_bytes
            || snapshot_bytes > self.document_bytes || identity_bytes > self.identity_bytes { return Err(Error::Invalid); }
        Ok(Arc::new(CallbackStore { host: self.host.clone(), tenant: self.tenant, owner,
            header_bytes, snapshot_bytes, identity_bytes, writer: self.writer.clone() }))
    }
}
impl CheckpointControlRuntime for RuntimePort {
    fn checkpoint_owner_open(&self, owner: u64) -> Result<(), Error> { if owner == self.owner { Ok(()) } else { Err(Error::Invalid) } }
    fn checkpoint_lease(&self, scope: Scope, header: usize, snapshot: usize, identity: usize, token: &str) -> Result<OwnedLease, Error> {
        let store = self.store(scope.owner, header, snapshot, identity)?;
        OwnedLease::acquire(scope, store, snapshot, identity, token)
    }
    fn checkpoint_recover(&self, scope: Scope, header: usize, snapshot: usize, identity: usize, token: &str) -> Result<OwnedLease, Error> {
        let store = self.store(scope.owner, header, snapshot, identity)?;
        OwnedLease::recover(scope, store, snapshot, identity, token)
    }
    fn checkpoint_frontier(&self, owner: u64, task: &str, header: usize, snapshot: usize, identity: usize) -> Result<Option<Value>, Error> {
        let store = self.store(owner, header, snapshot, identity)?;
        journal::inspect_abandoned_frontier(store.as_ref(), owner, task, snapshot, identity)
    }
    fn checkpoint_invoice(&self, owner: u64, task: &str, header: usize) -> Result<Option<Value>, Error> {
        let store = self.store(owner, header, 1, self.identity_bytes)?;
        store.header(owner, task)?.map(|header| journal::wallet_invoice_view(owner, task, &header.value)).transpose()
    }
    fn checkpoint_handoff(&self, scope: Scope, header: usize, snapshot: usize, identity: usize, token: &str, lineage: &ResumeLineage) -> Result<OwnedLease, Error> {
        let store = self.store(scope.owner, header, snapshot, identity)?;
        OwnedLease::handoff(scope, store, snapshot, identity, token, lineage)
    }
    fn checkpoint_goal_claim(&self,owner:u64,header:usize,snapshot:usize,payload:&Value,evidence:usize)->Result<Value,Error>{
        if evidence==0 || evidence>self.document_bytes{return Err(Error::Invalid);}
        self.store(owner,header,snapshot,self.identity_bytes)?.goal_callback("goal_claim",payload,&[],evidence)
    }
    fn checkpoint_goal_handoff(&self,owner:u64,header:usize,snapshot:usize,identity:usize,actors:Vec<ActorHandoffSpec>,authorization:&Value,evidence:usize)->Result<Vec<OwnedLease>,Error>{
        if evidence==0 || evidence>self.document_bytes{return Err(Error::Invalid);}
        OwnedLease::handoff_retired_actors(self.store(owner,header,snapshot,identity)?,actors,authorization,snapshot,identity,evidence)
    }
}
