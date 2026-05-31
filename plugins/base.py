"""
Legion — Plugin Base

Every plugin:
1. Extends LegionPlugin
2. Registers via plugins/registry.py (auto-discovered from ~/.legion/plugins/)
3. Provides commands, a name, and an optional init hook
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PluginCommand:
    """A command exposed by a plugin."""
    name: str                          # e.g., 'start', 'build'
    description: str                   # Help text
    usage: str = ""                    # e.g., 'expo start [--project <slug>]'
    handler: Optional[callable] = None  # Callable(context, args)


@dataclass
class PluginContext:
    """Context passed to every plugin command handler."""
    project_slug: str                  # Current project (or default)
    work_dir: str                      # Project working directory
    args: list[str]                    # Remaining CLI args
    extra: dict[str, Any] = field(default_factory=dict)


class LegionPlugin(ABC):
    """Base class for all Legion plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name (lowercase, e.g., 'expo')."""
        ...

    @property
    def description(self) -> str:
        """Short description."""
        return ""

    @property
    def version(self) -> str:
        return "0.1.0"

    def init(self) -> None:
        """Called once when the plugin is loaded. Optional."""
        pass

    @abstractmethod
    def get_commands(self) -> dict[str, PluginCommand]:
        """
        Return {command_name: PluginCommand} for all commands this plugin provides.
        Commands are namespaced under the plugin name:
        e.g., 'legion expo start' → plugin='expo', command='start'
        """
        ...
