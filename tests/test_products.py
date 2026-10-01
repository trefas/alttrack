"""Tests for products: CRUD, bulk tracking from an image, diff-update."""

from __future__ import annotations

import json
import sqlite3

from alttrack import metasync, products
from alttrack.models import TRACKING_STARTED


UUID = "img-uuid-1"


def _setup(conn, client, *, names=("alpha", "beta", "gamma")):
    """Catalog image + reference data + image binaries with srpm mapping."""
    client.images = [{
        "uuid": UUID, "branch": "p11", "edition": "education", "arch": "x86_64",
        "variant": "", "type": "salt", "release": "release", "tag": "11.0",
        "file": "alt-11.iso", "date": "2025-06-01",
    }]
    client.image_pkgs[UUID] = [
        {"name": f"{n}-libs", "version": "1.0", "release": "alt1", "arch": "x86_64"}
        for n in names
    ]
    client.source_map = {
        f"{n}-libs": {"name": f"{n}-libs", "sourcepkgname": n, "status": "found",
                      "version": "1.0", "release": "alt1"}
        for n in names
    }
    client.repo[("p11", "source")] = [
        {"name": n, "version": "1.0", "release": "alt1", "hash": f"h-{n}",
         "summary": f"{n} summary", "category": "System", "maintainer": "dev@alt"}
        for n in names
    ]
    product = products.create_product(
        conn, title="Образование 11", branch="p11", edition="education", arch="x86_64"
    )
    from alttrack import images
    images.refresh_catalog(conn, client)
    return product


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def test_crud(conn):
    p = products.create_product(conn, title="АРМ СБ", branch="p11", edition="server")
    assert p.id is not None
    assert products.get_product(conn, "АРМ СБ").id == p.id
    assert products.get_product(conn, p.id).title == "АРМ СБ"
    try:
        products.require_product(conn, "nope")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    products.delete_product(conn, p)
    assert products.list_products(conn) == []


def test_create_rejects_empty_title(conn):
    try:
        products.create_product(conn, title="  ", branch="p11")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# First-time tracking
# ---------------------------------------------------------------------------
def test_track_from_image(conn, client):
    product = _setup(conn, client)
    report = products.track_from_image(conn, client, product, UUID)

    assert report["added"] == ["alpha", "beta", "gamma"]
    assert report["binaries"] == 3
    assert report["srpms"] == 3
    assert report["existing"] == []
    assert report["not_found"] == []

    # tracked + baseline snapshots from the reference data
    members = products.product_members(conn, product)
    assert len(members) == 3
    assert all(m["added_by"] == "image" for m in members)
    assert all(m["watch_errata"] == 0 and m["watch_tasks"] == 0 for m in members)

    snap = conn.execute(
        "SELECT * FROM snapshots WHERE branch='p11' AND version='1.0'"
    ).fetchone()
    assert snap is not None and snap["present"] == 1
    assert snap["pkghash"] is not None

    # journal: one tracking_started per package, no history events
    events = conn.execute("SELECT * FROM journal").fetchall()
    assert all(e["event_type"] == TRACKING_STARTED for e in events)
    assert len(events) == 3
    assert json.loads(events[0]["detail"])["bulk"] is True

    # meta was synced automatically (first use)
    assert metasync.is_stale(conn, "p11") is False

    # image registered
    imgs = products.product_images(conn, product)
    assert len(imgs) == 1 and imgs[0]["kind"] == "release"


def test_image_binding_is_idempotent(conn, client):
    product = _setup(conn, client)
    products.track_from_image(conn, client, product, UUID)
    # update/track on the same image must not double-bind it
    products.track_from_image(conn, client, product, UUID)
    imgs = products.product_images(conn, product)
    assert len(imgs) == 1
    assert imgs[0]["package_count"] == 3


def test_init_db_dedupes_product_images(conn):
    from alttrack.db import init_db

    # simulate a pre-index database that accumulated duplicate bindings
    conn.execute("DROP INDEX IF EXISTS idx_product_images_uniq")
    with conn:
        conn.execute(
            "INSERT INTO product_images (product_id, image_uuid, added_at) VALUES "
            "(1, 'u1', '2025-01-01'), (1, 'u1', '2025-01-02')"
        )
    init_db(conn)

    rows = conn.execute(
        "SELECT added_at FROM product_images WHERE image_uuid='u1'"
    ).fetchall()
    assert [r["added_at"] for r in rows] == ["2025-01-01"]  # earliest binding kept
    try:
        conn.execute(
            "INSERT INTO product_images (product_id, image_uuid, added_at) "
            "VALUES (1, 'u1', 'x')"
        )
        raise AssertionError("expected IntegrityError")
    except sqlite3.IntegrityError:
        pass


def test_track_respects_watch_flags(conn, client):
    product = _setup(conn, client, names=("alpha",))
    report = products.track_from_image(
        conn, client, product, UUID, watch_errata=True, watch_maintainer=True
    )
    assert report["added"] == ["alpha"]
    row = conn.execute("SELECT watch_errata, watch_maintainer, watch_tasks FROM tracked_packages").fetchone()
    assert (row["watch_errata"], row["watch_maintainer"], row["watch_tasks"]) == (1, 1, 0)


def test_track_links_existing_package(conn, client):
    product = _setup(conn, client)
    # "alpha" is already tracked manually in another branch
    conn.execute(
        "INSERT INTO tracked_packages (name, branches, added_at) "
        "VALUES ('alpha', ?, '2024-01-01')",
        (json.dumps(["sisyphus"]),),
    )
    report = products.track_from_image(conn, client, product, UUID)
    assert report["added"] == ["beta", "gamma"]
    assert report["existing"] == ["alpha"]
    assert report["branch_updated"] == ["alpha"]  # p11 appended to branches

    row = conn.execute("SELECT branches, enabled FROM tracked_packages WHERE name='alpha'").fetchone()
    assert json.loads(row["branches"]) == ["sisyphus", "p11"]
    assert row["enabled"] == 1
    # still exactly one tracked_packages row per name (UNIQUE)
    assert conn.execute("SELECT COUNT(*) c FROM tracked_packages WHERE name='alpha'").fetchone()["c"] == 1


def test_track_not_found_binaries(conn, client):
    product = _setup(conn, client, names=("alpha",))
    client.source_map = {}  # nothing resolves
    report = products.track_from_image(conn, client, product, UUID)
    assert report["added"] == []
    assert report["not_found"] == ["alpha-libs"]
    assert report["srpms"] == 0


def test_track_registers_product_package_without_duplicates(conn, client):
    product = _setup(conn, client, names=("alpha",))
    products.track_from_image(conn, client, product, UUID)
    products.track_from_image(conn, client, product, UUID)  # idempotent re-add
    members = products.product_members(conn, product)
    assert len(members) == 1
    assert products.product_counts(conn, product) == {"total": 1, "active": 1, "paused": 0}


# ---------------------------------------------------------------------------
# Diff-update
# ---------------------------------------------------------------------------
def _set_image_names(client, names):
    client.image_pkgs[UUID] = [
        {"name": f"{n}-libs", "version": "1.0", "release": "alt1", "arch": "x86_64"}
        for n in names
    ]
    client.source_map = {
        f"{n}-libs": {"name": f"{n}-libs", "sourcepkgname": n, "status": "found"}
        for n in names
    }


def test_update_adds_and_pauses(conn, client):
    product = _setup(conn, client)  # alpha, beta, gamma
    products.track_from_image(conn, client, product, UUID)

    # new image: alpha, beta, delta (gamma dropped)
    _set_image_names(client, ("alpha", "beta", "delta"))
    client.repo[("p11", "source")].append(
        {"name": "delta", "version": "1.0", "release": "alt1", "hash": "h-delta",
         "summary": "", "category": "", "maintainer": ""}
    )
    metasync.sync(conn, client, branch="p11")

    report = products.update_from_image(conn, client, product, UUID)
    assert report["added"] == ["delta"]
    assert report["paused"] == ["gamma"]

    counts = products.product_counts(conn, product)
    assert counts == {"total": 4, "active": 3, "paused": 1}

    gamma = conn.execute(
        "SELECT t.enabled, pp.paused_by_image FROM product_packages pp "
        "JOIN tracked_packages t ON t.id = pp.package_id WHERE t.name='gamma'"
    ).fetchone()
    assert gamma["enabled"] == 0 and gamma["paused_by_image"] == 1


def test_update_reactivates_returned_package(conn, client):
    product = _setup(conn, client)
    products.track_from_image(conn, client, product, UUID)
    _set_image_names(client, ("alpha",))  # beta, gamma dropped
    products.update_from_image(conn, client, product, UUID)
    assert products.product_counts(conn, product)["active"] == 1

    _set_image_names(client, ("alpha", "beta"))  # beta returns
    report = products.update_from_image(conn, client, product, UUID)
    assert "beta" in report["reactivated"]
    counts = products.product_counts(conn, product)
    assert counts["active"] == 2 and counts["paused"] == 1  # gamma stays paused


def test_update_preserves_manual_members(conn, client):
    product = _setup(conn, client)
    products.track_from_image(conn, client, product, UUID)
    # add a manual package not in the image
    conn.execute(
        "INSERT INTO tracked_packages (name, branches, added_at) "
        "VALUES ('manualpkg', ?, '2025-01-01')",
        (json.dumps(["p11"]),),
    )
    pid = conn.execute("SELECT id FROM tracked_packages WHERE name='manualpkg'").fetchone()["id"]
    conn.execute(
        "INSERT INTO product_packages (product_id, package_id, added_by, paused_by_image, added_at) "
        "VALUES (?, ?, 'manual', 0, '2025-01-01')",
        (product.id, pid),
    )

    _set_image_names(client, ("alpha",))  # beta, gamma out of image
    report = products.update_from_image(conn, client, product, UUID)
    assert report["paused"] == ["beta", "gamma"]
    manual = conn.execute(
        "SELECT t.enabled, pp.added_by FROM product_packages pp "
        "JOIN tracked_packages t ON t.id = pp.package_id WHERE t.name='manualpkg'"
    ).fetchone()
    assert manual["enabled"] == 1 and manual["added_by"] == "manual"


def test_preview_counts_updated_versions(conn, client):
    product = _setup(conn, client)
    metasync.sync(conn, client, branch="p11")
    # reference says alpha 1.0, image mapping says alpha 2.0
    client.source_map["alpha-libs"].update({"version": "2.0", "release": "alt1"})
    preview = products.preview_from_image(conn, client, product, UUID)
    assert preview["updated"] == 1
    assert preview["new"] == ["alpha", "beta", "gamma"]
