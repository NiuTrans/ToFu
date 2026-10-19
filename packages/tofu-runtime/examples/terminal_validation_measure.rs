//! Component comparison for terminal validation ownership; excludes task/storage I/O.
use serde_json::{json, Value};
use std::time::Instant;
use tofu_runtime::tool_contract::Catalog;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).expect("baseline or candidate");
    assert!(mode == "baseline" || mode == "candidate");
    let size: usize = args.get(2).expect("payload bytes").parse().unwrap();
    let samples: usize = args.get(3).expect("sample count").parse().unwrap();
    let warmups: usize = args.get(4).expect("warmup count").parse().unwrap();
    assert!(size <= 16 << 20 && samples > 0 && samples <= 10000 && warmups <= 10000);
    let contract: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_agent_stdio_v1.schema.json"
    ))
    .unwrap();
    let catalog = Catalog::compile(
        7,
        "terminal-measure",
        &json!({"terminal_request":{
        "name":"terminal_request","contractVersion":"tofu.tool-contract/v2",
        "arguments_schema":contract["$defs"]["terminal_request"]}}),
    )
    .unwrap();
    let fixture = json!({"version":"tofu.native-agent-stdio/v1","type":"terminal_request",
        "request_id":"commit","owner_user_id":7,"task_id":"task","attempt":1,
        "event":{"status":"done","type":"done","result":{
            "messages":[{"role":"assistant","content":"x".repeat(size)}],
            "usage":[{"prompt_tokens":3,"completion_tokens":2}]}},
        "receipt":{"execution_id":"task","invariants_satisfied":true}});
    let expected = serde_json::to_vec(&fixture).unwrap();
    let mut raw = Vec::with_capacity(samples);
    for index in 0..warmups + samples {
        // Both modes borrow the same frozen candidate shape; envelope construction
        // is outside this component clock. Baseline copies only for validation.
        let mut value = fixture.clone();
        let started = Instant::now();
        if mode == "baseline" {
            catalog
                .validate(7, "terminal_request", value.clone())
                .unwrap();
        } else {
            value = catalog.validate(7, "terminal_request", value).unwrap();
        }
        let bytes = serde_json::to_vec(&value).unwrap();
        let elapsed = started.elapsed().as_nanos() as u64;
        assert_eq!(bytes, expected);
        if index >= warmups {
            raw.push(elapsed);
        }
    }
    println!(
        "{}",
        json!({"scope":"terminal validation plus serialization; excludes envelope construction, SDK, storage, startup and allocator residency",
        "mode":mode,"payload_bytes":size,"samples":samples,"warmups":warmups,
        "serialized_bytes":expected.len(),"raw_wall_ns":raw,"exact_bytes_verified":true,
        "performance_admitted":false,"default_selection_admitted":false})
    );
}
