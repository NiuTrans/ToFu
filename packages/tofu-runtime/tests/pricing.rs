//! Resolved cost parity with current Python engine; no external billing effects.
use serde_json::Value;
use tofu_runtime::pricing::{compute, Currency, Error, ResolvedPrice, Tokens};
fn fixture_tokens(v: &Value) -> Tokens {
    Tokens {
        input: v["input"].as_u64().unwrap(),
        output: v["output"].as_u64().unwrap(),
        cache_write: v["cache_write"].as_u64().unwrap(),
        cache_read: v["cache_read"].as_u64().unwrap(),
        thinking: v["thinking"].as_u64().unwrap(),
        uncached: v["uncached"].as_u64().unwrap(),
        total_input: v["total_input"].as_u64().unwrap(),
    }
}
fn fixture_price(v: &Value) -> ResolvedPrice {
    ResolvedPrice {
        selected_prompt_tokens: v["selected_prompt_tokens"].as_u64().unwrap(),
        input: v["input"].as_f64().unwrap(),
        output: v["output"].as_f64().unwrap(),
        cache_write_multiplier: v["cacheWriteMul"].as_f64().unwrap(),
        cache_read_multiplier: v["cacheReadMul"].as_f64().unwrap(),
        usd_to_cny: v["usd_to_cny"].as_f64().unwrap(),
        currency: if v["currency"] == "CNY" {
            Currency::Cny
        } else {
            Currency::Usd
        },
    }
}
#[test]
fn current_python_cost_numeric_components_match_exactly() {
    let fixtures: Value =
        serde_json::from_str(include_str!("fixtures/python_pricing.json")).unwrap();
    let cases = fixtures["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 1710);
    for (index, case) in cases.iter().enumerate() {
        let normalized = tofu_runtime::pricing_usage::normalize(&case["usage"]).unwrap();
        assert_eq!(
            normalized,
            fixture_tokens(&case["tokens"]),
            "normalized case {index}: {case}"
        );
        let actual = compute(
            normalized,
            fixture_price(&case["price"]),
        )
        .unwrap();
        let actual = actual
            .map(|cost| cost.numeric_projection())
            .unwrap_or(Value::Null);
        assert_eq!(actual, case["expected"], "case {index}: {case}");
    }
}
#[test]
fn mismatched_prompt_and_invalid_money_refuse_without_a_quote() {
    let tokens = Tokens {
        input: 100,
        output: 1,
        cache_write: 0,
        cache_read: 0,
        thinking: 0,
        uncached: 100,
        total_input: 100,
    };
    let price = ResolvedPrice {
        selected_prompt_tokens: 100,
        input: 1.,
        output: 1.,
        cache_write_multiplier: 1.,
        cache_read_multiplier: 0.1,
        usd_to_cny: 7.24,
        currency: Currency::Usd,
    };
    assert_eq!(
        compute(
            tokens,
            ResolvedPrice {
                selected_prompt_tokens: 101,
                ..price
            }
        )
        .unwrap_err(),
        Error::PromptBinding
    );
    for bad in [f64::NAN, f64::INFINITY, -1.] {
        assert_eq!(
            compute(
                tokens,
                ResolvedPrice {
                    input: bad,
                    ..price
                }
            )
            .unwrap_err(),
            Error::InvalidPrice
        );
    }
    for bad in [f64::NAN, f64::INFINITY, -1., 0.] {
        assert_eq!(
            compute(
                tokens,
                ResolvedPrice {
                    usd_to_cny: bad,
                    ..price
                }
            )
            .unwrap_err(),
            Error::InvalidPrice
        );
    }
    assert_eq!(
        compute(
            tokens,
            ResolvedPrice {
                input: f64::MAX,
                ..price
            }
        )
        .unwrap_err(),
        Error::NonFiniteCost
    );
    assert_eq!(
        compute(
            Tokens {
                uncached: 99,
                ..tokens
            },
            price
        )
        .unwrap_err(),
        Error::InvalidSplit
    );
}

#[test]
fn zero_usage_cannot_hide_invalid_split_or_snapshot() {
    let tokens = Tokens {
        input: 0,
        output: 0,
        cache_write: 0,
        cache_read: 0,
        thinking: 0,
        uncached: 0,
        total_input: 0,
    };
    let price = ResolvedPrice {
        selected_prompt_tokens: 0,
        input: 1.,
        output: 1.,
        cache_write_multiplier: 1.,
        cache_read_multiplier: 0.1,
        usd_to_cny: 7.24,
        currency: Currency::Usd,
    };
    assert!(compute(tokens, price).unwrap().is_none());
    assert_eq!(
        compute(
            Tokens {
                total_input: 1,
                ..tokens
            },
            ResolvedPrice {
                selected_prompt_tokens: 1,
                ..price
            }
        )
        .unwrap_err(),
        Error::InvalidSplit
    );
    for invalid in [
        ResolvedPrice {
            output: f64::NAN,
            ..price
        },
        ResolvedPrice {
            cache_write_multiplier: f64::INFINITY,
            ..price
        },
        ResolvedPrice {
            cache_read_multiplier: -1.,
            ..price
        },
    ] {
        assert_eq!(compute(tokens, invalid).unwrap_err(), Error::InvalidPrice);
    }
}

#[test]
fn python_unicode_decimal_and_integer_whitespace_match_without_allocating_a_string() {
    let fixtures: Value =
        serde_json::from_str(include_str!("fixtures/python_pricing.json")).unwrap();
    let cases = fixtures["numeric_text_cases"].as_array().unwrap();
    assert!(cases.len() > 600);
    for case in cases {
        let usage = serde_json::json!({"prompt_tokens":case["text"]});
        let tokens = tofu_runtime::pricing_usage::normalize(&usage).unwrap();
        assert_eq!(tokens.input, case["expected"].as_u64().unwrap(), "{case}");
        assert_eq!(tokens.uncached, tokens.input);
        assert_eq!(tokens.total_input, tokens.input);
    }
}
#[test]
fn malformed_negative_or_wide_selected_evidence_never_becomes_free_tokens() {
    use serde_json::json;
    use tofu_runtime::pricing_usage::{normalize, Error as UsageError};
    for value in [json!(-1), json!(-1.5), json!("-1")] {
        assert_eq!(
            normalize(&json!({"prompt_tokens":value})).unwrap_err(),
            UsageError::NegativeCounter
        );
    }
    for value in [
        json!("bad"),
        json!("1__2"),
        json!("1_"),
        json!("_1"),
        json!(" "),
        json!([1]),
        json!({"tokens":1}),
        json!("\u{001c}1"),
    ] {
        assert_eq!(
            normalize(&json!({"prompt_tokens":value,"input_tokens":100})).unwrap_err(),
            UsageError::InvalidCounter
        );
    }
    assert_eq!(
        normalize(&json!({"prompt_tokens":"Ⅻ"})).unwrap_err(),
        UsageError::UnsupportedNumericText
    );
    for value in [json!("18446744073709551616"), json!(18446744073709551616.0)] {
        assert_eq!(
            normalize(&json!({"prompt_tokens":value})).unwrap_err(),
            UsageError::CounterOverflow
        );
    }
    assert_eq!(
        normalize(&json!({"input_tokens":u64::MAX,"cache_read_input_tokens":1})).unwrap_err(),
        UsageError::CounterOverflow
    );
}
#[test]
fn cost_normalization_cannot_assert_financial_completeness_or_mutate_provider_usage() {
    let usage = serde_json::json!({"prompt_tokens":null,"completion_tokens":5,"cached_tokens":100});
    let before = usage.clone();
    let tokens = tofu_runtime::pricing_usage::normalize(&usage).unwrap();
    assert_eq!(tokens.total_input, 100);
    let observation = tofu_runtime::usage::observed_totals(&usage).unwrap();
    assert!(!observation.complete);
    assert_eq!(usage, before);
}
