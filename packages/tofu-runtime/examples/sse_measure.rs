//! Isolated framing measurement, not network/provider or application admission.
use std::{hint::black_box, time::Instant};
use tofu_runtime::sse::Framer;
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n: usize = args[1].parse().unwrap();
    let chunk: usize = args[2].parse().unwrap();
    let wire="data: {\"choices\":[{\"delta\":{\"content\":\"中文😀hello\"}}]}\r\n\r\ndata: [DONE]\r\n\r\n".as_bytes();
    let start = Instant::now();
    let mut checksum = 0usize;
    for _ in 0..n {
        let mut f = Framer::new(1 << 20).unwrap();
        for part in wire.chunks(chunk) {
            f.feed_until(black_box(part), |e, i| {
                assert_eq!(i.count, 0);
                checksum += e.data.len();
                false
            })
            .unwrap();
        }
        f.finalize();
        assert_eq!(f.drain_issues().count, 0);
    }
    println!(
        "{{\"operations\":{n},\"chunk\":{chunk},\"wall_ns\":{},\"checksum\":{checksum}}}",
        start.elapsed().as_nanos()
    );
}
