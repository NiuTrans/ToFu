# Monorepo and release boundaries

This repository is the only source authority for the Tofu application and its
first-party Python artifacts. Source location, installable package, and runtime
process are separate boundaries: sharing one Git repository does not make an
optional capability part of the core wheel or force it into another process.

## Owned artifacts

| Source owner | Distribution | Runtime role | Version authority |
|---|---|---|---|
| repository root, `tofu_agent/`, selected `lib/` | `tofu-agent` | application source composition plus embeddable/headless agent | root `pyproject.toml` and `VERSION` |
| `packages/tofu-db/` | `tofu-db` (not yet released) | pre-authority personal storage-engine certification target | member `Cargo.toml` |
| `packages/tofu-search/` | `tofu-search` | standalone, lazily loaded search/fetch capability | member `pyproject.toml` |
| `plugins/tofu-trading/` | `tofu-trading` | optional application plugin discovered through entry points | member `pyproject.toml` |

The root application may consume the public `tofu-search` API and provider
seams. `tofu-search` never imports host modules. The host discovers
`tofu-trading`; core modules never import `tofu_trading`. Trading uses declared
host runtime/storage seams and owns durable records only in `tofu.trading`.

## Workspace and verification

`uv sync --all-packages` resolves first-party dependencies from the workspace.
Published metadata continues to contain normal version ranges, so every wheel
remains independently installable. Run the smallest member tests first:

```text
uv run --package tofu-search pytest packages/tofu-search/tests
uv run --package tofu-trading pytest plugins/tofu-trading/tests
python3 scripts/check_monorepo.py
```

Root gates then verify the integration adapters and full application. A source
change that affects a public member contract updates producer, consumer,
compatibility test, and version range together.

`tofu-db` remains outside application assembly and release assets while it is
pre-authority.  Its focused gate is
`cargo test --manifest-path packages/tofu-db/Cargo.toml`; promotion adds native
build artifacts and application compatibility gates in the same change.

## Releases

Use `search-vX.Y.Z` and `trading-vX.Y.Z` for workspace members. The established
agent release contract retains `vX.Y.Z` until its OCI/SDK promotion workflow is
versioned together. A tag builds only its owning member plus shared compatibility
gates. GitHub's `rangehow/ToFu` repository owns issues and development; retired
standalone repositories remain read-only historical pointers and never accept
new source changes.

## Checkout path

The canonical checkout basename is `tofu`, not `chatui`. Runtime code derives
the repository root and never embeds a developer-machine absolute path.
Project-local `.tofu/` and legacy `.chatui/` state move with the checkout and
remain untracked. When an existing checkout moves, repair linked worktrees and
use the public `project.relink` operation to atomically move recent-project,
active/recoverable conversation project-pin, and Project Brain identity.
The maintenance-lane command has a two-minute hard deadline, stops its owner-scoped
conversation scan at 10,000 candidates, and applies verified rewrites through
one backend batch per physical table.
Historical fixtures may retain `chatui` as sample data;
they are not launch paths or source authorities.

Active product copy and new identifiers use **Tofu**. Existing `.chatui/`,
`CHATUI_*`, `chatui_*`, database filenames, and explicitly legacy adapter names
remain only as compatibility surfaces; removing them would strand existing
installations. Their presence is not evidence of a second product or checkout.

After the filesystem move, run the bounded state migration in dry-run and
apply modes, repair Git's linked-worktree metadata, resync the workspace, and
replace the lifecycle recovery entry. The migration also updates a
checkout-local `.tofu_env.json`; if that marker deliberately selects an
external Conda environment, keep it only when that environment does not carry
editable installs from the retired sibling repositories.

```text
python3 scripts/migrate_checkout_path.py --old-root /old/chatui --new-root /new/tofu
python3 scripts/migrate_checkout_path.py --old-root /old/chatui --new-root /new/tofu --apply
git worktree repair
uv sync --all-packages --extra test
./.venv/bin/python serverctl.py install-recovery
```
