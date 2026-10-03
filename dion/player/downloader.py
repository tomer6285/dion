from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from rich.console import Console

from ..metadata.models import EpisodeItem, MediaItem, StreamSource

console = Console()


class StreamDownloader:
    """Downloads media streams using yt-dlp or ffmpeg."""

    def __init__(self):
        self.yt_dlp = shutil.which("yt-dlp")
        self.ffmpeg = shutil.which("ffmpeg")

    def is_available(self) -> bool:
        return bool(self.yt_dlp or self.ffmpeg)

    def download(
        self,
        source: StreamSource,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        output_dir: Optional[Path] = None,
    ) -> int:
        if not self.is_available():
            console.print(
                "[bold red]Error:[/bold red] Neither [bold cyan]yt-dlp[/bold cyan] nor [bold cyan]ffmpeg[/bold cyan] is installed!\n"
                "Install yt-dlp to download streams: [yellow]brew install yt-dlp[/yellow]\n"
            )
            return 1

        if output_dir is None:
            output_dir = Path.home() / "Downloads"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate clean filename
        safe_title = "".join(c for c in media.title if c.isalnum() or c in " ._-").strip()
        if episode:
            filename = f"{safe_title}.S{episode.season:02d}E{episode.episode:02d}.mp4"
        else:
            year_part = f".({media.year})" if media.year else ""
            filename = f"{safe_title}{year_part}.mp4"

        output_path = output_dir / filename
        console.print(f"[bold cyan]Saving to:[/bold cyan] {output_path}")

        if self.yt_dlp:
            cmd = [self.yt_dlp, "-o", str(output_path)]
            for k, v in source.headers.items():
                cmd.extend(["--add-header", f"{k}:{v}"])
            cmd.append(source.url)
        else:
            # Fallback to ffmpeg
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in source.headers.items())
            cmd = [
                self.ffmpeg,
                "-headers",
                headers_str,
                "-i",
                source.url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(output_path),
            ]

        try:
            res = subprocess.run(cmd, check=False)
            return res.returncode
        except KeyboardInterrupt:
            console.print("\n[yellow]Download cancelled.[/yellow]")
            return 130
        except Exception as e:
            console.print(f"[bold red]Download error:[/bold red] {e}")
            return 1

