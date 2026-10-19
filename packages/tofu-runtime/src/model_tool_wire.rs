//! Model-visible function projection over one owned execution catalog.
//! A visible name never grants authority: Catalog validates actual arguments,
//! permissions and callbacks. Default visibility preserves the legacy catalog.
use serde_json::{Map, Value};
use std::collections::BTreeSet;

pub fn project(catalog: &Map<String, Value>, visible: Option<&Value>) -> Result<Vec<Value>, &'static str> {
    let Some(visible) = visible else {
        return Ok(crate::execution_contract::native_model_tools(catalog));
    };
    let tools = visible.as_array().ok_or("model tools must be an array")?;
    let mut names = BTreeSet::new();
    for tool in tools {
        let entry = tool.as_object().ok_or("model tool must be an object")?;
        if tool["type"] != "function" || entry.keys().any(|key| !matches!(key.as_str(),"type"|"function")) {
            return Err("model tool kind is unsupported");
        }
        let function = tool["function"].as_object().ok_or("model function is missing")?;
        let name = tool["function"]["name"].as_str().filter(|s| !s.is_empty()).ok_or("model tool name is missing")?;
        if !catalog.contains_key(name) || !names.insert(name) {
            return Err("model tool name is undeclared or duplicated");
        }
        if function.keys().any(|key| !matches!(key.as_str(),"name"|"description"|"parameters"))
            || !tool["function"]["parameters"].is_object()
            || function.get("description").is_some_and(|v| !v.is_string()) {
            return Err("model tool schema is unsupported");
        }
    }
    Ok(tools.clone())
}
