"""SQLite storage layer: schema, migrations, connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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
