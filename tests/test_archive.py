"""Archiving and full-text search across live journal and archive."""

from __future__ import annotations

from alttrack import archive, journal


def _seed_events(conn, count: int, *, old_days: int = 0, package: str = "firefox") -> list[int]:
    seqs = []
    for i in range(count):
        if old_days:
            ts = f"2020-01-{(i % 28) + 1:02d}T00:00:{i % 60:02d}Z"
        else:
            ts = f"2026-09-30T10:{i % 60:02d}:00Z"
        seqs.append(
            journal.insert_event(
                conn,
                package=package,
                branch="sisyphus",
                event_type="version_changed",
                old_value=f"{i}-old",
                new_value=f"{i}-new",
                detail={"idx": i, "payload": f"findme-{i}"},
                ts=ts,
            )
        )
    return seqs


def test_archive_moves_old_rows(conn, cfg):
    _seed_events(conn, 3, old_days=1)
    _seed_events(conn, 2)

    result = archive.archive_now(conn, cfg, older_than_days=90)
    assert result["moved"] == 3
    live = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
    stored = conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"]
    assert (live, stored) == (2, 3)


def test_archive_dry_run_changes_nothing(conn, cfg):
    _seed_events(conn, 4, old_days=1)
    result = archive.archive_now(conn, cfg, older_than_days=90, dry_run=True)
    assert result["moved"] == 4 and result["dry_run"]
    assert conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"] == 4


def test_max_live_rows_trims_oldest(conn, cfg):
    _seed_events(conn, 10)
    result = archive.archive_now(conn, cfg, older_than_days=10_000, max_live_rows=4)
    assert result["moved"] == 6
    remaining = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
    assert remaining == 4


def test_archive_scoped_to_package(conn, cfg):
    _seed_events(conn, 3, old_days=1, package="firefox")
    _seed_events(conn, 2, old_days=1, package="htop")
    result = archive.archive_now(conn, cfg, older_than_days=90, package="firefox")
    assert result["moved"] == 3
    left = {r["package"] for r in conn.execute("SELECT DISTINCT package FROM journal")}
    assert left == {"htop"}


def test_search_covers_archive_and_live(conn, cfg):
    _seed_events(conn, 3, old_days=1)
    _seed_events(conn, 2)
    archive.archive_now(conn, cfg, older_than_days=90)

    live, _ = journal.query_events(conn, q="findme", scope="live")
    archived, _ = journal.query_events(conn, q="findme", scope="archive")
    both, _ = journal.query_events(conn, q="findme", scope="all")
    assert len(live) == 2
    assert len(archived) == 3
    assert len(both) == 5
    assert {e.store for e in both} == {"live", "archive"}


def test_archived_rows_are_hidden_from_live_scope(conn, cfg):
    _seed_events(conn, 3, old_days=1)
    archive.archive_now(conn, cfg, older_than_days=90)
    rows, _ = journal.query_events(conn, scope="live")
    assert rows == []
    rows, _ = journal.query_events(conn, scope="archive")
    assert len(rows) == 3


def test_fts_query_matches_detail_values(conn, cfg):
    journal.insert_event(
        conn,
        package="firefox",
        branch="sisyphus",
        event_type="errata",
        new_value="ALT-PU-2026-1",
        detail={"refs": ["CVE-2026-1234"]},
    )
    events, _ = journal.query_events(conn, q="CVE-2026-1234", scope="all")
    assert len(events) == 1
    events, _ = journal.query_events(conn, q="nonexistent-token-xyz", scope="all")
    assert events == []


def test_archive_by_package_only_matches_package(conn, cfg):
    _seed_events(conn, 5, old_days=1, package="firefox")
    _seed_events(conn, 5, old_days=1, package="htop")
    seqs = archive.archive_plan(conn, cfg, older_than_days=90, package="htop")
    assert len(seqs) == 5


def test_purge_archive(conn, cfg):
    _seed_events(conn, 3, old_days=1)
    archive.archive_now(conn, cfg, older_than_days=90)
    res = archive.purge_archive(conn)
    assert res["deleted"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"] == 0
    # FTS index must follow the purge.
    assert journal.query_events(conn, q="findme", scope="all")[0] == []


def test_stats_counts_both_stores(conn, cfg):
    _seed_events(conn, 3, old_days=1)
    _seed_events(conn, 2)
    archive.archive_now(conn, cfg, older_than_days=90)
    data = journal.stats(conn, scope="all")
    assert data["total"] == 5
    assert data["live"] == 2
    assert data["archive"] == 3
    assert data["by_type"]["version_changed"] == 5


def test_auto_archive_respects_flag(conn, cfg):
    _seed_events(conn, 3, old_days=1)
    cfg.auto_archive = False
    assert archive.auto_archive(conn, cfg) == 0
    cfg.auto_archive = True
    assert archive.auto_archive(conn, cfg) == 3
