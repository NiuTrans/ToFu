//! Borrowed Chat wire normalization over durable history. Field/ordering
//! constants derive from Python; context seams, ghost/empty content and plain
//! blocks normalize only in the request view. No durable mutation or routing.
use crate::chat_content::{truthy, Content};
use serde_core::ser::{SerializeMap, SerializeSeq};
use serde_core::Serialize;
use serde_json::{Map, Value};

pub(crate) struct Messages<'a>(pub &'a [Value], pub bool);
struct Message<'a> {
    fields: &'a Map<String, Value>,
    content: Content<'a>,
}
impl serde_core::Serialize for Messages<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut normalized: Vec<Message<'_>> = Vec::with_capacity(self.0.len());
        for raw in self.0 {
            let fields = raw.as_object().ok_or_else(|| {
                <S::Error as serde_core::ser::Error>::custom("Chat message must be an object")
            })?;
            let mut content = Content::new(fields.get("content"));
            let calls = truthy(fields.get("tool_calls"));
            if calls && content.empty() {
                content = Content::Absent;
            }
            let role = fields.get("role").and_then(Value::as_str).unwrap_or("");
            let meaningful = crate::execution_contract::CHAT_GHOST_FIELDS
                .iter()
                .any(|key| {
                    crate::execution_contract::CHAT_MESSAGE_FIELDS.contains(key)
                        && truthy(fields.get(*key))
                });
            if role == "assistant" && content.empty() && !meaningful {
                continue;
            }
            let message = Message { fields, content };
            if let Some(previous) = normalized.last_mut() {
                if (role == "user" || role == "assistant")
                    && fields.get("role") == previous.fields.get("role")
                    && !calls
                    && !truthy(previous.fields.get("tool_calls"))
                    && !truthy(fields.get("_responses_items"))
                    && !truthy(previous.fields.get("_responses_items"))
                {
                    previous
                        .content
                        .merge(message.content)
                        .map_err(<S::Error as serde_core::ser::Error>::custom)?;
                    continue;
                }
            }
            normalized.push(message);
        }
        let mut sequence = serializer.serialize_seq(Some(normalized.len()))?;
        for mut message in normalized {
            let role = message
                .fields
                .get("role")
                .and_then(Value::as_str)
                .unwrap_or("");
            if (role == "user" || role == "tool") && message.content.empty() {
                message.content = Content::Text(std::borrow::Cow::Borrowed(if role == "tool" {
                    "[empty tool result]"
                } else {
                    "[empty message]"
                }));
            }
            if !self.1 {
                message.content.collapse();
            }
            sequence.serialize_element(&message)?;
        }
        sequence.end()
    }
}
impl serde_core::Serialize for Message<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(None)?;
        for key in crate::execution_contract::CHAT_MESSAGE_ORDER.iter().chain(
            crate::execution_contract::CHAT_MESSAGE_FIELDS
                .iter()
                .filter(|k| !crate::execution_contract::CHAT_MESSAGE_ORDER.contains(k)),
        ) {
            if !crate::execution_contract::CHAT_MESSAGE_FIELDS.contains(key) {
                continue;
            }
            if *key == "content" {
                if !matches!(self.content, Content::Absent) {
                    map.serialize_entry(key, &self.content)?;
                }
            } else if let Some(value) = self.fields.get(*key) {
                map.serialize_entry(key, &Field(key, value))?;
            }
        }
        map.end()
    }
}

struct Field<'a>(&'a str, &'a Value);
struct Ordered<'a>(&'a Value, &'a [&'a str]);
impl Serialize for Field<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let order = match self.0 {
            "tool_calls" => crate::execution_contract::CHAT_TOOL_CALL_ORDER,
            "reasoning_details" => crate::execution_contract::CHAT_REASONING_ORDER,
            "function" => {
                return Ordered(self.1, crate::execution_contract::CHAT_FUNCTION_ORDER)
                    .serialize(serializer)
            }
            _ => return self.1.serialize(serializer),
        };
        if let Some(values) = self.1.as_array() {
            let mut sequence = serializer.serialize_seq(Some(values.len()))?;
            for value in values {
                sequence.serialize_element(&Ordered(value, order))?;
            }
            sequence.end()
        } else {
            self.1.serialize(serializer)
        }
    }
}
impl Serialize for Ordered<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let Some(fields) = self.0.as_object() else {
            return self.0.serialize(serializer);
        };
        let mut map = serializer.serialize_map(Some(fields.len()))?;
        for key in self.1 {
            if let Some(value) = fields.get(*key) {
                map.serialize_entry(key, &Field(key, value))?;
            }
        }
        let mut rest: Vec<_> = fields
            .iter()
            .filter(|(k, _)| !self.1.contains(&k.as_str()))
            .collect();
        rest.sort_by_key(|(key, _)| *key);
        for (key, value) in rest {
            map.serialize_entry(key, value)?;
        }
        map.end()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn python_shared_chat_stages_match_values_and_wire_order_without_history_mutation() {
        let external = std::env::var("TOFU_CHAT_WIRE_ORACLE")
            .ok()
            .map(|path| std::fs::read_to_string(path).unwrap());
        let cases: Value = serde_json::from_str(
            external
                .as_deref()
                .unwrap_or(include_str!("../tests/fixtures/python_chat_wire.json")),
        )
        .unwrap();
        for case in cases.as_array().unwrap() {
            let messages = case["messages"].as_array().unwrap();
            let before = messages.clone();
            let preserve = case["preserve"].as_bool().unwrap();
            assert_eq!(
                serde_json::to_value(Messages(messages, preserve)).unwrap(),
                case["output"],
                "value case {} preserve {preserve}",
                case["name"]
            );
            let wire = serde_json::to_string(&Messages(messages, preserve)).unwrap();
            if cfg!(feature = "provider-json-order") {
                assert_eq!(
                    wire,
                    case["wire"].as_str().unwrap(),
                    "wire case {} preserve {preserve}",
                    case["name"]
                );
            } else {
                // Checkpoint-only consumers opt out of provider insertion order.
                // Their normalization must still preserve the same values.
                assert_eq!(
                    serde_json::from_str::<Value>(&wire).unwrap(),
                    case["output"]
                );
            }
            assert_eq!(messages, &before);
        }
    }
    #[test]
    fn projection_preserves_nested_user_keys_and_durable_history() {
        let history = vec![json!({"role":"assistant","content":"answer",
            "_contextComposer":true,"carrier":"host","unknown_future_metadata":7,
            "usage":{"tokens":9},"tool_calls":[{"id":"call","type":"function",
            "function":{"name":"echo","arguments":"{\"_contextComposer\":true}"}}],
            "reasoning_details":[{"type":"thinking","thinking":"proof","signature":"sig"}]})];
        let before = history.clone();
        let wire = serde_json::to_value(Messages(&history, true)).unwrap();
        assert_eq!(wire[0].as_object().unwrap().len(), 4);
        assert_eq!(wire[0]["tool_calls"], history[0]["tool_calls"]);
        assert_eq!(
            wire[0]["reasoning_details"],
            history[0]["reasoning_details"]
        );
        assert_eq!(history, before);
        assert!(serde_json::to_value(Messages(&[Value::Null], true)).is_err());
    }
}
