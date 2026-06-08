"""
Legion — Import V1 (legacy features.db → legion.db)

Scans legacy V1 kanban boards directories for features.db files and imports
their contents into the unified legion.db, with conflict detection,
divergence logging, metadata fusion, and dry-run support.
"""
import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


# ── Data types ─────────────────────────────────────────────────────────────


@dataclass
class ImportReport:
    """Per-project report for a V1 import operation."""
    project_slug: str
    features_found: int = 0
    imported: int = 0
    skipped_existing: int = 0
    divergent: list = field(default_factory=list)   # [{slug, v1_status, legion_status}]
    skipped_no_project: bool = False
    skipped_corrupt: bool = False
    errors: list = field(default_factory=list)       # [str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


# ── V1 features.db schema ──────────────────────────────────────────────────

V1_SCHEMA_SQL = """
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


# ── Core import function ───────────────────────────────────────────────────


def detect_v1_boards(kanban_boards_path: str) -> list[tuple[str, str]]:
    """
    Scan a kanban boards directory for V1 features.db files.

    Returns a list of (project_slug, features_db_path) tuples.
    V1 boards are subdirectories containing a features.db file.
    """
    results = []
    if not os.path.isdir(kanban_boards_path):
        return results

    for entry in sorted(os.listdir(kanban_boards_path)):
        board_dir = os.path.join(kanban_boards_path, entry)
        features_db = os.path.join(board_dir, "features.db")
        if os.path.isdir(board_dir) and os.path.isfile(features_db):
            results.append((entry, features_db))

    return results


def import_v1_features(
    db_path: Optional[str] = None,
    kanban_boards_path: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, ImportReport]:
    """
    Import V1 features from legacy features.db files into legion.db.

    Scans each project directory under *kanban_boards_path* for a features.db,
    then imports every feature row into the unified legion database.

    Args:
        db_path: Path to legion.db.  Defaults to ~/.legion/db/legion.db.
        kanban_boards_path: Path to the kanban boards directory containing
            per-project subdirectories.  Defaults to ~/.hermes/kanban/boards.
        dry_run: If True, analyse but do not write anything.

    Returns:
        A dict mapping project_slug → ImportReport.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.legion/db/legion.db")
    if kanban_boards_path is None:
        kanban_boards_path = os.path.expanduser("~/.hermes/kanban/boards")

    reports: dict[str, ImportReport] = {}
    boards = detect_v1_boards(kanban_boards_path)

    if not boards:
        return reports

    for project_slug, features_db_path in boards:
        report = ImportReport(project_slug=project_slug)
        reports[project_slug] = report
        _import_single_project(report, project_slug, features_db_path, db_path, dry_run)

    return reports


# ── Internal helpers ────────────────────────────────────────────────────────


def _import_single_project(
    report: ImportReport,
    project_slug: str,
    features_db_path: str,
    db_path: str,
    dry_run: bool,
) -> None:
    """Handle the import for one project, populating *report*."""

    # 1. Verify the project exists in legion.db
    if not _project_exists(db_path, project_slug):
        report.skipped_no_project = True
        return

    # 2. Open V1 features.db (handle corruption)
    try:
        v1_conn = sqlite3.connect(features_db_path)
        v1_conn.row_factory = sqlite3.Row
        v1_conn.execute("PRAGMA query_only = 1")  # safety: read-only
    except (sqlite3.DatabaseError, Exception) as exc:
        report.skipped_corrupt = True
        report.errors.append(str(exc))
        return

    try:
        # Check if table exists (handles corrupt DBs that connect() doesn't reject)
        try:
            cur = v1_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='features'"
            )
        except sqlite3.DatabaseError as exc:
            report.skipped_corrupt = True
            report.errors.append(f"corrupted database: {exc}")
            v1_conn.close()
            return

        if not cur.fetchone():
            report.skipped_corrupt = True
            report.errors.append("features table not found")
            v1_conn.close()
            return

        # Read all features
        try:
            rows = v1_conn.execute("SELECT * FROM features").fetchall()
        except sqlite3.DatabaseError as exc:
            report.skipped_corrupt = True
            report.errors.append(f"cannot read features: {exc}")
            v1_conn.close()
            return

        report.features_found = len(rows)

        if dry_run:
            # In dry-run mode, just analyse — don't write
            # Fetch existing features to calculate what would happen
            existing_slugs = _get_existing_slugs(db_path, project_slug)
            for row in rows:
                feat = dict(row)
                if feat["slug"] in existing_slugs:
                    existing_feat = _get_feature_by_slug(db_path, feat["slug"], project_slug)
                    if existing_feat and existing_feat["status"] != feat.get("status", "backlog"):
                        report.divergent.append({
                            "slug": feat["slug"],
                            "v1_status": feat.get("status", "backlog"),
                            "legion_status": existing_feat["status"],
                        })
                    else:
                        report.skipped_existing += 1
                else:
                    report.imported += 1
            v1_conn.close()
            return

        # 3. Real import
        leg_conn = _get_leg_conn(db_path)
        try:
            existing_slugs = _get_existing_slugs(db_path, project_slug)

            for row in rows:
                feat = dict(row)

                if feat["slug"] in existing_slugs:
                    existing_feat = _get_feature_by_slug(db_path, feat["slug"], project_slug)
                    if existing_feat and existing_feat["status"] != feat.get("status", "backlog"):
                        report.divergent.append({
                            "slug": feat["slug"],
                            "v1_status": feat.get("status", "backlog"),
                            "legion_status": existing_feat["status"],
                        })
                    report.skipped_existing += 1
                else:
                    _insert_feature(leg_conn, project_slug, feat)
                    report.imported += 1

            # 4. Import feature_meta (merge: keep existing, add missing)
            _import_meta_fusion(v1_conn, leg_conn, project_slug, existing_slugs)

            # 5. Update sync_status (UPSERT)
            if not dry_run:
                _upsert_sync_status(leg_conn, project_slug)

        except Exception as exc:
            report.errors.append(str(exc))
        finally:
            leg_conn.close()

    finally:
        v1_conn.close()


# ── Low-level DB helpers ────────────────────────────────────────────────────


def _project_exists(db_path: str, project_slug: str) -> bool:
    """Check if a project exists in legion.db."""
    conn = _get_leg_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM projects WHERE slug=?", (project_slug,)
        )
        return cur.fetchone() is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _get_existing_slugs(db_path: str, project_slug: str) -> set[str]:
    """Return the set of feature slugs that already exist for this project."""
    conn = _get_leg_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT slug FROM features WHERE project_slug=?", (project_slug,)
        )
        return {r["slug"] for r in cur.fetchall()}
    finally:
        conn.close()


def _get_feature_by_slug(db_path: str, slug: str, project_slug: str) -> Optional[dict]:
    """Fetch a single feature from legion.db."""
    conn = _get_leg_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM features WHERE slug=? AND project_slug=?",
            (slug, project_slug),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _insert_feature(conn: sqlite3.Connection, project_slug: str, feat: dict) -> None:
    """Insert one feature into legion.db."""
    now = int(datetime.now().timestamp())
    conn.execute(
        """INSERT OR IGNORE INTO features
           (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            feat["slug"],
            project_slug,
            feat.get("prefix", ""),
            feat.get("name", ""),
            feat.get("domaine", ""),
            feat.get("status", "backlog"),
            feat.get("created_at", now),
            now,
        ),
    )
    conn.commit()


def _import_meta_fusion(
    v1_conn: sqlite3.Connection,
    leg_conn: sqlite3.Connection,
    project_slug: str,
    existing_slugs: set[str],
) -> None:
    """Merge feature_meta: keep existing legion keys, add missing V1 keys."""
    try:
        meta_rows = v1_conn.execute(
            "SELECT slug, key, value FROM feature_meta"
        ).fetchall()
    except sqlite3.DatabaseError:
        return  # no meta table — nothing to import

    for m in meta_rows:
        v1_slug = m["slug"]
        v1_key = m["key"]
        v1_value = m["value"]

        if v1_slug in existing_slugs:
            # Check if key already exists in legion
            cur = leg_conn.execute(
                "SELECT 1 FROM feature_meta WHERE feature_slug=? AND project_slug=? AND key=?",
                (v1_slug, project_slug, v1_key),
            )
            if cur.fetchone():
                continue  # keep existing value

        leg_conn.execute(
            "INSERT OR IGNORE INTO feature_meta (feature_slug, project_slug, key, value) VALUES (?, ?, ?, ?)",
            (v1_slug, project_slug, v1_key, v1_value),
        )
    leg_conn.commit()


def _upsert_sync_status(conn: sqlite3.Connection, project_slug: str) -> None:
    """Update sync_status after import."""
    now = int(datetime.now().timestamp())
    conn.execute(
        """INSERT INTO sync_status (project_slug, last_sync_at, status, message)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(project_slug) DO UPDATE SET
               last_sync_at = excluded.last_sync_at,
               status = excluded.status,
               message = excluded.message""",
        (project_slug, now, "ok", "V1 import done"),
    )
    conn.commit()


def _get_leg_conn(db_path: str) -> sqlite3.Connection:
    """Open a legion.db connection with Row factory and WAL mode."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
