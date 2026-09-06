//! Tofu-DB pre-authority engine.
//!
//! The crate currently proves the durability envelope only: an alternating
//! checksummed control file, a bounded hash-chained active commit log, bounded
//! durability groups, a bounded single-writer sequencer, and exclusive process
//! ownership. It is intentionally not wired into Tofu's
//! storage supervisor until semantic and operational certification is complete.

mod artifact;
pub mod authority;
pub mod authority_gc;
pub mod backup;
mod backup_gc;
mod billing;
pub mod blob;
pub mod block;
mod browser_site_observation;
pub mod certification;
mod compaction_archive;
pub mod control;
mod conversation_header;
mod credential;
pub mod daemon;
mod daily_cost;
pub mod engine;
pub mod entity;
pub mod generated_storage_operations;
mod generated_storage_v2;
pub mod generated_tofudb_ir;
mod generated_unicode_casefold;
mod generated_unicode_simple_fold;
pub mod history;
mod indexed_stream;
mod integration_workspace;
mod knowledge;
pub mod listener;
mod log_aggregate;
pub mod logical_outbox;
pub mod maintenance;
mod model_routing;
mod optimizer;
mod orchestration;
pub mod outbox_multi_sink;
pub mod outbox_publisher;
pub mod outbox_relay;
pub mod outbox_sink;
pub mod outbox_worker;
mod paper;
mod paper_artifact;
mod paper_library;
mod paper_podcast;
pub mod payload_manifest;
pub mod payload_segment;
mod plugin;
mod project_brain;
pub mod protocol;
mod provider;
mod queue;
mod rate_limit;
mod raw_archive;
pub mod receipt;
mod recent_project;
mod research;
pub mod resource_probe;
mod scheduler;
mod search_dirty;
pub mod semantic;
pub mod semantic_executor;
pub mod sequencer;
pub mod server;
pub mod stream;
mod swarm;
mod task_result;
mod tenant_user;
mod timer;
pub mod transaction;
pub mod transaction_ir;
mod turn;
mod turn_projection_patch;
pub mod turn_search_projection;
pub mod turn_search_worker;
mod versioned_document;
pub mod vfs;
pub mod wal;
mod worker_job;

pub const FORMAT_VERSION: u32 = 1;
pub const ACTIVE_LOG_MAX_BYTES: u64 = 64 * 1024 * 1024;
pub const ACTIVE_LOG_ROTATE_BYTES: u64 = 60 * 1024 * 1024;
