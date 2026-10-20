//! Tool caller normalization and lossless argument/approval fingerprints.
//! Python caller authority: tool_results/tool_caller_identity.py.
//! FloodGuard is retained only as a legacy library API, never stream admission.
//! No schema authorization, execution, storage or user-content diagnostics.
use crate::execution_contract::{
    MAX_IDENTICAL_TOOL_CALLS, MAX_TOOL_CALLER_ID_CHARS, MIN_IDENTICAL_TOOL_CALLS,
};
use serde_core::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallerError {
    ObjectRequired,
    TypeRequired,
    IdentityRequired,
    IdentityTooLong,
}
// Python str.strip additionally treats the C0 information separators as space.
pub(crate) fn python_trim(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}
pub fn normalize_caller(
    value: &Value,
    require_program_identity: bool,
) -> Result<Option<Value>, CallerError> {
    if value.is_null() {
        return Ok(None);
    }
    let object = value.as_object().ok_or(CallerError::ObjectRequired)?;
    let (kind, field, required) = match object.get("type").and_then(Value::as_str) {
        Some("program") => ("program", "caller_id", require_program_identity),
        Some("multi_agent") => ("multi_agent", "agent_name", true),
        _ => return Err(CallerError::TypeRequired),
    };
    let identity = object.get(field).unwrap_or(&Value::Null);
    if !required && (identity.is_null() || identity.as_str() == Some("")) {
        return Ok(Some(json!({"type":kind})));
    }
    let identity = identity
        .as_str()
        .map(python_trim)
        .filter(|s| !s.is_empty())
        .ok_or(CallerError::IdentityRequired)?;
    if identity.chars().count() > MAX_TOOL_CALLER_ID_CHARS {
        return Err(CallerError::IdentityTooLong);
    }
    let mut normalized = json!({"type":kind});
    normalized[field] = identity.into();
    Ok(Some(normalized))
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FloodError {
    InvalidBudget,
    PositionLimit,
    ArgumentsTooDeep,
    InvalidArguments,
    Flood {
        count: usize,
        limit: usize,
        matching_positions: Vec<usize>,
    },
}
struct Position {
    signature: [u8; 32],
    order: u64,
}
pub struct FloodGuard {
    limit: usize,
    max_positions: usize,
    order: u64,
    positions: BTreeMap<usize, Position>,
    counts: BTreeMap<[u8; 32], usize>,
}
impl FloodGuard {
    pub fn new(limit: usize, max_positions: usize) -> Result<Self, FloodError> {
        if !(MIN_IDENTICAL_TOOL_CALLS..=MAX_IDENTICAL_TOOL_CALLS).contains(&limit)
            || max_positions == 0
        {
            return Err(FloodError::InvalidBudget);
        }
        Ok(Self {
            limit,
            max_positions,
            order: 0,
            positions: BTreeMap::new(),
            counts: BTreeMap::new(),
        })
    }
    pub fn observe(
        &mut self,
        position: usize,
        call: &Value,
        final_call: bool,
    ) -> Result<(), FloodError> {
        let signature = signature(call, final_call)?;
        self.record(position, signature)
    }
    pub fn observe_parts(
        &mut self,
        position: usize,
        name: &str,
        arguments: &str,
        caller: Option<&Value>,
        final_call: bool,
    ) -> Result<(), FloodError> {
        let signature =
            signature_parts(name, arguments, caller.unwrap_or(&Value::Null), final_call)?;
        self.record(position, signature)
    }
    fn record(&mut self, position: usize, signature: Option<[u8; 32]>) -> Result<(), FloodError> {
        let previous = self.positions.get(&position).map(|p| p.signature);
        if signature == previous {
            return Ok(());
        }
        if let Some(previous) = previous {
            let count = self.counts.get_mut(&previous).unwrap();
            *count -= 1;
            if *count == 0 {
                self.counts.remove(&previous);
            }
            self.positions.remove(&position);
        }
        let Some(signature) = signature else {
            return Ok(());
        };
        if self.positions.len() >= self.max_positions {
            return Err(FloodError::PositionLimit);
        }
        let count = self.counts.get(&signature).copied().unwrap_or(0) + 1;
        if count > self.limit {
            let mut matching: Vec<_> = self
                .positions
                .iter()
                .filter(|(_, p)| p.signature == signature)
                .map(|(i, p)| (p.order, *i))
                .collect();
            matching.sort_unstable();
            let mut positions: Vec<_> = matching.into_iter().map(|(_, i)| i).collect();
            positions.push(position);
            return Err(FloodError::Flood {
                count,
                limit: self.limit,
                matching_positions: positions,
            });
        }
        self.counts.insert(signature, count);
        self.positions.insert(
            position,
            Position {
                signature,
                order: self.order,
            },
        );
        self.order = self.order.saturating_add(1);
        Ok(())
    }
    pub fn retained_positions(&self) -> usize {
        self.positions.len()
    }
}
fn field(hash: &mut Sha256, bytes: &[u8]) {
    hash.update((bytes.len() as u64).to_le_bytes());
    hash.update(bytes);
}
// Hash validated JSON tokens without materializing numeric values. Integer
// spelling stays exact; float bits match Python's rounded IEEE value. Object
// children sort by decoded keys and duplicate keys retain their last value.
const MAX_SIGNATURE_JSON_DEPTH: usize = 128;
struct JsonHash<'a> {
    text: &'a str,
    position: usize,
}
impl JsonHash<'_> {
    fn whitespace(&mut self) {
        while self
            .text
            .as_bytes()
            .get(self.position)
            .is_some_and(u8::is_ascii_whitespace)
        {
            self.position += 1;
        }
    }
    fn string(&mut self) -> Result<String, FloodError> {
        let start = self.position;
        self.position += 1;
        loop {
            match self.text.as_bytes().get(self.position) {
                Some(b'"') => {
                    self.position += 1;
                    break;
                }
                Some(b'\\') => self.position += 2,
                Some(_) => self.position += 1,
                None => return Err(FloodError::InvalidArguments),
            }
        }
        serde_json::from_str(&self.text[start..self.position])
            .map_err(|_| FloodError::InvalidArguments)
    }
    fn value(&mut self, depth: usize) -> Result<[u8; 32], FloodError> {
        let mut hash = Sha256::new();
        self.emit(&mut hash, depth)?;
        Ok(hash.finalize().into())
    }
    fn emit(&mut self, hash: &mut Sha256, depth: usize) -> Result<(), FloodError> {
        if depth > MAX_SIGNATURE_JSON_DEPTH {
            return Err(FloodError::ArgumentsTooDeep);
        }
        self.whitespace();
        let first = *self
            .text
            .as_bytes()
            .get(self.position)
            .ok_or(FloodError::InvalidArguments)?;
        match first {
            b'{' => {
                self.position += 1;
                self.whitespace();
                let mut members = BTreeMap::new();
                if self.text.as_bytes().get(self.position) != Some(&b'}') {
                    loop {
                        let key = self.string()?;
                        self.whitespace();
                        self.position += 1;
                        let child = self.value(depth + 1)?;
                        members.insert(key, child);
                        self.whitespace();
                        if self.text.as_bytes().get(self.position) == Some(&b'}') {
                            break;
                        }
                        self.position += 1;
                        self.whitespace();
                    }
                }
                self.position += 1;
                hash.update([6]);
                hash.update((members.len() as u64).to_le_bytes());
                for (key, value) in members {
                    field(hash, key.as_bytes());
                    hash.update(value);
                }
            }
            b'[' => {
                self.position += 1;
                self.whitespace();
                hash.update([5]);
                if self.text.as_bytes().get(self.position) != Some(&b']') {
                    loop {
                        self.emit(hash, depth + 1)?;
                        self.whitespace();
                        if self.text.as_bytes().get(self.position) == Some(&b']') {
                            break;
                        }
                        self.position += 1;
                    }
                }
                self.position += 1;
                hash.update([7]);
            }
            b'"' => {
                hash.update([2]);
                field(hash, self.string()?.as_bytes());
            }
            b't' => {
                self.position += 4;
                hash.update([1, 1]);
            }
            b'f' => {
                self.position += 5;
                hash.update([1, 0]);
            }
            b'N' => {
                self.position += 3;
                hash.update([3]);
                hash.update(f64::NAN.to_bits().to_le_bytes());
            }
            b'I' => {
                self.position += 8;
                hash.update([3]);
                hash.update(f64::INFINITY.to_bits().to_le_bytes());
            }
            b'-' if self.text[self.position..].starts_with("-Infinity") => {
                self.position += 9;
                hash.update([3]);
                hash.update(f64::NEG_INFINITY.to_bits().to_le_bytes());
            }
            b'n' => {
                self.position += 4;
                hash.update([0]);
            }
            _ => {
                let start = self.position;
                while self
                    .text
                    .as_bytes()
                    .get(self.position)
                    .is_some_and(|b| matches!(b, b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E'))
                {
                    self.position += 1;
                }
                let number = &self.text[start..self.position];
                if number.contains(['.', 'e', 'E']) {
                    hash.update([3]);
                    let number = number
                        .parse::<f64>()
                        .map_err(|_| FloodError::InvalidArguments)?;
                    hash.update(number.to_bits().to_le_bytes());
                } else {
                    hash.update([4]);
                    field(
                        hash,
                        if number == "-0" {
                            b"0"
                        } else {
                            number.as_bytes()
                        },
                    );
                }
            }
        }
        Ok(())
    }
}
// Python's JSON decoder accepts these nonfinite constants. Normalize only
// for syntax validation; the fingerprint still reads their original IEEE kind.
fn python_json_syntax(text: &str) -> bool {
    fn valid(text: &str) -> bool {
        let mut decoder = serde_json::Deserializer::from_str(text);
        serde_core::de::IgnoredAny::deserialize(&mut decoder).is_ok() && decoder.end().is_ok()
    }
    if valid(text) {
        return true;
    }
    let bytes = text.as_bytes();
    let mut position = 0;
    let mut quoted = false;
    let mut parts = String::new();
    let mut copied = 0;
    while position < bytes.len() {
        if bytes[position] == b'"' {
            quoted = !quoted;
            position += 1;
            continue;
        }
        if quoted {
            position += if bytes[position] == b'\\' { 2 } else { 1 };
            continue;
        }
        let token = ["-Infinity", "Infinity", "NaN"]
            .iter()
            .find(|token| bytes[position..].starts_with(token.as_bytes()));
        if let Some(token) = token {
            let end = position + token.len();
            if end == bytes.len()
                || matches!(
                    bytes[end],
                    b',' | b'}' | b']' | b' ' | b'\n' | b'\r' | b'\t'
                )
            {
                parts.push_str(&text[copied..position]);
                parts.push_str("0.0");
                position = end;
                copied = end;
                continue;
            }
        }
        position += 1;
    }
    if copied == 0 {
        return false;
    }
    parts.push_str(&text[copied..]);
    valid(&parts)
}
/// Refuse numeric-kind/value changes before decoded arguments reach a handler.
/// Reuses the exact lexical fingerprint rather than coercing large integers.
pub fn lossless_arguments(text: &str) -> Result<Value, FloodError> {
    let value: Value = serde_json::from_str(text).map_err(|_| FloodError::InvalidArguments)?;
    let encoded = serde_json::to_string(&value).map_err(|_| FloodError::InvalidArguments)?;
    let original = JsonHash { text, position: 0 }.value(0)?;
    let decoded = JsonHash {
        text: &encoded,
        position: 0,
    }
    .value(0)?;
    if original != decoded {
        return Err(FloodError::InvalidArguments);
    }
    Ok(value)
}

pub(crate) fn approval_fingerprint(name: &str, arguments: &Value) -> Result<[u8; 32], FloodError> {
    let encoded = serde_json::to_string(arguments).map_err(|_| FloodError::InvalidArguments)?;
    signature_parts(name, &encoded, &Value::Null, true)?.ok_or(FloodError::InvalidArguments)
}
fn signature(call: &Value, final_call: bool) -> Result<Option<[u8; 32]>, FloodError> {
    let Some(function) = call.get("function").and_then(Value::as_object) else {
        return Ok(None);
    };
    let Some(name) = function
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    else {
        return Ok(None);
    };
    let Some(arguments) = function.get("arguments").and_then(Value::as_str) else {
        return Ok(None);
    };
    signature_parts(name, arguments, &call["caller"], final_call)
}
fn signature_parts(
    name: &str,
    arguments: &str,
    caller: &Value,
    final_call: bool,
) -> Result<Option<[u8; 32]>, FloodError> {
    if name.is_empty() {
        return Ok(None);
    }
    if !final_call && python_trim(arguments).is_empty() {
        return Ok(None);
    }
    let arguments = if python_trim(arguments).is_empty() {
        "{}"
    } else {
        arguments
    };
    if !python_trim(arguments).starts_with('{') {
        return Ok(None);
    }
    if !python_json_syntax(arguments) {
        return Ok(None);
    }
    let caller = match normalize_caller(caller, false) {
        Ok(caller) => caller,
        Err(_) => return Ok(None),
    };
    let mut parser = JsonHash {
        text: arguments,
        position: 0,
    };
    let argument_hash = parser.value(0)?;
    let mut hash = Sha256::new();
    field(&mut hash, name.as_bytes());
    hash.update(argument_hash);
    if let Some(caller) = caller {
        field(&mut hash, caller["type"].as_str().unwrap().as_bytes());
        let field_name = if caller["type"] == "program" {
            "caller_id"
        } else {
            "agent_name"
        };
        field(
            &mut hash,
            caller[field_name].as_str().unwrap_or("").as_bytes(),
        );
    } else {
        field(&mut hash, b"root");
    }
    Ok(Some(hash.finalize().into()))
}
