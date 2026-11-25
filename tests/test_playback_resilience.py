"""Regression tests for VixSrc URL generation, mpv options/errors, and provider fallback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from dion.metadata.models import EpisodeItem, MediaItem, MediaType, StreamSource
from dion.player.mpv import MpvPlayer
from dion.providers.manager import ProviderManager
from dion.providers.vixsrc import VixSrcProvider
from dion.storage.history import HistoryManager
from dion.storage.settings import SettingsManager


class TestVixSrcPlaylistParams(unittest.TestCase):
    def _mock_responses(self, embed_html: str):
        api_resp = MagicMock(status_code=200)
        api_resp.json.return_value = {"src": "/embed/314921?token=abc"}
        embed_resp = MagicMock(status_code=200, text=embed_html)
        return [api_resp, embed_resp]

    def test_omits_h_param_when_can_play_fhd_is_false(self):
        html = """
        <script>
            window.streams = [
                {"name": "Server1", "active": true, "url": "https://vixsrc.to/playlist/314921?cf=1"},
                {"name": "Server2", "active": false, "url": "https://vixsrc.to/playlist/314921?cf=0"}
            ];
            window.masterPlaylist = {
                params: {'token': 'tok123', 'expires': '9999999999'},
                url: 'https://vixsrc.to/playlist/314921'
            };
            window.canPlayFHD = false
        </script>
        """
        provider = VixSrcProvider()
        with patch.object(provider.client, "get", side_effect=self._mock_responses(html)):
            sources = provider.resolve_episode("tt0455275", 1, 1, "Prison Break")

        self.assertEqual(len(sources), 2)
        self.assertNotIn("&h=1", sources[0].url)
        self.assertIn("token=tok123&expires=9999999999", sources[0].url)
        self.assertEqual(sources[0].headers["Referer"], "https://vixsrc.to/embed/314921?token=abc")

    def test_includes_h_param_when_can_play_fhd_is_true(self):
        html = """
        <script>
            window.streams = [
                {"name": "Server1", "active": true, "url": "https://vixsrc.to/playlist/314921?cf=1"}
            ];
            window.masterPlaylist = {
                params: {'token': 'tok123', 'expires': '9999999999'},
                url: 'https://vixsrc.to/playlist/314921'
            };
            window.canPlayFHD = true
        </script>
        """
        provider = VixSrcProvider()
        with patch.object(provider.client, "get", side_effect=self._mock_responses(html)):
            sources = provider.resolve_movie("tt1375666", "Inception")

        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0].url.endswith("&h=1"))


class TestMpvLavfWhitelistAndErrors(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_mgr = SettingsManager(config_dir=Path(self.temp_dir.name))
        self.player = MpvPlayer(executable="/usr/bin/mpv", settings_mgr=self.settings_mgr)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pruned_playlist_uses_lavf_append_without_quotes(self):
        pruned_tmp = Path(self.temp_dir.name) / "pruned.m3u8"
        pruned_tmp.write_text("#EXTM3U\n", encoding="utf-8")

        source = StreamSource(url="https://example.com/master.m3u8", server="vixsrc-server1", is_hls=True)
        media = MediaItem(id="tt0455275", imdb_id="tt0455275", title="Prison Break", media_type=MediaType.SERIES)

        with patch.object(self.player, "_prune_hls_playlist", return_value=pruned_tmp), \
             patch("dion.player.mpv.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            code = self.player.play(source=source, media=media)

        self.assertEqual(code, 0)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--hwdec=auto-safe", cmd)
        self.assertIn("--demuxer-lavf-o=reconnect=1,reconnect_delay_max=2", cmd)
        self.assertIn("--demuxer-lavf-o-append=protocol_whitelist=file,http,https,tcp,tls,crypto", cmd)
        self.assertFalse(any('protocol_whitelist="' in arg for arg in cmd))

    def test_nonzero_exit_prints_stderr_details(self):
        source = StreamSource(url="https://example.com/video.mp4", server="vidlink", is_hls=False)
        media = MediaItem(id="tt1375666", imdb_id="tt1375666", title="Inception", media_type=MediaType.MOVIE)

        with patch("dion.player.mpv.subprocess.run") as mock_run, \
             patch("dion.player.mpv.console.print") as mock_print:
            mock_run.return_value = MagicMock(returncode=2, stderr="HTTP error 403 Forbidden\nFailed to open.\n")
            code = self.player.play(source=source, media=media)

        self.assertEqual(code, 2)
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("HTTP error 403 Forbidden", printed)


class TestProviderFallback(unittest.TestCase):
    def test_provider_manager_exclude_providers_skips_failed_provider(self):
        pm = ProviderManager()
        pm.subtitle_resolver = MagicMock()

        p1 = MagicMock()
        p1.name = "vixsrc"
        p1.resolve_episode.return_value = [StreamSource(url="https://a.m3u8", server="vixsrc-server1")]

        p2 = MagicMock()
        p2.name = "vidlink"
        p2.resolve_episode.return_value = [StreamSource(url="https://b.mp4", server="vidlink", is_hls=False)]

        pm.providers = [p1, p2]
        with patch.object(pm, "_attach_subtitles"):
            sources = pm.resolve_episode(
                "tt0455275", 1, 1, "Prison Break", exclude_providers={"vixsrc"}
            )

        p1.resolve_episode.assert_not_called()
        p2.resolve_episode.assert_called_once()
        self.assertEqual(sources[0].server, "vidlink")

    def test_play_or_download_falls_back_to_next_provider(self):
        from dion.cli import play_or_download

        with tempfile.TemporaryDirectory() as tmp:
            mgr = HistoryManager(Path(tmp))
            pm = ProviderManager()
            pm.subtitle_resolver = MagicMock()

            p1 = MagicMock()
            p1.name = "vixsrc"
            p1.resolve_episode.return_value = [
                StreamSource(url="https://bad1.m3u8", server="vixsrc-server1", is_hls=False),
                StreamSource(url="https://bad2.m3u8", server="vixsrc-server2", is_hls=False),
            ]
            p2 = MagicMock()
            p2.name = "vidlink"
            p2.resolve_episode.return_value = [
                StreamSource(url="https://good.mp4", server="vidlink", is_hls=False)
            ]
            pm.providers = [p1, p2]

            mock_player = MagicMock()
            mock_player.play.side_effect = [1, 1, 0]

            media = MediaItem(id="tt0455275", imdb_id="tt0455275", title="Prison Break", media_type=MediaType.SERIES)
            ep = EpisodeItem(id="tt0455275:1:1", season=1, episode=1, title="Pilot")

            with patch("dion.cli.get_history_mgr", return_value=mgr), \
                 patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
                 patch("dion.cli.get_provider_manager", return_value=pm), \
                 patch("dion.cli.get_player", return_value=mock_player), \
                 patch.object(pm, "_attach_subtitles"):
                mock_auth.return_value.is_authenticated.return_value = False
                success = play_or_download(media, episode=ep, download=False, auto_select=True)

            self.assertTrue(success)
            self.assertEqual(mock_player.play.call_count, 3)
            self.assertEqual(mock_player.play.call_args_list[2].kwargs["source"].server, "vidlink")
