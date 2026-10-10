//! Post-tool progress control for native agent rounds. Trusted composition owns
//! world/result/verification facts; this module owns run-local consecutive state.
//! Missing visible-result evidence resets the stall streak. No prose, durable
//! checkpoint, routing authority or retry is inferred from these observations.
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

#[derive(Clone, Copy, Default)]
pub struct Limits {
    pub no_progress_rounds: usize,
    pub nonretryable_failure_rounds: usize,
}
#[derive(Default)]
pub struct Observation {
    pub world_version: String,
    pub evidence_ids: Vec<String>,
    pub evidence_complete: bool,
    pub verification: String,
    /// Supply signatures only when every executed call failed non-retryably.
    /// Mixed, successful and unknown rounds supply an empty vector.
    pub nonretryable_failure_signatures: Vec<String>,
}
impl Observation {
    pub fn fits_bytes(&self, limit: usize) -> bool {
        std::iter::once(self.world_version.as_str())
            .chain(std::iter::once(self.verification.as_str()))
            .chain(self.evidence_ids.iter().map(String::as_str))
            .chain(
                self.nonretryable_failure_signatures
                    .iter()
                    .map(String::as_str),
            )
            .try_fold(0usize, |total, value| total.checked_add(value.len().max(1)))
            .is_some_and(|total| total <= limit)
    }
}
/// Calls have already passed the native complete-batch argument preflight.
/// Ignore response-local call IDs and caller metadata, as the Python ledger does.
pub fn call_signature(
    name: &str,
    arguments: &serde_json::Value,
) -> Result<[u8; 32], crate::tool_policy::FloodError> {
    crate::tool_policy::approval_fingerprint(name, arguments)
}
pub struct Context<'a> {
    pub owner: u64,
    pub task_id: &'a str,
    pub round: usize,
    pub tool_calls: usize,
    pub messages: &'a [serde_json::Value],
    pub session: &'a crate::session::ExecutionSession,
}
pub trait Policy {
    fn limits(&self) -> Limits;
    fn observe(&self, context: Context<'_>) -> Result<Observation, serde_json::Value>;
}
#[derive(Debug, PartialEq, Eq)]
pub enum Halt {
    NoProgress,
    NonretryableFailure,
}
#[derive(Default)]
pub struct Ledger {
    last_calls: Option<[u8; 32]>,
    last_world: String,
    last_evidence: Option<[u8; 32]>,
    last_failure: Option<[u8; 32]>,
    pub no_progress_streak: usize,
    pub nonretryable_failure_streak: usize,
}
fn set_signature<'a>(values: impl Iterator<Item = &'a str>) -> [u8; 32] {
    let values: BTreeSet<_> = values.filter(|s| !s.is_empty()).collect();
    let mut hash = Sha256::new();
    for value in values {
        hash.update((value.len() as u64).to_le_bytes());
        hash.update(value.as_bytes());
    }
    hash.finalize().into()
}
impl Ledger {
    pub fn snapshot(&self) -> serde_json::Value {
        let hex = |value: Option<[u8; 32]>| {
            value.map(|bytes| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>())
        };
        serde_json::json!({"codec":"sha256-token-sequence/v1","last_calls":hex(self.last_calls),"last_world":self.last_world,
            "last_evidence":hex(self.last_evidence),"last_failure":hex(self.last_failure),
            "no_progress_streak":self.no_progress_streak,"failure_streak":self.nonretryable_failure_streak})
    }
    pub fn restore(value: &serde_json::Value, world_bytes: usize, limits: Limits) -> Option<Self> {
        let object = value.as_object()?;
        let keys = [
            "codec",
            "last_calls",
            "last_world",
            "last_evidence",
            "last_failure",
            "no_progress_streak",
            "failure_streak",
        ];
        if object.len() != keys.len()
            || keys.iter().any(|key| !object.contains_key(*key))
            || value["codec"] != "sha256-token-sequence/v1"
        {
            return None;
        }
        let digest = |key: &str| -> Option<Option<[u8; 32]>> {
            if value[key].is_null() {
                return Some(None);
            }
            let text = value[key].as_str()?;
            if text.len() != 64
                || !text
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return None;
            }
            let mut bytes = [0; 32];
            for (index, chunk) in text.as_bytes().chunks(2).enumerate() {
                bytes[index] = u8::from_str_radix(std::str::from_utf8(chunk).ok()?, 16).ok()?;
            }
            Some(Some(bytes))
        };
        let last_calls = digest("last_calls")?;
        let last_evidence = digest("last_evidence")?;
        let last_failure = digest("last_failure")?;
        let world = value["last_world"].as_str()?;
        if world.len() > world_bytes {
            return None;
        }
        let no_progress_streak = usize::try_from(value["no_progress_streak"].as_u64()?).ok()?;
        let failure_streak = usize::try_from(value["failure_streak"].as_u64()?).ok()?;
        if (last_calls.is_none() && (last_evidence.is_some() || !world.is_empty()))
            || (last_failure.is_some() && failure_streak == 0)
            || (no_progress_streak > 0 && (last_calls.is_none() || last_evidence.is_none()))
            || (failure_streak > 0 && last_failure.is_none())
            || (limits.no_progress_rounds == 0
                && (last_calls.is_some()
                    || last_evidence.is_some()
                    || !world.is_empty()
                    || no_progress_streak != 0))
            || (limits.nonretryable_failure_rounds == 0
                && (last_failure.is_some() || failure_streak != 0))
            || (limits.no_progress_rounds > 0 && no_progress_streak >= limits.no_progress_rounds)
            || (limits.nonretryable_failure_rounds > 0
                && failure_streak >= limits.nonretryable_failure_rounds)
        {
            return None;
        }
        Some(Self {
            last_calls,
            last_world: world.into(),
            last_evidence,
            last_failure,
            no_progress_streak,
            nonretryable_failure_streak: failure_streak,
        })
    }
    /// Keep signatures plus the last world version, never historical results.
    pub fn observe(
        &mut self,
        calls: &[[u8; 32]],
        observation: Observation,
        limits: Limits,
    ) -> Option<Halt> {
        if limits.no_progress_rounds > 0 {
            let mut hash = Sha256::new();
            for call in calls {
                hash.update(call);
            }
            let signature: [u8; 32] = hash.finalize().into();
            let evidence = observation
                .evidence_complete
                .then(|| set_signature(observation.evidence_ids.iter().map(String::as_str)));
            let verified = matches!(
                observation.verification.to_lowercase().as_str(),
                "passed" | "success" | "verified" | "ok"
            );
            let stalled = self.last_calls == Some(signature)
                && self.last_world == observation.world_version
                && evidence.is_some()
                && evidence == self.last_evidence
                && !verified;
            self.no_progress_streak = if stalled {
                self.no_progress_streak.saturating_add(1)
            } else {
                0
            };
            self.last_calls = Some(signature);
            if self.last_world != observation.world_version {
                // Do not retain a caller's unused oversized String allocation.
                self.last_world = observation.world_version.into_boxed_str().into_string();
            }
            self.last_evidence = evidence;
            if self.no_progress_streak >= limits.no_progress_rounds {
                return Some(Halt::NoProgress);
            }
        }
        if limits.nonretryable_failure_rounds > 0 {
            let normalized: Vec<_> = observation
                .nonretryable_failure_signatures
                .iter()
                .map(|s| crate::tool_policy::python_trim(s))
                .filter(|s| !s.is_empty())
                .collect();
            let signature = (!normalized.is_empty()).then(|| set_signature(normalized.into_iter()));
            self.nonretryable_failure_streak = match signature {
                None => 0,
                Some(_) if signature == self.last_failure => {
                    self.nonretryable_failure_streak.saturating_add(1)
                }
                Some(_) => 1,
            };
            self.last_failure = signature;
            if self.nonretryable_failure_streak >= limits.nonretryable_failure_rounds {
                return Some(Halt::NonretryableFailure);
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn latest_world_retention_discards_unused_caller_capacity() {
        let mut world = String::with_capacity(1024 * 1024);
        world.push('v');
        let mut ledger = Ledger::default();
        ledger.observe(
            &[[1; 32]],
            Observation {
                world_version: world,
                ..Default::default()
            },
            Limits {
                no_progress_rounds: 2,
                ..Default::default()
            },
        );
        assert_eq!(ledger.last_world, "v");
        assert_eq!(ledger.last_world.capacity(), ledger.last_world.len());
    }
}
