use serde_json::{json,Map,Value};
use std::sync::{Arc,mpsc};
use tofu_runtime::prepared_tool_wire::PreparedToolWire;

fn state() -> PreparedToolWire {
    PreparedToolWire::new(json!({"model":"bound","max_tokens":64}).as_object().unwrap().clone(),
        json!({"read":{"name":"read"},"hidden":{"name":"hidden"}}).as_object().unwrap().clone()).unwrap()
}
fn tools(name:&str) -> Value {json!([{"type":"function","function":{"name":name,"parameters":{}}}])}

#[test]
fn first_restored_round_requires_preparation_and_later_projections_retain_generation() {
    let state=state();assert!(state.template().is_err());
    state.install(7,&tools("read")).unwrap();
    {let view=state.template().unwrap();assert_eq!(view["model"],"bound");assert_eq!(view["max_tokens"],64);assert_eq!(view["tools"],tools("read"));}
    assert!(state.install(7,&tools("hidden")).is_err());
    state.install(8,&tools("hidden")).unwrap();
    assert_eq!(state.template().unwrap()["tools"],tools("hidden"));
    state.install(9,&json!([])).unwrap();assert_eq!(state.template().unwrap()["tools"],json!([]));
}

#[test]
fn foreign_or_malformed_tools_do_not_replace_the_valid_projection() {
    let state=state();state.install(0,&tools("read")).unwrap();
    for value in [tools("foreign"),json!([tools("read")[0].clone(),tools("read")[0].clone()]),json!([{"type":"web_search"}]),json!(null)] {
        assert!(state.install(1,&value).is_err());assert_eq!(state.template().unwrap()["tools"],tools("read"));
    }
    let mut base=Map::new();base.insert("tools".into(),json!([]));assert!(PreparedToolWire::new(base,Map::new()).is_err());
}

#[test]
fn counting_and_serialization_guard_blocks_a_concurrent_projection_change() {
    let state=Arc::new(state());state.install(0,&tools("read")).unwrap();
    let guard=state.template().unwrap();let other=state.clone();let(send,receive)=mpsc::channel();
    let thread=std::thread::spawn(move||{other.install(1,&tools("hidden")).unwrap();send.send(()).unwrap();});
    assert_eq!(guard["tools"],tools("read"));assert!(receive.try_recv().is_err());
    drop(guard);receive.recv_timeout(std::time::Duration::from_secs(2)).unwrap();thread.join().unwrap();
    assert_eq!(state.template().unwrap()["tools"],tools("hidden"));
}
