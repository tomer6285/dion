from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ..metadata.models import EpisodeItem, MediaItem, MediaType


class ScrobbleAction(str, Enum):
    START = "start"
    PAUSE = "pause"
    STOP = "scrobble"


@dataclass
class TraktDeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class TraktToken:
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 7776000
    refresh_token: str = ""
    scope: str = "public"
    created_at: int = field(default_factory=lambda: int(time.time()))
    username: Optional[str] = None

    def is_expired(self, buffer_seconds: int = 86400) -> bool:
        """Returns True if the token is expired or within the buffer window (default 24h)."""
        now = int(time.time())
        return now >= (self.created_at + self.expires_in - buffer_seconds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
            "created_at": self.created_at,
            "username": self.username,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TraktToken:
        return cls(
            access_token=data.get("access_token", ""),
            token_type=data.get("token_type", "bearer"),
            expires_in=int(data.get("expires_in", 7776000)),
            refresh_token=data.get("refresh_token", ""),
            scope=data.get("scope", "public"),
            created_at=int(data.get("created_at", int(time.time()))),
            username=data.get("username"),
        )


@dataclass
class TraktPlaybackItem:
    """Represents a paused or in-progress item from /sync/playback."""
    id: int
    progress: float
    paused_at: datetime
    media_type: MediaType
    imdb_id: str
    title: str
    year: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    duration: Optional[float] = None


def format_trakt_scrobble_payload(
    media: MediaItem,
    episode: Optional[EpisodeItem] = None,
    progress: float = 0.0,
) -> Dict[str, Any]:
    """Format payload for Trakt /scrobble/start, /scrobble/pause, and /scrobble/stop."""
    # Trakt progress is a percentage between 0.0 and 100.0
    safe_progress = max(0.0, min(100.0, float(progress)))

    if episode:
        payload: Dict[str, Any] = {
            "show": {
                "title": media.title,
                "ids": {"imdb": media.imdb_id},
            },
            "episode": {
                "season": episode.season,
                "number": episode.episode,
            },
            "progress": round(safe_progress, 2),
        }
        if episode.title:
            payload["episode"]["title"] = episode.title
        return payload

    year_val = None
    if media.year:
        digits = "".join(c for c in str(media.year) if c.isdigit())
        if len(digits) >= 4:
            try:
                year_val = int(digits[:4])
            except ValueError:
                pass

    movie_obj: Dict[str, Any] = {
        "title": media.title,
        "ids": {"imdb": media.imdb_id},
    }
    if year_val:
        movie_obj["year"] = year_val

    return {
        "movie": movie_obj,
        "progress": round(safe_progress, 2),
    }
