"""Reference lists from rdb.altlinux.org: branches, architectures, groups.

All lists change rarely (a branch appears, an architecture, a group), so they
are read from the API at first database fill (first refresh pass), stored
locally and can be re-synced manually from the Settings section (web) or
``alttrack meta refs`` (CLI).

* branch    — ``/packageset/active_packagesets`` (published packagesets);
              feeds the branch selectors on Products, Images and Lists —
              previously they were hardcoded or free-text fields;
* arch      — ``/site/all_pkgset_archs`` for every active branch, unioned with
              the built-in parser defaults and the architectures seen in the
              image catalog;
* category  — ``/site/pkgset_categories_count`` (source packages) for every
              active branch; the count is the maximum per-branch package count.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import rpmparse
from .api import ALTRepoClient, ApiError
from .journal import utcnow

KIND_BRANCH = "branch"
KIND_ARCH = "arch"
KIND_CATEGORY = "category"
KINDS = (KIND_BRANCH, KIND_ARCH, KIND_CATEGORY)

# Most common architectures first (display order for selectors/checkboxes).
COMMON_ARCHS = ("x86_64", "aarch64", "i586", "armh", "ppc64le")

# The branches most products are built from come first in selectors; the rest
# of the stored list follows alphabetically.
COMMON_BRANCHES = ("sisyphus", "p11", "p10", "p9", "p8")

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
        # A failed active_packagesets call must not wipe an existing branch
        # list — only the kinds we actually fetched are replaced.
        kinds = [KIND_ARCH, KIND_CATEGORY]
        if branches:
            kinds.append(KIND_BRANCH)
        conn.execute(
            f"DELETE FROM reference_lists WHERE kind IN ({', '.join('?' * len(kinds))})",
            tuple(kinds),
        )
        conn.executemany(
            "INSERT INTO reference_lists (kind, value, count, synced_at) "
            "VALUES (?, ?, ?, ?)",
            [(KIND_ARCH, a, 0, now) for a in sorted(arches)]
            + [(KIND_CATEGORY, c, categories[c], now) for c in sorted(categories)]
            + [(KIND_BRANCH, b, 0, now) for b in branches],
        )
    return {
        "branch": len(branches),
        "arch": len(arches),
        "category": len(categories),
        "synced_at": now,
        "branches": branches,
    }


def ensure(conn: sqlite3.Connection, client: ALTRepoClient) -> dict[str, Any] | None:
    """Sync when any kind is missing (first fill, or an upgrade that adds
    the branch reference to a database created by an older version)."""
    stored = {
        str(r[0])
        for r in conn.execute("SELECT DISTINCT kind FROM reference_lists")
    }
    if set(KINDS) <= stored:
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


def branches(conn: sqlite3.Connection) -> list[str]:
    """Stored branch list; falls back to the active-branches cache written by
    every refresh pass (older databases get the reference on their next
    ``ensure``/manual sync)."""
    rows = conn.execute(
        "SELECT value FROM reference_lists WHERE kind = ? ORDER BY value",
        (KIND_BRANCH,),
    ).fetchall()
    if rows:
        return [str(r[0]) for r in rows]
    row = conn.execute("SELECT value FROM meta WHERE key='active_branches'").fetchone()
    if row:
        try:
            cached = json.loads(row[0])
            if isinstance(cached, list) and cached:
                return sorted(str(b) for b in cached)
        except (TypeError, ValueError):
            pass
    return []


def branch_options(conn: sqlite3.Connection) -> list[str]:
    """Branch selector order: the usual product branches first, then the rest
    of the stored reference list alphabetically."""
    values = branches(conn)
    common = [b for b in COMMON_BRANCHES if b in values]
    return common + sorted(b for b in values if b not in common)


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
