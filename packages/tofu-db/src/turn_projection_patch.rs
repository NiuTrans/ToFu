//! Deterministic bounded patches for live Turn projection documents.
//!
//! This is the Rust authority twin of `lib/turn_projection_patch.py` and the
//! browser projection-patch runtime. It owns only pure JSON diff/application;
//! Turn revision fencing, durable patch heads, and event publication remain in
//! the Turn transaction layer.

use std::collections::BTreeSet;
use std::fmt;

use serde_json::{json, Map, Value};

use crate::generated_tofudb_ir::{
    MAX_TURN_PROJECTION_PATCH_BYTES, MAX_TURN_PROJECTION_PATCH_DEPTH,
    MAX_TURN_PROJECTION_PATCH_OPERATIONS,
};

pub(crate) const PROJECTION_PATCH_VERSION: u64 = 1;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ProjectionPatchError(&'static str);

impl fmt::Display for ProjectionPatchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for ProjectionPatchError {}

fn error(message: &'static str) -> ProjectionPatchError {
    ProjectionPatchError(message)
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum PathPart {
    Object(String),
    Array(usize),
}

fn parse_path(value: &Value) -> Result<Vec<PathPart>, ProjectionPatchError> {
    let parts = value
        .as_array()
        .ok_or_else(|| error("Projection patch path is invalid"))?;
    if parts.len() > MAX_TURN_PROJECTION_PATCH_DEPTH {
        return Err(error("Projection patch path exceeds its depth bound"));
    }
    parts
        .iter()
        .map(|part| match part {
            Value::String(value) => Ok(PathPart::Object(value.clone())),
            Value::Number(value) => value
                .as_u64()
                .and_then(|value| usize::try_from(value).ok())
                .map(PathPart::Array)
                .ok_or_else(|| error("Projection patch path is invalid")),
            _ => Err(error("Projection patch path is invalid")),
        })
        .collect()
}

fn descend_mut<'a>(
    root: &'a mut Value,
    path: &[PathPart],
) -> Result<&'a mut Value, ProjectionPatchError> {
    let mut current = root;
    for part in path {
        current = match (current, part) {
            (Value::Object(object), PathPart::Object(key)) => object
                .get_mut(key)
                .ok_or_else(|| error("Projection patch object path is invalid"))?,
            (Value::Array(array), PathPart::Array(index)) => array
                .get_mut(*index)
                .ok_or_else(|| error("Projection patch array path is out of bounds"))?,
            _ => return Err(error("Projection patch path container differs")),
        };
    }
    Ok(current)
}

fn set_at_path(
    root: &mut Value,
    path: &[PathPart],
    replacement: Value,
) -> Result<(), ProjectionPatchError> {
    let Some((leaf, parent_path)) = path.split_last() else {
        *root = replacement;
        return Ok(());
    };
    let parent = descend_mut(root, parent_path)?;
    match (parent, leaf) {
        (Value::Object(object), PathPart::Object(key)) => {
            object.insert(key.clone(), replacement);
            Ok(())
        }
        (Value::Array(array), PathPart::Array(index)) if *index < array.len() => {
            array[*index] = replacement;
            Ok(())
        }
        (Value::Array(_), PathPart::Array(_)) => {
            Err(error("Projection patch array path is out of bounds"))
        }
        _ => Err(error("Projection patch object path is invalid")),
    }
}

fn remove_at_path(root: &mut Value, path: &[PathPart]) -> Result<(), ProjectionPatchError> {
    let (leaf, parent_path) = path
        .split_last()
        .ok_or_else(|| error("Projection patch cannot remove its root"))?;
    let parent = descend_mut(root, parent_path)?;
    match (parent, leaf) {
        (Value::Object(object), PathPart::Object(key)) => {
            object.remove(key);
            Ok(())
        }
        (Value::Array(array), PathPart::Array(index)) if *index < array.len() => {
            array.remove(*index);
            Ok(())
        }
        (Value::Array(_), PathPart::Array(_)) => {
            Err(error("Projection patch array removal is out of bounds"))
        }
        _ => Err(error("Projection patch object removal is invalid")),
    }
}

fn validate_patch_budget(raw_patch: &Map<String, Value>) -> Result<(), ProjectionPatchError> {
    if serde_json::to_vec(raw_patch)
        .map_err(|_| error("Projection patch cannot be encoded"))?
        .len()
        > MAX_TURN_PROJECTION_PATCH_BYTES
    {
        return Err(error("Projection patch exceeds its byte bound"));
    }
    Ok(())
}

pub(crate) fn apply_projection_patch(
    projection: Option<&Map<String, Value>>,
    raw_patch: &Map<String, Value>,
) -> Result<Map<String, Value>, ProjectionPatchError> {
    validate_patch_budget(raw_patch)?;
    if raw_patch.get("version").and_then(Value::as_u64) != Some(PROJECTION_PATCH_VERSION) {
        return Err(error("Projection patch version is unsupported"));
    }
    let operations = raw_patch
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| error("Projection patch operations must be an array"))?;
    if operations.len() > MAX_TURN_PROJECTION_PATCH_OPERATIONS {
        return Err(error("Projection patch operation count exceeds its bound"));
    }
    let mut next = Value::Object(projection.cloned().unwrap_or_default());
    for operation in operations {
        let operation = operation
            .as_object()
            .ok_or_else(|| error("Projection patch operation must be an object"))?;
        let path = parse_path(
            operation
                .get("path")
                .ok_or_else(|| error("Projection patch path is missing"))?,
        )?;
        match operation.get("op").and_then(Value::as_str) {
            Some("set") => set_at_path(
                &mut next,
                &path,
                operation.get("value").cloned().unwrap_or(Value::Null),
            )?,
            Some("remove") => remove_at_path(&mut next, &path)?,
            Some("append_text") => {
                let suffix = operation
                    .get("value")
                    .and_then(Value::as_str)
                    .ok_or_else(|| error("Projection text append value must be a string"))?;
                let target = descend_mut(&mut next, &path)?;
                let target = target
                    .as_str()
                    .ok_or_else(|| error("Projection text append target must be a string"))?;
                *descend_mut(&mut next, &path)? = Value::String(format!("{target}{suffix}"));
            }
            Some("append") => {
                let suffix = operation
                    .get("value")
                    .and_then(Value::as_array)
                    .ok_or_else(|| error("Projection list append value must be an array"))?;
                let target = descend_mut(&mut next, &path)?;
                let target = target
                    .as_array_mut()
                    .ok_or_else(|| error("Projection list append target must be an array"))?;
                target.extend(suffix.iter().cloned());
            }
            Some("truncate") => {
                let length = operation
                    .get("length")
                    .and_then(Value::as_u64)
                    .and_then(|value| usize::try_from(value).ok())
                    .ok_or_else(|| error("Projection list truncation length is invalid"))?;
                let target = descend_mut(&mut next, &path)?;
                let target = target
                    .as_array_mut()
                    .ok_or_else(|| error("Projection list truncation target is invalid"))?;
                if length > target.len() {
                    return Err(error("Projection list truncation target is invalid"));
                }
                target.truncate(length);
            }
            _ => return Err(error("Projection patch operation is unsupported")),
        }
    }
    next.as_object()
        .cloned()
        .ok_or_else(|| error("Projection patch result must be an object"))
}

fn push_operation(
    operations: &mut Vec<Value>,
    operation: Value,
) -> Result<(), ProjectionPatchError> {
    if operations.len() >= MAX_TURN_PROJECTION_PATCH_OPERATIONS {
        return Err(error("Projection patch operation count exceeds its bound"));
    }
    operations.push(operation);
    Ok(())
}

fn path_json(path: &[PathPart]) -> Value {
    Value::Array(
        path.iter()
            .map(|part| match part {
                PathPart::Object(value) => Value::String(value.clone()),
                PathPart::Array(value) => Value::from(*value),
            })
            .collect(),
    )
}

fn diff_value(
    before: &Value,
    after: &Value,
    path: &mut Vec<PathPart>,
    operations: &mut Vec<Value>,
) -> Result<(), ProjectionPatchError> {
    if before == after {
        return Ok(());
    }
    if path.len() > MAX_TURN_PROJECTION_PATCH_DEPTH {
        return Err(error("Projection patch path exceeds its depth bound"));
    }
    match (before, after) {
        (Value::String(before), Value::String(after)) if after.starts_with(before) => {
            let suffix = &after[before.len()..];
            if !suffix.is_empty() {
                push_operation(
                    operations,
                    json!({"op": "append_text", "path": path_json(path), "value": suffix}),
                )?;
            }
        }
        (Value::Object(before), Value::Object(after)) => {
            let before_keys = before.keys().cloned().collect::<BTreeSet<_>>();
            let after_keys = after.keys().cloned().collect::<BTreeSet<_>>();
            for key in before_keys.difference(&after_keys) {
                path.push(PathPart::Object(key.clone()));
                push_operation(operations, json!({"op": "remove", "path": path_json(path)}))?;
                path.pop();
            }
            for key in after_keys.difference(&before_keys) {
                path.push(PathPart::Object(key.clone()));
                push_operation(
                    operations,
                    json!({"op": "set", "path": path_json(path), "value": after[key]}),
                )?;
                path.pop();
            }
            for key in before_keys.intersection(&after_keys) {
                path.push(PathPart::Object(key.clone()));
                diff_value(&before[key], &after[key], path, operations)?;
                path.pop();
            }
        }
        (Value::Array(before), Value::Array(after)) => {
            let shared = before.len().min(after.len());
            for index in 0..shared {
                path.push(PathPart::Array(index));
                diff_value(&before[index], &after[index], path, operations)?;
                path.pop();
            }
            if after.len() < before.len() {
                push_operation(
                    operations,
                    json!({"op": "truncate", "path": path_json(path), "length": after.len()}),
                )?;
            } else if after.len() > before.len() {
                push_operation(
                    operations,
                    json!({"op": "append", "path": path_json(path), "value": &after[before.len()..]}),
                )?;
            }
        }
        _ => push_operation(
            operations,
            json!({"op": "set", "path": path_json(path), "value": after}),
        )?,
    }
    Ok(())
}

pub(crate) fn build_projection_patch(
    before: &Map<String, Value>,
    after: &Map<String, Value>,
    base_revision: u64,
    target_revision: u64,
) -> Result<Map<String, Value>, ProjectionPatchError> {
    let mut operations = Vec::new();
    diff_value(
        &Value::Object(before.clone()),
        &Value::Object(after.clone()),
        &mut Vec::new(),
        &mut operations,
    )?;
    let patch = json!({
        "version": PROJECTION_PATCH_VERSION,
        "baseRevision": base_revision,
        "targetRevision": target_revision,
        "operations": operations,
    })
    .as_object()
    .expect("projection patch is an object")
    .clone();
    validate_patch_budget(&patch)?;
    Ok(patch)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_patch_round_trip_matches_the_shared_wire_vocabulary() {
        let before = json!({
            "content": "hello",
            "thinking": "old",
            "toolRounds": [{"status": "running", "results": [1, 2]}],
            "obsolete": true,
        });
        let after = json!({
            "content": "hello world",
            "thinking": "new",
            "toolRounds": [
                {"status": "done", "results": [1]},
                {"status": "running", "results": []},
            ],
            "usage": {"tokens": 7},
        });
        let patch = build_projection_patch(
            before.as_object().unwrap(),
            after.as_object().unwrap(),
            41,
            42,
        )
        .unwrap();
        assert_eq!(patch["version"], 1);
        assert_eq!(patch["baseRevision"], 41);
        assert_eq!(patch["targetRevision"], 42);
        let operation_kinds = patch["operations"]
            .as_array()
            .unwrap()
            .iter()
            .map(|operation| operation["op"].as_str().unwrap())
            .collect::<BTreeSet<_>>();
        assert!(operation_kinds.contains("append_text"));
        assert!(operation_kinds.contains("append"));
        assert!(operation_kinds.contains("truncate"));
        assert!(operation_kinds.contains("remove"));
        assert!(operation_kinds.contains("set"));
        assert_eq!(
            apply_projection_patch(Some(before.as_object().unwrap()), &patch).unwrap(),
            *after.as_object().unwrap(),
        );
    }

    #[test]
    fn malformed_patch_paths_and_root_results_fail_closed() {
        let base = json!({"items": [1], "text": "a"});
        for patch in [
            json!({"version": 2, "operations": []}),
            json!({"version": 1, "operations": [{"op": "remove", "path": []}]}),
            json!({"version": 1, "operations": [{"op": "append", "path": ["text"], "value": []}]}),
            json!({"version": 1, "operations": [{"op": "truncate", "path": ["items"], "length": 2}]}),
            json!({"version": 1, "operations": [{"op": "set", "path": [], "value": []}]}),
        ] {
            assert!(apply_projection_patch(
                Some(base.as_object().unwrap()),
                patch.as_object().unwrap(),
            )
            .is_err());
        }
    }

    #[test]
    fn patch_depth_operation_and_byte_budgets_reject_before_application() {
        let deep_path = (0..=MAX_TURN_PROJECTION_PATCH_DEPTH)
            .map(|index| Value::String(format!("level-{index}")))
            .collect::<Vec<_>>();
        let deep = json!({
            "version": 1,
            "operations": [{"op": "set", "path": deep_path, "value": 1}],
        });
        assert!(apply_projection_patch(None, deep.as_object().unwrap()).is_err());

        let too_many = json!({
            "version": 1,
            "operations": (0..=MAX_TURN_PROJECTION_PATCH_OPERATIONS)
                .map(|_| json!({"op": "set", "path": ["x"], "value": 1}))
                .collect::<Vec<_>>(),
        });
        assert!(apply_projection_patch(None, too_many.as_object().unwrap()).is_err());

        let too_large = json!({
            "version": 1,
            "operations": [{
                "op": "set", "path": ["content"],
                "value": "x".repeat(MAX_TURN_PROJECTION_PATCH_BYTES),
            }],
        });
        assert!(apply_projection_patch(None, too_large.as_object().unwrap()).is_err());
    }
}
