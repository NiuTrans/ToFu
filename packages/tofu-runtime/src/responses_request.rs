//! Borrowed core Responses request serialization, exact aliases and replay.
//! Caller supplies declared reasoning-replay support. Hosted/program/agent,
//! compaction and credential refresh require separate owners and refuse here.
use crate::execution_contract::{
    RESPONSES_ALIAS_DIGEST_CHARS as NAME_DIGEST, RESPONSES_ALIAS_MAX_CHARS as NAME_MAX,
    RESPONSES_ALIAS_PREFIX_CHARS as NAME_PREFIX,
};
use serde_core::{
    ser::{Error, SerializeMap, SerializeSeq},
    Serialize, Serializer,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
/// Trusted upstream wire dialect; model names never select a profile.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Profile {
    #[default]
    Compatible,
    OpenAI,
    Codex,
}
impl Profile {
    pub fn parse(value: &str) -> Result<Self, &'static str> {
        match value {
            "compatible" => Ok(Self::Compatible),
            "openai" => Ok(Self::OpenAI),
            "codex" => Ok(Self::Codex),
            _ => Err("native Responses profile invalid"),
        }
    }
}
pub struct Body<'a> {
    pub profile: Profile,
    pub template: &'a Map<String, Value>,
    pub messages: &'a [Value],
    pub output: Option<u64>,
    pub replay: bool,
    pub reverse: &'a BTreeMap<String, String>,
}
fn alias<'a>(name: &'a str, reverse: &'a BTreeMap<String, String>) -> &'a str {
    reverse
        .iter()
        .find_map(|(a, n)| (n == name).then_some(a.as_str()))
        .unwrap_or(name)
}
pub fn aliases(
    template: &Map<String, Value>,
    messages: &[Value],
) -> Result<BTreeMap<String, String>, &'static str> {
    let mut names = BTreeSet::new();
    let mut add = |name: &Value| -> Result<(), &'static str> {
        let name = name
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or("native Responses tool name invalid")?;
        names.insert(name.to_owned());
        Ok(())
    };
    if let Some(tools) = template.get("tools") {
        for tool in tools.as_array().ok_or("native Responses tools invalid")? {
            if tool["type"] == "function" {
                add(&tool["function"]["name"])?;
            } else {
                return Err("native Responses hosted tools unsupported");
            }
        }
    }
    for message in messages {
        if let Some(calls) = message.get("tool_calls").filter(|v| !v.is_null()) {
            for call in calls.as_array().ok_or("native Responses calls invalid")? {
                add(&call["function"]["name"])?;
            }
        }
    }
    if template
        .get("tool_choice")
        .is_some_and(|v| v["type"] == "function")
    {
        add(&template["tool_choice"]["function"]["name"])?;
    }
    let mut occupied: BTreeSet<String> = names
        .iter()
        .filter(|n| n.chars().take(NAME_MAX + 1).count() <= NAME_MAX)
        .cloned()
        .collect();
    let mut reverse = BTreeMap::new();
    for name in &names {
        if name.chars().take(NAME_MAX + 1).count() <= NAME_MAX {
            continue;
        }
        let mut found = false;
        for attempt in 0..=names.len() {
            let digest = format!(
                "{:x}",
                Sha256::digest(format!("{attempt}:{name}").as_bytes())
            );
            let candidate = format!(
                "{}_{}",
                name.chars().take(NAME_PREFIX).collect::<String>(),
                &digest[..NAME_DIGEST]
            );
            if occupied.insert(candidate.clone()) {
                reverse.insert(candidate, name.clone());
                found = true;
                break;
            }
        }
        if !found {
            return Err("native Responses alias collision");
        }
    }
    Ok(reverse)
}
fn image(value: &Value) -> Result<Value, &'static str> {
    let image = &value["image_url"];
    let url = image
        .as_str()
        .or_else(|| image["url"].as_str())
        .filter(|s| !s.is_empty())
        .ok_or("native Responses image URL invalid")?;
    let mut result = json!({"type":"input_image","image_url":url});
    if let Some(detail) = image
        .get("detail")
        .and_then(Value::as_str)
        .filter(|s| ["auto", "low", "high", "original"].contains(s))
    {
        result["detail"] = detail.into();
    }
    Ok(result)
}
fn parts(content: &Value, assistant: bool) -> Result<Vec<Value>, &'static str> {
    let kind = if assistant {
        "output_text"
    } else {
        "input_text"
    };
    match content {
        Value::Null => Ok(vec![]),
        Value::String(s) => Ok(if s.is_empty() {
            vec![]
        } else {
            vec![json!({"type":kind,"text":s})]
        }),
        Value::Array(a) => a
            .iter()
            .map(|v| match v["type"].as_str() {
                Some("text") => {
                    let text = v["text"].as_str().ok_or("native Responses text invalid")?;
                    Ok(json!({"type":kind,"text":text}))
                }
                Some("image_url") if !assistant => image(v),
                _ => Err("native Responses content unsupported"),
            })
            .collect(),
        _ => Err("native Responses content invalid"),
    }
}
fn phase_replay(message: &Value) -> Result<Vec<Value>, &'static str> {
    use crate::execution_contract::{
        RESPONSES_MAX_PHASE_MESSAGES as MAX_ITEMS, RESPONSES_MAX_PHASE_TEXT_BYTES as MAX_BYTES,
        RESPONSES_MESSAGE_PHASES,
    };
    let Some(items) = message.get("_responses_items").and_then(Value::as_array) else {
        return Ok(vec![]);
    };
    let mut result = vec![];
    let mut texts = vec![];
    let mut bytes = 0usize;
    let mut any_phase = false;
    for item in items.iter().filter(|i| i["type"] == "message") {
        if item["role"] != "assistant" || !item["agent"].is_null() {
            return Err("native Responses phase replay unsupported");
        }
        if !item["phase"].is_null()
            && !item["phase"]
                .as_str()
                .is_some_and(|p| RESPONSES_MESSAGE_PHASES.contains(&p))
        {
            return Err("native Responses phase invalid");
        }
        let parts = item["content"]
            .as_array()
            .ok_or("native Responses phase invalid")?;
        if parts.len() > MAX_ITEMS || result.len() >= MAX_ITEMS {
            return Ok(vec![]);
        }
        let mut saved = vec![];
        for part in parts {
            let field = match part["type"].as_str() {
                Some("output_text") => "text",
                Some("refusal") => "refusal",
                _ => return Err("native Responses phase invalid"),
            };
            let text = part[field]
                .as_str()
                .ok_or("native Responses phase invalid")?;
            bytes = bytes
                .checked_add(text.len())
                .ok_or("native Responses phase overflow")?;
            if bytes > MAX_BYTES {
                return Ok(vec![]);
            }
            if !text.is_empty() {
                texts.push(text);
            }
            let mut value = json!({"type":part["type"]});
            value[field] = text.into();
            saved.push(value);
        }
        let mut value = json!({"type":"message","role":"assistant","content":saved});
        if let Some(phase) = item.get("phase") {
            value["phase"] = phase.clone();
            any_phase |= !phase.is_null();
        }
        result.push(value);
    }
    if !any_phase {
        return Ok(vec![]);
    }
    let Some(content) = message["content"].as_str() else {
        return Ok(vec![]);
    };
    if !["", "\n"]
        .iter()
        .any(|separator| content.trim() == texts.join(separator).trim())
    {
        return Ok(vec![]);
    }
    Ok(result)
}
struct Input<'a>(&'a Body<'a>);
impl Serialize for Input<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let body = self.0;
        let mut sequence = serializer.serialize_seq(None)?;
        for message in body.messages {
            let role = message["role"]
                .as_str()
                .ok_or_else(|| S::Error::custom("native Responses role invalid"))?;
            if message
                .get("_anthropic_content_blocks")
                .is_some_and(|v| v.as_array().is_some_and(|a| !a.is_empty()))
                || message
                    .get("thinking_signature")
                    .is_some_and(|v| v.as_str().is_some_and(|s| !s.is_empty()))
            {
                return Err(S::Error::custom(
                    "native Responses cross-protocol continuity unsupported",
                ));
            }
            if let Some(items) = message.get("_responses_items").filter(|v| !v.is_null()) {
                if role != "assistant" {
                    return Err(S::Error::custom("native Responses replay role invalid"));
                }
                for item in items
                    .as_array()
                    .ok_or_else(|| S::Error::custom("native Responses replay invalid"))?
                {
                    match item["type"].as_str() {
                        Some("reasoning") if body.replay => sequence.serialize_element(item)?,
                        Some("reasoning")
                            if item.get("encrypted_content").is_none_or(|v| {
                                v.is_null() || v == &Value::String(String::new())
                            }) => {}
                        Some("message") => {}
                        _ => {
                            return Err(S::Error::custom(
                                "native Responses replay profile unsupported",
                            ))
                        }
                    }
                }
            }
            if role == "tool" {
                if message.get("caller").is_some_and(|v| !v.is_null()) {
                    return Err(S::Error::custom(
                        "native Responses delegated/program output unsupported",
                    ));
                }
                let content = &message["content"];
                let images = content
                    .as_array()
                    .is_some_and(|a| a.iter().any(|v| v["type"] == "image_url"));
                let output = if images {
                    parts(content, false).map_err(S::Error::custom)?.into()
                } else if content.is_string() {
                    content.clone()
                } else {
                    if content.is_null() {
                        "\"\"".into()
                    } else {
                        serde_json::to_string(content)
                            .map_err(S::Error::custom)?
                            .into()
                    }
                };
                let id = message["tool_call_id"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| S::Error::custom("native Responses result identity invalid"))?;
                sequence.serialize_element(
                    &json!({"type":"function_call_output","call_id":id,"output":output}),
                )?;
                continue;
            }
            if !["system", "developer", "user", "assistant"].contains(&role) {
                return Err(S::Error::custom("native Responses role unsupported"));
            }
            let captured = phase_replay(message).map_err(S::Error::custom)?;
            let content =
                parts(&message["content"], role == "assistant").map_err(S::Error::custom)?;
            if !captured.is_empty() {
                for item in captured {
                    sequence.serialize_element(&item)?;
                }
            } else if role != "assistant" || !content.is_empty() {
                sequence.serialize_element(&json!({"type":"message","role":if role=="system"{"developer"}else{role},"content":content}))?;
            }
            if let Some(calls) = message.get("tool_calls").filter(|v| !v.is_null()) {
                for call in calls
                    .as_array()
                    .ok_or_else(|| S::Error::custom("native Responses calls invalid"))?
                {
                    if role != "assistant"
                        || call.get("type").is_some_and(|v| v != "function")
                        || call.get("caller").is_some_and(|v| !v.is_null())
                    {
                        return Err(S::Error::custom(
                            "native Responses call profile unsupported",
                        ));
                    }
                    let name = call["function"]["name"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .ok_or_else(|| S::Error::custom("native Responses call name invalid"))?;
                    let id = call["id"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .ok_or_else(|| S::Error::custom("native Responses call id invalid"))?;
                    let arguments = call["function"]["arguments"].as_str().ok_or_else(|| {
                        S::Error::custom("native Responses call arguments invalid")
                    })?;
                    let parsed: Value = serde_json::from_str(arguments)
                        .map_err(|_| S::Error::custom("native Responses call JSON invalid"))?;
                    if !parsed.is_object() {
                        return Err(S::Error::custom("native Responses call object required"));
                    }
                    sequence.serialize_element(&json!({"type":"function_call","call_id":id,"name":alias(name,body.reverse),"arguments":arguments}))?;
                }
            }
        }
        sequence.end()
    }
}
impl Serialize for Body<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let allowed = [
            "model",
            "prompt_cache_key",
            "max_tokens",
            "stream",
            "temperature",
            "top_p",
            "response_format",
            "text",
            "reasoning",
            "reasoning_effort",
            "effort",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stop",
            "stop_sequences",
            "output_config",
        ];
        if self.template.keys().any(|k| !allowed.contains(&k.as_str())) {
            return Err(S::Error::custom(
                "native Responses template profile unsupported",
            ));
        }
        if self
            .template
            .get("stop")
            .is_some_and(|v| !v.is_null() && !v.as_array().is_some_and(Vec::is_empty))
            || self
                .template
                .get("stop_sequences")
                .is_some_and(|v| !v.is_null() && !v.as_array().is_some_and(Vec::is_empty))
        {
            return Err(S::Error::custom("native Responses stop unrepresentable"));
        }
        if self
            .template
            .get("output_config")
            .is_some_and(|v| !v.is_null() && v != &json!({}))
        {
            return Err(S::Error::custom(
                "native Responses output config unrepresentable",
            ));
        }
        let mut map = serializer.serialize_map(None)?;
        map.serialize_entry(
            "model",
            self.template
                .get("model")
                .ok_or_else(|| S::Error::custom("native Responses model required"))?,
        )?;
        if self.profile == Profile::Codex {
            map.serialize_entry("instructions", crate::execution_contract::RESPONSES_CODEX_INSTRUCTIONS)?;
        }
        if let Some(value) = self.template.get("prompt_cache_key") {
            let key = value.as_str().filter(|key| !key.is_empty() && key.len() <= crate::execution_contract::MODEL_CACHE_AFFINITY_KEY_BYTES)
                .filter(|_| self.profile == Profile::Codex)
                .ok_or_else(|| S::Error::custom("native Responses cache binding invalid"))?;
            map.serialize_entry("prompt_cache_key", key)?;
        }
        map.serialize_entry("store", &false)?;
        map.serialize_entry("stream", &true)?;
        let parallel = self
            .template
            .get("parallel_tool_calls")
            .map(|v| {
                v.as_bool()
                    .ok_or_else(|| S::Error::custom("native Responses parallel policy invalid"))
            })
            .transpose()?
            .unwrap_or(true);
        map.serialize_entry("parallel_tool_calls", &parallel)?;
        for key in ["temperature", "top_p"] {
            if self.profile == Profile::Codex && crate::execution_contract::RESPONSES_CODEX_DROP_PARAMS.contains(&key) {
                continue;
            }
            if let Some(value) = self.template.get(key) {
                map.serialize_entry(key, value)?;
            }
        }
        if self
            .template
            .get("max_tokens")
            .is_some_and(|v| v.as_u64().is_none() || v.as_u64() == Some(0))
        {
            return Err(S::Error::custom("native Responses output budget invalid"));
        }
        if let Some(limit) = self
            .output
            .or_else(|| self.template.get("max_tokens").and_then(Value::as_u64))
        {
            if limit == 0 {
                return Err(S::Error::custom("native Responses output budget invalid"));
            }
            if self.profile != Profile::Codex {
                map.serialize_entry("max_output_tokens", &limit)?;
            }
        }
        let mut text = self
            .template
            .get("text")
            .map(|v| {
                v.as_object()
                    .cloned()
                    .ok_or_else(|| S::Error::custom("native Responses text config invalid"))
            })
            .transpose()?
            .unwrap_or_default();
        if let Some(format) = self
            .template
            .get("response_format")
            .filter(|v| !v.is_null())
        {
            let fields = format
                .as_object()
                .ok_or_else(|| S::Error::custom("native Responses format invalid"))?;
            let projected = match format["type"].as_str() {
                Some("text" | "json_object") if fields.len() == 1 => format.clone(),
                Some("json_schema") => {
                    let schema = format["json_schema"]
                        .as_object()
                        .ok_or_else(|| S::Error::custom("native Responses schema invalid"))?;
                    if fields
                        .keys()
                        .any(|k| !["type", "json_schema"].contains(&k.as_str()))
                        || schema.keys().any(|k| {
                            !["name", "schema", "strict", "description"].contains(&k.as_str())
                        })
                        || !schema
                            .get("name")
                            .and_then(Value::as_str)
                            .is_some_and(|s| !s.is_empty())
                        || !schema.get("schema").is_some_and(Value::is_object)
                        || schema.get("strict").is_some_and(|v| !v.is_boolean())
                        || schema.get("description").is_some_and(|v| !v.is_string())
                    {
                        return Err(S::Error::custom("native Responses schema invalid"));
                    }
                    let mut value = schema.clone();
                    value.insert("type".into(), "json_schema".into());
                    value.into()
                }
                _ => return Err(S::Error::custom("native Responses format unsupported")),
            };
            if text.get("format").is_some_and(|v| v != &projected) {
                return Err(S::Error::custom("native Responses format conflict"));
            }
            text.insert("format".into(), projected);
        }
        if !text.is_empty() {
            map.serialize_entry("text", &text)?;
        }
        let mut reasoning = self
            .template
            .get("reasoning")
            .map(|v| {
                v.as_object()
                    .cloned()
                    .ok_or_else(|| S::Error::custom("native Responses reasoning invalid"))
            })
            .transpose()?
            .unwrap_or_default();
        if reasoning
            .keys()
            .any(|k| !["effort", "summary", "context"].contains(&k.as_str()))
        {
            return Err(S::Error::custom(
                "native Responses reasoning profile unsupported",
            ));
        }
        let mut effort = None;
        for value in [
            self.template.get("effort"),
            self.template.get("reasoning_effort"),
            reasoning.get("effort"),
        ]
        .into_iter()
        .flatten()
        .filter(|v| !v.is_null())
        {
            let value = value
                .as_str()
                .filter(|s| !s.is_empty() && s.chars().take(NAME_MAX + 1).count() <= NAME_MAX)
                .ok_or_else(|| S::Error::custom("native Responses effort invalid"))?;
            if effort.is_some_and(|old| old != value) {
                return Err(S::Error::custom("native Responses effort conflict"));
            }
            effort = Some(value);
        }
        let effort = effort.map(str::to_owned);
        if let Some(value) = effort {
            reasoning.insert("effort".into(), value.into());
        }
        if reasoning.get("summary").is_some_and(|v| {
            !v.is_null() && !matches!(v.as_str(), Some("auto" | "concise" | "detailed"))
        }) {
            return Err(S::Error::custom("native Responses summary invalid"));
        }
        if reasoning
            .get("context")
            .is_some_and(|v| !self.replay || v != "all_turns")
        {
            return Err(S::Error::custom("native Responses context unsupported"));
        }
        if self.profile == Profile::Codex {
            reasoning.entry("summary").or_insert_with(|| crate::execution_contract::RESPONSES_CODEX_SUMMARY.into());
        }
        if self.replay {
            reasoning.insert("context".into(), "all_turns".into());
        }
        if self.profile == Profile::Codex {
            map.serialize_entry("include", crate::execution_contract::RESPONSES_CODEX_INCLUDE)?;
        } else if self.replay {
            map.serialize_entry("include", &["reasoning.encrypted_content"])?;
        }
        if !reasoning.is_empty() {
            map.serialize_entry("reasoning", &reasoning)?;
        }
        if let Some(tools) = self.template.get("tools") {
            let mut projected = vec![];
            for tool in tools
                .as_array()
                .ok_or_else(|| S::Error::custom("native Responses tools invalid"))?
            {
                if tool["type"] != "function" {
                    return Err(S::Error::custom("native Responses hosted tool unsupported"));
                }
                let function = tool["function"]
                    .as_object()
                    .ok_or_else(|| S::Error::custom("native Responses function invalid"))?;
                let name = function
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| S::Error::custom("native Responses name invalid"))?;
                let mut value = json!({"type":"function","name":alias(name,self.reverse)});
                if let Some(v) = function
                    .get("description")
                    .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
                {
                    value["description"] = v.clone();
                }
                if let Some(v) = function.get("parameters") {
                    if !v.is_object() {
                        return Err(S::Error::custom("native Responses schema invalid"));
                    }
                    value["parameters"] = v.clone();
                }
                if let Some(v) = function.get("strict").filter(|v| !v.is_null()) {
                    if !v.is_boolean() {
                        return Err(S::Error::custom("native Responses strict invalid"));
                    }
                    value["strict"] = v.clone();
                }
                projected.push(value);
            }
            if !projected.is_empty() {
                map.serialize_entry("tools", &projected)?;
            }
        }
        if let Some(choice) = self.template.get("tool_choice").filter(|v| !v.is_null()) {
            let value = if let Some(name) = choice.as_str() {
                if !["auto", "none", "required", "any"].contains(&name) {
                    return Err(S::Error::custom("native Responses choice unsupported"));
                }
                Value::String(if name == "any" { "required" } else { name }.into())
            } else if choice["type"] == "function" {
                let name = choice["function"]["name"]
                    .as_str()
                    .ok_or_else(|| S::Error::custom("native Responses choice invalid"))?;
                json!({"type":"function","name":alias(name,self.reverse)})
            } else {
                return Err(S::Error::custom("native Responses choice unsupported"));
            };
            map.serialize_entry("tool_choice", &value)?;
        }
        map.serialize_entry("input", &Input(self))?;
        map.end()
    }
}
