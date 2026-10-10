//! Bounded Responses event interpretation. Caller owns framing/HTTP/routing.
//! Position-bound tools, proven terminal suffixes and opaque replay stay here;
//! no provider retry, tool execution or extension authorization is granted.
use crate::execution_contract::{
    RESPONSES_MAX_ITEM_ID_CHARS, RESPONSES_MAX_OUTPUT_INDEX, RESPONSES_MAX_PHASE_MESSAGES,
    RESPONSES_MAX_PHASE_TEXT_BYTES, RESPONSES_MAX_TOOL_NAME_CHARS, RESPONSES_MESSAGE_PHASES,
    RESPONSES_REPLAY_ITEM_TYPES,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Copy)]
pub struct Budget {
    pub items: usize,
    pub retained_bytes: usize,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Protocol,
    Capacity,
    Closed,
    UnsupportedItem,
}
#[derive(Debug, PartialEq)]
pub enum Projection {
    Chunk(Value),
    Done,
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Position {
    Index(u64),
    Id(String),
}
struct Slot {
    identity: Value,
    arguments: String,
}
pub struct Translator {
    budget: Budget,
    charged: usize,
    slots: Vec<Slot>,
    positions: BTreeMap<Position, usize>,
    ids: BTreeMap<String, usize>,
    ambiguous: BTreeSet<String>,
    indices: BTreeMap<u64, usize>,
    hidden: BTreeSet<u64>,
    reverse: BTreeMap<String, String>,
    replay: Vec<Value>,
    replay_positions: BTreeMap<Position, usize>,
    text_chars: usize,
    text_digest: Sha256,
    reasoning_open: bool,
    closed: bool,
    response_id: String,
    provider_error: Option<Value>,
    unknown_events: u64,
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn string(value: Option<&Value>) -> Result<&str, Error> {
    match value {
        None | Some(Value::Null) => Ok(""),
        Some(Value::String(text)) => Ok(text),
        _ => Err(Error::Protocol),
    }
}
fn output_index(event: &Value) -> Result<Option<u64>, Error> {
    match event.get("output_index") {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_u64()
            .filter(|n| *n <= RESPONSES_MAX_OUTPUT_INDEX as u64)
            .map(Some)
            .ok_or(Error::Protocol),
    }
}
fn item_id(value: Option<&Value>) -> Result<&str, Error> {
    let id = string(value)?;
    if id.chars().take(RESPONSES_MAX_ITEM_ID_CHARS + 1).count() > RESPONSES_MAX_ITEM_ID_CHARS {
        Err(Error::Protocol)
    } else {
        Ok(id)
    }
}
fn agent(value: Option<&Value>) -> Result<Option<&Value>, Error> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(value)
            if value.is_object() && value["agent_name"].as_str().is_some_and(|s| !s.is_empty()) =>
        {
            Ok(Some(value))
        }
        _ => Err(Error::Protocol),
    }
}
fn visible(item: &Value) -> bool {
    item["agent"].is_null()
        || (item["agent"]["agent_name"] == "/root" && item["phase"] == "final_answer")
}
fn count(value: &Value) -> Result<u64, Error> {
    match value {
        Value::Bool(b) => Ok(u64::from(*b)),
        Value::Number(n) => {
            if let Some(n) = n.as_u64() {
                Ok(n)
            } else if let Some(n) = n.as_i64() {
                Ok(n.max(0) as u64)
            } else {
                let n = n.as_f64().ok_or(Error::Capacity)?;
                if n >= u64::MAX as f64 {
                    Err(Error::Capacity)
                } else {
                    Ok(n.max(0.0) as u64)
                }
            }
        }
        Value::String(s) => {
            let s = s.trim().trim_start_matches('+');
            if s.starts_with('-') {
                return Ok(0);
            }
            match s.parse() {
                Ok(n) => Ok(n),
                Err(_) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => {
                    Err(Error::Capacity)
                }
                Err(_) => Ok(0),
            }
        }
        _ => Ok(0),
    }
}
pub fn convert_usage(value: &Value) -> Result<Value, Error> {
    let Some(raw) = value.as_object().filter(|v| !v.is_empty()) else {
        return Ok(json!({}));
    };
    let input = count(raw.get("input_tokens").unwrap_or(&Value::Null))?;
    let output = count(raw.get("output_tokens").unwrap_or(&Value::Null))?;
    let total = if let Some(v) = raw.get("total_tokens") {
        count(v)?
    } else {
        input.checked_add(output).ok_or(Error::Capacity)?
    };
    let mut result = json!({"prompt_tokens":input,"completion_tokens":output,"total_tokens":total});
    if let Some(details) = raw.get("input_tokens_details").and_then(Value::as_object) {
        let mut saved = Map::new();
        for name in ["cached_tokens", "cache_write_tokens"] {
            if let Some(value) = details.get(name) {
                let n = count(value)?;
                saved.insert(name.into(), n.into());
                if name == "cache_write_tokens" {
                    result[name] = n.into();
                }
            }
        }
        if !saved.is_empty() {
            result["prompt_tokens_details"] = saved.into();
        }
    }
    if let Some(value) = raw
        .get("output_tokens_details")
        .and_then(|v| v.get("reasoning_tokens"))
    {
        result["completion_tokens_details"] = json!({"reasoning_tokens":count(value)?});
    }
    Ok(result)
}
struct Size {
    bytes: usize,
    limit: usize,
}
impl std::io::Write for Size {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(b.len())
            .filter(|n| *n <= self.limit)
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
        Ok(b.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn size(value: &Value, limit: usize) -> Result<usize, Error> {
    let mut writer = Size { bytes: 0, limit };
    serde_json::to_writer(&mut writer, value).map_err(|_| Error::Capacity)?;
    Ok(writer.bytes)
}
impl Translator {
    pub fn new(budget: Budget, reverse: BTreeMap<String, String>) -> Result<Self, Error> {
        if budget.items == 0
            || budget.items > RESPONSES_MAX_OUTPUT_INDEX + 1
            || budget.retained_bytes == 0
        {
            return Err(Error::Configuration);
        }
        let charged = reverse.iter().try_fold(0usize, |n, (a, b)| {
            n.checked_add(a.len() + b.len() + 64).ok_or(Error::Capacity)
        })?;
        if charged > budget.retained_bytes {
            return Err(Error::Capacity);
        }
        Ok(Self {
            budget,
            charged,
            slots: vec![],
            positions: BTreeMap::new(),
            ids: BTreeMap::new(),
            ambiguous: BTreeSet::new(),
            indices: BTreeMap::new(),
            hidden: BTreeSet::new(),
            reverse,
            replay: vec![],
            replay_positions: BTreeMap::new(),
            text_chars: 0,
            text_digest: Sha256::new(),
            reasoning_open: false,
            closed: false,
            response_id: String::new(),
            provider_error: None,
            unknown_events: 0,
        })
    }
    fn reserve(&mut self, n: usize) -> Result<(), Error> {
        self.charged = self
            .charged
            .checked_add(n)
            .filter(|n| *n <= self.budget.retained_bytes)
            .ok_or(Error::Capacity)?;
        Ok(())
    }
    pub fn charged_bytes(&self) -> usize {
        self.charged
    }
    pub fn response_id(&self) -> &str {
        &self.response_id
    }
    pub fn replay_items(&self) -> Vec<Value> {
        self.replay.clone()
    }
    pub fn provider_error(&self) -> Option<&Value> {
        self.provider_error.as_ref()
    }
    pub fn unknown_events(&self) -> u64 {
        self.unknown_events
    }
    fn chunk(
        &mut self,
        delta: Value,
        finish: Option<&str>,
        usage: Option<Value>,
    ) -> Result<Projection, Error> {
        if let Some(text) = delta.get("content").and_then(Value::as_str) {
            self.text_chars = self
                .text_chars
                .checked_add(text.chars().count())
                .ok_or(Error::Capacity)?;
            self.text_digest.update(text.as_bytes());
        }
        let mut out = json!({"choices":[{"delta":delta,"finish_reason":finish}]});
        if let Some(value) = usage.filter(|v| v.as_object().is_some_and(|m| !m.is_empty())) {
            out["usage"] = value;
        }
        Ok(Projection::Chunk(out))
    }
    fn observe_item(item: &Value) -> Result<(), Error> {
        let kind = item["type"].as_str().ok_or(Error::Protocol)?;
        if kind == "message"
            || kind == "function_call"
            || RESPONSES_REPLAY_ITEM_TYPES.contains(&kind)
        {
            Ok(())
        } else {
            Err(Error::UnsupportedItem)
        }
    }
    fn position(event: &Value, item: &Value) -> Result<Option<Position>, Error> {
        if let Some(n) = output_index(event)? {
            Ok(Some(Position::Index(n)))
        } else {
            let id = item_id(item.get("id"))?;
            Ok((!id.is_empty()).then(|| Position::Id(id.into())))
        }
    }
    fn route(&self, event: &Value) -> Result<Option<usize>, Error> {
        let id = item_id(event.get("item_id"))?;
        let index = output_index(event)?;
        let mut resolved = None;
        if !id.is_empty() {
            if self.ambiguous.contains(id) {
                if index.is_none() {
                    return Err(Error::Protocol);
                }
            } else {
                resolved = Some(*self.ids.get(id).ok_or(Error::Protocol)?);
            }
        }
        if let Some(index) = index {
            let slot = *self.indices.get(&index).ok_or(Error::Protocol)?;
            if resolved.is_some_and(|prior| prior != slot) {
                return Err(Error::Protocol);
            }
            resolved = Some(slot);
        }
        Ok(resolved.or_else(|| self.slots.len().checked_sub(1)))
    }
    fn complete(&mut self, slot: usize, value: &Value) -> Result<Vec<Projection>, Error> {
        let complete = value.as_str().ok_or(Error::Protocol)?;
        let prior = &self.slots[slot].arguments;
        let suffix = complete
            .strip_prefix(prior)
            .ok_or(Error::Protocol)?
            .to_owned();
        if suffix.is_empty() {
            return Ok(vec![]);
        }
        self.reserve(suffix.len() * 2)?;
        self.slots[slot].arguments.push_str(&suffix);
        Ok(vec![self.chunk(
            json!({"tool_calls":[{"index":slot,"function":{"arguments":suffix}}]}),
            None,
            None,
        )?])
    }
    fn reconcile_call(
        &mut self,
        item: &Value,
        index: Option<u64>,
    ) -> Result<Vec<Projection>, Error> {
        let mut route = json!({"item_id":item.get("id")});
        if index.is_some_and(|i| self.indices.contains_key(&i)) {
            route["output_index"] = index.into();
        }
        let slot = self.route(&route)?.ok_or(Error::Protocol)?;
        let identity = &self.slots[slot].identity;
        for field in ["call_id", "name"] {
            let value = string(item.get(field))?;
            if !value.is_empty() && identity[field] != value {
                return Err(Error::Protocol);
            }
        }
        if item.get("caller").is_some()
            && (identity["caller_present"] != true || identity["caller"] != item["caller"])
        {
            return Err(Error::Protocol);
        }
        if let Some(value) = item.get("arguments") {
            self.complete(slot, value)
        } else {
            Ok(vec![])
        }
    }
    fn capture(&mut self, item: &Value, position: Option<Position>) -> Result<(), Error> {
        if !RESPONSES_REPLAY_ITEM_TYPES.contains(&item["type"].as_str().ok_or(Error::Protocol)?) {
            return Ok(());
        }
        self.reserve(size(item, self.budget.retained_bytes)?)?;
        if let Some(slot) = position
            .as_ref()
            .and_then(|p| self.replay_positions.get(p))
            .copied()
        {
            self.replay[slot] = item.clone();
            return Ok(());
        }
        if self.replay.len() >= self.budget.items {
            return Err(Error::Capacity);
        }
        if let Some(position) = position {
            self.replay_positions.insert(position, self.replay.len());
        }
        self.replay.push(item.clone());
        Ok(())
    }
    fn terminal_text(&mut self, items: &[Value]) -> Result<Vec<Projection>, Error> {
        let mut parts = vec![];
        let mut complete = true;
        for item in items
            .iter()
            .filter(|v| v["type"] == "message" && visible(v))
        {
            let content: &[Value] = if item["content"].is_null() {
                &[]
            } else {
                item["content"].as_array().ok_or(Error::Protocol)?
            };
            let mut has = false;
            for part in content {
                if !part.is_object() {
                    return Err(Error::Protocol);
                }
                let kind = part["type"].as_str().ok_or(Error::Protocol)?;
                let field = match kind {
                    "output_text" => "text",
                    "refusal" => "refusal",
                    _ => return Err(Error::UnsupportedItem),
                };
                if part[field].is_null() {
                    complete = false;
                    continue;
                }
                let text = part[field].as_str().ok_or(Error::Protocol)?;
                has = true;
                parts.push(text);
            }
            complete &= has;
        }
        if !complete || parts.is_empty() {
            return Ok(vec![]);
        }
        let mut left = self.text_chars;
        let mut digest = Sha256::new();
        let mut suffix = String::new();
        for text in parts {
            let split = text
                .char_indices()
                .nth(left)
                .map(|(i, _)| i)
                .unwrap_or(text.len());
            let prefix = &text[..split];
            let chars = prefix.chars().count();
            left = left.saturating_sub(chars);
            digest.update(prefix.as_bytes());
            suffix.push_str(&text[split..]);
        }
        if left != 0 || digest.finalize() != self.text_digest.clone().finalize() {
            return Err(Error::Protocol);
        }
        if suffix.is_empty() {
            return Ok(vec![]);
        }
        self.reserve(suffix.len())?;
        Ok(vec![self.chunk(json!({"content":suffix}), None, None)?])
    }
    fn capture_phases(items: &[Value]) -> Vec<Value> {
        let mut result = vec![];
        let mut bytes = 0usize;
        let mut phase = false;
        for item in items.iter().filter(|v| v["type"] == "message") {
            if !item["agent"].is_null() || item["role"] != "assistant" {
                return vec![];
            }
            if !item["phase"].is_null()
                && !item["phase"]
                    .as_str()
                    .is_some_and(|p| RESPONSES_MESSAGE_PHASES.contains(&p))
            {
                return vec![];
            }
            let Some(parts) = item["content"]
                .as_array()
                .filter(|a| a.len() <= RESPONSES_MAX_PHASE_MESSAGES)
            else {
                return vec![];
            };
            let mut saved = vec![];
            for part in parts {
                let field = match part["type"].as_str() {
                    Some("output_text") => "text",
                    Some("refusal") => "refusal",
                    _ => return vec![],
                };
                let Some(text) = part[field].as_str() else {
                    return vec![];
                };
                bytes = bytes.saturating_add(text.len());
                if bytes > RESPONSES_MAX_PHASE_TEXT_BYTES {
                    return vec![];
                }
                saved.push(json!({"type":part["type"],field:text}));
            }
            let mut message = json!({"type":"message","role":"assistant","content":saved});
            if let Some(value) = item.get("phase") {
                message["phase"] = value.clone();
            }
            phase |= item["phase"]
                .as_str()
                .is_some_and(|p| RESPONSES_MESSAGE_PHASES.contains(&p));
            result.push(message);
            if result.len() > RESPONSES_MAX_PHASE_MESSAGES {
                return vec![];
            }
        }
        if phase {
            result
        } else {
            vec![]
        }
    }
    pub fn translate(&mut self, event: &Value) -> Result<Vec<Projection>, Error> {
        if self.closed {
            return Err(Error::Closed);
        }
        let result = size(event, self.budget.retained_bytes).and_then(|_| self.event(event));
        if result.is_err()
            || result
                .as_ref()
                .is_ok_and(|r| r.iter().any(|v| matches!(v, Projection::Done)))
        {
            self.closed = true;
        }
        result
    }
    fn event(&mut self, event: &Value) -> Result<Vec<Projection>, Error> {
        let object = event.as_object().ok_or(Error::Protocol)?;
        let kind = object
            .get("type")
            .map_or(Ok(""), |v| v.as_str().ok_or(Error::Protocol))?;
        match kind {
            "response.output_text.delta" => {
                let index = output_index(event)?;
                let owner = agent(event.get("agent"))?;
                let text = string(event.get("delta"))?;
                if index.is_some_and(|i| self.hidden.contains(&i))
                    || owner.is_some_and(|a| a["agent_name"] != "/root")
                {
                    return Ok(vec![]);
                }
                self.reserve(text.len())?;
                Ok(vec![self.chunk(json!({"content":text}), None, None)?])
            }
            "response.refusal.delta" => {
                let text = string(event.get("delta"))?;
                self.reserve(text.len())?;
                Ok(vec![self.chunk(json!({"content":text}), None, None)?])
            }
            "response.reasoning_summary_text.delta" | "response.reasoning_text.delta" => {
                let text = string(event.get("delta"))?;
                self.reserve(text.len())?;
                self.reasoning_open = true;
                Ok(vec![self.chunk(
                    json!({"reasoning_content":text}),
                    None,
                    None,
                )?])
            }
            "response.reasoning_summary_part.added" => {
                if self.reasoning_open {
                    self.reasoning_open = false;
                    self.reserve(2)?;
                    Ok(vec![self.chunk(
                        json!({"reasoning_content":"\n\n"}),
                        None,
                        None,
                    )?])
                } else {
                    Ok(vec![])
                }
            }
            "response.output_item.added" => {
                let item = event
                    .get("item")
                    .filter(|v| v.is_object())
                    .ok_or(Error::Protocol)?;
                Self::observe_item(item)?;
                let index = output_index(event)?;
                agent(item.get("agent"))?;
                let mut out = vec![];
                if item["type"] == "message" && !visible(item) {
                    if let Some(i) = index {
                        if !self.hidden.contains(&i) {
                            if self.hidden.len() >= self.budget.items {
                                return Err(Error::Capacity);
                            }
                            self.reserve(std::mem::size_of::<u64>() + 64)?;
                            self.hidden.insert(i);
                        }
                    }
                }
                if self.reasoning_open {
                    self.reasoning_open = false;
                    self.reserve(2)?;
                    out.push(self.chunk(json!({"reasoning_content":"\n\n"}), None, None)?);
                }
                if item["type"] != "function_call" {
                    return Ok(out);
                }
                let id = item_id(item.get("id"))?;
                let call_id = string(item.get("call_id"))?;
                let name = string(item.get("name"))?;
                let initial = string(item.get("arguments"))?;
                if call_id
                    .chars()
                    .take(RESPONSES_MAX_ITEM_ID_CHARS + 1)
                    .count()
                    > RESPONSES_MAX_ITEM_ID_CHARS
                    || name.chars().take(RESPONSES_MAX_TOOL_NAME_CHARS + 1).count()
                        > RESPONSES_MAX_TOOL_NAME_CHARS
                {
                    return Err(Error::Protocol);
                }
                let caller_present = item.get("caller").is_some_and(|v| !v.is_null());
                let owner = agent(
                    item.get("agent")
                        .filter(|v| !v.is_null())
                        .or_else(|| event.get("agent")),
                )?;
                let identity = json!({"call_id":call_id,"name":name,"caller_present":caller_present,"caller":item.get("caller"),"agent":owner,"initial_arguments":initial});
                let position = Self::position(event, item)?;
                if let Some(prior) = position.as_ref().and_then(|p| self.positions.get(p)) {
                    if self.slots[*prior].identity != identity {
                        return Err(Error::Protocol);
                    }
                    return Ok(out);
                }
                if self.slots.len() >= self.budget.items {
                    return Err(Error::Capacity);
                }
                self.reserve(
                    size(&identity, self.budget.retained_bytes)?
                        .checked_mul(2)
                        .and_then(|n| id.len().checked_mul(2).and_then(|ids| n.checked_add(ids)))
                        .and_then(|n| n.checked_add(512))
                        .ok_or(Error::Capacity)?,
                )?;
                let slot = self.slots.len();
                if let Some(position) = position {
                    self.positions.insert(position, slot);
                }
                if !id.is_empty() {
                    if self.ids.get(id).is_some_and(|old| *old != slot) {
                        self.ids.remove(id);
                        self.ambiguous.insert(id.into());
                    } else if !self.ambiguous.contains(id) {
                        self.ids.insert(id.into(), slot);
                    }
                }
                if let Some(index) = index {
                    if self.indices.insert(index, slot).is_some() {
                        return Err(Error::Protocol);
                    }
                }
                self.slots.push(Slot {
                    identity,
                    arguments: initial.into(),
                });
                let mut call = json!({"index":slot,"id":call_id,"type":"function","function":{"name":self.reverse.get(name).map(String::as_str).unwrap_or(name),"arguments":initial}});
                if caller_present {
                    call["caller"] = item["caller"].clone();
                }
                if let Some(owner) = owner {
                    if call.get("caller").is_none() {
                        call["caller"] = json!({"type":"multi_agent"});
                    }
                    if call["caller"].is_object() {
                        call["caller"]["agent_name"] = owner["agent_name"].clone();
                    }
                }
                out.push(self.chunk(json!({"tool_calls":[call]}), None, None)?);
                Ok(out)
            }
            "response.function_call_arguments.delta" => {
                let text = string(event.get("delta"))?;
                let slot = self.route(event)?.ok_or(Error::Protocol)?;
                self.reserve(text.len() * 2)?;
                self.slots[slot].arguments.push_str(text);
                Ok(vec![self.chunk(
                    json!({"tool_calls":[{"index":slot,"function":{"arguments":text}}]}),
                    None,
                    None,
                )?])
            }
            "response.function_call_arguments.done" => {
                let slot = self.route(event)?.ok_or(Error::Protocol)?;
                self.complete(slot, &event["arguments"])
            }
            "response.output_item.done" => {
                let item = event
                    .get("item")
                    .filter(|v| v.is_object())
                    .ok_or(Error::Protocol)?;
                Self::observe_item(item)?;
                let out = if item["type"] == "function_call" {
                    self.reconcile_call(item, output_index(event)?)?
                } else {
                    vec![]
                };
                self.capture(item, Self::position(event, item)?)?;
                Ok(out)
            }
            "response.completed" | "response.incomplete" => {
                let response = event
                    .get("response")
                    .filter(|v| v.is_object())
                    .ok_or(Error::Protocol)?;
                let empty = vec![];
                let items = if response["output"].is_null() {
                    &empty
                } else {
                    response["output"].as_array().ok_or(Error::Protocol)?
                };
                if items.len() > self.budget.items || items.iter().any(|v| !v.is_object()) {
                    return Err(Error::Protocol);
                }
                for item in items {
                    Self::observe_item(item)?;
                }
                self.reserve(size(response, self.budget.retained_bytes)?)?;
                self.response_id = string(response.get("id"))?.into();
                self.replay.clear();
                self.replay_positions.clear();
                let mut out = vec![];
                for (index, item) in items.iter().enumerate() {
                    if RESPONSES_REPLAY_ITEM_TYPES
                        .contains(&item["type"].as_str().ok_or(Error::Protocol)?)
                    {
                        self.replay.push(item.clone());
                    }
                    if item["type"] == "function_call" {
                        out.extend(self.reconcile_call(item, Some(index as u64))?);
                    }
                }
                self.replay.extend(Self::capture_phases(items));
                let finish = if kind == "response.incomplete" {
                    let details = response["incomplete_details"]
                        .as_object()
                        .ok_or(Error::Protocol)?;
                    let reason = details
                        .get("reason")
                        .and_then(Value::as_str)
                        .ok_or(Error::Protocol)?;
                    if reason != "max_output_tokens" {
                        let error = json!({"message":format!("response.incomplete: {}",if reason.is_empty(){"missing reason"}else{reason}),"type":if reason.is_empty(){"server_error"}else{reason}});
                        self.provider_error = Some(error.clone());
                        out.push(Projection::Chunk(json!({"error":error})));
                        out.push(Projection::Done);
                        return Ok(out);
                    }
                    "length"
                } else if self.slots.is_empty() {
                    "stop"
                } else {
                    "tool_calls"
                };
                out.extend(self.terminal_text(items)?);
                let mut usage = convert_usage(&response["usage"])?;
                if items.iter().any(|i| i["type"] == "program_output")
                    && !items.iter().any(|i| i["type"] == "message")
                {
                    usage["_program_pending"] = true.into();
                }
                out.push(self.chunk(json!({}), Some(finish), Some(usage))?);
                out.push(Projection::Done);
                Ok(out)
            }
            "error" | "response.failed" | "response.error" => {
                if event
                    .get("response")
                    .is_some_and(|v| !v.is_null() && !v.is_object())
                {
                    return Err(Error::Protocol);
                }
                let raw = event["response"]
                    .get("error")
                    .filter(|v| truthy(v))
                    .or_else(|| event.get("error").filter(|v| truthy(v)))
                    .unwrap_or(if kind == "error" { event } else { &Value::Null });
                let mut error = raw.as_object().cloned().unwrap_or_default();
                let code = error
                    .get("code")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or(kind)
                    .to_owned();
                let message = error
                    .get("message")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or(kind);
                let combined = format!("{code}: {message}");
                error.insert("message".into(), combined.into());
                if error.get("type").is_none_or(|v| !truthy(v)) {
                    error.insert("type".into(), code.into());
                }
                let error = Value::Object(error);
                self.reserve(size(&error, self.budget.retained_bytes)?)?;
                self.provider_error = Some(error.clone());
                self.closed = true;
                Ok(vec![Projection::Chunk(json!({"error":error}))])
            }
            _ => {
                self.unknown_events = self.unknown_events.saturating_add(1);
                Ok(vec![])
            }
        }
    }
}

/// Responses SSE byte owner over the retained framing/canonical accumulator.
/// This does not select an endpoint or authorize a provider/tool profile.
pub struct Stream {
    framer: crate::sse::Framer,
    translator: Translator,
    canonical: crate::provider::ChatStream,
    closed: bool,
    failure: Option<crate::provider::Error>,
    parse_errors: usize,
}
impl Stream {
    pub fn new(
        output: crate::provider::Budget,
        retained: Budget,
        identical_limit: usize,
        names: std::sync::Arc<BTreeSet<String>>,
        reverse: BTreeMap<String, String>,
    ) -> Result<Self, crate::provider::Error> {
        Ok(Self {
            framer: crate::sse::Framer::new(output.event_bytes)
                .map_err(|_| crate::provider::Error::InvalidBudget)?,
            translator: Translator::new(retained, reverse)
                .map_err(|_| crate::provider::Error::InvalidBudget)?,
            canonical: crate::provider::ChatStream::with_tool_names(
                output,
                identical_limit,
                names,
            )?,
            closed: false,
            failure: None,
            parse_errors: 0,
        })
    }
    pub fn span_text(&self, span: crate::provider::TextSpan) -> Option<&str> {
        self.canonical.span_text(span)
    }
    pub fn provider_error(&self) -> Option<&Value> {
        self.translator.provider_error()
    }
    pub fn response_id(&self) -> &str {
        self.translator.response_id()
    }
    pub fn feed(&mut self, bytes: &[u8]) -> Result<bool, crate::provider::Error> {
        self.feed_with_text(bytes, |_| {})
    }
    pub fn feed_with_text(
        &mut self,
        bytes: &[u8],
        mut observe: impl FnMut(crate::provider::TextSpan),
    ) -> Result<bool, crate::provider::Error> {
        if self.closed {
            return self.failure.map_or(Ok(true), Err);
        }
        let translator = &mut self.translator;
        let canonical = &mut self.canonical;
        let parse_errors = &mut self.parse_errors;
        let mut stopped = false;
        let mut failure = None;
        self.framer
            .feed_until(bytes, |event, issues| {
                canonical.framing_issues(issues.count);
                let value = match serde_json::from_str::<Value>(&event.data) {
                    Ok(value) => {
                        *parse_errors = 0;
                        value
                    }
                    Err(_) => {
                        canonical.framing_issues(1);
                        *parse_errors = parse_errors.saturating_add(1);
                        if *parse_errors >= crate::execution_contract::MAX_CONSECUTIVE_PARSE_ERRORS
                        {
                            failure = Some(crate::provider::Error::CorruptStream);
                            return true;
                        }
                        return false;
                    }
                };
                let projections = match translator.translate(&value) {
                    Ok(values) => values,
                    Err(Error::Capacity) => {
                        failure = Some(crate::provider::Error::OutputLimit);
                        return true;
                    }
                    Err(_) => {
                        failure = Some(crate::provider::Error::CorruptStream);
                        return true;
                    }
                };
                for projection in projections {
                    let result = match projection {
                        Projection::Chunk(value) => {
                            canonical.accept_projection(&value, &mut observe)
                        }
                        Projection::Done => {
                            canonical.provider_done();
                            Ok(true)
                        }
                    };
                    match result {
                        Ok(true) => {
                            stopped = true;
                            return true;
                        }
                        Ok(false) => {}
                        Err(error) => {
                            failure = Some(error);
                            return true;
                        }
                    }
                }
                false
            })
            .map_err(|_| crate::provider::Error::Closed)?;
        self.failure = failure;
        self.closed = stopped || failure.is_some();
        failure.map_or(Ok(stopped), Err)
    }
    pub fn finish(
        mut self,
        aborted: bool,
    ) -> Result<crate::provider::StreamResult, crate::provider::Error> {
        if let Some(error) = self.failure {
            return Err(error);
        }
        if aborted || self.closed {
            self.framer.close();
        } else {
            self.framer.finalize();
        }
        self.canonical
            .framing_issues(self.framer.drain_issues().count);
        let items = self.translator.replay_items();
        let mut result = self.canonical.finish(aborted)?;
        if !items.is_empty() {
            result.message["_responses_items"] = items.into();
        }
        Ok(result)
    }
}
