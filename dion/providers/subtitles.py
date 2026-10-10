from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from datetime import datetime
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Set
import zipfile

import httpx

from ..metadata.models import SubtitleTrack
from ..storage.settings import SettingsManager


@dataclass
class SubtitleCandidate:
    provider: str
    sub_id: str
    url: str
    lang: str
    lang_id: str
    release_name: str
    file_name: str
    release_format: str = ""
    release_group: str = ""
    downloads: int = 0
    rating: float = 0.0
    sub_bad: bool = False
    sub_date: str = ""


class SubtitleResolver:
    """Fetches, ranks, and prefetches subtitle tracks using parallel multi-API fallback."""

    BASE_URL = "https://rest.opensubtitles.org/search"
    HEADERS = {
        "User-Agent": "VLSub 0.10.2",
        "X-User-Agent": "VLSub 0.10.2",
    }
    STREMIO_OPENSUBTITLES_URL = "https://opensubtitles-v3.strem.io/subtitles"
    WYZIE_BASE_URL = "https://sub.wyzie.io/search"
    SUBDL_BASE_URL = "https://api.subdl.com/api/v1/subtitles"

    def __init__(
        self,
        timeout: float = 4.0,
        settings_mgr: Optional[SettingsManager] = None,
    ):
        self.timeout = timeout
        self._settings_mgr = settings_mgr
        self._client: Optional[httpx.Client] = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="dion-subs"
        )
        self._inflight: Dict[str, concurrent.futures.Future[List[SubtitleTrack]]] = {}
        self._lock = threading.Lock()

    @property
    def settings(self) -> SettingsManager:
        if self._settings_mgr is None:
            self._settings_mgr = SettingsManager()
        return self._settings_mgr

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers=self.HEADERS,
            )
        return self._client

    def close(self) -> None:
        if self._client and not self._client.is_closed:
            self._client.close()
        self._executor.shutdown(wait=False)

    def _make_cache_key(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        preferred_langs: Optional[List[str]] = None,
    ) -> str:
        s_part = f"s{season}" if season is not None else ""
        e_part = f"e{episode}" if episode is not None else ""
        langs_part = ",".join(sorted(preferred_langs or ["en"]))
        return f"{imdb_id}:{s_part}:{e_part}:{langs_part}"

    def prefetch_subtitles(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        preferred_langs: Optional[List[str]] = None,
        released: Optional[str] = None,
        max_subtitles: int = 3,
    ) -> concurrent.futures.Future[List[SubtitleTrack]]:
        """Asynchronously prefetch subtitle tracks in the background while stream sources are resolving."""
        key = self._make_cache_key(imdb_id, season, episode, preferred_langs)
        with self._lock:
            existing = self._inflight.get(key)
            if existing and not existing.cancelled():
                return existing

            future = self._executor.submit(
                self._resolve_subtitles,
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                preferred_langs=preferred_langs,
                released=released,
                max_subtitles=max_subtitles,
            )
            self._inflight[key] = future
            return future

    def get_subtitles(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        preferred_langs: Optional[List[str]] = None,
        released: Optional[str] = None,
        max_subtitles: int = 3,
    ) -> List[SubtitleTrack]:
        """Fetch and rank subtitle tracks for a movie or TV episode using release timing and quality heuristics."""
        key = self._make_cache_key(imdb_id, season, episode, preferred_langs)
        with self._lock:
            future = self._inflight.get(key)

        if future is None:
            future = self.prefetch_subtitles(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                preferred_langs=preferred_langs,
                released=released,
                max_subtitles=max_subtitles,
            )

        try:
            return future.result(timeout=self.timeout)
        except Exception:
            # Fallback to direct resolution if future failed or timed out
            try:
                return self._resolve_subtitles(
                    imdb_id=imdb_id,
                    season=season,
                    episode=episode,
                    preferred_langs=preferred_langs,
                    released=released,
                    max_subtitles=max_subtitles,
                )
            except Exception:
                return []

    def _resolve_subtitles(
        self,
        imdb_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        preferred_langs: Optional[List[str]] = None,
        released: Optional[str] = None,
        max_subtitles: int = 3,
    ) -> List[SubtitleTrack]:
        """Core pipeline: Multi-API discovery, heuristic scoring, and parallel downloading/caching."""
        if preferred_langs is None:
            preferred_langs = ["English", "en", "eng"]
        pref_set = {p.lower() for p in preferred_langs}

        match = re.search(r"\d+", imdb_id)
        if not match:
            return []
        imdb_num = match.group(0)

        air_date: Optional[datetime] = None
        if released:
            try:
                clean_rel = released.replace("Z", "+00:00")
                if len(clean_rel) == 4 and clean_rel.isdigit():
                    air_date = datetime(int(clean_rel), 1, 1)
                else:
                    air_date = datetime.fromisoformat(clean_rel).replace(tzinfo=None)
            except Exception:
                air_date = None

        # Gather candidates concurrently from multiple providers
        provider_tasks = [
            (
                "opensubtitles_v3",
                lambda: self._fetch_opensubtitles_v3(
                    imdb_id, imdb_num, season, episode, pref_set
                ),
            ),
            (
                "wyzie",
                lambda: self._fetch_wyzie(imdb_id, season, episode, pref_set),
            ),
            (
                "subdl",
                lambda: self._fetch_subdl(imdb_id, season, episode, pref_set),
            ),
        ]

        candidates: List[SubtitleCandidate] = []
        future_map = {
            self._executor.submit(task): name for name, task in provider_tasks
        }

        for f in concurrent.futures.as_completed(future_map, timeout=self.timeout):
            try:
                found = f.result()
                if found:
                    candidates.extend(found)
            except Exception:
                continue

        # If OpenSubtitles v3 and others returned nothing, fallback to OpenSubtitles legacy REST
        if not candidates:
            try:
                legacy_candidates = self._fetch_opensubtitles_legacy(
                    imdb_num, season, episode, pref_set
                )
                candidates.extend(legacy_candidates)
            except Exception:
                pass

        if not candidates:
            return []

        # Scoring heuristics
        def score_sub(cand: SubtitleCandidate) -> int:
            if cand.sub_bad:
                return -9999

            rel = cand.release_name.upper()
            fn = cand.file_name.upper()
            fmt = cand.release_format.upper()
            grp = cand.release_group.upper()
            comb = f"{rel} {fn} {fmt} {grp}"
            score = 0

            # 1. Compare upload date vs official release date
            if cand.sub_date and air_date:
                try:
                    sub_date = datetime.fromisoformat(cand.sub_date)
                    if (air_date - sub_date).days > 2:
                        score -= 1000
                    elif sub_date >= air_date:
                        score += 150
                except Exception:
                    pass

            # 2. Penalize pre-air leaks, screeners, and cam rips
            bad_tags = [
                "PREAIR", "PRE-AIR", "WORKPRINT", "SCREENER", "SCR",
                "NOGRP", "CAM", "TELESYNC", "TS", "HDCAM", "HDTS",
            ]
            for tag in bad_tags:
                if tag in comb:
                    score -= 600

            # 3. Boost high-quality matching release types (Vidora/Vixsrc streams WEB-DL/1080p)
            if "1080P" in comb:
                score += 100
            if "WEB-DL" in comb or "WEBDL" in comb:
                score += 150
            elif "HDTV" in comb:
                score += 80
            elif "BLURAY" in comb or "BDRIP" in comb or "BRRIP" in comb:
                score += 100

            # 4. Scene group bonus
            scene_groups = [
                "NTB", "LOL", "DIMENSION", "YFN", "FLUX", "AMZN",
                "BATV", "DEFLATE", "KILLERS", "AVS", "PSA",
            ]
            for sg in scene_groups:
                if sg in comb:
                    score += 50

            # 5. Community rating
            try:
                score += int(cand.rating * 10)
            except Exception:
                pass

            # 6. Popularity
            try:
                score += min(50, cand.downloads // 10000)
            except Exception:
                pass

            return score

        candidates.sort(key=score_sub, reverse=True)

        # Filter out bad candidates & deduplicate
        selected: List[SubtitleCandidate] = []
        seen_urls: Set[str] = set()
        seen_ids: Set[str] = set()

        for cand in candidates:
            if score_sub(cand) < -500 and selected:
                continue
            if cand.url in seen_urls or cand.sub_id in seen_ids:
                continue
            seen_urls.add(cand.url)
            seen_ids.add(cand.sub_id)
            selected.append(cand)
            if len(selected) >= max_subtitles:
                break

        if not selected:
            return []

        # Download and cache selected tracks in parallel
        return self._download_and_cache_parallel(selected, imdb_id)

    def _matches_lang(self, lang_name: str, lang_id: str, pref_set: Set[str]) -> bool:
        if not pref_set:
            return True
        name_l = (lang_name or "").lower()
        id_l = (lang_id or "").lower()
        for pref in pref_set:
            p = pref.lower()
            if p == name_l or p == id_l:
                return True
            if len(p) >= 2 and (id_l.startswith(p) or name_l.startswith(p)):
                return True
            if len(id_l) >= 2 and p.startswith(id_l):
                return True
        return False

    def _fetch_opensubtitles_v3(
        self,
        imdb_id: str,
        imdb_num: str,
        season: Optional[int],
        episode: Optional[int],
        pref_set: Set[str],
    ) -> List[SubtitleCandidate]:
        """Fetch subtitles from OpenSubtitles v3 (Stremio edge endpoint)."""
        if season is not None and episode is not None:
            url = f"{self.STREMIO_OPENSUBTITLES_URL}/series/{imdb_id}:{season}:{episode}.json"
        else:
            url = f"{self.STREMIO_OPENSUBTITLES_URL}/movie/{imdb_id}.json"

        try:
            resp = self.client.get(url, timeout=2.5)
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data.get("subtitles", [])
            if not isinstance(items, list):
                return []

            results = []
            for item in items:
                raw_url = item.get("url")
                if not raw_url:
                    continue
                lang_code = item.get("lang", "en")
                if not self._matches_lang(lang_code, lang_code, pref_set):
                    continue

                sub_id = str(item.get("id", ""))
                rel_name = item.get("movieReleaseName", "") or ""
                fn = item.get("subtitleFileName", "") or ""
                grp = item.get("releaseGroup", "") or ""
                fmt = item.get("releaseFormat", "") or ""

                results.append(
                    SubtitleCandidate(
                        provider="os_v3",
                        sub_id=f"os3_{sub_id}",
                        url=raw_url,
                        lang=lang_code,
                        lang_id=lang_code,
                        release_name=rel_name,
                        file_name=fn,
                        release_group=grp,
                        release_format=fmt,
                    )
                )
            return results
        except Exception:
            return []

    def _fetch_opensubtitles_legacy(
        self,
        imdb_num: str,
        season: Optional[int],
        episode: Optional[int],
        pref_set: Set[str],
    ) -> List[SubtitleCandidate]:
        """Fetch subtitles from OpenSubtitles legacy REST API."""
        if season is not None and episode is not None:
            url = f"{self.BASE_URL}/episode-{episode}/imdbid-{imdb_num}/season-{season}"
        else:
            url = f"{self.BASE_URL}/imdbid-{imdb_num}"

        try:
            resp = self.client.get(url, timeout=2.5)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list):
                return []

            results = []
            for item in data:
                raw_link = item.get("SubDownloadLink")
                if not raw_link:
                    continue
                lang = item.get("LanguageName", "Unknown")
                sub_lang_id = item.get("SubLanguageID", "en")
                if not self._matches_lang(lang, sub_lang_id, pref_set):
                    continue

                clean_url = raw_link.replace(".gz", "").replace(
                    "download/", "download/subencoding-utf8/"
                )
                sub_id = str(item.get("IDSubtitleFile", ""))
                rel = item.get("MovieReleaseName", "") or ""
                fn = item.get("SubFileName", "") or ""
                sub_bad = str(item.get("SubBad", "0")) != "0"
                sub_date = item.get("SubAddDate", "") or ""

                try:
                    rating = float(item.get("SubRating", 0))
                except Exception:
                    rating = 0.0

                try:
                    downloads = int(item.get("SubDownloadsCnt", 0))
                except Exception:
                    downloads = 0

                results.append(
                    SubtitleCandidate(
                        provider="os_legacy",
                        sub_id=f"os_{sub_id}",
                        url=clean_url,
                        lang=lang,
                        lang_id=sub_lang_id,
                        release_name=rel,
                        file_name=fn,
                        rating=rating,
                        downloads=downloads,
                        sub_bad=sub_bad,
                        sub_date=sub_date,
                    )
                )
            return results
        except Exception:
            return []

    def _fetch_wyzie(
        self,
        imdb_id: str,
        season: Optional[int],
        episode: Optional[int],
        pref_set: Set[str],
    ) -> List[SubtitleCandidate]:
        """Fetch subtitles from Wyzie Subs API."""
        params: Dict[str, Any] = {"id": imdb_id}
        if season is not None and episode is not None:
            params["season"] = season
            params["episode"] = episode

        wyzie_key = self.settings.wyzie_api_key
        if wyzie_key:
            params["key"] = wyzie_key

        try:
            resp = self.client.get(self.WYZIE_BASE_URL, params=params, timeout=2.5)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list):
                return []

            results = []
            for item in data:
                raw_url = item.get("url")
                if not raw_url:
                    continue
                display_lang = item.get("display", "English")
                lang_code = item.get("language", "en")
                if not self._matches_lang(display_lang, lang_code, pref_set):
                    continue

                fmt = item.get("format", "srt")
                source_grp = item.get("source", "")
                media_title = item.get("media", "")
                sub_hash = hashlib.md5(raw_url.encode()).hexdigest()[:8]

                results.append(
                    SubtitleCandidate(
                        provider="wyzie",
                        sub_id=f"wy_{sub_hash}",
                        url=raw_url,
                        lang=display_lang,
                        lang_id=lang_code,
                        release_name=source_grp,
                        file_name=f"{media_title}.{fmt}",
                        release_format=fmt,
                        release_group=source_grp,
                    )
                )
            return results
        except Exception:
            return []

    def _fetch_subdl(
        self,
        imdb_id: str,
        season: Optional[int],
        episode: Optional[int],
        pref_set: Set[str],
    ) -> List[SubtitleCandidate]:
        """Fetch subtitles from SubDL API."""
        params: Dict[str, Any] = {
            "imdb_id": imdb_id,
            "unpack": 1,
        }
        if season is not None and episode is not None:
            params["type"] = "tv"
            params["season_number"] = season
            params["episode_number"] = episode
        else:
            params["type"] = "movie"

        subdl_key = self.settings.subdl_api_key
        if subdl_key:
            params["api_key"] = subdl_key

        try:
            resp = self.client.get(self.SUBDL_BASE_URL, params=params, timeout=2.5)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, dict) or not data.get("status"):
                return []

            sub_items = data.get("subtitles", [])
            if not isinstance(sub_items, list):
                return []

            results = []
            for sub in sub_items:
                rel_name = sub.get("release_name", "") or ""
                unpack_files = sub.get("unpack_files", [])

                if isinstance(unpack_files, list) and unpack_files:
                    for uf in unpack_files:
                        file_url = uf.get("url", "")
                        if not file_url:
                            continue
                        if file_url.startswith("/"):
                            file_url = f"https://dl.subdl.com{file_url}"

                        lang = uf.get("language", "English")
                        if not self._matches_lang(lang, "en", pref_set):
                            continue

                        fn = uf.get("name", "")
                        sub_hash = hashlib.md5(file_url.encode()).hexdigest()[:8]
                        results.append(
                            SubtitleCandidate(
                                provider="subdl",
                                sub_id=f"sd_{sub_hash}",
                                url=file_url,
                                lang=lang,
                                lang_id="en",
                                release_name=rel_name,
                                file_name=fn,
                            )
                        )
                else:
                    raw_url = sub.get("url", "")
                    if not raw_url:
                        continue
                    if raw_url.startswith("/"):
                        raw_url = f"https://dl.subdl.com{raw_url}"
                    sub_hash = hashlib.md5(raw_url.encode()).hexdigest()[:8]
                    results.append(
                        SubtitleCandidate(
                            provider="subdl",
                            sub_id=f"sd_{sub_hash}",
                            url=raw_url,
                            lang="English",
                            lang_id="en",
                            release_name=rel_name,
                            file_name="",
                        )
                    )
            return results
        except Exception:
            return []

    def _extract_subtitle_text(self, content_bytes: bytes) -> Optional[str]:
        """Extract subtitle text, handling gzip, zip, or raw UTF-8 / Latin-1 encoding."""
        # 1. Gzip compressed
        if content_bytes.startswith(b"\x1f\x8b"):
            try:
                decompressed = gzip.decompress(content_bytes)
                text = decompressed.decode("utf-8", errors="replace")
                if "-->" in text or "WEBVTT" in text:
                    return text
            except Exception:
                pass

        # 2. Zip compressed
        if content_bytes.startswith(b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                    for name in zf.namelist():
                        if name.lower().endswith((".srt", ".vtt")):
                            text = zf.read(name).decode("utf-8", errors="replace")
                            if "-->" in text or "WEBVTT" in text:
                                return text
            except Exception:
                pass

        # 3. Direct UTF-8
        try:
            text = content_bytes.decode("utf-8")
            if "-->" in text or "WEBVTT" in text:
                return text
        except Exception:
            pass

        # 4. Latin-1 fallback
        try:
            text = content_bytes.decode("latin-1")
            if "-->" in text or "WEBVTT" in text:
                return text
        except Exception:
            pass

        return None

    def _download_single_candidate(
        self,
        candidate: SubtitleCandidate,
        imdb_id: str,
        cache_dir: Path,
    ) -> SubtitleTrack:
        """Download and cache a single subtitle candidate, returning a SubtitleTrack."""
        rel_upper = candidate.release_name.upper()
        tag = (
            "WEB-DL"
            if "WEB" in rel_upper
            else ("HDTV" if "HDTV" in rel_upper else "")
        )
        clean_tag = tag.replace("-", "_") if tag else "default"
        label = f"{candidate.lang} ({tag})" if tag else candidate.lang

        clean_sub_id = re.sub(r"[^\w]", "_", candidate.sub_id)
        local_path = (
            cache_dir / f"{imdb_id}_{clean_sub_id}_{candidate.lang_id}_{clean_tag}.srt"
        )

        # Check existing cache
        if local_path.exists() and local_path.stat().st_size > 100:
            return SubtitleTrack(
                url=str(local_path),
                label=label,
                lang=candidate.lang_id,
            )

        # Legacy dionysus cache check
        old_path = (
            Path.home()
            / ".cache"
            / "dionysus"
            / "subtitles"
            / f"{imdb_id}_{clean_sub_id}_{candidate.lang_id}_{clean_tag}.srt"
        )
        if not local_path.exists() and old_path.exists():
            import shutil

            try:
                shutil.copy2(old_path, local_path)
                return SubtitleTrack(
                    url=str(local_path),
                    label=label,
                    lang=candidate.lang_id,
                )
            except Exception:
                pass

        track_url = candidate.url
        try:
            resp = self.client.get(candidate.url, timeout=3.0)
            if resp.status_code == 200:
                sub_text = self._extract_subtitle_text(resp.content)
                if sub_text:
                    local_path.write_text(sub_text, encoding="utf-8")
                    track_url = str(local_path)
        except Exception:
            track_url = candidate.url

        return SubtitleTrack(
            url=track_url,
            label=label,
            lang=candidate.lang_id,
        )

    def _download_and_cache_parallel(
        self,
        candidates: List[SubtitleCandidate],
        imdb_id: str,
    ) -> List[SubtitleTrack]:
        """Download multiple candidate subtitle files in parallel in the background."""
        cache_dir = Path.home() / ".cache" / "dion" / "subtitles"
        cache_dir.mkdir(parents=True, exist_ok=True)

        futures = [
            self._executor.submit(
                self._download_single_candidate, cand, imdb_id, cache_dir
            )
            for cand in candidates
        ]

        subtitles: List[SubtitleTrack] = []
        for f in futures:
            try:
                track = f.result(timeout=3.5)
                subtitles.append(track)
            except Exception:
                continue

        return subtitles
