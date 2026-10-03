from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..metadata.models import EpisodeItem, MediaItem, MediaType


class HistoryManager:
    """Manages local watch history and resume points."""

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            config_dir = Path.home() / ".config" / "dion"
            old_config = Path.home() / ".config" / "dionysus"
            if not config_dir.exists() and old_config.exists():
                import shutil
                try:
                    shutil.copytree(old_config, config_dir)
                except Exception:
                    pass
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.config_dir / "history.json"
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.history_file.exists():
            return {"items": []}
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"items": []}

    def _save(self) -> None:
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            pass

    def record_watch(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        playback_position: float = 0.0,
    ) -> None:
        """Save or update an entry in the watch history."""
        items: List[Dict[str, Any]] = self._data.get("items", [])

        # Remove existing entry for the same media if it exists
        items = [i for i in items if i.get("imdb_id") != media.imdb_id]

        entry: Dict[str, Any] = {
            "imdb_id": media.imdb_id,
            "title": media.title,
            "media_type": media.media_type.value,
            "year": media.year,
            "last_watched": datetime.now().isoformat(),
            "position": playback_position,
        }

        if episode:
            entry["season"] = episode.season
            entry["episode"] = episode.episode
            entry["episode_title"] = episode.title

        items.insert(0, entry)
        # Keep last 50 entries
        self._data["items"] = items[:50]
        self._save()

    def get_last_watched(self) -> Optional[Dict[str, Any]]:
        """Get the most recently watched title."""
        items = self._data.get("items", [])
        return items[0] if items else None

    def list_history(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Return recent watch history."""
        return self._data.get("items", [])[:limit]

    def clear_history(self) -> None:
        """Clear all watch history entries."""
        self._data = {"items": []}
        if self.history_file.exists():
            try:
                self.history_file.unlink()
            except Exception:
                self._save()

    def get_sub_delay(self, imdb_id: str) -> float:
        """Get saved subtitle delay in seconds for this title."""
        delays_file = self.config_dir / "sub_delays.json"
        if not delays_file.exists():
            return 0.0
        try:
            with open(delays_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return float(data.get(imdb_id, 0.0))
        except Exception:
            return 0.0

    def set_sub_delay(self, imdb_id: str, delay: float) -> None:
        """Persist subtitle delay for this title across playback sessions."""
        delays_file = self.config_dir / "sub_delays.json"
        data: Dict[str, float] = {}
        if delays_file.exists():
            try:
                with open(delays_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[imdb_id] = round(delay, 2)
        try:
            with open(delays_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def clear_history(self) -> None:
        """Clear all watch history entries."""
        self._data = {"items": []}
        if self.history_file.exists():
            try:
                self.history_file.unlink()
            except Exception:
                self._save()

    def get_sub_delay(self, imdb_id: str) -> float:
        """Get saved subtitle delay in seconds for this title."""
        delays_file = self.config_dir / "sub_delays.json"
        if not delays_file.exists():
            return 0.0
        try:
            with open(delays_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return float(data.get(imdb_id, 0.0))
        except Exception:
            return 0.0

    def set_sub_delay(self, imdb_id: str, delay: float) -> None:
        """Persist subtitle delay for this title across playback sessions."""
        delays_file = self.config_dir / "sub_delays.json"
        data: Dict[str, float] = {}
        if delays_file.exists():
            try:
                with open(delays_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[imdb_id] = round(delay, 2)
        try:
            with open(delays_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

