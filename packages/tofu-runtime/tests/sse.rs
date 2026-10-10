//! Actual canonical Python oracle replayed across arbitrary transport chunking.
use serde_json::{json, Value};
use tofu_runtime::sse::{Error, Framer, Issue};
#[test]
fn actual_python_oracle_survives_transport_fragmentation() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_sse_frames.json")).unwrap();
    for (case_index, c) in cases.as_array().unwrap().iter().enumerate() {
        let wire: Vec<u8> = c["wire"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        for size in [1, 2, 3, 7, 64, usize::MAX] {
            let mut f = Framer::new(c["budget"].as_u64().unwrap() as usize).unwrap();
            let mut events = Vec::new();
            let mut count = 0;
            let mut diagnostics = Vec::new();
            for chunk in wire.chunks(size) {
                f.feed_until(chunk,|e,i|{count+=i.count;diagnostics.extend(i.diagnostics);events.push(json!({"data":e.data,"event":e.event,"event_id":e.event_id,"retry_ms":e.retry_ms}));false}).unwrap();
            }
            f.finalize();
            let issues = f.drain_issues();
            count += issues.count;
            diagnostics.extend(issues.diagnostics);
            assert_eq!(
                Value::Array(events),
                c["events"],
                "case {case_index}, chunk {size}"
            );
            assert_eq!(
                count,
                c["issue_count"].as_u64().unwrap(),
                "case {case_index}, chunk {size}"
            );
            // Native drains at delivery boundaries; count is authoritative. Diagnostic
            // kinds are checked separately because each drain gets its own bounded set.
            assert!(diagnostics.iter().all(|i| matches!(
                i,
                Issue::InvalidUtf8 | Issue::EventTooLarge | Issue::TruncatedEvent
            )));
        }
    }
}
#[test]
fn terminal_cutoff_never_inspects_malformed_trailer() {
    let mut f = Framer::new(128).unwrap();
    let bytes = b"data: [DONE]\r\n\r\n\xffdata: bad";
    let consumed = f
        .feed_until(bytes, |e, i| {
            assert_eq!(e.data, "[DONE]");
            assert_eq!(i.count, 0);
            true
        })
        .unwrap();
    assert_eq!(consumed, b"data: [DONE]\r\n\r".len());
    assert_eq!(f.drain_issues().count, 0);
    f.finalize();
    assert_eq!(f.drain_issues().count, 0);
    assert_eq!(f.feed_until(b"", |_, _| false), Err(Error::Closed));
}
#[test]
fn intentional_close_discards_partial_utf8_without_inventing_eof() {
    let mut f = Framer::new(128).unwrap();
    f.feed_until(b"data: \xff\xf0\x9f", |_, _| false).unwrap();
    f.close();
    f.finalize();
    let i = f.drain_issues();
    assert_eq!(i.count, 1);
    assert_eq!(i.diagnostics, vec![Issue::InvalidUtf8]);
}
#[test]
fn diagnostics_remain_bounded_during_large_invalid_and_oversized_input() {
    let mut f = Framer::new(16).unwrap();
    f.feed_until(&vec![0xff; 100_000], |_, _| false).unwrap();
    let i = f.drain_issues();
    assert_eq!(i.count, 100_000);
    assert_eq!(i.diagnostics.len(), 4);
    let mut f = Framer::new(16).unwrap();
    f.feed_until(&vec![b'x'; 100_000], |_, _| false).unwrap();
    f.finalize();
    let i = f.drain_issues();
    assert_eq!(
        i.diagnostics,
        vec![Issue::EventTooLarge, Issue::TruncatedEvent]
    );
}
#[test]
fn consumer_unwind_retires_framer_and_preserves_prior_faults() {
    let mut f = Framer::new(128).unwrap();
    let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        f.feed_until(b"data: first\n\ndata: tail", |_, _| {
            panic!("consumer rejected")
        })
    }));
    assert!(panic.is_err());
    assert_eq!(f.feed_until(b"", |_, _| false), Err(Error::Closed));
    f.finalize();
    assert_eq!(f.drain_issues().count, 0);
}
