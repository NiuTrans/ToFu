//! Anthropic event translation and replay state. Caller owns SSE framing,
//! request conversion, routing and transport. No tool execution is authorized.
//! Explicit byte/block admission bounds retained protocol state; JSON tool
//! fragments append without reparsing a growing prefix on every delta.
use crate::execution_contract::{
    anthropic_stop_reason, ANTHROPIC_MAX_BLOCK_INDEX, ANTHROPIC_MAX_TOOL_ID_CHARS,
    ANTHROPIC_MAX_TOOL_NAME_CHARS,
};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
#[derive(Clone, Copy)]
pub struct Budget {
    pub blocks: usize,
    pub retained_bytes: usize,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Protocol,
    Capacity,
    Closed,
}
#[derive(Debug, PartialEq)]
pub enum Projection {
    Chunk(Value),
    Done,
}
struct Block {
    identity: Option<Value>,
    value: Value,
    input: String,
    complete_initial: bool,
    stopped: bool,
}
pub struct Translator {
    budget: Budget,
    charged: usize,
    blocks: BTreeMap<u64, Block>,
    usage: Map<String, Value>,
    reverse: BTreeMap<String, String>,
    replayed: usize,
    closed: bool,
}
struct EncodedSize {
    bytes: usize,
    limit: usize,
}
impl std::io::Write for EncodedSize {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(bytes.len())
            .filter(|n| *n <= self.limit)
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn encoded_size(value: &Value, limit: usize) -> Result<usize, Error> {
    let mut counter = EncodedSize { bytes: 0, limit };
    serde_json::to_writer(&mut counter, value).map_err(|_| Error::Capacity)?;
    Ok(counter.bytes)
}
fn text(value: Option<&Value>) -> Result<&str, Error> {
    match value {
        None | Some(Value::Null) => Ok(""),
        Some(Value::String(s)) => Ok(s),
        _ => Err(Error::Protocol),
    }
}
fn chunk(delta: Value) -> Projection {
    Projection::Chunk(json!({"choices":[{"delta":delta}]}))
}
fn index(value: &Value) -> Result<u64, Error> {
    value["index"]
        .as_u64()
        .filter(|n| *n <= ANTHROPIC_MAX_BLOCK_INDEX as u64)
        .ok_or(Error::Protocol)
}
fn replay(block: &Value) -> bool {
    match block["type"].as_str() {
        Some("compaction") => block["content"]
            .as_str()
            .is_some_and(|s| !s.trim().is_empty()),
        Some("server_tool_use" | "tool_search_tool_result" | "redacted_thinking") => true,
        _ => false,
    }
}
fn count(value: &Value) -> u64 {
    match value {
        Value::Bool(b) => u64::from(*b),
        Value::Number(n) => n.as_u64().unwrap_or_else(|| {
            n.as_f64()
                .filter(|n| n.is_finite() && *n > 0.0)
                .map(|n| n as u64)
                .unwrap_or(0)
        }),
        Value::String(s) => s.trim().parse::<u64>().unwrap_or(0),
        _ => 0,
    }
}
/// Same billed aggregate/context distinction as the retained Python authority.
pub fn convert_usage(raw: &Map<String, Value>) -> Result<Value, Error> {
    let keys = [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "thinking_tokens",
    ];
    let validate = |row: &Map<String, Value>| -> Result<(), Error> {
        for key in keys {
            if let Some(value) = row.get(key) {
                let overflow = match value {
                    Value::Number(number) => {
                        number.as_u64().is_none()
                            && number.as_f64().is_some_and(|n| n >= u64::MAX as f64)
                    }
                    Value::String(text) => text
                        .trim()
                        .parse::<u128>()
                        .is_ok_and(|n| n > u64::MAX as u128),
                    _ => false,
                };
                if overflow {
                    return Err(Error::Capacity);
                }
            }
        }
        Ok(())
    };
    validate(raw)?;
    let top = keys.map(|k| count(raw.get(k).unwrap_or(&Value::Null)));
    let iterations: Vec<&Map<String, Value>> = raw
        .get("iterations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .collect();
    let mut totals = top;
    if !iterations.is_empty() {
        totals = [0; 5];
        for row in &iterations {
            validate(row)?;
            for (i, key) in keys.iter().enumerate() {
                totals[i] = totals[i]
                    .checked_add(count(row.get(*key).unwrap_or(&Value::Null)))
                    .ok_or(Error::Capacity)?;
            }
        }
        for i in 2..5 {
            totals[i] = totals[i].max(top[i]);
        }
    }
    let sum = |a: u64, b: u64, c: u64| {
        a.checked_add(b)
            .and_then(|v| v.checked_add(c))
            .ok_or(Error::Capacity)
    };
    let prompt = sum(totals[0], totals[2], totals[3])?;
    let mut out = json!({"prompt_tokens":prompt,"completion_tokens":totals[1],"total_tokens":sum(prompt,totals[1],0)?,"input_tokens":totals[0],"output_tokens":totals[1],"cache_creation_input_tokens":totals[2],"cache_read_input_tokens":totals[3],"effective_prompt_tokens":sum(top[0],top[2],top[3])?,"effective_output_tokens":top[1]});
    if totals[4] > 0 {
        out["thinking_tokens"] = totals[4].into();
        out["reasoning_tokens"] = totals[4].into();
    }
    if !iterations.is_empty() {
        let mut rows = 0u64;
        let mut input = 0u64;
        let mut output = 0u64;
        for row in iterations {
            if row.get("type").and_then(Value::as_str) == Some("compaction") {
                rows += 1;
                input = input
                    .checked_add(sum(
                        count(row.get(keys[0]).unwrap_or(&Value::Null)),
                        count(row.get(keys[2]).unwrap_or(&Value::Null)),
                        count(row.get(keys[3]).unwrap_or(&Value::Null)),
                    )?)
                    .ok_or(Error::Capacity)?;
                output = output
                    .checked_add(count(row.get(keys[1]).unwrap_or(&Value::Null)))
                    .ok_or(Error::Capacity)?;
            }
        }
        out["compaction_iterations"] = rows.into();
        out["compaction_input_tokens"] = input.into();
        out["compaction_output_tokens"] = output.into();
    }
    Ok(out)
}
impl Translator {
    pub fn new(budget: Budget, reverse: BTreeMap<String, String>) -> Result<Self, Error> {
        if budget.blocks == 0
            || budget.blocks > ANTHROPIC_MAX_BLOCK_INDEX + 1
            || budget.retained_bytes == 0
        {
            return Err(Error::Configuration);
        }
        let mut owner = Self {
            budget,
            charged: 0,
            blocks: BTreeMap::new(),
            usage: Map::new(),
            reverse,
            replayed: 0,
            closed: false,
        };
        let map_bytes = owner.reverse.iter().try_fold(0usize, |n, (a, b)| {
            n.checked_add(a.len() + b.len() + 64).ok_or(Error::Capacity)
        })?;
        owner.reserve(map_bytes)?;
        Ok(owner)
    }
    fn reserve(&mut self, bytes: usize) -> Result<(), Error> {
        self.charged = self
            .charged
            .checked_add(bytes)
            .filter(|n| *n <= self.budget.retained_bytes)
            .ok_or(Error::Capacity)?;
        Ok(())
    }
    pub fn charged_bytes(&self) -> usize {
        self.charged
    }
    pub fn replayed_tool_starts(&self) -> usize {
        self.replayed
    }
    /// Detached snapshot; parsing partial tool input is deferred until requested.
    pub fn replay_blocks(&self) -> Vec<Value> {
        if !self.blocks.values().any(|b| replay(&b.value)) {
            return vec![];
        }
        self.blocks
            .values()
            .filter(|b| b.value["type"] != "compaction" || replay(&b.value))
            .map(|b| {
                let mut value = b.value.clone();
                if !b.input.is_empty() {
                    if let Ok(input) = serde_json::from_str::<Value>(&b.input) {
                        value["input"] = input;
                    }
                }
                value
            })
            .collect()
    }
    /// Each structural/capacity error permanently closes this response owner.
    pub fn translate(&mut self, event: &Value) -> Result<Option<Projection>, Error> {
        if self.closed {
            return Err(Error::Closed);
        }
        // Bound caller-supplied event data before cloning protocol fields.
        let result =
            encoded_size(event, self.budget.retained_bytes).and_then(|_| self.event(event));
        if result.is_err() || matches!(result, Ok(Some(Projection::Done))) {
            self.closed = true;
        }
        result
    }
    fn validate_input(block: &mut Block) -> Result<(), Error> {
        if !block.input.is_empty() {
            let value: Value = serde_json::from_str(&block.input).map_err(|_| Error::Protocol)?;
            if !value.is_object() {
                return Err(Error::Protocol);
            }
            block.value["input"] = value;
        }
        Ok(())
    }
    fn event(&mut self, event: &Value) -> Result<Option<Projection>, Error> {
        let object = event.as_object().ok_or(Error::Protocol)?;
        let kind = object
            .get("type")
            .map_or(Ok(""), |v| v.as_str().ok_or(Error::Protocol))?;
        match kind {
            "message_start" => {
                let message = event["message"].as_object().ok_or(Error::Protocol)?;
                if let Some(value) = message.get("usage").filter(|v| !v.is_null()) {
                    let usage = value.as_object().ok_or(Error::Protocol)?;
                    if !usage.is_empty() {
                        self.reserve(encoded_size(value, self.budget.retained_bytes)?)?;
                        self.usage = usage.clone();
                        return Ok(Some(Projection::Chunk(
                            json!({"choices":[{"delta":{}}],"usage":convert_usage(&self.usage)?}),
                        )));
                    }
                }
                Ok(None)
            }
            "content_block_start" => {
                let i = index(event)?;
                let mut value = event["content_block"].clone();
                let kind = value["type"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or(Error::Protocol)?
                    .to_owned();
                let mut delta = json!({});
                let mut input = String::new();
                match kind.as_str() {
                    "text" => {
                        let t = text(value.get("text"))?.to_owned();
                        value["text"] = t.clone().into();
                        if !t.is_empty() {
                            delta["content"] = t.into();
                        }
                    }
                    "thinking" => {
                        let t = text(value.get("thinking"))?.to_owned();
                        let s = text(value.get("signature"))?.to_owned();
                        value["thinking"] = t.clone().into();
                        if value.get("signature").is_some() {
                            value["signature"] = s.clone().into();
                        }
                        if !t.is_empty() {
                            delta["reasoning_content"] = t.into();
                        }
                        if !s.is_empty() {
                            delta["thinking_signature"] = s.into();
                        }
                    }
                    "tool_use" | "server_tool_use" => {
                        if value["input"].is_null() {
                            value["input"] = json!({});
                        }
                        let args = value["input"].as_object().ok_or(Error::Protocol)?;
                        if !args.is_empty() {
                            input = serde_json::to_string(args).map_err(|_| Error::Protocol)?;
                        }
                    }
                    "compaction" => {
                        text(value.get("content"))?;
                    }
                    _ => {}
                }
                if let Some(previous) = self.blocks.get(&i) {
                    if previous.identity.as_ref() != Some(&value) {
                        return Err(Error::Protocol);
                    }
                    if kind == "tool_use" {
                        self.replayed += 1;
                    }
                    return Ok(None);
                }
                if self.blocks.len() >= self.budget.blocks {
                    return Err(Error::Capacity);
                }
                let bytes = encoded_size(&value, self.budget.retained_bytes)?;
                self.reserve(
                    bytes
                        .checked_mul(3)
                        .and_then(|n| n.checked_add(256))
                        .ok_or(Error::Capacity)?,
                )?;
                if kind == "tool_use" {
                    let name = value["name"]
                        .as_str()
                        .filter(|s| {
                            !s.is_empty() && s.chars().count() <= ANTHROPIC_MAX_TOOL_NAME_CHARS
                        })
                        .ok_or(Error::Protocol)?;
                    let id = value["id"]
                        .as_str()
                        .filter(|s| {
                            !s.is_empty() && s.chars().count() <= ANTHROPIC_MAX_TOOL_ID_CHARS
                        })
                        .ok_or(Error::Protocol)?;
                    delta = json!({"tool_calls":[{"index":i,"id":id,"type":"function","function":{"name":self.reverse.get(name).map(String::as_str).unwrap_or(name),"arguments":input}}]});
                }
                self.blocks.insert(
                    i,
                    Block {
                        identity: Some(value.clone()),
                        value,
                        complete_initial: !input.is_empty(),
                        input,
                        stopped: false,
                    },
                );
                if delta.as_object().is_some_and(|o| !o.is_empty()) {
                    Ok(Some(chunk(delta)))
                } else {
                    Ok(None)
                }
            }
            "content_block_delta" => {
                let i = index(event)?;
                if self.blocks.get(&i).is_some_and(|block| block.stopped) {
                    return Err(Error::Protocol);
                }
                let delta = event["delta"].as_object().ok_or(Error::Protocol)?;
                let kind = delta
                    .get("type")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .ok_or(Error::Protocol)?;
                let (field, expected, projection) = match kind {
                    "text_delta" => ("text", "text", Some("content")),
                    "thinking_delta" => ("thinking", "thinking", Some("reasoning_content")),
                    "signature_delta" => ("signature", "thinking", Some("thinking_signature")),
                    "compaction_delta" => ("content", "compaction", None),
                    "input_json_delta" => ("partial_json", "tool", None),
                    _ => {
                        return if self.blocks.contains_key(&i) {
                            Ok(None)
                        } else {
                            Err(Error::Protocol)
                        }
                    }
                };
                let t = text(delta.get(field))?;
                if !self.blocks.contains_key(&i) {
                    if !matches!(kind, "text_delta" | "thinking_delta" | "signature_delta") {
                        return Err(Error::Protocol);
                    }
                    if self.blocks.len() >= self.budget.blocks {
                        return Err(Error::Capacity);
                    }
                    self.reserve(256)?;
                    self.blocks.insert(
                        i,
                        Block {
                            identity: None,
                            value: json!({"type":expected}),
                            input: String::new(),
                            complete_initial: false,
                            stopped: false,
                        },
                    );
                }
                // String growth plus eventual parsed input/replay copies fit this
                // conservative byte charge; allocation capacity may be up to 2x.
                self.reserve(t.len().checked_mul(4).ok_or(Error::Capacity)?)?;
                let block = self.blocks.get_mut(&i).unwrap();
                let btype = block.value["type"].as_str().ok_or(Error::Protocol)?;
                if block.stopped
                    || if expected == "tool" {
                        !matches!(btype, "tool_use" | "server_tool_use")
                    } else {
                        btype != expected
                    }
                {
                    return Err(Error::Protocol);
                }
                if kind == "input_json_delta" {
                    if block.complete_initial {
                        return Err(Error::Protocol);
                    }
                    let client = btype == "tool_use";
                    block.input.push_str(t);
                    return Ok(if client {
                        Some(chunk(
                            json!({"tool_calls":[{"index":i,"function":{"arguments":t}}]}),
                        ))
                    } else {
                        None
                    });
                }
                if kind == "compaction_delta" {
                    block.value[field] = t.into();
                    return Ok(None);
                }
                if block.value[field].is_null() {
                    block.value[field] = "".into();
                }
                match block.value.get_mut(field) {
                    Some(Value::String(target)) => target.push_str(t),
                    _ => return Err(Error::Protocol),
                }
                Ok(projection.map(|key| chunk(json!({key:t}))))
            }
            "content_block_stop" => {
                let i = index(event)?;
                let block = self.blocks.get_mut(&i).ok_or(Error::Protocol)?;
                if !block.stopped {
                    Self::validate_input(block)?;
                    block.stopped = true;
                }
                Ok(None)
            }
            "message_delta" => {
                let delta = event["delta"].as_object().ok_or(Error::Protocol)?;
                let reason = text(delta.get("stop_reason"))?;
                let mut out = json!({"choices":[{"delta":{}}]});
                if !reason.is_empty() {
                    out["choices"][0]["finish_reason"] = anthropic_stop_reason(reason).into();
                }
                if let Some(value) = event.get("usage").filter(|v| !v.is_null()) {
                    let usage = value.as_object().ok_or(Error::Protocol)?;
                    if !usage.is_empty() {
                        self.reserve(encoded_size(value, self.budget.retained_bytes)?)?;
                        for (key, v) in usage {
                            if v.is_null() {
                                continue;
                            }
                            let merged = if key == "output_tokens"
                                || (!v.is_number()
                                    && !v.is_boolean()
                                    && !v
                                        .as_str()
                                        .is_some_and(|s| s.trim().parse::<i128>().is_ok()))
                            {
                                v.clone()
                            } else {
                                count(self.usage.get(key).unwrap_or(&Value::Null))
                                    .max(count(v))
                                    .into()
                            };
                            self.usage.insert(key.clone(), merged);
                        }
                        out["usage"] = convert_usage(&self.usage)?;
                    }
                }
                Ok(Some(Projection::Chunk(out)))
            }
            "message_stop" => {
                for block in self.blocks.values_mut() {
                    Self::validate_input(block)?;
                }
                Ok(Some(Projection::Done))
            }
            "error" => {
                let mut error = event["error"].as_object().cloned().unwrap_or_default();
                if !error.get("message").is_some_and(Value::is_string) {
                    error.insert("message".into(), "anthropic stream error".into());
                }
                if error.get("type").is_some_and(|v| !v.is_string()) {
                    error.insert("type".into(), "server_error".into());
                }
                self.closed = true;
                Ok(Some(Projection::Chunk(json!({"error":error}))))
            }
            _ => Ok(None),
        }
    }
}

/// Byte-stream owner over the existing SSE framer and Chat accumulator. Native
/// text spans, tool assembly/flood checks and terminal classification stay shared.
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
        names: std::sync::Arc<std::collections::BTreeSet<String>>,
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
        let mut stopped = false;
        let mut failure = None;
        let parse_errors = &mut self.parse_errors;
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
                let result = match translator.translate(&value) {
                    Ok(Some(Projection::Chunk(value))) => {
                        canonical.accept_projection(&value, &mut observe)
                    }
                    Ok(Some(Projection::Done)) => {
                        canonical.provider_done();
                        Ok(true)
                    }
                    Ok(None) => Ok(false),
                    Err(Error::Capacity) => Err(crate::provider::Error::OutputLimit),
                    Err(_) => Err(crate::provider::Error::CorruptStream),
                };
                match result {
                    Ok(done) => {
                        stopped = done;
                        done
                    }
                    Err(error) => {
                        failure = Some(error);
                        true
                    }
                }
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
        if !aborted {
            for block in self.translator.blocks.values_mut() {
                Translator::validate_input(block)
                    .map_err(|_| crate::provider::Error::CorruptStream)?;
            }
        }
        let blocks = self.translator.replay_blocks();
        let mut result = self.canonical.finish(aborted)?;
        if !blocks.is_empty() {
            result.message["_anthropic_content_blocks"] = blocks.into();
        }
        Ok(result)
    }
}
