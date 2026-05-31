# Legion — Système de société virtuelle

Legion est un système de gestion de features multi-projet, détaché de tout projet
spécifique. Installable sur n'importe quelle instance Hermes.

## Structure

```
~/.legion/
├── legion              ← CLI (bash wrapper, point d'entrée)
├── core/
│   ├── cli.py          ← CLI Python (argparse)
│   ├── db.py           ← Base SQLite centralisée
│   └── features.py     ← CRUD features (à venir)
├── plugins/
│   ├── base.py         ← Classe de base des plugins
│   └── registry.py     ← Découverte automatique
├── tui/                ← Interface Textual (à venir)
├── db/
│   ├── legion.db       ← SQLite (projets, features, meta)
│   └── legion.log      ← Logs des commandes
├── scripts/
│   └── migrate.py      ← Migration depuis l'ancien système
├── install.sh          ← Script d'installation
└── README.md
```

## Utilisation

```bash
# Lister les projets
legion projects

# Voir les détails d'un projet
legion -p skull-game projects show

# Lister les features d'un projet
legion -p skull-game features

# Statut d'une feature
legion -p skull-game status AUTH

# Initialiser la DB
legion init
```

## Installation sur une autre instance Hermes

```bash
git clone <url> ~/.legion
cd ~/.legion && bash install.sh
```

## Roadmap

- [x] Base SQLite centralisée (projets + features)
- [x] CLI multi-projet (legion features, status, projects)
- [x] Migration depuis l'ancien système
- [ ] Plugin system (base + registry)
- [ ] Plugin Expo (serveur + builds EAS)
- [ ] TUI Textual
- [ ] Web Kanban (textual-serve)
- [ ] Pipeline générique (state machine)
