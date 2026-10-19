//! Actual stdio owner Stop/deadline and exact nullable HTTP policy admission.
#![cfg(target_os = "linux")]
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn configuration(endpoint: String) -> Value {
    json!({
        "version":"tofu.native-agent-stdio/v1", "type":"configure",
        "owner_user_id":7, "task_id":"headers", "epoch":"headers",
        "endpoint":endpoint, "headers":{}, "template":{"model":"fixture"},
        "messages":[{"role":"user","content":"hello"}], "tools":{},
        "event_bytes":65536, "output_bytes":65536, "tool_calls":8,
        "request_bytes":65536, "rounds":3, "retained_bytes":65536,
        "feedback_bytes":4096, "tool_frame_bytes":8192, "deadline_ms":5000,
        "policy":{"connections":1, "idle_connections":1, "keepalive_ms":1000,
            "connect_ms":1000, "request_headers_ms":null, "write_ms":30, "pool_wait_ms":1000,
            "stream_idle_ms":30, "drain_timeout_ms":100,
            "drain_bytes":65536, "drain_chunks":16}
    })
}

fn start() -> Process {
    start_with_limit(65536)
}

fn start_with_limit(limit: usize) -> Process {
    Process(
        Command::new(env!("CARGO_BIN_EXE_tofu-native"))
            .args(["run-http-agent", &limit.to_string()])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    )
}

fn request_headers(listener: &TcpListener) -> (TcpStream, usize) {
    listener.set_nonblocking(true).unwrap();
    let expires = Instant::now() + Duration::from_secs(5);
    let mut stream = loop {
        match listener.accept() {
            Ok((stream, _)) => break stream,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(
                    Instant::now() < expires,
                    "native request never reached its peer"
                );
                std::thread::sleep(Duration::from_millis(1));
            }
            Err(error) => panic!("accept: {error}"),
        }
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut headers = Vec::new();
    let mut byte = [0];
    while !headers.ends_with(b"\r\n\r\n") {
        stream.read_exact(&mut byte).unwrap();
        headers.push(byte[0]);
        assert!(headers.len() < 8192);
    }
    let text = String::from_utf8(headers).unwrap();
    let length = text
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length:")
                .map(|v| v.trim().parse::<usize>().unwrap())
        })
        .unwrap();
    (stream, length)
}

fn read_request(listener: &TcpListener) -> TcpStream {
    let (mut stream, length) = request_headers(listener);
    assert!(length < 65536);
    let mut body = vec![0; length];
    stream.read_exact(&mut body).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&body).unwrap()["model"],
        "fixture"
    );
    stream
}

#[test]
fn unbounded_headers_keep_owner_stop_deadline_and_successful_late_response() {
    for mode in ["stop", "deadline", "response"] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut config = configuration(format!("http://{}/chat", listener.local_addr().unwrap()));
        if mode == "deadline" {
            config["deadline_ms"] = json!(1000);
        }
        let (arrived, arrival) = std::sync::mpsc::channel();
        std::thread::scope(|scope| {
            let server = scope.spawn(move || {
                let mut stream = read_request(&listener);
                arrived.send(()).unwrap();
                if mode == "response" {
                    std::thread::sleep(Duration::from_millis(150));
                    let body = "data: {\"choices\":[{\"delta\":{\"content\":\"late headers\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n";
                    write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{body}", body.len()).unwrap();
                    stream.flush().unwrap();
                }
                let mut byte = [0];
                assert_eq!(stream.read(&mut byte).unwrap(), 0, "native scope did not release its socket");
            });
            let mut process = start();
            let mut input = process.0.stdin.take().unwrap();
            writeln!(input, "{config}").unwrap();
            input.flush().unwrap();
            arrival.recv_timeout(Duration::from_secs(5)).unwrap();
            if mode == "stop" {
                writeln!(
                    input,
                    "{}",
                    json!({"version":"tofu.native-agent-stdio/v1","type":"cancel"})
                )
                .unwrap();
                input.flush().unwrap();
            }
            let mut output = BufReader::new(process.0.stdout.take().unwrap());
            let mut line = String::new();
            output.read_line(&mut line).unwrap();
            let event: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(
                event["status"],
                if mode == "response" {
                    "done"
                } else {
                    "aborted"
                }
            );
            assert!(process.0.wait().unwrap().success());
            line.clear();
            assert_eq!(output.read_line(&mut line).unwrap(), 0);
            server.join().unwrap();
        });
    }
}

#[test]
fn absent_or_invalid_header_budget_refuses_before_network_dispatch() {
    for value in [
        None,
        Some(json!(0)),
        Some(json!(-1)),
        Some(json!(true)),
        Some(json!("1000")),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut config = configuration(format!("http://{}/chat", listener.local_addr().unwrap()));
        match value {
            Some(value) => {
                config["policy"]["request_headers_ms"] = value;
            }
            None => {
                config["policy"]
                    .as_object_mut()
                    .unwrap()
                    .remove("request_headers_ms");
            }
        }
        let mut process = start();
        writeln!(process.0.stdin.take().unwrap(), "{config}").unwrap();
        assert!(!process.0.wait().unwrap().success());
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
}

#[test]
fn invalid_write_budget_refuses_before_network_dispatch() {
    for value in [json!(0), json!(-1), json!(true), json!("1000"), json!(1.5)] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut config = configuration(format!("http://{}/chat", listener.local_addr().unwrap()));
        config["policy"]["write_ms"] = value;
        let mut process = start();
        writeln!(process.0.stdin.take().unwrap(), "{config}").unwrap();
        assert!(!process.0.wait().unwrap().success());
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
}

#[test]
fn stalled_upload_retains_typed_terminal_phase_and_closes_the_physical_peer() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut config = configuration(format!("http://{}/chat", listener.local_addr().unwrap()));
    config["policy"]["write_ms"] = json!(80);
    config["messages"][0]["content"] = json!("x".repeat(32 << 20));
    config["request_bytes"] = json!(64 << 20);
    config["retained_bytes"] = json!(64 << 20);
    let (release, released) = std::sync::mpsc::channel();
    std::thread::scope(|scope| {
        let server = scope.spawn(move || {
            let (mut peer, length) = request_headers(&listener);
            assert!(length > 32 << 20 && length < 64 << 20);
            released.recv_timeout(Duration::from_secs(5)).unwrap();
            let mut received = 0;
            let mut buffer = [0; 65536];
            loop {
                match peer.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) => received += count,
                    Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => break,
                    Err(error) => panic!("upload peer: {error}"),
                }
            }
            assert!(received < length);
            length
        });
        let mut process = start_with_limit(64 << 20);
        let mut input = process.0.stdin.take().unwrap();
        writeln!(input, "{config}").unwrap();
        input.flush().unwrap();
        let mut output = BufReader::new(process.0.stdout.take().unwrap());
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        release.send(()).unwrap();
        let length = server.join().unwrap();
        let event: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(event["status"], "error", "{event}");
        assert_eq!(event["error"]["kind"], "provider_transport");
        assert_eq!(event["error"]["failureStage"], "upload");
        assert_eq!(event["error"]["transportReason"], "timeout");
        assert_eq!(event["error"]["transportCause"], "WriteTimeout");
        assert_eq!(event["error"]["requestBytes"], length);
        assert_eq!(event["error"]["timeoutSeconds"], 0.08);
        assert!(!line.contains("http://") && line.len() < 65536);
        assert!(!process.0.wait().unwrap().success());
        line.clear();
        assert_eq!(output.read_line(&mut line).unwrap(), 0);
    });
}
