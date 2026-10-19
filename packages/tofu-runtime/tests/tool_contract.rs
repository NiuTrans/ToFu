//! Actual Python validation/errors and independent final dispatch authorization.
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc,
};
use tofu_runtime::tool_contract::*;
fn document(permission: &str) -> Value {
    json!({"lookup":{"name":"lookup","contractVersion":"tofu.tool-contract/v2","arguments_schema":{"type":"object","properties":{"x":{"type":"integer","default":1}},"additionalProperties":false},"permission":permission}})
}
#[test]
fn actual_python_validation_preserves_defaults_priority_and_error_payloads() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/python_tool_contract.json")).unwrap();
    for (case, c) in cases.as_array().unwrap().iter().enumerate() {
        let catalog = Catalog::compile(7, "epoch", &json!({"lookup":c["document"]})).unwrap();
        let result = catalog.validate(7, "lookup", c["value"].clone());
        match result {
            Ok(value) => {
                assert!(c["error"].is_null(), "case {case}");
                assert_eq!(value, c["result"], "case {case}")
            }
            Err(AdmissionError::Validation(e)) => {
                assert_eq!(e.to_value(), c["error"], "case {case}")
            }
            other => panic!("unexpected case {case}: {other:?}"),
        }
    }
}
struct Policy(AtomicBool);
impl Authorization for Policy {
    fn allow(
        &self,
        owner: u64,
        name: &str,
        permission: Permission,
        _: &Value,
        _: Option<&Value>,
    ) -> bool {
        owner == 7
            && name == "lookup"
            && permission == Permission::Read
            && self.0.load(Ordering::SeqCst)
    }
}
struct Counter(AtomicUsize);
impl Handler for Counter {
    fn invoke(&self, context: InvocationContext<'_>, args: &Value) -> Result<Value, Value> {
        assert_eq!(context.owner, 7);
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(args.clone())
    }
}
#[test]
fn schema_success_never_grants_authority_and_dispatch_rechecks_revocation() {
    let c = Catalog::compile(7, "epoch", &document("read")).unwrap();
    assert!(matches!(
        c.admit(7, "lookup", json!({}), &Value::Null, &DenyAll),
        Err(AdmissionError::Denied)
    ));
    let policy = Policy(AtomicBool::new(true));
    let handler = Arc::new(Counter(AtomicUsize::new(0)));
    let registry = Registry::new(7, "epoch", vec![("lookup".into(), handler.clone())]).unwrap();
    let call = c
        .admit(7, "lookup", json!({}), &Value::Null, &policy)
        .unwrap();
    policy.0.store(false, Ordering::SeqCst);
    assert!(matches!(
        registry.dispatch(call, &policy),
        Err(DispatchError::Denied)
    ));
    assert_eq!(handler.0.load(Ordering::SeqCst), 0);
    policy.0.store(true, Ordering::SeqCst);
    let call = c
        .admit(7, "lookup", json!({}), &Value::Null, &policy)
        .unwrap();
    assert_eq!(registry.dispatch(call, &policy).unwrap(), json!({"x":1}));
    assert_eq!(handler.0.load(Ordering::SeqCst), 1);
}
#[test]
fn owner_epoch_caller_and_missing_permission_fail_closed() {
    let c = Catalog::compile(7, "epoch", &document("read")).unwrap();
    let p = Policy(AtomicBool::new(true));
    assert!(matches!(
        c.validate(8, "lookup", json!({})),
        Err(AdmissionError::WrongOwner)
    ));
    assert!(matches!(
        c.admit(7, "lookup", json!({}), &json!({"type":"program"}), &p),
        Err(AdmissionError::Caller(_))
    ));
    let call = c.admit(7, "lookup", json!({}), &Value::Null, &p).unwrap();
    let registry = Registry::new(7, "different", Vec::new()).unwrap();
    assert!(matches!(
        registry.dispatch(call, &p),
        Err(DispatchError::WrongScope)
    ));
    let mut doc = document("read");
    doc["lookup"].as_object_mut().unwrap().remove("permission");
    let c = Catalog::compile(7, "epoch", &doc).unwrap();
    assert!(c.validate(7, "lookup", json!({})).is_ok());
    assert!(matches!(
        c.admit(7, "lookup", json!({}), &Value::Null, &p),
        Err(AdmissionError::Denied)
    ));
}
#[test]
fn defaults_are_owned_per_call_and_required_null_is_not_repaired() {
    let c = Catalog::compile(7, "epoch", &document("read")).unwrap();
    let mut first = c.validate(7, "lookup", json!({})).unwrap();
    first["x"] = 2.into();
    assert_eq!(c.validate(7, "lookup", json!({})).unwrap(), json!({"x":1}));
    let doc = json!({"lookup":{"name":"lookup","permission":"read","contractVersion":"tofu.tool-contract/v2","arguments_schema":{"type":"object","required":["x"],"properties":{"x":{"type":["integer","null"],"default":1}}}}});
    let c = Catalog::compile(7, "epoch", &doc).unwrap();
    for args in [json!({}), json!({"x":null})] {
        assert!(matches!(
            c.validate(7, "lookup", args),
            Err(AdmissionError::Validation(_))
        ));
    }
}
#[test]
fn failed_handlers_and_panics_never_become_success_or_automatic_retries() {
    struct Failed;
    impl Handler for Failed {
        fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            Err(json!({"kind":"fixture_failure"}))
        }
    }
    struct Panicked;
    impl Handler for Panicked {
        fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            panic!("fixture panic")
        }
    }
    let c = Catalog::compile(7, "epoch", &document("read")).unwrap();
    let p = Policy(AtomicBool::new(true));
    for handler in [Arc::new(Failed) as Arc<dyn Handler>, Arc::new(Panicked)] {
        let registry = Registry::new(7, "epoch", vec![("lookup".into(), handler)]).unwrap();
        let call = c.admit(7, "lookup", json!({}), &Value::Null, &p).unwrap();
        assert!(matches!(
            registry.dispatch(call, &p),
            Err(DispatchError::Failed(_) | DispatchError::Panicked)
        ));
    }
}
#[test]
fn authorization_panics_fail_closed_before_any_handler() {
    struct Broken;
    impl Authorization for Broken {
        fn allow(&self, _: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
            panic!("authorization failed")
        }
    }
    let c = Catalog::compile(7, "epoch", &document("read")).unwrap();
    assert!(matches!(
        c.admit(7, "lookup", json!({}), &Value::Null, &Broken),
        Err(AdmissionError::Denied)
    ));
}

#[test]
fn required_nullable_field_accepts_explicit_null_but_not_absence() {
    let catalog=Catalog::compile(7,"epoch",&json!({"limits":{"name":"limits","contractVersion":"tofu.tool-contract/v2","permission":"read",
        "arguments_schema":{"type":"object","required":["rounds"],"properties":{"rounds":{"type":["integer","null"],"minimum":1}}}}})).unwrap();
    assert!(catalog.validate_protocol(7,"limits",json!({"rounds":null})).is_ok());
    assert!(catalog.validate(7,"limits",json!({"rounds":null})).is_err());
    assert!(catalog.validate(7,"limits",json!({"rounds":3})).is_ok());
    assert!(catalog.validate(7,"limits",json!({})).is_err());
    assert!(catalog.validate(7,"limits",json!({"rounds":0})).is_err());
}

#[test]
fn native_configure_deadline_requires_explicit_valid_policy() {
    let document:Value=serde_json::from_str(include_str!("../../../contracts/native_agent_stdio_v1.schema.json")).unwrap();
    let schema=&document["$defs"]["configure"];
    assert!(schema["required"].as_array().unwrap().contains(&json!("deadline_ms")));
    let catalog=Catalog::compile(7,"epoch",&json!({"limits":{"name":"limits","contractVersion":"tofu.tool-contract/v2","permission":"read",
        "arguments_schema":{"type":"object","required":["deadline_ms"],"properties":{"deadline_ms":schema["properties"]["deadline_ms"]}}}})).unwrap();
    for value in [Value::Null,json!(50)] {
        assert!(catalog.validate_protocol(7,"limits",json!({"deadline_ms":value})).is_ok());
    }
    for value in [json!(0),json!(-1),json!(false),json!(1.5)] {
        assert!(catalog.validate_protocol(7,"limits",json!({"deadline_ms":value})).is_err());
    }
    assert!(catalog.validate_protocol(7,"limits",json!({})).is_err());
}

#[test]
fn total_tool_limit_protocol_admits_null_zero_and_positive_only() {
    let document:Value=serde_json::from_str(include_str!("../../../contracts/native_agent_stdio_v1.schema.json")).unwrap();
    let catalog=Catalog::compile(7,"epoch",&json!({"limits":{"name":"limits","contractVersion":"tofu.tool-contract/v2","permission":"read",
        "arguments_schema":{"type":"object","properties":{"total_tool_calls":document["$defs"]["configure"]["properties"]["total_tool_calls"]}}}})).unwrap();
    for value in [Value::Null,json!(0),json!(3)] {
        assert!(catalog.validate_protocol(7,"limits",json!({"total_tool_calls":value})).is_ok());
    }
    for value in [json!(-1),json!(false),json!(1.5)] {
        assert!(catalog.validate_protocol(7,"limits",json!({"total_tool_calls":value})).is_err());
    }
    assert!(catalog.validate_protocol(7,"limits",json!({})).is_ok());
}


#[test]
fn terminal_protocol_validation_preserves_exact_frozen_wire_for_all_outcomes() {
    let contract: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_agent_stdio_v1.schema.json"
    ))
    .unwrap();
    let schema = &contract["$defs"]["terminal_request"];
    let catalog = Catalog::compile(7,"terminal",&json!({"terminal_request":{
        "name":"terminal_request","contractVersion":"tofu.tool-contract/v2","arguments_schema":schema}})).unwrap();
    for status in ["done", "error", "aborted"] {
        for optional in [false, true] {
            let mut event = json!({"status":status,"type":status});
            if status == "done" {
                event["result"] = json!({"messages":[{"role":"assistant","content":"unicode 中\nquoted \""}],"usage":[{"input_tokens":u64::MAX,"fraction":2.5}]});
            } else {
                event["execution"] = json!({"messages":[{"role":"user","content":"question"}],"usage":[{"input_tokens":1}]});
                if status == "error" {
                    event["error"] = json!({"kind":"fixture","message":"failed"});
                }
            }
            let mut request = json!({"version":"tofu.native-agent-stdio/v1","type":"terminal_request",
                "request_id":"commit","owner_user_id":7,"task_id":"task","attempt":1,
                "event":event,"receipt":{"execution_id":"task","invariants_satisfied":true}});
            if optional {
                request["checkpoint_suspended"] = json!(status == "aborted");
                if status == "done" {
                    request["usage_summary"] = json!({"profile":"tofu.additive-usage/u64-f64-v1","counts":{"input_tokens":u64::MAX}});
                }
            }
            let original = serde_json::to_vec(&request).unwrap();
            let normalized = catalog.validate(7, "terminal_request", request).unwrap();
            assert_eq!(
                serde_json::to_vec(&normalized).unwrap(),
                original,
                "{status} {optional}"
            );
            let mut invalid = normalized.clone();
            invalid["owner_user_id"] = json!(0);
            assert!(catalog.validate(7, "terminal_request", invalid).is_err());
            assert!(catalog.validate(8, "terminal_request", normalized).is_err());
        }
    }
}
