//! Native entry point. The first executable gate exercises owned native jobs.
#[cfg(target_os = "linux")]
mod cli_agent;
#[cfg(target_os = "linux")]
mod cli_agent_control;
mod cli_agent_worker;
#[cfg(target_os = "linux")]
mod cli_model_control;
#[cfg(target_os = "linux")]
mod cli_agent_rounds;
mod cli_http;
#[cfg(target_os = "linux")]
mod cli_token_counter;
#[cfg(target_os = "linux")]
mod cli_terminal;
#[cfg(target_os = "linux")]
mod cli_text;
#[cfg(target_os = "linux")]
mod cli_approval;
use tofu_runtime::executor::Executor;
fn check_runtime() -> Result<(), Box<dyn std::error::Error>> {
    let capacity = tofu_runtime::capacity::probe_linux(
        std::path::Path::new("/proc"),
        &std::env::current_dir()?,
    );
    let budget = capacity
        .executor_budget(tofu_runtime::capacity::Demand {
            worker_bytes: 2 * 1024 * 1024,
            job_bytes: 8,
            queued_jobs_per_worker: 2,
            memory_divisor: 4,
            cpu_units_per_worker: 1000,
        })
        .map_err(|e| format!("native capacity: {e:?}"))?;
    let workers = budget.workers;
    let jobs = budget.max_inflight_jobs;
    // This explicit check workload owns exactly one u64 per accepted job.
    let allocator = tofu_runtime::capacity_allocation::Allocator::new(capacity, 4)
        .map_err(|e| format!("native allocation: {e:?}"))?;
    let mut runtime = Executor::with_capacity(
        &allocator,
        tofu_runtime::capacity::Demand {
            worker_bytes: 2 * 1024 * 1024,
            job_bytes: 8,
            queued_jobs_per_worker: 2,
            memory_divisor: 1,
            cpu_units_per_worker: 1000,
        },
    )
    .map_err(|error| format!("native runtime configuration: {error:?}"))?;
    let handles = (0..jobs)
        .map(|index| runtime.submit(8, move || index as u64))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("native admission: {error:?}"))?;
    for (index, handle) in handles.into_iter().enumerate() {
        let value = handle
            .wait()
            .map_err(|error| format!("native execution: {error:?}"))?;
        if value != index as u64 {
            return Err("native result order differs".into());
        }
    }
    let peak_resident_workers = runtime.resident_workers();
    runtime.close();
    let allocation = allocator
        .snapshot()
        .map_err(|e| format!("native allocation: {e:?}"))?;
    if allocation.pools != 0 || allocation.weighted_bytes != 0 || allocation.workers != 0 {
        return Err("native pool allocation leaked".into());
    }
    let state = runtime
        .snapshot()
        .map_err(|error| format!("native snapshot: {error:?}"))?;
    if state.inflight_jobs != 0 || state.inflight_bytes != 0 {
        return Err("native admission leaked".into());
    }
    println!(
        "{{\"runtime\":\"rust\",\"worker_capacity\":{workers},\"peak_resident_workers\":{peak_resident_workers},\"completed_jobs\":{},\"closed\":true}}",
        state.completed_jobs
    );
    Ok(())
}

fn check_session() -> Result<(), Box<dyn std::error::Error>> {
    use tofu_runtime::execution_contract::{ExecutionPhase as Phase, ResourceName};
    use tofu_runtime::session::{Registry, ReleaseAck};
    let registry = Registry::new();
    let session = registry
        .start("native-lifecycle-check", "native_check", 7, "", None)
        .map_err(|error| format!("native identity: {error:?}"))?;
    session
        .bind_acquired(
            ResourceName::Admission,
            100,
            true,
            Box::new(|_| Ok(ReleaseAck::Released)),
            Box::new(|| ()),
            Some(Phase::Admitted),
        )
        .map_err(|error| format!("native binding: {error:?}"))?;
    session
        .advance(Phase::Dispatching)
        .map_err(|error| format!("native dispatch: {error:?}"))?;
    let receipt = session
        .settle(Phase::Completed, "native_check")
        .map_err(|error| format!("native settlement: {error:?}"))?;
    if !receipt.invariants_satisfied
        || !registry
            .snapshots(1)
            .map_err(|error| format!("native registry: {error:?}"))?
            .is_empty()
    {
        return Err("native terminal invariant failed".into());
    }
    println!(
        "{{\"execution_phase\":\"{}\",\"invariants_satisfied\":true,\"released_resources\":{}}}",
        receipt.outcome.as_str(),
        receipt.resource_dispositions.len()
    );
    Ok(())
}
// Native stdin composition for an already authorized Chat-compatible attempt.
// HTTP, credential routing and the agent tool loop remain separate owners.
fn run_chat_stream() -> Result<(), Box<dyn std::error::Error>> {
    use serde_json::{json, Value};
    use std::io::Read;
    use std::sync::{Arc, Mutex};
    use tofu_runtime::provider::{Budget, ChatStream};
    use tofu_runtime::session::Registry;
    use tofu_runtime::task::{PortError, Task, TaskIdentity, TerminalCandidate, TerminalPublisher};
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 6 && args.len() != 7 {
        return Err(
            "usage: tofu-native run-chat-stream OWNER TASK_ID OUTPUT_BYTES TOOL_CALLS [IDENTICAL_LIMIT]".into(),
        );
    }
    let owner = args[2].parse::<u64>()?;
    let identical_limit = args
        .get(6)
        .map(|v| v.parse::<usize>())
        .transpose()?
        .unwrap_or(tofu_runtime::execution_contract::DEFAULT_IDENTICAL_TOOL_CALLS);
    let budget = Budget {
        event_bytes: 1 << 20,
        output_bytes: args[4].parse()?,
        tool_calls: args[5].parse()?,
    };
    struct Output(Mutex<Option<Value>>);
    impl TerminalPublisher for Output {
        fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
            let mut current = self.0.lock().map_err(|_| PortError)?;
            if let Some(value) = current.as_ref() {
                if value != event {
                    return Err(PortError);
                }
            } else {
                *current = Some(event.clone());
            }
            Ok(())
        }
    }
    let registry = Registry::new();
    let session = registry
        .start(&args[3], "provider_stdin", owner, "", None)
        .map_err(|e| format!("native session: {e:?}"))?;
    let output = Arc::new(Output(Mutex::new(None)));
    let task = Task::new(
        TaskIdentity {
            task_id: args[3].clone(),
            owner_user_id: owner,
        },
        session,
        None,
        output.clone(),
    )
    .map_err(|e| format!("native task: {e:?}"))?;
    let candidate = task
        .run(owner, |_| {
            let mut stream =
                ChatStream::with_tool_limit(budget, identical_limit).map_err(|e| e.task_error())?;
            let mut input = std::io::stdin().lock();
            let mut bytes = [0u8; 65536];
            loop {
                let n = input
                    .read(&mut bytes)
                    .map_err(|_| json!({"kind":"provider_stream","message":"input read failed"}))?;
                if n == 0 {
                    break;
                }
                if stream.feed(&bytes[..n]).map_err(|e| e.task_error())? {
                    break;
                }
            }
            stream
                .finish(false)
                .map_err(|e| e.task_error())?
                .into_task_result()
        })
        .map_err(|e| format!("native terminal debt: {e:?}"))?;
    println!(
        "{}",
        output
            .0
            .lock()
            .map_err(|_| "native output poisoned")?
            .as_ref()
            .ok_or("missing native terminal event")?
    );
    if candidate.error.is_some() {
        return Err("provider attempt did not complete".into());
    }
    Ok(())
}
fn check_tool_gateway() -> Result<(), Box<dyn std::error::Error>> {
    use serde_json::{json, Value};
    use std::sync::Arc;
    use tofu_runtime::tool_contract::*;
    struct ReadPolicy;
    impl Authorization for ReadPolicy {
        fn allow(
            &self,
            owner: u64,
            name: &str,
            permission: Permission,
            _: &Value,
            _: Option<&Value>,
        ) -> bool {
            owner == 7 && name == "native_check" && permission == Permission::Read
        }
    }
    struct Echo;
    impl Handler for Echo {
        fn invoke(&self, context: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
            assert_eq!(context.owner, 7);
            Ok(value.clone())
        }
    }
    let documents = json!({"native_check":{"name":"native_check","contractVersion":tofu_runtime::execution_contract::TOOL_CONTRACT_VERSION,"permission":"read","arguments_schema":{"type":"object","properties":{"value":{"type":"integer","default":42}},"additionalProperties":false}}});
    let catalog = Catalog::compile(7, "native-check", &documents).map_err(|e| e.message)?;
    if !matches!(
        catalog.admit(7, "native_check", json!({}), &Value::Null, &DenyAll),
        Err(AdmissionError::Denied)
    ) {
        return Err("schema validity granted authority".into());
    }
    let call = catalog
        .admit(7, "native_check", json!({}), &Value::Null, &ReadPolicy)
        .map_err(|e| format!("native admission: {e:?}"))?;
    let registry = Registry::new(
        7,
        "native-check",
        vec![("native_check".into(), Arc::new(Echo))],
    )
    .map_err(|e| format!("native registry: {e:?}"))?;
    let value = registry
        .dispatch(call, &ReadPolicy)
        .map_err(|e| format!("native dispatch: {e:?}"))?;
    if value != json!({"value":42}) {
        return Err("native default/dispatch mismatch".into());
    }
    println!("{{\"native_tool_gateway\":true,\"default_deny\":true,\"executed\":1}}");
    Ok(())
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    match std::env::args().nth(1).as_deref() {
        #[cfg(target_os = "linux")]
        Some("process-worker") => tofu_runtime::process_supervisor::worker_main(),
        Some("build-info") => {
            let contract: serde_json::Value = serde_json::from_str(include_str!(
                "../../../contracts/native_agent_stdio_v1.schema.json"
            ))?;
            println!(
                "{}",
                serde_json::json!({
                    "format":serde_json::from_str::<serde_json::Value>(include_str!("../../../contracts/native_runtime_artifact_v1.json"))?["build_format"],
                    "application_version":include_str!("../../../VERSION").trim(),
                    "runtime_version":env!("CARGO_PKG_VERSION"),
                    "target_os":std::env::consts::OS,"target_arch":std::env::consts::ARCH,
                    "stdio_contract":contract["$id"],"runtime":"rust"
                })
            );
            Ok(())
        }
        Some("check-runtime") => check_runtime(),
        Some("probe-capacity") => {
            let volume = std::env::args()
                .nth(2)
                .map(std::path::PathBuf::from)
                .unwrap_or(std::env::current_dir()?);
            println!(
                "{}",
                tofu_runtime::capacity::probe_linux(std::path::Path::new("/proc"), &volume)
                    .to_value()
            );
            Ok(())
        }
        Some("check-session") => check_session(),
        Some("check-tool-gateway") => check_tool_gateway(),
        Some("run-chat-stream") => run_chat_stream(),
        Some("run-http-chat") => cli_http::run(),
        #[cfg(target_os = "linux")]
        Some("run-http-agent") => cli_agent::run(),
        #[cfg(target_os = "linux")]
        Some("run-http-agent-worker") => cli_agent_worker::run(),
        _ => {
            eprintln!("usage: tofu-native probe-capacity [VOLUME] | check-runtime | check-session | run-chat-stream OWNER TASK_ID OUTPUT_BYTES TOOL_CALLS [IDENTICAL_LIMIT]");
            std::process::exit(2);
        }
    }
}
