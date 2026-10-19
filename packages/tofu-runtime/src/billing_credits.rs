//! Pure native request fee conversion, not wallet or reservation authority.
//! Entry convert: consumes canonical nine-decimal USD components and an explicit
//! resolved nonnegative relay margin. Round components individually using Python
//! ties-to-even, then truncate the base*margin product as lib/billing/cost.py does.
//! No prices, global configuration, wallet mutation, queue, cache or new account.
use crate::pricing::Cost;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Credits {
    pub input: u64,
    pub output: u64,
    pub cache_read: u64,
    pub cache_write: u64,
    pub base: u64,
    pub margin: u64,
    pub total: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidComponent,
    InvalidMargin,
    Overflow,
}
fn integer(value: f64) -> Result<u64, Error> {
    if !value.is_finite() || value >= 18446744073709551616.0 {
        return Err(Error::Overflow);
    }
    Ok(value as u64)
}
fn component(usd: f64) -> Result<u64, Error> {
    if !usd.is_finite() || usd < 0.0 {
        return Err(Error::InvalidComponent);
    }
    integer((usd * crate::billing_contract::MICRO_PER_USD as f64).round_ties_even())
}
pub fn convert(cost: Option<&Cost>, resolved_margin: f64) -> Result<Credits, Error> {
    if !resolved_margin.is_finite() || resolved_margin < 0.0 {
        return Err(Error::InvalidMargin);
    }
    let (input, output, cache_read, cache_write) = if let Some(cost) = cost {
        (
            component(cost.input_usd)?,
            component(cost.output_usd)?,
            component(cost.cache_read_usd)?,
            component(cost.cache_write_usd)?,
        )
    } else {
        (0, 0, 0, 0)
    };
    let base = input
        .checked_add(output)
        .and_then(|n| n.checked_add(cache_read))
        .and_then(|n| n.checked_add(cache_write))
        .ok_or(Error::Overflow)?;
    let margin = integer((base as f64 * resolved_margin).trunc())?;
    let total = base.checked_add(margin).ok_or(Error::Overflow)?;
    Ok(Credits {
        input,
        output,
        cache_read,
        cache_write,
        base,
        margin,
        total,
    })
}
