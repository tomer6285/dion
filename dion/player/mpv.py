from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import List, Optional

from rich.console import Console

from ..metadata.models import EpisodeItem, MediaItem, StreamSource
from ..storage import HistoryManager, SettingsManager

console = Console()


class MpvPlayer:
    """Controls playback via mpv (with fallback to VLC/IINA)."""

    def __init__(
        self,
        executable: Optional[str] = None,
        settings_mgr: Optional[SettingsManager] = None,
    ):
        self.settings_mgr = settings_mgr or SettingsManager()
        self.executable = executable or self.settings_mgr.resolve_player_executable() or self._find_player()

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

        lua_code = """-- Dion Subtitle Synchronization & Playback Resume Engine
local mp = require 'mp'

local imdb_id = mp.get_opt("dion_imdb", "")
if imdb_id == "" then imdb_id = mp.get_opt("dionysus_imdb", "") end
local config_dir = mp.get_opt("dion_config", "")
if config_dir == "" then config_dir = mp.get_opt("dionysus_config", "") end
if config_dir == "" then
    local home = os.getenv("HOME") or ""
    if home ~= "" then config_dir = home .. "/.config/dion" end
end
local season = mp.get_opt("dion_season", "")
local episode = mp.get_opt("dion_episode", "")

local delays_file = (config_dir ~= "") and (config_dir .. "/sub_delays.json") or nil
local pos_file = (config_dir ~= "") and (config_dir .. "/playback_positions.json") or nil

local pos_key = imdb_id
if season ~= "" and episode ~= "" then
    pos_key = imdb_id .. ":" .. season .. ":" .. episode
end

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

local function save_position(force_pos)
    if not pos_file or pos_key == "" then return end
    local pos = force_pos or mp.get_property_number("time-pos", 0.0)
    local duration = mp.get_property_number("duration", 0.0)
    if force_pos == nil then
        if not pos or pos <= 0 then return end
        if duration and duration > 60 and (pos >= duration - 30 or pos >= duration * 0.95) then
            pos = 0.0
        elseif pos < 10 then
            return
        end
    end

    local data = {}
    local f = io.open(pos_file, "r")
    if f then
        local content = f:read("*all")
        f:close()
        if content then
            for k, v in content:gmatch('"([^"]+)"%s*:%s*([%-0-9%.]+)') do
                data[k] = tonumber(v)
            end
        end
    end
    data[pos_key] = math.floor(pos * 10 + 0.5) / 10
    local out = io.open(pos_file, "w")
    if out then
        out:write("{\\n")
        local first = true
        for k, v in pairs(data) do
            if not first then out:write(",\\n") end
            first = false
            out:write(string.format('  "%s": %.1f', k, v))
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

-- v: toggle subtitles on / off
local function toggle_sub()
    local sid = mp.get_property("sid")
    if sid == "no" or not sid then
        mp.set_property("sid", "auto")
        local new_sid = mp.get_property("sid")
        if new_sid == "no" or not new_sid then
            mp.commandv("cycle", "sub", "up")
            new_sid = mp.get_property("sid")
        end
        if new_sid and new_sid ~= "no" then
            local title = mp.get_property("current-tracks/sub/title") or mp.get_property("current-tracks/sub/lang") or ("Track " .. new_sid)
            mp.osd_message(string.format("💬 Subtitles: On (%s)", title), 2.5)
        else
            mp.osd_message("💬 Subtitles: None available", 2.0)
        end
    else
        mp.set_property("sid", "no")
        mp.osd_message("💬 Subtitles: Off", 2.0)
    end
end
mp.add_forced_key_binding("v", "dion_sub_toggle", toggle_sub)

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

-- Initial banner on playback start & restore subtitle delay
mp.register_event("file-loaded", function()
    local saved = load_saved_delay()
    if math.abs(saved) > 0.01 then
        mp.set_property_number("sub-delay", saved)
    end

    mp.add_timeout(0.6, function()
        local pos = mp.get_property_number("time-pos", 0.0)
        if pos and pos > 10 then
            local total_s = math.floor(pos)
            local hrs = math.floor(total_s / 3600)
            local mins = math.floor((total_s % 3600) / 60)
            local secs = total_s % 60
            local time_str = (hrs > 0) and string.format("%02d:%02d:%02d", hrs, mins, secs) or string.format("%02d:%02d", mins, secs)
            mp.osd_message(string.format("▶ Resumed at %s", time_str), 2.5)
        end
    end)

    mp.add_timeout(1.5, function()
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

-- Periodic position persistence (every 5 seconds while playing)
mp.add_periodic_timer(5.0, function()
    local paused = mp.get_property_bool("pause", false)
    if not paused then
        save_position()
    end
end)

-- Observe pause property: save position whenever paused
mp.observe_property("pause", "bool", function(name, paused)
    if paused then
        save_position()
    end
end)

-- Window close & quit bindings: ensure closing the window or quitting exits mpv cleanly and saves position
local function quit_player()
    save_position()
    mp.command("quit 0")
end

mp.add_forced_key_binding("CLOSE_WIN", "dion_close_win", quit_player)
mp.add_forced_key_binding("q", "dion_quit_q", quit_player)
mp.add_forced_key_binding("Q", "dion_quit_Q", quit_player)
mp.add_forced_key_binding("Meta+w", "dion_cmd_w", quit_player)
mp.add_forced_key_binding("Meta+q", "dion_cmd_q", quit_player)

mp.register_event("end-file", function(event)
    if event and event.reason == "eof" then
        save_position(0.0)
    else
        save_position()
    end
    mp.command("quit 0")
end)

mp.register_event("shutdown", function()
    save_position()
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

        # Prepare script-opts
        script_opts = [
            f"dion_imdb={media.imdb_id}",
            f"dion_config={config_dir}",
        ]
        if episode:
            script_opts.append(f"dion_season={episode.season}")
            script_opts.append(f"dion_episode={episode.episode}")
        script_opts_str = ",".join(script_opts)

        # Subtitle preferences from settings
        subtitles_enabled = self.settings_mgr.subtitles_enabled
        sub_lang = self.settings_mgr.sub_lang or "en"

        if is_mpv:
            # Suppress all terminal output
            cmd.append("--really-quiet")
            cmd.append("--terminal=no")
            cmd.append("--msg-level=all=no")

            # Set window title
            cmd.append(f"--title=Dion: {title_str}")

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

            # Stream connection stability:
            # Reconnect on dropped connections without treating playlist EOF as a severed stream
            cmd.append("--demuxer-lavf-o=reconnect=1,reconnect_delay_max=2")
            cmd.append("--force-window=immediate")
            cmd.append("--keep-open=no")
            cmd.append("--idle=no")

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

            # Preferred audio & subtitle language & synchronization engine
            cmd.append("--alang=en,eng,English")
            cmd.append(f"--slang={sub_lang},en,eng,English")
            cmd.append("--sub-auto=fuzzy")
            cmd.append("--sub-fix-timing=yes")
            if not subtitles_enabled:
                cmd.append("--sid=no")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--sub-delay={sub_delay}")

            # Attach Dion Lua script and pass context parameters
            cmd.append(f"--script={lua_script}")
            cmd.append(f"--script-opts={script_opts_str}")

            # Add subtitles
            for sub in source.subtitles:
                cmd.append(f"--sub-file={sub.url}")

            # Resume position if provided
            if start_time and start_time > 10:
                cmd.append(f"--start={int(start_time)}")

        elif is_iina:
            cmd.append(f"--mpv-title=Dion: {title_str}")
            cmd.append("--mpv-hwdec=auto")
            cmd.append("--mpv-profile=fast")
            cmd.append("--mpv-really-quiet")
            cmd.append("--mpv-demuxer-lavf-o=reconnect=1,reconnect_delay_max=2")
            cmd.append("--mpv-keep-open=no")
            cmd.append("--mpv-idle=no")
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
            cmd.append("--mpv-alang=en,eng,English")
            cmd.append(f"--mpv-slang={sub_lang},en,eng,English")
            cmd.append("--mpv-sub-fix-timing=yes")
            if not subtitles_enabled:
                cmd.append("--mpv-sid=no")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--mpv-sub-delay={sub_delay}")
            cmd.append(f"--mpv-script={lua_script}")
            cmd.append(f"--mpv-script-opts={script_opts_str}")
            for sub in source.subtitles:
                cmd.append(f"--mpv-sub-file={sub.url}")
            if start_time and start_time > 10:
                cmd.append(f"--mpv-start={int(start_time)}")

        elif "vlc" in self.executable.lower():
            cmd.append(f"--meta-title={title_str}")
            cmd.append("--quiet")
            if not subtitles_enabled:
                cmd.append("--no-spu")
            ref = source.headers.get("Referer") or source.headers.get("referer")
            if ref:
                cmd.append(f"--http-referrer={ref}")
            ua = source.headers.get("User-Agent") or source.headers.get("user-agent")
            if ua:
                cmd.append(f"--http-user-agent={ua}")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--sub-delay={sub_delay}")
            for sub in source.subtitles:
                cmd.append(f"--sub-file={sub.url}")
            if start_time and start_time > 10:
                cmd.append(f"--start-time={int(start_time)}")

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

