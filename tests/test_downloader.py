from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from dion.metadata.models import EpisodeItem, MediaItem, MediaType, StreamSource, SubtitleTrack
from dion.player.downloader import (
    BatchDownloadResult,
    DownloadTask,
    StreamDownloader,
    normalize_iso_639,
    parse_episode_range,
)


class TestRangeAndLanguageParsing(unittest.TestCase):
    def test_parse_episode_range(self):
        available = list(range(1, 11))  # 1 to 10

        self.assertEqual(parse_episode_range("1-4", available), [1, 2, 3, 4])
        self.assertEqual(parse_episode_range("1,3,5", available), [1, 3, 5])
        self.assertEqual(parse_episode_range("1-3,5,8-10", available), [1, 2, 3, 5, 8, 9, 10])
        self.assertEqual(parse_episode_range("7-", available), [7, 8, 9, 10])
        self.assertEqual(parse_episode_range("-3", available), [1, 2, 3])
        self.assertEqual(parse_episode_range("all", available), available)
        self.assertEqual(parse_episode_range("*", available), available)
        self.assertEqual(parse_episode_range("", available), available)
        self.assertEqual(parse_episode_range("6", available), [6])
        self.assertEqual(parse_episode_range("99", available), [])

    def test_normalize_iso_639(self):
        self.assertEqual(normalize_iso_639("en"), "eng")
        self.assertEqual(normalize_iso_639("English"), "eng")
        self.assertEqual(normalize_iso_639("es"), "spa")
        self.assertEqual(normalize_iso_639("Spanish"), "spa")
        self.assertEqual(normalize_iso_639("fr"), "fra")
        self.assertEqual(normalize_iso_639("French"), "fra")
        self.assertEqual(normalize_iso_639("de"), "deu")
        self.assertEqual(normalize_iso_639("German"), "deu")
        self.assertEqual(normalize_iso_639("it"), "ita")
        self.assertEqual(normalize_iso_639("ja"), "jpn")
        self.assertEqual(normalize_iso_639("ko"), "kor")
        self.assertEqual(normalize_iso_639("zh"), "zho")
        self.assertEqual(normalize_iso_639("ar"), "ara")
        self.assertEqual(normalize_iso_639("he"), "heb")
        self.assertEqual(normalize_iso_639("xyz"), "xyz")
        self.assertEqual(normalize_iso_639(""), "und")


class TestProgressParsing(unittest.TestCase):
    def test_parse_template_progress(self):
        line = "download:1048576/20971520|50.0%|5.25MiB/s|00:18"
        res = StreamDownloader._parse_progress_line(line)
        self.assertIsNotNone(res)
        pct, speed, eta = res
        self.assertEqual(pct, 50.0)
        self.assertEqual(speed, "5.25MiB/s")
        self.assertEqual(eta, "00:18")

    def test_parse_standard_ytdl_progress(self):
        line = "[download]  42.5% of ~ 150.00MiB at  4.20MiB/s ETA 00:25"
        res = StreamDownloader._parse_progress_line(line)
        self.assertIsNotNone(res)
        pct, speed, eta = res
        self.assertEqual(pct, 42.5)
        self.assertEqual(speed, "4.20MiB/s")
        self.assertEqual(eta, "00:25")

    def test_parse_fragment_progress(self):
        line = "[download] 15 of 150 fragments (10.0%) at 6.10MiB/s ETA 00:40"
        res = StreamDownloader._parse_progress_line(line)
        self.assertIsNotNone(res)
        pct, speed, eta = res
        self.assertEqual(pct, 10.0)
        self.assertEqual(speed, "6.10MiB/s")
        self.assertEqual(eta, "00:40")

    def test_parse_irrelevant_line(self):
        self.assertIsNone(StreamDownloader._parse_progress_line("[info] Downloading webpage"))
        self.assertIsNone(StreamDownloader._parse_progress_line(""))


class TestFilenameGeneration(unittest.TestCase):
    def setUp(self):
        self.downloader = StreamDownloader()
        self.media_movie = MediaItem(
            id="tt1375666",
            imdb_id="tt1375666",
            title="Inception",
            media_type=MediaType.MOVIE,
            year="2010",
        )
        self.media_show = MediaItem(
            id="tt11280740",
            imdb_id="tt11280740",
            title="Severance",
            media_type=MediaType.SERIES,
            year="2022",
        )
        self.episode = EpisodeItem(
            id="tt11280740:1:1",
            season=1,
            episode=1,
            title="Good News About Hell",
        )

    def test_movie_filename(self):
        fn_mp4 = self.downloader.get_output_filename(self.media_movie, video_format="mp4")
        self.assertEqual(fn_mp4, "Inception.(2010).mp4")

        fn_mkv = self.downloader.get_output_filename(self.media_movie, video_format="mkv")
        self.assertEqual(fn_mkv, "Inception.(2010).mkv")

    def test_episode_filename(self):
        fn_mp4 = self.downloader.get_output_filename(self.media_show, self.episode, video_format="mp4")
        self.assertEqual(fn_mp4, "Severance.S01E01.mp4")

        fn_mkv = self.downloader.get_output_filename(self.media_show, self.episode, video_format="mkv")
        self.assertEqual(fn_mkv, "Severance.S01E01.mkv")


class TestSubtitleAndArtworkPreparation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)
        self.downloader = StreamDownloader()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prepare_local_subtitles(self):
        local_sub = self.work_dir / "test.srt"
        local_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

        source = StreamSource(
            url="https://video.mp4",
            server="vixsrc",
            subtitles=[SubtitleTrack(url=str(local_sub), label="English", lang="en")],
        )
        media = MediaItem(id="tt1", imdb_id="tt1", title="Test", media_type=MediaType.MOVIE)

        prepared = self.downloader._prepare_subtitles(source, media, None, self.work_dir)
        self.assertEqual(len(prepared), 1)
        sub_path, iso_lang, label = prepared[0]
        self.assertEqual(sub_path, local_sub)
        self.assertEqual(iso_lang, "eng")
        self.assertEqual(label, "English")

    def test_prepare_remote_gzipped_subtitles(self):
        sub_content = "1\n00:00:01,000 --> 00:00:02,000\nGzipped subtitle text\n"
        compressed = gzip.compress(sub_content.encode("utf-8"))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = compressed

        source = StreamSource(
            url="https://video.mp4",
            server="vixsrc",
            subtitles=[SubtitleTrack(url="https://example.com/sub.srt.gz", label="Spanish", lang="es")],
        )
        media = MediaItem(id="tt1", imdb_id="tt1", title="Test", media_type=MediaType.MOVIE)

        with patch("httpx.get", return_value=mock_resp):
            prepared = self.downloader._prepare_subtitles(source, media, None, self.work_dir)
            self.assertEqual(len(prepared), 1)
            sub_path, iso_lang, label = prepared[0]
            self.assertTrue(sub_path.exists())
            self.assertIn("Gzipped subtitle text", sub_path.read_text(encoding="utf-8"))
            self.assertEqual(iso_lang, "spa")

    def test_prepare_cover_artwork(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"\xff\xd8\xff\xe0" + (b"0" * 1000)

        with patch("httpx.get", return_value=mock_resp):
            cover = self.downloader._prepare_cover_artwork("https://example.com/poster.jpg", self.work_dir)
            self.assertIsNotNone(cover)
            self.assertTrue(cover.exists())
            self.assertEqual(cover.name, "cover.jpg")


class TestMuxingCommands(unittest.TestCase):
    def setUp(self):
        self.downloader = StreamDownloader()
        self.downloader.ffmpeg = "/usr/local/bin/ffmpeg"

    @patch("subprocess.run")
    def test_mux_subtitles_mp4_mov_text_and_tags(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            p_td = Path(td)
            in_vid = p_td / "in.mp4"
            in_vid.write_bytes(b"0" * 2000)
            sub_file = p_td / "sub.srt"
            sub_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
            cover_file = p_td / "cover.jpg"
            cover_file.write_bytes(b"0" * 1000)
            out_file = p_td / "out.mp4"
            out_file.write_bytes(b"0" * 2000)

            media = MediaItem(
                id="tt11280740",
                imdb_id="tt11280740",
                title="Severance",
                media_type=MediaType.SERIES,
                year="2022",
                overview="Office mystery series",
            )
            episode = EpisodeItem(
                id="tt11280740:1:1",
                season=1,
                episode=1,
                title="Good News About Hell",
                overview="Mark leads the severed floor.",
                released="2022-02-18",
            )

            ok = self.downloader._mux_subtitles_and_metadata(
                input_video=in_vid,
                output_path=out_file,
                media=media,
                episode=episode,
                subtitles=[(sub_file, "eng", "English")],
                cover_art=cover_file,
                video_format="mp4",
            )
            self.assertTrue(ok)

            cmd = mock_run.call_args[0][0]
            # Subtitle codec for MP4 must be mov_text
            self.assertIn("-c:s", cmd)
            self.assertIn("mov_text", cmd)
            # Cover artwork attached pic disposition for MP4
            self.assertIn("-disposition:v:1", cmd)
            self.assertIn("attached_pic", cmd)
            # TV episode metadata
            self.assertIn("-metadata", cmd)
            self.assertIn("show=Severance", cmd)
            self.assertIn("season_number=1", cmd)
            self.assertIn("episode_sort=1", cmd)
            self.assertIn("episode_id=S01E01", cmd)
            self.assertIn("language=eng", cmd)

    @patch("subprocess.run")
    def test_mux_subtitles_mkv_srt_and_attachment(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            p_td = Path(td)
            in_vid = p_td / "in.mp4"
            in_vid.write_bytes(b"0" * 2000)
            sub_file = p_td / "sub.srt"
            sub_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
            cover_file = p_td / "cover.jpg"
            cover_file.write_bytes(b"0" * 1000)
            out_file = p_td / "out.mkv"
            out_file.write_bytes(b"0" * 2000)

            media = MediaItem(
                id="tt1375666",
                imdb_id="tt1375666",
                title="Inception",
                media_type=MediaType.MOVIE,
                year="2010",
            )

            ok = self.downloader._mux_subtitles_and_metadata(
                input_video=in_vid,
                output_path=out_file,
                media=media,
                episode=None,
                subtitles=[(sub_file, "eng", "English")],
                cover_art=cover_file,
                video_format="mkv",
            )
            self.assertTrue(ok)

            cmd = mock_run.call_args[0][0]
            # Subtitle codec for MKV must be srt
            self.assertIn("-c:s", cmd)
            self.assertIn("srt", cmd)
            # MKV attachment
            self.assertIn("-attach", cmd)
            self.assertIn(str(cover_file), cmd)
            self.assertIn("title=Inception", cmd)


class TestBatchSeasonDownloader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)
        self.downloader = StreamDownloader()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_download_batch_with_skipping_existing_and_mocked_workers(self):
        media = MediaItem(
            id="tt11280740",
            imdb_id="tt11280740",
            title="Severance",
            media_type=MediaType.SERIES,
            year="2022",
        )
        ep1 = EpisodeItem(id="e1", season=1, episode=1, title="Good News")
        ep2 = EpisodeItem(id="e2", season=1, episode=2, title="Half Loop")

        # Pre-create episode 1 file to test skip logic
        existing_file = self.output_dir / "Severance.S01E01.mp4"
        existing_file.write_bytes(b"0" * (2 * 1024 * 1024))

        tasks = [
            DownloadTask(media=media, episode=ep1, source=StreamSource(url="https://v1.mp4", server="s1")),
            DownloadTask(media=media, episode=ep2, source=StreamSource(url="https://v2.mp4", server="s1")),
        ]

        with patch.object(self.downloader, "_download_stream_raw", return_value=True), \
             patch.object(self.downloader, "_mux_subtitles_and_metadata", side_effect=lambda inv, outv, *a, **k: outv.write_bytes(b"0" * 2000) or True):

            res: BatchDownloadResult = self.downloader.download_batch(
                tasks=tasks,
                output_dir=self.output_dir,
                video_format="mp4",
                max_concurrent=2,
                overwrite=False,
            )

            self.assertEqual(res.total, 2)
            self.assertEqual(res.skipped, 1)
            self.assertEqual(res.completed, 1)
            self.assertEqual(res.failed, 0)
            self.assertEqual(len(res.files), 2)


class TestCliBatchIntegration(unittest.TestCase):
    def test_download_season_batch_calls_downloader(self):
        from dion.cli import download_season_batch

        media = MediaItem(
            id="tt11280740",
            imdb_id="tt11280740",
            title="Severance",
            media_type=MediaType.SERIES,
            year="2022",
        )
        episodes = [
            EpisodeItem(id="e1", season=1, episode=1, title="Good News"),
            EpisodeItem(id="e2", season=1, episode=2, title="Half Loop"),
        ]

        fake_result = BatchDownloadResult(
            total=2,
            completed=2,
            failed=0,
            skipped=0,
            output_dir=Path("/tmp/downloads"),
            files=[Path("/tmp/downloads/e1.mp4"), Path("/tmp/downloads/e2.mp4")],
        )

        with patch("dion.cli.get_downloader") as mock_get_dl, \
             patch("dion.cli.get_provider_manager"):
            mock_dl = MagicMock()
            mock_dl.download_batch.return_value = fake_result
            mock_get_dl.return_value = mock_dl

            success = download_season_batch(
                media=media,
                episodes=episodes,
                output_dir=Path("/tmp/downloads"),
                video_format="mp4",
                max_concurrent=2,
            )

            self.assertTrue(success)
            mock_dl.download_batch.assert_called_once()
            call_kwargs = mock_dl.download_batch.call_args[1]
            self.assertEqual(len(call_kwargs["tasks"]), 2)
            self.assertEqual(call_kwargs["video_format"], "mp4")


if __name__ == "__main__":
    unittest.main()

