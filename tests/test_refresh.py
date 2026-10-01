"""Refresh: snapshot diffing, errata/maintainer events, not_found handling."""

from __future__ import annotations

from alttrack import journal, refresh, watchlist
from alttrack.models import (
    ADDED_TO_BRANCH,
    ERRATA,
    MAINTAINER_CHANGED,
    NOT_FOUND,
    REMOVED_FROM_BRANCH,
    VERSION_CHANGED,
)
from tests.conftest import make_pkg_versions


def _add(conn, client, *, branches=("sisyphus",), backfill=False):
    pkg, _ = watchlist.add_package(
        conn, client, name="firefox", branches=list(branches), backfill=backfill
    )
    return pkg


def _events(conn, **kwargs):
    events, total = journal.query_events(conn, scope="all", limit=1000, **kwargs)
    return events, total


def test_first_refresh_is_quiet(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client)
    result = refresh.run_refresh(conn, client, cfg)
    assert result.status == "ok"
    events, _ = _events(conn)
    assert [e.event_type for e in events] == ["tracking_started"]


def test_version_change_is_journaled(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client)

    client.versions["firefox"] = make_pkg_versions(("sisyphus", "157.0", "alt1", "h2"))
    refresh.run_refresh(conn, client, cfg)

    events, _ = _events(conn, event_types=[VERSION_CHANGED])
    assert len(events) == 1
    assert events[0].old_value == "156.0.1-alt1"
    assert events[0].new_value == "157.0-alt1"
    assert events[0].branch == "sisyphus"

    # A second run without changes must be silent.
    refresh.run_refresh(conn, client, cfg)
    events, _ = _events(conn, event_types=[VERSION_CHANGED])
    assert len(events) == 1


def test_removed_and_added_back(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client)

    client.versions["firefox"] = []
    refresh.run_refresh(conn, client, cfg)
    events, _ = _events(conn, event_types=[REMOVED_FROM_BRANCH])
    assert len(events) == 1
    snaps = watchlist.snapshot_map(conn, 1)
    assert snaps["sisyphus"]["present"] == 0

    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.2", "alt1", "h9"))
    refresh.run_refresh(conn, client, cfg)
    added, _ = _events(conn, event_types=[ADDED_TO_BRANCH])
    assert len(added) == 1
    assert added[0].new_value == "156.0.2-alt1"


def test_not_found_emitted_once(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client)
    client.versions.clear()  # package disappears everywhere

    refresh.run_refresh(conn, client, cfg)
    refresh.run_refresh(conn, client, cfg)
    events, _ = _events(conn, event_types=[NOT_FOUND])
    assert len(events) == 1, "not_found должен дедуплицироваться"


def test_missing_branch_stays_silent(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client, branches=("sisyphus", "p10"))  # p10 has no package
    refresh.run_refresh(conn, client, cfg)
    events, _ = _events(conn)
    assert [e.event_type for e in events] == ["tracking_started"]
    snaps = watchlist.snapshot_map(conn, 1)
    assert snaps["p10"]["present"] == 0


def test_errata_history_is_silent_new_one_is_journaled(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    client.erratas["firefox"] = [
        {"id": "ALT-PU-2020-1", "pkg_name": "firefox", "pkgset_name": "sisyphus",
         "type": "task", "created": "2020-01-01T00:00:00+00:00",
         "references": [{"id": "CVE-2020-1", "type": "vuln"}]},
        {"id": "ALT-PU-9999-1", "pkg_name": "firefox", "pkgset_name": "sisyphus",
         "type": "task", "created": "2999-01-01T00:00:00+00:00",
         "references": [{"id": "CVE-9999-1", "type": "vuln"}]},
        {"id": "ALT-PU-9999-2", "pkg_name": "otherpkg", "pkgset_name": "sisyphus",
         "type": "task", "created": "2999-01-01T00:00:00+00:00", "references": []},
    ]
    _add(conn, client)
    refresh.run_refresh(conn, client, cfg)

    events, _ = _events(conn, event_types=[ERRATA])
    assert len(events) == 1
    assert events[0].new_value == "ALT-PU-9999-1"
    assert "CVE-9999-1" in events[0].detail["refs"]

    # Dedup: the next run adds nothing.
    refresh.run_refresh(conn, client, cfg)
    events, _ = _events(conn, event_types=[ERRATA])
    assert len(events) == 1


def test_acl_change_is_reported_as_maintainer_change(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    client.acl["sisyphus"] = [{"name": "firefox", "members": ["legion", "rauty"]}]
    _add(conn, client)

    refresh.run_refresh(conn, client, cfg)  # seeds the ACL snapshot silently
    events, _ = _events(conn, event_types=[MAINTAINER_CHANGED])
    assert events == []

    client.acl["sisyphus"] = [{"name": "firefox", "members": ["rauty", "sbolshakov"]}]
    refresh.run_refresh(conn, client, cfg)
    events, _ = _events(conn, event_types=[MAINTAINER_CHANGED])
    assert len(events) == 1
    assert events[0].old_value == "legion, rauty"
    assert events[0].new_value == "rauty, sbolshakov"
    assert events[0].detail["kind"] == "acl"


def test_refresh_marks_run_and_checked_at(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client)
    result = refresh.run_refresh(conn, client, cfg)
    assert result.checked == 1
    run = refresh.last_run(conn)
    assert run["status"] == "ok"
    assert run["finished_at"]
    pkg = watchlist.get_package(conn, "firefox")
    assert pkg.last_checked_at


def test_partial_status_on_client_error(conn, client, cfg):
    client.versions["firefox"] = make_pkg_versions(("sisyphus", "156.0.1", "alt1", "h1"))
    _add(conn, client)

    def boom(name):
        from alttrack.api import ApiError

        raise ApiError("boom")

    client.errata_search = boom  # type: ignore[method-assign]
    result = refresh.run_refresh(conn, client, cfg)
    assert result.status == "partial"
    assert any("boom" in e for e in result.errors)
