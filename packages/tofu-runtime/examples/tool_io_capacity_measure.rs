//! Synthetic waiting-tool CPU occupancy comparison; not application admission.
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tofu_runtime::{
    execution_contract::ExecutionPhase as Phase, session::Registry as Sessions, tool_contract::*,
    tool_scheduler::*,
};
struct Allow;
impl Authorization for Allow {
    fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        owner == 7
    }
}
struct IoWork(Arc<AtomicUsize>, String);
impl Handler for IoWork {
    fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
        match self.1.as_str() {
            "wait" => std::thread::sleep(Duration::from_millis(20)),
            "empty" => {}
            "cpu" => {
                let mut hash = 7u64;
                for n in 0..100000u64 {
                    hash = hash.rotate_left(3).wrapping_mul(6364136223846793005) ^ n;
                }
                std::hint::black_box(hash);
            }
            _ => panic!("unsupported benchmark profile"),
        }
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(json!("done"))
    }
}
fn main() {
    let args: Vec<_> = std::env::args().collect();
    let cpu_units: usize = args[1].parse().unwrap();
    let samples: usize = args[2].parse().unwrap();
    let calls_per_batch = 8usize;
    let profile = args.get(4).cloned().unwrap_or_else(|| "wait".into());
    let warmup = if samples == 1 { 0 } else { 20 };
    let sessions = Sessions::new();
    let parent = sessions.start("measure", "task", 7, "", None).unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let registry = Registry::new(
        7,
        "epoch",
        vec![(
            "read".into(),
            Arc::new(IoWork(calls.clone(), profile.clone())),
        )],
    )
    .unwrap();
    let catalog=Catalog::compile(7,"epoch",&json!({"read":{"name":"read","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}}})).unwrap();
    let allocator = tofu_runtime::capacity_allocation::Allocator::new(
        tofu_runtime::capacity::Snapshot {
            host_cpus: 2,
            affinity_cpus: Some(2),
            quota_cpus: Some(2),
            effective_cpus: 2,
            memory_capacity_bytes: Some(128 * 1024 * 1024),
            memory_available_bytes: Some(128 * 1024 * 1024),
            volume_total_bytes: None,
            volume_available_bytes: None,
        },
        1,
    )
    .unwrap();
    let scheduler = Scheduler::with_capacity(
        &allocator,
        tofu_runtime::capacity::Demand {
            worker_bytes: 2 * 1024 * 1024,
            job_bytes: 16384,
            queued_jobs_per_worker: 4,
            memory_divisor: 1,
            cpu_units_per_worker: cpu_units,
        },
        BTreeSet::from(["read".into()]),
    )
    .unwrap();
    let allocation = allocator.snapshot().unwrap();
    let mut times = Vec::with_capacity(samples);
    for round in 0..warmup + samples {
        let start = Instant::now();
        let batch = Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "measure",
            round,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &registry,
            authorization: &Allow,
            calls: (0..calls_per_batch)
                .map(|position| Invocation {
                    position,
                    proof: catalog
                        .admit(7, "read", json!({}), &Value::Null, &Allow)
                        .unwrap(),
                })
                .collect(),
        };
        let results = scheduler.dispatch(batch).unwrap();
        assert_eq!(results.len(), calls_per_batch);
        assert!(results
            .iter()
            .all(|r| r.outcome.as_ref().unwrap().receipt.invariants_satisfied));
        if round >= warmup {
            times.push(start.elapsed().as_nanos() as u64);
        }
        assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
        assert_eq!(sessions.active_count().unwrap(), 1);
    }
    let resident = scheduler.snapshot().unwrap().resident_workers;
    scheduler.close().unwrap();
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
    assert_eq!(allocator.snapshot().unwrap().weighted_bytes, 0);
    assert_eq!(allocator.snapshot().unwrap().cpu_units, 0);
    parent.settle(Phase::Completed, "").unwrap();
    let status = std::fs::read_to_string("/proc/self/status").unwrap();
    let vmhwm: u64 = status
        .lines()
        .find_map(|line| {
            line.strip_prefix("VmHWM:")
                .and_then(|value| value.split_whitespace().next())
                .and_then(|value| value.parse().ok())
        })
        .unwrap();
    let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe {
        assert_eq!(libc::getrusage(libc::RUSAGE_SELF, &mut usage), 0);
    };
    let micros = |t: libc::timeval| t.tv_sec * 1_000_000 + t.tv_usec;
    println!(
        "{}",
        json!({"profile":profile,"variant":args[1],"samples_ns":times,"warmup_batches":warmup,"calls":calls.load(Ordering::SeqCst),"worker_capacity":allocation.workers,"reserved_cpu_units":allocation.cpu_units,"reserved_weighted_bytes":allocation.weighted_bytes,"resident_workers_before_close":resident,"resident_workers_after_close":scheduler.snapshot().unwrap().resident_workers,"active_scopes":sessions.active_count().unwrap(),"peak_rss_kib":vmhwm,"cpu_us":micros(usage.ru_utime)+micros(usage.ru_stime)})
    );
}
