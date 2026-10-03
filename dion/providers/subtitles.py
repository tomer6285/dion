from __future__ import annotations

from datetime import datetime
from pathlib import Path
from datetime import datetime
from pathlib import Path
import re
from typing import List, Optional
import httpx

from ..metadata.models import SubtitleTrack


class SubtitleResolver:
    """Fetches subtitle tracks from OpenSubtitles REST API."""

    BASE_URL = "https://rest.opensubtitles.org/search"
    HEADERS = {
        "User-Agent": "VLSub 0.10.2",
        "X-User-Agent": "VLSub 0.10.2",
    }

    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None

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
        if preferred_langs is None:
            preferred_langs = ["English", "en", "eng"]
        pref_set = {p.lower() for p in preferred_langs}

        match = re.search(r"\d+", imdb_id)
        if not match:
            return []
        imdb_num = match.group(0)

        if season is not None and episode is not None:
            url = f"{self.BASE_URL}/episode-{episode}/imdbid-{imdb_num}/season-{season}"
        else:
            url = f"{self.BASE_URL}/imdbid-{imdb_num}"

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

        def score_sub(item: dict) -> int:
            if str(item.get("SubBad", "0")) != "0":
                return -9999

            rel = (item.get("MovieReleaseName") or "").upper()
            fn = (item.get("SubFileName") or "").upper()
            comb = f"{rel} {fn}"
            score = 0

            # 1. Compare upload date vs official release date
            sub_date_str = item.get("SubAddDate", "")
            if sub_date_str and air_date:
                try:
                    sub_date = datetime.fromisoformat(sub_date_str)
                    # Uploaded more than 2 days before broadcast -> leaked pre-air screener
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

            # 3. Boost high-quality matching release types (Vidora streams WEB-DL/1080p)
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
            for grp in scene_groups:
                if grp in comb:
                    score += 50

            # 5. Community rating
            try:
                score += int(float(item.get("SubRating", 0)) * 10)
            except Exception:
                pass

            # 6. Popularity
            try:
                score += min(50, int(item.get("SubDownloadsCnt", 0)) // 10000)
            except Exception:
                pass

            return score

        try:
            resp = self.client.get(url)
            if resp.status_code != 200:
                return []

            data = resp.json()
            if not isinstance(data, list):
                return []

            candidates = []
            for item in data:
                raw_link = item.get("SubDownloadLink")
                if not raw_link:
                    continue
                lang = item.get("LanguageName", "Unknown")
                sub_lang_id = item.get("SubLanguageID", "en")

                if pref_set and (
                    lang.lower() not in pref_set and sub_lang_id.lower() not in pref_set
                ):
                    continue

                candidates.append(item)

            candidates.sort(key=score_sub, reverse=True)

            subtitles: List[SubtitleTrack] = []
            seen_ids = set()
            for item in candidates:
                if score_sub(item) < -500 and subtitles:
                    # Don't add known pre-air leaks if we already have valid tracks
                    continue

                sub_id = item.get("IDSubtitleFile")
                if sub_id in seen_ids:
                    continue
                seen_ids.add(sub_id)

                raw_link = item.get("SubDownloadLink", "")
                clean_url = raw_link.replace(".gz", "").replace(
                    "download/", "download/subencoding-utf8/"
                )
                lang = item.get("LanguageName", "English")
                sub_lang_id = item.get("SubLanguageID", "en")
                rel_name = item.get("MovieReleaseName") or ""

                tag = "WEB-DL" if "WEB" in rel_name.upper() else ("HDTV" if "HDTV" in rel_name.upper() else "")
                label = f"{lang} ({tag})" if tag else lang

                # Cache subtitle locally for instant zero-latency loading and clean display names in MPV
                cache_dir = Path.home() / ".cache" / "dion" / "subtitles"
                cache_dir.mkdir(parents=True, exist_ok=True)
                clean_tag = tag.replace("-", "_") if tag else "default"
                local_path = cache_dir / f"{imdb_id}_{sub_id}_{lang}_{clean_tag}.srt"
                old_path = Path.home() / ".cache" / "dionysus" / "subtitles" / f"{imdb_id}_{sub_id}_{lang}_{clean_tag}.srt"
                if not local_path.exists() and old_path.exists():
                    import shutil
                    try:
                        shutil.copy2(old_path, local_path)
                    except Exception:
                        pass

                track_url = clean_url
                if local_path.exists() and local_path.stat().st_size > 100:
                    track_url = str(local_path)
                else:
                    try:
                        dl_resp = self.client.get(clean_url, timeout=3.0)
                        if dl_resp.status_code == 200 and "-->" in dl_resp.text:
                            local_path.write_text(dl_resp.text, encoding="utf-8")
                            track_url = str(local_path)
                    except Exception:
                        track_url = clean_url

                subtitles.append(
                    SubtitleTrack(
                        url=track_url,
                        label=label,
                        lang=sub_lang_id,
                    )
                )
                if len(subtitles) >= max_subtitles:
                    break

            return subtitles
        except Exception:
            return []

