"""Reference data (summary / group / maintainer) sync from rdb.altlinux.org.

Replaces the CSV import of pkgcmp: one request to ``/site/repository_packages``
fills the whole ``package_meta`` table for a branch.  Sync happens on first use
(table is empty), after adding/updating an image, and manually on demand.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import journal
from .api import ALTRepoClient


def sync(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    *,
    branch: str,
    kind: str = "source",
) -> dict[str, Any]:
    """Download the package reference data of ``branch`` into package_meta."""
    rows = client.repository_packages(branch, package_type=kind)
    now = journal.utcnow()
    with conn:
        conn.execute("DELETE FROM package_meta WHERE branch = ? AND kind = ?", (branch, kind))
        conn.executemany(
            """
            INSERT INTO package_meta
                (name, kind, branch, version, release, pkghash, summary, category, maintainer, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (name, kind, branch) DO UPDATE SET
                version = excluded.version, release = excluded.release,
                pkghash = excluded.pkghash,
                summary = excluded.summary, category = excluded.category,
                maintainer = excluded.maintainer, synced_at = excluded.synced_at
            """,
            [
                (
                    row.get("name") or "",
                    kind,
                    branch,
                    str(row.get("version") or ""),
                    str(row.get("release") or ""),
                    str(row.get("hash") or "") or None,
                    str(row.get("summary") or ""),
                    str(row.get("category") or ""),
                    str(row.get("maintainer") or ""),
                    now,
                )
                for row in rows
                if row.get("name")
            ],
        )
    return {"branch": branch, "kind": kind, "count": len(rows), "synced_at": now}


def ensure(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    *,
    branch: str,
    kind: str = "source",
) -> dict[str, Any] | None:
    """Sync ``branch`` only if there is no meta for it yet (first use)."""
    if not is_stale(conn, branch, kind):
        return None
    return sync(conn, client, branch=branch, kind=kind)


def is_stale(conn: sqlite3.Connection, branch: str, kind: str = "source") -> bool:
    row = conn.execute(
        "SELECT 1 FROM package_meta WHERE branch = ? AND kind = ? LIMIT 1", (branch, kind)
    ).fetchone()
    return row is None


def status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-branch sync state for the UI."""
    rows = conn.execute(
        """
        SELECT branch, kind, COUNT(*) AS count, MAX(synced_at) AS synced_at
        FROM package_meta GROUP BY branch, kind ORDER BY branch, kind
        """
    )
    return [dict(r) for r in rows]


def lookup(
    conn: sqlite3.Connection,
    names: list[str],
    *,
    kind: str = "source",
    branch: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch meta for ``names``; exact branch wins, any other branch is a fallback."""
    out: dict[str, dict[str, Any]] = {}
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return out
    placeholders = ",".join("?" for _ in names)
    sql = (
        f"SELECT name, branch, version, release, summary, category, maintainer "
        f"FROM package_meta WHERE kind = ? AND name IN ({placeholders})"
    )
    for row in conn.execute(sql, [kind, *names]):
        key = str(row["name"])
        if branch and row["branch"] == branch:
            out[key] = dict(row)  # exact match overrides any fallback
        elif key not in out:
            out[key] = dict(row)
    return out
