//! Accepted borrowed text, network backpressure and cancellation quiescence.
use std::{
    future::Future,
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};
use tofu_runtime::{
    http::{Cancellation, Error, Policy, Request, TextDeliveryError, TextObserver, Transport},
    provider::{Budget, ChatStream, Error as ParseError, TextChannel},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::Notify,
};

fn budget() -> Budget {
    Budget {
        event_bytes: 65536,
        output_bytes: 65536,
        tool_calls: 8,
    }
}
fn wire(delta: serde_json::Value) -> String {
    format!(
        "data: {}\n\n",
        serde_json::json!({"choices":[{"delta":delta}]})
    )
}
#[test]
fn accepted_channel_order_and_unicode_match_across_all_network_splits() {
    let data = wire(serde_json::json!({"reasoning":"想","content":"答"}))
        + &wire(serde_json::json!({"content":"案"}))
        + &wire(
            serde_json::json!({"role":"thinking","content":"💡","thinking_signature":"private"}),
        );
    for chunk_bytes in [1, 3, 64, 65536] {
        let mut stream = ChatStream::new(budget()).unwrap();
        let mut seen = Vec::new();
        for chunk in data.as_bytes().chunks(chunk_bytes) {
            let mut spans = Vec::new();
            stream
                .feed_with_text(chunk, |span| spans.push(span))
                .unwrap();
            for span in spans {
                seen.push((span.channel, stream.span_text(span).unwrap().to_owned()));
            }
        }
        assert_eq!(
            seen,
            vec![
                (TextChannel::Reasoning, "想".into()),
                (TextChannel::Content, "答".into()),
                (TextChannel::Content, "案".into()),
                (TextChannel::Reasoning, "💡".into())
            ]
        );
        let result = stream.finish(true).unwrap();
        assert_eq!(result.message["content"], "答案");
        assert_eq!(result.message["reasoning_content"], "想💡");
    }
}
#[test]
fn rejected_output_has_no_span_and_valid_prefix_survives_later_failure() {
    let mut limits = budget();
    limits.output_bytes = 3;
    let mut stream = ChatStream::new(limits).unwrap();
    let mut spans = Vec::new();
    let data = wire(serde_json::json!({"content":"ok"}))
        + &wire(serde_json::json!({"content":"overflow"}));
    assert_eq!(
        stream.feed_with_text(data.as_bytes(), |span| spans.push(span)),
        Err(ParseError::OutputLimit)
    );
    assert_eq!(spans.len(), 1);
    assert_eq!(stream.span_text(spans[0]), Some("ok"));
    assert!(stream
        .feed_with_text(data.as_bytes(), |_| panic!("closed stream repeated text"))
        .is_err());
}
#[test]
fn span_metadata_is_bounded_by_nonempty_admitted_output() {
    let mut stream = ChatStream::new(budget()).unwrap();
    let mut spans = Vec::new();
    let data = wire(serde_json::json!({"content":"a"})).repeat(512);
    stream
        .feed_with_text(data.as_bytes(), |span| spans.push(span))
        .unwrap();
    assert_eq!(spans.len(), 512);
    assert_eq!(
        std::mem::size_of_val(spans.as_slice()),
        512 * std::mem::size_of::<tofu_runtime::provider::TextSpan>()
    );
    assert!(spans.capacity() * std::mem::size_of::<tofu_runtime::provider::TextSpan>() <= 512 * 32);
}
#[test]
fn same_channel_network_batch_reuses_one_span_without_copying_text() {
    let mut stream = ChatStream::new(budget()).unwrap();
    let mut spans: Vec<tofu_runtime::provider::TextSpan> = Vec::new();
    let data = wire(serde_json::json!({"content":"a"})).repeat(512);
    stream
        .feed_with_text(data.as_bytes(), |span| {
            if spans
                .last_mut()
                .is_some_and(|last| last.append_contiguous(span))
            {
                return;
            }
            spans.push(span);
        })
        .unwrap();
    assert_eq!(spans.len(), 1);
    assert_eq!(stream.span_text(spans[0]).unwrap(), "a".repeat(512));
    assert!(spans.capacity() * std::mem::size_of::<tofu_runtime::provider::TextSpan>() <= 128);
}
fn policy() -> Policy {
    Policy {
        connections: 1,
        idle_connections: 1,
        keepalive: Duration::from_secs(1),
        connect: Duration::from_secs(1),
        request_headers: Duration::from_secs(1),
        pool_wait: Duration::from_secs(1),
        stream_idle: Some(Duration::from_secs(1)),
        drain_timeout: Duration::from_millis(30),
        drain_bytes: 65536,
        drain_chunks: 8,
    }
}
fn request() -> Request {
    Request {
        headers: Default::default(),
        body: b"{}".to_vec(),
        budget: budget(),
        identical_tool_limit: None,
        tool_names: Arc::new(Default::default()),
    }
}
struct Observer {
    entered: Notify,
    release: Notify,
    seen: Mutex<Vec<String>>,
    failure: bool,
}
impl TextObserver for Observer {
    fn observe<'a>(
        &'a self,
        _channel: TextChannel,
        text: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), TextDeliveryError>> + Send + 'a>> {
        Box::pin(async move {
            self.seen.lock().unwrap().push(text.into());
            self.entered.notify_one();
            self.release.notified().await;
            if self.failure {
                Err(TextDeliveryError)
            } else {
                Ok(())
            }
        })
    }
}
async fn server() -> (String, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/chat", listener.local_addr().unwrap());
    let work = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut head = Vec::new();
        while !head.ends_with(b"\r\n\r\n") {
            head.push(socket.read_u8().await.unwrap());
            assert!(head.len() < 8192);
        }
        let length = std::str::from_utf8(&head)
            .unwrap()
            .lines()
            .find_map(|line| {
                line.to_ascii_lowercase()
                    .strip_prefix("content-length:")
                    .map(|value| value.trim().parse::<usize>().unwrap())
            })
            .unwrap();
        let mut body = vec![0; length];
        socket.read_exact(&mut body).await.unwrap();
        let data="data: {\"choices\":[{\"delta\":{\"content\":\"first\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n";
        socket.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{data}",data.len()).as_bytes()).await.unwrap();
    });
    (url, work)
}
#[tokio::test]
async fn cancellation_retains_response_admission_until_started_consumer_finishes() {
    let (url, server) = server().await;
    let transport = Arc::new(Transport::new(&url, None, policy()).unwrap());
    let observer = Arc::new(Observer {
        entered: Notify::new(),
        release: Notify::new(),
        seen: Mutex::new(Vec::new()),
        failure: false,
    });
    let cancel = Cancellation::new();
    let run = {
        let (t, o, c) = (transport.clone(), observer.clone(), cancel.clone());
        tokio::spawn(async move { t.execute_with_text(request(), &c, Some(o.as_ref())).await })
    };
    observer.entered.notified().await;
    cancel.cancel();
    tokio::task::yield_now().await;
    assert!(!run.is_finished());
    assert_eq!(transport.available_slots(), 0);
    observer.release.notify_one();
    assert!(matches!(run.await.unwrap(), Err(Error::Cancelled)));
    assert_eq!(transport.available_slots(), 1);
    server.await.unwrap();
}
#[tokio::test]
async fn required_consumer_failure_cannot_become_provider_success() {
    let (url, server) = server().await;
    let transport = Transport::new(&url, None, policy()).unwrap();
    let observer = Observer {
        entered: Notify::new(),
        release: Notify::new(),
        seen: Mutex::new(Vec::new()),
        failure: true,
    };
    observer.release.notify_one();
    assert!(matches!(
        transport
            .execute_with_text(request(), &Cancellation::new(), Some(&observer))
            .await,
        Err(Error::TextDelivery)
    ));
    assert_eq!(transport.available_slots(), 1);
    server.await.unwrap();
}
#[tokio::test]
async fn provider_session_stop_and_deadline_join_the_started_text_consumer() {
    use tofu_runtime::{
        agent::{Provider, ProviderError},
        agent_http::{Configuration, HttpProvider},
        session::Registry,
    };
    for deadline in [false, true] {
        let (url, server) = server().await;
        let observer = Arc::new(Observer {
            entered: Notify::new(),
            release: Notify::new(),
            seen: Mutex::new(Vec::new()),
            failure: false,
        });
        let session = Registry::new()
            .start(
                "stream-owner",
                "task",
                7,
                "",
                if deadline {
                    Some(Duration::from_millis(50))
                } else {
                    None
                },
            )
            .unwrap();
        let worker = {
            let (observer, session) = (observer.clone(), session.clone());
            std::thread::spawn(move || {
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                let transport = {
                    let _enter = runtime.enter();
                    Transport::new(&url, None, policy()).unwrap()
                };
                let cancel = Cancellation::new();
                let provider = HttpProvider::new(
                    &runtime,
                    &transport,
                    Configuration {
                        headers: Default::default(),
                        template: Default::default(),
                        budget: budget(),
                        request_bytes: 65536,
                        identical_tool_limit: None,
                    },
                    &cancel,
                )
                .unwrap()
                .with_text_observer(observer);
                let result = provider.round(
                    &[serde_json::json!({"role":"user","content":"hello"})],
                    &session,
                );
                assert_eq!(transport.available_slots(), 1);
                assert!(matches!(result, Err(ProviderError::Stopped)));
            })
        };
        observer.entered.notified().await;
        if deadline {
            tokio::time::sleep(Duration::from_millis(80)).await;
        } else {
            session.request_cancel("test stop").unwrap();
        }
        tokio::task::yield_now().await;
        assert!(!worker.is_finished());
        observer.release.notify_one();
        worker.join().unwrap();
        server.await.unwrap();
    }
}
