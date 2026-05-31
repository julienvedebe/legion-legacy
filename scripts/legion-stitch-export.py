#!/usr/bin/env python3
"""
Legion — Stitch Export

Exporte les screens Stitch (mockups de design) en HTML local,
organisés par feature selon docs/design/stitch-screens.yaml.

Usage:
    legion stitch export <slug>           # Export + organisation par feature
    legion stitch export <slug> -l        # Lister les screens
    legion stitch export <slug> --remap   # Réorganiser les fichiers existants
    legion stitch export <slug> --no-map  # Export sans organisation (tout dans le dossier racine)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

try:
    import yaml
except ImportError:
    yaml = None

STITCH_PROJECTS = {
    "skull-game": "4160339772971209403",
}

HERMES_CONFIG = os.path.expanduser("~/.hermes/config.yaml")
FEATURE_NAMES = {}  # populated from legion DB


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = s.replace(" ", "-").replace("'", "").replace("é", "e").replace("è", "e")
    s = s.replace("ê", "e").replace("à", "a").replace("ù", "u").replace("ç", "c")
    s = "".join(c for c in s if c.isalnum() or c in "-_")
    return s[:60]


def get_feature_name(prefix: str) -> str:
    """Get human feature name from prefix."""
    if prefix in FEATURE_NAMES:
        return FEATURE_NAMES[prefix]
    return prefix


def load_mapping(work_dir: str) -> dict:
    """Load stitch-screens.yaml mapping."""
    mapping_file = os.path.join(work_dir, "docs", "design", "stitch-screens.yaml")
    if not os.path.isfile(mapping_file):
        return {}
    if not yaml:
        print("  ⚠️  PyYAML non installé, pip install pyyaml pour le mapping")
        return {}
    try:
        with open(mapping_file) as f:
            data = yaml.safe_load(f)
        return data.get("mappings", {})
    except Exception as e:
        print(f"  ⚠️  Erreur lecture mapping: {e}")
        return {}


def get_stitch_api_key() -> str:
    try:
        with open(HERMES_CONFIG) as f:
            for line in f:
                if "STITCH_API_KEY:" in line:
                    return line.split(":", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return os.environ.get("STITCH_API_KEY", "")


class StitchMCP:
    """Minimal MCP client for stitch-mcp over stdio."""

    def __init__(self, api_key: str):
        self.env = os.environ.copy()
        self.env["STITCH_API_KEY"] = api_key
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["npx", "@_davideast/stitch-mcp", "proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self._recv(timeout=15)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *args):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    _req_id = 0

    def _send(self, msg):
        self._req_id += 1
        payload = json.dumps(msg) + "\n"
        self.proc.stdin.write(payload.encode())
        self.proc.stdin.flush()

    def _recv(self, timeout=30):
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = self.proc.stdout.read1(4096)
            if chunk:
                buf += chunk
                text = buf.decode()
                for line in text.strip().split("\n"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            else:
                time.sleep(0.2)
        return None

    def call_tool(self, name: str, args: dict, timeout=60) -> dict:
        self._send({
            "jsonrpc": "2.0", "id": self._req_id + 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        resp = self._recv(timeout=timeout)
        if resp and "result" in resp:
            content = resp["result"].get("content", [])
            for c in content:
                if c.get("type") == "text":
                    try:
                        return json.loads(c["text"])
                    except json.JSONDecodeError:
                        return {"_text": c["text"]}
        return {}


def download_html(url: str, output_path: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Legion/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(output_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  ⚠️  Erreur téléchargement: {e}")
        return False


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, no accents, no special chars."""
    text = text.lower()
    text = text.replace("é", "e").replace("è", "e").replace("ê", "e").replace("ë", "e")
    text = text.replace("à", "a").replace("â", "a").replace("ä", "a")
    text = text.replace("ô", "o").replace("ö", "o").replace("ù", "u").replace("û", "u")
    text = text.replace("î", "i").replace("ï", "i").replace("ç", "c")
    text = re.sub(r"[^a-z0-9]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def remap_existing(output_dir: str, mapping: dict, dry_run: bool = False):
    """Reorganize existing files by feature subfolders."""
    if not os.path.isdir(output_dir):
        print("  📁 Dossier vide, rien à réorganiser")
        return

    files = [f for f in os.listdir(output_dir) if f.endswith(".html") and os.path.isfile(os.path.join(output_dir, f))]
    if not files:
        print("  📁 Aucun fichier HTML à réorganiser")
        return

    # Normalize mapping patterns for matching
    norm_mapping = {_normalize(k): v for k, v in mapping.items()}

    moved = 0
    for fn in sorted(files):
        fn_normalized = _normalize(fn.replace(".html", ""))

        prefix = None
        for pattern, feat in norm_mapping.items():
            if pattern in fn_normalized:
                prefix = feat
                break

        if not prefix:
            print(f"  ⏭️  {fn} → aucun mapping, reste dans racine")
            continue

        feat_dir = os.path.join(output_dir, prefix)
        os.makedirs(feat_dir, exist_ok=True)
        src = os.path.join(output_dir, fn)
        dst = os.path.join(feat_dir, fn)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue

        if dry_run:
            print(f"  🔀  {fn} → {prefix}/")
        else:
            shutil.move(src, dst)
            print(f"  ✅ {fn} → {prefix}/")
        moved += 1


def main():
    parser = argparse.ArgumentParser(description="Export Stitch screens to local HTML")
    parser.add_argument("slug", help="Project slug (ex: skull-game)")
    parser.add_argument("--output", "-o", default=None, help="Output directory")
    parser.add_argument("--project-id", help="Stitch project ID (override)")
    parser.add_argument("--list", "-l", action="store_true", help="Just list screens")
    parser.add_argument("--remap", action="store_true", help="Reorganize existing files by mapping")
    parser.add_argument("--no-map", action="store_true", help="Export without feature organization")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    project_id = args.project_id or STITCH_PROJECTS.get(args.slug)
    if not project_id:
        print(f"❌ Pas de projet Stitch pour '{args.slug}'.")
        print(f"   Connus: {', '.join(STITCH_PROJECTS.keys())}")
        sys.exit(1)

    api_key = get_stitch_api_key()
    if not api_key:
        print("❌ Clé API Stitch introuvable.")
        sys.exit(1)

    # Resolve work_dir and load feature names
    sys.path.insert(0, os.path.expanduser("~/.legion"))
    work_dir = ""
    try:
        from core.db import get_project, list_features
        proj = get_project(args.slug)
        if proj:
            work_dir = proj.get("work_dir", "")
            for f in list_features(args.slug):
                FEATURE_NAMES[f["prefix"]] = f["name"]
    except Exception:
        pass
    if not work_dir:
        work_dir = os.path.expanduser(f"~/projects/{args.slug}")

    # Load mapping
    mapping = load_mapping(work_dir) if not args.no_map else {}

    # Resolve output dir
    if args.output:
        output_dir = args.output
    else:
        from core.db import get_project as gp
        p = gp(args.slug)
        wd = p.get("work_dir", "") if p else ""
        if not wd:
            wd = os.path.expanduser(f"~/projects/{args.slug}")
        output_dir = os.path.join(wd, "docs", "design", "skull-game-html-mockups")

    # ── Remap mode ──
    if args.remap:
        print(f"🔄 Réorganisation de {output_dir} par feature...")
        remap_existing(output_dir, mapping, dry_run=args.dry_run)
        return

    # ── List mode ──
    print(f"🔌 Connexion à Stitch (projet {project_id})...")
    with StitchMCP(api_key) as stitch:
        print("📋 Récupération de la liste des screens...")
        project_info = stitch.call_tool("get_project", {"name": f"projects/{project_id}"}, timeout=30)

        # Extract screens from project info
        all_screens = []
        if isinstance(project_info, dict):
            # Try screenInstances first (most complete)
            instances = project_info.get("screenInstances", [])
            if instances:
                for inst in instances:
                    sid = inst.get("id", "")
                    src = inst.get("sourceScreen", "")
                    title = inst.get("title", inst.get("displayName", "Sans titre"))
                    all_screens.append({"id": sid, "sourceScreen": src, "title": title})
            # Fallback: screens
            screens = project_info.get("screens", [])
            if screens and not all_screens:
                for s in screens:
                    sid = s.get("id", s.get("name", ""))
                    title = s.get("title", s.get("displayName", "Sans titre"))
                    all_screens.append({"id": sid, "sourceScreen": sid, "title": title})

        if not all_screens:
            print("❌ Aucun screen trouvé.")
            return

        print(f"\n📱 {len(all_screens)} screens trouvés:")

        # Deduplicate by sourceScreen
        seen = set()
        unique_screens = []
        for s in all_screens:
            key = s.get("sourceScreen", s.get("id", ""))
            if key and key not in seen:
                seen.add(key)
                unique_screens.append(s)

        for i, s in enumerate(unique_screens):
            title = s.get("title", "Sans titre")
            short_id = s.get("sourceScreen", s.get("id", ""))
            # Detect prefix from mapping
            prefix = ""
            for pattern, feat in mapping.items():
                if pattern.lower() in title.lower():
                    prefix = f" [{feat}]"
                    break
            print(f"  {i+1:2d}. {title[:40]:40s} {prefix}")

        if args.list:
            return

        # ── Download mode ──
        print(f"\n📥 Téléchargement des HTML...")
        os.makedirs(output_dir, exist_ok=True)
        downloaded = 0
        skipped = 0

        for s in unique_screens:
            screen_id = s.get("sourceScreen", s.get("id", "")).split("/")[-1]
            title = s.get("title", "Sans titre")
            display = title[:40]
            print(f"  {display:40s}...", end=" ", flush=True)

            detail = stitch.call_tool("get_screen", {
                "name": f"projects/{project_id}/screens/{screen_id}",
                "projectId": project_id,
                "screenId": screen_id,
            }, timeout=30)

            if not detail:
                print("⚠️  pas de détail")
                skipped += 1
                continue

            html_info = detail.get("htmlCode", {})
            if not isinstance(html_info, dict) or not html_info.get("downloadUrl"):
                print("⚠️  pas de htmlCode")
                skipped += 1
                continue

            download_url = html_info["downloadUrl"]

            # Determine feature subfolder using normalized matching
            norm_mapping = {_normalize(k): v for k, v in mapping.items()}
            prefix = None
            title_norm = _normalize(title)
            for pattern, feat in norm_mapping.items():
                if pattern in title_norm:
                    prefix = feat
                    break

            if prefix and not args.no_map:
                feat_dir = os.path.join(output_dir, prefix)
                os.makedirs(feat_dir, exist_ok=True)
                save_dir = feat_dir
            else:
                save_dir = output_dir

            safe_title = slugify(title) or screen_id[:12]
            filepath = os.path.join(save_dir, f"{safe_title}.html")

            if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
                print("✅ déjà présent")
                downloaded += 1
                continue

            if download_html(download_url, filepath):
                size = os.path.getsize(filepath)
                loc = f" ({prefix}/)" if prefix else ""
                print(f"✅ {size//1024}KB{loc}")
                downloaded += 1
            else:
                print("❌ échec")

        # Summary
        print(f"\n✅ Terminé: {downloaded} fichiers")
        if skipped:
            print(f"   ({skipped} screens sans HTML)")
        if mapping:
            print(f"   Organisation: {output_dir}/<FEATURE>/")
        else:
            print(f"   Organisation: tout dans {output_dir}/")
            print(f"   💡 Crée docs/design/stitch-screens.yaml pour mapper par feature")


if __name__ == "__main__":
    main()
