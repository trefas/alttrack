"""Saved package lists (file / image / repository branch).

Lists are the inputs of comparisons and are kept in the database so a
comparison can be replayed later without re-uploading anything.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any, Sequence

from . import journal, products, refs, rpmparse
from .api import ALTRepoClient, ApiError


@dataclasses.dataclass
class PackageList:
    id: int | None
    title: str
    kind: str                      # file | image | branch
    product_id: int | None = None
    params: dict[str, Any] = dataclasses.field(default_factory=dict)
    item_count: int = 0
    bad_lines: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "PackageList":
        try:
            params = json.loads(row["params"] or "{}")
        except (TypeError, ValueError):
            params = {}
        return cls(
            id=row["id"],
            title=row["title"],
            kind=row["kind"],
            product_id=row["product_id"],
            params=params,
            item_count=row["item_count"],
            bad_lines=row["bad_lines"],
            created_at=row["created_at"],
        )


def _insert(
    conn: sqlite3.Connection,
    *,
    title: str,
    kind: str,
    product_id: int | None,
    params: dict[str, Any],
    parsed: list[rpmparse.ParsedRPM],
    source_map: dict[str, str | None],
    summaries: dict[str, str],
    bad_lines: int,
) -> PackageList:
    now = journal.utcnow()
    seen: set[str] = set()
    rows: list[tuple] = []
    for item in parsed:
        key = item.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            (
                0, item.name, item.version, item.release, item.arch,
                summaries.get(key, ""),
                source_map.get(key),
            )
        )
    with conn:
        cur = conn.execute(
            "INSERT INTO lists (title, kind, product_id, params, item_count, bad_lines, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, kind, product_id, json.dumps(params, ensure_ascii=False),
             len(rows), bad_lines, now),
        )
        list_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO list_items (list_id, name, version, release, arch, summary, source_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(list_id, *row[1:]) for row in rows],
        )
    return require_list(conn, list_id)


def _map_sources(
    client: ALTRepoClient | None,
    branch: str | None,
    names: list[str],
    *,
    progress: Any = None,
) -> dict[str, str | None]:
    """Binary → source mapping via POST /packageset/source_packages."""
    if client is None or not branch or not names:
        return {}
    if progress:
        progress("поиск исходных пакетов…")
    try:
        _sources, bin_to_src, _unresolved = products.resolve_sources(client, branch, names)
    except ApiError:
        return {}
    return {name.casefold(): src for name, src in bin_to_src.items()}


def save_file_list(
    conn: sqlite3.Connection,
    *,
    text: str,
    title: str = "",
    product_id: int | None = None,
    source_branch: str | None = None,
    client: ALTRepoClient | None = None,
    progress: Any = None,
) -> tuple[PackageList, dict[str, Any]]:
    """Parse an uploaded/pasted rpm file list and store it."""
    parsed, bad = rpmparse.parse_rpm_lines(text, arches=refs.parser_architectures(conn))
    names = [p.name for p in parsed]
    source_map = _map_sources(client, source_branch, names, progress=progress)
    # keep only names we actually resolved
    source_map = {k: v for k, v in source_map.items() if v}
    list_obj = _insert(
        conn,
        title=title or "Список от " + journal.utcnow()[:16].replace("T", " "),
        kind="file",
        product_id=product_id,
        params={"branch": source_branch, "total_lines": len(text.splitlines())},
        parsed=parsed,
        source_map=source_map,
        summaries={},
        bad_lines=len(bad),
    )
    report = {
        "parsed": len(parsed),
        "bad": [b.raw.strip() for b in bad][:50],
        "bad_count": len(bad),
        "mapped": sum(1 for v in source_map.values() if v),
    }
    return list_obj, report


def save_image_list(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    *,
    uuid: str,
    title: str | None = None,
    product_id: int | None = None,
    progress: Any = None,
) -> tuple[PackageList, dict[str, Any]]:
    """Snapshot an image's binaries into a list (with srpm mapping + summaries)."""
    from . import images  # local import to avoid a cycle

    row = images.find_image(conn, uuid)
    if progress:
        progress("загрузка пакетов образа…")
    binaries = client.all_image_packages(uuid)
    summaries = {
        str(b.get("name") or "").casefold(): str(b.get("summary") or "")
        for b in binaries
    }
    names = [str(b.get("name") or "") for b in binaries if b.get("name")]
    branch = str(row["branch"]) if row is not None else None
    source_map = _map_sources(client, branch, names, progress=progress)

    parsed: list[rpmparse.ParsedRPM] = []
    for b in binaries:
        item = rpmparse.ParsedRPM(
            raw=str(b.get("name") or ""),
            name=str(b.get("name") or ""),
            version=str(b.get("version") or ""),
            release=str(b.get("release") or ""),
            arch=str(b.get("arch") or ""),
            ok=True,
        )
        parsed.append(item)

    list_obj = _insert(
        conn,
        title=title or (str(row["file"]) if row is not None and row["file"] else f"образ {uuid[:8]}"),
        kind="image",
        product_id=product_id,
        params={"uuid": uuid, "branch": branch, "tag": str(row["tag"]) if row is not None else ""},
        parsed=parsed,
        source_map={k: v for k, v in source_map.items() if v},
        summaries=summaries,
        bad_lines=0,
    )
    return list_obj, {"parsed": len(parsed), "bad_count": 0, "mapped": sum(1 for v in source_map.values() if v)}


def save_branch_list(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    *,
    branch: str,
    arch: str | Sequence[str] | None = None,
    title: str | None = None,
    product_id: int | None = None,
    progress: Any = None,
) -> tuple[PackageList, dict[str, Any]]:
    """Snapshot a released repository branch (export endpoint carries source).

    ``arch`` accepts one architecture, several (one export request each,
    deduplicated by package name) or ``None``/empty for the whole branch.
    """
    if isinstance(arch, str):
        archs = [arch] if arch else []
    else:
        archs = [str(a) for a in (arch or []) if a]

    packages: list[dict[str, Any]] = []
    if archs:
        seen: set[str] = set()
        for one in archs:
            if progress:
                progress(f"загрузка репозитория ({one})…")
            for p in client.branch_binary_packages(branch, arch=one):
                name = str(p.get("name") or "")
                if name and name not in seen:
                    seen.add(name)
                    packages.append(p)
    else:
        if progress:
            progress("загрузка репозитория…")
        packages = client.branch_binary_packages(branch)

    parsed: list[rpmparse.ParsedRPM] = []
    source_map: dict[str, str | None] = {}
    summaries: dict[str, str] = {}
    for p in packages:
        name = str(p.get("name") or "")
        if not name:
            continue
        parsed.append(
            rpmparse.ParsedRPM(
                raw=name,
                name=name,
                version=str(p.get("version") or ""),
                release=str(p.get("release") or ""),
                arch=str(p.get("arch") or ""),
                ok=True,
            )
        )
        source = p.get("source")
        if source:
            source_map[name.casefold()] = str(source)

    arch_param: str | list[str] | None = (
        archs[0] if len(archs) == 1 else (archs or None)
    )
    arch_part = (
        f"-{archs[0]}" if len(archs) == 1 else (f"-{'+'.join(archs)}" if archs else "")
    )
    list_obj = _insert(
        conn,
        title=title or f"{branch}{arch_part} (репозиторий)",
        kind="branch",
        product_id=product_id,
        params={"branch": branch, "arch": arch_param},
        parsed=parsed,
        source_map=source_map,
        summaries=summaries,
        bad_lines=0,
    )
    return list_obj, {"parsed": len(parsed), "bad_count": 0, "mapped": len(source_map)}


# ---------------------------------------------------------------------------
# Read / delete
# ---------------------------------------------------------------------------
def list_lists(conn: sqlite3.Connection, *, product_id: int | None = None) -> list[PackageList]:
    if product_id is None:
        rows = conn.execute("SELECT * FROM lists ORDER BY created_at DESC")
    else:
        rows = conn.execute(
            "SELECT * FROM lists WHERE product_id = ? ORDER BY created_at DESC",
            (product_id,),
        )
    return [PackageList.from_row(r) for r in rows]


def get_list(conn: sqlite3.Connection, key: int | str) -> PackageList | None:
    row = conn.execute("SELECT * FROM lists WHERE id = ?", (int(key),)).fetchone() if (
        isinstance(key, int) or str(key).isdigit()
    ) else None
    return PackageList.from_row(row) if row else None


def require_list(conn: sqlite3.Connection, key: int | str) -> PackageList:
    found = get_list(conn, key)
    if found is None:
        raise KeyError(f"список {key!r} не найден")
    return found


def list_items(conn: sqlite3.Connection, list_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM list_items WHERE list_id = ? ORDER BY name COLLATE NOCASE",
            (list_id,),
        )
    )


def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
    """Delete a list and any comparisons that reference it."""
    with conn:
        conn.execute(
            "DELETE FROM comparisons WHERE left_list_id = ? OR right_list_id = ?",
            (list_id, list_id),
        )
        conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
