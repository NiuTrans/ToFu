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
