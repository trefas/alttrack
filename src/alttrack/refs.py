"""Reference lists from rdb.altlinux.org: architectures and software groups.

Both lists change rarely (new architecture, new group), so they are read from
the API at first database fill (first refresh pass), stored locally and can be
re-synced manually from the Settings section (web) or ``alttrack meta refs``
(CLI).

* arch      — ``/site/all_pkgset_archs`` for every active branch, unioned with
              the built-in parser defaults and the architectures seen in the
              image catalog;
* category  — ``/site/pkgset_categories_count`` (source packages) for every
              active branch; the count is the maximum per-branch package count.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import rpmparse
from .api import ALTRepoClient, ApiError
from .journal import utcnow

KIND_ARCH = "arch"
KIND_CATEGORY = "category"
KINDS = (KIND_ARCH, KIND_CATEGORY)

# Most common architectures first (display order for selectors/checkboxes).
COMMON_ARCHS = ("x86_64", "aarch64", "i586", "armh", "ppc64le")

# Pseudo-architectures: not installable architectures of a repository branch.
_PSEUDO_ARCHS = frozenset({"src", "srpm", "noarch"})


def sync(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    *,
    progress: Any = None,
) -> dict[str, Any]:
    """Reload both reference lists from rdb (replaces stored rows)."""

    def say(text: str) -> None:
        if progress:
            progress(text)

    try:
        branches = sorted(client.active_packagesets())
    except ApiError:
        branches = []

    # -- architectures -----------------------------------------------------
    say("загрузка списка архитектур…")
    arches: set[str] = {str(a).lower() for a in rpmparse.ARCHITECTURES}
    for branch in branches:
        try:
            data = client.get("/site/all_pkgset_archs", {"branch": branch})
        except ApiError:
            continue
        for item in data.get("archs") or []:
            name = str(item.get("arch") or "").strip()
            if name:
                arches.add(name.lower())
    # The image catalog (rdb /image) knows install architectures that the
    # package endpoint may not report for source packages (armh, ppc64le…).
    for row in conn.execute(
        "SELECT DISTINCT arch FROM image_catalog WHERE arch <> ''"
    ):
        arches.add(str(row[0]).lower())

    # -- categories --------------------------------------------------------
    say("загрузка списка групп ПО…")
    categories: dict[str, int] = {}
    for branch in branches:
        try:
            data = client.get(
                "/site/pkgset_categories_count",
                {"branch": branch, "package_type": "source"},
            )
        except ApiError:
            continue
        for item in data.get("categories") or []:
            name = str(item.get("category") or "").strip()
            if not name:
                continue
            categories[name] = max(categories.get(name, 0), int(item.get("count") or 0))

    now = utcnow()
    with conn:
        conn.execute(
            "DELETE FROM reference_lists WHERE kind IN (?, ?)",
            (KIND_ARCH, KIND_CATEGORY),
        )
        conn.executemany(
            "INSERT INTO reference_lists (kind, value, count, synced_at) "
            "VALUES (?, ?, ?, ?)",
            [(KIND_ARCH, a, 0, now) for a in sorted(arches)]
            + [(KIND_CATEGORY, c, categories[c], now) for c in sorted(categories)],
        )
    return {
        "arch": len(arches),
        "category": len(categories),
        "synced_at": now,
        "branches": branches,
    }


def ensure(conn: sqlite3.Connection, client: ALTRepoClient) -> dict[str, Any] | None:
    """Sync only when nothing is stored yet (first database fill)."""
    stored = conn.execute("SELECT COUNT(*) FROM reference_lists").fetchone()[0]
    if stored:
        return None
    try:
        return sync(conn, client)
    except ApiError:
        return None


def architectures(conn: sqlite3.Connection) -> list[str]:
    """Stored architecture list; falls back to the built-in parser defaults."""
    rows = conn.execute(
        "SELECT value FROM reference_lists WHERE kind = ? ORDER BY value",
        (KIND_ARCH,),
    ).fetchall()
    if rows:
        return [str(r[0]) for r in rows]
    return sorted(rpmparse.ARCHITECTURES)


def parser_architectures(conn: sqlite3.Connection) -> frozenset[str]:
    """Architecture suffixes recognised when parsing uploaded file lists."""
    return rpmparse.known_architectures(architectures(conn))


def branch_arch_options(conn: sqlite3.Connection) -> list[str]:
    """Real repository architectures for selectors: the common ones first,
    then the rest of the stored reference list (src/srpm/noarch excluded)."""
    values = [a for a in architectures(conn) if a.lower() not in _PSEUDO_ARCHS]
    common = [a for a in COMMON_ARCHS if a in values]
    return common + sorted(a for a in values if a not in common)


def categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {"value": str(r[0]), "count": int(r[1]), "synced_at": str(r[2])}
        for r in conn.execute(
            "SELECT value, count, synced_at FROM reference_lists "
            "WHERE kind = ? ORDER BY value",
            (KIND_CATEGORY,),
        )
    ]


def status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-kind summary: rows and last sync time (empty kinds are omitted)."""
    return [
        {"kind": str(r[0]), "count": int(r[1]), "synced_at": str(r[2])}
        for r in conn.execute(
            "SELECT kind, COUNT(*), MAX(synced_at) FROM reference_lists "
            "GROUP BY kind ORDER BY kind"
        )
    ]
