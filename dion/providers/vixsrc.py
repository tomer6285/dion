from __future__ import annotations

import json
import re
import time
from typing import List, Optional
import httpx

from ..metadata.models import StreamSource
from .base import BaseProvider


class VixSrcProvider(BaseProvider):
    """Streams movies and TV shows via VixSrc."""

    name = "vixsrc"
    BASE_URL = "https://vixsrc.to"

    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
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

    @staticmethod
    def _build_playlist_url(
        base_url: str, token: str, expires: str, can_fhd: bool
    ) -> str:
        """Append VixSrc auth query params, adding h=1 only when FHD is enabled."""
        if not (token and expires):
            return base_url
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}token={token}&expires={expires}"
        if can_fhd:
            url += "&h=1"
        return url

    def _resolve(self, path: str) -> List[StreamSource]:
        api_url = f"{self.BASE_URL}/api/{path}"
        try:
            resp = self.client.get(api_url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            src = data.get("src")
            if not src:
                return []

            embed_url = f"{self.BASE_URL}{src}" if src.startswith("/") else src
            embed_resp = self.client.get(embed_url)
            if embed_resp.status_code != 200:
                return []
            html = embed_resp.text

            token_m = re.search(r'token[\"\']\s*:\s*[\"\']([^\"\']+)', html)
            expires_m = re.search(r'expires[\"\']\s*:\s*[\"\']([^\"\']+)', html)
            token = token_m.group(1) if token_m else ""
            expires = expires_m.group(1) if expires_m else ""
            can_fhd = bool(re.search(r'window\.canPlayFHD\s*=\s*true\b', html, re.IGNORECASE))

            # Check if token is expired
            if expires:
                try:
                    if int(expires) - 60 < int(time.time()):
                        return []
                except Exception:
                    pass

            sources: List[StreamSource] = []

            # Extract servers from window.streams
            streams_m = re.search(r'window\.streams\s*=\s*(\[.*?\]);', html, re.DOTALL)
            stream_entries = []
            if streams_m:
                try:
                    stream_entries = json.loads(streams_m.group(1))
                except Exception:
                    stream_entries = []

            headers = {
                "User-Agent": self.headers["User-Agent"],
                "Referer": embed_url,
            }

            if stream_entries:
                for entry in stream_entries:
                    s_name = entry.get("name", "server").lower()
                    s_url = entry.get("url")
                    if not s_url:
                        continue
                    full_url = self._build_playlist_url(s_url, token, expires, can_fhd)
                    sources.append(
                        StreamSource(
                            url=full_url,
                            server=f"vixsrc-{s_name}",
                            is_hls=True,
                            quality="1080p" if can_fhd else "720p",
                            headers=headers,
                        )
                    )
            else:
                pl_m = re.search(r'url\s*:\s*[\"\']([^\"\']+)', html)
                if pl_m:
                    pl = pl_m.group(1)
                    full_url = self._build_playlist_url(pl, token, expires, can_fhd)
                    sources.append(
                        StreamSource(
                            url=full_url,
                            server="vixsrc",
                            is_hls=True,
                            quality="1080p" if can_fhd else "720p",
                            headers=headers,
                        )
                    )

            return sources
        except Exception:
            return []

    def resolve_movie(self, imdb_id: str, title: str) -> List[StreamSource]:
        return self._resolve(f"movie/{imdb_id}")

    def resolve_episode(
        self, imdb_id: str, season: int, episode: int, title: str
    ) -> List[StreamSource]:
        return self._resolve(f"tv/{imdb_id}/{season}/{episode}")
