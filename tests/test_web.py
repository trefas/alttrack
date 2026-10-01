"""Web dashboard: the CLI commands must work through HTTP too."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from alttrack import journal, watchlist
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


# ---------------------------------------------------------------------------
# Branch grid & sync guards (rdb-free pages while a sync is running)
# ---------------------------------------------------------------------------
def _create_firefox(client) -> int:
    resp = client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus", "p11"],
              "branches_submitted": "1", "backfill_limit": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].split("?")[0].rstrip("/").split("/")[-1])


def test_api_branches_prefers_cache(web, conn):
    """Во время освежения /api/branches не должен ходить в rdb."""
    client, fake = web
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('active_branches', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        ('["p11", "sisyphus"]',),
    )
    conn.commit()
    resp = client.get("/api/branches")
    assert resp.status_code == 200
    assert resp.json() == ["p11", "sisyphus"]
    assert "active_packagesets" not in fake.calls


def test_api_branches_caches_first_live_fetch(web, conn):
    client, fake = web
    resp = client.get("/api/branches")
    assert resp.status_code == 200
    assert resp.json() == sorted(fake.active)
    fake.calls.clear()
    resp = client.get("/api/branches")
    assert resp.json() == sorted(fake.active)
    assert "active_packagesets" not in fake.calls  # второй запрос — из кэша


def test_edit_page_renders_branch_grid_server_side(web):
    """Чекбоксы веток рисует сервер — им не нужен ни JS, ни живой /api/branches."""
    client, fake = web
    pkg_id = _create_firefox(client)
    page = client.get(f"/packages/{pkg_id}/edit")
    assert page.status_code == 200
    assert 'value="sisyphus" checked' in page.text
    assert 'value="p11" checked' in page.text
    # остальная активная ветка (p10) показана, но не отмечена
    assert 'value="p10"' in page.text
    assert 'value="p10" checked' not in page.text
    # состояния наличия — из отчёта
    assert 'data-meta="sisyphus">есть<' in page.text


def test_edit_page_skips_api_while_syncing(web):
    """Пока идёт освежение, страница правки пакета не делает ни одного запроса к rdb."""
    client, fake = web
    pkg_id = _create_firefox(client)
    client.get("/api/branches")  # прогревает кэш списка веток
    fake.calls.clear()

    client.app.state.refresh_status["running"] = True
    try:
        page = client.get(f"/packages/{pkg_id}/edit")
    finally:
        client.app.state.refresh_status["running"] = False

    assert page.status_code == 200
    assert "по локальным данным" in page.text
    assert 'value="sisyphus" checked' in page.text
    assert fake.calls == [], f"страница обратилась к rdb: {fake.calls}"


def test_packages_actions_disabled_while_syncing(web):
    client, fake = web
    pkg_id = _create_firefox(client)
    client.app.state.jobs["sync:1"] = {
        "running": True, "stage": "x", "error": None, "last": None, "finished_at": None,
    }
    try:
        listing = client.get("/packages")
        assert "Идёт синхронизация" in listing.text
        assert '<span class="btn disabled"' in listing.text          # «+ Добавить пакет»
        assert '<span class="muted" title="Идёт синхронизация' in listing.text  # «изменить»

        detail = client.get(f"/packages/{pkg_id}")
        assert '<span class="btn btn-secondary disabled"' in detail.text  # «Изменить»
        assert "type=\"submit\" disabled" in detail.text                   # импорт / удаление

        form = client.get(f"/packages/{pkg_id}/edit")
        assert "Идёт синхронизация" in form.text
        assert "type=\"submit\" disabled>Сохранить" in form.text
        assert "дождитесь завершения" in form.text
        # сетка веток при этом остаётся серверной и по-прежнему отмечена
        assert 'value="sisyphus" checked' in form.text
    finally:
        client.app.state.jobs.pop("sync:1", None)

    # синхронизация кончилась — всё активно
    listing = client.get("/packages")
    assert "Идёт синхронизация" not in listing.text
    assert '<a class="btn" href="/packages/new"' in listing.text
    assert f'<a href="/packages/{pkg_id}/edit">изменить</a>' in listing.text
    form = client.get(f"/packages/{pkg_id}/edit")
    assert "type=\"submit\" >Сохранить" in form.text


# ---------------------------------------------------------------------------
# Branch reference selects, menu ergonomics, errata/maintainer discoverability
# ---------------------------------------------------------------------------
def test_products_branch_select_uses_reference(web, conn):
    client, fake = web
    from alttrack import refs

    refs.sync(conn, fake)
    page = client.get("/products")
    assert page.status_code == 200
    for b in ("sisyphus", "p11", "p10"):  # активные пакетные наборы FakeClient
        assert f'<option value="{b}"' in page.text
    # старого хардкода больше нет
    assert '<option value="p9"' not in page.text
    assert '<option value="p8"' not in page.text


def test_lists_page_branch_selects_noarch_and_enctype(web, conn):
    client, fake = web
    from alttrack import refs

    refs.sync(conn, fake)
    page = client.get("/lists")
    assert page.status_code == 200
    # иначе браузер отправит имя файла строкой и FastAPI ответит
    # «Expected UploadFile, received: str»
    assert 'enctype="multipart/form-data"' in page.text
    # обе «ветки» — выпадающие списки из справочника, не свободный ввод
    assert page.text.count('<select name="branch"') == 2
    assert '<input type="text" name="branch"' not in page.text
    assert '<option value="sisyphus"' in page.text
    # noarch есть и отмечен по умолчанию — иначе срез теряет такие пакеты
    assert 'value="noarch" checked' in page.text


def test_menu_hides_reporting_pages_but_settings_links_them(web):
    client, _ = web
    nav = client.get("/packages")
    assert ">Архив<" not in nav.text
    assert ">Статистика<" not in nav.text
    assert ">Прогоны<" not in nav.text
    # сами страницы по-прежнему работают
    for path in ("/archive", "/stats", "/runs"):
        assert client.get(path).status_code == 200
    settings = client.get("/settings")
    assert 'href="/archive"' in settings.text
    assert 'href="/stats"' in settings.text
    assert 'href="/runs"' in settings.text


def test_package_detail_shortcuts_to_errata_and_maintainer(web):
    client, _ = web
    pkg_id = _create_firefox(client)
    page = client.get(f"/packages/{pkg_id}")
    assert 'href="/journal?package=firefox&event_type=errata"' in page.text
    assert 'href="/journal?package=firefox&event_type=maintainer_changed"' in page.text


def test_journal_event_filter_shows_human_labels(web):
    client, _ = web
    page = client.get("/journal")
    assert '<option value="errata" >errata / уязвимость</option>' in page.text
    assert '<option value="maintainer_changed" >смена сопровождающего</option>' in page.text


def test_package_detail_errata_block(web, conn):
    client, fake = web
    pkg_id = _create_firefox(client)
    # форма по умолчанию отмечает чекбокс, а тестовый POST его не передавал
    with conn:
        conn.execute("UPDATE tracked_packages SET watch_errata = 1 WHERE id = ?", (pkg_id,))

    # errata ещё не было — нейтральная подсказка
    page = client.get(f"/packages/{pkg_id}")
    assert "Errata и уязвимости" in page.text
    assert "Errata не обнаружено" in page.text

    # освежение обнаружило errata (errata_seen + событие с CVE в детали)
    with conn:
        conn.execute(
            "INSERT INTO errata_seen (errata_id, package_id, branch, first_seen) "
            "VALUES ('E-1', ?, 'sisyphus', '2026-01-05T00:00:00Z')",
            (pkg_id,),
        )
    journal.insert_event(
        conn, package="firefox", package_id=pkg_id, branch="sisyphus",
        event_type="errata", new_value="E-1",
        detail={"errata_id": "E-1", "type": "security", "version": "156.0.1-alt1",
                "refs": ["CVE-2026-1111"]},
    )
    page = client.get(f"/packages/{pkg_id}")
    assert "E-1" in page.text
    assert "CVE-2026-1111" in page.text
    assert "156.0.1-alt1" in page.text

    # отслеживание выключено — показываем состояние вместо списка
    with conn:
        conn.execute("UPDATE tracked_packages SET watch_errata = 0 WHERE id = ?", (pkg_id,))
    page = client.get(f"/packages/{pkg_id}")
    assert "Отслеживание errata для этого пакета отключено" in page.text
