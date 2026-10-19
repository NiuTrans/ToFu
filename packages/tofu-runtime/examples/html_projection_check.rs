//! Bounded line-oriented differential driver; no application or performance gate.
use std::io::{self, BufRead, Write};

#[cfg(unix)]
fn cpu_ns() -> u64 {
    let mut clock = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    assert_eq!(
        unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut clock) },
        0
    );
    clock.tv_sec as u64 * 1_000_000_000 + clock.tv_nsec as u64
}
#[cfg(not(unix))]
fn cpu_ns() -> u64 {
    panic!("component CPU diagnostic requires a Unix process clock")
}

fn main() {
    if std::env::args().nth(1).as_deref() == Some("--identity") {
        use sha2::{Digest, Sha256};
        let sources = [
            (
                "html_projection.rs",
                include_bytes!("../src/html_projection.rs").as_slice(),
            ),
            (
                "html_tokenizer.rs",
                include_bytes!("../src/html_tokenizer.rs").as_slice(),
            ),
            (
                "generated_html_projection.rs",
                include_bytes!("../src/generated_html_projection.rs").as_slice(),
            ),
        ];
        let witness: serde_json::Map<String, serde_json::Value> = sources
            .into_iter()
            .map(|(name, data)| (name.into(), format!("{:x}", Sha256::digest(data)).into()))
            .collect();
        println!("{}", serde_json::Value::Object(witness));
        return;
    }
    let mut output = io::BufWriter::new(io::stdout().lock());
    let measure = std::env::args().nth(1).as_deref() == Some("--measure");
    for line in io::stdin().lock().lines() {
        let source: String = serde_json::from_str(&line.unwrap()).unwrap();
        if measure {
            let expected = tofu_runtime::html_projection::project(&source).unwrap();
            let mut walls = Vec::with_capacity(100);
            let mut cpus = Vec::with_capacity(100);
            for i in 0..120 {
                let cpu = cpu_ns();
                let wall = std::time::Instant::now();
                let actual =
                    tofu_runtime::html_projection::project(std::hint::black_box(&source)).unwrap();
                let wall = wall.elapsed().as_nanos() as u64;
                let cpu = cpu_ns() - cpu;
                assert_eq!(actual, expected);
                if i >= 20 {
                    walls.push(wall);
                    cpus.push(cpu);
                }
            }
            serde_json::to_writer(
                &mut output,
                &serde_json::json!({"text":expected,
                "wall_ns":walls,"cpu_ns":cpus,"warmups":20,"pid":std::process::id()}),
            )
            .unwrap();
            writeln!(output).unwrap();
            continue;
        }
        let result = match tofu_runtime::html_projection::project(&source) {
            Ok(text) => serde_json::json!({"text":text}),
            Err(error) => serde_json::json!({"error":format!("{error:?}")}),
        };
        serde_json::to_writer(&mut output, &result).unwrap();
        writeln!(output).unwrap();
    }
}
