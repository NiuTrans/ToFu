//! Caller parity, frozen legacy guard compatibility and live occurrence preservation.
use serde_json::{json, Value};
use tofu_runtime::{
    provider::{Budget, ChatStream, Error},
    tool_policy::{normalize_caller, FloodError, FloodGuard},
};
#[test]
fn historical_guard_and_python_caller_behavior_match() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/python_tool_policy.json")).unwrap();
    for (case, c) in fixture["guards"].as_array().unwrap().iter().enumerate() {
        let mut guard = FloodGuard::new(c["limit"].as_u64().unwrap() as usize, 7).unwrap();
        for (step, op) in c["operations"].as_array().unwrap().iter().enumerate() {
            let result = guard.observe(
                op["position"].as_u64().unwrap() as usize,
                &op["call"],
                op["final"].as_bool().unwrap(),
            );
            let error = match result {
                Ok(()) => Value::Null,
                Err(FloodError::Flood {
                    count,
                    limit,
                    matching_positions,
                }) => json!({"count":count,"limit":limit,"positions":matching_positions}),
                other => panic!("unexpected {other:?}"),
            };
            assert_eq!(error, op["error"], "case {case}, step {step}");
            assert_eq!(
                guard.retained_positions(),
                op["retained"].as_u64().unwrap() as usize
            );
        }
    }
    for c in fixture["callers"].as_array().unwrap() {
        let result = normalize_caller(&c["caller"], c["required"].as_bool().unwrap());
        assert_eq!(result.is_err(), c["invalid"].as_bool().unwrap(), "{c}");
        if let Ok(normalized) = result {
            assert_eq!(normalized.unwrap_or(Value::Null), c["normalized"]);
        }
    }
}
fn budget() -> Budget {
    Budget {
        event_bytes: 1048576,
        output_bytes: 1048576,
        tool_calls: 128,
    }
}
#[test]
fn equal_arguments_at_distinct_positions_reach_terminal_usage() {
    let mut stream = ChatStream::with_tool_limit(budget(), 2).unwrap();
    for index in 0..7 {
        let event = json!({"choices":[{"delta":{"tool_calls":[{"index":index,"id":format!("c{index}"),"function":{"name":"lookup","arguments":"{}"}}]}}]});
        assert_eq!(stream.feed(format!("data: {event}\n\n").as_bytes()), Ok(false));
    }
    let terminal = json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"completion_tokens":37}});
    stream.feed(format!("data: {terminal}\n\ndata: [DONE]\n\n").as_bytes()).unwrap();
    let result = stream.finish(false).unwrap();
    assert_eq!(result.message["tool_calls"].as_array().unwrap().len(), 7);
    assert_eq!(result.usage["completion_tokens"], 37);
    assert_eq!(result.state.as_str(), "provider_finished");
}
#[test]
fn empty_arguments_at_distinct_positions_are_preserved() {
    let mut stream = ChatStream::with_tool_limit(budget(), 2).unwrap();
    let calls: Vec<_> = (0..3)
        .map(|i| json!({"index":i,"id":format!("c{i}"),"function":{"name":"status","arguments":""}}))
        .collect();
    let event = json!({"choices":[{"delta":{"tool_calls":calls},"finish_reason":"tool_calls"}]});
    stream.feed(format!("data: {event}\n\ndata: [DONE]\n\n").as_bytes()).unwrap();
    assert_eq!(stream.finish(false).unwrap().message["tool_calls"].as_array().unwrap().len(), 3);
}
#[test]
fn caller_metadata_is_normalized_and_cannot_change_existing_authority() {
    let mut stream = ChatStream::new(budget()).unwrap();
    let event = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","caller":{"type":"program","caller_id":" p ","junk":"drop"},"function":{"name":"lookup","arguments":"{}"}}]},"finish_reason":"tool_calls"}]});
    stream
        .feed(format!("data: {event}\n\ndata: [DONE]\n\n").as_bytes())
        .unwrap();
    let result = stream.finish(false).unwrap();
    assert_eq!(
        result.message["tool_calls"][0]["caller"],
        json!({"type":"program","caller_id":"p"})
    );
}
#[test]
fn position_budget_and_configuration_are_explicit() {
    assert!(matches!(
        FloodGuard::new(1, 7),
        Err(FloodError::InvalidBudget)
    ));
    let mut guard = FloodGuard::new(2, 1).unwrap();
    let call = json!({"function":{"name":"x","arguments":"{}"}});
    guard.observe(0, &call, true).unwrap();
    assert_eq!(
        guard.observe(1, &call, true),
        Err(FloodError::PositionLimit)
    );
}
#[test]
fn fixed_signature_retention_and_error_projection_are_bounded() {
    let mut guard = FloodGuard::new(32, 128).unwrap();
    let call = json!({"function":{"name":"lookup","arguments":"{\"text\":\"a long value\"}"}});
    for _ in 0..10000 {
        guard.observe(0, &call, true).unwrap();
    }
    assert_eq!(guard.retained_positions(), 1);
    let error = Error::ToolFlood {
        count: 33,
        limit: 32,
    }
    .task_error();
    assert_eq!(error["kind"], "tool_loop");
    assert_eq!(error["retryable"], true);
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/python_tool_policy.json")).unwrap();
    for (key, value) in fixture["flood_envelope"].as_object().unwrap() {
        assert_eq!(&error[key], value);
    }
    assert_eq!(error["autoRetryExhausted"], true);
    assert_eq!(error["failureStage"], "provider_response");
    assert_eq!(error["providerErrorCode"], "tool_call_flood");
    assert_eq!(error["identicalToolCalls"], 33);
}
#[test]
fn caller_authority_revision_is_rejected_before_arguments_are_adopted() {
    let mut stream = ChatStream::new(budget()).unwrap();
    for (index, id) in ["one", "two"].iter().enumerate() {
        let event = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call","caller":{"type":"program","caller_id":id},"function":{"name":"lookup","arguments":"{}"}}]}}]});
        let result = stream.feed(format!("data: {event}\n\n").as_bytes());
        if index == 0 {
            assert!(result.is_ok())
        } else {
            assert_eq!(result, Err(Error::InvalidToolCaller));
        }
    }
}
#[test]
fn named_retransmission_does_not_duplicate_arguments_or_occurrences() {
    let mut stream = ChatStream::with_tool_limit(budget(), 2).unwrap();
    let event = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call","function":{"name":"lookup","arguments":"{\"x\":1}"}}]}}]});
    for _ in 0..3 {
        assert_eq!(
            stream.feed(format!("data: {event}\n\n").as_bytes()),
            Ok(false)
        );
    }
    stream.feed(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\ndata: [DONE]\n\n").unwrap();
    let result = stream.finish(false).unwrap();
    assert_eq!(
        result.message["tool_calls"][0]["function"]["arguments"],
        "{\"x\":1}"
    );
    assert_eq!(result.message["tool_calls"].as_array().unwrap().len(), 1);
}

#[test]
fn decoded_execution_arguments_preserve_numeric_kind_and_value() {
    use tofu_runtime::tool_policy::lossless_arguments;
    let value = lossless_arguments("{\"a\":18446744073709551615,\"b\":1.0,\"c\":-0.0}").unwrap();
    assert_eq!(value["a"].as_u64(), Some(u64::MAX));
    assert!(value["b"].is_f64());
    assert!(value["c"].as_f64().unwrap().is_sign_negative());
    for raw in [
        "{\"a\":18446744073709551616}",
        "{\"a\":-9223372036854775809}",
        "{\"a\":-0}",
    ] {
        assert!(lossless_arguments(raw).is_err());
    }
    assert!(
        lossless_arguments("{\"__serde_json_private_number\":\"18446744073709551616\"}")
            .unwrap()
            .is_object()
    );
}
