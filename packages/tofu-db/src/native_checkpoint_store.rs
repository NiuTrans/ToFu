//! Namespaces reserved for the native journal Store; transitions belong to runtime.
pub(crate) const HEADERS: &str = "native_checkpoint_headers_v1";
pub(crate) const REPLAY: &str = "native_checkpoint_replay_v1";
pub(crate) const SNAPSHOTS: &str = "native_checkpoint_snapshots_v1";
#[cfg(feature = "native-runtime-checkpoints")]
#[path = "native_checkpoint_store_adapter.rs"]
mod adapter;
#[cfg(feature = "native-runtime-checkpoints")]
pub use adapter::NativeCheckpointStore;

#[cfg(test)]
mod tests {
    #[test]
    fn generic_record_access_cannot_bypass_native_journal_authority() {
        use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
        use crate::semantic::{admit_request, AuthenticatedScope};
        let directory = tempfile::tempdir().unwrap();
        let mut database =
            crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap();
        for namespace in [super::HEADERS, super::SNAPSHOTS, super::REPLAY] {
            for operation in ["record.get", "record.put", "record.delete", "record.list"] {
                let command = if matches!(operation, "record.put" | "record.delete") {
                    Some(format!("deny-{operation}-{namespace}"))
                } else {
                    None
                };
                let request = Request {
                    correlation_id: [3; 16],
                    deadline_unix_ms: 10000000000,
                    owner_id: 7,
                    tenant_id: Some(3),
                    command_id: command,
                    schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
                    operation: operation.into(),
                    payload: serde_json::to_vec(
                        &serde_json::json!({"namespace":namespace,"key":"task","value":{}}),
                    )
                    .unwrap(),
                };
                let protocol = NegotiatedProtocol {
                    protocol_version: PROTOCOL_VERSION,
                    schema_id: request.schema_id,
                };
                let admitted = admit_request(
                    &request,
                    protocol,
                    AuthenticatedScope {
                        owner_id: 7,
                        tenant_id: Some(3),
                    },
                    1000,
                )
                .unwrap();
                let error = crate::semantic_executor::execute_admitted_request(
                    &mut database,
                    admitted,
                    1000,
                )
                .unwrap_err();
                assert_eq!(error.code(), "database_protocol_error");
            }
        }
    }
}

#[cfg(feature = "native-runtime-checkpoints")]
#[path = "native_checkpoint_publisher.rs"]
mod publisher;
#[cfg(feature = "native-runtime-checkpoints")]
pub use publisher::NativeCheckpointSink;

#[cfg(all(test, feature = "native-runtime-checkpoints"))]
#[path = "native_checkpoint_test_support.rs"]
pub(crate) mod test_support;
