//! Fixed anonymous causes recorded before the semantic I/O mapper loses them.
//! No request logging, paths, IDs, payload retention, timers or heap formatting.
//! The generated storage.v2 declaration owns labels and scratch capacity.
use std::fmt::{self, Write};
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use crate::generated_storage_v2::{NATIVE_RESOURCE_EXHAUSTION_CONTRACT,
    NATIVE_RESOURCE_EXHAUSTION_FORMAT_BYTES, NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES};

const REASONS: usize = NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES.len();
static COUNTERS: [AtomicU64; REASONS] = [const { AtomicU64::new(0) }; REASONS];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Reason {
    ArchiveOwner = 0, ArchiveConversation, ArchiveRestoreBudget, DomainBound,
    AccountingOverflow, ReservationFailure, OsMemoryFailure, Other,
}

struct FixedMessage {
    bytes: [u8; NATIVE_RESOURCE_EXHAUSTION_FORMAT_BYTES],
    len: usize,
}
impl Write for FixedMessage {
    fn write_str(&mut self, value: &str) -> fmt::Result {
        let Some(end) = self.len.checked_add(value.len()) else { return Err(fmt::Error); };
        if end > self.bytes.len() { return Err(fmt::Error); }
        self.bytes[self.len..end].copy_from_slice(value.as_bytes());
        self.len = end;
        Ok(())
    }
}

fn classify(error: &io::Error) -> Reason {
    if error.get_ref().is_some_and(|inner| inner.is::<std::collections::TryReserveError>()) {
        // Stable Rust cannot inspect TryReserveErrorKind: capacity overflow and
        // allocation refusal both remain reservation failure, not host OOM.
        return Reason::ReservationFailure;
    }
    if error.kind() == io::ErrorKind::OutOfMemory && error.raw_os_error().is_some() {
        return Reason::OsMemoryFailure;
    }
    let mut message = FixedMessage { bytes: [0; NATIVE_RESOURCE_EXHAUSTION_FORMAT_BYTES], len: 0 };
    if write!(&mut message, "{error}").is_err() { return Reason::Other; }
    let Ok(text) = std::str::from_utf8(&message.bytes[..message.len]) else { return Reason::Other; };
    match text {
        "raw archive owner capacity is exhausted" => Reason::ArchiveOwner,
        "raw archive conversation capacity is exhausted" => Reason::ArchiveConversation,
        "raw archive snapshot exceeds retained-byte budget" => Reason::ArchiveRestoreBudget,
        "storage.v2 frame byte accounting overflow" | "raw archive byte count overflow"
            | "raw archive metadata byte count overflow" | "daily-cost count overflow" => Reason::AccountingOverflow,
        "storage.v2 streamed request allocation failed" => Reason::ReservationFailure,
        "conversation identity scan exceeds its bound" | "conversation lane count exceeds 256"
            | "turn attempt history exceeds 64 entries" | "attempt Turn directory exceeds 64 KiB"
            | "turn tombstone attempt list exceeds 16 KiB" | "Turn compaction metadata exceeds 100000 rows"
            | "turn response exceeds its bounded streamed payload"
            | "Turn projection patch exceeds its deterministic resource bound"
            | "conversation clone exceeds 8 MiB of Turn projection"
            | "conversation clone exceeds 8 MiB of cloned projection"
            | "visible sync event exceeds 8 MiB" | "related Turn projection exceeds 8 MiB"
            | "turn delete closure exceeds 2000 rows" | "Turn compaction materialization exceeds 64 MiB"
            | "turn delete closure exceeds 256 branch lanes" | "Turn compaction projections exceed 8 MiB"
            | "Turn compaction mutation set exceeds its bound" | "Turn compaction reparenting exceeds 64 MiB"
            | "legacy Turn image response exceeds the streaming bound"
            | "turn delta exceeds 2000 changed rows" | "turn delta exceeds its bounded streamed payload"
            | "turn delta exceeds 2000 tombstones" | "daily-cost document exceeds its bound"
            | "daily-cost month response exceeds 8 MiB" => Reason::DomainBound,
        _ => Reason::Other,
    }
}

fn increment(counters: &[AtomicU64; REASONS], reason: Reason) {
    let _ = counters[reason as usize].fetch_update(Ordering::Relaxed, Ordering::Relaxed,
        |value| Some(value.saturating_add(1)));
}

pub(crate) fn note_resource_exhaustion(error: &io::Error) {
    debug_assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
    increment(&COUNTERS, classify(error));
}

#[derive(Clone, Copy, Debug, serde::Serialize)]
pub(crate) struct Count {
    reason: &'static str,
    count: u64,
}
#[derive(Clone, Copy, Debug, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Snapshot {
    contract: &'static str,
    fixed_counter_bytes: usize,
    counters: [Count; REASONS],
}
fn snapshot(counters: &[AtomicU64; REASONS]) -> Snapshot {
    Snapshot { contract: NATIVE_RESOURCE_EXHAUSTION_CONTRACT,
        fixed_counter_bytes: std::mem::size_of_val(counters),
        counters: std::array::from_fn(|index| Count {
            reason: NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES[index],
            count: counters[index].load(Ordering::Relaxed),
        }) }
}
pub(crate) fn resource_exhaustion_snapshot() -> Snapshot { snapshot(&COUNTERS) }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_internal_limits_reservations_and_os_memory_are_distinct() {
        let cases = [
            ("raw archive owner capacity is exhausted", Reason::ArchiveOwner),
            ("raw archive conversation capacity is exhausted", Reason::ArchiveConversation),
            ("raw archive snapshot exceeds retained-byte budget", Reason::ArchiveRestoreBudget),
            ("Turn projection patch exceeds its deterministic resource bound", Reason::DomainBound),
            ("storage.v2 frame byte accounting overflow", Reason::AccountingOverflow),
            ("storage.v2 streamed request allocation failed", Reason::ReservationFailure),
        ];
        for (text, expected) in cases {
            assert_eq!(classify(&io::Error::new(io::ErrorKind::OutOfMemory, text)), expected);
        }
        let reserve = Vec::<u8>::new().try_reserve(usize::MAX).unwrap_err();
        assert_eq!(classify(&io::Error::new(io::ErrorKind::OutOfMemory, reserve)), Reason::ReservationFailure);
        #[cfg(unix)]
        assert_eq!(classify(&io::Error::from_raw_os_error(12)), Reason::OsMemoryFailure);
        // Mere ErrorKind has no allocator or quota evidence.
        assert_eq!(classify(&io::Error::from(io::ErrorKind::OutOfMemory)), Reason::Other);
    }

    #[test]
    fn unknown_message_path_body_and_known_prefix_never_escape_snapshot() {
        let counters = [const { AtomicU64::new(0) }; REASONS];
        for private in ["/private/user-key payload-secret", "raw archive owner capacity is exhausted /private/user-key",
            "raw archive owner capacity is exhausted\nsecret body"] {
            let reason = classify(&io::Error::new(io::ErrorKind::OutOfMemory, private));
            assert_eq!(reason, Reason::Other);
            increment(&counters, reason);
        }
        let large = "secret-body".repeat(1024);
        assert_eq!(classify(&io::Error::new(io::ErrorKind::OutOfMemory, large)), Reason::Other);
        let encoded = serde_json::to_string(&snapshot(&counters)).unwrap();
        assert!(!encoded.contains("private")); assert!(!encoded.contains("secret"));
        assert!(!encoded.contains("capacity is exhausted"));
        assert!(encoded.contains("other"));
    }

    #[test]
    fn semantic_wire_error_and_retry_classification_are_preserved() {
        for text in ["raw archive owner capacity is exhausted", "/private/key user-body",
            "Turn projection patch exceeds its deterministic resource bound"] {
            let error = crate::semantic_executor::map_storage_error(
                io::Error::new(io::ErrorKind::OutOfMemory, text));
            assert_eq!(error.code(), "database_resource_exhausted");
            assert!(error.retryable());
            assert_eq!(error.to_string(), "database_resource_exhausted");
        }
        let payload = crate::semantic_executor::map_storage_error(io::Error::new(
            io::ErrorKind::OutOfMemory, "storage_payload_too_large: retained existing priority"));
        assert_eq!(payload.code(), "storage_payload_too_large");
        assert!(!payload.retryable());
        let snapshot = serde_json::to_string(&resource_exhaustion_snapshot()).unwrap();
        assert!(!snapshot.contains("private")); assert!(!snapshot.contains("user-body"));
        assert!(resource_exhaustion_snapshot().counters[Reason::ArchiveOwner as usize].count >= 1);
    }

    #[test]
    fn concurrent_snapshot_stays_fixed_and_counts_saturate() {
        let counters = std::sync::Arc::new([const { AtomicU64::new(0) }; REASONS]);
        let mut writers = Vec::new();
        for _ in 0..4 {
            let counters = counters.clone();
            writers.push(std::thread::spawn(move || {
                for _ in 0..1000 { increment(&counters, Reason::ArchiveConversation); }
            }));
        }
        for _ in 0..100 {
            let snap = snapshot(&counters);
            assert_eq!(snap.fixed_counter_bytes, 64);
            assert_eq!(snap.counters.len(), 8);
            assert!(snap.counters[Reason::ArchiveConversation as usize].count <= 4000);
        }
        for writer in writers { writer.join().unwrap(); }
        assert_eq!(snapshot(&counters).counters[Reason::ArchiveConversation as usize].count, 4000);
        counters[Reason::Other as usize].store(u64::MAX, Ordering::Relaxed);
        increment(&counters, Reason::Other);
        assert_eq!(snapshot(&counters).counters[Reason::Other as usize].count, u64::MAX);
    }
}
