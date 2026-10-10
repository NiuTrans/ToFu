//! Bounded account allocation/lifetime and CPU diagnostics, excluding HTTP/tokenizer.
use std::{
    alloc::{GlobalAlloc, Layout, System},
    hint::black_box,
    sync::atomic::{AtomicUsize, Ordering},
    time::Instant,
};
use tofu_runtime::model_account::{Account, Bounds, Counts};
struct Allocator;
static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if !p.is_null() {
            let n = LIVE.fetch_add(l.size(), Ordering::Relaxed) + l.size();
            PEAK.fetch_max(n, Ordering::Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        LIVE.fetch_sub(l.size(), Ordering::Relaxed);
        System.dealloc(p, l)
    }
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
fn cpu() -> u128 {
    let mut t = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    assert_eq!(
        unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut t) },
        0
    );
    t.tv_sec as u128 * 1_000_000_000 + t.tv_nsec as u128
}
fn main() {
    for n in [1, 8, 64, 512] {
        let before = LIVE.load(Ordering::Relaxed);
        PEAK.store(before, Ordering::Relaxed);
        let a = Account::new(
            7,
            "root".into(),
            None,
            Bounds {
                actors: n,
                attempts: n,
                identity_bytes: 64,
            },
            None,
        )
        .unwrap();
        let mut leases = Vec::with_capacity(n);
        for i in 0..n {
            let name = if i == 0 {
                "root".into()
            } else {
                format!("actor-{i:026}")
            };
            if i != 0 {
                a.attach(7, name.clone(), Some(100)).unwrap();
            }
            leases.push(
                a.try_reserve(
                    7,
                    &name,
                    Counts {
                        input: 1,
                        output: 1,
                        minimum_output: 1,
                    },
                )
                .unwrap(),
            );
        }
        let retained = LIVE.load(Ordering::Relaxed) - before;
        let peak = PEAK.load(Ordering::Relaxed) - before;
        drop(leases);
        drop(a);
        let after = LIVE.load(Ordering::Relaxed) - before;
        println!(
            "{}",
            serde_json::json!({"actors_and_attempts":n,"live_requested_bytes_including_probe_lease_vector":retained,"peak_requested_bytes":peak,"remaining_after_drop":after})
        );
    }
    let raw = serde_json::json!({"prompt_tokens":20,"completion_tokens":5});
    let a = Account::new(
        7,
        "root".into(),
        None,
        Bounds {
            actors: 1,
            attempts: 1,
            identity_bytes: 64,
        },
        None,
    )
    .unwrap();
    for _ in 0..1000 {
        let mut l = a
            .try_reserve(
                7,
                "root",
                Counts {
                    input: 20,
                    output: 30,
                    minimum_output: 1,
                },
            )
            .unwrap();
        l.mark_dispatched(|| false).unwrap();
        l.settle(Some(&raw), false).unwrap();
    }
    let start = Instant::now();
    let c = cpu();
    for _ in 0..20000 {
        let mut l = a
            .try_reserve(
                7,
                "root",
                Counts {
                    input: 20,
                    output: 30,
                    minimum_output: 1,
                },
            )
            .unwrap();
        l.mark_dispatched(|| false).unwrap();
        l.settle(Some(&raw), false).unwrap();
        black_box(a.snapshot(7).unwrap());
    }
    println!(
        "{}",
        serde_json::json!({"calls":20000,"wall_ns":start.elapsed().as_nanos(),"cpu_ns":cpu()-c,"tokens":a.snapshot(7).unwrap().tokens})
    );
}
