import re
from typing import List, Optional, Set
import httpx
from rich.console import Console

from ..metadata.models import StreamSource, SubtitleTrack
from ..storage.settings import SettingsManager
from .base import BaseProvider
from .subtitles import SubtitleResolver
from .vidlink import VidLinkProvider
from .vidora import VidoraProvider
from .vixsrc import VixSrcProvider

console = Console()


class ProviderManager:
    """Orchestrates resolution across multiple streaming providers and subtitles."""

    def __init__(self, settings_mgr: Optional[SettingsManager] = None):
        self.settings_mgr = settings_mgr or SettingsManager()
        self.providers: List[BaseProvider] = [
            VixSrcProvider(),
            VidoraProvider(),
            VidLinkProvider(),
        ]
        self.subtitle_resolver = SubtitleResolver(settings_mgr=self.settings_mgr)

    def resolve_movie(
        self,
        imdb_id: str,
        title: str,
        year: Optional[str] = None,
        preferred_langs: Optional[List[str]] = None,
    ) -> List[StreamSource]:
        """Resolve all working stream sources for a movie."""
        effective_langs = preferred_langs
        if effective_langs is None:
            user_lang = self.settings_mgr.sub_lang or "en"
            effective_langs = [user_lang, "en", "eng", "English"]

        # Initiate parallel background subtitle prefetching immediately
        if preferred_langs is not None:
            self.subtitle_resolver.prefetch_subtitles(
                imdb_id=imdb_id, released=year, preferred_langs=preferred_langs
            )
        else:
            self.subtitle_resolver.prefetch_subtitles(imdb_id=imdb_id, released=year)

        sources: List[StreamSource] = []

        for provider in self.providers:
            try:
                found = provider.resolve_movie(imdb_id, title)
                sources.extend(found)
                if sources:
                    break
            except Exception:
                continue

        # Fetch extra subtitles if available and needed
        if sources:
            self._attach_subtitles(
                sources, imdb_id, released=year, preferred_langs=effective_langs
            )

        return sources

    def resolve_episode(
        self,
        imdb_id: str,
        season: int,
        episode: int,
        title: str,
        released: Optional[str] = None,
        preferred_langs: Optional[List[str]] = None,
    ) -> List[StreamSource]:
        """Resolve all working stream sources for a TV series episode."""
        effective_langs = preferred_langs
        if effective_langs is None:
            user_lang = self.settings_mgr.sub_lang or "en"
            effective_langs = [user_lang, "en", "eng", "English"]

        # Initiate parallel background subtitle prefetching immediately
        if preferred_langs is not None:
            self.subtitle_resolver.prefetch_subtitles(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                released=released,
                preferred_langs=preferred_langs,
            )
        else:
            self.subtitle_resolver.prefetch_subtitles(
                imdb_id=imdb_id, season=season, episode=episode, released=released
            )

        sources: List[StreamSource] = []

        for provider in self.providers:
            try:
                found = provider.resolve_episode(imdb_id, season, episode, title)
                sources.extend(found)
                if sources:
                    break
            except Exception:
                continue

        # Fetch extra subtitles if available and needed
        if sources:
            self._attach_subtitles(
                sources,
                imdb_id,
                season=season,
                episode=episode,
                released=released,
                preferred_langs=preferred_langs,
            )

        return sources

    def _has_hls_embedded_subtitles(
        self, source: StreamSource, pref_set: Set[str]
    ) -> bool:
        """Inspect HLS master playlist for embedded subtitle tracks matching the preferred language."""
        try:
            headers = dict(source.headers)
            resp = httpx.get(
                source.url,
                headers=headers,
                timeout=2.0,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return False
            sub_lines = re.findall(
                r'#EXT-X-MEDIA:TYPE=SUBTITLES[^\r\n]*', resp.text, re.IGNORECASE
            )
            for line in sub_lines:
                lang_m = re.search(r'LANGUAGE="([^"]+)"', line, re.IGNORECASE)
                name_m = re.search(r'NAME="([^"]+)"', line, re.IGNORECASE)
                lang_id = lang_m.group(1) if lang_m else ""
                name = name_m.group(1) if name_m else ""
                if self.subtitle_resolver._matches_lang(name, lang_id, pref_set):
                    return True
        except Exception:
            pass
        return False

    def _has_native_subtitles(
        self, source: StreamSource, pref_set: Set[str]
    ) -> bool:
        """
        Determine if the stream source already provides native subtitles
        matching the user's preferred language (either via embedded HLS
        renditions or provider-attached caption tracks).
        """
        # 1. Check existing source subtitle tracks (e.g. from VidLink or Vidora)
        for track in source.subtitles:
            if self.subtitle_resolver._matches_lang(track.label, track.lang, pref_set):
                return True

        # 2. Check embedded HLS subtitle renditions (e.g. from VixSrc)
        if source.is_hls or ".m3u8" in source.url:
            if self._has_hls_embedded_subtitles(source, pref_set):
                return True

        return False

    def _prioritize_subtitles(
        self, subtitles: List[SubtitleTrack], pref_set: Set[str]
    ) -> List[SubtitleTrack]:
        """Place tracks matching the preferred language at the front."""
        matching = [
            s
            for s in subtitles
            if self.subtitle_resolver._matches_lang(s.label, s.lang, pref_set)
        ]
        non_matching = [s for s in subtitles if s not in matching]
        return matching + non_matching

    def _attach_subtitles(
        self,
        sources: List[StreamSource],
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        released: Optional[str] = None,
        preferred_langs: Optional[List[str]] = None,
    ) -> None:
        """
        Attach subtitles to stream sources only if they lack native subtitles
        matching the user's preferred language.
        """
        if preferred_langs is None:
            user_lang = self.settings_mgr.sub_lang or "en"
            preferred_langs = [user_lang, "en", "eng", "English"]
        pref_set = {p.lower() for p in preferred_langs if p}

        # Check which sources already have native subtitles matching user preference
        sources_needing_subs: List[StreamSource] = []
        for src in sources:
            if self._has_native_subtitles(src, pref_set):
                # Native subtitles exist in the stream!
                # Prioritize any existing attached tracks to ensure preferred language is first
                if src.subtitles:
                    src.subtitles = self._prioritize_subtitles(src.subtitles, pref_set)
            else:
                sources_needing_subs.append(src)

        if not sources_needing_subs:
            return

        try:
            extra_subs = self.subtitle_resolver.get_subtitles(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                released=released,
                preferred_langs=preferred_langs,
            )
            if extra_subs:
                for src in sources_needing_subs:
                    existing_urls = {s.url for s in extra_subs}
                    src.subtitles = extra_subs + [
                        s for s in src.subtitles if s.url not in existing_urls
                    ]
        except Exception:
            pass


