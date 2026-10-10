//! Borrowed canonical-history serialization to Anthropic Messages. Native
//! inference preparation owns model policy. Unsupported media/policy carriers
//! refuse explicitly; no whole-history Value clone or provider authorization.
use base64::Engine;
use serde_core::{
    ser::{Error, SerializeMap, SerializeSeq},
    Serialize, Serializer,
};
use serde_json::{json, Map, Value};
pub struct Body<'a> {
    pub template: &'a Map<String, Value>,
    pub messages: &'a [Value],
    pub output: Option<u64>,
}
fn present(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn cc(content: &Value) -> Option<&Value> {
    content
        .as_array()?
        .iter()
        .find_map(|block| block.get("cache_control").filter(|v| present(v)))
}
fn image(value: &Value) -> Result<Value, &'static str> {
    let url = value
        .get("url")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or("native Anthropic image URL invalid")?;
    if let Some(raw) = url.strip_prefix("data:") {
        let (header, data) = raw
            .split_once(',')
            .ok_or("native Anthropic data URL invalid")?;
        let declared = header
            .split(';')
            .next()
            .filter(|s| !s.is_empty())
            .unwrap_or("image/png");
        // Match the retained boundary's bounded best-effort MIME reconciliation.
        let sample = data.get(..data.len().min(1364));
        let decoded = sample.and_then(|sample| {
            base64::engine::general_purpose::STANDARD
                .decode(sample)
                .ok()
        });
        let media = decoded
            .as_deref()
            .and_then(|head| {
                crate::execution_contract::IMAGE_MIME_MAGICS
                    .iter()
                    .find_map(|(magic, mime)| head.starts_with(magic).then_some(*mime))
                    .or_else(|| {
                        (head.len() >= 12 && head.starts_with(b"RIFF") && &head[8..12] == b"WEBP")
                            .then_some("image/webp")
                    })
            })
            .unwrap_or(declared);
        Ok(json!({"type":"image","source":{"type":"base64","media_type":media,"data":data}}))
    } else {
        Ok(json!({"type":"image","source":{"type":"url","url":url}}))
    }
}
fn content(value: &Value) -> Result<Vec<Value>, &'static str> {
    match value {
        Value::Null => Ok(vec![]),
        Value::String(s) => Ok(if s.trim().is_empty() {
            vec![]
        } else {
            vec![json!({"type":"text","text":s})]
        }),
        Value::Array(values) => values
            .iter()
            .filter_map(|v| {
                if v["type"] == "image_url" {
                    return Some(image(&v["image_url"]).map(|mut block| {
                        if let Some(marker) = v.get("cache_control").filter(|v| present(v)) {
                            block["cache_control"] = marker.clone();
                        }
                        block
                    }));
                }
                if v["type"] != "text" {
                    return Some(Err("native Anthropic content adapter unsupported"));
                }
                let Some(text) = v["text"].as_str() else {
                    return Some(Err("native Anthropic text invalid"));
                };
                if text.trim().is_empty() {
                    return None;
                }
                let mut block = json!({"type":"text","text":text});
                if let Some(cc) = v.get("cache_control").filter(|v| present(v)) {
                    block["cache_control"] = cc.clone();
                }
                Some(Ok(block))
            })
            .collect(),
        _ => Err("native Anthropic content invalid"),
    }
}
fn hoist(blocks: &mut [Value], marker: Option<&Value>) {
    if let Some(marker) = marker {
        for block in blocks.iter_mut() {
            block.as_object_mut().unwrap().remove("cache_control");
        }
        if let Some(last) = blocks.last_mut() {
            last["cache_control"] = marker.clone();
        }
    }
}
fn assistant(message: &Value) -> Result<Vec<Value>, &'static str> {
    let mut blocks = if let Some(replay) = message
        .get("_anthropic_content_blocks")
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
    {
        if replay.iter().any(|v| !v.is_object()) {
            return Err("native Anthropic replay invalid");
        }
        replay.clone()
    } else {
        let mut blocks = vec![];
        if let (Some(thinking), Some(signature)) = (
            message["reasoning_content"]
                .as_str()
                .filter(|s| !s.is_empty()),
            message["thinking_signature"]
                .as_str()
                .filter(|s| !s.is_empty()),
        ) {
            blocks.push(json!({"type":"thinking","thinking":thinking,"signature":signature}));
        }
        blocks.extend(content(&message["content"])?);
        if let Some(calls) = message.get("tool_calls").filter(|v| !v.is_null()) {
            for call in calls
                .as_array()
                .ok_or("native Anthropic tool calls invalid")?
            {
                let function = &call["function"];
                let arguments = match function.get("arguments") {
                    None | Some(Value::Null) => json!({}),
                    Some(Value::String(s)) if s.trim().is_empty() => json!({}),
                    Some(Value::String(s)) => serde_json::from_str::<Value>(s)
                        .map_err(|_| "native Anthropic tool arguments invalid")?,
                    Some(value) if value.is_object() => value.clone(),
                    _ => return Err("native Anthropic tool arguments invalid"),
                };
                if !arguments.is_object() {
                    return Err("native Anthropic tool arguments must be object");
                }
                let id = call["id"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or("native Anthropic tool id invalid")?;
                let name = function["name"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or("native Anthropic tool name invalid")?;
                blocks.push(json!({"type":"tool_use","id":id,"name":name,"input":arguments}));
            }
        }
        blocks
    };
    hoist(&mut blocks, cc(&message["content"]));
    if blocks.is_empty() {
        blocks.push(json!({"type":"text","text":"[empty response]"}));
    }
    Ok(blocks)
}
fn result(message: &Value) -> Result<Value, &'static str> {
    let mut data = if message["content"].is_string() {
        message["content"].clone()
    } else {
        let mut blocks = content(&message["content"])?;
        if cc(&message["content"]).is_some() {
            for block in &mut blocks {
                block.as_object_mut().unwrap().remove("cache_control");
            }
        }
        if blocks.is_empty() {
            Value::String(String::new())
        } else {
            blocks.into()
        }
    };
    let id = message["tool_call_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("native Anthropic result id invalid")?;
    let mut out = json!({"type":"tool_result","tool_use_id":id,"content":data.take()});
    if let Some(marker) = cc(&message["content"]) {
        out["cache_control"] = marker.clone();
    }
    Ok(out)
}
struct Messages<'a>(&'a [Value]);
impl Serialize for Messages<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut sequence = serializer.serialize_seq(None)?;
        let mut i = 0;
        while i < self.0.len() {
            let message = &self.0[i];
            let role = message["role"]
                .as_str()
                .ok_or_else(|| S::Error::custom("native Anthropic role invalid"))?;
            match role {
                "system" | "developer" => {
                    i += 1;
                }
                "tool" => {
                    let mut results = vec![];
                    while i < self.0.len() {
                        match self.0[i]["role"].as_str() {
                            Some("tool") => {
                                results.push(result(&self.0[i]).map_err(S::Error::custom)?)
                            }
                            Some("system" | "developer") => {}
                            _ => break,
                        }
                        i += 1;
                    }
                    sequence.serialize_element(&json!({"role":"user","content":results}))?;
                }
                "assistant" => {
                    sequence.serialize_element(&json!({"role":"assistant","content":assistant(message).map_err(S::Error::custom)?}))?;
                    i += 1;
                }
                "user" => {
                    let mut blocks = content(&message["content"]).map_err(S::Error::custom)?;
                    if blocks.is_empty() {
                        blocks.push(json!({"type":"text","text":"[empty message]"}));
                    }
                    sequence.serialize_element(&json!({"role":"user","content":blocks}))?;
                    i += 1;
                }
                _ => return Err(S::Error::custom("native Anthropic role unsupported")),
            }
        }
        sequence.end()
    }
}
impl Serialize for Body<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let allowed = [
            "model",
            "max_tokens",
            "stream",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "thinking",
            "tool_choice",
            "output_config",
            "response_format",
            "effort",
            "reasoning_effort",
            "tools",
            "parallel_tool_calls",
            "stop",
        ];
        if self.template.keys().any(|k| !allowed.contains(&k.as_str())) {
            return Err(S::Error::custom(
                "native Anthropic template adapter unsupported",
            ));
        }
        let mut map = serializer.serialize_map(None)?;
        map.serialize_entry(
            "model",
            self.template
                .get("model")
                .ok_or_else(|| S::Error::custom("native Anthropic model required"))?,
        )?;
        if self
            .template
            .get("max_tokens")
            .is_some_and(|value| value.as_u64().is_none() || value.as_u64() == Some(0))
        {
            return Err(S::Error::custom("native Anthropic output budget invalid"));
        }
        let output = self
            .output
            .or_else(|| self.template.get("max_tokens").and_then(Value::as_u64))
            .unwrap_or(4096);
        if output == 0 {
            return Err(S::Error::custom("native Anthropic output budget invalid"));
        }
        map.serialize_entry("max_tokens", &output)?;
        map.serialize_entry("stream", &true)?;
        for key in ["temperature", "top_p", "top_k", "thinking"] {
            if let Some(value) = self.template.get(key) {
                map.serialize_entry(key, value)?;
            }
        }
        if let Some(stop) = self
            .template
            .get("stop")
            .or_else(|| self.template.get("stop_sequences"))
        {
            let stops = if let Some(text) = stop.as_str() {
                vec![Value::String(text.into())]
            } else {
                stop.as_array()
                    .ok_or_else(|| S::Error::custom("native Anthropic stops invalid"))?
                    .clone()
            };
            if stops.iter().any(|s| !s.is_string()) {
                return Err(S::Error::custom("native Anthropic stops invalid"));
            }
            map.serialize_entry("stop_sequences", &stops)?;
        }
        let mut choice = self.template.get("tool_choice").cloned();
        if let Some(Value::String(value)) = &choice {
            choice = Some(json!({"type":if value=="required"{"any"}else{value}}));
        }
        if choice.as_ref().is_some_and(|v| v["type"] == "function") {
            let name = choice.as_ref().unwrap()["function"]["name"]
                .as_str()
                .ok_or_else(|| S::Error::custom("native Anthropic choice invalid"))?;
            choice = Some(json!({"type":"tool","name":name}));
        }
        if let Some(parallel) = self.template.get("parallel_tool_calls") {
            let parallel = parallel
                .as_bool()
                .ok_or_else(|| S::Error::custom("native Anthropic parallel policy invalid"))?;
            if choice
                .as_ref()
                .and_then(|v| v.get("disable_parallel_tool_use"))
                .is_some_and(|v| v.as_bool() == Some(parallel))
            {
                return Err(S::Error::custom(
                    "native Anthropic parallel policy conflict",
                ));
            }
            let value = choice.get_or_insert_with(|| json!({"type":"auto"}));
            if !value.is_object() {
                return Err(S::Error::custom("native Anthropic choice invalid"));
            }
            value["disable_parallel_tool_use"] = (!parallel).into();
        }
        if let Some(value) = choice {
            if !matches!(
                value["type"].as_str(),
                Some("auto" | "any" | "none" | "tool")
            ) || value
                .get("disable_parallel_tool_use")
                .is_some_and(|v| !v.is_boolean())
                || (value["type"] == "tool"
                    && !value["name"].as_str().is_some_and(|s| !s.is_empty()))
            {
                return Err(S::Error::custom("native Anthropic choice invalid"));
            }
            map.serialize_entry("tool_choice", &value)?;
        }
        let mut config = self
            .template
            .get("output_config")
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_object()
                    .cloned()
                    .ok_or_else(|| S::Error::custom("native Anthropic output config invalid"))
            })
            .transpose()?
            .unwrap_or_default();
        if let Some(format) = self.template.get("response_format") {
            let outer = format.as_object().ok_or_else(|| {
                S::Error::custom("native Anthropic response format unrepresentable")
            })?;
            let schema = outer
                .get("json_schema")
                .and_then(Value::as_object)
                .ok_or_else(|| {
                    S::Error::custom("native Anthropic response format unrepresentable")
                })?;
            if format["type"] != "json_schema"
                || outer
                    .keys()
                    .any(|key| !["type", "json_schema"].contains(&key.as_str()))
                || schema
                    .keys()
                    .any(|key| !["name", "schema", "strict"].contains(&key.as_str()))
                || !schema.get("schema").is_some_and(Value::is_object)
                || schema
                    .get("strict")
                    .is_some_and(|value| value != &Value::Bool(true))
            {
                return Err(S::Error::custom(
                    "native Anthropic response format unrepresentable",
                ));
            }
            let projected = json!({"type":"json_schema","schema":schema["schema"]});
            if config
                .get("format")
                .is_some_and(|value| value != &projected)
            {
                return Err(S::Error::custom(
                    "native Anthropic response format conflict",
                ));
            }
            config.insert("format".into(), projected);
        }
        if let Some(effort) = self
            .template
            .get("effort")
            .or_else(|| self.template.get("reasoning_effort"))
        {
            config.insert("effort".into(), effort.clone());
        }
        if !config.is_empty() {
            map.serialize_entry("output_config", &config)?;
        }
        if let Some(raw) = self.template.get("tools") {
            let mut tools = vec![];
            for tool in raw
                .as_array()
                .ok_or_else(|| S::Error::custom("native Anthropic tools invalid"))?
            {
                if tool["type"] != "function" {
                    return Err(S::Error::custom("native Anthropic tool type unsupported"));
                }
                let function = tool["function"]
                    .as_object()
                    .ok_or_else(|| S::Error::custom("native Anthropic function invalid"))?;
                let mut projected = json!({"name":function.get("name").ok_or_else(||S::Error::custom("native Anthropic name required"))?,"description":function.get("description").unwrap_or(&Value::String(String::new())),"input_schema":function.get("parameters").cloned().unwrap_or_else(||json!({"type":"object","properties":{}}))});
                for key in ["strict", "cache_control"] {
                    if let Some(value) = function.get(key) {
                        projected[key] = value.clone();
                    }
                }
                tools.push(projected);
            }
            if !tools.is_empty() {
                map.serialize_entry("tools", &tools)?;
            }
        }
        let mut system = vec![];
        for message in self.messages {
            if matches!(message["role"].as_str(), Some("system" | "developer")) {
                system.extend(content(&message["content"]).map_err(S::Error::custom)?);
            }
        }
        if system.len() == 1 && system[0].get("cache_control").is_none() {
            map.serialize_entry("system", &system[0]["text"])?;
        } else if !system.is_empty() {
            map.serialize_entry("system", &system)?;
        }
        map.serialize_entry("messages", &Messages(self.messages))?;
        map.end()
    }
}
