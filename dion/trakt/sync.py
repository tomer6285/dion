from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..metadata.models import EpisodeItem, MediaItem, MediaType
from ..storage.history import HistoryManager
from .auth import TraktAuthManager
from .client import TraktClient

logger = logging.getLogger(__name__)


def parse_iso_datetime(dt_str: Any) -> Optional[datetime]:
    """Parse ISO datetime string, supporting Z suffix and offsets."""
    if not dt_str:
        return None
    val = str(dt_str).strip()
    if not val:
        return None
    try:
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class TraktSyncManager:
    """Handles synchronization of Watchlist, History, and Continue Watching between Dion and Trakt."""

    def __init__(
        self,
        auth_mgr: Optional[TraktAuthManager] = None,
        client: Optional[TraktClient] = None,
    ):
        self.auth_mgr = auth_mgr or TraktAuthManager()
        self.client = client or self.auth_mgr.client

    def fetch_continue_watching(
        self, max_age_days: int = 60
    ) -> List[Dict[str, Any]]:
        """Fetch paused items from Trakt, strictly filtered to the last `max_age_days` (default 60 days)."""
        token = self.auth_mgr.get_valid_token()
        if not token:
            return []

        raw_items = self.client.get_playback(token)
        if not raw_items:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        valid_items: List[Dict[str, Any]] = []

        for entry in raw_items:
            paused_at_str = entry.get("paused_at")
            paused_at = parse_iso_datetime(paused_at_str)
            if not paused_at or paused_at < cutoff:
                continue

            item_type = entry.get("type")
            progress = float(entry.get("progress", 0.0))

            if item_type == "movie":
                movie = entry.get("movie", {})
                ids = movie.get("ids", {})
                imdb_id = ids.get("imdb")
                if not imdb_id:
                    continue

                duration_min = movie.get("runtime")
                duration = float(duration_min * 60) if duration_min else None
                position = (progress / 100.0) * duration if duration else 0.0

                valid_items.append({
                    "imdb_id": imdb_id,
                    "title": movie.get("title", "Unknown"),
                    "media_type": MediaType.MOVIE.value,
                    "year": str(movie.get("year")) if movie.get("year") else None,
                    "position": round(position, 1),
                    "duration": round(duration, 1) if duration else None,
                    "last_watched": paused_at.isoformat(),
                    "progress_pct": progress,
                    "from_trakt": True,
                })

            elif item_type == "episode":
                show = entry.get("show", {})
                episode = entry.get("episode", {})
                show_ids = show.get("ids", {})
                imdb_id = show_ids.get("imdb")
                if not imdb_id:
                    continue

                s_num = episode.get("season", 1)
                e_num = episode.get("number", 1)
                duration_min = episode.get("runtime") or show.get("runtime")
                duration = float(duration_min * 60) if duration_min else None
                position = (progress / 100.0) * duration if duration else 0.0

                valid_items.append({
                    "imdb_id": imdb_id,
                    "title": show.get("title", "Unknown"),
                    "media_type": MediaType.SERIES.value,
                    "year": str(show.get("year")) if show.get("year") else None,
                    "season": s_num,
                    "episode": e_num,
                    "episode_title": episode.get("title") or f"Episode {e_num}",
                    "position": round(position, 1),
                    "duration": round(duration, 1) if duration else None,
                    "last_watched": paused_at.isoformat(),
                    "progress_pct": progress,
                    "from_trakt": True,
                })

        return valid_items

    def merge_continue_watching(
        self,
        local_items: List[Dict[str, Any]],
        max_age_days: int = 60,
    ) -> List[Dict[str, Any]]:
        """Merge local continue watching list with Trakt playback items within the last 60 days."""
        trakt_items = self.fetch_continue_watching(max_age_days=max_age_days)
        if not trakt_items:
            return list(local_items)

        # Index local items by imdb_id
        merged: List[Dict[str, Any]] = []
        local_map: Dict[str, Dict[str, Any]] = {
            i.get("imdb_id"): i for i in local_items if i.get("imdb_id")
        }
        seen_ids = set()

        # Combine items, with newer timestamp winning for duplicates
        for t_item in trakt_items:
            imdb_id = t_item["imdb_id"]
            seen_ids.add(imdb_id)
            if imdb_id in local_map:
                loc = local_map[imdb_id]
                t_dt = parse_iso_datetime(t_item.get("last_watched"))
                loc_dt = parse_iso_datetime(loc.get("last_watched"))
                # If Trakt entry is newer, take Trakt's position and episode
                if t_dt and loc_dt and t_dt > loc_dt:
                    combined = dict(loc)
                    combined["position"] = t_item["position"]
                    if t_item.get("duration"):
                        combined["duration"] = t_item["duration"]
                    if "season" in t_item and "episode" in t_item:
                        combined["season"] = t_item["season"]
                        combined["episode"] = t_item["episode"]
                        combined["episode_title"] = t_item.get("episode_title")
                    combined["last_watched"] = t_item["last_watched"]
                    combined["from_trakt"] = True
                    merged.append(combined)
                else:
                    merged.append(loc)
            else:
                merged.append(t_item)

        # Append any remaining local items not present on Trakt
        for loc in local_items:
            if loc.get("imdb_id") not in seen_ids:
                merged.append(loc)

        # Sort by last_watched descending
        def get_sort_key(item: Dict[str, Any]) -> datetime:
            dt = parse_iso_datetime(item.get("last_watched"))
            return dt if dt else datetime.min.replace(tzinfo=timezone.utc)

        merged.sort(key=get_sort_key, reverse=True)
        return merged

    def fetch_watchlist_media(self) -> List[MediaItem]:
        """Fetch user's Trakt watchlist converted to Dion MediaItems."""
        token = self.auth_mgr.get_valid_token()
        if not token:
            return []

        raw_list = self.client.get_watchlist(token)
        media_list: List[MediaItem] = []
        seen = set()

        for entry in raw_list:
            item_type = entry.get("type")
            if item_type == "movie":
                m = entry.get("movie", {})
                ids = m.get("ids", {})
                imdb_id = ids.get("imdb")
                if not imdb_id or imdb_id in seen:
                    continue
                seen.add(imdb_id)
                media_list.append(
                    MediaItem(
                        id=imdb_id,
                        imdb_id=imdb_id,
                        title=m.get("title", "Unknown"),
                        media_type=MediaType.MOVIE,
                        year=str(m.get("year")) if m.get("year") else None,
                        overview=m.get("overview"),
                        rating=str(round(m.get("rating", 0.0), 1)) if m.get("rating") else None,
                        runtime=str(m.get("runtime")) if m.get("runtime") else None,
                    )
                )
            elif item_type == "show":
                s = entry.get("show", {})
                ids = s.get("ids", {})
                imdb_id = ids.get("imdb")
                if not imdb_id or imdb_id in seen:
                    continue
                seen.add(imdb_id)
                media_list.append(
                    MediaItem(
                        id=imdb_id,
                        imdb_id=imdb_id,
                        title=s.get("title", "Unknown"),
                        media_type=MediaType.SERIES,
                        year=str(s.get("year")) if s.get("year") else None,
                        overview=s.get("overview"),
                        rating=str(round(s.get("rating", 0.0), 1)) if s.get("rating") else None,
                        runtime=str(s.get("runtime")) if s.get("runtime") else None,
                    )
                )

        return media_list

    def sync_history(self, history_mgr: HistoryManager) -> Tuple[int, int]:
        """Two-way history sync: pulls complete history from Trakt and pushes local history.

        Returns (pulled_count, pushed_count).
        """
        token = self.auth_mgr.get_valid_token()
        if not token:
            return 0, 0

        pulled_count = 0
        pushed_count = 0

        # 1. Pull complete Trakt history into Dion
        trakt_history = self.client.get_history(token, limit=None)
        local_history = history_mgr.list_history(limit=None)
        local_imdb_keys = {item.get("imdb_id") for item in local_history if item.get("imdb_id")}
        local_map: Dict[str, Dict[str, Any]] = {
            item.get("imdb_id"): item for item in local_history if item.get("imdb_id")
        }

        for h in trakt_history:
            h_type = h.get("type")
            watched_at = h.get("watched_at")
            if h_type == "movie":
                m = h.get("movie", {})
                imdb_id = m.get("ids", {}).get("imdb")
                if not imdb_id:
                    continue
                if imdb_id not in local_imdb_keys:
                    history_mgr.record_watch(
                        media=MediaItem(
                            id=imdb_id,
                            imdb_id=imdb_id,
                            title=m.get("title", "Unknown"),
                            media_type=MediaType.MOVIE,
                            year=str(m.get("year")) if m.get("year") else None,
                        ),
                        playback_position=0.0,
                        last_watched=watched_at,
                        save=False,
                    )
                    local_imdb_keys.add(imdb_id)
                    local_map[imdb_id] = {
                        "imdb_id": imdb_id,
                        "title": m.get("title", "Unknown"),
                        "media_type": MediaType.MOVIE.value,
                        "last_watched": watched_at,
                    }
                    pulled_count += 1
                else:
                    loc = local_map[imdb_id]
                    t_dt = parse_iso_datetime(watched_at)
                    loc_dt = parse_iso_datetime(loc.get("last_watched"))
                    if t_dt and loc_dt and t_dt > loc_dt:
                        loc["last_watched"] = watched_at
                        history_mgr.record_watch(
                            media=MediaItem(
                                id=imdb_id,
                                imdb_id=imdb_id,
                                title=m.get("title", loc.get("title", "Unknown")),
                                media_type=MediaType.MOVIE,
                                year=str(m.get("year")) if m.get("year") else loc.get("year"),
                            ),
                            playback_position=0.0,
                            last_watched=watched_at,
                            save=False,
                        )
                        pulled_count += 1
            elif h_type == "episode":
                show = h.get("show", {})
                ep = h.get("episode", {})
                imdb_id = show.get("ids", {}).get("imdb")
                if not imdb_id:
                    continue
                s_num = ep.get("season", 1)
                e_num = ep.get("number", 1)
                if imdb_id not in local_imdb_keys:
                    history_mgr.record_watch(
                        media=MediaItem(
                            id=imdb_id,
                            imdb_id=imdb_id,
                            title=show.get("title", "Unknown"),
                            media_type=MediaType.SERIES,
                            year=str(show.get("year")) if show.get("year") else None,
                        ),
                        episode=EpisodeItem(
                            id=f"{imdb_id}:{s_num}:{e_num}",
                            season=s_num,
                            episode=e_num,
                            title=ep.get("title") or f"Episode {e_num}",
                        ),
                        playback_position=0.0,
                        last_watched=watched_at,
                        save=False,
                    )
                    local_imdb_keys.add(imdb_id)
                    local_map[imdb_id] = {
                        "imdb_id": imdb_id,
                        "title": show.get("title", "Unknown"),
                        "media_type": MediaType.SERIES.value,
                        "season": s_num,
                        "episode": e_num,
                        "last_watched": watched_at,
                    }
                    pulled_count += 1
                else:
                    loc = local_map[imdb_id]
                    t_dt = parse_iso_datetime(watched_at)
                    loc_dt = parse_iso_datetime(loc.get("last_watched"))
                    if t_dt and loc_dt and t_dt > loc_dt:
                        history_mgr.record_watch(
                            media=MediaItem(
                                id=imdb_id,
                                imdb_id=imdb_id,
                                title=show.get("title", loc.get("title", "Unknown")),
                                media_type=MediaType.SERIES,
                                year=str(show.get("year")) if show.get("year") else loc.get("year"),
                            ),
                            episode=EpisodeItem(
                                id=f"{imdb_id}:{s_num}:{e_num}",
                                season=s_num,
                                episode=e_num,
                                title=ep.get("title") or f"Episode {e_num}",
                            ),
                            playback_position=0.0,
                            last_watched=watched_at,
                            save=False,
                        )
                        loc["season"] = s_num
                        loc["episode"] = e_num
                        loc["last_watched"] = watched_at
                        pulled_count += 1

        if pulled_count > 0:
            def get_sort_key(item: Dict[str, Any]) -> datetime:
                dt = parse_iso_datetime(item.get("last_watched"))
                return dt if dt else datetime.min.replace(tzinfo=timezone.utc)

            history_mgr._data.get("items", []).sort(key=get_sort_key, reverse=True)
            history_mgr._save()

        # 2. Push local watched items to Trakt
        trakt_movie_keys = set()
        trakt_episode_keys = set()
        for h in trakt_history:
            h_type = h.get("type")
            if h_type == "movie":
                m = h.get("movie", {})
                imdb = m.get("ids", {}).get("imdb")
                if imdb:
                    trakt_movie_keys.add(imdb)
            elif h_type == "episode":
                show = h.get("show", {})
                ep = h.get("episode", {})
                s_imdb = show.get("ids", {}).get("imdb")
                s_num = ep.get("season")
                e_num = ep.get("number")
                if s_imdb and s_num is not None and e_num is not None:
                    trakt_episode_keys.add((s_imdb, s_num, e_num))
                elif s_imdb:
                    trakt_episode_keys.add((s_imdb, None, None))

        movies_to_push = []
        shows_to_push: Dict[str, Any] = {}
        episodes_to_push = []

        current_local = history_mgr.list_history(limit=None)
        for loc in current_local:
            imdb_id = loc.get("imdb_id")
            if not imdb_id:
                continue

            m_type = loc.get("media_type")
            if m_type == MediaType.MOVIE.value:
                if imdb_id in trakt_movie_keys:
                    continue
                movies_to_push.append({
                    "title": loc.get("title"),
                    "ids": {"imdb": imdb_id},
                    "watched_at": loc.get("last_watched"),
                })
            elif m_type == MediaType.SERIES.value:
                s_num = loc.get("season")
                e_num = loc.get("episode")
                if s_num is not None and e_num is not None:
                    if (imdb_id, s_num, e_num) in trakt_episode_keys or (imdb_id, None, None) in trakt_episode_keys:
                        continue
                    episodes_to_push.append({
                        "show": {
                            "title": loc.get("title"),
                            "ids": {"imdb": imdb_id},
                        },
                        "season": s_num,
                        "number": e_num,
                        "watched_at": loc.get("last_watched"),
                    })
                    if imdb_id not in shows_to_push:
                        shows_to_push[imdb_id] = {
                            "title": loc.get("title"),
                            "ids": {"imdb": imdb_id},
                            "seasons": {},
                        }
                    show_entry = shows_to_push[imdb_id]
                    if s_num not in show_entry["seasons"]:
                        show_entry["seasons"][s_num] = []
                    show_entry["seasons"][s_num].append({
                        "number": e_num,
                        "watched_at": loc.get("last_watched"),
                    })

        formatted_shows = []
        for s_data in shows_to_push.values():
            formatted_shows.append({
                "title": s_data["title"],
                "ids": s_data["ids"],
                "seasons": [
                    {"number": s, "episodes": eps}
                    for s, eps in s_data["seasons"].items()
                ],
            })

        if movies_to_push or formatted_shows or episodes_to_push:
            payload: Dict[str, Any] = {}
            if movies_to_push:
                payload["movies"] = movies_to_push
            if formatted_shows:
                payload["shows"] = formatted_shows
            elif episodes_to_push:
                payload["episodes"] = episodes_to_push

            res = self.client.add_to_history(token, payload)
            if res:
                added = res.get("added", {})
                pushed_count = (
                    added.get("movies", 0)
                    + added.get("episodes", 0)
                    + added.get("shows", 0)
                )

        return pulled_count, pushed_count
