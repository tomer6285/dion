from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.parse

import httpx
from rich.console import Console

from ..metadata.models import EpisodeItem, MediaItem, StreamSource, SubtitleTrack
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
local server = mp.get_opt("dion_server", ""):lower()

local delays_file = (config_dir ~= "") and (config_dir .. "/sub_delays.json") or nil
local pos_file = (config_dir ~= "") and (config_dir .. "/playback_positions.json") or nil
local dur_file = (config_dir ~= "") and (config_dir .. "/playback_durations.json") or nil

local pos_key = imdb_id
if season ~= "" and episode ~= "" then
    pos_key = imdb_id .. ":" .. season .. ":" .. episode
end

local delay_key = imdb_id
if season ~= "" and episode ~= "" and server ~= "" then
    delay_key = imdb_id .. ":" .. season .. ":" .. episode .. ":" .. server
elseif server ~= "" then
    delay_key = imdb_id .. ":" .. server
elseif season ~= "" and episode ~= "" then
    delay_key = imdb_id .. ":" .. season .. ":" .. episode
end

local function load_saved_delay()
    if not delays_file or imdb_id == "" then return 0.0 end
    local f = io.open(delays_file, "r")
    if not f then return 0.0 end
    local content = f:read("*all")
    f:close()
    if not content or content == "" then return 0.0 end
    local data = {}
    for k, v in content:gmatch('"([^"]+)"%s*:%s*([%-0-9%.]+)') do
        data[k] = tonumber(v)
    end
    local candidates = {}
    if season ~= "" and episode ~= "" and server ~= "" then
        table.insert(candidates, imdb_id .. ":" .. season .. ":" .. episode .. ":" .. server)
    end
    if server ~= "" then
        table.insert(candidates, imdb_id .. ":" .. server)
    end
    if season ~= "" and episode ~= "" then
        table.insert(candidates, imdb_id .. ":" .. season .. ":" .. episode)
    end
    table.insert(candidates, imdb_id)
    for _, k in ipairs(candidates) do
        if data[k] ~= nil then
            return data[k]
        end
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
        if content then
            for k, v in content:gmatch('"([^"]+)"%s*:%s*([%-0-9%.]+)') do
                data[k] = tonumber(v)
            end
        end
    end
    data[delay_key] = delay
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

local function save_duration(dur)
    if not dur_file or pos_key == "" or not dur or dur <= 0 then return end
    local data = {}
    local f = io.open(dur_file, "r")
    if f then
        local content = f:read("*all")
        f:close()
        if content then
            for k, v in content:gmatch('"([^"]+)"%s*:%s*([%-0-9%.]+)') do
                data[k] = tonumber(v)
            end
        end
    end
    data[pos_key] = math.floor(dur * 10 + 0.5) / 10
    local out = io.open(dur_file, "w")
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

local function save_position(force_pos)
    if not pos_file or pos_key == "" then return end
    local pos = force_pos or mp.get_property_number("time-pos", 0.0)
    local duration = mp.get_property_number("duration", 0.0)
    if duration and duration > 0 then
        save_duration(duration)
    end
    if force_pos == nil then
        if not pos or pos <= 0 then return end
        if duration and duration > 60 and (pos >= duration - 60 or pos >= duration * 0.95) then
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

    mp.add_timeout(1.0, function()
        local subs_file = (config_dir ~= "") and (config_dir .. "/secondary_subs.json") or nil
        if subs_file then
            local f = io.open(subs_file, "r")
            if f then
                local content = f:read("*all")
                f:close()
                if content and content ~= "" then
                    for url, label, lang in content:gmatch('"url"%s*:%s*"([^"]+)"%s*,%s*"label"%s*:%s*"([^"]*)"%s*,%s*"lang"%s*:%s*"([^"]*)"') do
                        mp.commandv("sub-add", url, "auto", (label ~= "") and label or nil, (lang ~= "") and lang or nil)
                    end
                end
            end
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

-- Observe duration property: save duration as soon as player demuxes stream length
mp.observe_property("duration", "number", function(name, dur)
    if dur and dur > 0 then
        save_duration(dur)
    end
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
        if not script_file.exists() or script_file.read_text(encoding="utf-8") != lua_code:
            script_file.write_text(lua_code, encoding="utf-8")
        return script_file

    def _is_matching_sub(self, sub: SubtitleTrack, pref_set: Set[str]) -> bool:
        """Check if an external subtitle track matches user language preferences."""
        l_clean = (sub.lang or "").strip().lower()
        lbl_clean = (sub.label or "").strip().lower()
        if l_clean in pref_set:
            return True
        pattern = r"\b(" + "|".join(map(re.escape, pref_set)) + r")\b"
        if re.search(pattern, lbl_clean, re.IGNORECASE):
            return True
        return False

    def _prune_hls_playlist(
        self, source: StreamSource, preferred_lang: str
    ) -> Optional[Path]:
        """Prune multi-variant HLS master playlists to avoid FFmpeg downloading dozens of unused tracks."""
        if not source.is_hls:
            return None
        if not (".m3u8" in source.url or "playlist" in source.url or "/hls/" in source.url):
            return None

        try:
            resp = httpx.get(
                source.url,
                headers=source.headers,
                timeout=3.0,
                follow_redirects=True,
            )
            if resp.status_code != 200 or not resp.text:
                return None
            text = resp.text
        except Exception:
            return None

        # Only prune if this is a master playlist defining variants or media tracks
        if "#EXT-X-STREAM-INF" not in text and "#EXT-X-MEDIA" not in text:
            return None

        try:
            pref_set = {preferred_lang.strip().lower(), "en", "eng", "english"}
            pattern_word = r"\b(" + "|".join(map(re.escape, pref_set)) + r")\b"

            def is_match(lang_str: str, name_str: str) -> bool:
                l_clean = lang_str.strip().lower()
                n_clean = name_str.strip().lower()
                if l_clean in pref_set:
                    return True
                if re.search(pattern_word, n_clean, re.IGNORECASE):
                    return True
                return False

            def abs_uri(rel_url: str) -> str:
                if not re.match(r"^[a-zA-Z]+://", rel_url):
                    return urllib.parse.urljoin(source.url, rel_url)
                return rel_url

            def fix_media_uri(line_str: str) -> str:
                m = re.search(r'URI="([^"]+)"', line_str)
                if m:
                    full = abs_uri(m.group(1))
                    return line_str[:m.start(1)] + full + line_str[m.end(1):]
                return line_str

            lines = text.splitlines()
            kept_subs: List[str] = []
            kept_audio: List[str] = []
            default_audio: Optional[str] = None

            for line in lines:
                if line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES"):
                    lang_m = re.search(r'LANGUAGE="([^"]+)"', line, re.IGNORECASE)
                    name_m = re.search(r'NAME="([^"]+)"', line, re.IGNORECASE)
                    lang_val = lang_m.group(1) if lang_m else ""
                    name_val = name_m.group(1) if name_m else ""
                    if is_match(lang_val, name_val):
                        kept_subs.append(fix_media_uri(line))

                elif line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                    lang_m = re.search(r'LANGUAGE="([^"]+)"', line, re.IGNORECASE)
                    name_m = re.search(r'NAME="([^"]+)"', line, re.IGNORECASE)
                    lang_val = lang_m.group(1) if lang_m else ""
                    name_val = name_m.group(1) if name_m else ""
                    if "DEFAULT=YES" in line.upper() or default_audio is None:
                        default_audio = fix_media_uri(line)
                    if is_match(lang_val, name_val):
                        mod = re.sub(r"DEFAULT=[A-Za-z]+", "DEFAULT=YES", line)
                        mod = re.sub(r"AUTOSELECT=[A-Za-z]+", "AUTOSELECT=YES", mod)
                        kept_audio.append(fix_media_uri(mod))

            if not kept_audio and default_audio:
                kept_audio.append(default_audio)

            # Parse video streams (#EXT-X-STREAM-INF followed by URL)
            streams: List[Tuple[int, str, str]] = []
            i = 0
            while i < len(lines):
                line = lines[i]
                if line.startswith("#EXT-X-STREAM-INF"):
                    inf = line
                    url = None
                    j = i + 1
                    while j < len(lines):
                        if lines[j].strip() and not lines[j].startswith("#"):
                            url = lines[j].strip()
                            break
                        j += 1
                    if url:
                        bw_m = re.search(r"BANDWIDTH=([0-9]+)", inf)
                        bw = int(bw_m.group(1)) if bw_m else 0
                        streams.append((bw, inf, abs_uri(url)))
                    i = j
                i += 1

            if streams:
                streams.sort(key=lambda s: s[0], reverse=True)
                top_stream = streams[0]
            else:
                top_stream = None

            out: List[str] = ["#EXTM3U"]
            for l in lines:
                if l.startswith("#EXT-X-VERSION"):
                    out.append(l)

            for a in kept_audio:
                out.append(a)
            for s in kept_subs:
                out.append(s)

            if top_stream:
                inf_line = top_stream[1]
                if not kept_subs:
                    inf_line = re.sub(r',?SUBTITLES="[^"]*"', "", inf_line)
                out.append(inf_line)
                out.append(top_stream[2])

            pruned_text = "\n".join(out)
            temp_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".m3u8", delete=False, encoding="utf-8"
            )
            temp_file.write(pruned_text)
            temp_file.close()
            return Path(temp_file.name)
        except Exception:
            return None

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
            sub_delay = HistoryManager(config_dir).get_sub_delay(
                media.imdb_id,
                season=episode.season if episode else None,
                episode=episode.episode if episode else None,
                server=source.server if source else None,
            )

        lua_script = self._ensure_lua_script()

        # Subtitle preferences from settings
        subtitles_enabled = self.settings_mgr.subtitles_enabled
        sub_lang = self.settings_mgr.sub_lang or "en"

        # Solution 1: Prune multi-variant HLS master playlists to eliminate sequential sub-playlist downloads
        pruned_file: Optional[Path] = None
        play_url = source.url
        if is_mpv or is_iina:
            pruned_file = self._prune_hls_playlist(source, sub_lang)
            if pruned_file:
                play_url = str(pruned_file)

        # Solution 2: Select primary external subtitle for instant launch and queue remainder for background loading
        pref_set = {sub_lang.strip().lower(), "en", "eng", "english"}
        primary_subs: List[SubtitleTrack] = []
        secondary_subs: List[SubtitleTrack] = []

        if source.subtitles:
            for s in source.subtitles:
                if self._is_matching_sub(s, pref_set):
                    if not primary_subs:
                        primary_subs.append(s)
                    else:
                        secondary_subs.append(s)
                else:
                    secondary_subs.append(s)

            if not primary_subs and source.subtitles:
                primary_subs.append(source.subtitles[0])
                secondary_subs = source.subtitles[1:]

        # Save secondary subtitles for background loading via Lua engine
        secondary_file = config_dir / "secondary_subs.json"
        if secondary_subs:
            try:
                sub_data = [
                    {"url": s.url, "label": s.label, "lang": s.lang}
                    for s in secondary_subs
                ]
                secondary_file.write_text(json.dumps(sub_data), encoding="utf-8")
            except Exception:
                pass
        else:
            if secondary_file.exists():
                try:
                    secondary_file.unlink(missing_ok=True)
                except Exception:
                    pass

        cmd = [self.executable]

        # Prepare script-opts
        script_opts = [
            f"dion_imdb={media.imdb_id}",
            f"dion_config={config_dir}",
        ]
        if source and source.server:
            script_opts.append(f"dion_server={source.server.strip().lower()}")
        if episode:
            script_opts.append(f"dion_season={episode.season}")
            script_opts.append(f"dion_episode={episode.episode}")
        script_opts_str = ",".join(script_opts)

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

            # Stream connection stability & protocol whitelist for pruned local playlists
            lavf_opts = ["reconnect=1", "reconnect_delay_max=2"]
            if pruned_file:
                lavf_opts.insert(0, 'protocol_whitelist="file,http,https,tcp,tls,crypto"')
            cmd.append(f"--demuxer-lavf-o={','.join(lavf_opts)}")
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
            cmd.append("--sub-clear-on-seek=yes")
            if not subtitles_enabled:
                cmd.append("--sid=no")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--sub-delay={sub_delay}")

            # Attach Dion Lua script and pass context parameters
            cmd.append(f"--script={lua_script}")
            cmd.append(f"--script-opts={script_opts_str}")

            # Add primary subtitles (secondary loaded in background by Lua script)
            for sub in primary_subs:
                cmd.append(f"--sub-file={sub.url}")

            # Resume position if provided
            if start_time and start_time > 10:
                cmd.append(f"--start={int(start_time)}")

        elif is_iina:
            cmd.append(f"--mpv-title=Dion: {title_str}")
            cmd.append("--mpv-hwdec=auto")
            cmd.append("--mpv-profile=fast")
            cmd.append("--mpv-really-quiet")
            lavf_opts = ["reconnect=1", "reconnect_delay_max=2"]
            if pruned_file:
                lavf_opts.insert(0, 'protocol_whitelist="file,http,https,tcp,tls,crypto"')
            cmd.append(f"--mpv-demuxer-lavf-o={','.join(lavf_opts)}")
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
            cmd.append("--mpv-sub-clear-on-seek=yes")
            if not subtitles_enabled:
                cmd.append("--mpv-sid=no")
            if sub_delay and abs(sub_delay) > 0.01:
                cmd.append(f"--mpv-sub-delay={sub_delay}")
            cmd.append(f"--mpv-script={lua_script}")
            cmd.append(f"--mpv-script-opts={script_opts_str}")
            for sub in primary_subs:
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
            for sub in primary_subs:
                cmd.append(f"--sub-file={sub.url}")
            if start_time and start_time > 10:
                cmd.append(f"--start-time={int(start_time)}")

        # Target video URL (pruned playlist or original stream URL)
        cmd.append(play_url)

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
        finally:
            if pruned_file and pruned_file.exists():
                try:
                    pruned_file.unlink(missing_ok=True)
                except Exception:
                    pass

