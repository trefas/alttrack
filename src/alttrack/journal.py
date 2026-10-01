"""Journal: event storage, filtered queries and full-text search.

Live events live in the ``journal`` table; archived ones in
``journal_archive``.  Both are indexed by the single ``events_fts`` virtual
table whose rowid equals the journal sequence number, so a search can cover
the live journal, the archive, or both.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .models import EVENT_LABELS, Event, event_text

SCOPES = ("live", "archive", "all")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_event(
    conn: sqlite3.Connection,
    *,
    package: str,
    event_type: str,
    branch: str | None = None,
    package_id: int | None = None,
    run_id: int | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    detail: dict[str, Any] | None = None,
    ts: str | None = None,
) -> int:
    """Append one event to the live journal and the FTS index."""
    ts = ts or utcnow()
    detail_json = json.dumps(detail or {}, ensure_ascii=False)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO journal
                (ts, run_id, package_id, package, branch, event_type,
                 old_value, new_value, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, run_id, package_id, package, branch, event_type, old_value, new_value, detail_json),
        )
        seq = int(cur.lastrowid)
        flat = Event(
            seq=seq,
            ts=ts,
            package=package,
            package_id=package_id,
            branch=branch,
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
            detail=detail or {},
            store="live",
        )
        conn.execute(
            """
            INSERT INTO events_fts (rowid, seq, ts, package, branch, event_type, text, store)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'live')
            """,
            (seq, seq, ts, package, branch or "", event_type, event_text(flat)),
        )
    return seq


def _scope_tables(scope: str) -> list[tuple[str, str]]:
    if scope == "live":
        return [("journal", "live")]
    if scope == "archive":
        return [("journal_archive", "archive")]
    if scope == "all":
        return [("journal", "live"), ("journal_archive", "archive")]
    raise ValueError(f"unknown scope: {scope}")


def _select_sql(scope: str) -> tuple[str, int]:
    """SQL selecting rows shaped like journal + a ``store`` column."""
    parts: list[str] = []
    for table, store in _scope_tables(scope):
        parts.append(
            f"SELECT seq, ts, run_id, package_id, package, branch, event_type, "
            f"old_value, new_value, detail, '{store}' AS store FROM {table}"
        )
    return "\nUNION ALL\n".join(parts), len(_scope_tables(scope))


def _fts_seqs(conn: sqlite3.Connection, query: str, scope: str) -> list[int] | None:
    """Resolve an FTS query to a set of sequence numbers, or None if invalid."""
    stores = [store for _, store in _scope_tables(scope)]
    placeholders = ",".join("?" for _ in stores)
    try:
        rows = conn.execute(
            f"SELECT rowid FROM events_fts WHERE events_fts MATCH ? AND store IN ({placeholders})",
            (query, *stores),
        ).fetchall()
    except sqlite3.OperationalError:
        # Malformed FTS syntax from the user: treat it as a phrase search.
        safe = '"' + query.replace('"', '""') + '"'
        rows = conn.execute(
            f"SELECT rowid FROM events_fts WHERE events_fts MATCH ? AND store IN ({placeholders})",
            (safe, *stores),
        ).fetchall()
    return [int(r["rowid"]) for r in rows]


def query_events(
    conn: sqlite3.Connection,
    *,
    package: str | None = None,
    package_id: int | None = None,
    packages: Sequence[str] | None = None,
    branch: str | None = None,
    event_types: Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    scope: str = "all",
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "desc",
) -> tuple[list[Event], int]:
    """Filtered, paginated event listing.  Returns (events, total_count)."""
    where: list[str] = []
    args: list[Any] = []

    if package:
        where.append("package = ?")
        args.append(package)
    if package_id is not None:
        where.append("package_id = ?")
        args.append(package_id)
    if packages:
        # Product filter: one condition per chunk (SQLite parameter limit).
        names = [str(p) for p in dict.fromkeys(packages)]
        clauses: list[str] = []
        for start in range(0, len(names), 900):
            chunk = names[start:start + 900]
            clauses.append(f"package IN ({','.join('?' for _ in chunk)})")
            args.extend(chunk)
        where.append("(" + " OR ".join(clauses) + ")")
    if branch:
        where.append("branch = ?")
        args.append(branch)
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        where.append(f"event_type IN ({placeholders})")
        args.extend(event_types)
    if since:
        where.append("ts >= ?")
        args.append(since)
    if until:
        where.append("ts <= ?")
        args.append(until)

    if q:
        seqs = _fts_seqs(conn, q, scope)
        if not seqs:
            return [], 0
        placeholders = ",".join("?" for _ in seqs)
        where.append(f"seq IN ({placeholders})")
        args.extend(seqs)

    select_sql, _ = _select_sql(scope)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = "DESC" if order != "asc" else "ASC"

    total = conn.execute(f"SELECT COUNT(*) AS n FROM ({select_sql}) {where_sql}", args).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT * FROM ({select_sql}) {where_sql}
        ORDER BY ts {order_sql}, seq {order_sql}
        LIMIT ? OFFSET ?
        """,
        [*args, limit, offset],
    ).fetchall()
    return [Event.from_row(r, store=r["store"]) for r in rows], int(total)


def get_event(conn: sqlite3.Connection, seq: int, store: str = "live") -> Event | None:
    table = "journal" if store == "live" else "journal_archive"
    row = conn.execute(f"SELECT * FROM {table} WHERE seq = ?", (seq,)).fetchone()
    return Event.from_row(row, store=store) if row else None


def stats(conn: sqlite3.Connection, *, scope: str = "all") -> dict[str, Any]:
    """Aggregate counters used by ``alttrack stats`` and the dashboard."""
    by_type: dict[str, int] = {}
    by_branch: dict[str, int] = {}
    by_package: dict[str, int] = {}
    total = 0
    oldest = newest = None

    for table, _store in _scope_tables(scope):
        for row in conn.execute(f"SELECT event_type, COUNT(*) AS n FROM {table} GROUP BY event_type"):
            by_type[row["event_type"]] = by_type.get(row["event_type"], 0) + int(row["n"])
        for row in conn.execute(
            f"SELECT COALESCE(branch, '(нет ветки)') AS b, COUNT(*) AS n FROM {table} GROUP BY b ORDER BY n DESC LIMIT 20"
        ):
            by_branch[row["b"]] = by_branch.get(row["b"], 0) + int(row["n"])
        for row in conn.execute(
            f"SELECT package, COUNT(*) AS n FROM {table} GROUP BY package ORDER BY n DESC LIMIT 20"
        ):
            by_package[row["package"]] = by_package.get(row["package"], 0) + int(row["n"])
        row = conn.execute(
            f"SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest FROM {table}"
        ).fetchone()
        total += int(row["n"])
        if row["oldest"]:
            oldest = row["oldest"] if oldest is None else min(oldest, row["oldest"])
        if row["newest"]:
            newest = row["newest"] if newest is None else max(newest, row["newest"])

    return {
        "total": total,
        "live": conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"],
        "archive": conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"],
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "by_branch": dict(sorted(by_branch.items(), key=lambda kv: -kv[1])),
        "by_package": dict(sorted(by_package.items(), key=lambda kv: -kv[1])),
        "oldest": oldest,
        "newest": newest,
        "labels": EVENT_LABELS,
    }


def recent(conn: sqlite3.Connection, limit: int = 10, *, scope: str = "live") -> list[Event]:
    events, _ = query_events(conn, scope=scope, limit=limit)
    return events


def types_present(conn: sqlite3.Connection, *, scope: str = "all") -> list[str]:
    seen: set[str] = set()
    for table, _store in _scope_tables(scope):
        for row in conn.execute(f"SELECT DISTINCT event_type FROM {table}"):
            seen.add(row["event_type"])
    return sorted(seen)
