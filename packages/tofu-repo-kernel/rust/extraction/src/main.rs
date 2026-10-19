//! One JSON batch per owned process. Byte limits; serialize fully before stdout.
use std::{
    env,
    io::{self, Read, Write},
    path::PathBuf,
};
use tofu_repository_extraction::wire;

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    if args.len() != 3 {
        return Err("expected grammar-directory request-byte-limit response-byte-limit".into());
    }
    let request_limit: u64 = args[1].to_str().ok_or("invalid request limit")?.parse()?;
    let response_limit: usize = args[2].to_str().ok_or("invalid response limit")?.parse()?;
    let read_limit = request_limit
        .checked_add(1)
        .ok_or("request limit overflow")?;
    let mut bytes = Vec::new();
    io::stdin()
        .lock()
        .take(read_limit)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > request_limit {
        return Err("request exceeds caller byte limit".into());
    }
    let output = wire::response(PathBuf::from(&args[0]), &bytes, response_limit)?;
    io::stdout().lock().write_all(&output)?;
    Ok(())
}

fn main() {
    match std::panic::catch_unwind(run) {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            eprintln!("native extraction refused: {error}");
            std::process::exit(2);
        }
        Err(_) => {
            eprintln!("native extraction panicked");
            std::process::exit(3);
        }
    }
}
