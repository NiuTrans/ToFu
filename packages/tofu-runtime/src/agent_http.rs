//! Native authorized HTTP provider port for agent rounds. Borrows message
//! carriers while serializing; no full-history Value clone, routing or retries.
use crate::{
    agent::{Provider, ProviderError},
    http::{Cancellation, Error, Request, Transport},
    provider::{Budget, StreamResult},
    session::ExecutionSession,
};
use serde_core::ser::SerializeMap;
use serde_json::{json, Map, Value};
use std::{
    io::{self, Write},
    sync::Arc,
    time::Duration,
};
pub struct Configuration {
    pub headers: reqwest::header::HeaderMap,
    pub template: Map<String, Value>,
    pub budget: Budget,
    pub request_bytes: usize,
    /// Retired v1 input: accepted/validated for compatibility; never removes calls.
    pub identical_tool_limit: Option<usize>,
}
pub struct HttpProvider<'a> {
    runtime: &'a tokio::runtime::Runtime,
    transport: &'a Transport,
    configuration: Configuration,
    tool_names: Arc<std::collections::BTreeSet<String>>,
    cancellation: &'a Cancellation,
    text_observer: Option<Arc<dyn crate::http::TextObserver>>,
    anthropic: Option<crate::anthropic::Budget>,
    responses: Option<(crate::responses::Budget, bool, crate::responses_request::Profile)>,
    preserve_chat_text_parts: bool,
    prepared_tools: Option<Arc<crate::prepared_tool_wire::PreparedToolWire>>,
}
impl<'a> HttpProvider<'a> {
    pub fn new(
        runtime: &'a tokio::runtime::Runtime,
        transport: &'a Transport,
        configuration: Configuration,
        cancellation: &'a Cancellation,
    ) -> Result<Self, Value> {
        let c = &configuration;
        if c.request_bytes == 0
            || c.template.contains_key("messages")
            || c.template
                .get("stream")
                .is_some_and(|v| v != &Value::Bool(true))
            || c.budget.event_bytes == 0
            || c.budget.output_bytes == 0
            || c.budget.tool_calls == 0
            || !(crate::execution_contract::MIN_IDENTICAL_TOOL_CALLS
                ..=crate::execution_contract::MAX_IDENTICAL_TOOL_CALLS)
                .contains(
                    &c.identical_tool_limit
                        .unwrap_or(crate::execution_contract::DEFAULT_IDENTICAL_TOOL_CALLS),
                )
        {
            return Err(
                json!({"kind":"agent_http_configuration","message":"native provider configuration refused"}),
            );
        }
        let tool_names = Arc::new(crate::tool_assembly::wire_tool_names(
            c.template.get("tools").unwrap_or(&Value::Null),
        ));
        Ok(Self {
            runtime,
            transport,
            configuration,
            tool_names,
            cancellation,
            text_observer: None,
            anthropic: None,
            responses: None,
            preserve_chat_text_parts: true,
            prepared_tools: None,
        })
    }
    pub fn with_anthropic(mut self, budget: crate::anthropic::Budget) -> Result<Self, Value> {
        crate::anthropic::Translator::new(budget,Default::default())
            .map_err(|_|json!({"kind":"agent_http_configuration","message":"native Anthropic budget refused"}))?;
        self.responses = None;
        self.anthropic = Some(budget);
        Ok(self)
    }
    pub fn with_responses(
        self,
        budget: crate::responses::Budget,
        replay: bool,
    ) -> Result<Self, Value> {
        self.with_responses_profile(budget, replay, crate::responses_request::Profile::Compatible)
    }
    pub fn with_responses_profile(
        mut self,
        budget: crate::responses::Budget,
        replay: bool,
        profile: crate::responses_request::Profile,
    ) -> Result<Self, Value> {
        crate::responses::Translator::new(budget,Default::default()).map_err(|_|json!({"kind":"agent_http_configuration","message":"native Responses budget refused"}))?;
        self.anthropic = None;
        self.responses = Some((budget, replay, profile));
        Ok(self)
    }
    async fn execute(
        &self,
        request: Request,
        cancel: &Cancellation,
        observer: Option<&dyn crate::http::TextObserver>,
        reverse: std::collections::BTreeMap<String, String>,
    ) -> Result<StreamResult, Error> {
        if let Some((budget, replay, _)) = self.responses {
            let response = self
                .transport
                .execute_responses_with_text(request, cancel, observer, budget, reverse)
                .await?;
            if response
                .message
                .get("_responses_items")
                .and_then(Value::as_array)
                .is_some_and(|items| {
                    items.iter().any(|item| match item["type"].as_str() {
                        Some("message") => false,
                        Some("reasoning") => {
                            !replay
                                && item
                                    .get("encrypted_content")
                                    .is_some_and(|v| v.as_str().is_some_and(|s| !s.is_empty()))
                        }
                        _ => true,
                    })
                })
            {
                return Err(Error::UnsupportedProviderProfile);
            }
            return Ok(response);
        }
        match self.anthropic {
            Some(budget) => {
                self.transport
                    .execute_anthropic_with_text(request, cancel, observer, budget)
                    .await
            }
            None => {
                self.transport
                    .execute_with_text(request, cancel, observer)
                    .await
            }
        }
    }
    pub fn with_text_observer(mut self, observer: Arc<dyn crate::http::TextObserver>) -> Self {
        self.text_observer = Some(observer);
        self
    }
    pub fn with_chat_text_parts(mut self, preserve: bool) -> Self {
        self.preserve_chat_text_parts = preserve;
        self
    }
}
struct Body<'a> {
    template: &'a Map<String, Value>,
    messages: &'a [Value],
    output: Option<(&'a str, u64)>,
    preserve_text_parts: bool,
}
impl serde_core::Serialize for Body<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(None)?;
        for (key, value) in self.template {
            if key != "messages"
                && key != "stream"
                && !self.output.is_some_and(|(field, _)| field == key)
            {
                map.serialize_entry(key, value)?;
            }
        }
        if let Some((field, limit)) = self.output {
            map.serialize_entry(field, &limit)?;
        }
        map.serialize_entry("messages", &crate::chat_wire::Messages(self.messages, self.preserve_text_parts))?;
        map.serialize_entry("stream", &true)?;
        map.end()
    }
}
struct Bytes {
    value: Vec<u8>,
    limit: usize,
}
impl Write for Bytes {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.value.len()) {
            return Err(io::Error::from(io::ErrorKind::InvalidInput));
        }
        self.value.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
impl HttpProvider<'_> {
    pub fn with_prepared_tools(mut self, state: Arc<crate::prepared_tool_wire::PreparedToolWire>, names: std::collections::BTreeSet<String>) -> Self {
        self.prepared_tools=Some(state);
        self.tool_names=Arc::new(names);
        self
    }
    fn round_inner(
        &self,
        template: &Map<String, Value>,
        messages: &[Value],
        session: &Arc<ExecutionSession>,
        output: Option<(&str, u64)>,
        lease: Option<&mut SelectedLease>,
    ) -> Result<StreamResult, ProviderError> {
        let failure = |kind| {
            ProviderError::Failed(json!({"kind":kind,"message":"native provider round refused"}))
        };
        let c = &self.configuration;
        let mut body = Bytes {
            value: Vec::new(),
            limit: c.request_bytes,
        };
        let reverse = if self.responses.is_some() {
            crate::responses_request::aliases(template, messages)
                .map_err(|_| failure("agent_responses_alias_refused"))?
        } else {
            Default::default()
        };
        if let Some((_, replay, profile)) = self.responses {
            serde_json::to_writer(
                &mut body,
                &crate::responses_request::Body {
                    profile,
                    template,
                    messages,
                    output: output.map(|(_, n)| n),
                    replay,
                    reverse: &reverse,
                },
            )
            .map_err(|_| failure("agent_responses_request_refused"))?;
        } else if self.anthropic.is_some() {
            if output.is_some_and(|(field, _)| field != "max_tokens") {
                return Err(failure("agent_anthropic_output_field"));
            }
            serde_json::to_writer(
                &mut body,
                &crate::anthropic_request::Body {
                    template,
                    messages,
                    output: output.map(|(_, n)| n),
                },
            )
            .map_err(|_| failure("agent_anthropic_request_refused"))?;
        } else {
            serde_json::to_writer(
                &mut body,
                &Body {
                    template,
                    messages,
                    output,
                    preserve_text_parts: self.preserve_chat_text_parts,
                },
            )
            .map_err(|error| failure(if error.is_io() { "agent_request_budget" }
                else { "agent_chat_request_refused" }))?;
        }
        let prepared_headers = if let Some(lease) = lease {
            lease.mark_dispatched(session).map_err(|error| {
                if stop_now(session) { ProviderError::Stopped }
                else { ProviderError::Failed(error) }
            })?
        } else { None };
        let headers = prepared_headers.unwrap_or_else(|| c.headers.clone());
        let request_bytes = body.value.len();
        let request = Request {
            headers,
            body: body.value,
            budget: c.budget,
            identical_tool_limit: c.identical_tool_limit,
            tool_names: self.tool_names.clone(),
        };
        let snapshot = session.snapshot().map_err(|_| failure("execution_state"))?;
        let deadline = async {
            if let Some(nanos) = snapshot.deadline_remaining_nanos {
                if nanos <= 0 {
                    return;
                }
                let nanos = nanos as u128;
                let duration = Duration::new(
                    (nanos / 1_000_000_000).min(u64::MAX as u128) as u64,
                    (nanos % 1_000_000_000) as u32,
                );
                if let Some(at) = tokio::time::Instant::now().checked_add(duration) {
                    tokio::time::sleep_until(at).await;
                } else {
                    std::future::pending::<()>().await;
                }
            } else {
                std::future::pending::<()>().await;
            }
        };
        let observer = session.stop_observer();
        let result = self.runtime.block_on(async {
            if let Some(text) = self.text_observer.as_deref() {
                let cancel = Cancellation::new();
                let transfer = self.execute(request, &cancel, Some(text), reverse);
                tokio::pin!(transfer);
                tokio::select! {
                    biased;
                    _ = observer.stopped() => { cancel.cancel(); transfer.await },
                    _ = deadline => { cancel.cancel(); transfer.await },
                    _ = self.cancellation.cancelled() => { cancel.cancel(); transfer.await },
                    result = &mut transfer => result,
                }
            } else {
                tokio::select! {
                    biased;
                    _ = observer.stopped() => Err(Error::Cancelled),
                    _ = deadline => Err(Error::Cancelled),
                    result = self.execute(request, self.cancellation, None, reverse) => result,
                }
            }
        });
        match result {
            // A final text consumer can publish Stop during the transfer's
            // winning poll, after select checked the stop branches. The
            // transfer has joined its consumer and released HTTP before this
            // publication check; retain transport/provider failures below.
            Ok(_) if stop_now(session) || self.cancellation.is_cancelled() =>
                Err(ProviderError::Stopped),
            Ok(response) => Ok(response),
            Err(Error::Cancelled) => Err(ProviderError::Stopped),
            Err(Error::WriteTimeout) => Err(ProviderError::Failed(json!({
                "kind":"provider_transport", "message":"Request upload timed out",
                "failureStage":"upload", "transportReason":"timeout", "transportCause":"WriteTimeout",
                "requestBytes":request_bytes,
                "timeoutSeconds":self.transport.write_timeout().map(|value| value.as_secs_f64())
            }))),
            Err(Error::Provider(error)) => Err(ProviderError::Failed(error.task_error())),
            Err(Error::UnsupportedProviderProfile) => Err(ProviderError::Failed(json!({"kind":"agent_responses_profile_refused"}))),
            Err(Error::ProviderRejected(error)) => Err(ProviderError::Failed(
                json!({"kind":"provider_rejection","provider":error}),
            )),
            Err(error) => Err(ProviderError::Failed(
                json!({"kind":"provider_transport","message":format!("{error:?}")}),
            )),
        }
    }
}

impl Provider for HttpProvider<'_> {
    fn round(
        &self,
        messages: &[Value],
        session: &Arc<ExecutionSession>,
    ) -> Result<StreamResult, ProviderError> {
        let prepared=self.prepared_tools.as_ref().map(|state|state.template()).transpose()
            .map_err(|error|ProviderError::Failed(json!({"kind":"model_projection","message":error})))?;
        let template=prepared.as_deref().unwrap_or(&self.configuration.template);
        self.round_inner(template, messages, session, None, None)
    }
}
/// Trusted model ingress counter; count the frozen messages/tools, never hints.
pub trait InputCounter: Send + Sync {
    fn tokens(
        &self,
        owner: u64,
        task: &str,
        messages: &[Value],
        template: &Map<String, Value>,
    ) -> Result<u64, Value>;
}
#[derive(Clone, Copy)]
pub enum OutputField {
    MaxTokens,
    MaxCompletionTokens,
}
impl OutputField {
    fn key(self) -> &'static str {
        match self {
            Self::MaxTokens => "max_tokens",
            Self::MaxCompletionTokens => "max_completion_tokens",
        }
    }
}
// Select one authority. Native leases stay inline and allocate no adapter box.
enum SelectedAdmission {
    Native(Arc<crate::model_account::Account>),
    External(Arc<dyn crate::model_admission::Admission>),
}
enum SelectedLease {
    Native(crate::model_account::Lease),
    External { lease: Option<Box<dyn crate::model_admission::Lease>>, dispatch_attempted: bool },
}
fn external_callback<T>(call: impl FnOnce() -> Result<T, Value>) -> Result<T, Value> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(call))
        .unwrap_or_else(|_| Err(json!({"kind":"model_admission_callback"})))
}
impl SelectedAdmission {
    fn authorize(&self, owner: u64, task: &str) -> Result<(), Value> {
        match self {
            Self::Native(account) => account.authorize(owner, task)
                .map_err(|_| json!({"kind":"model_account_owner"})),
            Self::External(port) => external_callback(|| port.authorize(owner, task)),
        }
    }
    fn reserve(&self, owner: u64, task: &str, counts: crate::model_account::Counts,
               session: &Arc<ExecutionSession>, runtime: &tokio::runtime::Runtime) -> Result<SelectedLease, Value> {
        match self {
            Self::Native(account) => runtime.block_on(account.reserve(owner, task, counts, account_stop(session)))
                .map(SelectedLease::Native).map_err(|error| json!({"kind":match error {
                    crate::model_account::Error::UnknownUsage => "execution_token_usage_unknown",
                    crate::model_account::Error::Exhausted => "execution_token_budget_exhausted",
                    _ => "model_account_admission",
                }})),
            Self::External(port) => external_callback(|| port.reserve(owner, task, counts, &crate::model_admission::Observation::new(session)))
                .map(|lease| SelectedLease::External { lease: Some(lease), dispatch_attempted: false }),
        }
    }
}
impl SelectedLease {
    fn output_tokens(&self) -> u64 {
        match self {
            Self::Native(lease) => lease.output_tokens(),
            Self::External { lease, .. } => lease.as_ref().map_or(0, |lease| lease.output_tokens()),
        }
    }
    fn mark_dispatched(&mut self, session: &Arc<ExecutionSession>) -> Result<Option<reqwest::header::HeaderMap>, Value> {
        if stop_now(session) {
            return Err(json!({"kind":"model_account_dispatch"}));
        }
        match self {
            Self::Native(lease) => lease.mark_dispatched(|| stop_now(session)).map(|_| None).map_err(|error|
                json!({"kind":if error == crate::model_account::Error::UnknownUsage {"execution_token_usage_unknown"} else {"model_account_dispatch"}})),
            Self::External { lease, dispatch_attempted } => {
                // A lost dispatch ACK cannot prove the authority did not enter.
                *dispatch_attempted = true;
                external_callback(|| {
                    let lease = lease.as_mut().ok_or_else(|| json!({"kind":"model_account_dispatch"}))?;
                    let headers = lease.mark_dispatched_with_headers(&crate::model_admission::Observation::new(session))?;
                    if headers.as_ref().is_some_and(|headers| headers.keys().any(|name|
                        crate::execution_contract::MODEL_DISPATCH_FORBIDDEN_HEADERS.contains(&name.as_str())
                        || headers.get_all(name).iter().take(2).count() > 1)) {
                        return Err(json!({"kind":"model_account_dispatch_headers"}));
                    }
                    Ok(headers)
                })
            }
        }
    }
    fn settle(&mut self,usage:Option<&Value>,unsent:bool) -> Result<(),Value> {
        self.settle_with_cost(usage,unsent).map(|_|())
    }
    fn settle_with_cost(&mut self, usage: Option<&Value>, unsent: bool) -> Result<Option<Box<Value>>, Value> {
        match self {
            Self::Native(lease) => lease.settle(usage, unsent).map(|_|None).map_err(|_| json!({"kind":"model_account_settlement"})),
            Self::External { lease, dispatch_attempted } => {
                let Some(mut lease) = lease.take() else { return Ok(None); };
                external_callback(|| lease.settle_with_cost(usage, unsent && !*dispatch_attempted))
            }
        }
    }
}
impl Drop for SelectedLease {
    fn drop(&mut self) {
        if matches!(self, Self::External { .. }) { let _ = self.settle(None, true); }
        // Native Lease's own Drop preserves its original account semantics.
    }
}

pub struct BudgetedHttpProvider<'a> {
    inner: HttpProvider<'a>,
    admission: SelectedAdmission,
    owner: u64,
    task: String,
    counter: Arc<dyn InputCounter>,
    output: OutputField,
}
impl<'a> HttpProvider<'a> {
    pub fn with_account(
        self,
        account: Arc<crate::model_account::Account>,
        owner: u64,
        task: String,
        counter: Arc<dyn InputCounter>,
        output: OutputField,
    ) -> Result<BudgetedHttpProvider<'a>, Value> {
        self.with_selected_admission(SelectedAdmission::Native(account), owner, task, counter, output)
    }
    /// Use the caller's existing shared account; no local mirror is constructed.
    pub fn with_model_admission(
        self, admission: Arc<dyn crate::model_admission::Admission>, owner: u64,
        task: String, counter: Arc<dyn InputCounter>, output: OutputField,
    ) -> Result<BudgetedHttpProvider<'a>, Value> {
        self.with_selected_admission(SelectedAdmission::External(admission), owner, task, counter, output)
    }
    fn with_selected_admission(
        self, admission: SelectedAdmission, owner: u64, task: String,
        counter: Arc<dyn InputCounter>, output: OutputField,
    ) -> Result<BudgetedHttpProvider<'a>, Value> {
        let key = output.key();
        if owner == 0 || task.trim().is_empty()
            || admission.authorize(owner, &task).is_err()
            || self
                .configuration
                .template
                .get(key)
                .and_then(Value::as_u64)
                .is_none_or(|n| n == 0)
            || self
                .configuration
                .template
                .contains_key(if key == "max_tokens" {
                    "max_completion_tokens"
                } else {
                    "max_tokens"
                })
        {
            return Err(
                json!({"kind":"model_account_configuration","message":"native counted provider configuration refused"}),
            );
        }
        Ok(BudgetedHttpProvider {
            inner: self,
            admission,
            owner,
            task,
            counter,
            output,
        })
    }
}
fn stop_now(session: &ExecutionSession) -> bool {
    let facts = session.stop_observer().facts();
    facts.cancellation_requested
        || facts.closing
        || facts.deadline_remaining_nanos.is_some_and(|n| n <= 0)
}
async fn account_stop(session: &ExecutionSession) {
    let observer = session.stop_observer();
    let facts = observer.facts();
    let deadline = async {
        if let Some(n) = facts.deadline_remaining_nanos {
            if n <= 0 {
                return;
            }
            let n = n as u128;
            let duration = Duration::new(
                (n / 1_000_000_000).min(u64::MAX as u128) as u64,
                (n % 1_000_000_000) as u32,
            );
            if let Some(at) = tokio::time::Instant::now().checked_add(duration) {
                tokio::time::sleep_until(at).await;
                return;
            }
        }
        std::future::pending::<()>().await;
    };
    tokio::select! {_=observer.stopped()=>{},_=deadline=>{}}
}
impl BudgetedHttpProvider<'_> {
    pub fn with_text_observer(mut self, observer: Arc<dyn crate::http::TextObserver>) -> Self {
        self.inner = self.inner.with_text_observer(observer);
        self
    }
}
impl Provider for BudgetedHttpProvider<'_> {
    fn round(
        &self,
        messages: &[Value],
        session: &Arc<ExecutionSession>,
    ) -> Result<StreamResult, ProviderError> {
        if !session.belongs_to(self.owner, &self.task) {
            return Err(ProviderError::Failed(json!({"kind":"model_account_owner"})));
        }
        if stop_now(session) {
            return Err(ProviderError::Stopped);
        }
        let prepared=self.inner.prepared_tools.as_ref().map(|state|state.template()).transpose()
            .map_err(|error|ProviderError::Failed(json!({"kind":"model_projection","message":error})))?;
        let template=prepared.as_deref().unwrap_or(&self.inner.configuration.template);
        let input = self
            .counter
            .tokens(
                self.owner,
                &self.task,
                messages,
                template,
            )
            .map_err(ProviderError::Failed)?;
        let output = self.inner.configuration.template[self.output.key()]
            .as_u64()
            .unwrap();
        if stop_now(session) {
            return Err(ProviderError::Stopped);
        }
        let counts = crate::model_account::Counts {
            input,
            output,
            minimum_output: 1,
        };
        let mut lease = self.admission.reserve(self.owner, &self.task, counts, session, self.inner.runtime)
            .map_err(|error| if stop_now(session) { ProviderError::Stopped } else { ProviderError::Failed(error) })?;
        let admitted_output = lease.output_tokens();
        if admitted_output < counts.minimum_output || admitted_output > counts.output {
            let settled = lease.settle(None, true);
            return Err(ProviderError::Failed(json!({"kind":if settled.is_ok() {"model_account_output_cap"} else {"model_account_settlement"}})));
        }
        let mut result = self.inner.round_inner(
            template,
            messages,
            session,
            Some((self.output.key(), admitted_output)),
            Some(&mut lease),
        );
        match lease.settle_with_cost(result.as_ref().ok().map(|r|&r.usage),true) {
            Ok(cost) => { if let Ok(result)=&mut result {result.cost=cost;} }
            Err(error) => {
                let usage=result.as_ref().ok().map(|r|&r.usage);
                return Err(ProviderError::Failed(json!({"kind":if error["kind"]=="model_pricing" {"model_pricing"} else {"model_account_settlement"},"usage":usage})));
            }
        }
        result
    }
}

#[cfg(test)]
mod model_admission_cost {
    #[test]
    fn selected_authority_retains_metadata_and_native_leases_stay_inline() {
        println!("selected_admission_inline_bytes={} prior_account_arc_bytes={} selected_lease_inline_bytes={} native_lease_inline_bytes={}",
            std::mem::size_of::<super::SelectedAdmission>(),std::mem::size_of::<std::sync::Arc<crate::model_account::Account>>(),
            std::mem::size_of::<super::SelectedLease>(),std::mem::size_of::<crate::model_account::Lease>());
    }
}
