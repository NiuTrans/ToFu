# Desktop agent guidance

## Scope

This package is the connected-device runtime that executes explicitly addressed
desktop, project, worktree, and egress commands. The server bridge owns routing;
`lib/project_mod/` owns local project write semantics.

## Editing rules

- Validate command version, capability, owner/device addressing, project root,
  and permission before execution. Treat every server-supplied path, URL,
  argument, and payload as untrusted.
- Keep dispatch thin and route each command to one focused implementation.
  Server and agent wire shapes evolve together with explicit compatibility.
- Process execution has explicit cwd/environment, time, output, process-tree,
  and cancellation bounds. Parent death and disconnect terminate owned work.
- Remote worktree/file operations enforce root containment, symlink safety,
  freshness, atomic writes, and rollback just like local project tools.
- Egress is allow-list driven and revalidated across resolution/redirect/failover.
  Never expose bridge/provider credentials to commands or logs.
- Stream progress and settle exactly once; retain enough attribution for retry
  without replaying committed side effects.

## Verification

Run focused desktop-agent dispatch/project/exec/egress tests plus server-agent
wire parity, streaming cancellation, root safety, and pairing integration tests.
