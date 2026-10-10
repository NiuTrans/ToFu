#!/usr/bin/env python3
"""Generate the Tofu-DB TofuSQL catalog from the canonical catalog contract.

The catalog declares which physical entity namespaces are addressable through
the read-only SQL front, how each logical entity key decodes into key-part
columns, and the per-statement resource ceilings.  Namespace names are
cross-checked against contracts/tofusql_catalog_v1.json's sibling IR contract
so catalog drift fails here, before dispatch code compiles.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts" / "tofusql_catalog_v1.json"
IR_SOURCE = ROOT / "contracts" / "tofudb_ir_v1.json"
OUTPUT = ROOT / "packages" / "tofu-db" / "src" / "generated_tofusql_catalog.rs"

EXPECTED_CEILINGS = {
    "maximumQueryTextBytes": 16384,
    "maximumEntityReadsPerStatement": 1000,
    "maximumResultRows": 2000,
    "maximumResponseBytes": 8388608,
    "maximumBodyHydrationBytes": 67108864,
    "maximumStatementMilliseconds": 30000,
}

EXPECTED_KEY_ENCODINGS = {
    "text": "u16 big-endian byte length followed by UTF-8 bytes; equality pushdown only because encoding order is length-major",
    "restText": "remaining raw UTF-8 bytes; must be the final key part; equality and range pushdown because encoding order equals BINARY collation",
    "orderedBinary": "remaining raw key bytes that must not decode as one logical value (opaque, usually a tag, text prefix, or fixed-width big-endian integer); must be the final key part; equality pushdown only, and values are addressable through the tofusql_key() table function as a lowercase hex string",
}

IDENTITY_PATH_PATTERN = re.compile(r"^\$\.[A-Za-z_][A-Za-z0-9_]*$")

RESERVED_COLUMN_NAMES = {"version", "updated_at_ms", "body"}

EXPECTED_VALUE_COLUMNS = [
    {
        "name": "version",
        "type": "INTEGER",
        "source": "document envelope version",
    },
    {
        "name": "updated_at_ms",
        "type": "INTEGER",
        "source": "document envelope update timestamp in milliseconds",
    },
    {
        "name": "body",
        "type": "TEXT",
        "source": "raw stored entity value decoded as JSON when it decodes and null otherwise; versioned document envelopes hydrate their document JSON through the bounded path charged against maximumBodyHydrationBytes",
    },
]


def _constant_name(name: str) -> str:
    characters: list[str] = []
    for character in name:
        if character.isupper() and characters:
            characters.append("_")
        characters.append(character.upper())
    return "".join(characters)


def _ir_namespaces() -> set[str]:
    document = json.loads(IR_SOURCE.read_text(encoding="utf-8"))
    if document.get("format") != "tofu.database-ir.v1":
        raise ValueError("tofudb_ir_v1.json has an unexpected format")
    namespaces: set[str] = set()
    for table in document.get("logicalTables", []):
        for field, value in table.items():
            if field.endswith("Namespace") and isinstance(value, str):
                namespaces.add(value)
    if not namespaces:
        raise ValueError("tofudb_ir_v1.json declares no physical namespaces")
    return namespaces


def _load() -> dict:
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    if document.get("format") != "tofu.tofusql-catalog.v1":
        raise ValueError("tofusql_catalog_v1.json has an unexpected format")
    if not isinstance(document.get("catalogVersion"), int) or document["catalogVersion"] < 1:
        raise ValueError("tofusql catalog needs a positive integer catalogVersion")
    if document.get("statementCeilings") != EXPECTED_CEILINGS:
        raise ValueError("tofusql statement ceilings contract is incomplete")
    if document.get("keyEncodings") != EXPECTED_KEY_ENCODINGS:
        raise ValueError("tofusql key-encoding contract is incomplete")
    codec = document.get("valueCodec")
    if (
        not isinstance(codec, dict)
        or codec.get("name") != "value"
        or codec.get("columns") != EXPECTED_VALUE_COLUMNS
    ):
        raise ValueError("tofusql value codec contract is incomplete")
    tables = document.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("tofusql catalog needs at least one table")
    names = [table.get("table") for table in tables]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("tofusql tables must be sorted and unique")
    namespaces = _ir_namespaces()
    for table in tables:
        namespace = table.get("namespace")
        if namespace not in namespaces:
            raise ValueError(
                f"tofusql table {table.get('table')} references namespace "
                f"{namespace!r} that the IR contract does not declare"
            )
        key_parts = table.get("keyParts")
        if not isinstance(key_parts, list) or not key_parts:
            raise ValueError(f"tofusql table {table['table']} needs key parts")
        part_names = [part.get("name") for part in key_parts]
        if len(part_names) != len(set(part_names)) or not all(part_names):
            raise ValueError(f"tofusql table {table['table']} key parts must be named and unique")
        for index, part in enumerate(key_parts):
            encoding = part.get("encoding")
            if encoding not in EXPECTED_KEY_ENCODINGS:
                raise ValueError(
                    f"tofusql table {table['table']} key part {part.get('name')} "
                    f"has an unknown encoding"
                )
            if encoding in ("restText", "orderedBinary") and index != len(key_parts) - 1:
                raise ValueError(
                    f"tofusql table {table['table']} trailing key part "
                    f"{part.get('name')} with encoding {encoding} must be the final key part"
                )
        identity_columns = table.get("identityColumns", [])
        if not isinstance(identity_columns, list):
            raise ValueError(f"tofusql table {table['table']} identityColumns must be a list")
        identity_names = [column.get("name") for column in identity_columns]
        if len(identity_names) != len(set(identity_names)) or not all(identity_names):
            raise ValueError(
                f"tofusql table {table['table']} identity columns must be named and unique"
            )
        for column in identity_columns:
            name = column["name"]
            if name in part_names or name in RESERVED_COLUMN_NAMES:
                raise ValueError(
                    f"tofusql table {table['table']} identity column {name!r} collides with "
                    "a key-part or value column"
                )
            path = column.get("path")
            if not isinstance(path, str) or not IDENTITY_PATH_PATTERN.match(path):
                raise ValueError(
                    f"tofusql table {table['table']} identity column {name!r} needs a "
                    "single-field JSON path of the form $.field"
                )
            if set(column) != {"name", "path"}:
                raise ValueError(
                    f"tofusql table {table['table']} identity column {name!r} has unknown fields"
                )
    edges = document.get("joinEdges", [])
    if not isinstance(edges, list):
        raise ValueError("tofusql joinEdges must be a list")
    by_name = {table["table"]: table for table in tables}
    seen_edges: set[tuple] = set()
    canonical: list[tuple] = []
    for edge in edges:
        if set(edge) != {"left", "right"}:
            raise ValueError("tofusql join edge needs exactly left and right endpoints")
        endpoints = []
        for side in ("left", "right"):
            endpoint = edge[side]
            if set(endpoint) != {"table", "column"}:
                raise ValueError("tofusql join edge endpoint needs exactly table and column")
            table = by_name.get(endpoint["table"])
            if table is None:
                raise ValueError(
                    f"tofusql join edge references unknown table {endpoint['table']!r}"
                )
            columns = {part["name"] for part in table["keyParts"]}
            columns.update(
                column["name"] for column in table.get("identityColumns", [])
            )
            if endpoint["column"] not in columns:
                raise ValueError(
                    f"tofusql join edge column {endpoint['table']}.{endpoint['column']} is "
                    "not a key part or declared identity column"
                )
            endpoints.append((endpoint["table"], endpoint["column"]))
        if endpoints[0][0] == endpoints[1][0]:
            raise ValueError("tofusql join edges must connect two distinct tables")
        pair = tuple(sorted(endpoints))
        if pair in seen_edges:
            raise ValueError(f"tofusql duplicate join edge {pair}")
        seen_edges.add(pair)
        canonical.append(pair)
    if canonical != sorted(canonical):
        raise ValueError("tofusql joinEdges must be sorted by (left, right) endpoint")
    return document


def _rust_variant(encoding: str) -> str:
    return {"text": "Text", "restText": "RestText", "orderedBinary": "OrderedBinary"}[encoding]


def _render(document: dict) -> str:
    ceilings = document["statementCeilings"]
    lines = [
        "// Generated by scripts/gen_tofusql_catalog.py; do not edit.",
        "",
        f"pub const TOFUSQL_CATALOG_VERSION: u32 = {document['catalogVersion']};",
    ]
    for field, value in ceilings.items():
        name = _constant_name(field)
        lines.append(f"pub const TOFUSQL_{name}: usize = {value};")
    lines += [
        "",
        "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
        "pub enum TofuSqlKeyEncoding {",
        "    Text,",
        "    RestText,",
        "    OrderedBinary,",
        "}",
        "",
        "impl TofuSqlKeyEncoding {",
        "    /// Range predicates may only push down when the encoded byte order",
        "    /// matches SQLite BINARY collation for the decoded value.",
        "    pub const fn supports_range_pushdown(self) -> bool {",
        "        matches!(self, TofuSqlKeyEncoding::RestText)",
        "    }",
        "}",
        "",
        "#[derive(Clone, Copy, Debug)]",
        "pub struct TofuSqlKeyPart {",
        "    pub name: &'static str,",
        "    pub encoding: TofuSqlKeyEncoding,",
        "}",
        "",
        "/// A documented single-field JSON path out of the decoded body, surfaced as a",
        "/// nullable TEXT column so joins can correlate identities that live inside",
        "/// documents rather than keys. `path` is the field name (the `$.field`",
        "/// contract path with the prefix stripped).",
        "#[derive(Clone, Copy, Debug)]",
        "pub struct TofuSqlIdentityColumn {",
        "    pub name: &'static str,",
        "    pub path: &'static str,",
        "}",
        "",
        "#[derive(Clone, Copy, Debug)]",
        "pub struct TofuSqlTable {",
        "    pub name: &'static str,",
        "    pub namespace: &'static str,",
        "    pub key_parts: &'static [TofuSqlKeyPart],",
        "    pub identity_columns: &'static [TofuSqlIdentityColumn],",
        "}",
        "",
        "impl TofuSqlTable {",
        "    /// A join-visible column: a key part or a declared identity column.",
        "    pub fn has_join_column(&self, name: &str) -> bool {",
        "        self.key_parts.iter().any(|part| part.name == name)",
        "            || self",
        "                .identity_columns",
        "                .iter()",
        "                .any(|column| column.name == name)",
        "    }",
        "}",
        "",
        "/// A declared identity correlation between two catalog tables. P2 joins may",
        "/// equate exactly these column pairs in their ON clause; both endpoints are",
        "/// key parts or declared identity columns, sorted canonically.",
        "#[derive(Clone, Copy, Debug)]",
        "pub struct TofuSqlJoinEdge {",
        "    pub left_table: &'static str,",
        "    pub left_column: &'static str,",
        "    pub right_table: &'static str,",
        "    pub right_column: &'static str,",
        "}",
        "",
        "pub static TOFUSQL_TABLES: &[TofuSqlTable] = &[",
    ]
    for table in document["tables"]:
        lines.append("    TofuSqlTable {")
        lines.append(f'        name: "{table["table"]}",')
        lines.append(f'        namespace: "{table["namespace"]}",')
        # Emit rustfmt's empty/single/multiple struct-array shapes directly.
        # This keeps generation deterministic without a Rust toolchain dependency.
        fields = [
            ("key_parts", "TofuSqlKeyPart", [
                [f'name: "{part["name"]}",',
                 f"encoding: TofuSqlKeyEncoding::{_rust_variant(part['encoding'])},"]
                for part in table["keyParts"]]),
            ("identity_columns", "TofuSqlIdentityColumn", [
                [f'name: "{column["name"]}",', f'path: "{column["path"][2:]}",']
                for column in table.get("identityColumns", [])]),
        ]
        for field, kind, rows in fields:
            if not rows:
                lines.append(f"        {field}: &[],")
            elif len(rows) == 1:
                lines.append(f"        {field}: &[{kind} {{")
                lines.extend(f"            {value}" for value in rows[0])
                lines.append("        }],")
            else:
                lines.append(f"        {field}: &[")
                for row in rows:
                    lines.append(f"            {kind} {{")
                    lines.extend(f"                {value}" for value in row)
                    lines.append("            },")
                lines.append("        ],")
        lines.append("    },")
    lines += [
        "];",
        "",
        "pub fn tofusql_table(name: &str) -> Option<&'static TofuSqlTable> {",
        "    TOFUSQL_TABLES.iter().find(|table| table.name == name)",
        "}",
        "",
        "pub fn tofusql_table_for_namespace(namespace: &str) -> Option<&'static TofuSqlTable> {",
        "    TOFUSQL_TABLES",
        "        .iter()",
        "        .find(|table| table.namespace == namespace)",
        "}",
        "",
        "pub static TOFUSQL_JOIN_EDGES: &[TofuSqlJoinEdge] = &[",
    ]
    for edge in document.get("joinEdges", []):
        endpoints = sorted(
            (edge[side]["table"], edge[side]["column"]) for side in ("left", "right")
        )
        (left_table, left_column), (right_table, right_column) = endpoints
        lines.append("    TofuSqlJoinEdge {")
        lines.append(f'        left_table: "{left_table}",')
        lines.append(f'        left_column: "{left_column}",')
        lines.append(f'        right_table: "{right_table}",')
        lines.append(f'        right_column: "{right_column}",')
        lines.append("    },")
    lines += [
        "];",
        "",
        "/// Every declared join edge between one unordered pair of catalog tables.",
        "pub fn tofusql_join_edges_between<'a>(",
        "    table_a: &'a str,",
        "    table_b: &'a str,",
        ") -> impl Iterator<Item = &'static TofuSqlJoinEdge> + use<'a> {",
        "    TOFUSQL_JOIN_EDGES.iter().filter(move |edge| {",
        "        (edge.left_table == table_a && edge.right_table == table_b)",
        "            || (edge.left_table == table_b && edge.right_table == table_a)",
        "    })",
        "}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the generated file is current instead of writing it",
    )
    args = parser.parse_args()
    document = _load()
    rendered = _render(document)
    if args.check:
        existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if existing != rendered:
            print(f"{OUTPUT} is stale; run scripts/gen_tofusql_catalog.py")
            return 1
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
