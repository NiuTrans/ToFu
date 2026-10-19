//! Current Python normalized tier selection and complete native fee parity.
use serde_json::Value;
use tofu_runtime::{
    pricing::Currency,
    pricing_tiers::{Card, Error, Rates, Tier},
};
fn rates(v: &Value) -> Rates {
    Rates {
        input: v["input"].as_f64().unwrap(),
        output: v["output"].as_f64().unwrap(),
        cache_write_multiplier: v["cacheWriteMul"].as_f64().unwrap_or(1.25),
        cache_read_multiplier: v["cacheReadMul"].as_f64().unwrap_or(0.1),
        currency: if v["currency"] == "CNY" {
            Currency::Cny
        } else {
            Currency::Usd
        },
    }
}
#[test]
fn actual_python_tier_thresholds_duplicate_order_peak_and_components_match() {
    let fixtures: Value =
        serde_json::from_str(include_str!("fixtures/python_pricing.json")).unwrap();
    let cases = fixtures["tier_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 168);
    for (index, case) in cases.iter().enumerate() {
        let tiers: Vec<_> = case["card"]["contextTiers"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .map(|v| Tier {
                        id: v["id"].as_str().unwrap(),
                        max_prompt_tokens: v["maxPromptTokens"].as_u64().unwrap(),
                        rates: rates(v),
                    })
                    .collect()
            })
            .unwrap_or_default();
        let card = Card {
            base: rates(&case["card"]),
            tiers: &tiers,
            usd_to_cny: 7.24,
            peak_multiplier: case["peak"].as_f64().unwrap(),
        };
        let tokens = tofu_runtime::pricing_usage::normalize(&case["usage"]).unwrap();
        let quote = card.admitted().unwrap().quote(tokens).unwrap().unwrap();
        assert_eq!(
            quote.cost.numeric_projection(),
            case["expected"],
            "{index}: {case}"
        );
        assert_eq!(
            quote.selected_tier.map(|tier| tier.id),
            case["tier_id"].as_str()
        );
        assert_eq!(
            quote.selected_tier.map(|tier| tier.max_prompt_tokens),
            case["max_prompt_tokens"].as_u64()
        );
        assert_eq!(
            quote.resolved.input,
            case["resolved_input"].as_f64().unwrap()
        );
        assert_eq!(
            quote.resolved.output,
            case["resolved_output"].as_f64().unwrap()
        );
        if let Some(tier) = quote.selected_tier {
            assert!(tiers.iter().any(|source| std::ptr::eq(source, tier)));
        }
    }
}
#[test]
fn invalid_unordered_cards_refuse_before_any_quote() {
    let base = Rates {
        input: 1.,
        output: 2.,
        cache_write_multiplier: 1.25,
        cache_read_multiplier: 0.1,
        currency: Currency::Usd,
    };
    let valid = [
        Tier {
            id: "small",
            max_prompt_tokens: 100,
            rates: base,
        },
        Tier {
            id: "large",
            max_prompt_tokens: 200,
            rates: base,
        },
    ];
    let reversed = [valid[1], valid[0]];
    let card = Card {
        base,
        tiers: &reversed,
        usd_to_cny: 7.24,
        peak_multiplier: 1.,
    };
    assert!(matches!(card.admitted(), Err(Error::InvalidOrder)));
    for bad in [f64::NAN, f64::INFINITY, 0., -1.] {
        assert!(matches!(
            Card {
                base,
                tiers: &valid,
                usd_to_cny: bad,
                peak_multiplier: 1.
            }
            .admitted(),
            Err(Error::InvalidCard)
        ));
        assert!(matches!(
            Card {
                base,
                tiers: &valid,
                usd_to_cny: 7.24,
                peak_multiplier: bad
            }
            .admitted(),
            Err(Error::InvalidCard)
        ));
    }
    let bad = [Tier {
        id: "",
        max_prompt_tokens: 1,
        rates: base,
    }];
    assert!(matches!(
        Card {
            base,
            tiers: &bad,
            usd_to_cny: 7.24,
            peak_multiplier: 1.
        }
        .admitted(),
        Err(Error::InvalidTier)
    ));
}
