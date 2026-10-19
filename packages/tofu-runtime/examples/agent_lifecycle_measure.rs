//! Bounded transient native agent lifetime counts; no application speed claim.
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use tofu_runtime::{
    agent::*,
    execution_contract::{ProviderStreamState, ResourceName},
    provider::StreamResult,
    session::{ExecutionSession, Registry as Sessions, ReleaseAck},
    task::*,
    tool_contract::*,
};
struct Model;
impl Provider for Model {
    fn round(
        &self,
        messages: &[Value],
        _: &Arc<ExecutionSession>,
    ) -> Result<StreamResult, ProviderError> {
        let message = if messages.last().is_some_and(|m| m["role"] == "tool") {
            json!({"role":"assistant","content":"done"})
        } else {
            json!({"role":"assistant","tool_calls":[{"id":"one","type":"function","function":{"name":"echo","arguments":"{}"}}]})
        };
        Ok(StreamResult {
            state: ProviderStreamState::ProviderFinished,
            message,
            usage: json!({}),
        cost: None,
            finish_reason: Some("stop".into()),
            done_seen: true,
            malformed_frames: 0,
        })
    }
}
struct Allow;
impl Authorization for Allow {
    fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        owner == 7
    }
}
struct Text;
impl Feedback for Text {
    fn content(&self, result: Result<Value, ToolFailure>, _: usize) -> Result<String, Value> {
        match result {
            Ok(Value::String(text)) => Ok(text),
            _ => Err(json!({"kind":"unexpected_tool_result"})),
        }
    }
}
struct Echo(Arc<AtomicUsize>);
impl Handler for Echo {
    fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
        let released = self.0.clone();
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
        Ok(json!("42"))
    }
}
struct Output(AtomicUsize);
impl TerminalPublisher for Output {
    fn publish_once(&self, _: &TerminalCandidate, _: &Value) -> Result<(), PortError> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
fn main() {
    let count: usize = std::env::args().nth(1).unwrap().parse().unwrap();
    let sessions = Sessions::new();
    let releases = Arc::new(AtomicUsize::new(0));
    let output = Arc::new(Output(AtomicUsize::new(0)));
    let catalog=Catalog::compile(7,"epoch",&json!({"echo":{"name":"echo","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}}})).unwrap();
    let registry = Registry::new(
        7,
        "epoch",
        vec![("echo".into(), Arc::new(Echo(releases.clone())))],
    )
    .unwrap();
    let agent = Agent {
        provider: &Model,
        catalog: &catalog,
        handlers: &registry,
        authorization: &Allow,
        feedback: &Text,
        sessions: &sessions,
        epoch: "epoch",
        checkpoints: None,
        progress: None,
        scheduling: None,
        approvals: None,
        round_admission: None,
        budget: Budget {
            rounds: Some(2),
            tool_calls: Some(1),
            retained_bytes: 4096,
            feedback_bytes: 1024,
        },
    };
    for index in 0..count {
        let id = format!("agent-lifecycle:{index}");
        let session = sessions.start(&id, "task", 7, "", None).unwrap();
        let task = Task::new(
            TaskIdentity {
                task_id: id.clone(),
                owner_user_id: 7,
            },
            session,
            None,
            output.clone(),
        )
        .unwrap();
        let candidate = agent.run(&task, 7, &id, vec![]).unwrap();
        assert_eq!(candidate.status, TaskStatus::Done);
        assert_eq!(candidate.result.as_ref().unwrap()["llm_rounds"], 2);
        assert_eq!(sessions.active_count().unwrap(), 0);
    }
    assert_eq!(releases.load(Ordering::SeqCst), count);
    assert_eq!(output.0.load(Ordering::SeqCst), count);
    println!(
        "{}",
        json!({"completed_agents":count,"provider_rounds":count*2,"tool_releases":releases.load(Ordering::SeqCst),"terminal_events":output.0.load(Ordering::SeqCst),"active_scopes":sessions.active_count().unwrap()})
    );
}
