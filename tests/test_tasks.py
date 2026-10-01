"""Build task watcher: discovery, transitions, failures, stage timeline."""

from __future__ import annotations

from alttrack import journal, refresh, tasks, watchlist
from alttrack.models import (
    TASK_DISCOVERED,
    TASK_DONE,
    TASK_FAILED,
    TASK_RETRIED,
    TASK_STATE_CHANGED,
)
from tests.conftest import make_pkg_versions


def _task(task_id, state, *, branch="sisyphus", stage="task-build", try_no=1,
          changed="2026-09-30T10:00:00+00:00", tag="157.0-alt1"):
    return {
        "task_id": task_id,
        "task_repo": branch,
        "task_state": state,
        "task_owner": "rauty",
        "task_try": try_no,
        "task_iter": 1,
        "task_testonly": 0,
        "task_changed": changed,
        "task_message": "",
        "task_stage": stage,
        "subtasks": [{"subtask_tag_name": tag, "subtask_dir": "/people/x/packages/firefox.git",
                      "archs": [{"arch": "x86_64", "stage_status": "processed"}]}],
    }


def _setup(conn, client):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus"], backfill=False
    )
    return pkg


def _events(conn, event_type):
    events, _ = journal.query_events(conn, event_types=[event_type], scope="all", limit=100)
    return events


def test_new_running_task_is_journaled(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "BUILDING")]
    refresh.run_refresh(conn, client, cfg)

    events = _events(conn, TASK_DISCOVERED)
    assert len(events) == 1
    assert events[0].detail["task_id"] == 1001
    assert events[0].new_value == "BUILDING"
    row = conn.execute("SELECT * FROM build_tasks WHERE task_id=1001").fetchone()
    assert row["terminal"] == 0
    assert row["state"] == "BUILDING"


def test_state_transition_and_failure(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "BUILDING")]
    refresh.run_refresh(conn, client, cfg)

    client.tasks["firefox"] = [_task(1001, "TESTED", stage="task-repo-elfsym")]
    refresh.run_refresh(conn, client, cfg)
    changes = _events(conn, TASK_STATE_CHANGED)
    assert len(changes) == 1
    assert changes[0].old_value == "BUILDING"
    assert changes[0].new_value == "TESTED"

    client.tasks["firefox"] = [_task(1001, "FAILED", stage="task-build", try_no=2)]
    refresh.run_refresh(conn, client, cfg)

    failures = _events(conn, TASK_FAILED)
    assert len(failures) == 1
    assert failures[0].old_value == "TESTED"
    retries = _events(conn, TASK_RETRIED)
    assert len(retries) == 1 and retries[0].new_value == "2"

    # Terminal task: no further events even if polled again.
    refresh.run_refresh(conn, client, cfg)
    assert len(_events(conn, TASK_FAILED)) == 1


def test_task_done_on_success_transition(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "BUILDING")]
    refresh.run_refresh(conn, client, cfg)
    client.tasks["firefox"] = [_task(1001, "DONE", stage="task-save-repo")]
    refresh.run_refresh(conn, client, cfg)
    assert len(_events(conn, TASK_DONE)) == 1


def test_first_seen_terminal_task_is_silent_for_success(conn, client, cfg):
    """Historical completed tasks must not flood the journal on first sight."""
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "DONE")]
    refresh.run_refresh(conn, client, cfg)
    assert _events(conn, TASK_DONE) == []
    assert _events(conn, TASK_DISCOVERED) == []
    row = conn.execute("SELECT * FROM build_tasks WHERE task_id=1001").fetchone()
    assert row is not None and row["terminal"] == 1


def test_first_seen_recent_failure_is_journaled(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "FAILED", changed="2026-09-30T09:00:00+00:00")]
    refresh.run_refresh(conn, client, cfg)
    assert len(_events(conn, TASK_FAILED)) == 1


def test_stale_tasks_are_ignored(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [
        _task(1001, "FAILED", changed="2001-01-01T00:00:00+00:00")
    ]
    refresh.run_refresh(conn, client, cfg)
    assert _events(conn, TASK_FAILED) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM build_tasks").fetchone()["n"] == 0


def test_tasks_from_untracked_branches_are_ignored(conn, client, cfg):
    _setup(conn, client)  # only sisyphus is tracked
    client.tasks["firefox"] = [_task(1001, "FAILED", branch="p11")]
    refresh.run_refresh(conn, client, cfg)
    assert conn.execute("SELECT COUNT(*) AS n FROM build_tasks").fetchone()["n"] == 0
    assert _events(conn, TASK_FAILED) == []


def test_stage_timeline_recorded_without_journal_noise(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "BUILDING", stage="task-build")]
    refresh.run_refresh(conn, client, cfg)
    client.tasks["firefox"] = [_task(1001, "BUILDING", stage="task-repo-elfsym")]
    refresh.run_refresh(conn, client, cfg)
    client.tasks["firefox"] = [_task(1001, "BUILDING", stage="task-repo-elfsym")]
    refresh.run_refresh(conn, client, cfg)

    stages = conn.execute(
        "SELECT stage FROM task_stages WHERE task_id=1001 ORDER BY id"
    ).fetchall()
    assert [s["stage"] for s in stages] == ["task-build", "task-repo-elfsym"]
    # Stage-only movement must not produce journal events.
    assert _events(conn, TASK_STATE_CHANGED) == []


def test_task_detail_returns_timeline_and_events(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "BUILDING")]
    refresh.run_refresh(conn, client, cfg)
    data = tasks.get_task(conn, 1001, "sisyphus")
    assert data is not None
    assert data["state"] == "BUILDING"
    assert data["stages"]
    assert data["packages"] == ["firefox"]
    assert data["events"]
    assert data["subtasks"][0]["subtask_tag_name"] == "157.0-alt1"


def test_task_counters(conn, client, cfg):
    _setup(conn, client)
    client.tasks["firefox"] = [_task(1001, "BUILDING"), _task(1002, "FAILED")]
    refresh.run_refresh(conn, client, cfg)
    counters = tasks.task_counters(conn)
    assert counters["active"] == 1
    assert counters["failed"] == 1
    assert counters["total"] == 2
