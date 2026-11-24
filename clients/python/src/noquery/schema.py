"""The schema graph: the ORM-independent description of a database.

Every introspection adapter (SQLAlchemy, Django, the Postgres catalog) emits this shape.
It is plain data with the same keys as the TypeScript packages use:

    {"tables": [{
        "name": "trips", "schema": "public", "comment": "...",
        "columns": [{"name": "id", "kind": "uuid", "dbType": "uuid", "nullable": False,
                     "primaryKey": True, "enumValues": [...], "withTimezone": True, "comment": "..."}],
        "foreignKeys": [{"columns": ["device_id"], "referencedTable": "devices",
                         "referencedSchema": "public", "referencedColumns": ["id"]}],
    }]}
"""

from __future__ import annotations

from typing import Any, Dict

COLUMN_KINDS = ("string", "integer", "number", "boolean", "timestamp", "date", "uuid", "enum", "json", "unknown")

ColumnInfo = Dict[str, Any]
ForeignKeyInfo = Dict[str, Any]
TableInfo = Dict[str, Any]
SchemaGraph = Dict[str, Any]
