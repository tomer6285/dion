from __future__ import annotations

import logging
import threading
from typing import Optional

from ..metadata.models import EpisodeItem, MediaItem
from .auth import TraktAuthManager
from .client import TraktClient

logger = logging.getLogger(__name__)


class TraktScrobbler:
    """Non-blocking, background scrobbler for Trakt.tv playback tracking."""

    def __init__(
        self,
        auth_mgr: Optional[TraktAuthManager] = None,
        client: Optional[TraktClient] = None,
        enabled: bool = True,
    ):
        self.auth_mgr = auth_mgr or TraktAuthManager()
        self.client = client or self.auth_mgr.client
        self.enabled = enabled
        self._lock = threading.Lock()
        self._active_session: Optional[dict] = None
        self.last_scrobble_result: Optional[dict] = None

    def start_playback(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        start_time: Optional[float] = None,
        duration: Optional[float] = None,
    ) -> None:
        """Asynchronously send /scrobble/start to Trakt."""
        if not self.enabled:
            return

        def _worker():
            try:
                token = self.auth_mgr.get_valid_token()
                if not token:
                    return

                progress = 0.0
                if start_time and duration and duration > 0:
                    progress = min(100.0, max(0.0, (start_time / duration) * 100.0))

                with self._lock:
                    self._active_session = {
                        "media": media,
                        "episode": episode,
                    }

                self.client.scrobble_start(
                    token=token,
                    media=media,
                    episode=episode,
                    progress=progress,
                )
            except Exception as e:
                logger.debug("Trakt scrobble start failed: %s", e)

        threading.Thread(target=_worker, name="dion-trakt-scrobble-start", daemon=True).start()

    def pause_playback(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        current_time: float = 0.0,
        duration: Optional[float] = None,
    ) -> None:
        """Asynchronously send /scrobble/pause to Trakt."""
        if not self.enabled:
            return

        def _worker():
            try:
                token = self.auth_mgr.get_valid_token()
                if not token:
                    return

                progress = 0.0
                if current_time and duration and duration > 0:
                    progress = min(100.0, max(0.0, (current_time / duration) * 100.0))

                self.client.scrobble_pause(
                    token=token,
                    media=media,
                    episode=episode,
                    progress=progress,
                )
            except Exception as e:
                logger.debug("Trakt scrobble pause failed: %s", e)

        threading.Thread(target=_worker, name="dion-trakt-scrobble-pause", daemon=True).start()

    def stop_playback(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        current_time: float = 0.0,
        duration: Optional[float] = None,
    ) -> Optional[dict]:
        """Synchronously or with short timeout send /scrobble/stop to Trakt.

        If progress >= 80%, Trakt automatically records the item as watched.
        Returns the scrobble response if available.
        """
        if not self.enabled:
            return None

        result: Optional[dict] = None

        def _worker():
            nonlocal result
            try:
                token = self.auth_mgr.get_valid_token()
                if not token:
                    return

                progress = 0.0
                if current_time and duration and duration > 0:
                    progress = min(100.0, max(0.0, (current_time / duration) * 100.0))
                elif current_time and current_time > 10:
                    # If duration missing but position >= 0, estimate or report minimum
                    progress = 10.0

                result = self.client.scrobble_stop(
                    token=token,
                    media=media,
                    episode=episode,
                    progress=progress,
                )
                self.last_scrobble_result = result
            except Exception as e:
                logger.debug("Trakt scrobble stop failed: %s", e)

        # Use short join timeout so exiting player returns promptly
        t = threading.Thread(target=_worker, name="dion-trakt-scrobble-stop", daemon=True)
        t.start()
        t.join(timeout=1.5)
        return result
