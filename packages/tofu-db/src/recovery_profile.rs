//! One Engine::open diagnostic owner, behind the non-default recovery-profile feature.
//! Engine uses one observer interface; default builds inline a zero-sized no-op,
//! with no environment read, clock, state or Drop. Enabled builds additionally
//! require TOFU_DB_RECOVERY_PROFILE=1. Recovery and failure ownership stay in Engine.

#[derive(Clone, Copy)]
#[repr(usize)]
pub(crate) enum Phase {
    Control,
    WalScan,
    BlockCatalog,
    RecoveryIds,
    PackedLookup,
    HistoryCatalog,
    PayloadReplay,
    WitnessPublish,
    RuntimeInit,
    SealedPrepare,
    SealedEncode,
    SealedPublish,
    Finish,
}
#[derive(Clone, Copy)]
#[repr(usize)]
pub(crate) enum Work {
    WalRecords,
    WalPayloadBytes,
    SealedWalRecords,
    SelectedRecoveryIds,
    PayloadSegments,
    ReplayedTransactions,
}

#[cfg(feature = "recovery-profile")]
pub(crate) use enabled::RecoveryProfile;

#[cfg(feature = "recovery-profile")]
mod enabled {
    use super::{Phase, Work};
    use std::time::Instant;
    const PHASE_NAMES: [&str; 13] = [
        "control",
        "wal_scan",
        "block_catalog",
        "recovery_ids",
        "packed_lookup",
        "history_catalog",
        "payload_replay",
        "witness_publish",
        "runtime_init",
        "sealed_prepare",
        "sealed_encode",
        "sealed_publish",
        "finish",
    ];

    const WORK_NAMES: [&str; 6] = [
        "wal_records",
        "wal_payload_bytes",
        "sealed_wal_records",
        "selected_recovery_ids",
        "payload_segments",
        "replayed_transactions",
    ];

    pub(crate) struct RecoveryProfile(Option<ActiveProfile>);

    struct ActiveProfile {
        started: Instant,
        phase_started: Instant,
        phase: Phase,
        elapsed_nanos: [u64; PHASE_NAMES.len()],
        entered: [bool; PHASE_NAMES.len()],
        work: [Option<u64>; WORK_NAMES.len()],
        finished: Option<Instant>,
    }

    impl ActiveProfile {
        fn new(now: Instant) -> Self {
            let mut entered = [false; PHASE_NAMES.len()];
            entered[Phase::Control as usize] = true;
            Self {
                started: now,
                phase_started: now,
                phase: Phase::Control,
                elapsed_nanos: [0; PHASE_NAMES.len()],
                entered,
                work: [None; WORK_NAMES.len()],
                finished: None,
            }
        }

        fn charge_until(&mut self, now: Instant) {
            let nanos = u64::try_from(now.duration_since(self.phase_started).as_nanos())
                .unwrap_or(u64::MAX);
            let slot = &mut self.elapsed_nanos[self.phase as usize];
            *slot = slot.saturating_add(nanos);
            self.phase_started = now;
        }

        fn advance_at(&mut self, phase: Phase, now: Instant) {
            self.charge_until(now);
            self.phase = phase;
            self.entered[phase as usize] = true;
        }

        fn report(&mut self, now: Instant) -> serde_json::Value {
            self.charge_until(self.finished.unwrap_or(now));
            serde_json::json!({
                "contract": "tofudb.recovery-profile/v1",
                "completed": self.finished.is_some(),
                "elapsedNanos": self.phase_started.duration_since(self.started).as_nanos(),
                "phases": PHASE_NAMES.iter().enumerate().map(|(index, phase)| {
                    serde_json::json!({"phase": phase, "entered": self.entered[index],
                        "elapsedNanos": self.elapsed_nanos[index]})
                }).collect::<Vec<_>>(),
                "work": WORK_NAMES.iter().enumerate().map(|(index, name)| {
                    ((*name).to_owned(), self.work[index].map_or(serde_json::Value::Null, serde_json::Value::from))
                }).collect::<serde_json::Map<_, _>>(),
                "scope": "one Engine::open; sequential non-overlapping elapsed phases, not CPU; excludes report emission; incomplete opens include unwinding to this owner",
            })
        }
    }

    impl RecoveryProfile {
        pub(crate) fn from_environment() -> Self {
            let enabled =
                std::env::var_os("TOFU_DB_RECOVERY_PROFILE").is_some_and(|value| value == "1");
            Self(enabled.then(|| ActiveProfile::new(Instant::now())))
        }

        pub(crate) fn enabled(&self) -> bool {
            self.0.is_some()
        }

        pub(crate) fn advance(&mut self, phase: Phase) {
            if let Some(active) = &mut self.0 {
                active.advance_at(phase, Instant::now());
            }
        }

        pub(crate) fn set_work(&mut self, work: Work, value: u64) {
            if let Some(active) = &mut self.0 {
                active.work[work as usize] = Some(value);
            }
        }

        pub(crate) fn complete(&mut self) {
            if let Some(active) = &mut self.0 {
                active.finished = Some(Instant::now());
            }
        }
    }

    impl Drop for RecoveryProfile {
        fn drop(&mut self) {
            if let Some(active) = &mut self.0 {
                let now = active.finished.unwrap_or_else(Instant::now);
                crate::engine_log::info("recovery_profile", &active.report(now).to_string());
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::time::Duration;

        #[test]
        fn sequential_phases_reconcile_exactly_and_exclude_emission() {
            let start = Instant::now();
            let mut active = ActiveProfile::new(start);
            active.advance_at(Phase::WalScan, start + Duration::from_nanos(11));
            active.advance_at(Phase::PayloadReplay, start + Duration::from_nanos(30));
            active.work[Work::WalRecords as usize] = Some(7);
            active.finished = Some(start + Duration::from_nanos(80));
            let report = active.report(start + Duration::from_secs(10));
            let phases = report["phases"].as_array().unwrap();
            assert_eq!(report["elapsedNanos"], 80);
            assert_eq!(report["completed"], true);
            assert_eq!(
                phases
                    .iter()
                    .map(|phase| phase["elapsedNanos"].as_u64().unwrap())
                    .sum::<u64>(),
                80
            );
            assert_eq!(phases[Phase::WalScan as usize]["elapsedNanos"], 19);
            assert_eq!(phases[Phase::SealedEncode as usize]["entered"], false);
            assert_eq!(report["work"]["wal_records"], 7);
            assert_eq!(active.report(start + Duration::from_secs(20)), report);
        }

        #[test]
        fn incomplete_open_keeps_the_failing_phase_and_completed_work() {
            let start = Instant::now();
            let mut active = ActiveProfile::new(start);
            active.advance_at(Phase::PackedLookup, start + Duration::from_nanos(9));
            let report = active.report(start + Duration::from_nanos(21));
            assert_eq!(report["completed"], false);
            assert_eq!(report["elapsedNanos"], 21);
            assert_eq!(
                report["phases"][Phase::PackedLookup as usize]["elapsedNanos"],
                12
            );
            assert_eq!(
                report["phases"][Phase::PayloadReplay as usize]["entered"],
                false
            );
        }

        #[test]
        fn disabled_profile_has_no_owner_or_dynamic_residency() {
            let mut profile = RecoveryProfile(None);
            assert!(!profile.enabled());
            profile.advance(Phase::PayloadReplay);
            profile.set_work(Work::WalRecords, 9);
            profile.complete();
            assert!(profile.0.is_none());
            assert!(std::mem::size_of::<RecoveryProfile>() <= 512);
            println!(
                "recovery_profile_stack_bytes={}",
                std::mem::size_of::<RecoveryProfile>()
            );
        }
    }
}

#[cfg(not(feature = "recovery-profile"))]
pub(crate) struct RecoveryProfile;

#[cfg(not(feature = "recovery-profile"))]
impl RecoveryProfile {
    #[inline(always)]
    pub(crate) fn from_environment() -> Self {
        Self
    }
    #[inline(always)]
    pub(crate) fn enabled(&self) -> bool {
        false
    }
    #[inline(always)]
    pub(crate) fn advance(&mut self, _phase: Phase) {}
    #[inline(always)]
    pub(crate) fn set_work(&mut self, _work: Work, _value: u64) {}
    #[inline(always)]
    pub(crate) fn complete(&mut self) {}
}

#[cfg(all(test, not(feature = "recovery-profile")))]
mod tests {
    use super::*;
    #[test]
    fn recovery_profile_default_build_has_no_state_or_drop() {
        assert_eq!(std::mem::size_of::<RecoveryProfile>(), 0);
        assert!(!std::mem::needs_drop::<RecoveryProfile>());
        let mut observer = RecoveryProfile::from_environment();
        assert!(!observer.enabled());
        observer.advance(Phase::PayloadReplay);
        observer.set_work(Work::ReplayedTransactions, 9);
        observer.complete();
    }
}
