//! Owner-scoped segmented dispatch persistence using existing document IR.
//! Head CAS and child mutations share one authority transaction/receipt. No
//! dispatch authorization, executors, caches, queues or physical storage here.

use super::*;
use crate::generated_project_dispatch_storage as contract;
use std::collections::BTreeSet;

fn identity(value: Option<&Value>, empty: bool) -> Result<&str, SemanticExecutionError> {
    match value {
        Some(Value::String(text))
            if (empty || !text.is_empty()) && text.len() <= contract::IDENTITY_CHARACTERS
                && text.bytes().all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b)) => Ok(text),
        _ => Err(SemanticExecutionError::InvalidPayload),
    }
}

fn collection(payload: &Map<String, Value>) -> Result<&str, SemanticExecutionError> {
    let value = required_text(payload, "collection", 32)?;
    if !contract::COLLECTIONS.contains(&value) {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    Ok(value)
}

fn revision(payload: &Map<String, Value>, field: &str) -> Result<u64, SemanticExecutionError> {
    payload.get(field).and_then(Value::as_u64)
        .filter(|value| *value <= contract::MAXIMUM_REVISION as u64)
        .ok_or(SemanticExecutionError::InvalidPayload)
}

fn document(value: Option<&Value>, maximum: usize) -> Result<Vec<u8>, SemanticExecutionError> {
    let value = value.filter(|value| value.is_object()).ok_or(SemanticExecutionError::InvalidPayload)?;
    let encoded = canonical_json(value)?;
    if encoded.len() > maximum {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    Ok(encoded)
}

fn list_step(
    payload: &Map<String, Value>, tenant: u64, owner: u64,
    namespace: &str, prefix: &str, include_value: bool,
) -> Result<TransactionIrStep, SemanticExecutionError> {
    let limit = match payload.get("limit") {
        None => contract::PAGE_ROWS,
        Some(value) => value.as_u64().and_then(|value| usize::try_from(value).ok())
            .filter(|value| (1..=contract::PAGE_ROWS).contains(value))
            .ok_or(SemanticExecutionError::InvalidPayload)?,
    };
    let after = match payload.get("after_key") {
        None => "",
        Some(Value::String(value)) if value.len() <= 512
            && value.bytes().all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
            && (value.is_empty() || value.starts_with(prefix)) => value,
        _ => return Err(SemanticExecutionError::InvalidPayload),
    };
    let reverse = match payload.get("reverse") {
        None => false,
        Some(Value::Bool(value)) => *value,
        _ => return Err(SemanticExecutionError::InvalidPayload),
    };
    let (mut start, mut end) = EntityKey::prefix_range(tenant, owner, RECORD_DOCUMENT_NAMESPACE,
        &record_composite_key(namespace, prefix)?).map_err(map_storage_error)?;
    if !after.is_empty() {
        let boundary = record_key(tenant, owner, namespace, after)?.exact_range().map_err(map_storage_error)?;
        if reverse { end = boundary.0; } else { start = boundary.1; }
    }
    Ok(TransactionIrStep::VersionedDocumentList {
        start, end, namespace: namespace.to_owned(), limit,
        reverse, include_value, slot: 0,
    })
}

pub(super) fn compile(admitted: AdmittedRequest<'_>, now: u64) -> Result<TransactionIr, SemanticExecutionError> {
    let payload = payload_object(&admitted.request.payload)?;
    let owner = positive_identity(&payload, "user_id")?;
    if owner != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    if owner > i64::MAX as u64 {
        return Err(SemanticExecutionError::InvalidPayload);
    }
    let tenant = admitted.request.tenant_id.ok_or(SemanticExecutionError::AuthorityScopeMismatch)?;
    let mut steps = Vec::new();
    let mut missing = b"null".to_vec();
    if admitted.operation.name == "project.dispatch.projects" {
        steps.push(list_step(&payload, tenant, owner, contract::HEADS, &format!("{owner}:"), true)?);
        missing = b"[]".to_vec();
    } else {
        let project = required_text(&payload, "project_path", contract::PROJECT_PATH_CHARACTERS)?;
        if project.trim().is_empty() || project.contains('\0') {
            return Err(SemanticExecutionError::InvalidPayload);
        }
        let project_key = format!("{owner}:{:x}", Sha256::digest(project.as_bytes()));
        match admitted.operation.name {
            "project.dispatch.get" => {
                let id = identity(payload.get("id"), false)?;
                let (namespace, logical_key) = if payload.get("collection").and_then(Value::as_str) == Some("head") {
                    if id != "root" { return Err(SemanticExecutionError::InvalidPayload); }
                    (contract::HEADS, project_key)
                } else {
                    (contract::DOCUMENTS, format!("{project_key}:{}:{id}", collection(&payload)?))
                };
                steps.push(TransactionIrStep::VersionedDocumentGet {
                    key: record_key(tenant, owner, namespace, &logical_key)?,
                    namespace: namespace.to_owned(), logical_key, slot: 0,
                });
            }
            "project.dispatch.list" => {
                let collection = collection(&payload)?;
                let prefix = match payload.get("prefix") {
                    None => "", value => identity(value, true)?,
                };
                steps.push(list_step(&payload, tenant, owner, contract::DOCUMENTS,
                    &format!("{project_key}:{collection}:{prefix}"), collection == "indexes")?);
                missing = b"[]".to_vec();
            }
            "project.dispatch.commit" => {
                let expected = revision(&payload, "expected_revision")?;
                let mut size = document(payload.get("head"), contract::HEAD_BYTES)?.len();
                let changes = payload.get("changes").and_then(Value::as_array)
                    .filter(|rows| rows.len() <= contract::COMMIT_CHANGES)
                    .ok_or(SemanticExecutionError::InvalidPayload)?;
                let head = json!({"format": contract::FORMAT, "owner": owner,
                    "projectPath": project, "state": payload.get("head").unwrap()});
                steps.push(TransactionIrStep::VersionedDocumentPut {
                    key: record_key(tenant, owner, contract::HEADS, &project_key)?,
                    namespace: contract::HEADS.to_owned(), logical_key: project_key.clone(),
                    value_json: canonical_json(&head)?, expected_version: Some(expected),
                    updated_at_ms: now, slot: 0,
                });
                let mut seen = BTreeSet::new();
                for (index, change) in changes.iter().enumerate() {
                    let change = change.as_object().ok_or(SemanticExecutionError::InvalidPayload)?;
                    let collection = collection(change)?;
                    let id = identity(change.get("id"), false)?;
                    let logical_key = format!("{project_key}:{collection}:{id}");
                    if !seen.insert(logical_key.clone()) { return Err(SemanticExecutionError::InvalidPayload); }
                    let expected = revision(change, "expected_version")?;
                    let deleting = change.get("delete") == Some(&Value::Bool(true));
                    let value_json = if deleting {
                        if collection != "indexes" || change.contains_key("value") {
                            return Err(SemanticExecutionError::InvalidPayload);
                        }
                        // CAS an ephemeral empty object before deleting to fence
                        // absent + nonzero expected versions too. Both changes
                        // are invisible until the enclosing transaction commits.
                        b"{}".to_vec()
                    } else {
                        if change.contains_key("delete") { return Err(SemanticExecutionError::InvalidPayload); }
                        let bytes = document(change.get("value"), if collection == "indexes" {
                            contract::INDEX_BYTES
                        } else { contract::DOCUMENT_BYTES })?;
                        size += bytes.len();
                        bytes
                    };
                    if size > contract::COMMIT_BYTES { return Err(SemanticExecutionError::InvalidPayload); }
                    let key = record_key(tenant, owner, contract::DOCUMENTS, &logical_key)?;
                    steps.push(TransactionIrStep::VersionedDocumentPut {
                        key: key.clone(), namespace: contract::DOCUMENTS.to_owned(), logical_key: logical_key.clone(),
                        value_json, expected_version: Some(expected), updated_at_ms: now, slot: index + 1,
                    });
                    if deleting {
                        steps.push(TransactionIrStep::VersionedDocumentDelete {
                            key, namespace: contract::DOCUMENTS.to_owned(), logical_key,
                            expected_version: Some(expected + 1), slot: changes.len() + index + 1,
                        });
                    }
                }
            }
            _ => return Err(SemanticExecutionError::UnsupportedOperation),
        }
    }
    let effects = admitted.operation.kind.mutates_state()
        .then(|| command_effects(admitted, &payload, now)).transpose()?;
    Ok(TransactionIr { tenant_id: tenant, owner_user_id: owner,
        operation: admitted.operation.name.to_owned(), operation_kind: admitted.operation.kind,
        steps, result: TransactionIrResult::SlotOrLiteral { slot: 0, missing }, command_effects: effects })
}
