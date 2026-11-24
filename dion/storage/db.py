from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Current UTC timestamp formatted as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class DatabaseManager:
    """Manages SQLite storage for watch history, resume state, and pending Trakt sync queue."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            
            with conn:
                # 1. Plays table: tracks individual completed plays (including rewatches)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS plays (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        imdb_id TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        year TEXT,
                        season INTEGER,
                        episode INTEGER,
                        episode_title TEXT,
                        watched_at TEXT NOT NULL,
                        trakt_play_id INTEGER,
                        source TEXT DEFAULT 'playback'
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_lookup ON plays (imdb_id, season, episode);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_watched_at ON plays (watched_at DESC);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_trakt_id ON plays (trakt_play_id);")

                # 2. Resume state table: one entry per show / movie for Continue Watching
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS resume_state (
                        imdb_id TEXT PRIMARY KEY,
                        media_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        year TEXT,
                        season INTEGER,
                        episode INTEGER,
                        episode_title TEXT,
                        position REAL DEFAULT 0.0,
                        duration REAL,
                        last_watched TEXT NOT NULL,
                        hidden INTEGER DEFAULT 0
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_resume_last_watched ON resume_state (last_watched DESC);")

                # 3. Sync queue table: durable pending additions and removals for Trakt
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sync_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        imdb_id TEXT NOT NULL,
                        title TEXT,
                        year TEXT,
                        season INTEGER,
                        episode INTEGER,
                        episode_title TEXT,
                        watched_at TEXT,
                        trakt_play_id INTEGER,
                        created_at TEXT NOT NULL
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_queue_action ON sync_queue (action);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_queue_lookup ON sync_queue (imdb_id, season, episode);")

                # 4. Metadata table: sync timestamps, migration markers, etc.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sync_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );
                """)

    def migrate_legacy_json(self, legacy_history_file: Path) -> bool:
        """Migrate legacy history.json entries into resume_state.
        
        Preserves existing resume data during migration.
        Leaves ambiguous legacy entries as resume data (confirmed Trakt plays imported via sync).
        """
        if not legacy_history_file.exists():
            return False

        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM sync_meta WHERE key = 'legacy_migrated';")
            if cursor.fetchone():
                return False

            try:
                with open(legacy_history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning("Failed to parse legacy history file for migration: %s", e)
                return False

            items = data.get("items", [])
            hidden_keys = set(data.get("hidden_from_continue", []))

            with conn:
                for it in items:
                    imdb_id = it.get("imdb_id")
                    if not imdb_id:
                        continue
                    m_type = it.get("media_type", "movie")
                    title = it.get("title", "Unknown")
                    year = str(it.get("year")) if it.get("year") else None
                    season = it.get("season")
                    episode = it.get("episode")
                    ep_title = it.get("episode_title")
                    pos = float(it.get("position", 0.0) or 0.0)
                    dur = it.get("duration")
                    if dur is not None:
                        try:
                            dur = float(dur)
                        except (ValueError, TypeError):
                            dur = None
                    last_watched = it.get("last_watched") or utc_now_iso()
                    hidden = 1 if imdb_id in hidden_keys else 0

                    conn.execute("""
                        INSERT OR REPLACE INTO resume_state (
                            imdb_id, media_type, title, year, season, episode,
                            episode_title, position, duration, last_watched, hidden
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (imdb_id, m_type, title, year, season, episode, ep_title, pos, dur, last_watched, hidden))

                conn.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('legacy_migrated', '1');")

        return True

    # -------------------------------------------------------------------------
    # Plays (Watch History) Operations
    # -------------------------------------------------------------------------

    def record_play(
        self,
        imdb_id: str,
        media_type: str,
        title: str,
        year: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        episode_title: Optional[str] = None,
        watched_at: Optional[str] = None,
        trakt_play_id: Optional[int] = None,
        source: str = "playback",
        queue_sync: bool = True,
    ) -> int:
        """Record an individual completed play. Rewatches create distinct entries."""
        ts = watched_at or utc_now_iso()
        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO plays (
                        imdb_id, media_type, title, year, season, episode,
                        episode_title, watched_at, trakt_play_id, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    imdb_id,
                    media_type,
                    title,
                    str(year) if year else None,
                    season,
                    episode,
                    episode_title,
                    ts,
                    trakt_play_id,
                    source,
                ))
                play_id = cursor.lastrowid or 0

                if queue_sync and trakt_play_id is None:
                    # Queue for push to Trakt if not already assigned a Trakt play ID
                    cursor.execute("""
                        INSERT INTO sync_queue (
                            action, media_type, imdb_id, title, year, season,
                            episode, episode_title, watched_at, created_at
                        ) VALUES ('add', ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (
                        "episode" if media_type == "series" else "movie",
                        imdb_id,
                        title,
                        str(year) if year else None,
                        season,
                        episode,
                        episode_title,
                        ts,
                        utc_now_iso(),
                    ))

        return play_id

    def update_play_trakt_id(self, play_id: int, trakt_play_id: int) -> None:
        """Backfill Trakt play ID on an existing local play."""
        with self.connection() as conn:
            with conn:
                conn.execute(
                    "UPDATE plays SET trakt_play_id = ? WHERE id = ? AND trakt_play_id IS NULL;",
                    (trakt_play_id, play_id),
                )

    def delete_play(self, play_id: int, queue_sync: bool = True) -> bool:
        """Delete a single play by ID. Any remaining plays for the episode/movie keep it watched."""
        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, imdb_id, media_type, title, year, season, episode, watched_at, trakt_play_id
                    FROM plays WHERE id = ?;
                """, (play_id,))
                row = cursor.fetchone()
                if not row:
                    return False

                cursor.execute("DELETE FROM plays WHERE id = ?;", (play_id,))

                if queue_sync:
                    cursor.execute("""
                        INSERT INTO sync_queue (
                            action, media_type, imdb_id, title, year, season,
                            episode, watched_at, trakt_play_id, created_at
                        ) VALUES ('remove', ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (
                        "episode" if row["media_type"] == "series" else "movie",
                        row["imdb_id"],
                        row["title"],
                        row["year"],
                        row["season"],
                        row["episode"],
                        row["watched_at"],
                        row["trakt_play_id"],
                        utc_now_iso(),
                    ))
        return True

    def clear_plays_for_episode(
        self,
        imdb_id: str,
        season: int,
        episode: int,
        queue_sync: bool = True,
    ) -> int:
        """Clear all plays for a specific episode."""
        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, imdb_id, media_type, title, year, season, episode, watched_at, trakt_play_id
                    FROM plays
                    WHERE imdb_id = ? AND season = ? AND episode = ?;
                """, (imdb_id, season, episode))
                rows = cursor.fetchall()
                if not rows:
                    return 0

                cursor.execute("""
                    DELETE FROM plays
                    WHERE imdb_id = ? AND season = ? AND episode = ?;
                """, (imdb_id, season, episode))

                if queue_sync:
                    for row in rows:
                        cursor.execute("""
                            INSERT INTO sync_queue (
                                action, media_type, imdb_id, title, year, season,
                                episode, watched_at, trakt_play_id, created_at
                            ) VALUES ('remove', 'episode', ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            row["imdb_id"],
                            row["title"],
                            row["year"],
                            row["season"],
                            row["episode"],
                            row["watched_at"],
                            row["trakt_play_id"],
                            utc_now_iso(),
                        ))
                return len(rows)

    def clear_plays_for_season(
        self,
        imdb_id: str,
        season: int,
        queue_sync: bool = True,
    ) -> int:
        """Clear all plays for an entire season."""
        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, imdb_id, media_type, title, year, season, episode, watched_at, trakt_play_id
                    FROM plays
                    WHERE imdb_id = ? AND season = ?;
                """, (imdb_id, season))
                rows = cursor.fetchall()
                if not rows:
                    return 0

                cursor.execute("DELETE FROM plays WHERE imdb_id = ? AND season = ?;", (imdb_id, season))

                if queue_sync:
                    for row in rows:
                        cursor.execute("""
                            INSERT INTO sync_queue (
                                action, media_type, imdb_id, title, year, season,
                                episode, watched_at, trakt_play_id, created_at
                            ) VALUES ('remove', 'episode', ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            row["imdb_id"],
                            row["title"],
                            row["year"],
                            row["season"],
                            row["episode"],
                            row["watched_at"],
                            row["trakt_play_id"],
                            utc_now_iso(),
                        ))
                return len(rows)

    def clear_plays_for_show_or_movie(
        self,
        imdb_id: str,
        include_specials: bool = True,
        queue_sync: bool = True,
    ) -> int:
        """Clear all plays for a movie or show. If include_specials is False, season 0 plays are preserved."""
        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                if include_specials:
                    cursor.execute("""
                        SELECT id, imdb_id, media_type, title, year, season, episode, watched_at, trakt_play_id
                        FROM plays WHERE imdb_id = ?;
                    """, (imdb_id,))
                else:
                    cursor.execute("""
                        SELECT id, imdb_id, media_type, title, year, season, episode, watched_at, trakt_play_id
                        FROM plays WHERE imdb_id = ? AND (season IS NULL OR season > 0);
                    """, (imdb_id,))
                rows = cursor.fetchall()
                if not rows:
                    return 0

                if include_specials:
                    cursor.execute("DELETE FROM plays WHERE imdb_id = ?;", (imdb_id,))
                else:
                    cursor.execute("DELETE FROM plays WHERE imdb_id = ? AND (season IS NULL OR season > 0);", (imdb_id,))

                if queue_sync:
                    for row in rows:
                        cursor.execute("""
                            INSERT INTO sync_queue (
                                action, media_type, imdb_id, title, year, season,
                                episode, watched_at, trakt_play_id, created_at
                            ) VALUES ('remove', ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            "episode" if row["media_type"] == "series" else "movie",
                            row["imdb_id"],
                            row["title"],
                            row["year"],
                            row["season"],
                            row["episode"],
                            row["watched_at"],
                            row["trakt_play_id"],
                            utc_now_iso(),
                        ))
                return len(rows)

    def bulk_mark_episodes_watched(
        self,
        imdb_id: str,
        title: str,
        year: Optional[str],
        episodes_to_mark: List[Dict[str, Any]],
        watched_at: Optional[str] = None,
        queue_sync: bool = True,
    ) -> int:
        """Atomically insert plays and sync queue entries for multiple episodes."""
        if not episodes_to_mark:
            return 0
        ts = watched_at or utc_now_iso()
        now_ts = utc_now_iso()

        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                for ep in episodes_to_mark:
                    s = ep.get("season")
                    e = ep.get("episode")
                    ep_title = ep.get("title")
                    cursor.execute("""
                        INSERT INTO plays (
                            imdb_id, media_type, title, year, season, episode,
                            episode_title, watched_at, source
                        ) VALUES (?, 'series', ?, ?, ?, ?, ?, ?, 'manual');
                    """, (imdb_id, title, str(year) if year else None, s, e, ep_title, ts))

                    if queue_sync:
                        cursor.execute("""
                            INSERT INTO sync_queue (
                                action, media_type, imdb_id, title, year, season,
                                episode, episode_title, watched_at, created_at
                            ) VALUES ('add', 'episode', ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            imdb_id,
                            title,
                            str(year) if year else None,
                            s,
                            e,
                            ep_title,
                            ts,
                            now_ts,
                        ))
        return len(episodes_to_mark)

    def is_watched(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> bool:
        """Returns True if at least one play exists for this movie or episode."""
        with self.connection() as conn:
            cursor = conn.cursor()
            if season is not None and episode is not None:
                cursor.execute("""
                    SELECT 1 FROM plays
                    WHERE imdb_id = ? AND season = ? AND episode = ? LIMIT 1;
                """, (imdb_id, season, episode))
            else:
                cursor.execute("SELECT 1 FROM plays WHERE imdb_id = ? LIMIT 1;", (imdb_id,))
            return cursor.fetchone() is not None

    def get_play_count(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> int:
        """Returns the number of plays for a movie or episode."""
        with self.connection() as conn:
            cursor = conn.cursor()
            if season is not None and episode is not None:
                cursor.execute("""
                    SELECT COUNT(*) FROM plays
                    WHERE imdb_id = ? AND season = ? AND episode = ?;
                """, (imdb_id, season, episode))
            else:
                cursor.execute("SELECT COUNT(*) FROM plays WHERE imdb_id = ?;", (imdb_id,))
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_watched_episode_set(self, imdb_id: str, season: Optional[int] = None) -> set[Tuple[int, int]]:
        """Get set of (season, episode) pairs that have at least one play."""
        with self.connection() as conn:
            cursor = conn.cursor()
            if season is not None:
                cursor.execute("""
                    SELECT DISTINCT season, episode FROM plays
                    WHERE imdb_id = ? AND season = ? AND episode IS NOT NULL;
                """, (imdb_id, season))
            else:
                cursor.execute("""
                    SELECT DISTINCT season, episode FROM plays
                    WHERE imdb_id = ? AND season IS NOT NULL AND episode IS NOT NULL;
                """, (imdb_id,))
            return {(r["season"], r["episode"]) for r in cursor.fetchall()}

    def get_all_plays(self, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        """Get all plays sorted newest first with optional pagination."""
        with self.connection() as conn:
            cursor = conn.cursor()
            if limit is not None:
                cursor.execute("""
                    SELECT id, imdb_id, media_type, title, year, season, episode,
                           episode_title, watched_at, trakt_play_id, source
                    FROM plays
                    ORDER BY watched_at DESC, id DESC
                    LIMIT ? OFFSET ?;
                """, (limit, offset))
            else:
                cursor.execute("""
                    SELECT id, imdb_id, media_type, title, year, season, episode,
                           episode_title, watched_at, trakt_play_id, source
                    FROM plays
                    ORDER BY watched_at DESC, id DESC;
                """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_plays_count(self) -> int:
        """Get total count of all plays."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM plays;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    # -------------------------------------------------------------------------
    # Resume State Operations (Continue Watching)
    # -------------------------------------------------------------------------

    def update_resume_state(
        self,
        imdb_id: str,
        media_type: str,
        title: str,
        year: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        episode_title: Optional[str] = None,
        position: float = 0.0,
        duration: Optional[float] = None,
        last_watched: Optional[str] = None,
        restore_continue: bool = True,
    ) -> None:
        """Update or insert resume state. At most one resume row per show/movie."""
        ts = last_watched or utc_now_iso()
        with self.connection() as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT hidden FROM resume_state WHERE imdb_id = ?;", (imdb_id,))
                existing = cursor.fetchone()

                hidden = 0
                if existing:
                    hidden = existing["hidden"]
                    if restore_continue:
                        hidden = 0

                cursor.execute("""
                    INSERT OR REPLACE INTO resume_state (
                        imdb_id, media_type, title, year, season, episode,
                        episode_title, position, duration, last_watched, hidden
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    imdb_id,
                    media_type,
                    title,
                    str(year) if year else None,
                    season,
                    episode,
                    episode_title,
                    round(position, 1),
                    round(duration, 1) if duration else None,
                    ts,
                    hidden,
                ))

    def list_continue_watching(self, limit: Optional[int] = 15) -> List[Dict[str, Any]]:
        """List active continue watching items (hidden == 0), ordered by last_watched DESC."""
        with self.connection() as conn:
            cursor = conn.cursor()
            if limit is not None:
                cursor.execute("""
                    SELECT imdb_id, media_type, title, year, season, episode,
                           episode_title, position, duration, last_watched
                    FROM resume_state
                    WHERE hidden = 0
                    ORDER BY last_watched DESC
                    LIMIT ?;
                """, (limit,))
            else:
                cursor.execute("""
                    SELECT imdb_id, media_type, title, year, season, episode,
                           episode_title, position, duration, last_watched
                    FROM resume_state
                    WHERE hidden = 0
                    ORDER BY last_watched DESC;
                """)
            return [dict(r) for r in cursor.fetchall()]

    def set_continue_hidden(self, imdb_id: str, hidden: bool) -> None:
        """Set hidden flag for an item in resume_state."""
        with self.connection() as conn:
            with conn:
                conn.execute("""
                    UPDATE resume_state SET hidden = ? WHERE imdb_id = ?;
                """, (1 if hidden else 0, imdb_id))

    def get_hidden_resume_ids(self) -> List[str]:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT imdb_id FROM resume_state WHERE hidden = 1;")
            return [r[0] for r in cursor.fetchall()]

    def get_resume_item(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT imdb_id, media_type, title, year, season, episode,
                       episode_title, position, duration, last_watched, hidden
                FROM resume_state WHERE imdb_id = ?;
            """, (imdb_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_resume_items(self) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT imdb_id, media_type, title, year, season, episode,
                       episode_title, position, duration, last_watched
                FROM resume_state
                ORDER BY last_watched DESC;
            """)
            return [dict(r) for r in cursor.fetchall()]

    def delete_resume_item(self, imdb_id: str) -> None:
        with self.connection() as conn:
            with conn:
                conn.execute("DELETE FROM resume_state WHERE imdb_id = ?;", (imdb_id,))

    def clear_all(self) -> None:
        """Clear all plays, resume entries, and pending sync actions."""
        with self.connection() as conn:
            with conn:
                conn.execute("DELETE FROM plays;")
                conn.execute("DELETE FROM resume_state;")
                conn.execute("DELETE FROM sync_queue;")

    # -------------------------------------------------------------------------
    # Sync Queue Operations
    # -------------------------------------------------------------------------

    def get_pending_sync_actions(self, action: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            cursor = conn.cursor()
            if action:
                cursor.execute("""
                    SELECT id, action, media_type, imdb_id, title, year, season,
                           episode, episode_title, watched_at, trakt_play_id, created_at
                    FROM sync_queue
                    WHERE action = ?
                    ORDER BY id ASC;
                """, (action,))
            else:
                cursor.execute("""
                    SELECT id, action, media_type, imdb_id, title, year, season,
                           episode, episode_title, watched_at, trakt_play_id, created_at
                    FROM sync_queue
                    ORDER BY id ASC;
                """)
            return [dict(r) for r in cursor.fetchall()]

    def delete_sync_actions(self, action_ids: List[int]) -> None:
        if not action_ids:
            return
        with self.connection() as conn:
            with conn:
                placeholders = ",".join("?" for _ in action_ids)
                conn.execute(f"DELETE FROM sync_queue WHERE id IN ({placeholders});", action_ids)

    def is_play_pending_removal(
        self,
        trakt_play_id: Optional[int] = None,
        imdb_id: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        watched_at: Optional[str] = None,
    ) -> bool:
        """Check if a specific play is currently pending deletion so remote sync doesn't re-import it."""
        with self.connection() as conn:
            cursor = conn.cursor()
            if trakt_play_id is not None:
                cursor.execute("""
                    SELECT 1 FROM sync_queue
                    WHERE action = 'remove' AND trakt_play_id = ? LIMIT 1;
                """, (trakt_play_id,))
                if cursor.fetchone():
                    return True

            if imdb_id is not None:
                if season is not None and episode is not None:
                    if watched_at is not None:
                        cursor.execute("""
                            SELECT 1 FROM sync_queue
                            WHERE action = 'remove' AND imdb_id = ? AND season = ? AND episode = ?
                              AND (watched_at IS NULL OR watched_at = ?)
                            LIMIT 1;
                        """, (imdb_id, season, episode, watched_at))
                    else:
                        cursor.execute("""
                            SELECT 1 FROM sync_queue
                            WHERE action = 'remove' AND imdb_id = ? AND season = ? AND episode = ?
                              AND watched_at IS NULL
                            LIMIT 1;
                        """, (imdb_id, season, episode))
                    if cursor.fetchone():
                        return True
                else:
                    if watched_at is not None:
                        cursor.execute("""
                            SELECT 1 FROM sync_queue
                            WHERE action = 'remove' AND imdb_id = ? AND season IS NULL AND episode IS NULL
                              AND (watched_at IS NULL OR watched_at = ?)
                            LIMIT 1;
                        """, (imdb_id, watched_at))
                    else:
                        cursor.execute("""
                            SELECT 1 FROM sync_queue
                            WHERE action = 'remove' AND imdb_id = ? AND season IS NULL AND episode IS NULL
                              AND watched_at IS NULL
                            LIMIT 1;
                        """, (imdb_id,))
                    if cursor.fetchone():
                        return True
            return False

    # -------------------------------------------------------------------------
    # Sync Metadata Operations
    # -------------------------------------------------------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM sync_meta WHERE key = ?;", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connection() as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?);", (key, value))
