//! Select one owner-supplied normalized rate-card tier for actual prompt usage.
//! Entry Card::admitted / AdmittedCard::quote; borrows the normalized card.
//! Creates no registry,
//! model/provider lookup, clock, peak schedule, price cache or wallet authority.
//! Inclusive thresholds and final-tier overflow follow lib/pricing/_provider.py.
use crate::pricing::{self, Cost, Currency, ResolvedPrice, Tokens};
#[derive(Clone, Copy, Debug)]
pub struct Rates {
    pub input: f64,
    pub output: f64,
    pub cache_write_multiplier: f64,
    pub cache_read_multiplier: f64,
    pub currency: Currency,
}
#[derive(Clone, Copy, Debug)]
pub struct Tier<'a> {
    pub id: &'a str,
    pub max_prompt_tokens: u64,
    pub rates: Rates,
}
pub struct Card<'a> {
    pub base: Rates,
    /// Already normalized, stable threshold order. Equal thresholds retain
    /// source order; their final entry owns requests above the last threshold.
    pub tiers: &'a [Tier<'a>],
    pub usd_to_cny: f64,
    /// Explicit multiplier already resolved by the existing clock/date policy.
    /// Apply only to input/output unit rates, never again to cache multipliers.
    pub peak_multiplier: f64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidCard,
    InvalidOrder,
    InvalidTier,
    Arithmetic(pricing::Error),
}
pub struct Quote<'a> {
    pub cost: Cost,
    pub selected_tier: Option<&'a Tier<'a>>,
    pub resolved: ResolvedPrice,
}
fn valid_rates(rates: Rates) -> bool {
    [
        rates.input,
        rates.output,
        rates.cache_write_multiplier,
        rates.cache_read_multiplier,
    ]
    .iter()
    .all(|value| value.is_finite() && *value >= 0.0)
}
impl<'a> Card<'a> {
    pub fn validate(&self) -> Result<(), Error> {
        if !valid_rates(self.base)
            || !self.usd_to_cny.is_finite()
            || self.usd_to_cny <= 0.0
            || !self.peak_multiplier.is_finite()
            || self.peak_multiplier <= 0.0
        {
            return Err(Error::InvalidCard);
        }
        let mut previous = 0;
        for tier in self.tiers {
            if tier.id.is_empty() || tier.max_prompt_tokens == 0 || !valid_rates(tier.rates) {
                return Err(Error::InvalidTier);
            }
            if tier.max_prompt_tokens < previous {
                return Err(Error::InvalidOrder);
            }
            previous = tier.max_prompt_tokens;
        }
        Ok(())
    }
    /// Validate once at the composition boundary. The returned borrowed card
    /// cannot be mutated while quotes are being computed; no per-quote sort/copy.
    pub fn admitted(&'a self) -> Result<AdmittedCard<'a>, Error> {
        self.validate()?;
        Ok(AdmittedCard(self))
    }
}
pub struct AdmittedCard<'a>(&'a Card<'a>);
impl<'a> AdmittedCard<'a> {
    pub fn quote(&self, tokens: Tokens) -> Result<Option<Quote<'a>>, Error> {
        let tiers = self.0.tiers;
        let index = tiers.partition_point(|tier| tier.max_prompt_tokens < tokens.total_input);
        let selected = tiers.get(index).or_else(|| tiers.last());
        let rates = selected.map(|tier| tier.rates).unwrap_or(self.0.base);
        let resolved = ResolvedPrice {
            selected_prompt_tokens: tokens.total_input,
            input: rates.input * self.0.peak_multiplier,
            output: rates.output * self.0.peak_multiplier,
            cache_write_multiplier: rates.cache_write_multiplier,
            cache_read_multiplier: rates.cache_read_multiplier,
            usd_to_cny: self.0.usd_to_cny,
            currency: rates.currency,
        };
        Ok(pricing::compute(tokens, resolved)
            .map_err(Error::Arithmetic)?
            .map(|cost| Quote {
                cost,
                selected_tier: selected,
                resolved,
            }))
    }
}
