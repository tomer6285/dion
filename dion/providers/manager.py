from __future__ import annotations

from typing import List, Optional

from rich.console import Console

from ..metadata.models import StreamSource
from .base import BaseProvider
from .subtitles import SubtitleResolver
from .vidora import VidoraProvider
from .vixsrc import VixSrcProvider

console = Console()


class ProviderManager:
    """Orchestrates resolution across multiple streaming providers and subtitles."""

    def __init__(self):
        self.providers: List[BaseProvider] = [
            VixSrcProvider(),
            VidoraProvider(),
        ]
        self.subtitle_resolver = SubtitleResolver()

    def resolve_movie(
        self, imdb_id: str, title: str, year: Optional[str] = None
    ) -> List[StreamSource]:
        """Resolve all working stream sources for a movie."""
        sources: List[StreamSource] = []

        for provider in self.providers:
            try:
                found = provider.resolve_movie(imdb_id, title)
                sources.extend(found)
                if sources:
                    break
            except Exception:
                continue

        # Fetch extra subtitles if available
        if sources:
            self._attach_subtitles(sources, imdb_id, released=year)

        return sources

    def resolve_episode(
        self,
        imdb_id: str,
        season: int,
        episode: int,
        title: str,
        released: Optional[str] = None,
    ) -> List[StreamSource]:
        """Resolve all working stream sources for a TV series episode."""
        sources: List[StreamSource] = []

        for provider in self.providers:
            try:
                found = provider.resolve_episode(imdb_id, season, episode, title)
                sources.extend(found)
                if sources:
                    break
            except Exception:
                continue

        # Fetch extra subtitles if available
        if sources:
            self._attach_subtitles(
                sources, imdb_id, season=season, episode=episode, released=released
            )

        return sources

    def _attach_subtitles(
        self,
        sources: List[StreamSource],
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        released: Optional[str] = None,
    ) -> None:
        try:
            extra_subs = self.subtitle_resolver.get_subtitles(
                imdb_id=imdb_id, season=season, episode=episode, released=released
            )
            for src in sources:
                existing_urls = {s.url for s in extra_subs}
                src.subtitles = extra_subs + [s for s in src.subtitles if s.url not in existing_urls]
        except Exception:
            pass

