//! Actual canonical Python observation oracle, numeric bounds and no mutation.
use serde_json::{json, Value};
use tofu_runtime::usage::{observed_totals, Error};
#[test]
fn actual_python_usage_observations_match() {
    let fixtures: Value =
        serde_json::from_str(include_str!("fixtures/python_usage_observations.json")).unwrap();
    for (index, case) in fixtures["cases"].as_array().unwrap().iter().enumerate() {
        let before = case["usage"].clone();
        let result = observed_totals(&case["usage"]).unwrap();
        assert_eq!(
            result.tokens,
            case["tokens"].as_u64().unwrap(),
            "{index}: {case}"
        );
        assert_eq!(
            result.complete,
            case["complete"].as_bool().unwrap(),
            "{index}: {case}"
        );
        assert_eq!(case["usage"], before);
    }
}
#[test]
fn unsupported_wide_or_non_ascii_numeric_evidence_never_becomes_zero() {
    for usage in [
        json!({"input_tokens":"18446744073709551616","output_tokens":0}),
        json!({"prompt_tokens":u64::MAX,"completion_tokens":1}),
        json!({"input_tokens":1,"output_tokens":0,"cache_read_input_tokens":u64::MAX}),
    ] {
        assert_eq!(observed_totals(&usage), Err(Error::CounterOverflow));
    }
    assert_eq!(
        observed_totals(&json!({"prompt_tokens":"١٢","completion_tokens":0})),
        Err(Error::UnsupportedNumericText)
    );
    let max = observed_totals(&json!({"prompt_tokens":u64::MAX,"completion_tokens":0})).unwrap();
    assert_eq!(max.tokens, u64::MAX);
    assert!(max.complete);
}

#[test]
fn host_observation_schema_accepts_supported_and_unknown_counts() {
    use tofu_runtime::{tool_contract::Catalog, usage::evidence};
    let schema: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_agent_stdio_v1.schema.json"
    ))
    .unwrap();
    let catalog=Catalog::compile(7,"usage",&json!({"usage_observation":{"name":"usage_observation","contractVersion":tofu_runtime::execution_contract::TOOL_CONTRACT_VERSION,"arguments_schema":schema["$defs"]["usage_observation"]}})).unwrap();
    for raw in [
        json!({"prompt_tokens":0,"completion_tokens":0}),
        json!({}),
        json!({"prompt_tokens":"18446744073709551616"}),
        json!({"prompt_tokens":"١٢"}),
    ] {
        catalog
            .validate(7, "usage_observation", evidence(&raw))
            .unwrap();
    }
    assert_eq!(
        evidence(&json!({"prompt_tokens":100,"completion_tokens":5,"cached_tokens":100})),
        json!({"tokens":105,"complete":true,"error":null})
    );
    assert_eq!(
        evidence(&json!({"prompt_tokens":"18446744073709551616"})),
        json!({"tokens":null,"complete":false,"error":"counter_overflow"})
    );
}
