//! Native transient agent rounds over Task, provider and scoped tool authorities.
//! Composition supplies authorized providers, compiled tools, feedback projection
//! and explicit budgets. No routing, retries, durable conversation or compaction
//! policy is invented here; unsupported wire shapes fail before tool effects.
use crate::{
    execution_contract::ProviderStreamState,
    provider::StreamResult,
    session::{ExecutionSession, Registry as Sessions},
    task::{Task, TaskError, TerminalCandidate},
    tool_contract::{AdmissionError, Authorization, Catalog, DispatchError, Registry},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    io::{self, Write},
    sync::Arc,
};

#[derive(Clone, Copy)]
pub struct Budget {
    pub rounds: Option<usize>,
    pub tool_calls: Option<usize>,
    pub retained_bytes: usize,
    pub feedback_bytes: usize,
}
pub enum ProviderError {
    Stopped,
    Failed(Value),
}
pub trait Provider {
    /// One physical attempt. Stop must reach response quiescence before return.
    fn round(
        &self,
        messages: &[Value],
        session: &Arc<ExecutionSession>,
    ) -> Result<StreamResult, ProviderError>;
}
pub enum ToolFailure {
    Admission(AdmissionError),
    Dispatch(DispatchError),
    ApprovalDenied(Value),
}
pub trait Feedback {
    /// The owning tool-result policy produces model-facing text. No implicit
    /// Debug/error serialization or multimodal conversion is performed here.
    fn content(
        &self,
        result: Result<Value, ToolFailure>,
        byte_limit: usize,
    ) -> Result<String, Value>;
    /// Explicit model-facing projection. Existing text policies remain unchanged.
    fn model_content(&self,result:Result<Value,ToolFailure>,byte_limit:usize)->Result<Value,Value>{
        self.content(result,byte_limit).map(Value::String)
    }
}
/// Trusted composition supplies approval decisions; waiting adapters must wake
/// on parent cancellation/deadline and return only after their work quiesces.
pub enum ApprovalDecision {
    /// Ordinary authorization and the final receipt requirement still apply.
    NotRequired,
    Approved(crate::approval::Ticket),
    /// Owner policy supplies the rejection payload for Feedback projection.
    Denied(Value),
}
pub trait Approvals {
    fn decide(
        &self,
        session: &Arc<ExecutionSession>,
        call_id: &str,
        call: &crate::tool_contract::AdmittedCall,
    ) -> Result<ApprovalDecision, Value>;
}
pub struct Agent<'a> {
    pub provider: &'a dyn Provider,
    pub catalog: &'a Catalog,
    pub handlers: &'a Registry,
    pub authorization: &'a dyn Authorization,
    pub feedback: &'a dyn Feedback,
    pub sessions: &'a Sessions,
    pub epoch: &'a str,
    pub budget: Budget,
    pub round_admission: Option<&'a dyn crate::round_admission::Admission>,
    pub approvals: Option<&'a dyn Approvals>,
    pub checkpoints: Option<&'a dyn crate::agent_checkpoint::Port>,
    pub progress: Option<&'a dyn crate::progress::Policy>,
    pub scheduling: Option<&'a crate::tool_scheduler::Scheduler>,
}
fn error(kind: &str) -> Value {
    json!({"kind":kind,"message":"native agent composition refused the operation"})
}
fn stopped(session: &ExecutionSession) -> Result<bool, Value> {
    let snapshot = session.snapshot().map_err(|_| error("execution_state"))?;
    Ok(snapshot.cancel_requested || snapshot.deadline_remaining_nanos.is_some_and(|n| n <= 0))
}
struct Count {
    bytes: usize,
    limit: usize,
}
impl Write for Count {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(bytes.len())
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        if self.bytes > self.limit {
            return Err(io::Error::from(io::ErrorKind::InvalidInput));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn encoded_size(value: &impl serde_core::Serialize, limit: usize) -> Result<usize, Value> {
    let mut count = Count { bytes: 0, limit };
    serde_json::to_writer(&mut count, value).map_err(|_| error("agent_context_budget"))?;
    Ok(count.bytes)
}
fn append(
    values: &mut Vec<Value>,
    value: Value,
    retained: &mut usize,
    limit: usize,
) -> Result<(), Value> {
    let size = encoded_size(&value, limit.saturating_sub(*retained))?;
    *retained = retained
        .checked_add(size)
        .and_then(|v| v.checked_add(usize::from(!values.is_empty())))
        .filter(|v| *v <= limit)
        .ok_or_else(|| error("agent_context_budget"))?;
    values.push(value);
    Ok(())
}
struct Call {
    id: String,
    caller: Option<Value>,
    proof: Option<Result<crate::tool_contract::AdmittedCall, AdmissionError>>,
}
struct History {
    messages: Vec<Value>,
    usage: Vec<Value>,
    admitted: bool,
}
impl Agent<'_> {
    pub fn run(
        &self,
        task: &Task,
        owner: u64,
        task_id: &str,
        messages: Vec<Value>,
    ) -> Result<Arc<TerminalCandidate>, TaskError> {
        let session = task.execution_session(owner)?;
        let mut history = History { messages, usage: Vec::new(), admitted: false };
        task.run(owner, |_| self.drive(owner, task_id, &session, &mut history))
    }
    pub fn run_retained(
        &self, task: &Task, owner: u64, task_id: &str, messages: Vec<Value>,
    ) -> Result<Arc<TerminalCandidate>, TaskError> {
        let session = task.execution_session(owner)?;
        let mut history = History { messages, usage: Vec::new(), admitted: false };
        task.run_captured(owner, |_| {
            let output = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                self.drive(owner, task_id, &session, &mut history)))
                .unwrap_or_else(|_| Err(json!({"kind":"generic","message":"native task execution panicked"})));
            let execution = if history.admitted && (output.is_err() || output.as_ref().is_ok_and(Value::is_null)) {
                let mut value = json!({});
                value["messages"] = Value::Array(std::mem::take(&mut history.messages));
                value["usage"] = Value::Array(std::mem::take(&mut history.usage));
                Some(value)
            } else {None};
            (output, execution)
        })
    }
    fn drive(
        &self,
        owner: u64,
        task_id: &str,
        session: &Arc<ExecutionSession>,
        history: &mut History,
    ) -> Result<Value, Value> {
        let b = self.budget;
        if !session.belongs_to(owner, task_id)
            || !self.catalog.belongs_to(owner, self.epoch)
            || !self.handlers.belongs_to(owner, self.epoch)
            || b.rounds == Some(0)
            || [b.retained_bytes, b.feedback_bytes].contains(&0)
            || history.messages.iter().any(|v| !v.is_object())
        {
            return Err(error("agent_configuration"));
        }
        let progress_limits = self.progress.map(|p| p.limits()).unwrap_or_default();
        let progress_enabled = progress_limits.no_progress_rounds > 0 || progress_limits.nonretryable_failure_rounds > 0;
        let mut progress = crate::progress::Ledger::default();
        let History {messages, usage, admitted} = history;
        let mut tool_count = 0usize;
        let mut next_round = 0usize;
        let checkpoint_binding = self.checkpoints.map(|port|port.binding()).unwrap_or("");
        let identity = || crate::agent_checkpoint::Identity {owner,task_id,epoch:self.epoch,binding:checkpoint_binding,session};
        let finish_tool_round = |round: usize, tool_count: usize, messages: &[Value], usage: &[Value],
                                 progress: &mut crate::progress::Ledger, call_signatures: &[[u8; 32]]|
                                 -> Result<bool, Value> {
            if progress_enabled {
                if stopped(session)? { return Ok(true); }
                let observation = match self.progress.unwrap().observe(crate::progress::Context {
                    owner, task_id, round, tool_calls: call_signatures.len(), messages, session,
                }) {
                    Ok(observation) => observation,
                    Err(error) => {
                        if stopped(session)? { return Ok(true); }
                        return Err(error);
                    }
                };
                if stopped(session)? { return Ok(true); }
                if !observation.fits_bytes(b.feedback_bytes) { return Err(error("agent_progress_budget")); }
                if let Some(halt) = progress.observe(call_signatures, observation, progress_limits) {
                    return Err(json!({
                        "kind": match halt {crate::progress::Halt::NoProgress => "agent_no_progress", crate::progress::Halt::NonretryableFailure => "agent_nonretryable_tool_failure"},
                        "message": "native agent halted by trusted post-tool progress evidence",
                        "halted": true,
                        "noProgressStreak": progress.no_progress_streak,
                        "nonretryableFailureStreak": progress.nonretryable_failure_streak,
                    }));
                }
            }
            if let Some(port)=self.checkpoints {
                if stopped(session)? {return Ok(true);}
                let disposition=match port.complete(crate::agent_checkpoint::View {
                    identity:identity(),next_round:round+1,tool_calls:tool_count,
                    messages,usage,progress,budget:b,limits:progress_limits,model_account:None,deadline_unix_ms:None,
                }) {
                    Ok(value)=>value,
                    Err(error)=>{if stopped(session)? {return Ok(true);} return Err(error);}
                };
                if matches!(disposition,crate::agent_checkpoint::Disposition::Suspend) {
                    session.request_cancel("checkpoint_suspend").map_err(|_|error("execution_state"))?;
                    return Ok(true);
                }
            }
            Ok(false)
        };
        let mut pending_recovery = None;
        if let Some(port) = self.checkpoints {
            if checkpoint_binding.is_empty() {return Err(error("agent_checkpoint_binding"));}
            if stopped(session)? {return Ok(Value::Null);}
            let loaded=match port.load(identity()) {
                Ok(value)=>value,
                Err(error)=>{if stopped(session)? {return Ok(Value::Null);} return Err(error);}
            };
            if stopped(session)? {return Ok(Value::Null);}
            if let Some(state) = loaded {
                // Retain the bounded saved authority only on the recovery path.
                // Live execution and completed checkpoints allocate no extra copy.
                let pending = (state["tools_pending"] == true).then(|| state.clone());
                let state=crate::agent_checkpoint::restore(state,identity(),b,progress_limits)?;
                pending_recovery = if state.tools_pending { pending } else { None };
                next_round=state.next_round;tool_count=state.tool_calls;*messages=state.messages;*usage=state.usage;progress=state.progress;
            }
        }
        let mut usage_bytes = encoded_size(&usage,b.retained_bytes)?;
        let mut retained = encoded_size(&messages,b.retained_bytes.saturating_sub(usage_bytes))?
            .checked_add(usage_bytes).filter(|v|*v<=b.retained_bytes).ok_or_else(||error("agent_context_budget"))?;
        *admitted = true;
        if let Some(saved) = pending_recovery {
            let port = self.checkpoints.expect("pending restoration requires its checkpoint port");
            if stopped(session)? { return Ok(Value::Null); }
            let observations = match port.reconcile(identity(), &saved) {
                Ok(value) => value,
                Err(error) => { if stopped(session)? { return Ok(Value::Null); } return Err(error); }
            };
            if stopped(session)? { return Ok(Value::Null); }
            let calls = crate::agent_checkpoint::pending_calls(messages)
                .ok_or_else(|| error("agent_checkpoint_invalid"))?.to_vec();
            if observations.len() != calls.len() || observations.iter().zip(&calls)
                .any(|(result, call)| call["id"] != result.call_id) {
                return Err(error("agent_checkpoint_reconciliation"));
            }
            let mut signatures = Vec::new();
            for (call, observation) in calls.iter().zip(observations) {
                if progress_enabled {
                    let arguments = crate::tool_policy::lossless_arguments(call["function"]["arguments"].as_str().unwrap())
                        .map_err(|_| error("agent_tool_arguments"))?;
                    signatures.push(crate::progress::call_signature(call["function"]["name"].as_str().unwrap(), &arguments)
                        .map_err(|_| error("agent_tool_arguments"))?);
                }
                let content = observation.content;
                let bytes = match &content { Value::String(text) => text.len(),
                    _ => encoded_size(&content, b.feedback_bytes).map_err(|_| error("agent_feedback_budget"))? };
                if bytes > b.feedback_bytes { return Err(error("agent_feedback_budget")); }
                let mut feedback = json!({"role":"tool","tool_call_id":call["id"],"content":content});
                if let Some(caller) = call.get("caller") { feedback["caller"] = caller.clone(); }
                append(messages, feedback, &mut retained, b.retained_bytes)?;
            }
            // The same post-tool ledger and commit fence run before any new
            // model reservation. The paid round and its tools are never replayed.
            if finish_tool_round(next_round - 1, tool_count, messages, usage, &mut progress, &signatures)? {
                return Ok(Value::Null);
            }
        }
        let ledger = if self.round_admission.is_some() {
            Some(crate::round_admission::Ledger::bind(session)?)
        } else {None};
        for round in next_round..b.rounds.unwrap_or(usize::MAX) {
            if stopped(session)? {
                return Ok(Value::Null);
            }
            if let Some(port)=self.checkpoints {
                if let Err(error)=port.begin(identity(),round) {
                    if stopped(session)? {return Ok(Value::Null);} return Err(error);
                }
            }
            let mut permit = match self.round_admission {
                Some(admission) => match admission.reserve(crate::round_admission::Context {
                    owner,
                    task_id,
                    round,
                    session,
                    messages,
                }) {
                    Ok(permit) => Some(permit.attach(ledger.as_ref().unwrap())),
                    Err(error) => {
                        if stopped(session)? {
                            return Ok(Value::Null);
                        }
                        return Err(error);
                    }
                },
                None => None,
            };
            if let Some(prepared) = permit.as_mut().and_then(|p| p.take_messages()) {
                let size = if prepared.iter().all(Value::is_object) {
                    encoded_size(&prepared, b.retained_bytes.saturating_sub(usage_bytes))
                        .and_then(|size| size.checked_add(usage_bytes).ok_or_else(|| error("agent_context_budget")))
                } else { Err(error("agent_prepared_history")) };
                let size = match size {
                    Ok(size) => size,
                    Err(error) => {
                        if let Some(permit) = permit { permit.not_dispatched()?; }
                        return Err(error);
                    }
                };
                *messages = prepared;
                retained = size;
            }
            if stopped(session)? {
                if let Some(permit) = permit {
                    permit.not_dispatched()?;
                }
                return Ok(Value::Null);
            }
            let response = self.provider.round(messages, session);
            if let Some(permit) = permit {
                permit.settle(&response)?;
            }
            let response = match response {
                Ok(response) => response,
                Err(ProviderError::Stopped) => {
                    if !stopped(session)? {
                        session
                            .request_cancel("provider_stop")
                            .map_err(|_| error("execution_state"))?;
                    }
                    return Ok(Value::Null);
                }
                Err(ProviderError::Failed(error)) => return Err(error),
            };
            if stopped(session)? {
                return Ok(Value::Null);
            }
            if response.state != ProviderStreamState::ProviderFinished {
                return response.into_task_result();
            }
            if response.message["role"] != "assistant" {
                return Err(error("agent_provider_shape"));
            }
            let calls = match response.message.get("tool_calls") {
                None => &[][..],
                Some(Value::Array(calls)) => calls.as_slice(),
                _ => return Err(error("agent_tool_shape")),
            };
            tool_count = tool_count
                .checked_add(calls.len())
                .filter(|n| b.tool_calls.is_none_or(|limit| *n <= limit))
                .ok_or_else(|| error("agent_tool_budget"))?;
            // Preflight the complete provider batch before any physical effect.
            let mut ids = BTreeSet::new();
            let mut prepared = Vec::with_capacity(calls.len());
            let mut call_signatures = if progress_enabled { Vec::with_capacity(calls.len()) } else { Vec::new() };
            for call in calls {
                let id = call["id"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| error("agent_tool_shape"))?;
                let name = call["function"]["name"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| error("agent_tool_shape"))?;
                if call["type"] != "function" || !ids.insert(id) {
                    return Err(error("agent_tool_shape"));
                }
                let text = call["function"]["arguments"]
                    .as_str()
                    .ok_or_else(|| error("agent_tool_shape"))?;
                let arguments = crate::tool_policy::lossless_arguments(text)
                    .map_err(|_| error("agent_tool_arguments"))?;
                if progress_enabled {
                    call_signatures.push(crate::progress::call_signature(name, &arguments)
                        .map_err(|_| error("agent_tool_arguments"))?);
                }
                let caller = call.get("caller").cloned();
                let proof = self.catalog.admit(
                    owner,
                    name,
                    arguments,
                    caller.as_ref().unwrap_or(&Value::Null),
                    self.authorization,
                );
                prepared.push(Call {
                    id: id.into(),
                    caller,
                    proof: Some(proof),
                });
            }
            if !prepared.is_empty() && Some(round + 1) == b.rounds {
                return Err(error("agent_round_budget"));
            }
            append(
                messages,
                response.message,
                &mut retained,
                b.retained_bytes,
            )?;
            let before_usage = retained;
            append(usage, response.usage, &mut retained, b.retained_bytes)?;
            // Charge only the newly appended usage; never rescan older rounds.
            usage_bytes += retained - before_usage;
            if prepared.is_empty() {
                let mut result = json!({});
                result["messages"] = Value::Array(std::mem::take(messages));
                result["usage"] = Value::Array(std::mem::take(usage));
                result["llm_rounds"] = (round+1).into();
                result["tool_calls"] = tool_count.into();
                return Ok(result);
            }
            if stopped(session)? {
                return Ok(Value::Null);
            }
            if let Some(port) = self.checkpoints {
                if let Err(error) = port.pending(crate::agent_checkpoint::View {
                    identity:identity(), next_round:round+1, tool_calls:tool_count,
                    messages, usage, progress:&progress, budget:b, limits:progress_limits,
                    model_account:None, deadline_unix_ms:None,
                }) {
                    if stopped(session)? { return Ok(Value::Null); }
                    return Err(error);
                }
                if stopped(session)? { return Ok(Value::Null); }
            }
            let mut results: Vec<Option<Result<Value, ToolFailure>>> =
                (0..prepared.len()).map(|_| None).collect();
            let mut invocations = Vec::new();
            for (position, call) in prepared.iter_mut().enumerate() {
                match call.proof.take().unwrap() {
                    Err(error) => results[position] = Some(Err(ToolFailure::Admission(error))),
                    Ok(mut proof) => {
                        if let Some(approvals) = self.approvals {
                            let decision = approvals.decide(session, &call.id, &proof)?;
                            if stopped(session)? {
                                return Ok(Value::Null);
                            }
                            match decision {
                                ApprovalDecision::NotRequired => {}
                                ApprovalDecision::Approved(ticket) => {
                                    proof = proof
                                        .with_approval(&call.id, ticket)
                                        .map_err(|_| error("agent_tool_approval"))?;
                                }
                                ApprovalDecision::Denied(reason) => {
                                    encoded_size(&reason, b.feedback_bytes)
                                        .map_err(|_| error("agent_feedback_budget"))?;
                                    results[position] =
                                        Some(Err(ToolFailure::ApprovalDenied(reason)));
                                    continue;
                                }
                            }
                        }
                        invocations.push(crate::tool_scheduler::Invocation {
                            position, proof: proof.with_model_call_id(&call.id),
                        })
                    }
                }
            }
            let batch = crate::tool_scheduler::Batch {
                owner,
                epoch: self.epoch,
                task_id,
                round,
                parent: session.clone(),
                sessions: self.sessions,
                handlers: self.handlers,
                authorization: self.authorization,
                calls: invocations,
            };
            let dispatched = match self.scheduling {
                Some(scheduler) => scheduler.dispatch(batch),
                None => crate::tool_scheduler::ordered(batch),
            };
            let dispatched = match dispatched {
                Ok(results) => results,
                Err(_) => {
                    if stopped(session)? {
                        return Ok(Value::Null);
                    }
                    return Err(error("agent_tool_scheduler"));
                }
            };
            let mut outcomes = Vec::with_capacity(dispatched.len());
            for item in dispatched {
                let outcome = item.outcome.map_err(|_| error("agent_tool_scope"))?;
                if !outcome.receipt.invariants_satisfied {
                    return Err(error("agent_tool_cleanup"));
                }
                outcomes.push((item.position, outcome));
            }
            if stopped(session)? {
                return Ok(Value::Null);
            }
            if outcomes.iter().any(|(_, outcome)| {
                outcome.cancellation_requested
                    || outcome.deadline_exceeded
                    || outcome.parent_closing
            }) {
                if !stopped(session)? {
                    session
                        .request_cancel("tool_batch_stop")
                        .map_err(|_| error("execution_state"))?;
                }
                return Ok(Value::Null);
            }
            for (position, outcome) in outcomes {
                results[position] = Some(outcome.into_result().map_err(ToolFailure::Dispatch));
            }
            for (position, call) in prepared.into_iter().enumerate() {
                let result = results[position]
                    .take()
                    .ok_or_else(|| error("agent_tool_scope"))?;
                if stopped(session)? {
                    return Ok(Value::Null);
                }
                let content = self.feedback.model_content(result, b.feedback_bytes)?;
                let bytes=match &content {Value::String(text)=>text.len(),_=>encoded_size(&content,b.feedback_bytes).map_err(|_|error("agent_feedback_budget"))?};
                if bytes > b.feedback_bytes {
                    return Err(error("agent_feedback_budget"));
                }
                let mut feedback = json!({"role":"tool","tool_call_id":call.id,"content":content});
                if let Some(caller) = call.caller {
                    feedback["caller"] = caller;
                }
                append(messages, feedback, &mut retained, b.retained_bytes)?;
            }
            if finish_tool_round(round, tool_count, messages, usage, &mut progress, &call_signatures)? {
                return Ok(Value::Null);
            }
        }
        Err(error("agent_round_budget"))
    }
}
