//! Offline, real ChatStream completion measurement. Feed/setup and exact output
//! validation sit outside the finish clock. Build a separate allocation_counts
//! example with `cargo rustc --release --example provider_finish_measure --
//! --cfg allocation_counts`; its instrumented clocks are deliberately omitted.
#![allow(unexpected_cfgs)]
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{hint::black_box, time::Instant};
use tofu_runtime::{
    execution_contract::GATEWAY_LIMIT_MAX_CHARS,
    provider::{Budget, ChatStream},
};

#[cfg(allocation_counts)]
mod allocations {
    use std::{
        alloc::{GlobalAlloc, Layout, System},
        sync::atomic::{AtomicUsize, Ordering::Relaxed},
    };
    pub struct Allocator;
    static CALLS: AtomicUsize = AtomicUsize::new(0);
    static BYTES: AtomicUsize = AtomicUsize::new(0);
    fn record(bytes: usize) {
        CALLS.fetch_add(1, Relaxed);
        BYTES.fetch_add(bytes, Relaxed);
    }
    unsafe impl GlobalAlloc for Allocator {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let pointer = System.alloc(layout);
            if !pointer.is_null() {
                record(layout.size());
            }
            pointer
        }
        unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
            System.dealloc(pointer, layout)
        }
        unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, bytes: usize) -> *mut u8 {
            let result = System.realloc(pointer, layout, bytes);
            if !result.is_null() {
                record(bytes);
            }
            result
        }
    }
    pub fn begin() {
        CALLS.store(0, Relaxed);
        BYTES.store(0, Relaxed);
    }
    pub fn finish() -> (usize, usize) {
        (CALLS.load(Relaxed), BYTES.load(Relaxed))
    }
}
#[cfg(allocation_counts)]
#[global_allocator]
static ALLOCATOR: allocations::Allocator = allocations::Allocator;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let case = args.get(1).map(String::as_str).unwrap_or("ascii_long");
    let operations: usize = args.get(2).map(|v| v.parse().unwrap()).unwrap_or(64);
    let phrase = "RATE LIMIT EXCEEDED";
    let content = match case {
        "short" => "A useful short answer.".to_owned(),
        "gateway" => phrase.to_owned(),
        "unicode_boundary" => format!(
            "{phrase}{}",
            "İ".repeat(GATEWAY_LIMIT_MAX_CHARS - phrase.chars().count())
        ),
        "ascii_long" => "X".repeat(64 * 1024),
        "fragmented" => "X".repeat(64 * 1024 + 1),
        "unicode_long" | "non_stop" | "with_tool" => "İ".repeat(32 * 1024),
        "tool_arguments" | "tool_fragments" => "Tool arguments ready.".to_owned(),
        "whitespace" => " \t".repeat(32 * 1024),
        "megabyte" => "X".repeat(1024 * 1024),
        _ => panic!("unknown fixture"),
    };
    assert!((1..=10000).contains(&operations));
    let finish = if case == "non_stop" { "length" } else { "stop" };
    let with_tool = matches!(case, "with_tool" | "tool_arguments" | "tool_fragments");
    let arguments = if matches!(case, "tool_arguments" | "tool_fragments") {
        json!({"text":"X".repeat(64 * 1024)}).to_string()
    } else {
        "{}".to_owned()
    };
    assert!((content.len() + arguments.len()) * operations <= 64 * 1024 * 1024);
    let mut delta = json!({"content":content});
    if with_tool {
        delta["tool_calls"] = json!([{"index":0,"id":"call-1","type":"function",
            "function":{"name":"status","arguments":arguments}}]);
    }
    let event = json!({"choices":[{"delta":delta,"finish_reason":finish}],
        "usage":{"prompt_tokens":10,"completion_tokens":20}});
    let wire = if matches!(case, "fragmented" | "tool_fragments") {
        let source = if case == "fragmented" {
            &content
        } else {
            &arguments
        };
        let mut wire = String::new();
        for (index, bytes) in source.as_bytes().chunks(1024).enumerate() {
            let part = std::str::from_utf8(bytes).unwrap();
            let delta = if case == "fragmented" {
                json!({"content":part})
            } else {
                json!({"content":if index == 0 { &content } else { "" },
                    "tool_calls":[{"index":0,"id":"call-1","type":"function",
                        "function":{"name":"status","arguments":part}}]})
            };
            let event = json!({"choices":[{"delta":delta}]});
            wire.push_str(&format!("data: {event}\n\n"));
        }
        let terminal = json!({"choices":[{"delta":{},"finish_reason":finish}],
            "usage":{"prompt_tokens":10,"completion_tokens":20}});
        wire.push_str(&format!("data: {terminal}\n\ndata: [DONE]\n\n"));
        wire
    } else {
        format!("data: {event}\n\ndata: [DONE]\n\n")
    };
    let expected_state = if case == "whitespace" {
        "empty_response"
    } else if case == "gateway" || case == "unicode_boundary" {
        "gateway_limit_response"
    } else {
        "provider_finished"
    };
    let mut expected = json!({"role":"assistant","content":content});
    if with_tool {
        expected["tool_calls"] = json!([{"id":"call-1","type":"function",
            "function":{"name":"status","arguments":arguments}}]);
    }
    let expected_usage = json!({"prompt_tokens":10,"completion_tokens":20});
    let mut samples = [0u128; 7];
    #[allow(unused_mut)]
    let mut allocation_samples = [(0usize, 0usize); 7];
    for sample in 0..samples.len() {
        let mut ready = Vec::with_capacity(operations);
        for _ in 0..operations {
            let mut stream = ChatStream::new(Budget {
                event_bytes: wire.len(),
                output_bytes: content.len() + arguments.len() + 1024,
                tool_calls: 1,
            })
            .unwrap();
            assert!(stream.feed(black_box(wire.as_bytes())).unwrap());
            ready.push(stream);
        }
        let mut results = Vec::with_capacity(operations);
        #[cfg(allocation_counts)]
        allocations::begin();
        let started = Instant::now();
        for stream in ready {
            results.push(black_box(stream).finish(false).unwrap());
        }
        samples[sample] = started.elapsed().as_nanos();
        #[cfg(allocation_counts)]
        {
            allocation_samples[sample] = allocations::finish();
        }
        for result in results {
            assert_eq!(result.state.as_str(), expected_state);
            assert_eq!(result.message, expected);
            assert_eq!(result.usage, expected_usage);
            assert_eq!(result.finish_reason.as_deref(), Some(finish));
            assert!(result.done_seen);
            assert_eq!(result.malformed_frames, 0);
            assert!(result.cost.is_none());
            let compact = |value: &serde_json::Value| {
                let serde_json::Value::String(text) = value else {
                    panic!("missing string")
                };
                assert_eq!(text.capacity(), text.len(), "retained string growth slack");
            };
            compact(&result.message["content"]);
            if with_tool {
                let call = &result.message["tool_calls"][0];
                compact(&call["id"]);
                compact(&call["function"]["name"]);
                compact(&call["function"]["arguments"]);
            }
        }
    }
    if cfg!(allocation_counts) {
        samples.fill(0);
    }
    println!(
        "{}",
        json!({"case":case,"operations":operations,"payload_bytes":content.len(),
        "wire_sha256":format!("{:x}", Sha256::digest(wire.as_bytes())),"state":expected_state,
        "samples_ns":samples,"allocation_counts":cfg!(allocation_counts),
        "allocation_samples":allocation_samples,"scope":"ChatStream::finish; feed and output validation excluded"})
    );
}
