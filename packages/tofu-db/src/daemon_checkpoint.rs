//! Explicit authenticated checkpoint control on the selected daemon authority.
//! Reuses the embedded capability registry and shared checkpoint runtime; no
//! semantic catalog operation, second database, retry loop or resident worker.
//! Listener shutdown refuses new callbacks and joins every connection worker.
use crate::{protocol::{Request, Response}, semantic::AuthenticatedScope};
use serde::Deserialize;
use serde_json::{json, value::RawValue};
use std::{io, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}};

#[path = "generated_checkpoint_transport.rs"]
mod contract;
pub(crate) use contract::{CAPABILITIES, CONTROL};
use contract::FORMAT;

pub(crate) struct DaemonCheckpointControl {
    #[cfg(feature = "native-runtime-checkpoints")]
    runtime: Arc<Runtime>,
    #[cfg(feature = "native-runtime-checkpoints")]
    registry: crate::embed_ffi::checkpoint::Registry,
    runtime_id: String,
    scope: AuthenticatedScope,
}

#[cfg(feature = "native-runtime-checkpoints")]
struct Runtime {
    database: Arc<Mutex<crate::authority::AuthorityDatabase>>,
    scope: AuthenticatedScope,
    stopping: Arc<AtomicBool>,
    calls: std::sync::RwLock<()>,
}

#[cfg(feature = "native-runtime-checkpoints")]
impl crate::checkpoint_runtime::CheckpointRuntime for Runtime {
    fn checkpoint_guard(&self) -> Result<std::sync::RwLockReadGuard<'_, ()>, tofu_runtime::checkpoint_journal::Error> {
        use tofu_runtime::checkpoint_journal::Error;
        let guard = self.calls.read().map_err(|_| Error::Storage)?;
        if self.stopping.load(Ordering::Acquire) { return Err(Error::Storage); }
        Ok(guard)
    }
    fn checkpoint_database(&self) -> &Arc<Mutex<crate::authority::AuthorityDatabase>> { &self.database }
    fn checkpoint_scope(&self) -> AuthenticatedScope { self.scope }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ControlRequest<'a> {
    format: &'a str,
    runtime_id: &'a str,
    operation: &'a str,
    #[serde(borrow)]
    payload: &'a RawValue,
    response_bytes: usize,
}

impl DaemonCheckpointControl {
    pub(crate) fn new(database: Arc<Mutex<crate::authority::AuthorityDatabase>>,
        scope: AuthenticatedScope, stopping: Arc<AtomicBool>) -> Arc<Self> {
        #[cfg(not(feature = "native-runtime-checkpoints"))]
        let _ = (database, scope, stopping);
        Arc::new(Self {
            #[cfg(feature = "native-runtime-checkpoints")]
            runtime: Arc::new(Runtime {database, scope, stopping, calls: std::sync::RwLock::new(())}),
            #[cfg(feature = "native-runtime-checkpoints")]
            registry: Default::default(),
            runtime_id: uuid::Uuid::new_v4().to_string(),
            scope,
        })
    }

    fn available(&self) -> bool {
        #[cfg(feature = "native-runtime-checkpoints")]
        {
            use crate::checkpoint_runtime::CheckpointRuntime;
            self.runtime.checkpoint_guard().is_ok() &&
                crate::native_checkpoint_store::NativeCheckpointStore::new(
                    self.runtime.database.clone(), self.runtime.scope.tenant_id.unwrap_or(0),
                    self.runtime.scope.owner_id, 1, 1).is_ok()
        }
        #[cfg(not(feature = "native-runtime-checkpoints"))]
        { false }
    }

    pub(crate) fn respond(&self, request: &Request, schema_id: u32, now: u64) -> Response {
        let result = self.execute(request, schema_id, now);
        let (status, payload, error_code, error_message) = match result {
            Ok(payload) => (0, payload, None, None),
            Err(error) => (1, Vec::new(), Some(if error.kind() == io::ErrorKind::TimedOut {
                "deadline_elapsed" } else { "checkpoint_control_refused" }.into()), Some(error.to_string())),
        };
        Response {correlation_id: request.correlation_id, schema_id, payload, status,
            error_code, error_message, retryable: false}
    }

    fn execute(&self, request: &Request, schema_id: u32, now: u64) -> io::Result<Vec<u8>> {
        if request.owner_id != self.scope.owner_id || request.tenant_id != self.scope.tenant_id {
            return Err(invalid("checkpoint authority scope differs"));
        }
        if request.schema_id != schema_id || request.command_id.is_some() {
            return Err(invalid("checkpoint control requires the negotiated schema and no command receipt"));
        }
        if request.deadline_unix_ms <= now {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "checkpoint deadline elapsed before effects"));
        }
        if request.operation == CAPABILITIES {
            let payload: serde_json::Value = serde_json::from_slice(&request.payload).map_err(io::Error::other)?;
            if payload != json!({}) { return Err(invalid("checkpoint capability query must be empty")); }
            return serde_json::to_vec(&json!({"format":FORMAT, "available":self.available(),
                "runtime_id":self.runtime_id, "maximum_response_bytes":crate::server::MAX_STREAMED_RESPONSE_BYTES}))
                .map_err(io::Error::other);
        }
        let query: ControlRequest<'_> = serde_json::from_slice(&request.payload).map_err(io::Error::other)?;
        if request.operation != CONTROL || query.format != FORMAT || query.runtime_id != self.runtime_id
            || query.response_bytes == 0 {
            return Err(invalid("invalid or stale checkpoint transport binding"));
        }
        #[cfg(feature = "native-runtime-checkpoints")]
        {
            let payload = query.payload.get().as_bytes();
            let required = self.registry.response_bound(query.operation, payload)?;
            if required > query.response_bytes || required as u64 > crate::server::MAX_STREAMED_RESPONSE_BYTES {
                return Err(invalid("checkpoint response capacity is insufficient before effects"));
            }
            let response = self.registry.execute(self.runtime.clone(), query.operation, payload, request.deadline_unix_ms)?;
            if response.len() > required { return Err(io::Error::other("checkpoint response exceeded its admitted bound")); }
            Ok(response)
        }
        #[cfg(not(feature = "native-runtime-checkpoints"))]
        { Err(invalid("native checkpoints are not enabled")) }
    }
}

fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message) }

#[cfg(test)]
#[path = "daemon_checkpoint_tests.rs"]
mod tests;
