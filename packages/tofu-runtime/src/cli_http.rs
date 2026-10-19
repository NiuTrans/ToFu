//! Native authorized HTTP attempt CLI composition. Input declares identity,
//! endpoint, frozen headers/body and all pool/resource budgets; no routing store.
use serde_json::Value;
use std::{
    io::Read,
    sync::{Arc, Mutex},
    time::Duration,
};
use tofu_runtime::{
    http::{Cancellation, Policy, Request, Transport},
    provider::Budget,
    session::Registry,
    task::{PortError, Task, TaskIdentity, TerminalCandidate, TerminalPublisher},
};
pub(crate) fn number(v: &Value, key: &str) -> Result<u64, Box<dyn std::error::Error>> {
    v[key]
        .as_u64()
        .ok_or_else(|| format!("missing integer configuration: {key}").into())
}
pub(crate) fn size(v: &Value, key: &str) -> Result<usize, Box<dyn std::error::Error>> {
    Ok(usize::try_from(number(v, key)?)?)
}
pub(crate) fn duration(v: &Value, key: &str) -> Result<Duration, Box<dyn std::error::Error>> {
    Ok(Duration::from_millis(number(v, key)?))
}
pub(crate) fn nullable_duration(v: &Value, key: &str) -> Result<Option<Duration>, Box<dyn std::error::Error>> {
    match v.get(key) {
        Some(Value::Null) => Ok(None),
        _ => Ok(Some(duration(v, key)?)),
    }
}
pub(crate) struct Output(pub(crate) Mutex<Option<Value>>);
impl TerminalPublisher for Output {
    fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
        let mut output = self.0.lock().map_err(|_| PortError)?;
        if let Some(previous) = output.as_ref() {
            if previous != event {
                return Err(PortError);
            }
        } else {
            *output = Some(event.clone());
        }
        Ok(())
    }
}
pub(crate) fn policy(input: &Value) -> Result<Policy, Box<dyn std::error::Error>> {
    let p = &input["policy"];
    let policy = Policy {
        connections: size(p, "connections")?,
        idle_connections: size(p, "idle_connections")?,
        keepalive: duration(p, "keepalive_ms")?,
        connect: duration(p, "connect_ms")?,
        request_headers: match p.get("request_headers_ms") {
            Some(Value::Null) => None,
            _ => Some(duration(p, "request_headers_ms")?),
        },
        write: match p.get("write_ms") {
            None | Some(Value::Null) => None,
            _ => Some(duration(p, "write_ms")?),
        },
        pool_wait: duration(p, "pool_wait_ms")?,
        stream_idle: if p["stream_idle_ms"].is_null() {
            None
        } else {
            Some(duration(p, "stream_idle_ms")?)
        },
        drain_timeout: duration(p, "drain_timeout_ms")?,
        drain_bytes: size(p, "drain_bytes")?,
        drain_chunks: size(p, "drain_chunks")?,
    };
    Ok(policy)
}
pub(crate) fn headers(
    input: &Value,
) -> Result<reqwest::header::HeaderMap, Box<dyn std::error::Error>> {
    let mut headers = reqwest::header::HeaderMap::new();
    if let Some(object) = input["headers"].as_object() {
        for (name, value) in object {
            headers.insert(
                reqwest::header::HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| "invalid header name")?,
                reqwest::header::HeaderValue::from_str(
                    value.as_str().ok_or("invalid header value")?,
                )
                .map_err(|_| "invalid header value")?,
            );
        }
    } else if !input["headers"].is_null() {
        return Err("invalid headers".into());
    }
    Ok(headers)
}
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 {
        return Err(
            "usage: tofu-native run-http-chat CONFIG_BYTES_LIMIT (explicit attempt JSON on stdin)"
                .into(),
        );
    }
    let limit: u64 = args[2].parse()?;
    if limit == 0 || limit == u64::MAX {
        return Err("invalid configuration byte limit".into());
    }
    let mut bytes = Vec::new();
    std::io::stdin().take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err("configuration byte limit exceeded".into());
    }
    let input: Value = serde_json::from_slice(&bytes)?;
    let policy = policy(&input)?;
    let endpoint = input["endpoint"].as_str().ok_or("missing endpoint")?;
    let proxy = input["proxy"].as_str();
    if !input["proxy"].is_null() && proxy.is_none() {
        return Err("invalid proxy".into());
    }
    let headers = headers(&input)?;
    if !input["body"].is_object() {
        return Err("request body must be a canonical object".into());
    }
    let owner = number(&input, "owner_user_id")?;
    let task_id = input["task_id"].as_str().ok_or("missing task_id")?;
    let request = Request {
        tool_names: Arc::new(tofu_runtime::tool_assembly::wire_tool_names(
            &input["body"]["tools"],
        )),
        identical_tool_limit: if input["identical_tool_limit"].is_null() {
            None
        } else {
            Some(size(&input, "identical_tool_limit")?)
        },
        headers,
        body: serde_json::to_vec(&input["body"])?,
        budget: Budget {
            event_bytes: size(&input, "event_bytes")?,
            output_bytes: size(&input, "output_bytes")?,
            tool_calls: size(&input, "tool_calls")?,
        },
    };
    if policy.connections == 0 {
        return Err("invalid connection capacity".into());
    }
    let runtime = tofu_runtime::reactor::build()?;
    let transport = {
        let _entered = runtime.enter();
        Transport::new(endpoint, proxy, policy)
            .map_err(|e| format!("transport configuration: {e:?}"))?
    };
    let registry = Registry::new();
    let session = registry
        .start(task_id, "http_attempt", owner, "", None)
        .map_err(|e| format!("session: {e:?}"))?;
    let output = Arc::new(Output(Mutex::new(None)));
    let task = Task::new(
        TaskIdentity {
            task_id: task_id.into(),
            owner_user_id: owner,
        },
        session,
        None,
        output.clone(),
    )
    .map_err(|e| format!("task: {e:?}"))?;
    let candidate = transport
        .run_task(&runtime, &task, owner, request, &Cancellation::new())
        .map_err(|e| format!("terminal debt: {e:?}"))?;
    println!(
        "{}",
        output
            .0
            .lock()
            .map_err(|_| "output poisoned")?
            .as_ref()
            .ok_or("missing terminal publication")?
    );
    if candidate.error.is_some() {
        return Err("HTTP provider attempt failed".into());
    }
    Ok(())
}
