"""
Legion — Database module
Central SQLite database for projects, features, and metadata.

Location: ~/.legion/db/legion.db
"""

import sqlite3
import os
import json
from datetime import datetime
from typing import Optional

LEGION_HOME = os.path.expanduser("~/.legion")
DB_PATH = os.path.join(LEGION_HOME, "db", "legion.db")

SCHEMA_SQL = """
-- ═══════════════════════════════════════════════════════════════
-- Legion — Core Database Schema
-- ═══════════════════════════════════════════════════════════════

-- 1. Projects (replaces pipeline-projects.yaml)
CREATE TABLE IF NOT EXISTS projects (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    project_type    TEXT DEFAULT 'custom',
    work_dir        TEXT NOT NULL,
    board           TEXT NOT NULL,
    extra_skills    TEXT DEFAULT '[]',         -- JSON array
    docs_structure  TEXT DEFAULT 'product/',
    pipeline_config TEXT DEFAULT '{}',         -- JSON: stage_order, profiles, body_templates, doc_patterns
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- 2. Project profiles (one-to-many)
CREATE TABLE IF NOT EXISTS project_profiles (
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    role            TEXT NOT NULL,             -- 'architect', 'backend', etc.
    profile_name    TEXT NOT NULL,             -- 'skull-game-architect'
    PRIMARY KEY (project_slug, role)
);

-- 3. Project conventions (docs paths per stage)
CREATE TABLE IF NOT EXISTS project_conventions (
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    stage           TEXT NOT NULL,             -- 'explore', 'spec', 'design', 'architect'
    doc_path        TEXT NOT NULL,             -- 'docs/product/exploration-{slug}.md'
    PRIMARY KEY (project_slug, stage)
);

-- 4. Features (previously per-project features.db)
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

-- 5. Feature metadata (extensible key-value)
CREATE TABLE IF NOT EXISTS feature_meta (
    feature_slug    TEXT NOT NULL,
    project_slug    TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    PRIMARY KEY (feature_slug, project_slug, key),
    FOREIGN KEY (feature_slug, project_slug) REFERENCES features(slug, project_slug) ON DELETE CASCADE
);

-- 6. Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

-- 7. Skill bundles (wraps hermes bundles)
CREATE TABLE IF NOT EXISTS bundles (
    name            TEXT PRIMARY KEY,
    description     TEXT DEFAULT '',
    project_slug    TEXT REFERENCES projects(slug) ON DELETE SET NULL,
    instruction     TEXT DEFAULT '',
    skills          TEXT NOT NULL DEFAULT '[]',  -- JSON array of skill names
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- 8. Profile templates (builder for Hermes profiles)
CREATE TABLE IF NOT EXISTS profile_templates (
    name            TEXT NOT NULL,
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    bundle_name     TEXT REFERENCES bundles(name) ON DELETE SET NULL,
    role            TEXT DEFAULT '',              -- 'product', 'design', 'architect', etc.
    channel_id      TEXT DEFAULT '',              -- Discord channel ID
    instruction     TEXT DEFAULT '',              -- Extra system prompt / SOUL.md content
    model           TEXT DEFAULT '',              -- Model override
    provider        TEXT DEFAULT '',              -- Provider override
    is_active       INTEGER DEFAULT 0,            -- 1 if profile dir exists
    is_system       INTEGER DEFAULT 0,            -- 1 if system default (product, design...)
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (name, project_slug)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_features_project ON features(project_slug);
CREATE INDEX IF NOT EXISTS idx_features_prefix ON features(prefix);
CREATE INDEX IF NOT EXISTS idx_project_profiles_project ON project_profiles(project_slug);
CREATE INDEX IF NOT EXISTS idx_feature_meta_lookup ON feature_meta(feature_slug, project_slug);
CREATE INDEX IF NOT EXISTS idx_bundles_project ON bundles(project_slug);
CREATE INDEX IF NOT EXISTS idx_profile_templates_project ON profile_templates(project_slug);
"""


def get_conn() -> sqlite3.Connection:
    """Get a connection to the legion database, creating it if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize the database schema."""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA_SQL)
        # Check current version
        cur = conn.execute("SELECT MAX(version) as v FROM schema_version")
        row = cur.fetchone()
        current_version = row["v"] if row and row["v"] else 0

        if current_version < 1:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, int(datetime.now().timestamp())),
            )
            current_version = 1

        if current_version < 2:
            # Add pipeline_config column to existing tables
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN pipeline_config TEXT DEFAULT '{}'")
            except sqlite3.OperationalError:
                pass  # already exists
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (2, int(datetime.now().timestamp())),
            )
            conn.commit()
            return True

        if current_version < 3:
            # Tables created by SCHEMA_SQL for v3 (bundles, profile_templates)
            # Just mark the version
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (3, int(datetime.now().timestamp())),
            )
            conn.commit()
            return True

        return False
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Projects CRUD
# ═══════════════════════════════════════════════════════════════

def add_project(
    slug: str,
    name: str,
    work_dir: str,
    board: str,
    project_type: str = "custom",
    extra_skills: Optional[list] = None,
    docs_structure: str = "product/",
) -> dict:
    """Add a project to the database. Returns the project dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO projects
               (slug, name, project_type, work_dir, board, extra_skills, docs_structure, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM projects WHERE slug=?), ?), ?)""",
            (slug, name, project_type, work_dir, board,
             json.dumps(extra_skills or []), docs_structure,
             slug, now, now),
        )
        conn.commit()
        return get_project(slug)
    finally:
        conn.close()


def get_project(slug: str) -> Optional[dict]:
    """Get a project by slug. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,))
        row = cur.fetchone()
        if not row:
            return None
        proj = dict(row)
        proj["extra_skills"] = json.loads(proj.get("extra_skills", "[]"))
        proj["pipeline_config"] = json.loads(proj.get("pipeline_config", "{}"))

        # Fetch profiles
        cur2 = conn.execute(
            "SELECT role, profile_name FROM project_profiles WHERE project_slug=?",
            (slug,),
        )
        proj["profiles"] = {r["role"]: r["profile_name"] for r in cur2.fetchall()}

        # Fetch conventions
        cur3 = conn.execute(
            "SELECT stage, doc_path FROM project_conventions WHERE project_slug=?",
            (slug,),
        )
        proj["conventions"] = {r["stage"]: r["doc_path"] for r in cur3.fetchall()}

        return proj
    finally:
        conn.close()


def list_projects() -> list[dict]:
    """List all projects."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT slug, name, project_type, work_dir, board FROM projects ORDER BY slug")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def delete_project(slug: str) -> bool:
    """Delete a project. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM projects WHERE slug=?", (slug,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_project_pipeline_config(slug: str, config: dict) -> dict:
    """Set pipeline configuration for a project. Returns updated project."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET pipeline_config=?, updated_at=? WHERE slug=?",
            (json.dumps(config), int(datetime.now().timestamp()), slug),
        )
        conn.commit()
        return get_project(slug)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Features CRUD
# ═══════════════════════════════════════════════════════════════

def add_feature(
    slug: str,
    project_slug: str,
    prefix: str,
    name: str,
    domaine: str = "",
    status: str = "backlog",
) -> dict:
    """Add a feature to the database. Returns the feature dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO features
               (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM features WHERE slug=? AND project_slug=?), ?), ?)""",
            (slug, project_slug, prefix, name, domaine, status,
             slug, project_slug, now, now),
        )
        conn.commit()
        return get_feature(slug, project_slug)
    finally:
        conn.close()


def get_feature(slug: str, project_slug: str) -> Optional[dict]:
    """Get a feature by slug + project. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM features WHERE slug=? AND project_slug=?",
            (slug, project_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
        feat = dict(row)

        # Fetch metadata
        cur2 = conn.execute(
            "SELECT key, value FROM feature_meta WHERE feature_slug=? AND project_slug=?",
            (slug, project_slug),
        )
        feat["meta"] = {r["key"]: r["value"] for r in cur2.fetchall()}
        return feat
    finally:
        conn.close()


def get_feature_by_prefix(prefix: str, project_slug: str) -> Optional[dict]:
    """Get a feature by prefix within a project."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM features WHERE prefix=? AND project_slug=?",
            (prefix, project_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
        return get_feature(row["slug"], project_slug)
    finally:
        conn.close()


def list_features(project_slug: Optional[str] = None) -> list[dict]:
    """List features, optionally filtered by project."""
    conn = get_conn()
    try:
        if project_slug:
            cur = conn.execute(
                "SELECT * FROM features WHERE project_slug=? ORDER BY slug",
                (project_slug,),
            )
        else:
            cur = conn.execute("SELECT * FROM features ORDER BY project_slug, slug")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def set_feature_meta(feature_slug: str, project_slug: str, key: str, value: str):
    """Set a metadata key on a feature."""
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO feature_meta (feature_slug, project_slug, key, value)
               VALUES (?, ?, ?, ?)""",
            (feature_slug, project_slug, key, value),
        )
        conn.commit()
    finally:
        conn.close()


def delete_feature(slug: str, project_slug: str) -> bool:
    """Delete a feature."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM features WHERE slug=? AND project_slug=?",
            (slug, project_slug),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Migration helpers
# ═══════════════════════════════════════════════════════════════

def import_from_pipeline_yaml(yaml_path: str) -> list[str]:
    """Import projects from the old pipeline-projects.yaml.
    Returns list of imported project slugs."""
    import yaml

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    imported = []
    for slug, info in data.get("projects", {}).items():
        add_project(
            slug=slug,
            name=info.get("label", slug),
            work_dir=os.path.expanduser(info.get("repo", f"~/projects/{slug}")),
            board=info.get("kanban_board", slug),
            project_type=info.get("project_type", "custom"),
            extra_skills=info.get("extra_skills", []),
            docs_structure=info.get("docs_root", "docs/") + info.get("project_type", "product"),
        )

        # Import profiles
        conn = get_conn()
        try:
            for role, profile_name in info.get("profiles", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO project_profiles (project_slug, role, profile_name) VALUES (?, ?, ?)",
                    (slug, role, profile_name),
                )

            # Import conventions
            for stage, doc_path in info.get("conventions", {}).get("docs", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO project_conventions (project_slug, stage, doc_path) VALUES (?, ?, ?)",
                    (slug, stage, doc_path),
                )
            conn.commit()
        finally:
            conn.close()

        imported.append(slug)

    return imported


def import_features_from_db(project_slug: str, features_db_path: str) -> int:
    """Import features from a legacy per-project features.db.
    Returns count of imported features."""
    if not os.path.exists(features_db_path):
        return 0

    old_conn = sqlite3.connect(features_db_path)
    old_conn.row_factory = sqlite3.Row
    new_conn = get_conn()

    try:
        cur = old_conn.execute("SELECT * FROM features")
        count = 0
        for row in cur.fetchall():
            feat = dict(row)
            add_feature(
                slug=feat["slug"],
                project_slug=project_slug,
                prefix=feat["prefix"],
                name=feat["name"],
                domaine=feat.get("domaine", ""),
                status=feat.get("status", "backlog"),
            )

            # Import meta
            meta_cur = old_conn.execute(
                "SELECT key, value FROM feature_meta WHERE slug=?",
                (feat["slug"],),
            )
            for m in meta_cur.fetchall():
                set_feature_meta(feat["slug"], project_slug, m["key"], m["value"])

            count += 1

        return count
    finally:
        old_conn.close()
        new_conn.close()


def ensure_indexes():
    """Create indexes if they don't exist."""
    conn = get_conn()
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_features_project ON features(project_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_features_prefix ON features(prefix)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_profiles_project ON project_profiles(project_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_meta_lookup ON feature_meta(feature_slug, project_slug)")
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Bundles CRUD
# ═══════════════════════════════════════════════════════════════

def add_bundle(
    name: str,
    skills: list,
    description: str = "",
    project_slug: str = None,
    instruction: str = "",
) -> dict:
    """Add a bundle. Returns the bundle dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO bundles
               (name, description, project_slug, instruction, skills, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM bundles WHERE name=?), ?), ?)""",
            (name, description, project_slug, instruction, json.dumps(skills),
             name, now, now),
        )
        conn.commit()
        return get_bundle(name)
    finally:
        conn.close()


def get_bundle(name: str) -> Optional[dict]:
    """Get a bundle by name. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM bundles WHERE name=?", (name,))
        row = cur.fetchone()
        if not row:
            return None
        b = dict(row)
        b["skills"] = json.loads(b.get("skills", "[]"))
        return b
    finally:
        conn.close()


def list_bundles(project_slug: str = None) -> list[dict]:
    """List bundles, optionally filtered by project."""
    conn = get_conn()
    try:
        if project_slug:
            cur = conn.execute(
                "SELECT * FROM bundles WHERE project_slug=? OR project_slug IS NULL ORDER BY name",
                (project_slug,),
            )
        else:
            cur = conn.execute("SELECT * FROM bundles ORDER BY name")
        bundles = [dict(r) for r in cur.fetchall()]
        for b in bundles:
            b["skills"] = json.loads(b.get("skills", "[]"))
        return bundles
    finally:
        conn.close()


def delete_bundle(name: str) -> bool:
    """Delete a bundle. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM bundles WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Profile Templates CRUD
# ═══════════════════════════════════════════════════════════════

def add_profile_template(
    name: str,
    project_slug: str,
    bundle_name: str = None,
    role: str = "",
    channel_id: str = "",
    instruction: str = "",
    model: str = "",
    provider: str = "",
    is_system: bool = False,
) -> dict:
    """Add a profile template. Returns the profile dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO profile_templates
               (name, project_slug, bundle_name, role, channel_id, instruction,
                model, provider, is_active, is_system, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, COALESCE((SELECT created_at FROM profile_templates WHERE name=? AND project_slug=?), ?), ?)""",
            (name, project_slug, bundle_name, role, channel_id, instruction,
             model, provider, 1 if is_system else 0,
             name, project_slug, now, now),
        )
        conn.commit()
        return get_profile_template(name, project_slug)
    finally:
        conn.close()


def get_profile_template(name: str, project_slug: str) -> Optional[dict]:
    """Get a profile template by name + project. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM profile_templates WHERE name=? AND project_slug=?",
            (name, project_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def list_profile_templates(project_slug: str = None) -> list[dict]:
    """List profile templates, optionally filtered by project."""
    conn = get_conn()
    try:
        if project_slug:
            cur = conn.execute(
                "SELECT * FROM profile_templates WHERE project_slug=? ORDER BY is_system DESC, name",
                (project_slug,),
            )
        else:
            cur = conn.execute("SELECT * FROM profile_templates ORDER BY project_slug, name")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def update_profile_active(name: str, project_slug: str, is_active: bool) -> bool:
    """Set the active flag on a profile template."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE profile_templates SET is_active=?, updated_at=? WHERE name=? AND project_slug=?",
            (1 if is_active else 0, int(datetime.now().timestamp()), name, project_slug),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_profile_template(name: str, project_slug: str) -> bool:
    """Delete a profile template. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM profile_templates WHERE name=? AND project_slug=?",
            (name, project_slug),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_system_profiles(project_slug: str):
    """Seed default system profiles for a project if they don't exist."""
    defaults = [
        {"name": f"product", "role": "product", "is_system": True,
         "instruction": "Product Explorer — exploration et specs produit"},
        {"name": f"design", "role": "design", "is_system": True,
         "instruction": "UI/UX Designer — maquettes Stitch uniquement"},
        {"name": f"architect", "role": "architect", "is_system": True,
         "instruction": "Architecte — décomposition technique et tickets IMPLEMENT"},
        {"name": f"backend", "role": "backend", "is_system": True,
         "instruction": "Backend Dev — Supabase Cloud, SQL, Edge Functions"},
        {"name": f"frontend", "role": "frontend", "is_system": True,
         "instruction": "Frontend Dev — Expo, React Native, TypeScript"},
        {"name": f"master-agent", "role": "master-agent", "is_system": True,
         "instruction": "Master Agent — coordination, bugs, triage"},
    ]
    for d in defaults:
        existing = get_profile_template(d["name"], project_slug)
        if not existing:
            add_profile_template(
                name=d["name"],
                project_slug=project_slug,
                role=d["role"],
                instruction=d["instruction"],
                is_system=d["is_system"],
            )
