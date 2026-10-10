# 🍇 Dion

<p align="center">
  <strong>A blazing-fast terminal movie & TV streaming and downloading CLI with zero configuration, auto-subtitles, and smart resume.</strong>
</p>

<p align="center">
  <em>Inspired by <code>ani-cli</code>, built for movies, series, and general media.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey?style=flat" alt="Platforms">
  <img src="https://img.shields.io/badge/stream-100%25%20Direct%20HTTPS%20%2F%20HLS-blue?style=flat" alt="Direct HTTPS">
  <img src="https://img.shields.io/badge/player-mpv%20%7C%20iina%20%7C%20vlc-orange?style=flat" alt="Supported Players">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat" alt="License">
</p>

---

## 🌟 Highlights

- 🔍 **Zero-Config Discovery:** Instant search for movies and TV series powered by Cinemeta & IMDb metadata—no API keys or accounts required.
- ⚡ **Interactive Terminal UI:** Blazing-fast fuzzy finder with [`fzf`](https://github.com/junegunn/fzf), backed by an interactive arrow-key fallback menu.
- 📺 **TV Show Binge Mode:** Intuitive season & episode browser with an automatic *"Watch next episode?"* prompt upon episode completion.
- 🌐 **Multi-Provider Failover:** Automatic stream resolution across multi-server HLS (`.m3u8`) and MP4 sources with instant failover.
- 🔒 **100% Direct HTTPS / No P2P:** All media is fetched via direct outbound client-to-server HTTP/HTTPS streams. Zero torrents, zero DHT swarms, and zero peer exposure.
- 💬 **Subtitles & Live Sync:** Auto-fetches subtitles via OpenSubtitles and stream providers. Adjust and persist subtitle sync timing on-the-fly (`z` / `x`).
- ⏱️ **Smart Resume & History:** Remembers your exact playback position (saves every 5s, on pause, and on quit) and lets you pick up right where you left off (`-c`).
- 🎬 **Multi-Player Support:** Built for [`mpv`](https://mpv.io/) with a custom Lua synchronization engine; also auto-detects and supports [IINA](https://iina.io/) and [VLC](https://www.videolan.org/).
- ⚙️ **Interactive Settings Manager (`-S`):** Configure default player, subtitle preferences, auto-server selection, and download paths from a clean TUI menu.
- 📥 **Offline Download Mode:** Save full movies or episodes to disk using `yt-dlp` or `ffmpeg` (`-d`).
- 🚀 **Standalone Executable:** Can be compiled into a single self-caching executable binary with zero Python runtime required.

---

## 📋 Table of Contents

- [Prerequisites](#-prerequisites)
- [Installation](#-installation)
  - [Option A: Standalone Executable (Recommended)](#option-a-standalone-executable-recommended)
  - [Option B: Install with Pip / Pipx](#option-b-install-with-pip--pipx)
- [Usage](#-usage)
  - [Interactive Search & Stream](#interactive-search--stream)
  - [Direct Search](#direct-search)
  - [TV Series Direct Episode Jump](#tv-series-direct-episode-jump)
  - [Continue Watching & Watch History](#continue-watching--watch-history)
  - [Settings Dashboard](#settings-dashboard)
  - [Download Mode](#download-mode)
  - [Auto-Pick Best Server](#auto-pick-best-server)
  - [Manual Subtitle Delay](#manual-subtitle-delay)
- [CLI Reference](#-cli-reference)
- [In-Player Controls (mpv)](#-in-player-controls-mpv)
- [Configuration & Storage](#-configuration--storage)
- [Building from Source](#-building-from-source)
- [Project Architecture](#-project-architecture)
- [Disclaimer](#-disclaimer)

---

## 📦 Prerequisites

Dion uses a video player for media playback and optional command-line tools for fuzzy-finding and downloads.

### 1. Video Player (Required)
[`mpv`](https://mpv.io/) is strongly recommended for the best experience (subtitles sync, auto-resume Lua engine, and hardware acceleration):

```bash
# macOS
brew install mpv

# Ubuntu / Debian
sudo apt install mpv

# Arch Linux
sudo pacman -S mpv

# Fedora
sudo dnf install mpv
```
*(Note: Dion also auto-detects `iina` on macOS or `vlc` on Linux/macOS if mpv is not installed).*

### 2. Recommended Utilities (Optional)
- **`fzf`**: Enables lightning-fast interactive fuzzy search (recommended):
  ```bash
  brew install fzf          # macOS
  sudo apt install fzf      # Debian/Ubuntu
  sudo pacman -S fzf        # Arch
  ```
- **`yt-dlp`** or **`ffmpeg`**: Required only if you plan to use download mode (`-d`):
  ```bash
  brew install yt-dlp       # macOS
  sudo apt install yt-dlp   # Debian/Ubuntu
  sudo pacman -S yt-dlp     # Arch
  ```

---

## ⚡ Installation

### Option A: Standalone Executable (Recommended)

You can build and run Dion as a self-contained, high-performance binary with zero external Python dependencies:

```bash
# Clone the repository
git clone https://github.com/your-username/dion.git
cd dion

# Build the binary
./build.sh

# Install globally
sudo cp ./dist/dion /usr/local/bin/dion

# Or install for your user only
cp ./dist/dion ~/.local/bin/dion
```

### Option B: Install with Pip / Pipx

If you prefer running Dion via Python (requires Python 3.9+):

```bash
# Clone the repository
git clone https://github.com/your-username/dion.git
cd dion

# Install in editable mode
pip install -e .

# Or install with pipx for an isolated CLI environment
pipx install .
```

Verify your installation:
```bash
dion --help
```

---

## 🚀 Usage

### Interactive Search & Stream
Launch Dion without arguments to open an interactive search prompt:
```bash
dion
```

### Direct Search
Search for a movie or TV show directly by title:
```bash
# Search for a movie
dion "Inception"
dion "Dune: Part Two"

# Search for a TV series
dion "Breaking Bad"
dion "The Bear"
```

### TV Series Direct Episode Jump
Jump straight into a specific season and episode without navigating the interactive menus:
```bash
# Jump directly to Season 2, Episode 4 of Severance
dion "Severance" -s 2 -e 4

# Jump directly to Season 1, Episode 1 of Succession
dion "Succession" -s 1 -e 1
```

### Continue Watching & Watch History
Dion tracks what you watch and automatically bookmarks your playback timestamp whenever you pause or exit:
```bash
# Resume your last watched movie or episode right where you left off
dion -c
# (or)
dion --continue

# Browse and select from your recent watch history
dion --history

# Clear your watch history
dion -D
# (or)
dion --delete-history
```

### Settings Dashboard
Configure default playback preferences with the built-in interactive menu:
```bash
dion -S
# (or)
dion --settings
```
From here you can:
- Change your default media player (`auto`, `mpv`, `iina`, `vlc`, or a custom path).
- Toggle subtitles on/off by default at launch.
- Change preferred subtitle language (e.g. `en`, `es`, `fr`, `de`, `ja`, etc.).
- Enable/disable automatic server selection.
- Set your default downloads directory.
- Reset all configurations to factory defaults.

### Download Mode
Save media files locally for offline viewing with soft-embedded subtitles, container metadata, and cover art:
```bash
# Download a movie (saved to ~/Downloads or your configured directory)
dion "Interstellar" -d

# Download a specific TV episode
dion "The Bear" -s 1 -e 1 -d

# Download an ENTIRE season in a single command
dion "Severance" -s 1 -d --all

# Download a specific episode range
dion "Severance" -s 1 -d --range 1-4

# Choose container format (MP4 with mov_text or MKV with SRT)
dion "Severance" -s 1 -d --all -f mkv

# Set concurrent episode download workers (default: 2)
dion "Severance" -s 1 -d --all -C 3

# Specify a custom download folder
dion "Blade Runner 2049" -d -o ~/Movies
```

All downloaded files automatically include:
- **Soft-Muxed Subtitles:** Selectable `.srt`/`.vtt` subtitle tracks embedded directly in the container without re-encoding video.
- **Rich Progress Dashboard:** Multi-task concurrent progress bars showing download speeds (MB/s), percentages, and ETAs.
- **Metadata Tags & Cover Art:** Apple TV / Jellyfin / Plex compatible tags (title, season, episode, year, overview, and poster artwork).

### Auto-Pick Best Server
Skip the stream server selection menu and immediately start playback on the first working server:
```bash
dion "Oppenheimer" --best
```

### Manual Subtitle Delay
If you already know a stream requires a subtitle offset, you can pass it at launch:
```bash
# Advance subtitles by 1.85 seconds
dion "Inception" --sub-delay 1.85

# Delay subtitles by 0.5 seconds
dion "The Matrix" --sub-delay -0.5
```

---

## 📖 CLI Reference

```text
Usage: dion [OPTIONS] [QUERY]

Arguments:
  [QUERY]                   Movie or TV show title to search for.

Options:
  -c, --continue            Continue watching the last title where you left off.
      --history             Browse and select from recent watch history.
  -D, --delete-history      Delete watch history.
  -S, --settings            Open settings menu to configure player, subtitles, and preferences.
  -s, --season <int>        Specific season number for TV shows.
  -e, --episode <int>       Specific episode number for TV shows.
  -a, --all                 Download all episodes for the season (batch mode).
      --range <str>         Download a range of episodes (e.g. '1-4', '1,2,5', '3-').
  -f, --format <str>        Output container format ('mp4' or 'mkv', default: mp4).
  -C, --concurrent <int>    Number of concurrent downloads in batch mode (default: 2).
      --overwrite, --force  Overwrite existing downloaded files.
      --best                Automatically pick the first server without prompting.
  -d, --download            Download the media instead of streaming.
  -o, --output <path>       Output directory for downloads (default: ~/Downloads).
      --sub-delay <float>   Subtitle delay offset in seconds (e.g. 1.85 or -0.5).
  -h, --help                Show this message and exit.
```

---

## 🎮 In-Player Controls (mpv)

When streaming with `mpv`, Dion injects a custom Lua engine providing enhanced subtitle synchronization, OSD status messages, and seamless position tracking:

| Key | Action |
| :--- | :--- |
| `z` / `x` | Fine adjust subtitle delay (**±0.1s**) |
| `Z` / `X` (*Shift+z/x*) | Coarse adjust subtitle delay (**±0.5s**) |
| `Ctrl + z` | Reset subtitle delay to **0.0s** |
| `v` | Toggle subtitles on / off |
| `j` / `J` | Cycle subtitle tracks forward / backward |
| `Space` | Pause / Resume playback *(saves position)* |
| `←` / `→` | Seek backward / forward 5 seconds |
| `↓` / `↑` | Seek backward / forward 1 minute |
| `[` / `]` | Decrease / Increase playback speed by 10% |
| `9` / `0` | Decrease / Increase volume |
| `m` | Mute / Unmute audio |
| `f` | Toggle fullscreen |
| `q` / `Q` / `Cmd+w` | Save current position and quit cleanly |

> [!TIP]
> Any subtitle timing adjustments made with `z` or `x` are automatically saved per IMDb title and will be restored the next time you watch!

---

## ⚙️ Configuration & Storage

Dion stores your preferences and watch progress in standard XDG configuration folders:

```text
~/.config/dion/
├── settings.json            # User preferences (player, sub language, paths)
├── history.json             # Watch history records
├── playback_positions.json  # Resume timestamps (saved every 5s & on exit)
├── sub_delays.json          # Persistent subtitle sync adjustments
└── scripts/
    └── dion_subtitles.lua   # Automatic subtitle sync & OSD engine for mpv
```

---

## 🛠️ Building from Source

Dion includes an optimized build script (`build.sh`) that bundles Python dependencies and the application code into a standalone, single-file executable using PyInstaller:

```bash
# 1. Ensure you have bash, tar, and python3 installed
chmod +x build.sh

# 2. Run the build script
./build.sh
```

The script will:
1. Create and configure a local virtual environment in `.venv/`.
2. Install the Dion package and PyInstaller.
3. Build an optimized directory bundle.
4. Package it with a self-caching extraction wrapper and SHA-256 integrity verification.
5. Produce the executable at `dist/dion`.

---

## 📂 Project Architecture

```text
dion/
├── dion/
│   ├── cli.py             # CLI entrypoint, Typer app & binge watch loop
│   ├── ui.py              # fzf integration, Questionary menus & Rich panels
│   ├── metadata/
│   │   ├── cinemeta.py    # Zero-config movie & TV metadata resolver
│   │   └── models.py      # Data models (MediaItem, EpisodeItem, StreamSource)
│   ├── providers/
│   │   ├── base.py        # Abstract stream provider interface
│   │   ├── manager.py     # Provider failover & subtitle aggregation
│   │   ├── vidora.py      # Vidora HLS multi-stream resolver
│   │   ├── vixsrc.py      # VixSrc HLS & MP4 stream resolver
│   │   └── subtitles.py   # OpenSubtitles REST API resolver
│   ├── player/
│   │   ├── mpv.py         # Player controller & dynamic Lua script injector
│   │   └── downloader.py  # yt-dlp & ffmpeg stream downloader
│   └── storage/
│       ├── history.py     # Watch history & playback position persistence
│       └── settings.py    # Persistent user preferences & configuration
├── main.py                # PyInstaller packaging entrypoint
├── build.sh               # One-click standalone binary build script
├── pyproject.toml         # Package definition and dependencies
└── README.md
```

---

## 🔒 Privacy & Architecture Principles

- **Zero P2P / No Torrents:** Dion never uses BitTorrent protocols, magnet links, or peer-to-peer swarms.
- **Client-to-Server Only:** All streaming and downloading traffic consists of 100% direct client-to-server outbound HTTP/HTTPS requests.
- **No Data Collection:** Dion does not collect telemetry, track user behavior, or require external user accounts.

---

## ⚖️ Disclaimer

This software is developed strictly for educational and personal research purposes. Dion does not host, store, or distribute any media files or video streams. All stream scraping and metadata retrieval are performed dynamically from publicly accessible third-party web services. Users are solely responsible for ensuring their usage complies with all applicable local copyright laws and regulations.
