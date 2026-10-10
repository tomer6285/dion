from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer
from rich.console import Console
from rich.panel import Panel

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
    from .storage import HistoryManager, SettingsManager

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
_settings_mgr: Optional[SettingsManager] = None


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


def get_settings_mgr() -> SettingsManager:
    global _settings_mgr
    if _settings_mgr is None:
        from .storage import SettingsManager
        _settings_mgr = SettingsManager()
    return _settings_mgr


def get_player() -> MpvPlayer:
    global _player
    if _player is None:
        from .player import MpvPlayer
        _player = MpvPlayer(settings_mgr=get_settings_mgr())
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
    if name == "settings_mgr":
        return get_settings_mgr()
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
    video_format: str = "mp4",
    overwrite: bool = False,
    auto_select: bool = False,
    sub_delay: Optional[float] = None,
    start_time: Optional[float] = None,
) -> bool:
    """Resolve stream and launch player or downloader. Returns True if played/downloaded successfully."""
    show_media_info(media, episode)

    pm = get_provider_manager()
    history_mgr = get_history_mgr()
    settings_mgr = get_settings_mgr()

    # Determine resume start time if streaming and not explicitly specified
    if start_time is None and not download:
        start_time = history_mgr.get_playback_position(
            media.imdb_id,
            season=episode.season if episode else None,
            episode=episode.episode if episode else None,
        )

    # Kick off background async subtitle prefetching immediately
    pm.subtitle_resolver.prefetch_subtitles(
        imdb_id=media.imdb_id,
        season=episode.season if episode else None,
        episode=episode.episode if episode else None,
        released=episode.released if episode else media.year,
        preferred_langs=[settings_mgr.sub_lang, "en", "eng", "English"],
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
                video_format=video_format,
                overwrite=overwrite,
                provider_manager=pm,
            )
            return status == 0
        else:
            history_mgr.record_watch(
                media=media,
                episode=episode,
                playback_position=start_time or 0.0,
            )
            if sub_delay is not None:
                history_mgr.set_sub_delay(
                    media.imdb_id,
                    sub_delay,
                    season=episode.season if episode else None,
                    episode=episode.episode if episode else None,
                    server=source.server if source else None,
                )

            if start_time and start_time > 10:
                total_s = int(start_time)
                hrs = total_s // 3600
                mins = (total_s % 3600) // 60
                secs = total_s % 60
                time_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"
                console.print(f"[bold green]▶ Resuming playback at {time_str}...[/bold green]")

            rpc = None
            if settings_mgr.discord_rpc:
                from .discord import DiscordRPC
                rpc = DiscordRPC(client_id=settings_mgr.discord_client_id)
                rpc.start_playback(media=media, episode=episode, start_time=start_time)

            try:
                exit_code = get_player().play(
                    source=source,
                    media=media,
                    episode=episode,
                    start_time=start_time,
                    sub_delay=sub_delay,
                )
            finally:
                if rpc:
                    rpc.stop()

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


def download_season_batch(
    media: MediaItem,
    episodes: list[EpisodeItem],
    output_dir: Optional[Path] = None,
    video_format: str = "mp4",
    max_concurrent: int = 2,
    overwrite: bool = False,
) -> bool:
    """Download multiple episodes concurrently with progress dashboard and summary."""
    from .player.downloader import DownloadTask

    if not episodes:
        console.print("[yellow]No episodes to download.[/yellow]")
        return False

    season_num = episodes[0].season
    settings_mgr = get_settings_mgr()
    if output_dir is None:
        output_dir = settings_mgr.download_dir

    show_media_info(media, episodes[0])
    console.print(
        Panel(
            f"[bold cyan]📦 Batch TV Download:[/bold cyan] {media.title} — Season {season_num}\n"
            f"[dim]• Episodes to download:[/dim] [bold white]{len(episodes)}[/bold white] (E{episodes[0].episode:02d} - E{episodes[-1].episode:02d})\n"
            f"[dim]• Container format:[/dim] [yellow]{video_format.upper()}[/yellow]\n"
            f"[dim]• Concurrent downloads:[/dim] [green]{max_concurrent}[/green]\n"
            f"[dim]• Save directory:[/dim] [blue]{output_dir}[/blue]",
            title="[bold purple] Dion Downloader [/bold purple]",
            border_style="cyan",
            expand=False,
        )
    )

    tasks = [DownloadTask(media=media, episode=ep) for ep in episodes]
    pm = get_provider_manager()
    downloader = get_downloader()

    res = downloader.download_batch(
        tasks=tasks,
        output_dir=output_dir,
        video_format=video_format,
        max_concurrent=max_concurrent,
        overwrite=overwrite,
        provider_manager=pm,
    )

    # Print summary card
    summary_text = (
        f"[bold green]✓ Downloaded:[/bold green] {res.completed}/{res.total}\n"
        f"[dim]• Skipped (Already existed):[/dim] {res.skipped}\n"
        f"[dim]• Failed:[/dim] {res.failed}\n"
        f"[bold cyan]📁 Destination:[/bold cyan] {res.output_dir}"
    )
    console.print(
        Panel(
            summary_text,
            title="[bold green] 📦 Batch Download Finished [/bold green]",
            border_style="green" if res.failed == 0 else "yellow",
            expand=False,
        )
    )
    return res.completed > 0 or res.skipped > 0


def interactive_settings_menu() -> None:
    settings_mgr = get_settings_mgr()
    while True:
        console.clear()

        player_pref = settings_mgr.player
        resolved_path = settings_mgr.resolve_player_executable()
        if player_pref == "auto":
            resolved_name = Path(resolved_path).name if resolved_path else "None found"
            player_desc = f"Auto-detect (Using: {resolved_name})"
        elif resolved_path:
            player_desc = f"{player_pref} ({resolved_path})"
        else:
            player_desc = f"{player_pref} [Not Found]"

        subs_desc = "Enabled (On launch)" if settings_mgr.subtitles_enabled else "Disabled (Off on launch)"
        server_desc = "Enabled (Auto-select first server)" if settings_mgr.auto_select_server else "Disabled (Prompt each time)"
        lang_desc = settings_mgr.sub_lang.upper()
        dl_desc = str(settings_mgr.download_dir)
        fmt_desc = settings_mgr.download_format.upper()
        conc_desc = str(settings_mgr.download_concurrent)
        rpc_status = "Enabled" if settings_mgr.discord_rpc else "Disabled"
        custom_id_tag = f" [ID: {settings_mgr.discord_client_id}]" if settings_mgr.discord_client_id else ""
        rpc_desc = f"{rpc_status}{custom_id_tag}"

        console.print(
            Panel(
                "[bold cyan]⚙️  Dion Settings[/bold cyan]\n"
                "[dim]Configure default video player, launch subtitles, download directory, and playback defaults.[/dim]",
                border_style="cyan",
                expand=False,
            )
        )

        choices = [
            (f"🎬 Default Player: {player_desc}", "player"),
            (f"💬 Subtitles on launch: {subs_desc}", "subtitles"),
            (f"🌐 Preferred Subtitle Language: {lang_desc}", "sub_lang"),
            (f"⚡ Auto-select server: {server_desc}", "auto_server"),
            (f"📂 Default Download Directory: {dl_desc}", "download_dir"),
            (f"📦 Download Container Format: {fmt_desc}", "download_format"),
            (f"🔄 Batch Download Concurrency: {conc_desc}", "download_concurrent"),
            (f"🎮 Discord Rich Presence (RPC): {rpc_desc}", "discord_rpc"),
            ("↺ Reset all settings to defaults", "reset"),
            ("↩ Save & Exit", "exit"),
        ]

        action = prompt_select("Select setting to configure", choices)
        if not action or action == "exit" or action is False:
            console.clear()
            break

        if action == "player":
            console.clear()
            installed = settings_mgr.get_installed_players()
            player_choices = [
                ("Auto-detect (automatically use best available: mpv > iina > vlc)", "auto"),
            ]
            for p in installed:
                player_choices.append((f"{p['name']} ({p['path']})", p["name"]))
            player_choices.append(("Custom binary or full path...", "custom"))
            player_choices.append(("↩ Cancel", "cancel"))

            chosen_player = prompt_select("Choose Default Video Player", player_choices)
            if chosen_player == "custom":
                custom_val = typer.prompt("Enter player binary or path").strip()
                if custom_val:
                    settings_mgr.set("player", custom_val)
                    global _player
                    _player = None
            elif chosen_player and chosen_player != "cancel":
                settings_mgr.set("player", chosen_player)
                _player = None

        elif action == "subtitles":
            console.clear()
            sub_choices = [
                ("❌ Disabled (Off by default on launch - clean screen)", False),
                ("✅ Enabled (On by default on launch)", True),
                ("↩ Cancel", "cancel"),
            ]
            chosen_sub = prompt_select("Subtitles on Launch", sub_choices)
            if chosen_sub in (True, False):
                settings_mgr.set("subtitles_enabled", chosen_sub)

        elif action == "sub_lang":
            console.clear()
            lang_choices = [
                ("English (en)", "en"),
                ("Spanish (es)", "es"),
                ("French (fr)", "fr"),
                ("German (de)", "de"),
                ("Italian (it)", "it"),
                ("Portuguese (pt)", "pt"),
                ("Arabic (ar)", "ar"),
                ("Japanese (ja)", "ja"),
                ("Custom ISO language code...", "custom"),
                ("↩ Cancel", "cancel"),
            ]
            chosen_lang = prompt_select("Select Preferred Subtitle Language", lang_choices)
            if chosen_lang == "custom":
                custom_lang = typer.prompt("Enter 2-letter language code (e.g. en, es, fr)").strip().lower()
                if custom_lang:
                    settings_mgr.set("sub_lang", custom_lang)
            elif chosen_lang and chosen_lang != "cancel":
                settings_mgr.set("sub_lang", chosen_lang)

        elif action == "auto_server":
            console.clear()
            server_choices = [
                ("Prompt me (Choose server each time)", False),
                ("Auto-select (Instantly stream first working server)", True),
                ("↩ Cancel", "cancel"),
            ]
            chosen_server = prompt_select("Auto-select Server", server_choices)
            if chosen_server in (True, False):
                settings_mgr.set("auto_select_server", chosen_server)

        elif action == "download_dir":
            console.clear()
            new_dir = typer.prompt(
                "Enter default download directory",
                default=str(settings_mgr.download_dir),
            ).strip()
            if new_dir:
                p = Path(new_dir).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                settings_mgr.set("download_dir", str(p))

        elif action == "download_format":
            console.clear()
            fmt_choices = [
                ("MP4 (Universal compatibility)", "mp4"),
                ("MKV", "mkv"),
                ("↩ Cancel", "cancel"),
            ]
            chosen_fmt = prompt_select("Select Default Download Container Format", fmt_choices)
            if chosen_fmt and chosen_fmt != "cancel":
                settings_mgr.set("download_format", chosen_fmt)

        elif action == "download_concurrent":
            console.clear()
            c_choices = [
                ("1 episode at a time (Sequential)", 1),
                ("2 episodes simultaneously (Recommended)", 2),
                ("3 episodes simultaneously (Fast connection)", 3),
                ("4 episodes simultaneously", 4),
                ("↩ Cancel", "cancel"),
            ]
            chosen_c = prompt_select("Concurrent Download Workers", c_choices)
            if isinstance(chosen_c, int):
                settings_mgr.set("download_concurrent", chosen_c)

        elif action == "discord_rpc":
            console.clear()
            rpc_choices = [
                ("✅ Enabled (Show status on Discord during playback)", "enable"),
                ("❌ Disabled (Turn off Discord Rich Presence)", "disable"),
                ("🔑 Configure custom Discord Application Client ID...", "set_client_id"),
                ("↩ Cancel", "cancel"),
            ]
            chosen_rpc = prompt_select("Discord Rich Presence Settings", rpc_choices)
            if chosen_rpc == "enable":
                settings_mgr.set("discord_rpc", True)
            elif chosen_rpc == "disable":
                settings_mgr.set("discord_rpc", False)
            elif chosen_rpc == "set_client_id":
                current = settings_mgr.discord_client_id
                console.print(f"[dim]Current Client ID: {current or '(using default)'}[/dim]")
                new_id = typer.prompt("Enter Discord Application Client ID (leave blank for default)", default=current).strip()
                settings_mgr.set("discord_client_id", new_id)

        elif action == "reset":
            console.clear()
            confirm = typer.confirm("Reset all settings to default values?")
            if confirm:
                settings_mgr.reset()
                _player = None


@app.command(context_settings=CONTEXT_SETTINGS)
def main(
    query: Optional[str] = typer.Argument(
        None, help="Movie or TV show title to search for."
    ),
    download: bool = typer.Option(
        False, "-d", "--download", help="Download the media instead of streaming."
    ),
    all_episodes: bool = typer.Option(
        False, "-a", "--all", help="Download all episodes for the season (batch mode)."
    ),
    episode_range: Optional[str] = typer.Option(
        None, "--range", help="Download a range of episodes (e.g. '1-4', '1,2,5', '3-')."
    ),
    video_format: Optional[str] = typer.Option(
        None, "-f", "--format", help="Output container format ('mp4' or 'mkv')."
    ),
    concurrent: Optional[int] = typer.Option(
        None, "-C", "--concurrent", help="Number of concurrent downloads in batch mode."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", "--force", help="Overwrite existing downloaded files."
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
    settings: bool = typer.Option(
        False, "-S", "--settings", help="Open settings menu to configure default player, subtitles, and preferences."
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
    # 0. Handle settings menu
    if settings:
        interactive_settings_menu()
        raise typer.Exit(0)

    metadata_client = get_metadata_client()
    history_mgr = get_history_mgr()
    settings_mgr = get_settings_mgr()

    if all_episodes or episode_range:
        download = True

    if output_dir is None:
        output_dir = settings_mgr.download_dir
    if video_format is None:
        video_format = settings_mgr.download_format
    if concurrent is None:
        concurrent = settings_mgr.download_concurrent
    best = best or settings_mgr.auto_select_server

    # 1. Handle delete history
    if delete_history:
        history_mgr.clear_history()
        console.print("[bold green]✓ Watch history deleted successfully.[/bold green]")
        raise typer.Exit(0)

    # 1. Handle continue watching & history selection
    if continue_watching or show_history:
        console.clear()
        items = history_mgr.list_history()
        if not items:
            console.print("[yellow]No watch history found.[/yellow]")
            raise typer.Exit(0)

        # Advance any completed series episodes to the next episode
        for item in items:
            if item.get("media_type") == MediaType.SERIES.value:
                imdb_id = item.get("imdb_id")
                s = item.get("season")
                e = item.get("episode")
                if s is not None and e is not None and imdb_id:
                    if history_mgr.is_episode_completed(imdb_id, s, e):
                        try:
                            all_eps = metadata_client.get_episodes(imdb_id)
                            curr_idx = next(
                                (
                                    i
                                    for i, ep_item in enumerate(all_eps)
                                    if ep_item.season == s and ep_item.episode == e
                                ),
                                -1,
                            )
                            if curr_idx != -1 and curr_idx + 1 < len(all_eps):
                                next_ep = all_eps[curr_idx + 1]
                                item["season"] = next_ep.season
                                item["episode"] = next_ep.episode
                                item["episode_title"] = next_ep.title
                                item["position"] = 0.0
                                if next_ep.runtime:
                                    from .storage.history import parse_runtime
                                    ep_dur = parse_runtime(next_ep.runtime)
                                    if ep_dur:
                                        item["duration"] = ep_dur
                                history_mgr.record_watch(
                                    media=MediaItem(
                                        id=imdb_id,
                                        imdb_id=imdb_id,
                                        title=item.get("title", ""),
                                        media_type=MediaType.SERIES,
                                        year=item.get("year"),
                                    ),
                                    episode=next_ep,
                                    playback_position=0.0,
                                    duration=item.get("duration"),
                                )
                        except Exception:
                            pass

        # Populate missing duration data for history items
        history_mgr.populate_missing_durations(items, metadata_client)

        def on_history_info(item: Dict[str, Any]) -> None:
            imdb_id = item.get("imdb_id")
            if not imdb_id:
                return
            m_type = MediaType(item.get("media_type", "movie"))
            s_num = item.get("season")
            e_num = item.get("episode")

            with console.status("[bold cyan]Fetching information...[/bold cyan]"):
                details = metadata_client.get_details(m_type, imdb_id)
                if not details:
                    details = MediaItem(
                        id=imdb_id,
                        imdb_id=imdb_id,
                        title=item.get("title", "Unknown"),
                        media_type=m_type,
                        year=item.get("year"),
                    )

                ep = None
                if m_type == MediaType.SERIES and s_num is not None and e_num is not None:
                    eps = metadata_client.get_episodes(imdb_id)
                    ep = next(
                        (x for x in eps if x.season == s_num and x.episode == e_num),
                        None,
                    )
                    if not ep:
                        ep = EpisodeItem(
                            id=f"{imdb_id}:{s_num}:{e_num}",
                            season=s_num,
                            episode=e_num,
                            title=item.get("episode_title") or f"Episode {e_num}",
                        )

            show_media_info(details, ep)

        choices = [(format_history_choice(i), i) for i in items]

        prompt_title = "Continue watching" if continue_watching else "Select from history"
        selected = prompt_select(prompt_title, choices, on_info=on_history_info)
        console.clear()
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

            if ep and history_mgr.is_episode_completed(details.imdb_id, ep.season, ep.episode):
                curr_idx = next(
                    (
                        i
                        for i, item in enumerate(all_episodes)
                        if item.id == ep.id
                        or (item.season == ep.season and item.episode == ep.episode)
                    ),
                    -1,
                )
                if curr_idx != -1 and curr_idx + 1 < len(all_episodes):
                    ep = all_episodes[curr_idx + 1]
                    saved_pos = 0.0

            _binge_loop(
                details,
                all_episodes,
                ep,
                download,
                output_dir,
                best,
                video_format=video_format,
                overwrite=overwrite,
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
                video_format=video_format,
                overwrite=overwrite,
                auto_select=best,
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
            video_format=video_format,
            overwrite=overwrite,
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

        # Determine target season
        chosen_season = season_arg
        if chosen_season is None:
            chosen_season = select_season(episodes)
            if chosen_season is None:
                return

        season_episodes = [ep for ep in episodes if ep.season == chosen_season]
        if not season_episodes:
            console.print(f"[bold red]No episodes found for Season {chosen_season}.[/bold red]")
            raise typer.Exit(1)

        available_ep_nums = [ep.episode for ep in season_episodes]

        # DOWNLOAD MODE ROUTING
        if download:
            from .player.downloader import parse_episode_range

            target_batch: Optional[list[EpisodeItem]] = None

            if episode_arg is not None and not all_episodes and not episode_range:
                # Single episode download via -e
                target_episode = next(
                    (ep for ep in season_episodes if ep.episode == episode_arg),
                    None,
                )
                if not target_episode:
                    console.print(
                        f"[bold red]Episode S{chosen_season:02d}E{episode_arg:02d} not found.[/bold red]"
                    )
                    raise typer.Exit(1)
                play_or_download(
                    media=selected_media,
                    episode=target_episode,
                    download=True,
                    output_dir=output_dir,
                    video_format=video_format,
                    overwrite=overwrite,
                    auto_select=best,
                    sub_delay=sub_delay,
                )
                return

            elif all_episodes:
                target_batch = season_episodes

            elif episode_range:
                matched_nums = parse_episode_range(episode_range, available_ep_nums)
                if not matched_nums:
                    console.print(
                        f"[bold red]No episodes in Season {chosen_season} match range '{episode_range}'. Available: 1-{len(season_episodes)}[/bold red]"
                    )
                    raise typer.Exit(1)
                target_batch = [ep for ep in season_episodes if ep.episode in matched_nums]

            else:
                # Interactive episode/batch selection
                dl_choices: list[tuple[str, Any]] = [
                    (f"📥 Download Entire Season {chosen_season} ({len(season_episodes)} episodes)", "all"),
                    ("📥 Download Episode Range (e.g. 1-4)...", "range"),
                ]
                for ep in season_episodes:
                    label = f"E{ep.episode:02d}: {ep.title}"
                    if ep.released:
                        label += f" ({ep.released[:10]})"
                    dl_choices.append((label, ep))

                action = prompt_select(f"Download Season {chosen_season}", dl_choices)
                if not action:
                    return
                if action == "all":
                    target_batch = season_episodes
                elif action == "range":
                    r_str = typer.prompt("Enter episode range (e.g. 1-4, 1,3,5)").strip()
                    matched_nums = parse_episode_range(r_str, available_ep_nums)
                    if not matched_nums:
                        console.print(f"[bold red]No episodes match range '{r_str}'.[/bold red]")
                        return
                    target_batch = [ep for ep in season_episodes if ep.episode in matched_nums]
                elif isinstance(action, EpisodeItem):
                    play_or_download(
                        media=selected_media,
                        episode=action,
                        download=True,
                        output_dir=output_dir,
                        video_format=video_format,
                        overwrite=overwrite,
                        auto_select=best,
                        sub_delay=sub_delay,
                    )
                    return

            if target_batch:
                download_season_batch(
                    media=selected_media,
                    episodes=target_batch,
                    output_dir=output_dir,
                    video_format=video_format,
                    max_concurrent=concurrent,
                    overwrite=overwrite,
                )
                return

        # STREAMING PLAYBACK MODE
        if episode_arg is not None:
            target_episode = next(
                (ep for ep in season_episodes if ep.episode == episode_arg),
                None,
            )
            if not target_episode:
                console.print(
                    f"[bold red]Episode S{chosen_season:02d}E{episode_arg:02d} not found.[/bold red]"
                )
                raise typer.Exit(1)
        else:
            target_episode = select_episode(episodes, chosen_season, selected_media)
            if target_episode is None:
                return

        _binge_loop(
            selected_media,
            episodes,
            target_episode,
            download,
            output_dir,
            best,
            video_format=video_format,
            overwrite=overwrite,
            sub_delay=sub_delay,
        )


def _binge_loop(
    media: MediaItem,
    all_episodes: list[EpisodeItem],
    current_episode: Optional[EpisodeItem],
    download: bool,
    output_dir: Optional[Path],
    best: bool,
    video_format: str = "mp4",
    overwrite: bool = False,
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
            video_format=video_format,
            overwrite=overwrite,
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
            history_mgr = get_history_mgr()
            if ep and history_mgr.is_episode_completed(media.imdb_id, ep.season, ep.episode):
                history_mgr.record_watch(
                    media=media,
                    episode=next_ep,
                    playback_position=0.0,
                )

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
                history_mgr.record_watch(
                    media=media,
                    episode=ep,
                    playback_position=0.0,
                )
            elif action == "select_episode":
                # Re-select episode
                chosen_season = select_season(all_episodes)
                console.clear()
                if not chosen_season:
                    break
                ep = select_episode(all_episodes, chosen_season, media)
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
                ep = select_episode(all_episodes, chosen_season, media)
                console.clear()
                if not ep:
                    break
            else:
                break


if __name__ == "__main__":
    app()
