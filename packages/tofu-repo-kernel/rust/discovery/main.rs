//! One owned discovery request. Root changes happen after exec so the parent
//! can enforce its deadline even if chdir or a later filesystem call blocks.
//! No helpers, persistent state, threads or partial success frames are created.
use std::io::Write;

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let Some(root) = arguments.next() else {
        std::process::exit(1);
    };
    let timeout = arguments
        .next()
        .and_then(|value| value.to_str()?.parse::<u64>().ok());
    let Some(timeout) = timeout else {
        std::process::exit(1);
    };
    if arguments.next().is_some() {
        std::process::exit(1);
    }
    if timeout == 0 {
        std::process::exit(3);
    }
    if std::env::set_current_dir(root).is_err() {
        std::process::exit(2);
    }
    match tofu_repository_discovery::current_directory_files(timeout) {
        Ok(bytes) => {
            let stdout = std::io::stdout();
            let mut output = stdout.lock();
            if output
                .write_all(&bytes)
                .and_then(|_| output.flush())
                .is_err()
            {
                std::process::exit(2);
            }
        }
        Err(status) => std::process::exit(status),
    }
}
