use serde_json::Value;
use tofu_runtime::progress::{call_signature, Halt, Ledger, Limits, Observation};

#[test]
fn actual_python_decisions_match_native_post_tool_state() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/python_progress.json")).unwrap();
    for sequence in fixture["cases"].as_array().unwrap() {
        let mut ledger = Ledger::default();
        for row in sequence.as_array().unwrap() {
            let calls: Vec<_> = row["calls"]
                .as_array()
                .unwrap()
                .iter()
                .map(|call| {
                    let arguments = tofu_runtime::tool_policy::lossless_arguments(
                        call["function"]["arguments"].as_str().unwrap(),
                    )
                    .unwrap();
                    call_signature(call["function"]["name"].as_str().unwrap(), &arguments).unwrap()
                })
                .collect();
            let strings = |key: &str| {
                row[key]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_owned())
                    .collect()
            };
            let observation = Observation {
                world_version: row["world_version"].as_str().unwrap().into(),
                evidence_ids: strings("evidence_ids"),
                evidence_complete: row["evidence_complete"].as_bool().unwrap(),
                verification: row["verification"].as_str().unwrap().into(),
                nonretryable_failure_signatures: strings("failures"),
            };
            assert_eq!(
                ledger.observe(
                    &calls,
                    observation,
                    Limits {
                        no_progress_rounds: usize::MAX,
                        nonretryable_failure_rounds: usize::MAX
                    }
                ),
                None
            );
            assert_eq!(
                ledger.no_progress_streak,
                row["expected_no_progress"].as_u64().unwrap() as usize,
                "{row}"
            );
            assert_eq!(
                ledger.nonretryable_failure_streak,
                row["expected_failure"].as_u64().unwrap() as usize,
                "{row}"
            );
        }
    }
}

#[test]
fn explicit_limits_and_unknown_results_preserve_breaker_semantics() {
    let calls = vec![[7; 32]];
    let observation = || Observation {
        evidence_ids: vec!["visible".into()],
        evidence_complete: true,
        ..Default::default()
    };
    let limits = Limits {
        no_progress_rounds: 2,
        nonretryable_failure_rounds: 2,
    };
    let mut ledger = Ledger::default();
    assert_eq!(ledger.observe(&calls, observation(), limits), None);
    assert_eq!(ledger.observe(&calls, observation(), limits), None);
    assert_eq!(
        ledger.observe(&calls, observation(), limits),
        Some(Halt::NoProgress)
    );
    assert_eq!(ledger.observe(&calls, Observation::default(), limits), None);
    assert_eq!(ledger.no_progress_streak, 0);
    let failure = || Observation {
        nonretryable_failure_signatures: vec!["permission".into()],
        ..Default::default()
    };
    assert_eq!(ledger.observe(&calls, failure(), limits), None);
    assert_eq!(
        ledger.observe(&[[8; 32]], failure(), limits),
        Some(Halt::NonretryableFailure)
    );
    assert_eq!(ledger.nonretryable_failure_streak, 2);
}

#[test]
fn observation_bytes_cover_world_verification_and_both_evidence_sets() {
    let value = Observation {
        world_version: "v".into(),
        verification: "ok".into(),
        evidence_ids: vec!["result".into()],
        nonretryable_failure_signatures: vec!["failure".into()],
        evidence_complete: true,
    };
    assert!(value.fits_bytes(16));
    assert!(!value.fits_bytes(15));
    let empty = Observation {
        evidence_ids: vec![String::new(); 1000],
        ..Default::default()
    };
    assert!(!empty.fits_bytes(100));
}

#[test]
fn call_ids_and_argument_order_do_not_create_progress() {
    let a = call_signature("read", &serde_json::json!({"a":1,"b":2})).unwrap();
    let b = call_signature("read", &serde_json::json!({"b":2,"a":1})).unwrap();
    assert_eq!(a, b);
    assert_ne!(
        a,
        call_signature("read", &serde_json::json!({"a":1.0,"b":2})).unwrap()
    );
}
