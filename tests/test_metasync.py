"""Tests for package_meta sync and lookup (metasync)."""

from __future__ import annotations

from alttrack import metasync


def test_sync_stores_reference_data(conn, client):
    client.repo[("p11", "source")] = [
        {"name": "firefox", "version": "155.0.1", "release": "alt1",
         "hash": "3386769748582984287", "summary": "Browser",
         "category": "Networking/WWW", "maintainer": "rauty@altlinux.org"},
        {"name": "kernel-image", "version": "6.6", "release": "alt1",
         "hash": "42", "summary": "Kernel", "category": "System/Kernel",
         "maintainer": "kernel@altlinux.org"},
    ]
    report = metasync.sync(conn, client, branch="p11")
    assert report["count"] == 2
    assert metasync.is_stale(conn, "p11") is False
    assert metasync.is_stale(conn, "p10") is True

    status = metasync.status(conn)
    assert status[0]["branch"] == "p11"
    assert status[0]["count"] == 2


def test_ensure_syncs_only_once(conn, client):
    client.repo[("p11", "source")] = [{"name": "a", "version": "1", "release": "alt1", "hash": "1"}]
    first = metasync.ensure(conn, client, branch="p11")
    assert first is not None
    second = metasync.ensure(conn, client, branch="p11")
    assert second is None
    assert client.calls.count("repository_packages:p11") == 1


def test_lookup_prefers_branch(conn, client):
    client.repo[("p11", "source")] = [
        {"name": "foo", "version": "2", "release": "alt1", "hash": "b", "summary": "p11", "category": "", "maintainer": ""},
    ]
    client.repo[("sisyphus", "source")] = [
        {"name": "foo", "version": "3", "release": "alt1", "hash": "c", "summary": "sis", "category": "", "maintainer": ""},
    ]
    metasync.sync(conn, client, branch="p11")
    metasync.sync(conn, client, branch="sisyphus")

    hit = metasync.lookup(conn, ["foo"], branch="p11")
    assert hit["foo"]["summary"] == "p11"
    # fallback: unknown branch resolves to any branch
    fb = metasync.lookup(conn, ["foo"], branch="p10")
    assert fb["foo"]["summary"] in ("p11", "sis")
    # missing name simply absent
    assert "bar" not in metasync.lookup(conn, ["bar", "foo"], branch="p11")


def test_pkghash_stored(conn, client):
    client.repo[("p11", "source")] = [
        {"name": "foo", "version": "1", "release": "alt1", "hash": "12345"},
    ]
    metasync.sync(conn, client, branch="p11")
    row = conn.execute("SELECT pkghash FROM package_meta WHERE name='foo'").fetchone()
    assert row["pkghash"] == "12345"
