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

#[test]
fn multiline_payload_preserves_empty_values_and_owned_delivery_at_every_split() {
    let wire = "data\r\ndata:  中文\ndata:\rid: one\n: comment\nevent: delta\ndata: tail\n\ndata: next\n\n";
    for split in 0..=wire.len() {
        let mut framer = Framer::new(256).unwrap();
        let mut events = Vec::new();
        for chunk in [&wire.as_bytes()[..split], &wire.as_bytes()[split..]] {
            framer
                .feed_until(chunk, |event, issues| {
                    assert_eq!(issues.count, 0);
                    events.push(event);
                    false
                })
                .unwrap();
        }
        framer.finalize();
        assert_eq!(framer.drain_issues().count, 0, "split {split}");
        assert_eq!(events.len(), 2, "split {split}");
        assert_eq!(events[0].data, "\n 中文\n\ntail", "split {split}");
        assert_eq!(events[0].event, "delta");
        assert_eq!(events[0].event_id, "one");
        assert_eq!(events[1].data, "next");
        assert_eq!(events[1].event, "");
        assert_eq!(events[1].event_id, "");
    }
}

#[test]
fn multiline_budget_counts_wire_bytes_and_discards_only_the_rejected_event() {
    // Each nonempty wire line includes one normalized newline in the budget.
    let wire = b"data: a\ndata: b\n\ndata: ok\n\n";
    for budget in [15, 16] {
        for chunk_size in 1..=wire.len() {
            let mut framer = Framer::new(budget).unwrap();
            let mut events = Vec::new();
            let mut issues = Vec::new();
            for chunk in wire.chunks(chunk_size) {
                framer
                    .feed_until(chunk, |event, pending| {
                        events.push(event.data);
                        issues.extend(pending.diagnostics);
                        false
                    })
                    .unwrap();
            }
            framer.finalize();
            assert_eq!(framer.drain_issues().count, 0);
            if budget == 15 {
                assert_eq!(events, ["ok"]);
                assert_eq!(issues, [Issue::EventTooLarge]);
            } else {
                assert_eq!(events, ["a\nb", "ok"]);
                assert!(issues.is_empty());
            }
        }
    }
}

#[test]
fn delivered_payload_does_not_retain_line_growth_capacity() {
    for bytes in [0, 1, 8, 1024, 65536] {
        let data = "x".repeat(bytes);
        let wire = format!("data: {data}\n\n");
        let mut framer = Framer::new(wire.len()).unwrap();
        let mut delivered = None;
        framer
            .feed_until(wire.as_bytes(), |event, issues| {
                assert_eq!(issues.count, 0);
                delivered = Some(event);
                false
            })
            .unwrap();
        framer.close();
        let delivered = delivered.unwrap();
        assert_eq!(delivered.data, data);
        assert_eq!(delivered.data.capacity(), delivered.data.len());
    }
}

#[test]
fn malformed_and_oversized_streams_are_invariant_under_chunking() {
    use tofu_runtime::sse::{Event, Issues};
    fn observe(wire: &[u8], budget: usize, chunk: usize) -> (Vec<(Event, Issues)>, Issues) {
        let mut framer = Framer::new(budget).unwrap();
        let mut events = Vec::new();
        for bytes in wire.chunks(chunk) {
            framer
                .feed_until(bytes, |event, issues| {
                    events.push((event, issues));
                    false
                })
                .unwrap();
        }
        framer.finalize();
        (events, framer.drain_issues())
    }
    let mut seed = 0x5eeda11u32;
    let alphabet = b"data: id event retry 01234\n\r\0\xef\xbb\xbf\xe0\xed\xf0\xf4\x80\x9f\xff";
    for case in 0..512 {
        let mut wire = "\u{feff}data: initial ASCII line with 中文\r\n"
            .as_bytes()
            .to_vec();
        for _ in 0..case % 193 {
            seed ^= seed << 13;
            seed ^= seed >> 17;
            seed ^= seed << 5;
            wire.push(alphabet[seed as usize % alphabet.len()]);
        }
        wire.extend_from_slice(b"\r\n\r\ndata: resynchronized ASCII event\n\ndata: unfinished");
        let budget = [1, 16, 32, 128, 512][case % 5];
        let expected = observe(&wire, budget, 1);
        for chunk in [2, 7, 15, 16, 17, 31, 64, wire.len()] {
            assert_eq!(
                observe(&wire, budget, chunk),
                expected,
                "case {case}, chunk {chunk}"
            );
        }
    }
}
