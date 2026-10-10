//! Differential lifecycle cases derived from the canonical execution owner.
use std::sync::{
    atomic::{AtomicU64, AtomicUsize, Ordering},
    Arc, Mutex,
};
use std::time::Duration;
use tofu_runtime::execution_contract::{
    ExecutionPhase as Phase, ResourceDisposition as Disposition, ResourceName as Name,
};
use tofu_runtime::session::{Registry, ReleaseAck, ReleaseError, SessionError};

#[test]
fn observed_deadline_does_not_refund_preparation_and_expiry_still_settles() {
    let registry = Registry::new();
    let observed = std::time::Instant::now();
    std::thread::sleep(Duration::from_millis(25));
    let session = registry.start_observed("observed", "task", 7, "",
        Duration::from_secs(1), observed).unwrap();
    assert!(session.snapshot().unwrap().deadline_remaining_nanos.unwrap() < 980_000_000);
    let expired = registry.start_observed("expired", "task", 7, "",
        Duration::ZERO, observed).unwrap();
    assert!(expired.snapshot().unwrap().cancel_requested);
    let receipt = expired.settle(Phase::Cancelled, "expired").unwrap();
    assert!(receipt.invariants_satisfied);
    assert_eq!(receipt.outcome, Phase::Cancelled);
}

#[test]
fn phases_and_terminal_receipt_are_monotonic_and_idempotent() {
    let registry = Registry::new();
    let session = registry
        .start("phases", "task", 7, "request", None)
        .unwrap();
    for phase in [
        Phase::Routed,
        Phase::Reserved,
        Phase::Admitted,
        Phase::Dispatching,
    ] {
        assert!(session.advance(phase).unwrap());
    }
    assert_eq!(
        session.advance(Phase::Routed),
        Err(SessionError::RegressedPhase)
    );
    let receipt = session.settle(Phase::Completed, "ok").unwrap();
    assert!(receipt.invariants_satisfied);
    assert!(Arc::ptr_eq(
        &receipt,
        &session.settle(Phase::Failed, "late").unwrap()
    ));
    assert!(!session.advance(Phase::Dispatching).unwrap());
    assert!(registry.snapshots(256).unwrap().is_empty());
}

#[test]
fn concurrent_settlement_runs_every_release_exactly_once() {
    let registry = Registry::new();
    let session = registry.start("race", "task", 8, "", None).unwrap();
    let count = Arc::new(AtomicUsize::new(0));
    let capture = Arc::clone(&count);
    session
        .hold(
            Name::Route,
            0,
            false,
            Box::new(move |_| {
                capture.fetch_add(1, Ordering::SeqCst);
                Ok(ReleaseAck::Released)
            }),
        )
        .unwrap();
    let threads: Vec<_> = (0..8)
        .map(|_| {
            let session = Arc::clone(&session);
            std::thread::spawn(move || session.settle(Phase::Completed, "").unwrap())
        })
        .collect();
    let receipts: Vec<_> = threads
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect();
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert!(receipts
        .iter()
        .all(|receipt| Arc::ptr_eq(receipt, &receipts[0])));
}

#[test]
fn cleanup_failure_refuses_success_and_recoverable_failure_defers() {
    for recoverable in [false, true] {
        let registry = Registry::new();
        let session = registry.start("failure", "task", 9, "", None).unwrap();
        session
            .hold(
                Name::Billing,
                300,
                recoverable,
                Box::new(|_| Err(ReleaseError)),
            )
            .unwrap();
        let receipt = session.settle(Phase::Completed, "").unwrap();
        assert_eq!(
            receipt.outcome,
            if recoverable {
                Phase::Completed
            } else {
                Phase::Failed
            }
        );
        assert_eq!(
            receipt.resource_dispositions[0].1,
            if recoverable {
                Disposition::Deferred
            } else {
                Disposition::Failed
            }
        );
        assert_eq!(receipt.requested_outcome, Phase::Completed);
    }
}

#[test]
fn release_order_and_context_match_canonical_binding_rules() {
    let registry = Registry::new();
    let session = registry
        .start("order", "task", 31, "secret-request", None)
        .unwrap();
    let calls = Arc::new(Mutex::new(Vec::new()));
    for (name, order) in [
        (Name::Admission, 100),
        (Name::ModelRoute, 200),
        (Name::Billing, 300),
    ] {
        let calls = Arc::clone(&calls);
        session
            .hold(
                name,
                order,
                true,
                Box::new(move |context| {
                    assert_eq!(context.owner_user_id, 31);
                    assert!(context.dispatch_started);
                    calls.lock().unwrap().push(name);
                    Ok(ReleaseAck::Released)
                }),
            )
            .unwrap();
    }
    session.advance(Phase::Dispatching).unwrap();
    session.settle(Phase::Cancelled, "stop").unwrap();
    assert_eq!(
        *calls.lock().unwrap(),
        vec![Name::Billing, Name::ModelRoute, Name::Admission]
    );
    assert_eq!(session.request_id(), "secret-request");
}

#[test]
fn overdue_requests_cancel_without_releasing_live_resources() {
    let now = Arc::new(AtomicU64::new(0));
    let clock = Arc::clone(&now);
    let registry = Registry::with_clock(Arc::new(move || {
        Duration::from_secs(clock.load(Ordering::SeqCst))
    }));
    let session = registry
        .start("deadline", "task", 7, "", Some(Duration::from_secs(10)))
        .unwrap();
    session
        .hold(
            Name::Route,
            0,
            false,
            Box::new(|_| Ok(ReleaseAck::Released)),
        )
        .unwrap();
    now.store(11, Ordering::SeqCst);
    assert_eq!(registry.reconcile_deadlines().unwrap(), 1);
    assert_eq!(registry.reconcile_deadlines().unwrap(), 0);
    let snapshot = session.snapshot().unwrap();
    assert!(snapshot.cancel_requested);
    assert_eq!(snapshot.held_resources, vec![Name::Route]);
    session
        .settle(Phase::TimedOut, "execution_deadline_exceeded")
        .unwrap();
}

#[test]
fn identity_is_explicit_duplicates_are_fenced_and_terminal_ids_can_reenter() {
    let registry = Registry::new();
    assert!(matches!(
        registry.start("", "task", 7, "", None),
        Err(SessionError::InvalidIdentity)
    ));
    assert!(matches!(
        registry.start("id", "task", 0, "", None),
        Err(SessionError::InvalidIdentity)
    ));
    let first = registry.start("id", "task", 7, "", None).unwrap();
    assert!(matches!(
        registry.start("id", "task", 8, "", None),
        Err(SessionError::DuplicateIdentity)
    ));
    first.settle(Phase::Completed, "").unwrap();
    assert!(registry.start("id", "task", 8, "", None).is_ok());
}

#[test]
fn release_can_inspect_state_and_reentrant_settlement_is_refused() {
    let registry = Registry::new();
    let session = registry.start("inspect", "task", 7, "", None).unwrap();
    let weak = Arc::downgrade(&session);
    session
        .hold(
            Name::Route,
            0,
            false,
            Box::new(move |_| {
                let session = weak.upgrade().unwrap();
                let snapshot = session.snapshot().unwrap();
                assert_eq!(snapshot.phase, Phase::Settling);
                assert_eq!(snapshot.held_resources, vec![Name::Route]);
                assert_eq!(
                    session.settle(Phase::Completed, "").unwrap_err(),
                    SessionError::ReentrantSettlement
                );
                Ok(ReleaseAck::Released)
            }),
        )
        .unwrap();
    assert!(
        session
            .settle(Phase::Completed, "")
            .unwrap()
            .invariants_satisfied
    );
}

#[test]
fn failed_bind_rolls_back_unbound_resources_and_settles_bound_regression() {
    let registry = Registry::new();
    let session = registry.start("bind", "task", 7, "", None).unwrap();
    session.advance(Phase::Dispatching).unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let captured = Arc::clone(&calls);
    assert_eq!(
        session.bind_acquired(
            Name::ModelRoute,
            200,
            false,
            Box::new(move |_| {
                captured.fetch_add(1, Ordering::SeqCst);
                Ok(ReleaseAck::Released)
            }),
            Box::new(|| panic!("bound rollback must not run")),
            Some(Phase::Routed)
        ),
        Err(SessionError::RegressedPhase)
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let rollback = Arc::clone(&calls);
    assert_eq!(
        session.bind_acquired(
            Name::Route,
            0,
            false,
            Box::new(|_| panic!("unbound release must not run")),
            Box::new(move || {
                rollback.fetch_add(1, Ordering::SeqCst);
            }),
            None
        ),
        Err(SessionError::Settling)
    );
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[test]
fn abandoned_native_scope_releases_resources_without_durable_task_authority() {
    let registry = Registry::new();
    let count = Arc::new(AtomicUsize::new(0));
    {
        let session = registry.start("drop", "task", 42, "", None).unwrap();
        let count = Arc::clone(&count);
        session
            .hold(
                Name::Route,
                0,
                false,
                Box::new(move |context| {
                    assert_eq!(context.outcome, Phase::Cancelled);
                    count.fetch_add(1, Ordering::SeqCst);
                    Ok(ReleaseAck::Released)
                }),
            )
            .unwrap();
    }
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert!(registry.snapshots(256).unwrap().is_empty());
}

#[test]
fn terminal_receipts_match_actual_frozen_python_oracle() {
    let mut rows = Vec::new();
    for case in [
        "success",
        "hard_failure",
        "recoverable_failure",
        "explicit_defer",
        "preserved_cause",
        "ordered_release",
    ] {
        let registry = Registry::new();
        let session = registry.start(case, "task", 7, "", None).unwrap();
        match case {
            "success" => {
                session.advance(Phase::Dispatching).unwrap();
            }
            "hard_failure" | "preserved_cause" => session
                .hold(Name::Route, 0, false, Box::new(|_| Err(ReleaseError)))
                .unwrap(),
            "recoverable_failure" => session
                .hold(Name::Billing, 0, true, Box::new(|_| Err(ReleaseError)))
                .unwrap(),
            "explicit_defer" => session
                .hold(
                    Name::Upstream,
                    0,
                    false,
                    Box::new(|_| Ok(ReleaseAck::Deferred)),
                )
                .unwrap(),
            "ordered_release" => {
                for (name, order) in [
                    (Name::Admission, 100),
                    (Name::Route, 200),
                    (Name::Billing, 300),
                ] {
                    session
                        .hold(name, order, false, Box::new(|_| Ok(ReleaseAck::Released)))
                        .unwrap();
                }
            }
            _ => unreachable!(),
        }
        let receipt = session
            .settle(
                Phase::Completed,
                if case == "preserved_cause" {
                    "provider_error"
                } else {
                    ""
                },
            )
            .unwrap();
        let resources = receipt
            .resource_dispositions
            .iter()
            .map(|(name, disposition)| format!("{}={}", name.as_str(), disposition.as_str()))
            .collect::<Vec<_>>()
            .join(",");
        rows.push(format!(
            "{}\t{}\t{}\t{}\t{}\t{}",
            case,
            receipt.requested_outcome.as_str(),
            receipt.outcome.as_str(),
            receipt.invariants_satisfied,
            receipt.cause,
            resources
        ));
    }
    assert_eq!(
        rows.join("\n") + "\n",
        include_str!("fixtures/python_execution_receipts.tsv")
    );
}

#[test]
fn every_live_phase_can_cancel_and_release_exactly_once() {
    for phase in [
        Phase::Created,
        Phase::Routed,
        Phase::Reserved,
        Phase::Admitted,
        Phase::Dispatching,
        Phase::Settling,
    ] {
        let registry = Registry::new();
        let session = registry.start(phase.as_str(), "task", 7, "", None).unwrap();
        let releases = Arc::new(AtomicUsize::new(0));
        for name in [Name::Billing, Name::Route, Name::Admission] {
            let releases = Arc::clone(&releases);
            session
                .hold(
                    name,
                    0,
                    true,
                    Box::new(move |_| {
                        releases.fetch_add(1, Ordering::SeqCst);
                        Ok(ReleaseAck::Released)
                    }),
                )
                .unwrap();
        }
        session.advance(phase).unwrap();
        assert_eq!(
            session
                .settle(Phase::Cancelled, "phase_matrix")
                .unwrap()
                .outcome,
            Phase::Cancelled
        );
        session.settle(Phase::Completed, "late").unwrap();
        assert_eq!(releases.load(Ordering::SeqCst), 3);
    }
}

#[test]
fn panicking_cleanup_does_not_skip_other_resources_or_publish_success() {
    let registry = Registry::new();
    let session = registry
        .start("panic-release", "task", 7, "", None)
        .unwrap();
    let releases = Arc::new(AtomicUsize::new(0));
    let capture = Arc::clone(&releases);
    session
        .hold(
            Name::Billing,
            300,
            false,
            Box::new(|_| panic!("owned release panic")),
        )
        .unwrap();
    session
        .hold(
            Name::Route,
            200,
            false,
            Box::new(move |_| {
                capture.fetch_add(1, Ordering::SeqCst);
                Ok(ReleaseAck::Released)
            }),
        )
        .unwrap();
    let receipt = session.settle(Phase::Completed, "").unwrap();
    assert_eq!(receipt.outcome, Phase::Failed);
    assert_eq!(releases.load(Ordering::SeqCst), 1);
    assert_eq!(receipt.resource_dispositions.len(), 2);
}
