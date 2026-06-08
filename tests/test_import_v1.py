"""
Tests for core/import_v1.py — V1 features.db → legion.db import.

Each test creates an isolated temporary database so they never touch the real
~/.legion/db/legion.db.
"""
import os
import sqlite3
import sys

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.import_v1 import (
    import_v1_features,
    detect_v1_boards,
    ImportReport,
)

from .conftest import (
    create_legion_db,
    create_features_db,
    add_project_to_legion,
    make_boards_dir,
)


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

SAMPLE_FEATURES = [
    {"slug": "AUTH",    "prefix": "AUTH", "name": "Authentication",     "status": "done"},
    {"slug": "PROFILE", "prefix": "PROFILE", "name": "User Profile",    "status": "in_progress"},
    {"slug": "NOTIF",   "prefix": "NOTIF", "name": "Notifications",     "status": "backlog"},
    {"slug": "SEARCH",  "prefix": "SEARCH", "name": "Search Engine",    "status": "done"},
    {"slug": "ADMIN",   "prefix": "ADMIN", "name": "Admin Panel",       "status": "backlog"},
]


# ══════════════════════════════════════════════════════════════════════════
# Test 1: Aucun features.db trouvé → rapport vide
# ══════════════════════════════════════════════════════════════════════════

def test_import_empty_v1(tmp_path):
    """No features.db found → empty report."""
    db_path = tmp_path / "legion.db"
    boards_path = tmp_path / "kanban-boards"
    boards_path.mkdir()

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(boards_path),
    )

    assert reports == {}, f"Expected empty dict, got {reports}"


# ══════════════════════════════════════════════════════════════════════════
# Test 2: Projet a 5 features V1, 0 dans legion.db → 5 importées
# ══════════════════════════════════════════════════════════════════════════

def test_import_all_new(tmp_path):
    """All features are new → all 5 are imported."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, SAMPLE_FEATURES)

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    assert "myproject" in reports
    r = reports["myproject"]
    assert r.features_found == 5, f"Expected 5 found, got {r.features_found}"
    assert r.imported == 5, f"Expected 5 imported, got {r.imported}"
    assert r.skipped_existing == 0
    assert r.divergent == []

    # Verify in DB
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute("SELECT slug, status FROM features WHERE project_slug='myproject'")
    rows = {r["slug"]: r["status"] for r in cur.fetchall()}
    vconn.close()
    assert len(rows) == 5
    assert rows["AUTH"] == "done"
    assert rows["PROFILE"] == "in_progress"
    assert rows["NOTIF"] == "backlog"


# ══════════════════════════════════════════════════════════════════════════
# Test 3: 5 features V1, toutes dans legion.db → 0 importées
# ══════════════════════════════════════════════════════════════════════════

def test_import_all_existing(tmp_path):
    """All features already exist → 0 imported, all skipped."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")

    # Pre-insert all 5 features with the same status
    now = int(__import__("time").time())
    for f in SAMPLE_FEATURES:
        conn.execute(
            """INSERT OR IGNORE INTO features
               (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f["slug"], "myproject", f["prefix"], f["name"], "", f["status"], now, now),
        )
    conn.commit()
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, SAMPLE_FEATURES)

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    r = reports["myproject"]
    assert r.features_found == 5
    assert r.imported == 0, f"Expected 0 imported, got {r.imported}"
    assert r.skipped_existing == 5, f"Expected 5 skipped, got {r.skipped_existing}"
    assert r.divergent == []


# ══════════════════════════════════════════════════════════════════════════
# Test 4: 5 features V1, 3 existantes → 2 importées, 3 skipped
# ══════════════════════════════════════════════════════════════════════════

def test_import_partial(tmp_path):
    """3 of 5 features already exist → 2 imported."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")

    now = int(__import__("time").time())
    existing = ["AUTH", "PROFILE", "NOTIF"]
    for slug in existing:
        f = next(x for x in SAMPLE_FEATURES if x["slug"] == slug)
        conn.execute(
            """INSERT OR IGNORE INTO features
               (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, "myproject", f["prefix"], f["name"], "", f["status"], now, now),
        )
    conn.commit()
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, SAMPLE_FEATURES)

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    r = reports["myproject"]
    assert r.features_found == 5
    assert r.imported == 2, f"Expected 2 imported, got {r.imported}"
    assert r.skipped_existing == 3, f"Expected 3 skipped, got {r.skipped_existing}"

    # Verify only the 2 new ones were added
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute("SELECT slug FROM features WHERE project_slug='myproject'")
    slugs = {r["slug"] for r in cur.fetchall()}
    vconn.close()
    assert slugs == {"AUTH", "PROFILE", "NOTIF", "SEARCH", "ADMIN"}


# ══════════════════════════════════════════════════════════════════════════
# Test 5: Feature présente avec statut différent → divergent listée
# ══════════════════════════════════════════════════════════════════════════

def test_import_divergence(tmp_path):
    """Feature exists with different status → logged as divergent, NOT overwritten."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")

    now = int(__import__("time").time())
    # AUTH exists in legion.db as "backlog" but V1 says "done"
    conn.execute(
        """INSERT INTO features (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("AUTH", "myproject", "AUTH", "Authentication", "", "backlog", now, now),
    )
    conn.commit()
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, [
        {"slug": "AUTH", "prefix": "AUTH", "name": "Authentication", "status": "done"},
    ])

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    r = reports["myproject"]
    assert r.features_found == 1
    assert r.imported == 0
    assert r.skipped_existing == 1
    assert len(r.divergent) == 1, f"Expected 1 divergent, got {r.divergent}"
    div = r.divergent[0]
    assert div["slug"] == "AUTH"
    assert div["v1_status"] == "done"
    assert div["legion_status"] == "backlog"

    # Verify the status was NOT overwritten
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute("SELECT status FROM features WHERE slug='AUTH' AND project_slug='myproject'")
    status = cur.fetchone()["status"]
    vconn.close()
    assert status == "backlog", f"Expected 'backlog', got '{status}'"


# ══════════════════════════════════════════════════════════════════════════
# Test 6: Projet inexistant → skip, report.skipped_no_project = True
# ══════════════════════════════════════════════════════════════════════════

def test_import_skipped_project(tmp_path):
    """Project not in legion.db → skipped, skipped_no_project=True."""
    db_path = tmp_path / "legion.db"
    create_legion_db(str(db_path))  # DB exists but no project

    board_dir = make_boards_dir(tmp_path, "ghost-project")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, SAMPLE_FEATURES)

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    assert "ghost-project" in reports
    r = reports["ghost-project"]
    assert r.skipped_no_project is True
    assert r.features_found == 0  # never opened
    assert r.imported == 0


# ══════════════════════════════════════════════════════════════════════════
# Test 7: features.db corrompu → pas de plantage, report.skipped_corrupt
# ══════════════════════════════════════════════════════════════════════════

def test_import_corrupt_db(tmp_path):
    """Corrupted features.db → no crash, skipped_corrupt=True."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")

    # Write garbage instead of a valid SQLite file
    with open(features_db, "wb") as f:
        f.write(b"\x00\x01\x02\x03NOT A VALID SQLITE FILE\xff\xfe\xfd")

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    assert "myproject" in reports
    r = reports["myproject"]
    assert r.skipped_corrupt is True
    assert r.imported == 0

    # Verify legion.db was untouched
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute("SELECT COUNT(*) as c FROM features WHERE project_slug='myproject'")
    assert cur.fetchone()["c"] == 0
    vconn.close()


# ══════════════════════════════════════════════════════════════════════════
# Test 8: dry_run=True → ne modifie pas la DB
# ══════════════════════════════════════════════════════════════════════════

def test_import_dry_run(tmp_path):
    """dry_run=True → analysis only, DB unchanged."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, SAMPLE_FEATURES)

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
        dry_run=True,
    )

    assert "myproject" in reports
    r = reports["myproject"]
    assert r.features_found == 5
    assert r.imported == 5  # would import 5
    assert r.skipped_existing == 0

    # Verify DB unchanged — no features inserted
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute("SELECT COUNT(*) as c FROM features WHERE project_slug='myproject'")
    assert cur.fetchone()["c"] == 0
    vconn.close()


# ══════════════════════════════════════════════════════════════════════════
# Test 9: feature_meta importées pour les nouvelles features
# ══════════════════════════════════════════════════════════════════════════

def test_import_meta(tmp_path):
    """feature_meta rows are imported for new features."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(
        features_db,
        features=[{"slug": "AUTH", "prefix": "AUTH", "name": "Auth", "status": "done"}],
        meta={
            "AUTH": [
                {"key": "description", "value": "User authentication system"},
                {"key": "priority", "value": "high"},
            ],
        },
    )

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    r = reports["myproject"]
    assert r.imported == 1

    # Verify meta was imported
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute(
        "SELECT key, value FROM feature_meta WHERE feature_slug='AUTH' AND project_slug='myproject'"
    )
    meta = {r["key"]: r["value"] for r in cur.fetchall()}
    vconn.close()
    assert meta["description"] == "User authentication system"
    assert meta["priority"] == "high"


# ══════════════════════════════════════════════════════════════════════════
# Test 10: Meta fusion — clés existantes conservées, manquantes ajoutées
# ══════════════════════════════════════════════════════════════════════════

def test_import_meta_fusion(tmp_path):
    """Existing legion keys kept, missing V1 keys added."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")

    now = int(__import__("time").time())
    # Pre-insert AUTH feature with existing meta
    conn.execute(
        """INSERT INTO features (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("AUTH", "myproject", "AUTH", "Auth", "", "done", now, now),
    )
    conn.execute(
        "INSERT INTO feature_meta (feature_slug, project_slug, key, value) VALUES (?, ?, ?, ?)",
        ("AUTH", "myproject", "description", "Legion description (kept)"),
    )
    conn.commit()
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(
        features_db,
        features=[{"slug": "AUTH", "prefix": "AUTH", "name": "Auth", "status": "done"}],
        meta={
            "AUTH": [
                {"key": "description", "value": "V1 description"},
                {"key": "priority", "value": "high"},
            ],
        },
    )

    import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    # Verify fusion: existing key kept, new key added
    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute(
        "SELECT key, value FROM feature_meta WHERE feature_slug='AUTH' AND project_slug='myproject'"
    )
    meta = {r["key"]: r["value"] for r in cur.fetchall()}
    vconn.close()
    assert meta["description"] == "Legion description (kept)", "Existing key was overwritten!"
    assert meta["priority"] == "high", "Missing key from V1 was not added"


# ══════════════════════════════════════════════════════════════════════════
# Test 11: sync_status mis à jour après import
# ══════════════════════════════════════════════════════════════════════════

def test_import_sync_status(tmp_path):
    """sync_status table is upserted after a successful import."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "myproject")
    conn.close()

    board_dir = make_boards_dir(tmp_path, "myproject")
    features_db = os.path.join(board_dir, "features.db")
    create_features_db(features_db, SAMPLE_FEATURES)

    import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    vconn = sqlite3.connect(str(db_path))
    vconn.row_factory = sqlite3.Row
    cur = vconn.execute("SELECT * FROM sync_status WHERE project_slug='myproject'")
    row = cur.fetchone()
    vconn.close()

    assert row is not None, "sync_status row not found"
    assert row["status"] == "ok"
    assert "done" in row["message"].lower()


# ══════════════════════════════════════════════════════════════════════════
# Test 12: Rapport retourné a la bonne structure (dict[str, ImportReport])
# ══════════════════════════════════════════════════════════════════════════

def test_import_report_structure(tmp_path):
    """The returned dict has the correct structure: dict[str, ImportReport]."""
    db_path = tmp_path / "legion.db"
    conn = create_legion_db(str(db_path))
    add_project_to_legion(conn, "alpha")
    add_project_to_legion(conn, "beta")
    conn.close()

    for slug in ("alpha", "beta"):
        board_dir = make_boards_dir(tmp_path, slug)
        features_db = os.path.join(board_dir, "features.db")
        create_features_db(features_db, [
            {"slug": f"{slug.upper()}_F1", "prefix": "F1", "name": f"Feature 1 in {slug}"},
        ])

    reports = import_v1_features(
        db_path=str(db_path),
        kanban_boards_path=str(tmp_path / "kanban-boards"),
    )

    # Type check
    assert isinstance(reports, dict)
    assert len(reports) == 2

    for slug in ("alpha", "beta"):
        assert slug in reports
        r = reports[slug]
        assert isinstance(r, ImportReport) or isinstance(r, dict)
        if isinstance(r, ImportReport):
            assert isinstance(r.project_slug, str)
            assert isinstance(r.features_found, int)
            assert isinstance(r.imported, int)
            assert isinstance(r.skipped_existing, int)
            assert isinstance(r.divergent, list)
            assert isinstance(r.skipped_no_project, bool)
            assert isinstance(r.skipped_corrupt, bool)
            assert isinstance(r.errors, list)

    # Verify counts
    assert reports["alpha"].imported == 1
    assert reports["beta"].imported == 1
