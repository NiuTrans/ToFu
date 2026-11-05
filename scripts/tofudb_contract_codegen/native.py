"""Assemble the native IR projection in its established output order.
Entry: render_native. Tables are indexed once. ContractInputs owns file I/O;
projections share that invocation's parsed documents."""

from .native_engine import render_engine
from .native_identity import render_identity
from .native_services import render_services
from .native_content import render_content
from .native_execution import render_execution
from .native_migration import render_migration_suffix


def render_native(document, worker, inputs) -> str:
    tables = {table["name"]: table for table in document["logicalTables"]}
    lines = []
    lines.extend(render_engine(document, tables))
    lines.extend(render_identity(tables))
    lines.extend(render_services(tables))
    lines.extend(render_content(tables))
    lines.extend(render_execution(tables, worker, inputs))
    lines.extend(render_migration_suffix(document))
    return "\n".join(lines) + "\n"
