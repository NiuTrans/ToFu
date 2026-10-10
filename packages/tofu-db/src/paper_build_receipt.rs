//! Bounded receipt validation against the generated public paper-build schema.
//! This interpreter accepts only the vocabulary certified by the generator;
//! build-specific success and uniqueness rules match lib/paper/builds/contract.py.

use std::io::{self, Write};
use std::sync::OnceLock;

use serde::Serialize;
use serde_json::Value;

use crate::generated_paper_build as limits;

pub(crate) fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub(crate) fn encode_bounded<T: Serialize>(value: &T, maximum: usize) -> io::Result<Vec<u8>> {
    struct BoundedWriter {
        bytes: Vec<u8>,
        maximum: usize,
    }
    impl Write for BoundedWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if bytes.len() > self.maximum.saturating_sub(self.bytes.len()) {
                return Err(invalid("retained artifact JSON exceeds its byte bound"));
            }
            if self.bytes.capacity() < self.bytes.len() + bytes.len() {
                let capacity = (self.bytes.len() + bytes.len())
                    .max(self.bytes.capacity().saturating_mul(2))
                    .min(self.maximum);
                self.bytes.reserve_exact(capacity - self.bytes.len());
            }
            self.bytes.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let mut writer = BoundedWriter {
        bytes: Vec::new(),
        maximum,
    };
    serde_json::to_writer(&mut writer, value)
        .map_err(|_| invalid("retained artifact JSON exceeds its byte bound"))?;
    Ok(writer.bytes)
}

/// Hash the same canonical JSON bytes used for replay metadata without retaining
/// a second full encoded value. The bound is checked before each hash update.
pub(crate) fn digest_bounded<T: Serialize>(
    value: &T,
    maximum: usize,
) -> io::Result<([u8; 32], usize)> {
    use sha2::{Digest, Sha256};
    struct DigestWriter {
        state: Sha256,
        bytes: usize,
        maximum: usize,
    }
    impl Write for DigestWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.bytes = self
                .bytes
                .checked_add(bytes.len())
                .filter(|count| *count <= self.maximum)
                .ok_or_else(|| invalid("retained artifact JSON exceeds its byte bound"))?;
            self.state.update(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let mut writer = DigestWriter {
        state: Sha256::new(),
        bytes: 0,
        maximum,
    };
    serde_json::to_writer(&mut writer, value)
        .map_err(|_| invalid("retained artifact JSON exceeds its byte bound"))?;
    Ok((writer.state.finalize().into(), writer.bytes))
}

pub(crate) fn unsigned(value: &Value) -> Option<u64> {
    value.as_u64().or_else(|| {
        let number = value.as_f64()?;
        ((0.0..18446744073709551616.0).contains(&number) && number.fract() == 0.0)
            .then_some(number as u64)
    })
}

/// Python document equality treats integral JSON floats as equal to integers.
pub(crate) fn equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(l, r)| equal(l, r))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, l)| right.get(key).is_some_and(|r| equal(l, r)))
        }
        (Value::Number(left), Value::Number(right)) => {
            if left.is_f64() && right.is_f64() {
                return left.as_f64() == right.as_f64();
            }
            let integer = |number: &serde_json::Number| {
                number
                    .as_i64()
                    .map(i128::from)
                    .or_else(|| number.as_u64().map(i128::from))
            };
            match (integer(left), integer(right)) {
                (Some(l), Some(r)) => l == r,
                (Some(i), None) | (None, Some(i)) => {
                    let f = if left.is_f64() {
                        left.as_f64()
                    } else {
                        right.as_f64()
                    }
                    .unwrap();
                    f.fract() == 0.0
                        && (-9223372036854775808.0..18446744073709551616.0).contains(&f)
                        && f as i128 == i
                }
                _ => false,
            }
        }
        (Value::Bool(b), Value::Number(n)) | (Value::Number(n), Value::Bool(b)) => {
            n.as_f64() == Some(if *b { 1.0 } else { 0.0 })
        }
        _ => left == right,
    }
}

pub(crate) fn lower_hex(value: &str, minimum: usize, maximum: usize) -> bool {
    (minimum..=maximum).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn matches_schema(schema: &Value, value: &Value, root: &Value, depth: usize) -> bool {
    if depth > 16 {
        return false;
    }
    let next = |schema: &Value, value: &Value| matches_schema(schema, value, root, depth + 1);
    if let Some(reference) = schema.get("$ref").and_then(Value::as_str) {
        if !root
            .pointer(reference.trim_start_matches('#'))
            .is_some_and(|s| next(s, value))
        {
            return false;
        }
    }
    if let Some(ty) = schema.get("type") {
        let matches_type = |ty: &Value| match ty.as_str() {
            Some("object") => value.is_object(),
            Some("array") => value.is_array(),
            Some("string") => value.is_string(),
            Some("boolean") => value.is_boolean(),
            Some("null") => value.is_null(),
            Some("number") => value.is_number(),
            Some("integer") => {
                value.is_number() && value.as_f64().is_some_and(|n| n.fract() == 0.0)
            }
            _ => false,
        };
        if !ty
            .as_array()
            .map_or_else(|| matches_type(ty), |types| types.iter().any(matches_type))
        {
            return false;
        }
    }
    // JSON Schema const equality distinguishes boolean and numeric types.
    let schema_equal =
        |other: &Value| value.is_boolean() == other.is_boolean() && equal(value, other);
    if schema.get("const").is_some_and(|v| !schema_equal(v))
        || schema
            .get("enum")
            .and_then(Value::as_array)
            .is_some_and(|vs| !vs.iter().any(schema_equal))
    {
        return false;
    }
    if let Some(number) = value.as_f64() {
        if schema
            .get("minimum")
            .and_then(Value::as_f64)
            .is_some_and(|n| number < n)
            || schema
                .get("maximum")
                .and_then(Value::as_f64)
                .is_some_and(|n| number > n)
        {
            return false;
        }
    }
    if let Some(text) = value.as_str() {
        let count = text.chars().count() as u64;
        if schema
            .get("minLength")
            .and_then(Value::as_u64)
            .is_some_and(|n| count < n)
            || schema
                .get("maxLength")
                .and_then(Value::as_u64)
                .is_some_and(|n| count > n)
        {
            return false;
        }
        if let Some(pattern) = schema.get("pattern").and_then(Value::as_str) {
            let valid = match pattern {
                "^[0-9a-f]{24}$" => lower_hex(text, 24, 24),
                "^[0-9a-f]{32}$" => lower_hex(text, 32, 32),
                "^[0-9a-f]{64}$" => lower_hex(text, 64, 64),
                "^[a-zA-Z0-9_-]{1,128}$" => {
                    (1..=128).contains(&text.len())
                        && text
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
                }
                _ => false,
            };
            if !valid {
                return false;
            }
        }
    }
    if let Some(values) = value.as_array() {
        if schema
            .get("maxItems")
            .and_then(Value::as_u64)
            .is_some_and(|n| values.len() as u64 > n)
            || schema
                .get("items")
                .is_some_and(|s| values.iter().any(|v| !next(s, v)))
        {
            return false;
        }
    }
    if let Some(object) = value.as_object() {
        if schema
            .get("required")
            .and_then(Value::as_array)
            .is_some_and(|keys| {
                keys.iter()
                    .any(|k| !k.as_str().is_some_and(|k| object.contains_key(k)))
            })
        {
            return false;
        }
        for (key, value) in object {
            match schema.get("properties").and_then(|p| p.get(key)) {
                Some(s) if !next(s, value) => return false,
                None if schema.get("additionalProperties") == Some(&Value::Bool(false)) => {
                    return false
                }
                _ => (),
            }
        }
    }
    if schema
        .get("anyOf")
        .and_then(Value::as_array)
        .is_some_and(|ss| !ss.iter().any(|s| next(s, value)))
        || schema
            .get("allOf")
            .and_then(Value::as_array)
            .is_some_and(|ss| !ss.iter().all(|s| next(s, value)))
    {
        return false;
    }
    !(schema.get("if").is_some_and(|s| next(s, value))
        && schema.get("then").is_some_and(|s| !next(s, value)))
}

/// Validate one public document/reading/receipt definition from the same schema.
pub(crate) fn validate_definition(value: &Value, definition: &str) -> io::Result<()> {
    static SCHEMA: OnceLock<Option<Value>> = OnceLock::new();
    let schema = SCHEMA
        .get_or_init(|| serde_json::from_str(limits::RECEIPT_SCHEMA).ok())
        .as_ref()
        .ok_or_else(|| invalid("invalid generated paper receipt schema"))?;
    let definition_schema = schema["$defs"]
        .get(definition)
        .ok_or_else(|| invalid("unknown paper schema definition"))?;
    if (value.get("schema_version").is_some() && value["schema_version"].as_u64() != Some(1))
        || !matches_schema(definition_schema, value, schema, 0)
    {
        return Err(invalid("invalid paper schema value"));
    }
    if definition == "PaperBuildReceiptV1" {
        let mut names = std::collections::BTreeSet::new();
        if value["artifacts"]
            .as_array()
            .unwrap()
            .iter()
            .any(|a| !names.insert(a["name"].as_str().unwrap()))
            || (value["status"] == "success"
                && (!names.contains("document.pdf")
                    || value["diagnostics"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|d| d["severity"] == "error")))
        {
            return Err(invalid("invalid build artifacts or success diagnostics"));
        }
    }
    Ok(())
}

/// Returns logical reserved bytes (receipt encoding plus all declared artifacts).
pub(crate) fn validate(receipt: &Value) -> io::Result<usize> {
    let receipt_bytes = encode_bounded(receipt, limits::RETAINED_RECEIPT_BYTES)?.len();
    validate_definition(receipt, "PaperBuildReceiptV1")?;
    let artifacts = receipt["artifacts"]
        .as_array()
        .ok_or_else(|| invalid("invalid artifacts"))?;
    let mut names = std::collections::BTreeSet::new();
    let mut payload_bytes = 0usize;
    for artifact in artifacts {
        if !names.insert(artifact["name"].as_str().unwrap()) {
            return Err(invalid("duplicate paper build artifact"));
        }
        let size =
            unsigned(&artifact["size_bytes"]).ok_or_else(|| invalid("invalid artifact size"))?;
        if size > limits::ARTIFACT_BYTES as u64 {
            return Err(invalid("artifact exceeds its byte bound"));
        }
        payload_bytes += size as usize;
    }
    if payload_bytes > limits::BUILD_OUTPUT_BYTES
        || receipt["status"] != "success"
        || !names.contains("document.pdf")
        || receipt["diagnostics"]
            .as_array()
            .unwrap()
            .iter()
            .any(|d| d["severity"] == "error")
    {
        return Err(invalid(
            "retained receipt must describe a bounded successful build",
        ));
    }
    Ok(receipt_bytes + payload_bytes)
}
