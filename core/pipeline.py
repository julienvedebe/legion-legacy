#!/usr/bin/env python3
"""
Legion — Centralized Pipeline Engine

Usage:
    python3 -m core.pipeline <project_slug> <feature_prefix>
    python3 -m core.pipeline <project_slug> <feature_prefix> --reset

Reads project config from Legion DB (stage_order, profiles, doc_patterns, body_templates)
and manages the Kanban pipeline: create cards, advance stages, track progress.

Supports:
    - Multi-project (reads config from legion.db)
    - Configurable stage order, profiles, doc patterns
    - auto-commit body templates with {slug}, {prefix}, {name}, {work_dir}
    - Pipeline stage tracking via Kanban features.db (backward compatible)
    - Reset mode to restart a feature's pipeline
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Add ~/.legion to path ──
sys.path.insert(0, str(Path.home() / ".legion"))
from core.db import get_project, list_features, get_feature_by_prefix

HERMES_HOME = Path.home() / ".hermes"

# ── Defaults (used if project has no pipeline_config) ──
DEFAULT_STAGE_ORDER = ["EXPLORE", "SPEC", "DESIGN", "ARCHITECT", "IMPLEMENT"]
DEFAULT_STAGE_DOC_PATTERNS = {
    "EXPLORE": "docs/product/exploration-{slug}.md",
    "SPEC": "docs/product/fonctionnalite-{slug}.md",
    "DESIGN": "docs/design/design-{slug}.md",
    "ARCHITECT": "docs/architecture/archi-{slug}.md",
}
DEFAULT_STAGE_PROFILES = {}


def stage_label(stage: str) -> str:
    labels = {
        "EXPLORE": "Exploration", "SPEC": "Spécification",
        "DESIGN": "Design", "ARCHITECT": "Architecture",
        "IMPLEMENT": "Implémentation", "TEST": "Test",
    }
    return labels.get(stage, stage)


# ── Kanban features.db helpers ──

def _features_db_path(board: str) -> Path:
    return HERMES_HOME / "kanban" / "boards" / board / "features.db"


def _ensure_features_db(board: str):
    """Create features.db schema if tables don't exist."""
    db = _features_db_path(board)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS features (
                slug TEXT PRIMARY KEY,
                prefix TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'backlog'
            );
            CREATE TABLE IF NOT EXISTS feature_meta (
                slug TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (slug, key)
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _kanban_db_path(board: str) -> Path:
    return HERMES_HOME / "kanban" / "boards" / board / "kanban.db"


def get_pipeline_stage(board: str, prefix: str) -> str | None:
    """Read pipeline_stage from Kanban features.db. None = never run."""
    db = _features_db_path(board)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(str(db))
        c = conn.cursor()
        c.execute(
            "SELECT value FROM feature_meta WHERE slug=? AND key='pipeline_stage'",
            (prefix,),
        )
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def set_pipeline_stage(board: str, prefix: str, stage: str):
    """Write pipeline_stage to Kanban features.db."""
    db = _features_db_path(board)
    conn = sqlite3.connect(str(db))
    try:
        c = conn.cursor()
        c.execute(
            "SELECT 1 FROM feature_meta WHERE slug=? AND key='pipeline_stage'",
            (prefix,),
        )
        if c.fetchone():
            c.execute(
                "UPDATE feature_meta SET value=? WHERE slug=? AND key='pipeline_stage'",
                (stage, prefix),
            )
        else:
            c.execute(
                "INSERT INTO feature_meta (slug, key, value) VALUES (?, 'pipeline_stage', ?)",
                (prefix, stage),
            )
        conn.commit()
    finally:
        conn.close()


# ── Feature lookup ──

def find_feature(project_slug: str, prefix: str) -> tuple[str | None, str | None]:
    """Find feature by prefix. Returns (slug, name) or (None, None)."""
    features = list_features(project_slug)
    for f in features:
        if f["prefix"] == prefix:
            return f["slug"], f["name"]
    return None, None


# ── Doc detection ──

def find_docs(work_dir: str, slug: str, doc_patterns: dict) -> dict:
    """Scan docs/ for existing documents matching a feature slug."""
    found = {}
    work_path = Path(work_dir)
    for stage, pattern in doc_patterns.items():
        # Replace {slug} with actual slug
        actual_pattern = pattern.replace("{slug}", slug)
        matched = []
        for f in sorted(work_path.glob(actual_pattern)):
            name = f.stem.lower()
            slug_parts = set(slug.replace("-", "_").split("_"))
            name_parts = set(name.replace("-", "_").split("_"))
            common = slug_parts & name_parts
            if slug in name or len(common) >= 2:
                matched.append(f.name)
        found[stage] = matched
    return found


def detect_initial_stage(work_dir: str, slug: str, stage_order: list, doc_patterns: dict) -> str:
    """Detect the first stage that's missing a doc."""
    docs = find_docs(work_dir, slug, doc_patterns)
    for stage in stage_order:
        if stage in doc_patterns:
            if not docs.get(stage):
                return stage
    # All docs exist → start at the first actionable stage after docs
    return stage_order[-2] if len(stage_order) >= 2 else stage_order[0]


# ── Stage card lookup ──

def find_stage_cards(cursor, stage: str, prefix: str, slug: str):
    """Find existing Kanban cards for a given stage + feature."""
    for pat in [
        f"%[{stage}]%{prefix}%",
        f"%{stage}%{prefix}%",
        f"%{stage}%{slug.replace('-', ' ')}%",
    ]:
        cursor.execute(
            """SELECT id, title, status FROM tasks
               WHERE title LIKE ? AND status NOT IN ('archived')
               ORDER BY created_at DESC LIMIT 3""",
            (pat,),
        )
        cards = cursor.fetchall()
        if cards:
            return cards
    # Fallback: match on individual slug parts
    parts = slug.replace("-", " ").split()
    for part in parts[:3]:
        if len(part) > 3:
            cursor.execute(
                """SELECT id, title, status FROM tasks
                   WHERE title LIKE ? AND title LIKE ?
                   AND status NOT IN ('archived')
                   ORDER BY created_at DESC LIMIT 3""",
                (f"%{stage}%", f"%{part}%"),
            )
            cards = cursor.fetchall()
            if cards:
                return cards
    return []


def find_last_done_card(cursor, prefix: str, slug: str):
    """Find the last done card for this feature (used as parent)."""
    patterns = [
        f"%[{prefix}]%",
        f"%IMP-{prefix}%",
        f"%{prefix}-%",
        f"%{prefix}:%",
    ]
    for pat in patterns:
        cursor.execute(
            """SELECT id FROM tasks WHERE status='done' AND title LIKE ?
               ORDER BY completed_at DESC LIMIT 1""",
            (pat,),
        )
        r = cursor.fetchone()
        if r:
            return r[0]
    parts = slug.replace("-", " ").split()
    for part in parts:
        if len(part) > 3:
            cursor.execute(
                """SELECT id FROM tasks WHERE status='done' AND title LIKE ?
                   ORDER BY completed_at DESC LIMIT 1""",
                (f"%{part}%",),
            )
            r = cursor.fetchone()
            if r:
                return r[0]
    return None


# ── Card creation ──

def create_card(title: str, profile: str, parent_id: str | None, board: str, body: str | None = None) -> tuple[str, str, int]:
    """Create a Kanban card via `hermes kanban create`."""
    cmd = f"HERMES_KANBAN_BOARD={board} hermes kanban create --assignee {profile} --initial-status running"
    if body:
        escaped = body.replace('"', '\\"').replace("\n", "\\n")
        cmd += f' --body "{escaped}"'
    cmd += f' "{title}"'
    if parent_id:
        cmd += f" --parent {parent_id}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def render_body(template: str | None, **kwargs) -> str | None:
    """Render body template with {slug}, {prefix}, {name}, {work_dir}."""
    if not template:
        return None
    return template.format(**kwargs)


# ── Advance stage ──

def advance_stage(board: str, current_stage: str, prefix: str, slug: str, name: str, stage_order: list):
    """Advance to the next stage or mark done."""
    idx = stage_order.index(current_stage) + 1
    if idx >= len(stage_order):
        set_pipeline_stage(board, prefix, "done")
        print(f"  ✅ Pipeline terminée pour {name} !")
        print(f"     Marqué comme done dans feature_meta.")
    else:
        next_stage = stage_order[idx]
        set_pipeline_stage(board, prefix, next_stage)
        print(f"  ▶ Prochain stage: {stage_label(next_stage)}")
        print(f"     Lance 'legion pipeline <project> {prefix}' pour continuer.")


# ── Reset ──

def reset_pipeline(board: str, prefix: str, stage_order: list):
    """Reset pipeline to the first stage."""
    set_pipeline_stage(board, prefix, stage_order[0])
    print(f"  🔄 Pipeline réinitialisée au stage {stage_label(stage_order[0])}")


# ═══════════════════════════════════════════════════════════════
# Main run
# ═══════════════════════════════════════════════════════════════

def run_pipeline(project_slug: str, prefix: str, reset: bool = False) -> int:
    """Run the pipeline for a feature. Returns exit code."""
    prefix = prefix.upper()

    # 1. Load project
    proj = get_project(project_slug)
    if not proj:
        print(f"❌ Projet '{project_slug}' introuvable.")
        return 1

    work_dir = proj["work_dir"]
    board = proj["board"]
    cfg = proj.get("pipeline_config", {})

    stage_order = cfg.get("stage_order", DEFAULT_STAGE_ORDER)
    doc_patterns = cfg.get("stage_doc_patterns", DEFAULT_STAGE_DOC_PATTERNS)
    stage_profiles = cfg.get("stage_profiles", DEFAULT_STAGE_PROFILES)
    body_templates = cfg.get("body_templates", {})

    # 2. Find feature
    slug, name = find_feature(project_slug, prefix)
    if not slug:
        print(f"❌ Préfixe inconnu: {prefix}")
        print("   Utilise 'legion features' pour voir la liste")
        return 1

    # 3. Check board exists
    kanban_db = _kanban_db_path(board)
    if not kanban_db.exists():
        print(f"❌ Board introuvable: {kanban_db}")
        return 1

    # 4. Reset mode
    if reset:
        reset_pipeline(board, prefix, stage_order)
        return 0

    # 5. Ensure features.db exists
    _ensure_features_db(board)

    # 6. Read current stage
    stage = get_pipeline_stage(board, prefix)
    conn = sqlite3.connect(str(kanban_db))
    c = conn.cursor()
    now = int(time.time())

    print(f"\n{'=' * 60}")
    print(f"  Pipeline — {prefix} ({name})")
    print(f"  Projet: {project_slug}  |  Board: {board}")
    print(f"{'=' * 60}\n")

    if stage is None:
        # First run — detect initial stage from docs
        stage = detect_initial_stage(work_dir, slug, stage_order, doc_patterns)
        set_pipeline_stage(board, prefix, stage)
        print(f"  ▶ [{1}/{len(stage_order)}] {stage_label(stage)}")
        print(f"  ▶ Première exécution — initialisation au stade {stage_label(stage)}\n")
    elif stage == "done":
        print(f"  ✅ Feature {prefix} déjà terminée — pipeline_stage=done")
        print("  Utilise 'legion pipeline <project> <prefix> --reset' pour relancer")
        conn.close()
        return 0
    else:
        idx = stage_order.index(stage) + 1 if stage in stage_order else 1
        print(f"  ▶ [{idx}/{len(stage_order)}] {stage_label(stage)}")
        print(f"  ▶ Stade actuel (prochaine carte à créer)\n")

    # 6. Check if a card for this stage already exists
    found_cards = find_stage_cards(c, stage, prefix, slug)
    if found_cards:
        card_id, card_title, card_status = found_cards[0]
        if card_status == "todo":
            c.execute("UPDATE tasks SET status='ready' WHERE id=?", (card_id,))
            conn.commit()
            print(f"✅ Carte {stage_label(stage)} déjà existante — promue en ready")
            print(f"   {card_title}")
            conn.close()
            return 0
        elif card_status in ("ready", "running", "in_progress"):
            print(f"ℹ️  Carte {stage_label(stage)} déjà en cours ({card_status})")
            print(f"   {card_title}")
            conn.close()
            return 0
        elif card_status == "done":
            print(f"ℹ️  Carte {stage_label(stage)} déjà faite — advancement automatique")
            advance_stage(board, stage, prefix, slug, name, stage_order)
            conn.close()
            return 0
        else:
            print(f"ℹ️  Carte en statut {card_status} — rien à faire")
            conn.close()
            return 0

    # 7. Find parent card
    parent_id = find_last_done_card(c, prefix, slug)
    if parent_id:
        print(f"   Parent: {parent_id}")

    # 8. Create the card
    profile = stage_profiles.get(stage, "default")
    body = render_body(
        body_templates.get(stage),
        slug=slug, prefix=prefix, name=name, work_dir=work_dir,
    )

    title = f"[{stage}] {name}"
    stdout, stderr, rc = create_card(title, profile, parent_id, board, body=body)

    if rc == 0:
        match = re.search(r"(t_[a-z0-9]+)", stdout)
        card_id = match.group(1) if match else "?"
        if card_id and card_id != "?":
            c.execute("UPDATE tasks SET status='ready' WHERE id=?", (card_id,))
            c.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'status', ?, ?)",
                (card_id, json.dumps({"status": "ready", "by": "user:pipeline-auto"}), now),
            )
            conn.commit()
        print(f"\n✅ Carte « {title} » créée (ID: {card_id}) — en ready")
    else:
        print(f"\n❌ Erreur création: {stderr or stdout}")
        conn.close()
        return 1

    # 9. Advance stage
    advance_stage(board, stage, prefix, slug, name, stage_order)

    conn.commit()
    print(f"\n   Le dispatcher va la picker dans ~60s")
    conn.close()
    return 0


# ═══════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Legion — Pipeline Engine")
    parser.add_argument("project", help="Slug du projet (ex: skull-game)")
    parser.add_argument("prefix", help="Préfixe de la feature (ex: AUTH)")
    parser.add_argument("--reset", action="store_true", help="Réinitialiser la pipeline")
    args = parser.parse_args()

    sys.exit(run_pipeline(args.project, args.prefix.upper(), reset=args.reset))


if __name__ == "__main__":
    main()
