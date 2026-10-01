"""CRUD for the list of tracked packages, with branch presence validation."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import journal
from .api import ALTRepoClient, ApiError, PackageNotFound
from .models import TRACKING_STARTED, TrackedPackage

# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def list_packages(
    conn: sqlite3.Connection, *, enabled: bool | None = None, q: str | None = None
) -> list[TrackedPackage]:
    sql = "SELECT * FROM tracked_packages"
    where: list[str] = []
    args: list[Any] = []
    if enabled is not None:
        where.append("enabled = ?")
        args.append(1 if enabled else 0)
    if q:
        where.append("name LIKE ?")
        args.append(f"%{q}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name COLLATE NOCASE"
    return [TrackedPackage.from_row(r) for r in conn.execute(sql, args)]


def get_package(conn: sqlite3.Connection, key: int | str) -> TrackedPackage | None:
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        row = conn.execute("SELECT * FROM tracked_packages WHERE id = ?", (int(key),)).fetchone()
        if row:
            return TrackedPackage.from_row(row)
    row = conn.execute(
        "SELECT * FROM tracked_packages WHERE name = ? COLLATE NOCASE", (str(key),)
    ).fetchone()
    return TrackedPackage.from_row(row) if row else None


def require_package(conn: sqlite3.Connection, key: int | str) -> TrackedPackage:
    pkg = get_package(conn, key)
    if pkg is None:
        raise KeyError(f"package {key!r} is not tracked")
    return pkg


def snapshots(conn: sqlite3.Connection, package_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM snapshots WHERE package_id = ? ORDER BY branch", (package_id,)
        )
    )


def snapshot_map(conn: sqlite3.Connection, package_id: int) -> dict[str, sqlite3.Row]:
    return {row["branch"]: row for row in snapshots(conn, package_id)}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class ValidationError(ValueError):
    """User-facing validation failure (bad name, bad branch set, ...)."""


def validate_name(client: ALTRepoClient, name: str) -> str:
    """Resolve the name to a source package; raise ValidationError if unknown."""
    name = (name or "").strip()
    if not name:
        raise ValidationError("имя пакета не может быть пустым")
    try:
        versions = client.source_package_versions(name)
    except PackageNotFound:
        raise ValidationError(f"пакет {name!r} не найден в репозиториях") from None
    except ApiError as exc:
        raise ValidationError(f"ошибка API при проверке имени: {exc}") from exc
    if not versions:
        raise ValidationError(f"пакет {name!r} не найден в репозиториях")
    return name


def check_branches(
    client: ALTRepoClient, name: str, branches: list[str]
) -> dict[str, Any]:
    """Check the requested branches against reality.

    Returns a report: ``{active, present, missing, inactive}`` where
    ``missing`` are active branches without the package (allowed, but the user
    is warned) and ``inactive`` are branches that are not published anymore.
    """
    active = client.active_packagesets()
    try:
        versions = client.source_package_versions(name)
    except PackageNotFound:
        versions = []
    present = {str(v.get("branch")) for v in versions if v.get("branch")}

    report = {
        "active_branches": active,
        "present_branches": sorted(present),
        "requested": branches,
        "ok": [b for b in branches if b in present and b in active],
        "missing": [b for b in branches if b not in present and b in active],
        "inactive": [b for b in branches if b not in active],
        "warnings": [],
    }
    for branch in report["missing"]:
        report["warnings"].append(
            f"в ветке {branch} пакета {name} сейчас нет — отслеживание разрешено, "
            f"событие появления будет записано автоматически"
        )
    for branch in report["inactive"]:
        report["warnings"].append(
            f"ветка {branch} не входит в опубликованные пакетные наборы — "
            f"отслеживать её бессмысленно"
        )
    return report


# ---------------------------------------------------------------------------
# Create / Update / Delete
# ---------------------------------------------------------------------------
def add_package(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    *,
    name: str,
    branches: list[str] | None = None,
    note: str = "",
    watch_errata: bool = True,
    watch_maintainer: bool = True,
    watch_tasks: bool = True,
    backfill_limit: int = 50,
    backfill: bool = True,
    run_id: int | None = None,
) -> tuple[TrackedPackage, dict[str, Any]]:
    """Create a tracked package, seed its snapshot and import build history."""
    from . import refresh  # local import to avoid a cycle

    name = validate_name(client, name)
    existing = get_package(conn, name)
    if existing is not None:
        raise ValidationError(
            f"пакет {name} уже отслеживается (id={existing.id}) — используйте "
            f"alttrack watch edit {existing.id}"
        )
    active = client.active_packagesets()
    versions = {str(v.get("branch")): v for v in client.source_package_versions(name)}

    if branches is None:
        # Sensible default: all active branches where the package exists.
        branches = [b for b in active if b in versions]
    branches = [b for b in dict.fromkeys(branches)]
    if not branches:
        where = ", ".join(sorted(versions)) if versions else "нет ни одной ветки"
        raise ValidationError(f"не выбрано ни одной ветки; пакет опубликован в: {where}")
    unknown = [b for b in branches if b not in versions and b not in active]
    if unknown:
        raise ValidationError("неизвестные ветки: " + ", ".join(unknown))

    report = check_branches(client, name, branches)

    now = journal.utcnow()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO tracked_packages
                (name, branches, note, watch_errata, watch_maintainer, watch_tasks,
                 enabled, added_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (name, json.dumps(branches), note, int(watch_errata), int(watch_maintainer),
             int(watch_tasks), now),
        )
        package_id = int(cur.lastrowid)

        # Baseline snapshot: current state, without generating history events.
        for branch in branches:
            v = versions.get(branch)
            if v is None:
                conn.execute(
                    """
                    INSERT INTO snapshots (package_id, branch, present, observed_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (package_id, branch, now),
                )
                continue
            conn.execute(
                """
                INSERT INTO snapshots
                    (package_id, branch, present, version, release, pkghash, observed_at)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (package_id, branch, v.get("version"), v.get("release"), v.get("pkghash"), now),
            )

    journal.insert_event(
        conn,
        package=name,
        package_id=package_id,
        event_type=TRACKING_STARTED,
        branch=None,
        new_value=",".join(branches),
        detail={"note": note, "warnings": report["warnings"], "backfill": backfill},
        ts=now,
        run_id=run_id,
    )

    if backfill and backfill_limit:
        refresh.backfill_build_history(conn, client, package_id, name, branches, backfill_limit)
        refresh.import_tasks_history(conn, client, package_id, name, branches, run_id=run_id)

    return require_package(conn, package_id), report


def update_package(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    package: TrackedPackage,
    *,
    branches: list[str] | None = None,
    note: str | None = None,
    watch_errata: bool | None = None,
    watch_maintainer: bool | None = None,
    watch_tasks: bool | None = None,
    enabled: bool | None = None,
    run_id: int | None = None,
) -> tuple[TrackedPackage, dict[str, Any]]:
    """Edit a tracked package.

    Snapshots of removed branches are kept: if the branch is added back later,
    the comparison resumes from the stored snapshot (per the agreed rules).
    """
    from . import refresh  # local import

    report: dict[str, Any] = {"warnings": [], "added": [], "removed": [], "kept": []}
    name = package.name
    active = client.active_packagesets()

    if branches is not None:
        branches = list(dict.fromkeys(branches))
        if not branches:
            raise ValidationError("нельзя оставить пустой список веток — удалите пакет")
        versions = {str(v.get("branch")) for v in client.source_package_versions(name)}
        unknown = [b for b in branches if b not in versions and b not in active]
        if unknown:
            raise ValidationError("неизвестные ветки: " + ", ".join(unknown))

        report = check_branches(client, name, branches)
        old = set(package.branches)
        new = set(branches)
        report["added"] = sorted(new - old)
        report["removed"] = sorted(old - new)
        report["kept"] = sorted(old & new)

        with conn:
            conn.execute(
                "UPDATE tracked_packages SET branches = ? WHERE id = ?",
                (json.dumps(branches), package.id),
            )
            # Seed snapshots for newly added branches (baseline, no events).
            for branch in report["added"]:
                cur = conn.execute(
                    "SELECT 1 FROM snapshots WHERE package_id = ? AND branch = ?",
                    (package.id, branch),
                ).fetchone()
                if cur:
                    continue  # snapshot kept: compare against the stored state
                versions_now = {
                    str(v.get("branch")): v for v in client.source_package_versions(name)
                }
                v = versions_now.get(branch)
                if v is None:
                    conn.execute(
                        "INSERT INTO snapshots (package_id, branch, present, observed_at) "
                        "VALUES (?, ?, 0, ?)",
                        (package.id, branch, journal.utcnow()),
                    )
                else:
                    conn.execute(
                        "INSERT INTO snapshots (package_id, branch, present, version, release, "
                        "pkghash, observed_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                        (package.id, branch, v.get("version"), v.get("release"),
                         v.get("pkghash"), journal.utcnow()),
                    )
                refresh.import_tasks_history(
                    conn, client, package.id, name, [branch], run_id=run_id
                )

    sets: list[str] = []
    args: list[Any] = []
    if note is not None:
        sets.append("note = ?")
        args.append(note)
    for column, value in (
        ("watch_errata", watch_errata),
        ("watch_maintainer", watch_maintainer),
        ("watch_tasks", watch_tasks),
        ("enabled", enabled),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(int(value))
    if sets:
        sets.append("last_checked_at = last_checked_at")
        args.append(package.id)
        with conn:
            conn.execute(f"UPDATE tracked_packages SET {', '.join(sets)} WHERE id = ?", args)

    return require_package(conn, package.id), report


def delete_package(
    conn: sqlite3.Connection, package: TrackedPackage, *, purge: bool = False
) -> None:
    """Remove a package from tracking; ``purge`` also drops journal and tasks."""
    with conn:
        if purge:
            for store in ("journal", "journal_archive"):
                seqs = [
                    int(r["seq"])
                    for r in conn.execute(
                        f"SELECT seq FROM {store} WHERE package_id = ?", (package.id,)
                    )
                ]
                for seq in seqs:
                    conn.execute("DELETE FROM events_fts WHERE rowid = ?", (seq,))
                conn.execute(f"DELETE FROM {store} WHERE package_id = ?", (package.id,))
            task_ids = [
                int(r["task_id"])
                for r in conn.execute(
                    "SELECT DISTINCT task_id FROM task_packages WHERE package_id = ?",
                    (package.id,),
                )
            ]
            conn.execute("DELETE FROM task_packages WHERE package_id = ?", (package.id,))
            # A task may be shared with other tracked packages (mass rebuilds).
            for task_id in task_ids:
                still = conn.execute(
                    "SELECT 1 FROM task_packages WHERE task_id = ? LIMIT 1", (task_id,)
                ).fetchone()
                if still:
                    continue
                conn.execute("DELETE FROM build_tasks WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM task_stages WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM errata_seen WHERE package_id = ?", (package.id,))
            conn.execute("DELETE FROM tasks_seen WHERE package_id = ?", (package.id,))
        conn.execute("DELETE FROM snapshots WHERE package_id = ?", (package.id,))
        conn.execute("DELETE FROM tracked_packages WHERE id = ?", (package.id,))


def autocomplete(client: ALTRepoClient, fragment: str, *, limit: int = 15) -> list[dict[str, Any]]:
    """Package name suggestions for CLI and web forms."""
    fragment = (fragment or "").strip()
    if not fragment:
        return []
    try:
        found = client.find_packages(fragment, limit=limit)
    except ApiError:
        return []
    out: list[dict[str, Any]] = []
    for item in found:
        versions = item.get("versions") or []
        branches = [v.get("branch") for v in versions if v.get("branch")]
        out.append(
            {
                "name": item.get("name"),
                "summary": item.get("summary") or "",
                "branches": branches,
            }
        )
    return out
