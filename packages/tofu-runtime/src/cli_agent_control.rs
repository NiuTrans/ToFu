//! One bounded control reader on the existing native reactor. It observes cancel
//! during provider/tool waits, binds replies to published requests, and joins
//! through the parent session resource lease before terminal publication.
use crate::cli_agent::Input;
use serde_json::{json, Value};
use std::{collections::HashMap, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}};
use tofu_runtime::{
    agent::{Provider, ProviderError},
    http::Cancellation,
    provider::StreamResult,
    session::{ExecutionSession, ReleaseAck},
    tool_contract::Catalog,
};
fn failure() -> Value {
    json!({"kind":"client_tool_protocol","message":"native client control exchange refused"})
}
pub(crate) struct Exchange {
    pub(crate) scope: Option<Arc<crate::cli_agent_worker::Scope>>,
    pending: Mutex<Option<(String, bool, String)>>,
    fatal: Mutex<Option<Value>>,
    input_closed: AtomicBool,
    replies: tokio::sync::Mutex<tokio::sync::mpsc::Receiver<Result<Value, Value>>>,
    tools: Mutex<HashMap<String, ToolReply>>,
    pub(crate) publication: tokio::sync::Mutex<()>,
}
struct ToolReply {
    published: bool,
    sender: tokio::sync::oneshot::Sender<Result<Value, Value>>,
}
pub(crate) struct ToolExchange {
    control: Arc<Exchange>,
    id: String,
    receiver: Option<tokio::sync::oneshot::Receiver<Result<Value, Value>>>,
}
impl ToolExchange {
    pub(crate) async fn response(&mut self) -> Result<Value, Value> {
        let result = self.receiver.take().ok_or_else(failure)?.await.map_err(|_| failure())?;
        if let Some(error) = self.control.failure() { return Err(error); }
        result
    }
}
impl Drop for ToolExchange {
    fn drop(&mut self) {
        if let Ok(mut tools) = self.control.tools.lock() { tools.remove(&self.id); }
    }
}
impl Exchange {
    pub(crate) fn quiescent(&self) -> bool {
        self.failure().is_none() && self.pending.lock().is_ok_and(|pending|pending.is_none())
            && self.tools.lock().is_ok_and(|tools|tools.is_empty())
            && self.replies.try_lock().is_ok_and(|replies|replies.is_empty())
    }
    pub(crate) fn prepare(&self, id: String, response: &str) -> Result<(), Value> {
        if let Some(error) = self.failure() {
            return Err(error);
        }
        let mut pending = self.pending.lock().map_err(|_| failure())?;
        if self.input_closed.load(Ordering::SeqCst) || pending.is_some() || !self.tools.lock().map_err(|_| failure())?.is_empty() {
            return Err(failure());
        }
        *pending = Some((id, false, response.into()));
        Ok(())
    }
    pub(crate) fn prepare_tool(self: &Arc<Self>, id: String) -> Result<ToolExchange, Value> {
        if let Some(error) = self.failure() { return Err(error); }
        let pending = self.pending.lock().map_err(|_| failure())?;
        let mut tools = self.tools.lock().map_err(|_| failure())?;
        // Fatal publication and waiter draining can race preparation. Recheck
        // while holding the map so no waiter can be inserted after the drain.
        if let Some(error) = self.failure() { return Err(error); }
        if self.input_closed.load(Ordering::SeqCst) || pending.is_some() || id.is_empty() || tools.contains_key(&id) { return Err(failure()); }
        let (sender, receiver) = tokio::sync::oneshot::channel();
        tools.insert(id.clone(), ToolReply {published:false, sender});
        Ok(ToolExchange {control:self.clone(), id, receiver:Some(receiver)})
    }
    /// Bind publication and the final nonblocking write under the same lock.
    /// A fast host cannot deliver a reply between write and publication proof.
    pub(crate) fn write_request(&self, tool_id: Option<&str>, remaining: usize,
        write: impl FnOnce() -> std::io::Result<usize>) -> std::io::Result<usize> {
        let invalid = || std::io::Error::from(std::io::ErrorKind::InvalidData);
        if let Some(id) = tool_id {
            let mut tools = self.tools.lock().map_err(|_| invalid())?;
            let reply = tools.get_mut(id).ok_or_else(invalid)?;
            if reply.published { return Err(invalid()); }
            let count = write()?;
            if count == remaining { reply.published = true; }
            Ok(count)
        } else {
            let mut pending = self.pending.lock().map_err(|_| invalid())?;
            let (_, published, _) = pending.as_mut().ok_or_else(invalid)?;
            if *published { return Err(invalid()); }
            let count = write()?;
            if count == remaining { *published = true; }
            Ok(count)
        }
    }
    fn deliver_tool(&self, value: Value) -> Result<(), Value> {
        let id = value["request_id"].as_str().ok_or_else(failure)?;
        let mut tools = self.tools.lock().map_err(|_| failure())?;
        if !tools.get(id).is_some_and(|reply|reply.published) { return Err(failure()); }
        tools.remove(id).ok_or_else(failure)?.sender.send(Ok(value)).map_err(|_| failure())
    }
    fn close_input(&self) -> bool {
        // Half-close after delivered replies preserves them. Future requests
        // refuse; outstanding requests must wake rather than await forever.
        let pending = self.pending.lock().unwrap_or_else(|error| error.into_inner());
        let tools = self.tools.lock().unwrap_or_else(|error| error.into_inner());
        self.input_closed.store(true, Ordering::SeqCst);
        pending.is_some() || !tools.is_empty()
    }
    fn fail_waiters(&self) {
        if let Ok(mut fatal) = self.fatal.lock() { *fatal = Some(failure()); }
        if let Ok(mut tools) = self.tools.lock() {
            for (_, reply) in tools.drain() { let _ = reply.sender.send(Err(failure())); }
        }
    }
    pub(crate) fn failure(&self) -> Option<Value> {
        self.fatal
            .lock()
            .map(|v| v.clone())
            .unwrap_or_else(|_| Some(failure()))
    }
    pub(crate) async fn response(&self) -> Result<Value, Value> {
        let value = self.replies.lock().await.recv().await.ok_or_else(failure)?;
        if let Some(error) = self.failure() {
            return Err(error);
        }
        value
    }
}
enum ReaderJoin {
    Running(tokio::task::JoinHandle<Vec<u8>>),
    Joined,
    Failed,
}
pub(crate) struct ReaderLease {
    runtime: Arc<tokio::runtime::Runtime>,
    tail: Mutex<Option<Vec<u8>>>,
    shutdown: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
    worker: Mutex<ReaderJoin>,
}
impl ReaderLease {
    pub(crate) fn finish(&self) -> Result<ReleaseAck, ()> {
        if let Some(signal) = self.shutdown.lock().map_err(|_| ())?.take() {
            let _ = signal.send(());
        }
        // Serialize observations of this exact join. Taking the handle and
        // unlocking lets another caller acknowledge before the actor exits.
        let mut state = self.worker.lock().map_err(|_| ())?;
        let worker = match std::mem::replace(&mut *state, ReaderJoin::Failed) {
            ReaderJoin::Running(worker) => worker,
            ReaderJoin::Joined => {
                *state = ReaderJoin::Joined;
                return Ok(ReleaseAck::Released);
            }
            ReaderJoin::Failed => return Err(()),
        };
        let tail = self.runtime.block_on(worker).map_err(|_| ())?;
        *self.tail.lock().map_err(|_| ())? = Some(tail);
        *state = ReaderJoin::Joined;
        Ok(ReleaseAck::Released)
    }
}
impl ReaderLease {
    pub(crate) fn take_tail(&self) -> Result<Vec<u8>, ()> {
        self.finish()?;
        Ok(self.tail.lock().map_err(|_| ())?.take().unwrap_or_default())
    }
}
impl Drop for ReaderLease {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}
pub(crate) fn start(
    runtime: Arc<tokio::runtime::Runtime>,
    mut input: Input,
    session: Arc<ExecutionSession>,
    schemas: Arc<Catalog>,
    owner: u64,
    frame_bytes: usize,
    cancellation: Cancellation,
) -> (Arc<Exchange>, Arc<ReaderLease>) {
    let (tx, rx) = tokio::sync::mpsc::channel(1);
    let (shutdown, mut stop) = tokio::sync::oneshot::channel();
    let exchange = Arc::new(Exchange {
        scope: input.scope(),
        pending: Mutex::new(None),
        fatal: Mutex::new(None),
        input_closed: AtomicBool::new(false),
        replies: tokio::sync::Mutex::new(rx),
        tools: Mutex::new(HashMap::new()),
        publication: tokio::sync::Mutex::new(()),
    });
    let control = exchange.clone();
    let session = Arc::downgrade(&session);
    let worker = runtime.spawn(async move {
        loop {
            let value =
                tokio::select! {biased;_= &mut stop=>return input.tail(),value=input.frame(frame_bytes)=>value};
            if matches!(&value,Err(e) if e.kind()==std::io::ErrorKind::UnexpectedEof) {
                if control.close_input() {
                    control.fail_waiters();
                    let _ = tx.try_send(Err(failure()));
                    cancellation.cancel();
                }
                return input.tail();
            }
            let value = value.map_err(|_| failure()).and_then(|value| {
                if value["type"] == "cancel" {
                    schemas
                        .validate(owner, "cancel", value)
                        .map_err(|_| failure())?;
                    session
                        .upgrade()
                        .ok_or_else(failure)?
                        .request_cancel("client_cancel")
                        .map_err(|_| failure())?;
                    return Ok(None);
                }
                if value["type"] == "tool_result" {
                    let value = schemas.validate(owner, "tool_result", value).map_err(|_| failure())?;
                    control.deliver_tool(value)?;
                    return Ok(None);
                }
                let mut pending = control.pending.lock().map_err(|_| failure())?;
                let response = pending.as_ref().ok_or_else(failure)?.2.clone();
                let value = schemas
                    .validate(owner, &response, value)
                    .map_err(|_| failure())?;
                if !pending
                    .as_ref()
                    .is_some_and(|(id, published, _)| *published && value["request_id"] == *id)
                {
                    return Err(failure());
                }
                pending.take();
                Ok(Some(value))
            });
            match value {
                Ok(None) => {
                    tokio::task::yield_now().await;
                    continue;
                }
                Ok(Some(value)) => {
                    if tx.try_send(Ok(value)).is_ok() {
                        continue;
                    }
                }
                Err(_) => {}
            }
            control.fail_waiters();
            let _ = tx.try_send(Err(failure()));
            cancellation.cancel();
            return input.tail();
        }
    });
    (
        exchange,
        Arc::new(ReaderLease {
            runtime,
            tail: Mutex::new(None),
            shutdown: Mutex::new(Some(shutdown)),
            worker: Mutex::new(ReaderJoin::Running(worker)),
        }),
    )
}
pub(crate) struct ControlledProvider<P: Provider> {
    pub inner: P,
    pub control: Arc<Exchange>,
}
impl<P: Provider> Provider for ControlledProvider<P> {
    fn round(
        &self,
        messages: &[Value],
        session: &Arc<ExecutionSession>,
    ) -> Result<StreamResult, ProviderError> {
        if let Some(error) = self.control.failure() {
            return Err(ProviderError::Failed(error));
        }
        let value = self.inner.round(messages, session);
        if let Some(error) = self.control.failure() {
            return Err(ProviderError::Failed(error));
        }
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    fn exchange() -> Arc<Exchange> {
        let (_, rx) = tokio::sync::mpsc::channel(1);
        Arc::new(Exchange {
            scope: None, pending: Mutex::new(None), fatal: Mutex::new(None), input_closed: AtomicBool::new(false),
            replies: tokio::sync::Mutex::new(rx), tools: Mutex::new(HashMap::new()),
            publication: tokio::sync::Mutex::new(()),
        })
    }
    #[test]
    fn parallel_replies_bind_exact_published_requests_in_reverse_order() {
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
        let control = exchange();
        let mut requests = (0..8).map(|i| control.prepare_tool(i.to_string()).unwrap()).collect::<Vec<_>>();
        assert!(control.prepare("round".into(), "round_result").is_err());
        assert!(control.prepare_tool("0".into()).is_err());
        for i in 0..8 {
            assert!(control.deliver_tool(json!({"request_id":i.to_string()})).is_err());
            control.write_request(Some(&i.to_string()), 4, || Ok(4)).unwrap();
        }
        for i in (0..8).rev() {
            control.deliver_tool(json!({"request_id":i.to_string(),"content":i})).unwrap();
            assert!(control.deliver_tool(json!({"request_id":i.to_string()})).is_err());
        }
        for (i, request) in requests.iter_mut().enumerate() {
            assert_eq!(runtime.block_on(request.response()).unwrap()["content"], i);
        }
        assert!(control.quiescent());
    }
    #[test]
    fn partial_failed_and_blocked_writes_never_publish_a_reply_slot() {
        let control = exchange();
        let request = control.prepare_tool("read".into()).unwrap();
        control.write_request(Some("read"), 4, || Ok(2)).unwrap();
        assert!(control.deliver_tool(json!({"request_id":"read"})).is_err());
        assert!(control.write_request(Some("read"), 2, || Err(std::io::ErrorKind::WouldBlock.into())).is_err());
        assert!(control.deliver_tool(json!({"request_id":"read"})).is_err());
        control.write_request(Some("read"), 2, || Ok(2)).unwrap();
        assert!(control.write_request(Some("read"), 2, || panic!("already published")).is_err());
        drop(request);
        assert!(control.quiescent());
        assert!(control.deliver_tool(json!({"request_id":"read"})).is_err());
        control.prepare("round".into(), "round_result").unwrap();
        assert!(control.prepare_tool("other".into()).is_err());
        control.write_request(None, 1, || Ok(1)).unwrap();
    }
    #[test]
    fn fatal_input_wakes_every_tool_and_refuses_new_requests() {
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
        let control = exchange();
        let mut requests = (0..8).map(|i| control.prepare_tool(i.to_string()).unwrap()).collect::<Vec<_>>();
        control.fail_waiters();
        for request in &mut requests { assert!(runtime.block_on(request.response()).is_err()); }
        assert!(control.tools.lock().unwrap().is_empty());
        assert!(control.prepare_tool("new".into()).is_err());
        assert!(!control.quiescent());
    }
    #[test]
    fn input_half_close_keeps_delivered_results_and_refuses_later_requests() {
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
        let control = exchange();
        let mut request = control.prepare_tool("read".into()).unwrap();
        control.write_request(Some("read"), 1, || Ok(1)).unwrap();
        control.deliver_tool(json!({"request_id":"read","content":"retained"})).unwrap();
        assert!(!control.close_input());
        assert_eq!(runtime.block_on(request.response()).unwrap()["content"], "retained");
        assert!(control.prepare_tool("later".into()).is_err());
        assert!(control.prepare("round".into(), "round_result").is_err());
    }
    #[test]
    fn shutdown_joins_actor_before_release_ack_and_is_idempotent() {
        let runtime = Arc::new(
            tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap(),
        );
        let (tx, rx) = tokio::sync::oneshot::channel();
        let exited = Arc::new(AtomicBool::new(false));
        let marker = exited.clone();
        let worker = runtime.spawn(async move {
            let _ = rx.await;
            marker.store(true, Ordering::SeqCst);
            vec![]
        });
        let lease = ReaderLease {
            runtime,
            tail: Mutex::new(None),
            shutdown: Mutex::new(Some(tx)),
            worker: Mutex::new(ReaderJoin::Running(worker)),
        };
        lease.finish().unwrap();
        assert!(exited.load(Ordering::SeqCst));
        lease.finish().unwrap();
    }
    #[test]
    fn concurrent_release_waits_for_the_same_actor_join() {
        let runtime = Arc::new(tokio::runtime::Builder::new_current_thread().build().unwrap());
        let (shutdown, _stop) = tokio::sync::oneshot::channel();
        let (release, blocked) = tokio::sync::oneshot::channel();
        let (entered, entry) = std::sync::mpsc::channel();
        let exited = Arc::new(AtomicBool::new(false));
        let marker = exited.clone();
        let worker = runtime.spawn(async move {
            entered.send(()).unwrap();
            blocked.await.unwrap();
            marker.store(true, Ordering::SeqCst);
            b"joined-tail".to_vec()
        });
        let lease = Arc::new(ReaderLease {
            runtime, tail: Mutex::new(None), shutdown: Mutex::new(Some(shutdown)),
            worker: Mutex::new(ReaderJoin::Running(worker)),
        });
        let first = lease.clone();
        let first = std::thread::spawn(move || first.finish());
        entry.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        let (started, start) = std::sync::mpsc::channel();
        let (returned, returns) = std::sync::mpsc::channel();
        let second = lease.clone();
        let second = std::thread::spawn(move || {
            started.send(()).unwrap();
            returned.send(second.finish()).unwrap();
        });
        start.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        let premature = returns.recv_timeout(std::time::Duration::from_millis(25));
        release.send(()).unwrap();
        assert!(first.join().unwrap().is_ok());
        second.join().unwrap();
        assert!(matches!(premature, Err(std::sync::mpsc::RecvTimeoutError::Timeout)));
        assert!(returns.recv().unwrap().is_ok());
        assert!(exited.load(Ordering::SeqCst));
        assert_eq!(lease.take_tail().unwrap(), b"joined-tail");
    }

    #[test]
    fn actor_panic_is_cleanup_failure() {
        let runtime = Arc::new(
            tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap(),
        );
        let (tx, _rx) = tokio::sync::oneshot::channel();
        let worker = runtime.spawn(async { panic!("reader fault") });
        let lease = ReaderLease {
            runtime,
            tail: Mutex::new(None),
            shutdown: Mutex::new(Some(tx)),
            worker: Mutex::new(ReaderJoin::Running(worker)),
        };
        assert!(lease.finish().is_err());
        assert!(lease.finish().is_err());
        assert!(lease.take_tail().is_err());
    }
}
