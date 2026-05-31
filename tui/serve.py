#!/usr/bin/env python3
"""
Legion — Web Server

Serves the Legion TUI via textual-web.
Accessible at http://localhost:8000 (or custom port).

Usage:
  python3 ~/.legion/tui/serve.py [--port 8000] [--project skull-game]
"""

import os
import sys
import subprocess
import argparse

sys.path.insert(0, os.path.expanduser("~/.legion"))


def main():
    parser = argparse.ArgumentParser(description="Legion Web Server")
    parser.add_argument("--port", "-p", type=int, default=8000, help="Port web")
    parser.add_argument("--project", help="Projet par défaut")
    parser.add_argument("--host", default="0.0.0.0", help="Hôte (défaut: 0.0.0.0)")

    args, _ = parser.parse_known_args()

    tui_path = os.path.join(os.path.dirname(__file__), "app.py")
    project_arg = f"--project {args.project}" if args.project else ""

    cmd = (
        f"textual serve --port {args.port} --host {args.host} "
        f"--url http://{args.host}:{args.port} "
        f"-c 'python3 {tui_path} {project_arg}'"
    )

    print(f"🌐 Legion Web — http://localhost:{args.port}")
    print(f"   (aussi sur http://{args.host}:{args.port})")
    print()

    try:
        subprocess.run(cmd, shell=True)
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
