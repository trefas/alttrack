"""Distribution images: catalog cache and package listing helpers.

The catalog (``/image/image_info``) is cached in the ``image_catalog`` table;
it drives the cascading filters (branch → edition → arch) in the UI.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import journal
from .api import ALTRepoClient


def refresh_catalog(conn: sqlite3.Connection, client: ALTRepoClient) -> int:
    """Re-download the whole image catalog; returns the number of images."""
    images = client.image_info()
    now = journal.utcnow()
    with conn:
        conn.execute("DELETE FROM image_catalog")
        conn.executemany(
            """
            INSERT OR REPLACE INTO image_catalog
                (uuid, branch, edition, arch, variant, type, release, tag, file, date, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    img.get("uuid") or "",
                    img.get("branch") or "",
                    img.get("edition") or "",
                    img.get("arch") or "",
                    img.get("variant") or "",
                    img.get("type") or "",
                    img.get("release") or "",
                    img.get("tag") or "",
                    img.get("file") or "",
                    img.get("date"),
                    now,
                )
                for img in images
                if img.get("uuid")
            ],
        )
    return len(images)


def catalog_is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM image_catalog LIMIT 1").fetchone() is None


def catalog(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
    edition: str | None = None,
    arch: str | None = None,
    release: str | None = None,
    type: str | None = None,  # noqa: A002 - matches the API field name
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Filtered catalog, newest first."""
    where: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("branch", branch), ("edition", edition), ("arch", arch),
        ("release", release), ("type", type),
    ):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    sql = "SELECT * FROM image_catalog"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, edition"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return list(conn.execute(sql, args))


def find_image(conn: sqlite3.Connection, uuid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM image_catalog WHERE uuid = ?", (uuid,)).fetchone()


def filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Distinct values for the cascading filters."""
    def values(column: str) -> list[str]:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM image_catalog "
            f"WHERE {column} != '' ORDER BY {column}"
        )
        return [str(r[0]) for r in rows]

    return {
        "branches": values("branch"),
        "editions": values("edition"),
        "archs": values("arch"),
        "releases": values("release"),
        "types": values("type"),
    }


def latest_release_image(
    conn: sqlite3.Connection, *, branch: str, edition: str, arch: str
) -> sqlite3.Row | None:
    """Newest released image of a product (left side of the default comparison)."""
    return conn.execute(
        """
        SELECT * FROM image_catalog
        WHERE branch = ? AND edition = ? AND arch = ? AND release = 'release'
        ORDER BY date DESC LIMIT 1
        """,
        (branch, edition, arch),
    ).fetchone()


def image_binaries(
    client: ALTRepoClient, uuid: str, *, progress: Any = None
) -> list[dict[str, Any]]:
    """All binary packages of an image (name/version/release/arch/summary)."""
    if progress:
        progress("загрузка пакетов образа…")
    return client.all_image_packages(uuid)
