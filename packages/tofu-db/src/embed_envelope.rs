//! Borrowed C ABI request-envelope decoding. Entry: `RequestEnvelope::deserialize`.
//! Depends on serde/serde_json only; validates all values and owns no caller data.
//! Collects recognized raw fields with last-key-wins semantics, avoiding a map.

use serde::de::{self, Deserialize, Deserializer, MapAccess, Visitor};
use serde_json::value::RawValue;
use std::fmt;

#[derive(Default)]
pub(super) struct RequestEnvelope<'a> {
    pub operation: Option<&'a RawValue>,
    pub payload: Option<&'a RawValue>,
    pub command_id: Option<&'a RawValue>,
    pub deadline_unix_ms: Option<&'a RawValue>,
}

enum Field { Operation, Payload, CommandId, Deadline, Unknown }
impl<'de> Deserialize<'de> for Field {
    fn deserialize<D: Deserializer<'de>>(decoder: D) -> Result<Self, D::Error> {
        struct FieldVisitor;
        impl Visitor<'_> for FieldVisitor {
            type Value = Field;
            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a string")
            }
            fn visit_str<E: de::Error>(self, value: &str) -> Result<Field, E> {
                Ok(match value {
                    "operation" => Field::Operation,
                    "payload" => Field::Payload,
                    "command_id" => Field::CommandId,
                    "deadline_unix_ms" => Field::Deadline,
                    _ => Field::Unknown,
                })
            }
        }
        decoder.deserialize_str(FieldVisitor)
    }
}

impl<'de> Deserialize<'de> for RequestEnvelope<'de> {
    fn deserialize<D: Deserializer<'de>>(decoder: D) -> Result<Self, D::Error> {
        struct EnvelopeVisitor;
        impl<'de> Visitor<'de> for EnvelopeVisitor {
            type Value = RequestEnvelope<'de>;
            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a map")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut envelope = RequestEnvelope::default();
                while let Some(field) = map.next_key::<Field>()? {
                    // RawValue checks the entire value, including unknown fields.
                    // Do not decode recognized values yet: a later duplicate may
                    // replace an earlier value whose type is not admitted.
                    let value: &'de RawValue = map.next_value()?;
                    match field {
                        Field::Operation => envelope.operation = Some(value),
                        Field::Payload => envelope.payload = Some(value),
                        Field::CommandId => envelope.command_id = Some(value),
                        Field::Deadline => envelope.deadline_unix_ms = Some(value),
                        Field::Unknown => {},
                    }
                }
                Ok(envelope)
            }
        }
        decoder.deserialize_map(EnvelopeVisitor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn parity(input: &[u8]) {
        let old = serde_json::from_slice::<BTreeMap<String, &RawValue>>(input);
        let new = serde_json::from_slice::<RequestEnvelope<'_>>(input);
        match (old, new) {
            (Ok(old), Ok(new)) => {
                for (key, actual) in [("operation", new.operation), ("payload", new.payload),
                    ("command_id", new.command_id), ("deadline_unix_ms", new.deadline_unix_ms)] {
                    let expected = old.get(key).copied();
                    assert_eq!(expected.map(RawValue::get), actual.map(RawValue::get), "{key}");
                    if let Some(raw) = actual {
                        let start = raw.get().as_ptr() as usize;
                        assert!(start >= input.as_ptr() as usize);
                        assert!(start + raw.get().len() <= input.as_ptr() as usize + input.len());
                    }
                }
            },
            (Err(old), Err(new)) => assert_eq!(old.to_string(), new.to_string(), "{input:?}"),
            _ => panic!("envelope acceptance differs: {input:?}"),
        }
    }

    #[test]
    fn raw_fields_preserve_duplicates_escapes_types_and_unknown_validation() {
        for input in [
            r#"{}"#, r#"{"operation":"record.get","payload":{"k":1},"deadline_unix_ms":42}"#,
            r#"{"operation":false,"operation":"record.get","payload":null,"payload":[],"command_id":7,"command_id":"c","deadline_unix_ms":-1,"deadline_unix_ms":42}"#,
            r#"{"operation":"ok","operation":null,"deadline_unix_ms":42,"deadline_unix_ms":null}"#,
            r#"{"oper\u0061tion":"record.get","p\u0061yload":{"n":-0,"wide":18446744073709551616},"command\u005fid":"c","deadline\u005funix_ms":42}"#,
            r#"{"unknown":{"x":[1,true,null,"\\\""]},"operation":"x","unknown":3}"#,
            r#"{"operation":null,"payload":0,"command_id":{},"deadline_unix_ms":1.5}"#,
            r#"{"unknown":1e400,"payload":{"duplicate":1,"duplicate":2}}"#,
            r#"{"unknown":"\uD800"}"#, r#"{"unknown":[1,]}"#, r#"{"operation":"x",}"#,
            r#"{"unknown":{"x":1}"#, r#"[]"#, r#"null"#, r#"1"#, r#""x""#,
            r#"{"operation":"x"} trailing"#,
        ] { parity(input.as_bytes()); }
        parity(b"{\"unknown\":\"\xff\"}");
        let nested = format!("{{\"unknown\":{}0{}}}", "[".repeat(256), "]".repeat(256));
        parity(nested.as_bytes());
    }

    #[test]
    fn byte_mutations_match_original_map_parser_and_borrowed_slices() {
        let seed = br#"{"operation":"record.get","payload":{"key":"x","user_id":11},"deadline_unix_ms":42,"command_id":"c"}"#;
        for position in 0..seed.len() {
            for replacement in [0, b'"', b'\\', b'[', b'}', 0xff] {
                let mut input = seed.to_vec(); input[position] = replacement; parity(&input);
            }
            parity(&seed[..position]);
        }
    }
}
