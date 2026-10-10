//! Counted host-account exchange through the existing one-slot native reader.
//! No local account mirror, history carrier, second reader or automatic retry.
use crate::{cli_agent::publish_request, cli_agent_control::Exchange};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use tofu_runtime::{
    model_account::Counts,
    model_admission::{Admission, Lease, Observation},
    tool_contract::Catalog,
};
fn failure() -> Value {
    json!({"kind":"model_control","message":"native host model authority refused"})
}
pub(crate) struct Port {
    pub runtime: Arc<tokio::runtime::Runtime>,
    pub control: Arc<Exchange>,
    pub schemas: Arc<Catalog>,
    pub owner: u64,
    pub task: String,
    pub version: String,
    pub frame_bytes: usize,
    pub ack: std::time::Duration,
    pub ordinal: AtomicU64,
}
struct Held {
    port: Arc<Port>,
    ordinal: u64,
    counts: Counts,
    output: u64,
    observation: Observation,
}
impl Port {
    fn exchange(
        &self,
        ordinal: u64,
        counts: Counts,
        action: &str,
        observation: &Observation,
        settlement: Option<(Option<&Value>, bool)>,
    ) -> Result<Value, Value> {
        let began = std::time::Instant::now();
        let budget = if action == "settle" {
            self.ack
        } else {
            observation
                .deadline_remaining()
                .map_or(self.ack, |remaining| remaining.min(self.ack))
        };
        if budget.is_zero() {
            return Err(failure());
        }
        let id = format!("{}-model-{ordinal}-{action}", self.task);
        let mut clock = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut clock) } != 0
            || clock.tv_sec < 0
        {
            return Err(failure());
        }
        let expires =
            (clock.tv_sec as u128) * 1_000_000_000 + clock.tv_nsec as u128 + budget.as_nanos();
        let expires = u64::try_from(expires).map_err(|_| failure())?;
        let mut frame = json!({"version":self.version,"type":"model_request","request_id":id,
            "owner_user_id":self.owner,"task_id":self.task,"attempt_ordinal":ordinal,"action":action,
            "input_tokens":counts.input,"max_output_tokens":counts.output,"minimum_output_tokens":counts.minimum_output,
            "expires_monotonic_ns":expires});
        if let Some((usage, unsent)) = settlement {
            frame["usage"] = usage.cloned().unwrap_or(Value::Null);
            frame["unsent"] = json!(unsent);
        }
        self.schemas
            .validate(self.owner, "model_request", frame.clone())
            .map_err(|_| failure())?;
        let bytes = serde_json::to_vec(&frame).map_err(|_| failure())?;
        if bytes.len() > self.frame_bytes {
            return Err(failure());
        }
        self.control.prepare(id, "model_decision")?;
        self.runtime.block_on(async {
            let cancelled = async {
                if action != "settle" {
                    observation.cancelled().await;
                } else {
                    std::future::pending::<()>().await;
                }
            };
            let io = async {
                publish_request(&self.runtime, &bytes, cancelled, &self.control)
                    .await
                    .map_err(|_| failure())?;
                let reply = self.control.response().await?;
                if reply["accepted"] != true {
                    return Err(reply.get("error").cloned().unwrap_or_else(failure));
                }
                if reply.get("error").is_some() || (action != "reserve" && reply.get("output_tokens").is_some()) || (action != "dispatch" && reply.get("headers").is_some()) {
                    return Err(failure());
                }
                Ok(reply)
            };
            tokio::time::timeout(budget.saturating_sub(began.elapsed()), io)
                .await
                .map_err(|_| failure())?
        })
    }
}
pub(crate) struct Authority(pub Arc<Port>);
impl Admission for Authority {
    fn authorize(&self, owner: u64, task: &str) -> Result<(), Value> {
        if owner == self.0.owner && task == self.0.task {
            Ok(())
        } else {
            Err(failure())
        }
    }
    fn reserve(
        &self,
        owner: u64,
        task: &str,
        counts: Counts,
        observation: &Observation,
    ) -> Result<Box<dyn Lease>, Value> {
        self.authorize(owner, task)?;
        let ordinal = self
            .0
            .ordinal
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| n.checked_add(1))
            .map_err(|_| failure())?
            + 1;
        let reply = self
            .0
            .exchange(ordinal, counts, "reserve", observation, None)?;
        let output = reply["output_tokens"].as_u64().ok_or_else(failure)?;
        Ok(Box::new(Held {
            port: self.0.clone(),
            ordinal,
            counts,
            output,
            observation: observation.clone(),
        }))
    }
}
impl Lease for Held {
    fn output_tokens(&self) -> u64 {
        self.output
    }
    fn mark_dispatched(&mut self, observation: &Observation) -> Result<(), Value> {
        self.mark_dispatched_with_headers(observation).map(|_| ())
    }
    fn mark_dispatched_with_headers(&mut self, observation: &Observation) -> Result<Option<reqwest::header::HeaderMap>, Value> {
        let reply = self.port.exchange(self.ordinal, self.counts, "dispatch", observation, None)?;
        if let Some(value) = reply.get("headers") {
            let mut headers = reqwest::header::HeaderMap::new();
            for (name, value) in value.as_object().ok_or_else(failure)? {
                let name = reqwest::header::HeaderName::from_bytes(name.as_bytes()).map_err(|_| failure())?;
                if tofu_runtime::execution_contract::MODEL_DISPATCH_FORBIDDEN_HEADERS.contains(&name.as_str()) {
                    return Err(failure());
                }
                let value = reqwest::header::HeaderValue::from_str(value.as_str().ok_or_else(failure)?).map_err(|_| failure())?;
                if headers.insert(name, value).is_some() { return Err(failure()); }
            }
            return Ok(Some(headers));
        }
        Ok(None)
    }
    fn settle(&mut self, usage: Option<&Value>, unsent: bool) -> Result<(), Value> {
        self.port
            .exchange(
                self.ordinal,
                self.counts,
                "settle",
                &self.observation,
                Some((usage, unsent)),
            )
            .map(|_| ())
    }
}
