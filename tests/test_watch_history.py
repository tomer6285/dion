from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dion.metadata.models import EpisodeItem, MediaItem, MediaType
from dion.storage.db import DatabaseManager
from dion.storage.history import HistoryManager
from dion.trakt.auth import TraktAuthManager
from dion.trakt.models import TraktToken
from dion.trakt.sync import TraktSyncManager
from dion.history_view import select_from_history
from dion.trakt.background import start_background_sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_movie() -> MediaItem:
    return MediaItem(
        id="tt0137523",
        imdb_id="tt0137523",
        title="Fight Club",
        media_type=MediaType.MOVIE,
        year="1999",
        runtime="139 min",
    )


@pytest.fixture
def sample_show() -> MediaItem:
    return MediaItem(
        id="tt0903747",
        imdb_id="tt0903747",
        title="Breaking Bad",
        media_type=MediaType.SERIES,
        year="2008",
    )


@pytest.fixture
def sample_episodes() -> list[EpisodeItem]:
    return [
        EpisodeItem(id="tt0903747:0:1", season=0, episode=1, title="Good Cop / Bad Cop", released="2009-02-17"),
        EpisodeItem(id="tt0903747:1:1", season=1, episode=1, title="Pilot", released="2008-01-20"),
        EpisodeItem(id="tt0903747:1:2", season=1, episode=2, title="Cat's in the Bag...", released="2008-01-27"),
        EpisodeItem(id="tt0903747:1:3", season=1, episode=3, title="...And the Bag's in the River", released="2008-02-10"),
        EpisodeItem(id="tt0903747:2:1", season=2, episode=1, title="Seven Thirty-Seven", released="2009-03-08"),
    ]


@pytest.fixture
def mock_token() -> TraktToken:
    return TraktToken(
        access_token="test_access_token",
        token_type="bearer",
        expires_in=7776000,
        refresh_token="test_refresh_token",
        scope="public",
        created_at=int(time.time()),
        username="testuser",
    )


# ---------------------------------------------------------------------------
# 1. Migration tests
# ---------------------------------------------------------------------------

def test_migration_preserves_resume_without_unconfirmed_plays(tmp_path: Path):
    """Legacy history.json should populate resume_state without inventing unconfirmed plays."""
    legacy_json = tmp_path / "history.json"
    legacy_data = {
        "items": [
            {
                "imdb_id": "tt0903747",
                "title": "Breaking Bad",
                "media_type": "series",
                "season": 1,
                "episode": 2,
                "episode_title": "Cat's in the Bag...",
                "position": 500.0,
                "duration": 2800.0,
                "last_watched": "2024-01-15T12:00:00Z",
            },
            {
                "imdb_id": "tt0137523",
                "title": "Fight Club",
                "media_type": "movie",
                "position": 1200.0,
                "duration": 8340.0,
                "last_watched": "2024-01-10T20:00:00Z",
            },
        ],
        "hidden_from_continue": ["tt0137523"],
    }
    legacy_json.write_text(json.dumps(legacy_data))

    mgr = HistoryManager(tmp_path)

    # Plays must be empty (ambiguous legacy items are left as resume data only)
    plays, total_plays = mgr.list_plays()
    assert total_plays == 0
    assert len(plays) == 0

    # Resume state must be preserved
    resume_items = mgr.list_history()
    assert len(resume_items) == 2
    by_id = {i["imdb_id"]: i for i in resume_items}
    assert by_id["tt0903747"]["season"] == 1
    assert by_id["tt0903747"]["episode"] == 2
    assert by_id["tt0903747"]["position"] == 500.0

    # Continue watching must respect hidden status from legacy JSON
    cw = mgr.list_continue_watching()
    assert len(cw) == 1
    assert cw[0]["imdb_id"] == "tt0903747"


# ---------------------------------------------------------------------------
# 2. Play tracking & completion rule vs early exit
# ---------------------------------------------------------------------------

def test_early_exit_only_updates_resume_position(tmp_path: Path, sample_movie: MediaItem):
    """Stopping playback before completion threshold should update resume position only."""
    from dion.cli import play_or_download

    mgr = HistoryManager(tmp_path)
    mock_pm = MagicMock()
    mock_source = MagicMock(server="Vidplay")
    mock_pm.resolve_movie.return_value = [mock_source]
    mock_player = MagicMock()
    mock_player.play.return_value = 0

    # 500s of 8340s (~6% played, definitely not completed)
    with patch("dion.cli.get_history_mgr", return_value=mgr), \
         patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
         patch("dion.cli.get_provider_manager", return_value=mock_pm), \
         patch("dion.cli.get_player", return_value=mock_player), \
         patch.object(mgr, "get_playback_position", return_value=500.0), \
         patch.object(mgr, "get_playback_duration", return_value=8340.0):
        mock_auth.return_value.is_authenticated.return_value = False

        success = play_or_download(sample_movie, episode=None, download=False, auto_select=True)
        assert success is True

    # No play should be recorded in plays table
    plays, total = mgr.list_plays()
    assert total == 0
    assert len(plays) == 0

    # Resume position must be updated to 500.0
    item = mgr.get_history_item(sample_movie.imdb_id)
    assert item is not None
    assert item["position"] == 500.0


def test_playback_completion_rule_records_play(tmp_path: Path, sample_show: MediaItem, sample_episodes: list[EpisodeItem]):
    """Finishing playback (>=90% or pos==0) records completed play with timestamp."""
    from dion.cli import play_or_download

    mgr = HistoryManager(tmp_path)
    ep = sample_episodes[1]  # S01E01
    mock_pm = MagicMock()
    mock_source = MagicMock(server="Vidplay")
    mock_pm.resolve_episode.return_value = [mock_source]
    mock_player = MagicMock()
    mock_player.play.return_value = 0

    # 95% played of 3000s duration
    with patch("dion.cli.get_history_mgr", return_value=mgr), \
         patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
         patch("dion.cli.get_provider_manager", return_value=mock_pm), \
         patch("dion.cli.get_player", return_value=mock_player), \
         patch.object(mgr, "get_playback_position", return_value=2850.0), \
         patch.object(mgr, "get_playback_duration", return_value=3000.0):
        mock_auth.return_value.is_authenticated.return_value = False

        success = play_or_download(sample_show, episode=ep, download=False, auto_select=True)
        assert success is True

    # Completed play must be recorded
    plays, total = mgr.list_plays()
    assert total == 1
    assert plays[0]["imdb_id"] == sample_show.imdb_id
    assert plays[0]["season"] == 1
    assert plays[0]["episode"] == 1
    assert plays[0]["watched_at"] is not None

    # Status helper should now report it as watched
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=1) is True
    assert mgr.get_play_count(sample_show.imdb_id, season=1, episode=1) == 1


def test_rewatch_creates_separate_play_entries(tmp_path: Path, sample_movie: MediaItem):
    """Rewatching the same movie creates separate plays with distinct timestamps."""
    mgr = HistoryManager(tmp_path)

    mgr.record_play(sample_movie, watched_at="2024-01-01T12:00:00Z")
    mgr.record_play(sample_movie, watched_at="2024-02-01T12:00:00Z")

    plays, total = mgr.list_plays()
    assert total == 2
    assert mgr.get_play_count(sample_movie.imdb_id) == 2
    assert mgr.is_watched(sample_movie.imdb_id) is True

    # Sorted newest first
    assert plays[0]["watched_at"] == "2024-02-01T12:00:00Z"
    assert plays[1]["watched_at"] == "2024-01-01T12:00:00Z"


def test_scrobble_reconciliation_yields_one_play_with_trakt_id(tmp_path: Path, sample_movie: MediaItem):
    """Live Trakt scrobble response is reconciled so exactly one play is recorded with trakt_play_id."""
    from dion.cli import play_or_download

    mgr = HistoryManager(tmp_path)
    mock_pm = MagicMock()
    mock_source = MagicMock(server="Vidplay")
    mock_pm.resolve_movie.return_value = [mock_source]
    mock_player = MagicMock()
    mock_player.play.return_value = 0

    mock_scrobble_result = {
        "id": 987654,
        "action": "scrobble",
        "movie": {"title": sample_movie.title, "year": 1999, "ids": {"imdb": sample_movie.imdb_id}},
    }

    mock_settings = MagicMock()
    mock_settings.trakt_scrobble = True
    mock_settings.discord_rpc = False
    mock_settings.sub_lang = "en"

    with patch("dion.cli.get_history_mgr", return_value=mgr), \
         patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
         patch("dion.cli.get_provider_manager", return_value=mock_pm), \
         patch("dion.cli.get_player", return_value=mock_player), \
         patch("dion.cli.get_settings_mgr", return_value=mock_settings), \
         patch.object(mgr, "get_playback_position", return_value=4800.0), \
         patch.object(mgr, "get_playback_duration", return_value=5000.0), \
         patch("dion.trakt.scrobbler.TraktScrobbler.stop_playback", return_value=mock_scrobble_result):
        mock_auth.return_value.is_authenticated.return_value = True

        play_or_download(sample_movie, episode=None, download=False, auto_select=True)

    plays, total = mgr.list_plays()
    assert total == 1
    assert plays[0]["trakt_play_id"] == 987654
    assert plays[0]["source"] == "scrobble"


def test_quick_exit_with_zero_position_does_not_record_false_watch(
    tmp_path: Path, sample_movie: MediaItem
):
    """Exiting quickly before player saves progress (pos=0.0, no completion marker) must NOT record a play."""
    from dion.cli import play_or_download

    mgr = HistoryManager(tmp_path)
    # Simulate an older 0.0 position already sitting in playback_positions.json
    mgr.set_playback_position(sample_movie.imdb_id, 0.0)

    mock_pm = MagicMock()
    mock_pm.resolve_movie.return_value = [MagicMock(server="Vidplay")]
    mock_player = MagicMock()
    mock_player.play.return_value = 0

    with patch("dion.cli.get_history_mgr", return_value=mgr), \
         patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
         patch("dion.cli.get_provider_manager", return_value=mock_pm), \
         patch("dion.cli.get_player", return_value=mock_player):
        mock_auth.return_value.is_authenticated.return_value = False

        success = play_or_download(sample_movie, episode=None, download=False, auto_select=True)
        assert success is True

    plays, total = mgr.list_plays()
    assert total == 0
    assert len(plays) == 0


def test_explicit_mpv_completion_marker_records_play_when_position_reset_to_zero(
    tmp_path: Path, sample_movie: MediaItem
):
    """When mpv writes an explicit completion marker on EOF and resets position to 0.0, play IS recorded."""
    from dion.cli import play_or_download

    mgr = HistoryManager(tmp_path)
    mock_pm = MagicMock()
    mock_pm.resolve_movie.return_value = [MagicMock(server="Vidplay")]

    def fake_play(**kwargs):
        # Simulate mpv Lua script reaching EOF: writes completion marker & resets pos to 0.0
        mgr._save_completions({sample_movie.imdb_id: 1700000000})
        mgr.set_playback_position(sample_movie.imdb_id, 0.0)
        return 0

    mock_player = MagicMock()
    mock_player.play.side_effect = fake_play

    with patch("dion.cli.get_history_mgr", return_value=mgr), \
         patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
         patch("dion.cli.get_provider_manager", return_value=mock_pm), \
         patch("dion.cli.get_player", return_value=mock_player):
        mock_auth.return_value.is_authenticated.return_value = False

        success = play_or_download(sample_movie, episode=None, download=False, auto_select=True)
        assert success is True

    plays, total = mgr.list_plays()
    assert total == 1
    assert plays[0]["imdb_id"] == sample_movie.imdb_id
    # Marker must have been consumed so a subsequent quick exit won't reuse it
    assert mgr.consume_playback_completion(sample_movie.imdb_id) is False


# ---------------------------------------------------------------------------
# 3. Manual watched controls ('w' key actions)
# ---------------------------------------------------------------------------

def test_manual_mark_episode_and_single_play_removal_preserves_rewatches(tmp_path: Path, sample_show: MediaItem, sample_episodes: list[EpisodeItem]):
    """Marking an episode creates a play; removing one rewatch keeps the episode watched."""
    mgr = HistoryManager(tmp_path)
    ep = sample_episodes[1]  # S01E01

    # First watch
    mgr.record_play(sample_show, episode=ep, watched_at="2024-01-01T00:00:00Z")
    # Second watch (rewatch)
    mgr.record_play(sample_show, episode=ep, watched_at="2024-02-01T00:00:00Z")

    assert mgr.get_play_count(sample_show.imdb_id, season=1, episode=1) == 2
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=1) is True

    plays, _ = mgr.list_plays()
    newest_play_id = plays[0]["id"]

    # Delete only the newest play
    mgr.delete_play(newest_play_id)

    # One play remains; episode is still watched!
    assert mgr.get_play_count(sample_show.imdb_id, season=1, episode=1) == 1
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=1) is True

    # Clear all plays for episode
    mgr.clear_plays_for_episode(sample_show.imdb_id, season=1, episode=1)
    assert mgr.get_play_count(sample_show.imdb_id, season=1, episode=1) == 0
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=1) is False


def test_bulk_mark_season_preserves_existing_play_dates(tmp_path: Path, sample_show: MediaItem, sample_episodes: list[EpisodeItem]):
    """Marking season watched marks unplayed episodes while preserving existing play dates."""
    mgr = HistoryManager(tmp_path)
    ep1 = sample_episodes[1]  # S01E01
    ep2 = sample_episodes[2]  # S01E02
    ep3 = sample_episodes[3]  # S01E03

    # Already played ep1 on 2023-01-01
    mgr.record_play(sample_show, episode=ep1, watched_at="2023-01-01T00:00:00Z")

    w_count, tot_rel = mgr.get_season_watched_status(sample_show.imdb_id, season=1, episodes=sample_episodes)
    assert w_count == 1
    assert tot_rel == 3

    # Mark remaining unplayed episodes in season 1
    unplayed = [ep for ep in [ep1, ep2, ep3] if not mgr.is_watched(sample_show.imdb_id, 1, ep.episode)]
    assert len(unplayed) == 2  # E02 and E03

    mgr.mark_episodes_watched(sample_show, unplayed, watched_at="2024-05-01T00:00:00Z")

    # Now season 1 is completely watched
    w_count_after, tot_rel_after = mgr.get_season_watched_status(sample_show.imdb_id, season=1, episodes=sample_episodes)
    assert w_count_after == 3
    assert tot_rel_after == 3

    # Original play date for ep1 was preserved!
    plays, _ = mgr.list_plays(limit=10)
    by_ep = {p["episode"]: p for p in plays if p["season"] == 1}
    assert by_ep[1]["watched_at"] == "2023-01-01T00:00:00Z"
    assert by_ep[2]["watched_at"] == "2024-05-01T00:00:00Z"
    assert by_ep[3]["watched_at"] == "2024-05-01T00:00:00Z"


def test_bulk_mark_show_specials_opt_in(tmp_path: Path, sample_show: MediaItem, sample_episodes: list[EpisodeItem]):
    """Show-wide marking excludes specials (season 0) unless explicitly opted in."""
    mgr = HistoryManager(tmp_path)

    # Status without specials
    w_count, tot_rel = mgr.get_show_watched_status(sample_show.imdb_id, sample_episodes, include_specials=False)
    assert tot_rel == 4  # 3 in S1 + 1 in S2

    # Status with specials
    w_count_s, tot_rel_s = mgr.get_show_watched_status(sample_show.imdb_id, sample_episodes, include_specials=True)
    assert tot_rel_s == 5  # 1 in S0 + 3 in S1 + 1 in S2

    # Mark all excluding specials
    regular_eps = [ep for ep in sample_episodes if ep.season > 0]
    mgr.mark_episodes_watched(sample_show, regular_eps, watched_at="2024-06-01T00:00:00Z")

    assert mgr.is_watched(sample_show.imdb_id, season=0, episode=1) is False
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=1) is True
    assert mgr.is_watched(sample_show.imdb_id, season=2, episode=1) is True


def test_manual_mark_earlier_episodes(tmp_path: Path, sample_show: MediaItem, sample_episodes: list[EpisodeItem]):
    """'Mark earlier episodes watched' marks all released episodes prior to target episode."""
    mgr = HistoryManager(tmp_path)
    # S01E03 is target
    target = sample_episodes[3]

    earlier = [
        ep for ep in sample_episodes
        if (ep.season < target.season or (ep.season == target.season and ep.episode < target.episode))
        and ep.season > 0
        and not mgr.is_watched(sample_show.imdb_id, ep.season, ep.episode)
    ]
    assert len(earlier) == 2  # S01E01 and S01E02

    mgr.mark_episodes_watched(sample_show, earlier, watched_at="2024-04-01T00:00:00Z")
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=1) is True
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=2) is True
    assert mgr.is_watched(sample_show.imdb_id, season=1, episode=3) is False


# ---------------------------------------------------------------------------
# 4. Dedicated --history view
# ---------------------------------------------------------------------------

def test_dedicated_history_view_pagination_and_removal(tmp_path: Path, sample_movie: MediaItem):
    """History view presents plays newest first with pagination and w single-play removal."""
    mgr = HistoryManager(tmp_path)

    # Insert 25 plays
    for i in range(25):
        mgr.record_play(
            sample_movie,
            watched_at=f"2024-01-{i+1:02d}T10:00:00Z",
            queue_sync=False,
        )

    plays, total = mgr.list_plays(limit=15, offset=0)
    assert total == 25
    assert len(plays) == 15
    # Newest play first
    assert plays[0]["watched_at"] == "2024-01-25T10:00:00Z"

    # Second page
    page2, _ = mgr.list_plays(limit=15, offset=15)
    assert len(page2) == 10
    assert page2[-1]["watched_at"] == "2024-01-01T10:00:00Z"

    # Test removing one play
    newest_id = plays[0]["id"]
    mgr.delete_play(newest_id)

    _, total_after = mgr.list_plays()
    assert total_after == 24
    new_page, _ = mgr.list_plays(limit=15, offset=0)
    assert new_page[0]["watched_at"] == "2024-01-24T10:00:00Z"


# ---------------------------------------------------------------------------
# 5. Offline sync retry & durable removals/additions
# ---------------------------------------------------------------------------

def test_durable_offline_sync_queue_and_stale_remote_suppression(tmp_path: Path, mock_token: TraktToken, sample_movie: MediaItem):
    """Sync queue durably holds additions and removals, suppressing stale remote plays from reappearing."""
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)
    history_mgr = HistoryManager(config_dir=tmp_path)

    # 1. Offline add: play recorded while offline
    history_mgr.record_play(
        sample_movie,
        watched_at="2024-03-01T12:00:00Z",
        queue_sync=True,
    )
    pending_adds = history_mgr.db.get_pending_sync_actions("add")
    assert len(pending_adds) == 1
    assert pending_adds[0]["imdb_id"] == sample_movie.imdb_id

    # 2. Offline delete of a previously synced play while offline
    synced_play_id = history_mgr.record_play(
        sample_movie,
        watched_at="2024-02-01T12:00:00Z",
        trakt_play_id=55555,
        queue_sync=False,
    )
    history_mgr.delete_play(synced_play_id, queue_sync=True)

    pending_removals = history_mgr.db.get_pending_sync_actions("remove")
    assert len(pending_removals) == 1
    assert pending_removals[0]["imdb_id"] == sample_movie.imdb_id

    # 3. Simulate remote Trakt still returning the old deleted play
    stale_remote_history = [
        {
            "id": 55555,
            "type": "movie",
            "watched_at": "2024-02-01T12:00:00Z",
            "movie": {"title": sample_movie.title, "ids": {"imdb": sample_movie.imdb_id}},
        }
    ]

    with patch.object(sync_mgr.client, "remove_from_history", return_value={"deleted": {"movies": 1, "shows": 0, "episodes": 0}}) as mock_remove, \
         patch.object(sync_mgr.client, "add_to_history", return_value={"added": {"movies": 1, "shows": 0, "episodes": 0}}), \
         patch.object(sync_mgr.client, "get_history", return_value=stale_remote_history):

        sync_mgr.sync_history(history_mgr)
        assert mock_remove.called

    # Removals queue must now be cleared after successful Trakt acknowledgement
    assert len(history_mgr.db.get_pending_sync_actions("remove")) == 0

    # Stale remote play 55555 must NOT have been re-imported; only the offline-added play remains
    plays, total = history_mgr.list_plays()
    assert total == 1
    assert plays[0]["watched_at"] == "2024-03-01T12:00:00Z"


def test_sync_lock_prevents_overlapping_syncs(tmp_path: Path, mock_token: TraktToken):
    """Non-overlapping sync lock ensures only one sync runs at a time."""
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)
    history_mgr = HistoryManager(config_dir=tmp_path)

    # Acquire lock manually
    assert sync_mgr._sync_lock.acquire(blocking=False) is True
    try:
        # A second sync invocation should return immediately (0, 0) without overlapping
        pulled, pushed = sync_mgr.sync_history(history_mgr)
        assert pulled == 0
        assert pushed == 0
    finally:
        sync_mgr._sync_lock.release()


def test_last_successful_sync_time_tracking(tmp_path: Path, mock_token: TraktToken):
    """Last successful sync time is recorded and retrievable."""
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)
    history_mgr = HistoryManager(config_dir=tmp_path)

    assert sync_mgr.get_last_successful_sync_time(history_mgr) is None

    with patch.object(sync_mgr.client, "remove_from_history", return_value={"deleted": {}}), \
         patch.object(sync_mgr.client, "add_to_history", return_value={"added": {}}), \
         patch.object(sync_mgr.client, "get_history", return_value=[]), \
         patch.object(sync_mgr, "fetch_continue_watching", return_value=[]):
        sync_mgr.sync_history(history_mgr)

    last_sync = sync_mgr.get_last_successful_sync_time(history_mgr)
    assert last_sync is not None
    assert "T" in last_sync


# ---------------------------------------------------------------------------
# 6. Responsive launch with slow / unavailable Trakt connection
# ---------------------------------------------------------------------------

def test_responsive_launch_background_sync(tmp_path: Path, mock_token: TraktToken):
    """Interactive launch starts background thread for Trakt sync, never blocking the main thread."""
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    history_mgr = HistoryManager(config_dir=tmp_path)

    sync_started = False

    def slow_sync(self, *args, **kwargs):
        nonlocal sync_started
        sync_started = True
        time.sleep(0.1)
        return 1, 0

    with patch.object(TraktSyncManager, "sync_history", slow_sync):
        t0 = time.time()
        thread = start_background_sync(history_mgr, auth_mgr)
        elapsed = time.time() - t0

        # Starting background sync must return practically instantly (< 50ms)
        assert elapsed < 0.05
        assert thread is not None
        thread.join(timeout=2.0)
        assert sync_started is True


def test_single_play_removal_without_trakt_id_preserves_other_rewatches(
    tmp_path: Path, mock_token: TraktToken, sample_movie: MediaItem
):
    """Removing one play without a local trakt_play_id resolves its ID and never broad-removes rewatches."""
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)
    history_mgr = HistoryManager(config_dir=tmp_path)

    # Play 1 (older, has trakt_play_id) + Play 2 (newer rewatch, no trakt_play_id locally yet)
    history_mgr.record_play(
        sample_movie, watched_at="2024-01-01T12:00:00Z", trakt_play_id=101, queue_sync=False
    )
    play2_id = history_mgr.record_play(
        sample_movie, watched_at="2024-02-01T12:00:00Z", trakt_play_id=None, queue_sync=False
    )

    # User removes ONLY Play 2 in History
    history_mgr.delete_play(play2_id, queue_sync=True)

    remote_history = [
        {
            "id": 202,
            "type": "movie",
            "watched_at": "2024-02-01T12:00:00Z",
            "movie": {"title": sample_movie.title, "ids": {"imdb": sample_movie.imdb_id}},
        },
        {
            "id": 101,
            "type": "movie",
            "watched_at": "2024-01-01T12:00:00Z",
            "movie": {"title": sample_movie.title, "ids": {"imdb": sample_movie.imdb_id}},
        },
    ]

    with patch.object(sync_mgr.client, "get_history", return_value=remote_history), \
         patch.object(sync_mgr.client, "remove_from_history", return_value={"deleted": {"movies": 1}}) as mock_remove, \
         patch.object(sync_mgr, "fetch_continue_watching", return_value=[]):
        sync_mgr.sync_history(history_mgr)

    # Must have targeted ONLY play ID 202, never {"movies": ...}
    mock_remove.assert_called_once()
    sent_payload = mock_remove.call_args[0][1]
    assert sent_payload == {"ids": [202]}

    # Older rewatch (play 101) remains intact locally
    plays, total = history_mgr.list_plays()
    assert total == 1
    assert plays[0]["trakt_play_id"] == 101


def test_sync_does_not_mark_complete_on_failure_and_imports_over_2500_plays(
    tmp_path: Path, mock_token: TraktToken
):
    """Failed history requests must not mark initial sync complete, and >2500 plays must import completely."""
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)
    history_mgr = HistoryManager(config_dir=tmp_path)

    # 1. Simulate network/HTTP failure during get_history
    def failing_get_history(token, limit=50, page=1, start_at=None):
        sync_mgr.client._last_history_request_ok = False
        return []

    with patch.object(sync_mgr.client, "get_history", side_effect=failing_get_history), \
         patch.object(sync_mgr, "fetch_continue_watching", return_value=[]):
        sync_mgr.sync_history(history_mgr)

    assert history_mgr.db.get_meta("first_full_sync_completed") is None
    assert sync_mgr.get_last_successful_sync_time(history_mgr) is None

    # 2. Simulate full import of 2,600 plays across 26 pages of 100 items
    def large_get_history(token, limit=100, page=1, start_at=None):
        sync_mgr.client._last_history_request_ok = True
        sync_mgr.client._last_history_page_count = 26
        if page > 26:
            return []
        base = (page - 1) * 100
        return [
            {
                "id": 10000 + base + i,
                "type": "movie",
                "watched_at": f"2024-01-01T00:{(base + i) // 60:02d}:{(base + i) % 60:02d}Z",
                "movie": {"title": f"Movie {base + i}", "ids": {"imdb": f"tt{1000000 + base + i}"}},
            }
            for i in range(100)
        ]

    with patch.object(sync_mgr.client, "get_history", side_effect=large_get_history), \
         patch.object(sync_mgr, "fetch_continue_watching", return_value=[]):
        pulled, _ = sync_mgr.sync_history(history_mgr)

    assert pulled == 2600
    _, total = history_mgr.list_plays(limit=1)
    assert total == 2600
    assert history_mgr.db.get_meta("first_full_sync_completed") == "1"


def test_expired_token_does_not_block_launch_and_notifies_open_views(tmp_path: Path):
    """Expired Trakt tokens refresh inside the background thread and notify open views on page save."""
    from dion.trakt.background import (
        register_sync_refresh_listener,
        unregister_sync_refresh_listener,
    )

    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    expired_token = TraktToken(
        access_token="old_access",
        token_type="bearer",
        expires_in=3600,
        refresh_token="refresh_tok",
        scope="public",
        created_at=1000,  # expired
        username="testuser",
    )
    auth_mgr._save_token(expired_token)
    history_mgr = HistoryManager(config_dir=tmp_path)

    refreshed_token = TraktToken(
        access_token="new_access",
        token_type="bearer",
        expires_in=7776000,
        refresh_token="refresh_tok_2",
        scope="public",
        created_at=int(time.time()),
        username="testuser",
    )

    def slow_refresh(refresh_tok):
        time.sleep(0.1)
        return refreshed_token

    refresh_notifications = []
    listener = lambda: refresh_notifications.append(True)
    register_sync_refresh_listener(listener)

    try:
        with patch.object(auth_mgr.client, "refresh_token", side_effect=slow_refresh) as mock_refresh, \
             patch.object(auth_mgr.client, "get_history", return_value=[{"id": 1, "type": "movie", "watched_at": "2024-01-01T00:00:00Z", "movie": {"title": "M", "ids": {"imdb": "tt1"}}}]), \
             patch.object(TraktSyncManager, "fetch_continue_watching", return_value=[]):
            assert auth_mgr.has_credentials() is True
            t0 = time.time()
            thread = start_background_sync(history_mgr, auth_mgr)
            elapsed = time.time() - t0

            # Launch must not block waiting on slow_refresh
            assert elapsed < 0.05
            assert thread is not None
            thread.join(timeout=2.0)

        assert mock_refresh.called
        assert len(refresh_notifications) >= 1
    finally:
        unregister_sync_refresh_listener(listener)


def test_cli_short_help_and_uppercase_history_flags(tmp_path: Path):
    """Verify -h runs --help while -H runs --history."""
    from typer.testing import CliRunner
    from dion.cli import app

    runner = CliRunner()
    history_mgr = HistoryManager(config_dir=tmp_path)

    with patch("dion.cli.get_history_mgr", return_value=history_mgr), \
         patch("dion.cli.get_trakt_auth_mgr") as mock_auth, \
         patch("dion.history_view.select_from_history", return_value=None) as mock_select_history:
        mock_auth.return_value.has_credentials.return_value = False

        import re
        res_h = runner.invoke(app, ["-h"])
        assert res_h.exit_code == 0
        assert re.search(r"--history\s+-H", res_h.stdout)
        assert re.search(r"--help\s+-h", res_h.stdout)
        mock_select_history.assert_not_called()

        res_H = runner.invoke(app, ["-H"])
        assert res_H.exit_code == 0
        mock_select_history.assert_called_once_with(history_mgr)

        res_long = runner.invoke(app, ["--history"])
        assert res_long.exit_code == 0
        assert mock_select_history.call_count == 2
