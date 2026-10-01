"""Build task watcher: poll, journal transitions, expose task listings.

Build tasks are polled together with the regular repository refresh (per the
agreed design).  Stage progression (``task-build`` -> ``task-repo-elfsym`` -> ...)
is stored as a timeline in ``task_stages``; the journal only receives coarse
events: discovery, state change, failure, success and retries.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from . import journal
from .api import ALTRepoClient, ApiError
from .models import (
    TASK_DISCOVERED,
    TASK_DONE,
    TASK_FAILED,
    TASK_RETRIED,
    TASK_STATE_CHANGED,
    TrackedPackage,
    task_is_terminal,
    task_outcome,
)

# Tasks untouched for longer than this are ignored on first sight.
DEFAULT_HISTORY_DAYS = 7


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event(
    conn: sqlite3.Connection,
    *,
    pkg: TrackedPackage,
    task: dict[str, Any],
    event_type: str,
    old: str | None,
    new: str | None,
    detail: dict[str, Any],
    run_id: int | None,
) -> int:
    return journal.insert_event(
        conn,
        package=pkg.name,
        package_id=pkg.id,
        branch=str(task.get("task_repo") or ""),
        event_type=event_type,
        old_value=old,
        new_value=new,
        detail={"task_id": task.get("task_id"), **detail},
        run_id=run_id,
    )


def _record_stage(conn: sqlite3.Connection, task_id: int, state: str, stage: str | None) -> None:
    last = conn.execute(
        "SELECT stage, state FROM task_stages WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if last and last["stage"] == stage and last["state"] == state:
        return
    conn.execute(
        "INSERT INTO task_stages (task_id, ts, stage, state) VALUES (?, ?, ?, ?)",
        (task_id, journal.utcnow(), stage, state),
    )


def poll_tasks(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    packages: Sequence[TrackedPackage],
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    run_id: int | None = None,
    tasks_limit: int = 100,
) -> int:
    """Poll live build tasks for every tracked package; returns event count."""
    watched = [p for p in packages if p.enabled and p.watch_tasks and p.branches]
    if not watched:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(history_days, 1))
    events = 0

    for pkg in watched:
        try:
            tasks = client.find_tasks([pkg.name], tasks_limit=tasks_limit)
        except ApiError:
            continue
        for task in tasks:
            events += _handle_task(conn, pkg, task, cutoff=cutoff, run_id=run_id)
    return events


def _handle_task(
    conn: sqlite3.Connection,
    pkg: TrackedPackage,
    task: dict[str, Any],
    *,
    cutoff: datetime,
    run_id: int | None,
) -> int:
    branch = str(task.get("task_repo") or "")
    if branch not in pkg.branches:
        return 0
    task_id = int(task.get("task_id") or 0)
    if not task_id:
        return 0

    state = str(task.get("task_state") or "")
    stage = task.get("task_stage") or None
    owner = task.get("task_owner") or None
    try_no = int(task.get("task_try") or 0)
    iter_no = int(task.get("task_iter") or 0)
    testonly = int(bool(task.get("task_testonly")))
    changed_at = task.get("task_changed") or None
    message = str(task.get("task_message") or "")
    subtasks = task.get("subtasks") or []
    now = journal.utcnow()

    prev = conn.execute(
        "SELECT * FROM build_tasks WHERE task_id = ? AND branch = ?", (task_id, branch)
    ).fetchone()

    if prev is None:
        # Brand new observation.
        changed_dt = parse_ts(changed_at)
        terminal = task_is_terminal(state)
        if terminal:
            # Completed tasks count as fresh only within the history window;
            # older ones are skipped entirely (history is backfilled from
            # /site/tasks_by_package when the package is added).
            if changed_dt is None or changed_dt < cutoff:
                return 0
            fresh = True
        else:
            fresh = changed_dt is None or changed_dt >= cutoff

        with conn:
            conn.execute(
                """
                INSERT INTO build_tasks
                    (task_id, branch, state, stage, owner, try_no, iter_no, testonly,
                     changed_at, message, subtasks, first_seen_at, last_seen_at,
                     terminal, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT (task_id, branch) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (task_id, branch, state, stage, owner, try_no, iter_no, testonly,
                 changed_at, message, json.dumps(subtasks, ensure_ascii=False),
                 now, now, int(terminal)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_packages (task_id, package_id, subtask_tag) "
                "VALUES (?, ?, ?)",
                (task_id, pkg.id, _subtask_tag(pkg, subtasks)),
            )
            _record_stage(conn, task_id, state, stage)

        if not fresh:
            return 0

        if task_outcome(state) == "failure":
            _event(conn, pkg=pkg, task=task, event_type=TASK_FAILED, old=None, new=state,
                   detail=_task_detail(task), run_id=run_id)
            return 1
        if terminal:
            # A freshly seen completed task is recorded silently: its effect on
            # the repository shows up as version_changed, and a transition to
            # DONE will be journaled later if the task was observed running.
            return 0
        _event(conn, pkg=pkg, task=task, event_type=TASK_DISCOVERED, old=None, new=state,
               detail=_task_detail(task), run_id=run_id)
        return 1

    # -- known task --------------------------------------------------------
    prev_state = str(prev["state"])
    prev_try = int(prev["try_no"] or 0)
    prev_stage = prev["stage"]
    events = 0

    if state == prev_state and try_no == prev_try and stage == prev_stage:
        # Nothing new, but refresh the timestamp.
        conn.execute(
            "UPDATE build_tasks SET last_seen_at = ?, message = ?, subtasks = ? WHERE task_id = ? AND branch = ?",
            (now, message, json.dumps(subtasks, ensure_ascii=False), task_id, branch),
        )
        return 0

    with conn:
        conn.execute(
            """
            UPDATE build_tasks SET state = ?, stage = ?, owner = ?, try_no = ?, iter_no = ?,
                testonly = ?, changed_at = ?, message = ?, subtasks = ?, last_seen_at = ?,
                terminal = ?
            WHERE task_id = ? AND branch = ?
            """,
            (state, stage, owner, try_no, iter_no, testonly, changed_at, message,
             json.dumps(subtasks, ensure_ascii=False), now, int(task_is_terminal(state)),
             task_id, branch),
        )
        _record_stage(conn, task_id, state, stage)

    detail = _task_detail(task)

    if try_no > prev_try and prev_try:
        _event(conn, pkg=pkg, task=task, event_type=TASK_RETRIED, old=str(prev_try),
               new=str(try_no), detail=detail, run_id=run_id)
        events += 1

    if state != prev_state:
        outcome = task_outcome(state)
        if outcome == "failure":
            event_type = TASK_FAILED
        elif outcome == "success":
            event_type = TASK_DONE
        else:
            event_type = TASK_STATE_CHANGED
        _event(conn, pkg=pkg, task=task, event_type=event_type, old=prev_state, new=state,
               detail={**detail, "stage": stage}, run_id=run_id)
        events += 1
    elif stage != prev_stage:
        # Stage movement alone is kept in the timeline only (agreed rule).
        pass

    return events


def _task_detail(task: dict[str, Any]) -> dict[str, Any]:
    subtasks = task.get("subtasks") or []
    return {
        "task_id": task.get("task_id"),
        "owner": task.get("task_owner"),
        "stage": task.get("task_stage"),
        "try": task.get("task_try"),
        "iter": task.get("task_iter"),
        "changed": task.get("task_changed"),
        "message": task.get("task_message") or "",
        "nvr": [s.get("subtask_tag_name") for s in subtasks if s.get("subtask_tag_name")][:5],
    }


def _subtask_tag(pkg: TrackedPackage, subtasks: Iterable[dict[str, Any]]) -> str:
    """Best-effort NVR of the tracked package inside the task."""
    wanted = pkg.name.lower()
    for sub in subtasks:
        tag = str(sub.get("subtask_tag_name") or "")
        if not tag:
            continue
        if wanted in str(sub.get("subtask_dir") or "").lower() or wanted in tag.lower():
            return tag
    for sub in subtasks:
        tag = str(sub.get("subtask_tag_name") or "")
        if tag:
            return tag
    return ""


def import_task_history(
    conn: sqlite3.Connection,
    client: ALTRepoClient,
    package: TrackedPackage,
    *,
    limit: int = 50,
    branches: Sequence[str] | None = None,
    run_id: int | None = None,
) -> int:
    """Backfill closed build tasks of a package (no journal events)."""
    del run_id
    try:
        history = client.tasks_by_package(package.name, limit=limit)
    except ApiError:
        return 0
    wanted = set(branches or package.branches)
    now = journal.utcnow()
    added = 0
    for task in history:
        branch = str(task.get("branch") or "")
        if branch not in wanted:
            continue
        task_id = int(task.get("id") or 0)
        if not task_id:
            continue
        state = str(task.get("state") or "")
        changed = task.get("changed")
        with conn:
            conn.execute(
                """
                INSERT INTO build_tasks
                    (task_id, branch, state, stage, owner, changed_at, subtasks,
                     first_seen_at, last_seen_at, terminal, resolved)
                VALUES (?, ?, ?, NULL, ?, ?, '[]', ?, ?, ?, 1)
                ON CONFLICT (task_id, branch) DO NOTHING
                """,
                (task_id, branch, state, task.get("owner"), changed, now, now,
                 int(task_is_terminal(state))),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_packages (task_id, package_id, subtask_tag) "
                "VALUES (?, ?, ?)",
                (task_id, package.id, _tag_from_history(task, package.name)),
            )
            added += cur.rowcount
        _record_stage(conn, task_id, state, None)
    return added


def _tag_from_history(task: dict[str, Any], name: str) -> str:
    for pkg in task.get("packages") or []:
        if str(pkg.get("name") or "") == name:
            return f"{pkg.get('version')}-{pkg.get('release')}"
    return ""


# ---------------------------------------------------------------------------
# Queries for CLI / web
# ---------------------------------------------------------------------------
def list_tasks(
    conn: sqlite3.Connection,
    *,
    package: str | None = None,
    package_ids: Sequence[int] | None = None,
    branch: str | None = None,
    active: bool | None = None,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    args: list[Any] = []
    if package:
        where.append("tp.package_id = (SELECT id FROM tracked_packages WHERE name = ? COLLATE NOCASE)")
        args.append(package)
    if package_ids:
        ids = [int(i) for i in dict.fromkeys(package_ids)]
        clauses: list[str] = []
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            clauses.append(f"tp.package_id IN ({','.join('?' for _ in chunk)})")
            args.extend(chunk)
        where.append("(" + " OR ".join(clauses) + ")")
    if branch:
        where.append("bt.branch = ?")
        args.append(branch)
    if active is True:
        where.append("bt.terminal = 0")
    elif active is False:
        where.append("bt.terminal = 1")
    if outcome == "failure":
        where.append("bt.state IN ('FAILED','EPERM')")
    elif outcome == "success":
        where.append("bt.state = 'DONE'")

    join = "FROM build_tasks bt LEFT JOIN task_packages tp ON tp.task_id = bt.task_id"
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(DISTINCT bt.task_id || ':' || bt.branch) AS n {join} {where_sql}", args
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT bt.*,
               (SELECT GROUP_CONCAT(DISTINCT p.name) FROM task_packages tp2
                  JOIN tracked_packages p ON p.id = tp2.package_id
                 WHERE tp2.task_id = bt.task_id) AS packages
        {join} {where_sql}
        GROUP BY bt.task_id, bt.branch
        ORDER BY bt.terminal ASC, bt.last_seen_at DESC
        LIMIT ? OFFSET ?
        """,
        [*args, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], int(total)


def get_task(conn: sqlite3.Connection, task_id: int, branch: str | None = None) -> dict[str, Any] | None:
    if branch:
        row = conn.execute(
            "SELECT * FROM build_tasks WHERE task_id = ? AND branch = ?", (task_id, branch)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM build_tasks WHERE task_id = ? ORDER BY last_seen_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["stages"] = [
        dict(r)
        for r in conn.execute(
            "SELECT ts, stage, state FROM task_stages WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]
    data["packages"] = [
        r["name"]
        for r in conn.execute(
            "SELECT DISTINCT p.name FROM task_packages tp "
            "JOIN tracked_packages p ON p.id = tp.package_id WHERE tp.task_id = ?",
            (task_id,),
        )
    ]
    data["events"] = [
        dict(r)
        for r in conn.execute(
            "SELECT seq, ts, branch, event_type, old_value, new_value FROM journal "
            "WHERE json_extract(detail, '$.task_id') = ? ORDER BY ts DESC LIMIT 20",
            (task_id,),
        )
    ]
    try:
        data["subtasks"] = json.loads(data.get("subtasks") or "[]")
    except (TypeError, ValueError):
        data["subtasks"] = []
    return data


def task_counters(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN terminal = 0 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN terminal = 1 AND state = 'DONE' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN state IN ('FAILED','EPERM') THEN 1 ELSE 0 END) AS failed, "
        "COUNT(*) AS total "
        "FROM build_tasks"
    ).fetchone()
    return {k: int(row[k] or 0) for k in ("active", "done", "failed", "total")}
