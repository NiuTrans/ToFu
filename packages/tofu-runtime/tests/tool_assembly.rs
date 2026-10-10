//! Actual Python stream results replayed through native occurrence assembly.
use serde_json::{json, Value};
use std::{collections::BTreeSet, sync::Arc};
use tofu_runtime::{
    provider::{Budget, ChatStream, Error},
    tool_assembly::wire_tool_names,
};
fn budget() -> Budget {
    Budget {
        event_bytes: 1048576,
        output_bytes: 1048576,
        tool_calls: 128,
    }
}
#[test]
fn actual_python_assembly_matches_at_every_transport_chunk_size() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_tool_assembly.json")).unwrap();
    for (case, c) in cases.as_array().unwrap().iter().enumerate() {
        let wire: Vec<_> = c["wire"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        let names: Arc<BTreeSet<String>> = Arc::new(
            c["names"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().into())
                .collect(),
        );
        for size in [1, 3, 7, 64, 65536] {
            let mut stream = ChatStream::with_tool_names(budget(), 32, names.clone()).unwrap();
            for bytes in wire.chunks(size) {
                if stream.feed(bytes).unwrap() {
                    break;
                }
            }
            let result = stream.finish(false).unwrap();
            assert_eq!(
                result.state.as_str(),
                c["state"].as_str().unwrap(),
                "case {case}, size {size}"
            );
            assert_eq!(result.message, c["message"], "case {case}, size {size}");
            assert_eq!(
                result.malformed_frames,
                c["malformed"].as_u64().unwrap(),
                "case {case}, size {size}"
            );
        }
    }
}
#[test]
fn namespace_catalog_uses_callable_names_and_excludes_search_wrappers() {
    let tools = json!([{"type":"namespace","name":"group","tools":[{"type":"function","name":"lookup"},{"type":"namespace","tools":[{"function":{"name":"read_files"}}]}]},{"type":"tool_search","name":"search"},{"name":"anthropic_native","input_schema":{}}]);
    assert_eq!(
        wire_tool_names(&tools),
        BTreeSet::from([
            "lookup".into(),
            "read_files".into(),
            "anthropic_native".into()
        ])
    );
}
#[test]
fn fresh_ids_under_one_upstream_index_still_count_distinct_flood_positions() {
    let mut stream = ChatStream::with_tool_limit(budget(), 2).unwrap();
    for i in 0..3 {
        let event = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":format!("c{i}"),"function":{"name":"lookup","arguments":"{}"}}]}}]});
        let result = stream.feed(format!("data: {event}\n\n").as_bytes());
        if i < 2 {
            assert!(result.is_ok());
        } else {
            assert_eq!(result, Err(Error::ToolFlood { count: 3, limit: 2 }));
        }
    }
}
#[test]
fn split_occurrences_obey_the_declared_capacity() {
    let mut b = budget();
    b.tool_calls = 1;
    let mut stream = ChatStream::new(b).unwrap();
    for i in 0..2 {
        let event = json!({"choices":[{"delta":{"tool_calls":[{"id":format!("c{i}"),"function":{"name":"lookup","arguments":"{}"}}]}}]});
        let result = stream.feed(format!("data: {event}\n\n").as_bytes());
        if i == 0 {
            assert!(result.is_ok());
        } else {
            assert_eq!(result, Err(Error::ToolLimit));
        }
    }
}
#[test]
fn long_retransmission_keeps_occurrence_and_alias_state_bounded() {
    let mut stream = ChatStream::new(budget()).unwrap();
    let mut last = String::new();
    for i in 0..8 {
        let event = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":format!("c{i}"),"function":{"name":"lookup","arguments":format!("{{\"x\":{i}}}")}}]}}]});
        last = format!("data: {event}\n\n");
        stream.feed(last.as_bytes()).unwrap();
    }
    for _ in 0..10000 {
        stream.feed(last.as_bytes()).unwrap();
    }
    let state = stream.tool_assembly_footprint();
    assert_eq!(state.positions, 8);
    assert_eq!(state.index_aliases, 1);
}
#[test]
fn large_immutable_catalog_resolves_fragments_without_joining_complete_names() {
    let mut names: BTreeSet<String> = (0..10000).map(|i| format!("tool_{i:05}")).collect();
    names.extend(["read_files".into(), "run_command".into()]);
    let names = Arc::new(names);
    let mut stream = ChatStream::with_tool_names(budget(), 32, names.clone()).unwrap();
    for (name, args) in [("read_", ""), ("files", "{}"), ("run_command", "{}")] {
        let event = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":name,"arguments":args}}]}}]});
        stream
            .feed(format!("data: {event}\n\n").as_bytes())
            .unwrap();
    }
    stream.feed(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\ndata: [DONE]\n\n").unwrap();
    let result = stream.finish(false).unwrap();
    assert_eq!(
        result.message["tool_calls"][0]["function"]["name"],
        "read_files"
    );
    assert_eq!(
        result.message["tool_calls"][1]["function"]["name"],
        "run_command"
    );
    assert_eq!(Arc::strong_count(&names), 1);
}
