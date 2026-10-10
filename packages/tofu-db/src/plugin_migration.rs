//! Offline plugin manifest/row/unique-value snapshot restoration for the
//! exclusive operator importer.
//!
//! Three sub-domains:
//!
//! - `storage_plugin_manifests` re-validates the source manifest through the
//!   live `plugin::validate_manifest` admission and stores the canonical
//!   normalized document with the source `manifest_version` recorded. The
//!   live register path rejects equal-version byte drift, so the restore
//!   keeps the source `updated_at_ms` and refuses a divergent occupant.
//! - `storage_plugin_rows` stores the exact document JSON under a
//!   tenant/owner scoped versioned document keyed by
//!   (namespace, table_name, row_key) with the source version and timestamp.
//!   The legacy engine keeps plugin rows in raw SQLite tables; the native
//!   layout makes them migration-owned versioned documents whose unique
//!   claims are rebuilt below.
//! - `storage_plugin_unique_values` re-asserts one unique claim per row
//!   only after the referenced row snapshot is present, and fails closed
//!   when the referenced row is missing or the claim identity differs.
//!
//! Insert-or-match keeps replays idempotent. Scans enumerate identities
//! owner-scoped and independent of the source. Entry points: the nine
//! `migration.plugin.manifest.*`/`migration.plugin.rows.*`/
//! `migration.plugin.unique.*` operations dispatched from
//! `migration_session`.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PLUGIN_MANIFEST_BYTES, MAX_PLUGIN_ROW_DOCUMENT_BYTES, MAX_PLUGIN_ROW_FIELDS,
    MIGRATION_SESSION_MAX_PAGE_ROWS, PLUGIN_MANIFEST_DOCUMENT_NAMESPACE,
    PLUGIN_ROW_DOCUMENT_NAMESPACE, PLUGIN_ROW_UNIQUE_NAMESPACE,
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

// ---------------------------------------------------------------------------
// storage_plugin_manifests
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestRestoreRequest {
    namespace: String,
    manifest_version: u64,
    manifest_json: Value,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestGetRequest {
    namespace: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    cursor: Option<String>,
}

fn manifest_key(transaction: &AuthorityTransaction, namespace: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PLUGIN_MANIFEST_DOCUMENT_NAMESPACE,
        namespace.as_bytes(),
    )
}

fn manifest_snapshot_value(request: &ManifestRestoreRequest) -> Value {
    json!({
        "namespace": request.namespace,
        "manifest_version": request.manifest_version,
        "manifest_json": request.manifest_json,
        "updated_at_ms": request.updated_at_ms,
    })
}

fn read_manifest_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<Option<ManifestRestoreRequest>> {
    // Presence is determined by the canonical manifest document; the record
    // entity carries the exact source spelling (un-normalized manifest plus
    // the source metadata) so insert-or-match stays byte-exact.
    let Some(document) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &manifest_key(transaction, namespace)?,
        "plugin_manifests",
        namespace,
        transaction.owner_user_id(),
        MAX_PLUGIN_MANIFEST_BYTES,
    )?
    else {
        return Ok(None);
    };
    let canonical: Value = serde_json::from_slice(&document)
        .map_err(|_| invalid_data("stored plugin manifest is malformed"))?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &manifest_record_key(transaction, namespace)?,
        "plugin_manifest_records",
        namespace,
        transaction.owner_user_id(),
        MAX_PLUGIN_MANIFEST_BYTES + 4_096,
    )?
    else {
        return Err(invalid_data("stored plugin manifest record is missing"));
    };
    let record: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("stored plugin manifest record is malformed"))?;
    let manifest_version = record
        .get("manifest_version")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("stored plugin manifest record is malformed"))?;
    let updated_at_ms = record
        .get("updated_at_ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("stored plugin manifest record is malformed"))?;
    let manifest_json = record
        .get("manifest_json")
        .cloned()
        .ok_or_else(|| invalid_data("stored plugin manifest record is malformed"))?;
    if canonical.get("namespace").and_then(Value::as_str) != Some(namespace)
        || canonical.get("version").and_then(Value::as_u64) != Some(manifest_version)
    {
        return Err(invalid_data("stored plugin manifest identity differs"));
    }
    Ok(Some(ManifestRestoreRequest {
        namespace: namespace.to_owned(),
        manifest_version,
        manifest_json,
        updated_at_ms,
    }))
}

/// Entity namespace for the byte-exact source-spelling records kept next to
/// each canonical manifest. Kept out of `PLUGIN_MANIFEST_DOCUMENT_NAMESPACE`
/// so the manifest catalog scan enumerates canonical documents only.
const PLUGIN_MANIFEST_RECORD_DOCUMENT_NAMESPACE: &str = "plugin_manifest_record_documents";

fn manifest_record_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
) -> io::Result<EntityKey> {
    let mut raw = namespace.as_bytes().to_vec();
    raw.push(0);
    raw.extend_from_slice(b"record");
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PLUGIN_MANIFEST_RECORD_DOCUMENT_NAMESPACE,
        &raw,
    )
}

fn manifest_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: ManifestRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin manifest restore request"))?;
    if request.namespace.chars().count() > 128
        || request.manifest_version == 0
        || request.updated_at_ms == 0
    {
        return Err(invalid_input("invalid plugin manifest snapshot metadata"));
    }
    // Re-run the live admission so a malformed source manifest cannot land.
    let canonical = crate::plugin::validate_manifest(&request.manifest_json)?;
    if canonical.get("namespace").and_then(Value::as_str) != Some(request.namespace.as_str())
        || canonical.get("version").and_then(Value::as_u64) != Some(request.manifest_version)
    {
        return Err(invalid_input(
            "plugin manifest snapshot identity differs from its manifest",
        ));
    }
    if let Some(existing) = read_manifest_snapshot(database, transaction, &request.namespace)? {
        if manifest_snapshot_value(&existing) == manifest_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict("plugin manifest snapshot differs"));
    }
    let canonical_bytes = serde_json::to_vec(&canonical)
        .map_err(|_| invalid_data("plugin manifest cannot be encoded"))?;
    if canonical_bytes.len() > MAX_PLUGIN_MANIFEST_BYTES {
        return Err(invalid_input("plugin manifest exceeds its bound"));
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: manifest_key(transaction, &request.namespace)?,
            namespace: "plugin_manifests".to_owned(),
            logical_key: request.namespace.clone(),
            value_json: canonical_bytes,
            expected_version: None,
            updated_at_ms: request.updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_PLUGIN_MANIFEST_BYTES,
    )?;
    // Read back through the same path the live register exposes. The record
    // carries the un-normalized manifest verbatim, so it can exceed the
    // inline entity ceiling just like the canonical document; it uses the
    // same blob-capable placement instead of an inline entity value.
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: manifest_record_key(transaction, &request.namespace)?,
            namespace: "plugin_manifest_records".to_owned(),
            logical_key: request.namespace.clone(),
            value_json: serde_json::to_vec(&json!({
                "manifest_version": request.manifest_version,
                "updated_at_ms": request.updated_at_ms,
                "manifest_json": request.manifest_json,
            }))
            .map_err(|_| invalid_data("plugin manifest record cannot be encoded"))?,
            expected_version: None,
            updated_at_ms: request.updated_at_ms,
        },
        transaction.owner_user_id(),
        MAX_PLUGIN_MANIFEST_BYTES + 4_096,
    )?;
    let written = read_manifest_snapshot(database, transaction, &request.namespace)?
        .ok_or_else(|| invalid_data("plugin manifest snapshot is missing"))?;
    if manifest_snapshot_value(&written) != manifest_snapshot_value(&request) {
        return Err(invalid_data("plugin manifest snapshot readback differs"));
    }
    Ok(true)
}

fn manifest_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: ManifestGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin manifest get request"))?;
    match read_manifest_snapshot(database, transaction, &request.namespace)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(manifest_snapshot_value(&snapshot)),
    }
}

// ---------------------------------------------------------------------------
// storage_plugin_rows / storage_plugin_unique_values
// ---------------------------------------------------------------------------

pub(crate) fn row_composite(namespace: &str, table: &str, row_key: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(namespace.len() + table.len() + row_key.len() + 4);
    for part in [namespace, table] {
        raw.extend_from_slice(
            &u16::try_from(part.len())
                .map_err(|_| invalid_input("plugin row identity exceeds its bound"))?
                .to_be_bytes(),
        );
        raw.extend_from_slice(part.as_bytes());
    }
    raw.extend_from_slice(row_key.as_bytes());
    Ok(raw)
}

pub(crate) fn row_document_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    table: &str,
    row_key: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PLUGIN_ROW_DOCUMENT_NAMESPACE,
        &row_composite(namespace, table, row_key)?,
    )
}

pub(crate) fn unique_composite(
    namespace: &str,
    table: &str,
    index_name: &str,
    index_value: &str,
) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(
        namespace.len() + table.len() + index_name.len() + index_value.len() + 6,
    );
    for part in [namespace, table, index_name] {
        raw.extend_from_slice(
            &u16::try_from(part.len())
                .map_err(|_| invalid_input("plugin unique identity exceeds its bound"))?
                .to_be_bytes(),
        );
        raw.extend_from_slice(part.as_bytes());
    }
    raw.extend_from_slice(index_value.as_bytes());
    Ok(raw)
}

pub(crate) fn unique_claim_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    table: &str,
    index_name: &str,
    index_value: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PLUGIN_ROW_UNIQUE_NAMESPACE,
        &unique_composite(namespace, table, index_name, index_value)?,
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RowRestoreRequest {
    namespace: String,
    table_name: String,
    row_key: String,
    document_json: String,
    version: u64,
    updated_at_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RowGetRequest {
    namespace: String,
    table_name: String,
    row_key: String,
}

fn row_snapshot_value(request: &RowRestoreRequest) -> Value {
    json!({
        "namespace": request.namespace,
        "table_name": request.table_name,
        "row_key": request.row_key,
        "document_json": request.document_json,
        "version": request.version,
        "updated_at_ms": request.updated_at_ms,
    })
}

fn read_row_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RowGetRequest,
) -> io::Result<Option<RowRestoreRequest>> {
    let composite = row_composite(&request.namespace, &request.table_name, &request.row_key)?;
    let Some(snapshot) = versioned_document::read_snapshot(
        database,
        transaction,
        &EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            PLUGIN_ROW_DOCUMENT_NAMESPACE,
            &composite,
        )?,
        PLUGIN_ROW_DOCUMENT_NAMESPACE,
        &format!(
            "{}:{}:{}",
            request.namespace, request.table_name, request.row_key
        ),
    )?
    else {
        return Ok(None);
    };
    let document_json = String::from_utf8(snapshot.value_json)
        .map_err(|_| invalid_data("plugin row document is not UTF-8"))?;
    Ok(Some(RowRestoreRequest {
        namespace: request.namespace.clone(),
        table_name: request.table_name.clone(),
        row_key: request.row_key.clone(),
        document_json,
        version: snapshot.version,
        updated_at_ms: snapshot.updated_at_ms,
    }))
}

fn row_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: RowRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin row restore request"))?;
    if request.namespace.is_empty()
        || request.namespace.chars().count() > 128
        || request.table_name.is_empty()
        || request.table_name.chars().count() > 128
        || request.row_key.chars().count() > 512
        || request.version == 0
        || request.updated_at_ms == 0
        || request.document_json.len() > MAX_PLUGIN_ROW_DOCUMENT_BYTES
    {
        return Err(invalid_input("invalid plugin row snapshot metadata"));
    }
    let document: Value = serde_json::from_str(&request.document_json)
        .map_err(|_| invalid_input("plugin row document is not valid JSON"))?;
    if !document.is_object() || document.as_object().expect("checked").len() > MAX_PLUGIN_ROW_FIELDS
    {
        return Err(invalid_input("plugin row document is not a bounded object"));
    }
    if let Some(existing) = read_row_snapshot(
        database,
        transaction,
        &RowGetRequest {
            namespace: request.namespace.clone(),
            table_name: request.table_name.clone(),
            row_key: request.row_key.clone(),
        },
    )? {
        if row_snapshot_value(&existing) == row_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict("plugin row snapshot differs"));
    }
    let outcome = versioned_document::restore_snapshot(
        database,
        transaction,
        versioned_document::SnapshotDocument {
            key: row_document_key(
                transaction,
                &request.namespace,
                &request.table_name,
                &request.row_key,
            )?,
            namespace: PLUGIN_ROW_DOCUMENT_NAMESPACE.to_owned(),
            logical_key: format!(
                "{}:{}:{}",
                request.namespace, request.table_name, request.row_key
            ),
            version: request.version,
            updated_at_ms: request.updated_at_ms,
            value_json: request.document_json.clone().into_bytes(),
            blob_owner_user_id: transaction.owner_user_id(),
        },
    )?;
    Ok(matches!(
        outcome,
        versioned_document::SnapshotRestoreOutcome::Inserted
    ))
}

fn row_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: RowGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin row get request"))?;
    match read_row_snapshot(database, transaction, &request)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(row_snapshot_value(&snapshot)),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UniqueRestoreRequest {
    namespace: String,
    table_name: String,
    index_name: String,
    index_value: String,
    row_key: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UniqueGetRequest {
    namespace: String,
    table_name: String,
    index_name: String,
    index_value: String,
}

fn unique_snapshot_value(request: &UniqueRestoreRequest) -> Value {
    json!({
        "namespace": request.namespace,
        "table_name": request.table_name,
        "index_name": request.index_name,
        "index_value": request.index_value,
        "row_key": request.row_key,
    })
}

fn read_unique_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &UniqueGetRequest,
) -> io::Result<Option<UniqueRestoreRequest>> {
    let key = unique_claim_key(
        transaction,
        &request.namespace,
        &request.table_name,
        &request.index_name,
        &request.index_value,
    )?;
    let Some(raw) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("plugin unique claim is malformed"))?;
    let row_key = value
        .get("row_key")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("plugin unique claim is malformed"))?
        .to_owned();
    Ok(Some(UniqueRestoreRequest {
        namespace: request.namespace.clone(),
        table_name: request.table_name.clone(),
        index_name: request.index_name.clone(),
        index_value: request.index_value.clone(),
        row_key,
    }))
}

fn unique_restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: UniqueRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin unique restore request"))?;
    if request.namespace.is_empty()
        || request.table_name.is_empty()
        || request.index_name.is_empty()
        || request.index_name.chars().count() > 128
        || request.index_value.chars().count() > 1024
        || request.row_key.chars().count() > 512
    {
        return Err(invalid_input("invalid plugin unique snapshot metadata"));
    }
    if let Some(existing) = read_unique_snapshot(
        database,
        transaction,
        &UniqueGetRequest {
            namespace: request.namespace.clone(),
            table_name: request.table_name.clone(),
            index_name: request.index_name.clone(),
            index_value: request.index_value.clone(),
        },
    )? {
        if unique_snapshot_value(&existing) == unique_snapshot_value(&request) {
            return Ok(false);
        }
        return Err(snapshot_conflict("plugin unique claim differs"));
    }
    // Fail closed unless the referenced row snapshot already landed.
    if read_row_snapshot(
        database,
        transaction,
        &RowGetRequest {
            namespace: request.namespace.clone(),
            table_name: request.table_name.clone(),
            row_key: request.row_key.clone(),
        },
    )?
    .is_none()
    {
        return Err(invalid_input(
            "plugin unique claim references a missing row",
        ));
    }
    database.entity_put(
        transaction,
        unique_claim_key(
            transaction,
            &request.namespace,
            &request.table_name,
            &request.index_name,
            &request.index_value,
        )?,
        serde_json::to_vec(&json!({"row_key": request.row_key}))
            .map_err(|_| invalid_data("plugin unique claim cannot be encoded"))?,
    )?;
    Ok(true)
}

fn unique_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: UniqueGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin unique get request"))?;
    match read_unique_snapshot(database, transaction, &request)? {
        None => Ok(Value::Null),
        Some(snapshot) => Ok(unique_snapshot_value(&snapshot)),
    }
}

// ---------------------------------------------------------------------------
// scans
// ---------------------------------------------------------------------------

fn scan_namespace(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix_label: &str,
    payload: Value,
) -> io::Result<Value> {
    let request: ScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid plugin migration scan request"))?;
    let mut start = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix(prefix_label)
            .ok_or_else(|| invalid_input("invalid plugin catalog cursor"))?;
        if encoded.is_empty()
            || encoded.len() > 4096
            || !encoded.len().is_multiple_of(2)
            || !encoded.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err(invalid_input("invalid plugin catalog cursor"));
        }
        start = encoded
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                    .map_err(|_| invalid_input("invalid plugin catalog cursor"))
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
    for (physical, _) in &page {
        let raw = physical.key_bytes();
        let (identity, expected) = match namespace {
            PLUGIN_MANIFEST_DOCUMENT_NAMESPACE => {
                let name = std::str::from_utf8(raw)
                    .map_err(|_| invalid_data("plugin manifest catalog is malformed"))?;
                if read_manifest_snapshot(database, transaction, name)?.is_none() {
                    return Err(invalid_data("plugin manifest catalog target is missing"));
                }
                (json!({"namespace": name}), manifest_key(transaction, name)?)
            }
            PLUGIN_ROW_DOCUMENT_NAMESPACE => {
                let Some(snapshot) = versioned_document::read_snapshot(
                    database,
                    transaction,
                    physical,
                    PLUGIN_ROW_DOCUMENT_NAMESPACE,
                    physical_logical_key(raw)?,
                )?
                else {
                    return Err(invalid_data("plugin row catalog target is missing"));
                };
                let mut parts = snapshot.logical_key.splitn(3, ':');
                let (ns, table, row_key) = (
                    parts.next().unwrap_or_default(),
                    parts.next().unwrap_or_default(),
                    parts.next().unwrap_or_default(),
                );
                (
                    json!({"namespace": ns, "table_name": table, "row_key": row_key}),
                    row_document_key(transaction, ns, table, row_key)?,
                )
            }
            _ => {
                let claim: Value = serde_json::from_slice(
                    &database
                        .entity_get(transaction, physical)?
                        .ok_or_else(|| invalid_data("plugin unique catalog target is missing"))?,
                )
                .map_err(|_| invalid_data("plugin unique catalog is malformed"))?;
                let row_key = claim
                    .get("row_key")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("plugin unique catalog is malformed"))?;
                // Decode (namespace, table, index_name, index_value) composite.
                let identity = decode_unique_composite(raw)?;
                (
                    json!({
                        "namespace": identity.0,
                        "table_name": identity.1,
                        "index_name": identity.2,
                        "index_value": identity.3,
                        "row_key": row_key,
                    }),
                    unique_claim_key(
                        transaction,
                        &identity.0,
                        &identity.1,
                        &identity.2,
                        &identity.3,
                    )?,
                )
            }
        };
        if &expected != physical {
            return Err(invalid_data("plugin catalog identity differs"));
        }
        rows.push(identity);
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        Some(format!(
            "{}{}",
            prefix_label,
            page.last()
                .expect("a full page has a last row")
                .0
                .key_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        ))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn physical_logical_key(raw: &[u8]) -> io::Result<&str> {
    // Plugin row physical keys are the composite u16-length-prefixed encoding;
    // logical keys use `namespace:table:row_key`. Recover the logical key from
    // the composite so read_snapshot can validate identity.
    let mut parts = Vec::with_capacity(3);
    let mut rest = raw;
    for _ in 0..2 {
        if rest.len() < 2 {
            return Err(invalid_data("plugin row catalog key is malformed"));
        }
        let length = u16::from_be_bytes([rest[0], rest[1]]) as usize;
        let part = rest
            .get(2..2 + length)
            .ok_or_else(|| invalid_data("plugin row catalog key is malformed"))?;
        parts.push(
            std::str::from_utf8(part)
                .map_err(|_| invalid_data("plugin row catalog key is malformed"))?,
        );
        rest = &rest[2 + length..];
    }
    let row_key = std::str::from_utf8(rest)
        .map_err(|_| invalid_data("plugin row catalog key is malformed"))?;
    Ok(Box::leak(
        format!("{}:{}:{}", parts[0], parts[1], row_key).into_boxed_str(),
    ))
}

fn decode_unique_composite(raw: &[u8]) -> io::Result<(String, String, String, String)> {
    let mut parts = Vec::with_capacity(3);
    let mut rest = raw;
    for _ in 0..3 {
        if rest.len() < 2 {
            return Err(invalid_data("plugin unique catalog key is malformed"));
        }
        let length = u16::from_be_bytes([rest[0], rest[1]]) as usize;
        let part = rest
            .get(2..2 + length)
            .ok_or_else(|| invalid_data("plugin unique catalog key is malformed"))?;
        parts.push(
            std::str::from_utf8(part)
                .map_err(|_| invalid_data("plugin unique catalog key is malformed"))?
                .to_owned(),
        );
        rest = &rest[2 + length..];
    }
    let index_value = std::str::from_utf8(rest)
        .map_err(|_| invalid_data("plugin unique catalog key is malformed"))?
        .to_owned();
    let mut parts = parts.into_iter();
    Ok((
        parts.next().unwrap_or_default(),
        parts.next().unwrap_or_default(),
        parts.next().unwrap_or_default(),
        index_value,
    ))
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
        "migration.plugin.manifest.restore" => {
            let inserted = manifest_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.plugin.manifest.get" => manifest_get(database, &mut transaction, payload),
        "migration.plugin.manifest.scan" => scan_namespace(
            database,
            &mut transaction,
            PLUGIN_MANIFEST_DOCUMENT_NAMESPACE,
            "plugin_manifests:",
            payload,
        ),
        "migration.plugin.rows.restore" => {
            let inserted = row_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.plugin.rows.get" => row_get(database, &mut transaction, payload),
        "migration.plugin.rows.scan" => scan_namespace(
            database,
            &mut transaction,
            PLUGIN_ROW_DOCUMENT_NAMESPACE,
            "plugin_rows:",
            payload,
        ),
        "migration.plugin.unique.restore" => {
            let inserted = unique_restore(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.plugin.unique.get" => unique_get(database, &mut transaction, payload),
        "migration.plugin.unique.scan" => scan_namespace(
            database,
            &mut transaction,
            PLUGIN_ROW_UNIQUE_NAMESPACE,
            "plugin_unique:",
            payload,
        ),
        _ => Err(invalid_input("unknown plugin migration operation")),
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

    fn manifest() -> Value {
        json!({
            "namespace": "alpha.beta",
            "version": 1,
            "tables": [{
                "name": "items",
                "columns": [{"name": "id", "type": "string", "required": true}],
                "primary_key": ["id"],
                "indexes": [{"name": "by_id", "columns": ["id"], "unique": true}],
            }],
            "operations": [{
                "name": "fetch",
                "action": "get",
                "table": "items",
                "limit_max": 100,
            }],
        })
    }

    #[test]
    fn manifest_restore_preserves_exact_normalized_document_across_reopen() {
        let (vfs, mut database) = prepared();
        let snapshot = json!({
            "namespace": "alpha.beta",
            "manifest_version": 1,
            "manifest_json": manifest(),
            "updated_at_ms": 424242,
        });
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.plugin.manifest.restore",
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
                "migration.plugin.manifest.restore",
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
                "migration.plugin.manifest.get",
                json!({"namespace": "alpha.beta"})
            )
            .unwrap(),
            snapshot
        );
        // The live manifest get path sees the normalized document.
        let mut transaction = database.begin(7, 11).unwrap();
        let live = crate::plugin::manifest_get(&database, &mut transaction, "alpha.beta").unwrap();
        let live: Value = serde_json::from_slice(&live).unwrap();
        assert_eq!(live["namespace"], "alpha.beta");
        assert_eq!(live["version"], 1);
        drop(transaction);
        let mut divergent = snapshot.clone();
        divergent["manifest_version"] = json!(2);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.plugin.manifest.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn row_and_unique_restore_enforce_row_presence_and_identity() {
        let (_vfs, mut database) = prepared();
        let row = json!({
            "namespace": "alpha.beta",
            "table_name": "items",
            "row_key": "item-1",
            "document_json": "{\"id\":\"item-1\",\"note\":\"n\"}",
            "version": 3,
            "updated_at_ms": 999,
        });
        // Unique claim before its row fails closed.
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.plugin.unique.restore",
            json!({
                "namespace": "alpha.beta",
                "table_name": "items",
                "index_name": "by_id",
                "index_value": "item-1",
                "row_key": "item-1",
            })
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.plugin.rows.restore",
                row.clone()
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.plugin.rows.restore",
                row.clone()
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.plugin.unique.restore",
                json!({
                    "namespace": "alpha.beta",
                    "table_name": "items",
                    "index_name": "by_id",
                    "index_value": "item-1",
                    "row_key": "item-1",
                })
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.plugin.rows.get",
                json!({
                    "namespace": "alpha.beta",
                    "table_name": "items",
                    "row_key": "item-1",
                })
            )
            .unwrap(),
            row
        );
        let rows_page = execute(
            &mut database,
            7,
            11,
            "migration.plugin.rows.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(rows_page["rows"].as_array().unwrap().len(), 1);
        let unique_page = execute(
            &mut database,
            7,
            11,
            "migration.plugin.unique.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(unique_page["rows"].as_array().unwrap().len(), 1);
        // Owner isolation.
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.plugin.rows.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
        let mut divergent = row.clone();
        divergent["version"] = json!(4);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.plugin.rows.restore",
            divergent
        )
        .is_err());
    }

    #[test]
    fn invalid_snapshots_fail_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"namespace": "", "manifest_version": 1, "manifest_json": {},
                   "updated_at_ms": 1}),
            json!({"namespace": "alpha.beta", "manifest_version": 0,
                   "manifest_json": manifest(), "updated_at_ms": 1}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.plugin.manifest.restore",
                payload
            )
            .is_err());
        }
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.plugin.rows.restore",
            json!({"namespace": "alpha.beta", "table_name": "items", "row_key": "r",
                   "document_json": "[]", "version": 1, "updated_at_ms": 1})
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.plugin.manifest.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
