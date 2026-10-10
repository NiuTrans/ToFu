//! Canonical Python Responses oracle; no network/provider effects.
use serde_json::Value;
use std::collections::BTreeMap;
use tofu_runtime::responses::{Budget, Error, Projection, Translator};
#[test]
fn actual_python_responses_translation_and_replay_match() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_responses_stream.json")).unwrap();
    for case in cases.as_array().unwrap() {
        let mut owner = Translator::new(
            Budget {
                items: 128,
                retained_bytes: 1 << 20,
            },
            BTreeMap::from([("short".into(), "long_tool_name".into())]),
        )
        .unwrap();
        let mut out = vec![];
        let mut failed = false;
        for event in case["events"].as_array().unwrap() {
            match owner.translate(event) {
                Ok(items) => {
                    for item in items {
                        match item {
                            Projection::Chunk(value) => out.push(value),
                            Projection::Done => out.push("[DONE]".into()),
                        }
                    }
                }
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
        assert_eq!(Value::Array(out), case["outputs"], "{}", case["name"]);
        if !failed {
            assert_eq!(
                Value::Array(owner.replay_items()),
                case["replay"],
                "{}",
                case["name"]
            );
            assert_eq!(
                owner.response_id(),
                case["response_id"].as_str().unwrap(),
                "{}",
                case["name"]
            );
        }
    }
}

#[test]
fn byte_fragmentation_preserves_tools_replay_and_exact_terminal_cutoff() {
    use serde_json::json;
    use std::{collections::BTreeSet, sync::Arc};
    use tofu_runtime::{execution_contract::ProviderStreamState, provider, responses::Stream};
    let events = [
        json!({"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item","call_id":"call","name":"read","arguments":""}}),
        json!({"type":"response.function_call_arguments.delta","item_id":"item","delta":"{\"path\":"}),
        json!({"type":"response.output_text.delta","delta":"你好"}),
        json!({"type":"response.completed","response":{"id":"response","output":[{"type":"function_call","id":"item","call_id":"call","name":"read","arguments":"{\"path\":\"file\"}"},{"type":"reasoning","id":"reasoning","encrypted_content":"opaque"},{"type":"message","role":"assistant","content":[{"type":"output_text","text":"你好😀"}],"phase":"final_answer"}],"usage":{"input_tokens":5,"output_tokens":7}}}),
    ];
    let mut wire = events
        .iter()
        .map(|v| format!("data: {v}\n\n"))
        .collect::<String>()
        .into_bytes();
    wire.extend_from_slice(b"data: {invalid\xff\n\n");
    for size in [1, 3, 7, 64, 65536] {
        let mut stream = Stream::new(
            provider::Budget {
                event_bytes: 65536,
                output_bytes: 65536,
                tool_calls: 8,
            },
            Budget {
                items: 32,
                retained_bytes: 65536,
            },
            5,
            Arc::new(BTreeSet::from(["read".into()])),
            BTreeMap::new(),
        )
        .unwrap();
        let mut spans = vec![];
        for part in wire.chunks(size) {
            if stream
                .feed_with_text(part, |span| spans.push(span))
                .unwrap()
            {
                break;
            }
        }
        assert_eq!(stream.response_id(), "response");
        assert_eq!(
            spans
                .iter()
                .map(|span| stream.span_text(*span).unwrap())
                .collect::<String>(),
            "你好😀"
        );
        let result = stream.finish(false).unwrap();
        assert_eq!(result.state, ProviderStreamState::ProviderFinished);
        assert_eq!(result.message["content"], "你好😀");
        assert_eq!(
            result.message["tool_calls"][0]["function"]["arguments"],
            "{\"path\":\"file\"}"
        );
        assert_eq!(
            result.message["_responses_items"].as_array().unwrap().len(),
            2
        );
        assert_eq!(result.malformed_frames, 0);
        assert_eq!(result.usage["total_tokens"], 12);
    }
}
#[test]
fn bounded_errors_and_eof_never_grant_completion() {
    use serde_json::json;
    use std::sync::Arc;
    use tofu_runtime::{execution_contract::ProviderStreamState, provider, responses::Stream};
    let new = || {
        Stream::new(
            provider::Budget {
                event_bytes: 4096,
                output_bytes: 4096,
                tool_calls: 8,
            },
            Budget {
                items: 8,
                retained_bytes: 4096,
            },
            5,
            Arc::new(Default::default()),
            BTreeMap::new(),
        )
        .unwrap()
    };
    let mut stream = new();
    stream
        .feed(b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n")
        .unwrap();
    let result = stream.finish(false).unwrap();
    assert_eq!(result.state, ProviderStreamState::PrematureClose);
    assert_eq!(result.message["content"], "partial");
    let mut stream = new();
    assert_eq!(stream.feed(b"data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"code\":\"quota\",\"message\":\"failure\",\"http_status\":429}}}\n\n"),Err(provider::Error::ProviderError));
    assert_eq!(stream.provider_error().unwrap()["http_status"], 429);
    assert!(stream.finish(false).is_err());
    let mut owner = Translator::new(
        Budget {
            items: 1,
            retained_bytes: 4096,
        },
        BTreeMap::new(),
    )
    .unwrap();
    owner.translate(&json!({"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item","call_id":"call","name":"read","arguments":"{}"}})).unwrap();
    assert_eq!(owner.translate(&json!({"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","id":"second","call_id":"second","name":"read","arguments":"{}"}})),Err(Error::Capacity));
    assert_eq!(
        owner.translate(&json!({"type":"response.completed","response":{}})),
        Err(Error::Closed)
    );
}

#[test]
fn unicode_identity_bytes_are_charged_before_retaining_aliases() {
    use serde_json::json;
    let mut owner = Translator::new(
        Budget {
            items: 1,
            retained_bytes: 2500,
        },
        BTreeMap::new(),
    )
    .unwrap();
    let event = json!({"type":"response.output_item.added","item":{"type":"function_call","id":"😀".repeat(512),"call_id":"call","name":"read","arguments":"{}"}});
    assert!(serde_json::to_vec(&event).unwrap().len() < 2500);
    assert_eq!(owner.translate(&event), Err(Error::Capacity));
    assert_eq!(
        owner.translate(&json!({"type":"response.completed","response":{}})),
        Err(Error::Closed)
    );
}

#[test]
fn replay_snapshots_are_detached_and_unknown_items_cannot_complete() {
    use serde_json::json;
    let mut owner = Translator::new(
        Budget {
            items: 8,
            retained_bytes: 4096,
        },
        BTreeMap::new(),
    )
    .unwrap();
    owner.translate(&json!({"type":"response.completed","response":{"output":[{"type":"reasoning","id":"reason","encrypted_content":"opaque","summary":[{"text":"original"}]}]}})).unwrap();
    let mut snapshot = owner.replay_items();
    snapshot[0]["summary"][0]["text"] = "changed".into();
    assert_eq!(owner.replay_items()[0]["summary"][0]["text"], "original");
    let mut owner = Translator::new(
        Budget {
            items: 8,
            retained_bytes: 4096,
        },
        BTreeMap::new(),
    )
    .unwrap();
    assert_eq!(owner.translate(&json!({"type":"response.completed","response":{"output":[{"type":"future_executable_item","payload":"opaque"}]}})),Err(Error::UnsupportedItem));
    assert_eq!(
        owner.translate(&json!({"type":"response.completed","response":{}})),
        Err(Error::Closed)
    );
}

#[test]
fn unknown_message_content_cannot_disappear_into_success() {
    use serde_json::json;
    let mut owner = Translator::new(
        Budget {
            items: 8,
            retained_bytes: 4096,
        },
        BTreeMap::new(),
    )
    .unwrap();
    assert_eq!(owner.translate(&json!({"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_audio","data":"opaque"}]}]}})),Err(Error::UnsupportedItem));
}

#[test]
fn replayed_hidden_position_does_not_overbook_item_or_byte_admission() {
    use serde_json::json;
    let mut owner = Translator::new(
        Budget {
            items: 1,
            retained_bytes: 4096,
        },
        BTreeMap::new(),
    )
    .unwrap();
    let event = json!({"type":"response.output_item.added","output_index":0,"item":{"type":"message","agent":{"agent_name":"/child"},"phase":"final_answer","content":[]}});
    owner.translate(&event).unwrap();
    let charge = owner.charged_bytes();
    assert!(charge > 0);
    owner.translate(&event).unwrap();
    assert_eq!(owner.charged_bytes(), charge);
}
