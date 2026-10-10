//! Native Chat-compatible stream owner over bounded SSE framing. No model
//! routing, credential, transport retry or tool execution authority lives here.
use crate::tool_assembly::{Assembly, Delta, Error as AssemblyError};
use crate::tool_policy::{normalize_caller, FloodError, FloodGuard};
use crate::{
    execution_contract::{
        classify_stream, ProviderStreamState, StreamFacts, DEFAULT_IDENTICAL_TOOL_CALLS,
        GATEWAY_LIMIT_MAX_CHARS, GATEWAY_LIMIT_PHRASES, INTERNAL_TOOL_PREFIXES,
        MAX_CONSECUTIVE_PARSE_ERRORS, MAX_TOOL_CALL_ID_CHARS, MAX_TOOL_CALL_INDEX,
        TOOL_FLOOD_ENVELOPE_JSON,
    },
    sse::{Error as FramingError, Framer},
};
use serde_json::{json, Value};
use std::{collections::BTreeSet, sync::Arc};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TextChannel {
    Content,
    Reasoning,
}
/// One nonempty accepted append; offsets borrow the existing accumulator.
/// Only this parser mints spans. They are read before consuming the stream.
#[derive(Clone, Copy, Debug)]
pub struct TextSpan {
    pub channel: TextChannel,
    start: usize,
    end: usize,
}
impl TextSpan {
    /// Adjacent appends on one channel have identical concatenated text.
    /// Channel changes remain separate so their provider order is retained.
    pub fn append_contiguous(&mut self, next: Self) -> bool {
        if self.channel != next.channel || self.end != next.start {
            return false;
        }
        self.end = next.end;
        true
    }
}
#[derive(Clone, Copy)]
pub struct Budget {
    pub event_bytes: usize,
    pub output_bytes: usize,
    pub tool_calls: usize,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    InvalidBudget,
    Closed,
    OutputLimit,
    ToolLimit,
    ToolFlood { count: usize, limit: usize },
    InvalidToolCaller,
    UnsupportedToolArguments,
    UnsupportedToolShape,
    CorruptStream,
    ProviderError,
}
#[derive(Debug)]
pub struct StreamResult {
    pub state: ProviderStreamState,
    pub message: Value,
    pub usage: Value,
    pub finish_reason: Option<String>,
    pub done_seen: bool,
    pub malformed_frames: u64,
}
impl StreamResult {
    pub fn into_task_result(self) -> Result<Value, Value> {
        if self.state == ProviderStreamState::ProviderFinished {
            Ok(
                json!({"message":self.message,"usage":self.usage,"finish_reason":self.finish_reason}),
            )
        } else {
            Err(
                json!({"kind":"provider_stream","message":self.state.as_str(),"malformed_frames":self.malformed_frames}),
            )
        }
    }
}
struct Accumulator {
    budget: Budget,
    bytes: usize,
    content: String,
    reasoning: String,
    signature: String,
    tools: Assembly,
    usage: Value,
    finish: Option<String>,
    done: bool,
    malformed: u64,
    parse_errors: usize,
    failure: Option<Error>,
    flood: FloodGuard,
}
impl Accumulator {
    fn reserve(&mut self, n: usize) -> Result<(), Error> {
        self.bytes = self.bytes.checked_add(n).ok_or(Error::OutputLimit)?;
        if self.bytes > self.budget.output_bytes {
            Err(Error::OutputLimit)
        } else {
            Ok(())
        }
    }
    fn text(&mut self, v: &Value) -> Option<String> {
        match v {
            Value::Null => None,
            Value::String(s) => Some(s.clone()),
            _ => {
                self.malformed += 1;
                None
            }
        }
    }
    fn payload(&mut self, text: &str, observe: &mut impl FnMut(TextSpan)) -> Result<bool, Error> {
        if text == "[DONE]" {
            self.done = true;
            return Ok(true);
        }
        if text.trim().is_empty() {
            return Ok(false);
        }
        let v: Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(_) => {
                self.malformed += 1;
                self.parse_errors = self.parse_errors.saturating_add(1);
                if self.parse_errors >= MAX_CONSECUTIVE_PARSE_ERRORS {
                    return Err(Error::CorruptStream);
                }
                return Ok(false);
            }
        };
        self.parse_errors = 0;
        self.projected(&v, observe)
    }
    fn projected(&mut self, v: &Value, observe: &mut impl FnMut(TextSpan)) -> Result<bool, Error> {
        let Some(object) = v.as_object() else {
            self.malformed += 1;
            return Ok(false);
        };
        if object.get("error").is_some_and(|e| !e.is_null()) {
            return Err(Error::ProviderError);
        }
        if !object.contains_key("choices") {
            if let Some(kind) = v["type"].as_str() {
                if kind.starts_with("response.")
                    || [
                        "message_start",
                        "content_block_start",
                        "content_block_delta",
                        "content_block_stop",
                        "message_delta",
                        "message_stop",
                    ]
                    .contains(&kind)
                {
                    self.malformed += 1;
                    return Ok(true);
                }
            }
        }
        if !v["usage"].is_null() {
            if v["usage"].is_object() {
                self.reserve(serde_json::to_vec(&v["usage"]).unwrap().len())?;
                self.usage = v["usage"].clone();
            } else {
                self.malformed += 1;
            }
        }
        let choices = match v.get("choices") {
            None => return Ok(false),
            Some(Value::Array(a)) => a,
            _ => {
                self.malformed += 1;
                return Ok(false);
            }
        };
        let Some(choice) = choices.first() else {
            return Ok(false);
        };
        if !choice.is_object() {
            self.malformed += 1;
            return Ok(false);
        }
        if choice.get("message").is_some() && choice.get("delta").is_none() {
            self.malformed += 1;
        }
        if let Some(reason) = self.text(&choice["finish_reason"]) {
            if !reason.is_empty() {
                self.reserve(reason.len())?;
                self.finish = Some(reason);
            }
        }
        if !choice["usage"].is_null() {
            if choice["usage"].is_object() {
                self.reserve(serde_json::to_vec(&choice["usage"]).unwrap().len())?;
                self.usage = choice["usage"].clone();
            } else {
                self.malformed += 1;
            }
        }
        let delta = &choice["delta"];
        if delta.is_null() {
            return Ok(false);
        }
        if !delta.is_object() {
            self.malformed += 1;
            return Ok(false);
        }
        let mut thinking = None;
        for key in ["thinking", "reasoning_content", "reasoning"] {
            if let Some(text) = self.text(&delta[key]) {
                if !text.is_empty() {
                    thinking = Some(text);
                    break;
                }
            }
        }
        if delta["role"] == "thinking" && thinking.is_none() {
            thinking = self.text(&delta["content"]);
        }
        if let Some(text) = thinking {
            self.reserve(text.len())?;
            let start = self.reasoning.len();
            self.reasoning.push_str(&text);
            if !text.is_empty() {
                observe(TextSpan {
                    channel: TextChannel::Reasoning,
                    start,
                    end: self.reasoning.len(),
                });
            }
        }
        if let Some(text) = self.text(&delta["thinking_signature"]) {
            self.reserve(text.len())?;
            self.signature.push_str(&text);
        }
        // Rich reasoning-details require the dedicated provider translator. Fail
        // closed until that adapter is ported rather than silently lose signatures.
        if !delta["reasoning_details"].is_null() {
            self.malformed += 1;
        }
        if delta["role"] != "thinking" {
            if let Some(text) = self.text(&delta["content"]) {
                self.reserve(text.len())?;
                let start = self.content.len();
                self.content.push_str(&text);
                if !text.is_empty() {
                    observe(TextSpan {
                        channel: TextChannel::Content,
                        start,
                        end: self.content.len(),
                    });
                }
            }
        }
        if !delta["tool_calls"].is_null() {
            let Some(calls) = delta["tool_calls"].as_array() else {
                self.malformed += 1;
                return Ok(false);
            };
            for call in calls {
                if !call.is_object() {
                    self.malformed += 1;
                    continue;
                }
                let index = call
                    .get("index")
                    .and_then(|value| match value {
                        Value::Number(_) => value.as_u64(),
                        Value::String(text) => {
                            let digits = text.trim();
                            if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) {
                                digits.parse::<u64>().ok()
                            } else {
                                None
                            }
                        }
                        _ => None,
                    })
                    .filter(|index| *index <= MAX_TOOL_CALL_INDEX as u64);
                if call.get("index").is_some() && index.is_none() {
                    self.malformed += 1;
                }
                let caller = normalize_caller(&call["caller"], false)
                    .map_err(|_| Error::InvalidToolCaller)?;
                if let Some(caller) = &caller {
                    self.reserve(serde_json::to_vec(caller).unwrap().len())?;
                }
                let mut id = self.text(&call["id"]);
                if id
                    .as_ref()
                    .is_some_and(|id| id.chars().count() > MAX_TOOL_CALL_ID_CHARS)
                {
                    self.malformed += 1;
                    id = None;
                }
                let name = self.text(&call["function"]["name"]);
                let args = self.text(&call["function"]["arguments"]);
                if !call["function"].is_null() && !call["function"].is_object() {
                    self.malformed += 1;
                }
                let extra = call
                    .get("extra_content")
                    .filter(|value| json_truthy(value))
                    .cloned();
                self.reserve(
                    id.as_ref().map_or(0, String::len)
                        + name.as_ref().map_or(0, String::len)
                        + args.as_ref().map_or(0, String::len)
                        + extra
                            .as_ref()
                            .map_or(0, |v| serde_json::to_vec(v).unwrap().len()),
                )?;
                let position = self
                    .tools
                    .feed(Delta {
                        index,
                        id,
                        name,
                        arguments: args,
                        caller,
                        caller_present: call.get("caller").is_some(),
                        extra_content: extra,
                    })
                    .map_err(|error| match error {
                        AssemblyError::Capacity => Error::ToolLimit,
                        AssemblyError::CallerChanged => Error::InvalidToolCaller,
                    })?;
                let slot = &self.tools.calls[&position];
                if slot.arguments.trim_end().ends_with('}') {
                    self.flood
                        .observe_parts(
                            position as usize,
                            &slot.name,
                            &slot.arguments,
                            slot.caller.as_ref(),
                            false,
                        )
                        .map_err(flood_error)?;
                }
            }
        }
        Ok(false)
    }
}
pub struct ChatStream {
    framer: Framer,
    acc: Accumulator,
    closed: bool,
}
impl ChatStream {
    pub fn new(budget: Budget) -> Result<Self, Error> {
        Self::with_tool_limit(budget, DEFAULT_IDENTICAL_TOOL_CALLS)
    }
    pub fn with_tool_limit(budget: Budget, identical_limit: usize) -> Result<Self, Error> {
        Self::with_tool_names(budget, identical_limit, Arc::new(BTreeSet::new()))
    }
    pub fn with_tool_names(
        budget: Budget,
        identical_limit: usize,
        names: Arc<BTreeSet<String>>,
    ) -> Result<Self, Error> {
        if budget.event_bytes == 0 || budget.output_bytes == 0 || budget.tool_calls == 0 {
            return Err(Error::InvalidBudget);
        }
        Ok(Self {
            framer: Framer::new(budget.event_bytes).map_err(|_| Error::InvalidBudget)?,
            acc: Accumulator {
                budget,
                bytes: 0,
                content: String::new(),
                reasoning: String::new(),
                signature: String::new(),
                tools: Assembly::new(budget.tool_calls, names),
                usage: json!({}),
                finish: None,
                done: false,
                malformed: 0,
                parse_errors: 0,
                failure: None,
                flood: FloodGuard::new(identical_limit, budget.tool_calls).map_err(flood_error)?,
            },
            closed: false,
        })
    }
    /// Provider adapters submit decoded canonical projections without a JSON
    /// serialization/reparse round trip. Only in-crate translators use this seam.
    pub(crate) fn accept_projection(&mut self, value: &Value, observe: &mut impl FnMut(TextSpan)) -> Result<bool, Error> {
        if self.closed { return self.acc.failure.map_or(Ok(true), Err); }
        match self.acc.projected(value, observe) {
            Ok(stop) => { self.closed = stop; Ok(stop) }
            Err(error) => { self.acc.failure = Some(error); self.closed = true; Err(error) }
        }
    }
    pub(crate) fn provider_done(&mut self) { self.acc.done = true; self.closed = true; }
    pub(crate) fn framing_issues(&mut self, count: u64) { self.acc.malformed = self.acc.malformed.saturating_add(count); }
    pub fn tool_assembly_footprint(&self) -> crate::tool_assembly::Footprint {
        self.acc.tools.footprint()
    }
    pub fn feed(&mut self, bytes: &[u8]) -> Result<bool, Error> {
        self.feed_with_text(bytes, |_| {})
    }
    pub fn span_text(&self, span: TextSpan) -> Option<&str> {
        let text = match span.channel {
            TextChannel::Content => &self.acc.content,
            TextChannel::Reasoning => &self.acc.reasoning,
        };
        text.get(span.start..span.end)
    }
    /// Report accepted text in provider order, including valid prefixes before
    /// a later frame failure. Observation does not certify terminal success.
    pub fn feed_with_text(
        &mut self,
        bytes: &[u8],
        mut observe: impl FnMut(TextSpan),
    ) -> Result<bool, Error> {
        if self.closed {
            return self.acc.failure.map_or(Ok(true), Err);
        }
        let acc = &mut self.acc;
        let mut stopped = false;
        match self.framer.feed_until(bytes, |event, issues| {
            acc.malformed = acc.malformed.saturating_add(issues.count);
            match acc.payload(&event.data, &mut observe) {
                Ok(stop) => {
                    stopped = stop;
                    stop
                }
                Err(error) => {
                    acc.failure = Some(error);
                    true
                }
            }
        }) {
            Ok(_) => {}
            Err(FramingError::Closed) => return Err(Error::Closed),
            Err(_) => return Err(Error::InvalidBudget),
        }
        if let Some(error) = self.acc.failure {
            self.closed = true;
            return Err(error);
        }
        self.closed = stopped;
        Ok(stopped)
    }
    pub fn finish(mut self, aborted: bool) -> Result<StreamResult, Error> {
        if let Some(error) = self.acc.failure {
            return Err(error);
        }
        if aborted {
            self.framer.close();
        } else {
            self.framer.finalize();
        }
        self.acc.malformed = self
            .acc
            .malformed
            .saturating_add(self.framer.drain_issues().count);
        let mut a = self.acc;
        let mut tools = Vec::new();
        for (index, call) in a.tools.calls {
            a.flood
                .observe_parts(
                    index as usize,
                    &call.name,
                    &call.arguments,
                    call.caller.as_ref(),
                    true,
                )
                .map_err(flood_error)?;
            if INTERNAL_TOOL_PREFIXES
                .iter()
                .any(|prefix| call.name.starts_with(prefix))
            {
                continue;
            }
            let caller = call.caller;
            let extra_content = call.extra_content;
            tools.push(json!({"id":call.id,"type":"function","function":{"name":call.name,"arguments":if crate::tool_policy::python_trim(&call.arguments).is_empty(){"{}".into()}else{call.arguments}}}));
            if let Some(extra) = extra_content {
                tools.last_mut().unwrap()["extra_content"] = extra;
            }
            if let Some(caller) = caller {
                tools.last_mut().unwrap()["caller"] = caller;
            }
        }
        let missing =
            matches!(a.finish.as_deref(), Some("tool_calls" | "tool_use")) && tools.is_empty();
        let empty = a.content.trim().is_empty() && tools.is_empty();
        let text = a.content.trim();
        let lowered = text.to_lowercase();
        let gateway = a.finish.as_deref() == Some("stop")
            && tools.is_empty()
            && text.chars().count() <= GATEWAY_LIMIT_MAX_CHARS
            && GATEWAY_LIMIT_PHRASES
                .iter()
                .any(|p| lowered.contains(p) || text.contains(p));
        let state = classify_stream(&StreamFacts {
            aborted,
            saw_finish_reason: a.finish.is_some(),
            malformed_frame_count: a.malformed,
            empty_response: empty,
            tool_payload_missing: missing,
            gateway_limit_response: gateway,
            ..Default::default()
        });
        let omit_content = a.content.is_empty() && !tools.is_empty();
        let mut message = json!({"role":"assistant","content":a.content});
        if omit_content {
            message.as_object_mut().unwrap().remove("content");
        }
        if !a.reasoning.is_empty() {
            message["reasoning_content"] = a.reasoning.into();
        }
        if !a.signature.is_empty() {
            message["thinking_signature"] = a.signature.into();
        }
        if !tools.is_empty() {
            message["tool_calls"] = tools.into();
        }
        Ok(StreamResult {
            state,
            message,
            usage: a.usage,
            finish_reason: a.finish,
            done_seen: a.done,
            malformed_frames: a.malformed,
        })
    }
}

fn flood_error(error: FloodError) -> Error {
    match error {
        FloodError::Flood { count, limit, .. } => Error::ToolFlood { count, limit },
        FloodError::PositionLimit => Error::ToolLimit,
        FloodError::InvalidBudget => Error::InvalidBudget,
        FloodError::ArgumentsTooDeep | FloodError::InvalidArguments => {
            Error::UnsupportedToolArguments
        }
    }
}

impl Error {
    pub fn task_error(self) -> Value {
        match self {
            Self::ToolFlood { count, limit } => {
                let mut envelope: Value = serde_json::from_str(TOOL_FLOOD_ENVELOPE_JSON)
                    .expect("generated flood envelope");
                envelope["identicalToolCalls"] = count.into();
                envelope["identicalToolCallLimit"] = limit.into();
                envelope
            }
            error => json!({"kind":"provider_stream","message":format!("{error:?}")}),
        }
    }
}

fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
