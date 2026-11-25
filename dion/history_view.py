from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import typer
from .console import console
from .storage.history import HistoryManager
from .ui import format_relative_time, prompt_select

logger = logging.getLogger(__name__)

_TITLE_COL_WIDTH = 30
_DETAIL_COL_WIDTH = 34


def _pad_col(text: str, width: int) -> str:
    """Truncate text with an ellipsis if needed and pad with spaces to fixed width."""
    if len(text) > width:
        return text[: max(1, width - 1)] + "…"
    return text.ljust(width)


def _format_short_timestamp(watched_at: Optional[str]) -> str:
    """Format an ISO timestamp into a compact, readable label."""
    if not watched_at:
        return ""
    rel = format_relative_time(watched_at)
    try:
        dt = datetime.fromisoformat(str(watched_at).replace("Z", "+00:00"))
        if rel in ("Today", "Yesterday"):
            return f"{rel} {dt.strftime('%H:%M')}"
        if rel:
            return f"{dt.strftime('%b %d, %Y')} ({rel})"
        return dt.strftime("%b %d, %Y")
    except Exception:
        short = str(watched_at)[:16]
        return f"{short} ({rel})" if rel else short


def _is_series_play(play: Dict[str, Any]) -> bool:
    return play.get("media_type") == "series" or ("season" in play and play.get("season") is not None)


def _format_episode_range(plays: List[Dict[str, Any]]) -> str:
    """Summarize episode codes across a list of series plays (e.g. 'S01E01–S02E01')."""
    ep_coords: List[Tuple[int, int]] = []
    for p in plays:
        s = p.get("season")
        e = p.get("episode")
        if s is not None and e is not None:
            ep_coords.append((int(s), int(e)))
    if not ep_coords:
        return ""
    first_s, first_e = min(ep_coords)
    last_s, last_e = max(ep_coords)
    if (first_s, first_e) == (last_s, last_e):
        return f"S{first_s:02d}E{first_e:02d}"
    return f"S{first_s:02d}E{first_e:02d}–S{last_s:02d}E{last_e:02d}"


def format_play_choice(
    play: Dict[str, Any],
    play_number: Optional[int] = None,
    include_show: bool = True,
) -> str:
    """Format a single watch history play into an aligned, compact menu entry."""
    is_series = _is_series_play(play)
    badge = "📺" if is_series else "🎬"
    title = play.get("title", "Unknown")
    year = play.get("year")

    cols: List[str] = []

    if include_show:
        title_plain = f"{title} ({year})" if year else str(title)
        cols.append(f"{badge}  \033[1m{_pad_col(title_plain, _TITLE_COL_WIDTH)}\033[0m")

    if is_series and play.get("season") is not None and play.get("episode") is not None:
        s = int(play["season"])
        e = int(play["episode"])
        ep_code = f"S{s:02d}E{e:02d}"
        ep_title = play.get("episode_title")
        ep_plain = f"{ep_code}: {ep_title}" if ep_title else ep_code
        if play_number and play_number > 1:
            ep_plain = f"{ep_plain} (×{play_number})"
        cols.append(f"\033[36m{_pad_col(ep_plain, _DETAIL_COL_WIDTH)}\033[0m")
    else:
        detail_plain = f"Rewatch #{play_number}" if play_number and play_number > 1 else ""
        if include_show:
            cols.append(f"\033[35m{_pad_col(detail_plain, _DETAIL_COL_WIDTH)}\033[0m")
        elif detail_plain:
            cols.append(f"\033[35m{detail_plain}\033[0m")

    time_tag = _format_short_timestamp(play.get("watched_at") or play.get("last_watched"))
    if time_tag:
        cols.append(f"\033[2m{time_tag}\033[0m")

    return "  ".join(cols)


def format_series_group_choice(group: Dict[str, Any]) -> str:
    """Format a grouped series history entry into an aligned summary row."""
    title = group.get("title", "Unknown")
    year = group.get("year")
    plays: List[Dict[str, Any]] = group.get("plays", [])

    title_plain = f"{title} ({year})" if year else str(title)
    title_col = f"📺  \033[1m{_pad_col(title_plain, _TITLE_COL_WIDTH)}\033[0m"

    unique_eps = {
        (p.get("season"), p.get("episode"))
        for p in plays
        if p.get("season") is not None and p.get("episode") is not None
    }
    ep_range = _format_episode_range(plays)
    if len(unique_eps) > 1:
        summary = f"{len(unique_eps)} episodes ({ep_range})" if ep_range else f"{len(unique_eps)} episodes"
    else:
        summary = f"{len(plays)} plays ({ep_range})" if ep_range else f"{len(plays)} plays"
    detail_col = f"\033[36m{_pad_col(summary, _DETAIL_COL_WIDTH)}\033[0m"

    latest = plays[0] if plays else {}
    time_tag = _format_short_timestamp(latest.get("watched_at") or latest.get("last_watched"))
    time_col = f"\033[2m{time_tag}\033[0m" if time_tag else ""

    return "  ".join(part for part in (title_col, detail_col, time_col) if part)


def group_plays_by_show(plays: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse multi-episode series plays by imdb_id, preserving newest-first order."""
    entries: List[Dict[str, Any]] = []
    series_groups: Dict[str, Dict[str, Any]] = {}

    for play in plays:
        imdb_id = play.get("imdb_id")
        if _is_series_play(play) and imdb_id:
            group = series_groups.get(imdb_id)
            if group is None:
                group = {
                    "_grouped_series": True,
                    "imdb_id": imdb_id,
                    "title": play.get("title", "Unknown"),
                    "year": play.get("year"),
                    "plays": [],
                }
                series_groups[imdb_id] = group
                entries.append(group)
            group["plays"].append(play)
        else:
            entries.append(play)

    # Unwrap single-play series so selecting them plays immediately without a 1-item submenu
    result: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("_grouped_series") and len(entry["plays"]) == 1:
            result.append(entry["plays"][0])
        else:
            result.append(entry)
    return result


def _confirm_delete_play(history_mgr: HistoryManager, item: Dict[str, Any]) -> None:
    """Prompt and remove a single play from history."""
    ep_info = ""
    if item.get("season") is not None and item.get("episode") is not None:
        ep_info = f" S{int(item['season']):02d}E{int(item['episode']):02d}"
    date_str = str(item.get("watched_at", ""))[:10]
    confirm = typer.confirm(
        f"\nRemove this single play from history: '{item.get('title')}{ep_info}' ({date_str})?\n"
        "(Any remaining rewatches will keep the item marked as watched)",
        default=False,
    )
    if confirm:
        history_mgr.delete_play(item["id"])
        console.print("[bold green]✓ Play removed from history.[/bold green]")


def _select_series_play(
    history_mgr: HistoryManager,
    imdb_id: str,
    title: str,
    year: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Display watched episodes for a grouped series so the user can replay or remove one."""
    header_title = f"{title} ({year})" if year else title

    def get_series_plays() -> List[Dict[str, Any]]:
        all_plays, _ = history_mgr.list_plays(limit=None, offset=0)
        return [p for p in all_plays if p.get("imdb_id") == imdb_id]

    def build_choices() -> List[Tuple[str, Any]]:
        s_plays = get_series_plays()
        chs: List[Tuple[str, Any]] = []
        for p in s_plays:
            tot = history_mgr.get_play_count(
                p["imdb_id"],
                season=p.get("season"),
                episode=p.get("episode"),
            )
            lbl = format_play_choice(p, play_number=tot if tot > 1 else None, include_show=False)
            chs.append((lbl, p))
        chs.append(("\033[2m↩ Back to History\033[0m", "__back__"))
        return chs

    def on_watch(item: Any) -> None:
        if isinstance(item, dict) and "id" in item:
            _confirm_delete_play(history_mgr, item)

    while True:
        console.clear()
        s_plays = get_series_plays()
        if not s_plays:
            return None

        choices = build_choices()
        selected = prompt_select(
            f"📺 {header_title} ({len(s_plays)} plays)",
            choices,
            on_watch=on_watch,
            refresh_choices=build_choices,
        )
        if selected is None or selected == "__back__":
            return None
        if isinstance(selected, dict):
            return selected


def select_from_history(history_mgr: HistoryManager) -> Optional[Dict[str, Any]]:
    """Display dedicated watch history view with show grouping ([g] toggle) and single-play removal."""
    grouped_mode = True

    def build_choices() -> List[Tuple[str, Any]]:
        plays, _ = history_mgr.list_plays(limit=None, offset=0)
        if not plays:
            return []
        chs: List[Tuple[str, Any]] = []
        if grouped_mode:
            for entry in group_plays_by_show(plays):
                if entry.get("_grouped_series"):
                    chs.append((format_series_group_choice(entry), entry))
                else:
                    tot = history_mgr.get_play_count(
                        entry["imdb_id"],
                        season=entry.get("season"),
                        episode=entry.get("episode"),
                    )
                    chs.append((format_play_choice(entry, play_number=tot if tot > 1 else None), entry))
        else:
            for p in plays:
                tot = history_mgr.get_play_count(
                    p["imdb_id"],
                    season=p.get("season"),
                    episode=p.get("episode"),
                )
                chs.append((format_play_choice(p, play_number=tot if tot > 1 else None), p))
        return chs

    def build_prompt() -> str:
        plays, total_count = history_mgr.list_plays(limit=None, offset=0)
        if grouped_mode:
            grouped_count = len(group_plays_by_show(plays))
            return f"📜 Watch History (Grouped · {grouped_count} items / {total_count} plays)"
        return f"📜 Watch History (All Plays · {total_count} plays)"

    def toggle_group_mode() -> None:
        nonlocal grouped_mode
        grouped_mode = not grouped_mode

    def toggle_label() -> str:
        return "All plays: [g]" if grouped_mode else "Group by show: [g]"

    def on_watch(item: Any) -> None:
        if not isinstance(item, dict):
            return
        if item.get("_grouped_series"):
            plays_in_group = item.get("plays", [])
            if not plays_in_group:
                return
            confirm = typer.confirm(
                f"\nRemove all {len(plays_in_group)} plays for '{item.get('title')}' from history?",
                default=False,
            )
            if confirm:
                for p in plays_in_group:
                    history_mgr.delete_play(p["id"])
                console.print("[bold green]✓ Show plays removed from history.[/bold green]")
            return
        if "id" in item:
            _confirm_delete_play(history_mgr, item)

    while True:
        console.clear()
        choices = build_choices()
        if not choices:
            console.print("[yellow]No watch history found.[/yellow]")
            return None

        selected = prompt_select(
            build_prompt,
            choices,
            on_watch=on_watch,
            refresh_choices=build_choices,
            on_toggle=("g", toggle_label, toggle_group_mode),
        )

        if selected is None:
            return None
        if isinstance(selected, dict) and selected.get("_grouped_series"):
            picked = _select_series_play(
                history_mgr,
                imdb_id=selected["imdb_id"],
                title=selected.get("title", "Unknown"),
                year=selected.get("year"),
            )
            if picked is not None:
                return picked
            continue
        if isinstance(selected, dict):
            return selected
