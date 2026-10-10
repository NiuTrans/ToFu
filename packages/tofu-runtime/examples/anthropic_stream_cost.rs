//! Offline translation kernel measurement; excludes transport and model latency.
use serde_json::{json, Value};
use std::{collections::BTreeMap, time::Instant};
use tofu_runtime::anthropic::{Budget, Translator};
fn main() {
    let fragments: usize = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "10000".into())
        .parse()
        .unwrap();
    assert!((1..=100000).contains(&fragments));
    let first=json!({"type":"content_block_start","index":0,"content_block":{"type":"server_tool_use","id":"host","name":"search","input":{}}}).to_string();
    let begin=json!({"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"text\":\""}}).to_string();
    let append=json!({"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"x"}}).to_string();
    let end=json!({"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\"}"}}).to_string();
    let stop = json!({"type":"message_stop"}).to_string();
    let mut samples = Vec::new();
    let mut retained = 0;
    for _ in 0..7 {
        let started = Instant::now();
        let mut owner = Translator::new(
            Budget {
                blocks: 1,
                retained_bytes: fragments * 32 + 65536,
            },
            BTreeMap::new(),
        )
        .unwrap();
        for raw in [&first, &begin] {
            let value: Value = serde_json::from_str(raw).unwrap();
            owner.translate(&value).unwrap();
        }
        for _ in 0..fragments {
            let value: Value = serde_json::from_str(&append).unwrap();
            owner.translate(&value).unwrap();
        }
        for raw in [&end, &stop] {
            let value: Value = serde_json::from_str(raw).unwrap();
            owner.translate(&value).unwrap();
        }
        let snapshot = owner.replay_blocks();
        assert_eq!(
            snapshot[0]["input"]["text"].as_str().unwrap().len(),
            fragments
        );
        samples.push(started.elapsed().as_secs_f64());
        retained = owner.charged_bytes();
    }
    println!(
        "{}",
        json!({"fragments":fragments,"events":fragments+4,"samples_seconds":samples,"charged_bytes":retained,"input_bytes":fragments})
    );
}
