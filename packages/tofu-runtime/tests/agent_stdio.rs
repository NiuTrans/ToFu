//! Real subprocess stdio, real TCP provider rounds and bounded client-tool gates.
#![cfg(target_os = "linux")]
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn config(endpoint: String) -> Value {
    json!({"version":"tofu.native-agent-stdio/v1","type":"configure","owner_user_id":7,"task_id":"stdio-task","epoch":"epoch","endpoint":endpoint,"headers":{},"template":{"model":"fixture"},"messages":[{"role":"user","content":"hello"}],"tools":{"lookup":{"name":"lookup","permission":"read","contractVersion":"tofu.tool-contract/v2","arguments_schema":{"type":"object"}}},"event_bytes":65536,"output_bytes":65536,"tool_calls":8,"request_bytes":65536,"rounds":3,"retained_bytes":65536,"feedback_bytes":4096,"tool_frame_bytes":8192,"deadline_ms":2000,"policy":{"connections":1,"idle_connections":1,"keepalive_ms":1000,"connect_ms":1000,"request_headers_ms":2000,"pool_wait_ms":1000,"stream_idle_ms":1000,"drain_timeout_ms":100,"drain_bytes":65536,"drain_chunks":16}})
}
fn request(stream: &mut TcpStream) -> Value { request_with_headers(stream).0 }
fn request_with_headers(stream: &mut TcpStream) -> (Value, String) {
    let mut head = vec![];
    let mut byte = [0];
    while !head.ends_with(b"\r\n\r\n") {
        stream.read_exact(&mut byte).unwrap();
        head.push(byte[0]);
        assert!(head.len() < 65536)
    }
    let text = String::from_utf8(head).unwrap();
    let len = text
        .lines()
        .find_map(|s| {
            s.to_lowercase()
                .strip_prefix("content-length:")
                .map(|s| s.trim().parse::<usize>().unwrap())
        })
        .unwrap();
    let mut body = vec![0; len];
    stream.read_exact(&mut body).unwrap();
    (serde_json::from_slice(&body).unwrap(), text)
}
fn response(stream: &mut TcpStream, delta: Value, finish: &str) {
    let wire = format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({"choices":[{"delta":delta,"finish_reason":finish}]})
    );
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
        wire.len(),
        wire
    )
    .unwrap();
    stream.flush().unwrap();
}
#[test]
fn actual_stdio_agent_tools_continue_and_protocol_failure_never_restarts_provider() {
    for mode in [
        "ok",
        "empty",
        "ok_eof",
        "duplicate",
        "wrong_id",
        "wrong_version",
        "cancel",
        "deadline",
        "eof",
        "oversize",
    ] {
        let successful = matches!(mode, "ok" | "empty" | "ok_eof");
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}/chat", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(vec![]));
        let observed = seen.clone();
        let worker = std::thread::spawn(move || {
            let until = Instant::now() + Duration::from_secs(3);
            let mut stream = loop {
                match listener.accept() {
                    Ok((s, _)) => break s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < until);
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            observed.lock().unwrap().push(request(&mut stream));
            response(
                &mut stream,
                json!({"tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"lookup","arguments":"{}"}}]}),
                "tool_calls",
            );
            if successful {
                let next = request(&mut stream);
                assert_eq!(
                    next["messages"].as_array().unwrap().last().unwrap()["content"],
                    if mode == "empty" {
                        "[empty tool result]"
                    } else {
                        "client evidence"
                    }
                );
                observed.lock().unwrap().push(next);
                response(&mut stream, json!({"content":"native done"}), "stop");
            }
        });
        let mut c = config(endpoint);
        if mode == "deadline" {
            c["deadline_ms"] = json!(150)
        }
        let mut process = Process(
            Command::new(env!("CARGO_BIN_EXE_tofu-native"))
                .args(["run-http-agent", "65536"])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let mut stdin = process.0.stdin.take().unwrap();
        writeln!(stdin, "{c}").unwrap();
        stdin.flush().unwrap();
        let mut stdout = BufReader::new(process.0.stdout.take().unwrap());
        let mut line = String::new();
        stdout.read_line(&mut line).unwrap();
        let request: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(request["type"], "tool_request");
        assert_eq!(request["model_round"], 0);
        assert_eq!(request["owner_user_id"], 7);
        assert_eq!(request["task_id"], "stdio-task");
        if mode == "eof" {
            drop(stdin)
        } else if mode == "oversize" {
            writeln!(stdin, "{}", "x".repeat(8193)).unwrap();
            stdin.flush().unwrap();
        } else if mode != "deadline" {
            let reply = if mode == "cancel" {
                json!({"version":"tofu.native-agent-stdio/v1","type":"cancel"})
            } else {
                json!({"version":if mode=="wrong_version"{"other"}else{"tofu.native-agent-stdio/v1"},"type":"tool_result","request_id":if mode=="wrong_id"{json!("foreign")}else{request["request_id"].clone()},"content":if mode=="empty" {""} else {"client evidence"}})
            };
            if mode == "duplicate" {
                stdin
                    .write_all(format!("{reply}\n{reply}\n").as_bytes())
                    .unwrap();
            } else {
                writeln!(stdin, "{reply}").unwrap();
            }
            stdin.flush().unwrap();
            if mode == "ok_eof" {
                drop(stdin);
            }
        }
        line.clear();
        stdout.read_line(&mut line).unwrap();
        let terminal: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(
            terminal["status"],
            if successful {
                "done"
            } else if mode == "cancel" || mode == "deadline" {
                "aborted"
            } else {
                "error"
            },
            "{mode}: {terminal}"
        );
        if mode == "duplicate" {
            assert_eq!(terminal["error"]["kind"], "client_tool_protocol");
        }
        let status = process.0.wait().unwrap();
        assert_eq!(
            status.success(),
            successful || mode == "cancel" || mode == "deadline"
        );
        worker.join().unwrap();
        assert_eq!(seen.lock().unwrap().len(), if successful { 2 } else { 1 });
        line.clear();
        assert_eq!(stdout.read_line(&mut line).unwrap(), 0);
    }
}
#[test]
fn write_catalog_refuses_before_any_network_or_tool_effect() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
    c["tools"]["lookup"]["permission"] = json!("write");
    let mut process = Process(
        Command::new(env!("CARGO_BIN_EXE_tofu-native"))
            .args(["run-http-agent", "65536"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    writeln!(process.0.stdin.as_mut().unwrap(), "{c}").unwrap();
    let out = process.0.stdout.take().unwrap();
    let mut text = String::new();
    BufReader::new(out).read_to_string(&mut text).unwrap();
    assert!(!process.0.wait().unwrap().success());
    assert!(text.is_empty());
    assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
}

#[test]
fn protocol_schemas_enforce_version_fields_and_allow_empty_text() {
    use tofu_runtime::tool_contract::Catalog;
    let contract: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_agent_stdio_v1.schema.json"
    ))
    .unwrap();
    let schemas:serde_json::Map<String,Value>=contract["$defs"].as_object().unwrap().iter().map(|(n,s)|(n.clone(),json!({"name":n,"contractVersion":tofu_runtime::execution_contract::TOOL_CONTRACT_VERSION,"arguments_schema":s}))).collect();
    let catalog = Catalog::compile(7, "protocol", &Value::Object(schemas)).unwrap();
    let valid =
        json!({"version":contract["$id"],"type":"tool_result","request_id":"one","content":""});
    assert!(catalog.validate(7, "tool_result", valid.clone()).is_ok());
    let mut configuration = config("http://127.0.0.1:1/chat".to_owned());
    for preserve in [json!(true), json!(false)] {
        configuration["chat_preserve_text_parts"] = preserve;
        assert!(catalog.validate_protocol(7,"configure",configuration.clone()).is_ok());
    }
    for invalid in [json!("false"), json!(0), Value::Null] {
        configuration["chat_preserve_text_parts"] = invalid;
        assert!(catalog.validate_protocol(7,"configure",configuration.clone()).is_err());
    }
    for bad in [
        json!({"version":"wrong"}),
        json!({"type":"cancel"}),
        json!({"extra":"injected"}),
        json!({"content":false}),
    ] {
        let mut value = valid.clone();
        for (k, v) in bad.as_object().unwrap() {
            value[k] = v.clone();
        }
        assert!(catalog.validate(7, "tool_result", value).is_err());
    }
}

#[test]
fn silent_http_is_aborted_by_cli_deadline_without_a_tool_request() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
    c["deadline_ms"] = json!(100);
    let worker = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let _ = request(&mut stream);
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 10000\r\n\r\n"
        )
        .unwrap();
        stream.flush().unwrap();
        std::thread::sleep(Duration::from_millis(250));
    });
    let mut process = Process(
        Command::new(env!("CARGO_BIN_EXE_tofu-native"))
            .args(["run-http-agent", "65536"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    writeln!(process.0.stdin.as_mut().unwrap(), "{c}").unwrap();
    let mut reader = BufReader::new(process.0.stdout.take().unwrap());
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    let event: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(event["status"], "aborted");
    assert!(process.0.wait().unwrap().success());
    line.clear();
    assert_eq!(reader.read_line(&mut line).unwrap(), 0);
    worker.join().unwrap();
}

#[test]
fn expired_absolute_deadline_aborts_without_model_or_tool_effects() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
    c["deadline_unix_ms"] = json!(0);
    let mut process = Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent", "65536"]).stdin(Stdio::piped())
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(process.0.stdin.as_mut().unwrap(), "{c}").unwrap();
    let mut reader = BufReader::new(process.0.stdout.take().unwrap());
    let mut line = String::new();
    assert!(reader.read_line(&mut line).unwrap() > 0);
    let terminal: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(terminal["status"], "aborted", "{terminal}");
    assert_eq!(listener.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
    assert!(process.0.wait().unwrap().success());
}

#[test]
fn absolute_deadline_interrupts_silent_provider_before_relative_budget() {
    std::thread::scope(|scope| {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
    c["deadline_unix_ms"] = json!(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64 + 300);
    let worker = scope.spawn(move || {
        let until = Instant::now() + Duration::from_secs(2);
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < until => std::thread::sleep(Duration::from_millis(1)),
                Err(error) => panic!("{error}"),
            }
        };
        stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        request(&mut stream);
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 10000\r\n\r\n").unwrap();
        stream.flush().unwrap();
        std::thread::sleep(Duration::from_millis(500));
    });
    let began = Instant::now();
    let mut process = Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent", "65536"]).stdin(Stdio::piped())
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(process.0.stdin.as_mut().unwrap(), "{c}").unwrap();
    let mut reader = BufReader::new(process.0.stdout.take().unwrap());
    let mut line = String::new();
    assert!(reader.read_line(&mut line).unwrap() > 0);
    let terminal: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(terminal["status"], "aborted", "{terminal}");
    assert!(began.elapsed() < Duration::from_millis(1500));
    assert!(process.0.wait().unwrap().success());
    worker.join().unwrap();
    });
}

#[test]
fn control_input_cancels_silent_provider_and_rejects_unissued_replies() {
    use std::os::fd::{AsRawFd, FromRawFd};
    for mode in ["cancel", "invalid", "unissued", "oversize"] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
        let (ready, started) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let _ = request(&mut stream);
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 10000\r\n\r\n").unwrap();
            stream.flush().unwrap();
            ready.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(250));
        });
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        let input = unsafe { std::fs::File::from_raw_fd(fds[0]) };
        let mut writer = unsafe { std::fs::File::from_raw_fd(fds[1]) };
        let flags = unsafe { libc::fcntl(input.as_raw_fd(), libc::F_GETFL) };
        let mut process = Process(
            Command::new(env!("CARGO_BIN_EXE_tofu-native"))
                .args(["run-http-agent", "65536"])
                .stdin(Stdio::from(input.try_clone().unwrap()))
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        writeln!(writer, "{c}").unwrap();
        writer.flush().unwrap();
        started.recv_timeout(Duration::from_secs(2)).unwrap();
        let at = Instant::now();
        match mode {
            "cancel"=>writeln!(writer,"{}",json!({"version":"tofu.native-agent-stdio/v1","type":"cancel"})).unwrap(),
            "unissued"=>writeln!(writer,"{}",json!({"version":"tofu.native-agent-stdio/v1","type":"tool_result","request_id":"stdio-task-0","content":"unsolicited"})).unwrap(),
            "oversize"=>writeln!(writer,"{}","x".repeat(8193)).unwrap(),
            _=>writeln!(writer,"invalid JSON").unwrap(),
        };
        writer.flush().unwrap();
        let mut out = BufReader::new(process.0.stdout.take().unwrap());
        let mut line = String::new();
        out.read_line(&mut line).unwrap();
        let event: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(
            event["status"],
            if mode == "cancel" { "aborted" } else { "error" }
        );
        assert!(
            at.elapsed() < Duration::from_millis(750),
            "{mode} waited for provider deadline"
        );
        assert_eq!(
            unsafe { libc::fcntl(input.as_raw_fd(), libc::F_GETFL) },
            flags,
            "reader flags must restore before terminal publication"
        );
        assert_eq!(process.0.wait().unwrap().success(), mode == "cancel");
        line.clear();
        assert_eq!(out.read_line(&mut line).unwrap(), 0);
        worker.join().unwrap();
    }
}

#[test]
fn round_authority_fences_network_and_settlement_fences_followup() {
    for mode in ["denied", "ok", "settle_denied", "wrong_kind", "cancel"] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
        c["round_control"] = json!(true);
        c["tools"] = json!({});
        let mut process = Process(
            Command::new(env!("CARGO_BIN_EXE_tofu-native"))
                .args(["run-http-agent", "65536"])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let mut input = process.0.stdin.take().unwrap();
        writeln!(input, "{c}").unwrap();
        let mut output = BufReader::new(process.0.stdout.take().unwrap());
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        let reserve: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(reserve["type"], "round_request");
        assert_eq!(reserve["round"], 0);
        assert_eq!(reserve["history_messages"], 1);
        assert!(matches!(listener.accept(), Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
        let reply = if mode == "cancel" {
            json!({"version":c["version"],"type":"cancel"})
        } else if mode == "wrong_kind" {
            json!({"version":c["version"],"type":"tool_result","request_id":reserve["request_id"],"content":"bad"})
        } else {
            json!({"version":c["version"],"type":"round_decision","request_id":reserve["request_id"],"approved":mode!="denied"})
        };
        writeln!(input, "{reply}").unwrap();
        input.flush().unwrap();
        let mut stream = if matches!(mode, "ok" | "settle_denied") {
            let until = Instant::now() + Duration::from_secs(2);
            let mut stream = loop {
                match listener.accept() {
                    Ok((s, _)) => break s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < until);
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            request(&mut stream);
            response(&mut stream, json!({"content":"bounded"}), "stop");
            Some(stream)
        } else {
            None
        };
        line.clear();
        output.read_line(&mut line).unwrap();
        let mut terminal: Value = serde_json::from_str(&line).unwrap();
        if stream.is_some() {
            assert_eq!(terminal["type"], "round_settled");
            assert_eq!(terminal["evidence"]["kind"], "returned");
            assert_eq!(terminal["evidence"]["finish_reason"], "stop");
            assert_eq!(terminal["evidence"]["usage"], json!({}));
            assert_eq!(
                terminal["evidence"]["usage_observation"],
                json!({"tokens":0,"complete":false,"error":null})
            );
            writeln!(input,"{}",json!({"version":c["version"],"type":"round_ack","request_id":terminal["request_id"],"accepted":mode=="ok"})).unwrap();
            input.flush().unwrap();
            line.clear();
            output.read_line(&mut line).unwrap();
            terminal = serde_json::from_str(&line).unwrap();
        }
        assert_eq!(
            terminal["status"],
            if mode == "ok" {
                "done"
            } else if mode == "cancel" {
                "aborted"
            } else {
                "error"
            },
            "{mode}: {terminal}"
        );
        assert_eq!(
            process.0.wait().unwrap().success(),
            matches!(mode, "ok" | "cancel")
        );
        assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
        if let Some(s) = stream.as_mut() {
            let mut bytes = [0; 1];
            assert_eq!(s.read(&mut bytes).unwrap(), 0);
        }
        line.clear();
        assert_eq!(output.read_line(&mut line).unwrap(), 0);
    }
}

#[test]
fn selected_local_tokenizer_caps_real_http_and_refuses_unknown_or_exhausted_followup() {
    use tofu_runtime::token_ingress::{Bounds, Counter, Encoder, Encoding};
    for known in [true, false] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
        c["template"]["max_tokens"] = json!(100);
        let counter = Counter::new(
            7,
            "stdio-task".into(),
            "fixture".into(),
            Encoder::new(Encoding::Cl100k),
            Bounds {
                input_bytes: 65536,
                text_bytes: 65536,
                tool_json_bytes: 65536,
                depth: 128,
            },
        )
        .unwrap();
        let mut counted_template = c["template"].clone();
        counted_template["tools"] = json!([{"type":"function","function":{"name":"lookup","description":"","parameters":c["tools"]["lookup"]["arguments_schema"]}}]);
        let input = counter
            .count(
                7,
                "stdio-task",
                c["messages"].as_array().unwrap(),
                counted_template.as_object().unwrap(),
            )
            .unwrap();
        c["token_admission"] = json!({"encoding":"cl100k_base","output_field":"max_tokens","total_tokens":input+10,"input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"actor_records":1,"attempt_records":1,"identity_bytes":128,"worker_stack_bytes":1048576});
        let server = listener.try_clone().unwrap();
        let worker = std::thread::spawn(move || {
            let until = Instant::now() + Duration::from_secs(3);
            let mut stream = loop {
                match server.accept() {
                    Ok((s, _)) => break s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < until);
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let body = request(&mut stream);
            assert_eq!(body["max_tokens"], 10);
            let wire = format!(
                "data: {}\n\ndata: [DONE]\n\n",
                json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"lookup","arguments":"{}"}}]},"finish_reason":"tool_calls"}],"usage":if known{json!({"prompt_tokens":input,"completion_tokens":10})}else{json!({})}})
            );
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{wire}",wire.len()).unwrap();
            stream.flush().unwrap();
        });
        let mut p = Process(
            Command::new(env!("CARGO_BIN_EXE_tofu-native"))
                .args(["run-http-agent", "65536"])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        writeln!(p.0.stdin.as_mut().unwrap(), "{c}").unwrap();
        let mut out = BufReader::new(p.0.stdout.take().unwrap());
        let mut line = String::new();
        out.read_line(&mut line).unwrap();
        let call: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(call["type"], "tool_request");
        assert_eq!(call["model_round"], 0);
        writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"tool_result","request_id":call["request_id"],"content":"evidence"})).unwrap();
        line.clear();
        out.read_line(&mut line).unwrap();
        let terminal: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(terminal["status"], "error", "{terminal}");
        assert_eq!(
            terminal["error"]["kind"],
            if known {
                "execution_token_budget_exhausted"
            } else {
                "execution_token_usage_unknown"
            }
        );
        assert!(!p.0.wait().unwrap().success());
        worker.join().unwrap();
        assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
    }
}

#[test]
fn control_cancel_during_cold_token_ingress_is_joined_before_any_http() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
    c["template"]["max_tokens"] = json!(100);
    c["token_admission"] = json!({"encoding":"o200k_base","output_field":"max_tokens","total_tokens":10000,"input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"actor_records":1,"attempt_records":1,"identity_bytes":128,"worker_stack_bytes":1048576});
    let mut p = Process(
        Command::new(env!("CARGO_BIN_EXE_tofu-native"))
            .args(["run-http-agent", "65536"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let input = p.0.stdin.as_mut().unwrap();
    writeln!(input, "{c}").unwrap();
    writeln!(input, "{}", json!({"version":c["version"],"type":"cancel"})).unwrap();
    input.flush().unwrap();
    let mut text = String::new();
    BufReader::new(p.0.stdout.take().unwrap())
        .read_line(&mut text)
        .unwrap();
    let terminal: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(terminal["status"], "aborted", "{terminal}");
    assert!(p.0.wait().unwrap().success());
    assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
}

#[test]
fn terminal_commit_ack_precedes_visibility_and_retry_never_reexecutes_model() {
    for mode in ["ok", "retry", "retry_reference", "reference", "reject", "wrong_id", "timeout", "large"] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
        c["tools"] = json!({});
        c["terminal_persistence"] = json!({"operation_id":format!("commit-{mode}"),"ack_ms":if mode=="timeout"{100}else{1500},"attempts":2,"frame_bytes":65536});
        c["terminal_persistence"]["usage_summary"] = json!(mode == "retry");
        if mode=="reference" {c["terminal_persistence"]["completion_reference"]=json!(true);}
        if mode=="retry_reference" {c["terminal_persistence"]["retry_reference"]=json!(true);}
        let server = listener.try_clone().unwrap();
        let worker = std::thread::spawn(move || {
            let until = Instant::now() + Duration::from_secs(3);
            let mut stream = loop {
                match server.accept() {
                    Ok((s, _)) => break s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < until);
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => panic!("{e}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            request(&mut stream);
            response(
                &mut stream,
                json!({"content":if mode=="large"{"x".repeat(10000)}else{"committed result".into()}}),
                "stop",
            );
        });
        let mut p = Process(
            Command::new(env!("CARGO_BIN_EXE_tofu-native"))
                .args(["run-http-agent", "65536"])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        writeln!(p.0.stdin.as_mut().unwrap(), "{c}").unwrap();
        let mut output = BufReader::new(p.0.stdout.take().unwrap());
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        let first: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(first["type"], "terminal_request");
        assert_eq!(first["event"]["status"], "done");
        if mode == "retry" {
            assert_eq!(first["usage_summary"]["profile"], "tofu.additive-usage/u64-f64-v1");
            let counts = tofu_runtime::usage_aggregate::aggregate(first["event"]["result"]["usage"].as_array().unwrap()).unwrap();
            assert_eq!(first["usage_summary"]["counts"], Value::Object(counts));
        } else { assert!(first.get("usage_summary").is_none()); }
        assert_eq!(first["receipt"]["invariants_satisfied"], true);
        assert!(first["receipt"]["resource_count"].as_u64().unwrap() > 0);
        if mode != "timeout" {
            writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"terminal_ack","request_id":if mode=="wrong_id"{json!("foreign")}else{first["request_id"].clone()},"attempt":1,"accepted":matches!(mode,"ok"|"large"),"retry":matches!(mode,"retry"|"retry_reference"|"reference")})).unwrap();
            if matches!(mode,"retry"|"retry_reference"|"reference") {
                line.clear();
                output.read_line(&mut line).unwrap();
                let second: Value = serde_json::from_str(&line).unwrap();
                assert_eq!(second["attempt"], 2);
                if mode=="retry_reference" {
                    assert_eq!(second,json!({"version":c["version"],"type":"terminal_retry",
                        "request_id":first["request_id"],"owner_user_id":7,"task_id":"stdio-task",
                        "attempt":2,"status":"done"}));
                } else {
                    assert_eq!(second["type"],"terminal_request");
                    assert_eq!(second["event"],first["event"]);
                    assert_eq!(second["receipt"],first["receipt"]);
                    assert_eq!(second["usage_summary"],first["usage_summary"]);
                }
                writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"terminal_ack","request_id":second["request_id"],"attempt":2,"accepted":true,"retry":false})).unwrap();
            }
        }
        line.clear();
        let n = output.read_line(&mut line).unwrap();
        let successful = matches!(mode, "ok" | "retry" | "retry_reference" | "reference" | "large");
        if successful {
            let terminal: Value = serde_json::from_str(&line).unwrap();
            if mode=="reference" {
                assert_eq!(terminal,json!({"version":c["version"],"type":"terminal_committed",
                    "request_id":first["request_id"],"owner_user_id":7,"task_id":"stdio-task",
                    "attempt":2,"status":"done"}));
            } else {assert_eq!(terminal, first["event"]);}
        } else {
            assert_eq!(n, 0, "{mode}: {line}");
        }
        assert_eq!(p.0.wait().unwrap().success(), successful);
        worker.join().unwrap();
        assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
    }
}

#[test]
fn actual_preparation_handles_large_history_and_fences_invalid_or_cancelled_dispatch() {
    for mode in ["prepared", "large", "invalid", "cancel"] {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
        c["round_control"]=json!(true);c["round_preparation"]=json!(true);c["round_preparation_bytes"]=json!(65536);c["tools"]=json!({});
        if mode=="large" {c["messages"][0]["content"]=json!("x".repeat(16000));}
        if mode=="invalid" {c["retained_bytes"]=json!(128);}
        let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
        let mut input=process.0.stdin.take().unwrap();writeln!(input,"{c}").unwrap();input.flush().unwrap();
        let mut output=BufReader::new(process.0.stdout.take().unwrap());let mut line=String::new();output.read_line(&mut line).unwrap();
        let prepare:Value=serde_json::from_str(&line).unwrap();assert_eq!(prepare["type"],"round_request");assert_eq!(prepare["messages"],c["messages"]);
        if mode=="large" {assert!(line.len()>4096);}
        if mode=="cancel" {writeln!(input,"{}",json!({"version":c["version"],"type":"cancel"})).unwrap();}
        else {writeln!(input,"{}",json!({"version":c["version"],"type":"round_decision","request_id":prepare["request_id"],"approved":true,"messages":[{"role":"user","content":if mode=="invalid" {"x".repeat(256)}else{"prepared".into()}}]})).unwrap();}
        input.flush().unwrap();
        if matches!(mode,"prepared"|"large") {
            let until=Instant::now()+Duration::from_secs(2);
            let mut stream=loop {match listener.accept(){Ok((stream,_))=>break stream,Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>{assert!(Instant::now()<until);std::thread::sleep(Duration::from_millis(2));},Err(e)=>panic!("{e}")}};
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();let body=request(&mut stream);assert_eq!(body["messages"],json!([{"role":"user","content":"prepared"}]));response(&mut stream,json!({"content":"done"}),"stop");
        }
        line.clear();output.read_line(&mut line).unwrap();let mut event:Value=serde_json::from_str(&line).unwrap();
        if mode!="cancel" {
            assert_eq!(event["type"],"round_settled");assert_eq!(event["evidence"]["kind"],if mode=="invalid" {"not_dispatched"}else{"returned"});
            writeln!(input,"{}",json!({"version":c["version"],"type":"round_ack","request_id":event["request_id"],"accepted":true})).unwrap();input.flush().unwrap();
            line.clear();output.read_line(&mut line).unwrap();event=serde_json::from_str(&line).unwrap();
        }
        assert_eq!(event["status"],if mode=="invalid" {"error"}else if mode=="cancel" {"aborted"}else{"done"});
        assert_eq!(process.0.wait().unwrap().success(),mode!="invalid");
        if matches!(mode,"invalid"|"cancel") {assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));}
    }
}

#[test]
fn stop_during_partial_preparation_publication_preserves_frame_then_aborts() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
    let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
    c["round_control"]=json!(true);c["round_preparation"]=json!(true);c["round_preparation_bytes"]=json!(1048576);c["retained_bytes"]=json!(1048576);c["messages"][0]["content"]=json!("x".repeat(200000));c["tools"]=json!({});
    let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","1048576"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    let mut input=process.0.stdin.take().unwrap();writeln!(input,"{c}").unwrap();input.flush().unwrap();
    let mut output=BufReader::new(process.0.stdout.take().unwrap());let mut first=[0];output.read_exact(&mut first).unwrap();
    writeln!(input,"{}",json!({"version":c["version"],"type":"cancel"})).unwrap();input.flush().unwrap();
    let mut line=String::from_utf8(first.to_vec()).unwrap();output.read_line(&mut line).unwrap();
    let request:Value=serde_json::from_str(&line).unwrap();assert_eq!(request["type"],"round_request");assert_eq!(request["messages"],c["messages"]);
    line.clear();output.read_line(&mut line).unwrap();let terminal:Value=serde_json::from_str(&line).unwrap();assert_eq!(terminal["status"],"aborted");assert!(process.0.wait().unwrap().success());
    assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
}

#[test]
fn preparation_request_byte_refusal_never_exposes_history_or_opens_network() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
    let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
    c["round_control"]=json!(true);c["round_preparation"]=json!(true);c["round_preparation_bytes"]=json!(128);c["messages"][0]["content"]=json!(format!("private_marker{}","x".repeat(16000)));
    let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(process.0.stdin.as_mut().unwrap(),"{c}").unwrap();process.0.stdin.as_mut().unwrap().flush().unwrap();
    let mut output=BufReader::new(process.0.stdout.take().unwrap());let mut line=String::new();output.read_line(&mut line).unwrap();
    assert!(!line.contains("private_marker"));let terminal:Value=serde_json::from_str(&line).unwrap();assert_eq!(terminal["status"],"error");assert!(!process.0.wait().unwrap().success());
    assert!(matches!(listener.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
}

#[test]
fn late_tool_visibility_and_prepared_context_reach_each_physical_request() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move||{
        let(mut socket,_)=listener.accept().unwrap();socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let first=request(&mut socket);assert_eq!(first["tools"][0]["function"]["description"],"after context 0");
        assert_eq!(first["messages"].as_array().unwrap().last().unwrap()["content"],"hello\n\nprepared context 0");
        response(&mut socket,json!({"tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"lookup","arguments":"{}"}}]}),"tool_calls");
        let second=request(&mut socket);assert_eq!(second["tools"],json!([]));
        assert_eq!(second["messages"].as_array().unwrap().last().unwrap()["content"],"prepared context 1");
        response(&mut socket,json!({"content":"done"}),"stop");
    });
    let mut c=config(format!("http://{address}/chat"));c["round_control"]=json!(true);c["round_preparation"]=json!(true);
    c["round_preparation_bytes"]=json!(65536);c["wire_tool_preparation"]=json!(true);
    let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    let mut input=process.0.stdin.take().unwrap();writeln!(input,"{c}").unwrap();input.flush().unwrap();
    let mut output=BufReader::new(process.0.stdout.take().unwrap());let mut rounds=0;
    loop {
        let mut line=String::new();assert!(output.read_line(&mut line).unwrap()>0);let event:Value=serde_json::from_str(&line).unwrap();
        let reply=match event["type"].as_str().unwrap() {
            "round_request"=>{
                let round=event["round"].as_u64().unwrap();rounds+=1;
                let mut messages=event["messages"].as_array().unwrap().clone();messages.push(json!({"role":"user","content":format!("prepared context {round}")}));
                let tools=if round==0 {json!([{"type":"function","function":{"name":"lookup","description":"after context 0","parameters":{"type":"object"}}}])} else {json!([])};
                json!({"version":c["version"],"type":"round_decision","request_id":event["request_id"],"approved":true,"messages":messages,"wire_tools":tools})
            },
            "round_settled"=>json!({"version":c["version"],"type":"round_ack","request_id":event["request_id"],"accepted":true}),
            "tool_request"=>json!({"version":c["version"],"type":"tool_result","request_id":event["request_id"],"content":"evidence"}),
            "done"=>{assert_eq!(event["status"],"done");assert_eq!(rounds,2);break;},other=>panic!("{other}"),
        };
        writeln!(input,"{reply}").unwrap();input.flush().unwrap();
    }
    assert!(process.0.wait().unwrap().success());server.join().unwrap();
}

#[test]
fn preparation_inherits_one_fixed_deadline_across_model_rounds() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move || {
        let (mut socket,_)=listener.accept().unwrap();socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        request(&mut socket);response(&mut socket,json!({"tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"lookup","arguments":"{}"}}]}),"tool_calls");
        request(&mut socket);response(&mut socket,json!({"content":"done"}),"stop");
    });
    let mut c=config(format!("http://{address}/chat"));c["round_control"]=json!(true);c["round_preparation"]=json!(true);c["round_preparation_bytes"]=json!(65536);
    let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    let mut input=process.0.stdin.take().unwrap();writeln!(input,"{c}").unwrap();input.flush().unwrap();let mut output=BufReader::new(process.0.stdout.take().unwrap());let mut first_deadline=None;let mut rounds=0;
    loop {
        let mut line=String::new();assert!(output.read_line(&mut line).unwrap()>0);let event:Value=serde_json::from_str(&line).unwrap();
        let reply=match event["type"].as_str().unwrap() {
            "round_request"=>{
                let deadline=event["deadline_monotonic_ns"].as_u64().unwrap();assert!(deadline>0);
                if let Some(first)=first_deadline {assert_eq!(deadline,first);}else{first_deadline=Some(deadline);}
                rounds+=1;json!({"version":c["version"],"type":"round_decision","request_id":event["request_id"],"approved":true,"messages":event["messages"]})
            },
            "round_settled"=>json!({"version":c["version"],"type":"round_ack","request_id":event["request_id"],"accepted":true}),
            "tool_request"=>json!({"version":c["version"],"type":"tool_result","request_id":event["request_id"],"content":"evidence"}),
            "done"=>{assert_eq!(rounds,2);break;},other=>panic!("{other}"),
        };
        writeln!(input,"{reply}").unwrap();input.flush().unwrap();
    }
    assert!(process.0.wait().unwrap().success());server.join().unwrap();
}

#[test]
fn post_tool_progress_uses_one_slot_and_fences_later_http() {
    for mode in ["halt", "changed", "unknown", "verified", "fault", "cancel", "deadline"] {
        let expected_requests = match mode {"halt"=>2,"changed"|"unknown"|"verified"=>3,_=>1};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
        c["rounds"]=json!(4);c["round_control"]=json!(true);c["progress_control"]=json!(true);
        c["no_progress_rounds"]=json!(1);c["nonretryable_failure_rounds"]=json!(0);
        if mode=="deadline" {c["deadline_ms"]=json!(500);}
        let server=listener.try_clone().unwrap();
        let worker=std::thread::spawn(move || {
            let until=Instant::now()+Duration::from_secs(3);
            let mut stream=loop {match server.accept(){Ok((stream,_))=>break stream,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock && Instant::now()<until=>std::thread::sleep(Duration::from_millis(1)),Err(error)=>panic!("{error}")}};
            stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            for round in 0..expected_requests {
                request(&mut stream);
                if round==2 {response(&mut stream,json!({"role":"assistant","content":"done"}),"stop");}
                else {response(&mut stream,json!({"role":"assistant","tool_calls":[{"index":0,"id":format!("call-{round}"),"type":"function","function":{"name":"lookup","arguments":"{}"}}]}),"tool_calls");}
            }
        });
        let mut p=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
        writeln!(p.0.stdin.as_mut().unwrap(),"{c}").unwrap();
        let mut output=BufReader::new(p.0.stdout.take().unwrap());
        let mut observations=0;
        let terminal=loop {
            let mut line=String::new();assert!(output.read_line(&mut line).unwrap()>0,"{mode}: missing terminal");
            let value:Value=serde_json::from_str(&line).unwrap();
            let reply=match value["type"].as_str().unwrap() {
                "round_request"=>json!({"version":c["version"],"type":"round_decision","request_id":value["request_id"],"approved":true}),
                "round_settled"=>json!({"version":c["version"],"type":"round_ack","request_id":value["request_id"],"accepted":true}),
                "tool_request"=>json!({"version":c["version"],"type":"tool_result","request_id":value["request_id"],"content":"visible"}),
                "progress_request"=>{
                    assert!(value.get("messages").is_none());assert!(value.get("history_messages").is_none());assert_eq!(value["tool_calls"],1);
                    observations+=1;
                    if mode=="deadline" {continue;}
                    if mode=="cancel" {writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"cancel"})).unwrap();}
                    json!({"version":c["version"],"type":"progress_decision","request_id":value["request_id"],"accepted":mode!="fault","observation":{"worldVersion":"","evidenceIds":[if mode=="changed" {value["round"].to_string()}else{"visible".into()}],"resultEvidenceComplete":mode!="unknown","verification":if mode=="verified" {"passed"}else{""},"nonretryableFailureSignatures":[]}})
                },
                "done"|"error"|"aborted"=>break value,
                other=>panic!("{mode}: unexpected {other}"),
            };
            writeln!(p.0.stdin.as_mut().unwrap(),"{reply}").unwrap();p.0.stdin.as_mut().unwrap().flush().unwrap();
        };
        let status=match mode {"halt"|"fault"=>"error","cancel"|"deadline"=>"aborted",_=>"done"};
        assert_eq!(terminal["status"],status,"{mode}: {terminal}");
        assert_eq!(observations,expected_requests.min(2));
        if mode=="halt" {assert_eq!(terminal["error"]["kind"],"agent_no_progress");}
        assert_eq!(p.0.wait().unwrap().success(),status!="error");worker.join().unwrap();
        assert!(matches!(listener.accept(),Err(error) if error.kind()==std::io::ErrorKind::WouldBlock));
    }
}

#[test]
fn checkpoint_stdio_suspend_resume_and_commit_failure_fence_http() {
    for mode in ["resume","commit_fault","account_resume","account_exhausted","account_unknown","account_missing","account_profile_change","account_ordinal_change","account_extra","account_removed","deadline_resume","deadline_changed","deadline_added","deadline_missing","deadline_removed"] {
        let counted=mode.starts_with("account_");
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
        c["round_control"]=json!(true);c["checkpoint_control"]=json!(true);c["checkpoint_binding"]=json!("fixture-route-tools");c["checkpoint_frame_bytes"]=json!(65536);
        if mode.starts_with("deadline_") && mode != "deadline_added" {
            c["deadline_unix_ms"] = json!(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64 + 30000);
        }
        if counted {
            c["template"]["max_tokens"]=json!(100);
            c["token_admission"]=json!({"encoding":"cl100k_base","output_field":"max_tokens","total_tokens":if mode=="account_exhausted" {800}else{10000},"input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"actor_records":1,"attempt_records":1,"identity_bytes":128,"worker_stack_bytes":1048576});
        }
        c["checkpoint_terminal_fence"] = json!(true);
        c["terminal_persistence"] = json!({"operation_id":"checkpoint-terminal","ack_ms":1500,"attempts":2,"frame_bytes":65536});
        let server=listener.try_clone().unwrap();let expected=if matches!(mode,"resume"|"account_resume"|"deadline_resume") {2}else{1};
        let worker=std::thread::spawn(move || {
            let mut bodies=Vec::new();
            for round in 0..expected {
                let until=Instant::now()+Duration::from_secs(3);
                let mut stream=loop {match server.accept(){Ok((stream,_))=>break stream,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock && Instant::now()<until=>std::thread::sleep(Duration::from_millis(1)),Err(error)=>panic!("{error}")}};
                stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();bodies.push(request(&mut stream));
                let delta=if round==0 {json!({"role":"assistant","tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"lookup","arguments":"{}"}}]})}else{json!({"role":"assistant","content":"done"})};
                let finish=if round==0 {"tool_calls"}else{"stop"};
                if counted && mode!="account_unknown" {
                    let wire=format!("data: {}\n\ndata: [DONE]\n\n",json!({"choices":[{"delta":delta,"finish_reason":finish}],"usage":{"prompt_tokens":760,"completion_tokens":10,"total_tokens":770}}));
                    write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",wire.len(),wire).unwrap();stream.flush().unwrap();
                }else{response(&mut stream,delta,finish);}

            }
            bodies
        });
        let mut saved:Option<Value>=None;let mut starts=Vec::new();let mut tools=0;
        for run in 0..if mode=="commit_fault" {1}else{2} {
            if run == 1 {
                if mode == "deadline_removed" { c.as_object_mut().unwrap().remove("deadline_unix_ms"); }
                if mode == "deadline_added" { c["deadline_unix_ms"] = json!(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64 + 30000); }
                if mode == "deadline_changed" { c["deadline_unix_ms"] = json!(c["deadline_unix_ms"].as_u64().unwrap() + 1000); }
                if mode == "deadline_missing" { saved.as_mut().unwrap().as_object_mut().unwrap().remove("deadline_unix_ms"); }
            }
            if run==1 && mode=="account_removed" {c.as_object_mut().unwrap().remove("token_admission");}
            let mut p=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
            writeln!(p.0.stdin.as_mut().unwrap(),"{c}").unwrap();
            let mut output=BufReader::new(p.0.stdout.take().unwrap());
            let mut frozen_terminal: Option<Value> = None;
            let terminal=loop {
                let mut line=String::new();assert!(output.read_line(&mut line).unwrap()>0);let value:Value=serde_json::from_str(&line).unwrap();
                let reply=match value["type"].as_str().unwrap(){
                    "checkpoint_request"=>{
                        assert_eq!(value["binding"],"fixture-route-tools");assert!(value["deadline_monotonic_ns"].as_u64().is_some());
                        let mut reply=json!({"version":c["version"],"type":"checkpoint_decision","request_id":value["request_id"],"accepted":true,"disposition":"continue"});
                        match value["action"].as_str().unwrap(){
                            "load"=>{if let Some(state)=&saved {
                                let mut loaded=state.clone();
                                match mode {
                                    "account_missing"=>{loaded.as_object_mut().unwrap().remove("model_account");},
                                    "account_profile_change"=>loaded["model_account"]["policy"]["total_tokens"]=json!(9999),
                                    "account_ordinal_change"=>loaded["model_account"]["last_ordinal"]=json!(2),
                                    "account_extra"=>loaded["model_account"]["pending"]=json!(1),
                                    _=>{},
                                }
                                reply["state"]=loaded;
                            }},
                            "begin"=>{starts.push(value["round"].as_u64().unwrap());},
                            "complete"=>{
                                if mode.starts_with("deadline_") && mode != "deadline_added" { assert_eq!(value["state"]["deadline_unix_ms"], c["deadline_unix_ms"]); }
                                assert_eq!(value["state"]["next_round"],1);assert_eq!(value["state"]["tool_calls"],1);
                                if mode=="commit_fault" {reply["accepted"]=json!(false);}else{
                                    if counted {assert_eq!(value["state"]["model_account"]["last_ordinal"],1);assert_eq!(value["state"]["model_account"]["complete"],mode!="account_unknown");if mode!="account_unknown" {assert_eq!(value["state"]["model_account"]["tokens"],770);}}
                                    saved=Some(value["state"].clone());reply["disposition"]=json!("suspend");
                                }
                            },other=>panic!("{other}"),
                        }
                        reply
                    },
                    "round_request"=>{assert_eq!(value["round"],run);json!({"version":c["version"],"type":"round_decision","request_id":value["request_id"],"approved":true})},
                    "round_settled"=>json!({"version":c["version"],"type":"round_ack","request_id":value["request_id"],"accepted":true}),
                    "tool_request"=>{tools+=1;json!({"version":c["version"],"type":"tool_result","request_id":value["request_id"],"content":"stored result"})},
                    "terminal_request"=>{
                        assert_eq!(value["checkpoint_suspended"], run == 0 && mode != "commit_fault");
                        let mut candidate=value.clone();candidate.as_object_mut().unwrap().remove("attempt");
                        if let Some(frozen)=&frozen_terminal {assert_eq!(&candidate,frozen);}else{frozen_terminal=Some(candidate);}
                        let retry=value["attempt"]==1;
                        json!({"version":c["version"],"type":"terminal_ack","request_id":value["request_id"],"attempt":value["attempt"],"accepted":!retry,"retry":retry})
                    },
                    "done"|"error"|"aborted"=>{assert!(frozen_terminal.is_some());break value},
                    other=>panic!("{other}"),
                };
                writeln!(p.0.stdin.as_mut().unwrap(),"{reply}").unwrap();p.0.stdin.as_mut().unwrap().flush().unwrap();
            };
            let successful=mode!="commit_fault" && (run==0 || matches!(mode,"resume"|"account_resume"|"deadline_resume"));
            assert_eq!(terminal["status"],if !successful {"error"}else if run==0 {"aborted"}else{"done"},"{mode}: {terminal}");
            if run==1 && successful {assert_eq!(terminal["result"]["llm_rounds"],2);assert_eq!(terminal["result"]["tool_calls"],1);}
            if run==1 && mode=="account_exhausted" {assert_eq!(terminal["error"]["kind"],"execution_token_budget_exhausted");}
            if run==1 && mode=="account_unknown" {assert_eq!(terminal["error"]["kind"],"execution_token_usage_unknown");}
            assert_eq!(p.0.wait().unwrap().success(),successful);
        }
        assert_eq!(tools,1);assert_eq!(starts,if matches!(mode,"resume"|"account_resume"|"account_exhausted"|"account_unknown"|"deadline_resume") {vec![0,1]}else{vec![0]});
        let bodies=worker.join().unwrap();assert_eq!(bodies.len(),expected);
        if expected==2 {assert_eq!(bodies[1]["messages"].as_array().unwrap().last().unwrap()["content"],"stored result");}
        assert!(matches!(listener.accept(),Err(error) if error.kind()==std::io::ErrorKind::WouldBlock));
    }
}

#[test]
fn host_model_account_counts_then_caps_dispatches_and_settles_actual_http() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    for denied in [false,true] {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
        c["template"]["max_tokens"]=json!(100);c["model_control"]=json!({"ack_ms":2000});
        c["model_counter"]=json!({"encoding":"cl100k_base","output_field":"max_tokens","input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"worker_stack_bytes":1048576});
        let received=Arc::new(AtomicUsize::new(0));let count=received.clone();
        std::thread::scope(|scope| {
            let server=if !denied {Some(scope.spawn(|| {
                let until=Instant::now()+Duration::from_secs(3);
                let mut stream=loop {match listener.accept() {Ok((s,_))=>break s,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock && Instant::now()<until=>std::thread::sleep(Duration::from_millis(1)),Err(error)=>panic!("{error}")}};
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();let body=request(&mut stream);assert_eq!(body["max_tokens"],7);count.fetch_add(1,Ordering::AcqRel);
                let wire=format!("data: {}\n\ndata: [DONE]\n\n",json!({"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}));
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",wire.len(),wire).unwrap();stream.flush().unwrap();
            }))}else{None};
            let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
            writeln!(process.0.stdin.as_mut().unwrap(),"{c}").unwrap();let mut reader=BufReader::new(process.0.stdout.take().unwrap());let mut actions=Vec::new();
            loop {
                let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);let value:Value=serde_json::from_str(&line).unwrap();
                if value["type"]!="model_request" {assert_eq!(value["status"],if denied {"error"}else{"done"});break;}
                assert_eq!(value["attempt_ordinal"],1);assert!(value["input_tokens"].as_u64().unwrap()>0);let action=value["action"].as_str().unwrap();actions.push(action.to_owned());
                let mut reply=json!({"version":c["version"],"type":"model_decision","request_id":value["request_id"],"accepted":!denied});
                if action=="reserve" && !denied {reply["output_tokens"]=json!(7);}
                if action=="settle" {assert_eq!(value["usage"],json!({"prompt_tokens":3,"completion_tokens":2}));assert_eq!(value["unsent"],false);}
                writeln!(process.0.stdin.as_mut().unwrap(),"{reply}").unwrap();process.0.stdin.as_mut().unwrap().flush().unwrap();
            }
            assert_eq!(actions,if denied {vec!["reserve"]}else{vec!["reserve","dispatch","settle"]});assert_eq!(process.0.wait().unwrap().success(),!denied);
            if let Some(server)=server {server.join().unwrap();}
        });
        assert_eq!(received.load(Ordering::Acquire),usize::from(!denied));assert_eq!(listener.accept().unwrap_err().kind(),std::io::ErrorKind::WouldBlock);
    }
}

#[test]
fn responses_dialect_reaches_actual_stdio_http_without_model_name_inference() {
    for profile in ["compatible", "openai", "codex"] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut c = config(format!("http://{}/responses", listener.local_addr().unwrap()));
        c["provider_protocol"] = json!("responses");
        c["responses_profile"] = json!(profile);
        c["tools"] = json!({});
        c["model_control"] = json!({"ack_ms":2000});
        c["model_counter"] = json!({"encoding":"cl100k_base","output_field":"max_tokens","input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"worker_stack_bytes":1048576});
        c["template"] = json!({"model":"fixture","max_tokens":100,"temperature":0.4,"top_p":0.8});
        std::thread::scope(|scope| {
            let server = scope.spawn(|| {
                let until = Instant::now() + Duration::from_secs(3);
                let mut stream = loop {
                    match listener.accept() {
                        Ok((stream, _)) => break stream,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < until => std::thread::sleep(Duration::from_millis(1)),
                        Err(error) => panic!("{error}"),
                    }
                };
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let body = request(&mut stream);
                assert_eq!(body["store"], false);
                if profile == "codex" {
                    assert_eq!(body["instructions"], "");
                    assert_eq!(body["reasoning"]["summary"], "auto");
                    assert_eq!(body["include"], json!(["reasoning.encrypted_content"]));
                    for field in ["temperature", "top_p", "max_tokens", "max_output_tokens"] { assert!(body.get(field).is_none()); }
                } else {
                    assert!(body.get("instructions").is_none());
                    assert_eq!(body["max_output_tokens"], 7);
                    assert_eq!(body["temperature"], 0.4);
                }
                let wire = format!("data: {}\n\n", json!({"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}],"usage":{"input_tokens":3,"output_tokens":2}}}));
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{wire}",wire.len()).unwrap();
                stream.flush().unwrap();
            });
            let mut process = Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap());
            writeln!(process.0.stdin.as_mut().unwrap(), "{c}").unwrap();
            let mut reader = BufReader::new(process.0.stdout.take().unwrap());
            let mut actions = Vec::new();
            let event = loop {
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                let value: Value = serde_json::from_str(&line).unwrap();
                if value["type"] != "model_request" { break value; }
                let action = value["action"].as_str().unwrap();
                actions.push(action.to_owned());
                if action == "settle" {
                    assert_eq!(value["usage"]["prompt_tokens"],3);
                    assert_eq!(value["usage"]["completion_tokens"],2);
                }
                let mut decision = json!({"version":c["version"],"type":"model_decision","request_id":value["request_id"],"accepted":true});
                if action == "reserve" { decision["output_tokens"] = json!(7); }
                writeln!(process.0.stdin.as_mut().unwrap(),"{decision}").unwrap();
                process.0.stdin.as_mut().unwrap().flush().unwrap();
            };
            assert_eq!(actions, vec!["reserve","dispatch","settle"]);
            assert_eq!(event["status"], "done", "{event}");
            assert!(process.0.wait().unwrap().success());
            server.join().unwrap();
        });
        assert_eq!(listener.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
    }
}

#[test]
fn dispatch_headers_replace_configured_credentials_and_invalid_headers_never_reach_http() {
    for mode in ["valid", "empty", "bad_name", "bad_value", "duplicate", "forbidden", "wrong_stage"] {
        let successful = matches!(mode,"valid"|"empty");
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
        c["headers"]=json!({"Authorization":"Bearer stale","X-Removed":"old"});
        c["template"]["max_tokens"]=json!(100);c["model_control"]=json!({"ack_ms":2000});
        c["model_counter"]=json!({"encoding":"cl100k_base","output_field":"max_tokens","input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"worker_stack_bytes":1048576});
        std::thread::scope(|scope| {
            let server=if successful {Some(scope.spawn(|| {
                let until=Instant::now()+Duration::from_secs(3);
                let mut stream=loop {match listener.accept() {Ok((s,_))=>break s,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock && Instant::now()<until=>std::thread::sleep(Duration::from_millis(1)),Err(error)=>panic!("{error}")}};
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let (body,headers)=request_with_headers(&mut stream);assert_eq!(body["max_tokens"],7);
                let headers=headers.to_ascii_lowercase();assert!(!headers.contains("stale"));assert!(!headers.contains("x-removed"));
                if mode=="valid" {assert!(headers.contains("authorization: bearer fresh"));} else {assert!(!headers.contains("authorization:"));}
                let wire=format!("data: {}\n\ndata: [DONE]\n\n",json!({"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}));
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",wire.len(),wire).unwrap();stream.flush().unwrap();
            }))}else{None};
            let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native")).args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap());
            writeln!(process.0.stdin.as_mut().unwrap(),"{c}").unwrap();
            let mut reader=BufReader::new(process.0.stdout.take().unwrap());
            loop {
                let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);let value:Value=serde_json::from_str(&line).unwrap();
                if value["type"]!="model_request" {assert_eq!(value["status"],if successful {"done"}else{"error"},"{mode}: {value}");break;}
                let action=value["action"].as_str().unwrap();
                let mut reply=json!({"version":c["version"],"type":"model_decision","request_id":value["request_id"],"accepted":true});
                if action=="reserve" {reply["output_tokens"]=json!(7);}
                if action=="dispatch" || (mode=="wrong_stage" && action=="reserve") {
                    reply["headers"]=match mode {
                        "valid"|"wrong_stage"=>json!({"Authorization":"Bearer fresh"}),
                        "empty"=>json!({}),
                        "bad_name"=>json!({"bad header":"token"}),
                        "bad_value"=>json!({"Authorization":"token\r\nX-Injected: value"}),
                        "duplicate"=>json!({"Authorization":"one","authorization":"two"}),
                        "forbidden"=>json!({"Host":"other"}),
                        _=>unreachable!(),
                    };
                }
                if action=="settle" {assert!(value.get("headers").is_none());}
                writeln!(process.0.stdin.as_mut().unwrap(),"{reply}").unwrap();process.0.stdin.as_mut().unwrap().flush().unwrap();
            }
            assert_eq!(process.0.wait().unwrap().success(),successful);
            if let Some(server)=server {server.join().unwrap();}
        });
        assert_eq!(listener.accept().unwrap_err().kind(),std::io::ErrorKind::WouldBlock);
    }
}

#[test]
fn initial_stop_preserves_future_deadline_and_aborts_before_http() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat", listener.local_addr().unwrap()));
    c["deadline_unix_ms"] = json!(u64::MAX);
    c["cancel_requested"] = json!(true);
    let mut process = Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent", "65536"]).stdin(Stdio::piped())
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(process.0.stdin.as_mut().unwrap(), "{c}").unwrap();
    let mut reader = BufReader::new(process.0.stdout.take().unwrap());
    let mut line = String::new();
    assert!(reader.read_line(&mut line).unwrap() > 0);
    let terminal: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(terminal["status"], "aborted", "{terminal}");
    assert_eq!(listener.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
    assert!(process.0.wait().unwrap().success());
}

#[test]
fn pricing_without_round_delivery_refuses_before_model_reservation() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
    c["tools"]=json!({});
    c["model_control"]=json!({"ack_ms":100,"pricing":true});
    c["model_counter"]=json!({"encoding":"cl100k_base","output_field":"max_tokens","input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,"worker_stack_bytes":1048576});
    let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent","65536"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(process.0.stdin.as_mut().unwrap(),"{c}").unwrap();
    assert!(!process.0.wait().unwrap().success());
    let mut output=String::new();process.0.stdout.take().unwrap().read_to_string(&mut output).unwrap();
    assert!(output.is_empty(),"No model reservation may precede delivery admission: {output}");
    assert_eq!(listener.accept().unwrap_err().kind(),std::io::ErrorKind::WouldBlock);
}

#[test]
fn persistent_worker_reuses_encoder_and_keeps_task_account_and_terminal_scope_independent() {
    persistent_worker_scenario(false);
}
#[test]
fn persistent_worker_refuses_previous_invocation_terminal_ack_and_never_advertises_readiness() {
    persistent_worker_scenario(true);
}
fn persistent_worker_scenario(stale_ack: bool) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/chat", listener.local_addr().unwrap());
    let mut c = config(endpoint);
    c["tools"] = json!({}); c["template"]["max_tokens"] = json!(32);
    let counter = tofu_runtime::token_ingress::Counter::new(7,"stdio-task".into(),"fixture".into(),
        tofu_runtime::token_ingress::Encoder::new(tofu_runtime::token_ingress::Encoding::Cl100k),
        tofu_runtime::token_ingress::Bounds {input_bytes:65536,text_bytes:65536,tool_json_bytes:65536,depth:128}).unwrap();
    let input = counter.count(7,"stdio-task",c["messages"].as_array().unwrap(),c["template"].as_object().unwrap()).unwrap();
    c["token_admission"] = json!({"encoding":"cl100k_base","output_field":"max_tokens","total_tokens":input+2,
        "input_bytes":65536,"text_bytes":65536,"tool_json_bytes":65536,"depth":128,
        "actor_records":1,"attempt_records":1,"identity_bytes":128,"worker_stack_bytes":1048576});
    c["terminal_persistence"] = json!({"operation_id":"same-operation","frame_bytes":65536,"ack_ms":2000,"attempts":2});
    let server = std::thread::spawn(move || {
        for ordinal in 1..=2 {
            let (mut socket,_) = listener.accept().unwrap();
            socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let (body,headers) = request_with_headers(&mut socket);
            assert_eq!(body["max_tokens"],2);
            assert!(headers.to_lowercase().contains(&format!("x-run: {ordinal}")));
            let event = json!({"choices":[{"delta":{"content":"reply"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":input,"completion_tokens":1,"total_tokens":input+1}});
            let wire=format!("data: {event}\n\ndata: [DONE]\n\n");
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",wire.len(),wire).unwrap();
            socket.flush().unwrap();
        }
    });
    let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent-worker","131072","7"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    let mut stdout=BufReader::new(process.0.stdout.take().unwrap());
    let read=|reader:&mut BufReader<std::process::ChildStdout>| {
        let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);serde_json::from_str::<Value>(&line).unwrap()
    };
    let ready=read(&mut stdout);assert_eq!(ready["type"],"worker_ready");assert_eq!(ready["cached_encodings"],0);
    for sequence in 1..=2 {
        c["headers"]=json!({"X-Run":sequence.to_string()});
        writeln!(process.0.stdin.as_mut().unwrap(),"{}",json!({"version":"tofu.native-agent-worker/v1","type":"worker_run",
            "owner_user_id":7,"sequence":sequence,"configuration":c})).unwrap();
        let request=read(&mut stdout);assert_eq!(request["type"],"worker_event");assert_eq!(request["sequence"],sequence);
        assert_eq!(request["frame"]["type"],"terminal_request");
        writeln!(process.0.stdin.as_mut().unwrap(),"{}",json!({"version":"tofu.native-agent-worker/v1","type":"worker_control",
            "owner_user_id":7,"sequence":if stale_ack && sequence==2 {1}else{sequence},"frame":{"version":"tofu.native-agent-stdio/v1","type":"terminal_ack",
                "request_id":"same-operation","attempt":1,"accepted":true,"retry":false}})).unwrap();
        if stale_ack && sequence==2 {
            let mut line=String::new();assert_eq!(stdout.read_line(&mut line).unwrap(),0);
            assert!(!process.0.wait().unwrap().success());server.join().unwrap();return;
        }
        let terminal=read(&mut stdout);assert_eq!(terminal["type"],"worker_event");assert_eq!(terminal["frame"]["type"],"done");
        let ready=read(&mut stdout);assert_eq!(ready["type"],"worker_ready");assert_eq!(ready["sequence"],sequence);assert_eq!(ready["cached_encodings"],1);
    }
    writeln!(process.0.stdin.as_mut().unwrap(),"{}",json!({"version":"tofu.native-agent-worker/v1","type":"worker_shutdown","owner_user_id":7,"sequence":3})).unwrap();
    assert_eq!(read(&mut stdout)["type"],"worker_closed");assert!(process.0.wait().unwrap().success());server.join().unwrap();
}

#[test]
fn persistent_worker_refuses_foreign_principals_sequences_and_bare_commands_before_http() {
    for mode in ["owner","configuration_owner","sequence","extra","bare"] {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let c=config(format!("http://{}/chat",listener.local_addr().unwrap()));
        let mut command=json!({"version":"tofu.native-agent-worker/v1","type":"worker_run","owner_user_id":7,"sequence":1,"configuration":c});
        match mode {
            "owner"=>command["owner_user_id"]=json!(8),
            "configuration_owner"=>command["configuration"]["owner_user_id"]=json!(8),
            "sequence"=>command["sequence"]=json!(0),
            "extra"=>command["foreign"]=json!(true),
            "bare"=>command=command["configuration"].clone(),
            _=>unreachable!(),
        }
        let mut process=Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
            .args(["run-http-agent-worker","131072","7"]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
        let mut stdout=BufReader::new(process.0.stdout.take().unwrap());let mut line=String::new();
        stdout.read_line(&mut line).unwrap();assert_eq!(serde_json::from_str::<Value>(&line).unwrap()["type"],"worker_ready");
        writeln!(process.0.stdin.as_mut().unwrap(),"{command}").unwrap();line.clear();
        assert_eq!(stdout.read_line(&mut line).unwrap(),0);assert!(!process.0.wait().unwrap().success());
        assert!(matches!(listener.accept(),Err(error) if error.kind()==std::io::ErrorKind::WouldBlock));
    }
}


#[test]
fn selected_terminal_reference_commits_abort_without_http() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat",listener.local_addr().unwrap()));
    c["cancel_requested"] = json!(true);
    c["terminal_persistence"] = json!({"operation_id":"abort-commit","ack_ms":1500,
        "attempts":2,"frame_bytes":65536,"completion_reference":true});
    let mut p = Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent","65536"]).stdin(Stdio::piped())
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(p.0.stdin.as_mut().unwrap(),"{c}").unwrap();
    let mut reader=BufReader::new(p.0.stdout.take().unwrap());
    let mut line=String::new();reader.read_line(&mut line).unwrap();
    let request:Value=serde_json::from_str(&line).unwrap();
    assert_eq!(request["type"],"terminal_request");
    assert_eq!(request["event"]["status"],"aborted");
    writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"terminal_ack",
        "request_id":"abort-commit","attempt":1,"accepted":true,"retry":false})).unwrap();
    line.clear();reader.read_line(&mut line).unwrap();
    let reference:Value=serde_json::from_str(&line).unwrap();
    assert_eq!(reference,json!({"version":c["version"],"type":"terminal_committed",
        "request_id":"abort-commit","owner_user_id":7,"task_id":"stdio-task","attempt":1,"status":"aborted"}));
    assert!(p.0.wait().unwrap().success());
    assert_eq!(listener.accept().unwrap_err().kind(),std::io::ErrorKind::WouldBlock);
}

#[test]
fn selected_retry_reference_commits_abort_without_http() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut c = config(format!("http://{}/chat",listener.local_addr().unwrap()));
    c["cancel_requested"] = json!(true);
    c["terminal_persistence"] = json!({"operation_id":"abort-commit","ack_ms":1500,
        "attempts":2,"frame_bytes":65536,"completion_reference":true,"retry_reference":true});
    let mut p = Process(Command::new(env!("CARGO_BIN_EXE_tofu-native"))
        .args(["run-http-agent","65536"]).stdin(Stdio::piped())
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap());
    writeln!(p.0.stdin.as_mut().unwrap(),"{c}").unwrap();
    let mut reader=BufReader::new(p.0.stdout.take().unwrap());
    let mut line=String::new();reader.read_line(&mut line).unwrap();
    let request:Value=serde_json::from_str(&line).unwrap();
    assert_eq!(request["type"],"terminal_request");
    assert_eq!(request["event"]["status"],"aborted");
    writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"terminal_ack",
        "request_id":"abort-commit","attempt":1,"accepted":false,"retry":true})).unwrap();
    line.clear();reader.read_line(&mut line).unwrap();
    let retry:Value=serde_json::from_str(&line).unwrap();
    assert_eq!(retry["type"],"terminal_retry");
    assert_eq!(retry["status"],"aborted");
    assert_eq!(retry["attempt"],2);
    assert!(retry.get("event").is_none());
    writeln!(p.0.stdin.as_mut().unwrap(),"{}",json!({"version":c["version"],"type":"terminal_ack",
        "request_id":"abort-commit","attempt":2,"accepted":true,"retry":false})).unwrap();
    line.clear();reader.read_line(&mut line).unwrap();
    let reference:Value=serde_json::from_str(&line).unwrap();
    assert_eq!(reference,json!({"version":c["version"],"type":"terminal_committed",
        "request_id":"abort-commit","owner_user_id":7,"task_id":"stdio-task","attempt":2,"status":"aborted"}));
    assert!(p.0.wait().unwrap().success());
    assert_eq!(listener.accept().unwrap_err().kind(),std::io::ErrorKind::WouldBlock);
}
