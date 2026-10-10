//! Native authorization, shared/local caps, settlement, recovery and quiescence.
use serde_json::json;
use std::sync::{Arc, Mutex};
use tofu_runtime::model_account::{Account, Bounds, Counts, Error, Observer, Phase, Receipt};
fn account(cap: Option<u64>) -> Arc<Account> {
    Account::new(
        7,
        "root".into(),
        cap,
        Bounds {
            actors: 8,
            attempts: 8,
            identity_bytes: 128,
        },
        None,
    )
    .unwrap()
}
fn counts(input: u64, output: u64) -> Counts {
    Counts {
        input,
        output,
        minimum_output: 1,
    }
}
#[test]
fn reserves_output_clamps_and_refunds_observed_tokens_only_once() {
    let a = account(Some(50));
    let mut l = a.try_reserve(7, "root", counts(20, 100)).unwrap();
    assert_eq!(l.output_tokens(), 30);
    l.mark_dispatched(|| false).unwrap();
    l.settle(
        Some(&json!({"prompt_tokens":20,"completion_tokens":5})),
        false,
    )
    .unwrap();
    l.settle(
        Some(&json!({"prompt_tokens":100,"completion_tokens":100})),
        false,
    )
    .unwrap();
    let mut next = a.try_reserve(7, "root", counts(20, 30)).unwrap();
    assert_eq!(next.output_tokens(), 5);
    next.settle(
        Some(&json!({"prompt_tokens":20,"completion_tokens":5})),
        false,
    )
    .unwrap();
    assert!(matches!(
        a.try_reserve(7, "root", counts(1, 1)),
        Err(Error::Exhausted)
    ));
    assert_eq!(a.snapshot(7).unwrap().tokens, 50);
}
#[test]
fn unsent_drop_voids_but_dispatched_drop_fences_finite_authority() {
    let a = account(Some(30));
    {
        let _l = a.try_reserve(7, "root", counts(10, 30)).unwrap();
    }
    assert!(a.snapshot(7).unwrap().complete);
    {
        let mut l = a.try_reserve(7, "root", counts(10, 30)).unwrap();
        l.mark_dispatched(|| false).unwrap();
    }
    assert!(!a.snapshot(7).unwrap().complete);
    assert!(matches!(
        a.try_reserve(7, "root", counts(1, 1)),
        Err(Error::UnknownUsage)
    ));
}
#[test]
fn sent_request_cannot_be_voided_and_cache_usage_is_accounted() {
    let a = account(Some(100));
    let mut l = a.try_reserve(7, "root", counts(1, 20)).unwrap();
    l.mark_dispatched(|| false).unwrap();
    l.settle(
        Some(&json!({"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":60})),
        true,
    )
    .unwrap();
    assert_eq!(a.snapshot(7).unwrap().tokens, 75);
}
#[test]
fn shared_and_local_caps_preserve_child_charges_after_detach() {
    let a = account(Some(100));
    a.attach(7, "child".into(), Some(35)).unwrap();
    let mut l = a.try_reserve(7, "child", counts(20, 80)).unwrap();
    assert_eq!(l.output_tokens(), 15);
    assert_eq!(a.detach(7, "child"), Err(Error::Busy));
    l.settle(
        Some(&json!({"prompt_tokens":20,"completion_tokens":10})),
        false,
    )
    .unwrap();
    assert!(matches!(
        a.try_reserve(7, "child", counts(20, 1)),
        Err(Error::Exhausted)
    ));
    a.detach(7, "child").unwrap();
    assert_eq!(a.snapshot(7).unwrap().tokens, 30);
    assert!(a.snapshot(7).unwrap().complete);
    assert!(matches!(
        a.try_reserve(8, "root", counts(1, 1)),
        Err(Error::Owner)
    ));
}
#[test]
fn restoration_is_one_time_and_unknown_usage_does_not_refund() {
    let a = account(Some(100));
    a.restore(7, "root", 70, 18, true).unwrap();
    let mut l = a.try_reserve(7, "root", counts(10, 50)).unwrap();
    assert_eq!(l.output_tokens(), 20);
    assert_eq!(l.ordinal(), 19);
    l.settle(None, true).unwrap();
    assert_eq!(a.restore(7, "root", 0, 0, true), Err(Error::Invalid));
    let b = account(Some(100));
    b.restore(7, "root", 70, 18, false).unwrap();
    assert!(matches!(
        b.try_reserve(7, "root", counts(1, 1)),
        Err(Error::UnknownUsage)
    ));
}
#[test]
fn atomic_reservations_never_overcommit_and_revoke_fences_dispatch() {
    let a = account(Some(50));
    let mut first = a.try_reserve(7, "root", counts(20, 30)).unwrap();
    assert!(matches!(
        a.try_reserve(7, "root", counts(1, 1)),
        Err(Error::Wait)
    ));
    assert_eq!(a.close(7), Err(Error::Busy));
    a.revoke(7, "root").unwrap();
    assert_eq!(first.mark_dispatched(|| false), Err(Error::Owner));
    first.settle(None, true).unwrap();
    a.close(7).unwrap();
}
struct Failing;
impl Observer for Failing {
    fn observe(&self, r: &Receipt) -> Result<(), Error> {
        if r.phase == Phase::Settled {
            Err(Error::Observer)
        } else {
            Ok(())
        }
    }
}
#[test]
fn settlement_observer_failure_releases_lease_but_never_certifies_usage() {
    let a = Account::new(
        7,
        "root".into(),
        Some(100),
        Bounds {
            actors: 2,
            attempts: 2,
            identity_bytes: 32,
        },
        Some(Arc::new(Failing)),
    )
    .unwrap();
    let mut l = a.try_reserve(7, "root", counts(1, 1)).unwrap();
    l.mark_dispatched(|| false).unwrap();
    assert_eq!(
        l.settle(
            Some(&json!({"prompt_tokens":1,"completion_tokens":1})),
            false
        ),
        Err(Error::Observer)
    );
    let s = a.snapshot(7).unwrap();
    assert_eq!(s.pending, 0);
    assert_eq!(s.tokens, 2);
    assert!(!s.complete);
    assert!(matches!(
        a.try_reserve(7, "root", counts(1, 1)),
        Err(Error::UnknownUsage)
    ));
}
#[test]
fn overflow_never_wraps_consumption_or_grants_authority() {
    let a = account(None);
    a.restore(7, "root", u64::MAX, 0, true).unwrap();
    let mut l = a.try_reserve(7, "root", counts(1, 1)).unwrap();
    assert_eq!(
        l.settle(
            Some(&json!({"prompt_tokens":1,"completion_tokens":1})),
            false
        ),
        Err(Error::Overflow)
    );
    assert_eq!(a.snapshot(7).unwrap().tokens, u64::MAX);
    assert!(!a.snapshot(7).unwrap().complete);
}
#[test]
fn async_wait_wakes_on_settlement_and_observes_cancellation() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    rt.block_on(async{
        let a=account(Some(50));let mut held=a.try_reserve(7,"root",counts(20,30)).unwrap();
        let (signal,stop)=tokio::sync::oneshot::channel::<()>();let work=a.reserve(7,"root",counts(1,1),async{let _=stop.await;});tokio::pin!(work);
        tokio::select!{biased;_= &mut work=>panic!("wait completed before release"),_=tokio::time::sleep(std::time::Duration::from_millis(5))=>{}}
        held.settle(None,true).unwrap();let l=work.await.unwrap();drop(l);drop(signal);
        let canceled=a.reserve(7,"root",counts(1,1),async{}).await;assert!(matches!(canceled,Err(Error::Owner)));
    });
}
struct RevokeDuringIntent(Mutex<Option<Arc<Account>>>);
impl Observer for RevokeDuringIntent {
    fn observe(&self, r: &Receipt) -> Result<(), Error> {
        if r.phase == Phase::DispatchIntent {
            self.0
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .revoke(7, "root")
                .unwrap();
        }
        Ok(())
    }
}
#[test]
fn dispatch_rechecks_authority_after_unlocked_observer() {
    let port = Arc::new(RevokeDuringIntent(Mutex::new(None)));
    let a = Account::new(
        7,
        "root".into(),
        Some(10),
        Bounds {
            actors: 1,
            attempts: 1,
            identity_bytes: 32,
        },
        Some(port.clone()),
    )
    .unwrap();
    *port.0.lock().unwrap() = Some(a.clone());
    let mut l = a.try_reserve(7, "root", counts(1, 1)).unwrap();
    assert_eq!(l.mark_dispatched(|| false), Err(Error::Owner));
    l.settle(None, true).unwrap();
    *port.0.lock().unwrap() = None;
}

#[test]
fn settlement_observation_blocks_new_admission_until_failure_is_known() {
    use std::sync::Barrier;
    struct Gate(Arc<Barrier>, Arc<Barrier>);
    impl Observer for Gate {
        fn observe(&self, r: &Receipt) -> Result<(), Error> {
            if r.phase == Phase::Settled {
                self.0.wait();
                self.1.wait();
                Err(Error::Observer)
            } else {
                Ok(())
            }
        }
    }
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let a = Account::new(
        7,
        "root".into(),
        Some(100),
        Bounds {
            actors: 1,
            attempts: 1,
            identity_bytes: 32,
        },
        Some(Arc::new(Gate(entered.clone(), release.clone()))),
    )
    .unwrap();
    let mut lease = a.try_reserve(7, "root", counts(1, 1)).unwrap();
    let worker = std::thread::spawn(move || {
        lease.settle(
            Some(&json!({"prompt_tokens":1,"completion_tokens":1})),
            false,
        )
    });
    entered.wait();
    assert!(matches!(
        a.try_reserve(7, "root", counts(1, 1)),
        Err(Error::Wait)
    ));
    let snap = a.snapshot(7).unwrap();
    assert_eq!(snap.pending_observations, 1);
    assert!(!snap.complete);
    assert_eq!(a.close(7), Err(Error::Busy));
    release.wait();
    assert_eq!(worker.join().unwrap(), Err(Error::Observer));
    assert!(matches!(
        a.try_reserve(7, "root", counts(1, 1)),
        Err(Error::UnknownUsage)
    ));
}

#[test]
fn budgeted_http_caps_the_real_wire_and_unknown_or_exhausted_usage_blocks_followup() {
    use std::{
        io::{Read, Write},
        net::TcpListener,
        time::Duration,
    };
    use tofu_runtime::{
        agent::{Provider, ProviderError},
        agent_http::{Configuration, HttpProvider, InputCounter, OutputField},
        http::{Cancellation, Policy, Transport},
        provider::Budget,
        session::Registry,
    };
    struct Count;
    impl InputCounter for Count {
        fn tokens(
            &self,
            owner: u64,
            task: &str,
            messages: &[serde_json::Value],
            template: &serde_json::Map<String, serde_json::Value>,
        ) -> Result<u64, serde_json::Value> {
            assert_eq!(owner, 7);
            assert_eq!(task, "root");
            assert_eq!(messages[0]["content"], "sealed fixture");
            assert_eq!(
                template
                    .get("max_tokens")
                    .or_else(|| template.get("max_completion_tokens"))
                    .unwrap(),
                100
            );
            Ok(20)
        }
    }
    for external in [false, true] {
    for (outputkey, outputfield, complete, observer_fault) in [
        ("max_tokens", OutputField::MaxTokens, true, false),
        ("max_tokens", OutputField::MaxTokens, false, false),
        (
            "max_completion_tokens",
            OutputField::MaxCompletionTokens,
            true,
            false,
        ),
        (
            "max_completion_tokens",
            OutputField::MaxCompletionTokens,
            false,
            false,
        ),
        ("max_tokens", OutputField::MaxTokens, true, true),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}/chat", listener.local_addr().unwrap());
        let sibling = listener.try_clone().unwrap();
        let server = std::thread::spawn(move || {
            let until = std::time::Instant::now() + Duration::from_secs(3);
            let mut stream = loop {
                match sibling.accept() {
                    Ok((s, _)) => break s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(std::time::Instant::now() < until);
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut head = vec![];
            let mut byte = [0];
            while !head.ends_with(b"\r\n\r\n") {
                stream.read_exact(&mut byte).unwrap();
                head.push(byte[0]);
                assert!(head.len() < 8192);
            }
            let len = String::from_utf8(head)
                .unwrap()
                .lines()
                .find_map(|line| {
                    line.to_lowercase()
                        .strip_prefix("content-length:")
                        .map(|n| n.trim().parse::<usize>().unwrap())
                })
                .unwrap();
            let mut body = vec![0; len];
            stream.read_exact(&mut body).unwrap();
            let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(body[outputkey], 30);
            assert_eq!(body["messages"][0]["content"], "sealed fixture");
            let wire = format!(
                "data: {}\n\ndata: [DONE]\n\n",
                json!({"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}],"usage":if complete{json!({"prompt_tokens":20,"completion_tokens":30})}else{json!({})}})
            );
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{wire}",wire.len()).unwrap();
            stream.flush().unwrap();
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let policy = Policy {
            connections: 1,
            idle_connections: 1,
            keepalive: Duration::from_secs(1),
            connect: Duration::from_secs(1),
            request_headers: Duration::from_secs(1),
            pool_wait: Duration::from_secs(1),
            stream_idle: Some(Duration::from_secs(1)),
            drain_timeout: Duration::from_millis(20),
            drain_bytes: 65536,
            drain_chunks: 16,
        };
        let transport = {
            let _guard = rt.enter();
            Transport::new(&url, None, policy).unwrap()
        };
        let cancel = Cancellation::new();
        let a = if observer_fault {
            Account::new(
                7,
                "root".into(),
                Some(50),
                Bounds {
                    actors: 1,
                    attempts: 1,
                    identity_bytes: 32,
                },
                Some(Arc::new(Failing)),
            )
            .unwrap()
        } else {
            account(Some(50))
        };
        let provider = HttpProvider::new(
            &rt,
            &transport,
            Configuration {
                headers: Default::default(),
                template: json!({"model":"fixture",(outputkey):100})
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
            },
            &cancel,
        )
        .unwrap();
        let provider = if external {
            provider.with_model_admission(Arc::new(ExternalAccount(a.clone())), 7, "root".into(), Arc::new(Count), outputfield)
        } else {
            provider.with_account(a.clone(), 7, "root".into(), Arc::new(Count), outputfield)
        }.unwrap();
        let session = Registry::new()
            .start("root", "task", 7, "request", Some(Duration::from_secs(2)))
            .unwrap();
        let messages = [json!({"role":"user","content":"sealed fixture"})];
        let returned = provider.round(&messages, &session);
        if observer_fault {
            match returned {
                Err(ProviderError::Failed(error)) => {
                    assert_eq!(error["kind"], "model_account_settlement");
                    assert_eq!(
                        error["usage"],
                        json!({"prompt_tokens":20,"completion_tokens":30})
                    );
                }
                _ => panic!("failed observation became success"),
            }
        } else {
            assert!(returned.is_ok());
        }
        match provider.round(&messages, &session) {
            Err(ProviderError::Failed(error)) => assert_eq!(
                error["kind"],
                if complete && !observer_fault {
                    "execution_token_budget_exhausted"
                } else {
                    "execution_token_usage_unknown"
                }
            ),
            _ => panic!("second request was admitted"),
        }
        server.join().unwrap();
        assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
        let snap = a.snapshot(7).unwrap();
        assert_eq!(snap.pending, 0);
        assert_eq!(snap.pending_observations, 0);
        assert_eq!(snap.complete, complete && !observer_fault);
    }
}
}

#[test]
fn actual_python_model_account_traces_match_native_reservation_and_settlement() {
    let cases: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python_model_account.json")).unwrap();
    for (index, row) in cases.as_array().unwrap().iter().enumerate() {
        let a = account(row["cap"].as_u64());
        let result = a.try_reserve(
            7,
            "root",
            counts(
                row["input"].as_u64().unwrap(),
                row["output"].as_u64().unwrap(),
            ),
        );
        if row.get("refused").is_some() {
            assert!(matches!(result, Err(Error::Exhausted)), "{index}: {row}");
        } else {
            let mut lease = result.unwrap();
            assert_eq!(
                lease.output_tokens(),
                row["admitted_output"].as_u64().unwrap(),
                "{index}"
            );
            assert_eq!(lease.ordinal(), row["ordinal"].as_u64().unwrap());
            if row["dispatched"] == true {
                lease.mark_dispatched(|| false).unwrap();
            }
            let raw = if row["usage"].is_null() {
                None
            } else {
                Some(&row["usage"])
            };
            lease.settle(raw, row["unsent"] == true).unwrap();
            lease.settle(raw, row["unsent"] == true).unwrap();
        }
        let actual = a.snapshot(7).unwrap();
        assert_eq!(
            actual.tokens,
            row["tokens"].as_u64().unwrap(),
            "{index}: {row}"
        );
        assert_eq!(
            actual.complete,
            row["complete"].as_bool().unwrap(),
            "{index}: {row}"
        );
        assert_eq!(actual.pending, row["pending"].as_u64().unwrap() as usize);
    }
}

// External composition reuses an actual shared account, never a mirror ledger.
struct ExternalAccount(Arc<Account>);
struct ExternalLease(tofu_runtime::model_account::Lease, std::sync::atomic::AtomicUsize);
fn account_failure(error: Error) -> serde_json::Value {
    json!({"kind": match error {
        Error::UnknownUsage => "execution_token_usage_unknown",
        Error::Exhausted => "execution_token_budget_exhausted",
        _ => "model_account_admission",
    }})
}
impl tofu_runtime::model_admission::Admission for ExternalAccount {
    fn authorize(&self, owner: u64, task: &str) -> Result<(), serde_json::Value> {
        self.0.authorize(owner, task).map_err(account_failure)
    }
    fn reserve(&self, owner: u64, task: &str, counts: Counts,
        _: &tofu_runtime::model_admission::Observation) -> Result<Box<dyn tofu_runtime::model_admission::Lease>, serde_json::Value> {
        self.0.try_reserve(owner, task, counts).map(|lease|
            Box::new(ExternalLease(lease, std::sync::atomic::AtomicUsize::new(0))) as Box<dyn tofu_runtime::model_admission::Lease>)
            .map_err(account_failure)
    }
}
impl tofu_runtime::model_admission::Lease for ExternalLease {
    fn output_tokens(&self) -> u64 {
        // The wire must use one frozen admission value, never reread a live port.
        if self.1.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 { self.0.output_tokens() } else { u64::MAX }
    }
    fn mark_dispatched(&mut self, observation: &tofu_runtime::model_admission::Observation) -> Result<(), serde_json::Value> {
        self.0.mark_dispatched(|| observation.is_stopped()).map_err(account_failure)
    }
    fn settle(&mut self, usage: Option<&serde_json::Value>, unsent: bool) -> Result<(), serde_json::Value> {
        self.0.settle(usage, unsent).map_err(account_failure)
    }
}
