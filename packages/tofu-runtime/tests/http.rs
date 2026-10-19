//! Real TCP fixtures exercise pooling, cancellation and terminal drain ownership.
use std::time::Duration;
use tofu_runtime::{
    execution_contract::ProviderStreamState,
    http::{Cancellation, Error, Policy, Request, Transport},
    provider::Budget,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};
fn policy() -> Policy {
    Policy {
        connections: 1,
        idle_connections: 1,
        keepalive: Duration::from_secs(1),
        connect: Duration::from_secs(1),
        request_headers: Some(Duration::from_secs(1)),
        write: None,
        pool_wait: Duration::from_secs(1),
        stream_idle: Some(Duration::from_millis(80)),
        drain_timeout: Duration::from_millis(30),
        drain_bytes: 65536,
        drain_chunks: 32,
    }
}
fn request() -> Request {
    Request {
        tool_names: std::sync::Arc::new(Default::default()),
        identical_tool_limit: None,
        headers: Default::default(),
        body: b"{}".to_vec(),
        budget: Budget {
            event_bytes: 1048576,
            output_bytes: 1048576,
            tool_calls: 128,
        },
    }
}
const WIRE:&str="data: {\"choices\":[{\"delta\":{\"content\":\"hello\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n";
#[tokio::test]
async fn responses_reuse_shared_connection_and_retain_provider_rejection() {
    let (listener, url) = listener().await;
    let failure = serde_json::json!({"code":"rate_limit_exceeded","message":"slow down","http_status":429});
    let wire_failure = failure.clone();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        read_request(&mut socket).await;
        reply(&mut socket, "data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"message\",\"role\":\"assistant\",\"content\":[{\"type\":\"output_text\",\"text\":\"hello\"}]}]}}\n\n").await;
        read_request(&mut socket).await;
        reply(&mut socket, &format!("data: {}\n\n", serde_json::json!({"type":"response.failed","response":{"error":wire_failure}}))).await;
    });
    let transport = Transport::new(&url, None, policy()).unwrap();
    let budget = tofu_runtime::responses::Budget { items:128, retained_bytes:1048576 };
    let result = transport.execute_responses_with_text(request(), &Cancellation::new(), None, budget, Default::default()).await.unwrap();
    assert_eq!(result.message["content"], "hello");
    let error = transport.execute_responses_with_text(request(), &Cancellation::new(), None, budget, Default::default()).await.unwrap_err();
    let Error::ProviderRejected(error) = error else { panic!("missing provider rejection") };
    assert_eq!(error["code"], failure["code"]);
    assert_eq!(error["http_status"], 429);
    assert_eq!(error["message"], "rate_limit_exceeded: slow down");
    server.await.unwrap();
}
async fn listener() -> (TcpListener, String) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}/chat", listener.local_addr().unwrap());
    (listener, endpoint)
}
async fn read_request(stream: &mut TcpStream) {
    let mut bytes = Vec::new();
    while !bytes.ends_with(b"\r\n\r\n") {
        let b = stream.read_u8().await.unwrap();
        bytes.push(b);
        assert!(bytes.len() < 8192);
    }
    let mut body = [0; 2];
    stream.read_exact(&mut body).await.unwrap();
    assert_eq!(&body, b"{}");
}
async fn reply(stream: &mut TcpStream, body: &str) {
    stream.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{body}",body.len()).as_bytes()).await.unwrap();
}
#[tokio::test]
async fn two_attempts_reuse_one_connection_and_release_all_slots() {
    let (listener, url) = listener().await;
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        for _ in 0..2 {
            read_request(&mut socket).await;
            reply(&mut socket, WIRE).await;
        }
    });
    let transport = Transport::new(&url, None, policy()).unwrap();
    for _ in 0..2 {
        let result = transport
            .execute(request(), &Cancellation::new())
            .await
            .unwrap();
        assert_eq!(result.state, ProviderStreamState::ProviderFinished);
        assert_eq!(result.message["content"], "hello");
        assert_eq!(transport.active_requests(), 0);
    }
    server.await.unwrap();
}
#[tokio::test]
async fn cancellation_interrupts_silent_body_and_releases_admission() {
    let (listener, url) = listener().await;
    let server = tokio::spawn(async move {
        let (mut s, _) = listener.accept().await.unwrap();
        read_request(&mut s).await;
        s.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n").await.unwrap();
        let mut b = [0; 1];
        assert_eq!(s.read(&mut b).await.unwrap(), 0);
    });
    let transport = Transport::new(&url, None, policy()).unwrap();
    let cancel = Cancellation::new();
    let trigger = cancel.clone();
    let cancellation = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(20)).await;
        trigger.cancel();
    });
    assert!(matches!(
        transport.execute(request(), &cancel).await,
        Err(Error::Cancelled)
    ));
    assert_eq!(transport.active_requests(), 0);
    cancellation.await.unwrap();
    server.await.unwrap();
}
#[tokio::test]
async fn silent_stream_times_out_but_terminal_drain_timeout_preserves_success() {
    for terminal in [false, true] {
        let (listener, url) = listener().await;
        let server = tokio::spawn(async move {
            let (mut s, _) = listener.accept().await.unwrap();
            read_request(&mut s).await;
            s.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n").await.unwrap();
            if terminal {
                s.write_all(format!("{:x}\r\n{WIRE}\r\n", WIRE.len()).as_bytes())
                    .await
                    .unwrap();
            }
            let mut b = [0; 1];
            assert_eq!(s.read(&mut b).await.unwrap(), 0);
        });
        let transport = Transport::new(&url, None, policy()).unwrap();
        let result = transport.execute(request(), &Cancellation::new()).await;
        if terminal {
            assert_eq!(result.unwrap().state, ProviderStreamState::ProviderFinished);
        } else {
            assert!(matches!(result, Err(Error::ReadTimeout)));
        }
        assert_eq!(transport.active_requests(), 0);
        server.await.unwrap();
    }
}
#[tokio::test]
async fn status_and_wrong_content_type_do_not_parse_or_follow_redirects() {
    for response in [
        "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/private\r\nContent-Length: 0\r\n\r\n",
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
    ] {
        let (listener, url) = listener().await;
        let server = tokio::spawn(async move {
            let (mut s, _) = listener.accept().await.unwrap();
            read_request(&mut s).await;
            s.write_all(response.as_bytes()).await.unwrap();
        });
        let transport = Transport::new(&url, None, policy()).unwrap();
        let result = transport.execute(request(), &Cancellation::new()).await;
        assert!(matches!(
            result,
            Err(Error::HttpStatus(302) | Error::UnexpectedContentType)
        ));
        assert_eq!(transport.active_requests(), 0);
        server.await.unwrap();
    }
}
#[tokio::test]
async fn already_cancelled_attempt_never_contacts_endpoint() {
    let transport = Transport::new("http://127.0.0.1:1/chat", None, policy()).unwrap();
    let cancel = Cancellation::new();
    cancel.cancel();
    assert!(matches!(
        transport.execute(request(), &cancel).await,
        Err(Error::Cancelled)
    ));
    assert_eq!(transport.active_requests(), 0);
}
#[test]
fn misconfiguration_is_rejected_before_network_work() {
    let mut p = policy();
    p.connections = 0;
    assert!(matches!(
        Transport::new("https://example.org", None, p),
        Err(Error::Configuration)
    ));
    assert!(matches!(
        Transport::new("https://secret@example.org", None, policy()),
        Err(Error::Configuration)
    ));
}

#[tokio::test]
async fn unbounded_response_headers_do_not_consume_the_stream_idle_window() {
    let (listener, url) = listener().await;
    let (arrived, arrival) = tokio::sync::oneshot::channel();
    let (release, released) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        read_request(&mut socket).await;
        arrived.send(()).unwrap();
        released.await.unwrap();
        reply(&mut socket, WIRE).await;
    });
    let mut limits = policy();
    limits.request_headers = None;
    limits.stream_idle = Some(Duration::from_millis(30));
    let transport = Transport::new(&url, None, limits).unwrap();
    let cancel = Cancellation::new();
    let operation = transport.execute(request(), &cancel);
    tokio::pin!(operation);
    tokio::select! {
        ready = arrival => ready.unwrap(),
        result = &mut operation => panic!("header wait finished before response: {result:?}"),
    }
    tokio::select! {
        _ = tokio::time::sleep(Duration::from_millis(150)) => {},
        result = &mut operation => panic!("stream idle budget covered header wait: {result:?}"),
    }
    assert_eq!(transport.active_requests(), 1);
    release.send(()).unwrap();
    let result = tokio::time::timeout(Duration::from_secs(5), &mut operation)
        .await.unwrap().unwrap();
    assert_eq!(result.message["content"], "hello");
    assert_eq!(transport.active_requests(), 0);
    server.await.unwrap();
}

#[tokio::test]
async fn header_timeout_and_unbounded_wait_stop_close_the_peer_and_allow_next_attempt() {
    for stopped in [false, true] {
        let (listener, url) = listener().await;
        let (arrived, arrival) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            read_request(&mut socket).await;
            arrived.send(()).unwrap();
            let mut byte = [0];
            assert_eq!(tokio::time::timeout(Duration::from_secs(5), socket.read(&mut byte))
                .await.unwrap().unwrap(), 0, "retired request retained its physical connection");
            let (mut next, _) = listener.accept().await.unwrap();
            read_request(&mut next).await;
            reply(&mut next, WIRE).await;
        });
        let mut limits = policy();
        limits.request_headers = if stopped { None } else { Some(Duration::from_millis(150)) };
        let transport = Transport::new(&url, None, limits).unwrap();
        let cancel = Cancellation::new();
        let operation = transport.execute(request(), &cancel);
        tokio::pin!(operation);
        tokio::select! {
            ready = arrival => ready.unwrap(),
            result = &mut operation => panic!("request ended before owned peer read: {result:?}"),
        }
        assert_eq!(transport.active_requests(), 1);
        if stopped { cancel.cancel(); }
        assert_eq!(tokio::time::timeout(Duration::from_secs(5), &mut operation).await.unwrap()
            .unwrap_err(), if stopped { Error::Cancelled } else { Error::RequestTimeout });
        assert_eq!(transport.active_requests(), 0);
        let result = tokio::time::timeout(Duration::from_secs(5),
            transport.execute(request(), &Cancellation::new())).await.unwrap().unwrap();
        assert_eq!(result.message["content"], "hello");
        assert_eq!(transport.active_requests(), 0);
        tokio::time::timeout(Duration::from_secs(5), server).await.unwrap().unwrap();
    }
}

#[test]
fn zero_header_budget_refuses_while_explicit_unbounded_policy_is_valid() {
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let _entered = runtime.enter();
    let mut limits = policy();
    limits.request_headers = Some(Duration::ZERO);
    assert!(matches!(Transport::new("http://127.0.0.1:1/chat", None, limits), Err(Error::Configuration)));
    limits.request_headers = None;
    assert!(Transport::new("http://127.0.0.1:1/chat", None, limits).is_ok());
}
#[tokio::test]
async fn already_cancelled_demand_never_sends_a_second_request() {
    let (listener, url) = listener().await;
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut s, _) = listener.accept().await.unwrap();
        read_request(&mut s).await;
        s.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n").await.unwrap();
        ready_tx.send(()).unwrap();
        let mut b = [0; 1];
        assert_eq!(s.read(&mut b).await.unwrap(), 0);
    });
    let transport = std::sync::Arc::new(Transport::new(&url, None, policy()).unwrap());
    let first_cancel = Cancellation::new();
    let first_transport = transport.clone();
    let first_token = first_cancel.clone();
    let first = tokio::spawn(async move { first_transport.execute(request(), &first_token).await });
    tokio::time::timeout(Duration::from_secs(1), async {
        while transport.active_requests() != 1 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    ready_rx.await.unwrap();
    let second_cancel = Cancellation::new();
    second_cancel.cancel();
    assert!(matches!(
        transport.execute(request(), &second_cancel).await,
        Err(Error::Cancelled)
    ));
    first_cancel.cancel();
    assert!(matches!(first.await.unwrap(), Err(Error::Cancelled)));
    assert_eq!(transport.active_requests(), 0);
    server.await.unwrap();
}
#[test]
fn task_stop_wakes_silent_http_and_publishes_aborted_once() {
    use serde_json::Value;
    use std::sync::{Arc, Mutex};
    use tofu_runtime::{session::Registry, task::*};
    struct Publisher(Mutex<Vec<Value>>);
    impl TerminalPublisher for Publisher {
        fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
            self.0.lock().unwrap().push(event.clone());
            Ok(())
        }
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let (listener, url) = runtime.block_on(listener());
    let registry = Registry::new();
    let session = registry.start("network-stop", "task", 7, "", None).unwrap();
    let publisher = Arc::new(Publisher(Mutex::new(Vec::new())));
    let task = Arc::new(
        Task::new(
            TaskIdentity {
                task_id: "network-stop".into(),
                owner_user_id: 7,
            },
            session,
            None,
            publisher.clone(),
        )
        .unwrap(),
    );
    let stop = task.clone();
    // Stop before the client consumes response headers may close with TCP RST.
    // Both EOF and reset attest closure; timeout/other I/O failures still fail.
    let server=runtime.spawn(async move{let(mut socket,_)=listener.accept().await.unwrap();read_request(&mut socket).await;socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n").await.unwrap();assert!(stop.request_abort(7,"user_stop").unwrap());let mut byte=[0];let closed=socket.read(&mut byte).await;assert!(matches!(&closed,Ok(0))||matches!(&closed,Err(error) if error.kind()==std::io::ErrorKind::ConnectionReset),"cancelled peer remained live: {closed:?}");});
    let transport = {
        let _entered = runtime.enter();
        Transport::new(&url, None, policy()).unwrap()
    };
    let result = transport
        .run_task(&runtime, &task, 7, request(), &Cancellation::new())
        .unwrap();
    assert_eq!(result.status, TaskStatus::Aborted);
    assert_eq!(transport.active_requests(), 0);
    assert_eq!(publisher.0.lock().unwrap().len(), 1);
    runtime.block_on(server).unwrap();
}

#[tokio::test]
async fn one_legacy_connection_hint_admits_independent_live_requests() {
    const COUNT: usize = 32;
    let (listener, url) = listener().await;
    let (arrived_tx, mut arrived_rx) = tokio::sync::mpsc::unbounded_channel();
    let (release_tx, release_rx) = tokio::sync::watch::channel(false);
    let server = tokio::spawn(async move {
        let mut handlers = tokio::task::JoinSet::new();
        for _ in 0..COUNT {
            let (mut socket, _) = listener.accept().await.unwrap();
            let arrived = arrived_tx.clone();
            let mut release = release_rx.clone();
            handlers.spawn(async move {
                read_request(&mut socket).await;
                arrived.send(()).unwrap();
                while !*release.borrow_and_update() {
                    release.changed().await.unwrap();
                }
                reply(&mut socket, WIRE).await;
            });
        }
        while let Some(result) = handlers.join_next().await { result.unwrap(); }
    });
    let mut hints = policy();
    hints.pool_wait = Duration::from_millis(1);
    hints.request_headers = Some(Duration::from_secs(5));
    let transport = std::sync::Arc::new(Transport::new(&url, None, hints).unwrap());
    let mut clients = tokio::task::JoinSet::new();
    for _ in 0..COUNT {
        let transport = transport.clone();
        clients.spawn(async move { transport.execute(request(), &Cancellation::new()).await });
    }
    let start = std::time::Instant::now();
    let arrivals = tokio::time::timeout(Duration::from_secs(3), async {
        for _ in 0..COUNT { arrived_rx.recv().await.unwrap(); }
    }).await;
    let live = transport.active_requests();
    release_tx.send_replace(true);
    assert!(arrivals.is_ok(), "independent requests were gated by a legacy pool hint");
    assert_eq!(live, COUNT);
    println!("HTTP_DEMAND_MEASUREMENT count={COUNT} legacy_connections=1 live={live} arrival_us={}",start.elapsed().as_micros());
    while let Some(result) = clients.join_next().await {
        assert_eq!(result.unwrap().unwrap().state, ProviderStreamState::ProviderFinished);
    }
    assert_eq!(transport.active_requests(), 0);
    server.await.unwrap();
}

#[test]
fn idle_reuse_is_independent_of_retired_active_hint() {
    let mut hints = policy();
    hints.idle_connections = 32;
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let _entered = runtime.enter();
    assert!(Transport::new("http://127.0.0.1:1",None,hints).is_ok());
}

#[tokio::test]
async fn pending_physical_upload_times_out_closes_socket_and_allows_next_request() {
    exercise_physical_upload_failure(false, Some(Duration::from_millis(80))).await;
}

#[tokio::test]
async fn stop_during_physical_upload_closes_socket_and_allows_next_request() {
    for budget in [None, Some(Duration::from_secs(5))] {
        exercise_physical_upload_failure(true, budget).await;
    }
}

async fn exercise_physical_upload_failure(cancel_upload: bool, write_budget: Option<Duration>) {
    let (listener, url) = listener().await;
    let (release, released) = tokio::sync::oneshot::channel();
    let (arrived, arrival) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut headers = Vec::new();
        while !headers.ends_with(b"\r\n\r\n") {
            headers.push(socket.read_u8().await.unwrap());
            assert!(headers.len() < 8192);
        }
        arrived.send(()).unwrap();
        // Keep the request physically blocked until the transport has reported
        // its write failure; body-production callbacks cannot satisfy this.
        released.await.unwrap();
        let mut bytes = [0; 65536];
        let mut received = 0;
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                match socket.read(&mut bytes).await {
                    Ok(0) => break,
                    Ok(count) => received += count,
                    Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => break,
                    Err(error) => panic!("upload peer: {error}"),
                }
            }
        }).await.expect("failed request did not close its physical socket");
        assert!(received < 32 << 20, "fixture did not block the upload");
        let (mut next, _) = listener.accept().await.unwrap();
        read_request(&mut next).await;
        reply(&mut next, WIRE).await;
    });
    let mut settings = policy();
    settings.request_headers = None;
    settings.write = write_budget;
    let transport = Transport::new(&url, None, settings).unwrap();
    let mut upload = request();
    upload.body = vec![b'x'; 32 << 20];
    let cancel = Cancellation::new();
    let trigger = cancel.clone();
    let cancellation = tokio::spawn(async move {
        arrival.await.unwrap();
        if cancel_upload { trigger.cancel(); }
    });
    let result = tokio::time::timeout(Duration::from_secs(5),
        transport.execute(upload, &cancel)).await;
    cancellation.await.unwrap();
    // Always release and join the peer, including an unexpected client result.
    release.send(()).unwrap();
    assert_eq!(transport.active_requests(), 0);
    let next = transport.execute(request(), &Cancellation::new()).await;
    server.await.unwrap();
    assert_eq!(result.expect("write budget never fired").unwrap_err(), if cancel_upload { Error::Cancelled } else { Error::WriteTimeout });
    assert_eq!(next.unwrap().message["content"], "hello");
    assert_eq!(transport.active_requests(), 0);
}

#[tokio::test]
async fn write_budget_excludes_header_wait_and_idle_connection_reuse() {
    let (listener, url) = listener().await;
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        for _ in 0..2 {
            read_request(&mut socket).await;
            tokio::time::sleep(Duration::from_millis(150)).await;
            reply(&mut socket, WIRE).await;
        }
    });
    let mut settings = policy();
    settings.request_headers = None;
    settings.write = Some(Duration::from_millis(30));
    let transport = Transport::new(&url, None, settings).unwrap();
    for _ in 0..2 {
        assert_eq!(transport.execute(request(), &Cancellation::new()).await.unwrap().message["content"], "hello");
        tokio::time::sleep(Duration::from_millis(90)).await;
    }
    server.await.unwrap();
    assert_eq!(transport.active_requests(), 0);
}

#[test]
fn zero_write_budget_refuses_before_dispatch() {
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let _entered = runtime.enter();
    let mut settings = policy();
    settings.write = Some(Duration::ZERO);
    assert!(matches!(Transport::new("http://127.0.0.1:1", None, settings), Err(Error::Configuration)));
}
