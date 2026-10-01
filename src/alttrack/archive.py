"""Journal archiving: move stale rows to ``journal_archive`` and keep FTS in sync."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import journal
from .config import Config

ARCHIVE_COLUMNS = (
    "seq, ts, run_id, package_id, package, branch, event_type, old_value, new_value, detail"
)


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _move(conn: sqlite3.Connection, seqs: list[int], *, dry_run: bool) -> int:
    if not seqs:
        return 0
    if dry_run:
        return len(seqs)
    now = journal.utcnow()
    with conn:
        for seq in seqs:
            row = conn.execute(f"SELECT {ARCHIVE_COLUMNS} FROM journal WHERE seq = ?", (seq,)).fetchone()
            if row is None:
                continue
            conn.execute(
                f"INSERT OR IGNORE INTO journal_archive ({ARCHIVE_COLUMNS}, archived_at) "
                f"VALUES ({', '.join('?' for _ in ARCHIVE_COLUMNS.split(', '))}, ?)",
                (*[row[c] for c in ARCHIVE_COLUMNS.split(', ')], now),
            )
            conn.execute("DELETE FROM journal WHERE seq = ?", (seq,))
            conn.execute("UPDATE events_fts SET store = 'archive' WHERE rowid = ?", (seq,))
    return len(seqs)


def archive_plan(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    older_than_days: int | None = None,
    max_live_rows: int | None = None,
    package: str | None = None,
) -> list[int]:
    """Return the sequence numbers that should be archived right now."""
    days = cfg.archive_after_days if older_than_days is None else older_than_days
    limit_rows = cfg.max_live_rows if max_live_rows is None else max_live_rows

    args: list[Any] = []
    package_sql = ""
    if package:
        package_sql = " AND package = ? COLLATE NOCASE"
        args.append(package)

    seqs = [
        int(r["seq"])
        for r in conn.execute(
            f"SELECT seq FROM journal WHERE ts < ?{package_sql} ORDER BY ts, seq",
            (_cutoff(days), *args),
        )
    ]

    # Trim the live journal down to the row cap (oldest first).
    count_args: list[Any] = []
    count_sql = ""
    if package:
        count_sql = " WHERE package = ? COLLATE NOCASE"
        count_args.append(package)
    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM journal{count_sql}", count_args).fetchone()["n"])
    live_after_age = total - len(seqs)
    overflow = live_after_age - limit_rows
    if overflow > 0:
        already = set(seqs)
        package_filter = " WHERE package = ? COLLATE NOCASE" if package else ""
        for r in conn.execute(
            f"SELECT seq FROM journal{package_filter} ORDER BY ts, seq",
            count_args,
        ):
            seq = int(r["seq"])
            if seq not in already:
                seqs.append(seq)
                overflow -= 1
                if overflow <= 0:
                    break
    return seqs


def auto_archive(conn: sqlite3.Connection, cfg: Config) -> int:
    """Archive by the configured policy; returns the number of moved rows."""
    if not cfg.auto_archive:
        return 0
    seqs = archive_plan(conn, cfg)
    return _move(conn, seqs, dry_run=False)


def archive_now(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    older_than_days: int | None = None,
    max_live_rows: int | None = None,
    package: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Manual archiving (CLI + web).  Reports what was (or would be) moved."""
    seqs = archive_plan(
        conn, cfg, older_than_days=older_than_days, max_live_rows=max_live_rows, package=package
    )
    moved = _move(conn, seqs, dry_run=dry_run)
    total = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
    archived = conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"]
    return {
        "selected": len(seqs),
        "moved": moved,
        "dry_run": dry_run,
        "live_remaining": int(total) if dry_run else int(total),
        "archive_total": int(archived),
    }


def purge_archive(
    conn: sqlite3.Connection, *, older_than_days: int | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Delete archived rows (keeps the live journal untouched)."""
    if older_than_days is None:
        rows = [int(r["seq"]) for r in conn.execute("SELECT seq FROM journal_archive")]
    else:
        cutoff = _cutoff(older_than_days)
        rows = [
            int(r["seq"])
            for r in conn.execute("SELECT seq FROM journal_archive WHERE ts < ?", (cutoff,))
        ]
    if dry_run:
        return {"selected": len(rows), "deleted": 0, "dry_run": True}
    with conn:
        for seq in rows:
            conn.execute("DELETE FROM events_fts WHERE rowid = ?", (seq,))
            conn.execute("DELETE FROM journal_archive WHERE seq = ?", (seq,))
    return {"selected": len(rows), "deleted": len(rows), "dry_run": False}
