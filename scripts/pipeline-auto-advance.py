#!/usr/bin/env python3
"""
Pipeline Auto-Advance — détecte les features dont la carte du stage actuel
est 'done' et lance le pipeline centralisé pour créer la carte du stage suivant.

Usage:
    python3 ~/.legion/scripts/pipeline-auto-advance.py [--dry-run]

Cron recommandé : toutes les 5 minutes
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

LEGION_DB = Path.home() / ".legion" / "db" / "legion.db"
PIPELINE_SCRIPT = Path.home() / ".legion" / "core" / "pipeline.py"
KANBAN_BASE = Path.home() / ".hermes" / "kanban" / "boards"

STAGE_ORDER = ["EXPLORE", "SPEC", "DESIGN", "ARCHITECT", "IMPLEMENT"]
PIPELINE_LABEL = {s: f"[{s}]" for s in STAGE_ORDER}


def get_active_projects():
    """Return active or draft project slugs (draft counts for legion-v2 style projects)."""
    conn = sqlite3.connect(str(LEGION_DB))
    rows = conn.execute("SELECT slug, board FROM projects WHERE status IN ('active', 'draft')").fetchall()
    conn.close()
    return rows


def get_features(project_slug):
    """Return features for a project: (slug, prefix, name)"""
    conn = sqlite3.connect(str(LEGION_DB))
    rows = conn.execute(
        "SELECT slug, prefix, name FROM features WHERE project_slug=?", (project_slug,)
    ).fetchall()
    conn.close()
    return rows


def get_pipeline_stage(board, prefix):
    """Read pipeline_stage from kanban features.db."""
    feat_db = KANBAN_BASE / board / "features.db"
    if not feat_db.exists():
        return None
    try:
        conn = sqlite3.connect(str(feat_db))
        row = conn.execute(
            "SELECT value FROM feature_meta WHERE slug=? AND key='pipeline_stage'",
            (prefix,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def find_done_card(board, stage, feat_name="", feat_slug=""):
    """Check if the latest card with [STAGE] prefix for this board is 'done'
    and belongs to the given feature (by name or slug words)."""
    kanban_db = KANBAN_BASE / board / "kanban.db"
    if not kanban_db.exists():
        return None
    try:
        conn = sqlite3.connect(str(kanban_db))
        rows = conn.execute(
            """SELECT id, title FROM tasks
               WHERE title LIKE ? AND status='done'
               ORDER BY completed_at DESC LIMIT 20""",
            (f"%[{stage}] %",),
        ).fetchall()
        conn.close()
        if not rows:
            return None
        # Filter by feature name/slug
        keywords = set()
        if feat_name:
            for w in feat_name.lower().split():
                if len(w) > 3:
                    keywords.add(w)
        for w in feat_slug.replace("-", " ").lower().split():
            if len(w) > 3:
                keywords.add(w)
        if not keywords:
            return rows[0] if rows else None
        for cid, title in rows:
            title_lower = title.lower()
            if any(kw in title_lower for kw in keywords):
                return (cid, title)
        return None
    except Exception:
        return None


def run_pipeline(project_slug, prefix, dry_run=False):
    """Run the centralized pipeline for a feature."""
    if dry_run:
        print(f"  [DRY-RUN] python3 {PIPELINE_SCRIPT} {project_slug} {prefix}")
        return True
    try:
        result = subprocess.run(
            [sys.executable, str(PIPELINE_SCRIPT), project_slug, prefix],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            print(f"  ✅ {project_slug} {prefix} — OK")
            return True
        else:
            print(f"  ⚠️  {project_slug} {prefix} — exit={result.returncode}: {output[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  {project_slug} {prefix} — timeout")
        return False
    except Exception as e:
        print(f"  ❌ {project_slug} {prefix} — {e}")
        return False


def main():
    dry_run = "--dry-run" in sys.argv
    now = int(time.time())
    advanced = 0

    print(f"Pipeline Auto-Advance — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Dry-run: {'OUI' if dry_run else 'NON'}")
    print()

    projects = get_active_projects()
    if not projects:
        print("Aucun projet actif.")
        return 0

    for slug, board in projects:
        print(f"--- {slug} (board: {board}) ---")
        features = get_features(slug)
        if not features:
            print("  Aucune feature.")
            continue

        for feat_slug, prefix, name in features:
            stage = get_pipeline_stage(board, prefix)
            if not stage or stage == "done" or stage not in STAGE_ORDER:
                continue

            card = find_done_card(board, stage, feat_name=name, feat_slug=feat_slug)
            if not card:
                continue

            card_id, card_title = card
            print(f"  {prefix} ({name}):")
            print(f"    Stage actuel: {stage}")
            print(f"    Carte done: {card_id} — {card_title}")
            print(f"    → Lancement du pipeline...")
            ok = run_pipeline(slug, prefix, dry_run=dry_run)
            if ok:
                advanced += 1
            print()

    print(f"--- Terminé: {advanced} feature(s) avancée(s) ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
