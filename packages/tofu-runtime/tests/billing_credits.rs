//! Exact canonical component rounding, margin truncation and overflow refusal.
use serde_json::{json, Value};
use tofu_runtime::{
    billing_credits::{convert, Error},
    pricing::{compute, Cost, Currency, ResolvedPrice, Tokens},
};
fn zero_cost() -> Cost {
    compute(
        Tokens {
            input: 1,
            output: 0,
            cache_write: 0,
            cache_read: 0,
            thinking: 0,
            uncached: 1,
            total_input: 1,
        },
        ResolvedPrice {
            selected_prompt_tokens: 1,
            input: 0.,
            output: 0.,
            cache_write_multiplier: 0.,
            cache_read_multiplier: 0.,
            usd_to_cny: 1.,
            currency: Currency::Usd,
        },
    )
    .unwrap()
    .unwrap()
}
#[test]
fn current_python_billing_components_and_margins_match() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/python_pricing.json")).unwrap();
    let cases = fixture["billing_cases"].as_array().unwrap();
    assert!(cases.len() > 8000);
    for (index, case) in cases.iter().enumerate() {
        let cost = if case["cost"].is_null() {
            None
        } else {
            Some(Cost {
                input_usd: case["cost"]["inputCostUsd"].as_f64().unwrap(),
                output_usd: case["cost"]["outputCostUsd"].as_f64().unwrap(),
                cache_read_usd: case["cost"]["cacheReadCostUsd"].as_f64().unwrap(),
                cache_write_usd: case["cost"]["cacheWriteCostUsd"].as_f64().unwrap(),
                ..zero_cost()
            })
        };
        if let Some(source_index) = case["source_index"].as_u64() {
            let source = &fixture["cases"][source_index as usize];
            let v = &source["price"];
            let native = compute(
                tofu_runtime::pricing_usage::normalize(&source["usage"]).unwrap(),
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
                },
            )
            .unwrap();
            assert_eq!(
                convert(native.as_ref(), case["margin"].as_f64().unwrap()).unwrap(),
                convert(cost.as_ref(), case["margin"].as_f64().unwrap()).unwrap(),
                "raw to credits {index}"
            );
        }
        let credits = convert(cost.as_ref(), case["margin"].as_f64().unwrap()).unwrap();
        assert_eq!(
            json!({"input":credits.input,"output":credits.output,"cache_read":credits.cache_read,"cache_write":credits.cache_write,"base":credits.base,"margin":credits.margin,"total":credits.total}),
            case["expected"],
            "{index}: {case}"
        );
    }
}
#[test]
fn invalid_components_margins_and_all_overflow_paths_refuse() {
    for value in [f64::NAN, f64::INFINITY, -1.] {
        assert_eq!(
            convert(
                Some(&Cost {
                    input_usd: value,
                    ..zero_cost()
                }),
                0.
            ),
            Err(Error::InvalidComponent)
        );
        assert_eq!(convert(None, value), Err(Error::InvalidMargin));
    }
    assert_eq!(
        convert(
            Some(&Cost {
                input_usd: f64::MAX,
                ..zero_cost()
            }),
            0.
        ),
        Err(Error::Overflow)
    );
    assert_eq!(
        convert(
            Some(&Cost {
                input_usd: 1e13,
                output_usd: 1e13,
                ..zero_cost()
            }),
            0.
        ),
        Err(Error::Overflow)
    );
    assert_eq!(
        convert(
            Some(&Cost {
                input_usd: 1e13,
                ..zero_cost()
            }),
            1.
        ),
        Err(Error::Overflow)
    );
    assert_eq!(
        convert(
            Some(&Cost {
                input_usd: 1.,
                ..zero_cost()
            }),
            f64::MAX
        ),
        Err(Error::Overflow)
    );
}
