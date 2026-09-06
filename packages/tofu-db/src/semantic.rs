//! Default-deny semantic admission between storage.v2 decoding and dispatch.
//!
//! This module authenticates no caller and executes no operation. Its narrow
//! responsibility is to bind a validated request to the frozen storage.v1
//! operation catalog, enforce negotiated schema and deadline constraints, and
//! prevent state-changing work without a command ID. A later dispatcher must
//! consume `AdmittedRequest`; it must not dispatch a raw protocol request.

use std::fmt;

use crate::generated_storage_operations::{
    storage_operation, StorageOperationMetadata, STORAGE_SCHEMA_VERSION,
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

#[derive(Clone, Copy, Debug)]
pub struct AdmittedRequest<'request> {
    pub request: &'request Request,
    pub operation: &'static StorageOperationMetadata,
    pub remaining_millis: u64,
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
    let operation =
        storage_operation(&request.operation).ok_or(AdmissionError::UnknownOperation)?;
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

/// Admit a payload reassembled by the authenticated connection's bounded
/// blob-chunk state machine. Callers cannot use this for decoded single-frame
/// requests, whose stricter protocol bound remains enforced by `admit_request`.
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
    let operation =
        storage_operation(&request.operation).ok_or(AdmissionError::UnknownOperation)?;
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
        let counts = STORAGE_OPERATIONS
            .iter()
            .fold([0_usize; 3], |mut counts, operation| {
                counts[match operation.kind {
                    StorageOperationKind::Query => 0,
                    StorageOperationKind::Command => 1,
                    StorageOperationKind::Maintenance => 2,
                }] += 1;
                counts
            });
        assert_eq!(counts, [139, 188, 4]);
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
}
