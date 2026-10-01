"""SQLite storage layer: schema, migrations, connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_packages (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    branches         TEXT NOT NULL DEFAULT '[]',
    note             TEXT NOT NULL DEFAULT '',
    watch_errata     INTEGER NOT NULL DEFAULT 1,
    watch_maintainer INTEGER NOT NULL DEFAULT 1,
    watch_tasks      INTEGER NOT NULL DEFAULT 1,
    enabled          INTEGER NOT NULL DEFAULT 1,
    added_at         TEXT NOT NULL,
    last_checked_at  TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    package_id     INTEGER NOT NULL,
    branch         TEXT NOT NULL,
    present        INTEGER NOT NULL DEFAULT 1,
    version        TEXT,
    release        TEXT,
    pkghash        TEXT,
    packager       TEXT,
    packager_nick  TEXT,
    acl            TEXT,
    task_id        INTEGER,
    task_date      TEXT,
    observed_at    TEXT NOT NULL,
    PRIMARY KEY (package_id, branch)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    checked      INTEGER NOT NULL DEFAULT 0,
    events       INTEGER NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    run_id     INTEGER,
    package_id INTEGER,
    package    TEXT NOT NULL,
    branch     TEXT,
    event_type TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    detail     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS journal_archive (
    seq            INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    run_id         INTEGER,
    package_id     INTEGER,
    package        TEXT NOT NULL,
    branch         TEXT,
    event_type     TEXT NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    detail         TEXT NOT NULL DEFAULT '{}',
    archived_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errata_seen (
    errata_id  TEXT NOT NULL,
    package_id INTEGER NOT NULL,
    branch     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (errata_id, package_id, branch)
);

CREATE TABLE IF NOT EXISTS tasks_seen (
    pkghash    TEXT NOT NULL,
    package_id INTEGER NOT NULL,
    branch     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (pkghash, package_id, branch)
);

CREATE TABLE IF NOT EXISTS build_tasks (
    task_id       INTEGER NOT NULL,
    branch        TEXT NOT NULL,
    state         TEXT NOT NULL,
    stage         TEXT,
    owner         TEXT,
    try_no        INTEGER,
    iter_no       INTEGER,
    testonly      INTEGER NOT NULL DEFAULT 0,
    changed_at    TEXT,
    message       TEXT NOT NULL DEFAULT '',
    subtasks      TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    terminal      INTEGER NOT NULL DEFAULT 0,
    resolved      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, branch)
);

CREATE TABLE IF NOT EXISTS task_packages (
    task_id     INTEGER NOT NULL,
    package_id  INTEGER NOT NULL,
    subtask_tag TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, package_id)
);

CREATE TABLE IF NOT EXISTS task_stages (
    id     INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    ts     TEXT NOT NULL,
    stage  TEXT,
    state  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal (ts);
CREATE INDEX IF NOT EXISTS idx_journal_pkg ON journal (package_id);
CREATE INDEX IF NOT EXISTS idx_journal_type ON journal (event_type);
CREATE INDEX IF NOT EXISTS idx_archive_ts ON journal_archive (ts);
CREATE INDEX IF NOT EXISTS idx_archive_pkg ON journal_archive (package_id);
CREATE INDEX IF NOT EXISTS idx_archive_type ON journal_archive (event_type);
CREATE INDEX IF NOT EXISTS idx_tasks_terminal ON build_tasks (terminal, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_task_packages_pkg ON task_packages (package_id);
CREATE INDEX IF NOT EXISTS idx_task_stages_task ON task_stages (task_id, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_pkg ON snapshots (package_id);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    seq UNINDEXED,
    ts,
    package,
    branch,
    event_type,
    text,
    store,
    tokenize = 'unicode61'
);

-- ------------------------------------------------------------------
-- Phase "products / images / comparison" (schema v2)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    branch     TEXT NOT NULL,
    edition    TEXT NOT NULL DEFAULT '',
    arch       TEXT NOT NULL DEFAULT 'x86_64',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_packages (
    product_id      INTEGER NOT NULL,
    package_id      INTEGER NOT NULL,
    added_by        TEXT NOT NULL DEFAULT 'image',   -- image | manual
    paused_by_image INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL,
    PRIMARY KEY (product_id, package_id)
);

CREATE TABLE IF NOT EXISTS product_images (
    id            INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL,
    image_uuid    TEXT NOT NULL,
    tag           TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'release',   -- release | other
    date          TEXT,
    package_count INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_catalog (
    uuid       TEXT PRIMARY KEY,
    branch     TEXT NOT NULL,
    edition    TEXT NOT NULL DEFAULT '',
    arch       TEXT NOT NULL DEFAULT '',
    variant    TEXT NOT NULL DEFAULT '',
    type       TEXT NOT NULL DEFAULT '',
    release    TEXT NOT NULL DEFAULT '',
    tag        TEXT NOT NULL DEFAULT '',
    file       TEXT NOT NULL DEFAULT '',
    date       TEXT,
    synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_meta (
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,                 -- source | binary
    branch     TEXT NOT NULL,
    version    TEXT NOT NULL DEFAULT '',
    release    TEXT NOT NULL DEFAULT '',
    pkghash    TEXT,
    summary    TEXT NOT NULL DEFAULT '',
    category   TEXT NOT NULL DEFAULT '',
    maintainer TEXT NOT NULL DEFAULT '',
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (name, kind, branch)
);

CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- file | image | branch
    product_id  INTEGER,
    params      TEXT NOT NULL DEFAULT '{}',
    item_count  INTEGER NOT NULL DEFAULT 0,
    bad_lines   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_items (
    list_id     INTEGER NOT NULL,
    name        TEXT NOT NULL COLLATE NOCASE,
    version     TEXT NOT NULL DEFAULT '',
    release     TEXT NOT NULL DEFAULT '',
    arch        TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    source_name TEXT,
    PRIMARY KEY (list_id, name)
);

CREATE TABLE IF NOT EXISTS comparisons (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    product_id    INTEGER,
    left_list_id  INTEGER NOT NULL,
    right_list_id INTEGER NOT NULL,
    stats         TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_packages_pkg ON product_packages (package_id);
CREATE INDEX IF NOT EXISTS idx_product_images_prod ON product_images (product_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_product_images_uniq ON product_images (product_id, image_uuid);
CREATE INDEX IF NOT EXISTS idx_image_catalog_sel ON image_catalog (branch, edition, arch);
CREATE INDEX IF NOT EXISTS idx_package_meta_branch ON package_meta (branch, kind);
CREATE INDEX IF NOT EXISTS idx_list_items_name ON list_items (name);
CREATE INDEX IF NOT EXISTS idx_comparisons_product ON comparisons (product_id);

-- Reference lists from rdb: architectures and software groups (categories).
CREATE TABLE IF NOT EXISTS reference_lists (
    kind      TEXT NOT NULL,        -- arch | category
    value     TEXT NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
"""


def connect(db_path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database with sane defaults (WAL, foreign keys, row factory)."""
    path = Path(db_path)
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if missing and apply pending migrations."""
    with conn:
        # Older databases could double-bind the same image to a product
        # (track + update both inserted a row): keep the earliest binding
        # before the unique index in SCHEMA must be created.
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_images'"
        ).fetchone():
            conn.execute(
                "DELETE FROM product_images WHERE id NOT IN "
                "(SELECT MIN(id) FROM product_images GROUP BY product_id, image_uuid)"
            )
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            version = int(row["value"])
            if version < SCHEMA_VERSION:
                _migrate(conn, version)
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Placeholder for future schema migrations."""
    del conn, from_version


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Open and initialise the database in one step."""
    conn = connect(db_path)
    init_db(conn)
    return conn
