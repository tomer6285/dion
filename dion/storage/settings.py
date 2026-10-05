from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_SETTINGS: Dict[str, Any] = {
    "player": "auto",  # auto, mpv, iina, vlc, or custom path
    "subtitles_enabled": False,  # default launch state
    "sub_lang": "en",  # preferred subtitle language
    "auto_select_server": False,  # whether to auto-pick best server without prompt
    "download_dir": str(Path.home() / "Downloads"),
}


class SettingsManager:
    """Manages persistent Dion configuration and user preferences."""

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            config_dir = Path.home() / ".config" / "dion"
            old_config = Path.home() / ".config" / "dionysus"
            if not config_dir.exists() and old_config.exists():
                try:
                    shutil.copytree(old_config, config_dir)
                except Exception:
                    pass
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.config_dir / "settings.json"
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.settings_file.exists():
            return dict(DEFAULT_SETTINGS)
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update(loaded)
                return merged
        except Exception:
            return dict(DEFAULT_SETTINGS)

    def _save(self) -> None:
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def reset(self) -> None:
        self._data = dict(DEFAULT_SETTINGS)
        self._save()

    @property
    def player(self) -> str:
        return str(self.get("player", "auto"))

    @property
    def subtitles_enabled(self) -> bool:
        return bool(self.get("subtitles_enabled", False))

    @property
    def sub_lang(self) -> str:
        return str(self.get("sub_lang", "en"))

    @property
    def auto_select_server(self) -> bool:
        return bool(self.get("auto_select_server", False))

    @property
    def download_dir(self) -> Path:
        raw = self.get("download_dir", str(Path.home() / "Downloads"))
        return Path(raw).expanduser().resolve()

    def resolve_player_executable(self) -> Optional[str]:
        """Resolves the executable path according to configured player preference."""
        pref = self.player.strip()
        if pref and pref != "auto":
            path = shutil.which(pref) or (pref if Path(pref).is_file() else None)
            if path:
                return path

        # Auto-detection fallback among supported players
        for candidate in ["mpv", "iina", "vlc"]:
            path = shutil.which(candidate)
            if path:
                return path
        return None

    @staticmethod
    def get_installed_players() -> List[Dict[str, str]]:
        """Return list of supported media players installed on the current system."""
        found = []
        for name in ["mpv", "iina", "vlc"]:
            p = shutil.which(name)
            if p:
                found.append({"name": name, "path": p})
        return found
