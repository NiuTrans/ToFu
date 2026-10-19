//! Real HTTP refusal and shared-account debt through the explicit counted port.
use serde_json::{json, Value};
use std::{sync::Arc, time::Duration};
use tofu_runtime::{
    agent::{Provider, ProviderError},
    agent_http::{Configuration, HttpProvider, InputCounter, OutputField},
    http::{Cancellation, Policy, Transport},
    model_account::{Account, Bounds, Counts},
    model_admission::{Admission, Lease},
    provider::Budget,
    session::{ExecutionSession, Registry},
};
struct Counter;
impl InputCounter for Counter {
    fn tokens(
        &self,
        owner: u64,
        task: &str,
        _: &[Value],
        _: &serde_json::Map<String, Value>,
    ) -> Result<u64, Value> {
        assert_eq!((owner, task), (7, "root"));
        Ok(20)
    }
}
#[derive(Clone, Copy)]
enum Mode {
    Deny,
    Cap(u64),
    DispatchFault,
    DispatchPanic,
    Headers(&'static str, bool),
}
struct Port {
    account: Arc<Account>,
    mode: Mode,
}
struct Held {
    lease: tofu_runtime::model_account::Lease,
    mode: Mode,
}
impl Admission for Port {
    fn authorize(&self, owner: u64, task: &str) -> Result<(), Value> {
        self.account
            .authorize(owner, task)
            .map_err(|_| json!({"kind":"owner"}))
    }
    fn reserve(
        &self,
        owner: u64,
        task: &str,
        counts: Counts,
        _: &tofu_runtime::model_admission::Observation,
    ) -> Result<Box<dyn Lease>, Value> {
        if matches!(self.mode, Mode::Deny) {
            return Err(json!({"kind":"shared_budget_denied"}));
        }
        let lease = self
            .account
            .try_reserve(owner, task, counts)
            .map_err(|_| json!({"kind":"reserve"}))?;
        Ok(Box::new(Held {
            lease,
            mode: self.mode,
        }))
    }
}
impl Lease for Held {
    fn output_tokens(&self) -> u64 {
        match self.mode {
            Mode::Cap(cap) => cap,
            _ => self.lease.output_tokens(),
        }
    }
    fn mark_dispatched(
        &mut self,
        _: &tofu_runtime::model_admission::Observation,
    ) -> Result<(), Value> {
        self.lease
            .mark_dispatched(|| false)
            .map_err(|_| json!({"kind":"dispatch"}))?;
        match self.mode {
            Mode::DispatchFault => Err(json!({"kind":"dispatch_delivery_unknown"})),
            Mode::DispatchPanic => panic!("dispatch reply lost after account intent"),
            _ => Ok(()),
        }
    }
    fn mark_dispatched_with_headers(&mut self, observation: &tofu_runtime::model_admission::Observation) -> Result<Option<reqwest::header::HeaderMap>, Value> {
        self.mark_dispatched(observation)?;
        if let Mode::Headers(name, duplicate) = self.mode {
            let mut headers = reqwest::header::HeaderMap::new();
            headers.insert(name, reqwest::header::HeaderValue::from_static("first"));
            if duplicate { headers.append(name, reqwest::header::HeaderValue::from_static("second")); }
            Ok(Some(headers))
        } else { Ok(None) }
    }
    fn settle(&mut self, usage: Option<&Value>, unsent: bool) -> Result<(), Value> {
        self.lease
            .settle(usage, unsent)
            .map_err(|_| json!({"kind":"settle"}))
    }
}
fn account() -> Arc<Account> {
    Account::new(
        7,
        "root".into(),
        Some(50),
        Bounds {
            actors: 1,
            attempts: 1,
            identity_bytes: 64,
        },
        None,
    )
    .unwrap()
}
fn policy() -> Policy {
    Policy {
        connections: 1,
        idle_connections: 1,
        keepalive: Duration::from_secs(1),
        connect: Duration::from_secs(1),
        request_headers: Some(Duration::from_secs(1)),
        write: None,
        pool_wait: Duration::from_secs(1),
        stream_idle: Some(Duration::from_secs(1)),
        drain_timeout: Duration::from_millis(20),
        drain_bytes: 65536,
        drain_chunks: 16,
    }
}
fn configuration() -> Configuration {
    Configuration {
        headers: Default::default(),
        template: json!({"model":"fixture","max_tokens":100})
            .as_object()
            .unwrap()
            .clone(),
        budget: Budget {
            event_bytes: 65536,
            output_bytes: 65536,
            tool_calls: 8,
        },
        request_bytes: 65536,
        identical_tool_limit: None,
    }
}
#[test]
fn refusal_and_invalid_output_never_send_or_charge_shared_authority() {
    for mode in [Mode::Deny, Mode::Cap(0), Mode::Cap(101)] {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let transport = {
            let _entered = runtime.enter();
            Transport::new(
                &format!("http://{}/chat", listener.local_addr().unwrap()),
                None,
                policy(),
            )
            .unwrap()
        };
        let cancelled = Cancellation::new();
        let account = account();
        let provider = HttpProvider::new(&runtime, &transport, configuration(), &cancelled)
            .unwrap()
            .with_model_admission(
                Arc::new(Port {
                    account: account.clone(),
                    mode,
                }),
                7,
                "root".into(),
                Arc::new(Counter),
                OutputField::MaxTokens,
            )
            .unwrap();
        let session = Registry::new()
            .start("root", "task", 7, "", Some(Duration::from_secs(2)))
            .unwrap();
        let result = provider.round(&[json!({"role":"user","content":"bounded"})], &session);
        let Err(ProviderError::Failed(error)) = result else {
            panic!("invalid admission succeeded")
        };
        assert_eq!(
            error["kind"],
            if matches!(mode, Mode::Deny) {
                "shared_budget_denied"
            } else {
                "model_account_output_cap"
            }
        );
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
        let state = account.snapshot(7).unwrap();
        assert_eq!(
            (state.tokens, state.pending, state.reserved, state.complete),
            (0, 0, 0, true)
        );
    }
}
#[test]
fn unknown_dispatch_intent_joins_settlement_and_never_refunds_as_unsent() {
    for mode in [Mode::DispatchFault, Mode::DispatchPanic, Mode::Headers("host", false), Mode::Headers("authorization", true)] {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let transport = {
            let _entered = runtime.enter();
            Transport::new(
                &format!("http://{}/chat", listener.local_addr().unwrap()),
                None,
                policy(),
            )
            .unwrap()
        };
        let cancelled = Cancellation::new();
        let account = account();
        let provider = HttpProvider::new(&runtime, &transport, configuration(), &cancelled)
            .unwrap()
            .with_model_admission(
                Arc::new(Port {
                    account: account.clone(),
                    mode,
                }),
                7,
                "root".into(),
                Arc::new(Counter),
                OutputField::MaxTokens,
            )
            .unwrap();
        let session = Registry::new()
            .start("root", "task", 7, "", Some(Duration::from_secs(2)))
            .unwrap();
        let result = provider.round(&[json!({"role":"user","content":"bounded"})], &session);
        assert!(matches!(result, Err(ProviderError::Failed(_))));
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
        let state = account.snapshot(7).unwrap();
        assert_eq!(state.pending, 0);
        assert!(!state.complete);
        assert!(matches!(
            account.try_reserve(
                7,
                "root",
                Counts {
                    input: 1,
                    output: 1,
                    minimum_output: 1
                }
            ),
            Err(tofu_runtime::model_account::Error::UnknownUsage)
        ));
    }
}

#[test]
fn cancellation_during_counting_never_acquires_shared_authority() {
    struct Cancels(Arc<ExecutionSession>);
    impl InputCounter for Cancels {
        fn tokens(
            &self,
            _: u64,
            _: &str,
            _: &[Value],
            _: &serde_json::Map<String, Value>,
        ) -> Result<u64, Value> {
            self.0.request_cancel("count-cancel").unwrap();
            Ok(20)
        }
    }
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let transport = {
        let _entered = runtime.enter();
        Transport::new(
            &format!("http://{}/chat", listener.local_addr().unwrap()),
            None,
            policy(),
        )
        .unwrap()
    };
    let cancelled = Cancellation::new();
    let account = account();
    let session = Registry::new()
        .start("root", "task", 7, "", Some(Duration::from_secs(2)))
        .unwrap();
    let provider = HttpProvider::new(&runtime, &transport, configuration(), &cancelled)
        .unwrap()
        .with_model_admission(
            Arc::new(Port {
                account: account.clone(),
                mode: Mode::Deny,
            }),
            7,
            "root".into(),
            Arc::new(Cancels(session.clone())),
            OutputField::MaxTokens,
        )
        .unwrap();
    assert!(matches!(
        provider.round(&[json!({"role":"user","content":"bounded"})], &session),
        Err(ProviderError::Stopped)
    ));
    let state = account.snapshot(7).unwrap();
    assert_eq!(state.last_ordinal, 0);
    assert_eq!(state.pending, 0);
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#[test]
fn serialization_failure_voids_shared_reservation_before_dispatch_intent() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let transport = {
        let _entered = runtime.enter();
        Transport::new(
            &format!("http://{}/chat", listener.local_addr().unwrap()),
            None,
            policy(),
        )
        .unwrap()
    };
    let cancelled = Cancellation::new();
    let account = account();
    let mut config = configuration();
    config.request_bytes = 1;
    let provider = HttpProvider::new(&runtime, &transport, config, &cancelled)
        .unwrap()
        .with_model_admission(
            Arc::new(Port {
                account: account.clone(),
                mode: Mode::DispatchFault,
            }),
            7,
            "root".into(),
            Arc::new(Counter),
            OutputField::MaxTokens,
        )
        .unwrap();
    let session = Registry::new()
        .start("root", "task", 7, "", Some(Duration::from_secs(2)))
        .unwrap();
    assert!(matches!(
        provider.round(&[json!({"role":"user","content":"bounded"})], &session),
        Err(ProviderError::Failed(_))
    ));
    let state = account.snapshot(7).unwrap();
    assert_eq!((state.tokens, state.pending, state.complete), (0, 0, true));
    assert_eq!(state.last_ordinal, 1);
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
