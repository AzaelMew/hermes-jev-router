#!/usr/bin/env python3
"""Install the two Hermes surfaces without copying any secret files."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]


def install(home: Path) -> None:
    general = home / "plugins" / "jev-router-inline"
    provider = home / "plugins" / "model-providers" / "jev-router"
    general.mkdir(parents=True, exist_ok=True)
    provider.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py"):
        shutil.copy2(ROOT / name, general / name)
    source_package = ROOT / "hermes_jev_router"
    destination_package = general / "hermes_jev_router"
    shutil.copytree(source_package, destination_package, dirs_exist_ok=True)
    for source in (ROOT / "providers" / "jev-router").iterdir():
        if source.is_file() and source.name != "__pycache__":
            shutil.copy2(source, provider / source.name)
    shutil.copy2(ROOT / "providers" / "jev-router" / "router_client.py", general / "router_client.py")
    print(f"Installed general plugin: {general}")
    print(f"Installed model provider: {provider}")
    print("No environment files or credentials were copied.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", default="~/.hermes")
    args = parser.parse_args()
    install(Path(args.hermes_home).expanduser())


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
