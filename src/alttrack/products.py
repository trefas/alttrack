"""Products: named sets of packages that make up a distribution image.

A product is the working unit for a product owner: branch + edition + arch.
Its composition is (re)built from images published at rdb.altlinux.org:

* ``preview_from_image`` — dry-run: what would be added/paused;
* ``track_from_image``   — first-time bulk tracking (one transaction);
* ``update_from_image``  — diff-update when a new image appears.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from typing import Any, Callable

from . import journal, metasync, watchlist
from .api import ALTRepoClient, ApiError
from .models import TRACKING_STARTED, TrackedPackage

# How many names we send per IN() clause / API batch.
_CHUNK = 500


@dataclasses.dataclass
class Product:
    id: int | None
    title: str
    branch: str
    edition: str = ""
    arch: str = "x86_64"
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "Product":
        return cls(
            id=row["id"],
            title=row["title"],
            branch=row["branch"],
            edition=row["edition"],
            arch=row["arch"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_products(conn: sqlite3.Connection) -> list[Product]:
    rows = conn.execute("SELECT * FROM products ORDER BY title COLLATE NOCASE")
    return [Product.from_row(r) for r in rows]


def get_product(conn: sqlite3.Connection, key: int | str) -> Product | None:
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        row = conn.execute("SELECT * FROM products WHERE id = ?", (int(key),)).fetchone()
        if row:
            return Product.from_row(row)
    row = conn.execute(
        "SELECT * FROM products WHERE title = ? COLLATE NOCASE", (str(key),)
    ).fetchone()
    return Product.from_row(row) if row else None


def require_product(conn: sqlite3.Connection, key: int | str) -> Product:
    product = get_product(conn, key)
    if product is None:
        raise KeyError(f"продукт {key!r} не найден")
    return product


def create_product(
    conn: sqlite3.Connection, *, title: str, branch: str, edition: str = "", arch: str = "x86_64"
) -> Product:
    title = (title or "").strip()
    if not title:
        raise ValueError("название продукта не может быть пустым")
    with conn:
        cur = conn.execute(
            "INSERT INTO products (title, branch, edition, arch, created_at) VALUES (?, ?, ?, ?, ?)",
            (title, branch, edition, arch, journal.utcnow()),
        )
    return require_product(conn, int(cur.lastrowid))


def delete_product(conn: sqlite3.Connection, product: Product) -> None:
    with conn:
        conn.execute("DELETE FROM product_packages WHERE product_id = ?", (product.id,))
        conn.execute("DELETE FROM product_images WHERE product_id = ?", (product.id,))
        conn.execute(
            "UPDATE lists SET product_id = NULL WHERE product_id = ?", (product.id,)
        )
        conn.execute(
            "UPDATE comparisons SET product_id = NULL WHERE product_id = ?", (product.id,)
        )
        conn.execute("DELETE FROM products WHERE id = ?", (product.id,))


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def product_members(
    conn: sqlite3.Connection, product: Product, *, include_paused: bool = True
) -> list[dict[str, Any]]:
    """Tracked packages of a product with membership flags."""
    sql = """
        SELECT t.*, pp.added_by, pp.paused_by_image, pp.added_at AS member_since
        FROM product_packages pp
        JOIN tracked_packages t ON t.id = pp.package_id
        WHERE pp.product_id = ?
    """
    if not include_paused:
        sql += " AND pp.paused_by_image = 0 AND t.enabled = 1"
    sql += " ORDER BY t.name COLLATE NOCASE"
    return [dict(r) for r in conn.execute(sql, (product.id,))]


def product_counts(conn: sqlite3.Connection, product: Product) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN pp.paused_by_image = 0 AND t.enabled = 1 THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN pp.paused_by_image = 1 OR t.enabled = 0 THEN 1 ELSE 0 END) AS paused
        FROM product_packages pp
        JOIN tracked_packages t ON t.id = pp.package_id
        WHERE pp.product_id = ?
        """,
        (product.id,),
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "active": int(row["active"] or 0),
        "paused": int(row["paused"] or 0),
    }


def _chunked(names: list[str], size: int = _CHUNK):
    for start in range(0, len(names), size):
        yield names[start:start + size]


def _tracked_by_name(conn: sqlite3.Connection, names: list[str]) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for chunk in _chunked(names):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT * FROM tracked_packages WHERE name COLLATE NOCASE IN ({placeholders})",
            chunk,
        ):
            out[str(row["name"]).casefold()] = row
    return out


def _meta_for(conn: sqlite3.Connection, branch: str, names: list[str]) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for chunk in _chunked(names):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT name, version, release, pkghash, summary, category, maintainer "
            f"FROM package_meta WHERE kind = 'source' AND branch = ? "
            f"AND name IN ({placeholders})",
            [branch, *chunk],
        ):
            out[str(row["name"]).casefold()] = row
    return out


def _membership(conn: sqlite3.Connection, product: Product) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM product_packages WHERE product_id = ?", (product.id,)
    )
    return {int(r["package_id"]): r for r in rows}


# ---------------------------------------------------------------------------
# Image preview / tracking
# ---------------------------------------------------------------------------
# x86_64 images also ship i586 variants ("i586-bzlib", "i586-glibc-core");
# such names do not exist as packages of their own and never resolve directly.
_ARCH_PREFIX = re.compile(r"^(?:i386|i486|i586|i686|armh|armv6|armv7hl)-", re.I)


def resolve_sources(
    client: ALTRepoClient, branch: str, names: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    """Map binary package names to their source packages.

    Two passes: direct mapping first, then a retry with the architecture
    prefix stripped (``i586-glibc-core`` → ``glibc-core`` → source ``glibc``).

    Returns ``(sources, bin_to_src, unresolved)`` where ``sources`` maps each
    source package name to one API entry (carrying version/release).
    """
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return {}, {}, []

    found: dict[str, dict[str, Any]] = {}       # bin name -> entry
    unresolved: list[str] = []
    for entry in client.source_packages(branch, names):
        bin_name = str(entry.get("name") or "")
        if entry.get("sourcepkgname"):
            found[bin_name] = entry
        else:
            unresolved.append(bin_name)

    # Second pass: strip a known architecture prefix and retry.
    if unresolved:
        stripped = {n: _ARCH_PREFIX.sub("", n) for n in unresolved}
        retry = sorted({s for orig, s in stripped.items() if s != orig})
        if retry:
            try:
                for entry in client.source_packages(branch, retry):
                    name = str(entry.get("name") or "")
                    if not entry.get("sourcepkgname"):
                        continue
                    for orig, s in stripped.items():
                        if s == name and orig not in found:
                            found[orig] = dict(entry, name=orig)
            except ApiError:
                pass
        unresolved = [n for n in unresolved if n not in found]

    sources: dict[str, dict[str, Any]] = {}
    bin_to_src: dict[str, str] = {}
    for bin_name, entry in found.items():
        src = str(entry["sourcepkgname"])
        bin_to_src[bin_name] = src
        sources.setdefault(src, entry)
    return sources, bin_to_src, unresolved


def preview_from_image(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    product: Product,
    image_uuid: str,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Dry-run: resolve an image's binaries to source packages and diff them
    against what is already tracked."""
    def say(text: str) -> None:
        if progress:
            progress(text)

    say("загрузка пакетов образа…")
    binaries = client.all_image_packages(image_uuid)
    names = [str(b.get("name") or "") for b in binaries if b.get("name")]

    say(f"поиск исходных пакетов ({len(names)} бинарников)…")
    try:
        sources, _bin_to_src, not_found = resolve_sources(client, product.branch, names)
    except ApiError as exc:
        raise ApiError(f"маппинг в srpm не удался: {exc}") from exc

    say("сверка с текущим списком…")
    tracked = _tracked_by_name(conn, list(sources))
    srpms = sorted(sources, key=str.casefold)
    new = [n for n in srpms if n.casefold() not in tracked]
    existing = [n for n in srpms if n.casefold() in tracked]

    # Version delta between the image mapping and the stored reference data.
    meta = _meta_for(conn, product.branch, srpms)
    updated = 0
    for name, entry in sources.items():
        row = meta.get(name.casefold())
        if not row:
            continue
        if (str(entry.get("version") or ""), str(entry.get("release") or "")) != (
            str(row["version"]), str(row["release"]),
        ):
            updated += 1

    return {
        "image_uuid": image_uuid,
        "binaries": len(names),
        "srpms": srpms,
        "sources": sources,
        "new": new,
        "existing": existing,
        "not_found": sorted(set(not_found)),
        "meta_missing": [n for n in srpms if n.casefold() not in meta],
        "updated": updated,
        "tracked": tracked,
        "meta": meta,
    }


def _bulk_insert(
    conn: sqlite3.Connection,
    product: Product,
    names: list[str],
    *,
    meta: dict[str, sqlite3.Row],
    tracked: dict[str, sqlite3.Row],
    watch_errata: bool,
    watch_maintainer: bool,
    watch_tasks: bool,
) -> dict[str, Any]:
    """One transaction: tracked_packages + product_packages + snapshots + events."""
    now = journal.utcnow()
    added: list[str] = []
    added_ids: list[tuple[str, int]] = []
    linked: list[str] = []
    branch_updated: list[str] = []
    reactivated: list[str] = []
    membership = _membership(conn, product)

    with conn:
        for name in names:
            key = name.casefold()
            row = tracked.get(key)
            if row is None:
                cur = conn.execute(
                    """
                    INSERT INTO tracked_packages
                        (name, branches, note, watch_errata, watch_maintainer,
                         watch_tasks, enabled, added_at)
                    VALUES (?, ?, '', ?, ?, ?, 1, ?)
                    """,
                    (
                        name,
                        json.dumps([product.branch]),
                        int(watch_errata), int(watch_maintainer), int(watch_tasks),
                        now,
                    ),
                )
                package_id = int(cur.lastrowid)
                added.append(name)
                added_ids.append((name, package_id))
            else:
                package_id = int(row["id"])
                branches = json.loads(row["branches"] or "[]")
                if product.branch not in branches:
                    branches.append(product.branch)
                    conn.execute(
                        "UPDATE tracked_packages SET branches = ? WHERE id = ?",
                        (json.dumps(branches), package_id),
                    )
                    branch_updated.append(name)
                # Reactivate packages paused by an earlier image update.
                if not row["enabled"]:
                    paused = membership.get(package_id)
                    if paused is not None and paused["paused_by_image"]:
                        conn.execute(
                            "UPDATE tracked_packages SET enabled = 1 WHERE id = ?",
                            (package_id,),
                        )
                        reactivated.append(name)

            member = membership.get(package_id)
            if member is None:
                conn.execute(
                    "INSERT INTO product_packages "
                    "(product_id, package_id, added_by, paused_by_image, added_at) "
                    "VALUES (?, ?, 'image', 0, ?)",
                    (product.id, package_id, now),
                )
                if row is not None:
                    linked.append(name)
            elif member["paused_by_image"]:
                conn.execute(
                    "UPDATE product_packages SET paused_by_image = 0 "
                    "WHERE product_id = ? AND package_id = ?",
                    (product.id, package_id),
                )

            # Baseline snapshot from the branch reference data (no history events).
            mrow = meta.get(key)
            if mrow is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO snapshots "
                    "(package_id, branch, present, version, release, pkghash, observed_at) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (
                        package_id, product.branch, mrow["version"], mrow["release"],
                        mrow["pkghash"], now,
                    ),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO snapshots "
                    "(package_id, branch, present, observed_at) VALUES (?, ?, 0, ?)",
                    (package_id, product.branch, now),
                )

    # Journal: one tracking_started per newly tracked package (bulk detail).
    for name, package_id in added_ids:
        journal.insert_event(
            conn,
            package=name,
            package_id=package_id,
            event_type=TRACKING_STARTED,
            branch=product.branch,
            new_value=product.branch,
            detail={"product": product.title, "source": "image", "bulk": True},
            ts=now,
        )

    return {
        "added": added,
        "linked": linked,
        "branch_updated": branch_updated,
        "reactivated": reactivated,
    }


def track_from_image(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    product: Product,
    image_uuid: str,
    *,
    watch_errata: bool = False,
    watch_maintainer: bool = False,
    watch_tasks: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """First-time bulk tracking of an image's source packages."""
    def say(text: str) -> None:
        if progress:
            progress(text)

    say("проверка справочника…")
    metasync.ensure(conn, client, branch=product.branch)

    preview = preview_from_image(conn, client, product, image_uuid, progress=progress)
    say(f"добавление {len(preview['srpms'])} пакетов…")

    report = _bulk_insert(
        conn,
        product,
        preview["srpms"],
        meta=preview["meta"],
        tracked=preview["tracked"],
        watch_errata=watch_errata,
        watch_maintainer=watch_maintainer,
        watch_tasks=watch_tasks,
    )
    report.update(
        {
            "image_uuid": image_uuid,
            "binaries": preview["binaries"],
            "srpms": len(preview["srpms"]),
            "existing": preview["existing"],
            "not_found": preview["not_found"],
            "updated": preview["updated"],
        }
    )
    _register_image(conn, product, image_uuid, count=len(preview["srpms"]))
    return report


def update_from_image(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    product: Product,
    image_uuid: str,
    *,
    watch_errata: bool = False,
    watch_maintainer: bool = False,
    watch_tasks: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Diff-update: add new srpms, pause dropped ones, reactivate returned."""
    def say(text: str) -> None:
        if progress:
            progress(text)

    say("проверка справочника…")
    metasync.ensure(conn, client, branch=product.branch)

    preview = preview_from_image(conn, client, product, image_uuid, progress=progress)
    new_names = preview["new"]  # not tracked at all
    all_names = preview["srpms"]

    say(f"добавление {len(new_names)} новых пакетов…")
    report = _bulk_insert(
        conn,
        product,
        all_names,
        meta=preview["meta"],
        tracked=preview["tracked"],
        watch_errata=watch_errata,
        watch_maintainer=watch_maintainer,
        watch_tasks=watch_tasks,
    )

    # Pause image-added members that fell out of the image.
    in_image = {n.casefold() for n in all_names}
    paused: list[str] = []
    kept: list[str] = []
    now = journal.utcnow()
    with conn:
        for member in conn.execute(
            """
            SELECT pp.package_id, pp.added_by, pp.paused_by_image, t.name, t.enabled
            FROM product_packages pp
            JOIN tracked_packages t ON t.id = pp.package_id
            WHERE pp.product_id = ? AND pp.added_by = 'image' AND pp.paused_by_image = 0
            """,
            (product.id,),
        ):
            if str(member["name"]).casefold() in in_image:
                kept.append(str(member["name"]))
                continue
            paused.append(str(member["name"]))
            conn.execute(
                "UPDATE product_packages SET paused_by_image = 1 "
                "WHERE product_id = ? AND package_id = ?",
                (product.id, member["package_id"]),
            )
            if member["enabled"]:
                conn.execute(
                    "UPDATE tracked_packages SET enabled = 0 WHERE id = ?",
                    (member["package_id"],),
                )

    report.update(
        {
            "image_uuid": image_uuid,
            "binaries": preview["binaries"],
            "srpms": len(all_names),
            "existing": preview["existing"],
            "not_found": preview["not_found"],
            "updated": preview["updated"],
            "paused": sorted(paused, key=str.casefold),
            "kept": kept,
            "ts": now,
        }
    )
    _register_image(conn, product, image_uuid, count=len(all_names))
    return report


def _register_image(conn: sqlite3.Connection, product: Product, uuid: str, *, count: int) -> None:
    from . import images  # local import to avoid a cycle

    row = images.find_image(conn, uuid)
    kind = "release" if row is not None and row["release"] == "release" else "other"
    with conn:
        conn.execute(
            "INSERT INTO product_images "
            "(product_id, image_uuid, tag, kind, date, package_count, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                product.id, uuid,
                str(row["tag"]) if row else "",
                kind,
                str(row["date"]) if row else None,
                count, journal.utcnow(),
            ),
        )


def product_images(conn: sqlite3.Connection, product: Product) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM product_images WHERE product_id = ? ORDER BY added_at DESC",
            (product.id,),
        )
    )
