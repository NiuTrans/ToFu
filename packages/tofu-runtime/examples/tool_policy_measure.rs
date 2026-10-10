//! Isolated occurrence-admission measurement, excluding provider/task execution.
use serde_json::Value;
use std::{hint::black_box, io::Read, time::Instant};
use tofu_runtime::tool_policy::FloodGuard;
fn main() {
    let n: usize = std::env::args().nth(1).unwrap().parse().unwrap();
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let call: Value = serde_json::from_str(&input).unwrap();
    let mut guard = FloodGuard::new(32, 128).unwrap();
    let start = Instant::now();
    for _ in 0..n {
        guard.observe(0, black_box(&call), true).unwrap();
    }
    println!(
        "{{\"operations\":{n},\"wall_ns\":{},\"retained_positions\":{}}}",
        start.elapsed().as_nanos(),
        guard.retained_positions()
    );
}
