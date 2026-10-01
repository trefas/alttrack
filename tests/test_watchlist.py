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


# ---------------------------------------------------------------------------
# Local branch report (no API calls — used while a sync is running)
# ---------------------------------------------------------------------------
def _cache_active(conn, branches: list[str]) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('active_branches', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (json.dumps(branches),),
    )
    conn.commit()


def test_local_branch_report_uses_cache_not_api(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox",
        branches=["sisyphus", "p11", "p10"], backfill=False,
    )
    # p10 входит в активные ветки, но baseline запомнил её отсутствие (present=0)
    _cache_active(conn, ["sisyphus", "p11", "p10"])
    client.calls.clear()

    report = watchlist.local_branch_report(conn, pkg)

    assert report is not None
    assert report["ok"] == ["sisyphus", "p11"]
    assert report["missing"] == ["p10"]
    assert report["inactive"] == []
    assert any("сейчас нет" in w for w in report["warnings"])
    assert client.calls == [], f"ожидалось отсутствие обращений к rdb: {client.calls}"


def test_local_branch_report_marks_inactive_branch(conn, client):
    _seed(client)
    client.versions["firefox"] = list(client.versions["firefox"]) + [
        {"branch": "p07", "version": "154.0", "release": "alt1", "pkghash": "h3"}
    ]
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=["sisyphus", "p07"], backfill=False
    )
    _cache_active(conn, ["sisyphus", "p11", "p10"])  # p07 больше не публикуется

    report = watchlist.local_branch_report(conn, pkg)

    assert report is not None
    assert report["inactive"] == ["p07"]
    assert any("не входит" in w for w in report["warnings"])


def test_local_branch_report_needs_cache(conn, client):
    """Без завершённого освежения кэша нет — отчёт должен просить живую проверку."""
    _seed(client)
    pkg, _ = watchlist.add_package(conn, client, name="firefox", backfill=False)
    assert watchlist.local_branch_report(conn, pkg) is None


# ---------------------------------------------------------------------------
# Current errata view for the package page (errata_seen + event detail)
# ---------------------------------------------------------------------------
def test_erratas_for_package_lists_seen_with_detail(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(conn, client, name="firefox", backfill=False)
    with conn:
        conn.executemany(
            "INSERT INTO errata_seen (errata_id, package_id, branch, first_seen) "
            "VALUES (?, ?, ?, ?)",
            [("E-1", pkg.id, "sisyphus", "2026-01-01T00:00:00Z"),
             ("E-2", pkg.id, "p11", "2026-01-02T00:00:00Z")],
        )
    journal.insert_event(
        conn, package="firefox", package_id=pkg.id, branch="sisyphus",
        event_type="errata", new_value="E-1",
        detail={"errata_id": "E-1", "type": "bugfix", "version": "156.0.1-alt1",
                "refs": ["CVE-2026-1111", "CVE-2026-2222"]},
    )

    rows = journal.erratas_for_package(conn, pkg.id)
    # новые первыми (first_seen DESC)
    assert [r["errata_id"] for r in rows] == ["E-2", "E-1"]
    assert rows[1]["refs"] == ["CVE-2026-1111", "CVE-2026-2222"]
    assert rows[1]["version"] == "156.0.1-alt1" and rows[1]["type"] == "bugfix"
    # события ещё нет — сама errata всё равно видна (id/ветка/дата)
    assert rows[0]["refs"] == []

    assert journal.erratas_for_package(conn, -1) == []


def test_erratas_fall_back_to_archived_event(conn, client):
    _seed(client)
    pkg, _ = watchlist.add_package(conn, client, name="firefox", backfill=False)
    with conn:
        conn.execute(
            "INSERT INTO errata_seen (errata_id, package_id, branch, first_seen) "
            "VALUES ('E-9', ?, 'sisyphus', '2026-01-03T00:00:00Z')",
            (pkg.id,),
        )
        conn.execute(
            "INSERT INTO journal_archive "
            "(seq, ts, package_id, package, branch, event_type, new_value, detail, archived_at) "
            "VALUES (1, '2026-01-03T00:00:00Z', ?, 'firefox', 'sisyphus', 'errata', 'E-9', ?, "
            "'2026-02-01T00:00:00Z')",
            (pkg.id, json.dumps({"type": "security", "refs": ["CVE-2026-9999"]})),
        )
    rows = journal.erratas_for_package(conn, pkg.id)
    assert rows[0]["refs"] == ["CVE-2026-9999"]
    assert rows[0]["type"] == "security"
