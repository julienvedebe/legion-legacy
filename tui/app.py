"""
Legion — Textual TUI App

Main application with dashboard view.
Run: python3 -m tui.app [--project <slug>]
Serve web: textual-serve 'python3 -m tui.app'
"""

import os
import sys
from typing import Optional

# Ensure ~/.legion is in path
sys.path.insert(0, os.path.expanduser("~/.legion"))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)
from textual.css.query import NoMatches


# ── Legion DB access ──
from core.db import (
    init_db,
    list_projects,
    get_project,
    list_features,
)


# ── Styles CSS ──

CSS = """
Screen {
    background: $surface;
}

DashboardScreen {
    layout: grid;
    grid-size: 2 1;
    grid-columns: 1fr 3fr;
}

#sidebar {
    background: $panel;
    border-right: solid $primary;
    padding: 1;
}

#sidebar-title {
    text-style: bold;
    color: $primary;
    padding-bottom: 1;
}

#project-list {
    height: 100%;
}

#main-panel {
    padding: 1;
    overflow-y: auto;
}

#dashboard-header {
    text-style: bold;
    height: 3;
    padding: 1;
    background: $boost;
}

#feature-list {
    margin-top: 1;
}

.feature-card {
    border: solid $surface;
    padding: 1;
    margin-bottom: 1;
    background: $panel;
}

.feature-card:hover {
    border: solid $accent;
}

.feature-prefix {
    color: $primary;
    text-style: bold;
    min-width: 7;
}

.feature-name {
    text-style: bold;
}

.feature-stats {
    color: $text-muted;
}

.stage-dot {
    margin-right: 1;
}

ProgressBar {
    width: 100%;
    margin-top: 1;
}

#status-bar {
    height: 1;
    background: $boost;
    color: $text-muted;
    padding: 0 1;
}
"""


# ── Dashboard Screen ──

class DashboardScreen(Screen):
    """Main dashboard showing project overview + features."""

    current_project = reactive("skull-game")

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Vertical(
                Label("📋 Projets", id="sidebar-title"),
                ListView(id="project-list"),
                id="sidebar",
            ),
            Vertical(
                Static(id="dashboard-header"),
                VerticalScroll(
                    Static(id="stats-panel"),
                    Static(id="feature-list"),
                    id="main-panel",
                ),
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        """Initialize on mount."""
        self._refresh_projects()
        self._refresh_dashboard()

    def _refresh_projects(self) -> None:
        """Populate the project list."""
        try:
            projects = list_projects()
        except Exception:
            projects = []

        lv = self.query_one("#project-list", ListView)
        lv.clear()

        for p in projects:
            slug = p["slug"]
            label = f"{slug}"
            if slug == self.current_project:
                label = f"▶ {slug}"
            lv.append(ListItem(Label(label)))

    def _refresh_dashboard(self) -> None:
        """Refresh the dashboard content for the current project."""
        try:
            proj = get_project(self.current_project)
        except Exception:
            proj = None

        if not proj:
            self.query_one("#dashboard-header", Static).update(
                f"[bold red]❌ Projet '{self.current_project}' introuvable[/]"
            )
            return

        # Header
        profiles = proj.get("profiles", {})
        header = (
            f"[bold $primary]🏛️  {proj['name']}[/]  "
            f"[dim]{self.current_project} | {proj['project_type']}[/]\n"
            f"[dim]{proj['work_dir']}  |  {len(profiles)} profils[/]"
        )
        self.query_one("#dashboard-header", Static).update(header)

        # Stats
        try:
            features = list_features(self.current_project)
        except Exception:
            features = []

        total = len(features)
        counts = {}
        for f in features:
            meta = f.get("meta", {})
            stage = meta.get("pipeline_stage", f.get("status", "backlog"))
            counts[stage] = counts.get(stage, 0) + 1

        stages_order = ["backlog", "explore", "spec", "design", "architect", "implement", "done"]
        stats_lines = [f"[bold]📊 Features: {total} total[/]"]
        for s in stages_order:
            if s in counts:
                stats_lines.append(f"   {s:12s} : {counts[s]}")
        self.query_one("#stats-panel", Static).update("\n".join(stats_lines))

        # Feature cards
        feature_html = "[bold]📋 Liste des features[/]\n"
        if not features:
            feature_html += "\n[dim]Aucune feature[/]\n"
        else:
            for f in features:
                meta = f.get("meta", {})
                stage = meta.get("pipeline_stage", f.get("status", "backlog"))
                stage_icon = {
                    "backlog": "⬜",
                    "explore": "🔍",
                    "spec": "📝",
                    "design": "🎨",
                    "architect": "🏗️",
                    "implement": "⚙️",
                    "done": "✅",
                }.get(stage, "⬜")

                feature_html += (
                    f"\n{stage_icon} "
                    f"[bold $primary]{f['prefix']:6s}[/] "
                    f"[bold]{f['name']}[/]\n"
                    f"  [dim]{stage:12s}[/]"
                )

        self.query_one("#feature-list", Static).update(feature_html)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle project selection from the sidebar."""
        item = event.item
        label = item.query_one(Label).renderable or ""
        # Strip the "▶ " prefix if present
        slug = label.replace("▶ ", "").strip()
        if slug and slug != self.current_project:
            self.current_project = slug
            self._refresh_projects()
            self._refresh_dashboard()

    def watch_current_project(self, old: str, new: str) -> None:
        """React to project changes."""
        if new != old:
            self._refresh_dashboard()


# ── Main App ──

class LegionTUI(App):
    """Legion Textual TUI Application."""

    TITLE = "🏛️  Legion"
    CSS = CSS

    SCREENS = {
        "dashboard": DashboardScreen,
    }

    BINDINGS = [
        Binding("q", "quit", "Quitter"),
        Binding("r", "refresh", "Rafraîchir"),
        Binding("d", "go_dashboard", "Dashboard"),
        Binding("p", "cycle_project", "Projet suivant"),
        Binding("?", "help", "Aide"),
    ]

    def __init__(self, project_slug: Optional[str] = None):
        super().__init__()
        self._initial_project = project_slug

    def on_mount(self) -> None:
        """Go to dashboard on startup."""
        self.push_screen("dashboard")

        # Set initial project if provided
        if self._initial_project:
            try:
                ds = self.get_screen("dashboard")
                ds.current_project = self._initial_project
            except Exception:
                pass

    def action_go_dashboard(self) -> None:
        """Switch to dashboard view."""
        try:
            self.switch_screen("dashboard")
        except Exception:
            self.push_screen("dashboard")

    def action_refresh(self) -> None:
        """Refresh current view."""
        try:
            ds = self.get_screen("dashboard")
            ds._refresh_projects()
            ds._refresh_dashboard()
            self.notify("✅ Rafraîchi", timeout=2)
        except Exception:
            pass

    def action_cycle_project(self) -> None:
        """Cycle to the next project."""
        try:
            projects = list_projects()
            ds = self.get_screen("dashboard")
            current = ds.current_project
            slugs = [p["slug"] for p in projects]
            if current in slugs:
                idx = (slugs.index(current) + 1) % len(slugs)
                ds.current_project = slugs[idx]
                self.notify(f"📋 Projet: {slugs[idx]}", timeout=2)
        except Exception:
            pass


# ── Entry point ──

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Legion TUI")
    parser.add_argument("--project", "-p", help="Projet par défaut")

    args, _ = parser.parse_known_args()

    app = LegionTUI(project_slug=args.project)
    app.run()


if __name__ == "__main__":
    main()
