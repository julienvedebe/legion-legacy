#!/usr/bin/env python3
"""
Migration script — Import existing projects into Legion DB.
Usage: python3 migrate.py [--from-pipeline-yaml] [--from-features-db]

Migrates:
1. pipeline-projects.yaml → projects + profiles + conventions
2. per-project features.db → features + feature_meta
"""

import sys
import os

sys.path.insert(0, os.path.expanduser("~/.legion"))
from core.db import (
    init_db, import_from_pipeline_yaml, import_features_from_db,
    list_projects, list_features, get_project,
)


def main():
    init_db()

    print("🏛️  Migration Légion")
    print("═" * 40)

    # 1. Import projets depuis pipeline-projects.yaml
    yaml_path = os.path.expanduser("~/.hermes/pipeline-projects.yaml")
    if os.path.exists(yaml_path):
        print(f"\n📦 Import depuis {yaml_path}...")
        imported = import_from_pipeline_yaml(yaml_path)
        print(f"   ✅ {len(imported)} projets importés : {', '.join(imported)}")
    else:
        print(f"\n⚠️  {yaml_path} introuvable, skip projets")

    # 2. Import features depuis les DBs legacy
    print("\n📋 Import des features...")
    total_features = 0
    for proj in list_projects():
        board = proj["board"]
        features_db = os.path.expanduser(
            f"~/.hermes/kanban/boards/{board}/features.db"
        )
        if os.path.exists(features_db):
            count = import_features_from_db(proj["slug"], features_db)
            if count > 0:
                print(f"   ✅ {proj['slug']}: {count} features importées")
                total_features += count
        else:
            print(f"   ⚠️  {proj['slug']}: pas de features.db ({features_db})")

    print(f"\n📊 Total: {len(list_projects())} projets, {total_features} features")
    print()

    # 3. Vérification
    print("🔍 Vérification...")
    for proj in list_projects():
        p = get_project(proj["slug"])
        profiles = p.get("profiles", {})
        features = list_features(proj["slug"])
        print(f"   {proj['slug']:20s} │ {len(profiles)} profils │ {len(features)} features")


if __name__ == "__main__":
    main()
