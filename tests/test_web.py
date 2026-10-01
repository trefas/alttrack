"""Web dashboard: the CLI commands must work through HTTP too."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from alttrack import watchlist
from alttrack.web.app import create_app
from tests.conftest import FakeClient, make_pkg_versions


@pytest.fixture
def web(conn, cfg):
    cfg.auto_archive = False
    app = create_app(cfg, initial_refresh=False)
    fake = FakeClient()
    fake.versions["firefox"] = make_pkg_versions(
        ("sisyphus", "156.0.1", "alt1", "h1"), ("p11", "155.0.1", "alt1", "h2")
    )
    app.state.shared_client = fake
    with TestClient(app) as client:
        yield client, fake


def test_dashboard_renders_empty_state(web):
    client, _ = web
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Добавить пакет" in resp.text


def test_create_package_via_web(web, conn):
    client, _ = web
    resp = client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "note": "via web",
              "backfill": "1", "backfill_limit": "3"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pkg = watchlist.get_package(conn, "firefox")
    assert pkg is not None and pkg.branches == ["sisyphus"]
    assert pkg.note == "via web"


def test_create_with_multiple_branches(web, conn):
    """Each selected branch must arrive as its own field, not as 'a,b'."""
    client, _ = web
    resp = client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus", "p11"],
              "branches_submitted": "1", "backfill_limit": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    pkg = watchlist.get_package(conn, "firefox")
    assert set(pkg.branches) == {"sisyphus", "p11"}


def test_create_tolerates_comma_joined_branch_value(web, conn):
    client, _ = web
    resp = client.post(
        "/packages",
        data={"name": "firefox", "branches": "sisyphus,p11",
              "branches_submitted": "1", "backfill_limit": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    pkg = watchlist.get_package(conn, "firefox")
    assert set(pkg.branches) == {"sisyphus", "p11"}


def test_create_rejects_explicitly_empty_branch_selection(web):
    client, _ = web
    resp = client.post(
        "/packages",
        data={"name": "firefox", "branches_submitted": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "ветк" in resp.text


def test_update_with_multiple_branches(web, conn):
    client, _ = web
    client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "backfill_limit": "0"},
        follow_redirects=False,
    )
    resp = client.post(
        "/packages/1",
        data={"branches": ["sisyphus", "p11"], "branches_submitted": "1", "note": "two"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    pkg = watchlist.get_package(conn, "firefox")
    assert set(pkg.branches) == {"sisyphus", "p11"}


def test_create_rejects_unknown_package(web):
    client, _ = web
    resp = client.post("/packages", data={"name": "no-such"}, follow_redirects=False)
    assert resp.status_code == 400
    assert "не найден" in resp.text


def test_edit_package_branches_via_web(web, conn):
    client, _ = web
    client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "backfill_limit": "0"},
        follow_redirects=False,
    )
    resp = client.post(
        "/packages/1",
        data={"branches": ["sisyphus", "p11"], "note": "edited"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pkg = watchlist.get_package(conn, "firefox")
    assert set(pkg.branches) == {"sisyphus", "p11"}
    assert pkg.note == "edited"


def test_delete_package_via_web(web, conn):
    client, _ = web
    client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "backfill_limit": "0"},
        follow_redirects=False,
    )
    resp = client.post("/packages/1/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert watchlist.get_package(conn, "firefox") is None


def test_journal_page_with_filters(web):
    client, _ = web
    client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "backfill_limit": "0"},
        follow_redirects=False,
    )
    for path in ("/journal", "/journal?scope=live", "/journal?scope=archive",
                 "/journal?q=firefox", "/journal?event_type=tracking_started"):
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_stats_tasks_runs_archive_pages(web):
    client, _ = web
    for path in ("/stats", "/tasks", "/runs", "/archive", "/settings",
                 "/packages", "/packages/new"):
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_archive_run_endpoint(web):
    client, _ = web
    resp = client.post("/archive/run", data={"dry_run": "1"})
    assert resp.status_code == 200
    assert "перенесено" in resp.text


def test_settings_save_partial_form(web, cfg):
    client, _ = web
    resp = client.post("/settings", data={"refresh_interval": "15"}, follow_redirects=False)
    assert resp.status_code == 303
    assert cfg.refresh_interval == 15
    assert cfg.auto_archive is False  # untouched by the refresh card


def test_autocomplete_and_check_endpoints(web):
    client, fake = web
    fake.search = [{"name": "firefox", "summary": "browser", "versions": []}]
    assert client.get("/api/autocomplete?q=fire").json()[0]["name"] == "firefox"
    report = client.get(
        "/api/packages/check?name=firefox&branches=sisyphus,p10"
    ).json()
    assert report["ok"] == ["sisyphus"]
    assert report["missing"] == ["p10"]


def test_manual_refresh_endpoint(web, conn):
    client, _ = web
    client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "backfill_limit": "0"},
        follow_redirects=False,
    )
    resp = client.post("/refresh")
    assert resp.status_code in (200, 409)
    if resp.status_code == 200:
        import time

        for _ in range(60):
            status = client.get("/api/refresh/status").json()
            if not status["running"] and status["last"]:
                break
            time.sleep(0.2)
        status = client.get("/api/refresh/status").json()
        assert status["last"]["status"] in ("ok", "partial")


def test_healthz(web):
    client, _ = web
    assert client.get("/healthz").json()["status"] == "ok"
