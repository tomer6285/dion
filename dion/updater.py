"""Self-updater module for Dion supporting pipx and standalone installations."""
from __future__ import annotations

from enum import Enum
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import zipfile

import httpx
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from . import __version__
from .console import console

GITHUB_REPO = "tomer6285/dion"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


class InstallType(str, Enum):
    STANDALONE = "standalone"
    PIPX = "pipx"
    PIP = "pip"
    SOURCE = "source"
    UNKNOWN = "unknown"


def parse_version(v: str) -> Tuple[int, ...]:
    """Parse a version string (e.g. 'v0.1.0' or '0.2.1') into an integer tuple."""
    v = v.lstrip("v").strip()
    parts: List[int] = []
    for chunk in re.split(r"[.\-+]", v):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (0,)


def detect_install_type() -> InstallType:
    """Detect how Dion is currently installed and executed."""
    # 1. Standalone PyInstaller frozen binary or wrapper
    if getattr(sys, "frozen", False) or os.environ.get("DION_STANDALONE_BINARY"):
        return InstallType.STANDALONE

    # 2. Pipx installation:
    # Pipx creates a virtual environment containing pipx_metadata.json
    pipx_meta = Path(sys.prefix) / "pipx_metadata.json"
    if pipx_meta.is_file():
        return InstallType.PIPX
    # Fallback path check: pipx default paths include 'pipx/venvs'
    if "pipx" in Path(sys.prefix).parts:
        return InstallType.PIPX

    # 3. Source repository (editable or local git checkout / worktree)
    dion_root = Path(__file__).resolve().parent.parent
    if (dion_root / ".git").exists():
        return InstallType.SOURCE

    # 4. Standard pip installation (venv or system python)
    return InstallType.PIP


def get_standalone_target_path() -> Path:
    """
    Resolve the actual standalone binary on disk that should be replaced on update.
    For shell-bundled binaries (build.sh), DION_STANDALONE_BINARY points to the wrapper script.
    """
    env_wrapper = os.environ.get("DION_STANDALONE_BINARY")
    if env_wrapper:
        candidate = Path(env_wrapper).resolve()
        if candidate.exists():
            return candidate

    which_dion = shutil.which("dion")
    if which_dion:
        candidate = Path(which_dion).resolve()
        if candidate.is_file():
            return candidate

    return Path(sys.executable).resolve()


def check_target_writable(target_path: Path) -> bool:
    """Check if the current process has write permissions to the target executable."""
    if target_path.exists():
        return os.access(target_path, os.W_OK) and os.access(target_path.parent, os.W_OK)
    return os.access(target_path.parent, os.W_OK)


def get_latest_github_release() -> Dict[str, Any]:
    """Fetch the latest release information from GitHub API."""
    headers = {
        "User-Agent": f"dion/{__version__}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(follow_redirects=True, timeout=15.0) as client:
        resp = client.get(GITHUB_RELEASES_API, headers=headers)
        if resp.status_code == 404:
            raise RuntimeError("No releases found on GitHub repository yet.")
        elif resp.status_code == 403:
            raise RuntimeError(
                "GitHub API rate limit exceeded. Please try again later or update via pipx."
            )
        resp.raise_for_status()
        return resp.json()


def select_release_asset(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Select the correct release asset for the current OS and architecture."""
    current_os = platform.system().lower()

    if current_os == "linux":
        patterns = ["linux"]
    elif current_os == "darwin":
        patterns = ["macos", "darwin", "osx"]
    elif current_os == "windows":
        patterns = ["windows", "win"]
    else:
        return None

    for asset in assets:
        name = asset.get("name", "").lower()
        if any(pat in name for pat in patterns):
            return asset

    return None


def download_asset_with_progress(url: str, dest_path: Path) -> None:
    """Download a remote URL to dest_path displaying a Rich progress bar."""
    headers = {"User-Agent": f"dion/{__version__}"}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=30.0) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get("Content-Length", 0))

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Downloading release...", total=total_size or None)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=16384):
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))


def update_standalone(force: bool = False) -> None:
    """Update a standalone binary installation from GitHub Releases."""
    with console.status("[bold cyan]Checking GitHub for latest release...[/bold cyan]"):
        try:
            release = get_latest_github_release()
        except Exception as err:
            console.print(f"[bold red]✕ Update check failed:[/bold red] {err}")
            return

    tag_name = release.get("tag_name", "")
    latest_version = tag_name.lstrip("v")
    curr_parsed = parse_version(__version__)
    latest_parsed = parse_version(latest_version)

    console.print(f"[dim]Current version:[/dim] [bold white]v{__version__}[/bold white]")
    console.print(f"[dim]Latest release:[/dim]  [bold white]{tag_name}[/bold white]")

    if latest_parsed <= curr_parsed and not force:
        console.print(f"[bold green]✓ Dion is already up to date ({tag_name}).[/bold green]")
        console.print("[dim]Use --force-update to reinstall the latest release.[/dim]")
        return

    assets = release.get("assets", [])
    selected_asset = select_release_asset(assets)
    if not selected_asset:
        current_os = platform.system()
        console.print(
            f"[bold red]✕ No matching release binary found for {current_os} in release {tag_name}.[/bold red]"
        )
        return

    asset_name = selected_asset.get("name", "")
    download_url = selected_asset.get("browser_download_url", "")
    target_path = get_standalone_target_path()

    console.print(f"[cyan]Target installation path:[/cyan] [bold white]{target_path}[/bold white]")

    if not check_target_writable(target_path):
        console.print(
            f"\n[bold red]✕ Permission Denied:[/bold red] You do not have write access to [bold cyan]{target_path}[/bold cyan]."
        )
        if sys.platform != "win32":
            console.print("Please re-run the update with sudo permissions:")
            console.print("  [bold white]sudo dion -U[/bold white]\n")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_download = Path(tmp_dir) / asset_name
        console.print(f"[cyan]Fetching {asset_name}...[/cyan]")
        try:
            download_asset_with_progress(download_url, tmp_download)
        except Exception as err:
            console.print(f"[bold red]✕ Download failed:[/bold red] {err}")
            return

        # Handle Windows zip archive or direct executable
        if asset_name.endswith(".zip"):
            extract_dir = Path(tmp_dir) / "extracted"
            with zipfile.ZipFile(tmp_download, "r") as zf:
                zf.extractall(extract_dir)

            new_bin = extract_dir / "dion.exe"
            if not new_bin.is_file():
                # Search nested folder if zipped with top-level directory
                candidates = list(extract_dir.glob("**/dion.exe"))
                if candidates:
                    new_bin = candidates[0]
                else:
                    console.print("[bold red]✕ Could not find dion.exe in downloaded archive.[/bold red]")
                    return

            _replace_binary(new_bin, target_path)
        else:
            # Unix binary or shell bundle
            _replace_binary(tmp_download, target_path)

    console.print(f"\n[bold green]✓ Successfully updated Dion to {tag_name}![/bold green]")


def _replace_binary(new_binary: Path, target_path: Path) -> None:
    """Atomically replace the target binary with the new binary."""
    if sys.platform == "win32":
        # On Windows, running executables are locked and cannot be directly overwritten.
        # But they CAN be renamed!
        old_exe = target_path.with_name(f"{target_path.stem}.old.exe")
        if old_exe.exists():
            try:
                old_exe.unlink()
            except Exception:
                pass
        try:
            target_path.rename(old_exe)
        except Exception:
            pass
        shutil.copy2(new_binary, target_path)

        # If new_binary has sibling files or directories (PyInstaller onedir distribution)
        source_dir = new_binary.parent
        target_dir = target_path.parent
        for item in source_dir.iterdir():
            if item.name == new_binary.name:
                continue
            dest = target_dir / item.name
            try:
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
            except Exception:
                pass
    else:
        # On Unix, stage the new file in the target directory and use atomic rename
        temp_dest = target_path.with_name(f".{target_path.name}.new-{os.getpid()}")
        shutil.copy2(new_binary, temp_dest)
        temp_dest.chmod(0o755)
        temp_dest.replace(target_path)


def update_pipx(force: bool = False) -> None:
    """Update Dion when running in a pipx-managed environment."""
    pipx_meta_path = Path(sys.prefix) / "pipx_metadata.json"
    package_spec = "dion"
    is_git_or_url = False

    if pipx_meta_path.is_file():
        try:
            data = json.loads(pipx_meta_path.read_text(encoding="utf-8"))
            main_pkg = data.get("main_package", {})
            spec = main_pkg.get("package_or_url", "")
            if spec:
                package_spec = spec
            if "git+" in package_spec or "github.com" in package_spec:
                is_git_or_url = True
        except Exception:
            pass

    pipx_bin = shutil.which("pipx")

    if pipx_bin:
        if is_git_or_url or force:
            cmd = [pipx_bin, "install", "--force", package_spec]
            cmd_desc = f"pipx install --force {package_spec}"
        else:
            cmd = [pipx_bin, "upgrade", "dion"]
            cmd_desc = "pipx upgrade dion"

        console.print(f"[cyan]Running:[/cyan] [bold white]{cmd_desc}[/bold white]\n")
        try:
            res = subprocess.run(cmd)
            if res.returncode == 0:
                console.print(f"\n[bold green]✓ Successfully upgraded Dion via pipx![/bold green]")
            else:
                console.print(f"\n[bold red]✕ pipx upgrade returned non-zero exit code {res.returncode}[/bold red]")
        except Exception as err:
            console.print(f"[bold red]✕ Failed to run pipx:[/bold red] {err}")
    else:
        # Fallback if pipx CLI is not in PATH: invoke pip within the pipx virtualenv directly
        console.print("[yellow]pipx CLI not found in PATH; upgrading virtual environment directly...[/yellow]")
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", package_spec]
        console.print(f"[cyan]Running:[/cyan] [bold white]{' '.join(cmd)}[/bold white]\n")
        try:
            res = subprocess.run(cmd)
            if res.returncode == 0:
                console.print(f"\n[bold green]✓ Successfully upgraded Dion venv![/bold green]")
            else:
                console.print(f"\n[bold red]✕ pip upgrade returned non-zero exit code {res.returncode}[/bold red]")
        except Exception as err:
            console.print(f"[bold red]✕ Failed to upgrade via pip:[/bold red] {err}")


def run_self_update(force: bool = False) -> None:
    """Entry point for `dion -U` / `dion --update`."""
    install_type = detect_install_type()

    title = "[bold purple]🍇 Dion Self-Updater[/bold purple]"
    info_lines = [
        f"[dim]Current Version:[/dim] [bold white]v{__version__}[/bold white]",
        f"[dim]Install Method:[/dim]  [bold cyan]{install_type.value}[/bold cyan]",
    ]
    console.print(Panel("\n".join(info_lines), title=title, border_style="purple", expand=False))

    if install_type == InstallType.STANDALONE:
        update_standalone(force=force)
    elif install_type == InstallType.PIPX:
        update_pipx(force=force)
    elif install_type == InstallType.SOURCE:
        console.print(
            "[yellow]Dion is running from a local git source repository.[/yellow]\n"
            "To update your development installation, pull latest changes:\n\n"
            "  [bold white]git pull && pip install -e .[/bold white]\n"
        )
    elif install_type == InstallType.PIP:
        console.print(
            "[yellow]Dion is installed in a standard Python environment via pip.[/yellow]\n"
            "To upgrade, run:\n\n"
            "  [bold white]pip install --upgrade dion[/bold white]\n"
            "Or to upgrade inside your active python environment:\n"
            f"  [bold white]{sys.executable} -m pip install --upgrade dion[/bold white]\n"
        )
    else:
        console.print("[yellow]Unknown installation method. Please update manually.[/yellow]")
