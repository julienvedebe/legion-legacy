"""
Legion — Web Dashboard (mobile-friendly)

Usage: python3 tui/web.py [--port 8000] [--host 0.0.0.0]
"""

import argparse
import base64
import html
import io
import mimetypes
import os
import re
import socket
import sqlite3
import sys

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

from core.db import list_projects, get_project, list_features
from core.db import list_bundles, get_bundle, add_bundle, delete_bundle
from core.db import list_profile_templates, get_profile_template, add_profile_template, delete_profile_template, update_profile_active


def _nav_tabs(slug: str, active: str) -> str:
    """Generate navigation tabs for a project."""
    tabs = {
        "features": f"/{slug}",
        "kanban": f"/kanban/{slug}",
        "docs": f"/{slug}/docs",
        "expo": f"/expo/{slug}",
        "bundles": f"/bundles/{slug}",
        "profiles": f"/profiles/{slug}",
    }
    labels = {"features": "📋 Features", "kanban": "📌 Kanban", "docs": "📄 Docs",
              "expo": "⚡ Expo", "bundles": "📦 Bundles", "profiles": "👤 Profils"}
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
    <div id="toast"></div>
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

        cards += f"""
        <div class="card" style="cursor:pointer" onclick="window.location='/{slug}'">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <a href="/{slug}" style="color:#f8fafc;text-decoration:none;font-size:16px;font-weight:600">{p['name']}</a>
                <span style="color:#64748b;font-size:12px">{p['project_type']} · {len(features)} features · {n_profiles} profils</span>
            </div>
            {proj_bar}
            <div style="margin-top:8px;border-top:1px solid #334155;padding-top:8px">
                {feature_lines}
            </div>
        </div>"""

    return _page("Accueil", cards)


@app.get("/api/projects")
async def api_projects():
    """JSON endpoint for projects."""
    return list_projects()


@app.get("/api/{slug}/features")
async def api_features(slug: str):
    """JSON endpoint for features of a project."""
    return list_features(slug)


@app.post("/api/{slug}/pipeline/{prefix}")
async def api_pipeline(slug: str, prefix: str):
    """Run legion pipeline for a feature using centralized pipeline engine."""
    import subprocess
    import sys
    print(f"[PIPELINE] Request: slug={slug}, prefix={prefix}")
    
    pipeline_script = os.path.expanduser("~/.legion/core/pipeline.py")
    print(f"[PIPELINE] Running centralized: python3 {pipeline_script} {slug} {prefix.upper()}")
    
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


@app.get("/{slug}", response_class=HTMLResponse)
async def project_detail(slug: str):
    proj = get_project(slug)
    if not proj:
        return HTMLResponse(content=_page(
            "Introuvable",
            f"<p>Projet '{slug}' introuvable.</p>"
            f"<a href='/' class='back-link'>← Retour</a>"
        ), status_code=404)

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
    body = f"""
    <div class="project-header">{proj['name']}</div>
    <div class="project-meta">
        {proj['project_type']} · {proj['work_dir']} · {len(profiles)} profils
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


# ═══════════════════════════════════════════════════════════════
# Bundles pages
# ═══════════════════════════════════════════════════════════════

@app.get("/bundles/{slug}", response_class=HTMLResponse)
async def bundles_page(slug: str):
    """Bundle management page."""
    proj = get_project(slug)
    if not proj:
        return _page("Projet introuvable", "<p>Projet introuvable.</p>")

    bundles = list_bundles(slug)
    nav = _nav_tabs(slug, "bundles")
    back = f'<a href="/{slug}" class="back-link">← Retour au projet</a>'

    rows = ""
    for b in bundles:
        skills_str = ", ".join(b["skills"]) if b["skills"] else "—"
        rows += f"""
        <div class="card">
            <div class="card-row">
                <span class="prefix-lg">📦</span>
                <div style="flex:1">
                    <div class="name">{html.escape(b['name'])}</div>
                    <div class="meta">{html.escape(b.get('description', ''))}</div>
                    <div class="meta">Skills: {html.escape(skills_str)}</div>
                </div>
                <button onclick="deleteBundle('{b['name']}')" class="pipeline-btn" style="border-color:#ef4444;color:#fca5a5;background:#450a0a">🗑️</button>
            </div>
        </div>"""

    body = f"""
    {back}
    <h2>📦 Bundles — {html.escape(proj['name'])}</h2>
    {nav}

    <div class="card" style="border-color:#3b82f6">
        <div class="name" style="margin-bottom:8px">➕ Nouveau bundle</div>
        <form id="bundleForm" onsubmit="return createBundle(event)" style="display:flex;flex-direction:column;gap:8px">
            <input name="name" placeholder="Nom du bundle" required style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
            <input name="description" placeholder="Description" style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
            <input name="skills" placeholder="Skills (séparés par des virgules)" style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0">
            <textarea name="instruction" placeholder="Instruction additionnelle" rows="2" style="padding:8px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0"></textarea>
            <button type="submit" class="pipeline-btn" style="align-self:flex-start">✅ Créer</button>
        </form>
    </div>

    <h3 style="margin-bottom:8px;color:#94a3b8">{len(bundles)} bundle(s)</h3>
    {rows or '<p style="color:#475569">Aucun bundle.</p>'}

    <script>
    function createBundle(e) {{
        e.preventDefault();
        var form = document.getElementById('bundleForm');
        var data = {{}};
        for (var i = 0; i < form.elements.length; i++) {{
            var el = form.elements[i];
            if (el.name) data[el.name] = el.value;
        }}
        fetch('/api/bundles/{slug}/create', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(data)
        }}).then(r => r.json()).then(d => {{
            if (d.success) {{ showToast('✅ Bundle créé', 'success'); setTimeout(function() {{ location.reload(); }}, 500); }}
            else {{ showToast('❌ ' + d.error, 'error'); }}
        }});
        return false;
    }}
    function deleteBundle(name) {{
        if (!confirm('Supprimer le bundle «' + name + '» ?')) return;
        fetch('/api/bundles/{slug}/delete', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: name}})
        }}).then(r => r.json()).then(d => {{
            if (d.success) {{ showToast('🗑️ Bundle supprimé', 'success'); setTimeout(function() {{ location.reload(); }}, 500); }}
            else {{ showToast('❌ ' + d.error, 'error'); }}
        }});
    }}
    </script>
    """
    return _page(f"Bundles {slug}", body)


@app.post("/api/bundles/{slug}/create")
async def api_bundle_create(slug: str, request: Request):
    """API: Create a bundle."""
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return {"success": False, "error": "Nom requis"}
    skills_str = data.get("skills", "").strip()
    skills = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []
    bundle = add_bundle(
        name=name, skills=skills,
        description=data.get("description", "").strip(),
        project_slug=slug, instruction=data.get("instruction", "").strip(),
    )
    if bundle:
        import subprocess
        cmd = ["hermes", "bundles", "create", name]
        for s in skills:
            cmd += ["--skill", s]
        if data.get("description"):
            cmd += ["--description", data["description"]]
        subprocess.run(cmd, capture_output=True, timeout=15)
        return {"success": True, "bundle": bundle}
    return {"success": False, "error": "Erreur création"}


@app.post("/api/bundles/{slug}/delete")
async def api_bundle_delete(slug: str, request: Request):
    """API: Delete a bundle."""
    data = await request.json()
    name = data.get("name", "")
    if delete_bundle(name):
        import subprocess
        subprocess.run(["hermes", "bundles", "delete", name], capture_output=True, timeout=15)
        return {"success": True}
    return {"success": False, "error": "Bundle introuvable"}


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
        <div class="card">
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
    if p.get("channel_id"):
        msg += f"\n   Canal: {p['channel_id']}\n   ⚠️ Redémarre la gateway: hermes gateway restart"
    return {"success": True, "message": msg}


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
