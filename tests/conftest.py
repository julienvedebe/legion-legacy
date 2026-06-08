"""
Shared test fixtures and helpers for test_import_v1.py.
"""
import os
import sqlite3
import pytest

# Same schema as core/db.py (simplified for testing)
LEGION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    project_type    TEXT DEFAULT 'custom',
    work_dir        TEXT NOT NULL,
    board           TEXT NOT NULL,
    extra_skills    TEXT DEFAULT '[]',
    docs_structure  TEXT DEFAULT 'product/',
    pipeline_config TEXT DEFAULT '{}',
    status          TEXT DEFAULT 'draft',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_profiles (
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    profile_name    TEXT NOT NULL,
    PRIMARY KEY (project_slug, role)
);

CREATE TABLE IF NOT EXISTS project_conventions (
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    stage           TEXT NOT NULL,
    doc_path        TEXT NOT NULL,
    PRIMARY KEY (project_slug, stage)
);

CREATE TABLE IF NOT EXISTS features (
    slug            TEXT NOT NULL,
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    prefix          TEXT NOT NULL,
    name            TEXT NOT NULL,
    domaine         TEXT DEFAULT '',
    status          TEXT DEFAULT 'backlog',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (slug, project_slug)
);

CREATE TABLE IF NOT EXISTS feature_meta (
    feature_slug    TEXT NOT NULL,
    project_slug    TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    PRIMARY KEY (feature_slug, project_slug, key),
    FOREIGN KEY (feature_slug, project_slug) REFERENCES features(slug, project_slug) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_status (
    project_slug    TEXT PRIMARY KEY,
    last_sync_at    INTEGER,
    status          TEXT DEFAULT 'pending',
    message         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
"""

V1_FEATURES_SQL = """
CREATE TABLE IF NOT EXISTS features (
    slug            TEXT PRIMARY KEY,
    prefix          TEXT NOT NULL,
    name            TEXT NOT NULL,
    domaine         TEXT DEFAULT '',
    status          TEXT DEFAULT 'backlog',
    created_at      INTEGER,
    updated_at      INTEGER
);

CREATE TABLE IF NOT EXISTS feature_meta (
    slug            TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    PRIMARY KEY (slug, key),
    FOREIGN KEY (slug) REFERENCES features(slug) ON DELETE CASCADE
);
"""


def create_legion_db(path: str) -> sqlite3.Connection:
    """Create a temporary legion.db with the full schema and return a connection."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(LEGION_SCHEMA_SQL)
    conn.commit()
    return conn


def create_features_db(path: str, features: list[dict], meta: dict[str, list[dict]] = None) -> None:
    """
    Create a V1 features.db at *path* with the given features and optional metadata.

    Args:
        path: Filesystem path for the database.
        features: List of feature dicts with at least {'slug', 'prefix', 'name'}.
        meta: Dict mapping feature_slug -> list of {'key': ..., 'value': ...}
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_FEATURES_SQL)

    for f in features:
        conn.execute(
            """INSERT INTO features (slug, prefix, name, domaine, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f["slug"],
                f.get("prefix", ""),
                f.get("name", ""),
                f.get("domaine", ""),
                f.get("status", "backlog"),
                f.get("created_at", int(__import__('time').time())),
                f.get("updated_at", int(__import__('time').time())),
            ),
        )

    if meta:
        for slug, entries in meta.items():
            for entry in entries:
                conn.execute(
                    "INSERT INTO feature_meta (slug, key, value) VALUES (?, ?, ?)",
                    (slug, entry["key"], entry.get("value", "")),
                )

    conn.commit()
    conn.close()


def add_project_to_legion(conn: sqlite3.Connection, slug: str, name: str = None) -> None:
    """Insert a project into an existing legion.db connection."""
    import time
    now = int(time.time())
    conn.execute(
        """INSERT OR IGNORE INTO projects
           (slug, name, project_type, work_dir, board, extra_skills, docs_structure, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, name or slug, "custom", f"/tmp/{slug}", f"boards/{slug}", "[]", "product/", "active", now, now),
    )
    conn.commit()


def make_boards_dir(tmp_path, slug: str) -> str:
    """Create a kanban board subdirectory for a project. Returns the path."""
    board_dir = tmp_path / "kanban-boards" / slug
    board_dir.mkdir(parents=True, exist_ok=True)
    return str(board_dir)
