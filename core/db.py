"""
Legion — Database module
Central SQLite database for projects, features, and metadata.

Location: ~/.legion/db/legion.db
"""

import sqlite3
import os
import json
from datetime import datetime
from typing import Optional

LEGION_HOME = os.path.expanduser("~/.legion")
DB_PATH = os.path.join(LEGION_HOME, "db", "legion.db")

SCHEMA_SQL = """
-- ═══════════════════════════════════════════════════════════════
-- Legion — Core Database Schema
-- ═══════════════════════════════════════════════════════════════

-- 1. Projects (replaces pipeline-projects.yaml)
CREATE TABLE IF NOT EXISTS projects (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    project_type    TEXT DEFAULT 'custom',
    work_dir        TEXT NOT NULL,
    board           TEXT NOT NULL,
    extra_skills    TEXT DEFAULT '[]',         -- JSON array
    docs_structure  TEXT DEFAULT 'product/',
    pipeline_config TEXT DEFAULT '{}',         -- JSON: stage_order, profiles, body_templates, doc_patterns
    status          TEXT DEFAULT 'draft',            -- 'draft' or 'active'
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- 2. Project profiles (one-to-many)
CREATE TABLE IF NOT EXISTS project_profiles (
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    role            TEXT NOT NULL,             -- 'architect', 'backend', etc.
    profile_name    TEXT NOT NULL,             -- 'skull-game-architect'
    PRIMARY KEY (project_slug, role)
);

-- 3. Project conventions (docs paths per stage)
CREATE TABLE IF NOT EXISTS project_conventions (
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    stage           TEXT NOT NULL,             -- 'explore', 'spec', 'design', 'architect'
    doc_path        TEXT NOT NULL,             -- 'docs/product/exploration-{slug}.md'
    PRIMARY KEY (project_slug, stage)
);

-- 4. Features (previously per-project features.db)
CREATE TABLE IF NOT EXISTS features (
    slug            TEXT NOT NULL,
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    prefix          TEXT NOT NULL,
    name            TEXT NOT NULL,
    domaine         TEXT DEFAULT '',
    status          TEXT DEFAULT 'backlog',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (slug, project_slug)
);

-- 5. Feature metadata (extensible key-value)
CREATE TABLE IF NOT EXISTS feature_meta (
    feature_slug    TEXT NOT NULL,
    project_slug    TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    PRIMARY KEY (feature_slug, project_slug, key),
    FOREIGN KEY (feature_slug, project_slug) REFERENCES features(slug, project_slug) ON DELETE CASCADE
);

-- 6. Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

-- 7. Skill bundles (wraps hermes bundles)
CREATE TABLE IF NOT EXISTS bundles (
    name            TEXT PRIMARY KEY,
    description     TEXT DEFAULT '',
    project_slug    TEXT REFERENCES projects(slug) ON DELETE SET NULL,
    instruction     TEXT DEFAULT '',
    skills          TEXT NOT NULL DEFAULT '[]',  -- JSON array of skill names
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- 8. Profile templates (builder for Hermes profiles)
CREATE TABLE IF NOT EXISTS profile_templates (
    name            TEXT NOT NULL,
    project_slug    TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    bundle_name     TEXT REFERENCES bundles(name) ON DELETE SET NULL,
    role            TEXT DEFAULT '',              -- 'product', 'design', 'architect', etc.
    channel_id      TEXT DEFAULT '',              -- Discord channel ID
    instruction     TEXT DEFAULT '',              -- Extra system prompt / SOUL.md content
    model           TEXT DEFAULT '',              -- Model override
    provider        TEXT DEFAULT '',              -- Provider override
    is_active       INTEGER DEFAULT 0,            -- 1 if profile dir exists
    is_system       INTEGER DEFAULT 0,            -- 1 if system default (product, design...)
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (name, project_slug)
);

-- 9. Agent Templates (global, reusable — indépendant du projet)
CREATE TABLE IF NOT EXISTS agent_templates (
    name            TEXT PRIMARY KEY,              -- 'product', 'design-stitch', 'frontend-expo'
    label           TEXT NOT NULL,                 -- 'Product Manager', 'Designer Stitch'
    category        TEXT DEFAULT '',               -- 'product', 'design', 'frontend', 'backend', 'architect', 'devops'
    description     TEXT DEFAULT '',               -- Description du template
    is_system       INTEGER DEFAULT 1,             -- 1 = template système (seedé par défaut)

    -- Contenu avec variables mustache : {{project_name}}, {{slug}}, {{work_dir}}, {{role}}, {{role_title}}, {{channel_name}}
    soul_template   TEXT DEFAULT '',               -- Template SOUL.md
    channel_prompt  TEXT DEFAULT '',               -- Template channel prompt Discord
    bundle_name     TEXT DEFAULT '',               -- Bundle associé (optionnel)
    bind_skills     TEXT DEFAULT '[]',             -- JSON array: skills pour channel_skill_bindings

    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_features_project ON features(project_slug);
CREATE INDEX IF NOT EXISTS idx_features_prefix ON features(prefix);
CREATE INDEX IF NOT EXISTS idx_project_profiles_project ON project_profiles(project_slug);
CREATE INDEX IF NOT EXISTS idx_feature_meta_lookup ON feature_meta(feature_slug, project_slug);
CREATE INDEX IF NOT EXISTS idx_bundles_project ON bundles(project_slug);
CREATE INDEX IF NOT EXISTS idx_profile_templates_project ON profile_templates(project_slug);
CREATE INDEX IF NOT EXISTS idx_agent_templates_category ON agent_templates(category);
"""


def get_conn() -> sqlite3.Connection:
    """Get a connection to the legion database, creating it if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize the database schema."""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA_SQL)
        # Check current version
        cur = conn.execute("SELECT MAX(version) as v FROM schema_version")
        row = cur.fetchone()
        current_version = row["v"] if row and row["v"] else 0

        if current_version < 1:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, int(datetime.now().timestamp())),
            )
            current_version = 1

        if current_version < 2:
            # Add pipeline_config column to existing tables
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN pipeline_config TEXT DEFAULT '{}'")
            except sqlite3.OperationalError:
                pass  # already exists
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (2, int(datetime.now().timestamp())),
            )
            conn.commit()
            return True

        if current_version < 3:
            # Tables created by SCHEMA_SQL for v3 (bundles, profile_templates)
            # Just mark the version
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (3, int(datetime.now().timestamp())),
            )
            conn.commit()
            return True

        if current_version < 4:
            # Add status column for draft/active workflow
            try:
                conn.execute("ALTER TABLE projects ADD COLUMN status TEXT DEFAULT 'draft'")
            except sqlite3.OperationalError:
                pass  # already exists
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (4, int(datetime.now().timestamp())),
            )
            conn.commit()
            return True

        if current_version < 5:
            # Add bundle_names (JSON array) for multi-bundle support
            try:
                conn.execute("ALTER TABLE profile_templates ADD COLUMN bundle_names TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # already exists
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (5, int(datetime.now().timestamp())),
            )
            conn.commit()
            return True

        if current_version < 6:
            # v6: Agent Templates (globaux, indépendants du projet)
            # Table créée par SCHEMA_SQL, juste marquer la version
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (6, int(datetime.now().timestamp())),
            )
            conn.commit()

            # Seed default agent templates
            _seed_default_templates(conn)
            return True

        return False

    finally:
        conn.close()


def _seed_default_templates(conn):
    """Seed default agent templates if the table is empty."""
    cur = conn.execute("SELECT COUNT(*) as cnt FROM agent_templates")
    if cur.fetchone()["cnt"] > 0:
        return  # already seeded

    now = int(datetime.now().timestamp())
    templates = [
        # ── Product ──
        {
            "name": "product",
            "label": "Product Manager",
            "category": "product",
            "description": "Exploration, spécification et vision produit",
            "soul_template": """Tu es le **Product Explorer** pour **{{project_name}}**.

## Règles absolues
- ❌ Jamais de code — tu explores, documentes et recommandes
- ❌ Jamais de tickets IMPLEMENT ou architecture
- ✅ Tu produis des specs structurées pour l'architecte
- ✅ Tu tagges chaque feature : [concurrent], [social], [usage], [ia]

## Mission

### Phase 1 — Document de vision
Si {{work_dir}}/docs/product/vision-{{slug}}.md n'existe PAS :
1. Discute avec l'utilisateur pour comprendre le projet, ses objectifs, son public
2. Produis **docs/product/vision-{{slug}}.md** couvrant :
   - Présentation du projet (quoi, pour qui, pourquoi)
   - Objectifs et critères de succès
   - Fonctionnalités pressenties (10 minimum)
   - Public cible et personas
   - Contraintes connues (budget, délais, plateformes)

### Phase 2 — Exploration continue
Une fois la vision établie :
1. **Analyse concurrentielle** — étudier les apps concurrentes
2. **Analyse des usages** — améliorer les features existantes
3. **Écoute sociale** — Reddit, X, forums
4. **Idéation IA** — générer des idées créatives

### Phase 3 — Features (après activation)
Quand le projet est actif et que les features sont demandées :
- EXPLORE → docs/product/exploration-{slug}.md
- SPEC → docs/product/fonctionnalite-{slug}.md

Vérifie le contenu de docs/product/ pour savoir où tu en es.

## Grille d'évaluation des features
- **Problème** : quel problème concret ça résout ?
- **Solution** : description de la feature
- **Sources** : d'où vient l'idée
- **Valeur utilisateur** : ⭐ à ⭐⭐⭐⭐⭐
- **Effort estimé** : 🔧 Faible / Moyen / Élevé
- **Priorité** : P0 (critique) / P1 (important) / P2 (nice to have) / P3 (vision)

## Règles
- Tu parles français
- Tu fais des recherches web avant de proposer une feature
- Tu utilises `kanban_complete` quand ta mission Kanban est terminée""",
            "channel_prompt": """Product Explorer — {{project_name}}

Tu es le **Product Explorer** pour {{project_name}}.

## Règles absolues
- ❌ Jamais de code — tu explores, documentes et recommandes
- ❌ Jamais de tickets IMPLEMENT ou architecture

## Mission — 3 phases selon l'avancement

### Phase 1 — Vision (projet en draft)
Si docs/product/vision-{{slug}}.md n'existe PAS :
1. Discute avec l'utilisateur pour comprendre le projet
2. Produis **{{work_dir}}/docs/product/vision-{{slug}}.md**
   (présentation, objectifs, features pressenties, public cible, contraintes)

### Phase 2 — Exploration continue
Analyse concurrentielle, écoute sociale, idéation IA.
Tagge chaque feature : [concurrent], [social], [usage], [ia]

### Phase 3 — Pipeline (projet actif)
EXPLORE → docs/product/exploration-{slug}.md
SPEC → docs/product/fonctionnalite-{slug}.md

## Grille d'évaluation
- Problème / Solution / Sources
- Valeur (⭐) / Effort (🔧) / Priorité (P0-P3)

WORK_DIR : {{work_dir}}

ÉCRITURE :
- {{work_dir}}/docs/product/*.md

GIT : cd {{work_dir}} && git add -A && git commit -m "docs(product): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["product-trend-researcher", "product-feedback-synthesizer", "product-manager", "duckduckgo-search"]),
        },
        # ── Architect ──
        {
            "name": "architect",
            "label": "Architecte technique",
            "category": "architect",
            "description": "Conception d'architecture, choix de stack, décomposition technique",
            "soul_template": """Tu es l'**Architecte** pour **{{project_name}}**.

## Mission
1. Analyse la vision produit (docs/product/vision-{{slug}}.md) pour proposer une architecture adaptée
2. Rédige **docs/architecture/archi-{{slug}}.md** couvrant :
   - Stack technique justifiée (langages, frameworks, BDD, hébergement)
   - Architecture globale (flux de données, composants)
   - Décisions techniques clés et trade-offs
   - **Templates recommandés** (design-stitch, frontend-expo, backend-supabase, etc.)
3. Présente tes recommandations à l'utilisateur

## Règles
- Justifie chaque choix technique
- Considère les contraintes : hébergement, budget, compétences, scalabilité
- Ne code PAS — tu conçois
- Produis des documents en français""",
            "channel_prompt": """Architecte — {{project_name}}

Tu es l'**Architecte** pour {{project_name}}.

## Mission
1. Analyse la vision produit (docs/product/vision-{{slug}}.md)
2. Propose une architecture dans docs/architecture/archi-{{slug}}.md :
   - Stack, architecture, décisions, templates recommandés
3. Discute avec l'utilisateur pour affiner

WORK_DIR : {{work_dir}}

GIT : cd {{work_dir}} && git add -A && git commit -m "docs(archi): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["engineering-software-architect", "engineering-backend-architect"]),
        },
        # ── Design Stitch ──
        {
            "name": "design-stitch",
            "label": "Designer UI/UX (Stitch)",
            "category": "design",
            "description": "Création de maquettes UI avec Google Stitch",
            "soul_template": """Tu es le **Designer UI/UX** pour **{{project_name}}**.

## Mission
1. Depuis la spec produit (docs/product/fonctionnalite-{slug}.md), crée les maquettes avec Google Stitch
2. Produis des mockups HTML dans docs/design/{slug}-mockups/
3. Un fichier HTML par écran, numéroté (ecran-01-xxx.html)

## Règles
- Utilise Stitch MCP pour toute création/modification de maquettes
- Ne code PAS le frontend — tu designes
- Valide chaque écran avec l'utilisateur avant de passer au suivant""",
            "channel_prompt": """Designer UI/UX — {{project_name}}

Tu conçois les maquettes avec Google Stitch.

WORK_DIR : {{work_dir}}

ÉCRITURE : {{work_dir}}/docs/design/{slug}-mockups/

Utilise Stitch MCP. Un fichier HTML par écran.
Ne code PAS le frontend natif.""",
            "bundle_name": "",
            "bind_skills": json.dumps(["design-ui-designer", "design-ux-architect", "stitch-to-react-native"]),
        },
        # ── Design HTML ──
        {
            "name": "design-html",
            "label": "Designer UI/UX (HTML/CSS)",
            "category": "design",
            "description": "Création de maquettes en HTML/CSS pur",
            "soul_template": """Tu es le **Designer UI/UX** pour **{{project_name}}**.

## Mission
1. Depuis la spec produit, crée les maquettes en HTML/CSS pur
2. Produis des mockups dans docs/design/{slug}-mockups/
3. Un fichier HTML par écran, responsive mobile-first

## Règles
- HTML/CSS vanilla — pas de framework
- Mobile-first, dark theme cohérent
- Valide chaque écran avec l'utilisateur""",
            "channel_prompt": """Designer UI/UX — {{project_name}}

Maquettes HTML/CSS pures, mobile-first.

WORK_DIR : {{work_dir}}
ÉCRITURE : {{work_dir}}/docs/design/{slug}-mockups/""",
            "bundle_name": "",
            "bind_skills": json.dumps(["design-ui-designer", "design-ux-architect"]),
        },
        # ── Frontend Expo ──
        {
            "name": "frontend-expo",
            "label": "Développeur Frontend (Expo)",
            "category": "frontend",
            "description": "Développement mobile React Native / Expo",
            "soul_template": """Tu es le **Développeur Frontend** pour **{{project_name}}**.

## Mission
1. Implémente les écrans depuis les maquettes (docs/design/{slug}-mockups/)
2. Stack : Expo (React Native), TypeScript, Expo Router
3. Suis les patterns UI définis dans le projet

## Règles
- Un fichier par écran dans app/
- Composants réutilisables dans components/
- Valide le rendu avant de marquer done""",
            "channel_prompt": """Frontend — {{project_name}}

Stack : Expo (React Native), TypeScript.

WORK_DIR : {{work_dir}}
GIT : cd {{work_dir}} && git add -A && git commit -m "feat(frontend): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["engineering-frontend-developer", "expo-react-native-ui", "stitch-to-react-native", "expo-debugging"]),
        },
        # ── Frontend Next.js ──
        {
            "name": "frontend-nextjs",
            "label": "Développeur Frontend (Next.js)",
            "category": "frontend",
            "description": "Développement web React / Next.js",
            "soul_template": """Tu es le **Développeur Frontend** pour **{{project_name}}**.

## Mission
1. Implémente les pages depuis les maquettes
2. Stack : Next.js, React, TypeScript, Tailwind
3. Suis les patterns Next.js (App Router, Server Components)

## Règles
- App Router (pas Pages Router)
- Composants serveur par défaut, client si nécessaire
- Valide le rendu avant de marquer done""",
            "channel_prompt": """Frontend — {{project_name}}

Stack : Next.js, React, TypeScript, Tailwind.

WORK_DIR : {{work_dir}}
GIT : cd {{work_dir}} && git add -A && git commit -m "feat(frontend): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["next-best-practices", "next-cache-components"]),
        },
        # ── Backend Supabase ──
        {
            "name": "backend-supabase",
            "label": "Développeur Backend (Supabase)",
            "category": "backend",
            "description": "Backend Supabase Cloud — SQL, RLS, Edge Functions",
            "soul_template": """Tu es le **Développeur Backend** pour **{{project_name}}**.

## Mission
1. Implémente la couche backend : tables SQL, RLS, Edge Functions
2. Stack : Supabase Cloud, PostgreSQL, TypeScript Edge Functions
3. Suis le document d'architecture

## Règles
- Migration SQL versionnée
- RLS sur toutes les tables
- Valide avant de marquer done""",
            "channel_prompt": """Backend — {{project_name}}

Stack : Supabase Cloud, PostgreSQL, Edge Functions.

WORK_DIR : {{work_dir}}
GIT : cd {{work_dir}} && git add -A && git commit -m "feat(backend): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["supabase-backend-debug", "engineering-database-optimizer", "engineering-backend-architect"]),
        },
        # ── Backend API ──
        {
            "name": "backend-api",
            "label": "Développeur Backend (API)",
            "category": "backend",
            "description": "Backend REST/GraphQL — FastAPI, Django, Node.js",
            "soul_template": """Tu es le **Développeur Backend** pour **{{project_name}}**.

## Mission
1. Implémente l'API REST/GraphQL
2. Stack définie dans le document d'architecture
3. Documentation OpenAPI

## Règles
- Tests unitaires obligatoires
- Validation des entrées
- Valide avant de marquer done""",
            "channel_prompt": """Backend — {{project_name}}

Stack : API REST/GraphQL.

WORK_DIR : {{work_dir}}
GIT : cd {{work_dir}} && git add -A && git commit -m "feat(backend): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["engineering-backend-architect", "engineering-security-engineer"]),
        },
        # ── DevOps ──
        {
            "name": "devops",
            "label": "Ingénieur DevOps",
            "category": "devops",
            "description": "Infrastructure, CI/CD, déploiement, monitoring",
            "soul_template": """Tu es l'**Ingénieur DevOps** pour **{{project_name}}**.

## Mission
1. Configure l'infrastructure (CI/CD, hébergement, base de données)
2. Assure le déploiement continu
3. Surveille la santé des services

## Règles
- Infrastructure as Code
- Secrets dans le vault, pas dans le code
- Monitoring et alerting""",
            "channel_prompt": """DevOps — {{project_name}}

WORK_DIR : {{work_dir}}
GIT : cd {{work_dir}} && git add -A && git commit -m "chore(devops): ..." && git push""",
            "bundle_name": "",
            "bind_skills": json.dumps(["engineering-devops-automator", "engineering-sre", "engineering-security-engineer"]),
        },
    ]

    for t in templates:
        conn.execute(
            """INSERT OR IGNORE INTO agent_templates
               (name, label, category, description, is_system,
                soul_template, channel_prompt, bundle_name, bind_skills,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (t["name"], t["label"], t["category"], t["description"],
             t["soul_template"], t["channel_prompt"], t["bundle_name"],
             t["bind_skills"],
             now, now),
        )
    conn.commit()


# ═══════════════════════════════════════════════════════════════
# Projects CRUD
# ═══════════════════════════════════════════════════════════════

def add_project(
    slug: str,
    name: str,
    work_dir: str,
    board: str,
    project_type: str = "custom",
    extra_skills: Optional[list] = None,
    docs_structure: str = "product/",
    status: str = "draft",
) -> dict:
    """Add a project to the database. Returns the project dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO projects
               (slug, name, project_type, work_dir, board, extra_skills, docs_structure, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM projects WHERE slug=?), ?), ?)""",
            (slug, name, project_type, work_dir, board,
             json.dumps(extra_skills or []), docs_structure, status,
             slug, now, now),
        )
        conn.commit()
        return get_project(slug)
    finally:
        conn.close()


def get_project(slug: str) -> Optional[dict]:
    """Get a project by slug. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,))
        row = cur.fetchone()
        if not row:
            return None
        proj = dict(row)
        proj["extra_skills"] = json.loads(proj.get("extra_skills", "[]"))
        proj["pipeline_config"] = json.loads(proj.get("pipeline_config", "{}"))

        # Fetch profiles
        cur2 = conn.execute(
            "SELECT role, profile_name FROM project_profiles WHERE project_slug=?",
            (slug,),
        )
        proj["profiles"] = {r["role"]: r["profile_name"] for r in cur2.fetchall()}

        # Fetch conventions
        cur3 = conn.execute(
            "SELECT stage, doc_path FROM project_conventions WHERE project_slug=?",
            (slug,),
        )
        proj["conventions"] = {r["stage"]: r["doc_path"] for r in cur3.fetchall()}

        return proj
    finally:
        conn.close()


def list_projects() -> list[dict]:
    """List all projects."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT slug, name, project_type, work_dir, board FROM projects ORDER BY slug")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def delete_project(slug: str) -> bool:
    """Delete a project. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM projects WHERE slug=?", (slug,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_project_pipeline_config(slug: str, config: dict) -> dict:
    """Set pipeline configuration for a project. Returns updated project."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET pipeline_config=?, updated_at=? WHERE slug=?",
            (json.dumps(config), int(datetime.now().timestamp()), slug),
        )
        conn.commit()
        return get_project(slug)
    finally:
        conn.close()


def set_project_status(slug: str, status: str = "draft") -> dict:
    """Set project status (draft/active). Returns updated project."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE projects SET status=?, updated_at=? WHERE slug=?",
            (status, int(datetime.now().timestamp()), slug),
        )
        conn.commit()
        return get_project(slug)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Features CRUD
# ═══════════════════════════════════════════════════════════════

def add_feature(
    slug: str,
    project_slug: str,
    prefix: str,
    name: str,
    domaine: str = "",
    status: str = "backlog",
) -> dict:
    """Add a feature to the database. Returns the feature dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO features
               (slug, project_slug, prefix, name, domaine, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM features WHERE slug=? AND project_slug=?), ?), ?)""",
            (slug, project_slug, prefix, name, domaine, status,
             slug, project_slug, now, now),
        )
        conn.commit()
        return get_feature(slug, project_slug)
    finally:
        conn.close()


def get_feature(slug: str, project_slug: str) -> Optional[dict]:
    """Get a feature by slug + project. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM features WHERE slug=? AND project_slug=?",
            (slug, project_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
        feat = dict(row)

        # Fetch metadata
        cur2 = conn.execute(
            "SELECT key, value FROM feature_meta WHERE feature_slug=? AND project_slug=?",
            (slug, project_slug),
        )
        feat["meta"] = {r["key"]: r["value"] for r in cur2.fetchall()}
        return feat
    finally:
        conn.close()


def get_feature_by_prefix(prefix: str, project_slug: str) -> Optional[dict]:
    """Get a feature by prefix within a project."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM features WHERE prefix=? AND project_slug=?",
            (prefix, project_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
        return get_feature(row["slug"], project_slug)
    finally:
        conn.close()


def list_features(project_slug: Optional[str] = None) -> list[dict]:
    """List features, optionally filtered by project."""
    conn = get_conn()
    try:
        if project_slug:
            cur = conn.execute(
                "SELECT * FROM features WHERE project_slug=? ORDER BY slug",
                (project_slug,),
            )
        else:
            cur = conn.execute("SELECT * FROM features ORDER BY project_slug, slug")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def set_feature_meta(feature_slug: str, project_slug: str, key: str, value: str):
    """Set a metadata key on a feature."""
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO feature_meta (feature_slug, project_slug, key, value)
               VALUES (?, ?, ?, ?)""",
            (feature_slug, project_slug, key, value),
        )
        conn.commit()
    finally:
        conn.close()


def delete_feature(slug: str, project_slug: str) -> bool:
    """Delete a feature."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM features WHERE slug=? AND project_slug=?",
            (slug, project_slug),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Migration helpers
# ═══════════════════════════════════════════════════════════════

def import_from_pipeline_yaml(yaml_path: str) -> list[str]:
    """Import projects from the old pipeline-projects.yaml.
    Returns list of imported project slugs."""
    import yaml

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    imported = []
    for slug, info in data.get("projects", {}).items():
        add_project(
            slug=slug,
            name=info.get("label", slug),
            work_dir=os.path.expanduser(info.get("repo", f"~/projects/{slug}")),
            board=info.get("kanban_board", slug),
            project_type=info.get("project_type", "custom"),
            extra_skills=info.get("extra_skills", []),
            docs_structure=info.get("docs_root", "docs/") + info.get("project_type", "product"),
        )

        # Import profiles
        conn = get_conn()
        try:
            for role, profile_name in info.get("profiles", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO project_profiles (project_slug, role, profile_name) VALUES (?, ?, ?)",
                    (slug, role, profile_name),
                )

            # Import conventions
            for stage, doc_path in info.get("conventions", {}).get("docs", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO project_conventions (project_slug, stage, doc_path) VALUES (?, ?, ?)",
                    (slug, stage, doc_path),
                )
            conn.commit()
        finally:
            conn.close()

        imported.append(slug)

    return imported


def import_features_from_db(project_slug: str, features_db_path: str) -> int:
    """Import features from a legacy per-project features.db.
    Returns count of imported features."""
    if not os.path.exists(features_db_path):
        return 0

    old_conn = sqlite3.connect(features_db_path)
    old_conn.row_factory = sqlite3.Row
    new_conn = get_conn()

    try:
        cur = old_conn.execute("SELECT * FROM features")
        count = 0
        for row in cur.fetchall():
            feat = dict(row)
            add_feature(
                slug=feat["slug"],
                project_slug=project_slug,
                prefix=feat["prefix"],
                name=feat["name"],
                domaine=feat.get("domaine", ""),
                status=feat.get("status", "backlog"),
            )

            # Import meta
            meta_cur = old_conn.execute(
                "SELECT key, value FROM feature_meta WHERE slug=?",
                (feat["slug"],),
            )
            for m in meta_cur.fetchall():
                set_feature_meta(feat["slug"], project_slug, m["key"], m["value"])

            count += 1

        return count
    finally:
        old_conn.close()
        new_conn.close()


def ensure_indexes():
    """Create indexes if they don't exist."""
    conn = get_conn()
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_features_project ON features(project_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_features_prefix ON features(prefix)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_profiles_project ON project_profiles(project_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_meta_lookup ON feature_meta(feature_slug, project_slug)")
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Bundles CRUD
# ═══════════════════════════════════════════════════════════════

def add_bundle(
    name: str,
    skills: list,
    description: str = "",
    project_slug: str = None,
    instruction: str = "",
) -> dict:
    """Add a bundle. Returns the bundle dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO bundles
               (name, description, project_slug, instruction, skills, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM bundles WHERE name=?), ?), ?)""",
            (name, description, project_slug, instruction, json.dumps(skills),
             name, now, now),
        )
        conn.commit()
        return get_bundle(name)
    finally:
        conn.close()


def get_bundle(name: str) -> Optional[dict]:
    """Get a bundle by name. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM bundles WHERE name=?", (name,))
        row = cur.fetchone()
        if not row:
            return None
        b = dict(row)
        b["skills"] = json.loads(b.get("skills", "[]"))
        return b
    finally:
        conn.close()


def list_bundles(project_slug: str = None) -> list[dict]:
    """List bundles, optionally filtered by project."""
    conn = get_conn()
    try:
        if project_slug:
            cur = conn.execute(
                "SELECT * FROM bundles WHERE project_slug=? OR project_slug IS NULL ORDER BY name",
                (project_slug,),
            )
        else:
            cur = conn.execute("SELECT * FROM bundles ORDER BY name")
        bundles = [dict(r) for r in cur.fetchall()]
        for b in bundles:
            b["skills"] = json.loads(b.get("skills", "[]"))
        return bundles
    finally:
        conn.close()


def delete_bundle(name: str) -> bool:
    """Delete a bundle. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM bundles WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_all_skills() -> list[dict]:
    """List all available Hermes skills from ~/.hermes/skills/."""
    import os, json
    skills_dir = os.path.expanduser("~/.hermes/skills")
    results = []
    for root, dirs, files in os.walk(skills_dir):
        if "SKILL.md" not in files:
            continue
        rel = os.path.relpath(root, skills_dir)
        # rel is like "devops/kanban-worker" or "creative/excalidraw" or "product-manager"
        parts = rel.split(os.sep)
        skill_name = parts[-1]  # last segment
        category = parts[0] if len(parts) > 1 else ""
        # Read description from first line after --- in SKILL.md
        md_path = os.path.join(root, "SKILL.md")
        desc = ""
        try:
            with open(md_path) as f:
                content = f.read()
            # Try to get description from YAML frontmatter
            import re
            m = re.search(r'description:\s*>\s*\n\s*(.+?)(?=\n\w+:|$)', content, re.DOTALL)
            if not m:
                m = re.search(r'description:\s*[\'"](.+?)[\'"]', content)
            if not m:
                m = re.search(r'description:\s*(.+?)$', content, re.MULTILINE)
            if m:
                desc = m.group(1).strip().replace('\n', ' ')
        except:
            pass
        results.append({
            "name": skill_name,
            "path": rel,
            "category": category,
            "description": desc[:120],
        })
    results.sort(key=lambda x: x["name"])
    return results


# ═══════════════════════════════════════════════════════════════
# Profile Templates CRUD
# ═══════════════════════════════════════════════════════════════

def add_profile_template(
    name: str,
    project_slug: str,
    bundle_name: str = None,
    bundle_names: str = "",  # JSON array: '["bundle1","bundle2"]'
    role: str = "",
    channel_id: str = "",
    instruction: str = "",
    model: str = "",
    provider: str = "",
    is_system: bool = False,
) -> dict:
    """Add a profile template. Returns the profile dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO profile_templates
               (name, project_slug, bundle_name, bundle_names, role, channel_id, instruction,
                model, provider, is_active, is_system, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, COALESCE((SELECT created_at FROM profile_templates WHERE name=? AND project_slug=?), ?), ?)""",
            (name, project_slug, bundle_name, bundle_names, role, channel_id, instruction,
             model, provider, 1 if is_system else 0,
             name, project_slug, now, now),
        )
        conn.commit()
        return get_profile_template(name, project_slug)
    finally:
        conn.close()


def get_profile_template(name: str, project_slug: str) -> Optional[dict]:
    """Get a profile template by name + project. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM profile_templates WHERE name=? AND project_slug=?",
            (name, project_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def list_profile_templates(project_slug: str = None) -> list[dict]:
    """List profile templates, optionally filtered by project."""
    conn = get_conn()
    try:
        if project_slug:
            cur = conn.execute(
                "SELECT * FROM profile_templates WHERE project_slug=? ORDER BY is_system DESC, name",
                (project_slug,),
            )
        else:
            cur = conn.execute("SELECT * FROM profile_templates ORDER BY project_slug, name")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def update_profile_active(name: str, project_slug: str, is_active: bool) -> bool:
    """Set the active flag on a profile template."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE profile_templates SET is_active=?, updated_at=? WHERE name=? AND project_slug=?",
            (1 if is_active else 0, int(datetime.now().timestamp()), name, project_slug),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_profile_template(name: str, project_slug: str) -> bool:
    """Delete a profile template. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM profile_templates WHERE name=? AND project_slug=?",
            (name, project_slug),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_system_profiles(project_slug: str):
    """Seed default system profiles for a project if they don't exist."""
    defaults = [
        {"name": f"product", "role": "product", "is_system": True,
         "instruction": "Product Explorer — exploration et specs produit"},
        {"name": f"design", "role": "design", "is_system": True,
         "instruction": "UI/UX Designer — maquettes Stitch uniquement"},
        {"name": f"architect", "role": "architect", "is_system": True,
         "instruction": "Architecte — décomposition technique et tickets IMPLEMENT"},
        {"name": f"backend", "role": "backend", "is_system": True,
         "instruction": "Backend Dev — Supabase Cloud, SQL, Edge Functions"},
        {"name": f"frontend", "role": "frontend", "is_system": True,
         "instruction": "Frontend Dev — Expo, React Native, TypeScript"},
        {"name": f"master-agent", "role": "master-agent", "is_system": True,
         "instruction": "Master Agent — coordination, bugs, triage"},
    ]
    for d in defaults:
        existing = get_profile_template(d["name"], project_slug)
        if not existing:
            add_profile_template(
                name=d["name"],
                project_slug=project_slug,
                role=d["role"],
                instruction=d["instruction"],
                is_system=d["is_system"],
            )


# ═══════════════════════════════════════════════════════════════
# Agent Templates CRUD (globaux, réutilisables)
# ═══════════════════════════════════════════════════════════════

MUSTACHE_VARS = {
    "project_name": "(nom du projet)",
    "slug": "(slug du projet)",
    "work_dir": "(chemin du projet)",
    "role": "(nom du rôle)",
    "role_title": "(titre du rôle)",
    "channel_name": "(nom du channel Discord)",
}


def render_template(template_text: str, variables: dict) -> str:
    """Replace {{var}} placeholders with values."""

    def _replace(m):
        key = m.group(1)
        return str(variables.get(key, m.group(0)))
    import re
    return re.sub(r"\{\{(\w+)\}\}", _replace, template_text)


def add_agent_template(
    name: str,
    label: str,
    category: str = "",
    description: str = "",
    is_system: bool = True,
    soul_template: str = "",
    channel_prompt: str = "",
    bundle_name: str = "",
    bind_skills: Optional[list] = None,
) -> dict:
    """Add a global agent template. Returns the template dict."""
    now = int(datetime.now().timestamp())
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO agent_templates
               (name, label, category, description, is_system,
                soul_template, channel_prompt, bundle_name, bind_skills,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       COALESCE((SELECT created_at FROM agent_templates WHERE name=?), ?), ?)""",
            (name, label, category, description, 1 if is_system else 0,
             soul_template, channel_prompt, bundle_name,
             json.dumps(bind_skills or []),
             name, now, now),
        )
        conn.commit()
        return get_agent_template(name)
    finally:
        conn.close()


def get_agent_template(name: str) -> Optional[dict]:
    """Get an agent template by name. Returns None if not found."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM agent_templates WHERE name=?", (name,))
        row = cur.fetchone()
        if not row:
            return None
        tpl = dict(row)
        tpl["bind_skills"] = json.loads(tpl.get("bind_skills", "[]"))
        return tpl
    finally:
        conn.close()


def list_agent_templates(category: str = None) -> list[dict]:
    """List agent templates, optionally filtered by category.
    Category values: 'product', 'design', 'frontend', 'backend', 'architect', 'devops'
    """
    conn = get_conn()
    try:
        if category:
            cur = conn.execute(
                "SELECT * FROM agent_templates WHERE category=? ORDER BY name",
                (category,),
            )
        else:
            cur = conn.execute("SELECT * FROM agent_templates ORDER BY is_system DESC, category, name")
        templates = [dict(r) for r in cur.fetchall()]
        for t in templates:
            t["bind_skills"] = json.loads(t.get("bind_skills", "[]"))
        return templates
    finally:
        conn.close()


def update_agent_template(
    name: str,
    label: str = None,
    category: str = None,
    description: str = None,
    is_system: bool = None,
    soul_template: str = None,
    channel_prompt: str = None,
    bundle_name: str = None,
    bind_skills: Optional[list] = None,
) -> Optional[dict]:
    """Update an agent template. Only provided fields are updated."""
    existing = get_agent_template(name)
    if not existing:
        return None
    now = int(datetime.now().timestamp())
    fields = {
        "label": label, "category": category, "description": description,
        "soul_template": soul_template, "channel_prompt": channel_prompt,
        "bundle_name": bundle_name, "updated_at": now,
    }
    if is_system is not None:
        fields["is_system"] = 1 if is_system else 0
    if bind_skills is not None:
        fields["bind_skills"] = json.dumps(bind_skills)

    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE agent_templates SET {sets} WHERE name=?",
            (*vals, name),
        )
        conn.commit()
        return get_agent_template(name)
    finally:
        conn.close()


def delete_agent_template(name: str) -> bool:
    """Delete an agent template. Returns True if deleted."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM agent_templates WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def render_agent_template(name: str, variables: dict) -> Optional[dict]:
    """Render an agent template with mustache variables.
    Returns the template dict with soul_template, channel_prompt, and bind_skills
    resolved with the given variables.

    Variables: project_name, slug, work_dir, role, role_title, channel_name
    """
    tpl = get_agent_template(name)
    if not tpl:
        return None
    rendered = dict(tpl)
    rendered["soul_template"] = render_template(tpl["soul_template"], variables)
    rendered["channel_prompt"] = render_template(tpl["channel_prompt"], variables)
    # bind_skills is a JSON array — no mustache needed (they're skill names)
    return rendered
