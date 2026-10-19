//! Default-deny semantic admission between storage.v2 decoding and dispatch.
//!
//! This module authenticates no caller and executes no operation. Its narrow
//! responsibility is to bind a validated request to the frozen storage.v1
//! operation catalog, enforce negotiated schema and deadline constraints, and
//! prevent state-changing work without a command ID. A later dispatcher must
//! consume `AdmittedRequest`; it must not dispatch a raw protocol request.

use std::fmt;

use crate::generated_storage_operations::{
    storage_operation, StorageOperationKind, StorageOperationMetadata, StorageOwnerScope,
    STORAGE_SCHEMA_VERSION,
};
use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AdmissionError {
    InvalidEnvelope,
    ProtocolNotNegotiated,
    SchemaNotNegotiated,
    UnsupportedSchema,
    AuthorityScopeMismatch,
    DeadlineElapsed,
    UnknownOperation,
    CommandIdRequired,
}

impl AdmissionError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::InvalidEnvelope => "invalid_request",
            Self::ProtocolNotNegotiated => "protocol_not_negotiated",
            Self::SchemaNotNegotiated => "schema_not_negotiated",
            Self::UnsupportedSchema => "unsupported_schema",
            Self::AuthorityScopeMismatch => "authority_scope_mismatch",
            Self::DeadlineElapsed => "deadline_elapsed",
            Self::UnknownOperation => "unknown_operation",
            Self::CommandIdRequired => "command_id_required",
        }
    }

    pub const fn retryable(self) -> bool {
        false
    }
}

impl fmt::Display for AdmissionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for AdmissionError {}

/// Operation metadata an admitted request was bound to: either a frozen
/// catalog entry or a manifest-declared plugin row operation.
///
/// The legacy authority resolves `plugin.<namespace>.<operation>` names
/// dynamically against registered storage manifests (the operation catalog
/// itself only ever names `plugin.register`/`plugin.manifest.get`). The
/// frozen-catalog default-deny posture is unchanged for those two names;
/// namespaced plugin operations are admitted with a shape-derived kind and
/// re-validated against the live manifest during execution, exactly like the
/// legacy `_plugin_dynamic` dispatch (an unknown namespace or operation is
/// rejected there, never admitted early).
#[derive(Clone, Copy, Debug)]
pub struct AdmittedOperation<'request> {
    pub name: &'request str,
    pub domain: &'static str,
    pub kind: StorageOperationKind,
    pub receipt_required: bool,
    pub transaction_timeout_millis: Option<u64>,
    pub owner_scope: StorageOwnerScope,
}

impl<'request> AdmittedOperation<'request> {
    fn catalog(metadata: &'static StorageOperationMetadata) -> Self {
        Self {
            name: metadata.name,
            domain: metadata.domain,
            kind: metadata.kind,
            receipt_required: metadata.receipt_required,
            transaction_timeout_millis: metadata.transaction_timeout_millis,
            owner_scope: metadata.owner_scope,
        }
    }

    fn synthetic_plugin(name: &'request str, command: bool) -> Self {
        Self {
            name,
            domain: "plugins",
            kind: if command {
                StorageOperationKind::Command
            } else {
                StorageOperationKind::Query
            },
            // The legacy contract receipts every plugin command
            // (`resolve_operation_contract` returns kind == "command").
            receipt_required: command,
            transaction_timeout_millis: None,
            owner_scope: StorageOwnerScope::HandlerEnforced,
        }
    }
}

/// True when `name` addresses a manifest-declared plugin row operation
/// (`plugin.<namespace>.<operation>`), as opposed to a frozen catalog entry.
pub(crate) fn is_plugin_row_operation(name: &str) -> bool {
    name.starts_with("plugin.") && name.len() > "plugin.".len()
}

#[derive(Clone, Copy, Debug)]
pub struct AdmittedRequest<'request> {
    pub request: &'request Request,
    pub operation: AdmittedOperation<'request>,
    pub remaining_millis: u64,
}

/// Queue-owned admission receipt. Private fields prevent mutation of the
/// validated request or operation metadata while an execution carrier is free.
/// Construction uses the same default-deny boundary as synchronous dispatch.
#[derive(Debug)]
pub(crate) struct OwnedAdmittedRequest {
    request: Request,
    domain: &'static str,
    kind: StorageOperationKind,
    receipt_required: bool,
    transaction_timeout_millis: Option<u64>,
    owner_scope: StorageOwnerScope,
    remaining_millis: u64,
}

impl OwnedAdmittedRequest {
    pub(crate) fn into_request(self) -> Request { self.request }
    pub(crate) fn as_borrowed(&self) -> AdmittedRequest<'_> {
        AdmittedRequest {
            request: &self.request,
            operation: AdmittedOperation {
                name: &self.request.operation,
                domain: self.domain,
                kind: self.kind,
                receipt_required: self.receipt_required,
                transaction_timeout_millis: self.transaction_timeout_millis,
                owner_scope: self.owner_scope,
            },
            remaining_millis: self.remaining_millis,
        }
    }
}

pub(crate) fn admit_owned_request(
    request: Request, negotiated: NegotiatedProtocol,
    authenticated_scope: AuthenticatedScope, now_unix_ms: u64,
) -> Result<OwnedAdmittedRequest, AdmissionError> {
    admit_owned_request_preserving_request(request, negotiated, authenticated_scope, now_unix_ms, false)
        .map_err(|(error, _request)| error)
}

pub(crate) fn admit_owned_request_preserving_request(
    request: Request,
    negotiated: NegotiatedProtocol,
    authenticated_scope: AuthenticatedScope,
    now_unix_ms: u64,
    streamed: bool,
) -> Result<OwnedAdmittedRequest, (AdmissionError, Request)> {
    let admission = if streamed {admit_streamed_request(&request, negotiated, authenticated_scope, now_unix_ms, usize::MAX)}
        else {admit_request(&request, negotiated, authenticated_scope, now_unix_ms)};
    let admitted = match admission {
        Ok(admitted) => admitted,
        Err(error) => return Err((error, request)),
    };
    debug_assert_eq!(admitted.operation.name, request.operation);
    Ok(OwnedAdmittedRequest {
        domain: admitted.operation.domain,
        kind: admitted.operation.kind,
        receipt_required: admitted.operation.receipt_required,
        transaction_timeout_millis: admitted.operation.transaction_timeout_millis,
        owner_scope: admitted.operation.owner_scope,
        remaining_millis: admitted.remaining_millis,
        request,
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AuthenticatedScope {
    pub owner_id: u64,
    pub tenant_id: Option<u64>,
}

pub fn admit_request<'request>(
    request: &'request Request,
    negotiated: NegotiatedProtocol,
    authenticated_scope: AuthenticatedScope,
    now_unix_ms: u64,
) -> Result<AdmittedRequest<'request>, AdmissionError> {
    request
        .validate()
        .map_err(|_| AdmissionError::InvalidEnvelope)?;
    if negotiated.protocol_version != PROTOCOL_VERSION {
        return Err(AdmissionError::ProtocolNotNegotiated);
    }
    if request.schema_id != negotiated.schema_id {
        return Err(AdmissionError::SchemaNotNegotiated);
    }
    if request.schema_id != STORAGE_SCHEMA_VERSION {
        return Err(AdmissionError::UnsupportedSchema);
    }
    if request.owner_id != authenticated_scope.owner_id
        || request.tenant_id != authenticated_scope.tenant_id
    {
        return Err(AdmissionError::AuthorityScopeMismatch);
    }
    let mut remaining_millis = request
        .deadline_unix_ms
        .checked_sub(now_unix_ms)
        .filter(|remaining| *remaining > 0)
        .ok_or(AdmissionError::DeadlineElapsed)?;
    let operation = match storage_operation(&request.operation) {
        Some(metadata) => AdmittedOperation::catalog(metadata),
        None if is_plugin_row_operation(&request.operation) => {
            // The wire carries no kind for namespaced plugin operations; a
            // command ID marks command intent, and execution re-checks the
            // manifest's declared kind (mismatch is a protocol error, the
            // same answer the legacy kind check gives).
            AdmittedOperation::synthetic_plugin(
                &request.operation,
                request.command_id.is_some(),
            )
        }
        None => return Err(AdmissionError::UnknownOperation),
    };
    if operation.kind.mutates_state() && request.command_id.is_none() {
        return Err(AdmissionError::CommandIdRequired);
    }
    if let Some(timeout_millis) = operation.transaction_timeout_millis {
        remaining_millis = remaining_millis.min(timeout_millis);
    }
    Ok(AdmittedRequest {
        request,
        operation,
        remaining_millis,
    })
}

/// Admit a bounded aggregate from an authenticated transport. Framed callers
/// prove ordered chunk completion; embedded callers prove operation eligibility
/// and reserved encoded bytes. Ordinary single-frame requests use `admit_request`.
pub(crate) fn admit_streamed_request<'request>(
    request: &'request Request,
    negotiated: NegotiatedProtocol,
    authenticated_scope: AuthenticatedScope,
    now_unix_ms: u64,
    maximum_payload_bytes: usize,
) -> Result<AdmittedRequest<'request>, AdmissionError> {
    if request.payload.is_empty() || request.payload.len() > maximum_payload_bytes {
        return Err(AdmissionError::InvalidEnvelope);
    }
    let envelope = Request {
        correlation_id: request.correlation_id,
        deadline_unix_ms: request.deadline_unix_ms,
        owner_id: request.owner_id,
        tenant_id: request.tenant_id,
        command_id: request.command_id.clone(),
        schema_id: request.schema_id,
        operation: request.operation.clone(),
        payload: Vec::new(),
    };
    envelope
        .validate()
        .map_err(|_| AdmissionError::InvalidEnvelope)?;
    if negotiated.protocol_version != PROTOCOL_VERSION {
        return Err(AdmissionError::ProtocolNotNegotiated);
    }
    if request.schema_id != negotiated.schema_id {
        return Err(AdmissionError::SchemaNotNegotiated);
    }
    if request.schema_id != STORAGE_SCHEMA_VERSION {
        return Err(AdmissionError::UnsupportedSchema);
    }
    if request.owner_id != authenticated_scope.owner_id
        || request.tenant_id != authenticated_scope.tenant_id
    {
        return Err(AdmissionError::AuthorityScopeMismatch);
    }
    let mut remaining_millis = request
        .deadline_unix_ms
        .checked_sub(now_unix_ms)
        .filter(|remaining| *remaining > 0)
        .ok_or(AdmissionError::DeadlineElapsed)?;
    let operation = match storage_operation(&request.operation) {
        Some(metadata) => AdmittedOperation::catalog(metadata),
        None if is_plugin_row_operation(&request.operation) => {
            AdmittedOperation::synthetic_plugin(
                &request.operation,
                request.command_id.is_some(),
            )
        }
        None => return Err(AdmissionError::UnknownOperation),
    };
    if operation.kind.mutates_state() && request.command_id.is_none() {
        return Err(AdmissionError::CommandIdRequired);
    }
    if let Some(timeout_millis) = operation.transaction_timeout_millis {
        remaining_millis = remaining_millis.min(timeout_millis);
    }
    Ok(AdmittedRequest {
        request,
        operation,
        remaining_millis,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generated_storage_operations::{
        StorageOperationKind, STORAGE_OPERATIONS, STORAGE_OPERATION_COUNT,
    };

    fn request(operation: &str, command_id: Option<&str>) -> Request {
        Request {
            correlation_id: [1; 16],
            deadline_unix_ms: 200_000,
            owner_id: 9,
            tenant_id: Some(7),
            command_id: command_id.map(str::to_owned),
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: operation.to_owned(),
            payload: vec![],
        }
    }

    fn negotiated() -> NegotiatedProtocol {
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        }
    }

    fn authenticated_scope() -> AuthenticatedScope {
        AuthenticatedScope {
            owner_id: 9,
            tenant_id: Some(7),
        }
    }

    #[test]
    fn generated_catalog_is_sorted_complete_and_classified() {
        assert_eq!(STORAGE_OPERATIONS.len(), STORAGE_OPERATION_COUNT);
        assert!(STORAGE_OPERATIONS
            .windows(2)
            .all(|pair| pair[0].name < pair[1].name));
        let catalog: serde_json::Value = serde_json::from_str(include_str!(
            "../../../contracts/storage_operations_v1.json"
        ))
        .unwrap();
        let operations = catalog["operations"].as_array().unwrap();
        assert_eq!(operations.len(), STORAGE_OPERATION_COUNT);
        for (actual, expected) in STORAGE_OPERATIONS.iter().zip(operations) {
            assert_eq!(actual.name, expected["name"].as_str().unwrap());
            assert_eq!(actual.domain, expected["domain"].as_str().unwrap());
            let kind = match actual.kind {
                StorageOperationKind::Query => "query",
                StorageOperationKind::Command => "command",
                StorageOperationKind::Maintenance => "maintenance",
            };
            assert_eq!(kind, expected["kind"].as_str().unwrap(), "{}", actual.name);
            assert_eq!(
                actual.receipt_required,
                expected["receiptRequired"].as_bool().unwrap(),
                "{}",
                actual.name
            );
        }
    }

    #[test]
    fn every_mutation_requires_command_id_independently_of_receipt_policy() {
        for operation in STORAGE_OPERATIONS {
            let request = request(operation.name, None);
            let admitted = admit_request(&request, negotiated(), authenticated_scope(), 100_000);
            if operation.kind.mutates_state() {
                assert_eq!(admitted.unwrap_err(), AdmissionError::CommandIdRequired);
            } else {
                assert_eq!(admitted.unwrap().operation.name, operation.name);
            }
        }
        let no_receipt_command = request("event.append", Some("command-1"));
        let admitted = admit_request(
            &no_receipt_command,
            negotiated(),
            authenticated_scope(),
            100_000,
        )
        .unwrap();
        assert!(!admitted.operation.receipt_required);
    }

    #[test]
    fn admission_rejects_unnegotiated_schema_unknown_operation_and_elapsed_deadline() {
        let known = request("artifact.get", None);
        assert_eq!(
            admit_request(
                &known,
                NegotiatedProtocol {
                    protocol_version: 1,
                    schema_id: STORAGE_SCHEMA_VERSION,
                },
                authenticated_scope(),
                100_000,
            )
            .unwrap_err(),
            AdmissionError::ProtocolNotNegotiated
        );
        let mut wrong_schema = known.clone();
        wrong_schema.schema_id += 1;
        assert_eq!(
            admit_request(&wrong_schema, negotiated(), authenticated_scope(), 100_000,)
                .unwrap_err(),
            AdmissionError::SchemaNotNegotiated
        );
        let unknown = request("not.registered", None);
        assert_eq!(
            admit_request(&unknown, negotiated(), authenticated_scope(), 100_000).unwrap_err(),
            AdmissionError::UnknownOperation
        );
        assert_eq!(
            admit_request(
                &known,
                negotiated(),
                authenticated_scope(),
                known.deadline_unix_ms,
            )
            .unwrap_err(),
            AdmissionError::DeadlineElapsed
        );
    }

    #[test]
    fn request_identity_must_match_authenticated_scope() {
        let known = request("artifact.get", None);
        assert_eq!(
            admit_request(
                &known,
                negotiated(),
                AuthenticatedScope {
                    owner_id: 10,
                    tenant_id: Some(7),
                },
                100_000,
            )
            .unwrap_err(),
            AdmissionError::AuthorityScopeMismatch
        );
        assert_eq!(
            admit_request(
                &known,
                negotiated(),
                AuthenticatedScope {
                    owner_id: 9,
                    tenant_id: None,
                },
                100_000,
            )
            .unwrap_err(),
            AdmissionError::AuthorityScopeMismatch
        );
    }

    #[test]
    fn operation_timeout_caps_but_never_extends_the_wire_deadline() {
        let long = request("project.relink", Some("command-1"));
        assert_eq!(
            admit_request(&long, negotiated(), authenticated_scope(), 1_000)
                .unwrap()
                .remaining_millis,
            120_000
        );
        let short = request("project.relink", Some("command-1"));
        assert_eq!(
            admit_request(&short, negotiated(), authenticated_scope(), 150_000)
                .unwrap()
                .remaining_millis,
            50_000
        );
    }
    #[test]
    fn owned_admission_moves_payload_across_threads_without_copy_or_metadata_drift() {
        for operation in ["artifact.get", "event.append", "plugin.example.put"] {
            let request = request(operation, Some("owned-command"));
            let pointer = request.payload.as_ptr() as usize;
            let expected = admit_request(&request, negotiated(), authenticated_scope(), 100_000).unwrap();
            let expected_metadata = (expected.operation.domain, expected.operation.kind,
                expected.operation.receipt_required, expected.operation.transaction_timeout_millis,
                expected.operation.owner_scope, expected.remaining_millis);
            let owned = admit_owned_request(request, negotiated(), authenticated_scope(), 100_000).unwrap();
            let (actual_pointer, name, actual_metadata) = std::thread::spawn(move || {
                let borrowed = owned.as_borrowed();
                (borrowed.request.payload.as_ptr() as usize, borrowed.operation.name.to_owned(),
                    (borrowed.operation.domain, borrowed.operation.kind,
                     borrowed.operation.receipt_required, borrowed.operation.transaction_timeout_millis,
                     borrowed.operation.owner_scope, borrowed.remaining_millis))
            }).join().unwrap();
            assert_eq!(actual_pointer, pointer);
            assert_eq!(name, operation);
            assert_eq!(actual_metadata, expected_metadata);
        }
    }

    #[test]
    fn owned_admission_preserves_scope_schema_deadline_and_unknown_denials() {
        let mut wrong_owner = request("artifact.get", None);
        wrong_owner.owner_id += 1;
        assert!(matches!(admit_owned_request(wrong_owner, negotiated(), authenticated_scope(), 100_000), Err(AdmissionError::AuthorityScopeMismatch)));
        let mut wrong_schema = request("artifact.get", None);
        wrong_schema.schema_id += 1;
        assert!(matches!(admit_owned_request(wrong_schema, negotiated(), authenticated_scope(), 100_000), Err(AdmissionError::SchemaNotNegotiated)));
        assert!(matches!(admit_owned_request(request("unknown.operation", None), negotiated(), authenticated_scope(), 100_000), Err(AdmissionError::UnknownOperation)));
        assert!(matches!(admit_owned_request(request("artifact.get", None), negotiated(), authenticated_scope(), u64::MAX), Err(AdmissionError::DeadlineElapsed)));
        assert!(matches!(admit_owned_request(request("event.append", None), negotiated(), authenticated_scope(), 100_000), Err(AdmissionError::CommandIdRequired)));
    }

}
