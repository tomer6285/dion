from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
import urllib.parse
import httpx

from ..metadata.models import StreamSource, SubtitleTrack
from .base import BaseProvider


class VidoraProvider(BaseProvider):
    """Streams movies and TV shows via Vidora (VidSrc v2 engine)."""

    name = "vidora"
    BASE_URL = "https://vidsrc.pm"
    SERVERS = ["scrapify", "oreon", "lookmovie", "vaplayer", "fsonic"]
    PLAYER_KEY = "f3b72e73c80c9a996574379798703796a1936efa3516a7105cb0e43048b46b5a"

    def __init__(self, timeout: float = 3.5):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "x-player-key": self.PLAYER_KEY,
            "Referer": f"{self.BASE_URL}/",
        }
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=2.0),
                headers=self.headers,
            )
        return self._client

    def close(self) -> None:
        if self._client and not self._client.is_closed:
            self._client.close()

    def _fetch_from_server(self, path: str, server: str) -> Optional[StreamSource]:
        url = f"{self.BASE_URL}/api/vidora{path}?source={server}"
        try:
            resp = self.client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("result"):
                return None

            sources = data.get("sources", [])
            if not sources:
                return None

            first_src = sources[0]
            stream_url = first_src.get("url")
            if not stream_url:
                return None

            headers = {
                "User-Agent": self.headers["User-Agent"],
                "Referer": f"{self.BASE_URL}/",
            }

            # Direct upstream CDN unwrap:
            # Vidora returns streams proxied via 'p1.netocdn.site/proxy?url=...' which suffers from
            # severe 503 throttling, segment skipping (causing A/V & subtitle desync), and high latency.
            # Unwrapping the direct upstream playlist provides zero 503s, instant startup, and stable sync.
            try:
                parsed_url = urllib.parse.urlsplit(stream_url)
                qs = urllib.parse.parse_qs(parsed_url.query)
                if "url" in qs and ("netocdn.site" in parsed_url.netloc or "proxy" in parsed_url.path):
                    target_url = qs["url"][0]
                    stream_url = target_url
                    if "data" in qs:
                        try:
                            raw_data = base64.b64decode(qs["data"][0]).decode("utf-8")
                            for item in raw_data.split("|"):
                                if "=" in item:
                                    k, v = item.split("=", 1)
                                    headers[k] = v
                        except Exception:
                            pass
                    target_parsed = urllib.parse.urlsplit(stream_url)
                    origin_base = f"{target_parsed.scheme}://{target_parsed.netloc}"
                    if "Origin" not in headers:
                        headers["Origin"] = origin_base
                    if "Referer" not in headers or headers["Referer"] == f"{self.BASE_URL}/":
                        headers["Referer"] = f"{origin_base}/"
            except Exception:
                pass

            subs: List[SubtitleTrack] = []
            for trk in first_src.get("tracks", []):
                f = trk.get("file")
                if f:
                    subs.append(
                        SubtitleTrack(
                            url=f,
                            label=trk.get("label", "Unknown"),
                        )
                    )

            return StreamSource(
                url=stream_url,
                server=f"vidora-{server}",
                is_hls=True,
                quality="1080p",
                headers=headers,
                subtitles=subs,
            )
        except Exception:
            return None

    def resolve_movie(self, imdb_id: str, title: str) -> List[StreamSource]:
        path = f"/v1/movie/{imdb_id}"
        return self._resolve_fast(path)

    def resolve_episode(
        self, imdb_id: str, season: int, episode: int, title: str
    ) -> List[StreamSource]:
        path = f"/v1/tv/{imdb_id}/{season}/{episode}"
        return self._resolve_fast(path)

    def _resolve_fast(self, path: str) -> List[StreamSource]:
        # Fast path: query the primary working server 'scrapify' first
        primary_src = self._fetch_from_server(path, "scrapify")
        if primary_src:
            return [primary_src]

        # If primary failed, query backup servers concurrently to avoid serial timeouts
        backups = [s for s in self.SERVERS if s != "scrapify"]
        results: List[StreamSource] = []
        with ThreadPoolExecutor(max_workers=len(backups)) as executor:
            futures = {executor.submit(self._fetch_from_server, path, s): s for s in backups}
            for fut in as_completed(futures):
                try:
                    src = fut.result()
                    if src:
                        results.append(src)
                        break
                except Exception:
                    pass
        return results

