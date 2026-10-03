# 🍇 Dion

> A terminal-based movie and TV show streaming & downloading CLI inspired by `ani-cli`.

Stream or download non-anime movies and series directly to [`mpv`](https://mpv.io/) from the command line with zero configuration, auto-fetching subtitles, and an interactive fuzzy search interface.

---

## ✨ Features

- **Zero-Config Discovery:** Instant search for movies and TV series powered by Cinemeta (no API keys required).
- **Interactive Terminal UI:** Fuzzy search with [`fzf`](https://github.com/junegunn/fzf) if installed, with a built-in arrow-key menu fallback.
- **TV Show Binge Mode:** Seamless season & episode picker with an automatic *"Watch next episode?"* prompt upon completion.
- **Multi-Server Streams:** Master HLS (`.m3u8`) 1080p stream resolution across multiple failover sources.
- **Auto Subtitles:** Automatically pulls and injects UTF-8 subtitles in multiple languages directly into `mpv`.
- **Watch History & Resume:** Track watched items and jump back into your last show with `dion -c`.
- **Download Mode:** Save movies and episodes locally with `-d` using `yt-dlp` or `ffmpeg`.
- **Standalone Binary:** Can be compiled into a single executable binary with no Python installation required.

---

## ⚡ Quick Start

### Prerequisites
Make sure you have `mpv` installed for video playback:
```bash
# macOS
brew install mpv fzf

# Ubuntu / Debian
sudo apt install mpv fzf

# Arch Linux
sudo pacman -S mpv fzf
```
*(Optional: install `yt-dlp` if you want to use the `-d` download feature: `brew install yt-dlp`)*

---

### Running the Standalone Executable
You can run the compiled binary directly:
```bash
./dist/dion "Inception"
```
Or move it to your system path:
```bash
sudo cp ./dist/dion /usr/local/bin/dion
dion "Inception"
```

---

## 🚀 Usage

### Search & Stream
```bash
# Interactive search prompt
dion

# Search for a specific title directly
dion "The Matrix"
dion "Breaking Bad"
```

### TV Series Direct Selection
```bash
# Jump straight to Season 2, Episode 4
dion "Severance" -s 2 -e 4
```

### Continue Watching / Watch History
```bash
# Resume your last watched movie or show
dion -c

# Browse and select from recent watch history
dion --history
```

### Download Mode
```bash
# Download a movie or episode to ~/Downloads
dion "Interstellar" -d
dion "The Bear" -s 1 -e 1 -d
```

### Pick the Best Server Automatically
```bash
# Skip server selection prompt
dion "Dune" --best
```

---

## 🛠️ Building the Standalone Binary

To re-compile the single executable using PyInstaller:
```bash
./build.sh
```
The output binary will be created at `./dist/dion`.

---

## 📂 Project Structure

```text
dion/
├── dion/
│   ├── cli.py             # CLI commands, options & binge loop
│   ├── ui.py              # fzf and arrow-key menus + Rich cards
│   ├── metadata/
│   │   ├── cinemeta.py    # Zero-config movie & series metadata
│   │   └── models.py      # Dataclasses (MediaItem, EpisodeItem, StreamSource)
│   ├── providers/
│   │   ├── base.py        # Abstract stream provider
│   │   ├── vidora.py      # Multi-source HLS stream resolver
│   │   ├── subtitles.py   # OpenSubtitles REST API resolver
│   │   └── manager.py     # Provider failover and subtitle attachment
│   ├── player/
│   │   ├── mpv.py         # mpv launcher with headers & subtitles
│   │   └── downloader.py  # yt-dlp & ffmpeg download integration
│   └── storage/
│       └── history.py     # Watch history persistence (~/.config/dion)
├── main.py                # Standalone entrypoint for PyInstaller
├── build.sh               # One-click build script
├── pyproject.toml         # Python package specification
└── README.md
```

---

## ⚖️ Disclaimer
This software is provided for educational and research purposes only. Users are responsible for complying with local laws and terms of service regarding media consumption.

