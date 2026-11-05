//! Resource refusals carry their class from the construction site to the wire.
//!
//! A site that refuses because a resource limit is reached declares the class
//! with one typed constructor (`domain_bound`, `capacity_quota`, ...). The
//! semantic mapper reads that declared class, never the message text, counts
//! it under its contract reason and derives the wire retry flag from the
//! generated per-reason policy (`nativeResourceExhaustionDiagnostics` in
//! contracts/storage_v2.json). A raw `io::ErrorKind::OutOfMemory` carries no
//! class and would be replayed as load shedding, so the engine constructs that
//! kind only in this module (`every_resource_refusal_declares_its_class`), and
//! only this module can build the retryable
//! `SemanticExecutionError::ResourceExhausted`.
//! Counters are process-local fixed labels: no request logging, paths, IDs,
//! payload retention, timers or heap formatting.
use std::collections::TryReserveError;
use std::fmt;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::generated_storage_v2::{
    NATIVE_RESOURCE_EXHAUSTION_CONTRACT, NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES,
    NATIVE_RESOURCE_EXHAUSTION_REASON_RETRYABLE,
};
use crate::semantic_executor::SemanticExecutionError;

const REASONS: usize = NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES.len();
static COUNTERS: [AtomicU64; REASONS] = [const { AtomicU64::new(0) }; REASONS];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Reason {
    ArchiveOwner = 0, ArchiveConversation, ArchiveRestoreBudget, DomainBound, CapacityQuota,
    AccountingOverflow, ReservationFailure, OsMemoryFailure, Other,
}
const _: () = assert!(Reason::Other as usize + 1 == REASONS);

/// The class and fixed literal one construction site declared.
#[derive(Debug)]
struct Refusal {
    reason: Reason,
    message: &'static str,
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for Refusal {}

fn refusal(reason: Reason, message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, Refusal { reason, message })
}

/// A fixed, hardware-independent bound on one request, response, page, scan,
/// batch, document or entity. The identical request against the same state
/// re-hits it, so the wire marks it non-retryable and forwards `message`; the
/// caller shrinks the request (page, window, batch) instead of replaying it.
pub(crate) fn domain_bound(message: &'static str) -> io::Error {
    refusal(Reason::DomainBound, message)
}

/// A fixed quota on retained state (rows or bytes per owner, tenant or
/// collection) that retirement, reaping, deletion or completion by other work
/// frees, such as the swarm session and agent capacity. It stays retryable.
pub(crate) fn capacity_quota(message: &'static str) -> io::Error {
    refusal(Reason::CapacityQuota, message)
}

/// A checked size, count or identifier computation that cannot be represented.
pub(crate) fn accounting_overflow(message: &'static str) -> io::Error {
    refusal(Reason::AccountingOverflow, message)
}

/// A fallible reservation (`try_reserve`) was refused by the allocator.
pub(crate) fn reservation_failure(message: &'static str) -> io::Error {
    refusal(Reason::ReservationFailure, message)
}

/// `reservation_failure` that keeps the allocator's own error as the source.
pub(crate) fn reservation_error(error: TryReserveError) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, error)
}

/// The raw-archive owner ceiling (reclaimed oldest-first, docs/LOGGING.md).
pub(crate) fn raw_archive_owner_capacity(message: &'static str) -> io::Error {
    refusal(Reason::ArchiveOwner, message)
}

/// The raw-archive per-conversation ceiling.
pub(crate) fn raw_archive_conversation_capacity(message: &'static str) -> io::Error {
    refusal(Reason::ArchiveConversation, message)
}

/// The retained-byte budget a raw-archive snapshot restore must fit.
pub(crate) fn raw_archive_restore_budget(message: &'static str) -> io::Error {
    refusal(Reason::ArchiveRestoreBudget, message)
}

/// Whether `error` is a resource refusal of any class, or an allocator or OS
/// memory failure, rather than an integrity, conflict or I/O failure.
pub(crate) fn is_refusal(error: &io::Error) -> bool {
    is_refusal_kind(error.kind())
}

/// `is_refusal` for an error that only exposes its kind (`serde_json::Error`).
pub(crate) fn is_refusal_kind(kind: io::ErrorKind) -> bool {
    kind == io::ErrorKind::OutOfMemory
}

fn classify(error: &io::Error) -> Reason {
    match error.get_ref() {
        Some(inner) => match inner.downcast_ref::<Refusal>() {
            Some(refusal) => refusal.reason,
            // Stable Rust cannot inspect TryReserveErrorKind: capacity overflow and
            // allocation refusal both remain reservation failure, not host OOM.
            None if inner.is::<TryReserveError>() => Reason::ReservationFailure,
            // Untyped text is never classification evidence.
            None => Reason::Other,
        },
        None if error.raw_os_error().is_some() => Reason::OsMemoryFailure,
        None => Reason::Other,
    }
}

fn increment(counters: &[AtomicU64; REASONS], reason: Reason) {
    let _ = counters[reason as usize].fetch_update(Ordering::Relaxed, Ordering::Relaxed,
        |value| Some(value.saturating_add(1)));
}

/// Proof that a retryable refusal passed `semantic_refusal`. Its private field
/// keeps every other module from emitting a retryable
/// `database_resource_exhausted` without a declared class.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TransientRefusal(());

/// The fixed literal of a counted deterministic bound; it travels as the wire
/// `error_message`. Built only by `semantic_refusal` and `semantic_bound`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BoundRefusal(&'static str);

impl BoundRefusal {
    pub(crate) fn message(self) -> &'static str {
        self.0
    }
}

/// The semantic error of an `OutOfMemory` storage error, counted under its
/// declared reason; `None` for every other kind.
pub(crate) fn semantic_refusal(error: &io::Error) -> Option<SemanticExecutionError> {
    if !is_refusal(error) {
        return None;
    }
    let reason = classify(error);
    increment(&COUNTERS, reason);
    if NATIVE_RESOURCE_EXHAUSTION_REASON_RETRYABLE[reason as usize] {
        return Some(SemanticExecutionError::ResourceExhausted(TransientRefusal(())));
    }
    let message = error
        .get_ref()
        .and_then(|inner| inner.downcast_ref::<Refusal>())
        .map_or(NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES[reason as usize], |refusal| {
            refusal.message
        });
    Some(SemanticExecutionError::ResourceBound(BoundRefusal(message)))
}

/// A deterministic bound the semantic surface enforces before any storage
/// read (a request's offset, list or document size); same class and counter
/// as `domain_bound`.
pub(crate) fn semantic_bound(message: &'static str) -> SemanticExecutionError {
    increment(&COUNTERS, Reason::DomainBound);
    SemanticExecutionError::ResourceBound(BoundRefusal(message))
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
#[path = "resource_exhaustion_tests.rs"]
mod tests;
