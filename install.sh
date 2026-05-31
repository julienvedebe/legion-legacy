#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Legion — Install script
# Ajoute ~/.legion/legion au PATH et initialise la base.
# ═══════════════════════════════════════════════════════════════

set -e

echo "🏛️  Installation de Legion"
echo "═" * 40

LEGION_HOME="${HOME}/.legion"

# 1. S'assurer que le script est présent
if [[ ! -f "${LEGION_HOME}/legion" ]]; then
    echo "❌ ${LEGION_HOME}/legion introuvable"
    echo "   Clone d'abord le dépôt : git clone <url> ~/.legion"
    exit 1
fi

chmod +x "${LEGION_HOME}/legion"

# 2. Ajouter au PATH
SHELL_RC="${HOME}/.bashrc"
if [[ -f "${HOME}/.zshrc" ]]; then
    SHELL_RC="${HOME}/.zshrc"
fi

if grep -q 'export PATH="\$HOME/.legion:\$PATH"' "$SHELL_RC" 2>/dev/null; then
    echo "✅ Legion déjà dans le PATH"
else
    echo "" >> "$SHELL_RC"
    echo "# Legion — Système de société virtuelle" >> "$SHELL_RC"
    echo 'export PATH="$HOME/.legion:$PATH"' >> "$SHELL_RC"
    echo "✅ Ajouté à ${SHELL_RC}"
    echo "   → Relance : source ${SHELL_RC}"
fi

# 3. Ajouter un lien symbolique (alternative)
if [[ ! -L /usr/local/bin/legion ]]; then
    sudo ln -sf "${LEGION_HOME}/legion" /usr/local/bin/legion 2>/dev/null || true
fi

# 4. Initialiser la DB
python3 "${LEGION_HOME}/core/cli.py" init

# 5. Vérification
echo ""
echo "🔍 Vérification..."
LEGION_PROJECT=skull-game "${LEGION_HOME}/legion" features 2>/dev/null || true

echo ""
echo "✅ Installation terminée !"
echo "   Teste : legion projects"
echo "         : legion -p skull-game features"