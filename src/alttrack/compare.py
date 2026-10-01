"""Comparison of two package lists (pkgcmp-style diff, four statuses).

The report is computed on the fly from ``list_items``; only the comparison
metadata (title, sides, stats snapshot) is stored, so history stays small.
"""

from __future__ import annotations

import dataclasses
import csv
import io
import json
import sqlite3
from typing import Any

from . import journal, metasync

LEFT_ONLY = "left_only"    # ++ only in the left list
RIGHT_ONLY = "right_only"  # -- only in the right list
CHANGED = "changed"        # >> different version/release
SAME = "same"              # == identical

# Right side of this list kind = whole repository branch: rows that exist
# only there are excluded from reports (see report()).
RIGHT_KIND = "branch"

STATUSES: tuple[str, ...] = (LEFT_ONLY, RIGHT_ONLY, CHANGED, SAME)

STATUS_LABELS: dict[str, str] = {
    LEFT_ONLY: "++ только слева",
    RIGHT_ONLY: "-- только справа",
    CHANGED: ">> версии отличаются",
    SAME: "== совпадают",
}


@dataclasses.dataclass
class Comparison:
    id: int | None
    title: str
    left_list_id: int
    right_list_id: int
    product_id: int | None = None
    stats: dict[str, Any] = dataclasses.field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "Comparison":
        try:
            stats = json.loads(row["stats"] or "{}")
        except (TypeError, ValueError):
            stats = {}
        return cls(
            id=row["id"],
            title=row["title"],
            left_list_id=row["left_list_id"],
            right_list_id=row["right_list_id"],
            product_id=row["product_id"],
            stats=stats,
            created_at=row["created_at"],
        )


def _side(conn: sqlite3.Connection, list_id: int) -> tuple[dict[str, sqlite3.Row], dict[str, Any]]:
    row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
    if row is None:
        raise KeyError(f"список {list_id} не найден")
    items = {
        str(r["name"]).casefold(): r
        for r in conn.execute(
            "SELECT * FROM list_items WHERE list_id = ?", (list_id,)
        )
    }
    return items, dict(row)


def compute(conn: sqlite3.Connection, left_id: int, right_id: int) -> dict[str, Any]:
    """Join two lists by package name and enrich with reference meta."""
    left, left_meta = _side(conn, left_id)
    right, right_meta = _side(conn, right_id)

    rows: list[dict[str, Any]] = []
    stats = {s: 0 for s in STATUSES}
    source_names: set[str] = set()

    for key in sorted(set(left) | set(right)):
        lrow, rrow = left.get(key), right.get(key)
        if lrow and not rrow:
            status = LEFT_ONLY
        elif rrow and not lrow:
            status = RIGHT_ONLY
        elif lrow["version"] == rrow["version"] and lrow["release"] == rrow["release"]:
            status = SAME
        else:
            status = CHANGED
        stats[status] += 1

        source = lrow["source_name"] if lrow and lrow["source_name"] else (
            rrow["source_name"] if rrow and rrow["source_name"] else None
        )
        if source:
            source_names.add(str(source))
        rows.append(
            {
                "name": str((lrow or rrow)["name"]),
                "status": status,
                "left": {
                    "version": str(lrow["version"]), "release": str(lrow["release"]),
                    "arch": str(lrow["arch"]), "summary": str(lrow["summary"] or ""),
                } if lrow else None,
                "right": {
                    "version": str(rrow["version"]), "release": str(rrow["release"]),
                    "arch": str(rrow["arch"]), "summary": str(rrow["summary"] or ""),
                } if rrow else None,
                "source": str(source) if source else "",
                "summary": str((lrow or rrow)["summary"] or ""),
                "category": "",
            }
        )

    # Reference data: summary/category of the source package.
    branch = left_meta.get("branch") or right_meta.get("branch")
    if source_names:
        meta = metasync.lookup(conn, sorted(source_names), branch=branch)
        by_key = {k.casefold(): v for k, v in meta.items()}
        for row in rows:
            info = by_key.get(row["source"].casefold()) if row["source"] else None
            if info:
                row["category"] = str(info.get("category") or "")
                if not row["summary"]:
                    row["summary"] = str(info.get("summary") or "")

    return {
        "rows": rows,
        "stats": stats,
        "left": left_meta,
        "right": right_meta,
        "total": len(rows),
    }


def save(
    conn: sqlite3.Connection,
    *,
    left_id: int,
    right_id: int,
    title: str = "",
    product_id: int | None = None,
) -> Comparison:
    """Create a comparison entry (stats snapshot; report stays on the fly)."""
    result = compute(conn, left_id, right_id)
    left_title = str(result["left"].get("title") or "")
    right_title = str(result["right"].get("title") or "")
    with conn:
        cur = conn.execute(
            "INSERT INTO comparisons "
            "(title, product_id, left_list_id, right_list_id, stats, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                title or f"{left_title} ↔ {right_title}",
                product_id, left_id, right_id,
                json.dumps(result["stats"], ensure_ascii=False),
                journal.utcnow(),
            ),
        )
    return require_comparison(conn, int(cur.lastrowid))


def get_comparison(conn: sqlite3.Connection, key: int | str) -> Comparison | None:
    if not (isinstance(key, int) or str(key).isdigit()):
        return None
    row = conn.execute("SELECT * FROM comparisons WHERE id = ?", (int(key),)).fetchone()
    return Comparison.from_row(row) if row else None


def require_comparison(conn: sqlite3.Connection, key: int | str) -> Comparison:
    found = get_comparison(conn, key)
    if found is None:
        raise KeyError(f"сравнение {key!r} не найдено")
    return found


def report(
    conn: sqlite3.Connection,
    comparison: Comparison,
    *,
    include_right_extra: bool = False,
) -> dict[str, Any]:
    """Recompute the report of a stored comparison.

    When the right side is a repository snapshot (``kind == "branch"``),
    ``right_only`` rows are noise: a single image can never contain the whole
    branch, so thousands of repository-only packages say nothing about the
    product.  They are filtered out unless ``include_right_extra`` is set;
    ``hidden_right_only`` tells how many were filtered (for an UI hint).
    """
    result = compute(conn, comparison.left_list_id, comparison.right_list_id)
    result["comparison"] = comparison
    result["hidden_right_only"] = 0
    if str(result["right"].get("kind") or "") == RIGHT_KIND and not include_right_extra:
        rows = [r for r in result["rows"] if r["status"] != RIGHT_ONLY]
        stats = {s: 0 for s in STATUSES}
        for row in rows:
            stats[row["status"]] += 1
        result["hidden_right_only"] = int(result["stats"].get(RIGHT_ONLY, 0))
        result["rows"] = rows
        result["stats"] = stats
        result["total"] = len(rows)
    return result


def list_comparisons(
    conn: sqlite3.Connection, *, product_id: int | None = None
) -> list[Comparison]:
    if product_id is None:
        rows = conn.execute("SELECT * FROM comparisons ORDER BY created_at DESC")
    else:
        rows = conn.execute(
            "SELECT * FROM comparisons WHERE product_id = ? ORDER BY created_at DESC",
            (product_id,),
        )
    return [Comparison.from_row(r) for r in rows]


def delete_comparison(conn: sqlite3.Connection, cmp_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM comparisons WHERE id = ?", (cmp_id,))


def to_csv(rows: list[dict[str, Any]], *, status: str | None = None) -> str:
    """CSV export of a report (optionally limited to one status group)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["статус", "пакет", "слева версия", "слева релиз",
         "справа версия", "справа релиз", "исходный пакет", "группа", "описание"]
    )
    for row in rows:
        if status and row["status"] != status:
            continue
        left = row["left"] or {}
        right = row["right"] or {}
        writer.writerow(
            [
                STATUS_LABELS.get(row["status"], row["status"]),
                row["name"],
                left.get("version", ""), left.get("release", ""),
                right.get("version", ""), right.get("release", ""),
                row["source"], row["category"], row["summary"],
            ]
        )
    return buf.getvalue()
