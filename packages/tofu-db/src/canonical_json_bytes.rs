//! Recognize serde_json Value's canonical bytes without building its object tree.
//! Entry points: canonicalize and canonicalize_with_depth return borrowed bytes
//! only after strict parsing,
//! ascending unique object keys and byte-for-byte serializer comparison. Other
//! inputs use the original Value parser/serializer, preserving legacy semantics.
//! canonicalize_with_depth also certifies container depth for immutable response
//! composition. Pure recognition owns no persisted representation or I/O.
//! The cache child composes certificates only with completed scoped blob reads.

use std::{borrow::Cow, fmt, io::{self, Write}};
use serde::{Serialize, de::{Deserialize, DeserializeSeed, Deserializer, MapAccess, SeqAccess, Visitor}};

#[path = "canonical_json_cache.rs"]
pub(crate) mod cache;

/// A depth certificate exists only for strictly recognized canonical input.
/// Legacy conversion is byte-compatible but does not establish this proof.
pub(crate) struct CanonicalJsonBody<'a> {
    bytes: Cow<'a, [u8]>,
    maximum_container_depth: Option<usize>,
}
impl CanonicalJsonBody<'_> {
    pub(crate) fn bytes(&self) -> &[u8] {
        &self.bytes
    }
    pub(crate) fn maximum_container_depth(&self) -> Option<usize> {
        self.maximum_container_depth
    }
}
pub(crate) fn canonicalize(bytes: &[u8]) -> Result<Cow<'_, [u8]>, serde_json::Error> {
    canonicalize_with_depth(bytes).map(|body| body.bytes)
}
pub(crate) fn canonicalize_with_depth(
    bytes: &[u8],
) -> Result<CanonicalJsonBody<'_>, serde_json::Error> {
    let mut comparison = Comparison {
        input: bytes,
        offset: 0,
        borrowed_strings: 0,
        borrowed_string_bytes: 0,
        container_depth: 0,
        maximum_container_depth: 0,
    };
    let mut parser = serde_json::Deserializer::from_slice(bytes);
    let recognized = JsonSeed(&mut comparison)
        .deserialize(&mut parser)
        .and_then(|()| parser.end());
    // Aggregate once per recognition attempt. Never do per-string TLS probes.
    crate::request_profile::work(
        crate::request_profile::Work::JsonBorrowedStrings,
        comparison.borrowed_strings,
    );
    crate::request_profile::work(
        crate::request_profile::Work::JsonBorrowedStringBytes,
        comparison.borrowed_string_bytes,
    );
    if recognized.is_ok() && comparison.offset == bytes.len() {
        return Ok(CanonicalJsonBody {
            bytes: Cow::Borrowed(bytes),
            maximum_container_depth: Some(comparison.maximum_container_depth),
        });
    }
    // Recognition failure is never acceptance: the previous parser owns all
    // malformed-input, duplicate-key, numeric and reserved-marker behavior.
    let _fallback = crate::fsync_stats::StageTimer::diagnostic(
        crate::fsync_stats::STAGE_DOCUMENT_JSON_FALLBACK,
    );
    let value = serde_json::from_slice::<serde_json::Value>(bytes)?;
    serde_json::to_vec(&value).map(|bytes| CanonicalJsonBody {
        bytes: Cow::Owned(bytes),
        maximum_container_depth: None,
    })
}

struct Comparison<'input> {
    input: &'input [u8],
    offset: usize,
    borrowed_strings: u64,
    borrowed_string_bytes: u64,
    container_depth: usize,
    maximum_container_depth: usize,
}

impl Comparison<'_> {
    fn value<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), serde_json::Error> {
        serde_json::to_writer(self, value)
    }

    fn borrowed_string(&mut self, value: &str) -> Result<(), serde_json::Error> {
        // SliceRead lends strings only after validating UTF-8, controls and
        // absence of escapes. Compact serde serialization emits those contents
        // unchanged. Prove this is precisely the input token, including quotes;
        // equal text from another allocation does not establish that proof.
        let start = self.offset.checked_add(1);
        let end = start.and_then(|start| start.checked_add(value.len()));
        if let (Some(start), Some(end)) = (start, end) {
            if self.input.get(self.offset) == Some(&b'"')
                && self.input.get(end) == Some(&b'"')
                && self.input.get(start..end).is_some_and(|contents|
                    std::ptr::eq(contents.as_ptr(), value.as_ptr())) {
                self.offset = end + 1;
                self.borrowed_strings += 1;
                self.borrowed_string_bytes += value.len() as u64;
                return Ok(());
            }
        }
        // This helper accepts no new grammar. The ordinary serializer still
        // owns all other string sources/positions, including escaped keys.
        self.value(value)
    }
}

impl Write for Comparison<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if !self.input[self.offset..].starts_with(bytes) {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "JSON needs canonicalization"));
        }
        self.offset += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}

struct JsonSeed<'state, 'input>(&'state mut Comparison<'input>);

impl<'de> DeserializeSeed<'de> for JsonSeed<'_, 'de> {
    type Value = ();
    fn deserialize<D: Deserializer<'de>>(self, parser: D) -> Result<(), D::Error> {
        parser.deserialize_any(JsonVisitor(self.0))
    }
}

struct JsonVisitor<'state, 'input>(&'state mut Comparison<'input>);

impl<'de> Visitor<'de> for JsonVisitor<'_, 'de> {
    type Value = ();
    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("canonical JSON")
    }
    fn visit_bool<E: serde::de::Error>(self, value: bool) -> Result<(), E> {
        self.0.value(&value).map_err(E::custom)
    }
    fn visit_i64<E: serde::de::Error>(self, value: i64) -> Result<(), E> {
        self.0.value(&value).map_err(E::custom)
    }
    fn visit_u64<E: serde::de::Error>(self, value: u64) -> Result<(), E> {
        self.0.value(&value).map_err(E::custom)
    }
    fn visit_f64<E: serde::de::Error>(self, value: f64) -> Result<(), E> {
        self.0.value(&value).map_err(E::custom)
    }
    fn visit_unit<E: serde::de::Error>(self) -> Result<(), E> {
        self.0.write_all(b"null").map_err(E::custom)
    }
    fn visit_borrowed_str<E: serde::de::Error>(self, value: &'de str) -> Result<(), E> {
        self.0.borrowed_string(value).map_err(E::custom)
    }
    fn visit_str<E: serde::de::Error>(self, value: &str) -> Result<(), E> {
        // Escaped strings cause many tiny serializer writes. Encode once with
        // serde's Vec writer, then compare one contiguous byte range. This
        // temporary releases before the next value; ordinary strings borrow.
        let encoded = serde_json::to_vec(value).map_err(E::custom)?;
        self.0.write_all(&encoded).map_err(E::custom)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<(), A::Error> {
        self.0.write_all(b"[").map_err(serde::de::Error::custom)?;
        self.0.container_depth += 1;
        self.0.maximum_container_depth = self.0.maximum_container_depth.max(self.0.container_depth);
        // next_element_seed must consume only present values, so its visitor
        // owns separators. A separate state tracks them across this sequence.
        let mut first = true;
        while sequence.next_element_seed(ElementSeed { comparison: self.0, first: &mut first })?.is_some() {}
        self.0.container_depth -= 1;
        self.0.write_all(b"]").map_err(serde::de::Error::custom)
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        self.0.write_all(b"{").map_err(serde::de::Error::custom)?;
        self.0.container_depth += 1;
        self.0.maximum_container_depth = self.0.maximum_container_depth.max(self.0.container_depth);
        let mut previous: Option<Cow<'de, str>> = None;
        while let Some(key) = map.next_key::<Key<'de>>()? {
            if previous.is_none() && matches!(key.0.as_ref(),
                "$serde_json::private::RawValue" | "$serde_json::private::Number") {
                return Err(serde::de::Error::custom("serde_json Value marker needs canonicalization"));
            }
            if let Some(previous) = &previous {
                if previous.as_ref() >= key.0.as_ref() {
                    return Err(serde::de::Error::custom("JSON keys need canonicalization"));
                }
                self.0.write_all(b",").map_err(serde::de::Error::custom)?;
            }
            match &key.0 {
                Cow::Borrowed(value) => self.0.borrowed_string(value),
                Cow::Owned(value) => self.0.value(value),
            }.map_err(serde::de::Error::custom)?;
            self.0.write_all(b":").map_err(serde::de::Error::custom)?;
            map.next_value_seed(JsonSeed(self.0))?;
            previous = Some(key.0);
        }
        self.0.container_depth -= 1;
        self.0.write_all(b"}").map_err(serde::de::Error::custom)
    }
}

struct ElementSeed<'state, 'input> {
    comparison: &'state mut Comparison<'input>,
    first: &'state mut bool,
}

impl<'de> DeserializeSeed<'de> for ElementSeed<'_, 'de> {
    type Value = ();
    fn deserialize<D: Deserializer<'de>>(self, parser: D) -> Result<(), D::Error> {
        if !*self.first {
            self.comparison.write_all(b",").map_err(serde::de::Error::custom)?;
        }
        *self.first = false;
        JsonSeed(self.comparison).deserialize(parser)
    }
}

struct Key<'input>(Cow<'input, str>);

impl<'de> Deserialize<'de> for Key<'de> {
    fn deserialize<D: Deserializer<'de>>(parser: D) -> Result<Self, D::Error> {
        parser.deserialize_str(KeyVisitor)
    }
}

struct KeyVisitor;

impl<'de> Visitor<'de> for KeyVisitor {
    type Value = Key<'de>;
    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a JSON object key")
    }
    fn visit_borrowed_str<E>(self, key: &'de str) -> Result<Self::Value, E> {
        Ok(Key(Cow::Borrowed(key)))
    }
    fn visit_str<E>(self, key: &str) -> Result<Self::Value, E> {
        Ok(Key(Cow::Owned(key.to_owned())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_same(bytes: &[u8]) {
        let old = serde_json::from_slice::<serde_json::Value>(bytes).and_then(|v| serde_json::to_vec(&v));
        let new = canonicalize(bytes);
        match (old, new) {
            (Ok(old), Ok(new)) => assert_eq!(old.as_slice(), new.as_ref(), "different canonical bytes"),
            (Err(_), Err(_)) => {},
            _ => panic!("different acceptance for {:?}", String::from_utf8_lossy(bytes)),
        }
    }




    #[test]
    fn depth_certificates_cover_nested_containers_and_exclude_legacy_conversion() {
        for depth in 0..=140 {
            for (open,close) in [("[","]"),("{\"a\":","}")] {
                let bytes=format!("{}null{}",open.repeat(depth),close.repeat(depth)).into_bytes();
                match canonicalize_with_depth(&bytes) {
                    Ok(body)=>assert_eq!(body.maximum_container_depth(),Some(depth)),
                    Err(_)=>assert!(serde_json::from_slice::<serde_json::Value>(&bytes).is_err()),
                }
            }
        }
        for bytes in [b" null ".as_slice(),b"-0",br#"{"z":1,"a":2}"#,br#"{"a":1,"a":2}"#] {
            let body=canonicalize_with_depth(bytes).unwrap();assert_eq!(body.maximum_container_depth(),None);
            assert_eq!(body.bytes(),serde_json::to_vec(&serde_json::from_slice::<serde_json::Value>(bytes).unwrap()).unwrap());
        }
    }

    #[test]
    fn borrowed_string_proof_requires_exact_input_position_and_alias() {
        let input = br#""same""#;
        let mut comparison = Comparison { input, offset: 0, borrowed_strings: 0, borrowed_string_bytes: 0, container_depth: 0, maximum_container_depth: 0 };
        let external = String::from("same");
        comparison.borrowed_string(&external).unwrap();
        assert_eq!(comparison.offset, input.len());
        assert_eq!(comparison.borrowed_strings, 0);
        comparison.offset = 0;
        comparison.borrowed_string(std::str::from_utf8(&input[1..5]).unwrap()).unwrap();
        assert_eq!((comparison.borrowed_strings, comparison.borrowed_string_bytes), (1, 4));
        comparison.offset = input.len();
        assert!(comparison.borrowed_string("same").is_err());
        let input = b"\"\"";
        let mut empty = Comparison { input, offset: 0, borrowed_strings: 0, borrowed_string_bytes: 0, container_depth: 0, maximum_container_depth: 0 };
        empty.borrowed_string(std::str::from_utf8(&input[1..1]).unwrap()).unwrap();
        assert_eq!((empty.offset, empty.borrowed_strings, empty.borrowed_string_bytes), (2, 1, 0));
    }

    #[test]
    fn every_unescaped_unicode_scalar_keeps_compact_value_serialization() {
        let check = |text: &str| {
            // Independent raw tokens: serializer-generated inputs would hide
            // a future change to its escaping rules. Check values and keys.
            let mut bytes = Vec::with_capacity(text.len() + 2);
            bytes.push(b'"');
            bytes.extend_from_slice(text.as_bytes());
            bytes.push(b'"');
            assert!(matches!(canonicalize(&bytes).unwrap(), Cow::Borrowed(_)));
            assert_same(&bytes);
            let mut object = Vec::with_capacity(bytes.len() + 7);
            object.push(b'{');
            object.extend_from_slice(&bytes);
            object.extend_from_slice(b":null}");
            assert!(matches!(canonicalize(&object).unwrap(), Cow::Borrowed(_)));
            assert_same(&object);
        };
        let mut chunk = String::new();
        for scalar in 0x20..=0x10ffff {
            if let Some(character) = char::from_u32(scalar) {
                if character != '"' && character != '\\' {
                    chunk.push(character);
                    if chunk.len() >= 4096 { check(&chunk); chunk.clear(); }
                }
            }
        }
        check(&chunk);
        check("");
    }

    #[test]
    fn escaped_and_whitespace_tokens_keep_exact_previous_output() {
        for input in [br#""a\u0062""#.as_slice(),br#"{"\u0061":"plain"}"#,
            br#""\u2028\u2029\u007f""#,br#""\\\"\n\r\t\b\f\u0000""#,
            br#" {"": "", "plain": "plain"} "#,br#"{"a":"value", "b":"other"}"#] {
            assert_same(input);
        }
    }

    #[test]
    fn canonical_containers_and_scalars_borrow_the_original_input() {
        for value in [serde_json::json!(null), serde_json::json!(true), serde_json::json!(-123),
            serde_json::json!(u64::MAX), serde_json::json!(-0.0), serde_json::json!(1.23456789e-123),
            serde_json::json!("你好😀\n\\\""), serde_json::json!({"text":"\n\\\"".repeat(65536)}), serde_json::json!([]), serde_json::json!({}),
            serde_json::json!({"a":[1,{"key":"v","other":[null,true,false]}],"b":"x"})] {
            let bytes = serde_json::to_vec(&value).unwrap();
            assert!(matches!(canonicalize(&bytes).unwrap(), Cow::Borrowed(_)), "{:?}", value);
        }
    }

    #[test]
    fn whitespace_duplicates_key_order_and_number_spellings_keep_value_semantics() {
        for text in [" null ", "[1, 2]", "{\"z\":1,\"a\":2}", "{\"a\":1,\"a\":2}",
            "{\"a\":{\"b\":1,\"a\":2}}", "1e0", "1.000", "-0", "1e-9999",
            "18446744073709551616", "-9223372036854775809", "10000000000000000000000000000000000",
            r#"{"\u0061":"\u0041","b":"\/"}"#, r#"{"b":1,"\u0061":2}"#] {
            assert_same(text.as_bytes());
        }
    }

    #[test]
    fn serde_markers_and_escaped_marker_names_use_original_interpretation() {
        for text in [r#"{"$serde_json::private::RawValue":"[1, 2]"}"#,
            r#"{"$serde_json::private::RawValue":"not json"}"#,
            r#"{"\u0024serde_json::private::RawValue":"1e309"}"#,
            r#"{"$serde_json::private::RawValue":"null","extra":0}"#,
            r#"{"$serde_json::private::Number":"1e309"}"#,
            r#"[{"a":{"$serde_json::private::RawValue":"{\"z\":1,\"a\":2}"}}]"#,
            r#"{"first":0,"$serde_json::private::RawValue":"not json"}"#] {
            assert_same(text.as_bytes());
        }
    }

    #[test]
    fn malformed_utf8_escapes_numbers_and_trailing_data_remain_rejected() {
        for bytes in [b"\"\xff\"".as_slice(), b"{\"\xff\":0}", br#""\uD800""#,
            br#""\uDC00""#, br#""\uD800\u0000""#, b"\"\x01\"", b"1e309", b"-1e309",
            b"01", b"1.", b"[1,]", b"{\"a\":1,}", b"{}[]", b"{}\0", b"", b"[}"] {
            assert_same(bytes);
            assert!(canonicalize(bytes).is_err());
        }
    }

    #[test]
    fn recursion_limits_and_escaped_object_keys_match_the_original_parser() {
        for depth in 1..=140 {
            for (open, close) in [("[", "]"), ("{\"a\":", "}")] {
                assert_same(format!("{}null{}",open.repeat(depth),close.repeat(depth)).as_bytes());
            }
        }
        for text in [r#"{"\n":0,"a":1}"#,r#"{"a":0,"\n":1}"#,
            r#"{"a":0,"\u0061":1}"#,r#"{"\ud83d\ude00":1,"😀":2}"#] {
            assert_same(text.as_bytes());
        }
    }

    #[test]
    fn deterministic_byte_mutations_preserve_canonical_output_and_acceptance() {
        let seed = br#"{"a":[0,-1,1.25e3,{"k":"\uD83D\uDE00"}],"b":true,"c":null}"#;
        for end in 0..=seed.len() { assert_same(&seed[..end]); }
        for index in 0..seed.len() {
            for byte in [0,1,b' ',b'"',b'\\',b'[',b']',b'{',b'}',b':',b',',b'9',0xff] {
                let mut changed = seed.to_vec();changed[index] = byte;assert_same(&changed);
            }
        }
    }
}
