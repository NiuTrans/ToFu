//! Visibility never adds execution authority or weakens argument validation.
use serde_json::json;
use tofu_runtime::{model_tool_wire::project, tool_contract::Catalog};

#[test]
fn visible_subset_keeps_full_execution_catalog_and_source_argument_contract() {
    let tools = json!({"read":{"name":"read","contractVersion":"tofu.tool-contract/v2","permission":"read",
        "arguments_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":false}},
        "hidden":{"name":"hidden","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}}});
    let catalog = Catalog::compile(7,"wire",&tools).unwrap();
    let visible = json!([{"type":"function","function":{"name":"read","parameters":{"type":"object"}}}]);
    assert_eq!(project(tools.as_object().unwrap(),Some(&visible)).unwrap(), visible.as_array().unwrap().clone());
    assert!(catalog.validate(7,"hidden",json!({})).is_ok());
    assert!(catalog.validate(7,"read",json!({"path":3})).is_err());
    assert!(catalog.validate(7,"read",json!({"path":"a"})).is_ok());
    assert_eq!(project(tools.as_object().unwrap(),None).unwrap().len(),2);
    assert!(project(tools.as_object().unwrap(),Some(&json!([]))).unwrap().is_empty());
}

#[test]
fn undeclared_duplicate_hosted_and_malformed_visibility_refuse() {
    let tools = json!({"read":{}});
    let read = json!({"type":"function","function":{"name":"read","parameters":{}}});
    for visible in [json!(null), json!([read.clone(),read.clone()]),
        json!([{"type":"function","function":{"name":"write","parameters":{}}}]),
        json!([{"type":"web_search"}]),json!([{"type":"function","function":{"name":"read","parameters":null}}]),
        json!([{"type":"function","function":{"name":"read","parameters":{},"permission":"write"}}])] {
        assert!(project(tools.as_object().unwrap(),Some(&visible)).is_err());
    }
}
