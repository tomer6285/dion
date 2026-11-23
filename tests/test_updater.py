"""Unit tests for Dion self-updater."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from dion import updater
from dion.updater import (
    InstallType,
    detect_install_type,
    get_standalone_target_path,
    parse_version,
    select_release_asset,
    update_pipx,
    update_standalone,
)


class TestUpdater(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(parse_version("0.1.0"), (0, 1, 0))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("v2.0.0-beta"), (2, 0, 0))
        self.assertGreater(parse_version("v0.2.0"), parse_version("v0.1.9"))
        self.assertEqual(parse_version("invalid"), (0,))

    def test_detect_install_type_standalone_frozen(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(detect_install_type(), InstallType.STANDALONE)

    def test_detect_install_type_standalone_env(self):
        with patch.dict(os.environ, {"DION_STANDALONE_BINARY": "/usr/local/bin/dion"}):
            self.assertEqual(detect_install_type(), InstallType.STANDALONE)

    def test_detect_install_type_pipx_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta = Path(tmp_dir) / "pipx_metadata.json"
            meta.write_text("{}", encoding="utf-8")
            with patch.object(sys, "frozen", False, create=True), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(sys, "prefix", tmp_dir):
                self.assertEqual(detect_install_type(), InstallType.PIPX)

    def test_detect_install_type_pipx_path(self):
        pipx_prefix = "/home/user/.local/pipx/venvs/dion"
        with patch.object(sys, "frozen", False, create=True), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "prefix", pipx_prefix):
            self.assertEqual(detect_install_type(), InstallType.PIPX)

    def test_detect_install_type_source_checkout(self):
        with patch.object(sys, "frozen", False, create=True), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "prefix", "/some/other/venv"):
            # The repository itself has a .git folder
            self.assertEqual(detect_install_type(), InstallType.SOURCE)

    def test_select_release_asset_linux(self):
        assets = [
            {"name": "dion-windows.zip", "browser_download_url": "https://.../win.zip"},
            {"name": "dion-linux", "browser_download_url": "https://.../linux"},
            {"name": "dion-macos", "browser_download_url": "https://.../mac"},
        ]
        with patch("platform.system", return_value="Linux"):
            selected = select_release_asset(assets)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["name"], "dion-linux")

    def test_select_release_asset_windows(self):
        assets = [
            {"name": "dion-linux", "browser_download_url": "https://.../linux"},
            {"name": "dion-windows.zip", "browser_download_url": "https://.../win.zip"},
        ]
        with patch("platform.system", return_value="Windows"):
            selected = select_release_asset(assets)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["name"], "dion-windows.zip")

    def test_select_release_asset_darwin(self):
        assets = [
            {"name": "dion-macos", "browser_download_url": "https://.../mac"},
            {"name": "dion-linux", "browser_download_url": "https://.../linux"},
        ]
        with patch("platform.system", return_value="Darwin"):
            selected = select_release_asset(assets)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["name"], "dion-macos")

    def test_get_standalone_target_path(self):
        # 1. Env variable takes precedence
        with tempfile.NamedTemporaryFile() as tf:
            with patch.dict(os.environ, {"DION_STANDALONE_BINARY": tf.name}):
                self.assertEqual(get_standalone_target_path(), Path(tf.name).resolve())

        # 2. shutil.which when no env var
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.NamedTemporaryFile() as tf:
                with patch("shutil.which", return_value=tf.name):
                    self.assertEqual(get_standalone_target_path(), Path(tf.name).resolve())

    @patch("dion.updater.get_latest_github_release")
    def test_update_standalone_already_up_to_date(self, mock_get_release):
        mock_get_release.return_value = {
            "tag_name": "v0.1.0",
            "assets": [{"name": "dion-linux", "browser_download_url": "https://..."}],
        }
        with patch("dion.updater.__version__", "0.1.0"), \
             patch("dion.updater.download_asset_with_progress") as mock_download:
            update_standalone(force=False)
            mock_download.assert_not_called()

    @patch("dion.updater.get_latest_github_release")
    @patch("dion.updater.download_asset_with_progress")
    @patch("dion.updater._replace_binary")
    def test_update_standalone_success(self, mock_replace, mock_download, mock_get_release):
        mock_get_release.return_value = {
            "tag_name": "v0.9.0",
            "assets": [{"name": "dion-linux", "browser_download_url": "https://.../dion-linux"}],
        }
        with tempfile.NamedTemporaryFile() as target_bin:
            with patch("dion.updater.__version__", "0.1.0"), \
                 patch("platform.system", return_value="Linux"), \
                 patch("dion.updater.get_standalone_target_path", return_value=Path(target_bin.name)), \
                 patch("dion.updater.check_target_writable", return_value=True):
                update_standalone()
                mock_download.assert_called_once()
                mock_replace.assert_called_once()

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_pipx_standard(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/pipx"
        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            meta = Path(tmp_dir) / "pipx_metadata.json"
            meta.write_text(json.dumps({"main_package": {"package_or_url": "dion"}}), encoding="utf-8")
            with patch.object(sys, "prefix", tmp_dir):
                update_pipx()
                mock_run.assert_called_once_with(["/usr/bin/pipx", "upgrade", "dion"])

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_pipx_git(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/pipx"
        mock_run.return_value = MagicMock(returncode=0)
        git_spec = "git+https://github.com/tomer6285/dion.git"

        with tempfile.TemporaryDirectory() as tmp_dir:
            meta = Path(tmp_dir) / "pipx_metadata.json"
            meta.write_text(json.dumps({"main_package": {"package_or_url": git_spec}}), encoding="utf-8")
            with patch.object(sys, "prefix", tmp_dir):
                update_pipx()
                mock_run.assert_called_once_with(["/usr/bin/pipx", "install", "--force", git_spec])

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_pipx_fallback_to_pip(self, mock_run, mock_which):
        mock_which.return_value = None  # pipx not in PATH
        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(sys, "prefix", tmp_dir):
                update_pipx()
                mock_run.assert_called_once_with([sys.executable, "-m", "pip", "install", "--upgrade", "dion"])


class TestCliUpdateFlag(unittest.TestCase):
    @patch("dion.updater.run_self_update")
    def test_cli_update_flag_invokes_updater(self, mock_run_self_update):
        from typer.testing import CliRunner
        from dion.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["-U"])
        self.assertEqual(result.exit_code, 0)
        mock_run_self_update.assert_called_once_with(force=False)
