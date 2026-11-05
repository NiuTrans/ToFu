//! Optional storage-side request-list view. Full replay and durable bytes are unchanged.
use serde_json::{json, Value};
use crate::generated_request_inspector_projection::SNAPSHOT_FIELDS;

pub(crate) fn project(event: Value) -> Value {
    let Some(object) = event.as_object() else { return event };
    if object.get("type").and_then(Value::as_str) != Some("messages_snapshot")
        || !object.contains_key("prefixLen")
        || !["messageCount", "toolsCount"].iter().all(|key|
            object.get(*key).is_some_and(|v| v.is_i64() || v.is_u64())) {
        return event;
    }
    let empty = vec![];
    let messages = match object.get("newMessages") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => &empty,
        Some(Value::Array(messages)) => messages,
        _ => return event,
    };
    let mut tail = Vec::new();
    for message in messages {
        let Some(message) = message.as_object() else { continue };
        let calls = match message.get("tool_calls") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => &empty,
            Some(Value::Array(calls)) => calls,
            _ => return event,
        };
        let names: Vec<Value> = calls.iter().filter_map(|call| {
            let call = call.as_object()?;
            let name = match call.get("function").and_then(Value::as_object) {
                Some(function) => function.get("name"),
                None => call.get("name"),
            };
            Some(json!({"name": name}))
        }).collect();
        let blocks: Vec<Value> = message.get("content").and_then(Value::as_array)
            .into_iter().flatten().filter(|block| block.get("type").and_then(Value::as_str) == Some("tool_use"))
            .map(|block| json!({"type": "tool_use", "name": block.get("name")})).collect();
        if !names.is_empty() || !blocks.is_empty() {
            tail.push(json!({"tool_calls": names, "content": blocks}));
        }
    }
    let mut projected = serde_json::Map::new();
    for key in SNAPSHOT_FIELDS {
        if let Some(value) = object.get(*key) { projected.insert((*key).into(), value.clone()); }
    }
    projected.insert("newMessages".into(), Value::Array(tail));
    Value::Object(projected)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_inspector_projection_matches_shared_contract() {
        let cases: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/request_inspector_projection.json")).unwrap();
        for case in cases.as_array().unwrap() {
            assert_eq!(project(case["event"].clone()), case["expected"], "{}", case["name"]);
        }
    }
}
