from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
import httpx

from ..metadata.models import EpisodeItem, MediaItem
from .models import (
    TraktDeviceCode,
    TraktToken,
    format_trakt_scrobble_payload,
)

logger = logging.getLogger(__name__)

# Default Dion Application Client ID registered for Dion on Trakt.tv (public client)
DEFAULT_TRAKT_CLIENT_ID = "LUiYexjudpqu7Tl7kf7ZRWA2bMLnWrdwfhjSF8eao_U"


class TraktClient:
    """Client for Trakt.tv API v2 with automatic header handling and resilience."""

    BASE_URL = "https://api.trakt.tv"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: float = 8.0,
    ):
        self.client_id = (
            client_id
            or os.environ.get("TRAKT_CLIENT_ID")
            or DEFAULT_TRAKT_CLIENT_ID
        ).strip()
        self.client_secret = (
            client_secret
            or os.environ.get("TRAKT_CLIENT_SECRET")
            or ""
        ).strip()
        self.timeout = timeout

    def _headers(self, token: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def request_device_code(self) -> TraktDeviceCode:
        """Initiate OAuth 2.0 Device Code authorization."""
        url = f"{self.BASE_URL}/oauth/device/code"
        payload = {"client_id": self.client_id}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=self._headers())
            if resp.status_code != 200:
                err_desc = ""
                try:
                    data = resp.json()
                    err_desc = f": {data.get('error_description') or data.get('error')}"
                except Exception:
                    pass
                raise RuntimeError(f"Trakt device code request failed: HTTP {resp.status_code}{err_desc}")
            data = resp.json()
            return TraktDeviceCode(
                device_code=data["device_code"],
                user_code=data["user_code"],
                verification_url=data.get("verification_url", "https://trakt.tv/activate"),
                expires_in=int(data.get("expires_in", 600)),
                interval=int(data.get("interval", 5)),
            )

    def poll_device_token(self, device_code: str) -> tuple[Optional[TraktToken], str]:
        """Poll for token. Returns (token, status_string)."""
        url = f"{self.BASE_URL}/oauth/device/token"
        payload: Dict[str, Any] = {
            "code": device_code,
            "client_id": self.client_id,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                token = TraktToken(
                    access_token=data["access_token"],
                    token_type=data.get("token_type", "bearer"),
                    expires_in=int(data.get("expires_in", 7776000)),
                    refresh_token=data.get("refresh_token", ""),
                    scope=data.get("scope", "public"),
                    created_at=int(data.get("created_at", data.get("expires_in", 0))),
                )
                return token, "success"
            elif resp.status_code == 400:
                return None, "pending"
            elif resp.status_code == 404:
                return None, "invalid"
            elif resp.status_code == 409:
                return None, "already_used"
            elif resp.status_code == 410:
                return None, "expired"
            elif resp.status_code == 418:
                return None, "denied"
            elif resp.status_code == 429:
                return None, "slow_down"
            else:
                return None, f"error_{resp.status_code}"

    def refresh_token(self, refresh_token: str) -> Optional[TraktToken]:
        """Refresh expired OAuth token."""
        url = f"{self.BASE_URL}/oauth/token"
        payload: Dict[str, Any] = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers())
                if resp.status_code == 200:
                    data = resp.json()
                    return TraktToken(
                        access_token=data["access_token"],
                        token_type=data.get("token_type", "bearer"),
                        expires_in=int(data.get("expires_in", 7776000)),
                        refresh_token=data.get("refresh_token", refresh_token),
                        scope=data.get("scope", "public"),
                        created_at=int(data.get("created_at", 0)),
                    )
        except Exception as e:
            logger.warning("Failed to refresh Trakt token: %s", e)
        return None

    def revoke_token(self, token: str) -> bool:
        """Revoke an active OAuth access token on logout."""
        url = f"{self.BASE_URL}/oauth/revoke"
        payload: Dict[str, Any] = {
            "token": token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers())
                return resp.status_code == 200
        except Exception:
            return False

    def get_user_profile(self, token: str) -> Optional[Dict[str, Any]]:
        """Fetch current authenticated user profile."""
        url = f"{self.BASE_URL}/users/me"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, headers=self._headers(token))
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning("Failed to fetch Trakt profile: %s", e)
        return None

    def scrobble_start(
        self,
        token: str,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        progress: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Notify Trakt that playback has started."""
        url = f"{self.BASE_URL}/scrobble/start"
        payload = format_trakt_scrobble_payload(media, episode, progress)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers(token))
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            logger.warning("Trakt scrobble_start error: %s", e)
        return None

    def scrobble_pause(
        self,
        token: str,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        progress: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Notify Trakt that playback has paused."""
        url = f"{self.BASE_URL}/scrobble/pause"
        payload = format_trakt_scrobble_payload(media, episode, progress)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers(token))
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            logger.warning("Trakt scrobble_pause error: %s", e)
        return None

    def scrobble_stop(
        self,
        token: str,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        progress: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Notify Trakt that playback stopped. Auto-marks watched if progress >= 80%."""
        url = f"{self.BASE_URL}/scrobble/stop"
        payload = format_trakt_scrobble_payload(media, episode, progress)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers(token))
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            logger.warning("Trakt scrobble_stop error: %s", e)
        return None

    def get_playback(self, token: str) -> List[Dict[str, Any]]:
        """Retrieve paused playback items across devices."""
        url = f"{self.BASE_URL}/sync/playback?extended=full"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, headers=self._headers(token))
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning("Failed to fetch Trakt playback items: %s", e)
        return []

    def remove_playback(self, token: str, playback_id: int) -> bool:
        """Remove a playback item from Trakt's paused list."""
        url = f"{self.BASE_URL}/sync/playback/{playback_id}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.delete(url, headers=self._headers(token))
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.warning("Failed to delete Trakt playback item: %s", e)
        return False

    def get_watchlist(
        self, token: str, media_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve items from user's watchlist."""
        results: List[Dict[str, Any]] = []
        endpoints = []
        if media_type in (None, "movie", "movies"):
            endpoints.append(f"{self.BASE_URL}/sync/watchlist/movies?extended=full")
        if media_type in (None, "show", "shows", "series"):
            endpoints.append(f"{self.BASE_URL}/sync/watchlist/shows?extended=full")

        try:
            with httpx.Client(timeout=self.timeout) as client:
                for ep in endpoints:
                    resp = client.get(ep, headers=self._headers(token))
                    if resp.status_code == 200:
                        results.extend(resp.json())
        except Exception as e:
            logger.warning("Failed to fetch Trakt watchlist: %s", e)
        return results

    def get_history(
        self,
        token: str,
        limit: Optional[int] = 50,
        page: int = 1,
        start_at: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve user's watched history from Trakt.

        If limit is None, fetches complete history across all pages.
        Sets `_last_history_request_ok` and `_last_history_page_count` so callers
        can distinguish an empty HTTP 200 page from a failed request.
        """
        extra_query = f"&start_at={start_at}" if start_at else ""
        if limit is not None:
            self._last_history_request_ok = False
            self._last_history_page_count: Optional[int] = None
            url = f"{self.BASE_URL}/sync/history?page={page}&limit={limit}&extended=full{extra_query}"
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, headers=self._headers(token))
                    if resp.status_code == 200:
                        self._last_history_request_ok = True
                        page_count_header = resp.headers.get("X-Pagination-Page-Count")
                        if page_count_header:
                            try:
                                self._last_history_page_count = int(page_count_header)
                            except ValueError:
                                pass
                        return resp.json()
            except Exception as e:
                logger.warning("Failed to fetch Trakt history: %s", e)
            return []

        # Complete history across all pages
        results: List[Dict[str, Any]] = []
        current_page = 1
        page_size = 100
        while True:
            url = f"{self.BASE_URL}/sync/history?page={current_page}&limit={page_size}&extended=full{extra_query}"
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, headers=self._headers(token))
                    if resp.status_code != 200:
                        break
                    items = resp.json()
                    if not items or not isinstance(items, list):
                        break
                    results.extend(items)
                    page_count_header = resp.headers.get("X-Pagination-Page-Count")
                    if page_count_header:
                        try:
                            if current_page >= int(page_count_header):
                                break
                        except ValueError:
                            if len(items) < page_size:
                                break
                    elif len(items) < page_size:
                        break
                    current_page += 1
            except Exception as e:
                logger.warning("Failed to fetch Trakt history page %d: %s", current_page, e)
                break
        return results

    def add_to_history(self, token: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Mark items as watched in Trakt history."""
        url = f"{self.BASE_URL}/sync/history"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers(token))
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            logger.warning("Failed to add to Trakt history: %s", e)
        return None

    def remove_from_history(self, token: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Remove items from user's Trakt history."""
        url = f"{self.BASE_URL}/sync/history/remove"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers(token))
                if resp.status_code in (200, 201):
                    return resp.json()
        except Exception as e:
            logger.warning("Failed to remove from Trakt history: %s", e)
        return None
