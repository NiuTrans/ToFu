//! Bounded accumulated-backlog spill benchmark on an explicit filesystem.
//! Usage: cargo run --release --example spill_backlog_bench -- /path/to/parent
use std::time::Instant;
use tofu_db::engine::Engine;
use tofu_db::transaction::MAX_CARRIED_PAYLOAD_BYTES;
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let parent = std::env::args().nth(1).ok_or("filesystem parent required")?;
    let directory = tempfile::Builder::new().prefix("tofu-spill-bench-").tempdir_in(parent)?;
    let mut engine = Engine::initialize(directory.path())?;
    let started = Instant::now();
    for index in 0_u32..256 {
        let mut payload = vec![0x51; 4096];
        payload[..4].copy_from_slice(&index.to_le_bytes());
        engine.commit_transaction(b"small", &[&payload])?;
    }
    let seed_ms = started.elapsed().as_secs_f64() * 1000.0;
    let payload = vec![0xAB; MAX_CARRIED_PAYLOAD_BYTES + 1];
    let started = Instant::now();
    let result = engine.commit_transaction(b"large", &[&payload])?;
    let spill_ms = started.elapsed().as_secs_f64() * 1000.0;
    drop(engine);
    let reopened = Engine::open(directory.path())?;
    assert_eq!(reopened.state().durable_sequence, 257);
    assert_eq!(reopened.read_block(result.block_ids[0])?, payload);
    println!("backlog_commits=256 seed_ms={seed_ms:.2} spill_ms={spill_ms:.2} reopen=ok");
    Ok(())
}
