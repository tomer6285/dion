from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import gzip
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple
import zipfile

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)

from ..metadata.models import EpisodeItem, MediaItem, MediaType, StreamSource, SubtitleTrack

if TYPE_CHECKING:
    from ..providers import ProviderManager

console = Console()

# ISO 639-1 / common name to ISO 639-2 (3-letter) mapping for FFmpeg
ISO_639_MAP: Dict[str, str] = {
    "en": "eng", "eng": "eng", "english": "eng",
    "es": "spa", "spa": "spa", "spanish": "spa",
    "fr": "fra", "fra": "fra", "fre": "fra", "french": "fra",
    "de": "deu", "deu": "deu", "ger": "deu", "german": "deu",
    "it": "ita", "ita": "ita", "italian": "ita",
    "pt": "por", "por": "por", "portuguese": "por",
    "ar": "ara", "ara": "ara", "arabic": "ara",
    "ja": "jpn", "jpn": "jpn", "japanese": "jpn",
    "ko": "kor", "kor": "kor", "korean": "kor",
    "zh": "zho", "zho": "zho", "chi": "zho", "chinese": "zho",
    "ru": "rus", "rus": "rus", "russian": "rus",
    "nl": "nld", "nld": "nld", "dut": "nld", "dutch": "nld",
    "pl": "pol", "pol": "pol", "polish": "pol",
    "tr": "tur", "tur": "tur", "turkish": "tur",
    "he": "heb", "heb": "heb", "hebrew": "heb",
    "hi": "hin", "hin": "hin", "hindi": "hin",
    "sv": "swe", "swe": "swe", "swedish": "swe",
    "no": "nor", "nor": "nor", "norwegian": "nor",
    "da": "dan", "dan": "dan", "danish": "dan",
    "fi": "fin", "fin": "fin", "finnish": "fin",
    "el": "ell", "ell": "ell", "gre": "ell", "greek": "ell",
    "cs": "ces", "ces": "ces", "cze": "ces", "czech": "ces",
    "ro": "ron", "ron": "ron", "rum": "ron", "romanian": "ron",
    "hu": "hun", "hun": "hun", "hungarian": "hun",
    "uk": "ukr", "ukr": "ukr", "ukrainian": "ukr",
    "id": "ind", "ind": "ind", "indonesian": "ind",
    "vi": "vie", "vie": "vie", "vietnamese": "vie",
    "th": "tha", "tha": "tha", "thai": "tha",
}


def normalize_iso_639(lang: str) -> str:
    """Normalize language code or name to ISO 639-2 3-letter code for FFmpeg metadata."""
    clean = (lang or "").strip().lower()
    if clean in ISO_639_MAP:
        return ISO_639_MAP[clean]
    if len(clean) == 3 and clean.isalpha():
        return clean
    if len(clean) == 2 and clean.isalpha():
        return clean
    return "und"


def parse_episode_range(range_str: str, available_episodes: List[int]) -> List[int]:
    """
    Parse an episode range specification into a list of episode numbers.
    Supports formats like:
      - '1-4' -> [1, 2, 3, 4]
      - '1,2,5' -> [1, 2, 5]
      - '1-3,5,8-10' -> [1, 2, 3, 5, 8, 9, 10]
      - '3-' -> all available episodes >= 3
      - '-4' -> all available episodes <= 4
      - 'all' or '*' -> all available episodes
      - '5' -> [5]
    """
    clean = range_str.strip().lower()
    if not clean or clean in ("all", "*"):
        return sorted(list(set(available_episodes)))

    available_set = set(available_episodes)
    min_avail = min(available_episodes, default=1)
    max_avail = max(available_episodes, default=1)
    selected: Set[int] = set()

    for part in clean.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            chunks = part.split("-", 1)
            start_str, end_str = chunks[0].strip(), chunks[1].strip()
            start_val = int(start_str) if start_str.isdigit() else min_avail
            end_val = int(end_str) if end_str.isdigit() else max_avail
            for ep in available_episodes:
                if start_val <= ep <= end_val:
                    selected.add(ep)
        elif part.isdigit():
            val = int(part)
            if val in available_set:
                selected.add(val)

    return sorted(list(selected))


@dataclass
class DownloadTask:
    """Represents a single media item to be downloaded."""
    media: MediaItem
    episode: Optional[EpisodeItem] = None
    source: Optional[StreamSource] = None
    output_filename: Optional[str] = None


@dataclass
class BatchDownloadResult:
    """Summary of batch download operation."""
    total: int
    completed: int
    failed: int
    skipped: int
    output_dir: Path
    files: List[Path] = field(default_factory=list)


class StreamDownloader:
    """Downloads media streams with soft-subtitle muxing, metadata tagging, and multi-progress dashboards."""

    def __init__(self):
        self.yt_dlp = shutil.which("yt-dlp")
        self.ffmpeg = shutil.which("ffmpeg")
        self._active_procs: Set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()

    def is_available(self) -> bool:
        return bool(self.yt_dlp or self.ffmpeg)

    def _register_proc(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._active_procs.add(proc)

    def _unregister_proc(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._active_procs.discard(proc)

    def abort_all(self) -> None:
        """Terminate all active child download and muxing processes."""
        with self._proc_lock:
            for proc in list(self._active_procs):
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._active_procs.clear()

    # -------------------------------------------------------------------------
    # Path & Filename Helpers
    # -------------------------------------------------------------------------

    def get_output_filename(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        video_format: str = "mp4",
    ) -> str:
        safe_title = "".join(c for c in media.title if c.isalnum() or c in " ._-").strip()
        ext = video_format.lstrip(".").lower()
        if episode:
            return f"{safe_title}.S{episode.season:02d}E{episode.episode:02d}.{ext}"
        else:
            year_part = f".({media.year})" if media.year else ""
            return f"{safe_title}{year_part}.{ext}"

    def get_output_path(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        output_dir: Optional[Path] = None,
        video_format: str = "mp4",
    ) -> Path:
        if output_dir is None:
            output_dir = Path.home() / "Downloads"
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = self.get_output_filename(media, episode, video_format)
        return output_dir / filename

    # -------------------------------------------------------------------------
    # Subtitle & Artwork Preparation
    # -------------------------------------------------------------------------

    def _prepare_subtitles(
        self,
        source: StreamSource,
        media: MediaItem,
        episode: Optional[EpisodeItem],
        temp_dir: Path,
        provider_manager: Optional[ProviderManager] = None,
    ) -> List[Tuple[Path, str, str]]:
        """
        Download and validate subtitle tracks, returning (local_file_path, iso_lang, label).
        """
        raw_tracks = list(source.subtitles)

        # If no subtitles on source, attempt retrieval via subtitle resolver
        if not raw_tracks and provider_manager:
            try:
                found = provider_manager.subtitle_resolver.get_subtitles(
                    imdb_id=media.imdb_id,
                    season=episode.season if episode else None,
                    episode=episode.episode if episode else None,
                    released=episode.released if episode else media.year,
                )
                if found:
                    raw_tracks.extend(found)
            except Exception:
                pass

        prepared: List[Tuple[Path, str, str]] = []
        seen_langs: Set[str] = set()

        for idx, track in enumerate(raw_tracks):
            iso_lang = normalize_iso_639(track.lang)
            # Limit to 4 subtitle tracks to prevent container bloat
            if len(prepared) >= 4:
                break

            url = track.url
            local_sub_path: Optional[Path] = None

            if Path(url).is_file():
                local_sub_path = Path(url)
            elif url.startswith("http://") or url.startswith("https://"):
                try:
                    resp = httpx.get(url, timeout=5.0, follow_redirects=True)
                    if resp.status_code == 200 and resp.content:
                        content_bytes = resp.content
                        if content_bytes.startswith(b"\x1f\x8b"):
                            content_bytes = gzip.decompress(content_bytes)
                        elif content_bytes.startswith(b"PK\x03\x04"):
                            with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                                for name in zf.namelist():
                                    if name.lower().endswith((".srt", ".vtt")):
                                        content_bytes = zf.read(name)
                                        break

                        text = content_bytes.decode("utf-8", errors="replace")
                        if "-->" in text or "WEBVTT" in text:
                            target_file = temp_dir / f"sub_{idx}_{iso_lang}.srt"
                            target_file.write_text(text, encoding="utf-8")
                            local_sub_path = target_file
                except Exception:
                    local_sub_path = None

            if local_sub_path and local_sub_path.exists() and local_sub_path.stat().st_size > 30:
                label = track.label or f"Subtitles ({iso_lang.upper()})"
                prepared.append((local_sub_path, iso_lang, label))
                seen_langs.add(iso_lang)

        return prepared

    def _prepare_cover_artwork(
        self,
        poster_url: Optional[str],
        temp_dir: Path,
    ) -> Optional[Path]:
        """Download poster artwork for container metadata tagging."""
        if not poster_url or not (poster_url.startswith("http://") or poster_url.startswith("https://")):
            return None
        try:
            resp = httpx.get(poster_url, timeout=5.0, follow_redirects=True)
            if resp.status_code == 200 and len(resp.content) > 500:
                cover_path = temp_dir / "cover.jpg"
                cover_path.write_bytes(resp.content)
                return cover_path
        except Exception:
            pass
        return None

    # -------------------------------------------------------------------------
    # Progress Parsing
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_progress_line(line: str) -> Optional[Tuple[float, str, str]]:
        """
        Parses output from yt-dlp or ffmpeg into (percent: float, speed_str: str, eta_str: str).
        """
        line = line.strip()
        if not line:
            return None

        # Template format: download:123/456|45.2%|5.20MiB/s|00:15
        if line.startswith("download:"):
            parts = line[len("download:"):].split("|")
            if len(parts) >= 4:
                pct_str = parts[1].replace("%", "").strip()
                speed_str = parts[2].strip()
                eta_str = parts[3].strip()
                try:
                    pct = float(pct_str)
                    return (pct, speed_str, eta_str)
                except ValueError:
                    pass

        # Standard yt-dlp: [download]  45.2% of ~ 150.00MiB at  5.20MiB/s ETA 00:15
        m = re.search(
            r"\[download\]\s+([0-9.]+)%\s+of\s+(?:~?\s*)?([0-9.]+[A-Za-z]+)\s+at\s+([0-9.]+[A-Za-z]+/s)(?:\s+ETA\s+(\S+))?",
            line,
        )
        if m:
            try:
                pct = float(m.group(1))
                speed = m.group(3)
                eta = m.group(4) or ""
                return (pct, speed, eta)
            except ValueError:
                pass

        # yt-dlp fragment progress: [download] 12 of 150 fragments (8.0%) at 4.50MiB/s ETA 00:30
        m2 = re.search(
            r"\[download\]\s+\d+\s+of\s+\d+\s+fragments\s+\(([0-9.]+)%\)\s+at\s+([0-9.]+[A-Za-z]+/s)(?:\s+ETA\s+(\S+))?",
            line,
        )
        if m2:
            try:
                pct = float(m2.group(1))
                speed = m2.group(2)
                eta = m2.group(3) or ""
                return (pct, speed, eta)
            except ValueError:
                pass

        return None

    # -------------------------------------------------------------------------
    # Core Stream Download
    # -------------------------------------------------------------------------

    def _download_stream_raw(
        self,
        source: StreamSource,
        temp_video_path: Path,
        on_progress: Optional[Callable[[float, str, str], None]] = None,
    ) -> bool:
        """
        Download raw media stream to temp_video_path using yt-dlp or ffmpeg,
        streaming progress line-by-line.
        """
        if self.yt_dlp:
            cmd = [
                self.yt_dlp,
                "--newline",
                "--no-part",
                "--no-warnings",
                "--progress-template",
                "download:%(progress.downloaded_bytes)s/%(progress.total_bytes)s|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
                "-o",
                str(temp_video_path),
            ]
            for k, v in source.headers.items():
                cmd.extend(["--add-header", f"{k}:{v}"])
            cmd.append(source.url)
        elif self.ffmpeg:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in source.headers.items())
            cmd = [
                self.ffmpeg,
                "-y",
                "-headers",
                headers_str,
                "-i",
                source.url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-progress",
                "pipe:1",
                str(temp_video_path),
            ]
        else:
            return False

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._register_proc(proc)

            last_pct = 0.0
            last_speed = ""
            last_eta = ""

            if proc.stdout:
                for line in proc.stdout:
                    parsed = self._parse_progress_line(line)
                    if parsed and on_progress:
                        pct, speed, eta = parsed
                        last_pct = pct
                        last_speed = speed
                        last_eta = eta
                        on_progress(pct, speed, eta)
                    elif self.ffmpeg and line.startswith("total_size="):
                        # Approximate progress for ffmpeg
                        try:
                            size_bytes = int(line.split("=")[1].strip())
                            size_mb = size_bytes / (1024 * 1024)
                            if on_progress:
                                on_progress(min(99.0, last_pct + 1.0), f"{size_mb:.1f}MB", "")
                        except Exception:
                            pass

            proc.wait()
            self._unregister_proc(proc)
            return proc.returncode == 0 and temp_video_path.exists() and temp_video_path.stat().st_size > 1024
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Subtitle Soft-Muxing & Tagging
    # -------------------------------------------------------------------------

    def _mux_subtitles_and_metadata(
        self,
        input_video: Path,
        output_path: Path,
        media: MediaItem,
        episode: Optional[EpisodeItem],
        subtitles: List[Tuple[Path, str, str]],
        cover_art: Optional[Path],
        video_format: str = "mp4",
    ) -> bool:
        """
        Soft-mux subtitles and inject MP4/MKV metadata tags using FFmpeg.
        """
        if not self.ffmpeg:
            # If ffmpeg is not available, move raw video directly
            try:
                shutil.move(str(input_video), str(output_path))
                return True
            except Exception:
                return False

        fmt = video_format.lstrip(".").lower()
        cmd = [self.ffmpeg, "-y", "-i", str(input_video)]

        # Add subtitle inputs
        for sub_path, _, _ in subtitles:
            cmd.extend(["-i", str(sub_path)])

        # If MP4 and cover artwork available, add as video input
        has_cover = cover_art and cover_art.exists()
        if has_cover and fmt == "mp4":
            cmd.extend(["-i", str(cover_art)])

        # Stream mapping
        cmd.extend(["-map", "0:v:0", "-map", "0:a?"])
        for idx in range(len(subtitles)):
            cmd.extend(["-map", f"{idx + 1}:0"])

        if has_cover and fmt == "mp4":
            cover_idx = len(subtitles) + 1
            cmd.extend(["-map", f"{cover_idx}:0"])

        # Codecs
        cmd.extend(["-c:v", "copy", "-c:a", "copy"])
        if subtitles:
            sub_codec = "mov_text" if fmt == "mp4" else "srt"
            cmd.extend(["-c:s", sub_codec])

        # Cover attachment
        if has_cover:
            if fmt == "mp4":
                cmd.extend(["-disposition:v:1", "attached_pic"])
            elif fmt == "mkv":
                cmd.extend(["-attach", str(cover_art), "-metadata:s:t", "mimetype=image/jpeg"])

        # Subtitle stream metadata
        for s_idx, (_, lang, label) in enumerate(subtitles):
            cmd.extend([
                f"-metadata:s:s:{s_idx}", f"language={lang}",
                f"-metadata:s:s:{s_idx}", f"title={label}",
                f"-metadata:s:s:{s_idx}", f"handler_name={label}",
            ])

        # Metadata Tags for Apple TV / Jellyfin / Media Centers
        if episode:
            title_str = f"S{episode.season:02d}E{episode.episode:02d} - {episode.title}"
            cmd.extend([
                "-metadata", f"title={title_str}",
                "-metadata", f"show={media.title}",
                "-metadata", f"season_number={episode.season}",
                "-metadata", f"episode_sort={episode.episode}",
                "-metadata", f"episode_id=S{episode.season:02d}E{episode.episode:02d}",
                "-metadata", "media_type=10",
            ])
            year_val = (episode.released[:4] if episode.released else None) or media.year
            if year_val:
                cmd.extend(["-metadata", f"date={year_val}"])
            synopsis = episode.overview or media.overview
            if synopsis:
                cmd.extend([
                    "-metadata", f"description={synopsis}",
                    "-metadata", f"synopsis={synopsis}",
                    "-metadata", f"comment={synopsis}",
                ])
        else:
            cmd.extend([
                "-metadata", f"title={media.title}",
                "-metadata", "media_type=9",
            ])
            if media.year:
                cmd.extend(["-metadata", f"date={media.year}"])
            if media.overview:
                cmd.extend([
                    "-metadata", f"description={media.overview}",
                    "-metadata", f"synopsis={media.overview}",
                    "-metadata", f"comment={media.overview}",
                ])

        cmd.append(str(output_path))

        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
                return True
        except Exception:
            pass

        # Fallback: copy input_video directly if muxing failed
        try:
            shutil.copy2(input_video, output_path)
            return True
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Single Download Entrypoint
    # -------------------------------------------------------------------------

    def download(
        self,
        source: StreamSource,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        output_dir: Optional[Path] = None,
        video_format: str = "mp4",
        overwrite: bool = False,
        provider_manager: Optional[ProviderManager] = None,
    ) -> int:
        """
        Download a single media item with soft subtitles, metadata tags, and live progress bar.
        """
        if not self.is_available():
            console.print(
                "[bold red]Error:[/bold red] Neither [bold cyan]yt-dlp[/bold cyan] nor [bold cyan]ffmpeg[/bold cyan] is installed!\n"
                "Install yt-dlp to download streams: [yellow]brew install yt-dlp[/yellow]\n"
            )
            return 1

        output_path = self.get_output_path(media, episode, output_dir, video_format)
        if not overwrite and output_path.exists() and output_path.stat().st_size > 1024 * 1024:
            console.print(f"[bold green]✓ File already exists:[/bold green] {output_path}")
            return 0

        item_label = f"S{episode.season:02d}E{episode.episode:02d} - {episode.title}" if episode else media.title
        console.print(f"[bold cyan]Saving to:[/bold cyan] {output_path}")

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.fields[label]:<28}"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            TextColumn("[bold green]{task.fields[speed]:>10}"),
            TextColumn("[yellow]{task.fields[eta]:>8}"),
            TextColumn("{task.fields[status]}"),
            console=console,
        )

        temp_dir = output_path.parent / f".tmp_{output_path.stem}_{int(time.time())}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_video_path = temp_dir / f"raw_stream.{video_format.lstrip('.')}"

        try:
            with progress:
                tid = progress.add_task(
                    "",
                    label=item_label[:28],
                    total=100,
                    completed=0,
                    speed="",
                    eta="",
                    status="[blue]Downloading[/blue]",
                )

                def on_progress(pct: float, speed: str, eta: str):
                    progress.update(tid, completed=pct, speed=speed, eta=eta, status="[blue]Downloading[/blue]")

                # Step 1: Download raw stream
                dl_ok = self._download_stream_raw(source, temp_video_path, on_progress=on_progress)
                if not dl_ok:
                    progress.update(tid, status="[bold red]✕ Download failed[/bold red]")
                    return 1

                # Step 2: Prepare subtitles & artwork
                progress.update(tid, status="[magenta]Muxing subs & metadata[/magenta]", speed="", eta="")
                subtitles = self._prepare_subtitles(source, media, episode, temp_dir, provider_manager)
                cover_art = self._prepare_cover_artwork(media.poster, temp_dir)

                # Step 3: Mux subtitles & tags
                mux_ok = self._mux_subtitles_and_metadata(
                    temp_video_path, output_path, media, episode, subtitles, cover_art, video_format
                )

                if mux_ok:
                    progress.update(tid, completed=100, status="[bold green]✓ Done[/bold green]")
                    return 0
                else:
                    progress.update(tid, status="[bold red]✕ Mux failed[/bold red]")
                    return 1
        except KeyboardInterrupt:
            self.abort_all()
            console.print("\n[yellow]Download cancelled by user.[/yellow]")
            return 130
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Batch Season Download Dashboard
    # -------------------------------------------------------------------------

    def download_batch(
        self,
        tasks: List[DownloadTask],
        output_dir: Optional[Path] = None,
        video_format: str = "mp4",
        max_concurrent: int = 2,
        overwrite: bool = False,
        provider_manager: Optional[ProviderManager] = None,
    ) -> BatchDownloadResult:
        """
        Batch downloads multiple episodes concurrently using a Rich multi-progress dashboard.
        """
        if output_dir is None:
            output_dir = Path.home() / "Downloads"
        output_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_available():
            console.print(
                "[bold red]Error:[/bold red] Neither [bold cyan]yt-dlp[/bold cyan] nor [bold cyan]ffmpeg[/bold cyan] is installed!\n"
            )
            return BatchDownloadResult(
                total=len(tasks), completed=0, failed=len(tasks), skipped=0, output_dir=output_dir
            )

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.fields[label]:<28}"),
            BarColumn(bar_width=22),
            TaskProgressColumn(),
            TextColumn("[bold green]{task.fields[speed]:>10}"),
            TextColumn("[yellow]{task.fields[eta]:>8}"),
            TextColumn("{task.fields[status]}"),
            console=console,
        )

        task_id_map: Dict[int, TaskID] = {}
        completed_count = 0
        skipped_count = 0
        failed_count = 0
        completed_files: List[Path] = []
        lock = threading.Lock()

        # Map each task to an item index
        items_to_download: List[Tuple[int, DownloadTask, Path]] = []

        with progress:
            for idx, task in enumerate(tasks):
                ep = task.episode
                label = f"S{ep.season:02d}E{ep.episode:02d} - {ep.title}" if ep else task.media.title
                out_path = self.get_output_path(task.media, ep, output_dir, video_format)

                if not overwrite and out_path.exists() and out_path.stat().st_size > 1024 * 1024:
                    tid = progress.add_task(
                        "",
                        label=label[:28],
                        total=100,
                        completed=100,
                        speed="",
                        eta="",
                        status="[bold green]✓ Already exists[/bold green]",
                    )
                    task_id_map[idx] = tid
                    skipped_count += 1
                    completed_files.append(out_path)
                else:
                    tid = progress.add_task(
                        "",
                        label=label[:28],
                        total=100,
                        completed=0,
                        speed="",
                        eta="",
                        status="[dim]Queued[/dim]",
                    )
                    task_id_map[idx] = tid
                    items_to_download.append((idx, task, out_path))

            def process_item(item_info: Tuple[int, DownloadTask, Path]) -> bool:
                nonlocal completed_count, failed_count
                idx, task, out_path = item_info
                tid = task_id_map[idx]
                media = task.media
                ep = task.episode

                # Step 1: Resolve stream source just-in-time if not provided
                source = task.source
                if not source and provider_manager:
                    progress.update(tid, status="[cyan]Resolving source...[/cyan]")
                    try:
                        if ep:
                            sources = provider_manager.resolve_episode(
                                media.imdb_id,
                                ep.season,
                                ep.episode,
                                media.title,
                                released=ep.released,
                            )
                        else:
                            sources = provider_manager.resolve_movie(
                                media.imdb_id, media.title, year=media.year
                            )
                        if sources:
                            source = sources[0]
                    except Exception:
                        source = None

                if not source:
                    progress.update(tid, status="[bold red]✕ No source found[/bold red]")
                    with lock:
                        failed_count += 1
                    return False

                # Step 2: Download stream
                temp_dir = out_path.parent / f".tmp_{out_path.stem}_{int(time.time())}"
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp_video_path = temp_dir / f"raw_stream.{video_format.lstrip('.')}"

                try:
                    def on_progress(pct: float, speed: str, eta: str):
                        progress.update(tid, completed=pct, speed=speed, eta=eta, status="[blue]Downloading[/blue]")

                    dl_ok = self._download_stream_raw(source, temp_video_path, on_progress=on_progress)
                    if not dl_ok:
                        progress.update(tid, status="[bold red]✕ Download error[/bold red]")
                        with lock:
                            failed_count += 1
                        return False

                    # Step 3: Prepare subtitles and artwork
                    progress.update(tid, status="[magenta]Muxing tags...[/magenta]", speed="", eta="")
                    subtitles = self._prepare_subtitles(source, media, ep, temp_dir, provider_manager)
                    cover_art = self._prepare_cover_artwork(media.poster, temp_dir)

                    # Step 4: Mux subtitles & metadata
                    mux_ok = self._mux_subtitles_and_metadata(
                        temp_video_path, out_path, media, ep, subtitles, cover_art, video_format
                    )

                    if mux_ok:
                        progress.update(tid, completed=100, speed="", eta="", status="[bold green]✓ Done[/bold green]")
                        with lock:
                            completed_count += 1
                            completed_files.append(out_path)
                        return True
                    else:
                        progress.update(tid, status="[bold red]✕ Mux error[/bold red]")
                        with lock:
                            failed_count += 1
                        return False
                except Exception:
                    progress.update(tid, status="[bold red]✕ Failed[/bold red]")
                    with lock:
                        failed_count += 1
                    return False
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)

            if items_to_download:
                worker_count = min(max_concurrent, len(items_to_download))
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                    try:
                        futures = [executor.submit(process_item, item) for item in items_to_download]
                        concurrent.futures.wait(futures)
                    except KeyboardInterrupt:
                        self.abort_all()
                        console.print("\n[yellow]Batch download cancelled by user.[/yellow]")

        return BatchDownloadResult(
            total=len(tasks),
            completed=completed_count,
            failed=failed_count,
            skipped=skipped_count,
            output_dir=output_dir,
            files=completed_files,
        )
