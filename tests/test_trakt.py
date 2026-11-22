from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import httpx

from dion.metadata.models import EpisodeItem, MediaItem, MediaType
from dion.storage.history import HistoryManager
from dion.storage.settings import SettingsManager
from dion.trakt.auth import TraktAuthManager
from dion.trakt.client import TraktClient
from dion.trakt.models import (
    TraktDeviceCode,
    TraktToken,
    format_trakt_scrobble_payload,
)
from dion.trakt.scrobbler import TraktScrobbler
from dion.trakt.sync import TraktSyncManager, parse_iso_datetime


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
def sample_episode() -> EpisodeItem:
    return EpisodeItem(
        id="tt0903747:1:1",
        season=1,
        episode=1,
        title="Pilot",
        runtime="58 min",
    )


@pytest.fixture
def mock_token() -> TraktToken:
    return TraktToken(
        access_token="test_access_token",
        refresh_token="test_refresh_token",
        expires_in=7776000,
        created_at=int(time.time()),
        username="testuser",
    )


# ---------------------------------------------------------------------------
# Models & Payload Formatting Tests
# ---------------------------------------------------------------------------

def test_token_expiry_check(mock_token: TraktToken):
    # Fresh token is not expired
    assert not mock_token.is_expired(buffer_seconds=3600)

    # Token created far in the past is expired
    old_token = TraktToken(
        access_token="old_token",
        expires_in=3600,
        created_at=int(time.time()) - 4000,
    )
    assert old_token.is_expired()


def test_format_trakt_scrobble_payload_movie(sample_movie: MediaItem):
    payload = format_trakt_scrobble_payload(sample_movie, episode=None, progress=45.5)
    assert "movie" in payload
    assert payload["movie"]["title"] == "Fight Club"
    assert payload["movie"]["year"] == 1999
    assert payload["movie"]["ids"]["imdb"] == "tt0137523"
    assert payload["progress"] == 45.5


def test_format_trakt_scrobble_payload_episode(sample_show: MediaItem, sample_episode: EpisodeItem):
    payload = format_trakt_scrobble_payload(sample_show, episode=sample_episode, progress=85.0)
    assert "show" in payload
    assert payload["show"]["title"] == "Breaking Bad"
    assert payload["show"]["ids"]["imdb"] == "tt0903747"
    assert "episode" in payload
    assert payload["episode"]["season"] == 1
    assert payload["episode"]["number"] == 1
    assert payload["progress"] == 85.0


def test_format_trakt_scrobble_payload_bounds(sample_movie: MediaItem):
    # Clamps below 0 and above 100
    p_low = format_trakt_scrobble_payload(sample_movie, progress=-10.0)
    assert p_low["progress"] == 0.0

    p_high = format_trakt_scrobble_payload(sample_movie, progress=120.0)
    assert p_high["progress"] == 100.0


# ---------------------------------------------------------------------------
# TraktClient Tests
# ---------------------------------------------------------------------------

def test_client_request_device_code():
    client = TraktClient()
    mock_resp = {
        "device_code": "dev123",
        "user_code": "WDGLPBMQ",
        "verification_url": "https://trakt.tv/activate",
        "expires_in": 600,
        "interval": 5,
    }
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = httpx.Response(
            200,
            json=mock_resp,
            request=httpx.Request("POST", "https://api.trakt.tv/oauth/device/code"),
        )
        code = client.request_device_code()
        assert code.device_code == "dev123"
        assert code.user_code == "WDGLPBMQ"
        assert code.verification_url == "https://trakt.tv/activate"
        assert code.expires_in == 600
        assert code.interval == 5


# ---------------------------------------------------------------------------
# CLI & Download Isolation Tests
# ---------------------------------------------------------------------------

def test_download_isolation_from_trakt(sample_movie: MediaItem):
    """Verify that download=True strictly bypasses Trakt scrobbler and sync."""
    from dion.cli import play_or_download
    from dion.metadata.models import StreamSource

    dummy_source = StreamSource(url="https://example.com/stream.mp4", server="vidlink", is_hls=False)

    with patch("dion.cli.get_provider_manager") as mock_pm, \
         patch("dion.cli.get_downloader") as mock_dl, \
         patch("dion.cli.get_trakt_scrobbler") as mock_scrobbler:

        mock_pm.return_value.resolve_movie.return_value = [dummy_source]
        mock_dl.return_value.download.return_value = 0

        # Run with download=True
        success = play_or_download(
            media=sample_movie,
            download=True,
            auto_select=True,
        )

        assert success is True
        assert mock_dl.return_value.download.called
        # Scrobbler must NEVER be called during download
        assert not mock_scrobbler.called


def test_cli_trakt_status(tmp_path: Path, mock_token: TraktToken):
    """Verify 'dion trakt status' outputs connection state and exits cleanly."""
    from typer.testing import CliRunner
    from dion.cli import app

    runner = CliRunner()
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)

    with patch("dion.cli.get_trakt_auth_mgr", return_value=auth_mgr):
        result = runner.invoke(app, ["trakt", "status"])
        assert result.exit_code == 0
        assert "@testuser" in result.stdout



def test_client_poll_device_token_states():
    client = TraktClient()
    with patch("httpx.Client.post") as mock_post:
        # Success (200)
        mock_post.return_value = httpx.Response(
            200,
            json={
                "access_token": "acc123",
                "refresh_token": "ref123",
                "expires_in": 7776000,
                "created_at": int(time.time()),
            },
        )
        token, state = client.poll_device_token("dev123")
        assert state == "success"
        assert token is not None
        assert token.access_token == "acc123"

        # Pending (400)
        mock_post.return_value = httpx.Response(400)
        token, state = client.poll_device_token("dev123")
        assert state == "pending"
        assert token is None

        # Slow down (429)
        mock_post.return_value = httpx.Response(429)
        token, state = client.poll_device_token("dev123")
        assert state == "slow_down"

        # Expired (410)
        mock_post.return_value = httpx.Response(410)
        token, state = client.poll_device_token("dev123")
        assert state == "expired"


def test_client_refresh_token():
    client = TraktClient()
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = httpx.Response(
            200,
            json={
                "access_token": "new_acc",
                "refresh_token": "new_ref",
                "expires_in": 7776000,
                "created_at": int(time.time()),
            },
        )
        new_token = client.refresh_token("old_ref")
        assert new_token is not None
        assert new_token.access_token == "new_acc"
        assert new_token.refresh_token == "new_ref"


def test_client_scrobble_actions(sample_movie: MediaItem):
    client = TraktClient()
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = httpx.Response(201, json={"id": 123, "action": "start"})
        res = client.scrobble_start("tok", sample_movie, progress=0.0)
        assert res is not None
        assert res["action"] == "start"

        mock_post.return_value = httpx.Response(201, json={"id": 123, "action": "scrobble"})
        res = client.scrobble_stop("tok", sample_movie, progress=90.0)
        assert res is not None
        assert res["action"] == "scrobble"


# ---------------------------------------------------------------------------
# TraktAuthManager Tests
# ---------------------------------------------------------------------------

def test_auth_manager_persistence(tmp_path: Path, mock_token: TraktToken):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    assert not auth_mgr.is_authenticated()

    auth_mgr._save_token(mock_token)
    assert auth_mgr.is_authenticated()
    assert auth_mgr.get_valid_token() == "test_access_token"
    assert auth_mgr.get_username() == "testuser"

    # Reload from disk
    reloaded = TraktAuthManager(config_dir=tmp_path)
    assert reloaded.is_authenticated()
    assert reloaded.get_valid_token() == "test_access_token"
    assert reloaded.get_username() == "testuser"

    # Logout
    with patch.object(reloaded.client, "revoke_token", return_value=True):
        reloaded.logout()
    assert not reloaded.is_authenticated()
    assert not reloaded.auth_file.exists()


def test_auth_manager_auto_refresh(tmp_path: Path):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    # Expired token
    expired_token = TraktToken(
        access_token="expired_acc",
        refresh_token="valid_ref",
        expires_in=100,
        created_at=int(time.time()) - 500,
        username="user123",
    )
    auth_mgr._save_token(expired_token)

    refreshed_token = TraktToken(
        access_token="fresh_acc",
        refresh_token="fresh_ref",
        expires_in=7776000,
        created_at=int(time.time()),
    )
    with patch.object(auth_mgr.client, "refresh_token", return_value=refreshed_token) as mock_refresh:
        valid_tok = auth_mgr.get_valid_token()
        assert valid_tok == "fresh_acc"
        assert mock_refresh.called


# ---------------------------------------------------------------------------
# TraktScrobbler Tests
# ---------------------------------------------------------------------------

def test_scrobbler_start_and_stop(tmp_path: Path, mock_token: TraktToken, sample_movie: MediaItem):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)

    scrobbler = TraktScrobbler(auth_mgr=auth_mgr)

    with patch.object(scrobbler.client, "scrobble_start") as mock_start, \
         patch.object(scrobbler.client, "scrobble_stop") as mock_stop:

        scrobbler.start_playback(media=sample_movie, start_time=120.0, duration=1000.0)
        time.sleep(0.05)
        assert mock_start.called

        # Stop at 90% progress
        scrobbler.stop_playback(media=sample_movie, current_time=900.0, duration=1000.0)
        assert mock_stop.called
        call_kwargs = mock_stop.call_args[1]
        assert round(call_kwargs["progress"], 1) == 90.0


def test_scrobbler_disabled_or_unauthenticated(tmp_path: Path, sample_movie: MediaItem):
    # Unauthenticated
    unauth_mgr = TraktAuthManager(config_dir=tmp_path)
    scrobbler = TraktScrobbler(auth_mgr=unauth_mgr)
    with patch.object(scrobbler.client, "scrobble_start") as mock_start:
        scrobbler.start_playback(media=sample_movie)
        time.sleep(0.05)
        assert not mock_start.called

    # Authenticated but disabled
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(TraktToken(access_token="tok"))
    disabled_scrobbler = TraktScrobbler(auth_mgr=auth_mgr, enabled=False)
    with patch.object(disabled_scrobbler.client, "scrobble_start") as mock_start:
        disabled_scrobbler.start_playback(media=sample_movie)
        time.sleep(0.05)
        assert not mock_start.called


# ---------------------------------------------------------------------------
# TraktSyncManager & 60-Day Recency Filter Tests
# ---------------------------------------------------------------------------

def test_fetch_continue_watching_60_day_filter(tmp_path: Path, mock_token: TraktToken):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)

    now = datetime.now(timezone.utc)
    recent_date = (now - timedelta(days=10)).isoformat()
    old_date = (now - timedelta(days=75)).isoformat()  # > 60 days old!

    mock_playback = [
        {
            "id": 1,
            "type": "movie",
            "progress": 50.0,
            "paused_at": recent_date,
            "movie": {
                "title": "Recent Movie",
                "year": 2024,
                "runtime": 120,
                "ids": {"imdb": "tt1111111"},
            },
        },
        {
            "id": 2,
            "type": "movie",
            "progress": 30.0,
            "paused_at": old_date,
            "movie": {
                "title": "Old Movie",
                "year": 2020,
                "runtime": 100,
                "ids": {"imdb": "tt2222222"},
            },
        },
        {
            "id": 3,
            "type": "episode",
            "progress": 70.0,
            "paused_at": recent_date,
            "show": {
                "title": "Recent Show",
                "year": 2023,
                "ids": {"imdb": "tt3333333"},
            },
            "episode": {
                "season": 2,
                "number": 4,
                "title": "Episode 4",
                "runtime": 45,
            },
        },
    ]

    with patch.object(sync_mgr.client, "get_playback", return_value=mock_playback):
        items = sync_mgr.fetch_continue_watching(max_age_days=60)
        # Old movie (>60 days) must be excluded!
        assert len(items) == 2
        imdb_ids = [i["imdb_id"] for i in items]
        assert "tt1111111" in imdb_ids
        assert "tt3333333" in imdb_ids
        assert "tt2222222" not in imdb_ids  # Excluded!


def test_merge_continue_watching_deduplication(tmp_path: Path, mock_token: TraktToken):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)

    now = datetime.now(timezone.utc)
    t_newer = (now - timedelta(days=2)).isoformat()
    t_older = (now - timedelta(days=5)).isoformat()

    # Local item has older progress
    local_items = [
        {
            "imdb_id": "tt1111111",
            "title": "Movie 1",
            "media_type": "movie",
            "position": 100.0,
            "duration": 1000.0,
            "last_watched": t_older,
        },
        {
            "imdb_id": "tt9999999",
            "title": "Local Only Movie",
            "media_type": "movie",
            "position": 50.0,
            "duration": 500.0,
            "last_watched": t_newer,
        },
    ]

    trakt_items = [
        {
            "imdb_id": "tt1111111",
            "title": "Movie 1",
            "media_type": "movie",
            "position": 600.0,  # Newer on Trakt!
            "duration": 1000.0,
            "last_watched": t_newer,
            "progress_pct": 60.0,
            "from_trakt": True,
        },
        {
            "imdb_id": "tt8888888",
            "title": "Trakt Only Movie",
            "media_type": "movie",
            "position": 300.0,
            "duration": 900.0,
            "last_watched": t_newer,
            "progress_pct": 33.3,
            "from_trakt": True,
        },
    ]

    with patch.object(sync_mgr, "fetch_continue_watching", return_value=trakt_items):
        merged = sync_mgr.merge_continue_watching(local_items, max_age_days=60)
        assert len(merged) == 3

        merged_map = {m["imdb_id"]: m for m in merged}
        # Duplicate tt1111111 should have taken the newer Trakt position (600.0)
        assert merged_map["tt1111111"]["position"] == 600.0
        # Local-only and Trakt-only items are both present
        assert "tt9999999" in merged_map
        assert "tt8888888" in merged_map


def test_fetch_watchlist_media(tmp_path: Path, mock_token: TraktToken):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)

    mock_watchlist = [
        {
            "type": "movie",
            "movie": {
                "title": "Inception",
                "year": 2010,
                "overview": "A thief who steals corporate secrets...",
                "rating": 8.8,
                "ids": {"imdb": "tt1375666"},
            },
        },
        {
            "type": "show",
            "show": {
                "title": "Severance",
                "year": 2022,
                "overview": "Mark leads a team of office workers...",
                "rating": 8.7,
                "ids": {"imdb": "tt11280740"},
            },
        },
    ]

    with patch.object(sync_mgr.client, "get_watchlist", return_value=mock_watchlist):
        items = sync_mgr.fetch_watchlist_media()
        assert len(items) == 2
        assert items[0].title == "Inception"
        assert items[0].media_type == MediaType.MOVIE
        assert items[0].imdb_id == "tt1375666"

        assert items[1].title == "Severance"
        assert items[1].media_type == MediaType.SERIES
        assert items[1].imdb_id == "tt11280740"


def test_two_way_history_sync(tmp_path: Path, mock_token: TraktToken):
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)
    sync_mgr = TraktSyncManager(auth_mgr=auth_mgr)

    history_mgr = HistoryManager(config_dir=tmp_path)
    # Add 1 local item
    history_mgr.record_watch(
        MediaItem(id="ttLocal1", imdb_id="ttLocal1", title="Local Movie", media_type=MediaType.MOVIE)
    )

    mock_trakt_history = [
        {
            "id": 101,
            "type": "movie",
            "watched_at": "2024-01-01T00:00:00Z",
            "movie": {
                "title": "Trakt Remote Movie",
                "ids": {"imdb": "ttRemote1"},
            },
        }
    ]

    with patch.object(sync_mgr.client, "get_history", return_value=mock_trakt_history), \
         patch.object(sync_mgr.client, "add_to_history", return_value={"added": {"movies": 1, "episodes": 0}}) as mock_push:

        pulled, pushed = sync_mgr.sync_history(history_mgr)
        assert pulled == 1
        assert pushed == 1
        assert mock_push.called

        # Remote item should now be in local history
        local_ids = [i["imdb_id"] for i in history_mgr.list_history()]
        assert "ttRemote1" in local_ids
        assert "ttLocal1" in local_ids


def test_cli_trakt_sync_command(tmp_path: Path, mock_token: TraktToken):
    from typer.testing import CliRunner
    from dion.cli import app

    runner = CliRunner()
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)

    with patch("dion.cli.get_trakt_auth_mgr", return_value=auth_mgr), \
         patch("dion.cli.get_trakt_sync_mgr") as mock_sync_mgr:
        mock_sync_mgr.return_value.sync_history.return_value = (3, 2)
        result = runner.invoke(app, ["trakt", "sync"])
        assert result.exit_code == 0
        assert "Pulled 3 titles, pushed 2 titles" in result.stdout


def test_cli_trakt_logout_command(tmp_path: Path, mock_token: TraktToken):
    from typer.testing import CliRunner
    from dion.cli import app

    runner = CliRunner()
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)

    with patch("dion.cli.get_trakt_auth_mgr", return_value=auth_mgr):
        result = runner.invoke(app, ["trakt", "logout"])
        assert result.exit_code == 0
        assert "Disconnected from Trakt.tv" in result.stdout
        assert not auth_mgr.is_authenticated()


def test_cli_watchlist_empty(tmp_path: Path, mock_token: TraktToken):
    from typer.testing import CliRunner
    from dion.cli import app

    runner = CliRunner()
    auth_mgr = TraktAuthManager(config_dir=tmp_path)
    auth_mgr._save_token(mock_token)

    with patch("dion.cli.get_trakt_auth_mgr", return_value=auth_mgr), \
         patch("dion.cli.get_trakt_sync_mgr") as mock_sync_mgr:
        mock_sync_mgr.return_value.fetch_watchlist_media.return_value = []
        result = runner.invoke(app, ["-w"])
        assert result.exit_code == 0
        assert "Watchlist is empty" in result.stdout

