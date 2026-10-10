//! FIFO foreground mutex admission. One waiter polls the authority; other
//! connections park without a 1 ms thundering herd. No I/O or extra worker.
//! Background maintenance still owns its existing authority mutex protocol.
use std::collections::VecDeque;
use std::io;
use std::sync::{Arc, Mutex};
use std::thread::{self, Thread};
use std::time::{Duration, Instant};

#[derive(Debug, Default)]
pub(crate) struct AuthorityAdmission {
    waiting: Mutex<VecDeque<Arc<Thread>>>,
}

pub(crate) struct Permit<'a> {
    admission: &'a AuthorityAdmission,
    thread: Arc<Thread>,
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
        let thread = Arc::new(thread::current());
        self.waiting
            .lock()
            .map_err(|_| io::Error::other("authority admission poisoned"))?
            .push_back(Arc::clone(&thread));
        let permit = Permit {
            admission: self,
            thread,
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
                .is_some_and(|head| Arc::ptr_eq(head, &permit.thread));
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
    fn drop(&mut self) {
        let mut waiting = self
            .admission
            .waiting
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if let Some(index) = waiting.iter().position(|t| Arc::ptr_eq(t, &self.thread)) {
            waiting.remove(index);
        }
        if let Some(next) = waiting.front() {
            next.unpark();
        }
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
}
