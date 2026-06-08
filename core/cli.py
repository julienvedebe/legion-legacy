"""
Legion — CLI entry point
Usage: python3 ~/.legion/core/cli.py <command> [args]
"""

import sys
import os
import argparse
import json

# Add ~/.legion to path
sys.path.insert(0, os.path.expanduser("~/.legion"))

from core.db import (
    init_db, list_projects, get_project, list_features, get_feature_by_prefix,
    add_bundle, get_bundle, list_bundles, delete_bundle,
    add_profile_template, get_profile_template, list_profile_templates,
    update_profile_active, delete_profile_template, seed_system_profiles,
)


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


# ═══════════════════════════════════════════════════════════════
# Bundle commands
# ═══════════════════════════════════════════════════════════════

def cmd_bundle(args):
    """Manage skill bundles."""
    action = args.bundle_action
    project_slug = args.project or os.environ.get("LEGION_PROJECT") or "skull-game"

    if action == "list":
        bundles = list_bundles(project_slug)
        if not bundles:
            print("📭 Aucun bundle.")
            return
        print(f"📦 Bundles ({len(bundles)})")
        print()
        for b in bundles:
            skills_str = ", ".join(b["skills"]) if b["skills"] else "(aucun)"
            active_count = " ⭐" if b["project_slug"] and b["project_slug"] == project_slug else ""
            print(f"  {b['name']:25s} │ {b.get('description', ''):40s} │ {skills_str}{active_count}")

    elif action == "create":
        name = args.bundle_name
        if not name:
            print("❌ Indique un nom : legion bundle create <name> --skill s1 --skill s2")
            return
        skills = args.skill or []
        desc = args.desc or ""
        instruction = args.instruction or ""
        bundle = add_bundle(name=name, skills=skills, description=desc,
                           project_slug=project_slug, instruction=instruction)
        if bundle:
            print(f"✅ Bundle '{name}' créé ({len(skills)} skills)")
            # Also create Hermes bundle
            cmd = f"hermes bundles create \"{name}\""
            for s in skills:
                cmd += f" --skill {s}"
            if desc:
                cmd += f" --description \"{desc}\""
            if instruction:
                cmd += f" --instruction \"{instruction}\""
            cmd += " 2>/dev/null"
            os.system(cmd)
        else:
            print(f"❌ Erreur création bundle '{name}'")

    elif action == "show":
        name = args.bundle_name
        if not name:
            print("❌ Indique un nom : legion bundle show <name>")
            return
        b = get_bundle(name)
        if not b:
            print(f"❌ Bundle '{name}' introuvable.")
            return
        print(f"📦 {b['name']}")
        print(f"   Description: {b.get('description', '')}")
        print(f"   Projet:      {b.get('project_slug', '(global)')}")
        print(f"   Skills:      {', '.join(b['skills'])}")
        print(f"   Instruction: {b.get('instruction', '')}")

    elif action == "delete":
        name = args.bundle_name
        if not name:
            print("❌ Indique un nom : legion bundle delete <name>")
            return
        if delete_bundle(name):
            print(f"🗑️  Bundle '{name}' supprimé.")
            os.system(f"hermes bundles delete \"{name}\" 2>/dev/null")
        else:
            print(f"❌ Bundle '{name}' introuvable.")


# ═══════════════════════════════════════════════════════════════
# Profile commands
# ═══════════════════════════════════════════════════════════════

def cmd_profile(args):
    """Manage profile templates."""
    action = args.profile_action
    project_slug = args.project or os.environ.get("LEGION_PROJECT") or "skull-game"

    if action == "list":
        profiles = list_profile_templates(project_slug)
        if not profiles:
            print("📭 Aucun profil.")
            return
        print(f"👤 Profils — {project_slug} ({len(profiles)})")
        print()
        for p in profiles:
            system_tag = " ⚙️ système" if p.get("is_system") else ""
            active_tag = " ✅ actif" if p.get("is_active") else ""
            bundle_info = f"  bundle: {p['bundle_name']}" if p.get("bundle_name") else ""
            print(f"  {p['name']:20s} │ {p.get('role', ''):15s} │ {bundle_info}{system_tag}{active_tag}")

    elif action == "create":
        name = args.profile_name
        if not name:
            print("❌ Indique un nom : legion profile create <name> --role <role>")
            return
        profile = add_profile_template(
            name=name,
            project_slug=project_slug,
            bundle_name=args.bundle or None,
            role=args.role or "",
            channel_id=args.channel or "",
            instruction=args.instruction or "",
            model=args.model or "",
            provider=args.provider or "",
        )
        if profile:
            print(f"✅ Profil '{name}' créé (projet: {project_slug})")
        else:
            print(f"❌ Erreur création profil '{name}'")

    elif action == "show":
        name = args.profile_name
        if not name:
            print("❌ Indique un nom : legion profile show <name>")
            return
        p = get_profile_template(name, project_slug)
        if not p:
            print(f"❌ Profil '{name}' introuvable.")
            return
        print(f"👤 {p['name']}")
        print(f"   Projet:      {p['project_slug']}")
        print(f"   Rôle:        {p.get('role', '')}")
        print(f"   Bundle:      {p.get('bundle_name', '(aucun)')}")
        print(f"   Canal:       {p.get('channel_id', '(aucun)')}")
        print(f"   Actif:       {'✅ oui' if p.get('is_active') else '⬜ non'}")
        print(f"   Système:     {'⚙️ oui' if p.get('is_system') else 'non'}")
        print(f"   Modèle:      {p.get('model', '(défaut)')}")
        print(f"   Provider:    {p.get('provider', '(défaut)')}")
        inst = p.get('instruction', '')
        if inst:
            print(f"   Instruction: {inst[:100]}{'...' if len(inst) > 100 else ''}")

    elif action == "delete":
        name = args.profile_name
        if not name:
            print("❌ Indique un nom : legion profile delete <name>")
            return
        p = get_profile_template(name, project_slug)
        if p and p.get("is_system"):
            print(f"❌ Impossible de supprimer un profil système '{name}'.")
            return
        if delete_profile_template(name, project_slug):
            print(f"🗑️  Profil '{name}' supprimé.")
        else:
            print(f"❌ Profil '{name}' introuvable.")

    elif action == "seed":
        """Seed default system profiles."""
        seed_system_profiles(project_slug)
        print(f"✅ Profils systèmes créés pour '{project_slug}'.")

    elif action == "activate":
        name = args.profile_name
        if not name:
            print("❌ Indique un nom : legion profile activate <name>")
            return
        p = get_profile_template(name, project_slug)
        if not p:
            print(f"❌ Profil '{name}' introuvable.")
            return
        if p.get("is_active"):
            print(f"ℹ️  Profil '{name}' déjà actif.")
            return

        # Create Hermes profile directory
        profile_name = f"{project_slug}-{name}"
        import subprocess
        subprocess.run(["hermes", "profile", "create", profile_name,
                        "--clone-from", "default"], capture_output=True, timeout=15)

        # Write SOUL.md
        soul_dir = os.path.expanduser(f"~/.hermes/profiles/{profile_name}")
        os.makedirs(soul_dir, exist_ok=True)
        instruction = p.get("instruction", "") or f"Tu es le profil {name} du projet {project_slug}."
        soul_content = f"# {name} — {project_slug}\n\n{instruction}\n"
        with open(os.path.join(soul_dir, "SOUL.md"), "w") as f:
            f.write(soul_content)

        # Update workdir in config.yaml
        proj = get_project(project_slug)
        if proj:
            config_path = os.path.join(soul_dir, "config.yaml")
            work_dir = proj["work_dir"]
            config_lines = []
            if os.path.exists(config_path):
                with open(config_path) as f:
                    for line in f:
                        config_lines.append(line)
            # Ensure workdir is set
            has_workdir = any("workdir:" in l for l in config_lines)
            if not has_workdir:
                config_lines.append(f"\nworkdir: {work_dir}\n")
                with open(config_path, "w") as f:
                    f.writelines(config_lines)

        # If channel_id provided, add to main config.yaml
        channel_id = p.get("channel_id", "")
        if channel_id:
            config_path = os.path.expanduser("~/.hermes/config.yaml")
            if os.path.exists(config_path):
                with open(config_path) as f:
                    config_text = f.read()
                # Check if channel already has a prompt
                if f"'{channel_id}'" not in config_text:
                    prompt_text = f"Channel {project_slug} - {name}\\nTu es le profil {name} du projet {project_slug}.\\n{instruction.replace(chr(10), ' ')}"
                    # Add to discord.channel_prompts
                    import re
                    # Find the channel_prompts section
                    if "channel_prompts:" in config_text:
                        # Add before the next top-level key after channel_prompts
                        config_text = config_text.replace(
                            "channel_prompts:",
                            f"channel_prompts:\n    '{channel_id}': '{prompt_text}'",
                            1
                        ) if "channel_prompts: {}" in config_text else config_text
                        if "channel_prompts: {}" not in config_text:
                            # Add as a new entry before closing }
                            pass  # Manual edit needed for complex YAML
                    with open(config_path, "w") as f:
                        f.write(config_text)

        update_profile_active(name, project_slug, True)
        print(f"✅ Profil '{name}' activé → ~/.hermes/profiles/{profile_name}/")
        if channel_id:
            print(f"   Canal Discord: {channel_id}")
            print(f"   ⚠️  Redémarre la gateway : hermes gateway restart")

    elif action == "deactivate":
        name = args.profile_name
        if not name:
            print("❌ Indique un nom : legion profile deactivate <name>")
            return
        if p and p.get("is_system"):
            print(f"❌ Impossible de désactiver un profil système.")
            return
        update_profile_active(name, project_slug, False)
        print(f"⬜ Profil '{name}' désactivé.")
        print(f"   🔄 Redémarre la gateway : hermes gateway restart")


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

    # sync
    p_sync = sub.add_parser("sync", help="Synchroniser avec Supabase Cloud (Face B)")
    p_sync.add_argument("project_slug", nargs="?", help="Slug du projet (optionnel)")
    p_sync.add_argument("--status", action="store_true", help="Voir l'état de la dernière sync")
    p_sync.add_argument("--pull", action="store_true", help="Sync inverse : commentaires ← origin")
    p_sync.add_argument("--features", action="store_true", help="Sync uniquement les features")
    p_sync.add_argument("--wiki", action="store_true", help="Sync uniquement le wiki")

    # pipeline
    p_pipeline = sub.add_parser("pipeline", help="Lancer la pipeline pour une feature")
    p_pipeline.add_argument("prefix", help="Préfixe de la feature (ex: AUTH)")
    p_pipeline.add_argument("--reset", action="store_true", help="Réinitialiser la pipeline")

    # ── Plugin commands: expo ──
    p_expo = sub.add_parser("expo", help="Commandes Expo (plugin)")
    p_expo.add_argument("subcommand", nargs="?", default="status",
                        choices=["start", "stop", "status", "build", "update", "help"],
                        help="Sous-commande Expo")
    p_expo.add_argument("extra", nargs="*", help="Arguments supplémentaires")

    # ── Bundle commands ──
    p_bundle = sub.add_parser("bundle", help="Gérer les bundles de skills")
    p_bundle.add_argument("bundle_action", choices=["list", "create", "show", "delete"],
                          help="Action sur le bundle")
    p_bundle.add_argument("bundle_name", nargs="?", help="Nom du bundle")
    p_bundle.add_argument("--skill", "-s", action="append", help="Skill à inclure (répétable)")
    p_bundle.add_argument("--desc", "-d", help="Description du bundle")
    p_bundle.add_argument("--instruction", "-i", help="Instruction additionnelle")

    # ── Profile commands ──
    p_profile = sub.add_parser("profile", help="Gérer les profils d'agents")
    p_profile.add_argument("profile_action", choices=["list", "create", "show", "delete", "seed", "activate", "deactivate"],
                           help="Action sur le profil")
    p_profile.add_argument("profile_name", nargs="?", help="Nom du profil")
    p_profile.add_argument("--bundle", "-b", help="Nom du bundle à associer")
    p_profile.add_argument("--role", "-r", help="Rôle du profil (product, design...)")
    p_profile.add_argument("--channel", "-c", help="ID du canal Discord")
    p_profile.add_argument("--instruction", "-i", help="Instruction / SOUL.md content")
    p_profile.add_argument("--model", "-m", help="Modèle override")
    p_profile.add_argument("--provider", help="Provider override")

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print("✅ Base Légion initialisée (~/.legion/db/legion.db)")

    elif args.command == "sync":
        project_slug = args.project_slug or args.project or os.environ.get("LEGION_PROJECT")
        if args.status:
            print("ℹ️  Sync status — à implémenter dans LEG-V2-04")
        else:
            print(f"ℹ️  Legion sync — à implémenter dans LEG-V2-04")
            print(f"   Projet: {project_slug or '(tous)'}")
            print(f"   --pull: {'oui' if args.pull else 'non'}")
            print(f"   --features: {'oui' if args.features else 'non'}")
            print(f"   --wiki: {'oui' if args.wiki else 'non'}")

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

    elif args.command == "pipeline":
        project_slug = args.project or os.environ.get("LEGION_PROJECT") or _detect_default_project()
        from core.pipeline import run_pipeline
        sys.exit(run_pipeline(project_slug, args.prefix, reset=args.reset))

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

    elif args.command == "bundle":
        cmd_bundle(args)

    elif args.command == "profile":
        cmd_profile(args)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
