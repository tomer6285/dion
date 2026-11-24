from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from ..storage.history import HistoryManager
from .auth import TraktAuthManager
from .client import TraktClient
from .sync import TraktSyncManager

logger = logging.getLogger(__name__)

_listeners_lock = threading.Lock()
_refresh_listeners: List[Callable[[], None]] = []


def register_sync_refresh_listener(callback: Callable[[], None]) -> None:
    """Register a callback to be invoked when background sync saves new data."""
    with _listeners_lock:
        if callback not in _refresh_listeners:
            _refresh_listeners.append(callback)


def unregister_sync_refresh_listener(callback: Callable[[], None]) -> None:
    """Unregister a previously registered sync refresh callback."""
    with _listeners_lock:
        if callback in _refresh_listeners:
            _refresh_listeners.remove(callback)


def _notify_sync_refresh_listeners() -> None:
    """Notify all active UI listeners that new sync data has been saved."""
    with _listeners_lock:
        listeners = list(_refresh_listeners)
    for cb in listeners:
        try:
            cb()
        except Exception:
            pass


def start_background_sync(
    history_mgr: HistoryManager,
    auth_mgr: Optional[TraktAuthManager] = None,
    client: Optional[TraktClient] = None,
    on_page_saved: Optional[Callable] = None,
) -> Optional[threading.Thread]:
    """Start non-blocking background synchronization with Trakt on interactive launch."""
    auth = auth_mgr or TraktAuthManager(config_dir=history_mgr.config_dir)
    if not auth.has_credentials():
        return None

    if TraktSyncManager.is_syncing():
        logger.debug("Trakt sync already in progress, skipping background launch.")
        return None

    sync_mgr = TraktSyncManager(auth_mgr=auth, client=client)

    def _on_page(page_items):
        if on_page_saved:
            try:
                on_page_saved(page_items)
            except Exception:
                pass
        _notify_sync_refresh_listeners()

    def _worker():
        try:
            # Refresh token in the background if needed so launch never blocks
            token = auth.get_valid_token()
            if not token:
                return
            sync_mgr.sync_history(
                history_mgr=history_mgr,
                full=False,
                on_page_saved=_on_page,
            )
            _notify_sync_refresh_listeners()
        except Exception as e:
            logger.debug("Background Trakt sync encountered exception: %s", e)

    t = threading.Thread(target=_worker, name="dion-trakt-background-sync", daemon=True)
    t.start()
    return t
