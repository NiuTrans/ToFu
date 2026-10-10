//! Native process/runtime ownership for the Rust-primary Tofu migration.
//! Domain services supply explicit resource budgets and owned job closures.
//! This crate owns no conversation, storage authority, provider or user globals.
pub mod anthropic;
pub mod anthropic_request;
pub mod executor;
pub mod model_content;
pub mod responses;
pub mod responses_request;

pub mod execution_contract;
pub mod session;

pub mod task;

pub mod sse;

pub mod provider;

pub mod http;

pub mod tool_policy;

pub mod tool_assembly;

pub mod tool_contract;

pub mod tool_execution;

pub mod process_launch;

#[cfg(target_os = "linux")]
pub mod process_supervisor;

#[cfg(target_os = "linux")]
pub mod process_boundary;

pub mod agent;

pub mod agent_http;

pub mod tool_scheduler;

pub mod approval;

pub mod capacity;

pub mod capacity_allocation;

pub mod round_admission;

pub mod usage;

pub mod model_account;

pub mod token_ingress;

pub mod progress;

pub mod agent_checkpoint;

pub mod checkpoint_journal;

pub mod model_admission;
