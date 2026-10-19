//! Offline request-count comparison; raw clocks and separate RSS processes.
//! Full-encoder parity runs outside timing. No provider, storage or live models.
use serde_json::json;
use std::{hint::black_box, io::{self, BufRead, Write}, time::Instant};
use tofu_runtime::token_ingress::{Bounds, Counter, Encoder, Encoding};

fn cpu_ns() -> u128 {
    let mut clock = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    assert_eq!(unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut clock) }, 0);
    clock.tv_sec as u128 * 1_000_000_000 + clock.tv_nsec as u128
}

fn clocks(count: &impl Fn() -> u64, expected: u64, samples: usize, memory: bool) -> (Vec<u128>, Vec<u128>) {
    let mut wall = Vec::new();
    let mut cpu = Vec::new();
    for _ in 0..samples {
        let clock = cpu_ns();
        let start = Instant::now();
        let actual = black_box(count());
        let elapsed = start.elapsed().as_nanos();
        let used = cpu_ns() - clock;
        assert_eq!(actual, expected);
        if !memory { wall.push(elapsed); cpu.push(used); }
    }
    (wall, cpu)
}

fn main() {
    let arguments: Vec<_> = std::env::args().collect();
    let memory = arguments.iter().any(|value| value == "--memory");
    let worker = arguments.iter().any(|value| value == "--worker");
    let selected = |flag: &str| arguments.iter().position(|value| value == flag)
        .map(|position| arguments.get(position + 1).expect("selection value missing").as_str());
    let samples: usize = std::env::var("TOFU_TOKEN_SCRATCH_SAMPLES")
        .map(|value| value.parse().expect("sample count must be an integer"))
        .unwrap_or(100);
    assert!((100..=10000).contains(&samples));
    let template = json!({"model":"fixture"}).as_object().unwrap().clone();
    for (name, encoding) in [("cl100k_base", Encoding::Cl100k), ("o200k_base", Encoding::O200k)] {
        if selected("--encoding").is_some_and(|value| value != name) { continue; }
        let fixtures = [
            ("small", "Hello, world.".to_owned()),
            ("prose", "The request owns temporary buffers and releases them after counting. ".repeat(64)),
            ("code", "fn request_scope(value: usize) -> usize { value.saturating_add(17) }\n".repeat(64)),
            ("unicode", "上下文计数必须保持结果一致，也需要验证并发与取消。😀 café\n".repeat(64)),
            ("short_unmerged", " qzxvjk".repeat(512)),
            ("mixed", ["a".repeat(1023), " text ".repeat(64), "a".repeat(1024), " qzxvjk".repeat(32)].join("\n")),
            ("long_piece", "abcdef".repeat(1024)),
        ];
        let expected: Vec<_> = {
            let full = match encoding {
                Encoding::Cl100k => tiktoken_rs::cl100k_base().unwrap(),
                Encoding::O200k => tiktoken_rs::o200k_base().unwrap(),
            };
            fixtures.iter().map(|(_, text)| full.encode_ordinary(text).len() as u64
                + tofu_runtime::execution_contract::TOKEN_MESSAGE_OVERHEAD
                + tofu_runtime::execution_contract::TOKEN_REQUEST_OVERHEAD).collect()
        };
        for ((shape, text), expected) in fixtures.into_iter().zip(expected) {
            if selected("--case").is_some_and(|value| value != shape) { continue; }
            let encoder = Encoder::new(encoding);
            let counter = Counter::new(7, "task".into(), "fixture".into(), encoder,
                Bounds { input_bytes: 1 << 20, text_bytes: 1 << 20, tool_json_bytes: 1 << 20, depth: 128 }).unwrap();
            let messages = [json!({"role":"user","content":text})];
            let count = || counter.count(7, "task", &messages, &template).unwrap();
            let cold_start = Instant::now();
            assert_eq!(count(), expected);
            let cold_wall_ns = cold_start.elapsed().as_nanos();
            for _ in 0..20 { assert_eq!(black_box(count()), expected); }
            if worker {
                assert!(!memory && selected("--encoding").is_some() && selected("--case").is_some());
                println!("{}", json!({"ready":true,"encoding":name,"shape":shape,"tokens":expected,
                    "cold_wall_ns":cold_wall_ns,"warmup_operations":20,"pid":std::process::id()}));
                io::stdout().flush().unwrap();
                for line in io::stdin().lock().lines() {
                    let operations: usize = line.unwrap().parse().expect("batch count must be an integer");
                    if operations == 0 { return; }
                    assert!(operations <= 1000);
                    let (wall, cpu) = clocks(&count, expected, operations, false);
                    println!("{}", json!({"wall_ns":wall,"cpu_ns":cpu}));
                    io::stdout().flush().unwrap();
                }
                return;
            }
            let (wall, cpu) = clocks(&count, expected, samples, memory);
            let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
            assert_eq!(unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) }, 0);
            let usage = unsafe { usage.assume_init() };
            println!("{}", json!({"encoding":name,"shape":shape,"tokens":expected,
                "cold_wall_ns":cold_wall_ns,"wall_ns":wall,"cpu_ns":cpu,
                "warmup_operations":20,"samples":samples,"memory_worker":memory,
                "peak_rss_bytes":usage.ru_maxrss as u64 * 1024}));
        }
    }
}
