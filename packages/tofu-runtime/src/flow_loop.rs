//! Transient producer/verifier loop decisions. Host owns effects and ledgers;
//! this state owns iteration/challenge/replan/checkpoint transitions, never I/O.
use crate::flow_graph::contract::{
    LOOP_BODY_FIELDS, LOOP_CONFIG_FIELDS, STATUS_INVALID_INPUT, STATUS_INVALID_STATE,
};
use serde_json::{json, Value};
use std::{cmp::Ordering, collections::BTreeSet};
#[path = "generated_loop_policy.rs"]
mod policy;
use policy::*;
#[derive(Clone, Copy, PartialEq, Eq)]
enum Stage {
    Start,
    Body,
    Replan,
    Done,
}
#[derive(Clone)]
pub struct State {
    pub body: Option<usize>,
    pub exit: Option<usize>,
    pub planner: Option<usize>,
    node_id: String,
    planner_id: Option<String>,
    window: u64,
    cap: u64,
    iteration: u64,
    zero: u64,
    replans: u64,
    producer_total: i64,
    stop_challenged: bool,
    blocked_challenged: bool,
    strategy_nudged: bool,
    checkpoints: u64,
    checkpoint_sc: i64,
    checkpoint_resolved: Option<String>,
    replan_exhausted: bool,
    extend: bool,
    stage: Stage,
}
fn text<'a>(v: &'a Value, k: &str) -> Result<&'a str, u32> {
    v[k].as_str().ok_or(STATUS_INVALID_INPUT)
}
fn number(v: &Value, k: &str) -> Result<u64, u32> {
    v[k].as_u64().ok_or(STATUS_INVALID_INPUT)
}
fn boolean(v: &Value, k: &str) -> Result<bool, u32> {
    v[k].as_bool().ok_or(STATUS_INVALID_INPUT)
}
fn decimal(v: &Value) -> Result<Option<String>, u32> {
    if v.is_null() {
        return Ok(None);
    }
    let s = v.as_str().ok_or(STATUS_INVALID_INPUT)?;
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return Err(STATUS_INVALID_INPUT);
    }
    let normalized = s.trim_start_matches('0');
    Ok(Some(if normalized.is_empty() {
        "0".into()
    } else {
        normalized.into()
    }))
}
fn compare(a: &str, b: &str) -> Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}
fn subtract(a: &str, b: &str) -> String {
    if compare(a, b) == Ordering::Less {
        return format!("-{}", subtract(b, a));
    }
    let mut out = Vec::with_capacity(a.len());
    let mut borrow = 0i16;
    let mut other = b.bytes().rev();
    for digit in a.bytes().rev() {
        let mut n = (digit - b'0') as i16 - borrow - other.next().map_or(0, |d| (d - b'0') as i16);
        borrow = if n < 0 {
            n += 10;
            1
        } else {
            0
        };
        out.push(b'0' + n as u8)
    }
    out.reverse();
    let s = String::from_utf8(out).unwrap();
    let clean = s.trim_start_matches('0');
    if clean.is_empty() {
        "0".into()
    } else {
        clean.into()
    }
}
fn words(text: &str) -> BTreeSet<String> {
    crate::verdict_policy::lower(text)
        .split(crate::verdict_policy::space)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}
fn similarity(a: &BTreeSet<String>, b: &BTreeSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    a.intersection(b).count() as f64 / a.union(b).count() as f64
}
fn stuck(history: &[Value], latest: &str, vu: bool) -> Result<bool, u32> {
    let window = if vu {
        AUTOPILOT_STUCK_WINDOW as usize
    } else {
        2
    };
    if history.len() + 1 < window {
        return Ok(false);
    }
    let mut sets = history[history.len() + 1 - window..]
        .iter()
        .map(|v| v.as_str().map(words).ok_or(STATUS_INVALID_INPUT))
        .collect::<Result<Vec<_>, _>>()?;
    sets.push(words(latest));
    Ok(sets
        .windows(2)
        .all(|pair| similarity(&pair[0], &pair[1]) > STUCK_JACCARD))
}
fn diminishing(tail: &[Value], entry: &Value, window: u64) -> Result<bool, u32> {
    if window <= 1 || tail.len() as u64 + 1 < window {
        return Ok(false);
    }
    let mut rows: Vec<&Value> = tail[tail.len() + 1 - window as usize..].iter().collect();
    rows.push(entry);
    let mut sets = Vec::new();
    for row in rows {
        let delta = decimal(&row["resolved_delta"])?;
        if delta.as_deref() != Some("0") {
            return Ok(false);
        }
        let names = row["targets"].as_array().ok_or(STATUS_INVALID_INPUT)?;
        let set = names
            .iter()
            .map(|v| v.as_str().map(str::to_owned).ok_or(STATUS_INVALID_INPUT))
            .collect::<Result<BTreeSet<_>, _>>()?;
        if set.is_empty() {
            return Ok(false);
        }
        sets.push(set);
    }
    Ok(sets
        .windows(2)
        .all(|pair| similarity(&pair[0], &pair[1]) >= DIMINISHING_TARGET_OVERLAP))
}
impl State {
    pub fn new(
        node_id: String,
        body: Option<usize>,
        exit: Option<usize>,
        planner: Option<(usize, String)>,
        config: &Value,
    ) -> Result<Self, u32> {
        if config.as_object().is_none_or(|v| {
            v.len() != LOOP_CONFIG_FIELDS.len()
                || LOOP_CONFIG_FIELDS.iter().any(|key| !v.contains_key(*key))
        }) {
            return Err(STATUS_INVALID_INPUT);
        }
        let window = number(config, "max_iterations")?
            .min(number(config, "configured_cap")?)
            .max(1);
        let extend = match text(config, "budget_policy")? {
            "bounded" => false,
            "checkpoint_extend" => true,
            _ => return Err(STATUS_INVALID_INPUT),
        };
        Ok(Self {
            body,
            exit,
            planner: planner.as_ref().map(|p| p.0),
            planner_id: planner.map(|p| p.1),
            node_id,
            window,
            cap: window,
            iteration: 0,
            zero: 0,
            replans: 0,
            producer_total: 0,
            stop_challenged: false,
            blocked_challenged: false,
            strategy_nudged: false,
            checkpoints: 0,
            checkpoint_sc: 0,
            checkpoint_resolved: None,
            replan_exhausted: false,
            extend,
            stage: Stage::Start,
        })
    }
    pub fn metadata(&self) -> Value {
        json!({"body":self.body,"exit":self.exit,"planner":self.planner,"window":self.window})
    }
    fn event(&self, effects: &mut Vec<Value>, kind: &str, mut fields: Value) {
        fields["type"] = kind.into();
        fields["node_id"] = self.node_id.clone().into();
        effects.push(json!({"kind":"event","event":fields}));
    }
    fn directive(effects: &mut Vec<Value>, text: &str) {
        effects.push(json!({"kind":"directive","text":text}));
    }
    fn next_body(&mut self, effects: &mut Vec<Value>) -> Result<&'static str, u32> {
        if self.iteration >= self.cap {
            self.stage = Stage::Done;
            return Ok("halt");
        }
        self.iteration = self.iteration.checked_add(1).ok_or(STATUS_INVALID_STATE)?;
        self.stage = Stage::Body;
        self.event(
            effects,
            "loop_iteration",
            json!({"iteration":self.iteration,"max":self.cap}),
        );
        Ok("body")
    }
    pub fn advance(&mut self, input: &Value) -> Result<Value, u32> {
        self.advance_mode(input, false)
    }
    pub fn advance_text(&mut self, input: &Value) -> Result<Value, u32> {
        self.advance_mode(input, true)
    }
    fn advance_mode(&mut self, input: &Value, native_text: bool) -> Result<Value, u32> {
        let operation = text(input, "operation")?;
        let fields: &[&str] = if operation == "body_done" {
            LOOP_BODY_FIELDS
        } else {
            &["operation", "iteration"]
        };
        if input.as_object().is_none_or(|v| {
            v.len() != fields.len() || fields.iter().any(|key| !v.contains_key(*key))
        }) {
            return Err(STATUS_INVALID_INPUT);
        }
        if number(input, "iteration")? != self.iteration {
            return Err(STATUS_INVALID_STATE);
        }
        if operation == "body_done" {
            for key in ["digit_limit", "verifier_tool_rounds", "progress_window"] {
                number(input, key)?;
            }
            if native_text {
                if !input["justification_chars"].is_null() {
                    return Err(STATUS_INVALID_INPUT);
                }
            } else {
                number(input, "justification_chars")?;
            }
            boolean(input, "producer_reported")?;
            boolean(input, "replan_enabled")?;
            text(input, "verifier_output")?;
            text(input, "verifier_role")?;
            decimal(&input["previous_resolved"])?;
            for key in ["history_tail", "producer_names"] {
                let items = input[key].as_array().ok_or(STATUS_INVALID_INPUT)?;
                if items.iter().any(|v| v.as_str().is_none()) {
                    return Err(STATUS_INVALID_INPUT);
                }
            }
            for row in input["progress_tail"]
                .as_array()
                .ok_or(STATUS_INVALID_INPUT)?
            {
                if row.as_object().is_none_or(|v| {
                    v.len() != 2 || !v.contains_key("resolved_delta") || !v.contains_key("targets")
                }) {
                    return Err(STATUS_INVALID_INPUT);
                }
                decimal(&row["resolved_delta"])?;
                if row["targets"]
                    .as_array()
                    .is_none_or(|items| items.iter().any(|v| v.as_str().is_none()))
                {
                    return Err(STATUS_INVALID_INPUT);
                }
            }
        }
        let mut effects = Vec::new();
        let mut classification = Value::Null;
        let mut progress = Value::Null;
        let mut append = false;
        let mut defect = Value::Null;
        let mut exit_reason = if self.replan_exhausted {
            "replan_exhausted"
        } else {
            "max_iterations"
        };
        let action = match (self.stage, operation) {
            (Stage::Start, "start") | (Stage::Replan, "replan_done") => {
                self.next_body(&mut effects)?
            }
            (Stage::Body, "body_done") => {
                let count = input["producer_count"]
                    .as_i64()
                    .ok_or(STATUS_INVALID_INPUT)?;
                self.producer_total = self
                    .producer_total
                    .checked_add(count)
                    .ok_or(STATUS_INVALID_STATE)?;
                self.zero = if boolean(input, "producer_reported")? && count <= 0 {
                    self.zero.checked_add(1).ok_or(STATUS_INVALID_STATE)?
                } else {
                    0
                };
                if self.zero >= MAX_ZERO_DELIVERABLE_TURNS && self.iteration < self.cap {
                    Self::directive(&mut effects, ZERO_DELIVERABLE_DIRECTIVE);
                    self.event(
                        &mut effects,
                        "zero_deliverable_guard",
                        json!({"iteration":self.iteration,"streak":self.zero}),
                    );
                    self.zero = 0;
                    self.next_body(&mut effects)?
                } else {
                    let output = text(input, "verifier_output")?;
                    let vu = text(input, "verifier_role")? == "virtual_user";
                    classification = crate::verdict_policy::classify(
                        output,
                        vu,
                        true,
                        false,
                        boolean(input, "replan_enabled")?,
                        number(input, "digit_limit")? as usize,
                    )
                    .map_err(|_| STATUS_INVALID_INPUT)?;
                    append = true;
                    let mut phase = classification["phase"]
                        .as_str()
                        .ok_or(STATUS_INVALID_INPUT)?
                        .to_owned();
                    defect = classification["plan_defect"].clone();
                    let previous = decimal(&input["previous_resolved"])?;
                    let mut resolved_now = previous.clone();
                    if vu {
                        let (resolved, remaining) = crate::verdict_policy::progress(
                            output,
                            number(input, "digit_limit")? as usize,
                        )
                        .map_err(|_| STATUS_INVALID_INPUT)?;
                        let delta = resolved.as_ref().map(|r| {
                            let d = subtract(r, previous.as_deref().unwrap_or("0"));
                            if d.starts_with('-') {
                                "0".into()
                            } else {
                                d
                            }
                        });
                        resolved_now = resolved.or(previous);
                        let names = input["producer_names"]
                            .as_array()
                            .ok_or(STATUS_INVALID_INPUT)?
                            .iter()
                            .map(|v| v.as_str().map(str::to_owned).ok_or(STATUS_INVALID_INPUT))
                            .collect::<Result<BTreeSet<_>, _>>()?;
                        progress = json!({"resolved_delta":delta,"cum_resolved":resolved_now,"targets":names});
                        let tools = number(input, "verifier_tool_rounds")?;
                        let mut rejection = None;
                        if phase == "stop" {
                            match remaining.as_deref() {
                                None => {
                                    Self::directive(
                                        &mut effects,
                                        GOAL_COMPLETION_EVIDENCE_DIRECTIVE,
                                    );
                                    self.event(
                                        &mut effects,
                                        "goal_completion_evidence_missing",
                                        json!({"iteration":self.iteration}),
                                    );
                                    phase = "worker".into();
                                }
                                Some(n) if n != "0" => {
                                    Self::directive(
                                        &mut effects,
                                        &GOAL_REMAINING_DIRECTIVE.replace("{remaining}", n),
                                    );
                                    rejection = Some("remaining");
                                }
                                _ if !self.stop_challenged
                                    && self.producer_total > 0
                                    && tools == 0 =>
                                {
                                    self.stop_challenged = true;
                                    Self::directive(
                                        &mut effects,
                                        GOAL_STOP_VERIFY_CHALLENGE_DIRECTIVE,
                                    );
                                    rejection = Some("unverified");
                                }
                                _ if !self.stop_challenged
                                    && resolved_now.as_deref().is_none_or(|v| v == "0")
                                    && self.producer_total == 0
                                    && tools == 0 =>
                                {
                                    self.stop_challenged = true;
                                    Self::directive(
                                        &mut effects,
                                        GOAL_STOP_VACUOUS_CHALLENGE_DIRECTIVE,
                                    );
                                    rejection = Some("vacuous");
                                }
                                _ => {}
                            }
                            if let Some(reason) = rejection {
                                phase = "worker".into();
                                let mut fields =
                                    json!({"iteration":self.iteration,"reason":reason});
                                if reason == "remaining" {
                                    fields["remaining_decimal"] = remaining.clone().into();
                                }
                                self.event(&mut effects, "goal_stop_rejected", fields);
                            }
                        } else if phase == "blocked" {
                            let reason = match remaining.as_deref() {
                                None => {
                                    Some(("evidence_missing", GOAL_COMPLETION_EVIDENCE_DIRECTIVE))
                                }
                                Some("0") => {
                                    Some(("remaining_zero", GOAL_BLOCKED_REMAINING_DIRECTIVE))
                                }
                                _ if (if native_text {
                                    crate::verdict_policy::justification_chars(output)
                                        .map_err(|_| STATUS_INVALID_INPUT)?
                                } else {
                                    number(input, "justification_chars")?
                                }) < GOAL_BLOCKED_MIN_JUSTIFICATION_CHARS =>
                                {
                                    Some(("vacuous", GOAL_BLOCKED_JUSTIFICATION_DIRECTIVE))
                                }
                                _ if !self.blocked_challenged => {
                                    self.blocked_challenged = true;
                                    Some(("challenged", GOAL_BLOCKED_CHALLENGE_DIRECTIVE))
                                }
                                _ => None,
                            };
                            if let Some((reason, directive)) = reason {
                                phase = "worker".into();
                                Self::directive(&mut effects, directive);
                                self.event(
                                    &mut effects,
                                    "goal_blocked_rejected",
                                    json!({"iteration":self.iteration,"reason":reason}),
                                );
                            } else {
                                exit_reason = "vu_blocked";
                            }
                        }
                    }
                    if phase == "stop" || exit_reason == "vu_blocked" {
                        if phase == "stop" {
                            exit_reason = "stop"
                        }
                        self.stage = Stage::Done;
                        "halt"
                    } else {
                        if !self.strategy_nudged && self.iteration < self.cap {
                            let history = input["history_tail"]
                                .as_array()
                                .ok_or(STATUS_INVALID_INPUT)?;
                            if stuck(history, output, vu)? {
                                self.strategy_nudged = true;
                                Self::directive(&mut effects, REPEATED_FEEDBACK_DIRECTIVE);
                                self.event(
                                    &mut effects,
                                    "stuck_detected",
                                    json!({"iteration":self.iteration,"action":"strategy_nudge"}),
                                );
                            }
                        }
                        if vu && !self.strategy_nudged && self.iteration < self.cap {
                            let window = number(input, "progress_window")?;
                            let tail = input["progress_tail"]
                                .as_array()
                                .ok_or(STATUS_INVALID_INPUT)?;
                            if diminishing(tail, &progress, window)? {
                                self.strategy_nudged = true;
                                Self::directive(&mut effects, DIMINISHING_RETURNS_DIRECTIVE);
                                self.event(&mut effects,"no_progress",json!({"iteration":self.iteration,"window":window,"action":"strategy_nudge"}));
                            }
                        }
                        if phase == "planner"
                            && self.planner.is_some()
                            && self.replans < MAX_REPLANS
                            && self.iteration < self.cap
                        {
                            self.replans += 1;
                            self.event(&mut effects,"replan",json!({"planner":self.planner_id,"replan":self.replans,"defect":defect.as_str().unwrap_or("").chars().take(200).collect::<String>()}));
                            self.stage = Stage::Replan;
                            "replan"
                        } else {
                            if phase == "planner" && self.replans >= MAX_REPLANS {
                                self.replan_exhausted = true;
                                exit_reason = "replan_exhausted";
                            }
                            if self.extend && self.iteration == self.cap {
                                let sc = self
                                    .producer_total
                                    .checked_sub(self.checkpoint_sc)
                                    .ok_or(STATUS_INVALID_STATE)?;
                                let rd = resolved_now.as_ref().map(|r| {
                                    subtract(r, self.checkpoint_resolved.as_deref().unwrap_or("0"))
                                });
                                if sc > 0
                                    || rd
                                        .as_deref()
                                        .is_some_and(|r| r != "0" && !r.starts_with('-'))
                                {
                                    self.checkpoints = self
                                        .checkpoints
                                        .checked_add(1)
                                        .ok_or(STATUS_INVALID_STATE)?;
                                    self.checkpoint_sc = self.producer_total;
                                    if let Some(now) = resolved_now {
                                        if self.checkpoint_resolved.as_deref().is_none_or(|old| {
                                            compare(&now, old) == Ordering::Greater
                                        }) {
                                            self.checkpoint_resolved = Some(now)
                                        }
                                    }
                                    self.cap = self
                                        .cap
                                        .checked_add(self.window)
                                        .ok_or(STATUS_INVALID_STATE)?;
                                    Self::directive(&mut effects, GOAL_BUDGET_CHECKPOINT_DIRECTIVE);
                                    self.event(&mut effects,"goal_budget_checkpoint",json!({"iteration":self.iteration,"action":"extend","window":self.window,"new_max":self.cap,"state_changing_delta":sc,"resolved_delta_decimal":rd,"checkpoint":self.checkpoints}));
                                } else {
                                    self.event(&mut effects,"goal_budget_checkpoint",json!({"iteration":self.iteration,"action":"stalled","window":self.window,"state_changing_delta":sc,"resolved_delta_decimal":rd}));
                                    exit_reason = "stalled";
                                    self.stage = Stage::Done;
                                }
                            }
                            if self.stage == Stage::Done {
                                "halt"
                            } else {
                                self.next_body(&mut effects)?
                            }
                        }
                    }
                }
            }
            _ => return Err(STATUS_INVALID_STATE),
        };
        Ok(
            json!({"action":action,"iteration":self.iteration,"cap":self.cap,"effects":effects,"classification":classification,"progress":progress,"append_feedback":append,"defect":defect,"replan":self.replans,"exit_reason":exit_reason}),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn progress_arithmetic_preserves_large_signed_differences() {
        let high = "9".repeat(4000);
        let difference = subtract(&high, "1");
        assert_eq!(difference.len(), 4000);
        assert!(difference.ends_with('8'));
        assert_eq!(subtract("1", &high), format!("-{difference}"));
        assert_eq!(subtract(&high, &high), "0");
        assert_eq!(
            subtract("1000000000000000000000000", "999999999999999999999999"),
            "1"
        );
    }
}
