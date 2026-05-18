from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import libsql_client

from gender_reveal_media.config import Settings


def connect(settings: Settings) -> libsql_client.ClientSync:
    return libsql_client.create_client_sync(
        settings.turso_database_url,
        auth_token=settings.turso_auth_token,
    )


def _split_sql_script(sql: str) -> list[str]:
    """Split schema file on semicolons outside of string literals (simplified)."""
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_single:
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'" and in_single:
            if i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_single = False
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not in_single:
            stmt = "".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def apply_schema(client: libsql_client.ClientSync, schema_path: Path | None = None) -> None:
    if schema_path is None:
        schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
    text = schema_path.read_text(encoding="utf-8")
    for stmt in _split_sql_script(text):
        if stmt.upper().startswith("PRAGMA"):
            continue
        client.execute(stmt)


def touch_episode_updated(client: libsql_client.ClientSync, episode_id: int) -> None:
    client.execute(
        "UPDATE episodes SET updated_at = datetime('now') WHERE id = ?",
        [episode_id],
    )


def insert_log(
    client: libsql_client.ClientSync,
    *,
    severity: str,
    component: str,
    message: str,
    episode_id: int | None = None,
    import_run_id: int | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    client.execute(
        """
        INSERT INTO log_events (severity, component, episode_id, import_run_id, message, context_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            severity,
            component,
            episode_id,
            import_run_id,
            message,
            json.dumps(context, ensure_ascii=False) if context else None,
        ],
    )
