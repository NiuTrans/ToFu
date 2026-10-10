//! Native multi-round composition, real HTTP wire feedback and stop fences.
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tofu_runtime::{
    agent::{Agent, ApprovalDecision, Budget, Feedback, Provider, ProviderError, ToolFailure},
    execution_contract::ProviderStreamState,
    provider::StreamResult,
    session::{ExecutionSession, Registry as Sessions},
    task::{PortError, Task, TaskIdentity, TaskStatus, TerminalCandidate, TerminalPublisher},
    tool_contract::*,
};
struct Allow;
impl Authorization for Allow {
    fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        owner == 7
    }
}
#[derive(Default)]
struct Published(Mutex<Vec<Value>>);
impl TerminalPublisher for Published {
    fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
        self.0.lock().unwrap().push(event.clone());
        Ok(())
    }
}
struct Text;
impl Feedback for Text {
    fn content(&self, result: Result<Value, ToolFailure>, _: usize) -> Result<String, Value> {
        match result {
            Ok(Value::String(text)) => Ok(text),
            Err(ToolFailure::Admission(_)) => Ok("denied".into()),
            Err(ToolFailure::Dispatch(_)) => Ok("failed".into()),
            Err(ToolFailure::ApprovalDenied(reason)) => Ok(reason.to_string()),
            _ => Err(json!({"kind":"unsupported_projection"})),
        }
    }
}
struct Echo(Arc<AtomicUsize>);
impl Handler for Echo {
    fn invoke(&self, context: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
        assert_eq!(context.owner, 7);
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(Value::String(value["value"].to_string()))
    }
}
#[cfg(target_os = "linux")]
struct ProcessEcho(Arc<AtomicUsize>);
#[cfg(target_os = "linux")]
impl Handler for ProcessEcho {
    fn invoke(&self, context: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
        use tofu_runtime::{
            process_boundary::ResourceLimits,
            process_supervisor::{execute, Request},
        };
        self.0.fetch_add(1, Ordering::SeqCst);
        let request = Request {
            executable: "/bin/echo".into(),
            arguments: vec![value["value"].to_string()],
            directory: "/tmp".into(),
            environment: Default::default(),
            stdout_bytes: 4096,
            stderr_bytes: 4096,
            timeout: Duration::from_secs(2),
            termination_grace: Duration::from_millis(20),
            poll_interval: Duration::from_millis(10),
            config_bytes: 65536,
            limits: Some(ResourceLimits {
                address_space_bytes: 64 * 1024 * 1024,
                cpu_seconds: 1,
                file_bytes: 4096,
                file_descriptors: 64,
            }),
        };
        let output = execute(
            std::path::Path::new(env!("CARGO_BIN_EXE_tofu-native")),
            &request,
            || context.runtime.checkpoint().is_err(),
        )
        .unwrap();
        assert_eq!(output.metadata["exit_code"], 0);
        assert_eq!(output.metadata["reaped"], true);
        Ok(Value::String(String::from_utf8(output.stdout).unwrap()))
    }
}
fn catalog() -> Catalog {
    Catalog::compile(7,"epoch",&json!({"echo":{"name":"echo","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object","properties":{"value":{"type":"integer","default":42}},"additionalProperties":false}}})).unwrap()
}
fn budget() -> Budget {
    Budget {
        rounds: 3,
        tool_calls: 8,
        retained_bytes: 65536,
        feedback_bytes: 4096,
    }
}
fn task(sessions: &Sessions, deadline: Option<Duration>) -> (Arc<Task>, Arc<Published>) {
    task_with_persistence(sessions,deadline,None)
}
fn task_with_persistence(sessions:&Sessions,deadline:Option<Duration>,persistence:Option<Arc<dyn tofu_runtime::task::TerminalPersistence>>)->(Arc<Task>,Arc<Published>){
    let session = sessions.start("agent", "task", 7, "", deadline).unwrap();
    let output = Arc::new(Published::default());
    (
        Arc::new(
            Task::new(
                TaskIdentity {
                    task_id: "agent".into(),
                    owner_user_id: 7,
                },
                session,
                persistence,
                output.clone(),
            )
            .unwrap(),
        ),
        output,
    )
}
fn response(message: Value) -> StreamResult {
    StreamResult {
        state: ProviderStreamState::ProviderFinished,
        message,
        usage: json!({"total_tokens":1}),
        finish_reason: Some("stop".into()),
        done_seen: true,
        malformed_frames: 0,
    }
}
fn tools(args: &str) -> Value {
    json!({"role":"assistant","tool_calls":[{"id":"a1","type":"function","function":{"name":"echo","arguments":args}}]})
}
struct Rounds {
    values: Mutex<VecDeque<Value>>,
    seen: Mutex<Vec<Vec<Value>>>,
}
impl Provider for Rounds {
    fn round(
        &self,
        messages: &[Value],
        _: &Arc<ExecutionSession>,
    ) -> Result<StreamResult, ProviderError> {
        self.seen.lock().unwrap().push(messages.to_vec());
        Ok(response(self.values.lock().unwrap().pop_front().unwrap()))
    }
}
fn rounds(values: Vec<Value>) -> Rounds {
    Rounds {
        values: Mutex::new(values.into()),
        seen: Mutex::new(vec![]),
    }
}
fn run(
    provider: &dyn Provider,
    authorization: &dyn Authorization,
    b: Budget,
) -> (Arc<TerminalCandidate>, usize) {
    let calls = Arc::new(AtomicUsize::new(0));
    run_with_handler(
        provider,
        authorization,
        b,
        Arc::new(Echo(calls.clone())),
        calls,
    )
}
fn run_with_handler(
    provider: &dyn Provider,
    authorization: &dyn Authorization,
    b: Budget,
    handler: Arc<dyn Handler>,
    calls: Arc<AtomicUsize>,
) -> (Arc<TerminalCandidate>, usize) {
    run_with_handler_progress(provider, authorization, b, handler, calls, None)
}
fn run_with_handler_progress(
    provider: &dyn Provider,
    authorization: &dyn Authorization,
    b: Budget,
    handler: Arc<dyn Handler>,
    calls: Arc<AtomicUsize>,
    progress: Option<&dyn tofu_runtime::progress::Policy>,
) -> (Arc<TerminalCandidate>, usize) {
    run_with_handler_recovery(provider, authorization, b, handler, calls, progress, None)
}
#[allow(clippy::too_many_arguments)]
fn run_with_handler_recovery(
    provider: &dyn Provider,
    authorization: &dyn Authorization,
    b: Budget,
    handler: Arc<dyn Handler>,
    calls: Arc<AtomicUsize>,
    progress: Option<&dyn tofu_runtime::progress::Policy>,
    checkpoints: Option<&dyn tofu_runtime::agent_checkpoint::Port>,
) -> (Arc<TerminalCandidate>, usize) {
    let sessions = Sessions::new();
    let (t, output) = task(&sessions, None);
    let handlers = Registry::new(7, "epoch", vec![("echo".into(), handler)]).unwrap();
    let catalog = catalog();
    let agent = Agent {
        provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints,
        progress,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: b,
    };
    let result = agent
        .run(
            &t,
            7,
            "agent",
            vec![json!({"role":"user","content":"hello"})],
        )
        .unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
    assert_eq!(output.0.lock().unwrap().len(), 1);
    (result, calls.load(Ordering::SeqCst))
}
#[test]
fn ordered_feedback_continues_and_terminalizes_once() {
    let p = rounds(vec![
        tools("{}"),
        json!({"role":"assistant","content":"done"}),
    ]);
    let (r, c) = run(&p, &Allow, budget());
    assert_eq!(r.status, TaskStatus::Done);
    assert_eq!(c, 1);
    let seen = p.seen.lock().unwrap();
    assert_eq!(seen.len(), 2);
    assert_eq!(
        seen[1][2],
        json!({"role":"tool","tool_call_id":"a1","content":"42"})
    );
    assert_eq!(r.result.as_ref().unwrap()["llm_rounds"], 2);
    assert_eq!(
        r.result.as_ref().unwrap()["usage"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}
#[test]
fn denied_tools_do_not_execute_but_model_gets_feedback() {
    let p = rounds(vec![
        tools("{}"),
        json!({"role":"assistant","content":"denial explained"}),
    ]);
    let (r, c) = run(&p, &DenyAll, budget());
    assert_eq!(r.status, TaskStatus::Done);
    assert_eq!(c, 0);
    assert_eq!(p.seen.lock().unwrap()[1][2]["content"], "denied");
}
#[test]
fn entire_batch_preflights_before_effects() {
    for malformed in ["{", "{\"value\":18446744073709551616000}"] {
        let mut m = tools("{}");
        m["tool_calls"].as_array_mut().unwrap().push(
            json!({"id":"a2","type":"function","function":{"name":"echo","arguments":malformed}}),
        );
        let p = rounds(vec![m]);
        let (r, c) = run(&p, &Allow, budget());
        assert_eq!(r.status, TaskStatus::Error);
        assert_eq!(c, 0);
        assert_eq!(p.seen.lock().unwrap().len(), 1);
    }
}
#[test]
fn duplicate_ids_and_missing_continuation_budget_prevent_effects() {
    let mut m = tools("{}");
    let duplicate = m["tool_calls"][0].clone();
    m["tool_calls"].as_array_mut().unwrap().push(duplicate);
    let p = rounds(vec![m]);
    let (r, c) = run(&p, &Allow, budget());
    assert_eq!(r.status, TaskStatus::Error);
    assert_eq!(c, 0);
    let p = rounds(vec![tools("{}")]);
    let mut b = budget();
    b.rounds = 1;
    let (r, c) = run(&p, &Allow, b);
    assert_eq!(r.error.as_ref().unwrap()["kind"], "agent_round_budget");
    assert_eq!(c, 0);
}
#[test]
fn context_budget_stops_before_provider_dispatch() {
    let p = rounds(vec![]);
    let mut b = budget();
    b.retained_bytes = 4;
    let (r, c) = run(&p, &Allow, b);
    assert_eq!(r.status, TaskStatus::Error);
    assert_eq!(c, 0);
    assert!(p.seen.lock().unwrap().is_empty());
}

fn policy() -> tofu_runtime::http::Policy {
    tofu_runtime::http::Policy {
        connections: 1,
        idle_connections: 1,
        keepalive: Duration::from_secs(1),
        connect: Duration::from_secs(1),
        request_headers: Duration::from_secs(1),
        pool_wait: Duration::from_secs(1),
        stream_idle: None,
        drain_timeout: Duration::from_millis(20),
        drain_bytes: 65536,
        drain_chunks: 32,
    }
}
fn read_request(stream: &mut std::net::TcpStream) -> Value {
    use std::io::Read;
    let mut headers = vec![];
    while !headers.ends_with(b"\r\n\r\n") {
        let mut b = [0];
        stream.read_exact(&mut b).unwrap();
        headers.push(b[0]);
        assert!(headers.len() < 16384);
    }
    let text = String::from_utf8(headers).unwrap();
    assert_eq!(text.lines().filter(|line|line.to_ascii_lowercase().starts_with("content-type:")).count(),1);
    let size: usize = text
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length:")
                .map(|v| v.trim().parse().unwrap())
        })
        .unwrap();
    assert!(size < 65536);
    let mut body = vec![0; size];
    stream.read_exact(&mut body).unwrap();
    serde_json::from_slice(&body).unwrap()
}
fn provider_config() -> tofu_runtime::agent_http::Configuration {
    tofu_runtime::agent_http::Configuration{headers:Default::default(),template:json!({"model":"fixture","tools":[{"type":"function","function":{"name":"echo","parameters":{"type":"object"}}}]}).as_object().unwrap().clone(),budget:tofu_runtime::provider::Budget{event_bytes:65536,output_bytes:65536,tool_calls:8},request_bytes:65536,identical_tool_limit:None}
}
#[test]
fn real_http_model_tool_feedback_model_flow_reuses_pool() {
    use std::io::Write;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/chat", listener.local_addr().unwrap());
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut bodies = vec![];
        let replies=["data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"a1\",\"type\":\"function\",\"function\":{\"name\":\"echo\",\"arguments\":\"{}\"}}]},\"finish_reason\":\"tool_calls\"}]}\n\ndata: [DONE]\n\n","data: {\"choices\":[{\"delta\":{\"content\":\"done\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"];
        for reply in replies {
            bodies.push(read_request(&mut stream));
            stream.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{reply}",reply.len()).as_bytes()).unwrap();
        }
        bodies
    });
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let transport = {
        let _enter = runtime.enter();
        tofu_runtime::http::Transport::new(&endpoint, None, policy()).unwrap()
    };
    let cancel = tofu_runtime::http::Cancellation::new();
    let provider = tofu_runtime::agent_http::HttpProvider::new(
        &runtime,
        &transport,
        provider_config(),
        &cancel,
    )
    .unwrap();
    #[cfg(target_os = "linux")]
    let (r, c) = {
        let calls = Arc::new(AtomicUsize::new(0));
        run_with_handler(
            &provider,
            &Allow,
            budget(),
            Arc::new(ProcessEcho(calls.clone())),
            calls,
        )
    };
    #[cfg(not(target_os = "linux"))]
    let (r, c) = run(&provider, &Allow, budget());
    assert_eq!(r.status, TaskStatus::Done);
    assert_eq!(c, 1);
    assert_eq!(transport.available_slots(), 1);
    let bodies = server.join().unwrap();
    assert_eq!(bodies.len(), 2);
    #[cfg(target_os = "linux")]
    assert_eq!(bodies[1]["messages"][2]["content"], "42\n");
    #[cfg(not(target_os = "linux"))]
    assert_eq!(bodies[1]["messages"][2]["content"], "42");
    assert_eq!(bodies[1]["stream"], true);
}

#[test]
fn parent_abort_stops_live_tool_before_another_model_round() {
    use tofu_runtime::{execution_contract::ResourceName, session::ReleaseAck};
    struct Wait {
        entered: std::sync::mpsc::Sender<()>,
        released: Arc<AtomicUsize>,
    }
    impl Handler for Wait {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            let released = self.released.clone();
            context
                .runtime
                .bind_resource(
                    ResourceName::ToolEnvironment,
                    0,
                    false,
                    Box::new(move |_| {
                        released.fetch_add(1, Ordering::SeqCst);
                        Ok(ReleaseAck::Released)
                    }),
                    Box::new(|| {}),
                )
                .unwrap();
            self.entered.send(()).unwrap();
            let until = std::time::Instant::now() + Duration::from_secs(2);
            while context.runtime.checkpoint().is_ok() {
                assert!(std::time::Instant::now() < until);
                std::thread::sleep(Duration::from_millis(2));
            }
            assert_eq!(self.released.load(Ordering::SeqCst), 0);
            Ok(json!("late"))
        }
    }
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, None);
    let worker_task = t.clone();
    let released = Arc::new(AtomicUsize::new(0));
    let worker_released = released.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    let thread = std::thread::spawn(move || {
        let provider = rounds(vec![tools("{}")]);
        let catalog = catalog();
        let handlers = Registry::new(
            7,
            "epoch",
            vec![(
                "echo".into(),
                Arc::new(Wait {
                    entered: tx,
                    released: worker_released,
                }),
            )],
        )
        .unwrap();
        let agent = Agent {
            provider: &provider,
            catalog: &catalog,
            handlers: &handlers,
            authorization: &Allow,
            feedback: &Text,
            sessions: &sessions,
            epoch: "epoch",
            checkpoints: None,
            progress: None,
            scheduling: None,
            approvals: None,
            round_admission: None,
            budget: budget(),
        };
        let result = agent.run(&worker_task, 7, "agent", vec![]).unwrap();
        assert_eq!(provider.seen.lock().unwrap().len(), 1);
        assert_eq!(sessions.active_count().unwrap(), 0);
        result
    });
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(released.load(Ordering::SeqCst), 0);
    t.request_abort(7, "user_stop").unwrap();
    let result = thread.join().unwrap();
    assert_eq!(result.status, TaskStatus::Aborted);
    assert!(result.result.is_none());
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(published.0.lock().unwrap().len(), 1);
}
#[test]
fn silent_http_observes_parent_deadline_and_releases_pool() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/chat", listener.local_addr().unwrap());
    let server = std::thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        read_request(&mut socket);
        std::thread::sleep(Duration::from_millis(150));
    });
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let transport = {
        let _entered = runtime.enter();
        tofu_runtime::http::Transport::new(&endpoint, None, policy()).unwrap()
    };
    let cancel = tofu_runtime::http::Cancellation::new();
    let provider = tofu_runtime::agent_http::HttpProvider::new(
        &runtime,
        &transport,
        provider_config(),
        &cancel,
    )
    .unwrap();
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, Some(Duration::from_millis(80)));
    let catalog = catalog();
    let calls = Arc::new(AtomicUsize::new(0));
    let handlers = Registry::new(
        7,
        "epoch",
        vec![("echo".into(), Arc::new(Echo(calls.clone())))],
    )
    .unwrap();
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: budget(),
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(result.status, TaskStatus::Aborted);
    assert!(result.result.is_none());
    assert_eq!(transport.available_slots(), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(sessions.active_count().unwrap(), 0);
    assert_eq!(published.0.lock().unwrap().len(), 1);
    server.join().unwrap();
}
#[test]
fn failed_tool_cleanup_is_fatal_even_if_feedback_would_hide_it() {
    use tofu_runtime::execution_contract::ResourceName;
    struct Fault;
    impl Handler for Fault {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            context
                .runtime
                .bind_resource(
                    ResourceName::ToolEnvironment,
                    0,
                    false,
                    Box::new(|_| Err(tofu_runtime::session::ReleaseError)),
                    Box::new(|| {}),
                )
                .unwrap();
            Ok(json!("late"))
        }
    }
    let sessions = Sessions::new();
    let (t, _) = task(&sessions, None);
    let p = rounds(vec![tools("{}")]);
    let catalog = catalog();
    let handlers = Registry::new(7, "epoch", vec![("echo".into(), Arc::new(Fault))]).unwrap();
    let agent = Agent {
        provider: &p,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: budget(),
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(result.status, TaskStatus::Error);
    assert_eq!(result.error.as_ref().unwrap()["kind"], "agent_tool_cleanup");
    assert_eq!(p.seen.lock().unwrap().len(), 1);
}
#[test]
fn feedback_budget_and_binding_errors_prevent_more_dispatches() {
    let p = rounds(vec![tools("{}")]);
    let mut b = budget();
    b.feedback_bytes = 1;
    let (r, c) = run(&p, &Allow, b);
    assert_eq!(c, 1);
    assert_eq!(r.error.as_ref().unwrap()["kind"], "agent_feedback_budget");
    assert_eq!(p.seen.lock().unwrap().len(), 1);
    let sessions = Sessions::new();
    let (t, _) = task(&sessions, None);
    let catalog = catalog();
    let handlers = Registry::new(8, "foreign", vec![]).unwrap();
    let p = rounds(vec![]);
    let agent = Agent {
        provider: &p,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: budget(),
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(result.status, TaskStatus::Error);
    assert!(p.seen.lock().unwrap().is_empty());
}

#[test]
fn request_body_limit_refuses_before_http_and_duplicate_run_never_reexecutes() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let transport = {
        let _entered = runtime.enter();
        tofu_runtime::http::Transport::new("http://127.0.0.1:9/chat", None, policy()).unwrap()
    };
    let cancel = tofu_runtime::http::Cancellation::new();
    let mut config = provider_config();
    config.request_bytes = 1;
    let provider =
        tofu_runtime::agent_http::HttpProvider::new(&runtime, &transport, config, &cancel).unwrap();
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, None);
    let catalog = catalog();
    let handlers = Registry::new(7, "epoch", vec![]).unwrap();
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: budget(),
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(
        result.error.as_ref().unwrap()["kind"],
        "agent_request_budget"
    );
    assert!(agent.run(&t, 7, "agent", vec![]).is_err());
    assert_eq!(published.0.lock().unwrap().len(), 1);
    assert_eq!(transport.available_slots(), 1);
}

#[test]
fn authorization_revocation_between_siblings_blocks_later_effects() {
    use std::sync::atomic::AtomicBool;
    struct Policy(Arc<AtomicBool>);
    impl Authorization for Policy {
        fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
            owner == 7 && self.0.load(Ordering::SeqCst)
        }
    }
    struct Revoke {
        allowed: Arc<AtomicBool>,
        calls: Arc<AtomicUsize>,
    }
    impl Handler for Revoke {
        fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.allowed.store(false, Ordering::SeqCst);
            Ok(json!("first"))
        }
    }
    let sessions = Sessions::new();
    let (t, _) = task(&sessions, None);
    let allowed = Arc::new(AtomicBool::new(true));
    let calls = Arc::new(AtomicUsize::new(0));
    let policy = Policy(allowed.clone());
    let catalog = catalog();
    let handlers = Registry::new(
        7,
        "epoch",
        vec![(
            "echo".into(),
            Arc::new(Revoke {
                allowed,
                calls: calls.clone(),
            }),
        )],
    )
    .unwrap();
    let mut message = tools("{}");
    let mut second = message["tool_calls"][0].clone();
    second["id"] = json!("a2");
    message["tool_calls"].as_array_mut().unwrap().push(second);
    let provider = rounds(vec![message, json!({"role":"assistant","content":"done"})]);
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &policy,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: budget(),
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(result.status, TaskStatus::Done);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let seen = provider.seen.lock().unwrap();
    assert_eq!(seen[1][2]["content"], "failed");
    assert_eq!(sessions.active_count().unwrap(), 0);
}

#[test]
fn parallel_agent_joins_batch_and_restores_feedback_order() {
    struct BarrierEcho(Arc<std::sync::Barrier>, Arc<AtomicUsize>);
    impl Handler for BarrierEcho {
        fn invoke(&self, _: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
            self.1.fetch_add(1, Ordering::SeqCst);
            self.0.wait();
            Ok(Value::String(value["value"].to_string()))
        }
    }
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, None);
    let catalog = catalog();
    let calls = Arc::new(AtomicUsize::new(0));
    let handlers = Registry::new(
        7,
        "epoch",
        vec![(
            "echo".into(),
            Arc::new(BarrierEcho(
                Arc::new(std::sync::Barrier::new(2)),
                calls.clone(),
            )),
        )],
    )
    .unwrap();
    let scheduler = tofu_runtime::tool_scheduler::Scheduler::new(
        tofu_runtime::tool_scheduler::Budget {
            workers: 2,
            inflight_calls: 8,
            inflight_bytes: 65536,
        },
        std::collections::BTreeSet::from(["echo".into()]),
    )
    .unwrap();
    let mut message = tools("{\"value\":1}");
    message["tool_calls"].as_array_mut().unwrap().push(json!({"id":"two","type":"function","function":{"name":"echo","arguments":"{\"value\":2}"}}));
    let provider = rounds(vec![message, json!({"role":"assistant","content":"done"})]);
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: Some(&scheduler),
        approvals: None,
        round_admission: None,
        budget: budget(),
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(result.status, TaskStatus::Done);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    let seen = provider.seen.lock().unwrap();
    assert_eq!(seen[1][1]["content"], "1");
    assert_eq!(seen[1][2]["content"], "2");
    assert_eq!(published.0.lock().unwrap().len(), 1);
    assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
    assert_eq!(sessions.active_count().unwrap(), 0);
    scheduler.close().unwrap();
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
}

#[test]
fn tool_local_stop_aborts_parent_instead_of_empty_success() {
    struct Stop;
    impl Handler for Stop {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            context.runtime.request_cancel("tool_stop").unwrap();
            Ok(json!("late"))
        }
    }
    let provider = rounds(vec![tools("{}")]);
    let calls = Arc::new(AtomicUsize::new(0));
    let (result, _) = run_with_handler(&provider, &Allow, budget(), Arc::new(Stop), calls);
    assert_eq!(result.status, TaskStatus::Aborted);
    assert!(result.result.is_none());
    assert!(result.error.is_none());
    assert_eq!(provider.seen.lock().unwrap().len(), 1);
}

#[test]
fn parent_stop_does_not_hide_required_tool_cleanup_failure() {
    use tofu_runtime::execution_contract::ResourceName;
    struct StopAndFail;
    impl Handler for StopAndFail {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            context
                .runtime
                .bind_resource(
                    ResourceName::ToolEnvironment,
                    0,
                    false,
                    Box::new(|_| Err(tofu_runtime::session::ReleaseError)),
                    Box::new(|| {}),
                )
                .unwrap();
            context
                .runtime
                .request_cancel("stop_with_cleanup_fault")
                .unwrap();
            Ok(json!("late"))
        }
    }
    let provider = rounds(vec![tools("{}")]);
    let calls = Arc::new(AtomicUsize::new(0));
    let (result, _) = run_with_handler(&provider, &Allow, budget(), Arc::new(StopAndFail), calls);
    assert_eq!(result.status, TaskStatus::Error);
    assert_eq!(result.error.as_ref().unwrap()["kind"], "agent_tool_cleanup");
    assert_eq!(provider.seen.lock().unwrap().len(), 1);
}

#[test]
fn explicit_approval_port_approves_denies_and_stops_before_batch_effects() {
    use tofu_runtime::{
        agent::Approvals,
        approval::{Authority, Budget as ReceiptBudget, Store},
    };
    struct Decision(u8);
    impl Approvals for Decision {
        fn decide(
            &self,
            session: &Arc<ExecutionSession>,
            id: &str,
            call: &AdmittedCall,
        ) -> Result<ApprovalDecision, Value> {
            if self.0 == 0 {
                return Ok(ApprovalDecision::Denied(json!({"kind":"approval_denied"})));
            }
            if self.0 == 2 {
                session.request_cancel("approval_stop").unwrap();
                return Ok(ApprovalDecision::Denied(json!({"kind":"approval_denied"})));
            }
            let store = Store::new(
                session,
                7,
                "agent",
                "epoch",
                ReceiptBudget {
                    receipts: 64,
                    identity_bytes: 65536,
                },
                Arc::new(|| Duration::ZERO),
            )
            .unwrap();
            Ok(ApprovalDecision::Approved(
                store.mint(id, call, Authority::Human).unwrap(),
            ))
        }
    }
    for decision in [0, 1, 2] {
        let sessions = Sessions::new();
        let (t, output) = task(&sessions, None);
        let calls = Arc::new(AtomicUsize::new(0));
        let handlers = Registry::new(
            7,
            "epoch",
            vec![("echo".into(), Arc::new(Echo(calls.clone())))],
        )
        .unwrap();
        let catalog=Catalog::compile(7,"epoch",&json!({"echo":{"name":"echo","contractVersion":"tofu.tool-contract/v2","permission":"approval","arguments_schema":{"type":"object"}}})).unwrap();
        let provider = rounds(vec![
            tools("{\"value\":42}"),
            json!({"role":"assistant","content":"done"}),
        ]);
        let decision_port = Decision(decision);
        let agent = Agent {
            provider: &provider,
            catalog: &catalog,
            handlers: &handlers,
            authorization: &Allow,
            feedback: &Text,
            sessions: &sessions,
            epoch: "epoch",
            budget: budget(),
            checkpoints: None,
            progress: None,
            scheduling: None,
            approvals: Some(&decision_port),
            round_admission: None,
        };
        let candidate = agent
            .run(
                &t,
                7,
                "agent",
                vec![json!({"role":"user","content":"hello"})],
            )
            .unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), usize::from(decision == 1));
        assert_eq!(output.0.lock().unwrap().len(), 1);
        assert_eq!(sessions.active_count().unwrap(), 0);
        if decision == 2 {
            assert_eq!(candidate.status, TaskStatus::Aborted);
        } else {
            assert_eq!(candidate.status, TaskStatus::Done);
        }
    }
}

#[test]
fn pending_approval_emits_no_terminal_and_cancel_wakes_before_any_tool() {
    use tofu_runtime::agent::Approvals;
    struct Wait(std::sync::mpsc::Sender<()>);
    impl Approvals for Wait {
        fn decide(
            &self,
            session: &Arc<ExecutionSession>,
            _: &str,
            _: &AdmittedCall,
        ) -> Result<ApprovalDecision, Value> {
            self.0.send(()).unwrap();
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap()
                .block_on(async {
                    tokio::time::timeout(Duration::from_secs(2), session.cancelled())
                        .await
                        .unwrap();
                });
            Ok(ApprovalDecision::NotRequired)
        }
    }
    let sessions = Sessions::new();
    let (t, output) = task(&sessions, None);
    let count = Arc::new(AtomicUsize::new(0));
    let handlers = Registry::new(
        7,
        "epoch",
        vec![("echo".into(), Arc::new(Echo(count.clone())))],
    )
    .unwrap();
    let catalog = catalog();
    let provider = rounds(vec![tools("{}")]);
    let (tx, rx) = std::sync::mpsc::channel();
    let wait = Wait(tx);
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        budget: budget(),
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: Some(&wait),
        round_admission: None,
    };
    std::thread::scope(|scope| {
        let observed_output = &output;
        let observed_count = &count;
        let observed_task = &t;
        let observer = scope.spawn(move || {
            rx.recv_timeout(Duration::from_secs(2)).unwrap();
            assert!(observed_output.0.lock().unwrap().is_empty());
            assert_eq!(observed_count.load(Ordering::SeqCst), 0);
            observed_task.request_abort(7, "user_stop").unwrap();
        });
        assert_eq!(
            agent
                .run(
                    &t,
                    7,
                    "agent",
                    vec![json!({"role":"user","content":"hello"})]
                )
                .unwrap()
                .status,
            TaskStatus::Aborted
        );
        observer.join().unwrap();
    });
    assert_eq!(output.0.lock().unwrap().len(), 1);
    assert_eq!(count.load(Ordering::SeqCst), 0);
}

#[test]
fn explicit_denial_fences_write_permission_and_preserves_ordered_feedback() {
    use tofu_runtime::agent::Approvals;
    struct Select;
    impl Approvals for Select {
        fn decide(
            &self,
            _: &Arc<ExecutionSession>,
            id: &str,
            _: &AdmittedCall,
        ) -> Result<ApprovalDecision, Value> {
            Ok(if id == "deny" {
                ApprovalDecision::Denied(json!({"kind":"approval_denied","message":"user refusal"}))
            } else {
                ApprovalDecision::NotRequired
            })
        }
    }
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, None);
    let count = Arc::new(AtomicUsize::new(0));
    let handlers = Registry::new(
        7,
        "epoch",
        vec![("echo".into(), Arc::new(Echo(count.clone())))],
    )
    .unwrap();
    let catalog=Catalog::compile(7,"epoch",&json!({"echo":{"name":"echo","contractVersion":"tofu.tool-contract/v2","permission":"write","arguments_schema":{"type":"object"}}})).unwrap();
    let provider = rounds(vec![
        json!({"role":"assistant","tool_calls":[
            {"id":"deny","type":"function","function":{"name":"echo","arguments":"{\"value\":1}"}},
            {"id":"run","type":"function","function":{"name":"echo","arguments":"{\"value\":2}"}}
        ]}),
        json!({"role":"assistant","content":"done"}),
    ]);
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        budget: budget(),
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: Some(&Select),
        round_admission: None,
    };
    assert_eq!(
        agent.run(&t, 7, "agent", vec![]).unwrap().status,
        TaskStatus::Done
    );
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert_eq!(published.0.lock().unwrap().len(), 1);
    let seen = provider.seen.lock().unwrap();
    let feedback: Vec<_> = seen[1].iter().filter(|m| m["role"] == "tool").collect();
    assert_eq!(feedback.len(), 2);
    assert_eq!(feedback[0]["tool_call_id"], "deny");
    assert_eq!(
        serde_json::from_str::<Value>(feedback[0]["content"].as_str().unwrap()).unwrap(),
        json!({"kind":"approval_denied","message":"user refusal"})
    );
    assert_eq!(feedback[1]["tool_call_id"], "run");
    assert_eq!(feedback[1]["content"], "2");
}

#[test]
fn not_required_decision_never_bypasses_catalog_receipt_requirement() {
    use tofu_runtime::agent::Approvals;
    struct Skip;
    impl Approvals for Skip {
        fn decide(
            &self,
            _: &Arc<ExecutionSession>,
            _: &str,
            _: &AdmittedCall,
        ) -> Result<ApprovalDecision, Value> {
            Ok(ApprovalDecision::NotRequired)
        }
    }
    let sessions = Sessions::new();
    let (t, _) = task(&sessions, None);
    let count = Arc::new(AtomicUsize::new(0));
    let handlers = Registry::new(
        7,
        "epoch",
        vec![("echo".into(), Arc::new(Echo(count.clone())))],
    )
    .unwrap();
    let catalog=Catalog::compile(7,"epoch",&json!({"echo":{"name":"echo","contractVersion":"tofu.tool-contract/v2","permission":"approval","arguments_schema":{"type":"object"}}})).unwrap();
    let provider = rounds(vec![
        tools("{}"),
        json!({"role":"assistant","content":"done"}),
    ]);
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        budget: budget(),
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: Some(&Skip),
        round_admission: None,
    };
    assert_eq!(
        agent.run(&t, 7, "agent", vec![]).unwrap().status,
        TaskStatus::Done
    );
    assert_eq!(count.load(Ordering::SeqCst), 0);
    assert_eq!(
        provider.seen.lock().unwrap()[1].last().unwrap()["content"],
        "failed"
    );
}

#[test]
fn approval_port_fault_or_oversize_denial_refuses_whole_batch_before_effects() {
    use tofu_runtime::agent::Approvals;
    struct Fault(u8);
    impl Approvals for Fault {
        fn decide(
            &self,
            _: &Arc<ExecutionSession>,
            id: &str,
            _: &AdmittedCall,
        ) -> Result<ApprovalDecision, Value> {
            if id == "first" {
                return Ok(ApprovalDecision::NotRequired);
            }
            match self.0 {
                0 => Err(json!({"kind":"approval_port_unavailable"})),
                1 => Ok(ApprovalDecision::Denied(json!("x".repeat(8192)))),
                _ => panic!("approval adapter panic"),
            }
        }
    }
    for mode in 0..3 {
        let sessions = Sessions::new();
        let (t, published) = task(&sessions, None);
        let count = Arc::new(AtomicUsize::new(0));
        let handlers = Registry::new(
            7,
            "epoch",
            vec![("echo".into(), Arc::new(Echo(count.clone())))],
        )
        .unwrap();
        let catalog = catalog();
        let provider = rounds(vec![json!({"role":"assistant","tool_calls":[
            {"id":"first","type":"function","function":{"name":"echo","arguments":"{}"}},
            {"id":"fault","type":"function","function":{"name":"echo","arguments":"{}"}}
        ]})]);
        let fault = Fault(mode);
        let agent = Agent {
            provider: &provider,
            catalog: &catalog,
            handlers: &handlers,
            authorization: &Allow,
            feedback: &Text,
            sessions: &sessions,
            epoch: "epoch",
            budget: budget(),
            checkpoints: None,
            progress: None,
            scheduling: None,
            approvals: Some(&fault),
            round_admission: None,
        };
        let result = agent.run(&t, 7, "agent", vec![]).unwrap();
        assert_eq!(result.status, TaskStatus::Error);
        assert_eq!(count.load(Ordering::SeqCst), 0);
        assert_eq!(published.0.lock().unwrap().len(), 1);
        assert_eq!(sessions.active_count().unwrap(), 0);
        if mode < 2 {
            assert_eq!(
                result.error.as_ref().unwrap()["kind"],
                if mode == 0 {
                    "approval_port_unavailable"
                } else {
                    "agent_feedback_budget"
                }
            );
        }
    }
}

struct RoundGate {
    allow: usize,
    behavior: &'static str,
    events: Arc<Mutex<Vec<(usize, String)>>>,
}
impl tofu_runtime::round_admission::Admission for RoundGate {
    fn reserve(
        &self,
        context: tofu_runtime::round_admission::Context<'_>,
    ) -> Result<tofu_runtime::round_admission::Permit, Value> {
        assert_eq!(context.owner, 7);
        assert_eq!(context.task_id, "agent");
        assert!(!context.snapshot().unwrap().cancel_requested);
        let round = context.round;
        let messages = context.messages;
        if round >= self.allow {
            return Err(json!({"kind":"round_denied"}));
        }
        assert!(messages.iter().all(Value::is_object));
        self.events.lock().unwrap().push((round, "reserved".into()));
        if self.behavior == "stop" {
            context.request_cancel("round_gate_stop").unwrap();
        }
        let events = self.events.clone();
        let behavior = self.behavior;
        Ok(tofu_runtime::round_admission::Permit::new(Box::new(
            move |evidence| {
                use tofu_runtime::round_admission::Evidence;
                let name = match evidence {
                    Evidence::Returned(result) => {
                        assert_eq!(result.usage, json!({"total_tokens":1}));
                        "returned"
                    }
                    Evidence::Failed(_) => "failed",
                    Evidence::Stopped => "stopped",
                    Evidence::NotDispatched => "not_dispatched",
                    Evidence::Abandoned => "abandoned",
                };
                events.lock().unwrap().push((round, name.into()));
                match behavior {
                    "settle_error" => Err(json!({"kind":"round_accounting_failed"})),
                    "settle_panic" => panic!("round accounting fault"),
                    _ => Ok(()),
                }
            },
        )))
    }
}
fn run_round_gate(provider: &dyn Provider, gate: &dyn tofu_runtime::round_admission::Admission) -> (Arc<TerminalCandidate>, usize) {
    run_round_gate_budget(provider, gate, budget())
}
fn run_round_gate_budget(provider: &dyn Provider, gate: &dyn tofu_runtime::round_admission::Admission, budget: Budget) -> (Arc<TerminalCandidate>, usize) {
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, None);
    let calls = Arc::new(AtomicUsize::new(0));
    let handlers = Registry::new(
        7,
        "epoch",
        vec![("echo".into(), Arc::new(Echo(calls.clone())))],
    )
    .unwrap();
    let catalog = catalog();
    let agent = Agent {
        provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        budget,
        approvals: None,
        checkpoints: None,
        progress: None,
        scheduling: None,
        round_admission: Some(gate),
    };
    let result = agent
        .run(
            &t,
            7,
            "agent",
            vec![json!({"role":"user","content":"hello"})],
        )
        .unwrap();
    assert_eq!(published.0.lock().unwrap().len(), 1);
    assert_eq!(sessions.active_count().unwrap(), 0);
    (result, calls.load(Ordering::SeqCst))
}
#[test]
fn every_physical_round_reserves_and_settles_before_another_dispatch() {
    for allowed in [1, 2] {
        let provider = rounds(vec![
            tools("{}"),
            json!({"role":"assistant","content":"done"}),
        ]);
        let events = Arc::new(Mutex::new(vec![]));
        let gate = RoundGate {
            allow: allowed,
            behavior: "normal",
            events: events.clone(),
        };
        let (result, tools) = run_round_gate(&provider, &gate);
        assert_eq!(tools, 1);
        assert_eq!(provider.seen.lock().unwrap().len(), allowed);
        assert_eq!(
            result.status,
            if allowed == 1 {
                TaskStatus::Error
            } else {
                TaskStatus::Done
            }
        );
        let expected: Vec<_> = (0..allowed)
            .flat_map(|r| [(r, "reserved".into()), (r, "returned".into())])
            .collect();
        assert_eq!(*events.lock().unwrap(), expected);
        assert!(result.receipt.invariants_satisfied);
    }
}
#[test]
fn stop_after_reservation_is_not_physical_dispatch_and_settlement_fault_is_required_failure() {
    for behavior in ["stop", "settle_error", "settle_panic"] {
        let provider = rounds(vec![json!({"role":"assistant","content":"done"})]);
        let events = Arc::new(Mutex::new(vec![]));
        let gate = RoundGate {
            allow: 1,
            behavior,
            events: events.clone(),
        };
        let (result, tools) = run_round_gate(&provider, &gate);
        assert_eq!(tools, 0);
        if behavior == "stop" {
            assert_eq!(result.status, TaskStatus::Aborted);
            assert!(provider.seen.lock().unwrap().is_empty());
            assert_eq!(events.lock().unwrap()[1].1, "not_dispatched");
            assert!(result.receipt.invariants_satisfied);
        } else {
            assert_eq!(result.status, TaskStatus::Error);
            assert_eq!(provider.seen.lock().unwrap().len(), 1);
            assert!(!result.receipt.invariants_satisfied);
        }
    }
}
#[test]
fn provider_panic_abandons_reservation_once_and_provider_error_keeps_failure_evidence() {
    struct Fault(bool);
    impl Provider for Fault {
        fn round(
            &self,
            _: &[Value],
            _: &Arc<ExecutionSession>,
        ) -> Result<StreamResult, ProviderError> {
            if self.0 {
                panic!("provider fixture panic")
            }
            Err(ProviderError::Failed(json!({"kind":"fixture_transport"})))
        }
    }
    for panic in [false, true] {
        let events = Arc::new(Mutex::new(vec![]));
        let gate = RoundGate {
            allow: 1,
            behavior: "normal",
            events: events.clone(),
        };
        let (result, tools) = run_round_gate(&Fault(panic), &gate);
        assert_eq!(result.status, TaskStatus::Error);
        assert_eq!(tools, 0);
        assert_eq!(events.lock().unwrap().len(), 2);
        assert_eq!(
            events.lock().unwrap()[1].1,
            if panic { "abandoned" } else { "failed" }
        );
        assert!(result.receipt.invariants_satisfied);
    }
}
#[test]
fn returned_usage_is_settled_even_when_parent_was_cancelled_during_the_round() {
    struct Late;
    impl Provider for Late {
        fn round(
            &self,
            _: &[Value],
            session: &Arc<ExecutionSession>,
        ) -> Result<StreamResult, ProviderError> {
            session.request_cancel("late_cancel").unwrap();
            Ok(response(tools("{}")))
        }
    }
    let events = Arc::new(Mutex::new(vec![]));
    let gate = RoundGate {
        allow: 1,
        behavior: "normal",
        events: events.clone(),
    };
    let (result, tools) = run_round_gate(&Late, &gate);
    assert_eq!(result.status, TaskStatus::Aborted);
    assert_eq!(tools, 0);
    assert_eq!(events.lock().unwrap()[1].1, "returned");
    assert!(result.receipt.invariants_satisfied);
}

#[test]
fn denied_native_round_never_opens_http_and_stopped_provider_settles_once() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let transport = {
        let _enter = runtime.enter();
        tofu_runtime::http::Transport::new(
            &format!("http://{}/chat", listener.local_addr().unwrap()),
            None,
            policy(),
        )
        .unwrap()
    };
    let cancel = tofu_runtime::http::Cancellation::new();
    let provider = tofu_runtime::agent_http::HttpProvider::new(
        &runtime,
        &transport,
        provider_config(),
        &cancel,
    )
    .unwrap();
    let gate = RoundGate {
        allow: 0,
        behavior: "normal",
        events: Arc::new(Mutex::new(vec![])),
    };
    let (result, tools) = run_round_gate(&provider, &gate);
    assert_eq!(result.error.as_ref().unwrap()["kind"], "round_denied");
    assert_eq!(tools, 0);
    assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
    assert_eq!(transport.available_slots(), 1);
    struct Stopped;
    impl Provider for Stopped {
        fn round(
            &self,
            _: &[Value],
            _: &Arc<ExecutionSession>,
        ) -> Result<StreamResult, ProviderError> {
            Err(ProviderError::Stopped)
        }
    }
    let events = Arc::new(Mutex::new(vec![]));
    let gate = RoundGate {
        allow: 1,
        behavior: "normal",
        events: events.clone(),
    };
    let (result, _) = run_round_gate(&Stopped, &gate);
    assert_eq!(result.status, TaskStatus::Aborted);
    assert_eq!(events.lock().unwrap()[1].1, "stopped");
    assert!(result.receipt.invariants_satisfied);
}

#[test]
fn admission_wait_observes_parent_deadline_without_starting_provider() {
    use tofu_runtime::round_admission::{Admission, Context, Evidence, Permit};
    struct Wait {
        runtime: tokio::runtime::Runtime,
        settled: Arc<AtomicUsize>,
    }
    impl Admission for Wait {
        fn reserve(&self, context: Context<'_>) -> Result<Permit, Value> {
            self.runtime.block_on(async {
                tokio::time::timeout(Duration::from_secs(1), context.cancelled())
                    .await
                    .unwrap();
            });
            assert!(
                context
                    .snapshot()
                    .unwrap()
                    .deadline_remaining_nanos
                    .unwrap()
                    <= 0
            );
            let settled = self.settled.clone();
            Ok(Permit::new(Box::new(move |outcome| {
                assert!(matches!(outcome, Evidence::NotDispatched));
                settled.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })))
        }
    }
    let sessions = Sessions::new();
    let (t, published) = task(&sessions, Some(Duration::from_millis(40)));
    let catalog = catalog();
    let handlers = Registry::new(7, "epoch", vec![]).unwrap();
    let provider = rounds(vec![]);
    let settled = Arc::new(AtomicUsize::new(0));
    let wait = Wait {
        runtime: tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap(),
        settled: settled.clone(),
    };
    let agent = Agent {
        provider: &provider,
        catalog: &catalog,
        handlers: &handlers,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        budget: budget(),
        round_admission: Some(&wait),
        approvals: None,
        checkpoints: None,
        progress: None,
        scheduling: None,
    };
    let result = agent.run(&t, 7, "agent", vec![]).unwrap();
    assert_eq!(result.status, TaskStatus::Aborted);
    assert!(provider.seen.lock().unwrap().is_empty());
    assert_eq!(settled.load(Ordering::SeqCst), 1);
    assert_eq!(published.0.lock().unwrap().len(), 1);
    assert!(result.receipt.invariants_satisfied);
}

#[test]
fn native_anthropic_model_tool_model_replays_signed_and_hosted_state() {
    use std::{io::Write,net::TcpListener};
    use tofu_runtime::{agent_http::HttpProvider,http::{Transport,Cancellation},anthropic};
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move || {
        let (mut stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut bodies=vec![];
        for round in 0..2 {
            let body=read_request(&mut stream);assert!(body.get("system").is_none());assert_eq!(body["tools"][0]["input_schema"],json!({"type":"object"}));
            assert_eq!(body["messages"][0]["content"][0]["text"],"hello");
            if round==1 {
                let replay=&body["messages"][1]["content"];
                assert_eq!(replay[0],json!({"type":"thinking","thinking":"plan","signature":"opaque"}));
                assert_eq!(replay[1]["type"],"server_tool_use");assert_eq!(replay[1]["input"],json!({"q":"source"}));
                assert_eq!(replay[2]["type"],"tool_use");assert_eq!(replay[2]["input"],json!({"value":42}));
                assert_eq!(body["messages"][2]["content"][0],json!({"type":"tool_result","tool_use_id":"call","content":"42"}));
            }
            bodies.push(body);
            let events=if round==0 {vec![
                json!({"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"plan","signature":"opaque"}}),
                json!({"type":"content_block_start","index":1,"content_block":{"type":"server_tool_use","id":"host","name":"search","input":{"q":"source"}}}),
                json!({"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"call","name":"echo","input":{"value":42}}}),
                json!({"type":"message_delta","delta":{"stop_reason":"tool_use"}}),json!({"type":"message_stop"}),
            ]}else{vec![json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"native Anthropic answer"}}),json!({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),json!({"type":"message_stop"})]};
            let wire=events.iter().map(|v|format!("data: {v}\n\n")).collect::<String>();
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{wire}",wire.len()).unwrap();stream.flush().unwrap();
        }
        bodies
    });
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let transport={let _entered=runtime.enter();Transport::new(&format!("http://{address}/v1/messages"),None,policy()).unwrap()};let cancel=Cancellation::new();
    let mut configuration=provider_config();configuration.headers.insert("content-type",reqwest::header::HeaderValue::from_static("application/json"));
    let provider=HttpProvider::new(&runtime,&transport,configuration,&cancel).unwrap().with_anthropic(anthropic::Budget{blocks:32,retained_bytes:65536}).unwrap();
    let (result,calls)=run(&provider,&Allow,budget());assert_eq!(result.status,TaskStatus::Done);assert_eq!(calls,1);assert_eq!(transport.available_slots(),1);
    let bodies=server.join().unwrap();assert_eq!(bodies.len(),2);
}

#[test]
fn unsupported_anthropic_request_refuses_before_any_tcp_effect() {
    use std::net::TcpListener;
    use tofu_runtime::{agent_http::HttpProvider,http::{Transport,Cancellation},anthropic};
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
    let transport={let _enter=runtime.enter();Transport::new(&format!("http://{}/v1/messages",listener.local_addr().unwrap()),None,policy()).unwrap()};
    let cancel=Cancellation::new();let sessions=Sessions::new();
    let cases=[
        json!({"response_format":{"type":"json_object"}}),
        json!({"response_format":{"type":"json_schema","json_schema":{"schema":{},"strict":false}}}),
        json!({"response_format":{"type":"json_schema","json_schema":{"schema":{}}},"output_config":{"format":{"type":"json_schema","schema":{"type":"string"}}}}),
    ];
    for (ordinal,fields) in cases.iter().enumerate(){
        let mut config=provider_config();config.template.extend(fields.as_object().unwrap().clone());
        let provider=HttpProvider::new(&runtime,&transport,config,&cancel).unwrap().with_anthropic(anthropic::Budget{blocks:32,retained_bytes:65536}).unwrap();
        let session=sessions.start(&format!("unsupported-{ordinal}"),"task",7,"",None).unwrap();
        assert!(matches!(provider.round(&[json!({"role":"user","content":"question"})],&session),Err(ProviderError::Failed(value)) if value["kind"]=="agent_anthropic_request_refused"));
        assert!(matches!(listener.accept(),Err(error) if error.kind()==std::io::ErrorKind::WouldBlock));
        assert_eq!(transport.available_slots(),1);
        session.settle(tofu_runtime::execution_contract::ExecutionPhase::Failed,"").unwrap();
    }
}

#[test]
fn native_responses_model_tool_model_replays_encrypted_state() {
    for profile in [tofu_runtime::responses_request::Profile::Compatible, tofu_runtime::responses_request::Profile::Codex] {
        responses_model_tool_model_with_profile(profile);
    }
}
fn responses_model_tool_model_with_profile(profile: tofu_runtime::responses_request::Profile) {
    use std::{io::Write,net::TcpListener};
    use tofu_runtime::{agent_http::HttpProvider,http::{Transport,Cancellation},responses};
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move || {
        let (mut stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut bodies=vec![];
        for round in 0..2 {
            let body=read_request(&mut stream);assert_eq!(body["store"],false);assert_eq!(body["tools"][0]["type"],"function");
            if profile == tofu_runtime::responses_request::Profile::Codex {
                assert_eq!(body["instructions"], "");
                assert_eq!(body["reasoning"]["summary"], "auto");
                assert!(body.get("max_output_tokens").is_none());
                assert!(body.get("temperature").is_none());
            } else { assert!(body.get("instructions").is_none()); }

            assert_eq!(body["input"][0]["content"][0]["text"],"hello");
            if round==1 {
                assert_eq!(body["input"][1],json!({"type":"reasoning","id":"reason","encrypted_content":"opaque"}));
                assert_eq!(body["input"][2]["type"],"function_call");
                assert_eq!(body["input"][3],json!({"type":"function_call_output","call_id":"call","output":"42"}));
            }
            bodies.push(body);
            let events=if round==0 {vec![
                json!({"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item","call_id":"call","name":"echo","arguments":"{\"value\":42}"}}),
                json!({"type":"response.completed","response":{"output":[{"type":"reasoning","id":"reason","encrypted_content":"opaque"},{"type":"function_call","id":"item","call_id":"call","name":"echo","arguments":"{\"value\":42}"}]}}),
            ]}else{vec![json!({"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"native Responses answer"}]}]}})]};
            let wire=events.iter().map(|v|format!("data: {v}\n\n")).collect::<String>();
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{wire}",wire.len()).unwrap();stream.flush().unwrap();
        }
        bodies
    });
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let transport={let _entered=runtime.enter();Transport::new(&format!("http://{address}/v1/responses"),None,policy()).unwrap()};let cancel=Cancellation::new();
    let mut configuration=provider_config();configuration.headers.insert("content-type",reqwest::header::HeaderValue::from_static("application/json"));
    let provider=HttpProvider::new(&runtime,&transport,configuration,&cancel).unwrap().with_responses_profile(responses::Budget{items:32,retained_bytes:65536},true,profile).unwrap();
    let (result,calls)=run(&provider,&Allow,budget());assert_eq!(result.status,TaskStatus::Done);assert_eq!(calls,1);assert_eq!(transport.available_slots(),1);
    let bodies=server.join().unwrap();assert_eq!(bodies.len(),2);
}


#[test]
fn unsupported_responses_profile_refuses_before_tool_effects() {
    use std::{io::Write,net::TcpListener};
    use tofu_runtime::{agent_http::HttpProvider,http::{Transport,Cancellation},responses};
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move || {
        let (mut stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut bodies=vec![];
        for round in 0..1 {
            let body=read_request(&mut stream);assert_eq!(body["store"],false);assert_eq!(body["tools"][0]["type"],"function");
            assert_eq!(body["input"][0]["content"][0]["text"],"hello");
            if round==1 {
                assert_eq!(body["input"][1],json!({"type":"reasoning","id":"reason","encrypted_content":"opaque"}));
                assert_eq!(body["input"][2]["type"],"function_call");
                assert_eq!(body["input"][3],json!({"type":"function_call_output","call_id":"call","output":"42"}));
            }
            bodies.push(body);
            let events=if round==0 {vec![
                json!({"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item","call_id":"call","name":"echo","arguments":"{\"value\":42}"}}),
                json!({"type":"response.completed","response":{"output":[{"type":"program_output","id":"program","output":"unsupported"},{"type":"function_call","id":"item","call_id":"call","name":"echo","arguments":"{\"value\":42}"}]}}),
            ]}else{vec![json!({"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"native Responses answer"}]}]}})]};
            let wire=events.iter().map(|v|format!("data: {v}\n\n")).collect::<String>();
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{wire}",wire.len()).unwrap();stream.flush().unwrap();
        }
        bodies
    });
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let transport={let _entered=runtime.enter();Transport::new(&format!("http://{address}/v1/responses"),None,policy()).unwrap()};let cancel=Cancellation::new();
    let mut configuration=provider_config();configuration.headers.insert("content-type",reqwest::header::HeaderValue::from_static("application/json"));
    let provider=HttpProvider::new(&runtime,&transport,configuration,&cancel).unwrap().with_responses(responses::Budget{items:32,retained_bytes:65536},true).unwrap();
    let (result,calls)=run(&provider,&Allow,budget());assert_eq!(result.status,TaskStatus::Error);assert_eq!(calls,0);assert_eq!(transport.available_slots(),1);
    let bodies=server.join().unwrap();assert_eq!(bodies.len(),1);
}

#[test]
fn prepared_history_is_validated_and_invalid_permits_settle_before_dispatch() {
    use tofu_runtime::round_admission::{Admission,Context,Permit,Evidence};
    struct Prepare { messages: Vec<Value>, settled: Arc<Mutex<Vec<String>>> }
    impl Admission for Prepare {
        fn reserve(&self, _:Context<'_>)->Result<Permit,Value>{
            let settled=self.settled.clone();
            Ok(Permit::new(Box::new(move |evidence|{
                settled.lock().unwrap().push(match evidence {Evidence::NotDispatched=>"not_dispatched",Evidence::Returned(_)=>"returned",_=>"other"}.into());Ok(())
            })).with_messages(self.messages.clone()))
        }
    }
    for messages in [vec![json!({"role":"user","content":"prepared"})],vec![json!(7)],vec![json!({"role":"user","content":"x".repeat(65536)})]] {
        let provider=rounds(vec![json!({"role":"assistant","content":"done"})]);
        let settled=Arc::new(Mutex::new(vec![]));let gate=Prepare{messages:messages.clone(),settled:settled.clone()};
        let (result,calls)=run_round_gate(&provider,&gate);
        assert_eq!(calls,0);
        let valid=messages[0]["content"]=="prepared";
        assert_eq!(result.status,if valid {TaskStatus::Done}else{TaskStatus::Error});
        assert_eq!(settled.lock().unwrap().as_slice(),&[if valid {"returned"}else{"not_dispatched"}]);
        if valid {assert_eq!(provider.seen.lock().unwrap()[0],messages);}else{assert!(provider.seen.lock().unwrap().is_empty());}
    }
}


#[test]
fn prepared_history_cannot_erase_retained_usage_charge() {
    use tofu_runtime::round_admission::{Admission, Context, Evidence, Permit};
    struct Prepare { second: Vec<Value>, settled: Arc<Mutex<Vec<&'static str>>> }
    impl Admission for Prepare {
        fn reserve(&self, context: Context<'_>) -> Result<Permit, Value> {
            let settled = self.settled.clone();
            let messages = if context.round == 0 { vec![] } else { self.second.clone() };
            Ok(Permit::new(Box::new(move |evidence| {
                settled.lock().unwrap().push(match evidence {
                    Evidence::Returned(_) => "returned",
                    Evidence::NotDispatched => "not_dispatched",
                    _ => "other",
                });
                Ok(())
            })).with_messages(messages))
        }
    }
    let first = tools("{}");
    let last = json!({"role":"assistant","content":"done"});
    let huge_usage = json!({"provider_extension":"u".repeat(2048)});
    struct Measured { count: AtomicUsize, first: Value, last: Value, usage: Value, large_final_usage: bool }
    impl Provider for Measured {
        fn round(&self, _: &[Value], _: &Arc<ExecutionSession>) -> Result<StreamResult, ProviderError> {
            let first = self.count.fetch_add(1, Ordering::SeqCst) == 0;
            let mut result = response(if first {self.first.clone()} else {self.last.clone()});
            if first || self.large_final_usage {result.usage = self.usage.clone();}
            Ok(result)
        }
    }
    // Each component fits independently; together they exceed the run budget.
    for (second, large_final_usage) in [(vec![json!({"role":"user","content":"h".repeat(2048)})], false), (vec![], false), (vec![], true)] {
        let provider = Measured {count: AtomicUsize::new(0), first: first.clone(), last: last.clone(), usage: huge_usage.clone(), large_final_usage};
        let settled = Arc::new(Mutex::new(vec![]));
        let gate = Prepare {second: second.clone(), settled: settled.clone()};
        let mut b = budget(); b.retained_bytes = 3000;
        let (result, calls) = run_round_gate_budget(&provider, &gate, b);
        assert_eq!(calls, 1);
        if second.is_empty() && !large_final_usage {
            assert_eq!(result.status, TaskStatus::Done);
            assert_eq!(provider.count.load(Ordering::SeqCst), 2);
            assert_eq!(settled.lock().unwrap().as_slice(), &["returned", "returned"]);
        } else {
            assert_eq!(result.status, TaskStatus::Error);
            assert_eq!(provider.count.load(Ordering::SeqCst), if large_final_usage {2} else {1});
            assert_eq!(settled.lock().unwrap().as_slice(), if large_final_usage {&["returned", "returned"]} else {&["returned", "not_dispatched"]});
            assert_eq!(result.error.as_ref().unwrap()["kind"], "agent_context_budget");
        }
    }
}


#[test]
fn trusted_progress_halts_only_after_joined_tools() {
    use tofu_runtime::progress::{Context, Limits, Observation, Policy};
    struct Probe { changed: bool, complete: bool, verified: bool, fail: bool, stop: bool }
    impl Policy for Probe {
        fn limits(&self) -> Limits { Limits {no_progress_rounds: 2, nonretryable_failure_rounds: 2} }
        fn observe(&self, context: Context<'_>) -> Result<Observation, Value> {
            assert_eq!((context.owner, context.task_id), (7, "agent"));
            assert_eq!(context.messages.last().unwrap()["role"], "tool");
            if self.fail { return Err(json!({"kind":"owned_progress_fault"})); }
            if self.stop {context.session.request_cancel("owned_progress_stop").unwrap();}
            Ok(Observation {
                evidence_ids: vec![if self.changed {context.round.to_string()} else {"same-result".into()}],
                evidence_complete: self.complete,
                verification: if self.verified {"passed".into()} else {String::new()},
                ..Observation::default()
            })
        }
    }
    for (probe, expected_rounds, expected_status) in [
        (Probe{changed:false,complete:true,verified:false,fail:false,stop:false},3,TaskStatus::Error),
        (Probe{changed:true,complete:true,verified:false,fail:false,stop:false},5,TaskStatus::Done),
        (Probe{changed:false,complete:false,verified:false,fail:false,stop:false},5,TaskStatus::Done),
        (Probe{changed:false,complete:true,verified:true,fail:false,stop:false},5,TaskStatus::Done),
        (Probe{changed:false,complete:true,verified:false,fail:true,stop:false},1,TaskStatus::Error),
        (Probe{changed:false,complete:true,verified:false,fail:false,stop:true},1,TaskStatus::Aborted),
    ] {
        let provider = rounds(vec![tools("{}"),tools("{}"),tools("{}"),tools("{}"),json!({"role":"assistant","content":"done"})]);
        let calls = Arc::new(AtomicUsize::new(0));
        let mut b=budget(); b.rounds=6;
        let (result, count)=run_with_handler_progress(&provider,&Allow,b,Arc::new(Echo(calls.clone())),calls,Some(&probe));
        assert_eq!(result.status, expected_status);
        assert_eq!(provider.seen.lock().unwrap().len(), expected_rounds);
        assert_eq!(count, expected_rounds.min(4));
        if expected_rounds==3 {assert_eq!(result.error.as_ref().unwrap()["kind"],"agent_no_progress");}
        if probe.fail {assert_eq!(result.error.as_ref().unwrap()["kind"],"owned_progress_fault");}
    }
}

#[test]
fn native_progress_failure_policy_and_disabled_or_oversized_probes() {
    use tofu_runtime::progress::{Context, Limits, Observation, Policy};
    struct Probe {disabled: bool, oversized: bool}
    impl Policy for Probe {
        fn limits(&self)->Limits {if self.disabled {Limits::default()} else {Limits{no_progress_rounds:0,nonretryable_failure_rounds:2}}}
        fn observe(&self, context:Context<'_>)->Result<Observation,Value> {
            assert!(!self.disabled,"disabled policy must not be invoked");
            assert_eq!(context.messages.last().unwrap()["content"],"failed");
            Ok(Observation{nonretryable_failure_signatures:vec!["permission".into()],world_version:if self.oversized {"w".repeat(4097)} else {String::new()},..Default::default()})
        }
    }
    struct Fail;
    impl Handler for Fail {
        fn invoke(&self,_:InvocationContext<'_>,_:&Value)->Result<Value,Value>{Err(json!({"kind":"permission","retryable":false}))}
    }
    for (disabled,oversized,expected_rounds,expected_kind) in [(false,false,2,Some("agent_nonretryable_tool_failure")),(true,false,4,None),(false,true,1,Some("agent_progress_budget"))] {
        let provider=rounds(vec![tools("{\"value\":1}"),tools("{\"value\":2}"),tools("{\"value\":3}"),json!({"role":"assistant","content":"done"})]);
        let mut b=budget();b.rounds=5;
        let probe=Probe{disabled,oversized};
        let (result,_)=run_with_handler_progress(&provider,&Allow,b,Arc::new(Fail),Arc::new(AtomicUsize::new(0)),Some(&probe));
        assert_eq!(provider.seen.lock().unwrap().len(),expected_rounds);
        match expected_kind {
            Some(kind)=>{assert_eq!(result.status,TaskStatus::Error);assert_eq!(result.error.as_ref().unwrap()["kind"],kind);},
            None=>assert_eq!(result.status,TaskStatus::Done),
        }
    }
}

#[derive(Default)]
struct RecoveryState {
    saved: Option<Value>,
    inflight: bool,
    starts: Vec<usize>,
    commits: Vec<usize>,
    suspend: bool,
    reject_begin: bool,
    reject_commit: bool,
}
#[derive(Default)]
struct Recovery(Mutex<RecoveryState>);
impl tofu_runtime::agent_checkpoint::Port for Recovery {
    fn binding(&self)->&str {"fixture-model-and-tools"}
    fn load(&self,identity:tofu_runtime::agent_checkpoint::Identity<'_>)->Result<Option<Value>,Value>{
        assert_eq!((identity.owner,identity.task_id),(7,"agent"));
        let state=self.0.lock().unwrap();
        if state.inflight {return Err(json!({"kind":"recovery_inflight"}));}
        Ok(state.saved.clone())
    }
    fn begin(&self,_:tofu_runtime::agent_checkpoint::Identity<'_>,round:usize)->Result<(),Value>{
        let mut state=self.0.lock().unwrap();
        if state.reject_begin {return Err(json!({"kind":"recovery_begin"}));}
        assert!(!state.inflight);state.inflight=true;state.starts.push(round);Ok(())
    }
    fn complete(&self,view:tofu_runtime::agent_checkpoint::View<'_>)->Result<tofu_runtime::agent_checkpoint::Disposition,Value>{
        let mut state=self.0.lock().unwrap();assert!(state.inflight);
        assert_eq!(state.starts.last().copied(),Some(view.next_round-1));
        if state.reject_commit {return Err(json!({"kind":"recovery_commit"}));}
        state.saved=Some(view.capture());state.inflight=false;state.commits.push(view.next_round);
        Ok(if state.suspend {tofu_runtime::agent_checkpoint::Disposition::Suspend}else{tofu_runtime::agent_checkpoint::Disposition::Continue})
    }
}
fn recover(provider:&dyn Provider,b:Budget,port:&dyn tofu_runtime::agent_checkpoint::Port,progress:Option<&dyn tofu_runtime::progress::Policy>)->(Arc<TerminalCandidate>,usize) {
    let calls=Arc::new(AtomicUsize::new(0));
    run_with_handler_recovery(provider,&Allow,b,Arc::new(Echo(calls.clone())),calls,progress,Some(port))
}
#[test]
fn completed_frontier_resumes_without_replaying_provider_or_tools() {
    let port=Recovery::default();port.0.lock().unwrap().suspend=true;
    let first=rounds(vec![tools("{}")]);let (paused,calls)=recover(&first,budget(),&port,None);
    assert_eq!(paused.status,TaskStatus::Aborted);assert_eq!(calls,1);
    assert_eq!(first.seen.lock().unwrap().len(),1);
    let saved=port.0.lock().unwrap().saved.clone().unwrap();
    assert_eq!(saved["next_round"],1);assert_eq!(saved["usage"].as_array().unwrap().len(),1);
    port.0.lock().unwrap().suspend=false;
    let second=rounds(vec![json!({"role":"assistant","content":"done"})]);
    let (done,calls)=recover(&second,budget(),&port,None);
    assert_eq!(done.status,TaskStatus::Done);assert_eq!(calls,0);
    assert_eq!(second.seen.lock().unwrap().len(),1);
    assert_eq!(second.seen.lock().unwrap()[0],saved["messages"].as_array().unwrap().clone());
    assert_eq!(done.result.as_ref().unwrap()["llm_rounds"],2);
    assert_eq!(done.result.as_ref().unwrap()["tool_calls"],1);
    assert_eq!(done.result.as_ref().unwrap()["usage"].as_array().unwrap().len(),2);
    assert_eq!(port.0.lock().unwrap().starts,vec![0,1]);
}
#[test]
fn invalid_frontiers_and_failed_fences_refuse_before_more_effects() {
    let source=Recovery::default();source.0.lock().unwrap().suspend=true;
    recover(&rounds(vec![tools("{}")]),budget(),&source,None);
    let saved=source.0.lock().unwrap().saved.clone().unwrap();
    for field in ["owner_user_id","task_id","epoch","binding","next_round","tool_calls","round_limit","tool_limit","progress","messages","usage","extra"] {
        let mut invalid=saved.clone();
        invalid[field]=match field {"next_round"|"tool_calls"=>json!(0),"owner_user_id"=>json!(8),"round_limit"|"tool_limit"=>json!(999),"messages"|"usage"=>json!([]),"progress"=>json!({}),_=>json!("invalid")};
        let port=Recovery::default();port.0.lock().unwrap().saved=Some(invalid);
        let provider=rounds(vec![]);let (result,calls)=recover(&provider,budget(),&port,None);
        assert_eq!(result.status,TaskStatus::Error,"{field}");assert_eq!(calls,0);
        assert!(provider.seen.lock().unwrap().is_empty());assert!(port.0.lock().unwrap().starts.is_empty());
    }
    for mode in ["inflight","begin","commit"] {
        let port=Recovery::default();{
            let mut state=port.0.lock().unwrap();state.inflight=mode=="inflight";state.reject_begin=mode=="begin";state.reject_commit=mode=="commit";
        }
        let provider=rounds(if mode=="commit" {vec![tools("{}")]} else {vec![]});
        let (result,calls)=recover(&provider,budget(),&port,None);
        assert_eq!(result.status,TaskStatus::Error);assert_eq!(calls,usize::from(mode=="commit"));
        assert_eq!(provider.seen.lock().unwrap().len(),usize::from(mode=="commit"));
        if mode=="commit" {
            assert!(port.0.lock().unwrap().inflight);
            let (result,calls)=recover(&rounds(vec![]),budget(),&port,None);
            assert_eq!(result.error.as_ref().unwrap()["kind"],"recovery_inflight");assert_eq!(calls,0);
        }
    }
}

#[test]
fn recovery_preserves_progress_streaks_and_total_tool_budget() {
    use tofu_runtime::progress::{Context,Limits,Observation,Policy};
    struct Same;
    impl Policy for Same {
        fn limits(&self)->Limits {Limits{no_progress_rounds:2,nonretryable_failure_rounds:0}}
        fn observe(&self,_:Context<'_>)->Result<Observation,Value>{Ok(Observation{evidence_ids:vec!["visible".into()],evidence_complete:true,..Default::default()})}
    }
    let port=Recovery::default();port.0.lock().unwrap().suspend=true;
    let mut b=budget();b.rounds=5;
    let (paused,calls)=recover(&rounds(vec![tools("{}")]),b,&port,Some(&Same));
    assert_eq!(paused.status,TaskStatus::Aborted);assert_eq!(calls,1);
    let saved=port.0.lock().unwrap().saved.clone().unwrap();
    for field in ["last_calls","last_evidence","no_progress_streak","codec","extra"] {
        let mut invalid=saved.clone();invalid["progress"][field]=match field {"no_progress_streak"=>json!(2),"last_calls"|"last_evidence"=>json!("A".repeat(64)),_=>json!("invalid")};
        let bad=Recovery::default();bad.0.lock().unwrap().saved=Some(invalid);
        let (result,count)=recover(&rounds(vec![]),b,&bad,Some(&Same));
        assert_eq!(result.status,TaskStatus::Error);assert_eq!(count,0);assert!(bad.0.lock().unwrap().starts.is_empty());
    }
    port.0.lock().unwrap().suspend=false;
    let provider=rounds(vec![tools("{}"),tools("{}")]);
    let (halted,calls)=recover(&provider,b,&port,Some(&Same));
    assert_eq!(calls,2);assert_eq!(halted.error.as_ref().unwrap()["kind"],"agent_no_progress");
    assert_eq!(halted.error.as_ref().unwrap()["noProgressStreak"],2);
    assert_eq!(port.0.lock().unwrap().starts,vec![0,1,2]);
    let limited=Recovery::default();limited.0.lock().unwrap().suspend=true;
    let mut b=budget();b.tool_calls=1;
    recover(&rounds(vec![tools("{}")]),b,&limited,None);
    limited.0.lock().unwrap().suspend=false;
    let (halted,calls)=recover(&rounds(vec![tools("{}")]),b,&limited,None);
    assert_eq!(calls,0);assert_eq!(halted.error.as_ref().unwrap()["kind"],"agent_tool_budget");
}

#[test]
fn recovery_load_cancel_dominates_loader_error_before_provider() {
    use tofu_runtime::agent_checkpoint::{Disposition,Identity,Port,View};
    struct Stop;
    impl Port for Stop {
        fn binding(&self)->&str {"bound"}
        fn load(&self,identity:Identity<'_>)->Result<Option<Value>,Value>{identity.session.request_cancel("recovery_stop").unwrap();Err(json!({"kind":"loader_failed"}))}
        fn begin(&self,_:Identity<'_>,_:usize)->Result<(),Value>{panic!("cancelled recovery began a round")}
        fn complete(&self,_:View<'_>)->Result<Disposition,Value>{panic!("cancelled recovery committed")}
    }
    let calls=Arc::new(AtomicUsize::new(0));let provider=rounds(vec![]);
    let (result,calls)=run_with_handler_recovery(&provider,&Allow,budget(),Arc::new(Echo(calls.clone())),calls,None,Some(&Stop));
    assert_eq!(result.status,TaskStatus::Aborted);assert_eq!(calls,0);assert!(provider.seen.lock().unwrap().is_empty());
}


#[path = "support/checkpoint_store.rs"]
mod checkpoint_store;
#[test]
fn journal_port_resumes_agent_once_and_refuses_failed_begin_before_provider() {
    use tofu_runtime::checkpoint_journal::{Journal,Policy,Scope,Recovery};
    struct Restore(AtomicUsize);
    impl Recovery for Restore {
        fn restore(&self,identity:tofu_runtime::agent_checkpoint::Identity<'_>,state:&Value)->Result<(),Value>{
            assert_eq!(identity.owner,7);assert_eq!(state["next_round"],1);
            self.0.fetch_add(1,Ordering::SeqCst);Ok(())
        }
    }
    let store=checkpoint_store::MemoryStore::default();let restoration=Restore(AtomicUsize::new(0));
    let journal=Journal::new(Scope{owner:7,task_id:"agent".into(),epoch:"epoch".into(),
        binding:"fixture-model-and-tools".into()},&store,1048576,1024).unwrap();
    let first=Policy::new(journal.acquire("first").unwrap(),&restoration,true);
    let provider=rounds(vec![tools("{}")]);let (paused,calls)=recover(&provider,budget(),&first,None);
    assert_eq!(paused.status,TaskStatus::Aborted);assert_eq!(calls,1);first.lease.release().unwrap();
    let second=Policy::new(journal.acquire("second").unwrap(),&restoration,false);
    let provider=rounds(vec![json!({"role":"assistant","content":"done"})]);
    let (done,calls)=recover(&provider,budget(),&second,None);
    assert_eq!(done.status,TaskStatus::Done);assert_eq!(calls,0);assert_eq!(restoration.0.load(Ordering::SeqCst),1);
    second.lease.seal().unwrap();assert!(journal.acquire("third").is_err());

    let store=checkpoint_store::MemoryStore::default();let journal=Journal::new(Scope{owner:7,
        task_id:"agent".into(),epoch:"epoch".into(),binding:"fixture-model-and-tools".into()},&store,1048576,1024).unwrap();
    let policy=Policy::new(journal.acquire("failed").unwrap(),&restoration,false);
    store.fail_before.store(true,Ordering::Release);
    let provider=rounds(vec![tools("{}")]);let (error,calls)=recover(&provider,budget(),&policy,None);
    assert_eq!(error.status,TaskStatus::Error);assert_eq!(calls,0);assert!(provider.seen.lock().unwrap().is_empty());
    assert!(journal.acquire("another").is_err());
}


#[test]
fn agent_terminal_persistence_automatically_seals_completion_and_preserves_suspend() {
    use tofu_runtime::checkpoint_journal::{Journal,OwnedLease,OwnedPolicy,Scope,Recovery};
    use tofu_runtime::task::{TerminalPersistence,PortError};
    struct Restore;
    impl Recovery for Restore {
        fn restore(&self,_:tofu_runtime::agent_checkpoint::Identity<'_>,_:&Value)->Result<(),Value>{Ok(())}
    }
    struct Persist(Arc<checkpoint_store::MemoryStore>,bool);
    impl TerminalPersistence for Persist {
        fn persist_once(&self,_:&TerminalCandidate)->Result<(),PortError>{
            assert_eq!(self.0.record.lock().unwrap().as_ref().unwrap().1["phase"],if self.1 {"completed"}else{"terminal"});Ok(())
        }
    }
    for suspend in [false,true] {
        let store=Arc::new(checkpoint_store::MemoryStore::default());let journal=Journal::new(Scope{owner:7,task_id:"agent".into(),epoch:"epoch".into(),binding:"fixture-model-and-tools".into()},store.as_ref(),65536,1024).unwrap();
        let lease=OwnedLease::acquire(Scope{owner:7,task_id:"agent".into(),epoch:"epoch".into(),binding:"fixture-model-and-tools".into()},store.clone(),65536,1024,"run").unwrap();
        let policy=OwnedPolicy::new(lease,Arc::new(Restore),suspend);
        let persistence=policy.terminal_persistence(Arc::new(Persist(store.clone(),suspend))).unwrap();
        let sessions=Sessions::new();let (task,output)=task_with_persistence(&sessions,None,Some(persistence));
        let calls=Arc::new(AtomicUsize::new(0));let handlers=Registry::new(7,"epoch",vec![("echo".into(),Arc::new(Echo(calls.clone())) as Arc<dyn Handler>)]).unwrap();let catalog=catalog();
        let provider=rounds(if suspend {vec![tools("{}")]}else{vec![tools("{}"),json!({"role":"assistant","content":"done"})]});
        let agent=Agent{provider:&provider,catalog:&catalog,handlers:&handlers,authorization:&Allow,feedback:&Text,sessions:&sessions,epoch:"epoch",budget:budget(),checkpoints:Some(&policy),progress:None,scheduling:None,approvals:None,round_admission:None};
        let candidate=agent.run(&task,7,"agent",vec![json!({"role":"user","content":"hello"})]).unwrap();
        assert_eq!(candidate.status,if suspend {TaskStatus::Aborted}else{TaskStatus::Done});assert_eq!(calls.load(Ordering::SeqCst),1);assert_eq!(provider.seen.lock().unwrap().len(),if suspend {1}else{2});assert_eq!(output.0.lock().unwrap().len(),1);
        if suspend {policy.release().unwrap();assert!(journal.acquire("resume").is_ok());}else{assert!(journal.acquire("resume").is_err());}
    }
}
