"""Web: products, image catalog, lists, comparison, background jobs."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from alttrack import lists as lists_mod
from alttrack import products
from alttrack import refs as refs_mod
from alttrack.web.app import create_app
from tests.conftest import FakeClient, make_pkg_versions

UUID = "web-img-1"


@pytest.fixture
def web(conn, cfg):
    cfg.auto_archive = False
    app = create_app(cfg, initial_refresh=False)
    fake = FakeClient()
    fake.versions["firefox"] = make_pkg_versions(
        ("sisyphus", "156.0.1", "alt1", "h1"), ("p11", "155.0.1", "alt1", "h2")
    )
    fake.versions["zsh"] = make_pkg_versions(
        ("sisyphus", "5.9", "alt1", "z1"), ("p11", "5.9", "alt1", "z2")
    )
    fake.images = [{
        "uuid": UUID, "branch": "p11", "edition": "alt-edu", "arch": "x86_64",
        "variant": "", "type": "iso", "release": "release", "tag": "p11:alt-edu:::release.11.2:x86_64:install:iso",
        "file": "alt-edu-11.2-x86_64.iso", "date": "2025-06-01",
    }]
    fake.image_pkgs[UUID] = [
        {"name": "bash-libs", "version": "5.2", "release": "alt1", "arch": "x86_64", "summary": "GNU shell"},
        {"name": "zsh-libs", "version": "5.9", "release": "alt1", "arch": "x86_64", "summary": "zsh"},
    ]
    fake.source_map = {
        "bash-libs": {"name": "bash-libs", "sourcepkgname": "bash", "status": "found",
                      "version": "5.2.15", "release": "alt1"},
        "zsh-libs": {"name": "zsh-libs", "sourcepkgname": "zsh", "status": "found",
                     "version": "5.9", "release": "alt1"},
    }
    fake.repo[("p11", "source")] = [
        {"name": "bash", "version": "5.2.15", "release": "alt1", "hash": "hb",
         "summary": "shell", "category": "Shells", "maintainer": "a@alt"},
        {"name": "zsh", "version": "5.9", "release": "alt1", "hash": "hz",
         "summary": "zsh", "category": "Shells", "maintainer": "z@alt"},
    ]
    fake.binary_export["p11"] = [
        {"name": "bash-libs", "version": "5.2", "release": "alt1", "arch": "x86_64", "source": "bash"},
        {"name": "coreutils", "version": "9.4", "release": "alt2", "arch": "x86_64", "source": "coreutils"},
    ]
    app.state.shared_client = fake
    with TestClient(app) as client:
        yield client, fake


def _create_product(client, **extra) -> None:
    resp = client.post(
        "/products",
        data={"title": "Образование 11", "branch": "p11", "edition": "alt-edu",
              "arch": "x86_64", **extra},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _wait_job(client, name_prefix: str, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()
        matching = {k: v for k, v in jobs.items() if k.startswith(name_prefix)}
        if matching and not any(v["running"] for v in matching.values()):
            return matching
        time.sleep(0.1)
    raise AssertionError(f"job {name_prefix} did not finish: {client.get('/api/jobs').json()}")


# ---------------------------------------------------------------------------
# products CRUD
# ---------------------------------------------------------------------------
def test_products_page_and_create(web, conn):
    client, _ = web
    resp = client.get("/products")
    assert resp.status_code == 200 and "Продуктов нет" in resp.text

    _create_product(client)
    assert products.get_product(conn, "Образование 11") is not None
    resp = client.get("/products")
    assert "Образование 11" in resp.text


def test_product_detail_renders(web):
    client, _ = web
    _create_product(client)
    resp = client.get("/products/1")
    assert resp.status_code == 200
    for fragment in ("Состав по образу", "Обновить состав", "Справочник", "alt-edu-11.2"):
        assert fragment in resp.text


def test_product_selector_sets_cookie(web):
    client, _ = web
    _create_product(client)
    resp = client.post(
        "/products/select", data={"product_id": "1", "referer": "/journal"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert client.cookies.get("product_id") == "1"
    page = client.get("/journal")
    assert page.status_code == 200


def test_delete_product(web, conn):
    client, _ = web
    _create_product(client)
    resp = client.post("/products/1/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert products.list_products(conn) == []


# ---------------------------------------------------------------------------
# background track job
# ---------------------------------------------------------------------------
def test_track_job_adds_composition(web, conn):
    client, _ = web
    _create_product(client)
    resp = client.post(
        "/products/1/track", data={"uuid": UUID}, follow_redirects=False
    )
    assert resp.status_code == 303
    jobs = _wait_job(client, "track:")
    job = next(iter(jobs.values()))
    assert job["error"] is None, job["error"]
    assert job["summary"]["added"] == 2

    members = products.product_members(conn, products.require_product(conn, 1))
    assert sorted(m["name"] for m in members) == ["bash", "zsh"]

    page = client.get("/products/1")
    assert "завершено" in page.text and "добавлено" in page.text


def test_track_job_reports_not_found(web, conn):
    client, fake = web
    _create_product(client)
    fake.source_map = {}  # nothing resolves
    client.post("/products/1/track", data={"uuid": UUID}, follow_redirects=False)
    jobs = _wait_job(client, "track:")
    job = next(iter(jobs.values()))
    assert job["summary"]["added"] == 0
    assert job["summary"]["not_found"] == 2


def test_update_job_with_latest_image(web, conn):
    client, _ = web
    _create_product(client)
    client.post("/products/1/track", data={"uuid": UUID}, follow_redirects=False)
    _wait_job(client, "track:")
    resp = client.post(
        "/products/1/update", data={"uuid": ""}, follow_redirects=False
    )
    assert resp.status_code == 303
    jobs = _wait_job(client, "update:")
    job = next(iter(jobs.values()))
    assert job["error"] is None, job["error"]
    assert job["summary"]["kept"] == 2  # same composition, nothing added/paused


def test_busy_job_is_rejected(web):
    client, _ = web
    _create_product(client)

    app = client.app
    app.state.jobs["track:1"] = {
        "running": True, "stage": "x", "error": None, "last": None, "finished_at": None,
    }
    resp = client.post(
        "/products/1/track", data={"uuid": UUID}, follow_redirects=False
    )
    assert resp.status_code == 303 and "busy=1" in resp.headers["location"]
    app.state.jobs["track:1"]["running"] = False


# ---------------------------------------------------------------------------
# image catalog page
# ---------------------------------------------------------------------------
def test_images_page(web, conn):
    client, _ = web
    resp = client.get("/images")
    assert resp.status_code == 200
    assert "alt-edu-11.2" in resp.text
    # filters
    resp = client.get("/images?branch=p11&arch=x86_64")
    assert resp.status_code == 200 and "alt-edu-11.2" in resp.text
    resp = client.get("/images?branch=p10")
    assert "не найдены" in resp.text


def test_images_refresh_endpoint(web):
    client, fake = web
    resp = client.post("/images/refresh", follow_redirects=False)
    assert resp.status_code == 303
    assert "image_info" in fake.calls


# ---------------------------------------------------------------------------
# lists upload
# ---------------------------------------------------------------------------
def test_upload_list_via_file(web, conn):
    client, _ = web
    resp = client.post(
        "/lists/file",
        files={"file": ("pkgs.txt", b"bash-5.2.15-alt9.x86_64\nbroken-line\n")},
        data={"branch": "p11"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    created = resp.headers["location"].split("=")[1]
    list_obj = lists_mod.require_list(conn, created)
    assert list_obj.item_count == 1 and list_obj.bad_lines == 1
    assert client.get("/lists").status_code == 200


def test_upload_list_rejects_empty(web):
    client, _ = web
    resp = client.post("/lists/file", data={"text": "   "}, follow_redirects=False)
    assert resp.status_code == 303 and "error" in resp.headers["location"]


def test_list_from_branch_and_image(web, conn):
    client, _ = web
    resp = client.post(
        "/lists/branch", data={"branch": "p11"}, follow_redirects=False
    )
    assert resp.status_code == 303
    resp = client.post(
        "/lists/image", data={"uuid": UUID, "title": "Образ"}, follow_redirects=False
    )
    assert resp.status_code == 303
    rows = lists_mod.list_lists(conn)
    assert {r.kind for r in rows} == {"branch", "image"}


def test_delete_list(web, conn):
    client, _ = web
    client.post("/lists/file", data={"text": "foo-1.0-alt1.x86_64"}, follow_redirects=False)
    list_obj = lists_mod.list_lists(conn)[0]
    resp = client.post(f"/lists/{list_obj.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert lists_mod.list_lists(conn) == []


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
def _two_lists(client) -> tuple[int, int]:
    client.post(
        "/lists/file",
        files={"file": ("a.txt", b"bash-5.2.15-alt9.x86_64\nzsh-5.9-alt1.x86_64\n")},
        follow_redirects=False,
    )
    client.post(
        "/lists/file",
        files={"file": ("b.txt", b"bash-5.2.16-alt1.x86_64\nsed-4.9-alt1.x86_64\n")},
        follow_redirects=False,
    )
    return 1, 2


def test_compare_run_and_report(web):
    client, _ = web
    left, right = _two_lists(client)
    resp = client.post(
        "/compare/run", data={"left": left, "right": right, "title": "A ↔ B"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/compare/1")
    assert page.status_code == 200
    for fragment in ("A ↔ B", "только слева", "только справа", "версии отличаются"):
        assert fragment in page.text

    filtered = client.get("/compare/1?status=changed")
    assert "sed" not in filtered.text  # right_only filtered out
    assert "bash" in filtered.text

    csv_resp = client.get("/compare/1/export")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "bash" in csv_resp.text


def test_compare_report_pagination(web, conn):
    client, fake = web
    left, _ = lists_mod.save_file_list(
        conn, text="\n".join(f"p{i}-1.0-alt1.x86_64" for i in range(25)),
        title="L", source_branch="p11", client=fake,
    )
    right, _ = lists_mod.save_file_list(
        conn, text="p0-2.0-alt1.x86_64\n", title="R",
        source_branch="p11", client=fake,
    )
    resp = client.post(
        "/compare/run", data={"left": left.id, "right": right.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    p1 = client.get("/compare/1?size=10")
    assert p1.status_code == 200
    assert "строки 1–10 из 25" in p1.text
    assert "страница 1 из 3" in p1.text
    assert 'value="10" selected' in p1.text
    # pager links preserve the chosen size
    assert "?size=10&amp;page=2" in p1.text

    p2 = client.get("/compare/1?size=10&page=2")
    assert "строки 11–20 из 25" in p2.text
    p3 = client.get("/compare/1?size=10&page=3")
    assert "строки 21–25 из 25" in p3.text

    # out-of-range page clamps to the last one; big page covers everything
    assert "строки 21–25 из 25" in client.get("/compare/1?size=10&page=99").text
    assert "строки 1–25 из 25" in client.get("/compare/1?size=100").text
    # unknown page sizes fall back to the default (50)
    assert 'value="50" selected' in client.get("/compare/1?size=25").text

    # the status filter keeps the page size
    filtered = client.get("/compare/1?size=10&status=left_only")
    assert "строки 1–10 из 24" in filtered.text
    assert 'value="10" selected' in filtered.text


def test_compare_page_history_and_delete(web):
    client, _ = web
    left, right = _two_lists(client)
    client.post("/compare/run", data={"left": left, "right": right}, follow_redirects=False)
    page = client.get("/compare")
    assert page.status_code == 200 and "сравнений" in page.text.lower()
    resp = client.post("/compare/1/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/compare/1").status_code == 404


def test_preset_release_vs_branch(web, conn):
    client, _ = web
    _create_product(client)
    resp = client.post(
        "/compare/preset", data={"preset": "release-branch"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/compare/")
    url = resp.headers["location"]
    page = client.get(url)
    assert page.status_code == 200
    # right side is the repository branch: packages missing from the image
    # (coreutils) are hidden — they say nothing about the product
    assert "coreutils" not in page.text
    assert "Скрыто" in page.text and "пакетов репозитория" in page.text
    assert "-- только справа" not in page.text
    # shared packages with different versions are still reported
    assert "bash" in page.text
    # the row popup is hidden until a row is clicked
    assert '<div id="modal" class="modal-backdrop" hidden>' in page.text
    # …and the hidden rows are available on demand
    page = client.get(url + "?extra=1")
    assert "coreutils" in page.text
    assert "-- только справа" in page.text
    # CSV export honours the same filter
    csv_default = client.get(url + "/export").text
    assert "coreutils" not in csv_default
    csv_extra = client.get(url + "/export?extra=1").text
    assert "coreutils" in csv_extra
    # history chips: no "--" pill for repository-side comparisons
    index = client.get("/compare")
    assert '<span class="pill pill-bad">' not in index.text


def test_modal_css_hides_popup_by_default(web):
    client, _ = web
    css = client.get("/static/style.css")
    assert css.status_code == 200
    # without this rule the author `display:flex` beats the UA [hidden] style
    # and the popup covers the report from the first render
    assert ".modal-backdrop[hidden]" in css.text


def test_compare_selects_disabled_while_syncing(web):
    client, _ = web
    _create_product(client)
    client.post("/lists/branch", data={"branch": "p11"}, follow_redirects=False)
    client.post("/lists/branch", data={"branch": "p11"}, follow_redirects=False)
    assert client.get("/compare").status_code == 200

    app = client.app
    app.state.jobs["sync:1"] = {
        "running": True, "stage": "x", "error": None, "last": None, "finished_at": None,
    }
    try:
        page = client.get("/compare")
        assert "Идёт синхронизация" in page.text
        assert '<select name="left" required disabled>' in page.text
        assert '<select name="right" required disabled>' in page.text
        assert 'type="submit" disabled>Сравнить' in page.text
        # пресеты ходят в rdb — на время синхронизации тоже выключены
        assert 'type="submit" disabled>Образ ↔ репозиторий' in page.text
        assert 'type="submit" disabled>Последний образ ↔ загруженный список' in page.text
    finally:
        app.state.jobs.pop("sync:1", None)

    # sync over: the form is usable again
    page = client.get("/compare")
    assert "Идёт синхронизация" not in page.text
    assert '<select name="left" required >' in page.text
    assert 'disabled>Образ ↔ репозиторий' not in page.text
    assert '<select name="left" required disabled>' not in page.text


def test_preset_requires_product(web):
    client, _ = web  # no products created
    resp = client.post(
        "/compare/preset", data={"preset": "release-branch"}, follow_redirects=False
    )
    assert "error" in resp.headers["location"]


# ---------------------------------------------------------------------------
# reference lists in settings + entry point from Packages
# ---------------------------------------------------------------------------
def test_settings_shows_and_syncs_reference_lists(web, conn):
    client, _ = web
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Справочники rdb" in page.text
    # first visit fills them automatically (first database fill)
    kinds = {r["kind"] for r in refs_mod.status(conn)}
    assert kinds == {"arch", "category", "branch"}
    assert "Ветки" in page.text
    assert "x86_64" in page.text and "System/Base" in page.text

    resp = client.post("/settings/refs/sync", follow_redirects=False)
    assert resp.status_code == 303 and "refs=1" in resp.headers["location"]
    assert client.get("/settings").status_code == 200


def test_packages_page_links_to_image_tracking(web):
    client, _ = web
    page = client.get("/packages")
    assert page.status_code == 200
    assert "+ Добавить из образа" in page.text
    assert 'href="/images"' in page.text


def test_images_page_hints_when_no_product(web):
    client, _ = web
    page = client.get("/images")
    assert "через <strong>продукт</strong>" in page.text


def test_products_form_uses_reference_arch_list(web):
    client, _ = web
    page = client.get("/products")
    assert '<select name="arch"' in page.text
    # options come from the reference list (fallback = built-in defaults)
    assert "aarch64" in page.text and "x86_64" in page.text
    # pseudo-architectures never appear as a product architecture
    assert ">srpm<" not in page.text and ">src<" not in page.text


def test_images_arch_filter_uses_reference_list(web):
    client, _ = web
    page = client.get("/images")
    assert page.status_code == 200
    # the arch dropdown is the rdb reference list, not the 5 catalog values
    assert ">riscv64</option>" in page.text and ">armv6</option>" in page.text
    assert ">все</option>" in page.text  # «все» keeps working


# ---------------------------------------------------------------------------
# image lists from the Lists section
# ---------------------------------------------------------------------------
def test_lists_page_offers_image_selector(web, conn):
    client, _ = web
    page = client.get("/lists")
    assert page.status_code == 200
    # catalog is loaded on first visit (same as /images)
    assert conn.execute("SELECT COUNT(*) FROM image_catalog").fetchone()[0] >= 1
    assert 'action="/lists/image"' in page.text
    assert '<optgroup label="Каталог образов">' in page.text
    assert f'value="{UUID}"' in page.text
    assert "Сохранить список образа" in page.text


def test_lists_page_groups_bound_product_images(web, conn):
    client, _ = web
    _create_product(client)
    client.post(
        "/products/1/track", data={"uuid": UUID}, follow_redirects=False
    )
    _wait_job(client, "track:")

    page = client.get("/lists")
    assert '<optgroup label="Продукт: Образование 11">' in page.text
    # the bound image lives in the product group only — no duplicate option
    assert page.text.count(f'value="{UUID}"') == 1


def test_lists_image_flow_makes_comparable_lists(web, conn):
    client, _ = web
    # two saves of the same image stand in for two releases (11.0 / 11.1)
    resp = client.post(
        "/lists/image",
        data={"uuid": UUID, "title": "alt-server 11.0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "created=1" in resp.headers["location"]
    resp = client.post(
        "/lists/image",
        data={"uuid": UUID, "title": "alt-server 11.1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = lists_mod.list_lists(conn)
    assert [r.title for r in rows] == ["alt-server 11.0", "alt-server 11.1"]
    assert all(r.kind == "image" for r in rows)
    # both lists are selectable on the compare page
    page = client.get("/compare")
    for r in rows:
        assert r.title in page.text


def test_product_bound_images_have_list_button(web, conn):
    client, _ = web
    _create_product(client)
    client.post(
        "/products/1/track", data={"uuid": UUID}, follow_redirects=False
    )
    _wait_job(client, "track:")
    page = client.get("/products/1")
    assert "Привязанные образы" in page.text
    assert 'action="/lists/image"' in page.text
    assert f'value="{UUID}"' in page.text


def test_lists_form_shows_arch_checkboxes(web):
    client, _ = web
    page = client.get("/lists")
    assert page.status_code == 200
    # several checkboxes instead of a free-text field
    assert page.text.count('name="arch"') >= 5
    assert '<input type="checkbox" name="arch" value="x86_64"' in page.text
    assert '<input type="checkbox" name="arch" value="aarch64"' in page.text
    assert "Ничего не отмечено" in page.text


def test_list_from_branch_with_several_arches(web, conn):
    client, _ = web
    resp = client.post(
        "/lists/branch",
        data={"branch": "p11", "arch": ["x86_64", "aarch64"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    branch_list = next(r for r in lists_mod.list_lists(conn) if r.kind == "branch")
    assert branch_list.params["arch"] == ["x86_64", "aarch64"]


# ---------------------------------------------------------------------------
# product filter + backfill
# ---------------------------------------------------------------------------
def test_journal_filtered_by_selected_product(web, conn):
    client, _ = web
    for name in ("firefox", "zsh"):
        client.post(
            "/packages",
            data={"name": name, "branches": ["sisyphus"], "backfill_limit": "0"},
            follow_redirects=False,
        )
    # product containing only firefox
    _create_product(client)
    pid = conn.execute("SELECT id FROM tracked_packages WHERE name='firefox'").fetchone()["id"]
    with conn:
        conn.execute(
            "INSERT INTO product_packages (product_id, package_id, added_by, paused_by_image, added_at) "
            "VALUES (1, ?, 'manual', 0, '2025-01-01')",
            (pid,),
        )
    client.post("/products/select", data={"product_id": "1"}, follow_redirects=False)

    page = client.get("/journal").text
    assert ">firefox</a>" in page       # event row of the product's package
    assert ">zsh</a>" not in page       # foreign package's event filtered out

    # selector back to "all products" restores everything
    client.post("/products/select", data={"product_id": "0"}, follow_redirects=False)
    page = client.get("/journal").text
    assert ">zsh</a>" in page


def test_backfill_job(web):
    client, _ = web
    client.post(
        "/packages",
        data={"name": "firefox", "branches": ["sisyphus"], "backfill_limit": "0"},
        follow_redirects=False,
    )
    resp = client.post("/packages/1/backfill", data={}, follow_redirects=False)
    assert resp.status_code == 303
    jobs = _wait_job(client, "backfill:")
    job = next(iter(jobs.values()))
    assert job["error"] is None
    assert job["summary"]["imported"] == 0  # fake history is empty
    page = client.get("/packages/1")
    assert "История сборок импортирована" in page.text


def test_meta_sync_job(web, conn):
    client, _ = web
    _create_product(client)
    resp = client.post("/products/1/meta-sync", data={}, follow_redirects=False)
    assert resp.status_code == 303
    jobs = _wait_job(client, "meta:")
    job = next(iter(jobs.values()))
    assert job["error"] is None
    assert job["summary"]["count"] == 2
