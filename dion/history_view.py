from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import typer
from .console import console
from .metadata.models import MediaType
from .storage.history import HistoryManager
from .ui import format_playback_progress, format_relative_time, prompt_select

logger = logging.getLogger(__name__)


def format_play_choice(play: Dict[str, Any], play_number: Optional[int] = None) -> str:
    """Format a single watch history play into a clean, informative menu entry."""
    is_series = play.get("media_type") == "series" or ("season" in play and play.get("season") is not None)
    badge = "📺" if is_series else "🎬"
    title = play.get("title", "Unknown")
    year = play.get("year")

    parts: List[str] = []

    # Title & Year
    title_str = f"{badge}  \033[1m{title}\033[0m"
    if year:
        title_str += f" ({year})"
    parts.append(title_str)

    # Episode details if series
    if is_series and play.get("season") is not None and play.get("episode") is not None:
        s = play["season"]
        e = play["episode"]
        ep_code = f"S{s:02d}E{e:02d}"
        ep_title = play.get("episode_title")
        ep_str = f"{ep_code}: {ep_title}" if ep_title else ep_code
        parts.append(f"\033[36m{ep_str}\033[0m")

    # Rewatch indicator
    if play_number and play_number > 1:
        parts.append(f"\033[35m(Rewatch #{play_number})\033[0m")

    # Watched timestamp
    watched_at = play.get("watched_at") or play.get("last_watched")
    if watched_at:
        try:
            dt = datetime.fromisoformat(str(watched_at).replace("Z", "+00:00"))
            date_display = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_display = str(watched_at)[:16]
        rel = format_relative_time(watched_at)
        time_tag = f"{date_display} ({rel})" if rel else date_display
        parts.append(f"\033[2m{time_tag}\033[0m")

    return "  ·  ".join(parts)


def select_from_history(history_mgr: HistoryManager) -> Optional[Dict[str, Any]]:
    """Display dedicated watch history view with pagination, date breakdown, and single-play removal."""
    page_size = 15
    current_page = 1

    while True:
        console.clear()
        offset = (current_page - 1) * page_size
        plays, total_count = history_mgr.list_plays(limit=page_size, offset=offset)

        if not plays and current_page > 1:
            current_page -= 1
            continue
        if not plays and current_page == 1:
            console.print("[yellow]No watch history found.[/yellow]")
            return None

        total_pages = max(1, (total_count + page_size - 1) // page_size)

        # Build choices
        choices: List[Tuple[str, Any]] = []
        for play in plays:
            total_for_item = history_mgr.get_play_count(
                play["imdb_id"],
                season=play.get("season"),
                episode=play.get("episode"),
            )
            label = format_play_choice(play, play_number=total_for_item if total_for_item > 1 else None)
            choices.append((label, play))

        if current_page < total_pages:
            choices.append((f"\033[33m⏩ Next Page (Page {current_page + 1} of {total_pages}) ->\033[0m", "__next_page__"))
        if current_page > 1:
            choices.append((f"\033[33m⏪ <- Previous Page (Page {current_page - 1} of {total_pages})\033[0m", "__prev_page__"))
        choices.append(("\033[2m↩ Exit History\033[0m", "__exit__"))

        prompt_title = f"📜 Watch History (Page {current_page}/{total_pages} · {total_count} total plays)"

        def on_watch(item: Any) -> None:
            if not isinstance(item, dict) or "id" not in item:
                return
            ep_info = ""
            if item.get("season") is not None and item.get("episode") is not None:
                ep_info = f" S{item['season']:02d}E{item['episode']:02d}"
            date_str = str(item.get("watched_at", ""))[:10]
            confirm = typer.confirm(
                f"\nRemove this single play from history: '{item.get('title')}{ep_info}' ({date_str})?\n"
                "(Any remaining rewatches will keep the item marked as watched)",
                default=False,
            )
            if confirm:
                history_mgr.delete_play(item["id"])
                console.print("[bold green]✓ Play removed from history.[/bold green]")

        def build_choices() -> List[Tuple[str, Any]]:
            p_list, t_count = history_mgr.list_plays(limit=page_size, offset=offset)
            t_pages = max(1, (t_count + page_size - 1) // page_size)
            chs: List[Tuple[str, Any]] = []
            for p in p_list:
                tot = history_mgr.get_play_count(
                    p["imdb_id"],
                    season=p.get("season"),
                    episode=p.get("episode"),
                )
                lbl = format_play_choice(p, play_number=tot if tot > 1 else None)
                chs.append((lbl, p))
            if current_page < t_pages:
                chs.append((f"\033[33m⏩ Next Page (Page {current_page + 1} of {t_pages}) ->\033[0m", "__next_page__"))
            if current_page > 1:
                chs.append((f"\033[33m⏪ <- Previous Page (Page {current_page - 1} of {t_pages})\033[0m", "__prev_page__"))
            chs.append(("\033[2m↩ Exit History\033[0m", "__exit__"))
            return chs

        selected = prompt_select(
            prompt_title,
            choices,
            on_watch=on_watch,
            refresh_choices=build_choices,
        )

        if selected is None or selected == "__exit__":
            return None
        elif selected == "__next_page__":
            current_page += 1
            continue
        elif selected == "__prev_page__":
            current_page = max(1, current_page - 1)
            continue
        elif isinstance(selected, dict):
            return selected
