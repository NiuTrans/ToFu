//! Real CLI callback/terminal waits preserve owner Stop, deadlines and EOF.
#![cfg(target_os = "linux")]
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

struct Process {
    child: Child,
    lines: Receiver<Result<Option<Value>, String>>,
    reader: Option<JoinHandle<()>>,
}
impl Process {
    fn start(config: &Value) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_tofu-native"))
            .args(["run-http-agent", "65536"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let output = child.stdout.take().unwrap();
        let (send, lines) = mpsc::channel();
        let reader = thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let frame = line.map_err(|error| error.to_string()).and_then(|line| {
                    serde_json::from_str(&line).map_err(|error| error.to_string())
                });
                if send.send(frame.map(Some)).is_err() {
                    return;
                }
            }
            let _ = send.send(Ok(None));
        });
        let mut process = Self {
            child,
            lines,
            reader: Some(reader),
        };
        process.send(config);
        process
    }
    fn send(&mut self, frame: &Value) {
        let input = self.child.stdin.as_mut().unwrap();
        writeln!(input, "{frame}").unwrap();
        input.flush().unwrap();
    }
    fn next(&self) -> Option<Value> {
        self.lines
            .recv_timeout(Duration::from_secs(5))
            .expect("native frame wait exceeded test bound")
            .unwrap()
    }
    fn finish(&mut self, success: bool) {
        assert!(
            self.next().is_none(),
            "unexpected frame after terminal outcome"
        );
        self.child.stdin.take();
        let until = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert_eq!(status.success(), success);
                break;
            }
            assert!(Instant::now() < until, "native child did not exit");
            thread::sleep(Duration::from_millis(2));
        }
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}
struct Peer(Option<JoinHandle<()>>);
impl Drop for Peer {
    fn drop(&mut self) {
        if let Some(peer) = self.0.take() {
            let _ = peer.join();
        }
    }
}
fn configuration(listener: &TcpListener) -> Value {
    json!({"version":"tofu.native-agent-stdio/v1","type":"configure","owner_user_id":7,
        "task_id":"ack-task","epoch":"ack-epoch","endpoint":format!("http://{}/chat",listener.local_addr().unwrap()),
        "headers":{},"template":{"model":"fixture","max_tokens":10},
        "messages":[{"role":"user","content":"hello"}],"tools":{},
        "event_bytes":65536,"output_bytes":65536,"request_bytes":65536,"retained_bytes":65536,
        "feedback_bytes":65536,"tool_frame_bytes":65536,"tool_calls":8,"rounds":3,"deadline_ms":null,
        "policy":{"connections":1,"idle_connections":1,"keepalive_ms":1000,"connect_ms":1000,
            "request_headers_ms":null,"write_ms":1000,"pool_wait_ms":1000,"stream_idle_ms":1000,
            "drain_timeout_ms":100,"drain_bytes":65536,"drain_chunks":16}})
}
fn listener() -> TcpListener {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    listener
}
fn ack(request: &Value, accepted: bool) -> Value {
    json!({"version":"tofu.native-agent-stdio/v1","type":"terminal_ack",
        "request_id":request["request_id"],"attempt":request["attempt"],
        "accepted":accepted,"retry":!accepted})
}
fn cancel() -> Value {
    json!({"version":"tofu.native-agent-stdio/v1","type":"cancel"})
}

#[test]
fn null_terminal_ack_waits_for_frozen_retry_despite_late_cancel() {
    let listener = listener();
    let mut config = configuration(&listener);
    config["cancel_requested"] = json!(true);
    config["terminal_persistence"] =
        json!({"operation_id":"commit","ack_ms":null,"attempts":2,"frame_bytes":65536});
    let mut process = Process::start(&config);
    let first = process.next().unwrap();
    assert_eq!(first["type"], "terminal_request");
    assert_eq!(first["event"]["status"], "aborted");
    thread::sleep(Duration::from_millis(60));
    process.send(&cancel());
    assert!(process.child.try_wait().unwrap().is_none());
    process.send(&ack(&first, false));
    let second = process.next().unwrap();
    assert_eq!(second["attempt"], 2);
    assert_eq!(second["event"], first["event"]);
    assert_eq!(second["receipt"], first["receipt"]);
    thread::sleep(Duration::from_millis(60));
    process.send(&ack(&second, true));
    assert_eq!(process.next().unwrap(), first["event"]);
    process.finish(true);
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#[test]
fn null_terminal_ack_eof_and_finite_expiry_never_publish_success() {
    for finite in [false, true] {
        let listener = listener();
        let mut config = configuration(&listener);
        config["cancel_requested"] = json!(true);
        config["terminal_persistence"] = json!({"operation_id":"commit","ack_ms":if finite {json!(30)} else {Value::Null},"attempts":2,"frame_bytes":65536});
        let mut process = Process::start(&config);
        assert_eq!(process.next().unwrap()["type"], "terminal_request");
        if !finite {
            process.child.stdin.take();
        }
        process.finish(false);
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
}

fn counted_configuration(listener: &TcpListener) -> Value {
    let mut config = configuration(listener);
    config["model_control"] = json!({"ack_ms":null});
    config["model_counter"] = json!({"encoding":"cl100k_base","output_field":"max_tokens",
        "input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"worker_stack_bytes":1048576});
    config
}

#[test]
fn null_model_ack_stop_deadline_and_eof_retire_before_http() {
    for mode in ["stop", "deadline", "eof"] {
        let listener = listener();
        let mut config = counted_configuration(&listener);
        if mode == "deadline" {
            config["deadline_ms"] = json!(2000);
        }
        let mut process = Process::start(&config);
        let request = process.next().unwrap();
        assert_eq!(request["type"], "model_request");
        assert_eq!(request["action"], "reserve");
        assert_eq!(
            request["expires_monotonic_ns"].is_null(),
            mode != "deadline"
        );
        if mode == "stop" {
            process.send(&cancel());
        }
        if mode == "eof" {
            process.child.stdin.take();
        }
        let terminal = process.next().unwrap();
        assert_eq!(terminal["type"], terminal["status"], "{terminal}");
        assert_eq!(
            terminal["status"],
            if mode == "eof" { "error" } else { "aborted" },
            "{mode}: {terminal}"
        );
        if mode == "eof" {
            assert_eq!(terminal["error"]["kind"], "client_tool_protocol");
        }
        process.finish(mode != "eof");
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
}

#[test]
fn null_model_ack_all_phases_accept_delayed_owned_reply_once() {
    delayed_model_replies(false);
}

#[test]
fn null_model_settlement_joins_authority_after_stop() {
    delayed_model_replies(true);
}

fn delayed_model_replies(stop_during_settlement: bool) {
    let listener = listener();
    let config = counted_configuration(&listener);
    let server = listener.try_clone().unwrap();
    let mut peer = Peer(Some(thread::spawn(move || {
        let until = Instant::now() + Duration::from_secs(5);
        let mut stream = loop {
            match server.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now() < until);
                    thread::sleep(Duration::from_millis(2));
                }
                Err(error) => panic!("{error}"),
            }
        };
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut headers = Vec::new();
        let mut byte = [0];
        while !headers.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            headers.push(byte[0]);
            assert!(headers.len() < 8192);
        }
        let headers = String::from_utf8(headers).unwrap();
        let length = headers
            .lines()
            .find_map(|line| {
                line.to_ascii_lowercase()
                    .strip_prefix("content-length:")
                    .map(|value| value.trim().parse::<usize>().unwrap())
            })
            .unwrap();
        assert!(length < 65536);
        let mut body = vec![0; length];
        stream.read_exact(&mut body).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&body).unwrap()["max_tokens"],
            10
        );
        let body = "data: {\"choices\":[{\"delta\":{\"content\":\"done\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n\ndata: [DONE]\n\n";
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{body}",body.len()).unwrap();
        stream.flush().unwrap();
    })));
    let mut process = Process::start(&config);
    for action in ["reserve", "dispatch", "settle"] {
        let request = process.next().unwrap();
        assert_eq!(request["type"], "model_request");
        assert_eq!(request["action"], action);
        assert!(request["expires_monotonic_ns"].is_null());
        if stop_during_settlement && action == "settle" {
            process.send(&cancel());
        }
        thread::sleep(Duration::from_millis(60));
        assert!(process.child.try_wait().unwrap().is_none());
        let mut reply = json!({"version":config["version"],"type":"model_decision","request_id":request["request_id"],"accepted":true});
        if action == "reserve" {
            reply["output_tokens"] = json!(10);
        }
        if action == "settle" {
            assert_eq!(request["usage"]["prompt_tokens"], 3, "{request}");
        }
        process.send(&reply);
    }
    let terminal = process.next().unwrap();
    assert_eq!(terminal["type"], terminal["status"], "{terminal}");
    assert_eq!(
        terminal["status"],
        if stop_during_settlement {
            "aborted"
        } else {
            "done"
        },
        "{terminal}"
    );
    process.finish(true);
    peer.0.take().unwrap().join().unwrap();
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
