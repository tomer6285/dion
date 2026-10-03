from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import List, Optional

from rich.console import Console

from ..metadata.models import EpisodeItem, MediaItem, StreamSource
from ..storage import HistoryManager

console = Console()


class MpvPlayer:
    """Controls playback via mpv (with fallback to VLC/IINA)."""

    def __init__(self, executable: Optional[str] = None):
        self.executable = executable or self._find_player()

    def _find_player(self) -> Optional[str]:
        for candidate in ["mpv", "iina", "vlc"]:
            path = shutil.which(candidate)
            if path:
                return path
        return None

    def is_available(self) -> bool:
        return self.executable is not None

    def _ensure_lua_script(self) -> Path:
        """Create or update the Dion subtitle synchronization and OSD Lua engine."""
        scripts_dir = Path.home() / ".config" / "dion" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_file = scripts_dir / "dion_subtitles.lua"

        lua_code = """-- Dion Subtitle Synchronization & OSD Engine
local mp = require 'mp'

local imdb_id = mp.get_opt("dion_imdb", "")
if imdb_id == "" then imdb_id = mp.get_opt("dionysus_imdb", "") end
local config_dir = mp.get_opt("dion_config", "")
if config_dir == "" then config_dir = mp.get_opt("dionysus_config", "") end
local delays_file = (config_dir ~= "") and (config_dir .. "/sub_delays.json") or nil

local function load_saved_delay()
    if not delays_file or imdb_id == "" then return 0.0 end
    local f = io.open(delays_file, "r")
    if not f then return 0.0 end
    local content = f:read("*all")
    f:close()
    if not content or content == "" then return 0.0 end
    local pattern = '"' .. imdb_id .. '"%s*:%s*([%-0-9%.]+)'
    local val = content:match(pattern)
    if val then
        return tonumber(val) or 0.0
    end
    return 0.0
end

local function save_delay(delay)
    if not delays_file or imdb_id == "" then return end
    local data = {}
    local f = io.open(delays_file, "r")
    if f then
        local content = f:read("*all")
        f:close()
        for k, v in content:gmatch('"([%w_]+)"%s*:%s*([%-0-9%.]+)') do
            data[k] = tonumber(v)
        end
    end
    data[imdb_id] = delay
    local out = io.open(delays_file, "w")
    if out then
        out:write("{\\n")
        local first = true
        for k, v in pairs(data) do
            if not first then out:write(",\\n") end
            first = false
            out:write(string.format('  "%s": %.2f', k, v))
        end
        out:write("\\n}\\n")
        out:close()
    end
end

local function notify_delay(delay, extra)
    local sign = delay > 0 and "+" or ""
    local msg = string.format("💬 Subtitle Delay: %s%.2fs", sign, delay)
    if extra then
        msg = msg .. "  (" .. extra .. ")"
    end
    mp.osd_message(msg, 2.5)
end

local function add_sub_delay(delta, label)
    local current = mp.get_property_number("sub-delay", 0.0)
    local new_val = math.floor((current + delta) * 100 + 0.5) / 100
    mp.set_property_number("sub-delay", new_val)
    notify_delay(new_val, label)
    save_delay(new_val)
end

local function reset_sub_delay()
    mp.set_property_number("sub-delay", 0.0)
    notify_delay(0.0, "Reset")
    save_delay(0.0)
end

-- Key bindings:
-- z / x: fine adjust (-0.1s / +0.1s)
mp.add_forced_key_binding("z", "dion_sub_down_fine", function() add_sub_delay(-0.1, "z/x: ±0.1s") end, {repeatable=true})
mp.add_forced_key_binding("x", "dion_sub_up_fine", function() add_sub_delay(0.1, "z/x: ±0.1s") end, {repeatable=true})

-- Z / X (Shift+z / Shift+x): coarse adjust (-0.5s / +0.5s)
mp.add_forced_key_binding("Z", "dion_sub_down_coarse", function() add_sub_delay(-0.5, "Z/X: ±0.5s") end, {repeatable=true})
mp.add_forced_key_binding("X", "dion_sub_up_coarse", function() add_sub_delay(0.5, "Z/X: ±0.5s") end, {repeatable=true})

-- Ctrl+z: reset to 0.0s
mp.add_forced_key_binding("ctrl+z", "dion_sub_reset", reset_sub_delay)

-- j / J: cycle subtitle tracks with clean OSD
local function cycle_sub(step)
    mp.commandv("cycle", "sub", step > 0 and "up" or "down")
    local sid = mp.get_property("sid")
    if sid == "no" or not sid then
        mp.osd_message("💬 Subtitles: Off", 2.0)
    else
        local title = mp.get_property("current-tracks/sub/title") or mp.get_property("current-tracks/sub/lang") or ("Track " .. sid)
        mp.osd_message(string.format("💬 Subtitle: %s", title), 2.5)
    end
end
mp.add_forced_key_binding("j", "dion_sub_cycle_fwd", function() cycle_sub(1) end)
mp.add_forced_key_binding("J", "dion_sub_cycle_back", function() cycle_sub(-1) end)

-- Initial banner on playback start
mp.register_event("file-loaded", function()
    local saved = load_saved_delay()
    if math.abs(saved) > 0.01 then
        mp.set_property_number("sub-delay", saved)
    end

    mp.add_timeout(1.2, function()
        local sid = mp.get_property("sid")
        if sid and sid ~= "no" then
            local current = mp.get_property_number("sub-delay", 0.0)
            local title = mp.get_property("current-tracks/sub/title") or "English"
            local delay_info = ""
            if math.abs(current) > 0.05 then
                local sign = current > 0 and "+" or ""
                delay_info = string.format(" [Sync: %s%.2fs]", sign, current)
            end
            mp.osd_message(string.format("💬 Subtitle: %s%s\\n• Adjust: z/x (±0.1s)  Z/X (±0.5s)  j (cycle)", title, delay_info), 4.0)
        end
    end)
end)
"""
        script_file.write_text(lua_code, encoding="utf-8")
        return script_file

    def play(
        self,
        source: StreamSource,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        start_time: Optional[float] = None,
        sub_delay: Optional[float] = None,
    ) -> int:
        """Launch the media player with the stream source and required headers."""
        if not self.executable:
            console.print(
                "[bold red]Error:[/bold red] No compatible video player found!\n"
                "Please install [bold cyan]mpv[/bold cyan]:\n"
                "  • macOS: [yellow]brew install mpv[/yellow]\n"
                "  • Linux: [yellow]sudo apt install mpv[/yellow] or [yellow]sudo pacman -S mpv[/yellow]\n"
            )
            return 1

        is_mpv = "mpv" in self.executable.lower()
        is_iina = "iina" in self.executable.lower()
        title_str = media.title
        if episode:
            title_str += f" - {episode.display_name}"

        # Resolve persistent subtitle delay if not explicitly provided
        config_dir = Path.home() / ".config" / "dion"
        if sub_delay is None:
            sub_delay = HistoryManager(config_dir).get_sub_delay(media.imdb_id)

        lua_script = self._ensure_lua_script()

        cmd = [self.executable]

        if is_mpv:
            # Suppress all terminal output
            cmd.append("--really-quiet")
            cmd.append("--terminal=no")
            cmd.append("--msg-level=all=no")

            # Set window title
            cmd.append(f"--title=Dion: {title_str}")
            cmd.append("--save-position-on-quit")

            # Hardware acceleration (VideoToolbox on macOS, VAAPI/NVDEC on Linux)
            cmd.append("--hwdec=auto")
            cmd.append("--profile=fast")

            # Disable ytdl hook so mpv directly plays HLS/m3u8 streams using ffmpeg demuxer
            if source.is_hls or ".m3u8" in source.url or "proxy" in source.url:
                cmd.append("--ytdl=no")

            # Set user-agent and referrer using dedicated mpv options
            ua = source.headers.get("User-Agent") or source.headers.get("user-agent")
            if ua:
                cmd.append(f"--user-agent={ua}")

            ref = source.headers.get("Referer") or source.headers.get("referer")
            if ref:
                cmd.append(f"--referrer={ref}")

            # Pass any extra headers (e.g. Origin) via http-header-fields-append
            for k, v in source.headers.items():
                if k.lower() not in ("user-agent", "referer"):
                    cmd.append(f"--http-header-fields-append={k}: {v}")

            # Stream connection stability & reconnection:
            # - seg_max_retry=5: retries transient segment errors rather than skipping (preventing A/V desync)
            # - reconnect_on_http_error=4xx,5xx: automatically retries temporary CDN errors
            cmd.append(
                r"--demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_on_http_error=4xx\,5xx,reconnect_delay_max=2,seg_max_retry=5"
            )

            # High-performance buffering and non-blocking playback
            cmd.append("--cache=yes")
            cmd.append("--cache-pause=no")
            cmd.append("--cache-pause-initial=no")
            cmd.append("--cache-pause-wait=0.5")
            cmd.append("--cache-secs=300")
            cmd.append("--demuxer-readahead-secs=60")
            cmd.append("--demuxer-max-bytes=256M")
            cmd.append("--demuxer-max-back-bytes=128M")
            cmd.append("--demuxer-seekable-cache=yes")

            # Instant scrubbing & audio pitch correction
            cmd.append("--audio-pitch-correction=yes")
            cmd.append("--audio-buffer=0.5")
            cmd.append("--hr-seek=default")
            cmd.append("--hr-seek-framedrop=yes")
            cmd.append("--correct-pts=yes")

            # Preferred subtitle language & synchronization engine
            cmd.append("--slang=en,eng,English")
            cmd.append("--sub-auto=fuzzy")
            cmd.append("--sub-fix-timing=yes")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--sub-delay={sub_delay}")

            # Attach Dion Lua script and pass context parameters
            cmd.append(f"--script={lua_script}")
            cmd.append(f"--script-opts=dion_imdb={media.imdb_id},dion_config={config_dir}")

            # Add subtitles (top ranked tracks)
            for sub in source.subtitles[:3]:
                cmd.append(f"--sub-file={sub.url}")

            # Resume position if provided
            if start_time and start_time > 10:
                cmd.append(f"--start={int(start_time)}")

        elif is_iina:
            cmd.append(f"--mpv-title=Dion: {title_str}")
            cmd.append("--mpv-hwdec=auto")
            cmd.append("--mpv-profile=fast")
            cmd.append("--mpv-really-quiet")
            cmd.append(
                r"--mpv-demuxer-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_on_http_error=4xx\,5xx,reconnect_delay_max=2,seg_max_retry=5"
            )
            cmd.append("--mpv-cache=yes")
            cmd.append("--mpv-cache-pause=no")
            cmd.append("--mpv-cache-pause-initial=no")
            cmd.append("--mpv-demuxer-readahead-secs=60")
            cmd.append("--mpv-demuxer-seekable-cache=yes")
            cmd.append("--mpv-hr-seek=default")
            if source.is_hls or ".m3u8" in source.url or "proxy" in source.url:
                cmd.append("--mpv-ytdl=no")
            ua = source.headers.get("User-Agent") or source.headers.get("user-agent")
            if ua:
                cmd.append(f"--mpv-user-agent={ua}")
            ref = source.headers.get("Referer") or source.headers.get("referer")
            if ref:
                cmd.append(f"--mpv-referrer={ref}")
            for k, v in source.headers.items():
                if k.lower() not in ("user-agent", "referer"):
                    cmd.append(f"--mpv-http-header-fields-append={k}: {v}")
            cmd.append("--mpv-slang=en,eng,English")
            cmd.append("--mpv-sub-fix-timing=yes")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--mpv-sub-delay={sub_delay}")
            cmd.append(f"--mpv-script={lua_script}")
            cmd.append(f"--mpv-script-opts=dion_imdb={media.imdb_id},dion_config={config_dir}")
            for sub in source.subtitles[:3]:
                cmd.append(f"--mpv-sub-file={sub.url}")

        elif "vlc" in self.executable.lower():
            cmd.append(f"--meta-title={title_str}")
            cmd.append("--quiet")
            ref = source.headers.get("Referer") or source.headers.get("referer")
            if ref:
                cmd.append(f"--http-referrer={ref}")
            ua = source.headers.get("User-Agent") or source.headers.get("user-agent")
            if ua:
                cmd.append(f"--http-user-agent={ua}")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--sub-delay={sub_delay}")
            if source.subtitles:
                cmd.append(f"--sub-file={source.subtitles[0].url}")

        # Target video URL
        cmd.append(source.url)

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return res.returncode
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            console.print(f"[bold red]Player error:[/bold red] {e}")
            return 1

