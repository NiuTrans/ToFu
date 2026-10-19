//! Composition-owned, single-endpoint HTTP pool for authorized provider attempts.
//! Routing/retries/credentials remain caller-owned. Errors omit URLs and bodies.
use crate::provider::{Budget, ChatStream, Error as ProviderError, StreamResult, TextChannel};
use reqwest::{
    header::{HeaderMap, CONTENT_TYPE},
    Client, Url,
};
use std::{future::Future, pin::Pin, sync::{Arc, atomic::{AtomicUsize, Ordering}}, time::Duration};
use tokio::sync::watch;
#[derive(Clone, Copy)]
pub struct Policy {
    /// Compatibility hint (also used by CLI blocking workers), not HTTP admission.
    pub connections: usize,
    pub idle_connections: usize,
    pub keepalive: Duration,
    pub connect: Duration,
    /// Optional overall budget through upload and response headers. None keeps
    /// waiting under caller cancellation/deadline; it is not an upload timeout.
    pub request_headers: Option<Duration>,
    /// Independent post-connect physical write/flush budget; no header/read timer.
    pub write: Option<Duration>,
    /// Retained positive compatibility field; HTTP attempts have no slot queue.
    pub pool_wait: Duration,
    pub stream_idle: Option<Duration>,
    pub drain_timeout: Duration,
    pub drain_bytes: usize,
    pub drain_chunks: usize,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Cancelled,
    PoolTimeout,
    RequestTimeout,
    WriteTimeout,
    ConnectTimeout,
    Connection,
    ReadTimeout,
    ReadFailure,
    HttpStatus(u16),
    UnexpectedContentType,
    Provider(ProviderError),
    ProviderRejected(serde_json::Value),
    UnsupportedProviderProfile,
    TextDelivery,
}
/// A required consumer owns all work through its returned future. No detached
/// writes: cancellation waits for a started observation before releasing HTTP.
pub trait TextObserver: Send + Sync {
    fn observe<'a>(
        &'a self,
        channel: TextChannel,
        text: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), TextDeliveryError>> + Send + 'a>>;
}
#[derive(Debug, Clone, Copy)]
pub struct TextDeliveryError;
#[derive(Clone)]
pub struct Cancellation(watch::Sender<bool>);
impl Cancellation {
    pub fn new() -> Self {
        Self(watch::channel(false).0)
    }
    pub fn cancel(&self) {
        self.0.send_replace(true);
    }
    pub fn is_cancelled(&self) -> bool {
        *self.0.borrow()
    }
    pub(crate) async fn cancelled(&self) {
        let mut receiver = self.0.subscribe();
        loop {
            if *receiver.borrow_and_update() {
                return;
            }
            if receiver.changed().await.is_err() {
                return;
            }
        }
    }
}
impl Default for Cancellation {
    fn default() -> Self {
        Self::new()
    }
}
pub struct Request {
    pub headers: HeaderMap,
    pub body: Vec<u8>,
    pub budget: Budget,
    pub identical_tool_limit: Option<usize>,
    pub tool_names: Arc<std::collections::BTreeSet<String>>,
}
pub struct Transport {
    client: Client,
    endpoint: Url,
    active: Arc<AtomicUsize>,
    policy: Policy,
}
struct ActiveRequest(Arc<AtomicUsize>);
impl Drop for ActiveRequest {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}
impl Transport {
    pub fn new(endpoint: &str, proxy: Option<&str>, policy: Policy) -> Result<Self, Error> {
        if policy.connections == 0
            || policy.keepalive.is_zero()
            || policy.connect.is_zero()
            || policy.request_headers.is_some_and(|d| d.is_zero())
            || policy.write.is_some_and(|d| d.is_zero())
            || policy.pool_wait.is_zero()
            || policy.stream_idle.is_some_and(|d| d.is_zero())
            || policy.drain_timeout.is_zero()
            || policy.drain_bytes == 0
            || policy.drain_chunks == 0
        {
            return Err(Error::Configuration);
        }
        let endpoint = Url::parse(endpoint).map_err(|_| Error::Configuration)?;
        if !matches!(endpoint.scheme(), "http" | "https")
            || endpoint.host_str().is_none()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.fragment().is_some()
        {
            return Err(Error::Configuration);
        }
        let mut builder = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .connect_timeout(policy.connect)
            .pool_max_idle_per_host(policy.idle_connections)
            .pool_idle_timeout(policy.keepalive)
            .tcp_nodelay(true);
        builder = builder.write_timeout(policy.write);
        if let Some(proxy) = proxy {
            builder = builder.proxy(reqwest::Proxy::all(proxy).map_err(|_| Error::Configuration)?);
        }
        let client = builder.build().map_err(|_| Error::Configuration)?;
        Ok(Self {
            client,
            endpoint,
            active: Arc::new(AtomicUsize::new(0)),
            policy,
        })
    }
    pub(crate) fn write_timeout(&self) -> Option<Duration> { self.policy.write }

    /// Diagnostic count; this observation never grants or refuses an attempt.
    pub fn active_requests(&self) -> usize {
        self.active.load(Ordering::Acquire)
    }
    pub async fn execute(
        &self,
        request: Request,
        cancel: &Cancellation,
    ) -> Result<StreamResult, Error> {
        self.execute_with_text(request, cancel, None).await
    }
    pub async fn execute_with_text(
        &self,
        request: Request,
        cancel: &Cancellation,
        observer: Option<&dyn TextObserver>,
    ) -> Result<StreamResult, Error> {
        let stream = ChatStream::with_tool_names(
            request.budget,
            request
                .identical_tool_limit
                .unwrap_or(crate::execution_contract::DEFAULT_IDENTICAL_TOOL_CALLS),
            request.tool_names.clone(),
        )
        .map_err(Error::Provider)?;
        self.execute_with_reader(request, cancel, observer, stream)
            .await
    }
    pub async fn execute_anthropic_with_text(
        &self,
        request: Request,
        cancel: &Cancellation,
        observer: Option<&dyn TextObserver>,
        retained: crate::anthropic::Budget,
    ) -> Result<StreamResult, Error> {
        let stream = crate::anthropic::Stream::new(
            request.budget,
            retained,
            request
                .identical_tool_limit
                .unwrap_or(crate::execution_contract::DEFAULT_IDENTICAL_TOOL_CALLS),
            request.tool_names.clone(),
            Default::default(),
        )
        .map_err(Error::Provider)?;
        self.execute_with_reader(request, cancel, observer, stream)
            .await
    }
    pub async fn execute_responses_with_text(
        &self,
        request: Request,
        cancel: &Cancellation,
        observer: Option<&dyn TextObserver>,
        retained: crate::responses::Budget,
        reverse: std::collections::BTreeMap<String, String>,
    ) -> Result<StreamResult, Error> {
        let stream = crate::responses::Stream::new(
            request.budget,
            retained,
            request
                .identical_tool_limit
                .unwrap_or(crate::execution_contract::DEFAULT_IDENTICAL_TOOL_CALLS),
            request.tool_names.clone(),
            reverse,
        )
        .map_err(Error::Provider)?;
        self.execute_with_reader(request, cancel, observer, stream)
            .await
    }
    async fn execute_with_reader<S: ResponseReader>(
        &self,
        request: Request,
        cancel: &Cancellation,
        observer: Option<&dyn TextObserver>,
        mut stream: S,
    ) -> Result<StreamResult, Error> {
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        self.active.fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| value.checked_add(1))
            .map_err(|_| Error::Configuration)?;
        let _active = ActiveRequest(self.active.clone());
        let mut headers = request.headers;
        headers.insert(
            CONTENT_TYPE,
            reqwest::header::HeaderValue::from_static("application/json"),
        );
        let send = self
            .client
            .post(self.endpoint.clone())
            .headers(headers)
            .body(request.body)
            .send();
        let response = async {
            let result = match self.policy.request_headers {
                Some(limit) => tokio::time::timeout(limit, send)
                    .await
                    .map_err(|_| Error::RequestTimeout)?,
                None => send.await,
            };
            result.map_err(|error| {
                if error.is_write_timeout() {
                    Error::WriteTimeout
                } else if error.is_timeout() && error.is_connect() {
                    Error::ConnectTimeout
                } else if error.is_timeout() {
                    Error::RequestTimeout
                } else {
                    Error::Connection
                }
            })
        };
        let mut response = tokio::select! {
            biased;
            _ = cancel.cancelled() => return Err(Error::Cancelled),
            result = response => result?,
        };
        if response.status().as_u16() != 200 {
            return Err(Error::HttpStatus(response.status().as_u16()));
        }
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim();
        if !content_type.eq_ignore_ascii_case("text/event-stream") {
            return Err(Error::UnexpectedContentType);
        }
        loop {
            let chunk = tokio::select! {biased; _=cancel.cancelled()=>return Err(Error::Cancelled), result=next_chunk(&mut response,self.policy.stream_idle)=>result?};
            let Some(chunk) = chunk else {
                return stream.finish(false).map_err(Error::Provider);
            };
            let parsed = if let Some(observer) = observer {
                // Every span charges at least one admitted output byte. Keep
                // offset metadata only; no second text/history allocation.
                let mut spans: Vec<crate::provider::TextSpan> = Vec::new();
                let parsed = stream.feed_with_text(&chunk, &mut |span| {
                    if spans
                        .last_mut()
                        .is_some_and(|last| last.append_contiguous(span))
                    {
                        return;
                    }
                    spans.push(span);
                });
                for span in spans {
                    if cancel.is_cancelled() {
                        return Err(Error::Cancelled);
                    }
                    let delivered = observer
                        .observe(
                            span.channel,
                            stream.span_text(span).ok_or(Error::TextDelivery)?,
                        )
                        .await;
                    if cancel.is_cancelled() {
                        return Err(Error::Cancelled);
                    }
                    delivered.map_err(|_| Error::TextDelivery)?;
                }
                parsed
            } else {
                stream.feed(&chunk)
            };
            let stopped = match parsed {
                Ok(stopped) => stopped,
                Err(error) => {
                    return Err(stream
                        .provider_error()
                        .map(|v| Error::ProviderRejected(v.clone()))
                        .unwrap_or(Error::Provider(error)))
                }
            };
            if stopped {
                // EOF cleanup owns the response, never provider interpretation. A failed
                // drain discards the connection but cannot revoke verified provider work.
                let deadline = tokio::time::Instant::now() + self.policy.drain_timeout;
                let mut bytes = 0usize;
                for _ in 0..self.policy.drain_chunks {
                    let chunk = tokio::select! {biased; _=cancel.cancelled()=>return Err(Error::Cancelled), result=tokio::time::timeout_at(deadline,response.chunk())=>match result{Ok(Ok(chunk))=>chunk,_=>break}};
                    let Some(chunk) = chunk else { break };
                    bytes = bytes.saturating_add(chunk.len());
                    if bytes >= self.policy.drain_bytes {
                        break;
                    }
                }
                return stream.finish(false).map_err(Error::Provider);
            }
        }
    }
}
async fn next_chunk(
    response: &mut reqwest::Response,
    idle: Option<Duration>,
) -> Result<Option<bytes::Bytes>, Error> {
    // Each raw transport chunk, including comments, renews idle. Active generation
    // has no request-wide timeout; pending reads belong to this awaited future.
    if let Some(idle) = idle {
        tokio::time::timeout(idle, response.chunk())
            .await
            .map_err(|_| Error::ReadTimeout)?
            .map_err(|error| if error.is_write_timeout() { Error::WriteTimeout } else { Error::ReadFailure })
    } else {
        response.chunk().await.map_err(|error| if error.is_write_timeout() { Error::WriteTimeout } else { Error::ReadFailure })
    }
}
impl Transport {
    /// Synchronous native task composition over an explicitly owned Tokio runtime.
    /// Request authorization and provider admission must already be resolved.
    pub fn run_task(
        &self,
        runtime: &tokio::runtime::Runtime,
        task: &crate::task::Task,
        owner: u64,
        request: Request,
        cancel: &Cancellation,
    ) -> Result<Arc<crate::task::TerminalCandidate>, crate::task::TaskError> {
        task.run(owner,|session|match runtime.block_on(async { tokio::select! { biased; _ = session.cancelled() => Err(Error::Cancelled), result = self.execute(request, cancel) => result } }) {
   Ok(result)=>result.into_task_result(),
   Err(Error::Cancelled)=>{session.request_cancel("client_abort").map_err(|_|serde_json::json!({"kind":"provider_transport","message":"cancel settlement failed"}))?;Ok(serde_json::Value::Null)},
   Err(Error::Provider(error))=>Err(error.task_error()),
   Err(Error::ProviderRejected(error))=>Err(serde_json::json!({"kind":"provider_rejection","provider":error})),
   Err(error)=>Err(serde_json::json!({"kind":"provider_transport","message":format!("{error:?}")})),
  })
    }
}

// Monomorphized readers share the same physical HTTP/consumer lifetime owner.
trait ResponseReader {
    fn provider_error(&self) -> Option<&serde_json::Value> {
        None
    }
    fn feed_with_text(
        &mut self,
        bytes: &[u8],
        observe: &mut dyn FnMut(crate::provider::TextSpan),
    ) -> Result<bool, ProviderError>;
    fn span_text(&self, span: crate::provider::TextSpan) -> Option<&str>;
    fn finish(self, aborted: bool) -> Result<StreamResult, ProviderError>;
    fn feed(&mut self, bytes: &[u8]) -> Result<bool, ProviderError> {
        self.feed_with_text(bytes, &mut |_| {})
    }
}
impl ResponseReader for ChatStream {
    fn feed_with_text(
        &mut self,
        b: &[u8],
        o: &mut dyn FnMut(crate::provider::TextSpan),
    ) -> Result<bool, ProviderError> {
        ChatStream::feed_with_text(self, b, o)
    }
    fn span_text(&self, s: crate::provider::TextSpan) -> Option<&str> {
        ChatStream::span_text(self, s)
    }
    fn finish(self, a: bool) -> Result<StreamResult, ProviderError> {
        ChatStream::finish(self, a)
    }
}
impl ResponseReader for crate::anthropic::Stream {
    fn feed_with_text(
        &mut self,
        b: &[u8],
        o: &mut dyn FnMut(crate::provider::TextSpan),
    ) -> Result<bool, ProviderError> {
        Self::feed_with_text(self, b, o)
    }
    fn span_text(&self, s: crate::provider::TextSpan) -> Option<&str> {
        Self::span_text(self, s)
    }
    fn finish(self, a: bool) -> Result<StreamResult, ProviderError> {
        Self::finish(self, a)
    }
}

impl ResponseReader for crate::responses::Stream {
    fn provider_error(&self) -> Option<&serde_json::Value> {
        Self::provider_error(self)
    }
    fn feed_with_text(
        &mut self,
        b: &[u8],
        o: &mut dyn FnMut(crate::provider::TextSpan),
    ) -> Result<bool, ProviderError> {
        Self::feed_with_text(self, b, o)
    }
    fn span_text(&self, s: crate::provider::TextSpan) -> Option<&str> {
        Self::span_text(self, s)
    }
    fn finish(self, a: bool) -> Result<StreamResult, ProviderError> {
        Self::finish(self, a)
    }
}
