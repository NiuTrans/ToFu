# Platform evaluation launcher

Read README.md first. request.schema.json owns frozen worker inputs; catalog,
task locks and Harbor CLI own benchmark semantics. source_identity.py verifies
Git exports. Never copy a mutable checkout, user data or private providers.

The optional tofu-environments JobService owns scope, approval, CAS, uncertain
writes, cancellation and collection. Do not add another platform state machine.
Worker success means diagnostic outputs exist, never a benchmark score.

Verify tests/evaluations/test_platform_jobs.py, neighboring Harbor contracts and
packages/tofu-environments/tests. Live allocation requires the provider's exact
approved plan. Archives and trial outputs remain ignored artifacts.
