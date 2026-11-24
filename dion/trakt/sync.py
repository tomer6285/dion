from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..metadata.models import EpisodeItem, MediaItem, MediaType
from ..storage.db import utc_now_iso
from ..storage.history import HistoryManager
from .auth import TraktAuthManager
from .client import TraktClient

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()


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

    _sync_lock = _sync_lock

    def __init__(
        self,
        auth_mgr: Optional[TraktAuthManager] = None,
        client: Optional[TraktClient] = None,
    ):
        self.auth_mgr = auth_mgr or TraktAuthManager()
        self.client = client or self.auth_mgr.client

    @classmethod
    def is_syncing(cls) -> bool:
        """Check if a synchronization task is currently running."""
        locked = _sync_lock.acquire(blocking=False)
        if locked:
            _sync_lock.release()
            return False
        return True

    def get_last_successful_sync_time(self, history_mgr: Optional[HistoryManager] = None) -> Optional[str]:
        """Get the timestamp of the last successful sync."""
        if history_mgr and hasattr(history_mgr, "db"):
            return history_mgr.db.get_meta("last_successful_sync_time")
        return None

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

        for loc in local_items:
            if loc.get("imdb_id") not in seen_ids:
                merged.append(loc)

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

    def _resolve_trakt_play_id(
        self, token: str, removal: Dict[str, Any]
    ) -> Tuple[Optional[int], bool]:
        """Resolve a single play's Trakt history ID by matching item and watched_at.

        Returns (trakt_play_id_or_None, request_succeeded).
        """
        target_imdb = removal.get("imdb_id")
        target_type = removal.get("media_type")
        target_season = removal.get("season")
        target_ep = removal.get("episode")
        target_dt = parse_iso_datetime(removal.get("watched_at"))
        if not target_imdb or not target_dt:
            return None, True

        page = 1
        page_size = 100
        seen_ids = set()
        while True:
            self.client._last_history_request_ok = True
            self.client._last_history_page_count = None
            items = self.client.get_history(token, limit=page_size, page=page)
            if not getattr(self.client, "_last_history_request_ok", True):
                return None, False
            if not items or not isinstance(items, list):
                return None, True

            page_ids = [h.get("id") for h in items if h.get("id") is not None]
            if page_ids and all(tid in seen_ids for tid in page_ids):
                return None, True
            seen_ids.update(page_ids)

            oldest_dt_in_page: Optional[datetime] = None
            for h in items:
                h_dt = parse_iso_datetime(h.get("watched_at"))
                if h_dt and (oldest_dt_in_page is None or h_dt < oldest_dt_in_page):
                    oldest_dt_in_page = h_dt

                h_type = h.get("type")
                if target_type == "movie" and h_type == "movie":
                    imdb_id = h.get("movie", {}).get("ids", {}).get("imdb")
                    if imdb_id == target_imdb and h_dt and abs((h_dt - target_dt).total_seconds()) <= 2:
                        return h.get("id"), True
                elif target_type == "episode" and h_type == "episode":
                    show_imdb = h.get("show", {}).get("ids", {}).get("imdb")
                    ep = h.get("episode", {})
                    if (
                        show_imdb == target_imdb
                        and ep.get("season") == target_season
                        and ep.get("number") == target_ep
                        and h_dt
                        and abs((h_dt - target_dt).total_seconds()) <= 2
                    ):
                        return h.get("id"), True

            if oldest_dt_in_page and oldest_dt_in_page < target_dt:
                return None, True

            page_count = getattr(self.client, "_last_history_page_count", None)
            if page_count is not None and page >= page_count:
                return None, True
            if len(items) < page_size:
                return None, True
            page += 1

    def sync_history(
        self,
        history_mgr: HistoryManager,
        full: bool = False,
        on_page_saved: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Tuple[int, int]:
        """Two-way history sync with non-overlapping lock, durable removals, and incremental checks."""
        acquired = _sync_lock.acquire(blocking=True, timeout=15.0)
        if not acquired:
            logger.debug("Could not acquire sync lock (sync already running)")
            return 0, 0

        try:
            token = self.auth_mgr.get_valid_token()
            if not token:
                return 0, 0

            pulled_count = 0
            pushed_count = 0
            sync_failed = False
            pull_failed = False

            # -----------------------------------------------------------------
            # Step 1: Process durable pending removals from sync_queue
            # -----------------------------------------------------------------
            acknowledged_removal_action_ids: List[int] = []
            cancelled_add_action_ids: List[int] = []
            pending_removals = history_mgr.db.get_pending_sync_actions(action="remove")
            pending_additions = history_mgr.db.get_pending_sync_actions(action="add")

            # Index pending additions by (imdb_id, season, episode, watched_at) so an offline
            # add followed by offline delete cancels out cleanly without touching Trakt.
            pending_add_by_key: Dict[Tuple[Any, ...], List[int]] = {}
            for a in pending_additions:
                key = (a["imdb_id"], a.get("season"), a.get("episode"), a.get("watched_at"))
                pending_add_by_key.setdefault(key, []).append(a["id"])

            if pending_removals:
                remove_ids: List[int] = []
                action_ids_to_clear: List[int] = []
                movies_to_remove: List[Dict[str, Any]] = []
                episodes_to_remove: List[Dict[str, Any]] = []
                resolved_noop_ids: List[int] = []

                for r in pending_removals:
                    if r.get("trakt_play_id"):
                        remove_ids.append(r["trakt_play_id"])
                        action_ids_to_clear.append(r["id"])
                    elif r.get("watched_at") is not None:
                        key = (r["imdb_id"], r.get("season"), r.get("episode"), r.get("watched_at"))
                        matching_adds = pending_add_by_key.get(key)
                        if matching_adds:
                            cancelled_add_action_ids.append(matching_adds.pop(0))
                            resolved_noop_ids.append(r["id"])
                            continue

                        resolved_id, req_ok = self._resolve_trakt_play_id(token, r)
                        if not req_ok:
                            sync_failed = True
                            continue
                        if resolved_id is not None:
                            remove_ids.append(resolved_id)
                            action_ids_to_clear.append(r["id"])
                        else:
                            resolved_noop_ids.append(r["id"])
                    else:
                        action_ids_to_clear.append(r["id"])
                        if r["media_type"] == "movie":
                            movies_to_remove.append({"ids": {"imdb": r["imdb_id"]}})
                        elif r["media_type"] == "episode" and r.get("season") is not None and r.get("episode") is not None:
                            episodes_to_remove.append({
                                "show": {"ids": {"imdb": r["imdb_id"]}},
                                "season": r["season"],
                                "number": r["episode"],
                            })

                payload: Dict[str, Any] = {}
                if remove_ids:
                    payload["ids"] = remove_ids
                if movies_to_remove:
                    payload["movies"] = movies_to_remove
                if episodes_to_remove:
                    payload["episodes"] = episodes_to_remove

                acknowledged_removal_action_ids.extend(resolved_noop_ids)
                if payload:
                    res = self.client.remove_from_history(token, payload)
                    if res is not None:
                        acknowledged_removal_action_ids.extend(action_ids_to_clear)
                    else:
                        sync_failed = True

            # -----------------------------------------------------------------
            # Step 2: Process durable pending additions from sync_queue
            # -----------------------------------------------------------------
            cancelled_add_set = set(cancelled_add_action_ids)
            movies_to_push = []
            shows_to_push: Dict[str, Any] = {}
            addition_action_ids = []

            for r in pending_additions:
                if r["id"] in cancelled_add_set:
                    continue
                addition_action_ids.append(r["id"])
                imdb_id = r["imdb_id"]
                if r["media_type"] == "movie":
                    movies_to_push.append({
                        "title": r.get("title"),
                        "ids": {"imdb": imdb_id},
                        "watched_at": r.get("watched_at"),
                    })
                elif r["media_type"] == "episode":
                    s_num = r.get("season", 1)
                    e_num = r.get("episode", 1)
                    if imdb_id not in shows_to_push:
                        shows_to_push[imdb_id] = {
                            "title": r.get("title"),
                            "ids": {"imdb": imdb_id},
                            "seasons": {},
                        }
                    show_entry = shows_to_push[imdb_id]
                    if s_num not in show_entry["seasons"]:
                        show_entry["seasons"][s_num] = []
                    show_entry["seasons"][s_num].append({
                        "number": e_num,
                        "watched_at": r.get("watched_at"),
                    })

            # Also check if any existing local plays lack trakt_play_id and aren't in sync_queue
            # (e.g. from legacy migration or test setup)
            existing_plays = history_mgr.db.get_all_plays()
            queued_keys = {
                (q["imdb_id"], q.get("season"), q.get("episode"), q.get("watched_at"))
                for q in pending_additions
            }
            for p in existing_plays:
                if p.get("trakt_play_id") is None and p.get("source") in ("playback", "manual", None):
                    key = (p["imdb_id"], p.get("season"), p.get("episode"), p.get("watched_at"))
                    if key in queued_keys:
                        continue
                    imdb_id = p["imdb_id"]
                    if p["media_type"] == "movie":
                        movies_to_push.append({
                            "title": p.get("title"),
                            "ids": {"imdb": imdb_id},
                            "watched_at": p.get("watched_at"),
                        })
                    elif p["media_type"] == "series":
                        s_num = p.get("season", 1)
                        e_num = p.get("episode", 1)
                        if imdb_id not in shows_to_push:
                            shows_to_push[imdb_id] = {
                                "title": p.get("title"),
                                "ids": {"imdb": imdb_id},
                                "seasons": {},
                            }
                        show_entry = shows_to_push[imdb_id]
                        if s_num not in show_entry["seasons"]:
                            show_entry["seasons"][s_num] = []
                        show_entry["seasons"][s_num].append({
                            "number": e_num,
                            "watched_at": p.get("watched_at"),
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

            if movies_to_push or formatted_shows:
                push_payload: Dict[str, Any] = {}
                if movies_to_push:
                    push_payload["movies"] = movies_to_push
                if formatted_shows:
                    push_payload["shows"] = formatted_shows

                res = self.client.add_to_history(token, push_payload)
                if res is not None:
                    added = res.get("added", {})
                    pushed_count = (
                        added.get("movies", 0)
                        + added.get("episodes", 0)
                        + added.get("shows", 0)
                    )
                    history_mgr.db.delete_sync_actions(addition_action_ids + cancelled_add_action_ids)
                else:
                    sync_failed = True
            elif cancelled_add_action_ids:
                history_mgr.db.delete_sync_actions(cancelled_add_action_ids)

            # -----------------------------------------------------------------
            # Step 3: Pull history from Trakt (newest first, page by page)
            # -----------------------------------------------------------------
            page_size = 100
            current_page = 1
            is_initial = history_mgr.db.get_meta("first_full_sync_completed") is None
            last_full_reconcile = history_mgr.db.get_meta("last_full_reconcile_time")
            reconcile_due = False
            if last_full_reconcile is None:
                reconcile_due = True
            else:
                rec_dt = parse_iso_datetime(last_full_reconcile)
                if rec_dt and (datetime.now(timezone.utc) - rec_dt).total_seconds() >= 86400:
                    reconcile_due = True
            do_full = full or is_initial or reconcile_due

            seen_trakt_ids = set()
            stopped_by_duplicate_mock = False

            while True:
                self.client._last_history_request_ok = True
                self.client._last_history_page_count = None
                page_items = self.client.get_history(token, limit=page_size, page=current_page)
                if not getattr(self.client, "_last_history_request_ok", True):
                    pull_failed = True
                    break
                if not page_items or not isinstance(page_items, list):
                    break

                # Guard against static-list mocks returning identical items on every page
                page_t_ids = [h.get("id") for h in page_items if h.get("id") is not None]
                if page_t_ids and all(tid in seen_trakt_ids for tid in page_t_ids):
                    stopped_by_duplicate_mock = True
                    break

                new_in_page = 0
                local_plays = history_mgr.db.get_all_plays()
                for h in page_items:
                    t_id = h.get("id")
                    if t_id and t_id in seen_trakt_ids:
                        continue
                    if t_id:
                        seen_trakt_ids.add(t_id)

                    watched_at = h.get("watched_at")
                    h_type = h.get("type")

                    if h_type == "movie":
                        m = h.get("movie", {})
                        imdb_id = m.get("ids", {}).get("imdb")
                        if not imdb_id:
                            continue
                        if history_mgr.db.is_play_pending_removal(
                            trakt_play_id=t_id, imdb_id=imdb_id, watched_at=watched_at
                        ):
                            continue

                        title = m.get("title", "Unknown")
                        year = str(m.get("year")) if m.get("year") else None

                        existing = [
                            p for p in local_plays
                            if (t_id and p.get("trakt_play_id") == t_id)
                            or (p.get("imdb_id") == imdb_id and p.get("watched_at") == watched_at)
                        ]
                        if existing:
                            if t_id and existing[0].get("trakt_play_id") is None:
                                history_mgr.db.update_play_trakt_id(existing[0]["id"], t_id)
                        else:
                            history_mgr.db.record_play(
                                imdb_id=imdb_id,
                                media_type="movie",
                                title=title,
                                year=year,
                                watched_at=watched_at,
                                trakt_play_id=t_id,
                                source="trakt",
                                queue_sync=False,
                            )
                            history_mgr.db.update_resume_state(
                                imdb_id=imdb_id,
                                media_type="movie",
                                title=title,
                                year=year,
                                position=0.0,
                                last_watched=watched_at,
                                restore_continue=False,
                            )
                            pulled_count += 1
                            new_in_page += 1

                    elif h_type == "episode":
                        show = h.get("show", {})
                        ep = h.get("episode", {})
                        imdb_id = show.get("ids", {}).get("imdb")
                        if not imdb_id:
                            continue
                        s_num = ep.get("season", 1)
                        e_num = ep.get("number", 1)
                        if history_mgr.db.is_play_pending_removal(
                            trakt_play_id=t_id, imdb_id=imdb_id, season=s_num, episode=e_num, watched_at=watched_at
                        ):
                            continue

                        title = show.get("title", "Unknown")
                        year = str(show.get("year")) if show.get("year") else None
                        ep_title = ep.get("title") or f"Episode {e_num}"

                        existing = [
                            p for p in local_plays
                            if (t_id and p.get("trakt_play_id") == t_id)
                            or (p.get("imdb_id") == imdb_id and p.get("season") == s_num and p.get("episode") == e_num and p.get("watched_at") == watched_at)
                        ]
                        if existing:
                            if t_id and existing[0].get("trakt_play_id") is None:
                                history_mgr.db.update_play_trakt_id(existing[0]["id"], t_id)
                        else:
                            history_mgr.db.record_play(
                                imdb_id=imdb_id,
                                media_type="series",
                                title=title,
                                year=year,
                                season=s_num,
                                episode=e_num,
                                episode_title=ep_title,
                                watched_at=watched_at,
                                trakt_play_id=t_id,
                                source="trakt",
                                queue_sync=False,
                            )
                            history_mgr.db.update_resume_state(
                                imdb_id=imdb_id,
                                media_type="series",
                                title=title,
                                year=year,
                                season=s_num,
                                episode=e_num,
                                episode_title=ep_title,
                                position=0.0,
                                last_watched=watched_at,
                                restore_continue=False,
                            )
                            pulled_count += 1
                            new_in_page += 1

                # Notify view if page had items
                if on_page_saved:
                    try:
                        on_page_saved(page_items)
                    except Exception:
                        pass

                # Incremental stop when page has no new plays
                if not do_full and new_in_page == 0:
                    break

                page_count = getattr(self.client, "_last_history_page_count", None)
                if page_count is not None and current_page >= page_count:
                    break

                if len(page_items) < page_size:
                    break

                current_page += 1

            # Reconcile remote deletions when a complete full pull succeeded
            if do_full and not pull_failed and not sync_failed and not is_initial and not stopped_by_duplicate_mock and seen_trakt_ids:
                for p in history_mgr.db.get_all_plays():
                    tid = p.get("trakt_play_id")
                    if tid is not None and tid not in seen_trakt_ids:
                        history_mgr.db.delete_play(p["id"], queue_sync=False)

            # -----------------------------------------------------------------
            # Step 4: Sync Continue Watching playback states in background
            # -----------------------------------------------------------------
            try:
                trakt_continue = self.fetch_continue_watching(max_age_days=60)
                for item in trakt_continue:
                    imdb_id = item["imdb_id"]
                    existing_resume = history_mgr.db.get_resume_item(imdb_id)
                    t_dt = parse_iso_datetime(item.get("last_watched"))
                    loc_dt = parse_iso_datetime(existing_resume.get("last_watched")) if existing_resume else None

                    if not existing_resume or (t_dt and loc_dt and t_dt > loc_dt):
                        history_mgr.db.update_resume_state(
                            imdb_id=imdb_id,
                            media_type=item["media_type"],
                            title=item["title"],
                            year=item.get("year"),
                            season=item.get("season"),
                            episode=item.get("episode"),
                            episode_title=item.get("episode_title"),
                            position=item.get("position", 0.0),
                            duration=item.get("duration"),
                            last_watched=item.get("last_watched"),
                            restore_continue=False,
                        )
            except Exception as e:
                logger.debug("Failed to sync continue watching items: %s", e)

            # Clear acknowledged removals from sync_queue now that remote pull has finished
            if acknowledged_removal_action_ids:
                history_mgr.db.delete_sync_actions(acknowledged_removal_action_ids)

            # Record successful sync metadata ONLY if pull and queue flush succeeded
            if not pull_failed and not sync_failed:
                if do_full:
                    history_mgr.db.set_meta("first_full_sync_completed", "1")
                    history_mgr.db.set_meta("last_full_reconcile_time", utc_now_iso())
                history_mgr.db.set_meta("last_successful_sync_time", utc_now_iso())
            history_mgr._sync_legacy_json()

            return pulled_count, pushed_count

        finally:
            _sync_lock.release()
