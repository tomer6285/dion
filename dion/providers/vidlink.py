from __future__ import annotations

import base64
import json
import struct
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx
try:
    import _cffi_backend
except ImportError:
    pass
import nacl.secret
import nacl.utils

from ..metadata.models import StreamSource, SubtitleTrack
from .base import BaseProvider

# Production XSalsa20-Poly1305 key for VidLink Pro API
KEY_HEX = "c75136c5668bbfe65a7ecad431a745db68b5f381555b38d8f6c699449cf11fcd"
KEY_BYTES = bytes.fromhex(KEY_HEX)
BOX = nacl.secret.SecretBox(KEY_BYTES)

# Global in-memory cache for IMDb ID -> TMDB ID mapping
_TMDB_CACHE: Dict[str, str] = {}


def encrypt_vidlink_token(media_id: str, offset_seconds: int = 480) -> str:
    """
    Encrypt media ID with XSalsa20-Poly1305 and an adaptive timestamp offset.
    Produces the URL-safe base64 token required by VidLink endpoints.
    """
    timestamp = int(time.time() + offset_seconds)
    message = str(media_id).strip().encode("utf-8") + struct.pack(">Q", timestamp)
    nonce = bytes(24)
    encrypted = BOX.encrypt(message, nonce)
    full_payload = nonce + encrypted.ciphertext
    return base64.urlsafe_b64encode(full_payload).decode("utf-8").rstrip("=")


def decrypt_vidlink_token(token_str: str) -> Tuple[str, int]:
    """
    Decrypt a VidLink token back into (media_id, timestamp).
    """
    token_clean = token_str.strip()
    rem = len(token_clean) % 4
    if rem:
        token_clean += "=" * (4 - rem)
    data = base64.urlsafe_b64decode(token_clean)
    nonce = data[:24]
    ciphertext = data[24:]
    decrypted = BOX.decrypt(ciphertext, nonce)
    media_id = decrypted[:-8].decode("utf-8")
    timestamp = struct.unpack(">Q", decrypted[-8:])[0]
    return media_id, timestamp


def decrypt_vidlink_payload(payload: str | bytes) -> str:
    """
    Decrypt an arbitrary XSalsa20-Poly1305 encrypted endpoint payload.
    Supports either raw ciphertext bytes or URL-safe base64 string.
    """
    if isinstance(payload, str):
        payload_clean = payload.strip().strip('"')
        rem = len(payload_clean) % 4
        if rem:
            payload_clean += "=" * (4 - rem)
        raw = base64.urlsafe_b64decode(payload_clean)
    else:
        raw = payload

    nonce = raw[:24]
    ciphertext = raw[24:]
    decrypted = BOX.decrypt(ciphertext, nonce)
    return decrypted.decode("utf-8")


def resolve_tmdb_id(
    imdb_id: str,
    media_type: str = "movie",
    client: Optional[httpx.Client] = None,
    timeout: float = 4.0,
) -> Optional[str]:
    """
    Convert Cinemeta IMDb ID (tt...) to TMDB numeric ID via lightweight mapping endpoint.
    Caches results in memory for high-performance repeat lookups.
    """
    clean_id = imdb_id.strip()
    if clean_id.isdigit():
        return clean_id

    cache_key = f"{clean_id}:{media_type}"
    if cache_key in _TMDB_CACHE:
        return _TMDB_CACHE[cache_key]

    local_client = client or httpx.Client(
        timeout=timeout,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )

    try:
        # Primary lookup: themoviedb.vidsrc.su lightweight mapping endpoint
        primary_url = f"https://themoviedb.vidsrc.su/api/tmdb/find/{clean_id}?external_source=imdb_id"
        resp = local_client.get(primary_url)
        if resp.status_code == 200:
            data = resp.json()
            results_key = "movie_results" if media_type == "movie" else "tv_results"
            fallback_key = "tv_results" if media_type == "movie" else "movie_results"
            items = data.get(results_key) or data.get(fallback_key) or []
            if items and isinstance(items, list):
                tmdb_id = str(items[0].get("id"))
                if tmdb_id and tmdb_id != "None":
                    _TMDB_CACHE[cache_key] = tmdb_id
                    return tmdb_id

        # Secondary fallback: Stremio Cinemeta metadata API
        cinemeta_type = "movie" if media_type == "movie" else "series"
        cm_url = f"https://v3-cinemeta.strem.io/meta/{cinemeta_type}/{clean_id}.json"
        cm_resp = local_client.get(cm_url)
        if cm_resp.status_code == 200:
            meta = cm_resp.json().get("meta", {})
            moviedb_id = meta.get("moviedb_id")
            if moviedb_id:
                tmdb_id = str(moviedb_id)
                _TMDB_CACHE[cache_key] = tmdb_id
                return tmdb_id
    except Exception:
        pass
    finally:
        if client is None:
            local_client.close()

    return None


class VidLinkProvider(BaseProvider):
    """
    Streams movies and TV shows via VidLink (vidlink.pro).
    Tier S media provider serving pristine multi-quality streams and embedded captions.
    """

    name = "vidlink"
    BASE_URL = "https://vidlink.pro"

    def __init__(self, timeout: float = 4.5):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            ),
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
        }
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=2.5),
                headers=self.headers,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client and not self._client.is_closed:
            self._client.close()

    def _parse_stream_data(self, data: Dict[str, Any]) -> List[StreamSource]:
        """
        Extract StreamSource objects and subtitles from VidLink JSON response.
        Supports both direct multi-quality file streams and HLS playlists.
        """
        stream_info = data.get("stream")
        if not stream_info or not isinstance(stream_info, dict):
            return []

        source_id = str(data.get("sourceId", "vidlink")).lower().strip()

        # Extract embedded captions / subtitles
        subtitles: List[SubtitleTrack] = []
        for cap in stream_info.get("captions", []):
            if isinstance(cap, dict) and cap.get("url"):
                lang_label = cap.get("language") or "Unknown"
                subtitles.append(
                    SubtitleTrack(
                        url=cap["url"],
                        label=lang_label,
                        lang=lang_label[:3].lower(),
                    )
                )

        sources: List[StreamSource] = []

        # 1. Multi-quality file/stream resolution (sorted highest resolution first)
        qualities = stream_info.get("qualities")
        if isinstance(qualities, dict) and qualities:
            def quality_sort_key(item: Tuple[str, Any]) -> int:
                q_key = str(item[0]).strip().lower().replace("p", "")
                if q_key == "4k":
                    return 2160
                return int(q_key) if q_key.isdigit() else 0

            sorted_qualities = sorted(qualities.items(), key=quality_sort_key, reverse=True)
            for q_key, q_val in sorted_qualities:
                if not isinstance(q_val, dict):
                    continue
                url = q_val.get("url")
                if not url:
                    continue
                q_type = str(q_val.get("type", "")).lower()
                is_hls = (q_type in ("hls", "m3u8")) or (".m3u8" in url)
                q_label = f"{q_key}p" if str(q_key).isdigit() else str(q_key)

                sources.append(
                    StreamSource(
                        url=url,
                        server="vidlink",
                        is_hls=is_hls,
                        quality=q_label,
                        headers={},
                        subtitles=list(subtitles),
                    )
                )

        # 2. HLS master manifest playlist (if available and no qualities or as addition)
        playlist_url = stream_info.get("playlist")
        if playlist_url and not sources:
            sources.append(
                StreamSource(
                    url=playlist_url,
                    server="vidlink",
                    is_hls=True,
                    quality="1080p",
                    headers={},
                    subtitles=list(subtitles),
                )
            )

        # 3. Direct single stream url fallback
        direct_url = stream_info.get("url")
        if direct_url and not sources:
            is_hls = ".m3u8" in direct_url
            sources.append(
                StreamSource(
                    url=direct_url,
                    server="vidlink",
                    is_hls=is_hls,
                    quality="1080p",
                    headers={},
                    subtitles=list(subtitles),
                )
            )

        return sources

    def resolve_movie(self, imdb_id: str, title: str) -> List[StreamSource]:
        """Resolve streaming sources for a movie using VidLink."""
        try:
            tmdb_id = resolve_tmdb_id(imdb_id, media_type="movie", client=self.client)
            if not tmdb_id:
                return []

            token = encrypt_vidlink_token(tmdb_id)
            url = f"{self.BASE_URL}/api/b/movie/{token}?multiLang=1"
            resp = self.client.get(url)

            if resp.status_code != 200:
                return []

            # Check if response is encrypted payload string or plain JSON
            text = resp.text.strip()
            if not text or text == "null":
                return []

            if text.startswith("{") or text.startswith("["):
                data = resp.json()
            else:
                decrypted_json = decrypt_vidlink_payload(text)
                data = json.loads(decrypted_json)

            if not isinstance(data, dict):
                return []

            return self._parse_stream_data(data)
        except Exception:
            return []

    def resolve_episode(
        self, imdb_id: str, season: int, episode: int, title: str
    ) -> List[StreamSource]:
        """Resolve streaming sources for a TV episode using VidLink."""
        try:
            tmdb_id = resolve_tmdb_id(imdb_id, media_type="tv", client=self.client)
            if not tmdb_id:
                return []

            token = encrypt_vidlink_token(tmdb_id)
            url = f"{self.BASE_URL}/api/b/tv/{token}/{season}/{episode}?multiLang=1"
            resp = self.client.get(url)

            if resp.status_code != 200:
                return []

            text = resp.text.strip()
            if not text or text == "null":
                return []

            if text.startswith("{") or text.startswith("["):
                data = resp.json()
            else:
                decrypted_json = decrypt_vidlink_payload(text)
                data = json.loads(decrypted_json)

            if not isinstance(data, dict):
                return []

            return self._parse_stream_data(data)
        except Exception:
            return []
