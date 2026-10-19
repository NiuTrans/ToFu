//! Scoped borrowed-usage/price allocator diagnostic: excludes JSON parsing/projection,
//! route/clock policy, wallet writes, process startup and RSS. No speedup claim.
use std::{
    alloc::{GlobalAlloc, Layout, System},
    hint::black_box,
    sync::atomic::{AtomicUsize, Ordering},
};
use tofu_runtime::pricing::{Currency, ResolvedPrice, Tokens};
use tofu_runtime::pricing_tiers::{Card, Rates, Tier};
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
fn main() {
    let tokens = Tokens {
        input: 10000,
        output: 200,
        cache_write: 0,
        cache_read: 9000,
        thinking: 0,
        uncached: 1000,
        total_input: 10000,
    };
    let base = Rates {
        input: 2.5,
        output: 10.,
        cache_write_multiplier: 1.25,
        cache_read_multiplier: 0.1,
        currency: Currency::Usd,
    };
    let tiers = [
        Tier {
            id: "small",
            max_prompt_tokens: 5000,
            rates: base,
        },
        Tier {
            id: "large",
            max_prompt_tokens: 20000,
            rates: Rates {
                input: 5.,
                output: 20.,
                ..base
            },
        },
    ];
    let card = Card {
        base,
        tiers: &tiers,
        usd_to_cny: 7.24,
        peak_multiplier: 1.,
    };
    let admitted = card.admitted().unwrap();
    let usage = serde_json::json!({"prompt_tokens":10000,"input_tokens":1000,"cache_read_input_tokens":9000,"completion_tokens":"２００"});
    assert_eq!(
        tofu_runtime::pricing_usage::normalize(&usage).unwrap(),
        tokens
    );
    let before = LIVE.load(Ordering::Relaxed);
    PEAK.store(before, Ordering::Relaxed);
    let mut checksum = 0.0;
    for _ in 0..100000 {
        let quote = black_box(
            admitted
                .quote(black_box(
                    tofu_runtime::pricing_usage::normalize(black_box(&usage)).unwrap(),
                ))
                .unwrap()
                .unwrap(),
        );
        let credits = tofu_runtime::billing_credits::convert(Some(&quote.cost), 0.125).unwrap();
        checksum += black_box(credits.total) as f64;
    }
    let peak = PEAK.load(Ordering::Relaxed).saturating_sub(before);
    let retained = LIVE.load(Ordering::Relaxed).saturating_sub(before);
    println!(
        "{}",
        serde_json::json!({"scope":"borrowed_usage_tier_arithmetic_and_micro_credits","operations":100000,"peak_additional_requested_bytes":peak,"retained_additional_requested_bytes":retained,"tokens_layout_bytes":std::mem::size_of::<Tokens>(),"price_layout_bytes":std::mem::size_of::<ResolvedPrice>(),"cost_layout_bytes":std::mem::size_of::<tofu_runtime::pricing::Cost>(),"checksum":checksum})
    );
    assert_eq!(peak, 0);
    assert_eq!(retained, 0);
}
