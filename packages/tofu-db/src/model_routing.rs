//! Owner-scoped model-routing authority, migration receipts, and sealed secrets.
//!
//! Aggregate parts are independent blob-capable documents so a migration can
//! atomically preserve the prior authority without constructing one oversized
//! physical row. Secret listing and pruning use bounded covering indexes; this
//! module accepts only ciphertext produced above the storage boundary.

use std::collections::BTreeSet;
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_MODEL_ROUTING_DOCUMENT_BYTES, MAX_MODEL_ROUTING_PRUNED_PER_COMMAND,
    MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY, MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE,
    MODEL_ROUTING_AUTHORITY_NAMESPACE, MODEL_ROUTING_BACKUP_NAMESPACE,
    MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE, MODEL_ROUTING_SECRET_COUNT_NAMESPACE,
    MODEL_ROUTING_SECRET_NAMESPACE, MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
    MODEL_ROUTING_SECRET_UPDATED_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn boundary_digest(tenant_label: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:model-routing-boundary:v1\0");
    hasher.update(tenant_label.len().to_be_bytes());
    hasher.update(tenant_label.as_bytes());
    hasher.finalize().into()
}

fn boundary_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    tenant_label: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &boundary_digest(tenant_label),
    )
}

fn secret_raw_key(tenant_label: &str, secret_reference: &str) -> Vec<u8> {
    let mut raw = Vec::with_capacity(32 + secret_reference.len());
    raw.extend_from_slice(&boundary_digest(tenant_label));
    raw.extend_from_slice(secret_reference.as_bytes());
    raw
}

fn secret_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    tenant_label: &str,
    secret_reference: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &secret_raw_key(tenant_label, secret_reference),
    )
}

fn secret_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    tenant_label: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &boundary_digest(tenant_label),
    )
}

fn updated_index_key(
    transaction: &AuthorityTransaction,
    tenant_label: &str,
    updated_at: f64,
    secret_reference: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(40 + secret_reference.len());
    raw.extend_from_slice(&boundary_digest(tenant_label));
    raw.extend_from_slice(&updated_at.to_bits().to_be_bytes());
    raw.extend_from_slice(secret_reference.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        MODEL_ROUTING_SECRET_UPDATED_INDEX_NAMESPACE,
        &raw,
    )
}

fn physical_timestamp_milliseconds(updated_at: f64) -> u64 {
    ((updated_at * 1_000.0) as u64).max(1)
}

fn logical_boundary_key(tenant_label: &str) -> String {
    format!("boundary:{tenant_label}")
}

fn logical_secret_key(tenant_label: &str, secret_reference: &str) -> String {
    format!("boundary:{tenant_label}:secret:{secret_reference}")
}

fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    tenant_label: &str,
    value: &Value,
    updated_at: f64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(value)
        .map_err(|_| invalid_input("model-routing document cannot be encoded"))?;
    if value_json.len() > MAX_MODEL_ROUTING_DOCUMENT_BYTES {
        return Err(invalid_input("model-routing document exceeds 8 MiB"));
    }
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: boundary_key(transaction, namespace, tenant_label)?,
            namespace: namespace.to_owned(),
            logical_key: logical_boundary_key(tenant_label),
            value_json,
            expected_version: None,
            updated_at_ms: physical_timestamp_milliseconds(updated_at),
        },
    )?;
    Ok(())
}

fn get_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    tenant_label: &str,
) -> io::Result<Option<Value>> {
    versioned_document::get_value(
        database,
        transaction,
        &boundary_key(transaction, namespace, tenant_label)?,
        namespace,
        &logical_boundary_key(tenant_label),
    )?
    .map(|raw| {
        serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("stored model-routing document is malformed"))
    })
    .transpose()
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct AuthorityMetadata {
    owner_user_id: u64,
    tenant_id: String,
    revision: u64,
    updated_at: f64,
}

fn read_authority_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<Option<AuthorityMetadata>> {
    let Some(raw) = database.entity_get(
        transaction,
        &boundary_key(
            transaction,
            MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE,
            tenant_label,
        )?,
    )?
    else {
        return Ok(None);
    };
    let metadata: AuthorityMetadata = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("stored model-routing authority metadata is malformed"))?;
    if metadata.owner_user_id != transaction.owner_user_id()
        || metadata.tenant_id != tenant_label
        || !metadata.updated_at.is_finite()
    {
        return Err(invalid_data(
            "stored model-routing authority metadata identity is invalid",
        ));
    }
    Ok(Some(metadata))
}

fn write_authority_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    metadata: &AuthorityMetadata,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        boundary_key(
            transaction,
            MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE,
            &metadata.tenant_id,
        )?,
        serde_json::to_vec(metadata)
            .map_err(|_| invalid_data("model-routing authority metadata cannot be encoded"))?,
    )
}

fn read_authority(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<Option<(AuthorityMetadata, Value)>> {
    let metadata = read_authority_metadata(database, transaction, tenant_label)?;
    let document = get_document(
        database,
        transaction,
        MODEL_ROUTING_AUTHORITY_NAMESPACE,
        tenant_label,
    )?;
    match (metadata, document) {
        (None, None) => Ok(None),
        (Some(metadata), Some(document)) if document.is_object() => Ok(Some((metadata, document))),
        _ => Err(invalid_data(
            "model-routing authority metadata and document differ",
        )),
    }
}

pub(crate) struct CommitRequest {
    pub tenant_label: String,
    pub expected_revision: u64,
    pub document: Value,
    pub migration_receipt: Option<Value>,
    pub updated_at: f64,
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some((metadata, document)) = read_authority(database, transaction, tenant_label)? else {
        return Ok(None);
    };
    serde_json::to_vec(&json!({
        "owner_user_id": metadata.owner_user_id,
        "tenant_id": metadata.tenant_id,
        "revision": metadata.revision,
        "document": document,
        "updated_at": metadata.updated_at,
    }))
    .map(Some)
    .map_err(|_| invalid_data("model-routing authority response cannot be encoded"))
}

pub(crate) fn commit(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: CommitRequest,
) -> io::Result<Vec<u8>> {
    let current = read_authority(database, transaction, &request.tenant_label)?;
    let current_revision = current
        .as_ref()
        .map_or(0, |(metadata, _)| metadata.revision);
    if current_revision != request.expected_revision {
        return Err(conflict("model-routing revision changed"));
    }
    let next_revision = request
        .expected_revision
        .checked_add(1)
        .ok_or_else(|| invalid_input("model-routing revision overflow"))?;
    if request
        .document
        .get("contract_version")
        .and_then(Value::as_str)
        != Some("tofu.model-routing/v2")
        || request.document.get("revision").and_then(Value::as_u64) != Some(next_revision)
    {
        return Err(invalid_input("model-routing document revision is invalid"));
    }
    if let Some(receipt) = &request.migration_receipt {
        if let Some((_, current_document)) = &current {
            put_document(
                database,
                transaction,
                MODEL_ROUTING_BACKUP_NAMESPACE,
                &request.tenant_label,
                current_document,
                request.updated_at,
            )?;
        }
        put_document(
            database,
            transaction,
            MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE,
            &request.tenant_label,
            receipt,
            request.updated_at,
        )?;
    }
    put_document(
        database,
        transaction,
        MODEL_ROUTING_AUTHORITY_NAMESPACE,
        &request.tenant_label,
        &request.document,
        request.updated_at,
    )?;
    write_authority_metadata(
        database,
        transaction,
        &AuthorityMetadata {
            owner_user_id: transaction.owner_user_id(),
            tenant_id: request.tenant_label.clone(),
            revision: next_revision,
            updated_at: request.updated_at,
        },
    )?;
    serde_json::to_vec(&json!({
        "owner_user_id": transaction.owner_user_id(),
        "tenant_id": request.tenant_label,
        "revision": next_revision,
        "updated_at": request.updated_at,
    }))
    .map_err(|_| invalid_data("model-routing acknowledgement cannot be encoded"))
}

pub(crate) fn migration_receipt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<Option<Vec<u8>>> {
    let Some(receipt) = get_document(
        database,
        transaction,
        MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE,
        tenant_label,
    )?
    else {
        return Ok(None);
    };
    let (metadata, _) = read_authority(database, transaction, tenant_label)?
        .ok_or_else(|| invalid_data("migration receipt authority is missing"))?;
    let backup = get_document(
        database,
        transaction,
        MODEL_ROUTING_BACKUP_NAMESPACE,
        tenant_label,
    )?;
    serde_json::to_vec(&json!({
        "revision": metadata.revision,
        "backup": backup,
        "receipt": receipt,
        "updated_at": metadata.updated_at,
    }))
    .map(Some)
    .map_err(|_| invalid_data("migration receipt response cannot be encoded"))
}

pub(crate) fn put_migration_receipt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    receipt: Value,
    initial_document: Option<Value>,
    updated_at: f64,
) -> io::Result<Vec<u8>> {
    let current = read_authority(database, transaction, tenant_label)?;
    let revision = if let Some((metadata, _)) = current {
        metadata.revision
    } else {
        let initial_document = initial_document
            .ok_or_else(|| invalid_input("initial model-routing receipt document is required"))?;
        if initial_document
            .get("contract_version")
            .and_then(Value::as_str)
            != Some("tofu.model-routing/v2")
            || initial_document.get("revision").and_then(Value::as_u64) != Some(0)
        {
            return Err(invalid_input(
                "initial model-routing receipt document must be revision zero",
            ));
        }
        put_document(
            database,
            transaction,
            MODEL_ROUTING_AUTHORITY_NAMESPACE,
            tenant_label,
            &initial_document,
            updated_at,
        )?;
        0
    };
    put_document(
        database,
        transaction,
        MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE,
        tenant_label,
        &receipt,
        updated_at,
    )?;
    write_authority_metadata(
        database,
        transaction,
        &AuthorityMetadata {
            owner_user_id: transaction.owner_user_id(),
            tenant_id: tenant_label.to_owned(),
            revision,
            updated_at,
        },
    )?;
    serde_json::to_vec(&json!({
        "owner_user_id": transaction.owner_user_id(),
        "tenant_id": tenant_label,
        "revision": revision,
        "status": receipt.get("status").and_then(Value::as_str).unwrap_or(""),
        "updated_at": updated_at,
    }))
    .map_err(|_| invalid_data("migration receipt acknowledgement cannot be encoded"))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct SecretDocument {
    owner_user_id: u64,
    tenant_id: String,
    secret_reference: String,
    ciphertext: String,
    key_hint: String,
    created_at: f64,
    updated_at: f64,
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<usize> {
    let Some(raw) = database.entity_get(
        transaction,
        &boundary_key(
            transaction,
            MODEL_ROUTING_SECRET_COUNT_NAMESPACE,
            tenant_label,
        )?,
    )?
    else {
        return Ok(0);
    };
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("model-routing secret count is malformed"))?;
    if value["tenant_id"] != tenant_label {
        return Err(invalid_data("model-routing secret count identity differs"));
    }
    value["count"]
        .as_u64()
        .and_then(|count| usize::try_from(count).ok())
        .filter(|count| *count <= MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY)
        .ok_or_else(|| invalid_data("model-routing secret count exceeds its bound"))
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    count: usize,
) -> io::Result<()> {
    if count > MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY {
        return Err(conflict("model-routing secret quota reached"));
    }
    database.entity_put(
        transaction,
        boundary_key(
            transaction,
            MODEL_ROUTING_SECRET_COUNT_NAMESPACE,
            tenant_label,
        )?,
        serde_json::to_vec(&json!({"tenant_id": tenant_label, "count": count}))
            .map_err(|_| invalid_data("model-routing secret count cannot be encoded"))?,
    )
}

fn read_secret(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    secret_reference: &str,
) -> io::Result<Option<SecretDocument>> {
    let key = secret_key(
        transaction,
        MODEL_ROUTING_SECRET_NAMESPACE,
        tenant_label,
        secret_reference,
    )?;
    let Some(raw) = versioned_document::get_value(
        database,
        transaction,
        &key,
        MODEL_ROUTING_SECRET_NAMESPACE,
        &logical_secret_key(tenant_label, secret_reference),
    )?
    else {
        return Ok(None);
    };
    let document: SecretDocument = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("stored model-routing secret is malformed"))?;
    if document.owner_user_id != transaction.owner_user_id()
        || document.tenant_id != tenant_label
        || document.secret_reference != secret_reference
        || !document.created_at.is_finite()
        || !document.updated_at.is_finite()
    {
        return Err(invalid_data(
            "stored model-routing secret identity is invalid",
        ));
    }
    Ok(Some(document))
}

fn put_secret_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &SecretDocument,
) -> io::Result<()> {
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: secret_key(
                transaction,
                MODEL_ROUTING_SECRET_NAMESPACE,
                &document.tenant_id,
                &document.secret_reference,
            )?,
            namespace: MODEL_ROUTING_SECRET_NAMESPACE.to_owned(),
            logical_key: logical_secret_key(&document.tenant_id, &document.secret_reference),
            value_json: serde_json::to_vec(document)
                .map_err(|_| invalid_data("model-routing secret cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: physical_timestamp_milliseconds(document.updated_at),
        },
    )?;
    Ok(())
}

fn write_secret_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &SecretDocument,
) -> io::Result<()> {
    let identity = serde_json::to_vec(&json!({
        "tenant_id": document.tenant_id,
        "secret_reference": document.secret_reference,
    }))
    .map_err(|_| invalid_data("model-routing secret index cannot be encoded"))?;
    database.entity_put(
        transaction,
        secret_key(
            transaction,
            MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
            &document.tenant_id,
            &document.secret_reference,
        )?,
        identity.clone(),
    )?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &document.tenant_id,
            document.updated_at,
            &document.secret_reference,
        )?,
        identity,
    )
}

fn remove_secret(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &SecretDocument,
) -> io::Result<()> {
    database.entity_delete(
        transaction,
        secret_key(
            transaction,
            MODEL_ROUTING_SECRET_NAMESPACE,
            &document.tenant_id,
            &document.secret_reference,
        )?,
    )?;
    database.entity_delete(
        transaction,
        secret_key(
            transaction,
            MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
            &document.tenant_id,
            &document.secret_reference,
        )?,
    )?;
    database.entity_delete(
        transaction,
        updated_index_key(
            transaction,
            &document.tenant_id,
            document.updated_at,
            &document.secret_reference,
        )?,
    )
}

pub(crate) fn secret_put(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    secret_reference: &str,
    ciphertext: &str,
    key_hint: &str,
    updated_at: f64,
) -> io::Result<Vec<u8>> {
    let current = read_secret(database, transaction, tenant_label, secret_reference)?;
    let created_at = current.as_ref().map_or(updated_at, |row| row.created_at);
    if let Some(row) = &current {
        database.entity_delete(
            transaction,
            updated_index_key(transaction, tenant_label, row.updated_at, secret_reference)?,
        )?;
    } else {
        let count = read_count(database, transaction, tenant_label)?;
        if count >= MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY {
            return Err(conflict("model-routing secret quota reached (1024)"));
        }
        write_count(database, transaction, tenant_label, count + 1)?;
    }
    let document = SecretDocument {
        owner_user_id: transaction.owner_user_id(),
        tenant_id: tenant_label.to_owned(),
        secret_reference: secret_reference.to_owned(),
        ciphertext: ciphertext.to_owned(),
        key_hint: key_hint.to_owned(),
        created_at,
        updated_at,
    };
    put_secret_document(database, transaction, &document)?;
    write_secret_indexes(database, transaction, &document)?;
    serde_json::to_vec(&json!({
        "secret_reference": secret_reference,
        "key_hint": key_hint,
        "updated_at": updated_at,
    }))
    .map_err(|_| invalid_data("model-routing secret acknowledgement cannot be encoded"))
}

pub(crate) fn secret_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    secret_reference: &str,
) -> io::Result<Option<Vec<u8>>> {
    read_secret(database, transaction, tenant_label, secret_reference)?
        .map(|document| {
            serde_json::to_vec(&json!({
                "secret_reference": document.secret_reference,
                "ciphertext": document.ciphertext,
                "key_hint": document.key_hint,
                "created_at": document.created_at,
                "updated_at": document.updated_at,
            }))
            .map_err(|_| invalid_data("model-routing secret response cannot be encoded"))
        })
        .transpose()
}

pub(crate) fn secret_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<Vec<u8>> {
    let count = read_count(database, transaction, tenant_label)?;
    let (start, end) = secret_range(
        transaction,
        MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
        tenant_label,
    )?;
    let first_limit = (count + 1).min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
    let mut rows = database.entity_scan(transaction, &start, &end, first_limit)?;
    if rows.len() == crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS {
        let mut successor = rows
            .last()
            .expect("a full first secret-index page has a last row")
            .0
            .key_bytes()
            .to_vec();
        successor.push(0);
        let cursor = EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
            &successor,
        )?;
        let second_limit = count
            .saturating_sub(rows.len())
            .saturating_add(1)
            .min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        rows.extend(database.entity_scan(transaction, &cursor, &end, second_limit)?);
    }
    if rows.len() != count {
        return Err(invalid_data("model-routing secret count and index differ"));
    }
    let mut output = Vec::with_capacity(rows.len());
    for (_, raw) in rows {
        let identity: Value = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("model-routing reference index is malformed"))?;
        let reference = identity["secret_reference"]
            .as_str()
            .ok_or_else(|| invalid_data("model-routing reference index has no identity"))?;
        if identity["tenant_id"] != tenant_label {
            return Err(invalid_data(
                "model-routing reference index boundary differs",
            ));
        }
        let document = read_secret(database, transaction, tenant_label, reference)?
            .ok_or_else(|| invalid_data("model-routing indexed secret is missing"))?;
        output.push(json!({
            "secret_reference": document.secret_reference,
            "key_hint": document.key_hint,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
        }));
    }
    serde_json::to_vec(&output)
        .map_err(|_| invalid_data("model-routing secret list cannot be encoded"))
}

pub(crate) fn secret_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    secret_reference: &str,
) -> io::Result<Vec<u8>> {
    let current = read_secret(database, transaction, tenant_label, secret_reference)?;
    if let Some(document) = &current {
        remove_secret(database, transaction, document)?;
        let count = read_count(database, transaction, tenant_label)?;
        write_count(
            database,
            transaction,
            tenant_label,
            count
                .checked_sub(1)
                .ok_or_else(|| invalid_data("model-routing secret count underflow"))?,
        )?;
    }
    serde_json::to_vec(&json!({
        "deleted": current.is_some(),
        "secret_reference": secret_reference,
    }))
    .map_err(|_| invalid_data("model-routing delete response cannot be encoded"))
}

pub(crate) fn secret_prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    active_secret_references: &BTreeSet<String>,
    updated_before: f64,
) -> io::Result<Vec<u8>> {
    let (start, end) = secret_range(
        transaction,
        MODEL_ROUTING_SECRET_UPDATED_INDEX_NAMESPACE,
        tenant_label,
    )?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_MODEL_ROUTING_PRUNED_PER_COMMAND,
    )?;
    let mut removed = Vec::new();
    for (_, raw) in rows {
        let identity: Value = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("model-routing updated index is malformed"))?;
        let reference = identity["secret_reference"]
            .as_str()
            .ok_or_else(|| invalid_data("model-routing updated index has no identity"))?;
        if identity["tenant_id"] != tenant_label {
            return Err(invalid_data("model-routing updated index boundary differs"));
        }
        let document = read_secret(database, transaction, tenant_label, reference)?
            .ok_or_else(|| invalid_data("model-routing updated-index secret is missing"))?;
        if document.updated_at >= updated_before {
            break;
        }
        if active_secret_references.contains(reference) {
            continue;
        }
        remove_secret(database, transaction, &document)?;
        removed.push(reference.to_owned());
    }
    if !removed.is_empty() {
        let count = read_count(database, transaction, tenant_label)?;
        write_count(
            database,
            transaction,
            tenant_label,
            count
                .checked_sub(removed.len())
                .ok_or_else(|| invalid_data("model-routing secret count underflow"))?,
        )?;
    }
    let removed_count = removed.len();
    serde_json::to_vec(&json!({
        "removed": removed,
        "count": removed_count,
        "limit": MAX_MODEL_ROUTING_PRUNED_PER_COMMAND,
    }))
    .map_err(|_| invalid_data("model-routing prune response cannot be encoded"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existing_authority_receipt_update_does_not_rewrite_route_document() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let document = json!({
            "contract_version": "tofu.model-routing/v2",
            "revision": 1,
            "padding": "x".repeat(128 * 1024),
        });
        let mut create = database.begin(7, 11).unwrap();
        commit(
            &database,
            &mut create,
            CommitRequest {
                tenant_label: "workspace".to_owned(),
                expected_revision: 0,
                document,
                migration_receipt: None,
                updated_at: 1.0,
            },
        )
        .unwrap();
        database.commit(create).unwrap();

        let authority_key = {
            let read = database.begin(7, 11).unwrap();
            boundary_key(&read, MODEL_ROUTING_AUTHORITY_NAMESPACE, "workspace").unwrap()
        };
        let authority_version = {
            let mut read = database.begin(7, 11).unwrap();
            let stored = database
                .entity_get(&mut read, &authority_key)
                .unwrap()
                .unwrap();
            versioned_document::stored_document_version(
                &stored,
                MODEL_ROUTING_AUTHORITY_NAMESPACE,
                &logical_boundary_key("workspace"),
            )
            .unwrap()
        };
        let mut receipt = database.begin(7, 11).unwrap();
        put_migration_receipt(
            &database,
            &mut receipt,
            "workspace",
            json!({"status": "rejected"}),
            None,
            2.0,
        )
        .unwrap();
        database.commit(receipt).unwrap();

        let mut verify = database.begin(7, 11).unwrap();
        let stored = database
            .entity_get(&mut verify, &authority_key)
            .unwrap()
            .unwrap();
        assert_eq!(
            versioned_document::stored_document_version(
                &stored,
                MODEL_ROUTING_AUTHORITY_NAMESPACE,
                &logical_boundary_key("workspace"),
            )
            .unwrap(),
            authority_version
        );
        assert_eq!(
            serde_json::from_slice::<Value>(
                &migration_receipt(&database, &mut verify, "workspace")
                    .unwrap()
                    .unwrap()
            )
            .unwrap()["receipt"]["status"],
            "rejected"
        );

        let mut missing = database.begin(7, 12).unwrap();
        assert_eq!(
            put_migration_receipt(
                &database,
                &mut missing,
                "workspace",
                json!({"status": "rejected"}),
                None,
                2.0,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn authority_document_uses_the_complete_eight_mib_blob_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let empty_bytes = serde_json::to_vec(&json!({"padding": ""})).unwrap().len();
        let document = json!({
            "padding": "x".repeat(MAX_MODEL_ROUTING_DOCUMENT_BYTES - empty_bytes)
        });
        assert_eq!(
            serde_json::to_vec(&document).unwrap().len(),
            MAX_MODEL_ROUTING_DOCUMENT_BYTES
        );
        let mut write = database.begin(7, 11).unwrap();
        put_document(
            &database,
            &mut write,
            MODEL_ROUTING_AUTHORITY_NAMESPACE,
            "workspace",
            &document,
            1.0,
        )
        .unwrap();
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let restored = get_document(
            &database,
            &mut read,
            MODEL_ROUTING_AUTHORITY_NAMESPACE,
            "workspace",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            serde_json::to_vec(&restored).unwrap().len(),
            MAX_MODEL_ROUTING_DOCUMENT_BYTES
        );
    }

    #[test]
    fn secret_quota_and_two_page_reference_index_are_exact_at_1024_rows() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY {
            let reference = format!("secret-{index:04}");
            secret_put(
                &database,
                &mut transaction,
                "workspace",
                &reference,
                "sealed",
                "hint",
                index as f64 + 1.0,
            )
            .unwrap();
        }
        database.commit(transaction).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let list: Value =
            serde_json::from_slice(&secret_list(&database, &mut read, "workspace").unwrap())
                .unwrap();
        let list = list.as_array().unwrap();
        assert_eq!(list.len(), 1024);
        assert_eq!(list.first().unwrap()["secret_reference"], "secret-0000");
        assert_eq!(list.last().unwrap()["secret_reference"], "secret-1023");
        drop(read);

        let mut overflow = database.begin(7, 11).unwrap();
        assert_eq!(
            secret_put(
                &database,
                &mut overflow,
                "workspace",
                "secret-overflow",
                "sealed",
                "hint",
                2_000.0,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::WouldBlock
        );
    }
}
