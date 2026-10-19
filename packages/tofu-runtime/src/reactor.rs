//! Shared current-thread reactor for native CLI I/O and demand blocking work.
//! Entry: build. Tokio owns lazy worker creation, idle reclamation and joined
//! shutdown. HTTP reuse hints never authorize or cap independent blocking jobs.
use tokio::runtime::{Builder, Runtime};

pub fn build() -> std::io::Result<Runtime> {
    Builder::new_current_thread()
        // This is a representability boundary, not a preallocated worker pool.
        // Tokio starts workers only for submitted blocking work and reclaims idle
        // workers. Physical thread creation failures retain Tokio's error path.
        .max_blocking_threads(usize::MAX)
        .enable_all()
        .build()
}
