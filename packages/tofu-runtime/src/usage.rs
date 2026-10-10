//! Provider consumption evidence only: aliases/cache conventions and completeness.
//! Entry observed_totals; aliases derive from lib/cost.py. No pricing, estimates,
//! reservation, durability or provider retry authority. Unsupported wide counters
//! fail explicitly instead of turning consumed tokens into zero.
use crate::execution_contract::*;
use serde_json::{Map, Value};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observation {
    pub tokens: u64,
    pub complete: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    CounterOverflow,
    UnsupportedNumericText,
}
fn count(value: &Value) -> Result<Option<u64>, Error> {
    match value {
        Value::Number(n) => {
            if let Some(n) = n.as_u64() {
                return Ok(Some(n));
            }
            if let Some(n) = n.as_i64() {
                return Ok((n >= 0).then_some(n as u64));
            }
            let n = n.as_f64().unwrap();
            if n < 0.0 || n.fract() != 0.0 {
                return Ok(None);
            }
            if n >= 18446744073709551616.0 {
                return Err(Error::CounterOverflow);
            }
            Ok(Some(n as u64))
        }
        Value::String(s) => {
            let s = s.trim();
            let negative = s.starts_with('-');
            let s = s.strip_prefix(['+', '-']).unwrap_or(s);
            if s.is_empty() {
                return Ok(None);
            }
            if s.chars().any(|c| !c.is_ascii() && c.is_numeric()) {
                return Err(Error::UnsupportedNumericText);
            }
            let mut previous_digit = false;
            let mut value = 0u64;
            let mut overflow = false;
            let mut nonzero = false;
            for c in s.bytes() {
                if c == b'_' && previous_digit {
                    previous_digit = false;
                    continue;
                }
                if !c.is_ascii_digit() {
                    return Ok(None);
                }
                previous_digit = true;
                nonzero |= c != b'0';
                if let Some(n) = value
                    .checked_mul(10)
                    .and_then(|n| n.checked_add((c - b'0') as u64))
                {
                    value = n;
                } else {
                    overflow = true;
                }
            }
            if !previous_digit || (negative && nonzero) {
                return Ok(None);
            }
            if overflow {
                return Err(Error::CounterOverflow);
            }
            Ok(Some(value))
        }
        _ => Ok(None),
    }
}
#[derive(Default)]
struct Counter {
    first: u64,
    present: bool,
    invalid: bool,
}
impl Counter {
    fn add(&mut self, value: Option<&Value>) -> Result<(), Error> {
        if let Some(value) = value {
            match count(value)? {
                Some(n) => {
                    self.present = true;
                    if self.first == 0 {
                        self.first = n;
                    }
                }
                None => self.invalid = true,
            }
        }
        Ok(())
    }
    fn known(&self) -> bool {
        self.present && !self.invalid
    }
}
fn counter(
    usage: &Map<String, Value>,
    keys: &[&str],
    nested: &[(&str, &str)],
) -> Result<Counter, Error> {
    let mut c = Counter::default();
    for key in keys {
        c.add(usage.get(*key))?;
    }
    for (parent, key) in nested {
        c.add(
            usage
                .get(*parent)
                .and_then(|v| v.as_object())
                .and_then(|v| v.get(*key)),
        )?;
    }
    Ok(c)
}
fn parsed(usage: &Map<String, Value>, key: &str) -> Result<u64, Error> {
    Ok(usage
        .get(key)
        .map(count)
        .transpose()?
        .flatten()
        .unwrap_or(0))
}
pub fn observed_totals(usage: &Value) -> Result<Observation, Error> {
    let Some(usage) = usage.as_object() else {
        return Ok(Observation {
            tokens: 0,
            complete: false,
        });
    };
    let input = counter(usage, USAGE_INPUT_KEYS, &[])?;
    let output = counter(usage, USAGE_OUTPUT_KEYS, &[])?;
    let write = counter(usage, USAGE_CACHE_WRITE_KEYS, &[])?;
    let read = counter(usage, USAGE_CACHE_READ_KEYS, USAGE_CACHE_READ_NESTED)?;
    // Thinking is already within output totals. Still validate supported numeric
    // representation exactly as the canonical observation's cleaning pass does.
    counter(usage, USAGE_THINKING_KEYS, USAGE_THINKING_NESTED)?;
    let cached = write
        .first
        .checked_add(read.first)
        .ok_or(Error::CounterOverflow)?;
    let cache_fallback = input.first == 0 && cached != 0;
    let mut anthropic = USAGE_CACHE_READ_KEYS[1..2]
        .iter()
        .chain(USAGE_CACHE_WRITE_KEYS[1..2].iter())
        .any(|k| usage.contains_key(*k));
    if !anthropic {
        if usage.contains_key(USAGE_INPUT_KEYS[0]) {
            // The impossible-cache guard uses canonical flat cache then vendor
            // spellings/nested evidence, rather than residual keys.
            let canonical = parsed(usage, USAGE_CACHE_READ_KEYS[0])?;
            let fallback =
                counter(usage, &USAGE_CACHE_READ_KEYS[2..], USAGE_CACHE_READ_NESTED)?.first;
            let cached_guard = if canonical != 0 { canonical } else { fallback };
            let written = parsed(usage, USAGE_CACHE_WRITE_KEYS[0])?;
            anthropic = cached_guard
                .checked_add(written)
                .ok_or(Error::CounterOverflow)?
                > parsed(usage, USAGE_INPUT_KEYS[0])?;
        } else {
            anthropic = usage.contains_key(USAGE_INPUT_KEYS[1]);
        }
    }
    let total_input = if cached == 0 {
        input.first
    } else if anthropic {
        let residual = if usage.contains_key(USAGE_INPUT_KEYS[1]) {
            parsed(usage, USAGE_INPUT_KEYS[1])?
        } else {
            input.first
        };
        residual.checked_add(cached).ok_or(Error::CounterOverflow)?
    } else if input.first == 0 {
        cached
    } else {
        input.first
    };
    let tokens = total_input
        .checked_add(output.first)
        .ok_or(Error::CounterOverflow)?;
    Ok(Observation {
        tokens,
        complete: input.known()
            && output.known()
            && !cache_fallback
            && ((!read.invalid && !write.invalid) || !anthropic),
    })
}

/// Bounded JSON evidence for host accounting; unsupported counts stay unknown.
pub fn evidence(usage: &Value) -> Value {
    match observed_totals(usage) {
        Ok(value) => {
            serde_json::json!({"tokens":value.tokens,"complete":value.complete,"error":null})
        }
        Err(error) => serde_json::json!({"tokens":null,"complete":false,"error": match error {
            Error::CounterOverflow => "counter_overflow",
            Error::UnsupportedNumericText => "unsupported_numeric_text",
        }}),
    }
}
