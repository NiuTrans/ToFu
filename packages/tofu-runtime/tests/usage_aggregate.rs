//! Python canonical accumulation parity and explicit native numeric boundaries.
use serde_json::{json, Value};
use tofu_runtime::usage_aggregate::{aggregate, aggregate_json, Error};

#[test]
fn canonical_python_rounds_match_without_retaining_provider_evidence() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/python_usage_aggregate.json")).unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let rounds = case["rounds"].as_array().unwrap();
        let before = rounds.clone();
        assert_eq!(Value::Object(aggregate(rounds).unwrap()), case["expected"]);
        assert_eq!(rounds, &before);
        let bytes = serde_json::to_vec(rounds).unwrap();
        assert_eq!(
            Value::Object(aggregate_json(&bytes).unwrap()),
            case["expected"]
        );
    }
}

#[test]
fn integer_overflow_is_explicit_even_in_nested_usage_details() {
    for rounds in [
        vec![json!({"input_tokens":u64::MAX}), json!({"input_tokens":1})],
        vec![
            json!({"cache_creation":{"tokens":u64::MAX}}),
            json!({"cache_creation":{"tokens":1}}),
        ],
    ] {
        let before = rounds.clone();
        assert_eq!(aggregate(&rounds), Err(Error::CounterOverflow));
        assert_eq!(rounds, before);
    }
}

#[test]
fn raw_integer_width_is_checked_before_any_float_conversion() {
    for raw in [
        r#"[{"x":18446744073709551616}]"#,
        r#"[{"x":0.0},{"x":18446744073709551616}]"#,
        r#"[{"cache_creation":{"x":18446744073709551616}}]"#,
        r#"[{"x":18446744073709551615},{"x":1}]"#,
    ] {
        assert_eq!(aggregate_json(raw.as_bytes()), Err(Error::CounterOverflow));
    }
    let exact = aggregate_json(br#"[{"x":18446744073709551615}]"#).unwrap();
    assert_eq!(exact["x"].as_u64(), Some(u64::MAX));
}

#[test]
fn raw_opaque_negative_duplicate_and_float_semantics_are_preserved() {
    let raw = br#"[{"x":18446744073709551616,"x":3,"negative":-99999999999999999999999999999999,"zero":-0,"float":-0.0,"exponent":1e0,"overflow":1e999,"tiny":1e-999,"text":"18446744073709551616","_private":18446744073709551616,"opaque":{"x":18446744073709551616},"cache_creation":{"nested":{"x":18446744073709551616},"tokens":2},"\u005fhidden":18446744073709551616}]"#;
    let result = aggregate_json(raw).unwrap();
    assert_eq!(
        Value::Object(result.clone()),
        json!({"x":3,"zero":0,"float":0.0,"exponent":1.0,"tiny":0.0,"cache_creation":{"tokens":2}})
    );
    assert!(result["zero"].as_number().unwrap().is_u64());
    assert!(result["float"].as_number().unwrap().is_f64());
    assert!(result["exponent"].as_number().unwrap().is_f64());
    assert_eq!(
        aggregate_json(br#"[{"x":1e308},{"x":1e308}]"#).unwrap()["x"],
        json!(1e308)
    );
}

#[test]
fn malformed_or_non_round_json_returns_no_projection() {
    for raw in ["[", "[{\"x\":1},", "[01]", "[] trailing"] {
        assert_eq!(aggregate_json(raw.as_bytes()), Err(Error::InvalidJson));
    }
    for raw in ["{}", "null", "1", "true"] {
        assert_eq!(aggregate_json(raw.as_bytes()), Err(Error::InvalidRounds));
    }
}
