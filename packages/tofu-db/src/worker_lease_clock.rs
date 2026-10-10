//! Advancing worker lease time shared by one authority and its read replicas.
//! Entry point: WorkerLeaseClock::now_ms. UTC offset high-water plus monotonic
//! elapsed time prevents wall-clock rollback from reviving an expired lease.
//! No RPC timestamp, environment override, thread or durable clock row participates.
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

pub(crate) struct WorkerLeaseClock {
    origin: Instant,
    offset_ms: AtomicU64,
    observed_ms: AtomicU64,
    #[cfg(test)]
    test_now_ms: AtomicU64,
    #[cfg(test)]
    test_utc_now_ms: AtomicU64,
}

pub(crate) struct WorkerClockSample {
    pub(crate) lease_now_ms: u64,
    pub(crate) utc_now_ms: u64,
}

fn invalid_clock() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid worker authority clock")
}

fn wall_ms() -> io::Result<u64> {
    let value = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| invalid_clock())?;
    let millis = u64::try_from(value.as_millis()).map_err(|_| invalid_clock())?;
    if millis == 0 {
        return Err(invalid_clock());
    }
    Ok(millis)
}

fn paired_clock_sample<T>(
    wall_probe: impl FnOnce() -> io::Result<u64>,
    monotonic_probe: impl FnOnce() -> io::Result<T>,
) -> io::Result<(u64, T)> {
    let wall = wall_probe()?;
    let monotonic = monotonic_probe()?;
    Ok((wall, monotonic))
}

impl WorkerLeaseClock {
    pub(crate) fn new() -> io::Result<Self> {
        let (wall, origin) = paired_clock_sample(wall_ms, || Ok(Instant::now()))?;
        Ok(Self {
            origin,
            offset_ms: AtomicU64::new(wall),
            observed_ms: AtomicU64::new(wall),
            #[cfg(test)]
            test_now_ms: AtomicU64::new(0),
            #[cfg(test)]
            test_utc_now_ms: AtomicU64::new(0),
        })
    }

    fn observe(&self, elapsed: u64, wall: u64) -> io::Result<u64> {
        if wall == 0 {
            return Err(invalid_clock());
        }
        let offset = wall.saturating_sub(elapsed);
        let previous_offset = self.offset_ms.fetch_max(offset, Ordering::Relaxed);
        let candidate = previous_offset
            .max(offset)
            .checked_add(elapsed)
            .ok_or_else(invalid_clock)?;
        // A delayed thread may have sampled elapsed before another reader. The
        // second high-water makes the returned observation linearizable too.
        let previous = self.observed_ms.fetch_max(candidate, Ordering::Relaxed);
        let value = candidate.max(previous);
        if value > crate::generated_tofudb_ir::MAX_WORKER_JOB_LEASE_NOW_MILLISECONDS {
            return Err(invalid_clock());
        }
        Ok(value)
    }

    pub(crate) fn snapshot(&self) -> io::Result<WorkerClockSample> {
        #[cfg(test)]
        {
            let value = self.test_now_ms.load(Ordering::Relaxed);
            if value > 0 {
                return Ok(WorkerClockSample {
                    lease_now_ms: value,
                    utc_now_ms: self.test_utc_now_ms.load(Ordering::Relaxed),
                });
            }
        }
        // Sample UTC before elapsed time: a scheduling pause between probes
        // must not permanently inflate the UTC offset and expire a lease early.
        let (wall, elapsed) = paired_clock_sample(wall_ms, || {
            u64::try_from(self.origin.elapsed().as_millis()).map_err(|_| invalid_clock())
        })?;
        Ok(WorkerClockSample {
            lease_now_ms: self.observe(elapsed, wall)?,
            utc_now_ms: wall,
        })
    }

    pub(crate) fn now_ms(&self) -> io::Result<u64> {
        Ok(self.snapshot()?.lease_now_ms)
    }

    #[cfg(test)]
    pub(crate) fn set_test_time(&self, value: u64) {
        assert!(value > 0);
        self.test_now_ms.store(value, Ordering::Relaxed);
        self.test_utc_now_ms.store(value, Ordering::Relaxed);
    }

    #[cfg(test)]
    pub(crate) fn set_test_utc_time(&self, value: u64) {
        assert!(value > 0);
        self.test_utc_now_ms.store(value, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paused_wall_probes_do_not_inflate_constructor_or_observation_offset() {
        use std::cell::Cell;
        let wall = Cell::new(100_000);
        let monotonic = Cell::new(0);
        let sample = |delay| {
            paired_clock_sample(
                || {
                    wall.set(wall.get() + delay);
                    monotonic.set(monotonic.get() + delay);
                    Ok(wall.get())
                },
                || Ok(monotonic.get()),
            )
            .unwrap()
        };
        let (initial_wall, origin) = sample(1_000);
        let clock = WorkerLeaseClock::new().unwrap();
        clock.offset_ms.store(initial_wall, Ordering::Relaxed);
        clock.observed_ms.store(initial_wall, Ordering::Relaxed);
        for pause in [0, 5_000, 0] {
            let (observed_wall, elapsed) = sample(pause);
            assert_eq!(
                clock.observe(elapsed - origin, observed_wall).unwrap(),
                wall.get()
            );
        }
    }

    #[test]
    fn rollback_and_delayed_read_keep_advancing_from_the_last_utc_offset() {
        let clock = WorkerLeaseClock::new().unwrap();
        clock.offset_ms.store(100_000, Ordering::Relaxed);
        clock.observed_ms.store(100_000, Ordering::Relaxed);
        assert_eq!(clock.observe(1_000, 101_000).unwrap(), 101_000);
        assert_eq!(clock.observe(2_000, 200_000).unwrap(), 200_000);
        assert_eq!(clock.observe(3_000, 90_000).unwrap(), 201_000);
        assert_eq!(clock.observe(2_500, 90_000).unwrap(), 201_000);
        assert_eq!(clock.observe(4_000, 90_000).unwrap(), 202_000);
        assert!(clock.observe(4_000, 0).is_err());
        assert!(clock.observe(u64::MAX, 1).is_err());
    }
}
