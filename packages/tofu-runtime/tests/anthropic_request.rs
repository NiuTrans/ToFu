//! Canonical Python request oracle; unsupported data never silently drops.
use serde_json::{json, Value};
use tofu_runtime::anthropic_request::Body;
#[test]
fn actual_python_request_conversion_matches_borrowed_serialization() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_anthropic_request.json")).unwrap();
    for (i, case) in cases.as_array().unwrap().iter().enumerate() {
        let mut template = case["body"].as_object().unwrap().clone();
        let messages = template.remove("messages").unwrap();
        let body = Body {
            template: &template,
            messages: messages.as_array().unwrap(),
            output: None,
        };
        assert_eq!(
            serde_json::to_value(body).unwrap(),
            case["output"],
            "case {i}"
        );
    }
}
#[test]
fn unsupported_media_or_policy_and_invalid_inputs_refuse() {
    for value in [
        json!({"model":"fixture","response_format":{"type":"json_object"}}),
        json!({"model":"fixture","response_format":{"type":"json_schema","json_schema":{"schema":{},"strict":false}}}),
        json!({"model":"fixture","response_format":{"type":"json_schema","json_schema":{"schema":{},"strict":true,"unknown":1}}}),
        json!({"model":"fixture","response_format":{"type":"json_schema","json_schema":{"schema":[]}}}),
        json!({"model":"fixture","response_format":{"type":"json_schema","json_schema":{"schema":{}}},"output_config":{"format":{"type":"json_schema","schema":{"type":"string"}}}}),
        json!({"model":"fixture","max_tokens":false}),
        json!({"model":"fixture","tool_choice":false}),
        json!({"model":"fixture","tool_choice":{"type":"auto","disable_parallel_tool_use":1}}),
        json!({"model":"fixture","max_tokens":0}),
        json!({"model":"fixture","parallel_tool_calls":true,"tool_choice":{"type":"auto","disable_parallel_tool_use":true}}),
    ] {
        assert!(serde_json::to_vec(&Body {
            template: value.as_object().unwrap(),
            messages: &[],
            output: None
        })
        .is_err());
    }
    for messages in [
        json!([{"role":"user","content":[{"type":"input_audio","input_audio":{"data":"AAAA","format":"wav"}}]}]),
        json!([{"role":"assistant","tool_calls":[{"id":"call","function":{"name":"read","arguments":"[]"}}]}]),
        json!([{"role":"unknown","content":"text"}]),
    ] {
        let template = json!({"model":"fixture"});
        assert!(serde_json::to_vec(&Body {
            template: template.as_object().unwrap(),
            messages: messages.as_array().unwrap(),
            output: None
        })
        .is_err());
    }
}
