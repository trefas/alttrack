"""Reference lists: architectures and software groups (refs module)."""

from __future__ import annotations

from alttrack import lists, refs, rpmparse


# ---------------------------------------------------------------------------
# sync / ensure
# ---------------------------------------------------------------------------
def test_sync_stores_all_lists(conn, client):
    report = refs.sync(conn, client)

    assert report["category"] == 2
    assert report["branch"] == 3
    assert report["branches"] == ["p10", "p11", "sisyphus"]

    arches = refs.architectures(conn)
    # endpoint values + built-in parser defaults
    assert {"x86_64", "i586", "noarch"} <= set(arches)
    assert "aarch64" in arches  # from the built-in defaults

    cats = {c["value"]: c["count"] for c in refs.categories(conn)}
    assert cats == {"Shells": 5, "System/Base": 12}

    assert refs.branches(conn) == ["p10", "p11", "sisyphus"]

    status = {r["kind"]: r for r in refs.status(conn)}
    assert status["category"]["count"] == 2
    assert status["arch"]["count"] == len(arches)
    assert status["arch"]["synced_at"]
    assert status["branch"]["count"] == 3
    assert status["branch"]["synced_at"]


def test_sync_adds_image_catalog_architectures(conn, client):
    with conn:
        conn.execute(
            "INSERT INTO image_catalog (uuid, branch, edition, arch, synced_at) "
            "VALUES ('u1', 'p11', 'alt-edu', 'ppc64le', '2025-01-01')"
        )
    refs.sync(conn, client)
    assert "ppc64le" in refs.architectures(conn)


def test_ensure_fills_only_once(conn, client):
    first = refs.ensure(conn, client)
    assert first is not None
    calls = len(client.calls)
    assert refs.ensure(conn, client) is None
    assert len(client.calls) == calls  # no API traffic on second ensure


def test_architectures_fallback_without_sync(conn):
    assert refs.architectures(conn) == sorted(rpmparse.ARCHITECTURES)
    assert refs.categories(conn) == []
    assert refs.status(conn) == []


def test_refresh_fills_reference_lists(conn, client, cfg):
    from alttrack import refresh

    result = refresh.run_refresh(conn, client, cfg)
    assert result.status != "busy"
    kinds = {r["kind"] for r in refs.status(conn)}
    assert kinds == {"arch", "category", "branch"}


# ---------------------------------------------------------------------------
# consumers
# ---------------------------------------------------------------------------
def test_parser_recognises_stored_architectures(conn):
    with conn:
        conn.execute(
            "INSERT INTO reference_lists (kind, value, count, synced_at) "
            "VALUES ('arch', 'z80', 0, '2025-01-01')"
        )
    known = refs.parser_architectures(conn)

    injected = rpmparse.parse_rpm_line("foo-1.0-alt1.z80", arches=known)
    assert injected.ok and injected.arch == "z80" and injected.release == "alt1"

    default = rpmparse.parse_rpm_line("foo-1.0-alt1.z80")
    assert default.ok and default.arch == "" and default.release == "alt1.z80"


def test_save_file_list_uses_reference_architectures(conn, client):
    with conn:
        conn.execute(
            "INSERT INTO reference_lists (kind, value, count, synced_at) "
            "VALUES ('arch', 'z80', 0, '2025-01-01')"
        )
    list_obj, report = lists.save_file_list(conn, text="foo-1.0-alt1.z80\n")
    assert report["parsed"] == 1
    item = lists.list_items(conn, list_obj.id)[0]
    assert item["name"] == "foo" and item["arch"] == "z80"
    assert item["release"] == "alt1"


# ---------------------------------------------------------------------------
# branch reference (feeds the selects on Products / Images / Lists)
# ---------------------------------------------------------------------------
def test_branch_options_common_first(conn, client):
    refs.sync(conn, client)
    with conn:
        conn.execute(
            "INSERT INTO reference_lists (kind, value, count, synced_at) "
            "VALUES ('branch', 'c10f2', 0, '2025-01-01')"
        )
    # привычные продуктовые ветки впереди, остальное — по алфавиту
    assert refs.branch_options(conn) == ["sisyphus", "p11", "p10", "c10f2"]


def test_branches_fall_back_to_refresh_cache(conn):
    # база ещё не заполнила справочник веток — берём кэш из метаданных
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('active_branches', "
            "'[\"p11\", \"sisyphus\"]') "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        )
    assert refs.branches(conn) == ["p11", "sisyphus"]
    assert refs.branch_options(conn) == ["sisyphus", "p11"]

    with conn:
        conn.execute("DELETE FROM meta WHERE key='active_branches'")
    assert refs.branches(conn) == []
    assert refs.branch_options(conn) == []


def test_ensure_upgrades_database_without_branch_ref(conn, client):
    # база, заполненная до появления справочника веток
    with conn:
        conn.executemany(
            "INSERT INTO reference_lists (kind, value, count, synced_at) "
            "VALUES (?, ?, 0, '2025-01-01')",
            [("arch", "x86_64"), ("category", "Shells")],
        )
    assert refs.ensure(conn, client) is not None
    assert refs.branches(conn) == ["p10", "p11", "sisyphus"]

    calls = len(client.calls)
    assert refs.ensure(conn, client) is None
    assert len(client.calls) == calls  # заполнено — повторно в API не ходим
