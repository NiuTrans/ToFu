//! Selected local tiktoken-tier input counter over immutable Chat request data.
//! Owned lazy encoder sharing is explicit; no global cache, network, model-name
//! guessing, tokenizer fallback, pricing or billing authority. Framing constants
//! derive from Python; missing/unsupported input fails before model dispatch.
use crate::{
    agent_http::InputCounter,
    execution_contract::{TOKEN_IMAGE_OVERHEAD, TOKEN_MESSAGE_OVERHEAD, TOKEN_REQUEST_OVERHEAD},
};
use serde_json::{json, Map, Value};
use std::{
    io::{self, Write},
    sync::{Arc, OnceLock},
};
#[derive(Clone, Copy, Debug)]
pub enum Encoding {
    Cl100k,
    O200k,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Identity,
    InputShape,
    InputBytes,
    ToolBytes,
    Depth,
    Overflow,
    Encoder,
}
pub struct Encoder {
    encoding: Encoding,
    core: OnceLock<Result<crate::token_rank_counter::RankCounter, Error>>,
}
impl Encoder {
    pub fn new(encoding: Encoding) -> Arc<Self> {
        Arc::new(Self {
            encoding,
            core: OnceLock::new(),
        })
    }
    pub fn loaded(&self) -> bool {
        self.core.get().is_some()
    }
    pub(crate) fn count(&self, text: &str, scratch: &mut Option<fancy_regex::Regex>) -> Result<u64, Error> {
        if text.is_empty() {
            return Ok(0);
        }
        let core = self.core.get_or_init(|| crate::token_rank_counter::RankCounter::new(self.encoding));
        let core = core.as_ref().map_err(|e| *e)?;
        core.count(text, scratch)

    }
}
#[derive(Clone, Copy)]
pub struct Bounds {
    pub input_bytes: usize,
    pub text_bytes: usize,
    pub tool_json_bytes: usize,
    pub depth: usize,
}
pub struct Counter {
    owner: u64,
    task: String,
    wire_model: String,
    encoder: Arc<Encoder>,
    bounds: Bounds,
}
impl Counter {
    pub fn new(
        owner: u64,
        task: String,
        wire_model: String,
        encoder: Arc<Encoder>,
        bounds: Bounds,
    ) -> Result<Self, Error> {
        if owner == 0
            || task.is_empty()
            || wire_model.is_empty()
            || bounds.depth > 128
            || [
                bounds.input_bytes,
                bounds.text_bytes,
                bounds.tool_json_bytes,
                bounds.depth,
            ]
            .contains(&0)
        {
            return Err(Error::Configuration);
        }
        Ok(Self {
            owner,
            task,
            wire_model,
            encoder,
            bounds,
        })
    }
    pub fn count(
        &self,
        owner: u64,
        task: &str,
        messages: &[Value],
        template: &Map<String, Value>,
    ) -> Result<u64, Error> {
        if owner != self.owner
            || task != self.task
            || template.get("model").and_then(Value::as_str) != Some(&self.wire_model)
        {
            return Err(Error::Identity);
        }
        let mut total = TOKEN_MESSAGE_OVERHEAD
            .checked_mul(messages.len() as u64)
            .and_then(|v| v.checked_add(TOKEN_REQUEST_OVERHEAD))
            .ok_or(Error::Overflow)?;
        let mut bytes = 0usize;
        let mut scratch = None;
        let mut add = |text: &str| -> Result<(), Error> {
            if text.len() > self.bounds.text_bytes {
                return Err(Error::InputBytes);
            }
            bytes = bytes
                .checked_add(text.len())
                .filter(|n| *n <= self.bounds.input_bytes)
                .ok_or(Error::InputBytes)?;
            total = total
                .checked_add(self.encoder.count(text, &mut scratch)?)
                .ok_or(Error::Overflow)?;
            Ok(())
        };
        if let Some(system) = template.get("system") {
            match system {
                Value::String(s) => add(s)?,
                Value::Array(blocks) => {
                    for b in blocks {
                        if let Some(s) = b.get("text").and_then(Value::as_str) {
                            add(s)?;
                        }
                    }
                }
                _ => {}
            }
        }
        if let Some(tools) = template.get("tools").filter(|v| truthy(v)) {
            let text = tool_text(tools, self.bounds.tool_json_bytes, self.bounds.depth)?;
            add(&text)?;
        }
        for message in messages {
            let message = message.as_object().ok_or(Error::InputShape)?;
            match message.get("content") {
                Some(Value::String(s)) => add(s)?,
                Some(Value::Array(blocks)) => {
                    for b in blocks {
                        if let Some(kind) = b.get("type").and_then(Value::as_str) {
                            match kind {
                                "text" | "input_text" | "output_text" => {
                                    if let Some(s) = b.get("text").and_then(Value::as_str) {
                                        add(s)?;
                                    }
                                }
                                "tool_result" => match b.get("content") {
                                    Some(Value::String(s)) => add(s)?,
                                    Some(Value::Array(sub)) => {
                                        for b in sub {
                                            if let Some(s) = b.get("text").and_then(Value::as_str) {
                                                add(s)?;
                                            }
                                        }
                                    }
                                    _ => {}
                                },
                                _ => {}
                            }
                        }
                    }
                }
                _ => {}
            }
            match message.get("reasoning_content") {
                Some(Value::String(s)) => add(s)?,
                Some(Value::Array(blocks)) => {
                    for b in blocks {
                        if let Some(s) = b.get("text").and_then(Value::as_str) {
                            add(s)?;
                        }
                    }
                }
                _ => {}
            }
            if let Some(calls) = message.get("tool_calls").filter(|v| truthy(v)) {
                let calls = calls.as_array().ok_or(Error::InputShape)?;
                for call in calls {
                    let call = call.as_object().ok_or(Error::InputShape)?;
                    if let Some(function) = call.get("function").filter(|v| truthy(v)) {
                        let function = function.as_object().ok_or(Error::InputShape)?;
                        if let Some(s) = function.get("arguments").and_then(Value::as_str) {
                            add(s)?;
                        }
                        if let Some(s) = function.get("name").and_then(Value::as_str) {
                            add(s)?;
                        }
                    }
                }
            }
        }
        // End the text closure's mutable borrow before the separate image fee.
        for message in messages {
            if let Some(blocks) = message.get("content").and_then(Value::as_array) {
                for b in blocks {
                    if b.get("type").and_then(Value::as_str) == Some("image_url") {
                        total = total
                            .checked_add(TOKEN_IMAGE_OVERHEAD)
                            .ok_or(Error::Overflow)?;
                    }
                }
            }
        }
        Ok(total)
    }
}
impl InputCounter for Counter {
    fn tokens(
        &self,
        owner: u64,
        task: &str,
        messages: &[Value],
        template: &Map<String, Value>,
    ) -> Result<u64, Value> {
        self.count(owner, task, messages, template)
            .map_err(|error| json!({"kind":"token_input_counter","message":format!("{error:?}")}))
    }
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::Number(v) => v.as_f64().is_some_and(|v| v != 0.0),
    }
}
fn depth(v: &Value, remaining: usize) -> Result<(), Error> {
    if remaining == 0 {
        return Err(Error::Depth);
    }
    match v {
        Value::Array(a) => {
            for v in a {
                depth(v, remaining - 1)?;
            }
        }
        Value::Object(a) => {
            for v in a.values() {
                depth(v, remaining - 1)?;
            }
        }
        _ => {}
    }
    Ok(())
}
pub fn tool_text(tools: &Value, limit: usize, max_depth: usize) -> Result<String, Error> {
    if limit == 0 || max_depth == 0 || max_depth > 128 {
        return Err(Error::Configuration);
    }
    depth(tools, max_depth)?;
    let mut output = Bounded {
        data: Vec::new(),
        limit,
    };
    let mut serializer = serde_json::Serializer::with_formatter(&mut output, PythonJson);
    serde_core::Serialize::serialize(tools, &mut serializer).map_err(|_| Error::ToolBytes)?;
    String::from_utf8(output.data).map_err(|_| Error::InputShape)
}
struct Bounded {
    data: Vec<u8>,
    limit: usize,
}
impl Write for Bounded {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        if b.len() > self.limit.saturating_sub(self.data.len()) {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        self.data.extend_from_slice(b);
        Ok(b.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
/// Python json.dumps separators and finite-float lexical conventions affect BPE.
struct PythonJson;
impl serde_json::ser::Formatter for PythonJson {
    fn begin_array_value<W: Write + ?Sized>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if !first {
            w.write_all(b", ")?;
        }
        Ok(())
    }
    fn begin_object_key<W: Write + ?Sized>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if !first {
            w.write_all(b", ")?;
        }
        Ok(())
    }
    fn begin_object_value<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
    fn write_f64<W: Write + ?Sized>(&mut self, w: &mut W, n: f64) -> io::Result<()> {
        let display = n.to_string();
        let negative = display.starts_with('-');
        let s = display.strip_prefix('-').unwrap_or(&display);
        let point = s.find('.').unwrap_or(s.len());
        let digits: String = s.chars().filter(|c| *c != '.').collect();
        let first = digits.find(|c| c != '0');
        let rendered = if let Some(first) = first {
            let exponent = point as i32 - first as i32 - 1;
            if !(-4..16).contains(&exponent) {
                let significant = digits[first..].trim_end_matches('0');
                let coefficient = if significant.len() > 1 {
                    format!("{}.{}", &significant[..1], &significant[1..])
                } else {
                    significant.into()
                };
                format!(
                    "{}{}e{}{:02}",
                    if negative { "-" } else { "" },
                    coefficient,
                    if exponent >= 0 { "+" } else { "-" },
                    exponent.unsigned_abs()
                )
            } else if s.contains('.') {
                display
            } else {
                format!("{display}.0")
            }
        } else {
            if negative { "-0.0" } else { "0.0" }.into()
        };
        w.write_all(rendered.as_bytes())
    }
}
