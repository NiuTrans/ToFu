//! One bounded control reader on the existing native reactor. It observes cancel
//! during provider/tool waits, binds replies to published requests, and joins
//! through the parent session resource lease before terminal publication.
use crate::cli_agent::Input;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
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
    pending: Mutex<Option<(String, bool, String)>>,
    fatal: Mutex<Option<Value>>,
    replies: tokio::sync::Mutex<tokio::sync::mpsc::Receiver<Result<Value, Value>>>,
}
impl Exchange {
    pub(crate) fn prepare(&self, id: String, response: &str) -> Result<(), Value> {
        if let Some(error) = self.failure() {
            return Err(error);
        }
        let mut pending = self.pending.lock().map_err(|_| failure())?;
        if pending.is_some() {
            return Err(failure());
        }
        *pending = Some((id, false, response.into()));
        Ok(())
    }
    pub(crate) fn published(&self) -> Result<(), Value> {
        let mut pending = self.pending.lock().map_err(|_| failure())?;
        let (_, published, _) = pending.as_mut().ok_or_else(failure)?;
        *published = true;
        Ok(())
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
pub(crate) struct ReaderLease {
    runtime: Arc<tokio::runtime::Runtime>,
    tail: Mutex<Option<Vec<u8>>>,
    shutdown: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
    worker: Mutex<Option<tokio::task::JoinHandle<Vec<u8>>>>,
}
impl ReaderLease {
    pub(crate) fn finish(&self) -> Result<ReleaseAck, ()> {
        if let Some(signal) = self.shutdown.lock().map_err(|_| ())?.take() {
            let _ = signal.send(());
        }
        let worker = self.worker.lock().map_err(|_| ())?.take();
        if let Some(worker) = worker {
            let tail = self.runtime.block_on(worker).map_err(|_| ())?;
            *self.tail.lock().map_err(|_| ())? = Some(tail);
        }
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
        pending: Mutex::new(None),
        fatal: Mutex::new(None),
        replies: tokio::sync::Mutex::new(rx),
    });
    let control = exchange.clone();
    let session = Arc::downgrade(&session);
    let worker = runtime.spawn(async move {
        loop {
            let value =
                tokio::select! {biased;_= &mut stop=>return input.tail(),value=input.frame(frame_bytes)=>value};
            if matches!(&value,Err(e) if e.kind()==std::io::ErrorKind::UnexpectedEof) {
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
            if let Ok(mut fatal) = control.fatal.lock() {
                *fatal = Some(failure());
            }
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
            worker: Mutex::new(Some(worker)),
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
            worker: Mutex::new(Some(worker)),
        };
        lease.finish().unwrap();
        assert!(exited.load(Ordering::SeqCst));
        lease.finish().unwrap();
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
            worker: Mutex::new(Some(worker)),
        };
        assert!(lease.finish().is_err());
    }
}
