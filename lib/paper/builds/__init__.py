"""Paper build domain: frozen sources, compilation and immutable receipts.

Compiler and source modules are transport-independent and can execute on the
host or Desktop Agent. HTTP, task scheduling and provider adapters depend on
this domain, never the reverse. Wire authority: paper_build_v1.schema.json.
"""
