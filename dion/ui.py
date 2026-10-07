from __future__ import annotations

import curses
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import questionary
from prompt_toolkit.formatted_text import ANSI
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .metadata.models import EpisodeItem, MediaItem, MediaType, StreamSource

console = Console()
T = TypeVar("T")


def has_fzf() -> bool:
    """Check if fzf is installed and available in PATH."""
    return shutil.which("fzf") is not None


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*[mK]", "", text)


def fzf_select(
    prompt: str,
    choices: List[Tuple[str, T]],
    on_info: Optional[Callable[[T], None]] = None,
) -> Optional[T]:
    """Run interactive fuzzy selection using fzf."""
    if not choices:
        return None

    # Mapping from displayed line to object
    lookup: Dict[str, T] = {}
    lines: List[str] = []
    for display_str, val in choices:
        clean_str = display_str.replace("\n", " ").strip()
        lines.append(clean_str)
        lookup[clean_str] = val
        plain_str = strip_ansi(clean_str)
        lookup[plain_str] = val

    input_text = "\n".join(lines)
    header = (
        "Navigate: [↑/↓]  ·  Select: [Enter]  ·  Info: [i]  ·  Cancel: [Esc]"
        if on_info
        else "Use arrows/typing to search, Enter to select, Esc to cancel"
    )
    cmd = [
        "fzf",
        "--ansi",
        "--reverse",
        "--height=50%",
        f"--prompt={prompt} > ",
        f"--header={header}",
        "--cycle",
        "--no-multi",
    ]
    if on_info:
        cmd.append("--expect=i")

    while True:
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            raw = proc.stdout
            if not raw:
                return None

            lines_out = raw.splitlines()
            if on_info and len(lines_out) >= 2 and lines_out[0].strip() == "i":
                selected_line = lines_out[1].strip()
                val = lookup.get(selected_line) or lookup.get(strip_ansi(selected_line))
                if val is not None:
                    console.clear()
                    on_info(val)
                    try:
                        console.input("\n[dim]Press Enter to return to selection...[/dim]")
                    except (KeyboardInterrupt, EOFError):
                        pass
                    console.clear()
                    continue
                return None
            elif on_info and len(lines_out) >= 2:
                selected_line = lines_out[1].strip()
                return lookup.get(selected_line) or lookup.get(strip_ansi(selected_line))
            elif lines_out:
                selected_line = lines_out[0].strip()
                return lookup.get(selected_line) or lookup.get(strip_ansi(selected_line))
            return None
        except Exception:
            # Fall back to questionary if fzf encounters issues
            return questionary_select(prompt, choices, on_info=on_info)


def questionary_select(
    prompt: str,
    choices: List[Tuple[str, T]],
    on_info: Optional[Callable[[T], None]] = None,
) -> Optional[T]:
    """Fallback interactive arrow-key selection using questionary."""
    if not choices:
        return None

    while True:
        q_choices = [questionary.Choice(title=ANSI(display), value=val) for display, val in choices]
        instruction = (
            "(Use arrows or j/k to navigate, [i] for info, Enter to select)"
            if on_info
            else None
        )
        q = questionary.select(
            message=prompt,
            choices=q_choices,
            use_jk_keys=True,
            instruction=instruction,
            style=questionary.Style(
                [
                    ("qmark", "fg:#ff79c6 bold"),
                    ("question", "bold"),
                    ("answer", "fg:#50fa7b bold"),
                    ("pointer", "fg:#bd93f9 bold"),
                    ("highlighted", "fg:#8be9fd bold"),
                    ("selected", "fg:#50fa7b"),
                ]
            ),
        )

        if on_info:
            ic = None
            for c in q.application.layout.find_all_controls():
                if hasattr(c, "get_pointed_at"):
                    ic = c
                    break

            if ic is not None:
                @q.application.key_bindings.add("i", eager=True)
                def _(event, control=ic):
                    val = control.get_pointed_at().value
                    event.app.exit(result=("__info__", val))

        try:
            res = q.ask()
            if isinstance(res, tuple) and len(res) == 2 and res[0] == "__info__":
                val = res[1]
                console.clear()
                on_info(val)
                try:
                    console.input("\n[dim]Press Enter to return to selection...[/dim]")
                except (KeyboardInterrupt, EOFError):
                    pass
                console.clear()
                continue
            return res
        except (KeyboardInterrupt, Exception):
            return None


def prompt_select(
    prompt: str,
    choices: List[Tuple[str, T]],
    on_info: Optional[Callable[[T], None]] = None,
) -> Optional[T]:
    """Select an item using fzf if available, otherwise questionary."""
    if has_fzf():
        return fzf_select(prompt, choices, on_info=on_info)
    return questionary_select(prompt, choices, on_info=on_info)


def format_relative_time(iso_str: Optional[str]) -> str:
    """Convert an ISO datetime string into human-friendly relative time."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        now = datetime.now()
        diff = now - dt
        seconds = int(diff.total_seconds())

        if seconds < 60:
            return "Just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24 and dt.date() == now.date():
            return f"{hours}h ago"
        if dt.date() == (now.date() - timedelta(days=1)):
            return "Yesterday"
        days = (now.date() - dt.date()).days
        if days < 7:
            return f"{days}d ago"
        return dt.strftime("%b %d")
    except Exception:
        return iso_str[:10]


def format_playback_progress(position: float, duration: Optional[float] = None) -> str:
    """Format playback position and duration into progress string, e.g. '15:35 / 43:00 (36%)'."""
    pos = max(0.0, float(position or 0.0))
    if duration and duration > 0:
        dur = float(duration)
        force_hours = dur >= 3600 or pos >= 3600

        def _fmt(s: float) -> str:
            total_s = max(0, int(round(s)))
            hrs = total_s // 3600
            mins = (total_s % 3600) // 60
            secs = total_s % 60
            if hrs > 0 or force_hours:
                return f"{hrs:02d}:{mins:02d}:{secs:02d}"
            return f"{mins:02d}:{secs:02d}"

        pos_str = _fmt(pos)
        dur_str = _fmt(dur)
        pct = min(100, max(0, int(round((pos / dur) * 100))))
        return f"{pos_str} / {dur_str} ({pct}%)"
    elif pos > 10:
        total_secs = int(pos)
        hrs = total_secs // 3600
        mins = (total_secs % 3600) // 60
        secs = total_secs % 60
        pos_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"
        return f"at {pos_str}"
    else:
        return "Not started"


def format_history_choice(item: Dict[str, Any], ansi: bool = True) -> str:
    """Format a watch history item into an aesthetic, informative menu entry."""
    is_series = item.get("media_type") == "series" or ("season" in item and "episode" in item)
    badge = "📺" if is_series else "🎬"
    title = item.get("title", "Unknown")
    year = item.get("year")

    parts: List[str] = []

    # Title & Year
    title_str = f"{badge}  {title}"
    if year:
        title_str += f" ({year})"
    if ansi:
        title_str = f"\033[1m{title_str}\033[0m"
    parts.append(title_str)

    # Episode details if series
    if is_series and "season" in item and "episode" in item:
        s = item["season"]
        e = item["episode"]
        ep_code = f"S{s:02d}E{e:02d}"
        ep_title = item.get("episode_title")
        if ep_title:
            ep_str = f"{ep_code}: {ep_title}"
        else:
            ep_str = ep_code
        if ansi:
            ep_str = f"\033[36m{ep_str}\033[0m"
        parts.append(ep_str)

    # Playback resume position and progress
    pos = float(item.get("position", 0.0) or 0.0)
    dur = item.get("duration")
    if dur is not None:
        try:
            dur = float(dur)
        except (ValueError, TypeError):
            dur = None

    prog_str = format_playback_progress(pos, dur)
    if prog_str:
        if ansi:
            prog_str = f"\033[33m{prog_str}\033[0m"
        parts.append(prog_str)

    # Relative time
    rel_time = format_relative_time(item.get("last_watched"))
    if rel_time:
        if ansi:
            rel_time = f"\033[2m{rel_time}\033[0m"
        parts.append(rel_time)

    return "  ·  ".join(parts)


class MediaSelectorState:
    """State machine managing navigation, search, and type filters for media items."""

    def __init__(self, items: List[MediaItem]):
        self.items = items
        self.type_filter: str = "all"  # "all", "movie", "series"
        self.search_query: str = ""
        self.is_searching: bool = False
        self.selected_idx: int = 0
        self.scroll_offset: int = 0

    def get_filtered_items(self) -> List[MediaItem]:
        filtered = self.items
        if self.type_filter == "movie":
            filtered = [i for i in filtered if i.media_type == MediaType.MOVIE]
        elif self.type_filter == "series":
            filtered = [i for i in filtered if i.media_type == MediaType.SERIES]

        if self.search_query:
            q = self.search_query.lower()
            filtered = [
                i
                for i in filtered
                if q in i.title.lower() or (i.year and q in str(i.year))
            ]
        return filtered

    def handle_key(
        self, ch, visible_height: int = 10
    ) -> Optional[Tuple[str, Optional[MediaItem]]]:
        filtered = self.get_filtered_items()
        n = len(filtered)

        if self.is_searching:
            if ch in ("\n", "\r", curses.KEY_ENTER, 10, 13):
                self.is_searching = False
                return None
            elif ch in (27, "\x1b"):  # Escape cancels search mode
                self.is_searching = False
                return None
            elif ch in (curses.KEY_BACKSPACE, "\b", "\x7f", 127, 8):
                self.search_query = self.search_query[:-1]
                self.selected_idx = 0
                return None
            elif ch in (curses.KEY_DOWN, "\x0e", 14):  # Down / Ctrl-N
                if n > 0:
                    self.selected_idx = (self.selected_idx + 1) % n
                return None
            elif ch in (curses.KEY_UP, "\x10", 16):  # Up / Ctrl-P
                if n > 0:
                    self.selected_idx = (self.selected_idx - 1) % n
                return None
            elif isinstance(ch, str) and ch.isprintable():
                self.search_query += ch
                self.selected_idx = 0
                return None
            return None

        # Normal mode
        if ch in ("j", curses.KEY_DOWN, "\x0e", 14):
            if n > 0:
                self.selected_idx = (self.selected_idx + 1) % n
        elif ch in ("k", curses.KEY_UP, "\x10", 16):
            if n > 0:
                self.selected_idx = (self.selected_idx - 1) % n
        elif ch in ("g", curses.KEY_HOME):
            self.selected_idx = 0
        elif ch in ("G", curses.KEY_END):
            if n > 0:
                self.selected_idx = n - 1
        elif ch in (curses.KEY_NPAGE, "\x04", 4):  # PageDown / Ctrl-D
            if n > 0:
                step = max(1, visible_height // 2)
                self.selected_idx = min(n - 1, self.selected_idx + step)
        elif ch in (curses.KEY_PPAGE, "\x15", 21):  # PageUp / Ctrl-U
            if n > 0:
                step = max(1, visible_height // 2)
                self.selected_idx = max(0, self.selected_idx - step)
        elif ch == "/":
            self.is_searching = True
        elif ch in ("m", "M"):
            self.type_filter = "movie"
            self.selected_idx = 0
            self.scroll_offset = 0
        elif ch in ("s", "S"):
            self.type_filter = "series"
            self.selected_idx = 0
            self.scroll_offset = 0
        elif ch in ("a", "A"):
            self.type_filter = "all"
            self.selected_idx = 0
            self.scroll_offset = 0
        elif ch in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            if n > 0:
                return ("select", filtered[self.selected_idx])
        elif ch in (27, "\x1b"):  # Escape in normal mode
            if self.search_query:
                self.search_query = ""
                self.selected_idx = 0
            else:
                return ("quit", None)
        elif ch in ("q", "Q"):
            return ("quit", None)

        return None


def curses_select_media(items: List[MediaItem]) -> Optional[MediaItem]:
    """Interactive full-screen media selector with Vim keybindings and filters."""
    if not items:
        return None

    state = MediaSelectorState(items)

    def _ui(stdscr) -> Optional[MediaItem]:
        try:
            curses.curs_set(0)
        except curses.error:
            pass

        if hasattr(curses, "set_escdelay"):
            try:
                curses.set_escdelay(25)
            except curses.error:
                pass

        has_color = False
        if curses.has_colors():
            try:
                curses.start_color()
                try:
                    curses.use_default_colors()
                    bg = -1
                except curses.error:
                    bg = curses.COLOR_BLACK

                curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)    # Selected item
                curses.init_pair(2, curses.COLOR_MAGENTA, bg)                  # Movie badge
                curses.init_pair(3, curses.COLOR_CYAN, bg)                     # Show badge
                curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_YELLOW)   # Active filter tab
                curses.init_pair(5, curses.COLOR_WHITE, bg)                    # Inactive tab
                curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_GREEN)    # Normal mode badge
                curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_YELLOW)   # Search mode badge
                curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, bg)  # Dim/borders
                curses.init_pair(9, curses.COLOR_YELLOW, bg)                   # Year / search highlight
                has_color = True
            except (curses.error, ValueError):
                has_color = False

        stdscr.keypad(True)

        while True:
            filtered = state.get_filtered_items()
            if filtered:
                state.selected_idx = max(0, min(state.selected_idx, len(filtered) - 1))
            else:
                state.selected_idx = 0

            max_y, max_x = stdscr.getmaxyx()
            stdscr.erase()

            if max_y < 8 or max_x < 30:
                try:
                    stdscr.addstr(0, 0, "Terminal too small"[:max_x - 1])
                except curses.error:
                    pass
                stdscr.refresh()
                try:
                    ch = stdscr.get_wch()
                    if ch in ("q", "Q", 27, "\x1b"):
                        return None
                except curses.error:
                    pass
                continue

            # 1. Header Title
            title_text = " ✦ DION — SEARCH RESULTS ✦ "
            start_x = max(0, (max_x - len(title_text)) // 2)
            try:
                stdscr.addstr(0, start_x, title_text, curses.A_BOLD)
            except curses.error:
                pass

            # Filter tabs line
            total_movies = sum(1 for i in items if i.media_type == MediaType.MOVIE)
            total_shows = sum(1 for i in items if i.media_type == MediaType.SERIES)

            try:
                stdscr.addstr(1, 2, "Filter: ", curses.color_pair(8) if has_color else curses.A_DIM)

                # [a] All
                all_str = f" [a] All ({len(items)}) "
                if state.type_filter == "all":
                    stdscr.addstr(all_str, (curses.color_pair(4) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                else:
                    stdscr.addstr(all_str, curses.color_pair(5) if has_color else curses.A_NORMAL)

                stdscr.addstr("  ")

                # [m] Movies
                mov_str = f" [m] Movies ({total_movies}) "
                if state.type_filter == "movie":
                    stdscr.addstr(mov_str, (curses.color_pair(4) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                else:
                    stdscr.addstr(mov_str, curses.color_pair(5) if has_color else curses.A_NORMAL)

                stdscr.addstr("  ")

                # [s] Shows
                ser_str = f" [s] Shows ({total_shows}) "
                if state.type_filter == "series":
                    stdscr.addstr(ser_str, (curses.color_pair(4) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                else:
                    stdscr.addstr(ser_str, curses.color_pair(5) if has_color else curses.A_NORMAL)

                count_label = f"Showing {len(filtered)} items"
                if len(count_label) + 2 < max_x - 45:
                    stdscr.addstr(1, max_x - len(count_label) - 2, count_label, curses.color_pair(8) if has_color else curses.A_DIM)
            except curses.error:
                pass

            # Divider line
            try:
                stdscr.addstr(2, 0, "─" * (max_x - 1), curses.color_pair(8) if has_color else curses.A_DIM)
            except curses.error:
                pass

            # 2. Results List
            list_start_y = 3
            list_max_lines = max_y - 6

            if list_max_lines > 0:
                if state.selected_idx < state.scroll_offset:
                    state.scroll_offset = state.selected_idx
                elif state.selected_idx >= state.scroll_offset + list_max_lines:
                    state.scroll_offset = state.selected_idx - list_max_lines + 1

                for line_idx in range(list_max_lines):
                    item_idx = state.scroll_offset + line_idx
                    curr_y = list_start_y + line_idx
                    if item_idx >= len(filtered):
                        break

                    item = filtered[item_idx]
                    is_curr = (item_idx == state.selected_idx)

                    prefix = " ▶ " if is_curr else "   "
                    badge = "[Movie] " if item.media_type == MediaType.MOVIE else "[Show]  "
                    year_str = f" ({item.year})" if item.year else ""
                    title_str = item.title

                    used_len = len(prefix) + len(badge) + len(year_str) + 1
                    avail_len = max(8, max_x - used_len - 1)
                    if len(title_str) > avail_len:
                        title_str = title_str[:avail_len - 1] + "…"

                    full_line = f"{prefix}{badge}{title_str}{year_str}"
                    full_line_padded = full_line.ljust(max_x - 2)

                    try:
                        if is_curr:
                            curr_attr = (curses.color_pair(1) if has_color else curses.A_REVERSE) | curses.A_BOLD
                            stdscr.addstr(curr_y, 0, full_line_padded[:max_x - 1], curr_attr)
                        else:
                            stdscr.addstr(curr_y, 0, prefix, curses.color_pair(8) if has_color else curses.A_DIM)
                            badge_color = (curses.color_pair(2) if item.media_type == MediaType.MOVIE else curses.color_pair(3)) if has_color else curses.A_BOLD
                            stdscr.addstr(badge, badge_color | curses.A_BOLD)
                            stdscr.addstr(title_str, curses.A_BOLD)
                            stdscr.addstr(year_str, curses.color_pair(9) if has_color else curses.A_DIM)
                    except curses.error:
                        pass

            if not filtered:
                empty_msg = "No results found matching current filter."
                try:
                    stdscr.addstr(list_start_y + 2, max(0, (max_x - len(empty_msg)) // 2), empty_msg, (curses.color_pair(8) if has_color else curses.A_DIM) | curses.A_ITALIC)
                except curses.error:
                    pass

            # 3. Footer Area
            footer_sep_y = max_y - 3
            try:
                stdscr.addstr(footer_sep_y, 0, "─" * (max_x - 1), curses.color_pair(8) if has_color else curses.A_DIM)
            except curses.error:
                pass

            footer_y = max_y - 2
            help_y = max_y - 1

            if state.is_searching:
                try:
                    curses.curs_set(1)
                except curses.error:
                    pass
                search_mode_tag = " SEARCH "
                try:
                    stdscr.addstr(footer_y, 2, search_mode_tag, (curses.color_pair(7) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                    search_display = f" / {state.search_query}"
                    stdscr.addstr(footer_y, 2 + len(search_mode_tag) + 1, search_display[:max_x - 20], curses.A_BOLD)
                    stdscr.addstr(help_y, 2, "Type to filter  |  [Enter] Confirm filter  |  [Esc] Normal mode", curses.color_pair(8) if has_color else curses.A_DIM)
                    cursor_x = min(max_x - 2, 2 + len(search_mode_tag) + 1 + len(search_display))
                    stdscr.move(footer_y, cursor_x)
                except curses.error:
                    pass
            else:
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                normal_mode_tag = " NORMAL "
                try:
                    stdscr.addstr(footer_y, 2, normal_mode_tag, (curses.color_pair(6) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                    if state.search_query:
                        active_query_tag = f" Filtered by: '{state.search_query}' (press / to edit, Esc to clear)"
                        stdscr.addstr(footer_y, 2 + len(normal_mode_tag) + 1, active_query_tag[:max_x - 15], curses.color_pair(9) if has_color else curses.A_BOLD)

                    help_text = "[j/k] Navigate   [/] Search   [m] Movies   [s] Shows   [a] All   [Enter] Select   [q/Esc] Quit"
                    stdscr.addstr(help_y, 2, help_text[:max_x - 3], curses.color_pair(8) if has_color else curses.A_DIM)
                except curses.error:
                    pass

            stdscr.refresh()

            # Input dispatch
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue

            if ch == curses.KEY_RESIZE:
                continue

            visible_height = max(1, list_max_lines)
            res = state.handle_key(ch, visible_height=visible_height)
            if res:
                action, selected_item = res
                if action == "select":
                    return selected_item
                elif action == "quit":
                    return None

    try:
        return curses.wrapper(_ui)
    except Exception:
        # Fallback to prompt_select if curses cannot initialize (e.g. non-interactive stream)
        choices: List[Tuple[str, MediaItem]] = []
        for item in items:
            type_badge = "🎬 [Movie]" if item.media_type == MediaType.MOVIE else "📺 [Series]"
            year_str = f"({item.year})" if item.year else ""
            label = f"{type_badge} {item.title} {year_str}".strip()
            choices.append((label, item))
        return prompt_select("Select title", choices)


def select_media(items: List[MediaItem]) -> Optional[MediaItem]:
    """Prompt user to choose a movie or show from search results with Vim keybindings and filters."""
    if not items:
        return None
    return curses_select_media(items)


def select_season(episodes: List[EpisodeItem]) -> Optional[int]:
    """Prompt user to select a season from available episodes."""
    seasons = sorted(list({ep.season for ep in episodes if ep.season > 0}))
    if not seasons:
        return None
    if len(seasons) == 1:
        return seasons[0]

    choices: List[Tuple[str, int]] = []
    for s in seasons:
        count = sum(1 for ep in episodes if ep.season == s)
        choices.append((f"Season {s} ({count} episodes)", s))

    return prompt_select("Select season", choices)


def select_episode(
    episodes: List[EpisodeItem],
    season: int,
    media: Optional[MediaItem] = None,
) -> Optional[EpisodeItem]:
    """Prompt user to choose an episode in the selected season."""
    season_episodes = [ep for ep in episodes if ep.season == season]
    if not season_episodes:
        return None

    choices: List[Tuple[str, EpisodeItem]] = []
    for ep in season_episodes:
        label = f"E{ep.episode:02d}: {ep.title}"
        if ep.released:
            label += f" ({ep.released[:10]})"
        choices.append((label, ep))

    def on_info(ep: EpisodeItem) -> None:
        if media:
            show_media_info(media, ep)
        else:
            dummy_media = MediaItem(
                id=ep.id,
                imdb_id=ep.id.split(":")[0] if ":" in ep.id else "",
                title=f"Season {season}",
                media_type=MediaType.SERIES,
            )
            show_media_info(dummy_media, ep)

    return prompt_select(f"Select episode for Season {season}", choices, on_info=on_info)


def select_source(sources: List[StreamSource]) -> Optional[StreamSource]:
    """Prompt user to choose a streaming server/source."""
    if not sources:
        return None
    if len(sources) == 1:
        return sources[0]

    choices: List[Tuple[str, StreamSource]] = []
    for s in sources:
        label = f"[{s.server.upper()}] Quality: {s.quality}"
        choices.append((label, s))

    return prompt_select("Select stream source", choices)


def show_media_info(item: MediaItem, episode: Optional[EpisodeItem] = None) -> None:
    """Print an aesthetic information card for the media item."""
    text = Text()
    text.append(f"{item.title}\n", style="bold magenta")
    if item.year:
        text.append(f"Year: {item.year}  |  ", style="dim")
    text.append(f"Type: {item.media_type.value.capitalize()}\n", style="cyan")

    if episode:
        text.append(f"\nEpisode: {episode.display_name}\n", style="bold green")
        if episode.overview:
            text.append(f"{episode.overview}\n", style="italic")
    elif item.overview:
        text.append(f"\n{item.overview}\n", style="dim")

    console.print(
        Panel(
            text,
            title="[bold purple] Dion [/bold purple]",
            subtitle=f"[dim]IMDb: {item.imdb_id}[/dim]",
            border_style="magenta",
            padding=(1, 2),
        )
    )

