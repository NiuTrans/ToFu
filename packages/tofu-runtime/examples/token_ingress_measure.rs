//! Owned encoder cold/warm CPU and requested-allocation diagnostics; no HTTP.
use serde_json::Value;
use std::{
    alloc::{GlobalAlloc, Layout, System},
    hint::black_box,
    sync::atomic::{AtomicUsize, Ordering},
    time::Instant,
};
use tofu_runtime::token_ingress::{Bounds, Counter, Encoder, Encoding};
struct Allocator;
static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if !p.is_null() {
            let n = LIVE.fetch_add(l.size(), Ordering::Relaxed) + l.size();
            PEAK.fetch_max(n, Ordering::Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        LIVE.fetch_sub(l.size(), Ordering::Relaxed);
        System.dealloc(p, l)
    }
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
fn cpu() -> u128 {
    let mut t = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    assert_eq!(
        unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut t) },
        0
    );
    t.tv_sec as u128 * 1_000_000_000 + t.tv_nsec as u128
}
fn main() {
    let arguments: Vec<_> = std::env::args().collect();
    let cold_only = arguments.iter().any(|value| value == "--cold-only");
    let selected = arguments.iter().position(|value| value == "--encoding")
        .map(|position| arguments.get(position + 1).expect("encoding missing").as_str());
    assert!(!cold_only || selected.is_some(), "cold worker requires one encoding");
    assert!(selected.is_none_or(|value| matches!(value, "cl100k_base" | "o200k_base")));
    let rows: Value =
        serde_json::from_str(include_str!("../tests/fixtures/python_token_ingress.json")).unwrap();
    for (label, encoding) in [
        ("cl100k_base", Encoding::Cl100k),
        ("o200k_base", Encoding::O200k),
    ] {
        if selected.is_some_and(|value| value != label) { continue; }
        let cases: Vec<_> = rows
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["encoding"] == label)
            .collect();
        let before = LIVE.load(Ordering::Relaxed);
        PEAK.store(before, Ordering::Relaxed);
        let encoder = Encoder::new(encoding);
        let count = |r: &Value| {
            let c = Counter::new(
                7,
                "task".into(),
                r["model"].as_str().unwrap().into(),
                encoder.clone(),
                Bounds {
                    input_bytes: 1048576,
                    text_bytes: 1048576,
                    tool_json_bytes: 1048576,
                    depth: 128,
                },
            )
            .unwrap();
            let n = c
                .count(
                    7,
                    "task",
                    r["messages"].as_array().unwrap(),
                    r["template"].as_object().unwrap(),
                )
                .unwrap();
            assert_eq!(n, r["tokens"].as_u64().unwrap());
            n
        };
        let c = cpu();
        let t = Instant::now();
        black_box(count(cases[0]));
        let cold_wall = t.elapsed().as_nanos();
        let cold_cpu = cpu() - c;
        if !cold_only { for r in &cases { black_box(count(r)); } }
        let c = cpu();
        let t = Instant::now();
        for _ in 0..if cold_only { 0 } else { 20 } {
            for r in &cases {
                black_box(count(r));
            }
        }
        let wall = t.elapsed().as_nanos();
        let cpu = cpu() - c;
        let retained = LIVE.load(Ordering::Relaxed) - before;
        let peak = PEAK.load(Ordering::Relaxed) - before;
        drop(encoder);
        let remaining = LIVE.load(Ordering::Relaxed) - before;
        println!(
            "{}",
            serde_json::json!({"encoding":label,"cold_wall_ns":cold_wall,"cold_cpu_ns":cold_cpu,
                "pid":std::process::id(),"cold_only":cold_only,"calls":if cold_only { 0 } else { cases.len()*20 },"wall_ns":wall,"cpu_ns":cpu,"retained_requested_bytes":retained,"peak_requested_bytes":peak,"remaining_after_drop":remaining})
        );
    }
}
