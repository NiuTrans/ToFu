# GitHub metadata and workflow guidance

## Scope

This directory owns repository automation, release workflows, and issue intake.
Runtime and deployment semantics remain with their source modules and contracts.

## Editing rules

- Keep workflow permissions least-privilege and job inputs explicit. Never
  expose repository secrets to logs, artifacts, pull-request code, or forked
  workflows.
- Preserve reproducibility: use repository-owned scripts for build logic,
  retain pinned tool/action versions, and make produced artifact names match
  the runtime defaults and release tests that consume them.
- Keep platform release streams independent where their artifact lifecycles
  differ, notably Android and desktop.
- Treat matrices, caches, concurrency, retention, and uploaded artifacts as
  bounded resource policy. Do not hide required gates behind best-effort steps.
- Issue forms collect actionable, non-secret reproduction data; they do not
  redefine support or product contracts.

## Verification

Run the repository test that owns the changed workflow, such as
`tests/test_desktop_build_workflow.py`, `tests/test_mobile_client_apk_url.py`,
or `tests/test_installer_parity.py`. Inspect the rendered YAML diff and run the
same repository script invoked by the affected job when locally available.
