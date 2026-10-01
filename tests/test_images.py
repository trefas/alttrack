"""Tests for the image catalog cache (images)."""

from __future__ import annotations

from alttrack import images


def _img(**kw):
    base = {
        "uuid": "u-1", "branch": "p11", "edition": "education", "arch": "x86_64",
        "variant": "", "type": "salt", "release": "release", "tag": "Sisyphus",
        "file": "alt-edu-11-20250101-x86_64.iso", "date": "2025-01-01",
    }
    base.update(kw)
    return base


def test_refresh_and_filter(conn, client):
    client.images = [
        _img(),
        _img(uuid="u-2", edition="kworkstation", date="2025-06-01"),
        _img(uuid="u-3", branch="p10", date="2024-01-01"),
    ]
    assert images.refresh_catalog(conn, client) == 3
    assert images.catalog_is_empty(conn) is False

    assert len(images.catalog(conn)) == 3
    assert len(images.catalog(conn, branch="p11")) == 2
    assert len(images.catalog(conn, branch="p11", edition="kworkstation")) == 1
    # newest first
    assert images.catalog(conn, branch="p11")[0]["uuid"] == "u-2"


def test_filter_options(conn, client):
    client.images = [_img(), _img(uuid="u-2", branch="p10", arch="aarch64")]
    images.refresh_catalog(conn, client)
    opts = images.filter_options(conn)
    assert opts["branches"] == ["p10", "p11"]
    assert opts["archs"] == ["aarch64", "x86_64"]


def test_find_image_and_latest_release(conn, client):
    client.images = [
        _img(uuid="old", date="2024-01-01"),
        _img(uuid="new", date="2025-06-01"),
        _img(uuid="test", date="2025-12-01", release="test"),
    ]
    images.refresh_catalog(conn, client)
    assert images.find_image(conn, "new")["uuid"] == "new"
    assert images.find_image(conn, "nope") is None
    latest = images.latest_release_image(conn, branch="p11", edition="education", arch="x86_64")
    assert latest["uuid"] == "new"  # test image is newer but not 'release'


def test_empty_refresh(conn, client):
    assert images.refresh_catalog(conn, client) == 0
    assert images.catalog_is_empty(conn) is True
