# Workspace acceptance evidence

Problem: a completed task, delivered workspace, or merged candidate did not
identify what had actually been checked. An old successful command could be
mistaken for evidence about a new source revision or changed acceptance rule.
Decision: project the real Git gate's version-bound result alongside execution
and delivery status. Ordinary workspace publication remains independent of
release checks; no test is re-executed merely to construct this projection.

`contracts/workspace_acceptance_v1.schema.json` is the wire authority.
`lib/project_mod/delivery_acceptance.py` owns construction and projection;
`integration_control._gate_commands` supplies actual observations. The record
binds the base commit, target commit/tree, workspace checkpoint, checker digest,
time, and two explicit checks: built-in validation and the configured project
command. Command text, output, credentials and local paths do not enter the
public receipt. Version 1 identifies the checker protocol, not a task verdict.

Only successful real checks on the immutable candidate can produce a verified
candidate. Built-in-only checks remain unverified for project acceptance. A
missing, malformed or legacy receipt is unverified. A failed gate is failed.
Changing the checkpoint or configured command makes previous evidence stale;
unreadable current policy cannot produce a verified projection. History remains
attached to its original version. The record says nothing about uncommitted
canonical edits, ignored dependencies, deployment, or correctness beyond the
configured checks. It is not human acceptance or a signature/trust chain.

The validation command runs in the existing private checkout. If it changes
tracked candidate source, even exit zero cannot certify the original candidate.
Console claims never override the observed process exit. Transient preparation,
checker or policy failures retain the ordinary gate failure behavior.

Records live in the existing owner-scoped workspace origin as
`acceptanceRecord`, before candidate publication or explicit adoption proceeds.
A failed evidence write cannot publish an unrecorded verified candidate.
Checkpoint identity is checked by the existing publication owner; replay and
visibility remain with that owner. SQLite and native TofuDB use the same named
storage operation. No second journal, worker, retry loop or approval grant is
introduced. Caller-supplied origin provenance cannot create a structured record.

The integration status response and integration center show candidate checks independently of task/workspace
state, includes the exact checked revision, and lists both check outcomes.
Delivered or merged rows without records remain visibly unverified. Acceptance
is scoped to the recorded candidate; it does not turn working-tree delivery
into a gated operation. Artifact/research/creative acceptance remains with its
own evaluator and is not inferred from these Git facts.

Alternatives rejected: parsing “tests passed” from model/output text creates
false evidence; re-running every test during automatic delivery delays ordinary
workspace publication; treating any completed stage or syntax check as whole
task acceptance invents coverage; a new acceptance database duplicates the
workspace authority. Tests exercise actual Git/process execution, source and
criterion changes, unknown records, owner isolation and storage faults, plus
the built application at both languages and desktop/mobile widths.

Resource boundary: each workspace retains one latest structured receipt in its
existing origin metadata, not an additional evidence history. Existing storage
command receipts and retention still apply. A 60-sample warm local fixture
measured 499 compact JSON bytes per record, evidence commit p95 1.28 ms (SQLite)
and 0.96 ms (TofuDB), and projection p95 0.18 ms per populated row. Gate recording
adds a target-tree lookup; immutable-source validation adds a Git diff. The
lookup alone measured 10–15 ms p95 on this host. These are isolated diagnostics,
not task throughput or end-to-end latency claims. No new resident workers,
unbounded cache, automatic delivery gate, or startup probe is introduced.
