"""Repository refresh: diff snapshots, emit lifecycle events, poll build tasks.

One refresh pass:
  1. record a run;
  2. fetch current versions (and erratas) for every tracked package in parallel;
  3. fetch build metadata (packager) for changed builds only;
  4. diff against the stored snapshots and write journal events serially;
  5. refresh the ACL snapshot in one batched call per branch;
  6. poll build tasks;
  7. auto-archive the journal if configured.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from typing import Any, Sequence

from . import journal, refs, tasks as tasks_mod, watchlist
from .api import ALTRepoClient, ApiError, PackageNotFound
from .config import Config
from .models import (
    ADDED_TO_BRANCH,
    ERRATA,
    MAINTAINER_CHANGED,
    NOT_FOUND,
    REMOVED_FROM_BRANCH,
    VERSION_CHANGED,
    TrackedPackage,
)

log = logging.getLogger("alttrack.refresh")

# In-process guard: CLI and the web UI share one database.
_run_lock = threading.Lock()


@dataclasses.dataclass
class RefreshResult:
    run_id: int | None = None
    status: str = "ok"          # ok | partial | error | busy
    checked: int = 0
    events: int = 0
    message: str = ""
    errors: list[str] = dataclasses.field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def busy(self) -> bool:
        return self.status == "busy"


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def start_run(conn: sqlite3.Connection) -> int:
    now = journal.utcnow()
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now,)
        )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    checked: int = 0,
    events: int = 0,
    message: str = "",
) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, checked = ?, events = ?, message = ? "
            "WHERE id = ?",
            (journal.utcnow(), status, checked, events, message, run_id),
        )


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def last_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Backfill helpers (used by watchlist.add_package)
# ---------------------------------------------------------------------------
def backfill_build_history(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    package_id: int,
    name: str,
    branches: Sequence[str],
    limit: int,
) -> int:
    """Import past build tasks of a package as ``build`` journal events."""
    added = 0
    for branch in branches:
        try:
            history = client.package_versions_from_tasks(name, branch, limit=limit)
        except ApiError:
            continue
        for item in history[:limit]:
            pkghash = str(item.get("hash") or item.get("pkghash") or "")
            task_id = int(item.get("task") or 0)
            if not pkghash:
                continue
            key = (pkghash, package_id, branch)
            already = conn.execute(
                "SELECT 1 FROM tasks_seen WHERE pkghash = ? AND package_id = ? AND branch = ?",
                key,
            ).fetchone()
            if already:
                continue
            nvr = f"{item.get('version')}-{item.get('release')}"
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO tasks_seen (pkghash, package_id, branch, first_seen) "
                    "VALUES (?, ?, ?, ?)",
                    (pkghash, package_id, branch, journal.utcnow()),
                )
            journal.insert_event(
                conn,
                package=name,
                package_id=package_id,
                branch=branch,
                event_type="build",
                new_value=nvr,
                detail={
                    "task": task_id,
                    "pkghash": pkghash,
                    "owner": item.get("owner"),
                    "changed": item.get("changed"),
                    "backfill": True,
                },
                ts=str(item.get("changed") or journal.utcnow()),
            )
            added += 1
    return added


def import_tasks_history(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    package_id: int,
    name: str,
    branches: Sequence[str],
    run_id: int | None = None,
) -> int:
    """Backfill closed build-task records (shown on the tasks page)."""
    pkg = watchlist.get_package(conn, package_id)
    if pkg is None:
        return 0
    return tasks_mod.import_task_history(
        conn, client, pkg, branches=branches, run_id=run_id
    )


# ---------------------------------------------------------------------------
# Fetch phase (parallel)
# ---------------------------------------------------------------------------
def _fetch_package(client: ALTRepoClient, pkg: TrackedPackage, cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {"id": pkg.id, "name": pkg.name, "versions": None,
                           "errata": None, "error": None, "missing": False}
    try:
        out["versions"] = client.source_package_versions(pkg.name)
    except PackageNotFound:
        out["missing"] = True
    except ApiError as exc:
        out["error"] = f"{pkg.name}: versions: {exc}"
    if pkg.watch_errata and out["versions"] is not None:
        try:
            out["errata"] = client.errata_search(pkg.name)
        except ApiError as exc:
            out["error"] = f"{pkg.name}: errata: {exc}"
    return out


def _fetch_build_meta(client: ALTRepoClient, item: tuple[str, str]) -> dict[str, Any]:
    pkghash, branch = item
    try:
        info = client.package_info(pkghash, branch)
    except ApiError as exc:
        return {"pkghash": pkghash, "branch": branch, "error": str(exc)}
    return {
        "pkghash": pkghash,
        "branch": branch,
        "packager": info.get("packager"),
        "packager_nick": info.get("packager_nickname"),
        "task": info.get("task"),
        "task_date": info.get("task_date"),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Diff phase (serial writes)
# ---------------------------------------------------------------------------
def _diff_package(
    conn: sqlite3.Connection,
    pkg: TrackedPackage,
    data: dict[str, Any],
    meta: dict[tuple[str, str], dict[str, Any]],
    *,
    active: set[str],
    run_id: int,
) -> int:
    events = 0
    now = journal.utcnow()

    if data.get("error"):
        return 0

    if data.get("missing"):
        last = conn.execute(
            "SELECT event_type FROM journal WHERE package_id = ? ORDER BY seq DESC LIMIT 1",
            (pkg.id,),
        ).fetchone()
        if last is None or last["event_type"] != NOT_FOUND:
            journal.insert_event(
                conn,
                package=pkg.name,
                package_id=pkg.id,
                event_type=NOT_FOUND,
                detail={"reason": "пакет отсутствует в репозиториях API"},
                run_id=run_id,
            )
            events += 1
        return events

    versions = {str(v.get("branch")): v for v in (data.get("versions") or [])}
    snapshots = watchlist.snapshot_map(conn, pkg.id)

    for branch in pkg.branches:
        if branch not in active:
            # Retired branch: keep the snapshot untouched.
            continue
        current = versions.get(branch)
        snap = snapshots.get(branch)

        if snap is None:
            # Snapshot missing (should not happen): seed it silently.
            _seed_snapshot(conn, pkg.id, branch, current, now)
            continue

        if current is None:
            if int(snap["present"] or 0) == 1:
                with conn:
                    conn.execute(
                        "UPDATE snapshots SET present = 0, version = NULL, release = NULL, "
                        "pkghash = NULL, observed_at = ? WHERE package_id = ? AND branch = ?",
                        (now, pkg.id, branch),
                    )
                journal.insert_event(
                    conn,
                    package=pkg.name,
                    package_id=pkg.id,
                    branch=branch,
                    event_type=REMOVED_FROM_BRANCH,
                    old_value=_nvr(snap["version"], snap["release"]),
                    run_id=run_id,
                    detail={"last_pkghash": snap["pkghash"]},
                )
                events += 1
            continue

        version = str(current.get("version") or "")
        release = str(current.get("release") or "")
        pkghash = str(current.get("pkghash") or "")

        if int(snap["present"] or 0) == 0:
            with conn:
                conn.execute(
                    "UPDATE snapshots SET present = 1, version = ?, release = ?, pkghash = ?, "
                    "observed_at = ? WHERE package_id = ? AND branch = ?",
                    (version, release, pkghash, now, pkg.id, branch),
                )
            journal.insert_event(
                conn,
                package=pkg.name,
                package_id=pkg.id,
                branch=branch,
                event_type=ADDED_TO_BRANCH,
                new_value=_nvr(version, release),
                run_id=run_id,
                detail={"pkghash": pkghash},
            )
            events += 1
            _maintainer_diff(conn, pkg, branch, snap, meta.get((pkghash, branch)), run_id)
            continue

        old_nvr = _nvr(snap["version"], snap["release"])
        new_nvr = _nvr(version, release)
        changed = old_nvr != new_nvr or str(snap["pkghash"] or "") != pkghash
        if not changed:
            # Still refresh packager/ACL provenance if we never captured it.
            if pkg.watch_maintainer and not snap["packager"]:
                _maintainer_diff(conn, pkg, branch, snap, meta.get((pkghash, branch)), run_id)
            continue

        with conn:
            conn.execute(
                "UPDATE snapshots SET present = 1, version = ?, release = ?, pkghash = ?, "
                "observed_at = ? WHERE package_id = ? AND branch = ?",
                (version, release, pkghash, now, pkg.id, branch),
            )
        journal.insert_event(
            conn,
            package=pkg.name,
            package_id=pkg.id,
            branch=branch,
            event_type=VERSION_CHANGED,
            old_value=old_nvr,
            new_value=new_nvr,
            run_id=run_id,
            detail={"pkghash": pkghash, "task": (meta.get((pkghash, branch)) or {}).get("task")},
        )
        events += 1
        _maintainer_diff(conn, pkg, branch, snap, meta.get((pkghash, branch)), run_id)

    events += _errata_diff(conn, pkg, data.get("errata") or [], run_id=run_id)
    return events


def _maintainer_diff(
    conn: sqlite3.Connection,
    pkg: TrackedPackage,
    branch: str,
    snap: sqlite3.Row,
    info: dict[str, Any] | None,
    run_id: int,
) -> None:
    if not pkg.watch_maintainer or not info or info.get("error"):
        return
    packager = info.get("packager")
    if not packager:
        return
    old = snap["packager"]
    if old and str(old) != str(packager):
        journal.insert_event(
            conn,
            package=pkg.name,
            package_id=pkg.id,
            branch=branch,
            event_type=MAINTAINER_CHANGED,
            old_value=str(old),
            new_value=str(packager),
            run_id=run_id,
            detail={"old_nick": snap["packager_nick"], "new_nick": info.get("packager_nick")},
        )
        with conn:
            conn.execute(
                "UPDATE snapshots SET packager = ?, packager_nick = ?, task_id = ?, task_date = ? "
                "WHERE package_id = ? AND branch = ?",
                (packager, info.get("packager_nick"), info.get("task"),
                 info.get("task_date"), pkg.id, branch),
            )
    else:
        with conn:
            conn.execute(
                "UPDATE snapshots SET packager = ?, packager_nick = ?, task_id = ?, task_date = ? "
                "WHERE package_id = ? AND branch = ?",
                (packager, info.get("packager_nick"), info.get("task"),
                 info.get("task_date"), pkg.id, branch),
            )


def _errata_diff(
    conn: sqlite3.Connection,
    pkg: TrackedPackage,
    erratas: Sequence[dict[str, Any]],
    *,
    run_id: int | None = None,
) -> int:
    """Record new erratas.

    Erratas published before the package was added to the watch list are
    remembered silently (dedup only), so adding a package does not flood the
    journal with its historical security updates.
    """
    if not pkg.watch_errata:
        return 0
    events = 0
    name_cf = pkg.name.casefold()
    added_at = pkg.added_at or ""
    for errata in erratas:
        pkg_name = str(errata.get("pkg_name") or "")
        if pkg_name.casefold() != name_cf:
            continue
        branch = str(errata.get("pkgset_name") or "")
        if branch not in pkg.branches:
            continue
        errata_id = str(errata.get("id") or "")
        if not errata_id:
            continue
        already = conn.execute(
            "SELECT 1 FROM errata_seen WHERE errata_id = ? AND package_id = ? AND branch = ?",
            (errata_id, pkg.id, branch),
        ).fetchone()
        if already:
            continue
        created = str(errata.get("created") or "")
        historical = bool(added_at and created and created < added_at)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO errata_seen (errata_id, package_id, branch, first_seen) "
                "VALUES (?, ?, ?, ?)",
                (errata_id, pkg.id, branch, journal.utcnow()),
            )
        if historical:
            continue
        refs = [
            r.get("id")
            for r in (errata.get("references") or [])
            if r.get("type") in {"vuln", "cve", "advisory"}
        ]
        journal.insert_event(
            conn,
            package=pkg.name,
            package_id=pkg.id,
            branch=branch,
            event_type=ERRATA,
            new_value=errata_id,
            run_id=run_id,
            detail={
                "errata_id": errata_id,
                "type": errata.get("type"),
                "version": _nvr(errata.get("pkg_version"), errata.get("pkg_release")),
                "refs": refs[:50],
                "created": errata.get("created"),
                "task_id": errata.get("task_id"),
            },
        )
        events += 1
    return events


def _acl_refresh(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    packages: Sequence[TrackedPackage],
    *,
    active: set[str],
    run_id: int,
) -> int:
    """One batched ACL call per branch; emits maintainer changes."""
    events = 0
    by_branch: dict[str, list[TrackedPackage]] = {}
    for pkg in packages:
        if not pkg.enabled or not pkg.watch_maintainer:
            continue
        for branch in pkg.branches:
            if branch in active:
                by_branch.setdefault(branch, []).append(pkg)

    for branch, pkgs in by_branch.items():
        names = [p.name for p in pkgs]
        try:
            acl = client.acl_by_packages(branch, names)
        except ApiError:
            continue
        name_to_pkg = {p.name.casefold(): p for p in pkgs}
        for entry in acl:
            pkg = name_to_pkg.get(str(entry.get("name") or "").casefold())
            if pkg is None:
                continue
            members = sorted(str(m) for m in (entry.get("members") or []))
            snap = conn.execute(
                "SELECT acl FROM snapshots WHERE package_id = ? AND branch = ?", (pkg.id, branch)
            ).fetchone()
            if snap is None:
                continue
            old_raw = snap["acl"]
            if old_raw is None:
                with conn:
                    conn.execute(
                        "UPDATE snapshots SET acl = ? WHERE package_id = ? AND branch = ?",
                        (",".join(members), pkg.id, branch),
                    )
                continue
            old = sorted(x for x in str(old_raw).split(",") if x)
            if old == members:
                continue
            with conn:
                conn.execute(
                    "UPDATE snapshots SET acl = ? WHERE package_id = ? AND branch = ?",
                    (",".join(members), pkg.id, branch),
                )
            journal.insert_event(
                conn,
                package=pkg.name,
                package_id=pkg.id,
                branch=branch,
                event_type=MAINTAINER_CHANGED,
                old_value=", ".join(old),
                new_value=", ".join(members),
                run_id=run_id,
                detail={"kind": "acl"},
            )
            events += 1
    return events


def _seed_snapshot(
    conn: sqlite3.Connection,
    package_id: int,
    branch: str,
    current: dict[str, Any] | None,
    now: str,
) -> None:
    with conn:
        if current is None:
            conn.execute(
                "INSERT OR IGNORE INTO snapshots (package_id, branch, present, observed_at) "
                "VALUES (?, ?, 0, ?)",
                (package_id, branch, now),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO snapshots "
                "(package_id, branch, present, version, release, pkghash, observed_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?)",
                (package_id, branch, current.get("version"), current.get("release"),
                 current.get("pkghash"), now),
            )


def _nvr(version: Any, release: Any) -> str:
    if not version:
        return ""
    if not release:
        return str(version)
    return f"{version}-{release}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_refresh(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    cfg: Config,
    *,
    package_ids: Sequence[int] | None = None,
    do_archive: bool | None = None,
) -> RefreshResult:
    """Execute one full refresh pass (thread-safe within the process)."""
    if not _run_lock.acquire(blocking=False):
        return RefreshResult(status="busy", message="предыдущее освежение ещё выполняется")
    try:
        return _refresh_locked(conn, client, cfg, package_ids, do_archive)
    finally:
        _run_lock.release()


def _refresh_locked(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    cfg: Config,
    package_ids: Sequence[int] | None,
    do_archive: bool | None,
) -> RefreshResult:
    run_id = start_run(conn)
    result = RefreshResult(run_id=run_id, started_at=journal.utcnow())

    # First database fill: read the reference lists (architectures, software
    # groups) from rdb.  Cheap afterwards — ensure() is a single COUNT(*).
    try:
        refs_ = refs.ensure(conn, client)
        if refs_:
            log.info(
                "reference lists filled: %s arches, %s categories",
                refs_["arch"], refs_["category"],
            )
    except ApiError as exc:
        log.debug("reference lists not filled: %s", exc)

    packages = watchlist.list_packages(conn, enabled=True)
    if package_ids is not None:
        wanted = set(package_ids)
        packages = [p for p in packages if p.id in wanted]

    try:
        active = set(client.active_packagesets())
    except ApiError as exc:
        finish_run(conn, run_id, status="error", message=str(exc))
        result.status = "error"
        result.message = str(exc)
        result.finished_at = journal.utcnow()
        return result

    # Cache the published packageset list for the UI (branch presence marks).
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('active_branches', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (json.dumps(sorted(active)),),
        )

    # 1. parallel fetch of versions + erratas
    fetched = client.map(lambda p: _fetch_package(client, p, cfg), packages)

    # 2. parallel fetch of build metadata for changed/new builds
    changed_keys: list[tuple[str, str]] = []
    for pkg, data in zip(packages, fetched):
        if data.get("versions") is None:
            continue
        versions = {str(v.get("branch")): v for v in data["versions"]}
        snapshots = watchlist.snapshot_map(conn, pkg.id)
        for branch in pkg.branches:
            current = versions.get(branch)
            if not current or branch not in active:
                continue
            pkghash = str(current.get("pkghash") or "")
            snap = snapshots.get(branch)
            needs_meta = (
                snap is None
                or str(snap["pkghash"] or "") != pkghash
                or (pkg.watch_maintainer and not snap["packager"])
            )
            if needs_meta and pkghash:
                changed_keys.append((pkghash, branch))

    meta_pairs = client.map(lambda item: _fetch_build_meta(client, item), changed_keys)
    meta = {(m["pkghash"], m["branch"]): m for m in meta_pairs}

    # 3. serial diff + journal writes
    errors: list[str] = [d["error"] for d in fetched if d.get("error")]
    events = 0
    checked = 0
    for pkg, data in zip(packages, fetched):
        checked += 1
        try:
            events += _diff_package(conn, pkg, data, meta, active=active, run_id=run_id)
            with conn:
                conn.execute(
                    "UPDATE tracked_packages SET last_checked_at = ? WHERE id = ?",
                    (journal.utcnow(), pkg.id),
                )
        except Exception as exc:  # noqa: BLE001 - one bad package must not stop the run
            log.exception("refresh failed for %s", pkg.name)
            errors.append(f"{pkg.name}: {exc}")

    # 4. batched ACL check
    try:
        events += _acl_refresh(conn, client, packages, active=active, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"acl: {exc}")

    # 5. build tasks
    try:
        events += tasks_mod.poll_tasks(
            conn, client, packages, history_days=cfg.task_history_days, run_id=run_id
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tasks: {exc}")

    # 6. auto-archive
    if do_archive if do_archive is not None else cfg.auto_archive:
        try:
            from . import archive as archive_mod

            archive_mod.auto_archive(conn, cfg)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"archive: {exc}")

    status = "ok" if not errors else "partial"
    message = "; ".join(errors[:5])
    finish_run(conn, run_id, status=status, checked=checked, events=events, message=message)
    result.status = status
    result.checked = checked
    result.events = events
    result.errors = errors
    result.message = message
    result.finished_at = journal.utcnow()
    return result
