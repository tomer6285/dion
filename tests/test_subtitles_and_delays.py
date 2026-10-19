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


class TestNativeSubtitlesDetectionAndFixA(unittest.TestCase):
    def setUp(self):
        self.pm = ProviderManager()

    def test_has_hls_embedded_subtitles_match(self):
        sample_hls = (
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="eng"\n'
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English [CC]",DEFAULT=NO,AUTOSELECT=NO,LANGUAGE="eng",URI="sub_eng.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Greek",LANGUAGE="gre",URI="sub_gre.m3u8"\n'
        )
        source = StreamSource(url="https://example.com/stream.m3u8", server="vixsrc", is_hls=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = sample_hls

        with patch("httpx.get", return_value=mock_resp):
            # Matches English
            self.assertTrue(self.pm._has_hls_embedded_subtitles(source, {"en", "eng", "english"}))
            # Matches Greek
            self.assertTrue(self.pm._has_hls_embedded_subtitles(source, {"gre", "el", "greek"}))
            # Does not match Spanish
            self.assertFalse(self.pm._has_hls_embedded_subtitles(source, {"es", "spa", "spanish"}))

    def test_has_native_subtitles_provider_tracks(self):
        source = StreamSource(
            url="https://example.com/video.mp4",
            server="vidlink",
            subtitles=[
                SubtitleTrack(url="https://sub.com/ar.srt", label="Arabic", lang="ar"),
                SubtitleTrack(url="https://sub.com/en.srt", label="English", lang="eng"),
            ],
        )
        self.assertTrue(self.pm._has_native_subtitles(source, {"en", "eng"}))
        self.assertFalse(self.pm._has_native_subtitles(source, {"fr", "fra"}))

    def test_prioritize_subtitles_reorders_preferred_to_front(self):
        sub_ar = SubtitleTrack(url="https://sub.com/ar.srt", label="Arabic", lang="ar")
        sub_en = SubtitleTrack(url="https://sub.com/en.srt", label="English", lang="eng")
        sub_fr = SubtitleTrack(url="https://sub.com/fr.srt", label="French", lang="fra")

        reordered = self.pm._prioritize_subtitles([sub_ar, sub_en, sub_fr], {"en", "eng"})
        self.assertEqual(reordered[0], sub_en)
        self.assertIn(sub_ar, reordered[1:])
        self.assertIn(sub_fr, reordered[1:])

    def test_attach_subtitles_skips_when_hls_has_native(self):
        source = StreamSource(url="https://example.com/stream.m3u8", server="vixsrc", is_hls=True)
        self.pm.subtitle_resolver = MagicMock()

        with patch.object(self.pm, "_has_hls_embedded_subtitles", return_value=True):
            self.pm._attach_subtitles([source], "tt1632701", preferred_langs=["en", "eng"])
            # Resolver was NOT called because stream already has native synced subtitles
            self.pm.subtitle_resolver.get_subtitles.assert_not_called()
            self.assertEqual(source.subtitles, [])

    def test_attach_subtitles_fetches_when_stream_lacks_language(self):
        source = StreamSource(url="https://example.com/stream.m3u8", server="vixsrc", is_hls=True)
        self.pm.subtitle_resolver = MagicMock()
        mock_sub = SubtitleTrack(url="/cached/sub.srt", label="Spanish", lang="es")
        self.pm.subtitle_resolver.get_subtitles.return_value = [mock_sub]

        with patch.object(self.pm, "_has_hls_embedded_subtitles", return_value=False):
            self.pm._attach_subtitles([source], "tt1632701", preferred_langs=["es", "spa"])
            # Resolver WAS called because native subtitles lacked Spanish
            self.pm.subtitle_resolver.get_subtitles.assert_called_once()
            self.assertEqual(len(source.subtitles), 1)
            self.assertEqual(source.subtitles[0].url, "/cached/sub.srt")


class TestMpvStartupOptimizations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.settings_mgr = SettingsManager(config_dir=self.config_dir)
        self.player = MpvPlayer(settings_mgr=self.settings_mgr)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_matching_sub(self):
        pref_set = {"en", "eng", "english"}
        sub_en = SubtitleTrack(url="https://sub.com/en.vtt", label="English [CC]", lang="eng")
        sub_fr = SubtitleTrack(url="https://sub.com/fr.vtt", label="French", lang="fre")
        sub_es = SubtitleTrack(url="https://sub.com/es.vtt", label="Spanish", lang="spa")

        self.assertTrue(self.player._is_matching_sub(sub_en, pref_set))
        self.assertFalse(self.player._is_matching_sub(sub_fr, pref_set))
        self.assertFalse(self.player._is_matching_sub(sub_es, pref_set))

    def test_prune_hls_playlist(self):
        sample_master = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Italian",DEFAULT=YES,AUTOSELECT=YES,LANGUAGE="ita",URI="audio/ita.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=NO,AUTOSELECT=NO,LANGUAGE="eng",URI="audio/eng.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English [CC]",LANGUAGE="eng",URI="sub/eng.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="French",LANGUAGE="fre",URI="sub/fre.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Spanish",LANGUAGE="spa",URI="sub/spa.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080,AUDIO="audio",SUBTITLES="subs"\n'
            "video/1080p.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720,AUDIO="audio",SUBTITLES="subs"\n'
            "video/720p.m3u8\n"
        )
        source = StreamSource(url="https://example.com/live/master.m3u8", server="vixsrc", is_hls=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = sample_master

        with patch("httpx.get", return_value=mock_resp):
            pruned_path = self.player._prune_hls_playlist(source, preferred_lang="en")
            self.assertIsNotNone(pruned_path)
            try:
                content = pruned_path.read_text(encoding="utf-8")
                # English audio kept and marked default
                self.assertIn("LANGUAGE=\"eng\"", content)
                self.assertIn("DEFAULT=YES", content)
                # English subtitle kept
                self.assertIn("English [CC]", content)
                # French and Spanish subtitles omitted
                self.assertNotIn("French", content)
                self.assertNotIn("Spanish", content)
                # Italian audio omitted
                self.assertNotIn("Italian", content)
                # Top quality video kept with absolute URL
                self.assertIn("https://example.com/live/video/1080p.m3u8", content)
                # Lower quality 720p omitted
                self.assertNotIn("720p.m3u8", content)
            finally:
                pruned_path.unlink(missing_ok=True)

    def test_selective_subtitle_loading_in_play(self):
        source = StreamSource(
            url="https://example.com/video.mp4",
            server="vidlink",
            is_hls=False,
            subtitles=[
                SubtitleTrack(url="https://sub.com/ar.vtt", label="Arabic", lang="ar"),
                SubtitleTrack(url="https://sub.com/en.vtt", label="English", lang="eng"),
                SubtitleTrack(url="https://sub.com/es.vtt", label="Spanish", lang="spa"),
            ],
        )
        media = MediaItem(
            id="tt0111161",
            imdb_id="tt0111161",
            title="The Shawshank Redemption",
            media_type=MediaType.MOVIE,
        )

        with patch.object(self.player, "executable", "/usr/local/bin/mpv"), \
             patch("subprocess.run") as mock_run, \
             patch.object(self.player, "_ensure_lua_script", return_value=Path("/tmp/dion.lua")), \
             patch("pathlib.Path.home", return_value=self.config_dir.parent):
            mock_run.return_value = MagicMock(returncode=0)

            self.player.play(source=source, media=media)

            cmd_called = mock_run.call_args[0][0]

            # Only 1 sub-file passed on command line at launch (the matching English one)
            sub_args = [arg for arg in cmd_called if arg.startswith("--sub-file=")]
            self.assertEqual(len(sub_args), 1)
            self.assertEqual(sub_args[0], "--sub-file=https://sub.com/en.vtt")

            # Secondary subtitles saved to JSON for background Lua loading
            sec_file = Path.home() / ".config" / "dion" / "secondary_subs.json"
            if sec_file.exists():
                data = json.loads(sec_file.read_text(encoding="utf-8"))
                urls = [d["url"] for d in data]
                self.assertIn("https://sub.com/ar.vtt", urls)
                self.assertIn("https://sub.com/es.vtt", urls)
                self.assertNotIn("https://sub.com/en.vtt", urls)




class TestSubtitleTagWordBoundariesAndDurationSanity(unittest.TestCase):
    def setUp(self):
        self.pm = ProviderManager()
        self.resolver = self.pm.subtitle_resolver

    def test_word_boundary_does_not_penalize_suits_or_scrubs(self):
        c_suits = SubtitleCandidate(
            provider="os_v3",
            sub_id="1",
            url="http://sub/suits.srt",
            lang="en",
            lang_id="en",
            release_name="Suits.S03E01.1080p.WEB-DL.DD5.1.H.264-HWD",
            file_name="Suits.S03E01.1080p.WEB-DL.DD5.1.H.264-HWD.srt",
            release_format="WEB-DL",
        )
        c_scrubs = SubtitleCandidate(
            provider="os_v3",
            sub_id="2",
            url="http://sub/scrubs.srt",
            lang="en",
            lang_id="en",
            release_name="Scrubs.S01E01.HDTV",
            file_name="Scrubs.S01E01.HDTV.srt",
            release_format="HDTV",
        )
        c_telesync = SubtitleCandidate(
            provider="os_v3",
            sub_id="3",
            url="http://sub/ts.srt",
            lang="en",
            lang_id="en",
            release_name="Movie.2023.TS.X264",
            file_name="Movie.2023.TS.srt",
            release_format="TS",
        )

        with patch.object(self.resolver, "_fetch_opensubtitles_v3", return_value=[c_suits, c_telesync, c_scrubs]), \
             patch.object(self.resolver, "_fetch_wyzie", return_value=[]), \
             patch.object(self.resolver, "_fetch_subdl", return_value=[]), \
             patch.object(self.resolver, "_download_and_cache_parallel", side_effect=lambda cands, imdb: [
                 SubtitleTrack(url=c.url, label=c.release_name, lang=c.lang_id) for c in cands
             ]):
            selected = self.resolver._resolve_subtitles("tt1632701", season=3, episode=1)
            selected_urls = [s.url for s in selected]

            self.assertIn("http://sub/suits.srt", selected_urls)
            self.assertIn("http://sub/scrubs.srt", selected_urls)
            self.assertEqual(selected[0].url, "http://sub/suits.srt")

    def test_extract_subtitle_duration(self):
        sample_srt = (
            "1\n00:00:01,000 --> 00:00:04,500\nHello\n\n"
            "2\n00:43:02,000 --> 00:43:05,250\nGoodbye\n"
        )
        dur = self.resolver._extract_subtitle_duration(sample_srt)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, 43 * 60 + 5.25, places=2)

        self.assertIsNone(self.resolver._extract_subtitle_duration(""))
        self.assertIsNone(self.resolver._extract_subtitle_duration("No timestamps here"))

    def test_get_stream_duration_direct_hls(self):
        sample_media_hls = (
            "#EXTM3U\n"
            "#EXTINF:10.500,\nsegment1.ts\n"
            "#EXTINF:9.500,\nsegment2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        source = StreamSource(url="https://example.com/media.m3u8", server="test", is_hls=True)
        mock_resp = MagicMock(status_code=200, text=sample_media_hls)

        with patch("httpx.get", return_value=mock_resp):
            dur = self.pm._get_stream_duration(source)
            self.assertEqual(dur, 20.0)

    def test_get_stream_duration_master_playlist(self):
        sample_master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
            "variant/720p.m3u8\n"
        )
        sample_variant = (
            "#EXTM3U\n"
            "#EXTINF:15.000,\nseg1.ts\n"
            "#EXTINF:15.000,\nseg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        source = StreamSource(url="https://example.com/master.m3u8", server="test", is_hls=True)

        def mock_get(url, **kwargs):
            m = MagicMock(status_code=200)
            if "master.m3u8" in str(url):
                m.text = sample_master
                m.url = "https://example.com/master.m3u8"
            else:
                m.text = sample_variant
                m.url = "https://example.com/variant/720p.m3u8"
            return m

        with patch("httpx.get", side_effect=mock_get):
            dur = self.pm._get_stream_duration(source)
            self.assertEqual(dur, 30.0)

    def test_attach_subtitles_rejects_cut_mismatch(self):
        source = StreamSource(url="https://example.com/stream.m3u8", server="vixsrc", is_hls=True)
        mismatched_sub = SubtitleTrack(
            url="/path/sub_47m.srt",
            label="English (WEB-DL)",
            lang="en",
            duration=2840.0,
        )

        with patch.object(self.pm, "_has_native_subtitles", return_value=False), \
             patch.object(self.pm, "_get_stream_duration", return_value=2580.0), \
             patch.object(self.pm.subtitle_resolver, "get_subtitles", return_value=[mismatched_sub]):
            self.pm._attach_subtitles([source], "tt1632701", preferred_langs=["en", "eng"])
            self.assertEqual(source.subtitles, [])

    def test_attach_subtitles_accepts_matching_duration(self):
        source = StreamSource(url="https://example.com/stream.m3u8", server="vidora", is_hls=True)
        matching_sub = SubtitleTrack(
            url="/path/sub_47m.srt",
            label="English (WEB-DL)",
            lang="en",
            duration=2840.0,
        )

        with patch.object(self.pm, "_has_native_subtitles", return_value=False), \
             patch.object(self.pm, "_get_stream_duration", return_value=2832.0), \
             patch.object(self.pm.subtitle_resolver, "get_subtitles", return_value=[matching_sub]):
            self.pm._attach_subtitles([source], "tt1632701", preferred_langs=["en", "eng"])
            self.assertEqual(len(source.subtitles), 1)
            self.assertEqual(source.subtitles[0].url, "/path/sub_47m.srt")


if __name__ == "__main__":
    unittest.main()

