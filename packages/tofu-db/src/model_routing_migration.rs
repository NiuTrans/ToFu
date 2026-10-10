//! Offline model-routing authority and sealed-secret snapshot restoration
//! for the exclusive operator importer.
//!
//! Two sub-domains share the boundary digest layout of `model_routing`:
//!
//! - `storage_model_routing_authorities` preserves the exact revision,
//!   authority document, optional backup, optional migration receipt and the
//!   float `updated_at` timestamp. The live commit path derives the next
//!   revision, moves the current document into the backup slot and restamps
//!   metadata with the caller clock, so it cannot preserve the exact source
//!   triple. This adapter writes the authority/backup/receipt documents and
//!   the metadata claim directly inside one authority transaction.
//! - `storage_model_routing_secrets` preserves the exact ciphertext bytes,
//!   key hint and float timestamps. The live put path restamps `updated_at`
//!   and synthesizes `created_at`, so migration writes the exact document and
//!   rebuilds the reference index, updated index and owner boundary count in
//!   the same transaction. Ciphertext bytes are never logged: error strings
//!   carry no secret material.
//!
//! Insert-or-match keeps replays idempotent: an identical snapshot is
//! read-only, a divergent occupant fails closed. Get returns the exact
//! snapshot spelling; scans enumerate boundary/identity pairs owner-scoped
//! and independent of the source. Entry points: the six
//! `migration.model.routing.authority.*`/`migration.model.routing.secret.*`
//! operations dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_MODEL_ROUTING_DOCUMENT_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS,
    MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE, MODEL_ROUTING_AUTHORITY_NAMESPACE,
    MODEL_ROUTING_BACKUP_NAMESPACE, MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE,
    MODEL_ROUTING_SECRET_COUNT_NAMESPACE, MODEL_ROUTING_SECRET_NAMESPACE,
    MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE, MODEL_ROUTING_SECRET_UPDATED_INDEX_NAMESPACE,
};
use crate::versioned_document::{self, PutRequest};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn boundary_digest(tenant_label: &str) -> [u8; 32] {
    use sha2::{Digest, Sha256};
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

fn finite(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

// ---------------------------------------------------------------------------
// storage_model_routing_authorities
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthorityRestoreRequest {
    tenant_id: String,
    revision: u64,
    document: Value,
    backup: Value,
    migration_receipt: Value,
    updated_at: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BoundaryGetRequest {
    tenant_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn authority_snapshot_value(request: &AuthorityRestoreRequest) -> Value {
    json!({
        "tenant_id": request.tenant_id,
        "revision": request.revision,
        "document": request.document,
        "backup": request.backup,
        "migration_receipt": request.migration_receipt,
        "updated_at": request.updated_at,
    })
}

fn put_raw_document(
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
        return Err(invalid_input(
            "model-routing document exceeds its byte bound",
        ));
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

fn get_raw_document(
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

fn read_authority_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
) -> io::Result<Option<AuthorityRestoreRequest>> {
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
    let metadata: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("model-routing authority metadata is malformed"))?;
    if metadata.get("owner_user_id").and_then(Value::as_u64) != Some(transaction.owner_user_id())
        || metadata.get("tenant_id").and_then(Value::as_str) != Some(tenant_label)
    {
        return Err(invalid_data(
            "model-routing authority metadata identity is invalid",
        ));
    }
    let revision = metadata
        .get("revision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("model-routing authority metadata is malformed"))?;
    let updated_at = metadata
        .get("updated_at")
        .and_then(Value::as_f64)
        .filter(|value| finite(*value))
        .ok_or_else(|| invalid_data("model-routing authority metadata is malformed"))?;
    let document = get_raw_document(
        database,
        transaction,
        MODEL_ROUTING_AUTHORITY_NAMESPACE,
        tenant_label,
    )?
    .ok_or_else(|| invalid_data("model-routing authority document is missing"))?;
    let backup = get_raw_document(
        database,
        transaction,
        MODEL_ROUTING_BACKUP_NAMESPACE,
        tenant_label,
    )?
    .unwrap_or(Value::Null);
    let migration_receipt = get_raw_document(
        database,
        transaction,
        MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE,
        tenant_label,
    )?
    .unwrap_or(Value::Null);
    Ok(Some(AuthorityRestoreRequest {
        tenant_id: tenant_label.to_owned(),
        revision,
        document,
        backup,
        migration_receipt,
        updated_at,
    }))
}

fn authority_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: AuthorityRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid model routing authority restore request"))?;
    if request.tenant_id.chars().count() > 256
        || !request.document.is_object()
        || !(request.backup.is_null() || request.backup.is_object())
        || !(request.migration_receipt.is_null() || request.migration_receipt.is_object())
        || !finite(request.updated_at)
        || request.updated_at == 0.0
    {
        return Err(invalid_input("invalid model routing authority snapshot"));
    }
    if let Some(existing) = read_authority_snapshot(database, transaction, &request.tenant_id)? {
        if authority_snapshot_value(&existing) == authority_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict(
            "model routing authority snapshot differs",
        ));
    }
    put_raw_document(
        database,
        transaction,
        MODEL_ROUTING_AUTHORITY_NAMESPACE,
        &request.tenant_id,
        &request.document,
        request.updated_at,
    )?;
    if request.backup.is_object() {
        put_raw_document(
            database,
            transaction,
            MODEL_ROUTING_BACKUP_NAMESPACE,
            &request.tenant_id,
            &request.backup,
            request.updated_at,
        )?;
    }
    if request.migration_receipt.is_object() {
        put_raw_document(
            database,
            transaction,
            MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE,
            &request.tenant_id,
            &request.migration_receipt,
            request.updated_at,
        )?;
    }
    database.entity_put(
        transaction,
        boundary_key(
            transaction,
            MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE,
            &request.tenant_id,
        )?,
        serde_json::to_vec(&json!({
            "owner_user_id": transaction.owner_user_id(),
            "tenant_id": request.tenant_id,
            "revision": request.revision,
            "updated_at": request.updated_at,
        }))
        .map_err(|_| invalid_data("model-routing authority metadata cannot be encoded"))?,
    )?;
    let written = read_authority_snapshot(database, transaction, &request.tenant_id)?
        .ok_or_else(|| invalid_data("model routing authority snapshot is missing"))?;
    if authority_snapshot_value(&written) != authority_snapshot_value(&request) {
        return Err(invalid_data(
            "model routing authority snapshot readback differs",
        ));
    }
    Ok(true)
}

fn authority_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: BoundaryGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid model routing authority get request"))?;
    match read_authority_snapshot(database, transaction, &request.tenant_id)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(authority_snapshot_value(&snapshot)),
    }
}

// ---------------------------------------------------------------------------
// storage_model_routing_secrets
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SecretRestoreRequest {
    tenant_id: String,
    secret_reference: String,
    ciphertext: String,
    key_hint: String,
    created_at: f64,
    updated_at: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SecretGetRequest {
    tenant_id: String,
    secret_reference: String,
}

fn secret_snapshot_value(request: &SecretRestoreRequest) -> Value {
    json!({
        "tenant_id": request.tenant_id,
        "secret_reference": request.secret_reference,
        "ciphertext": request.ciphertext,
        "key_hint": request.key_hint,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
    })
}

fn read_secret_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    tenant_label: &str,
    secret_reference: &str,
) -> io::Result<Option<SecretRestoreRequest>> {
    let Some(raw) = versioned_document::get_value(
        database,
        transaction,
        &secret_key(
            transaction,
            MODEL_ROUTING_SECRET_NAMESPACE,
            tenant_label,
            secret_reference,
        )?,
        MODEL_ROUTING_SECRET_NAMESPACE,
        &logical_secret_key(tenant_label, secret_reference),
    )?
    else {
        return Ok(None);
    };
    let document: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("stored model-routing secret is malformed"))?;
    if document.get("owner_user_id").and_then(Value::as_u64) != Some(transaction.owner_user_id())
        || document.get("tenant_id").and_then(Value::as_str) != Some(tenant_label)
        || document.get("secret_reference").and_then(Value::as_str) != Some(secret_reference)
    {
        return Err(invalid_data(
            "stored model-routing secret identity is invalid",
        ));
    }
    let ciphertext = document
        .get("ciphertext")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("stored model-routing secret is malformed"))?;
    let key_hint = document
        .get("key_hint")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("stored model-routing secret is malformed"))?;
    let created_at = document
        .get("created_at")
        .and_then(Value::as_f64)
        .filter(|value| finite(*value))
        .ok_or_else(|| invalid_data("stored model-routing secret is malformed"))?;
    let updated_at = document
        .get("updated_at")
        .and_then(Value::as_f64)
        .filter(|value| finite(*value))
        .ok_or_else(|| invalid_data("stored model-routing secret is malformed"))?;
    Ok(Some(SecretRestoreRequest {
        tenant_id: tenant_label.to_owned(),
        secret_reference: secret_reference.to_owned(),
        ciphertext: ciphertext.to_owned(),
        key_hint: key_hint.to_owned(),
        created_at,
        updated_at,
    }))
}

fn read_secret_count(
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
        .ok_or_else(|| invalid_data("model-routing secret count exceeds its bound"))
}

fn secret_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: SecretRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid model routing secret restore request"))?;
    if request.tenant_id.chars().count() > 256
        || request.secret_reference.is_empty()
        || request.secret_reference.chars().count() > 512
        || request.ciphertext.len() > MAX_MODEL_ROUTING_DOCUMENT_BYTES
        || request.key_hint.chars().count() > 256
        || !finite(request.created_at)
        || request.created_at == 0.0
        || !finite(request.updated_at)
        || request.updated_at == 0.0
    {
        return Err(invalid_input("invalid model routing secret snapshot"));
    }
    if let Some(existing) = read_secret_snapshot(
        database,
        transaction,
        &request.tenant_id,
        &request.secret_reference,
    )? {
        if secret_snapshot_value(&existing) == secret_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict("model routing secret snapshot differs"));
    }
    let count = read_secret_count(database, transaction, &request.tenant_id)?;
    let next_count = count
        .checked_add(1)
        .ok_or_else(|| invalid_data("model-routing secret count overflow"))?;
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: secret_key(
                transaction,
                MODEL_ROUTING_SECRET_NAMESPACE,
                &request.tenant_id,
                &request.secret_reference,
            )?,
            namespace: MODEL_ROUTING_SECRET_NAMESPACE.to_owned(),
            logical_key: logical_secret_key(&request.tenant_id, &request.secret_reference),
            value_json: serde_json::to_vec(&json!({
                "owner_user_id": transaction.owner_user_id(),
                "tenant_id": request.tenant_id,
                "secret_reference": request.secret_reference,
                "ciphertext": request.ciphertext,
                "key_hint": request.key_hint,
                "created_at": request.created_at,
                "updated_at": request.updated_at,
            }))
            .map_err(|_| invalid_data("model-routing secret cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: physical_timestamp_milliseconds(request.updated_at),
        },
    )?;
    let identity = serde_json::to_vec(&json!({
        "tenant_id": request.tenant_id,
        "secret_reference": request.secret_reference,
    }))
    .map_err(|_| invalid_data("model-routing secret index cannot be encoded"))?;
    database.entity_put(
        transaction,
        secret_key(
            transaction,
            MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
            &request.tenant_id,
            &request.secret_reference,
        )?,
        identity.clone(),
    )?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &request.tenant_id,
            request.updated_at,
            &request.secret_reference,
        )?,
        identity,
    )?;
    database.entity_put(
        transaction,
        boundary_key(
            transaction,
            MODEL_ROUTING_SECRET_COUNT_NAMESPACE,
            &request.tenant_id,
        )?,
        serde_json::to_vec(&json!({"tenant_id": request.tenant_id, "count": next_count}))
            .map_err(|_| invalid_data("model-routing secret count cannot be encoded"))?,
    )?;
    let written = read_secret_snapshot(
        database,
        transaction,
        &request.tenant_id,
        &request.secret_reference,
    )?
    .ok_or_else(|| invalid_data("model routing secret snapshot is missing"))?;
    if secret_snapshot_value(&written) != secret_snapshot_value(&request) {
        return Err(invalid_data(
            "model routing secret snapshot readback differs",
        ));
    }
    Ok(true)
}

fn secret_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: SecretGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid model routing secret get request"))?;
    match read_secret_snapshot(
        database,
        transaction,
        &request.tenant_id,
        &request.secret_reference,
    )? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(secret_snapshot_value(&snapshot)),
    }
}

// ---------------------------------------------------------------------------
// shared scan
// ---------------------------------------------------------------------------

fn scan(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    kind: &str,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid model routing scan request"))?;
    let namespace = match kind {
        "authority" => MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE,
        _ => MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE,
    };
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("model_routing:")
            .ok_or_else(|| invalid_input("invalid model routing catalog cursor"))?;
        if encoded.len() != 64 || !encoded.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(invalid_input("invalid model routing catalog cursor"));
        }
        start = encoded
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                    .map_err(|_| invalid_input("invalid model routing catalog cursor"))
            })
            .collect::<io::Result<Vec<u8>>>()?;
        start.push(0);
    }
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &start,
    )?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        b"",
    )?;
    let page = database.entity_scan(transaction, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (_, raw) in &page {
        if kind == "authority" {
            let metadata: Value = serde_json::from_slice(raw)
                .map_err(|_| invalid_data("model-routing authority catalog is malformed"))?;
            if metadata.get("owner_user_id").and_then(Value::as_u64)
                != Some(transaction.owner_user_id())
            {
                return Err(invalid_data("model-routing authority catalog differs"));
            }
            let tenant_id = metadata
                .get("tenant_id")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("model-routing authority catalog is malformed"))?;
            rows.push(json!({"tenant_id": tenant_id}));
        } else {
            let identity: Value = serde_json::from_slice(raw)
                .map_err(|_| invalid_data("model-routing reference index is malformed"))?;
            let tenant_id = identity
                .get("tenant_id")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("model-routing reference index is malformed"))?;
            let reference = identity
                .get("secret_reference")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("model-routing reference index is malformed"))?;
            if read_secret_snapshot(database, transaction, tenant_id, reference)?.is_none() {
                return Err(invalid_data("model-routing indexed secret is missing"));
            }
            rows.push(json!({"tenant_id": tenant_id, "secret_reference": reference}));
        }
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        Some(format!(
            "model_routing:{}",
            page.last()
                .expect("a full page has a last row")
                .0
                .key_bytes()
                .iter()
                .take(32)
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        ))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin(tenant, owner)?;
    match operation {
        "migration.model.routing.authority.restore" => {
            let inserted = authority_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.model.routing.authority.get" => {
            authority_get(database, &mut transaction, payload)
        }
        "migration.model.routing.authority.scan" => {
            scan(database, &mut transaction, "authority", payload)
        }
        "migration.model.routing.secret.restore" => {
            let inserted = secret_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.model.routing.secret.get" => secret_get(database, &mut transaction, payload),
        "migration.model.routing.secret.scan" => {
            scan(database, &mut transaction, "secret", payload)
        }
        _ => Err(invalid_input("unknown model routing migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn authority_snapshot() -> Value {
        json!({
            "tenant_id": "team-a",
            "revision": 5,
            "document": {"contract_version": "tofu.model-routing/v2", "revision": 5},
            "backup": {"contract_version": "tofu.model-routing/v2", "revision": 4},
            "migration_receipt": {"status": "complete"},
            "updated_at": 1_700_000_000.5,
        })
    }

    fn secret_snapshot(reference: &str) -> Value {
        // Test ciphertext is opaque placeholder bytes, never real key material.
        json!({
            "tenant_id": "team-a",
            "secret_reference": reference,
            "ciphertext": "dGVzdC1jaXBoZXJ0ZXh0LXBhZGRpbmc",
            "key_hint": "hint-1",
            "created_at": 1_700_000_000.25,
            "updated_at": 1_700_000_000.5,
        })
    }

    #[test]
    fn authority_restore_preserves_exact_triple_across_reopen() {
        let (vfs, mut database) = prepared();
        let snapshot = authority_snapshot();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.authority.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.authority.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.authority.get",
                json!({"tenant_id": "team-a"})
            )
            .unwrap(),
            snapshot
        );
        // The live get path observes the exact revision and document.
        let mut transaction = database.begin(7, 11).unwrap();
        let live = crate::model_routing::get(&database, &mut transaction, "team-a").unwrap();
        let live: Value = serde_json::from_slice(&live.unwrap()).unwrap();
        assert_eq!(live["revision"], 5);
        assert_eq!(live["updated_at"], 1_700_000_000.5);
        drop(transaction);
        let mut divergent = snapshot.clone();
        divergent["revision"] = json!(6);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.model.routing.authority.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn secret_restore_demand_preserves_full_count_and_replay() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..2105 {
            assert!(secret_restore(
                &database,
                &mut transaction,
                secret_snapshot(&format!("ref-{index:04}"))
            )
            .unwrap());
        }
        database.commit(transaction).unwrap();
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(!secret_restore(&database, &mut transaction, secret_snapshot("ref-2104")).unwrap());
        let mut divergent = secret_snapshot("ref-2104");
        divergent["ciphertext"] = json!("different");
        assert!(secret_restore(&database, &mut transaction, divergent).is_err());
        let list: Value = serde_json::from_slice(
            &crate::model_routing::secret_list(&database, &mut transaction, "team-a").unwrap(),
        )
        .unwrap();
        assert_eq!(list.as_array().unwrap().len(), 2105);
        assert_eq!(list[2104]["secret_reference"], "ref-2104");
    }

    #[test]
    fn secret_restore_preserves_exact_bytes_and_live_list_matches() {
        let (vfs, mut database) = prepared();
        let snapshot = secret_snapshot("ref-1");
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.secret.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.secret.restore",
                snapshot.clone()
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.secret.get",
                json!({"tenant_id": "team-a", "secret_reference": "ref-1"})
            )
            .unwrap(),
            snapshot
        );
        let mut transaction = database.begin(7, 11).unwrap();
        let live =
            crate::model_routing::secret_list(&database, &mut transaction, "team-a").unwrap();
        let live: Value = serde_json::from_slice(&live).unwrap();
        let rows = live.as_array().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["secret_reference"], "ref-1");
        assert_eq!(rows[0]["key_hint"], "hint-1");
        drop(transaction);
        let mut divergent = snapshot.clone();
        divergent["key_hint"] = json!("other");
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.model.routing.secret.restore",
            divergent
        )
        .is_err());
        // Scans are owner scoped.
        let page = execute(
            &mut database,
            7,
            11,
            "migration.model.routing.secret.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 1);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.model.routing.secret.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }

    #[test]
    fn invalid_snapshots_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"tenant_id": "a", "revision": 1, "document": [],
                   "backup": null, "migration_receipt": null, "updated_at": 1.0}),
            json!({"tenant_id": "a", "revision": 1, "document": {},
                   "backup": null, "migration_receipt": null, "updated_at": 0.0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.model.routing.authority.restore",
                payload
            )
            .is_err());
        }
        let mut bad = secret_snapshot("ref-1");
        bad["updated_at"] = json!(0.0);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.model.routing.secret.restore",
            bad
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.model.routing.authority.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
