"""Trakt.tv integration for Dion."""

from .auth import TraktAuthManager
from .client import TraktClient
from .models import (
    ScrobbleAction,
    TraktDeviceCode,
    TraktPlaybackItem,
    TraktToken,
    format_trakt_scrobble_payload,
)
from .scrobbler import TraktScrobbler
from .sync import TraktSyncManager

__all__ = [
    "ScrobbleAction",
    "TraktAuthManager",
    "TraktClient",
    "TraktDeviceCode",
    "TraktPlaybackItem",
    "TraktScrobbler",
    "TraktSyncManager",
    "TraktToken",
    "format_trakt_scrobble_payload",
]
