//! Native process/runtime ownership for the Rust-primary Tofu migration.
//! Domain services supply explicit resource budgets and owned job closures.
//! This crate owns no conversation, storage authority, provider or user globals.
pub mod anthropic;
pub mod anthropic_request;
pub mod executor;
pub mod reactor;
pub mod model_content;
pub mod responses;
pub mod responses_request;
pub mod html_projection;
#[cfg(feature = "html-projection-abi")]
pub mod html_projection_abi;

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
pub mod usage_aggregate;

pub mod model_account;

pub mod token_ingress;

pub mod progress;

pub mod agent_checkpoint;

pub mod checkpoint_journal;
pub mod checkpoint_lineage;
pub mod checkpoint_goal_context;
pub mod checkpoint_goal_policy;
pub mod checkpoint_goal_codec;
pub mod checkpoint_goal_claim;
pub mod checkpoint_goal_consume;
pub mod checkpoint_control;
pub mod checkpoint_writer;

pub mod model_admission;

pub mod pricing;

pub mod pricing_usage;

mod pricing_unicode;

pub mod pricing_tiers;

pub mod billing_credits;
mod billing_contract;

mod token_rank_counter;

mod token_merge_heap;

mod token_merge_linear;
mod token_pair_ranks;
mod chat_wire;
mod chat_content;
pub mod model_tool_wire;
pub mod prepared_tool_wire;

#[cfg(feature = "token-count-abi")]
pub mod token_count_abi;

#[cfg(feature = "flow-abi")]
pub mod flow_graph;
#[cfg(feature = "flow-abi")]
pub mod flow_abi;

#[cfg(feature = "flow-abi")]
pub mod verdict_policy;

#[cfg(feature = "flow-abi")]
pub mod flow_loop;

#[cfg(feature = "flow-abi")]
pub mod flow_branch;

#[cfg(any(feature = "flow-abi", feature = "checkpoint-storage-abi"))]
mod joined_calls;
#[cfg(feature = "checkpoint-storage-abi")]
pub mod checkpoint_storage_abi;
