from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class MediaType(str, Enum):
    MOVIE = "movie"
    SERIES = "series"


@dataclass
class MediaItem:
    id: str
    imdb_id: str
    title: str
    media_type: MediaType
    year: Optional[str] = None
    overview: Optional[str] = None
    poster: Optional[str] = None
    rating: Optional[str] = None
    runtime: Optional[str] = None

    @property
    def display_title(self) -> str:
        parts = [self.title]
        if self.year:
            parts.append(f"({self.year})")
        type_str = "TV" if self.media_type == MediaType.SERIES else "Movie"
        parts.append(f"[{type_str}]")
        return " ".join(parts)


@dataclass
class EpisodeItem:
    id: str
    season: int
    episode: int
    title: str
    overview: Optional[str] = None
    released: Optional[str] = None
    runtime: Optional[str] = None

    @property
    def display_name(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d} - {self.title}"


@dataclass
class SubtitleTrack:
    url: str
    label: str
    lang: str = "en"


@dataclass
class StreamSource:
    url: str
    server: str
    is_hls: bool = True
    quality: str = "auto"
    headers: Dict[str, str] = field(default_factory=dict)
    subtitles: List[SubtitleTrack] = field(default_factory=list)

