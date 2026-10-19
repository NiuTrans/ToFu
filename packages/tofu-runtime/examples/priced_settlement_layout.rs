//! Layout-only diagnostic, not RSS or throughput. Baseline fields are the exact
//! pre-pricing StreamResult shape; no runtime changes or provider calls here.
use serde_json::Value;
use tofu_runtime::{execution_contract::ProviderStreamState,provider::StreamResult};
#[allow(dead_code)]
struct BaselineStreamResult {
    state:ProviderStreamState,
    message:Value,
    usage:Value,
    finish_reason:Option<String>,
    done_seen:bool,
    malformed_frames:u64,
}
fn main() {
    println!("{}",serde_json::json!({"baseline_stream_result_bytes":std::mem::size_of::<BaselineStreamResult>(),"priced_stream_result_bytes":std::mem::size_of::<StreamResult>(),"optional_box_bytes":std::mem::size_of::<Option<Box<Value>>>()}));
}
