"""Regression coverage for Windows paths, discovery, and optional terminal UI."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dion import platforms, ui
from dion.metadata.models import MediaItem, MediaType, StreamSource
from dion.player.downloader import StreamDownloader
from dion.player.mpv import MpvPlayer
from dion.storage.settings import SettingsManager
from dion.storage.history import HistoryManager


class WindowsSupportTests(unittest.TestCase):
    def test_cli_import_without_curses(self):
        code = "import sys; sys.modules['curses'] = None; from dion.cli import app"
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)

    def test_batch_downloads_with_cp1252_output(self):
        env = dict(os.environ, PYTHONIOENCODING="cp1252:strict")
        result = subprocess.run([
            sys.executable, "-m", "unittest",
            "tests.test_downloader.TestBatchSeasonDownloader.test_download_batch_with_skipping_existing_and_mocked_workers",
            "tests.test_downloader.TestCliBatchIntegration.test_download_season_batch_calls_downloader",
        ], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("cp1252"))

    def test_output_preserves_encoding_and_utf8_glyphs(self):
        code = (
            "import sys; from dion.console import console; "
            "print(sys.stdout.encoding); console.print('🍇⠋', end='')"
        )
        for encoding, expected in (("cp1252", "??"), ("utf-8", "🍇⠋")):
            with self.subTest(encoding=encoding):
                result = subprocess.run(
                    [sys.executable, "-c", code], capture_output=True,
                    env=dict(os.environ, PYTHONIOENCODING=encoding + ":strict"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = result.stdout.decode(encoding).splitlines()
                self.assertEqual(output, [encoding, expected])

    def test_config_migrates_and_managers_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / '.config' / 'dion'
            legacy.mkdir(parents=True)
            (legacy / 'settings.json').write_text('{"sub_lang": "fr"}', encoding='utf-8')
            with patch.object(platforms.sys, 'platform', 'win32'), \
                 patch.object(Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'APPDATA': str(home / 'Roaming')}):
                settings = SettingsManager()
                history = HistoryManager()
                self.assertEqual(settings.config_dir, home / 'Roaming' / 'Dion')
                self.assertEqual(settings.config_dir, history.config_dir)
                self.assertEqual(settings.sub_lang, 'fr')
                settings.set('sub_lang', 'de')
                self.assertEqual(SettingsManager().sub_lang, 'de')
                self.assertTrue((legacy / 'settings.json').is_file())

    def test_player_in_program_files_and_custom_path(self):
        with tempfile.TemporaryDirectory(prefix='dion space ') as directory:
            root = Path(directory)
            executable = root / 'VideoLAN' / 'VLC' / 'vlc.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(platforms.sys, 'platform', 'win32'), \
                 patch.object(platforms.shutil, 'which', return_value=None), \
                 patch.dict(os.environ, {'ProgramFiles': str(root)}):
                self.assertEqual(platforms.find_player('vlc'), str(executable))
                settings = SettingsManager(root / 'config')
                settings.set('player', str(executable))
                self.assertEqual(settings.resolve_player_executable(), str(executable))

    def test_media_selection_without_curses(self):
        item = MediaItem(id='tt1', imdb_id='tt1', title='Example', media_type=MediaType.MOVIE)
        with patch.object(ui, 'curses', None), patch.object(ui, 'prompt_select', return_value=item) as select:
            self.assertIs(ui.select_media([item]), item)
            self.assertEqual(select.call_args.args[1][0][1], item)

    def test_lua_and_player_share_config_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix='dion café ') as directory:
            settings = SettingsManager(Path(directory))
            player = MpvPlayer(executable='C:/Program Files/mpv/mpv.exe', settings_mgr=settings)
            item = MediaItem(id='tt1', imdb_id='tt1', title='Example', media_type=MediaType.MOVIE)
            with patch('dion.player.mpv.subprocess.run') as run:
                run.return_value.returncode = 0
                player.play(StreamSource(url='https://example.com/video.mp4', server='test', is_hls=False), item)
            args = run.call_args.args[0]
            self.assertEqual(args[0], player.executable)
            self.assertIn('--script=' + str(settings.config_dir / 'scripts' / 'dion_subtitles.lua'), args)
            self.assertTrue(any('dion_config=' + settings.config_dir.as_posix() in arg for arg in args))

    def test_download_reserved_and_empty_names(self):
        downloader = StreamDownloader()
        for title, expected in [('CON', '_CON'), ('nul.txt', '_nul.txt'), ('...', 'untitled'), ('Example.', 'Example')]:
            with self.subTest(title=title):
                item = MediaItem(id='tt1', imdb_id='tt1', title=title, media_type=MediaType.MOVIE)
                self.assertEqual(downloader.get_output_filename(item), expected + '.mp4')
