//! Owner-scoped BYO-provider documents, quotas, ordering, and global ID claims.
//!
//! Provider secrets arrive already envelope-encrypted. This module never sees
//! plaintext: it atomically stores the ciphertext with the public document in
//! the blob-capable document family and removes it from list projections.

use std::io;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PROVIDERS_PER_OWNER_TENANT_LABEL, PROVIDER_COUNT_NAMESPACE,
    PROVIDER_CREATED_INDEX_NAMESPACE, PROVIDER_DOCUMENT_NAMESPACE, PROVIDER_ID_CLAIM_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "byo_providers";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}

fn digest(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    for part in parts {
        hasher.update((*part).len().to_be_bytes());
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn label_digest(label: &str) -> [u8; 32] {
    digest(b"tofu-db:provider-label:v1\0", &[label.as_bytes()])
}

fn logical_key(label: &str, provider_id: &str) -> String {
    format!("{}:{provider_id}", hex(&label_digest(label)))
}

fn hex(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    output
}

fn document_key(
    transaction: &AuthorityTransaction,
    label: &str,
    provider_id: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROVIDER_DOCUMENT_NAMESPACE,
        &digest(
            b"tofu-db:provider-document:v1\0",
            &[label.as_bytes(), provider_id.as_bytes()],
        ),
    )
}

fn count_key(transaction: &AuthorityTransaction, label: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROVIDER_COUNT_NAMESPACE,
        &label_digest(label),
    )
}

fn claim_key(transaction: &AuthorityTransaction, provider_id: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        PROVIDER_ID_CLAIM_NAMESPACE,
        &digest(b"tofu-db:provider-id:v1\0", &[provider_id.as_bytes()]),
    )
}

fn index_prefix(label: &str) -> [u8; 32] {
    label_digest(label)
}

fn index_key(
    transaction: &AuthorityTransaction,
    label: &str,
    created_at: f64,
    provider_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(41 + provider_id.len());
    raw.extend_from_slice(&index_prefix(label));
    raw.extend_from_slice(&(!created_at.to_bits()).to_be_bytes());
    raw.extend(provider_id.as_bytes().iter().map(|byte| !byte));
    raw.push(u8::MAX);
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROVIDER_CREATED_INDEX_NAMESPACE,
        &raw,
    )
}

fn index_range(
    transaction: &AuthorityTransaction,
    label: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROVIDER_CREATED_INDEX_NAMESPACE,
        &index_prefix(label),
    )
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
) -> io::Result<usize> {
    let Some(raw) = database.entity_get(transaction, &count_key(transaction, label)?)? else {
        return Ok(0);
    };
    let value: Value =
        serde_json::from_slice(&raw).map_err(|_| invalid_data("provider count is malformed"))?;
    if value["tenant_id"] != label {
        return Err(invalid_data("provider count label digest collision"));
    }
    value["count"]
        .as_u64()
        .and_then(|count| usize::try_from(count).ok())
        .filter(|count| *count <= MAX_PROVIDERS_PER_OWNER_TENANT_LABEL)
        .ok_or_else(|| invalid_data("provider count exceeds its bound"))
}

fn write_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    count: usize,
) -> io::Result<()> {
    if count > MAX_PROVIDERS_PER_OWNER_TENANT_LABEL {
        return Err(conflict("provider quota reached (32 per owner)"));
    }
    database.entity_put(
        transaction,
        count_key(transaction, label)?,
        serde_json::to_vec(&json!({"tenant_id": label, "count": count}))
            .map_err(|_| invalid_data("provider count cannot be encoded"))?,
    )
}

fn value_from_envelope(raw: &[u8]) -> io::Result<Map<String, Value>> {
    serde_json::from_slice::<Value>(raw)
        .ok()
        .and_then(|value| value.get("value").and_then(Value::as_object).cloned())
        .ok_or_else(|| invalid_data("provider document envelope is malformed"))
}

fn valid_text(
    document: &Map<String, Value>,
    field: &str,
    maximum: usize,
    allow_empty: bool,
) -> bool {
    document
        .get(field)
        .and_then(Value::as_str)
        .is_some_and(|value| (allow_empty || !value.is_empty()) && value.chars().count() <= maximum)
}

fn valid_number(document: &Map<String, Value>, field: &str, nullable: bool) -> bool {
    nullable && document.get(field) == Some(&Value::Null)
        || document
            .get(field)
            .and_then(Value::as_f64)
            .is_some_and(|value| value.is_finite() && (0.0..=32_503_680_000.0).contains(&value))
}

fn validate_document(
    document: &Map<String, Value>,
    owner_user_id: u64,
    label: &str,
    provider_id: &str,
) -> io::Result<()> {
    if document.get("id").and_then(Value::as_str) != Some(provider_id)
        || document.get("tenant_id").and_then(Value::as_str) != Some(label)
        || document.get("owner_user_id").and_then(Value::as_u64) != Some(owner_user_id)
    {
        return Err(invalid_data("provider document digest collision"));
    }
    if !valid_text(document, "name", 80, true)
        || !valid_text(document, "base_url", 500, true)
        || !valid_text(document, "api_key_ciphertext", 32_768, true)
        || !valid_text(document, "key_hint", 64, true)
        || !valid_text(document, "thinking_format", 64, true)
        || document
            .get("models")
            .and_then(Value::as_array)
            .is_none_or(|values| values.len() > 64)
        || document
            .get("extra_headers")
            .and_then(Value::as_object)
            .is_none_or(|values| values.len() > 16)
        || document.get("disabled").and_then(Value::as_bool).is_none()
        || !valid_number(document, "created_at", false)
        || !valid_number(document, "updated_at", false)
        || !valid_number(document, "last_used_at", true)
    {
        return Err(invalid_data("provider document fields are malformed"));
    }
    Ok(())
}

fn read_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    provider_id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    let key = document_key(transaction, label, provider_id)?;
    let Some(raw) = versioned_document::get(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        &logical_key(label, provider_id),
    )?
    else {
        return Ok(None);
    };
    let document = value_from_envelope(&raw)?;
    validate_document(&document, transaction.owner_user_id(), label, provider_id)?;
    Ok(Some(document))
}

fn encode_document(document: &Map<String, Value>, include_ciphertext: bool) -> io::Result<Vec<u8>> {
    let mut document = document.clone();
    if !include_ciphertext {
        document.remove("api_key_ciphertext");
    }
    serde_json::to_vec(&document).map_err(|_| invalid_data("provider response cannot be encoded"))
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    provider_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    read_document(database, transaction, label, provider_id)?
        .map(|document| encode_document(&document, true))
        .transpose()
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
) -> io::Result<Vec<u8>> {
    let expected = read_count(database, transaction, label)?;
    let (start, end) = index_range(transaction, label)?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_PROVIDERS_PER_OWNER_TENANT_LABEL + 1,
    )?;
    if rows.len() != expected {
        return Err(invalid_data("provider count does not match created index"));
    }
    let mut documents = Vec::with_capacity(rows.len());
    for (_, raw) in rows {
        let identity: Value = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("provider created index is malformed"))?;
        let provider_id = identity["id"]
            .as_str()
            .ok_or_else(|| invalid_data("provider created index ID is malformed"))?;
        if identity["tenant_id"] != label {
            return Err(invalid_data("provider created index label differs"));
        }
        let document = read_document(database, transaction, label, provider_id)?
            .ok_or_else(|| invalid_data("provider created index document is missing"))?;
        documents.push(Value::Object({
            let mut public = document;
            public.remove("api_key_ciphertext");
            public
        }));
    }
    serde_json::to_vec(&documents).map_err(|_| invalid_data("provider list cannot be encoded"))
}

pub(crate) fn create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    provider_id: &str,
    document_json: &[u8],
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let document = serde_json::from_slice::<Value>(document_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("provider document must be an object"))?;
    validate_document(&document, transaction.owner_user_id(), label, provider_id)?;
    let count = read_count(database, transaction, label)?;
    if count >= MAX_PROVIDERS_PER_OWNER_TENANT_LABEL {
        return Err(conflict("provider quota reached (32 per owner)"));
    }
    let claim = claim_key(transaction, provider_id)?;
    if database.entity_get(transaction, &claim)?.is_some() {
        return Err(conflict("provider ID already exists"));
    }
    let created_at = document["created_at"]
        .as_f64()
        .ok_or_else(|| invalid_input("provider created_at is malformed"))?;
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, label, provider_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: logical_key(label, provider_id),
            value_json: document_json.to_vec(),
            expected_version: Some(0),
            updated_at_ms: physical_updated_at_ms,
        },
    )?;
    database.entity_put(
        transaction,
        index_key(transaction, label, created_at, provider_id)?,
        serde_json::to_vec(&json!({"tenant_id": label, "id": provider_id}))
            .map_err(|_| invalid_data("provider index cannot be encoded"))?,
    )?;
    database.entity_put(
        transaction,
        claim,
        serde_json::to_vec(&json!({
            "owner_user_id": transaction.owner_user_id(),
            "tenant_id": label,
            "id": provider_id
        }))
        .map_err(|_| invalid_data("provider claim cannot be encoded"))?,
    )?;
    write_count(database, transaction, label, count + 1)?;
    encode_document(&document, true)
}

pub(crate) fn update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    provider_id: &str,
    updates_json: &[u8],
    updated_at: f64,
    physical_updated_at_ms: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some(mut document) = read_document(database, transaction, label, provider_id)? else {
        return Ok(None);
    };
    let updates = serde_json::from_slice::<Value>(updates_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .filter(|updates| !updates.is_empty())
        .ok_or_else(|| invalid_input("provider updates must be a nonempty object"))?;
    for (field, value) in updates {
        if !matches!(
            field.as_str(),
            "name"
                | "base_url"
                | "api_key_ciphertext"
                | "key_hint"
                | "models"
                | "extra_headers"
                | "thinking_format"
                | "disabled"
        ) {
            return Err(invalid_input("provider update contains an unknown field"));
        }
        document.insert(field, value);
    }
    document.insert("updated_at".to_owned(), json!(updated_at));
    validate_document(&document, transaction.owner_user_id(), label, provider_id)?;
    let value_json = serde_json::to_vec(&document)
        .map_err(|_| invalid_input("provider update cannot be encoded"))?;
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, label, provider_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: logical_key(label, provider_id),
            value_json,
            expected_version: None,
            updated_at_ms: physical_updated_at_ms,
        },
    )?;
    encode_document(&document, true).map(Some)
}

pub(crate) fn touch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    provider_id: &str,
    used_at: f64,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some(mut document) = read_document(database, transaction, label, provider_id)? else {
        return Ok(br#"{"touched":false}"#.to_vec());
    };
    document.insert("last_used_at".to_owned(), json!(used_at));
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, label, provider_id)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: logical_key(label, provider_id),
            value_json: serde_json::to_vec(&document)
                .map_err(|_| invalid_input("provider touch cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: physical_updated_at_ms,
        },
    )?;
    Ok(br#"{"touched":true}"#.to_vec())
}

pub(crate) fn delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    label: &str,
    provider_id: &str,
) -> io::Result<Vec<u8>> {
    let Some(document) = read_document(database, transaction, label, provider_id)? else {
        return Ok(br#"{"deleted":false}"#.to_vec());
    };
    let created_at = document["created_at"]
        .as_f64()
        .ok_or_else(|| invalid_data("provider created_at is malformed"))?;
    let claim = claim_key(transaction, provider_id)?;
    let expected_claim = serde_json::to_vec(&json!({
        "owner_user_id": transaction.owner_user_id(),
        "tenant_id": label,
        "id": provider_id
    }))
    .map_err(|_| invalid_data("provider claim cannot be encoded"))?;
    if database.entity_get(transaction, &claim)?.as_deref() != Some(expected_claim.as_slice()) {
        return Err(invalid_data("provider ID claim differs or is missing"));
    }
    versioned_document::delete(
        database,
        transaction,
        document_key(transaction, label, provider_id)?,
        LOGICAL_NAMESPACE,
        &logical_key(label, provider_id),
        None,
    )?;
    database.entity_delete(
        transaction,
        index_key(transaction, label, created_at, provider_id)?,
    )?;
    database.entity_delete(transaction, claim)?;
    let count = read_count(database, transaction, label)?;
    write_count(
        database,
        transaction,
        label,
        count
            .checked_sub(1)
            .ok_or_else(|| invalid_data("provider count underflow"))?,
    )?;
    Ok(br#"{"deleted":true}"#.to_vec())
}
