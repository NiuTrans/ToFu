//! Pure additive usage projection over already-admitted native JSON values.
//! Entries aggregate and aggregate_json; labels/oracle derive from lib.cost.accumulate_usage_into.
//! u64 integer sums refuse overflow; finite f64 counters follow Python addition,
//! including ignoring nonfinite sums. No model, account, money or store rights.
#[path = "generated_usage_aggregate.rs"]
mod labels;
use serde_json::{Map, Number, Value};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    CounterOverflow,
    InvalidJson,
    InvalidRounds,
}

#[derive(Clone, Copy)]
enum Counter {
    Integer(u64),
    Float(f64),
}

fn counter(value: &Value) -> Option<Counter> {
    let Value::Number(number) = value else {
        return None;
    };
    if let Some(value) = number.as_u64() {
        return Some(Counter::Integer(value));
    }
    number
        .as_f64()
        .filter(|value| value.is_finite() && *value >= 0.0)
        .map(Counter::Float)
}

fn floating(value: Counter) -> f64 {
    match value {
        Counter::Integer(value) => value as f64,
        Counter::Float(value) => value,
    }
}

fn add_counts(total: &mut Map<String, Value>, source: &Map<String, Value>) -> Result<(), Error> {
    for (key, raw) in source {
        if key.starts_with('_') {
            continue;
        }
        let Some(value) = counter(raw) else { continue };
        add_counter(total, key, value)?;
    }
    Ok(())
}

fn add_counter(total: &mut Map<String, Value>, key: &str, value: Counter) -> Result<(), Error> {
    let previous = total
        .get(key)
        .and_then(counter)
        .unwrap_or(Counter::Integer(0));
    let sum = match (previous, value) {
        (Counter::Integer(previous), Counter::Integer(value)) => {
            Value::from(previous.checked_add(value).ok_or(Error::CounterOverflow)?)
        }
        _ => {
            let value = floating(previous) + floating(value);
            let Some(number) = Number::from_f64(value) else {
                return Ok(());
            };
            Value::Number(number)
        }
    };
    total.insert(key.to_owned(), sum);
    Ok(())
}

/// Project supported native counters without copying opaque provider evidence.
/// Caller must admit integer lexical width before converting wire JSON to Value;
/// this function cannot recover a wide integer already rounded by a decoder.
/// Failure returns no partial aggregate and never modifies input rounds.
pub fn aggregate(rounds: &[Value]) -> Result<Map<String, Value>, Error> {
    let mut total = Map::new();
    for round in rounds {
        let Some(source) = round.as_object() else {
            continue;
        };
        add_counts(&mut total, source)?;
        for key in labels::ADDITIVE_DETAILS {
            let Some(details) = source.get(*key).and_then(Value::as_object) else {
                continue;
            };
            let previous = total.remove(*key);
            let (mut combined, scalar) = match previous {
                Some(Value::Object(value)) => (value, None),
                scalar => (Map::new(), scalar),
            };
            add_counts(&mut combined, details)?;
            if !combined.is_empty() {
                total.insert((*key).to_owned(), Value::Object(combined));
            } else if let Some(value) = scalar {
                total.insert((*key).to_owned(), value);
            }
        }
    }
    Ok(total)
}

/// Aggregate raw round JSON before a generic decoder can round wide integers.
/// Only counter-bearing objects are decoded; opaque values remain borrowed raw
/// spans. Positive wide integer counts refuse, negative integers are ignored,
/// and integer -0 stays an integer. This grants no wire/account admission rights.
pub fn aggregate_json(input: &[u8]) -> Result<Map<String, Value>, Error> {
    use serde_json::value::RawValue;
    use std::collections::BTreeMap;
    let rounds: Vec<&RawValue> = serde_json::from_slice(input).map_err(|error| {
        if error.is_data() {
            Error::InvalidRounds
        } else {
            Error::InvalidJson
        }
    })?;
    fn raw_counter(raw: &RawValue) -> Result<Option<Counter>, Error> {
        let text = raw.get();
        let first = text.as_bytes()[0];
        if first != b'-' && !first.is_ascii_digit() {
            return Ok(None);
        }
        if text.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
            let number: f64 = text.parse().map_err(|_| Error::InvalidJson)?;
            return Ok((number.is_finite() && number >= 0.0).then_some(Counter::Float(number)));
        }
        if let Some(digits) = text.strip_prefix('-') {
            return Ok(digits
                .bytes()
                .all(|byte| byte == b'0')
                .then_some(Counter::Integer(0)));
        }
        Ok(Some(Counter::Integer(
            text.parse().map_err(|_| Error::CounterOverflow)?,
        )))
    }
    fn add_raw(
        total: &mut Map<String, Value>,
        source: &BTreeMap<String, &RawValue>,
    ) -> Result<(), Error> {
        for (key, raw) in source {
            if key.starts_with('_') {
                continue;
            }
            let Some(value) = raw_counter(raw)? else {
                continue;
            };
            add_counter(total, key, value)?;
        }
        Ok(())
    }
    let mut total = Map::new();
    for round in rounds {
        if !round.get().starts_with('{') {
            continue;
        }
        let source: BTreeMap<String, &RawValue> =
            serde_json::from_str(round.get()).map_err(|_| Error::InvalidJson)?;
        add_raw(&mut total, &source)?;
        for key in labels::ADDITIVE_DETAILS {
            let Some(raw) = source.get(*key).filter(|raw| raw.get().starts_with('{')) else {
                continue;
            };
            let details: BTreeMap<String, &RawValue> =
                serde_json::from_str(raw.get()).map_err(|_| Error::InvalidJson)?;
            let previous = total.remove(*key);
            let (mut combined, scalar) = match previous {
                Some(Value::Object(value)) => (value, None),
                scalar => (Map::new(), scalar),
            };
            add_raw(&mut combined, &details)?;
            if !combined.is_empty() {
                total.insert((*key).to_owned(), Value::Object(combined));
            } else if let Some(value) = scalar {
                total.insert((*key).to_owned(), value);
            }
        }
    }
    Ok(total)
}
