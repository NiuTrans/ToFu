//! Current Python conversion parity, including exact Unicode aliases and replay.
use serde_json::{json, Value};
use tofu_runtime::responses_request::{aliases, Body, Profile};

#[test]
fn canonical_python_requests_match() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_responses_request.json")).unwrap();
    for case in cases.as_array().unwrap() {
        let mut template = case["body"].as_object().unwrap().clone();
        let messages = template.remove("messages").unwrap();
        let reverse = aliases(&template, messages.as_array().unwrap()).unwrap();
        assert_eq!(
            serde_json::to_value(&reverse).unwrap(),
            case["reverse"],
            "{}",
            case["name"]
        );
        let body = Body {
            profile: Profile::parse(case["profile"].as_str().unwrap_or("compatible")).unwrap(),
            template: &template,
            messages: messages.as_array().unwrap(),
            output: None,
            replay: case["replay"].as_bool().unwrap(),
            reverse: &reverse,
        };
        assert_eq!(
            serde_json::to_value(body).unwrap(),
            case["output"],
            "{}",
            case["name"]
        );
    }
}

#[test]
fn unsupported_profiles_and_conflicting_policy_refuse() {
    for template in [
        json!({"model":"fixture","stop":["stop"]}),
        json!({"model":"fixture","reasoning_effort":"high","reasoning":{"effort":"low"}}),
        json!({"model":"fixture","reasoning":{"context":"all_turns"}}),
        json!({"model":"fixture","output_config":{"effort":"high"}}),
        json!({"model":"fixture","tools":[{"type":"web_search"}]}),
    ] {
        let reverse = Default::default();
        assert!(serde_json::to_vec(&Body {
            profile: Profile::Compatible,
            template: template.as_object().unwrap(),
            messages: &[],
            output: None,
            replay: false,
            reverse: &reverse
        })
        .is_err());
    }
    let template = json!({"model":"fixture"});
    for messages in [
        json!([{"role":"assistant","content":"", "_responses_items":[{"type":"reasoning","encrypted_content":"opaque"}]}]),
        json!([{"role":"assistant","content":"", "_responses_items":[{"type":"program_output","output":"result"}]}]),
        json!([{"role":"tool","tool_call_id":"call","content":"result","caller":{"type":"program"}}]),
    ] {
        assert!(serde_json::to_vec(&Body {
            profile: Profile::Compatible,
            template: template.as_object().unwrap(),
            messages: messages.as_array().unwrap(),
            output: None,
            replay: false,
            reverse: &Default::default()
        })
        .is_err());
    }
}

#[test]
fn dialect_validation_keeps_local_output_admission_and_explicit_policy() {
    assert!(Profile::parse("unknown").is_err());
    let template = json!({"model":"fixture","max_tokens":0});
    assert!(serde_json::to_vec(&Body { profile:Profile::Codex, template:template.as_object().unwrap(), messages:&[], output:Some(7), replay:false, reverse:&Default::default() }).is_err());
    println!("profile_bytes={} previous_policy_bytes={} profiled_policy_bytes={}",std::mem::size_of::<Profile>(),std::mem::size_of::<(tofu_runtime::responses::Budget,bool)>(),std::mem::size_of::<(tofu_runtime::responses::Budget,bool,Profile)>());
}

#[test]
fn cache_key_is_bounded_and_only_admitted_for_explicit_codex_dialect() {
    for (profile,key) in [(Profile::Compatible,json!("key")),(Profile::Codex,json!("")),(Profile::Codex,json!(true)),(Profile::Codex,json!("x".repeat(tofu_runtime::execution_contract::MODEL_CACHE_AFFINITY_KEY_BYTES+1)))] {
        let template=json!({"model":"fixture","max_tokens":100,"prompt_cache_key":key});
        assert!(serde_json::to_vec(&Body{profile,template:template.as_object().unwrap(),messages:&[],output:None,replay:false,reverse:&Default::default()}).is_err());
    }
}
