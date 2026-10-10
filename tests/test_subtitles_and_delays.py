from __future__ import annotations

import concurrent.futures
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from dion.metadata.models import EpisodeItem, MediaItem, MediaType, StreamSource, SubtitleTrack
from dion.player.mpv import MpvPlayer
from dion.providers.manager import ProviderManager
from dion.providers.subtitles import SubtitleCandidate, SubtitleResolver
from dion.storage.history import HistoryManager
from dion.storage.settings import SettingsManager


class TestServerScopedSubtitleDelays(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.history_mgr = HistoryManager(config_dir=self.config_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_server_and_episode_scoped_persistence(self):
        # Save delay for server vidora-lookmovie on S01E02
        self.history_mgr.set_sub_delay(
            imdb_id="tt0903747",
            delay=1.25,
            season=1,
            episode=2,
            server="vidora-lookmovie",
        )

        # Save delay for server vixsrc on S01E02
        self.history_mgr.set_sub_delay(
            imdb_id="tt0903747",
            delay=-0.50,
            season=1,
            episode=2,
            server="vixsrc",
        )

        # Verify exact server scoped delays
        d_vidora = self.history_mgr.get_sub_delay(
            imdb_id="tt0903747",
            season=1,
            episode=2,
            server="vidora-lookmovie",
        )
        self.assertEqual(d_vidora, 1.25)

        d_vixsrc = self.history_mgr.get_sub_delay(
            imdb_id="tt0903747",
            season=1,
            episode=2,
            server="vixsrc",
        )
        self.assertEqual(d_vixsrc, -0.50)

    def test_fallback_hierarchy(self):
        # 1. Global movie fallback to imdb_id
        self.history_mgr.set_sub_delay(
            imdb_id="tt1375666",
            delay=0.75,
        )
        # Should fallback to 0.75 for any server if server-specific key doesn't exist
        self.assertEqual(
            self.history_mgr.get_sub_delay("tt1375666", server="vixsrc"),
            0.75,
        )

        # 2. Server-specific override for movie
        self.history_mgr.set_sub_delay(
            imdb_id="tt1375666",
            delay=1.50,
            server="vixsrc",
        )
        # vixsrc gets its specific offset
        self.assertEqual(
            self.history_mgr.get_sub_delay("tt1375666", server="vixsrc"),
            1.50,
        )
        # other server still gets global fallback
        self.assertEqual(
            self.history_mgr.get_sub_delay("tt1375666", server="vidora"),
            0.75,
        )

        # 3. Episode fallback without server
        self.history_mgr.set_sub_delay(
            imdb_id="tt0903747",
            delay=-0.20,
            season=2,
            episode=5,
        )
        # Querying with an unconfigured server should fallback to episode key
        self.assertEqual(
            self.history_mgr.get_sub_delay("tt0903747", season=2, episode=5, server="vidora"),
            -0.20,
        )

        # 4. Unknown title returns 0.0
        self.assertEqual(
            self.history_mgr.get_sub_delay("tt9999999", season=1, episode=1, server="vixsrc"),
            0.0,
        )

    def test_legacy_sub_delays_file_compatibility(self):
        # Pre-seed legacy format where key is just imdb_id
        delays_file = self.config_dir / "sub_delays.json"
        with open(delays_file, "w", encoding="utf-8") as f:
            json.dump({"tt0816692": -1.1}, f)

        # HistoryManager should seamlessly read legacy entry as fallback
        delay = self.history_mgr.get_sub_delay("tt0816692", server="vixsrc")
        self.assertEqual(delay, -1.1)


class TestSubtitleExtractionAndMatching(unittest.TestCase):
    def setUp(self):
        self.resolver = SubtitleResolver(timeout=2.0)

    def tearDown(self):
        self.resolver.close()

    def test_matches_lang(self):
        pref_set = {"en", "eng", "english"}
        self.assertTrue(self.resolver._matches_lang("English", "en", pref_set))
        self.assertTrue(self.resolver._matches_lang("English", "eng", pref_set))
        self.assertTrue(self.resolver._matches_lang("en", "en", pref_set))
        self.assertFalse(self.resolver._matches_lang("Spanish", "es", pref_set))
        self.assertFalse(self.resolver._matches_lang("French", "fra", pref_set))

    def test_extract_subtitle_text_utf8(self):
        sample = "1\n00:00:01,000 --> 00:00:04,000\nHello World\n"
        extracted = self.resolver._extract_subtitle_text(sample.encode("utf-8"))
        self.assertIsNotNone(extracted)
        self.assertIn("Hello World", extracted)

    def test_extract_subtitle_text_gzip(self):
        sample = "1\n00:00:01,000 --> 00:00:04,000\nGzipped Subtitle\n"
        compressed = gzip.compress(sample.encode("utf-8"))
        extracted = self.resolver._extract_subtitle_text(compressed)
        self.assertIsNotNone(extracted)
        self.assertIn("Gzipped Subtitle", extracted)

    def test_extract_subtitle_text_zip(self):
        sample = "1\n00:00:01,000 --> 00:00:04,000\nZipped Subtitle\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("test.srt", sample.encode("utf-8"))
        extracted = self.resolver._extract_subtitle_text(buf.getvalue())
        self.assertIsNotNone(extracted)
        self.assertIn("Zipped Subtitle", extracted)


class TestAsyncPrefetchPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.settings_mgr = SettingsManager(config_dir=self.config_dir)
        self.resolver = SubtitleResolver(timeout=2.0, settings_mgr=self.settings_mgr)

    def tearDown(self):
        self.resolver.close()
        self.temp_dir.cleanup()

    def test_prefetch_returns_future_immediately(self):
        with patch.object(
            self.resolver,
            "_resolve_subtitles",
            return_value=[SubtitleTrack(url="/fake/sub.srt", label="English", lang="en")],
        ):
            future = self.resolver.prefetch_subtitles("tt1375666")
            self.assertIsInstance(future, concurrent.futures.Future)
            # Re-calling prefetch reuses the in-flight future
            future2 = self.resolver.prefetch_subtitles("tt1375666")
            self.assertIs(future, future2)

            # Wait on result
            results = future.result(timeout=1.0)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].url, "/fake/sub.srt")

    def test_get_subtitles_waits_for_prefeteched_future(self):
        with patch.object(
            self.resolver,
            "_resolve_subtitles",
            return_value=[SubtitleTrack(url="/fake/sub.srt", label="English (WEB-DL)", lang="en")],
        ):
            # Start prefetch
            self.resolver.prefetch_subtitles("tt0903747", season=1, episode=1)
            # get_subtitles uses the in-flight future
            subs = self.resolver.get_subtitles("tt0903747", season=1, episode=1)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].label, "English (WEB-DL)")


class TestMultiAPIProviderFallback(unittest.TestCase):
    def setUp(self):
        self.resolver = SubtitleResolver(timeout=2.0)

    def tearDown(self):
        self.resolver.close()

    def test_fetch_opensubtitles_v3_parsing(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subtitles": [
                {
                    "id": "12345",
                    "url": "https://subs.strem.io/test.srt",
                    "lang": "eng",
                    "subtitleFileName": "Inception.2010.1080p.WEB-DL.srt",
                    "movieReleaseName": "Inception.2010.1080p.WEB-DL",
                    "releaseGroup": "FLUX",
                    "releaseFormat": "WEB-DL",
                },
                {
                    "id": "67890",
                    "url": "https://subs.strem.io/spanish.srt",
                    "lang": "spa",
                    "subtitleFileName": "Inception.spa.srt",
                    "movieReleaseName": "Inception",
                    "releaseGroup": "",
                    "releaseFormat": "",
                },
            ]
        }

        with patch.object(self.resolver.client, "get", return_value=mock_response):
            candidates = self.resolver._fetch_opensubtitles_v3(
                "tt1375666", "1375666", None, None, {"en", "eng"}
            )
            # Spanish track filtered out by preferred_langs
            self.assertEqual(len(candidates), 1)
            c = candidates[0]
            self.assertEqual(c.provider, "os_v3")
            self.assertEqual(c.sub_id, "os3_12345")
            self.assertEqual(c.release_format, "WEB-DL")
            self.assertEqual(c.release_group, "FLUX")

    def test_fetch_wyzie_parsing(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "media": "Inception",
                "display": "English",
                "language": "en",
                "format": "srt",
                "source": "PSA",
                "url": "https://sub.wyzie.io/c/123",
            }
        ]

        with patch.object(self.resolver.client, "get", return_value=mock_response):
            candidates = self.resolver._fetch_wyzie("tt1375666", None, None, {"en", "eng"})
            self.assertEqual(len(candidates), 1)
            c = candidates[0]
            self.assertEqual(c.provider, "wyzie")
            self.assertEqual(c.lang, "English")
            self.assertEqual(c.release_group, "PSA")

    def test_fetch_subdl_parsing(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": True,
            "subtitles": [
                {
                    "release_name": "Inception.2010.1080p.BluRay",
                    "unpack_files": [
                        {
                            "name": "Inception.srt",
                            "url": "https://dl.subdl.com/subtitle/1/2.srt",
                            "language": "English",
                        }
                    ],
                }
            ],
        }

        with patch.object(self.resolver.client, "get", return_value=mock_response):
            candidates = self.resolver._fetch_subdl("tt1375666", None, None, {"en", "eng"})
            self.assertEqual(len(candidates), 1)
            c = candidates[0]
            self.assertEqual(c.provider, "subdl")
            self.assertEqual(c.file_name, "Inception.srt")


class TestProviderManagerPrefetchIntegration(unittest.TestCase):
    def test_provider_manager_prefetches_and_attaches(self):
        pm = ProviderManager()
        pm.subtitle_resolver = MagicMock()
        mock_sub = SubtitleTrack(url="/path/sub.srt", label="English", lang="en")
        pm.subtitle_resolver.get_subtitles.return_value = [mock_sub]

        mock_provider = MagicMock()
        mock_provider.resolve_movie.return_value = [
            StreamSource(url="https://video.mp4", server="vixsrc")
        ]
        pm.providers = [mock_provider]

        sources = pm.resolve_movie("tt1375666", "Inception", "2010")

        # Verified prefetch was initiated
        pm.subtitle_resolver.prefetch_subtitles.assert_called_once_with(
            imdb_id="tt1375666", released="2010"
        )
        self.assertEqual(len(sources), 1)
        self.assertEqual(len(sources[0].subtitles), 1)
        self.assertEqual(sources[0].subtitles[0].url, "/path/sub.srt")


class TestMpvScriptOptsAndServerScoping(unittest.TestCase):
    def test_script_opts_contains_server(self):
        player = MpvPlayer()
        source = StreamSource(url="https://video.mp4", server="Vidora-Lookmovie")
        media = MediaItem(
            id="tt0903747",
            imdb_id="tt0903747",
            title="Breaking Bad",
            media_type=MediaType.SERIES,
        )
        episode = EpisodeItem(
            id="tt0903747:1:1",
            season=1,
            episode=1,
            title="Pilot",
        )

        with patch.object(player, "executable", "/usr/local/bin/mpv"), \
             patch("subprocess.run") as mock_run, \
             patch.object(player, "_ensure_lua_script", return_value=Path("/tmp/dion.lua")):
            mock_run.return_value = MagicMock(returncode=0)

            player.play(source=source, media=media, episode=episode)

            cmd_called = mock_run.call_args[0][0]
            script_opts_arg = [arg for arg in cmd_called if arg.startswith("--script-opts=")][0]

            self.assertIn("dion_imdb=tt0903747", script_opts_arg)
            self.assertIn("dion_server=vidora-lookmovie", script_opts_arg)
            self.assertIn("dion_season=1", script_opts_arg)
            self.assertIn("dion_episode=1", script_opts_arg)


if __name__ == "__main__":
    unittest.main()
