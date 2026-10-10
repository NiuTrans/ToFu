//! Canonical provider evidence and native task composition parity.
use serde_json::{json, Value};
use tofu_runtime::{
    execution_contract::{classify_stream, ProviderStreamState, StreamFacts},
    provider::{Budget, ChatStream, Error},
};
fn budget() -> Budget {
    Budget {
        event_bytes: 1 << 20,
        output_bytes: 1 << 20,
        tool_calls: 128,
    }
}
#[test]
fn actual_python_stream_results_match_native_chunking() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_provider_stream.json")).unwrap();
    for (case_index, c) in cases.as_array().unwrap().iter().enumerate() {
        let wire: Vec<u8> = c["wire"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        for size in [1, 3, 7, 64, 65536] {
            let mut stream = ChatStream::new(budget()).unwrap();
            for chunk in wire.chunks(size) {
                if stream.feed(chunk).unwrap() {
                    break;
                }
            }
            let result = stream.finish(c["aborted"].as_bool().unwrap()).unwrap();
            assert_eq!(
                result.state.as_str(),
                c["state"].as_str().unwrap(),
                "case {case_index}, size {size}"
            );
            assert_eq!(
                result.message, c["message"],
                "case {case_index}, size {size}"
            );
            assert_eq!(result.usage, c["usage"]);
            assert_eq!(
                result.finish_reason,
                c["finish"].as_str().map(str::to_owned)
            );
            assert_eq!(result.done_seen, c["done"].as_bool().unwrap());
            assert_eq!(result.malformed_frames, c["malformed"].as_u64().unwrap());
        }
    }
}
#[test]
fn finish_without_done_is_evidence_but_done_alone_is_not() {
    assert_eq!(
        classify_stream(&StreamFacts {
            saw_finish_reason: true,
            ..Default::default()
        }),
        ProviderStreamState::ProviderFinished
    );
    assert_eq!(
        classify_stream(&StreamFacts::default()),
        ProviderStreamState::PrematureClose
    );
    assert_eq!(
        classify_stream(&StreamFacts {
            aborted: true,
            malformed_frame_count: 1,
            ..Default::default()
        }),
        ProviderStreamState::ClientAborted
    );
}
#[test]
fn bounded_output_and_tool_admission_fail_before_task_publication() {
    let mut b = budget();
    b.output_bytes = 3;
    let mut stream = ChatStream::new(b).unwrap();
    assert_eq!(
        stream.feed(b"data: {\"choices\":[{\"delta\":{\"content\":\"long\"}}]}\n\n"),
        Err(Error::OutputLimit)
    );
    assert!(matches!(stream.finish(false), Err(Error::OutputLimit)));
    let mut b = budget();
    b.tool_calls = 1;
    let mut stream = ChatStream::new(b).unwrap();
    let wire = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a","function":{"name":"x"}},{"index":1,"id":"b","function":{"name":"y"}}]}}]});
    assert_eq!(
        stream.feed(format!("data: {wire}\n\n").as_bytes()),
        Err(Error::ToolLimit)
    );
}
#[test]
fn malformed_trailer_after_done_does_not_contaminate_completion() {
    let mut stream = ChatStream::new(budget()).unwrap();
    assert!(stream.feed(b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n\xff").unwrap());
    assert!(stream.feed(b"\xff").unwrap());
    let result = stream.finish(false).unwrap();
    assert_eq!(result.state, ProviderStreamState::ProviderFinished);
    assert_eq!(result.malformed_frames, 0);
}
#[test]
fn truncation_and_provider_errors_cannot_become_success() {
    let mut stream = ChatStream::new(budget()).unwrap();
    stream.feed(b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: {unfinished").unwrap();
    assert_eq!(
        stream.finish(false).unwrap().state,
        ProviderStreamState::MalformedStream
    );
    let mut stream = ChatStream::new(budget()).unwrap();
    assert_eq!(
        stream.feed(b"data: {\"error\":{\"message\":\"CANARY\"}}\n\n"),
        Err(Error::ProviderError)
    );
    assert!(matches!(stream.finish(false), Err(Error::ProviderError)));
}
#[test]
fn parsed_result_drives_native_resource_settlement_and_terminal_publication() {
    use std::sync::{Arc, Mutex};
    use tofu_runtime::{
        execution_contract::ResourceName,
        session::{Registry, ReleaseAck},
        task::*,
    };
    struct Publisher(Mutex<Vec<Value>>);
    impl TerminalPublisher for Publisher {
        fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
            self.0.lock().unwrap().push(event.clone());
            Ok(())
        }
    }
    for complete in [true, false] {
        let r = Registry::new();
        let s = r.start("provider", "task", 7, "", None).unwrap();
        s.hold(
            ResourceName::Upstream,
            200,
            false,
            Box::new(|_| Ok(ReleaseAck::Released)),
        )
        .unwrap();
        let publisher = Arc::new(Publisher(Mutex::new(Vec::new())));
        let task = Task::new(
            TaskIdentity {
                task_id: "provider".into(),
                owner_user_id: 7,
            },
            s.clone(),
            None,
            publisher.clone(),
        )
        .unwrap();
        let candidate=task.run(7,|_|{let mut stream=ChatStream::new(budget()).unwrap();let finish=if complete{",\"finish_reason\":\"stop\""}else{""};stream.feed(format!("data: {{\"choices\":[{{\"delta\":{{\"content\":\"ok\"}}{finish}}}]}}\n\n").as_bytes()).unwrap();stream.finish(false).unwrap().into_task_result()}).unwrap();
        assert_eq!(
            candidate.status,
            if complete {
                TaskStatus::Done
            } else {
                TaskStatus::Error
            }
        );
        assert!(s.snapshot().unwrap().held_resources.is_empty());
        assert_eq!(publisher.0.lock().unwrap().len(), 1);
    }
}
#[test]
fn parse_error_threshold_and_unindexed_tool_identity_are_preserved() {
    let mut s = ChatStream::new(budget()).unwrap();
    for _ in 0..9 {
        assert_eq!(s.feed(b"data: broken\n\n"), Ok(false));
    }
    assert_eq!(s.feed(b"data: broken\n\n"), Err(Error::CorruptStream));
    let mut s = ChatStream::new(budget()).unwrap();
    assert_eq!(s.feed(b"data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"function\":{\"name\":\"x\"}}]}}]}\n\n"),Ok(false));
}
