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
        request_headers: Duration::from_secs(1),
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
        assert_eq!(transport.available_slots(), 1);
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
    assert_eq!(transport.available_slots(), 1);
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
        assert_eq!(transport.available_slots(), 1);
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
        assert_eq!(transport.available_slots(), 1);
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
    assert_eq!(transport.available_slots(), 1);
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
async fn cancellation_while_waiting_for_pool_never_sends_a_second_request() {
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
        while transport.available_slots() != 0 {
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
    assert_eq!(transport.available_slots(), 1);
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
    let server=runtime.spawn(async move{let(mut socket,_)=listener.accept().await.unwrap();read_request(&mut socket).await;socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n").await.unwrap();assert!(stop.request_abort(7,"user_stop").unwrap());let mut byte=[0];assert_eq!(socket.read(&mut byte).await.unwrap(),0);});
    let transport = {
        let _entered = runtime.enter();
        Transport::new(&url, None, policy()).unwrap()
    };
    let result = transport
        .run_task(&runtime, &task, 7, request(), &Cancellation::new())
        .unwrap();
    assert_eq!(result.status, TaskStatus::Aborted);
    assert_eq!(transport.available_slots(), 1);
    assert_eq!(publisher.0.lock().unwrap().len(), 1);
    runtime.block_on(server).unwrap();
}
