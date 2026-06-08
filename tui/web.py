"""
Legion — Web Dashboard (mobile-friendly)

Usage: python3 tui/web.py [--port 8000] [--host 0.0.0.0]
"""

import argparse
import base64
import html
import io
import json
import mimetypes
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import yaml

import qrcode
from qrcode.image.pil import PilImage

try:
    import markdown as md_lib
except ImportError:
    md_lib = None

sys.path.insert(0, os.path.expanduser("~/.legion"))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

from core.db import list_projects, get_project, list_features, add_project, set_project_status, set_project_pipeline_config
from core.db import list_bundles, get_bundle, add_bundle, delete_bundle
from core.db import list_profile_templates, get_profile_template, add_profile_template, delete_profile_template, update_profile_active
from core.db import list_all_skills
from core.db import list_agent_templates, get_agent_template, render_agent_template


def _nav_tabs(slug: str, active: str) -> str:
    """Generate navigation tabs for a project."""
    tabs = {
        "features": f"/{slug}",
        "kanban": f"/kanban/{slug}",
        "docs": f"/{slug}/docs",
        "pipeline": f"/pipeline/{slug}",
        "expo": f"/expo/{slug}",
        "profiles": f"/profiles/{slug}",
    }
    labels = {"features": "📋 Features", "kanban": "📌 Kanban", "docs": "📄 Docs",
              "pipeline": "🔧 Pipeline", "expo": "⚡ Expo",
              "profiles": "👤 Profils"}
    html = '<div class="nav-tabs">'
    for key, url in tabs.items():
        cls = ' class="nav-tab active"' if key == active else ' class="nav-tab"'
        html += f'<a href="{url}"{cls}>{labels[key]}</a>'
    html += "</div>"
    return html

# ── Expo helpers ──

def _expo_pid_file(work_dir: str) -> str:
    return os.path.join(work_dir, ".expo", "dev-server.pid")

def _expo_log_file(work_dir: str) -> str:
    return os.path.join(work_dir, ".expo", "dev-server.log")

def _expo_status(work_dir: str) -> dict:
    """Check Expo dev server status. Returns {running, pid, port, logs}."""
    pid_file = _expo_pid_file(work_dir)
    log_file = _expo_log_file(work_dir)

    status = {"running": False, "pid": None, "port": None, "logs": ""}

    # Read logs
    if os.path.isfile(log_file):
        try:
            with open(log_file) as f:
                lines = f.readlines()
                status["logs"] = "".join(lines[-30:])  # last 30 lines
        except Exception:
            pass

    # Check PID file
    if os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            status["running"] = True
            status["pid"] = pid

            # Read port file (most reliable)
            pfile = os.path.join(os.path.dirname(pid_file), "dev-server.port")
            if os.path.isfile(pfile):
                with open(pfile) as f:
                    p = f.read().strip()
                    if p:
                        status["port"] = int(p)
            else:
                # Try to detect port from log
                import re
                m = re.search(r'Waiting on http://localhost:(\d+)', status.get("logs", ""))
                if m:
                    status["port"] = int(m.group(1))
                else:
                    # Fallback: check common Expo ports via ss
                    import subprocess
                    try:
                        for port_candidate in [8081, 8082, 8083, 19000, 19001, 19002, 8080]:
                            r = subprocess.run(
                                f"ss -tlnp | grep -w {port_candidate} | grep -q 'pid={pid}'",
                                shell=True, capture_output=True, timeout=2
                            )
                            if r.returncode == 0:
                                status["port"] = port_candidate
                                break
                    except Exception:
                        pass
        except (ProcessLookupError, ValueError, OSError):
            pass

    return status


def _expo_qr_data_uri(port: int) -> str:
    """Generate an Expo QR code as a base64 data URI for the LAN IP."""
    try:
        # Detect LAN IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = "192.168.0.17"  # fallback

    url = f"exp://{lan_ip}:{port}"
    img = qrcode.make(url, image_factory=PilImage)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


# ── Discord helpers ──

def _discord_token() -> str:
    """Get Discord bot token from .env."""
    env_path = os.path.expanduser("~/.hermes/.env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.strip().split("=", 1)[1].strip('"').strip("'")
    except OSError:
        pass
    return ""


DISCORD_GUILD_ID = "1500165584686678219"


def _discord_create_channel(name: str) -> str:
    """Create a Discord channel. Returns the channel ID or empty string."""
    token = _discord_token()
    if not token:
        return ""
    import subprocess
    data = json.dumps({"name": name, "type": 0})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-H", f"Authorization: Bot {token}",
             "-H", "Content-Type: application/json",
             "-d", data,
             f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            ch = json.loads(result.stdout)
            return ch.get("id", "")
    except Exception:
        pass
    return ""


def _discord_delete_channel(channel_id: str) -> bool:
    """Delete a Discord channel. Returns True if successful."""
    token = _discord_token()
    if not token or not channel_id:
        return False
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "DELETE",
             "-H", f"Authorization: Bot {token}",
             f"https://discord.com/api/v10/channels/{channel_id}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Profile helpers ──

def _create_profile(
    profile_name: str,
    work_dir: str,
    role_name: str,
    project_name: str,
    instruction: str = "",
) -> bool:
    """Create a Hermes profile directory with config.yaml and SOUL.md.
    Returns True if created."""
    profile_dir = os.path.expanduser(f"~/.hermes/profiles/{profile_name}")
    if os.path.isdir(profile_dir):
        return False  # already exists
    try:
        os.makedirs(profile_dir, exist_ok=True)
        # config.yaml
        config = {
            "_config_version": 23,
            "model": {
                "default": "deepseek-v4-flash",
                "provider": "deepseek",
            },
            "workdir": work_dir,
        }
        with open(os.path.join(profile_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        # SOUL.md
        soul = f"# {role_name.title()} — {project_name}\n\n"
        soul += f"Tu es le/la **{role_name}** pour le projet **{project_name}**.\n\n"
        soul += "## Règles\n\n"
        soul += "- Produis des documents clairs et concis en français.\n"
        soul += "- Commite tes fichiers avec `git add -A && git commit ... && git push`.\n"
        soul += "- Utilise `kanban_complete` quand ta mission est terminée.\n"
        if instruction:
            soul += f"\n## Mission spécifique\n\n{instruction}\n"
        with open(os.path.join(profile_dir, "SOUL.md"), "w") as f:
            f.write(soul)
        return True
    except OSError:
        return False


def _ensure_profile_stitch_mcp(profile_name: str):
    """Add Stitch MCP server config to a profile's config.yaml."""
    profile_dir = os.path.expanduser(f"~/.hermes/profiles/{profile_name}")
    config_path = os.path.join(profile_dir, "config.yaml")
    if not os.path.isfile(config_path):
        return
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    if "mcp_servers" in cfg and "stitch" in cfg.get("mcp_servers", {}):
        return  # already configured
    # Read stitch key from the main Hermes config or from any existing design profile
    stitch_key = _get_stitch_api_key()
    if not stitch_key:
        print("  ⚠️  STITCH_API_KEY introuvable — MCP Stitch non ajouté")
        return
    cfg.setdefault("mcp_servers", {})
    cfg["mcp_servers"]["stitch"] = {
        "command": "npx",
        "args": ["@_davideast/stitch-mcp", "proxy"],
        "env": {"STITCH_API_KEY": stitch_key},
        "timeout": 300,
    }
    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print(f"  ✅ MCP Stitch ajouté au profil {profile_name}")


def _get_stitch_api_key() -> str | None:
    """Read STITCH_API_KEY from the main Hermes config.yaml."""
    hermes_cfg = os.path.expanduser("~/.hermes/config.yaml")
    if os.path.isfile(hermes_cfg):
        with open(hermes_cfg) as f:
            cfg = yaml.safe_load(f) or {}
        try:
            return cfg["mcp_servers"]["stitch"]["env"]["STITCH_API_KEY"]
        except (KeyError, TypeError):
            pass
    # Fallback: scan existing design profiles
    profiles_dir = os.path.expanduser("~/.hermes/profiles")
    if os.path.isdir(profiles_dir):
        for d in sorted(os.listdir(profiles_dir)):
            p = os.path.join(profiles_dir, d, "config.yaml")
            if os.path.isfile(p):
                with open(p) as f:
                    try:
                        pcfg = yaml.safe_load(f) or {}
                        key = pcfg.get("mcp_servers", {}).get("stitch", {}).get("env", {}).get("STITCH_API_KEY")
                        if key:
                            return key
                    except Exception:
                        continue
    return None


PRODUCT_SOUL = """Tu es le **Product Manager / Explorateur** pour ce projet.

## Mission
1. Discute avec l'utilisateur pour comprendre le cadre du projet, ses objectifs, et ce qui le démarque
2. Produis une **spec globale** (`docs/product/{slug}/spec-globale.md`) listant les fonctionnalités pressenties
3. Définis les priorités et le MVP

## Règles
- Pose des questions pour clarifier les besoins avant d'écrire
- Ne code PAS — tu es là pour spécifier, pas implémenter
- Produis des documents en français
- Commite via `git add -A && git commit -m "docs(product): spec {slug}" && git push`
"""

ARCHITECT_SOUL = """Tu es l'**Architecte technique** pour ce projet.

## Mission
1. Analyse la spec produit pour proposer une stack technique adaptée
2. Rédige un document d'architecture (`docs/architecture/archi-{slug}.md`) couvrant :
   - Stack technique justifiée (langages, frameworks, BDD, hébergement)
   - Architecture globale (diagramme, flux de données, composants)
   - Décisions techniques clés et trade-offs
3. Définis les agents et bundles de skills nécessaires
4. Présente tes recommandations à l'utilisateur avant d'implémenter

## Règles
- Justifie chaque choix technique (pourquoi cette stack plutôt qu'une autre)
- Considère les contraintes : hébergement, budget, compétences, scalabilité
- Ne code PAS — tu es là pour concevoir, pas implémenter
- Commite via `git add -A && git commit -m "docs(archi): architecture {slug}" && git push`
"""


# ── Docs helpers ──

def _scan_project_docs(work_dir: str, features: list) -> dict:
    """Scan docs/ directory and return documents grouped by feature.

    Returns {feature_slug: {type: {title, path, ext, size}}}
    Types: product, design, architecture, features, demo, design_mockup, other
    """
    docs_dir = os.path.join(work_dir, "docs")
    if not os.path.isdir(docs_dir):
        return {}

    # Build prefix→slug map from features
    prefix_to_slug = {f["prefix"].lower(): f["slug"] for f in features}
    slug_to_prefix = {f["slug"]: f["prefix"] for f in features}
    slug_to_name = {f["slug"]: f["name"] for f in features}

    # Also detect slugs from filenames in docs/product/fonctionnalite-{slug}.md
    product_dir = os.path.join(docs_dir, "product")
    if os.path.isdir(product_dir):
        for fn in os.listdir(product_dir):
            m = re.match(r"fonctionnalite-(.+)\.md$", fn)
            if m and m.group(1) not in slug_to_name:
                slug_to_name[m.group(1)] = m.group(1).replace("-", " ").title()

    result = {}

    def _add(slug: str, doc_type: str, title: str, path: str, ext: str, size: int):
        if slug not in result:
            result[slug] = {"slug": slug, "name": slug_to_name.get(slug, slug), "prefix": slug_to_prefix.get(slug, ""), "docs": []}
        result[slug]["docs"].append({
            "type": doc_type, "title": title, "path": path,
            "ext": ext, "size": size,
        })

    ICON = {
        "product": "📝", "spec": "📝", "explore": "🔍",
        "design": "🎨", "design_mockup": "🖼️",
        "architecture": "🏗️", "archi": "🏗️",
        "features": "📦", "demo": "🚀",
    }

    for root, dirs, files in os.walk(docs_dir):
        # Skip hidden dirs
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for fn in sorted(files):
            fpath = os.path.join(root, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".md", ".html", ".png", ".jpg", ".jpeg", ".webp", ".svg"):
                continue
            size = os.path.getsize(fpath)
            rel = os.path.relpath(fpath, docs_dir)

            # Parse slug from path
            slug = None
            doc_type = "other"

            # docs/product/fonctionnalite-{slug}.md
            m = re.match(r"product/fonctionnalite-(.+)\.md$", rel)
            if m:
                slug = m.group(1)
                doc_type = "product"

            # docs/design/design-{slug}.md or docs/design/{slug}/...
            m = re.match(r"design/design-(.+)\.md$", rel)
            if m:
                slug = m.group(1)
                doc_type = "design"

            # docs/design/design-{slug}-mockups/*.html / *.png
            m = re.match(r"design/design-(.+)-mockups/(.+)", rel)
            if m:
                slug = m.group(1)
                doc_type = "design_mockup"

            # docs/design/{project-slug}-html-mockups/*.html (export Stitch)
            m = re.match(r"design/([^/]+)-html-mockups/(.+)", rel)
            if m and not slug:
                slug = m.group(1)
                doc_type = "design_mockup"

                # Check if in a feature subfolder (e.g. AUTH/file.html)
                subpath = m.group(2)
                if "/" in subpath:
                    feat_prefix, filename = subpath.split("/", 1)
                    if feat_prefix.isupper() and len(feat_prefix) <= 5:
                        # Tag with feature prefix for better display
                        pass  # filename already unique enough

            # docs/architecture/archi-{slug}.md
            m = re.match(r"architecture/archi-(.+)\.md$", rel)
            if m:
                slug = m.group(1)
                doc_type = "architecture"

            # docs/features/{slug}/INDEX.md or *.md
            m = re.match(r"features/([^/]+)/", rel)
            if m:
                slug = m.group(1)
                doc_type = "features"

            # docs/demo/{slug}/*.html
            m = re.match(r"demo/([^/]+)/", rel)
            if m:
                slug = m.group(1)
                doc_type = "demo"

            # docs/exploration-{slug}*.md
            m = re.match(r"product/exploration-(.+)\.md$", rel)
            if m:
                slug = m.group(1)
                doc_type = "explore"

            # docs/spec-{slug}*.md
            m = re.match(r"product/spec-(.+)\.md$", rel)
            if m:
                slug = m.group(1)
                doc_type = "spec"

            # Fallback: any .md in docs/product/ (generic product doc)
            if not slug:
                m = re.match(r"product/(.+)\.md$", rel)
                if m:
                    slug = "product"
                    doc_type = "product"

            if slug:
                title = fn.replace(".md", "").replace("-", " ").replace("fonctionnalite ", "").title()
                _add(slug, doc_type, title, rel, ext, size)

    # Sort: features with tickets first, then by prefix
    ordered = dict(sorted(result.items(), key=lambda x: (
        not bool(x[1]["prefix"]),
        x[1]["name"].lower(),
    )))
    return ordered


def _render_markdown(filepath: str) -> str:
    """Render a markdown file to HTML."""
    if not os.path.isfile(filepath):
        return "<p>Fichier introuvable.</p>"
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return "<p>Erreur de lecture.</p>"

    if md_lib:
        extras = ["fenced_code", "codehilite", "tables", "nl2br"]
        return md_lib.markdown(content, extensions=extras)
    else:
        # Fallback: basic HTML
        lines = []
        for line in content.split("\n"):
            if line.startswith("# "):
                lines.append(f"<h1>{html.escape(line[2:])}</h1>")
            elif line.startswith("## "):
                lines.append(f"<h2>{html.escape(line[3:])}</h2>")
            elif line.startswith("### "):
                lines.append(f"<h3>{html.escape(line[4:])}</h3>")
            elif line.startswith("- "):
                lines.append(f"<li>{html.escape(line[2:])}</li>")
            elif line.strip():
                lines.append(f"<p>{html.escape(line)}</p>")
        return "\n".join(lines)


_DOC_ICONS = {
    "product": "📝", "explore": "🔍", "spec": "📋",
    "design": "🎨", "design_mockup": "🖼️",
    "architecture": "🏗️", "archi": "🏗️",
    "features": "📦", "demo": "🚀", "other": "📄",
}

def _kanban_path(project_slug: str) -> str:
    proj = get_project(project_slug)
    if not proj:
        return ""
    board = proj.get("board", project_slug)
    return os.path.expanduser(f"~/.hermes/kanban/boards/{board}/kanban.db")


def _count_tickets(project_slug: str, prefix: str) -> tuple[int, int, dict]:
    """Count done/total tickets for a feature prefix.
    Returns (done, total, {status: count})
    """
    db_path = _kanban_path(project_slug)
    if not db_path or not os.path.isfile(db_path):
        return 0, 0, {}

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute("""
            SELECT status, COUNT(*) FROM tasks
            WHERE (title LIKE ? OR title LIKE ? OR title LIKE ?)
            AND status NOT IN ('archived')
            GROUP BY status ORDER BY status
        """, (f'{prefix}-%', f'%[{prefix}]%', f'%{prefix}-%'))
        counts = dict(cur.fetchall())
        conn.close()
        total = sum(counts.values())
        done = counts.get("done", 0)
        return done, total, counts
    except Exception:
        return 0, 0, {}


def _pipeline_stage(project_slug: str, prefix: str) -> str:
    """Read pipeline stage from kanban features.db."""
    proj = get_project(project_slug)
    if not proj:
        return "backlog"
    board = proj.get("board", project_slug)
    db_path = os.path.expanduser(f"~/.hermes/kanban/boards/{board}/features.db")
    if not os.path.isfile(db_path):
        return "backlog"
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT value FROM feature_meta WHERE slug=? AND key='pipeline_stage'",
            (prefix,)
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else "backlog"
    except Exception:
        return "backlog"


def _progress_bar(done: int, total: int) -> str:
    """Generate HTML progress bar."""
    if total == 0:
        return '<div style="height:6px;background:#1e293b;border-radius:3px;margin-top:8px"><div style="width:0%;height:100%;background:#334155;border-radius:3px"></div></div>'
    pct = round(done / total * 100)
    color = "#22c55e" if pct == 100 else "#3b82f6" if pct >= 50 else "#f59e0b"
    return f"""<div style="display:flex;align-items:center;gap:8px;margin-top:8px">
    <div style="flex:1;height:6px;background:#1e293b;border-radius:3px">
        <div style="width:{pct}%;height:100%;background:{color};border-radius:3px;transition:width 0.3s"></div>
    </div>
    <span style="font-size:12px;color:#94a3b8;min-width:50px;text-align:right">{done}/{total}</span>
</div>"""

app = FastAPI(title="Legion Dashboard")

# ── CSS ──

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    padding: 16px;
    max-width: 800px;
    margin: 0 auto;
}
h1 {
    font-size: 24px;
    font-weight: 700;
    color: #f8fafc;
    margin-bottom: 4px;
}
h2 {
    font-size: 18px;
    font-weight: 600;
    color: #94a3b8;
    margin-bottom: 16px;
}
.subtitle {
    color: #64748b;
    font-size: 14px;
    margin-bottom: 24px;
}
.card {
    background: #1e293b;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
    border: 1px solid #334155;
}
.card:hover {
    border-color: #3b82f6;
}
.card-row {
    display: flex;
    align-items: center;
    gap: 12px;
}
.prefix {
    font-weight: 700;
    color: #3b82f6;
    font-size: 14px;
    min-width: 48px;
}
.prefix-lg {
    font-weight: 700;
    color: #3b82f6;
    font-size: 18px;
    min-width: 60px;
}
.name {
    font-size: 16px;
    font-weight: 600;
    color: #f1f5f9;
}
.meta {
    color: #64748b;
    font-size: 13px;
    margin-top: 4px;
}
.stage {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    margin-top: 6px;
}
.stage-done { background: #064e3b; color: #6ee7b7; }
.stage-implement { background: #1e3a5f; color: #93c5fd; }
.stage-architect { background: #5b2e0e; color: #fdba74; }
.stage-design { background: #4a1942; color: #e879f9; }
.stage-spec { background: #1e3a5f; color: #7dd3fc; }
.stage-explore { background: #3b2f0e; color: #fde68a; }
.stage-backlog { background: #1e293b; color: #64748b; }

.project-header {
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 8px;
}
.project-meta {
    color: #64748b;
    font-size: 13px;
    margin-bottom: 16px;
}
.stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
    gap: 8px;
    margin-bottom: 16px;
}
.stat-card {
    background: #1e293b;
    border-radius: 8px;
    padding: 8px;
    text-align: center;
}
.stat-value {
    font-size: 20px;
    font-weight: 700;
    color: #f1f5f9;
}
.stat-label {
    font-size: 11px;
    color: #64748b;
}
.back-link {
    display: inline-block;
    color: #3b82f6;
    text-decoration: none;
    font-size: 14px;
    margin-bottom: 16px;
}
.back-link:hover { text-decoration: underline; }

.project-list-item {
    display: block;
    text-decoration: none;
    color: inherit;
    background: #1e293b;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 8px;
    border: 1px solid #334155;
}
.project-list-item:hover {
    border-color: #3b82f6;
}
.project-list-name {
    font-size: 18px;
    font-weight: 600;
    color: #f1f5f9;
}
.project-list-meta {
    font-size: 13px;
    color: #64748b;
    margin-top: 4px;
}
.footer {
    text-align: center;
    padding: 24px;
    color: #475569;
    font-size: 12px;
}

/* ── Kanban Board ── */
.nav-tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
}
.nav-tab {
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 14px;
    text-decoration: none;
    color: #94a3b8;
    background: #1e293b;
    border: 1px solid #334155;
}
.nav-tab.active {
    color: #f8fafc;
    background: #1d4ed8;
    border-color: #2563eb;
}
.nav-tab:hover { border-color: #3b82f6; }

/* ── Pipeline Button ── */
.pipeline-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid #3b82f6;
    background: #1e3a5f;
    color: #93c5fd;
    cursor: pointer;
    margin-top: 6px;
    transition: all 0.15s;
}
.pipeline-btn:hover { background: #2563eb; color: #fff; border-color: #60a5fa; }
.pipeline-btn:disabled { opacity: 0.5; cursor: wait; }
.pipeline-btn.running { background: #854d0e; border-color: #f59e0b; color: #fde68a; }

/* ── Toast Notification ── */
#toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    max-width: 420px;
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 12px 16px;
    font-size: 13px;
    color: #e2e8f0;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    z-index: 9999;
    display: none;
    white-space: pre-wrap;
    font-family: 'SF Mono', 'Fira Code', monospace;
    line-height: 1.5;
    max-height: 60vh;
    overflow-y: auto;
}
#toast.show { display: block; }
#toast .toast-close {
    float: right;
    cursor: pointer;
    color: #64748b;
    font-size: 18px;
    margin-left: 8px;
}
#toast .toast-close:hover { color: #f1f5f9; }
#toast.toast-success { border-left: 4px solid #22c55e; }
#toast.toast-error { border-left: 4px solid #ef4444; }
#toast.toast-info { border-left: 4px solid #3b82f6; }

.kanban-grid {
    display: flex;
    gap: 12px;
    overflow-x: auto;
    padding-bottom: 16px;
    min-height: 60vh;
}
.kanban-col {
    flex: 1;
    min-width: 260px;
    max-width: 340px;
    background: #1e293b;
    border-radius: 12px;
    padding: 12px;
    border: 1px solid #334155;
}
.kanban-col-header {
    font-size: 14px;
    font-weight: 600;
    color: #f1f5f9;
    padding-bottom: 8px;
    margin-bottom: 8px;
    border-bottom: 2px solid #334155;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.kanban-col-header .count {
    font-size: 12px;
    color: #64748b;
    font-weight: 400;
}
.kanban-card {
    background: #0f172a;
    border-radius: 8px;
    padding: 10px;
    margin-bottom: 8px;
    border: 1px solid #334155;
    font-size: 13px;
}
.kanban-card:hover { border-color: #3b82f6; }
.kanban-card .title { color: #e2e8f0; font-weight: 500; }
.kanban-card .meta { color: #64748b; font-size: 11px; margin-top: 4px; }
.kanban-card .assignee {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 4px;
    background: #1e293b;
    color: #94a3b8;
    font-size: 11px;
    margin-top: 6px;
}
.kanban-card.feature-highlight {
    border-left: 3px solid #3b82f6;
}

/* ── Forms (Projects) ── */
.form-card {
    background: #1e293b;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
    border: 1px solid #334155;
}
.form-card h3 {
    font-size: 16px;
    font-weight: 600;
    color: #e2e8f0;
    margin-bottom: 16px;
}
.form-row {
    margin-bottom: 12px;
}
.form-row label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: #94a3b8;
    margin-bottom: 4px;
}
.form-row input,
.form-row select,
.form-row textarea {
    width: 100%;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid #334155;
    background: #0f172a;
    color: #e2e8f0;
    font-size: 14px;
    outline: none;
    transition: border-color 0.15s;
}
.form-row input:focus,
.form-row select:focus,
.form-row textarea:focus {
    border-color: #3b82f6;
}
.form-row input::placeholder,
.form-row textarea::placeholder {
    color: #475569;
}
.form-row select option {
    background: #0f172a;
    color: #e2e8f0;
}
.form-row .hint {
    font-size: 11px;
    color: #64748b;
    margin-top: 2px;
}
.form-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}
.form-actions .btn-primary {
    padding: 10px 20px;
    border-radius: 8px;
    border: none;
    background: #2563eb;
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s;
}
.form-actions .btn-primary:hover {
    background: #1d4ed8;
}
.form-actions .btn-primary:disabled {
    opacity: 0.5;
    cursor: wait;
}
.form-actions .btn-secondary {
    padding: 10px 20px;
    border-radius: 8px;
    border: 1px solid #475569;
    background: transparent;
    color: #94a3b8;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.15s;
}
.form-actions .btn-secondary:hover {
    border-color: #64748b;
    color: #e2e8f0;
}
.form-loader {
    display: none;
    align-items: center;
    gap: 8px;
    padding: 16px 0;
    color: #94a3b8;
    font-size: 14px;
}
.form-loader.show { display: flex; }
.form-loader .spinner {
    width: 18px; height: 18px;
    border: 2px solid #334155;
    border-top-color: #3b82f6;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.form-result {
    display: none;
    padding: 12px 16px;
    border-radius: 8px;
    margin: 12px 0;
    font-size: 14px;
    line-height: 1.5;
}
.form-result.show { display: block; }
.form-result.success {
    background: #052e16;
    border: 1px solid #166534;
    color: #bbf7d0;
}
.form-result.error {
    background: #450a0a;
    border: 1px solid #991b1b;
    color: #fca5a5;
}
.form-result a { color: #93c5fd; }
.home-actions {
    display: flex;
    gap: 8px;
    margin-bottom: 20px;
}
.home-actions a {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    text-decoration: none;
    transition: all 0.15s;
}
.home-actions .btn-new {
    background: #2563eb;
    color: #fff;
    border: none;
}
.home-actions .btn-new:hover { background: #1d4ed8; }
.home-actions .btn-import {
    background: #1e293b;
    color: #94a3b8;
    border: 1px solid #334155;
}
.home-actions .btn-import:hover {
    border-color: #3b82f6;
    color: #e2e8f0;
}
.home-actions .btn-templates {
    background: #1e293b;
    color: #94a3b8;
    border: 1px solid #334155;
}
.home-actions .btn-templates:hover {
    border-color: #a855f7;
    color: #e2e8f0;
}
.kanban-col-todo .kanban-col-header { border-bottom-color: #f59e0b; }
.kanban-col-in_progress .kanban-col-header { border-bottom-color: #3b82f6; }
.kanban-col-blocked .kanban-col-header { border-bottom-color: #ef4444; }
.kanban-col-done .kanban-col-header { border-bottom-color: #22c55e; }

/* ── Expo Management ── */
.expo-status-card {
    background: #1e293b;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 16px;
    border: 1px solid #334155;
}
.expo-status-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
}
.expo-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    display: inline-block;
}
.expo-dot.running { background: #22c55e; box-shadow: 0 0 8px #22c55e66; }
.expo-dot.stopped { background: #ef4444; }
.expo-status-label {
    font-size: 16px;
    font-weight: 600;
}
.expo-detail {
    font-size: 13px;
    color: #94a3b8;
    margin-top: 4px;
}
.expo-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
}
.expo-btn {
    padding: 10px 20px;
    border-radius: 8px;
    border: 1px solid #334155;
    background: #1e293b;
    color: #e2e8f0;
    font-size: 14px;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: all 0.15s;
}
.expo-btn:hover { border-color: #3b82f6; background: #334155; }
.expo-btn.primary { background: #1d4ed8; border-color: #2563eb; color: #fff; }
.expo-btn.primary:hover { background: #2563eb; }
.expo-btn.danger { border-color: #ef4444; color: #fca5a5; }
.expo-btn.danger:hover { background: #7f1d1d; border-color: #ef4444; color: #fff; }
.expo-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.expo-log-box {
    background: #0f172a;
    border-radius: 8px;
    padding: 12px;
    margin-top: 12px;
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: #94a3b8;
    max-height: 300px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.5;
}
.expo-log-box .info { color: #60a5fa; }
.expo-log-box .error { color: #f87171; }
.expo-log-box .success { color: #4ade80; }
"""

# ── Routes ──

def _page(title: str, body: str, project_link: str = "") -> str:
    """Wrap content in full HTML page."""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — Legion</title>
    <style>{CSS}</style>
</head>
<body>
    <h1>🏛️  Legion</h1>
    <p class="subtitle">Dashboard</p>
    <div style="display:flex;gap:12px;margin:8px 0 12px 0;flex-wrap:wrap">
        <a href='/' style="color:#60a5fa;text-decoration:none;font-size:13px">🏠 Accueil</a>
        <a href='/wiki' style="color:#60a5fa;text-decoration:none;font-size:13px">📖 Wiki</a>
        <a href='/boards' style="color:#60a5fa;text-decoration:none;font-size:13px">📋 Boards</a>
        <a href='/bundles' style="color:#60a5fa;text-decoration:none;font-size:13px">📦 Bundles</a>
    </div>
    <div id="toast"></div>
    <div style="position:absolute;top:16px;right:16px;display:flex;gap:8px;align-items:center">
        <span id="gwStatus" style="font-size:12px;color:#64748b">●</span>
        <button onclick="restartGateway()" style="padding:6px 12px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#94a3b8;cursor:pointer;font-size:12px" title="Redémarrer la gateway Hermes">🔄 Gateway</button>
    </div>
    {project_link}
    {body}
    <div class="footer">Legion — Hermes Framework</div>
    <script>
    // Toast notification
    function showToast(message, type) {{
        var t = document.getElementById('toast');
        t.className = 'show toast-' + (type || 'info');
        t.innerHTML = '<span class="toast-close" onclick="this.parentElement.className=\\\'\\\'">×</span>' + message;
    }}

    // Pipeline runner
    function runPipeline(btn) {{
        var slug = btn.getAttribute('data-slug');
        var prefix = btn.getAttribute('data-prefix');
        console.log('🚀 Pipeline clicked:', slug, prefix);
        btn.disabled = true;
        btn.className = 'pipeline-btn running';
        btn.textContent = '⏳ Pipeline...';
        showToast('🔁 Pipeline ' + prefix + ' en cours...', 'info');

        console.log('📡 Fetching POST /api/' + slug + '/pipeline/' + prefix);
        fetch('/api/' + slug + '/pipeline/' + prefix, {{ method: 'POST' }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                btn.disabled = false;
                btn.className = 'pipeline-btn';
                btn.textContent = '▶ Pipeline';
                if (data.success) {{
                    showToast('✅ Pipeline ' + prefix + ' terminé !<br><br>' + data.output, 'success');
                }} else {{
                    showToast('❌ Pipeline ' + prefix + ' échoué<br><br>' + (data.error || data.output), 'error');
                }}
            }})
            .catch(function(err) {{
                btn.disabled = false;
                btn.className = 'pipeline-btn';
                btn.textContent = '▶ Pipeline';
                showToast('❌ Erreur réseau: ' + err.message, 'error');
            }});
    }}

    // Gateway restart
    function restartGateway() {{
        var btn = document.querySelector('button[onclick*=\"restartGateway\"]');
        if (!confirm('Redémarrer la gateway Hermes ? Les sessions en cours seront coupées.')) return;
        var status = document.getElementById('gwStatus');
        btn.disabled = true;
        btn.textContent = '⏳...';
        status.style.color = '#f59e0b';
        status.title = 'Redémarrage...';
        fetch('/api/gateway/restart', {{ method: 'POST' }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (data.success) {{
                    status.style.color = '#22c55e';
                    status.title = 'Gateway active';
                    showToast('✅ ' + data.message, 'success');
                }} else {{
                    status.style.color = '#ef4444';
                    status.title = 'Erreur: ' + data.error;
                    showToast('❌ ' + data.error, 'error');
                }}
                btn.disabled = false;
                btn.textContent = '🔄 Gateway';
            }})
            .catch(function(err) {{
                status.style.color = '#ef4444';
                status.title = 'Erreur réseau';
                btn.disabled = false;
                btn.textContent = '🔄 Gateway';
                showToast('❌ Erreur: ' + err.message, 'error');
            }});
    }}

    // Auto-refresh for Expo pages
    if (window.location.pathname.startsWith('/expo/')) {{
        setInterval(function() {{
            fetch(window.location.pathname + '/refresh')
                .then(r => r.json())
                .then(data => {{
                    // Update status dot
                    const dot = document.querySelector('.expo-dot');
                    const label = document.querySelector('.expo-status-label');
                    const detail = document.querySelector('.expo-detail');
                    const actions = document.querySelector('.expo-actions');
                    if (dot && label) {{
                        dot.className = 'expo-dot ' + (data.running ? 'running' : 'stopped');
                        label.textContent = data.running ? '🟢 En cours' : '🔴 Arrêté';
                        if (detail) detail.innerHTML = data.running ? `PID ${{data.pid}} · Port ${{data.port}}` : '';
                    }}
                    // Update QR code
                    const qrImg = document.getElementById('expo-qr');
                    const qrCard = qrImg?.closest('.expo-status-card');
                    if (qrCard) {{
                        if (!data.running) {{
                            qrCard.style.display = 'none';
                        }} else {{
                            qrCard.style.display = 'block';
                            if (qrImg && qrImg.src !== data.qr_uri) qrImg.src = data.qr_uri;
                        }}
                    }}
                    // Update logs
                    const logBox = document.querySelector('.expo-log-box');
                    if (logBox && data.logs_html) {{
                        logBox.innerHTML = data.logs_html;
                    }}
                }})
                .catch(() => {{}});
        }}, 3000);
    }}
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    projects = list_projects()

    actions = """<div class="home-actions">
        <a href="/projects/new" class="btn-new">➕ Nouveau Projet</a>
        <a href="/projects/import" class="btn-import">📥 Importer depuis GitHub</a>
        <a href="/templates" class="btn-templates">📋 Templates</a>
    </div>"""

    cards = ""
    for p in projects:
        slug = p["slug"]
        proj = get_project(slug)
        features = list_features(slug)
        n_profiles = len(proj.get("profiles", {}))

        # Build progress summary
        total_done = total_tickets = 0
        feature_lines = ""
        for f in features:
            done, total, _ = _count_tickets(slug, f['prefix'])
            total_done += done
            total_tickets += total
            bar = ""
            if total > 0:
                pct = round(done / total * 100)
                bar = f'<span style="color:{"#22c55e" if pct==100 else "#f59e0b"};font-size:12px;margin-left:8px">{done}/{total}</span>'
            feature_lines += f'<div style="display:flex;align-items:center;gap:8px;padding:2px 0;font-size:13px">' \
                f'<span style="color:#64748b;font-size:11px;font-weight:700;min-width:32px">{f["prefix"]}</span>' \
                f'<span style="flex:1;color:#cbd5e1">{f["name"][:30]}</span>' \
                f'{bar}</div>'

        proj_bar = ""
        if total_tickets > 0:
            proj_pct = round(total_done / total_tickets * 100)
            proj_bar = f'<div style="display:flex;align-items:center;gap:8px;margin:8px 0">' \
                f'<div style="flex:1;height:4px;background:#1e293b;border-radius:2px">' \
                f'<div style="width:{proj_pct}%;height:100%;background:#3b82f6;border-radius:2px"></div></div>' \
                f'<span style="font-size:12px;color:#94a3b8">{total_done}/{total_tickets}</span></div>'

        p_status = proj.get("status", "draft") if proj else "draft"
        status_tag = '<span class="stage-design" style="font-size:11px;padding:2px 6px">📝 Brouillon</span>' if p_status == "draft" else '<span class="stage-done" style="font-size:11px;padding:2px 6px">✅ Actif</span>'

        cards += f"""
        <div class="card" style="cursor:pointer" onclick="window.location='/{slug}'">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <a href="/{slug}" style="color:#f8fafc;text-decoration:none;font-size:16px;font-weight:600">{p['name']}</a>
                <span style="color:#64748b;font-size:12px">{status_tag} · {p['project_type']} · {len(features)} features · {n_profiles} profils</span>
            </div>
            {proj_bar}
            <div style="margin-top:8px;border-top:1px solid #334155;padding-top:8px">
                {feature_lines}
            </div>
        </div>"""

    return _page("Accueil", actions + cards)


@app.get("/templates", response_class=HTMLResponse)
async def templates_page():
    """List all agent templates."""
    back = '<a href="/" class="back-link">← Accueil</a>'

    all_templates = list_agent_templates()
    projects = list_projects()

    # Build project selector options
    proj_options = '<option value="">-- Sélectionner un projet --</option>'
    for p in projects:
        slug = p.get("slug", "")
        name = p.get("name", slug)
        proj_options += f'<option value="{html.escape(slug)}">{html.escape(name)}</option>'

    # Group by category
    cats = {}
    for t in all_templates:
        c = t["category"] or "(autres)"
        cats.setdefault(c, []).append(t)

    category_labels = {
        "product": "🎯 Product",
        "architect": "🏗️ Architecte",
        "design": "🎨 Design",
        "frontend": "📱 Frontend",
        "backend": "⚙️ Backend",
        "devops": "🛠️ DevOps",
    }

    tbody = ""
    for cat in ["product", "architect", "design", "frontend", "backend", "devops"]:
        items = cats.get(cat, [])
        if not items:
            continue
        label = category_labels.get(cat, cat)
        tbody += f'<h3 style="margin:20px 0 8px 0;color:#94a3b8;font-size:14px">{label} ({len(items)})</h3>'
        for t in items:
            skills_html = ", ".join(
                f'<code style="background:#1e293b;padding:1px 5px;border-radius:3px;font-size:11px">{s}</code>'
                for s in t["bind_skills"]
            )

            # Show rendered preview (with generic vars)
            # Use generic placeholders to give a sense of what the template produces
            tbody += f"""
            <div class="card" style="margin-bottom:8px">
                <div style="display:flex;justify-content:space-between;align-items:flex-start">
                    <div>
                        <strong style="color:#f8fafc;font-size:15px">{t['label']}</strong>
                        <code style="background:#1e293b;padding:1px 5px;border-radius:3px;font-size:11px;margin-left:6px;color:#64748b">{t['name']}</code>
                    </div>
                    <div style="display:flex;align-items:center;gap:6px">
                        <span style="color:#64748b;font-size:11px">{'🔵 Système' if t['is_system'] else '🟢 Personnalisé'}</span>
                        <button onclick="createProfile('{t['name']}', '{html.escape(t['label'])}')" class="pipeline-btn" style="border-color:#22c55e;color:#bbf7d0;background:#052e16;font-size:12px">➕ Profil</button>
                    </div>
                </div>
                <p style="color:#94a3b8;font-size:13px;margin:4px 0">{html.escape(t['description'])}</p>
                <div style="margin-top:6px;font-size:12px;color:#64748b">
                    <details>
                        <summary style="cursor:pointer;color:#3b82f6;font-size:12px">📖 Voir le SOUL.md</summary>
                        <pre style="background:#0f172a;border:1px solid #334155;border-radius:6px;padding:8px;margin:6px 0;font-size:12px;white-space:pre-wrap;max-height:200px;overflow-y:auto">{html.escape(t['soul_template'][:800])}{'...' if len(t['soul_template']) > 800 else ''}</pre>
                    </details>
                    <details>
                        <summary style="cursor:pointer;color:#3b82f6;font-size:12px">💬 Voir le channel prompt</summary>
                        <pre style="background:#0f172a;border:1px solid #334155;border-radius:6px;padding:8px;margin:6px 0;font-size:12px;white-space:pre-wrap;max-height:150px;overflow-y:auto">{html.escape(t['channel_prompt'][:500])}{'...' if len(t['channel_prompt']) > 500 else ''}</pre>
                    </details>
                </div>
                <div style="margin-top:6px;font-size:12px">
                    <span style="color:#64748b">Skills :</span>
                    <span style="color:#94a3b8">{skills_html or '<em style="color:#475569">aucun</em>'}</span>
                </div>
            </div>"""

    # Summary header
    n_templates = len(all_templates)
    body = f"""<h2>📋 Templates d'agents</h2>
<p style="color:#94a3b8;font-size:13px">{n_templates} templates disponibles · crée un profil pour un projet existant</p>
{back}
<div style="margin-top:12px">
    <div class="card" style="border-color:#3b82f6;padding:12px;margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="color:#e2e8f0;font-weight:500">🚀 Créer un profil depuis un template :</span>
            <select id="projectSelector" style="flex:1;min-width:200px;padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:13px">
                {proj_options}
            </select>
        </div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
        <span style="background:#1e293b;padding:4px 10px;border-radius:12px;font-size:12px;color:#94a3b8">🎯 Product ({len(cats.get("product", []))})</span>
        <span style="background:#1e293b;padding:4px 10px;border-radius:12px;font-size:12px;color:#94a3b8">🏗️ Architecte ({len(cats.get("architect", []))})</span>
        <span style="background:#1e293b;padding:4px 10px;border-radius:12px;font-size:12px;color:#94a3b8">🎨 Design ({len(cats.get("design", []))})</span>
        <span style="background:#1e293b;padding:4px 10px;border-radius:12px;font-size:12px;color:#94a3b8">📱 Frontend ({len(cats.get("frontend", []))})</span>
        <span style="background:#1e293b;padding:4px 10px;border-radius:12px;font-size:12px;color:#94a3b8">⚙️ Backend ({len(cats.get("backend", []))})</span>
        <span style="background:#1e293b;padding:4px 10px;border-radius:12px;font-size:12px;color:#94a3b8">🛠️ DevOps ({len(cats.get("devops", []))})</span>
    </div>
    {tbody}
</div>
<script>
function createProfile(templateName, templateLabel) {{
    var projSelect = document.getElementById('projectSelector');
    var slug = projSelect.value;
    if (!slug) {{
        showToast('❌ Sélectionne d\\'abord un projet dans le menu en haut', 'error');
        return;
    }}
    if (!confirm('Créer un profil \\"'+templateLabel+'\\" dans le projet sélectionné ?\\n\\nUn channel Discord #'+slug+'-'+templateName+' sera créé.')) return;

    fetch('/api/templates/' + encodeURIComponent(templateName) + '/create-profile', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{project_slug: slug}})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
        if (d.success) {{
            showToast(d.message, 'success');
        }} else {{
            showToast('❌ ' + (d.error || 'Erreur inconnue'), 'error');
        }}
    }})
    .catch(function(err) {{
        showToast('❌ Erreur réseau: ' + err.message, 'error');
    }});
}}
</script>
"""

    return _page("Templates", body)


@app.post("/api/templates/{name}/create-profile")
async def api_create_profile_from_template(name: str, request: Request):
    """API: Create a Hermes profile from a global agent template, attached to an existing project."""
    import json as j
    data = await request.json()
    slug = (data.get("project_slug") or "").strip()
    if not slug:
        return {"success": False, "error": "Projet requis"}

    proj = get_project(slug)
    if not proj:
        return {"success": False, "error": "Projet introuvable"}

    tmpl = get_agent_template(name)
    if not tmpl:
        return {"success": False, "error": "Template introuvable"}

    work_dir = proj["work_dir"]
    project_name = proj["name"]
    role = name  # template name = role
    profile_name = f"{slug}-{role}"

    # Check if already exists
    existing = get_profile_template(profile_name, slug)
    if existing:
        return {"success": False, "error": f"Un profil '{role}' existe déjà pour ce projet"}

    # Render template with project vars
    variables = {
        "project_name": project_name,
        "slug": slug,
        "work_dir": work_dir,
        "role": role,
        "role_title": tmpl.get("label", role),
        "channel_name": f"{slug}-{role}",
    }
    rendered_soul = render_agent_template(name, variables)
    soul_content = rendered_soul.get("soul", tmpl["soul_template"]) if rendered_soul else tmpl["soul_template"]
    channel_prompt = rendered_soul.get("channel_prompt", tmpl["channel_prompt"]) if rendered_soul else ""

    # 1. Create Discord channel
    channel_id = _discord_create_channel(f"{slug}-{role}")

    # 2. Create Hermes profile
    prof_created = _create_profile(profile_name, work_dir, role, project_name, soul_content)

    # 2b. Add Stitch MCP config if template is design-stitch
    bind_skills = tmpl.get("bind_skills", [])
    if prof_created and ("stitch" in name.lower() or "stitch" in str(bind_skills).lower()):
        _ensure_profile_stitch_mcp(profile_name)

    # 3. Create or get bundle for this role
    bundle_name = tmpl.get("bundle_name", "")
    if bind_skills and not bundle_name:
        # Create an auto-bundle for this role's skills
        auto_bundle = add_bundle(
            name=f"{profile_name}-auto",
            skills=bind_skills,
            description=f"Auto-bundle from template {name}",
            project_slug=None,
            instruction="",
        )
        bundle_name = auto_bundle["name"] if auto_bundle else ""

    # 4. Save in DB
    add_profile_template(
        name=profile_name,
        project_slug=slug,
        bundle_name=bundle_name or None,
        bundle_names=j.dumps([bundle_name]) if bundle_name else "",
        role=role,
        channel_id=channel_id,
        instruction=channel_prompt[:500],
    )

    # 5. Sync to config
    if channel_id and bundle_name:
        _sync_bundle_to_config(profile_name, slug, channel_prompt or f"Tu es le {tmpl['label']} de {project_name}.")

    return {
        "success": True,
        "message": f"✅ Profil '{role}' créé pour {project_name} — channel #{slug}-{role}",
        "profile": {"name": profile_name, "role": role, "channel_id": channel_id},
    }


@app.get("/projects/new", response_class=HTMLResponse)
async def project_new():
    """Create a new project page (draft mode)."""
    back = '<a href="/" class="back-link">← Accueil</a>'

    body = f"""<h2>➕ Nouveau Projet</h2>
<p class="subtitle">Le projet sera créé en mode brouillon avec un canal Product et Architecte sur Discord.</p>
{back}

<div class="form-card">
    <form id="projectCreateForm" onsubmit="return createProject(event)">
        <div class="form-row">
            <label for="name">Nom du projet</label>
            <input type="text" id="name" name="name" required
                   placeholder="Ex: Mon Super Projet..."
                   oninput="autoSlug(this)" autofocus>
            <div class="hint">Slug généré automatiquement : <span id="slugPreview" style="color:#3b82f6">—</span></div>
        </div>

        <div class="form-row">
            <label for="github_url">Lien GitHub (optionnel)</label>
            <input type="url" id="github_url" name="github_url"
                   placeholder="https://github.com/user/repo">
            <div class="hint">Si fourni, le dépôt sera cloné. Sinon, un dossier vide sera créé.</div>
        </div>

        <div class="form-actions">
            <button type="submit" class="btn-primary" id="createBtn">🚀 Créer le projet (brouillon)</button>
            <a href="/" class="btn-secondary">Annuler</a>
        </div>

        <div class="form-loader" id="createLoader">
            <div class="spinner"></div>
            <span id="createProgress">Création du projet...</span>
        </div>

        <div class="form-result" id="createResult"></div>
    </form>
</div>

<script>
function autoSlug(input) {{
    var slug = input.value.toLowerCase()
        .replace(/[^a-z0-9\\s-]/g, '')
        .replace(/\\s+/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '') || '—';
    document.getElementById('slugPreview').textContent = slug;
}}

function updateProgress(msg) {{
    document.getElementById('createProgress').textContent = msg;
}}

function createProject(e) {{
    e.preventDefault();
    var form = document.getElementById('projectCreateForm');
    var data = {{}};
    for (var i = 0; i < form.elements.length; i++) {{
        var el = form.elements[i];
        if (el.name) data[el.name] = el.value;
    }}
    data.slug = document.getElementById('slugPreview').textContent;

    document.getElementById('createBtn').disabled = true;
    document.getElementById('createLoader').className = 'form-loader show';
    document.getElementById('createResult').className = 'form-result';

    var apiUrl = data.github_url ? '/api/projects/import' : '/api/projects/create';

    updateProgress('Initialisation...');

    fetch(apiUrl, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(data)
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
        var result = document.getElementById('createResult');
        document.getElementById('createLoader').className = 'form-loader';
        document.getElementById('createBtn').disabled = false;
        if (d.success) {{
            result.className = 'form-result success show';
            result.innerHTML = '✅ Projet créé ! <a href="/' + d.slug + '">Accéder au projet →</a>';
        }} else {{
            result.className = 'form-result error show';
            result.textContent = '❌ ' + (d.error || 'Erreur inconnue');
        }}
    }})
    .catch(function(err) {{
        var result = document.getElementById('createResult');
        document.getElementById('createLoader').className = 'form-loader';
        document.getElementById('createBtn').disabled = false;
        result.className = 'form-result error show';
        result.textContent = '❌ Erreur réseau: ' + err.message;
    }});
    return false;
}}
</script>"""

    return _page("Nouveau Projet", body)


@app.get("/projects/import", response_class=HTMLResponse)
async def project_import():
    """Import project from GitHub page."""
    back = '<a href="/" class="back-link">← Accueil</a>'

    body = f"""<h2>📥 Importer depuis GitHub</h2>
{back}

<div class="form-card">
    <form id="projectImportForm" onsubmit="return importProject(event)">
        <div class="form-row">
            <label for="github_url">URL du dépôt GitHub</label>
            <input type="url" id="github_url" name="github_url" required
                   placeholder="https://github.com/user/repo">
            <div class="hint">Le dépôt sera cloné dans ~/projects/&lt;slug&gt;/</div>
        </div>

        <div class="form-row">
            <label for="name">Nom du projet (optionnel — auto-détecté depuis le repo)</label>
            <input type="text" id="name" name="name"
                   placeholder="Laissez vide pour utiliser le nom du repo GitHub">
        </div>

        <div class="form-actions">
            <button type="submit" class="btn-primary" id="importBtn">📥 Importer</button>
            <a href="/" class="btn-secondary">Annuler</a>
        </div>

        <div class="form-loader" id="importLoader">
            <div class="spinner"></div>
            <span id="importProgress">Clonage du dépôt...</span>
        </div>

        <div class="form-result" id="importResult"></div>
    </form>
</div>

<script>
function updateImportProgress(msg) {{
    document.getElementById('importProgress').textContent = msg;
}}

function importProject(e) {{
    e.preventDefault();
    var form = document.getElementById('projectImportForm');
    var data = {{}};
    for (var i = 0; i < form.elements.length; i++) {{
        var el = form.elements[i];
        if (el.name) data[el.name] = el.value;
    }}

    document.getElementById('importBtn').disabled = true;
    document.getElementById('importLoader').className = 'form-loader show';
    document.getElementById('importResult').className = 'form-result';
    updateImportProgress('Clonage du dépôt...');

    fetch('/api/projects/import', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(data)
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
        var result = document.getElementById('importResult');
        document.getElementById('importLoader').className = 'form-loader';
        document.getElementById('importBtn').disabled = false;
        if (d.success) {{
            result.className = 'form-result success show';
            result.innerHTML = '✅ Projet importé ! <a href="/' + d.slug + '">Accéder au projet →</a>';
        }} else {{
            result.className = 'form-result error show';
            result.textContent = '❌ ' + (d.error || 'Erreur inconnue');
        }}
    }})
    .catch(function(err) {{
        var result = document.getElementById('importResult');
        document.getElementById('importLoader').className = 'form-loader';
        document.getElementById('importBtn').disabled = false;
        result.className = 'form-result error show';
        result.textContent = '❌ Erreur réseau: ' + err.message;
    }});
    return false;
}}
</script>"""

    return _page("Importer depuis GitHub", body)



@app.get("/api/projects")
async def api_projects():
    """JSON endpoint for projects."""
    return list_projects()


@app.post("/api/projects/create")
async def api_project_create(request: Request):
    """API: Create a new project (draft) with Discord channels + profiles."""
    data = await request.json()
    name = data.get("name", "").strip()
    slug = data.get("slug", "").strip()
    github_url = data.get("github_url", "").strip()

    if not name:
        return {"success": False, "error": "Nom du projet requis"}
    if not slug:
        return {"success": False, "error": "Slug invalide"}

    existing = get_project(slug)
    if existing:
        return {"success": False, "error": f"Un projet avec le slug '{slug}' existe déjà"}

    work_dir = os.path.expanduser(f"~/projects/{slug}")
    board = slug

    # Create work directory
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(os.path.join(work_dir, "docs"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "docs", "product"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "docs", "architecture"), exist_ok=True)

    # If GitHub URL provided, clone first
    if github_url and github_url.startswith("http"):
        try:
            result = subprocess.run(
                ["git", "clone", github_url, work_dir],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return {"success": False,
                        "error": f"Échec du clonage:\n{result.stderr.strip()}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Timeout clonage git (>120s)"}
        except FileNotFoundError:
            return {"success": False, "error": "git non installé sur le serveur"}

    # Detect project type from repo if cloned
    project_type = "custom"
    if os.path.isfile(os.path.join(work_dir, "package.json")):
        project_type = "expo-supabase-app"
    elif os.path.isfile(os.path.join(work_dir, "pyproject.toml")):
        project_type = "api-backend"

    # Create project in DB (draft)
    proj = add_project(
        slug=slug, name=name, work_dir=work_dir,
        board=board, project_type=project_type, status="draft",
    )
    if not proj:
        return {"success": False, "error": "Erreur création dans la base"}

    # Create Discord channels
    channel_product_id = _discord_create_channel(f"{slug}-product")
    channel_architect_id = _discord_create_channel(f"{slug}-architect")

    # Create Hermes profiles
    profiles_created = []
    if _create_profile(f"{slug}-product", work_dir, "product", name, PRODUCT_SOUL):
        profiles_created.append("product")
    if _create_profile(f"{slug}-architect", work_dir, "architect", name, ARCHITECT_SOUL):
        profiles_created.append("architect")

    # Register profiles in Legion DB
    if profiles_created:
        from core.db import get_conn
        for role, profile_name in [
            ("product", f"{slug}-product"),
            ("architect", f"{slug}-architect"),
        ]:
            if role in profiles_created:
                try:
                    add_profile_template(
                        name=profile_name,
                        project_slug=slug,
                        role=role,
                        is_system=True,
                    )
                except Exception:
                    pass  # Profile already exists or DB error
                try:
                    conn = get_conn()
                    conn.execute(
                        "INSERT OR IGNORE INTO project_profiles (project_slug, role, profile_name) VALUES (?, ?, ?)",
                        (slug, role, profile_name),
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

    # Create bundles + sync channel prompts + skills for draft agents
    for role, channel_id in [("product", channel_product_id), ("architect", channel_architect_id)]:
        if not channel_id:
            continue
        profile_name = f"{slug}-{role}"
        instruction = PRODUCT_SOUL if role == "product" else ARCHITECT_SOUL
        # Use global bundle for this role (reusable across all projects)
        # product → skills product-manager, product-trend-researcher, etc.
        # architect → skills engineering-software-architect, engineering-backend-architect, etc.
        bundle_name = role  # "product" or "architect"
        # Ensure global bundle exists (created once, reused)
        tmpl = get_agent_template(role)
        template_skills = tmpl["bind_skills"] if tmpl and tmpl.get("bind_skills") else []
        # Extra skills: product gets research/planning, architect gets debugging
        extra = ["duckduckgo-search", "kanban-worker"]
        if role == "architect":
            extra.append("systematic-debugging")
        bundle_skills = list(set(template_skills + extra))
        add_bundle(name=bundle_name, skills=bundle_skills,
                   description=f"Bundle global {role} - réutilisable tous projets",
                   project_slug=None)
        # Update profile_template in DB: channel_id + bundle_name
        try:
            conn2 = get_conn()
            conn2.execute(
                "UPDATE profile_templates SET channel_id=?, bundle_name=? WHERE name=?",
                (str(channel_id), bundle_name, profile_name),
            )
            conn2.commit()
            conn2.close()
        except Exception:
            pass
        # Sync bundle to config.yaml (channel_prompt + skills bindings + channel_prompts.json)
        _sync_bundle_to_config(profile_name, slug, instruction)

    # Create .gitignore + git init
    gitignore = os.path.join(work_dir, ".gitignore")
    if not os.path.isfile(gitignore):
        with open(gitignore, "w") as f:
            f.write("node_modules/\n.expo/\n__pycache__/\n.env\n")
    if not os.path.isdir(os.path.join(work_dir, ".git")):
        subprocess.run(["git", "init"], cwd=work_dir, capture_output=True, timeout=10)
        subprocess.run(["git", "add", "-A"], cwd=work_dir, capture_output=True, timeout=10)
        subprocess.run(["git", "commit", "-m", "chore: initialisation projet"], cwd=work_dir, capture_output=True, timeout=10)

    return {
        "success": True,
        "slug": slug,
        "name": name,
        "status": "draft",
        "channels": {
            "product": channel_product_id or "⚠️ Non créé (token Discord ?)",
            "architect": channel_architect_id or "⚠️ Non créé (token Discord ?)",
        },
        "profiles": profiles_created,
    }


@app.post("/api/projects/import")
async def api_project_import(request: Request):
    """API: Import a project from GitHub (draft mode)."""
    data = await request.json()
    github_url = data.get("github_url", "").strip()
    custom_name = data.get("name", "").strip()

    if not github_url:
        return {"success": False, "error": "URL GitHub requise"}
    if not github_url.startswith("http"):
        return {"success": False, "error": "URL invalide (doit commencer par http)"}

    repo_name = github_url.rstrip("/").split("/")[-1].replace(".git", "")
    slug = custom_name or repo_name
    slug = slug.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug).strip("-") or "imported-project"

    existing = get_project(slug)
    if existing:
        return {"success": False, "error": f"Un projet avec le slug '{slug}' existe déjà"}

    work_dir = os.path.expanduser(f"~/projects/{slug}")
    board = slug

    # Clone repo
    try:
        result = subprocess.run(
            ["git", "clone", github_url, work_dir],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return {"success": False,
                    "error": f"Échec du clonage:\n{result.stderr.strip()}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timeout clonage git (>300s)"}
    except FileNotFoundError:
        return {"success": False, "error": "git non installé sur le serveur"}

    # Detect project type
    project_type = "custom"
    if os.path.isfile(os.path.join(work_dir, "package.json")):
        package = {}
        try:
            with open(os.path.join(work_dir, "package.json")) as f:
                package = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        if "expo" in deps:
            project_type = "expo-supabase-app"
        elif "react" in deps:
            project_type = "static-website"
    elif os.path.isfile(os.path.join(work_dir, "pyproject.toml")):
        with open(os.path.join(work_dir, "pyproject.toml")) as f:
            c = f.read()
        if "fastapi" in c or "django" in c:
            project_type = "api-backend"

    proj = add_project(
        slug=slug, name=custom_name or repo_name,
        work_dir=work_dir, board=board,
        project_type=project_type, status="draft",
    )
    if not proj:
        return {"success": False, "error": "Erreur création dans la base"}

    # Create Discord channels + profiles
    _discord_create_channel(f"{slug}-product")
    _discord_create_channel(f"{slug}-architect")
    _create_profile(f"{slug}-product", work_dir, "product", custom_name or repo_name, PRODUCT_SOUL)
    _create_profile(f"{slug}-architect", work_dir, "architect", custom_name or repo_name, ARCHITECT_SOUL)

    return {"success": True, "slug": slug, "name": custom_name or repo_name, "status": "draft"}


@app.post("/api/{slug}/activate")
async def api_activate_project(slug: str):
    """API: Activate a project — create Kanban board, features.db, register pipeline.
    Note: Profiles are created manually from templates beforehand, not automatically."""
    proj = get_project(slug)
    if not proj:
        return {"success": False, "error": "Projet introuvable"}

    if proj.get("status") == "active":
        return {"success": False, "error": "Projet déjà actif"}

    work_dir = proj["work_dir"]
    board = proj["board"]
    name = proj["name"]

    # 1. Create Kanban board via Hermes CLI (guarantees correct schema)
    import subprocess
    board_dir = os.path.expanduser(f"~/.hermes/kanban/boards/{board}")
    os.makedirs(board_dir, exist_ok=True)
    db_path = os.path.join(board_dir, "kanban.db")
    if not os.path.isfile(db_path):
        result = subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"Échec création board Kanban: {result.stderr}"}

    # 2. Create features.db with Hermes-compatible schema
    features_db = os.path.join(board_dir, "features.db")
    if not os.path.isfile(features_db):
        conn = sqlite3.connect(features_db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feature_meta (
                slug TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (slug, key)
            );
        """)
        conn.commit()
        conn.close()

    # 3. Register in pipeline-projects.yaml
    yaml_path = os.path.expanduser("~/.hermes/pipeline-projects.yaml")
    try:
        with open(yaml_path) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    projects = cfg.get("projects", {})
    if slug not in projects:
        projects[slug] = {
            "label": name,
            "slug": slug,
            "repo": work_dir,
            "kanban_board": board,
            "docs_root": "docs/",
            "project_type": proj.get("project_type", "custom"),
            "inherits": "starter_kit",
            "profiles": {},
        }
        cfg["projects"] = projects
        with open(yaml_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

    # 4. Update DB status
    set_project_status(slug, "active")

    return {
        "success": True,
        "slug": slug,
    }


@app.get("/api/{slug}/features")
async def api_features(slug: str):
    """JSON endpoint for features of a project."""
    return list_features(slug)


@app.post("/api/{slug}/pipeline/{prefix}")
async def api_pipeline(slug: str, prefix: str, skip_stages: str = ""):
    """Run legion pipeline for a feature using centralized pipeline engine.
    Optional query param: ?skip_stages=DESIGN (comma-separated)
    """
    import subprocess
    import sys
    print(f"[PIPELINE] Request: slug={slug}, prefix={prefix}, skip_stages={skip_stages}")

    pipeline_script = os.path.expanduser("~/.legion/core/pipeline.py")
    cmd = [sys.executable, pipeline_script, slug, prefix.upper()]
    if skip_stages:
        for s in skip_stages.split(","):
            s = s.strip().upper()
            if s:
                cmd.extend(["--skip-stage", s])
    print(f"[PIPELINE] Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            [sys.executable, pipeline_script, slug, prefix.upper()],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        print(f"[PIPELINE] Done: returncode={result.returncode}")
        print(f"[PIPELINE] Output: {output[:300]}...")
        return {
            "success": result.returncode == 0,
            "output": output.strip(),
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        print(f"[PIPELINE] ERROR: Timeout (120s)")
        return {"error": "Pipeline timeout (120s)", "success": False}
    except Exception as e:
        print(f"[PIPELINE] ERROR: {e}")
        return {"error": str(e), "success": False}





# ──────────────────────────────────────────────
# Wiki — LLM Wiki reader + editor
# ──────────────────────────────────────────────

WIKI_SUBDIRS = ["entities","concepts","projects","comparisons","queries","raw/articles","raw/youtube"]
WIKI_CSS = """
.wiki-wrap { display:flex; gap:24px; margin-top:12px; min-height:70vh; }
.wiki-side { width:260px; flex-shrink:0; }
.wiki-side-sticky { position:sticky; top:16px; background:#0f172a; border-radius:12px; border:1px solid #1e293b; padding:16px; max-height:80vh; overflow-y:auto; }
.wiki-side h3 { color:#f1f5f9; font-size:14px; font-weight:600; margin:0 0 12px 0; letter-spacing:0.02em; display:flex; align-items:center; gap:6px; }
.wiki-search { width:100%; padding:8px 12px; background:#020617; border:1px solid #1e293b; border-radius:8px; color:#e2e8f0; font-size:13px; outline:none; box-sizing:border-box; margin-bottom:12px; font-family:inherit; transition:border-color .15s; }
.wiki-search:focus { border-color:#6366f1; }
.wiki-search::placeholder { color:#475569; }
.wiki-search-results { display:none; position:absolute; top:100%; left:0; right:0; background:#0f172a; border:1px solid #1e293b; border-radius:8px; margin-top:4px; max-height:300px; overflow-y:auto; z-index:50; }
.wiki-search-results.show { display:block; }
.wiki-search-results a { display:block; padding:8px 12px; color:#cbd5e1; text-decoration:none; font-size:13px; border-bottom:1px solid #1e293b; transition:background .1s; }
.wiki-search-results a:hover { background:#1e293b; color:#f1f5f9; }
.wiki-search-results a:last-child { border-bottom:none; }
.wiki-search-results .result-path { font-size:11px; color:#64748b; margin-top:2px; }
.wiki-search-wrap { position:relative; }
.wiki-hover-preview { position:fixed; z-index:9999; background:#0f172a; border:1px solid #6366f1; border-radius:10px; padding:12px 16px; max-width:360px; max-height:200px; overflow:hidden; font-size:13px; line-height:1.5; color:#cbd5e1; box-shadow:0 8px 32px rgba(0,0,0,.5); pointer-events:none; opacity:0; transition:opacity .15s; }
.wiki-hover-preview.show { opacity:1; }
.wiki-hover-preview .preview-title { font-weight:600; color:#f1f5f9; margin-bottom:6px; font-size:14px; }
.wiki-hover-preview .preview-body { overflow:hidden; text-overflow:ellipsis; }
.wiki-hover-preview .preview-body p { margin:0; }
.wiki-hover-preview::after { content:''; position:absolute; bottom:0; left:0; right:0; height:24px; background:linear-gradient(transparent,#0f172a); }
.wiki-side h3::before { content:''; display:inline-block; width:3px; height:16px; background:#6366f1; border-radius:2px; }
.wiki-side-content { font-size:13px; line-height:1.7; }
.wiki-side-content a { color:#94a3b8; text-decoration:none; display:block; padding:3px 8px; border-radius:6px; transition:all .15s; }
.wiki-side-content a:hover { color:#e2e8f0; background:#1e293b; }
.wiki-side-content h2 { font-size:12px; color:#64748b; text-transform:uppercase; letter-spacing:0.05em; margin:16px 0 6px 0; font-weight:600; }
.wiki-side-content ul { list-style:none; padding:0; margin:0; }
.wiki-side-content li { padding:0; }
.wiki-main { flex:1; min-width:0; }
.wiki-bar { display:flex; gap:8px; padding:0 0 16px 0; border-bottom:1px solid #1e293b; margin-bottom:20px; flex-wrap:wrap; align-items:center; }
.wiki-bar a, .wiki-bar button { color:#64748b; text-decoration:none; font-size:13px; padding:6px 12px; border-radius:8px; border:1px solid transparent; transition:all .15s; background:transparent; cursor:pointer; font-family:inherit; display:inline-flex; align-items:center; gap:4px; }
.wiki-bar a:hover, .wiki-bar button:hover { color:#e2e8f0; background:#1e293b; border-color:#334155; }
.wiki-bar .wiki-bar-spacer { flex:1; }
.wiki-bar .wiki-btn-edit { color:#a78bfa; }
.wiki-bar .wiki-btn-edit:hover { color:#c4b5fd; border-color:#a78bfa33; background:#1e1338; }
.wiki-card { background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:32px; }
.wiki-content { line-height:1.8; color:#e2e8f0; font-size:15px; max-width:780px; }
.wiki-content h1 { color:#f1f5f9; font-size:28px; font-weight:700; margin:0 0 20px 0; letter-spacing:-0.02em; line-height:1.3; }
.wiki-content h2 { color:#e2e8f0; font-size:20px; font-weight:600; margin:32px 0 12px 0; padding-bottom:8px; border-bottom:1px solid #1e293b; }
.wiki-content h3 { color:#e2e8f0; font-size:16px; font-weight:600; margin:24px 0 8px 0; }
.wiki-content h4 { color:#94a3b8; font-size:14px; font-weight:600; margin:20px 0 6px 0; text-transform:uppercase; letter-spacing:0.03em; }
.wiki-content p { margin:12px 0; color:#cbd5e1; }
.wiki-content code { background:#1e293b; color:#e2e8f0; padding:2px 8px; border-radius:6px; font-size:13px; font-family:'JetBrains Mono','Fira Code',monospace; }
.wiki-content pre { background:#020617; padding:20px; border-radius:12px; overflow-x:auto; border:1px solid #1e293b; margin:16px 0; }
.wiki-content pre code { background:transparent; padding:0; border-radius:0; }
.wiki-content table { border-collapse:collapse; width:100%; margin:16px 0; font-size:14px; }
.wiki-content th, .wiki-content td { border:1px solid #1e293b; padding:10px 14px; text-align:left; }
.wiki-content th { background:#1e293b; color:#94a3b8; font-weight:600; font-size:13px; text-transform:uppercase; letter-spacing:0.03em; }
.wiki-content td { color:#cbd5e1; }
.wiki-content a { color:#818cf8; text-decoration:underline; text-underline-offset:2px; }
.wiki-content a:hover { color:#a5b4fc; }
.wiki-link { color:#a78bfa; text-decoration:none !important; font-weight:500; }
.wiki-link:hover { color:#c4b5fd; text-decoration:underline !important; }
.wiki-content ul, .wiki-content ol { padding-left:24px; margin:8px 0; }
.wiki-content li { margin:4px 0; color:#cbd5e1; }
.wiki-content blockquote { border-left:3px solid #6366f1; padding:8px 16px; margin:16px 0; background:#1e293b40; border-radius:0 8px 8px 0; color:#94a3b8; font-style:italic; }
.wiki-content img { max-width:100%; border-radius:8px; margin:16px 0; }
.wiki-content hr { border:none; border-top:1px solid #1e293b; margin:24px 0; }
.wiki-content .tag { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:500; background:#1e293b; color:#64748b; margin:2px; }
.wiki-meta { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; padding-bottom:16px; border-bottom:1px solid #1e293b; font-size:12px; color:#64748b; }
.wiki-meta span { display:flex; align-items:center; gap:4px; }
.wiki-meta code { font-size:11px; }
.back-link { color:#818cf8; text-decoration:none; font-size:13px; display:inline-flex; align-items:center; gap:4px; margin-bottom:8px; }
.back-link:hover { color:#a5b4fc; text-decoration:underline; }
@media (max-width:768px) { .wiki-wrap { flex-direction:column; } .wiki-side { width:100%; } .wiki-side-sticky { max-height:200px; position:static; } .wiki-card { padding:16px; } }
.wiki-edit-area { width:100%; min-height:60vh; background:#020617; color:#e2e8f0; border:1px solid #1e293b; border-radius:8px; padding:16px; font-family:'JetBrains Mono','Fira Code',monospace; font-size:14px; line-height:1.6; resize:vertical; }
.wiki-edit-area:focus { outline:none; border-color:#6366f1; }
.wiki-btn-save { padding:10px 24px; background:#6366f1; color:#fff; border:none; border-radius:8px; font-size:14px; font-weight:500; cursor:pointer; transition:all .15s; }
.wiki-btn-save:hover { background:#4f46e5; }
.wiki-btn-cancel { padding:10px 24px; background:transparent; color:#64748b; border:1px solid #334155; border-radius:8px; font-size:14px; cursor:pointer; transition:all .15s; }
.wiki-btn-cancel:hover { color:#e2e8f0; border-color:#475569; }
"""

BOARD_CSS = """
.boards-wrap { margin-top:12px; }
.boards-header { display:flex; align-items:center; gap:12px; margin-bottom:20px; flex-wrap:wrap; }
.boards-header h2 { color:#f1f5f9; font-size:22px; font-weight:600; margin:0; }
.boards-list { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:16px; }
.board-card { background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:20px; cursor:pointer; transition:all .15s; }
.board-card:hover { border-color:#6366f1; transform:translateY(-2px); }
.board-card h3 { color:#f1f5f9; font-size:16px; font-weight:600; margin:0 0 6px 0; }
.board-card p { color:#64748b; font-size:13px; margin:0 0 12px 0; }
.board-card .board-meta { font-size:12px; color:#475569; display:flex; gap:12px; }
.board-card .board-meta span { display:flex; align-items:center; gap:4px; }
.board-view-header { display:flex; align-items:center; gap:12px; margin-bottom:20px; flex-wrap:wrap; }
.board-view-header h2 { color:#f1f5f9; font-size:22px; font-weight:600; margin:0; }
.board-view-header .board-actions { margin-left:auto; display:flex; gap:8px; }
.board-columns { display:flex; gap:16px; overflow-x:auto; padding-bottom:16px; min-height:60vh; align-items:flex-start; }
.board-col { background:#0f172a; border:1px solid #1e293b; border-radius:12px; min-width:280px; max-width:320px; flex-shrink:0; display:flex; flex-direction:column; max-height:75vh; }
.board-col-header { padding:14px 16px 10px; border-bottom:1px solid #1e293b; display:flex; align-items:center; gap:8px; }
.board-col-header .col-name { color:#f1f5f9; font-size:14px; font-weight:600; flex:1; }
.board-col-header .col-count { color:#64748b; font-size:12px; background:#1e293b; padding:2px 8px; border-radius:999px; }
.board-col-header .col-actions { display:flex; gap:4px; }
.board-col-body { padding:8px; flex:1; overflow-y:auto; min-height:100px; }
.board-card-item { background:#1e293b; border:1px solid #334155; border-radius:8px; padding:12px; margin-bottom:8px; cursor:grab; transition:all .1s; }
.board-card-item:hover { border-color:#6366f1; }
.board-card-item.dragging { opacity:0.4; }
.board-card-item .card-title { font-size:13px; font-weight:500; color:#e2e8f0; margin-bottom:6px; }
.board-card-item .card-labels { display:flex; gap:4px; flex-wrap:wrap; margin-bottom:6px; }
.board-card-item .card-label { display:inline-block; width:8px; height:8px; border-radius:999px; }
.board-card-item .card-footer { display:flex; justify-content:space-between; align-items:center; font-size:11px; }
.board-card-item .card-assignee { color:#818cf8; }
.board-card-item .card-actions { display:flex; gap:4px; opacity:0; transition:opacity .1s; }
.board-card-item:hover .card-actions { opacity:1; }
.board-card-item .card-actions button { background:none; border:none; color:#64748b; cursor:pointer; padding:2px 4px; border-radius:4px; font-size:13px; }
.board-card-item .card-actions button:hover { background:#334155; color:#e2e8f0; }
.board-col.drag-over { border-color:#6366f1; background:#1e1338; }
.board-col.drag-over .board-col-body { background:#1e1338; }
.board-add-card { padding:8px; }
.board-add-card-btn { width:100%; padding:8px; background:transparent; border:1px dashed #334155; border-radius:8px; color:#64748b; font-size:12px; cursor:pointer; transition:all .1s; }
.board-add-card-btn:hover { border-color:#6366f1; color:#a78bfa; }
.board-add-col-btn { background:transparent; border:1px dashed #334155; border-radius:12px; min-width:280px; max-width:320px; flex-shrink:0; display:flex; align-items:center; justify-content:center; padding:24px; color:#64748b; font-size:13px; cursor:pointer; transition:all .1s; }
.board-add-col-btn:hover { border-color:#6366f1; color:#a78bfa; }
.board-modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:1000; align-items:center; justify-content:center; }
.board-modal-bg.show { display:flex; }
.board-modal { background:#0f172a; border:1px solid #1e293b; border-radius:16px; padding:24px; min-width:400px; max-width:500px; max-height:80vh; overflow-y:auto; }
.board-modal h3 { color:#f1f5f9; font-size:18px; font-weight:600; margin:0 0 16px 0; }
.board-modal label { display:block; font-size:12px; color:#64748b; margin:12px 0 4px; font-weight:500; text-transform:uppercase; letter-spacing:0.03em; }
.board-modal input, .board-modal textarea, .board-modal select { width:100%; padding:10px 12px; background:#020617; border:1px solid #1e293b; border-radius:8px; color:#e2e8f0; font-size:14px; outline:none; box-sizing:border-box; font-family:inherit; }
.board-modal input:focus, .board-modal textarea:focus, .board-modal select:focus { border-color:#6366f1; }
.board-modal textarea { min-height:80px; resize:vertical; }
.board-modal .modal-btns { display:flex; gap:8px; margin-top:16px; justify-content:flex-end; }
.board-modal .modal-btns button { padding:8px 20px; border-radius:8px; font-size:13px; font-weight:500; cursor:pointer; border:none; }
.board-modal .modal-btns .btn-primary { background:#6366f1; color:#fff; }
.board-modal .modal-btns .btn-primary:hover { background:#4f46e5; }
.board-modal .modal-btns .btn-secondary { background:transparent; color:#64748b; border:1px solid #334155; }
.board-modal .modal-btns .btn-secondary:hover { color:#e2e8f0; border-color:#475569; }
.board-modal .modal-btns .btn-danger { background:#dc2626; color:#fff; }
.board-modal .modal-btns .btn-danger:hover { background:#b91c1c; }
.label-picker { display:flex; gap:8px; flex-wrap:wrap; }
.label-picker .lp-item { width:28px; height:28px; border-radius:999px; cursor:pointer; border:2px solid transparent; transition:all .1s; }
.label-picker .lp-item.selected { border-color:#f1f5f9; transform:scale(1.15); }
@media (max-width:768px) { .board-columns { flex-direction:column; overflow-x:visible; } .board-col { min-width:100%; max-width:100%; max-height:none; } }
"""

WIKI_DIR = os.path.expanduser("~/wiki")

def _wiki_link(m):
    link_text = m.group(1)
    parts = link_text.split('|')
    target = parts[0].strip()
    label = parts[1].strip() if len(parts) > 1 else target
    return f'<a href="/wiki/{target}" class="wiki-link">{label}</a>'

def _wiki_resolve(path: str):
    safe = re.sub(r'\.\.', '', path) or "index"
    md_file = os.path.join(WIKI_DIR, f"{safe}.md")
    if os.path.isfile(md_file):
        return md_file, safe
    for sub in WIKI_SUBDIRS:
        c = os.path.join(WIKI_DIR, sub, f"{safe}.md")
        if os.path.isfile(c):
            return c, safe
    return None, safe

def _wiki_read(md_file: str):
    with open(md_file) as f:
        raw = f.read()
    title_m = re.search(r'^# (.+)$', raw, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else os.path.splitext(os.path.basename(md_file))[0]
    body = re.sub(r'^---.*?---\s*', '', raw, flags=re.DOTALL)
    body = re.sub(r'\[\[([^\]]+)\]\]', _wiki_link, body)
    try:
        html = md_lib.markdown(body, extensions=['fenced_code','tables'])
    except:
        html = f"<pre>{html.escape(body)}</pre>"

    # Extract frontmatter tags for display
    fm_tags = ""
    fm = re.search(r'^---\n(.*?)\n---', raw, re.DOTALL)
    if fm:
        tag_m = re.search(r'tags:\s*\[(.*?)\]', fm.group(1))
        if tag_m:
            tags = [t.strip() for t in tag_m.group(1).split(',')]
            fm_tags = '<div class="wiki-meta">' + ''.join(f'<code class="tag">{t}</code>' for t in tags) + '</div>'
    return title, fm_tags + html

def _wiki_sidebar():
    idx_path = os.path.join(WIKI_DIR, "index.md")
    if not os.path.isfile(idx_path):
        return ""
    with open(idx_path) as f:
        raw = f.read()
    raw = re.sub(r'^---.*?---\s*', '', raw, flags=re.DOTALL)
    raw = re.sub(r'\[\[([^\]]+)\]\]', _wiki_link, raw)
    return md_lib.markdown(raw, extensions=['fenced_code','tables'])

def _wiki_page(title: str, content_html: str, current: str = "", is_edit: bool = False):
    edit_btn = f'<button class="wiki-btn-edit" onclick="location.href=\'/wiki/{current}/edit\'">✏️ Éditer</button>' if current and not is_edit else ""
    nav = f"""
    <div class="wiki-bar">
        <a href='/wiki'>📖 Index</a>
        <a href='/wiki/log'>📋 Log</a>
        <a href='/wiki/SCHEMA'>📐 Schema</a>
        <a href='/'>🏛️ Legion</a>
        <span class="wiki-bar-spacer"></span>
        {edit_btn}
    </div>"""
    sidebar = _wiki_sidebar()
    return f"""<div id="wiki-hover-preview" class="wiki-hover-preview"></div>
    <div class="wiki-wrap">
        <div class="wiki-side">
            <div class="wiki-side-sticky">
                <h3>Wiki</h3>
                <div class="wiki-search-wrap">
                    <input class="wiki-search" type="text" placeholder="Rechercher..." id="wikiSearch"
                           oninput="debounceSearch(this.value, 300)" autocomplete="off" />
                    <div class="wiki-search-results" id="wikiSearchResults"></div>
                </div>
                <div class="wiki-side-content" id="wikiSideContent">{sidebar}</div>
            </div>
        </div>
        <div class="wiki-main">
            {nav}
            <div class="wiki-card">
                <div class="wiki-content">{content_html}</div>
            </div>
        </div>
    </div>
    <style>{WIKI_CSS}</style>
    <script>
    var searchTimer;
    function debounceSearch(v, ms) {{
        clearTimeout(searchTimer);
        if (!v.trim()) {{ document.getElementById('wikiSearchResults').className = 'wiki-search-results'; return; }}
        searchTimer = setTimeout(function() {{ doSearch(v); }}, ms);
    }}
    function doSearch(q) {{
        var r = document.getElementById('wikiSearchResults');
        fetch('/api/wiki/search?q=' + encodeURIComponent(q))
            .then(function(res) {{ return res.json(); }})
            .then(function(d) {{
                r.innerHTML = '';
                if (!d.results || d.results.length === 0) {{
                    r.innerHTML = '<a style="color:#64748b;cursor:default">Aucun résultat</a>';
                }} else {{
                    for (var i = 0; i < d.results.length; i++) {{
                        var item = d.results[i];
                        var a = document.createElement('a');
                        a.href = '/wiki/' + item.path;
                        a.innerHTML = '<div>' + item.title + '</div><div class="result-path">' + item.path + '</div>';
                        r.appendChild(a);
                    }}
                }}
                r.className = 'wiki-search-results show';
            }});
    }}
    document.addEventListener('click', function(e) {{
        if (!e.target.closest('.wiki-search-wrap')) {{
            document.getElementById('wikiSearchResults').className = 'wiki-search-results';
        }}
    }});
    // Hover preview on wiki-links
    var previewTimer;
    var previewEl = document.getElementById('wiki-hover-preview');
    document.addEventListener('mouseover', function(e) {{
        var link = e.target.closest('.wiki-link');
        if (!link) {{ previewEl.className = 'wiki-hover-preview'; return; }}
        var href = link.getAttribute('href');
        if (!href || !href.startsWith('/wiki/')) {{ return; }}
        var path = href.replace('/wiki/', '');
        clearTimeout(previewTimer);
        previewTimer = setTimeout(function() {{
            fetch('/api/wiki/preview/' + encodeURIComponent(path))
                .then(function(r) {{ return r.json(); }})
                .then(function(d) {{
                    if (!d.content) return;
                    previewEl.innerHTML = '<div class="preview-title">📄 ' + d.title + '</div><div class="preview-body">' + d.content + '</div>';
                    var r = link.getBoundingClientRect();
                    var top = r.top + window.scrollY - 10;
                    var left = r.right + 12;
                    if (left + 370 > window.innerWidth) {{ left = r.left - 370; }}
                    if (top + 220 > window.innerHeight) {{ top = window.innerHeight - 230; }}
                    if (top < 10) top = 10;
                    previewEl.style.left = left + 'px';
                    previewEl.style.top = top + 'px';
                    previewEl.className = 'wiki-hover-preview show';
                }});
        }}, 400);
    }});
    document.addEventListener('mouseout', function(e) {{
        if (e.target.closest('.wiki-link')) {{
            clearTimeout(previewTimer);
            previewEl.className = 'wiki-hover-preview';
        }}
    }});
    </script>"""


# ── Board data ──────────────────────────────────────────────────────
BOARDS_DIR = os.path.expanduser("~/.legion/boards")

def _board_list():
    os.makedirs(BOARDS_DIR, exist_ok=True)
    boards = []
    for fn in sorted(os.listdir(BOARDS_DIR)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(BOARDS_DIR, fn)) as f:
                    b = json.load(f)
                boards.append(b)
            except:
                pass
    return boards

def _board_get(bid):
    fp = os.path.join(BOARDS_DIR, f"{bid}.json")
    if not os.path.isfile(fp):
        return None
    with open(fp) as f:
        return json.load(f)

def _board_save(b):
    fp = os.path.join(BOARDS_DIR, f"{b['id']}.json")
    with open(fp, 'w') as f:
        json.dump(b, f, indent=2)

BOARD_LABELS = ["#ef4444","#f97316","#eab308","#22c55e","#06b6d4","#6366f1","#ec4899","#78716c"]

def _board_html_view(bid):
    board = _board_get(bid)
    if not board:
        return "Board introuvable"
    # Generate columns HTML
    cols_list = []
    for col in board.get("columns", []):
        cards_html = ""
        for card in col.get("cards", []):
            labels_html = "".join(f'<span class="card-label" style="background:{l}"></span>' for l in card.get("labels", []))
            assignee = f'<span class="card-assignee">👤 {card["assignee"]}</span>' if card.get("assignee") else ""
            cards_html += f"""<div class="board-card-item" draggable="true" data-card-id="{card['id']}" data-col-id="{col['id']}">
                <div class="card-labels">{labels_html}</div>
                <div class="card-title">{card['title']}</div>
                <div class="card-footer">{assignee}
                    <span class="card-actions">
                        <button onclick="window.editCard('{col['id']}','{card['id']}')" title="Éditer">✏️</button>
                        <button onclick="window.deleteCard('{col['id']}','{card['id']}')" title="Supprimer">🗑️</button>
                    </span>
                </div>
            </div>"""
        col_label = ""
        if col.get("color"):
            col_label = f'<span class="card-label" style="background:{col["color"]}"></span> '
        card_count = len(col.get("cards", []))
        cols_list.append(f"""<div class="board-col" data-col-id="{col['id']}" ondrop="window.drop(event)" ondragover="window.allowDrop(event)" ondragleave="window.dragLeave(event)">
            <div class="board-col-header">{col_label}<span class="col-name">{col['name']}</span>
                <span class="col-count">{card_count}</span>
                <span class="col-actions">
                    <button onclick="window.editColumn('{col['id']}')" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:12px;padding:2px 4px;border-radius:4px" title="Renommer">⚙️</button>
                </span>
            </div>
            <div class="board-col-body" ondrop="window.drop(event)" ondragover="window.allowDrop(event)" ondragleave="window.dragLeave(event)">
                {cards_html}
                <div class="board-add-card">
                    <button class="board-add-card-btn" onclick="window.openAddCard('{col['id']}')">+ Ajouter une carte</button>
                </div>
            </div>
        </div>""")
    cols_html = "\n".join(cols_list)
    # Load template and substitute
    tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "board_view.html")
    with open(tmpl_path) as f:
        html = f.read()
    html = html.replace("%%BOARD_NAME%%", board["name"])
    html = html.replace("%%BOARD_ID%%", bid)
    html = html.replace("%%BOARD_COLUMNS%%", cols_html)
    html = html.replace("%%BOARD_CSS%%", BOARD_CSS)
    return html

def _board_html_index():
    boards = _board_list()
    items = ""
    for b in boards:
        col_count = len(b.get("columns", []))
        card_count = sum(len(c.get("cards", [])) for c in b.get("columns", []))
        items += f"""<div class="board-card" onclick="location.href='/boards/{b['id']}'">
            <h3>{b['name']}</h3>
            <p>{b.get('description','')}</p>
            <div class="board-meta">
                <span>📋 {col_count} colonnes</span>
                <span>📌 {card_count} cartes</span>
            </div>
        </div>"""
    if not items:
        items = '<p style="color:#64748b">Aucun board. Créez-en un !</p>'
    tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "board_index.html")
    with open(tmpl_path) as f:
        html = f.read()
    html = html.replace("%%BOARD_ITEMS%%", items)
    html = html.replace("%%BOARD_CSS%%", BOARD_CSS)
    return html


@app.get("/wiki", response_class=HTMLResponse)
async def wiki_index():
    fp, safe = _wiki_resolve("index")
    if not fp:
        return HTMLResponse(content=_page("Wiki", "<p>Wiki introuvable.</p>"), status_code=404)
    title, html = _wiki_read(fp)
    body = _wiki_page(title, html, "")
    return _page("Wiki — Index", body)

@app.get("/wiki/{p}/edit", response_class=HTMLResponse)
async def wiki_edit(p: str):
    fp, safe = _wiki_resolve(p)
    if not fp:
        return HTMLResponse(content=_page("Wiki", "<h2>Page introuvable</h2><a href='/wiki'>← Index</a>"), status_code=404)
    with open(fp) as f:
        raw = f.read()
    title_m = re.search(r'^# (.+)$', raw, re.MULTILINE)
    page_title = title_m.group(1).strip() if title_m else safe
    import html as hlib
    
    # Convert markdown to HTML for Quill
    md_html = md_lib.markdown(raw, extensions=['fenced_code','tables'])

    escaped = hlib.escape(raw)
    escaped_html = hlib.escape(md_html)
    edit_form = f"""
    <div id="editMode" style="display:flex;gap:8px;margin-bottom:12px">
        <button class="wiki-btn-save" id="btnVisual" onclick="switchMode('visual')" style="font-size:12px;padding:6px 14px">🎨 Visuel</button>
        <button class="wiki-btn-cancel" id="btnSource" onclick="switchMode('source')" style="font-size:12px;padding:6px 14px">💻 Source</button>
        <span style="flex:1"></span>
        <button class="wiki-btn-save" id="btnSave" onclick="saveWiki()" style="font-size:12px;padding:6px 14px">💾 Enregistrer</button>
        <a href="/wiki/{safe}" class="wiki-btn-cancel" style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none;font-size:12px;padding:6px 14px">Annuler</a>
    </div>
    <div id="editorVisual" class="wiki-editor-visual" style="display:block">
        <div id="quillEditor">{escaped_html}</div>
    </div>
    <div id="editorSource" style="display:none">
        <textarea id="sourceArea" class="wiki-edit-area" spellcheck="false">{escaped}</textarea>
    </div>
    <input type="hidden" id="editorContent" name="content" value="">
    <link href="https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.snow.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/turndown@7.2.0/dist/turndown.js"></script>
    <script>
    var quill = new Quill('#quillEditor', {{
        theme: 'snow',
        modules: {{
            toolbar: [
                [{{'header':[1,2,3,false]}}],
                ['bold','italic','underline','strike'],
                [{{'list':'ordered'}},{{'list':'bullet'}}],
                ['blockquote','code-block','link'],
                ['clean']
            ]
        }},
        placeholder: 'Commencez à écrire...'
    }});
    // Override quill snow styles for dark theme
    var qs = document.createElement('style');
    qs.textContent = `
        .ql-toolbar {{ background:#1e293b; border:1px solid #334155; border-radius:8px 8px 0 0; }}
        .ql-toolbar button .ql-stroke {{ stroke:#94a3b8; }}
        .ql-toolbar button:hover .ql-stroke {{ stroke:#e2e8f0; }}
        .ql-toolbar button .ql-fill {{ fill:#94a3b8; }}
        .ql-toolbar button:hover .ql-fill {{ fill:#e2e8f0; }}
        .ql-toolbar button.ql-active .ql-stroke {{ stroke:#6366f1; }}
        .ql-toolbar button.ql-active .ql-fill {{ fill:#6366f1; }}
        .ql-toolbar .ql-picker-label {{ color:#94a3b8; }}
        .ql-container {{ background:#020617; border:1px solid #334155; border-top:none; border-radius:0 0 8px 8px; font-family:inherit; font-size:15px; min-height:50vh; color:#e2e8f0; }}
        .ql-editor.ql-blank::before {{ color:#475569; font-style:normal; }}
        .ql-editor h1 {{ color:#f1f5f9; font-size:28px; font-weight:700; }}
        .ql-editor h2 {{ color:#e2e8f0; font-size:20px; font-weight:600; }}
        .ql-editor h3 {{ color:#e2e8f0; font-size:16px; font-weight:600; }}
        .ql-editor p {{ color:#cbd5e1; line-height:1.7; }}
        .ql-editor pre.ql-syntax {{ background:#0f172a; color:#e2e8f0; padding:16px; border-radius:8px; border:1px solid #1e293b; }}
        .ql-editor blockquote {{ border-left:3px solid #6366f1; padding:4px 16px; background:#1e293b40; color:#94a3b8; }}
        .ql-editor a {{ color:#818cf8; }}
        .ql-editor ul, .ql-editor ol {{ color:#cbd5e1; }}
        .ql-picker-options {{ background:#1e293b; border-color:#334155; }}
        .ql-picker-item {{ color:#94a3b8; }}
    `;
    document.head.appendChild(qs);

    function switchMode(mode) {{
        if (mode === 'visual') {{
            // Convert source markdown to HTML and set in Quill
            var src = document.getElementById('sourceArea').value;
            var temp = document.querySelector('.ql-editor');
            // Use turndown in reverse? No - use a simple server-side approach:
            // Send the markdown to a conversion endpoint, or use the existing HTML
            // Actually, just re-render: fetch from server
            fetch('/api/wiki/md2html', {{
                method:'POST',
                headers:{{'Content-Type':'application/json'}},
                body:JSON.stringify({{markdown: src}})
            }}).then(function(r){{return r.json()}}).then(function(d){{
                if (d.html) {{
                    document.querySelector('.ql-editor').innerHTML = d.html;
                    document.getElementById('editorVisual').style.display = 'block';
                    document.getElementById('editorSource').style.display = 'none';
                    document.getElementById('btnVisual').className = 'wiki-btn-save';
                    document.getElementById('btnSource').className = 'wiki-btn-cancel';
                }}
            }});
        }} else {{
            // Convert Quill HTML to markdown for editing
            var html = document.querySelector('.ql-editor').innerHTML;
            var turndownService = new TurndownService({{headingStyle:'atx', codeBlockStyle:'fenced'}});
            var md = turndownService.turndown(html);
            document.getElementById('sourceArea').value = md;
            document.getElementById('editorVisual').style.display = 'none';
            document.getElementById('editorSource').style.display = 'block';
            document.getElementById('btnVisual').className = 'wiki-btn-cancel';
            document.getElementById('btnSource').className = 'wiki-btn-save';
        }}
    }}

    function saveWiki() {{
        var btn = document.getElementById('btnSave');
        btn.disabled = true;
        btn.textContent = '⏳...';
        var isVisual = document.getElementById('editorVisual').style.display !== 'none';
        var content;
        if (isVisual) {{
            var html = document.querySelector('.ql-editor').innerHTML;
            var turndownService = new TurndownService({{headingStyle:'atx', codeBlockStyle:'fenced'}});
            content = turndownService.turndown(html);
        }} else {{
            content = document.getElementById('sourceArea').value;
        }}
        var fd = new FormData();
        fd.append('path', '{hlib.escape(fp)}');
        fd.append('content', content);
        fetch('/api/wiki/save', {{ method:'POST', body:fd }})
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
                if(d.success) {{
                    showToast('✅ Page enregistrée', 'success');
                    setTimeout(function() {{ window.location.href = '/wiki/{safe}'; }}, 500);
                }} else {{
                    showToast('❌ ' + (d.error || 'Erreur'), 'error');
                    btn.disabled = false;
                    btn.textContent = '💾 Enregistrer';
                }}
            }})
            .catch(function() {{
                showToast('❌ Erreur réseau', 'error');
                btn.disabled = false;
                btn.textContent = '💾 Enregistrer';
            }});
    }}
    </script>"""
    body = _wiki_page(page_title, edit_form, safe, is_edit=True)
    return _page(f"Wiki — Édition: {page_title}", body)

@app.get("/wiki/{p:path}", response_class=HTMLResponse)
async def wiki_page(p: str):
    if p.endswith("/edit"):
        return await wiki_edit(p[:-5])
    fp, safe = _wiki_resolve(p)
    if not fp:
        return HTMLResponse(content=_page("Wiki", "<h2>Page introuvable</h2><a href='/wiki'>← Index</a>"), status_code=404)
    title, html = _wiki_read(fp)
    body = _wiki_page(title, html, safe)
    return _page(f"Wiki — {title}", body)

@app.post("/api/wiki/save")
async def wiki_save(request: Request):
    import html as hlib
    form = await request.form()
    path = form.get("path", "")
    content = form.get("content", "")
    if not path or not os.path.isfile(path):
        return {"success": False, "error": "Fichier introuvable"}
    if not path.startswith(os.path.expanduser("~/wiki")):
        return {"success": False, "error": "Chemin non autorisé"}
    try:
        with open(path, 'w') as f:
            f.write(content)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/wiki/md2html")
async def wiki_md2html(request: Request):
    data = await request.json()
    md = data.get("markdown", "")
    html = md_lib.markdown(md, extensions=['fenced_code','tables'])
    return {"html": html}


@app.get("/api/wiki/search")
async def wiki_search(q: str = ""):
    if not q.strip():
        return {"results": []}
    import glob
    results = []
    q_lower = q.lower()
    md_files = glob.glob(os.path.join(WIKI_DIR, "**", "*.md"), recursive=True)
    for fp in md_files:
        # Skip index, log, SCHEMA — too generic
        basename = os.path.basename(fp)
        if basename in ("index.md", "log.md", "SCHEMA.md"):
            continue
        rel = os.path.relpath(fp, WIKI_DIR)
        path_key = rel[:-3]  # remove .md
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except:
            continue
        if q_lower not in content.lower():
            continue
        title_m = re.search(r'^# (.+)$', content, re.MULTILINE)
        title = title_m.group(1).strip() if title_m else path_key
        results.append({"path": path_key, "title": title, "file": rel})
        if len(results) >= 20:
            break
    return {"results": results}


@app.get("/api/wiki/preview/{path:path}")
async def wiki_preview(path: str):
    import html as hlib
    fp, safe = _wiki_resolve(path)
    if not fp:
        return {"content": None}
    try:
        with open(fp, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except:
        return {"content": None}
    body = re.sub(r'^---.*?---\s*', '', raw, flags=re.DOTALL)
    body = re.sub(r'^# .+\n?', '', body, count=1)  # remove title
    body = re.sub(r'\[\[([^\]]+)\]\]', r'\1', body)  # strip wikilinks
    body = re.sub(r'\s+', ' ', body).strip()
    # First 250 chars as preview
    preview = body[:250]
    if len(body) > 250:
        preview += "…"
    title_m = re.search(r'^# (.+)$', raw, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else safe
    return {"content": hlib.escape(preview), "title": hlib.escape(title)}


# ── Board routes ─────────────────────────────────────────────────────
@app.get("/boards", response_class=HTMLResponse)
async def boards_index():
    body = _board_html_index()
    return _page("Boards", body)

@app.get("/boards/{bid}", response_class=HTMLResponse)
async def board_view(bid: str):
    board = _board_get(bid)
    if not board:
        return HTMLResponse(content=_page("Board", "<h2>Board introuvable</h2><a href='/boards'>← Tous les boards</a>"), status_code=404)
    board_name = board.get("name", "Board")
    body = _board_html_view(bid)
    return _page(f"Board — {board_name}", body)

@app.post("/api/boards")
async def api_create_board(request: Request):
    import uuid
    data = await request.json()
    bid = str(uuid.uuid4())[:8]
    board = {
        "id": bid,
        "name": data.get("name", "Sans titre"),
        "description": data.get("description", ""),
        "columns": [
            {"id": "todo", "name": "À faire", "color": "#ef4444", "cards": []},
            {"id": "doing", "name": "En cours", "color": "#f97316", "cards": []},
            {"id": "done", "name": "Fait", "color": "#22c55e", "cards": []},
        ]
    }
    _board_save(board)
    return {"success": True, "id": bid}

@app.get("/api/board/{bid}/card")
async def api_get_card(bid: str, col_id: str = "", card_id: str = ""):
    board = _board_get(bid)
    if not board:
        return {"error": "Board not found"}
    for col in board.get("columns", []):
        if col["id"] == col_id:
            for card in col.get("cards", []):
                if card["id"] == card_id:
                    return card
    return {"error": "Card not found"}

@app.get("/api/board/{bid}/column")
async def api_get_column(bid: str, col_id: str = ""):
    board = _board_get(bid)
    if not board:
        return {"error": "Board not found"}
    for col in board.get("columns", []):
        if col["id"] == col_id:
            return col
    return {"error": "Column not found"}

@app.post("/api/board/{bid}/cards")
async def api_add_card(bid: str, request: Request):
    import uuid
    board = _board_get(bid)
    if not board:
        return {"success": False, "error": "Board not found"}
    data = await request.json()
    card = {
        "id": str(uuid.uuid4())[:8],
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "assignee": data.get("assignee", ""),
        "labels": data.get("labels", []),
    }
    col_id = data.get("col_id", "")
    for col in board.get("columns", []):
        if col["id"] == col_id:
            col.setdefault("cards", []).append(card)
            _board_save(board)
            return {"success": True}
    return {"success": False, "error": "Column not found"}

@app.patch("/api/board/{bid}/cards")
async def api_update_card(bid: str, request: Request):
    board = _board_get(bid)
    if not board:
        return {"success": False, "error": "Board not found"}
    data = await request.json()
    col_id = data.get("col_id", "")
    card_id = data.get("card_id", "")
    for col in board.get("columns", []):
        if col["id"] == col_id:
            for card in col.get("cards", []):
                if card["id"] == card_id:
                    if "title" in data:
                        card["title"] = data["title"]
                    if "description" in data:
                        card["description"] = data["description"]
                    if "assignee" in data:
                        card["assignee"] = data["assignee"]
                    if "labels" in data:
                        card["labels"] = data["labels"]
                    _board_save(board)
                    return {"success": True}
    return {"success": False, "error": "Card not found"}

@app.delete("/api/board/{bid}/cards")
async def api_delete_card(bid: str, request: Request):
    board = _board_get(bid)
    if not board:
        return {"success": False, "error": "Board not found"}
    data = await request.json()
    col_id = data.get("col_id", "")
    card_id = data.get("card_id", "")
    for col in board.get("columns", []):
        if col["id"] == col_id:
            col["cards"] = [c for c in col.get("cards", []) if c["id"] != card_id]
            _board_save(board)
            return {"success": True}
    return {"success": False, "error": "Column not found"}

@app.post("/api/board/{bid}/columns")
async def api_add_column(bid: str, request: Request):
    import uuid
    board = _board_get(bid)
    if not board:
        return {"success": False, "error": "Board not found"}
    data = await request.json()
    col = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", ""),
        "color": data.get("color", "#6366f1"),
        "cards": [],
    }
    board.setdefault("columns", []).append(col)
    _board_save(board)
    return {"success": True}

@app.patch("/api/board/{bid}/columns")
async def api_update_column(bid: str, request: Request):
    board = _board_get(bid)
    if not board:
        return {"success": False, "error": "Board not found"}
    data = await request.json()
    col_id = data.get("col_id", "")
    for col in board.get("columns", []):
        if col["id"] == col_id:
            if "name" in data:
                col["name"] = data["name"]
            _board_save(board)
            return {"success": True}
    return {"success": False, "error": "Column not found"}

@app.post("/api/board/{bid}/move")
async def api_move_card(bid: str, request: Request):
    board = _board_get(bid)
    if not board:
        return {"success": False, "error": "Board not found"}
    data = await request.json()
    card_id = data.get("card_id", "")
    from_col = data.get("from_col", "")
    to_col = data.get("to_col", "")
    card = None
    for col in board.get("columns", []):
        if col["id"] == from_col:
            for c in col.get("cards", []):
                if c["id"] == card_id:
                    card = c
                    col["cards"] = [x for x in col["cards"] if x["id"] != card_id]
                    break
    if card:
        for col in board.get("columns", []):
            if col["id"] == to_col:
                col.setdefault("cards", []).append(card)
                _board_save(board)
                return {"success": True}
    return {"success": False, "error": "Move failed"}


# ── Global Bundles (no project slug) ──

@app.get("/bundles", response_class=HTMLResponse)
async def global_bundles_page():
    """Global bundles management page."""
    bundles = list_bundles()
    global_bundles = [b for b in bundles if not b.get("project_slug")]
    rows = ""
    for b in global_bundles:
        s = ", ".join(b["skills"]) if b["skills"] else "\u2014"
        rows += f'''<div class="card"><div class="card-row"><span class="prefix-lg">\U0001f4e6</span><div style="flex:1"><div class="name">{html.escape(b["name"])}</div><div class="meta">{html.escape(b.get("description",""))}</div><div class="meta">Skills: {html.escape(s)}</div></div><button onclick="editGlobalBundle('{html.escape(b["name"])}')" class="pipeline-btn" style="border-color:#3b82f6;color:#93c5fd;background:#172554">\u270f\ufe0f</button><button onclick="deleteGlobalBundle('{html.escape(b["name"])}')" class="pipeline-btn" style="border-color:#ef4444;color:#fca5a5;background:#450a0a">\U0001f5d1\ufe0f</button></div></div>'''
    body = f"""<h2>\U0001f4e6 Bundles globaux</h2>
<p style="color:#94a3b8;font-size:13px">Bundles r\u00e9utilisables par tous les projets. Les profils (product, architect) pointent vers ces bundles.</p>
<div class="card" style="border-color:#3b82f6">
<div class="name" style="margin-bottom:8px">\u2795 Nouveau bundle global</div>
<form id="bundleForm" onsubmit="return createGlobalBundle(event)" style="display:flex;flex-direction:column;gap:8px">
<input name="name" placeholder="Nom du bundle" required style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
<input name="description" placeholder="Description" style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
<div style="margin-top:8px"><div class="name" style="font-size:13px;margin-bottom:4px">\U0001f9e9 Skills</div>
<input id="skillSearch" type="text" placeholder="Filtrer..." oninput="filterSkills()" style="width:100%;padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;box-sizing:border-box;margin-bottom:8px">
<div id="skillList" style="max-height:300px;overflow-y:auto;border:1px solid #1e293b;border-radius:6px;padding:4px;background:#0f172a"><div style="color:#94a3b8;padding:8px;text-align:center">Chargement...</div></div>
<div style="display:flex;gap:8px;margin-top:6px">
<button type="button" onclick="selectAllSkills(true)" style="font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid #334155;background:#1e293b;color:#94a3b8;cursor:pointer">Tout cocher</button>
<button type="button" onclick="selectAllSkills(false)" style="font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid #334155;background:#1e293b;color:#94a3b8;cursor:pointer">Tout d\u00e9cocher</button>
<span id="skillCount" style="font-size:12px;color:#64748b;align-self:center">0 s\u00e9lectionn\u00e9(s)</span>
</div></div>
<button type="submit" class="pipeline-btn" style="align-self:flex-start">\u2705 Cr\u00e9er</button>
</form></div>
<h3 style="margin-bottom:8px;color:#94a3b8">{len(global_bundles)} bundle(s)</h3>
{rows or '<p style="color:#475569">Aucun bundle global.</p>'}
<script>
var allSkills=[];
function loadSkills(){{fetch('/api/skills-list').then(r=>r.json()).then(d=>{{allSkills=d.skills||[];renderSkills('');}})}}
function renderSkills(f){{var l=document.getElementById('skillList');f=(f||'').toLowerCase().trim();var h='';var c=0;for(var i=0;i<allSkills.length;i++){{var s=allSkills[i];var m=!f||s.name.toLowerCase().indexOf(f)!==-1||(s.category&&s.category.toLowerCase().indexOf(f)!==-1)||(s.description&&s.description.toLowerCase().indexOf(f)!==-1);if(!m)continue;c++;var lb=s.name;if(s.category)lb='<span style=\"color:#64748b;font-size:11px\">'+htmlEscape(s.category)+'</span> / '+htmlEscape(s.name);h+='<label style=\"display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:4px;cursor:pointer\" onmouseover=\"this.style.background=\\'#1e293b\\'\" onmouseout=\"this.style.background=\\'transparent\\'\"><input type=\"checkbox\" class=\"skill-cb\" value=\"'+htmlEscape(s.name)+'\" style=\"accent-color:#3b82f6\"><div><span>'+lb+'</span>'+(s.description?'<span style=\"font-size:11px;color:#64748b\">'+htmlEscape(s.description)+'</span>':'')+'</div></label>';}}
l.innerHTML=h||'<div style=\"color:#94a3b8;padding:8px;text-align:center\">Aucun skill.</div>';updateSkillCount();}}
function filterSkills(){{renderSkills(document.getElementById('skillSearch').value);}}
function selectAllSkills(v){{var c=document.querySelectorAll('.skill-cb');for(var i=0;i<c.length;i++)c[i].checked=v;updateSkillCount();}}
function updateSkillCount(){{document.getElementById('skillCount').textContent=document.querySelectorAll('.skill-cb:checked').length+' s\u00e9lectionn\u00e9(s)';}}
function createGlobalBundle(e){{e.preventDefault();var f=document.getElementById('bundleForm');var d={{}};d.name=f.querySelector('[name=\"name\"]').value.trim();d.description=f.querySelector('[name=\"description\"]').value.trim();var sk=[];var c=document.querySelectorAll('.skill-cb:checked');for(var i=0;i<c.length;i++)sk.push(c[i].value);d.skills=sk.join(',');if(!d.name){{showToast('Nom requis','error');return;}}
fetch('/api/bundles/global/create',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}}).then(r=>r.json()).then(res=>{{if(res.success){{showToast('Bundle cr\u00e9\u00e9 !','success');setTimeout(function(){{location.reload()}},1500);}}else{{showToast('Erreur: '+(res.error||'inconnue'),'error');}}}});return false;}}
function deleteGlobalBundle(n){{if(!confirm('Supprimer le bundle \\"'+n+'\\" ?'))return;
fetch('/api/bundles/global/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:n}})}}).then(r=>r.json()).then(res=>{{if(res.success){{showToast('Supprim\\u00e9','success');setTimeout(function(){{location.reload()}},1000);}}else{{showToast('Erreur: '+(res.error||'inconnue'),'error');}}}});}}
function editGlobalBundle(n){{fetch('/api/bundles/global/get/'+n).then(r=>r.json()).then(b=>{{if(!b.success){{showToast('Erreur: '+b.error,'error');return;}}
var f=document.getElementById('bundleForm');f.querySelector('[name="name"]').value=b.bundle.name;f.querySelector('[name="name"]').readOnly=true;f.querySelector('[name="description"]').value=b.bundle.description||'';
var sk=b.bundle.skills||[];renderSkills('');var cbs=document.querySelectorAll('.skill-cb');for(var i=0;i<cbs.length;i++){{cbs[i].checked=sk.indexOf(cbs[i].value)!==-1;}}updateSkillCount();
var btn=f.querySelector('button[type="submit"]');btn.textContent='\U0001f4be Sauvegarder';btn.onclick=function(e){{e.preventDefault();var d={{}};d.name=f.querySelector('[name="name"]').value.trim();d.description=f.querySelector('[name="description"]').value.trim();var skl=[];var c=document.querySelectorAll('.skill-cb:checked');for(var i=0;i<c.length;i++)skl.push(c[i].value);d.skills=skl.join(',');fetch('/api/bundles/global/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}}).then(r=>r.json()).then(res=>{{if(res.success){{showToast('Bundle mis \u00e0 jour ! Red\u00e9marrez la gateway.','success');setTimeout(function(){{location.reload()}},1500);}}else{{showToast('Erreur: '+(res.error||'inconnue'),'error');}}}});}};}});}}
function htmlEscape(s){{if(!s)return '';return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\"/g,'&quot;');}}
loadSkills();
</script>"""
    return _page("Bundles globaux", body)


@app.post("/api/bundles/global/create")
async def api_global_bundle_create(request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    if not name: return {"success": False, "error": "Nom requis"}
    sk = data.get("skills", "").strip()
    skills = [s.strip() for s in sk.split(",") if s.strip()] if sk else []
    b = add_bundle(name=name, skills=skills, description=data.get("description","").strip(), project_slug=None, instruction="")
    return {"success": True, "bundle": b} if b else {"success": False, "error": "Erreur"}


@app.post("/api/bundles/global/delete")
async def api_global_bundle_delete(request: Request):
    data = await request.json()
    return {"success": True} if delete_bundle(data.get("name","")) else {"success": False, "error": "Introuvable"}


@app.get("/api/bundles/global/get/{name}")
async def api_global_bundle_get(name: str):
    """API: Get a global bundle by name."""
    b = get_bundle(name)
    if b and not b.get("project_slug"):
        return {"success": True, "bundle": b}
    return {"success": False, "error": "Bundle introuvable"}


@app.post("/api/bundles/global/update")
async def api_global_bundle_update(request: Request):
    """API: Update a global bundle (skills, description)."""
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return {"success": False, "error": "Nom requis"}
    sk = data.get("skills", "").strip()
    skills = [s.strip() for s in sk.split(",") if s.strip()] if sk else []
    b = add_bundle(name=name, skills=skills, description=data.get("description","").strip(), project_slug=None, instruction="")
    return {"success": True, "bundle": b} if b else {"success": False, "error": "Erreur"}


@app.post("/api/{slug}/delete")
async def api_project_delete(slug: str):
    """API: Delete a project — Discord channels, profiles, Kanban, code, config."""
    import shutil, yaml
    proj = get_project(slug)
    if not proj:
        return {"success": False, "error": "Projet introuvable"}

    work_dir = proj["work_dir"]
    errors = []
    done = []

    # 1. Delete Discord channels
    profiles_db = list_profile_templates(slug)
    for p in profiles_db:
        cid = p.get("channel_id", "")
        if cid:
            if _discord_delete_channel(cid):
                done.append(f"Channel {p['name']} supprime")
            else:
                errors.append(f"Channel {p['name']} non trouve")

    # 2. Delete Hermes profiles (disk)
    for p in profiles_db:
        pdir = os.path.expanduser(f"~/.hermes/profiles/{p['name']}")
        if os.path.isdir(pdir):
            shutil.rmtree(pdir, ignore_errors=True)
            done.append(f"Profil {p['name']} supprime")

    # 3. Clean config.yaml (channel_prompts + bindings + free_response)
    config_path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        d = cfg.get("discord", {})
        channel_ids = [str(p.get("channel_id","")) for p in profiles_db if p.get("channel_id")]
        # Remove channel_prompts
        prompts = d.get("channel_prompts", {})
        for cid in channel_ids:
            prompts.pop(cid, None)
        # Remove from bindings
        bindings = d.get("channel_skill_bindings", [])
        d["channel_skill_bindings"] = [b for b in bindings if str(b.get("id","")) not in channel_ids]
        # Remove from free_response
        frc = d.get("free_response_channels", "")
        if frc:
            existing = [x.strip() for x in frc.split(",")]
            existing = [x for x in existing if x not in channel_ids]
            d["free_response_channels"] = ",".join(existing)
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        done.append("Config nettoyee")
    except Exception as e:
        errors.append(f"Config: {e}")

    # 4. Clean channel_prompts.json
    try:
        json_path = os.path.expanduser("~/.legion/channel_prompts.json")
        if os.path.isfile(json_path):
            with open(json_path) as f:
                jp = json.load(f)
            channel_ids = [str(p.get("channel_id","")) for p in profiles_db if p.get("channel_id")]
            for cid in channel_ids:
                jp.pop(cid, None)
            with open(json_path, "w") as f:
                json.dump(jp, f, indent=2, ensure_ascii=False)
                f.write("\n")
            done.append("channel_prompts.json nettoye")
    except Exception as e:
        errors.append(f"channel_prompts.json: {e}")

    # 5. Delete from Legion DB (profile_templates, project_profiles, features, bundles per-project, project)
    from core.db import get_conn
    conn = get_conn()
    try:
        conn.execute("DELETE FROM profile_templates WHERE project_slug=?", (slug,))
        conn.execute("DELETE FROM project_profiles WHERE project_slug=?", (slug,))
        conn.execute("DELETE FROM features WHERE project_slug=?", (slug,))
        conn.execute("DELETE FROM feature_meta WHERE project_slug=?", (slug,))
        conn.execute("DELETE FROM bundles WHERE project_slug=?", (slug,))
        conn.execute("DELETE FROM projects WHERE slug=?", (slug,))
        conn.commit()
        done.append("DB nettoyee")
    except Exception as e:
        errors.append(f"DB: {e}")
    finally:
        conn.close()

    # 6. Delete Kanban board
    kanban_dir = os.path.expanduser(f"~/.hermes/kanban/boards/{slug}")
    if os.path.isdir(kanban_dir):
        shutil.rmtree(kanban_dir, ignore_errors=True)
        done.append("Board Kanban supprime")

    # 7. Delete work directory
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
        done.append("Dossier projet supprime")

    # 8. Delete features.db
    features_db = os.path.expanduser(f"~/.hermes/kanban/boards/{slug}/features.db")
    if os.path.isfile(features_db):
        os.remove(features_db)

    return {
        "success": True,
        "message": " | ".join(done),
        "errors": errors if errors else None,
    }


@app.get("/{slug}", response_class=HTMLResponse)
async def project_detail(slug: str):
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content=_page(
            "Introuvable",
            f"<p>Projet '{slug}' introuvable.</p>"
            f"<a href='/' class='back-link'>← Retour</a>"
        ), status_code=404)

    status = proj.get("status", "draft")

    # Status badge
    if status == "draft":
        status_badge = '<span class="stage-design" style="display:inline-block;padding:4px 10px">📝 Brouillon</span>'
        status_desc = '<p style="color:#94a3b8;font-size:14px;margin:8px 0">Projet en phase de cadrage. Canaux <b>#product</b> et <b>#architect</b> disponibles sur Discord.</p>'
    else:
        status_badge = '<span class="stage-done" style="display:inline-block;padding:4px 10px">✅ Actif</span>'
        status_desc = ""

    features = list_features(slug)
    profiles = proj.get("profiles", {})

    # Stats
    counts = {}
    for f in features:
        stage = _pipeline_stage(slug, f['prefix']).lower()
        counts[stage] = counts.get(stage, 0) + 1

    stats_html = ""
    stage_labels = {
        "backlog": "Backlog", "explore": "Explore", "spec": "Spec",
        "design": "Design", "architect": "Architect", "implement": "Implement", "done": "Done"
    }
    for s in ["backlog", "explore", "spec", "design", "architect", "implement", "done"]:
        if s in counts:
            stats_html += f'<div class="stat-card"><div class="stat-value">{counts[s]}</div><div class="stat-label">{stage_labels.get(s, s)}</div></div>'

    # Features list with progress bars
    features_html = ""
    for f in features:
        stage = _pipeline_stage(slug, f['prefix'])
        stage_key = stage.lower()
        stage_class = f"stage-{stage_key}" if stage_key in stage_labels else "stage-backlog"
        stage_label = stage_labels.get(stage_key, stage)

        # Count tickets for progress bar
        done, total, _ = _count_tickets(slug, f['prefix'])

        features_html += f"""
        <div class="card">
            <div class="card-row">
                <span class="prefix">{f['prefix']}</span>
                <div>
                    <div class="name">{f['name']}</div>
                    <div class="meta">{f['slug']}</div>
                </div>
            </div>
            <span class="stage {stage_class}">{stage_label}</span>
            {_progress_bar(done, total)}
            <button class="pipeline-btn" data-slug="{slug}" data-prefix="{f['prefix']}" onclick="runPipeline(this)">▶ Pipeline</button>
        </div>"""

    if not features_html:
        features_html = '<p style="color: #64748b;">Aucune feature.</p>'

    proj_link = f'<a href="/" class="back-link">← Tous les projets</a>'
    tabs = _nav_tabs(slug, "features")

    # Activate section for draft projects
    activate_html = ""
    if status == "draft":
        activate_html = f"""<div class="card" style="border-color:#f59e0b;margin-bottom:16px">
    <div class="name" style="color:#fde68a;margin-bottom:8px">🚀 Activation du projet</div>
    <p style="color:#94a3b8;font-size:13px;margin-bottom:12px">
        Une fois la spec produit et l'architecture validées, active le projet pour créer
        tous les profils (backend, frontend, design, master-agent), les canaux Discord,
        le board Kanban, et enregistrer le projet dans la pipeline Legion.
    </p>
    <button class="pipeline-btn" onclick="activateProject('{slug}')" id="activateBtn"
            style="border-color:#22c55e;color:#bbf7d0;background:#052e16">
        🚀 Activer le projet
    </button>
    <div class="form-loader" id="activateLoader">
        <div class="spinner"></div>
        <span id="activateProgress">Activation en cours...</span>
    </div>
    <div class="form-result" id="activateResult"></div>
</div>

<script>
function activateProject(slug) {{
    var btn = document.getElementById('activateBtn');
    var loader = document.getElementById('activateLoader');
    var result = document.getElementById('activateResult');
    btn.disabled = true;
    loader.className = 'form-loader show';
    result.className = 'form-result';
    document.getElementById('activateProgress').textContent = 'Création des profils et canaux...';
    fetch('/api/' + slug + '/activate', {{ method: 'POST' }})
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{
            loader.className = 'form-loader';
            btn.disabled = false;
            if (d.success) {{
                result.className = 'form-result success show';
                result.innerHTML = '✅ Projet activé ! <a href="/' + slug + '">Rafraîchir →</a>';
            }} else {{
                result.className = 'form-result error show';
                result.textContent = '❌ ' + (d.error || 'Erreur');
            }}
        }})
        .catch(function(err) {{
            loader.className = 'form-loader';
            btn.disabled = false;
            result.className = 'form-result error show';
            result.textContent = '❌ Erreur réseau: ' + err.message;
        }});
}}
function deleteProject(slug) {{
    if (!confirm('Supprimer definitivement le projet ' + slug + ' ? Canaux Discord, profils, board Kanban, et code seront supprimes. Action irreversible.')) return;
    if (!confirm('Es-tu vraiment sur ? Toutes les donnees du projet seront perdues.')) return;
    var btn = document.getElementById('deleteBtn');
    var loader = document.getElementById('deleteLoader');
    var result = document.getElementById('deleteResult');
    btn.disabled = true;
    loader.className = 'form-loader show';
    result.className = 'form-result';
    document.getElementById('deleteProgress').textContent = 'Suppression des canaux Discord...';
    fetch('/api/' + slug + '/delete', {{ method: 'POST' }})
        .then(r => r.json())
        .then(d => {{
            loader.className = 'form-loader';
            btn.disabled = false;
            if (d.success) {{
                result.className = 'form-result success show';
                result.innerHTML = 'Projet supprime ! <a href=\\"/\\">Retour a l\\'accueil →</a>';
            }} else {{
                result.className = 'form-result error show';
                result.textContent = 'Erreur: ' + (d.error || 'inconnue');
            }}
        }})
        .catch(function(err) {{
            loader.className = 'form-loader';
            btn.disabled = false;
            result.className = 'form-result error show';
            result.textContent = 'Erreur reseau: ' + err.message;
        }});
}}
</script>"""

    body = f"""
    <div class="project-header">{proj['name']} {status_badge}</div>
    <div class="project-meta">{status_desc}
        {proj['project_type']} · {proj['work_dir']} · {len(profiles)} profils
    </div>
    {activate_html}
    <div class="card" style="border-color:#ef4444;margin-top:16px">
        <div class="name" style="color:#fca5a5;margin-bottom:8px">🗑️ Zone dangereuse</div>
        <p style="color:#94a3b8;font-size:13px;margin-bottom:12px">
            Supprime les canaux Discord, profils Hermes, board Kanban et données du projet.
            Les bundles et templates globaux ne sont pas affectés.
        </p>
        <button class="pipeline-btn" onclick="deleteProject('{slug}')" id="deleteBtn"
                style="border-color:#ef4444;color:#fca5a5;background:#450a0a">
            🗑️ Supprimer le projet
        </button>
        <div class="form-loader" id="deleteLoader">
            <div class="spinner"></div>
            <span id="deleteProgress">Suppression...</span>
        </div>
        <div class="form-result" id="deleteResult"></div>
    </div>
    {tabs}
    <div class="stats-grid">{stats_html}</div>
    {features_html}
    """

    return _page(proj['name'], body, proj_link)


@app.get("/kanban/{slug}", response_class=HTMLResponse)
async def kanban_board(slug: str):
    """Kanban board view for a project."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content=_page(
            "Introuvable",
            f"<p>Projet '{slug}' introuvable.</p>"
            f"<a href='/' class='back-link'>← Retour</a>"
        ), status_code=404)

    db_path = _kanban_path(slug)
    if not db_path or not os.path.isfile(db_path):
        return HTMLResponse(content=_page(
            "Kanban", 
            f"<p>Aucun board Kanban pour '{slug}'.</p>"
            f"<a href='/{slug}' class='back-link'>← Retour au projet</a>"
        ))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get all non-archived tasks
    tasks = conn.execute("""
        SELECT id, title, status, assignee, priority, body
        FROM tasks WHERE status NOT IN ('archived')
        ORDER BY priority DESC, created_at ASC
    """).fetchall()
    conn.close()

    # Group by status
    columns_order = ["todo", "in_progress", "blocked", "done"]
    status_labels = {"todo": "À faire", "in_progress": "En cours", "blocked": "Bloqué", "done": "Terminé"}
    columns = {s: [] for s in columns_order}
    for t in tasks:
        s = t["status"]
        if s in columns:
            columns[s].append(t)

    # Build HTML
    profiles = proj.get("profiles", {})
    proj_link = f'<a href="/{slug}" class="back-link">← {proj["name"]}</a>'

    tabs = _nav_tabs(slug, "kanban")

    cols_html = ""
    for s in columns_order:
        items = columns[s]
        label = status_labels.get(s, s)
        col_class = f"kanban-col-{s}"
        
        cards_html = ""
        for t in items:
            title = t["title"]
            assignee = t["assignee"] or ""
            # Extract prefix for color coding
            prefix = ""
            if "]" in title:
                prefix = title.split("]")[0].lstrip("[").strip()
            elif "-" in title:
                prefix = title.split("-")[0]
            
            # Check if done
            is_done = s == "done"
            
            cards_html += f"""
            <div class="kanban-card{' feature-highlight' if prefix else ''}">
                <div class="title">{title}</div>
                <div class="meta">
                    {'✅ Terminé' if is_done else ''}
                    {'🔴 Bloqué' if s == 'blocked' else ''}
                    {f'priorité {t["priority"]}' if t['priority'] > 0 else ''}
                </div>
                {f'<div class="assignee">{assignee}</div>' if assignee else ''}
            </div>"""

        cols_html += f"""
        <div class="kanban-col {col_class}">
            <div class="kanban-col-header">
                {label}
                <span class="count">{len(items)}</span>
            </div>
            {cards_html if cards_html else '<div style="color:#475569;font-size:12px;padding:8px">Aucune tâche</div>'}
        </div>"""

    total = sum(len(v) for v in columns.values())
    body = f"""
    <div class="project-header">{proj['name']} · Board</div>
    <div class="project-meta">{len(tasks)} tâches</div>
    {tabs}
    <div class="kanban-grid">{cols_html}</div>
    """

    return _page(f"Kanban · {proj['name']}", body, proj_link)


# ── Docs routes ──


@app.get("/{slug}/docs", response_class=HTMLResponse)
async def docs_list(slug: str):
    """List all docs grouped by feature."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content=_page("Introuvable", "<p>Projet introuvable.</p>"
            "<a href='/' class='back-link'>← Retour</a>"), status_code=404)

    work_dir = proj.get("work_dir", "")
    features = list_features(slug)
    docs = _scan_project_docs(work_dir, features)
    proj_link = f'<a href="/{slug}" class="back-link">← {proj["name"]}</a>'
    tabs = _nav_tabs(slug, "docs")

    if not docs:
        body = f"""
        <div class="project-header">{proj['name']}</div>
        {tabs}
        <p style="color:#64748b;padding:2rem;text-align:center">Aucun document trouvé dans <code>docs/</code>.</p>
        """
        return _page(f"Docs · {proj['name']}", body, proj_link)

    rows = ""
    for feat_slug, feat in docs.items():
        prefix_tag = f'<span class="prefix" style="font-size:11px">{feat["prefix"]}</span>' if feat["prefix"] else ""
        doc_list = ""
        for d in feat["docs"]:
            icon = _DOC_ICONS.get(d["type"], "📄")
            size_str = f"{d['size']//1024}KB" if d['size'] > 1024 else f"{d['size']}B"
            viewer_url = f"/{slug}/docs/view?path={d['path']}"
            doc_list += f"""<a href="{viewer_url}" class="doc-item" target="_blank">
                <span class="doc-icon">{icon}</span>
                <span class="doc-title">{html.escape(d['title'])}</span>
                <span class="doc-meta">{d['type']} · {size_str}</span>
            </a>"""

        rows += f"""
        <details class="doc-group" open>
            <summary class="doc-summary">
                <span>{icon} {html.escape(feat['name'])}</span>
                {prefix_tag}
                <span style="color:#64748b;font-size:12px;margin-left:8px">{len(feat['docs'])} docs</span>
            </summary>
            <div class="doc-list">{doc_list}</div>
        </details>"""

    body = f"""
    <div class="project-header">{proj['name']}</div>
    {tabs}
    <div class="docs-grid">{rows}</div>
    <style>
    .doc-group {{
        background:#1e293b; border-radius:8px; margin-bottom:8px; overflow:hidden;
    }}
    .doc-summary {{
        padding:10px 14px; cursor:pointer; display:flex; align-items:center; gap:8px;
        font-weight:600; color:#f8fafc; user-select:none;
    }}
    .doc-summary::-webkit-details-marker {{ color:#64748b; }}
    .doc-list {{
        padding:0 14px 10px; display:flex; flex-direction:column; gap:4px;
    }}
    .doc-item {{
        display:flex; align-items:center; gap:8px; padding:6px 10px;
        border-radius:6px; text-decoration:none; color:#cbd5e1;
        transition:background .15s;
    }}
    .doc-item:hover {{ background:#334155; color:#f8fafc; }}
    .doc-icon {{ font-size:16px; min-width:24px; }}
    .doc-title {{ flex:1; font-size:14px; }}
    .doc-meta {{ color:#64748b; font-size:11px; }}
    </style>
    """
    return _page(f"Docs · {proj['name']}", body, proj_link)


@app.get("/{slug}/docs/view", response_class=HTMLResponse)
async def docs_view(slug: str, path: str):
    """View a document (markdown rendered or mockup iframe)."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content="Projet introuvable", status_code=404)
    work_dir = proj.get("work_dir", "")
    full_path = os.path.normpath(os.path.join(work_dir, "docs", path))
    if not full_path.startswith(os.path.normpath(os.path.join(work_dir, "docs"))):
        return HTMLResponse(content="Chemin invalide", status_code=403)

    ext = os.path.splitext(path)[1].lower()
    title = os.path.basename(path)

    if ext == ".md":
        content = _render_markdown(full_path)
        body = f"""
        <div class="doc-toolbar">
            <a href="/{slug}/docs" class="back-link">← Docs</a>
            <span style="color:#94a3b8;font-size:13px;margin-left:12px">{html.escape(path)}</span>
            <a href="/{slug}/docs/raw?path={path}" target="_blank" style="margin-left:auto;color:#64748b;font-size:12px">Source</a>
        </div>
        <div class="md-content">{content}</div>
        <style>
        .md-content {{
            padding:1rem 1.5rem; max-width:800px; margin:0 auto; line-height:1.7;
        }}
        .md-content h1 {{ color:#f8fafc; font-size:24px; margin:24px 0 12px; }}
        .md-content h2 {{ color:#e2e8f0; font-size:18px; margin:20px 0 8px; border-bottom:1px solid #334155; padding-bottom:6px; }}
        .md-content h3 {{ color:#cbd5e1; font-size:16px; margin:16px 0 6px; }}
        .md-content p {{ margin:8px 0; color:#94a3b8; }}
        .md-content code {{ background:#0f172a; padding:2px 6px; border-radius:4px; font-size:13px; color:#e2e8f0; }}
        .md-content pre {{ background:#0f172a; padding:12px; border-radius:8px; overflow-x:auto; }}
        .md-content pre code {{ background:none; padding:0; }}
        .md-content table {{ border-collapse:collapse; width:100%; margin:12px 0; }}
        .md-content th, .md-content td {{ border:1px solid #334155; padding:8px 12px; text-align:left; }}
        .md-content th {{ background:#1e293b; color:#e2e8f0; }}
        .doc-toolbar {{
            display:flex; align-items:center; padding:8px 16px;
            background:#1e293b; border-bottom:1px solid #334155;
        }}
        </style>
        """
    elif ext in (".html", ".htm"):
        body = f"""
        <div class="doc-toolbar">
            <a href="/{slug}/docs" class="back-link">← Docs</a>
            <span style="color:#94a3b8;font-size:13px;margin-left:12px">{html.escape(path)}</span>
            <button onclick="toggleWidth()" id="widthToggle" style="margin-left:auto;background:#334155;color:#cbd5e1;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px">📱 Pleine largeur</button>
        </div>
        <div id="mockupContainer" style="display:flex;justify-content:center;height:calc(100vh - 120px)">
            <iframe id="mockupFrame" src="/{slug}/docs/raw?path={path}"
                style="width:390px;height:100%;border:none;background:#fff;border-radius:8px;box-shadow:0 0 0 1px #334155;transition:width .2s">
            </iframe>
        </div>
        <script>
        function toggleWidth() {{
            var frame = document.getElementById('mockupFrame');
            var btn = document.getElementById('widthToggle');
            if (frame.style.width === '390px') {{
                frame.style.width = '100%';
                frame.style.borderRadius = '0';
                frame.style.boxShadow = 'none';
                btn.textContent = '📱 Vue mobile';
            }} else {{
                frame.style.width = '390px';
                frame.style.borderRadius = '8px';
                frame.style.boxShadow = '0 0 0 1px #334155';
                btn.textContent = '📱 Pleine largeur';
            }}
        }}
        </script>
        """
    elif ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        body = f"""
        <div class="doc-toolbar">
            <a href="/{slug}/docs" class="back-link">← Docs</a>
            <span style="color:#94a3b8;font-size:13px;margin-left:12px">{html.escape(path)}</span>
        </div>
        <div style="padding:1rem;text-align:center">
            <img src="/{slug}/docs/raw?path={path}" style="max-width:100%;max-height:80vh;border-radius:8px;box-shadow:0 4px 20px rgba(0,0,0,.3)">
        </div>
        """
    else:
        body = "<p>Type de fichier non supporté.</p>"

    return _page(title, body, f'<a href="/{slug}/docs" class="back-link">← Docs</a>')


@app.get("/{slug}/docs/raw")
async def docs_raw(slug: str, path: str):
    """Serve raw doc file (for iframe mockups, images)."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content="Introuvable", status_code=404)
    work_dir = proj.get("work_dir", "")
    full_path = os.path.normpath(os.path.join(work_dir, "docs", path))
    if not full_path.startswith(os.path.normpath(os.path.join(work_dir, "docs"))):
        return HTMLResponse(content="Chemin invalide", status_code=403)
    if not os.path.isfile(full_path):
        return HTMLResponse(content="Fichier introuvable", status_code=404)

    ext = os.path.splitext(path)[1].lower()
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "text/plain"

    with open(full_path, "rb") as f:
        data = f.read()

    from fastapi.responses import Response
    return Response(content=data, media_type=mime)


@app.get("/expo/{slug}", response_class=HTMLResponse)
async def expo_view(slug: str):
    """Expo management page for a project."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content=_page(
            "Introuvable",
            f"<p>Projet '{slug}' introuvable.</p>"
            f"<a href='/' class='back-link'>← Retour</a>"
        ), status_code=404)

    work_dir = proj.get("work_dir", "")
    status = _expo_status(work_dir) if work_dir else {"running": False, "pid": None, "port": None, "logs": ""}
    profiles = proj.get("profiles", {})
    proj_link = f'<a href="/{slug}" class="back-link">← {proj["name"]}</a>'
    tabs = _nav_tabs(slug, "expo")

    # Status indicator
    dot_class = "running" if status["running"] else "stopped"
    status_text = "🟢 En cours" if status["running"] else "🔴 Arrêté"
    status_detail = ""
    if status["running"]:
        status_detail = f'<div class="expo-detail">PID {status["pid"]} · Port {status["port"] or "N/A"}</div>'

    # Logs
    logs_html = ""
    if status["logs"]:
        for line in status["logs"].split("\n"):
            css_class = ""
            if "error" in line.lower() or "fail" in line.lower():
                css_class = "error"
            elif "success" in line.lower() or "start" in line.lower():
                css_class = "success"
            elif "info" in line.lower() or "log" in line.lower():
                css_class = "info"
            logs_html += f'<div class="{css_class}">{line}</div>\n'

    body = f"""
    <div class="project-header">{proj['name']} · Expo</div>
    <div class="project-meta">{work_dir}</div>
    {tabs}

    <div class="expo-status-card">
        <div class="expo-status-row">
            <span class="expo-dot {dot_class}"></span>
            <div>
                <div class="expo-status-label">{status_text}</div>
                {status_detail}
            </div>
        </div>

        <div class="expo-actions">
            <a href="/expo/{slug}/start" class="expo-btn primary" onclick="return confirm('Démarrer Expo ?')">
                ▶ Démarrer
            </a>
            <a href="/expo/{slug}/stop" class="expo-btn danger" onclick="return confirm('Arrêter Expo ?')">
                ⏹ Arrêter
            </a>
            <a href="/expo/{slug}/status" class="expo-btn">
                🔄 Rafraîchir
            </a>
        </div>
    </div>

    {'<div class="expo-status-card" style="text-align:center"><div style="font-size:14px;font-weight:600;color:#f1f5f9;margin-bottom:8px">📱 QR Code Expo</div><img id="expo-qr" src="' + _expo_qr_data_uri(status["port"] or 8082) + '" style="width:200px;height:200px;border-radius:8px;display:block;margin:0 auto" alt="Expo QR Code"/><div style="color:#64748b;font-size:11px;margin-top:8px">Scanne avec Expo Go sur le même réseau</div></div>' if status["running"] else ''}

    <div class="expo-status-card">
        <div style="font-size:14px;font-weight:600;color:#f1f5f9;margin-bottom:8px">📦 Actions</div>
        <div class="expo-actions">
            <a href="/expo/{slug}/build?platform=ios" class="expo-btn" onclick="return confirm('Lancer EAS Build iOS ? Cela peut prendre du temps.')">
                📱 Build iOS
            </a>
            <a href="/expo/{slug}/build?platform=android" class="expo-btn" onclick="return confirm('Lancer EAS Build Android ?')">
                🤖 Build Android
            </a>
            <a href="/expo/{slug}/update" class="expo-btn primary" onclick="return confirm('Publier une mise à jour OTA ?')">
                ☁️ EAS Update
            </a>
        </div>
    </div>

    <div class="expo-status-card">
        <div style="font-size:14px;font-weight:600;color:#f1f5f9;margin-bottom:8px">📜 Logs</div>
        <div class="expo-log-box">
            {logs_html if logs_html else '<div style="color:#475569">Aucun log</div>'}
        </div>
        <div class="expo-detail" style="margin-top:8px">
            {_expo_log_file(work_dir) if work_dir else ""}
        </div>
    </div>
    """

    return _page(f"Expo · {proj['name']}", body, proj_link)


@app.get("/expo/{slug}/start")
async def expo_start(slug: str, port: int = 0):
    """Start Expo dev server. Auto-picks a free port if default is taken."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content="Projet introuvable", status_code=404)
    work_dir = proj.get("work_dir", "")
    if not work_dir:
        return HTMLResponse(content="Pas de work_dir", status_code=400)

    import subprocess, os, signal, time

    # ── Determine default port per slug ──
    _DEFAULT_PORTS = {"wonderfamilly": 8081, "skull-game": 8082, "easybuild": 8083}
    base_port = port or _DEFAULT_PORTS.get(slug, 8080)

    # ── Find a free port ──
    chosen_port = base_port
    port_file = os.path.join(work_dir, ".expo", "dev-server.port")

    for attempt in range(20):  # try up to 20 ports
        r = subprocess.run(
            f"ss -tlnp 'sport = :{chosen_port}' 2>/dev/null | grep -q .",
            shell=True, capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            # Port is free
            break

        # Port is taken — check what's on it
        r2 = subprocess.run(
            f"ss -tlnp 'sport = :{chosen_port}' 2>/dev/null | grep -Po 'pid=\\K[0-9]+' | head -1",
            shell=True, capture_output=True, text=True, timeout=3,
        )
        pid_on_port = r2.stdout.strip()

        if pid_on_port:
            # Try to find the workdir of that process via /proc
            try:
                cwd = os.readlink(f"/proc/{pid_on_port}/cwd")
                if cwd == work_dir:
                    # Same project — kill it and reuse port
                    os.kill(int(pid_on_port), signal.SIGTERM)
                    time.sleep(1)
                    break
            except (OSError, FileNotFoundError):
                pass

        # Different project (or unknown) — try next port
        chosen_port += 1

    # ── Clean up pid file ──
    pid_file = os.path.join(work_dir, ".expo", "dev-server.pid")
    if os.path.isfile(pid_file):
        os.remove(pid_file)
    if os.path.isfile(port_file):
        os.remove(port_file)

    # ── Start Expo ──
    os.makedirs(os.path.join(work_dir, ".expo"), exist_ok=True)
    log_file = os.path.join(work_dir, ".expo", "dev-server.log")

    expo_bin = os.path.join(work_dir, "node_modules", ".bin", "expo")
    if not os.path.isfile(expo_bin):
        expo_bin = "npx expo"

    proc = subprocess.Popen(
        f"cd {work_dir} && {expo_bin} start --clear --port {chosen_port} > {log_file} 2>&1",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # ── Write PID + port files ──
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    with open(port_file, "w") as f:
        f.write(str(chosen_port))

    # ── Wait and verify ──
    time.sleep(2)
    if proc.poll() is not None:
        err = ""
        if os.path.isfile(log_file):
            with open(log_file) as f:
                err = f.read()
    else:
        # Also capture actual node PID on the chosen port
        r = subprocess.run(
            f"ss -tlnp 'sport = :{chosen_port}' 2>/dev/null | grep -Po 'pid=\\K[0-9]+' | head -1",
            shell=True, capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            with open(pid_file, "w") as f:
                f.write(r.stdout.strip())

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/expo/{slug}", status_code=303)


@app.get("/expo/{slug}/stop")
async def expo_stop(slug: str):
    """Stop Expo dev server."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content="Projet introuvable", status_code=404)
    work_dir = proj.get("work_dir", "")

    import subprocess, os, signal

    # Read the port this project was using
    port_file = os.path.join(work_dir, ".expo", "dev-server.port")
    port = None
    if os.path.isfile(port_file):
        try:
            with open(port_file) as f:
                port = f.read().strip()
        except Exception:
            pass

    if port:
        # Kill by recorded port
        subprocess.run(["fuser", "-k", "-TERM", f"{port}/tcp"],
                       capture_output=True, timeout=5)
    else:
        # Fallback: kill by PID file
        pid_file = os.path.join(work_dir, ".expo", "dev-server.pid")
        if os.path.isfile(pid_file):
            try:
                with open(pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/expo/{slug}", status_code=303)


@app.get("/expo/{slug}/build")
async def expo_build(slug: str, platform: str = "all"):
    """Run EAS build."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content="Projet introuvable", status_code=404)
    work_dir = proj.get("work_dir", "")

    import subprocess, threading
    log_file = os.path.join(work_dir, ".expo", "build.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    def _run_build():
        with open(log_file, "w") as f:
            subprocess.run(
                f"cd {work_dir} && npx eas build --platform {platform} --non-interactive",
                shell=True, timeout=7200, stdout=f, stderr=subprocess.STDOUT,
            )

    threading.Thread(target=_run_build, daemon=True).start()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/expo/{slug}", status_code=303)


@app.get("/expo/{slug}/update")
async def expo_update(slug: str):
    """Publish EAS Update (OTA)."""
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content="Projet introuvable", status_code=404)
    work_dir = proj.get("work_dir", "")

    import subprocess, threading
    from datetime import datetime

    log_file = os.path.join(work_dir, ".expo", "update.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    def _run_update():
        message = f"Update {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        with open(log_file, "w") as f:
            subprocess.run(
                f"cd {work_dir} && npx eas update --branch main --message '{message}' --non-interactive",
                shell=True, timeout=600, stdout=f, stderr=subprocess.STDOUT,
            )

    threading.Thread(target=_run_update, daemon=True).start()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/expo/{slug}", status_code=303)


@app.get("/expo/{slug}/refresh")
def expo_refresh_json(slug: str):
    """JSON endpoint for auto-refresh (status + logs + QR)."""
    from fastapi.responses import JSONResponse
    proj = get_project(slug)
    if not proj:
        return JSONResponse({"running": False, "logs_html": ""})
    work_dir = proj.get("work_dir", "")
    status = _expo_status(work_dir) if work_dir else {"running": False, "pid": None, "port": None, "logs": ""}

    # Build logs HTML
    logs_html = ""
    if status.get("logs"):
        for line in status["logs"].split("\n"):
            css_class = ""
            if "error" in line.lower() or "fail" in line.lower():
                css_class = "error"
            elif "success" in line.lower() or "start" in line.lower():
                css_class = "success"
            elif "info" in line.lower() or "log" in line.lower():
                css_class = "info"
            logs_html += f'<div class="{css_class}">{line}</div>\n'

    # Generate QR URI if running
    qr_uri = ""
    if status["running"] and status.get("port"):
        qr_uri = _expo_qr_data_uri(status["port"])

    return JSONResponse({
        "running": status["running"],
        "pid": status.get("pid"),
        "port": status.get("port"),
        "qr_uri": qr_uri,
        "logs_html": logs_html if logs_html else '<div style="color:#475569">Aucun log</div>',
    })


@app.get("/expo/{slug}/status")
async def expo_refresh(slug: str):
    """Redirect back to expo page (refreshes status)."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/expo/{slug}", status_code=303)



@app.get("/api/skills-list")
async def api_skills_list():
    """API: List all available skills for the bundle picker."""
    skills = list_all_skills()
    return {"skills": skills}


@app.get("/pipeline/{slug}", response_class=HTMLResponse)
async def pipeline_config_page(slug: str):
    proj = get_project(slug)
    if not proj:
        return _page("Introuvable", "<p>Projet introuvable.</p>")
    profiles = list_profile_templates(slug)
    config = proj.get("pipeline_config", {})
    stage_order = config.get("stage_order", ["EXPLORE","SPEC","DESIGN","ARCHITECT","IMPLEMENT","TEST"])
    stage_profiles = config.get("stage_profiles", {})
    doc_patterns = config.get("stage_doc_patterns", {})
    body_templates = config.get("body_templates", {})
    nav = _nav_tabs(slug, "pipeline")
    back = f'<a href="/{slug}" class="back-link">← Retour</a>'
    stage_labels = {"EXPLORE":"🔍 Exploration","SPEC":"📝 Spec","DESIGN":"🎨 Design","ARCHITECT":"🏗️ Architecture","IMPLEMENT":"💻 Implémentation","TEST":"✅ Test"}
    rows = ""
    for i, stage in enumerate(stage_order):
        label = stage_labels.get(stage, stage)
        assigned = stage_profiles.get(stage, "")
        opts = '<option value="">(Non assigné)</option>'
        for p in profiles:
            sel = " selected" if p["name"] == assigned else ""
            pname = p["name"]
            prole = p.get("role", "")
            plabel = f"{prole} ({pname})" if prole else pname
            opts += f'<option value="{html.escape(pname)}"{sel}>{html.escape(plabel)}</option>'
        dp = html.escape(doc_patterns.get(stage, ""))
        bt = html.escape(body_templates.get(stage, ""))
        rows += f"""<div class="card" style="margin-bottom:8px">
            <div class="card-row" style="align-items:flex-start">
                <div style="flex:1;display:flex;flex-direction:column;gap:6px">
                    <div><span class="name">{label}</span> <span class="meta">étape {i+1}/{len(stage_order)}</span></div>
                    <div><select class="sp" data-s="{stage}" style="width:100%;padding:5px;border-radius:4px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:12px">{opts}</select></div>
                    <div><input type="text" class="sd" data-s="{stage}" value="{dp}" placeholder="docs/.../{{slug}}.md" style="width:100%;padding:5px;border-radius:4px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:12px;box-sizing:border-box"></div>
                    <div><textarea class="sb" data-s="{stage}" rows="1" placeholder="Instructions agent..." style="width:100%;padding:5px;border-radius:4px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:12px;box-sizing:border-box;resize:vertical">{bt}</textarea></div>
                </div>
            </div>
        </div>"""
    body = f"""{back}
    <h2>🔧 Pipeline — {html.escape(proj["name"])}</h2>
    {nav}
    <p style="color:#94a3b8;font-size:13px">Configure les étapes et assigne un agent.</p>
    {rows}
    <div style="display:flex;gap:8px;margin:12px 0">
        <button onclick="saveP()" class="pipeline-btn" style="border-color:#22c55e;color:#bbf7d0;background:#052e16">💾 Sauvegarder</button>
    </div>
    <div class="form-result" id="sr"></div>
    <script>
    function saveP() {{
        var c = {{stage_order:{json.dumps(stage_order)},stage_profiles:{{}},stage_doc_patterns:{{}},body_templates:{{}}}};
        document.querySelectorAll('.sp').forEach(function(e){{var s=e.getAttribute("data-s");if(e.value)c.stage_profiles[s]=e.value;}});
        document.querySelectorAll('.sd').forEach(function(e){{var s=e.getAttribute("data-s");if(e.value)c.stage_doc_patterns[s]=e.value;}});
        document.querySelectorAll('.sb').forEach(function(e){{var s=e.getAttribute("data-s");if(e.value)c.body_templates[s]=e.value;}});
        document.getElementById("sr").className="form-result";
        fetch('/api/{slug}/pipeline-config',{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(c)}})
        .then(function(r){{return r.json()}}).then(function(d){{var r=document.getElementById("sr");r.innerHTML=d.success?'<span style="color:#bbf7d0">✅ OK</span>':'<span style="color:#fca5a5">❌ '+d.error+'</span>';r.className="form-result show";}});
    }}
    </script>"""
    return _page(f"Pipeline {slug}", body, back)


@app.post("/api/{slug}/pipeline-config")
async def api_pipeline_config(slug: str, request: Request):
    data = await request.json()
    try:
        proj = set_project_pipeline_config(slug, data)
        return {"success": True, "config": proj.get("pipeline_config", {})}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/gateway/restart")
async def api_gateway_restart():
    """API: Restart the Hermes gateway (non-blocking)."""
    import subprocess, threading

    def _restart():
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "hermes-gateway"],
                capture_output=True, timeout=30
            )
        except:
            try:
                subprocess.run(
                    ["hermes", "gateway", "restart"],
                    capture_output=True, timeout=30
                )
            except:
                pass

    threading.Thread(target=_restart, daemon=True).start()
    return {"success": True, "message": "Gateway redémarrage en cours... (3-5s)"}


# ═══════════════════════════════════════════════════════════════
# Profiles pages
# ═══════════════════════════════════════════════════════════════

@app.get("/profiles/{slug}", response_class=HTMLResponse)
async def profiles_page(slug: str):
    """Profile management page."""
    proj = get_project(slug)
    if not proj:
        return _page("Projet introuvable", "<p>Projet introuvable.</p>")

    profiles = list_profile_templates(slug)
    bundles = list_bundles(slug)
    nav = _nav_tabs(slug, "profiles")
    back = f'<a href="/{slug}" class="back-link">← Retour au projet</a>'

    bundle_opts = '<option value="">(Aucun)</option>'
    for b in bundles:
        bundle_opts += f'<option value="{html.escape(b["name"])}">{html.escape(b["name"])}</option>'

    rows = ""
    for p in profiles:
        system_tag = "<span style='color:#64748b;font-size:11px'> ⚙️</span>" if p.get("is_system") else ""
        active_tag = "<span style='color:#22c55e'>✅</span>" if p.get("is_active") else "<span style='color:#64748b'>⬜</span>"
        role_colors = {"product":"#3b82f6","design":"#e879f9","architect":"#fdba74",
                       "backend":"#6ee7b7","frontend":"#93c5fd","master-agent":"#fbbf24"}
        role_color = role_colors.get(p.get("role",""), "#94a3b8")
        bundle_info = f" bundle: {p['bundle_name']}" if p.get("bundle_name") else ""
        delete_btn = "" if p.get("is_system") else f'<button onclick="deleteProfile(\'{p["name"]}\')" class="pipeline-btn" style="border-color:#ef4444;color:#fca5a5;background:#450a0a">🗑️</button>'
        activate_btn = ""
        if not p.get("is_system"):
            if p.get("is_active"):
                activate_btn = f'<button onclick="deactivateProfile(\'{p["name"]}\')" class="pipeline-btn" style="border-color:#f59e0b;color:#fde68a;background:#451a03">🔴 Désactiver</button>'
            else:
                activate_btn = f'<button onclick="activateProfile(\'{p["name"]}\')" class="pipeline-btn" style="border-color:#22c55e;color:#bbf7d0;background:#052e16">✅ Activer</button>'

        rows += f"""
        <div class="card" onclick="location.href='/profile/{slug}/{html.escape(p['name'])}'" style="cursor:pointer">
            <div class="card-row">
                <span class="prefix-lg" style="color:{role_color}">👤</span>
                <div style="flex:1">
                    <div class="name">{html.escape(p['name'])}{system_tag}{active_tag}</div>
                    <div class="meta">Rôle: {p.get('role','—')}{bundle_info}</div>
                    <div class="meta" style="font-size:11px">{html.escape(p.get('instruction','')[:80])}</div>
                </div>
                <div style="display:flex;gap:4px">{activate_btn}{delete_btn}</div>
            </div>
        </div>"""

    body = f"""
    {back}
    <h2>👤 Profils — {html.escape(proj['name'])}</h2>
    {nav}
    <div class="card" style="border-color:#3b82f6">
        <div class="name" style="margin-bottom:8px">➕ Nouveau profil</div>
        <form id="profileForm" onsubmit="return createProfile(event)" style="display:flex;flex-direction:column;gap:8px">
            <input name="name" placeholder="Nom du profil" required style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
            <div style="display:flex;gap:8px">
                <input name="role" placeholder="Rôle (product, design...)" style="flex:1;padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
                <select name="bundle" style="flex:1;padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">{bundle_opts}</select>
            </div>
            <input name="channel" placeholder="ID canal Discord (optionnel)" style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
            <textarea name="instruction" placeholder="Instruction / SOUL.md" rows="3" style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0"></textarea>
            <button type="submit" class="pipeline-btn" style="align-self:flex-start">✅ Créer</button>
        </form>
    </div>
    <h3 style="margin-bottom:8px;color:#94a3b8">{len(profiles)} profil(s)</h3>
    {rows or '<p style="color:#475569">Aucun profil.</p>'}
    <script>
    function createProfile(e) {{
        e.preventDefault(); var form = document.getElementById('profileForm'); var data = {{}};
        for (var i = 0; i < form.elements.length; i++) {{ var el = form.elements[i]; if (el.name) data[el.name] = el.value; }}
        fetch('/api/profiles/{slug}/create', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data) }})
        .then(r=>r.json()).then(d=>{{ if(d.success){{ showToast('✅ Profil créé','success'); setTimeout(function(){{ location.reload(); }},500); }} else {{ showToast('❌ '+d.error,'error'); }} }});
        return false;
    }}
    function deleteProfile(name) {{
        if (!confirm('Supprimer le profil «'+name+'» ?')) return;
        fetch('/api/profiles/{slug}/delete', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{name:name}}) }})
        .then(r=>r.json()).then(d=>{{ if(d.success){{ showToast('🗑️ Profil supprimé','success'); setTimeout(function(){{ location.reload(); }},500); }} else {{ showToast('❌ '+d.error,'error'); }} }});
    }}
    function activateProfile(name) {{
        fetch('/api/profiles/{slug}/activate', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{name:name}}) }})
        .then(r=>r.json()).then(d=>{{ if(d.success){{ showToast(d.message||'✅ Profil activé','success'); setTimeout(function(){{ location.reload(); }},500); }} else {{ showToast('❌ '+d.error,'error'); }} }});
    }}
    function deactivateProfile(name) {{
        fetch('/api/profiles/{slug}/deactivate', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{name:name}}) }})
        .then(r=>r.json()).then(d=>{{ if(d.success){{ showToast('⬜ Profil désactivé','success'); setTimeout(function(){{ location.reload(); }},500); }} else {{ showToast('❌ '+d.error,'error'); }} }});
    }}
    </script>
    """
    return _page(f"Profils {slug}", body)


@app.post("/api/profiles/{slug}/create")
async def api_profile_create(slug: str, request: Request):
    """API: Create a profile template."""
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return {"success": False, "error": "Nom requis"}
    profile = add_profile_template(
        name=name, project_slug=slug,
        bundle_name=data.get("bundle") or None,
        role=data.get("role", "").strip(),
        channel_id=data.get("channel", "").strip(),
        instruction=data.get("instruction", "").strip(),
        model=data.get("model", "").strip(),
        provider=data.get("provider", "").strip(),
    )
    if profile:
        return {"success": True, "profile": profile}
    return {"success": False, "error": "Erreur création"}


@app.post("/api/profiles/{slug}/delete")
async def api_profile_delete(slug: str, request: Request):
    """API: Delete a profile template."""
    data = await request.json()
    name = data.get("name", "")
    p = get_profile_template(name, slug)
    if p and p.get("is_system"):
        return {"success": False, "error": "Impossible de supprimer un profil système"}
    if delete_profile_template(name, slug):
        return {"success": True}
    return {"success": False, "error": "Profil introuvable"}


@app.post("/api/profiles/{slug}/activate")
async def api_profile_activate(slug: str, request: Request):
    """API: Activate a profile."""
    data = await request.json()
    name = data.get("name", "")
    p = get_profile_template(name, slug)
    if not p:
        return {"success": False, "error": "Profil introuvable"}

    profile_name = f"{slug}-{name}"
    soul_dir = os.path.expanduser(f"~/.hermes/profiles/{profile_name}")
    import subprocess
    subprocess.run(["hermes", "profile", "create", profile_name, "--clone-from", "default"],
                   capture_output=True, timeout=15)
    os.makedirs(soul_dir, exist_ok=True)
    instruction = p.get("instruction", "") or f"Tu es le profil {name} du projet {slug}."
    with open(os.path.join(soul_dir, "SOUL.md"), "w") as f:
        f.write(f"# {name} — {slug}\n\n{instruction}\n")

    proj = get_project(slug)
    if proj:
        config_path = os.path.join(soul_dir, "config.yaml")
        work_dir = proj["work_dir"]
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = f.read()
            if "workdir:" not in cfg:
                with open(config_path, "a") as f:
                    f.write(f"\nworkdir: {work_dir}\n")

    update_profile_active(name, slug, True)
    msg = f"✅ Profil '{name}' activé → ~/.hermes/profiles/{profile_name}/"

    # Sync bundle → channel_skill_bindings + channel_prompt
    sync_msg = _sync_bundle_to_config(name, slug, instruction)
    if sync_msg:
        msg += "\n" + sync_msg

    return {"success": True, "message": msg}


@app.post("/api/profiles/{slug}/{name}/sync-discord")
async def api_profile_sync_discord(slug: str, name: str):
    """API: Sync profile bundle to Discord channel config."""
    p = get_profile_template(name, slug)
    if not p:
        return {"success": False, "error": "Profil introuvable"}

    instruction = p.get("instruction", "") or f"Tu es le profil {name} du projet {slug}."
    sync_msg = _sync_bundle_to_config(name, slug, instruction)
    return {"success": True, "message": sync_msg or "✅ Synchronisé"}


def _build_channel_prompt(instruction: str, work_dir: str, role: str = "") -> str:
    """Build a complete channel prompt with work_dir, docs paths, and git instructions."""
    role_docs_map = {
        "product": "docs/product/",
        "architect": "docs/architecture/",
        "design": "docs/design/",
        "backend": "docs/backend/",
        "frontend": "docs/frontend/",
        "master-agent": "docs/",
        "devops": "docs/devops/",
        "tester": "docs/tests/",
    }
    docs_path = role_docs_map.get(role, f"docs/{role}/") if role else "docs/"
    
    parts = []
    if instruction:
        parts.append(instruction.strip())
    
    parts.append("")
    parts.append(f"**WORK_DIR**: {work_dir}")
    parts.append("")
    parts.append("**ECRITURE DE DOCUMENTS :**")
    parts.append(f"- Sauvegarde tes documents dans `{work_dir}/{docs_path}`")
    if role:
        parts.append(f"- Exemple: `{work_dir}/{docs_path}analyse-{role}.md`")
    parts.append("")
    parts.append("**GIT (apres avoir ecrit un fichier) :**")
    parts.append("```")
    parts.append(f"cd {work_dir}")
    parts.append("git add -A")
    parts.append(f'git commit -m "docs({role or slug}): description du document"')
    parts.append("git push")
    parts.append("```")
    parts.append("")
    parts.append("**WIKI (connaissances du projet) :**")
    parts.append("- Consulte le wiki pour le contexte du projet, les concepts et les decisions")
    parts.append("- Tu peux lire les fichiers dans `~/wiki/` pour comprendre le projet")
    parts.append("- Les pages wiki sont organisees par dossier : entities/, concepts/, projects/")
    parts.append("")
    parts.append("**CODE EXISTANT (exploration du code source) :**")
    parts.append("- Utilise `graphify query \"<question>\"` pour explorer le code")
    parts.append("- Utilise `graphify path \"<A>\" \"<B>\"` pour les relations entre fichiers")
    parts.append("- Utilise `graphify explain \"<concept>\"` pour comprendre un concept")
    parts.append("")
    return "\n".join(parts)


def _write_channel_prompt_json(channel_id: str, prompt: str) -> None:
    """Write a channel prompt to ~/.legion/channel_prompts.json for hot-reload."""
    json_path = os.path.expanduser("~/.legion/channel_prompts.json")
    try:
        if os.path.isfile(json_path):
            with open(json_path) as f:
                prompts = json.load(f)
        else:
            prompts = {}
        prompts[str(channel_id)] = prompt
        with open(json_path, "w") as f:
            json.dump(prompts, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception as e:
        print(f"[WARN] Could not write channel_prompts.json: {e}")


def _sync_bundle_to_config(name: str, slug: str, instruction: str) -> str:
    """Sync profile's bundle to root config.yaml channel_skill_bindings + channel_prompts.
    Returns a status message or empty string if nothing to sync.
    """
    import os, yaml
    p = get_profile_template(name, slug)
    if not p:
        return ""

    channel_id = p.get("channel_id", "")
    bundle_name = p.get("bundle_name", "")
    if not channel_id:
        return "   ℹ️ Pas de channel Discord défini pour ce profil"
    if not bundle_name:
        return "   ℹ️ Pas de bundle associé — rien à synchroniser"

    bundle = get_bundle(bundle_name)
    if not bundle:
        return f"   ⚠️ Bundle '{bundle_name}' introuvable"

    skills = bundle.get("skills", [])
    if not skills:
        return f"   ⚠️ Bundle '{bundle_name}' ne contient aucun skill"

    # Build rich channel prompt with work_dir and docs paths
    proj = get_project(slug)
    work_dir = proj["work_dir"] if proj else f"/home/hermes/projects/{slug}"
    role = p.get("role", "")
    channel_prompt = _build_channel_prompt(instruction, work_dir, role)

    # Read & update root config.yaml
    config_path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        return f"   ❌ Erreur lecture config.yaml: {e}"

    if "discord" not in cfg:
        cfg["discord"] = {}
    d = cfg["discord"]

    # Update channel_skill_bindings
    if "channel_skill_bindings" not in d:
        d["channel_skill_bindings"] = []
    bindings = d["channel_skill_bindings"]

    cid = str(channel_id)
    # Remove existing entry for this channel
    bindings = [b for b in bindings if str(b.get("id", "")) != cid]
    # Add new entry
    bindings.append({"id": cid, "skills": skills})
    d["channel_skill_bindings"] = bindings

    # Update channel_prompts (with work_dir, docs paths, git)
    if "channel_prompts" not in d:
        d["channel_prompts"] = {}
    d["channel_prompts"][cid] = channel_prompt

    # Also write to channel_prompts.json for legion-channels plugin (hot-reload)
    _write_channel_prompt_json(cid, channel_prompt)

    # Also ensure channel is in free_response_channels
    if "free_response_channels" in d:
        existing = [x.strip() for x in d["free_response_channels"].split(",")]
        if cid not in existing:
            existing.append(cid)
            d["free_response_channels"] = ",".join(existing)

    # Write back
    try:
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        return f"   ❌ Erreur écriture config.yaml: {e}"

    return f"   🔗 Bundle '{bundle_name}' → {len(skills)} skills synchronisés dans channel_skill_bindings (channel {channel_id[:8]}...)"


def _sync_bundles_to_config(name: str, slug: str, instruction: str) -> str:
    """Sync ALL bundles for a profile to root config.yaml.
    Supports multiple bundles via bundle_names JSON array.
    Returns a status message or empty string."""
    import os, yaml, json as j
    p = get_profile_template(name, slug)
    if not p:
        return ""

    channel_id = p.get("channel_id", "")
    if not channel_id:
        return "   ℹ️ Pas de channel Discord défini pour ce profil"

    # Collect all skills from all bundles
    bundle_names_raw = p.get("bundle_names", "") or ""
    bundle_names = []
    if bundle_names_raw:
        try:
            bundle_names = j.loads(bundle_names_raw)
        except (j.JSONDecodeError, TypeError):
            bundle_names = [bundle_names_raw]
    # Fallback to single bundle_name
    single = p.get("bundle_name", "")
    if single and single not in bundle_names:
        bundle_names.append(single)

    if not bundle_names:
        return "   ℹ️ Pas de bundle associé — rien à synchroniser"

    all_skills = []
    missing = []
    for bn in bundle_names:
        b = get_bundle(bn)
        if not b:
            missing.append(bn)
            continue
        skills = b.get("skills", [])
        if not skills:
            missing.append(bn)
            continue
        for s in skills:
            if s not in all_skills:
                all_skills.append(s)

    if not all_skills:
        return f"   ⚠️ Les bundles ne contiennent aucun skill"

    # Build rich channel prompt with work_dir and docs paths
    proj = get_project(slug)
    work_dir = proj["work_dir"] if proj else f"/home/hermes/projects/{slug}"
    role = p.get("role", "")
    channel_prompt = _build_channel_prompt(instruction, work_dir, role)

    # Read & update root config.yaml
    config_path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        return f"   ❌ Erreur lecture config.yaml: {e}"

    if "discord" not in cfg:
        cfg["discord"] = {}
    d = cfg["discord"]

    # Update channel_skill_bindings
    if "channel_skill_bindings" not in d:
        d["channel_skill_bindings"] = []
    bindings = d["channel_skill_bindings"]

    cid = str(channel_id)
    bindings = [b for b in bindings if str(b.get("id", "")) != cid]
    bindings.append({"id": cid, "skills": all_skills})
    d["channel_skill_bindings"] = bindings

    # Update channel_prompts (with work_dir, docs paths, git)
    if "channel_prompts" not in d:
        d["channel_prompts"] = {}
    d["channel_prompts"][cid] = channel_prompt

    # Also write to channel_prompts.json for legion-channels plugin (hot-reload)
    _write_channel_prompt_json(cid, channel_prompt)

    # Ensure in free_response_channels
    if "free_response_channels" in d:
        existing = [x.strip() for x in d["free_response_channels"].split(",")]
        if cid not in existing:
            existing.append(cid)
            d["free_response_channels"] = ",".join(existing)

    # Write back
    try:
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        return f"   ❌ Erreur écriture config.yaml: {e}"

    parts = [f"bundles: {', '.join(bundle_names)}", f"{len(all_skills)} skills"]
    if missing:
        parts.append(f"⚠️ introuvables: {', '.join(missing)}")
    return f"   🔗 {', '.join(parts)} → synchronisés dans channel_skill_bindings (channel {channel_id[:8]}...)"


@app.post("/api/profiles/{slug}/deactivate")
async def api_profile_deactivate(slug: str, request: Request):
    """API: Deactivate a profile."""
    data = await request.json()
    name = data.get("name", "")
    p = get_profile_template(name, slug)
    if p and p.get("is_system"):
        return {"success": False, "error": "Impossible de désactiver un profil système"}
    update_profile_active(name, slug, False)
    return {"success": True, "message": "⬜ Profil désactivé"}
@app.get("/profile/{slug}/{name}", response_class=HTMLResponse)
async def profile_detail_page(slug: str, name: str):
    """Profile detail page — server-side rendered."""
    import os, json, yaml

    proj = get_project(slug)
    if not proj:
        return _page("Projet introuvable", "<p>Projet introuvable.</p>")

    p = get_profile_template(name, slug)
    if not p:
        return _page("Profil introuvable", "<p>Profil introuvable.</p>")

    # Handle both naming conventions:
    # - Old: name="product" (short), dir="skull-game-product"
    # - New: name="virality-product" (full), dir="virality-product"
    if name.startswith(f"{slug}-"):
        profile_full_name = name
    else:
        profile_full_name = f"{slug}-{name}"
    profile_dir = os.path.expanduser(f"~/.hermes/profiles/{profile_full_name}")

    # 1. SOUL.md
    soul_content = ""
    soul_path = os.path.join(profile_dir, "SOUL.md")
    if os.path.exists(soul_path):
        with open(soul_path) as f:
            soul_content = f.read()

    # 2. Profile skills
    profile_skills = []
    skills_dir = os.path.join(profile_dir, "skills")
    if os.path.isdir(skills_dir):
        for root, dirs, files in os.walk(skills_dir):
            if "SKILL.md" not in files:
                continue
            rel = os.path.relpath(root, skills_dir)
            parts = rel.split(os.sep)
            skill_name = parts[-1]
            category = parts[0] if len(parts) > 1 else ""
            desc = ""
            md_path = os.path.join(root, "SKILL.md")
            try:
                with open(md_path) as f:
                    c = f.read()
                import re
                m = re.search(r'description:\s*>\s*\n\s*(.+?)(?=\n\w+:|$)', c, re.DOTALL)
                if not m:
                    m = re.search(r'description:\s*[\'"](.+?)[\'"]', c)
                if not m:
                    m = re.search(r'description:\s*(.+?)$', c, re.MULTILINE)
                if m:
                    desc = m.group(1).strip().replace('\n', ' ')
            except:
                pass
            profile_skills.append({"name": skill_name, "category": category, "description": desc[:120]})
    profile_skills.sort(key=lambda x: x["name"])

    # 3. Channel info from root config
    channel_id = p.get("channel_id", "")
    channel_prompt = ""
    channel_bindings = []
    if channel_id:
        config_path = os.path.expanduser("~/.hermes/config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            d = cfg.get("discord", {})
            channel_prompt = d.get("channel_prompts", {}).get(str(channel_id), "")
            bindings = d.get("channel_skill_bindings", [])
            for b in bindings:
                if str(b.get("id", "")) == str(channel_id):
                    channel_bindings = b.get("skills", [])
                    break
        except:
            pass

    nav = _nav_tabs(slug, "profiles")
    back = f'<a href="/profiles/{slug}" class="back-link">← Retour aux profils</a>'

    # Render skills by category (collapsible, server-side)
    cats = {}
    for s in profile_skills:
        c = s["category"] or "(autres)"
        cats.setdefault(c, []).append(s)
    cat_keys = sorted(cats.keys())

    skills_html = ""
    for i, cat in enumerate(cat_keys):
        items = cats[cat]
        skills_html += f"""
        <div style="margin-bottom:6px">
            <div onclick="toggleCat({i})" style="cursor:pointer;padding:6px 8px;background:#1e293b;border-radius:6px;display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#94a3b8">
                <span>📁 {html.escape(cat.capitalize())} <span style="color:#64748b;font-size:11px">({len(items)})</span></span>
                <span id="catArrow{i}" style="color:#64748b;font-size:11px">▶</span>
            </div>
            <div id="catBody{i}" style="display:none;padding:2px 0">"""
        for s in items:
            skills_html += f"""<div style="padding:3px 8px;font-size:12px;border-bottom:1px solid #0f172a">
                <span style="color:#e2e8f0">{html.escape(s['name'])}</span>
                <div style="font-size:11px;color:#64748b">{html.escape(s['description'][:120])}</div>
            </div>"""
        skills_html += "</div></div>"

    # Channel bindings chips
    bindings_html = ""
    if channel_bindings:
        for sk in channel_bindings:
            bindings_html += f'<span style="background:#1e293b;color:#93c5fd;padding:4px 10px;border-radius:12px;font-size:12px;display:inline-block;margin:2px">{html.escape(sk)}</span>'
    else:
        bindings_html = '<p style="color:#ef4444;font-size:13px">❌ Aucun skill binding — le chat Discord ne charge aucun skill Hermes</p>'

    # Issues detection
    issues = []
    if not channel_prompt: issues.append("❌ Pas de channel_prompt")
    if not channel_bindings: issues.append("❌ Pas de channel_skill_bindings")
    if not soul_content: issues.append("❌ Pas de SOUL.md")
    issues_html = "<p style='color:#22c55e'>✅ Tout est aligné</p>" if not issues else (
        '<ul style="color:#fca5a5;margin:0;padding-left:20px">' +
        "".join(f'<li style="margin:4px 0">{i}</li>' for i in issues) +
        "</ul>"
    )

    role_colors = {"product":"#3b82f6","design":"#e879f9","architect":"#fdba74",
                   "backend":"#6ee7b7","frontend":"#93c5fd","master-agent":"#fbbf24"}
    role_color = role_colors.get(p.get("role",""), "#94a3b8")

    body = f"""
    {back}
    <h2>👤 {html.escape(name)} — {html.escape(proj['name'])}</h2>
    {nav}

    <div class="card" style="border-left:4px solid {role_color}">
        <div class="name" style="margin-bottom:8px">ℹ️ Infos du profil</div>
        <table style="width:100%;border-collapse:collapse">
        <tr><td style="padding:4px 8px;color:#94a3b8">Rôle</td><td style="padding:4px 8px">{html.escape(p.get('role','—'))}</td></tr>
        <tr><td style="padding:4px 8px;color:#94a3b8">Bundle</td><td style="padding:4px 8px">{html.escape(p.get('bundle_name','') or '—')}</td></tr>
        <tr><td style="padding:4px 8px;color:#94a3b8">Channel Discord</td><td style="padding:4px 8px">{html.escape(channel_id or '—')}</td></tr>
        <tr><td style="padding:4px 8px;color:#94a3b8">Modèle</td><td style="padding:4px 8px">{html.escape(p.get('model','') or 'défaut')}</td></tr>
        <tr><td style="padding:4px 8px;color:#94a3b8">Actif</td><td style="padding:4px 8px">{'<span style="color:#22c55e">✅ Oui</span>' if p.get('is_active') else '<span style="color:#64748b">⬜ Non</span>'}</td></tr>
        </table>
        <div style="margin-top:10px">
            <button onclick="syncDiscord()" class="pipeline-btn" style="border-color:#6366f1;color:#c7d2fe;background:#1e1b4b">🔗 Sync bundle → Discord</button>
            <span id="syncStatus" style="font-size:12px;color:#64748b;margin-left:8px"></span>
        </div>
    </div>

    <div class="card">
        <div class="name" style="margin-bottom:8px">📋 SOUL.md — Personnalité Kanban</div>
        <pre style="background:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:12px;font-size:12px;overflow-x:auto;white-space:pre-wrap;color:#e2e8f0;max-height:400px;overflow-y:auto">{html.escape(soul_content)}</pre>
    </div>

    <div class="card">
        <div class="name" style="margin-bottom:8px">💬 Channel Prompt — Personnalité Discord</div>
        <pre style="background:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:12px;font-size:12px;overflow-x:auto;white-space:pre-wrap;color:#e2e8f0;max-height:300px;overflow-y:auto">{html.escape(channel_prompt) or '<span style="color:#64748b">(aucun)</span>'}</pre>
    </div>

    <div class="card">
        <div class="name" style="margin-bottom:8px">🔗 Channel Skill Bindings — Skills Discord</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">{bindings_html}</div>
    </div>

    <div class="card">
        <div class="name" style="margin-bottom:8px">📦 Skills du profil (Kanban) — {len(profile_skills)} skills</div>
        <input id="pskillsSearch" type="text" placeholder="Filtrer..." oninput="filterPSkills()" style="width:100%;padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;box-sizing:border-box;margin-bottom:8px">
        <div id="pskillsList">{skills_html}</div>
    </div>

    <div class="card" style="border-color:#f59e0b">
        <div class="name" style="margin-bottom:8px">⚖️ Alignement Kanban ↔ Discord</div>
        {issues_html}
    </div>

    <script>
    function toggleCat(id) {{
        var body = document.getElementById('catBody' + id);
        var arrow = document.getElementById('catArrow' + id);
        if (body.style.display === 'none') {{
            body.style.display = ''; arrow.textContent = '▼';
        }} else {{
            body.style.display = 'none'; arrow.textContent = '▶';
        }}
    }}
    function filterPSkills() {{
        var q = document.getElementById('pskillsSearch').value.toLowerCase().trim();
        var items = document.querySelectorAll('#pskillsList > div');
        for (var i = 0; i < items.length; i++) {{
            var body = items[i].querySelector('[id^="catBody"]');
            if (!body) continue;
            var lis = body.querySelectorAll('div');
            var hasMatch = !q;
            for (var j = 0; j < lis.length; j++) {{
                var txt = (lis[j].textContent || '').toLowerCase();
                var m = !q || txt.indexOf(q) !== -1;
                lis[j].style.display = m ? '' : 'none';
                if (m) hasMatch = true;
            }}
            body.style.display = hasMatch ? '' : 'none';
            var arrow = items[i].querySelector('[id^="catArrow"]');
            if (arrow && q) arrow.textContent = hasMatch ? '▼' : '▶';
        }}
    }}
    function syncDiscord() {{
        var btn = document.querySelector('button[onclick*=\"syncDiscord\"]');
        var status = document.getElementById('syncStatus');
        btn.disabled = true;
        status.textContent = '⏳ Synchronisation...';
        fetch('/api/profiles/{slug}/{name}/sync-discord', {{method: 'POST'}})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                btn.disabled = false;
                status.textContent = data.success ? data.message : '❌ ' + data.error;
                if (data.success) setTimeout(function(){{ location.reload(); }}, 1500);
            }})
            .catch(function(err) {{
                btn.disabled = false;
                status.textContent = '❌ Erreur: ' + err.message;
            }});
    }}
    </script>
    """
    return _page(f"Profil {name} — {proj['name']}", body)


@app.get("/api/profiles/{slug}/{name}/detail")
async def api_profile_detail(slug: str, name: str):
    """API: Get detailed profile info."""
    import os, json, yaml

    p = get_profile_template(name, slug)
    if not p:
        return {"success": False, "error": "Profil introuvable"}

    # Handle both naming conventions:
    # - Old: name="product" (short), dir="skull-game-product"
    # - New: name="virality-product" (full), dir="virality-product"
    if name.startswith(f"{slug}-"):
        profile_full_name = name
    else:
        profile_full_name = f"{slug}-{name}"
    profile_dir = os.path.expanduser(f"~/.hermes/profiles/{profile_full_name}")

    # 1. SOUL.md content
    soul_content = ""
    soul_path = os.path.join(profile_dir, "SOUL.md")
    if os.path.exists(soul_path):
        with open(soul_path) as f:
            soul_content = f.read()

    # 2. Skills bundled in profile's skills/
    profile_skills = []
    skills_dir = os.path.join(profile_dir, "skills")
    if os.path.isdir(skills_dir):
        for root, dirs, files in os.walk(skills_dir):
            if "SKILL.md" not in files:
                continue
            rel = os.path.relpath(root, skills_dir)
            parts = rel.split(os.sep)
            skill_name = parts[-1]
            category = parts[0] if len(parts) > 1 else ""
            desc = ""
            md_path = os.path.join(root, "SKILL.md")
            try:
                with open(md_path) as f:
                    c = f.read()
                import re
                m = re.search(r'description:\s*>\s*\n\s*(.+?)(?=\n\w+:|$)', c, re.DOTALL)
                if not m:
                    m = re.search(r'description:\s*[\'"](.+?)[\'"]', c)
                if not m:
                    m = re.search(r'description:\s*(.+?)$', c, re.MULTILINE)
                if m:
                    desc = m.group(1).strip().replace('\n', ' ')
            except:
                pass
            profile_skills.append({
                "name": skill_name, "category": category,
                "description": desc[:120],
            })
    profile_skills.sort(key=lambda x: x["name"])

    # 3. Channel info from root config
    channel_id = p.get("channel_id", "")
    channel_prompt = ""
    channel_bindings = []

    if channel_id:
        config_path = os.path.expanduser("~/.hermes/config.yaml")
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            d = cfg.get("discord", {})

            # Channel prompt
            prompts = d.get("channel_prompts", {})
            channel_prompt = prompts.get(str(channel_id), "")

            # Skill bindings for this channel
            bindings = d.get("channel_skill_bindings", [])
            for b in bindings:
                if str(b.get("id", "")) == str(channel_id):
                    channel_bindings = b.get("skills", [])
                    break
        except:
            pass

    return {
        "success": True,
        "profile": {
            "name": p["name"],
            "role": p.get("role", ""),
            "bundle_name": p.get("bundle_name", ""),
            "channel_id": channel_id,
            "is_active": p.get("is_active", False),
            "is_system": p.get("is_system", False),
            "model": p.get("model", ""),
            "provider": p.get("provider", ""),
        },
        "soul_content": soul_content,
        "profile_skills_count": len(profile_skills),
        "profile_skills": profile_skills,
        "channel_prompt": channel_prompt,
        "channel_bindings": channel_bindings,
    }


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Legion Web Dashboard")
    parser.add_argument("--port", "-p", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"🌐 Legion Web Dashboard")
    print(f"   http://{args.host}:{args.port}/")
    print()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
