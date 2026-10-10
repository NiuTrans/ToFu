//! Validate encoded semantic responses without rebuilding their JSON objects.
//! Entry point: validate. Uses the same serde_json parser and recursion budget
//! as Value; ordinary arrays, maps and strings retain no decoded objects.
//! Serde's reserved map markers fall back to Value to preserve its special
//! raw-value behavior rather than changing the existing integrity boundary.

/// The pinned serde_json default budget starts at 128 and rejects zero after
/// entering a container. Producer proofs include every added envelope layer.
/// Depth-oracle tests must pass when the dependency/configuration changes.
pub(crate) const MAX_VALIDATED_CONTAINER_DEPTH: usize = 127;

use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use std::fmt;

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
