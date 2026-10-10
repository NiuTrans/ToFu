//! Compiled contract validation measurement; excludes authorization and tools.
use serde_json::{json, Value};
use std::{io::Read, time::Instant};
use tofu_runtime::tool_contract::Catalog;
fn main() {
    let n: usize = std::env::args().nth(1).unwrap().parse().unwrap();
    let mut text = String::new();
    std::io::stdin().read_to_string(&mut text).unwrap();
    let input: Value = serde_json::from_str(&text).unwrap();
    let catalog = Catalog::compile(7, "measure", &json!({"lookup":input["document"]})).unwrap();
    let start = Instant::now();
    for _ in 0..n {
        catalog
            .validate(7, "lookup", input["arguments"].clone())
            .unwrap();
    }
    println!(
        "{{\"operations\":{n},\"wall_ns\":{}}}",
        start.elapsed().as_nanos()
    );
}
