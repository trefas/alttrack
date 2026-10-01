"""CRUD of the tracked package list, including branch presence checks."""

from __future__ import annotations

import json

import pytest

from alttrack import journal, watchlist
from alttrack.models import TRACKING_STARTED
from tests.conftest import FakeClient, make_pkg_versions


def _seed(client: FakeClient) -> None:
    client.versions["firefox"] = make_pkg_versions(
        ("sisyphus", "156.0.1", "alt1", "h1"),
        ("p11", "155.0.1", "alt1", "h2"),
    )
    client.task_history["firefox"] = [
        {"id": 111, "branch": "sisyphus", "state": "DONE", "owner": "rauty",
         "changed": "2026-09-01T00:00:00+00:00",
         "packages": [{"name": "firefox", "version": "156.0", "release": "alt1"}]}
    ]


def test_add_requires_known_package(conn, client):
    with pytest.raises(watchlist.ValidationError):
        watchlist.add_package(conn, client, name="no-such-package", branches=["sisyphus"])


def test_add_rejects_unknown_branch(conn, client):
    _seed(client)
    with pytest.raises(watchlist.ValidationError, match="неизвестные ветки"):
        watchlist.add_package(conn, client, name="firefox", branches=["nosuch"])


def test_add_warns_but_allows_missing_branch(conn, client):
    _seed(client)
    pkg, report = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus", "p10"]
    )
    assert report["missing"] == ["p10"]
    assert report["warnings"], "предупреждение об отсутствии ветки обязательно"
    snaps = watchlist.snapshot_map(conn, pkg.id)
    assert snaps["sisyphus"]["present"] == 1
    assert snaps["p10"]["present"] == 0


def test_add_writes_tracking_started_and_baseline(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus"], backfill=False
    )
    events, _ = journal.query_events(conn, package="firefox", scope="live")
    assert [e.event_type for e in events] == [TRACKING_STARTED]
    snap = watchlist.snapshot_map(conn, pkg.id)["sisyphus"]
    assert (snap["version"], snap["release"], snap["pkghash"]) == ("156.0.1", "alt1", "h1")


def test_add_default_branches_are_active_and_present(conn, client):
    _seed(client)
    client.versions["firefox"].append({"branch": "p10", "version": "1", "release": "alt1", "pkghash": "h3"})
    pkg, _ = watchlist.add_package(conn, client, name="firefox", backfill=False)
    assert sorted(pkg.branches) == ["p11", "p10", "sisyphus"] or set(pkg.branches) == {
        "sisyphus",
        "p11",
        "p10",
    }


def test_edit_keeps_snapshot_of_removed_branch(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus", "p11"], backfill=False
    )
    updated, report = watchlist.update_package(
        conn, client, pkg, branches=["sisyphus"]
    )
    assert updated.branches == ["sisyphus"]
    assert report["removed"] == ["p11"]
    # Snapshot must survive: returning the branch later diffs against it.
    assert "p11" in watchlist.snapshot_map(conn, pkg.id)

    # Adding it back must not create a new snapshot (the old one is reused).
    again, _ = watchlist.update_package(conn, client, updated, branches=["sisyphus", "p11"])
    assert again.branches == ["sisyphus", "p11"]
    snaps = watchlist.snapshot_map(conn, pkg.id)
    assert snaps["p11"]["version"] == "155.0.1"


def test_edit_rejects_empty_branch_set(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus"], backfill=False
    )
    with pytest.raises(watchlist.ValidationError, match="пустой список"):
        watchlist.update_package(conn, client, pkg, branches=[])


def test_delete_keeps_journal_unless_purged(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus"], backfill=False
    )
    watchlist.delete_package(conn, pkg)
    assert watchlist.get_package(conn, "firefox") is None
    events, _ = journal.query_events(conn, scope="all")
    assert events, "журнал должен сохраниться при обычном удалении"

    pkg2, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus"], backfill=False
    )
    watchlist.delete_package(conn, pkg2, purge=True)
    events, _ = journal.query_events(conn, scope="all")
    assert events == []


def test_branch_report_lists_present_branches(conn, client):
    _seed(client)
    report = watchlist.check_branches(client, "firefox", ["sisyphus", "p11"])
    assert report["ok"] == ["sisyphus", "p11"]
    assert not report["missing"] and not report["inactive"]


def test_snapshots_stored_as_json_branches(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus"], backfill=False
    )
    row = conn.execute("SELECT branches FROM tracked_packages WHERE id=?", (pkg.id,)).fetchone()
    assert json.loads(row["branches"]) == ["sisyphus"]
