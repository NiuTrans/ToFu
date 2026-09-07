//! Declarative plugin storage manifests with append-only version migration.
//!
//! One owner-scoped document per namespace carries the normalized manifest as
//! canonical JSON (sorted keys), so the legacy byte-equality redefinition
//! check is reproduced exactly. Validation, version ordering, and the
//! append-only walk mirror `lib/storage/manifest.py` and
//! `lib/storage_sidecar/operations_pkg/_plugins.py`; every semantic rejection
//! surfaces as the typed `plugin_storage_incompatible` error.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{MAX_PLUGIN_MANIFEST_BYTES, PLUGIN_MANIFEST_DOCUMENT_NAMESPACE};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "plugin_manifests";

const COLUMN_TYPES: [&str; 7] = [
    "string",
    "integer",
    "number",
    "boolean",
    "json",
    "bytes",
    "timestamp",
];
const ACTIONS: [&str; 6] = ["get", "list", "put", "delete", "batch", "legacy_scan"];

#[derive(Clone, Copy, Debug)]
pub(crate) struct PluginIncompatible;

impl fmt::Display for PluginIncompatible {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("plugin storage manifest is incompatible")
    }
}

impl std::error::Error for PluginIncompatible {}

fn incompatible() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, PluginIncompatible)
}

pub(crate) fn is_incompatible(error: &io::Error) -> bool {
    error
        .get_ref()
        .is_some_and(|inner| inner.is::<PluginIncompatible>())
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn resource_exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn python_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(value) => !value,
        Value::Number(value) => value.as_f64() == Some(0.0),
        Value::String(value) => value.is_empty(),
        Value::Array(value) => value.is_empty(),
        Value::Object(value) => value.is_empty(),
    }
}

fn truthy(value: Option<&Value>) -> bool {
    value.is_some_and(|value| !python_falsy(value))
}

fn absent() -> &'static Value {
    &Value::Null
}

// Legacy `_NAME`: ^[a-z][a-z0-9_]{0,62}$ — every accepted name is ASCII, so
// byte length equals the regex's code-point length.
fn valid_name(value: &Value) -> Option<String> {
    let text = value.as_str()?;
    let bytes = text.as_bytes();
    if bytes.is_empty() || bytes.len() > 63 || !bytes[0].is_ascii_lowercase() {
        return None;
    }
    if !bytes[1..]
        .iter()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_')
    {
        return None;
    }
    Some(text.to_owned())
}

// Legacy `_NAMESPACE`: ^[a-z][a-z0-9_.-]{2,127}$ — 3..=128 code points total.
fn valid_namespace(value: &Value) -> Option<String> {
    let text = value.as_str()?;
    let bytes = text.as_bytes();
    if !(3..=128).contains(&bytes.len()) || !bytes[0].is_ascii_lowercase() {
        return None;
    }
    if !bytes[1..].iter().all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(*byte, b'_' | b'.' | b'-')
    }) {
        return None;
    }
    Some(text.to_owned())
}

fn required_array<'a>(object: &'a Map<String, Value>, field: &str) -> io::Result<&'a Vec<Value>> {
    object
        .get(field)
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(incompatible)
}

fn validate_table(raw: &Value) -> io::Result<Value> {
    let raw = raw.as_object().ok_or_else(incompatible)?;
    let table_name = valid_name(raw.get("name").unwrap_or(absent())).ok_or_else(incompatible)?;
    let columns_in = required_array(raw, "columns")?;
    let mut columns = Vec::with_capacity(columns_in.len());
    let mut column_names: HashSet<String> = HashSet::new();
    for raw_column in columns_in {
        let raw_column = raw_column.as_object().ok_or_else(incompatible)?;
        let column_name =
            valid_name(raw_column.get("name").unwrap_or(absent())).ok_or_else(incompatible)?;
        let column_type = raw_column.get("type").and_then(Value::as_str).unwrap_or("");
        if column_names.contains(&column_name) || !COLUMN_TYPES.contains(&column_type) {
            return Err(incompatible());
        }
        column_names.insert(column_name.clone());
        columns.push(json!({
            "name": column_name,
            "type": column_type,
            "required": truthy(raw_column.get("required")),
        }));
    }
    let primary_key_in = raw
        .get("primary_key")
        .and_then(Value::as_array)
        .filter(|key| key.len() == 1)
        .ok_or_else(incompatible)?;
    let primary_key = valid_name(&primary_key_in[0]).ok_or_else(incompatible)?;
    if !column_names.contains(&primary_key) {
        return Err(incompatible());
    }
    let mut indexes = Vec::new();
    match raw.get("indexes") {
        None => {}
        Some(value) if python_falsy(value) => {}
        Some(Value::Array(raw_indexes)) => {
            for raw_index in raw_indexes {
                let raw_index = raw_index.as_object().ok_or_else(incompatible)?;
                let index_name = valid_name(raw_index.get("name").unwrap_or(absent()))
                    .ok_or_else(incompatible)?;
                let index_columns_in = raw_index
                    .get("columns")
                    .and_then(Value::as_array)
                    .filter(|items| !items.is_empty())
                    .ok_or_else(incompatible)?;
                let mut index_columns = Vec::with_capacity(index_columns_in.len());
                for column in index_columns_in {
                    let column = column
                        .as_str()
                        .filter(|column| column_names.contains(*column))
                        .ok_or_else(incompatible)?;
                    index_columns.push(Value::from(column));
                }
                indexes.push(json!({
                    "name": index_name,
                    "columns": index_columns,
                    "unique": truthy(raw_index.get("unique")),
                }));
            }
        }
        Some(_) => return Err(incompatible()),
    }
    Ok(json!({
        "name": table_name,
        "columns": columns,
        "primary_key": [primary_key],
        "indexes": indexes,
    }))
}

fn validate_operation(raw: &Value, table_names: &HashSet<String>) -> io::Result<Value> {
    let raw = raw.as_object().ok_or_else(incompatible)?;
    let name = valid_name(raw.get("name").unwrap_or(absent())).ok_or_else(incompatible)?;
    let action = raw.get("action").and_then(Value::as_str).unwrap_or("");
    let table_name = valid_name(raw.get("table").unwrap_or(absent())).ok_or_else(incompatible)?;
    if !ACTIONS.contains(&action) || !table_names.contains(&table_name) {
        return Err(incompatible());
    }
    let expected_kind = if matches!(action, "get" | "list" | "legacy_scan") {
        "query"
    } else {
        "command"
    };
    match raw.get("kind") {
        None => {}
        Some(value) if python_falsy(value) => {}
        Some(Value::String(value)) if value == expected_kind => {}
        Some(_) => return Err(incompatible()),
    }
    // Python accepts bool as int: True passes 1 <= limit_max <= 1000 and is
    // retained verbatim in the normalized manifest; False fails the range.
    let limit_max = match raw.get("limit_max") {
        None => Value::from(100_u64),
        Some(Value::Bool(true)) => Value::Bool(true),
        Some(value @ Value::Number(_)) => {
            value
                .as_u64()
                .filter(|limit| (1..=1000).contains(limit))
                .ok_or_else(incompatible)?;
            value.clone()
        }
        Some(_) => return Err(incompatible()),
    };
    let mut operation = Map::new();
    operation.insert("name".to_owned(), Value::from(name));
    operation.insert("kind".to_owned(), Value::from(expected_kind));
    operation.insert("action".to_owned(), Value::from(action));
    operation.insert("table".to_owned(), Value::from(table_name));
    operation.insert("limit_max".to_owned(), limit_max);
    if action == "legacy_scan" {
        let legacy_table =
            valid_name(raw.get("legacy_table").unwrap_or(absent())).ok_or_else(incompatible)?;
        let legacy_columns_in = required_array(raw, "legacy_columns")?;
        let mut legacy_columns = Vec::with_capacity(legacy_columns_in.len());
        for column in legacy_columns_in {
            legacy_columns.push(valid_name(column).ok_or_else(incompatible)?);
        }
        if legacy_columns.iter().collect::<HashSet<_>>().len() != legacy_columns.len() {
            return Err(incompatible());
        }
        let legacy_order_by = match raw.get("legacy_order_by") {
            None => Vec::new(),
            Some(value) if python_falsy(value) => Vec::new(),
            Some(Value::Array(items)) => {
                let mut order = Vec::with_capacity(items.len());
                for column in items {
                    let column = column
                        .as_str()
                        .filter(|column| {
                            legacy_columns.iter().any(|known| known.as_str() == *column)
                        })
                        .ok_or_else(incompatible)?;
                    order.push(Value::from(column));
                }
                order
            }
            Some(_) => return Err(incompatible()),
        };
        operation.insert("legacy_table".to_owned(), Value::from(legacy_table));
        operation.insert(
            "legacy_columns".to_owned(),
            Value::Array(legacy_columns.into_iter().map(Value::from).collect()),
        );
        operation.insert("legacy_order_by".to_owned(), Value::Array(legacy_order_by));
    }
    Ok(Value::Object(operation))
}

pub(crate) fn validate_manifest(value: &Value) -> io::Result<Value> {
    let object = value.as_object().ok_or_else(incompatible)?;
    let namespace =
        valid_namespace(object.get("namespace").unwrap_or(absent())).ok_or_else(incompatible)?;
    // Python requires an arbitrary-precision non-bool int >= 1; serde_json
    // integers beyond u64 parse as f64 and fall into the rejection window.
    let version = object
        .get("version")
        .and_then(Value::as_u64)
        .filter(|version| *version >= 1)
        .ok_or_else(incompatible)?;
    let tables_in = required_array(object, "tables")?;
    let operations_in = required_array(object, "operations")?;
    let mut tables = Vec::with_capacity(tables_in.len());
    let mut table_names: HashSet<String> = HashSet::new();
    for raw_table in tables_in {
        let table = validate_table(raw_table)?;
        let name = table["name"]
            .as_str()
            .expect("validated table name")
            .to_owned();
        if !table_names.insert(name) {
            return Err(incompatible());
        }
        tables.push(table);
    }
    let mut operations = Vec::with_capacity(operations_in.len());
    let mut operation_names: HashSet<String> = HashSet::new();
    for raw_operation in operations_in {
        let operation = validate_operation(raw_operation, &table_names)?;
        let name = operation["name"]
            .as_str()
            .expect("validated operation name")
            .to_owned();
        if !operation_names.insert(name) {
            return Err(incompatible());
        }
        operations.push(operation);
    }
    Ok(json!({
        "namespace": namespace,
        "version": version,
        "tables": tables,
        "operations": operations,
    }))
}

// Python equality for JSON values: bool coerces to int (True == 1), and int
// equals float numerically (1 == 1.0). Only manifests with the legacy
// `limit_max: true` quirk ever exercise the coercion arms.
fn python_value_eq(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::Bool(left), Value::Bool(right)) => left == right,
        (Value::Number(left), Value::Number(right)) => left.as_f64() == right.as_f64(),
        (Value::Bool(left), Value::Number(right)) | (Value::Number(right), Value::Bool(left)) => {
            right.as_f64() == Some(if *left { 1.0 } else { 0.0 })
        }
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right.iter())
                    .all(|(left, right)| python_value_eq(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .is_some_and(|other| python_value_eq(value, other))
                })
        }
        _ => false,
    }
}

fn named_items<'a>(value: &'a Value, field: &str) -> HashMap<&'a str, &'a Value> {
    let mut items = HashMap::new();
    if let Some(array) = value.get(field).and_then(Value::as_array) {
        for item in array {
            if let Some(name) = item.get("name").and_then(Value::as_str) {
                // Dict construction keeps the last duplicate, matching Python.
                items.insert(name, item);
            }
        }
    }
    items
}

fn append_only_compatible(previous: &Value, upgraded: &Value) -> bool {
    let upgraded_tables = named_items(upgraded, "tables");
    let previous_tables = previous
        .get("tables")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for table in &previous_tables {
        let Some(name) = table.get("name").and_then(Value::as_str) else {
            return false;
        };
        let Some(upgraded_table) = upgraded_tables.get(name) else {
            return false;
        };
        let empty_columns = Vec::new();
        let old_columns = table
            .get("columns")
            .and_then(Value::as_array)
            .unwrap_or(&empty_columns);
        let new_columns = upgraded_table
            .get("columns")
            .and_then(Value::as_array)
            .unwrap_or(&empty_columns);
        if new_columns.len() < old_columns.len()
            || !old_columns
                .iter()
                .zip(new_columns.iter())
                .all(|(old, new)| python_value_eq(old, new))
        {
            return false;
        }
        if !python_value_eq(
            upgraded_table.get("primary_key").unwrap_or(absent()),
            table.get("primary_key").unwrap_or(absent()),
        ) {
            return false;
        }
        let old_indexes = named_items(table, "indexes");
        let new_indexes = named_items(upgraded_table, "indexes");
        for (index_name, definition) in &old_indexes {
            match new_indexes.get(index_name) {
                Some(upgraded_index) if python_value_eq(upgraded_index, definition) => {}
                _ => return false,
            }
        }
        if new_columns[old_columns.len()..]
            .iter()
            .any(|column| truthy(column.get("required")))
        {
            return false;
        }
        let empty_indexes = Vec::new();
        if upgraded_table
            .get("indexes")
            .and_then(Value::as_array)
            .unwrap_or(&empty_indexes)
            .iter()
            .any(|index| {
                truthy(index.get("unique"))
                    && index
                        .get("name")
                        .and_then(Value::as_str)
                        .is_some_and(|name| !old_indexes.contains_key(name))
            })
        {
            return false;
        }
    }
    let upgraded_operations = named_items(upgraded, "operations");
    let previous_operations = previous
        .get("operations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for operation in &previous_operations {
        let Some(name) = operation.get("name").and_then(Value::as_str) else {
            return false;
        };
        match upgraded_operations.get(name) {
            Some(upgraded_operation) if python_value_eq(upgraded_operation, operation) => {}
            _ => return false,
        }
    }
    true
}

fn document_key(transaction: &AuthorityTransaction, namespace: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PLUGIN_MANIFEST_DOCUMENT_NAMESPACE,
        namespace.as_bytes(),
    )
}

fn read_manifest(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<Option<Vec<u8>>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &document_key(transaction, namespace)?,
        LOGICAL_NAMESPACE,
        namespace,
        transaction.owner_user_id(),
        MAX_PLUGIN_MANIFEST_BYTES,
    )
}

pub(crate) fn register(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    manifest_json: &[u8],
    now_ms: u64,
) -> io::Result<Vec<u8>> {
    let value: Value = serde_json::from_slice(manifest_json)
        .map_err(|_| invalid_data("plugin manifest step is malformed"))?;
    let manifest = validate_manifest(&value)?;
    let canonical = serde_json::to_vec(&manifest)
        .map_err(|_| invalid_data("plugin manifest cannot be encoded"))?;
    if canonical.len() > MAX_PLUGIN_MANIFEST_BYTES {
        return Err(resource_exhausted("plugin manifest exceeds its bound"));
    }
    let namespace = manifest["namespace"]
        .as_str()
        .expect("validated namespace")
        .to_owned();
    let version = manifest["version"].as_u64().expect("validated version");
    if let Some(current_bytes) = read_manifest(database, transaction, &namespace)? {
        let current: Value = serde_json::from_slice(&current_bytes)
            .map_err(|_| invalid_data("stored plugin manifest is malformed"))?;
        let current_version = current
            .get("version")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("stored plugin manifest version is malformed"))?;
        if version < current_version {
            return Err(incompatible());
        }
        if version == current_version && current_bytes != canonical {
            return Err(incompatible());
        }
        if version > current_version && !append_only_compatible(&current, &manifest) {
            return Err(incompatible());
        }
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, &namespace)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: namespace.clone(),
            value_json: canonical,
            expected_version: None,
            updated_at_ms: now_ms,
        },
        transaction.owner_user_id(),
        MAX_PLUGIN_MANIFEST_BYTES,
    )?;
    serde_json::to_vec(&json!({"namespace": namespace, "version": version}))
        .map_err(|_| invalid_data("plugin register response cannot be encoded"))
}

pub(crate) fn manifest_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<Vec<u8>> {
    // The legacy str() coercion maps a missing/empty namespace to a lookup
    // that can never hit; short-circuit it to the same miss.
    if namespace.is_empty() {
        return Ok(b"null".to_vec());
    }
    Ok(read_manifest(database, transaction, namespace)?.unwrap_or_else(|| b"null".to_vec()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table(name: &str, columns: Vec<Value>, primary_key: &str, indexes: Vec<Value>) -> Value {
        json!({
            "name": name,
            "columns": columns,
            "primary_key": [primary_key],
            "indexes": indexes,
        })
    }

    fn column(name: &str, column_type: &str) -> Value {
        json!({"name": name, "type": column_type})
    }

    fn operation(name: &str, action: &str, table: &str) -> Value {
        json!({"name": name, "action": action, "table": table})
    }

    fn manifest(tables: Vec<Value>, operations: Vec<Value>) -> Value {
        json!({
            "namespace": "alpha.beta",
            "version": 1,
            "tables": tables,
            "operations": operations,
        })
    }

    fn base_manifest() -> Value {
        manifest(
            vec![table(
                "items",
                vec![column("id", "string"), column("note", "string")],
                "id",
                vec![json!({"name": "by_note", "columns": ["note"]})],
            )],
            vec![operation("fetch", "get", "items")],
        )
    }

    #[test]
    fn validation_normalizes_defaults_and_truthiness() {
        let normalized = validate_manifest(&base_manifest()).unwrap();
        assert_eq!(normalized["namespace"], "alpha.beta");
        assert_eq!(normalized["version"], 1);
        let first_table = &normalized["tables"][0];
        assert_eq!(first_table["columns"][0]["required"], false);
        assert_eq!(first_table["indexes"][0]["unique"], false);
        let operation = &normalized["operations"][0];
        assert_eq!(operation["kind"], "query");
        assert_eq!(operation["limit_max"], 100);
        assert!(operation.get("legacy_table").is_none());

        let coerced_table = table(
            "items",
            vec![json!({"name": "id", "type": "string", "required": "yes"})],
            "id",
            vec![json!({"name": "by_id", "columns": ["id"], "unique": 1})],
        );
        let coerced = validate_manifest(&json!({
            "namespace": "alpha.beta",
            "version": 2,
            "tables": [(coerced_table)],
            "operations": [
                {"name": "store", "action": "put", "table": "items", "kind": 0,
                 "limit_max": true},
            ],
        }))
        .unwrap();
        assert_eq!(coerced["tables"][0]["columns"][0]["required"], true);
        assert_eq!(coerced["tables"][0]["indexes"][0]["unique"], true);
        assert_eq!(coerced["operations"][0]["kind"], "command");
        assert_eq!(coerced["operations"][0]["limit_max"], Value::Bool(true));
    }

    #[test]
    fn validation_rejects_every_legacy_failure_shape() {
        let rejected = vec![
            Value::Null,
            json!("manifest"),
            json!({"version": 1, "tables": [1], "operations": [1]}),
            manifest(vec![], vec![operation("fetch", "get", "items")]),
            manifest(
                vec![table("items", vec![column("id", "string")], "id", vec![])],
                vec![],
            ),
        ];
        for value in rejected {
            assert!(is_incompatible(&validate_manifest(&value).unwrap_err()));
        }
        let mut bad_namespace = base_manifest();
        for namespace in ["AB", "a", "ab", "1abc", "-abc", "a b", &"a".repeat(129)] {
            bad_namespace["namespace"] = Value::from(namespace);
            assert!(validate_manifest(&bad_namespace).is_err(), "{namespace}");
        }
        for namespace in ["abc", &"a".repeat(128), "a.b-c_d"] {
            bad_namespace["namespace"] = Value::from(namespace);
            assert!(validate_manifest(&bad_namespace).is_ok(), "{namespace}");
        }
        let mut bad_version = base_manifest();
        for version in [json!(0), json!(-1), json!(1.0), json!(true), json!("1")] {
            bad_version["version"] = version;
            assert!(validate_manifest(&bad_version).is_err());
        }
        let mut bad_limit = base_manifest();
        for limit in [json!(0), json!(1001), json!(1.5), json!(false), json!("5")] {
            bad_limit["operations"][0]["limit_max"] = limit;
            assert!(validate_manifest(&bad_limit).is_err());
        }
        bad_limit["operations"][0]["limit_max"] = json!(1000);
        assert!(validate_manifest(&bad_limit).is_ok());
        let mut kind_mismatch = base_manifest();
        kind_mismatch["operations"][0]["kind"] = json!("command");
        assert!(validate_manifest(&kind_mismatch).is_err());
        let mut bad_table_ref = base_manifest();
        bad_table_ref["operations"][0]["table"] = json!("unknown");
        assert!(validate_manifest(&bad_table_ref).is_err());
        let mut bad_column_type = base_manifest();
        bad_column_type["tables"][0]["columns"][0]["type"] = json!("text");
        assert!(validate_manifest(&bad_column_type).is_err());
        let mut bad_primary_key = base_manifest();
        bad_primary_key["tables"][0]["primary_key"] = json!(["note"]);
        // 'note' is a declared column, so this is accepted…
        assert!(validate_manifest(&bad_primary_key).is_ok());
        bad_primary_key["tables"][0]["primary_key"] = json!(["missing"]);
        assert!(validate_manifest(&bad_primary_key).is_err());
        bad_primary_key["tables"][0]["primary_key"] = json!([]);
        assert!(validate_manifest(&bad_primary_key).is_err());
        let mut bad_index = base_manifest();
        bad_index["tables"][0]["indexes"] = json!([{"name": "bad", "columns": ["missing"]}]);
        assert!(validate_manifest(&bad_index).is_err());
        bad_index["tables"][0]["indexes"] = json!("x");
        assert!(validate_manifest(&bad_index).is_err());
        bad_index["tables"][0]["indexes"] = json!(0);
        assert!(validate_manifest(&bad_index).is_ok());
        let mut legacy_scan = base_manifest();
        legacy_scan["operations"] = json!([{
            "name": "scan", "action": "legacy_scan", "table": "items",
            "legacy_table": "legacy_items", "legacy_columns": ["id", "note"],
        }]);
        let normalized = validate_manifest(&legacy_scan).unwrap();
        assert_eq!(normalized["operations"][0]["legacy_order_by"], json!([]));
        legacy_scan["operations"][0]["legacy_order_by"] = json!(["id"]);
        assert!(validate_manifest(&legacy_scan).is_ok());
        legacy_scan["operations"][0]["legacy_order_by"] = json!(["missing"]);
        assert!(validate_manifest(&legacy_scan).is_err());
        legacy_scan["operations"][0]["legacy_columns"] = json!(["id", "id"]);
        assert!(validate_manifest(&legacy_scan).is_err());
    }

    #[test]
    fn python_value_equality_coerces_bool_and_float_like_python() {
        assert!(python_value_eq(&json!(true), &json!(1)));
        assert!(python_value_eq(&json!(1), &json!(1.0)));
        assert!(python_value_eq(&json!(false), &json!(0)));
        assert!(!python_value_eq(&json!(true), &json!(2)));
        assert!(python_value_eq(
            &json!({"a": [1, {"b": true}]}),
            &json!({"a": [1.0, {"b": 1}]}),
        ));
        assert!(!python_value_eq(&json!({"a": 1}), &json!({"a": 1, "b": 2})));
    }

    #[test]
    fn append_only_walk_accepts_growth_and_rejects_redefinition() {
        let previous = validate_manifest(&base_manifest()).unwrap();
        let build = |version: u64, tables: Vec<Value>, operations: Vec<Value>| {
            json!({
                "namespace": "alpha.beta",
                "version": version,
                "tables": tables,
                "operations": operations,
            })
        };
        let base_items_table = || {
            table(
                "items",
                vec![column("id", "string"), column("note", "string")],
                "id",
                vec![json!({"name": "by_note", "columns": ["note"]})],
            )
        };
        // Growth: new optional column, new non-unique index, new table, new op.
        let upgraded = validate_manifest(&build(
            2,
            vec![
                table(
                    "items",
                    vec![
                        column("id", "string"),
                        column("note", "string"),
                        column("extra", "json"),
                    ],
                    "id",
                    vec![
                        json!({"name": "by_note", "columns": ["note"]}),
                        json!({"name": "by_extra", "columns": ["extra"]}),
                    ],
                ),
                table("logs", vec![column("id", "string")], "id", vec![]),
            ],
            vec![
                operation("fetch", "get", "items"),
                operation("listing", "list", "items"),
            ],
        ))
        .unwrap();
        assert!(append_only_compatible(&previous, &upgraded));

        let reject = |mutated: Value| {
            let upgraded = validate_manifest(&mutated).unwrap();
            assert!(!append_only_compatible(&previous, &upgraded));
        };
        // Drop a column from the existing prefix.
        reject(build(
            2,
            vec![table("items", vec![column("id", "string")], "id", vec![])],
            vec![operation("fetch", "get", "items")],
        ));
        // Change the primary key.
        reject(build(
            2,
            vec![table(
                "items",
                vec![column("id", "string"), column("note", "string")],
                "note",
                vec![json!({"name": "by_note", "columns": ["note"]})],
            )],
            vec![operation("fetch", "get", "items")],
        ));
        // Require a new column.
        reject(build(
            2,
            vec![table(
                "items",
                vec![
                    column("id", "string"),
                    column("note", "string"),
                    json!({"name": "extra", "type": "string", "required": true}),
                ],
                "id",
                vec![json!({"name": "by_note", "columns": ["note"]})],
            )],
            vec![operation("fetch", "get", "items")],
        ));
        // Add a unique index.
        reject(build(
            2,
            vec![table(
                "items",
                vec![column("id", "string"), column("note", "string")],
                "id",
                vec![
                    json!({"name": "by_note", "columns": ["note"]}),
                    json!({"name": "by_id", "columns": ["id"], "unique": true}),
                ],
            )],
            vec![operation("fetch", "get", "items")],
        ));
        // Change an operation.
        reject(build(
            2,
            vec![base_items_table()],
            vec![json!({"name": "fetch", "action": "get", "table": "items",
                        "limit_max": 50})],
        ));
        // Drop an operation against a two-op base.
        let two_op_previous = validate_manifest(&build(
            1,
            vec![table("items", vec![column("id", "string")], "id", vec![])],
            vec![
                operation("fetch", "get", "items"),
                operation("store", "put", "items"),
            ],
        ))
        .unwrap();
        let dropped = validate_manifest(&build(
            2,
            vec![table("items", vec![column("id", "string")], "id", vec![])],
            vec![operation("fetch", "get", "items")],
        ))
        .unwrap();
        assert!(!append_only_compatible(&two_op_previous, &dropped));
        // The limit_max bool/int quirk stays equal under Python coercion.
        let bool_previous = validate_manifest(&build(
            1,
            vec![table("items", vec![column("id", "string")], "id", vec![])],
            vec![json!({"name": "store", "action": "put", "table": "items",
                        "limit_max": true})],
        ))
        .unwrap();
        let int_upgraded = validate_manifest(&build(
            2,
            vec![table("items", vec![column("id", "string")], "id", vec![])],
            vec![json!({"name": "store", "action": "put", "table": "items",
                        "limit_max": 1})],
        ))
        .unwrap();
        assert!(append_only_compatible(&bool_previous, &int_upgraded));
    }
}
