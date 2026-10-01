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
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    # coreutils only in repo (right), bash/zsh shared with different versions
    assert "coreutils" in page.text


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
    assert kinds == {"arch", "category"}
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
