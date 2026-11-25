from __future__ import annotations

try:
    import curses
except ImportError:
    curses = None
import os
import re
import select
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

import questionary
from prompt_toolkit.formatted_text import ANSI
from .console import console
from rich.panel import Panel
from rich.text import Text

from .metadata.models import EpisodeItem, MediaItem, MediaType, StreamSource

T = TypeVar("T")


def wait_for_any_key(prompt: str = "\n[dim]Press any key to return to selection...[/dim]") -> None:
    """Display a prompt and wait for any keypress before continuing."""
    if sys.platform.startswith("win"):
        if prompt:
            console.print(prompt, end="")
            try:
                sys.stdout.flush()
            except Exception:
                pass
        try:
            import msvcrt

            msvcrt.getch()
            while msvcrt.kbhit():
                msvcrt.getch()
            return
        except (KeyboardInterrupt, EOFError):
            return
        except Exception:
            pass

    if not sys.stdin.isatty():
        if prompt:
            console.print(prompt, end="")
            try:
                sys.stdout.flush()
            except Exception:
                pass
        try:
            sys.stdin.read(1)
        except Exception:
            pass
        return

    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            if prompt:
                console.print(prompt, end="")
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
            ch = os.read(fd, 1)
            if ch == b"\x1b" or (ch and ch[0] >= 0x80):
                while True:
                    r, _, _ = select.select([fd], [], [], 0.02)
                    if not r:
                        break
                    os.read(fd, 1024)
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old_settings)
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception:
        try:
            sys.stdin.read(1)
        except Exception:
            pass


def has_fzf() -> bool:
    """Check if fzf is installed and available in PATH."""
    return shutil.which("fzf") is not None


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*[mK]", "", text)


def prompt_manual_watch_date() -> str:
    """Offer a date for manual marks, defaulting to today (ISO-8601 UTC)."""
    import typer
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = f"Enter watch date (YYYY-MM-DD)"
    try:
        val = typer.prompt(msg, default=today_str).strip()
        if not val or val == today_str:
            return datetime.now(timezone.utc).isoformat()
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def fzf_select(
    prompt: Union[str, Callable[[], str]],
    choices: List[Tuple[str, T]],
    on_info: Optional[Callable[[T], None]] = None,
    on_watch: Optional[Callable[[T], None]] = None,
    refresh_choices: Optional[Callable[[], List[Tuple[str, T]]]] = None,
    on_toggle: Optional[Tuple[str, Union[str, Callable[[], str]], Callable[[], None]]] = None,
) -> Optional[T]:
    """Run interactive fuzzy selection using fzf."""
    if not choices:
        return None

    while True:
        prompt_text = prompt() if callable(prompt) else prompt
        lookup: Dict[str, T] = {}
        lines: List[str] = []
        for display_str, val in choices:
            clean_str = display_str.replace("\n", " ").strip()
            lines.append(clean_str)
            lookup[clean_str] = val
            plain_str = strip_ansi(clean_str)
            lookup[plain_str] = val

        input_text = "\n".join(lines)
        nav_parts = ["Navigate: [↑/↓]", "Select: [Enter]"]
        if on_watch:
            nav_parts.append("Watched: [w]")
        if on_info:
            nav_parts.append("Info: [i]")
        toggle_key = None
        toggle_cb = None
        if on_toggle:
            toggle_key, toggle_label, toggle_cb = on_toggle
            nav_parts.append(toggle_label() if callable(toggle_label) else toggle_label)
        nav_parts.append("Cancel: [Esc]")
        header = "  ·  ".join(nav_parts)

        cmd = [
            "fzf",
            "--ansi",
            "--reverse",
            "--height=50%",
            f"--prompt={prompt_text} > ",
            f"--header={header}",
            "--cycle",
            "--no-multi",
        ]
        expects = []
        if on_info:
            expects.append("i")
        if on_watch:
            expects.append("w")
        if toggle_key:
            expects.append(toggle_key)
        if expects:
            cmd.append(f"--expect={','.join(expects)}")

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
            if expects and lines_out:
                key = lines_out[0].strip()
                if toggle_key and key == toggle_key and toggle_cb:
                    toggle_cb()
                    if refresh_choices:
                        choices = refresh_choices()
                    console.clear()
                    continue
                if len(lines_out) >= 2:
                    selected_line = lines_out[1].strip()
                    val = lookup.get(selected_line) or lookup.get(strip_ansi(selected_line))
                    if key == "i" and on_info and val is not None:
                        console.clear()
                        on_info(val)
                        wait_for_any_key()
                        console.clear()
                        if refresh_choices:
                            choices = refresh_choices()
                        continue
                    elif key == "w" and on_watch and val is not None:
                        on_watch(val)
                        if refresh_choices:
                            choices = refresh_choices()
                        console.clear()
                        continue
                    elif val is not None:
                        return val
            elif lines_out:
                selected_line = lines_out[0].strip()
                return lookup.get(selected_line) or lookup.get(strip_ansi(selected_line))
            return None
        except Exception:
            return questionary_select(
                prompt,
                choices,
                on_info=on_info,
                on_watch=on_watch,
                refresh_choices=refresh_choices,
                on_toggle=on_toggle,
            )


def questionary_select(
    prompt: Union[str, Callable[[], str]],
    choices: List[Tuple[str, T]],
    on_info: Optional[Callable[[T], None]] = None,
    on_watch: Optional[Callable[[T], None]] = None,
    refresh_choices: Optional[Callable[[], List[Tuple[str, T]]]] = None,
    on_toggle: Optional[Tuple[str, Union[str, Callable[[], str]], Callable[[], None]]] = None,
) -> Optional[T]:
    """Fallback interactive arrow-key selection using questionary."""
    if not choices:
        return None

    while True:
        prompt_text = prompt() if callable(prompt) else prompt
        q_choices = [questionary.Choice(title=ANSI(display), value=val) for display, val in choices]
        inst_parts = ["Use arrows or j/k to navigate"]
        if on_watch:
            inst_parts.append("[w] for watched toggle")
        if on_info:
            inst_parts.append("[i] for info")
        toggle_key = None
        toggle_cb = None
        if on_toggle:
            toggle_key, toggle_label, toggle_cb = on_toggle
            inst_parts.append(toggle_label() if callable(toggle_label) else toggle_label)
        inst_parts.append("Enter to select")
        instruction = f"({', '.join(inst_parts)})"

        q = questionary.select(
            message=prompt_text,
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

        ic = None
        for c in q.application.layout.find_all_controls():
            if hasattr(c, "get_pointed_at"):
                ic = c
                break

        if ic is not None:
            if on_info:
                @q.application.key_bindings.add("i", eager=True)
                def _(event, control=ic):
                    val = control.get_pointed_at().value
                    event.app.exit(result=("__info__", val))

            if on_watch:
                @q.application.key_bindings.add("w", eager=True)
                def _w(event, control=ic):
                    val = control.get_pointed_at().value
                    event.app.exit(result=("__watch__", val))

            if toggle_key:
                @q.application.key_bindings.add(toggle_key, eager=True)
                def _t(event):
                    event.app.exit(result="__toggle__")

        from .trakt.background import (
            register_sync_refresh_listener,
            unregister_sync_refresh_listener,
        )

        def _on_sync_refresh():
            try:
                if q.application.is_running and q.application.loop:
                    q.application.loop.call_soon_threadsafe(
                        lambda: q.application.exit(result="__sync_refresh__")
                    )
            except Exception:
                pass

        if refresh_choices:
            register_sync_refresh_listener(_on_sync_refresh)

        try:
            res = q.ask()
            if res == "__sync_refresh__" and refresh_choices:
                choices = refresh_choices()
                console.clear()
                continue
            if res == "__toggle__" and toggle_cb:
                toggle_cb()
                if refresh_choices:
                    choices = refresh_choices()
                console.clear()
                continue
            if isinstance(res, tuple) and len(res) == 2:
                if res[0] == "__info__" and on_info:
                    val = res[1]
                    console.clear()
                    on_info(val)
                    wait_for_any_key()
                    console.clear()
                    if refresh_choices:
                        choices = refresh_choices()
                    continue
                elif res[0] == "__watch__" and on_watch:
                    val = res[1]
                    on_watch(val)
                    if refresh_choices:
                        choices = refresh_choices()
                    console.clear()
                    continue
            return res
        except (KeyboardInterrupt, Exception):
            return None
        finally:
            if refresh_choices:
                unregister_sync_refresh_listener(_on_sync_refresh)


def prompt_select(
    prompt: Union[str, Callable[[], str]],
    choices: List[Tuple[str, T]],
    on_info: Optional[Callable[[T], None]] = None,
    on_watch: Optional[Callable[[T], None]] = None,
    refresh_choices: Optional[Callable[[], List[Tuple[str, T]]]] = None,
    on_toggle: Optional[Tuple[str, Union[str, Callable[[], str]], Callable[[], None]]] = None,
) -> Optional[T]:
    """Select an item using fzf if available, otherwise questionary."""
    if has_fzf():
        return fzf_select(
            prompt,
            choices,
            on_info=on_info,
            on_watch=on_watch,
            refresh_choices=refresh_choices,
            on_toggle=on_toggle,
        )
    return questionary_select(
        prompt,
        choices,
        on_info=on_info,
        on_watch=on_watch,
        refresh_choices=refresh_choices,
        on_toggle=on_toggle,
    )


def format_relative_time(iso_str: Optional[str]) -> str:
    """Convert an ISO datetime string into human-friendly relative time."""
    if not iso_str:
        return ""
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
        else:
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
        if s is not None and e is not None:
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
        elif ch in ("w", "W"):
            if n > 0:
                return ("watch", filtered[self.selected_idx])
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


def curses_select_media(
    items: List[MediaItem],
    history_mgr: Optional[Any] = None,
    metadata_client: Optional[Any] = None,
) -> Optional[MediaItem]:
    """Interactive full-screen media selector with Vim keybindings, filters, and manual watched toggle."""
    if not items:
        return None

    if history_mgr is None:
        try:
            from .cli import get_history_mgr
            history_mgr = get_history_mgr()
        except Exception:
            history_mgr = None

    if metadata_client is None:
        try:
            from .cli import get_metadata_client
            metadata_client = get_metadata_client()
        except Exception:
            metadata_client = None

    if curses is None or sys.platform == "win32":
        def build_fallback_choices():
            chs: List[Tuple[str, MediaItem]] = []
            for item in items:
                w_badge = ""
                if history_mgr:
                    is_w = history_mgr.is_watched(item.imdb_id)
                    w_badge = "[✓] " if is_w else "[ ] "
                type_badge = "🎬 [Movie]" if item.media_type == MediaType.MOVIE else "📺 [Series]"
                year_str = f"({item.year})" if item.year else ""
                label = f"{w_badge}{type_badge} {item.title} {year_str}".strip()
                chs.append((label, item))
            return chs

        def on_fallback_watch(item: MediaItem):
            if not history_mgr:
                return
            _toggle_media_watched(item, history_mgr, metadata_client)

        return prompt_select(
            "Select title",
            build_fallback_choices(),
            on_watch=on_fallback_watch if history_mgr else None,
            refresh_choices=build_fallback_choices,
        )

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
                curses.init_pair(10, curses.COLOR_GREEN, bg)                   # Watched indicator
                has_color = True
            except (curses.error, ValueError):
                has_color = False

        import threading
        from .trakt.background import (
            register_sync_refresh_listener,
            unregister_sync_refresh_listener,
        )

        stdscr.keypad(True)
        stdscr.timeout(250)
        refresh_event = threading.Event()
        register_sync_refresh_listener(refresh_event.set)

        try:
            return _curses_media_loop(stdscr, items, state, has_color, history_mgr, metadata_client, refresh_event)
        finally:
            unregister_sync_refresh_listener(refresh_event.set)


    def _curses_media_loop(stdscr, items, state, has_color, history_mgr, metadata_client, refresh_event):

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

                all_str = f" [a] All ({len(items)}) "
                if state.type_filter == "all":
                    stdscr.addstr(all_str, (curses.color_pair(4) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                else:
                    stdscr.addstr(all_str, curses.color_pair(5) if has_color else curses.A_NORMAL)

                stdscr.addstr("  ")

                mov_str = f" [m] Movies ({total_movies}) "
                if state.type_filter == "movie":
                    stdscr.addstr(mov_str, (curses.color_pair(4) if has_color else curses.A_REVERSE) | curses.A_BOLD)
                else:
                    stdscr.addstr(mov_str, curses.color_pair(5) if has_color else curses.A_NORMAL)

                stdscr.addstr("  ")

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
                    w_tag = ""
                    if history_mgr:
                        if item.media_type == MediaType.MOVIE:
                            w_tag = "[✓] " if history_mgr.is_watched(item.imdb_id) else "[ ] "
                        else:
                            w_tag = "[✓] " if history_mgr.is_watched(item.imdb_id) else "[ ] "

                    year_str = f" ({item.year})" if item.year else ""
                    title_str = item.title

                    used_len = len(prefix) + len(w_tag) + len(badge) + len(year_str) + 1
                    avail_len = max(8, max_x - used_len - 1)
                    if len(title_str) > avail_len:
                        title_str = title_str[:avail_len - 1] + "…"

                    full_line = f"{prefix}{w_tag}{badge}{title_str}{year_str}"
                    full_line_padded = full_line.ljust(max_x - 2)

                    try:
                        if is_curr:
                            curr_attr = (curses.color_pair(1) if has_color else curses.A_REVERSE) | curses.A_BOLD
                            stdscr.addstr(curr_y, 0, full_line_padded[:max_x - 1], curr_attr)
                        else:
                            stdscr.addstr(curr_y, 0, prefix, curses.color_pair(8) if has_color else curses.A_DIM)
                            if w_tag.startswith("[✓]"):
                                stdscr.addstr(w_tag, (curses.color_pair(10) if has_color else curses.A_BOLD))
                            else:
                                stdscr.addstr(w_tag, curses.color_pair(8) if has_color else curses.A_DIM)
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

                    help_text = "[j/k] Navigate   [/] Search   [w] Toggle Watched   [Enter] Select   [q/Esc] Quit"
                    stdscr.addstr(help_y, 2, help_text[:max_x - 3], curses.color_pair(8) if has_color else curses.A_DIM)
                except curses.error:
                    pass

            stdscr.refresh()

            # Input dispatch (poll timeout every 250ms so background sync can trigger redraws)
            ch = None
            while True:
                try:
                    ch = stdscr.get_wch()
                    break
                except curses.error:
                    if refresh_event.is_set():
                        refresh_event.clear()
                        ch = None
                        break
                    continue

            if ch is None or ch == curses.KEY_RESIZE:
                continue

            visible_height = max(1, list_max_lines)
            res = state.handle_key(ch, visible_height=visible_height)
            if res:
                action, selected_item = res
                if action == "select":
                    return selected_item
                elif action == "watch" and selected_item and history_mgr:
                    curses.def_prog_mode()
                    curses.endwin()
                    try:
                        _toggle_media_watched(selected_item, history_mgr, metadata_client)
                    finally:
                        curses.reset_prog_mode()
                        stdscr.refresh()
                    continue
                elif action == "quit":
                    return None

    try:
        return curses.wrapper(_ui)
    except Exception:
        choices: List[Tuple[str, MediaItem]] = []
        for item in items:
            type_badge = "🎬 [Movie]" if item.media_type == MediaType.MOVIE else "📺 [Series]"
            year_str = f"({item.year})" if item.year else ""
            label = f"{type_badge} {item.title} {year_str}".strip()
            choices.append((label, item))
        return prompt_select("Select title", choices)


def _toggle_media_watched(
    item: MediaItem,
    history_mgr: Any,
    metadata_client: Optional[Any] = None,
) -> None:
    """Handle watched status toggling for a movie or show with preview and confirmations."""
    import typer
    from .storage.history import is_episode_released

    if item.media_type == MediaType.MOVIE:
        is_w = history_mgr.is_watched(item.imdb_id)
        if is_w:
            p_count = history_mgr.get_play_count(item.imdb_id)
            confirm = typer.confirm(
                f"\nClear all {p_count} play(s) for '{item.title}'?",
                default=False,
            )
            if confirm:
                history_mgr.clear_plays_for_show_or_movie(item.imdb_id)
                console.print(f"[bold green]✓ Cleared plays for '{item.title}'.[/bold green]")
        else:
            w_date = prompt_manual_watch_date()
            history_mgr.record_play(item, watched_at=w_date, source="manual")
            console.print(f"[bold green]✓ Marked '{item.title}' watched.[/bold green]")
    else:
        episodes = []
        if metadata_client:
            with console.status(f"[bold cyan]Fetching episodes for {item.title}...[/bold cyan]"):
                episodes = metadata_client.get_episodes(item.imdb_id)
        if not episodes:
            console.print("[yellow]Could not retrieve episodes for show-wide toggle.[/yellow]")
            return

        w_count, tot_rel = history_mgr.get_show_watched_status(item.imdb_id, episodes, include_specials=False)
        if w_count == tot_rel and tot_rel > 0:
            p_count = sum(history_mgr.get_play_count(item.imdb_id, ep.season, ep.episode) for ep in episodes)
            confirm = typer.confirm(
                f"\nClear all {p_count} play(s) across all seasons for '{item.title}'?",
                default=False,
            )
            if confirm:
                history_mgr.clear_plays_for_show_or_movie(item.imdb_id, include_specials=True)
                console.print(f"[bold green]✓ Cleared all plays for '{item.title}'.[/bold green]")
        else:
            specials = [ep for ep in episodes if ep.season == 0 and is_episode_released(ep)]
            include_specials = False
            if specials:
                include_specials = typer.confirm(
                    f"\nInclude {len(specials)} specials (Season 0) for '{item.title}'?",
                    default=False,
                )
            unplayed = [
                ep for ep in episodes
                if (include_specials or ep.season > 0)
                and is_episode_released(ep)
                and not history_mgr.is_watched(item.imdb_id, ep.season, ep.episode)
            ]
            confirm = typer.confirm(
                f"\nMark '{item.title}' watched? {len(unplayed)} released episode(s) will be marked (preserving {w_count} existing play dates).",
                default=True,
            )
            if confirm:
                w_date = prompt_manual_watch_date()
                history_mgr.mark_episodes_watched(item, unplayed, watched_at=w_date)
                console.print(f"[bold green]✓ Marked {len(unplayed)} episode(s) watched.[/bold green]")


def select_media(
    items: List[MediaItem],
    history_mgr: Optional[Any] = None,
    metadata_client: Optional[Any] = None,
) -> Optional[MediaItem]:
    """Prompt user to choose a movie or show from search results with Vim keybindings and filters."""
    if not items:
        return None
    return curses_select_media(items, history_mgr=history_mgr, metadata_client=metadata_client)


def select_season(
    episodes: List[EpisodeItem],
    media: Optional[MediaItem] = None,
    history_mgr: Optional[Any] = None,
) -> Optional[int]:
    """Prompt user to select a season from available episodes with watched indicators and 'w' toggle."""
    seasons = sorted(list({ep.season for ep in episodes if ep.season > 0}))
    if not seasons:
        return None

    if history_mgr is None:
        try:
            from .cli import get_history_mgr
            history_mgr = get_history_mgr()
        except Exception:
            history_mgr = None

    imdb_id = media.imdb_id if media else (episodes[0].id.split(":")[0] if episodes and ":" in episodes[0].id else "")

    def build_choices() -> List[Tuple[str, int]]:
        chs: List[Tuple[str, int]] = []
        for s in seasons:
            count = sum(1 for ep in episodes if ep.season == s)
            if history_mgr and imdb_id:
                w_count, tot_rel = history_mgr.get_season_watched_status(imdb_id, s, episodes)
                if w_count == tot_rel and tot_rel > 0:
                    badge = f"\033[32m[✓ {w_count}/{tot_rel}]\033[0m"
                elif w_count > 0:
                    badge = f"\033[33m[◐ {w_count}/{tot_rel}]\033[0m"
                else:
                    badge = f"\033[2m[  {w_count}/{tot_rel}]\033[0m"
                chs.append((f"{badge} Season {s} ({count} episodes)", s))
            else:
                chs.append((f"Season {s} ({count} episodes)", s))
        return chs

    def on_watch(s: int) -> None:
        if not history_mgr or not imdb_id:
            return
        import typer
        from .storage.history import is_episode_released
        season_eps = [ep for ep in episodes if ep.season == s]
        w_count, tot_rel = history_mgr.get_season_watched_status(imdb_id, s, episodes)
        if w_count == tot_rel and tot_rel > 0:
            play_count = sum(history_mgr.get_play_count(imdb_id, s, ep.episode) for ep in season_eps)
            confirm = typer.confirm(
                f"\nClear all {play_count} plays for Season {s} across {w_count} episode(s)?",
                default=False,
            )
            if confirm:
                history_mgr.clear_plays_for_season(imdb_id, s)
                console.print(f"[bold green]✓ Cleared all plays for Season {s}.[/bold green]")
        else:
            unplayed = [
                ep for ep in season_eps
                if is_episode_released(ep) and not history_mgr.is_watched(imdb_id, s, ep.episode)
            ]
            confirm = typer.confirm(
                f"\nMark Season {s} watched? {len(unplayed)} released episode(s) will be marked (preserving {w_count} existing play dates).",
                default=True,
            )
            if confirm:
                date_str = prompt_manual_watch_date()
                dummy_media = media or MediaItem(
                    id=imdb_id,
                    imdb_id=imdb_id,
                    title=f"Season {s}",
                    media_type=MediaType.SERIES,
                )
                history_mgr.mark_episodes_watched(dummy_media, unplayed, watched_at=date_str)
                console.print(f"[bold green]✓ Marked {len(unplayed)} episodes watched.[/bold green]")

    return prompt_select(
        "Select season",
        build_choices(),
        on_watch=on_watch if history_mgr and imdb_id else None,
        refresh_choices=build_choices,
    )


def select_episode(
    episodes: List[EpisodeItem],
    season: int,
    media: Optional[MediaItem] = None,
    history_mgr: Optional[Any] = None,
) -> Optional[EpisodeItem]:
    """Prompt user to choose an episode with watched indicators and 'w' toggle."""
    season_episodes = [ep for ep in episodes if ep.season == season]
    if not season_episodes:
        return None

    if history_mgr is None:
        try:
            from .cli import get_history_mgr
            history_mgr = get_history_mgr()
        except Exception:
            history_mgr = None

    imdb_id = media.imdb_id if media else (season_episodes[0].id.split(":")[0] if season_episodes and ":" in season_episodes[0].id else "")

    def build_choices() -> List[Tuple[str, EpisodeItem]]:
        chs: List[Tuple[str, EpisodeItem]] = []
        for ep in season_episodes:
            ind = ""
            if history_mgr and imdb_id:
                p_count = history_mgr.get_play_count(imdb_id, season, ep.episode)
                if p_count > 1:
                    ind = f"\033[35m[✓ {p_count}x]\033[0m "
                elif p_count == 1:
                    ind = "\033[32m[✓]\033[0m "
                else:
                    ind = "\033[2m[ ]\033[0m "

            label = f"{ind}E{ep.episode:02d}: {ep.title}"
            if ep.released:
                label += f" \033[2m({ep.released[:10]})\033[0m"
            chs.append((label, ep))
        return chs

    def on_info(ep: EpisodeItem) -> None:
        if media:
            show_media_info(media, ep)
        else:
            dummy_media = MediaItem(
                id=ep.id,
                imdb_id=imdb_id,
                title=f"Season {season}",
                media_type=MediaType.SERIES,
            )
            show_media_info(dummy_media, ep)

    def on_watch(ep: EpisodeItem) -> None:
        if not history_mgr or not imdb_id:
            return
        import typer
        from .storage.history import is_episode_released

        dummy_media = media or MediaItem(
            id=ep.id,
            imdb_id=imdb_id,
            title=f"Season {season}",
            media_type=MediaType.SERIES,
        )
        p_count = history_mgr.get_play_count(imdb_id, season, ep.episode)
        if p_count > 0:
            confirm = typer.confirm(
                f"\nClear all {p_count} play(s) for E{ep.episode:02d} ({ep.title})?",
                default=False,
            )
            if confirm:
                history_mgr.clear_plays_for_episode(imdb_id, season, ep.episode)
                console.print(f"[bold green]✓ Cleared plays for E{ep.episode:02d}.[/bold green]")
        else:
            earlier_unplayed = [
                e for e in season_episodes
                if e.episode < ep.episode and is_episode_released(e) and not history_mgr.is_watched(imdb_id, season, e.episode)
            ]
            if earlier_unplayed:
                single_lbl = f"Mark E{ep.episode:02d} watched only"
                earlier_lbl = f"Mark earlier episodes and E{ep.episode:02d} watched (Preview: {len(earlier_unplayed) + 1} episodes)"
                act = prompt_select(
                    f"Watched action for E{ep.episode:02d}",
                    [(single_lbl, "single"), (earlier_lbl, "earlier"), ("Cancel", "cancel")],
                )
                if not act or act == "cancel":
                    return
                if act == "earlier":
                    preview = f"Mark {len(earlier_unplayed) + 1} released episodes watched? (Preserving existing play dates)"
                    if typer.confirm(f"\n{preview}", default=True):
                        w_date = prompt_manual_watch_date()
                        to_mark = earlier_unplayed + [ep]
                        history_mgr.mark_episodes_watched(dummy_media, to_mark, watched_at=w_date)
                        console.print(f"[bold green]✓ Marked {len(to_mark)} episodes watched.[/bold green]")
                else:
                    w_date = prompt_manual_watch_date()
                    history_mgr.record_play(dummy_media, episode=ep, watched_at=w_date, source="manual")
                    console.print(f"[bold green]✓ Marked E{ep.episode:02d} watched.[/bold green]")
            else:
                w_date = prompt_manual_watch_date()
                history_mgr.record_play(dummy_media, episode=ep, watched_at=w_date, source="manual")
                console.print(f"[bold green]✓ Marked E{ep.episode:02d} watched.[/bold green]")

    return prompt_select(
        f"Select episode for Season {season}",
        build_choices(),
        on_info=on_info,
        on_watch=on_watch if history_mgr and imdb_id else None,
        refresh_choices=build_choices,
    )


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

