"""Shared filesystem and executable discovery across supported platforms."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Optional


def config_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Dion"
    return Path.home() / ".config" / "dion"


def prepare_config_directory() -> Path:
    directory = config_directory()
    if not directory.exists():
        for legacy in (Path.home() / ".config" / "dion", Path.home() / ".config" / "dionysus"):
            if legacy != directory and legacy.is_dir():
                shutil.copytree(legacy, directory)
                break
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def find_player(name: str) -> Optional[str]:
    path = shutil.which(name)
    if path:
        return path
    if sys.platform != "win32":
        return None
    relative_paths = {
        "mpv": ("mpv/mpv.exe",),
        "vlc": ("VideoLAN/VLC/vlc.exe",),
    }
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if not root:
            continue
        for relative in relative_paths.get(name, ()):
            for base in (Path(root), Path(root) / "Programs"):
                candidate = base / relative
                if candidate.is_file():
                    return str(candidate)
    return None
