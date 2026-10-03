from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..metadata.models import StreamSource


class BaseProvider(ABC):
    """Abstract base class for streaming media providers."""

    name: str = "base"

    @abstractmethod
    def resolve_movie(self, imdb_id: str, title: str) -> List[StreamSource]:
        pass

    @abstractmethod
    def resolve_episode(
        self, imdb_id: str, season: int, episode: int, title: str
    ) -> List[StreamSource]:
        pass
