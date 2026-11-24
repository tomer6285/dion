from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..metadata.models import EpisodeItem, MediaItem, MediaType
from ..platforms import prepare_config_directory
from .db import DatabaseManager, utc_now_iso

logger = logging.getLogger(__name__)


def parse_runtime(val: Any) -> Optional[float]:
    """Parse runtime string or number into seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val * 60) if val <= 300 else float(val)
    val_str = str(val).strip().lower()
    if not val_str:
        return None

    h_match = re.search(r"(\d+)\s*(?:h|hr|hours?)", val_str)
    m_match = re.search(r"(\d+)\s*(?:m|min|mins|minutes?)", val_str)
    if h_match or m_match:
        hours = int(h_match.group(1)) if h_match else 0
        minutes = int(m_match.group(1)) if m_match else 0
        return float(hours * 3600 + minutes * 60)

    num_match = re.match(r"^(\d+(?:\.\d+)?)$", val_str)
    if num_match:
        num = float(num_match.group(1))
        return float(num * 60) if num <= 300 else num

    return None


def is_episode_released(ep: EpisodeItem) -> bool:
    """Check if an episode has been released as of today."""
    if not ep.released:
        return True
    rel = ep.released.strip()
    if len(rel) >= 10:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return rel[:10] <= today_str
    return True


class HistoryManager:
    """Manages watch history, play logs, resume states, and Trakt sync queue."""

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            config_dir = prepare_config_directory()
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.config_dir / "history.json"
        self.positions_file = self.config_dir / "playback_positions.json"
        self.durations_file = self.config_dir / "playback_durations.json"
        self.completions_file = self.config_dir / "playback_completions.json"

        # SQLite Database Engine
        self.db = DatabaseManager(self.config_dir / "history.sqlite3")
        self.db.migrate_legacy_json(self.history_file)

        # In-memory cached data mirroring for legacy callers/tests
        self._data: Dict[str, Any] = self._load_data_state()

    def _load_data_state(self) -> Dict[str, Any]:
        """Load combined state from database into dict format."""
        resume_items = self.db.get_all_resume_items()
        hidden = self.db.get_hidden_resume_ids()
        return {
            "items": resume_items,
            "hidden_from_continue": hidden,
        }

    def _sync_legacy_json(self) -> None:
        """Atomically persist legacy history.json for compatibility."""
        resume_items = self.db.get_all_resume_items()
        hidden = self.db.get_hidden_resume_ids()
        data = {
            "items": resume_items,
            "hidden_from_continue": hidden,
        }
        self._data = data
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.config_dir, delete=False
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(self.history_file)
        except Exception:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # Real-time Playback Positions & Durations (mpv Lua synchronization)
    # -------------------------------------------------------------------------

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

    def _load_durations(self) -> Dict[str, float]:
        if not self.durations_file.exists():
            return {}
        try:
            with open(self.durations_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
        except Exception:
            return {}

    def _save_durations(self, durations: Dict[str, float]) -> None:
        try:
            with open(self.durations_file, "w", encoding="utf-8") as f:
                json.dump(durations, f, indent=2)
        except Exception:
            pass

    def _load_completions(self) -> Dict[str, float]:
        if not self.completions_file.exists():
            return {}
        try:
            with open(self.completions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
        except Exception:
            return {}

    def _save_completions(self, completions: Dict[str, float]) -> None:
        try:
            with open(self.completions_file, "w", encoding="utf-8") as f:
                json.dump(completions, f, indent=2)
        except Exception:
            pass

    def clear_playback_completion(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> None:
        """Clear any stale player completion marker before starting playback."""
        key = f"{imdb_id}:{season}:{episode}" if (season is not None and episode is not None) else imdb_id
        completions = self._load_completions()
        if key in completions:
            completions.pop(key, None)
            self._save_completions(completions)

    def consume_playback_completion(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> bool:
        """Return True and clear marker if player explicitly recorded completion for this session."""
        key = f"{imdb_id}:{season}:{episode}" if (season is not None and episode is not None) else imdb_id
        completions = self._load_completions()
        if key in completions:
            completions.pop(key, None)
            self._save_completions(completions)
            return True
        return False

    def _sync_positions(self) -> None:
        """Sync realtime playback positions and durations into resume items."""
        positions = self._load_positions()
        durations = self._load_durations()
        if not positions and not durations:
            return

        for item in self.db.get_all_resume_items():
            imdb_id = item.get("imdb_id")
            s = item.get("season")
            e = item.get("episode")
            key = f"{imdb_id}:{s}:{e}" if (s is not None and e is not None) else imdb_id

            new_pos = None
            if key in positions:
                new_pos = float(positions[key])
            elif imdb_id in positions and s is None and e is None:
                new_pos = float(positions[imdb_id])

            new_dur = None
            if key in durations:
                new_dur = float(durations[key])
            elif imdb_id in durations and s is None and e is None:
                new_dur = float(durations[imdb_id])

            if new_pos is not None or new_dur is not None:
                pos = new_pos if new_pos is not None else item.get("position", 0.0)
                dur = new_dur if new_dur is not None else item.get("duration")
                if pos != item.get("position") or dur != item.get("duration"):
                    self.db.update_resume_state(
                        imdb_id=imdb_id,
                        media_type=item.get("media_type", "movie"),
                        title=item.get("title", ""),
                        year=item.get("year"),
                        season=s,
                        episode=e,
                        episode_title=item.get("episode_title"),
                        position=pos,
                        duration=dur,
                        last_watched=item.get("last_watched"),
                        restore_continue=False,
                    )
        self._sync_legacy_json()

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

        res = self.db.get_resume_item(imdb_id)
        if res:
            if season is not None and episode is not None:
                if res.get("season") == season and res.get("episode") == episode:
                    return float(res.get("position", 0.0))
            else:
                return float(res.get("position", 0.0))
        return 0.0

    def get_playback_duration(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[float]:
        """Get saved playback duration in seconds for a movie or episode."""
        durations = self._load_durations()
        if season is not None and episode is not None:
            key = f"{imdb_id}:{season}:{episode}"
            if key in durations and durations[key] > 0:
                return float(durations[key])
        if imdb_id in durations and durations[imdb_id] > 0:
            return float(durations[imdb_id])

        res = self.db.get_resume_item(imdb_id)
        if res:
            d = res.get("duration")
            if d and float(d) > 0:
                return float(d)
        return None

    def is_episode_completed(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> bool:
        """Check if an episode has completed playback (explicit play record or completion marker)."""
        key = f"{imdb_id}:{season}:{episode}" if (season is not None and episode is not None) else imdb_id
        if key in self._load_completions():
            return True
        return self.is_watched(imdb_id, season=season, episode=episode)

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

    def set_playback_duration(
        self,
        imdb_id: str,
        duration: float,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> None:
        """Persist playback duration in seconds."""
        if not duration or duration <= 0:
            return
        durations = self._load_durations()
        key = f"{imdb_id}:{season}:{episode}" if (season is not None and episode is not None) else imdb_id
        durations[key] = round(duration, 1)
        self._save_durations(durations)
        self._sync_positions()

    # -------------------------------------------------------------------------
    # Individual Play Tracking (Watch History)
    # -------------------------------------------------------------------------

    def record_play(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        watched_at: Optional[str] = None,
        trakt_play_id: Optional[int] = None,
        source: str = "playback",
        queue_sync: bool = True,
    ) -> int:
        """Record an individual completed play in watch history with its timestamp."""
        ts = watched_at or utc_now_iso()
        play_id = self.db.record_play(
            imdb_id=media.imdb_id,
            media_type=media.media_type.value,
            title=media.title,
            year=str(media.year) if media.year else None,
            season=episode.season if episode else None,
            episode=episode.episode if episode else None,
            episode_title=episode.title if episode else None,
            watched_at=ts,
            trakt_play_id=trakt_play_id,
            source=source,
            queue_sync=queue_sync,
        )
        return play_id

    def delete_play(self, play_id: int, queue_sync: bool = True) -> bool:
        """Delete a single play by ID. Remaining plays keep the item watched."""
        return self.db.delete_play(play_id, queue_sync=queue_sync)

    def clear_plays_for_episode(
        self,
        imdb_id: str,
        season: int,
        episode: int,
        queue_sync: bool = True,
    ) -> int:
        """Clear all plays for an episode."""
        return self.db.clear_plays_for_episode(imdb_id, season, episode, queue_sync=queue_sync)

    def clear_plays_for_season(
        self,
        imdb_id: str,
        season: int,
        queue_sync: bool = True,
    ) -> int:
        """Clear all plays for a season."""
        return self.db.clear_plays_for_season(imdb_id, season, queue_sync=queue_sync)

    def clear_plays_for_show_or_movie(
        self,
        imdb_id: str,
        include_specials: bool = True,
        queue_sync: bool = True,
    ) -> int:
        """Clear all plays for a movie or show."""
        return self.db.clear_plays_for_show_or_movie(
            imdb_id, include_specials=include_specials, queue_sync=queue_sync
        )

    def is_watched(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> bool:
        """Check if an episode or movie has at least one play recorded."""
        return self.db.is_watched(imdb_id, season=season, episode=episode)

    def get_play_count(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> int:
        """Get the count of plays recorded for this item."""
        return self.db.get_play_count(imdb_id, season=season, episode=episode)

    def get_season_watched_status(
        self,
        imdb_id: str,
        season: int,
        episodes: List[EpisodeItem],
    ) -> Tuple[int, int]:
        """Get (watched_episodes_count, total_released_episodes_count) for a season."""
        season_eps = [ep for ep in episodes if ep.season == season and is_episode_released(ep)]
        if not season_eps:
            return 0, 0
        watched_set = self.db.get_watched_episode_set(imdb_id, season=season)
        watched_count = sum(1 for ep in season_eps if (ep.season, ep.episode) in watched_set)
        return watched_count, len(season_eps)

    def get_show_watched_status(
        self,
        imdb_id: str,
        episodes: List[EpisodeItem],
        include_specials: bool = False,
    ) -> Tuple[int, int]:
        """Get (watched_episodes_count, total_released_episodes_count) for a show."""
        candidate_eps = [
            ep for ep in episodes
            if (include_specials or ep.season > 0) and is_episode_released(ep)
        ]
        if not candidate_eps:
            return 0, 0
        watched_set = self.db.get_watched_episode_set(imdb_id)
        watched_count = sum(1 for ep in candidate_eps if (ep.season, ep.episode) in watched_set)
        return watched_count, len(candidate_eps)

    def mark_episodes_watched(
        self,
        media: MediaItem,
        episodes_to_mark: List[EpisodeItem],
        watched_at: Optional[str] = None,
        queue_sync: bool = True,
    ) -> int:
        """Bulk mark released episodes as watched while preserving existing play dates."""
        ep_dicts = [
            {
                "season": ep.season,
                "episode": ep.episode,
                "title": ep.title,
            }
            for ep in episodes_to_mark
        ]
        count = self.db.bulk_mark_episodes_watched(
            imdb_id=media.imdb_id,
            title=media.title,
            year=str(media.year) if media.year else None,
            episodes_to_mark=ep_dicts,
            watched_at=watched_at,
            queue_sync=queue_sync,
        )
        return count

    # -------------------------------------------------------------------------
    # Resume State Operations (Continue Watching)
    # -------------------------------------------------------------------------

    def update_resume_state(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        playback_position: Optional[float] = None,
        duration: Optional[float] = None,
        last_watched: Optional[str] = None,
        restore_continue: bool = True,
    ) -> None:
        """Update resume point without recording a play history entry."""
        if playback_position is None:
            playback_position = self.get_playback_position(
                media.imdb_id,
                season=episode.season if episode else None,
                episode=episode.episode if episode else None,
            )

        if duration is None:
            duration = self.get_playback_duration(
                media.imdb_id,
                season=episode.season if episode else None,
                episode=episode.episode if episode else None,
            )
            if not duration:
                if episode and getattr(episode, "runtime", None):
                    duration = parse_runtime(episode.runtime)
                elif media and getattr(media, "runtime", None):
                    duration = parse_runtime(media.runtime)

        self.db.update_resume_state(
            imdb_id=media.imdb_id,
            media_type=media.media_type.value,
            title=media.title,
            year=str(media.year) if media.year else None,
            season=episode.season if episode else None,
            episode=episode.episode if episode else None,
            episode_title=episode.title if episode else None,
            position=playback_position or 0.0,
            duration=duration,
            last_watched=last_watched or utc_now_iso(),
            restore_continue=restore_continue,
        )
        self._sync_legacy_json()

    def record_watch(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        playback_position: Optional[float] = None,
        duration: Optional[float] = None,
        restore_continue: bool = True,
        last_watched: Optional[str] = None,
        save: bool = True,
        is_completed: Optional[bool] = None,
    ) -> None:
        """Save or update entry in resume state and record play if completed/indicated."""
        ts = last_watched or utc_now_iso()

        if playback_position is None:
            playback_position = self.get_playback_position(
                media.imdb_id,
                season=episode.season if episode else None,
                episode=episode.episode if episode else None,
            )

        if duration is None:
            duration = self.get_playback_duration(
                media.imdb_id,
                season=episode.season if episode else None,
                episode=episode.episode if episode else None,
            )
            if not duration:
                if episode and getattr(episode, "runtime", None):
                    duration = parse_runtime(episode.runtime)
                elif media and getattr(media, "runtime", None):
                    duration = parse_runtime(media.runtime)

        self.update_resume_state(
            media=media,
            episode=episode,
            playback_position=playback_position,
            duration=duration,
            last_watched=ts,
            restore_continue=restore_continue,
        )

        # For backward compatibility with tests/callers: if explicitly completed or at position 0.0
        # (and not starting playback from start), record play
        should_record_play = is_completed
        if should_record_play is None:
            if playback_position == 0.0 and duration is None:
                should_record_play = True

        if should_record_play:
            self.record_play(
                media=media,
                episode=episode,
                watched_at=ts,
            )

        if save:
            self._sync_legacy_json()

    def populate_missing_durations(
        self,
        items: List[Dict[str, Any]],
        metadata_client: Optional[Any] = None,
    ) -> None:
        """Ensure items have duration populated using stored data or Cinemeta."""
        import concurrent.futures

        needs_fetch = []
        modified = False

        for item in items:
            dur = item.get("duration")
            if dur and float(dur) > 0:
                continue

            imdb_id = item.get("imdb_id")
            s = item.get("season")
            e = item.get("episode")
            stored_dur = self.get_playback_duration(imdb_id, season=s, episode=e)
            if stored_dur and stored_dur > 0:
                item["duration"] = stored_dur
                modified = True
            elif metadata_client and imdb_id:
                needs_fetch.append(item)

        if needs_fetch and metadata_client:
            def _fetch_dur(it: Dict[str, Any]):
                try:
                    m_type = MediaType(it.get("media_type", "movie"))
                    mid = it.get("imdb_id")
                    details = metadata_client.get_details(m_type, mid)
                    if details and details.runtime:
                        sec = parse_runtime(details.runtime)
                        if sec and sec > 0:
                            return it, sec
                except Exception:
                    pass
                return it, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(needs_fetch))) as executor:
                futures = [executor.submit(_fetch_dur, it) for it in needs_fetch]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        it, dur = fut.result()
                        if dur and dur > 0:
                            it["duration"] = dur
                            self.set_playback_duration(
                                it.get("imdb_id"),
                                dur,
                                season=it.get("season"),
                                episode=it.get("episode"),
                            )
                            modified = True
                    except Exception:
                        pass

        if modified:
            self._sync_legacy_json()

    def get_resume_item(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        """Get resume item for a specific IMDb ID."""
        return self.db.get_resume_item(imdb_id)

    def get_history_item(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        """Alias for get_resume_item."""
        return self.db.get_resume_item(imdb_id)

    def get_last_watched(self) -> Optional[Dict[str, Any]]:
        """Get the most recently watched title from resume state or plays."""
        items = self.list_continue_watching(limit=1)
        if items:
            return items[0]
        plays = self.db.get_all_plays(limit=1)
        return plays[0] if plays else None

    def list_history(self, limit: Optional[int] = 15) -> List[Dict[str, Any]]:
        """Return watch history items (resume state entries) with up-to-date resume positions."""
        self._sync_positions()
        items = self.db.get_all_resume_items()
        if limit is None:
            return list(items)
        return items[:limit]

    def list_plays(self, limit: Optional[int] = 50, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """Return plays newest first with total count for dedicated History view pagination."""
        plays = self.db.get_all_plays(limit=limit, offset=offset)
        total = self.db.get_plays_count()
        return plays, total

    def list_continue_watching(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Filter hidden titles and return active continue watching items."""
        self._sync_positions()
        return self.db.list_continue_watching(limit=limit)

    def set_continue_hidden(self, imdb_id: str, hidden: bool) -> None:
        """Persist visibility atomically; failures leave disk unchanged."""
        # Update in-memory copy first to prepare payload
        keys = set(self._data.get("hidden_from_continue", []))
        if hidden:
            keys.add(imdb_id)
        else:
            keys.discard(imdb_id)
        updated = dict(self._data, hidden_from_continue=sorted(keys))

        # Write to temporary file with replace for atomic file swap
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.config_dir, delete=False,
            ) as stream:
                path = Path(stream.name)
                json.dump(updated, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            path.replace(self.history_file)
        finally:
            if path is not None and path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass

        # Update in SQLite database
        self.db.set_continue_hidden(imdb_id, hidden)
        self._data = updated

    def clear_history(self) -> None:
        """Clear all watch history, plays, resume points, and sync queue."""
        self.db.clear_all()
        self._data = {"items": [], "hidden_from_continue": []}
        if self.history_file.exists():
            try:
                self.history_file.unlink()
            except Exception:
                pass
        if self.positions_file.exists():
            try:
                self.positions_file.unlink()
            except Exception:
                pass
        if self.durations_file.exists():
            try:
                self.durations_file.unlink()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Subtitle Delay Persistence
    # -------------------------------------------------------------------------

    def get_sub_delay(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        server: Optional[str] = None,
    ) -> float:
        """Get saved subtitle delay in seconds."""
        delays_file = self.config_dir / "sub_delays.json"
        if not delays_file.exists():
            return 0.0
        try:
            with open(delays_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return 0.0

                clean_server = server.strip().lower() if server else ""
                candidates: List[str] = []
                if season is not None and episode is not None and clean_server:
                    candidates.append(f"{imdb_id}:{season}:{episode}:{clean_server}")
                if clean_server:
                    candidates.append(f"{imdb_id}:{clean_server}")
                if season is not None and episode is not None:
                    candidates.append(f"{imdb_id}:{season}:{episode}")
                candidates.append(imdb_id)

                for k in candidates:
                    if k in data:
                        return float(data[k])
                return 0.0
        except Exception:
            return 0.0

    def set_sub_delay(
        self,
        imdb_id: str,
        delay: float,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        server: Optional[str] = None,
    ) -> None:
        """Persist subtitle delay for this title, episode, and server."""
        delays_file = self.config_dir / "sub_delays.json"
        data: Dict[str, float] = {}
        if delays_file.exists():
            try:
                with open(delays_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
            except Exception:
                data = {}

        clean_server = server.strip().lower() if server else ""
        if season is not None and episode is not None and clean_server:
            key = f"{imdb_id}:{season}:{episode}:{clean_server}"
        elif clean_server:
            key = f"{imdb_id}:{clean_server}"
        elif season is not None and episode is not None:
            key = f"{imdb_id}:{season}:{episode}"
        else:
            key = imdb_id

        data[key] = round(delay, 2)
        try:
            temp_file = delays_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            temp_file.replace(delays_file)
        except Exception:
            try:
                with open(delays_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
