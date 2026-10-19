//! Real demand workers and shutdown quiescence for the shared native reactor.
use std::{
    sync::{atomic::{AtomicUsize, Ordering}, mpsc, Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

#[derive(Default)]
struct Gate(Arc<(Mutex<bool>, Condvar)>);
impl Gate {
    fn release(&self) {
        let (lock, wake) = &*self.0;
        *lock.lock().unwrap() = true;
        wake.notify_all();
    }
}
impl Drop for Gate {
    fn drop(&mut self) { self.release(); }
}

#[test]
fn independent_blocking_demand_starts_and_shutdown_joins_running_workers() {
    const COUNT: usize = 32;
    let runtime = tofu_runtime::reactor::build().unwrap();
    let gate = Gate::default();
    let active = Arc::new(AtomicUsize::new(0));
    let completed = Arc::new(AtomicUsize::new(0));
    let (arrived, arrivals) = mpsc::channel();
    let started = Instant::now();
    for _ in 0..COUNT {
        let (gate, active, completed, arrived) =
            (gate.0.clone(), active.clone(), completed.clone(), arrived.clone());
        runtime.spawn_blocking(move || {
            active.fetch_add(1, Ordering::AcqRel);
            arrived.send(()).unwrap();
            let (lock, wake) = &*gate;
            let _guard = wake.wait_while(lock.lock().unwrap(), |released| !*released).unwrap();
            active.fetch_sub(1, Ordering::AcqRel);
            completed.fetch_add(1, Ordering::AcqRel);
        });
    }
    drop(arrived);
    let all_started = (0..COUNT).all(|_| arrivals.recv_timeout(Duration::from_secs(5)).is_ok());
    let live = active.load(Ordering::Acquire);
    let (healthy, healthy_result) = mpsc::channel();
    runtime.spawn_blocking(move || healthy.send(42).unwrap());
    let independent_result = healthy_result.recv_timeout(Duration::from_secs(5));
    let arrival_us = started.elapsed().as_micros();
    let (closing, close_started) = mpsc::channel();
    let (closed, close_finished) = mpsc::channel();
    let closer = std::thread::spawn(move || {
        closing.send(()).unwrap();
        drop(runtime);
        closed.send(()).unwrap();
    });
    close_started.recv_timeout(Duration::from_secs(5)).unwrap();
    let remained_owned = close_finished.recv_timeout(Duration::from_millis(50)).is_err();
    gate.release();
    let joined = close_finished.recv_timeout(Duration::from_secs(5));
    closer.join().unwrap();
    assert!(all_started);
    assert_eq!(live, COUNT);
    assert_eq!(independent_result.unwrap(), 42);
    assert!(remained_owned);
    joined.unwrap();
    assert_eq!(active.load(Ordering::Acquire), 0);
    assert_eq!(completed.load(Ordering::Acquire), COUNT);
    println!("NATIVE_REACTOR_DEMAND count={COUNT} healthy=42 arrival_us={arrival_us} joined=true active=0");
}
