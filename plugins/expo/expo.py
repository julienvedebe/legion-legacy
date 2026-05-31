"""
Legion — Expo Plugin

Manages Expo dev servers, EAS builds, and EAS updates.

Commands:
  legion expo start [--port <port>]   → Start Expo dev server
  legion expo build [--platform <p>]  → EAS build (ios/android/all)
  legion expo update                  → EAS Update (OTA)
  legion expo status                  → Dev server status
  legion expo stop                    → Stop dev server
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from plugins.base import LegionPlugin, PluginCommand, PluginContext


# ── Helpers ──

def _log(msg: str):
    print(f"  ⚡ {msg}")


def _error(msg: str):
    print(f"  ❌ {msg}", file=sys.stderr)


def _find_expo(work_dir: str) -> str:
    """Find the expo CLI binary in the project."""
    candidates = [
        os.path.join(work_dir, "node_modules", ".bin", "expo"),
        os.path.join(work_dir, "node_modules", "expo", "bin", "cli"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "npx expo"  # fallback


def _find_eas(work_dir: str) -> str:
    """Find the EAS CLI."""
    candidates = [
        os.path.join(work_dir, "node_modules", ".bin", "eas"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "npx eas"  # fallback


def _get_pid_file(work_dir: str) -> str:
    """Path to the PID file for Expo dev server."""
    return os.path.join(work_dir, ".expo", "dev-server.pid")


def _get_log_file(work_dir: str) -> str:
    """Path to log file for Expo dev server."""
    return os.path.join(work_dir, ".expo", "dev-server.log")


# ── Command handlers ──

def cmd_start(ctx: PluginContext):
    """Start Expo dev server in the background."""
    work_dir = ctx.work_dir
    pid_file = _get_pid_file(work_dir)
    log_file = _get_log_file(work_dir)

    # Check if already running
    if os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # Test if alive
            _log(f"Expo déjà en cours d'exécution (PID {old_pid})")
            print(f"    Logs: {log_file}")
            return
        except (ProcessLookupError, ValueError, OSError):
            _log("PID trouvé mais processus mort, redémarrage...")
            os.remove(pid_file)

    # Parse custom port
    port = "8081"
    for i, arg in enumerate(ctx.args):
        if arg == "--port" and i + 1 < len(ctx.args):
            port = ctx.args[i + 1]

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    expo_bin = _find_expo(work_dir)

    _log(f"Démarrage Expo (port {port})...")
    _log(f"    Projet: {work_dir}")
    _log(f"    Logs:   {log_file}")

    # Start in background with nohup
    cmd = (
        f"cd {work_dir} && nohup {expo_bin} start --port {port} "
        f"> {log_file} 2>&1 & echo $!"
    )

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    pid_str = result.stdout.strip().split("\n")[-1].strip()

    try:
        pid = int(pid_str)
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        with open(pid_file, "w") as f:
            f.write(str(pid))
        _log(f"✅ Expo démarré (PID {pid})")
        print(f"    URL LAN: http://localhost:{port}")
        print(f"    Arrêt:   legion expo stop")
    except ValueError:
        _error(f"Impossible de récupérer le PID : {pid_str}")
        print(f"    Sortie: {result.stderr}")


def cmd_stop(ctx: PluginContext):
    """Stop the Expo dev server."""
    pid_file = _get_pid_file(ctx.work_dir)

    if not os.path.isfile(pid_file):
        _log("Aucun serveur Expo en cours.")
        return

    with open(pid_file) as f:
        pid_str = f.read().strip()

    try:
        pid = int(pid_str)
        os.kill(pid, 15)  # SIGTERM
        time.sleep(1)

        # Also kill any node processes on port 8081
        subprocess.run(
            f"lsof -ti:8081 2>/dev/null | xargs -r kill 2>/dev/null || true",
            shell=True, timeout=5,
        )

        os.remove(pid_file)
        _log(f"✅ Expo arrêté (PID {pid})")
    except ProcessLookupError:
        _log("Processus déjà terminé, nettoyage...")
        os.remove(pid_file)
    except Exception as e:
        _error(f"Erreur arrêt: {e}")


def cmd_status(ctx: PluginContext):
    """Check Expo dev server status."""
    pid_file = _get_pid_file(ctx.work_dir)
    log_file = _get_log_file(ctx.work_dir)

    if not os.path.isfile(pid_file):
        _log("Aucun serveur Expo en cours.")
        return

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # Test signal
        _log(f"✅ Expo en cours d'exécution (PID {pid})")
        print(f"    Logs: {log_file}")

        # Show last few log lines
        if os.path.isfile(log_file):
            result = subprocess.run(
                f"tail -5 {log_file}",
                shell=True, capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                print(f"    Dernières lignes:")
                for line in result.stdout.strip().split("\n"):
                    print(f"      {line}")

        # Check port
        port_check = subprocess.run(
            "lsof -ti:8081 2>/dev/null",
            shell=True, capture_output=True, text=True, timeout=5,
        )
        if port_check.stdout.strip():
            print(f"    Port 8081: ouvert")
        else:
            print(f"    Port 8081: fermé")

    except ProcessLookupError:
        _log("PID trouvé mais processus mort.")
        _log("Utilise 'legion expo start' pour relancer.")
    except Exception as e:
        _error(f"Erreur: {e}")


def cmd_build(ctx: PluginContext):
    """Run EAS build."""
    work_dir = ctx.work_dir
    eas_bin = _find_eas(work_dir)

    # Parse platform
    platform = "all"
    for i, arg in enumerate(ctx.args):
        if arg == "--platform" and i + 1 < len(ctx.args):
            platform = ctx.args[i + 1]

    if platform not in ("ios", "android", "all"):
        _error(f"Plateforme invalide: {platform} (ios/android/all)")
        return

    _log(f"Lancement EAS Build ({platform})...")
    _log(f"    Projet: {work_dir}")

    # Run EAS build (foreground — this takes a while)
    try:
        subprocess.run(
            f"cd {work_dir} && {eas_bin} build --platform {platform} --non-interactive",
            shell=True, timeout=7200,  # 2h timeout for builds
        )
        _log("✅ Build terminé")
    except subprocess.TimeoutExpired:
        _error("Build expiré (limite 2h)")
    except KeyboardInterrupt:
        _log("Build annulé")
    except Exception as e:
        _error(f"Erreur build: {e}")


def cmd_update(ctx: PluginContext):
    """Run EAS Update (OTA)."""
    work_dir = ctx.work_dir
    eas_bin = _find_eas(work_dir)

    _log("Lancement EAS Update...")
    _log(f"    Projet: {work_dir}")

    # Parse optional branch/message
    branch = "main"
    message = f"Update {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    for i, arg in enumerate(ctx.args):
        if arg == "--branch" and i + 1 < len(ctx.args):
            branch = ctx.args[i + 1]
        if arg == "--message" and i + 1 < len(ctx.args):
            message = ctx.args[i + 1]

    try:
        result = subprocess.run(
            f"cd {work_dir} && {eas_bin} update --branch {branch} --message '{message}' --non-interactive",
            shell=True, timeout=600, capture_output=True, text=True,
        )
        if result.returncode == 0:
            _log(f"✅ Update publié sur le branch '{branch}'")
            if result.stdout:
                print(f"    {result.stdout.strip()[-500:]}")
        else:
            _error(f"Échec update: {result.stderr[:500]}")
    except Exception as e:
        _error(f"Erreur update: {e}")


# ── Plugin definition ──

class ExpoPlugin(LegionPlugin):

    @property
    def name(self) -> str:
        return "expo"

    @property
    def description(self) -> str:
        return "Gère les serveurs Expo, builds EAS et mises à jour OTA"

    @property
    def version(self) -> str:
        return "0.1.0"

    def get_commands(self) -> dict[str, PluginCommand]:
        return {
            "start": PluginCommand(
                name="start",
                description="Démarrer le serveur Expo en arrière-plan",
                usage="expo start [--port 8081]",
                handler=cmd_start,
            ),
            "stop": PluginCommand(
                name="stop",
                description="Arrêter le serveur Expo",
                usage="expo stop",
                handler=cmd_stop,
            ),
            "status": PluginCommand(
                name="status",
                description="Vérifier l'état du serveur Expo",
                usage="expo status",
                handler=cmd_status,
            ),
            "build": PluginCommand(
                name="build",
                description="Lancer un build EAS",
                usage="expo build [--platform ios|android|all]",
                handler=cmd_build,
            ),
            "update": PluginCommand(
                name="update",
                description="Publier une mise à jour OTA (EAS Update)",
                usage="expo update [--branch main] [--message \"...\"]",
                handler=cmd_update,
            ),
        }


# Singleton exported for auto-discovery by registry.py
plugin = ExpoPlugin()
