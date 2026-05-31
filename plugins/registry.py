"""
Legion — Plugin Registry

Auto-discovers plugins from ~/.legion/plugins/<name>/<name>.py
Each plugin must export a 'plugin' attribute (instance of LegionPlugin).
"""

import importlib.util
import inspect
import os
import sys
from typing import Optional

from .base import LegionPlugin, PluginCommand, PluginContext

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")


def discover_plugins() -> dict[str, LegionPlugin]:
    """Scan ~/.legion/plugins/ and return {plugin_name: plugin_instance}."""
    plugins = {}

    if not os.path.isdir(PLUGINS_DIR):
        return plugins

    sys.path.insert(0, os.path.dirname(PLUGINS_DIR))

    for entry in sorted(os.listdir(PLUGINS_DIR)):
        plugin_dir = os.path.join(PLUGINS_DIR, entry)
        if not os.path.isdir(plugin_dir):
            continue
        if entry.startswith("_"):
            continue
        if entry.startswith("."):
            continue

        # Try: plugins/<name>/<name>.py
        main_file = os.path.join(plugin_dir, f"{entry}.py")
        if not os.path.isfile(main_file):
            continue

        try:
            spec = importlib.util.spec_from_file_location(f"plugins.{entry}", main_file)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            if hasattr(mod, "plugin") and isinstance(mod.plugin, LegionPlugin):
                p = mod.plugin
                p.init()
                plugins[entry] = p
        except Exception as e:
            print(f"⚠️  Plugin '{entry}': {e}", file=sys.stderr)

    return plugins


def get_plugin(name: str) -> Optional[LegionPlugin]:
    """Get a specific plugin by name."""
    return discover_plugins().get(name)


def get_all_commands() -> dict[str, dict[str, PluginCommand]]:
    """Return {plugin_name: {command_name: PluginCommand}}."""
    result = {}
    for pname, plugin in discover_plugins().items():
        cmds = plugin.get_commands()
        if cmds:
            result[pname] = cmds
    return result


def run_plugin_command(
    plugin_name: str,
    command_name: str,
    ctx: PluginContext,
) -> bool:
    """
    Run a plugin command. Returns True if found and executed.
    """
    plugins = discover_plugins()
    plugin = plugins.get(plugin_name)
    if not plugin:
        return False

    commands = plugin.get_commands()
    cmd = commands.get(command_name)
    if not cmd:
        return False

    if cmd.handler:
        cmd.handler(ctx)
        return True
    return False
