#!/usr/bin/env python3
"""
Pipeline Auto-Advance — Surveille TOUS les projets actifs et enchaîne
automatiquement le pipeline quand une carte du stage courant est complétée.

Usage:
    python3 pipeline-auto-advance.py
    python3 pipeline-auto-advance.py --project legion-v2   # Un seul projet
    python3 pipeline-auto-advance.py --dry-run              # Simulation

Sécurité: ne crée des cartes que pour les features qui ont un pipeline_stage
existant dans feature_meta (pas de création sauvage).
"""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
LEGION_HOME = Path.home() / ".legion"
DEFAULT_STAGE_ORDER = ["EXPLORE", "SPEC", "DESIGN", "ARCHITECT", "IMPLEMENT"]


def get_active_projects() -> list[dict]:
    """Load all active/draft projects from legion.db."""
    db = LEGION_HOME / "db" / "legion.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT slug, name, board, work_dir, pipeline_config, status "
            "FROM projects WHERE status IN ('active', 'draft')"
        ).fetchall()
        projects = []
        for r in rows:
            p = dict(r)
            p["pipeline_config"] = json.loads(p.get("pipeline_config", "{}"))
            projects.append(p)
        return projects
    finally:
        conn.close()


def get_features_for_project(project_slug: str) -> list[dict]:
    """Get features for a project from legion.db."""
    db = LEGION_HOME / "db" / "legion.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT slug, prefix, name FROM features WHERE project_slug=?",
            (project_slug,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pipeline_stage(board: str, prefix: str) -> str | None:
    """Read pipeline_stage from kanban features.db."""
    db = HERMES_HOME / "kanban" / "boards" / board / "features.db"
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


def get_current_stage_card(kanban_db_path: Path, stage: str, feature_name: str, feature_slug: str) -> dict | None:
    """Find the card for the current pipeline stage of this feature.

    Title format is f'[{stage}] {name}' — the card title uses the feature NAME,
    not the slug or prefix. We search by stage keyword + name/slug parts.
    """
    if not kanban_db_path.exists():
        return None

    conn = sqlite3.connect(str(kanban_db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Search by stage keyword
        cards = conn.execute(
            "SELECT id, title, status, completed_at FROM tasks "
            "WHERE title LIKE ? AND status NOT IN ('archived') "
            "ORDER BY created_at DESC LIMIT 20",
            (f"%{stage}%",),
        ).fetchall()

        # Build filter keywords
        filter_keywords = set()
        if feature_name:
            for w in feature_name.lower().split():
                if len(w) > 3:
                    filter_keywords.add(w)
        slug_words = feature_slug.replace("-", " ").lower().split()
        for w in slug_words:
            if len(w) > 3:
                filter_keywords.add(w)

        for card in cards:
            title_lower = card["title"].lower()
            # Must start with [STAGE]
            if not title_lower.startswith(f"[{stage.lower()}]"):
                continue
            if any(kw in title_lower for kw in filter_keywords):
                return dict(card)
        return None
    finally:
        conn.close()


def run_pipeline(project_slug: str, prefix: str) -> tuple[str, int]:
    """Run legion pipeline for a feature via the centralized engine."""
    script = LEGION_HOME / "core" / "pipeline.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script), project_slug, prefix.upper()],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return f"Timeout (60s) for {project_slug} {prefix}", 1
    except Exception as e:
        return f"Error: {e}", 1


def format_stage(stage: str) -> str:
    labels = {
        "EXPLORE": "Exploration", "SPEC": "Spec", "DESIGN": "Design",
        "ARCHITECT": "Architecture", "IMPLEMENT": "Implémentation",
    }
    return labels.get(stage, stage)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline auto-advance watchdog")
    parser.add_argument("--project", "-p", default=None, help="Project slug (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without creating cards")
    args = parser.parse_args()

    projects = get_active_projects()
    if not projects:
        print("Aucun projet actif trouvé.")
        return

    if args.project:
        projects = [p for p in projects if p["slug"] == args.project]
        if not projects:
            print(f"Projet '{args.project}' introuvable.")
            return

    total_advanced = 0
    for proj in projects:
        slug = proj["slug"]
        board = proj["board"]
        stage_order = proj.get("pipeline_config", {}).get("stage_order", DEFAULT_STAGE_ORDER)
        kanban_db = HERMES_HOME / "kanban" / "boards" / board / "kanban.db"

        if not kanban_db.exists():
            continue

        features = get_features_for_project(slug)
        if not features:
            continue

        for feat in features:
            prefix = feat["prefix"]
            pipeline_stage = get_pipeline_stage(board, prefix)

            if not pipeline_stage or pipeline_stage == "done":
                continue  # Pas encore lancé ou déjà terminé

            if pipeline_stage not in stage_order:
                continue  # Stage invalide

            card = get_current_stage_card(kanban_db, pipeline_stage, feat["name"], feat["slug"])
            if not card:
                continue

            if card["status"] == "done":
                if args.dry_run:
                    print(f"🔍 [{slug}/{prefix}] {format_stage(pipeline_stage)} done → "
                          f"avancerait au stage suivant")
                    continue

                print(f"▶ [{slug}/{prefix}] {format_stage(pipeline_stage)} done → "
                      f"lancement advancement...")

                output, rc = run_pipeline(slug, prefix)
                # Filtrer le output pour un résumé court
                summary_lines = [l for l in output.split("\n") if l.strip() and "ℹ️" not in l]
                summary = "\n  ".join(summary_lines[-6:])  # Dernières lignes utiles
                if rc == 0:
                    print(f"  ✅ OK: {summary[:300]}")
                    total_advanced += 1
                else:
                    print(f"  ❌ Erreur (rc={rc}): {summary[:300]}")

    if total_advanced == 0:
        print("Rien à avancer. ✓")


if __name__ == "__main__":
    main()
