from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer
from rich.console import Console

from .metadata import EpisodeItem, MediaItem, MediaType
from .ui import (
    console,
    format_history_choice,
    prompt_select,
    select_episode,
    select_media,
    select_season,
    select_source,
    show_media_info,
)

if TYPE_CHECKING:
    from .metadata import CinemetaClient
    from .player import MpvPlayer, StreamDownloader
    from .providers import ProviderManager
    from .storage import HistoryManager

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    name="dion",
    help="🍇 Dion - Stream and download movies and TV shows from your terminal.",
    add_completion=False,
    context_settings=CONTEXT_SETTINGS,
)

# Lazy instances to ensure instant CLI startup
_metadata_client: Optional[CinemetaClient] = None
_provider_manager: Optional[ProviderManager] = None
_player: Optional[MpvPlayer] = None
_downloader: Optional[StreamDownloader] = None
_history_mgr: Optional[HistoryManager] = None


def get_metadata_client() -> CinemetaClient:
    global _metadata_client
    if _metadata_client is None:
        from .metadata import CinemetaClient
        _metadata_client = CinemetaClient()
    return _metadata_client


def get_provider_manager() -> ProviderManager:
    global _provider_manager
    if _provider_manager is None:
        from .providers import ProviderManager
        _provider_manager = ProviderManager()
    return _provider_manager


def get_player() -> MpvPlayer:
    global _player
    if _player is None:
        from .player import MpvPlayer
        _player = MpvPlayer()
    return _player


def get_downloader() -> StreamDownloader:
    global _downloader
    if _downloader is None:
        from .player import StreamDownloader
        _downloader = StreamDownloader()
    return _downloader


def get_history_mgr() -> HistoryManager:
    global _history_mgr
    if _history_mgr is None:
        from .storage import HistoryManager
        _history_mgr = HistoryManager()
    return _history_mgr


def __getattr__(name: str):
    if name == "metadata_client":
        return get_metadata_client()
    if name == "provider_manager":
        return get_provider_manager()
    if name == "player":
        return get_player()
    if name == "downloader":
        return get_downloader()
    if name == "history_mgr":
        return get_history_mgr()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def play_or_download(
    media: MediaItem,
    episode: Optional[EpisodeItem] = None,
    download: bool = False,
    output_dir: Optional[Path] = None,
    auto_select: bool = False,
    sub_delay: Optional[float] = None,
    start_time: Optional[float] = None,
) -> bool:
    """Resolve stream and launch player or downloader. Returns True if played/downloaded successfully."""
    show_media_info(media, episode)

    pm = get_provider_manager()
    history_mgr = get_history_mgr()

    # Determine resume start time if streaming and not explicitly specified
    if start_time is None and not download:
        start_time = history_mgr.get_playback_position(
            media.imdb_id,
            season=episode.season if episode else None,
            episode=episode.episode if episode else None,
        )

    with console.status("[bold cyan]Resolving streaming sources...[/bold cyan]"):
        if episode:
            sources = pm.resolve_episode(
                media.imdb_id,
                episode.season,
                episode.episode,
                media.title,
                released=episode.released,
            )
        else:
            sources = pm.resolve_movie(media.imdb_id, media.title, year=media.year)

    if not sources:
        console.print("[bold red]✕ No active stream sources found for this title.[/bold red]")
        return False

    while sources:
        # Choose source
        source = sources[0] if (auto_select or len(sources) == 1) else select_source(sources)
        if not source:
            return False

        if download:
            status = get_downloader().download(
                source=source,
                media=media,
                episode=episode,
                output_dir=output_dir,
            )
            return status == 0
        else:
            history_mgr.record_watch(
                media=media,
                episode=episode,
                playback_position=start_time or 0.0,
            )
            if sub_delay is not None:
                history_mgr.set_sub_delay(media.imdb_id, sub_delay)

            if start_time and start_time > 10:
                total_s = int(start_time)
                hrs = total_s // 3600
                mins = (total_s % 3600) // 60
                secs = total_s % 60
                time_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"
                console.print(f"[bold green]▶ Resuming playback at {time_str}...[/bold green]")

            exit_code = get_player().play(
                source=source,
                media=media,
                episode=episode,
                start_time=start_time,
                sub_delay=sub_delay,
            )
            # Exit codes 0 (normal finish/quit), 4 (signal/VO quit), -2 (SIGINT), -15 (SIGTERM)
            # all indicate normal user quit or playback completion.
            if exit_code in (0, 4, -2, -15):
                # Update watch history with latest saved position from Lua engine
                history_mgr.record_watch(media=media, episode=episode)
                console.clear()
                return True

            sources = [s for s in sources if s != source]
            if sources:
                console.print(
                    f"\n[yellow]⚠️ Source [{source.server.upper()}] encountered an issue. Trying remaining servers...[/yellow]"
                )
                continue
            return False

    return False


@app.command(context_settings=CONTEXT_SETTINGS)
def main(
    query: Optional[str] = typer.Argument(
        None, help="Movie or TV show title to search for."
    ),
    download: bool = typer.Option(
        False, "-d", "--download", help="Download the media instead of streaming."
    ),
    continue_watching: bool = typer.Option(
        False, "-c", "--continue", help="Continue watching the last title."
    ),
    show_history: bool = typer.Option(
        False, "--history", help="Browse and play from watch history."
    ),
    delete_history: bool = typer.Option(
        False, "-D", "--delete-history", help="Delete watch history."
    ),
    season_arg: Optional[int] = typer.Option(
        None, "-s", "--season", help="Specific season number for TV shows."
    ),
    episode_arg: Optional[int] = typer.Option(
        None, "-e", "--episode", help="Specific episode number for TV shows."
    ),
    best: bool = typer.Option(
        False, "--best", help="Automatically pick the first server without asking."
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "-o", "--output", help="Output directory for downloads."
    ),
    sub_delay: Optional[float] = typer.Option(
        None, "--sub-delay", help="Subtitle delay offset in seconds (e.g. 1.85 or -0.5)."
    ),
):
    """Search, stream, and binge movies and TV shows directly in your terminal."""
    metadata_client = get_metadata_client()
    history_mgr = get_history_mgr()

    # 0. Handle delete history
    if delete_history:
        history_mgr.clear_history()
        console.print("[bold green]✓ Watch history deleted successfully.[/bold green]")
        raise typer.Exit(0)

    # 1. Handle continue watching & history selection
    if continue_watching or show_history:
        items = history_mgr.list_history()
        if not items:
            console.print("[yellow]No watch history found.[/yellow]")
            raise typer.Exit(0)

        choices = [(format_history_choice(i), i) for i in items]

        prompt_title = "Continue watching" if continue_watching else "Select from history"
        selected = prompt_select(prompt_title, choices)
        if not selected:
            return

        media_type = MediaType(selected["media_type"])
        details = metadata_client.get_details(media_type, selected["imdb_id"])
        if not details:
            console.print("[bold red]Failed to retrieve metadata for title.[/bold red]")
            raise typer.Exit(1)

        saved_pos = float(selected.get("position", 0.0))

        if media_type == MediaType.SERIES:
            all_episodes = metadata_client.get_episodes(details.imdb_id)
            s_num = selected.get("season", 1)
            e_num = selected.get("episode", 1)
            ep = next(
                (e for e in all_episodes if e.season == s_num and e.episode == e_num),
                None,
            )
            if not ep and all_episodes:
                ep = all_episodes[0]

            _binge_loop(
                details,
                all_episodes,
                ep,
                download,
                output_dir,
                best,
                sub_delay=sub_delay,
                start_time=saved_pos,
            )
            return
        else:
            play_or_download(
                details,
                None,
                download,
                output_dir,
                best,
                sub_delay=sub_delay,
                start_time=saved_pos,
            )
            return

    # 3. Interactive prompt if no query
    if not query:
        query = typer.prompt("🔍 Search movies & TV shows")
        if not query.strip():
            return

    # 4. Search metadata
    with console.status(f"[bold cyan]Searching for '[bold white]{query}[/bold white]'...[/bold cyan]"):
        results = metadata_client.search(query)

    if not results:
        console.print(f"[bold red]No results found for '{query}'.[/bold red]")
        raise typer.Exit(1)

    console.clear()
    selected_media = select_media(results)
    if not selected_media:
        return
    console.clear()

    # 5. Playback branch: Movie vs TV Show
    if selected_media.media_type == MediaType.MOVIE:
        play_or_download(
            media=selected_media,
            episode=None,
            download=download,
            output_dir=output_dir,
            auto_select=best,
            sub_delay=sub_delay,
        )
    else:
        # TV Series
        with console.status("[bold cyan]Fetching episodes...[/bold cyan]"):
            episodes = metadata_client.get_episodes(selected_media.imdb_id)

        if not episodes:
            console.print("[bold red]Could not find episode data for this series.[/bold red]")
            raise typer.Exit(1)

        # Check if season/episode flags were passed
        if season_arg is not None and episode_arg is not None:
            target_episode = next(
                (
                    ep
                    for ep in episodes
                    if ep.season == season_arg and ep.episode == episode_arg
                ),
                None,
            )
            if not target_episode:
                console.print(
                    f"[bold red]Episode S{season_arg:02d}E{episode_arg:02d} not found.[/bold red]"
                )
                raise typer.Exit(1)
        else:
            chosen_season = season_arg or select_season(episodes)
            if chosen_season is None:
                return
            target_episode = episode_arg or select_episode(episodes, chosen_season)
            if target_episode is None:
                return

        _binge_loop(
            selected_media,
            episodes,
            target_episode,
            download,
            output_dir,
            best,
            sub_delay=sub_delay,
        )


def _binge_loop(
    media: MediaItem,
    all_episodes: list[EpisodeItem],
    current_episode: Optional[EpisodeItem],
    download: bool,
    output_dir: Optional[Path],
    best: bool,
    sub_delay: Optional[float] = None,
    start_time: Optional[float] = None,
) -> None:
    """Handles episode playback and prompts for the next episode when finished."""
    ep = current_episode
    active_sub_delay = sub_delay
    active_start_time = start_time
    while ep:
        success = play_or_download(
            media=media,
            episode=ep,
            download=download,
            output_dir=output_dir,
            auto_select=best,
            sub_delay=active_sub_delay,
            start_time=active_start_time,
        )
        # For subsequent episodes, allow player to read the latest saved delay from storage
        active_sub_delay = None
        active_start_time = None

        if not success or download:
            break

        console.clear()

        # Find the next sequential episode
        current_idx = next(
            (
                i
                for i, item in enumerate(all_episodes)
                if item.id == ep.id
                or (item.season == ep.season and item.episode == ep.episode)
            ),
            -1,
        )
        if current_idx != -1 and current_idx + 1 < len(all_episodes):
            next_ep = all_episodes[current_idx + 1]
            choices = [
                (f"▶ Watch next episode ({next_ep.display_name})", next_ep),
                (f"↺ Replay current episode ({ep.display_name})", "replay"),
                ("↩ Return to episode selection", "select_episode"),
                ("✕ Exit", "exit"),
            ]
            action = prompt_select(f"{ep.display_name}", choices)
            console.clear()
            if not action or action == "exit" or action is False:
                break
            elif isinstance(action, EpisodeItem):
                ep = action
                active_start_time = None
            elif action == "replay":
                active_start_time = 0.0
            elif action == "select_episode":
                # Re-select episode
                chosen_season = select_season(all_episodes)
                console.clear()
                if not chosen_season:
                    break
                ep = select_episode(all_episodes, chosen_season)
                console.clear()
                if not ep:
                    break
            else:
                break
        else:
            console.clear()
            choices = [
                (f"↺ Replay current episode ({ep.display_name})", "replay"),
                ("↩ Return to episode selection", "select_episode"),
                ("✕ Exit", "exit"),
            ]
            action = prompt_select(f"{ep.display_name} (End of series)", choices)
            console.clear()
            if action == "replay":
                active_start_time = 0.0
            elif action == "select_episode":
                chosen_season = select_season(all_episodes)
                console.clear()
                if not chosen_season:
                    break
                ep = select_episode(all_episodes, chosen_season)
                console.clear()
                if not ep:
                    break
            else:
                break


if __name__ == "__main__":
    app()
