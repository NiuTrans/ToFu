//! Tofu-DB pre-authority engine.
//!
//! The crate currently proves the durability envelope only: an alternating
//! checksummed control file, a bounded hash-chained active commit log, bounded
//! durability groups, a bounded single-writer sequencer, and exclusive process
//! ownership. It is intentionally not wired into Tofu's
//! storage supervisor until semantic and operational certification is complete.

// The embedded allocator is an explicit build choice; rlib consumers may
// continue defining their own allocator when this feature is disabled.
#[cfg(all(feature = "embedded-mimalloc", not(feature = "allocator-diagnostics")))]
#[global_allocator]
static EMBEDDED_ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;
#[cfg(feature = "allocator-diagnostics")]
mod allocator_diagnostics;
#[cfg(feature = "allocator-diagnostics")]
#[global_allocator]
static EMBEDDED_ALLOCATOR: allocator_diagnostics::AccountingMiMalloc = allocator_diagnostics::AccountingMiMalloc::new();

mod artifact;
mod artifact_migration;
pub mod at_rest;
pub mod verify;
pub mod authority;
pub mod authority_gc;
pub mod authority_gc_online;
pub mod authority_watch;
mod online_backup;

pub mod background_pack;
pub mod backup;
mod backup_gc;
mod billing;
pub mod blob;
pub mod block;
mod payload_routing;
mod browser_observation_migration;
mod browser_site_observation;
pub mod certification;
pub mod certification_matrix;
#[cfg(test)]
mod cold_read_tests;
#[cfg(test)]
mod hot_cache_scaling_tests;
#[cfg(test)]
mod entity_overlay_tests;
#[cfg(test)]
mod write_amplification_tests;
mod compaction_archive;
pub mod control;
pub mod control_ops;
mod conversation_header;
mod credential;
pub mod daemon;
pub mod embed;
pub mod embed_ffi;
mod embed_job_queue;
mod daily_cost;
mod daily_cost_migration;
mod desktop_egress_migration;
pub mod engine;
pub mod fastpath;
pub mod engine_log;
mod transcript_archive;
pub(crate) mod entity_overlay;
pub mod entity;
mod frozen_transcript;
#[doc(hidden)]
pub mod fsync_stats;
mod request_profile;
mod generated_conversation_search;
mod generated_paper_build;
pub mod generated_storage_operations;
mod generated_storage_v2;
mod generated_task_event_delivery;
pub mod generated_tofudb_ir;
pub mod generated_tofusql_catalog;
mod generated_unicode_casefold;
mod generated_unicode_simple_fold;
pub mod history;
mod history_payload;
mod indexed_stream;
mod integration_workspace;
mod invocation;
mod knowledge;
mod knowledge_migration;
pub mod listener;
mod log_aggregate;
mod log_aggregate_migration;
pub mod logical_outbox;
pub mod maintenance;
pub mod migration;
pub mod migration_session;
mod generated_model_context_projection;
mod model_continuation;
mod model_routing;
mod model_routing_migration;
mod optimizer;
mod optimizer_migration;
mod orchestration;
pub mod outbox_multi_sink;
pub mod outbox_publisher;
pub mod outbox_relay;
pub mod outbox_sink;
pub mod outbox_worker;
mod paper;
mod paper_artifact;
mod paper_budget_migration;
mod paper_build_artifact;
mod paper_build_receipt;
mod paper_document;
mod paper_library;
mod paper_migration;
mod paper_podcast;
pub mod payload_manifest;
mod payload_hints;
pub mod payload_segment;
pub mod prefetch;
mod plugin;
mod plugin_migration;
mod plugin_rows;
mod project_brain;
mod project_brain_migration;
pub mod protocol;
mod provider;
mod queue;
mod queue_migration;
mod rate_limit;
mod rate_limit_migration;
mod raw_archive;
pub mod receipt;
mod receipt_migration;
mod legacy_receipt;
mod recent_project;
pub mod release_signature;
mod research;
pub mod resource_gate;
pub mod resource_probe;
mod rotation_barrier;
mod scheduler;
mod scheduler_migration;
mod search_dirty;
mod search_text;
pub mod semantic;
pub mod semantic_executor;
mod response_json;
mod canonical_json_bytes;
pub mod sequencer;
pub mod server;
pub mod stream;
mod swarm;
mod task_result;
mod tenant_user;
mod timer;
mod timer_migration;
pub mod tofusql;
pub mod transaction;
pub mod transaction_ir;
mod turn;
mod turn_projection_patch;
mod turn_projection_segments;
pub mod turn_search_projection;
pub mod turn_search_worker;
mod versioned_document;
pub mod vfs;
pub mod wal;
mod wal_retirement;
mod automation_run;
mod automation_presentation;
mod automation_approval;
mod automation_project;
mod automation_skill_source;
mod automation_definition;
mod automation_events;
mod worker_job;
mod worker_lease_clock;
mod worker_job_migration;

pub const FORMAT_VERSION: u32 = 1;
mod authority_admission;
pub use generated_tofudb_ir::{ACTIVE_LOG_MAX_BYTES, ACTIVE_LOG_ROTATE_BYTES};

pub mod native_checkpoint_store;
