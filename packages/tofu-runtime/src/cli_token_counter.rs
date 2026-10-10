//! One owned scoped CPU worker for CLI token ingress. Borrows frozen input,
//! keeps the existing reactor reading control during cold BPE work, and joins
//! before returning. Explicit stack bounds; no detached job or permanent pool.
use serde_json::{json, Map, Value};
use std::sync::Arc;
use tofu_runtime::{agent_http::InputCounter, token_ingress::Counter};
pub(crate) struct Worker {
    pub counter: Counter,
    pub runtime: Arc<tokio::runtime::Runtime>,
    pub stack_bytes: usize,
}
impl InputCounter for Worker {
    fn tokens(
        &self,
        owner: u64,
        task: &str,
        messages: &[Value],
        template: &Map<String, Value>,
    ) -> Result<u64, Value> {
        std::thread::scope(|scope| {
            let (send, receive) = tokio::sync::oneshot::channel();
            let worker = std::thread::Builder::new()
                .name("tofu-token-input".into())
                .stack_size(self.stack_bytes)
                .spawn_scoped(scope, move || {
                    let value = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        self.counter.tokens(owner, task, messages, template)
                    }));
                    let _ = send.send(value);
                })
                .map_err(|_| json!({"kind":"token_worker_start"}))?;
            let result = self
                .runtime
                .block_on(receive)
                .map_err(|_| json!({"kind":"token_worker_reply"}));
            worker
                .join()
                .map_err(|_| json!({"kind":"token_worker_panic"}))?;
            result?.map_err(|_| json!({"kind":"token_worker_panic"}))?
        })
    }
}
