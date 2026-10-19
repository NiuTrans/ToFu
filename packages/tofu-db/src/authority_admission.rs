//! FIFO foreground mutex admission. One waiter polls the authority; other
//! connections park without a 1 ms thundering herd. No I/O or extra worker.
//! Background maintenance still owns its existing authority mutex protocol.
use std::collections::VecDeque;
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, Thread};
use std::time::{Duration, Instant};
use std::task::Waker;
#[cfg(test)]
use std::task::Wake;

#[derive(Debug, Default)]
pub(crate) struct AuthorityAdmission {
    waiting: Mutex<VecDeque<Arc<Waiter>>>,
}

pub(crate) struct Permit<'a> {
    admission: &'a AuthorityAdmission,
    waiter: Arc<Waiter>,
}

pub(crate) enum Admission<'a> {
    Entered(Permit<'a>),
    DeadlineElapsed,
    ConnectionClosed,
}

impl AuthorityAdmission {
    /// Execute a capacity fast path only while no FIFO claimant can be passed.
    /// The queue guard spans the decision; this grants no authority access.
    pub(crate) fn when_uncontended<T>(
        &self, operation: impl FnOnce() -> io::Result<Option<T>>,
    ) -> io::Result<Option<T>> {
        let waiting = self.waiting.lock()
            .map_err(|_| io::Error::other("authority admission poisoned"))?;
        if !waiting.is_empty() { return Ok(None); }
        operation()
    }

    #[cfg(test)]
    pub(crate) fn waiting_count(&self) -> usize {
        self.waiting.lock().unwrap().len()
    }

    pub(crate) fn acquire(
        &self,
        deadline: Instant,
        alive: &mut impl FnMut() -> bool,
    ) -> io::Result<Admission<'_>> {
        let waiter = Arc::new(Waiter::Thread(thread::current()));
        self.waiting
            .lock()
            .map_err(|_| io::Error::other("authority admission poisoned"))?
            .push_back(Arc::clone(&waiter));
        let permit = Permit {
            admission: self,
            waiter,
        };
        loop {
            if !alive() {
                return Ok(Admission::ConnectionClosed);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(Admission::DeadlineElapsed);
            }
            let first = self
                .waiting
                .lock()
                .map_err(|_| io::Error::other("authority admission poisoned"))?
                .front()
                .is_some_and(|head| Arc::ptr_eq(head, &permit.waiter));
            if first {
                return Ok(Admission::Entered(permit));
            }
            // unpark retains a token even if release raced this park. The
            // slice only checks listener shutdown; no capacity busy-poll.
            thread::park_timeout(remaining.min(Duration::from_millis(25)));
        }
    }
}

impl Drop for Permit<'_> {
    fn drop(&mut self) { self.admission.release(&self.waiter); }
}

#[derive(Debug)]
enum Waiter { Thread(Thread), Continuation(Waker) }
impl Waiter {
    fn notify(&self) {
        match self {
            Self::Thread(thread) => thread.unpark(),
            Self::Continuation(wake) => wake.wake_by_ref(),
        }
    }
}

/// Unique registration; moving it into a permit preserves its FIFO position.
/// Dropping either a pending or entered registration cancels that claim.
struct OwnedRegistration { admission: Arc<AuthorityAdmission>, waiter: Arc<Waiter> }
impl Drop for OwnedRegistration {
    fn drop(&mut self) { self.admission.release(&self.waiter); }
}
pub(crate) struct QueuedAdmission { registration: OwnedRegistration }
pub(crate) struct OwnedPermit { _registration: OwnedRegistration }
impl OwnedPermit {
    pub(crate) fn belongs_to(&self, admission: &AuthorityAdmission) -> bool {
        std::ptr::eq(self._registration.admission.as_ref(), admission)
    }
}
pub(crate) enum AdmissionPoll {
    Entered(OwnedPermit),
    Waiting(QueuedAdmission),
}
impl QueuedAdmission {
    /// Poll once, never park. There is only one owner of a queue position.
    /// The scheduler retains Waiting and resumes it through the registered wake.
    pub(crate) fn try_enter(self) -> io::Result<AdmissionPoll> {
        let first = {
            let waiting = self.registration.admission.waiting.lock()
                .map_err(|_| io::Error::other("authority admission poisoned"))?;
            waiting.front().is_some_and(|head| Arc::ptr_eq(head, &self.registration.waiter))
        };
        if first { Ok(AdmissionPoll::Entered(OwnedPermit { _registration: self.registration })) }
        else { Ok(AdmissionPoll::Waiting(self)) }
    }
}
impl AuthorityAdmission {
    /// Register on the same FIFO used by synchronous connections. Wake targets
    /// are scheduler signals, never storage/user callbacks; run outside the lock.
    pub(crate) fn enqueue(self: &Arc<Self>, wake: Waker) -> io::Result<QueuedAdmission> {
        let waiter = Arc::new(Waiter::Continuation(wake));
        let first = {
            let mut waiting = self.waiting.lock()
                .map_err(|_| io::Error::other("authority admission poisoned"))?;
            let first = waiting.is_empty();
            waiting.push_back(Arc::clone(&waiter));
            first
        };
        let registration = OwnedRegistration { admission: Arc::clone(self), waiter };
        if first { registration.waiter.notify(); }
        Ok(QueuedAdmission { registration })
    }
    fn release(&self, waiter: &Arc<Waiter>) {
        let next = {
            let mut waiting = self.waiting.lock().unwrap_or_else(|error| error.into_inner());
            if let Some(index) = waiting.iter().position(|entry| Arc::ptr_eq(entry, waiter)) {
                waiting.remove(index);
            }
            waiting.front().cloned()
        };
        if let Some(next) = next { next.notify(); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn capacity_fast_path_cannot_pass_an_existing_fifo_claimant() {
        let gate = AuthorityAdmission::default();
        let Admission::Entered(first) = gate
            .acquire(Instant::now() + Duration::from_secs(1), &mut || true)
            .unwrap()
        else { panic!("first claimant must enter") };
        let blocked = gate.when_uncontended(|| -> io::Result<Option<usize>> {
            panic!("capacity cannot be taken ahead of the FIFO claimant")
        }).unwrap();
        assert_eq!(blocked, None);
        drop(first);
        assert_eq!(gate.when_uncontended(|| Ok(Some(7))).unwrap(), Some(7));
    }

    #[test]
    fn timed_out_waiter_does_not_strand_successors() {
        let gate = Arc::new(AuthorityAdmission::default());
        let Admission::Entered(first) = gate
            .acquire(Instant::now() + Duration::from_secs(5), &mut || true)
            .unwrap()
        else {
            panic!()
        };
        let other = Arc::clone(&gate);
        let timed = thread::spawn(move || {
            assert!(matches!(
                other
                    .acquire(Instant::now() + Duration::from_millis(10), &mut || true)
                    .unwrap(),
                Admission::DeadlineElapsed
            ));
        });
        timed.join().unwrap();
        let (tx, rx) = mpsc::channel();
        let other = Arc::clone(&gate);
        let next = thread::spawn(move || {
            let Admission::Entered(_permit) = other
                .acquire(Instant::now() + Duration::from_secs(2), &mut || true)
                .unwrap()
            else {
                panic!()
            };
            tx.send(()).unwrap();
        });
        drop(first);
        rx.recv_timeout(Duration::from_secs(1)).unwrap();
        next.join().unwrap();
        assert!(gate.waiting.lock().unwrap().is_empty());
    }

    #[test]
    fn cancelled_waiter_releases_its_position() {
        let gate = AuthorityAdmission::default();
        assert!(matches!(
            gate.acquire(Instant::now() + Duration::from_secs(60), &mut || false)
                .unwrap(),
            Admission::ConnectionClosed
        ));
        assert!(gate.waiting.lock().unwrap().is_empty());
    }
    struct ReentrantWake { gate: Arc<AuthorityAdmission>, wakes: std::sync::atomic::AtomicUsize }
    impl Wake for ReentrantWake {
        fn wake(self: Arc<Self>) { self.wake_by_ref(); }
        fn wake_by_ref(self: &Arc<Self>) {
            let _count = self.gate.waiting_count(); // proves wake runs outside queue lock
            self.wakes.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }

    #[test]
    fn queued_claims_move_across_threads_and_share_fifo_with_synchronous_waiters() {
        let gate = Arc::new(AuthorityAdmission::default());
        let Admission::Entered(first) = gate.acquire(Instant::now()+Duration::from_secs(1), &mut ||true).unwrap() else { panic!() };
        let wake = Arc::new(ReentrantWake { gate: Arc::clone(&gate), wakes: std::sync::atomic::AtomicUsize::new(0) });
        let queued = gate.enqueue(Waker::from(Arc::clone(&wake))).unwrap();
        let AdmissionPoll::Waiting(queued) = queued.try_enter().unwrap() else { panic!("queued claimant cannot bypass synchronous head") };
        assert_eq!(gate.waiting_count(), 2);
        drop(first);
        let AdmissionPoll::Entered(permit) = std::thread::spawn(move ||queued.try_enter().unwrap()).join().unwrap() else { panic!("released head must admit queued successor") };
        let second = gate.enqueue(Waker::from(Arc::clone(&wake))).unwrap();
        let AdmissionPoll::Waiting(second) = second.try_enter().unwrap() else { panic!() };
        std::thread::spawn(move ||drop(permit)).join().unwrap();
        let AdmissionPoll::Entered(second) = second.try_enter().unwrap() else { panic!() };
        drop(second);
        assert_eq!(gate.waiting_count(), 0);
        assert!(wake.wakes.load(std::sync::atomic::Ordering::Relaxed)>=2);
    }

    #[test]
    fn cancelling_queued_head_and_middle_never_strands_a_successor() {
        let gate = Arc::new(AuthorityAdmission::default());
        let wake = Arc::new(ReentrantWake { gate: Arc::clone(&gate), wakes: std::sync::atomic::AtomicUsize::new(0) });
        let head = gate.enqueue(Waker::from(Arc::clone(&wake))).unwrap();
        let middle = gate.enqueue(Waker::from(Arc::clone(&wake))).unwrap();
        let tail = gate.enqueue(Waker::from(Arc::clone(&wake))).unwrap();
        drop(middle);drop(head);
        let AdmissionPoll::Entered(tail) = tail.try_enter().unwrap() else { panic!() };
        assert_eq!(gate.waiting_count(), 1);drop(tail);
        assert_eq!(gate.waiting_count(), 0);
    }

}
