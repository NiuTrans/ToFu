//! Borrowed provider usage normalization for resolved native request pricing.
//! Entry normalize; aliases are generated from lib/cost.py. This is display/cost
//! input, not model admission or evidence of complete usage. Existing usage.rs
//! remains the settlement completeness authority. Selected malformed, negative,
//! wide or unsupported numeric carriers refuse instead of becoming free tokens.
use crate::execution_contract::*;
use crate::pricing::Tokens;
use serde_json::{Map, Value};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidCounter,
    NegativeCounter,
    CounterOverflow,
    UnsupportedNumericText,
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64() != Some(0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn count(value: &Value) -> Result<u64, Error> {
    match value {
        Value::Null => Ok(0),
        Value::Bool(v) => Ok(u64::from(*v)),
        Value::Number(v) => {
            if let Some(v) = v.as_u64() {
                return Ok(v);
            }
            if let Some(v) = v.as_i64() {
                return u64::try_from(v).map_err(|_| Error::NegativeCounter);
            }
            let v = v.as_f64().ok_or(Error::InvalidCounter)?.trunc();
            if !v.is_finite() {
                return Err(Error::InvalidCounter);
            }
            if v < 0.0 {
                return Err(Error::NegativeCounter);
            }
            if v >= 18446744073709551616.0 {
                return Err(Error::CounterOverflow);
            }
            Ok(v as u64)
        }
        Value::String(v) => {
            let v = v.trim_matches(|c: char| {
                crate::pricing_unicode::INTEGER_WHITESPACE
                    .binary_search(&(c as u32))
                    .is_ok()
            });
            let negative = v.starts_with('-');
            let v = v.strip_prefix(['+', '-']).unwrap_or(v);
            let mut previous = false;
            let mut total = 0u64;
            for c in v.chars() {
                if c == '_' && previous {
                    previous = false;
                    continue;
                }
                let code = c as u32;
                let digit = if c.is_ascii_digit() {
                    (code - b'0' as u32) as u64
                } else {
                    let index =
                        crate::pricing_unicode::DECIMAL_ZEROS.partition_point(|zero| *zero <= code);
                    index
                        .checked_sub(1)
                        .map(|index| code - crate::pricing_unicode::DECIMAL_ZEROS[index])
                        .filter(|digit| *digit <= 9)
                        .ok_or(if c.is_ascii() {
                            Error::InvalidCounter
                        } else {
                            Error::UnsupportedNumericText
                        })? as u64
                };
                total = total
                    .checked_mul(10)
                    .and_then(|n| n.checked_add(digit))
                    .ok_or(Error::CounterOverflow)?;
                previous = true;
            }
            if !previous {
                return Err(Error::InvalidCounter);
            }
            if negative && total != 0 {
                return Err(Error::NegativeCounter);
            }
            Ok(total)
        }
        _ => Err(Error::InvalidCounter),
    }
}
fn scalar(usage: &Map<String, Value>, key: &str) -> Result<u64, Error> {
    usage
        .get(key)
        .filter(|v| truthy(v))
        .map(count)
        .unwrap_or(Ok(0))
}
fn aliases(
    usage: &Map<String, Value>,
    keys: &[&str],
    nested: &[(&str, &str)],
) -> Result<u64, Error> {
    let value = keys
        .iter()
        .filter_map(|key| usage.get(*key))
        .find(|v| truthy(v));
    let result = value.map(count).unwrap_or(Ok(0))?;
    if result != 0 {
        return Ok(result);
    }
    let value = nested
        .iter()
        .filter_map(|(parent, key)| {
            usage
                .get(*parent)
                .and_then(Value::as_object)
                .and_then(|v| v.get(*key))
        })
        .find(|v| truthy(v));
    value.map(count).unwrap_or(Ok(0))
}
fn guard_read(usage: &Map<String, Value>) -> Result<u64, Error> {
    // Python _nested_cached returns a selected vendor zero string immediately;
    // normalize_usage instead allows its zero result to enter nested fallback.
    // Those distinct policies must not be collapsed into the alias helper.
    for key in &USAGE_CACHE_READ_KEYS[2..] {
        if let Some(value) = usage.get(*key).filter(|value| truthy(value)) {
            return count(value);
        }
    }
    aliases(usage, &[], USAGE_CACHE_READ_NESTED)
}
fn present(usage: &Map<String, Value>, key: &str) -> bool {
    usage.get(key).is_some_and(|v| !v.is_null())
}
pub fn normalize(value: &Value) -> Result<Tokens, Error> {
    let Some(usage) = value.as_object() else {
        return Ok(Tokens {
            input: 0,
            output: 0,
            cache_write: 0,
            cache_read: 0,
            thinking: 0,
            uncached: 0,
            total_input: 0,
        });
    };
    let input = aliases(usage, USAGE_INPUT_KEYS, &[])?;
    let output = aliases(usage, USAGE_OUTPUT_KEYS, &[])?;
    let cache_write = aliases(usage, USAGE_CACHE_WRITE_KEYS, &[])?;
    let cache_read = aliases(usage, USAGE_CACHE_READ_KEYS, USAGE_CACHE_READ_NESTED)?;
    let thinking = aliases(usage, USAGE_THINKING_KEYS, USAGE_THINKING_NESTED)?;
    let cached = cache_write
        .checked_add(cache_read)
        .ok_or(Error::CounterOverflow)?;
    let (uncached, total_input) = if cached == 0 {
        (input, input)
    } else {
        let anthropic = if present(usage, USAGE_CACHE_READ_KEYS[1])
            || present(usage, USAGE_CACHE_WRITE_KEYS[1])
        {
            true
        } else if present(usage, USAGE_INPUT_KEYS[0]) {
            let read = scalar(usage, USAGE_CACHE_READ_KEYS[0])?;
            let read = if read != 0 { read } else { guard_read(usage)? };
            let written = scalar(usage, USAGE_CACHE_WRITE_KEYS[0])?;
            read.checked_add(written).ok_or(Error::CounterOverflow)?
                > scalar(usage, USAGE_INPUT_KEYS[0])?
        } else {
            present(usage, USAGE_INPUT_KEYS[1])
        };
        if anthropic {
            let residual = if present(usage, USAGE_INPUT_KEYS[1]) {
                scalar(usage, USAGE_INPUT_KEYS[1])?
            } else {
                input
            };
            (
                residual,
                residual.checked_add(cached).ok_or(Error::CounterOverflow)?,
            )
        } else {
            (
                input.saturating_sub(cached),
                if input == 0 { cached } else { input },
            )
        }
    };
    Ok(Tokens {
        input,
        output,
        cache_write,
        cache_read,
        thinking,
        uncached,
        total_input,
    })
}
