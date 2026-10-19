"""Backend-neutral markers for JSON parameters and decoded JSON strings.

The semantic operation layer serializes every JSON document with
``orjson.dumps`` (bytes).  SQLite stores those bytes in BLOB-affinity
``JSONDOC`` columns and its json1 functions read them back unchanged, so the
plain-bytes representation is the on-disk contract there.

PostgreSQL binds Python ``bytes`` as ``bytea`` parameters, and ``bytea`` has
no assignment cast to ``jsonb`` — every INSERT/UPDATE of a ``JSONDOC``
column fails with SQLSTATE 42804.  ``str`` parameters would coerce (psycopg
sends them unknown-typed), but switching the shared dump helpers to ``str``
would silently change the SQLite storage format from BLOB to TEXT for every
existing row.

``JsonBytes`` keeps SQLite byte-identical (it *is* ``bytes``) while giving
the PostgreSQL adapter an exact type to bind with the ``jsonb`` OID.  Only
values destined for ``JSONDOC`` columns may be wrapped; genuine binary
payloads (``BLOB`` columns) keep plain ``bytes``.
PostgreSQL JSON/JSONB loaders mark decoded string results explicitly, so shared
codecs distinguish a string such as ``"true"`` from encoded JSON text. Other
structured results retain their existing representation; byte evidence must
not mistake PostgreSQL-normalized JSON text for the original encoded bytes.
"""


class JsonBytes(bytes):
    """A UTF-8 JSON document bound for a logical ``JSONDOC`` column."""

    __slots__ = ()


class DecodedJsonString(str):
    """A driver-decoded JSON string, distinct from encoded JSON text."""

    __slots__ = ()
