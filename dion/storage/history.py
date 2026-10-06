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
        self.positions_file = self.config_dir / "playback_positions.json"
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

    def _load_positions(self) -> Dict[str, float]:
        if not self.positions_file.exists():
            return {}
        try:
            with open(self.positions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
        except Exception:
            return {}

    def _save_positions(self, positions: Dict[str, float]) -> None:
        try:
            with open(self.positions_file, "w", encoding="utf-8") as f:
                json.dump(positions, f, indent=2)
        except Exception:
            pass

    def _sync_positions(self) -> None:
        """Sync realtime playback positions from playback_positions.json into history items."""
        positions = self._load_positions()
        if not positions:
            return
        modified = False
        for item in self._data.get("items", []):
            imdb_id = item.get("imdb_id")
            s = item.get("season")
            e = item.get("episode")
            key = f"{imdb_id}:{s}:{e}" if (s is not None and e is not None) else imdb_id
            if key in positions:
                new_pos = float(positions[key])
                if item.get("position") != new_pos:
                    item["position"] = new_pos
                    modified = True
            elif imdb_id in positions and s is None and e is None:
                new_pos = float(positions[imdb_id])
                if item.get("position") != new_pos:
                    item["position"] = new_pos
                    modified = True
        if modified:
            self._save()

    def get_playback_position(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> float:
        """Get saved playback position in seconds for a movie or episode."""
        positions = self._load_positions()
        if season is not None and episode is not None:
            key = f"{imdb_id}:{season}:{episode}"
            if key in positions:
                return float(positions[key])
        if imdb_id in positions:
            return float(positions[imdb_id])

        # Fallback to position recorded in history.json if available
        for item in self._data.get("items", []):
            if item.get("imdb_id") == imdb_id:
                if season is not None and episode is not None:
                    if item.get("season") == season and item.get("episode") == episode:
                        return float(item.get("position", 0.0))
                else:
                    return float(item.get("position", 0.0))
        return 0.0

    def is_episode_completed(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> bool:
        """Check if an episode has completed playback (saved position is 0.0 in positions file)."""
        positions = self._load_positions()
        if season is not None and episode is not None:
            key = f"{imdb_id}:{season}:{episode}"
            return positions.get(key) == 0.0
        return False

    def set_playback_position(
        self,
        imdb_id: str,
        position: float,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> None:
        """Persist playback position in seconds."""
        positions = self._load_positions()
        key = f"{imdb_id}:{season}:{episode}" if (season is not None and episode is not None) else imdb_id
        positions[key] = round(position, 1)
        self._save_positions(positions)
        self._sync_positions()

    def record_watch(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        playback_position: Optional[float] = None,
    ) -> None:
        """Save or update an entry in the watch history."""
        items: List[Dict[str, Any]] = self._data.get("items", [])

        # Remove existing entry for the same media if it exists
        items = [i for i in items if i.get("imdb_id") != media.imdb_id]

        if playback_position is None:
            playback_position = self.get_playback_position(
                media.imdb_id,
                season=episode.season if episode else None,
                episode=episode.episode if episode else None,
            )

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
        """Return recent watch history with up-to-date resume positions."""
        self._sync_positions()
        return self._data.get("items", [])[:limit]

    def clear_history(self) -> None:
        """Clear all watch history entries and playback positions."""
        self._data = {"items": []}
        if self.history_file.exists():
            try:
                self.history_file.unlink()
            except Exception:
                self._save()
        if self.positions_file.exists():
            try:
                self.positions_file.unlink()
            except Exception:
                pass

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

