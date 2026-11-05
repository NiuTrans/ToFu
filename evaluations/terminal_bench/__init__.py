"""Native Terminal-Bench evaluation on CodeLab sandbox VMs without Harbor or agentix.

Responsibility map:
- ``harness``: task loading, protocols, agent launch specs, trial spec + task bundle.
- ``vm_trial``: stdlib-only trial executor uploaded into each sandbox VM.
- ``gateway``: pssdk session lifecycle and the ingress native exec channel.
- ``runner``: concurrent orchestration, resumable attempts, ``summary.json``.
- ``relay``: key-holding model relay shared by every arm (same model, keys, sampling).
- ``audit``: benchmark-answer contamination audit over trial evidence.
- ``report``: merged raw/strict scores over the planned task set.
CLI: ``python -m evaluations.terminal_bench --help``; workflow in ``README.md``.
"""
