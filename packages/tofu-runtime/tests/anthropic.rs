//! Python translation/replay oracle and bounded native lifecycle contracts.
use serde_json::{json, Value};
use std::collections::BTreeMap;
use tofu_runtime::anthropic::{Budget, Error, Projection, Translator};
fn normalize(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, v) in map {
                if key == "arguments" {
                    if let Some(s) = v.as_str() {
                        if let Ok(parsed) = serde_json::from_str::<Value>(s) {
                            if parsed.is_object() {
                                *v = parsed;
                            }
                        }
                    }
                } else {
                    normalize(v);
                }
            }
        }
        Value::Array(array) => {
            for v in array {
                normalize(v);
            }
        }
        _ => {}
    }
}
#[test]
fn actual_python_translator_and_replay_state_match() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_anthropic_stream.json")).unwrap();
    for case in cases.as_array().unwrap() {
        let reverse = serde_json::from_value(case["reverse"].clone()).unwrap();
        let mut owner = Translator::new(
            Budget {
                blocks: 4096,
                retained_bytes: 1 << 20,
            },
            reverse,
        )
        .unwrap();
        let mut outputs = Vec::new();
        let mut failed = false;
        for event in case["events"].as_array().unwrap() {
            match owner.translate(event) {
                Ok(Some(Projection::Chunk(value))) => outputs.push(value),
                Ok(Some(Projection::Done)) => outputs.push("[DONE]".into()),
                Ok(None) => {}
                Err(Error::Protocol) => {
                    failed = true;
                    break;
                }
                other => panic!("{} {other:?}", case["name"]),
            }
        }
        assert_eq!(
            failed,
            case["failed"].as_bool().unwrap(),
            "{}",
            case["name"]
        );
        let mut actual = Value::Array(outputs);
        let mut expected = case["outputs"].clone();
        normalize(&mut actual);
        normalize(&mut expected);
        assert_eq!(actual, expected, "{}", case["name"]);
        if !failed {
            assert_eq!(
                Value::Array(owner.replay_blocks()),
                case["blocks"],
                "{}",
                case["name"]
            );
        }
        assert_eq!(
            owner.replayed_tool_starts(),
            case["replayed"].as_u64().unwrap() as usize,
            "{}",
            case["name"]
        );
    }
}
#[test]
fn exhausted_or_failed_owner_never_accepts_more_events() {
    let mut owner = Translator::new(
        Budget {
            blocks: 1,
            retained_bytes: 1024,
        },
        BTreeMap::new(),
    )
    .unwrap();
    owner.translate(&json!({"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}})).unwrap();
    assert_eq!(
        owner.translate(
            &json!({"type":"content_block_start","index":1,"content_block":{"type":"text"}})
        ),
        Err(Error::Capacity)
    );
    assert_eq!(
        owner.translate(&json!({"type":"message_stop"})),
        Err(Error::Closed)
    );
    let mut owner = Translator::new(
        Budget {
            blocks: 4096,
            retained_bytes: 1 << 20,
        },
        BTreeMap::new(),
    )
    .unwrap();
    assert_eq!(
        owner.translate(&json!({"type":"message_stop"})),
        Ok(Some(Projection::Done))
    );
    assert_eq!(owner.translate(&json!({"type":"ping"})), Err(Error::Closed));
}

#[test]
fn real_sse_fragmentation_preserves_replay_signature_and_terminal_cutoff() {
    use std::collections::BTreeSet;
    use std::sync::Arc;
    use tofu_runtime::{anthropic::Stream, execution_contract::ProviderStreamState, provider};
    let events = vec![
        json!({"type":"message_start","message":{"usage":{"input_tokens":10,"cache_read_input_tokens":20,"output_tokens":1}}}),
        json!({"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"计划","signature":"opaque"}}),
        json!({"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"😀"}}),
        json!({"type":"content_block_stop","index":0}),
        json!({"type":"content_block_start","index":1,"content_block":{"type":"server_tool_use","id":"host","name":"search","input":{}}}),
        json!({"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"q\":\"source\"}"}}),
        json!({"type":"content_block_stop","index":1}),
        json!({"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"call","name":"read","input":{}}}),
        json!({"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"path\":\"file\"}"}}),
        json!({"type":"content_block_stop","index":2}),
        json!({"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":8}}),
        json!({"type":"message_stop"}),
    ];
    let mut wire = String::new();
    for event in &events {
        wire += &format!(
            "event: {}\ndata: {event}\n\n",
            event["type"].as_str().unwrap()
        );
    }
    let mut bytes = wire.into_bytes();
    bytes.extend_from_slice(b"data: {invalid\xff\n\n");
    for size in [1, 3, 7, 64, 65536] {
        let mut owner = Stream::new(
            provider::Budget {
                event_bytes: 65536,
                output_bytes: 65536,
                tool_calls: 8,
            },
            Budget {
                blocks: 32,
                retained_bytes: 65536,
            },
            5,
            Arc::new(BTreeSet::from(["read".into()])),
            BTreeMap::new(),
        )
        .unwrap();
        for part in bytes.chunks(size) {
            if owner.feed(part).unwrap() {
                break;
            }
        }
        let result = owner.finish(false).unwrap();
        assert_eq!(result.state, ProviderStreamState::ProviderFinished);
        assert!(result.done_seen);
        assert_eq!(result.malformed_frames, 0);
        assert_eq!(result.message["reasoning_content"], "计划😀");
        assert_eq!(result.message["thinking_signature"], "opaque");
        assert_eq!(result.message["tool_calls"].as_array().unwrap().len(), 1);
        assert_eq!(result.message["tool_calls"][0]["function"]["name"], "read");
        assert_eq!(
            result.message["_anthropic_content_blocks"]
                .as_array()
                .unwrap()
                .len(),
            3
        );
        assert_eq!(result.usage["prompt_tokens"], 30);
        assert_eq!(result.usage["completion_tokens"], 8);
    }
}

#[test]
fn incomplete_input_and_eof_never_manufacture_success() {
    use std::sync::Arc;
    use tofu_runtime::{anthropic::Stream, execution_contract::ProviderStreamState, provider};
    let new = || {
        Stream::new(
            provider::Budget {
                event_bytes: 4096,
                output_bytes: 4096,
                tool_calls: 8,
            },
            Budget {
                blocks: 8,
                retained_bytes: 4096,
            },
            5,
            Arc::new(Default::default()),
            BTreeMap::new(),
        )
        .unwrap()
    };
    let mut owner = new();
    owner.feed(b"data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"partial\"}}\n\n").unwrap();
    let result = owner.finish(false).unwrap();
    assert_eq!(result.state, ProviderStreamState::PrematureClose);
    assert_eq!(result.message["content"], "partial");
    let mut owner = new();
    owner.feed(b"data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"server_tool_use\",\"input\":{}}}\n\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\"}}\n\n").unwrap();
    assert_eq!(
        owner.feed(b"data: {\"type\":\"message_stop\"}\n\n"),
        Err(provider::Error::CorruptStream)
    );
    assert!(owner.finish(false).is_err());
}

#[test]
fn unrepresentable_usage_fails_closed_and_truncated_server_input_cannot_finish() {
    use std::sync::Arc;
    use tofu_runtime::{
        anthropic::{convert_usage, Stream},
        provider,
    };
    let raw = json!({"input_tokens":"18446744073709551616"});
    assert_eq!(
        convert_usage(raw.as_object().unwrap()),
        Err(Error::Capacity)
    );
    let mut owner = Stream::new(
        provider::Budget {
            event_bytes: 4096,
            output_bytes: 4096,
            tool_calls: 8,
        },
        Budget {
            blocks: 8,
            retained_bytes: 4096,
        },
        5,
        Arc::new(Default::default()),
        BTreeMap::new(),
    )
    .unwrap();
    owner.feed(b"data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"server_tool_use\",\"input\":{}}}\n\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\"}}\n\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"}}\n\n").unwrap();
    assert!(owner.finish(false).is_err());
}
