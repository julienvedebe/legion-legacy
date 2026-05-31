"""
Legion — CLI entry point
Usage: python3 ~/.legion/core/cli.py <command> [args]
"""

import sys
import os
import argparse

# Add ~/.legion to path
sys.path.insert(0, os.path.expanduser("~/.legion"))

from core.db import init_db, list_projects, get_project, list_features, get_feature_by_prefix


def _detect_default_project() -> str:
    """Detect default project: LEGION_PROJECT env, then skull-game, then wonderfamilly."""
    env = os.environ.get("LEGION_PROJECT")
    if env:
        return env
    # Try skull-game first (it's the default test project)
    if get_project("skull-game"):
        return "skull-game"
    return "wonderfamilly"


def _show_plugin_help(plugin_name: str, project_slug: str):
    """Show help for a plugin's commands."""
    from plugins.registry import discover_plugins
    plugins = discover_plugins()
    plugin = plugins.get(plugin_name)
    if not plugin:
        print(f"❌ Plugin '{plugin_name}' non trouvé.")
        return
    print(f"📦 {plugin_name} v{plugin.version} — {plugin.description}")
    print(f"   Projet: {project_slug}")
    cmds = plugin.get_commands()
    if cmds:
        print(f"   Commandes:")
        for name, cmd in sorted(cmds.items()):
            print(f"     legion {plugin_name} {name:10s}  {cmd.description}")
            if cmd.usage:
                print(f"       Usage: legion {cmd.usage}")


def cmd_list_projects(args):
    """List all projects."""
    projects = list_projects()
    if not projects:
        print("📭 Aucun projet enregistré.")
        return

    print(f"📋 Projets Légion ({len(projects)})")
    print()
    for p in projects:
        print(f"  {p['slug']:20s} │ {p['name']:30s} │ {p['project_type']:20s} │ {p['work_dir']}")


def cmd_list_features(args):
    """List features for a project."""
    project_slug = args.project or os.environ.get("LEGION_PROJECT")
    if not project_slug:
        # Try default
        project_slug = "wonderfamilly"
        if not get_project(project_slug):
            print("❌ Utilise --project <slug> ou LEGION_PROJECT")
            return

    proj = get_project(project_slug)
    if not proj:
        print(f"❌ Projet '{project_slug}' introuvable.")
        return

    features = list_features(project_slug)
    if not features:
        print(f"📭 Aucune feature pour '{project_slug}'.")
        return

    print(f"📋 Features — {proj['name']} ({project_slug})")
    print(f"   Board: {proj['board']}  |  Profiles: {len(proj.get('profiles', {}))}")
    print()
    for f in features:
        status_icon = {
            "backlog": "⬜",
            "exploration": "🔍",
            "spec": "📝",
            "design": "🎨",
            "architect": "🏗️",
            "implement": "⚙️",
            "done": "✅",
        }.get(f.get("status", "backlog"), "⬜")
        meta = f.get("meta", {})
        stage = meta.get("pipeline_stage", "")
        stage_info = f"  [stage: {stage}]" if stage else ""
        print(f"  {status_icon} {f['prefix']:6s} │ {f['name']}{stage_info}")
    print()
    print(f"   Total: {len(features)} features")


def cmd_show_project(args):
    """Show project details."""
    project_slug = args.project or os.environ.get("LEGION_PROJECT")
    if not project_slug:
        print("❌ Utilise --project <slug> ou LEGION_PROJECT")
        return

    proj = get_project(project_slug)
    if not proj:
        print(f"❌ Projet '{project_slug}' introuvable.")
        return

    print(f"🏗️  {proj['name']}")
    print(f"   Slug:     {proj['slug']}")
    print(f"   Type:     {proj['project_type']}")
    print(f"   Dossier:  {proj['work_dir']}")
    print(f"   Board:    {proj['board']}")
    print(f"   Docs:     {proj['docs_structure']}")
    print(f"   Skills:   {', '.join(proj.get('extra_skills', [])) or '(aucun)'}")
    print()
    profiles = proj.get("profiles", {})
    if profiles:
        print(f"   Profils ({len(profiles)}) :")
        for role, pname in sorted(profiles.items()):
            print(f"     {role:15s} → {pname}")
    conventions = proj.get("conventions", {})
    if conventions:
        print(f"   Docs par étage :")
        for stage, path in sorted(conventions.items()):
            print(f"     {stage:10s} → {path}")


def cmd_status(args):
    """Show pipeline status for a feature."""
    prefix = args.prefix
    project_slug = args.project or os.environ.get("LEGION_PROJECT")
    if not project_slug:
        project_slug = "wonderfamilly"

    if not prefix:
        print("❌ Indique un préfixe : legion status <PREFIX>")
        return

    feat = get_feature_by_prefix(prefix, project_slug)
    if not feat:
        print(f"❌ Aucune feature avec le préfixe '{prefix}' dans '{project_slug}'")
        return

    print(f"📊 {feat['name']} [{prefix}]")
    print(f"   Statut:    {feat['status']}")
    print(f"   Slug:      {feat['slug']}")
    meta = feat.get("meta", {})
    if meta.get("pipeline_stage"):
        print(f"   Pipeline:  étape {meta['pipeline_stage']}")
    print()
    # Show document status based on conventions
    proj = get_project(project_slug)
    if proj:
        conv = proj.get("conventions", {})
        for stage, path_tpl in sorted(conv.items()):
            doc_path = path_tpl.replace("{slug}", feat["slug"])
            full_path = os.path.join(proj["work_dir"], doc_path)
            exists = os.path.exists(full_path)
            icon = "✅" if exists else "⬜"
            stage_name = stage.upper()
            print(f"   {icon} {stage_name:12s} {doc_path}")


def main():
    parser = argparse.ArgumentParser(description="Legion — Système de société virtuelle")
    parser.add_argument("--project", "-p", help="Slug du projet (ou LEGION_PROJECT)")

    sub = parser.add_subparsers(dest="command", help="Commande")

    # projects
    p_projects = sub.add_parser("projects", help="Gérer les projets")
    p_projects.add_argument("action", nargs="?", choices=["list", "show"], default="list")
    p_projects.add_argument("slug", nargs="?", help="Slug du projet")

    # features
    p_features = sub.add_parser("features", help="Lister les features")
    p_features.add_argument("action", nargs="?", choices=["list"], default="list")

    # status
    p_status = sub.add_parser("status", help="Statut d'une feature")
    p_status.add_argument("prefix", help="Préfixe de la feature (ex: AUTH)")

    # init
    p_init = sub.add_parser("init", help="Initialiser la DB Légion")

    # ── Plugin commands: expo ──
    p_expo = sub.add_parser("expo", help="Commandes Expo (plugin)")
    p_expo.add_argument("subcommand", nargs="?", default="status",
                        choices=["start", "stop", "status", "build", "update", "help"],
                        help="Sous-commande Expo")
    p_expo.add_argument("extra", nargs="*", help="Arguments supplémentaires")

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print("✅ Base Légion initialisée (~/.legion/db/legion.db)")

    elif args.command == "projects":
        if args.action == "list":
            cmd_list_projects(args)
        elif args.action == "show":
            # Use slug from args or --project
            if args.slug:
                args.project = args.slug
            cmd_show_project(args)

    elif args.command == "features":
        cmd_list_features(args)

    elif args.command == "status":
        cmd_status(args)

    elif args.command == "expo":
        # Dispatch to Expo plugin
        project_slug = args.project or os.environ.get("LEGION_PROJECT") or _detect_default_project()
        proj = get_project(project_slug) if project_slug else None
        if not proj:
            print(f"❌ Projet '{project_slug}' introuvable.")
            return

        from plugins.registry import run_plugin_command
        from plugins.base import PluginContext

        ctx = PluginContext(
            project_slug=project_slug,
            work_dir=proj["work_dir"],
            args=args.extra,
        )

        if args.subcommand == "help":
            _show_plugin_help("expo", project_slug)
        elif not run_plugin_command("expo", args.subcommand, ctx):
            print(f"❌ Commande 'expo {args.subcommand}' inconnue.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
