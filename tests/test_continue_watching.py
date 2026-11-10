"""Persistence and real key input coverage for both Continue Watching backends."""
import copy
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from dion.continue_menu import ContinueMenu, _questionary_action, select_continue
from dion.metadata.models import MediaItem, MediaType
from dion.storage.history import HistoryManager


@pytest.fixture
def history(tmp_path):
    manager = HistoryManager(tmp_path)
    for i in reversed(range(3)):
        media = MediaItem(id=f"tt{i}", imdb_id=f"tt{i}", title=f"Title {i}",
                          media_type=MediaType.MOVIE)
        manager.record_watch(media, playback_position=100 + i, duration=1000)
    return manager


def test_visibility_preserves_history_and_survives_restart(history):
    before = copy.deepcopy(history.list_history())
    history.set_continue_hidden("tt0", True)
    reloaded = HistoryManager(history.config_dir)
    assert reloaded.list_history() == before
    assert [i["imdb_id"] for i in reloaded.list_continue_watching(limit=1)] == ["tt1"]
    reloaded.set_continue_hidden("tt0", False)
    assert reloaded.list_continue_watching() == before


def test_playback_restores_hidden_title_but_background_updates_do_not(history):
    media = MediaItem(id="tt0", imdb_id="tt0", title="Title 0", media_type=MediaType.MOVIE)
    history.set_continue_hidden("tt0", True)
    history.record_watch(media, restore_continue=False)
    assert "tt0" not in [i["imdb_id"] for i in history.list_continue_watching()]
    history.record_watch(media)
    assert history.list_continue_watching()[0]["imdb_id"] == "tt0"


def test_failed_save_leaves_disk_memory_and_undo_unchanged(history):
    menu = ContinueMenu(history.list_continue_watching(), history)
    before = history.history_file.read_bytes()
    with patch.object(Path, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            menu.remove(0)
    assert not menu.undo
    assert len(menu.items) == 3
    assert len(history.list_continue_watching()) == 3
    assert history.history_file.read_bytes() == before
    assert sorted(p.name for p in history.config_dir.iterdir()) == ["history.json"]


def run_questionary(menu, keys):
    factory = questionary.select
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with patch("dion.continue_menu.questionary.select",
                   side_effect=lambda *a, **kw: factory(*a, input=pipe, output=DummyOutput(), **kw)):
            return _questionary_action(menu)


@pytest.mark.parametrize("cancel", ["\x1b[B", "\x1b[A", "x", "\x7f", "\x1b"])
def test_fallback_input_disarms_confirmation(history, cancel):
    menu = ContinueMenu(history.list_continue_watching(), history)
    # After cancellation, a fresh Ctrl+D must only arm; Enter returns selection.
    action, _ = run_questionary(menu, "\x04" + cancel + "\x04\r")
    assert action == "select"
    assert len(history.list_continue_watching()) == 3


def test_fallback_confirm_names_title_and_removes_only_after_second_press(history):
    menu = ContinueMenu(history.list_continue_watching(), history)
    assert run_questionary(menu, "\x04\x04") == ("remove", 0)
    menu.remove(0)
    assert menu.items[0]["imdb_id"] == "tt1"
    menu.restore()
    assert menu.items[0]["imdb_id"] == "tt0"


def test_fallback_search_and_empty_undo(history):
    menu = ContinueMenu(history.list_continue_watching(), history)
    assert run_questionary(menu, "2\x04\x04") == ("remove", 2)
    menu.remove(2)
    menu.restore()
    assert menu.query == "2"
    assert run_questionary(menu, "\r") == ("select", 2)
    menu.query = ""
    while menu.items:
        menu.remove(0)
    assert run_questionary(menu, "\x1a")[0] == "undo"
    menu.restore()
    assert len(menu.items) == 1


def test_failed_undo_can_be_retried(history):
    menu = ContinueMenu(history.list_continue_watching(), history)
    menu.remove(1)
    with patch.object(Path, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            menu.restore()
    assert len(menu.undo) == 1
    assert len(menu.items) == 2
    menu.restore()
    assert [i["imdb_id"] for i in menu.items] == ["tt0", "tt1", "tt2"]


def test_menu_handles_save_errors_and_retries(history):
    with patch("dion.continue_menu.ui.has_fzf", return_value=False), \
         patch("dion.continue_menu._questionary_action",
               side_effect=[("remove", 0), ("select", 0)]), \
         patch.object(history, "set_continue_hidden", side_effect=OSError("disk full")):
        assert select_continue(history.list_continue_watching(), history)["imdb_id"] == "tt0"


@pytest.mark.skipif(os.name == "nt", reason="fzf POSIX action bindings")
@pytest.mark.parametrize("cancel, expected", [
    ("", "remove"), ("\x1b[B", "select"), ("\x1b[A", "select"),
    ("\x1b", "select"), ("o", "select"),
])
def test_real_fzf_confirmation(tmp_path, cancel, expected):
    import shutil
    if not shutil.which("fzf"):
        pytest.skip("fzf not installed")
    pexpect = pytest.importorskip("pexpect")
    import sys
    script = tmp_path / "driver.py"
    # These punctuation marks must be displayed as data, never fzf actions or shell code.
    script.write_text('''
import json
from dion.continue_menu import ContinueMenu, _fzf_action
menu = ContinueMenu([{"imdb_id":"tt0", "title":"Zero $(false) + (test) 'quoted'", "media_type":"movie"},
                     {"imdb_id":"tt1", "title":"One", "media_type":"movie"}], None)
print("RESULT", json.dumps(_fzf_action(menu)), flush=True)
''')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               TERM="xterm-256color", SHELL="/usr/bin/fish", FZF_DEFAULT_OPTS="")
    with pexpect.spawn(sys.executable, [str(script)], env=env,
                       encoding="utf-8", timeout=5, dimensions=(30, 180)) as child:
        def expect(text):
            while child.expect([text, "\x1b\\[6n"]) == 1:
                child.send("\x1b[1;1R")
        expect("Not started")
        child.send("\x04")
        expect("again confirms")
        child.send(cancel + "\x04" + ("\r" if cancel else ""))
        expect("RESULT")
        child.expect(pexpect.EOF)
        import json
        action, index = json.loads(child.before.strip())
        assert action == expected
        assert index in (0, 1)


def test_hiding_does_not_modify_episode_playback_files(history):
    history.set_playback_position("tt0", 1250, season=2, episode=3)
    history.set_playback_duration("tt0", 3600, season=2, episode=3)
    before = {p: p.read_bytes() for p in (history.positions_file, history.durations_file)}
    history.set_continue_hidden("tt0", True)
    history.set_continue_hidden("tt0", False)
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.skipif(os.name == "nt", reason="fzf POSIX action bindings")
def test_real_fzf_can_undo_with_no_matches(tmp_path):
    import shutil
    if not shutil.which("fzf"):
        pytest.skip("fzf not installed")
    pexpect = pytest.importorskip("pexpect")
    import sys
    script = tmp_path / "empty.py"
    script.write_text('''
from dion.continue_menu import ContinueMenu, _fzf_action
menu = ContinueMenu([], None)
menu.query = "nomatches"
print("RESULT", repr(_fzf_action(menu)), flush=True)
''')
    with pexpect.spawn(sys.executable, [str(script)],
                       env=dict(os.environ, TERM="xterm-256color", FZF_DEFAULT_OPTS="",
                                PYTHONPATH=str(Path(__file__).resolve().parents[1])),
                       encoding="utf-8", timeout=5, dimensions=(30, 180)) as child:
        while child.expect(["0/1", "\x1b\\[6n"]) == 1:
            child.send("\x1b[1;1R")
        child.send("\x1a")
        child.expect("RESULT")
        child.expect(pexpect.EOF)
        assert "('undo', None)" in child.before
