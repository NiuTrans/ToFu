//! Exact receipt binding, revocation, expiry and bounded native dispatch.
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};
use tofu_runtime::{
    approval::{Authority, Budget, Store},
    session::Registry as Sessions,
    tool_contract::*,
    tool_execution::Scope,
};
struct Allow;
impl Authorization for Allow {
    fn allow(&self, _: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        true
    }
}
struct Count(AtomicUsize);
impl Handler for Count {
    fn invoke(&self, _: InvocationContext<'_>, a: &Value) -> Result<Value, Value> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(a.clone())
    }
}
fn catalog() -> Catalog {
    Catalog::compile(7,"epoch",&json!({"work":{"name":"work","contractVersion":"tofu.tool-contract/v2","permission":"approval","arguments_schema":{"type":"object"}}})).unwrap()
}
fn call(c: &Catalog, x: u64) -> AdmittedCall {
    c.admit(7, "work", json!({"x":x}), &Value::Null, &Allow)
        .unwrap()
}
#[test]
fn exact_receipt_executes_once_and_boolean_permission_never_substitutes() {
    let sessions = Sessions::new();
    let parent = sessions.start("task", "task", 7, "", None).unwrap();
    let store = Store::new(
        &parent,
        7,
        "task",
        "epoch",
        Budget {
            receipts: 2,
            identity_bytes: 4096,
        },
        Arc::new(|| Duration::ZERO),
    )
    .unwrap();
    let c = catalog();
    let handler = Arc::new(Count(AtomicUsize::new(0)));
    let registry = Registry::new(7, "epoch", vec![("work".into(), handler.clone())]).unwrap();
    let dispatch = |proof| {
        let scope =
            Scope::with_parent(&sessions, 7, "tool", "", None, parent.clone(), "task").unwrap();
        registry.dispatch_scoped(proof, &Allow, &scope).unwrap()
    };
    assert!(matches!(
        dispatch(call(&c, 1)).error,
        Some(DispatchError::ApprovalRequired)
    ));
    let proof = call(&c, 1);
    let ticket = store.mint("id", &proof, Authority::Human).unwrap();
    assert!(call(&c, 2).with_approval("id", ticket).is_err());
    let proof = call(&c, 1);
    let ticket = store.mint("id", &proof, Authority::Human).unwrap();
    assert_eq!(
        dispatch(proof.with_approval("id", ticket).unwrap()).value,
        Some(json!({"x":1}))
    );
    assert_eq!(store.retained().unwrap(), (0, 0));
    assert_eq!(handler.0.load(Ordering::SeqCst), 1);
    assert!(matches!(
        dispatch(call(&c, 1)).error,
        Some(DispatchError::ApprovalRequired)
    ));
}
#[test]
fn fifo_replacement_revoke_expiry_and_backwards_clock_fail_closed() {
    let sessions = Sessions::new();
    let parent = sessions.start("task", "task", 7, "", None).unwrap();
    let now = Arc::new(AtomicU64::new(10));
    let clock = now.clone();
    let store = Store::new(
        &parent,
        7,
        "task",
        "epoch",
        Budget {
            receipts: 2,
            identity_bytes: 4096,
        },
        Arc::new(move || Duration::from_secs(clock.load(Ordering::SeqCst))),
    )
    .unwrap();
    let c = catalog();
    let first = store.mint("a", &call(&c, 1), Authority::Human).unwrap();
    let _b = store.mint("b", &call(&c, 1), Authority::Human).unwrap();
    let _c = store.mint("c", &call(&c, 1), Authority::Human).unwrap();
    assert!(call(&c, 1).with_approval("a", first).is_err());
    assert_eq!(store.retained().unwrap().0, 2);
    let old = store.mint("c", &call(&c, 1), Authority::Human).unwrap();
    let replacement = store.mint("c", &call(&c, 2), Authority::Human).unwrap();
    assert!(call(&c, 1).with_approval("c", old).is_err());
    assert!(store.revoke("c").unwrap());
    assert!(call(&c, 2).with_approval("c", replacement).is_err());
    now.store(310, Ordering::SeqCst);
    assert_eq!(store.retained().unwrap().0, 1);
    now.store(311, Ordering::SeqCst);
    assert_eq!(store.retained().unwrap(), (0, 0));
    let _ = store.mint("d", &call(&c, 1), Authority::Human).unwrap();
    now.store(309, Ordering::SeqCst);
    assert_eq!(store.retained().unwrap(), (0, 0));
    for i in 0..10000 {
        let _ = store
            .mint(&format!("id-{i}"), &call(&c, 1), Authority::Human)
            .unwrap();
        let (n, bytes) = store.retained().unwrap();
        assert!(n <= 2 && bytes <= 4096);
    }
    store.close().unwrap();
    assert_eq!(store.retained().unwrap(), (0, 0));
    assert!(store
        .mint("closed", &call(&c, 1), Authority::Human)
        .is_err());
}
#[test]
fn foreign_task_and_parent_cancel_never_execute_and_store_does_not_pin_parent() {
    let sessions = Sessions::new();
    let parent = sessions.start("task", "task", 7, "", None).unwrap();
    let foreign = sessions.start("other", "task", 7, "", None).unwrap();
    let store = Store::new(
        &parent,
        7,
        "task",
        "epoch",
        Budget {
            receipts: 2,
            identity_bytes: 4096,
        },
        Arc::new(|| Duration::ZERO),
    )
    .unwrap();
    let c = catalog();
    let handler = Arc::new(Count(AtomicUsize::new(0)));
    let registry = Registry::new(7, "epoch", vec![("work".into(), handler.clone())]).unwrap();
    let proof = call(&c, 1);
    let ticket = store.mint("id", &proof, Authority::Human).unwrap();
    let scope =
        Scope::with_parent(&sessions, 7, "tool", "", None, foreign.clone(), "other").unwrap();
    assert!(matches!(
        registry
            .dispatch_scoped(proof.with_approval("id", ticket).unwrap(), &Allow, &scope)
            .unwrap()
            .error,
        Some(DispatchError::ApprovalRequired)
    ));
    drop(scope);
    let proof = call(&c, 1);
    let ticket = store.mint("id", &proof, Authority::Human).unwrap();
    parent.request_cancel("stop").unwrap();
    let scope =
        Scope::with_parent(&sessions, 7, "tool2", "", None, parent.clone(), "task").unwrap();
    let outcome = registry
        .dispatch_scoped(proof.with_approval("id", ticket).unwrap(), &Allow, &scope)
        .unwrap();
    assert!(outcome.error.is_some());
    assert_eq!(handler.0.load(Ordering::SeqCst), 0);
    drop(scope);
    assert_eq!(Arc::strong_count(&parent), 1);
    drop(parent);
    assert!(store.mint("id", &call(&c, 1), Authority::Human).is_err());
}

#[test]
fn expired_or_revoked_attached_receipts_are_rechecked_at_dispatch() {
    for revoke in [false, true] {
        let sessions = Sessions::new();
        let parent = sessions.start("task", "task", 7, "", None).unwrap();
        let now = Arc::new(AtomicU64::new(0));
        let clock = now.clone();
        let store = Store::new(
            &parent,
            7,
            "task",
            "epoch",
            Budget {
                receipts: 2,
                identity_bytes: 4096,
            },
            Arc::new(move || Duration::from_secs(clock.load(Ordering::SeqCst))),
        )
        .unwrap();
        let c = catalog();
        let proof = call(&c, 1);
        let ticket = store.mint("id", &proof, Authority::Human).unwrap();
        let proof = proof.with_approval("id", ticket).unwrap();
        if revoke {
            store.revoke("id").unwrap();
        } else {
            now.store(301, Ordering::SeqCst);
        }
        let handler = Arc::new(Count(AtomicUsize::new(0)));
        let registry = Registry::new(7, "epoch", vec![("work".into(), handler.clone())]).unwrap();
        let scope =
            Scope::with_parent(&sessions, 7, "tool", "", None, parent.clone(), "task").unwrap();
        assert!(matches!(
            registry
                .dispatch_scoped(proof, &Allow, &scope)
                .unwrap()
                .error,
            Some(DispatchError::ApprovalRequired)
        ));
        assert_eq!(handler.0.load(Ordering::SeqCst), 0);
        assert_eq!(store.retained().unwrap(), (0, 0));
    }
}
