# Pre-registration: forced-operation edit benchmark (edit_file / write_file)

Registered 2026-11-06 before any benchmark model call. Owner question: does
`edit_file`'s insert mode save tokens without costing correctness, and how do
Tofu's anchored edits compare with Claude Code `Edit` and Codex `apply_patch`
when the model is told which edit style to use?

Connectivity smoke probes ("call the record tool with the word apple", no task
content) ran before registration; they chose the route (`tool_choice=auto`,
because deepseek-v4-pro returns 502 for `required`) and the models.

## Reconciliation with the 10-tools lane (ba1f3cf0)

That lane proposed 4 arms x 60 tasks x 3 models x 3 repeats; this design is the
same. Its trajectory measurements (insert costs +1 token of operation skeleton
and wins from a 2-token anchor; 4 of 2,891 production edits used insert; 17 of
210 TB2.1 edit_file calls rejected for a missing `operation`) are context, not
re-measured here. This benchmark adds what trajectories cannot show: forced use.

## Experiment 1: edit styles

### Tasks (`tasks.jsonl`, 60 tasks; built by `build_tasks.py` from `task_specs.py`)

Six real files from public repositories at pinned commits (no internal code):
psf/requests `src/requests/auth.py`, pallets/click `src/click/formatting.py`,
expressjs/express `lib/view.js`, spf13/cobra `flag_groups.go`, rust-lang/log
`src/kv/key.rs`, sindresorhus/ky `source/utils/body.ts` (Python, JS, Go/tabs,
Rust, TS/tabs). Ten tasks per file. Each task has a natural-language
instruction that quotes the exact new text and names line numbers, and an exact
expected file (sha256 in `tasks.jsonl`).

Edit type x anchor stratum (counts):

| type | S | M | L |
|---|---|---|---|
| insert (pure insertion; 12 after, 12 before) | 8 | 8 | 8 |
| replace | 4 | 4 | 4 |
| delete | 4 | 4 | 4 |
| multi (2 sites, at least one other edit, one file) | 4 | 4 | 4 |

Stratum rules (derived by `build_tasks.py`, which refuses a mismatch):
insertion sites use the adjacent line (the line after which / before which text
goes): S = that whole line is unique in the file and its stripped text is <= 2
o200k tokens; M = unique and >= 3 tokens; L = the whole line occurs more than
once or is blank, so a unique anchor needs more than one line. Span edits:
S = changed/deleted fragment <= 2 tokens, else M. Whole-line edits: one line
= M (S if <= 2 tokens), 2+ lines = L. Multi = the maximum of its sites.

Offline oracle (`oracle.py`, `oracle.jsonl`): a minimal-context correct call for
every task x arm, run through the same executors, reproduces all 240 expected
files. Its payload size is the per-arm token floor.

### Arms (`arms.json`, frozen with sha256)

All arms see the same system prompt (except one "edit style" sentence) and the
same user message: the file with `cat -n`-style line numbers, then the
instruction. One tool is offered per arm.

- **A1 tofu_replace**: production `edit_file` schema (lib/tools/project.py at the
  registered commit, sha256 in arms.json) with `operation` enum narrowed to
  `["replace"]`; insert sentences and the write_file pointer removed.
- **A2 tofu_insert**: the same schema, all three operations, wording switched to
  insert-first (insert examples instead of the replace examples). On
  replace/delete tasks A2 must still use replace: those strata are a placebo
  control for the wording change.
- **A3 cc_edit**: Claude Code 2.1.207 `Edit` (file_path, old_string, new_string,
  replace_all), description reconstructed from the shipped bundle.
- **A4 codex_patch**: Codex `apply_patch` patch language (vendored codex
  5c5308fc9a9e: gpt_5_1_prompt.md apply_patch section + apply_patch.lark),
  wrapped as a JSON function `{input}` because these providers have no freeform
  grammar tools.

Execution: A1/A2 run Tofu's real front door and executor
(`lib.tool_input_repair.ingest_tool_call` with the offered schema as the
ToolContractV2 document, then unwrapped `tool_edit_file`) in a hermetic worker
on scratch copies. A3/A4 run Python ports pinned by `verify_cc_edit.py` (12
cases from the CC bundle) and `verify_codex_patch.py` (23/25 Codex scenario
fixtures; the two CRLF fixtures are out of scope because all task files are LF).

### Models and procedure (`run_bench.py`)

glm-5.3, deepseek-v4-pro, claude-opus-5 through Tofu's own OpenAI-compatible
route (`/v1/chat/completions`, our keys), `tool_choice=auto`, `max_tokens=16000`,
provider-default reasoning and temperature. 3 repeats. Attempt order is a fixed
shuffle (seed 20261106). One attempt: up to 3 model calls; after each response
all its tool calls run in order and their results are returned; stop when the
file equals the expected file, when the model answers without a tool call, or
after 3 calls. HTTP/transport errors are retried 5 times with backoff and never
count as model failures; attempts still failing are reported as missing.

Pilot: tasks A01, K03, F06, T08, V10 x 4 arms x glm-5.3 x 1 repeat. Pilot data
only debug the harness and are excluded from the analysis. Any harness change
after the pilot is listed under Deviations below before the main run.

### Metrics

Primary:
1. Final success: final file byte-identical to expected.
2. First-try success: file identical after the first response's tool calls.
3. Edit payload tokens: o200k_base tokens of all tool-call argument strings in
   the attempt (and of the first response alone). For A4 also the patch text
   alone (freeform-equivalent).

Secondary: provider-reported completion tokens (incl. reasoning) per attempt and
per first call; reasoning tokens where reported; model calls per attempt;
schema/validation failures (first-call tool calls refused before execution:
Tofu contract rejections such as a missing `operation` or an enum violation, CC
input validation, Codex parse errors); failure taxonomy.

Failure taxonomy (first failing tool call, and final state when wrong):
schema/validation; anchor not found; non-unique anchor; echo refused (Tofu);
wrong side (insert on the other side of the anchor); anchor echoed into the
file (duplicated anchor lines); whitespace-only difference; other wrong
content; no change; path error. Rules:
- tool-call errors: refused before execution (Tofu `admitted=false`; CC input
  validation, bad JSON or unknown tool; Codex `invalid patch`/`invalid hunk` or
  missing `input`) = schema; Tofu `[match-not-found]`, CC "String to replace
  not found", Codex "Failed to find expected lines|context" = anchor not found;
  Tofu `[ambiguous-match]`, CC "Found N matches" = non-unique; Tofu
  `[echo-content]` = echo refused; anything else = other tool error.
- wrong final files: unchanged = no change; for a one-site insertion, equal to
  the original with the text on the other side of the adjacent line = wrong
  side; equal to expected plus 1-3 duplicated adjacent original lines at the
  site = anchor echoed; equal to expected after removing all whitespace and
  blank lines = whitespace; otherwise other.

### Analysis

Unit = attempt; cluster = task (60 clusters). 95% CIs by task-cluster bootstrap
(10,000 resamples of tasks with replacement within the analysed subset,
percentile intervals; all attempts of a sampled task enter together).

1. Per arm x model and pooled over models (equal model weight): final success,
   first-try success, payload tokens, provider completion tokens.
2. Paired A2 - A1 within each stratum (edit type x anchor class) and pooled per
   edit type, pairing on (model, task, repeat): success difference and token
   difference with cluster-bootstrap CIs. Hypotheses: on insert tasks A2 uses
   fewer payload tokens and the gap grows S < M < L (oracle floor: 2.3 / 11.0 /
   17.1 tokens); success difference CI reported without a non-inferiority claim;
   on replace/delete (placebo) the difference should include 0.
3. A3 - A1, A4 - A1, A4 - A3 and A3 - A2 paired the same way, by edit type.
4. Failure taxonomy counts per arm x model.
Token summaries are reported over all attempts and over successful attempts.
No other test is primary; anything else is labelled exploratory.

## Experiment 2: write_file modes

Modes verified in code at registration (lib/tools/project.py:142-210;
handler lib/tasks_pkg/handlers/project.py:458-479; resolver
lib/tasks_pkg/executor/_content_ref.py): `content` (whole-file replacement),
`append: true` (+content), `content_ref {tool_round, start, end}` (text of a
previous tool round, character slice). Arms are forced the same way as Exp 1
and each mode is compared with the plain full rewrite on the same tasks:

- E2a append vs full rewrite, log/report growth: append a given section to a
  public changelog-style document at three sizes.
- E2b content_ref vs inline content: save a previous tool round's output (whole
  output, and a slice between marker lines) to a file.
- E2c large-file mid-file edit: full rewrite vs edit_file (A1 schema).

Same models, repeats, success/token metrics and bootstrap as Exp 1. The
concrete E2 task lists are appended to this file as a dated amendment before
the first E2 model call; Exp 1 data cannot influence them.

## Deviations

- 2026-11-06, after the pilot, before the main run: `run_bench.py` also stores
  the final file text of failed attempts (logging only; used by the final-state
  taxonomy). `analyze.py` (implementing the plan above) is committed with it.
- Pilot (glm-5.3, 5 tasks x 4 arms, excluded from analysis): 19/20 attempts
  succeeded on the first call. The failure is A2 on K03 (insert after a
  duplicated `}` line, new block starting with a blank line): 16,000 completion
  tokens of reasoning about whether insert content is line- or raw-text-based,
  then finish=length with no tool call. Counted as a failure under the
  registered stop rule; arms and budgets are unchanged.
- 2026-11-06 17:48 and 18:15, during the main run: our requests drew 429s
  (per-minute limits; the Claude deployment is shared with another project's
  daily evaluation) and HTTP 402 credit-exhausted keys, so concurrency went
  from 4 per model to 2 (Claude 1). Order, prompts and budgets are unchanged;
  the runner is resumable, so this only changes pacing.
- 2026-11-06 18:56, model substitution: deepseek-v4-pro stopped. From 17:29
  most of its keys answered HTTP 402 (credit exhausted) and the rest 429 with
  shared-project contention; by 18:55 only 31 of 720 attempts had finished,
  most after 1-5 server-side retries, and our traffic competed with other
  projects for the last keys. kimi-k3 (the brief's other allowed third model)
  replaces it with the identical protocol. The DeepSeek attempts are reported
  separately and excluded from the pooled analysis; Exp 2 uses the same three
  models (glm-5.3, claude-opus-5, kimi-k3).
- Exp 2 pacing: E2 for glm-5.3 ran at 1 then 3 attempts in parallel (after its
  Exp 1 run finished); claude-opus-5 at 1; kimi-k3 at 1 then 3. Exp 2 attempts
  of deepseek-v4-pro (13) and 3 early duplicate claude-opus-5 attempts are
  excluded or superseded (the later record per key is kept).
- Exp 2 task defect found after the runs (post hoc): the three markdown-log
  E2a tasks expect the file to end with the appended entry plus a trailing
  blank line (the fenced block ended with an empty line), while the instruction
  says only "the file must end with a newline". All three models wrote one final
  newline or reasoned about the ambiguity until max_tokens, in both arms alike.
  E2a is reported as registered and, separately, without the three md tasks
  (both arms drop together, so the paired comparison is unaffected); a lenient
  score that ignores trailing blank lines is reported as sensitivity.
- Exploratory, not registered: kimi-k3 sent write_file calls with only `path`
  in most Exp 2 calls (write_file requires only `path` because content and
  content_ref are alternatives). A 12-attempt probe (Wfull schema as frozen vs
  with `content` added to `required`, two E2a tasks x 3 repeats) is reported
  separately (`raw/probe_kimi_required.jsonl`).

## Amendment 1 (2026-11-06, before any Experiment 2 model call)

Exp 2 tasks (`tasks/write_tasks.jsonl`, `build_write_tasks.py`; seeded synthetic
logs plus public source files), arms (`tasks/write_arms.json`, built by
`run_write.py --build-arms` from the production write_file schema) and oracle
(`oracle_write.py`: 42/42 correct calls reproduce the expected files).

- E2a, 9 tasks: markdown run log / CSV metrics log / service log at S/M/L
  (about 0.4K / 1.5-2.2K / 5.6-7.8K o200k tokens of existing content); append
  a given 6-8 line block. Wfull vs Wappend.
- E2b, 6 tasks: a previous `run_command` round (Tofu's `$ cmd ... [exit code: 0]`
  framing) printed a pytest log, a CSV report or a JSON summary; save the whole
  program output or only the lines between two marker lines to a new file.
  Wfull (inline content) vs Wref (content_ref). The prompt states "tool round 1";
  whether production surfaces round numbers to models is not verified, so this
  is the best case for content_ref.
- E2c, 6 tasks: six public files of 555-1,050 lines; insert one comment line
  after a unique line at or after the middle. Wfull vs Eedit (= Exp 1 A1).

Arms: Wfull = production write_file without `append`/`content_ref`; Wappend =
without `content_ref`; Wref = without `append`; Eedit = Exp 1 A1. Execution:
Tofu's ingest_tool_call, then lib.project_mod.tools._exec_write_file (the
model-facing text), with content_ref resolved by the production resolver exactly
as lib/tasks_pkg/handlers/project.py does; Eedit runs tool_edit_file.
Same models, repeats and stop rule; max_tokens 32000 (full rewrites reach ~10K
tokens). Metrics: final and first-try success, payload o200k, provider completion
tokens, latency; paired (mode - Wfull) per sub-experiment with the task-cluster
bootstrap; for Wref also the rate of calls that used content_ref and of wrong
slice offsets. E2 runs alongside the Exp 1 main run at 2 attempts per model.
