//! Resolved request cost arithmetic for native execution.
//! Entry: compute. Caller supplies canonical normalized counts, cache split and
//! the one owner-resolved price/tier snapshot for this exact prompt. No model
//! lookup, exchange-rate refresh, wallet, storage, global table or worker here.
//! Differential authority: lib/cost.py compute_cost; money operation order and
//! ties-to-even rounding preserve its display/billing component projections.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tokens {
    pub input: u64,
    pub output: u64,
    pub cache_write: u64,
    pub cache_read: u64,
    pub thinking: u64,
    pub uncached: u64,
    pub total_input: u64,
}
#[derive(Clone, Copy, Debug)]
pub enum Currency {
    Usd,
    Cny,
}
#[derive(Clone, Copy, Debug)]
pub struct ResolvedPrice {
    pub selected_prompt_tokens: u64,
    pub input: f64,
    pub output: f64,
    pub cache_write_multiplier: f64,
    pub cache_read_multiplier: f64,
    pub usd_to_cny: f64,
    pub currency: Currency,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidPrice,
    PromptBinding,
    InvalidSplit,
    NonFiniteCost,
}
#[derive(Clone, Copy, Debug)]
pub struct Cost {
    pub tokens: Tokens,
    pub cost_usd: f64,
    pub cost_cny: f64,
    pub input_usd: f64,
    pub output_usd: f64,
    pub cache_write_usd: f64,
    pub cache_read_usd: f64,
    pub input_cny: f64,
    pub output_cny: f64,
    pub cache_write_cny: f64,
    pub cache_read_cny: f64,
    pub cache_savings_usd: f64,
    pub cache_savings_cny: f64,
}
fn rounded(value: f64, factor: f64) -> Result<f64, Error> {
    let scaled = value * factor;
    if !scaled.is_finite() {
        return Err(Error::NonFiniteCost);
    }
    Ok(scaled.round_ties_even() / factor)
}
pub fn compute(mut tokens: Tokens, price: ResolvedPrice) -> Result<Option<Cost>, Error> {
    if !price.usd_to_cny.is_finite()
        || price.usd_to_cny <= 0.0
        || [
            price.input,
            price.output,
            price.cache_write_multiplier,
            price.cache_read_multiplier,
        ]
        .iter()
        .any(|v| !v.is_finite() || *v < 0.0)
    {
        return Err(Error::InvalidPrice);
    }
    if tokens.uncached > tokens.total_input {
        return Err(Error::InvalidSplit);
    }
    if tokens.cache_write == 0
        && tokens.cache_read == 0
        && (tokens.uncached != tokens.input || tokens.total_input != tokens.input)
    {
        return Err(Error::InvalidSplit);
    }
    if price.selected_prompt_tokens != tokens.total_input {
        return Err(Error::PromptBinding);
    }
    if tokens.thinking > 0 && tokens.output == 0 {
        tokens.output = tokens.thinking;
    }
    if tokens.input == 0 && tokens.output == 0 && tokens.cache_write == 0 && tokens.cache_read == 0
    {
        return Ok(None);
    }
    let unit = match price.currency {
        Currency::Usd => 1.0,
        Currency::Cny => 1.0 / price.usd_to_cny,
    };
    let input_rate = price.input * unit;
    let output_rate = price.output * unit;
    let output = (tokens.output as f64 * output_rate) / 1e6;
    let (input, write, read) = if tokens.cache_write > 0 || tokens.cache_read > 0 {
        (
            (tokens.uncached as f64 * input_rate) / 1e6,
            (tokens.cache_write as f64 * input_rate * price.cache_write_multiplier) / 1e6,
            (tokens.cache_read as f64 * input_rate * price.cache_read_multiplier) / 1e6,
        )
    } else {
        if tokens.uncached != tokens.input || tokens.total_input != tokens.input {
            return Err(Error::InvalidSplit);
        }
        ((tokens.input as f64 * input_rate) / 1e6, 0.0, 0.0)
    };
    let total = input + write + read + output;
    let savings =
        ((tokens.total_input as f64 * input_rate) / 1e6 - (input + write + read)).max(0.0);
    Ok(Some(Cost {
        tokens,
        cost_usd: rounded(total, 1e4)?,
        cost_cny: rounded(total * price.usd_to_cny, 1e4)?,
        input_usd: rounded(input, 1e9)?,
        output_usd: rounded(output, 1e9)?,
        cache_write_usd: rounded(write, 1e9)?,
        cache_read_usd: rounded(read, 1e9)?,
        input_cny: rounded(input * price.usd_to_cny, 1e6)?,
        output_cny: rounded(output * price.usd_to_cny, 1e6)?,
        cache_write_cny: rounded(write * price.usd_to_cny, 1e6)?,
        cache_read_cny: rounded(read * price.usd_to_cny, 1e6)?,
        cache_savings_usd: rounded(savings, 1e6)?,
        cache_savings_cny: rounded(savings * price.usd_to_cny, 1e6)?,
    }))
}
impl Cost {
    /// Fixed canonical numeric projection. Caller preserves the exact borrowed
    /// resolved pricingSnapshot/source/peak metadata; this creates no registry.
    pub fn numeric_projection(&self) -> serde_json::Value {
        serde_json::json!({"costUsd":self.cost_usd,"costCny":self.cost_cny,
            "inputTokens":self.tokens.uncached,"outputTokens":self.tokens.output,
            "totalInputTokens":self.tokens.total_input,"cacheWriteTokens":self.tokens.cache_write,
            "cacheReadTokens":self.tokens.cache_read,"thinkingTokens":self.tokens.thinking,
            "inputCostUsd":self.input_usd,"outputCostUsd":self.output_usd,
            "cacheWriteCostUsd":self.cache_write_usd,"cacheReadCostUsd":self.cache_read_usd,
            "inputCostCny":self.input_cny,"outputCostCny":self.output_cny,
            "cacheWriteCostCny":self.cache_write_cny,"cacheReadCostCny":self.cache_read_cny,
            "cacheSavingsUsd":self.cache_savings_usd,"cacheSavingsCny":self.cache_savings_cny})
    }
}
