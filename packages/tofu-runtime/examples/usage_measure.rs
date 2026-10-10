//! CPU-only whole-observation diagnostic over identical captured input cases.
//! Parsing is excluded; no HTTP, billing, account admission or application claim.
use serde_json::Value;
use std::{hint::black_box, time::Instant};
fn cpu_ns() -> u128 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    assert_eq!(
        unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut value) },
        0
    );
    value.tv_sec as u128 * 1_000_000_000 + value.tv_nsec as u128
}
fn main() {
    let source: Value = serde_json::from_str(include_str!(
        "../tests/fixtures/python_usage_observations.json"
    ))
    .unwrap();
    let cases = source["cases"].as_array().unwrap();
    let rounds: usize = std::env::args()
        .nth(1)
        .unwrap_or("20".into())
        .parse()
        .unwrap();
    for _ in 0..20 {
        for c in cases {
            black_box(tofu_runtime::usage::observed_totals(black_box(&c["usage"])).unwrap());
        }
    }
    let cpu_start = cpu_ns();
    let start = Instant::now();
    for _ in 0..rounds {
        for c in cases {
            black_box(tofu_runtime::usage::observed_totals(black_box(&c["usage"])).unwrap());
        }
    }
    println!(
        "{}",
        serde_json::json!({"calls":rounds*cases.len(),"elapsed_ns":start.elapsed().as_nanos(),"cpu_ns":cpu_ns()-cpu_start})
    );
}
