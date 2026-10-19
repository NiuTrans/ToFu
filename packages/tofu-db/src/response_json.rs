//! Validate encoded semantic responses without rebuilding their JSON objects.
//! Entry point: validate. Uses the same serde_json parser and recursion budget
//! as Value; ordinary arrays, maps and strings retain no decoded objects.
//! Serde's reserved map markers fall back to Value to preserve its special
//! raw-value behavior rather than changing the existing integrity boundary.
//! EncodedResponse freezes a materialized Value and owns its canonical/depth
//! evidence; only its Value encoder can mint that evidence.

/// The pinned serde_json default budget starts at 128 and rejects zero after
/// entering a container. Producer proofs include every added envelope layer.
/// Depth-oracle tests must pass when the dependency/configuration changes.
pub(crate) const MAX_VALIDATED_CONTAINER_DEPTH: usize = 127;

use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use std::fmt;

/// Owned encoded response and producer-bound canonical/depth evidence. Mutable
/// extraction consumes the evidence; slot replacement retains its own fences.
pub(crate) struct EncodedResponse {
    bytes: Vec<u8>,
    maximum_container_depth: Option<usize>,
}

impl EncodedResponse {
    /// Freeze a complete materialized response. The same Value serializer owns
    /// both canonical spelling and encoding; strings need no second parse.
    pub(crate) fn from_value(value: serde_json::Value) -> Result<Self, serde_json::Error> {
        let maximum_container_depth = canonical_value_depth(&value, 0);
        let bytes = serde_json::to_vec(&value)?;
        Ok(Self { bytes, maximum_container_depth })
    }

    pub(crate) fn json_proven(&self) -> bool {
        self.maximum_container_depth.is_some_and(|depth| depth <= MAX_VALIDATED_CONTAINER_DEPTH)
    }

    pub(crate) fn len(&self) -> usize { self.bytes.len() }

    pub(crate) fn into_bytes(self) -> Vec<u8> { self.bytes }
}

/// Walk only the already-materialized structure. Reserved first map keys retain
/// Value's parser compatibility path. Early depth refusal bounds this walk even
/// when a caller constructs a deeper in-memory tree than the parser accepts.
pub(crate) fn canonical_value_depth(value: &serde_json::Value, enclosing: usize) -> Option<usize> {
    use serde_json::Value;
    match value {
        Value::Array(values) => {
            let depth = enclosing.checked_add(1)?;
            if depth > MAX_VALIDATED_CONTAINER_DEPTH { return None; }
            values.iter().try_fold(depth, |maximum, child|
                canonical_value_depth(child, depth).map(|child_depth| maximum.max(child_depth)))
        }
        Value::Object(values) => {
            let depth = enclosing.checked_add(1)?;
            if depth > MAX_VALIDATED_CONTAINER_DEPTH { return None; }
            let mut maximum = depth;
            let mut previous: Option<&str> = None;
            for (key, child) in values {
                if previous.is_none() && matches!(key.as_str(),
                    "$serde_json::private::RawValue" | "$serde_json::private::Number") {
                    return None;
                }
                // Retain canonical ordering if dependency feature unification
                // ever selects a map implementation preserving insertion order.
                if previous.is_some_and(|old| old >= key.as_str()) { return None; }
                previous = Some(key);
                maximum = maximum.max(canonical_value_depth(child, depth)?);
            }
            Some(maximum)
        }
        _ => Some(enclosing),
    }
}

/// Reuse an owned Value only when its encoding is a parser fixed point. Count
/// the exact serialized bytes without retaining a second full payload. Legacy
/// marker/depth cases keep the original serialize/parse normalization and error.
/// This is local materialization evidence, never source integrity or slot proof.
pub(crate) fn normalize_value_with_size(
    value: serde_json::Value,
    maximum_bytes: usize,
) -> Result<(serde_json::Value, usize), serde_json::Error> {
    if canonical_value_depth(&value, 0).is_none() {
        let bytes = serde_json::to_vec(&value)?;
        // The byte API checks its output bound before parsing that output.
        // Preserve that refusal order even for malformed reserved markers.
        if bytes.len() > maximum_bytes {
            return Err(serde_json::Error::io(std::io::Error::new(
                std::io::ErrorKind::OutOfMemory, "JSON exceeds its byte bound")));
        }
        return serde_json::from_slice(&bytes).map(|value| (value, bytes.len()));
    }
    struct ByteCount { bytes: usize, maximum: usize }
    impl std::io::Write for ByteCount {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.bytes = self.bytes.checked_add(bytes.len())
                .filter(|size| *size <= self.maximum)
                .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::OutOfMemory,
                    "JSON exceeds its byte bound"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
    }
    let mut count = ByteCount { bytes: 0, maximum: maximum_bytes };
    serde_json::to_writer(&mut count, &value)?;
    Ok((value, count.bytes))
}

pub(crate) fn validate(bytes: &[u8]) -> Result<(), serde_json::Error> {
    let checked = serde_json::from_slice::<CheckedJson>(bytes)?;
    if checked.requires_value_check {
        serde_json::from_slice::<serde_json::Value>(bytes)?;
    }
    Ok(())
}

struct CheckedJson {
    requires_value_check: bool,
}

impl CheckedJson {
    const ORDINARY: Self = Self { requires_value_check: false };
}

impl<'de> Deserialize<'de> for CheckedJson {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(JsonVisitor)
    }
}

struct JsonVisitor;

impl<'de> Visitor<'de> for JsonVisitor {
    type Value = CheckedJson;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("any valid JSON value")
    }

    fn visit_bool<E>(self, _: bool) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_i64<E>(self, _: i64) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_u64<E>(self, _: u64) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_f64<E>(self, _: f64) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_unit<E>(self) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_str<E>(self, _: &str) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }

    fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<Self::Value, A::Error> {
        let mut checked = CheckedJson::ORDINARY;
        while let Some(element) = sequence.next_element::<CheckedJson>()? {
            checked.requires_value_check |= element.requires_value_check;
        }
        Ok(checked)
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut checked = CheckedJson::ORDINARY;
        let mut first_key = true;
        while let Some(key) = map.next_key::<CheckedKey>()? {
            // Value's raw-value and arbitrary-precision extensions interpret
            // their private marker only in the first key of a map. Decode the
            // key through serde so escaped spellings cannot evade this fence.
            checked.requires_value_check |= first_key && key.serde_marker;
            first_key = false;
            checked.requires_value_check |= map.next_value::<CheckedJson>()?.requires_value_check;
        }
        Ok(checked)
    }
}

struct CheckedKey {
    serde_marker: bool,
}

impl<'de> Deserialize<'de> for CheckedKey {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_str(KeyVisitor)
    }
}

struct KeyVisitor;

impl<'de> Visitor<'de> for KeyVisitor {
    type Value = CheckedKey;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a JSON object key")
    }

    fn visit_str<E>(self, key: &str) -> Result<Self::Value, E> {
        Ok(CheckedKey { serde_marker: matches!(key,
            "$serde_json::private::RawValue" | "$serde_json::private::Number") })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalized_owned_values_match_parser_oracle_and_exact_byte_length() {
        let cases = [
            serde_json::json!({"payload": "汉字\"\\\n\u{1f}".repeat(10_000),
                "numbers": [1.0, -0.0, f64::MIN_POSITIVE, u64::MAX, i64::MIN]}),
            serde_json::json!({"nested": {"$serde_json::private::RawValue": "[1, 2]"}}),
            serde_json::json!({"nested": {"$serde_json::private::RawValue": "broken"}}),
            serde_json::json!({"nested": {"$serde_json::private::Number": "1.25"}}),
        ];
        for value in cases {
            let bytes = serde_json::to_vec(&value).unwrap();
            let expected = serde_json::from_slice::<serde_json::Value>(&bytes);
            let actual = normalize_value_with_size(value, usize::MAX);
            match (actual, expected) {
                (Ok((actual, size)), Ok(expected)) => {
                    assert_eq!(serde_json::to_vec(&actual).unwrap(), serde_json::to_vec(&expected).unwrap());
                    assert_eq!(size, bytes.len());
                }
                (Err(_), Err(_)) => {}
                _ => panic!("owned normalization changed parser acceptance"),
            }
        }
        for depth in 125..=130 {
            let mut value = serde_json::Value::Null;
            for _ in 0..depth { value = serde_json::Value::Array(vec![value]); }
            let bytes = serde_json::to_vec(&value).unwrap();
            assert_eq!(normalize_value_with_size(value, usize::MAX).is_ok(),
                       serde_json::from_slice::<serde_json::Value>(&bytes).is_ok());
        }
    }

    #[test]
    fn ordinary_owned_normalization_retains_the_payload_allocation() {
        let payload = "tool evidence".repeat(32_768);
        let pointer = payload.as_ptr();
        let value = serde_json::Value::Array(vec![serde_json::Value::String(payload)]);
        let (normalized, _) = normalize_value_with_size(value, usize::MAX).unwrap();
        assert_eq!(normalized[0].as_str().unwrap().as_ptr(), pointer);
    }

    #[test]
    fn owned_normalization_preserves_exact_bounds_before_parser_errors() {
        for value in [serde_json::json!(["汉字\n", 1.0]),
            serde_json::json!({"$serde_json::private::RawValue": "[1,2]"}),
            serde_json::json!({"$serde_json::private::RawValue": "broken"})] {
            let bytes = serde_json::to_vec(&value).unwrap();
            assert_eq!(normalize_value_with_size(value.clone(), bytes.len() - 1)
                .unwrap_err().io_error_kind(), Some(std::io::ErrorKind::OutOfMemory));
            assert_eq!(normalize_value_with_size(value, bytes.len()).is_ok(),
                serde_json::from_slice::<serde_json::Value>(&bytes).is_ok());
        }
    }

    #[test]
    fn value_certificate_matches_complete_canonical_parser_and_depth_boundary() {
        for source in ["null", "true", "-0", "-0.0", "1e0", "18446744073709551615",
            "-9223372036854775808", "1e100", "5e-324",
            r#"{"z":[true,null,"汉字\"\\\n"],"a":{"nested":1}}"#] {
            let value: serde_json::Value = serde_json::from_str(source).unwrap();
            let expected = serde_json::to_vec(&value).unwrap();
            let response = EncodedResponse::from_value(value).unwrap();
            assert!(response.json_proven(), "{source}");
            assert_eq!(response.into_bytes(), expected);
            assert_eq!(serde_json::to_vec(&serde_json::from_slice::<serde_json::Value>(&expected).unwrap()).unwrap(), expected);
        }
        for depth in 0..=140 {
            let mut value = serde_json::Value::Null;
            for _ in 0..depth { value = serde_json::Value::Array(vec![value]); }
            let response = EncodedResponse::from_value(value).unwrap();
            let proven = response.json_proven();
            let bytes = response.into_bytes();
            assert_eq!(proven, serde_json::from_slice::<serde_json::Value>(&bytes).is_ok(), "depth {depth}");
        }
    }

    #[test]
    fn nested_reserved_value_markers_keep_the_existing_validation_path() {
        for key in ["$serde_json::private::RawValue", "$serde_json::private::Number"] {
            for child in [serde_json::Value::from("null"), serde_json::Value::from("not JSON"), serde_json::Value::from(1)] {
                let value = serde_json::Value::Array(vec![serde_json::Value::Object(
                    serde_json::Map::from_iter([(key.to_owned(), child)]))]);
                let expected = serde_json::to_vec(&value).unwrap();
                let response = EncodedResponse::from_value(value).unwrap();
                assert!(!response.json_proven());
                assert_eq!(response.into_bytes(), expected);
            }
        }
    }

    #[test]
    fn encoded_number_certificates_preserve_float_roundtrip_bits() {
        let mut bits = 0x934e_6d2b_1ac7_f805_u64;
        for _ in 0..10_000 {
            bits ^= bits << 13;
            bits ^= bits >> 7;
            bits ^= bits << 17;
            let Some(number) = serde_json::Number::from_f64(f64::from_bits(bits)) else { continue; };
            let response = EncodedResponse::from_value(serde_json::Value::Number(number)).unwrap();
            assert!(response.json_proven());
            let bytes = response.into_bytes();
            let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(serde_json::to_vec(&parsed).unwrap(), bytes);
        }
    }

    fn assert_parity(bytes: &[u8]) {
        assert_eq!(validate(bytes).is_ok(), serde_json::from_slice::<serde_json::Value>(bytes).is_ok(),
            "acceptance differs for {:?}", String::from_utf8_lossy(bytes));
    }

    #[test]
    fn preserves_scalar_and_numeric_range_validation() {
        for text in ["null", "true", "false", "0", "-0", "-0.0", "1.5e20", "1e-9999",
            "18446744073709551615", "18446744073709551616", "-9223372036854775809",
            "1e309", "-1e309", "01", "NaN", "Infinity", "1.", "1e", "+1", "true false", ""] {
            assert_parity(text.as_bytes());
        }
    }

    #[test]
    fn preserves_utf8_unicode_escape_and_control_character_validation() {
        for bytes in [br#""plain""#.as_slice(), br#""\uD83D\uDE00""#, br#""\uD800""#,
            br#""\uDC00""#, br#""\uD800\u0000""#, br#""\x20""#, b"\"\xff\"",
            b"{\"\xff\":0}", b"\"\x01\"", b"\"\xc0\xaf\"", b"\"\xf4\x90\x80\x80\"",
            "\"你好😀\"".as_bytes(), br#"{"escaped\u006bey":"\n\t\\\""}"#] {
            assert_parity(bytes);
        }
    }

    #[test]
    fn preserves_container_and_trailing_byte_validation() {
        for text in ["[]", "{}", "[null,{},[true,1]]", "{\"a\":1,\"a\":2}",
            "[1,]", "{\"a\":1,}", "{1:2}", "[1 2]", "{}[]", "{}\0", " \n{}\t ",
            "{\"a\"}", "[", "{", "[}", "{]"] {
            assert_parity(text.as_bytes());
        }
    }

    #[test]
    fn preserves_the_same_recursion_limit() {
        for depth in 1..=140 {
            for (open, close) in [("[", "]"), ("{\"k\":", "}")] {
                assert_parity(format!("{}null{}", open.repeat(depth), close.repeat(depth)).as_bytes());
            }
        }
    }

    #[test]
    fn preserves_reserved_serde_marker_behavior_including_escaped_keys() {
        for text in [
            r#"{"$serde_json::private::RawValue":"null"}"#,
            r#"{"$serde_json::private::RawValue":"not json"}"#,
            r#"{"$serde_json::private::RawValue":"1e309"}"#,
            r#"{"$serde_json::private::RawValue":1}"#,
            r#"{"$serde_json::private::RawValue":"null","extra":1}"#,
            r#"{"\u0024serde_json::private::RawValue":"not json"}"#,
            r#"[0,{"$serde_json::private::RawValue":"not json"}]"#,
            r#"{"first":0,"$serde_json::private::RawValue":"not json"}"#,
            r#"{"$serde_json::private::Number":"1e309"}"#,
        ] { assert_parity(text.as_bytes()); }
        assert!(validate(br#"{"$serde_json::private::RawValue":"not json"}"#).is_err());
    }

    #[test]
    fn deterministic_byte_mutations_match_the_existing_integrity_fence() {
        let seed = br#"{"unicode":"\uD83D\uDE00","numbers":[0,-1,1.25e3],"nested":{"a":[null,true,false]},"repeat":1,"repeat":2}"#;
        for end in 0..=seed.len() { assert_parity(&seed[..end]); }
        for index in 0..seed.len() {
            for byte in [0, 1, b' ', b'"', b'\\', b'[', b']', b'{', b'}', b':', b',', b'9', 0xff] {
                let mut changed = seed.to_vec();
                changed[index] = byte;
                assert_parity(&changed);
            }
        }
    }
}
