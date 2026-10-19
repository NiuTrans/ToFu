# 对话即协作 · Sidebar Coordinator

**Status: interactive design prototype, not shipped application behavior.**
All conversations, model identities, budgets, results and receipts are synthetic.
Nothing here calls the application API, creates a real task, grants paid authority,
changes coordinator binding, reads/writes project files or changes production storage.

## Problem / decision / alternatives

**Problem:** the project workspace adds a second interaction model to a conversation
product. A broad `Recent outcomes: 100` counter competes with the answer without
identifying a concrete decision. Workers live in conversations but coordination
asks the user to enter a separate management surface.

**Decision:** put one explicit Coordinator at the top of the current project's
existing conversation list. It opens ordinary chat. One inline task card connects
ordinary worker conversations and presents the next necessary decision. Keep
project identity as scope, not a new dashboard. A small star-orbit mark identifies
the role; amber is reserved for decisions, with quiet green/blue worker accents,
soft borders, a bounded reading column and a fixed composer.

**Alternatives rejected:** moving the entire workspace into a sidebar drawer still
requires a second workbench; an always-open board competes with the conversation;
adding every ephemeral sub-agent to the sidebar makes it an execution log;
using pinning or renaming as coordinator binding creates hidden authority.

## Open and try

Open **`preview.html`** directly in a browser. It is an offline single-file artifact:
fonts, styles and the generated real UI factories are embedded. No server is needed.
`index.html` is the retained development shell, derived from the UI-kit scaffold.
It needs ordinary static hosting because it imports an ES module.

Suggested journey:

1. Open pinned Coordinator. Hover/focus a worker in the task card: the corresponding
   sidebar entry highlights too. Search the local example list without losing the
   Coordinator anchor.
2. Open a worker and return using `返回 Coordinator`. Composer drafts stay in their
   own conversation. `新对话` creates an independent in-page example, not a real one.
3. Review the research result. Accept records review only; propose changes prepares
   a draft and never starts rework.
4. Confirm the exact finite execution scope. Consent is mandatory and editing the
   scope clears it. Only the declared example path is confirmable. Tokens are not
   a guaranteed currency ceiling. Every confirmation is explicitly marked demo.
5. Use `外观与演示` → `模拟执行结果到达` to advance the synthetic executor. Execution,
   review and delivery remain separate. Accept the implementation, submit the
   pinned publication request, then separately simulate the exact receipt.
6. Use the same preview controls to try unbound Coordinator, uncertain authorization
   and a refused publication. Reconciliation retains the original example command;
   the blocked state names the missing project gate and never starts downstream work.
7. From a running worker, withdraw only its current authorization. The example
   shows withdrawal separately from unknown cancellation acknowledgment. No global
   stop, automation-disable acknowledgment or provider revocation is claimed.
8. At narrow widths, use the header list button; the conversation list occupies the
   screen and background controls are inert. Escape closes it; modal panels trap
   Tab/Shift+Tab and restore focus. Enter sends, Shift+Enter inserts a newline.

The prototype stores bounded in-page data only, and refresh resets it. No real
provider streaming, cross-device synchronization or durable reload recovery exists.
Sidebar search is deliberately fixture-only, not a repair of production search.

## Owners and regeneration

- `index.html`: scaffold-derived semantic shell.
- `prototype.css`: layout/novel visuals; existing tokens only. No UI-kit overrides.
- `prototype.js`: local interaction simulation; controls use UI factories.
- `verify.py`: narrow real Chromium visual/interaction specification and screenshots.
- `check-style.mjs`: reuse of the authoritative repository token policy.
- `build-preview.py`: single-file artifact generation; no parallel application build.
- `ui-kit.bundle.js`, `preview-cjk.woff2`, `preview.html`, `previews/*`: generated.
- `OFL.txt`: copyright and license for the preview-only installed CJK font subset.

```sh
python3 designs/sidebar-coordinator/verify.py
python3 designs/ui-gallery/verify.py
python3 designs/ui-gallery/capture.py
npm run check:styles
```

The first command rebuilds the UI factories, preview font and standalone HTML.
It serves only an exact allowlist of required public static assets on ephemeral
loopback, refuses unrelated source files and API/external/non-GET requests,
launches Chromium via the shared environment helper and tears down the
browser/server. It also opens the real single-file artifact via `file://`.

## Evidence and limitations

`previews/verification.json` records the current browser checks. It covers tofu,
dark and light × 1440/390/320px, initial and action-focused screenshots, worker
navigation, draft retention, search, correlated hover, literal user text, multiline
input, accessible modal names, modal geometry/focus, visible focus after mobile
scene/result changes and breakpoint crossings, finite consent and the separate
result/review/delivery ladder. The normal path and uncertain/unbound/withdrawn/blocked cases are simulation
proof only. Zero backend or paid-provider calls is an enforced test condition.

The existing gallery structural and capture tiers passed during design work.
Its contrast report still warns about existing light/tofu primary-button colors;
this prototype uses secondary confirmation controls instead. The repository-wide
style gate reports ten existing violations in unmodified
`frontend/src/styles/application/09-command-output.css`; the prototype's own CSS
passes the **same** authoritative token policy. This is not a claim of full-repo
or complete accessibility certification.

The gallery's default server-font capture also lacks CJK glyphs on this rootless
host; its screenshots are layout/component evidence only. The Coordinator
prototype separately embeds a licensed bounded CJK subset and was visually
inspected with readable Chinese glyphs.

## Future application integration, not implemented here

Use the current owner-scoped binding, conversation list/turn state and dispatch
contracts. Pin placement must stay separate from the server-bound role; do not
reuse the legacy pin-to-folder migration as binding. Fetch bound Coordinator
metadata independently of a partially loaded local catalog, and never turn a
missing/unavailable binding into an implicit worker promotion. Preserve command
identity across uncertain acknowledgment, revision/epoch/source checks, finite
budget accounting, Stop/revocation, exact checkpoint gates and durable delivery
receipts. Current production project-workspace UI is unchanged by this prototype.
