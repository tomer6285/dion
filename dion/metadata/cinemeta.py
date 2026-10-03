from __future__ import annotations

import urllib.parse
from typing import List, Optional
import httpx

from .models import EpisodeItem, MediaItem, MediaType


class CinemetaClient:
    """Zero-configuration metadata client using Stremio Cinemeta API."""

    BASE_URL = "https://v3-cinemeta.strem.io"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    def search(self, query: str) -> List[MediaItem]:
        """Search movies and TV shows simultaneously."""
        encoded = urllib.parse.quote(query.strip())
        results: List[MediaItem] = []

        with httpx.Client(timeout=self.timeout) as client:
            # Search Movies
            movie_url = f"{self.BASE_URL}/catalog/movie/top/search={encoded}.json"
            try:
                resp = client.get(movie_url)
                if resp.status_code == 200:
                    for item in resp.json().get("metas", []):
                        results.append(
                            MediaItem(
                                id=item.get("id", ""),
                                imdb_id=item.get("imdb_id") or item.get("id", ""),
                                title=item.get("name", "Unknown"),
                                media_type=MediaType.MOVIE,
                                year=item.get("releaseInfo"),
                                overview=item.get("description"),
                                poster=item.get("poster"),
                            )
                        )
            except Exception:
                pass

            # Search TV Shows
            series_url = f"{self.BASE_URL}/catalog/series/top/search={encoded}.json"
            try:
                resp = client.get(series_url)
                if resp.status_code == 200:
                    for item in resp.json().get("metas", []):
                        results.append(
                            MediaItem(
                                id=item.get("id", ""),
                                imdb_id=item.get("imdb_id") or item.get("id", ""),
                                title=item.get("name", "Unknown"),
                                media_type=MediaType.SERIES,
                                year=item.get("releaseInfo"),
                                overview=item.get("description"),
                                poster=item.get("poster"),
                            )
                        )
            except Exception:
                pass

        # De-duplicate by ID while maintaining order
        seen = set()
        deduped: List[MediaItem] = []
        for item in results:
            if item.id and item.id not in seen:
                seen.add(item.id)
                deduped.append(item)

        return deduped

    def get_episodes(self, imdb_id: str) -> List[EpisodeItem]:
        """Fetch all seasons and episodes for a TV series."""
        url = f"{self.BASE_URL}/meta/series/{imdb_id}.json"
        episodes: List[EpisodeItem] = []

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return episodes

            data = resp.json().get("meta", {})
            raw_videos = data.get("videos", [])

            for v in raw_videos:
                season = v.get("season", 0)
                ep_num = v.get("number", v.get("episode", 0))
                # Skip specials / season 0 if season is 0 unless it's all there is
                episodes.append(
                    EpisodeItem(
                        id=v.get("id", f"{imdb_id}:{season}:{ep_num}"),
                        season=season,
                        episode=ep_num,
                        title=v.get("name") or v.get("title") or f"Episode {ep_num}",
                        overview=v.get("overview") or v.get("description"),
                        released=v.get("released") or v.get("firstAired"),
                    )
                )

        # Sort by season and episode number
        episodes.sort(key=lambda ep: (ep.season, ep.episode))
        return episodes

    def get_details(self, media_type: MediaType, imdb_id: str) -> Optional[MediaItem]:
        """Fetch full metadata for a specific movie or series."""
        url = f"{self.BASE_URL}/meta/{media_type.value}/{imdb_id}.json"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return None

            meta = resp.json().get("meta", {})
            return MediaItem(
                id=meta.get("id", imdb_id),
                imdb_id=meta.get("imdb_id") or imdb_id,
                title=meta.get("name", "Unknown"),
                media_type=media_type,
                year=meta.get("releaseInfo") or meta.get("year"),
                overview=meta.get("description"),
                poster=meta.get("poster"),
                rating=meta.get("imdbRating"),
            )

