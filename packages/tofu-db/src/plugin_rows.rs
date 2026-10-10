//! Manifest-declared plugin row operations (`plugin.<namespace>.<operation>`).
//!
//! One-to-one port of the legacy `_plugin_dynamic` dispatch
//! (`operations_pkg/_plugins.py`): the registered manifest is the only
//! authority for tables, columns, primary keys, unique indexes and operation
//! kinds; every action is evaluated against that declaration — plugin code
//! never supplies an expression. Row/unique-claim physical layout is shared
//! with the certified migration lane (`plugin_migration.rs`), so rows written
//! by either path are indistinguishable.
//!
//! `legacy_scan` is the one deliberate constant: it reads pre-authority SQL
//! tables, and no such table can exist inside a native authority, so the
//! exact legacy answer for an absent legacy table is returned.

use std::io;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PLUGIN_NAMESPACE_CHARACTERS, PLUGIN_ROW_DOCUMENT_NAMESPACE,
};
use crate::plugin_migration::{row_composite, row_document_key, unique_claim_key, unique_composite};
use crate::versioned_document::{self, PutRequest};

const MAX_OPERATION_NAME_CHARACTERS: usize = 63;
const MAX_LIST_CURSOR_CHARACTERS: usize = 512;
const MAX_LIST_PAGE_ROWS: usize = 1000;
const ENVELOPE_OVERHEAD_BYTES: usize = 4096;

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

#[derive(Clone, Debug)]
pub(crate) struct DynRequest {
    pub namespace: String,
    pub operation: String,
    pub command: bool,
    pub payload: Map<String, Value>,
    pub updated_at_ms: u64,
}

impl DynRequest {
    pub(crate) fn validate(&self) -> io::Result<usize> {
        if self.namespace.is_empty()
            || self.namespace.chars().count() > MAX_PLUGIN_NAMESPACE_CHARACTERS
            || self.operation.is_empty()
            || self.operation.chars().count() > MAX_OPERATION_NAME_CHARACTERS
            || self.updated_at_ms == 0
        {
            return Err(invalid_input("invalid plugin row operation step"));
        }
        serde_json::to_vec(&self.payload)
            .map(|bytes| bytes.len())
            .map_err(|_| invalid_input("plugin row operation payload cannot be encoded"))
    }
}

fn text_field<'a>(object: &'a Map<String, Value>, field: &str) -> io::Result<&'a str> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("plugin manifest field is malformed"))
}

fn required_payload_text<'a>(
    payload: &'a Map<String, Value>,
    field: &str,
    maximum: usize,
) -> io::Result<&'a str> {
    match payload.get(field) {
        Some(Value::String(value)) if !value.is_empty() && value.chars().count() <= maximum => {
            Ok(value)
        }
        _ => Err(invalid_input("plugin operation payload is invalid")),
    }
}

fn expected_version(payload: &Map<String, Value>) -> io::Result<Option<u64>> {
    match payload.get("expected_version") {
        None => Ok(None),
        Some(Value::Number(number)) => number
            .as_u64()
            .map(Some)
            .ok_or_else(|| invalid_input("plugin expected version is invalid")),
        _ => Err(invalid_input("plugin expected version is invalid")),
    }
}

/// Python `str(key)` for manifest primary keys: text or integer, never bool.
fn key_string(value: Option<&Value>) -> io::Result<String> {
    match value {
        Some(Value::String(value)) => Ok(value.clone()),
        Some(Value::Number(number)) if number.is_i64() || number.is_u64() => {
            Ok(number.to_string())
        }
        _ => Err(crate::plugin::incompatible()),
    }
}

/// Port of `manifest.validate_document`: exact declared-column projection,
/// required fields, and per-column Python type semantics.
fn validate_document(table: &Map<String, Value>, value: &Value) -> io::Result<Map<String, Value>> {
    let document = value
        .as_object()
        .ok_or_else(crate::plugin::incompatible)?;
    let columns = table
        .get("columns")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_data("plugin manifest table is malformed"))?;
    let mut declared = std::collections::BTreeMap::<String, (String, bool)>::new();
    for column in columns {
        let column = column
            .as_object()
            .ok_or_else(|| invalid_data("plugin manifest column is malformed"))?;
        declared.insert(
            text_field(column, "name")?.to_owned(),
            (
                text_field(column, "type")?.to_owned(),
                column
                    .get("required")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
        );
    }
    if document.keys().any(|field| !declared.contains_key(field)) {
        return Err(crate::plugin::incompatible());
    }
    for (name, item) in document {
        let (expected, _) = declared.get(name).expect("declared field checked");
        // The legacy validator skips null values entirely — a present null
        // satisfies even a required column's type check.
        if item.is_null() {
            continue;
        }
        let valid = match expected.as_str() {
            "string" => item.is_string(),
            "integer" => item.as_i64().is_some() || item.as_u64().is_some(),
            "number" => item.is_number(),
            "boolean" => item.is_boolean(),
            "json" => true,
            "bytes" => item.is_string(),
            "timestamp" => item.is_number() || item.is_string(),
            _ => return Err(invalid_data("plugin manifest column type is malformed")),
        };
        if !valid {
            return Err(crate::plugin::incompatible());
        }
    }
    for (name, (_, required)) in &declared {
        if *required && !document.contains_key(name) {
            return Err(crate::plugin::incompatible());
        }
    }
    Ok(document.clone())
}

fn canonical(value: &Value) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data("plugin value cannot be encoded"))
}

fn unique_value_of(document: &Map<String, Value>, columns: &[&str]) -> Option<String> {
    let values: Vec<Value> = columns
        .iter()
        .map(|column| document.get(*column).cloned().unwrap_or(Value::Null))
        .collect();
    if values.iter().any(Value::is_null) {
        return None;
    }
    let encoded = serde_json::to_vec(&Value::Array(values)).ok()?;
    let mut hex = String::with_capacity(64);
    for byte in Sha256::digest(&encoded) {
        use std::fmt::Write as _;
        write!(&mut hex, "{byte:02x}").ok()?;
    }
    Some(hex)
}

fn unique_indexes_of<'a>(table: &'a Map<String, Value>) -> Vec<(String, Vec<&'a str>)> {
    table
        .get("indexes")
        .and_then(Value::as_array)
        .map(|indexes| {
            indexes
                .iter()
                .filter_map(Value::as_object)
                .filter(|index| {
                    index.get("unique").and_then(Value::as_bool) == Some(true)
                })
                .filter_map(|index| {
                    let name = index.get("name").and_then(Value::as_str)?.to_owned();
                    let columns = index
                        .get("columns")
                        .and_then(Value::as_array)?
                        .iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<&'a str>>();
                    Some((name, columns))
                })
                .collect()
        })
        .unwrap_or_default()
}

struct StoredRow {
    document: Map<String, Value>,
    version: u64,
    updated_at_ms: u64,
}

fn read_row(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    table: &str,
    row_key: &str,
) -> io::Result<Option<StoredRow>> {
    let Some(raw) = versioned_document::get_with_blob_owner_bounded(
        database,
        transaction,
        &row_document_key(transaction, namespace, table, row_key)?,
        PLUGIN_ROW_DOCUMENT_NAMESPACE,
        &format!("{namespace}:{table}:{row_key}"),
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_PLUGIN_ROW_DOCUMENT_BYTES + ENVELOPE_OVERHEAD_BYTES,
    )?
    else {
        return Ok(None);
    };
    let envelope: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("plugin row envelope is malformed"))?;
    let document = envelope
        .get("value")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("plugin row document is malformed"))?;
    let version = envelope
        .get("version")
        .and_then(Value::as_u64)
        .filter(|version| *version > 0)
        .ok_or_else(|| invalid_data("plugin row version is malformed"))?;
    let updated_at_ms = envelope
        .get("updated_at_ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("plugin row timestamp is malformed"))?;
    Ok(Some(StoredRow {
        document,
        version,
        updated_at_ms,
    }))
}

fn row_response(stored: &StoredRow) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&Value::Object(
        json_map(&[
            ("document", Value::Object(stored.document.clone())),
            ("version", Value::from(stored.version)),
            ("updated_at_ms", Value::from(stored.updated_at_ms)),
        ]),
    ))
    .map_err(|_| invalid_data("plugin row response cannot be encoded"))
}

fn json_map(fields: &[(&str, Value)]) -> Map<String, Value> {
    fields
        .iter()
        .map(|(name, value)| (name.to_string(), value.clone()))
        .collect()
}

/// Delete the row's current unique claims (computed from its stored
/// document, so claims always track the document that made them), then
/// insert the claims of the replacement document when supplied.
fn replace_unique_claims(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    table: &str,
    table_spec: &Map<String, Value>,
    row_key: &str,
    old_document: Option<&Map<String, Value>>,
    new_document: Option<&Map<String, Value>>,
) -> io::Result<()> {
    let indexes = unique_indexes_of(table_spec);
    for (index_name, columns) in &indexes {
        if let Some(old) = old_document {
            if let Some(old_value) = unique_value_of(old, columns) {
                database.entity_delete(
                    transaction,
                    unique_claim_key(transaction, namespace, table, index_name, &old_value)?,
                )?;
            }
        }
    }
    if let Some(new) = new_document {
        for (index_name, columns) in &indexes {
            let Some(new_value) = unique_value_of(new, columns) else {
                continue;
            };
            let claim_key =
                unique_claim_key(transaction, namespace, table, index_name, &new_value)?;
            if let Some(owner) = database.entity_get(transaction, &claim_key)? {
                let owner_key = std::str::from_utf8(&owner)
                    .map_err(|_| invalid_data("plugin unique claim owner is malformed"))?;
                if owner_key != row_key {
                    return Err(conflict("Plugin unique constraint conflict"));
                }
            }
            database.entity_put(
                transaction,
                claim_key,
                row_key.as_bytes().to_vec(),
            )?;
        }
    }
    Ok(())
}

fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    table: &str,
    table_spec: &Map<String, Value>,
    payload: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let primary_key = text_field(table_spec, "primary_key_name")?;
    let document = validate_document(
        table_spec,
        payload.get("document").unwrap_or(&Value::Null),
    )?;
    let key = key_string(document.get(primary_key))?;
    if key.is_empty() {
        return Err(crate::plugin::incompatible());
    }
    let current = read_row(database, transaction, namespace, table, &key)?;
    let actual = current.as_ref().map_or(0, |row| row.version);
    if let Some(expected) = expected_version(payload)? {
        if expected != actual {
            return Err(conflict("Plugin row version conflict"));
        }
    }
    let version = actual + 1;
    replace_unique_claims(
        database,
        transaction,
        namespace,
        table,
        table_spec,
        &key,
        current.as_ref().map(|row| &row.document),
        Some(&document),
    )?;
    versioned_document::put(
        database,
        transaction,
        PutRequest {
            key: row_document_key(transaction, namespace, table, &key)?,
            namespace: PLUGIN_ROW_DOCUMENT_NAMESPACE.to_owned(),
            logical_key: format!("{namespace}:{table}:{key}"),
            value_json: canonical(&Value::Object(document))?,
            expected_version: None,
            updated_at_ms,
        },
    )?;
    serde_json::to_vec(&Value::Object(json_map(&[
        ("key", Value::String(key)),
        ("version", Value::from(version)),
        ("updated_at_ms", Value::from(updated_at_ms)),
    ])))
    .map_err(|_| invalid_data("plugin put response cannot be encoded"))
}

fn delete_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    table: &str,
    table_spec: &Map<String, Value>,
    row_key: &str,
    payload: &Map<String, Value>,
) -> io::Result<Vec<u8>> {
    let current = read_row(database, transaction, namespace, table, row_key)?;
    if let Some(expected) = expected_version(payload)? {
        let actual = current.as_ref().map_or(0, |row| row.version);
        if expected != actual {
            return Err(conflict("Plugin row version conflict"));
        }
    }
    replace_unique_claims(
        database,
        transaction,
        namespace,
        table,
        table_spec,
        row_key,
        current.as_ref().map(|row| &row.document),
        None,
    )?;
    let deleted = current.is_some();
    if deleted {
        versioned_document::delete(
            database,
            transaction,
            row_document_key(transaction, namespace, table, row_key)?,
            PLUGIN_ROW_DOCUMENT_NAMESPACE,
            &format!("{namespace}:{table}:{row_key}"),
            None,
        )?;
    }
    serde_json::to_vec(&Value::Object(json_map(&[
        ("key", Value::String(row_key.to_owned())),
        ("deleted", Value::Bool(deleted)),
    ])))
    .map_err(|_| invalid_data("plugin delete response cannot be encoded"))
}

#[allow(clippy::too_many_arguments)]
fn list_rows(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    table: &str,
    table_spec: &Map<String, Value>,
    payload: &Map<String, Value>,
    limit_max: u64,
) -> io::Result<Vec<u8>> {
    let limit = match payload.get("limit") {
        None if limit_max >= 100 => 100_usize,
        None => return Err(invalid_input("plugin list limit is invalid")),
        Some(Value::Number(number)) => number
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| invalid_input("plugin list limit is invalid"))?,
        _ => return Err(invalid_input("plugin list limit is invalid")),
    };
    // The legacy `_integer` check bounds the *resolved* value: even the
    // default 100 fails when the operation's limit_max is smaller.
    if !(1..=limit_max as usize).contains(&limit) {
        return Err(invalid_input("plugin list limit is invalid"));
    }
    let cursor_text = |field: &str| -> io::Result<String> {
        match payload.get(field) {
            None => Ok(String::new()),
            Some(Value::String(value)) if value.chars().count() <= MAX_LIST_CURSOR_CHARACTERS => {
                Ok(value.clone())
            }
            _ => Err(invalid_input("plugin list cursor is invalid")),
        }
    };
    let key_prefix = cursor_text("key_prefix")?;
    let after_key = cursor_text("after_key")?;
    if !key_prefix.is_empty() && !after_key.is_empty() && !after_key.starts_with(&key_prefix) {
        return Err(invalid_input("plugin list cursor is invalid"));
    }
    let filters = match payload.get("filters") {
        None => Map::new(),
        Some(Value::Object(filters)) => filters.clone(),
        _ => return Err(invalid_input("plugin filters must be an object")),
    };
    let declared: std::collections::BTreeSet<&str> = table_spec
        .get("columns")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_data("plugin manifest table is malformed"))?
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|column| column.get("name").and_then(Value::as_str))
        .collect();
    if filters.keys().any(|field| !declared.contains(field.as_str())) {
        return Err(invalid_input("plugin filter is undeclared"));
    }
    let primary_key = text_field(table_spec, "primary_key_name")?;
    // The validated query model is evaluated after a bounded key-range read;
    // no plugin expression is interpolated into the scan itself.
    let read_limit = if key_prefix.is_empty() {
        MAX_LIST_PAGE_ROWS.min((limit * 10).max(limit))
    } else {
        limit
    };
    let scan_prefix = row_composite(namespace, table, "")?;
    let cursor = if after_key.is_empty() {
        &key_prefix
    } else {
        &after_key
    };
    let mut start_raw = row_composite(namespace, table, cursor)?;
    if !cursor.is_empty() {
        // The legacy cursor predicate is strictly-greater on the row key
        // (`row_key > ?`), so step one byte past the exact cursor key.
        start_raw.push(0);
    }
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PLUGIN_ROW_DOCUMENT_NAMESPACE,
        &scan_prefix,
    )?;
    let start = if cursor.is_empty() {
        EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            PLUGIN_ROW_DOCUMENT_NAMESPACE,
            &scan_prefix,
        )?
    } else {
        EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            PLUGIN_ROW_DOCUMENT_NAMESPACE,
            &start_raw,
        )?
    };
    let rows = database.entity_scan(transaction, &start, &end, read_limit)?;
    let mut result = Vec::with_capacity(limit);
    for (_, raw) in rows {
        let (value_json, version, updated_at_ms) =
            versioned_document::project_stored_document(
                database,
                transaction.tenant_id(),
                transaction.owner_user_id(),
                &raw,
                PLUGIN_ROW_DOCUMENT_NAMESPACE,
                crate::generated_tofudb_ir::MAX_PLUGIN_ROW_DOCUMENT_BYTES
                    + ENVELOPE_OVERHEAD_BYTES,
            )?;
        let document = serde_json::from_slice::<Value>(&value_json)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("plugin row document is malformed"))?;
        if !key_prefix.is_empty() {
            let key = key_string(document.get(primary_key)).unwrap_or_default();
            if !key.starts_with(&key_prefix) {
                break;
            }
        }
        if filters
            .iter()
            .all(|(field, value)| {
                crate::plugin::python_value_eq(
                    document.get(field).unwrap_or(&Value::Null),
                    value,
                )
            })
        {
            result.push(Value::Object(json_map(&[
                ("document", Value::Object(document)),
                ("version", Value::from(version)),
                ("updated_at_ms", Value::from(updated_at_ms)),
            ])));
            if result.len() >= limit {
                break;
            }
        }
    }
    serde_json::to_vec(&Value::Array(result))
        .map_err(|_| invalid_data("plugin list response cannot be encoded"))
}

/// Port the table-spec primary key into a flat field: manifests declare
/// `primary_key: [name]`, and actions need the bare column name.
fn normalize_table_spec(table: &Map<String, Value>) -> io::Result<Map<String, Value>> {
    let mut normalized = table.clone();
    let primary_key = table
        .get("primary_key")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("plugin manifest primary key is malformed"))?
        .to_owned();
    normalized.insert("primary_key_name".to_owned(), Value::String(primary_key));
    Ok(normalized)
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &DynRequest,
) -> io::Result<Option<Vec<u8>>> {
    let Some(manifest_raw) =
        crate::plugin::read_manifest(database, transaction, &request.namespace)?
    else {
        // The legacy dispatch raises plugin_storage_incompatible for an
        // unregistered namespace.
        return Err(crate::plugin::incompatible());
    };
    let manifest: Value = serde_json::from_slice(&manifest_raw)
        .map_err(|_| invalid_data("stored plugin manifest is malformed"))?;
    let manifest = manifest.as_object().ok_or_else(|| invalid_data("stored plugin manifest is malformed"))?;
    let operations = manifest
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_data("stored plugin manifest operations are malformed"))?;
    let spec = operations
        .iter()
        .filter_map(Value::as_object)
        .find(|item| {
            item.get("name").and_then(Value::as_str) == Some(request.operation.as_str())
        })
        .ok_or_else(|| invalid_input("Unknown plugin storage operation"))?;
    let declared_command = text_field(spec, "kind")? == "command";
    if declared_command != request.command {
        return Err(invalid_input("Plugin operation kind mismatch"));
    }
    let table_name = text_field(spec, "table")?;
    let tables = manifest
        .get("tables")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_data("stored plugin manifest tables are malformed"))?;
    let table = tables
        .iter()
        .filter_map(Value::as_object)
        .find(|item| {
            item.get("name").and_then(Value::as_str) == Some(table_name)
        })
        .ok_or_else(|| invalid_data("stored plugin manifest table is missing"))?;
    let table = &normalize_table_spec(table)?;
    let namespace = request.namespace.as_str();
    match text_field(spec, "action")? {
        "get" => {
            let primary_key = text_field(table, "primary_key_name")?;
            let key = required_payload_text(&request.payload, primary_key, 512)?;
            match read_row(database, transaction, namespace, table_name, key)? {
                None => Ok(None),
                Some(stored) => row_response(&stored).map(Some),
            }
        }
        "list" => {
            let limit_max = spec
                .get("limit_max")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("stored plugin manifest limit is malformed"))?;
            list_rows(
                database,
                transaction,
                namespace,
                table_name,
                table,
                &request.payload,
                limit_max,
            )
            .map(Some)
        }
        "put" => put_document(
            database,
            transaction,
            namespace,
            table_name,
            table,
            &request.payload,
            request.updated_at_ms,
        )
        .map(Some),
        "delete" => {
            let primary_key = text_field(table, "primary_key_name")?;
            let key = required_payload_text(&request.payload, primary_key, 512)?;
            delete_document(
                database,
                transaction,
                namespace,
                table_name,
                table,
                key,
                &request.payload,
            )
            .map(Some)
        }
        "batch" => execute_batch(database, transaction, namespace, table_name, table, spec, request)
            .map(Some),
        "legacy_scan" => {
            // Payload validation matches the legacy startup-only scan before
            // the (always absent) native legacy-table answer.
            let limit_max = spec
                .get("limit_max")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("stored plugin manifest limit is malformed"))?;
            match request.payload.get("limit") {
                None => {}
                Some(Value::Number(number))
                    if number
                        .as_u64()
                        .is_some_and(|value| (1..=limit_max).contains(&value)) => {}
                _ => return Err(invalid_input("plugin legacy scan limit is invalid")),
            }
            match request.payload.get("offset") {
                None => {}
                Some(Value::Number(number))
                    if number
                        .as_u64()
                        .is_some_and(|value| value <= 10_000_000) => {}
                _ => return Err(invalid_input("plugin legacy scan offset is invalid")),
            }
            // No pre-authority SQL table can exist inside a native
            // authority; the legacy answer for an absent table is exact.
            serde_json::to_vec(&Value::Object(json_map(&[
                ("exists", Value::Bool(false)),
                ("rows", Value::Array(Vec::new())),
                ("next_offset", Value::Null),
            ])))
            .map(Some)
            .map_err(|_| invalid_data("plugin legacy scan response cannot be encoded"))
        }
        _ => Err(invalid_input("Unsupported plugin action")),
    }
}

fn execute_batch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    table: &str,
    table_spec: &Map<String, Value>,
    spec: &Map<String, Value>,
    request: &DynRequest,
) -> io::Result<Vec<u8>> {
    let limit_max = spec
        .get("limit_max")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("stored plugin manifest limit is malformed"))?;
    let mutations = match request.payload.get("mutations") {
        Some(Value::Array(mutations))
            if !mutations.is_empty() && mutations.len() <= limit_max as usize =>
        {
            mutations
        }
        _ => return Err(invalid_input("Plugin batch must contain a bounded mutation list")),
    };
    let primary_key = text_field(table_spec, "primary_key_name")?;
    let mut seen = std::collections::BTreeSet::new();
    for mutation in mutations {
        let mutation = mutation
            .as_object()
            .ok_or_else(|| invalid_input("Plugin mutation must be an object"))?;
        let key_value = match mutation.get("action").and_then(Value::as_str) {
            Some("put") => mutation.get("document").and_then(|document| {
                document.as_object().and_then(|object| object.get(primary_key))
            }),
            Some("delete") => mutation.get(primary_key),
            _ => return Err(invalid_input("Unsupported plugin batch mutation")),
        };
        let key = key_string(key_value)
            .map_err(|_| invalid_input("Plugin batch keys must be strings or integers"))?;
        if key.is_empty() || !seen.insert(key) {
            return Err(invalid_input(
                "Plugin batch keys must be non-empty and unique",
            ));
        }
    }
    let mut results = Vec::with_capacity(mutations.len());
    for mutation in mutations {
        let mutation = mutation.as_object().expect("validated batch mutation");
        let encoded = match mutation.get("action").and_then(Value::as_str) {
            Some("put") => put_document(
                database,
                transaction,
                namespace,
                table,
                table_spec,
                mutation,
                request.updated_at_ms,
            )?,
            Some("delete") => {
                let key = key_string(mutation.get(primary_key))
                    .map_err(|_| invalid_input("Plugin batch keys must be strings or integers"))?;
                delete_document(
                    database,
                    transaction,
                    namespace,
                    table,
                    table_spec,
                    &key,
                    mutation,
                )?
            }
            _ => unreachable!("validated batch action"),
        };
        results.push(
            serde_json::from_slice::<Value>(&encoded)
                .map_err(|_| invalid_data("plugin batch result cannot be encoded"))?,
        );
    }
    serde_json::to_vec(&Value::Object(json_map(&[("results", Value::Array(results))])))
        .map_err(|_| invalid_data("plugin batch response cannot be encoded"))
}
