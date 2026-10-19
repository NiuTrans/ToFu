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
fn pricing_failure() -> Value {
    json!({"kind":"model_pricing","message":"native resolved pricing refused"})
}
pub(crate) struct Port {
    pub runtime: Arc<tokio::runtime::Runtime>,
    pub control: Arc<Exchange>,
    pub schemas: Arc<Catalog>,
    pub owner: u64,
    pub task: String,
    pub version: String,
    pub frame_bytes: usize,
    pub ack: Option<std::time::Duration>,
    pub pricing: bool,
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
            match (self.ack, observation.deadline_remaining()) {
                (Some(ack), Some(remaining)) => Some(remaining.min(ack)),
                (ack, remaining) => ack.or(remaining),
            }
        };
        if budget.is_some_and(|budget| budget.is_zero()) {
            return Err(failure());
        }
        let id = format!("{}-model-{ordinal}-{action}", self.task);
        let expires = if let Some(budget) = budget {
            let mut clock = libc::timespec { tv_sec: 0, tv_nsec: 0 };
            if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut clock) } != 0
                || clock.tv_sec < 0 {
                return Err(failure());
            }
            let expires = (clock.tv_sec as u128) * 1_000_000_000
                + clock.tv_nsec as u128 + budget.as_nanos();
            Some(u64::try_from(expires).map_err(|_| failure())?)
        } else { None };
        let mut frame = json!({"version":self.version,"type":"model_request","request_id":id,
            "owner_user_id":self.owner,"task_id":self.task,"attempt_ordinal":ordinal,"action":action,
            "input_tokens":counts.input,"max_output_tokens":counts.output,"minimum_output_tokens":counts.minimum_output,
            "expires_monotonic_ns":expires});
        if let Some((usage, unsent)) = settlement {
            frame["usage"] = usage.cloned().unwrap_or(Value::Null);
            frame["unsent"] = json!(unsent);
        }
        self.schemas
            .validate_protocol(self.owner, "model_request", frame.clone())
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
                if reply.get("error").is_some() || (action != "reserve" && reply.get("output_tokens").is_some()) || (action != "dispatch" && reply.get("headers").is_some()) || ((action != "settle" || !self.pricing) && reply.get("price").is_some()) {
                    return Err(failure());
                }
                Ok(reply)
            };
            // Stop/deadline must also wake a wait after publication, when the
            // one host reply has not arrived yet. Settlement retains its owner.
            let owned_wait = async {
                tokio::select! {
                    biased;
                    _ = observation.cancelled(), if action != "settle" => Err(failure()),
                    reply = io => reply,
                }
            };
            match budget {
                Some(budget) => tokio::time::timeout(budget.saturating_sub(began.elapsed()), owned_wait)
                    .await.map_err(|_| failure())?,
                None => owned_wait.await,
            }
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
    fn settle(&mut self,usage:Option<&Value>,unsent:bool) -> Result<(),Value> {
        self.settle_with_cost(usage,unsent).map(|_|())
    }
    fn settle_with_cost(&mut self,usage:Option<&Value>,unsent:bool) -> Result<Option<Box<Value>>,Value> {
        let reply=self.port.exchange(self.ordinal,self.counts,"settle",&self.observation,Some((usage,unsent)))?;
        let Some(price)=reply.get("price") else {return Ok(None)};
        let raw=usage.ok_or_else(pricing_failure)?;
        if !tofu_runtime::usage::observed_totals(raw).map_err(|_|pricing_failure())?.complete {
            return Err(pricing_failure());
        }
        let tokens=tofu_runtime::pricing_usage::normalize(raw).map_err(|_|pricing_failure())?;
        let field=|name:&str| price[name].as_f64().ok_or_else(pricing_failure);
        if price["prompt_tokens"].as_u64()!=Some(tokens.total_input) {return Err(pricing_failure());}
        let resolved=tofu_runtime::pricing::ResolvedPrice {
            selected_prompt_tokens:tokens.total_input,input:field("input")?,output:field("output")?,
            cache_write_multiplier:field("cache_write_multiplier")?,cache_read_multiplier:field("cache_read_multiplier")?,
            usd_to_cny:field("usd_to_cny")?,currency:match price["currency"].as_str() {Some("USD")=>tofu_runtime::pricing::Currency::Usd,Some("CNY")=>tofu_runtime::pricing::Currency::Cny,_=>return Err(pricing_failure())}
        };
        let cost=tofu_runtime::pricing::compute(tokens,resolved).map_err(|_|pricing_failure())?;
        let credits=tofu_runtime::billing_credits::convert(cost.as_ref(),field("margin")?).map_err(|_|pricing_failure())?;
        Ok(Some(Box::new(json!({"amounts":cost.map(|cost|cost.numeric_projection()).unwrap_or(Value::Null),"price":price,"credits":{
            "input":credits.input,"output":credits.output,"cache_read":credits.cache_read,"cache_write":credits.cache_write,
            "base":credits.base,"margin":credits.margin,"total":credits.total}}))))
    }
}
